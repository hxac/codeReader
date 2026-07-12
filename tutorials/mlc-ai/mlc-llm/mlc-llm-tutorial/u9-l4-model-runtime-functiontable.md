# 模型运行时与 FunctionTable

## 1. 本讲目标

在 [u9-l1](u9-l1-engine-threaded-state.md) 里，我们认识了 `ThreadedEngine` 这个「外壳」与 `EngineState` 这个「状态容器」，但故意留下一个问题没有回答：**引擎真正要算的模型（一次 prefill、一次 decode、一次采样）到底由谁来执行？这些计算函数又是从哪里冒出来的？**

本讲就打开这个黑盒。读完本讲，你应当能够：

1. 说出 `ModelObj` 这层 C++ 抽象对外暴露了哪些「计算函数」与「KV cache 接口」，并能区分 **C++ 方法名** 与 **model lib 里的 TVM 函数名**。
2. 解释 `FunctionTable` 如何在引擎启动时加载 `.so`/`.tar` 模型库、把它变成一堆可调用的 TVM packed function 句柄，并理解「按名字符串查函数」这条编译期↔运行期契约。
3. 看懂 `EngineConfig` 里那些直接影响 KV cache 容量与引擎并发能力的关键字段（`kv_cache_page_size`、`max_num_sequence`、`prefill_chunk_size`、`gpu_memory_utilization` 等），以及它们如何被传入 `Model->CreateKVCache(...)`。

本讲是「编译产物驱动引擎」这条主线的运行期落点：编译器（U7/U8）产出什么，本讲的 `FunctionTable` 就吃什么。

## 2. 前置知识

- **TVM Runtime / VM**：MLC 编译器把模型编译成一个 TVM「可执行模块（executable）」，它内部是一段字节码 + 一堆 PrimFunc。运行时通过 `vm_load_executable` 把它加载成可调用对象，再用名字取其中的函数。本讲的 `FunctionTable` 就是这套机制在 MLC 服务端的具体封装。
- **packed function**：TVM 的跨语言函数抽象。一个 C++ 函数或 Python 函数都能包成一个 `tvm::ffi::Function`，按位置参数调用、按返回值取回。它是 C++ 引擎与 TVM VM 之间的统一调用约定。
- **object system（`Object` / `ObjectRef`）**：TVM 的引用计数对象体系。`ModelObj` 是「对象本体」（裸指针语义），`Model` 是它的引用包装（智能指针语义），与 `EngineObj`/`Engine`、`RequestObj`/`Request` 完全同构——这是 [u9-l1](u9-l1-engine-threaded-state.md) 已建立的写法。
- **KV cache / 分页 KV cache**：Transformer 自回归解码需要缓存历史 token 的 K/V。MLC 用「分页（paged）」方式管理，类似操作系统的分页内存，把连续 token 切成固定大小的 page。详细机制见 [u10-l1](u10-l1-paged-kv-cache.md)，本讲只讲 C++ 侧的调用接口。
- **disco / 张量并行（tensor parallel）**：多卡时 MLC 用 TVM 的 disco 会话把同一份计算分发到多张卡上。`num_workers = num_shards * num_stages > 1` 时 `FunctionTable` 走 disco 分支，否则走单卡本地分支。

> 名词速查：**model lib** = `compile` 产出的平台专用库（`.so`/`.dylib`/`.dll`/`.tar`/`.wasm`）；**model_path** = 模型权重目录；**model_config** = `mlc-chat-config.json` 解析出的 JSON 对象。三者一起喂给 `Model::Create`。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [cpp/serve/model.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h) | 模型运行时的**抽象接口** | `ModelObj` 基类声明所有计算函数与 KV cache 接口；`Model` 引用包装 + `Create()` 工厂；`ModelWorkspace` 共享张量 |
| [cpp/serve/model.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc) | 模型运行时的**实现** | `ModelImpl`：把每个 C++ 方法翻译成对 `FunctionTable` 里某个函数句柄的调用 |
| [cpp/serve/function_table.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.h) | **函数句柄表**（声明） | `FunctionTable` 结构体：几十个 `Function` 字段、`Init`、`_InitFunctions`、`LoadParams` 等 |
| [cpp/serve/function_table.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) | **函数句柄表**（实现） | 加载模型库、按名字符串解析出每个 packed function、加载权重 |
| [cpp/serve/config.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h) | **引擎配置** | `EngineConfigNode`（KV cache、并发、推测解码、前缀缓存等）、各 enum、`InferrableEngineConfig` |
| [cpp/metadata/model.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/metadata/model.h) | **模型元数据** | `ModelMetadata`（编译期写进 model lib 的自我描述：`tensor_parallel_shards`、`kv_state_kind` 等）、`KVStateKind` 枚举 |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | **编排者** | 引擎如何依次 `Model::Create` → `LoadParams` → `CreateKVCache`，用 `EngineConfig` 把模型装配起来 |

一张总览图（数据从左到右流动）：

```
 mlc_llm compile                 Engine 启动 (engine.cc)
 ────────────────                ───────────────────────────
 Relax IRModule   ──build──▶  model lib (.so)  ──Model::Create──▶  FunctionTable.Init
        +                          +                                    │
  _metadata JSON  ─────写入──▶  model lib                            加载 + 按名解析
                                                                           │
                                                                           ▼
                                                                  ModelImpl (持 ft_)
                                                                           │
                                  EngineConfig  ──CreateKVCache──▶  KV cache 句柄
                                                                           │
                                                                  Action 循环 (u9-l2)
                                                                  调 TokenEmbed / BatchPrefill / ...
```

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**Model 计算函数**、**FunctionTable 加载**、**EngineConfig 配置**。三者关系是「接口 → 实现 → 参数」。

