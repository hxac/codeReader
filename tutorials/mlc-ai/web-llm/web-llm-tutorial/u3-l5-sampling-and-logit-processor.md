# 采样控制：GenerationConfig、惩罚与 LogitProcessor

## 1. 本讲目标

上一讲（u3-l4）我们看清了 `decodeStep` 的自回归循环与终止条件，但刻意跳过了一个关键问题：**每一步前向之后，模型到底是如何从一堆 logits 里「挑」出下一个 token 的？** 本讲就来补上这块拼图。学完本讲，你应该能够：

1. 说出 `GenerationConfig` 里 `temperature`、`top_p`、`frequency_penalty`、`presence_penalty`、`logit_bias` 等字段从请求到采样核的完整数据流；
2. 解释 `postInitAndCheckGenerationConfigValues` 在引擎层做了哪些校验与默认值补齐，以及管线层为什么还要再校验一遍；
3. 读懂 `sampleTokenFromLogits` 中「grammar 掩码 → LogitProcessor → logit_bias → 惩罚 → softmax → top-p 采样」这条 PackedFunc 链；
4. 编写并注册自己的 `LogitProcessor`，在每步解码时干预 logits（例如禁止某个 token 被采样），并理解它与 `seed` 的协作。

## 2. 前置知识

本讲要用到几个概率与采样的基础概念，先用直觉解释一遍。

**logits 是什么。** 模型每前向一次，会对词表里的每个 token 输出一个实数分数，称为 logit。分数越高代表模型越「想」输出这个 token，但它还不是概率——可能是 -3.2、也可能是 17.8，甚至全为负。

**softmax：从分数到概率。** softmax 把任意实数分数归一化成一组和为 1 的概率。引入温度 \( T \) 后公式为：

\[ p_i = \frac{\exp(l_i / T)}{\sum_{j} \exp(l_j / T)} \]

- \( T \to 0^+ \)：分布越陡，大概率 token 几乎吞掉全部概率，输出趋近「贪心解码」（总选最大分）；
- \( T \) 越大：分布越平，小概率 token 也有机会被抽中，输出更「放飞」。

**top-p（核采样）。** 即使温度调低，长尾里仍可能藏着病句 token。top-p 只保留「按概率从大到小排序后，累积概率首次达到 p 的最小前缀」，在这个小集合里重新归一化再抽样。\( p = 0.9 \) 就是「只在累积 90% 概率的核心词里抽」。

**惩罚项。** 自回归模型容易复读机。OpenAI 风格的惩罚是对「已经出现过的 token」的 logit 做削减，惯例形式为：

\[ l_i \leftarrow l_i - c_{\text{freq}} \cdot n_i - c_{\text{pres}} \cdot \mathbb{1}[n_i > 0] \]

其中 \( n_i \) 是 token \( i \) 已出现的次数。frequency_penalty 按次数线性加压，presence_penalty 只要出现过就罚。WebLLM 还支持 MLC 特有的 repetition_penalty。具体削减公式由模型库里的 GPU 内核执行，TypeScript 侧只负责统计出现次数并传参（见 4.2.3）。

**logit_bias。** 请求里传一个 `{token_id: bias}` 的字典，采样前直接给指定 token 的 logit 加偏置，正数鼓励、负数抑制。适合「硬控」个别 token。

**seed 与可复现性。** 采样要用随机数（从均匀分布抽一个点再映射到 token）。WebLLM 的随机数来自 tvmjs 运行时的 RNG，`seed` 相同、输入相同，则整条随机数序列相同，输出逐 token 一致。

**PackedFunc。** u3-l1 讲过：模型库（wasm）导出的函数经 tvmjs 包装成 `PackedFunc`，TypeScript 拿到的只是句柄，真正计算在 WebGPU 上。本讲的采样链就是一串 PackedFunc 的接力。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| `src/config.ts` | `GenerationConfig` 接口、`postInitAndCheckGenerationConfigValues` 校验函数、`MLCEngineConfig.logitProcessorRegistry` |
| `src/engine.ts` | 请求字段摘入 `genConfig`、校验调用时机、seed 设置与复位、LogitProcessor 注册表的查找与注入 |
| `src/llm_chat.ts` | `sampleTokenFromLogits`（本讲主角）、采样族 PackedFunc 绑定、`appearedTokensFreq` 统计、LogitProcessor 的三个回调调用点 |
| `src/types.ts` | `LogitProcessor` 接口定义、延迟分解字段 `logitProcessorTime` |
| `src/web_worker.ts` | Worker 场景下 LogitProcessor 的注册方式与限制 |
| `examples/logit-processor/` | LogitProcessor 官方示例（含 worker 与非 worker 两种用法、低层 API） |
| `examples/seed-to-reproduce/` | seed 固定复现实验的官方示例 |

