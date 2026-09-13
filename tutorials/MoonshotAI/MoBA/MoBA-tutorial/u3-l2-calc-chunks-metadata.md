# u3-l2 calc_chunks：chunk 元数据构造与最后一块的过滤

## 1. 本讲目标

上一讲（u3-l1）我们建立了 `moba_attn_varlen` 的四步心智模型：**算 chunk 元数据 → gate 选块 → varlen 重组 → LSE 合并**。本讲把第一步彻底拆开。读完本讲，你应该能够：

1. 独立推导 `calc_chunks` 返回的四个张量（`cu_chunk`、`filtered_chunk_indices`、`num_filtered_chunk`、`chunk_to_batch`）在任意 `cu_seqlens` / `moba_chunk_size` 输入下的取值；
2. 说清楚**为什么每个 batch 的最后一块要被剔除出 MoBA 候选集**，并且能证明这一步不损失任何注意力连接；
3. 理解 `@lru_cache` 用张量作 cache key 时的命中条件（对象身份而非值相等）与潜在风险。

本讲只涉及**索引计算**，不含任何注意力运算——但它是后面三步（u3-l3 ~ u3-l5）全部索引操作的坐标系。

## 2. 前置知识

### 2.1 前缀和与 cu_seqlens（回顾 u1-l3）

varlen 打包格式中，`cu_seqlens` 是长度为 \(B+1\)（\(B\) 为 batch 数）、单调递增、首元素为 0 的 int32 张量：第 \(i\) 条序列占据全局 token 序列的区间 \([\,cu[i],\ cu[i+1]\,)\)。本讲全程使用一个贯穿示例：

> **贯穿示例**：`cu_seqlens = [0, 10, 30, 45]`，`moba_chunk_size = 16`（记作 \(C\)）。
> 即 3 条序列，长度分别为 \(L = [10, 20, 15]\)，共 45 个 token 打包成一条长序列。

### 2.2 向上取整除法

把长度为 \(L\) 的序列按块长 \(C\) 切分，块数为 \(\lceil L/C \rceil\)。PyTorch/Python 中用「加除数减一再整除」实现：

\[
\text{batch\_num\_chunk}_i = \left\lceil \frac{L_i}{C} \right\rceil = (L_i + C - 1) \,//\, C
\]

每条序列的**最后一块**大小为：

\[
r_i = L_i - (\text{batch\_num\_chunk}_i - 1)\cdot C \quad\Rightarrow\quad 1 \le r_i \le C
\]

（本讲假设每条序列至少 1 个 token，与测试的构造方式一致。）

### 2.3 本讲用到的 PyTorch 索引操作

| 操作 | 语义 |
|---|---|
| `t[idx] = value`（`idx` 为张量） | 批量散射赋值：把 `idx` 列出的所有位置写成 `value` |
| `t.cumsum(dim=0)` | 前缀和，常用来构造「区间边界」 |
| `bool_mask.nonzero(as_tuple=True)[0]` | 提取所有 `True` 位置的下标（int64） |
| `t[idx_tensor]`（高级索引） | 按下标张量 gather |

### 2.4 functools.lru_cache

`lru_cache` 用一个字典缓存「参数 → 返回值」，参数作为字典键需要**可哈希**。命中条件是「哈希相同且相等」。Python 的 int 按值哈希；而 PyTorch 张量按**对象身份**哈希——这是 4.5 节的主题。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | 本讲主角：`calc_chunks` 函数（L14-L64），以及主流程 `moba_attn_varlen` 中消费这些元数据的位置 |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | `generate_data` 构造随机 varlen 边界（L26-L29），参数化网格（L37-L42）是本讲实践的数据来源 |
| [moba/moba_naive.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py) | 对照阅读：naive 实现用纯掩码表达同样的分块与因果语义（L45-L50、L58-L61） |
| [moba/wrapper.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py) | transformers 集成路径中 `cu_seqlens` 的构造处（L62-L66），是讨论 `lru_cache` 命中率的关键事实 |

## 4. 核心概念与源码讲解

### 4.1 calc_chunks：输入输出与在主流程中的位置

#### 4.1.1 概念说明

「chunk 元数据」是指一组**纯索引张量**（不含任何模型数据）：它们回答四个问题——

1. 这个 varlen 批次被切成了哪些块？每块从哪个全局 token 偏移开始？（`cu_chunk`）
2. 哪些块参与 MoBA 注意力？（`filtered_chunk_indices`）
3. 参与的块有多少个？（`num_filtered_chunk`）
4. 每个块属于哪条序列？（`chunk_to_batch`）

有了这套坐标系，后面 gate 打分（u3-l3）、varlen 重组（u3-l4）、LSE 合并（u3-l5）里的每一次 gather/scatter 都能落到具体行号。

