# JSON Mode 与结构化输出（xgrammar）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ResponseFormat` 四种 `type`（`text` / `json_object` / `grammar` / `structural_tag`）各自的含义，并纠正一个常见误解：**「json_schema 并不是一个独立的 type**」，schema 约束走的是 `type: "json_object"` 加 `schema` 字段的组合。
2. 追踪 GrammarMatcher 的完整生命周期：schema/grammar 字符串 → xgrammar 编译 → matcher 创建 → 每步解码的 bitmask 约束 → `acceptToken` 状态推进。
3. 解释 `responseFormatCacheKey` 如何让同一 schema 的后续请求复用 matcher、把编译开销摊薄到接近零。
4. 读懂 `usage.extra` 中的 `grammar_init_s` 与 `grammar_per_token_s` 两个性能观测字段，并说清楚 xgrammar 编译发生在请求处理的哪个阶段。

本讲依赖 u6-l1（协议层校验八道关卡）与 u3-l5（采样链路的固定顺序：grammar 掩码 → LogitProcessor → logit_bias → 惩罚 → softmax → top-p）。本讲就是那条链路最前端「grammar 掩码」的完整展开。

## 2. 前置知识

**结构化输出（Structured Output）**：让模型输出严格符合预定格式的文本（通常是合法 JSON，甚至符合某个 JSON Schema）。传统做法是「提示词里求模型输出 JSON，然后祈祷 + 事后 `JSON.parse` 重试」；结构化输出则从机制上**保证**输出合法。

**约束解码（Constrained Decoding）**：实现保证的核心手段。语言模型每步输出的是「整个词表上的概率分布」，采样前把不合法候选 token 的 logit 打成 \(-\infty\)，被禁止的 token 概率即为零，模型只能从合法候选中挑。这与 u3-l5 讲过的 `logit_bias`、惩罚项是同一层的「logit 修饰」，只是方向不同：惩罚是「软性压制」，语法掩码是「硬性禁止」。

**上下文无关语法与 EBNF**：JSON 的结构可以用一组产生式规则（grammar）精确描述，例如「对象 ::= `{` (字符串 `:` 值 (`,` 字符串 `:` 值)*)? `}`」。EBNF 是书写这类规则的常用记法。JSON Schema 则是更高层的描述：「必须有哪些字段、字段类型是什么」——它可以被自动翻译成 EBNF，再编译成状态机。

**xgrammar（`@mlc-ai/web-xgrammar`）**：WebLLM 引入的结构化输出引擎（WebAssembly 版本）。它把语法编译成自适应状态机，并为「当前状态下哪些 token 前缀合法」预计算一张**位掩码（bitmask）**——词表中每个 token 对应一个比特，1 表示允许、0 表示禁止。词表几万 token，逐个判断太慢，按 int32 打包后只要 \(\lceil \text{vocab\_size} / 32 \rceil\) 个字。

