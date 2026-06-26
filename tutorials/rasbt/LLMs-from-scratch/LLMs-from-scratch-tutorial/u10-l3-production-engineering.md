# 工程化：高效权重加载 / 扩展 tokenizer / 训练加速

## 1. 本讲目标

前 9 个单元我们一直在「从零写、跑得通」这条线上前进：模型能生成、能训练、能微调。本讲把视角从「能不能跑」转向「在生产规模上能不能跑得好、跑得省」，聚焦三项非常实用的工程技能。学完后你应当能够：

1. **内存高效地加载模型权重**——理解为什么朴素的 `load_state_dict` 会让大模型在内存里短暂存在两份，并掌握顺序加载、`meta` 设备、`mmap` 等把峰值内存压下来的手段。
2. **为 tiktoken 扩展新的特殊 token**——理解 BPE 为什么会拆碎生词，学会把自定义 token 注册为「单个 token」，并同步改造 LLM 的嵌入层与输出层。
3. **看懂常见的 LLM 训练加速手段**——理解 `tok/sec`（每秒处理 token 数）这个吞吐指标，并认识把教学代码加速 10 倍以上的 10 项优化。

这三个主题分别对应仓库 `ch05/` 下的三个附加目录，互不依赖，但都建立在你已经熟悉第 5 章 `state_dict` 保存/加载（见 [u5-l4](./u5-l4-weight-loading.md)）的基础上。

## 2. 前置知识

本讲假设你已经掌握以下概念（对应前面讲义）：

- **state_dict 与权重存取**：`torch.save(model.state_dict(), "model.pth")` 把模型权重序列化到磁盘，`torch.load(...)` 读回，`load_state_dict(...)` 灌进模型（[u5-l4](./u5-l4-weight-loading.md)）。
- **BPE 与 tiktoken**：tiktoken 加载 GPT-2 的 50,257 词表，`encode` 把文本切成 token ID，`decode` 还原；生词会被拆成子词（[u2-l2](./u2-l2-bpe-tokenizer.md)）。
- **GPTModel 的输入输出层**：`tok_emb = nn.Embedding(vocab_size, emb_dim)` 是输入嵌入，`out_head = nn.Linear(emb_dim, vocab_size, bias=False)` 是输出头，二者维度都由 `vocab_size` 决定（[u4-l3](./u4-l3-gpt-model-assembly.md)）。
- **weight tying（权重共享）**：让 `out_head` 与 `tok_emb` 共用同一份权重矩阵（[u5-l4](./u5-l4-weight-loading.md)）。
- **训练循环四件套**：`zero_grad → 前向 → backward → step`，以及 `evaluate_model` 里的 `model.eval()` + `torch.no_grad()`（[u5-l2](./u5-l2-training-loop.md)）。

两个本讲会用到的「内存」术语先点明：

- **峰值内存（peak memory）**：程序运行过程中某一瞬间占用的最大内存。加载大模型时，峰值往往远大于「模型本身大小」，因为加载过程会临时多拷贝一份数据。
- **VRAM** 指显卡显存（GPU memory），**RAM** 指主存（CPU memory）。本讲会频繁区分二者——很多优化是用一种内存换另一种内存。

## 3. 本讲源码地图

本讲涉及 `ch05/` 下三个并列的工程实践目录，各目录都很小：

| 目录 | 关键文件 | 作用 |
|---|---|---|
| `ch05/08_memory_efficient_weight_loading/` | `memory-efficient-state-dict.ipynb` | 演示 5 种权重加载方式及其 GPU/CPU 内存代价 |
| `ch05/09_extending-tokenizers/` | `extend-tiktoken.ipynb` | 给 tiktoken 添加特殊 token，并同步改造 GPT 模型 |
| `ch05/10_llm-training-speed/` | `00_orig.py`（baseline）、`01_opt_single_gpu.py`（单卡优化版）、`02_opt_multi_gpu_ddp.py`（多卡 DDP 版） | 从教学代码逐步加速训练，附 `README.md` 的逐项性能对比 |

> 阅读提示：前两个是 notebook（逐 cell 演进），第三个是可直接 `python xxx.py` 运行的脚本。本讲引用的行号均取自当前 HEAD `ff0b3d9`。

## 4. 核心概念与源码讲解

### 4.1 内存高效权重加载

#### 4.1.1 概念说明

在 [u5-l4](./u5-l4-weight-loading.md) 里我们学过，把一个训练好的模型存盘再加载回来，标准写法是：

