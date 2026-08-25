# 多模型管理与动态切换

## 1. 本讲目标

学完本讲，你应该能够：

1. 精确说出 `reload()` 切换模型时旧 pipeline 的资源释放全链路：从 `dispose()` 逐项释放 GPU 对象，到 `device.sync()` 等待真正销毁，再到四个状态 Map 的清空与进行中加载的中止。
2. 理解引擎内部「多 pipeline 字典管理」的设计：四个以 modelId 为键的 Map、每模型一把 `CustomLock`、请求如何被路由到正确的模型，以及多模型并存时的显存账本怎么算。
3. 掌握中断与恢复的完整语义：`interruptGenerate()` 是引擎级信号而非模型级，abort 之后引擎靠哪些复位动作回到可用状态，以及 device lost 这类灾难路径的自愈逻辑。

本讲是第七单元（高级主题）的第二篇，建立在 u2-l1（引擎生命周期）、u4-l1（缓存机制）之上，并把 u5-l1 中「Worker 双端记录期望模型清单」的多模型背景补全到主线程引擎层面。

## 2. 前置知识

- **pipeline（管线）**：一个 `LLMChatPipeline` 实例 = 一份模型权重 + 一段 KV cache + 一组 PackedFunc 句柄 + 一个独立的 tvmjs 运行时。可以把它理解为「一个已加载模型的全部 GPU 资源」。
- **modelId 与 ModelRecord**：`model_id` 是 `appConfig.model_list` 中的逻辑查找键（u1-l4），引擎的一切多模型状态都以 modelId 为键。
- **KV cache 与满分页分配**：管线构造时按 `context_window_size` 一次性分配 KV cache（u3-l1），这意味着显存占用在加载时就全部锁定，之后不再增长。
- **CustomLock**：WebLLM 自制的异步互斥锁（u2-l1 提过引擎构造时初始化锁 Map）。请求开始前 `acquire()`，结束后 `release()`，保证同一模型串行处理请求。
- **WebGPU device 的异步性**：GPU 资源的销毁是异步的，`device.lost` 是一个 Promise，设备丢失（常见于显存耗尽）会在事后才触发。
- **abort 与 AbortController**：Web 标准 API。`controller.abort()` 会让所有传入 `controller.signal` 的 `fetch` 抛出 `DOMException`（`name === "AbortError"`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 引擎主体：四个状态 Map、`reload`/`reloadInternal`/`unload`、`interruptGenerate`、`getModelStates` 路由 |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 推理管线：`dispose()` 资源释放清单、`triggerStop()` 中断落地、`prefillStep` 开头的状态复位 |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | `getModelIdToUse` 模型路由决策、`CustomLock` 实现 |
| [src/error.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts) | 本讲涉及的错误类：`UnclearModelToUseError`、`SpecifiedModelNotFoundError`、`ReloadModelIdNotUniqueError`、`DeviceLostError` 等 |
| [src/web_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts) | Worker 代理层的 `interruptGenerate` / `unload` 转发 |
| [examples/multi-models/src/main.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts) | 官方多模型示例：一次 reload 加载两模型，串行与并行两种生成方式 |
| [examples/abort-reload/src/get_started.js](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/src/get_started.js) | 官方中断加载示例：reload 进行中调用 unload 取消下载 |

## 4. 核心概念与源码讲解

### 4.1 reload 卸载旧模型流程

#### 4.1.1 概念说明

「切换模型」在 WebLLM 中是一个**全量换血**操作：`reload()` 的第一步永远是 `unload()` 所有已加载模型，然后从零开始逐个加载新清单里的模型。引擎**没有**「往已加载集合里追加一个模型」或「只卸载其中一个模型」的公开 API——多模型共存只能在**一次** `reload` 调用中把 modelId 数组一次性声明。

这个设计带来两个直接后果：

1. **切换成本高**：A 切到 B 必须 dispose 掉 A 的全部 GPU 资源再完整加载 B，即使 B 已在缓存里也要重新实例化 wasm、重新分配 KV cache。
2. **状态一致性简单**：任何时刻引擎内的模型集合都来自最近一次 `reload` 的参数，不存在「半个新半个旧」的中间态。

