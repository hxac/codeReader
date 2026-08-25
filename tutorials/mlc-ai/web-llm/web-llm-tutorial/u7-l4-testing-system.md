# 测试体系与质量保障

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 WebLLM 测试体系的真实分层——一个与直觉相反的关键事实：**`tests/` 下全部 18 个 jest 测试文件都不加载真实模型、不需要 WebGPU**，可以在纯 Node 环境的 CI 里完整跑通；真实模型的端到端验证由 `tests/scripts/sanity_checks` 这个独立于 jest 之外的浏览器页面承担。
2. 掌握三层测试写法：纯函数单测（不 mock 任何东西）、原型替身测试（`Object.create` 绕过构造函数）、模块级 mock 测试（`jest.mock` 替换整个兄弟模块）。
3. 读懂 `jest.config.cjs` 中测试环境、根目录与覆盖率门槛的配置，理解 `npm test` 背后发生了什么。
4. 学会「从测试反推 API 预期行为」的源码阅读技巧——测试断言是最精确的行为文档。
5. 亲手为 `temperature` 的边界值补充一个新测试用例，并跑通提交到自己的分支。

## 2. 前置知识

- **jest 与 ts-jest**：jest 是 JavaScript/TypeScript 最常用的测试框架；`ts-jest` 预设（preset）让 jest 能直接运行 `.ts` 文件，无需先编译。测试里用的 `describe`（分组）、`test`（用例）、`expect`（断言）、`jest.mock`（替身）都来自它。
- **测试替身（test double / mock）**：被测代码的协作对象（如 WebGPU 运行时、wasm 模型库）在 CI 里不存在或太贵，测试就用一个「假货」顶替。jest 的 `jest.mock("模块路径", 工厂函数)` 会把整个模块的导入替换成工厂返回的对象；`jest.requireActual("模块路径")` 则在 mock 环境里取回真模块；`jest.requireMock` 取回 mock 工厂产物本身。
- **为什么 WebLLM 的测试必须 mock**：推理管线依赖三样 CI 里没有的东西——WebGPU 设备（Node 环境无 `navigator.gpu`）、几十 MB 到几 GB 的模型权重（网络下载）、wasm 模型库。任何一项都足以让测试又慢又脆。WebLLM 的解法是：把「纯逻辑」与「GPU 执行」在源码里就分离开，然后只测纯逻辑。
- **TypeScript 的 `private` 只是编译期检查**：测试里 `pipeline["stopTokens"]` 或 `(pipeline as any).xxx` 这样的写法能直接读写私有字段，这是原型替身测试法的前提（本讲 4.3 详述）。
- **前置讲义承接**：u2-l2 讲过 `chatCompletion` 的请求校验与 usage 组装，u3-l4 讲过 `processNextToken` 的终止条件判定——本讲你会看到这些逻辑如何被测试精确锁住。u2-l1 讲过「引擎状态的全部载体是四个以 modelId 为键的 Map」，本讲 4.4 会展示测试如何直接向这四个 Map 注入假管线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [jest.config.cjs](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs) | jest 配置：node 环境、测试根目录、覆盖率门槛 |
| [package.json](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json) | `test` 等 npm scripts 与测试相关 devDependencies |
| [tests/generation_config.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/generation_config.test.ts) | 纯函数单测样板：测 `postInitAndCheckGenerationConfigValues` |
| [tests/llm_chat_pipeline.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts) | 管线级测试：mock xgrammar + `Object.create` 原型替身 |
| [tests/engine_integration.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts) | 引擎编排测试：整体 mock `../src/llm_chat`，手工注入引擎内部 Map |
| [tests/web_worker_handler.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts) | Worker 协议测试：mock `MLCEngine` 构造函数 + 伪造 `postMessage` |
| [tests/constants.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/constants.ts) | 测试共享的 ChatConfig 样例数据（llama3/llama2/phi3_v/qwen3） |
| [src/config.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts) | 被测函数 `postInitAndCheckGenerationConfigValues` 所在 |
| [tests/scripts/sanity_checks/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/scripts/sanity_checks/sanity_checks.ts) | jest 之外的真实模型浏览器验证页，有独立 package.json |

