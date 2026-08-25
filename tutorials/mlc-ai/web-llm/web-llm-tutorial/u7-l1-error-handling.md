# 错误处理体系：六十余个错误类的分层设计

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `src/error.ts` 中 68 个错误类的组织方式：几乎全平铺继承自 `Error`，只有 `ConfigValueError` 与 `FeatureSupportError` 两个小家族。
2. 按你自己确立的「环境不支持 / 配置非法 / 消息不合法 / 模型未就绪 / 功能不支持」五分类，给任意一个错误类归类，并指出它的触发条件与源码抛出位置。
3. 完整追踪两条 WebGPU 环境错误的触发路径：`detectGPUDevice()` 返回 `undefined` 时的 `WebGPUNotAvailableError`，以及 `required_features` 缺失时的 `ShaderF16SupportError` / `FeatureSupportError`。
4. 理解 `DeviceLostError` 如何把异步的 `device.lost` 回调转换为 `reload()` 末尾的同步抛出。
5. 在应用层针对可恢复错误做降级处理：区分「预期错误」（如未加载模型就调用 `resetChat`）与「真错误」，并在 Worker 场景下绕过 `instanceof` 失效的问题。

## 2. 前置知识

- **Error 与 `name` 字段**：JavaScript 的 `new Error(msg)` 会设置 `error.message`。子类化时通常还要手动设置 `error.name`，否则打印和日志里显示的仍是 `"Error"`。WebLLM 的每个错误类都在构造函数里写了 `this.name = "XXXError"`，这是后面「按 name 识别错误」策略的基础。
- **`instanceof` 的局限**：`instanceof` 只在同一份类定义（同一个模块实例）下可靠。如果页面同时从 CDN 和 npm 引入两份 WebLLM，或错误跨过 Worker 线程边界（结构化克隆会丢失类原型），`instanceof` 都会失效，只能退回检查 `error.name` 或匹配消息字符串。第 4.5 节会专门讨论。
- **WebGPU 基础**：浏览器通过 `navigator.gpu` 暴露 WebGPU API；`adapter.features` 是一个 Set，声明设备支持的扩展特性（如 `shader-f16`，半精度浮点）。q4f16 量化的模型必须在支持 `shader-f16` 的设备上才能运行。这些检测在 WebLLM 中由 tvmjs 的 `detectGPUDevice()` 完成。
- **三层校验架构回顾**（u6-l1 已讲）：协议层 `postInitAndCheckFields` 管请求结构 → 引擎层 `postInitAndCheckGenerationConfigValues` 管数值范围 → 会话层管角色分发。本讲把这三层里抛出的错误统一到一个视角下审视。
- **`reload()` 流程回顾**（u2-l1 已讲）：卸载旧模型 → 查 `ModelRecord` → 下载并校验 config/wasm → GPU 检测 → tokenizer 与权重 → 构造管线 → 预热 shader。本讲追踪的多数「环境错误」都发生在这条链路上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/error.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts) | 全部 68 个错误类的定义，每个类只含一个构造函数，本讲的「字典」 |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | `reloadInternal` 中 GPU 检测与设备丢失处理；`getModelStates` 的模型就绪守卫；`resetChat` 的吞错降级范例 |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | `findModelRecord` / `getModelIdToUse` 抛出的模型查找与选择错误；`getToolCallFromOutputMessage` 抛出的工具调用解析错误 |
| [src/config.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts) | 引擎层采样参数校验 `postInitAndCheckGenerationConfigValues` |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 管线层对采样参数的复核，以及窗口配置、多模态运行期错误 |
| [src/openai_api_protocols/chat_completion.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts) | 协议层 `postInitAndCheckFields` 的消息与多模态校验关卡 |
| [src/web_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts) | `handleTask`：错误跨线程退化为字符串的边界 |
| [src/extension_service_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts) | Chrome 扩展场景下 `WebGPUNotFoundError` 的唯一抛出点 |

## 4. 核心概念与源码讲解

### 4.1 错误体系总览：设计语法与两个陷阱

#### 4.1.1 概念说明

`src/error.ts` 共 629 行、68 个错误类，是全仓库错误语义的唯一来源。它的组织方式非常朴素：

- **几乎全平铺**：绝大多数类直接 `extends Error`，彼此没有继承关系。
- **仅两个小家族**：`ConfigValueError` 派生出 5 个数值校验子类（4.2 节）；`FeatureSupportError` 派生出 1 个 `ShaderF16SupportError`（4.3 节）。
- **构造函数即文档**：每个类的构造参数就是错误的「上下文」，消息模板里拼进这些参数，用户看到的报错因此天然带有诊断信息。

需要强调：源码文件本身**没有**按类别分节。「环境 / 配置 / 消息 / 模型未就绪 / 功能不支持」这五分类是我们为学习而施加的视角（本讲综合实践会用到），个别类（如 `ContextWindowSizeExceededError`、`IntegrityError`）横跨两类，归类时以「触发时刻的用户意图」为准即可。

#### 4.1.2 核心流程

一个错误从发生到被用户看到，路径是：

```text
构造参数（上下文）
   ↓  new XXXError(params)
模板化英文消息 + this.name = "XXXError"
   ↓  throw
沿调用栈穿透（协议层 → 引擎层 → 管线层谁检查谁抛）
   ↓
到达应用层 try/catch，或经 Worker 边界退化为字符串
   ↓
应用层按 instanceof / error.name / 消息匹配 做降级处理
```

#### 4.1.3 源码精读

先看「标准写法」——`ModelNotFoundError`，构造参数 `modelId` 直接进消息：

[src/error.ts:1-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L1-L8)：定义 `ModelNotFoundError`，消息里带上出错的 `modelId` 并提示检查 `model_list` 配置——这是全部 68 个类的样板写法。

再看**陷阱一：`name` 与类名不一致**。仓库里存在两处抄写不一致：

