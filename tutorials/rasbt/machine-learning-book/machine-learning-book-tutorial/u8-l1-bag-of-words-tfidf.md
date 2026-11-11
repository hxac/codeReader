# 词袋模型、TF-IDF 与文本清洗

> 对应书本第 8 章《Applying Machine Learning To Sentiment Analysis》前半部分。
> 前置讲义：u6-l3（混淆矩阵与指标体系）、u6-l2（GridSearchCV）、u3-l1（逻辑回归与正则化参数 C）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `CountVectorizer` 把一组文本变成「文档-词」稀疏矩阵，并能解释 `vocabulary_` 和 `fit_transform` 输出的含义。
2. 手算一遍 scikit-learn 版 TF-IDF，理解「文档频率」如何把到处都出现的词降权。
3. 读懂本章的文本清洗函数 `preprocessor`（去 HTML、留表情符号）与两种分词器（普通分词、Porter 词干还原），以及 NLTK 停用词过滤。
4. 理解 `ngram_range`、`min_df`/`max_df` 这类词表控制参数对特征规模的影响。
5. 用 `Pipeline` 把 `TfidfVectorizer` 和 `LogisticRegression` 串起来，交给 `GridSearchCV` 做带交叉验证的超参数搜索，并正确解读 `best_params_` 与 `best_score_`。

## 2. 前置知识

本讲处理的是**非结构化文本**（电影评论），而前面几讲处理的都是数值特征表格。先把几个术语用通俗语言说清楚：

- **文本分类 / 情感分析**：给定一条评论，预测它的标签。本章用 IMDb 影评数据集，5 万条评论，正面（1）与负面（0）各 2.5 万条，目标是判断评论褒贬。
- **词袋模型（bag-of-words）**：机器学习模型只吃数值向量，不吃字符串。词袋模型的做法非常"粗暴"：把整本文档的词序丢掉，只统计**每个词出现了几次**，用词频当特征。"我爱这部电影"和"这部电影我爱"得到的向量完全一样——这就是"词袋"的含义。
- **词表（vocabulary）**：语料中所有出现过的不同单词的集合。`CountVectorizer` 会给词表里每个词分配一个整数下标，特征向量的第 i 维就是词表第 i 个词的计数。
- **稀疏矩阵**：一部影评可能只有几百个词，而词表可能有几万、几十万维，所以文档向量里绝大多数位置是 0。scikit-learn 因此返回 `scipy.sparse` 稀疏矩阵，只存非零元素，需要肉眼检查时用 `.toarray()` 转成稠密数组。
- **词干提取（stemming）与词形还原**：running/runs/runners 意思接近，若当作 4 个不同的词，词表会虚胖、特征会稀疏。Porter 词干提取器用一套剥离后缀的规则把它们都削成 run。它不保证削出真正的英文单词（比如 thus 会被削成 thu），但胜在快、确定。
- **停用词（stopwords）**：the、is、and 这类几乎每篇文档都有的高频功能词，对区分褒贬几乎没帮助，常用停用词表过滤掉。
- **TF-IDF**：term frequency–inverse document frequency，词频-逆文档频率。直观理解：**一个词在本篇出现越多（tf 大）、在其他文档出现越少（df 小），它就越能代表本篇文档**。"电影"到处都有，区分度低；"烂尾"只在少数评论里出现，区分度高。
- **复习两个前面讲过的工具**：`Pipeline`（u6-l1）把预处理和模型绑成一个估计器，交叉验证时每折自动重新拟合；`GridSearchCV`（u6-l2）用「步骤名__参数名」的双下划线语法扫描管线内部参数；逻辑回归的 `C` 是正则化强度的倒数、`penalty` 选 l1/l2（u3-l1）。本讲会把它们全部串起来用在文本管线里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch08/ch08.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py) | 本章唯一主源码文件（由 notebook 自动转换生成），包含数据组装、词袋、TF-IDF、清洗分词、网格搜索、out-of-core 学习与 LDA 主题模型。本讲只读前半部分（约 L213–L548）。 |
| [ch08/ch08.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb) | 与 .py 等价的 notebook 版本，保留了各单元的**历史运行输出**（比如网格搜索的最优参数），本讲用它核对预期结果。 |
| [ch08/movie_data.csv.gz](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/movie_data.csv.gz) | 已组装好的 5 万条影评数据（约 26 MB 压缩包），两列：`review`（正文）与 `sentiment`（0/1）。本讲综合实践直接用它，省去下载与组装步骤。 |
| ch08/logistic-regression-bag-of-words/ | 两个附加 notebook（`log-reg.ipynb`、`log-reg-iterative.ipynb`），用词袋特征做逻辑回归的补充示例，本讲不展开。 |

数据组装代码（把 aclImdb 目录里 5 万个 txt 文件拼成 `movie_data.csv`）位于 [ch08/ch08.py:L148-L195](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L148-L195)：遍历 `train/test` × `pos/neg` 四个目录读文件，`np.random.permutation` 打乱行序后存 CSV。本讲实践直接从仓库自带的 `movie_data.csv.gz` 出发，无需自己组装。

## 4. 核心概念与源码讲解

### 4.1 CountVectorizer：把文档变成「文档-词」矩阵

#### 4.1.1 概念说明

`CountVectorizer` 是 scikit-learn 对词袋模型的实现，它同时做了两件事：