```python
model = GPTModel(CONFIG)
model.load_state_dict(torch.load("model.pth", weights_only=True))
```

这套写法在小模型上毫无问题。但当模型大到几个 GB（比如 GPT-2 XL 有 1.5 GB 参数）时，一个细节会咬人：**加载的瞬间，模型的权重在内存里同时存在了两份**。一份是 `GPTModel(CONFIG)` 实例化时随机初始化的权重，另一份是 `torch.load` 从磁盘读进来的 `state_dict`；等到 `load_state_dict` 把后者拷进前者，多余的 `state_dict` 才会被回收。这「两份并存」的短暂窗口，就是峰值内存翻倍的根因。

本节要回答的问题是：当 GPU 显存或 CPU 主存「刚好够放一份模型、放不下两份」时，怎么把权重加载进来？配套 notebook 把这个问题拆成了 5 种逐步演进的方案，并实测了每种方案的峰值内存。

#### 4.1.2 核心流程

五种方案的思路可以用一张表概括（数字取自 notebook 在 NVIDIA GB10 显卡 + gpt2-xl (1558M) 上的实测，见 [4.1.3](#413-源码精读)；你本地的绝对值会随硬件变化，但相对关系成立）：

| 方案 | 关键动作 | GPU 峰值 | CPU 峰值 |
|---|---|---|---|
| ① 基础加载 | 实例化模型 → `load_state_dict(torch.load(..., map_location=device))` | 12.8 GB（≈2×模型） | 4.4 GB |
| ② 顺序加载 | state_dict 先进 CPU → 逐参数 `copy_` 进 GPU | 6.7 GB | 6.3 GB |
| ③ `meta` 设备 | 模型在 `meta` 上「空壳化」→ `to_empty` → 逐参数 `copy_` | 12.8 GB | **1.3 GB** |
| ④ `mmap=True`（推荐） | `meta` 空壳 + `load_state_dict(..., mmap=True, assign=True)` | 6.4 GB | 受 RAM 限制时显著降低 |
| ⑤ 逐张量存盘/加载 | 每个参数单独存一个 `.pt`，逐个加载即用即抛 | 6.4 GB | **0.3 GB** |

核心权衡是 **GPU 显存 ↔ CPU 主存 之间的交换**：

- 想省 **GPU 显存**：让 state_dict 先待在 CPU（方案②），GPU 峰值从 \(2M\) 降到 \(M+\epsilon\)。
- 想省 **CPU 主存**：用 `meta` 设备跳过实例化分配（方案③），或用 `mmap` 让数据按需从磁盘读（方案④），或干脆每次只加载一个参数张量（方案⑤）。

下面解释两个关键工具：

**`meta` 设备**。PyTorch 的 `"meta"` 是一种「只记录形状、不真正分配内存」的特殊设备。在 `with torch.device("meta"):` 上下文里创建模型，得到的是一堆只有形状没有数据的「空壳」张量；随后用 `model.to_empty(device=...)` 再把真实显存一次性分配出来。这样就跳过了「实例化时先填一份随机权重」这一步。

**`mmap=True`**。开启内存映射文件 I/O：`torch.load` 不再把整个文件一次性读进 RAM，而是让张量「按需」从磁盘对应位置读取。当 CPU 主存有限时，它能把加载过程的 RAM 占用降到很低。

#### 4.1.3 源码精读

**(a) 基线：模型本身占多少显存**

notebook 先用一个工具函数族测量「干净」状态下的显存。`start_memory_tracking` 复位峰值统计，`print_memory_usage` 读出峰值显存：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L116-L125](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L116-L125) —— 定义 GPU 显存跟踪工具。

把 gpt2-xl 实例化并搬到 GPU，峰值显存是 6.4 GB——这就是「一份模型」的基准。接着模拟「训练完存盘」：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L289](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L289) —— `torch.save(model.state_dict(), "model.pth")` 把权重存盘（这就是 u5-l4 学过的写法）。

**(b) 方案①：基础加载，显存翻倍的元凶**

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L372-L373](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L372-L373) —— 先 `model.to(device)` 放了一份，再 `load_state_dict(torch.load(..., map_location=device))` 又读进来一份 state_dict，两者短暂并存，峰值显存 12.8 GB。

把 `map_location` 从 `device` 改成 `"cpu"`（让 state_dict 先进主存）并不能省显存，因为最终还是要把权重搬上 GPU：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L455-L456](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L455-L456) —— `map_location="cpu"` 写法，峰值显存仍是 6.4 GB，与方案①相当（只是把压力挪到 CPU 而已）。

