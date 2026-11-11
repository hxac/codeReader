# u15-l3 LSTM 语言模型与文本采样生成

## 1. 本讲目标

学完本讲,你应该能够:

1. 掌握语言模型样本的「错位一步」构造法:一个长度为 41 的字符块,前 40 个字符是输入 x,后 40 个字符是目标 y,每个位置都在预测「下一个字符」。
2. 读懂 `RNN` 类的单字符前向设计与逐字符展开的训练循环,理解为什么每个 epoch 都要重新初始化隐状态。
3. 读懂 `sample()` 函数:先用起始文本「预热」隐状态,再自回归地逐字符生成新文本。
4. 理解温度参数(`scale_factor`)与生成长度如何影响生成文本的多样性与连贯性。
5. 知道仓库代码中**没有** checkpoint 保存与预训练权重文件这一事实,并会用第 u13-l2 讲学过的 `state_dict` 方法自己补上保存与加载。

本讲是第 15 章(循环神经网络)的收官讲。前两讲(u15-l1、u15-l2)分别打下了 RNN/LSTM 的数学基础和 IMDb 词级情感分类的工程流程;本讲把任务换成**字符级语言模型的文本生成**,它对应 u15-l1 介绍的五种序列架构中的「同步多对多」型。

## 2. 前置知识

### 2.1 什么是语言模型

语言模型(language model)学习的是「给定前文,下一个符号是什么」的概率分布。对一段文本 \( s_1, s_2, \dots, s_T \),联合概率可以按链式法则分解:

\[
P(s_1, s_2, \dots, s_T) = \prod_{t=1}^{T} P(s_t \mid s_1, \dots, s_{t-1})
\]

只要能建模每个条件分布 \( P(s_t \mid s_{<t}) \),就能一边预测一边把预测结果拼回输入,生成任意长度的新文本——这叫**自回归生成**(autoregressive generation)。本讲用 LSTM 来建模这个条件分布。

### 2.2 字符级与词级的差别

u15-l2 的 IMDb 情感分类是**词级**(word-level)模型:词表是几万个单词,任务是对整句话输出一个情感概率(多对一)。本讲是**字符级**(character-level)模型:

| 维度 | u15-l2 词级情感分类 | 本讲 字符级语言模型 |
| --- | --- | --- |
| 词表 | 数万词 + `<pad>`/`<unk>` | 80 个左右唯一字符(含空格、换行、标点) |
| 输出 | 整句一个二分类概率 | 每个位置一个「下一字符」分布 |
| 架构类型 | 多对一 | 同步多对多 |
| 用途 | 判断情感 | 生成新文本 |
| 损失 | BCELoss | CrossEntropyLoss |

字符级的好处是不需要分词、不存在未登录词;代价是序列变长、模型要自己学会拼出单词甚至拼写。

### 2.3 需要复习的概念

- **隐状态与细胞状态**:LSTM 每步接收 `(hidden, cell)` 并返回更新后的 `(hidden, cell)`(u15-l1、u15-l2)。
- **Dataset 契约**:实现 `__len__` 与 `__getitem__` 即可被 `DataLoader` 使用(u12-l2)。
- **五步训练骨架**:前向 → 算损失 → `backward()` → `step()` → `zero_grad()`(u12-l3)。
- **state_dict 保存与 map_location 加载**(u13-l2),本讲综合实践会直接用到。
- **softmax 温度**:对 logits 整体乘一个系数再 softmax,可以调芷新采样分布的「尖」或「平」,详见 4.3 节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ch15/ch15_part3.py` | 本讲唯一核心源码:字符级语言模型的完整实现(数据预处理、模型、训练循环、`sample()` 生成) |
| `ch15/1268-0.txt` | 训练语料:儒勒·凡尔纳《神秘岛》(The Mysterious Island)的 Project Gutenberg 纯文本,编号 1268 |
| `ch15/ch15_part3.ipynb` | 与 `.py` 等价的 notebook 版本(`.py` 由它自动转换生成) |
| `ch15/README.md` | 第 15 章大纲,其中 "Project two: character-level language modeling" 即本讲范围 |
| `ch15/ch15_part2.py` | 上一讲(u15-l2)的 IMDb 情感分类代码,可对照两种序列任务的差异 |

`ch15_part3.py` 内部结构速览(行号供快速定位):

| 行号范围 | 内容 |
| --- | --- |
| 69–98 | 读入小说、截取正文、构建字符词表与整数编码 |
| 117–156 | 滑窗分块、`TextDataset` 错位一步、检视样本 |
| 180–233 | `RNN`(LSTM)模型定义与逐字符训练循环 |
| 242–265 | `Categorical` 分布采样的两个演示 |
| 270–299 | `sample()` 函数与首次调用 |
| 306–326 | 温度缩放演示与两次对比生成 |

## 4. 核心概念与源码讲解

### 4.1 从整本小说到训练样本:滑窗分块与错位一步

#### 4.1.1 概念说明

语言模型的监督信号就藏在文本自身里:把一段字符向右错开一步,左边当输入 x、右边当目标 y,就得到了「预测下一个字符」的训练样本。这个构造完全不需要人工标注,所以语言模型是**自监督学习**的典型例子。

因为整个语料只有一份,如何造出很多样本?答案是**滑动窗口**:窗口长度定为 `seq_length + 1`(多出来的 1 个字符就是「窗口末尾要被预测的那个字符」),从文本开头起步长 1 地一路滑过去,每个起点都切出一个块(chunk)。这就是本讲标题里「chunking + step 参数」的含义——仓库代码把步长写死为 1(`range` 的默认步长),即相邻两个 chunk 只差一个字符,样本几乎处处重叠,信息利用率最高。

#### 4.1.2 核心流程

```text
读入整本小说 (1268-0.txt)
   ↓ 截取正文(去掉 Gutenberg 头尾版权说明)
   ↓ set(text) 得到唯一字符集 → 排序 → char2int 字典 + char_array 反查数组
   ↓ 逐字符查表,把整本书编码成一长串整数 text_encoded
   ↓ 滑窗:每个位置 i 切出 text_encoded[i : i+41] 共 41 个字符的块
   ↓ TextDataset.__getitem__ 返回 (chunk[:-1], chunk[1:])
       x = 前 40 个字符   (输入)
       y = 后 40 个字符   (目标,恰好在 x 每个位置上右移一位)
