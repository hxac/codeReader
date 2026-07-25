# 投机解码（Speculative Decoding）

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清「投机解码为什么能加速」：在 decode 这种**显存带宽受限**的阶段，用一次前向同时验证 K 个草稿 token 的代价接近于生成 1 个 token，于是接受得越多、单位前向产出的 token 越多。
- 理解 `tensorrt_llm/_torch/speculative/` 模块的两套统一抽象：面向「双模型」的 `Drafter` 基类与面向「单模型」的 `SpecWorkerBase`，以及贯穿两者的 `SpecMetadata` 元数据契约。
- 区分各类 drafter：NGram（免费、模式匹配）、EAGLE3（学习型草稿网络）、MTP（原生多 token 预测）、PARD/DFlash/DSpark（并行草稿）、SA（后缀自动机增强）。
- 看懂「验证一步」的核心算法——用 `torch.cumprod` 找到第一个被拒绝的草稿位置，并把接受率聚合到统计与「投机门」上。
- 说清为什么接受率统计必须**排除 `is_dummy` 假请求**（attention-DP 填充 / CUDA Graph padding），否则会污染投机门的判定。

本讲属于专家层，承接 [u3-l3 ModelEngine 与模型前向](u3-l3-model-engine-forward.md)——你会看到「前向之后、回写之前」那段被我们刻意略过的采样，在投机解码下如何被改写成「草稿 → 验证 → 接受」。

## 2. 前置知识

### 2.1 为什么 decode 阶段值得「投机」

LLM 自回归生成有两个阶段（回顾 [u1-l1](u1-l1-project-overview.md)）：

- **Prefill（上下文）**：算力密集，GPU 算力打满。
- **Decode（生成）**：每步只产 1 个 token，batch 小、序列长，瓶颈是**显存带宽**——要把整个 KV cache 和权重读一遍，却只算出极少的 token。此时算力大量闲置。

「投机」的直觉是：既然算力闲着，不如**一次性多算几个候选 token**。一个轻量的「起草者（drafter）」先猜出 K 个后续 token，目标模型再在**一次前向**里把这 K+1 个位置一起验证。因为 decode 是带宽受限的，多验证几个 token 的**增量算力开销很小**，但只要猜对了，一次前向就能产出多个 token，等效吞吐随之上升。

### 2.2 关键术语速查

| 术语 | 含义 |
|------|------|
| draft token / 草稿 token | 起草者提出的候选 token |
| target model / 目标模型 | 真正负责验证与最终输出的模型 |
| accept / 接受 | 草稿 token 与目标模型采样结果一致，被采纳 |
| rejection sampling / 拒绝采样 | 保证投机解码输出分布与纯目标模型一致的修正采样 |
| acceptance rate / 接受率 | 被接受的草稿 token 数 / 提出的草稿 token 数 |
| linear tree / 线性链 | 草稿排成一条链（每步 K 个串行候选） |
| dynamic tree / 动态树 | 草稿排成一棵树（每层展开多个分支），提高接受率 |
| one-model / 单模型 | 草稿模块寄生在目标模型引擎内，共享前向 |
| two-model / 双模型 | 起草者是独立的模型/对象，由 PyExecutor 单独驱动 |

### 2.3 一个简化的收益模型

设最大草稿长度为 \(K\)，单步接受率为 \( \alpha \in [0,1] \)。理想情况下，每步产出的 token 数约为：

\[
\text{tokens per step} \;\approx\; 1 + \alpha \cdot K
\]

而验证 \(K+1\) 个 token 的前向代价约等于生成 1 个 token（带宽受限、算力闲置）。因此加速比大致为：

\[
\text{speedup} \;\approx\; \frac{1 + \alpha K}{1 + \text{draft 开销}}
\]

这给出三条工程结论，贯穿整个模块设计：

1. **接受率越高越好**——所以有 EAGLE3 这类「学习型」起草者，以及 SA 这类「精确匹配」增强。
2. **batch 大了会亏**——大 batch 时 decode 不再带宽受限，草稿的增量算力不再「免费」，所以有 `max_concurrency` 门控与按 batch 缩短草稿长度的调度。
3. **起草本身要便宜**——NGram 几乎零开销，EAGLE3 的草稿网络远小于目标模型。

> 上述公式为讲解用的简化模型，不是仓库代码里的精确公式，标注为「示例模型」。

## 3. 本讲源码地图

本讲涉及的关键文件（全部位于 `tensorrt_llm/_torch/speculative/` 与 `pyexecutor/` 下）：

| 文件 | 作用 |
|------|------|
| `speculative/interface.py` | 统一核心：`SpeculativeDecodingMode` 枚举、`SpecMetadata` 元数据、`SpecWorkerBase` 单模型基类、拒绝采样与验证逻辑 |
| `speculative/drafter.py` | `Drafter` 抽象基类，定义 `prepare_draft_tokens` 接口与 `should_use_spec_decode`、`get_draft_len_for_batch_size`、CUDA Graph padding 等通用能力 |
| `speculative/ngram.py` | NGram 起草者：`NGramPoolManager`（pattern→matches 池）+ `NGramDrafter` |
| `speculative/model_drafter.py` | `ModelDrafter`：双模型通用起草者（服务 EAGLE3 双模型 / DraftTarget 等） |
| `speculative/eagle3.py` | EAGLE3 单模型实现：`Eagle3OneModelWorker`、`MTPEagleWorker` |
| `speculative/eagle3_dynamic_tree.py` | EAGLE3 动态树变体 `Eagle3OneModelDynamicTreeWorker` |
| `speculative/mtp.py` | MTP 原生多 token 预测：`MTPWorker` |
| `speculative/pard.py` / `dflash.py` / `dspark.py` | 并行草稿家族（PARD / DFlash / DSpark） |
| `speculative/sa_worker.py` / `sa_enhancer.py` / `suffix_automaton.py` | 后缀自动机（SA）独立模式与「增强器」混用模式 |
| `speculative/speculation_gate.py` | `SpeculationGate`：滚动平均接受率，低于阈值则永久关闭投机 |
| `speculative/auto_heuristic.py` | `suggest_spec_config`：`AUTO` 模式下的启发式默认配置 |
| `pyexecutor/py_executor.py` | PyExecutor 单步循环：调用 drafter、验证、聚合接受率统计、投机门判定、dummy 请求排除 |
| `docs/source/features/speculative-decoding.md` | 官方使用文档（各算法的配置类与参数） |

