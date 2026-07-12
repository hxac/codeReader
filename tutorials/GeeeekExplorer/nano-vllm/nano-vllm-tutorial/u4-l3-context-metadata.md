# Context 元数据传递机制

## 1. 本讲目标

本讲解决一个贯穿整个模型执行层的问题：**每一步推理需要传给 Attention 的大量「注意力元数据」（写哪、读哪、序列边界、是否 prefill……）到底是怎么从调度器流到神经网络层里的？**

学完后你应当能够：

1. 说清楚为什么 nano-vllm 选择用一个**全局 `Context` 对象**来传递这些元数据，而不是把它们塞进函数签名。
2. 逐字段解释 `Context` 的八个字段各自含义、形状、以及在哪类阶段被使用。
3. 描述 `set_context` / `get_context` / `reset_context` 三个函数在一步推理中的生命周期，理解「先设置、再前向、后清空」的纪律。
4. 读懂 `Attention.forward` 与 `ParallelLMHead.forward` 是如何消费这些字段的，尤其是 prefill 时 LMHead「只取每序列最后一个 token」这一关键优化。

本讲是 u4-l1（ModelRunner 输入准备）与 u4-l2（Attention/Triton 内核）的承接：那两讲已经讲清楚 `slot_mapping`、`cu_seqlens_q/k`、`block_tables`、`context_lens` 这些张量**是什么、怎么算出来的**；本讲专门讲它们**用什么管道送到消费方手里**。

## 2. 前置知识

- **`nn.Module.forward` 的签名约束**：PyTorch 里一个层的计算入口是 `forward(...)`，子模块由父模块在它自己的 `forward` 里逐个调用。如果你想给深层的一个子模块（比如第 5 层的 Attention）传一个新参数，通常得让这条调用链上**每一层**的 `forward` 都多接一个参数并透传下去。
- **全局变量模式（global / singleton）**：在模块顶层定义一个对象，任何地方都能读写它。好处是「随用随取、不用透传」；坏处是隐式耦合（读方依赖一个看不见的全局状态）、不利于并发测试。nano-vllm 在这里刻意用了这个模式，并靠严格的「设置—清空」纪律来规避它的缺点。
- **varlen 打包**：多条不等长序列首尾相接拼成一维张量，用累计长度数组（`cu_seqlens`）标记每条的边界。这是 u4-l1 的核心概念，本讲直接复用。
- **prefill 与 decode 两阶段**：prefill 一次性算完 prompt（每序列多个 token），decode 每步每序列只算 1 个新 token。两者需要的元数据不同，`Context.is_prefill` 就是切换开关。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `nanovllm/utils/context.py` | 定义全局 `Context` 与三个访问函数 | 全部内容，本讲的核心 |
| `nanovllm/engine/model_runner.py` | 引擎执行器，`Context` 的**写端** | `prepare_prefill` / `prepare_decode` / `run` / `capture_cudagraph` 里对 `set_context` / `get_context` / `reset_context` 的调用 |
| `nanovllm/layers/attention.py` | Attention 层，`Context` 的**读端**之一 | `Attention.forward` 如何按 `is_prefill` 分流、如何用 `slot_mapping` / `cu_seqlens` / `block_tables` / `context_lens` |
| `nanovllm/layers/embed_head.py` | 词表嵌入与 LM Head，`Context` 的**读端**之二 | `ParallelLMHead.forward` 如何用 `cu_seqlens_q` 抽取每序列最后一个 token |
| `nanovllm/models/qwen3.py` | Qwen3 模型结构 | 仅用于观察：调用链上各 `forward` 的签名里**没有**任何注意力元数据，验证「走 Context 不走签名」 |

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要 Context：动机与取舍

#### 4.1.1 概念说明

一次 decode 步里，Attention 层要正确地读写 paged KV cache，至少需要这些信息：

- `slot_mapping`：本步新算出的 K/V 要写到 cache 的哪个物理槽位。
- `block_tables`：每条序列的历史 K/V 分散在哪些物理块里，attention 要去读。
- `context_lens`：每条序列当前总长，告诉 attention 往回读多远。
- `is_prefill`：走 varlen prefill 内核还是 paged decode 内核。

prefill 步还要多一组 `cu_seqlens_q` / `cu_seqlens_k` / `max_seqlen_q` / `max_seqlen_k`（varlen 打包的边界与最大长度）。这些值**每一步都在变**，因为每步调度的序列集合不同。

问题在于：这些信息**只有最顶层的 `ModelRunner` 知道**（是它根据 `Sequence` 列表算出来的），而**真正需要它们的却是埋在模型深处第 N 层的 `Attention`**。中间隔着 `Qwen3ForCausalLM.forward → Qwen3Model.forward → Qwen3DecoderLayer.forward → Qwen3Attention.forward → Attention.forward` 一长串调用。

有两种走法：

