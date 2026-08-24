# u2-l1 CreateMLCEngine 与引擎生命周期

## 1. 本讲目标

学完本讲,你应该能够:

- 说出 `CreateMLCEngine`、`MLCEngine` 构造函数、`reload()` 三者之间的关系。
- 按执行顺序梳理 `reload()` 从「查模型记录」到「pipeline 就绪」的全部关键步骤,并能对应到源码行号。
- 区分 `unload()`、`resetChat()`、`interruptGenerate()` 三者在引擎生命周期中的不同角色。
- 解释 `MLCEngineInterface` 这个抽象为什么存在,以及它如何让页面在「主线程引擎」和「Worker 引擎」之间无感切换。

本讲是单元二「引擎接口层」的第一讲,后续的 chatCompletion、流式输出、embedding 等讲义都建立在本讲的生命周期模型之上。

## 2. 前置知识

阅读本讲前,你需要具备以下认知(来自单元一,也可自行补充):

- **WebLLM 的三层结构**(u1-l3):协议门面层(`API.Chat` 等 OpenAI 风格门面)→ 引擎层(`MLCEngine`)→ 管线层(`LLMChatPipeline`,经 tvmjs 调 WebGPU)。本讲的主角就是中间的引擎层。
- **ModelRecord 与 AppConfig**(u1-l4):一个模型由 `model`(权重仓库)、`model_lib`(wasm 模型库)、`overrides`(运行时覆盖)描述;`appConfig.model_list` 是模型白名单,缺省兜底 `prebuiltAppConfig`。
- **缓存作用域**(u1-l2):模型产物缓存在 CacheStorage 的 `webllm/config`、`webllm/wasm`、`webllm/model` 三个作用域中。

补充解释两个本讲会遇到的术语:

- **工厂函数(factory function)**:一个负责「创建并初始化对象」的普通函数,调用者一行代码拿到完全就绪的实例,省去自己 `new` 再初始化的样板代码。`CreateMLCEngine` 就是 `MLCEngine` 的工厂函数。
- **管线(pipeline)**:这里指 `LLMChatPipeline`,真正持有 tvmjs 运行时、权重、KV cache 并执行推理的对象。引擎(`MLCEngine`)是管理者和门面,管线才是干活的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 引擎层:模型加载/卸载、请求分发 | `CreateMLCEngine`、构造函数、`reload`/`reloadInternal`、`unload`、`resetChat` |
| [src/types.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts) | 公共类型定义 | `MLCEngineInterface` 接口的全部方法签名 |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 推理管线 | `reload` 末尾构造 pipeline 时的关键动作(VM、权重、KV cache、shader) |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | 辅助函数 | `findModelRecord` 如何在 appConfig 里查表 |
| [examples/abort-reload/src/get_started.js](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/src/get_started.js) | 官方示例 | 演示「reload 进行中调用 unload 中断加载」的完整小例子 |

## 4. 核心概念与源码讲解

### 4.1 CreateMLCEngine 工厂函数

#### 4.1.1 概念说明

加载一个模型需要很多步:查记录、下配置、下 wasm、检测 GPU、下权重、编译 shader……如果每一步都要使用者自己调用,API 会非常难用。`CreateMLCEngine` 把这一切打包成一次异步调用:

> 「给我一个 modelId,我还你一个完全就绪、可以直接对话的引擎。」

它的关键设计是:**返回 Promise 之前,模型已经加载完毕**。这意味着 `await CreateMLCEngine(...)` 之后的代码可以立刻发起对话,不需要再等待任何加载状态。

#### 4.1.2 核心流程

```text
CreateMLCEngine(modelId, engineConfig?, chatOpts?)
        │
        ├─ new MLCEngine(engineConfig)     ← 只做轻量初始化,不碰网络
        │
        ├─ await engine.reload(modelId, chatOpts)   ← 真正的重活(见 4.2)
        │
        └─ return engine                   ← 此时模型已就绪
```

#### 4.1.3 源码精读

工厂函数本体只有 4 行,位于 [src/engine.ts:99-107](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L99-L107):

```typescript
export async function CreateMLCEngine(
  modelId: string | string[],
  engineConfig?: MLCEngineConfig,
  chatOpts?: ChatOptions | ChatOptions[],
): Promise<MLCEngine> {
  const engine = new MLCEngine(engineConfig);
  await engine.reload(modelId, chatOpts);
  return engine;
}
```

