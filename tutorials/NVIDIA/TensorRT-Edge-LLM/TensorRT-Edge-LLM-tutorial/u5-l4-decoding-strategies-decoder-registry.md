# 解码策略与解码器注册表

## 1. 本讲目标

学完本讲，你应当能够：

- 说出为什么 EdgeLLM 运行时要单独抽出一个「解码策略层」，以及它把谁和谁解耦了。
- 读懂 `DecodingStrategy` 抽象基类，列出策略类必须实现的全部纯虚方法。
- 解释 `DecoderRegistry` 如何在构造期决定用哪种策略，又如何在「逐请求」层面把投机解码关掉、回退到 vanilla。
- 读懂 `VanillaDecoder::decodeStep` 的自回归单步流程，并说出它与投机解码策略在 `decodeStep` 上的本质差异。

本讲是 u5（C++ 运行时核心）单元里承上启下的一篇：它向上承接 u5-l1 的 `LLMInferenceRuntime::handleRequest`（运行时主循环只是反复调 `DecodingStrategy::decodeStep`），向下为 u7（投机解码专题）铺好抽象接口。

---

## 2. 前置知识

阅读本讲前，请先确认你已经了解下面这些在 u5-l1 已经建立的概念：

- **LLMInferenceRuntime**：所有 LLM 推理的统一入口，用 `unique_ptr` 拥有执行器、KV 缓存、分词器、采样缓冲、多模态 runner，以及本讲的主角——解码器注册表 `DecoderRegistry`。
- **handleRequest 的三阶段**：准备（校验、套 chat 模板、分词、预热系统提示 KV）→ prefill（base 引擎用 profile 0 得首个 token）→ **解码循环**（反复调 `DecodingStrategy::decodeStep`）。
- **prefill / decode 双 profile**：运行时用 `setOptimizationProfileAsync` 在 profile 0（prefill，长序列）与 profile 1（decode，单 token）之间切换。
- **投机解码（speculative decoding）**：用一个小的「草稿模型（draft）」先猜几个 token，再用大的「目标模型（base）」一次性验证，命中就白赚几个 token，从而提升吞吐。EAGLE 用树、MTP 用链、DFlash 用块。

如果你对其中某项陌生，建议先回看 u5-l1。

还需要一个 C++ 小知识：**纯虚函数（pure virtual function）**。在基类里写成 `virtual 返回值 方法(参数) = 0;`，表示「我只定义接口，不提供实现，子类必须自己实现」。包含纯虚函数的类是**抽象类**，不能直接实例化，只能 new 一个具体子类。

---

## 3. 本讲源码地图

本讲围绕 `cpp/runtime/decoding/` 目录下的三个最小模块展开：

| 文件 | 作用 |
| --- | --- |
| [cpp/runtime/decoding/decodingStrategy.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h) | 定义抽象基类 `DecodingStrategy`、枚举 `DecodingStrategyKind`，以及策略所需的各种「资源包」结构体（执行器、采样缓冲、KV 缓存等）。 |
| [cpp/runtime/decoding/decoderRegistry.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.h) / [decoderRegistry.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp) | `DecoderRegistry`：持有「默认解码器」和「可选的投机解码器」两个指针，负责构造期分发与逐请求选择。 |
| [cpp/runtime/decoding/vanillaDecoder.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.h) / [vanillaDecoder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp) | `VanillaDecoder`：最朴素的自回归解码策略，每步只生成 1 个 token。是理解所有策略的基准线。 |

补充阅读（用于对比投机解码，非本讲核心）：

| 文件 | 作用 |
| --- | --- |
| cpp/runtime/decoding/eagleDecoder.cpp | EAGLE 树形投机解码，`decodeStep` 分 4 个阶段。 |
| cpp/runtime/decoding/mtpDecoder.cpp | MTP 链式投机解码，结构与 EAGLE 类似。 |
| cpp/runtime/decoding/dflashDecoder.cpp | DFlash 块状投机解码，`decodeStep` 分 3 个阶段。 |
| cpp/runtime/decoding/gemma4MTPDecoder.cpp | Gemma4-MTP，与目标模型共享 KV 的链式解码，`decodeStep` 分 5 个阶段。 |
| cpp/runtime/config/llmEngineConfig.h | `SpecDecodeMode` 枚举（kNONE/kEAGLE/kMTP/kDFlash/kGemma4MTP），驱动注册表的策略分发。 |

---

## 4. 核心概念与源码讲解

### 4.1 解码策略层的定位：把「解码算法」从「运行时主循环」里解耦

#### 4.1.1 概念说明

先想一个朴素的问题：运行时主循环每一步要做的事，在普通自回归和投机解码下是一样的吗？

