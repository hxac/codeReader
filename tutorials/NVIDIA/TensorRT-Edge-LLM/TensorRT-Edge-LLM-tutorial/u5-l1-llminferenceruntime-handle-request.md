# u5-l1 LLMInferenceRuntime 与 handleRequest

## 1. 本讲目标

前面 u4 单元讲完了「如何把 ONNX 编成 TensorRT engine」。从本讲开始，我们进入三段式流水线的最后一段——**C++ 运行时**：拿到 engine 之后，怎么真正用它做推理。

本讲聚焦运行时的「总指挥」类 `LLMInferenceRuntime`。学完后你应该能够：

1. 说清楚 `LLMInferenceRuntime` 在整个运行时里扮演的角色，以及它内部「拥有」了哪些组件。
2. 区分 vanilla（普通自回归）与 speculative（投机解码）两种构造方式的差异，知道它们在 engine 文件、内存、解码策略上的不同。
3. 跟着 `handleRequest` 从「请求进来」到「响应出去」走完全流程，理解 prefill、解码循环、批量驱逐、CUDA graph 捕获各自的职责。
4. 写出一段最小的 C++ 调用代码：构造运行时、填一个请求、捕获 CUDA graph、调用 `handleRequest` 并打印输出。

本讲是 u5 单元其余各篇（请求/响应模型、引擎执行器、解码策略、KV 缓存、张量、采样）的公共地基。

## 2. 前置知识

在进入源码前，先用三段大白话把背景补齐。

**引擎（engine）是什么。** TensorRT engine 是针对某一块具体 GPU 编译出来的「专用推理程序」，由 u4 的构建器从 ONNX 生成（见 u4-l1）。运行时不重新编译，只负责「加载 engine → 喂数据 → 拿结果」。一个 engine 的输入输出名字、形状范围，都是构建时就固定下来的契约。

**prefill 与 decode 两个阶段。** 生成式大模型推理天然分两步：

- **prefill（预填充）**：一次性把整条 prompt（可能几十到几千个 token）喂进去，并行算出每一层的隐藏状态，并填充 KV 缓存。这一步是「算力密集」的。
- **decode（解码）**：之后每一步只喂「上一步刚生成的 1 个 token」，用已积累的 KV 缓存算出下一个 token，循环往复直到 EOS 或达上限。这一步是「访存密集」的。

因为两个阶段的形状差异极大（prefill 是长序列、decode 是单 token），构建器会为同一个 engine 准备**两套优化 profile**（见 u4-l2），运行时负责在两者之间切换。

**CUDA graph（图捕获）是什么。** 每次 launch 一个 kernel，CPU 都要付出「启动开销」。decode 阶段每步只算一个 token，kernel 又小又多，CPU 启动开销会变成瓶颈。CUDA graph 允许你先把一串 kernel「录制」下来，之后直接整体回放，把 CPU 开销降到几乎为零。代价是录制要求每次执行的 kernel 序列与形状基本固定——这正是 decode 阶段的特点。

> 关键术语：engine、prefill、decode、KV 缓存、optimization profile（min/opt/max 三元组）、CUDA graph、`handleRequest`、`DecodingStrategy`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `cpp/runtime/llmInferenceRuntime.h` | `LLMInferenceRuntime` 类声明：两个构造函数、`handleRequest`、`captureDecodingCUDAGraph`，以及它拥有的全部成员指针。 |
| `cpp/runtime/llmInferenceRuntime.cpp` | 全部实现：`initializeCommon`（构造主流程）、`handleRequest`（请求主循环）、`runBaseModelPrefill`、`performBatchEvict`、CUDA graph 捕获等。 |
| `docs/source/developer_guide/software-design/llm-inference-runtime.md` | 官方运行时设计文档：架构图、vanilla/EAGLE 流程图、C++ 用法示例。本讲源码实践以它为蓝本。 |
| `cpp/runtime/decoding/decodingStrategy.h` | `DecodingStrategy` 抽象基类与 `DecodingRuntimeContext`——`handleRequest` 通过它把「怎么解码」解耦出去。 |
| `cpp/runtime/decoding/decoderRegistry.cpp` | `DecoderRegistry`：按部署配置选出 vanilla/eagle/mtp/dflash/gemma4-mtp 策略。 |
| `cpp/runtime/llmRuntimeUtils.h` | `LLMGenerationRequest` / `LLMGenerationResponse` / `Message` 等请求与响应数据结构。 |
| `cpp/runtime/config/deploymentConfig.h` | `SpecDecodeDraftingConfig`——构造投机解码运行时时传入的草稿参数。 |
| `examples/llm/llm_inference.cpp` | 真实的命令行示例，展示了「构造 → 捕获 graph → 循环 handleRequest」的完整骨架，是本讲综合实践的参照。 |

