# 数据管线：MoonDataset 的分词与分块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `examples/toy_train.py` 里数据从「HuggingFace 上的原始文本」到「`[16, 512]` 的 batch 张量」经过的四道工序：加载 → 分词 → 定长分块 → 组批。
2. 解释 `MoonDataset` 的 token 缓存机制：`.bin` 文件何时生成、何时复用、缓存键里**缺了什么**导致它可能「过期」。
3. 推导样本数公式 \( \lfloor N_{token}/L \rfloor \)，说明定长分块为什么让 DataLoader 完全不需要 padding。
4. 独立修改 `max_length`（512 → 256），并定量对比它对每步 token 数、每 epoch 步数、训练速度与 loss 走向的影响。
5. 为脚本补一个独立的语言建模评估函数，在若干 batch 上计算模型 loss，为后续优化器对比实验（u3-l1）提供「训前 / 训后」的统一量尺。

## 2. 前置知识

本讲承接 u1-l2（已经跑通过一次训练，知道 `.bin` 缓存的存在）与 u1-l3（知道训练循环里 `labels=input_ids` 依靠 CausalLM 内部错位 shift 实现下一 token 预测）。在此之上，补充四个概念：

- **token 与分词器（tokenizer）**：语言模型不直接读字符串，而是先把文本切成一串整数（token id）。Qwen2 系列使用 BPE 类分词器，词表大小 151936——这正是 u1-l2 里初始 loss 理论值 \(\ln(151936)\approx 11.93\) 的来源。分词器和模型的 `vocab_size` 必须**同源**：分词器产出的 id 不能超出模型词表的编号范围。
- **HuggingFace `datasets` 库**：`load_dataset("Elriggs/openwebtext-100k")` 会从远端拉取数据集到本地缓存目录，返回一个 `DatasetDict`（类似字典，键是切分名如 `train`）。用 `dataset["train"]["text"]` 可以一次性取出 train 切分中 `text` 列的全部字符串，得到一个 Python 列表。
- **PyTorch 的 `Dataset` 协议**：任何实现了 `__len__()`（返回样本总数）和 `__getitem__(idx)`（返回第 idx 个样本）的类都可以交给 `DataLoader`。DataLoader 负责按 `batch_size` 取样本、用 collate 函数堆成批、可选 shuffle 和多进程加载。本脚本 `num_workers` 用默认值 0，即取样本发生在主进程。
- **「拼接再切块」的语言模型预处理范式**：与大模型预训练的通行做法一致，`MoonDataset` 把所有文档的 token **首尾拼接成一条长流**，再按固定窗口切成样本，而不是一个文档一个样本。这保证每个样本长度相同，从而 DataLoader 组批时不需要 padding。

## 3. 本讲源码地图

本仓库唯一的源码文件是 `examples/toy_train.py`，共 359 行。本讲聚焦第一段，并借用第四段里「消费数据」的几行：

| 行号区域 | 内容 | 归属讲义 |
| --- | --- | --- |
| [L16-L43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L16-L43) | `MoonDataset`：数据集装配、分词缓存、定长分块 | **本讲核心** |
| [L46-L239](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L239) | Newton-Schulz 正交化与 `Muon` 优化器类 | u2 系列 |
| [L242-L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L242-L313) | `get_model_and_dataloader` 与 `get_optimizer` | 本讲读 L242-L254（数据部分），其余归 u2-l1 / u3-l2 |
| [L316-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L359) | `__main__` 入口与训练循环 | u1-l3（本讲只引用其中三处） |

本讲的阅读主线是一条「数据流水线」：

```text
HF 云端数据集 ──load_dataset──▶ DatasetDict
      │ 取出 dataset["train"]["text"]（全部原始文本）
      ▼ 逐篇 tokenizer.encode + 拼接（或直接读 .bin 缓存）
一维 token 长流 self.tokens（Python 列表）
      │ 按 max_length 无重叠切块（__len__ / __getitem__）
      ▼
DataLoader(batch_size=16, shuffle=True) ──▶ [16, 512] 的 long 张量 ──▶ 模型
```

## 4. 核心概念与源码讲解

### 4.1 数据集加载：`name2path` 与 `load_dataset`