**GrammarMatcher**：xgrammar 的运行时对象。每次解码前调用 `getNextTokenBitmask()` 拿当前合法 token 位掩码；采样出 token 后调用 `acceptToken(tokenId)` 让状态机前进一个 token。语法匹配到终点后，位掩码只允许停止 token，生成自然以 `stop` 收尾。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/openai_api_protocols/chat_completion.ts` | `ResponseFormat` 类型定义；`postInitAndCheckFields` 中对 response_format 的四组配对校验；`CompletionUsage.extra` 中 grammar 性能字段 |
| `src/llm_chat.ts` | 本讲主战场：GrammarMatcher 的创建、缓存复用、bitmask 约束采样、`acceptToken` 状态推进、资源释放 |
| `src/engine.ts` | 请求入口处调用协议校验；响应组装时读取管线的 grammar 统计并写入 `usage.extra` |
| `src/config.ts` | `GenerationConfig.response_format`——请求级采样配置如何一路传到管线 |
| `src/error.ts` | `InvalidResponseFormatError` 等 response_format 专属错误类 |
| `src/support.ts` | `getTokenTableFromTokenizer`：从 tokenizer 导出整张词表供 xgrammar 使用 |
| `examples/json-mode/` / `examples/json-schema/` | 两个可运行示例：纯 JSON 模式与 schema 约束模式 |
| `tests/llm_chat_pipeline.test.ts` | 用 mock 的 xgrammar 验证 matcher 复用与重建逻辑 |
| `tests/openai_chat_completion.test.ts` | 验证 response_format 字段配对错误 |

## 4. 核心概念与源码讲解

### 4.1 ResponseFormat 类型与协议层校验

#### 4.1.1 概念说明

用户通过 `ChatCompletionRequest.response_format` 声明「我要什么格式的输出」。这个字段在协议层是一个四选一的判别结构：`type` 决定走哪条约束路线，`schema` / `grammar` / `structural_tag` 三个附属字段与 `type` 一一配对。

必须先纠正一个广泛存在的误解（本讲大纲的原始表述也踩了这个坑）：**WebLLM 没有 `type: "json_schema"` 这个取值**。看类型定义就清楚了：

- 纯 JSON 模式：`{ type: "json_object" }`——只保证输出是合法 JSON，字段内容随意；
- Schema 约束模式：`{ type: "json_object", schema: "<JSON Schema 字符串>" }`——保证输出符合你给定的 JSON Schema（字段名、类型、枚举值都被强制）。

所谓「json_mode 与 json_schema 两个示例的区别」，实际是**同一个 `type` 下是否携带 `schema` 字段**的区别。`grammar`（直接给 EBNF）和 `structural_tag`（标签分段约束，下一讲 u6-l4 专题）则是另两条路线。

#### 4.1.2 核心流程

```text
用户请求 (response_format)
   │
   ├─ 协议层 postInitAndCheckFields（engine.chatCompletion 入口处调用）
   │     关卡 6   ：schema 字段存在 ⇒ type 必须是 json_object
   │     关卡 6.1 ：grammar 字段存在 ⇒ type 必须是 grammar
   │     关卡 6.2 ：type 是 grammar  ⇒ grammar 字段必须存在
   │     关卡 6.3 ：structural_tag 与 type 的双向配对校验
   │     关卡 7.2.1：Hermes 函数调用时强制覆写为 json_object + 官方 schema
   │
   ├─ 通过 ⇒ response_format 随 GenerationConfig 进入管线
   │
   └─ 不通过 ⇒ 抛 InvalidResponseFormatError 等错误，请求终止（模型不动）
