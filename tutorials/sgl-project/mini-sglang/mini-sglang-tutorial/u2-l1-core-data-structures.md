# 核心数据结构 Req/Batch/Context

## 1. 本讲目标

本讲我们要认识 Mini-SGLang 里「最底层、却被所有人用到」的四个数据类：`SamplingParams`、`Req`、`Batch`、`Context`。它们全部定义在一个不到 140 行的文件 [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) 里，却像血管一样贯穿调度器、引擎、注意力后端、KV cache、模型层。

学完本讲，你应该能够：

- 说出 `Req` 上每个长度字段（`cached_len` / `device_len` / `max_device_len` / `extend_len` / `remain_len`）的含义，以及它们之间恒成立的不等式；
- 手动推演一个请求从「构造 → prefill → 一次次 decode → 结束」时，这些长度字段如何随 `complete_one` 与 `append_host` 一步步变化；
- 区分 `Batch` 的 `prefill` 与 `decode` 两个阶段，并说清楚 `Batch` 上哪些字段由调度器填、哪些由注意力后端填；
- 解释 `Context` 作为「进程级全局状态持有者」+「当前 batch 上下文管理器」的双重身份，并理解它为何用模块级单例 `_GLOBAL_CTX` 暴露。

本讲是后续所有讲义的「公共词汇表」——[u4 调度器](u4-l1-scheduler-main-loop.md)、[u5 引擎](u5-l1-engine-init-memory.md)、[u6 KV cache](u6-l1-kvcache-pool-prefix-abstract.md) 都会反复用到这里的字段名，所以请务必在这里把它们钉死。

## 2. 前置知识

本讲假设你已经读过 [u1-l4 进程架构与请求生命周期](u1-l4-process-architecture.md)，至少知道下面几件事：

- LLM 推理分两段：**prefill**（把整条 prompt 一次性算出 KV）和 **decode**（之后每次只算 1 个新 token）。
- 一个请求从 API 进来后，会经过 Tokenizer → Scheduler → Engine，再原路把 token 送回去。`uid` 是全程不变的请求身份。
- 每张 GPU 上跑一个 Scheduler 进程；Engine 是 Scheduler 内部真正调用模型前向的部件。

如果这些概念你还觉得模糊，下面有几个本讲会用到的术语，先用最朴素的方式定义一下：

| 术语 | 朴素解释 |
| --- | --- |
| **KV Cache** | Transformer 每一层对「已经算过的 token」算出的 Key/Value 向量。后面再算新 token 时，要把历史 KV 拿出来参加注意力计算，避免重算。 |
| **Prefix Cache（前缀复用）** | 不同请求如果开头相同（比如同样的系统提示词），它们的 KV 可以复用。`cached_len` 就表示「这个请求有多少 token 的 KV 已经存在缓存里、不用再算了」。 |
| **Chunked Prefill（切块预填）** | 当 prompt 太长，一次性 prefill 会爆显存，于是把 prompt 切成几块分批算。 |
| **Tensor Parallelism（张量并行）** | 把一层里的权重矩阵切开分到多张 GPU 上算，再用通信把结果拼回去。 |

还有一点 Python 语法前置：本讲大量出现 `@dataclass`。它是 Python 标准库提供的装饰器，能让你用「声明字段」的方式自动生成 `__init__`。比如：

```python
@dataclass
class SamplingParams:
    temperature: float = 0.0
```

就等价于自动生成了一个 `def __init__(self, temperature: float = 0.0)`。带 `= 默认值` 的字段可不传，不带默认值的字段必须传。本讲的四个类全是 dataclass。

## 3. 本讲源码地图

本讲几乎只盯一个文件：

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) | 定义贯穿全系统的公共数据结构与全局上下文 | `SamplingParams` / `Req` / `Batch` / `Context` / `_GLOBAL_CTX` |

为了让你看清「这些结构不是孤立的、而是被各方真正调用」，本讲还会**引用**（但不深入）下面几个消费方，作为证据：

| 消费方文件 | 它怎么用 `core.py` |
| --- | --- |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | `set_global_ctx(ctx)` 注册全局上下文；`forward_batch` 里对每个 req 调 `complete_one()` |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | `_process_last_data` 里调 `req.append_host(...)`、读 `req.can_decode` |
| [python/minisgl/scheduler/prefill.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py) | 真正 `Req(...)` 构造的地方；还定义了子类 `ChunkedReq` |
| [python/minisgl/layers/attention.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py) | 通过 `get_global_ctx().batch` 拿到当前 batch 的 `input_ids` |

> **为什么本讲要带你看这些消费方？** 因为 `core.py` 本身几乎全是「数据声明 + 少量方法」，单看它会觉得平淡。只有当你看见 `complete_one` 在 engine 里被调、`append_host` 在 scheduler 里被调，这些字段才「活」起来。这也是 [u1-l3](u1-l3-directory-structure.md) 讲过的「core.py 是全系统的公共词汇表」的真正含义。

---

## 4. 核心概念与源码讲解

### 4.1 SamplingParams

#### 4.1.1 概念说明

`SamplingParams` 描述「这个请求想要怎样生成 token」。语言模型每一步会输出一整张词表上的概率分布，**采样策略**决定从这张分布里挑哪一个 token：

- `temperature`（温度）：>1 让分布更平、输出更随机；<1 更保守；=0 就是「永远挑概率最大的」，即**贪婪采样**。
- `top_k`：只保留概率最高的 k 个候选，其余置零。-1 表示不限制。
- `top_p`（核采样/nucleus）：保留累计概率达到 p 的最少候选。
- `max_tokens`：最多生成多少个新 token。
- `ignore_eos`：是否忽略「结束符」。正常情况下模型生成出 `<eos>` 就该停，但某些评测场景希望强制生成满 `max_tokens`。

#### 4.1.2 核心流程

```
用户请求 (temperature=0.7, top_k=50, top_p=0.9, max_tokens=512)
   │
   ▼
封装成 SamplingParams，挂在 Req 上
   │
   ▼
Engine 采样时：把整批 req 的参数「批量打包」交给 Sampler
   │  （批量打包的细节是 u5-l2 的内容）
   ▼
is_greedy == True 的请求走 torch.argmax 快路径；否则走 top_k/top_p
```