#### 4.1.1 概念说明

脚本不写死 HuggingFace 仓库名，而是先经过一张「别名 → 路径」的映射表 `name2path`。这样命令行参数 `--dataset openwebtext-100k` 是个简短别名，真实拉取的是 `Elriggs/openwebtext-100k`——一个约 10 万篇网页文档的小型语料（OpenWebText 的子集），规模正好适合玩具训练。要接入新数据集，只需在这张表里加一行（这是 u3-l5 二次开发实践的落点之一）。

`load_dataset(path, trust_remote_code=True)` 中的 `trust_remote_code=True` 表示允许执行数据集仓库自带的加载脚本（本数据集依赖它）。首次调用会下载并处理数据，之后走本地缓存，不再联网。

#### 4.1.2 核心流程

```text
1. name2path = {"openwebtext-100k": "Elriggs/openwebtext-100k"}
2. dataset = load_dataset(别名对应的路径, trust_remote_code=True)   # 返回 DatasetDict
3. 后续（L21）：texts = dataset["train"]["text"]                   # 取 train 切分的 text 列
```

注意加载发生在 `get_model_and_dataloader` 的**最前面**（L246），早于分词器构造与模型构造——数据下载是整条链路里最耗时、最可能失败的一环，把它放在最前可以尽早暴露网络问题。

#### 4.1.3 源码精读

[examples/toy_train.py:L242-L246](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L242-L246) —— 定义唯一的别名映射，并立刻加载整个 `DatasetDict`。若传入的 `--dataset` 不在表里，会在字典取值处抛 `KeyError`。

```python
def get_model_and_dataloader(model_name, dataset_name, hidden_size):
    name2path = {
        "openwebtext-100k": "Elriggs/openwebtext-100k",
    }
    train_dataset = load_dataset(name2path[dataset_name], trust_remote_code=True)
```

[examples/toy_train.py:L21](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L21) —— 在 `MoonDataset.__init__` 里取出 train 切分的 `text` 列。`dataset["train"]` 是一个 `Dataset`，再 `["text"]` 得到该列所有字符串组成的 Python 列表，从此与 HF datasets 的惰性机制无关，全部驻留内存。

```python
        self.texts = dataset["train"]["text"]
```

#### 4.1.4 代码实践

1. **实践目标**：看清 `load_dataset` 返回的对象长什么样，确认切分名与列名。
2. **操作步骤**：在仓库根目录用 `python -c` 或交互式环境执行（示例代码）：

   ```python
   from datasets import load_dataset
   ds = load_dataset("Elriggs/openwebtext-100k", trust_remote_code=True)
   print(ds)                      # 打印各切分的行数与列名
   print(ds["train"][0]["text"][:200])   # 看第一篇文档的开头
   ```

3. **需要观察的现象**：`print(ds)` 会列出全部切分（train 之外是否还有 validation/test）及各自行数；文档内容是网页正文。
4. **预期结果**：确认 `train` 切分存在且有 `text` 列，L21 的取法成立；具体行数待本地验证（不同数据集版本可能有差异）。

#### 4.1.5 小练习与答案

1. **问**：`--dataset foo`（表中不存在的名字）会在哪一行、以什么方式失败？
   **答**：在 [L246](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L246) 执行 `name2path["foo"]` 时抛 `KeyError: 'foo'`，不会走到网络请求。
2. **问**：为什么把数据加载放在模型构造之前是合理的？
   **答**：数据下载/处理是最容易出错的环节（网络、磁盘、远端脚本变更），先做可以在构造模型前尽早失败；同时 token 流的规模也决定了后续样本数与调度器总步数（见 4.4）。
3. **问**：`dataset["train"]["text"]` 与逐条 `dataset["train"][i]["text"]` 有什么区别？
   **答**：前者按列一次性物化出完整 Python 列表（快但占内存，之后与 HF datasets 再无交互）；后者逐行惰性访问。本脚本选前者，是玩具规模下「简单优先」的取舍。

### 4.2 分词与缓存：`Qwen2Tokenizer` 与 `.bin` 文件

#### 4.2.1 概念说明

