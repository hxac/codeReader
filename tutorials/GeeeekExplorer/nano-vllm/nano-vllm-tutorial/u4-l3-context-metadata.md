# Context 元数据传递机制

## 1. 本讲目标

上一讲（u4-l1）我们看到 `ModelRunner.prepare_prefill` / `prepare_decode` 构造出了一大堆注意力元数据——`cu_seqlens_q`、`cu_seqlens_k`、`slot_mapping`、`context_lens`、`block_tables`、`max_seqlen_q/k`——并且只把 `input_ids` 和 `positions` 作为返回值显式传给 `run_model`。可问题是：底层的 `Attention` 层要写 KV cache、读 paged cache，离不开 `slot_mapping` 和 `block_tables`；`ParallelLMHead` 在 prefill 时要「只取每条序列最后一个 token」，离不开 `cu_seqlens_q`。这些元数据**没有**出现在模型前向的函数签名里，它们是怎么「凭空」到达底层各个层的？

答案就是本讲的主角：一个名为 `Context` 的**全局单例**。本讲回答三个问题：

- 这个全局 `Context` 装了哪些字段，每个字段被谁消费？
- `set_context` / `get_context` / `reset_context` 三件套是如何协同，让元数据「按推理步存活、按推理步清空」的？
- 为什么 nano-vllm 选择「全局变量传参」而不是「改函数签名」？这种设计在 CUDA Graph 场景下又带来了什么约束？

学完本讲，你应当能够：

- 逐字段说清 `Context` dataclass 的含义，并画出「字段 → 消费者」的映射表。
- 描述一次 `run()` 调用中 `Context` 的完整生命周期：`prepare_*` 设值 → 各层 `get_context()` 读值 → `reset_context()` 清空。
- 解释 `ParallelLMHead.forward` 在 prefill 时用 `cu_seqlens_q[1:] - 1` 只取每序列最后一个 token 的优化原理，并手算给定 batch 时的下标。
- 理解「全局 Context」这一架构取舍的收益（保持 Transformer 前向签名干净）与代价（隐式依赖、需配合 reset）。

本讲是「模型执行」单元的接口骨架，承接 u4-l1（张量从哪来）与 u4-l2（attention 怎么用），为 u4-l4（Qwen3 结构）、u5-l1（CUDA Graph）打基础。

## 2. 前置知识

本讲默认你已掌握以下前置结论，这里只做最简回顾：

- **模型前向签名（来自 u4-l1）**：`Qwen3ForCausalLM.forward(input_ids, positions)` 与 `Qwen3Model.forward(input_ids, positions)` 只吃这两个张量。`Attention.forward(q, k, v)` 只吃当前层的 q/k/v。这些签名里**没有** `slot_mapping`、`block_tables` 之类的注意力元数据。
- **prefill / decode 的张量结构（来自 u4-l1）**：prefill 用 varlen 打包，`cu_seqlens_q`（新 token）/ `cu_seqlens_k`（含缓存前缀的全部 key）标记边界；decode 每序列送 1 个 token，靠 `context_lens`（序列总长）和 `block_tables` 读历史 cache。
- **Attention 的两套路径（来自 u4-l2）**：prefill 走 `flash_attn_varlen_func`（靠 `cu_seqlens`），decode 走 `flash_attn_with_kvcache`（靠 `context_lens` + `block_table`），新 K/V 都由 `store_kvcache` 经 `slot_mapping` 写入 cache。

此外你需要一个 Python 工程概念：

- **全局单例（module-level singleton）**：在一个模块里定义一个对象，同一进程内所有 `import` 它的代码拿到的都是同一个实例。这里用一个模块级变量 `_CONTEXT` 当「消息板」，写入方（`ModelRunner`）往里放数据，读取方（`Attention` / `ParallelLMHead`）从中取数据，从而绕开函数参数传递。

一句话直觉：**`Context` 是 `ModelRunner` 与底层 attention 层之间的一条「侧信道」——主路（函数参数）只走 `input_ids` / `positions`，旁路（全局 Context）走所有注意力元数据。**

## 3. 本讲源码地图

本讲围绕一个核心文件，并追踪它的两类消费者：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/utils/context.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py) | 本讲主角。定义 `Context` dataclass 与 `set_context` / `get_context` / `reset_context` 三件套，全文件不到 30 行。 |
| [nanovllm/layers/attention.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py) | 消费方一：`Attention.forward` 用 `get_context()` 取 `slot_mapping`（写 cache）、`cu_seqlens`/`max_seqlen`（prefill）、`context_lens`/`block_tables`（decode）。 |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | 消费方二：`ParallelLMHead.forward` 用 `get_context()` 取 `is_prefill` 与 `cu_seqlens_q`，在 prefill 时只取每序列最后一个 token。 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 写入方：`prepare_prefill` / `prepare_decode` 调 `set_context`，`run` 末尾调 `reset_context`，`capture_cudagraph` 在捕获期也会 `set_context`。 |

## 4. 核心概念与源码讲解

### 4.1 Context：为什么需要一个全局单例来传参

#### 4.1.1 概念说明

先看一个对照：标准 Transformer 的前向签名是什么样的？在 nano-vllm 里，模型主干是这样调用的（来自 u4-l1）：

```python
logits = self.model.compute_logits(self.model(input_ids, positions))
```

`self.model(input_ids, positions)` 会层层下传：`Qwen3ForCausalLM → Qwen3Model → Qwen3DecoderLayer → Qwen3Attention → Attention`。这条链路上的每一层，签名都只认 `input_ids` / `positions`（以及层内部的 `hidden_states`、`residual`）。

可是底层 `Attention` 在 nano-vllm 里要做的事远不止「标准 attention」——它要写 paged KV cache、要从 cache 跨块读历史 K/V、要区分 varlen prefill 与单 token decode。这些都依赖本步的注意力元数据。如果走「改函数签名」这条路，会发生什么？

```text
Qwen3Model.forward(input_ids, positions, cu_seqlens_q, cu_seqlens_k,
                   max_seqlen_q, max_seqlen_k, slot_mapping,
                   context_lens, block_tables, is_prefill)
  └── DecoderLayer.forward(... 同样十几个参数 ...)
        └── Qwen3Attention.forward(... 同样十几个参数 ...)
              └── Attention.forward(q, k, v, ... 同样十几个参数 ...)
```

