# 数据管线：tokenized .bin、dataloader 与 tokenizer

## 1. 本讲目标

本讲承接 [u1-l3 CPU 参考实现全景与训练主循环](u1-l3-cpu-reference-overview.md)。上一讲我们把 `train_gpt2.c` 的训练主循环看了一遍，其中出现了三行神秘调用：

```c
dataloader_next_batch(&train_loader);          // 取一个 batch
gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);
```

但那个 batch 从哪里来？`inputs` 和 `targets` 长什么样？生成时打印出的英文又是怎么从「一个整数 token id」变回「人类可读文字」的？这些就是本讲要回答的问题。

学完本讲，你应当能够：

1. 说清楚 llm.c 的 `.bin` 训练数据文件长什么样（1024 字节头 + `uint16` token 流），并理解 Python 端如何生成它。
2. 看懂 `llmc/dataloader.h` 如何把 `.bin` 切成一个个 batch、如何在多进程下分片、如何让 `inputs` 与 `targets`「错位一位」从而形成「预测下一个 token」的学习目标。
3. 理解 `llmc/tokenizer.h` 为什么只实现了「解码」、它如何加载词表、以及它在自回归生成里扮演的角色。

## 2. 前置知识

- **token（词元）**：大语言模型不直接读字符或词，而是读「整数 id」。文本先被分词器切成一段段 token，每段对应一个整数。GPT-2 的词表大小是 50257。
- **uint16**：16 位无符号整数，能表示 0 到 65535。GPT-2 的 token id 都在这个范围内，所以一个 token 用 2 个字节存就够，比直接存文本省空间、读取更快。
- **batch（B）与 sequence length（T）**：训练时一次喂给模型 B 条独立的句子，每条句子长度为 T 个 token。CPU 版默认 `B=4, T=64`（见 [train_gpt2.c:1090-1091](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1090-L1091)）。
- **下一个 token 预测（next-token prediction）**：GPT 的训练目标非常简单——给定前 i 个 token，预测第 i+1 个 token。所以同一个序列既是「输入」也是「答案」，只需错开一位。
- **EOT（end of text）**：GPT-2 用 `<|endoftext|>`（id = 50256）标记一段文本的结束，下一篇文章从这里重新开始。

> 目录与术语（`llmc/`、`dev/data`、`B/T/C/V`、`floatX`）在 [u1-l1 项目总览](u1-l1-project-overview.md) 中已介绍，本讲不再重复。

## 3. 本讲源码地图

| 文件 | 语言 | 作用 |
|------|------|------|
| `dev/data/data_common.py` | Python | 提供 `write_datafile`，把 token 列表写成 `.bin`（256 个 int32 的头 + token 流），是 Python 与 C 之间的「数据格式契约」。 |
| `dev/data/tinyshakespeare.py` | Python | 下载 TinyShakespeare 文本，用 GPT-2 分词器切成 token，调 `write_datafile` 产出 `tiny_shakespeare_train.bin` / `tiny_shakespeare_val.bin`。 |
| `llmc/dataloader.h` | C | 定义 `DataLoader`：负责 glob 多个分片、校验文件头、把 token 流切成 batch、按进程分片，并产出 `inputs`/`targets`。 |
| `llmc/tokenizer.h` | C | 定义 `Tokenizer`：只支持**解码**（token id → 字符串），加载 `gpt2_tokenizer.bin` 词表，用于把生成出的 token 打印成文字。 |

一句话概括数据流向：

```
原始文本 --(Python: 分词)--> token 列表 --(write_datafile)--> .bin 文件
                                                              |
                                  (C: dataloader 读取)         |
            一个 batch 的 inputs / targets  <-----------------+
                                  |
            模型前向训练 / 生成 token -> (tokenizer 解码) -> 人类可读文字
```

## 4. 核心概念与源码讲解

### 4.1 .bin 文件格式与数据集脚本

#### 4.1.1 概念说明

为什么不直接训练时才分词？因为分词（尤其正则）很慢，而训练要重复读取数据几百万次。llm.c 的做法是：**离线**把文本分好词，存成最朴素的二进制 `.bin`，训练时 `fread` 直接读，快到极致。