分词器在 [L248-L250](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L248-L250) 构造：`Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")`。这里只下载 **0.5B 小模型的分词器文件**（词表与合并规则），不下载模型权重。选它的原因是与模型侧对齐：`Qwen2Config` 的 `vocab_size=151936`（[L279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L279)）正是 Qwen2 系列分词器的词表大小——数据管线产出的 token id 天然落在模型词表范围内。

逐篇编码一篇 10 万文档的语料要花不少时间，所以 `MoonDataset` 把结果缓存为 `openwebtext-100k.bin`（`torch.save` 序列化的 Python 整数列表），第二次启动直接 `torch.load`。这个缓存有两个值得警惕的性质：

- **缓存键只有数据集名**：文件名是 `f"{self.dataset_name}.bin"`，**不含分词器身份、也不含版本信息**。如果你换了分词器（比如换成别的模型的 tokenizer），旧缓存仍会被直接加载——token 流是旧分词器产的，而且不会报错。这是本管线最隐蔽的坑。
- **缓存文件落在当前工作目录**：路径是相对路径，你在哪个目录执行 `python examples/toy_train.py`，`.bin` 就生成在哪里，与脚本所在目录无关。

另外注意 `max_length` **不在缓存键里也不需要**：分词产物是与窗口无关的一维 token 流，切块发生在 `__getitem__`（4.3），所以改 `max_length` 不需要重新分词，缓存直接复用。

#### 4.2.2 核心流程

```text
_tokenize_texts():
    if 当前目录存在 f"{dataset_name}.bin":
        self.tokens = torch.load(该文件)          # 跳过分词，秒级启动
    else:
        for text in tqdm(self.texts):              # 逐篇编码，带进度条
            encoded = tokenizer.encode(text, add_special_tokens=True)
            self.tokens.extend(encoded)            # 追加进同一条长流
        torch.save(self.tokens, f"{dataset_name}.bin")
```

`encode` 把一篇文档变成一个 id 列表，`extend` 把它**续接**到 `self.tokens` 末尾——多篇文档之间没有任何分隔符（Qwen2 分词器默认不在文本两端添加 BOS/EOS，`add_special_tokens=True` 在此近似空操作；可用 `tokenizer.encode("hi")` 打印验证，待本地验证）。于是 token 流里「上一篇的句号」和「下一篇的开头」直接相邻，模型必须自己学会应对这种拼接噪声——这也是大模型预处理的常见做法，只是工业界通常会用 EOS token 显式分隔文档。

#### 4.2.3 源码精读

[examples/toy_train.py:L247-L252](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L247-L252) —— 按 `--model` 选分词器。目前只支持 `qwen`，其余直接断言失败。构造出的分词器随后传入 `MoonDataset`。

```python
    if model_name == "qwen":
        tokenizer = Qwen2Tokenizer.from_pretrained(
            "Qwen/Qwen2.5-0.5B", trust_remote_code=True
        )
    else:
        assert 0, f"model {model_name} not supported"
```

[examples/toy_train.py:L23-L33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L23-L33) —— `__init__` 末尾调用 `_tokenize_texts()`，把「分词 + 缓存」整个封装在构造函数里。命中缓存走 L28，未命中走 L30-L33 的 tqdm 循环。

```python
        self.tokens = []
        self._tokenize_texts()

    def _tokenize_texts(self):
        if os.path.exists(f"{self.dataset_name}.bin"):
            self.tokens = torch.load(f"{self.dataset_name}.bin")
        else:
            for text in tqdm(self.texts, desc="Tokenizing texts"):
                encoded = self.tokenizer.encode(text, add_special_tokens=True)
                self.tokens.extend(encoded)
            torch.save(self.tokens, f"{self.dataset_name}.bin")
```

#### 4.2.4 代码实践

1. **实践目标**：体感「首次分词慢、二次启动快」，并亲手触发一次「缓存过期」。
2. **操作步骤**：
   - 删除旧缓存 `rm -f openwebtext-100k.bin`，运行 `python examples/toy_train.py`，用 `time` 记录从启动到出现第一条 `Epoch: ... Step: 0` 日志的耗时；中断后再次启动，对比耗时。
   - 中断后在 Python 里检查缓存规模（示例代码）：`import torch; t = torch.load("openwebtext-100k.bin"); print(len(t), t[:10])`。
   - 把 [L249](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L249) 的分词器临时换成另一款 Qwen2 模型（如 `"Qwen/Qwen2-0.5B"`），**不删缓存**再次启动，观察 tqdm 是否出现。