## 4. 核心概念与源码讲解

### 4.1 jest 配置与测试入口

#### 4.1.1 概念说明

一个项目的测试体系从「怎么跑」开始看。WebLLM 的入口是 `package.json` 里的 npm scripts，配置只有 21 行的 `jest.config.cjs`。看懂这两个文件，就知道：测试跑在哪、找哪些文件、以及一道容易被忽视的**覆盖率质量门禁**——低于门槛 `npm test` 直接失败，这保证了测试不会随着代码膨胀而逐渐「欠账」。

#### 4.1.2 核心流程

`npm test` → `jest --coverage` → 按配置装配环境 → 依次执行匹配的测试文件 → 收集覆盖率 → 对比门槛 → 全部通过且达标才退出码为 0。

#### 4.1.3 源码精读

npm scripts 定义了质量门禁三件套（本讲关注 `test`）：

[package.json:8-14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L8-L14)：`test` 脚本是 `jest --coverage`——每次跑测试都强制收集覆盖率；`lint` 同时检查 src、tests、examples 三个目录；`build` 走 rollup（下一讲 u7-l5 详述）。

[jest.config.cjs:1-6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L1-L6)：三个关键决定——
- `preset: "ts-jest"`：直接跑 TypeScript 源码，不经过编译产物，测试永远对着最新源码；
- `testEnvironment: "node"`：**没有浏览器 API**。这意味着测试拿不到 `navigator.gpu`、`caches`、真实 `Worker`——这正是整套测试必须 mock 的根本原因（也解释了 4.4 里 Worker 测试为什么要伪造 `globalThis.postMessage`）；
- `roots` 同时覆盖 `tests/` 与 `src/`：源码目录里如果写了测试也会被执行；`modulePathIgnorePatterns` 排除 examples，避免示例工程干扰模块解析。

[jest.config.cjs:7-20](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L7-L20)：覆盖率门槛分两档——全局 statements/branches/functions/lines ≥ 25/20/20/25，而 `src/engine.ts` 单独要求 35/25/40/35。引擎层是所有请求的编排中枢，故门槛更高。低于任一数值，`jest --coverage` 以失败退出，CI 会被拦下。

> 观察技巧：跑单个文件不必每次全量——`npx jest tests/generation_config.test.ts`；用 `-t "关键字"` 还能只跑名字匹配的用例，本讲实践会用到。

#### 4.1.4 代码实践

1. **实践目标**：验证「全部测试可在无 WebGPU 的 Node 环境运行」这一论断，并熟悉单文件运行。
2. **操作步骤**：
   - 在仓库根目录执行 `npm install`（首次）；
   - 执行 `npx jest tests/generation_config.test.ts`，观察输出；
   - 再执行 `npm test`（即 `jest --coverage`），观察最后的覆盖率表格里 `engine.ts` 一行是否过门槛。
3. **需要观察的现象**：单文件运行绿通过；全量运行不出现在某个测试里发起网络下载或报 `navigator is not defined` 之类的错误。
4. **预期结果**：所有测试在纯 Node 环境（含 CI 容器）通过，覆盖率表格显示各项超过门槛值。若你的环境 `npm test` 因覆盖率统计口径差异出现波动，以单文件运行为准。（具体耗时与覆盖率数值：待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `testEnvironment` 必须是 `node` 而不是 `jsdom`？
**答案**：WebLLM 的测试目标是纯逻辑（校验函数、消息路由、状态机），不需要 DOM；而 jsdom 也不提供 WebGPU、CacheStorage 的真实实现，换成 jsdom 并不能减少 mock 工作量，反而拖慢启动。真需要浏览器行为时（sanity check）就用独立页面（4.4 末尾）。

**练习 2**：把 `src/` 也放进 `roots` 有什么好处？
**答案**：允许在源码文件旁边写内联测试并被执行；同时 jest 的模块解析与 haste map 会覆盖 src，配置上的 `collectCoverageFrom: ["src/**/*.{ts,tsx}"]` 才能与测试范围对齐，覆盖率统计不遗漏。