## 4. 核心概念与源码讲解

### 4.1 投机解码基本原理与验证流程

#### 4.1.1 概念说明

投机解码 = **起草（draft）+ 验证（verify）+ 接受（accept）**三段式。

- **起草**：在本步前向之前，由 drafter 产出一条长度 ≤ K 的草稿 token 序列（线性链或树）。
- **验证**：目标模型把「真实上一 token + K 个草稿」共 K+1 个位置当作一次多 token 的前向，并行算出每个位置的目标 logits 并采样。
- **接受**：从左到右比较草稿与目标采样结果，**连续相同的部分全部接受**，遇到第一个不一致就停止（后续草稿全部丢弃），并在该位置补一个目标模型自己的新 token。

关键性质：只要配合拒绝采样（rejection sampling）修正，投机解码的**输出分布与纯目标模型完全一致**——它是一个「无损加速」，不是「近似加速」。贪心场景下退化为简单的逐位比较。

#### 4.1.2 核心流程

每一步 PyExecutor 的投机解码循环可概括为：

```text
for request in generation_requests:
    draft_tokens = drafter.prepare_draft_tokens(request)   # 起草，长度 ≤ K

# 目标模型一次前向，对 [last_real_token, draft_1, ..., draft_K] 算 logits
target_tokens = sample(target_model.forward(inputs_with_drafts))

# 验证：找第一条「草稿链」与目标采样一致的前缀
for i in 1..K:
    if draft_tokens[i-1] == target_tokens[i]:
        accepted += 1            # 接受
    else:
        break                    # 第一个不匹配处停止，丢弃其后所有草稿

new_token = target_tokens[accepted]   # 补一个目标模型自己的 token
rewind_kv_cache(accepted)             # 回退未被接受的草稿占用的 KV cache
```

「连续相同前缀」的长度，正是后面源码里用 `torch.cumprod` 算出来的。

#### 4.1.3 源码精读

**模式枚举 `SpeculativeDecodingMode`**。仓库用一个 `IntEnum` 把所有投机算法归一，并提供大量谓词方法（`is_eagle3()` / `is_ngram()` / `use_one_engine()` 等）供全代码库做能力判定：

[interface.py:271-287](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L271-L287) — 列出全部模式：`MTP`、`MTP_EAGLE`、`MTP_EAGLE_ONE_MODEL`、`EAGLE3`、`EAGLE3_ONE_MODEL`、`NGRAM`、`SA`、`DRAFT_TARGET`、`USER_PROVIDED`、`PARD`、`DFLASH`、`DSPARK`、`NONE`、`AUTO`。

谓词的典型用途是「能力开关」。例如哪些模式支持动态草稿长度、哪些需要回退 KV cache：

[interface.py:370-373](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L370-L373) — `support_dynamic_draft_len()` 标注了 MTP / EAGLE3 单模型 / PARD / DFlash / DraftTarget 单模型 / SA 这些模式支持运行时动态调整草稿长度（与 4.4 节的调度直接相关）。

**验证一步的核心：`torch.cumprod`**。这是整个投机解码最值得读的几行——它在 GPU 上一次性找出每个请求「连续接受了多少个草稿」：

[py_executor.py:4808-4815](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4808-L4815) — 说明：把「草稿 == 目标采样」的比较结果按最后一维做累计乘积 `torch.cumprod(...).sum(dim=1)`。因为一旦出现一个 `False`（不匹配），`cumprod` 之后该位及其右侧全归零，`sum` 恰好等于「第一个不匹配位置之前」的连续匹配数，即接受数 `num_accepted_tokens`。这段注释写得非常清楚：「Use cumprod to find the first rejection point」。

随后用 `num_accepted_tokens` 当作索引，把每个请求该接受的目标 token gather 出来：

[py_executor.py:4819-4821](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4819-L4821) — `new_tokens[0, :, 0] = target_tokens[num_accepted_tokens, batch_indices]`，即「跳过已接受的草稿，取第一个未接受位置作为本步新 token」。

> 注意：上面这段 `cumprod` 路径是 PyExecutor 内部对「采样后接受」的一种向量化实现（见函数注释 [py_executor.py:4759-4777](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4759-L4777)）。仓库里还有一条等价的、保证分布正确性的拒绝采样路径 `rejection_sampling_one_model`，它要求 `flashinfer>=0.6.4`：

[interface.py:51-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L51-L87) — 调用 `flashinfer.sampling.chain_speculative_sampling`，传入草稿分布 `draft_probs`、草稿 token `draft_token_ids` 与目标分布 `target_probs`，返回接受结果与接受的草稿数。它保证最终输出分布与纯目标模型一致。

#### 4.1.4 代码实践

**目标**：亲手验证「`cumprod` 等于连续前缀长度」这一核心断言。

**操作步骤**（纯源码阅读 + 最小复现，无需 GPU）：

1. 打开 [py_executor.py:4808-4815](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4808-L4815)，读注释，理解 `cumprod` 的作用。
2. 写一段最小复现（**示例代码**，非项目代码）：

   ```python
   import torch
   # 模拟 3 个请求，每个 4 个草稿，1=匹配 0=不匹配
   match = torch.tensor([[1,1,1,0],   # 第 4 个草稿错 → 接受 3
                         [1,0,1,1],   # 第 2 个错   → 接受 1
                         [0,1,1,1]])  # 第 1 个错   → 接受 0
   accepted = torch.cumprod(match.int(), dim=-1).sum(dim=-1)
   print(accepted)   # 期望 tensor([3, 1, 0])
   ```

**需要观察的现象**：`accepted` 恰好等于「第一个 0 之前（含）的连续 1 的个数」。