关键在于：`SamplingParams` 只是一个**被动的小袋子**，它本身不执行采样，只是把用户的意图带到采样器面前。它只暴露了一个派生属性 `is_greedy`，用来让采样器快速判断「这批是不是全贪婪」。

#### 4.1.3 源码精读

[python/minisgl/core.py:L15-L25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L15-L25) —— `SamplingParams` 的全部代码。

```python
@dataclass
class SamplingParams:
    temperature: float = 0.0
    top_k: int = -1
    top_p: float = 1.0
    ignore_eos: bool = False
    max_tokens: int = 1024

    @property
    def is_greedy(self) -> bool:
        return (self.temperature <= 0.0 or self.top_k == 1) and self.top_p == 1.0
```

几个要点：

- **默认值即「贪婪」**：`temperature=0.0`、`top_p=1.0`、`top_k=-1`。也就是说，如果用户什么都没指定，默认就是贪婪采样。这在引擎里能直接走最快的 `argmax` 路径（见 [u5-l2](u5-l2-engine-forward-sampling.md) 的实践题）。
- **`is_greedy` 的判定**：只要「温度 ≤ 0」**或**「top_k==1」，并且「top_p 没有截断（==1.0）」，就视为贪婪。注意它是 `and` 串联：`top_p != 1.0` 会哪怕温度为 0 也判定为非贪婪，因为核采样会改变候选集。
- `max_tokens` 默认 1024：这是一个请求级的上限，调度器还会用 `max_seq_len - input_len` 再夹一次（见下文 4.2 的不变量讨论）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `is_greedy` 在各种参数组合下的取值，建立对判定逻辑的直觉。

**操作步骤**：

1. 在能 `import minisgl` 的环境里（参见 [u1-l2](u1-l2-install-and-run.md) 的安装步骤），打开一个 Python 终端。
2. 依次构造几组 `SamplingParams` 并打印 `is_greedy`（以下为**示例代码**，非项目原有代码）：

   ```python
   from minisgl.core import SamplingParams

   cases = [
       SamplingParams(),                              # 全默认
       SamplingParams(temperature=0.7),               # 有温度
       SamplingParams(top_k=1),                       # 只取最高 1 个
       SamplingParams(temperature=0.0, top_p=0.9),    # 温度 0 但核采样
       SamplingParams(temperature=0.0, top_p=1.0),    # 纯贪婪
   ]
   for c in cases:
       print(c, "→ is_greedy =", c.is_greedy)
   ```

**需要观察的现象**：第 1、3、5 组应为 `True`，第 2、4 组为 `False`。尤其注意第 4 组——`temperature=0.0` 但 `top_p=0.9`，结果仍是 `False`，这印证了 `and self.top_p == 1.0` 的作用。

**预期结果**：你能在不看源码的情况下，凭直觉判断任意一组参数是否走贪婪快路径。

**待本地验证**：若无 GPU 环境，本实践仍然成立——`SamplingParams` 是纯 Python dataclass，不依赖 CUDA，`import` 即可运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_greedy` 里要写成 `temperature <= 0.0` 而不是 `temperature == 0.0`？

> **答案**：温度在工程上可能因为浮点运算或用户误填出现极小的负值（比如 `-0.0` 或 `-1e-9`）。用 `<= 0.0` 把「任何非正温度」都当作「确定性采样」处理，更稳健；同时它和「挑最大」的语义一致——负温度在物理上无意义，归并到贪婪是安全选择。

**练习 2**：`top_k == 1` 为什么也算贪婪？

> **答案**：`top_k == 1` 表示「候选集只保留概率最高的 1 个」，等价于永远选 argmax。此时温度和 top_p 都无从发挥作用（候选只有一个），所以它和 `temperature=0` 行为一致，自然归入贪婪。

---

### 4.2 Req 状态机

> 这是本讲最重要的模块，也是本讲**主实践任务**所在。

#### 4.2.1 概念说明

一个 `Req` 对象 = **一条正在被引擎处理的请求在「调度器/引擎」这一侧的全部分身**。它在 prefill 开始前被构造，在生成完最后一个 token 后被销毁；它的一生就是一部「长度计数器」的演化史。

先认识它最核心的几个字段（其余字段稍后再讲）：

| 字段 | 含义 | 谁来填 |
| --- | --- | --- |
| `input_ids` | 这条请求**到目前为止**的所有 token id（CPU 上的 1D tensor，会随生成不断变长） | 构造时由 prefill 切片填入，之后由 `append_host` 追加 |
| `cached_len` | 前 **多少个** token 的 KV 已经存在于缓存里、不需要再算 | 构造时由前缀缓存命中决定；`complete_one` 后被推进 |
| `device_len` | 「逻辑序列长度」——目前序列推进到了第几个位置 | 构造时 = `len(input_ids)`；`complete_one` 每次 +1 |
| `max_device_len` | 序列长度上限（= 输入长度 + 允许的输出长度） | 构造时一次性定下 |
| `output_len` | 允许生成的最大新 token 数 | 来自 `SamplingParams.max_tokens`（被调度器夹过） |
| `uid` | 请求的唯一身份，全程不变 | 来自 `UserMsg` |
| `table_idx` | 这条请求在 `token_pool` / `page_table` 里占用的「行号」 | 由 `TableManager` 分配（[u4-l4](u4-l4-decode-table-manager.md)） |
| `cache_handle` | 指向前缀缓存中对应条目的句柄 | 由 `CacheManager.match_req` 给出（[u6](u6-l1-kvcache-pool-prefix-abstract.md)） |
| `sampling_params` | 采样参数 | 来自 `UserMsg` |

注意 `device_len` 这个名字有点误导——它**并不是「在 GPU 上的 token 数」**，而是一个「逻辑序列游标」。叫 `device_len` 是因为它的取值最终决定了要在 GPU 上算多少。

#### 4.2.2 核心流程：长度的四则运算

`Req` 上有三个派生属性，它们都是上述字段的简单运算：

```
extend_len  = device_len - cached_len     # 本轮前向「真正要新算」的 token 数
remain_len  = max_device_len - device_len # 还能再生成多少 token
can_decode  = remain_len > 0              # 还能继续 decode 吗
```

- `extend_len` 是 prefill/decode 的「工作量」。prefill 一开始它等于要算的 prompt 长度；进入 decode 后它恒为 1（每步只算 1 个新 token）。注意力后端就是靠它来构造 `cu_seqlens`（见 [u7-l2](u7-l2-flashinfer-backend.md)）。
- `remain_len` 是「剩余预算」，耗尽即结束。

两个会**修改状态**的方法是状态机的齿轮：

```
complete_one():   # engine.forward_batch 里，模型算完一轮后调用
    cached_len = device_len      # 把「算到 device_len 为止」的 KV 标记为已缓存
    device_len += 1              # 预留出下一个新 token 的位置