每个中间层都得在自己签名里塞进 8 个它**根本不关心、只为往下透传**的参数，签名臃肿、易错、还和 HuggingFace 标准 Transformer 的接口对不齐。`ParallelLMHead`（`lm_head`）就更尴尬：它也需要 `is_prefill` 和 `cu_seqlens_q`，但它住在模型最末端，要把参数一路从 `compute_logits` 传过来。

nano-vllm 的解法是**侧信道（side channel）**：用一个全局 `Context` 对象当「消息板」，`ModelRunner` 在每步前向**之前**把本步的元数据贴上去，需要这些数据的层（`Attention`、`ParallelLMHead`）在 `forward` 里**主动去取**。这样：

- 模型主干签名保持干净：`forward(input_ids, positions)`，与标准 Transformer 一致。
- 只有真正需要元数据的层才 `get_context()`，中间层完全无感。
- 新增一种元数据（比如未来的某优化）只需给 `Context` 加字段、给写入方加赋值、给消费方加读取，**不动**任何中间层签名。

代价是引入了**隐式依赖**：`Attention.forward` 的行为不再只由参数决定，还取决于全局 `Context` 的当前状态。这就是为什么后面要专门讲生命周期——必须保证 `Attention` 读到的 `Context` 永远是「当前这一步」的。

#### 4.1.2 核心流程：八个字段与一个全局变量

`Context` 是一个带 `slots=True` 的 dataclass，共 8 个字段。`slots=True` 的作用是禁止动态新增属性、节省内存——它本质上是一个「定长结构体」，字段固定：

```text
Context 字段            含义                                主要写入方           主要消费方
─────────────────────────────────────────────────────────────────────────────────────────────
is_prefill             本步是否 prefill                    prepare_prefill/     Attention 分流、
                                                            prepare_decode       ParallelLMHead 分流

cu_seqlens_q           varlen 的 query 累积长度            prepare_prefill      Attention(prefill)、
                                                            （decode 留 None）    ParallelLMHead(末 token)

cu_seqlens_k           varlen 的 key 累积长度              prepare_prefill      Attention(prefill)
                                                            （decode 留 None）

max_seqlen_q           batch 内最长 query 序列长           prepare_prefill      Attention(prefill,
                                                            （decode 留 0）       flash_attn 内核启动)

max_seqlen_k           batch 内最长 key 序列长             prepare_prefill      Attention(prefill)
                                                            （decode 留 0）

slot_mapping           每个 token 的 K/V 写入槽位           prepare_prefill/     Attention → store_kvcache
                                                            prepare_decode       （两个阶段都用）

context_lens           decode 时每序列总长                 prepare_decode       Attention(decode,
                                                            （prefill 留 None）   cache_seqlens)

block_tables           分页块表（物理块号矩阵）            prepare_decode（必）  Attention(读 cache)、
                                                            prepare_prefill      （仅前缀缓存时）
```

一句话归纳：**`cu_seqlens_q/k`、`max_seqlen_q/k` 是 prefill 专属；`context_lens` 是 decode 专属；`slot_mapping` 两个阶段都用；`block_tables` decode 必用、prefill 仅前缀缓存时用；`is_prefill` 是总开关。**

这 8 个字段在「同一步内」是一组一致的快照——要么整组是 prefill 的元数据，要么整组是 decode 的。不存在「半 prefill 半 decode」的混搭，因为调度器每一步只产生纯 prefill 或纯 decode（见 u2-l2）。

#### 4.1.3 源码精读

整个 `Context` 机制的全貌只有这一个文件，请通读：

[utils/context.py:1-27 — Context 三件套全貌](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L1-L27)

```python
from dataclasses import dataclass
import torch


@dataclass(slots=True)
class Context:
    is_prefill: bool = False
    cu_seqlens_q: torch.Tensor | None = None
    cu_seqlens_k: torch.Tensor | None = None
    max_seqlen_q: int = 0
    max_seqlen_k: int = 0
    slot_mapping: torch.Tensor | None = None
    context_lens: torch.Tensor | None = None
    block_tables: torch.Tensor | None = None

_CONTEXT = Context()

def get_context():
    return _CONTEXT

def set_context(is_prefill, cu_seqlens_q=None, cu_seqlens_k=None, max_seqlen_q=0, max_seqlen_k=0, slot_mapping=None, context_lens=None, block_tables=None):
    global _CONTEXT
    _CONTEXT = Context(is_prefill, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, context_lens, block_tables)

def reset_context():
    global _CONTEXT
    _CONTEXT = Context()
```

逐行拆解：

- `@dataclass(slots=True)`：自动生成 `__init__`，字段顺序就是构造函数参数顺序。每个字段都带默认值，所以 `Context()` 会得到一个「全默认」的空对象——这正是 `reset_context` 想要的「白板」状态：`is_prefill=False`、所有张量为 `None`、所有整数为 `0`。
- 字段顺序不是随便排的：`is_prefill` 在最前，因为 `set_context` 把它作为唯一的位置必填参数；其余字段都有默认值，调用方可按需用关键字传。
- `_CONTEXT = Context()`：模块级变量，进程内唯一。它在 `import` 时就被初始化为一个空 `Context`。
- `get_context()`：只读，直接返回当前 `_CONTEXT`。**它不拷贝**——返回的是对象引用，所以消费方拿到后应「即取即用」，不要跨步持有（下一步 `set_context` 会换一个新对象）。
- `set_context(...)`：**新建**一个 `Context` 对象并让 `_CONTEXT` 重新指向它。注意它不是修改旧对象的字段，而是整个换一个新对象——这样上一步的对象即便被某处意外持有，也不会被「就地改坏」，每一步的数据互不污染。
- `reset_context()`：把 `_CONTEXT` 换回一个全默认的空 `Context()`，等价于「清空消息板」。

这里有一个值得注意的对比：`set_context` 选择「换新对象」而非「改字段」，是出于安全——每步的数据都是一次性的快照，换新对象能保证旧引用不可变，调试时也更容易定位「这一步的 Context 到底是什么」。但这也意味着：**不能用「把某字段置 None」来表示「本步不需要它」之外的任何渐进状态**，因为下一步 `set_context` 会整体覆盖。

#### 4.1.4 代码实践

**实践目标**：动手确认 `Context` 是全局单例、且 `set_context` 换的是对象引用。

**操作步骤**：