**(c) 方案②：顺序加载，省显存的关键**

把 state_dict 读进 CPU，然后**逐个参数**用 `copy_` 拷进 GPU——任何时刻 GPU 上只有「模型 + 当前这一个参数张量」：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L548-L556](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L548-L556) —— `state_dict` 加载到 CPU；循环里 `param.copy_(state_dict[name].to(device))` 把每个张量搬到 GPU 再就地写入。GPU 峰值从 12.8 降到 6.7 GB。

代价是 CPU 主存要放下一整份 state_dict（6.3 GB）。测量 CPU 主存用的是一个起后台线程采样的工具：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L641](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L641) —— `def memory_usage_in_gb(func, ...)`：在独立线程里每隔 0.1 秒采样进程 RSS（常驻内存），取峰值减基线，得到函数运行期间的 CPU 峰值内存。

**(d) 方案③：`meta` 设备，省 CPU 主存**

当机器「显存大、主存小」时，方案②那 6.3 GB 的 CPU 占用就成了瓶颈。`meta` 设备派上用场：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L762-L767](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L762-L767) —— `with torch.device("meta"): model = GPTModel(...)` 造空壳；`model.to_empty(device=device)` 分配真实显存；`torch.load(..., map_location=device)` 直接读到 GPU。CPU 峰值从 6.3 降到 1.3 GB。

注意这里的 `copy_` 不再带 `.to(device)`，因为 state_dict 已经直接加载到 GPU 上了：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L775](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L775) —— `param.copy_(state_dict[name])`（state_dict 已在 GPU，无需再搬运）。

**(e) 方案④：`mmap=True`，notebook 推荐写法**

把 `meta` 空壳和 `load_state_dict` 的两个新参数 `mmap=True` / `assign=True` 组合起来，是最简洁的推荐方案：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L887-L892](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L887-L892) —— `meta` 空壳 + `model.load_state_dict(torch.load(..., mmap=True), assign=True)`。`assign=True` 让 `load_state_dict` 直接把加载的张量「指派」给参数（替换引用），而不是拷贝，配合 `mmap` 可按需读盘，在主存受限的机器上显著省 RAM。

**(f) 方案⑤：逐张量，CPU 占用最低的暴力法**

如果连「一整份 state_dict 在 CPU」都吃不消，可以事先把每个参数单独存成一个 `.pt` 文件，加载时一次只读一个、用完即 `del`：

[ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb:L980-L993](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/08_memory_efficient_weight_loading/memory-efficient-state-dict.ipynb#L980-L993) —— 循环里 `torch.load(weight_path, map_location="cpu")` 只读一个张量，`param.copy_(param_data)` 后立即 `del param_data`。CPU 峰值低至 0.3 GB，代价是模型存盘时要预拆成成百上千个小文件。

> 一句话总结：**省 GPU 显存 → state_dict 先进 CPU（方案②）；省 CPU 主存 → `meta` 空壳 / `mmap` / 逐张量（方案③④⑤）**。

#### 4.1.4 代码实践

**实践目标**：亲手制造「峰值显存翻倍」现象，再用顺序加载把它压下来，并测量峰值内存。

**操作步骤**（建议在 notebook 里逐步执行，模型可先用 `gpt2-small (124M)` 以降低门槛）：

1. 复制 notebook 的工具函数 `start_memory_tracking` / `print_memory_usage` / `cleanup`。
2. 实例化一个小 GPT，存盘：`torch.save(model.state_dict(), "model.pth")`，再 `del model; cleanup()`。
3. 跑方案①（基础加载），用 `print_memory_usage()` 记录峰值显存。
4. `cleanup()` 后，跑方案②（顺序加载），再记录峰值显存。
5. 若想测 CPU 主存，把加载逻辑包进 `memory_usage_in_gb(...)`。

**需要观察的现象**：方案①的 GPU 峰值应接近「2 × 模型大小」，方案②应接近「1 × 模型大小 + 一个张量」。

**预期结果**：在 notebook 同款硬件上，方案①≈12.8 GB、方案②≈6.7 GB。你本地的绝对值取决于模型大小和显卡，但「方案① ≈ 2× 方案②」的关系应当成立。**待本地验证**（无 CUDA 环境时，可改用 `torch.cuda.max_memory_allocated` 的 CPU 等价物 `tracemalloc`，或仅做源码阅读理解峰值翻倍的成因）。

#### 4.1.5 小练习与答案

**练习 1**：为什么方案①把 `map_location` 从 `device` 改成 `"cpu"` 后，GPU 峰值显存几乎不变？

> **答案**：`map_location="cpu"` 只是让 state_dict 先落在主存，但随后仍要 `.to(device)` 把权重搬上 GPU，搬运的瞬间 GPU 上仍是「模型 + 待写入权重」两份数据，所以峰值显存不变；它真正改变的是「CPU 上是否多一份 state_dict」。

**练习 2**：方案⑤（逐张量加载）的 CPU 峰值最低（0.3 GB），代价是什么？

> **答案**：代价是存盘阶段要把 state_dict 预先拆成几百上千个 `.pt` 文件、磁盘 I/O 次数多、加载逻辑也更繁琐；它牺牲了简洁性和加载速度，换取极端受限主存下的可加载性。

---

### 4.2 扩展 tokenizer 新 token

#### 4.2.1 概念说明

在 [u2-l2](./u2-l2-bpe-tokenizer.md) 我们学过：BPE 遇到训练时没见过的词，会把它拆成一串子词。比如对 `MyNewToken_1` 这个生词，GPT-2 的 tokenizer 会拆成 `My`、`New`、`Token`、`_`、`1` 共 5 个子词 token——这是 BPE 处理生词的**正常且必要**行为（保证了任何文本都能被编码）。

但有些场景我们希望某个符号被当作**一个不可分割的整体**来编码，就像 `<|endoftext|>` 那样占一个 token ID。典型例子：聊天模型里的 `<|im_start|>`、工具调用标记 `<tool_call>`、或者你自己业务里的特殊占位符。把它们编成单个 token，既省序列长度，又能让模型学会「看到这个标记就该做特定的事」。

本节要解决两个问题：

1. **怎么把一个自定义字符串注册成 tiktoken 里的单个 token？**
2. **注册之后，已有的 GPT 模型还能直接用吗？**（剧透：不能，必须同步改造输入嵌入层和输出层。）

一个关键约束：**新 token 只能作为「特殊 token（special token）」添加，不能塞进 BPE 的合并规则里**。原因是 BPE 的合并规则（`bpe_merges`）是在分词器训练阶段学出来的，事后插入一条新规则极易破坏现有编码方案。而特殊 token 走的是另一条「精确字符串匹配」的通路，与 BPE 合并互不干扰。

#### 4.2.2 核心流程

完整流程分两大步：**扩展分词器**，再**同步改造模型**。

**第一步：扩展分词器**

```
1. 选定要添加的 token 列表，例如 ["MyNewToken_1", "MyNewToken_2"]
2. 给每个 token 分配一个不冲突的 ID：从 base_tokenizer.n_vocab（GPT-2 为 50257）开始递增
3. 新建一个 tiktoken.Encoding：
     - 复用原 tokenizer 的正则 pat_str 与合并表 mergeable_ranks
     - 把「原特殊 token 字典 + 新 token 字典」合并后传入 special_tokens
4. encode 时用 allowed_special 显式放行新 token，否则它们会被当普通文本拆碎
```

**第二步：同步改造模型**

新 token 的 ID 超出了原来的词表范围（50257、50258），而 `tok_emb` 与 `out_head` 都是按 `vocab_size=50257` 建的，直接喂新 ID 会触发 `IndexError: index out of range in self`（嵌入查表越界）。因此要给这两层「扩容」：

```
对于 tok_emb（Embedding）：
  1. 新建一个更大的 Embedding(vocab_size + N, emb_dim)
  2. 把旧权重的前 vocab_size 行拷过去
  3. 用新层替换旧层（新增的 N 行随机初始化，待训练）

对于 out_head（Linear）：
  1. 新建一个更大的 Linear(emb_dim, vocab_size + N)
  2. 拷贝旧权重（若有 bias 一并拷）
  3. 替换

若模型用了 weight tying：
  只需 gpt.out_head.weight = gpt.tok_emb.weight，一步到位
```

扩容后，新增 token 对应的行/列是随机初始化的，**必须在含新 token 的数据上微调**才能让模型真正学会它们的含义。

#### 4.2.3 源码精读

**(a) 问题演示：生词被拆碎**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L61](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L61) —— 用原始 `base_tokenizer.encode(..., allowed_special={"<|endoftext|>"})` 编码，`MyNewToken_1` 被拆成 5 个子词 ID。注意 `<|endoftext|>` 因为在 `allowed_special` 里，被正确编成单个 ID `50256`。

