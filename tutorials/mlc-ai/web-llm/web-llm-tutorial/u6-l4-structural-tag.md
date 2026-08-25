# Structural Tag 工具调用

## 1. 本讲目标

本讲是第六单元「OpenAI 兼容协议深度」的收官篇，承接 u6-l3 的 xgrammar 约束解码机制。学完本讲，你应该能够：

1. 说清 `response_format.type = "structural_tag"` 的协议定义：什么是「触发式标签」（triggered tags），它如何把一段回复切成「自由文本区」和「受约束 JSON 区」。
2. 追踪 structural tag 在管线层的完整执行链：`compileStructuralTag` 编译 → `getNextTokenBitmask` 逐 token 掩码 → `apply_bitmask_inplace` GPU 屏蔽 → `acceptToken` 推进状态机，并理解它与 `LogitProcessor` 的协作顺序。
3. 从机制上解释 structural tag 相比「整段 JSON schema 约束」的性能优势来源，并用实验验证。
4. 对比三种工具调用路线（Hermes 自动函数调用 / 手动模板 / structural tag）的适用场景与代价。

## 2. 前置知识

阅读本讲前，你应当已学完 u6-l2（函数调用）与 u6-l3（JSON Mode 与 xgrammar）。这里复习三个关键认知，并补充一个新概念。

**复习 1：约束解码的原理。** 采样前，xgrammar 的 `GrammarMatcher` 会计算出一个位掩码（bitmask）：词表中每个 token 对应一位，语法允许的 token 位为 1、不允许的为 0。WebLLM 把掩码拷到 GPU，在采样前就把非法 token 的 logit 屏蔽掉。因此无论模型「想」输出什么，最终文本一定符合预定义语法。

**复习 2：整段约束的痛点。** u6-l3 讲的 `json_object` + `schema` 模式把**整条回复**都关进 JSON 的「紧身衣」：模型哪怕只想说一句「我查一下天气」，也必须把它塞进 JSON 字符串字段里。约束越复杂，每 token 的掩码计算越贵，而且模型被迫用非自然的方式组织全部输出。

**新概念：触发式标签（triggered tags）。** structural tag 的思路是「分段管理」：平时模型自由生成文本（约束极弱，只需监视是否出现触发字符串）；一旦生成到触发串（如 `<tool_call>`），状态机切入严格模式，标签内部必须符合你指定的 JSON schema；标签结束串（如 `</tool_call>`）生成完毕后，又回到自由模式。一句话概括：**只约束必须精确的部分，放开可以自由的部分**。

**为什么这能降低开销？** 记每个 token 的采样延迟为：

\[ T_{token} = T_{forward} + T_{bitmask} + T_{sample} \]

其中 \( T_{bitmask} \) 是语法状态机计算合法 token 集合的耗时。自由文本区的状态机近似于「任何 token 都合法，只需匹配触发串」，远比多层嵌套 JSON schema 的状态机简单；同时模型不再需要为绕开 JSON 语法而生成转义字符等冗余 token。两者叠加，就是 structural tag 的性能与质量优势来源。注意：优势大小取决于自由文本占比——如果回复几乎全是 JSON，两种方式的开销差距会缩小，这正是本讲综合实践要用实验验证的问题。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/openai_api_protocols/chat_completion.ts` | OpenAI 协议定义与请求校验 | `ResponseFormat.structural_tag` 字段、关卡 6.3 的配对校验 |
| `src/llm_chat.ts` | 推理管线 | `compileStructuralTag` 分派、位掩码应用、`acceptToken`、缓存键 |
| `src/engine.ts` | 引擎编排 | `usedGrammar` 统计口径（有一个值得注意的细节）、`tool_calls` 自动解析条件 |
| `src/error.ts` | 错误类 | `InvalidResponseFormatStructuralTagError` |
| `src/config.ts` | 模型配置 | `functionCallingModelIds` 白名单（用于对比） |
| `examples/structural-tag-tool-use/src/mcp_structural_tag.ts` | MCP 风格工具调用示例 | structural tag 定义、手工解析、多轮闭环 |
| `examples/structural-tag-tool-use/README.md` | 示例说明 | 运行方式 |

依赖提示：structural tag 的编译能力来自 npm 依赖 `@mlc-ai/web-xgrammar`（`package.json` 固定为 `0.1.27`），WebLLM 以 `import * as xgr from "@mlc-ai/web-xgrammar"` 引入（[src/llm_chat.ts:2](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2)）。协议层的 `StructuralTagLike` 类型同样来自该包（[src/openai_api_protocols/chat_completion.ts:46](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L46)），其权威结构以官方示例（下文 4.1.3）为准。

## 4. 核心概念与源码讲解

### 4.1 `structural_tag` 响应格式：协议层定义

#### 4.1.1 概念说明

`ResponseFormat.type` 共四种取值：`text`、`json_object`、`grammar`、`structural_tag`（u6-l3 已讲前三种）。第四种 `structural_tag` 是「触发式约束」：你交给引擎一份标签定义（哪些触发串、每个标签内部什么结构、至少出现几个、第一个结束后是否停止），引擎保证：

- 触发串之外的文本**不受语法约束**（自由文本区）；
- 触发串到结束串之间的内容**严格符合**该标签指定的 JSON schema（受约束区）。

这让「工具调用」这类任务有了一种自然形态：模型平时正常说话，需要调工具时输出 `<tool_call>{"name": ..., "arguments": ...}</tool_call>`，标签内的 JSON 由语法保证合法，标签外的文字完全自由。

#### 4.1.2 核心流程

一次携带 structural tag 的请求在协议层只经历两步：

```text
用户请求 { response_format: { type: "structural_tag", structural_tag: {...} } }
   │
   ├─ 关卡 A：structural_tag 字段非空 ⟹ type 必须是 "structural_tag"（否则抛错）
   └─ 关卡 B：type 是 "structural_tag" ⟹ structural_tag 字段必须非空（否则抛错）
