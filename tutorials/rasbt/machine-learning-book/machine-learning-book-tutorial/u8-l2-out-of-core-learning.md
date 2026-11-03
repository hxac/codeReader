# u8-l2 out-of-core 流式学习

## 1. 本讲目标

上一讲（u8-l1）我们把全部 50,000 条 IMDb 影评一次性装进内存，用 `TfidfVectorizer` + `GridSearchCV` 训练逻辑回归。本讲解决的问题是：**当数据大到内存装不下时，如何用同一套 scikit-learn 接口继续训练？**

学完本讲，你应该能够：

1. 解释 out-of-core（核外）学习与批学习（batch learning）的区别，以及「内存占用不随数据总量增长」这一核心性质。
2. 读懂并会写 `stream_docs` 生成器：用 `yield` 把一个 CSV 文件变成逐条产出的惰性数据流。
3. 理解 `get_minibatch` 如何从流中切出小批量，以及 `StopIteration` 的处理方式。
4. 掌握 `HashingVectorizer` 的哈希技巧：为什么它不需要词表、维度固定为 `2**21`，以及哈希冲突的代价。
5. 掌握 `SGDClassifier.partial_fit` + `classes` 参数的在线更新流程，并能复现书中 45 个批次、最终准确率约 0.868 的实验。

## 2. 前置知识

### 2.1 批学习 vs 在线（增量）学习

- **批学习（batch learning）**：`fit(X, y)` 一次性吃下全部数据，训练期间整个数据矩阵都必须在内存里。上一讲的 `LogisticRegression(solver='liblinear')` 就是批学习。
- **在线学习（online / incremental learning）**：数据一批一批到来，模型每来一批就更新一次参数，永远不需要同时看到全部数据。本讲的 `partial_fit` 就是 scikit-learn 的在线学习入口。

你在 u2-l3 已经见过 `partial_fit`（AdalineSGD 的流式更新）。本讲是它在真实工程场景（大文本文件）中的完整应用。

### 2.2 生成器与 yield：惰性计算

普通函数调用后立刻执行完毕并返回结果；含 `yield` 的函数调用后返回一个**生成器对象**（generator），它只在你每次向它要数据（`next()` 或 `for` 循环）时才计算并产出一个值，然后**暂停**，把执行位置、局部变量都冻结起来，直到下一次要数据。这样：

- 文件不需要一次读入内存，读多少、算多少；
- 内存里任何时刻只有「当前正在处理的那个值」。

### 2.3 一个直观的内存账

上一讲的词袋矩阵是「文档 × 词表」的稀疏矩阵。批学习要求这个矩阵**整体**存在；而流式学习把问题切成 45 份，每份只有 1000 条文档——不管文件总共有 5 万条还是 5 亿条，内存峰值都只由「批大小」决定。这就是 out-of-core（数据在内存「核心」之外，比如磁盘上）的含义。

### 2.4 术语表

| 术语 | 含义 |
| --- | --- |
| out-of-core learning | 数据不完全装入内存、逐块读取训练的学习方式 |
| 在线学习 / 增量学习 | 模型随数据流分批更新，而非一次性拟合 |
| 生成器（generator） | 含 `yield` 的函数返回的惰性迭代器 |
| 小批量（minibatch） | 每次喂给模型的一小撮样本（本讲为 1000 条） |
| 哈希技巧（hashing trick） | 用哈希函数把词直接映射到固定维度向量的下标，无需维护词表 |
| `partial_fit` | scikit-learn 估计器的增量训练方法，与 `fit` 相反不清空已有参数 |

## 3. 本讲源码地图

本讲核心代码全部在 `ch08` 目录：

| 文件 | 作用 |
| --- | --- |
| [ch08/ch08.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py) | 第 8 章主脚本（与 ch08.ipynb 等价）。out-of-core 一节位于文件后半部（约 L580–L664） |
| [ch08/ch08.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb) | 章主 notebook，保存了代码的真实运行输出（如 `Accuracy: 0.868`），并含一个 .py 里没有的「解压 movie_data.csv.gz」单元 |
| [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz) | 仓库自带的压缩版 50,000 条影评数据，解压即得 movie_data.csv，无需去 Stanford 下载 80MB 原始数据集 |

`ch08.py` 中本讲相关的行号速查（均已在当前 HEAD 核实）：

- L100–L130：数据下载（`reporthook`）与解压
- L148–L184：从 aclImdb 目录拼装 DataFrame 并写出 movie_data.csv（含打乱）
- L355–L361：`preprocessor` 文本清洗函数（正则）
- L584–L600：停用词、独立版 `tokenizer`、生成器 `stream_docs`
- L610–L619：`get_minibatch` 切批
- L626–L635：`HashingVectorizer` 与 `SGDClassifier` 的构造
- L638–L664：在线训练主循环、测试评估、最后的增量更新

## 4. 核心概念与源码讲解

### 4.1 数据从哪来：下载回调与 CSV 的来历

#### 4.1.1 概念说明

