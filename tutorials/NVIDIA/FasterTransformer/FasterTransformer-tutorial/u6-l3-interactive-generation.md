# 交互式生成、共享上下文与流式生成

## 1. 本讲目标

学完本讲后，读者应能够：

- 理解 **交互式生成（interactive generation）** 解决什么问题：在多轮对话中，如何复用上一轮已经算好的 KV cache，避免每轮都重算整段历史。
- 掌握 FT 用 `continue_gen` / `session_len` 两个输入张量实现“续写”的机制，以及 `step_` 如何跨调用累加。
- 理解 **共享上下文（shared context）** 优化：当同一个 batch 里多条请求的 prompt 完全相同时，如何只算一次 context 阶段。
- 读懂 `gpt_kernels.cu` 中的 `invokeFindContextDups` / `invokeCompactInputs` / `invokeUnCompactOutputs` / `invokeUnCompactCaches` 四个核心 kernel。
- 了解 **流式生成（streaming generation）** 在 `multi_gpu_gpt_async_example.cc` 的 `GptStreamer` 中如何用 `std::async` 一边算一边把已生成的 token 吐给调用方。

本讲承接 [u6-l1 ParallelGpt 架构](u6-l1-parallel-gpt.md)（context / decoder 两阶段分裂）与 [u6-l2 KV Cache 机制与拼装](u6-l2-kv-cache.md)（cache 布局），回答“如何让 cache 跨请求/跨轮次复用”这一进阶问题。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（前置讲义已建立）：

- **KV cache**：自回归解码时，每一步把新 token 对应的 K/V 追加进一块按层组织的 GPU 缓冲，避免重算历史。见 u6-l2。
- **context 阶段 vs decoder 阶段**：FT 把 GPT 一次生成拆成两步——先用 context decoder 一次处理整段 prompt 并写满初始 cache，再由 decoder 逐 token 追加。见 u6-l1。
- **`TensorMap` 输入接口**：`ParallelGpt::forward` 的运行期参数全部通过名为 `input_tensors` 的 `TensorMap` 传入，每个张量有 `where`（CPU/GPU）、`type`、`shape`、`data`。见 u2-l1。
- **`invokeXxx` kernel 约定**：每个算子由“设备 kernel + host 启动函数”两层组成，启动后异步返回。见 u3-l1。

本讲会反复出现两个新名词：

- **session（会话）**：一次“对话”的全部 token（历史 prompt + 已生成 + 待生成）所占的最大时间长度。FT 用 `session_len` 预分配整块 cache，使多轮对话不必反复 `cudaMalloc`。
- **compact（紧凑）**：把 batch 中重复的 context 去重后只保留若干“代表”进入实际计算，算完再“散开”（uncompact）回所有原始请求。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc) | 交互式生成 C++ 示例：演示两段式 `forward`，第二轮把 `continue_gen` 置 `true` 续写。 |
| [examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc) | 流式生成示例：定义 `GptStreamer` / `GptFileStreamer`，在异步线程里做生成与停词检测。 |
| [src/fastertransformer/kernels/gpt_kernels.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.h) | 声明 `invokeFindContextDups`、`invokeCompactInputs`、`invokeUnCompactOutputs`、`invokeUnCompactCaches` 等共享上下文 kernel 的 host 启动函数。 |
| [src/fastertransformer/kernels/gpt_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu) | 上述 kernel 的 CUDA 实现，含 `find_context_dups`、`compact_inputs`、`uncompact_outputs`、`uncompact_caches`。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc) | 在 `forward` 中解析 `continue_gen` / `session_len`，决定 `initial_step`，并触发共享上下文的去重流程。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc) | context 阶段执行体：检测 `compact_idx` 决定是否走紧凑路径，并在算完后 uncompact 输出。 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | 官方文档中的 *Interactive generation* 与 *generate different sentences and enable shared context* 两节，是理解动机的最佳入口。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 交互式生成（`continue_gen` 续写）**
- **4.2 共享上下文（去重只算一次 context）**
- **4.3 流式生成（`GptStreamer` 异步逐 token 输出）**

### 4.1 交互式生成：用 `continue_gen` 跨轮复用 KV cache

#### 4.1.1 概念说明

考虑聊天场景：用户先说 A，模型回 B；用户再说 C，模型要基于「A+B+C」继续回 D。朴素做法是把「A+B+C」整体当作新 prompt 重新跑一遍 context 阶段——但 A、B 的 KV cache 上一轮其实已经算过并存在 GPU 里了，重算是浪费，且对话越长越慢。

FT 的解法是给 `ParallelGpt::forward` 增加一个布尔输入张量 **`continue_gen`**：

- `continue_gen=false`（首轮或一次性生成）：FT 视为全新会话，从第 0 步开始，跑完整 context + decoder。
- `continue_gen=true`（续写轮）：FT **不丢弃**之前已经生成并缓存的 token，`step_` 接着上一轮的值继续累加，context 阶段只处理“本次新增的 token id”，老 token 的 cache 直接复用。