## 4. 核心概念与源码讲解

本讲按数据流顺序拆成三个最小模块：**先在引擎层把请求参数装进 `GenerationConfig` 并校验**（4.1），**再进入管线层的采样 PackedFunc 链**（4.2），**最后看用户可插拔的 LogitProcessor 在链路的哪一步被调用**（4.3）。

### 4.1 模块一：GenerationConfig 与 postInitAndCheckGenerationConfigValues

#### 4.1.1 概念说明

一次生成请求里，与「采样行为」相关的参数（temperature、top_p、各种惩罚、logit_bias 等）不该散落在代码各处，WebLLM 把它们摘进一个 `GenerationConfig` 对象，随 prefill/decode 一路传到管线。它解决两个问题：

1. **参数分层**：`ChatConfig`（来自模型仓库的 `mlc-chat-config.json`）提供模型级默认值；`GenerationConfig` 是本次请求的覆盖值，字段全部可选，未指定就回退到 ChatConfig。u1-l4 已讲过这层关系。
2. **尽早失败**：非法参数（比如 `top_p = 0`）应该在发请求时就报错，而不是等 GPU 算到一半才炸。`postInitAndCheckGenerationConfigValues` 就是这道门卫。

注意：`postInitAndCheckGenerationConfigValues` 并未从库入口 `src/index.ts` 导出（回顾 u1-l3 的 barrel file 分类，它属于内部函数），所以只能通过发起请求间接触发。

#### 4.1.2 核心流程

```text
chatCompletion(request)
  ├─ 1. 把 request 中采样相关字段摘入 genConfig          (engine.ts)
  ├─ 2. 非流式：_generate() 入口调用校验函数              (engine.ts:467)
  │     流式：asyncGenerate() 预处理阶段调用校验函数       (engine.ts:525)
  │     校验内容：
  │     a. frequency_penalty / presence_penalty ∈ [-2, 2]
  │     b. repetition_penalty > 0、max_tokens > 0
  │     c. top_p ∈ (0, 1]、temperature >= 0
  │     d. 只设其一的 penalty，另一个补默认 0.0
  │     e. logit_bias 值 ∈ [-100, 100] 且键必须是数字串
  │     f. top_logprobs 依赖 logprobs=true，且 ∈ [0, 5]
  └─ 3. genConfig 随 prefill/decode 传入管线
        管线在 sampleTokenFromLogits 里再做一次范围复核（4.2）
```

一个重要细节：校验函数既做「检查」也做「补默认值」（post-init 的含义）——只写一个 penalty 时会把另一个补成 0.0，只开 `logprobs` 不写 `top_logprobs` 时补 0。

#### 4.1.3 源码精读

`GenerationConfig` 的字段清单——注意哪些是 MLC 特有、哪些是 OpenAI 兼容：

[文件路径:src/config.ts:L145-L165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L145-L165) 定义了 `GenerationConfig`：`repetition_penalty`/`ignore_eos` 仅 MLC 使用；`top_p`/`temperature` 两边共用；`max_tokens`、双 penalty、`logit_bias`、`logprobs` 等为 OpenAI 风格字段；全部可选，注释明确「未指定时沿用 reload 时初始化的 ChatConfig」。

[文件路径:src/config.ts:L167-L249](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L249) 是校验函数本体。几个值得精读的点：

- L170-L173 定义 `_hasValue`：注释特意说明不能用 `if (value)`，因为 `value` 为 0 时会被当成 falsy 而误判「未设置」——这是采样参数校验里最容易踩的坑（0 是合法的 temperature 吗？不是，但 0 是合法的 penalty）；
- L174-L185：两个 penalty 的范围检查用 `config.frequency_penalty &&` 做前置，意味着 **0 会跳过检查**（0 本来就合法）；
- L192：`top_p` 用 `<= 0` 和 `> 1` 判非法，即 0 非法、1 合法；
- L198-L214：双 penalty 互补默认 0.0，并打 warning；
- L216-L231：`logit_bias` 逐键检查值域 [-100, 100] 与键的可解析性（`parseInt` 失败抛 `InvalidNumberStringError`）；
- L233-L248：`top_logprobs` 必须搭配 `logprobs: true`（`DependencyError`），范围 [0, 5]。

