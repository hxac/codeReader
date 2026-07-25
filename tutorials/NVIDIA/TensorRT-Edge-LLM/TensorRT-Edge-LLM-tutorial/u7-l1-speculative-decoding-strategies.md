# 投机解码策略（EAGLE / MTP / DFlash / Gemma4-MTP）

## 1. 本讲目标

本讲是 u5-l4「解码策略与解码器注册表」的延续。在 u5-l4 里我们已经看到：运行时主循环只调用 `DecodingStrategy::decodeStep`，而 vanilla 与投机解码的差异全部藏在策略子类里。本讲就打开四种投机解码策略的 `decodeStep` 黑盒，学完后你应当能够：

- 说出 EAGLE 的「树形提议 + 树注意力验证」是怎么在 GPU 上一次跑完一棵候选树的；
- 说明 MTP 作为「最简多头预测」的概念定位，并理解它在当前运行时里为何与 EAGLE 共享同一套树形代码；
- 区分 DFlash 的「块状 / Jacobi 提议」与 EAGLE 的「深度优先树」，以及它的线性 / DDTree 两种验证形态；
- 解释 Gemma4-MTP 为何是唯一「与目标模型共享 KV 缓存」的策略，以及它的顺序链式接受算法；
- 沿「草稿如何提议 / base 如何验证 / KV 是否共享」三个维度，独立画出四种解码器的对比表。

## 2. 前置知识

- **投机解码（speculative decoding）的直觉。** 普通自回归解码每生成一个 token 都要跑一次完整的大模型（base/target）前向。投机解码额外引入一个很小的「草稿模型（draft）」，让它先廉价地猜出接下来的若干个候选 token，再让大模型**一次性**把这若干个候选全部验证。只要草稿猜得对，多个 token 就在一次大模型前向里被确认，从而用一次大模型前向换出多个 token。
- **验证与接受（accept）。** 投机解码几乎都采用「贪心验证」：base 在每个候选位置取 argmax，如果和草稿候选一致就「接受」并继续往后走，一旦不一致就停在该位置、采用 base 自己的 argmax。于是每轮真正产出的 token 数 = 接受长度 \( a \)，接受长度服从以命中率 \( p \) 为参数的几何分布，期望接受长度

\[
\mathbb{E}[a] = \frac{1 - p^{d+1}}{1 - p} \quad (\text{草稿深度 } d)
\]

只要 \( p \) 不太低，期望接受长度就显著大于 1，加速就成立。
- **base / draft 双引擎与双 KV。** 三个策略（EAGLE / MTP / DFlash）各自拥有一份独立的草稿 KV 缓存（`mDraftCacheManager`），base 另有一份 KV；只有 Gemma4-MTP 不持有草稿 KV，而是零拷贝复用 base 的 KV。
- **承接 u5-l4 的抽象。** `DecodingStrategy` 是无状态基类，`decodeStep` 是每轮唯一入口；运行时主循环在每一步通过 `DecoderRegistry::select` 决定用 vanilla 还是投机策略，与本讲算法无关。

## 3. 本讲源码地图

本讲涉及的源码全部集中在运行时的解码子包：

| 文件 | 作用 |
| --- | --- |
| `cpp/runtime/decoding/decodingStrategy.h` | `DecodingStrategyKind` 枚举与抽象基类（u5-l4 已讲） |
| `cpp/runtime/decoding/decoderRegistry.cpp` | 按 `SpecDecodeMode` 分发到四种解码器之一 |
| `cpp/runtime/decoding/decoderUtils.cpp` | 四种解码器共用的工具：加载 draft 引擎、把接受结果写回 host |
| `cpp/runtime/decoding/eagleDecoder.{h,cpp}` | EAGLE 树形投机解码 |
| `cpp/runtime/decoding/mtpDecoder.{h,cpp}` | MTP 多头预测投机解码 |
| `cpp/runtime/decoding/dflashDecoder.{h,cpp}` | DFlash 块状 / DDTree 投机解码 |
| `cpp/runtime/decoding/gemma4MTPDecoder.{h,cpp}` | Gemma4 assistant MTP（与目标共享 KV） |

> 说明：EAGLE / MTP / DFlash / Gemma4 四种解码器都 `final : public DecodingStrategy`，都实现了 `DecodingStrategy` 的 13 个纯虚方法；本讲只聚焦最能体现彼此差异的 `decodeStep` 及其拆出的几个私有阶段函数。

## 4. 核心概念与源码讲解

### 4.1 公共骨架与「三维度」对比框架

#### 4.1.1 概念说明

四种解码器虽然算法不同，但都遵循同一条骨架：**草稿提议 → base 验证 → 接受并提交 KV → 把接受的 token 写回上下文**。差异只体现在三个维度上。我们先把这个公共骨架和三维度框架立起来，后面每个解码器都往这个框架里填。