### 4.1 ModelObj：模型计算函数的统一抽象

#### 4.1.1 概念说明

`ModelObj` 是 C++ 引擎看到的「一台模型」——一个拥有内部 KV cache、能执行 prefill/decode/verify 等前向计算的对象。注意它**不是**模型权重的算术实现，而是一层**接口（abstract base class）**：所有 `= 0` 的纯虚方法只是约定「我能做什么」，真正的计算被委托给 `FunctionTable` 里那些从 model lib 解析出来的 TVM 函数。

这样设计的好处是**解耦**：

- 引擎（`EngineImpl`）、动作（`EngineActionObj`，见 [u9-l2](u9-l2-action-loop.md)）只依赖 `ModelObj` 这个稳定接口，不关心底层是 FlashInfer 还是 TIR、是单卡还是多卡。
- 具体实现 `ModelImpl`（在 model.cc 里）持有 `FunctionTable ft_`，把每个虚方法翻译成一次 `ft_.xxx_func_(...)` 调用。
- 编译期（U7/U8）产出的函数集是可变的（不同后端附加不同 pass），运行期只要函数名对得上就能跑——靠的是「函数名字符串」这条契约。

`ModelObj` 的能力可分四类（与 [model.h:L71-L94](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L71-L94) 的文档注释一致）：

1. **模型计算**：`TokenEmbed` / `ImageEmbed` / `BatchPrefill` / `BatchDecode` / `BatchVerify` / `GetLogits` / `FuseEmbedHidden`（及一组 `*ToLastHidden` 变体，服务于 EAGLE 推测解码）。
2. **KV cache 管理**：`CreateKVCache` / `AddNewSequence` / `ForkSequence` / `RemoveSequence` / `PopNFromKVCache` / `CommitAcceptedTokenTreeNodesToKVCache` / `EnableSlidingWindowForSeq` / `DisaggPrepareKVRecv` / `DisaggMarkKVSend`。
3. **原始信息查询**：`GetMetadata` / `GetNumAvailablePages` / `GetCurrentTotalSequenceLength`。
4. **工具**：`LoadParams` / `Reset` / `CreateLogitProcessor` / `CreateSampler` / `AllocEmbeddingTensor` 等。

#### 4.1.2 核心流程

一次「`NewRequestPrefill` 动作」要调用的模型函数链（简化伪代码）：

```text
# 1) 把 token id 变成 embedding（查表）
embeddings = model->TokenEmbed(token_ids, &dst_workspace, offset)

# 2) 告诉 KV cache「这一批 seq 各要前进多少 token」
model-> 内部调用 kv_cache_begin_forward(seq_ids, lengths)

# 3) 前向：embedding 进，logits 出（只在需要下一个 token 的位置）
logits = model->BatchPrefill(embeddings, seq_ids, lengths)

# 4) 取最后一个位置的概率分布
probs = softmax_with_temperature(logits, temperature)   # 也是 model lib 里的函数

# 5) 采样（CPU 或 GPU sampler）
next_token = sampler->Sample(probs)
```

解码阶段则是把第 2、3 步换成 `BatchDecode`（每个序列只前进一步）；推测解码的校验阶段换成 `BatchVerify`（一次校验一棵 token 树）。

`ModelWorkspace`（[model.h:L42-L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L42-L69)）是「由 Model 创建、由 Engine 持有」的共享张量集合（`embeddings` / `hidden_states` / `draft_probs` 等），让多个动作之间能复用同一块显存，避免每步重新分配。

#### 4.1.3 源码精读

`ModelObj` 的整体声明在 [cpp/serve/model.h:L95-L383](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L95-L383)：定义了上面四类纯虚方法。

最关键的是看「C++ 方法 → FunctionTable 字段 → model lib 函数名」这三层映射。先看 `TokenEmbed` 的实现（[cpp/serve/model.cc:L87-L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L87-L126)），它做三件事：把 token id 拷到设备 → 调 `ft_.embed_func_` → 把结果写回 workspace 或直接返回：

```cpp
// 把 token ids 拷到 worker0（单卡时就是本机 GPU）
token_ids_dref_or_nd = ft_.CopyToWorker0(token_ids_nd, "token_ids", {prefill_chunk_size_});
// 真正的计算：调用 model lib 里名为 "embed" 的函数
ObjectRef embeddings = ft_.embed_func_(token_ids_dref_or_nd, params_).cast<ObjectRef>();
```

再看 `GetLogits`（[model.cc:L160-L184](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L160-L184)）：它把 `hidden_states` 与模型权重 `params_` 一起喂给 `get_logits` 函数（本质是 lm_head 投影）。注意它对 disco 多卡的兼容：当处于 disco 模式时，返回值要先从 worker0 取回（`DebugGetFromRemote(0)`）。

`BatchPrefill`（[model.cc:L245-L290 附近](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L245-L290)）展示了 KV cache 的「进入-计算-退出」三明治结构：

```cpp
// 计算「每条序列最后一个 token 在拼接后的大序列中的位置」，作为 logit_pos
for (int i = 0; i < num_sequences; ++i) {
  total_length += lengths[i];
  p_logit_pos[i] = total_length - 1;
}
// ① 进入 forward：告诉 KV cache 接下来每条 seq 要新增多少 token
ft_.kv_cache_begin_forward_func_(kv_cache_, seq_ids_tuple, lengths_tuple);
// ② 前向计算（embedding 进，logits 出）
//    内部调用 ft_.prefill_func_(...) —— 即 model lib 的 "batch_prefill"
// ③ 退出 forward（在函数末尾）：kv_cache_end_forward_func_
```

