# CacheBlend：非前缀 KV 复用

## 1. 本讲目标

本讲讲解 LMCache 如何突破「前缀缓存」的限制，实现对**任意位置**重复文本块的 KV cache 复用——这就是 CacheBlend。

学完后你应当能够：

- 说清楚**前缀缓存（prefix caching）**与**非前缀复用（non-prefix reuse）**的区别，并能用一个 RAG 例子说明前缀缓存为什么会失效。
- 读懂 `lmcache/v1/compute/blend/blender.py` 与 `metadata.py`，讲清 `LMCBlender` 的 `blend → blend_layer → process_qkv` 三层调用关系。
- 理解 **selective recompute（选择性重算）** 的动机：为什么直接拼接不同来源的 KV 会损害生成质量，而只重算一小部分 token 就能恢复质量。
- 看懂 blend 流程中「逐层取回 KV」与「逐层重算 Q/K/V」两条协程如何交织（interleave）成一条流水线。

## 2. 前置知识

阅读本讲前，你应当已经具备（这些都在前置讲义中建立）：

- **KV cache 是什么**：Attention 在 prefill 阶段为每个历史 token 计算并缓存的 Key/Value 向量，显存随上下文线性增长（u1-l1）。
- **LMCacheEngine 的三大 API**：`store`（写入）、`retrieve`（取回）、`lookup`（查命中），以及它们返回的 `FFFFFTTTTTTT` 布尔 mask 约定（u1-l6）。
- **layerwise 取回**：`retrieve_layer` 是一个生成器，逐层把 KV 从存储后端搬到 GPU，而不是一次性全搬（u1-l6 的延伸，本讲会再次看到它的协程形态）。
- **token_database**：把一段 token 序列切成若干 `(start, end, CacheEngineKey)` 的流，作为缓存键的来源（u1-l6）。
- 基本的 PyTorch 与 Python `generator`/`yield` 协程知识。

> 名词速查
> - **prefix caching（前缀缓存）**：只有当请求开头若干 token 与已缓存内容**完全连续一致**时才能复用其 KV。
> - **RAG**：检索增强生成，把检索到的文档拼进 prompt 再交给 LLM。
> - **selective recompute（选择性重算）**：只重算少数「偏差最大」的 token 来修正质量，其余 token 直接复用旧 KV。
> - **RoPE / rotary_emb**：旋转位置编码，attention 前对 Q、K 做的位置变换。

## 3. 本讲源码地图

本讲聚焦 `lmcache/v1/compute/blend/` 这个小包，它只有 4 个文件：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `lmcache/v1/compute/blend/blender.py` | 169 | `LMCBlender` 主体：逐层 blend 的协程编排 + `process_qkv` 选择性重算逻辑 |
| `lmcache/v1/compute/blend/metadata.py` | 34 | 两个 dataclass：固定超参 `LMCBlendCommonMetadata` 与运行时状态 `LMCBlendMetadata` |
| `lmcache/v1/compute/blend/utils.py` | 63 | `LMCBlenderBuilder` 单例工厂，按 `instance_id` 复用 blender |
| `lmcache/v1/compute/blend/__init__.py` | 7 | 仅导出 `LMCBlenderBuilder` |

理解 blender 还需要这几个「周边」文件（本讲会引用但不会展开讲）：

- `lmcache/v1/compute/models/base.py`：`LMCBaseModel.compute_layer` 是「逐层重算 Q/K/V」的那条协程，它在每层调用 `blender.process_qkv`。
- `lmcache/v1/compute/attention/metadata.py`：`LMCAttnMetadata` 提供 `update_from_top_indices`，把「重算哪些 token」反映到 attention 的 mask/索引上。
- `lmcache/v1/cache_engine.py`：`retrieve_layer` 是「逐层取回 KV」的那条协程；`enable_blending=True` 时还会切换 token 数据库与内存格式。
- `lmcache/v1/token_database.py`：`SegmentTokenDatabase` 用「特殊分隔符」切 token，是 blend 复用的切分基础。
- `lmcache/integration/vllm/vllm_v1_adapter.py`：vLLM 适配器里创建 blender 并调用 `blend()` 的入口。