#### 4.1.2 核心流程

`reload(modelId, chatOpts)` 的执行序列：

```text
reload(modelId)
  ├── 0. await unload()               # 释放所有旧模型（见下）
  ├── 1. 参数归一化为数组               # "m" -> ["m"]，chatOpts 同理
  ├── 2. 校验 modelId 与 chatOpts 数量匹配，否则 ReloadArgumentSizeUnmatchedError
  ├── 3. 校验 modelId 无重复，否则 ReloadModelIdNotUniqueError
  ├── 4. new AbortController()         # 一个 abort 管住本次所有待加载模型
  └── 5. for 每个 modelId: await reloadInternal(id, chatOpts[i])
        └── catch AbortError -> 仅告警并静默返回（被 unload() 打断的合法路径）
```

`unload()` 的资源释放序列：

```text
unload()
  ├── deviceLostIsError = false        # 压住 dispose 引发的 device.lost 错误路径
  ├── for 每个 pipeline（顺序执行）:
  │     pipeline.dispose()             # 逐项释放 GPU 对象（见 4.1.3）
  │     await pipeline.sync()          # 等 device.sync()，确保设备真正销毁
  ├── 清空四个 Map（pipeline/config/modelType/lock）
  ├── deviceLostIsError = true         # 恢复错误监听
  └── 若 reloadController 存在: abort 它  # 取消仍在进行的下载
```

注意那个 `deviceLostIsError` 标志的「双态舞步」：dispose 大量 GPU 缓冲区可能触发设备丢失，而设备丢失的回调里本来会调用 `this.unload()` 并报错（见 4.1.3 的第三段代码）。卸载期间把这个标志置 false，就是告诉回调「这次设备变动是计划内的，别当错误处理」；等 `sync()` 确认设备销毁完毕后再恢复为 true。

#### 4.1.3 源码精读

先看 `reload` 的入口骨架——第 0 步无条件卸载，随后是三道参数校验和顺序加载循环。[src/engine.ts:203-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L203-L246)：这段代码先 `await this.unload()` 清场，把参数统一转成数组，校验数量匹配与唯一性，然后创建一个 `AbortController` 并循环调用 `reloadInternal` 逐个加载；若中途收到 `AbortError`（说明被 `unload()` 打断），只打一条警告日志就正常返回，不向上抛。

`unload` 的完整实现。[src/engine.ts:432-451](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L432-L451)：这段代码先把 `deviceLostIsError` 置 false 以屏蔽计划内的设备销毁，然后**顺序**遍历所有 pipeline 调用 `dispose()` 并 `await pipeline.sync()` 等待设备真正销毁（注释里明确说并行化是待优化项），再清空四个状态 Map、恢复标志位，最后 abort 掉可能仍在进行的 `reloadController`——这正是 abort-reload 示例能取消下载的机制来源。

pipeline 侧的 `dispose()` 清单。[src/llm_chat.ts:486-508](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L486-L508)：这段代码逐个释放管线持有的 GPU 与 CPU 资源——权重 `params`、KV cache、prefill/decoding/embed 等 PackedFunc、采样用的临时 buffer、VirtualMachine、tvm 运行时、tokenizer，以及结构化输出相关的 `grammarMatcher` 与 `grammarCompiler`。可选项（如 `image_embed`、`rnnState`）用 `?.` 安全跳过。dispose 一个 pipeline 就是把一个模型的全部显存与内存占用归还系统。

设备丢失的监听与自愈。[src/engine.ts:374-384](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L374-L384)：这段代码在每次 `reloadInternal` 检测到 GPU 设备后注册 `device.lost.then(...)` 回调——若 `deviceLostIsError` 为 true（即非计划内卸载），就记录错误日志、主动调用 `this.unload()` 清理全部引擎状态，并把 `deviceLostInReload` 置 true。配合 [src/engine.ts:427-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L427-L429)（`reloadInternal` 末尾检查该标志并同步抛出 `DeviceLostError`，错误类定义在 [src/error.ts:267](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L267)），一个本应异步冒出的设备丢失事件被收敛成 reload 结尾的一次同步异常，调用方能用普通的 try/catch 接住。这也解释了 `CreateMLCEngine` 文档注释中「设备丢失（多为显存不足）时应换更小模型重试」的建议。