- **普通自回归（vanilla）**：每步把「上一步生成的那个 token」喂回 base 模型，做一次前向，采样出 1 个新 token。主循环的「步数」等于「生成的 token 数」。
- **投机解码（speculative）**：每步先用 draft 模型猜一串，再用 base 模型一次性验证这串，**可能一次接受多个 token**。主循环的「步数」远小于「生成的 token 数」。

这两者的「步内行为」差别巨大。如果运行时主循环直接写死其中一种，那每加一种解码算法就得改主循环——这正是要避免的。

EdgeLLM 的做法是定义一个抽象基类 `DecodingStrategy`，主循环只调它的 `decodeStep(context)`：

```text
while (还有序列没结束):
    decodingStrategy.decodeStep(context)   # 步内细节全部藏在策略里
    decodePerSlot(...)                     # 算可发文本、判停、流式输出
    updateFinishStates()
    emitChunks(...)
    performBatchEvict(...)
```

这样主循环就成了「与解码算法无关」的固定骨架：采样参数检查、流式输出、批量驱逐这些通用逻辑都写在主循环里，而「每步到底怎么生成 token」交给策略。于是策略层解耦的是**两个方向**：

- 向上：运行时主循环（`handleRequest`）不必关心当前是 vanilla 还是 EAGLE。
- 向下：策略通过「资源包」结构体拿到执行器、KV 缓存、采样缓冲等基础设施，自己编排 draft/base 两套引擎。

#### 4.1.2 核心流程：运行时如何驱动一个策略

把策略层放进 u5-l1 介绍的整体流程里，关键调用点如下：

1. **构造期**（一次）：`LLMInferenceRuntime` 在构造时 `new` 出一个 `DecoderRegistry`，注册表内部决定好「用哪种策略」。
2. **请求期**（每个请求一次）：`handleRequest` 调 `mDecoderRegistry->select(request)`，拿到一个 `DecodingStrategy&` 引用。注意这一步是**逐请求**的——同一个注册表，不同请求可能选不同策略（例如某个请求显式要求关闭投机解码）。
3. **解码循环**（每个生成步一次）：循环里反复调 `decodingStrategy.decodeStep(context)`。

这三个调用点分别对应：

- 注册表构造：[llmInferenceRuntime.cpp:382-383](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L382-L383)
- 逐请求选择：[llmInferenceRuntime.cpp:554](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L554)
- 解码循环中的步调用：[llmInferenceRuntime.cpp:949](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L949)

下面三个小节分别拆开抽象接口、注册表、vanilla 实现。

---

### 4.2 DecodingStrategy：抽象基类与策略种类枚举

#### 4.2.1 概念说明

`DecodingStrategy` 是所有解码策略的抽象基类。它本身不持有任何状态（没有成员变量），只规定「一个解码策略必须能做什么」。具体的状态（执行器引用、采样缓冲引用等）被收进一个外部结构体 `DecodingRuntimeContext`，由运行时拥有，策略只持有它的引用——这一点很重要，它保证了策略是「无状态编排者」，真正昂贵的资源都在运行时统一管理。

策略的「种类」用枚举 `DecodingStrategyKind` 标识，与配置层的 `SpecDecodeMode` 一一对应：

```cpp
enum class DecodingStrategyKind : int32_t
{
    kVanilla,
    kEAGLE,
    kMTP,
    kDFlash,
    kGemma4MTP,
};
```

这段枚举在 [decodingStrategy.h:49-56](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L49-L56)。

#### 4.2.2 核心流程：策略拿到哪些资源

策略要干活，需要 base 引擎、KV 缓存、采样缓冲、分词器、logit bias 等一堆基础设施。这些不是逐个传参（参数会爆炸），而是打包成 `DecodingRuntimeContext`：

```cpp
struct DecodingRuntimeContext
{
    DeploymentConfig& deployment;       // 部署配置（含 specDecodeMode）
    int32_t maxRuntimeBatchSize;

    BaseEngineResources base;           // base 引擎：执行器、tensorMap、共享资源、KV 缓存、pipelineIO、CUDA graph 回调
    PreprocessResources preprocess;     // 预处理：step preparer、embedding、ids 输入、deepstack
    tokenizer::Tokenizer& tokenizer;    // 分词器
    LogitBias& logitBias;               // logit 偏置
    SamplingBuffers sampling;          // 采样缓冲
    LogprobsBuffers logprobs;          // logprobs 缓冲
};
```

定义见 [decodingStrategy.h:107-118](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L107-L118)。