三个对比维度：

1. **草稿如何提议**：草稿模型一次前向产出候选的「形状」——是一条链、一棵树、还是一个块（block）。
2. **base 如何验证**：base 一次前向吃下所有候选，用什么形状的注意力掩码（全树掩码 / 线性因果掩码 / 顺序链），以及用哪个接受 kernel。
3. **KV 是否共享**：草稿是否拥有自己的 KV 缓存，还是零拷贝复用 base 的 KV。

#### 4.1.2 核心流程

四者共有的阶段函数（命名略有不同）：

```text
decodeStep():
  1. 草稿准备 / 预填充      （draft 引擎，decode 或 prefill profile）
  2. 构造草稿提议           （链 / 树 / 块 + 对应注意力掩码）
  3. base 验证              （base 引擎，一次前向吃下全部候选）
  4. 接受 + 提交 KV         （accept kernel + commitSequenceLength）
  5. 写回 host              （decoder_utils::appendAcceptedTokens，唯一一次 D2H 同步）
```

四个解码器在阶段 5 都复用同一个工具函数：它把设备上的接受长度和接受 token 拷回 host，再 push 进 `context.tokenIds`，并处理 EOS / stop 串中途截断。这就是每轮唯一的 host↔device 同步点。

#### 4.1.3 源码精读

分发入口在 `DecoderRegistry` 构造期，按 `specDecodeMode()` 选择具体子类，vanilla 永远无条件保留：

- [decoderRegistry.cpp:L39-L60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L39-L60)：`switch (runtime.deployment.specDecodeMode())` 把 `kEAGLE / kMTP / kDFlash / kGemma4MTP` 分别 new 成对应解码器。
- [decoderRegistry.cpp:L64-L72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L64-L72)：逐请求的 `select()` 只看 `disableSpecDecode`，回退到 vanilla——与具体算法完全解耦。

公共写回工具：