引擎侧的组装与调用点：

[文件路径:src/engine.ts:L810-L825](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L810-L825) 把 `ChatCompletionRequest` 中的采样字段逐一摘入 `genConfig`（注意 `enable_thinking`、`enable_latency_breakdown` 来自 `extra_body`）。

[文件路径:src/engine.ts:L457-L479](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L457-L479) 非流式路径 `_generate`：L466-L468 在进入 prefill **之前**调用校验，失败则请求直接抛错，不占用 GPU；随后 L471-L477 就是 u3-l4 讲过的 `while (!pipeline.stopped())` 解码循环。

[文件路径:src/engine.ts:L519-L532](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L519-L532) 流式路径 `asyncGenerate` 在 try 块里做同样校验（L525），并顺带处理 `seed`（L526-L528：请求带 seed 就调 `pipeline.setSeed`）。流式生成器末尾（L640-L643）还会用 `Date.now()` 把 seed 复位，避免影响后续请求——非流式路径同理（[文件路径:src/engine.ts:L959](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L959)），非流式的 seed 设置则在 [文件路径:src/engine.ts:L843-L847](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L843-L847)。

#### 4.1.4 代码实践：非法参数五连测

这是一个纯「源码验证型」实践，目标是把 4.1.3 读到的校验规则逐条兑现成真实报错。

1. **实践目标**：确认校验函数确实在请求入口生效，并能按错误类型反推是哪条规则。
2. **操作步骤**（基于 examples/get-started 的工程骨架，模型任选一个已加载的小模型）：

   ````ts
   const badRequests = [
     { messages: [{ role: "user", content: "hi" }], temperature: -1 },
     { messages: [{ role: "user", content: "hi" }], top_p: 0 },
     { messages: [{ role: "user", content: "hi" }], frequency_penalty: 3 },
     { messages: [{ role: "user", content: "hi" }], logit_bias: { abc: 1 } },
     { messages: [{ role: "user", content: "hi" }], top_logprobs: 3 }, // 缺 logprobs
   ] as webllm.ChatCompletionRequest[];

   for (const req of badRequests) {
     try {
       await engine.chat.completions.create(req);
     } catch (e) {
       console.log(e?.constructor?.name, ": ", (e as Error).message);
     }
   }
   ````
3. **需要观察的现象**：五次请求都不产生任何 token，分别在进入 prefill 前抛错。
4. **预期结果**（对照 [文件路径:src/config.ts:L167-L249](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L249)：依次是 `NonNegativeError`（temperature）、`RangeError`（top_p）、`RangeError`（frequency_penalty）、`InvalidNumberStringError`（logit_bias 键）、`DependencyError`（top_logprobs 依赖 logprobs）。错误类型定义在 `src/error.ts`，u7-l1 会系统梳理。
5. 具体报错文案与错误类名的对应关系「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_hasValue` 不能直接写成 `if (value)`？举一个会被误判的采样参数。

答案：因为 `0` 是 falsy。若用 `if (value)` 判断「用户是否设置了该值」，`frequency_penalty: 0`（显式设置为 0，合法）会被误当成「未设置」，从而错误地回退到 ChatConfig 默认值，改变采样行为。

**练习 2**：请求里只传了 `frequency_penalty: 1.5`，最终参与采样计算的 `presence_penalty` 是多少？在哪个文件被补上？

答案：0.0。引擎层校验函数（src/config.ts L198-L205）和管线层取值函数（src/llm_chat.ts L1651-L1657）都会补这个默认值，两处逻辑一致，属于双重保险。

**练习 3**：`temperature: 0` 会被校验函数拒绝吗？

答案：会。L195-L197 用 `_hasValue`（0 有值）加 `< 0` 判断，0 本身不触发 `NonNegativeError`；但管线层采样时会把 temperature 钳到 `Math.max(1e-6, temperature)`（src/llm_chat.ts L1898）防止除零，所以 `temperature: 0` 合法且等价于极低温度的近贪心解码。

### 4.2 模块二：采样 PackedFunc 链——sampleTokenFromLogits

#### 4.2.1 概念说明

`LLMChatPipeline.sampleTokenFromLogits` 是本讲的主角：它接收 GPU 上的一枚 logits 张量，返回一个采样出的 token id。prefill 的最后一步（产出第一个回复 token）和每轮 decode 都要调它。它解决的问题是：**把 OpenAI 风格的一堆采样参数，按正确顺序作用到 logits 上，再走 softmax + top-p 抽样**。顺序很重要：

```text
logits（GPU）
  → 0. grammar 掩码（仅结构化输出时）     在 GPU 上
  → 1. LogitProcessor（仅注册时）          在 CPU 上，用户代码
  → 2. logit_bias                          在 GPU 上
  → 3. 双 penalty + repetition_penalty     在 GPU 上
  → 4. softmaxWithTemperature → argsort → sampleWithTopP   在 GPU 上
  → 返回 token id