- [src/error.ts:230-237](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L230-L237)：类名叫 `MissingModelWasmError`，但第 235 行写的是 `this.name = "MissingModelError"`——少了个 `Wasm`。
- [src/error.ts:212-219](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L212-L219)：类名叫 `ToolCallOutputMissingFieldsError`，但第 217 行写的是 `this.name = "JSONFieldError"`——完全不同的名字。

这意味着**按 `error.name` 做字符串匹配时要小心这两个类**；反过来，`instanceof` 永远不受影响。这是「优先 instanceof、name 匹配作兜底」的最好论据。

**陷阱二：遮蔽内置 `RangeError`**：

[src/error.ts:24-36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L24-L36)：WebLLM 定义了自己的 `RangeError extends ConfigValueError`，参数为 `(paramName, minValue, maxValue)`。它**不是** JavaScript 内置的 `RangeError`（内置的构造签名是 `(message)`）。如果你写了 `import { RangeError } from "@mlc-ai/web-llm"`，就会在当前模块里遮蔽全局 `RangeError`；且 `webllm 的 RangeError instanceof 全局 RangeError` 为 `false`，排查类型问题时别被名字骗了。

#### 4.1.4 代码实践

**实践目标**：不依赖运行，仅靠静态阅读建立「类 ↔ name」对照表，并发现所有不一致。

**操作步骤**：

1. 打开 `src/error.ts`，用编辑器的列选择或正则 `\bclass (\w+) extends` 提取全部 68 个类名。
2. 再用正则 `this\.name = "(\w+)"` 提取全部 name 赋值。
3. 两个列表按出现顺序对齐，找出类名与 name 不一致的行。

**需要观察的现象**：两个列表长度应当都是 68；对齐后恰有两处不匹配（第 235 行、第 217 行附近）。

**预期结果**：得到一张「类名 → name 字符串」映射表。它是你后面编写统一错误处理器（综合实践）时字符串匹配分支的依据。本实践为纯源码阅读，无需本地验证运行结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WebLLM 要给每个错误类手动设置 `this.name`，而不是依赖类名自动生成？

**答案**：JavaScript 的 `Error` 不会自动从子类推导 `name`；不设置的话 `err.name` 永远是 `"Error"`，`console.error` 与日志里无法区分错误种类。手动设置 `name` 后，即使错误被序列化或跨线程退化为字符串（`String(err)` 输出 `"NameError: message"` 格式），依然保留了可识别的种类信息。

**练习 2**：如果只允许你用一种方式识别错误，选 `instanceof` 还是 `error.name` 字符串匹配？为什么？

**答案**：在单线程、单一库实例的场景选 `instanceof`（不受第 4.1.3 节两处 name 不一致的影响，且有类型收窄的好处）；但一旦涉及 Web Worker（错误退化为字符串，见 4.5 节）或页面可能加载两份 WebLLM，`instanceof` 失效，只能退回 `error.name` / 消息匹配。生产代码通常两者结合：先 `instanceof`，再 `name` 兜底。

### 4.2 ConfigValueError 家族：配置与数值校验错误

#### 4.2.1 概念说明

采样参数（`temperature`、`top_p`、各类 penalty、`max_tokens`）和模型配置（`prefill_chunk_size`、窗口参数）都是「数值型配置」，它们的非法形态可以归纳为五种：低于下界、超出区间、为负、本该是数字字符串却不是、依赖其他参数却缺失。`ConfigValueError` 家族就是把这五种形态模板化：

| 类 | 语义 | 消息模板 |
| --- | --- | --- |
| `MinValueError` | 参数必须大于某下界 | `Make sure \`x\` > min.` |
| `RangeError` | 参数必须在开闭区间内 | `Make sure min < \`x\` <= max.` |
| `NonNegativeError` | 参数非负 | `Make sure x >= 0.` |
| `InvalidNumberStringError` | 字符串形式的数字非法 | `Make sure x to be number represented in string.` |
| `DependencyError` | 参数 A 依赖参数 B 取特定值 | `A requires B to be value.` |

#### 4.2.2 核心流程

以一次 `chatCompletion({ temperature: -1, ... })` 为例：

```text
请求进入 engine.chatCompletion
  ↓ 摘出采样参数为 GenerationConfig
生成前调用 postInitAndCheckGenerationConfigValues（引擎层，config.ts）
  ↓ 逐项检查，temperature < 0 → throw NonNegativeError("temperature")
（若通过）管线生成路径上 llm_chat.ts 再复核一遍（覆盖低层 API 直入管线的场景）
  ↓
错误穿透 await 链，被应用层 catch
```

这呼应 u3-l5 讲过的「双层校验」：引擎层校验服务公开 API，管线层复核服务 `forwardTokensAndSample` 等低层入口。

#### 4.2.3 源码精读

家族基类与五个子类：

- [src/error.ts:10-15](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L10-L15)：`ConfigValueError` 基类，只接收现成的 message。
- [src/error.ts:17-65](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L17-L65)：`MinValueError`、`RangeError`、`NonNegativeError`、`InvalidNumberStringError`、`DependencyError` 五个子类，各自把参数名与边界值拼进消息。

引擎层校验的主战场在 `config.ts`：

[src/config.ts:167-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L197)：`postInitAndCheckGenerationConfigValues` 依次检查 `frequency_penalty`（±2 区间）、`presence_penalty`（±2 区间）、`repetition_penalty`（> 0）、`max_tokens`（> 0）、`top_p`（0 < x <= 1）、`temperature`（>= 0），每一项越界就抛对应的家族成员。注意第 170-173 行的 `_hasValue` 辅助函数专门区分了 `0` 与 `undefined/null`——直接写 `if (config.x)` 会把合法的 `0` 当成未设置。

管线层的复核在 `llm_chat.ts`：

[src/llm_chat.ts:1679-1697](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1679-L1697)：管线在生成路径上对 `top_p`、`temperature`、`repetition_penalty`、两个 penalty 做同样的检查——与引擎层几乎逐行镜像，代价是重复代码，收益是不管从哪个入口进来都有守卫。

