# 采样与分词

## 1. 本讲目标

本讲是 C++ 运行时核心（u5 单元）的收尾篇。前面几讲已经讲清楚了 `LLMInferenceRuntime`、`handleRequest`、引擎执行器、解码策略与 KV 缓存。本讲要把这条流水线的**两端**补齐：

- **输入端（分词）**：用户的一段字符串 prompt，是怎么变成模型能吃的 `token id` 序列的。
- **输出端（采样）**：模型每一步前向算出的 logits（词表上的原始分数），是怎么变成下一个 `token id` 的。

学完后你应该能够：

1. 说清楚 greedy / top-k / top-p / temperature 四个采样旋钮各自的含义，以及运行时如何分流处理。
2. 读懂 `sampling.cu` 里 top-k 的两阶段 GPU kernel 与 top-p 的「softmax + 排序 + 前缀和采样」流水线。
3. 说清楚一次完整 token 来回：字符串 →（分词）→ input ids →（模型）→ logits →（采样）→ next token id →（解码）→ 文本。
4. 理解 EdgeLLM 自己实现的 BPE 分词器如何处理特殊 token、byte fallback、normalizer 与 chat 模板。
5. 了解采样为什么要在 GPU 上批量执行，以及它与解码策略层（u5-l4）的接口关系。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

**logits（对数几率）**：模型最后一层 `lm_head` 输出的、长度等于词表大小 `vocabSize` 的浮点向量。它还没有归一化，不是概率。位置 `i` 的值越大，模型越「倾向」下一个 token 是第 `i` 个词。采样要解决的就是「给定 logits，挑一个 token 出来」。

**softmax 与温度（temperature）**：把 logits 变成概率的标准做法是 softmax。为了数值稳定，先减去最大值再做指数：

\[ p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)} \]

其中 \(T\) 就是温度。

- \(T\) 越小（趋近 0）：分布越「尖」，几乎必然挑最大那个 token → 接近贪心（greedy）。
- \(T = 1\)：不缩放，按模型原始概率采样。
- \(T\) 越大：分布越「平」，采样越随机、越有创造性。

**greedy（贪心）**：不做随机，直接选 logits 最大的那个 token，即 argmax。确定性、可复现，但容易重复、缺乏多样性。

**top-k 采样**：只在 logits 最大的 K 个候选里做 softmax+随机采样，其余直接丢弃。K 越小越保守。

**top-p（nucleus / 核采样）**：把所有候选按概率从大到小排序，累加概率，只保留累加值首次达到 p（如 0.9）的那个「最小集合」，在这个集合里采样。候选数量是动态的，比 top-k 更自适应。

**token id（rank）**：词表里每个 token 的整数编号。分词器（tokenizer）负责把字符串映射成 `token id` 序列（encode），也负责把 `token id` 序列映射回字符串（decode）。

**BPE（Byte Pair Encoding，字节对编码）**：主流 LLM 用的分词算法。核心思想：先把文本拆成最小单元（字节或字符），再按一张预先学好的「合并表」反复把相邻两个单元合并成更大的 token，直到无法合并为止。合并的优先级决定了切分结果。

## 3. 本讲源码地图

本讲涉及两个目录：`cpp/sampler/`（采样）和 `cpp/tokenizer/`（分词）。

| 文件 | 作用 |
| --- | --- |
| [cpp/sampler/sampling.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.h) | 采样公共接口：`SamplingParams` 结构、`topKtopPSamplingFromLogits`、`selectAllTopK`、`applyLogitBias` 等声明 |
| [cpp/sampler/sampling.cu](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu) | 采样的 CUDA 实现：top-k / top-p kernel、softmax、workspace 管理 |
| [cpp/sampler/samplingUtils.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/samplingUtils.cpp) | `shouldUseNonGreedySampling` 判定函数（greedy vs 随机采样分流） |
| [cpp/tokenizer/tokenizer.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.h) | `Tokenizer` 类：encode / decode / chat 模板、特殊 token 处理 |
| [cpp/tokenizer/tokenizer.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp) | `Tokenizer` 的实现：从 HF 目录加载、分词主流程、流式解码 |
| [cpp/tokenizer/tokenEncoder.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenEncoder.h) | `TokenEncoder`：BPE 编码算法与词表 |
| [cpp/tokenizer/preTokenizer.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/preTokenizer.h) | `PreTokenizer`：分词前的正则切分（RegexSplit / Sequence） |
| [cpp/runtime/decoding/vanillaDecoder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp) | 调用采样函数的「上游」：解码策略层如何把 logits 交给采样器 |
| [unittests/samplingTests.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/samplingTests.cpp) | 采样的单元测试，是验证理解的最佳材料 |