> 一个重要的诚实说明：`docs/source/kv_cache_optimizations/blending.rst` 顶部明确标注本讲涉及的**进程内（in-process）CacheBlend 路径已弃用（deprecated）**，官方推荐改用 MP 模式（u3 单元会讲）。但 `compute/blend/` 这套源码仍然存在于代码树中，且 vLLM 适配器在 `enable_blending=True` 时仍会走这条路径，因此它仍是理解「非前缀复用 + selective recompute」思想最直接、最自包含的入口。本讲以源码阅读为主。

## 4. 核心概念与源码讲解

### 4.1 前缀缓存的局限与 CacheBlend 的动机

#### 4.1.1 概念说明

回顾 u1-l6：`LMCacheEngine` 默认按固定 `chunk_size` 把 token 切块，每块的 KV 用 `CacheEngineKey`（包含模型名、token hash 等）做键存入后端。下次请求只要**开头连续命中**，`lookup` 就会返回前缀命中 token 数。

这种「前缀缓存」在两类场景里非常好用：多轮对话（每轮在前一轮基础上追加）、共享 system prompt。但一旦遇到 **RAG**，它就失效了：

- 假设第一次请求是 `[系统提示] [文档A] [文档B] [问题1]`，我们把整段 KV 存了下来。
- 第二次请求是 `[系统提示] [文档B] [文档A] [问题2]`——文档顺序变了。
- 从「文档B」开始，后续 token 与第一次的对应位置**不再连续一致**，前缀命中在第一个顺序不同的 chunk 处就断裂，后面全部 miss。

可是直觉上，`文档A`、`文档B` 的内容没变，它们的 KV cache 应该还能用！问题只在于它们现在出现在 prompt 的不同位置。**CacheBlend 的目标就是复用这些「不在前缀位置」的重复文本块的 KV。**

#### 4.1.2 核心流程

直接复用非前缀位置的 KV，难点不在「找到它」，而在「复用之后生成质量会变差」。原因：

- Attention 的输出依赖 **query 与所有历史 key 的点积**。一段文本的 KV 是在「它当时的上下文」下算出来的；换到新位置后，它前后的 token 变了，理论上 KV 也该变。
- 但研究表明（CacheBlend 论文，SIGCOMM/EuroSys）：**大部分 token 的 KV 受上下文切换影响很小**，只有少数 token（典型在 chunk 边界附近）偏差较大。

于是 CacheBlend 的策略是 **selective recompute（选择性重算）**：

1. 用分隔符把 prompt 切成多个 chunk，每个 chunk 的 KV 若已缓存就直接取回（省下大部分 prefill 计算）。
2. 取回后，**仍然逐层重算一遍 Q/K/V**，但在某个「检查层」比较「重算的新 K」与「复用的旧 K」的差异。
3. 选出差异最大的 top-k 个 token，**只重算这 k 个 token**，把它们的 K/V 写回 GPU buffer；其余 token 保留复用的旧 KV。
4. 这样既复用了大部分 KV（省算），又修正了关键 token（保质）。

一句话：**先全量复用，再定点修补。**

#### 4.1.3 源码精读

「按分隔符切 chunk」由 `SegmentTokenDatabase` 负责。当 `enable_blending=True` 时，`LMCacheEngine` 会用它替换默认的 `ChunkedTokenDatabase`：

[lmcache/v1/cache_engine.py:2083-2090](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L2083-L2090) —— `_Create_token_database` 在 blending 开启时返回 `SegmentTokenDatabase`，否则返回 `ChunkedTokenDatabase`。

`SegmentTokenDatabase` 用配置项 `blend_special_str`（默认 `" # # "`）做分隔符，对 token 做滑窗匹配切分：