一个必须先建立的事实：**块不跨 batch 边界**。每条序列从自己的起点独立切块，前一条序列的最后一块不满时，下一条序列仍从自己的精确起点开新块。因此「全局第 \(j\) 块的起始偏移」不能用 \(j \cdot C\) 这种等差公式算，必须逐块累计——这正是 `cu_chunk` 存在的意义。

#### 4.1.2 核心流程

```text
输入: cu_seqlen [B+1], moba_chunk_size C
────────────────────────────────────────────
① batch_sizes[i]      = cu[i+1] - cu[i]                 每条序列长度
② batch_num_chunk[i]  = ceil(batch_sizes[i] / C)        每条序列块数
③ cu_num_chunk[1:]    = cumsum(batch_num_chunk)         每条序列的首块编号（下标0是哑元）
④ num_chunk           = 所有序列块数之和
⑤ chunk_sizes[1+j]    = 第 j 块大小（默认 C，各序列最后一块改为 r_i）
⑥ cu_chunk            = cumsum(chunk_sizes)             每块的全局起始偏移
⑦ chunk_to_batch      = 每块所属 batch 编号
⑧ 剔除每条序列的最后一块 → filtered_chunk_indices
────────────────────────────────────────────
输出: (cu_chunk, filtered_chunk_indices, num_filtered_chunk, chunk_to_batch)
```

#### 4.1.3 源码精读

函数签名与装饰器（装饰器在 4.5 专门讲）：

- [moba/moba_efficient.py:L14-L16](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L14-L16)：`calc_chunks(cu_seqlen, moba_chunk_size)` 被挂上 `@lru_cache(maxsize=16)`，返回四个元数据。

主流程中的调用点与两个直接消费：

- [moba/moba_efficient.py:L305-L311](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L305-L311)：`moba_attn_varlen` 第一步就解包这四个返回值。
- [moba/moba_efficient.py:L313-L315](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L315)：`moba_topk = min(moba_topk - 1, num_filtered_chunk)`。两件事：① 顶层传入的 topk 要**减一**，因为「当前块」已结构性分离给 self-attn 支路（见 4.4.1）；② 用 `num_filtered_chunk` 封顶——候选块不够时能选多少选多少。
- [moba/moba_efficient.py:L317-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L317-L321)：`need_moba_attn = moba_topk > 0` 为假时（例如 `topk=1`，或**所有**序列都短于两块导致 `num_filtered_chunk=0`），整个调用退化为普通因果 `flash_attn_varlen_func`。注意这是对整个 varlen 批次的**全局**判定。
- [moba/moba_efficient.py:L323](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L323)：`self_attn_cu_seqlen = cu_chunk`——`cu_chunk` 直接充当 self-attn 支路的 varlen 边界（覆盖**全部**块，不只最后一块；u3-l5 精读）。

四个返回值在 `moba_attn_varlen` 中的全部消费点（本讲后续小节逐个展开）：

| 返回值 | 消费位置 | 用途 |
|---|---|---|
| `cu_chunk` | L323、L329、L355 | self-attn varlen 边界；候选块 gather 起点；候选块结束偏移 |
| `filtered_chunk_indices` | L329、L355、L356 | 选出候选块、取块结束偏移、查所属 batch |
| `num_filtered_chunk` | L314、L328、L418 | topk 封顶；候选块数组的行数；moba_kv 的 cu_seqlen 上界 |
| `chunk_to_batch` | L356 | 反查候选块所属 batch 的结束位置 |

#### 4.1.4 代码实践（源码阅读型）

**目标**：不运行代码，仅凭阅读，把上表的每个消费点在源码中找出来并抄下对应代码行。

**步骤**：
1. 打开 [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py)，搜索 `cu_chunk`、`filtered_chunk_indices`、`num_filtered_chunk`、`chunk_to_batch` 四个名字；
2. 对每次出现，记录行号并判断它是「生产」（calc_chunks 内部）还是「消费」（moba_attn_varlen 内部）；
3. 对照上表核对是否找全。

**观察现象 / 预期结果**：四个名字在 calc_chunks 内各出现一次（生产+返回），消费点合计 8 处，与上表一致。若你找到的消费点数量不同，说明源码版本与讲义 HEAD（`b5d5836`）不一致，请先 `git log` 确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `calc_chunks` 的返回值里没有任何模型张量（Q/K/V）？
**答案**：它只做索引几何——描述「块怎么切、在哪、属于谁、谁参选」。把它与数据解耦后，同样的元数据可复用于 gate 计算、KV gather、LSE 合并等多个阶段；也正因如此它才能被缓存（4.5 节）。

**练习 2**：`num_filtered_chunk` 等于什么表达式？
**答案**：`num_chunk − batch`，即总块数减序列条数（每条序列恰好剔除一个最后一块）。

### 4.2 cu_num_chunk 与 cu_chunk：两级前缀和

#### 4.2.1 概念说明

