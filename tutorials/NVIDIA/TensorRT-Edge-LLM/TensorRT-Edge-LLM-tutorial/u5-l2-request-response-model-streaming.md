# 请求/响应数据模型与流式

> 承接 [u5-l1](u5-l1-llminferenceruntime-handle-request.md)。上一篇我们认识了运行时的「总指挥」`LLMInferenceRuntime` 和它的 `handleRequest()` 主流程，但当时刻意回避了一个问题：**请求长什么样？响应又长什么样？生成过程中如何把 token「边吐边发」给上层？** 本篇就来补齐这块「数据契约」。

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `llmRuntimeUtils.h` 中定义的请求结构 `LLMGenerationRequest`（含多轮 `Message`、多模态 `imageBuffers`/`audioBuffers`、采样参数、`loraWeightsName` 等），并能徒手构造一个「文本 + 图片」的多模态请求。
- 看懂响应结构 `LLMGenerationResponse`（`outputIds`/`outputTexts`/`logprobs`/`finishReasons`），理解每个字段的形状和含义。
- 区分两条「增量输出」通道：轻量的逐 token 回调 `TokenCallback`，以及完整的流式管道 `StreamChannel`（MPSC 有界队列 + `StreamChunk` 分块 + `FinishReason` 终止原因）。
- 理解流式输出在解码主循环里的执行点，以及 `StreamChannelFinalizer` 如何用 RAII 保证消费者「永不死等」。

## 2. 前置知识

- **值类型 vs 引用类型（C++）**：本讲的请求/响应都是 `struct`（值类型，整体拷贝/移动），而流式通道是 `std::shared_ptr<StreamChannel>`（引用语义，运行时和消费者共享同一对象）。理解这一点才能看懂为什么通道用智能指针传递。
- **生产者-消费者队列（MPSC）**：流式管道的本质是一个「多写一读」的有界队列——运行时是**生产者**（往里 push 文本块），上层服务是**唯一消费者**（从里 pop 文本块）。队列用 `std::mutex` + `std::condition_variable` 保护。
- **EOS / stop string**：模型自己采样到「结束符」（end-of-sequence token）算正常结束；用户也可以指定「停止字符串」，一旦输出里出现该子串就提前截断。两者对应不同的 `FinishReason`。
- **top-k / top-p / temperature 采样**：上一讲已铺垫，本讲只关心这些参数**放在请求的哪个字段**，采样算法本身在 [u5-l7](u5-l7-sampling-and-tokenization.md) 详讲。
- **RAII**：C++ 的「资源获取即初始化」。对象析构时自动释放资源/执行收尾。`StreamChannelFinalizer` 依赖它来保证异常路径下流通道也被正确终结。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpp/runtime/llmRuntimeUtils.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h) | **请求/响应结构的唯一权威定义**：`Message`、`LLMGenerationRequest`、`LLMGenerationResponse`、`LogprobEntry`、`TokenCallback`。还含 RoPE/嵌入/批量驱逐等工具（本讲不展开）。 |
| [cpp/runtime/streaming.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h) | **流式输出的全部类型与接口**：`FinishReason` 枚举、`StreamChunk`、`StreamChannel`（通道类）、`SlotStreamState`（每槽流式状态）、`StreamChannelFinalizer`（RAII 终结器），以及 `decodePerSlot`/`emitChunks` 等自由函数。 |
| [cpp/runtime/streaming.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp) | 流式逻辑的实现：通道的 push/pop/finish、停止串匹配 `applyStopStringMatch`、每槽解码 `decodePerSlot`、分块推送 `emitChunks`、终结器析构。 |
| [cpp/runtime/llmRuntimeUtils.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.cpp) | 与请求/响应相关的工具实现，本讲用到 `clampMaxGenerateLengthForKVCapacity`（请求的最大生成长度被 KV 容量夹紧）。 |
| [cpp/runtime/llmInferenceRuntime.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp) | `handleRequest` 主体——消费请求、装配响应、在解码循环里调用流式函数。本讲引用其中「校验/装配/解码循环/收尾」几段。 |
| [examples/multimodal/action_inference.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/action_inference.cpp) | 最清晰的「从 JSON 构造多模态请求」示例：解析 messages、`loadImageFromFile` 填 `imageBuffers`。本讲代码实践以此为基础。 |

> 术语：本讲把「一次 `handleRequest` 调用」称作一次**请求**（request），它内部可含一个**批次**（batch）的多条序列（`requests` 数组）；批次里每条序列称作一个**槽**（slot）。

---

## 4. 核心概念与源码讲解

### 4.1 请求结构：从 Message 到 LLMGenerationRequest

#### 4.1.1 概念说明

调用方要模型干活，得先填一张「请求单」。EdgeLLM 的请求单有三层嵌套，由内到外是：

1. **`Message`**：一条对话消息。模仿 OpenAI 的 chat 格式，由 `role`（`system`/`user`/`assistant`）和一个 `contents` 数组组成。`contents` 里每个元素带一个 `type`（`text`/`image`/`trajectory`…），这样一条消息就能混合「文字 + 图片」等多模态内容。
2. **`Request`**（`LLMGenerationRequest::Request`）：批次里的**一条序列**。它装着这轮对话的 `messages`，以及这条序列专属的多模态数据（`imageBuffers`/`audioBuffers`）、`stopStrings`、`logitBias` 等。
3. **`LLMGenerationRequest`**：**整张请求单**。装着这个批次的全部 `Request`，加上**批次级**的采样参数（`temperature`/`topK`/`topP`/`maxGenerateLength`）、`loraWeightsName`、各种行为开关，以及流式通道 `streamChannels`。