**预期结果**：输出 `tensor([3, 1, 0])`。若你能跑，可在本地 Python 验证；若环境不允许，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么贪心解码下「逐位比较」就够了，而非贪心必须走拒绝采样？
**参考答案**：贪心下目标 token 是 `argmax`，确定性，草稿与之一致即等价；非贪心下目标是从分布里采样的，直接逐位比较会改变输出分布，必须用拒绝采样在「接受/重采样」时做概率补偿，才能保证最终分布与纯目标模型一致。

**练习 2**：如果一个请求本步没有草稿 token（`draft_len=0`），验证路径会怎么走？
**参考答案**：`has_draft_tokens` 为假，走 `else` 分支直接取第一个采样 token（见 [py_executor.py:4822-4825](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4822-L4825)），等价于普通非投机的一步。

### 4.2 统一接口：两套抽象（Drafter 与 SpecWorkerBase）

#### 4.2.1 概念说明

`speculative/` 目录下有十几个文件，但接口层只有**两套抽象**，对应两种架构：

- **双模型（two-model）**：起草者是一个**独立对象**（独立的小模型，或纯算法对象如 NGram）。PyExecutor 在主循环里显式调用它的 `prepare_draft_tokens`，草稿模型与目标模型分别前向。抽象基类是 `Drafter`。代表：EAGLE3 双模型、DraftTarget、NGram、UserProvided。
- **单模型（one-model）**：草稿模块**寄生在目标模型引擎内部**，作为 `nn.Module` 挂进去，在目标模型前向过程中**就地起草**（共享 hidden states、共享引擎）。抽象基类是 `SpecWorkerBase`。代表：EAGLE3 单模型、MTP、PARD、DFlash、DSpark、SA。

两者的桥梁是 `SpecMetadata`——**每步一份、批次级**的投机元数据（草稿 token、长度、模式、采样参数），由 PyExecutor 构造、传给目标模型与注意力后端。

`SpeculativeDecodingMode` 的谓词告诉你某模式属于哪一脉：`use_one_engine()` 为真即单模型，`has_spec_drafter()` 为真即双模型有独立 drafter 对象。

#### 4.2.2 核心流程

```text
# 双模型路径
drafter = ModelDrafter(...) 或 NGramDrafter(...)
每步:
    drafter.prepare_draft_tokens(scheduled_requests)   # 独立起草（可能跑草稿模型前向）
    target_inputs.next_draft_tokens = 收集到的草稿
    target_outputs = target_model.forward(...)          # 目标前向验证
    accept(...)

# 单模型路径
worker = Eagle3OneModelWorker(...) / MTPWorker(...)
worker 挂在 model_engine 上
每步:
    # worker.forward 在目标前向内部被调用，就地起草 + 就地验证
    worker.forward(input_ids, hidden_states, logits, attn_metadata, spec_metadata, draft_model)
```

#### 4.2.3 源码精读

**`Drafter` 抽象基类**——双模型的统一契约：

[drafter.py:12-38](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L12-L38) — 唯一的抽象方法 `prepare_draft_tokens(scheduled_requests, resource_manager)`：给定本步调度的请求，把草稿 token 写回每个请求的 `py_draft_tokens`。子类负责「怎么猜」。

注意 `Drafter` 还内置了大量「通用能力」（非抽象），供所有子类复用：`should_use_spec_decode`（4.4 节的门控）、`get_draft_len_for_batch_size`（按 batch 选草稿长度）、`pad_draft_tokens_for_cuda_graph`（CUDA Graph 对齐）。这是典型的「基类沉淀通用逻辑、子类只填特化逻辑」设计。

**`SpecWorkerBase`——单模型的统一契约**：

[interface.py:1000-1006](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L1000-L1006) — `class SpecWorkerBase(nn.Module, ABC)`。注意它**同时是 `nn.Module`**——这意味着单模型草稿模块会被注册为模型图的一部分，参与前向、权重加载、流水线切分。它把起草与验证的大量共用方法（`sample_and_accept_draft_tokens`、`_accept_draft_tokens`、`_apply_force_accepted_tokens`、各种 buffer 准备）都放在基类，子类只覆盖 `_forward_impl`。

**`SpecMetadata`——两套抽象共享的每步契约**：

[interface.py:453-480](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L453-L480) — 关键字段：`max_num_requests`、`max_draft_len`（草稿层数/线性链草稿数）、`max_total_draft_tokens`（静态树/动态树的最大草稿总数）、`num_generations`（本步生成阶段请求数）、`spec_dec_mode`、`draft_tokens`、`draft_lens`、`request_ids`、`seq_lens`、`gather_ids`、`num_accepted_draft_tokens`。它是 PyExecutor 与目标模型/注意力后端之间的唯一正式数据通道（回顾 [u6-l1](u6-l1-attention-and-mla.md) 的 metadata 契约概念）。

> 区分两个「草稿数」：`max_draft_len` 是**草稿层数**（线性链下等于草稿 token 数），`max_total_draft_tokens` 是**树形草稿的 token 总预算**（线性链下两者相等，动态树下 `max_draft_len <= max_total_draft_tokens`）。

#### 4.2.4 代码实践

**目标**：建立「算法 → 抽象 → 类」的映射表。

**操作步骤**：

1. 在 `speculative/` 下用 `grep` 找出所有 `class .*Worker(SpecWorkerBase)` 与 `class .*Drafter(Drafter)`，确认哪些是单模型、哪些是双模型。
2. 填写下面这张表（**示例答案**，基于本讲源码地图）：

| 算法 | 模式枚举 | 架构 | 主要类 |
|------|----------|------|--------|
| NGram | `NGRAM` | 双模型（算法对象） | `NGramDrafter` |
| EAGLE3（默认） | `EAGLE3_ONE_MODEL` | 单模型 | `Eagle3OneModelWorker` |
| EAGLE3 双模型 | `EAGLE3` | 双模型 | `ModelDrafter` + 草稿模型 |
| MTP | `MTP` | 单模型 | `MTPWorker` |
| PARD / DFlash / DSpark | `PARD`/`DFLASH`/`DSPARK` | 单模型（外部 drafter） | `PARDWorker`/`DFlashWorker`/`DSparkWorker` |
| SA（独立） | `SA` | 单模型 | `SAWorker` |
| DraftTarget | `DRAFT_TARGET_ONE_MODEL` | 单模型（外部 drafter） | `DraftTargetOneModelWorker` |