从「每条序列的长度」到「每个块的全局起始偏移」需要两级前缀和：

- **第一级（batch → 块编号）**：`cu_num_chunk` 告诉你第 \(i\) 条序列的第一块是全局第几块；
- **第二级（块编号 → token 偏移）**：`cu_chunk` 告诉你全局第 \(j\) 块从哪个 token 位置开始。

之所以需要第二级，是因为块**不跨 batch**：序列末尾的短块会打乱等差节奏（见贯穿示例：块 1 从 token 10 开始而不是 16）。

#### 4.2.2 核心流程

以贯穿示例（`cu=[0,10,30,45]`，\(C=16\)）手工推导全表：

| 量 | 公式 | 值 |
|---|---|---|
| `batch_sizes` | `cu[1:] - cu[:-1]` | `[10, 20, 15]` |
| `batch_num_chunk` | `ceil(L/C)` | `[1, 2, 1]` |
| `cu_num_chunk` | `ones(B+1)`，`[1:] = cumsum` | `[1, 1, 3, 4]` |
| `num_chunk` | `cu_num_chunk[-1]` | `4` |
| `chunk_sizes` | 全填 C，下标 0 置 0，各序列最后一块槽位改为 \(r_i\) | `[0, 10, 16, 4, 15]` |
| `cu_chunk` | `cumsum(chunk_sizes)` | `[0, 10, 26, 30, 45]` |

由此得到**块布局表**（本讲最重要的表，后面所有小节都引用它）：

| 块编号 | token 区间（全局） | 大小 | 所属 batch | 是否 MoBA 候选 |
|---|---|---|---|---|
| 0 | \([0, 10)\) | 10 | 0 | 否（batch 0 的最后一块） |
| 1 | \([10, 26)\) | 16 | 1 | 是 |
| 2 | \([26, 30)\) | 4 | 1 | 否（batch 1 的最后一块） |
| 3 | \([30, 45)\) | 15 | 2 | 否（batch 2 的最后一块） |

注意两个结构性质：**只有各序列的最后一块可能小于 \(C\)**（中间块恒为满块，如块 1 恰好 16）；`cu_chunk[-1] = 45` 恰为总 token 数。

#### 4.2.3 源码精读

- [moba/moba_efficient.py:L18-L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L18-L21)：差分得序列长度，向上取整得每序列块数。
- [moba/moba_efficient.py:L22-L30](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L22-L30)：`cu_num_chunk` 用 `torch.ones(..., device=..., dtype=...)` 原地构造再把 `[1:]` 覆盖成 `cumsum`——这是一种直接在目标 device/dtype 上构造张量的惯用写法，避免从 Python list 构造引发 CPU→GPU 拷贝。**注意 `cu_num_chunk[0]` 是 ones 留下的哑元（值为 1 而非 0），从不被读取**：所有消费点（L30 的 `[-1]`、L37/L51 的 `[1:]`、L44 的 `[1:-1]`）都带偏移切片，哑元无害。理解时建议在脑中把它替换成标准前缀和 \(P = [0, 1, 3, 4]\)（\(P_i\) = 第 \(i\) 条序列首块编号，\(P_B\) = 总块数）。
- [moba/moba_efficient.py:L31-L39](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L31-L39)：`chunk_sizes` 长度为 `num_chunk + 1`、全填 \(C\)，先置 `chunk_sizes[0] = 0`（注释 "for calc cu chunk"，即凑前缀和的哑元），再用 `batch_last_chunk_size = batch_sizes - (batch_num_chunk - 1) * C`（即公式 \(r_i\)）覆盖各序列最后一块对应的槽位。**下标整体偏移一格：`chunk_sizes[j+1]` 才是第 \(j\) 块的大小**——覆盖用的索引 `cu_num_chunk[1:] = [1, 3, 4]` 恰好是各序列最后一块的「槽位号」（末块编号 + 1）。`cumsum` 后得到 `cu_chunk`。

#### 4.2.4 代码实践

**目标**：手工推导一个新输入的 `cu_chunk`，再用代码验证。

**步骤**：
1. 取 `cu_seqlens = [0, 7, 32]`，`C = 8`；
2. 在纸上依次写出 `batch_sizes`、`batch_num_chunk`、`chunk_sizes`、`cu_chunk`；
3. 在能运行 `moba` 环境的机器上执行：
   ```python
   import torch
   from moba.moba_efficient import calc_chunks
   cu = torch.tensor([0, 7, 32], dtype=torch.int32)
   cu_chunk, fci, nfc, c2b = calc_chunks(cu, 8)
   print(cu_chunk, fci, nfc, c2b)
   ```
   若机器没有 flash-attn（CPU 环境），把 `calc_chunks` 函数体复制到独立脚本运行即可——它只依赖 torch 基础算子（标注：示例代码）。