3. **需要观察的现象**：首次运行出现 `Tokenizing texts` 进度条；二次启动没有进度条、直接进入训练。换分词器后 tqdm 依旧**不出现**——说明缓存没有被正确失效。
4. **预期结果**：确认缓存把启动时间从「分钟级」降到「秒级」；同时确认缓存键的缺陷：换分词器会静默复用旧 token 流。各项具体耗时待本地验证。

#### 4.2.5 小练习与答案

1. **问**：为什么 `max_length` 改成 256 后不需要删缓存，而换分词器后应该删缓存？
   **答**：缓存里存的是分词产物（一维 token 流），与切块窗口无关，`__getitem__` 每次按新 `max_length` 现切；而分词器变了 token 流就应重算，但缓存文件名只含 `dataset_name`，不会自动失效，必须手动删。
2. **问**：`self.tokens` 用 Python 列表而不是 torch 张量存，有什么代价与好处？
   **答**：好处是 `torch.save/load` 直接可用、`extend` 追加方便；代价是每次 `__getitem__` 的列表切片都会复制一份，且数值无法用 GPU/向量化操作。玩具规模可接受，工业实现通常存成 numpy memmap 或 uint32 张量。
3. **问**：如果两台机器的训练目录都各自生成了 `openwebtext-100k.bin`，能直接拷贝复用吗？
   **答**：能。该文件只依赖「数据集内容 + 分词器」，与硬件、`max_length`、模型大小都无关，拷贝到目标工作目录即可命中 L28 的加载分支。

### 4.3 定长分块：`__len__` 与 `__getitem__`

#### 4.3.1 概念说明

有了 token 长流，`MoonDataset` 用最朴素的「无重叠滑窗」把它切成等长样本：第 `idx` 个样本取 `[idx*L, (idx+1)*L)` 这一段，\(L\) 即 `max_length`（默认 512）。于是：

\[ N_{sample} = \left\lfloor \frac{N_{token}}{L} \right\rfloor, \qquad \text{丢弃尾部 } N_{token} \bmod L \text{ 个 token} \]

两个直接推论：

- **样本数与窗口长度成反比**：同一个 token 流，`max_length` 减半则样本数翻倍、每个样本 token 数减半。这是综合实践中 512→256 对比实验的定量基础。
- **样本会跨越文档边界**：切块位置是 `idx*L`，与文档边界无关。一个样本前半段可能是 A 文档的结尾、后半段是 B 文档的开头（4.3.4 的实践会让你亲眼看到）。

`__getitem__` 返回 `dtype=torch.long` 的一维张量——这是 `nn.Embedding` 对输入 id 的类型要求（整数下标）。由于所有样本严格等长，DataLoader 组批时**永远不需要 padding**，也没有 attention mask 的用武之地（每个位置都是真实 token）。

#### 4.3.2 核心流程

```text
__len__():
    return len(self.tokens) // max_length        # 完整块的数量，尾部落单 token 被丢弃

__getitem__(idx):
    start = idx * max_length                     # 无重叠等距窗口
    end   = start + max_length
    return torch.tensor(self.tokens[start:end], dtype=torch.long)
```

#### 4.3.3 源码精读

[examples/toy_train.py:L35-L43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L35-L43) —— `Dataset` 协议的两个方法，总共 9 行，就是整条分块策略的全部。

```python
    def __len__(self):
        return len(self.tokens) // self.max_length

    def __getitem__(self, idx):
        start_idx = idx * (self.max_length)
        end_idx = start_idx + (self.max_length)
        token_slice = self.tokens[start_idx:end_idx]
        data = torch.tensor(token_slice, dtype=torch.long)
        return data
```

与它配套的一个模型侧细节在 [L265](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L265)：`Qwen2Config` 里 `max_position_embeddings=513`，恰好是 `max_length=512` 加一。这为位置编码留出了正好覆盖一个样本的窗口；反过来也意味着把 `max_length` 调大到超过 513 可能触发 RoPE 位置缓存的越界问题（待本地验证），做对比实验时建议只往小调。

