# 引擎执行器与张量注册表

## 1. 本讲目标

在上一讲（u5-l1）里，我们已经认识了运行时的总指挥 `LLMInferenceRuntime` 与它的 `handleRequest`。但「调用引擎跑一次前向」这件具体的事，是被委托给一个更底层、更纯粹的组件完成的——本讲的主角 **`EngineExecutor`**。

读完本讲，你应该能够：

1. 说清 `EngineExecutor` 为什么是「对模型一无所知」的薄封装，以及它如何用 `prepare()` / `execute()` / `captureGraph()` 三件套完成一次 TensorRT 推理。
2. 理解它如何用**同一个**执行上下文（`IExecutionContext`）在 prefill（profile 0）与 decode（profile 1）两套优化 profile 之间切换，而不需要重建引擎或上下文。
3. 掌握 `TensorRegistry`（声明式绑定规格）与 `TensorMap`（运行时张量缓冲映射）这两张「表」如何分工，以及它们如何配合 `InferenceDims` 把符号维度解析成具体形状。
4. 解释 KV 缓存不属于 `EngineExecutor`、LoRA 权重为何不进注册表，以及 LoRA 动态切换如何借助 CUDA graph 缓存实现。

本讲对应的最小模块：`engineExecutor`、`tensorRegistry`、`tensorMap`。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是 TensorRT 的执行上下文与优化 profile。** TensorRT 把一个网络（engine）编译成针对特定 GPU 的高度优化内核。由于 LLM 推理的输入形状在 prefill（一次吃一长串 token）和 decode（每次只吃 1 个 token）之间天差地别，单一一组内核无法都跑得快。于是构建器（u4-l1/u4-l2）会为同一个引擎登记**多个优化 profile**，每个 profile 用 `min/opt/max` 三元组刻画一组形状范围；profile 0 通常对应 prefill（长序列、小 batch），profile 1 对应 decode（单 token、可大 batch）。运行时按当前步骤的形状，把上下文切到合适的 profile，从而命中针对该形状选优过的内核。

**什么是「绑定（binding）」。** TensorRT 不知道你的张量放在显存的哪里，它只认张量的「名字」。一次推理前，运行时必须为引擎的每一个输入/输出张量做两件事：`setTensorAddress(name, ptr)` 告诉它缓冲区地址，`setInputShape(name, dims)` 告诉它这一步的实际形状。这套「名字 → 地址 + 形状」的登记，就是 binding。`EngineExecutor` 的一大职责就是把这件事自动化、集中化。

**为什么要拆成三张表。** 边缘 LLM 的引擎有几十上百个绑定（每个 attention 层一对 KV 缓存、可能还有 Mamba 状态、RoPE、投机解码掩码……）。如果每次推理都手写一遍 `setTensorAddress`，代码会又长又脆。本项目把这套信息拆成三层：① `TensorRegistry` 记录「这个引擎有哪些绑定、各自的形状模板」（声明式、构建一次）；② `TensorMap` 记录「每个名字现在由哪个 GPU 缓冲支撑」（运行时、按请求填充）；③ `InferenceDims` 记录「这一步符号维度的当前取值」（每步变化）。三者合一，一次 `bindAll()` 就完成全部绑定。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [cpp/runtime/exec/engineExecutor.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.h) | `EngineExecutor` 类声明：持有 TRT runtime/engine/context、`TensorRegistry`、CUDA graph 缓存；公开 `prepare/execute/captureGraph` 与若干只读访问器。 |
| [cpp/runtime/exec/engineExecutor.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp) | `EngineExecutor` 实现：构造引擎、profile 切换、绑定、enqueueV3 / graph 回放、绑定快照与哈希。 |
| [cpp/runtime/exec/tensorRegistry.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.h) / [.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.cpp) | `TensorRegistry`：保存 `TensorSpec`（含符号形状模板与逐层 `%d` 展开），`bindAll()` 一次性解析形状并绑定。 |
| [cpp/runtime/exec/tensorMap.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.h) / [.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.cpp) | `TensorMap`：名字 → `Tensor*` 的非拥有映射，绑定层用它查地址。 |
| [cpp/runtime/config/inferenceDims.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/inferenceDims.h) | `InferenceDims`（9 个符号维度字段）、`ShapeDim`（固定/符号维度）、`sym()/fixed()` 助手。 |
| [cpp/runtime/config/llmEngineConfig.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/llmEngineConfig.cpp) | `prefillDims/decodeDims/specVerifyDims` 等「配方方法」，是 `InferenceDims` 的正规构造入口。 |
| [cpp/runtime/exec/registryBuilder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp) | `buildRegistryForLLM` / `buildRegistryFor*Draft`：按配置生成 `TensorRegistry`，是「声明式规格」的来源。 |
| [cpp/runtime/state/pipelineIO.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp) | `buildTensorMap`：把 `PipelineIO`/`SharedResources` 里的真实缓冲填进 `TensorMap`。 |
| [cpp/runtime/llmInferenceRuntime.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp) | `LLMInferenceRuntime`：定义 `kPrefillProfile/kDecodeProfile`，并在 prefill/decode 处真正调用 `EngineExecutor`。 |