流式学习的第一个前提是「数据在磁盘上、按顺序可读」。本模块先弄清 `movie_data.csv` 这个文件是怎么来的、里面长什么样——后面 `stream_docs` 的解析逻辑完全建立在它的格式之上。

#### 4.1.2 核心流程

```text
Stanford aclImdb 压缩包（约 80MB）
   │  urllib.request.urlretrieve + reporthook（进度回调）
   ▼
解压出 50,000 个 txt（train/test × pos/neg 各 12,500）
   │  逐文件读入 DataFrame，打乱行序
   ▼
movie_data.csv：表头 review,sentiment + 50,000 行
   每行 = 一条影评原文 + 行尾固定的 ",0" 或 ",1"
```

关键事实（后面解析要依赖）：CSV 落盘发生在文本清洗**之前**，所以磁盘上的评论是**未清洗的原始文本**（含 HTML 标签、标点、大小写混杂）。

#### 4.1.3 源码精读

先看下载部分。`reporthook` 是传给 `urlretrieve` 的回调函数，每收到一个数据块被调用一次，用 `global start_time` 记住起始时刻，然后实时打印百分比、已下载量、速度：

- [ch08/ch08.py:106-118](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L106-L118) — `reporthook(count, block_size, total_size)`：`count` 是已收到的块数，`progress_size = count * block_size` 换算成字节数；`count == 0` 的第一次调用只记时不算速度，直接 `return`；`\r` 回车符让进度条在同一行刷新。
- [ch08/ch08.py:121-122](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L121-L122) — 只有当本地既没有解压目录也没有压缩包时才触发下载：`urllib.request.urlretrieve(source, target, reporthook)`，第三个参数就是上面的回调。

再看 CSV 是怎么拼出来的：

- [ch08/ch08.py:153-168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L153-L168) — 四层循环遍历 `test/train × pos/neg` 四个目录，逐个读 txt 原文进 DataFrame，`labels = {'pos': 1, 'neg': 0}`（L150）把子目录名变成 0/1 标签，`pyprind.ProgBar(50000)`（L151）显示进度。
- [ch08/ch08.py:176-177](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L176-L177) — **打乱行序**。这一步对流式学习至关重要：`stream_docs` 是按文件行序线性读取的，无法再打乱；如果 CSV 按类别排序（前 25,000 行全是 1、后 25,000 行全是 0），在线模型会先「学成只会预测 1」再被后半部分扭转，训练近乎失败。这和 u2-l3 中 AdalineSGD 每轮 shuffle 的动机完全一致，只是这里把打乱**前置到了造数据阶段**。
- [ch08/ch08.py:184](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L184) — `df.to_csv('movie_data.csv', index=False, encoding='utf-8')` 写盘。
- [ch08/ch08.py:355-361](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L355-L361) — `preprocessor`：剥 HTML 标签 `<[^>]*>`、抽表情符号、`\W+` 压成空格、小写化。注意 [L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L376) 的 `apply(preprocessor)` 作用在 L190 重新 `read_csv` 进内存的 df 上，**晚于 L184 的落盘**——所以磁盘上的 movie_data.csv 是未清洗的。

最后两个实用细节：

