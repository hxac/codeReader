# 第五单元第 1 讲：Web Worker 架构与消息协议

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「为什么要把推理放进 Web Worker」：主线程版引擎在解码期间会阻塞 UI，Worker 版让主线程只负责界面。
2. 掌握 `CreateWebWorkerMLCEngine` 的三参数用法（`worker`、`modelId`、`engineConfig`），并能把自己的页面从主线程引擎无痛迁移到 Worker 引擎。
3. 读懂 `src/message.ts` 定义的 `WorkerRequest` / `WorkerResponse` 协议：`kind + uuid + content` 三字段信封、19 种请求类型、3+1 种响应类型，以及流式请求为什么被拆成 `StreamInit` + `NextChunk` 两条消息。
4. 理解 `ChatWorker` 这个只有两个字段的最小抽象为什么是整个 Worker 架构（包括下一讲的 Service Worker）能够复用的关键。

## 2. 前置知识

在进入源码之前，先补几个本讲要用到的浏览器基础知识。

**浏览器主线程是独木桥。** 一个页面只有一个主线程，它要轮流做：执行你的 JavaScript、处理事件回调、计算样式、排版布局、绘制。只要有一段同步 JS 跑得很久（所谓「长任务」，long task），渲染和交互就得排队。回忆第三单元：`decodeStep` 每生成一个 token 就要做一次 GPU 前向，外加采样、统计等 CPU 侧逻辑——如果引擎跑在主线程，生成几百个 token 就意味着主线程被连续占用数秒到数十秒，页面上任何动画、按钮都会卡死。

**Web Worker 是浏览器提供的第二条线程。** 用 `new Worker(url)` 可以启动一个独立线程，它有自己的事件循环、自己的全局对象（`self`），**不能访问 DOM**。两个线程之间没有共享内存，唯一的主流通信方式是 `postMessage(message)`：消息内容会被「结构化克隆」（structured clone）——即在发送端深拷贝序列化、在接收端反序列化。这带来一个重要后果：**函数、类实例、`Error` 对象都不能原样过桥**，`Error` 跨线程后通常会退化成字符串。本讲源码里会有一个专门的细节讲这一点。

**Module Worker。** WebLLM 的 worker 脚本用 `new Worker(new URL(...), { type: "module" })` 创建，表示 worker 以 ES Module 方式加载，可以直接 `import` npm 包。

**回顾本手册已讲过的内容。** u2-l1 介绍了 `MLCEngineInterface` 是 `MLCEngine` 与 `WebWorkerMLCEngine` 共同实现的静态契约；u2-l3 介绍了流式接口 `AsyncIterable<ChatCompletionChunk>` 与协作式中断 `interruptGenerate`；u3-l5 提到过 `LogitProcessor` 在 Worker 模式下必须在 worker 脚本内注册——本讲会看到源码中对应的警告。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/message.ts` | 定义两个线程之间的消息协议 | `RequestKind`、`WorkerRequest`、`WorkerResponse` 及各种 `*Params` |
| `src/web_worker.ts` | Worker 架构的全部实现 | 上半部分 `WebWorkerMLCEngineHandler`（worker 侧真身），下半部分 `WebWorkerMLCEngine` 与 `CreateWebWorkerMLCEngine`（主线程侧代理）、`ChatWorker` 抽象 |
| `src/types.ts` | 引擎公共接口契约 | `MLCEngineInterface`（两个引擎类共同实现的目标类型） |
| `src/error.ts` | 错误类 | `WorkerEngineModelNotLoadedError`、`UnknownMessageKindError` |
| `src/support.ts` / `src/utils.ts` | 工具函数 | `getModelIdToUse`（多模型下选定模型）、`areArraysEqual` |
| `examples/get-started-web-worker/` | 官方 Worker 最小示例 | `src/main.ts`（主线程用法）、`src/worker.ts`（worker 侧 7 行代码） |
| `tests/web_worker_handler.test.ts` | Handler 的单元测试 | 不启动真实 worker、mock 掉 `MLCEngine` 来测消息路由 |

## 4. 核心概念与源码讲解

### 4.1 代理模式总览：为什么以及如何把推理搬进 Worker

#### 4.1.1 概念说明

WebLLM 的 Worker 架构用的是典型的**代理模式（Proxy Pattern）**：

- **真身**：`MLCEngine`（第二单元精读过），它持有 `LLMChatPipeline`、做真正的推理。在 Worker 模式下，它活在每个 worker 线程里，由 `WebWorkerMLCEngineHandler` 包裹。
- **代理**：`WebWorkerMLCEngine`，活在页面主线程。它**自己不做任何推理**，只是把每个方法调用打包成消息发给 worker，再等 worker 把结果寄回来。

关键是：两者都实现 `MLCEngineInterface`。于是你的业务代码写 `engine.chatCompletion(...)` 时，根本不需要知道背后是本线程的真身还是隔壁线程的代理——这就是 u1-l3 说的「Worker 代理层与真身实现同一接口，页面可无感切换」。收益在官方示例 README 里一句话点破：

> The main benefit of web worker is that all ML workloads runs on a separate thread as a result will less likely block the UI.（见 [examples/get-started-web-worker/README.md:3-6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/README.md#L3-L6)）

#### 4.1.2 核心流程

一次主线程上的 `chatCompletion` 非流式调用，在 Worker 架构下的完整旅程：

```text
页面代码
  │ await engine.chatCompletion(request)          ← engine 是 WebWorkerMLCEngine（代理）
  ▼
代理：把 (kind="chatCompletionNonStreaming", uuid=随机ID, content={request, modelId, chatOpts})
      打包成 WorkerRequest，worker.postMessage(msg)   ← 结构化克隆，跨线程
  ▼
