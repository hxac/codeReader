# 系统提示 KV 缓存与流式

## 1. 本讲目标

本讲是「特性与扩展」单元的第二篇，聚焦两个面向生产部署的性能与体验特性：

- **系统提示 KV 缓存（system prompt KV cache）**：把固定不变的系统提示（system prompt）的 KV 缓存预先算好、缓存起来，后续请求命中即跳过 prefill，降低首 token 延迟（TTFT）。
- **流式输出（streaming）**：让模型在生成过程中逐块（chunk）把文本推给调用方，而不是等整段生成完才返回。

学完本讲，你应当能够：

1. 说清楚系统提示 KV 缓存的「缓存键」「保存」「捕获」「恢复」四件事分别发生在代码哪里。
2. 解释为什么恢复时要复用「N-1」个 token 而不是 N 个，以及为什么缓存键里要带上 LoRA 名字。
3. 画出流式输出在解码主循环里的插入顺序，并理解取消（cancel）、停止串（stop string）、节流（streamInterval）三者如何协同。
4. 理解 `StreamChannelFinalizer` 这个 RAII 守卫为什么能保证消费者「永远不会死等」。

## 2. 前置知识

本讲建立在你已经学过下面两篇之上，不重复其内容，只承接：

- **u5-l5 KV 缓存与混合（Mamba）缓存管理**：你已经知道 `HybridCacheManager` 用线性 KV 缓存（attention 层）+ recurrent/conv 状态（Mamba 层）管理自回归步进的中间状态，并已经见过它的 `captureKVCache` / `restoreKVCache` 接口签名。本讲会真正打开这两个接口，讲它们如何被系统提示缓存复用。
- **u5-l2 请求/响应数据模型与流式**：你已经知道请求里有 `streamChannels` 字段、响应有 `FinishReason` 终止枚举、流式有 `StreamChannel` 这条 MPSC（多生产者单消费者）增量通道。本讲会从「接口」深入到「实现」：解码主循环到底在什么时候、以什么顺序推送 chunk。

此外需要两个背景概念：

- **prefill（预填充）**：把整条 prompt 一次性喂给模型，算出每个位置的 KV 缓存并产出第一个 token。它是 TTFT 的主要来源，也是系统提示缓存想节省的开销。
- **decode（解码）**：逐 token 自回归地生成后续 token，每步读历史 KV、写当前 KV。
- **RAII（Resource Acquisition Is Initialization）**：C++ 中「把资源生命周期绑到对象生命周期」的惯用法——构造时获取，析构时释放。本讲的 `StreamChannelFinalizer` 靠它保证任何退出路径下消费者都能被唤醒。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `cpp/runtime/state/systemPromptKVCache.h` | 系统提示缓存的数据结构 `SystemPromptKVCache`、缓存键类型与键构造函数。 |
| `cpp/runtime/llmInferenceRuntime.cpp` | 保存与恢复的总编排：`handleRequest` 触发保存、`genAndSaveSystemPromptKVCache` 预生成并捕获、`setUpForPrefillExecution` 在 prefill 前尝试恢复、解码主循环里插入流式钩子。 |
| `cpp/runtime/hybridCacheManager.cpp` | `captureKVCache` / `restoreKVCache` / `captureRecurrentStates` / `captureConvStates` 的真正实现（GPU 上的批量拷贝）。 |
| `cpp/runtime/streaming.h` / `cpp/runtime/streaming.cpp` | 流式的全部类型与逻辑：`StreamChunk`、`StreamChannel`、`FinishReason`、`SlotStreamState`、`StreamChannelFinalizer`，以及 `decodePerSlot` / `emitChunks` / `applyStopStringMatch` 等自由函数。 |
| `examples/llm/llm_stream.cpp` | 交互式流式 CLI demo，是本讲代码实践的入口。 |
| `docs/source/user_guide/features/system-prompt-cache.md` / `streaming.md` | 用户视角的用法文档（JSON 字段、最佳实践、常见陷阱）。 |
| `docs/source/developer_guide/software-design/llm-streaming.md` | 流式的设计文档，列出了运行时的五个插入点与 RAII 守卫。 |

## 4. 核心概念与源码讲解

### 4.1 系统提示 KV 缓存：数据结构与缓存键

#### 4.1.1 概念说明

很多 LLM 应用都有这样一类请求：**系统提示是固定的，只有用户问题在变**。比如一个编程助手，每次都先来一句「You are a helpful Python programming assistant.」，再接用户的具体问题。

如果每次请求都重新对这段系统提示做 prefill，就白白重算了同样的 KV 缓存。系统提示 KV 缓存的做法是：**第一次遇到某段系统提示时，把它对应的 KV 缓存（以及混合模型的 recurrent/conv 状态）算好并存起来；后续只要看到同样的系统提示，就直接把存好的 KV 拷回缓存池，跳过这段 prefill**。这样 TTFT 与系统提示长度成正比地下降。

要正确做到「命中即复用」，必须回答两个问题：

1. **拿什么当缓存键？** 同样一句话，配不同 LoRA 适配器，KV 是不一样的；反过来，不同 LoRA 也不该串台。所以键要同时包含「系统提示文本」和「LoRA 名字」。
2. **存什么？** 光存 KV 张量还不够，混合模型（attention + Mamba）还要存递归状态和卷积状态；此外还得存这段提示的分词结果，以便恢复时校验。

#### 4.1.2 核心流程

缓存键的构造流程：