append_host(tok):  # scheduler._process_last_data 里调用
    input_ids = cat([input_ids, tok])   # 把采样到的新 token 追到 input_ids 末尾
```

注意它们的**配对关系**：`complete_one` 先把 `device_len` 加 1（逻辑上「占位」），但此时 `input_ids` 还没变长；紧接着 `append_host` 把真实采样到的 token 追加进去，让 `len(input_ids)` 重新追上 `device_len`。两者一前一后，保证了「游标先走一步、数据随后补齐」的节奏。

整个状态机可以画成这样：

```
        构造 (prefill 切块决定 cached_len 与初始 device_len)
          │  不变量: 0 <= cached_len < device_len <= max_device_len
          ▼
   ┌─── prefill/decode 前向 (model.forward) ───┐
   │                                            │
   │   complete_one()  ← cached_len=device_len  │
   │                     device_len += 1        │
   └────────────────────────────────────────────┘
          │
          ▼
   append_host(next_token)  ← input_ids 追长 1
          │
          ▼
   can_decode ? ── 否 ──> finished (释放资源)
          │
         是
          │
          ▼
     回到「前向」（下一轮 decode，extend_len=1）
```

#### 4.2.3 源码精读

先看类声明与字段。[python/minisgl/core.py:L28-L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L28-L42)：

```python
@dataclass(eq=False)
class Req:
    input_ids: torch.Tensor  # cpu tensor
    table_idx: int
    cached_len: int
    output_len: int
    uid: int
    sampling_params: SamplingParams
    cache_handle: BaseCacheHandle

    def __post_init__(self) -> None:
        assert self.input_ids.is_cpu
        self.device_len = len(self.input_ids)
        self.max_device_len = len(self.input_ids) + self.output_len
        assert 0 <= self.cached_len < self.device_len <= self.max_device_len
```

要点：

- `@dataclass(eq=False)`：关掉自动生成的 `__eq__`。因为 `Req` 会被放进 `set()`（比如 scheduler 里的 `finished_reqs`、`running_reqs`），默认 `__eq__` 会拖慢比较且语义混乱，项目改用**默认的对象身份（id）**作为相等性，这正是 set 去重想要的。
- `input_ids` 标注为 `# cpu tensor`：`Req` 是 CPU 侧的账本，token 列表常驻 CPU pinned memory，真正上前向时才由调度器搬进 GPU。
- `__post_init__` 是 dataclass 的钩子，在自动生成的 `__init__` 结束后立即执行。它做了三件事：
  1. 断言 `input_ids` 确实在 CPU 上（防御性检查，避免有人误传 GPU tensor）；
  2. 派生 `device_len = len(input_ids)` 和 `max_device_len = len(input_ids) + output_len`；
  3. 断言核心不变量 `0 <= cached_len < device_len <= max_device_len`。

这条不变量是整个状态机的「宪法」。它的含义是：**已经缓存的长度，必须严格小于当前序列长度**（否则就没有新东西可算），而**当前序列长度不得超过上限**。

接着看三个派生属性与状态变更方法。[python/minisgl/core.py:L44-L61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L44-L61)：

```python
    @property
    def remain_len(self) -> int:
        return self.max_device_len - self.device_len

    @property
    def extend_len(self) -> int:
        return self.device_len - self.cached_len

    def complete_one(self) -> None:
        self.cached_len = self.device_len
        self.device_len += 1

    def append_host(self, next_token: torch.Tensor) -> None:
        self.input_ids = torch.cat([self.input_ids, next_token])

    @property
    def can_decode(self) -> bool:
        return self.remain_len > 0
```

对照 4.2.2 的流程图读，这段代码应该已经一目了然。

**关键证据：这些方法到底被谁调用？**

`complete_one` 在 Engine 的前向里被调用。[python/minisgl/engine/engine.py:L191-L206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) 里：

```python
    def forward_batch(self, batch: Batch, args: BatchSamplingArgs) -> ForwardOutput:
        ...
        with self.ctx.forward_batch(batch):
            if self.graph_runner.can_use_cuda_graph(batch):
                logits = self.graph_runner.replay(batch)
            else:
                logits = self.model.forward()

        for req in batch.reqs:
            req.complete_one()        # ← 模型算完后，推进每个 req 的游标
        ...
```

`append_host` 和 `can_decode` 在 Scheduler 处理「上一批结果」时被调用。[python/minisgl/scheduler/scheduler.py:L147-L156](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L147-L156)：

```python
            for i, req in enumerate(batch.reqs):
                ...
                next_token = next_tokens_cpu[i]
                req.append_host(next_token.unsqueeze(0))   # ← 追加新 token
                next_token = int(next_token.item())
                finished = not req.can_decode               # ← 预算耗尽则结束
                if not req.sampling_params.ignore_eos:
                    finished |= next_token == self.eos_token_id
                ...
```

注意第 153 行：一个请求「是否结束」有两个判据——**要么 `remain_len` 耗尽**（`can_decode` 为假），**要么撞上结束符 `<eos>`**（除非 `ignore_eos`）。这两条判据恰好分别来自 `Req` 的状态机和 `SamplingParams` 的字段，体现了两个数据类的协作。