```

为什么 LogitProcessor 排在 logit_bias 前面？因为用户处理器可能覆盖任何位置的 logit，而 logit_bias 是「最后声明的精修」；文档注释（见 4.3.3）明确了这个先后约定。

#### 4.2.2 核心流程

`sampleTokenFromLogits(logitsOnGPU, genConfig)` 的六步（源码注释自编号 0-6）：

1. **取值**：从 `this.config`（ChatConfig）取默认 temperature/top_p/惩罚，再用 genConfig 覆盖；top_p 未设则默认 1.0，双 penalty 缺一则补 0.0；随后**复核范围**。
2. **grammar 掩码**：若 `response_format` 是 json_object/grammar/structural_tag，取 GrammarMatcher 的下一 token 位掩码，用 `apply_bitmask_inplace` 把非法 token 的 logit 打成 `-inf`（细节在 u6-l3，本讲只记住它排在最前）。
3. **LogitProcessor**：若有注册的处理器，把 logits 拷到 CPU，交给用户函数处理后再拷回 GPU。
4. **logit_bias**：构造 tokenIds/bias 两个小数组上传 GPU，调 `apply_logit_bias_inplace`。
5. **惩罚**：把本轮已出现 token 的 id 与频次上传 GPU，连同三个惩罚系数调 `apply_penalty_inplace`。
6. **采样**：temperature 钳到至少 1e-6；`softmax_with_temperature` 把 logits 变概率；`argsort_probs` 降序排序；`tvm.uniform` 抽一个 [0,1) 均匀随机数；`sample_with_top_p` 在排序后的累积分布上完成 top-p 抽样；拷回 CPU 得到 token id。

之后还有两个收尾：把 token 回喂给 `logitProcessor.processSampledToken`（第 5 步注释）和 grammar matcher 的 `acceptToken`（第 6 步注释）。

**top-p 抽样的原理**（`sample_with_top_p` 内核做的事）：概率降序排列后累积求和，首个达到阈值 p 的位置之前构成候选集，再用那个均匀随机数在候选集内按比例抽取。源码 L1636-L1637 的 TODO 注释提到 relax 里 top-p 掩码用 `<` 而非 `<=`，因此 top_p=1.0 的边界行为值得留意；L1916 把 top_p 钳到至少 1e-5 也是防御性处理。

#### 4.2.3 源码精读

先看采样族 PackedFunc 在管线构造时如何绑定（u3-l1 讲过 `getRequiredVMFunctionByName` 按模型 ABI 查函数）：

[文件路径:src/llm_chat.ts:L300-L335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L300-L335) 依次绑定 `apply_bitmask_inplace`、`apply_penalty_inplace`、`apply_logit_bias_inplace`、`softmax_with_temperature`、`sample_with_top_p`、`argsort_probs` 六个函数句柄。这些函数名由模型库（wasm）导出，真正实现是 WebGPU/TVM 编出的内核，TypeScript 只做编排。

函数入口的取值与复核：

[文件路径:src/llm_chat.ts:L1609-L1698](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1609-L1698) 中，L1619-L1623 从 `this.config` 取模型默认值；L1629-L1676 用 genConfig 覆盖并补默认（与 4.1 的引擎层逻辑镜像，因为低层 API `forwardTokensAndSample` 不带 genConfig 也会走到这里）；L1677-L1698 对最终值再复核一遍——这解释了「为什么校验了两遍」：引擎层管 OpenAI 请求，管线层管包括低层 API 在内的所有入口。

惩罚项的数据准备：

[文件路径:src/llm_chat.ts:L115](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L115) 声明 `appearedTokensFreq: Map<number, number>`，统计**本轮生成**中每个 token 出现的次数；[文件路径:src/llm_chat.ts:L739-L741](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L739-L741) 在每轮 prefill 前清空（即惩罚只作用于「当前这条回复已生成的 token」，不含 prompt，也不跨轮）；[文件路径:src/llm_chat.ts:L1003-L1012](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1003-L1012) 在 `processNextToken` 把新 token 计入频次表。

[文件路径:src/llm_chat.ts:L1826-L1890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1826-L1890) 惩罚应用块：只有任一惩罚非默认（L1827-L1831 的条件：frequency != 0 或 presence != 0 或 repetition != 1.0）且确有已出现 token 时才执行；L1843-L1847 把三个系数打包成 `[1,3]` 张量，连同 tokenIds/tokenCnt 一起传给 `fapplyPenalty`。注意削减公式在 wasm 内核里，TypeScript 侧看不到。

采样核心：

[文件路径:src/llm_chat.ts:L1894-L1951](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1894-L1951) L1898 `temperature = Math.max(1e-6, temperature)` 防除零；L1910-L1913 调 `fsoftmaxWithTemperature` 得到概率张量；L1916-L1927 `fargsortProbs` 排序、`this.tvm.uniform` 生成随机数、准备 top-p 阈值张量；L1929-L1935 `fsampleWithTopP` 完成抽样；L1947-L1951 拷回 CPU 并断言 token 非负。随机数由 tvmjs 的 RNG 提供——这正是 `setSeed` 能固定输出的原因（[文件路径:src/llm_chat.ts:L670-L672](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L670-L672) 的 `setSeed` 直接转发 `tvm.setSeed`）。

调用点与低层 API：

[文件路径:src/llm_chat.ts:L887-L900](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L887-L900) prefillStep 末尾对整段 prompt 的 logits 采样出第一个回复 token（u3-l3 讲过「只取最后一个位置」）；[文件路径:src/llm_chat.ts:L902-L936](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L902-L936) decodeStep L926 对单步 logits 采样。此外 [文件路径:src/llm_chat.ts:L2137-L2186](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2137-L2186) 的 `forwardTokensAndSample`（examples/logit-processor 用到的低层 API）在 L2168 复用同一个采样函数——所以本讲所学对低层 API 同样成立。

#### 4.2.4 代码实践：亲眼看分布随温度变化

1. **实践目标**：用同一 seed、同一 prompt，对比不同 temperature 下输出与 token 概率分布的差异，把 softmax 公式变成可观察的现象。
2. **操作步骤**：基于 examples/get-started 工程改写（模型建议用 1-2B 级小模型节省时间）：

   ````ts
   const base: webllm.ChatCompletionRequest = {
     stream: false,
     messages: [{ role: "user", content: "Write a creative Haiku about Pittsburgh" }],
     n: 2,
     max_tokens: 64,
     seed: 42,
     logprobs: true,   // 返回每个 token 的 logprob，观察分布
     top_logprobs: 3,
   };
   for (const temperature of [0.05, 1.2]) {
     const reply = await engine.chat.completions.create({ ...base, temperature });
     for (const c of reply.choices) {
       console.log(`T=${temperature} choice=${c.index}\n${c.message.content}`);
     }
     // logprobs 在 reply.choices[0].logprobs 中
   }
   ````
3. **需要观察的现象**：低温度下两次 choice 与多次重跑的差异极小、top token 的 logprob 接近 0（概率接近 1）；高温度下两个 choice 明显不同、top-3 logprob 拉开梯度。参考官方示例 [文件路径:examples/seed-to-reproduce/src/seed.ts:L27-L58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/seed-to-reproduce/src/seed.ts#L27-L58)——它用 `temperature: 1.2` + `seed: 42` + `n: 3` 并逐 choice 比对两次请求的文本一致性（L48-L58 有严格的相等断言）。
4. **预期结果**：同 seed 同参数两次请求输出完全一致；固定 seed 后改 temperature，输出改变。
5. 三个温度档下 top-3 token 的具体 logprob 数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：惩罚为什么只作用于「本轮生成的 token」而不看 prompt 里的 token？从源码哪里可以看出？

答案：频次表 `appearedTokensFreq` 在每轮 prefill 的清理块中被 `clear()`（src/llm_chat.ts L739-L741），且只在 `processNextToken` 里随 `outputIds` 增长（L1003-L1012），prefill 的 prompt token 从不写入该表。效果是惩罚只防「本条回复内的复读」。

**练习 2**：`sample_with_top_p` 需要排序吗？为什么 `argsort_probs` 是独立的一个内核？

答案：需要——top-p 要按概率降序累积求和确定候选集。`softmax_with_temperature` 产概率、`argsort_probs` 排序、`sample_with_top_p` 抽样三者解耦，每个都是模型库导出的独立 PackedFunc（src/llm_chat.ts L318-L335 的绑定顺序），便于内核各自优化与复用。

**练习 3**：用户同时注册了 LogitProcessor 并传了 `logit_bias`，两者谁先生效？

答案：LogitProcessor 先（第 1 步，CPU），logit_bias 后（第 2 步，GPU）。`src/types.ts` L34-L37 与 `src/openai_api_protocols/chat_completion.ts` L179-L181 的注释都写明「logit_bias 在 processLogits() 之后应用」。

### 4.3 模块三：LogitProcessor 扩展点

#### 4.3.1 概念说明

采样链里所有参数（temperature、top_p、bias、penalty）都是「声明式」的：表达力受限于预定义字段。如果需求是「token A 出现超过 3 次后必须禁止 token B」这类**有状态、任意逻辑**的干预，就需要一个逃生口。`LogitProcessor` 就是这个扩展点：一个由用户实现的接口，在**每一步采样之前**拿到完整的 logits 数组（词表大小的 Float32Array），返回修改后的数组。它有状态（可以记历史），跨越整条生成过程，`resetChat` 时被重置。

它的设计权衡是**灵活换性能**：logits 平时在 GPU 上，而用户代码是 TypeScript，因此每步都要经历 GPU→CPU→用户函数→CPU→GPU 的往返（4.3.3 的源码会看到这段搬运，以及专门的耗时埋点）。

#### 4.3.2 核心流程

```text
注册：
  new MyLogitProcessor()
    → logitProcessorRegistry: Map<modelId, LogitProcessor>
    → CreateMLCEngine(modelId, { logitProcessorRegistry })     (主线程引擎)
       或 handler.setLogitProcessorRegistry(registry)          (Worker 内)
  engine.reload()
    → reloadInternal 按当前 modelId 查 registry
    → new LLMChatPipeline(..., logitProcessor)                 挂到管线上