为什么要分三层？因为 EdgeLLM 是**动态批量**（continuous batching）的：一次 `handleRequest` 可以同时处理多条序列，它们共享同一套采样参数，但每条序列自己的对话历史、图片、停止串各不相同。把「批次公共参数」和「单序列私有数据」分开，既方便批量推理，又保留了 per-slot 的灵活性。

#### 4.1.2 核心流程：一份请求单是如何被组装和消费的

```text
调用方（example / Python API / 服务端）
   │  1. 填 Message{role, contents[{type,content}]}
   │  2. 多模态：type=="image" 时 loadImageFromFile() → imageBuffers[]
   │  3. 组 Request{messages, imageBuffers, stopStrings, ...}
   │  4. 组 LLMGenerationRequest{requests[], temperature, topK, ..., loraWeightsName}
   ▼
LLMInferenceRuntime::handleRequest(request, response, stream)
   │  - validateRequestConfig / validateStreamingSubmission  (校验)
   │  - applyChatTemplate → formattedRequests                (套对话模板)
   │  - 分词 → inputIds，摆张量
   │  - prefill → 解码循环（流式在此 push 分块）→ 收尾
   ▼
response: LLMGenerationResponse{outputIds, outputTexts, logprobs, finishReasons}
```

关键点：**多模态内容是「双轨」的**。文字走 `contents`，最终被分词成 `inputIds`；图片/音频走 `imageBuffers`/`audioBuffers`，里面是已解码的像素/特征数据。运行时靠 token 序列里的「图片占位 token」把两者对齐——这点在 4.1.4 的实践中会具体看到。

#### 4.1.3 源码精读

**① 最内层 `Message`**（[llmRuntimeUtils.h:44-54](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L44-L54)）：

```cpp
struct Message {
    struct MessageContent {
        std::string type;    // text / image / trajectory
        std::string content; // 文本内容；image 时这里是文件路径，像素数据另存 imageBuffers
    };
    std::string role;                     // system / user / assistant
    std::vector<MessageContent> contents; // 这条消息的全部内容块（保持顺序）
};
```

注意注释里的设计：`content` 字段对于 `image` 类型存的是**文件路径**，真正的像素数据被剥离到 `Request::imageBuffers` 里。这样 `Message` 本身保持纯文本可序列化，而二进制数据单独管理。

**② 中间层 `Request`**（[llmRuntimeUtils.h:108-123](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L108-L123)）：

```cpp
struct Request {
    std::vector<Message> messages;                              // 结构化对话（必填）
    std::vector<rt::imageUtils::ImageData> imageBuffers;        // 多模态：图片
    std::vector<rt::audioUtils::AudioData> audioBuffers;        // 多模态：音频（Qwen3-Omni）
    std::optional<std::vector<PastTrajectoryPoint>> pastTrajectory; // Alpamayo 历史轨迹
    std::vector<std::string> stopStrings;                       // 停止串
    std::unordered_map<int32_t, float> logitBias;               // 按 token id 的 logit 偏置
    mutable FormattedRequest formatted;                         // 套模板后的文本（运行时回填）
};
```

**③ 最外层 `LLMGenerationRequest`**（[llmRuntimeUtils.h:94-167](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L94-L167)），核心字段摘录：

```cpp
struct LLMGenerationRequest {
    struct FormattedRequest { std::string formattedSystemPrompt, formattedCompleteRequest; };
    struct Request { /* 见上 */ };

    std::vector<Request> requests;          // 批次：多条序列
    std::vector<FormattedRequest> formattedRequests; // 套模板后（运行时回填）

    float    temperature;          // 采样温度
    float    topP;                 // 核采样
    int64_t  topK;                 // top-k
    int64_t  maxGenerateLength;    // 最大生成长度
    std::string loraWeightsName{}; // LoRA 权重名（空=不挂 LoRA）

    bool saveSystemPromptKVCache{false}; // 是否保存系统提示 KV（见 u9-l2）
    bool applyChatTemplate{true};        // 是否套对话模板
    bool addGenerationPrompt{true};      // 是否追加 assistant 头
    bool enableThinking{false};          // 思考模式
    bool disableSpecDecode{false};       // 本请求强制关闭投机解码

    int32_t numLogprobs{0};              // 返回每个 token 的 top-K 对数概率（0=关）

    std::vector<std::shared_ptr<StreamChannel>> streamChannels; // 每槽流式通道（空=全局关流式）

    bool generateAudio{false};           // Omni Talker 语音生成
    int32_t acceptHiddenLayer{0};        // Talker 隐层捕获
    std::optional<TokenCallback> onTokenGenerated; // 轻量逐 token 回调
};
```

**几个关键设计**：