worker 线程：WebWorkerMLCEngineHandler.onmessage(event)
      │ switch(msg.kind) 命中 "chatCompletionNonStreaming"
      ▼
handler：调用真身 this.engine.chatCompletion(request)   ← 真正的推理发生在这里（不阻塞主线程）
      │ 推理完成得到 res
      ▼
handler：打包 {kind:"return", uuid=同一个ID, content=res}，postMessage 回主线程
  ▼
代理：onmessage 收到响应，用 uuid 找到挂起的 Promise，resolve(content)
  ▼
页面代码拿到 ChatCompletion，await 返回
```

如果 handler 里的推理抛了异常，回程消息的 `kind` 换成 `"throw"`，代理侧把 Promise reject 掉——错误也走同一条管道。

#### 4.1.3 源码精读

**代理与真身实现同一接口**。类声明直接点明这一点：

- [src/web_worker.ts:412-422](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L412-L422)：`WebWorkerMLCEngine implements MLCEngineInterface`，类注释给出最小用法——`new webllm.WebWorkerMLCEngine(new Worker(new URL('./worker.ts', import.meta.url), {type: 'module'}))`。注意 `new URL('./worker.ts', import.meta.url)` 这个写法：它让打包器（Parcel/webpack）知道要把 `worker.ts` 单独编译成一个 worker 入口文件。

- [src/web_worker.ts:50-61](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L50-L61)：Handler 的类文档注释，展示 worker 脚本的标准接法——创建 engine、创建 handler、把 `onmessage` 指向 `handler.onmessage`。

- [src/web_worker.ts:61-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L61-L79)：Handler 的字段。注意 `public engine: MLCEngine`——真身引擎就放在这里；`loadedModelIdToAsyncGenerator` 是一个以 modelId 为键的 Map，存放流式生成器（4.3 详解）。

**用户侧的三行接线**。官方示例的 worker 脚本只有 7 行，值得逐行读：

- [examples/get-started-web-worker/src/worker.ts:1-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/worker.ts#L1-L7)：从 npm 包导入 `WebWorkerMLCEngineHandler`，`new` 出来，然后把 `self.onmessage` 指向 `handler.onmessage`。此后这个 worker 线程收到的每条消息都会进入 handler 的路由表。`self` 是 worker 内的全局对象（相当于主线程的 `window`，但没有 DOM）。

**主线程侧的工厂调用**：

- [examples/get-started-web-worker/src/main.ts:61-66](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/main.ts#L61-L66)：`CreateWebWorkerMLCEngine(new Worker(...), selectedModel, { initProgressCallback })`，注意返回值类型标注为 `webllm.MLCEngineInterface`——业务代码从这一刻起就只依赖抽象接口。

#### 4.1.4 代码实践

**实践目标**：建立「接口方法 ↔ 消息类型」的映射直觉，确认哪些调用真的跨了线程。

**操作步骤**：

1. 打开 [src/types.ts:62-87](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L62-L87) 起的 `MLCEngineInterface`，列出全部方法（`reload`、`chatCompletion`、`completion`、`embedding`、`unload`、`resetChat`、`interruptGenerate`、`getMessage`、`runtimeStatsText`、`forwardTokensAndSample`、`getMaxStorageBufferBindingSize`、`getGPUVendor`、`setInitProgressCallback` 等）。
2. 打开 [src/message.ts:18-37](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L18-L37) 的 `RequestKind` 联合类型，做一张两列对照表。
3. 标出三类特殊情况：
   - **本地处理、不发消息**：`setInitProgressCallback` 在代理上只保存回调（[src/web_worker.ts:469-471](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L469-L471)），因为进度是 worker 主动推送的（4.3 详解）。
   - **一个方法拆多条消息**：流式 `chatCompletion` 对应 `chatCompletionStreamInit` + `completionStreamNextChunk` 两种 kind（4.2 详解）。
   - **有 kind 但不属于公开接口**：`keepAlive`（Service Worker 保活，u5-l3 讲）、`customRequest`（自定义扩展点，u5-l2 讲）、`setAppConfig` / `setLogLevel`。

**需要观察的现象**：对照表里「公开方法」与「kind」不是一一对应，多出来的 kind 都是基础设施。

**预期结果**：你会发现协议比接口「厚」一层——这正是把一套函数调用语义压进纯消息通道所必须付出的复杂度。

（本实践为纯源码阅读型，无需运行环境。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WebWorkerMLCEngine` 选择「实现同一个接口」而不是「继承 `MLCEngine` 再覆写部分方法」？

**答案**：代理与真身没有任何共享实现——代理的全部行为是「序列化调用、发消息、等回信」，连字段都完全不同（真身持有管线 Map，代理持有 `pendingPromise` 挂起表）。继承会带来大量用不上的真身字段与逻辑，还会诱导在代理上误调用只在真身有意义的方法。接口（`MLCEngineInterface`）恰好只约束「对外长什么样」，是表达「可互换」的最小契约。

**练习 2**：把引擎搬进 worker 后，模型权重占的内存是算在哪个线程的？

**答案**：算在 worker 线程。权重、KV cache 等 GPU buffer 由 worker 内的 `MLCEngine` / `LLMChatPipeline` 持有，主线程只有轻量的代理对象。所以 Worker 版本的主线程内存占用几乎不变，这也是「主线程只做 UI」的另一面。

**练习 3**：如果不小心在 worker 脚本里写了 `document.getElementById(...)`，会发生什么？

**答案**：直接抛错——worker 的全局环境（`self`）里没有 `document`。这也是为什么 `initProgressCallback` 必须把进度**发消息回主线程**、由主线程更新 DOM，而不是在 worker 里直接改页面。

### 4.2 消息协议：WorkerRequest 与 WorkerResponse

#### 4.2.1 概念说明