```

#### 4.1.3 源码精读

`ResponseFormat` 的完整定义——注意 `schema` 是 `json_object` 的可选附属字段，不是独立 type：

[src/openai_api_protocols/chat_completion.ts:L1198-L1227](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1198-L1227) 定义了四个字段：`type`（L1202，四选一）、`schema`（L1206，JSON Schema 字符串，需配 `json_object`）、`grammar`（L1221，EBNF 字符串，需配 `grammar`）、`structural_tag`（L1226）。

请求字段上的注释还给出一条重要实践建议：使用 JSON 模式时**务必同时在提示词里要求模型输出 JSON**。这条注释沿袭自 OpenAI 文档（L237-L251）：[src/openai_api_protocols/chat_completion.ts:L237-L251](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L237-L251)。在 WebLLM 中语法约束已硬性保证「输出是合法 JSON」，但提示词仍然决定内容质量与生成长度——语法只管形状，不管内容。

四组配对校验集中在 `postInitAndCheckFields` 的关卡 6：

[src/openai_api_protocols/chat_completion.ts:L502-L548](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L502-L548) 依次做四件事：L502-L510 检查「给了 `schema` 却不是 `json_object`」→ 抛 `InvalidResponseFormatError`；L512-L520 检查「给了 `grammar` 却不是 `grammar` 类型」→ 抛 `InvalidResponseFormatGrammarError`；L522-L530 反向检查「type 是 `grammar` 却没给 grammar 字符串」→ 同样抛 `InvalidResponseFormatGrammarError`；L532-L548 对 `structural_tag` 做完全对称的双向校验。

这三个错误类的定义在 [src/error.ts:L407-L432](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L407-L432)，都是普通 `Error` 的简单子类，消息即校验规则本身。

协议层还会**原位改写**请求——Hermes 系模型走函数调用时（u6-l2 讲过的第 7.2.1 关），WebLLM 直接把 `response_format` 覆写为 `{ type: "json_object", schema: 官方工具调用schema }`：

[src/openai_api_protocols/chat_completion.ts:L566-L577](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L566-L577) 先拒绝用户自带的 response_format（抛 `CustomResponseFormatError`），再注入官方 schema。这正是「结构化输出是函数调用的底层机制」的证据：Hermes 的 tool_calls 解析之所以可靠，是因为输出被 schema 硬约束了。

校验的调用时机在引擎入口：[src/engine.ts:L787-L805](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L805) 中 `chatCompletion` 在真正触碰模型前调用 `API.postInitAndCheckFieldsChatCompletion`。校验通过后，`response_format` 作为 `GenerationConfig` 的一个字段传给管线（类型定义见 [src/config.ts:L145-L165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L145-L165)，`response_format` 在 L161）。

#### 4.1.4 代码实践

**实践目标**：用测试替身验证四组配对校验，不需要下载模型。

1. 打开 `tests/openai_chat_completion.test.ts`，阅读 L115-L123 的用例：[tests/openai_chat_completion.test.ts:L115-L123](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts#L115-L123)，它构造 `{ schema: "some json schema" }`（没有 `type: "json_object"`）并断言错误消息。
2. 模仿它写一个新用例（示例代码，可加到该文件后运行 `npx jest tests/openai_chat_completion.test.ts`）：

```ts
// 示例代码：验证 grammar 与 type 的配对校验
test("response_format grammar without type grammar throws", () => {
  expect(() =>
    API.postInitAndCheckFieldsChatCompletion(
      {
        messages: [{ role: "user", content: "hi" }],
        response_format: { type: "json_object", grammar: "root ::= A" },
      } as any,
      "Llama-3.2-3B-Instruct-q4f16_1-MLC",
      ModelType.LLM,
    ),
  ).toThrow(InvalidResponseFormatGrammarError);
});
```

3. **需要观察的现象**：三个配对错误（schema 配错 type、grammar 配错 type、type 是 grammar 但缺 grammar 字段）分别在哪个关卡抛出。
4. **预期结果**：三个用例全部按预期抛出对应错误类。错误消息内容以 `src/error.ts` 中的文案为准。

#### 4.1.5 小练习与答案

**练习 1**：`{ type: "json_object" }` 与 `{ type: "json_object", schema: "..." }` 在约束强度上有何区别？

**答案**：前者只保证输出是**语法合法的 JSON**（任意键值、任意嵌套都行）；后者把 JSON Schema 编译成语法，**字段名、类型、枚举取值范围**都被强制——例如 schema 里 `house` 是四选一枚举，模型在生成该字段的值时只能在四个候选中采样。

**练习 2**：为什么 `InvalidResponseFormatGrammarError` 不是「schema 写错了」的错误？它到底在报什么？

**答案**：它只报**字段配对**问题——`grammar` 字段与 `type: "grammar"` 必须成对出现（见 L512-L530）。而「schema 内容本身矛盾」发生在更晚的阶段：xgrammar 编译时（见 4.2），那是另一个层面的失败。

---

### 4.2 GrammarMatcher：从编译到逐 token 约束采样

#### 4.2.1 概念说明

`GrammarMatcher` 是管线（`LLMChatPipeline`）的私有成员，只在 `response_format.type` 是三种约束类型之一时才被创建。它的工作分三个时间尺度：

1. **首次初始化（一次性，较慢）**：导出整张词表给 xgrammar → 创建 `TokenizerInfo` → 创建 `GrammarCompiler` → 把 schema/grammar 编译成 `CompiledGrammar` → 创建 matcher。
2. **每个请求（很快）**：若 schema 没变（`responseFormatCacheKey` 命中），只调用 `matcher.reset()` 复位状态机，跳过全部编译。
3. **每个 token（极快，但累积可观）**：`getNextTokenBitmask()` 取合法 token 位掩码 → GPU 上按位掩码屏蔽 logits → 采样 → `acceptToken()` 推进状态机。

位掩码大小在构造函数里就定死了：\[\text{bitmaskSize} = \lceil \text{fullVocabSize} / 32 \rceil\] 个 int32。一个 128k 词表的模型约 4000 个 int32（16 KB），每步在 CPU（xgrammar wasm）与 GPU 之间往返一次。

#### 4.2.2 核心流程

```text
prefillStep(genConfig 含 response_format)
   │
   ├─ [-1] grammar 初始化（与 prompt prefill 并行！）
   │    ├─ key = getResponseFormatKey(response_format)
   │    ├─ key === responseFormatCacheKey 且已有 matcher
   │    │      ⇒ matcher.reset()            （复用，微秒级）
   │    └─ 否则 ⇒ 异步 Promise：
   │          ├─ 首次：getTokenTableFromTokenizer → TokenizerInfo → GrammarCompiler
   │          ├─ 按 type 分派编译：
   │          │    json_object(有schema) → compileJSONSchema(schema)
   │          │    grammar               → compileGrammar(ebnf)
   │          │    structural_tag        → compileStructuralTag(tag)
   │          │    （type 未定义的兜底   → compileBuiltinJSONGrammar）
   │          ├─ GrammarMatcher.createGrammarMatcher(compiled)
   │          └─ responseFormatCacheKey = key
   │
   ├─ [0..2] 正常 prefill prompt（写 KV cache）
   │
   └─ [4] await Promise.all([device.sync(), grammarInitPromise])
          └─ sampleTokenFromLogits(logits, genConfig)
                ├─ 步骤 0：getNextTokenBitmask() → 拷到 GPU → apply_bitmask_inplace
                ├─ 步骤 1+：LogitProcessor / logit_bias / 惩罚 / softmax / top-p
                └─ 步骤 6：acceptToken(sampledToken) —— 状态机前进

