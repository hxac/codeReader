# KV 缓存与混合（Mamba）缓存管理

## 1. 本讲目标

本讲聚焦 C++ 运行时里「上一轮算出来的中间状态如何被下一轮复用」这一核心机制。读完本讲，你应当能够：

- 说清楚 **线性 KV 缓存**（attention 层）的内存布局 `[batch, 2, numKVHeads, maxSeqLen, headDim]` 与它为什么随序列长度线性增长。
- 说清楚 **Mamba / 线性注意力（GDN）层** 缓存的两类状态：固定大小的 SSM **recurrent state** 与滑动窗口 **conv state**，以及它们为什么与序列长度无关。
- 理解 **混合（hybrid）模型**（如 Nemotron-H、Qwen3.5 这类 attention + Mamba 交错堆叠的模型）如何在同一套抽象下同时维护「逐 token 增长的 KV」与「原地更新的 SSM 状态」。
- 能沿着 `resetForNewSequences → prefill commit → decode commit → batch 驱逐 compact → 系统提示缓存 capture/restore` 这条真实调用链，讲清楚每一类缓存在自回归步进中是如何被更新与复用的。

本讲是 u5-l3（引擎执行器与张量注册表）的直接延续：执行器只负责「绑定 + 跑」，而「绑定到哪块显存、这块显存谁来分配/谁来更新」正是本讲三个 Manager 要回答的问题。

## 2. 前置知识

在进入源码前，先用一句话建立两类「中间状态」的直觉，这是本讲最重要的对比：

| 维度 | Attention 层（Transformer） | Mamba / SSM 层（线性注意力） |
| --- | --- | --- |
| 缓存内容 | 每个历史 token 的 Key 与 Value | 一个固定大小的隐状态矩阵 + 一个卷积滑窗 |
| 是否随序列长度增长 | **是**，存全部历史 token → O(L) | **否**，状态被不断「原地覆写」→ O(1) |
| 解码时如何更新 | 把新 token 的 K/V **追加**到末尾 | 用新输入把旧状态**原地变换**成新状态 |

- **KV 缓存（KV cache）**：自回归解码时，attention 要看「之前所有 token」。如果每步都重算全部历史的 K/V 太浪费，于是把算过的 K/V 存下来，下一步只算新 token 的 K/V 并追加。这块内存随上下文长度线性增长，是边缘设备显存的主要去向之一。
- **SSM recurrent state**：Mamba 这类状态空间模型把「全部历史」压缩进一个固定大小的隐状态 \(h\)。每来一个新 token，状态被更新公式 \(h_t = \bar{A}\,h_{t-1} + \bar{B}\,x_t\) 原地改写。所以它不需要存历史 token，只需存「当前这一份状态」。
- **conv state**：Mamba 在 SSM 之前有一个一维卷积分支（conv1d），卷积有一个宽度为 `convKernel` 的滑窗。为了逐步解码，需要缓存「最近 `convKernel-1` 个输入」作为卷积的滑窗状态。
- **混合模型（hybrid）**：Nemotron-H、Qwen3.5 这类模型把 attention 层与 Mamba 层**交错**堆叠（例如「attention → mamba → mamba → attention …」）。运行时必须按层的真实类型，给 attention 层配 KV 缓存、给 Mamba 层配 recurrent + conv 状态。