1. **透传参数**：给调用链上每一层的 `forward` 都加上这些参数。但 `Qwen3MLP`、`RMSNorm`、`VocabParallelEmbedding` 这些层根本用不到它们，却被迫在签名里挂着、一路往下传，签名臃肿、耦合严重。
2. **全局 `Context`**：在模块顶层放一个全局对象，`ModelRunner` 在前向前把它填好，任何一层在 `forward` 里 `get_context()` 随用随取，前向结束后清空。

nano-vllm 选了第 2 种。这是一个**刻意的设计取舍**：用「全局可变状态」这一通常被视为坏味道的模式，换取调用链签名的干净。它能成立，前提是推理主循环是严格单线程、单步串行的——每一步都是「设置 → 前向 → 清空」的封闭区间，不会有两个步的元数据互相串台。

#### 4.1.2 核心流程

```text
一步推理（ModelRunner.run）的 Context 生命周期：

  prepare_prefill / prepare_decode
        │
        ├── 计算各项张量（slot_mapping、cu_seqlens、block_tables…）
        └── set_context(...)          ← 把元数据写进全局 _CONTEXT
        │
        ▼
  model 前向（model.forward → ... → Attention.forward / LMHead.forward）
        │
        └── 每个需要的层 get_context()  ← 从全局 _CONTEXT 读元数据
        │
        ▼
  reset_context()                     ← 清空，回到默认空 Context
```

关键直觉：`Context` 是一个**只活一步**的临时信使。它在前向开始前被装满，前向中被层层查阅，前向一结束就被清空。没有任何跨步的状态残留。

#### 4.1.3 源码精读

先看调用链上的签名，确认元数据**没有**出现在任何 `forward` 参数里。

`Qwen3ForCausalLM.forward` 只接收 `input_ids` 和 `positions`（[nanovllm/models/qwen3.py:205-210](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L205-L210)），`Qwen3DecoderLayer.forward` 只多一个 `residual`（[nanovllm/models/qwen3.py:146-151](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L146-L151)）。全链路里找不到 `slot_mapping`、`cu_seqlens`、`block_tables` 这些名字——它们根本不在签名里。

再看 `Attention.forward` 怎么拿到这些值的：第一行就是 `context = get_context()`（[nanovllm/layers/attention.py:60](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L60)），此后所有元数据都从 `context` 这个局部变量取。这就是「走 Context 不走签名」的实证。

#### 4.1.4 代码实践

**实践目标**：亲手验证「注意力元数据走全局 `Context`，而非函数参数」。

**操作步骤**：

1. 打开 `nanovllm/models/qwen3.py`，记录 `Qwen3ForCausalLM.forward`、`Qwen3Model.forward`、`Qwen3DecoderLayer.forward`、`Qwen3Attention.forward` 四个函数的形参列表。
2. 打开 `nanovllm/layers/attention.py`，记录 `Attention.forward` 的形参。
3. 用搜索工具（`Grep`）在整个 `nanovllm/` 目录里搜 `slot_mapping`、`cu_seqlens_q`，统计它们作为「函数形参」出现的次数 vs 作为「`context.xxx` 属性访问」出现的次数。

**需要观察的现象**：这些元数据名字**从不**作为模型层 `forward` 的形参出现，只出现在 `ModelRunner.prepare_*`（写入侧）和 `Attention.forward` / `ParallelLMHead.forward` 内部对 `context` 的属性访问里。

**预期结果**：你会在 `model_runner.py` 看到它们作为局部变量被构造并塞进 `set_context`，在 `attention.py` / `embed_head.py` 看到 `context.xxx` 的读取，而在 `qwen3.py` 的所有 `forward` 签名里一无所获。

#### 4.1.5 小练习与答案

**练习 1**：如果改用「参数透传」方案，`Qwen3MLP.forward` 需不需要接收 `slot_mapping`？为什么？

> **答**：逻辑上不需要，MLP 根本不碰 KV cache。但透传方案下，为了让更深层的 `Attention` 拿到 `slot_mapping`，调用链每一层（含 MLP）都得在签名里挂上它并往下传——这正是 nano-vllm 用全局 `Context` 想避免的污染。

**练习 2**：全局可变状态通常被批评为「不利于并发」。为什么 nano-vllm 这里可以接受？

> **答**：因为推理主循环是严格单步串行的，且每步都用 `reset_context()` 收尾，`_CONTEXT` 在任一时刻只反映当前这一步的元数据，不存在多步并发读写同一全局的问题。多进程张量并行时，每个进程有自己独立的 `_CONTEXT`（见 4.3.4）。

---

### 4.2 Context 数据类：八字段速查表

#### 4.2.1 概念说明

`Context` 是一个用 `@dataclass(slots=True)` 定义的轻量数据类（与 `Config`、`SamplingParams` 同一种写法）。它本身**不持有任何可训练参数、不分配显存**，只是八个字段的容器：一个布尔标志、四个「张量或 None」、两个整数、再一个「张量或 None」。它的全部价值在于：把这些异构的元数据**打包成一个对象**，方便整体设置、整体清空。