abort-reload 示例如何利用这一切。[examples/abort-reload/src/get_started.js:24-32](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/src/get_started.js#L24-L32)：这段示例代码**不 await** `engine.reload(selectedModel)`（让它自己在后台跑），5 秒后调用 `engine.unload()`——后者会 abort `reloadController`，使仍在进行的 config/wasm/权重 fetch 抛出 `AbortError`，被 reload 的 catch 分支吞掉，于是加载被干净地取消且不报错。

#### 4.1.4 代码实践

**实践目标**：亲手验证「reload 可被 unload 打断」与「打断不产生错误」。

**操作步骤**：

1. 进入 `examples/abort-reload/` 目录，执行 `npm install` 然后 `npm start`（Parcel 会在 8887 端口起服务，见 [examples/abort-reload/README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/README.md)）。
2. 打开浏览器控制台，刷新页面，观察 `initProgressCallback` 打出的下载进度日志。
3. 把 `get_started.js` 末尾的 `setTimeout` 延时从 5000 改成 15000（超过下载总时长），刷新页面，等待模型加载完成并被 unload。
4. 再改回 3000（下载中途打断），刷新页面。

**需要观察的现象**：

- 第 3 步：控制台先出现进度日志，随后是 `calling unload`；由于加载已完成，这是一次对已就绪引擎的正常卸载。
- 第 4 步：进度日志进行到一半出现 `calling unload`，随后进度停止；控制台出现一条 `Reload() is aborted.` 的 **warn**（来自 reload 的 catch 分支），但没有未捕获异常。

**预期结果**：两种时机下 unload 都「静默」完成——加载中被打断走 AbortError 分支只告警，验证了切换/卸载操作在任何时刻调用都是安全的。

**待本地验证**：具体日志文本与 warn 的输出时机取决于模型下载速度，需实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：既然 `reload()` 第一步就是 `unload()`，应用层的「切换模型」还有必要先手动调用 `engine.unload()` 再 `reload` 吗？

**答案**：不是必须的，但有实际价值。(1) 显式 unload 可以**尽早**释放显存——在等用户确认下一个模型、或需要向 UI 明确展示「已卸载」状态时有用；(2) 它能打断一次仍在进行的 reload（abort-reload 示例正是这么用的）；(3) 若直接 reload，旧模型释放和新模型加载在同一个调用里连续发生，应用层拿不到中间态。而只做 `reload(newId)` 的好处是代码最短，且不存在「已 unload 但忘了 reload」的空窗状态。

**练习 2**：为什么 `unload()` 里对每个 pipeline 都要 `await pipeline.sync()`，而不能 dispose 完立刻清空 Map？

**答案**：WebGPU 资源销毁是异步提交给 GPU 队列的。`dispose()` 只是把对象标记为可释放，`pipeline.sync()` 内部调用 `device.sync()`（[src/llm_chat.ts:2239-2241](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2239-L2241)）才确保设备层面的清理真正完成。注释还指出：必须等所有 sync 结束后才能把 `deviceLostIsError` 恢复为 true，否则计划内的设备销毁会被误判为设备丢失错误。

**练习 3**：设备在生成阶段（而非 reload 阶段）丢失会发生什么？

**答案**：`device.lost` 回调仍会触发并调用 `this.unload()` 清空引擎状态，但由于不在 reload 内，`DeviceLostError` 不会被同步抛出到当前请求——源码注释也坦承这是「尚未遇到过但理论上可能」的路径（[src/engine.ts:369-373](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L369-L373) 的 TODO）。实践上后续任何请求会因四个 Map 被清空而抛 `ModelNotLoadedError`，应用应引导用户重新 reload。

### 4.2 多 pipeline 字典管理

#### 4.2.1 概念说明

引擎的全部多模型状态收敛在**四个以 modelId 为键的 Map** 里（[src/engine.ts:126-137](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L126-L137)）：pipeline、chatConfig、modelType、lock。这四个 Map 的键集合在任何时刻都应一致——`getModelStates` 甚至对 chatConfig 和 lock 的存在性做了内部断言（[src/engine.ts:1271-1285](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1271-L1285)）。

每个模型一把独立锁带来一个关键并发性质：

- **同一模型**的多个请求在锁上排队，FCFS 串行执行；
- **不同模型**的请求互不阻塞，可以真正并行生成——两个 pipeline 各持各的权重与 KV cache，在 GPU 上交替提交内核。

还有一点容易被忽略：`reloadInternal` 里每个模型都独立走一遍 `tvmjs.instantiate` 和 `detectGPUDevice`（[src/engine.ts:337-348](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L337-L348)），即**每个 pipeline 持有一个独立的 tvmjs 运行时实例和一个独立的 WebGPUDevice 对象**（背后通常是同一块物理 GPU）。

#### 4.2.2 核心流程

请求到达时的模型路由（`getModelStates` → `getModelIdToUse`）：

```text
getModelIdToUse(loadedModelIds, request.model)
  ├── 已加载数 = 0                    -> ModelNotLoadedError
  ├── request.model 已指定
  │     ├── 不在已加载列表             -> SpecifiedModelNotFoundError
  │     └── 在列表                    -> 选中该模型
  └── request.model 未指定
        ├── 已加载 > 1 个             -> UnclearModelToUseError（歧义，拒绝猜测）
        └── 已加载 = 1 个             -> 唯一模型被隐式选中
```

选中后按 modelType 校验 pipeline 类型（chat 请求拿到 `EmbeddingPipeline` 会抛 `IncorrectPipelineLoadedError`），再取出 chatConfig 与 lock。

多模型并存的显存账本：

\[ \text{总显存} \approx \sum_{i=1}^{N} \left( \text{权重}_i + \text{KV cache}_i + \text{运行时缓冲}_i \right) \]

其中 KV cache 随 `context_window_size` 满额预分配（u3-l1），是唯一容易被人忽略的大头——两个 4GB 模型并存的开销不是「4GB + 一点」，而是接近 8GB 起步。`ModelRecord.vram_required_MB` 是单模型（含默认上下文窗口）的参考值，多模型时应把各条记录的该值相加做初筛。

#### 4.2.3 源码精读

四个状态 Map 的声明。[src/engine.ts:124-137](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L124-L137)：这段代码声明了 pipeline、chatConfig、modelType、lock 四个 Map——注释明确说明 lock 的存在是「保证每个模型一次只处理一个请求」。它们在构造函数中初始化（[src/engine.ts:150-157](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L150-L157)），是引擎状态的全部载体；构造本身非常轻量，不做任何 GPU 工作。

模型路由的完整决策。[src/support.ts:227-255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L255)：这段代码实现了 `getModelIdToUse`——空列表抛 `ModelNotLoadedError`（[src/error.ts:86](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L86)）；指定了未加载的模型抛 `SpecifiedModelNotFoundError`（[src/error.ts:575](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L575)）；未指定且多于一个模型抛 `UnclearModelToUseError`（[src/error.ts:565](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L565)）；仅当未指定且恰好一个模型时才隐式选中。

每模型一把锁的取用。[src/engine.ts:827-829](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L827-L829)：这段代码在 `chatCompletion` 校验完请求后，按**选中的 modelId**（而非全局）取锁并 `acquire()`——不同模型的请求拿的是不同的锁对象，因此可以并行；同一模型的第二个请求会在这里等待。`CustomLock` 的实现（[src/support.ts:377-392](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L377-L392)）用 Promise 队列实现 FIFO 唤醒。

官方多模型示例的加载与并行。[examples/multi-models/src/main.ts:88-133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L88-L133)：`parallelGeneration` 一次 `CreateWebWorkerMLCEngine` 传入**模型数组**（[L51-55](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L51-L55) 与串行版相同），然后 `Promise.all` 同时向两个模型发流式请求（L124）；两个请求都在各自的 chunk 循环里推进，互不等待。示例作者在 [L125-127](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L125-L127) 留下注释：并发的**同模型**请求按 FCFS 顺序执行，并链到 web-llm PR #549。请求里显式带 `model` 字段（[L31-45](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L31-L45)，注释写明「不指定会因歧义抛错」），`getMessage` 同样要传 modelId（[L131-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L131-L132)）。

reload 对多模型的参数校验。[src/engine.ts:216-226](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L216-L226)：这段代码校验 `modelId` 数组与 `chatOpts` 数组长度一致（否则 `ReloadArgumentSizeUnmatchedError`，[src/error.ts:555](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L555)），且 modelId 不得重复（否则 `ReloadModelIdNotUniqueError`，[src/error.ts:604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L604)）——重复的 modelId 会破坏「Map 键 = 模型集合」这一不变量。

#### 4.2.4 代码实践

**实践目标**：跑通官方多模型示例，验证「跨模型并行、同模型排队」与歧义报错。

**操作步骤**：

1. 进入 `examples/multi-models/`，`npm install` 后 `npm start`（见 [examples/multi-models/README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/README.md)），浏览器打开页面，观察两个模型的回复**同时**逐字增长（`parallelGeneration` 默认启用前的 `sequentialGeneration()` 在 [L136-137](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L136-L137) 二选一，可切换体验）。
2. 打开控制台查看两个请求最后一个 chunk 的 `usage`，记录各自的 `decode_tokens_per_s`。
3. 把 `request2` 的 `model` 字段删掉，刷新页面，观察抛出的错误。
4. 把 [L128](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-models/src/main.ts#L128) 的注释打开（对同一模型发两个并发请求），观察输出顺序。

**需要观察的现象**：

- 第 1 步：两个 label 的文字都在滚动，证明两模型的生成并行推进。
- 第 2 步：并行时每个模型的解码速度通常低于独占时（GPU 被两个管线分时共享），但都保持产出。
- 第 3 步：抛出 `UnclearModelToUseError`，消息会列出已加载的两个 modelId 并指出请求未指定用哪个。
- 第 4 步：对同一模型的两个请求输出是**一先一后完整完成**，而非交错。

**预期结果**：与 `getModelIdToUse` 的三分支决策和 per-model 锁的行为完全吻合。

**待本地验证**：并行时的具体速度下降幅度取决于设备，需实测；第 4 步的 FCFS 顺序以控制台实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：在 8GB 显存的设备上想把 `Phi-3.5-mini-instruct-q4f32_1-MLC`（约 2.9GB VRAM 参考值）和一个 4GB 级模型同时加载，除了显存相加还要注意什么？

**答案**：还要核对每个模型的**上下文窗口档位**。`vram_required_MB` 对应记录里默认的 `context_window_size`（如 `-1k` 后缀的 1k 档），KV cache 是按窗口满额预分配的；若 overrides 把窗口调大，实际占用会超出参考值。另外两条记录的 `required_features`（如 shader-f16）都必须被当前 GPU 支持，任何一条不满足都会让整次 reload 失败（且由于第 0 步已 unload，旧模型也没了——失败即空引擎）。

**练习 2**：为什么 `getModelStates` 在取出 chatConfig 和 lock 时还要做 `undefined` 检查并抛 "InternalError"？既然四个 Map 总是一起更新？

**答案**：这是防御性编程的内部不变量断言：正常流程中 `reloadInternal` 末尾把 pipeline 与 lock 写入 Map（[src/engine.ts:414-415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L414-L415)）、chatConfig 更早写入（[L297](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L297)），`unload` 统一清空，四者键集合理应一致；但若未来代码改动打破了这个约定，宁可在这里抛出显式 InternalError 也不想让 `undefined` 静默扩散成更难查的崩溃。

### 4.3 中断与恢复

#### 4.3.1 概念说明

WebLLM 的生成中断是**协作式**的（u2-l3 讲过单模型视角），本讲补上多模型视角下的两个关键事实：

1. **`interruptSignal` 是引擎级（engine-wide）的单一布尔标志**，不属于任何模型或请求。多模型并行生成时调用一次 `interruptGenerate()`，所有正在 decode 循环里的模型都会在下一次循环检查时停下来。
2. **恢复是自动的**：中断不留下任何需要用户清理的引擎状态。标志在每次新生成开始时复位为 false；管线的 `stopTriggered` 在下一次 `prefillStep` 开头复位；锁在中断路径上照常释放。

「abort 后引擎状态如何恢复可用」的答案因此分三层：

| 状态 | 中断时的处置 | 下次请求时的恢复 |
| --- | --- | --- |
| `engine.interruptSignal` | 置 true，decode 循环检测到后 break | `_generate`/`asyncGenerate` 开头复位为 false |
| `pipeline.stopTriggered` / `finishReason` | `triggerStop()` 置 true / "abort"，部分回复经 `finishReply` 闭环进 Conversation | `prefillStep` 开头复位 |
| 模型锁 | 生成器走完收尾 chunk 后 `release()` | 下个请求正常 `acquire()` |

#### 4.3.2 核心流程

一次中断的完整时序（以流式请求为例）：

```text
用户调用 engine.interruptGenerate()
  └── interruptSignal = true                    # 引擎级，立即返回，不等生成结束
decode 循环下一轮开头:
  while (!pipeline.stopped())
    └── if (this.interruptSignal)
          ├── pipeline.triggerStop()            # stopTriggered=true, finishReason="abort"
          │     └── conversation.finishReply(部分输出)   # 把半截回复闭环进历史
          └── break
生成器继续走收尾流程:
  └── yield 空 delta 收尾帧 (finish_reason: "abort") → 可选 usage 帧 → lock.release()
下一次请求:
  └── interruptSignal=false → prefill → prefillStep 内 stopTriggered=false → 正常生成
```

需要特别强调的边界情形：中断对 `n > 1` 的多候选同样生效——非流式路径在**每个候选开始前**检查标志，一次中断停掉所有候选（[src/engine.ts:860-864](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L860-L864)）。而 prefill 阶段是不可中断的（u3-l3）：信号要等 prefill 跑完、进入 decode 循环后才被消费。

#### 4.3.3 源码精读

引擎级标志的置位与复位。[src/engine.ts:771-773](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L771-L773)：`interruptGenerate()` 只有一行——把引擎的 `interruptSignal` 置 true，立即返回。注意它**不区分模型**：多模型并行时这是一个「全停」开关。复位发生在两处：非流式 `_generate` 的开头（[src/engine.ts:465](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L465)）和流式 `asyncGenerate` 的初始化段（[src/engine.ts:537](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L537)）——每个新生成开始时都把标志擦干净，上一个请求的中断不会泄漏到下一个。

decode 循环对信号的消费。[src/engine.ts:621-638](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L621-L638)：流式生成的主循环在每轮 decode 前检查 `this.interruptSignal`，命中则 `pipeline.triggerStop()` 并 break——注意 break 之后**不是直接 return**，而是继续走函数后半段的收尾逻辑（空 delta 终止帧、usage 帧），最后在 [src/engine.ts:768](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L768) 释放锁。非流式版本结构相同（[src/engine.ts:471-477](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L471-L477)）。这就是「中断后锁不泄漏」的机制保证。

管线侧的中断落地。[src/llm_chat.ts:941-950](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L941-L950)：`triggerStop()` 是幂等的（已停止则直接返回），把 `stopTriggered` 置 true、`finishReason` 置 "abort"，并调用 `conversation.finishReply(this.outputMessage)` 把**已生成的半截回复**正式写进对话历史——所以中断后 `getMessage()` 拿到的是有效部分输出，而非垃圾状态。恢复侧，[src/llm_chat.ts:760](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L760) 表明 `prefillStep` 开头无条件把 `stopTriggered` 复位为 false；两次请求之间若有人误调 `decodeStep`，会被 [src/llm_chat.ts:902-905](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L902-L905) 的守卫拦下（"Cannot run decode when stopped"）。

Worker 场景下的中断转发。[src/web_worker.ts:587-594](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L587-L594)：主线程侧的 `WebWorkerMLCEngine.interruptGenerate()` 把中断编码成一条 `kind: "interruptGenerate"` 消息发进 worker，且**不 await** 结果（fire-and-forget）——中断要的就是快，不能排队等回包。这与 u5-l1 讲过的消息协议一致，也再次说明中断没有携带 modelId，天然作用于 worker 内引擎的全部在飞生成。

中断后的会话衔接。[src/engine.ts:1379-1397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1379-L1397)：下一次请求进入 `prefill` 时，引擎比对管线里的旧 Conversation 与新请求构建的 Conversation——若调用方把中断时的部分回复（`getMessage()` 的返回值）作为 assistant 消息追加进了 messages，比对命中，走 "Multiround chatting, reuse KVCache" 分支复用缓存；否则全量重算。中断本身不破坏这个机制，因为 `finishReply` 已经把历史整理成了可续接的形态。

#### 4.3.4 代码实践

**实践目标**：验证「abort 后立即发新请求，引擎无需任何手动清理即可正常工作」，并实测多模型并行的全停语义。

**操作步骤**：

1. 复制 `examples/multi-models` 为自己的实验目录（或直接修改其 `main.ts`），在 `parallelGeneration` 的两个 chunk 循环里各维护一个 `finished` 标记。
2. 加一个「停止」按钮，点击时调用 `engine.interruptGenerate()`。
3. 场景 A（单模型恢复）：只保留一个模型，流式生成中点停止；从最后一个 chunk 读 `finish_reason`，调用 `engine.getMessage()` 保存部分输出；**紧接着**（不做 resetChat、不做 reload）发起一条新请求。
4. 场景 B（多模型全停）：恢复双模型并行版本，生成进行中点停止，观察两个 label 是否**都**停止滚动。
5. 场景 C（中断后续接）：场景 A 的新请求把部分输出作为 assistant 消息放进 messages 再发。

**需要观察的现象**：

- 场景 A：最后一个 chunk 的 `finish_reason` 为 `"abort"`；新请求正常返回完整回复，无报错、无卡顿；控制台无锁相关的 InternalError。
- 场景 B：两个模型的生成都停止，两条流的收尾帧 `finish_reason` 均为 `"abort"`。
- 场景 C：控制台出现 `Multiround chatting, reuse KVCache.` 日志（[src/engine.ts:1396](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1396)），且新请求的 `usage.prompt_tokens` 只计增量。

**预期结果**：三个场景分别对应「标志复位—锁释放—会话可续接」三层恢复机制，全部无需应用层干预。

**待本地验证**：场景 B 中两个模型停止的先后间隔（取决于各自当前 decode 步进度）需实测；场景 C 的 prompt_tokens 数值以实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：多模型并行生成时，能否只中断其中一个模型？

**答案**：用公开 API 不能。`interruptSignal` 是引擎级标志，`interruptGenerate()` 也没有 modelId 参数（Worker 版消息 `content: null`）。所有正在 decode 循环中的管线都会在下轮检查时停止。变通做法：给不希望被中断的请求设置较小的 `max_tokens`，或为不同模型使用不同的引擎实例（各自的 `interruptSignal` 互不影响）——代价是失去共享 appConfig/缓存的便利并增加内存开销。

**练习 2**：中断发生在 prefill 阶段（prompt 很长）时会怎样？

**答案**：不会立即停。prefillStep 内部没有检查 `interruptSignal` 的检查点（u3-l3 的结论），信号最早在 prefill 完成、产出首 token、进入 decode 循环后的**第一轮**检查时被消费。因此长 prompt 下点「停止」后仍会观察到一段 TTFT 量级的延迟才真正停下，且首 chunk 已经 yield 出来。

**练习 3**：如果用户在中断后直接调用 `engine.resetChat()` 再发新请求，与不调用有什么区别？

**答案**：`resetChat` 清空 Conversation 与 KV cache（[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540)），因此新请求必定全量 prefill；不调用则由会话比对决定是否复用。两者都不影响「引擎可用性」——中断本身已把状态复位。另外引擎的 `resetChat` 对 `ModelNotLoadedError`/`SpecifiedModelNotFoundError` 做了吞错处理（[src/engine.ts:1326-1344](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1326-L1344)），空引擎上调用是无害的 no-op。

## 5. 综合实践

**任务**：实现一个「双模型切换器」页面，把本讲三个模块串起来。

**需求**：

1. 两个按钮：「加载小模型」（如 `Qwen2.5-0.5B-Instruct-q4f16_1-MLC`，以 `prebuiltAppConfig.model_list` 实际存在的 model_id 为准）与「加载大模型」（如 `Phi-3.5-mini-instruct-q4f32_1-MLC`）。
2. 切换逻辑：点击时先 `await engine.unload()`（在页面上显示「已卸载，释放显存中…」），等按钮回调完成后再 `await engine.reload(newModelId)`——体会显式两段式与一步 `reload` 的差异。
3. 显存观测（无法精确测量处标注「待确认」）：
   - 记录两条 ModelRecord 的 `vram_required_MB` 相加，作为并存预估；
   - 切换前后用 Chrome 任务管理器（Shift+Esc，查看页面进程与 GPU 进程内存）粗估变化——浏览器没有暴露 GPU 显存的标准查询 API，此处只能定性观察，标注「待确认」；
   - 对照组：把 `reload([modelA, modelB])` 双模型并存与单模型分别加载，比较任务管理器读数方向是否符合求和直觉。
4. 中断恢复：加载完成后发起一个 `max_tokens` 较大的流式请求，2 秒后调用 `engine.interruptGenerate()`；读回 `finish_reason` 与 `getMessage()`；**立即**再发一条请求验证引擎正常；把中断的部分输出续接进 messages 再发一条，观察是否命中 KV cache 复用日志。

**验收标准**：

- 切换过程无未捕获异常，unload 后立即 `getModelStates` 类调用（如 `getMessage()`）应抛 `ModelNotLoadedError`——这本身就是对「unload 清空了 Map」的验证；
- 中断后新请求正常完成，续接请求命中多轮复用；
- 产出一页实验记录：切换耗时（含缓存命中时的二次加载）、显存观测值及其「待确认」标注、中断的 finish_reason。

**提示**：二次切换时模型已进 CacheStorage（u4-l1），reload 应明显快于首次；若想进一步压显存，可在 chatOpts 里把 `context_window_size` 调小（注意 KV cache 是按窗口满额分配的，这是显存最大的可调项之一）。

## 6. 本讲小结

- `reload()` 是全量换血：第 0 步无条件 `unload()` 所有模型，多模型只能在一次调用中以 modelId 数组声明；没有追加或单独卸载某一个模型的公开 API。
- `unload()` 的资源释放链是「置 `deviceLostIsError=false` → 逐 pipeline `dispose()` + `await sync()` → 清空四个 Map → 恢复标志 → abort `reloadController`」，其中 abort 机制让进行中的加载可被干净取消（AbortError 只告警不上抛）。
- 引擎多模型状态 = 四个以 modelId 为键的 Map（pipeline/chatConfig/modelType/lock）；每模型一把 `CustomLock`，因此跨模型请求真正并行、同模型请求 FCFS 排队；路由决策由 `getModelIdToUse` 三分支给出，歧义时抛 `UnclearModelToUseError` 拒绝猜测。
- 多模型显存 ≈ Σ(权重 + 满额 KV cache + 运行时缓冲)，且每个 pipeline 各持一个独立 tvmjs 实例与 WebGPUDevice。
- `interruptGenerate()` 是引擎级单标志：多模型并行时一按全停；恢复是全自动的三层复位（引擎标志在新生成开头清零、`stopTriggered` 在 prefillStep 开头清零、锁在收尾帧后释放），abort 后无需任何手动清理。
- 设备丢失被收敛为 reload 末尾同步抛出的 `DeviceLostError`，且回调里主动 `unload()` 自清理；生成期设备丢失是已知的理论空档（TODO 注释），表现为后续请求的 `ModelNotLoadedError`。

## 7. 下一步学习建议

- 下一讲 u7-l3《性能观测：延迟分解与运行统计》会把本讲反复出现的 `decode_tokens_per_s`、TTFT 等 usage.extra 字段的计算口径讲透，与本讲的「并行时速度下降」观察直接衔接。
- 推荐继续阅读的源码：[src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) 的 `CustomLock` 全文（体会 FIFO Promise 队列的极简实现）；[src/web_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts) 中 `unload` 消息处理后对影子状态 `modelId`/`chatOpts` 的清空（对照 u5-l1 的 `reloadIfUnmatched`）。
- 若想动手深挖：给「只中断指定模型」写一个设计草案（例如把 `interruptSignal` 改为 per-model Map），评估需要改动的所有调用点（engine.ts 的三个检查点、web_worker.ts 的消息协议、Service Worker 子类），这正是 u7-l5 端到端二开演练的热身。