decodeStep 每轮同样走 sampleTokenFromLogits 的步骤 0 与步骤 6
语法匹配到终点 ⇒ 位掩码只放行停止 token ⇒ 生成以 finish_reason="stop" 收尾
```

注意一个精妙的工程设计：**编译与 prefill 并行**。语法编译（可能上百毫秒）不等 prompt 预填充做完才开工，两者赛跑，最后 `Promise.all` 汇合——编译开销被 prefill 时间掩盖了一部分。

#### 4.2.3 源码精读

管线持有的一组 grammar 相关字段：

[src/llm_chat.ts:L147-L161](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L147-L161) 声明了 `grammarMatcher`（L150，当前轮的 matcher）、`responseFormatCacheKey`（L154，缓存键）、`xgTokenizerInfo`（L157，词表信息，管线生命周期内只建一次）、`grammarCompiler`（L159，编译器，因绑定 TokenizerInfo 而持久化）、`bitmaskSize`（L161）。两个耗时统计字段在 [src/llm_chat.ts:L169-L173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L169-L173)。

位掩码尺寸与 tokenizer 后处理方式的确定在构造函数：

[src/llm_chat.ts:L190-L191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L190-L191) 计算 `bitmaskSize = Math.ceil(fullVocabSize / 32)`（注意用的是 `config.vocab_size`，某些模型因 dummy padding 与 token 表长度不同）。[src/llm_chat.ts:L202-L221](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L202-L221) 读取 `tokenizer_info` 决定 token 后处理方法（`byte_level` / `byte_fallback` 等）与是否前置空格——这两个字段决定 xgrammar 如何把「语法中的字符」映射到「词表中的 token」，缺失时回退 `raw` 并打警告。

缓存键的生成规则：

[src/llm_chat.ts:L674-L696](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L674-L696) `getResponseFormatKey` 按 type 取对应字符串做键：`json_object` 取 `schema`、`grammar` 取 `grammar`、`structural_tag` 取序列化后的标签定义。**键相同即视为同一约束**。

初始化与复用的分岔点在 `prefillStep` 开头：

[src/llm_chat.ts:L763-L831](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L763-L831) 是本讲最重要的一段。L767-L770 判定是否为约束类型；L772-L782 是**复用分支**——缓存键命中且 matcher 存在时只调 `this.grammarMatcher.reset()`，注释明确写着 "If we did not change the schema and have instantiated a GrammarMatcher, we reuse it"；L784-L830 是**重建分支**：L792-L802 首次使用时用 `getTokenTableFromTokenizer`（实现见 [src/support.ts:L77-L84](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L77-L84)，逐 id 导出整张词表）创建 `TokenizerInfo`，L803-L806 创建持久化的 `GrammarCompiler`；L808-L821 按 type 四路分派编译调用（`compileJSONSchema` / `compileGrammar` / `compileStructuralTag`，以及 type 未定义时兜底的 `compileBuiltinJSONGrammar`）；L822-L824 用编译产物创建 matcher 并立刻 `grammar.dispose()` 释放编译中间体；L825 写入新的缓存键。

编译与 prefill 的汇合点：

[src/llm_chat.ts:L887-L890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L887-L890) 注释写明 "We wait for prefill and grammar matcher init to finish"，`await Promise.all([this.device.sync(), grammarMatcherInitPromise])` 之后才采第一个 token——这保证首个 token 的 logits 一定已被掩码覆盖。

每步采样的约束应用（`sampleTokenFromLogits` 内）：

[src/llm_chat.ts:L1700-L1704](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1700-L1704) 计算 `grammarConstrained` 布尔值（三种约束 type 之一）。[src/llm_chat.ts:L1706-L1748](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1706-L1748) 是掩码应用的完整过程：L1717 调 `getNextTokenBitmask()` 得到 CPU 上的 `Int32Array`；L1721-L1726 校验长度必须等于 `bitmaskSize`；L1727-L1729 把掩码拷贝为 GPU 张量；L1733-L1737 调用 **GPU 端 PackedFunc** `fapplyBitmask`（即 `apply_bitmask_inplace`，绑定见 [src/llm_chat.ts:L300-L305](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L300-L305)，该函数在 VM 函数注册表中的位置见 [src/llm_chat.ts:L231-L246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L231-L246) 的 L242）把不合法 token 的 logit 就地置 \(-\infty\)。这段是 u3-l5 采样链路「第 0 步」，排在 LogitProcessor（第 1 步）、logit_bias、惩罚之前。

采样成功后推进状态机：

[src/llm_chat.ts:L1965-L1980](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1965-L1980) 在 LogitProcessor 的 `processSampledToken`（步骤 5）之后执行步骤 6：`this.grammarMatcher.acceptToken(sampledToken)` 让状态机吃进刚采样的 token；若返回 false（理论不可达——掩码已保证合法）抛内部错误。

资源释放在 `dispose()`：

[src/llm_chat.ts:L486-L508](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L486-L508) 卸载管线时释放 `grammarMatcher`（L488）、`xgTokenizerInfo`（L506）、`grammarCompiler`（L507）。

matcher 复用逻辑有专门的单元测试（mock 掉 xgrammar）：

[tests/llm_chat_pipeline.test.ts:L239-L262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L239-L262) 两个用例分别验证「schema 不变时只调 `reset` 不重建」与「schema 变化时走完整初始化并更新缓存键」。紧随其后的 L264-L274 验证 `grammar` type 会分派到 `compileGrammar`。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 matcher 复用，并量化编译开销。

1. 参照 `examples/json-schema`，写一个页面（或直接改 [examples/json-schema/src/json_schema.ts:L52-L67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-schema/src/json_schema.ts#L52-L67) 的请求），创建引擎时传 `logLevel: "INFO"`。
2. 用**同一个 schema** 连续发起两次 `chatCompletion`，并在请求里带 `stream_options: { include_usage: true }` 与 `stream: true`（或非流式后打印 `usage`）。
3. **需要观察的现象**：第一次请求时控制台出现 `Initialize new grammar matcher.` 与 `Initialize token table.`；第二次请求时出现 `Reuse grammar matcher.`；两次请求的 `usage.extra.grammar_init_s` 数值对比。
4. **预期结果**：第二次的 `grammar_init_s` 接近 0（只剩 `reset()` 的微秒级耗时），第一次则包含 TokenizerInfo 建表与编译耗时。再把 schema 字符串改动一个字符发起第三次请求，应看到重新走 `Initialize new grammar matcher.`。若你的环境无法运行 WebGPU，此实验「待本地验证」，可退而阅读 [tests/llm_chat_pipeline.test.ts:L239-L262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/llm_chat_pipeline.test.ts#L239-L262) 以测试断言代替观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GrammarCompiler` 与 `TokenizerInfo` 持久化在管线实例上，而 `GrammarMatcher` 需要随 schema 变化重建？