**需要观察的现象 / 预期结果**（手工推导值，待本地验证）：`cu_chunk = [0, 7, 15, 23, 31, 32]`，即 batch 1 的 25 个 token 切成 `[7,15) [15,23) [23,31) [31,32)` 四块，最后一块大小为 1。

#### 4.2.5 小练习与答案

**练习 1**：贯穿示例中，若没有「块不跨 batch」的约束（允许整条 45-token 长序列按 16 等距切块），块边界会是什么？为什么 MoBA 不这么做？
**答案**：等距切块得到 \([0,16) [16,32) [32,45)\)，batch 0 与 batch 1 会落进同一个块。MoBA 的因果性以 batch 为单位（batch 1 的 query 不能看 batch 0 的 key），跨 batch 的块会让 gate 掩码与块选择复杂化，还会在 batch 边界处引入无效 key。

**练习 2**：序列长度恰好是 \(C\) 的整数倍（如 \(L=32, C=16\)）时，最后一块还该被剔除吗？
**答案**：仍被剔除（`chunk_to_remove` 不区分满不满）。4.4 节的论证与此无关——最后一块只可能是「当前块」，从不是合法历史候选，剔除无损失。

### 4.3 chunk_to_batch：块到 batch 的归属

#### 4.3.1 概念说明

`chunk_to_batch[j]` = 全局第 \(j\) 块属于第几条序列。反向映射之所以必要，是因为 varlen 批次里 MoBA 的 gate 掩码需要同时施加两种约束（u3-l3 详述）：**块内因果**（query 位置须不小于块结束偏移）与 **batch 隔离**（query 不能看其他序列的块）。后者需要先知道每个候选块属于哪条序列，再取该序列的结束位置比较。

#### 4.3.2 核心流程

构造用一个经典的「打标记 + 前缀和」技巧：

```text
① zeros(num_chunk)
② 在「每条序列(除第 0 条)的首块」位置写 1
   （这些下标恰好就是 cu_num_chunk[1:-1]）
③ cumsum → 每块的 batch 编号
```

贯穿示例：`zeros(4)` → 在 `[1, 3]` 处写 1 得 `[0, 1, 0, 1]` → cumsum 得 `chunk_to_batch = [0, 1, 1, 2]`，与块布局表一致。

为什么第 0 条序列不用标记？因为 cumsum 的初值是 0，第 0 条序列的块天然继承编号 0。

#### 4.3.3 源码精读

- [moba/moba_efficient.py:L40-L45](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L40-L45)：注释 `# chunk_to_batch[chunk_idx] = batch idx of the chunk idx`。`chunk_to_batch[cu_num_chunk[1:-1]] = 1` 用高级索引批量散射：`cu_num_chunk[1:-1]` 恰是第 1..B-1 条序列首块的（真）编号——这里同一个数字身兼两职：在 `cu_num_chunk` 的语义里它是「首块编号」，在 `chunk_to_batch` 的下标里它就是块编号本身（因为偏移抵消：首块编号 = 末块槽位 − 1，而标记首块用的是真编号）。随后 `cumsum(dtype=torch.int32)` 保住 int32。
- 消费点：[moba/moba_efficient.py:L352-L356](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L352-L356)：`batch_end = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]`——先选出候选块的归属，再取各归属序列的结束偏移，供 `gate_batch_end_mask` 排除跨序列注意力（u3-l3 精读）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：验证「打标记 + 前缀和」技巧与你逐块手写的结果一致。

**步骤**：
1. 对贯穿示例逐块判断归属：块 0（区间 \([0,10)\)）落在序列 0 → 记 0；块 1、2 落在序列 1 → 记 1；块 3 落在序列 2 → 记 2；
2. 对照 4.3.2 中源码技巧的输出 `[0, 1, 1, 2]`；
3. 再对练习输入 `cu=[0, 7, 32]`、`C=8` 重复一遍（块布局见 4.2.4 预期）。

**预期结果**：`cu=[0,7,32]` 时 `chunk_to_batch = [0, 1, 1, 1, 1]`（batch 1 独占 4 块）。待本地验证。

#### 4.3.5 小练习与答案

**练习**：`chunk_to_batch` 的长度为什么是 `num_chunk` 而 `cu_chunk` 的长度是 `num_chunk + 1`？
**答案**：`cu_chunk` 是「边界」语义——\(n\) 个区间需要 \(n+1\) 个边界（与 `cu_seqlens` 同理，首元素 0）；`chunk_to_batch` 是「属性」语义——每块一个值。

### 4.4 filtered_chunk_indices：为什么以及如何剔除每个 batch 的最后一块

#### 4.4.1 概念说明

这是 `calc_chunks` 最核心的设计决策，也是高效实现与 naive 实现的分水岭。

回顾 u3-l1 的大图：高效实现把每个 query 的注意力拆成两路——

