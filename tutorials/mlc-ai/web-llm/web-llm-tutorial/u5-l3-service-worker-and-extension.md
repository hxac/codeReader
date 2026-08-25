# Service Worker 与 Chrome 扩展支持

> 本讲是第五单元「Worker 架构：多线程与扩展环境」的第三讲（u5-l3），依赖 u5-l1（Web Worker 架构与消息协议）与 u5-l2（WebWorkerMLCEngineHandler 源码解析）建立的认知。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `ServiceWorkerMLCEngineHandler` 继承 `WebWorkerMLCEngineHandler` 之后**复用了什么、重写了什么、为什么必须重写**。
2. 独立走通「页面注册 Service Worker → `CreateServiceWorkerMLCEngine` → 消息往返 → 流式输出」的完整链路，并理解 `clientRegistry`、`waitUntil`、心跳（heartbeat）三个机制各自解决什么问题。
3. 理解 `src/extension_service_worker.ts` 对 Chrome MV3 扩展的适配：用 `chrome.runtime.Port` 替代 `Client` 作为通信通道，并通过 `index.ts` 的**别名导出**以 `ExtensionServiceWorkerMLCEngineHandler` 等名字对外发布。
4. 能对比「普通 Web Worker 方案」与「Service Worker 方案」的适用场景与代价（生命周期、内存回收、多页面共享）。

## 2. 前置知识

### 2.1 回顾：第五单元前两讲已经建立的事实

- WebLLM 的 Worker 架构是**代理模式**：页面侧引擎（代理）与 worker 内的真身引擎实现同一 `MLCEngineInterface`，业务代码无感切换。
- 线程间通信被编码为信封消息 `WorkerRequest = { kind, uuid, content }`，响应是 `return` / `throw` / `initProgressCallback` 三种 `WorkerResponse`。
- `ChatWorker` 是一个只有 `onmessage` 和 `postMessage` 两个字段的最小接口，凡是满足这个形状的东西都能充当「worker」。
- 基类 `WebWorkerMLCEngineHandler` 内部持有一个真的 `MLCEngine`，把 19 种消息 kind 路由到引擎方法，其中 `keepAlive` 在基类中留给 Service Worker 子类处理。

如果不熟悉以上任何一条，建议先回看 u5-l1 与 u5-l2。

### 2.2 Service Worker 与 Web Worker 的本质区别

两者都不是「页面主线程」，但生命周期归属完全不同：

| 维度 | Web Worker | Service Worker |
|---|---|---|
| 归属 | 某一个页面，页面关闭即销毁 | 一个「源 + scope」，独立于任何页面存活 |
| 多页面共享 | 不能，每个页面各自 new 一个 | 能，同 scope 下所有页面共用一个实例 |
| 生命周期控制方 | 页面（你 new 的，你说了算） | 浏览器（空闲时可能被强制终止） |
| DOM 访问 | 无 | 无 |
| 环境要求 | 无特殊要求 | 需要安全上下文（HTTPS 或 localhost），且需先「注册」 |
| 典型用途 | 计算密集任务搬离主线程 | 离线缓存、推送、跨页面后台服务 |

对 WebLLM 来说，这意味着一个关键差异：**模型权重放在 Web Worker 里，页面一关就没了；放在 Service Worker 里，可以在多个标签页之间共享同一份加载好的模型（权重、KV cache、GPU 资源都只有一份）**。代价是浏览器会在它「空闲」时把它杀掉——这是本讲反复出现的主题。

> 术语：**注册（register）**指页面调用 `navigator.serviceWorker.register(swScriptURL)`，让浏览器安装并激活一个 Service Worker；**scope** 是它管辖的 URL 范围；**Client** 是浏览器里代表「一个被它控制的页面」的对象，可以对其 `postMessage`。

### 2.3 Service Worker 的生命周期问题

Service Worker 有两个让推理引擎「头疼」的特性：

1. **空闲终止**：浏览器（尤其是 Chrome 扩展的 MV3 Service Worker，约 30 秒空闲后）会终止它，内存中的模型权重、GPU 资源随之回收。
2. **事件驱动**：它只在被事件唤醒时运行。`ExtendableMessageEvent.waitUntil(promise)` 可以告诉浏览器「在这个 promise 结算之前我还有活没干完」，从而避免任务执行到一半被回收。

WebLLM 对这两个问题的回答分别是：**心跳（heartbeat）**与 **`waitUntil` + 失配自愈（`reloadIfUnmatched`）**。

### 2.4 Chrome 扩展 Manifest V3（MV3）

MV3 要求扩展的后台脚本是 Service Worker（`manifest.json` 中 `background.service_worker`），它「按需加载、休眠即卸载」。页面（popup、options）与它之间的长连接是 `chrome.runtime.Port`（通过 `chrome.runtime.connect()` 建立）。另外，扩展页面受内容安全策略（CSP）约束，跑 WebAssembly 需要 `wasm-unsafe-eval`。这些约束共同决定了 `extension_service_worker.ts` 的形态。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/service_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts) | 网页版 Service Worker 支持：`ServiceWorkerMLCEngineHandler`（worker 侧）、`ServiceWorker`（页面侧 ChatWorker 封装）、`ServiceWorkerMLCEngine` 与 `CreateServiceWorkerMLCEngine` |
| [src/extension_service_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts) | Chrome MV3 扩展适配：同名类族，但通信通道换成 `chrome.runtime.Port`，并新增 `PortAdapter`、`ExtensionMLCEngineConfig` |
| [src/web_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts) | 基类 `WebWorkerMLCEngineHandler` / `WebWorkerMLCEngine` 与 `ChatWorker` 接口，本讲的两个子类都从它继承 |
| [src/message.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts) | 消息协议：19 种请求 kind、`WorkerRequest`/`WorkerResponse`、心跳响应类型 |
| [src/index.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts) | 库入口：把扩展版类族以 `Extension*` 别名导出，避免与网页版同名冲突 |
| [examples/service-worker/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/README.md) | 网页版 Service Worker 可运行示例 |
| [examples/chrome-extension-webgpu-service-worker/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/README.md) | Chrome MV3 扩展示例（注意：大纲中写作 `examples/chrome-extension/`，实际目录名为 `chrome-extension-webgpu-service-worker`） |
| [tests/service_worker.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/service_worker.test.ts) / [tests/extension_service_worker.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/extension_service_worker.test.ts) | 无 GPU 也能跑的 mock 单测，是本讲实践的重要依据 |

