# u5-l2 WebWorkerMLCEngineHandler 源码解析

## 1. 本讲目标

上一讲（u5-l1）我们从「架构图」的视角理解了 Web Worker 代理模式：主线程的 `WebWorkerMLCEngine` 与 worker 线程里被 `WebWorkerMLCEngineHandler` 包裹的 `MLCEngine` 真身实现同一接口，线程间靠 `WorkerRequest{kind, uuid, content}` 信封通信。本讲我们钻进 worker 线程那一侧，逐行精读 Handler 的实现。学完后你应当能够：

1. 读懂 `onmessage` 如何按 `kind` 把 19 种消息路由到引擎方法，并能独立画出「请求字段 → 引擎方法 → 响应字段」的完整映射表。
2. 解释流式请求跨线程逐 chunk 回传的「拉模型」：`StreamInit` 建立生成器 + N 次 `NextChunk` 拉取，理解终止信号与多模型并发的实现。
3. 掌握 `CustomRequestParams` 自定义扩展点的真实用法——并明确指出：当前源码中**并不存在** `onrequest` 回调，扩展的实际做法是子类重写 `onmessage`。

## 2. 前置知识

本讲默认你已学完 u5-l1。再帮大家把几个会用到的底层概念热身一下：

- **结构化克隆（structured clone）**：浏览器 `postMessage` 传输数据时使用的序列化算法。普通对象、数组、Map 等可以克隆，但**函数、类实例的方法无法过线程**——这就是为什么跨线程只能传「数据信封」，而 `logitProcessorRegistry` 这类回调必须在 worker 脚本内注册。
- **`MessageEvent`**：worker 的 `onmessage` 收到的事件对象，真正的业务数据在 `event.data` 里。但 Handler 的 `onmessage` 也允许直接传入裸对象（测试里就是这么用的），所以入口处要做一次「拆包还是直用」的判断。
- **`AsyncGenerator.next()`**：调用一次、执行到下一个 `yield` 暂停并返回 `{value, done}`。生成器函数体是**惰性**的——不调 `next()` 就一行都不执行。这是理解流式拉模型的关键。
- **`event.waitUntil`**：Service Worker 专有 API，用于告诉浏览器「这条消息背后还有异步工作没做完，别回收我」。这解释了 Handler 的 `onmessage` 为什么多出 `onComplete`/`onError` 两个回调参数。
- **uuid 配对**：主线程为每个请求生成 `crypto.randomUUID()`，worker 回消息时原样带回；主线程用 `Map<uuid, resolver>` 把异步响应还原成 Promise。进度回调则是 `uuid: ""` 的广播。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/web_worker.ts` | Handler 与代理引擎的实现 | `WebWorkerMLCEngineHandler`（L61-378）是本讲主角；`WebWorkerMLCEngine.asyncGenerate`（L647-666）是流式拉模型的主线程侧 |
| `src/message.ts` | 线程间消息协议的类型定义 | 19 种 `RequestKind`、各种 `*Params` 结构、`CustomRequestParams` |
| `src/service_worker.ts` | Service Worker 子类 | `onComplete`/`onError` 如何接入 `waitUntil`、`keepAlive` 的特殊处理 |
| `src/support.ts` | 工具函数 | `getModelIdToUse`：多模型时如何定夺 `selectedModelId` |
| `src/utils.ts` | 工具函数 | `areArraysEqual`：`reloadIfUnmatched` 的比较基础 |
| `tests/web_worker_handler.test.ts` | Handler 单测（mock 引擎，无需 GPU） | 用测试断言验证我们对路由、流式、自愈的理解 |
| `examples/get-started-web-worker/src/worker.ts` | 最小 worker 脚本 | 3 行代码把 Handler 挂到 `self.onmessage` |

## 4. 核心概念与源码讲解

### 4.1 onmessage 路由：一条消息如何变成一次引擎调用

#### 4.1.1 概念说明

`WebWorkerMLCEngineHandler` 是 worker 线程里的「总机接线员」：主线程发来的每条 `WorkerRequest` 都落到它的 `onmessage`，由一个巨大的 `switch (msg.kind)` 决定转接给引擎的哪个方法、结果如何装进 `WorkerResponse` 发回去。

它解决的的问题是：线程边界上只有 `postMessage` 这一个窄门，而引擎接口有十几个方法。Handler 用「kind 字符串 → switch 分支 → 引擎方法」的翻译表，把窄门拓宽成了完整 API。

Handler 自身只维护三份状态，全部是引擎状态的「影子」或附属品：

- `modelId` / `chatOpts`：镜像记录后端引擎当前加载了什么（用于失配自愈）；
- `loadedModelIdToAsyncGenerator`：每个已加载模型各自的流式 chunk 生成器；
- `engine`：被包裹的 `MLCEngine` 真身（构造函数里自己 `new` 出来的，不从外部注入）。

#### 4.1.2 核心流程

一条消息的完整生命周期：

```
主线程 postMessage(WorkerRequest)
        │
        ▼
worker: self.onmessage = handler.onmessage     (由 worker 脚本挂接)
        │
        ▼
① 判断入参：MessageEvent 则取 event.data，否则当裸 WorkerRequest 用
        │
        ▼
② switch (msg.kind) 命中某个 case
        │
        ▼
③ 绝大多数 case 调 handleTask(uuid, async 闭包)：
     ├─ 闭包内：cast content 为对应 Params → 调 this.engine.xxx()
     ├─ 成功 → postMessage({kind:"return", uuid, content:结果})
     └─ 异常 → postMessage({kind:"throw", uuid, content:err.toString()})
        │
        ▼
