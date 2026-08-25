# 视觉模型：多模态输入处理

## 1. 本讲目标

本讲是单元三（核心推理管线）的收官篇。前面五讲我们一直在处理「纯文本进、纯文本出」的链路，本讲把图片加进来。学完后你应该能够：

1. 掌握多模态消息 `content` 数组的构造方式：如何在 OpenAI 风格的请求里携带图片，协议层做了哪些校验。
2. 说清一条完整的数据变换链：图片 URL → `ImageData` → RGB 像素数组 → GPU 张量 → `image_embed` 核算出的图像 embedding → 拼进 prefill。
3. 理解 `image_embed` 这个可选 PackedFunc 与 `imageDataCache` 各自的作用。
4. 识别视觉输入相关的六个错误类型，知道它们各自在哪个环节、什么条件下被抛出，尤其是 `PrefillChunkSizeSmallerThanImageError`（承接 [u3-l3](./u3-l3-prefill-step.md) 讲过的分块预填充）。

## 2. 前置知识

### 2.1 视觉语言模型（VLM）是什么

普通 LLM 只吃 token 序列。视觉语言模型（Vision-Language Model）在文本 token 之外还能接受图片：模型内部有一个**视觉编码器**（通常是 ViT 一类结构），把图片切成小块（patch）编码成一串向量，这些向量与文本 token 的 embedding 处在同一向量空间里，然后一并送入 Transformer 主干。

关键直觉：**图片在模型眼里不是「一张图」，而是一串准 token**。一张图到底折算成多少个 token，取决于模型和图片分辨率——对 Phi-3.5-vision 这类模型，一张 1080P 横版图片约折算 2353 个 token（下文会用源码与测试验证这个数字）。所以：

- 图片会大量消耗 `context_window_size`（这就是视觉示例里把窗口设成 6144 的原因）；
- 图片的 token 数还必须能塞进一个 prefill chunk，否则报错（本讲的重点错误之一）。

### 2.2 像素数据在浏览器里的表示

- `ImageData`：浏览器标准对象，`data` 字段是 RGBA 四通道、每通道 8 位的字节数组（Uint8ClampedArray），按行从左上到右下排列。
- NHWC：张量布局约定，即 `[批大小 N, 高 H, 宽 W, 通道 C]`。WebLLM 送入 `image_embed` 核的像素张量就是 `[1, H, W, 3]` 的 NHWC 布局，所以需要先把 RGBA 摘成 RGB。
- data URL：形如 `data:image/png;base64,...` 的内联图片编码，可以直接塞进字符串，不走网络与 CORS。OpenAI 的 `image_url.url` 字段同时接受 http(s) URL 和 data URL。

### 2.3 BOI / EOI 标记

BOI（Begin of Image）/ EOI（End of Image）是模型词表里的特殊 token，用来在 token 流中「框住」一段图像 embedding。Phi-3.5-vision 对应 `<image>` / `<end_of_image>`。它们在模型配置里以 `vision_start_token_id` / `vision_end_token_id`（旧名 `boi_token_index` / `eoi_token_index`）声明。

### 2.4 回顾两个前置结论

- [u3-l1](./u3-l1-pipeline-init-and-tvm.md)：管线的计算核都是从 wasm model library 的 VirtualMachine 里按名字取出的 PackedFunc，取不到就说明模型没编译这个能力。
- [u3-l3](./u3-l3-prefill-step.md)：prefill 的输入会被 `getChunkedPrefillInputData` 贪心切成不超过 `prefillChunkSize` 的块，**token 序列可以切开，图片不可以**——本讲会看到这条约束如何变成一个显式的错误。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/openai_api_protocols/chat_completion.ts` | 定义 `ChatCompletionContentPart` 系列类型（多模态 content 数组），并在 `postInitAndCheckFields` 里做请求级校验 |
| `src/engine.ts` | `chatCompletion` 入口：取模型状态、把 `modelType` 传给协议校验 |
| `src/conversation.ts` | 把 content 数组拍平成「字符串与 ImageURL 混合」的 prompt 数组，按模型类型排布图片位置 |
| `src/llm_chat.ts` | 管线主体：`getInputData` 预载图片尺寸并编码、`getImageEmbeddings` 调 `image_embed` 核、`embedAndForward` 拼接 embedding 并前向 |
| `src/support.ts` | `getImageDataFromURL`（URL→ImageData）、`getRGBArrayFromImageData`（RGBA→RGB）、`getChunkedPrefillInputData`（分块，图片超块即抛错） |
| `src/error.ts` | 多模态错误族的六个类 |
| `src/config.ts` | `ModelType` 枚举、`VisionModelConfig`（`mm_tokens_per_image` 等字段）、预置的 Phi-3.5-vision 模型记录 |
| `examples/vision-model/` | 官方视觉示例：双图 prefill + 纯文本追问 + 单图追问 |
| `tests/llm_chat_pipeline.test.ts` | mock 单测：`calculateResizeShape` / `calculateCropShape` / `computeImageEmbedSize` 的数值断言 |
| `tests/openai_chat_completion.test.ts` | mock 单测：`image_url.detail` 不支持等请求校验行为 |

## 4. 核心概念与源码讲解

### 4.1 多模态消息的表示与校验：content 数组如何进入引擎

#### 4.1.1 概念说明

WebLLM 的 chat 接口兼容 OpenAI 多模态写法：用户消息的 `content` 不再是字符串，而是一个 `contentPart` 数组，每个元素要么是 `{type: "text", text: ...}`，要么是 `{type: "image_url", image_url: {url: ...}}`。

但不是随便发都能被接受。协议层在请求进入引擎前要回答三个问题：

1. 当前加载的模型是不是 VLM？（纯文本模型收到图片数组要立刻拒绝）
2. 图片 URL 的格式是否受支持？（必须是 `http` 开头或 `data:image` 开头的 base64）
3. content 数组结构是否合法？（每条消息至多一个 text part；不支持 OpenAI 的 `detail` 字段）

这三道关卡由 `postInitAndCheckFields` 把守，而「我是不是 VLM」这个信息来自 `ModelType` 枚举——`LLM`、`embedding`、`VLM` 三值，记录在 ModelRecord 里：

- [src/config.ts:L251-L255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L251-L255)：`ModelType` 枚举定义，注释标明 `VLM` 即 vision-language model。

当前 `prebuiltAppConfig` 里只有两条 VLM 记录，都是 Phi-3.5-vision：

- [src/config.ts:L752-L767](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L752-L767)：`Phi-3.5-vision-instruct-q4f16_1-MLC`，model_lib 后缀 `_cs2k`，`vram_required_MB: 3952.18`，overrides 里 `context_window_size: 4096`，`model_type: ModelType.VLM`。
- [src/config.ts:L768-L782](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L768-L782)：q4f32_1 版本，其余结构相同。

#### 4.1.2 核心流程

```text
engine.chatCompletion(request)
  ├─ getLLMStates()：按 request.model 路由到已加载管线
  ├─ loadedModelIdToModelType.get()：取出该模型的 ModelType
  ├─ API.postInitAndCheckFieldsChatCompletion(request, modelId, modelType)
  │    ├─ user 消息 content 非字符串 且 modelType !== VLM
  │    │     → 抛 UserMessageContentErrorForNonVLM
  │    ├─ image_url.detail 有值 → 抛 UnsupportedDetailError
  │    ├─ url 不以 "data:image" 或 "http" 开头 → 抛 UnsupportedImageURLError
  │    └─ text part 多于 1 个 → 抛 MultipleTextContentError
  └─ ……（后续走 u2-l2 讲过的生成流程）