## 4. 核心概念与源码讲解

### 4.1 GPU 采样：从 logits 到 token

#### 4.1.1 概念说明

采样模块要回答一个问题：**给定一个 `[batch, vocab]` 的 logits 张量，每个 batch 元素输出一个 token id**。

这里有三层含义需要注意：

1. **批量**：EdgeLLM 是面向边缘设备的批处理推理框架，一条请求里有多个序列（slots）。采样必须一次性处理整个 batch，每个序列独立采一个 token。这就是为什么采样用 GPU kernel 而不是 CPU for 循环——batch 越大，GPU 并行的收益越高。

2. **确定性 vs 随机性**：
   - 当用户不想要随机性（`temperature=1.0, topK<=1, topP=1.0`，或温度趋近 0），应该走 **greedy**：直接取 argmax，又快又确定。
   - 当需要随机性（`topK>1`、`topP<1`、或 `temperature≠1`），才走代价更高的 **随机采样**路径。

   这个分流是性能关键：greedy 不需要 softmax、不需要排序、不需要随机数，只需一次 top-1 归约。

3. **温度的作用点**：温度并不是采样函数独有参数，它通过对 logits 乘 `1/T`（即 `logits * invTemp`）来起作用。温度趋近 0 时 `invTemp` 趋近无穷，会让 softmax 数值不稳定，所以代码里对极小温度做了特殊处理。

#### 4.1.2 核心流程

采样的整体分流逻辑（由解码策略层驱动）：

```
VanillaDecoder::decodeStep
  ├── base 引擎前向，得到 outputLogits [batch, vocab]
  ├── applyLogitBias(logits, ...)        # 可选：给某些 token 加偏置（如禁用某词）
  └── shouldUseNonGreedySampling(temp, topK, topP)?
        ├── true  → topKtopPSamplingFromLogits(...)   # 随机采样（top-k 或 top-p）
        └── false → selectAllTopK(..., topK=1, ...)     # greedy：取 top-1
  └── (可选) mapReducedVocabToFullVocab(...)            # 词表裁剪时把缩减 id 映射回全词表
```

**top-k 路径**（`useTopK=true`）是两阶段 kernel：

1. **Stage 1（`topKStage1`）**：每个 batch 用 `BLOCKS_PER_BEAM=8` 个 block 并行扫一遍 vocab，迭代 K 次，每次用 block 级归约（`cub::BlockReduce`）找出当前最大值并把它屏蔽掉，最终每个 batch 收集到 `8*K` 个候选。在此阶段顺便把 logits 乘上 `invTemp` 做温度缩放。
2. **Stage 2（`topKStage2Sampling`）**：对 `8*K` 个候选再做一次排序，取前 K 个，对这 K 个做 softmax 归一化，然后用 `curand` 生成一个随机数 `r ∈ [0, topP * sum)`，沿累积概率做线性扫描，落到哪个 token 就选哪个。

**top-p 路径**（`useTopP=true`，仅当未启用 top-k 时）是五阶段：

1. **softmax**：`softmaxKernelImpl` 把 `[batch, vocab]` 的 logits 变成概率（带温度、数值稳定的三遍归约）。
2. **初始化**：`topPInitialize` 给每个 vocab 位置生成它的原始 id 值与分段偏移表。
3. **早退优化**：`topPBeamTopKKernel` 先检查「最大概率那个 token 是否已 ≥ topP」，若是则直接选它、标记早退——大部分高置信度步可以跳过昂贵的全词表排序。
4. **排序**：用 CUB 的 `DeviceSegmentedRadixSort::SortPairsDescending` 对每个 batch 的概率降序排序（同步把 id 带上）。
5. **采样**：`topPSampling` 用 block 级前缀和（`cub::BlockScan`）在排序后的概率上累积，找到一个随机阈值 `r * topP` 落在哪个 token。

**greedy 路径**复用了 `selectAllTopK`（传 `topK=1`）：它内部用和 top-k 一样的两阶段归约，只是不排序、不 softmax、不取随机数，直接返回最大值那个 id。

#### 4.1.3 源码精读

**① 采样参数与 greedy 判定**

`SamplingParams` 把四个旋钮（temperature/topK/topP）与两个开关（useTopK/useTopP）打包。注意构造函数里对「温度趋近 0」的特殊处理——这关系到数值稳定性：