```

#### 4.1.3 源码精读

**第一步:读入并截取正文。** [ch15/ch15_part3.py:69-78](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L69-L78) 用 `find` 定位正文首尾(`'THE MYSTERIOUS ISLAND'` 与 `'End of the Project Gutenberg'`),把 Project Gutenberg 的头部说明与尾部版权声明剥掉,只留小说正文;再 `set(text)` 统计唯一字符。注意唯一字符里**包含空格、换行与各种标点**,它们都是模型要学习预测的「词表成员」。

**第二步:构建双向映射并编码。** [ch15/ch15_part3.py:87-98](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L87-L98) 中,`char2int` 是「字符 → 整数」的字典,`char_array` 是排序后的字符数组,可按整数下标反查字符——生成阶段把模型输出的整数变回字符全靠它。随后整本小说被编码成 `np.int32` 数组 `text_encoded`,并与原文互为正反映射。

**第三步:滑窗切块。** [ch15/ch15_part3.py:117-121](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L117-L121) 定义 `seq_length = 40`、`chunk_size = 41`,用列表推导式滑窗。`range` 没有显式步长参数,即 **step=1**;块的数量等于 `len(text_encoded) - chunk_size + 1`(约等于正文字符数减 40,数十万字符的语料会得到约十几万个块,具体数字以本地打印为准)。

**第四步:错位一步的 Dataset。** [ch15/ch15_part3.py:135-146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L135-L146) 是标准的 Dataset 契约(u12-l2):`__len__` 返回块数,`__getitem__` 对 41 长度的块做两次切片——`text_chunk[:-1]` 取前 40 个作为 x,`text_chunk[1:]` 取后 40 个作为 y。**两者错开一位:y 的第 t 个元素正是 x 的第 t 个元素之后的那个字符。** `.long()` 把张量转成 `torch.int64`,这是 `nn.Embedding` 对输入索引的要求。

**第五步:人工检视样本。** [ch15/ch15_part3.py:151-156](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L151-L156) 打印前两个样本的 x 与 y,你会看到 y 与 x 内容几乎相同、只是整体左移了一位——这正是「同步多对多」架构的标签形态:输入输出的时间步一一对应,每步都在预测下一个字符。

#### 4.1.4 代码实践

1. **实践目标**:亲眼确认「错位一步」与滑窗 step=1 的样本结构。
2. **操作步骤**(在 `ch15/` 目录下以交互方式执行,或抄进 notebook):
   ```python
   # 前置:按 4.1.3 的顺序执行到 seq_dataset = TextDataset(torch.tensor(text_chunks))
   print(len(seq_dataset))            # 样本总数 ≈ 正文字符数 - 40
   x0, y0 = seq_dataset[0]
   print(repr(''.join(char_array[x0])))   # 第一个样本的输入
   print(repr(''.join(char_array[y0])))   # 第一个样本的目标
   x1, y1 = seq_dataset[1]
   print(repr(''.join(char_array[x1])))   # 第二个样本的输入
   ```
3. **需要观察的现象**:y0 与 x0 相比整体左移一个字符;y1 与 x0 相比只差一个字符(开头去掉一个、结尾多出一个)。
4. **预期结果**:`len(seq_dataset)` 为十几万量级(待本地验证具体数值);打印出的字符串满足上述两个错位关系。
5. 上述结论均可由切片逻辑直接推出,属于确定性结果。

#### 4.1.5 小练习与答案

**练习 1**:如果把 `chunk_size` 保持 41,滑窗步长从 1 改成 5(即 `range(0, len(text_encoded)-chunk_size+1, 5)`),样本数量与训练效果会怎么变?

**答案**:样本数量大约变成原来的 1/5(每个起点间隔 5 个字符);每个字符作为「窗口起点」被用到的机会变少,相同 epoch 数下见到的监督信号变稀疏,但相邻样本高度冗余的程度也降低,内存占用更小。语言模型训练中常用这种「带步长的滑窗」来权衡样本量与冗余度。

**练习 2**:为什么 `__getitem__` 返回 `(chunk[:-1], chunk[1:])` 而不是 `(chunk[:-1], chunk[-1])`(只预测窗口最后一个字符)?

**答案**:整块切片让 40 个时间步**每一步**都产生监督信号,一次前向就能算 40 个位置的损失(见 4.2 的训练循环),训练效率高得多;只预测最后一个字符的话,同样的前向计算只换来 1 个损失项。两者数学上是同类条件分布,前者只是把「窗口内每个前缀」都当作了训练样例。

**练习 3**:语料唯一字符数(词表大小)包含换行符 `\n` 吗?这有什么好处?

**答案**:包含——`set(text)` 会对所有字符去重,换行、空格、引号都是词表成员。好处是模型能学会「什么时候该换行、什么时候该空格、什么时候开引号」,生成的文本自然带有段落与词间结构,而不只是字母流。

### 4.2 单字符展开的 LSTM:模型结构与逐字符训练循环

#### 4.2.1 概念说明

u15-l2 的 IMDb 模型一次把整句(整个序列)喂给 `nn.LSTM`,由 PyTorch 在内部沿时间步展开。本讲的 `RNN` 类反其道而行:**`forward` 一次只处理一个时间步的单个字符**,时间维度的循环完全暴露在 Python 训练循环里。

这样写有两个教学与工程上的好处:

1. **看清 BPTT(沿时间反向传播)**:损失 `loss += loss_fn(pred, target_batch[:, c])` 逐字符累加,40 步之后再 `backward()`,梯度沿 40 步展开的计算图回传——RNN「权重跨时间共享、梯度跨时间回传」这件事变得肉眼可见。
2. **显式传递隐状态**:`(hidden, cell)` 由调用方持有并逐步传入,生成阶段(4.3)可以直接复用同一套接口「喂一个字符、拿一个分布」。

另一个关键设计是:**每个 epoch 都重新 `init_hidden`,且每个 epoch 只取一个随机 batch**。因为 DataLoader 的 `shuffle=True`,相邻两个 epoch 的 batch 互不连续,窗口之间没有「上文关系」,继承隐状态没有意义;这也意味着梯度只在每个 40 字符窗口内部回传、跨窗口截断——这正是「长序列隐式截断」的实现方式(详见 4.4.1)。

**关于 checkpoint 的如实说明**:大纲与本讲规格中提到「checkpoint 保存」,但仓库的 `ch15_part3.py` **没有任何** `torch.save`/`state_dict` 相关代码(可用全文搜索验证),训练好的权重只存在于内存里,进程结束即丢失;仓库也**不提供**可下载的预训练权重文件。4.2.4 的实践会用 u13-l2 学过的方法把保存/加载补上。

#### 4.2.2 核心流程

一个 batch 内单次前向的形状流水(`batch_size=64`、`embed_dim=256`、`rnn_hidden_size=512`、词表大小 `vocab_size`):

```text
seq_batch                        (64, 40)   long 索引
seq_batch[:, c]                  (64,)      第 c 个字符的批次
embedding(x)                     (64, 256)
.unsqueeze(1)                    (64, 1, 256)   ← 补出"长度为 1 的序列"维
nn.LSTM                          out (64, 1, 512);(hidden, cell) 各 (1, 64, 512)
fc(out)                          (64, 1, vocab_size)
.reshape(out.size(0), -1)        (64, vocab_size)  ← 每行一个字符的分布 logits
```

训练循环沿用了 u12-l3 的五步骨架,只是把「前向 + 算损失」放进 40 步的字符循环:

```text
for epoch in range(num_epochs):
    hidden, cell = init_hidden(batch_size)        # 每个 epoch 重置隐状态
    取一个随机 batch (shuffle=True 保证每次不同)
    optimizer.zero_grad()
    for c in range(seq_length):                   # 逐步展开
        pred, hidden, cell = model(seq_batch[:, c], hidden, cell)
        loss += loss_fn(pred, target_batch[:, c]) # 40 个位置的损失累加
    loss.backward()                               # 一次回传穿过全部 40 步
    optimizer.step()