- **self-attn 支路**：以 `cu_chunk` 为 varlen 边界、`causal=True`，即**每个块内部**做因果注意力。对任意 query，这就是它「当前块」的贡献（块内因果）；
- **moba 支路**：query 对被 gate 选中的**其他块**做非因果注意力。

于是 gate 只需要从「严格位于 query 过去的**完整**块」里选 `moba_topk − 1` 个（顶层 topk 减一的由来，L313-L314）。而**每条序列的最后一块永远不可能是任何 query 的合法历史候选**：

> **命题**：设序列 \(b\) 的最后一块为 \(c_{\text{last}}\)。对 \(b\) 中任意 query（位于某块 \(c\)）：
> - 若 \(c = c_{\text{last}}\)，则 \(c_{\text{last}}\) 是它的当前块，块内注意力由 self-attn 支路覆盖，gate 不应也无法再选它；
> - 若 \(c < c_{\text{last}}\)，则 \(c_{\text{last}}\) 在该 query 的**未来**，因果规则本就禁止选择；
> - 序列 \(b\) 中不存在位于 \(c_{\text{last}}\) 之后的 query；其他序列的 query 则被 batch 隔离规则排除。
>
> 三种情形穷尽，故剔除 \(c_{\text{last}}\) 不损失任何注意力连接。∎

剔除还带来一个重要的工程红利：**候选块全部是满块**（大小恰为 \(C\)，因为只有各序列最后一块可能不满）。此后：

- [moba/moba_efficient.py:L325-L330](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L325-L330)：候选 KV 的 gather 索引可以写成「固定 `arange(C)` + 每块起始偏移」的规整矩阵；
- [moba/moba_efficient.py:L415-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L423)：moba 支路 KV 侧的 `cu_seqlen` 可以直接用公差为 \(C\) 的等差数列——若有不满块混入候选，这两处都会错。

对照 naive 实现可以看清「同一语义的两种表达」：naive 没有任何剔除，全部 `num_block` 块都进 gate（[moba/moba_naive.py:L45-L50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L45-L50) 的 `math.ceil` 分块与 calc_chunks 完全一致），靠 ±inf 修正（当前块置 `+inf` 必选、未来块置 `-inf`，[moba/moba_naive.py:L58-L61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61)）和 token 级 tril（[L79-L81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L79-L81)）在掩码层面表达同样的因果性。高效实现把这些语义**前移进了元数据**：剔除即「未来块 -inf」的不可选版本，self-attn 支路即「当前块 +inf」的必选版本。

#### 4.4.2 核心流程

```text
① chunk_to_remove = cu_num_chunk[1:] - 1     每条序列最后一块的块编号
② chunk_to_remain = ones(num_chunk, bool)
③ chunk_to_remain[chunk_to_remove] = False   剔除
④ filtered_chunk_indices = nonzero → 剩余块编号升序列表
⑤ num_filtered_chunk = len(...) = num_chunk - batch
```

贯穿示例：`cu_num_chunk[1:] - 1 = [1,3,4] - 1 = [0, 2, 3]`（三个序列的最后一块）→ `chunk_to_remain = [F, T, F, F]` → **`filtered_chunk_indices = [1]`，`num_filtered_chunk = 1`**。整批 4 块中只有块 1（batch 1 的那个满块）参与 MoBA 竞选；batch 0 与 batch 2 各只有一块，全部留给 self-attn 支路。

`cu_num_chunk[1:] - 1` 为什么恰好给出末块编号：`cu_num_chunk[1:]` 的第 \(i\) 个元素是序列 \(i+1\) 的首块编号 = 序列 \(i\) 末块编号 + 1，减一即得；末尾元素 `cu_num_chunk[-1] = num_chunk`，减一恰是最后一条序列的末块编号。

#### 4.4.3 源码精读

- [moba/moba_efficient.py:L47-L57](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L47-L57)：注释写明意图 `# filter chunks ( remove last chunk of each batch )`。`chunk_to_remove` 计算如上；`chunk_to_remain` 是 bool 张量，散射置 False 后 `nonzero` 得升序索引（int64）。
- [moba/moba_efficient.py:L59-L64](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L59-L64)：按 `cu_chunk, filtered_chunk_indices, num_filtered_chunk, chunk_to_batch` 顺序返回。
- 消费点回顾（各处含义）：[L329](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L329) `cu_chunk[filtered_chunk_indices]` 取候选块起始偏移用于 gather KV；[L355](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L355) `cu_chunk[filtered_chunk_indices + 1]` 取候选块**结束**偏移（`+1` 依赖 `cu_chunk` 比 `chunk_to_batch` 多一格的边界语义）用于 gate 因果掩码；[L356](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L356) 配合 `chunk_to_batch` 取归属序列边界。

#### 4.4.4 代码实践（本讲核心实践）