> 为什么 prefill 要传 `logit_pos`？因为一次 prefill 拼了多条序列，模型一次性算出所有位置的 hidden_states，但**只有每条序列的最后一个位置**需要 logits（用于预测下一个 token）。`logit_pos` 就是这些「要取 logits 的位置」的索引，让 `get_logits` 只投影这些行，省算力。

KV cache 接口侧，`CreateKVCache`（[model.cc:L852-L906](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L852-L906)）根据元数据里的 `kv_state_kind` 分三种建法（`kKVCache` / `kRNNState` / `kHybrid`），对应不同的底层函数与不同的参数形状（详见 4.3）。`AddNewSequence`（[model.cc:L908-L916](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L908-L916)）则非常薄，直接转调：

```cpp
void AddNewSequence(int64_t seq_id) final {
  if (this->kind == KVStateKind::kNone) return;
  ft_.kv_cache_add_sequence_func_(kv_cache_, seq_id);
  if (kind == KVStateKind::kHybrid) {
    ft_.kv_cache_add_sequence_func_(rnn_state_, seq_id);   // 混合模型还要在 RNN state 里也加一条
  }
}
```

`Model::Create` 工厂在 [model.h:L399-L402](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L399-L402)，它的实现（[model.cc:L33-L39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L33-L39)）只是 `new` 一个 `ModelImpl`。真正干活的是 `ModelImpl` 构造函数（[model.cc:L66-L83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L66-L83)）：

```cpp
// Step 1. 解析 model_config JSON
LoadModelConfigJSON(model_config);
// Step 2. 加载模型库并初始化所有函数句柄（本讲重点）
this->ft_.Init(reload_lib_path, device_, model_config, session, num_shards, num_stages);
// 从 model lib 的元数据里读回 TP/PP 度量
this->num_shards_ = ft_.model_metadata_.tensor_parallel_shards;
this->num_stages_ = ft_.model_metadata_.pipeline_parallel_stages;
// Step 3. Reset  Step 4. 记录模型类型
this->Reset();
this->kind = GetMetadata().kv_state_kind;
```

#### 4.1.4 代码实践

**实践目标**：建立「C++ 方法名 ↔ FunctionTable 字段 ↔ model lib 函数名」的三层映射直觉。

**操作步骤**：

1. 打开 [cpp/serve/model.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h)，在 `ModelObj` 里找到 `TokenEmbed`（约 L111）、`BatchPrefill`（约 L155）、`BatchDecode`（约 L178）、`BatchVerify`（约 L207）、`GetLogits`（约 L142）、`CreateKVCache`（约 L248）这 6 个方法的签名，抄下它们的**参数与返回类型**。
2. 打开 [cpp/serve/function_table.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) 的 `_InitFunctions`（L207 起），找到每个方法**实际调用**的 `Function` 字段（如 `embed_func_`、`prefill_func_`、`decode_func_`、`verify_func_`、`get_logits_func_`、`create_kv_cache_func_`）以及它从 model lib 里取的**字符串函数名**（如 `"embed"`、`"batch_prefill"`、`"batch_decode"`、`"batch_verify"`、`"get_logits"`、`"create_flashinfer_paged_kv_cache"`）。
3. 打开 [cpp/serve/model.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc)，在 `TokenEmbed`（L87）、`GetLogits`（L160）、`BatchPrefill`（L245）的实现里确认「C++ 方法 = 调用对应 `ft_.xxx_func_`」。

**需要观察的现象**：你会发现一个干净的「三层同名」规律——`BatchPrefill` 调 `prefill_func_`，而 `prefill_func_` 来自字符串 `"batch_prefill"`；这个字符串正是 [u3-l2](u3-l2-relax-nn-model.md) 里 `get_default_spec()`/`export_tvm(spec=...)` 在编译期声明的 Relax 函数名。

**预期结果**：整理出如下对照表（请自行补全）：

| C++ 方法 (ModelObj) | FunctionTable 字段 | model lib 函数名字符串 |
| --- | --- | --- |
| `TokenEmbed` | `embed_func_` | `"embed"` |
| `GetLogit`s | `get_logits_func_` | `"get_logits"` |
| `BatchPrefill` | `prefill_func_` | `"batch_prefill"` |
| `BatchDecode` | `decode_func_` | `"batch_decode"` |
| `BatchVerify` | `verify_func_` | `"batch_verify"` |
| `CreateKVCache` | `create_kv_cache_func_` | `"create_flashinfer_paged_kv_cache"` 或 `"create_tir_paged_kv_cache"` |

> 说明：本讲的实践任务里提到的 `create_paged_kv_cache` 是泛指；源码里实际的导出名是 `create_flashinfer_paged_kv_cache`（FlashInfer 后端）或 `create_tir_paged_kv_cache`（通用 TIR 后端），`create_kv_cache_func_` 会按元数据自动选择其一（见 4.2.3）。

#### 4.1.5 小练习与答案

**练习 1**：`BatchPrefill` 为什么要先调 `kv_cache_begin_forward_func_` 再调 `prefill_func_`，最后还要调 `kv_cache_end_forward_func_`？能否合并成一步？

> **答案**：分页 KV cache 需要在前向计算前知道「这次要为哪些序列、各自新增多少 token」，从而**预先分配/定位页**（begin_forward）；计算时直接读写这些页；计算完释放本次的临时索引（end_forward）。合并成一步会让 KV cache 无法在计算前准备好页表，导致运行时频繁查表/分配，破坏连续内存与 CUDA graph 友好性。