两个线程之间只能传可结构化克隆的数据，所以「调用哪个方法、参数是什么、结果是给哪次调用的」必须全部编码进普通对象。`src/message.ts` 就是这份编码规范，可以把它理解成 WebLLM 私有的一次 **JSON-RPC**：

- **`kind`**：方法名。请求侧是 `RequestKind` 联合类型，共 19 个字符串字面量；响应侧是 `ResponseKind`：`"return"`（成功）、`"throw"`（失败）、`"initProgressCallback"`（进度推送）。
- **`uuid`**：请求关联 ID。每次调用用 `crypto.randomUUID()` 生成一个，响应原样带回，代理据此把响应对到正确的 `Promise` 上。线程间消息没有「返回值」语法，全靠这个字段模拟出请求-响应语义。
- **`content`**：参数或结果本体，类型是 `MessageContent` 联合——从 `ReloadParams` 到 `ChatCompletionChunk` 到 `null` 都可能。

#### 4.2.2 核心流程

**非流式调用**是严格的一问一答：

```text
主线程                          worker 线程
  │ ── {kind:"chatCompletionNonStreaming",
  │      uuid:U, content:{request,...}} ──▶
  │                                    （推理若干秒）
  │ ◀── {kind:"return", uuid:U, content:ChatCompletion} ──
```

**流式调用**被拆成「一次安装 + N 次拉取」：

```text
主线程                              worker 线程
  │ ── chatCompletionStreamInit(uuid U1) ──▶ 创建 async generator，
  │ ◀── return(U1, null) ──────────────────  存入 loadedModelIdToAsyncGenerator
  │ ── completionStreamNextChunk(uuid U2) ─▶ generator.next()
  │ ◀── return(U2, chunk#1) ───────────────  （解出一个 token 的 chunk）
  │ ── completionStreamNextChunk(uuid U3) ─▶ generator.next()
  │ ◀── return(U3, chunk#2) ──
  │ ...（每个 chunk 一次往返）...
  │ ◀── return(Uk, void) ───────────────────  生成器耗尽，代理 break 退出循环
```

注意这是**拉（pull）模型**：worker 不会主动把 chunk 推过来，而是主线程每消费一个 chunk 才发一条 `nextChunk` 消息去要下一个。每个 chunk 的端到端延迟大致是：

\[ T_{\text{chunk}} \;\approx\; T_{\text{decode}} \;+\; 2\,T_{\text{post}} \;+\; T_{\text{clone}} \]

其中 \( T_{\text{decode}} \) 是一次解码步（占绝对大头），\( T_{\text{post}} \) 是一次跨线程投递，\( T_{\text{clone}} \) 是 chunk 对象的结构化克隆开销。后两项通常是微秒级，相比几十毫秒的解码步可以忽略——所以流式体验几乎无损，但换来主线程全程空闲。

**进度推送**是协议里唯一的「不请自来」：`reload` 期间 worker 会多次发 `{kind:"initProgressCallback", uuid:"", content:report}`。它的 `uuid` 是空串，因为它不是对任何请求的应答，而是一路单独的广播信道。

#### 4.2.3 源码精读

**请求侧的全部 19 种 kind**：

- [src/message.ts:18-37](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L18-L37)：`RequestKind`。逐个读一遍可以发现命名规律：绝大多数 kind 就是引擎方法名（`reload`、`unload`、`resetChat`、`interruptGenerate`、`getMessage`、`runtimeStatsText`、`embedding`…），少数是为流式与基础设施新增的（`chatCompletionStreamInit`、`completionStreamNextChunk`、`keepAlive`、`customRequest`、`setAppConfig`、`setLogLevel`）。

**请求信封**：

- [src/message.ts:137-141](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L137-L141)：`WorkerRequest = { kind, uuid, content }`，注释称之为「worker 与主线程之间交换的消息」。
- [src/message.ts:108-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L108-L131)：`MessageContent` 联合类型穷举了 content 可能承载的所有载荷——各种 `*Params`、`InitProgressReport`、`ChatCompletion` / `ChatCompletionChunk` / `Completion` / `CreateEmbeddingResponse`（响应体）、`AppConfig`、标量与 `null`/`void`。它同时服务于请求与响应两个方向，所以看起来很杂。

**响应侧三种类型**：

- [src/message.ts:148-152](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L148-L152)：`OneTimeWorkerResponse`——`kind: "return" | "throw"`，对某次请求的一次性应答。
- [src/message.ts:154-158](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L154-L158)：`InitProgressWorkerResponse`——content 固定为 `InitProgressReport`。
- [src/message.ts:143-146](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L143-L146) 与 [src/message.ts:160-163](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L160-L163)：`HeartbeatWorkerResponse`（心跳）与三者的并集 `WorkerResponse`。心跳配合请求侧的 `keepAlive` kind 使用，是为 Service Worker「闲置被浏览器回收」问题准备的，本讲不展开，u5-l3 详解。

**两个值得精读的参数结构**：

- [src/message.ts:62-67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L62-L67)：这段注释解释了为什么几乎每个 `*Params` 都带 `modelId: string[]` 与 `chatOpts?: ChatOptions[]`——这是**主线程期望 worker 已加载的模型清单**。Web Worker 一般常驻，但 Service Worker 可能被浏览器随时杀掉；线程复活后模型已丢，handler 拿这份期望清单就地 `reload()` 自愈（见 4.3 的 `reloadIfUnmatched`）。引擎支持多模型，所以是数组。
- [src/message.ts:69-83](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L69-L83)：`ChatCompletionStreamInitParams` 额外带 `selectedModelId`（单数）——本次请求实际选中的模型。注释说明它用于在多个并存的生成器中定位该 `next()` 哪一个；[src/message.ts:100-102](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L100-L102) 的 `CompletionStreamNextChunkParams` 则**只**带这一个字段——拉 chunk 时不再重复传请求。