运行（每步采样内）：
  sampleTokenFromLogits 第 1 步:
    logits GPU → CPU (updateLogitsOnCPU)
    → logitsOnCPU.toArray() 得 Float32Array
    → logitProcessor.processLogits(array)      ← 用户逻辑在此执行
    → 结果写回 GPU 张量与 CPU 副本
  采样完成后:
    logitProcessor.processSampledToken(token)  ← 通知用户采样结果
  engine.resetChat():
    logitProcessor.resetState()                ← 清理用户状态
```

#### 4.3.3 源码精读

接口契约：

[文件路径:src/types.ts:L34-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L34-L57) 定义 `LogitProcessor` 三个方法：`processLogits`（采样前处理 logits，入参出参都是 Float32Array，下标即 token id）；`processSampledToken`（采样出 token 后回调，用于维护内部状态）；`resetState`（`resetChat` 时清理状态）。注释明确它与 logit_bias 的先后关系。

注册与注入链（主线程引擎）：

[文件路径:src/config.ts:L130-L135](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L130-L135) `MLCEngineConfig.logitProcessorRegistry` 字段（注释说明仅 `MLCEngine` 生效，Worker 场景见下）；[文件路径:src/engine.ts:L184-L187](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L184-L187) setter；[文件路径:src/engine.ts:L252](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L252) `reloadInternal` 一开始就按 modelId 取处理器——**注册表按模型索引，一个处理器只绑定一个模型**；[文件路径:src/engine.ts:L406-L411](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L406-L411) 作为构造参数传入 `LLMChatPipeline`；[文件路径:src/llm_chat.ts:L183-L189](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L183-L189) 管线存为私有字段。

三个回调的调用点：

[文件路径:src/llm_chat.ts:L1750-L1778](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1750-L1778) 采样链第 1 步：L1754 `updateLogitsOnCPU` 把 GPU logits 拷到 CPU 常驻张量，`await device.sync()` 等拷贝完成；L1763-L1766 `toArray()` 转成 Float32Array 交给用户函数，返回值同时写回 GPU（`logitsOnGPU.copyFrom`）与 CPU 副本；L1770-L1777 若开了 `enable_latency_breakdown`，把这段用户代码耗时计入 `curRoundLatencyBreakdown.logitProcessorTime`（字段见 [文件路径:src/types.ts:L256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L256)，u7-l3 详述延迟分解）。

[文件路径:src/llm_chat.ts:L1962-L1966](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1962-L1966) 抽样完成后（第 5 步注释）回调 `processSampledToken(sampledToken)`——注意它在 grammar matcher 的 `acceptToken`（L1974）之前，用户处理器是最先知道采样结果的。

[文件路径:src/llm_chat.ts:L530-L540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540) `resetChat` 的 L538 调 `resetState()`，与清 KV cache、清对话历史同步。

Worker 场景的关键限制：

[文件路径:src/web_worker.ts:L456-L459](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L456-L459) 若把 `logitProcessorRegistry` 塞进 `CreateWebWorkerMLCEngine` 的 engineConfig 会被**忽略并告警**——函数无法跨线程序列化，处理器必须在 Worker 脚本内创建，经 [文件路径:src/web_worker.ts:L105-L108](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L105-L108) 的 `handler.setLogitProcessorRegistry` 注册到 Worker 内部的引擎上（u5-l2 会展开 Worker 架构）。

官方示例：

[文件路径:examples/logit-processor/src/my_logit_processor.ts:L4-L21](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/logit-processor/src/my_logit_processor.ts#L4-L21) 官方处理器：`processLogits` 里 `logits[0] = 100.0`——把 token 0 的分数抬到碾压级，演示「强制总采样 token 0」；`processSampledToken` 记录 token 序列展示有状态能力；`resetState` 清空。注册方式见 [文件路径:examples/logit-processor/src/logit_processor.ts:L19-L39](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/logit-processor/src/logit_processor.ts#L19-L39)：L20-L22 建 Map 并按 modelId（示例为 `phi-2-q4f32_1-MLC`）注册；L27-L33 Worker 分支（处理器在 worker.ts 里注册）、L34-L38 主线程分支；示例还演示了低层 API `forwardTokensAndSample` 手动驱动自回归循环。

#### 4.3.4 代码实践：从「强制」到「禁止」

1. **实践目标**：跑通官方示例后，把「抬分」改写成「封禁」，验证 `-Infinity` 能把 token 从候选集中彻底除名。
2. **操作步骤**：

   ```bash
   cd examples/logit-processor
   npm install
   npm start        # 打开 http://localhost:8888，观察控制台
   ```
   先确认控制台里 `processSampledToken: N` 递增、且低层 API 循环输出的 token 全是 0（被抬分的 token 独占概率）。然后把 `my_logit_processor.ts` 改成禁止版：

   ````ts
   export class BanTokenLogitProcessor implements webllm.LogitProcessor {
     constructor(private bannedTokens: Array<number>) {}
     processLogits(logits: Float32Array): Float32Array {
       for (const id of this.bannedTokens) {
         logits[id] = -Infinity; // softmax 后概率为 0，永不被采样
       }
       return logits;
     }
     processSampledToken(token: number): void {}
     resetState(): void {}
   }
   // 注册处：logitProcessorRegistry.set(modelId, new BanTokenLogitProcessor([0]));
   ````
   再调用 `engine.resetChat()` 后跑一遍，对比封禁前后低层 API 输出的 token 序列。
3. **需要观察的现象**：抬分版每个位置都采样出 token 0；封禁版 token 0 从此绝迹；`resetChat()` 触发 `resetState` 日志。
4. **预期结果**：与 [文件路径:examples/logit-processor/README.md:L11-L14](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/logit-processor/README.md#L11-L14) 描述一致：抬分后「总是采样 token 0」；封禁后 token 0 的出现次数为 0。
5. 若在无 WebGPU 环境运行，行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LogitProcessor 的 `processLogits` 每步都会造成 GPU↔CPU 往返？有办法量化这笔开销吗？

答案：用户逻辑是 JS，只能操作 CPU 数组，而 logits 在 GPU 显存，因此 `updateLogitsOnCPU` + `toArray()` 拷下来、`copyFrom` 拷回去（src/llm_chat.ts L1750-L1778）。请求里开 `enable_latency_breakdown: true` 后，耗时记入 `curRoundLatencyBreakdown.logitProcessorTime`，可从 usage.extra 精确读出（u7-l3 专题讲解）。

**练习 2**：想在 Worker 架构下用 LogitProcessor，注册代码应该写在哪？为什么不能写在主线程？

答案：写在 Worker 脚本内：创建处理器实例后调 `handler.setLogitProcessorRegistry(registry)`（示例 worker.ts 的做法）。因为函数无法 structured-clone 序列化跨线程传递，主线程传的 registry 会被忽略并告警（src/web_worker.ts L456-L459）。

**练习 3**：设计「同一个 token 连续出现 2 次后禁用它 5 步」的处理器，需要用到接口的哪几个方法？

答案：三个都要：`processSampledToken` 维护「最近采样序列」状态；`processLogits` 根据状态对命中条件的 token 写 `-Infinity`；`resetState` 在新会话开始时清空状态。这正是该接口被设计成有状态三件套的原因。

## 5. 综合实践

把本讲三个模块串成一个「违禁词过滤器 + 可复现实验」小项目（基于 examples/get-started 工程骨架，任选一个已加载的小模型）：

1. **实现处理器**：写 `ForbiddenTokenLogitProcessor`，构造参数为 `bannedTokenIds: Array<number>`，在 `processLogits` 中把这些 id 的 logit 置为 `-Infinity`（参照 4.3.4）；`processSampledToken` 里维护「被封禁 token 尝试被压制的次数」计数；`resetState` 清零。
2. **拿到目标词的 token id**：WebLLM 未公开 tokenizer 实例，两条诚实路径任选——(a) 从模型记录（u1-l4）指向的 HuggingFace 仓库下载 tokenizer 文件，用 HF tokenizer 库离线查出目标词（如 "Pittsburgh"）的一个或多个 token id，硬编码进处理器；(b) 先跑一轮探测：让模型输出目标词，用 4.2.4 的 `logprobs` 观察该词对应 token 的字符串形式，再结合 tokenizer 文件反查 id。多候选 id（词首带不带空格通常是不同 id）建议一起封禁。
3. **验证封禁**：问一个必然引出目标词的问题（如 "Tell me about Pittsburgh"），确认整段回复中该词不再出现；对照实验：注释掉处理器再问一次，确认该词本来会出现。
4. **seed 复现实验**：参照 [文件路径:examples/seed-to-reproduce/src/seed.ts:L27-L58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/seed-to-reproduce/src/seed.ts#L27-L58)，固定 `seed: 42`、`temperature: 1.2`，同一请求连发三次，逐 choice 比对文本是否完全一致；再换 `seed: 43` 各发一次，确认输出改变。
5. **记录与解释**：把「三次同 seed 输出是否一致」「封禁前后目标词出现次数」「延迟分解中的 logitProcessorTime（若开启 enable_latency_breakdown）」写成实验笔记；无法在本地跑 WebGPU 的条目标注「待本地验证」。

## 6. 本讲小结

- 采样参数走两层校验：引擎层 `postInitAndCheckGenerationConfigValues`（src/config.ts L167-L249）在生成前拦非法值并补默认；管线层在 `sampleTokenFromLogits` 取值后复核，覆盖低层 API 入口。
- `sampleTokenFromLogits`（src/llm_chat.ts L1609-L1989）按固定顺序施加干预：grammar 掩码 → LogitProcessor（CPU）→ logit_bias → 三惩罚 → `softmax_with_temperature` → `argsort_probs` → `sample_with_top_p`，全部经 PackedFunc 在 GPU 上执行。
- 惩罚只作用于本轮已生成的 token：`appearedTokensFreq` 每轮 prefill 清空、`processNextToken` 递增，削减公式在 wasm 内核内。
- temperature 被钳到至少 1e-6 防除零，top_p 至少 1e-5；随机数来自 tvmjs RNG，`seed` 经 `pipeline.setSeed` 固定整条随机数序列，请求结束后用 `Date.now()` 复位。
- `LogitProcessor` 是有状态三件套（`processLogits`/`processSampledToken`/`resetState`），按 modelId 注册、随管线构造注入，每步采样付出一次 GPU↔CPU 往返的代价；Worker 场景必须在 Worker 脚本内注册。

## 7. 下一步学习建议

本讲结束后，推理管线（第三单元）的主干——初始化、模板编码、prefill、decode、采样——已经完整。下一步：

1. 若想看「排在采样链最前面的 grammar 掩码」如何保证 JSON 合法，进入 u6-l3（JSON Mode 与结构化输出）与 u6-l4（Structural Tag）；
2. 若想量化本讲提到的 `logitProcessorTime`、`sampleTime` 等埋点，进入 u7-l3（延迟分解与运行统计）；
3. 若想在 Worker 架构中落地 LogitProcessor 的注册方式，进入 u5-l1/u5-l2（Web Worker 架构与 Handler 源码）；
4. 建议顺带阅读 `src/llm_chat.ts` 的 `forwardTokensAndSample`（L2137-L2186）与 examples/logit-processor 的完整代码，体会「低层 API + 自定义采样干预」组合出的完全控制力。
