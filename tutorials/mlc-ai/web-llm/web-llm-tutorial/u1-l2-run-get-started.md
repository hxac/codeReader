# 跑通第一个例子：get-started 实战

## 1. 本讲目标

学完本讲，你应该能够：

1. 在本地运行 `examples/get-started` 示例：`npm install` → `npm start` → 打开浏览器看到模型加载进度和第一条回复。
2. 读懂示例代码 `get_started.ts` 中的三步主干：`CreateMLCEngine` 创建引擎、`initProgressCallback` 汇报加载进度、`engine.chat.completions.create` 发起第一次对话。
3. 说清楚 `initProgressCallback` 收到的 `InitProgressReport`（`progress` / `timeElapsed` / `text`）分别是什么，以及引擎内部加载模型的各个阶段。
4. 学会更换默认模型为更小的模型（如 1B 或 0.5B），并能在浏览器 DevTools 中观察 WebGPU 设备信息与模型缓存的位置。
5. 在页面上打印每轮回复的 `finish_reason`，理解 `stop` 与 `length` 两种终止原因的来源。

## 2. 前置知识

在动手之前，先补齐几个本讲会用到的概念（都很轻量，不需要系统学习）：

- **静态服务器与打包器**：浏览器不能直接运行 TypeScript，也不能直接 `import` `node_modules` 里的包。示例使用 [Parcel](https://parceljs.org/) 做两件事：把 `get_started.ts` 编译成浏览器能执行的 JavaScript，并顺手起一个本地静态服务器。所以「跑示例」= 装依赖 + 起服务器 + 用浏览器访问。
- **WebGPU**：一组浏览器标准 API，让网页可以直接使用 GPU 做通用计算。WebLLM 的全部矩阵运算都跑在 WebGPU 上，因此需要较新的浏览器（如新版 Chrome / Edge）。可以在地址栏输入 `chrome://gpu` 查看自己的浏览器是否支持。
- **模型缓存**：模型权重（几百 MB 到几 GB）首次从远端（通常是 HuggingFace）下载后会存在浏览器里，默认后端是 CacheStorage（DevTools → Application → Cache Storage 可见）。第二次加载不再下载，速度会快一个量级。本讲会在 DevTools 里亲眼验证这件事，缓存机制的源码细节留到单元四展开。
- **OpenAI 风格 API**：上一讲提过 WebLLM 的接口兼容 OpenAI API。本讲会用到 `engine.chat.completions.create(...)` 这种「门面写法」，它最终会转发到 `engine.chatCompletion(...)`——这正是本讲的第三个最小模块。
- **model_id 与 ModelRecord**：上一讲讲过，`prebuiltAppConfig.model_list` 中每条 `ModelRecord` 描述一个预置模型，`model_id` 是它的唯一标识（如 `Llama-3.1-8B-Instruct-q4f32_1-MLC`），`vram_required_MB` 是预估显存占用。本讲要换模型，就是换一个 `model_id` 字符串。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/get-started/README.md` | 示例的运行说明，只有两条命令 |
| `examples/get-started/package.json` | 示例自身的依赖（`@mlc-ai/web-llm`）与 `npm start` 脚本（Parcel，端口 8888） |
| `examples/get-started/src/get_started.html` | 页面骨架：几个 `<label>` 用来展示进度/回复 |
| `examples/get-started/src/get_started.ts` | 示例主逻辑：创建引擎 → 发起对话，本讲的主角 |
| `src/engine.ts` | 库侧核心：`CreateMLCEngine`、`MLCEngine.reload` 加载流程、`chatCompletion` 入口 |
| `src/types.ts` | `InitProgressReport` / `InitProgressCallback` 类型定义 |
| `src/openai_api_protocols/chat_completion.ts` | `chat.completions.create` 门面类与 `ChatCompletion` 响应结构 |
| `src/config.ts` | `prebuiltAppConfig.model_list`：换模型时来这里查 `model_id` 与显存占用 |

阅读顺序建议：先读示例目录的四个文件（都很短），再带着问题去 `src/engine.ts` 里找答案。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：示例工程与启动方式、`CreateMLCEngine` 调用、`initProgressCallback` 回调、`chatCompletion` 基本调用。

### 4.1 示例工程的结构与启动方式

#### 4.1.1 概念说明

`examples/get-started` 是官方提供的最小可运行 demo：一个 HTML 页面 + 一个 TypeScript 入口。它演示了 WebLLM 最典型的使用形态——网页引入 `@mlc-ai/web-llm` 这个 npm 包，在浏览器里完成「下载模型 → 加载到 GPU → 对话」全过程，全程不依赖任何服务器推理。

值得注意的是：示例目录是一个**独立的 npm 工程**，它依赖的是发布到 npm 的 `@mlc-ai/web-llm` 包（版本 `^0.2.84`），而不是仓库里的 `src/` 源码。只有想改库本身源码时，才需要按 README 提示把依赖改成 `"file:../.."` 并从源码构建。

#### 4.1.2 核心流程

```text
cd examples/get-started
  └─> npm install        # 安装 parcel、typescript、@mlc-ai/web-llm 等
  └─> npm start          # parcel src/get_started.html --port 8888
        └─> 浏览器打开 http://localhost:8888
              └─> 加载 get_started.ts（type="module"）
                    └─> main()：创建引擎 → 下载模型 → 输出回复
```

#### 4.1.3 源码精读

README 只给了两条命令，这就是运行示例的全部：

- [examples/get-started/README.md:L6-L9](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/README.md#L6-L9)：`npm install` 后 `npm start` 即可；同时说明若想改 WebLLM 核心包，可把依赖改为 `file:../..` 并从源码构建（本讲不需要）。

`npm start` 实际执行的是 Parcel：

- [examples/get-started/package.json:L5-L8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/package.json#L5-L8)：`start` 脚本是 `parcel src/get_started.html --port 8888`，即以 HTML 为入口起开发服务器，默认地址 `http://localhost:8888`。
- [examples/get-started/package.json:L17-L19](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/package.json#L17-L19)：示例依赖的库版本是 `@mlc-ai/web-llm: ^0.2.84`，与仓库根 [package.json:L2-L3](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L2-L3) 中的包版本一致。

页面骨架非常朴素，几个 `<label>` 就是对外的「UI」：

- [examples/get-started/src/get_started.html:L6-L19](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.html#L6-L19)：页面上有 `init-label`（本例唯一真正被更新的标签，显示加载进度文本）、`prompt-label`、`generate-label`、`stats-label`。后三个标签在当前版本代码里没有被使用——这正好留给我们做实践时「在页面上显示回复与 finishReason」的挂载点。
- [examples/get-started/src/get_started.html:L21-L21](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.html#L21-L21)：通过 `<script type="module" src="./get_started.ts">` 加载 TypeScript 入口，Parcel 会现场编译。
- [examples/get-started/src/get_started.html:L3-L5](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.html#L3-L5)：定义了全局变量 `webLLMGlobal = {}`。仓库里所有示例的 HTML 都有这一行约定写法（可用 Grep 验证），示例页面保留它作为约定占位。

#### 4.1.4 代码实践

1. **实践目标**：把示例跑起来，第一次亲眼看到浏览器内加载 LLM。
2. **操作步骤**：
   - 确认浏览器支持 WebGPU：地址栏打开 `chrome://gpu`（或 Edge 的 `edge://gpu`），搜索 "WebGPU" 看是否显示可用。
   - 在 `examples/get-started` 目录下执行 `npm install`，然后 `npm start`。
   - 浏览器打开 `http://localhost:8888`，打开 DevTools 的 Console 面板。
3. **需要观察的现象**：页面上 `init-label` 的文字会从配置下载、wasm 加载、权重下载百分比一路变化，最后变成 "Finish loading on WebGPU - <你的 GPU 型号>"；随后 Console 里打印出回复对象和 `usage`。
4. **预期结果**：Console 中能看到 `reply0`（一个 `ChatCompletion` 对象，包含 `choices`、`usage` 等字段）和 `reply0.usage`（含 token 统计）。默认模型约需 6 GB 显存，如果你的机器显存不足，可能看到 device lost 相关报错——这正是下一小节换小模型的价值。
5. 首次运行的具体耗时取决于网络与机器，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么示例要用 Parcel（或任何打包器/静态服务器），而不是直接双击打开 HTML 文件？

**答案**：两个原因。其一，浏览器不认识 TypeScript，`get_started.ts` 需要被编译；其二，`import * as webllm from "@mlc-ai/web-llm"` 需要模块解析与打包，且 ES 模块通过 `file://` 协议直接打开会因跨域限制失败，必须经 HTTP 服务器访问。

**练习 2**：示例工程依赖的 `@mlc-ai/web-llm` 从哪里来？如果想调试仓库里的 `src/` 源码要怎么改？

**答案**：从 npm registry 安装发布版（`^0.2.84`）。想调试源码时，按 `examples/get-started/README.md` 第 11-14 行的提示，把依赖改成 `"file:../.."`，然后在仓库根目录按「build from source」说明先构建本地包。

### 4.2 CreateMLCEngine 调用

#### 4.2.1 概念说明

`CreateMLCEngine` 是创建引擎最常用的一步到位入口：传入 `model_id`（以及可选的引擎配置、聊天选项），它返回一个**已经完成模型加载**的 `MLCEngine`。它等价于「`new MLCEngine()` 再 `reload(modelId)`」两步的合并——示例代码中被注释掉的 Option 3 演示的就是这种分步写法。

第三个参数 `chatOpts`（类型 `ChatOptions`）可以在运行时覆盖模型自带的 `mlc-chat-config.json`，最常见的就是把 `context_window_size` 调小以省显存。

#### 4.2.2 核心流程

```text
CreateMLCEngine(modelId, engineConfig, chatOpts)
  ├─ new MLCEngine(engineConfig)     # 存下 appConfig / 回调 / 日志级别，创建三个门面对象
  └─ await engine.reload(modelId, chatOpts)
       ├─ 0. unload() 已加载模型
       ├─ 1-3. 参数数组化、长度校验、modelId 去重
       └─ 4. 对每个 modelId 调 reloadInternal：
            ① findModelRecord 查模型记录
            ② 缓存读取 mlc-chat-config.json，合并 overrides 与 chatOpts
            ③ 下载并实例化 wasm 模型库（tvmjs）
            ④ 检测 WebGPU 设备、校验 required_features
            ⑤ 加载 tokenizer、fetchTensorCache 下载权重（带进度）
            ⑥ 构造 LLMChatPipeline，编译 GPU shader
            ⑦ 汇报 progress=1，"Finish loading on WebGPU - ..."
```

#### 4.2.3 源码精读

示例中的调用（Option 1）：

- [examples/get-started/src/get_started.ts:L15-L29](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L15-L29)：不传 `appConfig` 时使用 `config.ts` 里的 `prebuiltAppConfig`；默认模型是 `Llama-3.1-8B-Instruct-q4f32_1-MLC`，引擎配置里传了 `initProgressCallback` 和 `logLevel: "INFO"`，第三个参数用 `context_window_size: 2048` 覆盖了模型默认上下文长度（注释里还给出了滑动窗口 + attention sink 的替代写法）。
- [examples/get-started/src/get_started.ts:L31-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L31-L57)：注释掉的 Option 2（自定义 `appConfig.model_list`）和 Option 3（`new MLCEngine()` + `reload()` 分步写法），是两种常见的进阶用法，单元一第 4 讲和单元二第 1 讲会分别展开。

库侧实现：

- [src/engine.ts:L99-L107](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L99-L107)：`CreateMLCEngine` 的全部实现就是 `new MLCEngine(engineConfig)` + `await engine.reload(modelId, chatOpts)`，与注释「Equivalent to `new webllm.MLCEngine().reload(...)`」一致。
- [src/engine.ts:L150-L166](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L150-L166)：构造函数里做了三件事：把 `engineConfig` 中的 `appConfig`（缺省 `prebuiltAppConfig`）、`logLevel`、`initProgressCallback` 存到引擎上；初始化若干记录已加载模型的 Map；创建 `chat` / `completions` / `embeddings` 三个门面对象——这就是下一小节 `engine.chat.completions.create` 能用的原因。
- [src/engine.ts:L203-L246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L203-L246)：`reload()` 先 `unload()` 旧模型，再做参数校验（模型数与 chatOpts 数量匹配、modelId 唯一），然后逐个加载，全程挂在一个 `AbortController` 下（支持中途 abort，单元二第 1 讲细讲）。
- [src/engine.ts:L248-L297](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L248-L297)：`reloadInternal` 的前半段：`findModelRecord(modelId, this.appConfig)` 查出 `ModelRecord`（第 256 行），从缓存作用域 `webllm/config` 取 `mlc-chat-config.json`，然后按 `模型自带配置 < ModelRecord.overrides < chatOpts` 的优先级合并出最终的 `ChatConfig`（第 292-296 行）——所以示例里传的 `context_window_size: 2048` 是最终生效值。
- [src/engine.ts:L299-L345](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L299-L345)：下载 `modelRecord.model_lib` 指向的 wasm 模型库（本地 URL 不缓存、远端 URL 走 `webllm/wasm` 缓存），`tvmjs.instantiate` 实例化后，把用户的 `initProgressCallback` 注册进 tvm 运行时（第 343-345 行）——此后权重下载的百分比文本就由 tvmjs 运行时直接回调给用户。
- [src/engine.ts:L347-L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L347-L397)：`detect_GPUDevice` 检测 WebGPU 设备（拿不到直接抛 `WebGPUNotAvailableError`）；若 `ModelRecord.required_features` 里有 `shader-f16` 而设备不支持，抛 `ShaderF16SupportError`；接着加载 tokenizer、`fetchTensorCache` 拉取权重（作用域 `webllm/model`）。
- [src/engine.ts:L399-L415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L415)：按 `model_type` 决定构造 `EmbeddingPipeline` 还是 `LLMChatPipeline`，再 `await newPipeline.asyncLoadWebGPUPipelines()` 异步编译 GPU shader——这就是首次加载最后阶段「卡一会儿」的来源。

换模型时查表的地方（`src/config.ts` 的 `prebuiltAppConfig.model_list`）：

- 默认模型 [src/config.ts:L465-L470](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L465-L470)：`Llama-3.1-8B-Instruct-q4f32_1-MLC`，`vram_required_MB: 6101.01`。
- 更小的候选一 [src/config.ts:L373-L378](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L373-L378)：`Llama-3.2-1B-Instruct-q4f16_1-MLC`，约 879 MB 显存，且标记 `low_resource_required: true`。
- 更小的候选二 [src/config.ts:L977-L982](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L977-L982)：`SmolLM2-360M-Instruct-q4f32_1-MLC`，约 580 MB。
- 更小的候选三 [src/config.ts:L1444-L1450](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L1444-L1450)：`Qwen2.5-0.5B-Instruct-q4f16_1-MLC`，约 945 MB。
- 注意：部分 `q4f16_1` 变体会声明 `required_features: ["shader-f16"]`（例如 [src/config.ts:L963-L971](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L963-L971) 的 SmolLM2-360M q4f16_1），老 GPU 不支持 f16 会在 [src/engine.ts:L358-L367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L358-L367) 处抛 `ShaderF16SupportError`。换模型前先看一眼这条字段。

#### 4.2.4 代码实践

1. **实践目标**：把默认 8B 模型换成一个约 1 GB 显存的小模型，验证「换模型 = 换 model_id 字符串」。
2. **操作步骤**：
   - 打开 `examples/get-started/src/get_started.ts` 第 16 行，把 `selectedModel` 改成 `"Llama-3.2-1B-Instruct-q4f16_1-MLC"`（或上面另外两个候选）。
   - 顺手把第 62-74 行的 `n: 3` 改成 `n: 1`，并**删掉 `logit_bias` 与 `logprobs`/`top_logprobs`**：示例第 65-66 行的注释说明了那几个 token id 是按 Llama-3.1-8B 的词表算的（"California"/"Texas"），换模型后词表不同，这些 id 就没有意义了。
   - 保存后 Parcel 会自动刷新页面，观察重新加载。
3. **需要观察的现象**：`init-label` 的进度文本滚动明显变快（要下载的权重量从约 4.6 GB 降到几百 MB）；Console 中的回复依然正常输出。
4. **预期结果**：加载成功且不再有显存压力；回复质量比 8B 模型有所下降（「List three US states」这类简单任务仍能答好）。
5. 具体下载耗时取决于网络，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：示例第 24-28 行传了 `{ context_window_size: 2048 }`，这个值是作用在哪一层级的配置？优先级如何？

**答案**：它是 `chatOpts`（`ChatOptions`），在 [src/engine.ts:L292-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L292-L296) 与模型自带的 `mlc-chat-config.json`、`ModelRecord.overrides` 合并，合并顺序为 `{...模型配置, ...overrides, ...chatOpts}`，因此 `chatOpts` 优先级最高，最终上下文窗口为 2048。

**练习 2**：`CreateMLCEngine` 返回前，引擎内部已经完成了哪些事？（至少列 4 项）

**答案**：已查到模型记录、下载并合并了聊天配置、下载并实例化了 wasm 模型库、检测了 WebGPU 设备与特性、加载了 tokenizer、下载了模型权重、构造了 `LLMChatPipeline` 并完成了 GPU shader 编译（对应 `reloadInternal` 的 ①-⑥，见 [src/engine.ts:L248-L415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L248-L415)）。

**练习 3**：为什么建议把示例里的 `logit_bias` 在换模型后删掉？

**答案**：`logit_bias` 的键是 token id，与具体模型的词表绑定。示例里的 id 是 Llama-3.1-8B-Instruct 词表里 "California"/"Texas" 的编码，换到 1B 或 0.5B 模型后这些 id 对应的是别的（甚至不存在的）token，偏置就失去了演示意义，还可能干扰输出。

### 4.3 initProgressCallback 回调

#### 4.3.1 概念说明

加载一个模型要经历「下配置 → 下 wasm → 下权重 → 编 shader」等多个阶段，其中权重下载可能占几十秒到几分钟，UI 必须能实时汇报进度。WebLLM 的做法很简单：用户在 `engineConfig.initProgressCallback` 里传一个函数，引擎在加载过程中反复调用它，每次传一个 `InitProgressReport` 对象。示例把这个对象的 `text` 直接写到页面标签上，就得到了一个一行代码的进度条。

#### 4.3.2 核心流程

```text
用户传入 initProgressCallback
  └─ MLCEngine 构造时存为 this.initProgressCallback
       ├─ 实例化 tvmjs 后：tvm.registerInitProgressCallback(callback)
       │     └─ 权重下载/解析期间：运行时多次回调（progress 0~1，text 含百分比）
       └─ reloadInternal 收尾：引擎自己回调一次
             progress: 1
             timeElapsed: 本次 reload 总秒数
             text: "Finish loading on WebGPU - <GPU 描述>"
```

`InitProgressReport` 三个字段：

| 字段 | 含义 |
| --- | --- |
| `progress` | 0 到 1 之间的进度比例；最终一次为 1 |
| `timeElapsed` | 已耗时（秒） |
| `text` | 人类可读的进度描述，可直接上 UI |

#### 4.3.3 源码精读

- [src/types.ts:L22-L31](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L22-L31)：`InitProgressReport` 接口（`progress` / `timeElapsed` / `text`）与 `InitProgressCallback` 类型定义。注意它是 `(report) => void` 的普通同步函数。
- [examples/get-started/src/get_started.ts:L12-L14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L12-L14)：示例的回调实现——把 `report.text` 写进 `init-label` 标签，页面于是实时显示加载文案。
- [src/engine.ts:L176-L182](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L176-L182)：`setInitProgressCallback` / `getInitProgressCallback`——回调可以在引擎创建后再更换，`MLCEngineInterface` 也把它列为标准接口（[src/types.ts:L79-L92](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L79-L92)）。
- [src/engine.ts:L343-L345](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L343-L345)：tvm 运行时实例化后，把回调注册进去。此后的进度文本（比如权重下载百分比）由 `@mlc-ai/web-runtime`（依赖包，源码不在本仓库）在 `fetchTensorCache` 期间产生并直接回调——本仓库里能确定的注册点就是这两行。
- [src/engine.ts:L417-L426](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L417-L426)：`reloadInternal` 结束时引擎亲自发出最后一次回调：`progress: 1`、`timeElapsed` 为整次加载耗时、`text` 为 `"Finish loading on " + gpuLabel`。`gpuLabel` 在 [src/engine.ts:L352-L357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L352-L357) 由 `adapterInfo.description` 或 `vendor` 拼出——所以页面上那行 "Finish loading on WebGPU - ..." 就是你自己 GPU 的型号，这也是最简单的「确认 WebGPU 用的是哪块卡」的方法。
- 补充：在 Web Worker 场景下，worker 内引擎的进度回调用同一种 `InitProgressReport` 结构打包成消息发回主线程（[src/web_worker.ts:L90-L92](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L90-L92)），类型完全复用，单元五会展开。

#### 4.3.4 代码实践

1. **实践目标**：把加载过程「录」下来，理解一次完整加载要经历哪些阶段。
2. **操作步骤**：
   - 把示例第 12-14 行的回调临时改为：
     ```ts
     // 示例代码：记录每次进度回调
     const initProgressCallback = (report: webllm.InitProgressReport) => {
       console.log(`[progress=${report.progress.toFixed(3)}] [${report.timeElapsed.toFixed(1)}s] ${report.text}`);
       setLabel("init-label", report.text);
     };
     ```
   - 刷新页面（如需重新下载，可先在 DevTools → Application → Cache Storage 里删除 `webllm/model` 等缓存再刷新），把 Console 里的日志按顺序复制下来。
3. **需要观察的现象**：日志是一串 `text` 逐步变化的记录：先是配置/wasm 相关阶段，中间是长时间的权重下载百分比爬升，最后一条是 `progress=1` 的 "Finish loading on WebGPU - ..."。
4. **预期结果**：得到一份加载阶段时间线；对比可见权重下载占了大头，而最后 shader 编译前后也有一段无百分比输出的间隙（`asyncLoadWebGPUPipelines` 阶段）。
5. 各阶段具体文案由 `@mlc-ai/web-runtime` 生成，不同版本措辞可能不同，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：回调的 `progress` 字段一定会从 0 匀速涨到 1 吗？

**答案**：不一定。它只在有权重下载这类可量化进度的阶段按比例增长；配置加载、shader 编译等阶段没有中间百分比，最后一次回调直接给出 `progress: 1`（[src/engine.ts:L419-L426](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L419-L426)）。做 UI 时应把 `text` 当主信息、`progress` 当辅助。

**练习 2**：页面上显示的 "Finish loading on WebGPU - ..." 里的 GPU 名称是从哪来的？

**答案**：`reloadInternal` 中 `tvmjs.detect_GPUDevice()` 返回的 `adapterInfo`（设备适配器信息），在 [src/engine.ts:L352-L357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L352-L357) 拼进 `gpuLabel`，再在收尾回调里作为 `text` 传出。

### 4.4 chatCompletion 基本调用

#### 4.4.1 概念说明

引擎就绪后就能对话了。示例用的是 OpenAI SDK 风格的门面写法 `engine.chat.completions.create(request)`；也可以直接调等价的底层方法 `engine.chatCompletion(request)`。不传 `stream: true` 时返回的是 `Promise<ChatCompletion>`——一个完整响应对象，包含 `choices`（回答列表）、`usage`（token 统计）等。

理解两个点即可入门：`messages` 是对话历史（`role` + `content`）；`finish_reason` 告诉你模型为什么停下来——`stop` 表示自然结束或命中 stop 序列，`length` 表示达到 `max_tokens` 上限，此外还有 `tool_calls`（调用了工具）和 `abort`（用户手动停止）。

#### 4.4.2 核心流程

```text
engine.chat.completions.create(request)          # 门面（OpenAI 风格）
  └─ engine.chatCompletion(request)              # 真正入口，二者只是转发
       ├─ getLLMStates：确认模型已加载，取出 pipeline/chatConfig
       ├─ postInitAndCheckFieldsChatCompletion：校验请求字段、填默认值
       ├─ 由请求字段组装 GenerationConfig（temperature、max_tokens、stop...）
       ├─ 获取该模型独占锁（同一模型同时只处理一个请求）
       └─ stream=true → 返回 AsyncIterable（下一讲）
          stream=false → 循环 n 次 _generate()，每次取
             pipeline.getFinishReason() 作为该 choice 的 finish_reason，
             汇总 usage 后返回 ChatCompletion
```

#### 4.4.3 源码精读

- [examples/get-started/src/get_started.ts:L59-L75](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L59-L75)：示例的完整请求——`messages` 只有一条 user 消息，另有一组可选参数：`n: 3`（生成 3 个候选回答）、`temperature: 1.5`、`max_tokens: 256`、`logit_bias`、`logprobs` / `top_logprobs`。注释明确写着 "below configurations are all optional"。
- [examples/get-started/src/get_started.ts:L76-L77](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L76-L77)：把回复对象和 `usage` 打到 Console——本讲实践中观察统计字段就看这里。
- [src/openai_api_protocols/chat_completion.ts:L50-L58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L50-L58)：`Chat` 门面类，构造时创建内层的 `completions` 对象，因此调用链是 `engine.chat.completions.create(...)`。
- [src/openai_api_protocols/chat_completion.ts:L60-L79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L60-L79)：`Completions.create` 用函数重载区分流式/非流式参数，但实现只有一行：`return this.engine.chatCompletion(request)`。门面纯粹是给熟悉 OpenAI SDK 的用户提供同形 API。
- [src/engine.ts:L787-L841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L841)：`chatCompletion` 入口的前半段：校验模型已加载、`postInitAndCheckFieldsChatCompletion` 校验/补全请求字段、把请求字段收进 `GenerationConfig`（第 810-825 行）、拿锁排队（第 828-829 行），`stream: true` 则返回异步迭代器。
- [src/engine.ts:L849-L872](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L849-L872)：非流式主循环：按 `n`（缺省 1）逐个调用 `_generate()` 生成候选，第 872 行 `selectedPipeline.getFinishReason()` 取出本候选的终止原因，作为该 choice 的 `finish_reason`。
- [src/openai_api_protocols/chat_completion.ts:L1042-L1066](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1042-L1066)：`ChatCompletion.Choice` 的类型定义，`finish_reason` 的文档注释列出了全部取值：`stop`（自然停止或命中 stop 序列）、`length`（达到 max_tokens）、`tool_calls`（调用工具）、`abort`（手动中止）。
- [src/openai_api_protocols/chat_completion.ts:L959-L989](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L959-L989)：`CompletionUsage`：`completion_tokens` / `prompt_tokens` / `total_tokens` 三个标准字段，外加 WebLLM 特有的 `extra` 对象（如 `e2e_latency_s` 端到端耗时、prefill 吞吐等），`console.log(reply0.usage)` 时能看到。

#### 4.4.4 代码实践

1. **实践目标**：把回复和 `finish_reason` 显示到页面上（而不是只躺在 Console 里），并制造两种不同的终止原因。
2. **操作步骤**：
   - 修改 `get_started.ts`，请求里设 `n: 1`、`max_tokens: 512`，拿到回复后：
     ```ts
     // 示例代码：把回复与 finishReason 显示到页面标签
     const reply0 = await engine.chat.completions.create({
       messages: [{ role: "user", content: "List three US states." }],
       n: 1,
       temperature: 1.0,
       max_tokens: 512,
     });
     setLabel("prompt-label", "List three US states.");
     setLabel("generate-label", reply0.choices[0].message.content ?? "");
     setLabel("stats-label", "finish_reason: " + reply0.choices[0].finish_reason);
     ```
     （`generate-label` / `stats-label` 本来就躺在 HTML 里没被用过，正好启用。）
   - 再做一次对照实验：把 `max_tokens` 改成 `8`，重复请求。
3. **需要观察的现象**：第一次 `stats-label` 显示 `finish_reason: stop`，回答完整；第二次回答被截断，`stats-label` 显示 `finish_reason: length`。
4. **预期结果**：两种 `finish_reason` 都被亲手触发过一遍，理解 `stop` 与 `length` 的区别来自 [src/engine.ts:L872](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L872) 处管线报告的终止原因。
5. 小模型上 `max_tokens: 8` 是否必然截断取决于分词情况，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`engine.chat.completions.create(req)` 与 `engine.chatCompletion(req)` 是什么关系？

**答案**：完全等价的转发关系。前者是 OpenAI SDK 风格的门面，[src/openai_api_protocols/chat_completion.ts:L74-L78](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L74-L78) 的 `create` 实现就一行 `return this.engine.chatCompletion(request)`。

**练习 2**：示例返回对象里 `reply0.choices` 为什么可能是数组？长度由什么决定？

**答案**：由请求参数 `n` 决定（示例中 `n: 3`）。非流式分支在 [src/engine.ts:L850-L872](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L850-L872) 循环调用 `_generate()` 生成 `n` 个候选，每个候选成为一个 `Choice`；不传 `n` 时默认 1。

**练习 3**：`reply0.usage.extra.e2e_latency_s` 和 `initProgressCallback` 最后一次的 `timeElapsed` 分别量的是哪段时间？

**答案**：`e2e_latency_s` 是**单次请求**从引擎收到请求到生成完响应的耗时（见 [src/openai_api_protocols/chat_completion.ts:L978-L985](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L978-L985) 的文档注释）；`timeElapsed` 是**一次模型加载（reloadInternal）** 的总耗时（[src/engine.ts:L417-L426](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L417-L426)）。一个量推理，一个量加载。

## 5. 综合实践

把本讲四个模块串成一个完整任务——「小模型 + 页面化输出 + 缓存收益实测」：

1. **准备**：按 4.1 的步骤运行示例；按 4.2 把模型换成 `Llama-3.2-1B-Instruct-q4f16_1-MLC`（去掉 Llama-3.1 专用的 `logit_bias`）。
2. **改造页面**：把 4.4 的代码扩展成三轮对话——维护一个 `messages` 数组，第一轮问 "List three US states."，之后每轮把模型的回复以 `role: "assistant"` 追加回数组再追问一句（如 "Now list three European countries."）。每轮结束后在 `stats-label` 追加显示本轮 `finish_reason` 和 `usage` 的 `completion_tokens`。
3. **实测缓存收益**：
   - 第一遍：先在 DevTools → Application → Cache Storage 删除 `webllm/config`、`webllm/wasm`、`webllm/model` 三个缓存（这三个作用域名来自 [src/engine.ts:L272-L275](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L272-L275)、[L300-L303](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L300-L303)、[L394-L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L394-L397)），刷新页面，用 4.3 的日志回调记录总加载时间（即最后一条 `timeElapsed`）。
   - 第二遍：不删缓存直接刷新，再次记录加载时间。
   - 截图两遍的 Console 时间线与 Application 面板中的缓存条目，写两三句话总结差异。
4. **观察 WebGPU**：打开 `chrome://gpu` 记录显卡型号，并与页面 "Finish loading on WebGPU - ..." 中显示的名称互相印证。
5. **预期结果**：
   - 第二遍加载明显快于第一遍（省去权重下载）；
   - 三轮对话的 `finish_reason` 均为 `stop`，且后续轮次的 `prompt_tokens` 逐轮增大（因为历史越拼越长）；
   - 缓存面板中能看到模型分片文件条目。
6. 各数值与网络环境强相关，**待本地验证**。

## 6. 本讲小结

- 运行 get-started 只需 `npm install` + `npm start`（Parcel 在 8888 端口起服务），但前提是浏览器支持 WebGPU（`chrome://gpu` 可查）。
- `CreateMLCEngine(modelId, engineConfig, chatOpts)` = `new MLCEngine()` + `reload()`，返回前已完成查模型记录、下配置、下 wasm、检测 GPU、载 tokenizer、下权重、编译 shader 全部工作。
- 换模型就是换 `model_id` 字符串，选型看 `config.ts` 里 `prebuiltAppConfig.model_list` 的 `vram_required_MB` 与 `required_features`（`shader-f16` 在老 GPU 上会直接抛错）。
- `initProgressCallback` 收到 `{progress, timeElapsed, text}`：权重下载期文本由 `@mlc-ai/web-runtime` 产生，最后一次 `progress: 1`、text 为 "Finish loading on WebGPU - <GPU>"。
- `engine.chat.completions.create` 只是 `engine.chatCompletion` 的门面；非流式时按 `n` 次循环生成，每个 choice 的 `finish_reason` 来自管线（`stop` / `length` / `tool_calls` / `abort`）。
- 模型默认缓存在 CacheStorage 的 `webllm/config`、`webllm/wasm`、`webllm/model` 三个作用域里，二次加载的提速来自命中这些缓存。

## 7. 下一步学习建议

- 下一讲（u1-l3 源码地图）会俯瞰 `src/` 全目录与库入口 `src/index.ts` 的导出分组，帮你把本讲「点到」的 `engine.ts`、`config.ts`、`types.ts` 放回整体架构中。
- 想先尝鲜流式输出的读者，可以提前翻看 `examples/streaming/`，正式讲解在 u2-l3。
- 对「模型从哪来、缓存怎么工作」感兴趣的读者，可在本讲基础上预读 `src/cache_util.ts` 的 `getCacheOptions`（[src/cache_util.ts:L20-L28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L20-L28)）与 [src/config.ts:L319-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L319-L324) 的默认缓存后端选择，单元四会系统展开。
- 跑示例时若遇到 `WebGPUNotAvailableError` 或 `ShaderF16SupportError`，可先读 `src/error.ts` 中对应错误类的注释，错误体系是 u7-l1 的主题。