`.bin` 的格式非常简单：

```
+-----------------------------------+
| 头部：256 个 int32（= 1024 字节） |
|   [0] = magic   （魔数，校验用）  |
|   [1] = version （版本号）        |
|   [2] = ntok    （token 总数）    |
|   [3..255] = 0  （保留）          |
+-----------------------------------+
| token 流：ntok 个 uint16          |
|   每个 uint16 是一个 token id     |
+-----------------------------------+
```

「魔数（magic number）」是一种文件自校验机制：写文件时塞一个约定好的奇怪整数（GPT-2 数据用的是 `20240520`，看起来像日期），读文件时先检查这个数对不对，不对就立刻报错——这样能避免你拿一个错误的文件（比如用错格式的旧数据）去训练，浪费几小时才发现。

#### 4.1.2 核心流程

`tinyshakespeare.py` 做三件事：

1. **下载**：从 GitHub 拉取 `tiny_shakespeare.txt` 原始文本。
2. **分词**：按空行 `\n\n` 把全文切成一段段「文档」，每段前面塞一个 EOT token，再用 `tiktoken` 的 GPT-2 编码器把字符串变成 token id 列表。
3. **切分与落盘**：前 32768 个 token 作为验证集（val），其余作为训练集（train），分别调 `write_datafile` 写成两个 `.bin`。

`data_common.py` 的 `write_datafile` 则是真正「按格式落盘」的地方：

```text
构造 header[256]（int32）
  header[0] = magic        # gpt-2: 20240520
  header[1] = version      # gpt-2: 1
  header[2] = len(toks)    # token 个数
把 token 列表转成 numpy 数组（gpt-2 用 uint16）
先写 header.tobytes()，再写 tokens.tobytes()
```

#### 4.1.3 源码精读

不同「模型格式」的魔数、版本、token 字宽都登记在一张表里，`write_datafile` 按描述符查表：