这段代码做的事情:`new MLCEngine(engineConfig)` 创建引擎并应用配置,然后 `await engine.reload(...)` 加载模型,最后返回。正如其文档注释([src/engine.ts:84-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L84-L98))所说,它「等价于 `new webllm.MLCEngine().reload(...)`」——你在 u1-l2 已经用过它,现在看到了它的全部实现。

再看 `MLCEngine` 的构造函数,位于 [src/engine.ts:150-166](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L150-L166):

```typescript
constructor(engineConfig?: MLCEngineConfig) {
  this.loadedModelIdToPipeline = new Map<...>();
  this.loadedModelIdToChatConfig = new Map<string, ChatConfig>();
  this.loadedModelIdToModelType = new Map<string, ModelType>();
  this.loadedModelIdToLock = new Map<string, CustomLock>();
  this.appConfig = engineConfig?.appConfig || prebuiltAppConfig;
  this.setLogLevel(engineConfig?.logLevel || DefaultLogLevel);
  this.setInitProgressCallback(engineConfig?.initProgressCallback);
  ...
  this.chat = new API.Chat(this);
  this.completions = new API.Completions(this);
  this.embeddings = new API.Embeddings(this);
}
```

要点:

- 构造函数**完全不做网络请求、不碰 GPU**,只初始化四个 Map、兜底 appConfig、注册回调和三个 OpenAI 风格门面。这就是为什么 `new MLCEngine()` 是同步的、廉价的,而 `reload()` 是异步的、昂贵的——职责被切得很干净。
- 四个 Map([src/engine.ts:126-137](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L126-L137))都以 `modelId` 为键,分别记录已加载模型的管线、ChatConfig、模型类型和互斥锁。**这四个 Map 就是「引擎当前状态」的全部载体**——`unload()` 清空它们,引擎就回到了「空」状态。
- `CustomLock`([src/support.ts:377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L377))保证每个模型同一时刻只处理一个请求,后续讲义会再遇到它。

#### 4.1.4 代码实践

1. **实践目标**:亲手使用「手动等价形式」,验证 `CreateMLCEngine` 只是 `new + reload` 的语法糖。
2. **操作步骤**:
   - 进入 `examples/get-started`(u1-l2 已跑通),把 `src/get_started.ts` 中 `CreateMLCEngine(..., { initProgressCallback })` 一行替换为如下写法(**示例代码**):
     ```typescript
     const engine = new webllm.MLCEngine({ initProgressCallback });
     await engine.reload("Llama-3.2-1B-Instruct-q4f32_1-MLC");
     ```
   - `npm start` 后打开页面,观察加载进度条与最终对话是否与原来完全一致。
3. **需要观察的现象**:进度回调照常打印;对话功能不变。这说明工厂函数没有额外的隐藏逻辑。
4. **预期结果**:行为完全一致。若把 `await` 去掉再立刻调用 `engine.chatCompletion(...)`,则会抛出 `ModelNotLoadedError`(请求在加载完成前到达)——这正好印证「就绪与否」由 Map 里有没有管线决定,而不是由引擎对象是否存在决定。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:`CreateMLCEngine` 为什么设计成 `async` 函数,而 `new MLCEngine()` 是同步的?

**答案**:构造函数只初始化内存中的 Map 和回调,不涉及任何异步资源;而加载模型必须下载配置/wasm/权重、等待 GPU 设备就绪,这些都是异步操作。把异步重活集中在 `reload()` 中,让「创建引擎」和「加载模型」两个生命周期阶段分离,也使得同一个引擎可以在不销毁的前提下反复 `reload` 换模型。

**练习 2**:`CreateMLCEngine` 的第二个参数 `engineConfig` 和第三个参数 `chatOpts` 分别影响什么?

**答案**:`engineConfig`(`MLCEngineConfig`)作用于**引擎级**:appConfig(模型白名单)、logLevel、initProgressCallback、logitProcessorRegistry,在构造函数里一次性生效;`chatOpts`(`ChatOptions`)作用于**模型级**:覆盖该模型的 `mlc-chat-config.json` 字段(如 context_window_size),在 `reload` 内部与配置三层合并(见 4.2.3)。这与 u1-l4 讲过的「仓库配置 → overrides → chatOpts,后写者赢」直接对应。