```

两道关卡互为镜像，保证「字段与类型严格配对」。这与 u6-l3 的关卡 6（schema 只能配 `json_object`）、关卡 6.1/6.2（grammar 双向配对）是同一套设计模式，只是换了错误类。

另一个协议层要点：**structural tag 请求通常不传 `tools` 字段**。u6-l1 讲过，关卡 7 只在 `request.tools` 非空时触发，而关卡 7.1 会检查模型是否在 `functionCallingModelIds` 白名单（仅 5 个 Hermes 模型）。structural tag 路线把工具描述写进 system prompt、由调用方自己解析输出，因此完全绕开白名单——**任何模型都能用**。

#### 4.1.3 源码精读

**ResponseFormat 接口与 structural_tag 字段**：

[src/openai_api_protocols/chat_completion.ts:1198-1227](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1198-L1227)

这段定义了 `ResponseFormat`：`type` 四选一，`structural_tag` 字段类型为 `StructuralTagLike | string`——既可传对象也可传字符串。接口文档注释明确说明了语义：触发式约束（trigger-based constraints）、标签分块（tag-delimited blocks）、触发区间外允许自由文本（free-form text outside the triggered spans）。

**关卡 6.3：structural_tag 与 type 的双向配对校验**：

[src/openai_api_protocols/chat_completion.ts:532-548](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L532-L548)

第一段检查「给了 structural_tag 但 type 不是 structural_tag」；第二段镜像检查「type 是 structural_tag 但没给 structural_tag」。两处都抛 `InvalidResponseFormatStructuralTagError`。

**配套错误类**：

[src/error.ts:424-432](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L424-L432)

错误消息把两个方向的条件都写明了，方便调用方自查。

**官方示例中的标签定义（结构权威来源）**：

[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:47-60](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L47-L60)

这段是理解 `StructuralTagLike` 结构的最佳入口，逐字段解读：

- 外层 `type: "structural_tag"` 与 `format`：标签定义本体。
- `format.type: "triggered_tags"`：采用「触发式标签」变体。
- `triggers: ["<tool_call>"]`：触发串列表。自由文本生成到这个串即切入约束模式。
- `tags`：每个元素是一个标签规则，包含三段：
  - `begin`：标签的完整开头（含触发串 + 固定的 JSON 前缀 `<tool_call>\n{"name": "get_weather", "arguments": `）——注意函数名被硬编码进 `begin`，所以示例为**每个工具生成一条标签规则**，模型「选择工具」等价于「选择生成哪条 begin」；
  - `content: { type: "json_schema", json_schema: tool.schema }`：标签内部用 JSON schema 约束（即只约束 `arguments` 的取值）；
  - `end`：固定结尾 `}\n</tool_call>`，补齐 JSON 的右花括号并关闭标签。
- `at_least_one: true`：至少要出现一个标签才允许停止（防止模型偷懒不调工具）。
- `stop_after_first: false`：第一个标签结束后不强制停止，可以继续输出更多工具调用或说明文字。

**请求侧的用法**：

[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:159-170](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L159-L170)

`response_format` 直接挂上 `{ type: "structural_tag", structural_tag: mcpStructuralTag }`，注意请求里**没有** `tools` 字段。

#### 4.1.4 代码实践

**实践目标**：验证配对校验的两条错误路径，并亲手确认「绕开白名单」。

**操作步骤**：

1. 进入 `examples/structural-tag-tool-use/`，执行 `npm install` 然后 `npm start`（Parcel 在 8887 端口起服务，见 [examples/structural-tag-tool-use/package.json:6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/package.json#L6)），先跑通原示例。
2. 在 `mcp_structural_tag.ts` 中复制一份请求，故意写成 `{ type: "json_object", structural_tag: mcpStructuralTag }`（类型与字段不配对），观察控制台报错。
3. 再改成 `{ type: "structural_tag" }`（缺 `structural_tag` 字段），观察报错。
4. 对照 [src/config.ts:340-346](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L340-L346) 的白名单，确认示例所用模型 `Llama-3.2-1B-Instruct-q4f16_1-MLC`（[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:130](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L130)）不在其中。

**需要观察的现象**：步骤 2、3 均抛出 `InvalidResponseFormatStructuralTagError`（消息文本与 [src/error.ts:424-432](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L424-L432) 一致）；而正常 structural tag 请求不带 `tools`，加载的是非 Hermes 模型却照常工作。

**预期结果**：配对校验按镜像两方向生效；structural tag 路线不受 `functionCallingModelIds` 限制。若你的环境无 WebGPU 浏览器，步骤 2、3 的校验在请求发出前就会抛错，可在任意浏览器验证；完整生成则需支持 WebGPU 的浏览器（Chrome/Edge 113+），其余部分标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把示例的 `stop_after_first` 改为 `true`、`at_least_one` 改为 `false`，模型行为分别会怎么变？

**答案**：`stop_after_first: true` 时第一个标签的 `end` 生成完毕后语法即走完，只放行停止 token，生成提前以 `stop` 收尾（相当于「一次只准调一个工具」）；`at_least_one: false` 时模型可以不输出任何 `<tool_call>` 就直接停止（允许「这次不调工具」）。两个开关组合决定了工具调用的强制程度。

**练习 2**：为什么示例把函数名写进 `begin` 而不是放进 `content` 的 schema 里？

**答案**：`begin` 是固定字符串，函数名进入 `begin` 后由语法逐字符强制保证拼写正确，模型无法幻觉出不存在的函数名；若放进 schema，函数名就成了被采样的自由字符串，需要再用 `enum` 约束。每工具一条标签规则的代价是标签数量随工具数线性增长，工具很多时需权衡。

**练习 3**：`structural_tag` 字段为什么允许传字符串？

**答案**：管线侧缓存键对两种形态做了归一（见 4.2.3 的 `getResponseFormatKey`：字符串原样、对象 `JSON.stringify`），`compileStructuralTag` 也同时接受两种输入。允许字符串便于把标签定义作为配置持久化、跨进程传递（例如从服务端下发）。

### 4.2 标签内约束应用：管线层执行链

#### 4.2.1 概念说明

协议层只做「配对校验」，真正的约束发生在 `LLMChatPipeline`。u6-l3 已讲过 grammar/json_object 的掩码链路，本模块聚焦 structural tag 在这条链上的三个专属细节：

1. **编译分派**：`prefillStep` 里按 `response_format.type` 四路分派，structural tag 走 `compileStructuralTag`。
2. **缓存复用**：`responseFormatCacheKey` 对标签定义做字符串化，同一标签定义的重复请求只 `reset()` 复用 matcher，跳过编译。
3. **采样链位置**：grammar 掩码是采样链的**第 0 步**，先于 `LogitProcessor`（CPU）与 `logit_bias`（GPU）执行——这一点对两者的协作至关重要。

关于与 `LogitProcessor` 的协作（本讲学习目标之三）：掩码先把非法 token 的 logit 置为负无穷，随后 `LogitProcessor.processLogits` 在 CPU 上继续加工剩余 logits。顺序保证了 processor 永远改不动「已被语法禁止的 token」——语法约束拥有最高优先级；processor 只能在合法集合内部做微调（如进一步压低某 token 概率）。反过来，processor 想「强行放开」某个非法 token 是做不到的。

#### 4.2.2 核心流程

```text
prefillStep（每轮请求开始）
  │
  ├─ response_format.type ∈ {json_object, grammar, structural_tag}？
  │    ├─ 是 ⟹ 计算 responseFormatCacheKey
  │    │     ├─ 键相同且已有 matcher ⟹ grammarMatcher.reset()   // 复用，免编译
  │    │     └─ 键不同 ⟹ 异步编译（Promise，与 prompt 预填充并行以隐藏开销）
  │    │           └─ 按类型分派：compileJSONSchema / compileGrammar / compileStructuralTag
  │    └─ 否 ⟹ 无约束
  │
  ├─ prompt 分块预填充（与编译并行）
  ├─ await Promise.all([device.sync(), grammarMatcherInitPromise])   // 汇合点
  │
  └─ 逐 token 循环（sampleTokenFromLogits）：
        0. grammar 掩码：getNextTokenBitmask → 拷贝到 GPU → apply_bitmask_inplace
        1. LogitProcessor（CPU）
        2. logit_bias（GPU）
        3. 惩罚（GPU）
        4. softmax + top-p 采样
        5. logitProcessor.processSampledToken(sampledToken)
        6. grammarMatcher.acceptToken(sampledToken)   // 推进状态机；拒绝则抛内部错误