主线程 onmessage 按 uuid 查 pendingPromise，resolve/reject 对应 Promise
```

三个不走 `handleTask` 的例外（同步直调、无 return/throw 响应）：`setLogLevel`、`setAppConfig`、`customRequest`。另有一个不在本类 switch 里的 kind：`keepAlive`——它会落入 `default` 分支，且因为消息里没有 `content` 字段而被当作「无关事件」静默忽略（Service Worker 子类会先拦截它再回复 `heartbeat`）。

自愈机制 `reloadIfUnmatched`：请求参数里携带的 `modelId`/`chatOpts` 是「主线程期望 worker 已加载的模型」。若 worker 曾被浏览器意外杀掉又重生，Handler 的镜像状态与期望不符，就在执行正事之前先静默 `reload`：

```
if (!areArraysEqual(this.modelId, 期望的 modelId)) {
    await this.engine.reload(期望的 modelId, 期望的 chatOpts);
}
```

#### 4.1.3 源码精读

**Handler 的三份状态**，注意 `engine` 是 public 的——这是后面 4.3 扩展点能成立的前提：

- [src/web_worker.ts:71-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L71-L79)：`modelId`/`chatOpts` 镜像字段、public 的 `engine`，以及按模型分桶的生成器 Map（注释说明 ChatCompletion 与 Completion 共用同一个 chunk 生成器）。
- [src/web_worker.ts:84-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L84-L98)：构造函数 `new MLCEngine()` 创建真身，并注册初始化进度回调——回调里把进度报告包装成 `uuid: ""` 的广播消息发出。这就是主线程 `initProgressCallback` 能跨线程工作的全部机关。
- [src/web_worker.ts:100-103](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L100-L103)：`postMessage` 直接转发全局 `postMessage`（即 DedicatedWorkerGlobalScope 的 API）。Service Worker 子类会重写它改走 `client.postMessage`。

**handleTask：统一的 try/catch 包装**：

- [src/web_worker.ts:111-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L111-L132)：执行任务，成功发 `return`，失败发 `throw`。注意 [L124](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L124) 的 `err.toString()`——错误跨线程会**退化为字符串**，自定义错误类的字段全部丢失（u5-l1 提过，这里就是发生地）。

**onmessage 入口拆包**：

- [src/web_worker.ts:139-145](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L139-L145)：`MessageEvent` 取 `event.data`，否则直接把参数当 `WorkerRequest`。这个「双形态入口」让同一份代码既能挂在真 worker 上，也能被单测直接喂裸对象调用。

**几个代表性 case**（完整映射表见下文表格）：

- [src/web_worker.ts:146-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L146-L156)：`reload` 分支——调 `engine.reload` 成功后**同步更新镜像字段** `this.modelId`/`this.chatOpts`。
- [src/web_worker.ts:171-181](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L171-L181)：`chatCompletionNonStreaming` 分支——先 `reloadIfUnmatched` 自愈，再调 `engine.chatCompletion`，把完整 `ChatCompletion` 作为 `return` 的 content。
- [src/web_worker.ts:282-296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L282-L296)：`unload` 分支——除了卸载引擎，还把镜像字段置 `undefined` 并清空生成器 Map。注释坦承：`reload()` 不会清生成器表，只有 `unload()` 清，Service Worker 场景可能跳过 `reload`，暂时保持现状。
- [src/web_worker.ts:331-347](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L331-L347)：三个「无响应」分支连在一起：`setLogLevel`（同时设置 worker 自身的 loglevel）、`setAppConfig`、以及 `customRequest` 的**空实现**（只调 `onComplete?.(null)`，是预留给子类的扩展点，见 4.3）。
- [src/web_worker.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L348-L356)：`default` 分支——有 `kind` 和 `content` 却不认识，先调 `onError` 再抛 `UnknownMessageKindError`；连 `content` 都没有的（比如 `keepAlive`）当作无关事件忽略。

**自愈逻辑**：

- [src/web_worker.ts:360-377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L360-L377)：`reloadIfUnmatched`。用 [src/utils.ts:4-12](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/utils.ts#L4-L12) 的 `areArraysEqual` 逐元素比较 `modelId` 数组，不等则打 warning 并重载。注释里的 TODO 说明 `chatOpts` 目前并未参与比较。

**onComplete/onError 参数的用途**——为 Service Worker 子类服务：

- [src/service_worker.ts:61-71](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L61-L71)：Service Worker 子类把 `onmessage` 包进 `message.waitUntil(new Promise((resolve, reject) => onmessage(message, resolve, reject)))`——`onComplete` 就是那个 `resolve`。这样每个 case 末尾的 `onComplete?.(null)` 实际是在告诉 Service Worker「这条消息处理完了」，防止浏览器在异步任务结束前回收 worker。这解释了基类 `onmessage` 签名里那两个可选回调的由来。

**19 种消息 kind 的完整映射表**（请求字段 → 引擎方法 → 响应 content）。类型定义见 [src/message.ts:18-37](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L18-L37)，各分支实现见 [src/web_worker.ts:145-357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L145-L357)：

| kind | 请求 content（Params） | 调用的引擎方法 | 响应 content |
| --- | --- | --- | --- |
| `reload` | `ReloadParams{modelId[], chatOpts?}` | `engine.reload(modelId, chatOpts)` | `null` |
| `forwardTokensAndSample` | `ForwardTokensAndSampleParams{inputIds, isPrefill, modelId?}` | `engine.forwardTokensAndSample(...)` | `number`（采样出的 token id） |
| `chatCompletionNonStreaming` | `ChatCompletionNonStreamingParams{request, modelId[], chatOpts?}` | 先 `reloadIfUnmatched` → `engine.chatCompletion(request)` | `ChatCompletion` |
| `chatCompletionStreamInit` | `ChatCompletionStreamInitParams{request, selectedModelId, modelId[], chatOpts?}` | 先 `reloadIfUnmatched` → `engine.chatCompletion(request)`（流式，返回生成器存入 Map） | `null` |
| `completionNonStreaming` | `CompletionNonStreamingParams{request, modelId[], chatOpts?}` | 先 `reloadIfUnmatched` → `engine.completion(request)` | `Completion` |
| `completionStreamInit` | `CompletionStreamInitParams{request, selectedModelId, modelId[], chatOpts?}` | 先 `reloadIfUnmatched` → `engine.completion(request)`（流式，返回生成器存入 Map） | `null` |
| `completionStreamNextChunk` | `CompletionStreamNextChunkParams{selectedModelId}` | `loadedModelIdToAsyncGenerator.get(id).next()` | `ChatCompletionChunk` / `Completion`；流结束时 `undefined` |
| `embedding` | `EmbeddingParams{request, modelId[], chatOpts?}` | 先 `reloadIfUnmatched` → `engine.embedding(request)` | `CreateEmbeddingResponse` |
| `runtimeStatsText` | `RuntimeStatsTextParams{modelId?}` | `engine.runtimeStatsText(modelId)` | `string` |
| `interruptGenerate` | `null` | `engine.interruptGenerate()` | `null` |
| `unload` | `null` | `engine.unload()`（并清镜像字段与生成器 Map） | `null` |
| `resetChat` | `ResetChatParams{keepStats, modelId?}` | `engine.resetChat(keepStats, modelId)` | `null` |
| `getMaxStorageBufferBindingSize` | `null` | `engine.getMaxStorageBufferBindingSize()` | `number` |
| `getGPUVendor` | `null` | `engine.getGPUVendor()` | `string` |
| `getMessage` | `GetMessageParams{modelId?}` | `engine.getMessage(modelId)` | `string` |
| `setLogLevel` | `LogLevel` 字符串 | `engine.setLogLevel` + worker 自身 `log.setLevel` | **无响应**（不走 handleTask） |
| `setAppConfig` | `AppConfig` | `engine.setAppConfig` | **无响应** |
| `customRequest` | `CustomRequestParams{requestName, requestMessage}` | **无（空实现，扩展点）** | **无响应** |
| `keepAlive` | 无 content | **本类不处理**，落入 default 被忽略 | 无（Service Worker 子类回 `heartbeat`，见 [src/service_worker.ts:98-106](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L98-L106)） |

**用测试验证理解**（这份测试 mock 掉了整个 `MLCEngine`，Node 环境即可运行，不需要 WebGPU）：

- [tests/web_worker_handler.test.ts:32-36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L32-L36)：`jest.mock("../src/engine")` 把 `MLCEngine` 替换成 mock 实例——证明 Handler 与引擎确实解耦，路由逻辑可以脱离 GPU 单测。
- [tests/web_worker_handler.test.ts:68-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L68-L91)：喂一条 `chatCompletionNonStreaming` 裸消息，断言 `reloadMock` 被以 `["demo"], []` 调用、`postMessage` 收到 `{kind:"return", uuid:"task-1", ...}`——精确复现了上面流程图的①②③。
- [tests/web_worker_handler.test.ts:158-167](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L158-L167)：`reloadIfUnmatched(["b"])` 触发 reload、`reloadIfUnmatched(["same"])` 不触发——自愈逻辑的最小验证。
- [tests/web_worker_handler.test.ts:169-176](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L169-L176)：未知 kind 断言抛 `UnknownMessageKindError` 且 `onError` 被调用。
- [tests/web_worker_handler.test.ts:245-259](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L245-L259)：任务 reject 时 `postMessage` 收到 `{kind:"throw", uuid:"fail", ...}`。

#### 4.1.4 代码实践：验证映射表

1. **实践目标**：亲手核对上表，而不是背下来——用检索证明每个 kind 确实路由到了对应的引擎方法。
2. **操作步骤**：
   - 在仓库根目录运行 `npx jest tests/web_worker_handler.test.ts`（等价于 `npm test` 的子集；该文件全程 mock 引擎，无需浏览器与 GPU）。
   - 用编辑器打开 `src/web_worker.ts`，搜索 `case "`，统计 switch 分支数，与 [src/message.ts:18-37](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L18-L37) 中 `RequestKind` 的成员数（19 个）对照，确认 `keepAlive` 确实没有分支。
   - 把上表复制到自己的笔记里，为每一行补上 `case` 的起止行号（例如 `chatCompletionNonStreaming` → L171-181）。