**目标**：对 `cu_seqlens = [0, 10, 30, 45]`、`chunk_size = 16` 手工算出全部四个返回张量，再用代码验证，并回答规格中的两个问题。

**步骤**：

1. **手工计算**（先合上讲义试试）：按 4.2.2 / 4.3.2 / 4.4.2 的公式推导四个张量与「每条序列最后一块的大小」；
2. **代码验证**（GPU 环境；无 flash-attn 时复制函数体到独立脚本，示例代码）：
   ```python
   import torch
   from moba.moba_efficient import calc_chunks

   cu = torch.tensor([0, 10, 30, 45], dtype=torch.int32)
   cu_chunk, filtered_chunk_indices, num_filtered_chunk, chunk_to_batch = calc_chunks(cu, 16)
   print("cu_chunk: ", cu_chunk)                      # 期望 [0, 10, 26, 30, 45]
   print("filtered: ", filtered_chunk_indices)        # 期望 [1]
   print("num_filtered_chunk: ", num_filtered_chunk)  # 期望 1
   print("chunk_to_batch: ", chunk_to_batch)          # 期望 [0, 1, 1, 2]
   ```
3. **回答两个问题**（见下方预期结果；块布局表在 4.2.2）。

**需要观察的现象 / 预期结果**（手工推导值，待本地验证）：
- 每条序列最后一块的大小：batch 0 → **10**（块 0，同时是它唯一的块）；batch 1 → **4**（块 2，20 = 16 + 4 的尾部）；batch 2 → **15**（块 3，唯一的块，也不满 16）。
- 它们不在 `filtered_chunk_indices` 中的原因：即 4.4.1 的命题——最后一块对本序列的任何 query 要么是当前块（由 self-attn 支路处理块内因果注意力），要么在未来（因果规则本就禁选），且本序列没有任何 query 位于其后，其他序列又被 batch 隔离排除；因此它从不是合法的 gate 候选，剔除无损失，还让候选块全部成为满块、方便后续规整的 gather 与等差 `cu_seqlen`。

#### 4.4.5 小练习与答案

**练习 1**：一个 varlen 批次 `cu=[0, 100, 200]`、`C=64`、`moba_topk=3`。`filtered_chunk_indices` 是什么？`moba_attn_varlen` 最终用几个块做 gate 竞选？
**答案**：每条序列 \(\lceil 100/64 \rceil = 2\) 块，块布局 `[0,64) [64,100) [100,164) [164,200)`；各序列末块（块 1、块 3）被剔除，`filtered_chunk_indices = [0, 2]`，`num_filtered_chunk = 2`；`moba_topk = min(3-1, 2) = 2`，两个候选块全部参选。

**练习 2**：若把剔除逻辑去掉（候选集含末块），源码里**最早**出错的会是哪一行？
**答案**：[L325-L330](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L325-L330)。gather 索引按「每块恰好 \(C\) 个 token」构造（`arange(C) + 起始偏移`），不满的末块会把**下一序列开头的 token** 错误地 gather 进来（最后一块还会越界到序列末尾之外，行为未定义）；即便侥幸不越界，[L415-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L423) 的等差 `cu_seqlen` 也会与实际块长不符。

**练习 3**：`need_moba_attn` 为假的条件有哪些？
**答案**：`min(moba_topk - 1, num_filtered_chunk) == 0`，即 ① 顶层 `moba_topk = 1`（只看当前块）；或 ② `num_filtered_chunk = 0`，即**每条**序列都只有一块（都短于 \(2C\) 时可能发生，如贯穿示例极端化后 batch 全部短于一块）。此时整个调用退化为全量因果 `flash_attn_varlen_func`（L317-L321），与 naive 在同条件下的行为一致。

### 4.5 lru_cache：张量作 cache key 的行为与风险

#### 4.5.1 概念说明

[moba/moba_efficient.py:L10-L14](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L10-L14)：`calc_chunks` 被 `@lru_cache(maxsize=16)` 装饰，缓存键是 `(cu_seqlen, moba_chunk_size)`。

关键背景：**PyTorch 张量是可哈希的，但其哈希按对象身份（id）而非值**。因此：

- **同一个张量对象**再传一次 → 命中；
- **值相同的新张量对象** → 未命中（重算）。

这决定了缓存的有效场景与局限：

| 场景 | 行为 |
|---|---|
| 同一前向里多个 MoBA 层共享同一个 `cu_seqlens` 对象（自管 varlen 打包的训练循环常见用法） | 首次计算、其后全命中——这是缓存的收益场景 |
| 调用方每次前向新建 `cu_seqlens` | 永不命中，等价于没有缓存（无害，只是白占 16 个槽位） |
| 张量对象在两次调用之间被**原地修改** | 仍然命中，返回**过期结果**——正确性风险 |
| 缓存存活期间 | 张量被强引用无法释放（内存驻留），由 `maxsize=16` 的 LRU 淘汰兜底 |