```

structural tag 与 json_object 在这条链上**共用全部基础设施**，唯一分岔点在编译入口。掩码阶段，自由文本区返回的位掩码近似全 1（任何 token 合法），受约束区返回的掩码则精确反映 JSON schema 状态机——「分段约束」就是在这里落到每个 token 的掩码上的。

#### 4.2.3 源码精读

**缓存键归一（structural_tag 分支）**：

[src/llm_chat.ts:674-696](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L674-L696)

`getResponseFormatKey` 对三种约束类型各返回一个字符串键：`json_object` 用 schema、`grammar` 用语法串、structural tag 用标签定义本身（字符串原样返回、对象 `JSON.stringify`，L691-693）。字段 `responseFormatCacheKey` 的注释（[src/llm_chat.ts:151-154](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L151-L154)）说明了用途：决定每轮 prefill 时是重新初始化 matcher 还是仅 reset。

**编译分派与并行隐藏**：

[src/llm_chat.ts:763-831](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L763-L831)

prefillStep 的第 -1 步：命中缓存则同步 `reset()`（L773-782，并打印 "Reuse grammar matcher." 日志）；未命中则创建异步 Promise 编译。注意 L808-821 的四路分派三元表达式，最后一支 `: await this.grammarCompiler!.compileStructuralTag(responseFormat.structural_tag!)` 就是 structural tag 的编译入口。这个 Promise 稍后在 [src/llm_chat.ts:889](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L889) 与 `device.sync()` 一起被 `Promise.all` 等待——编译与 prompt 预填充在时间上重叠，编译开销被 prefill 计算吸收。

**grammarConstrained 判定与逐 token 掩码**：

[src/llm_chat.ts:1700-1748](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1700-L1748)

`sampleTokenFromLogits` 的第 0 步。L1701-1704 判定 `grammarConstrained`，structural tag 与 json_object、grammar 并列在内。随后 `getNextTokenBitmask()` 拿到 CPU 上的 `Int32Array` 掩码，校验长度后 `copyFrom` 拷到 GPU，调 `this.fapplyBitmask` 对 logits **就地屏蔽**（L1733-1737）。耗时计入 `curRoundGrammarPerTokenTotalTime`。

**与 LogitProcessor 的顺序协作**：

[src/llm_chat.ts:1750-1778](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1750-L1778)

紧随掩码之后的第 1 步：logits 先搬回 CPU，交给 `logitProcessor.processLogits` 加工，再写回 GPU（L1766-1768）。这就是「掩码在前、processor 在后」的顺序保证——processor 看到的已是语法裁剪过的 logits，只能在合法集合内做调整。

**采样后推进状态机**：

[src/llm_chat.ts:1968-1980](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1968-L1980)

第 6 步：`grammarMatcher.acceptToken(sampledToken)` 把刚采样的 token 喂回状态机（自由文本区用来匹配触发串，受约束区用来推进 JSON 状态）。若返回 false 抛内部错误——正常情况下掩码已保证不会拒绝。

**一个值得注意的统计口径细节**：

[src/engine.ts:917-920](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b7d76/src/engine.ts#L917-L920)（非流式；流式同款在 [src/engine.ts:705-708](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L705-L708)）

引擎在组装 `usage.extra` 时用 `usedGrammar` 决定是否附加 `grammar_init_s` / `grammar_per_token_s`，但其判断只列了 `"grammar"` 和 `"json_object"`——**不含 `"structural_tag"`**。也就是说：管线内部确实统计了 structural tag 的掩码耗时（[src/llm_chat.ts:169-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L169-L173) 的两个计时段照常累加），但这份统计**不会出现在 structural_tag 请求的 `usage.extra` 里**。做性能实验时要意识到这一口径缺口（综合实践会给出替代观测手段），也可以把它当作一个入门级二开选题：把这两处判断补上 `structural_tag`。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「编译只发生一次、后续请求复用 matcher」的缓存行为。

**操作步骤**：

1. 继续使用 `examples/structural-tag-tool-use`（已 `npm install`）。修改 `mcp_structural_tag.ts` 的 `main()`：把「请求约束工具调用」那一段包进 `for` 循环，用**完全相同**的 `responseFormat` 连发 3 次请求（每次可以用新的 user 问题，例如分别问三个城市的天气）。
2. 创建引擎时 `logLevel` 已是 `"INFO"`（[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L133)）。打开浏览器控制台，观察日志。
3. 第 2 轮循环中，故意改动 `mcpStructuralTag` 的一个字段（比如把某个工具 `description` 改一个字），再发一次请求。

**需要观察的现象**：第 1 次请求出现 `"Initialize new grammar matcher."`（来自 [src/llm_chat.ts:788](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L788)）；第 2、3 次请求出现 `"Reuse grammar matcher."`（来自 [src/llm_chat.ts:779](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L779)）；改动字段后的那次请求又回到 "Initialize new grammar matcher."。

**预期结果**：缓存键是标签定义的字符串化整体（对象经 `JSON.stringify`），任何字段变化都会导致缓存失效并重新编译。生成部分需 WebGPU 浏览器，日志现象「待本地验证」；缓存键的归一逻辑本身已可从源码直接推出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GrammarMatcher` 的初始化要写成 Promise 与 prefill 并行，而不是串行 await？