#### 4.2.4 代码实践

**实践目标**：亲眼看到协议在两个线程间的真实流动顺序。

**操作步骤**：

1. 复制 `examples/get-started-web-worker` 为自己的实验目录（或直接修改示例），`npm install && npm start`。
2. 给 worker 脚本包一层探针（**示例代码**，非项目原有）：

   ```ts
   import { WebWorkerMLCEngineHandler } from "@mlc-ai/web-llm";

   const handler = new WebWorkerMLCEngineHandler();
   self.onmessage = (msg: MessageEvent) => {
     const { kind, uuid } = msg.data;
     if (kind !== "completionStreamNextChunk") {
       console.log(`[worker] ← ${kind} (${uuid.slice(0, 8)})`);
     }
     handler.onmessage(msg);
   };
   ```

   （对 `nextChunk` 降噪是因为它会来几十上百次，可单独计数后打印总数。）
3. 在主线程 `main.ts` 的 `initProgressCallback` 里打印 `report.progress`，跑一次流式对话。
4. 打开 DevTools Console，注意右上角的下拉框可以切换「top（主线程）」与 worker 上下文，分别能看到两侧日志。

**需要观察的现象**：消息时序应呈现出 4.2.2 描述的形状——`reload` 之后穿插大量 `initProgressCallback`（uuid 为空串），随后一条 `chatCompletionStreamInit`，接着是长串 `completionStreamNextChunk`，最后流自然终止。

**预期结果**：主线程 console 里能看到代理侧收到的 `return` 消息与请求 uuid 一一配对；worker console 里能看到请求按 kind 依次到达。若网络环境受限无法下载模型，此实验「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么每次请求都要生成新的 `uuid`，而不是全局用一个？

**答案**：线程间通信是异步且可能交错的——主线程可以在等待上一条响应期间发出第二条请求（例如流式进行中调用 `interruptGenerate`，它就不等结果）。没有唯一 uuid，代理收到响应就无法判断该 resolve 哪个 `Promise`。源码里代理用 `pendingPromise: Map<uuid, callback>` 精确维护这张挂起表（见 4.4.3）。

**练习 2**：`initProgressCallback` 响应的 `uuid` 为什么是空字符串而不是随机 ID？

**答案**：因为它不是对任何请求的应答，而是 worker 在 `reload` 执行期间主动、多次推送的事件，没有可配对的请求。代理侧对它的处理也不查 `pendingPromise`，而是直接调用本地保存的 `initProgressCallback` 回调（[src/web_worker.ts:812-817](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L812-L817)）。单元测试也把这个空串写进了断言（[tests/web_worker_handler.test.ts:59-63](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L59-L63)）。

**练习 3**：流式输出为什么设计成「每个 chunk 一次消息往返」的拉模型，而不是 worker 生成一个 chunk 就推一个？

**答案**：推模型需要处理背压（backpressure）——主线程消费慢时 worker 是否继续生成、消息是否堆积；拉模型下 `next()` 天然就是流控阀门：主线程消费完一个才会要下一个，worker 侧生成器 `await next()` 自动挂起，语义与主线程版的 `AsyncIterable` 完全对齐，代码最简单。代价是每 chunk 两次跨线程投递，但如 4.2.2 的公式所示，相比解码步耗时可以忽略。

### 4.3 Worker 侧真身：WebWorkerMLCEngineHandler

#### 4.3.1 概念说明

`WebWorkerMLCEngineHandler` 是跑在 worker 线程里的「服务端」。它做三件事：

1. **持有真身**：构造时 `new MLCEngine()`，此后一切推理都由它完成。
2. **路由**：`onmessage` 里一张巨大的 `switch (msg.kind)` 表，把每种消息翻译成对真身的一次方法调用。
3. **善后**：统一的 `handleTask` 包装器把调用结果（或异常）装进 `return` / `throw` 消息寄回；流式请求的生成器按 modelId 存进 Map 供后续拉取；模型状态失配时自动重载自愈。

此外它在构造时做了一件容易被忽略的事：向真身引擎注册一个 `initProgressCallback`，把加载进度转发回主线程——这就是 4.2 里那条「不请自来」信道的源头。

#### 4.3.2 核心流程

`onmessage` 的分发骨架（伪代码）：

```text
收到 event：
  若是 MessageEvent 则取 event.data，否则把 event 本身当 WorkerRequest
  switch (msg.kind):
    "reload"                   → engine.reload(modelId, chatOpts)；记录 this.modelId/chatOpts
    "chatCompletionNonStreaming"→ reloadIfUnmatched(...)；engine.chatCompletion(request)
    "chatCompletionStreamInit" → reloadIfUnmatched(...)；
                                 generator = await engine.chatCompletion(request)  // 流式重载
                                 loadedModelIdToAsyncGenerator.set(selectedModelId, generator)
    "completionStreamNextChunk"→ generator.next()，把 yield 的值寄回
    "interruptGenerate"        → engine.interruptGenerate()（无参数无结果）
    "unload"                   → engine.unload()；清空 modelId/chatOpts/generator Map
    ...（其余 kind 同理）
    default                    → 抛 UnknownMessageKindError

每个分支都包在 handleTask(uuid, task) 里：
  task 成功 → postMessage({kind:"return", uuid, content:结果})
  task 抛错 → postMessage({kind:"throw", uuid, content:err.toString()})
```

#### 4.3.3 源码精读

**构造函数：注册进度转发**。