---

## 4. 核心概念与源码讲解

### 4.1 LLMInferenceRuntime：统一的运行时类

#### 4.1.1 概念说明

`LLMInferenceRuntime` 是**所有 LLM 推理的统一入口**。无论你是跑一个最普通的 Qwen/Llama，还是跑带 EAGLE 投机解码的模型，还是跑带图像/音频的多模态模型，外部代码面对的都是同一个类、同一个 `handleRequest` 方法。

它的设计哲学是「**一个类，把所有零件都拥有起来**」：engine 执行器、KV 缓存、分词器、采样缓冲区、（可选的）视觉/音频/动作 runner、解码策略注册表……全都是它的成员，由它的构造函数一次性建好、析构时统一释放。这样做的好处是：调用方不用操心资源生命周期，只要「构造一次、反复调用 `handleRequest`」即可。

官方设计文档把这个关系画成了架构图，核心一句是：

> The **LLM Inference Runtime** (`LLMInferenceRuntime`) is the unified runtime for all LLM inference in TensorRT Edge-LLM. … When constructed without a drafting config, it operates as a pure vanilla decoding runtime with zero draft-model memory overhead.

见 [docs/source/developer_guide/software-design/llm-inference-runtime.md:L1-L5](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/llm-inference-runtime.md#L1-L5)（运行时定位说明）。

#### 4.1.2 核心流程

`LLMInferenceRuntime` 的生命周期可以拆成两个阶段：

1. **构造阶段（一次性）**——`initializeCommon` 按固定顺序把世界搭建好：
   - 加载共享词嵌入表（`embedding.safetensors`）；
   - 解析 base（以及可选的 draft）engine 配置，做跨 engine 一致性校验；
   - 构造 base `EngineExecutor`；
   - 分配 `SharedResources`（KV 缓存 / RoPE / LoRA）与 `PipelineIO`（每流水线 I/O 张量）；
   - 建 base `TensorMap`（把引擎输入输出名字绑定到运行时张量）；
   - 分配运行时局部张量（采样工作区、logits、host pinned 缓冲等）；
   - 加载分词器；
   - 通过 `buildDecodingRuntimeContext` + `DecoderRegistry` 选出解码策略；
   - 可选地加载多模态 runner（视觉/音频/动作）；
   - 为所有 engine 分配**一块共享的执行上下文内存**。

2. **服务阶段（反复调用）**——外部反复调用 `handleRequest`，每次完成一个批次的「prompt → 生成文本」。

```
[外部]
  │  (1) 构造 LLMInferenceRuntime  ──► initializeCommon: 建 executor / KV / tokenizer / 策略 / 多模态 / 共享显存
  │
  │  (2) captureDecodingCUDAGraph  ──► 录制 decode 阶段 CUDA graph（可选，失败可降级）
  │
  └─► (3) handleRequest(req, resp)  ──► prefill → 解码循环 → 组装 response
        ↑_______________ 反复调用 _______________|
```

#### 4.1.3 源码精读

类声明在 [cpp/runtime/llmInferenceRuntime.h:L53-L61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L53-L61)，注释点明了它的统一性与「两种模式、可选草稿」的设计意图。

「拥有所有零件」体现在它的私有成员上。下面是一组最具代表性的成员（完整列表见头文件私有区）：

[cpp/runtime/llmInferenceRuntime.h:L227-L249](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L227-L249) 这段声明了运行时的核心资产：`mBaseExecutor`（base 引擎执行器）、`mSharedResources`（KV 缓存/RoPE/LoRA）、`mPipelineIO`（每流水线张量）、`mDecoderRegistry`（解码策略注册表）、`mStepPreparer`/`mEmbeddingPre`（预处理）、`mTokenizer`（分词器），以及三个可选的多模态 runner `mVisionRunner`/`mAudioRunner`/`mActionRunner`。注意它们几乎都是 `std::unique_ptr`——意味着这些组件的生命周期与 runtime 完全绑定，runtime 析构时它们随之销毁。

构造阶段最值得读的是「共享执行上下文内存」这一步。TensorRT 的每个 engine 执行需要一个 device 侧的「上下文内存」缓冲。EdgeLLM 让 base、draft、视觉、音频、动作这些 engine **串行执行**，因此它们可以共用同一块缓冲，只按最大需求分配一次：

[cpp/runtime/llmInferenceRuntime.cpp:L443-L461](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L443-L461) 先取出每个 engine 各自需要的上下文内存大小，取最大值，分配一块 GPU `UINT8` 缓冲 `mSharedExecContextMemory`，然后逐一 `setContextMemory` 挂给每个 engine。这是一个典型的「以串行执行换内存节省」的取舍。

构造的收尾是选出解码策略：

[cpp/runtime/llmInferenceRuntime.cpp:L381-L383](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L381-L383) 先 `buildDecodingRuntimeContext()` 把 base 引擎、张量表、共享资源、采样缓冲等打包成一个 `DecodingRuntimeContext` 引用束，再用它构造 `DecoderRegistry`。策略的选择逻辑全部下沉到注册表，runtime 本身不写 `if (eagle) …`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「runtime 拥有哪些组件」的直观印象。

**操作步骤**：

1. 打开 [cpp/runtime/llmInferenceRuntime.h:L212-L298](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L212-L298)（私有成员区）。
2. 用下表把成员归类填好（第一行已示例）：

| 成员 | 类别 | 是否可选（unique_ptr 可能为空） |
|------|------|------|
| `mBaseExecutor` | 引擎执行 | 否（必建） |
| `mVisionRunner` | ？ | 是 |
| `mAudioRunner` | ？ | ？ |
| `mActionRunner` | ？ | ？ |
| `mTokenizer` | ？ | ？ |
| `mDecoderRegistry` | ？ | ？ |
| `mSharedExecContextMemory` | ？ | ？ |

**需要观察的现象**：你会发现几乎凡是「某种模型能力」的成员都是 `unique_ptr` 且可能为空（视觉/音频/动作），而「任何 LLM 都必需」的成员（base 执行器、分词器、采样缓冲）则是裸对象或必建的 unique_ptr。

**预期结果**：能说出「一个 vanilla 文本模型的 runtime 不会分配任何视觉/音频/动作 runner」——这正是「zero draft-model / zero multimodal memory overhead」的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mSharedExecContextMemory` 只分配一块、却要分给这么多 engine？答：因为这些 engine 是**串行执行**的（base 跑完才轮到 draft 或视觉），同一时刻只有一个 engine 在用上下文内存，所以按最大需求开一块即可，避免每个 engine 各开一份造成浪费。

**练习 2**：如果将来要让 base 和 draft engine **并行**执行，这块共享缓冲还能直接复用吗？答：不能。并行执行意味着两者会同时占用上下文内存，共用一块会互相覆盖；需要为每个并发执行的 engine 单独分配。

---

### 4.2 两种构造方式：vanilla vs 投机解码

#### 4.2.1 概念说明

`LLMInferenceRuntime` 有**两个构造函数**，区别只在于「要不要带 draft（草稿）模型」：

- **vanilla 构造（4 参数）**：不带 `SpecDecodeDraftingConfig`。运行时只加载一个 base engine，走标准自回归解码——每步生成 1 个 token。没有 draft 模型的任何内存开销。
- **speculative 构造（5 参数）**：多传一个 `SpecDecodeDraftingConfig`。运行时会额外加载一个 draft engine，启用投机解码（EAGLE/MTP/DFlash/Gemma4-MTP），每步可以「猜 + 验」多个 token，吞吐显著提升。

两者的**对外接口完全一致**（都是同一个类、同一个 `handleRequest`），差异全部藏在构造与内部策略里。这也是「统一运行时」设计的精髓：调用方换个构造函数，就从普通解码切到了投机解码。

#### 4.2.2 核心流程

两个构造函数都只是把参数转发给同一个私有方法 `initializeCommon`，用「`draftingConfig` 是否为 `std::nullopt`」作为分支开关：

```
vanilla 构造 ─► initializeCommon(..., draftingConfig = nullopt, ...)
                        │
                        ├─ 加载 llm.engine + config.json
                        ├─ SharedResources::createForLLM(...)        # 只建 base 的 KV 缓存
                        └─ DecoderRegistry 里只有 VanillaDecoder

spec 构造   ─► initializeCommon(..., draftingConfig = {topK, step, verify}, ...)
                        │
                        ├─ 加载 spec_base.engine + base_config.json
                        ├─ 加载 spec_draft.engine + draft_config.json   # 多一个 draft engine
                        ├─ SharedResources::createForSpecDecode(...)    # base + draft 两套 KV
                        └─ DecoderRegistry 里 VanillaDecoder + 投机解码器
```

| 维度 | vanilla 构造 | speculative 构造 |
|------|--------------|------------------|
| engine 文件 | `llm.engine` | `spec_base.engine` + `spec_draft.engine` |
| 配置文件 | `config.json` | `base_config.json` + `draft_config.json` |
| KV 缓存 | 单套（base） | 双套（base + draft） |
| 解码策略 | 只有 `VanillaDecoder` | `VanillaDecoder` + 一个投机解码器 |
| 每步产出 | 1 个 token | 可能多个 token（猜中则一并接受） |

#### 4.2.3 源码精读

两个构造函数的声明在头文件里紧挨着，参数列表只差一个 `SpecDecodeDraftingConfig`：

[cpp/runtime/llmInferenceRuntime.h:L73-L86](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L73-L86) 上面是 5 参数（spec）构造，下面是 4 参数（vanilla）构造。

实现里两者都直接转发，vanilla 把 `draftingConfig` 填成 `std::nullopt`：

[cpp/runtime/llmInferenceRuntime.cpp:L133-L144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L133-L144) 这就是「两种构造、同一套初始化」的代码体现。

`SpecDecodeDraftingConfig` 本身只是 4 个用户可调旋钮（草稿 topK、草稿步数、验证规模、DFlash 块大小），见 [cpp/runtime/config/deploymentConfig.h:L38-L49](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/deploymentConfig.h#L38-L49)。真正的「选哪种投机解码」由 base/draft engine 的配置元数据（`specDecodeMode()`）决定，不由用户在这个结构体里指定。

`initializeCommon` 里那处分流很清晰：

[cpp/runtime/llmInferenceRuntime.cpp:L208-L220](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L208-L220) 用 `hasDraft` 在 `createForSpecDecode` / `createForLLM` 与 `PipelineIO::createForSpecDecode` / `createForLLM` 之间二选一——也就是说 KV 缓存与流水线张量的「形状/数量」是按是否有 draft 来定的。

而具体选哪种投机解码器，在注册表构造时由 `specDecodeMode()` 派发：

[cpp/runtime/decoding/decoderRegistry.cpp:L34-L62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L34-L62) 构造函数始终先建一个 `VanillaDecoder` 作默认；只有当 `draftingConfig` 有值时，才按 `specDecodeMode()` `switch` 出 MTP/EAGLE/DFlash/Gemma4MTP 之一作为 `mSpeculativeDecoder`。注意：**即使开了投机解码，VanillaDecoder 也始终存在**——后面会看到它在运行时仍有用武之地。

#### 4.2.4 代码实践（命令行型）

**实践目标**：用同一个示例程序 `llm_inference`，通过开关在 vanilla 与投机解码之间切换。

**操作步骤**：

1. 阅读 [examples/llm/llm_inference.cpp:L564-L595](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L564-L595)。这里 `main` 用 `args.specDecodeArgs.enabled` 在两个构造函数之间分支：带 `--specDecode` 时构造 5 参数 runtime，否则构造 4 参数 runtime。
2. 对照两份 engine 目录：vanilla 用的是 `engineDir/llm.engine`；投机解码用的是 `engineDir/spec_base.engine` 与 `spec_draft.engine`（这正对应 u4-l3 里 `--specBase/--specDraft` 必须共用同一 `engineDir` 的产物）。

**需要观察的现象**：切换 `--specDecode` 开关，运行时加载的 engine 文件、占用的显存（多一套 draft KV 缓存）、以及生成的吞吐都会不同；而 `handleRequest` 的调用代码**一行都不用改**。

**预期结果**（无 GPU 环境为「待本地验证」）：在同一 prompt、同一 `maxGenerateLength` 下，投机解码的每秒 token 数应明显高于 vanilla（接受率正常时），但显存占用更高。

#### 4.2.5 小练习与答案

**练习 1**：为什么开了投机解码之后，`VanillaDecoder` 还要保留？答：因为请求可以**逐条**关闭投机解码（`LLMGenerationRequest::disableSpecDecode`，见 4.3.3），此时该请求必须回退到 vanilla 解码；此外系统提示 KV 缓存的预热等公共路径也复用 vanilla 的 prefill。

**练习 2**：`SpecDecodeDraftingConfig` 里没有「EAGLE/MTP/DFlash」这种字段，运行时怎么知道用哪种？答：由 base/draft engine 配置里的元数据（`DeploymentConfig::specDecodeMode()`）决定，见 `DecoderRegistry` 构造里的 `switch`；用户只调「草稿规模」这类通用旋钮。

---

### 4.3 handleRequest 主入口与 CUDA graph 捕获

#### 4.3.1 概念说明

`handleRequest` 是运行时的**心脏**：吃进一个 `LLMGenerationRequest`（一批 prompt + 采样参数），吐出一个 `LLMGenerationResponse`（每条 prompt 的生成 token 与文本）。它的签名极简：

```cpp
bool handleRequest(LLMGenerationRequest const& request,
                   LLMGenerationResponse& response,
                   cudaStream_t stream,
                   bool outputThinkerEmbeddings = false);
```

它内部把「整条生成」组织成清晰的三个阶段：

1. **准备阶段**：校验请求、选解码策略、套 chat 模板、分词、（多模态时）跑视觉/音频编码、可选地预热/复用系统提示 KV 缓存、为 prefill 摆好张量。
2. **prefill 阶段**：跑一遍 base engine 的 prefill profile，得到第一个生成 token。
3. **解码循环**：反复调用所选 `DecodingStrategy::decodeStep`，每步「采样 → 判停 → 流式输出 → 批量驱逐」，直到整批全部完成；最后把结果写回 response。

`cudaStream_t stream` 由外部传入——运行时**不创建自己的流**，而是在调用方给的流上排队所有 CUDA 操作。这给了调用方完全的并发控制权。

CUDA graph 捕获则是**独立的一步**：构造完 runtime 后，调用 `captureDecodingCUDAGraph(stream)` 把 decode 阶段的 kernel 序列录制成图。它**可选且可降级**——录制失败时运行时照常工作，只是 decode 每步走普通的 kernel launch（慢一些）。

#### 4.3.2 核心流程

`handleRequest` 的主循环可以概括成下面这段伪代码（省略多模态/流式/动作等分支）：

```
handleRequest(request, response, stream):
    清空 per-request 状态与 response
    activeBatchSize = request.requests.size()
    if not validateRequestConfig(request): return false

    strategy = mDecoderRegistry->select(request)      # 选 vanilla 或投机解码
    enableSpecDecode = strategy.isSpeculative()
    若投机解码但请求带了非 greedy 采样 → 警告并规范化为 greedy

    对每条请求 applyChatTemplate → 分词 → 填 context.rawBatchedInputIds
    （多模态时改走 multiModalRuntimePreprocess）

    可选: genAndSaveSystemPromptKVCache(...)           # 预热/保存系统提示缓存
    setUpForPrefillExecution(context, strategy)        # 摆张量、复位 KV、复用前缀缓存

    runBaseModelPrefill(context)                       # 阶段二：prefill，得首个 token

    # 阶段三：解码循环
    while not 全部完成:
        applyCancellationToFinishStates(context)       # 先观察取消
        strategy.decodeStep(context)                   # 走 vanilla 或投机解码
        decodePerSlot(context, tokenizer)              # 增量解码文本 + stop string 匹配
        updateFinishStates()                           # EOS / 长度 / stop 判停
        emitChunks(context, tokenizer)                 # 流式输出
        emitTokenCallbacks(context)                    # per-token 回调
        performBatchEvict(context, strategy)           # 把已完成槽位踢出 batch

    把 completedBatches 写回 response.outputIds / outputTexts / finishReasons
    return true
```

几个关键点：

- **prefill 永远走 base engine**，投机解码的 draft engine 只在解码循环里（由策略）使用。
- **批量驱逐（batch eviction）**让同一个 batch 里不同长度的序列可以「先完成先退场」，腾出的槽位逻辑上被压缩，避免空转——这是动态批处理的基础。
- **判停有三种原因**：EOS（`kEndId`）、达最大长度（`kLength`）、命中 stop string（`kStopWords`），外加取消/错误，写进 `response.finishReasons`。

CUDA graph 捕获的主流程更短：

```
captureDecodingCUDAGraph(stream):
    try: return mDecoderRegistry->captureCudaGraphs(stream)
    catch: 记录警告、清 CUDA 错误、return false   # 降级，不抛异常
```

真正干活的是 `DecoderRegistry::captureCudaGraphs`，它会调用 vanilla 解码器与（若有）投机解码器各自的 `captureCudaGraphs`，分别录制它们各自的 decode kernel 序列。

#### 4.3.3 源码精读

`handleRequest` 的完整实现很长（约 600 行），但骨架非常贴合上面的伪代码。先看入口的「清状态 + 校验 + 选策略」：

[cpp/runtime/llmInferenceRuntime.cpp:L541-L555](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L541-L555) 取出 `activeBatchSize`，先 `validateRequestConfig` 拦截非法请求（空 batch、超 max batch、多模态输入但无对应 runner 等，见 [cpp/runtime/llmInferenceRuntime.cpp:L1121-L1204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1121-L1204)），再用 `mDecoderRegistry->select(request)` 选策略。

策略选择本身非常轻量——这正是策略层解耦的价值：

[cpp/runtime/decoding/decoderRegistry.cpp:L64-L72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L64-L72) 只要「没有投机解码器」或「请求显式 `disableSpecDecode`」，就返回默认的 `VanillaDecoder`；否则返回投机解码器。于是「这条请求要不要投机解码」是**逐请求**决定的，runtime 主循环完全不用关心差异。

「选了投机解码却带了非 greedy 采样」会被规范化，并打一条警告：

[cpp/runtime/llmInferenceRuntime.cpp:L559-L563](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L559-L563) 原因是当前投机解码器只支持 greedy 兼容的采样；随后在 [cpp/runtime/llmInferenceRuntime.cpp:L611-L613](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L611-L613) 把 `temperature/topP/topK` 重置为 greedy 等价值（1.0/1.0/0）。

prefill 与解码循环的衔接处是理解整条流程的关键：

[cpp/runtime/llmInferenceRuntime.cpp:L727-L732](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L727-L732) 调 `runBaseModelPrefill(context)` 跑 prefill profile 拿到首个 token；失败则直接返回 false。

解码循环本身结构非常干净，注释里写明了每步的流水线「cancel → decode → finalize → emit」：

[cpp/runtime/llmInferenceRuntime.cpp:L943-L975](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L943-L975) 这是整个运行时的热路径。注意第 949 行的 `decodingStrategy.decodeStep(context)`——vanilla 与投机解码的差异全封装在这个虚函数里（接口定义见 [cpp/runtime/decoding/decodingStrategy.h:L129-L130](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decodingStrategy.h#L129-L130)）；第 969 行 `performBatchEvict` 在每步后压缩已完成槽位。

prefill 内部如何切换到 prefill profile 并执行，体现了 u4-l2 讲的「双 profile、单 context」：

[cpp/runtime/llmInferenceRuntime.cpp:L1415-L1417](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1415-L1417) 用常量 `kPrefillProfile`（=0）调 `mBaseExecutor->prepare(...)`，再 `execute(stream)`。这两个 profile 索引自 [cpp/runtime/llmInferenceRuntime.cpp:L54-L58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L54-L58)（profile 0 = prefill，profile 1 = decode），与构建器烘焙进 engine 的布局一一对应。`EngineExecutor::prepare` 的签名见 [cpp/runtime/exec/engineExecutor.h:L85-L96](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.h#L85-L96)——它接收 `profileIndex`，在同一个执行上下文上切换 profile（细节留到 u5-l3）。

投机解码的接受率统计也很直观，写成了一个简单的比值：

[cpp/runtime/llmInferenceRuntime.cpp:L1033-L1036](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1033-L1036) 定义为「被接受的验证 token 数 / 迭代轮数」，即

\[
\text{acceptanceRate} = \frac{\text{verificationTokens}}{\text{actualIterations}}
\]

这个值越接近 1，说明 draft 猜得越准、投机解码收益越大。

最后是 CUDA graph 捕获。runtime 对外暴露的入口极其简短，且**保证不抛异常**：

[cpp/runtime/llmInferenceRuntime.cpp:L1530-L1548](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1530-L1548) 全部委托给 `mDecoderRegistry->captureCudaGraphs(stream)`；`catch (...)` 兜底，保证即便录制炸了，也只是返回 false、记一条警告，不影响后续推理。

`DecoderRegistry::captureCudaGraphs` 负责把「默认解码器」和「投机解码器」各自的图都录一遍（DFlash 例外，会跳过默认图）：

[cpp/runtime/decoding/decoderRegistry.cpp:L79-L101](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L79-L101)。base 侧的录制还会按 LoRA adapter 逐个 fanout（默认一套 + 每个 adapter 一套）：

[cpp/runtime/llmInferenceRuntime.cpp:L1497-L1528](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1497-L1528) 对每个 LoRA 名字切换权重、`refreshTensorMap`，再 `prepare(kDecodeProfile, …)` + `captureGraph(stream)`，结果取逻辑与——任何一个失败只是把总结果拉成 false，其余 adapter 照录（优雅降级）。

#### 4.3.4 代码实践（编码型）

**实践目标**：参照官方文档用法示例（[docs/source/developer_guide/software-design/llm-inference-runtime.md:L316-L350](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/llm-inference-runtime.md#L316-L350)），写出一段「构造 vanilla runtime → 填请求 → 捕获 graph → handleRequest → 打印」的最小 C++ 骨架，并解释为何用 non-blocking CUDA stream。

**操作步骤**：把下面的代码（**示例代码**，未在本仓库编译验证，链接头与库需自行配置）当作蓝本，对照 [examples/llm/llm_inference.cpp:L561-L600](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L561-L600) 与 [examples/llm/llm_inference.cpp:L655-L668](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L655-L668) 阅读真实示例。

```cpp
// 示例代码：最小 vanilla 推理骨架
#include "runtime/llmInferenceRuntime.h"
#include "runtime/llmRuntimeUtils.h"   // LLMGenerationRequest / Response / Message
#include "common/checkMacros.h"        // CUDA_CHECK
#include <iostream>
#include <unordered_map>

int main()
{
    // 1) 创建 non-blocking CUDA stream
    cudaStream_t stream;
    CUDA_CHECK(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));

    // 2) 构造 vanilla runtime（四参数：engineDir, 多模态目录(空), LoRA 映射(空), stream）
    std::unordered_map<std::string, std::string> loraWeightsMap;       // 空 = 不用 LoRA
    std::string engineDir = "/path/to/engine_dir";                      // 含 llm.engine + config.json
    rt::LLMInferenceRuntime runtime(engineDir, /*multimodalEngineDir=*/"",
                                    loraWeightsMap, stream);

    // 3) 录制 decode 阶段 CUDA graph（失败可降级，不致命）
    if (!runtime.captureDecodingCUDAGraph(stream))
    {
        std::cerr << "CUDA graph 捕获失败，回退到普通 kernel launch\n";
    }

    // 4) 填一个 LLMGenerationRequest
    rt::LLMGenerationRequest request;
    request.requests.resize(1);
    rt::Message userMsg;
    userMsg.role = "user";
    userMsg.contents.push_back({"text", "What is the capital of France?"});
    request.requests[0].messages.push_back(std::move(userMsg));
    request.maxGenerateLength = 64;
    request.temperature = 1.0f;
    request.topK = 1;          // greedy，便于复现
    request.topP = 1.0f;

    // 5) 调 handleRequest 并打印
    rt::LLMGenerationResponse response;
    if (runtime.handleRequest(request, response, stream))
    {
        std::cout << "Generated: " << response.outputTexts[0] << '\n';
    }

    CUDA_CHECK(cudaStreamDestroy(stream));
    return 0;
}
```

**需要观察的现象 / 预期结果**：

- 若 engine 已用 u4-l3 的 `llm_build` 正确产出，且 `engineDir` 指向它，程序应打印一段关于 France 首都的回答（**实际文本待本地验证**）。
- 若把 `request.topK` 从 1 改成 50、`temperature` 改成 0.8，多次运行结果会变化（随机采样）。
- 若故意把 `engineDir` 指错，构造阶段（`initializeCommon`）就会抛 `std::runtime_error`——因为 [cpp/runtime/llmInferenceRuntime.cpp:L182-L188](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L182-L188) 在加载 base executor 失败时主动 throw。

**解释为何用 non-blocking stream**：CUDA 默认创建的是 blocking stream，它会在某些操作（如默认流上的同步原语）上引发**隐式的全设备同步**。TensorRT 内部为了并行（比如不同子图之间）会创建自己的辅助流（aux streams）；如果运行时用 blocking stream，这些辅助流的调度会被迫与默认流互相同步，拖慢整体流水。官方文档在每段用法示例里都特意注明了这一点：

> Initialize a non-blocking CUDA stream (avoids implicit sync with TensorRT's aux streams)

见 [docs/source/developer_guide/software-design/llm-inference-runtime.md:L320-L322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/llm-inference-runtime.md#L320-L322)。用 `cudaStreamNonBlocking` 标志创建的流不与默认流联动，让 TensorRT 的辅助流与运行时流能真正交错执行。

#### 4.3.5 小练习与答案

**练习 1**：`handleRequest` 里 prefill 之后、进入解码循环之前，runtime 用哪个表达式判断「整批是否已经全部完成」？答：靠 `checkAllFinished()` lambda——当 `context.activeBatchSize == 0` 或所有 `finishedStates[i]` 非零时返回 true，见 [cpp/runtime/llmInferenceRuntime.cpp:L753-L767](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L753-L767)。

**练习 2**：如果 `captureDecodingCUDAGraph` 返回 false，`handleRequest` 还能正常工作吗？答：能。CUDA graph 只是「把 decode 的 kernel 序列录下来整体回放」的加速手段；录制失败时，`decodeStep` 会回退到普通的逐 kernel launch，逻辑等价，只是每步的 CPU 启动开销更高、略慢。`handleRequest` 的正确性不依赖 graph。

**练习 3**：为什么 `handleRequest` 把流 `stream` 当参数传入，而不是 runtime 自己持有一个流？答：为了让调用方掌控并发——例如想在一个 GPU 上交错跑多个 runtime、或把推理流与数据拷贝流解耦时，调用方可以自己决定流的创建、优先级与同步时机；runtime 只是「在指定流上排队 work」。

---

## 5. 综合实践

把本讲的三块知识串起来：**用 `llm_inference` 示例走一遍「构造 → 捕获 graph → handleRequest → 看响应」，并用本讲的源码知识解释每一步对应到 runtime 内部的哪个阶段。**

1. **准备产物**：参照 u1-l5 / u4-l3，先 `export` + `llm_build` 出一个最小 LLM（如小尺寸 Qwen/Llama）的 `engineDir`，内含 `llm.engine`、`config.json`、`embedding.safetensors`、分词器文件。
2. **写一个最小输入 JSON**（类 OpenAI 格式，采样参数写在 JSON 而非命令行，这是 u1-l5 强调的约定），包含一条 `"role":"user"` 的文本问题。
3. **运行**（**待本地验证**）：

   ```bash
   ./llm_inference --engineDir=/path/to/engine_dir \
                   --inputFile=request.json \
                   --outputFile=out.json \
                   --warmup=1 --dumpOutput
   ```

4. **对照源码解释现象**，在笔记里填这张表：

   | 你观察到的外部行为 | 对应 runtime 内部阶段 | 关键源码位置 |
   |----|----|----|
   | 进程启动后加载 engine、分词器 | `initializeCommon` 构造阶段 | `llmInferenceRuntime.cpp:L146-L481` |
   | `--warmup=1` 跑了一次但不算分 | `handleRequest`（warmup 分支） | `examples/llm/llm_inference.cpp:L657-L668` |
   | 首个 token 与后续 token 都被打印 | prefill 采样 + 解码循环 `decodeStep` | `llmInferenceRuntime.cpp:L727`、`L949` |
   | 输出 JSON 里有 `finish_reason` | `updateFinishStates` + response 组装 | `llmInferenceRuntime.cpp:L852-L920`、`L1017-L1077` |

5. **进阶**：把 `--specDecode --specDraftTopK=10 --specDraftStep=6 --specVerifySize=60` 加上（engine 需是 `spec_base`+`spec_draft`），观察 `--dumpProfile` 输出里的 acceptance rate，并用本节的公式 \(\text{acceptanceRate} = \text{verificationTokens}/\text{actualIterations}\) 解释它。

> 若本机无 GPU，第 3 步无法真正运行——此时重点完成第 4 步的「行为→源码」映射表（源码阅读型实践），并把第 3 步的命令与参数逐项解释清楚即可。

---

## 6. 本讲小结

- `LLMInferenceRuntime` 是所有 LLM 推理的**统一入口**：它用 `unique_ptr` 把 engine 执行器、KV 缓存、分词器、采样缓冲、多模态 runner、解码策略注册表等全部「拥有」起来，构造一次、反复服务。
- 它有**两个构造函数**：4 参数（vanilla，加载 `llm.engine`，单套 KV）与 5 参数（投机解码，加载 `spec_base`+`spec_draft`，双套 KV）。两者都转发给同一个 `initializeCommon`，用「`draftingConfig` 是否为 nullopt」作分支开关。
- 具体用哪种投机解码器由 engine 配置的 `specDecodeMode()` 决定（在 `DecoderRegistry` 构造里 `switch`），用户只调草稿规模旋钮；VanillaDecoder 始终保留以支持逐请求回退。
- `handleRequest` 是三阶段主循环：**准备（校验/分词/系统提示缓存/摆张量）→ prefill（base engine 的 profile 0）→ 解码循环（`strategy.decodeStep` + 判停 + 流式 + 批量驱逐）**，最后把 `completedBatches` 写回 response。
- 策略选择是**逐请求**的：`DecoderRegistry::select` 看 `disableSpecDecode` 决定走 vanilla 还是投机解码，runtime 主循环因此与具体解码算法解耦。
- CUDA graph 捕获是**可选且可降级**的独立步骤（`captureDecodingCUDAGraph`），失败只降速不影响正确性；流必须由调用方传入，且**必须用 non-blocking stream** 以避免与 TensorRT 辅助流的隐式同步。

---

## 7. 下一步学习建议

本讲建立的是「运行时总指挥」的全局视图，但很多细节都故意封装起来了。建议按下面的顺序继续深入 u5 单元：

1. **先读 u5-l2（请求/响应数据模型与流式）**：本讲里反复出现的 `LLMGenerationRequest`/`LLMGenerationResponse`/`Message` 的字段逐一展开，理解多轮对话、`imageBuffers`、`loraWeightsName`、`finishReasons`、流式回调到底怎么用。
2. **再读 u5-l3（引擎执行器与张量注册表）**：本讲里 `mBaseExecutor->prepare(profile, …) + execute()` 这一行背后，`EngineExecutor` 如何用单个执行上下文在 prefill/decode profile 间切换、`TensorMap` 如何把引擎 I/O 名字绑定到运行时张量。
3. **接着读 u5-l4（解码策略与解码器注册表）**：本讲里 `decodingStrategy.decodeStep(context)` 这个虚函数，vanilla 与 EAGLE/MTP/DFlash 各自的实现差异。
4. **然后读 u5-l5（KV 缓存与混合缓存管理）**：本讲里 `setUpForPrefillExecution` 的「复用前缀缓存」与 `performBatchEvict` 的「压缩 KV」背后的缓存布局。
5. 若关注多模态或投机解码，可分别跳到 u6（多模态）与 u7（投机解码策略）。

一个很好的自检方式：回到本讲的 `handleRequest` 伪代码，确认你能为其中**每一行**指出它对应 u5 后续哪一篇讲义的深入内容——能做到这一点，说明全局地图已经建立。