### 4.2 MLCEngine.reload 主流程

#### 4.2.1 概念说明

`reload()` 是引擎生命周期中**最重的一个方法**:它把「一个 modelId 字符串」变成「一个可以推理的 pipeline」。整个过程可以类比为做菜前的备菜:

- 查菜谱(`mlc-chat-config.json`)= 模型配置;
- 买厨具(wasm model library)= TVM 运行时;
- 检查厨房(GPU 检测、shader-f16 特性);
- 备食材(tokenizer、模型权重);
- 组装灶台(构造 `LLMChatPipeline`:虚拟机、KV cache);
- 预热(编译 WebGPU shader)。

`reload()` 分两层:外层 `reload()` 负责入参规整和多模型循环,内层 `reloadInternal()` 负责单个模型的完整加载。同时它还实现了**多模型加载**(传数组)和**可中断**(通过 `AbortController`)两个能力。

#### 4.2.2 核心流程

外层 `reload()`([src/engine.ts:203-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L203-L246))的逻辑:

```text
reload(modelId, chatOpts)
  │
  ├─ 0. await this.unload()          ← 先卸载所有已加载模型(所以 reload 也是"换模型")
  ├─ 1. 把 modelId / chatOpts 统一转成数组
  ├─ 2. 若 chatOpts 存在且长度与 modelId 不符 → ReloadArgumentSizeUnmatchedError
  ├─ 3. 若 modelId 有重复            → ReloadModelIdNotUniqueError
  ├─ 4. this.reloadController = new AbortController()   ← 本轮加载的中断开关
  │     for 循环:逐个 await reloadInternal(modelId[i], chatOpts[i])
  │       ├─ 捕获 AbortError → 仅 log.warn 后正常返回(不算失败)
  │       └─ 其他错误 → 继续向上抛
  └─ finally: 清空 reloadController
```

内层 `reloadInternal()` 的完整步骤见 4.2.3 的步骤表——本讲实践任务要求你整理的正是它。

#### 4.2.3 源码精读

**第一步:查模型记录。** [src/engine.ts:256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L256) 调用 `findModelRecord(modelId, this.appConfig)`,其实现位于 [src/support.ts:208-217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L208-L217):

```typescript
export function findModelRecord(
  modelId: string,
  appConfig: AppConfig,
): ModelRecord {
  const matchedItem = appConfig.model_list.find(
    (item) => item.model_id == modelId,
  );
  if (matchedItem !== undefined) return matchedItem;
  throw new ModelNotFoundError(modelId);
}
```

这段代码在 appConfig 白名单里线性查找 `model_id`,找不到就抛 `ModelNotFoundError`——这就是「传入未登记的 modelId 会报错」的根源。

**第二步:下载并合并配置。** [src/engine.ts:272-296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L272-L296) 创建 `webllm/config` 缓存、拉取 `mlc-chat-config.json`、可选做 SRI 校验,最后做三层合并:

```typescript
const curModelConfig: ChatConfig = {
  ...JSON.parse(new TextDecoder().decode(configData)),
  ...modelRecord.overrides,
  ...chatOpts,
} as ChatConfig;
```

这一行正是 u1-l4 讲过的「仓库配置 → 记录级 overrides → 用户 chatOpts,后写者赢」的落地点。

**第三步:下载 wasm 并实例化 tvmjs 运行时。** [src/engine.ts:300-341](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L300-L341) 中有个值得注意的细节——`fetchWasmSource` 对三种 URL 采用三种策略([src/engine.ts:309-324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L309-L324)):

| URL 形态 | 策略 | 原因 |
| --- | --- | --- |
| 含 `localhost` | 直接 `fetch`,不缓存 | 本地开发时代码频繁更新 |
| 不以 `http` 开头(相对路径) | 相对页面地址解析后直接 `fetch` | 同源部署的 wasm 可能随时刷新 |
| 其他远程 URL | `wasmCache.fetchWithCache` 走 `webllm/wasm` 缓存 | 正式分发产物,值得持久化 |

随后 `tvmjs.instantiate(wasm.buffer, ...)` 把字节码变成可调用的 TVM 运行时实例。