承接 u2-l1 引入的概念：解析阶段会把每个层标注成 `layer_types`（`attention` / `mamba` / `gdn` / `mlp` / `moe`）。本讲的 Manager 正是按这份逐层类型表来决定「这一层要什么缓存」。其中 attention 以外的线性递归类层（mamba / gdn / 线性注意力）在运行时统一计入 `numLinearAttnLayers`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `cpp/runtime/kvCacheManager.h` / `.cpp` | 线性 KV 缓存管理器：按 attention 层逐层分配 `[batch,2,numKVHeads,maxSeq,headDim]` 张量，支持各层异构的 head 配置。 |
| `cpp/runtime/mambaCacheManager.h` / `.cpp` | Mamba 状态管理器：按递归层逐层分配 recurrent state 与 conv state，并可选地为投机解码分配「中间态」缓冲。 |
| `cpp/runtime/hybridCacheManager.h` / `.cpp` | 混合管理器：按绝对层号把访问路由到 KV 或 Mamba 子管理器，并统一管理共享的「KV 长度」张量、batch 驱逐压缩、系统提示缓存 capture/restore。 |
| `cpp/runtime/state/sharedResources.cpp` | 三个 Manager 的**唯一构造点**：从 `LLMEngineConfig` 读取 `layerTypes` / `kvLayerConfigs` / Mamba 字段，组装出 `HybridCacheManager`。 |
| `cpp/runtime/state/pipelineIO.cpp` | 把缓存张量**绑定**进引擎 TensorMap 的代码：揭示 KV 与 Mamba 状态都是「past/present 指向同一块显存」的**原地更新**契约。 |
| `cpp/common/bindingNames.h` | 引擎 I/O 张量名的「字典」：`recurrent_state` / `conv_state` / `present_*` / `intermediate_*` 等模板名。 |

## 4. 核心概念与源码讲解

### 4.1 KVCacheManager：线性 KV 缓存

#### 4.1.1 概念说明

`KVCacheManager` 负责给**每一个 attention 层**单独分配一块 GPU 显存，用来存放该层全部历史 token 的 Key 与 Value。之所以叫「线性」KV 缓存，是因为这块显存的大小与可寻址的最大序列长度 `maxSequenceLength` 成**线性**关系——你允许模型看多长的上下文，就得为每一层预留那么大的 K/V 表。

关键设计点有两个：

1. **逐层独立分配**（而非一块整的大张量）。不同层可能有不同的 `numKVHeads`（GQA 分组数）和 `headDim`（每个头的维度），尤其是混合架构里 attention 层的 head 配置可能不统一。每个 attention 层拿到自己尺寸的张量：`[maxBatchSize, 2, numKVHeads_i, maxSequenceLength, headDim_i]`，其中那个 `2` 就是把 K 和 V 拼在一起（combined KV）。
2. **支持 FP8 存储**。`kvCacheType` 可以是 `kHALF`（FP16）或 `kFP8`。FP8 KV 缓存能把这块显存砍半，是边缘设备省内存的关键手段（其启用与否由检查点的量化元数据决定，详见 u3 / u9-l3）。

#### 4.1.2 核心流程

`KVCacheManager` 本身是一个「分配器 + 取址器」，它的全部职责就是：构造时按配置分配好每一层的张量，之后只提供 `getCombinedKVCache(layerIdx)` 让外部（attention 插件）去读写。它本身**不负责**「追加新 token」或「更新长度」——那些动作由 attention 插件原地完成、由 `HybridCacheManager` 统一记账。

```
构造 KVCacheManager(config, stream):
  校验 dtype ∈ {kHALF, kFP8}、numAttentionLayers、layerConfigs.size 一致
  若 numAttentionLayers == 0（纯 Mamba 模型）→ 直接返回，什么都不分配
  检测 uniformity：所有层的 (numKVHeads, headDim) 是否全相同
  for 每个 attention 层 i:
      分配张量 [maxBatch, 2, numKVHeads_i, maxSeqLen, headDim_i]（device, kvCacheType）
  统计并日志总显存
```

显存占用公式（单层）：

\[
\text{bytes}_i = \text{maxBatchSize} \times 2 \times \text{numKVHeads}_i \times \text{maxSeqLen} \times \text{headDim}_i \times \text{elemSize}
\]

其中 \(\text{elemSize}\) 对 FP16 是 2、FP8 是 1。可以看到 `maxSeqLen`（即构建期的 `--maxKVCacheCapacity`）是显存大头。

#### 4.1.3 源码精读

配置结构体定义了逐层 head 配置与存储 dtype：