1. **建词表**（`fit`）：扫描全部文档，把出现的每个词（默认按小写、按字母排序）映射到一个整数下标，存进 `vocabulary_` 字典。
2. **数词频**（`transform`）：对每个文档统计词表中每个词的出现次数，产出形状为 `(文档数, 词表大小)` 的稀疏矩阵。第 (d, i) 个元素 = 词 i 在文档 d 中出现的次数，也叫**原始词频** tf(t, d)。

#### 4.1.2 核心流程

```
输入: 文档列表 docs（Python 字符串列表）
fit(docs):  分词 → 小写化 → 去重排序 → vocabulary_ = {词: 下标}
transform(docs): 对每篇文档数词频 → 稀疏矩阵 X, X.shape = (n_docs, |vocabulary_|)
fit_transform(docs): 一步完成上面两步
```

三个要点：

- 词表下标按**字母顺序**排列，与词在句子里的位置无关。
- 特征向量里每一维对应词表的一个词；全 0 的维度在稀疏矩阵里不占显式存储。
- 同一个 `CountVectorizer` 必须先用训练语料 `fit`（定词表），新文档只能 `transform`——新文档里词表之外的词直接被忽略。这与 u5-l1 里「PCA 只在训练集拟合」是同一条防泄漏原则。

#### 4.1.3 源码精读

本章用一个 3 句话的迷你语料演示 `CountVectorizer`：

[ch08/ch08.py:L228-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L228-L233) — 创建 `CountVectorizer`，定义 3 个文档，调用 `fit_transform` 得到词袋稀疏矩阵 `bag`：

```python
count = CountVectorizer()
docs = np.array([
        'The sun is shining',
        'The weather is sweet',
        'The sun is shining, the weather is sweet, and one and one is two'])
bag = count.fit_transform(docs)
```

[ch08/ch08.py:L240-L249](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L240-L249) — 先打印词表字典，再把稀疏矩阵转成稠密数组肉眼检查：

```python
print(count.vocabulary_)
print(bag.toarray())
```

词表输出（按字母序编号）为：

```
{'the': 6, 'sun': 4, 'is': 1, 'shining': 3, 'weather': 8, 'sweet': 5, 'and': 0, 'one': 2, 'two': 7}
```

即 `and→0, is→1, one→2, shining→3, sun→4, sweet→5, the→6, two→7, weather→8`，共 9 维。稠密化的词频矩阵为：

```
[[0 1 0 1 1 0 1 0 0]     # 文档1 "The sun is shining"
 [0 1 0 0 0 1 1 0 1]     # 文档2 "The weather is sweet"
 [2 3 2 1 1 1 2 1 1]]    # 文档3: and×2, is×3, one×2, ...
```

注意文档 3 里 `is` 出现 3 次、`and` 出现 2 次——这正是接下来 TF-IDF 一节手算要用的数字。

#### 4.1.4 代码实践

1. **实践目标**：亲手建一个词袋，确认词表编号规则与矩阵形状。
2. **操作步骤**：新建 Python 会话，粘贴 [L228-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L228-L233) 与 [L240-L249](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L240-L249) 的代码并运行（只需 numpy 与 scikit-learn）。再补两行检查：

```python
# 示例代码（自编检查，非仓库原有）
print(type(bag))            # <class 'scipy.sparse._csr.csr_matrix'>
print(bag.shape)            # (3, 9)：3 篇文档 × 9 个词
print(len(count.vocabulary_))
```

3. **需要观察的现象**：`bag` 不是 ndarray 而是 CSR 稀疏矩阵；词表是「词 → 下标」的字典且下标按字母序；矩阵每行对应一篇文档。
4. **预期结果**：`bag.shape == (3, 9)`，`vocabulary_` 与上文一致（此结果可确定性复现）。
5. 本实践不依赖 IMDb 数据集，可直接运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把文档 2 改成 `'The weather was sweet'`（is 换成 was），词表大小会变成多少？
**答案**：10。was 是新词，进入词表并按字母序插入；is 因文档 1、3 仍在词表中。这说明词表完全由语料决定，改一句话就可能改变所有列的含义——词表必须在交叉验证的每一折内**重新 fit**（这正是后面要用 Pipeline 的原因）。

**练习 2**：为什么 `CountVectorizer` 默认返回稀疏矩阵而不是稠密数组？
**答案**：真实语料词表动辄几万到几十万维，而单篇文档只含其中几百个词，稠密存储会浪费极大内存与计算；稀疏矩阵只存非零元，且 scikit-learn 的分类器原生支持稀疏输入。

### 4.2 TfidfTransformer：用文档频率重新加权

#### 4.2.1 概念说明

词频有个问题：`the`、`is` 这类词在**每篇**文档里都高频，却毫无区分度。TF-IDF 的思路是给每个词频乘一个**逆文档频率**权重：一个词出现在越多的文档里（df 大），权重越低；只在少数文档出现的词权重高。

教科书版定义：

\[
\text{tf-idf}(t,d)=\text{tf}(t,d)\times \text{idf}(t,d),
\qquad
\text{idf}(t,d) = \log\frac{n_d}{1+\text{df}(d,t)}
\]

其中 \(n_d\) 是文档总数，\(\text{df}(d,t)\) 是包含词 \(t\) 的文档数。