本仓库自带的 transformers 集成路径属于第二类：[moba/wrapper.py:L62-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L62-L66) 中 `moba_layer` **每次**前向都用 `torch.cumsum(torch.tensor(...))` 新建 `cu_seqlens_k`，因此在这条路径上 `calc_chunks` 的缓存基本不会命中。缓存真正的服务对象是「构造一次、多次复用同一个张量对象」的调用方（如直接调用 `moba_attn_varlen` 的训练循环、同一步内多层共享同一 `cu_seqlens` 引用——后者是对典型训练代码的合理推断，仓库内无直接示例，待确认）。

#### 4.5.2 核心流程

`lru_cache` 内部是一个字典：查询键时先比哈希、再判相等。张量的身份哈希使得「值相等」的两个不同张量落入不同哈希桶，永不触发相等比较，也就永不命中。`moba_chunk_size` 是 Python int，按值哈希，所以完整命中条件是：**同一个张量对象 + 同一个 int**。

`maxsize=16`：以 LRU（最近最少使用）淘汰，最多驻留 16 组键值，防止强引用的张量无限堆积。

#### 4.5.3 源码精读

- [moba/moba_efficient.py:L14](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L14)：`@lru_cache(maxsize=16)` 直接装饰在签名上。函数体内没有任何 CPU/GPU 同步的标量读取（没有 `.item()`），返回值全部是张量与 `len()`——这是它能被缓存的前提之一：纯函数、无副作用。
- 对照：[moba/wrapper.py:L62-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L62-L66) 每次调用新建 `cu_seqlens_k`，是「身份哈希导致 miss」的仓库内实例。

#### 4.5.4 代码实践

**目标**：用一个最小实验证实「身份命中、值不命中」。

**步骤**（纯 CPU 即可，示例代码）：

```python
import torch
from functools import lru_cache

@lru_cache(maxsize=16)
def probe(cu, chunk_size):   # 模仿 calc_chunks 的装饰方式
    return chunk_size        # 返回值无所谓，只看 cache_info

a = torch.tensor([0, 10, 30, 45], dtype=torch.int32)
b = torch.tensor([0, 10, 30, 45], dtype=torch.int32)  # 值相同、对象不同
print(torch.equal(a, b))     # True：值确实相同

probe(a, 16)                 # miss
probe(b, 16)                 # 仍是 miss（对象不同）
probe(a, 16)                 # hit（同一个对象）
print(probe.cache_info())
```

**需要观察的现象 / 预期结果**：`torch.equal(a, b)` 为 True，但 `cache_info()` 显示 `hits=1, misses=2`（待本地验证）。若再执行 `a[0] += 1` 后调用 `probe(a, 16)`，仍会命中——这就是原地修改导致过期结果的隐患演示（`calc_chunks` 的输入 `cu_seqlens` 在实践中通常不被原地修改，但这是使用该缓存的前提约定）。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `@lru_cache` 去掉，哪些调用方受影响？
**答案**：正确性不受任何影响（每次重算）；只有「同一张量对象被重复传给多层」的调用方损失一次重复计算的机会。在仓库自带的 wrapper 路径上几乎无差别（本来就不命中）。

**练习 2**：`maxsize=16` 防的是什么问题？
**答案**：缓存字典对键（张量）持有强引用，张量在淘汰前无法被垃圾回收；无界的缓存会随训练步数累积旧批次张量、占用显存。`maxsize=16` 以 LRU 淘汰把驻留量封顶。

**练习 3**：为什么 `calc_chunks` 适合缓存，而 `moba_attn_varlen` 整体不适合？
**答案**：`calc_chunks` 是纯索引函数——输出只依赖 `(cu_seqlen, moba_chunk_size)`，与 Q/K/V 数值无关，重用绝对安全；`moba_attn_varlen` 依赖随步变化的数据张量，且需参与 autograd，缓存既无意义也不可行。

## 5. 综合实践

把本讲所有内容串成一个可断言的脚本（GPU 环境直接 import；无 flash-attn 的机器把 `calc_chunks` 函数体复制进脚本，示例代码）：

**任务**：写一个 `verify_calc_chunks(cu_list, C)` 函数，对任意输入自动检验四条结构不变量，并在两组输入上运行。