3. **需要观察的现象**：jest 输出中 `web_worker_handler.test.ts` 全部用例通过（本讲引用的几条断言分别对应上面列出的测试名）。
4. **预期结果**：switch 分支数 = 18（19 种 kind 减去 `keepAlive`），其中 15 个走 `handleTask`、3 个同步直调。若与你数出的不符，回到 [src/web_worker.ts:145-357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L145-L357) 逐个核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `setLogLevel` 和 `setAppConfig` 不走 `handleTask`、也不回 `return` 消息？主线程对应方法（[src/web_worker.ts:477-494](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L477-L494)）是怎么配合的？

**答案**：这两个操作是同步、必然成功、调用方不需要结果的配置类动作。worker 侧直接调用引擎 setter 不包装 `handleTask`；主线程侧 `setAppConfig`/`setLogLevel` 也只是 `worker.postMessage(msg)` 而不经过 `getPromise`，即「发后即忘」（fire-and-forget），省掉一次消息往返和一个 pending 的 Promise。

**练习 2**：如果主线程发来 `{kind: "keepAlive", uuid: "x"}`（没有 content 字段），`WebWorkerMLCEngineHandler.onmessage` 会发生什么？在 Service Worker 场景下又是什么行为？

**答案**：Web Worker Handler 的 switch 没有 `keepAlive` 分支，落入 `default`；由于 `msg.content` 为 `undefined`（falsy），`if (msg.kind && msg.content)` 不成立，走「忽略无关事件」分支只调 `onComplete?.(null)`，不抛错也不回消息。Service Worker 场景下，子类在 `super.onmessage` 之前拦截（[src/service_worker.ts:98-106](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L98-L106)），回复 `{kind:"heartbeat", uuid}`，主线程借此重置 `missedHeartbeat` 计数。