一个容易踩坑的点：`src/service_worker.ts` 与 `src/extension_service_worker.ts` 里定义的类**同名**（都叫 `ServiceWorkerMLCEngineHandler` / `ServiceWorkerMLCEngine` / `CreateServiceWorkerMLCEngine`）。它们靠 `index.ts` 的别名导出区分，网页版用原名，扩展版加 `Extension` 前缀：

- [src/index.ts:51-55](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L51-L55)：从 `./service_worker` 原名导出；
- [src/index.ts:57-61](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L57-L61)：从 `./extension_service_worker` 以 `ExtensionServiceWorkerMLCEngineHandler`、`ExtensionServiceWorkerMLCEngine`、`CreateExtensionServiceWorkerMLCEngine` 别名导出。

所以扩展示例 `background.ts` 里的 `import { ExtensionServiceWorkerMLCEngineHandler } from "@mlc-ai/web-llm"`，实际指向的是 `extension_service_worker.ts` 里的 `ServiceWorkerMLCEngineHandler` 类。

## 4. 核心概念与源码讲解

### 4.1 ServiceWorkerMLCEngineHandler：继承与复用

#### 4.1.1 概念说明

`ServiceWorkerMLCEngineHandler` 是跑在 **Service Worker 脚本内**的处理器，任务是接收所有页面发来的 `WorkerRequest`，转交给内部的 `MLCEngine` 真身执行，再把结果送回「发出请求的那一个页面」。

它与 u5-l2 精读过的 `WebWorkerMLCEngineHandler` 是继承关系。为什么必须继承？因为 Service Worker 场景与普通 Web Worker 场景有**三点本质不同**：

1. **一对多**：Web Worker 只服务创建它的那一个页面，回消息时无需选择目标；Service Worker 同时服务多个页面，每条响应必须路由给正确的页面。
2. **生命周期不由页面控制**：浏览器可能在任务执行中途回收 Service Worker，需要 `waitUntil` 续命；也可能在空闲时杀掉它，导致「页面以为模型已加载、worker 里其实已空」，需要 reload 去重与失配自愈。
3. **需要心跳**：页面要定期发 `keepAlive` 消息维持 Service Worker 的存活，处理器要回 `heartbeat`。

继承让「消息路由到引擎方法」这 19 种 case 的通用逻辑原封不动地被复用，子类只补差异部分。

#### 4.1.2 核心流程

Service Worker 内的消息处理流程：

```text
页面 postMessage(WorkerRequest)
        │
        ▼
self.addEventListener("message")            ← Service Worker 全局监听
        │
        ├─ clientRegistry.set(uuid, message.source)   # 记下「这个 uuid 属于哪个页面」
        │
        └─ event.waitUntil( 包裹 onmessage 的 Promise )  # 告诉浏览器：活没干完别回收我
                 │
                 ▼
        handler.onmessage(event)
                 │
                 ├─ kind === "keepAlive" ──→ 回 {kind:"heartbeat"}，结束
                 │
                 ├─ kind === "reload" ──→ 与影子状态比较 modelId/chatOpts
                 │        ├─ 相同：跳过加载，直接报告 progress=1（"Finish loading on ..."）
                 │        └─ 不同：记录 initRequestUuid → engine.reload → 更新影子状态
                 │
                 └─ 其余 17 种 kind ──→ super.onmessage(msg)   # 基类通用路由
                                          │
                                          ▼
                                   handleTask(uuid, task)
                                   成功 → postMessage({kind:"return", uuid, content})
                                   失败 → postMessage({kind:"throw",  uuid, content:错误字符串})
                                          │
                                          ▼
                                   postMessage 按 uuid 查 clientRegistry
                                   定向发回对应页面；return/throw 后删除该表项
```

其中「影子状态」指处理器自己记录的 `modelId` / `chatOpts`（基类字段），用于判断模型是否已在当前 Service Worker 中加载。

#### 4.1.3 源码精读

**(1) 类声明与构造函数：注册表 + 环境守卫 + 全局监听**

[src/service_worker.ts:38-72](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L38-L72)：类声明、`clientRegistry` 映射表的定义，以及构造函数。构造函数做四件事：

- 环境守卫：如果 `self` 上没有 `addEventListener`，说明不在 worker 环境里，抛 `NonWorkerEnvironmentError`（错误定义见 [src/error.ts:367-372](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L367-L372)）。
- 调 `super()` 走基类构造（创建真身 `MLCEngine`、生成器 Map、注册默认进度回调，见 [src/web_worker.ts:84-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L84-L98)）。
- **重设进度回调**（[src/service_worker.ts:52-59](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L52-L59)）：基类把进度消息的 uuid 硬编码为空串，子类改为 `this.initRequestUuid || ""`——原因见第 (2) 点。
- 在 `self` 上挂全局 `message` 监听（[src/service_worker.ts:61-71](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L61-L71)）。注意对比：基类**不自己挂监听**，普通 Web Worker 由脚本末尾的 `onmessage = handler.onmessage` 接线（见 [src/web_worker.ts:50-60](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L50-L60) 的用法示例）。

监听器内部有两行关键代码（[src/service_worker.ts:62-70](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L62-L70)）：

```ts
if (message.source) {
  this.clientRegistry.set(message.data.uuid, message.source);
}
message.waitUntil(
  new Promise((resolve, reject) => {
    onmessage(message, resolve, reject);
  }),
);
```

- `message.source` 是发送方的 `Client`（即某个页面）。以请求的 `uuid` 为键登记它，就解决了「一对多路由」问题：回消息时按 uuid 查表即可定向送达。
- `waitUntil(promise)` 把整个消息处理包成一个 promise 交给浏览器——模型下载动辄几十秒，没有这一句，Service Worker 可能在任务中途被判定空闲而回收。