**练习 2**：`ModelObj` 里的 `BatchVerify` 注释强调「函数对**整批**每个序列都运行，不接受只对子集做 verify」。结合推测解码，猜猜为什么有这个限制。

> **答案**：一次 `batch_verify` 把多条序列各自的 token 树拼成一个大 batch 喂给模型，GPU kernel 按固定布局并行计算所有序列。如果允许「只对子集 verify」，就要为缺席的序列填占位数据并丢弃结果，既复杂又浪费算力；因此约定 verify 动作一次性处理整个 running 队列里需要校验的序列，子集化交给上层的动作调度（[u9-l2](u9-l2-action-loop.md)）。

### 4.2 FunctionTable：从 model lib 解析 TVM 函数

#### 4.2.1 概念说明

如果说 `ModelObj` 是「我能做什么」的接口，`FunctionTable` 就是「这些函数的句柄到底从哪来」的实现核心。它是一个**纯 struct**（[function_table.h:L50-L146](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.h#L50-L146)），装着几十个 `tvm::ffi::Function`（或 `TypedFunction`）字段——每个字段就是 model lib 里某个函数的「可调用句柄」。

理解 `FunctionTable` 的关键是一条贯穿全栈的**契约**：

- 编译期（[u8-l3 attach passes](u8-l3-attach-passes.md)、[u3-l2 get_default_spec](u3-l2-relax-nn-model.md)）往 IRModule 里塞入或声明一批函数，并给它们起确定的名字（`global_symbol` / Relax 函数名，如 `"embed"`、`"batch_prefill"`、`"multinomial_from_uniform"`、`"create_flashinfer_paged_kv_cache"`）。
- 编译产物（model lib）把这些函数连同 `_metadata` 一起打包。
- 运行期 `FunctionTable::Init` 加载这个库，**按这些名字符串逐个取出函数**，存进对应字段。
- `ModelImpl` 之后调用 `ft_.embed_func_(...)` 就等于调用编译期塞进去的那个函数。

这条「按名字符串绑定」的契约正是 MLC 能把「Python 编译器」与「C++ 引擎」两套独立代码拼起来的胶水。`FunctionTable` 还内置两条加载路径的抽象：

- **`mod_get_func(name)`**：从模型库的 VM 模块里取函数（模型自带的函数，如 `embed`、`batch_prefill`）。
- **`get_global_func(name)`**：取 TVM runtime 注册的全局函数（通常是 `vm.builtin.*` 这类 KV cache 内置操作，如 `vm.builtin.kv_state_add_sequence`）。

这两者在单卡与多卡（disco）下指向不同实现，但接口一致——这是 `FunctionTable` 抽象掉的关键差异。

#### 4.2.2 核心流程

`FunctionTable::Init` 的两条分支（单卡 vs 多卡）可以画成同一张流程图：

```text
                       Init(reload_lib_path, device, model_config, session, num_shards, num_stages)
                       │
                       ▼
            num_workers = num_shards * num_stages
                       │
          ┌────────────┴─────────────┐
          ▼                          ▼
    num_workers > 1                num_workers == 1
    (disco 多卡)                   (本地单卡)
          │                          │
   disco_mod = load_vm_module    executable = Module::LoadFromFile(reload_lib_path)
   mod_get_func = SessionFuncAsPackedFunc   local_vm = executable.vm_load_executable()
   get_global_func = 同上        local_vm.vm_initialization(...)
   metadata = FromModule(remote0) mod_get_func = local_vm->GetFunction
                                 get_global_func = Function::GetGlobalRequired
          │                          │
          └────────────┬─────────────┘
                       ▼
                _InitFunctions()      ← 按 name 解析全部函数句柄
                       ▼
           校验 metadata 的 TP/PP 与传入 num_shards/num_stages 一致
                       ▼
              若定义了 cuda_graph_alloc_init_func_ 则调用之
```

随后引擎会再调 `FunctionTable::LoadParams`（[function_table.cc:L155-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L155-L205)）把模型权重从磁盘加载进设备——同样分 disco/本地两条路径，权重经 `vm.builtin.tensor_cache.*` 或 disco `ShardLoader` 进入设备内存。

#### 4.2.3 源码精读

**入口 `Init`** 在 [cpp/serve/function_table.cc:L67-L153](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L67-L153)。先看单卡分支（更直观），关键是「把文件变成可调用 VM」：

```cpp
// 1) 把 .so/.tar 加载成 TVM 模块
executable = tvm::ffi::Module::LoadFromFile(reload_lib_path);
// 2) 从模块里取出 "vm_load_executable"，调用它得到 VM executable
fload_exec = executable.value()->GetFunction("vm_load_executable");
this->local_vm = fload_exec.value()().cast<Module>();
// 3) 初始化 VM 的内存分配器（设备类型、池化分配等）
this->local_vm.value()->GetFunction("vm_initialization").value()(
    device.device_type, device.device_id,
    kPooled, kDLCPU, 0, kPooled);
// 4) 定制「按名取函数」：找不到时返回 null 而非报错
this->mod_get_func = [this](const std::string& name) -> Function {
  return this->local_vm.value()->GetFunction(name, true).value_or(Function(nullptr));
};
this->get_global_func = [](const std::string& name) -> Function {
  return Function::GetGlobalRequired(name);
};
// 5) 从 VM 模块读出编译期写入的元数据
this->model_metadata_ = ModelMetadata::FromModule(this->local_vm.value(), std::move(model_config));
// 6) 按名解析所有函数
this->_InitFunctions();
```

多卡分支（[function_table.cc:L74-L101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L74-L101)）的区别在于：库被加载进 **disco 会话**（`runtime.disco.load_vm_module`），`mod_get_func` 被包成 `SessionFuncAsPackedFunc`——它不是直接调函数，而是往 disco 会话投递一个「在所有 worker 上调用此函数」的指令（见 [function_table.cc:L53-L65](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L53-L65)）。元数据则从 worker 0 取回（`DebugGetFromRemote(0)`）。还支持可选的 CPU 绑核（环境变量 `MLC_DISCO_WORKER_CPU_BINDING`）。

**`_InitFunctions`** 在 [function_table.cc:L207-L296](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L207-L296)，是整张表的「装配车间」。核心模式是「**按字符串取函数、容错地接受缺失**」：

```cpp
// 模型计算函数（来自 VM 模块）
this->embed_func_            = mod_get_func("embed");
this->prefill_func_          = mod_get_func("batch_prefill");
this->decode_func_           = mod_get_func("batch_decode");
this->verify_func_           = mod_get_func("batch_verify");
this->get_logits_func_       = mod_get_func("get_logits");
...
// 采样/logit 函数：用 GetFunction(name, true).value_or(null) —— 允许不存在
this->softmax_func_          = mod->GetFunction("softmax_with_temperature", true).value_or(...);
this->apply_logit_bias_func_ = mod->GetFunction("apply_logit_bias_inplace", true).value_or(...);
// GPU 采样函数：仅当设备支持 GPU 采样时才解析
if (Sampler::SupportGPUSampler(local_gpu_device)) {
  gpu_multinomial_from_uniform_func_ = mod->GetFunction("multinomial_from_uniform", true)...;
  gpu_sample_with_top_p_func_        = mod->GetFunction("sample_with_top_p", true)...;
  ...
}
// KV cache 创建函数：按后端与模型类型三选一
this->create_kv_cache_func_ = mod_get_func("create_flashinfer_paged_kv_cache");
if (sliding_window_size != -1 || !create_kv_cache_func_.defined()) {
  // 没有 FlashInfer 就退回通用 TIR 实现；hybrid 模型两者都要；纯 RNN 用 create_rnn_state
  this->create_kv_cache_func_ = mod_get_func("create_tir_paged_kv_cache");
}
// KV cache 操作函数：来自 TVM runtime 注册的全局 builtin
this->kv_cache_add_sequence_func_   = get_global_func("vm.builtin.kv_state_add_sequence");
this->kv_cache_fork_sequence_func_  = get_global_func("vm.builtin.kv_state_fork_sequence");
this->kv_cache_begin_forward_func_  = get_global_func("vm.builtin.kv_state_begin_forward");
...
```

读这段代码能读出三件事：

1. **「必需」与「可选」两类函数**：`embed`/`batch_prefill` 等核心函数用 `mod_get_func`（缺失则存 null，调用前会 `ICHECK`）；`softmax_with_temperature`/GPU 采样等用 `value_or(null)` 显式允许缺失——这正对应 [u8-l3](u8-l3-attach-passes.md) 里「条件附加」的 pass（有些函数只在特定 target/能力下才被编译进库）。
2. **三类来源**：模型计算函数来自 VM 模块本身（`mod_get_func`/`mod->GetFunction`）；KV cache 操作来自 runtime 全局 builtin（`get_global_func("vm.builtin.*")`）；采样函数按设备能力条件加载。这对应编译期三类 pass：模型定义（U3）、dispatch kv cache（[u8-l2](u8-l2-dispatch-passes.md)）、attach（[u8-l3](u8-l3-attach-passes.md)）。
3. **`create_kv_cache_func_` 的三选一**直接决定了 4.1 里 `CreateKVCache` 的行为，是连接「编译期 KV cache 派发 pass」与「运行期建缓存」的桥梁。

最后 `Init` 末尾（[function_table.cc:L147-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L147-L152)）做一致性校验：编译期写进 `_metadata` 的 `tensor_parallel_shards` / `pipeline_parallel_stages` 必须与引擎传入的 `num_shards` / `num_stages` 严格相等——编译时的并行度与运行时的并行度必须对齐，否则报错。这把「不一致」这种隐蔽 bug 前置拦截。

#### 4.2.4 代码实践

**实践目标**：亲手追踪 `FunctionTable::Init` 如何把一个 model lib 文件变成一堆可调用句柄。

**操作步骤**：

1. 在 [function_table.cc:L207-L296](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L207-L296) 的 `_InitFunctions` 里，统计有多少个函数来自 `mod_get_func(...)`（VM 模块内），多少来自 `get_global_func(...)`（runtime 全局 builtin），多少来自 `mod->GetFunction(..., true).value_or(...)`（可选）。各举两个例子。
2. 找到 `create_kv_cache_func_` 的三选一逻辑（约 L238-L250），回答：什么条件下用 FlashInfer？什么条件下退回 `create_tir_paged_kv_cache`？`kv_state_kind == kHybrid` 时会额外解析哪个函数？
3. 在 [function_table.h:L90-L146](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.h#L90-L146) 对照字段名，确认 `embed_func_`、`prefill_func_`、`verify_func_`、`kv_cache_commit_accepted_token_tree_nodes_func_` 这些字段都被 `_InitFunctions` 赋值（即「声明」与「赋值」一一对应）。

**需要观察的现象**：`FunctionTable` 头文件里声明的字段非常多（约 50 个），但 `_InitFunctions` 用极其规整的「按字符串取函数」一行一个地把它们填满——几乎没有逻辑分支（除了 KV cache 创建与 GPU 采样两处）。这种「声明的字段 = 字符串列表」的对齐，使得新增一个编译期函数只要「attach pass 起名 + 头文件加字段 + `_InitFunctions` 加一行」三处协同即可。

**预期结果**：你能用一句话描述 `FunctionTable` 的本质——「**model lib 函数名 → C++ 可调用句柄** 的查表容器，初始化时按名解析、运行时直接调用」。

> 待本地验证：如果你本地有一个编译好的 model lib（`.so`），可以用 `nm -D <lib>.so | grep embed` 之类的命令看到导出符号里确实包含这些函数对应的 TVM 注册名，印证「名字符串契约」确实落在磁盘文件里。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `embed` 用 `mod_get_func`（必需），而 `softmax_with_temperature` 用 `mod->GetFunction(..., true).value_or(null)`（可选）？如果换成 GPU sampler，又有什么额外前提？

> **答案**：`embed` 是任何模型都必须有的核心函数（无 embed 就无法把 token 变成向量），缺失等于库损坏，应在调用时用 `ICHECK(.defined())` 立即报错。`softmax_with_temperature` 等是 [u8-l3](u8-l3-attach-passes.md) 里按 target/能力条件附加的——某些后端可能把它们融进了别的函数或不在该后端启用，因此用容错方式解析、运行期按 `.defined()` 选路。GPU sampler 还有第二层前提：`Sampler::SupportGPUSampler(local_gpu_device)` 必须为真（仅 cuda/vulkan/metal/webgpu 等支持），连解析都不做。

**练习 2**：`SessionFuncAsPackedFunc`（[function_table.cc:L53-L65](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L53-L65)）返回的「函数」被调用时，实际发生了什么？

> **答案**：它返回一个 lambda，调用时把用户参数前面拼上 `(kCallPacked, 0, func)` 三个固定参数，再经 `sess->CallWithPacked(...)` 投递给 disco 会话。会话会把这次调用**广播到所有 worker**（即所有 GPU shard）上执行，结果以 DRef 形式留在各 worker。这就是「张量并行下，单次 `ft_.prefill_func_(...)` 实际在多卡上同步执行」的实现机制。

### 4.3 EngineConfig：驱动引擎与 KV cache 的配置

#### 4.3.1 概念说明

`EngineConfig`（`EngineConfigNode`）是整个引擎的「参数包」——它告诉引擎：用哪些模型、KV cache 多大、能并发多少序列、是否开推测解码/前缀缓存、用什么 prefill 策略。本讲关注它**直接影响 `Model` 的那部分**，尤其是 `Model->CreateKVCache(...)` 用到的几个字段。

回忆 [u9-l1](u9-l1-engine-threaded-state.md)：引擎启动时（[engine.cc:L457-L469](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L457-L469)）会依次对每个 model 调：

```cpp
model->LoadParams();
model->SetMaxNumSequence(engine_config->max_num_sequence);
model->SetPrefillChunkSize(engine_config->prefill_chunk_size);
model->CreateKVCache(
    engine_config->kv_cache_page_size,
    engine_config->max_num_sequence,
    engine_config->max_total_sequence_length,
    engine_config->prefill_chunk_size,
    engine_config->max_history_size,
    engine_config->prefix_cache_max_num_recycling_seqs);
```

这 6 个参数直接决定 KV cache 的「形状」与「容量」。除了显式传入的值，还有一部分是**推断出来的**（`InferrableEngineConfig::InferForKVCache`），依据是 `gpu_memory_utilization`（默认 0.85）——引擎会根据剩余显存反推 `max_num_sequence`、`max_total_sequence_length` 等可达多少。

#### 4.3.2 核心流程

`EngineConfig` 的字段如何影响系统（仅列本讲相关）：

| 字段 | 默认值 | 影响什么 |
| --- | --- | --- |
| `model` / `model_lib` | — | 模型权重目录 / 模型库路径，喂给 `Model::Create` |
| `mode` | `kLocal` | 引擎模式（local/interactive/server），决定未显式指定时的容量默认值 |
| `gpu_memory_utilization` | 0.85 | 推断 KV cache 容量时允许使用的显存比例 |
| `kv_cache_page_size` | 16 | 分页 KV cache 每页的 token 数 → `CreateKVCache` 的 `page_size` |
| `max_num_sequence` | 4 | 同时在 KV cache 里的最大序列数 → `CreateKVCache` 的 `max_num_sequence` |
| `max_total_sequence_length` | 4096 | KV cache 里所有序列长度之和上限 → `max_total_sequence_length` |
| `max_single_sequence_length` | 4096 | 单条序列最大长度（来自模型 context window） |
| `prefill_chunk_size` | 1024 | 单次 prefill 的最大 token 数 → `CreateKVCache` 与 `SetPrefillChunkSize` |
| `max_history_size` | 0 | RNN state 模型回滚所需的历史长度（KV cache 用不到） |
| `prefix_cache_mode` | `kRadix` | 前缀缓存模式（disable / radix 树） |
| `prefix_cache_max_num_recycling_seqs` | -1 | 前缀缓存可保留的回收序列数（-1 无限） |
| `speculative_mode` | `kDisable` | 推测解码模式（disable/small_draft/eagle/medusa） |
| `spec_draft_length` | 0 | 推测草稿长度（0 = 自适应） |
| `prefill_mode` | `kHybrid` | prefill 策略（chunked / hybrid split-fuse） |

`max_num_sequence` 与 `prefill_chunk_size` 一起决定**工作区显存**（`ModelWorkspace` 里的 `embeddings` / `hidden_states` 张量按 `prefill_chunk_size × max_num_sequence` 预分配）。`mode` 三档的语义见 [config.h:L177-L200](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L177-L200)：

- `kLocal`：低并发本地部署，`max_num_sequence=4`，长度类取 context window。
- `kInteractive`：单请求交互，`max_num_sequence=1`。
- `kServer`：高并发服务，自动推断尽量大的 batch 与长度。

#### 4.3.3 源码精读

`EngineConfigNode` 在 [cpp/serve/config.h:L236-L324](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L236-L324)，字段按功能分块声明（Models / KV cache / Prefix cache / Speculative / Prefill mode / Debug）。每个枚举（`EngineMode`、`PrefixCacheMode`、`SpeculativeMode`、`PrefillMode`）都配一对 `XxxToString` / `XxxFromString` 工具函数（[config.h:L369-L464](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L369-L464)），用于 JSON ↔ 枚举互转，使得配置能从 Python 层以字符串传进来。

`InferrableEngineConfig`（[config.h:L343-L362](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L343-L362)）是「可被引擎自动推断」的子集，其 `InferForKVCache` / `InferForRNNState` 两个静态方法承担「**给定显存预算与模型元数据，反推容量上限**」的计算。直觉上：

\[ \text{可用显存} \approx \text{gpu\_memory\_utilization} \times \text{GPU 总显存} - \text{权重显存} - \text{workspace 显存} \]

剩余的显存再按「每 token 的 KV 字节数 × 总 token 数」折算成 `max_total_sequence_length` 等上限。`ModelsUseKVCache`（[config.h:L367](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L367)）则用来判断走 KV cache 还是 RNN state 的推断路径。

引擎如何消费 `EngineConfig` 见 [engine.cc:L457-L469](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L457-L469)：先 `LoadParams` 装权重，再 `SetMaxNumSequence` / `SetPrefillChunkSize` 让模型按容量分配 workspace，最后 `CreateKVCache` 用 6 个参数真正建出 KV cache。注意 `CreateKVCache` 的实现（[model.cc:L852-L906](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L852-L906)）按 `kv_state_kind` 分支：

- `kKVCache`（普通 Transformer）：调 `create_kv_cache_func_(max_num_sequence, max_total_sequence_length, prefill_chunk_size, page_size, support_sliding_window)`。
- `kRNNState`（纯 RNN/RWKV 类）：调同一函数但参数变为 `(max_num_sequence + prefix_cache_max_num_recycling_seqs, max_history_size)`——RNN state 需要为前缀缓存的回收序列预留额外槽位。
- `kHybrid`（如 Mamba-Transformer 混合）：**两者都建**，KV cache + RNN state。
- `kNone`（embedding 模型）：什么都不建。

这就是为什么 `prefix_cache_max_num_recycling_seqs` 会被作为第 6 个参数传进 `CreateKVCache`——它只在 RNN/Hybrid 场景下被消费。

#### 4.3.4 代码实践

**实践目标**：搞清「一条 `mlc_llm serve` 命令的参数 → `EngineConfig` 字段 → `CreateKVCache` 实参」这条链。

**操作步骤**：

1. 在 [config.h:L236-L324](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L236-L324) 找到 `kv_cache_page_size`、`max_num_sequence`、`prefill_chunk_size`、`max_total_sequence_length`、`gpu_memory_utilization` 这 5 个字段的默认值。
2. 在 [engine.cc:L463-L466](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L463-L466) 确认这 5 个字段（外加 `max_history_size` 与 `prefix_cache_max_num_recycling_seqs`）是如何按位置传给 `model->CreateKVCache(...)` 的。
3. 对照 [model.cc:L852-L906](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L852-L906) 的 `CreateKVCache`，回答：对一台普通 Llama 模型（`kv_state_kind = kKVCache`），最终调 `create_kv_cache_func_` 时传的 5 个参数分别是什么？

**需要观察的现象**：`EngineConfig` 里有些字段（如 `kv_cache_page_size=16`）是**直接**透传给模型的；有些（如 `max_num_sequence`、`max_total_sequence_length`）在 `mode=server` 下会被 `InferForKVCache` **覆盖**成推断值；还有些（如 `prefix_cache_max_num_recycling_seqs`）只在特定模型类型下才有意义。

**预期结果**：你能画出「serve CLI 参数 → Python `EngineConfigOverride` → JSON → `EngineConfigNode` 字段 → `CreateKVCache` 实参 → KV cache 句柄」的完整链路，并指出哪一步会发生「自动推断覆盖」。

> 待本地验证：用 `mlc_llm serve ... --mode server` 启动时打开 verbose（`EngineConfig.verbose=true`），引擎会打印推断后的最终 `max_num_sequence` / `max_total_sequence_length`，可对照本节字段理解。

#### 4.3.5 小练习与答案

**练习 1**：假设 GPU 显存固定，把 `prefill_chunk_size` 调大一倍，会对哪些方面产生影响？

> **答案**：① 单次 prefill 能处理的 token 更多，长 prompt 的首 token 延迟（TTFT）通常下降（更少分块）；② 但 `ModelWorkspace` 的 `embeddings`/`hidden_states` 张量按 `prefill_chunk_size` 预分配，workspace 显存上升，留给 KV cache 的显存减少，`InferForKVCache` 推断出的 `max_total_sequence_length` / `max_num_sequence` 会变小，即并发能力下降。这是一个延迟↔并发的权衡。

**练习 2**：为什么 `CreateKVCache` 在 `kRNNState` 分支里要把 `max_num_sequence + prefix_cache_max_num_recycling_seqs` 作为第一参数？

> **答案**：RNN state（如 RWKV）不像 KV cache 那样能用页表共享前缀，前缀缓存复用的是「状态向量」本身，回收序列的状态必须**与活跃序列同时存活**在状态容器里。因此 RNN state 容器要额外预留 `prefix_cache_max_num_recycling_seqs` 个槽位给这些回收序列。KV cache 由于按页管理、可按引用共享，不需要这种额外预留（注释见 [model.cc:L870-L871](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L870-L871) 与 [model.h:L243-L247](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L243-L247)）。

## 5. 综合实践

**任务**：当一次「引擎冷启动」发生时，把本讲三个模块串起来，画出从「`.so` 文件躺在磁盘」到「Action 循环能调 `BatchPrefill`」的完整时序，并用源码行号佐证每一步。

**操作步骤**：

1. **加载与建表**（模块 4.2）。假设引擎以单卡模式启动。追踪 `Model::Create`（[model.cc:L33-L39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L33-L39)）→ `ModelImpl` 构造（[model.cc:L66-L83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L66-L83)）→ `ft_.Init`（[function_table.cc:L67-L153](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L67-L153)）→ `_InitFunctions`（[function_table.cc:L207-L296](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L207-L296)）。写出 `.so` 是在哪一行被 `LoadFromFile`、哪一行被 `vm_load_executable`、`"batch_prefill"` 是在哪一行被解析成 `prefill_func_`。
2. **配权重与 KV cache**（模块 4.3）。追踪 [engine.cc:L459-L469](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L459-L469)：`LoadParams`（[model.cc:L1026](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L1026) → [function_table.cc:L155-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L155-L205)）→ `CreateKVCache`（[model.cc:L852-L906](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L852-L906)）。标出 `EngineConfig` 的 6 个字段分别传给 `CreateKVCache` 的哪个形参。
3. **被 Action 调用**（模块 4.1）。追踪一次 `BatchPrefill` 的内部调用：`BatchPrefill`（[model.cc:L245](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L245)）→ `kv_cache_begin_forward_func_` → `prefill_func_`（即 4.2 解析出的句柄）→ `kv_cache_end_forward_func_`。
4. **画图**：把上述三步画成一张纵向时序图，左侧标注「磁盘 / FunctionTable / ModelImpl / EngineConfig / Action」五个泳道，箭头标注关键函数名与源码行号。

**预期结果**：一张能向别人解释「MLC 引擎如何把编译产物变成可执行计算」的时序图，且每个箭头都能在源码里指到具体行。若你本地能跑 `mlc_llm chat`，可在 verbose 模式下观察启动日志，对照图中的步骤。

## 6. 本讲小结

- **`ModelObj` 是 C++ 引擎看到的「一台模型」**：它是纯虚接口，按「模型计算 / KV cache 管理 / 信息查询 / 工具」四类组织能力，真正的计算被委托给 `FunctionTable` 里的函数句柄。
- **存在一条「三层同名」契约**：`ModelObj::BatchPrefill` → `ft_.prefill_func_` → model lib 里的 `"batch_prefill"` 函数。这条「按名字符串绑定」的契约是 Python 编译器与 C++ 引擎之间的胶水。
- **`FunctionTable` 是「函数名 → 句柄」的查表容器**：`Init` 把 `.so`/`.tar` 加载成 TVM VM，`_InitFunctions` 按字符串逐个解析出计算函数、采样函数、KV cache 操作函数；单卡走本地 VM，多卡走 disco 会话广播。
- **函数有三类来源与两种必需性**：VM 模块内函数（必需）/ runtime 全局 builtin（KV cache 操作）/ 可选附加函数（用 `value_or(null)` 容错）；GPU 采样还要先过 `Sampler::SupportGPUSampler`。
- **`EngineConfig` 驱动 KV cache 容量与并发**：`mode` 决定默认值，`gpu_memory_utilization` 驱动 `InferForKVCache` 反推上限，6 个字段（`page_size`/`max_num_sequence`/.../`prefix_cache_max_num_recycling_seqs`）被透传给 `CreateKVCache`。
- **`CreateKVCache` 按 `kv_state_kind` 分四种建法**（KVCache / RNNState / Hybrid / None），RNN 与 Hybrid 需要为前缀缓存的回收序列额外预留槽位——这是 `prefix_cache_max_num_recycling_seqs` 参数存在的理由。

## 7. 下一步学习建议

- **[u10-l1 分页 KV 缓存模型接口](u10-l1-paged-kv-cache.md)**：本讲只讲了 `CreateKVCache` 的 C++ 调用接口，下一讲深入分页 KV cache 的内部结构（page、序列增删分叉）与 `page_size` 等参数的真实影响。
- **[u10-l2 前缀缓存与 Radix Tree](u10-l2-prefix-cache-radix-tree.md)**：本讲多次提到 `prefix_cache_max_num_recycling_seqs`，下一讲讲清前缀缓存如何复用 KV、Radix Tree 如何存 token 路径。
- **[u10-l3 采样器：CPU 与 GPU](u10-l3-sampler.md)**：本讲提到 `attach_sampler` 生成的那些 GPU 采样函数（`multinomial_from_uniform` 等）在这里被 `Sampler` 调用，讲清 CPU 与 GPU 两条采样路径。
- **[u12-l1 多 GPU 与张量并行](u12-l1-multi-gpu-tensor-parallel.md)**：本讲的 disco 分支（`SessionFuncAsPackedFunc`、`load_vm_module`）在那里与编译期的 preshard/preprocs 闭环。
- **重读 [u8-l3 运行时函数附加 pass](u8-l3-attach-passes.md)**：带着本讲的视角回头看，你会更清晰地理解「编译期 attach 的那些函数，正是运行期 `FunctionTable` 按名解析、`ModelImpl` 按名调用的对象」——两个单元合起来才是「编译产物驱动引擎」的完整图景。