#### 4.3.4 代码实践

1. **实践目标**：验证分块公式，并亲眼确认「样本跨越文档边界」。
2. **操作步骤**（示例代码，可在仓库根目录运行）：

   ```python
   import torch
   from transformers import Qwen2Tokenizer
   tok = Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B", trust_remote_code=True)
   tokens = torch.load("openwebtext-100k.bin")     # 复用 4.2 生成的缓存

   L = 512
   n_samples = len(tokens) // L
   print("总 token 数:", len(tokens), "样本数:", n_samples, "丢弃:", len(tokens) % L)

   sample = tokens[0:L]                             # 第 0 个样本
   text = tok.decode(sample)
   print(text[:400], "\n---- 中途换文档的位置 ----\n", text[-400:])
   ```

3. **需要观察的现象**：decode 出的文本中某处会发生「上文话题突然中断、跳到另一篇文档开头」的切换；丢弃的 token 数小于 `L`。
4. **预期结果**：`n_samples * 512 + (len(tokens) % 512) == len(tokens)` 成立；文本中可见文档拼接处。总 token 数与样本数待本地验证。

#### 4.3.5 小练习与答案

1. **问**：`max_length=512` 时样本数是 \(S\)。改成 256 后样本数是多少？每步（batch_size 固定 16）消耗的 token 数变成多少？
   **答**：样本数约 \(2S\)（精确为 \(\lfloor N/256\rfloor\)，约等于两倍的 \(\lfloor N/512\rfloor\)，误差不超过 1）；每步 token 数从 \(16\times512=8192\) 降到 \(16\times256=4096\)。
2. **问**：如果把 `__len__` 的 `//` 改成向上取整，会发生什么？
   **答**：最后一个样本会取到不足 `L` 个 token，`torch.tensor` 得到变长样本；默认 collate 无法堆叠变长序列，会在组批时直接报错（除非最后一个索引恰好单独成批）。这正是丢弃尾部的原因。
3. **问**：无重叠窗口 vs 滑动步长为 `L/2` 的重叠窗口，各有什么取舍？
   **答**：无重叠：每个 token 只被采样一次，epoch 语义清晰（本脚本的做法）；重叠：样本数翻倍、同一段文本被学多次，小数据集上可缓解数据不足，但一个 epoch 的「有效新信息」并没有变多，且容易高估收敛效果。

### 4.4 DataLoader 组织：从 `Dataset` 到 `[16, 512]` 的 batch

#### 4.4.1 概念说明

`DataLoader` 把 `MoonDataset` 包成迭代器：每次 yield 一个形状 `[16, 512]`、`dtype=long` 的张量（默认 collate 把 16 个 `[512]` 样本堆叠成批）。三个值得注意的参数选择：

- `batch_size=16`：硬编码，没有命令行开关。每步前向消耗 \(16\times L\) 个 token。
- `shuffle=True`：每个 epoch 打乱的是**样本（块）的顺序**，不是 token 流本身；块内部内容不变。配合 `epoch=1`，每个块恰好被消费一次。
- 未设置 `num_workers`/`pin_memory`：默认单进程同步取数。因为 `__getitem__` 只是列表切片 + 建 tensor，开销极小，单进程足够。

DataLoader 的长度直接决定学习率调度的总步数：[L341-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346) 里 `num_training_steps=len(train_loader) * epoch`（u1-l3 讲过调度曲线）。所以改 `max_length` 不仅改了每步计算量，还会改 `len(train_loader)`，进而改写整条 cosine 退火曲线——做对比实验时必须意识到这层联动。

设样本数为 \(S\)、batch size 为 \(B=16\)，DataLoader 默认 `drop_last=False`：

\[ \text{len(train\_loader)} = \left\lceil \frac{S}{B} \right\rceil \]

#### 4.4.2 核心流程