1. 写一个最小的 Python 片段（**示例代码，不需放进仓库**），单独 import 这个模块：

   ```python
   # 示例代码：验证 Context 单例语义
   from nanovllm.utils.context import get_context, set_context, reset_context, Context

   c0 = get_context()
   print("初始 is_prefill =", c0.is_prefill, "slot_mapping =", c0.slot_mapping)

   set_context(True, slot_mapping="fake_tensor_a")   # 字符串代替张量，仅观察引用
   c1 = get_context()
   print("set 后 is_prefill =", c1.is_prefill, "slot_mapping =", c1.slot_mapping)

   print("c0 is c1 ?", c0 is c1)   # set_context 是否换了对象？
   print("c0.is_prefill =", c0.is_prefill)   # 旧引用是否被就地改坏？

   reset_context()
   c2 = get_context()
   print("reset 后 is_prefill =", c2.is_prefill, "c1.is_prefill =", c1.is_prefill)
   ```

2. 在仓库根目录（已 `pip install -e .` 或 `PYTHONPATH` 含当前目录）运行：
   ```bash
   python -c "exec(open('your_snippet.py').read())"
   ```

**需要观察的现象**：

- 初始 `c0.is_prefill` 为 `False`、`slot_mapping` 为 `None`（全默认空对象）。
- `set_context` 后 `c1.is_prefill=True`、`slot_mapping="fake_tensor_a"`。
- `c0 is c1` 应为 `False`——`set_context` **新建**了对象，`c0` 仍指向旧的空对象。
- `c0.is_prefill` 仍为 `False`——旧引用未被就地修改，证明「换新对象」而非「改字段」。
- `reset_context` 后 `c2.is_prefill=False`，而 `c1.is_prefill` 仍为 `True`（旧对象不受影响）。

**预期结果**：上述五条全部成立，说明 `Context` 是「按步替换、不可变快照」的全局单例。**待本地验证**（本实践不依赖 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@dataclass(slots=True)` 改成普通 `@dataclass`，本讲的逻辑会出错吗？
**答案**：不会立刻出错，但会失去两点好处。其一，普通 dataclass 允许 `c.some_new_field = x` 这种动态新增属性，容易把字段名拼错（比如 `c.cu_seqlens_q` 写成 `c.cu_seqLens_q`）却悄悄创建新属性而非报错，调试困难；`slots=True` 会直接 `AttributeError`。其二，普通 dataclass 每个实例多一个 `__dict__`，占用更大。这里用 `slots` 是为了「字段固定、早报错、省内存」。

**练习 2**：`set_context` 为什么用「新建对象再赋值」而不是「直接改 `_CONTEXT` 的字段」？请给出一个「改字段会出问题」的场景。
**答案**：换新对象能让每一步的 Context 成为一个不可变快照。若改成「就地改字段」，假设某段代码在某步前提前 `c = get_context()` 抓了一个引用，并在异步/延迟执行时才读取 `c.slot_mapping`，那么当主流程在下一步 `set_context` 就地改了字段后，这个旧引用读到的就是「下一步」的新值，造成错乱。换新对象则保证旧引用永远停留在它被抓那一刻的状态。nano-vllm 实际上每步都即时消费、不跨步持有，所以两种写法在正常路径下表现一致；换新对象是更稳健的防御式写法。

---

### 4.2 set_context / get_context / reset_context 的生命周期

#### 4.2.1 概念说明

全局单例最大的风险是「读到脏数据」——如果上一步的元数据没清干净，本步的 `Attention` 就可能用到上一步的 `slot_mapping`，把 K/V 写错位置。因此 nano-vllm 给 `Context` 设计了一套严格的**「每步生命周期」**：

- **每步开始**：`prepare_prefill` 或 `prepare_decode` 调 `set_context`，把本步的元数据贴上去。
- **前向期间**：模型主干被调用，沿途每个 `Attention` 层、末端的 `ParallelLMHead` 都 `get_context()` 取用本步数据。
- **每步结束**：`run` 的最后一行 `reset_context()`，把消息板擦回白板，为下一步兜底。

这套生命周期是「前向正确性」的基石：任何一层只要在前向期间调 `get_context()`，拿到的必然是「当前这一步」的元数据。

#### 4.2.2 核心流程

一次 `ModelRunner.run(seqs, is_prefill)` 中 `Context` 的状态变迁：

```text
run() 入口
  │
  ▼
(1) prepare_prefill / prepare_decode
    └── set_context(is_prefill, ...)     ← 写入本步元数据（消息板贴满）
  │
  ▼
(2) run_model(input_ids, positions, is_prefill)
    └── self.model(...) 前向
          ├── Qwen3DecoderLayer.forward(...)
          │     └── Qwen3Attention.forward(...)
          │           └── Attention.forward(q, k, v)
          │                 └── context = get_context()   ← 读取本步元数据
          │                       store_kvcache(... context.slot_mapping)
          │                       flash_attn_...(context.cu_seqlens_q, ...)
          ...（多层重复）...
          └── compute_logits(hidden_states)
                └── ParallelLMHead.forward(x)
                      └── context = get_context()         ← 读取本步元数据
                            if context.is_prefill: 用 context.cu_seqlens_q 取末 token
  │
  ▼
(3) sampler(logits, temperatures)         （采样，不碰 Context）
  │
  ▼
(4) reset_context()                       ← 消息板擦回白板（兜底）
  │
  ▼
run() 返回 token_ids
```

三条不变式（invariant）：

1. **写入早于读取**：`set_context` 一定在 `self.model(...)` 之前发生（二者都在 `run` 内、且 `prepare_*` 先返回）。否则 attention 会读到上一步或空白的 Context。
2. **读取方只读不写**：`Attention` / `ParallelLMHead` 只调 `get_context()`，从不 `set_context`。写入权独占在 `ModelRunner` 手里，避免多源写入冲突。
3. **每步必 reset**：无论本步是 prefill 还是 decode、无论采样是否成功，`run` 末尾的 `reset_context()` 都会执行（除非前向抛异常提前退出）。

#### 4.2.3 源码精读

先看 `run` 里的「设值—前向—清空」三连。这是 `Context` 生命周期的主舞台：

[model_runner.py:214-220 — run 中 Context 的设值与清空](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)

```python
def run(self, seqs: list[Sequence], is_prefill: bool) -> list[int]:
    input_ids, positions = self.prepare_prefill(seqs) if is_prefill else self.prepare_decode(seqs)
    temperatures = self.prepare_sample(seqs) if self.rank == 0 else None
    logits = self.run_model(input_ids, positions, is_prefill)
    token_ids = self.sampler(logits, temperatures).tolist() if self.rank == 0 else None
    reset_context()
    return token_ids