#### 4.2.2 核心流程

字段一览（按源码声明顺序）：

| 字段 | 类型 | 默认值 | 含义 | prefill 用 | decode 用 |
| --- | --- | --- | --- | --- | --- |
| `is_prefill` | `bool` | `False` | 阶段开关：True 走 varlen prefill 内核，False 走 paged decode 内核；同时控制 LMHead 是否抽取最后一个 token | ✅ | ✅ |
| `cu_seqlens_q` | `Tensor \| None` | `None` | 本步**新算的 Q** 的累计序列长度，形状 `(num_seqs+1,)`，int32 | ✅ | ❌（None） |
| `cu_seqlens_k` | `Tensor \| None` | `None` | **全部 K**（含缓存前缀）的累计序列长度，形状 `(num_seqs+1,)`，int32；`cu_seqlens_k > cu_seqlens_q` 即命中前缀缓存 | ✅ | ❌（None） |
| `max_seqlen_q` | `int` | `0` | 本步新 token 中最长的序列长度，供 flash attention 内核选 launch 配置 | ✅ | ❌（0） |
| `max_seqlen_k` | `int` | `0` | 全部 K 中最长的序列长度，同上 | ✅ | ❌（0） |
| `slot_mapping` | `Tensor \| None` | `None` | 每个新 token 的 K/V 要写入的物理槽位 `slot = block_id*block_size + 偏移`；prefill 形状 `(总新token数,)`，decode 形状 `(num_seqs,)`；值为 `-1` 表示跳过（CUDA Graph 填充位） | ✅ | ✅ |
| `context_lens` | `Tensor \| None` | `None` | 每条序列当前总长（历史长度），告诉 decode attention 往回读多远，形状 `(num_seqs,)`，int32 | ❌（None） | ✅ |
| `block_tables` | `Tensor \| None` | `None` | 每条序列的物理块号表，形状 `(num_seqs, max_num_blocks)`，int32；`-1` 为填充；prefill 仅在命中前缀缓存时才设置 | 视情况 | ✅ |

几个要点：

- 同一个字段在不同阶段语义一致：`slot_mapping` 永远表示「新 K/V 写哪里」，`block_tables` 永远表示「历史 K/V 从哪些块读」。
- prefill 与 decode 是**互补**的：prefill 不需要 `context_lens`（因为它通过 `cu_seqlens_k` 自己描述了全部 K 的范围），decode 不需要 `cu_seqlens_*`（因为每序列就 1 个 token，没有「打包边界」可言）。
- `block_tables` 在 prefill 中**条件性**出现：只有当前缀缓存命中、`cu_seqlens_k > cu_seqlens_q` 时才设置（见 u4-l1 / u4-l2）。这时 attention 要从 paged cache 里读出缓存的前缀 K/V，必须知道块号。

#### 4.2.3 源码精读

数据类定义见 [nanovllm/utils/context.py:5-14](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L5-L14)：

```python
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
```

注意几个细节：

- `slots=True` 禁止运行时新增属性，既省内存，也防止拼写错误地写入一个不存在的字段（比如 `context.solt_mapping=...`）而不报错。
- 所有字段都有默认值，因此 `Context()`（无参构造）会得到一个「全空」对象：`is_prefill=False`、所有张量为 `None`。这正是 `reset_context()` 想要的干净状态。
- 类型用 `torch.Tensor | None`，明确表示「这个阶段可能没有这个字段」，读端必须容忍 `None`。

#### 4.2.4 代码实践

**实践目标**：在不跑模型的前提下，亲手构造 `Context` 并观察字段默认值与赋值效果（纯 CPU 即可）。

**操作步骤**：

1. 在仓库根目录起一个 Python REPL（无需 GPU）。
2. 执行：
   ```python
   import torch
   from nanovllm.utils.context import Context, get_context, set_context, reset_context

   c = Context()
   print(c)                          # 观察默认值
   print(get_context().is_prefill)   # 模块初始全局对象
   ```
3. 手工塞入一组假张量：
   ```python
   cu = torch.tensor([0, 5, 12, 20], dtype=torch.int32)
   sm = torch.arange(20, dtype=torch.int32)
   set_context(True, cu, cu, 8, 20, sm, None, None)
   ctx = get_context()
   print(ctx.is_prefill, ctx.cu_seqlens_q.tolist(), ctx.slot_mapping.tolist())
   reset_context()
   print(get_context().slot_mapping)   # 应回到 None
   ```

**需要观察的现象**：`set_context` 之后 `get_context()` 拿到的是新填的对象；`reset_context()` 之后所有张量字段变回 `None`、`is_prefill` 变回 `False`。

**预期结果**：第三次打印输出 `None`，证明 reset 确实清空了全局状态。