需要特别留意 `BaseEngineResources`，它把 base 引擎的执行器、张量表、共享资源、混合缓存管理器、pipeline I/O、以及一个 **CUDA graph 捕获回调** `captureGraph` 打包（[decodingStrategy.h:86-94](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L86-L94)）。投机解码策略除了 base，还会自带一套 draft 执行器与 draft tensorMap（在具体子类如 `EagleDecoder` 内部，见 u7）。

#### 4.2.3 源码精读：抽象基类的全部纯虚方法

抽象基类本身在 [decodingStrategy.h:120-146](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L120-L146)。我们逐组看它要求子类实现的方法。

**身份标识组**（3 个，全部 `const noexcept`）：

```cpp
virtual DecodingStrategyKind kind() const noexcept = 0;   // 返回种类枚举
virtual char const* name() const noexcept = 0;            // 返回可读名字，如 "vanilla"/"eagle"
virtual bool isSpeculative() const noexcept = 0;          // 是否为投机解码
```

主循环里有 `if (enableSpecDecode && hasNonGreedySampling)` 这类判断（[llmInferenceRuntime.cpp:555-562](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L555-L562)），就靠 `isSpeculative()` 区分。

**核心动作组（最重要的两个）**：

```cpp
virtual bool decodeStep(DecodingInferenceContext& context) = 0;   // 执行一个解码步，返回是否成功
virtual bool captureCudaGraphs(cudaStream_t stream) = 0;          // 录制 CUDA graph
```

- `decodeStep` 是策略的「心脏」，本讲 4.4 会精读 vanilla 版。
- `captureCudaGraphs` 把 decode 阶段的 kernel 录制成 CUDA graph，省去每步的 CPU 启动开销。