---

### 4.2 纯函数单测层：以 generation_config.test.ts 为样板

#### 4.2.1 概念说明

最底层、也最值得模仿的是**纯函数单测**：被测函数不碰 GPU、不碰网络，输入一个对象、要么抛错要么原位补默认值。`tests/generation_config.test.ts` 测的就是 u3-l5 讲过的引擎层采样参数校验函数 `postInitAndCheckGenerationConfigValues`。这类测试一个 mock 都不需要——「把逻辑写成可测的纯函数」本身就是 WebLLM 架构给出的示范。

#### 4.2.2 核心流程

测试的两组 `describe` 对应被测函数的两种行为：
- **非法值必须抛错**：`max_tokens <= 0`、`logit_bias` 越界或键非数字、`top_logprobs` 越界或缺依赖——断言用 `expect(() => ...).toThrow("错误消息子串")`；
- **合法输入原位补默认**：只设 `frequency_penalty` 时补 `presence_penalty = 0.0`；设 `logprobs` 未设 `top_logprobs` 时补 0——断言直接检查传入对象被改写后的字段值（「post init」的原位语义）。

#### 4.2.3 源码精读

测试文件骨架——import 被测函数与类型，`toThrow` 里写的是错误消息子串：

[tests/generation_config.test.ts:1-15](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/generation_config.test.ts#L1-L15)：第一个用例锁定 `max_tokens: 0` 必须抛出含 "Make sure \`max_tokens\` > 0" 的错误。注意模式：把构造与调用包在 `expect(() => {...})` 里，才能断言「同步抛错」。

[tests/generation_config.test.ts:76-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/generation_config.test.ts#L76-L91)：第二组「post init」用例不抛错，而是**直接读取被改写后的对象**——`genConfig.presence_penalty` 被补成 0.0、`genConfig.top_logprobs` 被补成 0。这就是「函数会原位改写请求」这一行为的测试表达。

对照被测源码，理解断言从何而来：

[src/config.ts:167-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L197)：函数开头定义了哨兵函数 `_hasValue`——注释明确写道：如果直接 `if value`，`value` 为 0 时会误判为「未设置」。这正是 `temperature: 0`（合法的贪心采样意图）不被当成缺省值的保障。随后逐项校验：penalty 双侧范围、`repetition_penalty`/`max_tokens` 下界、`top_p` 的 (0,1] 区间、**temperature 只查下界**（`< 0` 抛 `NonNegativeError`，0 与任何正值都放行）。

[src/error.ts:38](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L38)：`NonNegativeError` 继承自 `ConfigValueError` 小家族（u7-l1 讲过全库仅有的两个错误家族之一），本讲实践会直接断言它。

放行 `temperature = 0` 不会导致采样除零吗？钳制发生在管线层：

[src/llm_chat.ts:1898](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1898)：采样前 `temperature = Math.max(1e-6, temperature)`，注释标明 "to prevent division by zero"。即校验层允许 0、执行层钳到 \( \max(10^{-6}, T) \)——两层各司其职，这是「从测试反推 API 行为」时必须跨文件对照才不会误读的地方。

#### 4.2.4 代码实践

1. **实践目标**：为 `temperature` 边界值补充测试（本讲规格指定的核心实践），并跑通。
2. **操作步骤**：
   - 新建分支：`git checkout -b test/temperature-boundary`；
   - 在 `tests/generation_config.test.ts` 的第二个 `describe`（"Check generation post init"）内追加两个用例（示例代码，模仿文件内既有风格）：

     ```ts
     import { NonNegativeError } from "../src/error"; // 加到文件顶部 import 区

     // 追加到 "Check generation post init" 组内
     test("temperature of zero is legal and kept as-is", () => {
       const genConfig: GenerationConfig = {
         max_tokens: 10,
         temperature: 0,
       };
       expect(() =>
         postInitAndCheckGenerationConfigValues(genConfig),
       ).not.toThrow();
       expect(genConfig.temperature).toBe(0);
     });

     test("negative temperature throws NonNegativeError", () => {
       const genConfig: GenerationConfig = {
         max_tokens: 10,
         temperature: -0.1,
       };
       expect(() =>
         postInitAndCheckGenerationConfigValues(genConfig),
       ).toThrow(NonNegativeError);
     });
     ```

   - 运行 `npx jest tests/generation_config.test.ts`；
   - 通过后运行 `npx prettier ./tests/ --check`（必要时 `--write`），再 `git add tests/generation_config.test.ts && git commit`。
3. **需要观察的现象**：两个新用例均绿；`temperature: 0` 用例不仅不抛错，且字段值保持 0（证明 `_hasValue` 哨兵起了作用）；`-0.1` 用例抛出 `NonNegativeError`。
4. **预期结果**：全文件测试通过，覆盖率不低于改动前。若你想进一步验证 `toThrow(NonNegativeError)` 与 `toThrow("temperature")` 两种断言写法等价，可各跑一次对比。（运行输出：待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_hasValue` 要显式排除 `undefined` 与 `null`，而不是用 `if (config.temperature)`？
**答案**：JS 的 falsy 集合包含 `0`、`""`、`NaN`。`temperature: 0`、`top_logprobs: 0` 都是合法显式取值，直接 `if (value)` 会把它们误判为「未设置」，从而跳过校验或错误地覆盖默认值。`_hasValue` 只把 `undefined`/`null` 当缺省。

**练习 2**：测试为何断言 `genConfig.presence_penalty` 被「补 0」而不是断言函数返回值？
**答案**：`postInitAndCheckGenerationConfigValues` 返回 `void`，它的契约就是**原位改写（post init）**传入的 config 对象。引擎随后把这个已被补全的对象继续往下传，因此测试直接检查副作用对象的状态，这才忠实于真实调用方式。

**练习 3**：从 [tests/generation_config.test.ts:17-41](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/generation_config.test.ts#L17-L41) 的两个 logit_bias 用例反推：键的合法格式是什么？
**答案**：键必须是「数字的字符串形式」（如 `"1355"`），值必须在 (-100, 100]。键写成标识符（`thisRaisesError`）抛 InvalidNumberStringError，值 155 抛 RangeError——对应 [src/config.ts:216-231](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L216-L231) 的两道检查。

---

### 4.3 管线级测试 llm_chat_pipeline.test.ts：原型替身法

#### 4.3.1 概念说明

`LLMChatPipeline` 的构造函数需要 tvmjs 实例、tokenizer、wasm 元数据——全是 CI 里不存在的东西。但 `processNextToken`、`prefillStep` 这些方法本身的逻辑（终止判定、KV 复用、grammar 缓存）又值得直接测。WebLLM 的解法非常巧妙：**不调用构造函数，用 `Object.create(LLMChatPipeline.prototype)` 造一个「只有方法、没有初始化」的对象，再手工把方法依赖的私有字段一个个填上**。被测的是真方法，依赖全是测试自己捏的——这比整体 mock 类保真得多。

#### 4.3.2 核心流程

```
jest.mock("@mlc-ai/web-xgrammar")     ← 替换掉 wasm 依赖的语法引擎包
        ↓
createPipeline():
  pipeline = Object.create(LLMChatPipeline.prototype)   ← 绕过构造函数
  pipeline["stopTokens"] = []            ← 手工填私有字段
  pipeline["conversation"] = { ...jest.fn() }   ← 依赖用 jest.fn 假对象
  pipeline["tokenizer"] = { encode/decode: jest.fn }
  pipeline["embedAndForward"] = async (chunk, len) => { filledKVCacheLength += len; ... }
        ↓
(pipeline as any).processNextToken(42)   ← 调用真实原型方法
        ↓
expect(pipeline["stopTriggered"]).toBe(true)  ← 断言私有状态
```

#### 4.3.3 源码精读

第一步，mock 掉 xgrammar（wasm 包，Node 里加载会失败），并预留跨 mock 断言的句柄：

[tests/llm_chat_pipeline.test.ts:6-40](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L6-L40)：`jest.mock("@mlc-ai/web-xgrammar", () => {...})` 用工厂把 `TokenizerInfo`、`GrammarCompiler`、`GrammarMatcher` 全换成 `jest.fn`。注意工厂里额外挂了 `__compileGrammar`、`__instances` 这类下划线句柄——这是「把 mock 内部状态暴露给测试」的惯用法。

[tests/llm_chat_pipeline.test.ts:58-67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L58-L67)：`jest.requireMock` 取回 mock 工厂产物，拿到 `__instances` 数组与编译 mock 的引用；`beforeEach` 里清空——保证用例之间互不污染（u6-l3 讲过的 grammar matcher 缓存复用，正是靠这些句柄断言的）。

第二步，原型替身工厂——本讲最想让你学会的模式：

[tests/llm_chat_pipeline.test.ts:69-100](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L69-L100)：`Object.create(LLMChatPipeline.prototype)` 创建对象但不执行构造函数；随后用方括号语法逐一填入方法运行所需的私有字段：`stopTriggered`、`conversation`（一个带 `jest.fn()` 方法的假会话）、`outputIds`、`appearedTokensFreq`、假 tokenizer（`decode` 把 id 映射成 `t${id}` 便于肉眼比对）。类型上用 `LLMChatPipeline & Record<string, any>` 消除私有字段访问的编译报错。

[tests/llm_chat_pipeline.test.ts:101-147](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L101-L147)：继续填数值型字段（`contextWindowSize: 16`、`prefillChunkSize: 8`）、假 `tvm`/`device`，以及关键的一行：`embedAndForward` 被替换成「把 chunkLen 累加进 `filledKVCacheLength`」的假实现——这就把 u3-l3 讲的事务式 KV 写入模拟出来了，使 `getInputData` 的全量/增量分支测试成为可能。

第三步，看一个用例如何锁住 u3-l4 讲的终止条件：

[tests/llm_chat_pipeline.test.ts:150-157](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L150-L157)：设置 `stopTokens = [42]` 后调真实 `processNextToken(42)`，断言 `stopTriggered` 为 true、`finishReason` 为 "stop"、且 `conversation.finishReply` 被以空字符串调用（停止 token 在追加前被拦截——正是 u3-l4 讲的「停止 token 不进正文」）。

[tests/llm_chat_pipeline.test.ts:276-286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L276-L286)：KV 复用分支——`filledKVCacheLength` 为 0 时走 `getPromptArray`（全量编码），置 1 后走 `getPromptArrayLastRound`（增量编码）。u2-l2 讲的「多轮对话命中 KV cache 只编增量」在测试层就是这么被验证的。

[tests/llm_chat_pipeline.test.ts:297-372](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L297-L372)：纯计算函数的参数化测试——`calculateResizeShape`/`calculateCropShape`/`computeImageEmbedSize` 只依赖 `config.model_type`，用假 config 就能对 u3-l6 讲过的 phi3_v 图片 token 折算（336×336 → 2509）做表驱动断言。

#### 4.3.4 代码实践

1. **实践目标**：体会「只跑一个 describe」与「阅读一个测试反推行为」。
2. **操作步骤**：
   - 执行 `npx jest tests/llm_chat_pipeline.test.ts -t "calculateResizeShape"`，只跑图片缩放那一组；
   - 打开源码对照：在 `src/llm_chat.ts` 中 Grep `calculateResizeShape`，核对测试断言的 `[1344, 1344]` 等数值对应实现里的哪段换算；
   - 挑一个用例（如 `processNextToken respects max_tokens...`）把 `max_tokens: 1` 改成 `2`，重跑观察哪个断言失败。
3. **需要观察的现象**：`-t` 过滤后仅 3 个用例执行；改动参数后 `finishReason` 相关断言失败——因为 decode 循环要多跑一步才触及上限。
4. **预期结果**：能指出失败断言与 [src/config.ts:189-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L189-L191) 的 `max_tokens` 校验、管线内 `processNextToken` 计数逻辑的对应关系。（改参数实验的完整输出：待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `Object.create(LLMChatPipeline.prototype)` 而不是 `new LLMChatPipeline(...)` 或整体 mock 类？
**答案**：`new` 需要真实 tvm 实例且构造里会分配 KV cache（GPU 操作），CI 不可行；整体 mock 类则把被测方法也换掉了，测试失去意义。`Object.create` 保留全部真方法、跳过初始化，依赖由测试手工提供——被测逻辑 100% 是产品代码。

**练习 2**：假 `conversation` 的方法为什么用 `jest.fn()` 而不是写个真的？
**答案**：测试关心的是「管线是否在正确时机调用了会话的哪个方法、传了什么参数」（如 `finishReply` 收到 `""`），`jest.fn` 自带调用记录，配合 `toHaveBeenCalledWith` 精确断言；若用真 Conversation，还要先构造合法模板配置，且断言只能落在间接状态上，测不出调用契约。

**练习 3**：`beforeEach` 里 `grammarMatcherInstances.length = 0` 的作用是什么？
**答案**：mock 工厂是模块级单例，`__instances` 数组会跨用例累积；不清空的话，「创建了几次 matcher」这类断言会被前一个用例的残留污染，产生顺序耦合的脆弱测试。

---

### 4.4 引擎与 Worker 的模块级 mock 测试

#### 4.4.1 概念说明

再往上一层是**编排逻辑**的测试：`MLCEngine` 如何把请求路由到管线、组装 usage；`WebWorkerMLCEngineHandler` 如何把 19 种消息路由到引擎方法（u5-l2）。这些被测对象本身不碰 GPU，但它们的协作者（管线、Worker 环境）需要替换。手段是把 `jest.mock` 指向**兄弟源码模块**（`../src/llm_chat`、`../src/engine`），而不是外部 npm 包。`tests/engine_integration.test.ts` 的文件头注释一语道破本讲主题：*"Deterministic MLCEngine tests that run without WebGPU by mocking LLMChatPipeline."*

#### 4.4.2 核心流程

引擎测试的装配流水线：

```
jest.mock("../src/llm_chat")   ← MockLLMChatPipeline 类整体顶替
jest.mock("../src/embedding")  ← MockEmbeddingPipeline 顶替
        ↓
new MLCEngine({ appConfig: 假 model_list })
        ↓
手工向 engine 的 4 个内部 Map 注入 (modelId → 假管线/假配置/假类型/新锁)
        ↓
调真实 engine.chatCompletion / completion / embedding
        ↓
断言响应结构、usage 数字、假管线上的调用计数
```

#### 4.4.3 源码精读

[tests/engine_integration.test.ts:25-35](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L25-L35)：`jest.mock("../src/llm_chat", ...)` 把整个管线模块替换掉；工厂内部第一行用 `jest.requireActual("../src/conversation")` 取回**真** Conversation——因为假管线要构造真会话对象来维持引擎的会话比对逻辑（u3-l2 讲的 `compareConversationObject`）。这种「mock 一层、actual 一层」的精细控制是模块级 mock 的精髓。

[tests/engine_integration.test.ts:80-115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L80-L115)：`MockLLMChatPipeline` 不是空壳——它忠实实现了引擎会读取的全部统计接口（`getCurRoundDecodingTokensPerSec` 等）与解码协议：`decodeStep` 每次追加 `|tokenN|`，到 `decodeLimit` 或 `max_tokens` 即置 stop。这使引擎层的 usage 组装（u7-l3 讲的 `usage.extra`）可以被确定性地断言。

[tests/engine_integration.test.ts:261-286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L261-L286)：装配函数先用假 `model_list` 构造真 `MLCEngine`（构造函数轻量，u2-l1 讲过只建四个 Map），然后**直接向 `loadedModelIdToPipeline` 等四个 Map set 假管线**——跳过 `reload()` 全流程，把引擎瞬间置为「已加载」状态。引擎封装的私有性挡不住测试的 `(engine as any)`。

[tests/engine_integration.test.ts:364-385](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L364-L385)：第一个用例验证 u2-l2 的核心行为——`n: 2` 产出 2 个 choice、`usage.completion_tokens` 按假管线的确定性输出算出精确值 6、`prefillCallCount` 为 2（每个候选一次 prefill）。另外注意 `jest.useFakeTimers().setSystemTime(FIXED_CREATED_DATE)`（[第 224-225 行](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L224-L225)）：固定时钟让 `created` 时间戳可精确断言为秒数 1712298896——这正是 git 近期提交 c5c7c86「Return OpenAI created timestamps in seconds」所加固的行为，测试与提交历史可以互相印证。

[tests/engine_integration.test.ts:415-444](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/engine_integration.test.ts#L415-L444)：流式测试用 `for await` 消费引擎返回的 AsyncIterable，断言 chunk 序列结构——首 chunk 的 delta 含 prompt 回显（假管线的 message 格式决定）、倒数第二个 chunk 带 `finish_reason: "stop"`、最后一个 chunk 是 `include_usage` 换来的 usage chunk。u2-l3 讲的流式骨架在测试里完整复现。

Worker 协议测试换一个 mock 靶子——引擎本身：

[tests/web_worker_handler.test.ts:32-36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L32-L36)：`jest.mock("../src/engine")` 把 `MLCEngine` 构造函数替换成返回手工拼的 `mockEngineInstance`（[第 19-30 行](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L19-L30)，每个方法都是 `jest.fn`）。被测的 `WebWorkerMLCEngineHandler` 是真类——它拿到的是假引擎。

[tests/web_worker_handler.test.ts:47](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L47)：node 环境没有 Worker 的 `postMessage`，测试在 `beforeEach` 里把它伪造到 `globalThis` 上。于是 u5-l1 讲的信封协议可以被逐字断言。

[tests/web_worker_handler.test.ts:68-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L68-L91)：发一条 `kind: "chatCompletionNonStreaming"` 消息，断言三件事——影子状态失配时先 `reloadMock`（u5-l2 的 reloadIfUnmatched 自愈）、假引擎方法被调用、`postMessage` 收到 `{kind:"return", uuid:"task-1", content:...}` 的完整信封。消息路由表就这样被测试固化。

最后补一块拼图——**真实模型的测试去哪了**：

[tests/scripts/sanity_checks/sanity_checks.ts:14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/scripts/sanity_checks/sanity_checks.ts#L14)：该目录有自己的 `package.json`（`tests/.gitignore` 还专门忽略其 `package-lock.json`），是一个用 `CreateMLCEngine` 真实加载模型、在浏览器里跑的验证页，**不在 jest 套件内**。结论：jest 负责「逻辑永远绿」，真实 GPU 验证由开发者手动/专项执行——这就是「无 GPU 环境跑单测」的完整答案。

#### 4.4.4 代码实践

1. **实践目标**：从 mock 引擎的调用记录里读出 Worker 消息路由表的一行。
2. **操作步骤**：
   - 运行 `npx jest tests/web_worker_handler.test.ts`；
   - 打开 `src/web_worker.ts`，找到 `chatCompletionNonStreaming` 分派到引擎方法的 switch/映射处；
   - 对照测试第 68-91 行，用一张三列表格记录：消息 kind → 调用的引擎方法 → 回发的 `kind`（return/throw）。
3. **需要观察的现象**：mock 引擎的每个 `jest.fn` 在对应消息到达后被调用一次；`postMessage` 的信封里 uuid 与请求完全一致。
4. **预期结果**：你为 u5-l2 讲过的「19 种消息路由」亲手核实了至少一条完整链路；如需全表，可继续为 `forwardTokensAndSample`、`interruptGenerate` 等消息各追一条。（运行输出：待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`engine_integration.test.ts` 为什么 mock `../src/llm_chat` 却 `requireActual("../src/conversation")`？
**答案**：被测对象是引擎的编排逻辑，管线是它的协作层（要替换）；但假管线内部要持有一个真 Conversation，引擎每次请求都会比对会话对象决定 KV 复用，用假 Conversation 会把这层真实交互测丢。

**练习 2**：向引擎的 Map 注入假管线后，`engine.getMessage()` 为什么能正常工作？
**答案**：u2-l1 讲过引擎只是「以 modelId 为键的四个 Map + 门面」，全部推理能力都委托给管线。注入假管线后，引擎的请求路由、锁、usage 组装走的全是真代码，只有最底层的「生成」是假的——这正好是引擎层该测的全部。

**练习 3**：fake timer 在 `created` 时间戳断言中解决了什么问题？
**答案**：若用真实时钟，`response.created` 随运行时刻变化无法精确断言。`setSystemTime` 把时间钉死在 2024-04-05T06:34:56.789Z，测试即可断言 `created` 等于该时刻对应的秒数 1712298896，同时顺带锁住了「秒级时间戳」这一单位约定。

---

## 5. 综合实践

把本讲三层技巧串成一次完整的「补测试」贡献流（在 4.2.4 实践基础上扩展）：

1. 建分支 `test/temperature-boundary`，完成 4.2.4 的两个 `temperature` 用例；
2. 再加第三层验证——通读 [src/config.ts:192-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L192-L197)，为 `top_p` 的边界补一个用例（提示：对照 `(_hasValue(config.top_p) && config.top_p! <= 0) || config.top_p! > 1` 思考 `top_p: 0`、`top_p: 1.5` 各自的结局，先在源码里推演再写断言）；
3. 运行 `npx jest tests/generation_config.test.ts` 全绿后，运行 `npm test` 观察覆盖率表格中 `All files` 与 `src/config.ts` 行的变化；
4. 执行 `npm run lint` 确认格式与规范通过（失败就 `npx prettier ./tests/ --write` 后重试）；
5. `git commit` 到自己的分支。如果打算提 PR，这正是仓库最低成本的贡献切入点——校验函数的行为一旦被测试锁住，后续重构都有安全网。

验收标准：新增用例全部通过；`npm test` 整体不因你的改动而失败；能口头回答「temperature=0 为什么合法、除零风险在哪一层被消除」。

## 6. 本讲小结

- WebLLM 的 18 个 jest 测试文件**全部不加载真实模型、不需要 WebGPU**，在纯 Node 的 CI 里运行；真实模型验证由 jest 之外、带独立 package.json 的 `tests/scripts/sanity_checks` 浏览器页面承担。
- 测试分三层：**纯函数单测**（generation_config 等，零 mock）、**原型替身测试**（`Object.create(类.prototype)` 绕过构造函数 + 手工填私有字段，被测方法 100% 是产品代码）、**模块级 mock 测试**（`jest.mock` 兄弟模块，配 `requireActual` 精细保留需要真实的层）。
- `jest.config.cjs` 用 `testEnvironment: "node"`、双 roots 和**覆盖率门槛**（全局 25/20/20/25，`engine.ts` 单独 35/25/40/35）把质量变成硬约束；`npm test` 即 `jest --coverage`。
- 测试断言即行为文档：`_hasValue` 哨兵保证 `temperature: 0` 合法（只查下界），除零风险由管线层 [src/llm_chat.ts:1898](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1898) 的 `Math.max(1e-6, T)` 钳制消除——跨文件对照才不会误读 API。
- 惯用技巧集：`jest.requireMock` 暴露 `__instances` 句柄做跨 mock 断言、`beforeEach` 清理模块级 mock 状态、fake timer 固定 `created` 秒级时间戳、伪造 `globalThis.postMessage` 测 Worker 信封协议、`(engine as any)` 直接向引擎的四个 Map 注入假管线。

## 7. 下一步学习建议

- 下一讲 u7-l5《构建、发布与二次开发路线图》会把本讲的 `npm test`、`npm run lint` 纳入完整的「fork → 修改 → 质量门禁 → 提 PR」二开流程，并讲解 rollup 构建链。
- 想继续读测试，推荐按此顺序：`tests/openai_chat_completion.test.ts`（对照 u6-l1 的八道校验关卡）、`tests/cache_util.test.ts`（看如何 mock `@mlc-ai/web-runtime` 测缓存层）、`tests/service_worker.test.ts`（对照 u5-l3 的心跳与 reload 去重）。
- 想跑真实模型验证，进入 `tests/scripts/sanity_checks/` 目录读其 README 与 `sanity_checks.ts`，在支持 WebGPU 的浏览器里执行一次——那是 jest 套件刻意留给真实世界的那一环。
