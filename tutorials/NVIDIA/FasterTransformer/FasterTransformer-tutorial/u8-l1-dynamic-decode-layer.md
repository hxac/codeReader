# DynamicDecodeLayer：运行期参数与统一解码入口

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `DynamicDecodeLayer` 在 FasterTransformer（下称 FT）解码流程里扮演的「统一入口」角色，以及它内部聚合了哪四种解码后端。
- 解释 `runtime_arg_names_` 这一组「运行期参数」的含义，以及 `hasDiffRuntimeArgs` 如何判断同一批请求内是否存在取值不一致的参数。
- 描述 `setup` 与 `forward` 的分工与协作，并能讲出「同一次推理、不同请求分别走 Top-K 与 Top-P」的实现机制。
- 看懂上层模型（如 `ParallelGpt`）是如何只面对一个 `DynamicDecodeLayer`，而不必关心底层是 beam search 还是 sampling 的。

本讲是第 8 单元「解码策略与动态解码」的第一篇，承接 u5-l2（`Decoding` 端到端生成）与 u6-l1（`ParallelGpt` 架构）中反复出现的 `DynamicDecodeLayer`，把它从「黑盒」拆解成可读的源码。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **生成式解码的两个家族**：beam search（保留 `beam_width` 条候选路径，逐步挑选）与 sampling（每步按概率分布采样一个 token）。
- **logits**：模型在每一步对整个词表输出的原始分数，形状通常是 `[batch, beam_width, vocab_size]`。解码层要把它变成「下一步选哪个 token」。
- **TensorMap**：FT 里按名字索引的张量容器（见 u2-l1），`DynamicDecodeLayer` 的输入输出全部以 `TensorMap*` 形式传递。
- **运行期参数 vs 编译期/构造期参数**：FT 很多参数（如 `vocab_size`）在构造对象时确定；而采样温度 `temperature`、`top_k`、`top_p` 这类参数希望「每次请求、甚至每个请求」都可以不同，这类参数就叫运行期参数。

一个常用术语：**temperature（温度）\( T \)**。它是对 logits 做缩放后再 softmax 的因子：

\[
p_i = \frac{\exp(\text{logit}_i / T)}{\sum_j \exp(\text{logit}_j / T)}
\]