**答案**：prefill（prompt 分块前向）是 GPU 密集操作，grammar 编译是 CPU/wasm 操作，二者无数据依赖。并行后编译耗时被 prefill 时间吸收，总延迟约等于二者较大者而非之和。汇合点在采样第一个 token 之前（`Promise.all([device.sync(), grammarMatcherInitPromise])`），因为第一个 token 就需要掩码。

**练习 2**：如果 `LogitProcessor` 试图把某个被语法禁止的 token 的 logit 加到很大，会发生什么？

**答案**：什么也不会改变。掩码在第 0 步已把非法 token 的 logit 置为负无穷（`apply_bitmask_inplace` 就地屏蔽），processor 在第 1 步加工的是屏蔽后的 logits，正无穷大的负值加不动。这保证了语法约束的绝对优先级——协作的方向是单向的：语法裁剪集合，processor 在集合内微调。

**练习 3**：一个 structural tag 请求的 `usage.extra` 里没有 `grammar_init_s`，能说明管线没有做约束吗？

**答案**：不能。如 4.2.3 最后一段所述，`usedGrammar` 的判断漏掉了 `structural_tag`，只是统计字段没附加；管线内部 `grammarConstrained` 包含 structural tag，掩码与 acceptToken 全程执行。判断「是否真的被约束」最直接的办法是看输出是否严格遵守标签格式。