**需要观察的现象**：谓词 `is_external_drafter()` 涵盖并行草稿与 DraftTarget 单模型（见 [interface.py:347-348](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L347-L348)），它们虽然走 `SpecWorkerBase`，但草稿来自「外部」模型而非寄生模块。

**预期结果**：能说清「为什么 NGram 不需要草稿模型权重，而 EAGLE3 单模型需要加载草稿网络权重」——因为前者是纯算法对象，后者是 `nn.Module`（`need_load_draft_weights()` 为真，见 [interface.py:386-391](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L386-L391)）。

#### 4.2.5 小练习与答案

**练习 1**：`SpecWorkerBase` 为什么继承 `nn.Module` 而 `Drafter` 不继承？
**参考答案**：单模型草稿模块的参数（如 EAGLE3 的草稿网络、投影层）需要和目标模型一起被 PyTorch 管理——权重加载、设备迁移、流水线切分、`state_dict` 序列化都依赖 `nn.Module` 的注册机制；而双模型 drafter 要么是无参数的算法对象（NGram），要么草稿模型由独立的 `ModelEngine` 管理（`ModelDrafter`），自身不需要成为模块。

**练习 2**：如何只看 `SpeculativeDecodingMode` 谓词就判断某算法需不需要独立草稿模型？
**参考答案**：用 `has_draft_model()`（[interface.py:375-376](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L375-L376)）判断是否有独立草稿模型（EAGLE3 双模型 / DraftTarget / MTP_EAGLE 双模型），用 `use_one_engine()` 判断是否单引擎寄生。

### 4.3 Drafter 家族：从免费到学习型

#### 4.3.1 概念说明

按「起草代价」从低到高，drafter 家族大致分四档：

1. **NGram（免费）**：维护一个「token 前缀 → 候选后续」的池，从已生成文本里找模式匹配。对重复性强的内容（代码、JSON、长篇重复）极其有效，对全新内容几乎无效。零模型、零额外前向。
2. **SA 后缀自动机（免费、GPU）**：NGram 的「精确版」——用后缀自动机在历史 token 上做精确后缀匹配，匹配命中时准确率极高。可独立使用，也可作为**增强器**叠加在 MTP/EAGLE3/PARD 上（命中时覆盖神经草稿）。
3. **EAGLE3（学习型，轻量）**：训练一个小型草稿网络，输入目标模型的隐状态，输出草稿 token。准确率远高于 NGram，且对全新内容有效。TRT-LLM 默认且**生产推荐**的 EAGLE 实现就是 EAGLE3 单模型。
4. **MTP（原生）**：模型自带的多 token 预测模块（如 DeepSeek、Step-3.x 的 `num_nextn_predict_layers`），把草稿层和目标模型一起训练，天然对齐。
5. **并行草稿 PARD / DFlash / DSpark（并行）**：与上面「逐 token 起草」不同，它们**一次前向并行预测 K 个草稿 token**（用 mask token / 跨注意力），延迟更低。

#### 4.3.2 核心流程

各类 drafter 的输入/输出/开销对比：

| Drafter | 起草输入 | 起草输出 | 额外前向 | 适合场景 |
|---------|----------|----------|----------|----------|
| NGram | 已生成 token 序列 | 匹配到的候选串 | 无 | 重复内容、代码、无草稿权重 |
| SA | 历史 token + 当前后缀 | 精确匹配的后续 | 无（GPU kernel） | 强重复；可与神经法叠加 |
| EAGLE3 单模型 | 目标隐状态 + 上一 token | K 个草稿 token | 草稿网络前向（轻） | 通用、生产默认 |
| MTP | 目标隐状态 | K 个草稿 token | MTP 层前向 | DeepSeek 等原生 MTP 模型 |
| PARD/DFlash/DSpark | 目标隐状态 + mask | K 个并行草稿 | 1 次并行前向 | 追求低起草延迟 |

#### 4.3.3 源码精读

**NGram——最直观的 drafter**。它有两个类：`NGramPoolManager`（管池，是 `BaseResourceManager` 的一种实现）和 `NGramDrafter`（驱动）：