**(b) 第一步：定义新 token 的 ID**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L141-L143](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L141-L143) —— `custom_token_ids = {token: base_tokenizer.n_vocab + i for i, token in enumerate(custom_tokens)}`，从原词表大小 `n_vocab`（50257）开始编号，保证不与任何已有 ID 冲突。

**(c) 第一步：新建扩展 Encoding**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L162-L167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L162-L167) —— 复用原 `pat_str` 与 `mergeable_ranks`，把 `**base_tokenizer._special_tokens` 与 `**custom_token_ids` 合并成新的 `special_tokens`，得到 `extended_tokenizer`。这一步只动了「特殊 token 通道」，BPE 合并表原封不动，因此原有文本的编码完全不变。

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L205](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L205) —— encode 时必须 `allowed_special=special_tokens_set` 把新 token 显式放行（与 u2-l2 里放行 `<|endoftext|>` 同理），否则 tiktoken 出于安全考虑会把它们当普通文本拆碎。

**(d) 没改造模型前：直接用新 ID 会崩**

notebook 演示把扩展后的 token ID 喂给**未改造**的 GPT，报 `IndexError: index out of range in self`——因为 `tok_emb` 只有 50257 行，50257/50258 越界。这就是必须改造模型的根因。

**(e) 第二步：扩容 tok_emb**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L542](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L542) —— 新建 `new_embedding = torch.nn.Embedding(new_num_tokens, emb_size)`，把旧权重的前 `num_tokens` 行拷过去，新增的 2 行随机初始化。

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L548](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L548) —— `gpt.tok_emb = new_embedding`，整层替换（注意是替换层对象，不是改 `weight`）。

**(f) 第二步：扩容 out_head**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L628](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L628) —— 新建 `new_linear = torch.nn.Linear(original_in_features, new_out_features)`，拷贝旧权重（含 bias）。

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L637](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L637) —— `gpt.out_head = new_linear`。注意这里新建的 Linear 默认 `bias=True`（原 `out_head` 是 `bias=False`），notebook 通过 `if gpt.out_head.bias is not None` 做了兼容处理。

**(g) weight tying 的简化**

[ch05/09_extending-tokenizers/extend-tiktoken.ipynb:L737](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L737) —— 若模型用了权重共享（如 Llama 3），扩容完 `tok_emb` 后，直接 `gpt.out_head.weight = gpt.tok_emb.weight` 让输出头共享同一份矩阵，无需单独扩 `out_head`。

#### 4.2.4 代码实践

**实践目标**：给 tiktoken 添加一个自定义特殊 token，验证它被编码成单个 ID；并体会模型为何必须扩容。

**操作步骤**：