```

注意 `prepare_prefill` / `prepare_decode` 在返回 `input_ids, positions` 的同时，**副作用**就是调了 `set_context`。来看两个写入点的具体参数：

[model_runner.py:169 — prepare_prefill 末尾的 set_context（prefill 全字段）](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L169)

```python
set_context(True, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, None, block_tables)
```

prefill 模式按**位置参数**顺序填满：`is_prefill=True`、`cu_seqlens_q`、`cu_seqlens_k`、`max_seqlen_q`、`max_seqlen_k`、`slot_mapping`、第 7 位 `context_lens=None`（prefill 用不到）、第 8 位 `block_tables`（仅前缀缓存时非 `None`）。prefill 显式把 `context_lens` 设为 `None`，保证即便上一步是 decode、`context_lens` 曾有值，本步也不会「漏」过来。

[model_runner.py:187 — prepare_decode 末尾的 set_context（decode 用关键字）](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L187)

```python
set_context(False, slot_mapping=slot_mapping, context_lens=context_lens, block_tables=block_tables)
```

decode 模式只传 `is_prefill=False` 与三个关键字参数：`slot_mapping`、`context_lens`、`block_tables`。其余字段（`cu_seqlens_q/k`、`max_seqlen_q/k`）取默认值 `None` / `0`——decode 的 attention 分支根本不读它们，所以留空无妨。

> **对比两种调用风格**：prefill 用位置参数「全填」，decode 用关键字参数「只填需要的」。二者都依赖 `set_context` 的默认值把未用字段填成「安全空值」。这正是 `Context` 每个字段都带默认值的设计意图——让 prefill / decode 各自只关心自己用得到的字段，未用字段自动是 `None`/`0`，消费方只要在对应分支里读对应字段就不会踩到脏数据。

再看一个容易被忽略的写入点：**CUDA Graph 捕获期**也会 `set_context`。这一点留到 u5-l1 详讲，这里只点出它的存在，因为它揭示了一个 `Context` 设计的深层约束：

[model_runner.py:238-248 — capture_cudagraph 在捕获每档 batch 时 set_context / reset_context](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L238-L248)

```python
for bs in reversed(self.graph_bs):
    graph = torch.cuda.CUDAGraph()
    set_context(False, slot_mapping=slot_mapping[:bs], context_lens=context_lens[:bs], block_tables=block_tables[:bs])
    outputs[:bs] = self.model(input_ids[:bs], positions[:bs])    # warmup
    with torch.cuda.graph(graph, self.graph_pool):
        outputs[:bs] = self.model(input_ids[:bs], positions[:bs])    # capture
    ...
    reset_context()
```

捕获时 `Attention` / `ParallelLMHead` 同样会 `get_context()`，所以必须在捕获前 `set_context` 把元数据喂上，捕获后再 `reset_context`。这里有一个 `Context` 与 CUDA Graph 交互的关键细节：捕获期 `set_context` 传入的 `slot_mapping[:bs]` 等是**对持久张量 `slot_mapping`（即后文 `graph_vars["slot_mapping"]`）的视图**，而图捕获的是「读这个张量存储」的操作。因此真正 decode 回放时，`run_model` 不能去 `set_context` 换新张量（换了图就读不到了），而是**就地修改 `graph_vars` 里那些持久张量的内容**：

[model_runner.py:200-210 — run_model 图回放路径：就地改 graph_vars，而非 set_context](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L200-L210)

```python
bs = input_ids.size(0)
context = get_context()
graph = self.graphs[next(x for x in self.graph_bs if x >= bs)]
graph_vars = self.graph_vars
graph_vars["input_ids"][:bs] = input_ids
graph_vars["positions"][:bs] = positions
graph_vars["slot_mapping"].fill_(-1)
graph_vars["slot_mapping"][:bs] = context.slot_mapping
graph_vars["context_lens"].zero_()
graph_vars["context_lens"][:bs] = context.context_lens
graph_vars["block_tables"][:bs, :context.block_tables.size(1)] = context.block_tables
graph.replay()
```

可以看到：图回放路径里 `context = get_context()` 只读不写，把 `context.slot_mapping` 等**拷进** `graph_vars` 的持久张量，然后 `graph.replay()`。这一段深刻体现了 `Context` 的定位——它是「每步的元数据入口」，但**真正被 CUDA Graph 固化的张量身份**是 `graph_vars`，而非 `Context` 本身。完整机制留到 u5-l1；这里只需记住：`Context` 在 eager 与 capture 两条路径下都被 `get_context` 读取，只是图回放路径多了一层「拷进持久张量」的转换。

#### 4.2.4 代码实践

**实践目标**：在真实推理中打印 `set_context` / `reset_context` 的调用时序与字段，验证「每步一设一清」的生命周期（即本讲指定的实践任务的上半部分）。

**操作步骤**：

1. 打开 `nanovllm/engine/model_runner.py`，给三处临时加日志（**示例代码，仅用于观察，验证后请删掉，勿提交**）。最不打扰逻辑的办法是包装 `set_context` / `reset_context`：

   ```python
   # 示例代码：在 model_runner.py 顶部 import 之后加一个轻量探针
   import nanovllm.utils.context as _ctxmod
   _orig_set = _ctxmod.set_context
   _orig_reset = _ctxmod.reset_context
   def _tracing_set(is_prefill, **kw):
       print(f"[set_context] is_prefill={is_prefill} "
             f"cu_seqlens_q={'set' if kw.get('cu_seqlens_q') is not None else 'None'} "
             f"slot_mapping={'set' if kw.get('slot_mapping') is not None else 'None'} "
             f"context_lens={'set' if kw.get('context_lens') is not None else 'None'} "
             f"block_tables={'set' if kw.get('block_tables') is not None else 'None'}")
       _orig_set(is_prefill, **kw)
   def _tracing_reset():
       print("[reset_context] <-- 清空")
       _orig_reset()
   _ctxmod.set_context = _tracing_set
   _ctxmod.reset_context = _tracing_reset
   ```

   > 注意：`attention.py` / `embed_head.py` 里写的是 `from ... import get_context`，所以 `get_context` 不需要替换；`set_context` / `reset_context` 只在 `model_runner.py` 内部用，而上面替换的是 `model_runner.py` 已 import 进来的名字空间——实践中更稳妥的做法是直接在 `set_context` / `reset_context` 函数体内加 `print`。下面给出**最稳妥**的改法，直接改 `context.py`：

   ```python
   # 示例代码：直接在 context.py 内打点（验证后务必还原）
   def set_context(is_prefill, cu_seqlens_q=None, cu_seqlens_k=None, max_seqlen_q=0, max_seqlen_k=0, slot_mapping=None, context_lens=None, block_tables=None):
       global _CONTEXT
       print(f"[set_context] is_prefill={is_prefill} "
             f"has_cu_q={cu_seqlens_q is not None} has_slot={slot_mapping is not None} "
             f"has_ctx_lens={context_lens is not None} has_bt={block_tables is not None}")
       _CONTEXT = Context(is_prefill, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, context_lens, block_tables)

   def reset_context():
       global _CONTEXT
       print("[reset_context]")
       _CONTEXT = Context()
   ```

2. 用 `example.py` 跑一次推理（需 GPU + 已下载的 Qwen3-0.6B）：
   ```bash
   python example.py
   ```

**需要观察的现象**：

- 第一组：`[set_context] is_prefill=True has_cu_q=True has_slot=True has_ctx_lens=False has_bt=False/True` → 紧跟一次前向 → `[reset_context]`。
- 之后每组：`[set_context] is_prefill=False has_cu_q=False has_slot=True has_ctx_lens=True has_bt=True` → 前向 → `[reset_context]`。
- 整个推理过程中，`set_context` 与 `reset_context` **严格成对、交替**出现，永不嵌套、永不为偶数个连续 `set_context`。

**预期结果**：日志呈现 `set → reset → set → reset → …` 的稳定节拍，prefill 那一次带 `cu_seqlens_q`、decode 那些次带 `context_lens`。具体次数取决于生成长度——**待本地验证**。

> 若无 GPU，可做「源码阅读型实践」：对照本节给出的状态变迁图，手动模拟两条序列各 prefill 一次、decode 三步的 `Context` 状态，列出每一步 `is_prefill` 与各字段是否非空，验证「prefill 与 decode 的非空字段集合互补」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prepare_prefill` 要显式把第 7 个位置参数写成 `None`（即 `context_lens=None`）？省略它（依赖默认值）会有什么不同？
**答案**：行为完全等价——`set_context` 的 `context_lens` 默认就是 `None`。显式写 `None` 是为了**可读性**：让读者一眼看出「prefill 不需要 context_lens」。同时它也起到防御作用：万一未来有人改了 `set_context` 的默认值，显式 `None` 仍能保证 prefill 路径下 `context_lens` 一定是空。这是一种「意图自文档化」的写法。