```text
用户请求 (messages + loraWeightsName)
   │
   │  套 chat 模板，抽出「格式化后的系统提示前缀」formattedSystemPrompt
   ▼
缓存键 = (formattedSystemPrompt, loraWeightsName)   ← 二元组，精确字符串匹配
```

关键设计点：

- **键用「格式化后」的系统提示**，而不是用户原始字符串。因为同一个模型可能用不同 chat 模板，格式化后的串才是真正被分词、送进模型的串。这也意味着用户指南里强调的「chat template aware」——它保证命中判断与实际送进模型的文本一致。
- **LoRA 名字进键**：同一个系统提示，`french` 适配器和 `spanish` 适配器会各自缓存一份，互不污染。
- **精确字符串匹配**：任何空白、标点差异都会导致 miss，这是文档里点名的限制。

#### 4.1.3 源码精读

缓存键是一个 `std::tuple<string, string>`，结构体 `SystemPromptKVCache` 持有缓存的全部内容：

[cpp/runtime/state/systemPromptKVCache.h:30-42](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/systemPromptKVCache.h#L30-L42) 定义键类型与缓存结构体——键是 `(systemPrompt, loraWeightsName)` 二元组，结构体里 `kvCacheLayers` 存每层 KV 张量，`recurrentStateContents` / `convStateContents` 给混合模型用、纯 attention 模型保持为空。

键的构造函数 `keySystemPromptWithLoraWeights` 只是把两个串打包成 tuple：

[cpp/runtime/state/systemPromptKVCache.h:44-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/systemPromptKVCache.h#L44-L48) 用 `std::make_tuple` 把系统提示与 LoRA 名字绑成一个键。

那「格式化后的系统提示」从哪来？答案在 `handleRequest` 准备阶段——`context.systemPrompts[i]` 直接取自请求的 `formattedSystemPrompt` 字段：

[cpp/runtime/llmInferenceRuntime.cpp:597-601](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L597-L601) 把请求里 chat 模板格式化好的系统提示前缀填进 `context.systemPrompts[i]`，这正是后面构造缓存键的文本来源。

请求侧的开关字段是布尔值 `saveSystemPromptKVCache`：

[cpp/runtime/llmRuntimeUtils.h:134-135](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L134-L135) 请求结构体里的 `saveSystemPromptKVCache` 标志位，对应 JSON 里的 `save_system_prompt_kv_cache` 字段。

> 备注：缓存只存在内存里（`mSystemPromptKVCacheBase` 这个 map），运行时一退出就丢，不是落盘的。

#### 4.1.4 代码实践

**实践目标**：理解缓存键由哪两部分组成，并验证「LoRA 名字进键」的隔离效果。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `docs/source/user_guide/features/system-prompt-cache.md` 的「LoRA Support」一节，观察示例 JSON 里同时出现 `lora_name: "french"` 和 `save_system_prompt_kv_cache: true`。
2. 对照 [systemPromptKVCache.h:44-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/systemPromptKVCache.h#L44-L48)，确认键的第二元就是 LoRA 名字。
3. 假想一个场景：先用 `french` 适配器存了系统提示 S 的缓存，紧接着用 `spanish` 适配器发同样的 S。问：会命中吗？

**预期结果**：不会命中。因为键 `(S, "french")` 与 `(S, "spanish")` 是两个不同的 tuple，`spanish` 那次会重新算一遍并存为新条目。这正是 LoRA 隔离的设计意图——不同适配器的 KV 数值不同，绝不能串台。

#### 4.1.5 小练习与答案

**练习 1**：为什么缓存键用「格式化后的系统提示」而不是用户原始的 system message 文本？

> **参考答案**：因为实际送进模型、参与 prefill 的是经 chat 模板格式化后的串。如果用原始文本做键，可能出现「键相同但格式化后串不同」（模板变了）的误命中，或「键不同但格式化后串相同」的漏命中。用格式化后的串做键，才能保证「键相同 ⟺ 送进模型的文本相同 ⟺ KV 缓存可复用」。

**练习 2**：如果两个用户分别用同一个模型、同一个 LoRA，发了只有结尾标点不同的系统提示（一个有句号一个没有），会发生什么？

> **参考答案**：cache miss，各自算一遍。因为键是精确字符串匹配，标点差异就构成不同的 tuple。文档把这点列为限制，并建议「标准化系统提示以最大化命中率」。

---

### 4.2 系统提示 KV 缓存：预生成、捕获与恢复

#### 4.2.1 概念说明

本模块讲清缓存「怎么存进去」和「怎么取出来用」两件事：

- **保存（save）**：当请求带 `save_system_prompt_kv_cache: true` 时，运行时单独把这段系统提示当成一次独立的单序列 prefill 跑一遍，然后把产出的 KV（和混合模型的状态）「捕获」出来存进 map。这是一次性的「预热」成本。
- **恢复（restore）**：之后任何请求，只要系统提示命中缓存，运行时就在真正 prefill 之前，把存好的 KV「恢复」回缓存池对应槽位，并告诉 KV 管理器「前 N-1 个位置已经有 KV 了，别重算」。

这里有个微妙但关键的细节：**恢复的是 N-1 个 token 的 KV，不是 N 个**。第 N 个 token（系统提示的最后一个）仍然作为「真实输入」参与 prefill。这样做的目的是让 draft 模型的 prefill 边界对齐到「真正的下一个 token 位置」，避免投机解码里 draft 与 base 的位置错位。

#### 4.2.2 核心流程

**保存流程**（`handleRequest` 检测到 `saveSystemPromptKVCache` 时触发）：

```text
for 每个序列 i in batch:
    genAndSaveSystemPromptKVCache(context, i):
      1. 守卫：Gemma4 视觉双向注意力不支持 → 直接返回
      2. 空提示 → 跳过
      3. 查 map：base + (draft) 都已存在 → 跳过；base 存在但 draft 残缺 → 删旧重算
      4. 分词、校验长度（base 与 draft 各自的上限）
      5. 构造临时单序列 context → setUpForPrefillExecution → runBaseModelPrefill
      6. currentGenerateLengths[0] -= 1   ← prefill 多算的那个 token 不计入
      7. 若投机解码：cacheStrategy.runSystemPromptPrefill（把 draft KV 也填上）
      8. 同步流
      9. 捕获：captureKVCache(base) +（混合模型）captureRecurrentStates/captureConvStates
     10. 存进 mSystemPromptKVCacheBase[promptKey]
     11. cacheStrategy.saveSystemPromptKVCache（draft 侧登记）
```

**恢复流程**（`setUpForPrefillExecution` 对每个序列）：

```text
promptKey = (context.systemPrompts[i], loraWeightsName)
if promptKey in mSystemPromptKVCacheBase:        # 命中
    cacheMgrBase.restoreKVCache(savedKV, i)       # 把存好的 KV 拷回槽 i
    if 投机解码: strategy.restoreSystemPromptKVCache(...)  # draft KV 同样恢复
    if 混合模型: restoreRecurrentStates(i, ...)
    reuseLength        = savedKV[0].shape[2]      # == 系统提示长度 N
    effectiveReuseLength = N - 1                  # 关键：复用 N-1
    reuseKVCacheLengthsData[i] = N - 1
    tokenIds[i]        = 输入去掉前 N-1 个        # 只 prefill 剩下的部分
    effectivePrefillLengths[i] = 原长 - (N-1)
    校验：存好的 tokenizedPrompt 是否与输入前缀逐 token 对齐（不对齐给警告）
else:                                              # 未命中
    全量输入，reuseKVCacheLengthsData[i] = 0
```

最后 `resetForNewSequences(mHostReuseKVCacheLengths, ...)` 把每个序列的复用长度交给 KV 管理器，后者据此调整内部长度记账。

#### 4.2.3 源码精读

**保存的触发点**——在 `handleRequest` 里，先临时关掉 profiling（让基准测试更贴近生产），再对批内每个序列调用保存：

[cpp/runtime/llmInferenceRuntime.cpp:666-680](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L666-L680) 检测到 `request.saveSystemPromptKVCache` 为真时，遍历批内每个序列调用 `genAndSaveSystemPromptKVCache`；单条失败只告警、不中断整个请求。

**保存的主体**——`genAndSaveSystemPromptKVCache`。先看它的守卫与缓存命中判断：

[cpp/runtime/llmInferenceRuntime.cpp:1736-1763](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1736-L1763) 三道关卡：Gemma4 视觉双向注意力不支持（直接返回 false）；空提示跳过；若 base 与 draft 缓存都已存在则跳过，若 base 存在但 draft 缺失则删除残缺的 base 重算。

接着构造临时单序列 context 跑一次完整 prefill，并把 draft 的 KV 也填上：

[cpp/runtime/llmInferenceRuntime.cpp:1787-1819](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1787-L1819) 复用现成的 `setUpForPrefillExecution` + `runBaseModelPrefill` 对系统提示做一次单序列 prefill；第 1808 行 `currentGenerateLengths[0] -= 1` 是因为 prefill 会顺带产出「系统提示之后的下一个 token」，但它不算真正生成出来的 token；投机解码场景再调 `cacheStrategy.runSystemPromptPrefill` 把 draft 的 KV 也算出来。

最后把算好的 KV「捕获」并存盘：

[cpp/runtime/llmInferenceRuntime.cpp:1825-1839](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1825-L1839) 调 `cacheMgrBase.captureKVCache` 把 base 的 KV 拷出来；混合模型（`numLinearAttnLayers > 0`）再额外捕获 recurrent/conv 状态；存进 `mSystemPromptKVCacheBase[promptKey]`，并让策略侧也登记 draft 缓存。

**恢复的主体**——在 `setUpForPrefillExecution` 里，对每个序列尝试命中：

[cpp/runtime/llmInferenceRuntime.cpp:1658-1693](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1658-L1693) 命中时先 `restoreKVCache` 把存好的 KV 拷回槽 i，投机解码再恢复 draft、混合模型再恢复 recurrent 状态；然后从存好的张量形状读出系统提示长度 `reuseLength`，取 `effectiveReuseLength = reuseLength - 1`，把输入前 N-1 个 token 剔除，只对剩余部分做 prefill。注释（1687-1689 行）解释了「复用 N-1 而非 N」是为了让 draft prefill 边界对齐真正的下一 token 位置。

恢复后还会校验存好的分词与当前输入前缀是否逐 token 对齐：

[cpp/runtime/llmInferenceRuntime.cpp:1695-1702](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1695-L1702) 若字符串键命中但 token 序列不完全对齐（典型原因：分词器行为变化或模板差异），给一条警告——这种情况下 KV 仍被复用，但结果可能有误，提示用户检查系统提示设计。

**捕获/恢复的底层实现**——在 `HybridCacheManager` 里，是按 headDim 分组的 GPU 批量拷贝。注意一个硬限制：**目前只支持 FP16（kHALF）KV 缓存，FP8 系统提示缓存尚未实现**：

[cpp/runtime/hybridCacheManager.cpp:364-371](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L364-L371) `captureKVCache` 起手就校验 KV 类型必须是 `kHALF`，FP8 直接报错——这与 u5-l5 讲过的「FP8 KV 缓存省内存」特性正交：FP8 KV 缓存用于常规推理，但系统提示缓存这条路径暂不支持 FP8。

[cpp/runtime/hybridCacheManager.cpp:414-448](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L414-L448) `restoreKVCache` 是 `captureKVCache` 的逆操作，从存好的张量形状读回序列长度，按 headDim 分组把 KV 拷回活跃缓存池的对应槽位。

**Gemma4-MTP 的特殊情形**——它在 u7-l1 里是「与目标模型共享 KV」的策略，所以草稿侧的系统提示缓存操作是空操作：

[cpp/runtime/decoding/gemma4MTPDecoder.cpp:235-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L235-L251) `restoreSystemPromptKVCache` 和 `saveSystemPromptKVCache` 都是空实现——草稿没有自己的 KV，base 的恢复已经把目标 KV 拷好了，草稿的共享 KV 注意力会直接读那份；`saveSystemPromptKVCache` 只是把键记进一个 set，为了通过一致性校验。

#### 4.2.4 代码实践

**实践目标**：固定系统提示 + 变化用户问题，用系统提示 KV 缓存把重复 prefill 省掉。

**操作步骤**：

1. 构造第一个请求 JSON（带保存标志），用 `examples/llm/llm_inference` 或 `llm_stream` 提交：

   ```json
   {
     "requests": [
       {
         "messages": [
           {"role": "system", "content": "You are a helpful Python programming assistant. <此处放一段超过 1K token 的长指令>"},
           {"role": "user", "content": "How do I read a CSV file?"}
         ],
         "save_system_prompt_kv_cache": true
       }
     ]
   }
   ```

2. 构造第二个请求 JSON，**去掉** `save_system_prompt_kv_cache` 字段，只换 user 问题（系统提示保持完全一致）。
3. 开启调试日志（命令行加 `--debug`），连续提交两次。
4. 对照 [llmInferenceRuntime.cpp:1658-1662](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1658-L1662) 确认第二次走的命中分支。

**需要观察的现象**：

- 第一次：日志能看到 prefill 跑了完整系统提示长度，TTFT 较高。
- 第二次：日志显示命中已有缓存，prefill 的输入长度大幅缩短（只剩用户问题部分），TTFT 明显下降。
- 若第二次 TTFT 没下降，最常见原因是系统提示文本不完全一致（多了一个空格、换行）→ 键 miss。

**预期结果**：待本地验证（依赖真实 GPU 与已构建的 engine）。在无 GPU 环境下，可通过阅读文档 `system-prompt-cache.md` 的「Best Practices」一节理解「长系统提示（>1K token）才划算、短提示（<100 token）预热开销大于收益」的权衡。

#### 4.2.5 小练习与答案

**练习 1**：为什么恢复时复用 `N-1` 个 token 的 KV，而不是 `N` 个？

> **参考答案**：为了让 prefill 的「真实输入」边界对齐到系统提示之后的第一个真正 token。如果复用全部 N 个，那么第 N+1 个位置就成了 prefill 的起点，但 draft 模型的预填充边界需要与 base 严格对齐到「下一个待预测 token」的位置；复用 N-1、把第 N 个留作真实输入，可保证 draft/base 两边的 prefill 边界一致，避免投机解码的位置错位。

**练习 2**：一个使用 FP8 KV 缓存的模型，能否享受系统提示 KV 缓存？为什么？

> **参考答案**：不能。`captureKVCache` / `restoreKVCache` 起手就校验 KV 类型必须是 `kHALF`（FP16），FP8 直接报错「FP8 system-prompt cache is not implemented」。这是当前实现的一个限制，不是设计上做不到，而是捕获/恢复的批量 kernel 只实例化了 `half` 模板。

**练习 3**：系统提示缓存为什么是「内存缓存」，而不是落盘？

> **参考答案**：KV 缓存是绑定到具体 GPU 型号、TensorRT 版本、engine 布局的运行时产物，跨进程/跨机器不可移植（这点在 u4-l1 讲过 engine 不可移植）。落盘意义不大，反而引入版本一致性的坑。内存缓存的代价是运行时重启后需要重新预热，所以文档建议「初始化阶段用 `save_system_prompt_kv_cache: true` 预热」。

---

### 4.3 流式输出：StreamChannel 生产者-消费者模型

#### 4.3.1 概念说明

u5-l2 已经介绍了流式的「接口层」：每个请求槽（slot）挂一个 `StreamChannel`，运行时是生产者、调用方是消费者，文本以 `StreamChunk` 增量推送。本模块从**实现**角度补三个 u5-l2 没展开的关键设计：

1. **生产者 API 与消费者 API 物理隔离**：消费者只能 `consume / tryPop / waitPop / cancel`，而 `push / finish` 这些「生产动作」是 private 的，只对运行时内部的几个 friend 函数开放。这保证消费者无法跳过终止块、无法乱序推送。
2. **共享指针强制的单实例所有权**：构造只能走工厂 `create()`，拷贝/移动都被 delete，强制用 `shared_ptr` 管理生命周期——因为运行时和调用方各持一份引用，谁先结束都不能把对方悬挂。
3. **终止语义的幂等性**：`finish()` 是「首调用者胜」（first caller wins）的幂等操作，多次调用安全。

#### 4.3.2 核心流程

`StreamChannel` 是一条 MPSC 管道，内部状态分两类：

```text
消费者线程                          运行时线程（handleRequest）
─────────────                      ─────────────────────────
consume(handler):                  push(chunk):
  loop:                              lock; mPending.push_back(chunk); unlock
    wait(cv: 有数据 or finished        cv.notify_one()
          or cancelled)
    pop 所有 pending → handler(chunk)
                                    finish(reason):
    若 finished && pending 空 → 退出      lock; 首次? set reason+finished; unlock
                                          cv.notify_all()
cancel():                            setOriginalBatchIdx(idx):  ← attach 时调
  mCancelled = true
  cv.notify_all()  ← 唤醒所有阻塞的 wait/waitPop
```

关键不变量：

- `consume` 只有在「pending 空 且 (finished 或 cancelled)」时才退出——保证消费者要么拿到每个 chunk，要么明确知道已终止。
- `finish` 与 `cancel` 都通过 `notify_all` 唤醒阻塞的消费者，所以消费者绝不会因为运行时异常而永久阻塞（再加上 4.4 的 RAII 守卫，双重保险）。

#### 4.3.3 源码精读

**类型定义**。先看终止原因枚举与 chunk 载荷：

[cpp/runtime/streaming.h:52-68](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L52-L68) `FinishReason` 枚举：`kNotFinished`（非终止块）、`kEndId`（模型出 EOS）、`kLength`（到 maxGenerateLength）、`kCancelled`（消费者取消）、`kError`（运行时异常）、`kStopWords`（命中停止串）。

[cpp/runtime/streaming.h:83-92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L83-L92) `StreamChunk` 载荷：`tokenIds`（自上次 chunk 以来的增量 token）、`text`（增量文本，**始终是合法 UTF-8**）、`finished`、`reason`、以及可选的逐 token top-K logprobs。

**类与所有权**。`StreamChannel` 把构造私有化、只留工厂，并 delete 拷贝/移动：

[cpp/runtime/streaming.h:151-162](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L151-L162) 工厂 `create()` 是唯一构造途径，强制 `shared_ptr` 所有权；拷贝与移动构造/赋值全部 delete，避免出现两个对象指向同一份内部状态。

[cpp/runtime/streaming.cpp:38-42](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L38-L42) `create()` 用 `shared_ptr<StreamChannel>(new ...)` 而非 `make_shared`，因为私有构造子 `make_shared` 访问不到。

**生产者 API 的私有性 + friend**。`push / finish / setOriginalBatchIdx` 是 private，只对几个 friend 开放：

[cpp/runtime/streaming.h:209-221](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L209-L221) 生产者 API（push/finish/setOriginalBatchIdx）声明为 private，并用 `friend` 把运行时侧的自由函数（attachStreamChannel、validateStreamingSubmission、applyCancellationToFinishStates、decodePerSlot、emitChunks）和 `StreamChannelFinalizer` 列为唯一合法调用者——消费者拿不到这些方法。

**幂等的 finish**。终止是「首次调用者胜」：

[cpp/runtime/streaming.cpp:53-65](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L53-L65) `finish` 先检查 `mFinished`，若已终止直接返回（幂等），否则记下 reason 并置 finished，最后 `notify_all` 唤醒所有等待者。这保证「正常 emit 路径的 finish」与「RAII 守卫异常路径的 finish」即使都调用也安全，首个写入的 reason 生效。

**阻塞消费**。`consume` 模板在头文件里，是消费者最常用的入口：

[cpp/runtime/streaming.h:238-262](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.h#L238-L262) `consume` 在 `pending 空 且 (finished 或 cancelled)` 时才退出；处理 chunk 时释放锁（`lk.unlock()`），让运行时能继续 push。这正是文档警告「不能在跑 handleRequest 的同一线程调 consume」的根因——同线程会死锁，因为没人 push。

**超时单次弹出**。`waitPop` 给需要 per-call 截止时间的场景：

[cpp/runtime/streaming.cpp:137-154](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L137-L154) `waitPop` 用条件变量等到「有数据 / finished / cancelled」三者任一，超时或被唤醒但队列仍空则返回 `nullopt`。

#### 4.3.4 代码实践

**实践目标**：用最小代码把一个 `StreamChannel` 跑起来，验证生产者-消费者解耦。

**操作步骤**（基于 `examples/llm/llm_stream.cpp`，可直接运行）：

1. 阅读 [examples/llm/llm_stream.cpp:391-410](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_stream.cpp#L391-L410)：`create()` 建通道、`setStreamInterval` 设节流、`consume` 里把 `chunk.text` 直接 `fwrite` 到 stdout。
2. 参照 `streaming.md` 的构建命令编译并运行 `llm_stream`（命令见综合实践）。
3. 观察终端：文本是逐字/逐块「流」出来的，而不是一次性出现。

**需要观察的现象**：在 worker 线程跑 `handleRequest` 的同时，主线程的 `consume` 回调已经在打印——两条线程解耦工作。若你故意把 `consume` 放到跑 `handleRequest` 的同一线程，会观察到死锁（文档「Common pitfalls」表格列了这条）。

**预期结果**：待本地验证（需 GPU + engine）。无 GPU 时，阅读 `llm_stream.cpp` 的 `runOneRequest` 函数即可理解线程分工。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `push / finish` 必须设成 private 且只能被 friend 调用，而不是公开 API？

> **参考答案**：因为 `push` 和 `finish` 决定了 chunk 的顺序与终止时机，这属于「运行时契约」。如果消费者也能调 `push`，就可能插入伪造 chunk 或提前 `finish`，破坏「终止块一定携带真实 reason」「chunk 顺序与生成顺序一致」等不变量。把它们私有化、只让运行时的几个固定函数调，就把契约钉死在实现侧。

**练习 2**：`StreamChannel` 为什么强制用 `shared_ptr`、禁止拷贝？

> **参考答案**：运行时和调用方各需一份引用，且谁先结束都不确定。`shared_ptr` 让最后存活的一方负责析构内部状态。禁止拷贝是为了避免两个独立对象指向同一份 mutex/deque，造成重复释放或无锁保护的并发访问。

---

### 4.4 流式输出：解码循环中的增量推送

#### 4.4.1 概念说明

本模块讲流式在 `handleRequest` 解码主循环里的具体编排，以及四个让流式「好用」的子机制：

1. **挂载与种子**：请求进来时，把 channel 挂到每个槽，并把「已发送 token 数」种子设为 prompt 长度——这样流式只推送**生成出来的** token，不推送 prompt。
2. **每轮三段流水线**：`applyCancellationToFinishStates`（观测取消）→ `decodeStep`（策略前向）→ `decodePerSlot`（解码成 UTF-8 + 停止串匹配）→ `updateFinishStates`（定终止原因）→ `emitChunks`（推送）。
3. **节流（streamInterval）与停止串 hold-back**：不必每 token 都推一块；停止串可能跨两块，所以每次只发「安全」的部分，把末尾几字节留到下一轮再判。
4. **RAII 终止保证**：`StreamChannelFinalizer` 在 `handleRequest` 的任何退出路径（正常返回、错误返回、异常）都确保每个 channel 被终止——消费者永不死等。

#### 4.4.2 核心流程

主循环里流式相关的顺序（简化伪代码）：

```text
handleRequest:
  # 准备阶段
  validateStreamingSubmission(request)          # 校验 channel 数量/状态
  for i: attachStreamChannel(ch[i], origIdx)     # 挂载，置 attached=true
         slotStreams[i].sentTokenCount = promptLen   # 种子：只流生成 token
  StreamChannelFinalizer finalizer(context, tok) # RAII 守卫，栈对象

  # prefill 后第一次推送
  applyCancellationToFinishStates(context)
  decodePerSlot(context, tok)         # emitDelta 出 UTF-8，停止串匹配
  updateFinishStates()                 # 定 kEndId/kLength/kStopWords
  emitChunks(context, tok)             # push chunk，若终态则 finish()

  # 解码主循环
  while 未全结束:
    applyCancellationToFinishStates(context)   # 顶部观测 cancel
    if !decodeStep(strategy): break
    decodePerSlot(context, tok)
    updateFinishStates()
    emitChunks(context, tok)
    emitTokenCallbacks(context)         # 轻量 TokenCallback 通道
    performBatchEvict()                 # 含 compactVector(slotStreams)

  # 函数返回 → finalizer 析构 → 未终止的 channel 以 kError 终止
```

`decodePerSlot` 每个槽的处理：

```text
若 (无 channel 且无停止串) → 跳过（快路径）
若 (有 channel 且 newTokens < streamInterval 且 非终态) → 跳过（节流）
delta = emitDelta(slot, tok, tokenIds[i])   # 增量解码，攒合法 UTF-8
若终态: delta += emitDeltaFlush(pendingBytes)
若启用停止串:
    stopMatchBuffer.append(delta)
    outcome = applyStopStringMatch(stopMatchBuffer, stops, maxStopLen, isFinal)
    pendingEmitText = outcome.emitted       # 安全可发的部分
    stopMatchedThisIter = outcome.stopMatched
否则: pendingEmitText = delta
```

停止串匹配的核心是「hold-back」：如果没匹配到也不是终态，就把缓冲区末尾 `maxStopLen - 1` 字节**扣留**到下一轮，因为停止串可能正好横跨「这次发的」和「下次发的」边界。

#### 4.4.3 源码精读

**挂载与种子**。在准备阶段，把 channel 挂上、种子置为 prompt 长度，并构造 RAII 守卫：

[cpp/runtime/llmInferenceRuntime.cpp:695-711](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L695-L711) 对每个有 channel 的槽调 `attachStreamChannel`（置 attached=true，记 originalBatchIdx），把 `sentTokenCount` 与 `lastEmittedTokenCount` 都种子为 prompt 长度——这样后续 `emitDelta` 只会处理生成出来的 token；紧接着构造栈上的 `StreamChannelFinalizer` 守卫，覆盖整个 handleRequest 生命周期。

**prefill 后与主循环的三段流水线**。prefill 后第一次推送：

[cpp/runtime/llmInferenceRuntime.cpp:922-930](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L922-L930) 注释点明顺序「cancel → decode(emitDelta+停止串匹配) → finalize(EOS/length/stop) → emit」；这正是 pref​ill 后的首次推送。

主循环每轮同样的顺序：

[cpp/runtime/llmInferenceRuntime.cpp:945-965](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L945-L965) 顶部先 `applyCancellationToFinishStates` 观测取消，再 `decodeStep`，再 `decodePerSlot → updateFinishStates → emitChunks`，最后 `emitTokenCallbacks`（轻量 TokenCallback 通道，u5-l2 已介绍）。

**节流与增量解码**——`decodePerSlot` 的关键路径：

[cpp/runtime/streaming.cpp:318-333](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L318-L333) streamInterval 节流——非终态且新增 token 不足 interval 时整槽跳过；`emitDelta` 把新增 token 解码成增量文本（内部用 u5-l7 讲的 `emitDelta` 攒合法 UTF-8），终态时再 flush 残留字节。

**停止串 hold-back 算法**——`applyStopStringMatch`：

[cpp/runtime/streaming.cpp:245-273](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L245-L273) 三种情形：命中（最早位置胜，发匹配前的部分、清缓冲、置 stopMatched）、终态未命中（全发）、否则发掉「缓冲区减去末尾 maxStopLen-1 字节」——被扣留的末尾字节留到下一轮，以捕捉横跨边界的停止串。

**取消传播**——`applyCancellationToFinishStates`：

[cpp/runtime/streaming.cpp:276-296](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L276-L296) 遍历槽，若 channel 的 cancelled 标志被置、且该槽尚未自然终止，就把 finishedStates 置 1、terminalReason 记为 `kCancelled`。「first-writer-wins」由 `if (context.finishedStates[i]) continue` 保证——已自然终止的槽不被 cancel 覆盖。

**推送**——`emitChunks`：

[cpp/runtime/streaming.cpp:357-415](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L357-L415) 只处理有 channel 的槽；组装 chunk（增量 tokenIds = `[lastEmittedTokenCount, sentTokenCount)`、text = pendingEmitText），`channel->push`；更新 `lastEmittedTokenCount`；若终态调 `channel->finish(reason)`。也处理逐 token logprobs 的打包。

**批量驱逐时的同步压缩**——`slotStreams` 要跟着 tokenIds 一起压缩，否则槽位错乱：

[cpp/runtime/llmInferenceRuntime.cpp:2010-2014](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L2010-L2014) `performBatchEvict` 里 `compactVector(batchMapping, context.slotStreams)` 与 tokenIds 等向量同步压缩——某个序列结束被驱逐时，它的流式状态也跟着移除，保持下标对齐。

**RAII 终止保证**——`StreamChannelFinalizer` 析构函数：

[cpp/runtime/streaming.cpp:428-497](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/streaming.cpp#L428-L497) 析构（在 handleRequest 任何退出路径触发）遍历所有 channel，对尚未终止的：尽力组装一个含未发 token 的终止块（reason=kError）并 push，**然后无条件调 `finish(kError)`**。即使组装块时 OOM 抛异常（被 `catch(...)` 吞掉），`finish` 仍会执行——因为「消费者必须被唤醒」这个不变量只靠 `finish` 承载，不靠 chunk 内容。这是消费者「永不死等」的最终保险。

#### 4.4.4 代码实践

**实践目标**：亲手触发取消与停止串，观察它们如何改变终止原因。

**操作步骤**（基于 `llm_stream` 交互式 demo）：

1. 用 `--streamInterval 4` 启动 `llm_stream`，观察 chunk 是 4 个 token 一块而非逐 token（验证节流）。
2. 生成过程中按 `s` 键（skip）——阅读 [examples/llm/llm_stream.cpp:447](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_stream.cpp#L447) 可知它调 `ch->cancel()`；观察当前请求的 footer，`finish reason` 应为 `cancelled`（3）。
3. 在输入 JSON 的某条请求里加 `"stop_strings": ["<某词>"]`，让模型生成到该词时停；观察 footer 的 finish reason 应为 `stop-words`（5），且输出里不含该停止串本身。

**需要观察的现象**：

- 节流：非终态块都恰好含 `≥ streamInterval` 个 token，最后一块无论够不够都触发（不丢尾 token）。
- 取消：footer 显示 reason=cancelled；输出是取消前已生成的部分。注意文档指出 cancel 在下一个迭代边界生效，vanilla 可能多生成 1 个 token。
- 停止串：输出被裁剪、reason=stop-words。

**预期结果**：待本地验证（需 GPU + engine）。无 GPU 时，可阅读 `streamingTests.cpp`（设计文档提到有 51 条 CPU 不变式测试）理解这些行为，它们以断言形式精确描述了上述现象。

#### 4.4.5 小练习与答案

**练习 1**：为什么停止串匹配要「扣留末尾 maxStopLen-1 字节」到下一轮？

> **参考答案**：因为停止串可能正好横跨两次推送的边界。假设停止串是 `"</tool>"`（7 字节），这次发到 `"</to"` 就发完了，剩下的 `"ol>"` 在下一批 token 里。如果这次把 `"</to"` 全发出去，下一轮即使匹配到也无法收回已发的部分。扣留末尾 6 字节（maxStopLen-1），就能在「这次缓冲区 + 下次新增」合并后完整匹配，匹配到则只发匹配前的部分。终态时则不再扣留、全发。

**练习 2**：`StreamChannelFinalizer` 为什么即使「组装终止块时抛异常」也要保证调 `finish`？

> **参考答案**：消费者阻塞在 `consume`/`waitPop` 上，等的是「finished 或 cancelled」信号。如果不调 `finish`，运行时因 OOM 异常退出后，消费者会永久阻塞——这是死等。`finish` 是幂等且 no-throw 的，它是「消费者必须被唤醒」这个核心不变量的唯一承载者；chunk 内容（可能因 OOM 失败）只是锦上添花。所以析构里用 `catch(...)` 吞掉组装异常，再无条件 `finish(kError)`。

**练习 3**：主循环为什么把 `applyCancellationToFinishStates` 放在每轮**最前面**，而不是 `emitChunks` 之前？

> **参考答案**：为了让 cancel 在终止原因的「first-writer-wins」竞争中优先。cancel 写 `kCancelled`，`updateFinishStates` 写 `kEndId/kLength`。把 cancel 观测放最前，意味着用户按取消后，即使这一轮模型恰好生成了 EOS，cancel 也会先抢占终止原因（前提是该槽尚未自然终止）。这也让取消尽早生效，减少无谓的前向计算。

---

## 5. 综合实践

设计一个**客服机器人**场景，把本讲两个特性串起来用：

**场景**：你部署一个客服 LLM，系统提示是一段固定的、约 2K token 的「客服人设 + 业务知识 + 回答规范」。大量用户轮流提问，每个问题都共用这段系统提示。你希望首 token 尽快出现（用户体验），并能逐字流式输出。

**任务**：

1. **启用系统提示缓存**：写一个初始化脚本，用一条带 `save_system_prompt_kv_cache: true` 的请求把这段 2K 系统提示预热进缓存（参照 [system-prompt-cache.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/system-prompt-cache.md) 的「Pre-warm cache during initialization」建议）。
2. **说明 TTFT 收益**：对照 [llmInferenceRuntime.cpp:1658-1693](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1658-L1693) 解释——预热后，每个用户请求的 prefill 输入只剩「用户问题 + 系统提示最后 1 token」那部分，省掉了 2K token 的重复 prefill，TTFT 与之成正比下降。
3. **叠加流式**：用 `llm_stream`（或自己写 C++ 客户端）给每个用户请求挂一个 `StreamChannel`、`setStreamInterval(1)`，参照 [llm_stream.cpp:391-410](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_stream.cpp#L391-L410)。
4. **解释首 token 提早返回**：流式让「首 token 一生成就立刻推给用户」，而不必等全部生成完。把「系统提示缓存降低 prefill 时间」与「流式让首 token 立刻可见」叠加，就是 TTFT 的双重优化——前者缩短了 prefill 本身，后者消除了「等整段生成」的尾延迟。
5. **勾选注意点**：
   - 系统提示必须**逐字符**一致（含模板格式化后），否则 miss。
   - 若用 FP8 KV 缓存模型，系统提示缓存当前不可用（4.2 练习 2）。
   - `handleRequest` 必须在独立线程跑，`consume` 在另一线程（4.3 练习 / 文档 pitfall）。
   - 多租户若用不同 LoRA，各自预热一份（键含 LoRA 名字）。

**可运行的命令骨架**（待本地验证，需 GPU + 已 export/build 的 LLM engine）：

```bash
# 1. 编译流式 demo
cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR && make -j$(nproc) llm_stream
export LD_LIBRARY_PATH=$TRT_PACKAGE_DIR/lib:$LD_LIBRARY_PATH
export EDGELLM_PLUGIN_PATH=$(pwd)/libNvInfer_edgellm_plugin.so

# 2. 预热系统提示缓存（warmup.json 里 save_system_prompt_kv_cache: true）
./examples/llm/llm_stream --engineDir /path/to/Engine --inputFile warmup.json

# 3. 正式服务，每个用户请求自动命中缓存 + 流式输出
./examples/llm/llm_stream --engineDir /path/to/Engine \
    --inputFile user_question.json --streamInterval 1 --maxGenerateLength 256
```

## 6. 本讲小结

- **系统提示 KV 缓存的键**是 `(格式化后的系统提示, LoRA 名字)` 二元组，靠精确字符串匹配，chat 模板感知、LoRA 隔离。
- **保存**是一次独立的单序列 prefill（`genAndSaveSystemPromptKVCache`），把 KV 与混合模型状态「捕获」进 map；**恢复**在 prefill 前发生，复用 `N-1` 个 token 的 KV，把第 N 个留作真实输入以对齐 draft 边界。
- 底层 `captureKVCache` / `restoreKVCache` **目前只支持 FP16（kHALF）**，FP8 系统提示缓存尚未实现；Gemma4-MTP 因与目标共享 KV，草稿侧保存/恢复是空操作。
- **流式的 `StreamChannel`** 是 MPSC 管道，生产者 API（push/finish）私有、仅 friend 可达，消费者只能 consume/tryPop/waitPop/cancel；`finish` 幂等、首调用者胜。
- **解码主循环**每轮按 `applyCancellationToFinishStates → decodeStep → decodePerSlot → updateFinishStates → emitChunks` 顺序，节流靠 streamInterval，停止串靠 hold-back，取消在迭代顶部观测。
- **`StreamChannelFinalizer`** 是 RAII 守卫，保证 `handleRequest` 任何退出路径下每个 channel 都被 `finish`，消费者永不死等。

## 7. 下一步学习建议

- **u9-l1 LoRA 支持**：本讲反复提到「缓存键含 LoRA 名字」「不同 LoRA 各存一份缓存」，下一篇会讲透 LoRA 的全链路（导出图插入 → sidecar → 运行时动态切换），两篇合起来你就理解了多租户 LoRA 服务的完整缓存语义。
- **u9-l3 词表裁剪、FP8 KV 缓存与 chat 模板**：本讲点到了 FP8 KV 缓存与系统提示缓存「正交且暂不互通」，那一篇会专门讲 FP8 KV 缓存的收益与启用条件。
- **直接读源码**：想更细看取消/停止串/节流的精确不变式，去读 `unittests/streamingTests.cpp`（设计文档说有 51 条 CPU 测试）和 `unittests/streamingRuntimeTests.cpp`（4 条带 engine 的场景测试），断言即规格。
- **多模态与系统提示缓存**：本讲强调了「系统提示只能含文本，多模态系统提示不支持」，若你做 VLM，建议阅读 u6-l1 后明确把图像放在 user 消息而非 system 消息里。