**Req 是在哪里被构造的？** 看 [python/minisgl/scheduler/prefill.py:L65-L90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L65-L90) 的 `_add_one_req`：

```python
        return CLS(
            input_ids=pending_req.input_ids[: cached_len + chunk_size],
            table_idx=table_idx,
            cached_len=cached_len,
            output_len=pending_req.output_len,
            uid=pending_req.uid,
            cache_handle=cache_handle,
            sampling_params=pending_req.sampling_params,
        )
```

这里能验证 4.2.1 表格里的来源：`cached_len` 来自前缀缓存命中，`table_idx` 来自 `TableManager`，`input_ids` 是「已缓存部分 + 本次切块」的切片。`CLS` 可能是 `Req` 也可能是子类 `ChunkedReq`（见练习）。

最后看一个有趣的「特例 req」：Engine 初始化时为了给 CUDA Graph 占位，构造了一个 `dummy_req`。[python/minisgl/engine/engine.py:L89-L97](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L89-L97)。它的 `uid=-1`、`sampling_params=None`、`cache_handle=None`，纯粹为了在 graph 捕获时充当「形状正确的假数据」。这告诉我们：`Req` 的字段虽然语义丰富，但在特定场景（如 graph 占位）可以填 `None`，前提是那段代码路径不会真的去读它们。

#### 4.2.4 代码实践（本讲主任务）

**实践目标**：阅读 `Req.__post_init__`、`complete_one`、`append_host`，手画一张「长度演化时间线」，把抽象的字段变成你能数的数字。

**场景设定**：一条 prompt 共 **5 个 token**，前缀缓存**没有命中**（`cached_len=0`），不切块（一次 prefill 完），`output_len=3`（只生成 3 个新 token）。

**操作步骤**：

1. 打开 [python/minisgl/core.py:L38-L61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L38-L61)，对照 `__post_init__` / `complete_one` / `append_host`。
2. 在纸上（或表格里）逐行填写下表的每一列。第一行「构造」我已经替你算好，作为样板：

   | 时刻 | 事件 | `cached_len` | `device_len` | `len(input_ids)` | `extend_len` | `remain_len` | `can_decode` |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | ① 构造 | `_add_one_req` 建好 Req | 0 | 5 | 5 | 5 | 3 | True |
   | ② prefill forward + `complete_one` | 模型算完 5 个 | 5 | 6 | 5 | 1 | 2 | True |
   | ③ `append_host(t₅)` | scheduler 追加新 token | 5 | 6 | 6 | 1 | 2 | True |
   | ④ decode forward + `complete_one` | 算第 1 个新 token | _?_ | _?_ | 6 | _?_ | _?_ | _?_ |
   | ⑤ `append_host(t₆)` | | _?_ | _?_ | _?_ | | | |
   | ⑥ decode forward + `complete_one` | 算第 2 个新 token | _?_ | _?_ | _?_ | _?_ | _?_ | _?_ |
   | ⑦ `append_host(t₇)` | | _?_ | _?_ | _?_ | | | |

3. 验证两个不变量在每一步都成立：
   - `0 <= cached_len < device_len`；
   - `extend_len` 在 prefill 阶段 = 5，在 decode 阶段恒为 1。

**需要观察的现象**：
- `device_len` 比 `len(input_ids)` 总是「先走一步」（在 `complete_one` 之后、`append_host` 之前），这是 overlap scheduling 里典型的「逻辑游标先动、数据后补」。
- 每经过一轮 decode，`remain_len` 减 1；当它降到 0，`can_decode` 变 `False`，请求被判定为 finished。

**预期结果**：第 ④ 步应得 `cached_len=6, device_len=7, extend_len=1, remain_len=1`；第 ⑥ 步应得 `cached_len=7, device_len=8, extend_len=1, remain_len=0`，此时 `can_decode=False`，恰好生成了 3 个 token（t₅、t₆、t₇），与 `output_len=3` 吻合。

**待本地验证**：上表是纯算术推演，不依赖 GPU。如果你想用代码验证，可以写一段**示例代码**手动模拟（注意：`Req` 的 `__post_init__` 要求 `input_ids` 是真实 CPU tensor）：

```python
import torch
from minisgl.core import Req, SamplingParams

req = Req(
    input_ids=torch.tensor([10, 11, 12, 13, 14], dtype=torch.int32),
    table_idx=0, cached_len=0, output_len=3, uid=1,
    sampling_params=SamplingParams(),
    cache_handle=None,  # 仅模拟长度演化，不真正写 KV
)
print("构造后:", req)                      # 看 __repr__
req.complete_one()
req.append_host(torch.tensor([15], dtype=torch.int32))
print("prefill 后:", req, "len=", len(req.input_ids))
```

> 注意：`cache_handle=None` 在真实调度器里是非法的，这里只是为了让你能单跑 `core.py` 的逻辑而做的模拟。Engine 里那个 `dummy_req` 也是同样套路（见 4.2.3 末尾）。

#### 4.2.5 小练习与答案

**练习 1**：`ChunkedReq` 是 `Req` 的子类（见 [python/minisgl/scheduler/prefill.py:L23-L29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L23-L29)）。它把 `append_host` 改成直接 `raise NotImplementedError`，把 `can_decode` 改成永远返回 `False`。为什么 chunked 的请求不能采样、也不能进 decode？

> **答案**：`ChunkedReq` 表示「这条 prompt 太长，被切成了好几块，现在算的只是其中一块 prefill」。它压根还没到「生成新 token」的阶段——它要先把整条 prompt 的 KV 算完，才能进入 decode。所以：调用 `append_host` 意味着「采样出了一个新 token」，这与「我还在 prefill 中部」矛盾，直接报错；`can_decode=False` 则保证它不会被误加入 `DecodeManager.running_reqs`（见 [python/minisgl/scheduler/decode.py:L15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L15)）。所有 chunk 算完后，prefill 管理器会用一个**新的普通 `Req`** 接续 decode。