```

#### 4.1.3 源码精读

先看类型定义。content part 是一个两成员的联合类型：

- [src/openai_api_protocols/chat_completion.ts:L613-L627](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L613-L627)：`ChatCompletionContentPart = ChatCompletionContentPartText | ChatCompletionContentPartImage`，text part 只有 `text` 和 `type: "text"` 两个字段。
- [src/openai_api_protocols/chat_completion.ts:L629-L649](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L629-L649)：`ImageURL` 接口——`url` 接受图片 URL 或 base64 数据，`detail?: "auto" | "low" | "high"` 声明了字段但实现上不支持。`ChatCompletionContentPartImage` 把它包成 `{image_url, type: "image_url"}`。
- [src/openai_api_protocols/chat_completion.ts:L726-L730](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L726-L730)：用户消息的 `content: string | Array<ChatCompletionContentPart>`——这正是多模态入口的类型层面体现。

再看引擎入口如何把模型类型传进校验：

- [src/engine.ts:L800-L809](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L800-L809)：`chatCompletion` 先 `getLLMStates` 路由到管线，再从 `loadedModelIdToModelType` 取出 `ModelType`，调用 `API.postInitAndCheckFieldsChatCompletion`（它在 [src/openai_api_protocols/index.ts:L27](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/index.ts#L27) 里由 `postInitAndCheckFields` 改名重导出）。

校验主体（注意所有多模态校验都只针对 `role === "user"` 的消息）：

- [src/openai_api_protocols/chat_completion.ts:L435-L447](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L435-L447)：遍历 messages；user 消息 content 非字符串时，若 `currentModelType !== ModelType.VLM` 立即抛 `UserMessageContentErrorForNonVLM`——非视觉模型连图片数组的门都进不了。
- [src/openai_api_protocols/chat_completion.ts:L448-L461](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L448-L461)：逐个检查 content part：`image_url.detail` 有值抛 `UnsupportedDetailError`；`url` 既不以 `data:image` 也不以 `http` 开头抛 `UnsupportedImageURLError`。
- [src/openai_api_protocols/chat_completion.ts:L462-L471](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L462-L471)：统计 text part 数量，超过 1 抛 `MultipleTextContentError`（源码注释留有 TODO：是否所有模型都只允许一个 text part）。

通过校验后，`Conversation` 负责把 content 数组「拍平」。它内部以三元组 `[Role, 角色串, content]` 维护历史，content 允许是 contentPart 数组：

- [src/conversation.ts:L127-L151](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L127-L151)：把一条消息拆成 `textContentPart`（至多一个，多了抛 `MultipleTextContentError`）与 `imageContentParts`（`ImageURL` 数组，顺序保留）。
- [src/conversation.ts:L188-L211](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L188-L211)：组装输出。无图片时消息仍是纯字符串；有图片时输出「字符串与 ImageURL 混合」的数组——先是 `role_prefix`，然后逐个插入图片，最后拼 text 与分隔符。注意按模型排布：`phi3_v` 会在每张图片后面补一个 `"\n"`，其他模型直接排。

于是 prompt 数组的形状变成了（对应 [src/llm_chat.ts:L1997-L2008](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1997-L2008) 的注释示例）：

```text
[
  "<|system|>\nSome system prompt\n",          // 纯字符串消息
  [
    "<|user|>\n",                               // 混合消息：字符串
    imageURL1,                                  //           图片（暂不编码）
    "\n",
    imageURL2,
    "\n",
    "Some user input<|end|>\n",
  ],
]
```

#### 4.1.4 代码实践

**实践目标**：亲手触发协议层的三种多模态校验错误，把「请求长什么样 → 抛什么错」对应起来。

**操作步骤**（改 `examples/vision-model/src/vision_model.ts`，每次只改 `messages` 后刷新页面）：

1. 按 [examples/vision-model/README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/vision-model/README.md) 跑起示例（`npm install && npm start`，Parcel 监听 8888 端口）。
2. 给图片 content part 加上 `detail: "high"`：

   ```ts
   { type: "image_url", image_url: { url: someUrl, detail: "high" } }
   ```

3. 把 `url` 换成 `"/not-a-valid-scheme.png"`（既非 `http` 也非 `data:image`）。
4. 在同一条 user 消息里放两个 `{type: "text"}` part。
5. 每次用 `try { await engine.chat.completions.create(request); } catch (e) { console.error(e.name, e.message); }` 包住调用，记录错误名。

**需要观察的现象**：控制台依次打出 `UnsupportedDetailError`、`UnsupportedImageURLError`、`MultipleTextContentError`，错误信息与 [src/error.ts:L145-L191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L145-L191) 中的文案一致；且错误在 prefill 开始前（模型还没耗任何算力）就抛出。

**预期结果**：三类错误都在 `postInitAndCheckFields` 阶段被拦截。第四类 `UserMessageContentErrorForNonVLM` 需要加载一个非 VLM 模型才能触发，成本较高，可直接读单测验证行为：[tests/openai_chat_completion.test.ts:L167-L194](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts#L167-L194) 断言了 `detail: "high"` 时必须抛出 `"Currently do not support field image_url.detail, but received: high"`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `system` 和 `assistant` 消息不需要做多模态校验？

**答案**：这套校验只处理「用户向模型提供输入图片」的场景；system/assistant 的 content 在类型上就是纯字符串（见 `ChatCompletionSystemMessageParam` 等定义），图片只能由用户注入。源码上，校验循环只对 `message.role === "user" && typeof message.content !== "string"` 分支做图片检查（[src/openai_api_protocols/chat_completion.ts:L439-L447](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L439-L447)）。

**练习 2**：`Conversation.getPromptArray` 为什么不直接把图片编码成 token，而是保留 `ImageURL` 对象原样放进 prompt 数组？

**答案**：因为图片「编码」不是 tokenizer 的工作，而是 GPU 上 `image_embed` 核的工作。Conversation 只负责排布顺序（谁在前、图片后面补不补换行），把 `ImageURL` 作为不可再分的原子元素传给管线，由管线在 prefill 时调视觉编码器。这也解释了为什么分块时「token 可切、图片不可切」。

**练习 3**：`imageDataCache` 与请求校验有什么关系？为什么它的 key 是 URL 字符串？

**答案**：没有直接关系——校验只看 URL 前缀合法性，不下载图片；下载发生在管线 `getInputData` 阶段。key 用 URL 是因为同一 URL 必然对应同一张图（base64 data URL 更是内容寻址），以 URL 为键可以在一次 prefill 的「预载尺寸」与「真正编码」两个阶段间复用同一份 `ImageData`，避免同一张图被 fetch 两次。

### 4.2 图像数据预处理：从 URL 到 GPU 张量

#### 4.2.1 概念说明

图片在送进 GPU 之前要经历三步纯浏览器端的变换：

1. **取像素**：`fetch` URL（或 data URL）拿到 blob，解码成位图，画到离屏 canvas 上读出 `ImageData`（RGBA）。
2. **摘通道**：丢掉 Alpha 通道，RGBA 变 RGB。
3. **算尺寸**：根据模型规则算出这张图**将被缩放（resize）与裁剪（crop）成什么形状**，以及它最终折算多少个 token（embed size）。

第 3 步是理解「图片占多少 token」的关键。`VisionModelConfig` 为此预留了字段：

- [src/config.ts:L31-L40](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L31-L40)：`mm_tokens_per_image`（每图固定 token 数）、`boi_token_index` / `eoi_token_index`、`vision_start_token_id` / `vision_end_token_id`。

`imageDataCache`（[src/llm_chat.ts:L116](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L116)）是一个 `Map<string, ImageData>`：`getInputData` 阶段按 URL 预载所有图片（为了提前拿到宽高算 embed size），`getImageEmbeddings` 阶段直接命中缓存，同一张图只下载一次。它在每次 prefill 开始（[src/llm_chat.ts:L2062](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2062)）和 prefill 结束（[src/llm_chat.ts:L884](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L884)）被清空——它是「单次 prefill 内的去重缓存」，不是跨轮缓存（跨轮复用的是写进 KV cache 的图像 embedding，不需要再碰像素）。

#### 4.2.2 核心流程

phi3_v（Phi-3.5-vision）的尺寸计算规则，设输入图为 \( H \times W \)，宽高比 \( r = W/H \)：

1. **resize**：求最大整数 \( s \) 使得 \( s \cdot \lceil s/r \rceil \le 16 \)（即最多 16 个 336×336 子图），然后 \( W' = 336s \)，\( H' = \lfloor W'/r \rfloor \)。
2. **crop**：\( H_{pad} = \lceil H'/336 \rceil \cdot 336 \)，\( cropH = \lfloor H_{pad}/336 \rfloor \)，\( cropW = \lfloor W'/336 \rfloor \)。
3. **embed size**：

\[ \text{subTokens} = cropH \cdot 12 \cdot (cropW \cdot 12 + 1), \qquad \text{glbTokens} = 12 \cdot (12 + 1) = 156 \]

\[ \text{embedSize} = \text{subTokens} + 1 + \text{glbTokens} \]

直觉：每个 336×336 子图切成 12×12 个 patch、每个 patch 一个 token（再加一个汇总位），全局再压缩出 156 个 token 的「全图摘要」，中间的 +1 是分类位。这与 mlc-llm 服务端 `serve/data.py` 的 `_compute_embed_size` 保持一致（源码注释注明了对齐关系）。

用三个具体例子验算（数值直接来自单测断言）：

| 输入 (H×W) | resize 后 | crop (cropH×cropW) | embedSize |
| --- | --- | --- | --- |
| 336×336（正方形） | 1344×1344 | 4×4 | \(48 \times 49 + 157 = 2509\) |
| 1080×1920（横版） | 945×1680 | 3×5 | \(36 \times 61 + 157 = 2353\) |
| 1920×1080（竖版） | 1194×672 | 4×2 | \(48 \times 25 + 157 = 1357\) |

注意两个推论：**正方形图片 token 最多**（16 个子图用满）；而 Phi-3.5-vision 的 wasm 后缀 `_cs2k` 表示 chunk size 2048——2509 与 2353 都超过 2048，这正是 4.4 节 `PrefillChunkSizeSmallerThanImageError` 的现实触发路径。

#### 4.2.3 源码精读

**取像素**——`getImageDataFromURL` 只认两种 URL：`http` 开头（fetch 走 CORS）与 `data:image` 开头（base64 内联）：

- [src/support.ts:L420-L432](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L420-L432)：`fetch(url, {mode: "cors"})` → `createImageBitmap` → 画到 `OffscreenCanvas` → `ctx.getImageData` 得到 RGBA 的 `ImageData`。用 `OffscreenCanvas` 而非 DOM canvas，是为了在 Web Worker 里也能用（视觉示例默认跑在 worker 中）。

**摘通道**：

- [src/support.ts:L439-L449](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L439-L449)：`getRGBArrayFromImageData` 每 4 字节取前 3 字节，RGBA → RGB，输出 `Uint8ClampedArray`，行序从左上到右下。

**算尺寸**——三个私有方法只认 `phi3_v`：

- [src/llm_chat.ts:L1112-L1134](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1112-L1134)：`calculateResizeShape`，`hdNum = 16`，while 循环求 scale，返回 `[newH, 336*scale]`；其他 model_type 抛 `Unsupported model type ... for image resize`。
- [src/llm_chat.ts:L1140-L1158](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1140-L1158)：`calculateCropShape`，先 resize 再对齐到 336 的整数倍。
- [src/llm_chat.ts:L1086-L1106](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1086-L1106)：`computeImageEmbedSize`——`phi3_v` 走上面的公式；否则查 `model_config.mm_tokens_per_image`（固定 token 数的模型如 Gemma3V）；两者都没有则抛 `"Cannot determine image embed size"`。也就是说：**图片 token 数要么由分辨率算出，要么由模型配置直接给定**。

**预载与缓存**——`getInputData` 在编码 prompt 之前先把所有图片的尺寸摸清（因为分块器需要知道每张图折算多少 token）：

- [src/llm_chat.ts:L2047-L2069](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2047-L2069)：收集 `uniqueImageUrls`；若存在图片但 `image_embed === undefined` 直接抛 `CannotFindImageEmbedError`；清空 `imageDataCache` 后用 `Promise.all` 并发下载所有图片，存入缓存并记录 `imageDimensions`。
- [src/llm_chat.ts:L2070-L2076](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2070-L2076)：闭包 `getEmbedSize(image)` 基于预载的宽高返回 `computeImageEmbedSize` 的结果，供后续分块器回调使用。

**编码与 BOI/EOI**：

- [src/llm_chat.ts:L2078-L2081](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2078-L2081)：解析 BOI/EOI token id，`vision_start_token_id ?? boi_token_index`（新字段优先、旧字段兜底）。
- [src/llm_chat.ts:L2086-L2118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2086-L2118)：遍历 prompt 数组——字符串部分正常 `tokenizer.encode`；遇到 `ImageURL` 时：先压入 BOI token（如配置），把已累积的 `curTokens` 作为一段 push 进结果、再 push 图片对象本身、`numPromptTokens += getEmbedSize(image)`，然后压入 EOI token 开启新一段。**图片的 token 数在这一步就计入了 prompt 长度**。
- [src/llm_chat.ts:L2124-L2133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2124-L2133)：`numPromptTokens + filledKVCacheLength > contextWindowSize` 时抛 `ContextWindowSizeExceededError`（非滑动窗口模型）——图片 token 计入窗口检查，这是视觉示例把 `context_window_size` 覆盖为 6144 的直接原因。

#### 4.2.4 代码实践

**实践目标**：不用 GPU，用 mock 单测验证尺寸计算公式，并学会估算「我的图片会折算成多少 token」。

**操作步骤**：

1. 打开 [tests/llm_chat_pipeline.test.ts:L297-L372](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L297-L372)，阅读 `calculateResizeShape`、`calculateCropShape`、`computeImageEmbedSize` 三组 describe——测试通过 `pipeline["config"] = { model_type: "phi3_v" } as any` 直接注入私有字段，无需真实模型。
2. 运行：`npx jest tests/llm_chat_pipeline.test.ts -t "computeImageEmbedSize"`（仓库根目录下；jest 配置见 `jest.config.cjs`）。
3. 仿照现有用例，在文件中追加一个自己的用例（示例代码，非仓库原有内容）：

   ```ts
   test("my image: 800x600 landscape", () => {
     const pipeline = createPipeline();
     pipeline["config"] = { model_type: "phi3_v" } as any;
     // r = 600? 先算：H=800? 注意参数顺序是 (height, width)
     expect(pipeline["computeImageEmbedSize"](600, 800)).toBe(1921);
   });
   ```

   先手算再验证：\( r = 800/600 \approx 1.333 \)，\( s=4 \) 时 \( 4 \cdot \lceil 3 \rceil = 12 \le 16 \)、\( s=5 \) 时 \( 5 \cdot 4 = 20 > 16 \)，故 \( W' = 1344, H' = 1008 \)，crop 为 3×4，embedSize \( = 36 \times 49 + 157 = 1921 \)。

**需要观察的现象**：测试通过；同时你会在 [src/support.ts:L258-L276](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L258-L276) 的注释里看到 1921 这个数字——官方注释里的分块示例正是一张 1921 token 的图片配 2048 的 chunk size。

**预期结果**：`computeImageEmbedSize(600, 800) === 1921`。手算与测试一致即说明你掌握了公式；若环境中 jest 无法运行（如缺依赖），此实践退化为纯手算验算，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：同一张 1920×1080 的图片，横放（1080×1920 参数序为 height=1080, width=1920）与竖放分别折算多少 token？为什么差这么多？

**答案**：横版（H=1080, W=1920）为 2353，竖版（H=1920, W=1080）为 1357。公式以宽度为基准做 `336*scale` 放大再按宽高比缩高度，宽高比不同导致允许的 scale 与裁出的子图网格（3×5 vs 4×2）不同；子图越多 patch 越多，token 越多。极端是 1:1 正方形，16 个子图用满，token 最多（2509）。

**练习 2**：为什么 `getInputData` 要在编码任何文本之前先并发下载所有图片？

**答案**：分块器 `getChunkedPrefillInputData` 需要一个 `getImageEmbedSize` 回调来判断每个 chunk 的长度，而 phi3_v 的 embed size 依赖图片宽高——不先拿到 `ImageData` 就无法知道宽高。所以流程必须是「先下载 → 算尺寸 → 再分块」，下载本身用 `Promise.all` 并发以省时间，结果顺手存进 `imageDataCache` 供后面 `getImageEmbeddings` 复用。

**练习 3**：`mm_tokens_per_image` 与 phi3_v 公式两条路径，分别适合什么样的视觉模型？

**答案**：phi3_v 这类**动态分辨率**模型，token 数随图片分辨率变化，必须按公式算；`mm_tokens_per_image` 适合**固定 token 数**的模型（如 Gemma3V，任何图片都编码成固定长度向量串），直接读配置即可。源码顺序是先查 phi3_v 公式、再查 `mm_tokens_per_image`、两者皆无则抛错（[src/llm_chat.ts:L1086-L1106](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1086-L1106)）。

### 4.3 image_embed 前向：图像如何嵌入 prefill

#### 4.3.1 概念说明

`image_embed` 是 wasm model library 导出的一个**可选** PackedFunc——模型编译时带了视觉编码器才有。它的职责是把 `[1, H, W, 3]` 的像素张量变成 `[embedSize, hiddenSize]` 的 embedding 张量，与文本 token 经 `embed` 核得到的张量**同形状、同空间**。正因如此，管线可以把「文本 embedding + 图像 embedding」直接 `concat` 成一条序列送进 prefill 内核，KV cache 照常写入——对 Transformer 主干来说，图像 embedding 就是排在前缀位置的一串「准 token」。

这也解释了多轮视觉对话为什么快：第一轮 prefill 把图像 embedding 写进了 KV cache，后续纯文本追问（示例第 2 步）只需增量编码新 token，图片完全不用重新编码。

#### 4.3.2 核心流程

```text
prefillStep（u3-l3 已讲的外壳）
  └─ getInputData()                        # 4.2 节：得到 [token段, ImageURL, token段, ...]
  └─ getChunkedPrefillInputData(...)       # 按 prefillChunkSize 分块（图片原子不可切）
  └─ 对每个 chunk 调 embedAndForward(chunk, chunkLen)
       ├─ 逐项编码：
       │    ├─ Array.isArray(data) → getTokensEmbeddings → this.embed(tokenIds, params)
       │    └─ 否则（ImageURL）  → getImageEmbeddings：
       │         ├─ imageDataCache 取或下载 ImageData
       │         ├─ RGBA → RGB → tvm.empty([H,W,3]).copyFrom().view([1,H,W,3])   # NHWC
       │         ├─ calculateResizeShape / calculateCropShape → 4 个 ShapeTuple
       │         └─ this.image_embed(pixelArray, 4个形状, params) → [embedSize, hidden]
       ├─ concatEmbeddings(embeddings) 拼成一条序列
       ├─ kv_state_begin_forward → invokePrefill(或 invokeDecode) → kv_state_end_forward
       └─ filledKVCacheLength += chunkLen   # 图像 token 计入 KV 账本