### 4.3 与 function calling 的对比：三种工具调用路线

#### 4.3.1 概念说明

现在把 WebLLM 里实现「让模型调工具」的三条路线并排放在一起。回顾 u6-l2 的核心结论：模型从不执行函数，只输出结构化调用声明，函数调用 = 输入侧 prompt 注入 + 输出侧文本解析。三条路线的差异全在「注入怎么做、约束怎么保证、解析谁来做」：

| 维度 | 路线一：Hermes 自动函数调用 | 路线二：手动模板 | 路线三：structural tag |
| --- | --- | --- | --- |
| 请求字段 | `tools` + `tool_choice` | 自写 system prompt | `response_format.structural_tag` + 自写 system prompt |
| 模型限制 | 仅 `functionCallingModelIds` 白名单（5 个 Hermes 模型） | 无 | 无 |
| 输出合法性保证 | json_object + 官方 schema 约束（整段 JSON） | 无（裸文本，可能非法） | 标签内 JSON schema 约束，标签外自由 |
| 输出形态 | 整段 JSON | 自定格式 | 自由文本 + `<tool_call>` 块 |
| `tool_calls` 解析 | 引擎自动（`getToolCallFromOutputMessage`），`finish_reason` 改写为 `tool_calls` | 调用方自解析 | 调用方自解析（示例用正则） |
| `finish_reason` | `tool_calls` | `stop` | `stop`（引擎不知晓这是工具调用） |
| 适用场景 | 恰好用 Hermes 模型、想省事 | 需要完全控制 prompt | 任意模型 + 语法保证 + 允许工具调用外自由文本 |

#### 4.3.2 核心流程

三条路线在引擎内的分岔可以画成一张决策图：