- [src/web_worker.ts:84-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L84-L98)：`new MLCEngine()` 创建真身；`setInitProgressCallback` 里把每份 `InitProgressReport` 包成 `{kind:"initProgressCallback", uuid:"", content:report}` 发回主线程。`uuid: ""` 的含义见练习 2。
- [src/web_worker.ts:100-103](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L100-L103)：handler 的 `postMessage` 直接调用 worker 全局环境的 `postMessage`（DOM 提供的「worker → 主线程」方向 API）。测试里正是 mock 了 `globalThis.postMessage` 来捕获输出（[tests/web_worker_handler.test.ts:47](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L47)）。

**handleTask：统一的成功/失败信封，以及错误的降级**。

- [src/web_worker.ts:111-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L111-L132)：注意 catch 分支里是 `const errStr = (err as object).toString()`——**异常被压成字符串再过线程**。结构化克隆无法可靠复制 `Error` 对象的类信息，所以主线程 catch 到的通常是字符串而非 `WebLLM` 的具体错误类，`instanceof` 判断会失效。这是 Worker 模式下一个重要的工程细节，错误体系详见 u7-l1。

**路由表中的代表性分支**。

- [src/web_worker.ts:139-145](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L139-L145)：`onmessage` 入口同时兼容 `MessageEvent`（真实 worker 环境）与裸对象（测试环境直接传普通对象）——`event instanceof MessageEvent ? event.data : event`。这让单元测试无需真的开线程。
- [src/web_worker.ts:146-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L146-L156)：`reload` 分支，调用真身后把 `modelId` / `chatOpts` 记到 `this` 上——handler 侧维护的是「实际已加载清单」，与代理侧的「期望清单」构成一对状态镜像。
- [src/web_worker.ts:182-200](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L182-L200)：`chatCompletionStreamInit` 分支——先 `reloadIfUnmatched` 自愈，再 `await engine.chatCompletion(request)` 拿到异步生成器（流式重载，见 u2-l3），存入 `loadedModelIdToAsyncGenerator`。注释强调：ChatCompletion 与 Completion **共享同一套生成器机制**。
- [src/web_worker.ts:233-252](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L233-L252)：`completionStreamNextChunk` 分支——按 `selectedModelId` 取生成器，`await generator.next()` 一次，把 `value` 寄回。生成器还没建好会抛内部错误。
- [src/web_worker.ts:282-296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L282-L296)：`unload` 分支——除卸载真身外，还清空 `modelId` / `chatOpts` / 生成器 Map。注释坦承：生成器 Map 只在 `unload` 时清理，`reload` 不清，是个已知的取舍。

**自愈机制：reloadIfUnmatched**。