`DecodingInferenceContext` 是「请求局部上下文」，持有 tokenIds、finishedStates、采样参数（temperature/topK/topP）、流式状态等（定义在 [decodingInferenceContext.h:77-154](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/decodingInferenceContext.h#L77-L154)）。它和 `DecodingRuntimeContext` 的分工是：前者随请求创建（tokenIds 这种是每请求不同的），后者跨请求复用（执行器、KV 缓存这种昂贵资源）。

**上下文显存管理组**：

```cpp
virtual int64_t getRequiredContextMemorySize() const noexcept = 0;  // 该策略需要多少 execution context 显存
virtual void setContextMemory(Tensor&) = 0;                         // 把一块显存交给策略使用
```

投机解码策略需要额外的 draft execution context 显存，所以这两个方法允许它申请更多。vanilla 则直接返回 0（见 4.4）。

**系统提示 KV 缓存组（4 个）**：

```cpp
virtual bool hasSystemPromptKVCache(SystemPromptCacheKey const&) const = 0;
virtual void restoreSystemPromptKVCache(SystemPromptCacheKey const&, int32_t, cudaStream_t) = 0;
virtual bool runSystemPromptPrefill(DecodingInferenceContext&) = 0;
virtual void saveSystemPromptKVCache(SystemPromptCacheKey const&, std::string const&,
    std::vector<tokenizer::Rank> const&, int32_t, cudaStream_t) = 0;
```

这组接口让策略能「预填充并保存系统提示的 KV 缓存」，下次相同系统提示直接恢复，省掉重复 prefill（详见 u9-l2）。注意头文件里有一段 TODO 注释，说明作者打算把它重构为通用的 `prefill` 入口（[decodingStrategy.h:135-137](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L135-L137)）——读源码时看到 TODO 是正常的，它告诉你这块接口还会演进。

**批量驱逐组（2 个）**：

```cpp
virtual void resetForNewSequences(Tensor&, cudaStream_t) = 0;
virtual void onBatchEvict(std::vector<int32_t> const&, int32_t, int32_t, Tensor&, cudaStream_t) = 0;
```

当一批序列里有些生成完了、要被「驱逐」出活跃批次时，KV 缓存需要回收或搬迁。投机解码策略因为多了一套 draft KV，必须额外处理。

**合起来**，抽象基类要求子类实现的纯虚方法共 **13 个**（外加一个非纯虚的虚析构 `~DecodingStrategy() noexcept = default;`）。

#### 4.2.4 代码实践：对照源码数纯虚方法

1. **实践目标**：亲手从 `decodingStrategy.h` 里数出策略必须实现的全部纯虚方法，并分类。
2. **操作步骤**：
   - 打开 [decodingStrategy.h:120-146](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L120-L146)。
   - 把所有以 `= 0;` 结尾的声明挑出来，按「身份标识 / 核心动作 / 上下文显存 / 系统提示 KV / 批量驱逐」五组归类填进一张表。
3. **需要观察的现象**：`~DecodingStrategy()` 这一行虽然带 `virtual`，但没有 `= 0`，所以它不是纯虚方法，而是有默认实现的虚析构。说明基类本身是抽象的（因为别的方法是纯虚），但析构是为了让 `unique_ptr<DecodingStrategy>` 能正确析构子类。
4. **预期结果**：纯虚方法合计 13 个；析构 1 个（非纯虚）。如果你数到别的数字，回到源文件核对每一行结尾。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DecodingStrategy` 自己不带成员变量，而是把所有资源塞进外部的 `DecodingRuntimeContext`？

> **参考答案**：因为策略对象希望是「无状态的编排者」，昂贵的资源（执行器、KV 缓存、采样缓冲）由运行时统一拥有、跨请求复用。策略只持有引用，构造/析构开销小，且多个策略可以共享同一套基础设施。这也让「逐请求切换策略」变得廉价——切换的只是引用，不重建引擎。

**练习 2**：`DecodingStrategyKind` 和配置层的 `SpecDecodeMode` 各有 5 个值且名字几乎一样，为什么要有两套枚举？

> **参考答案**：它们处于不同层次、职责不同。`SpecDecodeMode` 在**配置层**（`llmEngineConfig.h`），描述「这套 engine bundle 声明用哪种投机解码」，是从 config.json 的 `spec_decode_type` 解析来的（[llmEngineConfig.cpp:108-129](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/llmEngineConfig.cpp#L108-L129)）。`DecodingStrategyKind` 在**运行时层**（`decodingStrategy.h`），描述「当前这个解码器对象是什么种类」。前者是声明，后者是实例化后的自描述。保留两套枚举避免了运行时层反向依赖配置层的细节。

---

### 4.3 DecoderRegistry：策略选择与资源管理

#### 4.3.1 概念说明

`DecoderRegistry` 是策略层的「门面（facade）」。运行时不直接 `new VanillaDecoder` 或 `new EagleDecoder`，而是统一通过注册表。注册表内部只持有两个智能指针：

```cpp
std::unique_ptr<DecodingStrategy> mDefaultDecoder;      // 永远是 VanillaDecoder
std::unique_ptr<DecodingStrategy> mSpeculativeDecoder;  // 投机解码器，或 nullptr
```

定义在 [decoderRegistry.h:60-63](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.h#L60-L63)。

这个设计的关键直觉是：**vanilla 解码器永远被创建**。即使部署的是投机解码 engine，也总保留一个 vanilla 作为「逐请求回退」的后备。原因是用户可以在单个请求里用 `disableSpecDecode` 临时关掉投机解码——这时运行时不能临时 new 一个 vanilla（太贵），而是直接复用早已创建好的 `mDefaultDecoder`。

#### 4.3.2 核心流程：构造期分发 + 逐请求选择

注册表的两段式行为可以画成：

```text
【构造期，一次性】
  new VanillaDecoder → mDefaultDecoder        # 总是建
  if 有 draftingConfig:                        # 用户传了投机解码参数
      switch deployment.specDecodeMode():      # 看 engine bundle 声明哪种
        kMTP      → new MTPDecoder      → mSpeculativeDecoder
        kEAGLE    → new EagleDecoder    → mSpeculativeDecoder
        kDFlash   → new DFlashDecoder   → mSpeculativeDecoder
        kGemma4MTP→ new Gemma4MTPDecoder→ mSpeculativeDecoder
        kNONE     → 抛异常（有参数却没声明模式，配置矛盾）

【请求期，每个请求】
  select(request):
      if 没有 mSpeculativeDecoder 或 request.disableSpecDecode:
          return mDefaultDecoder               # 回退 vanilla
      return mSpeculativeDecoder               # 用投机解码
```

构造期分发对应 [decoderRegistry.cpp:34-62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L34-L62)。注意 `kNONE` 那一支会抛异常：传了 `draftingConfig` 却没声明任何投机解码模式，属于配置自相矛盾，宁可早失败也不静默跑错。

逐请求选择对应 [decoderRegistry.cpp:64-72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L64-L72)，核心就一句判断：

```cpp
if (!mSpeculativeDecoder || request.disableSpecDecode)
{
    return *mDefaultDecoder;
}
return *mSpeculativeDecoder;
```

这就是 u5-l1 提到的「`VanillaDecoder` 始终保留以支持逐请求回退」在代码里的落点。

#### 4.3.3 源码精读：资源管理与 CUDA graph 协调

除了 `select`，注册表还要替运行时协调「两种策略共有的资源管理」：

**系统提示 KV 缓存的预热策略**用 `cachePrimingStrategy()`：如果有投机解码器就用它预热（因为它有 draft KV 也要一起预热），否则用 vanilla（[decoderRegistry.cpp:74-77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L74-L77)）。

**上下文显存**取两者最大值，因为同一块显存要同时满足两种策略（[decoderRegistry.cpp:103-109](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L103-L109)）；`setContextMemory` 则对两个解码器都调用一次（[decoderRegistry.cpp:111-121](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L111-L121)）。

**CUDA graph 捕获**最值得细看（[decoderRegistry.cpp:79-101](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L79-L101)）：

```cpp
bool const skipDefaultCapture = mSpeculativeDecoder
    && mSpeculativeDecoder->kind() == DecodingStrategyKind::kDFlash;
// ...
bool const defaultCaptureStatus
    = (!skipDefaultCapture && mDefaultDecoder) ? mDefaultDecoder->captureCudaGraphs(stream) : true;
bool const speculativeCaptureStatus = mSpeculativeDecoder ? mSpeculativeDecoder->captureCudaGraphs(stream) : true;
```

这里有个细节：当部署的是 DFlash 投机解码时，**会跳过 vanilla 的 CUDA graph 捕获**。原因是 DFlash 的 verify 步骤本身就覆盖了 vanilla decode 路径，再单独录 vanilla 的图是浪费。这个 `skipDefaultCapture` 标志就体现了「注册表必须理解每种策略的特性」——纯抽象的统一接口不够用，注册表保留了对具体策略种类（`kind()`）的判断。

而整个 `captureCudaGraphs` 在运行时由 `captureDecodingCUDAGraph` 调起（[llmInferenceRuntime.cpp:1530-1534](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1530-L1534)）。注意它返回 bool：捕获失败不会致命，只是会打一条 warning，运行时退回「不录图也能跑、只是慢一点」（见 [decoderRegistry.cpp:95-99](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L95-L99)）。

#### 4.3.4 代码实践：追踪一次策略选择

1. **实践目标**：理解「同一个 runtime 上，不同请求可以选不同策略」。
2. **操作步骤**：
   - 读 [decoderRegistry.cpp:64-72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L64-L72) 的 `select`。
   - 再读调用方 [llmInferenceRuntime.cpp:554](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L554)，确认 `select` 的返回值直接绑定到本次请求的 `decodingStrategy` 引用。
   - 回想 u5-l2 学到的 `LLMGenerationRequest` 字段，确认 `disableSpecDecode` 是请求级别的开关。
3. **需要观察的现象**：`select` 是 `const noexcept` 的、且不修改任何成员——它纯粹是「读两个指针 + 读请求字段」做出选择，没有任何分配或锁。
4. **预期结果**：能说清楚「为何 vanilla 必须在构造期就建好，而不是临时按需 new」——因为 `select` 不能失败、不能抛异常（`noexcept`），所以它依赖的对象必须早就存在。

#### 4.3.5 小练习与答案

**练习 1**：假设部署了 EAGLE 投机解码 engine，但用户某个请求把 `disableSpecDecode` 设为 true。这个请求会走哪条路径？为什么不会报错？

> **参考答案**：走 `mDefaultDecoder`（vanilla）。因为注册表构造期**无条件**创建了 vanilla，所以 `select` 在检测到 `request.disableSpecDecode` 时，直接返回早已就绪的 vanilla 指针，无需临时构造，也不抛异常。这就是「vanilla 永远保留」设计的回报。

**练习 2**：`getRequiredContextMemorySize` 为什么取 `max` 而不是「两者之和」？

> **参考答案**：因为两种策略不会同时运行——同一个请求要么走 vanilla 要么走投机解码。它们复用**同一块** execution context 显存，所以取最大值就够了；求和会多算一份永远用不上的显存。

---

### 4.4 VanillaDecoder：自回归解码的最小实现

#### 4.4.1 概念说明

`VanillaDecoder` 是策略层的「基准实现」。它每步只做最朴素的事：把上一步的 token 喂回 base 模型，做一次 decode 前向，采样出 1 个新 token。理解了它，你就理解了「一个解码步」到底由哪些操作组成；而所有投机解码策略，都是在这个基准之上「多塞一个 draft、把验证也并进来」。

#### 4.4.2 核心流程：vanilla 的一个 decodeStep

vanilla 的 `decodeStep` 用伪代码描述（对应 [vanillaDecoder.cpp:50-177](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L50-L177)）：

```text
decodeStep(context):
    1. 取每个序列最后 1 个 token，拷到 device 的 idsInput            # [batch, 1]
    2. embedding lookup：token id → [batch, 1, hidden] 嵌入向量
    3. stepPreparer.prepare(kDecode, ...)                           # 摆好 KV cache 指针、位置编码
    4. executor.prepare(kDecodeProfile=1, decodeDims, tensorMap)    # 选 decode profile、绑输入形状
    5. executor.execute(stream)                                     # base 引擎前向
    6. cacheManager.commitSequenceLength(+1)                        # KV 长度 +1
    7. applyLogitBias(...)                                          # 对 logits 施加偏置
    8. 采样：greedy 或 topK/topP → 选出每序列 1 个 token
    9. 若开了词表裁剪：把裁剪后 id 映射回全词表 id
   10. enqueue logprobs 的 D2H
   11. D2H 把采样 id 拷回 host + cudaStreamSynchronize            # 唯一一次同步
   12. 把新 token push 进 context.tokenIds[i]，generateLength +1
   13. 收集 logprobs
   14. return true
```

一个关键细节是它用的是 **decode profile（索引 1）**：

```cpp
constexpr int32_t kDecodeProfile{1};
```

定义在 [vanillaDecoder.cpp:41](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L41)，对应 u4-l2 讲过的 prefill/decode 双 profile。vanilla 的每一步序列长度都是 1（单 token），所以它永远走为「单 token + 大 batch」优化过的 profile 1。

#### 4.4.3 源码精读：关键片段

**第 1、2 步——取上一个 token 并查表**（[vanillaDecoder.cpp:60-77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L60-L77)）：

```cpp
for (int32_t i = 0; i < activeBatchSize; ++i)
{
    hostPackedTokenIdsData[i] = context.tokenIds[i].back();   // 取每序列最后一个 token
}
// H2D ...
kernel::embeddingLookup(idsInput, embedding.table, ...,
    pipelineIO.inputsEmbeds, context.stream);                 // id → 嵌入向量
```

注意 `context.tokenIds[i].back()`——这是「自回归」的本质：每步只看上一步的输出。

**第 4、5、6 步——base 前向**（[vanillaDecoder.cpp:94-104](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L94-L104)）：

```cpp
auto const decodeDims = mRuntime.deployment.base.decodeDims(activeBatchSize);
bool decodingStatus = mRuntime.base.executor.prepare(kDecodeProfile, decodeDims, mRuntime.base.tensorMap, context.stream);
if (decodingStatus)
{
    decodingStatus = mRuntime.base.executor.execute(context.stream);
}
if (decodingStatus)
{
    mRuntime.base.cacheManager.commitSequenceLength(/*increment=*/1, context.stream);
}
```

这正是 u5-l3 讲的 `EngineExecutor` 的 prepare/execute 三件套（这里只用了 prepare+execute）。`commitSequenceLength(+1)` 把这一步算出的新 KV 行正式提交进缓存。

**第 8 步——采样**（[vanillaDecoder.cpp:113-126](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L113-L126)）：根据 `shouldUseNonGreedySampling(temperature, topK, topP)` 分两路——要么走 `topKtopPSamplingFromLogits`（非贪心），要么走 `selectAllTopK`（贪心，topK=1）。采样细节属于 u5-l7，这里只要知道「输出是每序列 1 个 token」。

**第 12 步——把 token 写回上下文**（[vanillaDecoder.cpp:165-169](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L165-L169)）：

```cpp
for (int32_t i = 0; i < activeBatchSize; ++i)
{
    context.tokenIds[i].push_back(hostSelectedTokenIdsData[i]);
    context.currentGenerateLengths[i] += 1;
}
```

**vanilla 把那些「它用不到」的纯虚方法怎么实现的？** 看 [vanillaDecoder.h:50-71](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.h#L50-L71)。它把系统提示 KV 缓存、批量驱逐、上下文显存这些接口几乎全实现成**空操作或返回常量**：

```cpp
int64_t getRequiredContextMemorySize() const noexcept override { return 0; }
void setContextMemory(Tensor&) override {}
bool hasSystemPromptKVCache(SystemPromptCacheKey const&) const override { return false; }
void restoreSystemPromptKVCache(...) override {}
bool runSystemPromptPrefill(DecodingInferenceContext&) override { return true; }   // 注意返回 true
void saveSystemPromptKVCache(...) override {}
void resetForNewSequences(Tensor&, cudaStream_t) override {}
void onBatchEvict(...) override {}
```

这里有个容易踩坑的点：`runSystemPromptPrefill` 返回 `true` 表示「成功」——vanilla 不做系统提示缓存预热，所以它直接「声称已经做完了」，让运行时跳过这块逻辑继续往下走。读接口时一定要同时看返回值的语义。

#### 4.4.4 代码实践：对比 vanilla 与投机解码的 decodeStep（本讲必做实践）

这是规格里要求的实践。目标：说清楚 vanilla 与投机解码在 `decodeStep` 上的本质差异。

1. **实践目标**：通过对四个投机解码器的 `decodeStep` 头部分析，归纳出「步内阶段数」和「单步可接受的 token 数」两个维度的差异。
2. **操作步骤**：
   - 读 vanilla 的 `decodeStep`：[vanillaDecoder.cpp:50-177](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L50-L177)。数它有几个子调用阶段（取 token → embedding → 前向 → 采样 → 回写）。
   - 读 EAGLE 的 `decodeStep`：[eagleDecoder.cpp:144-172](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L144-L172)。它调用了 `runDraftModelPrefill/runDraftModelAcceptToken` → `constructDraftProposal` → `runBaseModelVerification`。
   - 读 MTP 的 `decodeStep`：[mtpDecoder.cpp:135-163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L135-L163)。结构几乎和 EAGLE 一样。
   - 读 DFlash 的 `decodeStep`：[dflashDecoder.cpp:236-259](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L236-L259)。它是 `runDraftForward` → `prepareDFlashVerifyInputs` → `runBaseVerification`。
   - 读 Gemma4-MTP 的 `decodeStep`：[gemma4MTPDecoder.cpp:103-108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L103-L108)。它有 5 个阶段：`prepareSeed` → `runAssistantDraftChain` → `runBaseVerification` → `acceptAndCommit` → `updateNextSeed`。
3. **需要观察的现象**：vanilla 的 `decodeStep` 里**只出现 `mRuntime.base`**，从不出现 draft 执行器；而每个投机解码的 `decodeStep` 都有「draft 提议」和「base 验证」两个明确分离的阶段。
4. **预期结果**：能填出下表（本质差异）：

   | 维度 | vanilla | EAGLE/MTP | DFlash | Gemma4-MTP |
   | --- | --- | --- | --- | --- |
   | 用到的引擎 | 仅 base | base + draft | base + draft | base + draft（draft 与 base 共享 KV） |
   | 步内是否有「提议」阶段 | 否 | 是（树/链） | 是（整块，用 mask token 填充） | 是（链） |
   | 步内是否有「验证」阶段 | 否（直接采样即终） | 是 | 是 | 是 |
   | 单步最多接受 token 数 | 1 | draftingStep+1 | min(dflashBlockSize, verifySize) | draftingStep+1 |

   表中后三列的「单步最多接受 token 数」来自 [deploymentConfig.h:113-117](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/deploymentConfig.h#L113-L117) 的 `maxAcceptedTokensPerRound` 注释。

   **一句话总结本质差异**：vanilla 的 `decodeStep` 是「base 一次前向 → 采样 → 1 个 token 就是真的」；投机解码的 `decodeStep` 是「draft 先猜一批 → base 一次前向同时验证这批 → 命中的全部接受」，把「采样即终」推迟到了「验证后才终」。

   > 说明：本实践为「源码阅读型实践」，无需 GPU。若想观察运行时行为，待本地验证：用 `llm_inference` 分别对一个 base-only engine 和一个带 `--specBase/--specDraft` 的 engine 跑同一 prompt，对比日志里 `Selected ... decoding strategy.`（见 [decoderRegistry.cpp:60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L60)）以及每步生成的 token 数。

#### 4.4.5 小练习与答案

**练习 1**：vanilla 的 `decodeStep` 在哪里发生唯一一次 `cudaStreamSynchronize`？为什么需要它？

> **参考答案**：在采样 id 从 device 拷回 host 之后（[vanillaDecoder.cpp:145](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L145)）。因为下一步要把刚采样出的 token 写回 `context.tokenIds[i]` 并由 CPU 侧的主循环用来判停（EOS/长度/stop string），CPU 必须读到正确的 id，所以这一步必须等 GPU 算完。整步只同步这一次，其余全是异步 `cudaMemcpyAsync`。

**练习 2**：vanilla 把 `runSystemPromptPrefill` 实现成直接 `return true`，会不会让运行时误以为「系统提示缓存已经预热好了」从而导致错误？

> **参考答案**：不会，这是有意为之。vanilla 不支持系统提示 KV 缓存（它的 `hasSystemPromptKVCache` 恒返回 `false`）。返回 `true` 表示「这块预处理我不需要做、也视为完成」，让运行时顺畅地跳过预热继续 prefill。配合 `hasSystemPromptKVCache` 恒为 `false`，运行时不会去尝试「恢复」一个不存在的缓存。这是接口语义「成功」与「不需要」共享同一个 `true` 返回值的约定。

**练习 3**：投机解码时，运行时主循环为什么能把每步可能「接受多个 token」这件事藏在策略里，而主循环代码看起来和 vanilla 几乎一样？

> **参考答案**：因为「接受几个 token」是策略在 `decodeStep` 内部把多个 token push 进 `context.tokenIds[i]` 的，而主循环只看 `context` 里的序列状态（tokenIds 长度、finishedStates）来判停和流式输出。主循环对「这一步到底加了几个 token」是无感的——它只调一次 `decodeStep`、然后照常做 `decodePerSlot`/`updateFinishStates`/`emitChunks`。这正是策略层解耦的价值。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**画一张「请求的一生」时序图，标出解码策略层在每个环节的调用**。

场景：你部署了一个 EAGLE 投机解码 engine，然后先后处理两个请求 A 和 B，其中 B 在请求里把 `disableSpecDecode` 设为 true。

要求：

1. 从 `LLMInferenceRuntime` 构造开始，画出 `DecoderRegistry` 构造期如何同时 new 出 `VanillaDecoder` 和 `EagleDecoder`（依据 [decoderRegistry.cpp:34-62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L34-L62)）。
2. 对请求 A，画出 `handleRequest` 调 `select(A)` 返回 `EagleDecoder`，解码循环每步调 `EagleDecoder::decodeStep`（4 阶段）。
3. 对请求 B，画出 `select(B)` 因 `disableSpecDecode` 返回 `VanillaDecoder`，解码循环每步调 `VanillaDecoder::decodeStep`（单 token）。
4. 在图上标注：构造期是一次性的；`select` 是每请求一次；`decodeStep` 是每生成步一次；`captureCudaGraphs` 是构造后、首次推理前一次性调用。

完成后，用一句话验证你的图：「同一个 runtime、同一套 base/draft 引擎，A 走 EAGLE、B 走 vanilla，且两者共享同一份基础设施，只是 `decodeStep` 的实现不同。」这句话成立，就说明你真正理解了解码策略层的解耦设计。

---

## 6. 本讲小结

- 运行时主循环是「与解码算法无关」的固定骨架，每步只调 `decodingStrategy.decodeStep(context)`；解码算法的差异全部藏在 `DecodingStrategy` 子类里。
- `DecodingStrategy` 是无状态抽象基类，要求子类实现 13 个纯虚方法，分五组：身份标识、核心动作（`decodeStep`/`captureCudaGraphs`）、上下文显存、系统提示 KV 缓存、批量驱逐。所有昂贵资源收在外部的 `DecodingRuntimeContext` 里，由运行时统一拥有。
- `DecoderRegistry` 是策略层门面，持有「永远存在的 `mDefaultDecoder`（vanilla）」和「可选的 `mSpeculativeDecoder`」。构造期按 `SpecDecodeMode` 分发建投机解码器；逐请求 `select()` 据请求的 `disableSpecDecode` 决定走哪个。
- vanilla 总是被无条件创建，是为了支持「逐请求回退到普通解码」而无需临时 new 解码器。
- `VanillaDecoder::decodeStep` 的核心是「取上一步 token → embedding → base 前向（decode profile 1）→ commit KV → 采样 → 同步拷回 → push 1 个 token」，每步只产 1 个 token；它把用不到的接口（系统提示缓存、批量驱逐、上下文显存）实现成空操作或常量返回。
- vanilla 与投机解码的本质差异：vanilla 是「base 一次前向、采样即终、每步 1 token」；投机解码是「draft 先猜一批 → base 一次前向验证 → 命中的全部接受」，每步可接受多个 token，把「采样即终」推迟为「验证后才终」。

---

## 7. 下一步学习建议

- **横向扩展（继续 u5）**：要理解 vanilla `decodeStep` 里反复出现的 `executor.prepare/execute`、`tensorMap`、`commitSequenceLength` 的细节，去读 u5-l3（引擎执行器与张量注册表）；要理解采样那一段，去读 u5-l7（采样与分词）；要理解 `commitSequenceLength` 背后的缓存，去读 u5-l5（KV 缓存与混合缓存管理）。
- **纵向深入（u7 投机解码专题）**：本讲只对比了四种投机解码器 `decodeStep` 的「阶段骨架」。它们的草稿如何提议、base 如何验证、KV 是否共享等算法细节，是 u7-l1（投机解码策略）的主题，届时会精读 `eagleDecoder.cpp`/`mtpDecoder.cpp`/`dflashDecoder.cpp`/`gemma4MTPDecoder.cpp` 的子方法。
- **关联特性**：系统提示 KV 缓存那 4 个接口的实际用法在 u9-l2（系统提示 KV 缓存与流式）展开；批量驱逐 `onBatchEvict` 与 u5-l1 介绍的 `performBatchEvict` 呼应。