**练习 2**：`__post_init__` 里的断言是 `cached_len < device_len`（严格小于）。如果有一条请求的 prompt **完全**命中前缀缓存（即全部 token 都已缓存），这个断言会怎样？

> **答案**：会断言失败、直接抛错。这也正是项目的设计意图——如果整条 prompt 都在缓存里，那就没有任何新 token 需要算，调度器根本不应该为它构造一个「要前向」的 `Req`。换句话说，「构造 Req」这一动作本身就隐含了「至少有 1 个新 token 要算」的前置条件。这个边界由 `PrefillAdder` 在更上游保证（命中后只会构造需要补算的那一段）。

**练习 3**：`complete_one` 里为什么是先 `cached_len = device_len`、再 `device_len += 1`，顺序能反过来吗？

> **答案**：不能反过来。语义上，「这一轮前向把 KV 算到了 `device_len` 这个位置」，所以要把 `cached_len` 标记到当前的 `device_len`；之后再 `+1` 为下一个 token 预留位置。如果先 `device_len += 1` 再赋值，`cached_len` 会被错误地标成 `device_len+1`，导致 `extend_len = device_len - cached_len` 变成负数，破坏不变量 `cached_len < device_len`。

---

### 4.3 Batch 阶段

#### 4.3.1 概念说明

一条 `Req` 只代表「一个请求」。但 GPU 前向最怕的就是「一次只算一条」——那样算力浪费严重。于是调度器把**多个 Req 打包成一团**送进引擎，这个「团」就是 `Batch`。

`Batch` 还要携带一个关键标签：**阶段**（`phase`）。因为 prefill 和 decode 在 GPU 上的计算形状完全不同：

- **prefill batch**：每条 req 可能要算几百上千个 token（`extend_len` 很大），多条请求拼起来是一长串变长序列，注意力要用专门的「变长 prefill」kernel。
- **decode batch**：每条 req 只算 1 个新 token（`extend_len == 1`），形状极其规整，可以套用 CUDA Graph（见 [u5-l3](u5-l3-cuda-graph.md)）。

所以 `Batch` 不仅仅是「Req 的列表」，它还是「这一轮前向的元信息容器」。

#### 4.3.2 核心流程：谁负责填 Batch 的字段

`Batch` 的字段分两拨，分别由不同部件填：

```
Scheduler 构造 Batch(reqs, phase)
   │
   ├─ Scheduler 填写（执行前准备）:
   │     input_ids   ← 把各 req 的 token 拼成一条扁平 1D tensor
   │     positions   ← 每个 token 的绝对位置 (cached_len .. device_len-1)
   │     out_loc     ← 每条 req 采样结果要写到 KV pool 的哪个位置
   │     padded_reqs ← CUDA Graph 补齐用的 dummy req 列表
   │
   ├─ 注意力后端填写:
   │     attn_metadata ← prefill/decode 所需的索引、cu_seqlens、page_table 引用
   │
   ▼
Engine.forward_batch(batch) → 模型用 ctx.batch 读取这些字段
```

这种「数据声明在 core、填充分散在各方」的分工，正是 4.4 节 `Context` 要解决的「怎么让模型层拿到这些字段」的动因。

#### 4.3.3 源码精读

[python/minisgl/core.py:L71-L97](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L71-L97)：

```python
@dataclass
class Batch:
    reqs: List[Req]
    phase: Literal["prefill", "decode"]
    # these fields should be set by scheduler
    input_ids: torch.Tensor = field(init=False)
    positions: torch.Tensor = field(init=False)
    out_loc: torch.Tensor = field(init=False)
    padded_reqs: List[Req] = field(init=False)
    # this field should be set by attention backend
    attn_metadata: BaseAttnMetadata = field(init=False)

    @property
    def is_prefill(self) -> bool:
        return self.phase == "prefill"

    @property
    def is_decode(self) -> bool:
        return self.phase == "decode"

    @property
    def size(self) -> int:
        return len(self.reqs)

    @property
    def padded_size(self) -> int:
        return len(self.padded_reqs)
```

要点：

- **构造时只需 `reqs` 和 `phase`**：其余字段都用 `field(init=False)`，表示「不进 `__init__` 参数列表」，由后续部件赋值。注释 `# these fields should be set by scheduler` 和 `# this field should be set by attention backend` 直接写明了归属。
- **`size` vs `padded_size`**：`size` 是真实请求数；`padded_size` 是补齐到 CUDA Graph 捕获尺寸后的数量（含 dummy req）。`padded_reqs` 在没有 graph 时通常等于 `reqs`（详见 [u5-l3](u5-l3-cuda-graph.md) 的 `pad_batch`）。
- **`is_prefill` / `is_decode`**：两个布尔属性只是 `phase` 的语法糖，但让上层代码读起来更像自然语言。比如 scheduler 里直接写 `elif batch.is_prefill:`（见 [python/minisgl/scheduler/scheduler.py:L163](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L163)）。

**证据：positions 怎么由 req 的长度字段算出来？** 看 [python/minisgl/scheduler/scheduler.py:L236-L249](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L236-L249) 的 `_make_positions`：

```python
def _make_positions(batch, device):
    needed_size = sum(r.extend_len for r in batch.padded_reqs)
    ...
    for req in batch.padded_reqs:
        length = req.extend_len
        torch.arange(req.cached_len, req.device_len, ...)
```

这里直接印证了 4.2 的核心结论：**每个 token 的绝对位置就是 `arange(cached_len, device_len)`**。也就是说，`Req` 的长度字段最终决定了 Batch 里 `positions` 张量的内容。`core.py` 里的字段，就这样一路流进了 GPU 上的位置编码。

#### 4.3.4 代码实践

**实践目标**：搞清「同一个 Req 在不同 Batch 阶段里，`extend_len` 的取值」，从而理解 prefill/decode 的形状差异。

**操作步骤**：