但要注意：**scikit-learn 实际实现的公式和教科书版略有出入**（源码注释 [L288-L300](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L288-L300) 专门解释了这一点）：

\[
\text{idf}_{\text{sklearn}}(t,d) = \log\frac{1 + n_d}{1 + \text{df}(d,t)},
\qquad
\text{tf-idf}(t,d) = \text{tf}(t,d)\times\left(\text{idf}(t,d)+1\right)
\]

两个差异：分子加 1（平滑，对应 `smooth_idf=True`），以及 idf 之后再 **+1**——保证即使某词出现在所有文档（idf=0）时，它仍保留自身词频而非被清零。最后默认做 L2 归一化（`norm='l2'`），把每个文档向量缩放为单位长度：

\[
v_{\text{norm}} = \frac{v}{\|v\|_2} = \frac{v}{\sqrt{v_1^2 + v_2^2 + \cdots + v_n^2}}
\]

归一化的作用：让长短不同的文档可以公平比较（否则长评论的向量模长大、数值大）。

#### 4.2.2 核心流程

```
CountVectorizer.fit_transform(docs)          # 原始词频矩阵 X（稀疏）
TfidfTransformer.fit(X)                      # 从 X 统计每个词的 df → idf 权重表
TfidfTransformer.transform(X)                # 每个非零元素: x → x*(idf+1)，再逐行 L2 归一化
```

它是一个标准的 scikit-learn 转换器：`fit` 学到的只有一张「词 → idf」对照表，`transform` 无状态地重加权。

#### 4.2.3 源码精读

[ch08/ch08.py:L276-L280](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L276-L280) — 用三个参数显式实例化 `TfidfTransformer` 并对迷你语料重加权：

```python
tfidf = TfidfTransformer(use_idf=True, 
                         norm='l2', 
                         smooth_idf=True)
print(tfidf.fit_transform(count.fit_transform(docs))
      .toarray())
```

三个参数分别表示：启用 idf 加权、L2 归一化、平滑 idf（即上式分子分母各 +1）。

[ch08/ch08.py:L315-L319](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L315-L319) — 手算文档 3 中 "is" 的 tf-idf，验证公式：tf=3，df=3（三篇都有 is），于是

\[
\text{idf}(\text{"is"}, d_3) = \log\frac{1+3}{1+3} = 0,
\qquad
\text{tf-idf} = 3 \times (0+1) = 3
\]

```python
tf_is = 3
n_docs = 3
idf_is = np.log((n_docs+1) / (3+1))
tfidf_is = tf_is * (idf_is + 1)
print(f'tf-idf of term "is" = {tfidf_is:.2f}')   # 3.00
```

"is" 原始词频 3 是文档 3 里最高的，但因为它三篇文档都出现，重加权后 tf-idf 只有 3.0，反而不及只出现 2 次但 df=1 的 "and"（2×(ln2+1)≈3.39）。

[ch08/ch08.py:L334-L342](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L334-L342) — 关闭归一化（`norm=None`）重新变换，取文档 3 那一行，再手动做 L2 归一化，证明理解无误：

```python
tfidf = TfidfTransformer(use_idf=True, norm=None, smooth_idf=True)
raw_tfidf = tfidf.fit_transform(count.fit_transform(docs)).toarray()[-1]

l2_tfidf = raw_tfidf / np.sqrt(np.sum(raw_tfidf**2))
l2_tfidf
```

文档 3 未归一化的 tf-idf 行为 `[3.39, 3.0, 3.39, 1.29, 1.29, 1.29, 2.0, 1.69, 1.29]`，除以 L2 范数后得到 `[0.5, 0.45, 0.5, 0.19, 0.19, 0.19, 0.3, 0.25, 0.19]`——与 [L276-L280](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L276-L280) 默认参数的输出完全一致，其中 "is" 的归一化 tf-idf 正是书中强调的 0.45。

#### 4.2.4 代码实践

1. **实践目标**：亲手复算一个词的 tf-idf，确认自己掌握了 sklearn 变体公式。
2. **操作步骤**：接 4.1 的会话，运行 [L276-L280](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L276-L280) 得到默认输出；然后仿照 [L315-L319](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L315-L319) 手算 "and"（文档 3 中 tf=2，df=1）：

```python
# 示例代码（仿照 L315-L319 手算 "and"）
tf_and, df_and, n_docs = 2, 1, 3
idf_and = np.log((n_docs + 1) / (df_and + 1))
print(tf_and * (idf_and + 1))        # 期望 3.3863...
```

再与 `raw_tfidf`（`norm=None` 版本，[L334-L336](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L334-L336)）的第 0 列（"and" 的下标是 0）对数。
3. **需要观察的现象**：手算值与 `raw_tfidf[0]` 一致；"and" 的未归一化 tf-idf（≈3.39）大于 "is" 的（3.0），尽管 "is" 词频更高。
4. **预期结果**：3.3863（= \(2(\ln 2 + 1)\)），可确定性复现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 sklearn 要在 idf 之后加 1（即 \(\text{tf}\times(\text{idf}+1)\)）？
**答案**：若不加，出现在**所有**文档的词 idf 平滑后为 0，tf-idf 恒为 0，等于把这类词彻底删除；+1 让它们退化回"只用词频"，保留最基本的计数信息，数值上也更稳定。