#### 4.2.5 小练习与答案

**练习 1**：decode 阶段调用 `set_context(False, slot_mapping=..., context_lens=..., block_tables=...)` 时，`cu_seqlens_q` 会是什么值？为什么 decode 不需要它？

> **答**：会是默认值 `None`。decode 每序列每步只算 1 个 token，不存在「多条不等长序列打包」的边界问题，因此不需要 `cu_seqlens_*`；decode 内核 `flash_attn_with_kvcache` 靠 `context_lens` 与 `block_tables` 直接定位历史。

**练习 2**：为什么 `block_tables` 在 prefill 阶段有时是 `None`、有时又不是？

> **答**：prefill 默认直接用本步新算出的 K/V 做 attention（无前缀缓存时 `cu_seqlens_k == cu_seqlens_q`），不需要查物理块，故 `block_tables=None`；一旦命中前缀缓存（`cu_seqlens_k > cu_seqlens_q`），attention 要去 paged cache 读缓存的前缀 K/V，就必须知道块号，于是 `prepare_prefill` 会调用 `prepare_block_tables` 生成它（见 u4-l1）。

---

### 4.3 生命周期：set / get / reset 与 ModelRunner 的写入时序

#### 4.3.1 概念说明

光有数据类还不够，还要有三个访问函数把它的生命周期管起来：

- `set_context(...)`：**写入**。用传入的参数新建一个 `Context` 对象，替换掉全局的 `_CONTEXT`。注意它是「整体替换」而非「逐字段修改」——每次都造一个全新对象。
- `get_context()`：**读取**。返回当前的全局 `_CONTEXT`。任何消费方在前向里调它来拿元数据。
- `reset_context()`：**清空**。把 `_CONTEXT` 替换成一个全默认值的空 `Context()`，为下一步腾出干净状态。

这三者构成一个严格的「开—用—关」括号，由 `ModelRunner` 负责合上。

#### 4.3.2 核心流程

一步 `ModelRunner.run` 中的时序：

```text
run(seqs, is_prefill):
  1. prepare_prefill(seqs)  ──┐
     或 prepare_decode(seqs)   │ 内部末尾调用 set_context(...)
                              ─┘  → 全局 _CONTEXT 被填满
  2. run_model(input_ids, positions, is_prefill)
     └─ model 前向：Attention/LMHead 各自 get_context() 读元数据
        （decode 走 CUDA Graph 时，run_model 自己也 get_context()
         把元数据拷进静态 graph_vars，详见 4.3.4）
  3. sampler(logits, temperatures)   ← 与 Context 无关
  4. reset_context()                 → 全局 _CONTEXT 清空
```

两条写入路径：

- **prefill 路径**（`prepare_prefill`）：`set_context(True, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, None, block_tables)`，按位置参数填满七个槽，`context_lens` 显式传 `None`，`block_tables` 视前缀缓存而定。
- **decode 路径**（`prepare_decode`）：`set_context(False, slot_mapping=..., context_lens=..., block_tables=...)`，用关键字参数只填 decode 需要的三个，其余保持默认（`cu_seqlens_*=None`、`max_seqlen_*=0`）。

无论哪条路径，`run` 的最后一步永远是 `reset_context()`。

#### 4.3.3 源码精读

三个函数的定义极简，见 [nanovllm/utils/context.py:16-27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L16-L27)：

```python
_CONTEXT = Context()                      # 模块级全局，初始为空

def get_context():
    return _CONTEXT

def set_context(is_prefill, cu_seqlens_q=None, ...):
    global _CONTEXT
    _CONTEXT = Context(is_prefill, cu_seqlens_q, ...)   # 整体替换

def reset_context():
    global _CONTEXT
    _CONTEXT = Context()                   # 换回全默认空对象
```

注意 `set_context` / `reset_context` 都用了 `global _CONTEXT` 声明，因为它们要**重新绑定**模块级变量（而不是修改对象内部）。这是 Python 里「替换全局对象」的标准写法。

写端的两次调用在 `ModelRunner` 里。prefill 的填入见 [nanovllm/engine/model_runner.py:169](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L169)：

```python
set_context(True, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, None, block_tables)
```

decode 的填入见 [nanovllm/engine/model_runner.py:187](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L187)：

```python
set_context(False, slot_mapping=slot_mapping, context_lens=context_lens, block_tables=block_tables)
```

而「合上括号」的清空在 `run` 的末尾，[nanovllm/engine/model_runner.py:219](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L219)（`reset_context()`），紧跟在采样之后、`return` 之前。把 `run` 的完整骨架列出来看最清楚（[nanovllm/engine/model_runner.py:214-220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)）：

```python
def run(self, seqs, is_prefill):
    input_ids, positions = self.prepare_prefill(seqs) if is_prefill else self.prepare_decode(seqs)
    temperatures = self.prepare_sample(seqs) if self.rank == 0 else None
    logits = self.run_model(input_ids, positions, is_prefill)
    token_ids = self.sampler(logits, temperatures).tolist() if self.rank == 0 else None
    reset_context()
    return token_ids
```