**练习 3**：`reloadIfUnmatched` 为什么只在部分请求（chat/completion/embedding）里被调用，而 `resetChat`、`getMessage` 等没有？

**答案**：`chatCompletionNonStreaming` 等业务请求的 Params 里携带了 `modelId`/`chatOpts`（「主线程期望后端已加载的模型」，见 [src/message.ts:62-67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L62-L67) 的注释），因此有机会做失配检测与自愈；而 `resetChat`/`getMessage` 的 Params 只有可选的定位用 `modelId`，没有期望清单，无从比较。若这些请求在引擎空载时到达，会在引擎内部因模型未加载而抛错，经 `handleTask` 变成 `throw` 消息传回主线程。

### 4.2 流式请求如何跨线程逐 chunk 回传

#### 4.2.1 概念说明

u2-l3 讲过主线程引擎的流式输出：`AsyncIterable<ChatCompletionChunk>`。到了 Worker 架构下，困难在于——**生成器活在 worker 线程，`for await` 循环却在主线程**，而生成器本身（一个带闭包的异步状态机）无法被结构化克隆。

WebLLM 的解法是把「推」改成「拉」：worker 里的生成器原地不动，主线程每想要一个 chunk，就发一条 `completionStreamNextChunk` 消息过去，worker 代为调用一次 `generator.next()` 并把产出的 chunk 寄回来。拉模型天然自带**流控/背压**（backpressure）：消费方处理多快，生产方就跑多快，chunk 不会在消息队列里积压。

多模型并发是这套设计的第二个要点：一个引擎可加载多个模型（u2-l1），每个模型各有一条独立的流式生成器，因此 worker 侧用 `Map<selectedModelId, AsyncGenerator>` 分桶保管，请求里用 `selectedModelId` 指明拉哪一条。

#### 4.2.2 核心流程

一次流式 `chatCompletion` 的时序（completion 流完全同构，共用同一协议）：

```
主线程 WebWorkerMLCEngine                     worker 线程 Handler
──────────────────────────                    ───────────────────
chatCompletion(request{stream:true})
 ① getModelIdToUse(...) 算出 selectedModelId
 ② postMessage(chatCompletionStreamInit)
                                          ③ reloadIfUnmatched 自愈
                                          ④ engine.chatCompletion(request)
                                             → 返回惰性 AsyncGenerator
                                             → 存入 Map[selectedModelId]
        ◀──────── return(null) ────────
 ⑤ 返回 this.asyncGenerate(selectedModelId)
    给调用方（for await 开始驱动它）
 ┌─ 循环 ────────────────────────────────────────────────────┐
 │ ⑥ postMessage(completionStreamNextChunk)                  │
 │                                          ⑦ generator.next() │
 │                                             → 跑一步 prefill/decode，产出 chunk
        ◀──────── return(chunk) ───────                          │
 │ ⑧ typeof ret === "object" → yield ret 给页面               │
 │    …每个 chunk 重复 ⑥⑦⑧…                                   │
 │ ⑥ postMessage(completionStreamNextChunk)                  │
 │                                          ⑦ generator.next() │
 │                                             → done:true, value:undefined
        ◀────── return(undefined) ──────                        │
 │ ⑧ typeof ret !== "object" → break，流结束                   │
 └────────────────────────────────────────────────────────────┘
```

关键设计点：

- **Init 不生成任何 token**。生成器函数体是惰性的，`engine.chatCompletion` 只是「创建」了它（并按 u2-l3 所述在引擎入口拿到模型互斥锁），真正的 prefill/decode 由后续每次 `next()` 驱动一步。
- **终止信号是 `undefined`**。worker 侧 `next()` 走到生成器末尾时 `value` 为 `undefined`；主线程用 `typeof ret !== "object"` 判定流结束。所以协议里不需要额外的 "end of stream" 消息类型。
- **消息数与 chunk 数线性相关**：一次流式请求的跨线程往返约为 \( 2 \times (N_{\text{chunk}} + 1) \) 条（1 次 Init 往返 + 每 chunk 一次往返）。每个 token 一次跨线程往返是拉模型的固有开销，换来的是背压与多模型并发。
- **中断不受影响**：`interruptGenerate` 是独立的 kind 和 uuid，随时可以插进 NextChunk 序列之间；worker 的事件循环在等待 `next()` 期间仍能接收新消息。

#### 4.2.3 源码精读

**主线程侧：发起流式请求**（`chatCompletion` 的流式分支）：