1. 加载基础分词器：`base = tiktoken.get_encoding("gpt2")`。
2. 定义一个新 token，例如 `"<|my_sep|>"`，分配 ID `base.n_vocab`（即 50257）。
3. 按 [L162-L167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/09_extending-tokenizers/extend-tiktoken.ipynb#L162-L167) 的写法构造 `extended`。
4. 用 `extended.encode("a<|my_sep|>b", allowed_special={"<|my_sep|>"})` 编码，再对每个 ID 单独 `decode` 验证。

**需要观察的现象**：`<|my_sep|>` 应被编码成单个 ID `50257`，且能被 `decode([50257])` 还原回原字符串；而未经扩展的 `base` 会把它拆成多个子词。

**预期结果**：扩展后该 token 占 1 个 ID，原始文本（不含新 token 的部分）编码结果与 `base` 完全一致（因为合并表未动）。**待本地验证**（无需 GPU，纯 CPU 即可运行 tiktoken 部分）。

进阶（可选）：仿照 notebook 第 2 节，加载一个 124M GPT，把 `tok_emb` 和 `out_head` 扩容，确认扩容后 `gpt(torch.tensor([[50257]]))` 不再报 `IndexError`。

#### 4.2.5 小练习与答案

**练习 1**：为什么新 token 必须注册成 special token，而不能写进 `mergeable_ranks` / `bpe_merges`？

> **答案**：BPE 的合并规则是一套「在训练时按频率学出来的、全局有序的合并步骤」，事后插入一条会改变其他 token 的切分路径、破坏已有编码方案。特殊 token 走独立的精确字符串匹配通道，不参与 BPE 合并，因此能安全添加而不影响现有文本的编码。

**练习 2**：扩容 `tok_emb` 后，新增的那几行是随机值。如果不微调直接用，会怎样？

> **答案**：模型能正常运行（不再越界），但新 token 对应的嵌入是随机的、输出头那几列也是随机的，模型完全「不认识」这些 token 的语义。必须用含新 token 的语料微调（至少训练嵌入层和输出头），模型才能学会它们的含义。

---

### 4.3 训练加速优化

#### 4.3.1 概念说明

前面章节的训练代码是「教学代码」：写得最清楚、在任何机器（含纯 CPU）上都能跑，但完全没考虑速度。本节要回答：**同样的 GPT 模型与训练循环，在不改数学的前提下，能快多少、怎么快？**

衡量训练吞吐的标准指标是 **`tok/sec`（tokens per second，每秒处理的 token 数）**——每步前向 + 反向处理的 token 数除以该步耗时，越高越好。它与「损失是否下降」无关，纯粹衡量速度。

`ch05/10_llm-training-speed/` 给出了三个版本：

- `00_orig.py`：baseline，几乎就是第 5 章代码（只增大了上下文长度、batch size 与训练数据量，好让速度差异更明显）。
- `01_opt_single_gpu.py`：单卡优化版，叠加了 10 项优化。
- `02_opt_multi_gpu_ddp.py`：多卡 DDP 版（DDP 基础见 [u8-l3](./u8-l3-distributed-training-ddp.md)）。

据 `README.md` 实测（单张 A100），优化把吞吐从 **12,525 tok/sec 提升到 142,156 tok/sec**（约 11×）；4 张 A100 进一步到 **419,259 tok/sec**。下面逐项看这些优化。

#### 4.3.2 核心流程

10 项优化可粗分四类，下表摘自 `README.md` 的 A100 实测（「Before/After」为该项叠加后的 tok/sec 与显存）：

| # | 优化项 | 类别 | 作用 |
|---|---|---|---|
| 1 | 因果掩码即时生成 | 省显存 | 不再 `register_buffer` 整张掩码，改用 SDPA 的 `is_causal=True`，长上下文更省 |
| 2 | 启用 tensor cores | 加速 | 让矩阵乘对齐到 Ampere+ 显卡的 tensor core |
| 3 | Fused AdamW（`fused=True`） | 加速 | 优化器用融合内核，单卡上小幅提速 |
| 4 | `pin_memory=True` | 加速 | DataLoader 预锁定主机内存，加速主机→GPU 拷贝 |
| 5 | bfloat16 精度 | 加速 + 省显存 | 从 float32 换 16 位脑浮点，显存减半、速度大增 |
| 6 | 用原生 `nn.LayerNorm` / `nn.GELU` | 加速 + 省显存 | 用 PyTorch 高度优化的内置实现替换从零实现 |
| 7 | FlashAttention（SDPA） | 加速 + 省显存 | 用融合注意力替代手写 `Q@K^T→softmax→@V`，大幅省显存提速 |
| 8 | `torch.compile(model)` | 加速 | JIT 编译计算图，融合算子（首次迭代会慢） |
| 9 | 词表对齐到 64 的倍数 | 加速 | vocab 50257→50304，让嵌入/输出层维度利于 tensor core |
| 10 | 增大 batch size | 加速 | 把 batch 推到显存允许的最大 2 的幂，提高并行度 |

> 关键认识：**这些优化都不改变模型的数学**（输出与 baseline 在数值精度内一致），只改变「同样的计算用多快的内核、占多少显存来完成」。

其中影响最大的三项是 **bfloat16（#5）、FlashAttention（#7）、torch.compile（#8）**——它们分别对应「更窄的数据类型」「更聪明的注意力内核」「编译期算子融合」，是现代 LLM 训练的标配。

#### 4.3.3 源码精读

**(a) baseline 的「慢点」长什么样**

`00_orig.py` 是第 2~5 章代码的自包含汇总，三处典型慢点：

[ch05/10_llm-training-speed/00_orig.py:L76](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L76) —— 注意力层 `register_buffer("mask", torch.triu(torch.ones(context_length, context_length), diagonal=1))` 预存了整张 `context_length × context_length` 的因果掩码，长上下文下显存开销大（对应优化项 1）。

[ch05/10_llm-training-speed/00_orig.py:L121-L132](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L121-L132) —— 手写的 `LayerNorm`（用 `mean/var/sqrt` 手算）；下方 [L135-L143](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L135-L143) 是手写的 `GELU`（tanh 近似）。二者数学正确，但比 PyTorch 内置的 CUDA 内核慢（对应优化项 6）。

注意力前向是手写的三步法 `attn_scores = queries @ keys.transpose(2,3)` → `masked_fill_` → `softmax` → `@ values`（见 [L97-L109](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L97-L109)），会物化整张 \(N \times N\) 注意力矩阵（对应优化项 7，详见 [u9-l2](./u9-l2-efficient-multihead-attention.md)）。

baseline 的训练循环带计时（用来算 `tok/sec`）：

[ch05/10_llm-training-speed/00_orig.py:L305](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L305) —— `train_model_simple_with_timing` 在第 5 章 `train_model_simple` 基础上加了 CUDA Event 计时与 `tok/sec`、`memory_allocated/reserved` 上报，是衡量速度的标尺。

baseline 配置：`vocab_size=50257`、`context_length=1024`、`batch_size=8`、普通 `AdamW`：

[ch05/10_llm-training-speed/00_orig.py:L498-L513](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/00_orig.py#L498-L513) —— baseline 的 `GPT_CONFIG_124M` 与 `OTHER_SETTINGS`（batch_size=8、普通 AdamW）。

**(b) 优化版改了哪些行**

`01_opt_single_gpu.py` 在同骨架上做了针对性替换：

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L95-L96](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L95-L96) —— 注意力改用 `nn.functional.scaled_dot_product_attention(..., is_causal=True)`（SDPA，内部走 FlashAttention），同时干掉了手写掩码与物化的 \(N\times N\) 矩阵（优化项 1+7）。

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L116](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L116) 与 [L134-L135](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L134-L135) —— 用原生 `nn.GELU(approximate="tanh")` 与 `nn.LayerNorm` 替换从零实现（优化项 6）。

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L55](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L55) —— DataLoader 加 `pin_memory=True`（优化项 4）。

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L414-L418](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L414-L418) —— 三连击：`model = torch.compile(model)`（#8）、`.to(torch.bfloat16)`（#5）、`AdamW(..., fused=True)`（#3）。

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L474](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L474) —— `vocab_size: 50304`（最近的 64 的倍数，优化项 9）。