```text
chatCompletion(request)
  │
  ├─ request.tools 非空？
  │    ├─ 是 ⟹ 关卡 7：白名单检查 + Hermes 专属改写
  │    │         （覆写 response_format 为 json_object+官方 schema、注入系统消息）
  │    │         生成后：finish_reason=="stop" 且 tools 非空
  │    │              ⟹ 自动解析 tool_calls、finish_reason 改写为 "tool_calls"
  │    └─ 否 ⟹ 不做任何工具相关处理
  │         ├─ response_format.type == "structural_tag"
  │         │    ⟹ 标签内语法约束；输出原样返回，finish_reason 为 "stop"
  │         └─ （手动模板/无约束）
  │              ⟹ 裸文本输出，合法性全靠模型自觉
```

#### 4.3.3 源码精读

**路线一的引擎侧自动解析（对比基准）**：

[src/engine.ts:874-888](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L874-L888)

非流式路径的函数调用后处理：`isFunctionCalling` 由 `request.tools` 是否非空决定（L875-876）；仅当 `finish_reason` 为 `stop` 且是函数调用请求时，才调 `getToolCallFromOutputMessage` 解析并把 `finish_reason` 改写为 `tool_calls`。**structural tag 请求没有 `tools` 字段，永远走不进这个分支**——这就是「解析由调用方自己做」的源码依据。若因 `length`/`abort` 停止则不解析（注释 L882 说明）。

**路线三的调用方侧手工解析**：

[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:82-102](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L82-L102)

`parseToolCallBlocks` 用正则 `/<tool_call>\s*({[\s\S]*?})\s*<\/tool_call>/g` 从自由文本里抠出所有工具调用块，`JSON.parse` 载荷后校验 `name`/`arguments` 字段。因为标签内 JSON 已被语法保证合法，这里 `JSON.parse` 不会失败——手工解析只需要「定位」，不需要「容错」，这是 structural tag 相比手动模板路线的实质收益。

**路线三的多轮闭环**：