```text
get_model_and_dataloader 的数据侧收尾：
    train_dataset = MoonDataset(dataset_name, train_dataset, tokenizer)   # 未传 max_length → 默认 512
    train_loader  = DataLoader(train_dataset, batch_size=16, shuffle=True)

训练循环（u1-l3 已精读，这里只看数据视角）：
    for step, batch in enumerate(train_loader):   # batch: [16, 512] long
        batch = batch.to(device)
        model(input_ids=batch, labels=batch)      # 依靠内部 shift 做下一 token 预测
```

注意 `max_length` 没有命令行参数（argparse 只有六个参数，u1-l2 已盘点），想改窗口只能改 [L17](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L17) 的默认值或 [L253](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L253) 的调用处。

#### 4.4.3 源码精读

[examples/toy_train.py:L253-L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L253-L254) —— 用 `MoonDataset`（第三个参数之后全部用默认值，即 `max_length=512`）包装 HF 数据集，再套上 DataLoader。这两行就是 `get_model_and_dataloader` 里全部的数据组装逻辑。

```python
    train_dataset = MoonDataset(dataset_name, train_dataset, tokenizer)
    train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)
```

[examples/toy_train.py:L348-L351](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L348-L351) —— 训练循环消费端：`batch` 整体搬到设备后同时充当 `input_ids` 和 `labels`。数据管线在这里交接给模型：一份 `[16, 512]` 的整数张量，既不需要 padding 也不需要 mask。

```python
        for step, batch in enumerate(train_loader):
            batch = batch.to(device)
            input_ids = batch
            outputs = model(input_ids=input_ids, labels=input_ids)
            loss = outputs.loss
```

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认 batch 的形状与类型，并算出本机的每 epoch 步数。
2. **操作步骤**（示例代码，可直接放在脚本里 `get_model_and_dataloader` 调用之后，或独立小脚本）：

   ```python
   model, train_loader = get_model_and_dataloader("qwen", "openwebtext-100k", 1024)
   batch = next(iter(train_loader))
   print(batch.shape, batch.dtype, batch.min().item(), batch.max().item())
   print("每 epoch 步数:", len(train_loader))
   ```

3. **需要观察的现象**：形状 `torch.Size([16, 512])`，类型 `torch.int64`；id 的最小值不小于 0、最大值远小于 151936（说明确实落在 Qwen2 词表内）；`len(train_loader) == ceil(样本数/16)`。
4. **预期结果**：与 4.3.4 算出的样本数对上；max id 应小于 151936。具体数值待本地验证。

#### 4.4.5 小练习与答案

1. **问**：`shuffle=True` 洗乱的是什么？token 流里相邻两个文档还会在同一个样本里相遇吗？
   **答**：洗乱的是「块」的访问顺序。会——拼接发生在分词阶段（`extend`），块内容在切块那一刻已固定，shuffle 只决定块被消费的先后。
2. **问**：`max_length` 从 512 改到 256 后，`len(train_loader)` 大约变成几倍？cosine 调度受到什么影响？
   **答**：约 2 倍（样本数翻倍、batch_size 不变）。`num_training_steps=len(train_loader)*epoch` 也约翻倍，warmup 仍是固定 100 步但占比减半，整条退火曲线被拉长——对比实验中 loss 走向的差异同时混入了「上下文变短」和「调度变形」两个因素，下结论时要分开归因。
3. **问**：为什么这个管线里完全见不到 attention mask？
   **答**：定长分块保证同一批内所有样本都是真实的 512 个 token，没有 padding 位，也就无需 mask 来遮蔽无效位置。若改造成「按文档成样本」的方案，padding 和 mask 就会立刻变成必需品。

## 5. 综合实践

把本讲四个模块串成一个完整的 A/B 实验：**改 `max_length` 并评估影响**。

**任务一：512 vs 256 对比训练**

1. 把 [examples/toy_train.py:L17](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L17) 的默认值 `max_length=512` 改为 `max_length=256`（只改这一处；`.bin` 缓存无需删除，理由见 4.2.5 第 1 题）。
2. 分别在两种窗口下运行（建议用较小 `--hidden_size 512` 加速，注意 hidden_size 须为 16 的倍数，u1-l2）：

   ```bash
   python examples/toy_train.py --optimizer adamw --lr 1e-3 --hidden_size 512   # 记为 L512
   # 改 max_length 后
   python examples/toy_train.py --optimizer adamw --lr 1e-3 --hidden_size 512   # 记为 L256
   ```