- **批次级采样参数**：`temperature`/`topK`/`topP`/`maxGenerateLength` 是整批共享的，`handleRequest` 会把它们转发到 `DecodingInferenceContext`（[llmInferenceRuntime.cpp:611-613](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L611-L613)）；若本请求开了投机解码，会被强行规范化为 greedy 并打告警。
- **`maxGenerateLength` 会被夹紧**：实际允许生成的长度不能超过「KV 缓存容量 − 已用 prefill 长度 − 预留」。这一步在 [llmRuntimeUtils.cpp:569-582](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.cpp#L569-L582) 的 `clampMaxGenerateLengthForKVCapacity` 里完成，`handleRequest` 调用它（[llmInferenceRuntime.cpp:713-714](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L713-L714)）。公式即：

  \[
  \text{maxGen} = \min\!\bigl(\text{requested},\ \max(0,\ \text{kvCapacity} - \text{prefillLen} - \text{reserve})\bigr)
  \]

  取全批各槽里**最小**的余量，保证共享生成上限不会顶破任何一条序列的 KV 预算。
- **两条增量输出通道并存**：`onTokenGenerated`（轻量回调，见 4.3）与 `streamChannels`（完整流式，见 4.3）。前者只回调 token id，后者推送已解码文本块。
- **`loraWeightsName` 是运行时 LoRA 切换的钥匙**：同一个 runtime 上挂了多套 adapter，请求里填哪个名字就用哪套（详见 [u9-l1](u9-l1-lora-support.md)）。

#### 4.1.4 代码实践：徒手构造一个「文本 + 图片」多模态请求

**实践目标**：把 `LLMGenerationRequest` 的关键字段列清楚，并构造一个文本 + 单张图片的请求，说清 `contents` 顺序与 `imageBuffers` 的对应关系。

**操作步骤**：

1. 打开 [llmRuntimeUtils.h:94-167](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L94-L167)，把 `LLMGenerationRequest` 的字段抄一遍并分类（批次级 / 单序列级 / 开关 / 流式）。
2. 参考真实解析代码 [examples/multimodal/action_inference.cpp:398-519](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/action_inference.cpp#L398-L519)，理解 `type=="image"` 时如何 `loadImageFromFile` 并 `push_back` 进 `imageBuffers`（[action_inference.cpp:475-478](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/action_inference.cpp#L475-L478)）。
3. 用下面这段**示例代码**（非项目原有，仅为说明结构）徒手拼一个请求：

```cpp
// 示例代码：构造一个「文本 + 一张图片」的多模态请求
rt::LLMGenerationRequest req;
req.temperature = 0.7f;
req.topP = 0.9f;
req.topK = 0;                 // 0 表示不限制
req.maxGenerateLength = 256;
req.loraWeightsName = "";     // 不挂 LoRA

rt::LLMGenerationRequest::Request slot;

// 1) system 消息（纯文本）
rt::Message sys;
sys.role = "system";
sys.contents.push_back({/*type*/"text", /*content*/"你是一个图像助手。"});

// 2) user 消息：先文字后图片，顺序就是模型看到的顺序
rt::Message usr;
usr.role = "user";
usr.contents.push_back({"text",  "请描述这张图："});
usr.contents.push_back({"image", "path/to/cat.jpg"});   // content 存路径

slot.messages = {sys, usr};

// 3) 关键对应：每个 image 内容块 → 一张 ImageData
auto img = rt::imageUtils::loadImageFromFile("path/to/cat.jpg");
slot.imageBuffers.push_back(std::move(img));   // imageBuffers[0] 对应 contents 里的第 1 个 image 块

req.requests.push_back(std::move(slot));
```

**需要观察的现象 / 对应关系**：

- `contents` 里 `image` 块出现的**顺序**，与 `imageBuffers` 里的元素**一一对应、按下标对齐**。运行时在 token 序列里看到图片占位 token 时，就按出现次序从 `imageBuffers` 取对应张量。所以**两者顺序必须一致**，多张图片时尤其要注意。
- 文本 `contents` 不进 `imageBuffers`；只有 `type=="image"` 的块才会触发一次 `loadImageFromFile` 并追加到 `imageBuffers`。
- `loadImageFromFile` 返回的 `ImageData` 若 `buffer == nullptr`（加载失败）会被**跳过**（[action_inference.cpp:476-478](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/action_inference.cpp#L476-L478)）——此时 `imageBuffers` 与 `contents` 会错位，是常见踩坑点。

**预期结果**：你能口头复述「`contents` 中第 k 个 image 块 ↔ `imageBuffers[k-1]`」这条对齐规则，并解释为什么 `content` 字段存路径而像素数据另放。实际运行（需要带视觉编码器的 engine）效果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`temperature`/`topK`/`topP`/`maxGenerateLength` 放在 `LLMGenerationRequest` 还是 `Request` 里？为什么？
> **答**：放在外层 `LLMGenerationRequest`（[llmRuntimeUtils.h:128-131](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L128-L131)），即**批次级**。因为它们影响采样内核的配置，整批共享一套更利于批量推理；而每条序列的对话内容、图片才是 per-slot 私有，放在内层 `Request`。

**练习 2**：如果一个请求里写了「图片 A、文字、图片 B」，`imageBuffers` 应该有几个元素？下标如何对应？
> **答**：2 个元素。`imageBuffers[0]` ↔ 图片 A，`imageBuffers[1]` ↔ 图片 B，按 `contents` 中 image 块的出现次序排列，与中间穿插的文字无关。

---

### 4.2 响应结构：LLMGenerationResponse 与 FinishReason

#### 4.2.1 概念说明

请求处理完，`handleRequest` 把结果写进 `LLMGenerationResponse`。它是**纯出参**（调用方传入引用，运行时填充）。它承载四类信息：

- **生成 token**：`outputIds`（整数 id）与 `outputTexts`（已解码字符串）。
- **对数概率**（可选）：`logprobs`，当请求里 `numLogprobs > 0` 才填，给出每个生成位置上概率最高的若干 token 及其 `log(softmax)` 值。
- **多模态产出**：`outputTrajectories`（动作模型轨迹）、`outputAudios`（Omni 语音）。
- **为什么停**：`finishReasons`，每个槽一个 `FinishReason`，告诉调用方这次是正常结束、被长度截断、命中停止串、被取消，还是出错。

`FinishReason` 是一个 `uint8_t` 底层的枚举，**同时被流式管道复用**（每个 `StreamChunk` 终止块也带它），所以它定义在 `streaming.h` 而非 `llmRuntimeUtils.h`（后者只用前向声明引用，见 [llmRuntimeUtils.h:63-66](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L63-L66)）。

#### 4.2.2 核心流程：响应是如何被装配出来的

非流式槽（没挂 `StreamChannel`）的输出在 `handleRequest` **收尾阶段**一次性装配：

```text
解码循环结束 → 对每个已完成批次(completedBatches)：
   response.outputIds[origIdx]   = batchResult.tokenIds 的生成部分
   response.outputTexts[origIdx] = tokenizer.decode(outputIds, skipSpecial=true)
   response.finishReasons[origIdx] = batchResult.terminalReason
   response.logprobs[origIdx]    = batchResult.logprobs
   (若有 stopStrings) 用 applyStopStringMatch(isFinal=true) 裁剪文本
```

关键：**停止串裁剪是「单一真相来源」**。无论流式还是非流式，都走同一个 `applyStopStringMatch`（[streaming.cpp:217-274](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L217-L274)）。收尾时如果一次性解码又发现新的停止串，会把 `finishReason` 升级为 `kStopWords`（[llmInferenceRuntime.cpp:1067-1075](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1067-L1075)）——这是因为流式的增量解码与一次性 `decode` 在 BPE 切分边界上可能略有差异。

#### 4.2.3 源码精读

**① `LLMGenerationResponse`**（[llmRuntimeUtils.h:171-185](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L171-L185)）：

```cpp
struct LLMGenerationResponse {
    std::vector<std::vector<int32_t>> outputIds;        // [batch] → 生成的 token id 列表
    std::vector<std::string> outputTexts;               // [batch] → 解码后的文本
    // logprobs[batch][step] = [LogprobEntry, ...]，按概率降序；仅 numLogprobs>0 时填
    std::vector<std::vector<std::vector<LogprobEntry>>> logprobs;
    std::vector<std::vector<FutureTrajectoryPoint>> outputTrajectories; // 动作模型轨迹
    std::vector<rt::audioUtils::AudioData> outputAudios;               // Omni 语音
    std::vector<FinishReason> finishReasons;                           // 每槽终止原因
};
```

**② 单条对数概率条目 `LogprobEntry`**（[llmRuntimeUtils.h:85-90](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L85-L90)）：

```cpp
struct LogprobEntry {
    int32_t tokenId;   // token id
    float logprob;     // 自然对数概率（<=0）
    std::string piece; // 原始 token 字节，装配时才填（解码热路径只写 id/logprob）
};
```

其中 `logprob` 即 \(\log(\mathrm{softmax}(\text{logits}))\)，恒非正。注意注释强调 `piece` **不在解码热路径上填**，而是在装配输出时才补——这是性能优化：热路径只搬运 id 和浮点数，字符串化推迟到必要时刻。

**③ `FinishReason` 六种取值**（[streaming.h:52-68](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L52-L68)）：

| 枚举值 | 含义 |
| --- | --- |
| `kNotFinished` (0) | 还在生成（仅出现在非终止块上） |
| `kEndId` (1) | 采样到 EOS，正常结束 |
| `kLength` (2) | 触顶 `maxGenerateLength`（或 KV 夹紧后的上限），输出可能截断半句 |
| `kCancelled` (3) | 消费者调了 `channel->cancel()`，运行时在迭代边界观察到 |
| `kError` (4) | 运行时异常（OOM、引擎错误、handleRequest 抛异常），少见 |
| `kStopWords` (5) | 命中请求的停止串 |

人类可读名字由 [finishReasonName()](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L203-L215) 提供（`"end-of-sequence"`/`"max-length"`/`"stop-words"` 等），示例 [llm_inference.cpp:999-1001](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L999-L1001) 正是用它把 `finishReasons` 写进输出 JSON。

**④ 装配到响应的实际代码**（[llmInferenceRuntime.cpp:1047-1051](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1047-L1051)）：

```cpp
response.outputIds[originalIdx]   = std::vector<int32_t>(...);     // 只取生成部分
response.outputTexts[originalIdx] = mTokenizer->decode(..., true);  // skipSpecial=true
response.finishReasons[originalIdx] = batchResult.terminalReason;
response.logprobs[originalIdx]      = batchResult.logprobs;
```

注意 `outputIds` 用 `originalIdx` 而非当前批次下标——因为动态批量会**驱逐已完成序列**，`completedBatches` 记住了原始下标，保证响应顺序与请求顺序对齐（[llmInferenceRuntime.cpp:1019-1023](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1019-L1023) 先按 `completedBatches.size()` resize，再用 `originalIdx` 定位）。

#### 4.2.4 代码实践：读懂响应装配与 finishReason 升级

**实践目标**：理解响应四件套的填充点，以及「停止串二次校验」为何会改写 `finishReason`。

**操作步骤**：

1. 读 [llmInferenceRuntime.cpp:1017-1077](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1017-L1077)，列出 `response` 的五个字段分别在哪些行被赋值。
2. 定位 [streaming.cpp:217-274](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L217-L274) 的 `applyStopStringMatch`，读懂它的「保留末尾 `maxStopLen-1` 字节跨迭代匹配」策略。
3. 解释为什么收尾阶段（[llmInferenceRuntime.cpp:1067-1075](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1067-L1075)）要再做一次 `isFinal=true` 的匹配。

**需要观察的现象**：

- `outputIds` 被刻意保留**完整 token 流**（注释 [llmInferenceRuntime.cpp:1056](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1056)），只有 `outputTexts` 被裁掉停止串——id 和文本的长度可能不一致。
- 收尾二次匹配存在的原因：增量解码 `emitDelta` 按 token 边界吐字节，而一次性 `Tokenizer::decode` 重新切分 BPE，可能让一个被跨 token 拆开的停止串只在一次性解码后才浮现，此时需把 reason 从 `kEndId`/`kLength` 升级为 `kStopWords`。

**预期结果**：你能说出响应装配「先 resize、再按 originalIdx 填、最后停止串裁剪」的三步顺序。运行验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`logprobs` 的三层嵌套各代表什么？什么时候为空？
> **答**：`logprobs[batch][step][k]` 分别是「批次第几条序列」「第几个生成步」「该步 top-K 中的第 k 个候选」。仅当请求 `numLogprobs > 0` 时才填充，否则整体为空（[llmRuntimeUtils.h:175-177](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L175-L177)）。

**练习 2**：一个序列生成了 100 个 token 后碰到 EOS，`finishReasons` 对应位置应是什么？若它在第 80 个 token 时恰好输出了用户设定的停止串呢？
> **答**：前者是 `kEndId`。后者取决于匹配时机：若流式/增量路径已识别停止串，则为 `kStopWords`；若增量路径漏判、收尾一次性解码才识别，也会被升级为 `kStopWords`（[llmInferenceRuntime.cpp:1069-1074](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1069-L1074)）。

---

### 4.3 流式输出：StreamChannel 回调管道

#### 4.3.1 概念说明

「流式」指**生成过程中就把已产出的文本增量推给上层**，而不是等整段生成完才一次性返回。这对降低首字延迟（TTFT 体感）至关重要——用户想看到字一个一个蹦出来。

EdgeLLM 提供**两条**增量通道，按重量级从轻到重：

1. **`TokenCallback`（轻量）**：一个 `std::optional<std::function<void(TokenCallbackInfo const&)>>`。解码每步采样后，运行时回调一次，只给 `tokenId`/`batchIdx`/`generationStep`/`isFinished`（[llmRuntimeUtils.h:68-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L68-L79)）。没有文本解码、没有停止串处理、没有线程同步——适合只想拿 token id 做自定义处理的场景。`nullopt` 时零开销。
2. **`StreamChannel`（完整）**：一个 `shared_ptr` 持有的 MPSC 管道对象。运行时把**已解码、UTF-8 合法、停止串已裁剪**的文本块 `StreamChunk` 推进去，消费者在另一端阻塞或轮询取出。提供取消、节流、跳过特殊 token 等完整能力。

为什么需要 `StreamChannel` 这么重？因为它要解决一组真实问题：

- **跨迭代的不完整 UTF-8**：一个汉字可能被 BPE 拆成多个 token，单步的 token 解码出来可能是半个字符的非法 UTF-8字节。管道用 `pendingBytes` 跨迭代缓存，保证推出去的永远合法。
- **跨迭代的停止串**：停止串可能正好被拆在两次迭代的输出之间。管道保留末尾若干字节缓冲，跨迭代匹配。
- **并发安全**：消费者可能和运行时不在同一线程（如服务端把 token 转发给 HTTP 客户端），需要 mutex + 条件变量。
- **异常兜底**：运行时一旦抛异常，必须保证消费者不会永远阻塞在 `pop` 上——靠 RAII 终结器兜底。

#### 4.3.2 核心流程：流式在解码循环里的执行点

`handleRequest` 的解码主循环（[llmInferenceRuntime.cpp:943-975](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L943-L975)）每轮迭代执行一条「每槽流水线」：

```text
while (!checkAllFinished()):
    applyCancellationToFinishStates(context)   // 观察消费者取消
    decodingStrategy.decodeStep(context)        // 真正采样（vanilla 或投机解码）
    decodePerSlot(context, tokenizer)           // ① 解码新 token 成字节 + 停止串匹配
    updateThinkingDone()
    updateFinishStates()                        //   定稿 finishedStates 与 terminalReason
    emitChunks(context, tokenizer)              // ② 把 pendingEmitText 打包成 StreamChunk 并 push
    emitTokenCallbacks(context)                 //   触发轻量 TokenCallback
    performBatchEvict(...)                      //   驱逐已完成序列
```

注意职责被刻意拆成两个阶段（[streaming.h:103-116](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L103-L116)）：`decodePerSlot` 只算「这次能安全发什么文本」放进 `pendingEmitText`，**不碰** `finishedStates`；`emitChunks` 才真正 push 分块、并在终态时 `finish(reason)`。终止判定（`updateFinishStates`）夹在两者之间——这样「为什么停」永远是先定稿、再随终止块发出，不会出现「块已发但原因未定」的竞态。

通道与槽的绑定发生在 prefill 之后、解码之前（[llmInferenceRuntime.cpp:695-711](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L695-L711)）：`attachStreamChannel` 记录原始批次下标，并把 `sentTokenCount` 种子设为 prompt 长度——**确保流式只发「生成的」token，不发 prompt**。紧接着构造 `StreamChannelFinalizer streamFinalizer(context, tokenizer)`，它的析构（函数返回/异常时）保证所有未终结的通道都被 `finish(kError)`。

#### 4.3.3 源码精读

**① 请求侧的通道集合**（[llmRuntimeUtils.h:150-154](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L150-L154)）：

```cpp
//! 每槽流式通道。size 0 = 全局关闭流式。
//! 非空时 size 必须等于 requests.size()，单个元素可为 null 以按槽退出。
std::vector<std::shared_ptr<StreamChannel>> streamChannels;
```

**② 提交前校验 `validateStreamingSubmission`**（[streaming.cpp:171-201](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L171-L201)）：检查「通道数 == 请求数」「通道未 finished」「通道未被别的在飞请求占用」。`handleRequest` 一进来就调它（[llmInferenceRuntime.cpp:549](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L549)）。这意味着**一个 `StreamChannel` 一次性、绑一个请求**，不能复用、不能并发挂两个请求。

**③ 单个增量块 `StreamChunk`**（[streaming.h:83-92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L83-L92)）：

```cpp
struct StreamChunk {
    std::vector<int32_t> tokenIds;                   // 自上次块以来的增量 token（投机解码可能 >1）
    std::string text;                                // 增量文本，恒为合法 UTF-8
    bool finished{false};                            // 是否本通道终止块
    FinishReason reason{FinishReason::kNotFinished}; // 终止原因（仅 finished 时有意义）
    std::vector<std::vector<LogprobEntry>> logprobs; // 每 token 的 top-K（numLogprobs>0 时）
};
```

**④ 通道类 `StreamChannel`——消费者 API 与生产者 API 分离**（[streaming.h:151-235](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L151-L235)）。这是本讲最重要的一段：

- **消费者可见**（public）：阻塞式 `consume(handler)`（整段吃完）、`tryPop()`（非阻塞取一个）、`waitPop(timeout)`（带超时取一个）、`isFinished()`/`getReason()`/`isCancelled()`、`cancel()`、`setStreamInterval(n)`（节流：攒够 n 个 token 才发一块）、`setSkipSpecialTokens(bool)`。
- **仅运行时可见**（private，靠 friend 函数访问）：`push(chunk)`、`finish(reason)`、`setOriginalBatchIdx(idx)`。

这种「friend 隔离」保证消费者**拿不到锁、跳不过块**，只能按顺序消费；只有几个被声明为友元的自由函数（`attachStreamChannel`/`decodePerSlot`/`emitChunks` 等）能生产。通道私有状态用 `std::mutex mMutex` + `std::condition_variable mCv` + `std::deque<StreamChunk> mPending` 保护，终态标志用 `std::atomic`（[streaming.h:223-234](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L223-L234)）。

`consume()` 的模板定义就在头文件（[streaming.h:238-262](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L238-L262)）：循环 `wait_for` 直到「有块 或 finished 或 cancelled」，把队列里的块逐个 `pop_front` 并在**释放锁**后回调 handler，直到「终态且队列空」才退出。

**⑤ 生产侧 `push` / `finish`**（[streaming.cpp:44-65](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L44-L65)）：`push` 加锁追加并 `notify_one`；`finish` 是**幂等**的（先检查 `mFinished`，首调用者赢），写 reason 后 `notify_all` 唤醒所有阻塞消费者。

**⑥ 每槽状态 `SlotStreamState`**（[streaming.h:269-305](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L269-L305)）：住在 `DecodingInferenceContext` 里，和 `tokenIds` 同步被 `compactVector` 压缩（驱逐时一起移除，[llmRuntimeUtils.cpp:459](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.cpp#L459)）。它携带：`channel`、`sentTokenCount`/`lastEmittedTokenCount`（节流与增量切片用）、`pendingBytes`（跨迭代 UTF-8 缓存）、`stopMatchBuffer`/`maxStopLen`（跨迭代停止串缓存）、`terminalReason`、`pendingEmitText`、`stopMatchedThisIter`。

**⑦ 阶段①`decodePerSlot`**（[streaming.cpp:298-355](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L298-L355)）：对每个槽——先做 streamInterval 节流判断（没攒够就跳过），再调 `tokenizer::emitDelta` 把新 token 解码成增量字节（终态时 flush 残留 `pendingBytes`），最后若启用停止串则 `append` 到 `stopMatchBuffer` 并跑 `applyStopStringMatch` 得到本次可安全发出的文本 `pendingEmitText`。

**⑧ 停止串跨迭代匹配 `applyStopStringMatch`**（[streaming.cpp:217-274](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L217-L274)）的核心思路：

```text
若 buffer 里最早出现某个 stop → 发匹配前的部分，清空 buffer，标记 stopMatched
否则若 isFinal               → 全部发出
否则                          → 发出 buffer 除末尾 (maxStopLen-1) 字节外的部分
                                （末尾留着，防 stop 串横跨两次输出边界）
```

**⑨ 阶段②`emitChunks`**（[streaming.cpp:357-416](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L357-L416)）：非流式槽直接跳过；流式槽把 `[lastEmittedTokenCount, sentTokenCount)` 的 token 切进 `chunk.tokenIds`，把 `pendingEmitText` 移进 `chunk.text`，按需填 `chunk.logprobs`，然后 `channel->push(chunk)`。若是终态，`chunk.finished=true`、`chunk.reason=terminalReason`，并在 push 后调 `channel->finish(reason)`。

**⑩ RAII 终结器 `StreamChannelFinalizer`**（[streaming.h:317-331](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L317-L331)，析构 [streaming.cpp:428-497](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L428-L497)）：析构时对所有**还没终结**的通道，尽力拼一个含剩余 token/文本、`reason=kError` 的终止块 push 出去（OOM 等异常被 `catch(...)` 吞掉），然后**无条件** `finish(kError)`。这保证了「即便 handleRequest 中途抛异常，消费者也一定能等到一个终止信号、不会死等」——这是流式管道最关键的可靠性不变式。

#### 4.3.4 代码实践：用 StreamChannel 写一个流式消费者

**实践目标**：掌握 `StreamChannel` 的创建、挂载与消费，并验证「取消」与「RAII 兜底」两条不变式。

**操作步骤**：

1. 读 [streaming.h:151-235](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L151-L235) 与 [streaming.cpp:38-65](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L38-L65)，弄清消费者 API（`consume`/`tryPop`/`cancel`）与生产者 API（`push`/`finish`）的可见性差异。
2. 参考下面的**示例代码**（非项目原有），写一个最小流式调用：

```cpp
// 示例代码：挂一个流式通道并把生成的字增量打印
auto channel = rt::StreamChannel::create();   // 工厂创建，shared_ptr 所有权
channel->setSkipSpecialTokens(true);

rt::LLMGenerationRequest req;
req.requests.push_back({/*messages*/...});
req.maxGenerateLength = 128;
req.streamChannels.push_back(channel);        // 槽 0 启用流式（与 requests 等长）

rt::LLMGenerationResponse resp;

// 消费者线程：阻塞消费直到终止块
std::thread consumer([&]{
    channel->consume([&](rt::StreamChunk&& chunk){
        std::cout << chunk.text << std::flush;   // 已是合法 UTF-8、停止串已裁剪
        if (chunk.finished)
            std::cout << "\n[reason: "
                      << rt::finishReasonName(chunk.reason) << "]\n";
    });
});

runtime->handleRequest(req, resp, stream);    // 生产者：在解码循环里 push 分块
consumer.join();                               // 必然能 join：RAII 终结器保证 channel 会被 finish
```

3. 思考两个边界场景并写出你的预测：
   - 在 `handleRequest` 返回前，在另一个线程调 `channel->cancel()`：会发生什么？`finishReason` 会是什么？
   - 如果 `handleRequest` 内部因 OOM 抛异常：消费者线程会永远阻塞吗？

**需要观察的现象 / 预期结果**：

- `cancel()` 把 `mCancelled` 置位，下一轮迭代顶部的 `applyCancellationToFinishStates`（[streaming.cpp:276-296](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L276-L296)）把它写进 `terminalReason=kCancelled`，随后 `emitChunks` 发出终止块并以 `kCancelled` 结束。
- 即便运行时异常退出，`StreamChannelFinalizer` 的析构也会 `finish(kError)`，消费者的 `consume` 因 `mFinished` 唤醒而必然返回——**不会死等**。
- 通道数必须等于 `requests.size()`；若想只让某槽流式、其他槽非流式，把对应位置填 `nullptr` 即可（`validateStreamingSubmission` 允许，见 [streaming.cpp:184-188](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L184-L188)）。

以上运行行为**待本地验证**（需要可用的 engine 与 CUDA 环境）。

#### 4.3.5 小练习与答案

**练习 1**：`TokenCallback` 和 `StreamChannel` 都能「增量拿 token」，主要区别是什么？什么场景该用哪个？
> **答**：`TokenCallback`（[llmRuntimeUtils.h:68-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L68-L79)）只回调**原始 token id**，无文本解码、无停止串裁剪、无线程同步，开销极小，适合内部拿 id 做二次处理（如 Talker 流水线喂隐层）。`StreamChannel` 推送的是**已解码、UTF-8 合法、停止串已裁剪**的文本块，带 mutex/条件变量/RAII 兜底，适合面向用户的流式输出（如服务端 SSE）。要给最终用户「打字机效果」就用 `StreamChannel`；只想在引擎内部观察 token 就用 `TokenCallback`。

**练习 2**：为什么 `decodePerSlot` 不直接 `push` 分块，而是先把文本放进 `pendingEmitText`，交给后面的 `emitChunks`？
> **答**：为了把「算可发文本」和「定稿终止原因」解耦。`pendingEmitText` 算完后，中间隔着 `updateFinishStates` 来定稿 `finishedStates`/`terminalReason`，`emitChunks` 再据此决定是否打 `finished=true`、附带哪个 `reason`。这样「终止块的原因」永远是先定稿再发出，避免「块已发出但原因未定」的竞态（见 [streaming.h:103-116](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L103-L116) 的阶段注释）。

**练习 3**：`streamChannels` 为空数组 vs 含一个 `nullptr`，分别是什么含义？
> **答**：空数组表示**全局关闭流式**（`validateStreamingSubmission` 直接返回 true，[streaming.cpp:173-176](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L173-L176)）。非空数组里的 `nullptr` 元素表示**该槽按槽退出流式**（输出走非流式收尾路径，在 `handleRequest` 末尾一次性装配进 `response`）。

---

## 5. 综合实践

**任务：为一个「图文问答 + 流式输出」场景组装完整请求并预测响应形态。**

假设你要用 Qwen3-VL 做一个「看图说话」服务，要求：

1. 系统提示固定为「你是一个图像描述助手」。
2. 用户输入一段文字 + 一张图片，希望模型逐字流式返回描述。
3. 设 `temperature=0.8`、`topP=0.95`、`maxGenerateLength=200`，并设停止串 `"<end>"`。
4. 挂一套名为 `"desc_lora"` 的 LoRA 权重。

请完成：

1. **填请求**：写出 `LLMGenerationRequest` 各字段（提示：`requests[0].messages` 要含 system + user 两条；user 的 `contents` 是 `[text, image]`；`imageBuffers` 与之对齐；`stopStrings` 放在内层 `Request` 还是外层？对照 [llmRuntimeUtils.h:108-123](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L108-L123) 确认；`streamChannels` 要与 `requests` 等长）。
2. **画时序**：画出「消费者线程、`handleRequest` 解码循环、`StreamChannelFinalizer`」三者在一次正常结束和一次中途 `cancel()` 两种情况下的时序，标注 `push`/`finish`/`consume` 唤醒点。
3. **预测响应**：若模型在第 150 个 token 输出 `"<end>"`，预测 `response.finishReasons[0]`、`response.outputTexts[0]` 是否含 `"<end>"`、`response.outputIds[0]` 是否含对应 token。对照 [llmInferenceRuntime.cpp:1047-1076](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1047-L1076) 验证你的预测。

> 提示与坑：`stopStrings` 是**单序列级**（内层 `Request`），不是批次级；`outputIds` 故意不被停止串裁剪而 `outputTexts` 会被裁剪；流式路径与一次性解码对停止串的识别可能不同步，收尾会做二次校验。运行验证**待本地验证**。

## 6. 本讲小结

- 请求是三层嵌套：`Message`（role + contents[]）→ `Request`（单序列：messages + imageBuffers/audioBuffers + stopStrings）→ `LLMGenerationRequest`（整批：requests[] + 批次级采样参数 + loraWeightsName + streamChannels）。
- 多模态是「双轨」：文字走 `contents` → 分词成 `inputIds`；图片/音频走 `imageBuffers`/`audioBuffers`，按 `contents` 中 `image` 块的出现次序按下标对齐。
- 响应 `LLMGenerationResponse` 含 `outputIds`/`outputTexts`/`logprobs`/`finishReasons`，用 `originalIdx` 保持与请求顺序对齐；`outputIds` 保留完整 token 流，只有 `outputTexts` 被停止串裁剪。
- `FinishReason` 六值（EndId/Length/Cancelled/StopWords/Error/NotFinished）同时用于响应和流式终止块，是「为什么停」的唯一枚举。
- 两条增量通道：轻量 `TokenCallback`（只回 token id、零开销）与完整 `StreamChannel`（MPSC 队列、UTF-8/停止串处理、取消、节流、RAII 兜底）。
- 流式在解码循环里分两阶段：`decodePerSlot` 算可发文本（含跨迭代 UTF-8 与停止串缓存），`emitChunks` 真正 push 并在终态 `finish(reason)`；`StreamChannelFinalizer` 保证异常路径下消费者「永不死等」。

## 7. 下一步学习建议

- **走向引擎执行与张量绑定**：请求被消费后，token 如何喂进引擎、KV 缓存如何挂载？继续 [u5-l3 引擎执行器与张量注册表](u5-l3-engine-executor-tensor-registry.md)。
- **深入解码主循环的另一半**：`decodeStep` 内部、普通解码与投机解码的差异在 [u5-l4 解码策略与解码器注册表](u5-l4-decoding-strategies-decoder-registry.md)。
- **采样与分词细节**：`temperature`/`topK`/`topP` 如何在 GPU 上作用 logits、`emitDelta` 的增量解码原理，见 [u5-l7 采样与分词](u5-l7-sampling-and-tokenization.md)。
- **把流式接到服务端**：[u9-l5 实验性 Python API 与 OpenAI 服务端](u9-l5-experimental-python-api-server.md) 会展示 `StreamChannel` 如何对接 HTTP 流式响应。
- **系统提示 KV 缓存**：请求里的 `saveSystemPromptKVCache` 开关与流式首字延迟的关系，见 [u9-l2 系统提示 KV 缓存与流式](u9-l2-system-prompt-cache-and-streaming.md)。
