# 分页 KV 缓存模型接口

## 1. 本讲目标

在上一讲（u9-l4）里，我们已经建立了 C++ 引擎的「计算黑盒」心智模型：`ModelObj` 是一组纯虚接口，`ModelImpl` 经 `FunctionTable` 把每个虚方法翻译成 model lib 里的函数句柄，而 `EngineConfig` 又驱动 `InferrableEngineConfig::InferForKVCache` 反推出 KV cache 容量。这一讲我们钻进那组接口里**最核心、也最容易被忽视**的一类——**KV cache 管理**。

本讲聚焦三个问题：

1. **为什么 KV cache 要「分页」？** 一个 page 是什么，分页解决了什么问题。
2. **序列在 KV cache 里有哪几种生命周期操作？** 新建、分叉、删除、回退、提交推测解码接受的 token 树，分别对应模型接口的哪几个方法。
3. **`page_size` / `max_num_sequence` / `prefill_chunk_size` 等配置参数如何共同决定「能服务多少并发、多长上下文」？**

学完后你应当能：说清分页 KV cache 与朴素连续 KV cache 的差别；逐个解释 `CreateKVCache`、`AddNewSequence`、`ForkSequence`、`RemoveSequence`、`PopNFromKVCache`、`CommitAcceptedTokenTreeNodesToKVCache` 的语义与它们在 `function_table` 里对应的函数名字符串；并能把引擎配置参数换算成「总页数 / 并发数 / 单序列上限」。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u9-l4 的内容。这里简要回顾几个会反复出现的关键术语：

- **KV cache（键值缓存）**：Transformer 自回归解码时，每生成一个 token 都要让新 token 去「注意」之前所有 token 的 Key/Value 投影。为了避免重复计算，这些历史 K、V 被缓存下来。随序列变长，KV cache 是显存大头。
- **model lib**：`mlc_llm compile` 产出的平台专用模型库（`.so` 等）。它导出一组 TVM 函数，其中既有计算函数（prefill/decode/verify），也有 KV cache 管理函数。
- **FunctionTable**：`ModelImpl` 持有的「函数名 → 句柄」查表容器，把 C++ 虚方法翻译成对 model lib 函数的调用。
- **「三层同名」契约**：C++ 方法名 → `FunctionTable` 字段名 → model lib 里的函数名字符串，三者一一对应，是编译期与运行期的胶水。
- **kv_state_kind**：模型元数据里的字段，取值 `kKVCache`（标准注意力）、`kRNNState`（如 Mamba 类线性注意力）、`kHybrid`（混合，如 Gemma3）、`kNone`（无状态）。本讲主要讲 `kKVCache` 这一支。