**练习 2**：假设某次推理中途 `run_model` 抛了异常，导致 `run` 末尾的 `reset_context()` 没有执行。下一步会出错吗？
**答案**：通常不会**立刻**出错，因为下一步的 `prepare_prefill` / `prepare_decode` 会再次 `set_context` 整体覆盖 `_CONTEXT`，把残留的旧元数据冲掉。所以 `reset_context` 在正常交替的步进里是「冗余的兜底」。它的真正价值在异常路径与边界场景：比如某段代码在两次前向之间 `get_context()` 去读「应当为空」的 Context（用于断言当前不在前向中），若没 reset 就会读到上一步的脏数据。`reset_context` 把 Context 的「空闲态」明确化，让这种自检成为可能。

**练习 3**：decode 路径下 `set_context` 没传 `cu_seqlens_q`，那它为什么是 `None` 而不是「保留上一步 prefill 的值」？
**答案**：因为 `set_context` 是「换一个全新的 `Context` 对象」，新对象里 `cu_seqlens_q` 取默认值 `None`，与上一步的对象完全无关。这正是「换新对象」而非「改字段」的好处：不需要显式把每个字段清零，新建对象时所有未传字段自动是默认空值，杜绝了跨步残留。

---

### 4.3 消费方：Attention 与 ParallelLMHead 如何读取 Context

#### 4.3.1 概念说明

前面两节讲清了「消息板」本身。这一节看「读消息的人」——也就是 `get_context()` 的两个调用方：

1. **`Attention.forward`**：它在每个 decoder 层里被调用一次，是 `Context` 最重的消费者。它读 `is_prefill`（分流）、`slot_mapping`（写 cache）、`cu_seqlens_q/k` + `max_seqlen_q/k`（prefill 的 varlen）、`context_lens` + `block_tables`（decode 的 paged 读）。
2. **`ParallelLMHead.forward`**：它在模型最末端被 `compute_logits` 调用一次，只读两个字段：`is_prefill` 与 `cu_seqlens_q`。但它对 `cu_seqlens_q` 的用法很巧妙——**在 prefill 时只取每条序列最后一个 token**去算 logits。

第二个用法正是本节的重点。为什么只取最后一个 token？因为 prefill 阶段虽然一次性算了整条 prompt 的每个 token 的隐状态，但「生成下一个 token」只需要**每条序列最后一个位置**的 logits（用它的分布去采样下一个 token）。中间 token 的 logits 对采样毫无用处，算它们纯属浪费。`ParallelLMHead` 利用 `cu_seqlens_q` 精确地把这些「末 token」挑出来，把 logits 计算量从「所有 token」降到「每序列一个」。decode 阶段每序列本来就只有 1 个 token，无需此优化。

#### 4.3.2 核心流程：末 token 下标的数学

设一个 batch 有 \(n\) 条序列，它们的 query 长度（`seqlen_q`）分别是 \(l_1, l_2, \dots, l_n\)。varlen 打包后，这些序列首尾相接成一个一维张量，总长 \(\sum_i l_i\)。累积长度数组：

\[
\text{cu\_seqlens\_q} = [\,0,\ c_1,\ c_2,\ \dots,\ c_n\,], \quad c_i = \sum_{j=1}^{i} l_j
\]

第 \(i\) 条序列（从 1 计数）占据一维张量的区间 \([c_{i-1},\ c_i)\)，其**最后一个 token** 的一维下标是 \(c_i - 1\)。于是「每条序列末 token 的下标」恰为：