**第四步:GPU 检测与特性校验。** [src/engine.ts:348-367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L348-L367) 调用 `tvmjs.detect_GPUDevice()`:拿不到设备抛 `WebGPUNotAvailableError`;`required_features` 里有 `shader-f16` 而设备不支持时抛 `ShaderF16SupportError`(u1-l2 见过它的报错)。接着 [src/engine.ts:374-385](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L374-L385) 注册 `device.lost` 回调并 `tvm.initWebGPU(device)`——注意 `deviceLostIsError` 这个标志位,它让「主动 unload 导致的 device lost」不误报为错误。

**第五步:tokenizer 与权重。** [src/engine.ts:387-397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L397) 依次加载 tokenizer、调用 `tvm.fetchTensorCache(modelUrl, ...)` 把全部权重拉进 `webllm/model` 缓存。这一步通常是整个 reload 中耗时最长的,`initProgressCallback` 报告的下载进度主要来自这里。

**第六步:构造管线。** [src/engine.ts:402-415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L402-L415) 按 `model_type` 二选一:

```typescript
if (modelRecord.model_type === ModelType.embedding) {
  newPipeline = new EmbeddingPipeline(tvm, tokenizer, curModelConfig);
} else {
  newPipeline = new LLMChatPipeline(tvm, tokenizer, curModelConfig, logitProcessor);
}
await newPipeline.asyncLoadWebGPUPipelines();
this.loadedModelIdToPipeline.set(modelId, newPipeline);
```

`LLMChatPipeline` 的构造函数([src/llm_chat.ts:179-463](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L179-L463))内部完成:创建 VirtualMachine 并按注册表取出 `prefill`/`decode` 等 PackedFunc([src/llm_chat.ts:231-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L231-L246))、按名字从缓存装载权重 `params`([src/llm_chat.ts:343-350](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L343-L350))、调用 `create_tir_paged_kv_cache` 创建 KV cache([src/llm_chat.ts:426-440](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L426-L440))。最后的 `asyncLoadWebGPUPipelines()`([src/llm_chat.ts:715-716](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L715-L716))触发 WebGPU shader 异步编译。管线细节属于单元三,本讲只需记住「`new LLMChatPipeline(...)` 返回时,VM、权重、KV cache 都已就位」。

**第七步:收尾。** [src/engine.ts:418-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L418-L429) 用 `initProgressCallback` 汇报 `progress: 1` 和 "Finish loading on \<GPU 型号\>";若加载期间设备丢失(`deviceLostInReload`)则抛 `DeviceLostError`,提示用户换更小的模型重试。

**`reload()` 完整步骤表**(实践任务要求的成品,可直接对照):

| # | 步骤 | 源码位置 |
| --- | --- | --- |
| 1 | 卸载所有已加载模型 | [engine.ts:208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L208) |
| 2 | 在 appConfig.model_list 中查找 ModelRecord | [engine.ts:256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L256) / [support.ts:208-217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L208-L217) |
| 3 | 解析模型 URL、记录 model_type | [engine.ts:261-269](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L261-L269) |
| 4 | 从 `webllm/config` 缓存拉取 mlc-chat-config.json(含 SRI 校验) | [engine.ts:272-291](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L272-L291) |
| 5 | 三层合并得到最终 ChatConfig | [engine.ts:292-296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L292-L296) |
| 6 | 从 `webllm/wasm` 缓存拉取 model_lib(localhost/同源不缓存) | [engine.ts:300-325](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L300-L325) |
| 7 | wasm 完整性校验 + `tvmjs.instantiate` 实例化运行时 | [engine.ts:327-341](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L327-L341) |
| 8 | GPU 检测与 required_features 校验(shader-f16 等) | [engine.ts:348-367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L348-L367) |
| 9 | 注册 device.lost 回调并绑定 WebGPU 设备 | [engine.ts:374-385](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L374-L385) |
| 10 | 加载 tokenizer | [engine.ts:387-393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L393) |
| 11 | `fetchTensorCache` 下载全部权重到 `webllm/model` 缓存 | [engine.ts:394-397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L394-L397) |
| 12 | 构造 LLMChatPipeline / EmbeddingPipeline(VM、PackedFunc、权重 params、KV cache) | [engine.ts:402-412](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L402-L412) |
| 13 | `asyncLoadWebGPUPipelines()` 编译 WebGPU shader | [engine.ts:413](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L413) |
| 14 | 把 pipeline 与 lock 注册进引擎的 Map | [engine.ts:414-415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L414-L415) |
| 15 | 汇报 progress: 1,检查设备丢失 | [engine.ts:418-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L418-L429) |