[examples/structural-tag-tool-use/src/mcp_structural_tag.ts:179-215](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/structural-tag-tool-use/src/mcp_structural_tag.ts#L179-L215)

拿到工具调用后：先把 assistant 回复（含手工构造的 `tool_calls` 结构）追加进 messages（L179-190），再对每个调用执行 stub 工具并以 `role: "tool"` + `tool_call_id` 回传结果（L192-202），最后追加一条 user 消息请求自然语言总结并发起第二轮请求（L204-215）。第二轮请求不带 `response_format`，模型自由作答。这套消息结构与 OpenAI 协议完全兼容（u6-l2 讲过 `tool_call_id` 不进 prompt）。

**路线一的输入侧注入（对照）**：

[src/openai_api_protocols/chat_completion.ts:560-596](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L560-L596)

Hermes 专属的硬编码改写：拒绝用户自带 `response_format`（L568-573），覆写为 `json_object` + 官方 schema（L574-577），注入含工具清单的系统消息（L580-595）。对比之下，structural tag 把这些决定权全部交还调用方——工具描述怎么写（示例 L136-148 手写在 system prompt 里）、约束长什么样（标签定义）、输出怎么解析（正则），都由你掌控。

#### 4.3.4 代码实践

**实践目标**：用同一个小模型、同一个任务，对比「整段 JSON 约束」与「structural tag 分段约束」的输出形态与速度差异（即讲义规格中的对比实验，此处先做小型版，完整版见第 5 节）。

**操作步骤**：

1. 准备一个 JSON schema，把工具调用整体包进 JSON（模拟整段约束路线）。可参考 `examples/json-schema` 中 `functionCallingExample` 的 schema 构造方式（[examples/json-schema/src/json_schema.ts:151-160](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-schema/src/json_schema.ts#L151-L160)，形如 `{ "tool_calls": [ { "arguments": ..., "name": ... } ] }），用 typebox 或手写 JSON 字符串均可。
2. 在 `mcp_structural_tag.ts` 里新增第二个请求分支：同样的 messages 与模型，但 `response_format` 换成 `{ type: "json_object", schema: <第 1 步的 schema> }`（示例代码，非仓库原有）：

   ```ts
   // 示例代码：整段 JSON 约束路线（对照实验用）
   const jsonModeReply = await engine.chat.completions.create({
     stream: false,
     messages,
     max_tokens: 1024,
     response_format: { type: "json_object", schema: toolCallsSchema },
   });
   console.log(jsonModeReply.usage!.extra);
   ```

3. 让两条路线回答同一个问题（如「巴黎天气 + UTC 时间」），各跑一轮，先对比**输出形态**：整段 JSON 路线的回复是不是从头到尾都是 JSON？structural tag 路线的回复里标签外有没有自然语言？
4. 对比两份 `usage!.extra`：看 `decode_tokens_per_s`、`time_per_output_token_s`、`e2e_latency_s`。

**需要观察的现象**：整段 JSON 路线输出为纯 JSON（所有文字被压进字符串字段）；structural tag 路线输出为「自由文本 + 标签块」。速度字段两条路线都有输出，但 `grammar_init_s`/`grammar_per_token_s` 只在 json_object 路线的 extra 里出现（4.2.3 讲过的口径缺口）。

**预期结果**：输出形态差异稳定可见；速度差异方向与幅度「待本地验证」——理论上自由文本占比越高、structural tag 的 \( T_{bitmask} \) 优势越明显，但具体数字依赖模型与 prompt，请以实测为准。

#### 4.3.5 小练习与答案

**练习 1**：structural tag 请求的 `finish_reason` 是什么？为什么？

**答案**：`stop`。引擎只有在 `request.tools` 非空且正常停止时才把 `finish_reason` 改写为 `tool_calls`（[src/engine.ts:878-883](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L878-L883)），structural tag 请求不带 `tools`，引擎不知道这是一次工具调用。调用方应用层的判断依据应当是「输出中能否解析出标签块」，而非 `finish_reason`。

**练习 2**：三条路线里，哪条对「模型输出非法内容」的防御最强？哪条最弱？

**答案**：最强是 Hermes 自动与 structural tag（都有语法级保证：前者整段 JSON、后者标签内 JSON）；最弱是手动模板（完全依赖模型自觉，小模型经常产出缺引号、多尾逗号等非法 JSON）。structural tag 额外比 Hermes 路线多了「标签外自由」与「不限模型」两项收益。

**练习 3**：如果要让 structural tag 路线也产出 `finish_reason: "tool_calls"` 和自动填充的 `message.tool_calls`，最小改动应放在哪一层？

**答案**：放在引擎层 `chatCompletion` 的后处理（[src/engine.ts:874-888](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L874-L888) 附近）：把 `isFunctionCalling` 的判定扩展为「`tools` 非空 **或** `response_format.type === "structural_tag"`」，并补一个能识别标签块的解析函数（可复用示例 `parseToolCallBlocks` 的正则思路）。这正是 u7-l5 二开演练可以选的题目之一。

## 5. 综合实践

**任务：自定义标签 + 双路线三轮对比实验**（本讲规格指定的完整实践）。

### 5.1 实践目标

1. 验证标签只是普通字符串：改掉开始/结束标签后约束与解析依然成立。
2. 用实测数据回答：「分段约束」比「整段 JSON 约束」快多少，为什么。

### 5.2 操作步骤

**第一步：运行原示例。** 进入 `examples/structural-tag-tool-use/`，`npm install` 后 `npm start`，浏览器打开 `http://localhost:8887`，打开控制台确认能走完「工具调用 → 工具执行 → 总结回复」三段日志。

**第二步：替换标签。** 在 `mcp_structural_tag.ts` 中把 `<tool_call>` / `</tool_call>` 全部替换为自定义串（示例代码，比如 `[[CALL]]` / `[[/CALL]]`）。注意必须同步修改**五处**，缺一不可：

| 位置 | 字段/代码 | 作用 |
| --- | --- | --- |
| L51 | `triggers: ["[[CALL]]"]` | 状态机的触发串 |
| L53 | `begin` 模板串 | 标签完整开头（含 JSON 前缀） |
| L55 | `end` 模板串 | 标签结尾 |
| L88 | `parseToolCallBlocks` 的正则 | 调用方解析 |
| L137-139 | system prompt 中的标签说明 | 让模型知道要输出什么格式 |

**第三步：验证。** 重新发起请求，确认：输出中出现 `[[CALL]]{"name": ..., "arguments": ...}[[/CALL]]`；`parseToolCallBlocks` 仍能解析出调用；第二轮总结正常。若只改了定义没改正则，会出现「输出合法但解析抛 `Failed to find any <tool_call> blocks.`」的错位现象——这正好证明约束由标签定义驱动、解析由你的代码驱动，二者是独立的两半。

**第四步：双路线三轮对比。** 按 4.3.4 第 1-2 步准备 `json_object` 对照分支，然后：

1. 对同一问题（如「东京与巴黎的天气、以及 UTC 时间」），structural tag 与 json_object 两条路线**各连跑 3 轮**（每轮新开 messages，避免多轮 KV cache 复用干扰计时；参考 4.2.4 的循环改法）。
2. 每轮记录 `usage.extra` 的 `decode_tokens_per_s`、`time_per_output_token_s`、`e2e_latency_s`，json_object 路线额外记录 `grammar_per_token_s`。
3. 求每条路线三轮均值，算出 structural tag 相对 json_object 的 decode 提速百分比：

   \[ \text{提速比} = \frac{\bar{v}_{tag} - \bar{v}_{json}}{\bar{v}_{json}} \times 100\% \]

   其中 \( \bar{v} \) 为三轮 `decode_tokens_per_s` 均值。

### 5.3 需要观察的现象

- 自定义标签后整条链路依旧工作（第二步验证点）。
- 两条路线的输出形态差异（纯 JSON vs 自由文本 + 标签块）。
- `decode_tokens_per_s` 的差异方向与幅度；json_object 路线每 token 的 `grammar_per_token_s` 作为「整段约束的掩码成本」参考锚点。

### 5.4 预期结果与解释要点

- **形态**：json_object 路线全程 JSON；structural tag 路线标签外可出现自然语言。
- **速度**：若模型在 structural tag 路线下输出较多自由文本，预期 `decode_tokens_per_s` 更高——自由文本区的掩码计算近似「全合法」，\( T_{bitmask} \) 更小，且无需为符合 JSON 语法生成转义、引号等冗余 token。若回复几乎全是标签块，差距应收窄。具体数值「待本地验证」，请以实测为准，并检查输出 token 数是否相当（token 数不同会污染对比）。
- 已知口径缺口：structural tag 路线看不到 `grammar_init_s`/`grammar_per_token_s`（4.2.3），对比时用通用速度字段。

## 6. 本讲小结

- `ResponseFormat.type = "structural_tag"` 采用触发式约束：`triggers` 切入、`begin/content(json_schema)/end` 定义标签结构、`at_least_one` 与 `stop_after_first` 控制出现次数；协议层以镜像双向校验保证字段与类型配对（`InvalidResponseFormatStructuralTagError`）。
- 管线层与 json_object/grammar 共用同一套 xgrammar 掩码基础设施，分岔仅在编译入口 `compileStructuralTag`；缓存键为标签定义的字符串化，重复请求只 `reset()` 复用。
- grammar 掩码是采样链第 0 步，先于 `LogitProcessor` 与 `logit_bias`——语法约束拥有绝对优先级，processor 只能在合法 token 集合内微调；采样后 `acceptToken` 推进状态机。
- 性能优势来源是「分段」：自由文本区状态机近似全合法（\( T_{bitmask} \) 小），且模型不必把所有文字塞进 JSON 转义；收益大小取决于自由文本占比，已设计实验验证。
- 与 Hermes 自动函数调用相比：structural tag 不限模型（绕开 `functionCallingModelIds` 白名单）、约束只在标签内、解析与 prompt 完全由调用方掌控；代价是 `finish_reason` 保持 `stop`、`tool_calls` 需自解析。
- 已发现并验证的源码细节：引擎 `usedGrammar` 统计口径漏列 `structural_tag`，导致其 `usage.extra` 缺少 `grammar_init_s`/`grammar_per_token_s`——既是实验设计的坑，也是现成的二开选题。

## 7. 下一步学习建议

本讲讲完，第六单元（OpenAI 兼容协议深度）全部结束。接下来进入第七单元「高级主题」，建议顺序：

1. **u7-l1 错误处理体系**：本讲遇到的 `InvalidResponseFormatStructuralTagError` 属于配置非法家族，下一讲系统梳理六十余个错误类的分层。
2. **u7-l3 性能观测**：本讲的对比实验只用了 `usage.extra` 的粗粒度字段；下一讲学习 `curRoundLatencyBreakdown`、`enable_latency_breakdown` 等更细的埋点，可以把 \( T_{bitmask} \)（`grammarBitmaskTime`）单独量出来，弥补本讲指出的统计口径缺口。
3. **u7-l5 构建与二开路线图**：把 4.3.5 练习 3（为 structural tag 补 `tool_calls` 自动解析）或 4.2.3 的统计口径修补作为二开演练题目，走一遍 engine/types/协议三层打通加单测的完整流程。

源码层面，若想继续深挖约束解码，可以阅读 `node_modules/@mlc-ai/web-xgrammar/` 下 `GrammarCompiler.compileStructuralTag` 的类型定义（需先 `npm install`），以及 `examples/json-schema` 中其余示例（EBNF grammar 路线），对照体会四种 `response_format` 的谱系。