- 仓库已自带压缩数据 [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz)，无需真的下载 aclImdb。
- `ch08.py` 假设 `movie_data.csv` 已在工作目录；notebook 里有一个 .py 中**没有**的补丁单元，负责从 gz 解压出 csv：见 [ch08/ch08.ipynb:1351-1361](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb#L1351-L1361)，用 `gzip.open('movie_data.csv.gz','rb')` 读、`open('movie_data.csv','wb')` 写，一次性解压。

#### 4.1.4 代码实践

1. **实践目标**：确认数据可用、格式符合 `stream_docs` 的假设。
2. **操作步骤**（在 `ch08/` 目录下、u1-l2 搭建的 pyml-book 环境中）：

```bash
cd ch08
gunzip -k movie_data.csv.gz        # 保留 gz，得到 movie_data.csv
head -c 300 movie_data.csv         # 看表头和第一条评论的开头
```

   再用 Python 验证行数与标签取值（示例代码）：

```python
import csv
with open('movie_data.csv', 'r', encoding='utf-8') as f:
    reader = csv.reader(f)
    header = next(reader)
    labels = set()
    n = 0
    for row in reader:
        labels.add(row[-1])
        n += 1
print(header, n, labels)
```

3. **需要观察的现象**：表头为 `['review', 'sentiment']`；数据行共 50,000 行；标签集合为 `{'0', '1'}`；评论原文里能看到 `<br />` 之类的 HTML 残留（证明未清洗）。
4. **预期结果**：`(50000, {'0','1'})`。具体输出**待本地验证**（本讲义编写环境未运行该脚本）。

#### 4.1.5 小练习与答案

**练习 1**：为什么磁盘上的 movie_data.csv 里的评论含有 HTML 标签，而上一讲网格搜索用的数据却是干净的？

**答案**：[L184 的 `to_csv`](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L184) 先落盘，[L376 的 `apply(preprocessor)`](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L376) 后执行且只改内存中的 df，从未写回磁盘。因此本讲从文件流式读取的是**原始**评论，清洗必须在本讲流程里重新做（见 4.2 的独立版 tokenizer）。

**练习 2**：`reporthook` 中 `if count == 0: start_time = time.time(); return` 这两行为什么不能省略？

**答案**：第一次回调发生在第 0 块、尚未累计任何下载量时，若不单独处理，`duration` 约为 0 会导致除零，百分比也毫无意义。所以首块只记录起始时刻并立即返回，从第二块起才有「已用时间」可算速度。

**练习 3**：如果不打乱（L176–L177）直接落盘，本讲的在线训练会发生什么？

**答案**：CSV 将按 `test/pos, test/neg, train/pos, train/neg` 的块状顺序排列，前 12,500 行标签全为 1。`partial_fit` 逐批喂入时，模型在很长时间内只见到单一类别，学到的决策边界严重偏斜，后面的批次又把它拉向另一侧，最终性能远差于打乱后的情形。流式学习无法在读文件时打乱，所以**打乱必须在数据落盘前完成**。

### 4.2 stream_docs：用 yield 把 CSV 变成惰性数据流

#### 4.2.1 概念说明

`stream_docs` 是本讲的「数据管道入口」：一个生成器函数，每次被要数据时从文件读一行、解析出（评论文本, 标签）二元组并 `yield` 出去。它解决的问题是：让「读 50,000 条数据」变成「读 1 条、给 1 条」，内存里永远只有当前这一条。

#### 4.2.2 核心流程

```text
调用 next(doc_stream)
   │
   ▼
打开文件（首次）→ 跳过表头（首次）
   │
   ▼
for line in csv:          ← 文件对象本身也是迭代器，逐行惰性读取
       取 line[:-3] 为评论文本     （剥掉行尾 ",1\n" 三个字符）
       取 int(line[-2]) 为标签     （倒数第二个字符就是 0/1）
       yield (text, label)        ← 交出数据并在此冻结
   │
   ▼
下次 next() 从冻结处继续读下一行；文件读完抛 StopIteration
```

行尾结构是理解解析式的关键。每行以 `评论文本` + `","` + `"0或1"` + `"\n"` 结尾，因此 `line[-1]` 是换行符、`line[-2]` 是标签字符、`line[-3]` 是逗号；`line[:-3]` 恰好是纯文本。

#### 4.2.3 源码精读

- [ch08/ch08.py:595-600](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L595-L600) — `stream_docs(path)` 全部实现只有 6 行：`with open(...)` 保证生成器销毁时文件句柄关闭；`next(csv)` 跳过表头行；`for line in csv` 逐行迭代；切片解析后 `yield text, label`。注意 `yield` 使整个函数成为生成器函数——调用 `stream_docs(...)` 不会执行任何函数体，只返回生成器对象。
- [ch08/ch08.py:605](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L605) — `next(stream_docs(path='movie_data.csv'))` 是作者的冒烟测试：取第一条检查解析是否正确。notebook 中的真实输出显示第一条评论以 `"In 1974, the teenager Martha Moxley...` 开头、标签为 `1`，文本中确实混着 `<br /><br />`。
- 配套的清洗 tokenizer 在 [ch08/ch08.py:587-592](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L587-L592)：剥 HTML、抽表情符号、小写化压空格（与 L355 的 `preprocessor` 同一套正则），再额外滤掉 [L584](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L584) 定义的停用词 `stop`。它被故意重定义为「独立版」，让本节不依赖前文执行；清洗被放进 tokenizer 而不是 `preprocessor` 参数（见 4.4）。

一个需要点破的隐含假设：`line[:-3]` / `int(line[-2])` 的解析**假设每条评论恰好占一行**。IMDb 数据的评论本身不含换行符，这个假设成立；若换成内嵌换行的数据集（pandas 写 CSV 时会把字段内的换行包在引号里），逐行读取就会错位、`int()` 报错。工程上更稳妥的做法是用 `csv` 模块按字段解析。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到生成器「要一条、给一条」的惰性行为。
2. **操作步骤**（示例代码，可直接在 ch08 目录的 notebook 单元里跑）：

```python
from ch08_stream import stream_docs   # 或直接把 L595-600 的函数复制进来

doc_stream = stream_docs(path='movie_data.csv')
for i in range(3):
    text, label = next(doc_stream)
    print(f'--- 第 {i+1} 条, label={label}, 长度={len(text)} ---')
    print(text[:80])
```

3. **需要观察的现象**：三条文本互不相同；标签为 0/1；文本开头能看到引号、HTML 等原始字符。
4. **预期结果**：第一条以 `"In 1974...` 开头、label=1（与 notebook 中的输出一致）；后两条内容**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`text, label = line[:-3], int(line[-2])` 中，若某行末尾是 `...Available",1\n`，切片分别取到什么？

**答案**：`line[-1]` 为 `'\n'`，`line[-2]` 为 `'1'`（`int` 后标签为 1），`line[-3]` 为 `','`；`line[:-3]` 取到 `...Available"` 为止的评论文本，正好剥掉 `,1\n` 三个字符。

**练习 2**：为什么生成器函数体内的 `with open(...)` 不会在读第一条后就关闭文件？

**答案**：`yield` 不是 `return`：它交出值后把整个帧（包括打开的文件对象、循环位置）挂起而不是销毁，下次 `next()` 从挂起点继续。只有函数正常结束（文件读完）或生成器被垃圾回收 / `close()` 时，`with` 块才退出并关闭文件。

**练习 3**：一个生成器对象能被两个独立的训练循环各消费一遍吗？

**答案**：不能。生成器是**一次性**迭代器，没有「回头」语义；读完即抛 `StopIteration`。要训练两遍必须调用两次 `stream_docs(path)` 创建两个全新生成器——这正是主程序里 [L638](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L638) 重新调用一次的原因。

### 4.3 get_minibatch：从流中切出小批量

#### 4.3.1 概念说明

`partial_fit` 一次吃一小批。`get_minibatch` 就是「从生成器里连续抽 `size` 条，打包成 (文本列表, 标签列表)」的适配器，并把「流枯竭」翻译成调用方能判断的返回值。

#### 4.3.2 核心流程

```text
get_minibatch(doc_stream, size)
   ├── docs, y = [], []
   ├── 循环 size 次: next(doc_stream) → 追加到 docs / y
   ├── 中途流枯竭 (StopIteration) → return None, None   ← 哨兵值
   └── 正常取满 → return docs, y
```

调用方拿到 `(None, None)` 就知道文件读完了，应退出训练循环。

#### 4.3.3 源码精读

- [ch08/ch08.py:610-619](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L610-L619) — `get_minibatch(doc_stream, size)`：内部 `for _ in range(size)` 逐条 `next(doc_stream)`；`try/except StopIteration` 把生成器耗尽的信号转换成 `return None, None`。注意它对调用方**完全隐藏**了异常，主循环里因此可以用一个简单的 `if not X_train: break`（[L648-649](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L648-L649)）判断结束——`not None` 与 `not []` 都为真，两种枯竭情形都被覆盖。

顺带一笔账：主循环 45 次 × 每次 1000 条 = 45,000 条训练，随后再取 5000 条测试（[L657](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L657)），45,000 + 5,000 = 50,000，恰好把整个文件消费完，一个不多一个不少。

#### 4.3.4 代码实践

1. **实践目标**：验证切批逻辑与枯竭行为，不训练任何模型。
2. **操作步骤**（示例代码）：

```python
import itertools

def count_stream(n):
    for i in range(n):
        yield f'doc {i}', i % 2

def get_minibatch(doc_stream, size):        # 复制 ch08.py L610-619
    docs, y = [], []
    try:
        for _ in range(size):
            text, label = next(doc_stream)
            docs.append(text)
            y.append(label)
    except StopIteration:
        return None, None
    return docs, y

stream = count_stream(7)
print(get_minibatch(stream, 3))   # 3 条
print(get_minibatch(stream, 5))   # 只剩 4 条 → 不足 5
print(get_minibatch(stream, 2))   # 流已空 → (None, None)
```

3. **需要观察的现象**：第一次返回 3 条；第二次因为取满 5 条之前流就空了，返回 `(None, None)`（连同已凑的 4 条一起丢弃！）；第三次也是 `(None, None)`。
4. **预期结果**：如上所述。注意暴露的细节——**只要凑不满一批，整批作废**。数据量是批大小整数倍时没有影响；否则尾部不足一批的样本会被静默丢弃。这是书中实现的一个值得注意的行为。

#### 4.3.5 小练习与答案

**练习 1**：`except StopIteration: return None, None` 若改成 `return docs, y`（交回已收集的部分）会有什么不同？

**答案**：文件尾部不足 `size` 条时也能被利用上。但主循环的 `if not X_train: break` 只拦截 `None`/空列表，部分批次仍会被训练——两种策略都合法，取决于你是否想用「零头」数据。书中选择整批作废，保证每批大小严格一致。

**练习 2**：为什么 `get_minibatch` 内部用 `next(doc_stream)` 而不是 `for text, label in doc_stream`？

**答案**：`for` 会一路消费到枯竭，无法在取满 `size` 条后停下。`next()` 一次只取一条，循环 `size` 次即止，生成器状态留在第 `size` 条之后，下一批从那里继续。

**练习 3**：把 `if not X_train: break` 改成 `if X_train is None: break`，在本讲的数据上行为会变吗？

**答案**：不会。本讲中返回值要么是含 1000 条的非空列表、要么是 `None`，两种写法等价。但若 `get_minibatch` 有朝一日返回空列表（例如 size=0），`not X_train` 也能触发 break 而 `is None` 不能——前者的防御面更宽。

### 4.4 HashingVectorizer：无状态的固定维度向化

#### 4.4.1 概念说明

上一讲的 `CountVectorizer` / `TfidfVectorizer` 都是**有状态**的：必须先 `fit` 整个语料建立 `vocabulary_`（词 → 下标的字典），才能 `transform`。这意味着两件无法接受的事：① 需要先完整扫一遍语料（内存里要装下整个词表）；② 训练后遇到新词没有下标可用（词表之外只能丢弃）。

`HashingVectorizer` 用**哈希技巧**把「查词表」换成「算哈希」：每个词经 MurmurHash3 哈希后对维度取模，直接得到列下标并累加计数：

\[ \text{index}(w) = h(w) \bmod d, \qquad d = 2^{21} = 2{,}097{,}152 \]

它完全**无状态**：`fit` 是空操作，任何批次的 `transform` 互相独立、结果可拼接；维度 `d` 在构造时就钉死，与语料大小无关。代价是**哈希冲突**：两个不同的词可能落到同一下标（该列计数混在一起），且哈希是单射的破坏——**没有任何办法从下标反查回词**（所以它不支持 `get_feature_names_out`，模型只能当黑盒用）。`2**21` 约 209 万维，远大于影评语料的去重词数，冲突概率被压得很低；即便偶发冲突，sklearn 默认的带符号哈希（`alternate_sign=True`，交替赋予 ±1）让冲突引入的偏差在线性模型的期望意义下相互抵消。

#### 4.4.2 核心流程

```text
1000 条原始评论
   │  vect.transform(batch)        ← 无 fit、无词表
   ▼
每条评论: tokenizer 清洗分词（L587-592）
   │  每个词: h(词) mod 2**21 → 列号；出现一次计数 ±1
   ▼
(1000, 2**21) 稀疏矩阵 scipy.sparse（默认 L2 归一化）
```

#### 4.4.3 源码精读

- [ch08/ch08.py:626-629](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L626-L629) — 构造 `HashingVectorizer`，四个参数各司其职：
  - `decode_error='ignore'`：文件里混入无法按 utf-8 解码的字节时直接跳过，而不是抛异常——真实爬来的文本常有脏字节；
  - `n_features=2**21`：固定维度 209 万，远超词表规模，把冲突率压低；
  - `preprocessor=None`：**关闭**向化器默认的小写化预处理器——因为清洗（含小写化）已经全部在自定义 tokenizer（L587-592）里做了，避免重复处理；
  - `tokenizer=tokenizer`：把分词与清洗完全委托给书里的独立版函数（剥 HTML、滤停用词）。
- [ch08/ch08.py:650](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L650) — 每批调用的是 `vect.transform(X_train)` 而非 `fit_transform`：无状态向化器不需要（也不存在）fit 步骤，各批次产出的稀疏矩阵天然可以前后拼接。
- [ch08/ch08.py:27-28](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L27-L28) — 两行 import 正是对应 `HashingVectorizer` 与 `SGDClassifier` 的引入。

#### 4.4.4 代码实践

1. **实践目标**：直观看到「固定维度」与「哈希冲突」。
2. **操作步骤**（示例代码）：

```python
from sklearn.feature_extraction.text import HashingVectorizer

# 仿照书中参数，但把维度故意调小
vect_tiny = HashingVectorizer(n_features=8, alternate_sign=False)
X = vect_tiny.transform(['alpha beta gamma delta epsilon zeta '
                         'eta theta iota kappa lambda mu'])
print('shape =', X.shape)          # (1, 8)
print('非零列数 nnz =', X.nnz)     # 12 个不同词挤进 8 列 → nnz < 12

# 书中配置：2**21 维
vect = HashingVectorizer(n_features=2**21)
X21 = vect.transform(['the sun is shining'])
print('shape =', X21.shape, 'nnz =', X21.nnz)
```

3. **需要观察的现象**：小维度时 12 个互不相同的词只留下不超过 8 个非零列——这就是冲突被「叠加」了；`2**21` 维时形状恒为 `(1, 2097152)`，无论词多词少。
4. **预期结果**：`shape = (1, 8)` 且 `nnz < 12`（鸽笼原理保证必有冲突）；`2**21` 版本 `nnz` 为 4（The/sun/is/shining 各占一列，默认分词下按小写词计）。具体 nnz 数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CountVectorizer` 无法直接用于本讲的流式场景？

**答案**：它必须先对语料做一遍 `fit` 建立完整 `vocabulary_` 字典才能 `transform`。流式场景恰恰无法把全部文档同时交给 `fit`；即使分批 fit，先前批次没见过的词也没有下标，各批产出的矩阵列含义也无法对齐。哈希向化把「记住词表」换成「现算哈希」，天然无状态。

**练习 2**：`n_features=2**21` 是拍脑袋的吗？调小到 `2**10` 会怎样？

**答案**：不是。维度要显著大于语料去重词数才能把冲突率压低（生日问题式的直觉：\(d\) 远大于词数 \(m\) 时，碰撞的期望对数约为 \(m^2/2d\)）。影评语料词数在十万量级，`2**21`（约 209 万）让冲突几乎可忽略；调到 `2**10`（1024）后大量词挤进同一列，特征被严重混叠，准确率会明显下滑。合适的练习是扫 `n_features ∈ {2**10, 2**14, 2**18, 2**21}` 观察最终准确率。

**练习 3**：用 `HashingVectorizer` 训练出的逻辑回归，能像上一讲那样列出「权重最大的词」吗？为什么？

**答案**：不能。哈希是不可逆的单向映射，且偶有冲突，下标无法还原成词（它也不提供 `get_feature_names_out`）。这是哈希技巧用「可解释性」换「无状态 + 定长维度」的明码标价。

### 4.5 SGDClassifier.partial_fit：在线训练主循环

#### 4.5.1 概念说明

有了「流」和「无状态向化器」，最后一块拼图是**支持增量更新的模型**。`SGDClassifier` 用随机梯度下降拟合线性模型，默认 `loss='hinge'`（线性 SVM）；书中选 `loss='log'`（对数损失）后它本质上是**逻辑回归的 SGD 版**——与上一讲的 `LogisticRegression` 同一个损失函数，只是求解方式从「全量矩阵上精确求解」换成「逐批随机梯度」。

`partial_fit` 与 `fit` 的区别是本模块的核心：

| | `fit` | `partial_fit` |
| --- | --- | --- |
| 已有参数 | **清空重来** | **保留续训** |
| 数据要求 | 一次性全量 | 一批即可 |
| 首次调用的标签 | 由数据本身决定 | 必须显式传 `classes`（全部可能标签） |

`classes` 参数为什么必须在第一次 `partial_fit` 时给出：单看第一批 1000 条，无法保证 0/1 两个类都出现；模型必须在此刻就知道「输出空间是 {0,1}」，否则若首批恰好只有一个类，它就会把世界理解成单类的。这与 u6-l3 讲过的 `pos_label` 问题是同一主题——**标签语义要由人显式钉死，不能交给随机抽样**。

#### 4.5.2 核心流程

```text
doc_stream = stream_docs('movie_data.csv')
classes = np.array([0, 1])

重复 45 次:
    X_train, y_train ← get_minibatch(doc_stream, 1000)   # ① 取 1000 条原文
    若取空 → break
    X_train ← vect.transform(X_train)                     # ② 哈希向化 (1000, 2**21)
    clf.partial_fit(X_train, y_train, classes=classes)    # ③ 增量更新一次
    pbar.update()                                         # ④ 进度条 +1

取下一批 5000 条 → transform → clf.score(X_test, y_test) # ⑤ 评估
clf = clf.partial_fit(X_test, y_test)                     # ⑥ 连测试数据也喂回去续训
```

累计训练样本数随批次线性增长：第 \(k\) 批后为 \(1000k\)；45 批共 45,000 条。

#### 4.5.3 源码精读

- [ch08/ch08.py:635](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L635) — `clf = SGDClassifier(loss='log', random_state=1)`：对数损失即逻辑回归的交叉熵；`random_state=1` 固定 SGD 的随机扰动以便复现。（版本提示：scikit-learn 1.1 起 `loss='log'` 更名为 `'log_loss'`，旧名被弃用并在更新版本中移除；本仓库 environment.yml 钉的是 sklearn 1.0，按 u1-l2 搭建的环境可原样运行。）
- [ch08/ch08.py:638](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L638) — 创建**新的**生成器实例。生成器是一次性的（见 4.2 练习 3），从这里开始顺序消费整个文件。
- [ch08/ch08.py:643-652](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L643-L652) — 训练主循环。`pyprind.ProgBar(45)`（[L643](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L643)）以 45 次为总量画进度条；[L645](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L645) `classes = np.array([0, 1])` 钉死标签空间；循环体四步对应上面流程图的 ①–④，其中 [L650-651](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L650-L651) 先 `vect.transform` 再 `partial_fit`。notebook 里这段的真实耗时输出为 `Total time elapsed: 00:00:16`（见 [ch08/ch08.ipynb:1479](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb#L1479)）——5 万条文档的训练在一台笔记本上仅约十几秒，可作为本机预期量级的参照（待本地验证）。
- [ch08/ch08.py:657-659](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L657-L659) — 从同一个流继续取 5000 条作测试批，向化后 `clf.score` 输出准确率。notebook 保存的真实结果是 `Accuracy: 0.868`（见 [ch08/ch08.ipynb:1506](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb#L1506)）——比上一讲全量 TF-IDF + 网格搜索的 0.899 低约 3 个百分点：换来了「内存占用不随数据量增长」的可扩展性，而且没做任何超参搜索、也没用 IDF 加权。
- [ch08/ch08.py:664](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L664) — `clf = clf.partial_fit(X_test, y_test)`：评估完把测试批也喂回去继续增量训练。此处**不传** `classes`——模型在首次调用时已见过全部标签，后续调用可省略；这是 `classes` 只在第一次必需的现场证据。这个「顺手再学一把」假定之后还会有新数据到来；若训练到此为止，这一行没有意义。

#### 4.5.4 代码实践

1. **实践目标**：最小改动跑通在线训练，先只训 3 批，感受 `partial_fit` 的增量语义。
2. **操作步骤**：把 ch08.py 的 L638–L659 复制进 notebook，仅把循环次数 `range(45)` 改成 `range(3)`、测试批 `size=5000` 改成 `size=1000`（文件还剩 47,000 条，足够），运行。
3. **需要观察的现象**：进度条只走 3 格；打印准确率约为 0.8 上下，已明显高于随机猜测的 0.5，但低于 45 批的 0.868。
4. **预期结果**：3 批（3000 条）后约 0.8x；把次数改回 45 应复现 ≈0.868。具体数值**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：第一次 `partial_fit` 忘传 `classes=classes` 会怎样？

**答案**：scikit-learn 要求首次增量调用必须提供 `classes`（缺失会直接抛错），因为模型必须在见到全部标签之前就知道输出空间的形状。`classes` 只需在第一次给；[L664](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L664) 的后续调用省略它即是明证。

**练习 2**：把训练循环里的 `clf.partial_fit(...)` 误写成 `clf.fit(...)`，程序不报错，但结果会是什么？

**答案**：`fit` 每次都清空参数从头学，45 次循环结束后模型等价于**只在最后一批 1000 条上训练过**，测试准确率会显著低于 0.868。`fit` 与 `partial_fit` 的区别不在于名字，而在于是否保留状态——这是在线学习最容易踩的静默错误。

**练习 3**：`SGDClassifier(loss='log')` 和上一讲的 `LogisticRegression`（liblinear 求解器）是什么关系？

**答案**：两者最小化同一个对数损失（逻辑回归的交叉熵），是「同一个模型、不同求解器」。liblinear 在全量矩阵上做精确的凸优化求解；SGD 在小批上做随机梯度更新，单步便宜、天然支持 `partial_fit`。在大数据下 SGD 略牺牲一点精度（0.868 vs 0.899，且后者还做了网格搜索），换取常数级的内存占用。

## 5. 综合实践

**任务：把清洗搬进数据流，并画出 45 个批次的在线学习曲线。**

规格要求是「修改 `stream_docs` 让它在产出评论前先剥离噪声前缀（复用书中的正则），然后统计 45 个 batch 各自的准确率并画出准确率随已见样本数变化的曲线」。先交代一个经核对的数据事实：**本仓库的 movie_data.csv 每行评论正文没有评分前缀**——notebook 中 `next(stream_docs(...))` 的真实输出显示第一条评论以 `"In 1974, ...` 开头，标签只出现在行尾的 `,1`。因此「剥离前缀」在本仓库落地为：在生成器内部复用书中 `preprocessor` 的正则（[ch08/ch08.py:355-361](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L355-L361)）对每条评论做流式清洗，剥掉 HTML 标签等噪声、统一小写——即「数据进入向化器之前先剥壳」。

1. **实践目标**：
   - 会改造生成器，把清洗逻辑前移到数据流里（tokenizer 因此可以退化为简单 `split`）；
   - 用一个**固定测试集**记录每个批次训练后的准确率，得到随已见样本数增长的在线学习曲线。

2. **操作步骤**（以下为示例代码，保存为 `ch08/ooc_curve.py` 或在 notebook 中运行；需在 ch08 目录下、已解压出 movie_data.csv）：

```python
# 示例代码：改编自 ch08.py L587-L664
import re
import numpy as np
import pyprind
import matplotlib.pyplot as plt
from nltk.corpus import stopwords
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import SGDClassifier

stop = stopwords.words('english')

def preprocessor(text):                      # 原样复用书中正则 (L355-361)
    text = re.sub('<[^>]*>', '', text)
    emoticons = re.findall('(?::|;|=)(?:-)?(?:\)|\(|D|P)', text)
    text = (re.sub('[\W]+', ' ', text.lower()) +
            ' '.join(emoticons).replace('-', ''))
    return text

def stream_docs_clean(path):                 # 修改版 stream_docs (对照 L595-600)
    with open(path, 'r', encoding='utf-8') as csv:
        next(csv)  # skip header
        for line in csv:
            text, label = line[:-3], int(line[-2])
            yield preprocessor(text), label  # ← 清洗前移到产出之前

def get_minibatch(doc_stream, size):         # 原样复用 (L610-619)
    docs, y = [], []
    try:
        for _ in range(size):
            text, label = next(doc_stream)
            docs.append(text); y.append(label)
    except StopIteration:
        return None, None
    return docs, y

# 文本已被生成器清洗过，tokenizer 只需按空格切词并滤停用词
vect = HashingVectorizer(decode_error='ignore',
                         n_features=2**21,
                         preprocessor=None,
                         tokenizer=lambda t: [w for w in t.split()
                                               if w not in stop])
clf = SGDClassifier(loss='log', random_state=1)

doc_stream = stream_docs_clean(path='movie_data.csv')

# 与书中顺序不同：先取前 5000 条当固定测试集，剩下的恰好够 45 个训练批
X_test_raw, y_test = get_minibatch(doc_stream, size=5000)
X_test = vect.transform(X_test_raw)

classes = np.array([0, 1])
acc_hist, seen_hist = [], []
pbar = pyprind.ProgBar(45)
for i in range(45):
    X_batch_raw, y_batch = get_minibatch(doc_stream, size=1000)
    if not X_batch_raw:
        break
    X_batch = vect.transform(X_batch_raw)
    clf.partial_fit(X_batch, y_batch, classes=classes)
    acc_hist.append(clf.score(X_test, y_test))   # 每批后评一次
    seen_hist.append(1000 * (i + 1))
    pbar.update()

print('最终准确率:', acc_hist[-1])
plt.plot(seen_hist, acc_hist, marker='o')
plt.xlabel('samples seen'); plt.ylabel('test accuracy')
plt.title('Out-of-core learning curve (batch=1000)')
plt.savefig('ooc_curve.png', dpi=150)
```

   注意与书的两处刻意差异：① 清洗从 tokenizer 挪进了 `stream_docs`（tokenizer 退化为「按空格切 + 滤停用词」）；② 测试集取的是流开头的 5000 条——由于 CSV 落盘前整体打乱过（[L176-177](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L176-L177)），开头 5000 条与末尾 5000 条同样是随机样本，且这样剩余 45,000 条恰好够 45 个整批。

3. **需要观察的现象**：
   - 曲线在前几个批次迅速爬升（前 5,000–10,000 条样本带来的提升最大）；
   - 之后增速放缓，在 0.85 上下小幅波动并趋于平台；
   - `pbar` 显示 45 格全部走完（若中途 break，说明 `movie_data.csv` 行数不对，回到 4.1.4 检查）。

4. **预期结果**：最终准确率与书中的 0.868 大体相当（清洗位置的前移不改变信息量，只改变执行位置；测试切片不同会带来百分之一上下的差异）。曲线整体形态为「快升—平台」。具体数值与曲线**待本地验证**。

5. **延伸变体**：把 `acc_hist.append(clf.score(X_test, y_test))` 换成 `clf.score(X_batch, y_batch)`（在刚学过的批次上自评），会得到一条同样上升但更乐观的「拟合曲线」；两条曲线画在一起，就是流式版的偏差-方差观察实验（呼应 u6-l1 的学习曲线）。

## 6. 本讲小结

- **out-of-core 三件套**：`stream_docs` 生成器逐条惰性读文件 → `HashingVectorizer` 无状态定长向化 → `SGDClassifier.partial_fit` 增量更新；任意时刻内存里只有一个批次（1000 条），与文件总量无关。
- **生成器**：`yield` 让函数变成可暂停的数据流，`line[:-3]` / `int(line[-2])` 依赖「每评论一行、行尾 `,标签\n`」的格式假设；生成器是一次性迭代器，重新消费须重新创建。
- **`get_minibatch`**：连续 `next()` 抽 `size` 条，用 `except StopIteration → (None, None)` 把流枯竭翻译成哨兵值；凑不满一批时整批作废（45×1000 + 5000 = 50000，本讲数据正好整除）。
- **哈希技巧**：词 → `h(w) mod 2**21` 直接定列，无词表、无 fit、维度恒定；代价是冲突（小概率、可由带符号哈希缓解）与不可逆（无法列出重要词）。
- **`partial_fit` 语义**：保留参数续训（`fit` 则清空重来）；首次调用必须传 `classes` 钉死标签空间，之后可省（L664 即现场证据）。
- **实验结果**：45 批 × 1000 条后，在 5000 条测试批上 `Accuracy: 0.868`，比全量 TF-IDF + 网格搜索的 0.899 低约 3 个百分点——用少量精度换来了内存可扩展性。

## 7. 下一步学习建议

- **本章后续**：ch08 的最后一节是主题建模（LDA，[ch08/ch08.py:667](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L667) 起），同样以 movie_data.csv 为语料，可当作无监督文本特征的练习自行阅读。
- **换个角度复习 `partial_fit`**：回到 u2-l3 的 `AdalineSGD` 手写实现（[ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py)），对照本讲体会「逐样本更新 → 逐批更新」与「手写循环 → scikit-learn 接口」的对应关系。
- **向下一章进发**：第 9 章回归分析（u9-l1）将回到批学习世界，但「评估前先想清楚数据流」的习惯（哪里 fit、哪里 transform、哪部分数据算「已见」）会一直受用；若想继续深挖流式工具，可阅读 scikit-learn 文档中 `SGDClassifier` 的 `average` 参数（对增量学习做参数平均以稳定收敛），以及 ch08 附属目录 `logistic-regression-bag-of-words/` 下的基础版与迭代版两个 notebook（`log-reg.ipynb`、`log-reg-iterative.ipynb`），作为词袋 + 逻辑回归的补充阅读。