**练习 2**：默认 `norm='l2'` 归一化后，文档 3 的 tf-idf 向量满足什么性质？它对逻辑回归训练有什么好处？
**答案**：每行（每篇文档向量）的 L2 范数为 1，即各元素平方和为 1。好处是长评论与短评论的向量模长一致，特征量纲统一，对带正则化的线性模型（如逻辑回归）更友好——正则化惩罚的是权重大小，若特征模长悬殊，惩罚会不公平地偏向长文档里的大数值。

### 4.3 文本清洗与分词：preprocessor、Porter 词干与停用词

#### 4.3.1 概念说明

真实影评不是干净的单词流：有 `<br />` 这类 HTML 标签、标点、大写字母，还有 `:)` `:-(` 这类表情符号——而表情符号恰恰是情感的强信号，不能当垃圾删掉。本章的清洗策略分三层：

1. **preprocessor（文档级清洗）**：去 HTML 标签、统一小写、把非字母数字字符换成空格，但把表情符号"抢救"出来挪到句尾保留。
2. **tokenizer（分词）**：把字符串切成词列表，最简单就是 `str.split()`；可选 Porter 词干版 `tokenizer_porter`，把每个词削成词干。
3. **停用词过滤**：用 NLTK 的英文停用词表把 the/and/is 等去掉。

#### 4.3.2 核心流程

`preprocessor(text)` 的处理顺序（见下方源码）：

```
1. re.sub('<[^>]*>', '', text)          # 删除所有 <...> 形式的 HTML 标签
2. re.findall(表情符号模式)               # 先把 :) :( :D :P :-) 等抽出来存起来
3. 其余文本小写化，非单词字符(\W+)替换为空格
4. 拼接: 清洗后的文本 + 尾部表情符号（去掉表情里的 '-'）
```

Porter 词干与停用词：

```
tokenizer(text)        = text.split()
tokenizer_porter(text) = [porter.stem(w) for w in text.split()]
过滤停用词              = [w for w in tokens if w not in stop]
```

#### 4.3.3 源码精读

[ch08/ch08.py:L355-L361](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L355-L361) — 文本清洗函数 `preprocessor`，正则逐条对应上面的流程：

```python
def preprocessor(text):
    text = re.sub('<[^>]*>', '', text)
    emoticons = re.findall('(?::|;|=)(?:-)?(?:\)|\(|D|P)',
                           text)
    text = (re.sub('[\W]+', ' ', text.lower()) +
            ' '.join(emoticons).replace('-', ''))
    return text
```

[ch08/ch08.py:L366-L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L366-L376) — 先对一条带 HTML 和表情的字符串做单元测试式验证，然后把它应用到全部 5 万条评论：

```python
preprocessor("</a>This :) is :( a test :-)!")   # → 'this is a test :) :( :)'
df['review'] = df['review'].apply(preprocessor)
```

注意验证输出的三个细节：`</a>` 被删、文本小写化、三个表情符号（含 `:-)` 被削成 `:)`）统一挪到句尾。

[ch08/ch08.py:L385-L392](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L385-L392) — 两种分词器：普通切分与 Porter 词干版：

```python
porter = PorterStemmer()

def tokenizer(text):
    return text.split()

def tokenizer_porter(text):
    return [porter.stem(word) for word in text.split()]
```

[ch08/ch08.py:L397-L402](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L397-L402) — 对比运行效果（notebook 保存的输出）：

```python
tokenizer('runners like running and thus they run')
# ['runners', 'like', 'running', 'and', 'thus', 'they', 'run']

tokenizer_porter('runners like running and thus they run')
# ['runner', 'like', 'run', 'and', 'thu', 'they', 'run']
```

`running/runners/run` 被合并为词干 `run`；注意 `thus` 被削成了 `thu`——Porter 是纯规则的后缀剥离器，不保证产出真词，这是它和语言学意义上的"词形还原"（lemma）的区别。

[ch08/ch08.py:L408-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L408-L416) — 下载 NLTK 停用词表并在分词流水线里过滤：

```python
nltk.download('stopwords')

stop = stopwords.words('english')
[w for w in tokenizer_porter('a runner likes running and runs a lot')
 if w not in stop]
# ['runner', 'like', 'run', 'run', 'lot']
```

`a`、`and` 这类停用词被剔除，词干化后的 `run` 出现两次（likes→like、running/running→run）。

#### 4.3.4 代码实践

1. **实践目标**：验证 `preprocessor` 对脏文本的处理行为，并体会词干提取对词表的压缩。
2. **操作步骤**：运行 [L355-L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L355-L376) 的函数定义与两个验证调用（只需 re，无需数据集）；停用词部分需要先 `pip install nltk` 并执行 `nltk.download('stopwords')`。再自己构造一条脏文本：

```python
# 示例代码
preprocessor('<p>Hello<br />WORLD!! :-D</p>')
```

3. **需要观察的现象**：标签消失、大写变小写、标点变空格、表情 `:-D` 以 `:D` 的形式出现在结尾。
4. **预期结果**：`'hello world :d'`（正则 `\W` 把 `:` 和 `D` 之间的非单词字符处理后，表情符号作为整体被追加到尾部并小写化；`:-D` 中 `-` 被去掉变 `:D`，随后整体 lowercase）。具体字符串待本地验证。
5. 若在无网环境无法 `nltk.download`，可先跳过停用词部分，只做 preprocessor 与 tokenizer 实验。