1. 假设有一条 `Req`：`cached_len=0, device_len=100, output_len=10`（prompt 100 token，要生成 10 个）。
2. 分别计算它在下面两种 Batch 里的 `extend_len`，以及它会贡献给 `positions` 的位置区间：

   | 它所在的 Batch | 此刻 req 的 `extend_len` | 贡献的 `positions` 区间 | 含义 |
   | --- | --- | --- | --- |
   | 一个 **prefill** batch（刚构造） | _?_ | `arange(0, 100)` | 一次性算 100 个 prompt token |
   | 第一次 **decode** batch（`complete_one` 之后） | _?_ | `arange(100, 101)` | 只算 1 个新 token |

3. 打开 [python/minisgl/scheduler/scheduler.py:L236-L267](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L236-L267)，确认 `_make_positions` / `_make_input_tuple` / `_make_write_tuple` 三个函数都在用 `req.extend_len` / `req.cached_len` / `req.device_len` / `req.table_idx` 拼张量。

**需要观察的现象**：prefill 时 `extend_len=100`，decode 时 `extend_len=1`。同一组字段，仅仅因为状态机推进，就在两种 batch 里表现出截然不同的「形状」。

**预期结果**：你能解释「为什么 decode batch 适合用 CUDA Graph、而 prefill batch 不适合」——decode 每步形状固定（每条 req 恰好 +1），可以预先捕获；prefill 形状随 prompt 长度千变万化，无法预先捕获。

**待本地验证**：本实践是源码阅读 + 算术推演，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`Batch` 的 `input_ids` 等字段用 `field(init=False)` 而不是给默认值（比如 `None`）。这两种写法有什么区别？

> **答案**：`field(init=False)` 表示「这个字段不作为构造参数，但对象上一开始**不存在**这个属性」，直到某处显式 `batch.input_ids = ...` 赋值后才存在；在此之前访问会抛 `AttributeError`。如果改成默认值 `None`，则访问会得到 `None` 而不报错，反而可能掩盖「忘了填」的 bug。`field(init=False)` 让「未填就访问」变成显式失败，更安全。

**练习 2**：为什么 `phase` 用 `Literal["prefill", "decode"]` 而不是布尔 `is_prefill`？

> **答案**：`Literal` 把「只允许这两个字符串」写进类型，编辑器和 mypy 能在构造时检查拼写错误（比如误传 `"Prefill"`）。同时保留扩展空间——未来如果加入第三种阶段（例如某种「speculative」阶段），改 `Literal` 比把布尔改三态自然得多。`phase` 是单一事实源，`is_prefill`/`is_decode` 只是它的便利视图。

---

### 4.4 Context 全局上下文

#### 4.4.1 概念说明

到此为止我们有了 `Req`（一条请求）和 `Batch`（一团请求）。但还有一个尴尬问题：**模型层（`models/*.py`）在执行 `forward` 时，怎么拿到「当前正在算的 Batch」？** 模型前向函数的签名通常很干净，不想把 `batch`、`page_table`、`attn_backend`、`kv_cache` 这些东西一路当参数传进每一层。

`Context` 就是答案。它身兼两职：

1. **进程级共享设施的持有者**：`page_table`、`attn_backend`、`moe_backend`、`kv_cache` 这些「整个进程只有一份」的东西，全挂在 `Context` 上。
2. **「当前 batch」的临时挂载点**：通过一个上下文管理器 `forward_batch(batch)`，把「正在算的这个 batch」临时挂到 `ctx._batch`，算完摘下来。模型层用 `get_global_ctx().batch` 随时能拿到它。

而 `Context` 本身，又通过一个**模块级单例** `_GLOBAL_CTX` 暴露给整个进程——任何子包 `from minisgl.core import get_global_ctx` 就能取到同一个实例。

#### 4.4.2 核心流程：单例 + 上下文管理器

```
Engine.__init__:
   ctx = Context(page_size=...)
   填充 ctx.page_table / attn_backend / kv_cache / moe_backend
   set_global_ctx(ctx)          ← 写入 _GLOBAL_CTX（断言：只能写一次）
        │
        │  此后全进程任何地方: get_global_ctx() 都返回这个 ctx
        │
        ▼
每次前向 (Engine.forward_batch):
   with ctx.forward_batch(batch):   ← 进入时: _batch = batch（断言:不能嵌套）
       model.forward()              ← 模型层读 get_global_ctx().batch.input_ids
       attn_backend.forward(...)    ← 注意力层读 get_global_ctx().kv_cache / page_table
   ─────────────────────────────── ← 退出时(finally): _batch = None
```

两个断言守护这套机制的正确性：

- `set_global_ctx` 断言「全局上下文只能设一次」——防止 Engine 被误初始化两次导致旧引用失效。
- `forward_batch` 断言「不能嵌套」——保证任意时刻进程里只有**一个**活跃 batch。这是张量并行正确性的隐含前提：所有 rank 必须算同一个 batch。

#### 4.4.3 源码精读

先看 `Context` 类。[python/minisgl/core.py:L100-L122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L100-L122)：

```python
@dataclass
class Context:
    page_size: int
    # NOTE: this table always treat page_size = 1
    page_table: torch.Tensor = field(init=False)
    attn_backend: BaseAttnBackend = field(init=False)
    moe_backend: BaseMoeBackend = field(init=False)
    kv_cache: BaseKVCachePool = field(init=False)
    _batch: Batch | None = field(default=None, init=False)

    @property
    def batch(self) -> Batch:
        assert self._batch is not None, "No active batch in context"
        return self._batch

    @contextmanager
    def forward_batch(self, batch: Batch):
        assert self._batch is None, "Nested forward_batch is not allowed"
        try:
            self._batch = batch
            yield
        finally:
            self._batch = None
```

要点：

- **只有 `page_size` 是构造参数**：其余设施都用 `field(init=False)`，由 Engine 在 `__init__` 里逐个挂上。注释 `# NOTE: this table always treat page_size = 1` 提示了一个重要约定：`page_table` 这一维始终按 `page_size=1` 来理解（页分配的细节在 [u6](u6-l1-kvcache-pool-prefix-abstract.md)）。
- **`batch` 属性带断言**：访问 `ctx.batch` 时，若 `_batch is None`（即当前没有活跃 batch）会直接报错。这让「在非前向时刻误读 batch」变成显式失败。
- **`forward_batch` 是个 `@contextmanager`**：配合 `with` 语句使用。`try/finally` 保证即使 `model.forward()` 抛异常，`_batch` 也会被清空，不会「卡住」一个幽灵 batch。