与之配套的是 **`session_len`**：整段会话（所有轮次合计）允许的最大时间长度。FT 在**首轮**用 `session_len` 一次性把 cache 缓冲分配到足够大；后续轮次即使对话变长，只要不超过 `session_len`，就无需重新分配。

#### 4.1.2 核心流程

```text
首轮 forward(continue_gen=false):
  initial_step = 0
  session_len  = input["session_len"]      # 用它分配整块 cache
  context 阶段: 处理整段 prompt[0 .. max_input_len)
                写 KV cache 到时间槽 0..max_input_len-1
  decoder 阶段: step_ 从 max_input_len 走到 gen_len
  记录 step_ = 当前已生成的总长度, 保存 session_len_

续写轮 forward(continue_gen=true):
  initial_step = step_                      # 关键：接着上一轮的步数
  session_len  = session_len_               # 复用上轮分配的缓冲（不再 realloc）
  context 阶段: 只处理新增 input_ids
                写 KV cache 到时间槽 initial_step .. initial_step+max_input_len-1
  decoder 阶段: step_ 从 initial_step+max_input_len 继续走到新 gen_len
```

注意：续写轮传入的 `input_ids` **只包含本轮新增的 token**（FT 已经记住了历史），这一点和朴素做法不同。

#### 4.1.3 源码精读

**(1) 解析 `continue_gen` 并推导 `initial_step`**

在 `ParallelGpt::forward` 开头读取输入张量，决定本轮起始步：

[ParallelGpt.cc:645-659](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L645-L659) —— 读取 `continue_gen`，并把“本轮起始步”设为成员变量 `step_`（续写时）或 0（首轮），`max_context_len` 也相应抬高 `initial_step`：

```cpp
int max_input_length = input_tensors->at("input_ids").shape[1];
bool continue_gen = input_tensors->find("continue_gen") != input_tensors->end() ?
                        input_tensors->at("continue_gen").getVal<bool>() :
                        false;
...
const int initial_step    = continue_gen ? step_ : 0;
int       max_context_len = max_input_length + initial_step;
```

**(2) `session_len` 的取值与校验**

[ParallelGpt.cc:732-745](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L732-L745) —— 续写轮复用上一轮记录的 `session_len_`；首轮则用输入的 `session_len` 分配缓冲，并校验总长度不越界：

```cpp
size_t session_len = 0;
if (continue_gen) {
    session_len = session_len_;  // 复用上一轮已分配的缓冲大小
} else if (input_tensors->find("session_len") != input_tensors->end()) {
    session_len = input_tensors->at("session_len").getVal<uint32_t>();
}
session_len_ = session_len;
FT_CHECK_WITH_INFO(gen_len + initial_step <= session_len, ...);
```

**(3) `continue_gen` 的初始化分支**

[ParallelGpt.cc:833-867](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L833-L867) —— 续写轮里 FT 需要把上一轮的输出 ids 保留下来并接到本轮；首轮才会 `cudaMemsetAsync` 清零输出缓冲。

**(4) 示例：两段式 `forward` 与 `continue_gen` 张量**

交互式示例 `multi_gpu_gpt_interactive_example.cc` 把生成拆成两次 `gpt.forward`：第一次用 `start_ids.csv` 生成中间结果，第二次用 `interactive_inputs_ids.csv` 续写，并把 `continue_gen=true` 注入第二轮的输入：

[multi_gpu_gpt_interactive_example.cc:503-510](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L503-L510) —— 第二轮输入张量构造，关键是 `continue_gen`：

```cpp
input_tensors_final.insert(
    {"input_ids", {MEMORY_GPU, TYPE_INT32, {request_batch_size, (size_t)max_input_len_final}, d_input_ids_final}});
input_tensors_final.insert(
    {"input_lengths", {MEMORY_GPU, TYPE_INT32, {request_batch_size}, d_input_lengths_final}});
input_tensors_final.insert(
    {"output_seq_len", {MEMORY_CPU, TYPE_UINT32, {request_batch_size}, output_seq_len_final.data()}});
bool continue_gen = true;
input_tensors_final.insert({"continue_gen", {MEMORY_CPU, TYPE_BOOL, {1}, &continue_gen}});
```

[multi_gpu_gpt_interactive_example.cc:544-547](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L544-L547) —— 两次连续的 `forward`：第一次首轮生成、第二次续写：

```cpp
for (int i = 0; i < ite; ++i) {
    gpt.forward(&output_tensors, &input_tensors, &gpt_weights);        // 首轮
    gpt.forward(&output_tensors_final, &input_tensors_final, &gpt_weights); // 续写轮(continue_gen=true)
}
```