[ch05/10_llm-training-speed/01_opt_single_gpu.py:L486](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/10_llm-training-speed/01_opt_single_gpu.py#L486) —— `batch_size: 32`（优化项 10）。

多卡版 `02_opt_multi_gpu_ddp.py` 在单卡优化基础上再叠 DDP，需用 `torchrun --nproc_per_node=4` 启动（DDP 机制见 [u8-l3](./u8-l3-distributed-training-ddp.md)）。

#### 4.3.4 代码实践

**实践目标**：理解每项优化改了哪行代码；有 GPU 的读者可实测 `tok/sec` 提升。

**操作步骤**：

1. 用 VS Code 的「Compare Files」或命令行 `diff 00_orig.py 01_opt_single_gpu.py`，逐一对照本节列出的行号，确认每项优化对应的代码改动。
2. （有 CUDA GPU 时）`python 00_orig.py` 跑 baseline，记录第 1 个 epoch 结束打印的 `Avg tok/sec` 与 `Reserved memory`。
3. `python 01_opt_single_gpu.py` 跑优化版，记录同样指标。
4. （多卡时）`torchrun --nproc_per_node=4 02_opt_multi_gpu_ddp.py`。

**需要观察的现象**：优化版 `tok/sec` 显著升高、显存显著降低；`torch.compile` 的最初几个 step 会偏慢（编译开销），之后加速。

**预期结果**：README 在 A100 上 baseline≈12,525 tok/sec、优化版单卡≈142,156 tok/sec、4 卡≈419,259 tok/sec。你本地的绝对值取决于显卡型号与 CUDA 版本。**待本地验证**（无 GPU 时，本实践退化为「源码对比阅读型实践」——重点确认每项优化对应的行号改动，并理解其为何不改变数学结果）。

#### 4.3.5 小练习与答案

**练习 1**：优化项 9（vocab 50257→50304）为什么能加速？它改变了模型的数学吗？

> **答案**：tensor core 对矩阵维度的对齐有要求（常见为 8/16/64 的倍数），50257 不是 64 的倍数会让矩阵乘无法占满 tensor core；补到 50,304 后嵌入层与输出层的维度更友好，矩阵乘更快。新增的 47 个 token 位置对应的参数在训练中会被学到，输出层多出的列不影响原有 50257 个 token 的预测分布，本质上不改变模型能力，属于工程对齐而非数学改变。

**练习 2**：为什么 `torch.compile` 的前几个 step 反而更慢？

> **答案**：`torch.compile` 在首次前向时要用 Inductor 把计算图 JIT 编译成优化内核（还要触发 Triton 等代码生成），这是一次性开销；编译产物会被缓存，之后的 step 跑的都是已编译的快速内核。所以测速时要跳过最初的 warm-up 步骤。

---

## 5. 综合实践

把三项工程技能串成一个端到端小任务：**给一个已有 GPT 模型「打补丁」并高效加载它**。

1. **扩展 tokenizer**：仿照 4.2，给 tiktoken 添加两个自定义特殊 token（如 `<|user|>`、`<|assistant|>`），用 `allowed_special` 验证它们被编成单个 ID。
2. **改造模型**：新建一个 124M `GPTModel`，按 4.2 的方法扩容 `tok_emb` 与 `out_head`（或用 weight tying 一步搞定），确认喂入新 token ID 不再报 `IndexError`。
3. **存盘**：`torch.save(model.state_dict(), "model.pth")`。
4. **高效加载**：用 4.1 的方案④（`meta` 空壳 + `load_state_dict(..., mmap=True, assign=True)`）重新加载这份权重，并用 `start_memory_tracking` / `memory_usage_in_gb` 对比它与基础加载（方案①）的峰值内存差。
5. **（进阶）加速训练**：把改造后的模型放进 4.3 的优化训练脚本（`01_opt_single_gpu.py` 的骨架 + bfloat16 + SDPA），在含新 token 的小语料上微调若干步，观察 `tok/sec`。

> 这个任务同时检验：tokenizer 与模型词表必须同步（4.2）、大权重需省内存加载（4.1）、训练要跑得快（4.3）。无 GPU 时可只做 1~4 步，第 5 步退化为源码阅读。

## 6. 本讲小结

- **内存高效加载**的根因是「加载瞬间权重在内存里有两份」；省 GPU 显存让 state_dict 先进 CPU 再逐参数 `copy_`，省 CPU 主存用 `meta` 空壳 / `mmap=True` / 逐张量；推荐简洁写法是 `meta` + `load_state_dict(..., mmap=True, assign=True)`。
- **扩展 tokenizer** 时新 token 必须注册成 special token（不能动 BPE 合并表）；token ID 从 `n_vocab` 起编号，新建 `tiktoken.Encoding` 复用原 `pat_str`/`mergeable_ranks` 并合并 `special_tokens`，encode 时用 `allowed_special` 放行。
- 新 token 的 ID 会**越界**原模型，必须同步扩容 `tok_emb`（Embedding）和 `out_head`（Linear），weight tying 模型只需共享 `tok_emb.weight`；新增参数需微调才有效。
- 训练吞吐用 **`tok/sec`** 衡量；`ch05/10_llm-training-speed` 用 10 项优化把单卡 A100 从 ~1.25 万提到 ~14.2 万 tok/sec，其中 **bfloat16、FlashAttention（SDPA）、torch.compile** 收益最大，且都不改变模型数学。
- 这三项都是「不改数学、只改工程实现」的典型——理解了它们，你就从「能跑通教学代码」迈向「能部署与加速真实模型」。

## 7. 下一步学习建议

- 想深入「省显存训练」：阅读 `02_opt_multi_gpu_ddp.py` 并结合 [u8-l3](./u8-l3-distributed-training-ddp.md) 系统学 DDP；进一步可了解 FSDP、梯度检查点（gradient checkpointing）、混合精度（`torch.amp`）。
- 想深入注意力加速：[u9-l2](./u9-l2-efficient-multihead-attention.md) 对比了 9 种多头注意力实现，详解 FlashAttention 与 SDPA 的内存/速度差异。
- 想把 tokenizer 工程化：阅读 `ch05/09_extending-tokenizers/` 后，可进一步了解 HuggingFace `tokenizers` 库的 `add_tokens` API，以及 chat template（如 `<|im_start|>` 这类标记如何驱动多轮对话）。
- 想系统看现代 LLM 的工程取舍：结合 [u10-l1](./u10-l1-gpt-to-llama.md)（RoPE/RMSNorm）与 [u10-l2](./u10-l2-modern-llm-architectures.md)（Qwen3/Gemma3），理解 bfloat16、weight tying、GQA 等如何成为现代模型的「标配零件箱」。