再看单例机制。[python/minisgl/core.py:L125-L136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L125-L136)：

```python
_GLOBAL_CTX: Context | None = None

def set_global_ctx(ctx: Context):
    global _GLOBAL_CTX
    assert _GLOBAL_CTX is None, "Global context is already set"
    _GLOBAL_CTX = ctx

def get_global_ctx() -> Context:
    assert _GLOBAL_CTX is not None, "Global context is not set"
    return _GLOBAL_CTX
```

这是一个最朴素的「模块级变量 + 两个访问函数」单例：模块加载时 `_GLOBAL_CTX = None`；Engine 启动时 `set_global_ctx` 把它填上（且只能填一次）；之后所有子包 `get_global_ctx()` 拿到同一个实例。

**证据：谁在写、谁在读？**

写入方只有一个——Engine 初始化。[python/minisgl/engine/engine.py:L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L42) 的 `set_global_ctx(self.ctx)`。

读取方遍布全代码库。举几个典型：

- 模型层读「当前 batch 的 input_ids」：[python/minisgl/models/llama.py:L80](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L80) 的 `self.model.forward(get_global_ctx().batch.input_ids)`。
- 注意力后端读 `kv_cache` / `page_table`：[python/minisgl/attention/fi.py:L88](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L88) 与 [L210](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L210)。
- 前向时进入上下文：[python/minisgl/engine/engine.py:L193](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L193) 的 `with self.ctx.forward_batch(batch):`；CUDA Graph 回放路径同样要走它：[python/minisgl/engine/graph.py:L138](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L138)。

这就是为什么 `core.py` 看着短，却是「全系统的枢纽」——几乎所有运行时部件都通过 `get_global_ctx()` 这根线接到一起。

#### 4.4.4 代码实践

**实践目标**：用一次「全代码库搜索」，亲眼确认 `get_global_ctx` 的读取点有多广，从而体会「Context 是公共枢纽」并非虚言。

**操作步骤**：

1. 在仓库根目录搜索 `get_global_ctx` 的所有出现位置（这是源码阅读型实践，可用 IDE/GitHub 搜索，或 `git grep`）：

   ```bash
   git grep -n "get_global_ctx" -- 'python/minisgl/*.py' 'python/minisgl/**/*.py'
   ```

2. 把结果按子包归类：`models/` 有几处、`attention/` 有几处、`layers/` 有几处、`kvcache/` 有几处。
3. 再单独搜写入点：

   ```bash
   git grep -n "set_global_ctx" -- 'python/minisgl/**/*.py'
   ```

**需要观察的现象**：`get_global_ctx` 的读取点散布在 `models`、`attention`、`layers`、`kvcache` 等多个子包，数量明显多于 `set_global_ctx`；而 `set_global_ctx` 应当只有极少数（甚至唯一一处：`engine.py`）。

**预期结果**：你会得到一个「一写多读」的拓扑——这正符合「单例持有共享设施」的设计意图。这种不对称也解释了为什么 `set_global_ctx` 要断言「只能设一次」：如果有两个写入点，整个进程的共享状态就会分裂。

**待本地验证**：本实践只需 `git grep`（只读命令），任何能访问仓库的环境都能跑。若环境无 `git`，可在 GitHub 网页用代码搜索 `get_global_ctx`。

#### 4.4.5 小练习与答案

**练习 1**：`forward_batch` 为什么要做成「上下文管理器」(`with`)，而不是直接提供 `set_batch(batch)` / `clear_batch()` 两个方法？

> **答案**：上下文管理器 + `try/finally` 保证了「无论 `with` 块内是否抛异常，`_batch` 都一定会被清空」。如果用两个独立方法，一旦 `model.forward()` 抛异常，`clear_batch()` 就可能被跳过，导致 `_batch` 卡在一个已失效的 batch 上，下一次 `forward_batch` 会撞上「Nested forward_batch is not allowed」的断言而死锁。`with` 把「配对」这件事交给语言机制，杜绝遗漏。

**练习 2**：`Context` 把「共享设施」和「当前 batch」混在同一个类里。这种混放有什么好处和坏处？

> **答案**：好处是**统一入口**——模型层只需 `get_global_ctx()` 一根线，就能同时拿到不变设施（`kv_cache`）和瞬时状态（`batch`），调用方代码极简。坏处是**职责耦合**——「进程级不变量」和「每次前向变化的量」生命周期不同，放一起容易让人混淆哪些字段会变、哪些不会。项目用命名（`_batch` 带下划线表示内部、其余是稳定设施）和断言（嵌套保护）来缓解这个坏处，是教学项目里合理的取舍。

**练习 3**：如果某天我们想支持「同一进程里跑两个独立引擎」（比如两个不同模型），这套 `_GLOBAL_CTX` 单例会出什么问题？

> **答案**：会直接冲突——`set_global_ctx` 的「只能设一次」断言会拦住第二个引擎。单例模型天然假设「一个进程 = 一个引擎 = 一组共享设施」。要支持多引擎，就得把 `get_global_ctx()` 的所有读取点改成显式传 `ctx` 参数，或用 `ContextVar` 做线程/协程局部隔离。这是「用全局可变状态换调用简洁」所付出的代价，本讲把它点出来，方便你在 [u5](u5-l1-engine-init-memory.md) 读 Engine 初始化时留个心眼。

---

## 5. 综合实践

**任务**：用一条「假想请求」的完整一生，把本讲四个最小模块串起来验证。

**场景**：用户发来一条 prompt，经过 tokenize 后是 4 个 token `[a, b, c, d]`，前缀缓存命中了前 2 个（`[a, b]` 已缓存），要求生成 2 个新 token。假设不切块。

**步骤**：

1. **SamplingParams**：写出一个合理的 `SamplingParams`（比如默认贪婪、`max_tokens=2`）。