```

#### 4.2.3 源码精读

**模型定义。** [ch15/ch15_part3.py:180-198](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch15/ch15_part3.py#L180-L198) 定义了三层结构:`nn.Embedding(vocab_size, embed_dim)` 把字符索引变成可学习的稠密向量;`nn.LSTM(embed_dim, rnn_hidden_size, batch_first=True)` 是主干;`nn.Linear(rnn_hidden_size, vocab_size)` 把隐状态映射成**词表大小的 logits**(不是二分类的一个数,而是「80 个字符各自的得分」)。`forward`(189–193 行)里 `.unsqueeze(1)` 把 (batch, embed) 补成 (batch, 1, embed),凑出 `batch_first=True` 约定下的「序列长度为 1」;`init_hidden`(195–198 行)把隐状态与细胞状态都置零为 `(1, batch_size, rnn_hidden_size)` 并搬到 `device`——沿时间步数 1 这里的「1」是 LSTM 的层数(与 u15-l1 手算时 `h_0` 置零一致)。

**超参与实例化。** [ch15/ch15_part3.py:200-213](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L200-L213) 设定 `embed_dim=256`、`rnn_hidden_size=512`,损失用 `nn.CrossEntropyLoss()`(多分类 logits 的标准损失,内部自带 softmax,对照 u15-l2 二分类用的 BCELoss),优化器是 `Adam(lr=0.005)`。按形状可推出参数分布:嵌入层 \( 80 \times 256 \approx 2\text{万} \),LSTM 四个门各带 \( W_{ih}(4{\times}512{\times}256) \)、\( W_{hh}(4{\times}512{\times}512) \) 与双偏置,共约 158 万,全连接头 \( 512 \times 80 + 80 \approx 4.1\text{万} \)——**约 96% 的参数集中在 LSTM 里**。

**训练循环。** [ch15/ch15_part3.py:215-233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L215-L233) 中,221 行 `next(iter(seq_dl))` 每个新建一个迭代器并取第一个 batch——由于 172 行 `DataLoader(..., shuffle=True, drop_last=True)` 开了洗牌,每个 epoch 拿到的都是不同的随机 batch,这等价于「每个 epoch 做一步随机梯度下降」;226–228 行逐字符展开并累加损失;229 行一次 `backward()` 让梯度穿过 40 步的共享权重;231 行把总损失除以 `seq_length` 得到「平均每字符损失」,便于跨 `seq_length` 比较。随机初始化的模型输出近似均匀分布,首个 epoch 的每字符损失理论上应接近 \( \ln(\text{vocab\_size}) \approx \ln 80 \approx 4.38 \),训练中应观察到它持续下降(完整训练后的具体数值待本地验证)。

**补上 checkpoint(示例代码,仓库中不存在)。** 仓库未保存模型,以下按 u13-l2 的 `state_dict` 路线补全,标注为**示例代码**:

```python
# 示例代码:训练结束后保存(权重在 cuda 上训练时,cpu 加载需 map_location)
torch.save(model.state_dict(), 'char_lm_state.pt')