> 说明：本讲聚焦 `exec/` 目录下的三个核心文件，其余文件仅作为「上下游契约」引用以解释数据从哪来、到哪去。`InferenceDims` 虽不在最小模块清单里，但它是理解 `TensorRegistry` 的必备前置，所以单列。

## 4. 核心概念与源码讲解

本讲按自底向上的顺序展开：先看最简单的 `TensorMap`（4.1），再看建立在它之上的 `TensorRegistry`（4.2），最后看把两者串起来、并负责 profile 切换与 CUDA graph 的 `EngineExecutor`（4.3）。

### 4.1 TensorMap：名字到张量缓冲的映射

#### 4.1.1 概念说明

`TensorMap` 解决一个非常朴素的问题：引擎认识成百上千个绑定名（如 `inputs_embeds`、`past_key_values_3`、`logits`），运行时需要为每个名字提供一个具体的 `Tensor`（GPU/CPU 缓冲）。`TensorMap` 就是这张「名字 → 张量指针」的查找表。

它的两个关键设计：

- **非拥有（non-owning）。** 它只存 `Tensor*` 指针，不负责释放张量。真正的张量由 `PipelineIO`（流水线 I/O 缓冲）、`SharedResources`（KV 缓存等共享资源）、`LoRAManager`（LoRA 权重）等拥有。这要求调用方保证这些张量比 `TensorMap` 活得更久。
- **不参与形状解析。** `TensorMap` 只管「地址」，不管「这一步该用什么形状」——形状由 `TensorRegistry` 配合 `InferenceDims` 决定（见 4.2）。

#### 4.1.2 核心流程

`TensorMap` 的生命周期：

1. **填充**：在每次推理准备阶段，`buildTensorMap()`（pipelineIO）把核心 I/O、RoPE、逐层 KV/Mamba 缓存、`kvcache_start_index` 等真实缓冲 `set` 进来；若启用 LoRA，`LoRAManager::refreshTensorMap()` 再补上 LoRA 权重条目。
2. **查询**：`TensorRegistry::bindAll()` 与 `EngineExecutor::prepare()` 的兜底循环都用 `map.get(name)` 取出指针，交给 TensorRT 的 `setTensorAddress`。

#### 4.1.3 源码精读

`TensorMap` 本体极小，核心就是一个 `unordered_map<string, Tensor*>`：