3. 用 loguru 日志自带的时间戳（u1-l3 的日志格式）估算两种设置下每步平均耗时；再复用 u1-l3 搭的窗口平均 loss 仪表盘对比同一步数区间的 loss 走向。
4. **预期观察**：L256 每步 token 数减半（4096 vs 8192），每步墙钟时间应明显变短；`len(train_loader)` 约翻倍；由于上下文变短且调度曲线变形，同一步数的 loss 通常略高（定性结论，具体数值待本地验证）。

**任务二：实现独立的评估函数**

在 `toy_train.py` 末尾（训练循环之后）补一个函数（示例代码）：

```python
@torch.no_grad()
def evaluate_lm_loss(model, data_loader, device, max_batches=20):
    model.eval()
    losses = []
    for i, batch in enumerate(data_loader):
        if i >= max_batches:
            break
        batch = batch.to(device)
        loss = model(input_ids=batch, labels=batch).loss
        losses.append(loss.item())
    model.train()
    return sum(losses) / len(losses)
```

要点：

- `@torch.no_grad()` 关闭梯度，省显存省时间；`model.eval()` / `model.train()` 成对出现（本模型 `attention_dropout=0.0`，差异极小，但习惯要养好）。
- 因为定长分块保证每个 batch 形状相同、每个样本的有效预测位都是 \(L-1\) 个（u1-l3 的 shift），「各 batch loss 直接平均」恰好等于 token 级平均，不需要加权。
- 在训练循环**之前**和**之后**各调用一次（同一个 `train_loader` 即可；若想更严谨，可试着用 4.1.4 探明的其他切分构造验证集，待本地验证），对比 `evaluate_lm_loss` 的返回值。
- **预期结果**：训前约为 \(\ln(151936)\approx 11.9\) 附近，训后显著下降；下降幅度就是你在任务一里两个配置可比的共同标尺——这个函数在 u3-l1 的 Muon vs AdamW 对比实验中会被反复复用。

## 6. 本讲小结

- `MoonDataset` 用四道工序把 HuggingFace 文本变成训练批：`load_dataset` 加载 → `Qwen2Tokenizer` 逐篇编码并 `extend` 成一维 token 流 → 按 `max_length` 无重叠切块 → `DataLoader(batch_size=16, shuffle=True)` 组成 `[16, 512]` 的 long 张量。
- 分词结果缓存为 `openwebtext-100k.bin`（落在当前工作目录），键只含数据集名：改 `max_length` 不用重分词，但换分词器必须手动删缓存，否则静默复用旧 token 流。
- 样本数 \( \lfloor N_{token}/L \rfloor \)，尾部 token 被丢弃；切块跨越文档边界，样本内部可能出现两篇文档首尾相接。
- 定长分块的连带收益：批内无需 padding、无需 attention mask、各 batch 的 loss 可以直接平均。
- `len(train_loader)` 同时喂给了 cosine 调度的 `num_training_steps`，所以改 `max_length` 会同时改变每步计算量与整条学习率曲线，对比实验要分开归因。

## 7. 下一步学习建议

至此，入门单元四讲完成：你已经知道 Moonlight 是什么（u1-l1）、怎么跑（u1-l2）、训练循环如何运转（u1-l3）、数据从哪来到哪去（本讲）。接下来两条路：

- **主线（推荐）**：进入 u2 单元精读 Muon 优化器。建议从 [u2-l1 参数分组](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313) 开始，看懂哪些参数进 Muon、哪些留给内嵌 AdamW；数据管线这边你可以随时用本讲的 `evaluate_lm_loss` 为优化器对比提供测量标尺。
- **支线**：如果你对模型构造更感兴趣，可先跳到 u3-l2 精读 [L257-L280](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L257-L280) 的 `Qwen2Config`——理解 `vocab_size=151936` 与本讲分词器的对齐关系、`max_position_embeddings=513` 与 `max_length` 的配套关系。
- 动手型读者可以现在就给 `--max_length` 加一个 argparse 参数（放在 [L319-L326](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L319-L326)），把本讲的 A/B 实验参数化，这个改动会在 u3-l5 二次开发中派上用场。