#### 4.3.4 CUDA Graph 与多进程下的 Context

有两处进阶场景也依赖 `Context`，这里点到为止，细节留给后续讲义：

1. **CUDA Graph 捕获/回放**（详见 u5-l1）。捕获时，`capture_cudagraph` 对每个 batch 档位调用 `set_context(False, slot_mapping=..., context_lens=..., block_tables=...)` 设置一组**静态**元数据（[nanovllm/engine/model_runner.py:240](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L240)），捕获完再 `reset_context()`（[nanovllm/engine/model_runner.py:248](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L248)）。真正 decode 回放时，`run_model` 不在前向里读 `Context`，而是**在前向之外**用 `get_context()` 把当前步的 `slot_mapping` / `context_lens` / `block_tables` 拷进静态 `graph_vars` 缓冲区（[nanovllm/engine/model_runner.py:201-210](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L201-L210)），然后 `graph.replay()`。也就是说：图内的 Attention 在捕获时绑定的是静态张量的指针，图外靠 `get_context()` 往这些指针里填新内容。

2. **多进程张量并行**（详见 u5-l3）。`_CONTEXT` 是**进程级**全局，每个 worker 进程各有一份。因为 `run` 在每个 rank 上都被独立调用，每个 rank 各自跑 `prepare_*` 并 `set_context`，而调度元数据（块布局、`cu_seqlens`）在所有 rank 上完全一致，所以各进程的 `Context` 内容等价，无需跨进程同步。

#### 4.3.5 代码实践

**实践目标**：把写端的两个 `set_context` 调用画成一张「字段来源表」，并解释 warmup 时的边界情况。

**操作步骤**：

1. 阅读 [nanovllm/engine/model_runner.py:129-170](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L129-L170)（`prepare_prefill`），逐个写出 `cu_seqlens_q`、`cu_seqlens_k`、`max_seqlen_q`、`max_seqlen_k`、`slot_mapping`、`block_tables` 是由哪个局部变量、哪段循环算出来的。
2. 同样阅读 [nanovllm/engine/model_runner.py:172-188](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L172-L188)（`prepare_decode`），列出 decode 只填了哪三个字段。
3. 阅读 `warmup_model`（[nanovllm/engine/model_runner.py:91-101](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L91-L101)）：它构造的 `Sequence([0]*seq_len)` 没有 `block_table`。结合 `prepare_prefill` 里 `if not seq.block_table: continue`（[nanovllm/engine/model_runner.py:149](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L149)），解释 warmup 时 `slot_mapping` 与 `block_tables` 分别是什么。

**需要观察的现象**：warmup 时 `slot_mapping` 是空列表、`block_tables` 是 `None`；而此时 Attention 的 `k_cache`/`v_cache` 还没分配（仍是 `torch.tensor([])`）。

**预期结果**：你能解释为什么 warmup 这次 prefill 不会真的写 KV cache——因为 `Attention.forward` 里有 `if k_cache.numel() and v_cache.numel():` 这道守卫（见 4.4.3），空 cache 时 `store_kvcache` 被跳过。warmup 只是为了触发算子、测量激活峰值（u3-l3）。

#### 4.3.6 小练习与答案

**练习 1**：如果把 `run` 末尾的 `reset_context()` 删掉，下一步会发生什么？

> **答**：全局 `_CONTEXT` 会残留上一步的元数据。下一步前向开始前虽然会被新的 `set_context` 覆盖大部分字段，但万一某条新路径漏掉了某个字段（例如某个分支没设置 `block_tables`），就会读到上一步的脏值，产生难以排查的隐式 bug。`reset_context` 是一道「归零」保险。

**练习 2**：`set_context` 为什么选择「整体替换对象」而不是「逐个给 `_CONTEXT` 的属性赋值」？

> **答**：整体替换更安全、更清晰——一次调用要么全部生效、要么全不生效，不会出现「半个字段更新完」的中间态被别的读取方看到；同时与 `reset_context`（替换成空对象）对称，心智模型统一。

---

### 4.4 读端消费：Attention 与 ParallelLMHead 如何用 Context

#### 4.4.1 概念说明

`Context` 有两个读者，都在神经网络层里：

- **`Attention.forward`**：消费几乎所有字段。它要做两件事——把新算的 K/V 写进 paged cache（用 `slot_mapping`），再做注意力计算（prefill 用 `cu_seqlens_*`，decode 用 `context_lens` + `block_tables`），用 `is_prefill` 在两条内核间切换。
- **`ParallelLMHead.forward`**：只消费两个字段——`is_prefill` 与 `cu_seqlens_q`。它做的是一个优雅的优化：prefill 时**只算每条序列最后一个 token 的 logits**，其余 token 的 logits 直接不算，省下巨量线性层计算。