- [src/web_worker.ts:680-690](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L680-L690)：先检查 `modelId` 已加载（否则抛 `WorkerEngineModelNotLoadedError`），再用 `getModelIdToUse` 定夺 `selectedModelId`。该函数（[src/support.ts:227-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L256)）的裁决规则：没加载模型抛 `ModelNotLoadedError`；请求指定了 `model` 但不在已加载列表抛 `SpecifiedModelNotFoundError`；加载了多个又没指定抛 `UnclearModelToUseError`；否则用唯一/指定的那个。
- [src/web_worker.ts:692-712](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L692-L712)：`stream: true` 分支——先发 `chatCompletionStreamInit`（携带 request、selectedModelId、期望的 modelId/chatOpts），await 其 `return(null)` 确认生成器已就位，然后把 `this.asyncGenerate(selectedModelId)` 作为流返回给调用方。注释写明：因为 handler 可能同时维护多条生成器，必须用 selectedModelId 指明实例化/推进哪一条。

**主线程侧：拉取循环**：

- [src/web_worker.ts:647-666](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L647-L666)：`asyncGenerate` 是一个 `while (true)` 的异步生成器——每轮构造一条 `completionStreamNextChunk` 消息，`await this.getPromise(...)` 等待 worker 寄回的 chunk；`typeof ret !== "object"`（即 `undefined`）就 `break`，否则 `yield ret`。函数头部的 doc 注释就是这套拉模型的官方说明：最后一条消息是 `void` 表示生成器再无内容可产出。

**worker 侧：Init 分支**：

- [src/web_worker.ts:182-200](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L182-L200)：`chatCompletionStreamInit`——`reloadIfUnmatched` 后调用 `engine.chatCompletion(params.request)`，把返回值 cast 成 `AsyncGenerator` 并 `set` 进 `loadedModelIdToAsyncGenerator`。注意此刻**一个 token 都没算**，`return(null)` 立即发出（发起一次流式请求的握手成本很低）。

**worker 侧：NextChunk 分支**：

- [src/web_worker.ts:233-252](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L233-L252)：按 `selectedModelId` 从 Map 取生成器；取不到直接抛内部错误（说明 Init 被跳过了）；否则 `await curGenerator.next()` 把 `value` 作为 `return` 的 content 寄回。注释强调 Chat 与 Completion 共用此通道。

**completion 的同构实现**：

- [src/web_worker.ts:213-231](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L213-L231)：`completionStreamInit` 与 chat 版逐行对称，只是调 `engine.completion`、产出 `Completion` chunk。两套 Init 共用一张 Map 与同一个 `completionStreamNextChunk` kind。

**主线程侧：响应配对**：

- [src/web_worker.ts:496-520](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L496-L520)：`getPromise`——先在 `pendingPromise` Map 里以 uuid 注册回调，再 `postMessage`（注册先于发送，因此不会错过响应）；回调按 `kind` 决定 `resolve`（return）或 `reject`（throw）。
- [src/web_worker.ts:804-841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L804-L841)：主线程 `onmessage`——`return`/`throw` 按 uuid 查表、删除、触发回调；`initProgressCallback` 是无 uuid 的广播通道。

**测试佐证**：

- [tests/web_worker_handler.test.ts:93-118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L93-L118)：mock 引擎的 `chatCompletion` 返回一个手写生成器，断言 Init 之后 `loadedModelIdToAsyncGenerator.get("demo")` 已存在。
- [tests/web_worker_handler.test.ts:261-278](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L261-L278)：预先塞入一个只 yield 一个 chunk 的生成器，发 `completionStreamNextChunk`，断言 `onComplete` 收到 `{object: "chunk"}`——正是时序图第 ⑦ 步。
- [tests/web_worker_handler.test.ts:189-217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L189-L217)：`MockWorker` 反向模拟了 worker（收到消息后异步回 `{kind:"return", uuid, content}`），说明「uuid 配对」这一机制两侧对称。

#### 4.2.4 代码实践：亲眼看到消息序列