\[
[\,c_1 - 1,\ c_2 - 1,\ \dots,\ c_n - 1\,] = \text{cu\_seqlens\_q}[1:] - 1
\]

这正是源码里那一行 `last_indices = context.cu_seqlens_q[1:] - 1` 的全部数学。它无需知道每条序列的长度，只需累积长度的「右端点」减一。

举例：两条序列，\(l_1=4,\ l_2=3\)。

\[
\text{cu\_seqlens\_q} = [0, 4, 7]
\]

末 token 下标：

\[
\text{cu\_seqlens\_q}[1:] - 1 = [4, 7] - 1 = [3, 6]
\]

即第 1 条序列末 token 在一维下标 3、第 2 条在 6——与「第 1 条占 [0,4) 末位 3、第 2 条占 [4,7) 末位 6」完全吻合。

#### 4.3.3 源码精读

先看 `Attention.forward`——`Context` 的主消费者。它第一件事就是 `get_context()`，之后所有元数据都来自这个 `context`：

[layers/attention.py:59-75 — Attention.forward 全貌：get_context 后按 is_prefill 分流](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L59-L75)

```python
def forward(self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor):
    context = get_context()
    k_cache, v_cache = self.k_cache, self.v_cache
    if k_cache.numel() and v_cache.numel():
        store_kvcache(k, v, k_cache, v_cache, context.slot_mapping)
    if context.is_prefill:
        if context.block_tables is not None:    # prefix cache
            k, v = k_cache, v_cache
        o = flash_attn_varlen_func(q, k, v,
                                   max_seqlen_q=context.max_seqlen_q, cu_seqlens_q=context.cu_seqlens_q,
                                   max_seqlen_k=context.max_seqlen_k, cu_seqlens_k=context.cu_seqlens_k,
                                   softmax_scale=self.scale, causal=True, block_table=context.block_tables)
    else:    # decode
        o = flash_attn_with_kvcache(q.unsqueeze(1), k_cache, v_cache,
                                    cache_seqlens=context.context_lens, block_table=context.block_tables,
                                    softmax_scale=self.scale, causal=True)
    return o
```

逐字段对应（这正是 u4-l1 构造、本讲传递、u4-l2 使用的闭环）：

- `context.slot_mapping` → `store_kvcache`：把当前层刚算出的 k/v 按 slot 写进本层的 `k_cache`/`v_cache`。两个阶段都用。
- `context.is_prefill` → 分流到 `flash_attn_varlen_func`（prefill）或 `flash_attn_with_kvcache`（decode）。
- prefill 分支：`context.max_seqlen_q/k`、`context.cu_seqlens_q/k`、`context.block_tables`（前缀缓存时）。
- decode 分支：`context.context_lens`（作 `cache_seqlens`）、`context.block_tables`。

注意 `Attention` 本身**没有任何可训练参数**（`__init__` 里 `k_cache = v_cache = torch.tensor([])`，由 `ModelRunner.allocate_kv_cache` 事后挂载本层视图，见 u3-l3）。它只是「读 Context + 调 flash-attn + 写 cache」的薄封装。这种「无参数 + 靠 Context 拿元数据」的设计，正是 Transformer 主体签名能保持 `forward(q, k, v)` 这么干净的原因。

再看 `ParallelLMHead.forward`——`Context` 的另一个消费者，也是本节重点的「末 token 优化」：

[layers/embed_head.py:56-66 — ParallelLMHead.forward：prefill 时用 cu_seqlens_q 取末 token](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L56-L66)

```python
def forward(self, x: torch.Tensor):
    context = get_context()
    if context.is_prefill:
        last_indices = context.cu_seqlens_q[1:] - 1
        x = x[last_indices].contiguous()
    logits = F.linear(x, self.weight)
    if self.tp_size > 1:
        all_logits = [torch.empty_like(logits) for _ in range(self.tp_size)] if self.tp_rank == 0 else None
        dist.gather(logits, all_logits, 0)
        logits = torch.cat(all_logits, -1) if self.tp_rank == 0 else None
    return logits
```

四步逐行：

1. `context = get_context()`：取本步元数据。
2. `if context.is_prefill:` **末 token 优化**：`last_indices = context.cu_seqlens_q[1:] - 1`，再用 `x[last_indices]` 把 `x` 从「所有 token 的隐状态」筛成「每序列末 token 的隐状态」。`.contiguous()` 保证切片后内存连续（`F.linear` 对连续张量更高效）。decode 时跳过此分支，因为 `x` 本来就每序列只有一行。
3. `logits = F.linear(x, self.weight)`：用（可能被筛过的）`x` 乘以词表权重，得到 logits。注意 pref​ill 优化后 `x` 的行数 = 序列数，而非 token 总数，**矩阵乘法的规模直接降了一个量级**。
4. `if self.tp_size > 1:` 张量并行收尾：词表按 rank 切分（见 u4-l5），各 rank 只算自己那段 logits，再 `dist.gather` 到 rank 0 拼成完整词表。这是 u5-l3 的内容，本讲只需知道它读的是 `is_prefill` 优化后的 `logits`。

`compute_logits` 把 `ParallelLMHead` 接到模型主干上，串联起这条侧信道：

[models/qwen3.py:212-216 — compute_logits 调用 lm_head（ParallelLMHead）](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L212-L216)

```python
def compute_logits(
    self,
    hidden_states: torch.Tensor,
) -> torch.Tensor:
    return self.lm_head(hidden_states)
```

`hidden_states` 是 `self.model(input_ids, positions)` 返回的「所有 token 的最后一层隐状态」。在 prefill 时，它被 `ParallelLMHead` 用 `cu_seqlens_q` 筛成「末 token 的隐状态」再算 logits。注意 `compute_logits` 的签名里**也没有** `cu_seqlens_q`——它通过 `ParallelLMHead` 内部 `get_context()` 拿到。整条链路 `run_model → compute_logits → lm_head.forward → get_context` 全程不改函数签名，这正是侧信道的威力。

把两个消费方对字段的读取汇总成一张表（本讲最值得记的一张表）：