#### 4.4.2 核心流程

**Attention 的消费分流**：

```text
context = get_context()
if k_cache/v_cache 已分配:
    store_kvcache(k, v, k_cache, v_cache, context.slot_mapping)   # 写：新 K/V → 槽位
if context.is_prefill:
    if context.block_tables is not None:   # 命中前缀缓存
        k, v = k_cache, v_cache            # 改读整张 cache（前缀+刚写入后缀）
    o = flash_attn_varlen_func(q, k, v,
            max_seqlen_q, cu_seqlens_q, max_seqlen_k, cu_seqlens_k,
            block_table=context.block_tables)                       # prefill 内核
else:   # decode
    o = flash_attn_with_kvcache(q.unsqueeze(1), k_cache, v_cache,
            cache_seqlens=context.context_lens,
            block_table=context.block_tables)                       # decode 内核
```

**ParallelLMHead 的「只取最后一个 token」优化**：

prefill 时，varlen 打包后的隐状态 `x` 包含**所有序列的所有 token**。但采样只需要每条序列**最后一个 token** 的 logits（只有它负责预测下一个 token）。设 `cu_seqlens_q = [c_0, c_1, …, c_n]`（`c_0=0`），则序列 `i` 在打包张量里占据区间 \([c_{i-1}, c_i)\)，其最后一个 token 的下标是 \(c_i - 1\)。于是所有序列的末 token 下标为：

\[
\text{last\_indices} = [\,c_1-1,\ c_2-1,\ \ldots,\ c_n-1\,] = \text{cu\_seqlens\_q}[1:] - 1
\]

用 `x[last_indices]` 抽出这 `num_seqs` 行，再做 `F.linear(x, weight)` 算 logits。计算量从「总 token 数 × 词表」降到「序列数 × 词表」——当 prompt 很长、batch 较小时，这是数量级的节省。

decode 时 `x` 本来就每序列一行，无需抽取。

#### 4.4.3 源码精读

**Attention.forward**，[nanovllm/layers/attention.py:59-75](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L59-L75)：

```python
def forward(self, q, k, v):
    context = get_context()
    k_cache, v_cache = self.k_cache, self.v_cache
    if k_cache.numel() and v_cache.numel():
        store_kvcache(k, v, k_cache, v_cache, context.slot_mapping)   # 消费 slot_mapping
    if context.is_prefill:                                             # 消费 is_prefill
        if context.block_tables is not None:    # prefix cache         # 消费 block_tables（条件）
            k, v = k_cache, v_cache
        o = flash_attn_varlen_func(q, k, v,
                max_seqlen_q=context.max_seqlen_q, cu_seqlens_q=context.cu_seqlens_q,   # 消费 4 个字段
                max_seqlen_k=context.max_seqlen_k, cu_seqlens_k=context.cu_seqlens_k,
                softmax_scale=self.scale, causal=True, block_table=context.block_tables)
    else:    # decode
        o = flash_attn_with_kvcache(q.unsqueeze(1), k_cache, v_cache,
                cache_seqlens=context.context_lens, block_table=context.block_tables,   # 消费 context_lens + block_tables
                softmax_scale=self.scale, causal=True)
    return o
```

字段到行号的对应（均在同一函数内）：

- `context.slot_mapping` → 写 K/V 的槽位（[L63](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L63)）。
- `context.is_prefill` → 内核分支开关（[L64](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L64)）。
- `context.block_tables` → prefill 命中前缀时改读 cache（[L65-L66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L65-L66)），并传给两个 flash 内核（[L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L70)、[L73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L73)）。
- `context.max_seqlen_q` / `cu_seqlens_q` / `max_seqlen_k` / `cu_seqlens_k` → prefill 内核的边界参数（[L68-L69](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L68-L69)）。
- `context.context_lens` → decode 内核读取的历史长度（[L73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L73)）。

注意 `store_kvcache` 的守卫 `if k_cache.numel() and v_cache.numel():`（[L62](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L62)）：Attention 在 `__init__` 时把 `k_cache`/`v_cache` 初始化为空张量 `torch.tensor([])`（[L57](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L57)），直到 `allocate_kv_cache` 才挂上真实视图（见 u3-l3）。warmup 发生在分配之前，这道守卫正是为了避免 warmup 时往空 cache 里写。

**ParallelLMHead.forward**，[nanovllm/layers/embed_head.py:56-66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L56-L66)：

```python
def forward(self, x):
    context = get_context()
    if context.is_prefill:                                  # 消费 is_prefill
        last_indices = context.cu_seqlens_q[1:] - 1         # 消费 cu_seqlens_q，算末 token 下标
        x = x[last_indices].contiguous()                    # 只保留每序列最后一个 token
    logits = F.linear(x, self.weight)                       # 只对这 num_seqs 行算 logits
    if self.tp_size > 1:                                    # 张量并行 gather（u4-l5）
        all_logits = [torch.empty_like(logits) for _ in range(self.tp_size)] if self.tp_rank == 0 else None
        dist.gather(logits, all_logits, 0)
        logits = torch.cat(all_logits, -1) if self.tp_rank == 0 else None
    return logits
```