长度规划也体现“两段相加”：`first_output_len = max_input_len + request_output_len`，`total_output_len = first_output_len + max_input_len_final + request_output_len`（[L238](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L238) 与 [L268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L268)），而首轮的 `session_len` 必须 ≥ `total_output_len`，否则触发 [L269-276](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L269-L276) 的报错。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码与配置，理解交互式生成“只传新 token + 续写”的输入约定。

**操作步骤**：

1. 打开 `examples/cpp/multi_gpu_gpt/start_ids.csv` 与 `interactive_inputs_ids.csv`（若仓库未提供，参考 docs/gpt_guide.md 的 *Interactive generation* 节里给出的两段 token id 示例）。
2. 对照 [multi_gpu_gpt_interactive_example.cc:216-267](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L216-L267)，确认两份 CSV 分别被 `read_start_ids` 读进 `d_input_ids`（首轮）和 `d_input_ids_final`（续写轮）。
3. 在 [ParallelGpt.cc:647-649](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L647-L649) 处加一行 `FT_LOG_INFO("continue_gen=%d initial_step=%d", continue_gen, initial_step);`（仅用于学习，勿提交）。
4. 按 docs/gpt_guide.md 的 *Set up in interactive mode* 编译并运行 `./bin/multi_gpu_gpt_interactive_example`（需要真实权重，本地若无则标注「待本地验证」）。

**需要观察的现象**：

- 续写轮的日志里 `initial_step` 应等于首轮生成结束时的总长度，而不是 0。
- 输出文件 `out.interm`（首轮中间结果）与 `out`（续写后总结果），后者前缀完全包含前者，再接上本轮新增 token。

**预期结果**：续写轮不会重算首轮已有 token 的 KV cache，理论上延迟应接近“只算新增 prompt + 新一轮 decoder 步数”。

#### 4.1.5 小练习与答案

**练习 1**：如果把首轮的 `session_len` 设成 `first_output_len`（刚好够首轮），续写轮会发生什么？
**答案**：续写轮 `gen_len + initial_step > session_len_`，触发 [ParallelGpt.cc:744-746](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L744-L746) 的 `FT_CHECK_WITH_INFO` 报错。`session_len` 必须按“所有轮次最终总长度”预留。

**练习 2**：续写轮传入的 `input_ids` 应该包含整段历史吗？
**答案**：不应该。续写轮只需传入“本轮新增的 token”，因为历史 token 的 KV cache 已在首轮/上一轮写入并被保留。`initial_step = step_` 会让 context 阶段把新 token 写到正确的时间槽。

**练习 3**：为什么 `continue_gen` 必须放在 CPU（`MEMORY_CPU`）而不是 GPU？
**答案**：它是一个影响控制流的标量开关（决定 `initial_step`、是否清零缓冲等），FT 在 host 端用 `getVal<bool>()` 立即读取并据此分支，不进入 GPU 计算，所以必须是 CPU 张量。

---

### 4.2 共享上下文：去重让相同 prompt 只算一次

#### 4.2.1 概念说明

另一个常见场景：**同一个 batch 里多条请求的 prompt 完全相同**。典型例子是“一条系统 prompt + 不同随机种子生成多条回复”——4 条请求里前 256 个 token 全一样，只有随机种子不同。朴素做法会对这 4 条分别跑 context 阶段，等于把同一段长 prompt 算了 4 遍。

FT 的 **shared context** 优化思路是去重：

1. 在 context 阶段开始前，先用 `invokeFindContextDups` 找出 batch 里哪些请求的 input_ids 完全相同，建立“代表→重复者”的映射表。
2. 只对每个“唯一 context”跑一次 context 阶段（batch 从 `batch_size` 缩到 `compact_size`）。
3. 跑完后用 `invokeUnCompactOutputs` / `invokeUnCompactCaches` 把结果“散开”复制回所有原始请求。

开启由构造参数 **`shared_contexts_ratio`** 控制：当去重后的 `compact_size <= shared_contexts_ratio * batch_size` 时才启用（设为 0 关闭）。docs/gpt_guide.md 的实测显示，对 4 条相同长 prompt，开启后从 64.25ms 降到 41.69ms。

#### 4.2.2 核心流程

整个去重靠 **三张索引表** 协作（这是理解本模块的关键）：

- `shared_contexts[i]`：请求 `i` 的“代表”是谁（即与 `i` 完全相同且下标最小的那个请求）。初始化为 `i`，发现 `j` 与更早的 `i` 相同时写成 `i`。
- `compact_to_batch[c]`：紧凑 batch 第 `c` 行对应原始 batch 的哪一行（“代表”的真实下标）。
- `batch_to_compact[b]`：原始 batch 第 `b` 行应该去紧凑 batch 的哪一行取结果（重复者指向其代表的紧凑行）。