\( T \) 越大分布越平坦（生成越随机），\( T\to 0 \) 退化为贪心。这个公式会出现在本讲对 `temperature` 参数的解释里。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/fastertransformer/layers/DynamicDecodeBaseLayer.h` | 解码层的纯虚抽象基类，定义 `setup`/`forward` 接口契约。 |
| `src/fastertransformer/layers/DynamicDecodeLayer.h` | 统一解码入口的声明：聚合四个后端指针、`runtime_arg_names_` 列表。 |
| `src/fastertransformer/layers/DynamicDecodeLayer.cc` | 本讲核心：`initialize`/`setup`/`forward`/`hasDiffRuntimeArgs` 的全部实现。 |
| `src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc` | 上层调用方，演示 `setup` 与 `forward` 的协作时机。 |
| `src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.h` | sampling 后端的公共基类，含 `skip_decode_`/`skip_any_` 字段。 |
| `src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu` | Top-K 后端实现，能看到 `skip_decode[i] = k == 0` 的跳过判定。 |

后两个文件用于帮助理解「同批不同策略」的协作机制，本讲只引用其中关键片段；Top-K/Top-P/Beam Search 各自的算法细节分别在 u8-l2、u8-l3 详解。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **统一解码入口与四种后端分发**：`DynamicDecodeLayer` 为什么存在、内部如何按 `beam_width` 分发。
2. **`runtime_arg_names_` 运行期参数机制**：哪些参数算「运行期」、`hasDiffRuntimeArgs` 如何探测同批不一致。
3. **`setup` 与 `forward` 的协作**：两阶段如何配合，以及「同一次推理、不同请求走不同采样策略」的真相。

---

### 4.1 统一解码入口与四种后端分发

#### 4.1.1 概念说明

FT 的解码算法不止一种：在线 beam search、（已废弃的）朴素 beam search、Top-K sampling、Top-P（nucleus）sampling。如果让上层模型（`ParallelGpt`、`Decoding` 等）直接面对这四种实现，每个模型类都得写一堆 `if (beam_width > 1) ... else ...` 的分支，且要在 beam search 与 sampling 之间维护两套调用约定。

`DynamicDecodeLayer` 解决的就是这个问题：它是一个**外观（Facade）**。对外只暴露一个统一的 `forward(TensorMap* output, TensorMap* input)`，对内根据运行期信息决定调用哪一个具体后端。这样上层模型只持有一个 `dynamic_decode_layer_` 指针，完全不感知后端差异。

四种后端共享同一个抽象基类 `DynamicDecodeBaseLayer`，它用纯虚函数规定了所有解码层都必须实现的接口：

```cpp
virtual void setup(const size_t batch_size, const size_t beam_width, TensorMap* runtime_args) = 0;
virtual void forward(std::vector<...>* ...);                  // 旧式 vector 接口
virtual void forward(std::unordered_map<std::string, Tensor>* ...);
virtual void forward(TensorMap* output_tensors, TensorMap* input_tensors) = 0;   // 新式 TensorMap 接口
```

见 [DynamicDecodeBaseLayer.h:L26-L47](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeBaseLayer.h#L26-L47)，这里定义了抽象基类与三套 `forward` 重载（前两套是历史接口，新代码走 `TensorMap` 版本）。`setup` 也是纯虚的——这是下一节的重点。

#### 4.1.2 核心流程

`DynamicDecodeLayer` 内部持有四个基类指针，`initialize()` 阶段一次性把四个后端全部构造出来：

```text
DynamicDecodeLayer
 ├── online_beamsearch_decode_  (OnlineBeamSearchLayer)  ← beam_width>1 时实际使用
 ├── beamsearch_decode_         (BeamSearchLayer)        ← 已废弃，源码中 FT_CHECK(false)
 ├── topk_decode_               (TopKSamplingLayer)      ← beam_width==1 时使用
 └── topp_decode_               (TopPSamplingLayer)      ← beam_width==1 时使用
```

`forward` 的分发逻辑非常简洁，本质是一句判断：

```text
读 ite / step / batch_size / beam_width
若 beam_width > 1:  → 走 online_beamsearch_decode_（按需逐请求循环）
否则 (==1):         → 串行调用 topk_decode_ 与 topp_decode_
```

为什么 `beamsearch_decode_`（朴素 beam search）被废弃？因为在线 beam search（`OnlineBeamSearchLayer`）在功能上覆盖了它，且支持 `beam_search_diversity_rate`。源码里对应分支甚至直接写死 `FT_CHECK(false); // deprecate this module`，意味着这条路径在新代码里永不命中。

#### 4.1.3 源码精读