`ParallelLMHead` 继承自 `VocabParallelEmbedding`（[L45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L45)），复用其词表分片权重 `self.weight` 与 `weight_loader`，张量并行的细节（词表按 `tp_size` 切分、`gather` 回收）留给 u4-l5。

#### 4.4.4 代码实践

**实践目标**：手算 `ParallelLMHead` 的末 token 抽取，确认它与 `cu_seqlens_q` 的对应关系。

**操作步骤**：

1. 假设一个 prefill 步打包了 3 条序列，新 token 数分别为 5、7、8，即 `cu_seqlens_q = [0, 5, 12, 20]`。
2. 手算 `last_indices = cu_seqlens_q[1:] - 1`。
3. 写一小段 CPU 代码验证：
   ```python
   import torch
   cu_seqlens_q = torch.tensor([0, 5, 12, 20], dtype=torch.int32)
   last_indices = cu_seqlens_q[1:] - 1
   print(last_indices.tolist())          # 期望 [4, 11, 19]
   x = torch.arange(20*4).reshape(20, 4) # 假装是 20 个 token 的隐状态
   y = x[last_indices]                    # 抽出 3 行
   print(y.shape)                         # 期望 torch.Size([3, 4])
   ```
4. 对照 [nanovllm/layers/embed_head.py:57-61](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L57-L61)，确认逻辑一致。

**需要观察的现象**：`last_indices` 恰好是每条序列在打包张量里的最后一个位置；抽取后行数等于序列数 `num_seqs`。

**预期结果**：输出 `[4, 11, 19]` 与 `torch.Size([3, 4])`。

#### 4.4.5 小练习与答案

**练习 1**：decode 步里 `ParallelLMHead` 还会执行 `x = x[last_indices]` 吗？为什么？

> **答**：不会。decode 时 `context.is_prefill` 为 `False`，整个 `if context.is_prefill:` 块被跳过。decode 每序列本来就只有 1 个 token，`x` 已经是 `(num_seqs, hidden)`，无需抽取。

**练习 2**：在命中前缀缓存的 prefill 中，`cu_seqlens_k` 会大于 `cu_seqlens_q`。`ParallelLMHead` 用的是哪一个？为什么不会出错？

> **答**：用的是 `cu_seqlens_q`。因为隐状态 `x` 是按「本步新算的 token」打包的（对应 `cu_seqlens_q`），缓存的前缀 token 并不在 `x` 里。我们要的是每条序列「最后一个新 token」的 logits，它正是 `cu_seqlens_q[i] - 1` 处那一行。若误用 `cu_seqlens_k`，下标会越过 `x` 的长度而越界。

**练习 3**：`Attention.forward` 在前缀缓存命中时把 `k, v = k_cache, v_cache`（[L66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L66)），随后把 `context.block_tables` 传给 `flash_attn_varlen_func`。这两步配合解决了什么问题？

> **答**：前缀缓存的 K/V 物理上躺在 paged cache 的若干块里，不在本地 `k`/`v` 张量中。把 `k,v` 重指向整张 cache、并配上 `block_tables`，flash attention 才能按块号去 cache 里读出「前缀 + 刚由 `store_kvcache` 写入的后缀」，拼出完整的 key/value 参与注意力（详见 u4-l2）。

---

## 5. 综合实践

本任务对应本讲规格里的实践要求：**追踪一次 `run()` 中 `set_context` 设置的字段，分别说明它们被 `Attention.forward` 和 `ParallelLMHead.forward` 如何消费**。

**实践目标**：用插桩的方式，把一次 prefill + 一次 decode 中 `Context` 的「写入—读取」全过程落到一张表里，验证你对字段流向的理解。

**操作步骤**：

1. 在 `nanovllm/utils/context.py` 的 `set_context` 与 `get_context` 里临时加打印（**仅为观察，勿提交**）。例如：
   ```python
   def set_context(is_prefill, cu_seqlens_q=None, ...):
       global _CONTEXT
       _CONTEXT = Context(...)
       import traceback
       print(f"[set_context] is_prefill={is_prefill} "
             f"from={traceback.extract_stack()[-3].name}")
   ```
   （`get_context` 同理打印调用方名；可在 `Attention.forward` / `ParallelLMHead.forward` 里临时打印 `context.is_prefill`、`context.slot_mapping.shape` 等字段。）