```text
去重流程（host: ParallelGpt::forward）:
  invokeFindContextDups(...)        # GPU 上算出三张表 + compact_size
  compact_size = (拷回 host)
  if compact_size <= ratio * batch_size:
      use_shared_contexts = true
      把 compact_idx / batch_to_compact_idx 塞进 context decoder 的输入
      context decoder 只对 compact_size 行跑 context 阶段

context decoder 内部（ParallelGptContextDecoder）:
  invokeCompactInputs(...)          # 用 compact_idx 把输入紧凑化
  for layer: 跑 context（batch = compact_size）
  invokeUnCompactOutputs(...)       # 把 decoder_output 散回 batch_size 行
  invokeUnCompactCaches(...)        # 把 KV cache 散回 batch_size 行
```

为什么 uncompact 必须做？因为后续 decoder 阶段、beam search 都按原始 `batch_size` 运行，cache 必须为每条原始请求都备好一份。

#### 4.2.3 源码精读

**(1) 三表声明与 host 启动函数**

[gpt_kernels.h:124-132](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.h#L124-L132) —— `invokeFindContextDups` 的签名，注意它**不是模板**（只处理 int token id）：

```cpp
void invokeFindContextDups(int*         shared_contexts,
                           int*         batch_to_compact,
                           int*         compact_to_batch,
                           int*         compact_size,
                           const int*   input_ids,
                           const size_t batch_size,
                           const size_t beam_width,
                           const size_t input_seq_len,
                           cudaStream_t stream = 0);
```

[gpt_kernels.h:148-167](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.h#L148-L167) —— `invokeCompactInputs`（紧凑输入与 mask）与 `invokeUnCompactOutputs`（散开输出）的签名，二者都是模板（按数据类型实例化）：

```cpp
template<typename T>
void invokeCompactInputs(T* compact_input, T* compact_attention_mask, int* compact_input_lengths,
                         const T* decoder_input, const T* decoder_mask, const int* input_lengths,
                         const int* compact_idx, size_t compact_size, size_t seq_len,
                         size_t hidden_dimension, cudaStream_t stream = 0);

template<typename T>
void invokeUnCompactOutputs(T* uncompact_buffer, const T* compact_buffer,
                            const int* batch_to_compact_idx, size_t batch_size,
                            size_t buffer_stride, cudaStream_t stream = 0);
```

**(2) `find_context_dups`：成对比较找代表**

[gpt_kernels.cu:580-634](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L580-L634) —— 把 batch 中所有 \((i,j), i<j\) 的请求对两两比较，每对一个 block。关键技巧是把一维 block id 映射到三角形的 \((i,j)\) 对，并用 `atomicMin` 记录“最小的相同下标”：

```cpp
// 一维 blockIdx 映射到三角对的 (src_idx, tgt_idx)
const int base_index = floorf(0.5f * (sqrtf(1 + 8 * blockIdx.x) - 1));
const int src_idx    = base_index + 1;
const int tgt_idx    = blockIdx.x - base_index * (base_index + 1) / 2;
...
// 逐 TB_SIZE 段比较两个请求的 token，全相等才置 match
if (threadIdx.x == 0 && match) {
    atomicMin(&shared_contexts[src_idx], tgt_idx);  // 记录最小代表
}
```

这段注释（[gpt_kernels.cu:583-592](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L583-L592)）解释了 `shared_contexts[i] <= i` 这一不变量，它保证了后续 `generate_dups_indices` 能用前缀扫描正确建表。

**(3) `invokeFindContextDups`：三步串起来**

[gpt_kernels.cu:704-734](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L704-L734) —— 先 `init_shared_contexts` 把每位置成自己，再按序列长度选 128 或 256 线程跑 `find_context_dups`，最后单 block 跑 `generate_dups_indices` 用 CUB 前缀扫描生成另两张表与 `compact_size`：

```cpp
init_shared_contexts<<<grid, block, 0, stream>>>(shared_contexts, batch_size);
...
if (input_seq_len <= 128) {
    find_context_dups<128><<<grid, block, 0, stream>>>(shared_contexts, input_ids, batch_size, input_seq_len);
} else {
    find_context_dups<256><<<...>>>(shared_contexts, input_ids, batch_size, input_seq_len);
}
generate_dups_indices<<<1, DUPS_INDICES_BLOCK_SIZE, 0, stream>>>(
    batch_to_compact, compact_to_batch, compact_size, shared_contexts, batch_size, beam_width, input_seq_len);
```

`generate_dups_indices`（[gpt_kernels.cu:638-693](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L638-L693)）用 `is_first_occur = (shared_contexts[seq_idx] == seq_idx)` 标出“代表”，对其做独占前缀扫描得到紧凑下标，最终 `*compact_size = scan_offset` 写出唯一 context 的数量。

**(4) `compact_inputs` / `invokeCompactInputs`：用索引表收紧输入**

[gpt_kernels.cu:736-803](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L736-L803) —— 按 `compact_idx[batch_id]` 把原始 `[batch_size, seq_len, H]` 的 decoder_input 抽取成 `[compact_size, seq_len, H]`，attention mask 同理：

```cpp
compact_input[global_idx] = decoder_input[(compact_idx[batch_id] * seq_len + seq_id) * hidden_dimension + h_id];
...
compact_attention_mask[global_idx] = decoder_mask[(compact_idx[batch_id] * seq_len + seq2_id) * seq_len + seq1_id];
```

**(5) `uncompact_outputs` / `uncompact_caches`：算完散回去**

[gpt_kernels.cu:824-845](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L824-L845) —— 输出散开，反向用 `batch_to_compact_idx`：`OUT[i,:] = IN[batch_to_compact_idx[i],:]`。

[gpt_kernels.cu:877-927](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L877-L927) —— KV cache 散开。注意它要把紧凑 cache 的 K 重排回 `[dim/x, max_seq_len, x]` 布局、V 回 `[max_seq_len, dim]`（承接 u6-l2 讲过的 K/V 不同尾维布局），同时处理 `ite * local_batch_size` 的流水并行偏移（[gpt_kernels.cu:906](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L906)）。

**(6) host 编排：决定是否启用 + 把表塞进 context decoder**

[ParallelGpt.cc:889-905](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L889-L905) —— 触发去重并按 ratio 决定是否真用：

```cpp
int  compact_size;
bool use_shared_contexts = (shared_contexts_ratio_ > 0.0f) && (max_input_length >= 1) && (batch_size > 1);
if (use_shared_contexts) {
    invokeFindContextDups(shared_contexts_idx_, batch_to_compact_idx_, compact_idx_, compact_size_,
                          input_tensors->at("input_ids").getPtr<int>(),
                          batch_size, beam_width, max_input_length, stream_);
    cudaD2Hcpy(&compact_size, compact_size_, 1);
    use_shared_contexts = compact_size <= shared_contexts_ratio_ * batch_size;  // 按比例门槛
}
```

[ParallelGpt.cc:1064-1070](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1064-L1070) —— 把两张索引表作为 `compact_idx` / `batch_to_compact_idx` 注入 context decoder 的输入张量：

```cpp
if (use_shared_contexts) {
    decoder_input_tensors.insert("compact_idx",
        Tensor(MEMORY_GPU, TYPE_INT32, {(size_t)compact_size}, compact_idx_));
    decoder_input_tensors.insert("batch_to_compact_idx",
        Tensor(MEMORY_GPU, TYPE_INT32, {batch_size * beam_width}, batch_to_compact_idx_));
}
```

**(7) context decoder 内部：检测并走紧凑路径**

[ParallelGptContextDecoder.cc:335-345](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L335-L345) —— 通过“输入里有没有 `compact_idx`”来判断本次是否走紧凑路径，并把实际计算 batch 从 `request_batch_size` 改为 `compact_size`：

```cpp
const bool use_shared_contexts = input_tensors->isExist("compact_idx");
...
const size_t batch_size =
    use_shared_contexts ? input_tensors->at("compact_idx").shape[0] : decoder_input_tensor.shape[0];
```

[ParallelGptContextDecoder.cc:357-370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L357-L370) —— 紧凑化输入；[L825-833](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L825-L833) —— 算完散开输出。

#### 4.2.4 代码实践

**实践目标**：用一个小 batch 手工推演 `invokeFindContextDups` 与 `invokeCompactInputs` 如何让 4 条相同 prompt 只算 1 次。

**操作步骤**：

1. 假设 `batch_size=4`，4 条请求的 input_ids 完全相同（对应 docs/gpt_guide.md *generate different sentences and enable shared context* 节里“4 句相同输入”的例子）。
2. 推演 `find_context_dups` 跑完后：`shared_contexts = [0,0,0,0]`（1、2、3 的代表都是 0）。
3. 推演 `generate_dups_indices`：只有 `i=0` 满足 `shared_contexts[i]==i`，故 `compact_size=1`，`compact_to_batch=[0]`，`batch_to_compact=[0,0,0,0]`。
4. 推演 `invokeCompactInputs`：紧凑输入只剩 1 行（请求 0 的内容）；context decoder 只对 1 行跑一遍。
5. 推演 `invokeUnCompactOutputs`：把这 1 行结果复制成 4 行，每条原始请求都拿到相同 context。

**需要观察的现象**：

- `compact_size` 从 4 降到 1，context 阶段的 GEMM 的 batch 维（即 M 维乘子）缩小为 1/4。
- 散开后每条请求的 `decoder_output` 与 cache 完全一致（因为 prompt 相同），后续 decoder 阶段才因随机种子不同而分叉。

**预期结果**：与 docs/gpt_guide.md 的实测一致——开启 `shared_contexts_ratio=1.0` 比 `0.0` 显著更快（64.25ms → 41.69ms），且生成质量不受影响（仅 GEMM 形状改变，详见 guide 的 Notes）。

> 注：若想真实运行，需准备 megatron/openai 权重并按 docs/gpt_guide.md 的 *Build the project* 与 *Run GPT* 节编译。本地若无 GPU 与权重，则以上为「源码阅读型实践」，结论待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shared_contexts_ratio` 用“比例门槛”而不是只要 `compact_size < batch_size` 就启用？
**答案**：去重本身有开销（成对比较 + 紧凑/散开拷贝）。如果只省下很少几条（如 batch=32 只去重掉 1 条），收益可能抵不过开销。比例门槛（默认行为相当于“只有重复较多时才启用”）保证只在明显有利时触发；设为 0 直接关闭。

**练习 2**：`find_context_dups` 的 grid 大小为什么是 `batch_size*(batch_size-1)/2`？
**答案**：它要枚举所有 \(i<j\) 的请求对来判定是否完全相同，这正是大小为 `batch_size` 的三角数（[gpt_kernels.cu:599-601](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L599-L601) 的注释也指向三角数公式）。每个 block 处理一对，互不干扰地 `atomicMin`。

**练习 3**：去重只发生在 context 阶段，decoder 阶段为什么不需要？
**答案**：context 阶段算的是“prompt 的 KV cache”，相同 prompt 算一遍即可复制。decoder 阶段每条请求因随机种子/采样不同会生成不同 token，cache 内容随即分叉，不再可共享，所以 decoder 必须按原始 `batch_size` 跑。

---

### 4.3 流式生成：`GptStreamer` 异步逐 token 输出

#### 4.3.1 概念说明

交互式产品（如聊天框）通常希望“边生成边显示”，而不是等几百个 token 全算完一次性返回。FT 的 C++ 示例 `multi_gpu_gpt_async_example.cc` 用一个 **`GptStreamer`** 类演示了流式生成：

- 在一个 **异步线程**（`std::async`）里跑 `gpt.forward`（forward 内部本身是逐 step 的循环）。
- 主线程在生成进行中 **轮询** `sequence_length` 输出张量，把“自上次以来新生成的 token”拷回 host 并交给用户自定义的 `streamHook`。
- 提供 **`stopCriteria`** 钩子让用户自定义停词逻辑（如遇到 `END_TOKEN_ID` 提前停），并通过 `sendStopSignal` 把停词状态写回模型的 `finished` 缓冲，终止异步线程里的循环。

关键约束：**只有流水并行的最后一个 rank** 真正异步监控；其它 rank 只同步跑一次 forward 即可（见下文源码）。另外异步流式不支持 beam search（`FT_CHECK(beam_width == 1)`）。

#### 4.3.2 核心流程

```text
run() 入口:
  if 本 rank 不是 pipeline 最后一 rank:
      gpt.forward(...)              # 同步跑一次就返回（不监控）
      return
  else:
      std::async: gpt.forward(...)  # 异步线程里跑生成
      streamDecoding()              # 主线程同时轮询输出

streamDecoding() 主循环:
  while 未停 且 未达 max_output_len:
      拷回 sequence_length, 计算 curr_step = max(seqlen)
      if curr_step > prev_step:
          拷回 [prev_step, curr_step) 的 output_ids
          is_done = stopCriteria(...)    # 用户停词逻辑
          streamHook(...)                # 用户自定义处理(如写文件/推前端)
      else:
          拷回 finished, 判断是否全部完成
      prev_step = curr_step
  if is_done: sendStopSignal()       # 把 finished 写回 GPU 终止 forward
```

#### 4.3.3 源码精读

**(1) `run`：只在最后一 rank 异步**

[multi_gpu_gpt_async_example.cc:338-358](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L338-L358) —— 流式入口。非最后 rank 同步 forward 后直接返回；最后 rank 用 `std::async(std::launch::async, ...)` 在独立线程跑 forward，主线程进入 `streamDecoding`：

```cpp
if (gpt_->getPipelineParallelRank() < gpt_->getPipelineParallelSize() - 1) {
    gpt_->forward(output_tensors_, input_tensors_, gpt_weights_);
    return;
}
...
std::async(std::launch::async, [&]() {
    check_cuda_error(cudaSetDevice(device));
    gpt_->forward(output_tensors_, input_tensors_, gpt_weights_);
});
streamDecoding();
```

为什么只有最后 rank？因为 `output_ids` / `sequence_length` 只在流水并行的最后一站产出，只有它能看到“最新生成了几个 token”。

**(2) `streamDecoding`：轮询 + 钩子**

[multi_gpu_gpt_async_example.cc:192-258](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L192-L258) —— 主循环。每轮把 `sequence_length` 拷回 host 算出 `curr_step`，若有新 token 就拷回这段 `output_ids` 并触发 `stopCriteria` 与 `streamHook`：

```cpp
while (!(is_generation_done || curr_step == max_output_len)) {
    cudaMemcpyAsync(seqlen_buf_, (int*)output_tensors_->at("sequence_length").data, ...);
    cudaStreamSynchronize(stream);
    curr_step = *std::max_element(seqlen_buf_, seqlen_buf_ + batch_size);
    if (prev_step < curr_step) {
        int idx_from = prev_step * batch_size;
        cudaMemcpyAsync(output_ids + idx_from, ...(curr_step - prev_step) * batch_size, ...);
        cudaStreamSynchronize(stream);
        is_generation_done = stopCriteria(prev_step, curr_step, output_ids);
        streamHook(prev_step, curr_step, output_ids);   // 用户钩子
    } else {
        // 全部 finished 时退出
    }
    prev_step = curr_step;
}
```

**(3) `stopCriteria`：自定义停词**

[multi_gpu_gpt_async_example.cc:120-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L120-L145) —— 默认实现：检测到 `END_TOKEN_ID` 就把对应样本的 `finished[i]` 置真。用户可继承重写以实现自定义停词（如多 token 停词）。

**(4) `sendStopSignal`：把停词写回模型**

[multi_gpu_gpt_async_example.cc:179-190](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L179-L190) —— 把 host 端 `finished` 数组异步拷回模型内部 `getFinishBuffer()`，让异步线程里正在跑的 forward 循环感知到“该停了”：

```cpp
cudaMemcpyAsync(gpt_->getFinishBuffer(), finished,
                sizeof(bool) * ..., cudaMemcpyHostToDevice, stream);
cudaStreamSynchronize(stream);
```

**(5) `GptFileStreamer`：一个具体的 `streamHook`**

[multi_gpu_gpt_async_example.cc:362-398](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L362-L398) —— 继承 `GptStreamer`，把每步 token 即时写入 `out.stream` 文件，演示“边算边输出”。main 里由 `USE_ASYNC` 开关决定走流式还是普通同步 forward（[L788-793](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L788-L793)、[L816-822](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L816-L822)）。

> 关于 Triton backend：生产环境的流式部署通常走 Triton inference server。FT 仓库内 `src/fastertransformer/triton_backend/` 提供了 `ParallelGptTritonModel`（详见 u10-l3），把上述“逐 token 产出”能力暴露成 Triton 的 streaming 接口；具体后端封装在独立仓库 `fastertransformer_backend`（docs/gpt_guide.md 的 *gpt with triton backend* 节有指引），本讲不展开。

#### 4.3.4 代码实践

**实践目标**：理解 `GptStreamer` 的“异步 forward + 主线程轮询”并发结构。

**操作步骤**：

1. 阅读 [multi_gpu_gpt_async_example.cc:338-358](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L338-L358) 的 `run`，确认 forward 在 `std::async` 线程、`streamDecoding` 在主线程，二者共享同一组 `output_tensors`。
2. 在 `streamDecoding` 的 `streamHook` 调用处（[L228](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L228)）想象打印 `curr_step`，体会“每出一个 token 就触发一次钩子”。
3. 若本地有环境，按 docs/gpt_guide.md 编译并以 `./bin/multi_gpu_gpt_async_example <config> 1`（第二参数 1 启用 async）运行；否则标注「待本地验证」。

**需要观察的现象**：

- `out.stream` 文件会随生成推进**逐渐变长**（而非结束时一次性写出），这就是“流式”。
- 全部样本命中 `END_TOKEN_ID` 后，`sendStopSignal` 触发，异步 forward 提前结束，未到 `max_output_len` 也会停。

**预期结果**：开启 async 后用户能更早看到首 token；总耗时与同步模式接近（流式主要改善的是“首 token 延迟”与可中断性，不是总吞吐）。

#### 4.3.5 小练习与答案

**练习 1**：为什么异步流式强制 `beam_width == 1`？
**答案**：见 [multi_gpu_gpt_async_example.cc:556-558](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L556-L558) 的 `FT_CHECK(beam_width == 1)`。beam search 每步会在 beam 间重排（cache_indirection，见 u6-l2），输出下标与步数不是简单线性关系，轮询 `sequence_length` 取 token 的逻辑会失效；故流式示例只支持 sampling/greedy。

**练习 2**：`stopCriteria` 把 `finished[i]` 置真后，模型是怎么知道的？
**答案**：`stopCriteria` 只改 host 端副本；真正通知模型的是 [sendStopSignal](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L179-L190)，它把 `finished` 拷回 `gpt_->getFinishBuffer()` 这块 GPU 缓冲，forward 内部的 early-stopping 判定（每步把 finished 拷回 host 求和，见 u5-l2）读到全真就提前退出循环。

**练习 3**：为什么非最后 pipeline rank 的 `run` 不做异步监控？
**答案**：流水并行下，`output_ids` 与 `sequence_length` 只在最后一站（持有最后一层权重的 rank）才被写入；中间 rank 看不到最新进度，监控无意义。它们只需同步跑完自己的 forward 以推进流水线（[L346-349](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L346-L349)）。

---

## 5. 综合实践

设计一个把三大特性串起来的小任务（源码阅读 + 推演为主）。

**场景**：你要为一个客服聊天服务设计推理后端，需求是：

1. 每个会话多轮对话（交互式生成）。
2. 同一时刻可能有多个用户发来**完全相同**的标准问句（如“怎么重置密码”），希望复用计算（共享上下文）。
3. 前端要求 token 流式返回（流式生成）。

**任务**：

1. **输入张量设计**：参考 [multi_gpu_gpt_interactive_example.cc:423-510](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_interactive_example.cc#L423-L510)，写出“第二轮续写”时应注入的 4 个关键张量：`input_ids`（仅新增 token）、`input_lengths`、`output_seq_len`（=最终总长度）、`continue_gen=true`，并说明 `session_len` 必须在第几轮设定、设成多大。
2. **共享上下文触发**：参考 [ParallelGpt.cc:889-905](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L889-L905)，说明若 8 个用户里 5 个问句完全相同，`compact_size` 会是多少，以及 `shared_contexts_ratio` 应至少设为多少才会真正启用（`compact_size <= ratio * batch_size`）。
3. **流式接入**：参考 [multi_gpu_gpt_async_example.cc:338-358](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L338-L358) 与 [L120-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_async_example.cc#L120-L145)，指出若要支持多 token 停词（如遇到“再见”两个字才停），应重写哪个虚函数，以及为什么必须用 `beam_width=1`。

**参考要点**：

1. `session_len` 必须在**首轮**按“整个会话预估最大总长度”设定（[ParallelGpt.cc:736-737](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L736-L737)），续写轮复用 `session_len_`。
2. 5 个相同 → `compact_size = 4`（这 5 个合并为 1 个代表，加上另外 3 个不同问句）；要启用需 `ratio >= 4/8 = 0.5`。
3. 重写 `stopCriteria`（在 host 端判断多 token 停词并置 `finished`）；流式必须 `beam_width=1` 因为 beam search 的 cache 重排破坏了“步数↔token 位置”的线性关系。

---

## 6. 本讲小结

- **交互式生成**靠 `continue_gen` + `session_len` 两个输入张量实现：续写轮 `initial_step = step_`（接着上一轮），只处理新增 token 并复用首轮分配的 cache，`session_len` 须按整段会话预留。
- **共享上下文**用 `invokeFindContextDups` 建立 `shared_contexts`/`compact_to_batch`/`batch_to_compact` 三张表，把相同 prompt 去重成 `compact_size` 行只算一次，再由 `invokeCompactInputs` 紧凑、`invokeUnCompactOutputs`/`invokeUnCompactCaches` 散开；由 `shared_contexts_ratio` 按比例门槛决定是否启用。
- **去重的核心 kernel** `find_context_dups` 用“一维 block id → 三角 (i,j) 对”枚举所有请求对，靠 `atomicMin` 记录最小代表，`generate_dups_indices` 用 CUB 前缀扫描生成紧凑下标。
- **流式生成**在 `GptStreamer` 中用 `std::async` 跑 forward、主线程轮询 `sequence_length` 取新 token 并触发 `streamHook`/`stopCriteria`；只有流水并行最后 rank 异步，且强制 `beam_width=1`。
- 三者**正交可组合**：续写可叠加共享上下文（每轮 context 都去重），流式可叠加续写（每轮 forward 都流式输出），但流式与 beam search 互斥。
- 这些能力在 `ParallelGpt::forward` 的输入张量集合里统一暴露，是 FT 走向“真实服务”而非“一次性 batch 推理”的关键一步。

## 7. 下一步学习建议

- **Triton backend 部署（u10-l3）**：把本讲的 `continue_gen`/`session_len`/流式能力接到 Triton inference server，看 `ParallelGptTritonModel` 如何把“会话状态”封装成可被反复调用的 backend 实例。
- **张量并行与流水并行（u7-l1 / u7-l2）**：本讲的 `getPipelineParallelRank` 已出现流水并行概念，建议回头系统学习 `NcclParam` 与 MPI rank 划分，理解“为什么只有最后 rank 异步”。
- **DynamicDecodeLayer（u8-l1）**：本讲的 `stopCriteria` 与 `finished` 机制最终落到 forward 内部每步的动态解码，u8 会讲清 `finished` 如何驱动 early stopping 与跳过已完成序列。
- 继续阅读 [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) 的 *Advanced features* 全节，对照本讲源码把动机与实现逐条印证。