此外，`reload()` 自己的参数也受这个家族保护：

[src/engine.ts:217-226](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L217-L226)：`modelId` 数组与 `chatOpts` 数组长度不一致抛 `ReloadArgumentSizeUnmatchedError`；`modelId` 有重复抛 `ReloadModelIdNotUniqueError`（注意后者不在 ConfigValueError 家族里，是独立的类）。

#### 4.2.4 代码实践

**实践目标**：验证同一参数在引擎层与管线层被检查两次，并观察双层校验的报错顺序。

**操作步骤**：

1. 基于 `examples/get-started`（入口 [examples/get-started/src/get_started.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts)），在 `main()` 里把生成请求改为 `engine.chatCompletion({ messages, temperature: -1 })`，外层套 `try/catch` 打印 `err.name` 与 `err.message`。
2. 把 `temperature` 改回合法值，改为 `top_p: 1.5`，重复一次。
3. 阅读源码回答：这两个错误分别最早在哪一层被抛出？

**需要观察的现象**：两种非法参数都在请求发出后立即同步 reject（无需等模型生成）；`err.name` 分别为 `NonNegativeError` 与 `RangeError`。

**预期结果**：均在引擎层 `postInitAndCheckGenerationConfigValues`（`config.ts`）即被拦截，不会到达管线层——管线层复核只在绕过引擎的低层调用路径上生效。运行现象**待本地验证**（本讲义写作环境无浏览器与 WebGPU）。

#### 4.2.5 小练习与答案

**练习 1**：`RangeError` 的消息模板是 `Make sure min < x <= max`，为什么 `top_p` 检查写成 `<= 0 || > 1` 而消息却是 `0 < top_p <= 1`？

**答案**：二者一致——`config.ts:192-193` 的条件是 `top_p <= 0 || top_p > 1` 时抛错，等价于要求 `0 < top_p <= 1`，与 `RangeError("top_p", 0, 1)` 的模板「开区间下界、闭区间上界」吻合。上界取闭是因为 `top_p = 1`（不截断）是合法取值。

**练习 2**：为什么 `postInitAndCheckGenerationConfigValues` 要自己写 `_hasValue` 而不直接 `if (config.frequency_penalty)`？

**答案**：因为 `0` 是 falsy。`frequency_penalty = 0` 是合法的显式设置，直接真值判断会跳过本应执行的区间检查（虽然 0 恰在 ±2 内），更关键的是第 198 行之后的「只设置一个 penalty 时给另一个补 0」的逻辑需要准确区分「未设置」与「设置为 0」。

**练习 3**：`DependencyError` 在什么场景下有意义？举一个 OpenAI 协议里的例子。

**答案**：当参数 A 只有在参数 B 取特定值时才合法时使用。OpenAI 协议里的例子是 `stream_options` 只有 `stream: true` 时才可指定——不过 WebLLM 对这个具体场景用的是专门的 `InvalidStreamOptionsError`（[src/error.ts:463-468](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L463-L468)），`DependencyError` 是更通用的模板。

### 4.3 WebGPU 环境错误族：从设备检测到设备丢失

#### 4.3.1 概念说明

「环境错误」指代码没有任何写错、但当前浏览器/设备不满足运行条件的错误。它们全部集中在 `reload()` 链路与 GPU 信息查询方法中，共 5 个主力类：

| 类 | 触发条件 |
| --- | --- |
| `WebGPUNotAvailableError` | `tvmjs.detectGPUDevice()` 返回 `undefined`（无 WebGPU） |
| `WebGPUNotFoundError` | 仅 Chrome 扩展 Service Worker 的重载去重路径上检测不到 GPU |
| `FeatureSupportError` | 设备缺少 `required_features` 声明的某特性 |
| `ShaderF16SupportError` | 上一条的特化：缺少 `shader-f16`，消息附带 Chrome Canary 启动flag 建议 |
| `DeviceLostError` | 加载过程中 WebGPU 设备丢失（绝大多数是显存 OOM） |

设计要点是 **fail-fast**：环境问题在 `reload()` 期间一次性暴露，而不是留到生成中途以更晦涩的方式崩溃。

#### 4.3.2 核心流程

路径一（WebGPU 不可用）：

```text
reload(modelId)
  → reloadInternal()
    → 下载并实例化 wasm（此步已通过）
    → gpuDetectOutput = await tvmjs.detectGPUDevice()
    → 若 undefined：throw WebGPUNotAvailableError   ← reload 立即失败
```

路径二（shader-f16 缺失）：

```text
检测到设备后，遍历 modelRecord.required_features
  对每个 feature 检查 gpuDetectOutput.device.features.has(feature)
    缺失且 feature == "shader-f16" → throw ShaderF16SupportError
    缺失且是其他特性           → throw FeatureSupportError(feature)
```

路径三（设备丢失，异步转同步）：

```text
device.lost.then(info => { 记录日志; this.unload(); deviceLostInReload = true })
... reload 其余步骤继续执行 ...
reloadInternal 末尾：
  if (deviceLostInReload) throw DeviceLostError()
```

设备丢失本质是异步事件，可能在权重分配的任意时刻触发；WebLLM 用一个布尔标志把它「攒」到 `reload()` 末尾统一抛出，保证用户拿到的是一个同步、可 catch 的异常，而不是散落在回调里的日志。

#### 4.3.3 源码精读

路径一与路径二共用一段代码：

[src/engine.ts:347-367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L347-L367)：先 `detectGPUDevice()`，返回 `undefined` 即抛 `WebGPUNotAvailableError`（第 349-351 行）；随后遍历 `modelRecord.required_features`，逐个对照 `device.features`——`shader-f16` 缺失走特化的 `ShaderF16SupportError`，其他特性缺失走通用的 `FeatureSupportError`（第 358-367 行）。注意这段代码同时用 `adapterInfo.description` / `vendor` 拼出 `gpuLabel`，供加载完成回调展示。