**生命周期三兄弟的对比**——`unload`、`resetChat`、`interruptGenerate` 角色完全不同:

`unload()` 位于 [src/engine.ts:432-451](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L432-L451):

```typescript
async unload() {
  this.deviceLostIsError = false; // 避免 unload 自身触发 device.lost 错误
  for (const entry of Array.from(this.loadedModelIdToPipeline.entries())) {
    const pipeline = entry[1];
    pipeline.dispose();       // 释放权重、KV cache、所有 PackedFunc
    await pipeline.sync();    // 等设备真正销毁
  }
  this.loadedModelIdToPipeline.clear();
  ... // 清空其余三个 Map
  if (this.reloadController) {
    this.reloadController.abort("Engine.unload() is called.");
    this.reloadController = undefined;
  }
}
```

`dispose()` 的具体释放清单在 [src/llm_chat.ts:486-498](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L486-L498):params、decoding、prefill、kvCache、采样函数等逐一销毁。注意 `unload()` 末尾会 abort 正在进行的 reload——这正是 `examples/abort-reload` 演示的机制。

`resetChat()` 位于 [src/engine.ts:1326-1344](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1326-L1344),只是转发给管线的 `resetChat`([src/llm_chat.ts:530](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530)),清空对话历史与 KV cache 中的旧会话、但**保留模型本体**;它还捕获了「模型未加载」的异常并当作 no-op([src/engine.ts:1330-1343](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1330-L1343))——引擎还没加载模型时调用 `resetChat()` 不会报错。

`interruptGenerate()` 位于 [src/engine.ts:771-773](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L771-L773),仅一行 `this.interruptSignal = true`,由生成主循环检查并触发停止——它既不动模型也不动会话,只影响「当前这一次生成」。

| 方法 | 模型权重 | KV cache/会话 | 典型时机 |
| --- | --- | --- | --- |
| `reload(modelId)` | 卸载旧的、加载新的 | 全新 | 换模型 |
| `resetChat(keepStats?)` | 保留 | 清空 | 开新话题 |
| `interruptGenerate()` | 保留 | 保留 | 用户点「停止」 |
| `unload()` | 释放 | 释放 | 关闭页面/彻底释放显存 |

#### 4.2.4 代码实践