[gpt-2 用 magic=20240520、version=1、token_dtype=uint16；llama-3 用 magic=20240801、version=7、uint32](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py#L26-L37)

> 注意：llama-3 的 token id 可以超过 65535，所以必须用 `uint32`（4 字节）存；GPT-2 词表只有 50257 个，`uint16` 足够。本讲与 CPU 参考实现只涉及 gpt-2 格式。

真正落盘的函数，把 256 个 int32 的头和 token 流依次写出：

[write_datafile：header = 256 个 int32，header[0]=magic、[1]=version、[2]=len(toks)，随后写出 uint16 token 流](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py#L39-L60)

据此可算出文件大小公式：

\[
\text{file\_size} = 256 \times 4 + n_{tok} \times 2 \quad \text{(字节)}
\]

`tinyshakespeare.py` 的分词逻辑：按 `\n\n` 切段、每段前加 EOT、前 32768 个作 val：

[tokenize：text.split("\n\n") 切段，每段 tokens.append(eot)，前 32768 作 val，其余作 train，调 write_datafile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/tinyshakespeare.py#L47-L77)

文件顶部注释里给出了预期输出，可作为你运行时的对照基准：

[运行示例：writing 305,260 tokens to ...tiny_shakespeare_train.bin (611,544 bytes)](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/tinyshakespeare.py#L9-L12)

可以用上面的公式验证：`611544 = 1024 + 305260×2`？算一下 `305260×2 = 610520`，再加 `1024 = 611544`，✅ 完全吻合。

#### 4.1.4 代码实践

**实践目标**：亲手生成 TinyShakespeare 的 `.bin`，并验证文件大小与公式一致。

**操作步骤**：

1. 安装依赖（仓库需要 `tiktoken`、`transformers`、`requests`、`tqdm`、`numpy`）。
2. 在仓库根目录运行：

   ```bash
   python dev/data/tinyshakespeare.py --model gpt-2
   ```

3. 查看生成的文件大小（Linux）：

   ```bash
   ls -l dev/data/tinyshakespeare/tiny_shakespeare_train.bin
   ```

**需要观察的现象 / 预期结果**：

- 脚本会先打印 `Downloading ...`（或 `already exists`），再打印两行 `writing N tokens to ... (M bytes)`。
- `tiny_shakespeare_train.bin` 的大小应等于 `1024 + N×2` 字节，其中 N 是打印出来的 token 数。
- val 文件固定为 `1024 + 32768×2 = 66560` 字节（与文件头注释一致）。

**待本地验证**：train 文件的实际 token 数取决于下载文本与 tiktoken 版本，若与注释里的 305260 略有出入属正常，只要满足上面的等式即可。

> 如果你的环境无法联网下载文本，可跳到 4.2.4 的「源码阅读型实践」，它不需要真正运行。

#### 4.1.5 小练习与答案

**练习 1**：假设某 `.bin` 文件大小是 1024 字节，它里面有几个 token？

**参考答案**：根据公式 `file_size = 1024 + ntok×2`，`1024 = 1024 + ntok×2` ⇒ `ntok = 0`。即只有头、没有任何 token，是个空数据文件。

**练习 2**：为什么 llama-3 格式必须用 `uint32` 而 gpt-2 可以用 `uint16`？

**参考答案**：`uint16` 最大只能表示 65535。GPT-2 词表只有 50257 个 token，放得下；而 llama-3 词表超过 12 万（如 EOT 就在 128000），必须用 `uint32`。

---

### 4.2 DataLoader 初始化与 next_batch

#### 4.2.1 概念说明

有了 `.bin` 文件，还需要一个组件在训练时按需取出一个个 batch——这就是 `DataLoader`。它要解决几个问题：

1. **切 batch**：从一长串 token 里，每次取出 `B×T` 个 token 作为一批输入。
2. **错位一位造目标**：让 `targets[i] = inputs[i+1]`，把「预测下一个 token」直接编码进数据。
3. **多进程分片**：多卡训练时，每张卡（进程）只看数据的一部分，互不重叠，`DataLoader` 用 `process_rank` / `num_processes` 实现。
4. **多分片（shard）与 shuffle**：数据可以拆成多个 `.bin` 文件（分片），loader 用 `glob` 通配符一次接管所有分片，并在 epoch 之间打乱顺序。

#### 4.2.2 核心流程

`DataLoader` 的生命周期：

```text
dataloader_init(pattern, B, T, rank, num, shuffle)
  ├── glob(pattern) 收集所有分片文件
  ├── 逐个打开分片，校验头（magic=20240520, version=1）、算 ntok
  ├── 预分配 buffer[B*T+1]、inputs[B*T]、targets[B*T]
  └── dataloader_reset()：回到第 0 个分片、第 0 个样本

训练循环反复调用：
dataloader_next_batch(loader)
  └── dataloader_load_batch(loader)
        ├── fseek 到本进程本样本在文件中的偏移
        ├── fread 读 B*T+1 个 uint16 进 buffer
        └── for i in 0..B*T-1:
              inputs[i]  = buffer[i]
              targets[i] = buffer[i+1]     # 错位一位
```

多进程分片的关键偏移量有两个：

- `total_batch_size_bytes = num_processes × B × T × sizeof(uint16)` —— 一个「全局 batch」跨所有进程占的字节数。
- `local_batch_offset_bytes = process_rank × B × T × sizeof(uint16)` —— 本进程在自己那个全局 batch 里的字节起点。

读取位置 = 头部 + (第 idx 个全局 batch 的字节) + (本进程偏移)。

#### 4.2.3 源码精读

`DataLoader` 结构体集中存放「分布式参数 + batch 尺寸 + 文件游标 + 三个数据缓冲」：

[DataLoader 结构体：process_rank/num_processes、B/T、glob_result、current_shard_idx/current_sample_idx、buffer/inputs/targets](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L29-L59)

加载并校验单个分片：读 256 个 int32 的头，校验魔数和版本，再根据 `ntok` 反推文件大小是否吻合：

[dataloader_load_shard_：校验 header[0]==20240520、header[1]==1，读 ntok=header[2]，断言 expected_file_size 一致](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L61-L97)

> 这里的 `20240520` 正是 `data_common.py` 里 `HEADERS_INFO["gpt-2"]["magic"]` 写进去的值——同一个魔数在 Python 写端和 C 读端各出现一次，构成跨语言的「格式契约」。`HEADER_SIZE` 定义为 256（见 [dataloader.h:27](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L27)），所以 `HEADER_SIZE * sizeof(int)` = 1024 字节，与 4.1 完全对齐。

`dataloader_init` 计算两个分片偏移量、glob 收集分片、挨个校验、预分配三个缓冲：

[dataloader_init：total_batch_size_bytes 与 local_batch_offset_bytes 的计算，glob 收集分片，逐个校验并断言每个分片至少 num_processes×B×T+1 个 token，分配 buffer[B*T+1]/inputs/targets](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L142-L201)

> 注意 `buffer` 分配的是 `B*T+1` 个元素，比 `inputs`/`targets`（各 `B*T`）多一个。这「多出来的一个」就是为了下面错位一位时不越界。

最核心的一行行解码，把 token 流变成 inputs/targets，正是「下一个 token 预测」的物化：

[dataloader_load_batch：fseek 到本进程偏移，fread 读 B*T+1 个 uint16，循环 inputs[i]=buffer[i]、targets[i]=buffer[i+1]](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L203-L220)

外层调度：取下一个 batch，若当前分片读完就推进到下一个分片（`dataloader_advance_` 会自动跨 epoch）：

[dataloader_next_batch：若 current_sample_idx 超过 shard_num_samples 则 advance，再 load_batch](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L222-L229)

训练主循环里的真实用法（CPU 版单进程：rank=0、num=1）：

[dataloader_init(train_loader, train_tokens, B=4, T=64, rank=0, num=1, shuffle=1)，val_loader 同形参但 shuffle=0](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1092-L1094)

每个训练步取一个 batch 喂给前向：

[dataloader_next_batch(&train_loader); gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1164-L1165)

#### 4.2.4 代码实践

**实践目标**：用一个最小例子，亲手验证 `dataloader_load_batch` 里「错位一位」的逻辑，理解 inputs/targets 如何形成「预测下一个 token」。

**源码阅读型实践（无需运行环境）**：

1. 假设 `B=1, T=5`，分片里某处连续的 token id 为：

   ```text
   buffer 位置:  0    1    2    3    4    5
   token id:    [10,  20,  30,  40,  50,  60]
   ```

   （`dataloader_load_batch` 会读 `B*T+1 = 6` 个元素进 buffer。）

2. 对照 [dataloader.h:216-219](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L216-L219) 的循环，逐行填表：

   | i | inputs[i] = buffer[i] | targets[i] = buffer[i+1] | 含义 |
   |---|------------------------|---------------------------|------|
   | 0 | 10 | 20 | 看到 10，要预测 20 |
   | 1 | 20 | 30 | 看到 20，要预测 30 |
   | 2 | 30 | 40 | … |
   | 3 | 40 | 50 | … |
   | 4 | 50 | 60 | … |

3. **需要观察的现象 / 预期结果**：`targets` 正好比 `inputs` 整体左移一位——位置 i 的输入对应位置 i+1 的答案。这就是 GPT「自回归」训练的来源：模型在位置 0 拿到 token 10，前向后输出一个 50257 维概率分布，期望它把概率压在「20」上（与 `targets[0]` 算交叉熵）。

4. **进阶（可选，运行型）**：若你已在 4.1.4 跑通了 `tinyshakespeare.py`，再运行 `./dev/download_starter_pack.sh`（见 [download_starter_pack.sh 顶部 BASE_URL 与 FILES 列表](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh#L1-L29)），用 Python 读取 `tiny_shakespeare_val.bin`：跳过前 1024 字节头，按 `uint16` 解析前 6 个 token，验证它们确实是 GPT-2 token id（都在 0–50256 之间）。

   ```python
   import numpy as np
   raw = np.fromfile("dev/data/tinyshakespeare/tiny_shakespeare_val.bin", dtype=np.uint8)
   header = raw[:1024].view(np.int32)        # 256 个 int32
   assert header[0] == 20240520, "magic mismatch"
   toks = raw[1024:].view(np.uint16)
   print("ntok from header:", header[2], "actual:", len(toks))
   print("first 6 tokens:", toks[:6])
   ```

   **待本地验证**：`header[2]` 应等于 `len(toks)`；前几个 token 通常是 EOT(50256) 开头的一段莎士比亚文本片段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `buffer` 要分配 `B*T+1` 个元素，而不是 `B*T`？

**参考答案**：因为 `targets[i] = buffer[i+1]`，最后一个 `targets[B*T-1] = buffer[B*T]`。如果只分配 `B*T` 个元素，访问 `buffer[B*T]` 就越界了。多读的那一个 token 正是为这「错位一位」服务。

**练习 2**：单进程（`num_processes=1`）时，`local_batch_offset_bytes` 是多少？两个进程时，rank=1 的进程从哪里开始读？

**参考答案**：单进程时 `local_batch_offset_bytes = 0 × B×T×2 = 0`，从头读。两进程时 rank=1 的偏移是 `1 × B×T×2` 字节，即跳过 rank=0 那一份 `B×T` 个 token 之后才开始读，保证两张卡看到不同数据。

**练习 3**：`shard_num_samples`（每个分片能切出多少个全局 batch）是怎么算的？

**参考答案**：见 [dataloader.h:95](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L95)，化简后为

\[
\text{shard\_num\_samples} = \left\lfloor \frac{n_{tok} - 1}{\text{num\_processes} \times B \times T} \right\rfloor
\]

分子上的 `-1` 正是因为每个 batch 实际要消费 `B*T+1` 个 token、多出一个用于错位。

---

### 4.3 Tokenizer 加载与解码

#### 4.3.1 概念说明

数据流里 token id 是整数，但生成时我们要打印给人看的是文字。这就需要一个「解码器」：把 token id 翻译回字符串。这正是 `llmc/tokenizer.h` 的职责。

> 为什么 C 版**只实现解码、不实现编码**？因为训练数据已经离线分好词存进 `.bin` 了，C 端不需要再分词；而无条件生成（从 EOT 开始自由续写）也只需要解码。文件头注释明确说明了这一点（[tokenizer.h:1-7](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L1-L7)）。编码涉及复杂正则，在 C 里实现麻烦，故留待需要「提示词（prompt）」时再加。

GPT-2 用的是 **BPE（Byte-Pair Encoding）** 分词：把文本先拆成单字节，再按频率把常见字节对合并成更长的 token。词表里既有整词（如 `hello`），也有词片（如 `ello`），还有单个字节。所以解码时只需一张「id → 字符串」的查找表。

#### 4.3.2 核心流程

`gpt2_tokenizer.bin`（由 `train_gpt2.py` 生成，CPU 端只读不写）的格式：

```text
头部：256 个 uint32
  [0] = 20240328        # tokenizer 专用魔数（注意和数据 .bin 的 20240520 不同！）
  [1] = version         # 1 或 2
  [2] = vocab_size      # 词表大小（GPT-2 为 50257）
  [3] = eot_token       # 仅 version>=2 才写；version 1 默认 50256
词表：连续 vocab_size 个条目，每个条目：
  1 字节 length + length 个原始字节（即该 token 的字符串）
```

加载流程：

```text
tokenizer_init(file)
  ├── 读 256 个 uint32 的头，校验 magic==20240328
  ├── 据 version 取 vocab_size 与 eot_token
  └── 循环 vocab_size 次：读 length，读 length 字节，存进 token_table[id]

tokenizer_decode(token_id)
  └── return token_table[token_id]   # 查表，O(1)

safe_printf(piece)
  └── 只打印可打印字符/空白，过滤掉控制字节（BPE 词表里有原始字节）
```

#### 4.3.3 源码精读

`Tokenizer` 结构体：一张字符串指针数组 + 一个 EOT id + 一个初始化成功标志：

[Tokenizer 结构体：vocab_size、token_table (char**)、init_ok、eot_token](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L18-L23)

`safe_printf` 负责把原始字节安全地打印出来——BPE 词表里很多 token 是不可打印的控制字节，直接 `printf` 会搞乱终端：

[safe_printf：单字节 token 时用 isprint/isspace 过滤掉奇怪的控制字节](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L25-L39)

`tokenizer_init` 读头、校验魔数 `20240328`、按 version 取 eot、再逐条读入词表（每条先读 1 字节长度，再读对应字节数，补 `\0` 方便打印）：

[tokenizer_init：读 256 个 uint32 头，assert magic==20240328，version 1 默认 eot=50256、version 2 从 header[3] 读 eot，循环读 length+bytes 填 token_table](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L41-L84)

解码就是一次数组下标查询：

[tokenizer_decode：若 init_ok 且 token_id < vocab_size，返回 token_table[token_id]，否则返回 NULL](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L86-L96)

生成循环里的真实用法：每生成一个 token，就用 tokenizer 解码并安全打印；若词表加载失败则退回打印 id：

[生成循环：tokenizer_decode(&tokenizer, next_token) 得到字符串，safe_printf 打印；init_ok 为 0 时退回打印数字 id](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1149-L1156)

> 注意三个文件用了三个不同的魔数，互不冲突：训练/验证数据 `.bin` 用 `20240520`、评测数据用 `20240522`、tokenizer 用 `20240328`。读错文件类型时魔数校验会第一时间拦住你。

#### 4.3.4 代码实践

**实践目标**：理解 tokenizer 在生成回路中的位置，并搞清 EOT 的来源。

**操作步骤（源码阅读型）**：

1. 打开 [train_gpt2.c:1099-1101](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1099-L1101)，确认 tokenizer 从 `gpt2_tokenizer.bin` 加载。
2. 打开 [train_gpt2.c:1126-1130](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1126-L1130)，看生成开始前如何用 `tokenizer.eot_token` 填满 `gen_tokens`。
3. 追问：这里的 `tokenizer.eot_token` 是从哪来的？

**需要观察的现象 / 预期结果**：

- `eot_token` 来自 [tokenizer_init](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L41-L84)：若 `gpt2_tokenizer.bin` 是 version 2，则等于 `header[3]`；若是 version 1，则默认为 50256。在 GPT-2 里这个值就是 50256，对应 `<|endoftext|>`。
- 把 `gen_tokens` 全填成 EOT，相当于让模型从「一段文本结束」的位置开始续写，从而无条件地生成新文本。

**可选运行型实践（待本地验证）**：跑通 CPU 版后（`make train_gpt2` 并运行），在第 20 步会看到 `generating:` 字样，随后打印出一段由 `safe_printf(tokenizer_decode(...))` 逐 token 拼出的英文。若你故意把 `gpt2_tokenizer.bin` 重命名让它加载失败，会看到生成内容变成一串数字 id（走 `init_ok==0` 的 fallback 分支）。

#### 4.3.5 小练习与答案

**练习 1**：tokenizer 的魔数 `20240328` 和训练数据的魔数 `20240520` 为什么不一样？

**参考答案**：它们是两种不同的文件（词表文件 vs 训练数据文件），各自用不同魔数自校验，避免被对方误读。`tokenizer_init` 和 `dataloader_load_shard_` 各自只认自己的魔数。

**练习 2**：如果 `gpt2_tokenizer.bin` 不存在，程序会崩溃吗？生成会变成什么样？

**参考答案**：不会崩溃。[tokenizer_init](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L41-L84) 在打不开文件时只把 `init_ok` 置 0 并打印警告；生成时走 [train_gpt2.c:1153-1156](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1153-L1156) 的 fallback，打印原始 token id（一串数字）而不是文字。

**练习 3**：`safe_printf` 为什么要对单字节 token 做 `isprint/isspace` 检查？

**参考答案**：BPE 词表里有把任意单字节当 token 的条目（因为 BPE 从字节层面开始合并），其中包含退格、响铃等控制字符。直接打印会把终端搞乱，所以 `safe_printf` 把这些不可打印字节静默丢弃。

## 5. 综合实践

**任务：用 Python 写一个 ~20 行的「迷你 DataLoader」复现 C 版的核心读取逻辑，验证整条数据管线。**

要求：

1. 接收一个 `.bin` 路径、`B`、`T`，模拟单进程（rank=0、num=1）。
2. 读前 1024 字节作为头（256 个 int32），校验 `header[0] == 20240520`，打印 `ntok = header[2]`。
3. 实现一个 `next_batch(start_sample_idx)` 函数：从 `1024 + start_sample_idx*B*T*2` 字节处读 `B*T+1` 个 `uint16`，返回 `inputs = buf[:-1]`、`targets = buf[1:]`（这正是 [dataloader_load_batch](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L203-L220) 的等价 numpy 写法）。
4. 取出第一个 batch，断言 `len(inputs) == B*T` 且 `targets[:-1] == inputs[1:]`（即错位一位成立）。
5. （加分）如果你也下载了 `gpt2_tokenizer.bin`，按 [tokenizer_init](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L41-L84) 的格式解析它，把第一个 batch 的 inputs 解码成文字，确认读出来的是莎士比亚英文片段。

**预期结果**：第 4 步断言通过，说明你已经独立复现了 C 版 DataLoader 的「错位一位造目标」核心；第 5 步若完成，则端到端打通了「`.bin` → batch → 文字」整条管线。若缺少数据文件，第 1–4 步可用自己用 `write_datafile` 伪造的 `.bin` 替代——这一步**待本地验证**。

## 6. 本讲小结

- llm.c 的训练数据是离线分词后存成的 `.bin`：**1024 字节头（256 个 int32：magic/version/ntok）+ `uint16` token 流**，文件大小满足 `1024 + ntok×2`。
- Python 端 `write_datafile`（[data_common.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py)）写、C 端 `dataloader_load_shard_`（[dataloader.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h)）读，靠同一个魔数 `20240520` 形成跨语言契约。
- `DataLoader` 用 `process_rank`/`num_processes` 实现多卡分片，用 `glob` 接管多分片并支持 shuffle，核心的 `dataloader_load_batch` 读 `B*T+1` 个 token 并让 `targets[i]=buffer[i+1]`，把「预测下一个 token」直接编进数据。
- `llmc/tokenizer.h` 只做**解码**：加载 `gpt2_tokenizer.bin`（魔数 `20240328`，与数据文件不同），用一张 id→字符串查表把生成的 token 打印成文字；`safe_printf` 负责过滤 BPE 词表里的控制字节。
- 生成时把 `gen_tokens` 全填成 `tokenizer.eot_token`（GPT-2 为 50256），相当于从「文本结束」位置开始无条件续写。

## 7. 下一步学习建议

到这里，你已经知道数据如何变成 batch、token 如何变回文字。接下来建议：

1. **进入 Unit 2 逐层剖析前向**：先读 [u2-l1 编码层 encoder](u2-l1-encoder-layer.md)，看 `inputs`（本讲产出的 token id）如何经 `encoder_forward` 查 `wte`/`wpe` 变成嵌入向量——这是数据真正「进入模型」的第一步。
2. **想了解评测数据**：本讲的 `EvalLoader`（[dataloader.h:251-519](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L251-L519)）服务于 HellaSwag/MMLU 等多选题评测，留到 [u7-l2 评测](u7-l2-evaluation-sampler.md) 再深入，届时你会看到它与 `write_evalfile`（[data_common.py:62](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py#L62)）的对应关系。
3. **想跑通 CPU 版**：回到 [u1-l2 构建系统与三种运行方式](u1-l2-build-and-run.md)，按 quick start 执行 `./dev/download_starter_pack.sh` + `make train_gpt2`，亲眼看到本讲描述的 batch 流入训练循环、loss 从 ~5.3 下降。