# 示例代码:重新搭建同结构模型并注入权重
model2 = RNN(vocab_size, embed_dim, rnn_hidden_size)
model2.load_state_dict(torch.load('char_lm_state.pt',
                                  map_location=torch.device('cpu')))
model2.eval()
```

#### 4.2.4 代码实践

1. **实践目标**:在 CPU 上跑通一次「短训练 → 保存 → 加载验证」闭环,为 4.3/4.4 的生成实验准备模型。
2. **操作步骤**:
   - 先按 4.1–4.2 执行到训练循环之前;若没有 GPU,把 [ch15/ch15_part3.py:162](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L162) 的 `device` 改为注释中的 `torch.device('cpu')`(改自己本地副本,不动仓库)。
   - 把 `num_epochs` 从 10000 降为 1000~3000(CPU 也可承受),跑完训练循环。
   - 用上面的示例代码保存 `state_dict`,再新建 `model2` 加载,对同一个输入字符分别调用 `model` 与 `model2` 的前向,比较输出 logits。
3. **需要观察的现象**:训练日志每 500 个 epoch 打印一次损失,应从接近 4.4 附近单调下降;两个模型对同一输入的 logits 应逐元素相等。
4. **预期结果**:损失明显下降(短训练后能降到一个明显低于 4.4 的水平;能否生成可读英文以 4.3 的生成结果为准,待本地验证);加载验证的 logits 完全一致,说明 `state_dict` 保存/加载成功。
5. 若 CUDA 不可用且 CPU 训练太慢,可把 `num_epochs` 降到几百,只验证流程通、损失在降。

#### 4.2.5 小练习与答案

**练习 1**:为什么每个 epoch 都调用 `model.init_hidden(batch_size)` 重新置零,而不是让隐状态跨 epoch 延续?

**答案**:batch 是 `shuffle=True` 随机抽出的 64 个互不相关的窗口,窗口之间没有时间连续性;延续上一个 batch 的隐状态会把「上一批任意窗口的末端记忆」错误地当成「当前窗口的上文」,引入无意义的噪声。重置隐状态同时把反向传播截断在 40 步之内,避免跨 batch 保留计算图。

**练习 2**:训练循环里 `loss += loss_fn(...)` 在 40 步上累加的是同一个标量张量,最后只 `backward()` 一次。如果把 `backward()` 挪进字符循环(每步一次)会发生什么?

**答案**:每步 `backward()` 后计算图默认被释放,且中间的 `hidden/cell` 是图的一部分,后续步骤的前向就断了梯度来源;即便用 `retain_graph=True` 修正,也等价于把一次 BPTT 拆成 40 次,计算更慢且隐状态梯度衔接要手工处理。累加后一次回传是「整段展开、一次求导」的标准写法。

**练习 3**:全连接层输出为什么是 `vocab_size` 维而不是 1 维?

**答案**:语言模型每个位置要输出「下一个字符是词表中每一个字符的概率分布」,80 类字符对应 80 维 logits,配 CrossEntropyLoss;输出 1 维只能表达二分类(如 u15-l2 的正/负情感),无法在 80 个字符里做选择。

### 4.3 sample():预热隐状态、类别采样与温度缩放

#### 4.3.1 概念说明

生成新文本的朴素想法是「每步选概率最大的字符」(贪心,argmax)。但贪心是确定性的:给定起始串,输出永远固定,而且语言模型常把高频字符(空格、e、t)的概率推得很高,贪心极易陷入「the the the」式的机械重复。改进办法是**按分布随机采样**——把 logits 经 softmax 变成概率分布,再从分布中抽一个字符作为下一步输出。仓库用 `torch.distributions.categorical.Categorical` 完成这件事,`Categorical(logits=...)` 会自动对 logits 做 softmax,`m.sample()` 则返回一个按该分布抽样的下标张量。

**温度(temperature)**是调节采样随机性的旋钮:在 softmax 之前把 logits 整体乘上系数 \( s \)(仓库里叫 `scale_factor`):

\[
P_i = \frac{e^{s \cdot z_i}}{\sum_j e^{s \cdot z_j}}
\]

- \( s > 1 \):拉开分差,分布更「尖」,采样更贴近 argmax——输出更可预测、更保守,也更容易重复;
- \( 0 < s < 1 \):压平分差,分布更「平」,低概率字符也有机会被抽中——输出更多样,但更容易拼错、串句;
- \( s \to \infty\) 退化为贪心,\( s \to 0 \) 退化为均匀抽样。

用一个手算例子感受一下(与仓库演示代码同一组数):logits \( z = (1, 1, 3) \) 时,softmax 得

\[
P = \frac{(e^1, e^1, e^3)}{2e^1 + e^3} = \frac{(2.718, 2.718, 20.086)}{25.522} \approx (0.107,\ 0.107,\ 0.787)
\]

乘 0.5 后 \( z' = (0.5, 0.5, 1.5) \),softmax ≈ (0.212, 0.212, 0.576);乘 0.1 后 \( z'' = (0.1, 0.1, 0.3) \),softmax ≈ (0.310, 0.310, 0.379),已接近均匀。最大概率从 0.787 一路被压到 0.379——同一个模型,温度不同,「敢不敢选冷门字符」完全不同。

#### 4.3.2 核心流程

`sample()` 分两阶段:

```text
准备:编码 starting_str → (1, L);model.eval();为 batch=1 初始化零隐状态
阶段一 预热(warm-up):for c in 0..L-2:
        只前向、丢弃 logits,用起始文本把 (hidden, cell) 推到"读完全部上文"的状态