```

#### 4.3.3 源码精读

**函数从哪来**（承接 u3-l1 的 ABI 解析）：

- [src/llm_chat.ts:L231-L246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L231-L246)：`loadVMFunctionRegistry` 的候选名单里包含 `"image_embed"`——注意它与 `embed`（文本查表）是两个不同的核。
- [src/llm_chat.ts:L336-L341](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L336-L341)：注册表里**有**才绑定到 `this.image_embed`，没有只打一条 info 日志 `"Cannot find function image_embed."`——字段声明是 `PackedFunc | undefined`（[src/llm_chat.ts:L72](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L72)）。这与 `prefill`/`decode` 等**必需**函数（缺失直接抛错）形成对比：视觉能力是可选 ABI。

**图像编码全流程**——`getImageEmbeddings`：

- [src/llm_chat.ts:L1163-L1175](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1163-L1175)：先查 `imageDataCache`（4.2 节预载的命中处）， miss 才 `getImageDataFromURL`；`getRGBArrayFromImageData` 摘出 RGB；`tvm.empty([H, W, 3], "uint32", device).copyFrom(pixelValues).view([1, H, W, 3])` 构造 NHWC 张量。
- [src/llm_chat.ts:L1177-L1201](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1177-L1201)：算出 resize/crop 四个形状并包成 `makeShapeTuple`，然后调 `this.image_embed(pixelArray, resizeH, resizeW, cropH, cropW, this.params)`——**缩放与裁剪不是在 JS 里做的，而是把目标形状传给 GPU 核，由核在编码时一并完成**。
- [src/llm_chat.ts:L1202-L1214](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1202-L1214)：用 `computeImageEmbedSize` 做事后校验——核返回的 `embed.shape[0]` 必须等于 JS 侧算的 token 数，不等抛 InternalError。这是「JS 侧公式必须与编译期模型一致」的防御性断言。

**拼接与前向**——`embedAndForward`（prefill 与 decode 共用的前向原语，u3-l3 已见过它的 KV 事务结构）：

- [src/llm_chat.ts:L1239-L1249](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1239-L1249)：逐项编码——数组走 `getTokensEmbeddings`（内部调 `this.embed`，见 [src/llm_chat.ts:L1060-L1079](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1060-L1079)），`ImageURL` 走 `getImageEmbeddings`。源码 TODO 注明：`["hi", imageUrl, "hi"]` 目前会调 3 次编码核，其实 2 次就够——编码次数尚未优化。
- [src/llm_chat.ts:L1251-L1261](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1251-L1261)：`tvm.concatEmbeddings` 把多段 embedding 拼成一条，并断言拼接长度 `allEmbeddings.shape[0] === inputDataLen`（文本 token 数 + 图像 embedSize），最后 `view([1, ...])` 加 batch 维。
- [src/llm_chat.ts:L1265-L1283](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1265-L1283)：KV 事务（`fKVCacheBeginForward` 预留长度 → `invokePrefill`（长度 > 1）或 `invokeDecode`（长度 = 1）→ `fKVCacheEndForward` 提交），然后 `filledKVCacheLength += inputDataLen`——**图像的 embedSize 个位置就从此驻留在 KV cache 里**，和文本 token 一视同仁。

**prefillStep 外壳里的图像专属检查**：

- [src/llm_chat.ts:L853-L857](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L853-L857)：`inputData.some((data) => !Array.isArray(data))` 检测输入里有没有图片（图片不是数组），有而 `image_embed === undefined` 则抛 `CannotFindImageEmbedError`——这是协议层检查之外的管线层兜底（低层 API 直接调管线时仍能拦住）。

#### 4.3.4 代码实践

**实践目标**：把「URL → ImageData → RGB 张量 → image_embed → concat → prefill」这条链完整落到源码行号上，形成一张可复查的调用链说明。

**操作步骤**（源码阅读型实践，无需运行）：

1. 从 [examples/vision-model/src/vision_model.ts:L49-L68](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/vision-model/src/vision_model.ts#L49-L68) 的 `messages`（一个 text part + 两个 image part）出发。
2. 依次定位并抄下每一步的文件与行号：`engine.chatCompletion`（engine.ts）→ `postInitAndCheckFields`（chat_completion.ts）→ `getConversationFromChatCompletionRequest` / `getPromptArray`（conversation.ts，图片拍平）→ `prefillStep` → `getInputData`（预载图片）→ `getChunkedPrefillInputData`（分块）→ `embedAndForward` → `getImageEmbeddings` → `this.image_embed(...)`（llm_chat.ts L1193）。
3. 对照本讲 4.3.2 的流程图，检查你的行号链是否覆盖了全部 8 个环节。
4. 运行示例（`logLevel: "INFO"` 已在 [examples/vision-model/src/vision_model.ts:L29-L32](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/vision-model/src/vision_model.ts#L29-L32) 设好），在控制台找 `"Using prefillChunkSize: "` 日志（[src/llm_chat.ts:L353-L354](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L353-L354)），记下这个值——它是 wasm metadata 决定的，无法从 appConfig 修改。

**需要观察的现象**：控制台打印的 `prefillChunkSize`（`_cs2k` 版 wasm 预期为 2048，以实际日志为准）；随后第一轮请求的 `usage.extra` 中 `prefill_tokens_per_s`、`time_to_first_token_s` 明显大于纯文本请求（两张图片近 4000 token 要预填充）。

**预期结果**：得到一份「环节 → 文件:行号 → 一句话说明」的表格。第二轮纯文本追问（示例第 2 步）的 `prefill_tokens` 应远小于第一轮——因为图片已在 KV cache 里。若暂时无法运行浏览器实验，调用链表格部分仍可完成，运行部分标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `image_embed` 缺失时只是打日志，而 `prefill` 缺失时管线构造直接失败？

**答案**：`prefill`/`decode` 是任何 LLM 推理的必备内核，缺了管线根本无法工作，所以按必需 ABI 解析、缺失即抛错；`image_embed` 只在多模态输入时才需要，纯文本模型（绝大多数预置模型）没有这个核完全正常，所以做成可选绑定（`undefined`），到真正收到图片输入时才检查并抛 `CannotFindImageEmbedError`。

**练习 2**：图像 embedding 写入 KV cache 后，第二轮纯文本追问为什么不用重新编码图片？什么情况下会被强制清空？

**答案**：KV cache 里存的是前向过程中的 K/V 向量，图像位置一旦写入就与文本 token 无异；引擎比对新旧 Conversation 发现前缀一致即复用（u2-l2 讲过的增量编码机制），`getPromptArrayLastRound` 只编码新增部分。清空发生在 `resetChat` / `unload` / completion 路径（其每次全清，u2-l4），或新旧会话比对不一致时引擎主动重置。

**练习 3**：`embedAndForward` 里 `inputDataLen > 1` 走 `invokePrefill`、`=== 1` 走 `invokeDecode`，一个只含「1 个文本 token + 1 张 2509 token 图片」的 chunk 会走哪条？`inputDataLen` 是几？

**答案**：`inputDataLen` 是该 chunk 的总序列长度（文本 token 数 + 各图 embedSize 之和），此例为 2510，大于 1，走 `invokePrefill`。分块器保证任何 chunk 的 `inputDataLen` 不超过 `prefillChunkSize`（除非单张图片本身超限——那会在分块器里先抛错）。

### 4.4 多模态错误族：六个错误类与触发时机

#### 4.4.1 概念说明

视觉输入从请求到前向要过五道关卡，每道关卡都有专属错误。把它们按「在哪一层、什么条件」整理成一张表，是排查多模态问题的地图：

| 错误类 | 抛出位置 | 触发条件 |
| --- | --- | --- |
| `UserMessageContentErrorForNonVLM` | 协议层 `postInitAndCheckFields` | 非 VLM 模型收到 content 数组（含图片意图） |
| `UnsupportedDetailError` | 协议层 `postInitAndCheckFields` | `image_url.detail` 有值（类型声明了但未实现） |
| `UnsupportedImageURLError` | 协议层 `postInitAndCheckFields` | `url` 不以 `data:image` 或 `http` 开头 |
| `MultipleTextContentError` | 协议层与 Conversation 拍平处 | 一条消息 text part 超过 1 个 |
| `CannotFindImageEmbedError` | 管线层 `getInputData` / `prefillStep` | 有图片输入但模型无 `image_embed` 核 |
| `PrefillChunkSizeSmallerThanImageError` | 分块器 `getChunkedPrefillInputData` | 单张图片 embedSize > prefillChunkSize |

层次关系值得注意：前四个在**请求进入引擎前**就能拦截（cheap，零算力）；第五个是管线层兜底（覆盖绕过协议层的低层调用）；第六个最特殊——它不是「输入非法」，而是**输入合法但资源装不下**，发生在分块阶段，此时图片可能已经下载完了。

#### 4.4.2 核心流程

`PrefillChunkSizeSmallerThanImageError` 的判定逻辑（在贪心分块器里，对每个元素先做长度计算）：

```text
for 每个 inputData 元素 curData:
    curDataLen = 是token数组 ? curData.length : getImageEmbedSize(curData)
    if curData 是图片 且 curDataLen > prefillChunkSize:
        throw PrefillChunkSizeSmallerThanImageError(prefillChunkSize, curDataLen)
    ...（贪心装块：token 数组可再切细；图片不可切，装不下就独占新块）