[name → 指针映射，非拥有](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.h#L36-L89) ——`set` 存指针，`get` 查指针，`contains/allNames/size` 提供只读视图。

[set / get 实现](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.cpp#L25-L34)：`mTensors[name] = &tensor;` 直接覆盖（同名重复 set 以新值为准），`get` 找不到返回 `nullptr`。

值得专门留意的是一个**故意的编译期「绊线」**：

[禁止右值/临时 Tensor 绑定](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.h#L52-L56) ——`set(string, Tensor&&)` 被 `= delete`。因为 `TensorMap` 只存指针，若有人把一个临时 `Tensor` 传进来，临时对象在语句末就析构了，绑定的指针立刻变成悬空指针，而 `bindAll` 是稍后才解引用的。把右值重载删掉，这类错误在编译期就暴露。

`TensorMap` 是怎么被填满的？看 `buildTensorMap` 里几类典型条目：

[核心 I/O 与逐层 KV 绑定](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L131-L164)。注意 L163-L164 这两行：同一层的 `past_key_values_i`（输入）与 `present_key_values_i`（输出）被绑定到**同一个** `combinedKV` 张量（注释写明 `alias: in-place`）。这是 EdgeLLM 的 attention 插件约定：它把新生成的 KV 直接原地写回 KV 缓存缓冲，因此「past」和「present」共享地址。

[kvcache_start_index 绑定到缓存管理器的长度张量](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L198-L203)：这是一个「地址稳定、形状每步变」的典型——同一个缓冲，prefill 时形状是 `[0]`（哨兵）、decode 时是 `[batch]`。`TensorMap` 只提供地址，形状由 `InferenceDims::startIndexLen` 决定。

而 LoRA 条目是**后补**进去的：

[LoRA 注释](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L251-L252) 说明 LoRA 张量由 `LoRAManager::refreshTensorMap()` 在 `buildTensorMap()` 之后填充；具体见 [refreshTensorMap](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L210-L228)：对每个引擎 LoRA 绑定名，有对应 adapter 权重就 `set` 真实权重，否则 `set` 一个 dummy 张量（用于融合投影如 `qkv_proj` 没有独立权重的情况）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「`TensorMap` 只管地址、不管形状」，并理解 KV 缓存的就地别名。

**操作步骤**：

1. 打开 [tensorMap.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorMap.cpp)，确认 `set` 只写 `&tensor` 指针，`get` 只返回指针——全程没有任何 `setInputShape` 调用。
2. 打开 [pipelineIO.cpp 的 KV 绑定段](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L160-L165)，找到 `past_key_values` 与 `present_key_values` 绑定到同一个 `combinedKV`。
3. 追问自己：既然输入和输出绑的是同一块缓冲，插件怎么做到「读旧 KV、写新 KV」而不互相覆盖？答案藏在 attention 插件按 `seqLen`/`startIndexLen` 决定读写偏移（见 u8 单元）。

**需要观察的现象**：你会看到 `TensorMap` 完全不出现 `nvinfer1::Dims`、`batch`、`seqLen` 等字样——形状语义被严格排除在这张表之外。

**预期结果**：能用自己的话说明「`TensorMap` 是纯地址表，KV 的 past/present 同址是插件就地更新的约定」。

> 无 GPU 也可完成：本实践纯为源码阅读型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set(string, Tensor&&)` 被删除，而 `set(string, Tensor&)` 允许？

**参考答案**：`TensorMap` 只存非拥有指针。允许右值会让人绑定一个临时对象，临时对象析构后指针悬空，而 `bindAll` 是稍后才解引用，会造成难定位的内存错误。删除右值重载把这类用法挡在编译期。

**练习 2**：如果一个绑定名既没在 `buildTensorMap` 里 `set`，也不归 LoRA 管理，`map.get(name)` 会返回什么？下游会怎样？

**参考答案**：返回 `nullptr`。在 `TensorRegistry::bindAll` 里会记 `LOG_ERROR("tensor '%s' not in tensorMap")` 并返回失败；在 `EngineExecutor::prepare` 的兜底循环里同样会报错并返回 false——即「引擎有绑定却没人提供缓冲」是硬错误。

---

### 4.2 TensorRegistry：声明式绑定规格与 bindAll

#### 4.2.1 概念说明

`TensorMap` 只给地址，但 TensorRT 还要知道**每个输入这一步的形状**。形状里的 batch、序列长度等是每步变化的，不能写死。`TensorRegistry` 用一种「符号形状模板」优雅地解决它：

- 它存一组 `TensorSpec`，每个 spec 写明张量的 `name`、`io`（输入/输出）、`dtype`、以及一个 `shape` 模板。模板的每一维要么是**固定值**（如 `hiddenSize`、`2`），要么是**符号引用**——指向 `InferenceDims` 的某个字段（如 `&InferenceDims::batch`）。
- 绑定时（`bindAll`），它遍历所有 spec：用 `TensorMap` 取地址，用当前 `InferenceDims` 把符号维解析成具体数值，得到这一步的 `nvinfer1::Dims`，再 `setInputShape`。

「符号维」的实现非常巧妙：`ShapeDim` 里存的是 **C++ 指向成员的指针**（`int64_t InferenceDims::*`），解析时直接 `inferenceDims.*(shape[i].symbol)` 取值，无需字符串查表、零运行期开销。

支撑这个机制的是 `InferenceDims`——一个恰好 9 个字段、用 `static_assert` 钉死布局的结构体，它是所有 LLM/投机解码引擎用到的符号维度的**全集**。

#### 4.2.2 核心流程

`TensorRegistry` 的两条流程：

**构建期（一次性）**：由 `registryBuilder.cpp` 的 `buildRegistryForLLM` / `buildRegistryFor*Draft` 按 `LLMEngineConfig` 生成。例如对每个 attention 层加一条 KV 缓存 spec，对每个 mamba 层加 recurrent/conv state spec。spec 可以带 `perLayer` 计数与名字里的 `%d`，在查询时展开成多条具体 spec。

**绑定期（每步）**：

```
bindAll(ctx, tensorMap, inferenceDims):
    for spec in allExpandedSpecs():          # 展开逐层 %d 模板
        tensor = tensorMap.get(spec.name)    # 取地址
        ctx.setTensorAddress(spec.name, tensor.rawPointer())
        if spec.io == kInput:
            dims = resolveShape(spec.shape, inferenceDims)  # 符号维 → 数值
            ctx.setInputShape(spec.name, dims)
```

`resolveShape` 的核心一行：`dims.d[i] = shape[i].isSymbolic() ? inferenceDims.*(shape[i].symbol) : shape[i].value;`

#### 4.2.3 源码精读

先看支撑类型 `InferenceDims` 与 `ShapeDim`：

[`InferenceDims` 的 9 个字段](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/inferenceDims.h#L43-L71)：`batch/seqLen/kvLen/selectLen/attnMaskSeqLen/ropeBatch/packedMaskLen/startIndexLen/specVerifyPhaseLen`。注意头注释强调「字段没有类内默认值」，正规构造必须走 `LLMEngineConfig` 的配方方法。结构体后紧跟一组 `static_assert`（[L80-L94](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/inferenceDims.h#L80-L94)）把每个字段的 `offsetof` 钉死——任何增删/重排字段都会编译失败，迫使开发者同步更新诊断名表与配方方法。

[`ShapeDim` 与 sym/fixed 助手](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/inferenceDims.h#L172-L194)：`symbol` 非空即符号维，`sym(&InferenceDims::batch)` 造符号维，`fixed(2)` 造固定维。

配方方法长什么样？以 `decodeDims` 为例：

[`decodeDims` 把 seqLen 钉成 1](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/llmEngineConfig.cpp#L823-L836)：decode 阶段每步只处理 1 个 token，故 `seqLen=1`；`startIndexLen=batch`（每条序列各自记起始偏移）。对比 [`prefillDims`（L796-L821）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/llmEngineConfig.cpp#L796-L821)，prefill 的 `seqLen` 等于真实 prompt 长度，且 `startIndexLen` 在「空缓存」时取 `0`（哨兵），否则取 `batch`。这两个方法产出的 `InferenceDims`，正是 `bindAll` 解析符号维的依据。

再看 `TensorRegistry` 本体。`TensorSpec` 结构：

[`TensorSpec` 字段](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.h#L48-L55)：`name`（可含 `%d`）、`io`、`dtype`、`shape` 模板、`perLayer`（>0 表示逐层展开 0..perLayer-1）。

[逐层模板展开 expandPerLayer](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.cpp#L43-L62)：当 `perLayer>0`，用 `snprintf` 把名字里的 `%d` 替换成 `0..perLayer-1`，生成 N 条具体 spec。这让「64 层 KV 缓存」只需登记一条带 `%d` 的模板。

`bindAll` 是注册表的心脏：

[bindAll 主循环](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.cpp#L135-L169)：遍历展开后的 spec，取地址 → `setTensorAddress`；若是输入，`resolveShape` 解析形状 → `setInputShape`。注释点出一个关键点：KV 缓存张量按 `maxBatchSize` 分配，但执行时需要把输入形状设成 `activeBatchSize`，这正是符号维 `batch` 的作用。

最后看「注册表从哪来」——`registryBuilder`。以 LLM 为例：

[构建 LLM 注册表：核心 I/O](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L57-L66)：`inputs_embeds` 形状是 `[batch, seqLen, hiddenSize]`，前两维符号、最后一维固定。

[逐层 KV 缓存 spec（混合路由）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L106-L122)：遍历绝对层索引，按 `layerTypes` 分流——attention 层登记 5D 的合并 KV `[batch, 2, numKVHeads, kvLen, headDim]`，mamba 层登记 recurrent/conv state（见 [L123-L162](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L123-L162)）。注意 `%d` 后缀用的是**局部**索引（attention 内 0..numAttn-1），与引擎绑定名约定一致。

[LoRA 故意跳过](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L211-L218)：LoRA 绑定名依赖具体模型哪几层挂了 adapter，编译期无法确定，故不在注册表登记，改由 `EngineExecutor` 在运行时通过引擎内省 + `TensorMap` 兜底处理（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：追踪一个符号维从「配方方法赋值」到「注册表解析」的完整旅程，确认 `batch` 这个符号在 decode 一步里被解析成了什么。

**操作步骤**：

1. 从 [llmEngineConfig.cpp:823 decodeDims](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/llmEngineConfig.cpp#L823-L836) 读出 `batch` 字段被设为入参 `batch`（例如 4）。
2. 到 [registryBuilder.cpp:58 inputs_embeds spec](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L57-L59)，确认 `inputs_embeds` 的第 0 维是 `sym(&InferenceDims::batch)`。
3. 到 [tensorRegistry.cpp resolveShape L123-L133](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.cpp#L123-L133)，确认解析式 `inferenceDims.*(shape[i].symbol)` 在 `batch=4` 时给出 `dims.d[0]=4`。
4. 到 [bindAll L154-L166](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/tensorRegistry.cpp#L154-L166)，确认 `setInputShape("inputs_embeds", {4, 1, hiddenSize})`。

**需要观察的现象**：符号 `batch` 在注册表里只是一个「指向 `InferenceDims::batch` 的指针」，没有任何写死的数字；它的具体值完全由当步传入的 `InferenceDims` 决定。

**预期结果**：能画出 `batch=4`、`seqLen=1` 时 `inputs_embeds` 的解析结果 `{4, 1, hiddenSize}`。

> 无 GPU 也可完成：纯源码追踪。

#### 4.2.5 小练习与答案

**练习 1**：为什么符号维用「指向成员的指针」而不是字符串（如 `"batch"`）？

**参考答案**：指向成员的指针在编译期类型安全（只能指向 `InferenceDims` 的真实字段）、解析时是一次解引用、零字符串查找开销；且 `static_assert(offsetof ...)` 能在字段重排时立刻报错。字符串方案需要运行期查表、易拼错、且改名时静默失效。

**练习 2**：`inputs_embeds` 的形状模板是 `[batch, seqLen, hiddenSize]`，其中 `hiddenSize` 是 `fixed`。为什么 hiddenSize 不做成符号维？

**参考答案**：`hiddenSize` 是模型架构常量，在一次引擎的全生命周期里从不变，写死成固定维即可；把它塞进 `InferenceDims` 会无谓扩大每步都要赋值的符号集。`InferenceDims` 只收录真正随步骤/批次变化的维度。

**练习 3**：一个有 32 个 attention 层的模型，注册表里 `past_key_values_%d` 这条模板会展开成多少条具体 spec？

**参考答案**：32 条（`perLayer` 在 [registryBuilder KV 段](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L113-L121) 按层数循环逐层 `addTensor`，名字用 `localAttnIdx` 拼接；deepstack 则用真正的 `perLayer=cfg.numDeepstackFeatures` 模板由 `expandPerLayer` 展开）。展开结果被缓存（`mDirty` 标志），不会每步重算。

---

### 4.3 EngineExecutor：prepare/execute 双阶段执行与 profile 切换

#### 4.3.1 概念说明

`EngineExecutor` 是把 `TensorRegistry`、`TensorMap`、`InferenceDims` 与 TensorRT runtime 串起来的「执行器」。它在头注释里被明确描述为「对模型、阶段、特性一无所知的薄封装（约 300 行）」——它取代了旧的 `LLMEngineRunner` 与 `EagleDraftEngineRunner` 两个角色，成为所有 LLM 推理（vanilla、投机解码 base/draft、多模态 talker/code-predictor）通用的引擎驱动器。

它持有四样东西：一个 `IRuntime`、一个 `ICudaEngine`、一个 `IExecutionContext`，以及一份 `TensorRegistry`。此外还维护一个以「绑定哈希」为键的 CUDA graph 缓存。对外只暴露三个动作：

- `prepare(profileIndex, dims, map, stream)`：切 profile + 解析形状 + 绑定全部张量。
- `execute(stream)`：命中缓存图就 `cudaGraphLaunch`，否则 `enqueueV3`。
- `captureGraph(stream)`：为当前绑定状态录制一张 CUDA graph 入缓存。

**双阶段执行的直觉**：一次请求的推理分 prefill（长 prompt，一次）和 decode（逐 token，多次）。两者形状差异巨大，分别对应 profile 0 与 profile 1。`EngineExecutor` 不创建两个上下文，而是**复用同一个 `IExecutionContext`**，在每步用 `setOptimizationProfileAsync` 切换它当前使用哪套 profile——这正是本讲实践任务的核心。

#### 4.3.2 核心流程

**构造期**：工厂方法 `createForLLM`（vanilla 或投机解码 base 引擎）或 `createForDraft`（投机解码 draft 引擎，按 `specDecodeMode` 在 EAGLE/MTP/DFlash/Gemma4-MTP 间分发）选好 `TensorRegistry`，然后构造函数反序列化引擎、用 `kUSER_MANAGED` 策略建上下文（这样上下文显存可在多个 runner 间共享）。

**prepare 的五步**：

```
prepare(profileIndex, dims, map, stream):
  1. firstInvalidMember(dims, registry.referencedMembers())  # 只校验引擎真用到的符号维 > 0
  2. mContext.setOptimizationProfileAsync(profileIndex, stream)  # 切 profile（关键）
  3. registry.bindAll(ctx, map, dims)   # 注册表管理的张量：地址 + 形状
  4. for 每个引擎 I/O 张量 not in registry:   # 兜底：LoRA 权重、每步变形的 kvcache_start_index 等
         tensor = map.get(name); setTensorAddress(name, tensor.rawPointer())
         if 输入: setInputShape(name, tensor.getTRTDims())
  5. return true
```

**execute 的两路**：算绑定哈希 → 查 graph 缓存 → 若缓存图存在且快照完全一致，`cudaGraphLaunch`；否则 `enqueueV3`。`captureGraph` 则在 warmup 后录制图，连同「绑定快照」一起以哈希为键存入缓存。

#### 4.3.3 源码精读

**构造函数**：[反序列化引擎 + USER_MANAGED 上下文](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L74-L102)。注释说明用 `kUSER_MANAGED` 是为了「让上下文显存可跨 runner 共享」。随后还会按需补登两个可选的投机解码树元数据张量（`kTreeParentIds/kTreeDepths`）。

**工厂分发**：[createForDraft 按 specDecodeMode 选不同注册表](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L111-L136)——draft 引擎的绑定布局随策略而变（EAGLE/MTP 树形 vs DFlash 块状 vs Gemma4-MTP 共享 base KV），所以注册表也不同。

**profile 切换（本讲核心）**：[prepare 主体](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L154-L176)。关键三行在 L166-L176：先 `setOptimizationProfileAsync(profileIndex, stream)` 切 profile，再 `bindAll` 绑定。注意 L156-L164 的校验：`firstInvalidMember` 只检查「注册表真正引用的」符号维是否为正——因为不是每个引擎都用全部 9 个符号维（如无打包掩码的模型不必设 `packedMaskLen`）。其中 `startIndexLen` 与 `specVerifyPhaseLen` 允许为 0（见 [inferenceDims.h kZeroAllowedMembers](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/config/inferenceDims.h#L124-L127)），因为 0 是引擎有意义的哨兵值。

**兜底绑定循环（LoRA 与每步变形张量的归宿）**：[prepare 的 for 循环](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L189-L221)。这里有一段非常重要的注释：**TRT 在 `setOptimizationProfileAsync` 时不会清空绑定——形状和地址在 profile 切换与 `execute` 调用之间会持续保留**。因此那些「不在注册表、且形状每步变化」的张量（典型如 `kvcache_start_index`，prefill 用 `[0]`、decode 用 `[batch]`）必须**每次调用都重新设地址+形状**，否则会沿用上一步的形状。LoRA 权重也走这条路（由 `LoRAManager::refreshTensorMap` 在 `prepare` 前填入 `TensorMap`）。循环里对找不到的张量会给出明确的错误指引（L201-L206）。

**execute 与 graph 缓存**：[execute 主体](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L226-L245)：算哈希 → 查缓存 → `snapshotBindings()` 比对 → 命中则 `cudaGraphLaunch`，失败回退 `enqueueV3`。

**绑定哈希与快照**：[computeBindingHash](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L369-L387) 把所有绑定的「地址 + 形状」揉成一个哈希键；[snapshotBindings](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L389-L402) 存完整快照。`execute` 不只比哈希，还要 `snapshot == 缓存快照` 才回放——这既防哈希碰撞，也防「绑定在 capture 之后被偷偷改了」。

**上下文显存**：[getRequiredContextMemorySize 用全 profile 最大值](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L291-L303)。注释点明：投机解码 base 引擎有多个 profile（prefill + verification），显存需求不同，按单 profile 算会少分配，故用 `getDeviceMemorySizeV2()` 取所有 profile 的最大值。`setContextMemory` 再把共享显存挂到上下文。

**真实调用点**（prefill 与 decode 如何用同一执行器）：

- profile 常量：[kPrefillProfile=0, kDecodeProfile=1](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L57-L58)。
- prefill：[用 profile 0 prepare + execute](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1413-L1417)。
- decode 的 graph 捕获：[用 profile 1 prepare + captureGraph](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1512-L1516)。

可以看到：prefill 和 decode 用的是**同一个 `mBaseExecutor`**（同一 context），只是 `prepare` 传入的 profileIndex 不同（0 vs 1）。

**LoRA 动态切换如何借助 graph 缓存**：[captureBaseGraphWithLoraFanout](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1497-L1524) 对每个 adapter 名（含「空 adapter」）做 `switchWeights → refreshTensorMap → prepare(kDecodeProfile) → captureGraph`。由于 `computeBindingHash` 把 LoRA 权重地址也算进哈希，不同 adapter 的绑定哈希不同，于是各自录得一张独立 graph，缓存里和平共处。推理时 `execute` 按当前 LoRA 的哈希自动选对 graph 回放——这就是「同引擎、多 LoRA、零重捕获」的实现秘诀。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：在 `engineExecutor.cpp` 中定位 profile 切换相关代码，解释「一次 prefill 后切到 decode profile 时，引擎如何复用同一个 context」。

**操作步骤**：

1. 打开 [engineExecutor.cpp 构造函数 L74-L102](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L74-L102)，确认 `mContext` 在此**创建一次**（`createExecutionContext(kUSER_MANAGED)`），此后全程复用、从不重建。
2. 打开 [prepare L154-L176](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L154-L176)，定位 L166 的 `mContext->setOptimizationProfileAsync(profileIndex, stream)`。这就是「切 profile」的全部——它只是告诉这个 context「接下来用第 `profileIndex` 号 profile 的内核」，并不产生新 context。
3. 阅读 [L178-L221 的兜底循环注释](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L178-L221)，理解「TRT 不在切 profile 时清绑定」，所以每步都要重新 `setTensorAddress/setInputShape`。
4. 对照真实调用：prefill 走 [kPrefillProfile(0)](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1413-L1417)、decode 走 [kDecodeProfile(1)](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1512-L1516)，确认两者用的是同一个 `mBaseExecutor`。

**需要观察的现象 / 待本地验证**：

- 现象上，prefill 与 decode 之间没有引擎/上下文的析构与重建，只有 `setOptimizationProfileAsync` 的 profile 号从 0 变 1。
- prefill 用的内核针对「长序列」选优，decode 用的内核针对「单 token」选优——同一 context、不同 profile，各取所需。
- 你应当能解释：之所以能复用 context，是因为 TensorRT 的 `IExecutionContext` 本就支持多 profile，切换只是改「当前生效的 profile 指针」；而 EdgeLLM 每次 `prepare` 都重新绑定形状/地址，恰好规避了「绑定跨 profile 残留」的陷阱。

**预期结果**：用自己的话写出「同一 `mContext`、靠 `setOptimizationProfileAsync(0/1)` 切换、每步 `prepare` 重绑」这三句话，并指出 L166 是切换点、L178-L221 的注释解释了为何每步都要重绑。

> 本实践为源码阅读型，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：`execute()` 里，为什么除了比「绑定哈希」还要比「绑定快照」才回放 CUDA graph？

**参考答案**：哈希存在碰撞可能；更重要的是，绑定（地址/形状）可能在 `captureGraph` 之后被改动。只有当「当前快照」与「录制时的快照」逐字节一致，graph 回放才安全。快照比对是正确性兜底，哈希只是快速定位候选。

**练习 2**：若一个张量既不在 `TensorRegistry`、也不在 `TensorMap`，`prepare` 会怎样？

**参考答案**：在兜底循环（[L198-L206](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L198-L206)）里 `map.get(name)` 返回 `nullptr`，记 `LOG_ERROR` 并返回 false——引擎的每个 I/O 张量都必须由注册表、LoRA 或显式 TensorMap 条目之一覆盖。

**练习 3**：LoRA 权重为何不写进 `TensorRegistry`？运行时它怎么被绑定的？

**参考答案**：LoRA 绑定名取决于具体模型哪些层挂了 adapter，构建期（`registryBuilder`）无法确定，故显式跳过（[注释 L211-L218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/registryBuilder.cpp#L211-L218)）。运行时由 `LoRAManager::refreshTensorMap` 把当前 adapter 的权重（或 dummy）填进 `TensorMap`，再由 `prepare` 的兜底循环经引擎内省逐个绑定。多 LoRA 通过「绑定哈希含权重地址 → 各录一张 graph」实现零成本切换。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，复述一次 vanilla LLM 的 decode 一步里，数据如何从「张量缓冲」流到「TensorRT 执行」，并标注每一步分别用到 `TensorMap` / `TensorRegistry` / `InferenceDims` / `EngineExecutor` 的哪个环节。

**建议产出**：一张文字版时序图，包含以下节点（可按需补充）：

1. `LLMEngineConfig::decodeDims(batch)` 产出当步 `InferenceDims`（`seqLen=1, startIndexLen=batch`）。→ 用到 *InferenceDims / 配方方法*。
2. `buildTensorMap`（请求开始时已填好）提供 `inputs_embeds/logits/逐层 KV/kvcache_start_index` 的地址。→ 用到 *TensorMap*。
3. `EngineExecutor::prepare(kDecodeProfile=1, dims, map, stream)`：
   - `setOptimizationProfileAsync(1)` 切到 decode profile；→ *EngineExecutor profile 切换*。
   - `TensorRegistry::bindAll`：用 `InferenceDims` 解析符号维，`setTensorAddress + setInputShape`。→ *TensorRegistry + InferenceDims*。
   - 兜底循环重绑 `kvcache_start_index`（形状从 prefill 的 `[0]` 变 `[batch]`）等非注册表张量。→ *EngineExecutor 兜底*。
4. `EngineExecutor::execute(stream)`：算绑定哈希 → 命中 decode 的缓存 graph → `cudaGraphLaunch`（首步未捕获时回退 `enqueueV3`）。→ *graph 缓存*。

**检查点（自查）**：

- 你能否说清「KV 缓存归谁所有」？（答：归 `SharedResources::cacheManagers`，不在 `EngineExecutor`；执行器只通过 `TensorMap` 拿到它的地址去读写。）
- 你能否解释为何 `past/present` KV 绑同一地址？（答：attention 插件就地更新。）
- 你能否指出 decode 与 prefill 复用同一 context 的那行代码？（答：[engineExecutor.cpp:166](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/exec/engineExecutor.cpp#L166) 的 `setOptimizationProfileAsync`。）

> 如本机有已构建好的 engine，可在 `unittests/engineExecutorTest.cpp` 之类的测试里加一条日志，打印每次 `prepare` 的 `profileIndex` 与 `toString(dims)`，实地观察 prefill→decode 的 profile 切换；否则按上述源码阅读完成即可，相关运行结果标注「待本地验证」。

## 6. 本讲小结

- `TensorMap` 是一张**纯地址表**（名字 → `Tensor*`，非拥有），不参与形状解析；它删除右值 `set` 以防悬空指针，并把 KV 缓存的 `past/present` 绑到同一地址（插件就地更新）。
- `TensorRegistry` 是**声明式绑定规格**：用 `TensorSpec` + 符号形状模板（`ShapeDim` 指向 `InferenceDims` 的成员指针）描述引擎布局，`bindAll()` 一次性完成 `setTensorAddress + setInputShape`；逐层 `%d` 模板按 `perLayer` 展开。
- `InferenceDims`（9 个符号维、`static_assert` 钉死布局）由 `LLMEngineConfig` 的配方方法（`prefillDims/decodeDims/...`）产出，是符号维解析的唯一来源；`firstInvalidMember` 只校验引擎真正引用的维度。
- `EngineExecutor` 是「对模型一无所知」的薄封装，靠 `prepare/execute/captureGraph` 三件套驱动 TensorRT；它**复用同一个 `IExecutionContext`**，用 `setOptimizationProfileAsync(0/1)` 在 prefill/decode 间切换，因 TRT 切 profile 不清绑定，故每步都重新绑定。
- `execute` 用「绑定哈希 + 完整快照」做 CUDA graph 缓存命中判定；LoRA 权重不进注册表，而由 `LoRAManager` 填 `TensorMap` 后走兜底绑定，多 LoRA 借助「哈希含权重地址」各录一张 graph 实现零成本切换。
- KV 缓存与上下文显存都不归 `EngineExecutor` 独占：KV 缓存属于 `SharedResources`，上下文显存用 `kUSER_MANAGED` 以便跨 runner 共享，容量按所有 profile 的最大值分配。

## 7. 下一步学习建议

- **深入解码策略**：本讲的 `EngineExecutor` 是「引擎驱动器」，但它不决定「这一步该 prefill、propose 还是 verify」。那套调度逻辑在 `DecodingStrategy` 层——建议接着读 **u5-l4（解码策略与解码器注册表）**，看 `vanillaDecoder` / `eagleDecoder` 等如何编排 base/draft 两个 `EngineExecutor` 的 `prepare/execute`。
- **理解缓存归属**：若想彻底弄清 KV 缓存如何被 `buildTensorMap` 取用、又如何被 attention 插件就地更新，接着读 **u5-l5（KV 缓存与混合缓存管理）**。
- **向上回看**：可回头对照 **u5-l1** 的 `handleRequest` 三阶段，确认本讲的 `prepare/execute` 正好嵌在 prefill 与 decode 循环里；以及 **u4-l2** 的双 profile 构建侧——本讲运行时切换的 profile 0/1，正是那里 `setupLLMOptimizationProfiles` 登记的。
- **动手验证（可选）**：阅读 `cpp/runtime/exec/` 目录下的 `unittests`（如 `engineExecutorTest.cpp`），看单元测试如何在无完整模型的情况下验证 `prepare/execute` 与 graph 缓存行为。