四个后端指针的声明在 [DynamicDecodeLayer.h:L37-L40](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h#L37-L40)，类型统一是 `DynamicDecodeBaseLayer*`，这是实现「可替换后端」的关键——基类指针指向不同子类。

`initialize()` 把四个后端全部 `new` 出来，见 [DynamicDecodeLayer.cc:L44-L110](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L44-L110)。注意构造时传入的 `max_batch_size`、`beam_width`、`top_k`、`top_p` 等参数全部填 `0` 或默认值，源码注释明确写着 `// deprecated`：

```cpp
topk_decode_ = new TopKSamplingLayer<T>(0,            // max_batch_size, deprecated
                                        vocab_size_,
                                        vocab_size_padded_,
                                        0,            // end_id, deprecated
                                        0,            // top_k_, deprecated
                                        ...);
```

见 [DynamicDecodeLayer.cc:L80-L92](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L80-L92)。**这些构造期参数已经不再生效**，真正起作用的值是在每步解码前通过 `setup(runtime_args)` 传入的——这是 FT 把「构造期配置」逐步迁移到「运行期配置」的典型痕迹。

分发逻辑在 `forward` 里。先读出关键维度（[DynamicDecodeLayer.cc:L239-L245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L239-L245)）：

```cpp
const int ite  = (int)input_tensors->at("ite").getVal<uint>();
const int step = input_tensors->at("step").getVal<int>();
const size_t batch_size = input_tensors->at("logits").shape[0];
const size_t beam_width = input_tensors->at("logits").shape[1];   // ← 分发依据
```

注意 `beam_width` 直接从 `logits` 的第二维读出，这意味着**一次 `forward` 调用内 `beam_width` 是固定的全批统一值**——这一点在 4.3 节会再次强调。

beam search 分支与 sampling 分支的入口分别是 [DynamicDecodeLayer.cc:L286-L386](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L286-L386) 与 [DynamicDecodeLayer.cc:L387-L445](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L387-L445)。其中 beam search 分支里 online 与废弃朴素分支的抉择见 [DynamicDecodeLayer.cc:L375-L384](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L375-L384)：

```cpp
if (true || beam_width < 16 || ...) {
    online_beamsearch_decode_->forward(...);   // 实际走这里
}
else {
    FT_CHECK(false);                           // deprecate this module
    beamsearch_decode_->forward(...);
}
```

开头的 `true ||` 让条件永远成立，等于在源码层面把朴素 beam search 永久关闭。

#### 4.1.4 代码实践

**实践目标**：验证「`DynamicDecodeLayer` 对外是一个入口，对内按 `beam_width` 分发到不同后端」。

**操作步骤**（源码阅读型实践）：

1. 打开 [DynamicDecodeLayer.cc:L44-L110](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L44-L110)，数一下 `initialize()` 里一共 `new` 了几个后端对象。
2. 跳到 [DynamicDecodeLayer.cc:L286](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L286) 与 [DynamicDecodeLayer.cc:L387](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L387)，确认 `if (beam_width > 1)` 与 `else` 两个分支分别调用了哪个后端指针。
3. 在 [DynamicDecodeLayer.cc:L381-L383](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L381-L383) 找到 `FT_CHECK(false)`，理解为什么朴素 beam search 不会被触发。

**需要观察的现象**：四个后端在 `initialize()` 中被无条件构造，但同一次 `forward` 只会用到其中一两个指针。

**预期结果**：`beam_width>1` 用 `online_beamsearch_decode_`；`beam_width==1` 用 `topk_decode_` 和 `topp_decode_` 串行；`beamsearch_decode_` 永不命中。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `initialize()` 里四个后端都用 `0`/默认值构造，而不是用真实参数？

> **答案**：因为这些参数（`top_k`、`top_p`、`beam_search_diversity_rate` 等）被设计成运行期可变，真正的值通过每次 `setup(runtime_args)` 注入。构造期填占位值只是满足构造函数签名，源码注释也标了 `deprecated`。

**练习 2**：如果把 `online_beamsearch_decode_` 的构造去掉，`DynamicDecodeLayer` 在什么场景下会崩溃？

> **答案**：在 `beam_width>1`（即上层请求 beam search）的 `forward` 调用中，会在 [DynamicDecodeLayer.cc:L379](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L379) 解引用空指针。所以即便当前请求用 sampling，四个后端也都在构造期一次性建好。

---

### 4.2 `runtime_arg_names_`：运行期参数机制

#### 4.2.1 概念说明

并非所有解码参数都能「逐请求不同」。FT 区分了两类：

- **可批量（batched）的参数**：sampling 的 `top_k`、`top_p` 支持每个请求取不同值，因为 sampling kernel 写成了批量版本（一个 batch 里的每条请求独立处理）。
- **不可批量的参数**：beam search 的某些参数（如 `beam_search_diversity_rate`、`temperature`、`len_penalty`）目前**没有批量版 kernel**，要求同一批内取值必须一致；若不一致，就只能退化为「一次只处理一个请求」。

`runtime_arg_names_` 列出的正是后者——那一组「可以运行期改变、但 beam search 不支持批量、因此同批必须一致」的参数。它的定义在 [DynamicDecodeLayer.h:L48-L53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h#L48-L53)：

```cpp
const std::vector<std::string> runtime_arg_names_ = {
    "beam_search_diversity_rate",
    "temperature",
    "len_penalty",
    "repetition_penalty",
    "presence_penalty",
    "min_length"
};
```

注意上面的注释：*“argument names which can have different values in runtime and does not support a batched version of kernel in beam search.”*

#### 4.2.2 核心流程

`hasDiffRuntimeArgs(input_tensors)` 的任务：遍历 `runtime_arg_names_` 里的每个名字，若该参数存在，则检查它张量内所有元素是否两两相等；只要发现任意一个参数在同批内取值不一致，就返回 `true`。

判定结果存到成员 `has_diff_runtime_args_`（[DynamicDecodeLayer.h:L55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h#L55)），后者直接决定 beam search 分支的处理粒度：

```text
若 has_diff_runtime_args_ == true:
    dynamic_decode_batch_size = 1                  # 退化为逐请求
否则:
    dynamic_decode_batch_size = local_batch_size   # 整批一次处理
```

用一个表来概括：

| 场景 | `has_diff_runtime_args_` | beam search 处理粒度 |
| --- | --- | --- |
| 全批 `temperature` 都是 0.8 | `false` | 整批一次 kernel |
| 请求 0 用 `temperature=0.8`、请求 1 用 `1.0` | `true` | 拆成两次、每次 batch=1 |

#### 4.2.3 源码精读

`hasDiffRuntimeArgs` 实现在 [DynamicDecodeLayer.cc:L477-L514](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L477-L514)。核心是一个双层循环：外层遍历 6 个参数名，内层逐元素比对 `data[0]` 与 `data[j]`，并按 `tensor.type` 用 `switch` 分派到 FP32/INT32/UINT32/UINT64 四种类型的比较：

```cpp
for (int i = 0; i < (int)runtime_arg_names_.size(); i++) {
    if (input_tensors->isExist(runtime_arg_names_[i])) {
        auto tensor = input_tensors->at(runtime_arg_names_[i]);
        for (int j = 1; j < (int)tensor.shape[0]; j++) {
            switch (tensor.type) {
                case TYPE_FP32:
                    if (((const float*)data)[0] != ((const float*)data)[j]) return true;
                ...
            }
        }
    }
}
return false;
```

注意它只在 **CPU 上**做比较——这些运行期参数张量位于 CPU（见 `setup` 注释里 `[1] or [batch_size] on cpu`），所以可以直接读指针。这也是为什么 `hasDiffRuntimeArgs` 不需要任何 CUDA 同步。

判定结果的使用点在 beam search 分支开头，[DynamicDecodeLayer.cc:L289-L290](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L289-L290)：

```cpp
const size_t dynamic_decode_batch_size      = has_diff_runtime_args_ ? 1 : local_batch_size;
const int    dynamic_decode_total_iteration = local_batch_size / dynamic_decode_batch_size;
```

随后 [DynamicDecodeLayer.cc:L292-L294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L292-L294) 的 `for (dynamic_ite ...)` 循环就是「逐请求」的体现：当 `has_diff_runtime_args_==false` 时循环只跑一轮（整批），为 `true` 时跑 `local_batch_size` 轮（每轮一条请求）。

#### 4.2.4 代码实践

**实践目标**：亲手追一遍「同批参数不一致 → beam search 逐请求」的判定链。

**操作步骤**：

1. 阅读 [DynamicDecodeLayer.h:L48-L53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h#L48-L53)，把这 6 个参数名抄下来。
2. 在 [DynamicDecodeLayer.cc:L477-L514](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L477-L514) 确认：只要任一参数 `data[0] != data[j]` 就立刻 `return true`。
3. 跟踪 `has_diff_runtime_args_` 在 [DynamicDecodeLayer.cc:L289-L290](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L289-L290) 如何把 `true` 翻译成 `dynamic_decode_batch_size=1`。

**需要观察的现象**：当所有请求的 `temperature` 相同时，循环只执行 1 次；当存在差异时，循环执行 `local_batch_size` 次。

**预期结果**：你能用自己的话解释「为什么 beam search 比 sampling 更难做批量」——因为 beam search 的 kernel 内部要在一个 batch 内跨 beam 做排序/选择，目前实现没有给那 6 个参数留「逐请求槽位」。

#### 4.2.5 小练习与答案

**练习 1**：`runtime_top_k` 和 `runtime_top_p` 为什么**不在** `runtime_arg_names_` 里？

> **答案**：因为 sampling 的 Top-K/Top-P kernel 已经实现了批量版本，支持同一 batch 内每条请求取不同的 `top_k`/`top_p`（见 4.3 节的 skip 机制）。`runtime_arg_names_` 只收录「beam search 不支持批量」的那 6 个参数。

**练习 2**：`hasDiffRuntimeArgs` 在比较元素时为什么用 `switch(tensor.type)` 而不是模板？

> **答案**：因为参数张量的类型在运行期才知道（来自 `TensorMap`），而 C++ 模板是编译期分派。这里用 `switch` + `void*` 强转是 FT 处理「运行期多类型」的常见手法，与 u2-l1 讲过的 `DataType` 枚举 dispatch 一脉相承。

---

### 4.3 `setup` 与 `forward` 的协作

#### 4.3.1 概念说明

`DynamicDecodeLayer` 的每一步解码实际是两个调用：

- `setup(batch_size, beam_width, runtime_args)`：把运行期参数（如 `runtime_top_k`、`runtime_top_p`、`temperature` 等）从 host 拷贝到 device buffer，准备好后端要用的常量。
- `forward(output_tensors, input_tensors)`：拿本步的 `logits` 做实际解码，写出 `output_ids`、`finished`、`should_stop` 等。

两者**必须配对**：`setup` 在每步 `forward` 之前调用，因为运行期参数可能每步变化（尤其在多请求轮转或在线服务场景）。上层 `ParallelGpt` 正是这样用的，见 [ParallelGpt.cc:L821](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L821) 的 `setup` 与 [ParallelGpt.cc:L1501](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1501) 的 `forward`。

#### 4.3.2 核心流程

现在回答本讲实践任务的核心问题：**「同一次推理、不同请求分别使用不同策略」到底怎么实现？**

**先澄清一个常见误解**：beam search 与 sampling 之间的切换是由 `beam_width` 决定的，而 `beam_width` 在**一次 `forward` 内对全批固定**（从 `logits.shape[1]` 读出）。所以你不能在同一次 `forward` 里让某些请求走 beam search、另一些走 sampling——这是上层（如 Triton backend）通过把不同 `beam_width` 的请求分到不同 batch 来实现的。

真正能在「同一次 `forward`、不同请求」间混用的是 **Top-K 与 Top-P**（两者都属于 sampling，即 `beam_width==1` 分支）。机制如下：

```text
假设 batch=3，运行期参数为：
    runtime_top_k = [4,   0,   4  ]     # 0 表示「这个请求不走 Top-K」
    runtime_top_p = [0.0, 0.5, 0.5]     # 0.0 表示「这个请求不走 Top-P」

则两个后端串行跑在同一份 logits 上：
    topk_decode_ 处理请求 0、2（k=4），        跳过请求 1（k=0）
    topp_decode_ 处理请求 1（p=0.5），          跳过请求 0、2（p=0.0）
```

每个后端内部用一个 `skip_decode[batch]` 数组标记「这个请求归不归我管」。请求 0 走纯 Top-K，请求 1 走纯 Top-P，请求 2 走 Top-K（也可以同时设 `top_p`，那样会在 Top-K 缩减后的候选集上再做一次 Top-P 截断）。两个后端靠 `skip_decode` 互不干扰地协作。

对于 beam search（`beam_width>1`），「不同请求不同参数」的混用则受限于 4.2 节：只有那 6 个参数取值一致时才能整批跑；否则退化为逐请求。

#### 4.3.3 源码精读

`setup` 实现在 [DynamicDecodeLayer.cc:L152-L178](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L152-L178)，函数顶部的注释完整列出了所有合法的运行期参数名（包括 `runtime_top_k`、`runtime_top_p`、`top_p_decay` 等），值得通读。关键三行：

```cpp
has_diff_runtime_args_ = hasDiffRuntimeArgs(runtime_args);   // 先探测同批一致性
if (beam_width == 1) {  // sampling layers
    topk_decode_->setup(batch_size, beam_width, runtime_args);
    topp_decode_->setup(batch_size, beam_width, runtime_args);
}
```

见 [DynamicDecodeLayer.cc:L173-L177](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L173-L177)。注意 `setup` **只对 sampling 后端调用**——beam search 后端不需要提前 setup 运行期参数，因为它直接从 `forward` 的 `input_tensors` 里读取。`hasDiffRuntimeArgs` 在这里被调用一次，结果缓存到 `has_diff_runtime_args_`，供随后 `forward` 的 beam search 分支使用。这也是 `setup` 与 `forward` 之间隐含的数据依赖。

sampling 分支的串行协作在 [DynamicDecodeLayer.cc:L437-L444](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L437-L444)，源码注释把上面那个 `[4,0,4]`/`[0.0,0.5,0.5]` 的例子讲得很清楚：

```cpp
// Currently, we support batch sampling. If the runtime arguments are like
// topk = [4, 0, 4]. topp = [0.0, 0.5, 0.5]
// then topk_decode handles [4, x, 4 + 0.5]
//      topp_decode handles [x, 0.5, x]
// where "x" are skipped.
topk_decode_->forward(&decode_output_tensors, &decode_input_tensors);
topp_decode_->forward(&decode_output_tensors, &decode_input_tensors);
```

`skip_decode` 的判定在 Top-K 后端内部，[TopKSamplingLayer.cu:L75](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L75)：

```cpp
skip_decode[i] = k == 0;   // top_k 为 0 → 这个请求不归 Top-K 管
```

Top-P 后端同理：`top_p == 0.0` 的请求会被跳过。两个后端共享 `BaseSamplingLayer` 基类里的 `skip_decode_`/`skip_any_` 字段，见 [BaseSamplingLayer.h:L44-L51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.h#L44-L51)。

还有一个精妙点：当 `skip_any_==true`（本批里有请求需要跳过）时，Top-K 不能在原始 logits 上原地写，否则会污染还没轮到 Top-P 处理的请求。因此它会先把 logits 拷到一份 `runtime_logits_buf_`，见 [TopKSamplingLayer.cu:L218](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L218)：

```cpp
T* logits = !skip_any_ ? input_tensors->at("logits").getPtr<T>() : runtime_logits_buf_;
```

这样两个后端就能安全地「接力」处理各自的请求，而互不破坏对方的输入。

#### 4.3.4 代码实践

**实践目标**：解释 `DynamicDecodeLayer` 如何在「同一次推理、不同请求」间分别使用不同采样策略，并列出至少 4 个 `runtime_arg` 名称。

**操作步骤**：

1. 打开 [DynamicDecodeLayer.cc:L437-L444](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L437-L444)，抄下 `[4,0,4]`/`[0.0,0.5,0.5]` 的例子，在纸上标出每个请求最终走 Top-K、Top-P 还是两者。
2. 跳到 [TopKSamplingLayer.cu:L75](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L75)，确认 `k==0` 是 Top-K 跳过某个请求的唯一触发条件。
3. 回到 [DynamicDecodeLayer.h:L48-L53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h#L48-L53) 与 `setup` 注释 [DynamicDecodeLayer.cc:L159-L170](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.cc#L159-L170)，把所有运行期参数名汇总成一张表。

**需要观察的现象**：同一份 `logits` 被 `topk_decode_` 和 `topp_decode_` 串行消费；每个后端只改写「归自己管」的那些请求对应的行。

**预期结果**（参考答案）：

- **机制**：beam search 与 sampling 的选择由 `beam_width` 在每次 `forward` 固定，不可在同一次 `forward` 内混用；真正能按请求混用的是 sampling 内部的 Top-K 与 Top-P，靠 `top_k==0`/`top_p==0.0` 触发 `skip_decode`，两个后端串行接力、用 `runtime_logits_buf_` 避免污染。
- **至少 4 个 runtime_arg 名称**：`runtime_top_k`、`runtime_top_p`、`temperature`、`len_penalty`、`repetition_penalty`、`beam_search_diversity_rate`、`presence_penalty`、`min_length`（任选 4 个即可，前两个是最典型的「可逐请求不同」参数）。

> ⚠️ 说明：本实践为源码阅读型实践，不涉及命令行运行。如果你想实际验证行为，可在具备 GPU 的环境下参考 u1-l4 的 GPT 示例，通过 `request_config` 里的采样参数构造一个 `top_k=[4,0,4]` 风格的 batch 并观察输出——具体运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `topk_decode_` 和 `topp_decode_` 必须串行调用，而不能并行？

> **答案**：因为它们读写的是**同一份 logits** 和同一批输出槽位。Top-K 可能把 logits 缩减/改写进 `runtime_logits_buf_`，Top-P 要基于 Top-K 之后的中间结果继续处理（如「先 Top-K 再 Top-P」的组合采样）。并行会引发数据竞争，串行才能保证「接力」语义正确。

**练习 2**：若某个请求同时给了 `top_k=4` 和 `top_p=0.5`，会发生什么？

> **答案**：Top-K 后端不会跳过它（`k!=0`），先在词表里选出 top 4 个候选；随后 Top-P 后端也不会跳过它（`p!=0.0`），在 Top-K 缩减后的候选集上再做一次累积概率截断。这是 FT 支持的「Top-K + Top-P 组合采样」，`runtime_logits_buf_` 的副本机制正是为此而设。

**练习 3**：`setup` 为什么在 `beam_width>1` 时不调用 beam search 后端的 `setup`？

> **答案**：beam search 后端（`OnlineBeamSearchLayer`）直接从 `forward` 的 `input_tensors` 里读取它需要的运行期参数，不需要提前把参数拷到 device buffer；而 sampling 后端的 kernel 设计要求参数预先落在 `runtime_top_k_buf_` 等 buffer 里，所以 sampling 必须先 `setup`。这是两个家族在工程实现上的差异。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全链路追踪」任务：

**场景**：假设你接到一个需求——在一个 batch 里，请求 A 要用 beam search（`beam_width=4`），请求 B 要用 Top-P=0.9 采样，请求 C 要用 Top-K=10 采样。

**任务**：

1. **判断可行性**：参考 4.3.2 节，说明这三个请求能否塞进**同一次** `DynamicDecodeLayer::forward`。如果不能，应该如何在上层（如 Triton backend 的请求调度）拆分？
2. **追踪 beam search 路径**：若请求 A 单独成批（`beam_width=4`），且 A 的 `temperature=[0.8, 0.8, 0.8, 0.8]`（4 条 beam），画出从 [ParallelGpt.cc:L821](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L821) `setup` 到 [ParallelGpt.cc:L1501](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1501) `forward` 的调用链，并指出 `hasDiffRuntimeArgs` 此时返回什么、`dynamic_decode_batch_size` 取多少。
3. **追踪 sampling 路径**：若把请求 B 和 C 合成一个 batch（`beam_width=1`，`runtime_top_p=[0.9, 0.0]`、`runtime_top_k=[0, 10]`），描述 `topk_decode_` 与 `topp_decode_` 各自处理哪条请求、跳过哪条请求，并指出 `skip_any_` 会让 Top-K 走哪条 logits 指针分支（参考 [TopKSamplingLayer.cu:L218](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L218)）。

**参考思路**：

- 第 1 问：不能。`beam_width` 在一次 `forward` 内全批固定。应在调度层把 `beam_width>1` 与 `==1` 的请求分到不同 batch。
- 第 2 问：`temperature` 全部相同 → `hasDiffRuntimeArgs` 返回 `false` → `dynamic_decode_batch_size = local_batch_size`，beam search 整批一次跑。
- 第 3 问：`topk_decode_` 跳过 B（`top_p=0.9` 那条对应 `top_k=0`）、处理 C；`topp_decode_` 处理 B、跳过 C（`top_p=0.0`）。因存在跳过，`skip_any_=true`，Top-K 使用 `runtime_logits_buf_` 而非原始 logits 指针。

> 这一步为源码阅读型综合实践，不要求实际编译运行；如需运行验证，参考 u1-l4 与 u8-l3 的示例配置，结果**待本地验证**。

## 6. 本讲小结

- `DynamicDecodeLayer` 是解码层的**统一外观**：对外一个 `forward(TensorMap*, TensorMap*)`，对内聚合 `online_beamsearch_decode_`、`beamsearch_decode_`（已废弃）、`topk_decode_`、`topp_decode_` 四个后端，全部在 `initialize()` 一次性构造。
- 分发依据是 `beam_width`（从 `logits.shape[1]` 读出）：`>1` 走 online beam search，`==1` 走 Top-K + Top-P 串行 sampling。
- `runtime_arg_names_` 列出 6 个「运行期可变、但 beam search 不支持批量」的参数（`beam_search_diversity_rate`、`temperature`、`len_penalty`、`repetition_penalty`、`presence_penalty`、`min_length`）；`hasDiffRuntimeArgs` 在 CPU 上探测同批一致性，结果驱动 beam search 是否退化为逐请求。
- `setup` 与 `forward` 必须配对：`setup` 把运行期参数拷到 device buffer（仅 sampling 需要），`forward` 做实际解码；`has_diff_runtime_args_` 是两者之间的隐式数据通道。
- 「同一次 `forward`、不同请求走不同策略」的真相：beam search 与 sampling 不可混用（`beam_width` 全批固定）；能按请求混用的是 sampling 内部的 Top-K 与 Top-P，靠 `top_k==0`/`top_p==0.0` 触发 `skip_decode`，两个后端串行接力、用 `runtime_logits_buf_` 避免污染原始 logits。
- 构造期参数全部填 deprecated 占位值，真正起作用的值由每次 `setup(runtime_args)` 注入，体现 FT 从「构造期配置」向「运行期配置」的演进。

## 7. 下一步学习建议

- **u8-l2（Beam search 层）**：深入 `OnlineBeamSearchLayer` 的 topk 选择与 penalty 计算，理解本讲里 `online_beamsearch_decode_` 内部到底怎么做候选扩张与收缩。
- **u8-l3（Sampling 层：Top-K 与 Top-P）**：精读 `TopKSamplingLayer`/`TopPSamplingLayer` 的 `runSampling`，搞清本讲提到的 `skip_decode_`、`runtime_logits_buf_`、`runtime_top_k_buf_` 在 kernel 层面如何落地。
- **回到 u6-l1/u5-l2**：把本讲放到生成主循环里再看一遍，理解 `dynamic_decode_layer_->forward` 在每步「decoder 前向 → logits GEMM → 动态解码 → early stop」中的位置。
- **扩展阅读**：若你关心 Triton backend 如何按 `beam_width` 调度请求，可跳到 u10-l3，看 `ParallelGptTritonModelInstance` 如何组织 batch——那是「上层拆分」的现实落点。