**答案**：编译器只依赖「词表 + tokenizer 后处理方式」，两者在管线生命周期内不变（L155-L159 的注释），所以建一次反复用。matcher 内部是**有状态的语法状态机**，其结构由具体 schema 编译产物决定——换了 schema 就得换 matcher；即使 schema 不变，每轮生成开始也要 `reset()` 把状态机拨回起点。

**练习 2**：掩码为什么在 GPU 上应用（`apply_bitmask_inplace`），而不是把 logits 拉回 CPU 处理？

**答案**：logits 本来就在 GPU 上（prefill/decode 的输出张量）。词表规模数万，来回搬运整个 logits 向量的带宽开销远大于在 GPU 上按 int32 掩码就地屏蔽；且后续 softmax、top-p 采样也都在 GPU 端 PackedFunc 里完成（u3-l5），保持数据不落地是一条连贯的设计主线。真正跨 CPU/GPU 的只有每步那一小块位掩码（约 16 KB）。

**练习 3**：语法匹配完成后模型如何「知道」该停了？

**答案**：xgrammar 状态机到达语法终点后，`getNextTokenBitmask()` 返回的掩码只放行停止 token（创建 `TokenizerInfo` 时传入了管线的 `stopTokens`，见 L801），于是模型只能采样出停止 token，`processNextToken` 按停止 token 路径收尾，`finish_reason` 为 `"stop"`——不需要任何特殊的终止判断代码。