```

关键约束：**token 序列可以按 `prefillChunkSize - curChunkLen` 切成任意小段，图片是原子**——它的 embedding 由 `image_embed` 一次性产出，无法半个图半个图地前向（视觉编码器需要全图上下文）。所以「图片 token 数 ≤ 块大小」是硬性前提，源码在函数 docstring 里也写明了这条 precondition。

#### 4.4.3 源码精读

六个错误类的定义全部在 error.ts：

- [src/error.ts:L134-L143](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L134-L143)：`UserMessageContentErrorForNonVLM`——报错文案会带上当前 modelId 与 modelType，方便定位「用错模型」。
- [src/error.ts:L145-L154](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L145-L154)：`PrefillChunkSizeSmallerThanImageError(prefillChunkSize, imageEmbedSize)`——两个构造参数都会进错误信息。
- [src/error.ts:L156-L164](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L156-L164)：`CannotFindImageEmbedError`——提示「只应给视觉模型传图片」。
- [src/error.ts:L166-L173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L166-L173)：`UnsupportedDetailError`。
- [src/error.ts:L175-L182](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L175-L182)：`UnsupportedImageURLError`。
- [src/error.ts:L184-L191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L184-L191)：`MultipleTextContentError`。

抛出点：
- 协议层四个见 4.1.3 已引用的 [src/openai_api_protocols/chat_completion.ts:L435-L472](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L435-L472)；`MultipleTextContentError` 在 Conversation 拍平时还有第二个抛出点 [src/conversation.ts:L135-L147](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L135-L147)。
- `CannotFindImageEmbedError` 两个抛出点：[src/llm_chat.ts:L2059-L2061](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2059-L2061)（getInputData 预载阶段）与 [src/llm_chat.ts:L853-L857](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L853-L857)（prefillStep 兜底）。
- `PrefillChunkSizeSmallerThanImageError` 唯一抛出点在分块器：[src/support.ts:L296-L304](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L296-L304)——注意判定只对**非数组**元素（图片）做，token 数组超长会走 L319 起的循环细切而不是抛错；对应的 precondition 注释在 [src/support.ts:L279-L284](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L279-L284)。图片「装不下当前块但没超限」时则独占新块：[src/support.ts:L338-L357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L338-L357)。

还要澄清一个容易误解的点——`prefillChunkSize` 从哪来：

- [src/llm_chat.ts:L352-L356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L352-L356)：`this.prefillChunkSize = metadata.prefill_chunk_size`——它**只**来自 wasm model library 的编译期 metadata（非正数抛 `MinValueError`），全仓库没有任何 ChatOptions/appConfig 路径能覆盖它（与 u3-l3 的结论一致）。想改变「块大小与图片大小的相对关系」，用户侧唯一的杠杆是**换图片分辨率**（或换用不同 `_cs` 后缀的 wasm）。

#### 4.4.4 代码实践

**实践目标**：真实触发一次 `PrefillChunkSizeSmallerThanImageError`，并解释它为什么不可避免。

**操作步骤**：

1. 先确认块大小：跑起 `examples/vision-model`（`logLevel: "INFO"`），从控制台 `"Using prefillChunkSize: "` 日志记下实际值（`_cs2k` wasm 预期为 2048，以日志为准）。
2. 规格任务里「把 prefill_chunk_size 设得比图片 token 数更小」无法直接做到——它固化在 wasm metadata 里（见 4.4.3）。**等价实验是反向操作：把图片 token 数变得比块大小更大**。准备一张正方形图片（如 1024×1024）：按 4.2 节公式，任何 1:1 图片都会 resize 到 1344×1344、crop 成 4×4、折算 2509 token > 2048。
3. 在 `vision_model.ts` 里加一个文件选择器，把本地图片读成 data URL 再发起请求（示例代码）：

   ```ts
   const fileInput = document.createElement("input");
   fileInput.type = "file";
   fileInput.accept = "image/*";
   fileInput.onchange = async () => {
     const file = fileInput.files![0];
     const reader = new FileReader();
     reader.onload = async () => {
       const messages: webllm.ChatCompletionMessageParam[] = [
         {
           role: "user",
           content: [
             { type: "text", text: "Describe this image." },
             { type: "image_url", image_url: { url: reader.result as string } },
           ],
         },
       ];
       try {
         await engine.chat.completions.create({ messages, stream: false });
       } catch (e) {
         console.error(e.name, e.message);
       }
     };
     reader.readAsDataURL(file); // 产出 "data:image/..." 前缀，满足 URL 校验
   };
   document.body.appendChild(fileInput);
   ```

4. 用正方形大图发起请求，捕获错误；再换一张横版小图（如 800×600，1921 token）对照。

**需要观察的现象**：正方形图片抛出 `PrefillChunkSizeSmallerThanImageError`，错误信息形如 `prefillChunkSize needs to be greater than imageEmbedSize ... Got prefillChunkSize: 2048, imageEmbedSize: 2509`（数值以日志为准）；横版小图正常出字。

**预期结果**：错误在分块阶段（图片已下载、尺寸已算出）抛出，早于任何 GPU 前向。解释要点：`image_embed` 核一次性产出整张图的 embedding，无法切成两半分两次 prefill（视觉编码需要全图上下文），所以单图超块只能显式失败，而不是静默降级。若本地 GPU/浏览器条件不允许运行，可退化为：用 4.2.4 的单测数值（2509 > 2048）做推理说明，并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：用户报告「同一张图片，在 A 模型上能用、换到 Phi-3.5-vision 上反而报 `PrefillChunkSizeSmallerThanImageError`」，可能的原因是什么？

**答案**：`prefillChunkSize` 是每个 wasm 的编译期属性（`_cs2k` = 2048），不同模型的 wasm 块大小不同；同时不同视觉模型的 embed size 公式不同（phi3_v 按分辨率动态算，可达 2509；固定 `mm_tokens_per_image` 的模型恒为小常数）。图片 token 数只在这一个模型上超过了它的块大小，就会只在这个模型上报错。排查顺序：看日志里的 `prefillChunkSize` → 手算该图 embedSize → 换小分辨率/横版图，或换 `_cs` 更大的 wasm。

**练习 2**：`CannotFindImageEmbedError` 与 `UserMessageContentErrorForNonVLM` 都是「文本模型收到图片」，为什么要有两个？

**答案**：拦截层级不同。`UserMessageContentErrorForNonVLM` 在协议层，依据是 ModelRecord 声明的 `model_type`，走 `engine.chatCompletion` 的公开 API 请求会被它拦下，零开销；`CannotFindImageEmbedError` 在管线层，依据是 wasm 是否真的导出 `image_embed` 核，用于兜底那些绕过协议校验直接驱动管线的低层调用（也防「声明是 VLM 但 wasm 编译时没带视觉核」的配置不一致）。

**练习 3**：如果把 `MultipleTextContentError` 的限制放开（允许一条消息多个 text part），管线层面还需要改哪里？

**答案**：`Conversation.getPromptArrayInternal` 的拍平逻辑（[src/conversation.ts:L127-L151](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L127-L151)）目前用单个 `textContentPart` 字符串聚合文本、遇到第二个 text part 抛错，需要改成把多个 text part 与图片按出现顺序交错排布；协议层的计数检查（chat_completion.ts L462-L471）与单测断言也要同步放开。这正说明该限制目前是「协议与编码共同维护」的约束。

## 5. 综合实践

把本讲三个模块串成一个完整任务——**「本地图片问答 + 调用链取证 + 越界实验」**：

1. **跑通示例**：进入 `examples/vision-model`，`npm install && npm start`。注意示例默认图片走 `cors-anywhere.herokuapp.com` 代理（[examples/vision-model/src/vision_model.ts:L14-L18](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/vision-model/src/vision_model.ts#L14-L18)），该公共服务常不可用——用 4.4.4 的文件选择器把一张**本地横版图片**读成 `data:image` base64 URL 替换，顺便体会两种合法 URL 前缀。
2. **取证**：按 4.3.4 的步骤产出调用链表格（环节 → 文件:行号），并记录控制台的 `prefillChunkSize`、第一轮 `usage.extra` 的 prefill 统计（估算图片 token 占比）、第二轮纯文本追问的 `prefill_tokens`（验证 KV cache 复用图像 embedding）。
3. **越界实验**：换正方形大图触发 `PrefillChunkSizeSmallerThanImageError`，截图错误信息，用 4.2.2 的公式手算 embedSize 与日志里的块大小对照。
4. **写作输出**：写一段 200 字左右的说明，解释「为什么图片不能像 token 序列一样分块」，必须引用 `getChunkedPrefillInputData` 的 precondition 与 `image_embed` 的原子性。

完成标志：调用链表格覆盖 8 个环节、两轮请求的 token 统计能对上「图片写入 KV 后被复用」的预期、越界错误的手算值与日志一致。

## 6. 本讲小结

- 多模态输入的入口是 user 消息的 `content` 数组（`text` part + `image_url` part），协议层 `postInitAndCheckFields` 用 `ModelType.VLM` 做门禁，另有 `detail`、URL 前缀、单 text part 三道结构校验；当前预置 VLM 仅 Phi-3.5-vision 两条记录。
- `Conversation` 把 content 数组拍平成「字符串与 `ImageURL` 混合」的 prompt 数组（phi3_v 在每张图后补 `\n`），管线 `getInputData` 预载全部图片算出 embedSize、插入 BOI/EOI token，并把图片 token 计入 prompt 长度与上下文窗口检查。
- 数据变换链：URL →（fetch + OffscreenCanvas）→ RGBA `ImageData` →（摘 Alpha）→ RGB → `[1,H,W,3]` NHWC 张量；`imageDataCache` 以 URL 为键在单次 prefill 内去重下载。
- phi3_v 的图片 token 数由分辨率算出（resize 到 336 的倍数、最多 16 个子图，公式 \( \text{subTokens} + 1 + 156 \)，正方形最多 2509）；固定 token 模型走 `mm_tokens_per_image`。缩放裁剪的实际执行在 `image_embed` GPU 核内，JS 只传目标形状并做 shape[0] 一致性断言。
- 图像 embedding 与文本 embedding `concatEmbeddings` 拼接后走同一条 prefill 前向，`filledKVCacheLength` 照常累加——图像从此驻留 KV cache，后续追问免重算；`image_embed` 是可选 ABI，缺失时纯文本照常工作。
- 多模态错误族六个类分三层：协议层四个（非法模型/字段/URL/多 text）、管线层 `CannotFindImageEmbedError`（兜底）、分块层 `PrefillChunkSizeSmallerThanImageError`（图片原子性，块大小固化在 wasm metadata、不可从 JS 改）。

## 7. 下一步学习建议

本讲结课后，单元三（推理管线）完整收官。建议两条路线：

1. **横向补充分发侧**：进入 [u4-l1](./u4-l1-cache-mechanism.md) 学习模型缓存机制——视觉模型的 wasm 与权重同样走 CacheStorage/OPFS 缓存，且 VLM 权重普遍更大（Phi-3.5-vision 约 4GB 显存需求），缓存与选型问题更突出；随后读 u4-l4 了解如何把自定义（包括视觉）模型接入 appConfig。
2. **纵向深挖协议层**：第 5 单元（u5-l1 起）的 Worker 架构是视觉示例的默认运行环境（`OffscreenCanvas` 正是为 worker 准备的）；第 6 单元的结构化输出（u6-l3）会讲到 GrammarMatcher——本讲 4.3 流程图中 `prefillStep` 里与 prefill 并行的 grammar 初始化，就是在那里展开。

源码延伸阅读：`src/support.ts` 的 `getChunkedPrefillInputData`（结合 L258-L276 的分块策略 TODO 注释，思考「贪心装块 vs 最少内核数」的取舍）、`tests/llm_chat_pipeline.test.ts` 的 VLM 尺寸计算用例、mlc-llm 仓库的 `serve/data.py`（WebLLM 的 embed size 公式与之对齐）。