阶段二 生成:for i in 1..len_generated_text:
        用 last_char 前向 → logits (vocab,)
        logits × scale_factor(温度)
        Categorical(logits=...).sample() → 新字符下标
        拼接到 generated_str,并把该字符作为下一步输入
```

为什么预热只喂前 \( L-1 \) 个字符?因为起始串的**最后一个字符本身就是「已知的上文」**,它不需要被预测,而是作为生成阶段的第一个输入 `last_char`,从它出发预测「下一个字符」。预热循环处理的是它前面的 \( L-1 \) 个字符。

#### 4.3.3 源码精读

**采样的两个演示。** [ch15/ch15_part3.py:242-265](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L242-L265) 先用 logits `(1,1,1)` 展示均匀分布(softmax 后各 1/3,采样 10 次大约三类均分),再用 `(1,1,3)` 展示偏斜分布(约 0.107/0.107/0.787,采样 10 次绝大多数落在第三个类上)。这两段是为了建立「logits → 概率 → 抽样」的直觉。

**sample 函数本体。** [ch15/ch15_part3.py:270-295](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L270-L295):

- 274–275 行把起始串编码成 `(1, L)` 的张量;
- 279–282 行切到评估模式并初始化 `batch=1` 的隐状态。注意一个真实细节:`init_hidden` 内部用的是模块级 `device`(cuda),而 298 行已经 `model.to('cpu')`——模型参数搬回了 CPU,`device` 变量却不会跟着变,所以 281–282 行必须显式 `hidden.to('cpu')`、`cell.to('cpu')`,否则会在 CPU 输入与 CUDA 隐状态之间报设备不匹配错误;
- 283–284 行预热循环,`_` 丢弃每步的 logits,只保留 `(hidden, cell)`;
- 286–294 行生成循环:288 行取 `last_char` 前向得到 `(1, vocab)` 的 logits,289 行 `squeeze` 成 `(vocab,)`;290 行乘 `scale_factor`;291–292 行从 `Categorical` 采样。`m.sample()` 不传参数时返回 **0 维标量张量**,用它索引 `char_array` 得到单个 numpy 字符标量,`str()` 后正是字符本身,可直接拼接;下一轮 288 行的 `last_char.view(1)` 又把它统一回 `(1,)` 的输入形状;
- 297–299 行固定随机种子并把模型搬到 CPU 后,以 `'The island'` 为起始串生成 500 个字符。

**温度演示与对比生成。** [ch15/ch15_part3.py:306-312](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L306-L312) 打印缩放前后的概率(即 4.3.1 的手算数字);[ch15/ch15_part3.py:317-319](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L317-L319) 与 [ch15/ch15_part3.py:324-326](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L324-L326) 分别用 `scale_factor=2.0` 与 `0.5` 生成文本供肉眼对比——两处都先 `torch.manual_seed(1)`,保证差异只来自温度而非随机种子。

**贪心对照(示例代码,仓库中不存在)。** 把采样那行换成 argmax 就是贪心生成,**示例代码**如下:

```python
# 示例代码:贪心版生成,可与 sample() 对比
last_char = torch.argmax(logits)          # 永远取最大 logits 的字符
generated_str += str(char_array[last_char])
```

训练不充分或温度过高/过低的模型,贪心输出常表现为同一小段文本的无限循环。

#### 4.3.4 代码实践

1. **实践目标**:用同一个(短训练后的)模型,量化观察温度与生成长度对输出质量的影响。
2. **操作步骤**(承接 4.2.4 保存的权重;生成前先 `model.to('cpu')`、`model.eval()`):
   ```python
   for sf in [0.3, 0.5, 1.0, 2.0]:
       torch.manual_seed(1)   # 同一种子下,差异只来自温度
       out = sample(model, starting_str='The island',
                    len_generated_text=300, scale_factor=sf)
       print(f'--- scale_factor={sf} ---'); print(out, '\n')
   # 再固定 scale_factor=1.0,扫描长度
   for L in [50, 300, 800]:
       torch.manual_seed(1)
       print(f'--- len={L} ---'); print(sample(model, 'The island', L, 1.0)[:80], '...')
   ```
3. **需要观察的现象**:`scale_factor=2.0` 的输出更「守规矩」——更依赖高频词、结构重复感强;`0.3` 的输出更「野」——出现不常见字符组合甚至非词;`1.0` 介于两者之间。更长的输出让退化模式(重复、跑题)更明显。
4. **预期结果**:温度从高到低,文本呈现「保守重复 → 平衡 → 多样但杂乱」的渐变;具体生成内容待本地验证(取决于训练轮数)。
5. 若短训练模型的输出还难以分辨,可只比较 `scale_factor=0.3` 与 `2.0` 两个极端,差异最容易看出。

#### 4.3.5 小练习与答案

**练习 1**:`Categorical(logits=scaled_logits)` 与先 `softmax` 再 `Categorical(probs=...)` 等价吗?

**答案**:等价。`Categorical` 的 `logits` 与 `probs` 两个入口最终得到同一个分布,`logits` 入口内部会自动做(数值更稳定的)softmax。仓库选 `logits` 入口,省去手工 softmax 一步。

**练习 2**:本讲的 `scale_factor` 与常见文献里的「温度 \( T \)」(softmax 前除以 \( T \))是什么关系?

**答案**:互为倒数,\( s = 1/T \)。`scale_factor=2.0` 等价于 \( T = 0.5 \)(更尖),`scale_factor=0.5` 等价于 \( T = 2 \)(更平)。阅读其他资料时注意换算方向,别把「高温更多样」与本讲的「大 scale_factor 更保守」弄反。

**练习 3**:为什么每次生成前都要 `torch.manual_seed(1)` 再调用 `sample`?

**答案**:`sample` 内部有随机采样(还有 numpy 侧的输出),不固定种子则每次运行结果不同,温度之间的对比就混入了随机波动;固定种子后,同一温度下输出可复现,不同温度的输出差异才能归因于温度本身(仓库 317 与 324 行正是这么做的)。

### 4.4 上下文窗口、隐状态截断与生成质量评估

#### 4.4.1 概念说明

**上下文窗口**:训练时每个样本只含 40 个字符,模型的条件分布 \( P(s_t \mid s_{t-39..t-1}) \) 只在 40 步内被监督过。生成时隐状态虽然可以无限传递,但模型「可靠记忆」的跨度大体由训练窗口决定——想让模型学会更长的依赖(比如跨段的人名、伏笔),就要增大 `seq_length`,代价是展开步数更多、梯度链更长。

**长序列截断**:一些 RNN 训练实现在长文本上「每 N 步做一次 `backward` 但隐状态继续传递」(截断式 BPTT, truncated BPTT)。本讲规格中提到的「隐藏状态长序列截断」**在仓库代码中没有显式实现**;仓库的真实做法是更彻底的隐式截断——把长文本切成互不重叠监督的 41 字符窗口,每个 batch 重新置零隐状态(4.2.1),梯度只在 40 步内回传、跨窗口为零。两种做法的目标一致:不让反向传播沿整本书展开。

**两种生成思路的对照**:仓库的 `sample` 用「隐状态传递」维护上下文——每生成一个字符只做一次前向,计算量与生成长度无关地恒为 \( O(1) \)/字符。另一种常见思路是每一步把「起始串 + 已生成部分」整体重新喂入模型取末端输出(**示例代码**,仓库未采用):

```python
# 示例代码:拼接上下文式生成(每步整体重算,思路演示)
# 注意:本仓库的 RNN.forward 一次只吃一个字符,直接整段喂入需改造循环,
# 因此这里只示意流程,不针对 ch15_part3.py 直接可运行
for i in range(len_generated_text):
    对 (starting_str + generated) 全序列逐步前向        # O(当前长度)
    取最后一个位置的 logits → 采样 → 追加一个字符