路径三的挂接与触发：

- [src/engine.ts:374-385](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L374-L385)：注册 `device.lost.then(...)`——先 `log.error` 提示可能 OOM，再调用 `this.unload()` 清理四个状态 Map，并把 `deviceLostInReload` 置真。`deviceLostIsError` 标志（[src/engine.ts:147](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L147)）用于区分「真丢失」与「`unload()` 主动销毁设备」两种情况，主动销毁不触发该处理（见 [src/engine.ts:432-433](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L432-L433)）。
- [src/engine.ts:427-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L427-L429)：`reloadInternal` 末尾检查标志，抛出 `DeviceLostError`——错误消息明确建议换更小模型或更短上下文重试。

`DeviceLostError` 本体在 [src/error.ts:267-274](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L267-L274)，`ShaderF16SupportError` 在 [src/error.ts:258-266](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L258-L266)（消息里建议用 `--enable-dawn-features=allow_unsafe_apis` 启动 Chrome Canary）。

两个不依赖已加载模型的查询方法也会抛环境错误——这使它们天然适合做「预检」：

- [src/engine.ts:1156-1183](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1156-L1183)：`getMaxStorageBufferBindingSize()` 重新 `detectGPUDevice()`，不可用抛 `WebGPUNotAvailableError`；可用时返回设备上限，低于 1GB 还会打警告列出仍可运行的模型。
- [src/engine.ts:1185-1192](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1185-L1192)：`getGPUVendor()` 同样先检测再返回厂商名。

最后是容易混淆的一对：`WebGPUNotFoundError` 不在主线程引擎里抛，唯一抛出点在扩展 Service Worker：