2. 用 `example.py` 跑一次最小推理（1～2 条短 prompt，`max_tokens` 设小一些）。
3. 针对其中一次 prefill 步和一次 decode 步，填写下表：

   | 字段 | prefill 步的值（形状/示例） | decode 步的值 | 写入方 | `Attention` 怎么用 | `ParallelLMHead` 怎么用 |
   | --- | --- | --- | --- | --- | --- |
   | `is_prefill` | `True` | `False` | `prepare_*` | 选 `flash_attn_varlen_func` | 决定是否抽取末 token |
   | `cu_seqlens_q` | `(num_seqs+1,)` | `None` | `prepare_prefill` | varlen 边界 | `last_indices = [1:]-1` |
   | `slot_mapping` | `(总新token数,)` | `(num_seqs,)` | `prepare_*` | `store_kvcache` 写槽位 | 不用 |
   | `block_tables` | 命中前缀时 `(num_seqs,max_blocks)`，否则 `None` | `(num_seqs,max_blocks)` | `prepare_*` | 读历史 K/V | 不用 |
   | `context_lens` | `None` | `(num_seqs,)` | `prepare_decode` | decode 读历史长度 | 不用 |

4. 在 decode 步观察 `run_model` 走的是 eager 还是 CUDA Graph 分支（取决于 `enforce_eager`）。若走 Graph，确认 `get_context()` 是在 `graph.replay()` **之前**被调用、把元数据拷进 `graph_vars` 的（[L201-L210](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L201-L210)）。

**需要观察的现象**：每次 `model` 前向前都有恰好一次 `set_context`；前向后必有 `reset_context`；`Attention` 与 `ParallelLMHead` 对 `Context` 的读取都发生在前向内部。

**预期结果**：你能用一句话说清每个字段「谁写、谁读、读去做什么」，并把 `slot_mapping`（写 K/V）、`block_tables`（读历史 K/V）、`cu_seqlens_q`（varlen 边界 + 末 token 下标）这三者的角色区分清楚。

> 说明：本实践需要 GPU 环境与模型权重才能真正运行 `example.py`；插桩与填表部分可先在源码层面完成推演，**实际运行结果待本地验证**。若暂无 GPU，可只做步骤 1、3 的源码阅读与手算，跳过步骤 2 的实跑。

## 6. 本讲小结

- nano-vllm 用一个**模块级全局 `Context`** 在 `ModelRunner`（写端）与 `Attention` / `ParallelLMHead`（读端）之间传递注意力元数据，目的是不让这些每步都变的张量污染整条 `forward` 调用链的签名。
- `Context` 是 `@dataclass(slots=True)`，含八个字段：`is_prefill`、`cu_seqlens_q/k`、`max_seqlen_q/k`、`slot_mapping`、`context_lens`、`block_tables`；prefill 与 decode 各用其中互补的子集。
- 生命周期由三个函数管理：`set_context` 整体替换全局对象，`get_context` 返回当前对象，`reset_context` 清空回默认。`ModelRunner.run` 严格按「prepare→set → forward(get) → reset」封口，每步不留残留。
- `Attention.forward` 消费几乎所有字段：`slot_mapping` 写 K/V，`is_prefill` 切内核，prefill 用 `cu_seqlens_*`（命中前缀时改读 cache 并用 `block_tables`），decode 用 `context_lens` + `block_tables`。
- `ParallelLMHead.forward` 只用 `is_prefill` 和 `cu_seqlens_q`：prefill 时用 `cu_seqlens_q[1:] - 1` 抽取每序列最后一个 token，把 logits 计算从「总 token 数」降到「序列数」，是一项兼具正确性与性能的优化。
- `Context` 的设计能成立，靠的是推理主循环单步串行 + 每步 `reset`；在多进程张量并行下，每个 rank 各持一份等价的 `Context`，无需跨进程同步。

## 7. 下一步学习建议

- **向「优化」走（u5-l1 CUDA Graph）**：本讲提到 decode 回放时 `run_model` 用 `get_context()` 把元数据拷进静态 `graph_vars`。下一讲会完整讲清 `capture_cudagraph` 的捕获流程与 `graph_bs` 分档，你会看到 `Context` 在静态图场景下的特殊用法。
- **向「采样」走（u5-l2 / Sampler）**：`ParallelLMHead` 产出的 logits 进入 `Sampler`。建议阅读 `nanovllm/layers/sampler.py`，看它如何基于指数分布对 logits 做一步采样（这也是 `SamplingParams` 禁止 `temperature=0` 的根源）。
- **向「张量并行」走（u4-l5 / u5-l3）**：本讲的 `ParallelLMHead` 已经露出了词表分片与 `gather` 的尾巴。可继续读 `nanovllm/layers/linear.py` 与 `embed_head.py`，理解 `VocabParallelEmbedding` 如何按 `tp_size` 切词表，以及多进程运行时如何协同。
- **回头巩固**：若对 `slot_mapping`、`cu_seqlens`、`block_tables` 的具体构造还有疑问，建议重读 u4-l1（输入准备）与 u4-l2（Attention/Triton 内核），它们是本讲字段含义的真正来源。