```python
import torch
from moba.moba_efficient import calc_chunks

def verify_calc_chunks(cu_list, C):
    cu = torch.tensor(cu_list, dtype=torch.int32)
    cu_chunk, fci, nfc, c2b = calc_chunks(cu, C)

    # ① cu_chunk 是边界：首元素 0，末元素 = 总 token 数，长度 = 块数 + 1
    assert cu_chunk[0].item() == 0 and cu_chunk[-1].item() == cu_list[-1]

    # ② 中间块恒为满块：相邻边界差要么等于 C（非末块），要么属于某条序列的末块
    sizes = (cu_chunk[1:] - cu_chunk[:-1]).tolist()
    assert nfc == len(fci) and nfc == len(c2b) - len(cu_list) + 1  # num_chunk - batch

    # ③ 剔除的恰好是每条序列的最后一块
    removed = sorted(set(range(len(c2b))) - set(fci.tolist()))
    starts = {b: [j for j in range(len(c2b)) if c2b[j] == b] for b in range(len(cu_list) - 1)}
    assert removed == [starts[b][-1] for b in starts]

    # ④ 候选块全部是满块（本讲的工程红利）
    assert all(sizes[j] == C for j in fci.tolist())

    print(f"cu={cu_list} C={C} -> 块大小{sizes} 剔除{removed} 候选{fci.tolist()}")
    return cu_chunk, fci, nfc, c2b

verify_calc_chunks([0, 10, 30, 45], 16)   # 贯穿示例
verify_calc_chunks([0, 7, 32], 8)         # 4.2.4 的练习输入
verify_calc_chunks([0, 100, 200], 64)     # 4.4.5 练习 1 的输入
```

**观察与预期**（手工推导值，待本地验证）：

| 输入 | 块大小 | 被剔除的块 | 候选块 |
|---|---|---|---|
| `[0,10,30,45]`, C=16 | `[10, 16, 4, 15]` | `[0, 2, 3]` | `[1]` |
| `[0,7,32]`, C=8 | `[7, 8, 8, 8, 1]` | `[0, 4]` | `[1, 2, 3]` |
| `[0,100,200]`, C=64 | `[64, 36, 64, 36]` | `[1, 3]` | `[0, 2]` |

最后一行恰好印证 4.4.5 练习 1 的推导。四条断言全部通过即说明你已完全掌握本讲的结构性质；任一断言失败时，先回到 4.2.2 的块布局表逐块排查。

随后做一个小扩展：把 `verify_calc_chunks([0, 10, 30, 45], 16)` 的结果与 [tests/test_moba_attn.py:L26-L29](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L26-L29) 的随机 `cu_seqlen` 构造方式对照——测试从 `range(1, seqlen-1)` 无放回采样 `batch-1` 个切点再排序，保证了序列长度严格为正且边界严格递增，这正是 `calc_chunks` 隐含假设的输入形态（每条序列 ≥ 1 个 token）。你可以在 `verify_calc_chunks` 里再随机生成若干组这样的 `cu_list`，验证不变量在随机输入下依然成立。

## 6. 本讲小结

- `calc_chunks` 是 `moba_attn_varlen` 的坐标系：用两级前缀和（序列→块编号→token 偏移）产出 `cu_chunk`、`filtered_chunk_indices`、`num_filtered_chunk`、`chunk_to_batch` 四个纯索引张量。
- 块不跨 batch 边界，只有各序列的最后一块可能不满 `moba_chunk_size`；`cu_num_chunk[0]` 与 `chunk_sizes[0]` 都是凑前缀和的哑元，消费点全部带偏移切片。
- 每条序列的最后一块被剔除出 MoBA 候选集：它对本序列任何 query 要么是当前块（由 self-attn 支路以 `cu_chunk` 为 varlen 边界处理块内因果注意力），要么在未来（因果禁选），剔除零损失，且使候选块全部为满块——这是后续规整 gather（L326-L330）与等差 `cu_seqlen`（L415-L423）的前提，也是顶层 `moba_topk` 减一的由来。
- `num_filtered_chunk = num_chunk - batch`，它封顶 `moba_topk`；当其为 0 或 `topk=1` 时整个调用退化为全量因果 flash-attn。
- naive 实现用 ±inf 与 token 级 tril 在掩码层表达同样的分块与因果语义；高效实现把这部分语义前移进了元数据——「同一算法，两种表达」。
- `@lru_cache` 以张量身份为键：同一对象才命中，值相同的新张量不命中；仓库自带的 wrapper 路径每次新建 `cu_seqlens`，缓存基本不命中，其收益场景是同一张量对象被多层复用的训练循环；原地修改张量会导致过期结果的正确性风险。

## 7. 下一步学习建议

本讲产出的元数据将在下一讲 **u3-l3（gate 的向量化计算与跨 batch 因果掩码）** 中被密集消费：`cu_chunk[filtered_chunk_indices + 1]` 与 `cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]`（L355-L356）会分别变成 gate 的块内因果掩码与 batch 隔离掩码。建议：

1. 先完成本讲综合实践，确保能对任意 `cu_seqlens` 手推块布局表；
2. 预读 [moba/moba_efficient.py:L332-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L332-L371)，找出其中每个本讲元数据张量的出现位置；
3. 带着问题进入下一讲：为什么 `gate_chunk_end_mask` 用的是块的**结束**偏移而不是起始偏移？（提示：候选块必须是「严格完整的过去块」。）