```

隐状态传递是「增量」、拼接重算是「全量」,前者快、后者每步都显式重读全部上文。仓库实现选择了前者:预热一次、之后每字符一次前向。

**生成质量评估**:字符级语言模型在本章的评估方式是**人眼阅读**生成文本——能否拼出英文单词、空格换行是否自然、有没有局部像凡尔纳的句式。学术上更客观的指标是**困惑度**(perplexity,等于平均交叉熵的指数 \( e^{\text{CE}} \)),即「模型对真实文本下一个字符平均有多少个等概率的候选」;仓库未实现困惑度计算,本讲以人工检视与温度对照实验作为评估手段。

#### 4.4.2 核心流程

窗口、截断与评估三者在代码中的落点:

```text
seq_length=40 决定窗口 ──→ chunk_size=41(样本构造)
                        ──→ for c in range(seq_length)(训练展开步数)
每个 batch init_hidden ──→ 梯度截断在窗口内(隐式 TBPTT)
sample 预热 L-1 步     ──→ 生成时的上下文注入
人眼阅读生成文本        ──→ 本章的质量评估
```

#### 4.4.3 源码精读

- [ch15/ch15_part3.py:117-118](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L117-L118):`seq_length = 40`、`chunk_size = seq_length + 1`——窗口大小的单一事实来源,样本构造与训练循环共用。
- [ch15/ch15_part3.py:220](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L220):每个 epoch 调用 `init_hidden`,隐状态不跨 batch——梯度回传被限制在 40 步内,这是仓库对「长序列」的实际处理。
- [ch15/ch15_part3.py:226-228](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L226-L228):展开步数正是 `seq_length`,与窗口严格一致;窗口多长,计算图就多长。
- [ch15/ch15_part3.py:283-286](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L283-L286):预热循环把起始串读进隐状态后,`last_char` 从起始串最后一个字符起步——生成阶段的「上文」完全寄存在 `(hidden, cell)` 里,不再回看原文。
- [ch15/ch15_part3.py:297-299](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py#L297-L299):固定种子后调用 `sample`,输出结果供人眼评估;书中的示例输出呈现「单词拼写大体正确、语法松散」的文本,这正是字符级 LM 的典型水平。

#### 4.4.4 代码实践

1. **实践目标**:体会上下文窗口与起始串对生成内容的「定向」作用。
2. **操作步骤**(用 4.2.4 训练并加载的模型):
   ```python
   for start in ["The island", "Cyrus Harding", "Top"]:
       torch.manual_seed(1)
       print(f'--- start={start!r} ---')
       print(sample(model, starting_str=start,
                    len_generated_text=200, scale_factor=1.0), '\n')
   ```
   再把 `seq_length` 从 40 改成 10 与 120(连带 `chunk_size` 自动变化)各重新训练一次短轮数,用同一起始串生成对比。
3. **需要观察的现象**:不同起始串把生成引向不同的开头;窗口 10 的模型输出更像「字符级马尔可夫链」(单词破碎),窗口 120 的模型在长程上更连贯但训练更慢。
4. **预期结果**:起始串不同,生成的开头不同;`seq_length` 越大,局部连贯性一般越好、训练开销越大(具体文本质量待本地验证)。
5. 显存或内存不足时,优先减小 `rnn_hidden_size` 或 `batch_size` 再重试。

#### 4.4.5 小练习与答案

**练习 1**:生成 1000 个字符时,模型真的「记得」1000 步之前的内容吗?

**答案**:不可靠。隐状态虽逐步传递,但训练样本只有 40 个字符,模型被监督的条件分布至多覆盖 40 步上文;超出窗口的依赖从未在训练中出现,隐状态里更早的信息会逐步稀释。可靠上下文长度大体由 `seq_length` 决定。

**练习 2**:把 `seq_length` 提到 200 就一定能学到更长依赖吗?有什么代价?

**答案**:不一定。梯度链变成 200 步,即使 LSTM 缓解了梯度消失,长程梯度仍可能衰减或训练更慢更不稳;同时每个样本的序列变长、每 epoch 计算量约增至 5 倍,样本数也减少。窗口大小是「可学的依赖长度」与「训练难度/开销」的权衡。

**练习 3**:如果要用困惑度代替人眼评估,应该怎么算?

**答案**:在留出的一段真实文本上,把模型每步对真实下一字符的交叉熵取平均,再取指数:\( \text{PPL} = \exp\left(\frac{1}{T}\sum_t -\log P(s_t \mid s_{<t})\right) \)。困惑度越低,模型给真实文本的概率越高。仓库未实现该指标,需自行编写评估循环(**示例思路**,非仓库代码)。

## 5. 综合实践

**任务:打造一个可保存、可复现、可调温的「凡尔纳文风生成器」,并完成温度 × 长度扫描实验。**

1. **训练**:在 CPU 可承受的前提下训练模型(如 `num_epochs=2000~3000`;无 GPU 时把 `device` 改为 `'cpu'`),记录每 500 轮打印的损失。
2. **保存与加载闭环**:用 4.2.3 的示例代码把 `state_dict` 存为 `char_lm_state.pt`;重启进程后重新搭建 `RNN` 结构、`map_location` 加载权重并 `eval()`,确认模型「复活」(对同一输入 logits 一致)。这一步补上了仓库缺失的 checkpoint 环节。
3. **参数扫描**:固定 `torch.manual_seed(1)` 与起始串 `'The island'`,对 `scale_factor ∈ {0.3, 0.5, 1.0, 2.0}` 与 `len_generated_text ∈ {100, 500}` 做网格生成,把每个组合的开头 100 个字符抄进一张表。
4. **分析与结论**:回答三个问题——
   - 哪个温度下单词拼写正确率最高?哪个温度下多样性最好但开始胡言乱语?
   - 更长的生成是否放大了重复/跑题?在什么长度上开始退化?
   - 结合 4.3.1 的 softmax 数学,解释观察到的渐变为什么必然发生。
5. **预期结果**:呈「保守→平衡→杂乱」的温度渐变;`scale_factor=1.0` 附近通常是拼写与多样性的折中。具体文本一律以本地生成为准(待本地验证)。

## 6. 本讲小结

- 语言模型的训练信号来自文本自身:41 字符的滑窗块(step=1)错位一步切成 x=前 40、y=后 40,每个位置都在预测下一字符——「同步多对多」架构的自监督学习。
- 仓库的 `RNN` 每次前向只吃一个字符,把时间展开显式写进训练循环:40 步累加损失、一次 `backward()`,BPTT 一目了然;每个 epoch 重置隐状态并取一个随机 batch,梯度被隐式截断在 40 步窗口内。
- 仓库**没有** checkpoint 保存与预训练权重文件,模型需自己训练,保存/加载用 `state_dict` + `map_location` 补全(承接 u13-l2)。
- `sample()` 先用起始串前 \( L-1 \) 个字符预热隐状态,再「前向 → 温度缩放 → Categorical 采样 → 拼接」自回归生成;`m.sample()` 返回 0 维张量,可直接索引 `char_array` 取回字符。
- 温度 `scale_factor` 与常规温度互为倒数:放大更保守、缩小更多样;固定随机种子才能把输出差异归因于温度。
- 生成质量靠人眼评估(拼写、空格、句式);困惑度是更客观的指标但仓库未实现;可靠上下文长度由 `seq_length` 决定。

## 7. 下一步学习建议

本讲结束后,第 15 章全部完成。建议按以下顺序继续:

1. **下一讲 u16-l1「自注意力与多头注意力从零实现」**:LSTM 的顺序计算与有限窗口是它的根本瓶颈,Transformer 用自注意力一步实现「任意两位置直接交互」,请带着「它解决了 RNN 的什么痛点」的问题去读 `ch16/ch16-part1-self-attention.py`。
2. **回看本讲代码做一个小实验**:把 `nn.LSTM` 换成 `nn.RNN`/`nn.GRU` 对比同轮数下的生成质量,直观感受门控的价值(修改自己本地副本)。
3. **延伸阅读**:GPT 类模型的「下一个 token 预测 + 温度采样」与本讲的字符级生成在原理上一脉相承,u16-l2 的 GPT-2 微调可以看作本讲思想在真实大模型上的放大版。