[lmcache/v1/token_database.py:466-468](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/token_database.py#L466-L468) —— 把 `blend_special_str` 编码成 `sep_tokens`，后续用它切分整段 token。

[lmcache/v1/token_database.py:470-491](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/token_database.py#L470-L491) —— `_fast_split_by_subtensor` 用 `unfold` 做滑窗，找出所有分隔符位置，把 token 流切成多段。每一段就是一个可独立复用的 chunk。

> 为什么不直接按 `chunk_size` 固定切？因为 RAG 里文档长度不固定，按分隔符切才能保证「同一个文档」永远对应同一组 token、同一个 key，从而跨请求复用。注意文档里特别提醒：**必须先分别 tokenize 每段文本再拼接**，不能把整段字符串 tokenize——否则拼接处的 token 边界会变，key 就对不上了。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用一个具体 RAG 例子说明前缀缓存在哪里断裂、blend 又能救回哪些 chunk。
2. **操作步骤**：
   - 假设两个 prompt（用 `<SEP>` 代表 `blend_special_str`）：
     - P1 = `系统提示 <SEP> 文档A <SEP> 文档B <SEP> 问题1`
     - P2 = `系统提示 <SEP> 文档B <SEP> 文档A <SEP> 问题2`
   - 在 `lmcache/v1/token_database.py` 的 `_fast_split_by_subtensor` 里，跟踪 `<SEP>` 把 P1、P2 各切成 4 段。
   - 假设 P1 已经把 4 段的 KV 都存进了缓存。
3. **需要观察的现象**：
   - P2 的第 1 段（系统提示）与 P1 第 1 段一致 → 前缀命中。
   - P2 的第 2 段（文档B）在 P1 里是第 3 段 → **前缀缓存在此断裂**，但 **blend 仍能用 `文档B` 这个 chunk 的 key 命中并取回 KV**。
   - 同理 `文档A` 也能命中。
4. **预期结果**：画出一张表，列出 P2 的每段在「前缀缓存」与「CacheBlend」两种模式下是否命中。结论应当是 blend 多救回了「文档B、文档A」两段。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `blend_special_str` 改成空字符串会发生什么？
**答案**：`sep_len == 0`，`_fast_split_by_subtensor` 会直接 `yield tokens`（整段不切），blend 退化成「整段一个 chunk」，失去非前缀复用能力。所以分隔符不能为空。

**练习 2**：为什么 blend 要求「先分别 tokenize 再拼接」，而不是拼接成字符串后一次性 tokenize？
**答案**：tokenizer 是有状态的子词切分，字符串拼接处的 token 边界与分别 tokenize 再拼接不同，导致同一文档在不同 prompt 里 token 序列不一致、key 不一致，无法命中。

---

### 4.2 Blend 元数据：固定超参与运行时状态

#### 4.2.1 概念说明

blend 过程需要两类信息，`metadata.py` 用两个 dataclass 分别承载：

- **固定超参（common metadata）**：在整个请求生命周期内不变，来自配置。例如「在哪几层做差异检查」「重算比例」「阈值」。
- **运行时状态（runtime metadata）**：blend 进行中逐步产生、逐步消费的中间结果。例如「这一层选出了哪些 token 要重算（imp_indices）」「当前位置编码」「attention mask」。

把它们拆成两个 dataclass 的好处是职责清晰：common 在构造时定好就不动了，runtime 则在每层 `process_qkv` 里被读写、在请求结束时 `clean()` 清空。

#### 4.2.2 核心流程

```
请求开始
  └─ 构造 LMCBlendCommonMetadata  ← 从 config 读 check_layers / recomp_ratios / thresholds（固定）
  └─ 构造 LMCBlendMetadata        ← imp_indices=None, attn_mask=None, positions=None（空）
逐层 blend：
  layer 0: positions 初始化为 arange；若 layer 0 ∈ check_layers → 算 diff、选 topk → 写入 imp_indices
  layer 1..N: 若 imp_indices 已存在 → 后续层直接复用这套索引
请求结束
  └─ metadata.clean()             ← 三个字段重置为 None，准备下一个请求
```

关键点：**检查只在 `check_layers` 指定的层做一次**，选出的 `imp_indices` 会被后续所有层复用——也就是说「哪些 token 要重算」是在检查层一次性定下来的，不会每层都重选。

#### 4.2.3 源码精读

固定超参 `LMCBlendCommonMetadata`：

[lmcache/v1/compute/blend/metadata.py:10-18](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/metadata.py#L10-L18) —— 三个字段：`check_layers: List[int]`（必填）、`recomp_ratios` 与 `thresholds`（可选 `List[float]`）。

运行时状态 `LMCBlendMetadata`：

[lmcache/v1/compute/blend/metadata.py:21-34](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/metadata.py#L21-L34) —— `imp_indices`（要重算的 token 索引）、`attn_mask`、`positions`（位置编码），以及 `clean()` 把三者重置为 `None`。

这两个 dataclass 在 `LMCBlender.__init__` 里被实例化，超参直接来自配置：

[lmcache/v1/compute/blend/blender.py:47-58](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/blender.py#L47-L58) —— `common_metadata` 读 `config.blend_check_layers / blend_recompute_ratios / blend_thresholds`；`metadata` 初始化为空。

对应的配置项定义在 `config.py`：

[lmcache/v1/config.py:136-158](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/config.py#L136-L158) —— `enable_blending`、`blend_recompute_ratios`、`blend_thresholds`、`blend_check_layers`、`blend_min_tokens`、`blend_special_str`，每项都是 `_CONFIG_DEFINITIONS` 表里的一行（详见 u1-l5「一张表驱动一切」）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：弄清 common 与 runtime 两份元数据在 blend 全程的生命周期。
2. **操作步骤**：
   - 在 `blender.py` 里搜索 `self.common_metadata` 和 `self.metadata` 的所有读写点。
   - 标注哪些是「构造时写一次」（common）、哪些是「逐层写」（runtime 的 `imp_indices`/`positions`）、哪些是「结束时清空」（`clean()`）。
3. **需要观察的现象**：`imp_indices` 只在 `check_layers` 命中的那一层被赋值（`self.metadata.imp_indices = top_indices`），其后所有层都走 `if self.metadata.imp_indices is not None` 分支直接复用。
4. **预期结果**：你能用一句话说出「检查层决定要重算哪些 token，后续层照单全收」。

#### 4.2.5 小练习与答案

**练习 1**：`blend_check_layers` 配成 `[1]`、`blend_recompute_ratios` 配成 `[0.15]`，含义是什么？
**答案**：在第 1 层做差异检查，选出差异最大的 15% token 重算；这套索引后续所有层复用。

**练习 2**：如果 `blend_check_layers` 为空列表，blend 会怎样？
**答案**：`process_qkv` 里 `if layer_id in self.common_metadata.check_layers` 永远不成立，`imp_indices` 永远是 `None`，于是永远走 `else` 分支返回原始 `q,k,v`——相当于「只取回 KV、不做选择性重算」，质量可能下降但不会崩。

---

### 4.3 逐层 blend 协程：blend 与 blend_layer

#### 4.3.1 概念说明

blend 不是「先取回所有 KV，再统一重算」，而是把 **取回 KV** 与 **重算 Q/K/V** 交织成一条逐层流水线。这样做有两个好处：

- **省显存**：同一时刻只需要持有当前层和下一层的 KV，不必把所有层 KV 同时放在 GPU。
- **可流水线**：当层在 GPU 上做 attention 计算时，下一层的 KV 已经在从存储后端搬运的路上。

Python 的 `generator`/`yield` 是表达这种「逐层交替推进」的天然工具。`blend_layer` 就是一个生成器，外部每调一次 `next()`，它就向前推进一步，并在两个子协程之间「打拍子」。

#### 4.3.2 核心流程

`blend_layer` 内部启动两条子协程：

- **A：`layerwise_model_executor = self.layerwise_model.compute_layer(tokens)`** —— 逐层重算 Q/K/V（走 vLLM 模型的 forward，但每层只算到 attention 之前/之中）。
- **B：`layerwise_retriever = self.cache_engine.retrieve_layer(tokens, mask, **kwargs)`** —— 逐层从存储后端取回 KV 到 GPU buffer。

打拍子过程（共 `num_layers + 2` 步）：

```
next(B)        # 预热：取回第 0 层 KV
yield          # 把控制权交回调用方（外部可在此做 attention）
for i in range(num_layers):
    next(B)    # 取回第 i+1 层 KV
    next(A)    # 重算第 i 层 Q/K/V，并调用 blender.process_qkv 做 selective recompute
    yield      # 交回控制权
next(B)        # 收尾
metadata.clean()
yield
```

`blend()` 是对外入口，它创建 `blend_layer` 生成器后用一个循环把所有步跑完：

[lmcache/v1/compute/blend/blender.py:153-170](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/blender.py#L153-L170) —— `blend` 把 token 转成 tensor，构造 `blend_layer`，循环 `num_layers + 2` 次 `next()`。

#### 4.3.3 源码精读

`blend_layer` 的协程编排：

[lmcache/v1/compute/blend/blender.py:125-151](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/blender.py#L125-L151) —— 关键行：
- L137：`compute_layer(tokens)` 启动重算协程 A；
- L138：`retrieve_layer(tokens, mask, **kwargs)` 启动取回协程 B（这就是 u1-l6 讲过的 `retrieve_layer`）；
- L140 / L144：`next(layerwise_retriever)` 推进取回；
- L145：`next(layerwise_model_executor)` 推进重算——这一步内部会调 `blender.process_qkv`；
- L150：`self.metadata.clean()` 清空运行时状态。

重算协程 A 在每层调用 `blender.process_qkv`：

[lmcache/v1/compute/models/base.py:127-129](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/models/base.py#L127-L129) —— `compute_layer` 在算出 `q,k,v` 后立刻交给 `self.blender.process_qkv`，由 blender 决定「哪些 token 重算、把重算结果 patch 进 GPU buffer」。

vLLM 适配器里的调用点（在 layerwise 路径里二选一：blend 或纯 retrieve）：

[lmcache/integration/vllm/vllm_v1_adapter.py:835-844](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L835-L844) —— `enable_blending` 时调 `self.blender.blend(...)`，否则调 `retrieve_layer(...)`。注意上方注释 `Perform blending before layerwise prefix caching`，且有个 TODO 说「prefix caching 与 blending 暂不兼容」。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：数清楚 `blend_layer` 一共 yield 几次、每次 yield 前推进了哪条协程。
2. **操作步骤**：
   - 打开 `blender.py` 的 `blend_layer`，对照上面的「打拍子」伪代码逐行标注。
   - 数 `yield` 的个数：函数体里有 3 处 `yield`（L141、L146、L151），但 L146 在 `for i in range(num_layers)` 循环里，所以总 yield 次数 = `1 + num_layers + 1 = num_layers + 2`。
   - 对比 `blend()` 里的循环范围 `for i in range(self.num_layers + 2)`，验证两者匹配。
3. **需要观察的现象**：每对 `next(B); next(A)` 之间恰好对应「一层」的处理；B 总是比 A 早一步（预热），保证 A 重算某层时该层 KV 已在 GPU。
4. **预期结果**：你能讲清「为什么是 `num_layers + 2` 而不是 `num_layers`」——多出来的是首部预热和尾部收尾。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `blend_layer` 里 `next(layerwise_retriever)`（L144）和 `next(layerwise_model_executor)`（L145）的顺序对调，会出什么问题？
**答案**：重算协程 A 在 `process_qkv` 里要通过 `gpu_connector.get_kv(layer_id)` 读「已取回的旧 KV」。若 A 先于 B 推进，该层 KV 可能还没搬到 GPU buffer，`get_kv` 会抛 `Layer ... is not loaded into GPU buffer.`（见 `gpu_connectors.py` 的 `get_kv`）。

**练习 2**：`blend_layer` 末尾为什么要调 `metadata.clean()`？
**答案**：`imp_indices`/`positions`/`attn_mask` 是上一个请求的运行时状态，若不清空会污染下一个请求的 blend 决策，甚至导致索引越界。

---

### 4.4 选择性重算：process_qkv 的 topk 机制

#### 4.4.1 概念说明

`process_qkv` 是 CacheBlend 的「大脑」，每层被调用一次。它的职责是：

1. 拿到「重算出的新 Q/K/V」与「从缓存取回的旧 K/V（old_k, old_v）」。
2. 在检查层，比较新 K 与旧 K 的差异，选出差异最大的 top-k 个 token。
3. 只把这 k 个 token 的新 K/V 写回 old_k/old_v，其余位置保留旧值——这就是「选择性重算」。
4. 同步裁剪 Q、residual、attn_output 与 attention metadata，使后续 attention 只在这 k 个 query 上计算。

核心直觉：**差异越大，说明这个 token 的旧 KV 越不可信，越需要重算；差异小的 token 直接复用旧 KV 即可。**

#### 4.4.2 核心流程

```
process_qkv(q, k, v, residual, layer_id, attn_output, attn_metadata):
  old_k, old_v = gpu_connector.get_kv(layer_id)      # 取回的旧 KV（在 GPU buffer）
  positions 首次初始化为 arange(seq_len)
  q, k = rotary_emb(positions, q, k)                 # 旋转位置编码

  if layer_id in check_layers:                        # 只在检查层做一次决策
      diff_k = sum_over_head_dim( (k - old_k)^2 )     # 每个 token 的差异分
      topk_num = max( int(seq_len * recomp_ratios[0]), 1 )
      top_indices = sort(topk(diff_k, topk_num))      # 选差异最大的 k 个，并按位置排序
      k, v, q, residual = index_select(top_indices)   # 只保留这 k 个 token
      imp_indices = top_indices                        # 记下来供后续层复用
      positions = positions[top_indices]
      attn_metadata.update_from_top_indices(top_indices)  # 同步 attention mask/索引
  if imp_indices is not None:                          # 把重算结果 patch 回旧 KV
      old_k[imp_indices] = k
      old_v[imp_indices] = v
      return q, old_k, old_v, residual, attn_output, attn_metadata
  else:
      return q, k, v, residual, attn_output, attn_metadata  # 未做检查的层原样返回
```

差异分的数学定义（对每个 token \(i\)，沿 head 维求平方误差和）：

\[
\text{diff}_k[i] = \sum_{h} \left( k_{i,h} - \text{old\_k}_{i,h} \right)^2
\]

重算 token 数由比例 \(r\) 决定：

\[
\text{topk\_num} = \max\!\big(\lfloor \text{seq\_len} \cdot r \rfloor,\ 1\big)
\]

例如 `seq_len=2048`、`recompute_ratio=0.15`，则重算约 307 个 token，其余 1741 个直接复用旧 KV。

#### 4.4.3 源码精读

`process_qkv` 全貌：

[lmcache/v1/compute/blend/blender.py:60-121](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/blend/blender.py#L60-L121) —— 逐段：
- L71：`old_k, old_v = self.gpu_connector.get_kv(layer_id)` 从 GPU buffer 取回旧 KV。`get_kv` 的实现见 [lmcache/v1/gpu_connector/gpu_connectors.py:757-767](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L757-L767)，未加载会抛 `ValueError`。
- L81-87：`positions` 首次初始化为 `arange`，再用 `attn_layer.rotary_emb` 对 q、k 做旋转位置编码。
- L89-92：在检查层算 `diff_k = torch.sum((k - old_k)**2, dim=[1])`，即上面的差异分。
- L98-99：`topk_num = int(total_len * recomp_ratios[0])`，且 `max(topk_num, 1)` 保证至少重算 1 个。
- L101-102：`torch.topk(diff_k, k=topk_num).indices` 取差异最大的索引，再 `sort`（按位置升序，保持序列顺序）。
- L104-106：用 `top_indices` 裁剪 `k, v, q, residual`，只保留要重算的 token。
- L110-114：把 `top_indices` 写入 `metadata.imp_indices`，更新 `positions`，并调 `attn_metadata.update_from_top_indices`。
- L116-119：把重算后的 `k, v` 写回 `old_k[imp_indices], old_v[imp_indices]`——**这就是 selective recompute 的落点**：只有这 k 个位置被新值覆盖，其余位置仍是取回的旧 KV。

`update_from_top_indices` 的两种实现：

[lmcache/v1/compute/attention/metadata.py:31-36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/attention/metadata.py#L31-L36) —— 稠密 attention（FlashAttn）把 `query_start_loc` 改成 `[0, topk_num]`，`max_query_len` 改成 `topk_num`，即「只有 topk 个 query 参与计算」。

[lmcache/v1/compute/attention/metadata.py:142-176](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/compute/attention/metadata.py#L142-L176) —— 稀疏 attention（Triton block-sparse）进一步把 `top_indices` 转成 CSR 稀疏结构，让 attention 只在「重算 query × 全部 key」的块上计算，进一步省算力（这是 `enable_sparse=True` 时走的路径）。

> 内存格式细节：blend 开启时引擎用 `KV_2TD` 而非 `KV_T2D`：

[lmcache/v1/cache_engine.py:203-209](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L203-L209) —— 因为 `process_qkv` 要按 token 维做 `topk` 与索引切片，token 维在最前（2TD = [Token, ...]）更方便；T2D（[Tensor, 2, ...]）则不利于这种按 token 的随机访问。

#### 4.4.4 代码实践（参数实验型）

1. **实践目标**：理解 `recomp_ratios` 如何影响「重算多少 token」，并预测结果。
2. **操作步骤**：
   - 在 `blender.py` 的 `process_qkv` 里定位 L98-99 这两行：
     ```python
     topk_num = int(total_len * self.common_metadata.recomp_ratios[0])
     topk_num = max(topk_num, 1)
     ```
   - 假设 `total_len = 1024`，分别取 `recomp_ratios[0] = 0.0 / 0.15 / 0.5 / 1.0`，手算 `topk_num`。
   - 再读 L101 `torch.topk(diff_k, k=topk_num).indices`，思考 `diff_k` 全为 0（即旧 KV 完全正确）时 `topk` 会返回什么。
3. **需要观察的现象**：
   - `ratio=0.0` 时 `int(1024*0)=0`，但 `max(0,1)=1`，仍会重算 1 个 token（差异最大的那个，即便差异为 0）。
   - `ratio=1.0` 时重算全部 1024 个 token，退化成「全量重算」，blend 失去省算意义。
   - `diff_k` 全 0 时 `torch.topk` 仍返回前 k 个索引（按出现顺序），不会报错。
4. **预期结果**：你能解释为什么默认 `0.15` 是个折中——重算 15% 通常足以恢复质量，同时省下 85% 的计算。**实际质量曲线需待本地用 `examples/blend_kv_v1/blend.py` 跑对比验证。**

#### 4.4.5 小练习与答案

**练习 1**：`process_qkv` 里为什么用 `(k - old_k)**2` 而不是 `(k - old_k)`？
**答案**：差异可正可负，直接相减会正负抵消；平方后非负，沿 head 维求和才能得到每个 token 的「总偏差量」用于排序。

**练习 2**：为什么 `top_indices` 取出后还要 `torch.sort` 一下（L102）？
**答案**：`torch.topk` 返回的索引是按「差异值降序」排的，位置乱序；而后续 attention 需要按序列位置顺序处理（位置编码、causal mask 都依赖顺序），所以要重新按索引值升序排。

**练习 3**：blend 开启时为什么内存格式必须是 `KV_2TD`？
**答案**：`process_qkv` 要对 token 维做 `topk` 与 `k[top_indices]` 索引切片，token 维在最前（2TD）时这些操作是连续内存访问、最高效；T2D 把 token 维放在后面，按 token 索引会变成跨步访问。

---

## 5. 综合实践

**任务**：以 `examples/blend_kv_v1/blend.py` 为参照，端到端走一遍 CacheBlend，并画出完整数据流。

1. **阅读配置**：打开 `examples/blend_kv_v1/blend.py` 的 `setup_environment_variables`（[examples/blend_kv_v1/blend.py:20-60](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/blend_kv_v1/blend.py#L20-L60)），列出它设置了哪些 `LMCACHE_BLEND_*` 环境变量，并对应到 `config.py` 的字段。
2. **阅读 prompt 构造**：看 `blending.rst` 给出的 `first_prompt` / `second_prompt` 构造方式（分别 tokenize 后用 `blend_special_str` 拼接），理解为什么第二个 prompt 顺序变了仍能复用。
3. **跟踪调用链**：从 vLLM 适配器 `self.blender.blend(...)`（[vllm_v1_adapter.py:838](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L838)）出发，依次画出：
   - `blend` → `blend_layer` →（`retrieve_layer` 取回旧 KV + `compute_layer` 重算 Q/K/V）→ `process_qkv` 选 topk → patch 回 old_k/old_v；
   - 标注每一步发生在哪个文件、哪一行。
4. **运行验证（待本地验证）**：在一台有 GPU、装好 vLLM 的主机上：
   ```bash
   cd examples/blend_kv_v1
   python blend.py --model mistralai/Mistral-7B-Instruct-v0.2
   ```
   观察两次 generate 的耗时，并对比开/关 `LMCACHE_ENABLE_BLENDING` 时的输出文本质量与首 token 延迟。**若 `VLLMModelTracker.register_model` 未被调用导致 blender 拿不到 vLLM 模型，需在集成层补注册——这正是 in-process 路径已弃用的现实信号，建议结合 u3 的 MP blend 路径一起看。**

> 完成后，你应当能画出这样一张端到端数据流：
> ```
> vLLM forward
>   └─ adapter.blend(tokens, mask)
>       └─ blender.blend_layer  (generator)
>           ├─ retrieve_layer  → 逐层把旧 KV 从 CPU/磁盘 搬到 GPU buffer
>           └─ compute_layer   → 逐层重算 q/k/v
>                 └─ process_qkv
>                       ├─ check_layer: diff_k = (k-old_k)^2  → topk → imp_indices
>                       └─ old_k[imp_indices] = k   (selective recompute 落点)
> ```

## 6. 本讲小结

- **前缀缓存只认连续开头**：RAG 里文档顺序一变，前缀命中就断裂；CacheBlend 用分隔符切 chunk，让任意位置的重复文本块都能按 key 命中复用。
- **直接复用会损质量**：因为 attention 依赖上下文，非前缀位置的 KV 与新上下文不完全匹配；CacheBlend 用 **selective recompute** 只重算偏差最大的少数 token 来修正。
- **两份元数据分工**：`LMCBlendCommonMetadata`（固定超参：检查层、重算比例、阈值）与 `LMCBlendMetadata`（运行时状态：`imp_indices`/`positions`/`attn_mask`，请求结束 `clean()`）。
- **逐层协程交织**：`blend_layer` 用 generator 把「取回旧 KV」与「重算 Q/K/V」两条协程交替推进，共 `num_layers + 2` 步，省显存且可流水线。
- **topk 决策只在检查层做一次**：`process_qkv` 在 `check_layers` 命中层算 `diff_k = (k - old_k)^2`、选 top-k、把重算结果 patch 回 `old_k[imp_indices]`，后续层直接复用这套索引。
- **现状提醒**：本讲涉及的 in-process `compute/blend/` 路径在文档中标注为 deprecated，生产建议用 u3 单元的 MP blend；但它是理解 selective recompute 思想最直接的入口。

## 7. 下一步学习建议

- **下一讲 u2-l7（KV 编解码与 SERDE）**：blend 取回的 KV 在存储侧是如何序列化/压缩的？这与本讲的「取回旧 KV」直接衔接。
- **u3 单元（MP 架构）**：想看生产级的 CacheBlend，跳到 `lmcache/v1/multiprocess/modules/blend_v3.py` 与 `lmcache/v1/mp_coordinator/blend_directory.py`，那里有 paged-aware 的 BlendV3 与跨实例的 blend lookup。
- **论文**：`README.md` 引用的 `yao2025cacheblend`（CacheBlend: Fast large language model serving for RAG with cached knowledge fusion, EuroSys 2025）是本讲 selective recompute 思想的原始出处，建议配合阅读。
- **延伸源码**：`lmcache/v1/compute/attention/` 下的 `flash_infer_sparse.py` 与 `triton_kernels/`，看稀疏 attention 如何把「只重算 topk 个 query」进一步加速。