- [decoderUtils.cpp:L53-L92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderUtils.cpp#L53-L92)：`appendAcceptedTokens`——`cudaStreamSynchronize` 后按接受长度逐 slot `push_back`，EOS 中途断则把已追加计数写回 `hostAcceptLengths`，供后续 logprobs 收集跳过无效行。

#### 4.1.4 代码实践

**实践目标**：在动手逐个解码器之前，先建立「分发—选择—公共写回」这条骨架的肌肉记忆。

1. 打开 `decoderRegistry.cpp`，确认四种 `SpecDecodeMode` 各自 new 出哪个类。
2. 打开 `decoderUtils.cpp` 的 `appendAcceptedTokens`，找到唯一的 `cudaStreamSynchronize`，并解释为什么这是「每轮唯一的 D2H 同步点」。
3. **需要观察的现象**：四个解码器文件里都各自有一处调用 `decoder_utils::appendAcceptedTokens`；在编辑器里全局搜索该符号，应能命中 4 处（eagle / mtp / dflash / gemma4 各一处）。
4. **预期结果**：你应当能复述「无论哪种投机算法，最终都汇聚到同一个写回函数」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DecoderRegistry::select` 标了 `noexcept` 且不抛异常？

> **答案**：它只做一次指针判空 + 一次布尔判断（`disableSpecDecode`），没有任何可能失败的资源分配或 IO；让它 `noexcept` 是为了让主循环在「逐请求切换策略」时无需 try/catch，调用开销最小。参考 [decoderRegistry.cpp:L64-L72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderRegistry.cpp#L64-L72)。

**练习 2**：`appendAcceptedTokens` 在 EOS 处中途截断后，为什么要把截断后的 `appended` 写回 `hostAcceptLengths`？

> **答案**：截断后该 slot 实际产出的 token 数小于设备上的 `acceptLength`，若不回写，后续 `collectSpecLogprobsFromHost` 会去读 verify 图里已超出序列末尾的无效行，导致 logprobs 越界。参考 [decoderUtils.cpp:L80-L91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/decoderUtils.cpp#L80-L91)。

---

### 4.2 EAGLE：树形提议与 base 验证

#### 4.2.1 概念说明

EAGLE 是最经典的「自回归草稿 + 树形验证」投机解码。它的核心思想有两点：

- **草稿条件于 base 的隐状态**：EAGLE 草稿不只吃上一个 token 的 embedding，还吃 base 模型最后一层的隐状态。这让小模型能「看到」大模型在想什么，命中率显著提高。
- **草稿按深度优先扩展成一棵候选树**：每一层取 `draftTopK` 个候选，向下扩展 `draftingStep` 层，形成一棵候选树；最后从所有节点里挑出总得分最高的 `verifySize` 个，重新组织成一棵「验证树」交给 base 一次验证。

base 验证时用「树注意力掩码（tree attention mask）」：树里每个节点只 attend 到它到根路径上的祖先节点。验证完用 `eagleAccept` 自根向下匹配 base 的 argmax，接受到第一个不匹配处为止。

EAGLE 草稿自己有一份独立的 KV 缓存（`mDraftCacheManager`），与 base 的 KV 完全分离。

#### 4.2.2 核心流程

EAGLE 的 `decodeStep` 分四个阶段，且首尾各多一步「草稿预填充 / 草稿接受」：

```text
decodeStep():
  if 首轮(generationRound==0): runDraftModelPrefill   # 草稿对 prefill 隐状态做一次预填充
  else:                       runDraftModelAcceptToken # 草稿对上轮接受结果做一次前向
  constructDraftProposal   # 扩展候选树 → 选 verifySize 节点 → 构造验证树 + 树掩码
  runBaseModelVerification # base 一次前向验证整棵树 → eagleAccept → 提交 KV
```

注意：EAGLE 草稿的输入不是 token id，而是 base 上一轮产出的隐状态（`baseHiddenStates`），所以 `runDraftModelPrefill` 与 `runDraftModelAcceptToken` 都要把 base 隐状态摆到草稿引擎输入位上。

#### 4.2.3 源码精读

- [eagleDecoder.cpp:L144-L172](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L144-L172)：`decodeStep` 主干，四阶段串联。

- [eagleDecoder.cpp:L174-L266](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L174-L266)：`runDraftModelPrefill`。第 220-233 行把 prefill 的 token ids 做 embedding lookup 写到 `inputsEmbeds`；关键点是草稿吃的是 base 隐状态而非 token——这段在为草稿准备 embedding 输入的同时，前面已经校验过 `baseHiddenStates` 形状（第 192-194 行）。

- [eagleDecoder.cpp:L344-L416](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L344-L416)：`constructDraftProposal` 的核心循环。`for (round ... draftingStep - 1)` 每轮：草稿前向 → `selectAllTopK` 取每节点 `draftTopK` 候选 → `computeCuScoresAndTranslateToken` 累计得分 → `updateDraftTreeFullTables` 把新候选写进全表（含 `mDraftTokenPredecessorFullTable` 父指针）。这就是「逐层把树长出来」。

- [eagleDecoder.cpp:L432-L433](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L432-L433)：`constructVerificationDraftTree`——从全表里挑出 `verifySize` 个得分最高节点，重组成验证树并生成树注意力掩码。

- [eagleDecoder.cpp:L514-L516](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L514-L516)：base 验证后的 `eagleAccept`——在树掩码约束下自根向下匹配 base argmax，输出接受 token / 接受节点索引 / 接受长度。

- [eagleDecoder.cpp:L528-L536](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L528-L536)：`eagleBaseCommitKVCache` 把被接受节点对应的 KV 提交进 base 缓存，`eagleBaseAssembleHiddenState` 把接受节点的隐状态拼出来供下一轮草稿用，最后 `commitSequenceLength`。

草稿树结构相关张量声明在头文件，父指针表是树的灵魂：

- [eagleDecoder.h:L84-L99](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.h#L84-L99)：`mDraftTokenIdsFullTable`（节点 token）、`mDraftTokenScoreFullTable`（累计得分）、`mDraftTokenPredecessorFullTable`（父节点索引）三张表共同表达一棵树。
- [eagleDecoder.h:L77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.h#L77)：`HybridCacheManager& mDraftCacheManager`——EAGLE 拥有独立草稿 KV。

#### 4.2.4 代码实践

**实践目标**：通过阅读接受算法的单元测试，在不需要真实引擎的情况下理解 `eagleAccept` 的「树掩码 + 贪心匹配」行为。

1. 打开 [unittests/eagleAcceptTests.cpp:L49-L60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L49-L60)，看 `runEagleAcceptTest` 的签名：它接收合成的 `tokenIds`（候选树）、`attentionMask`（树掩码）、`logits`（base 各节点输出），再校验返回的接受结果。
2. 构造一个最小例子：根 token = A，两个子节点 B、C（即树掩码只允许根→B、根→C）。让 base logits 在 A 位置 argmax = B，在 B 位置 argmax = 某个新 token X。
3. **需要观察的现象**：接受应当从 A→B 命中后继续走到 B 的输出 X；C 因为不在已接受路径上被忽略。
4. **预期结果**：接受长度 ≥ 2，接受的节点索引沿「根→B」一条路径。**待本地验证**（需要编译单测目标并跑 `EagleAcceptTest` 用例）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `constructDraftProposal` 要先用 `selectAllTopK` 从全表里挑 `verifySize` 个节点，再 `constructVerificationDraftTree` 重组？直接把整棵扩展树交给 base 不行吗？

> **答案**：扩展出来的全表节点数是 `draftingStep × draftTopK` 量级，可能远超 base 一次前向能高效处理的规模；挑得分最高的 `verifySize` 个并重组，是在「候选覆盖」与「base 验证算力」之间做权衡，保证一次 base 前向成本可控。

**练习 2**：EAGLE 的草稿输入是 base 隐状态，那草稿 KV 缓存（`mDraftCacheManager`）存的是什么？

> **答案**：存的是草稿模型自己在各层 attention 产生的 K/V。隐状态只是草稿「每一步的输入特征」，草稿仍然是一个有 self-attention 的网络，需要把自己的历史 K/V 缓存下来才能正确做因果注意力。

---

### 4.3 MTP：最简多头预测（当前与 EAGLE 共享实现）

#### 4.3.1 概念说明

MTP（Multi-Token Prediction）在概念上是「最简」的投机解码：用一个从 base 派生的、很浅的辅助头（draft）来预测接下来的若干 token，是 DeepSeek-V3 风格的设计。在 EdgeLLM 的导出侧（见 u7-l2），MTP 草稿不是一个独立检查点，而是从 base 模型派生出来（`mtp_num_hidden_layers`），所以它的训练 / 导出成本极低。

**关键澄清（务必注意）**：MTP 的 *概念* 是链式多头预测，但在 *当前运行时代码* 里，`MTPDecoder` 几乎完整复用了 EAGLE 的树形提议 + 树形接受代码路径。头文件的 TODO 注释明确记录了「把 MTP 简化成最简链式、把树形逻辑收敛进 EAGLE」的待办意图。也就是说：今天 MTP 在算法层面 ≈ EAGLE，差异主要在「草稿引擎是用 EAGLE3 训练的还是 MTP 派生的」。真正在代码里实现成「纯顺序链」的是 4.5 节的 Gemma4-MTP。

#### 4.3.2 核心流程

`MTPDecoder::decodeStep` 的阶段划分与 EAGLE 一字不差：

```text
decodeStep():
  if 首轮: runDraftModelPrefill   else: runDraftModelAcceptToken
  constructDraftProposal   # 与 EAGLE 相同的扩展树 + constructVerificationDraftTree
  runBaseModelVerification # 与 EAGLE 相同的 eagleAccept + commitKV
```

#### 4.3.3 源码精读

- [mtpDecoder.h:L33-L35](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.h#L33-L35)：TODO 注释——「Simplify MTPDecoder to deliver the simplest chain-based speculative drafting + verification. AR + tree-based proposal logic should live exclusively in EagleDecoder.」这正是「MTP 当前复用 EAGLE 树形代码、未来要简化成链」的直接证据。

- [mtpDecoder.cpp:L135-L163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L135-L163)：`decodeStep` 与 EAGLE 的对应函数逐行同构。

- [mtpDecoder.cpp:L264-L432](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L264-L432)：`constructDraftProposal`，与 [eagleDecoder.cpp:L268-L436](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L268-L436) 几乎逐行一致，同样以 [mtpDecoder.cpp:L428-L429](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L428-L429) 的 `constructVerificationDraftTree` 收尾。

- [mtpDecoder.cpp:L521-L523](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L521-L523)：`runBaseModelVerification` 同样用 `eagleAccept`——证实接受算法也复用。

> 细微差异：MTP 的 `runDraftModelPrefill` 在 embedding lookup 后多了一段 `if (mRuntime.preprocess.gemma4Ple)` 的 PLE 处理（见 mtpDecoder.cpp 第 225-228 行），除此之外与 EAGLE 无别。这只是预处理上的小适配，不改变算法。

#### 4.3.4 代码实践

**实践目标**：亲手验证「MTP 与 EAGLE 共享同一条代码路径」这一结论，而不是轻信文档。

1. 用编辑器或 `diff` 把 `eagleDecoder.cpp` 的 `constructDraftProposal`（L268-L436）与 `mtpDecoder.cpp` 的 `constructDraftProposal`（L264-L432）并排对比。
2. **需要观察的现象**：两者除了 NVTX 标签名和极少数预处理行，主体逻辑、kernel 调用顺序、张量布局几乎完全一致。
3. **预期结果**：你应当得出结论——在 *当前 HEAD* 上，从纯算法角度无法把 MTP 与 EAGLE 区分开；真正决定走 MTP 还是 EAGLE 的是构建期写入 `spec_base/spec_draft.engine` 的模型（导出侧的 base/draft 角色与变体解析，见 u7-l2）。
4. 在 [mtpDecoder.h:L33-L35](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.h#L33-L35) 的 TODO 旁记一句自己的理解：未来若该 TODO 落地，本节结论会如何变化。

#### 4.3.5 小练习与答案

**练习 1**：既然 MTP 和 EAGLE 在运行时代码上几乎相同，为什么还要分成两个类、两个 `SpecDecodeMode`？

> **答案**：因为它们对应不同的 *模型与导出契约*——EAGLE 草稿是独立训练的 EAGLE3 检查点，MTP 草稿是从 base 派生的浅层辅助头；构建器据此走不同的 profile 分支、导出侧做不同的权重 remap（见 u4-l2 与 u7-l2）。运行时分两个 `kind` 也让未来把 MTP 简化成纯链（TODO）时有清晰的落脚点。

**练习 2**：如果有一天 `mtpDecoder.h` 的 TODO 真的被实现成「最简链式」，运行时主循环需要改吗？

> **答案**：不需要。主循环只调 `decodeStep`，链式只是 `decodeStep` 内部把「扩展树」换成「顺序单步 + 线性因果掩码」，对外接口不变——这正是 u5-l4 抽象基类带来的解耦收益。

---

### 4.4 DFlash：块状 / Jacobi 提议与 DDTree

#### 4.4.1 概念说明

DFlash 走的是和 EAGLE 完全不同的提议范式——**块状（blockwise）**，思路更接近 Jacobi / blockwise parallel decoding：

- 草稿一次前向处理一整个「块」`blockSize` 个位置，其中第 0 个位置是上一个被接受的 token，其余位置全部填一个 **mask token**。草稿模型像做填空题一样，一次预测出这一块里每个位置的下一个 token。
- 因为草稿也条件于 base 的隐状态，DFlash 会把 base 上一轮的隐状态以「target hidden delta」的形式喂给草稿（草稿 KV 更新插件按 `delta_lengths` 跳过 padding 行）。

验证阶段有两种形态：

- **线性（linear）**：`candidateTopK == 1`，每个块位置只取 1 个候选，拼成一条线性因果「验证链」。
- **DDTree**：`mUseDDTree == true` 时，用 `ddtreeBuild` 在块输出的 logits 上构造一棵带 `parent_ids / depths / 树掩码` 的验证树。

两种形态最终都复用 `eagleAccept` 做接受。DFlash 也拥有独立的草稿 KV 缓存。

> 注意 `decoderRegistry.cpp` 里有个细节：选了 DFlash 时会**跳过 vanilla 的 CUDA graph 捕获**（第 81 行），因为 DFlash 的 draft / base 流程结构特殊，与 vanilla graph 不兼容。

#### 4.4.2 核心流程

```text
decodeStep():
  runDraftForward          # 草稿一次前向处理整个 blockSize 块（输入=[last_accepted, mask, mask, ...]）
  prepareDFlashVerifyInputs# linear:每块选1个→线性因果链; 或 DDTree:ddtreeBuild→验证树
  runBaseVerification      # base 一次前向验证 → eagleAccept → 提交 KV（DDTree 走 commitAcceptedTreePath）
```

#### 4.4.3 源码精读

- [dflashDecoder.cpp:L236-L259](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L236-L259)：`decodeStep` 三阶段。

- [dflashDecoder.cpp:L282-L290](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L282-L290)：块状输入的构造——`hostDraftInputIds[b*BS] = 上一个接受 token`，`hostDraftInputIds[b*BS+j] = mMaskTokenId`（j≥1）。这是「块状 / mask 填空」范式的直接证据。

- [dflashDecoder.cpp:L335-L347](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L335-L347)：`compactTargetHidden` 把 base 隐状态按 `deltaLen` 紧凑拷贝并绑到 `kDFlashTargetHiddenConcat`——DFlash 草稿条件于 base 隐状态的「delta」。

- [dflashDecoder.cpp:L398-L423](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L398-L423)：草稿引擎一次前向，输出 `[batch, blockSize, draftVocabSize]` 的 logits——一块所有位置一次算完。

- [dflashDecoder.cpp:L452-L478](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L452-L478)：`prepareDFlashVerifyInputs` 的分支——`!mUseDDTree` 走 `launchDFlashBuildLinearVerifyInputs`（线性），否则走 `buildTreeVerifyInputs`（DDTree）。

- [dflashDecoder.cpp:L522](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L522)：`ddtreeBuild`——构造验证树（输出 `specTreeParentIds / specTreeDepths / 树掩码`）。

- [dflashDecoder.cpp:L622-L624](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L622-L624)：base 验证后复用 `eagleAccept`——注释明确写了「DFlash reuses the EAGLE accept utility for both linear-tree and branching-tree verification」。

头文件字段区分两种模式与块参数：

- [dflashDecoder.h:L92-L95](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.h#L92-L95)：草稿吃隐状态而非 token 的张量（`mDraftInputsEmbeds / mDraftTargetHidden / mDraftOutputLogits`）。
- [dflashDecoder.h:L111-L121](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.h#L111-L121)：DDTree 专用张量（`mTreeTokenIds / mTreeNodeScores / mVerifyTreeMask` 等）。
- [dflashDecoder.h:L127-L135](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.h#L127-L135)：`mBlockSize / mVerifySize / mCandidateTopK / mUseDDTree` 等块参数。

#### 4.4.4 代码实践

**实践目标**：理解 DFlash 的「块状输入」与「线性 vs DDTree」分叉。

1. 在 [dflashDecoder.cpp:L282-L290](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L282-L290) 处，把 `blockSize`（BS）假想成 8，手画一行输入：`[last_accepted, MASK, MASK, MASK, MASK, MASK, MASK, MASK]`。
2. 跟踪 `prepareDFlashVerifyInputs`：当 `mCandidateTopK == 1` 且 `mUseDDTree == false` 时，每个块位置选 1 个 token，拼成一条 `[last_accepted, d1, d2, ..., d7]` 的线性验证链。
3. **需要观察的现象**：线性模式下 `mVerifyTreeMask` 是一个下三角因果掩码（每个位置只看自己之前的块位置）；DDTree 模式下掩码由 `ddtreeBuild` 生成，是树结构。
4. **预期结果**：你能讲清「为什么 DFlash 一次草稿前向就能产出 blockSize 个候选」（因为是并行填空，不是顺序展开）。

#### 4.4.5 小练习与答案

**练习 1**：EAGLE 的候选是「逐层深度优先」长出来的，DFlash 的候选是怎么一次产出的？

> **答案**：DFlash 把一整个块（`blockSize` 个位置，首位置是上一个接受 token、其余是 mask token）一次性喂给草稿，草稿并行地为每个位置预测下一个 token，等价于 Jacobi / 块状并行解码，所以一次草稿前向就得到一整块候选。参考 [dflashDecoder.cpp:L282-L290](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L282-L290)。

**练习 2**：DFlash 的 linear 模式与 DDTree 模式，base 验证时分别用什么注意力结构？

> **答案**：linear 模式（`candidateTopK==1`）用下三角因果掩码，等价于验证一条链；DDTree 模式用 `ddtreeBuild` 生成的树掩码（含 parent/depth），验证一棵树。两种都最终用 `eagleAccept` 接受。参考 [dflashDecoder.cpp:L452-L478](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L452-L478) 与 [dflashDecoder.cpp:L622-L624](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L622-L624)。

---

### 4.5 Gemma4-MTP：与目标模型共享 KV 的链式解码

#### 4.5.1 概念说明

Gemma4-MTP 是四种策略里最特殊的一种，特殊在两点：

1. **真正的顺序链**：草稿（assistant）按 `draftingStep` 一步一步地顺序生成，每步只产 1 个 token，把上一步输出喂给下一步——这是教科书式的链式提议，与 EAGLE/MTP 的树形扩展、DFlash 的块状都不同。
2. **与 base 共享 KV**：草稿**没有自己的 KV 缓存**，它的 `past_key_values_*` 输入是 base KV 缓存的零拷贝别名（通过 `kv_sharing_map` 选择）。这是 Gemma4 assistant 的特殊设计——它本质上是 base 模型的一个额外「预测头」，复用 base 已经算好的 KV，省下一整份草稿 KV 的显存。

验证阶段把 `[seed, draft1, draft2, ..., draftN]` 拼成一条链交给 base，用顺序链因果掩码，接受用专门的 `sequentialAccept`（不是 `eagleAccept`），因为链是线性连续的，可以直接逐位置比对、无需树掩码。

#### 4.5.2 核心流程

```text
decodeStep():
  prepareSeed           # 取上一个 token 作种子，从 base 隐状态 gather 出种子隐状态
  runAssistantDraftChain# for draftingStep 步：草稿单 token 前向 → top1 → 喂回下一步（共享 base KV）
  runBaseVerification   # base 一次前向验证 [seed, draft1..draftN] 链（因果掩码）
  acceptAndCommit       # sequentialAccept 逐位置接受 → 提交 base KV → 写回 host
  updateNextSeed        # 下一轮种子从 context.tokenIds.back() 读（隐含）
```

#### 4.5.3 源码精读

- [gemma4MTPDecoder.h:L33-L38](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.h#L33-L38)：头注释明说「assistant has no draft KV cache: its `past_key_values_*` inputs are zero-copy aliases to the base target KV cache」。

- [gemma4MTPDecoder.cpp:L62-L63](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L62-L63)：构造期强校验 `draft->sharesTargetKV && !draft->hasOwnKVCache`——程序级保证 Gemma4 草稿必须共享 base KV、不得自带 KV。第 66-67 行 `buildTensorMapForGemma4MTPDraft` 用 **base 的** `sharedResources` 构造草稿张量表，把草稿的 KV 输入直接绑到 base KV。

- [gemma4MTPDecoder.cpp:L103-L108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L103-L108)：`decodeStep` 是一条短路的 `&&` 链，五阶段顺序执行。

- [gemma4MTPDecoder.cpp:L393-L396](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L393-L396)：`prepareSeed` 用 `launchGemma4MTPGatherSeedHidden` 从 base 隐状态里 gather 出种子位置的隐状态——草稿同样条件于 base 隐状态。

- [gemma4MTPDecoder.cpp:L423-L461](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L423-L461)：`runAssistantDraftChain` 的顺序循环——每步草稿单 token 前向、`selectAllTopK` 取 top1、`launchGemma4MTPStoreDraftToken` 存草稿 token，再把输出隐状态拷回输入喂下一步。这就是「链」。

- [gemma4MTPDecoder.cpp:L514-L518](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L514-L518)：base 验证用 `launchDFlashPrepareBaseVerifyInputs` 准备线性因果掩码——链而非树。

- [gemma4MTPDecoder.cpp:L551-L552](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L551-L552)：`sequentialAccept`——顺序链接受 kernel，逐位置比对 base argmax。注释也指出 Gemma4 的 verify 行是连续的，logprobs 可直接读 logits 无需 gather（第 555-556 行）。

- [gemma4MTPDecoder.cpp:L603-L608](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L603-L608)：`updateNextSeed` 几乎是空的——下一轮种子直接从 `context.tokenIds.back()` 读，配合 `mHostAcceptLengths` 重新 gather 种子隐状态。

#### 4.5.4 代码实践

**实践目标**：定位 Gemma4-MTP 区别于其他三种的「两个唯一性」——共享 KV、顺序链接受。

1. 在 [gemma4MTPDecoder.cpp:L62-L63](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L62-L63) 确认 KV 共享校验；在头文件 [gemma4MTPDecoder.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.h) 全文里搜索 `mDraftCacheManager`，应当**找不到**（其他三种都有这个成员）。
2. 对比 [gemma4MTPDecoder.cpp:L551-L552](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L551-L552) 的 `sequentialAccept` 与 EAGLE/DFlash 的 `eagleAccept`：前者输入是线性链 + 因果掩码，后者输入是树 + 树掩码。
3. **需要观察的现象**：`runAssistantDraftChain` 是一个 `for (step < draftingStep)` 的串行循环（每步一次 draft 前向），而 DFlash 的草稿是一次前向处理一整块。
4. **预期结果**：你能解释为什么 Gemma4-MTP 适合「显存极其紧张、但又想要一点投机加速」的边缘场景——因为它不额外占一份草稿 KV。

#### 4.5.5 小练习与答案

**练习 1**：Gemma4-MTP 草稿没有自己的 KV，那它做 self-attention 时用什么当历史 K/V？

> **答案**：直接复用 base 的 KV 缓存——草稿的 `past_key_values_*` 输入被零拷贝别名到 base 的目标 KV（由 `kv_sharing_map` 选择）。这要求草稿与 base 共享同一套注意力结构，正是 Gemma4「assistant 即 base 的额外预测头」的设计前提。参考 [gemma4MTPDecoder.cpp:L62-L67](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L62-L67)。

**练习 2**：为什么 Gemma4-MTP 用 `sequentialAccept` 而不是 `eagleAccept`？

> **答案**：Gemma4-MTP 的候选是一条线性链 `[seed, draft1, ..., draftN]`，verify 行在输出 logits 里是连续排列的，逐位置贪心比对即可，不需要树掩码；`eagleAccept` 是为树形（非连续接受路径）设计的，多带了树掩码遍历逻辑。用专门的顺序接受 kernel 更简单高效。

---

## 5. 综合实践

**任务**：完成本讲规格要求的核心对比——沿「草稿如何提议 / base 如何验证 / KV 是否共享」三个维度，为四种解码器产出一张对比表，并写出每种适合的场景。

请按下面步骤独立完成（这是源码阅读型实践，不需要 GPU）：

1. **打开四个 `decodeStep`** 并把每个的阶段函数名抄下来：
   - EAGLE：[eagleDecoder.cpp:L144-L172](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/eagleDecoder.cpp#L144-L172)
   - MTP：[mtpDecoder.cpp:L135-L163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/mtpDecoder.cpp#L135-L163)
   - DFlash：[dflashDecoder.cpp:L236-L259](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/dflashDecoder.cpp#L236-L259)
   - Gemma4-MTP：[gemma4MTPDecoder.cpp:L103-L108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/gemma4MTPDecoder.cpp#L103-L108)

2. **填出对比表**（参考答案见下，但请先自己填）：

   | 维度 | EAGLE | MTP | DFlash | Gemma4-MTP |
   | --- | --- | --- | --- | --- |
   | 草稿如何提议 | 逐层深度优先扩展成候选树（`draftingStep × draftTopK`） | 概念上链式，**当前代码复用 EAGLE 树形**（TODO 待简化） | 块状/Jacobi：一次前向填一整个 `blockSize` 块（线性或 DDTree） | 顺序链：`draftingStep` 步、每步 1 个 token |
   | base 如何验证 | 树注意力掩码 + `eagleAccept` | 同 EAGLE（树掩码 + `eagleAccept`） | linear 因果掩码 或 DDTree 树掩码，均用 `eagleAccept` | 线性因果掩码 + `sequentialAccept` |
   | KV 是否共享 | 独立草稿 KV（`mDraftCacheManager`） | 独立草稿 KV | 独立草稿 KV | **共享 base KV**（无草稿 KV，`sharesTargetKV`） |

3. **写出适用场景**（参考思路）：
   - **EAGLE**：通用、追求最高加速比、显存允许两份 KV 的场景。
   - **MTP**：草稿从 base 派生、训练/导出成本敏感的场景；当前等价于「用 MTP 派生草稿的 EAGLE 树形路径」。
   - **DFlash**：希望一次草稿前向覆盖更多候选（块状）、或需要 DDTree 分支验证的场景。
   - **Gemma4-MTP**：显存极其紧张、只想要轻量投机加速、且 base 本身就是 Gemma4 的边缘场景——靠共享 KV 省下一份草稿 KV。

4. **进阶（可选）**：在 [unittests/eagleAcceptTests.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp) 里找到一个用例，描述它构造的树/掩码/logits，并手算预期的接受长度，然后**待本地验证**（编译并运行该 GTest 目标）。

## 6. 本讲小结

- 四种投机解码器都遵循「草稿提议 → base 验证 → 接受并提交 KV → 写回 host」的公共骨架，写回阶段统一复用 `decoder_utils::appendAcceptedTokens`，每轮只有这一次 D2H 同步。
- **EAGLE** = 草稿条件于 base 隐状态、逐层扩展候选树、base 用树注意力 + `eagleAccept` 验证、自带独立草稿 KV。
- **MTP** 概念上是最简链式多头预测，但**当前运行时代码与 EAGLE 共享同一套树形提议/接受**（头文件 TODO 记录了简化意图）；区分二者主要靠导出/构建期的草稿来源。
- **DFlash** = 块状/Jacobi 提议（一次前向填一整块 mask token）、支持 linear 与 DDTree 两种验证形态、最终也复用 `eagleAccept`、自带独立草稿 KV。
- **Gemma4-MTP** = 真正的顺序链（每步 1 token）、base 用线性因果掩码 + `sequentialAccept` 接受、**唯一与 base 共享 KV**（构造期强校验 `sharesTargetKV && !hasOwnKVCache`）。
- 三维度（提议形状 / 验证掩码 / KV 归属）是区分这四种策略的最稳框架；选哪种策略由构建期写入的 `SpecDecodeMode` 决定，运行时分发在 `DecoderRegistry` 构造函数里。

## 7. 下一步学习建议

- **导出侧的投机解码（u7-l2）**：本讲只讲了 C++ 运行时四种解码器。它们对应的 base/draft 草稿引擎是如何导出的、权重如何 remap、`d2t`（draft-to-target）词表映射在哪一步产出——继续读 `tensorrt_edgellm/model.py` 与 `models/eagle3` / `models/dflash` 的导出实现。
- **构建期的双引擎（u4-l1 / u4-l2）**：投机解码让八阶段构建跑两遍，产出 `spec_base.engine` / `spec_draft.engine`；回头对照构建器的 profile 分发，理解 `spec_decode_type` / `engine_role` 如何决定运行时分发到哪种解码器。
- **投机解码的 GPU kernel**：本讲多次提到的 `eagleAccept` / `sequentialAccept` / `ddtreeBuild` / `constructVerificationDraftTree` 都在 `cpp/kernels/speculative/` 下，建议结合 `unittests/eagleAcceptTests.cpp` 与 `unittests/mtpStateScatterTests.cu` 阅读这些 kernel 的数学细节。
- **KV 缓存与系统提示缓存（u5-l5 / u9-l2）**：EAGLE/MTP/DFlash 各自的 `mDraftCacheManager` 与 base 的 KV 缓存如何协同、系统提示缓存如何在投机解码路径下预填充，可结合 `hybridCacheManager` 与 `systemPromptKVCache` 进一步深入。