2. **Req 构造**：参考 [prefill.py:L65-L90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L65-L90) 的字段来源，回答：
   - 构造时 `input_ids` 应包含哪些 token？（提示：是「已缓存部分 + 本次要算部分」的切片，本题没有切块，所以是全部 4 个）
   - `cached_len` 是多少？`output_len` 呢？
   - 用 `__post_init__` 算出 `device_len` 和 `max_device_len`，并验证 `0 <= cached_len < device_len <= max_device_len`。

3. **Batch 阶段**：这条 req 第一次进的是 prefill batch 还是 decode batch？此时它的 `extend_len` 是多少？它贡献给 `positions` 的区间是什么？（提示：`arange(cached_len, device_len)`）

4. **长度演化**：填完下面这张「一生时间线」（仿照 4.2.4 的表格），直到 `can_decode` 变 `False`：

   | 时刻 | 事件 | cached_len | device_len | extend_len | remain_len |
   | --- | --- | --- | --- | --- | --- |
   | 构造 | prefill 建 Req | 2 | 4 | 2 | 2 |
   | prefill forward + complete_one | | _?_ | _?_ | _?_ | _?_ |
   | append_host(t₄) | | | | | |
   | decode forward + complete_one | | _?_ | _?_ | 1 | _?_ |
   | append_host(t₅) | | | | | |

5. **Context**：在上述每一步「forward」发生时，Engine 都会执行 `with ctx.forward_batch(batch):`。请说明：
   - 进入 `with` 前后，`ctx._batch` 从什么值变成什么值？
   - 如果模型层在 `with` 块**外面**调用 `get_global_ctx().batch`，会发生什么？（提示：看 [core.py:L110-L113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L110-L113) 的断言）

**预期结果**：

- 第 2 步：`input_ids = [a,b,c,d]`，`cached_len=2`，`output_len=2`，`device_len=4`，`max_device_len=6`，不变量 `0 <= 2 < 4 <= 6` 成立。
- 第 3 步：第一次是 **prefill**，`extend_len = 4 - 2 = 2`（要算 c、d 两个），`positions = arange(2,4) = [2,3]`。
- 第 4 步：prefill 后 `cached_len=4, device_len=5, remain_len=1`；decode 后 `cached_len=5, device_len=6, remain_len=0`，`can_decode=False`，恰好生成 2 个新 token。
- 第 5 步：`_batch` 从 `None` 变为 `batch`，`with` 结束后回到 `None`；块外访问会触发断言 `"No active batch in context"`。

如果以上每一步你都能不查源码答对，说明你已经把 `core.py` 的四个数据结构真正吃透了。

---

## 6. 本讲小结

- **`core.py` 是全系统的公共词汇表**：`SamplingParams`、`Req`、`Batch`、`Context` 四个 dataclass 加一个 `_GLOBAL_CTX` 单例，定义了调度器、引擎、注意力、KV cache、模型层之间传递信息的标准格式。
- **`Req` 是一部长度计数器**：`cached_len`（已缓存）、`device_len`（逻辑游标）、`max_device_len`（上限）三个整数 + `extend_len`/`remain_len` 两个派生量，加上 `complete_one`/`append_host` 两个齿轮，就刻画了一条请求从 prefill 到结束的全过程，核心不变量是 `0 <= cached_len < device_len <= max_device_len`。
- **`complete_one` 与 `append_host` 配对**：前者先推进逻辑游标、后者再补齐 `input_ids`，形成「游标先走一步、数据随后补齐」的节奏；这也是 overlap scheduling 能把 CPU 处理与 GPU 计算重叠起来的微观基础（详见 [u4-l1](u4-l1-scheduler-main-loop.md)）。
- **`Batch` = Req 列表 + 阶段标签 + 待填字段**：`phase` 决定 prefill/decode 两种计算形状；`input_ids`/`positions`/`out_loc`/`padded_reqs` 由调度器填，`attn_metadata` 由注意力后端填。
- **`Context` 双重身份**：既是进程级共享设施（`page_table`/`attn_backend`/`kv_cache`/`moe_backend`）的持有者，又是「当前 batch」的临时挂载点（`forward_batch` 上下文管理器）。
- **单例 `_GLOBAL_CTX` 一写多读**：`set_global_ctx` 只在 Engine 初始化调一次（带断言），`get_global_ctx` 遍布 models/attention/layers/kvcache 等子包，把全进程部件串成一棵以 Context 为根的树。

## 7. 下一步学习建议

本讲建立的「词汇表」会在后续几乎每一讲里复用，建议按数据流往下走：

1. **紧接着读 [u2-l2 配置体系](u2-l2-config-system.md)**：看 `EngineConfig`/`SchedulerConfig`/`ServerArgs` 如何决定本讲提到的 `page_size`、`max_running_req`、`max_seq_len` 等上限——它们是 `Req`/`Batch`/`Context` 字段的「源头配置」。
2. **再读 [u2-l3 进程间消息与序列化](u2-l3-message-serialization.md)**：一个请求跨进程时，`SamplingParams` 和 `uid` 是怎么被序列化搬运的，补全「请求在进调度器之前」的半段生命。
3. **进入 [u4 调度器](u4-l1-scheduler-main-loop.md)**：看 `Batch` 是怎么被 `PrefillManager`/`DecodeManager` 组装出来、`Req` 又是怎么在 `running_reqs` 与 `pending` 之间流转的——本讲的 `can_decode`/`extend_len` 在那里会大量出现。
4. **进入 [u5 引擎](u5-l1-engine-init-memory.md)**：看 `Context` 是在哪里被构造并 `set_global_ctx`，以及 `forward_batch` 里 `complete_one` 之后采样如何发生。
5. **进入 [u6 KV cache](u6-l1-kvcache-pool-prefix-abstract.md)**：本讲反复出现的 `cache_handle`、`cached_len`、`page_table`、`out_loc` 在那里会落地为具体的页分配与回收逻辑。

> 一个阅读小贴士：后续读任何源码时，只要看到 `req.cached_len`/`req.extend_len`/`get_global_ctx().batch`，就回到本讲的表格对一遍——只要长度字段对得上，你基本就不会读丢。