#### 4.3.5 小练习与答案

**练习 1**：为什么表情符号要挪到句尾而不是留在原位？
**答案**：留在原位的话，下一步 `re.sub('[\W]+', ' ', ...)` 会把 `: )` 里的符号当非单词字符删掉；先抽取、清洗完再拼回，才能把 `:)` 保留成一个合法 token，让"笑脸/哭脸"这类情感信号进入特征。

**练习 2**：`tokenizer` 与 `tokenizer_porter` 各自的代价是什么？网格搜索里两者都要试的原因是什么？
**答案**：`tokenizer` 快但把 running/runners/run 当三个不同特征，词表大、单特征更稀疏；`tokenizer_porter` 合并同义词干、压缩词表，但要付出额外计算时间，且偶有过度合并（thus→thu）。哪种更好是经验问题，所以后面 4.5 的网格把 `vect__tokenizer` 也当超参数来搜。

### 4.4 ngram_range 与词表修剪：控制词表的形状与规模

#### 4.4.1 概念说明

- **n-gram**：把相邻 n 个词绑成一个特征。1-gram（unigram）就是单个词；2-gram（bigram）如 "not good"、"very bad"。"not good" 的褒贬与 "good" 完全相反——这是词袋丢掉词序的痛点，引入 bigram 能部分找回局部语序信息，代价是词表爆炸式增长。
- **ngram_range=(1, 1)** 表示只用 unigram；`(1, 2)` 表示 unigram + bigram 一起用。
- **min_df / max_df**：按文档频率修剪词表。`min_df` 要求词至少出现在 min_df 篇文档里（删掉拼写错误、只出现一两次的罕见词）；`max_df`（可取比例）删掉出现过于频繁的词（相当于内置停用词过滤）。`max_features` 则直接按词频取前 k 个词。
- **诚实提示**：本章代码的网格里 `ngram_range` 只用了 `(1, 1)`（[L440-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L440-L467)），没有出现 `min_df`；`max_df=.1, max_features=5000` 实际用在后面的 LDA 主题模型一节 [ch08/ch08.py:L687-L690](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L687-L690)。`min_df` 是 `CountVectorizer`/`TfidfVectorizer` 真实存在的参数，本讲把它作为扩展实验介绍。

#### 4.4.2 核心流程

```
CountVectorizer(ngram_range=(min_n, max_n), min_df=m, max_df=M, max_features=k)
fit:
  抽取所有长度在 [min_n, max_n] 的相邻词组
  统计每个词组的文档频率 df
  保留 m ≤ df ≤ M（且按词频排名前 k，若指定）的词组 → vocabulary_
transform: 统计词组频次（稀疏矩阵）
```

#### 4.4.3 源码精读

[ch08/ch08.py:L439-L453](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L439-L453) — 原始（被注释掉的完整版）参数网格，其中第一组字典扫描 `vect__ngram_range`，两组字典都固定在 `(1, 1)`：

```python
"""
param_grid = [{'vect__ngram_range': [(1, 1)],
               'vect__stop_words': [stop, None],
               'vect__tokenizer': [tokenizer, tokenizer_porter],
               'clf__penalty': ['l1', 'l2'],
               'clf__C': [1.0, 10.0, 100.0]},
              ...
```

即使在这个"完整版"里 n-gram 也只试 `(1,1)`——在 5 万条影评上搜 bigram 组合的拟合开销过大（48 个候选 × 5 折 = 240 次拟合），所以作者随后缩减出了小网格。

[ch08/ch08.py:L687-L690](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L687-L690) — 本章里真正使用文档频率修剪的地方是 LDA 主题模型一节，`max_df=.1` 丢掉出现在超过 10% 文档中的高频词、`max_features=5000` 把词表压到 5000：

```python
count = CountVectorizer(stop_words='english',
                        max_df=.1,
                        max_features=5000)
X = count.fit_transform(df['review'].values)
```

#### 4.4.4 代码实践

1. **实践目标**：直观感受 n-gram 与 min_df 对词表规模的影响。
2. **操作步骤**：接 4.1 的迷你语料 `docs`，运行以下示例代码：

```python
# 示例代码
from sklearn.feature_extraction.text import CountVectorizer

v12 = CountVectorizer(ngram_range=(1, 2)).fit(docs)   # unigram + bigram
print(len(v12.vocabulary_))                            # 词表从 9 暴涨

v_min2 = CountVectorizer(min_df=2).fit(docs)           # 只留出现在≥2篇文档的词
print(sorted(v_min2.vocabulary_))
```

3. **需要观察的现象**：`(1,2)` 词表显著大于 9（新增了 "the sun"、"is shining" 等二元词组）；`min_df=2` 后词表只剩多文档共现的词。
4. **预期结果**：`min_df=2` 的词表应为 `['is', 'shining', 'sun', 'sweet', 'the', 'weather']` 这 6 个词（每个都出现在 ≥2 篇文档；`and/one/two` 只在第 3 篇出现而被删）。`(1,2)` 的具体词表大小待本地验证。
5. 思考：在 5 万条影评上把 `(1,1)` 换成 `(1,2)`，词表通常从几万涨到几百万，网格搜索每折的拟合时间也会随之暴涨——这就是本章网格里保守使用 `(1,1)` 的原因。