[src/extension_service_worker.ts:63-76](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/extension_service_worker.ts#L63-L76)：Chrome MV3 扩展的 reload 去重逻辑里，如果模型已加载过就跳过重新加载，但仍要 `detectGPUDevice()` 来拼加载完成消息——此时检测不到 GPU 就抛 `WebGPUNotFoundError`。类定义见 [src/error.ts:79-84](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L79-L84)。

#### 4.3.4 代码实践

**实践目标**：在不下载任何模型的前提下探测当前环境能否运行 WebLLM。

**操作步骤**：

1. 新建页面（或改造 get-started），写入下面的示例代码：

```ts
// 示例代码：环境预检，不触发模型下载
import * as webllm from "@mlc-ai/web-llm";

async function preflight(): Promise<string> {
  if (navigator.gpu === undefined) {
    return "浏览器未暴露 navigator.gpu，基本不支持 WebGPU";
  }
  const engine = new webllm.MLCEngine();   // 构造函数很轻量，不加载模型
  try {
    const maxSize = await engine.getMaxStorageBufferBindingSize();
    const vendor = await engine.getGPUVendor();
    return `GPU 可用：vendor=${vendor}, maxStorageBufferBindingSize=${maxSize}`;
  } catch (err) {
    return `环境预检失败：${(err as Error).name}: ${(err as Error).message}`;
  }
}
```

2. 在普通 Chrome、以及 `chrome://flags` 里禁用 WebGPU 后的同一浏览器中各运行一次。

**需要观察的现象**：禁用 WebGPU 后，`navigator.gpu === undefined` 分支命中（或第二步抛 `WebGPUNotAvailableError`）；启用时返回厂商名与 buffer 上限字节数（典型桌面 GPU 为 1GB 或更高，移动端可能更低并触发源码里的警告日志）。

**预期结果**：预检函数在两种环境下都给出明确结论，且全程不下载模型文件。运行现象**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ShaderF16SupportError` 要单独子类化，而不是统一用 `FeatureSupportError("shader-f16")`？

**答案**：`shader-f16` 缺失是最常见的环境问题（所有 q4f16 模型都要求它），且存在已知的用户侧解法（换浏览器或带 flag 启动 Chrome Canary）。特化子类让消息携带这条可操作建议（[src/error.ts:258-266](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L258-L266)），应用层也可以精确 catch 它给出「如何开启」的指引，而其他特性缺失只能提示「不支持」。

**练习 2**：`deviceLostInReload` 标志为什么能在 `reloadInternal` 末尾被「同步」读取到？这不是竞态吗？

**答案**：`device.lost.then(...)` 的回调在微任务队列中执行；而 `reloadInternal` 从注册回调到末尾检查之间有大量 `await`（下载权重、fetchTensorCache、构造管线、`asyncLoadWebGPUPipelines`），每个 `await` 都会让出主线程、清空微任务队列，所以 OOM 导致的 `lost` 回调几乎必然先于末尾检查执行。这是「借异步步骤的让出点，把异步事件收敛为同步检查」的惯用法；严格说仍存在极端时序错过（源码注释里的 TODO 也承认这点），此时表现为设备丢失日志已打但 reload 仍返回成功。

**练习 3**：`getMaxStorageBufferBindingSize()` 为什么不直接复用 `reload()` 时缓存的设备，而要重新 `detectGPUDevice()`？

**答案**：因为该方法设计为可在**未加载模型**时调用（预检场景），此时引擎内根本没有保存任何设备；重新检测是最简单且无副作用的做法。代价是每次调用都新建一个临时 GPUDevice（未 initWebGPU、不分配资源，开销可接受）。

### 4.4 多模态与工具调用错误族

#### 4.4.1 概念说明

这一族错误围绕两类「结构化输入/输出」：

- **多模态输入侧**：用户消息的 `content` 数组（text part + image_url part）在进入管线前要过协议层的关卡——URL 前缀是否合法、是否带了不支持的 `detail` 字段、一条消息是否只有一个 text part、非 VLM 模型是否收到了数组 content。运行期还有两条管线层守卫：模型没有 `image_embed` 内核、单张图片的 token 数超过 `prefillChunkSize`。
- **工具调用输出侧**：模型生成的函数调用是一段 JSON 文本，需要解析成 `tool_calls` 结构；解析失败、类型不对、字段缺失各有专属错误。这些消息都以 "Internal error" 开头——因为按约定模型本应输出合法 JSON，失败通常意味着模型能力或模板配置问题，而非用户输入问题。

#### 4.4.2 核心流程

多模态校验（协议层 `postInitAndCheckFields` 扫描每条消息）：

```text
对 request.messages 逐条扫描：
  content 是数组？
    → 模型非 VLM：throw UserMessageContentErrorForNonVLM
    → 每个 part：image_url.url 前缀非法 → UnsupportedImageURLError
                 带 detail 字段        → UnsupportedDetailError
                 text part 计数 > 1    → MultipleTextContentError
  role === "system" 且不是第 0 条 → SystemMessageOrderError
末条消息 role 不是 user/tool → MessageOrderError
```

工具调用解析（引擎拿到生成文本后）：

```text
getToolCallFromOutputMessage(outputMessage)
  1. JSON.parse 失败      → ToolCallOutputParseError（内嵌原始 err）
  2. 解析结果不是数组     → ToolCallOutputInvalidTypeError("array")
  3. 元素缺 name/arguments → ToolCallOutputMissingFieldsError
```

#### 4.4.3 源码精读

协议层的消息扫描关卡：

[src/openai_api_protocols/chat_completion.ts:465-500](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L465-L500)：同一条消息里 text part 超过一个抛 `MultipleTextContentError`（第 466-471 行）；`system` 角色不在首位抛 `SystemMessageOrderError`（第 473-475 行）；末条消息不是 `user`/`tool` 抛 `MessageOrderError`（第 479-488 行）。多模态的三个错误（`UnsupportedImageURLError` 第 460 行、`UnsupportedDetailError` 第 455 行、`UserMessageContentErrorForNonVLM` 第 442 行）就在紧邻的上方同一循环里。这些类的定义集中在 [src/error.ts:134-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L134-L191)。

会话层还有一组镜像检查（服务低层入口）：[src/conversation.ts:492-515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L492-L515) 在把 OpenAI 消息翻译进 `Conversation` 时抛 `MessageOrderError` / `SystemMessageOrderError` / `ContentTypeError` / `UnsupportedRoleError`。

工具调用输出解析：

[src/support.ts:145-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L145-L156)：`getToolCallFromOutputMessage` 第一步 `JSON.parse` 包在 try/catch 里，失败即抛 `ToolCallOutputParseError(outputMessage, err)`——注意它把原始解析错误和模型完整输出都塞进了消息，这是排查「模型到底说了什么」的关键信息；第二步断言结果是数组，否则抛 `ToolCallOutputInvalidTypeError`。三个类的定义在 [src/error.ts:193-219](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L193-L219)。

管线层的两条多模态运行期守卫：

- [src/llm_chat.ts:856](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L856)（另见 [src/llm_chat.ts:2060](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2060)）：收到了图片输入但模型的 wasm 没有导出 `image_embed` 内核，抛 `CannotFindImageEmbedError`——即把图片发给了非视觉模型。
- [src/support.ts:300](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L300)：分块预填充时发现单张图片的 embedding 尺寸超过 `prefillChunkSize`，抛 `PrefillChunkSizeSmallerThanImageError`——呼应 u3-l6 的「token 可切、图片原子不可切」约束。

`tool_choice` 的校验错误（`InvalidToolChoiceError` 等 4 个）在 [src/conversation.ts:539-563](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L539-L563) 抛出，即 u6-l2 讲过的 `getFunctionCallUsage`。

#### 4.4.4 代码实践

**实践目标**：用构造非法请求的方式，逐个点亮协议层的消息类错误，并记录其 `name`。

**操作步骤**：

1. 在可运行 WebLLM 的页面里（模型已加载），分别发起以下四个请求，每个都套 `try/catch` 打印 `err.name`：

```ts
// 示例代码：四个应被协议层拦截的非法请求
const bad1 = { messages: [                        // system 不在首位
  { role: "user", content: "hi" },
  { role: "system", content: "be nice" }] };
const bad2 = { messages: [                        // 末条是 assistant
  { role: "user", content: "hi" },
  { role: "assistant", content: "hello" }] };
const bad3 = { messages: [                        // 非法图片 URL 前缀
  { role: "user", content: [
    { type: "text", text: "看图" },
    { type: "image_url", image_url: { url: "ftp://x/y.png" } }] }] };
const bad4 = { messages: [{ role: "user", content: "hi" }], stream: true, n: 2 };
```

2. 对照 [src/openai_api_protocols/chat_completion.ts:432-500](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L432-L500)，把每个错误的抛出行号写进笔记。

**需要观察的现象**：四个请求都在本地同步抛错，没有网络请求、没有 prefill 发生（可在 DevTools Network/Console 验证）；`err.name` 分别为 `SystemMessageOrderError`、`MessageOrderError`、`UnsupportedImageURLError`、`StreamingCountError`。

**预期结果**：错误与抛出位置一一对应，证明协议层关卡先于任何推理执行。运行现象**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ToolCallOutputParseError` 的消息为什么要包含完整的 `outputMessage` 和底层 `err`？

**答案**：函数调用输出解析失败通常不是用户代码问题，而是模型没按 schema 吐 JSON（能力不足、模板不对、被截断）。把原始输出带出来，开发者才能判断是「JSON 语法错」还是「模型输出了自由文本」，进而选择换模型、调 prompt 还是改用 manual 模板（u6-l2 讲过的两种函数调用方式）。

**练习 2**：`UserMessageContentErrorForNonVLM` 与 `CannotFindImageEmbedError` 都拦截「把图片发给非视觉模型」，为什么需要两个类在不同层各拦一次？

**答案**：前者在协议层，依据是 `ModelRecord.model_type`（声明式元数据），拦截「content 是数组但模型非 VLM」的请求，用户可自我纠正；后者在管线层，依据是 wasm 是否真的导出 `image_embed` 内核（事实性能力），防止元数据写错（比如把 VLM 标成 LLM）时图片漏进文本管线。一个查「户口」，一个查「体检」，双保险。

**练习 3**：`MultipleTextContentError` 为什么限制一条消息只能有一个 text part？

**答案**：源码注释（[src/openai_api_protocols/chat_completion.ts:468-469](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L468-L469)）标注了 TODO 询问这是否仅是 phi3vision 的约束。当前实现把 content 数组拍平进 `Conversation` 的 prompt 数组（u3-l2），多个 text part 与图片的相对顺序在模板层面没有定义，故直接收紧为最多一个。

### 4.5 模型未就绪错误与应用层降级策略

#### 4.5.1 概念说明

「模型未就绪」一族（`ModelNotLoadedError`、`SpecifiedModelNotFoundError`、`UnclearModelToUseError`、`IncorrectPipelineLoadedError`、`ConfigurationNotInitializedError`、`EngineNotLoadedError`、`WorkerEngineModelNotLoadedError`）的特殊之处在于：**它们不是 bug，而是引擎状态机的一部分**。在模型加载完成前调用生成接口、在多模型并存时不指明用哪个，都属于「用户时序问题」，正确响应是给出可操作的提示（「请先调用 reload」），而不是崩溃。

「降级处理」指应用层（甚至库自身）对某些预期错误选择**吞掉并转化为 no-op 或默认行为**。WebLLM 源码里有两个现成范例：`resetChat` 的吞错、`reload` 对 `AbortError` 的静默返回。

#### 4.5.2 核心流程

模型选择的守卫逻辑（每个生成/查询接口的必经之路）：

```text
getModelIdToUse(loadedModelIds, requestModel, requestName)
  loadedModelIds 为空        → ModelNotLoadedError
  指定了 model 且未加载      → SpecifiedModelNotFoundError（附已加载清单）
  未指定且加载了多个          → UnclearModelToUseError（附已加载清单）
  其余                       → 唯一确定的 selectedModelId
随后 getModelStates：
  管线类型与请求不符（LLM ↔ Embedding）→ IncorrectPipelineLoadedError
  chatConfig 或 lock 未注册            → 普通 Error（InternalError 前缀）
```

降级范例一（`resetChat` 吞错为 no-op）：

```text
try { 正常执行 resetChat }
catch (err):
  err 是 ModelNotLoadedError 或 SpecifiedModelNotFoundError
    → log.debug 后静默返回（空引擎上 resetChat 本来就无事可做）
  否则 → 原样 rethrow
```

降级范例二（Worker 边界的错误退化）：

```text
worker 内: handleTask(task) 
  try { postMessage({kind:"return", content}) }
  catch (err) { postMessage({kind:"throw", content: err.toString()}) }
主线程: 收到 throw → new Error(字符串) → 抛给调用方
       → 此时 instanceof 完全失效，只剩 name/消息可用
```

#### 4.5.3 源码精读

模型选择守卫：

[src/support.ts:227-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L256)：`getModelIdToUse` 的三分支——空列表抛 `ModelNotLoadedError`（第 233-235 行）、指定了未加载的模型抛 `SpecifiedModelNotFoundError`（第 238-243 行）、多模型未指定抛 `UnclearModelToUseError`（第 249-250 行）。后两者的消息都附带当前已加载模型清单，直接告诉用户可以填什么。

管线类型断言：

[src/engine.ts:1244-1268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1244-L1268)：`getModelStates` 用 `instanceof` 确认选出的管线是 `LLMChatPipeline` 还是 `EmbeddingPipeline`，不符抛 `IncorrectPipelineLoadedError`（如在嵌入模型上调 `chatCompletion`）；嵌入路径还额外复查 `ModelRecord.model_type`，不符抛 `EmbeddingUnsupportedModelError`。注意第 1275、1282 行两处「InternalError 前缀」的普通 `Error`——这是「不该发生」的内部不变量断言，与面向用户的错误类刻意区分。

吞错降级范例：

[src/engine.ts:1326-1344](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1326-L1344)：`resetChat` 捕获 `ModelNotLoadedError` 与 `SpecifiedModelNotFoundError` 后仅 `log.debug` 并返回——注释写明「允许在管线实例化之前调用 resetChat」。这是**可恢复错误的库内降级样板**：调用方不必先判断引擎状态，直接调用即可。

`reload` 对用户主动中断的降级：

[src/engine.ts:237-242](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L237-L242)：捕获 `DOMException` 且 `name === "AbortError"` 时只 `log.warn` 后正常返回——中止是用户意图，不是故障。

Worker 边界：

[src/web_worker.ts:111-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L111-L132)：`handleTask` 的 catch 分支把错误 `toString()` 后装进 `kind: "throw"` 的消息回传（第 123-130 行）。u5-l1 已讲过该协议；本讲的视角是它对错误处理的影响——**主线程拿到的只是一个普通 `Error`，`name` 字段已经丢失**（`err.toString()` 产出的字符串形如 `"Error: Cannot find WebGPU..."`，被 `new Error(str)` 包装后 name 是 `"Error"`）。因此 Worker 模式下的错误识别只能靠消息子串匹配（好消息：4.1 节确认每个类的消息模板足够独特）。

#### 4.5.4 代码实践

**实践目标**：设计一个「阶梯式」统一错误处理器，覆盖主线程与 Worker 两种模式。

**操作步骤**：

1. 阅读上面的源码链接，确认三个事实：`resetChat` 吞掉哪些错误、`handleTask` 如何序列化错误、`getModelIdToUse` 三个分支的顺序。
2. 写出如下的处理器骨架（示例代码）：

```ts
// 示例代码：阶梯式错误处理器
export function friendlyError(err: unknown): { level: string; tip: string } {
  const e = err as Error;
  const text = String(err);                    // Worker 退化后只剩这个可靠
  // 1. 环境类：换浏览器/换模型才能解决
  if (e?.name === "WebGPUNotAvailableError" || e?.name === "WebGPUNotFoundError"
      || text.includes("WebGPU is not supported") || text.includes("Cannot find WebGPU")) {
    return { level: "fatal", tip: "请更换支持 WebGPU 的浏览器" };
  }
  if (e?.name === "ShaderF16SupportError" || text.includes("shader-f16")) {
    return { level: "fatal", tip: "当前 GPU 不支持 shader-f16，请换 q4f32 模型或换设备" };
  }
  if (e?.name === "DeviceLostError" || text.includes("device was lost")) {
    return { level: "fatal", tip: "显存不足，请换更小模型或缩短上下文后重试" };
  }
  // 2. 状态类：用户可自我修复
  if (e?.name === "ModelNotLoadedError" || text.includes("Model not loaded")) {
    return { level: "recoverable", tip: "模型尚未加载完成，请等待 initProgressCallback 报告 1" };
  }
  // 3. 请求类：修参数后重发即可
  if (["MinValueError", "RangeError", "NonNegativeError",
       "SystemMessageOrderError", "MessageOrderError"].includes(e?.name ?? "")) {
    return { level: "retryable", tip: `请求参数有误：${e?.message ?? text}` };
  }
  return { level: "unknown", tip: text };
}
```

3. 把 `CreateMLCEngine` 与 `chatCompletion` 的调用都包上 `try/catch`，走这个处理器，把 `tip` 显示到页面。

**需要观察的现象**：环境错误给出换浏览器/换模型指引；参数错误提示具体哪个字段越界；Worker 模式下（改用 `CreateWebWorkerMLCEngine`）同样的非法参数走的是 `text.includes` 分支而非 `name` 分支。

**预期结果**：三种 level 的错误各有明确的用户可读文案，且两种引擎模式下都能正确分类。运行现象**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `resetChat` 敢吞掉 `ModelNotLoadedError`，而 `chatCompletion` 不敢？

**答案**：「清空会话」在空引擎上的合理语义就是无事可做（幂等 no-op），吞错让调用方免于状态判断；而「生成回复」在没加载模型时没有合理默认行为，必须让调用方知道失败并先去 `reload`。降级与否取决于该操作是否存在语义良好的空状态。

**练习 2**：Worker 模式下如何尽量保住错误的可识别性？

**答案**：库侧的 `handleTask` 目前只回传 `err.toString()`，无法改动（不改源码的前提下）。应用侧的对策：(a) 用消息子串匹配（如上例）；(b) 对关键环境错误做**前置预检**（4.3.4 节的 `navigator.gpu` 检查在创建引擎前就拦下，根本不进 Worker）；(c) 需要精确类型时自定义 `WebWorkerMLCEngineHandler` 子类重写 `onmessage`（u5-l2 讲过的扩展点），在 Worker 内部先 catch 转换成带 `name` 的信封再回传。

**练习 3**：`getModelStates` 里两处抛普通 `Error("InternalError: ...")` 而不是定义新错误类，这个区分说明了什么设计原则？

**答案**：错误类是给**调用方的契约**——调用方应当能通过 catch 特定类型做出反应；而「chatConfig 未注册」意味着引擎内部 Map 的一致性被破坏，正常使用不可能触发，调用方也无法处理，所以只留普通 `Error` 加 InternalError 前缀供开发者排查。可以据此反推：**凡是被导出为类的错误，都是库作者认为用户需要区分对待的**。

## 5. 综合实践

把本讲全部内容串起来：为 68 个错误类建立五分类档案，并落地一个统一错误处理页面。

**任务 A：全量分类表**

按「环境不支持 / 配置非法 / 消息不合法 / 模型未就绪 / 功能不支持」五类整理 `src/error.ts` 的全部 68 个类，列为 `类名 | 触发条件 | 抛出位置（文件:行号）`。下面是参考骨架（25 行，覆盖每类的代表），请补全其余 43 个类——抛出位置用 Grep `throw new <类名>` 查证，不要凭记忆填：

| 分类 | 类名 | 触发条件 | 抛出位置 |
| --- | --- | --- | --- |
| 环境不支持 | `WebGPUNotAvailableError` | `detectGPUDevice()` 返回 undefined | src/engine.ts:350 |
| 环境不支持 | `WebGPUNotFoundError` | 扩展 SW 重载去重时检测不到 GPU | src/extension_service_worker.ts:75 |
| 环境不支持 | `FeatureSupportError` | 设备缺少 required_features 中某特性 | src/engine.ts:364 |
| 环境不支持 | `ShaderF16SupportError` | 缺少 shader-f16 特性 | src/engine.ts:362 |
| 环境不支持 | `DeviceLostError` | reload 中 WebGPU 设备丢失（多为 OOM） | src/engine.ts:428 |
| 配置非法 | `ModelNotFoundError` | modelId 不在 model_list | src/support.ts:216、src/cache_util.ts:66 |
| 配置非法 | `MinValueError` / `RangeError` / `NonNegativeError` | 采样参数越界 | src/config.ts:178-196、src/llm_chat.ts:1679-1697 |
| 配置非法 | `MissingModelWasmError` | ModelRecord 缺 model_lib 字段 | src/engine.ts:307 |
| 配置非法 | `WindowSizeConfigurationError` | 两个窗口参数同时为正 | src/llm_chat.ts:364 |
| 配置非法 | `ReloadArgumentSizeUnmatchedError` | reload 的 modelId 与 chatOpts 数量不匹配 | src/engine.ts:218 |
| 配置非法 | `SeedTypeError` | seed 非整数 | src/openai_api_protocols/chat_completion.ts:498 |
| 消息不合法 | `SystemMessageOrderError` | system 消息不在首位 | src/openai_api_protocols/chat_completion.ts:474 |
| 消息不合法 | `MessageOrderError` | 末条消息非 user/tool | src/openai_api_protocols/chat_completion.ts:485 |
| 消息不合法 | `UnsupportedImageURLError` | image_url.url 前缀非法 | src/openai_api_protocols/chat_completion.ts:460 |
| 消息不合法 | `MultipleTextContentError` | 单消息 text part 超过一个 | src/openai_api_protocols/chat_completion.ts:470 |
| 消息不合法 | `UnsupportedRoleError` | 消息 role 未知 | src/conversation.ts:515 |
| 模型未就绪 | `ModelNotLoadedError` | 引擎未加载任何模型 | src/support.ts:234 |
| 模型未就绪 | `SpecifiedModelNotFoundError` | 指定的 model 未加载 | src/support.ts:239 |
| 模型未就绪 | `UnclearModelToUseError` | 多模型并存且未指定 | src/support.ts:250 |
| 模型未就绪 | `IncorrectPipelineLoadedError` | 管线类型与请求不符 | src/engine.ts:1248、src/engine.ts:1257 |
| 模型未就绪 | `ConfigurationNotInitializedError` | chatConfig 未注册就 prefill | src/engine.ts:1374 |
| 功能不支持 | `UnsupportedFieldsError` | 请求含未支持字段 | src/openai_api_protocols/completion.ts:360 |
| 功能不支持 | `StreamingCountError` | stream 时 n > 1 | src/openai_api_protocols/chat_completion.ts:492 |
| 功能不支持 | `ToolCallOutputParseError` | 工具调用输出 JSON 解析失败 | src/support.ts:150 |
| 功能不支持 | `PrefillChunkSizeSmallerThanImageError` | 单图 token 超过 prefillChunkSize | src/support.ts:300 |

归类时注意横跨两类的边界情况（如 `ContextWindowSizeExceededError` 算「消息不合法」还是「配置非法」——它由输入长度触发、但根因可能是 `context_window_size` 配小了；`IntegrityError` 建议单独注明「安全校验失败」），在表格备注列写下你的理由。

**任务 B：统一错误处理页面**

基于 `examples/get-started` 改造：

1. 把 `CreateMLCEngine` 调用包进 `try/catch`，套用 4.5.4 节的 `friendlyError`（或你自己的实现）。
2. 明确要求：捕获 `WebGPUNotFoundError` **或** `WebGPUNotAvailableError`（以及 Worker 退化后的消息匹配分支）时，页面显示「请更换支持 WebGPU 的浏览器」。读完 4.3.3 节你就知道为什么必须两个都 catch——主线程引擎抛的是 `Available`，`Found` 只出现在扩展 Service Worker 路径。
3. 在正常浏览器与禁用 WebGPU 的浏览器（`chrome://flags` 搜索 WebGPU 关闭后重启）各测一次，把两种输出截图存档。
4. 若你在前几讲搭过 Worker 版页面，再跑一遍验证消息子串匹配分支生效。

**验收标准**：表格覆盖 68 个类且每行行号经 Grep 核实；页面在无 WebGPU 环境显示指定提示文案。浏览器相关现象**待本地验证**。

## 6. 本讲小结

- `src/error.ts` 用 629 行定义 68 个错误类，组织近乎全平铺，仅 `ConfigValueError`（5 个数值校验子类）与 `FeatureSupportError`（`ShaderF16SupportError`）两个小家族；构造参数即错误上下文，消息模板自带诊断信息。
- 存在两处 `name` 与类名不一致（`MissingModelWasmError`→`"MissingModelError"`、`ToolCallOutputMissingFieldsError`→`"JSONFieldError"`），且 `RangeError` 遮蔽内置同名类——识别错误优先 `instanceof`，`name`/消息匹配仅作 Worker 等退化场景的兜底。
- WebGPU 环境错误遵循 fail-fast：`detectGPUDevice()` 失败抛 `WebGPUNotAvailableError`（`WebGPUNotFoundError` 仅在扩展 Service Worker 抛），`required_features` 缺失按是否为 `shader-f16` 分派特化/通用 FeatureSupport 错误；`DeviceLostError` 用布尔标志把异步 `device.lost` 回调收敛为 `reload()` 末尾的同步抛出。
- 多模态错误分协议层（元数据门禁，如 `UnsupportedImageURLError`）与管线层（能力门禁，如 `CannotFindImageEmbedError`、`PrefillChunkSizeSmallerThanImageError`）双层设防；工具调用错误（`ToolCallOutputParseError` 等）发生在输出解析阶段，消息携带模型原始输出便于排查。
- 模型未就绪族是状态机的一部分而非故障；库内的降级样板是 `resetChat` 吞掉 `ModelNotLoadedError` 当 no-op、`reload` 静默处理 `AbortError`；应用层降级的阶梯是：环境错误给换浏览器/换模型指引、状态错误提示等待加载、参数错误指出具体字段。
- Worker 边界（`handleTask`）会把错误 `toString()` 退化，`instanceof` 与 `name` 双双失效，跨线程的错误识别只剩消息子串匹配或前置预检（`navigator.gpu` / 未加载模型也可调用的 `getMaxStorageBufferBindingSize()`）。

## 7. 下一步学习建议

- **u7-l2（多模型管理与动态切换）**：本讲的 `UnclearModelToUseError` / `SpecifiedModelNotFoundError` / `IncorrectPipelineLoadedError` 正是多模型场景的守卫，下一讲看这些错误如何被「正确的 reload/unload 时序」从源头消除。
- **u7-l4（测试体系）**：`tests/` 下有大量「构造非法输入 → 断言抛出特定错误类」的用例，是本讲五分类表的现成交叉验证材料；也可以模仿其为 `friendlyError` 补单测。
- **延伸阅读**：对照 `src/integrity.ts`（u4-l3）理解 `IntegrityError` 为什么携带 `url/expected/actual` 三个字段而非拼接进消息——它是唯一有结构化属性的错误类，方便程序化处理。