[cpp/sampler/sampling.h:30-82](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.h#L30-L82) —— `SamplingParams` 结构定义。

关键点：

- `useTopK = topK > 0`、`useTopP = topP < 1.0f`（见 55-56 行）。
- 构造函数要求「topK 或 topP 至少一个被设置」，否则抛 `invalid_argument`（58-61 行）。
- 当 `temperature < 1e-3f`（即趋近 0）时，强行把 `topK=1, topP=1.0f`（68-80 行），并打一条 warning。原因是极小温度会让 `invTemp` 爆炸、softmax 溢出，此时语义上就是贪心，干脆直接走 greedy。

`shouldUseNonGreedySampling` 是 greedy 与随机采样的「路由器」，它非常简洁：

[cpp/sampler/samplingUtils.cpp:25-33](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/samplingUtils.cpp#L25-L33) —— greedy 判定逻辑。

解读：`topK==1` 强制 greedy（只有一个候选，没得随机）；否则当 `topK>1`、或 `topP<1`、或温度明显偏离 1（`>1e-3` 且 `|T-1|>1e-3`）时才走随机采样。默认的 `(1.0, ≤1, 1.0)` 与近零温度都判定为 greedy。

**② 解码层如何调用采样**

这是理解 greedy 与 top-p 差异的**最佳入口**——`vanillaDecoder.cpp` 里这一段把分流写得清清楚楚：

[cpp/runtime/decoding/vanillaDecoder.cpp:111-131](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L111-L131) —— 采样调用现场。

这段的语义是：

- 先 `applyLogitBias(...)`（111 行）给 logits 加偏置（如对某些 token 加大负值来「禁词」）。
- 114 行用 `shouldUseNonGreedySampling` 分流：true 走 `topKtopPSamplingFromLogits`（116-119 行，带 temperature/topK/topP），false 走 `selectAllTopK(..., topK=1)`（123-125 行，greedy）。
- 128-131 行：若启用了词表裁剪（u9-l3 会讲），把「缩减词表里的 id」映射回「全词表 id」。

注意：采样结果写进 `mRuntime.sampling.indices`（形状 `[batch, 1]`），这就是每一步选出的 next token。采样器本身对模型一无所知，只认 logits 和参数——这与 u5-l4「解码算法与运行时主循环解耦」的设计一致。

**③ top-k 两阶段 kernel**

Stage 1 在拷贝 logits 时顺带乘温度，然后迭代找 top-K：

[cpp/sampler/sampling.cu:415-476](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L415-L476) —— `topKStage1`：温度缩放 + 并行找 top-K。

温度缩放用的是这个 device 函数（注意它对近零温度的兜底）：

[cpp/sampler/sampling.cu:271-274](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L271-L274) —— `invTemp_device`：`temperature < 1e-3` 时返回 1000.0（近似贪心），否则 `1/T`。

Stage 2 在 `8*K` 个候选里排序、softmax、采样。重点是随机数生成与线性扫描（552-593 行）：

[cpp/sampler/sampling.cu:479-594](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L479-L594) —— `topKStage2Sampling`：排序 + softmax + 采样。

关键细节（556 行）：`curand_init(philoxSeed, batchIdx, philoxOffset, &localState)` —— 用 philox 伪随机数生成器，**种子是 batch 级别的**（`batchIdx` 作 subsequence），所以同一 batch 里不同序列得到不同的随机流，保证独立性；相同 seed+offset 则结果可复现。

**④ top-p 路径：softmax + 排序 + 前缀和采样**

数值稳定的 softmax 是三遍 block 归约（求 max → 求 exp 之和 → 归一化）：

[cpp/sampler/sampling.cu:600-652](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L600-L652) —— `softmaxKernelImpl`：这个模板同时服务 softmax（`LOG_OUTPUT=false`）和 log-softmax（`LOG_OUTPUT=true`，给 logprobs 用）。

top-p 的早退优化——大部分高置信度步可以不排序直接出结果：

[cpp/sampler/sampling.cu:736-784](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L736-L784) —— `topPBeamTopKKernel`：若最大概率已 ≥ topP 则早退。

最终采样用 block 级前缀和（`cub::BlockScan`）在排序后的概率上累积，找到随机阈值落点（注意 828 行 `__syncthreads_count` 用于「投票」找首个超过阈值的线程）：

[cpp/sampler/sampling.cu:787-844](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L787-L844) —— `topPSampling`：前缀和采样。

**⑤ 顶层分流函数**

`topKtopPSamplingFromLogits` 是采样模块对外的主入口，按 `useTopK` / `useTopP` 分发到上面两套 kernel，并负责 workspace 的切分：

[cpp/sampler/sampling.cu:847-938](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L847-L938) —— `topKtopPSamplingFromLogits`：校验 + workspace 切分 + 分发。

注意 881-898 行（top-k 分支）与 899-937 行（top-p 分支）是互斥的——这就是「top-k 与 top-p 在采样器内部是二选一」的体现。`SamplingParams` 构造函数保证不会两者皆空。

greedy 路径用的 `selectAllTopK` 实现在这里（它复用了 top-k 的两阶段归约，但 Stage 2 用的是只取值不采样的 `topKStage2ReturnAllTopK`）：

[cpp/sampler/sampling.cu:940-1002](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.cu#L940-L1002) —— `selectAllTopK`：greedy 与 logprobs 共用的 top-K 选择。

> **关于 workspace**：采样需要大量中间显存（临时 logits、候选缓冲、CUB 排序临时区）。`getTopKtopPSamplingWorkspaceSize` / `getSelectAllTopKWorkspaceSize` 用来提前算大小，运行时一次性分配一个 `Int8` workspace 传进来（见 `SamplingWorkspace::setupWorkspace`，39-156 行）。所有缓冲都按 256 字节对齐以满足 GPU 访存与 CUB 要求。这与 u5-l3「执行器不独占显存、workspace 由运行时统一管理」的设计一脉相承。

#### 4.1.4 代码实践

**实践目标**：通过阅读单元测试，验证你对 greedy / top-k / top-p 分流与结果正确性的理解。

**操作步骤**：

1. 打开 [unittests/samplingTests.cpp:284-294](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/samplingTests.cpp#L284-L294) 的 `ShouldUseNonGreedySampling` 测试，逐行解释为什么前 4 行 `EXPECT_FALSE`、后 4 行 `EXPECT_TRUE`。
2. 打开 [unittests/samplingTests.cpp:500-598](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/samplingTests.cpp#L500-L598) 的 `SamplingAccuracy` 测试。它构造了 `batchSize∈{1,4}`、`vocabSize=20` 的随机 logits，跑一组采样配置（`{"TopK", 20, 1.0, 1.0}`、`{"TopP", 0, 0.9f, 1.0f}`、`{"TopKTopP", 20, 0.9f, 1.0f}` 等），然后用 CPU 参考实现 `getTopKAllowedTokensRef` / `getTopPAllowedTokensRef` 算出「合法候选集合」，断言 GPU 采出的 token 必须落在合法集合内。
3. 如果你本机有 GPU 并按 u1-l3 构建了带单测的 C++ 工程，可执行（路径待本地确认）：
   ```bash
   ./bin/unittests --gtest_filter='SamplingTestSuites.SamplingAccuracy'
   ```

**需要观察的现象**：

- `TempZero` 配置（`{"TempZero", 20, 0.9f, 0.0f}`）虽然传了 `topK=20, topP=0.9`，但因为 `temperature=0.0`，`SamplingParams` 构造函数会把它覆盖成 `topK=1, topP=1.0`，于是最终走 greedy、永远只选最大那个 token。这正是 4.1.3 ① 讲的兜底逻辑。
- 同一份 logits、固定 `TEST_SEED=42`，top-k/top-p 多次运行结果应一致（可复现）；换 seed 则结果可能变化（随机性）。

**预期结果**：测试全部 `PASS`，输出一张方法 × batch × topK/topP/temp × 精度的表格。**如无法在本机运行，明确标注「待本地验证」**，重点放在读懂断言逻辑上。

#### 4.1.5 小练习与答案

**练习 1**：为什么 greedy 路径不直接调用 `topKtopPSamplingFromLogits(topK=1)`，而是单独用 `selectAllTopK(topK=1)`？

**参考答案**：`selectAllTopK` 只做归约取最大值，**不做 softmax、不生成随机数、不需要排序候选**（见 `topKStage2ReturnAllTopK`，它把选中值直接写回，325-390 行）。而 `topKtopPSamplingFromLogits` 即便 `topK=1`，Stage 2 仍会走 softmax+curand 采样路径（479-594 行），多出无谓开销且引入随机性。greedy 要的是确定性的 argmax，所以走更轻的 `selectAllTopK`。

**练习 2**：`shouldUseNonGreedySampling(1.0f, 0, 1.0f)` 返回什么？`shouldUseNonGreedySampling(0.7f, 1, 0.95f)` 又返回什么？为什么后者是这样？

**参考答案**：前者返回 `false`（默认三元组，走 greedy）；后者也返回 `false`，因为 `topK==1` 强制 greedy——只有一个候选，再做随机采样毫无意义（见 `samplingUtils.cpp:28-31`）。

**练习 3**：top-p 路径里的「早退优化」（`topPBeamTopKKernel`）解决什么问题？

**参考答案**：top-p 的完整代价是「对整个词表降序排序」，非常贵。但很多步模型对下一个 token 极度自信（最大概率本身就 ≥ topP），此时核集合只有一个 token，直接选它即可，省掉全词表排序。早退标志 `earlyExitFlags[batchId]=1` 让后续跳过 CUB 排序（见 766-783 行）。

---

### 4.2 分词器：字符串 ↔ token id

#### 4.2.1 概念说明

分词器（tokenizer）是「自然语言」与「模型整数序列」之间的翻译官。EdgeLLM 没有依赖 Python 的 `tokenizers` / `sentencepiece`，而是在 C++ 里**自己重新实现**了一份与 HuggingFace `tokenizer.json` 兼容的分词器。原因和采样一样：边缘部署要纯 C++、无 Python 运行时依赖（承接 u1-l1 的定位）。

一个完整分词器由三部分组成：

1. **normalizer（归一化器）**：编码前对文本做替换，例如把空格替换成 SentencePiece 的 `▁`。对应 `tokenizer.json` 的 `normalizer` 字段。
2. **pre_tokenizer（预分词器）**：用正则把文本切成「词块」，例如 GPT 那条著名的正则会把 `Hello world` 切成 `["Hello", " world"]`。每个词块再独立做 BPE。
3. **encoder（编码器）**：把词块用 BPE 算法编码成 token id 序列。核心是词表 `vocab`（token 字符串 → id）与合并优先级。

几个关键概念：

- **特殊 token（special token）**：如 `<|im_start|>`、`<|endoftext|>`、`<image>`。它们在编码时必须被**原样识别成一个整体 id**，不能被 BPE 拆碎。`<|endoftext|>` 通常是 EOS。
- **byte fallback（字节回退）**：SentencePiece 风格分词器（如 Gemma）遇到未知字符时，会退化到「逐字节」编码，每个字节存成 `<0xNN>` 形式的 token。解码时再把这些 `<0xNN>` 还原成原始字节。
- **BPE 合并优先级**：tiktoken 风格（如 Qwen）直接用「合并后字符串在词表里的 rank」作为合并优先级；SentencePiece 风格（如 Gemma）则用 `tokenizer.json` 里独立的 `merges` 列表显式定义优先级，二者不能混用。

#### 4.2.2 核心流程

**加载流程**（`loadFromHF`）：

```
loadFromHF(modelDir)
  ├── 解析 tokenizer.json  → parseTokenizerConfig
  │     ├── 读 normalizer → mNormalizerReplacements
  │     ├── 读 decoder     → mDecoderReplacements / mByteFallbackDecode
  │     ├── 读 pre_tokenizer → createPreTokenizer (RegexSplit / Sequence)
  │     ├── determineEncoderType (BPE/Unigram/WordPiece)
  │     ├── loadVocabulary → vocab map
  │     └── loadSpecialTokens → specialTokens map
  ├── 解析 tokenizer_config.json → parseSpecialTokenConfig
  │     └── 解析 bos_token / eos_token / pad_token / unk_token / context_image_token 的 id
  ├── mTokenEncoder->initialize(vocab, specialTokens)
  └── loadChatTemplate(processed_chat_template.json)  ← 必需
```

**编码流程**（`encode`）：

```
encode(text, addBos, addEos)
  ├── (可选) 头部加 BOS
  ├── partitionSpecialTokens(text)
  │     └── 把文本切成「特殊 token 片段」与「原始文本片段」交替的链表
  └── 对每个片段:
        ├── 特殊 token → 直接 push 它的 id
        └── 原始文本:
              ├── 应用 normalizer 替换 (如 space → ▁)
              ├── preTokenizer->process(piece) → 切成若干子串
              └── 对每个子串 mTokenEncoder->encode → BPE 合并 → push id 序列
  └── (可选) 尾部加 EOS
```

**解码流程**（`decode`）：

```
decode(tokens, skipSpecialTokens)
  ├── mTokenEncoder->decode(tokens, raw, skipSpecial)  → 把 id 拼成原始字节串
  ├── (若 byte fallback) 把 <0xNN> 还原成原始字节
  ├── 应用 decoder 替换 (如 ▁ → space，是 normalizer 的逆操作)
  └── UTF-8 清洗 → 保证输出是合法 UTF-8 (非法字节替换为 U+FFFD)
```

**流式解码**（`emitDelta` / `emitDeltaFlush`）：用于流式输出。每来一个新 token，查它的 piece 字节，拼到上一次没发完的残缺字节后面，过一遍 `sanitizeUtf8Streaming`，输出**合法 UTF-8 增量**，把仍不完整的尾部字节留在 `pendingBytes` 里等下一次（承接 u5-l2 的 `StreamChannel`）。

#### 4.2.3 源码精读

**① 加载：从 HF 目录建出分词器**

[cpp/tokenizer/tokenizer.cpp:60-137](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L60-L137) —— `loadFromHF`：加载主流程。

关键约束（124-132 行）：`processed_chat_template.json` 是**必需**的，缺失则加载失败。这个文件是 Python 导出端（u2-l6）产出的 sidecar——它把 Jinja chat 模板预处理成结构化 JSON，避免在 C++ 里跑 Jinja。

[cpp/tokenizer/tokenizer.cpp:489-511](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L489-L511) —— `determineEncoderType`：按 `tokenizer.json` 的 `model.type` 选编码算法（BPE/Unigram→SENTENCEPIECE/WordPiece），默认 BPE。

[cpp/tokenizer/tokenizer.cpp:140-355](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L140-L355) —— `parseTokenizerConfig`：解析 normalizer / decoder / pre_tokenizer / vocab / merges。

这里有两个值得注意的细节：

- **byte fallback 与 merges 的耦合**（301-352 行）：只有 SentencePiece 风格（`byte_fallback=true`）的分词器才会显式加载 `merges` 列表作为合并优先级；tiktoken 风格（如 Qwen）虽然也有 merges，但用 vocab rank 排序，加载它反而会破坏切分（注释 303-304 行明确说明）。
- **vocab key 的解码**（`loadVocabulary`，513-550 行）：GPT-2 风格词表把字节编码成可见 unicode（如 `\n` 存成 `Ċ`），需要 `decodeHFTokenToNormal` 还原；而 byte fallback 词表直接存原始字符，**不能**做这个还原（521-524 行注释给出反例：否则 `'\n'` 和 `'Ċ'` 会冲突）。

**② 编码：特殊 token 切分 + BPE**

[cpp/tokenizer/tokenizer.cpp:606-698](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L606-L698) —— `encode`：编码主流程。

核心是 `partitionSpecialTokens`（636 行调用）——它用 `forward_list<textPartition>` 把文本切成「特殊 token / 原始文本」交替的链表：

[cpp/tokenizer/tokenizer.cpp:700-778](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L700-L778) —— `partitionSpecialTokens`：扫描每个特殊 token 字符串，把命中处从原始文本里「切出来」单独成段。

`textPartition` 这个小联合体（88-127 行）用 `type` 字段区分两种片段，是理解切分逻辑的钥匙：

[cpp/tokenizer/tokenizer.h:88-127](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.h#L88-L127) —— `textPartition`：用同一结构表达「特殊 token（带 id）」与「原始文本片段（带 offset/length 引用）」。

切分后，原始文本片段依次过 normalizer → preTokenizer → tokenEncoder。normalizer 就是一个简单的字符串替换循环（654-663 行）。

**③ BPE 合并算法**

BPE 的核心在 `TokenEncoder::bytePairEncode`。算法是经典的「反复合并优先级最高的相邻对」：

[cpp/tokenizer/tokenEncoder.cpp:170](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenEncoder.cpp#L170) —— `bytePairEncode` 入口。

它的合并优先级来源由 `getPairPriority` 决定（199-214 行）：

- 若 `mUseMergePriorities=true`（SentencePiece 风格）：用 `left + '\0' + right` 作 key 查 `mMergePriorities` 表（null 字节分隔避免歧义）。
- 否则（tiktoken 风格）：把 `left+right` 拼起来在词表里查 rank，rank 即优先级。

每次循环找当前所有相邻对里**优先级最低**（rank 最小）的那一对合并，直到没有任何相邻对能在词表/merge 表里找到，剩下的单元就是最终 token 序列。初始单元的粒度也不同：tiktoken 按字节、SentencePiece 按 UTF-8 字符（216-240 行）。

[cpp/tokenizer/tokenEncoder.h:45-50](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenEncoder.h#L45-L50) —— `TokenEncoder::Type` 枚举：BPE 已实现，SENTENCEPIECE/WORDPIECE 标注未实现（实际靠 byte fallback + BPE 兼容）。

**④ 解码与 byte fallback**

[cpp/tokenizer/tokenizer.cpp:780-843](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L780-L843) —— `decode`：解码主流程。

byte fallback 还原（798-823 行）把 `<0xNN>` 模式（6 字符）解析回单字节；decoder 替换（826-834 行）是 normalizer 的逆操作（如 `▁ → space`）；最后强制 UTF-8 清洗（839-841 行），非法序列替换为 `U+FFFD`。

**⑤ 流式解码**

[cpp/tokenizer/tokenizer.cpp:888-908](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L888-L908) —— `emitDelta`：把新到的 token 转成合法 UTF-8 增量文本。

它依赖 `idToPiece`（845-886 行）查单个 token 的字节片段。注意 `emitDelta` 的契约（451-453 行注释）：每次解码循环后调用一次，**vanilla 路径来 1 个新 token、投机解码路径来 N 个新 token 都适用**——这是它能同时服务两种解码策略的关键。

[cpp/tokenizer/tokenizer.h:452-453](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.h#L452-L453) —— `emitDelta` 声明与契约。

#### 4.2.4 代码实践

**实践目标**：用真实的 HF 模型目录，在脑子里走一遍「字符串 → input ids → 文本」的完整来回，并定位每一步对应的源码。

**操作步骤**：

1. 找一个你已经 export 过的模型目录（含 `tokenizer.json`、`tokenizer_config.json`、`processed_chat_template.json`，见 u1-l5）。若手头没有，找一个 Qwen / Llama 的 HF 检查点目录即可（前两个文件 HF 自带，`processed_chat_template.json` 需 EdgeLLM 导出产出）。
2. 打开该目录的 `tokenizer_config.json`，找到 `bos_token` / `eos_token` / `pad_token` 字段。对照 [cpp/tokenizer/tokenizer.cpp:393-431](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L393-L431) 的 `parseSpecialToken`，理解这几个字符串是怎么变成 id 的（先在 specialTokens 里查 content，再取 id）。
3. 打开 `tokenizer.json`，看 `pre_tokenizer` 字段是一条 `Regex` 还是 `Sequence`；对照 [cpp/tokenizer/tokenizer.cpp:434-487](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L434-L487) 的 `createPreTokenizer`，判断它会建成 `RegexSplit` 还是 `Sequence`。
4. 看 `model.type` 字段（`BPE` / `Unigram` / `WordPiece`）与是否有 `byte_fallback`，判断你的分词器走 tiktoken 路径还是 SentencePiece 路径。

**需要观察的现象**：

- Qwen 系：`model.type=BPE`、无 `byte_fallback` → tiktoken 风格，按字节切分、用 vocab rank 合并。
- Gemma 系：有 `byte_fallback=true` 且有 `merges` → SentencePiece 风格，按 UTF-8 字符切分、用 merges 优先级合并、解码时还原 `<0xNN>`。

**预期结果**：你能用自己的话写出「`Hello world` 这串文本在你的模型里会经历哪几次切分、哪几次合并、最终是哪几个 id」。**若无法确认实际 id，标注「待本地验证」**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `partitionSpecialTokens` 要先于 BPE 执行？如果某个特殊 token 的字面量（比如 `<s>`）碰巧能被 BPE 拆成更小的 token，会发生什么？

**参考答案**：特殊 token 是「整体语义单元」，必须映射到它自己的 id（如 EOS），绝不能被 BPE 拆碎成 `<`、`s`、`>` 三个普通 token——否则语义全错、EOS 检测也会失效。所以编码时先用 `partitionSpecialTokens` 把所有特殊 token 字面量从文本里「挖」出来单独成段（直接用其 id），只对剩下的原始文本做 normalizer+BPE（见 `encode` 644-689 行）。

**练习 2**：normalizer 与 decoder 替换是什么关系？

**参考答案**：它们互为逆操作。编码时 normalizer 把人类文本转成模型训练时的内部形式（如 `space → ▁`）；解码时 decoder 替换把它转回来（`▁ → space`），见 `encode` 的 654-663 行与 `decode` 的 826-834 行。成对出现才能保证 encode→decode 的往返一致性。

**练习 3**：`emitDelta` 为什么需要 `pendingBytes`？为什么不直接每来一个 token 就立刻输出它的 piece？

**参考答案**：单个 token 的 piece 不一定是合法 UTF-8——一个多字节 UTF-8 码点可能被拆到相邻两个 token 里。若立刻输出，消费者会收到半截码点（非法 UTF-8）。`pendingBytes` 把不完整的尾部字节「攒」起来，等下一个 token 到了再拼上一起过 `sanitizeUtf8Streaming`，保证每次输出都是完整合法的 UTF-8（见 900-907 行）。终态时 `emitDeltaFlush` 把残留字节收尾成 `U+FFFD`。

## 5. 综合实践

把采样与分词串起来，走一遍**完整的 token 来回**。假设用户发了一条请求：`"你好，世界"`。

**任务**：画出从字符串到生成文本的全链路，标注每一步对应的源码函数，并解释采样在哪一步介入。

参考解答（请对照源码核对）：

```
① handleRequest 收到 request（u5-l1）
② tokenizer.applyChatTemplate(request, formattedRequest)        # tokenizer.cpp:1029
   → 套 chat 模板，得到一段拼好的字符串（含角色标记/特殊 token）
③ tokenizer.encode(formattedCompleteRequest, addBos, addEos)    # tokenizer.cpp:606
   → partitionSpecialTokens → normalizer → preTokenizer → BPE
   → 得到 input ids 序列，搬上 GPU
④ prefill：base 引擎用 profile 0 跑一遍，得到首个 token 的 logits（u5-l1）
⑤ decode 循环，每步 VanillaDecoder::decodeStep（u5-l4）：
     a. base 引擎用 profile 1 跑一步 → outputLogits [batch, vocab]  # vanillaDecoder.cpp:103
     b. applyLogitBias(...)                                         # vanillaDecoder.cpp:111
     c. shouldUseNonGreedySampling? → greedy(selectAllTopK) 或 随机(topKtopPSamplingFromLogits)  # :114-126
     d. (可选) mapReducedVocabToFullVocab                           # :128-131
     e. 采样得到 next token id → 写入 sampling.indices
     f. tokenizer.idToPiece(id) → emitDelta → 流式输出合法 UTF-8 文本  # tokenizer.cpp:845/888
     g. 若 id 是 EOS（isEosToken）→ 结束
⑥ 全部结束后，decode/emitDeltaFlush 把残留字节冲刷成最终文本
```

关键观察：

- **分词只在「入口」做一次**（把 prompt 变成 ids），以及**每步在「出口」做一次**（把单个 token id 变成文本片段）。
- **采样在「每一步 decode」都做一次**，它是 logits → token 的唯一桥梁。
- greedy 时第 ⑤c 步走 `selectAllTopK(topK=1)`，确定性；调高 temperature 或开 top-p 时走 `topKtopPSamplingFromLogits`，随机且可复现（由 philoxSeed 控制）。

试着在脑子里替换不同的采样参数，预测输出会如何变化（更确定 vs 更随机），这就是本讲所有概念的交汇点。

## 6. 本讲小结

- 采样模块（`cpp/sampler/`）把 `[batch, vocab]` 的 logits 变成 `[batch, 1]` 的 token id，**全程在 GPU 上批量执行**，因为 EdgeLLM 是批处理推理框架。
- greedy 与随机采样由 `shouldUseNonGreedySampling` 分流：默认三元组 `(temp=1, topK≤1, topP=1)` 与近零温度走轻量的 `selectAllTopK`（argmax）；其余走 `topKtopPSamplingFromLogits`。
- top-k 是「两阶段并行归约 + softmax + curand 采样」；top-p 是「softmax + 早退 + 全词表降序排序 + 前缀和采样」；两者在采样器内部二选一。
- 温度通过对 logits 乘 `1/T` 起作用；近零温度会被 `SamplingParams` 构造函数强行覆盖成 `topK=1`，避免 softmax 数值溢出——语义上就是贪心。
- 分词器（`cpp/tokenizer/`）是 EdgeLLM 自研的纯 C++ BPE 实现，加载需 `tokenizer.json` + `tokenizer_config.json` + `processed_chat_template.json`，兼容 tiktoken（vocab rank 合并）与 SentencePiece（merges 优先级 + byte fallback）两种风格。
- 编码链路是 `partitionSpecialTokens → normalizer → preTokenizer → BPE`；解码链路是 `piece 拼接 → byte fallback 还原 → decoder 替换 → UTF-8 清洗`；流式输出靠 `emitDelta` 攒字节保证每段都是合法 UTF-8。

## 7. 下一步学习建议

- 本讲只讲了 **vanilla 解码**的采样调用点。投机解码（EAGLE/MTP/DFlash）在验证阶段对一整棵候选树做采样与接受判定，会用到 `gatherSpecVerifyAcceptedLogitRows`（sampling.h:246）与 `extractTopKLogprobs`。建议进入 **u7-l1 投机解码策略** 时回头对照本讲的 top-k kernel。
- 采样还与 **logprobs 输出**（`extractTopKLogprobs`，返回 top-K 对数概率，上限 `kMaxLogprobsK=50`）相关，若你想理解 OpenAI 风格的 `logprobs` 字段如何产生，可重读 `sampling.cu` 的 1042-1088 行。
- **词表裁剪**（`mapReducedVocabToFullVocab`）在采样后把「缩减词表 id」映射回「全词表 id」，完整流程见 **u9-l3 词表裁剪、FP8 KV 缓存与 chat 模板**。
- 若要深入「分词如何接入新模型」，结合 **u9-l4 接入一个新模型架构**，看 chat 模板与特殊 token 在端到端流程里的契约。