| Context 字段 | Attention.forward | ParallelLMHead.forward | 备注 |
| --- | --- | --- | --- |
| `is_prefill` | ✓ 分流 varlen / with_kvcache | ✓ 是否做末 token 筛选 | 总开关 |
| `cu_seqlens_q` | ✓ prefill varlen 边界 | ✓ 末 token 下标 `[1:]-1` | prefill 专属 |
| `cu_seqlens_k` | ✓ prefill varlen 边界 | — | prefill 专属 |
| `max_seqlen_q` | ✓ flash 内核启动参数 | — | prefill 专属 |
| `max_seqlen_k` | ✓ flash 内核启动参数 | — | prefill 专属 |
| `slot_mapping` | ✓ store_kvcache 写 cache | — | 两阶段共用 |
| `context_lens` | ✓ decode 的 cache_seqlens | — | decode 专属 |
| `block_tables` | ✓ 读 paged cache | — | decode 必用、prefill 仅前缀缓存 |

这张表也回答了「为什么 prefill 不需要 `context_lens`、decode 不需要 `cu_seqlens`」——因为每个消费者只在对应分支读对应字段，互不越界。

#### 4.3.4 代码实践

**实践目标**：追踪一次 `run()` 中 `set_context` 设置的字段，分别说明它们被 `Attention.forward` 和 `ParallelLMHead.forward` 如何消费（即本讲指定的实践任务的下半部分）。

**操作步骤**：

1. 给两个消费者加探针，打印它们从 Context 读到的关键量（**示例代码，验证后删除**）。在 `attention.py` 的 `forward` 开头：

   ```python
   # 示例代码：Attention 消费探针
   def forward(self, q, k, v):
       context = get_context()
       print(f"[Attention] is_prefill={context.is_prefill} "
             f"q.shape={tuple(q.shape)} slot_mapping={None if context.slot_mapping is None else context.slot_mapping.shape} "
             f"block_tables={None if context.block_tables is None else context.block_tables.shape}")
       # ... 原逻辑 ...
   ```

   在 `embed_head.py` 的 `ParallelLMHead.forward` 开头：

   ```python
   # 示例代码：ParallelLMHead 消费探针
   def forward(self, x):
       context = get_context()
       if context.is_prefill:
           last_indices = context.cu_seqlens_q[1:] - 1
           print(f"[LMHead] prefill: x.shape={tuple(x.shape)} -> "
                 f"after_select={tuple(x[last_indices].shape)} last_indices={last_indices.tolist()}")
           x = x[last_indices].contiguous()
       else:
           print(f"[LMHead] decode: x.shape={tuple(x.shape)} (no selection)")
       # ... 原逻辑 ...
   ```

2. 跑 `example.py`，跑两个长度不同的 prompt（如 `example.py` 默认的两条）。

**需要观察的现象**：

- prefill 步（第一条序列被 prefill 时）：
  - `[Attention] is_prefill=True`，`q.shape` 第 0 维 = 该 prompt 的 token 数；`slot_mapping` 与 `q` 同长；`block_tables` 多数为 `None`（无前缀缓存）。
  - `[LMHead] prefill: x.shape=(L, H) -> after_select=(1, H)`，`last_indices=[L-1]`（单条序列时 `cu_seqlens_q=[0, L]`，末 token 下标 `L-1`）——**整条 prompt 的隐状态被筛成 1 行**。
- decode 步：
  - `[Attention] is_prefill=False`，`q.shape=(num_seqs, num_heads, head_dim)`，`block_tables` 形状 `(num_seqs, max_blocks)`。
  - `[LMHead] decode: x.shape=(num_seqs, H)`，不做筛选（每序列已是 1 行）。
- 若 batch 同时 prefill 多条序列：`after_select` 的第 0 维 = 序列数，`last_indices` 长度 = 序列数。

**手算小例**（教学示意）：设 batch 两条序列，`seqlen_q` 分别为 4、3，无前缀缓存。
- `cu_seqlens_q = [0, 4, 7]`。
- `last_indices = [4, 7] - 1 = [3, 6]`。
- `x` 形状 `(7, hidden)` → 筛后 `(2, hidden)`，即取第 3、6 行（分别是两条序列的末 token）。
- 验证：第 1 条占一维 [0,4)，末位 3；第 2 条占 [4,7)，末位 6。✓

**预期结果**：prefill 时 `LMHead` 打印的 `after_select` 第 0 维恰等于本步 batch 中的序列数；`last_indices` 的每个值都能由 `cu_seqlens_q` 还原。decode 时 `x` 行数 = 序列数、无筛选。具体数字依分词与序列数——**待本地验证**。

> 若无 GPU，可做「源码阅读型实践」：阅读本节引用的两段源码，对照「字段 → 消费者」表，逐行标注 `Attention.forward` 与 `ParallelLMHead.forward` 各读了 `Context` 的哪些字段，并解释为什么 decode 分支下 `ParallelLMHead` 不需要 `cu_seqlens_q`（因 `x` 每序列已是 1 行）。

#### 4.3.5 小练习与答案

**练习 1**：`last_indices = context.cu_seqlens_q[1:] - 1` 中，为什么用 `cu_seqlens_q` 而不是 `cu_seqlens_k`？
**答案**：因为筛选的是「隐状态 `x`」的行，而 `x` 是对 `input_ids`（即 query，新 token）做前向得到的，它的行数 = query 总数 = `cu_seqlens_q[-1]`。`cu_seqlens_k` 含缓存前缀的 key 数，在前缀缓存时大于 query 数，用它会导致下标越界或取错行。`ParallelLMHead` 要的是「每条序列最后一个**新算的** token」，对应 query 的末位，故用 `cu_seqlens_q`。

**练习 2**：如果 prefill 时不做这个「末 token」优化，直接对全部 token 算 logits，结果会错吗？
**答案**：数值上不会错（多算的中间 token logits 只是没人用），但会**严重浪费算力与显存**。prefill 的目的是「为每条序列预测下一个 token」，采样只需要末 token 的 logits。若对全部 token 算 logits，`F.linear` 的输入从「序列数行」膨胀到「token 总数行」，矩阵乘法规模大一个量级，且多算出的 logits 还占显存。所以这个优化是用 `cu_seqlens_q` 把计算量从 \(O(\text{token 总数} \cdot \text{词表})\) 压回 \(O(\text{序列数} \cdot \text{词表})\)。