**(2) postMessage 重写：按 uuid 定向回包**

[src/service_worker.ts:74-85](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L74-L85)：基类的 `postMessage` 是 Web Worker 的全局函数（[src/web_worker.ts:100-103](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L100-L103)），发出去就行；子类重写后先查 `clientRegistry`，找到对应页面再 `client.postMessage(message)`，且当消息是 `return` / `throw`（一次性响应）时删除该表项，避免内存泄漏。

这也解释了 (1) 中为什么要改进度回调的 uuid：`postMessage` 只会发往**注册过的** uuid。模型下载进度必须送达「发起 reload 的那个页面」，所以 reload 时要把当时的请求 uuid 存进 `initRequestUuid`，供后续所有进度消息使用。

**(3) onmessage 重写：两个特例 + 全部兜底给基类**

[src/service_worker.ts:87-148](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L87-L148)：

- **keepAlive 特例**（[src/service_worker.ts:98-106](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L98-L106)）：回一个 `{kind: "heartbeat", uuid}`。`heartbeat` 是专门为 Service Worker 增加的响应类型，见 [src/message.ts:143-146](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L143-L146)；请求侧的 `keepAlive` 则是 19 种 kind 之一（[src/message.ts:35](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L35)）。
- **reload 特例**（[src/service_worker.ts:108-144](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L108-L144)）：这是本类存在的核心理由。多个页面各自调用 `CreateServiceWorkerMLCEngine` 都会触发一次 `reload` 请求，但 Service Worker 里模型只有一份——于是先用 `areArraysEqual` 与 `areChatOptionsListEqual`（[src/utils.ts:4-12](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/utils.ts#L4-L12) 等）比对影子状态；相同则**跳过** `engine.reload`，直接探测一次 GPU 并把 `progress: 1`、`text: "Finish loading on <GPU 描述>"` 的进度报告发回去（[src/service_worker.ts:117-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L117-L131)）；不同才执行真正的 `engine.reload`，并在事前记录 `initRequestUuid`、事后更新影子状态（[src/service_worker.ts:136-139](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L136-L139)）。对比基类的 reload case（[src/web_worker.ts:145-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L145-L156)）——无条件 reload，因为 Web Worker 场景下每个页面一份引擎，不存在「已经加载过」。
- **其余全部兜底**（[src/service_worker.ts:147](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L147)）：一行 `super.onmessage(msg, onComplete, onError)` 把消息交给基类。基类的巨型 switch（[src/web_worker.ts:134-358](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L134-L358)）负责 chatCompletion/completion/embedding/流式拉取/中断/卸载等全部路由，`handleTask`（[src/web_worker.ts:111-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L111-L132)）负责统一的 return/throw 包装——这些**一行未改地被 Service Worker 场景复用**。

顺带一个源码细节：keepAlive 若意外漏到基类 switch，会落入 default 分支；default 里只有当 `msg.kind && msg.content` 都存在时才抛 `UnknownMessageKindError`，否则视为「无关事件」忽略（[src/web_worker.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L348-L356)）。心跳消息不带 content，所以是安全的。

**(4) 失配自愈：Service Worker 被杀后的恢复**

基类还提供了 `reloadIfUnmatched`（[src/web_worker.ts:360-377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L360-L377)）：chatCompletion、completion、embedding 等请求会把「页面期望的 modelId」随请求带上（见 [src/message.ts:62-67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L62-L67) 的注释），处理器发现影子状态对不上（典型原因就是 Service Worker 被浏览器杀掉后重启、内存清空）时，先自动 `engine.reload` 再处理请求。这是「浏览器随时可能回收 Service Worker」这一约束的最后一道保险。

#### 4.1.4 代码实践

这个模块的实践**不需要 GPU**：`tests/service_worker.test.ts` 把 `MLCEngine` 和 `@mlc-ai/web-runtime` 全部 mock 掉，用假的 `self`、`navigator` 环境驱动真实源码。

1. **实践目标**：通过单测验证 4.1.3 中三个行为的真实存在——心跳应答、同模型跳过加载、异模型触发重载。
2. **操作步骤**：
   - 在仓库根目录执行：`npx jest tests/service_worker.test.ts`（不跑覆盖率，速度快）。
   - 打开 [tests/service_worker.test.ts:13-36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/service_worker.test.ts#L13-L36)，看 mock 的手法：`globalThis.self` 被替换为 `{addEventListener: jest.fn()}`，`navigator.serviceWorker` 被替换为带 `controller` / `ready` 的假容器——正因为构造函数只依赖这些最小接口，mock 才可行。
   - 对照三个用例阅读：心跳应答断言回包恰为 `{kind: "heartbeat", uuid: "keep"}`（[tests/service_worker.test.ts:106-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/service_worker.test.ts#L106-L120)）；同模型 reload 断言 `engine.reload` **未被调用**且进度回调收到 `progress: 1`（[tests/service_worker.test.ts:122-138](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/service_worker.test.ts#L122-L138)）；换模型则断言 `reload(["fresh"], [])` 被调用（[tests/service_worker.test.ts:140-153](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/service_worker.test.ts#L140-L153)）。
3. **需要观察的现象**：测试输出中每个用例的通过情况；以及用例如何通过 `(handler as any).handleTask` 替身拿到内部 task 函数直接 await。
4. **预期结果**：`tests/service_worker.test.ts` 的 7 个用例全部通过（该文件属于根目录 `npm test` 的一部分，jest 配置见 [package.json:11](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L11)）。本讲义写作环境未实际执行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`ServiceWorkerMLCEngineHandler` 相对基类新增/重写了哪些成员？各自解决什么问题？

**答案**：新增 `clientRegistry`（uuid → 页面 Client 的路由表）与 `initRequestUuid`（记录发起 reload 的请求 uuid，让进度消息可路由）；重写构造函数（环境守卫 + `self` 全局监听 + 进度回调改造）、`postMessage`（按 uuid 定向回包并在一次性响应后清理表项）、`onmessage`（新增 keepAlive→heartbeat 与 reload 去重两个特例，其余转发基类）。核心动机是 Service Worker 的「一对多」与「随时被回收」两个特性。

**练习 2**：跳过加载路径中发出的「Finish loading」进度消息，在「第二个窗口打开同一页面」的场景下能送达第二个窗口吗？给出基于源码的推断。

**答案**：不能（推断自源码，**待本地验证**）。跳过路径调用 `this.engine.getInitProgressCallback()` 报告进度（[src/service_worker.ts:127-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L127-L131)），该回调发出的消息 uuid 取自 `initRequestUuid`（[src/service_worker.ts:55](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L55)），而 `initRequestUuid` 只在**真正执行 reload 的分支**里更新为本次请求的 uuid（[src/service_worker.ts:136](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L136)）——跳过路径不经过这一行，于是沿用的是**上一个真实 reload**（第一个窗口）的 uuid；且该 uuid 在第一次 reload 完成回包时已从 `clientRegistry` 删除（[src/service_worker.ts:79-80](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L79-L80)），查表失败后消息被静默丢弃。第二个窗口的 reload promise 本身仍会正常 resolve（`handleTask` 用的是它自己的 uuid），只是收不到这条进度文本。

**练习 3**：为什么 `waitUntil` 包住的是 `onmessage` 整个执行过程，而不是只包 `engine.reload`？

**答案**：因为消息分发是在 `onmessage` 内部动态决定的——构造时无法预知哪条消息会触发耗时操作（reload、非流式 chatCompletion、流式 StreamInit 都可能长时间运行）。把整个处理过程包进 promise 交给 `waitUntil`，浏览器会认为 Service Worker 在 promise 结算前都有工作 pending，从而不会中途回收；对一次性请求，`handleTask` 内任务完成即回包，promise 随之结算，不会无限续命。

### 4.2 ServiceWorker 封装层：页面侧客户端

#### 4.2.1 概念说明

页面侧需要三件套把「Service Worker」伪装成 u5-l1 认识的「worker」：

1. **`ServiceWorker` 类**：实现 `ChatWorker` 接口（只有 `onmessage` + `postMessage`），把这两个方法分别代理到 `navigator.serviceWorker` 容器和它的 `controller`。它是「适配器」——让 `WebWorkerMLCEngine` 基类以为自己连的是一个普通 worker。
2. **`ServiceWorkerMLCEngine`**：继承 `WebWorkerMLCEngine`，差异只有两个：定期发心跳维持 Service Worker 存活；容忍多窗口场景下的未知 uuid 回包。
3. **`CreateServiceWorkerMLCEngine`**：工厂函数，等待 Service Worker 就绪后创建引擎并 reload 模型。

> 命名细节：DOM 全局本来就有 `ServiceWorker` 类型，所以源码用 `type IServiceWorker = globalThis.ServiceWorker`（[src/service_worker.ts:20](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L20)）把 DOM 概念让给类型、把类名留给自己的封装类。

#### 4.2.2 核心流程

以 `examples/service-worker` 为例，从页面加载到第一条流式回复：

1. 页面注册：`navigator.serviceWorker.register(new URL("sw.ts", import.meta.url), {type: "module"})`（[examples/service-worker/src/main.ts:3-21](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/src/main.ts#L3-L21)）。
2. Service Worker 收到 `activate` 事件，创建处理器：`handler = new ServiceWorkerMLCEngineHandler()`（[examples/service-worker/src/sw.ts:5-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/src/sw.ts#L5-L8)），构造函数挂上全局 message 监听。
3. 页面调用 `CreateServiceWorkerMLCEngine(modelId, {initProgressCallback})`（[examples/service-worker/src/main.ts:80-83](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/src/main.ts#L80-L83)）。
4. 工厂 `await navigator.serviceWorker.ready` 拿到激活的 registration，取 `registration.active || controller`，然后 `new ServiceWorkerMLCEngine(...)` 并 `reload`（[src/service_worker.ts:192-213](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L192-L213)）。
5. `ServiceWorkerMLCEngine` 构造把 `new ServiceWorker()` 当作 ChatWorker 传给基类（[src/service_worker.ts:221-225](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L221-L225)），并启动心跳定时器。
6. 之后的一切（reload 进度、非流式请求、StreamInit + NextChunk 拉模型流式、interruptGenerate）都是 u5-l1/u5-l2 讲过的信封消息往返，只是物理通道换成了页面 ↔ Service Worker。

#### 4.2.3 源码精读

**(1) ServiceWorker 类：ChatWorker 的 Service Worker 实现**

[src/service_worker.ts:152-179](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L152-L179)：

- `onmessage` 的 setter（[src/service_worker.ts:159-166](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L159-L166)）：把处理器函数挂到 `navigator.serviceWorker.onmessage` 上——Service Worker 发给本页面的消息从这里进来；若浏览器没有 serviceWorker API，抛 `NoServiceWorkerAPIError`（提示需要 HTTPS 等安全上下文，见 [src/error.ts:374-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L374-L381)）。
- `postMessage`（[src/service_worker.ts:168-178](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L168-L178)）：取 `navigator.serviceWorker.controller`（当前控制本页面的 Service Worker 实例）再发送。`controller` 为 null（页面尚未被控制）时抛 `"There is no active service worker"`。

**(2) ServiceWorkerMLCEngine：心跳与容错**

[src/service_worker.ts:218-253](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L218-L253)：

- 构造函数末尾启动 `setInterval`，每 `keepAliveMs`（默认 10 秒）发一条 `{kind: "keepAlive", uuid}` 并把 `missedHeartbeat` 加一（[src/service_worker.ts:227-232](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L227-L232)）。消息活动会重置浏览器对 Service Worker 的空闲计时；收到处理器回的 `heartbeat` 则把计数清零（[src/service_worker.ts:241-244](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L241-L244)）。应用层可以轮询 `missedHeartbeat` 来判断 Service Worker 是否疑似失联。
- `onmessage` 重写捕获一个特例：错误消息以 `"return from a unknown uuid"` 开头时静默忽略——源码注释明确说这是**用户开多个窗口时的预期现象**（[src/service_worker.ts:246-251](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L246-L251)），此时只降级为日志不向上抛。基类的 onmessage 遇到未知 uuid 本会抛错（[src/web_worker.ts:818-835](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L818-L835)），这里把它变成可容忍事件。
- 其余全部继承：`getPromise` 的 uuid 配对（[src/web_worker.ts:496-520](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L496-L520)）、流式的 `asyncGenerate` 拉模型循环、`chat`/`completions`/`embeddings` 三个门面，一行未改。

**(3) CreateServiceWorkerMLCEngine：工厂与环境检查**

[src/service_worker.ts:192-213](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L192-L213)：依次检查 API 存在性（否则 `NoServiceWorkerAPIError`）、`await serviceWorkerAPI.ready`（等待本 scope 内出现激活的注册）、取 `registration.active || controller`（都没有则 `ServiceWorkerInitializationError`，错误文案建议用户刷新页面重试，见 [src/error.ts:384-391](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L384-L391)），最后 `new ServiceWorkerMLCEngine(...)` 并 `await reload(modelId, chatOpts)`。注意它**不负责注册**——注册必须由页面自己先完成（对比 `CreateMLCEngine` / `CreateWebWorkerMLCEngine` 都是「构造 + reload」的等价 shortcut，这一约定三个工厂一致）。

一个实用的坑：`navigator.serviceWorker.controller` 只有在「页面加载时 Service Worker 已处于激活状态」才非空，**首次访问**注册后立即调用可能拿到 null 而撞上 `"There is no active service worker"`——这正是错误文案让你刷新页面的原因。**待本地验证**（取决于浏览器对首次注册页面的控制时机）。

#### 4.2.4 代码实践

1. **实践目标**：亲手跑通网页版 Service Worker 示例，观察「消息流」与「多页面共享同一份模型」两个现象。
2. **操作步骤**：
   - `cd examples/service-worker && npm install`，然后 `npm start`（Parcel 在 3000 端口起服务，见 [examples/service-worker/package.json:6-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/package.json#L6-L7)；README 给出的 `npm run build` 只产出静态文件，本地调试用 `npm start` 更方便，见 [examples/service-worker/README.md:5-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/README.md#L5-L8)）。localhost 属于安全上下文，满足 Service Worker 要求。
   - 用支持 WebGPU 的浏览器打开 `http://localhost:3000`。若控制台报 "There is no active service worker"，刷新一次页面再试（原因见 4.2.3 (3)）。
   - 打开 DevTools → Application → Service Workers，点击该 SW 的链接打开**属于 Service Worker 的独立 console**；再开 Network 面板观察权重下载。
   - 等待 `init-label` 走完进度，页面开始流式输出（`main.ts` 默认执行 `mainStreaming()`，见 [examples/service-worker/src/main.ts:117-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/service-worker/src/main.ts#L117-L120)）。
   - **关键实验**：再开第二个标签页访问同一地址。在 Service Worker 的 console 里寻找 `Already loaded the model. Skip loading`（这条 `log.info` 来自 [src/service_worker.ts:116](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L116)），并对比两个标签页从发起到可对话的耗时与 Network 面板的下载量。
   - 在 Service Worker 的 console 里输入 `console.log` 之外的任何操作都会失败（无 DOM），体会它与页面环境的隔离。
3. **需要观察的现象**：首个标签页的下载进度文本；Service Worker console 中的消息 trace（可把 `engineConfig.logLevel` 设为 `"TRACE"` 观察 `[kind]` 日志，见 [src/service_worker.ts:93-95](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L93-L95)）；第二个标签页触发跳过加载的日志；心跳消息每 10 秒一次的痕迹。
4. **预期结果**：第二个标签页不重新下载权重、不重新初始化 GPU 管线，很快进入可对话状态；同一时刻两个标签页共用同一个 Service Worker 进程。具体耗时数字**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`CreateServiceWorkerMLCEngine` 为什么要 `await navigator.serviceWorker.ready` 而不是直接用 `controller`？

**答案**：`ready` 返回的 promise 在本 scope 内存在**激活的** registration 时才结算，它同时兜住了「注册尚在进行中」「正在等待旧版本释放」等情况；而 `controller` 在页面尚未被控制时是 null（典型如首次访问），直接用会误判为没有 Service Worker。代码两者都取：`registration.active || controller`，最大化拿到可用实例的概率（[src/service_worker.ts:202-206](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L202-L206)）。

**练习 2**：心跳机制里 `missedHeartbeat` 计数器有什么用？谁负责清零？

**答案**：引擎每发一次 keepAlive 就自增（[src/service_worker.ts:228-232](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L228-L232)），收到处理器回的 heartbeat 才清零（[src/service_worker.ts:241-244](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L241-L244)）。它是一个对应用暴露的健康信号：计数持续增长说明 Service Worker 已被终止或失联，应用可据此提示用户或主动重建引擎；WebLLM 本身没有基于它做自动重连（自愈靠请求路径上的 `reloadIfUnmatched`）。

**练习 3**：`ServiceWorker` 类为什么必须实现 `ChatWorker` 接口？

**答案**：`WebWorkerMLCEngine` 基类的构造函数只依赖 `worker.onmessage` 与 `worker.postMessage` 两个成员（[src/web_worker.ts:443-447](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L443-L447)，接口定义在 [src/web_worker.ts:380-383](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L380-L383)）。只要满足这个形状，真 Web Worker、Service Worker、Chrome Port（下一节的 `PortAdapter`）、甚至测试里的假对象都能接入同一个引擎代理——这是整个 Worker 架构「无感切换」的支点。

### 4.3 extension_service_worker：Chrome MV3 扩展适配

#### 4.3.1 概念说明

Chrome 扩展（MV3）的后台脚本也是一个 Service Worker，但通信方式与网页版不同：

- 网页版：页面 ↔ Service Worker 之间用 `navigator.serviceWorker.controller.postMessage`，回包目标是 `Client`。
- 扩展版：popup/options 等扩展页面通过 `chrome.runtime.connect()` 与后台建立一条**长连接 Port**，收发都走这个 Port；回包不需要注册表——Port 本身就是点对点的。

于是 [src/extension_service_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts) 里出现了一组**同名但通道不同**的类。它仍继承 `WebWorkerMLCEngineHandler`，复用全部消息路由；差异集中在三处：postMessage 走 Port、reload 去重逻辑（与网页版几乎相同但独立维护了一份）、页面侧用 `PortAdapter` 把 Port 适配成 `ChatWorker`。

另一个扩展特有的约束是 MV3 Service Worker 的空闲回收更激进（约 30 秒无事件即终止，源码注释直接链接了 Chrome 官方文档，见 [src/extension_service_worker.ts:113-115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L113-L115)），所以心跳在这里不是可选项。

#### 4.3.2 核心流程

以 `examples/chrome-extension-webgpu-service-worker` 为例：

```text
popup.ts（扩展页面）
  └─ CreateExtensionServiceWorkerMLCEngine(model, config)
       └─ chrome.runtime.connect({name:"web_llm_service_worker"})   ← 建立长连接 Port
            └─ PortAdapter 包装 Port，充当 ChatWorker 传给基类
                 └─ ServiceWorkerMLCEngine(扩展版)
                      ├─ 每 keepAliveMs 往 Port 发一条 {kind:"keepAlive"}
                      └─ reload/chatCompletion 等照常发 WorkerRequest 信封

background.ts（扩展的 Service Worker）
  └─ chrome.runtime.onConnect.addListener(port => {
        若断言 port.name === "web_llm_service_worker"
        handler 未创建 → new ServiceWorkerMLCEngineHandler(port)   ← 扩展版
        否则          → handler.setPort(port)                       ← 复用引擎，只换通道
        port.onMessage.addListener(handler.onmessage.bind(handler))
     })
       └─ 消息进入基类路由（keepAlive 除外），结果经 port.postMessage 回传

popup 关闭
  └─ Port 断开
       ├─ 引擎侧：清除心跳定时器，调用可选的 onDisconnect 回调
       └─ 处理器侧：onPortDisconnect 把 port 置 null（引擎与模型仍在 Service Worker 里，
          直到它因空闲被浏览器终止）
```

注意「handler 已存在则 `setPort(port)`」这一分支：popup 每次打开都是新 Port，但处理器（及其内部的 `MLCEngine` 与已加载模型）只有一个——这正是「模型在扩展内只加载一份、被反复打开的 popup 复用」的实现基础（[examples/chrome-extension-webgpu-service-worker/src/background.ts:6-14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/background.ts#L6-L14)）。

#### 4.3.3 源码精读

**(1) 扩展版处理器：Port 生命周期管理**

[src/extension_service_worker.ts:34-56](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L34-L56)：构造函数收一个 `chrome.runtime.Port`，注册断连监听；`postMessage` 直接 `this.port?.postMessage(msg)`——点对点通道，**不需要**网页版的 `clientRegistry`；`setPort` 供后续新 popup 接入时更换通道并重新挂断连监听；`onPortDisconnect` 在断开的正是当前 port 时把它置 null。

**(2) 扩展版 onmessage：keepAlive 与 reload 两个特例**

[src/extension_service_worker.ts:58-101](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L58-L101)：

- keepAlive 特例只做一件事：**直接 return，不转发基类**（[src/extension_service_worker.ts:58-61](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L58-L61)）。心跳消息在这里的意义不是求回复，而是让 Port 上出现一次消息活动、重置 MV3 的空闲计时器。严格读代码会发现它的判断条件是 `event.type === "keepAlive"`，而引擎实际发出的是 `{kind: "keepAlive"}` 形状的消息（[src/extension_service_worker.ts:184-186](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L184-L186)），`type` 字段并不存在——这条判断实际上永不命中；漏过去的心跳会落入基类 default 分支的「忽略无关事件」路径（不带 content），同样无害（[src/web_worker.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L348-L356)）。与网页版不同，扩展版**不回 heartbeat**，引擎侧也没有 `missedHeartbeat` 计数器。
- reload 特例（[src/extension_service_worker.ts:64-97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L64-L97)）与网页版逻辑相同（同样的 `areArraysEqual` + `areChatOptionsListEqual` 去重、同样的「Finish loading on <GPU>」进度报告），细微差别是 GPU 探测失败时抛的是类型化的 `WebGPUNotFoundError`（[src/extension_service_worker.ts:73-75](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L73-L75)），网页版抛的是普通 `Error`（[src/service_worker.ts:117-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L117-L120)）。同样**不使用 `waitUntil`**——Port 消息没有这个 API，MV3 的存活靠消息活动本身维持。
- 其余 kind 照旧 `super.onmessage(event)`（[src/extension_service_worker.ts:100](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L100)）。

**(3) PortAdapter：把 Port 变成 ChatWorker**

[src/extension_service_worker.ts:132-161](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L132-L161)：实现 `ChatWorker` 接口——构造时在 `port.onMessage` 上挂一个转发监听，收到消息就调用自己的 `_onmessage`（即引擎代理稍后 set 进来的处理函数）；`postMessage` 用箭头函数属性绑定 `this` 后代理到 `port.postMessage`。这是 4.2.5 练习 3 所说「接口支点」的第二个实例。

**(4) 扩展版引擎与配置**

- `ExtensionMLCEngineConfig` 扩展了 `MLCEngineConfig`，新增 `extensionId`（连接**其他**扩展的 Service Worker，走 `chrome.runtime.connect(extensionId, ...)`，见 [src/extension_service_worker.ts:13-16](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L13-L16) 与 [src/extension_service_worker.ts:170-178](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L170-L178)）和 `onDisconnect`（Port 断开时回调）。
- 引擎构造：连接 Port（名字固定为 `"web_llm_service_worker"`，background 侧有 `console.assert` 校验），包 `PortAdapter` 传给基类，启动心跳定时器；Port 断开时清除定时器并调用 `onDisconnect`（[src/extension_service_worker.ts:183-193](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L183-L193)）。定时器不清除会一直给已断开的 Port 发消息并阻止后台休眠，这个清理是扩展场景避免「僵尸心跳」的关键。
- 工厂 `CreateServiceWorkerMLCEngine`（扩展版，[src/extension_service_worker.ts:118-130](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L118-L130)）同样是「构造 + reload」，对外经 `index.ts` 别名为 `CreateExtensionServiceWorkerMLCEngine`。

**(5) 示例三件套：manifest、background、popup**

- [examples/chrome-extension-webgpu-service-worker/src/manifest.json:12-14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/manifest.json#L12-L14)：CSP 中 `script-src 'self' 'wasm-unsafe-eval'` 是加载 wasm 模型库的前提，`connect-src` 白名单放行了 HuggingFace 相关域名供下载权重；[manifest.json:25-28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/manifest.json#L25-L28) 声明 `background.service_worker` 与 `type: "module"`。
- [examples/chrome-extension-webgpu-service-worker/src/popup.ts:48-51](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/popup.ts#L48-L51)：popup 顶层 `await CreateExtensionServiceWorkerMLCEngine("Qwen2-0.5B-Instruct-q4f16_1-MLC", {...})`，之后 `engine.chat.completions.create({stream: true, ...})` 的用法与主线程引擎完全一致——代理模式的收益再次显现。
- README 提醒：Service Worker 中的 WebGPU 自 Chrome 124 起默认启用，更早版本需手动打开实验 flag（[examples/chrome-extension-webgpu-service-worker/README.md:5-7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/README.md#L5-L7)）。

#### 4.3.4 代码实践

1. **实践目标**：在不一定加载扩展的前提下，读懂扩展示例的结构与消息流；有条件时完整跑通它。
2. **操作步骤**：
   - 先做**源码阅读部分**：按「manifest.json（确认 background.service_worker 与 CSP）→ background.ts（onConnect 接线）→ popup.ts（CreateExtensionServiceWorkerMLCEngine 与流式消费）」的顺序通读三个文件，画出 4.3.2 那张时序图的实例版本。
   - 跑一遍无 GPU 依赖的单测验证扩展版行为：`npx jest tests/extension_service_worker.test.ts`。重点看两个用例：同模型 reload 不触发 `engine.reload` 但触发进度回调、换模型触发重载（[tests/extension_service_worker.test.ts:70-94](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/extension_service_worker.test.ts#L70-L94)）；以及 mock `chrome.runtime` 验证心跳与 `onDisconnect` 的用例（[tests/extension_service_worker.test.ts:117-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/extension_service_worker.test.ts#L117-L120) 起）。注意它 mock 的手法是用 `createPort()` 造一个带 `onMessage/onDisconnect` 监听器数组的假 Port——真实 Chrome API 的最小形状。
   - **可选的完整运行**（需要 Chrome ≥ 124）：`cd examples/chrome-extension-webgpu-service-worker && npm install && npm run build`（用 `@parcel/config-webextension` 构建，见 [package.json:7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/package.json#L7)），然后在 Chrome 的 Extensions → Manage Extensions → Load Unpacked 加载 `dist/` 目录，点开扩展图标对话（步骤来自 [README:16-23](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/README.md#L16-L23)）。
   - **关键实验**：对话一次后关闭 popup，等约 40 秒再重新打开 popup 提问。对照 `chrome://serviceworker-internals`（或扩展管理页的 Service Worker 状态）观察后台 SW 的 active/terminated 状态变化。
3. **需要观察的现象**：重开 popup 后，是否出现 `reloadIfUnmatched` 的自愈日志（SW 被杀后重建）；若 SW 未被杀（心跳仍在），重开后首个问题是否几乎即时响应（模型仍在内存）——两条路径的差异正是本讲的核心主题。
4. **预期结果**：SW 存活时重开 popup 秒级可用；SW 被终止后重开 popup 需要走一遍 reload（权重命中 CacheStorage，不重新联网下载，但 wasm/GPU 初始化仍需时间）。具体阈值与日志**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：manifest.json 里哪两处配置是「在扩展 Service Worker 里跑 WebGPU + wasm 推理」的硬前提？

**答案**：其一，CSP 的 `script-src` 必须含 `'wasm-unsafe-eval'`，否则实例化 wasm 模型库会被 CSP 拦截（[manifest.json:12-14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/manifest.json#L12-L14)）；其二，`background.service_worker` 指向后台脚本且 `type: "module"`，使 `background.ts`（及其 import 的 WebLLM 库）能作为 MV3 Service Worker 运行（[manifest.json:25-28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/chrome-extension-webgpu-service-worker/src/manifest.json#L25-L28)）。此外 `connect-src` 白名单决定了能从哪些域名下载模型。

**练习 2**：popup 关闭后，模型还在内存里吗？什么时候会不在？

**答案**：popup 关闭只是 Port 断开——处理器把 `port` 置 null（[src/extension_service_worker.ts:52-56](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b7d76/src/extension_service_worker.ts#L52-L56)），引擎侧清掉心跳定时器（[src/extension_service_worker.ts:188-193](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L188-L193)），但 `MLCEngine` 与模型仍留在后台 Service Worker 进程里。心跳一停，SW 很快空闲，被浏览器终止时内存中的模型权重与 GPU 资源随之回收——下次消息到达再由 `reloadIfUnmatched` 从缓存自愈。换言之：「在内存里」的窗口期 ≈ 最后一次消息活动后的空闲宽限期。

**练习 3**：扩展版处理器里 `event.type === "keepAlive"` 这个判断，与引擎发出的心跳消息形状匹配吗？后果是什么？

**答案**：不匹配。引擎发的是 `{kind: "keepAlive"}`（无 `content`、无 `type` 字段），所以该判断永不命中；心跳随后进入 `super.onmessage`，在基类 default 分支因「kind 存在但 content 不存在」而被当作无关事件忽略（[src/web_worker.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L348-L356)），不会抛 `UnknownMessageKindError`。心跳的目的（在 Port 上制造消息活动以重置空闲计时）仍然达成，只是「扩展版不回 heartbeat」这一点与网页版不同，引擎侧也确实没有对应的心跳计数器。

## 5. 综合实践

**任务：对比 Web Worker 与 Service Worker 两种方案，写一份《共享与代价》说明**。这是本讲规格中指定的实践，综合了全部三个模块。

第一步：跑通网页版示例（见 4.2.4），记录一条消息从 `engine.chat.completions.create` 到首个 chunk 的路径，填出下表（左列已给出答案，右列自己补全）：

| 环节 | Web Worker 方案（u5-l1） | Service Worker 方案（本讲） |
|---|---|---|
| 通道建立 | 页面 `new Worker(url)` | 页面 `register` + SW `activate` 时 `new ServiceWorkerMLCEngineHandler()` |
| 请求如何发出 | `worker.postMessage(信封)` | `navigator.serviceWorker.controller.postMessage(信封)` |
| 处理器如何收到 | 脚本 `onmessage = handler.onmessage` | `self.addEventListener("message")` + `waitUntil` |
| 回包如何定向 | 无需定向（唯一页面） | `clientRegistry` 按 uuid 查 `Client` |
| 模型加载几次 | 每个页面各一次 | 每个 SW 一次，后续 reload 去重跳过 |

第二步：打开 `examples/chrome-extension-webgpu-service-worker`，对照 4.3.2 的时序图阅读三个文件，把上表「Service Worker 方案」一列再细分出「网页版 SW」与「扩展版 SW（Port）」两列的差异（提示：通道、回包定向方式、心跳是否有应答、是否用 `waitUntil`）。

第三步：写一段 300 字左右的说明，回答两个问题（这是本实践的交付物）：

1. **共享**：与普通 Web Worker 方案相比，Service Worker 方案让模型在哪些场景下可以被多个页面共享？至少覆盖：同源多标签页应用（如多窗口助手类站点）、Chrome 扩展中 popup/options/content script 共用一个后台引擎、PWA 前后台页。说明共享的粒度是「同一份 GPU 显存中的权重与 KV cache」，并引用 `clientRegistry`（[src/service_worker.ts:39-43](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L39-L43)）与 reload 去重（[src/service_worker.ts:108-144](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/service_worker.ts#L108-L144)）作为证据。
2. **代价**：生命周期与内存回收两方面。生命周期——模型不再随页面生灭，而是随 SW 生灭，浏览器空闲即回收（MV3 约 30 秒），因此必须心跳续命、必须靠 `reloadIfUnmatched` 自愈（[src/web_worker.ts:360-377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L360-L377)）、任务执行中要 `waitUntil` 防中途回收；内存——心跳把一份可能数 GB 的权重长期钉在内存/显存里，即使没有任何页面在使用；此外错误跨线程退化为字符串、进度回包在多窗口下存在 4.1.5 练习 2 分析的送达缺口。

评判标准：说明中的每个论断都能落到一个具体文件与行号；对「模型什么时候真的从内存消失」给出了基于心跳与空闲终止的机制性解释，而不是「关闭页面就没了」。

## 6. 本讲小结

- `ServiceWorkerMLCEngineHandler` 继承 `WebWorkerMLCEngineHandler`，原封复用 19 种消息路由与 `handleTask` 包装，只重写三处：构造（全局监听 + `waitUntil` + 进度回调改造）、`postMessage`（`clientRegistry` 按 uuid 定向回包）、`onmessage`（keepAlive→heartbeat 与 reload 去重两个特例）。
- reload 去重是 Service Worker 场景的专属逻辑：多页面共享一份引擎，「模型已加载」时跳过 `engine.reload` 直接报告 `progress: 1`；配合请求路径上的 `reloadIfUnmatched`，构成「SW 被杀后自动恢复」的自愈闭环。
- 页面侧三件套：`ServiceWorker` 类把 `navigator.serviceWorker` 适配成 `ChatWorker`，`ServiceWorkerMLCEngine` 加上心跳与多窗口容错，`CreateServiceWorkerMLCEngine` 负责 `ready` 等待与「构造 + reload」。
- `extension_service_worker.ts` 是同一模式的 MV3 变体：通道换成 `chrome.runtime.Port`（`PortAdapter` 适配），回包免注册表，心跳只发不收、不回 heartbeat；经 `index.ts` 以 `Extension*` 别名导出，与网页版同名类共存。
- 两处值得注意的源码细节：跳过加载路径的进度消息复用旧 uuid 可能无法送达新窗口；扩展版 `event.type === "keepAlive"` 判断与实际消息形状不符（实际落入基类「忽略无关事件」分支，无害）。
- 测试方面，`tests/service_worker.test.ts` 与 `tests/extension_service_worker.test.ts` 通过 mock `MLCEngine`、`tvmjs`、`self`/`navigator`/`chrome.runtime`，在无 GPU 环境完整覆盖了上述行为。

## 7. 下一步学习建议

本讲完成后，第五单元（Worker 架构）收官。建议：

1. **动手巩固**：完成第 5 节综合实践的对比说明；如果把 4.2.4 的双标签页实验做完了，试着把 `keepAliveMs` 调大到 60000 再观察 SW 是否更容易被回收（**待本地验证**）。
2. **进入第六单元**（OpenAI 兼容协议深度）：下一讲 u6-l1「OpenAI 协议层设计与请求校验」将下钻 `openai_api_protocols/`，理解你这三讲里反复调用的 `engine.chat.completions.create` 背后的类型体系与校验机制。
3. **源码延伸阅读**：`src/web_worker.ts` 的 `reloadIfUnmatched` 注释中链接的 PR #533 与 `src/message.ts:62-67` 注释中链接的 PR #471，记录了 Service Worker 生命周期问题在真实 issue 中如何演进，值得一看。