#### 4.4.5 小练习与答案

**练习 1**：`min_df` 与 `max_df` 分别解决什么问题？各自的极端取值会发生什么？
**答案**：`min_df` 删罕见词（拼写错误、一次性俚语），防词表虚胖和过拟合；`max_df` 删超高频词（接近内置停用词过滤）。`min_df` 过大会把真正有信息量的低频词（如罕见但强情感的电影名）误删；`max_df` 过小会把普通常用词也删掉。

**练习 2**：为什么 bigram 对情感分析特别有价值？
**答案**：情感表达常靠局部搭配：not good、not bad、very funny。unigram 把 not 和 good 拆开，"not good" 会贡献 "good" 的正面特征；bigram "not good" 作为整体特征能把这类否定语义保留下来。

### 4.5 GridSearchCV 调「TF-IDF + 逻辑回归」管线

#### 4.5.1 概念说明

前面三块拼起来就是完整的文本分类管线：**清洗 → 分词 → TF-IDF 向量化 → 逻辑回归**。其中向化器的 tokenizer/停用词/n-gram 和分类器的 C/penalty 全都是超参数。关键设计（承接 u6-l1 的教训）：

- 向化器必须放进 `Pipeline`，让交叉验证的**每一折只用该折训练数据重建词表和 idf**。如果先在全部数据上 `fit_transform` 再交叉验证，验证折的词已经"见过"，属于数据泄漏。
- `TfidfVectorizer` = `CountVectorizer` + `TfidfTransformer` 的二合一，本章用它少写一步。

#### 4.5.2 核心流程

```
1. 划分数据: df 前 25000 行为训练、后 25000 行为测试（df 早已全局打乱）
2. Pipeline: vect = TfidfVectorizer(...) → clf = LogisticRegression(solver='liblinear')
3. 定义参数网格（双下划线语法 vect__xxx / clf__xxx）
4. GridSearchCV(pipe, grid, scoring='accuracy', cv=5, n_jobs=-1).fit(X_train, y_train)
5. best_params_ / best_score_ = 最优组合 与 该组合 5 折平均准确率
6. best_estimator_.score(X_test, y_test) = 锁定测试集上的最终成绩
```

#### 4.5.3 源码精读

[ch08/ch08.py:L426-L429](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L426-L429) — 训练/测试划分。因为组装数据时已用 `np.random.permutation` 打乱（[L176-L177](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L176-L177)），直接切前 25000/后 25000 行即可，无需再 stratify：

```python
X_train = df.loc[:25000, 'review'].values
y_train = df.loc[:25000, 'sentiment'].values
X_test = df.loc[25000:, 'review'].values
y_test = df.loc[25000:, 'sentiment'].values
```

[ch08/ch08.py:L435-L437](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L435-L437) — 实例化向化器。注意 `lowercase=False, preprocessor=None`：**因为评论在 [L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L376) 已用 `preprocessor` 清洗小写化过了**，无需向化器重复做：

```python
tfidf = TfidfVectorizer(strip_accents=None,
                        lowercase=False,
                        preprocessor=None)
```

[ch08/ch08.py:L455-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L455-L467) — 缩减版参数网格 `small_param_grid`，两个字典代表两种"向化器配置子空间"：第一组保留 idf 加权（默认 `use_idf=True, norm='l2'`），搜 tokenizer 与 C；第二组显式关闭 idf（`use_idf=False, norm=None`，即退回近似原始词频），只配普通分词器：

```python
small_param_grid = [{'vect__ngram_range': [(1, 1)],
                     'vect__stop_words': [None],
                     'vect__tokenizer': [tokenizer, tokenizer_porter],
                     'clf__penalty': ['l2'],
                     'clf__C': [1.0, 10.0]},
                    {'vect__ngram_range': [(1, 1)],
                     'vect__stop_words': [stop, None],
                     'vect__tokenizer': [tokenizer],
                     'vect__use_idf':[False],
                     'vect__norm':[None],
                     'clf__penalty': ['l2'],
                     'clf__C': [1.0, 10.0]},
              ]
```

候选数：第一组 1×1×2×1×2 = 4，第二组 1×2×1×1×1×1×2 = 4，共 8 个候选；相比被注释的完整版网格（[L439-L453](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L439-L453)，48 个候选）大幅省时。

[ch08/ch08.py:L469-L476](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L469-L476) — 组装管线与网格搜索器。`solver='liblinear'` 是有意选的：它对 l1、l2 两种惩罚都能求解（默认的 lbfgs 只支持 l2，跑不了完整版网格里的 `'clf__penalty': ['l1', 'l2']`）；`n_jobs=-1` 并行所有候选与折（[L479-L481](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L479-L481) 的注释还提醒 Windows 下 pickle 自定义分词器可能出问题，可退回 `str.split`，代价是不支持词干）：

```python
lr_tfidf = Pipeline([('vect', tfidf),
                     ('clf', LogisticRegression(solver='liblinear'))])

gs_lr_tfidf = GridSearchCV(lr_tfidf, small_param_grid,
                           scoring='accuracy',
                           cv=5,
                           verbose=1,
                           n_jobs=-1)
```