[cpp/runtime/kvCacheManager.h:49-56](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.h#L49-L56) —— `Config` 含 `numAttentionLayers`、`maxBatchSize`、`maxSequenceLength`、逐层 `layerConfigs` 与 `kvCacheType`。

[cpp/runtime/kvCacheManager.h:30-34](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.h#L30-L34) —— `KVLayerConfig{numKVHeads, headDim}`，正是「每层可异构」的载体。

构造函数的核心三段：dtype 校验、纯 Mamba 早退、逐层分配。

[cpp/runtime/kvCacheManager.cpp:32-33](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L32-L33) —— 只接受 `kHALF` 或 `kFP8` 两种 KV 存储类型，其余直接报错。

[cpp/runtime/kvCacheManager.cpp:42-46](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L42-L46) —— **纯 Mamba / 纯递归模型合法地拥有零个 attention 层**，此时 `mLayerCaches` 留空、跳过 uniformity 检测。这是「KV 管理器在纯 Mamba 模型里是个空壳」的根源。

[cpp/runtime/kvCacheManager.cpp:66-80](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L66-L80) —— 为每个 attention 层 `emplace_back` 一个形状为 `{maxBatchSize, 2, numKVHeads, maxSeqLen, headDim}` 的 device 张量；那个 `2` 即 combined K/V。

uniformity 检测结果由 `isUniform()` 暴露，供下游决定能否走「打包批量 kernel」路径：

[cpp/runtime/kvCacheManager.cpp:52-61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L52-L61) —— 遍历所有层，只要任一层的 `(numKVHeads, headDim)` 与第 0 层不同，就置 `mIsUniform=false`。

#### 4.1.4 代码实践

**实践目标**：估算一个真实模型的 KV 缓存显存，并定位「哪个构建参数控制它」。

**操作步骤**：

1. 任选一个 Llama3-8B 级别的配置（32 层、`numKVHeads=8`、`headDim=128`、FP16 KV）。
2. 套用公式估算 `maxKVCacheCapacity=4096`、`maxBatchSize=1` 时 KV 缓存总显存（MB）。
3. 在 [cpp/runtime/kvCacheManager.cpp:66-80](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L66-L80) 的 `LOG_DEBUG` 行确认运行时确实会打印这块显存。
4. 对照 u4-l3：把 `--maxKVCacheCapacity` 从 4096 改成 8192，说明对引擎构建出的 KV 张量形状与显存的影响。

**预期结果**：单层 ≈ \(1 \times 2 \times 8 \times 4096 \times 128 \times 2\) 字节 = 16 MB，32 层 ≈ 512 MB；翻倍 `maxKVCacheCapacity` 后 KV 显存线性翻倍。FP8 KV 会再减半。

> 待本地验证：上述数值需在你本机用真实 `config.json` 的 `num_key_value_heads` / `head_dim` 核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 KV 缓存张量的第 1 维是 `2`，而不是分成独立的 K 与 V 两个张量？
**答案**：把 K、V 拼成 combined KV（第 1 维 2）可以让 attention 插件用一次绑定、一次 kernel 调用同时读写 K 和 V，减少显存碎片与绑定开销；下游 `formatKVCacheName` 也是按「combined」契约绑定的（见 4.3.3 的 pipelineIO 代码）。

**练习 2**：纯 Mamba 模型（无 attention 层）实例化 `KVCacheManager` 会发生什么？
**答案**：构造函数在 [kvCacheManager.cpp:42-46](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L42-L46) 检测到 `numAttentionLayers == 0` 直接 `return`，`mLayerCaches` 为空、不分配任何显存，是一个合法的空壳。

---

### 4.2 MambaCacheManager：SSM 与 conv 状态缓存

#### 4.2.1 概念说明

`MambaCacheManager` 服务于模型的**递归层**（Mamba / GDN / 线性注意力，在运行时统称 `numRecurrentLayers`）。每一个递归层需要缓存**两类**与序列长度无关的固定状态：

- **recurrent state（SSM 隐状态）**：形状 `[maxBatchSize, recurrentStateNumHeads, recurrentStateHeadDim, recurrentStateSize]`。这就是把「全部历史」压缩后的状态矩阵 \(h\)，每步被原地改写。
- **conv state（卷积滑窗）**：形状 `[maxBatchSize, convDim, convKernel]`。缓存一维卷积分支最近 `convKernel` 个输入，使逐步解码成为可能。

与 KV 缓存最大的区别：**这两块状态的大小只与 batch、head 配置、`stateSize`、`convKernel` 有关，与上下文长度完全无关**。一个能看 4k 上下文的 Mamba 层和一个能看 32k 的，状态缓存大小一样——这正是 SSM 在长上下文下省内存的根本原因。

此外，当模型同时启用**投机解码**（MTP / DFlash）且带有递归层时，base 引擎在一次 verify 里要同时处理多个候选 token，会为每个候选各算一份「中间态」。因此 Manager 还可以分配一组 **intermediate（中间态）缓冲**：`[maxBatch, maxIntermediateSeqLen, ...]`，verify 完成后再把「被接受的那一段」scatter 回持久状态池。

#### 4.2.2 核心流程

```
构造 MambaCacheManager(config, stream):
  若 numRecurrentLayers == 0 → 直接返回（纯 attention 模型）
  for 每个递归层 i:
      分配 recurrent state [maxBatch, numHeads, headDim, stateSize]，memset 0
      分配 conv state    [maxBatch, convDim, convKernel]，memset 0
  若 maxIntermediateSeqLen > 0（投机解码的 hybrid base）:
      为每层额外分配 intermediate recurrent / conv 缓冲（含一个序列维）
      构建一次性的 device MtpLayerInfo[] 数组（scatter 时复用）
```

注意一个 dtype 细节：**持久 recurrent state 默认 FP16、可配置**；而 **intermediate recurrent state 的契约固定为 FP32**（见 [bindingNames.h:275-278](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/bindingNames.h#L275-L278) 标注 FLOAT32，以及 scatter 路径对 FP32 的强校验）。这是因为 verify 阶段多 token 并行展开需要更高精度的累加。

#### 4.2.3 源码精读

[cpp/runtime/mambaCacheManager.h:43-55](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.h#L43-L55) —— `Config` 字段一览：递归层数、batch、recurrent 的 heads/headDim/stateSize、conv 的 dim/kernel、是否启用 intermediate，以及两种状态的 dtype。

[cpp/runtime/mambaCacheManager.cpp:52-73](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L52-L73) —— 逐层分配 recurrent 与 conv 状态并 `cudaMemsetAsync` 清零。注释里写明了两个形状。

[cpp/runtime/mambaCacheManager.cpp:76-129](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L76-L129) —— 当 `maxIntermediateSeqLen > 0` 时分配投机解码用的中间态缓冲，并把每层的「持久指针 + 中间指针」打包成 device 端 `MtpLayerInfo[]`（指针稳定，只上传一次），供后续 scatter 复用。

[cpp/runtime/mambaCacheManager.cpp:175-183](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L175-L183) —— `clearStates`：预热推理后、捕获 CUDA graph 前把所有递归层状态清零，保证干净起点。

[cpp/runtime/mambaCacheManager.cpp:185-210](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L185-L210) —— `captureRecurrentStates`：把某个 batch 槽的每层 recurrent 状态拷成一份独立快照，用于系统提示缓存（conv 状态同理，[212-235](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L212-L235)）。

[cpp/runtime/mambaCacheManager.cpp:283-302](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L283-L302) —— `scatterAcceptedLinearStates`：投机解码 verify 后，按「每条序列接受的长度」把中间态里被接受的那一段批量 scatter 回持久池。树形变体 `scatterAcceptedTreeStates`（[304-354](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/mambaCacheManager.cpp#L304-L354)）则按树节点 id 散布，并对 recurrent/conv 的 dtype 做了严格断言。

#### 4.2.4 代码实践

**实践目标**：对比「同一层在 attention 与 Mamba 两种实现下的缓存成本」。

**操作步骤**：

1. 假设 `maxBatch=1`、`headDim=128`、`numKVHeads=8`、FP16。
2. 计算 attention 层在 `maxSeqLen=4096` 时的 KV 显存。
3. 计算一个 Mamba 层（`recurrentStateNumHeads=16, recurrentStateHeadDim=128, recurrentStateSize=128, convDim=5120, convKernel=4`，FP16 recurrent / FP16 conv）的 recurrent + conv 显存。
4. 把第 2、3 步结果相除，体会「Mamba 缓存与序列长度无关」在长上下文下的省内存幅度。

**预期结果**：attention KV ≈ \(1 \times 2 \times 8 \times 4096 \times 128 \times 2 \approx 16\) MB；Mamba ≈ \((16\times128\times128 + 5120\times4) \times 2 \approx 0.56\) MB。两者相差近 30 倍，且 Mamba 的成本不随 `maxSeqLen` 增长。

> 待本地验证：用真实 Mamba 模型的 `state_size` / `conv_kernel` / `inner_size` 替换上述假设值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 conv state 的最后一维恰好是 `convKernel`，而不是 `convKernel-1`？
**答案**：conv1d 逐步解码时，当前输入会和「之前缓存」一起做卷积。把当前输入也纳入滑窗（共 `convKernel` 个槽），插件可以直接原地覆写最老的一格、对整窗做卷积，比「只存 `convKernel-1` 个历史、每步临时拼一个」更省一次拼接。这是工程上常见的「滑窗状态含当前帧」约定。

**练习 2**：`maxIntermediateSeqLen` 什么时候大于 0？分配它的代价是什么？
**答案**：仅当模型含递归层（`numLinearAttnLayers > 0`）**且**走 MTP/DFlash 投机解码时，由 `SharedResources::createForSpecDecode` 设为 verify 序列长度（见 4.3.3）。代价是为每层额外分配一块含序列维的中间缓冲（recurrent 与 conv 各一），显存随 `maxBatch × verifySize` 增长。

---

### 4.3 HybridCacheManager：混合层路由与统一生命周期

#### 4.3.1 概念说明

`HybridCacheManager` 是运行时**唯一**对外暴露的缓存管理器——即便你的模型是纯 attention 或纯 Mamba，运行时也只会构造**一个** `HybridCacheManager`。它的角色是「项目经理」：

- **路由（routing）**：外部代码只认「绝对层号」（第 0 层、第 1 层……），但 KV 与 Mamba 状态存在两个子管理器里，各自有独立的「本地索引」。HybridCacheManager 内部维护两张路由表 `mAbsToKVIndex` / `mAbsToMambaIndex`，把绝对层号翻译成对应子管理器的本地索引。
- **统一记账**：attention 层需要知道「当前序列已存了多少 token」（KV 长度），这个长度由一个**共享的 device 张量** `mDeviceKVCacheLengths` 维护，所有 attention 层共用。它还管理 `mActiveBatchSize`（当前活跃序列数）与 `mKVCacheAllEmpty`（是否还没做过任何 prefill）。
- **跨子管理器的统一操作**：batch 驱逐后的**压缩**（compact）、系统提示缓存的 **capture / restore**，都需要同时搬运 KV 与 Mamba 两类状态，这些方法都收在 HybridCacheManager 上。

#### 4.3.2 核心流程

一个混合模型自回归步进的完整生命周期（以 vanilla 解码为例）：

```
请求开始:
  resetForNewSequences(reuseLengths)        # 设定每条序列的 KV 复用长度（系统提示缓存命中时>0）
prefill:
  base 引擎前向（profile 0）                # attention 插件原地追加 K/V；Mamba 插件原地改写状态
  commitSequenceLength(contextLengths)      # GPU 张量逐序列累加 KV 长度
decode 循环 (每步):
  stepPreparer 用 (KV长度 + 1) 算出本次 contextLength
  base 引擎前向（profile 1）                # attention 追加 1 个 token 的 K/V；Mamba 改写状态
  commitSequenceLength(1)                   # 标量 +1，KV 长度推进
有序列结束、batch 缩小:
  compactBatch(batchMapping, oldBatch, newBatch)  # 同时压缩 KV 与 Mamba 状态、重排长度张量
固定系统提示场景:
  captureKVCache / captureRecurrentStates   # 快照
  restoreKVCache / 回灌                      # 下次同提示直接复用，省 prefill
```

两类缓存「如何在步进中被更新」的本质差异：

- **attention 层**：attention 插件把新 token 的 K/V **追加**到 KV 张量末尾（位置由 `kvcache_start_index` 指示），KV 长度通过 `commitSequenceLength` 显式 +1 推进。
- **Mamba 层**：Mamba 插件读旧 recurrent/conv 状态、原地写出新状态（past 与 present 指向同一块显存，见 4.3.3），**不产生长度推进**——它的「进度」就编码在状态本身里，无需外部长度记账。

#### 4.3.3 源码精读

**路由表与子管理器构造**：

[cpp/runtime/hybridCacheManager.cpp:41-56](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L41-L56) —— 遍历 `layerTypes`，给每个 `kAttention` 层分配一个 KV 本地索引、其余（`kMamba`）分配一个 Mamba 本地索引，填入两张路由表（未命中记 `-1`）。随后强校验「`layerTypes` 里 attention/mamba 的计数必须分别等于两个子配置的层数」。

[cpp/runtime/hybridCacheManager.cpp:68-76](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L68-L76) —— 构造 KV 与 Mamba 两个子管理器，并分配共享的 device KV 长度张量 `mDeviceKVCacheLengths`（`[maxBatchSize]`，清零）。

**按绝对层号路由的取址**（这是「外部只认绝对层号」的关键）：

[cpp/runtime/hybridCacheManager.cpp:177-185](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L177-L185) —— `getCombinedKVCache(absLayerIdx)`：查 `mAbsToKVIndex` 得本地索引，转发给 KV 子管理器；若该层不是 attention 层则报错。`getRecurrentState` / `getConvState`（[187-203](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L187-L203)）同理走 Mamba 路由。

**生命周期四件套**：

[cpp/runtime/hybridCacheManager.cpp:244-271](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L244-L271) —— `resetForNewSequences`：把 CPU 端的复用长度拷到 device、reshape 到当前 batch、并据此重算 `mKVCacheAllEmpty`（全部为 0 则记为空）。

[cpp/runtime/hybridCacheManager.cpp:273-286](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L273-L286) —— `commitSequenceLength(GPU 张量)`：prefill 后用引擎写出的 context 长度对共享长度张量做**逐序列累加**（`incrementLengthTensor`），并清除「全空」标志。

[cpp/runtime/hybridCacheManager.cpp:288-294](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L288-L294) —— `commitSequenceLength(标量)`：decode 每步把长度 +1（通常 increment=1），同样清除「全空」标志。

[cpp/runtime/hybridCacheManager.cpp:322-358](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L322-L358) —— `compactBatch`：batch 驱逐后**一次性同时压缩** KV（按 headDim 分组批量 kernel）、共享长度张量、以及每层的 Mamba recurrent/conv 状态（注意 Mamba 张量要先 reshape 到 `oldBatch` 再压缩、再 reshape 回 `maxBatch`，以匹配压缩 kernel 对 `shape[0]==oldBatch` 的断言）。

**真实调用点（自回归步进的证据链）**：

[cpp/runtime/decoding/vanillaDecoder.cpp:103](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L103) —— vanilla 解码每步 `commitSequenceLength(/*increment=*/1, ...)`。

[cpp/runtime/llmInferenceRuntime.cpp:1418](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1418) —— prefill 后用 context 长度 commit。

[cpp/runtime/llmInferenceRuntime.cpp:1726](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1726) —— 请求开始处 `resetForNewSequences`；[1828](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1828) 的 `captureKVCache` 与 [1947](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1947) 的 `compactBatch` 分别服务系统提示缓存与 batch 驱逐。

[cpp/runtime/preprocess/stepPreparer.cpp:67-71](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/stepPreparer.cpp#L67-L71) —— decode 步用 `getKVCacheLengths() + 1` 算本次 contextLength，把「KV 长度记账」与「下一步输入长度」闭环。

**原地更新契约（KV 与 Mamba 状态都靠它）**：

[cpp/runtime/state/pipelineIO.cpp:161-164](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L161-L164) —— attention 层把 `past_kv` 与 `present_kv` 绑定到**同一个** combined KV 张量（注释 `alias: in-place`），意味着 attention 插件直接在原表上追加/更新。

[cpp/runtime/state/pipelineIO.cpp:169-174](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L169-L174) —— Mamba 层同理：`recurrent_state`(past) 与 `present_recurrent_state`(present) 绑同一块、`conv_state` 与 `present_conv_state` 绑同一块。这就是「SSM 状态原地覆写」在绑定层的实现。下面 [180-189](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L180-L189) 行按需绑定投机解码的 intermediate 输出。

[cpp/runtime/state/pipelineIO.cpp:203](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L203) —— `kvcache_start_index` 绑定到 `cacheMgr.getKVCacheLengths()`，attention 插件据此知道「新 token 该追加到哪个位置」。

**唯一的构造点**：

[cpp/runtime/state/sharedResources.cpp:85-110](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L85-L110) —— `SharedResources::createForLLM` 从 `LLMEngineConfig` 读取 `kvLayerConfigs`、`numLinearAttnLayers`（即递归层数）、recurrent/conv 字段，组装出**唯一一个** `HybridCacheManager`。注意 `layerTypes` 直接来自引擎配置——它是 Python 侧 `ModelConfig.layer_types`（u2-l1）一路传到 C++ 的同一条信息。

> 补充：投机解码 runtime 在 [sharedResources.cpp:170-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L170-L251) 会建**两个** HybridCacheManager（index 0 = base、index 1 = draft），并按 spec 类型给 base 设 `maxIntermediateSeqLen`（[187](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L187)）。Gemma4-MTP 的 draft 不自建缓存、直接读 base 的 KV（[295-301](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L295-L301)）。

#### 4.3.4 代码实践

**实践目标**：追踪一次 vanilla decode 步，把「KV 长度推进」与「Mamba 状态原地更新」两条线都讲清楚。

**操作步骤**：

1. 在 [vanillaDecoder.cpp:103](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/decoding/vanillaDecoder.cpp#L103) 确认每步 decode 会 `commitSequenceLength(1)`。
2. 顺着它跳到 [hybridCacheManager.cpp:288-294](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L288-L294)，确认它只更新了共享 KV 长度张量，**没有**碰任何 Mamba 状态。
3. 回到 [pipelineIO.cpp:169-174](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L169-L174)，说明 Mamba 的 recurrent/conv 状态是如何被引擎「读旧写新、原地覆写」的。
4. 最后看 [stepPreparer.cpp:67-71](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/stepPreparer.cpp#L67-L71)，确认下一步用更新后的 KV 长度算 contextLength，闭环成立。

**需要观察的现象**：attention 的「进度」是**外部记账**（长度张量 +1），Mamba 的「进度」是**内部编码**（状态本身被覆写，无外部长度）。两类缓存在同一次步进里被更新，但走的是完全不同的机制。

**预期结果**：你能画出一张表，对每一个动作标注「谁触发、改了 KV 还是 Mamba、是否动长度张量」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HybridCacheManager` 不需要为 Mamba 层维护一个「recurrent 长度」张量？
**答案**：Mamba 状态是固定大小的，其「记忆了多长历史」的信息已经**隐式编码在被覆写的状态矩阵里**，不存在「追加新行」的概念，因此没有可累加的长度。只有 attention 层才有「当前存了几个 token 的 K/V」这种线性进度，需要共享长度张量记账。

**练习 2**：`compactBatch` 里为何对 Mamba 状态要先 reshape 到 `oldBatch` 再压缩、再 reshape 回 `maxBatch`？
**答案**：压缩 kernel `compactTensorBatch` 断言 `shape[0] == oldBatch`，而 Mamba 张量为留足余量按 `maxBatchSize` 分配（[hybridCacheManager.cpp:339-357](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L339-L357) 注释）。先缩到 `oldBatch` 满足断言、压缩到 `newBatch`、再还原成 `maxBatchSize` 让后续 batch 复用。

**练习 3**：`getKVHeadDimGroups()` 解决什么问题？
**答案**：当不同 attention 层的 `headDim` 不同（如 Gemma4 式混合架构），批量 kernel 无法用同一份模板处理所有层。它按 `headDim` 把 KV 层分组（[hybridCacheManager.cpp:79-120](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L79-L120)），每组各自一次批量 launch；uniform 模型退化为单一组。

## 5. 综合实践

**任务**：为「纯 attention 模型」与「attention + Mamba 混合模型」各画一份「逐层缓存清单 + 自回归步进更新表」，并指出 HybridCacheManager 在两者上的行为差异。

**步骤**：

1. 选两个模型假设：
   - A：Llama3 风格，32 层全 attention。
   - B：Nemotron-H 风格，例如 5 层 attention + 5 层 mamba 交错（共 10 层，仅作示意）。
2. 为每个模型的每一层，在表格里填：层类型、该层缓存什么状态、状态形状、是否随序列长度增长。
3. 对照 [sharedResources.cpp:85-110](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L85-L110)，说明模型 A 的 `MambaCacheManager` 是空壳（`numRecurrentLayers=0`）、模型 B 的两个子管理器都非空。
4. 对模型 B 的一次 decode 步，分别写出 attention 层与 mamba 层的更新路径（参考 4.3.4），并标注两者都依赖「past/present 同址绑定」这一契约（[pipelineIO.cpp:161-174](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/pipelineIO.cpp#L161-L174)）。
5. 给出一个结论：**为什么 HybridCacheManager 能用同一套 API 同时服务纯 attention、纯 Mamba 与混合三类模型？**

**预期产出**：两张层清单表 + 一段结论。结论应落到「`numAttentionLayers` 与 `numRecurrentLayers` 任一可为 0，子管理器对 0 层是合法空壳，HybridCacheManager 的路由表与生命周期方法对两类状态都做幂等的『有则处理、无则跳过』」。

> 待本地验证：若你有可运行的混合模型检查点（如 Nemotron-H），可在 `LOG_DEBUG` 处确认 `kvLayers` / `mambaLayers` 的实际计数与显存。

## 6. 本讲小结

- 运行时对外**只暴露** `HybridCacheManager` 一个缓存管理器；它内部组合 `KVCacheManager`（attention 层）与 `MambaCacheManager`（递归层），并用两张路由表 `mAbsToKVIndex` / `mAbsToMambaIndex` 按绝对层号分发。
- KV 缓存形状 `[batch, 2, numKVHeads, maxSeqLen, headDim]`，**随序列长度线性增长**，是显存大头；支持 FP16/FP8 两种存储、支持各层异构 head 配置。
- Mamba 状态 = 固定大小的 **recurrent state**（SSM 隐状态）+ **conv state**（卷积滑窗），**与序列长度无关**，这是 SSM 在长上下文下省内存的根源。
- 两类缓存的「原地更新」契约一致：past 与 present 绑定同一块显存，attention 插件追加 K/V、Mamba 插件覆写状态。
- attention 的进度靠**共享 device 长度张量**记账（`resetForNewSequences` → prefill `commitSequenceLength(GPU)` → decode `commitSequenceLength(1)`），Mamba 的进度**隐式编码在状态里**，无需外部长度。
- batch 驱逐（`compactBatch`）与系统提示缓存（`captureKVCache` / `restoreKVCache` 及 Mamba 的 capture）都由 HybridCacheManager 统一编排，跨两个子管理器同时搬运。

## 7. 下一步学习建议

- **系统提示 KV 缓存**：本讲出现的 `captureKVCache` / `restoreKVCache` 与 `captureRecurrentStates` 是系统提示缓存的底层原语，建议接着读 `cpp/runtime/state/systemPromptKVCache.h` 与 u9-l2，看上层如何复用它们省掉重复 prefill。
- **投机解码下的缓存**：本讲多次出现 `intermediate` 中间态与 `scatterAccepted*States`，它们服务 MTP/DFlash 投机解码；完整图景见 u7-l1（四种投机解码策略）。
- **KV 缓存与执行器的边界**：复习 u5-l3，确认「KV 缓存属于 `SharedResources`、不属于 `EngineExecutor`」，理解执行器为何对缓存「无所有权」。
- **Python 侧的层类型源头**：`layerTypes` 来自 u2-l1 解析的 `ModelConfig.layer_types`，可回看该讲理解「逐层类型标注」是如何从检查点一路传到本讲的 C++ 配置里的。