- [src/web_worker.ts:360-377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L360-L377)：用 [src/utils.ts:4-12](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/utils.ts#L4-L12) 的 `areArraysEqual` 比较期望与实际加载清单，不一致（典型场景：Service Worker 被杀后复活、状态归零）就打一条 warning 并就地 `reload`。对主线程而言这一切透明——这正是 4.2.3 里「每个 Params 都带 modelId 数组」的用意。

**兜底分支**。

- [src/web_worker.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L348-L356) 与 [src/error.ts:469-474](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L469-L474)：未知 kind（且带 content）抛 `UnknownMessageKindError`；既无 kind 又无 content 的事件被当作无关噪音忽略（worker 的 `onmessage` 也会收到一些环境事件）。

#### 4.3.4 代码实践

**实践目标**：学会像维护者一样，用 mock 单测驱动 Handler，不启动真实 worker、不下载模型。

**操作步骤**：

1. 阅读 [tests/web_worker_handler.test.ts:9-48](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L9-L48)：测试用 `jest.mock("../src/engine")` 把 `MLCEngine` 整个替换成 mock 对象，再在 `beforeEach` 里 mock 掉 `globalThis.postMessage`——Handler 的两个外部依赖（真身引擎、回信 API）全部被切断。
2. 精读第一个用例 [tests/web_worker_handler.test.ts:54-66](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L54-L66)：直接 `new WebWorkerMLCEngineHandler()`，然后手动触发 mock 引擎保存的 init 回调，断言 `globalThis.postMessage` 被以 `{kind:"initProgressCallback", uuid:"", content:report}` 调用。
3. 在仓库根目录运行：`npm test -- web_worker_handler`。

**需要观察的现象**：测试全部通过；注意用例里给 `handler.onmessage` 传的是**普通对象**而非 `MessageEvent`，验证了 4.3.3 说的入口兼容逻辑。

**预期结果**：你会看到这类测试在**完全没有 WebGPU 的 CI 环境**里也能跑——这正是 4.1 里「handler 只依赖消息与引擎接口」设计的直接回报。若本地 `npm test` 因其他集成测试（需要真实模型）失败，可加 `--testPathPattern` 只跑本文件；在你的环境能否完整运行「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`handleTask` 里为什么必须 `err.toString()` 而不能直接把 `err` 放进消息？

**答案**：跨线程消息要过结构化克隆。`Error` 对象克隆后只会保留有限的内置属性，自定义的类名、字段（如 `IntegrityError` 的 `url/expected/actual`）与原型链都会丢失，`instanceof` 在主线程必然失效。与其得到一个残缺对象，不如明确降级为字符串，至少错误消息文本完整。要拿到结构化错误信息，得靠协议扩展（这也是 `customRequest` 这类扩展点的动机之一，见 u5-l2）。

**练习 2**：`loadedModelIdToAsyncGenerator` 为什么以 modelId 为键、而不是全局只存一个生成器？

**答案**：一个引擎可以同时加载多个模型（u2-l1 的 `loadedModelIdToPipeline`），每个模型都可能有正在进行的流式请求；`completionStreamNextChunk` 消息只带 `selectedModelId`，handler 必须能据此定位「该 next 哪一个」生成器。这就是 `ChatCompletionStreamInitParams.selectedModelId` 存在的理由。

**练习 3**：`reloadIfUnmatched` 里有一条 TODO：「should we also check expectedChatOpts here?」。如果只比对 modelId 不比对 chatOpts，什么情况下会出问题？

**答案**：同一 modelId 用不同 chatOpts（例如不同 `context_window_size`）先后 reload 的场景。线程复活后自愈逻辑只按 modelId 判断「已加载」，会沿用旧 chatOpts 初始化的管线，与主线程期望的配置不一致。当前实现接受这个缺口，注释里已标明是待完善项——读源码时留意这类 TODO 是理解工程取舍的好入口。

### 4.4 主线程侧代理：CreateWebWorkerMLCEngine 与 ChatWorker 抽象

#### 4.4.1 概念说明

主线程侧的主角是 `WebWorkerMLCEngine`（代理），而用户最常接触的入口是工厂函数 `CreateWebWorkerMLCEngine`——它与 u2-l1 的 `CreateMLCEngine` 完全平行：

```ts
CreateWebWorkerMLCEngine(worker, modelId, engineConfig?)
// 等价于：
const engine = new WebWorkerMLCEngine(worker, engineConfig);
await engine.reload(modelId, chatOpts);
return engine;
```

**三参数用法**：

1. `worker`：一个已创建的 Worker 实例（`new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' })`），worker 脚本里要安装 `WebWorkerMLCEngineHandler`。
2. `modelId`：字符串或字符串数组（多模型顺序加载），须在 `prebuiltAppConfig` 或 `engineConfig.appConfig` 中存在。
3. `engineConfig`：可选的 `MLCEngineConfig`——`appConfig`（自定义模型清单/缓存后端）、`logLevel`、`initProgressCallback`。注意 `logitProcessorRegistry` 在 Worker 版会被忽略并告警（见 4.4.3），因为回调函数无法跨线程传递。

第四个可选参数 `chatOpts` 与 `modelId` 数量配对，逐模型覆盖 `mlc-chat-config.json`（回顾 u1-l4 的三层合并）。

**`ChatWorker` 抽象**是本讲的点睛之笔：代理完全不依赖浏览器 `Worker` 类型，只要求传入的对象满足

```ts
interface ChatWorker {
  onmessage: any;
  postMessage: (message: any) => void;
}
```

这是 TypeScript 的**结构化类型（structural typing）**：任何「有这两个成员」的对象都合法——真实 Web Worker、Service Worker、乃至你在测试里手写的假对象。下一讲的 Service Worker 引擎正是复用了这个抽象。

#### 4.4.2 核心流程

代理把「函数调用」翻译成消息的核心机制是 `getPromise`：

```text
getPromise(msg):
  1. 取出 msg.uuid
  2. new Promise：把 resolve/reject 包装成 cb，存进 pendingPromise Map（键 = uuid）
     —— 注意：此时不 postMessage，Promise 处于挂起态
  3. worker.postMessage(msg)         ← 真正发出请求
  4. 返回这个 Promise

（稍后，worker 的回信到达）
代理 onmessage(event):
  kind == "return" → pendingPromise.get(uuid)，delete，resolve(content)
  kind == "throw"  → 同上，但 reject(content)
  kind == "initProgressCallback" → 调用本地 initProgressCallback
```

流式调用在此基础上叠加一层：`chatCompletion` 先发 `StreamInit`（等 worker 把生成器建好），然后返回**主线程本地的** `asyncGenerate` 生成器；它的循环体每轮发一条 `NextChunk` 消息、`await` 一个 chunk、`yield` 给消费者，直到 worker 寄回 `void`（非对象）为止。

代理还维护 `this.modelId` / `this.chatOpts`——「主线程期望 worker 已加载的清单」。每次带 `modelId[]` 的请求都捎上它，配合 handler 的 `reloadIfUnmatched` 构成双端状态对账。

#### 4.4.3 源码精读

**工厂函数**：

- [src/web_worker.ts:385-410](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L385-L410)：`CreateWebWorkerMLCEngine` 全文只有三行逻辑——new 代理、await reload、返回。JSDoc 完整说明了各参数与 `@note`：`engineConfig.logitProcessorRegistry` 被忽略。

**构造函数**：

- [src/web_worker.ts:443-467](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L443-L467)：把 `worker.onmessage` 接到自己的 `onmessage`（从此回信自动进路由）；`appConfig` / `logLevel` 分别转成 `setAppConfig` / `setLogLevel` 消息发给 worker（[src/web_worker.ts:477-494](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L477-L494)）；若传了 `logitProcessorRegistry` 则打 warning——回调没法过结构化克隆，必须在 worker 脚本内用 `handler.setLogitProcessorRegistry` 注册（呼应 u3-l5）。

**挂起表**：

- [src/web_worker.ts:496-520](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L496-L520)：`getPromise` 全文。注意 `pendingPromise.set(uuid, cb)` 之后才 `postMessage(msg)`——顺序保证不会出现「响应先到、表里还没登记」的竞态（单线程事件循环里 postMessage 是异步投递，回调一定晚于同步代码）。
- [src/web_worker.ts:804-841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L804-L841)：代理侧 `onmessage` 路由——`return` / `throw` 都先 `pendingPromise.get(msg.uuid)` 再 `delete` 再回调；查不到 uuid 会直接抛错（说明出了协议错乱）。

**reload 与状态镜像**：

- [src/web_worker.ts:522-545](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L522-L545)：单值统一转数组、发消息、await、然后把 `modelId` / `chatOpts` 记在 `this` 上。与 handler 侧的记录（4.3.3）对照着读，「期望清单 vs 实际清单」的设计就完整了。

**chatCompletion 的流式分支**：

- [src/web_worker.ts:680-692](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L680-L692)：入口先查 `this.modelId === undefined` 则抛 [src/error.ts:97-104](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L97-L104) 的 `WorkerEngineModelNotLoadedError`（「Did you call \`engine.reload()\`?」）；再用 [src/support.ts:227-239](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L239) 的 `getModelIdToUse` 从「已加载清单 + request.model」算出 `selectedModelId`（未加载时抛 `ModelNotLoadedError`，指定了未加载模型时抛 `SpecifiedModelNotFoundError`）。
- [src/web_worker.ts:692-712](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L692-L712)：流式路径——发 `chatCompletionStreamInit`（await，确保 worker 侧生成器已就绪），再返回本地 `asyncGenerate(selectedModelId)`。非流式路径则是一条 `chatCompletionNonStreaming` 消息了事（[src/web_worker.ts:714-724](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L714-L724)）。
- [src/web_worker.ts:647-666](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L647-L666)：`asyncGenerate` 的 `while(true)` 循环。终止判据是 `typeof ret !== "object"`——worker 侧生成器耗尽后 `next()` 的 value 是 `void`，结构化克隆后到达主线程即 `undefined`，据此 `break`。这与 JSDoc「The last message is `void`」一致。

**中断**：

- [src/web_worker.ts:587-594](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L587-L594)：`interruptGenerate` 调用了 `getPromise` 但**不 await**——发完消息立刻返回，不等 worker 确认。配合 u2-l3 讲过的协作式中断（worker 侧每个 decode 步前检查标志），点击「停止」按钮的主线程是瞬间返回的。

**ChatWorker 抽象**：

- [src/web_worker.ts:380-383](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L380-L383)：`ChatWorker` 全文就这两个成员。它把「代理需要线程做什么」压缩到最小：能收消息（`onmessage`）、能发消息（`postMessage`）。下一讲 Service Worker 与自定义测试替身都靠它接入。

#### 4.4.4 代码实践

**实践目标**：亲手验证「代理只认 `ChatWorker` 形状，不认真 Worker」——用一个假 worker 骗过代理，无需 GPU 与模型即可观察协议往返。

**操作步骤**：

1. 新建一个最小 HTML 页面（或直接在示例的 `main.ts` 里临时替换），写入（**示例代码**，非项目原有）：

   ```ts
   import * as webllm from "@mlc-ai/web-llm";

   // ChatWorker 未从库入口导出（见 src/index.ts:43-49），
   // 这里按其结构本地声明同样的最小形状
   interface ChatWorker {
     onmessage: any;
     postMessage: (message: any) => void;
   }

   // 手工实现一个满足 ChatWorker 接口的假 worker
   const fakeWorker: ChatWorker = {
     onmessage: null,
     postMessage(message: any) {
       const req = message as { kind: string; uuid: string };
       console.log("proxy 发出请求:", req.kind, req.uuid);
       // 假装 worker 线程处理完毕，立刻回一封 "return" 信
       setTimeout(() => {
         this.onmessage({
           data: { kind: "return", uuid: req.uuid, content: 12345 },
         });
       }, 100);
     },
   };

   const engine = new webllm.WebWorkerMLCEngine(fakeWorker);
   await engine.reload("Llama-3.1-8B-Instruct-q4f32_1-MLC"); // 会被假 worker 立刻"应答"
   console.log(await engine.getMaxStorageBufferBindingSize()); // 期望打印 12345
   ```

2. 运行页面，打开 console。

**需要观察的现象**：`reload` 与 `getMaxStorageBufferBindingSize` 两次调用各打印一条请求日志，且第二次调用的返回值是假 worker 塞进去的 `12345`——代理被彻底「骗」过，全程没有真实推理。

**预期结果**：证明代理的行为完全由 `{kind, uuid, content}` 协议驱动，`ChatWorker` 抽象是它对外的唯一依赖。这同时也是 `tests/web_worker_handler.test.ts` 一类测试的思路来源。（本实践不涉及真实推理，在无 WebGPU 环境也可运行；具体输出「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `engineConfig.logitProcessorRegistry` 在 Worker 版被忽略，而 `appConfig`、`logLevel` 可以照常传？

**答案**：`appConfig` 与 `logLevel` 是纯数据（JSON 对象、枚举字符串），能被结构化克隆；`LogitProcessor` 是带方法的接口对象，函数无法克隆过线程。所以注册表必须在 worker 线程内（拿到函数定义的那一侧）通过 `handler.setLogitProcessorRegistry` 注入——源码在构造函数里对误用打出了明确的 warning（[src/web_worker.ts:456-462](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L456-L462)）。

**练习 2**：`asyncGenerate` 判断流结束的条件是 `typeof ret !== "object"`。为什么不用 `ret === undefined` 之外的更明确信号，比如 worker 发一个 `{kind:"end"}`？

**答案**：复用现有信封即可表达：生成器耗尽时 `next()` 返回 `{value: undefined, done: true}`，handler 把 `undefined` 装进 `return` 消息，到达代理后 `typeof undefined !== "object"` 成立即 break。少一种消息类型就少一段两端都要处理的协议分支；代价是「chunk 恰好不是对象」的合法值都被当作终止信号——对本协议而言 chunk 一定是对象，所以是安全的约定。

**练习 3**：如果把一个 Service Worker 的注册对象（`registration.active`，即 `ServiceWorker` 实例）传给 `WebWorkerMLCEngine`，会正常工作吗？

**答案**：类型上可以——`ServiceWorker` 也拥有 `onmessage` 与 `postMessage`，满足 `ChatWorker` 形状，这正是该抽象的设计意图。但生命周期语义不同（Service Worker 会被浏览器回收，需要 4.3 的 `reloadIfUnmatched` 与 `keepAlive` 心跳机制兜底），所以 WebLLM 另外提供了 `CreateServiceWorkerMLCEngine` 做注册与保活封装——这就是 u5-l3 的主题。

## 5. 综合实践

把 u1-l2 跑通的 `examples/get-started` 页面改造成 Web Worker 版本，并用一个 CSS 动画量化「主线程被解放」的收益。

**步骤**：

1. **建工程**：复制 `examples/get-started` 为 `my-worker-demo`（或直接复制 `examples/get-started-web-worker` 再改名），确认 `package.json` 里有 `@mlc-ai/web-llm` 依赖，`npm install`。
2. **加 worker 脚本**：新建 `src/worker.ts`，内容照抄 [examples/get-started-web-worker/src/worker.ts:1-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/worker.ts#L1-L7)（导入 Handler、挂 `self.onmessage`）。
3. **改主线程**：参照 [examples/get-started-web-worker/src/main.ts:55-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-web-worker/src/main.ts#L55-L98)，把引擎创建换成：

   ```ts
   const engine = await webllm.CreateWebWorkerMLCEngine(
     new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }),
     selectedModel,
     { initProgressCallback: (r) => setLabel("init-label", r.text) },
   );
   ```

   对话部分保持 u2-l3 的流式写法：`for await (const chunk of await engine.chat.completions.create(request))` 逐帧拼接。
4. **加测量装置**：在页面上放两个动画探针（**示例代码**）：
   - 一个依赖主线程的 CSS 动画（例如对某元素做 `width: 0 → 100%` 的 keyframes 动画——宽度变化会触发重排，必须由主线程处理）；
   - 一个 `requestAnimationFrame` 帧率计数器，每秒统计回调次数并显示。
5. **对照实验**：保留原始主线程版 `get-started` 页面，两版用**同一个模型、同一段 prompt**（建议让回复长一些，例如「写一首 20 行的诗」）各跑一次，同时观察两个探针。
6. **记录与解释**：记录两版的 rAF 帧率（生成期间 vs 空闲期间）、动画是否肉眼卡顿，写成简短对比笔记。

**预期结果**：主线程版在解码期间 rAF 帧率显著下降（可能掉到个位数）、宽度动画明显卡顿；Worker 版生成期间帧率基本维持满帧，只有 chunk 到达并更新 DOM 的瞬间有微小开销。原因是主线程版每个 decode step 都是一个长任务连续霸占事件循环，而 Worker 版主线程只在每收到一个 chunk 时做一次微小的 DOM 更新。

**注意事项**：

- 如果你用的是纯 `transform: translateX(...)` 动画，可能观察不到差异——合成器线程可以独立渲染它，主线程被阻塞也不掉帧。这正是选 `width` 动画作探针的原因。
- 差异幅度与机器性能相关：GPU 越弱、模型越大、decode 越「重」，主线程版卡得越明显。
- 若本地没有支持 WebGPU 的浏览器或无法下载模型，本实践「待本地验证」，可退化为：只在 DevTools Performance 面板录制两版的 flamegraph，对比主线程上长任务（红色斜纹块）的有无。

## 6. 本讲小结

- **动机**：主线程版引擎的 prefill/decode 会让主线程长时间忙等，UI 卡死；Web Worker 提供独立线程与事件循环，让主线程只做 UI——代价是两线程间只能用 `postMessage` + 结构化克隆通信。
- **代理模式**：`WebWorkerMLCEngine`（主线程代理）与 `MLCEngine`（worker 内真身，由 `WebWorkerMLCEngineHandler` 包裹）实现同一个 `MLCEngineInterface`，业务代码无感切换。
- **消息协议**：一切调用被编码为 `WorkerRequest {kind, uuid, content}`，19 种请求 kind 对应引擎方法；响应为 `return` / `throw` / `initProgressCallback`（外加 Service Worker 用的 `heartbeat`）。uuid 是把异步消息还原成请求-响应语义的关键；进度推送是 uuid 为空串的广播。
- **流式的拆分**：一次流式请求 = 一条 `StreamInit`（在 worker 内创建异步生成器）+ N 条 `NextChunk`（每次拉取一个 chunk）+ 一条 `void` 终止。这是拉模型，`next()` 天然承担流控。
- **双端状态对账**：代理记「期望加载的 modelId/chatOpts」，handler 记「实际加载的」，失配（典型为 Service Worker 被杀复活）时 `reloadIfUnmatched` 自动重载自愈。
- **两个工程细节**：错误过线程会退化为字符串（`err.toString()`），主线程 `instanceof` 失效；回调类配置（`logitProcessorRegistry`）无法过线程，必须在 worker 脚本内注册。`ChatWorker {onmessage, postMessage}` 两字段抽象让真 Worker、Service Worker、测试替身都能接入。

## 7. 下一步学习建议

- **下一讲 u5-l2**《WebWorkerMLCEngineHandler 源码解析》：本讲只精读了 Handler 的骨架，下一讲逐 kind 展开路由表、跨线程流式回传的细节，以及 `CustomRequestParams` / `onrequest` 自定义扩展点——你会用它实现一个跨线程的 `runtimeStatsText` 查询按钮。
- **u5-l3**《Service Worker 与 Chrome 扩展支持》：看 `keepAlive` 心跳、`ServiceWorkerMLCEngine` 如何在本讲的 `ChatWorker` 抽象与 `reloadIfUnmatched` 自愈机制之上构建跨页面共享的引擎。
- **回看 u2-l3 与 u3-l5**：流式的 `prevMessageLength` 游标切片发生在 worker 侧生成器里，而 `interruptGenerate` 的协作式标志检查也都在 worker 内——把三讲对照读，能完整拼出「跨线程流式 + 中断」的全景。
- **延伸阅读**：MDN 的《Using Web Workers》与《Structured clone algorithm》，加深对本讲通信模型的理解；仓库内 [tests/web_worker_handler.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts) 是检验你是否真的读懂协议的最好习题集。