[ch08/ch08.py:L485-L497](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L485-L497) — 拟合后输出最优参数、5 折平均准确率与测试集准确率：

```python
gs_lr_tfidf.fit(X_train, y_train)

print(f'Best parameter set: {gs_lr_tfidf.best_params_}')
print(f'CV Accuracy: {gs_lr_tfidf.best_score_:.3f}')

clf = gs_lr_tfidf.best_estimator_
print(f'Test Accuracy: {clf.score(X_test, y_test):.3f}')
```

notebook 保存的运行结果（见 [ch08.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.ipynb) 对应单元输出）为：最优组合 `C=10.0, penalty='l2', ngram_range=(1,1), stop_words=None, tokenizer=普通 tokenizer`，**CV Accuracy 0.897，Test Accuracy 0.899**。两个值得注意的结论：停用词过滤与 Porter 词干**都没有**胜出（TF-IDF 的 idf 降权已弱化了高频词的作用）；正则化最弱（C 最大）的组合最优——文本特征多而稀疏时，弱正则通常更合适。重复运行的具体数字可能略有浮动，待本地验证。

[ch08/ch08.py:L509-L545](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L509-L545) — 一段独立小实验，专门解释 `best_score_` 的语义：用同一份 `StratifiedKFold` 折索引分别跑 `cross_val_score` 和空的 `GridSearchCV`，验证 `gs.best_score_` 恰好等于最优模型 5 折分数的**平均值**（这是平均交叉验证分数，不是测试集分数）。

#### 4.5.4 代码实践

1. **实践目标**：用最小数据亲手验证 `best_score_` = 最优候选的平均交叉验证分数。
2. **操作步骤**：直接运行 [L509-L545](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L509-L545) 的小实验（随机 25 个样本，秒级完成，无需影评数据）：

```python
np.random.seed(0)
y = [np.random.randint(3) for i in range(25)]
X = (y + np.random.randn(25)).reshape(-1, 1)
cv5_idx = list(StratifiedKFold(n_splits=5).split(X, y))

print(cross_val_score(LogisticRegression(), X, y, cv=cv5_idx))
print(cross_val_score(LogisticRegression(), X, y, cv=cv5_idx).mean())
print(GridSearchCV(LogisticRegression(), {}, cv=cv5_idx).fit(X, y).best_score_)
```

3. **需要观察的现象**：最后两个数字完全相等；第一个数组就是 5 折各自的准确率。
4. **预期结果**：三处输出一致（`np.random.seed(0)` 保证可复现）。待本地验证具体数值。
5. 这解释了为什么本章报出的是 "CV Accuracy 0.897" 而测试集另算 0.899——前者是模型选择的依据，后者才是对未见数据的最终评估。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `TfidfVectorizer` 放在 Pipeline **外面**、先对全量训练集 `fit_transform` 再做 5 折交叉验证，错在哪里？
**答案**：词表与 idf 权重会"看到"每一折的验证数据（验证折里的词进入了词表、影响了文档频率统计），交叉验证分数被乐观抬高，属于数据泄漏；放进 Pipeline 后每折只用训练部分重建向化器，才是无偏估计。

**练习 2**：`small_param_grid` 用两个字典而不是把所有参数塞进一个字典，为什么？
**答案**：两个字典对应两套**不同的向化器配置子空间**（第一套启用 idf+L2，第二套关闭 idf 与归一化）。`use_idf`/`norm` 只在第二套里有意义且各只有一个取值；若合并成一个字典，会组合出大量无意义的重复候选（比如 `use_idf=False` 配上 `tokenizer_porter` 再乘上 stop_words 两项），白白翻倍拟合次数。这与 u6-l2 讲过的"双列表网格给不同核定义独立子空间"是同一技巧。

**练习 3**：网格搜索后为什么还要再算一次 `clf.score(X_test, y_test)`？
**答案**：`best_score_` 是在训练数据上做交叉验证得到的**选型依据**，存在乐观偏差（u6-l2）；`best_estimator_` 已在 refit 后用全部 25000 条训练数据重训，在从未参与调参的 25000 条测试集上的分数才是对泛化性能的最终估计。

## 5. 综合实践

**任务**：在电影评论**子集**上完整走一遍「1-gram 词袋 + TF-IDF + 5 折网格搜索逻辑回归 C 与 penalty」流程。这是本章 [L435-L497](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L435-L497) 的缩小版——书上用 25000 条训练评论 × 8 候选要跑很久，我们取 5000 条子集让普通笔记本也能承受。

**步骤**：

1. 解压数据（仓库自带，26 MB 压缩包，解压约 130+ MB）：

```bash
cd ch08
gunzip -k movie_data.csv.gz
```

2. 运行以下脚本（示例代码，改编自 ch08.py 的 L435-L497）：