另外会用到一个生活类比：**操作系统的虚拟内存分页**。如果你了解进程地址空间被切成固定大小的 page、用页表映射到物理帧、多个进程可以共享同一物理帧（写时复制），那么分页 KV cache 几乎是同一套思想在 GPU 显存上的重演。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`cpp/serve/model.h`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h) | `ModelObj` 抽象接口。本讲关心其中的「KV Cache Management」与「Raw Info Query」两组虚方法声明。 |
| [`cpp/serve/model.cc`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc) | `ModelImpl` 的实现：把每个 KV cache 方法翻译成 `FunctionTable` 字段调用，并按 `kv_state_kind` 分支建池。 |
| [`cpp/serve/function_table.cc`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) | 把 model lib 里的函数名字符串解析成句柄，填进 `FunctionTable` 字段。是「名字契约」的落点。 |
| [`cpp/serve/config.h`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h) | `EngineConfigNode` 字段定义：`kv_cache_page_size`、`max_num_sequence`、`max_total_sequence_length`、`max_single_sequence_length`、`prefill_chunk_size` 等。 |
| [`cpp/serve/config.cc`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc) | `InferrableEngineConfig::InferForKVCache`：在显存预算内反推 `max_total_sequence_length`，即「总 KV token 容量」。 |
| [`cpp/serve/engine.cc`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | 引擎初始化处把 `EngineConfig` 的字段逐个透传给 `Model::CreateKVCache`。 |
| [`cpp/serve/engine_actions/new_request_prefill.cc`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc) | prefill 动作里调用 `AddNewSequence` / `ForkSequence`，展示前缀共享的真实用法。 |
| [`cpp/serve/prefix_cache.h`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h) | `PrefixCacheMatchedResult`：决定一个新序列是「新建」还是「从某父序列 fork」，是分叉操作的指令来源。 |

---

## 4. 核心概念与源码讲解

### 4.1 分页 KV cache

#### 4.1.1 概念说明

最朴素的 KV cache 给每个序列分配**一段连续的显存**，长度按「最大上下文」预留给满。这种做法有两个致命问题：

1. **内部碎片**：一个 8K 槽位只用了 200 token，剩下 7800 token 的显存白白空着。
2. **外部碎片**：序列不断创建、销毁、变长，连续显存被切得七零八落，最后「总显存够、但没有一整块大的」。

分页 KV cache（Paged KV Cache，思想来自 vLLM 的 PagedAttention）借鉴操作系统虚拟内存：把 KV 存储切成**固定大小的小块——page**，每个 page 存连续的 `page_size` 个 token 的 K/V。一个序列的 KV 不再要求物理连续，而是用一张**页表（page table）**把逻辑位置映射到物理 page。

- **page_size**：一个 page 承载的连续 token 数（MLC 默认 16）。
- **空闲页池（free page pool）**：所有可用 page 组成的池。序列要存新 token 就从池里领 page，序列结束就把 page 还回池里。
- **写时复制共享（copy-on-write sharing）**：两个序列如果前缀相同，可以**引用同一批物理 page**，只有分叉处之后才各自领新 page。这正是前缀缓存与并发生成（`n>1`）能省显存的根因。

分页把内部碎片限制在「最后一个 page 之内」（最多浪费 `page_size-1` 个 token 的空间），并彻底消除外部碎片——因为不再需要大块连续显存。

#### 4.1.2 核心流程

分页 KV cache 的创建与使用分两步：

```
[引擎初始化]
  EngineConfig ──(kv_cache_page_size, max_num_sequence,
                  max_total_sequence_length, prefill_chunk_size)──▶
  Model::CreateKVCache(...)
      └─▶ ft_.create_kv_cache_func_(...)        # 调 model lib 的建池函数
              └─▶ 申请 N 个物理 page 的显存池 + 空闲页池 + 页表结构
                  N = max_total_sequence_length / page_size（向上取整）

[每步推理]
  序列要写/读 KV
      └─▶ kv_cache_begin_forward(哪些序列、哪些 token 区间)
              └─▶ 从空闲池领 page、登记进页表（按需）
          计算（prefill/decode/verify，注意力按页表取 K/V）
      └─▶ kv_cache_end_forward()
```

关键点：**物理 page 数 = 总 KV token 容量 ÷ page_size**。`max_total_sequence_length` 决定「全局能存多少 token 的 KV」，再除以 `page_size` 就是 page 池大小。`max_num_sequence` 只决定「最多同时挂多少条序列」（页表条目数），不直接占 KV 显存。

#### 4.1.3 源码精读

`ModelObj` 在 `model.h` 里用一段注释点明了「KV cache related」要做的三件事——create / add_new_sequence / remove_sequence：

[cpp/serve/model.h:88-L95（KV cache 函数总览注释）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L88-L95)

`CreateKVCache` 是这一切的入口。它的参数就是分页设计的全部「旋钮」：

[cpp/serve/model.h:L248-L250（CreateKVCache 接口声明）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L248-L250)

上面这段声明配套的注释（`model.h:232-247`）逐字段说明了每个参数：`page_size` 是每页连续 token 数；`max_num_sequence` 是同时挂载序列上限；`max_total_sequence_length` 是单序列最大长度；`prefill_chunk_size` 是 KV cache 中允许同时存在的 KV token 总量上限；`max_history_size` 给 RNN state 回滚用；`prefix_cache_max_num_recycling_seqs` 给前缀缓存回收序列预留槽位（混合模型需要）。

实现位于 `model.cc`，按 `kv_state_kind` 分四种建法。**标准注意力（`kKVCache`）** 这一支最能体现分页：

[cpp/serve/model.cc:L852-L868（CreateKVCache 的 kKVCache 分支）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L852-L868)

可以看到它把五个 `int`/`int64_t` 包成 `Shape` 元组，连同「是否支持滑动窗口」一起喂给 `ft_.create_kv_cache_func_`——也就是 model lib 里那个真正建池的函数。`kv_cache_` 是跨 disco（多卡）的句柄，`local_kv_cache_` 是单卡视角下的同一对象（非 disco 时二者相同）。

那么 `create_kv_cache_func_` 到底指向 model lib 里的哪个函数？答案在 `function_table.cc`，它揭示了**编译期 dispatch pass 留下的两种建池实现**：

[cpp/serve/function_table.cc:L238-L250（建池函数的选择优先级）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L238-L250)

这里是一条「优先级链」：默认优先用 **`create_flashinfer_paged_kv_cache`**（FlashInfer 后端，最快）；如果模型开了滑动窗口、或 FlashInfer 函数不存在，则回退到 **`create_tir_paged_kv_cache`**（TIR 通用实现）；混合模型（`kHybrid`）则同时需要 `create_tir_paged_kv_cache` 与 `create_rnn_state`。还记得 u8-l2 的 `dispatch_kv_cache_creation` pass 吗？正是那个 pass 在编译期「按条件」往 model lib 里塞了 FlashInfer 或 TIR 实现中的某一个/两个，这里运行期才能 `mod_get_func` 取到。这就是「编译期记条件、运行期按存在性选路」的闭环。

#### 4.1.4 代码实践

**实践目标**：跟随 `CreateKVCache` 的调用链，确认「C++ 参数 → FunctionTable → model lib 函数」三层同名契约，并理解 `Shape` 元组的含义。

**操作步骤**：

1. 打开 `cpp/serve/engine.cc` 第 458–466 行，看引擎初始化如何把 `EngineConfig` 的六个字段透传给 `CreateKVCache`：

   [cpp/serve/engine.cc:L458-L466（引擎把 EngineConfig 透传给 CreateKVCache）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L458-L466)

   注意调用顺序：先 `LoadParams()`，再 `SetMaxNumSequence`、`SetPrefillChunkSize`，**最后**才 `CreateKVCache`——因为建池需要知道最终的并发数与 chunk 大小。

2. 跟到 `model.cc` 的 `kKVCache` 分支（上面 4.1.3 引用过的 852–868 行），观察每个 `Shape{...}` 元组里包了哪个字段。

3. 再跟到 `function_table.cc` 的 238–250 行，确认 `create_kv_cache_func_` 在你的模型上会指向哪个函数名。

**需要观察的现象**：`CreateKVCache` 接口的六个参数与 `function_table.cc` 里 `create_kv_cache_func_` 被调用时传入的五个 `Shape`（`max_num_sequence`、`max_total_sequence_length`、`prefill_chunk_size`、`page_size`、`support_sliding_window`）并非一一对应——`max_history_size` 只在 RNN 分支用到，`prefix_cache_max_num_recycling_seqs` 只在混合/RNN 分支用到。

**预期结果**：你能画出 `EngineConfig.kv_cache_page_size=16` → `CreateKVCache(page_size=16, ...)` → `create_kv_cache_func_(..., Shape{16}, ...)` → `create_flashinfer_paged_kv_cache`/`create_tir_paged_kv_cache` 的完整链路。

> 注：本实践为源码阅读型实践，无需运行模型；若要在运行期验证建池结果，可在开启 `verbose` 的引擎启动日志中看到「max KV cache token capacity will be set to …」（来自 4.3 节的 `config.cc`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `kv_cache_page_size` 从 16 调到 1，对显存利用率和性能分别有什么影响？

> **答案**：page_size=1 时内部碎片为零（每 token 一个 page，绝不浪费），KV 显存利用率最高；但页表条目数 = token 总数，页表元数据暴涨、注意力 kernel 需要按「每 token 一次间接寻址」取 K/V，调度开销显著上升，性能下降。page_size 是「显存利用率」与「元数据/kernel 开销」之间的权衡，16 是经验上的甜点。

**练习 2**：为什么 `create_kv_cache_func_` 要优先取 `create_flashinfer_paged_kv_cache`，而不是直接用 `create_tir_paged_kv_cache`？

> **答案**：FlashInfer 是针对注意力高度优化的后端，建池时把后续 prefill/decode/verify 的 attention kernel 也一并绑定到 FlashInfer 实现，吞吐更高。`create_tir_paged_kv_cache` 是 TIR 通用实现，覆盖面广（含滑动窗口）但在 CUDA 上未必最快。运行期靠 `mod_get_func(...).defined()` 探测函数是否存在来选路——这依赖编译期 `dispatch_kv_cache_creation` pass 已按 target 注入了对应实现。

---

### 4.2 序列增删分叉

#### 4.2.1 概念说明

分页 KV cache 里的「序列（sequence）」是一个**逻辑概念**：一条请求（或它的一根生成分支）在 KV cache 里的视图，由一串 page 按顺序拼成。引擎从不直接操作 page，而是通过对序列的操作来间接管理 page 的分配与回收。

`ModelObj` 暴露了一组「序列生命周期」方法，恰好对应序列的五种命运：

| 方法 | 语义 | 对 KV cache 的影响 |
| --- | --- | --- |
| `AddNewSequence(seq_id)` | 声明一条新空序列 | 在页表里登记一条空记录，暂不领 page |
| `ForkSequence(parent, child, fork_pos)` | 从父序列 fork 出子序列 | 子序列**共享**父序列 `[0, fork_pos)` 的 page（引用计数 +1，写时复制） |
| `RemoveSequence(seq_id)` | 删除序列 | 释放该序列独占的 page（共享 page 引用计数 -1，归零才回池） |
| `PopNFromKVCache(seq_id, n)` | 弹出末尾 N 个 token | 用于回退（如推测解码拒绝草稿后回滚） |
| `CommitAcceptedTokenTreeNodesToKVCache(...)` | 提交接受的 token 树节点 | 推测解码校验后，把接受的分支留下、拒绝的分支剪掉 |

其中最关键的是 **`ForkSequence`**：它实现了**写时复制的前缀共享**。子序列 fork 自父序列时不复制任何 KV 数据，只是让页表里那些 page 的引用计数加一；之后子序列写新 token 才领新 page。这意味着：

- **前缀缓存**：新请求若与某历史请求共享前缀，可直接 fork 历史序列，省掉整段前缀的 prefill 计算。
- **并发生成（`n>1`）**：一个请求要出 3 条候选，先 fork 出 3 个子序列共享 prompt 前缀，再各自发散。
- **推测解码**：起草阶段 fork 出「草稿树」，校验后用 `CommitAcceptedTokenTreeNodesToKVCache` 只保留命中分支。

#### 4.2.2 核心流程

以「带前缀缓存的新请求到达」为例，串起这些操作：

```
新请求 R 到达，token = [t0, t1, ..., t999]
  └─▶ 前缀缓存匹配 PrefixCache::InsertSequence(R)
          └─▶ 返回 PrefixCacheMatchedResult {
                  prefilled_offset = 800,      # 前 800 个 token 已在某活跃序列里算过
                  forked_seq_id = 42,          # 从序列 42 fork
                  reused_seq_id = -1           # 没有可整体复用的已完成序列
              }
  ┌─ if forked_seq_id != -1:
  │    Model::ForkSequence(42, R.internal_id, fork_pos=800)
  │       └─▶ 子序列 R 的页表前 800 token 指向序列 42 的 page（引用计数 +1）
  │    prefill 只需算 t800..t999（200 个 token），而非全部 1000 个
  └─ else:
       Model::AddNewSequence(R.internal_id)   # 全新序列，从零 prefill

[推理中：decode 不断追加 token，按需领新 page]

[请求结束 或 被抢占]
  Model::RemoveSequence(R.internal_id)
     └─▶ R 独占 page 回池；与序列 42 共享的 page 引用计数 -1（42 还活着，不回池）

[推测解码校验后]
  Model::CommitAcceptedTokenTreeNodesToKVCache(seq_ids, accepted_leaf_indices)
     └─▶ 保留从根到 accepted_leaf 的路径，剪掉拒绝分支的 page
```

注意 `fork_pos` 的含义：它告诉 KV cache「子序列从父序列的第几个 token 处分叉」。`fork_pos = -1` 表示从父序列末尾 fork（接续）。

#### 4.2.3 源码精读

五个方法在 `model.h` 里的声明很紧凑：

[cpp/serve/model.h:L252-L270（序列增删分叉与提交的接口声明）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L252-L270)

`model.cc` 里每个实现都遵循同一个套路：先判 `kv_state_kind == kNone` 早退，再调 `ft_.` 上的对应字段；混合模型（`kHybrid`）则对 `kv_cache_` 和 `rnn_state_` 各调一次。以 `ForkSequence` 为例：

[cpp/serve/model.cc:L908-L948（AddNewSequence / ForkSequence / RemoveSequence / PopNFromKVCache 实现）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L908-L948)

可以看到 `ForkSequence` 多做了一步 `prefilled_seq_ids_.insert(child_seq_id)`——把子序列标记为「前缀已 prefill」，这样后续 prefill 动作就知道不要重算 fork 来的那段。`CommitAcceptedTokenTreeNodesToKVCache` 则把两个 `vector` 包成 `Shape` 调用，专用于推测解码：

[cpp/serve/model.cc:L950-L957（CommitAcceptedTokenTreeNodesToKVCache 实现）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L950-L957)

这些 `ft_.` 字段在 `function_table.cc` 里都绑到了具体的全局函数名（注意它们都是 `vm.builtin.*` 开头的 TVM runtime 内建，而非 model lib 自带）：

[cpp/serve/function_table.cc:L252-L264（序列操作函数的名字绑定）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L252-L264)

把名字列成表，就是「三层同名契约」在序列操作上的落点：

| C++ 方法 | FunctionTable 字段 | 函数名字符串 |
| --- | --- | --- |
| `AddNewSequence` | `kv_cache_add_sequence_func_` | `vm.builtin.kv_state_add_sequence` |
| `ForkSequence` | `kv_cache_fork_sequence_func_` | `vm.builtin.kv_state_fork_sequence` |
| `RemoveSequence` | `kv_cache_remove_sequence_func_` | `vm.builtin.kv_state_remove_sequence` |
| `PopNFromKVCache` | `kv_cache_popn_func_` | `vm.builtin.kv_state_popn` |
| `CommitAcceptedTokenTreeNodesToKVCache` | `kv_cache_commit_accepted_token_tree_nodes_func_` | `vm.builtin.attention_kv_cache_commit_accepted_token_tree_nodes` |

注意 `kv_state_*` 是通用名（KV cache 与 RNN state 共用），而 `attention_kv_cache_commit_*` 是注意力专属——因为只有注意力才有「token 树」概念。

那么 **`ForkSequence` 在引擎里被谁调用、`fork_pos` 从哪来**？答案在 prefill 动作里，它直接消费前缀缓存的匹配结果：

[cpp/serve/engine_actions/new_request_prefill.cc:L89-L99（按 parent_idx 决定新建还是 fork）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L89-L99)

这段逻辑非常直白：`rsentry->parent_idx == -1` 表示这条分支没有父分支（是请求的主干），于是 `AddNewSequence`；否则 `ForkSequence(parent_internal_id, child_internal_id)`，把 `fork_pos` 留默认 `-1`（接续 fork）。更典型的「带偏移 fork」出现在消费 `PrefixCacheMatchedResult` 时：

[cpp/serve/engine_actions/new_request_prefill.cc:L314-L324（前缀缓存命中后用 fork_pos fork）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L314-L324)

这里 `result.forked_seq_id != -1` 表示前缀缓存找到了一条可 fork 的活跃序列，于是 `ForkSequence(result.forked_seq_id, ..., result.prefilled_offset)`——`prefilled_offset` 就是 `fork_pos`，告诉 KV cache「父序列前 `prefilled_offset` 个 token 的 KV 直接共享给子序列，别重算」。

而 `result` 的结构定义在 `prefix_cache.h`：

[cpp/serve/prefix_cache.h:L38-L56（PrefixCacheMatchedResult 四元组）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h#L38-L56)

四个字段 `prefilled_offset` / `forked_seq_id` / `reused_seq_id` / `reused_seq_pop_last_tokens` 恰好覆盖前缀缓存的四种命中形态：fork 一条活跃序列、整体复用一条已完成的序列（配合 `PopNFromKVCache` 弹掉末尾若干 token）。它们正是 `ForkSequence` / `PopNFromKVCache` 的指令来源。下一讲（u10-l2）会专门讲前缀缓存与 radix tree 本身。

#### 4.2.4 代码实践

**实践目标**：理清「前缀缓存命中 → ForkSequence 跳过部分 prefill」这条调用链，并验证你对 `fork_pos` 的理解。

**操作步骤**：

1. 阅读 [`cpp/serve/prefix_cache.h:L38-L56`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h#L38-L56)，记住 `PrefixCacheMatchedResult` 的四个字段含义。
2. 打开 `cpp/serve/engine_actions/new_request_prefill.cc`，定位到 300–324 行附近的 `if/else` 分支（处理 `forked_seq_id`、`reused_seq_id` 的几种组合）。
3. 追踪 `result.prefilled_offset` 如何作为第三个参数流进 `ForkSequence(..., result.prefilled_offset)`。

**需要观察的现象**：在 302–312 行（`forked_seq_id == -1 && reused_seq_id == -1` 的纯新建分支），调用的是 `AddNewSequence`；在 314–324 行（`forked_seq_id != -1`），调用的是带 `result.prefilled_offset` 的 `ForkSequence`。

**预期结果**：你能用自己的话说清——「前缀缓存命中的本质，是把『新建序列 + 全量 prefill』替换成『fork 父序列 + 只 prefill 增量部分』，而 `fork_pos` 就是这两段之间的切点」。

> 待本地验证：若有 GPU 环境，启动 `mlc_llm serve` 时加 `--verbose`，连续发两条共享长 system prompt 的请求，观察第二条请求的 prefill token 数是否显著少于第一条（前缀缓存命中后只 prefill 增量）。

#### 4.2.5 小练习与答案

**练习 1**：`RemoveSequence` 删除一条与别的序列共享过前缀的序列时，那段共享 page 会被立刻回收到空闲池吗？为什么？

> **答案**：不会。分页 KV cache 用引用计数管理共享 page：fork 时引用计数 +1，remove 时 -1。共享前缀的 page 在 `RemoveSequence` 后引用计数只减到「仍在使用」，只有归零才会回到空闲池。这保证了仍然存活的序列（如被 fork 的父序列）不会因为子序列被删而丢失 KV。

**练习 2**：`CommitAcceptedTokenTreeNodesToKVCache` 接收的 `accepted_leaf_indices` 是什么？它和 `PopNFromKVCache` 都能「丢弃 token」，区别在哪？

> **答案**：推测解码会构造一棵草稿 token 树（每个节点有 parent 指针，见 `BatchVerify` 的 `token_tree_parent_ptr`）。大模型校验后给出「哪些叶子节点被接受」，`accepted_leaf_indices` 就是这些被接受叶子的下标；函数保留从根到这些叶子的路径，剪掉所有拒绝分支的 page。`PopNFromKVCache` 是「从末尾弹出 N 个连续 token」（线性回退），`CommitAcceptedTokenTreeNodesToKVCache` 是「按树形拓扑保留/剪枝」（非线性），二者服务于不同场景——前者用于草稿整体拒绝的简单回退，后者用于树形推测解码的部分接受。

---

### 4.3 分页配置参数

#### 4.3.1 概念说明

`EngineConfigNode`（`config.h`）里有一组字段直接决定分页 KV cache 的「形状」。先把它们列清楚：

| 字段（`EngineConfigNode`） | 默认值 | 控制什么 |
| --- | --- | --- |
| `kv_cache_page_size` | 16 | 每个 page 承载的连续 token 数（分页粒度） |
| `max_num_sequence` | 4 | 同时挂载的序列数上限（并发请求数 + 它们的分支） |
| `max_total_sequence_length` | 4096 | **全局** KV cache 中允许同时存在的 KV token 总量（≈ 决定总 page 池大小） |
| `max_single_sequence_length` | 4096 | 单条序列的最大长度上限 |
| `prefill_chunk_size` | 1024 | 一次 prefill 步骤处理的最大 token 数（分块 prefill 粒度） |
| `max_history_size` | 0 | RNN state 回滚用的最大历史长度（KV cache 不需要） |
| `prefix_cache_max_num_recycling_seqs` | -1 | 前缀缓存保留的回收序列数（-1=无限，0=禁用） |

这里最易混淆的是 `max_total_sequence_length` 与 `max_single_sequence_length`：

- **`max_total_sequence_length`** 是**全局预算**——所有活跃序列的 KV token 加起来不能超过它。它决定了空闲 page 池的总大小：`总 page 数 ≈ max_total_sequence_length / kv_cache_page_size`。这是「能装多少 KV」的硬约束。
- **`max_single_sequence_length`** 是**单序列上限**——任何一条序列的长度不能超过它。这是「单条请求能有多长」的约束（通常由模型的 `context_window_size` 封顶）。

二者共同界定服务能力：

\[ \text{并发数} \approx \frac{\text{max\_total\_sequence\_length}}{\text{平均序列长度}} \le \text{max\_num\_sequence} \]

也就是说，「能同时服务多少并发」受三者共同约束——总预算 ÷ 平均序列长度 给出理论上限，但还要不超过 `max_num_sequence`（页表条目与采样/workspace 的并发开销由它决定）。

`prefill_chunk_size` 是另一个独立维度：它把一条长请求的 prefill **分块**进行（chunked prefill），每步最多算这么多 token，让长 prompt 不会独占整步、可与 decode 交错（hybrid prefill）。它影响的是**单步计算的显存与延迟**，而非 KV cache 容量。

#### 4.3.2 核心流程

这些字段的取值有两条路径：用户显式指定，或引擎在显存预算内**自动反推**。反推逻辑在 `InferrableEngineConfig::InferForKVCache`（`config.cc`），核心是一个「显存记账」公式：

\[ \text{可用显存} = \text{GPU总显存} \times \text{gpu\_memory\_utilization} - \text{权重} - \text{临时buffer} - \text{辅助workspace} \]

\[ \text{max\_total\_sequence\_length} = \left\lfloor \frac{\text{可用显存}}{\text{kv\_bytes\_per\_token}} \right\rfloor \]

其中每 token 的 KV 字节数为：

\[ \text{kv\_bytes\_per\_token} = \text{head\_dim} \times \text{num\_kv\_heads} \times \frac{\text{num\_layers}}{\text{pipeline\_stages}} \times 4 + 1.25 \]

（`×4` = K 与 V 各一份 × fp16 两字节；`+1.25` 是页表等元数据的小额摊销。）

最终这些字段在 `engine.cc` 初始化时被逐个透传给 `Model::CreateKVCache`（见 4.1.4 引用的 458–466 行），完成「配置 → 物理池」的落地。

#### 4.3.3 源码精读

配置字段的定义集中在 `EngineConfigNode`，注释清楚说明了 mode 如何影响默认值（local/interactive/server 三种预设）：

[cpp/serve/config.h:L265-L282（分页 KV cache 的核心配置字段）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L265-L282)

注意 `kv_cache_page_size` 默认 16、`max_num_sequence` 默认 4——这正是 `EngineMode::kLocal`（低并发本地部署）的预设；`server` 模式下引擎会自动反推更大的并发。前缀缓存的开关字段紧随其后：

[cpp/serve/config.h:L284-L290（前缀缓存配置）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L284-L290)

`prefix_cache_mode` 默认就是 `kRadix`（基于 radix tree 的分页前缀缓存，下一讲详讲），`prefix_cache_max_num_recycling_seqs = -1` 表示无限回收容量。

显存记账的核心代码在 `config.cc`，它把所有「非 KV」开销扣除后，用 `kv_bytes_per_token` 反推总容量：

[cpp/serve/config.cc:L701-L727（每 token KV 字节数与总容量反推）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc#L701-L727)

这段代码逐模型累加 `kv_bytes_per_token`（702–709 行）、再算各种 workspace（710–716 行），最后用「(总显存×利用率 − 各项固定开销) ÷ kv_bytes_per_token」得到 `model_max_total_sequence_length`（720–727 行）。注意它还把 `prefill_chunk_size` 算进了 `kv_aux_workspace_bytes` 与 `model_workspace_bytes`——所以**调大 `prefill_chunk_size` 会吃掉一部分本可给 KV cache 用的显存，间接降低可服务的总 token 数**。

得到 `model_max_total_sequence_length` 后，再按 mode 收敛成最终的 `max_total_sequence_length`：

[cpp/serve/config.cc:L763-L800（按 mode 定稿 max_total_sequence_length 与 prefill_chunk_size）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc#L763-L800)

可以看到三种 mode 的差别：`kLocal` 再与 8192 取 min（保守）、`kInteractive` 只与单序列上限取 min、`kServer` 直接取 `max_num_sequence × max_single_sequence_length`（激进，榨干显存换并发）。日志里那句「max KV cache token capacity will be set to …」就出自这里（779–780 行）。

最后，所有这些字段在 `engine.cc` 的初始化里被一次性透传给 `CreateKVCache`，完成「配置 → 物理池」：

[cpp/serve/engine.cc:L461-L466（六个配置字段透传给 CreateKVCache）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L461-L466)

注意第 466 行把 `prefix_cache_max_num_recycling_seqs` 也传了进去——它在 `kRNNState`/`kHybrid` 分支里会让 RNN state 多开 `max_num_sequence + prefix_cache_max_num_recycling_seqs` 个槽位（见 `model.cc` 871、895 行），让回收序列能与活跃序列同时驻留。

#### 4.3.4 代码实践

**实践目标**：用真实数字把「三个参数如何共同决定并发数与上下文长度」算清楚，并解释 `ForkSequence` 如何支持前缀共享。

**操作步骤**：

1. 假设一个 Llama-3-8B 模型：`head_dim=128`、`num_kv_heads=8`（GQA）、`num_layers=32`、无 pipeline 并行。代入 `config.cc` 707–709 行的公式估算每 token KV 字节数（示例计算，非项目内置常数）：

   \[ 128 \times 8 \times 32 \times 4 + 1.25 = 131073 \approx 128 \text{ KiB/token} \]

2. 假设 16GB 可用显存（`gpu_memory_utilization` 扣除权重/workspace 后）全部给 KV cache，反推总容量：

   \[ \text{max\_total\_sequence\_length} \approx \frac{16 \times 1024^3}{131073} \approx 131000 \text{ token} \]

   若 `kv_cache_page_size = 16`，则总 page 数 ≈ 8192。

3. 对照 `EngineConfigNode` 默认值（`config.h` 266–280 行）与 mode 收敛逻辑（`config.cc` 763–778 行），填写下表（示例推演，待本地验证）：

   | 场景 | max_num_sequence | max_single_sequence_length | 理论并发上限 |
   | --- | --- | --- | --- |
   | local（默认） | 4 | 8192 | min(4, 131000/平均长度) |
   | server | 反推（如 32） | 4096 | min(32, 131000/4096)≈32 |

4. 解释前缀共享：阅读 [`new_request_prefill.cc:L314-L324`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L314-L324)，说明当 `forked_seq_id != -1` 时，`ForkSequence(..., prefilled_offset)` 让子序列共享父序列前缀的 page，**不消耗新的 KV 显存**——等于在「总预算不变」的前提下「免费」复用了已算过的前缀，从而在效果上提升了并发能力。

**需要观察的现象**：调大 `prefill_chunk_size`（如 1024→2048）时，`config.cc` 710–714 行的 `kv_aux_workspace_bytes`/`model_workspace_bytes` 会增大，导致 `model_max_total_sequence_length` 减小——chunk size 与 KV 容量是**此消彼长**的。

**预期结果**：你能口头说清「`max_total_sequence_length` 决定总池子大小（÷page_size=页数）、`max_single_sequence_length` 决定单条上限、`max_num_sequence` 决定页表条目与并发开销上限；三者中前两者受显存硬约束，且 `prefill_chunk_size` 通过 workspace 间接挤压 KV 容量」。前缀缓存（`ForkSequence`）不增加总预算，但通过共享 page 让「逻辑并发数」超过「物理容量 ÷ 平均长度」的朴素估算。

> 待本地验证：以上数字为示例推演。可在 `--verbose` 启动日志里读到引擎实际反推出的 `max KV cache token capacity`，与你的估算对照。

#### 4.3.5 小练习与答案

**练习 1**：用户把 `prefill_chunk_size` 设得很大（比如等于完整上下文），会同时影响哪两个量？方向如何？

> **答案**：一是单步 prefill 的 workspace 显存增大（`config.cc` 710–714 行的 `kv_aux_workspace_bytes`、`model_workspace_bytes` 都含 `prefill_chunk_size` 因子），从而挤压 KV cache 容量、降低 `max_total_sequence_length`；二是单步 prefill 延迟上升、与 decode 交错变难（hybrid prefill 的优势消失）。所以 chunk size 并非越大越好，它是在「长 prompt 吞吐」与「显存/延迟」之间的权衡。

**练习 2**：`max_total_sequence_length` 与 `max_single_sequence_length` 哪个直接决定空闲 page 池大小？为什么 `server` 模式下 `max_total_sequence_length` 可以远大于 `max_single_sequence_length`？

> **答案**：`max_total_sequence_length` 直接决定空闲 page 池大小（总 page 数 ≈ 它 ÷ `page_size`）。`server` 模式按 `max_num_sequence × max_single_sequence_length` 反推总容量（`config.cc` 773–777 行），意思是「我打算同时跑 `max_num_sequence` 条、每条最长 `max_single_sequence_length` 的序列」，所以总预算是二者的乘积，远大于单条上限。这就是服务器模式用显存换并发的方式。

**练习 3**：为什么 `prefix_cache_max_num_recycling_seqs` 只在 RNN/Hybrid 模型里需要「额外预留槽位」（`model.cc` 871/895 行的 `+ prefix_cache_max_num_recycling_seqs`），而标准 KV cache 不需要？

> **答案**：标准分页 KV cache 的回收序列不占独立的连续存储——它们的 page 与活跃序列共享同一个池子，靠引用计数管理，所以不需要额外槽位。而 RNN state 是**每序列一份连续状态**（不能像 page 那样被任意分割共享），回收序列要与活跃序列**同时**驻留（例如 fork 时父序列还得在），就必须为它们预留独立的 batch 槽位，所以 `max_num_sequence` 上要叠加 `prefix_cache_max_num_recycling_seqs`。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**追踪一条带前缀缓存的请求如何落地到分页 KV cache**」的综合阅读任务：

**任务**：假设你用 `mlc_llm serve --model-lib ... --model ... --mode server` 启动引擎，连续收到两条请求 R1、R2，它们共享一段 500 token 的 system prompt，随后各自不同。

1. **建池阶段**（对应 4.1）：
   - 在 [`engine.cc:L461-L466`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L461-L466) 找到 `CreateKVCache` 的调用，列出它实际传入的六个值分别来自 `EngineConfigNode` 的哪个字段。
   - 在 [`function_table.cc:L238-L250`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L238-L250) 判断你的模型会用 `create_flashinfer_paged_kv_cache` 还是 `create_tir_paged_kv_cache`。

2. **R1 到达**（对应 4.2 + 4.3）：
   - 前缀缓存首次未命中，走 [`new_request_prefill.cc:L302-L312`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L302-L312) 的「纯新建」分支，调 `AddNewSequence`，全量 prefill。说明这一步会从空闲 page 池领走 ⌈prompt长度/page_size⌉ 个 page。

3. **R2 到达**（对应 4.2）：
   - 前缀缓存命中 R1 的前 500 token，走 [`new_request_prefill.cc:L314-L324`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L314-L324) 的 fork 分支，调 `ForkSequence(R1.internal_id, R2.internal_id, 500)`。说明 R2 的页表前 500 token **指向 R1 的 page**（引用计数 +1，不领新 page），R2 只需 prefill 第 500 token 之后的增量。

4. **容量核算**（对应 4.3）：
   - 用 [`config.cc:L701-L727`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc#L701-L727) 的公式，估算你的 GPU 在 server 模式下能反推出多大的 `max_total_sequence_length`，并据此说出「引擎最多能同时挂多少条像 R1、R2 这样的请求」。
   - 解释：因为有 `ForkSequence` 的前缀共享，R2 的 500 token 前缀**不额外占 KV 预算**，所以「逻辑并发能力」高于「物理容量 ÷ 平均序列长度」的朴素估算。

**交付物**：一张时序图（建池 → R1 新建并 prefill → R2 fork 并增量 prefill → 各自 decode → 结束 remove），并在图旁标注每一步调用了 `ModelObj` 的哪个方法、消耗/释放了多少 page。

> 待本地验证：以上为源码阅读推演。若在 GPU 上运行，可用 `--verbose` 观察日志里的 KV cache 容量与每步 prefill 的 token 数，验证 fork 是否真的只 prefill 了增量。

## 6. 本讲小结

- **分页 KV cache** 把 KV 存储切成固定大小的 page（默认 `kv_cache_page_size=16`），用页表把逻辑序列映射到物理 page，把内部碎片限制在「最后一个 page」之内、彻底消除外部碎片；建池由 `CreateKVCache` 经 `create_kv_cache_func_` 完成，运行期按 `mod_get_func(...).defined()` 在 FlashInfer 与 TIR 实现间选路。
- **序列的五种生命周期操作**——`AddNewSequence`（声明）、`ForkSequence`（写时复制共享前缀）、`RemoveSequence`（引用计数回收）、`PopNFromKVCache`（末尾回退）、`CommitAcceptedTokenTreeNodesToKVCache`（推测解码剪枝）——是引擎管理 page 的唯一接口，它们各自绑定到 `vm.builtin.kv_state_*` / `attention_kv_cache_*` 全局函数。
- **`ForkSequence(parent, child, fork_pos)`** 是前缀缓存与并发生成的物理基础：子序列 fork 后共享父序列 `[0, fork_pos)` 的 page（引用计数 +1），不复制数据、不额外占 KV 预算；`fork_pos` 由 `PrefixCacheMatchedResult.prefilled_offset` 提供。
- **三个核心配置参数分工**：`max_total_sequence_length` 决定全局 KV token 预算（÷page_size = 总页数）、`max_single_sequence_length` 决定单条上限、`max_num_sequence` 决定页表条目与并发开销上限；前两者由 `InferForKVCache` 在显存预算内反推，`prefill_chunk_size` 通过 workspace 间接挤压 KV 容量。
- **每 token KV 显存**约为 `head_dim × num_kv_heads × (num_layers/pipeline_stages) × 4 + 1.25` 字节（fp16、含 K/V），这是估算上下文容量的基本公式。
- **mode 决定激进程度**：`local` 保守（与 8192 取 min）、`interactive` 单并发、`server` 按 `max_num_sequence × max_single_sequence_length` 榨干显存换并发。

## 7. 下一步学习建议

本讲只讲了「分页 KV cache 的模型接口」——即引擎如何通过 `ModelObj` 操作序列与 page。接下来值得继续深入的方向：

1. **u10-l2 前缀缓存与 Radix Tree**：本讲多次出现的 `PrefixCacheMatchedResult`、`ForkSequence` 的「指令来源」就在 `prefix_cache.h` 与 `radix_tree.h`。下一讲会讲清 radix tree 如何按 token 路径存储序列、`MatchPrefix` 如何找到最长公共前缀、以及回收/驱逐策略。
2. **u10-l3 采样器**：分页 KV cache 只负责「存历史 K/V」；采样在 KV 之上发生，了解 CPU/GPU 采样器与 `CommitAcceptedTokenTreeNodesToKVCache` 的衔接。
3. **u10-l4 推测解码动作链**：本讲的 `CommitAcceptedTokenTreeNodesToKVCache` 是推测解码「校验后提交」的一环，结合 `batch_verify` / `eagle_batch_verify` 能看到 token 树从起草到剪枝的全貌。
4. **直接阅读 TVM runtime 的 KV cache 内建实现**：函数名字符串 `vm.builtin.kv_state_*` 的实现不在本仓库，而在 `3rdparty/tvm`（或其定制 fork）里。如果你想知道引用计数、page 分配的具体实现，可以 `Grep "kv_state_fork_sequence"` 进 TVM 源码继续追踪。