---

### 4.3 结构化输出的响应组装与性能观测

#### 4.3.1 概念说明

约束解码不是免费的：编译要时间，每步的掩码计算与 GPU 往返也要时间。WebLLM 把这两类开销做成可观测指标塞进 `usage.extra`：

- `grammar_init_s`：本次请求初始化 matcher 的耗时（复用时接近 0）——**编译发生在哪个阶段的直接答案：prefill 阶段**，与 prompt 预填充并行；
- `grammar_per_token_s`：平均每个 token 花在「取掩码 + 接受 token」上的秒数。

响应组装本身则没有「JSON 特殊分支」：输出文本就是模型逐 token 生成的字符串（恰好被约束为合法 JSON），照常放进 `choices[0].message.content`，由调用方自行 `JSON.parse`。这符合门面层「协议形状不变、内部机制替换」的一贯风格。

#### 4.3.2 核心流程

```text
管线统计（curRoundGrammarInitTotalTime / curRoundGrammarPerTokenTotalTime）
   │  prefillStep 初始化 matcher 时记录 init 耗时（L826-L827）
   │  每次 getNextTokenBitmask / acceptToken 累加 per-token 耗时
   │
engine.chatCompletion 组装响应
   ├─ usedGrammar = type 是 "json_object" 或 "grammar"（注意：不含 structural_tag！）
   ├─ usage.extra 基础字段（e2e_latency_s、prefill/decode_tokens_per_s…）
   └─ usedGrammar 为真 ⇒ 追加 grammar_init_s 与 grammar_per_token_s
```

#### 4.3.3 源码精读

非流式路径的组装：