**练习 3**：decode 阶段 `ParallelLMHead` 跳过了末 token 筛选，但 `Attention` 仍然读 `slot_mapping` 写 cache。这两者矛盾吗？
**答案**：不矛盾。`ParallelLMHead` 跳过筛选是因为 decode 每序列本就只有 1 个 token（`x` 行数 = 序列数），无需再筛；`Attention` 读 `slot_mapping` 是为了把这一个新 token 的 K/V 写进 cache 供**后续步**读。两者关心的是 `Context` 的不同字段、不同用途：`Attention` 管「读/写 cache」，`ParallelLMHead` 管「算 logits」。`Context` 把这些字段聚合在一个对象里，让两个消费者各取所需。

---

## 5. 综合实践

把本讲三块知识串起来：**绘制一张「一次 prefill 步」的 Context 全景图——从写入到两类消费者**。

**任务**：

1. 准备一个最小的 prefill 场景：batch 两条序列，`seqlen_q` 分别为 4、3（无前缀缓存）。在脑中（或纸上）列出 `prepare_prefill` 会传给 `set_context` 的全部参数值：
   - `is_prefill=True`
   - `cu_seqlens_q = [0, 4, 7]`、`cu_seqlens_k = [0, 4, 7]`（无前缀缓存，二者相等）
   - `max_seqlen_q = 4`、`max_seqlen_k = 4`
   - `slot_mapping` = 7 个槽位（按 u4-l1 的公式由 `block_table` 算出）
   - `context_lens = None`
   - `block_tables = None`（无前缀缓存）

2. 画一张「字段流向图」，左列为 `Context` 的 8 个字段，中列为字段值或形状，右列分两栏标注 `Attention.forward` 与 `ParallelLMHead.forward` 各用了哪些字段、用在哪一行。完成后应与 4.3.3 的「字段 → 消费者」表一致。

3. 接着改造场景为「命中前缀缓存」：第二条序列命中长度 2 的前缀，于是 `seqlen_q=3`、`seqlen_k=5`。重新算 `cu_seqlens_q = [0, 4, 7]`、`cu_seqlens_k = [0, 4, 9]`。指出：
   - `cu_seqlens_k[-1] (9) > cu_seqlens_q[-1] (7)` → `block_tables` 非 `None`。
   - `Attention` 此时会把 k/v 重指向整张 cache（`k, v = k_cache, v_cache`），用 `block_tables` 读前缀。
   - `ParallelLMHead` 的 `last_indices` 仍用 `cu_seqlens_q`：`[4,7]-1 = [3,6]`，不受前缀缓存影响（因为它筛的是 query，不是 key）。

4. 在真实代码上验证（需 GPU）：开启 4.2.4 与 4.3.4 的探针，跑 `example.py`，对照你画的图核对每一条字段值与消费者。

**产出**：一张覆盖「`ModelRunner.prepare_*` 写入 → `Context` 8 字段 → `Attention`/`ParallelLMHead` 读取」的全景图，并写明前缀缓存命中时哪些字段与读取行为发生变化。

## 6. 本讲小结

- `Context` 是 `ModelRunner` 与底层 attention 层之间的**侧信道**：用一个全局单例对象当消息板，让注意力元数据（`cu_seqlens`、`slot_mapping`、`block_tables`、`context_lens` 等）绕开函数参数传递，从而保持 Transformer 主干签名 `forward(input_ids, positions)` 干净、与标准实现对齐。
- `Context` 是带 `slots=True` 的 dataclass，8 个字段：`is_prefill`（总开关）、`cu_seqlens_q/k` + `max_seqlen_q/k`（prefill 专属）、`context_lens`（decode 专属）、`slot_mapping`（两阶段共用，写 cache）、`block_tables`（decode 必用、prefill 仅前缀缓存时用）。
- 生命周期严格遵循「每步一设一清」：`prepare_prefill`/`prepare_decode` 调 `set_context`（换一个新对象，未传字段取默认空值），前向期间各层 `get_context()` 只读，`run` 末尾 `reset_context()` 擦回白板兜底。写入权独占在 `ModelRunner`，读取方只读不写。
- `set_context` 选择「新建对象再赋值」而非「改字段」，保证每步 Context 是不可变快照、杜绝跨步残留与旧引用被就地改坏。
- `Attention.forward` 是 `Context` 的主消费者：读 `slot_mapping` 写 cache、按 `is_prefill` 分流到 `flash_attn_varlen_func`（用 `cu_seqlens`/`max_seqlen`）或 `flash_attn_with_kvcache`（用 `context_lens`/`block_tables`）；它本身无参数，全靠 Context 拿元数据。
- `ParallelLMHead.forward` 的 prefill 末 token 优化：用 `last_indices = context.cu_seqlens_q[1:] - 1` 把「所有 token 的隐状态」筛成「每序列末 token」，把 logits 计算从 \(O(\text{token 总数}\cdot\text{词表})\) 压回 \(O(\text{序列数}\cdot\text{词表})\)；decode 每序列已 1 行，无需筛选。

## 7. 下一步学习建议

本讲把「元数据如何到达底层」讲透了。建议接着阅读：

- **u4-l4 Qwen3 模型结构详解**：看 `Qwen3ForCausalLM → Qwen3Model → DecoderLayer → Attention` 这条调用链的每一层内部到底算了什么。本讲看到 `self.model(input_ids, positions)` 是一句调用，u4-l4 把它展开成 embed、多层 decoder、norm 的完整前向，你会更清楚 `Attention.forward` 与 `ParallelLMHead.forward` 在整条链路里的位置。
- **u4-l5 张量并行线性层与权重分片**：本讲提到 `ParallelLMHead` 在 `tp_size > 1` 时用 `dist.gather` 把各 rank 的 logits 汇总到 rank 0，u4-l5 会讲清词表如何按 rank 切分、`ColumnParallelLinear`/`RowParallelLinear` 如何配合。
- **u5-l1 CUDA Graph 捕获与回放**：本讲 4.2.3 点出了 `capture_cudagraph` 也用 `set_context`，以及图回放路径要「就地改 `graph_vars` 而非 `set_context`」这一约束。u5-l1 会完整解释为什么 CUDA Graph 要求张量身份固定、`Context` 在其中扮演什么角色。
- 回顾 **u4-l1** 的「字段流向图」与本讲的「字段 → 消费者」表，把它们合并成一张从 `Sequence` 到 `Context` 到 attention 内核的端到端映射，作为「模型执行」单元的总览。