```python
# 示例代码：IMDb 子集上的词袋 + TF-IDF + 网格搜索
import pandas as pd
import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV

df = pd.read_csv('movie_data.csv', encoding='utf-8')
df = df.rename(columns={"0": "review", "1": "sentiment"})  # 兼容列名异常，见 ch08.py L193
print(df.shape)                                            # 期望 (50000, 2)

# 取已打乱数据的前 5000 条做子集（数据在 L176-L177 已全局打乱）
X = df.loc[:4999, 'review'].values
y = df.loc[:4999, 'sentiment'].values

tfidf = TfidfVectorizer(strip_accents=None, lowercase=False, preprocessor=None,
                        ngram_range=(1, 1), stop_words=None)

param_grid = [{'clf__penalty': ['l1', 'l2'],
               'clf__C': [0.1, 1.0, 10.0]}]

pipe = Pipeline([('vect', tfidf),
                 ('clf', LogisticRegression(solver='liblinear'))])

gs = GridSearchCV(pipe, param_grid, scoring='accuracy', cv=5, n_jobs=-1)
gs.fit(X, y)

print(f'Best parameter set: {gs.best_params_}')
print(f'5-fold mean CV Accuracy: {gs.best_score_:.3f}')
```

3. 注意：`movie_data.csv` 是在 [L184](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L184) 保存的，而 `preprocessor` 清洗发生在读回之后的 [L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L376)——所以 **CSV 里存的是未清洗的原始评论**（含 HTML 标签）。子集必须先补一步清洗（函数定义见 [L355-L361](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L355-L361)），才能与书中的向化器配置（`lowercase=False, preprocessor=None`）配套：

```python
# 示例代码：补上 ch08.py L376 的清洗步骤
import re
def preprocessor(text):
    text = re.sub('<[^>]*>', '', text)
    emoticons = re.findall('(?::|;|=)(?:-)?(?:\)|\(|D|P)', text)
    text = (re.sub('[\W]+', ' ', text.lower()) +
            ' '.join(emoticons).replace('-', ''))
    return text

df['review'] = df['review'].apply(preprocessor)   # 在取 X、y 之前执行
```

可先打印 `df.loc[0, 'review'][-50:]`（[L350](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L350) 的检查方式）确认清洗前确实带标签、清洗后没有。

**需要观察与报告的内容**：

- `df.shape` 是否为 (50000, 2)；
- 6 个候选（l1/l2 × 3 个 C）× 5 折 = 30 次拟合的进度输出（`verbose=1` 时可见）；
- **最优参数组合**（`gs.best_params_`）与 **5 折平均准确率**（`gs.best_score_`）。

**预期结果**：书上全量数据（25000 训练、8 候选）的结果是 CV 0.897 / 测试 0.899（notebook 存档输出）；5000 条子集上准确率通常会低一些（大约在 0.80–0.88 区间），且 l1 与 l2、不同 C 的选择可能不同于全量结果——样本越少，模型选择越不稳定。具体数值待本地验证。

**思考题**（选做）：把 `ngram_range` 改成 `(1, 2)` 重跑，比较最优准确率与运行时间的变化；再在向化器上加 `min_df=2`，观察词表规模（`gs.best_estimator_.named_steps['vect']` 的 `vocabulary_` 长度）与准确率如何变化。

## 6. 本讲小结

- `CountVectorizer.fit_transform` 一步完成「建词表 + 数词频」，输出 `(n_docs, |vocab|)` 的稀疏矩阵；词表下标按字母序，`vocabulary_` 是「词 → 下标」字典。
- sklearn 的 TF-IDF 是教科书公式的变体：\(\text{idf}=\log\frac{1+n_d}{1+\text{df}}\)，且 tf 乘的是 \((\text{idf}+1)\)，最后默认逐行 L2 归一化；手算 "is"（tf=3, df=3 → 3×(0+1)=3 → 归一化 0.45）可与 `TfidfTransformer` 输出精确对上。
- 文本清洗三件套：`preprocessor` 删 HTML、小写化并保留尾部表情符号；`tokenizer`/`tokenizer_porter` 切词与 Porter 词干还原；NLTK 停用词表过滤高频功能词。
- `ngram_range` 用 bigram 找回局部语序（not good），`min_df`/`max_df`/`max_features` 按文档频率修剪词表；本章网格只用 `(1,1)`，`max_df/max_features` 出现在 LDA 一节。
- 向化器必须放进 `Pipeline` 再交给 `GridSearchCV`（cv=5）：每折重建词表与 idf，防止泄漏；`best_score_` 是最优候选的 5 折平均准确率（0.897），测试集成绩（0.899）要用 refit 后的 `best_estimator_` 单独评估。
- 全量搜索的胜出组合（停用词不过滤、不用词干、C=10、l2）说明：TF-IDF 的 idf 降权已部分替代停用词过滤；文本特征高维稀疏时弱正则更合适。

## 7. 下一步学习建议

- 下一讲 **u8-l2 out-of-core 流式学习**：当数据大到内存装不下时，用生成器 `stream_docs` 逐批读评论、`HashingVectorizer` 把词哈希进固定维度空间（彻底绕开词表）、`SGDClassifier.partial_fit` 在线更新——它正好解决本讲 `TfidfVectorizer` 必须先看全量数据建词表的痛点。重点读 [ch08/ch08.py:L595-L664](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L595-L664)。
- 本章剩余部分是 **LDA 主题模型**（[L687-L716](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch08/ch08.py#L687-L716)），一种无监督的"软聚类"，可顺带阅读，理解 `max_df`/`max_features` 在真实场景的用法。
- 巩固本讲可重读 u6-l1（Pipeline 防泄漏）与 u6-l2（嵌套交叉验证）；如果想在文本上继续深入，第 15/16 章的 LSTM 与 Transformer 会以"词序列"而非"词袋"的方式处理同一类问题。