[src/engine.ts:L917-L953](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L917-L953) 先判定 `usedGrammar`（L917-L920，**只认 `json_object` 与 `grammar`，不认 `structural_tag`**——用 structural_tag 时这两个字段不会出现，这是容易踩的观测盲区）；L913-L915 从管线累加两个 grammar 统计；L945-L951 在 `usedGrammar` 为真时把 `grammar_init_s` 与 `grammar_per_token_s / completion_tokens`（摊到每 token）并入 `usage.extra`。流式路径的对应逻辑在 [src/engine.ts:L704-L744](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L704-L744），仅当 `stream_options.include_usage` 为真时随最后一个 usage chunk 发出。

这两个字段的类型定义与文档：

[src/openai_api_protocols/chat_completion.ts:L1009-L1019](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1009-L1019) 写明 `grammar_init_s` 是「初始化 grammar matcher 的秒数」，`grammar_per_token_s` 是「每 token 用于生成位掩码与接受 token 的秒数」，均为 WebLLM 对 OpenAI 协议的私有扩展。

示例侧的最小用法（纯 JSON 模式，注意它同时用了 `n: 2`——非流式下多候选与结构化输出兼容）：

[examples/json-mode/src/json_mode.ts:L25-L36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-mode/src/json_mode.ts#L25-L36) 的请求只给 `{ type: "json_object" }`，无 schema；schema 约束模式的请求构造见 [examples/json-schema/src/json_schema.ts:L52-L67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-schema/src/json_schema.ts#L52-L67)（`schema` 直接用 typebox 序列化出的字符串，两种给 schema 的方式见 L15-L35）。

#### 4.3.4 代码实践

**实践目标**：量化「约束的代价」。

1. 运行 `examples/json-schema`（`npm install` 后 `npm start`，需 WebGPU 浏览器）。
2. 对同一模型、同一 prompt 分别发起三种请求：不带 `response_format`、`{ type: "json_object" }`、`{ type: "json_object", schema: <哈利波特示例的嵌套 schema> }`（schema 见 [examples/json-schema/src/json_schema.ts:L78-L108](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-schema/src/json_schema.ts#L78-L108)，请求组装见 L126-L141）。
3. **需要观察的现象**：三种请求的 `usage.extra.decode_tokens_per_s` 与 `grammar_per_token_s`。
4. **预期结果**：带 schema 的请求 `decode_tokens_per_s` 最低（每步多出掩码往返），纯 JSON 模式居中，无约束最高；`grammar_per_token_s` 直接给出每 token 的语法开销。精确数值依赖设备，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：多轮对话中第二次请求带同一个 schema，`grammar_init_s` 会很大吗？

**答案**：不会。缓存键命中走 `reset()` 复用（L772-L782），`curRoundGrammarInitTotalTime` 只记录 reset 的耗时，接近 0。首次建 `TokenizerInfo` 的开销更是整个管线生命周期只有一次。

**练习 2**：用户拿到 `message.content` 后还需要做错误处理吗（比如 `JSON.parse` 失败重试）？

**答案**：语法层面不需要——约束解码保证输出是合法 JSON（带 schema 时还符合 schema）。但**语义层面**仍需校验：模型可能生成「符合 schema 但内容荒谬」的值（如把人的年龄填 999）；且若 `finish_reason` 是 `"length"`，输出可能被 `max_tokens` 截断导致 JSON 不完整（L246-L249 的注释提醒的正是这一点）。

---

## 5. 综合实践

**任务：地址簿生成器 + 矛盾 schema 的失败观察**。

1. **跑通两个官方示例**。分别进入 `examples/json-mode` 与 `examples/json-schema`，`npm install && npm start`（需支持 WebGPU 的浏览器），确认控制台输出合法 JSON。

2. **写一个嵌套 schema 的地址簿请求**（示例代码）：

```ts
// 示例代码：嵌套对象 + 数组的地址簿 schema
const addressBookSchema = JSON.stringify({
  type: "object",
  properties: {
    owner: { type: "string" },
    contacts: {
      type: "array",
      items: {
        type: "object",
        properties: {
          name: { type: "string" },
          age: { type: "integer" },
          address: {
            type: "object",
            properties: {
              city: { type: "string", enum: ["北京", "上海", "深圳"] },
              zipcode: { type: "string" },
            },
            required: ["city", "zipcode"],
          },
          phones: { type: "array", items: { type: "string" } },
        },
        required: ["name", "age", "address", "phones"],
      },
    },
  },
  required: ["owner", "contacts"],
});

const reply = await engine.chatCompletion({
  messages: [{ role: "user", content: "生成一个包含两个联系人的地址簿 JSON" }],
  response_format: { type: "json_object", schema: addressBookSchema },
  stream_options: undefined,
});
const book = JSON.parse(reply.choices[0].message.content!); // 语法上必然成功
```

3. **观察点 A——约束生效**：尝试在提示词里要求「给联系人加一个 email 字段」，验证输出里**不会出现** email——schema 没定义的字段被语法禁止，模型「想加也加不了」。

4. **观察点 B——矛盾 schema 的失败行为**。把 schema 改成自相矛盾，例如一个 `minimum: 10` 且 `maximum: 5` 的 integer 字段，或 `enum` 为空数组的字段，重新发起请求并打开 DevTools 控制台。基于源码可以做出如下**预判**（请本地验证）：
   - 字段配对错误（如 `type` 写成别的但带了 `schema`）会在**请求校验阶段**立刻抛 `InvalidResponseFormatError`（4.1 关卡 6），此时模型完全未被触碰；
   - schema **内容**矛盾则是另一回事：它发生在 xgrammar 的 `compileJSONSchema` 内部（[src/llm_chat.ts:L808-L821](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L808-L821)），而这个调用包在 `new Promise(async (resolve) => {...})` 里且**没有 reject 路径**（L786-L829）——编译抛错时 promise 永不 settle，`await Promise.all`（L889）可能一直挂起，错误以 unhandled rejection 的形式出现在控制台。这是阅读源码得出的推论，「待本地验证」；无论实际表现是挂起还是报错，都请记录并与 `InvalidResponseFormatGrammarError` 的触发条件做对比。
   - **结论要能回答**：xgrammar 编译发生在请求处理的第一阶段——`prefillStep` 的前置步骤（L763 注释中的 "-1" 步），与 prompt 预填充**并行**，而不是在 `reload` 加载模型时预编译，也不是逐 token 时才编译。

5. **观察点 C——性能账单**：对地址簿 schema 连发两次请求，记录两次的 `usage.extra.grammar_init_s`，验证 4.2.4 的复用结论。

## 6. 本讲小结

- `ResponseFormat.type` 只有四个取值（`text` / `json_object` / `grammar` / `structural_tag`），**不存在 `json_schema` 类型**——schema 约束是 `json_object` + `schema` 字段的组合；协议层四组配对校验（关卡 6/6.1/6.2/6.3）保证附属字段与 type 严格成对。
- GrammarMatcher 的初始化发生在**每次请求的 prefill 阶段**（`prefillStep` 的 "-1" 步），与 prompt 预填充并行执行、`Promise.all` 汇合；编译按 type 四路分派到 `compileJSONSchema` / `compileGrammar` / `compileStructuralTag` / 兜底的 `compileBuiltinJSONGrammar`。
- `responseFormatCacheKey`（按 schema/grammar/标签字符串取键）命中时只 `reset()` 复用 matcher，跳过编译；`TokenizerInfo` 与 `GrammarCompiler` 绑定词表、管线级持久，只建一次。
- 约束的执行是采样链路的**第 0 步**：每 token 先 `getNextTokenBitmask()` 取 CPU 位掩码（\(\lceil V/32 \rceil\) 个 int32），拷到 GPU 后用 `apply_bitmask_inplace` 就地屏蔽 logits，排在 LogitProcessor、logit_bias、惩罚之前；采样后 `acceptToken()` 推进状态机，语法走完后掩码只放行停止 token，生成以 `stop` 自然收尾。
- 开销可观测：`usage.extra.grammar_init_s`（初始化/编译耗时）与 `grammar_per_token_s`（每 token 掩码开销）是 WebLLM 私有扩展，且**只在 type 为 `json_object` 或 `grammar` 时输出**（不含 structural_tag）。
- 输出语法合法性由机制保证，但语义合理性、以及 `finish_reason: "length"` 时的截断风险，仍需调用方兜底。

## 7. 下一步学习建议

本讲只讲了「全程约束」。下一讲 **u6-l4（Structural Tag 工具调用）** 讲「分段约束」：用自定义标签把输出切成自由文本区与受约束 JSON 区，只在标签内应用语法——这正好解释了 `structural_tag` 这个本讲一带而过的 type，以及它为什么比全程 JSON 约束更快。建议带着两个问题去读：

1. `compileStructuralTag` 编译出的状态机如何在「无约束区」与「约束区」之间切换？（对照本讲的 `getNextTokenBitmask` 每步掩码。）
2. 既然 structural_tag 也是约束类型，为什么 `engine.ts` 的 `usedGrammar` 判定不含它？去 `examples/structural-tag-tool-use` 实测它的 `usage.extra` 里有没有 grammar 字段。

另外推荐通读 [examples/json-schema/src/json_schema.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/json-schema/src/json_schema.ts) 中的 `ebnfGrammarExample`（L228-L279）——它展示了跳过 JSON Schema、直接手写 EBNF 语法的第三条路线，是把本讲知识从「用 schema」推进到「写语法」的最佳入口。