1. **实践目标**：在一次真实流式对话中，观察主线程与 worker 之间的消息序列（1 次 Init + N 次 NextChunk）。
2. **操作步骤**：
   - 复制 `examples/get-started-web-worker` 为自己的实验目录（示例代码属于你，可自由修改；不要改 `src/`）。
   - 修改实验目录里的 `worker.ts`（原版只有 [examples/get-started-web-worker/src/worker.ts:1-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/worker.ts#L1-L7) 三行），把挂接处包一层日志（示例代码）：
     ```ts
     import { WebWorkerMLCEngineHandler } from "@mlc-ai/web-llm";

     const handler = new WebWorkerMLCEngineHandler();
     self.onmessage = (msg: MessageEvent) => {
       const req = msg.data as { kind: string; uuid: string };
       console.log(`[worker <- main] kind=${req.kind} uuid=${req.uuid.slice(0, 8)}`);
       handler.onmessage(msg);
     };
     ```
   - 按 `examples/get-started-web-worker/README.md` 启动（`npm install` 后在示例目录 `npm start`），在支持 WebGPU 的浏览器中打开，跑 `mainStreaming` 演示（[examples/get-started-web-worker/src/main.ts:55-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/main.ts#L55-L98)）。
3. **需要观察的现象**：worker 控制台先打出一条 `kind=chatCompletionStreamInit`，随后是长串 `kind=completionStreamNextChunk`（每条 uuid 都不同），chunk 数与页面上打字机字数增长同步；若中途点停止（调用 `engine.interruptGenerate()`），会看到一条 `kind=interruptGenerate` 插在 NextChunk 序列中间。
4. **预期结果**：`chatCompletionStreamInit` 恰好出现 1 次，`completionStreamNextChunk` 次数 ≈ 收到的 chunk 数 + 1（最后一条拿到 `undefined` 结束流）。若本地没有 WebGPU 环境，此观察步骤**待本地验证**；作为替代，可直接阅读 [tests/web_worker_handler.test.ts:261-278](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L261-L278) 的断言推演同样结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把整个生成器「寄」给主线程，而要每 chunk 一次消息往返？

**答案**：生成器是携带闭包与执行状态的异步状态机，无法被结构化克隆跨线程传递；能传的只有纯数据（chunk 本身）。WebLLM 因此让生成器留在 worker，主线程用 NextChunk 消息「远程调用」`next()`。代价是每 chunk 一次往返的延迟，收益是天然背压（消费速率控制生产速率）与多模型并发（Map 按 selectedModelId 分桶，多条流互不干扰）。

**练习 2**：流式请求中某一步 decode 抛出了异常（例如上下文超窗），这个错误如何到达主线程的 `for await`？

**答案**：该步发生在 worker 侧某次 `completionStreamNextChunk` 的 `handleTask` 闭包内（[src/web_worker.ts:236-250](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L236-L250)），`handleTask` 捕获后把它 `toString()` 并以 `{kind:"throw", uuid}` 寄回；主线程 `onmessage` 的 throw 分支按 uuid reject 掉 `getPromise` 返回的 Promise（[src/web_worker.ts:827-835](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L827-L835)）；`asyncGenerate` 里 `await` 抛出，异步生成器向消费方传播异常，`for await` 的 catch 捕获到的是**字符串**而非错误对象。

**练习 3**：`chatCompletionStreamInit` 的 Params 里为什么同时有 `selectedModelId`（单个字符串）和 `modelId`（字符串数组）两个字段？

**答案**：`modelId[]` 是「期望 worker 已加载的全部模型清单」，供 `reloadIfUnmatched` 做失配自愈（[src/message.ts:69-72](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L69-L72) 注释）；`selectedModelId` 是「本次请求实际使用的模型」，用作生成器 Map 的键，因为 handler 要为多模型各自维护生成器。一个管「加载状态对齐」，一个管「流身份定位」。

### 4.3 CustomRequestParams 自定义扩展点

#### 4.3.1 概念说明

`customRequest` 是消息协议里预留的自定义通道：`CustomRequestParams` 只有 `requestName`（请求名）和 `requestMessage`（字符串载荷）两个字段，并且**已从库入口公开导出**（[src/index.ts:49](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L49)），说明它是给库使用者用的。

必须先澄清一个事实：**当前 HEAD 的源码中不存在 `onrequest` 回调**。用 `customRequest`、`CustomRequestParams`、`onrequest` 三个关键词检索 `src/` 全目录，命中只有三处——`message.ts` 的类型定义、`web_worker.ts:344` 的空 case、`index.ts` 的导出。基类 Handler 对 `customRequest` 的全部实现是：

```ts
case "customRequest": {
  onComplete?.(null);
  return;
}
```

也就是说基类把它当作「承认存在但不处理」的占位符。真正的扩展方式是**子类重写 `onmessage`**：在把消息交给 `super.onmessage` 之前，拦截 `customRequest` kind，按 `requestName` 分发到你自己的逻辑，并复用基类的 `handleTask` 获得 return/throw 的标准信封。这依赖两个前提，源码都满足：`this.engine` 是 public 的（可直接调引擎），`handleTask` 与 `postMessage` 也是 public 的（可直接复用发送协议）。

#### 4.3.2 核心流程

自定义一条跨线程请求需要两端各写一个子类：

```
主线程                                     worker 线程
MyEngine extends WebWorkerMLCEngine      MyHandler extends WebWorkerMLCEngineHandler
  myQuery() {                               onmessage(event) {
    postMessage({                             msg = 拆包(event)
      kind: "customRequest",                  if (msg.kind === "customRequest"
      uuid: randomUUID(),                         && params.requestName === "myQuery") {
      content: {                                this.handleTask(msg.uuid, async () => {
        requestName: "myQuery",                   // 自定义逻辑，可访问 this.engine
        requestMessage: "...",                    return 结果;
      },                                        });
    })  // 复用 protected getPromise            return;  // 不再交给 super
    await ...                                }
  }                                           super.onmessage(event);  // 其余 kind 照旧
                                            }
```

设计要点：

- **协议兼容**：自定义消息仍走 `WorkerRequest{kind, uuid, content}` 信封，主线程侧复用 `getPromise`（`protected`，子类可用）即自动获得 uuid 配对与 Promise 化。
- **分发命名空间**：`requestName` 充当自定义子协议的方法名，`requestMessage` 是字符串载荷（复杂结构需自行 `JSON.stringify`/`parse`）。
- **不侵入基类**：所有 kind 之外的请求都走 `customRequest` 一条通道，基类的 18 个 case 一行不改。

#### 4.3.3 源码精读

- [src/message.ts:104-107](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L104-L107)：`CustomRequestParams` 接口——仅 `requestName` 与 `requestMessage` 两个字符串字段。
- [src/web_worker.ts:344-347](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L344-L347)：基类的空实现——只调 `onComplete?.(null)` 便返回，既不调引擎也不回 `return`。这就是「占位扩展点」的准确含义。
- [src/index.ts:49](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L49)：`CustomRequestParams` 与 `WorkerRequest`/`WorkerResponse` 一起公开导出——库作者预期用户在自己的 worker 脚本与主线程代码里操纵这些类型。
- [src/web_worker.ts:74](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L74) 与 [src/web_worker.ts:111](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L111)：`public engine` 与 public 的 `handleTask`——子类扩展可用的两块基石。
- [src/web_worker.ts:496-520](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L496-L520)：`getPromise` 是 `protected`（[L496](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L496)）——主线程子类可直接复用它把任意 `WorkerRequest` 变成 Promise，且注册回调先于 `postMessage`，不会错过响应。
- 对照参考——Service Worker 子类示范了「重写 onmessage + 特判 + 委托 super」的标准姿态：[src/service_worker.ts:87-148](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L87-L148) 先特判 `keepAlive` 与 `reload`，其余消息交给 `super.onmessage(msg, onComplete, onError)`。自定义 `customRequest` 分发应当模仿这个结构。

另附一个与扩展点相关的提醒：实践中想查解码速度，引擎本来就有内置通道——主线程 `engine.runtimeStatsText()` 会发 `runtimeStatsText` kind（[src/web_worker.ts:576-585](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L576-L585)），worker 侧路由到 `engine.runtimeStatsText`（[src/web_worker.ts:265-273](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L265-L273)）。但该方法会打弃用警告，官方推荐改用 `usage` / `stream_options.include_usage`（[src/engine.ts:1315-1321](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1315-L1321)）。综合实践里我们会两条路都走一遍：内置 kind 验证功能，`customRequest` 演练扩展机制。

#### 4.3.4 代码实践：用 customRequest 实现跨线程查询（本讲核心实践之一）

1. **实践目标**：不改库源码，通过「子类 Handler + 子类 Engine」给 Worker 架构添加一个自定义的跨线程查询方法 `myRuntimeStats()`。
2. **操作步骤**：
   - **worker 侧**：在自己的实验工程里新建 `my-worker.ts`（示例代码，基于 [examples/get-started-web-worker/src/worker.ts:1-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/worker.ts#L1-L7) 扩展）：
     ```ts
     // 示例代码：自定义 handler，拦截 customRequest
     import { WebWorkerMLCEngineHandler } from "@mlc-ai/web-llm";
     import type { CustomRequestParams, WorkerRequest } from "@mlc-ai/web-llm";

     class MyHandler extends WebWorkerMLCEngineHandler {
       onmessage(event: any, onComplete?: (v: any) => void, onError?: () => void) {
         const msg: WorkerRequest = (
           event instanceof MessageEvent ? event.data : event
         ) as WorkerRequest;
         if (msg.kind === "customRequest") {
           const params = msg.content as CustomRequestParams;
           if (params.requestName === "myRuntimeStats") {
             // 复用 handleTask：自动获得 return/throw 信封与 uuid 配对
             this.handleTask(msg.uuid, async () => {
               return await this.engine.runtimeStatsText(params.requestMessage || undefined);
             });
             return; // 已处理，不再交给 super
           }
         }
         super.onmessage(event, onComplete, onError); // 其余 kind 走原有路由
       }
     }

     const handler = new MyHandler();
     self.onmessage = (msg: MessageEvent) => handler.onmessage(msg);
     ```
   - **主线程侧**：新建 `my-engine.ts`（示例代码）：
     ```ts
     // 示例代码：自定义主线程引擎，新增跨线程方法
     import { WebWorkerMLCEngine } from "@mlc-ai/web-llm";
     import type { WorkerRequest } from "@mlc-ai/web-llm";

     export class MyEngine extends WebWorkerMLCEngine {
       async myRuntimeStats(modelId?: string): Promise<string> {
         const msg: WorkerRequest = {
           kind: "customRequest",
           uuid: crypto.randomUUID(),
           content: { requestName: "myRuntimeStats", requestMessage: modelId ?? "" },
         };
         return await this.getPromise<string>(msg); // protected，子类可用
       }
     }
     ```
   - 页面中使用（注意 `CreateWebWorkerMLCEngine` 返回的是基类，自定义引擎要手动 new + reload）：
     ```ts
     // 示例代码
     const engine = new MyEngine(
       new Worker(new URL("./my-worker.ts", import.meta.url), { type: "module" }),
     );
     await engine.reload("Llama-3.1-8B-Instruct-q4f32_1-MLC");
     // ...发起一次对话后：
     console.log(await engine.myRuntimeStats());
     ```
3. **需要观察的现象**：点击查询按钮后，worker 控制台（若沿用 4.2.4 的日志）打出 `kind=customRequest`；主线程拿到一串形如 `prefill tokens per sec: ...\ndecode tokens per sec: ...` 的统计文本并显示在页面。
4. **预期结果**：`myRuntimeStats()` 的返回值与内置 `engine.runtimeStatsText()` 完全一致（两者最终都调 worker 内 `this.engine.runtimeStatsText()`）；区别只在于一个走预留的 `customRequest` 通道、一个走内置 kind。浏览器中的运行效果**待本地验证**（需要 WebGPU 环境）；子类编译正确性可先用 `npx tsc --noEmit`（在本仓库 tsconfig 下）或直接构建实验工程验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `customRequest` 消息发给**未被子类化**的默认 Handler，会发生什么？

**答案**：命中 [src/web_worker.ts:344-347](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L344-L347) 的空 case：只调用 `onComplete?.(null)` 然后返回，**不会**发任何 `return`/`throw` 消息。主线程若用 `getPromise` 等待，该 Promise 将永远 pending（没有 uuid 配对的响应到来）——这也是排查「自定义请求挂起不动」时的第一怀疑点。

**练习 2**：为什么 `CustomRequestParams.requestMessage` 只设计成 `string`？要传结构化数据怎么办？

**答案**：见 [src/message.ts:104-107](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L104-L107)，这是协议作者选择的最小公共形态——字符串最通用、必然可结构化克隆。传结构化数据时在发送侧 `JSON.stringify`、在 worker 侧 `JSON.parse` 即可；不要试图改成任意对象，因为 `MessageContent` 联合类型对 content 的形态有约束，且保持简单载荷能避免克隆失败的风险。

**练习 3**：本讲标题里提到的 `onrequest` 回调，在源码里存在吗？正确的扩展方式是什么？

**答案**：不存在。检索 `src/` 只有 `customRequest`（空 case）、`CustomRequestParams`（类型）与 `index.ts` 导出三处命中，没有任何名为 `onrequest` 的 API。正确的扩展方式是：worker 侧继承 `WebWorkerMLCEngineHandler` 重写 `onmessage`，在 `super.onmessage` 之前拦截 `customRequest` 并按 `requestName` 自行分发（可复用 public 的 `handleTask` 与 `this.engine`）；主线程侧继承 `WebWorkerMLCEngine`，用 protected 的 `getPromise` 发送自定义消息。

## 5. 综合实践

**任务：做一个「Worker 内运行状态面板」页面**，把本讲三个模块串起来。

需求：

1. 基于 `examples/get-started-web-worker` 搭建页面：加载模型、流式对话（打字机效果）。
2. 页面上有一个「查询解码速度」按钮，**同时**走两条通道并对比结果：
   - 通道 A（内置）：`engine.runtimeStatsText()`；
   - 通道 B（自定义）：4.3.4 的 `MyEngine.myRuntimeStats()`，经 `customRequest` 子协议。
3. 页面有一个「停止」按钮调用 `engine.interruptGenerate()`，验证中断消息能插进 NextChunk 流。

实现骨架（示例代码，细节可直接沿用 4.3.4 的两个子类）：

```ts
// 示例代码：页面主逻辑
const engine = new MyEngine(
  new Worker(new URL("./my-worker.ts", import.meta.url), { type: "module" }),
  { initProgressCallback: (r) => setLabel("init-label", r.text) },
);
await engine.reload("Llama-3.1-8B-Instruct-q4f32_1-MLC");

// 流式对话
const stream = await engine.chatCompletion({
  stream: true,
  messages: [{ role: "user", content: "用一百字介绍 WebGPU" }],
});
for await (const chunk of stream) {
  appendText(chunk.choices[0]?.delta?.content || "");
}

// 查询按钮：两条通道对比
btnStats.onclick = async () => {
  const viaBuiltin = await engine.runtimeStatsText();      // runtimeStatsText kind
  const viaCustom = await engine.myRuntimeStats();          // customRequest kind
  setLabel("stats-label", `内置通道:\n${viaBuiltin}\n自定义通道:\n${viaCustom}`);
};
```

验收清单：

- [ ] 4.1 的映射表已亲手核对并补上行号（4.1.4 实践产物）。
- [ ] 4.2 的消息序列日志显示 1 次 Init + N 次 NextChunk +（点击停止时）1 次 interruptGenerate。
- [ ] 综合页面中两条通道返回的统计文本一致，证明 `customRequest` 子协议与内置 kind 殊途同归。
- [ ] （加分）把统计改用 `stream_options: { include_usage: true }` 从最后一个 chunk 的 `usage.extra` 读取，体会 `runtimeStatsText` 的弃用警告所言非虚。

注：页面运行需要支持 WebGPU 的浏览器（Chrome/Edge 113+），本地无 GPU 环境时相应观察点标注「待本地验证」；4.1.4 的 jest 部分在任何 Node 环境均可完成。

## 6. 本讲小结

- `onmessage` 是一个 19-kind 的路由表（本类实现 18 个分支），绝大多数分支经 `handleTask` 包装：成功发 `return`、异常 `toString()` 后发 `throw`；`setLogLevel`/`setAppConfig`/`customRequest` 是无响应的例外，`keepAlive` 由 Service Worker 子类处理。
- Handler 只持有「影子状态」：`modelId`/`chatOpts` 镜像用于 `reloadIfUnmatched` 失配自愈（worker 被杀重生的兜底），生成器 Map 按 `selectedModelId` 为每个模型保管独立的流。
- 流式输出是拉模型：`StreamInit` 在 worker 内创建惰性生成器（不算任何 token），主线程 `asyncGenerate` 循环发 `NextChunk` 逐个拉取 chunk；`undefined`（非对象）是流结束信号，每 chunk 一次往返换来背压与多模型并发。
- `customRequest` 是协议预留但基类空实现的扩展点；源码中**没有** `onrequest` 回调，正确做法是子类重写 `onmessage` 拦截分发（复用 public 的 `handleTask`/`engine`），主线程侧子类复用 protected 的 `getPromise` 发消息。
- `onmessage` 的 `onComplete`/`onError` 参数是为 Service Worker 子类的 `event.waitUntil` 预留的桥接，体现了基类设计对扩展环境的 anticipating。
- `tests/web_worker_handler.test.ts` 用 `jest.mock` 替换整个 `MLCEngine`，证明 Handler 路由逻辑与 GPU 完全解耦、可在纯 Node 环境单测。

## 7. 下一步学习建议

下一讲（u5-l3）把视线移到 `src/service_worker.ts` 与 `src/extension_service_worker.ts`：ServiceWorkerMLCEngineHandler 如何继承本讲的 Handler、用 `clientRegistry` 把响应路由回正确的页面、用心跳消息对抗 Service Worker 的生命周期回收，以及 Chrome MV3 扩展如何经 `chrome.runtime.Port` 复用同一套协议。建议先行阅读 [src/service_worker.ts:38-149](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L38-L149)，重点体会它对本讲 `onmessage`/`postMessage` 两个方法的重写如何只用几十行就完成了运行环境的整体切换。之后再进入单元六，回到协议层深挖 OpenAI 兼容 API 的校验与结构化输出。