[ngram.py:14-49](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/ngram.py#L14-L49) — 类注释把核心数据结构讲透了：`pattern`（如 `["I","love"]`）映射到 `matches`（如 `[["apple","because"], ["banana","and"]]`）。`is_public_pool` 决定池是否跨请求共享，`is_keep_all`/`is_use_oldest` 控制多候选时的保留与选取策略。

NGram 的核心匹配逻辑在 `get_draft_tokens`：

[ngram.py:84-141](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/ngram.py#L84-L141) — 先用当前前缀「喂胖」池（把新见到的 pattern→match 关系记下），再用当前末尾的 `size`-gram 作为 key 去池里查，命中则返回对应的候选串作为草稿。注意它的「边生成边学习」特性：生成越多，池越丰富，重复内容上接受率越高。

NGram 的 `prepare_draft_tokens` 很薄，只是遍历请求、调 `get_draft_tokens`、写回 `py_draft_tokens`：

[ngram.py:182-205](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/ngram.py#L182-L205) — 注意 [ngram.py:163](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/ngram.py#L163) 的 `_needs_padding_kv_extension = True`：NGram 用 `TorchSampler`，回退长度从 `py_draft_tokens`（含 padding）算，所以 padding 后必须扩 KV cache 容量（见 4.4.3 的 `pad_draft_tokens_for_cuda_graph`）。

**EAGLE3 单模型**——生产默认。`Eagle3OneModelWorker` 统一服务 EAGLE3 与 MTP-EAGLE 单模型，靠 `is_mtp_eagle` 分支区分：

[eagle3.py:610-622](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/eagle3.py#L610-L622) — 类文档说明：EAGLE3 用多层隐状态 + `apply_eagle3_fc` 投影 + 独立草稿网络；MTP-EAGLE 用单层末尾隐状态 + 反复调用 MTP 层。两者差异处用 `self.is_mtp_eagle` 分支。

**MTP 原生**——`MTPWorker`：

[mtp.py:274-311](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/mtp.py#L274-L311) — `_forward_impl` 的文档用了一个非常清楚的「ABCD → E + 草稿」例子说明 context/decode 两阶段的隐状态流动（`H_t` 目标隐状态、`h_t` 草稿隐状态）。MTP 支持「放松接受」（`use_relaxed_acceptance_for_thinking`）用于推理模型的思考阶段，避免严格匹配卡住思考流。

**双模型通用 `ModelDrafter`**——服务 EAGLE3 双模型 / DraftTarget：

[model_drafter.py:52-67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/model_drafter.py#L52-L67) — 它持有独立的 `draft_model_engine`、`draft_seq_slot_manager`、`sampler`，自己跑一遍草稿模型前向来产草稿。`prepare_draft_tokens` 实现见 [model_drafter.py:972](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/model_drafter.py#L972)。

#### 4.3.4 代码实践

**目标**：对比 NGram 与 EAGLE3 两类 drafter 的输入、输出与开销（本讲指定的实践任务之一）。

**操作步骤**（源码阅读型）：

1. 读 [ngram.py:182-205](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/ngram.py#L182-L205) 与 [model_drafter.py:52-67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/model_drafter.py#L52-L67)（或 eagle3 单模型的 [eagle3.py:610-659](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/eagle3.py#L610-L659)）。
2. 填表对比三类信息：

| 维度 | NGramDrafter | Eagle3OneModelWorker |
|------|--------------|----------------------|
| 起草输入 | 已生成 token 列表 `request.get_tokens(0)` | 目标模型隐状态 + 上一 token |
| 起草输出 | 池匹配到的候选串 `draft_tokens` | 草稿网络产出的 K 个 token |
| 起草额外开销 | 纯 Python 字典查表，几乎为零 | 草稿网络前向（一次或多次） |
| 是否需要草稿权重 | 否 | 是（`need_load_draft_weights()` 为真） |
| 对重复内容 | 极强 | 一般 |
| 对全新内容 | 弱 | 强 |

3. （可选，**待本地验证**，需要 GPU 与模型）用文档 [speculative-decoding.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/speculative-decoding.md) 的配置各跑一次，对比同一 prompt（含大量重复 vs 全新）的接受率日志。示例配置（**示例代码**）：

   ```python
   from tensorrt_llm.llmapi import NGramDecodingConfig, Eagle3DecodingConfig, LLM
   # NGram：免费、无需草稿模型
   llm_ngram = LLM("/path/to/model",
                   speculative_config=NGramDecodingConfig(
                       max_draft_len=3, max_matching_ngram_size=4, is_public_pool=True),
                   disable_overlap_scheduler=True)
   ```

**需要观察的现象**：NGram 在重复 prompt 上接受率高、全新内容上接近 0；EAGLE3 在两者上都较稳定。

**预期结果**：理解「为什么 NGram 适合做 `AUTO` 启发式的默认 fallback」（4.4.3）——零开销、零权重，即使接受率低也几乎不亏。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SA 作为「增强器」叠加在 EAGLE3 上往往比单独用 EAGLE3 更好？
**参考答案**：神经法（EAGLE3）擅长全新内容的概率预测，但对「精确重复」不如模式匹配准；SA 命中时是精确后缀复制、准确率近乎 100%。两者互补：SA 命中长后缀时覆盖神经草稿，未命中时退回神经草稿，文档称之为「best of both worlds」（见 [speculative-decoding.md:182](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/speculative-decoding.md#L182)）。

**练习 2**：MTP 的 `use_relaxed_acceptance_for_thinking` 解决什么问题？
**参考答案**：推理模型在「思考阶段」输出高度发散、草稿很难精确命中，严格接受会让接受率暴跌、投机反而变慢；放松接受把「精确相等」放宽为「草稿落在目标 top-K 候选集内即可」，牺牲一点理论无损性换取思考阶段的吞吐（见 [speculative-decoding.md:110-112](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/speculative-decoding.md#L110-L112)）。

### 4.4 动态控制：草稿长度调度、投机门与动态树

#### 4.4.1 概念说明

「无脑开最大草稿」并不总是赚。三件事决定本步要不要起草、起草多少：

1. **`max_concurrency` 门控**：batch 大到一定程度，decode 不再带宽受限，草稿的增量算力不再「免费」，反而拖慢。用户可设一个并发上限，超过就关投机。
2. **`draft_len_schedule`（按 batch 缩放草稿长度）**：更精细的做法——不是简单开/关，而是「batch 越大、草稿越短」，用一张 `{batch 阈值: 草稿长度}` 表。
3. **`SpeculationGate`（按接受率永久关闭）**：运行时滚动统计真实接受率，若持续低于阈值（说明草稿质量差或场景不适合），**永久关闭**投机，避免持续亏本。
4. **`AUTO` 启发式**：用户不指定算法时，按 `max_batch_size` 自动选一个合理的 NGram 配置。

此外还有「草稿形状」的动态化——**动态树（dynamic tree）**：默认每步只起草一条线性链（K 个串行 token），动态树则在每层展开多个候选分支，用树形验证提高接受率，代价是更多草稿 token 的计算。

#### 4.4.2 核心流程

PyExecutor 每步开头做的「要不要投机」决策（伪代码）：

```text
if drafter has draft_len_schedule:
    new_draft_len = get_draft_len_for_batch_size(len(active_requests))  # 二分查表
    drafter.update_max_total_draft_tokens(new_draft_len)

if new_draft_len == 0:               # 调度表直接归零
    use_spec_decode = False
elif speculation_permanently_disabled:  # 投机门已永久关闭
    use_spec_decode = False
else:
    use_spec_decode = drafter.should_use_spec_decode(
        active_requests, max_batch_size, max_num_tokens, max_total_draft_tokens)
```

#### 4.4.3 源码精读

**决策主入口**——`py_executor.py` 的这段是「每步要不要投机」的真相源：

[py_executor.py:3615-3639](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3615-L3639) — 顺序就是上面伪代码的三段：先按 batch 缩放草稿长度，再尊重 `speculation_permanently_disabled`（投机门），最后调 `should_use_spec_decode`。

**`should_use_spec_decode`——并发门控**：

[drafter.py:40-65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L40-L65) — 核心逻辑：`tokens_per_request = 1 + max_total_draft_tokens`，算出单步 token 预算能容纳多少请求 `token_cap = max_num_tokens // tokens_per_request`，再取 `min(len(requests), max_batch_size, token_cap)` 得「有效请求数」，若它 `<= max_concurrency` 则开投机。若用户没设 `max_concurrency`（为 `None`），**默认永远开**（`return True`）——这与注释「ModelEngine assumes that speculation is always on if max_concurrency is not specified」一致。

**`get_draft_len_for_batch_size`——按 batch 二分查表**：

[drafter.py:106-134](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L106-L134) — 用 `bisect_right` 在已排序的阈值表里找「≤ 当前 batch_size 的最大阈值」对应的草稿长度。例：`draft_len_schedule = {1: 4, 4: 2, 8: 0}` 表示 batch 1~3 用 4、4~7 用 2、≥8 关闭。

**`SpeculationGate`——滚动平均接受率，低于阈值永久关闭**：

[speculation_gate.py:7-82](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/speculation_gate.py#L7-L82) — 用 `deque` 维护最近 `window` 次投机迭代的接受率，O(1) 滚动更新（[speculation_gate.py:58-65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/speculation_gate.py#L58-L65)）；样本数满 `window` 后检查平均接受率是否 `< threshold`，若是则置 `self.disabled = True` 并返回 `disabled_now=True`（[speculation_gate.py:69-79](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/speculation_gate.py#L69-L79)）。PyExecutor 收到 `disabled_now` 后把 `speculation_permanently_disabled` 置真（见 [py_executor.py:2223-2224](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2223-L2224)），此后每步开头都会因这个标志关掉投机。

投机门由 `acceptance_rate_window_size` / `acceptance_rate_threshold` 两个配置项启用：

[py_executor.py:612-621](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L612-L621) — 仅当两个参数都给定时才创建 `SpeculationGate`。

**`AUTO` 启发式**：

[auto_heuristic.py:1-17](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/auto_heuristic.py#L1-L17) — `suggest_spec_config(max_batch_size)` 在用户选 `AUTO` 时返回一个 NGram 配置：小 batch（≤4）用更长草稿（`max_draft_len=5`）、大 batch 用更短（`max_draft_len=3`），并设 `max_concurrency=32`——batch 上去就自动关。选 NGram 正是因为它零开销、零权重，做 fallback 最安全。

**动态树**。EAGLE3 默认线性链，可选动态树（文档 [speculative-decoding.md:53-81](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/speculative-decoding.md#L53-L81)）：

[eagle3_dynamic_tree.py:173](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/eagle3_dynamic_tree.py#L173) — `Eagle3OneModelDynamicTreeWorker` 继承自 `Eagle3OneModelWorker`，重写树形起草逻辑。文档明确：动态树要求 `max_draft_len <= max_total_draft_tokens <= dynamic_tree_max_topK * max_draft_len`，且**不支持滑动窗口注意力或 MLA**（DeepSeek、gpt-oss 等不可用）。

**CUDA Graph 兼容的 padding**。草稿长度被 `draft_len_schedule` 动态调小后，CUDA Graph（静态形状）需要把 `py_draft_tokens` pad 回静态最大值：

[drafter.py:74-104](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L74-L104) — `pad_draft_tokens_for_cuda_graph` 把草稿补 0 到 `_static_max_total_draft_tokens`。注释解释了 `_needs_padding_kv_extension` 的来由：用 `TorchSampler` 的 drafter（NGram、双模型）从 `len(py_draft_tokens)`（含 padding）算回退长度，所以 padding 后还要扩 KV cache 容量；而单模型 drafter（MTP/EAGLE3/SA）用 `SpecSamplerBase`，从 `runtime_draft_len` 算，padding 无害。

#### 4.4.4 代码实践

**目标**：定位 draft 长度如何动态调整（`draft_len_schedule` / `max_concurrency` / `SpeculationGate` / `AUTO`）——本讲指定实践任务。

**操作步骤**（源码阅读 + 配置实验型）：

1. **跟配置**：在 `llmapi/llm_args.py` 找 `NGramDecodingConfig` / `Eagle3DecodingConfig`，确认 `max_concurrency`、`draft_len_schedule`、`acceptance_rate_window_size`、`acceptance_rate_threshold`、`use_dynamic_tree` 等字段存在。
2. **跟调用链**：从 [py_executor.py:3615-3639](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3615-L3639) 出发，画出本步决策树：

   ```text
   有 draft_len_schedule? ──是──> get_draft_len_for_batch_size(batch) ──> 新长度
                                                              │
            ┌────────── 新长度==0? ──是──> 关闭                   │
            │ 否                                                 │
            ├── speculation_permanently_disabled? ──是──> 关闭     │
            │ 否                                                 │
            └── should_use_spec_decode(max_concurrency 门控) ──> 开/关
   ```

3. **跟投机门数据源**：找到调用 `record_acceptance_rate` 的地方（见 4.5.3），确认喂给门的接受率来自 `_update_batch_acceptance_rate`。
4. （可选，**待本地验证**）构造一个 `draft_len_schedule={1:4, 4:2, 8:0}` 的配置，在不同并发下观察日志 `Use spec decode: ...` 与草稿长度的变化。

**需要观察的现象**：并发上升时草稿长度阶梯式下降，到阈值彻底关闭；接受率持续低迷时投机门打印 `Speculative decoding disabled: rolling acceptance rate avg ... < threshold ...`。

**预期结果**：能说清「动态 draft 长度有两条独立机制」——`draft_len_schedule` 是**用户预设的、按 batch 的阶梯**，`SpeculationGate` 是**运行时按真实接受率的反馈式关闭**，二者都最终落到 `use_spec_decode` 标志。

#### 4.4.5 小练习与答案

**练习 1**：`should_use_spec_decode` 在 `max_concurrency is None` 时为什么直接返回 `True`？
**参考答案**：默认策略是「能开就开」——用户没显式给并发上限，说明没表达「batch 大了要关」的意图，而小 batch 下投机几乎稳赚，所以默认开启（见 [drafter.py:52-53](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L52-L53)）。真要防大 batch 亏损，靠 `AUTO` 启发式自动注入 `max_concurrency=32`。

**练习 2**：`pad_draft_tokens_for_cuda_graph` 为什么对 NGram 要额外扩 KV cache、对 EAGLE3 单模型不用？
**参考答案**：NGram 的 `TorchSampler` 用含 padding 的 `len(py_draft_tokens)` 算回退长度，padding 让有效长度变大，必须扩 KV cache 容量匹配；EAGLE3 单模型的 `SpecSamplerBase` 用 `runtime_draft_len`（真实草稿长，不含 padding），padding 不影响回退，故无需扩容（见 [drafter.py:67-72](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L67-L72)）。

### 4.5 接受率统计与 dummy 请求的排除

#### 4.5.1 概念说明

PyExecutor 里其实有**两条**接受率聚合，用途不同，务必区分：

1. **迭代统计（`specdec_stats`）**：每步把 `num_draft_tokens` / `num_accepted_tokens` / `acceptance_length` 汇总进 `IterationStats`，供指标导出与监控。`acceptance_length` = `(总接受 + 有草稿的请求数) / 有草稿的请求数`，即「每步平均产出几个 token」。
2. **投机门（`SpeculationGate`）**：把本步批次的 `总接受 / 总草稿` 作为一次样本喂给滚动窗口，用于「是否永久关闭投机」的判定。

两条都要做同一件关键事：**排除 `is_dummy` 假请求**。

`is_dummy` 请求是什么？在 **attention 数据并行（attention-DP）**下，为了让每个 DP rank 都至少有一个活跃请求（否则集体通信会挂起），PyExecutor 会**造一个假的 generation 请求**来填充；CUDA Graph 的静态形状 padding 也会产生类似的填充请求。这些假请求的「草稿」是 padding 出来的垃圾，接受率毫无意义。如果把它们算进分母，会让 `acceptance_length` 失真、甚至让投机门误判而关掉投机。

#### 4.5.2 核心流程

```text
每步 _handle_responses / 统计阶段:
    for req in generation_requests:
        if req.is_dummy:          # 假请求：跳过
            continue
        total_draft   += req.num_draft_tokens
        total_accepted += req.py_num_accepted_draft_tokens
    acceptance_rate = total_accepted / total_draft
    SpeculationGate.record_acceptance_rate(acceptance_rate)
```

#### 4.5.3 源码精读

**迭代统计里的排除**——`_append_iter_stats`：

[py_executor.py:2024-2062](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2024-L2062) — 注释直说：「exclude attention dp dummy / CUDA Graph padding requests from AL calculation」。关键跳过在 [py_executor.py:2032](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2032)：`if getattr(req, 'is_dummy', False): continue`。随后才算 `acceptance_length`。

**投机门里的排除**——`_update_batch_acceptance_rate`：

[py_executor.py:2191-2225](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2191-L2225) — 同样的守卫在 [py_executor.py:2212](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2212)：`if draft_len <= 0 or request.is_dummy: continue`。算出 `acceptance_rate = total_accepted_tokens / total_draft_tokens` 后喂给 `SpeculationGate.record_acceptance_rate`，若返回 `disabled_now` 则置永久关闭标志。

**假请求从哪来**——`_pad_attention_dp_dummy_request`：

[py_executor.py:5755-5795](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5755-L5795) — 函数注释：用 generation dummy 请求填充，确保每个 attention-DP rank 至少有一个活跃请求。它调 `kv_cache_manager.add_dummy_requests(...)`，置 `is_attention_dp_dummy = True`，并把 dummy 塞进 `active_requests`。

> 这正是 [u3-l2](u3-l2-pyexecutor-step-loop.md) 提到的「最新修复把 attention-DP/CUDA Graph 造出的 `is_dummy` 假请求排除出投机解码接受率统计」的落点——避免 padding 草稿污染分母、误导 `speculation_gate` 误关投机。

**假请求的终止也要特判**——dummy 的 request_id 每轮复用，不能走分离式 PP 终止处理：

[py_executor.py:6574-6583](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6574-L6583) — `_terminate_request` 对 dummy 直接 `_do_terminate_request`，跳过 PP 终止 handler（否则会在 KV cache manager 里留下过期序列）。

**`py_num_accepted_draft_tokens` 从哪来**——单模型路径在采样后回写：

[spec_sampler_base.py:210](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/spec_sampler_base.py#L210) — `req.py_num_accepted_draft_tokens = num_new_tokens - 1`（本步新产出 token 数减去目标模型自己产的那 1 个，即接受的草稿数）。该字段在请求创建时初始化为 0（[llm_request.py:741](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/llm_request.py#L741)）。

#### 4.5.4 代码实践

**目标**：说清为什么两条接受率统计都要跳过 `is_dummy`——本讲指定实践任务。

**操作步骤**（源码阅读型）：

1. 打开 [py_executor.py:2030-2033](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2030-L2033) 与 [py_executor.py:2210-2215](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2210-L2215)，对比两处 `is_dummy` 守卫。
2. 回溯假请求的诞生：读 [py_executor.py:5755-5795](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5755-L5795)，理解它为何存在（attention-DP 集体通信需要每 rank 有请求）。
3. 构造一个**反例论证**（纸面推导）：假设某步有 4 个真实请求（各起草 3 个，平均接受 2 个）+ 1 个 dummy（padding 出 3 个「草稿」，接受 0 个）。
   - 若**不排除**：接受率 = (4×2 + 0) / (4×3 + 3) = 8/15 ≈ 0.53
   - 若**排除**：接受率 = 8 / 12 ≈ 0.67
   - 真实接受率是 0.67，不排除会把它压低到 0.53；若投机门阈值设在 0.6，就会**误关**投机。

**需要观察的现象**：dummy 的草稿是 padding（[py_executor.py:3647-3649](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3647-L3649）会把 `draft_tokens` 初始化为全 0），与真实采样几乎不可能匹配，接受数恒为 0。

**预期结果**：能向别人解释——「假请求是系统为了对齐 DP rank / CUDA Graph 形状而造的占位，它的草稿是垃圾，接受率恒为 0；若计入分母会系统性压低统计值，让监控失真、让投机门在 attention-DP 场景下误关投机，所以两条聚合都显式 `if is_dummy: continue`。」

#### 4.5.5 小练习与答案

**练习 1**：为什么 `acceptance_length` 的公式是 `(总接受 + 有草稿请求数) / 有草稿请求数`，而不是直接用接受率？
**参考答案**：接受率是「草稿命中率」，而用户真正关心的是「每步产出几个 token」。每步每个请求至少产出 1 个目标 token + 接受的草稿数，所以 `(总接受 + 请求数) / 请求数` = 平均每步产出 token 数（即 `acceptance_length`），更直观反映加速效果（见 [py_executor.py:2054-2060](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2054-L2060)）。

**练习 2**：`_update_batch_acceptance_rate` 为什么在非最后 PP rank 上直接返回？
**参考答案**：投机解码的接受判定只在流水线**最后一个 PP stage** 才有完整 logits 可用（见 [py_executor.py:2200-2202](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2200-L2202)），中间 stage 算不出正确接受率，若各 rank 各自统计会导致不一致，故只让 last PP rank 记录。

## 5. 综合实践

把本讲四块知识串起来，完成一次「为某个模型设计投机解码配置并预测其行为」的纸面设计任务：

**任务**：假设你要为一个 `meta-llama/Llama-3.1-8B-Instruct` 模型部署在线服务（`trtllm-serve`），预期并发在 1~16 之间波动，且用户输入里有大量重复模板（如代码补全场景）。

1. **选算法**：在 NGram / EAGLE3 / MTP / SA-增强-EAGLE3 中选一个并说明理由。（提示：Llama 不是原生 MTP 模型，先排除 MTP；代码补全重复性强，可考虑 NGram 或 SA 增强；追求通用最高接受率则 EAGLE3。）
2. **写配置**：参照 [speculative-decoding.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/speculative-decoding.md)，写出对应的 `--config config.yaml` 片段（含 `decoding_type`）。
3. **设计并发策略**：用 `max_concurrency` 或 `draft_len_schedule` 表达「并发上去就降草稿/关投机」，并指出它会在 [py_executor.py:3615-3639](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3615-L3639) 的哪段生效。
4. **预测监控**：说明服务起来后，你会盯哪个指标（`acceptance_length`）判断投机有没有亏本；若持续亏本，`SpeculationGate` 会如何自动兜底（指出阈值由哪两个配置项控制）。
5. **attention-DP 注意事项**：若开了 attention 数据并行，说明为什么不用担心 dummy 请求污染你的监控（指向 4.5 的两处 `is_dummy` 排除）。

**产出**：一份不超过一页的「配置 + 决策依据」文档。运行验证部分如本地无 GPU/模型，标注「待本地验证」。

## 6. 本讲小结

- 投机解码在 decode 这种**带宽受限**阶段用「一次前向验证 K 个草稿」换取更多产出，配合拒绝采样可做到**分布无损**；核心验证用 `torch.cumprod` 找连续接受前缀（[py_executor.py:4808-4815](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4808-L4815)）。
- `speculative/` 有两套统一抽象：双模型的 `Drafter`（[drafter.py:12](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/drafter.py#L12)）与单模型的 `SpecWorkerBase`（[interface.py:1000](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/speculative/interface.py#L1000)），共享 `SpecMetadata` 每步契约，`SpeculativeDecodingMode` 用谓词统一描述各算法能力。
- drafter 家族按代价分档：NGram 免费、SA 精确匹配（可作增强器）、EAGLE3 学习型（生产默认）、MTP 原生、PARD/DFlash/DSpark 并行草稿；选型取决于场景与是否有草稿权重。
- 动态控制有四条独立机制：`max_concurrency` 并发门控、`draft_len_schedule` 按 batch 缩放、`SpeculationGate` 按真实接受率永久关闭、`AUTO` 启发式；动态树用 `max_total_draft_tokens` 预算在每层展开多分支提高接受率。
- 两条接受率聚合（迭代统计 `specdec_stats` 与投机门）都**显式排除 `is_dummy` 假请求**（[py_executor.py:2032](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2032) 与 [py_executor.py:2212](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2212)），否则 attention-DP / CUDA Graph 的 padding 草稿会污染分母、误导监控与投机门。

## 7. 下一步学习建议

- **CUDA Graph 与 torch.compile**：本讲多次提到「草稿长度动态化需 pad 到静态最大值以兼容 CUDA Graph」，这正是 [u10-l4 CUDA Graph 与 piecewise](u10-l4-cuda-graph-and-compile.md) 的主题，建议接着读 `cuda_graph_runner.py`，理解静态形状捕获如何约束投机解码。
- **采样细节**：本讲的接受/拒绝采样与 [u8-l3 Decoder 与 Sampling](u8-l3-decoder-and-sampling.md) 的 sampler 紧密相关；想深入「分布无损」的数学，可读 `pyexecutor/sampler/sampling_utils.py` 里的 `sampling_batch_spec_dec_one_model`。
- **分离式服务下的投机解码**：`is_dummy` 排除与 attention-DP、分离式 PP 终止（[py_executor.py:6574-6583](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6574-L6583)）呼应 [u11-l2 分离式服务](u11-l2-disaggregated-serving.md)，可结合阅读理解跨节点场景下投机解码的额外约束。
- **继续读源码**：动手实现一个 `UserProvidedDecodingConfig`（[speculative-decoding.md:165-178](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/speculative-decoding.md#L165-L178)）——继承 `Drafter`、实现 `prepare_draft_tokens`，是把本讲知识变成代码的最佳练习。