1. **实践目标**:利用官方 `abort-reload` 示例,体验「reload 中途被 unload 打断」的行为,理解 `AbortController` 的作用。
2. **操作步骤**:
   - 阅读 [examples/abort-reload/src/get_started.js](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/src/get_started.js) 全文(仅 32 行):它 `new MLCEngine({ initProgressCallback })` 后立刻 `engine.reload(selectedModel)`(不 await),然后在 5 秒后调用 `engine.unload()`([get_started.js:27-32](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/abort-reload/src/get_started.js#L27-L32))。
   - 进入 `examples/abort-reload`,`npm install && npm start`,打开页面。
   - 观察前 5 秒进度文本不断更新;5 秒时控制台打印 `calling unload`。
3. **需要观察的现象**:unload 之后进度文本停止更新,控制台出现一条 `Reload() is aborted.` 的 warn 日志(来自 [src/engine.ts:238-241](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L238-L241)——`reload` 捕获 `AbortError` 后只警告、不抛错),页面不崩溃。
4. **预期结果**:`reload()` 的 Promise 正常 resolve(而非 reject),引擎回到空状态。原因是 `unload()` 里的 `reloadController.abort(...)` 使缓存下载抛出 `DOMException`,被外层 `reload` 识别为中止信号。**待本地验证**(具体 warn 文本以运行为准)。

#### 4.2.5 小练习与答案

**练习 1**:`reload(["modelA", "modelA"])` 会发生什么?哪一行代码拦截了它?

**答案**:抛出 `ReloadModelIdNotUniqueError`。由 [src/engine.ts:224-226](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L224-L226) 拦截:`new Set(modelId).size < modelId.length` 说明存在重复。之所以禁止重复,是因为引擎用 modelId 作为四个 Map 的键,重复加载会互相覆盖。

**练习 2**:为什么 `reload()` 的第一步就要 `await this.unload()`,而不是直接加载新模型?

**答案**:模型的权重和 KV cache 占用的是同一个 WebGPU 设备的显存。不先释放旧模型就直接加载新模型,两份权重叠加极易超出显存上限导致 device lost。`unload()` 会 `dispose()` 所有旧 pipeline 并 `await pipeline.sync()` 等设备真正销毁后再继续,保证新模型在干净的显存状态下加载。

**练习 3**:用户在生成过程中点「停止」用 `interruptGenerate()`,想彻底释放显存用 `unload()`,想清空对话历史但保留模型用 `resetChat()`。如果调用顺序是先 `unload()` 再 `resetChat()`,会发生什么?

**答案**:不会报错。`resetChat()` 内部用 `getLLMStates` 取管线,模型已卸载时会抛 `ModelNotLoadedError`,但 [src/engine.ts:1330-1343](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1330-L1343) 明确捕获该异常并按 no-op 处理(仅打 debug 日志)。这是接口设计上「宽容调用」的一个例子。

### 4.3 MLCEngineInterface 抽象

#### 4.3.1 概念说明

`MLCEngineInterface` 定义在 [src/types.ts:62](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L62),它列出了一个「可对话引擎」必须具备的全部能力。`MLCEngine` 用 `implements MLCEngineInterface`([src/engine.ts:115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L115))声明自己实现了这份契约。

这个抽象存在的核心动机是**主线程/Worker 双实现**(u1-l3 提过,这里看细节):`WebWorkerMLCEngine` 是跑在主线程的「遥控器」,真正的 `MLCEngine` 跑在 Worker 线程,两者实现**同一个接口**。于是业务代码只需要面向接口编程:

```typescript
const engine: MLCEngineInterface = useWorker
  ? await CreateWebWorkerMLCEngine(worker, modelId)
  : await CreateMLCEngine(modelId);
// 后续所有调用完全一致,切换运行位置不改动任何业务代码
```

这也是典型的**依赖倒置**:上层 UI 依赖抽象接口,而非具体实现类。

#### 4.3.2 核心流程

接口成员可按生命周期分组(完整定义见 [src/types.ts:62-243](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L62-L243)):

```text
MLCEngineInterface
├─ 门面入口:chat / completions / embeddings(OpenAI 风格)
├─ 配置与观测:setInitProgressCallback / getInitProgressCallback /
│              setAppConfig / setLogLevel / runtimeStatsText /
│              getMaxStorageBufferBindingSize / getGPUVendor
├─ 生命周期:reload / unload / resetChat / interruptGenerate
├─ 生成类:chatCompletion / completion / embedding / forwardTokensAndSample
└─ 状态查询:getMessage
```

其中生命周期四方法在本讲的对应关系:`reload`([src/types.ts:112-115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L112-L115))、`unload`([src/types.ts:192](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L192))、`resetChat`([src/types.ts:199](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L199))、`interruptGenerate`([src/types.ts:185](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L185))。

引擎状态随这些方法迁移,可以画成一个小状态机:

```text
   new MLCEngine()
        │
        ▼
  ┌〔空〕── reload() ──▶ 〔加载中〕──成功──▶ 〔就绪〕◀──────────┐
  │   ▲                    │ │                     │  │          │
  │   │                 abort│ │异常                 │  │ 新请求    │
  │   │                    ▼ ▼                     ▼  │ 生成中    │
  │   └──── unload() ──────┴─┴──── unload() ◀──────┴──┘          │
  │                        (任何状态都可回到"空")                   │
  └〔空〕状态下 resetChat() 是 no-op;就绪状态可反复 resetChat()───┘
```

#### 4.3.3 源码精读

接口中 `reload` 的签名与文档位于 [src/types.ts:100-115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L100-L115):

```typescript
reload: (
  modelId: string | string[],
  chatOpts?: ChatOptions | ChatOptions[],
) => Promise<void>;
```

`unload` 与 `resetChat` 的契约注释([src/types.ts:187-199](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L187-L199))分别强调了「等待 WebGPU 设备完成所有已提交工作并自毁」和「清除全部记忆(可选保留统计)」——这两个语义在 4.2.3 的 `MLCEngine` 实现里都能找到一一对应的代码。

另外注意:接口里的请求方法都是**重载签名**。例如 `chatCompletion` 有四个重载([src/types.ts:131-142](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L131-L142)):非流式请求返回 `Promise<ChatCompletion>`,流式请求返回 `Promise<AsyncIterable<ChatCompletionChunk>>`。TypeScript 靠请求里是否有 `stream: true` 在**类型层面**区分两种返回值——这让你在写 `await engine.chatCompletion({ stream: true, ... })` 时能获得正确的自动补全,是单元二后续两讲(非流式/流式)的类型基础。

引擎内部路由请求的统一入口是私有方法 `getLLMStates`([src/engine.ts:1199-1208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1199-L1208))→ `getModelStates`([src/engine.ts:1229-1287](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1229-L1287)):根据请求的 `model` 字段和已加载模型列表选出管线与配置,选不出就抛 `ModelNotLoadedError` 或 `SpecifiedModelNotFoundError`。这个方法把「接口承诺的能力」和「引擎当前状态」连接了起来——**接口是静态的契约,四个 Map 是动态的状态,`getModelStates` 负责在每次调用时校验前者与后者匹配**。

#### 4.3.4 代码实践

1. **实践目标**:体会「面向 `MLCEngineInterface` 编程」带来的可替换性。
2. **操作步骤**:
   - 在你的 get-started 试验页(**示例代码**)中把变量显式声明为接口类型:
     ```typescript
     let engine: webllm.MLCEngineInterface;
     if (mode === "main-thread") {
       engine = await webllm.CreateMLCEngine("Llama-3.2-1B-Instruct-q4f32_1-MLC");
     } else {
       const worker = new Worker(new URL("./worker.mjs", import.meta.url), { type: "module" });
       engine = await webllm.CreateWebWorkerMLCEngine(
         worker, "Llama-3.2-1B-Instruct-q4f32_1-MLC",
       );
     }
     ```
   - 之后的 `engine.chatCompletion(...)`、`engine.resetChat()`、`engine.unload()` 写一份即可,两种模式共用。
3. **需要观察的现象**:TypeScript 对两个分支的返回值都不报类型错误;调用 `resetChat`、`unload` 等方法时自动补全一致。
4. **预期结果**:编译通过,切换分支只改一个布尔值。这正是接口抽象的价值——`WebWorkerMLCEngine` 将在 u5-l1 详细展开,此处只需验证类型层面的统一。

#### 4.3.5 小练习与答案

**练习 1**:`MLCEngineInterface` 里为什么没有 `getMessage` 之外的内部状态(比如四个 Map)的暴露?

**答案**:接口是给 UI/业务层用的契约,只应包含稳定、必要的能力。四个 Map 是实现细节,暴露它们会把调用方耦合到「多模型按 modelId 索引」这一具体实现上,未来改动内部结构就会破坏所有使用者。`getMessage(modelId?)` 这类以 modelId 为可选参数的方法,已经足以让多模型场景的调用方表达意图。

**练习 2**:对照接口列表([src/types.ts:62-243](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L62-L243)),`getMaxStorageBufferBindingSize` 和 `getGPUVendor` 属于哪一类能力?为什么它们也在引擎接口里?

**答案**:属于「环境观测」能力。它们本质是向底层 tvmjs/GPU 设备询问信息(实现在 [src/engine.ts:1156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1156) 与 [src/engine.ts:1185](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1185)),放在接口里是因为 Worker 模式下 UI 层拿不到设备对象,只能通过引擎(跨线程消息)代为查询——u3-l1 的实践会用到它们。

**练习 3**:如果一个新引擎实现(比如未来的 `SharedArrayBufferEngine`)漏实现了 `interruptGenerate`,会发生什么?

**答案**:TypeScript 在编译期直接报错(`implements MLCEngineInterface` 但缺少成员);即便绕过类型检查用 JS 调用,运行时也会得到 `undefined is not a function`。这就是「静态契约」的意义:接口把运行时才可能发现的遗漏提前到编译期。

## 5. 综合实践

**任务:制作一个「引擎生命周期实验台」页面**,把本讲三个模块串起来。

要求实现三个按钮和一个状态栏(**示例代码**框架):

```typescript
import * as webllm from "@mlc-ai/web-llm";

const MODEL_A = "Llama-3.2-1B-Instruct-q4f32_1-MLC";
const MODEL_B = "Qwen2.5-0.5B-Instruct-q4f16_1-MLC"; // 换模型,见证 reload 的"卸载+重载"

let engine = new webllm.MLCEngine({
  initProgressCallback: (r) => setStatus(`${r.text} (${(r.progress * 100).toFixed(0)}%)`),
});

// 按钮 1:加载/切换模型
async function onReload(modelId: string) {
  setStatus(`reload(${modelId}) 开始`);
  await engine.reload(modelId);
  setStatus("就绪");
}
// 按钮 2:卸载
async function onUnload() {
  await engine.unload();
  setStatus("空(已卸载)");
}
// 按钮 3:开新会话(观察 no-op 容错)
async function onReset() {
  await engine.resetChat();
  setStatus("会话已清空(模型保留)");
}
```

实验步骤与观察点:

1. 点击「加载 MODEL_A」:对照 4.2.3 的 15 步表格,把进度文本按阶段人工归类(配置下载 / wasm / 权重下载百分比 / "Finish loading on ...")。
2. 打开 DevTools → Application → Cache Storage,确认 `webllm/config`、`webllm/wasm`、`webllm/model` 三个作用域出现条目;打开浏览器任务管理器(Shift+Esc),记录 GPU 进程内存基线。
3. 点击「切换到 MODEL_B」:观察状态栏先经历 unload(对应步骤 1)再走完整加载流程;任务管理器中 GPU 内存先回落再上升。
4. 再次点击「加载 MODEL_A」(命中缓存):对比步骤 1 的总耗时,应显著缩短(权重来自缓存,无需重新下载)。
5. 点击「卸载」后点「开新会话」:验证 4.2.5 练习 3 的 no-op 行为,控制台应只有 debug 级日志,无报错。

**预期结果**:完整见证「空 → 加载中 → 就绪 → 空」的状态迁移;量化缓存对二次加载的加速;确认 `resetChat` 在空引擎上的容错。若你的浏览器任务管理器无法细分 GPU 内存,标注「待本地验证」即可,以 DevTools 缓存面板与进度文本为准。

## 6. 本讲小结

- `CreateMLCEngine` = `new MLCEngine(engineConfig)` + `await engine.reload(modelId)`,返回前模型已完全就绪;构造函数本身轻量,只初始化四个以 modelId 为键的 Map 和三个 OpenAI 风格门面。
- `reload()` 外层负责规整入参、多模型顺序加载与中断处理;内层 `reloadInternal()` 完成「查记录 → 下配置 → 下 wasm → GPU 检测 → tokenizer/权重 → 构造管线 → 编译 shader → 注册进 Map」约 15 个步骤,是引擎最重的路径。
- `reload` 的第一步永远是 `unload()`:先释放旧模型显存再加载新模型,避免两份权重叠加导致 device lost;`unload` 还会通过 `AbortController` 中断仍在进行的下载(`examples/abort-reload` 演示)。
- 生命周期三兄弟分工明确:`unload()` 释放一切、`resetChat()` 只清会话且对空引擎是 no-op、`interruptGenerate()` 只设中断标志影响当前生成。
- `MLCEngineInterface` 是引擎的静态契约,`MLCEngine` 与 `WebWorkerMLCEngine` 共同实现它,使业务代码面向接口编程、无感切换主线程/Worker 运行模式。

## 7. 下一步学习建议

下一讲 **u2-l2「chatCompletion 非流式调用与多轮对话」** 将沿着本讲建立的「就绪引擎」继续:精读 `engine.chatCompletion` 的非流式路径,包括请求字段校验(它会调用本讲提到的 `getModelStates` 路由)、多轮 messages 的 KV cache 复用、采样参数合并与最终响应组装。

建议提前浏览的源码:

- [src/engine.ts:787-970](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L970) — `chatCompletion` 的四个重载与实现体。
- [src/openai_api_protocols/chat_completion.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts) — 请求/响应类型定义(先只看类型,细节留到 u6-l1)。

如果想先深入了解本讲末尾一笔带过的管线内部(VM、PackedFunc、KV cache 的创建细节),可以跳到单元三的 u3-l1「LLMChatPipeline 初始化与 tvmjs 运行时」,再回到单元二继续接口层的学习。
