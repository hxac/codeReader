# PagedAttention 与 KV Cache 机制

## 1. 本讲目标

大模型推理之所以能「逐 token 生成」且每步不重新算历史，靠的就是 **KV Cache**；而当并发请求变多、序列变长，KV Cache 又会变成显存杀手，于是有了 **PagedAttention**（分页注意力）。本讲聚焦 ATB 中专门服务这两件事的算子，学完后你应当能够：

- 说清 **KV Cache** 解决什么问题、PagedAttention 的「分页 + block table」与普通注意力在数据组织上的本质区别。
- 读懂 [`PagedAttentionOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp) 的输入张量约定（`query` / `keyCache` / `valueCache` / `blockTables` / `contextLens`）、动态输入个数推导、`InferShapeImpl` 与 `CreateRunner` 决策树。
- 读懂 [`KvCacheOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp) 这一类「写入缓存」算子的「无输出、原地写回」设计，以及它与 `ReshapeAndCache`、`PagedCacheLoad` 算子族的分工。
- 亲手对比 PagedAttention 与 u4-l4 讲过的普通 `SelfAttention`，指出二者在 KV 来源（缓存指针 vs 完整张量）、block table、contextLens 上的关键差异。
- 了解 `PagedAttentionParam` 的 `mlaVHeadSize`、`compressType`、`calcType`（并行解码）等变体字段如何承接 MLA、压缩、prefix 等高阶场景。

本讲承接 u4-l4（Self-Attention 融合算子）——u4-l4 讲的是「一个 SelfAttention 算子如何覆盖 flash / paged / prefix 多形态」，本讲则单独深入 **paged 路径专属的 `PagedAttentionOperation`** 以及 **KV Cache 的写入/加载算子族**。两者一脉相承，可以对照阅读。

## 2. 前置知识

进入源码前，先用最直白的话把几个概念摆清楚。

**自回归生成与 KV Cache。** Transformer 解码时，每生成一个新 token，都要拿当前 query 去和「历史上所有 token 的 K、V」算注意力。如果不缓存，每一步都要重算所有历史 token 的 K/V，代价随步数平方增长。**KV Cache** 就是把每层每一步算出的 K、V 存下来，下一步直接读。于是推理被切成两段：

- **Prefill**：处理整段 prompt，算出并写入全部初始 KV Cache，query 序列长。
- **Decode**：每步 query 只有 1 个 token，但要从很长的历史 KV Cache 里读取 K/V。

**KV Cache 的显存痛点。** 一个直观事实：KV Cache 的大小正比于「batch × 序列长度 × 层数 × 头数 × 头维」。当多个请求并发、序列又长（如 4K～32K），KV Cache 很快就把显存撑爆；而且不同请求长度不一，若按「最大长度」给每个请求预分配一段连续显存，既浪费又碎化。

**分页（Paged Attention）。** 借鉴操作系统虚拟内存的「分页」思想：把 KV Cache 切成固定大小的 **block**（一个 block 存 `block_size` 个 token 的 K/V），用一个 **block table**（块表）记录「每个请求的第 i 段 KV 存在第几个物理 block」。这样：

- KV Cache 显存按需申请、按 block 粒度复用，不再为短请求预留整段连续空间；
- 不同请求共享同一块「block 池」，显存利用率高；
- block table 给出「逻辑位置 → 物理块号」的映射，算子按表索引读取。

这就是 vLLM 等推理框架的核心思路，ATB 的 `PagedAttentionOperation` 正是它在昇腾上的落点。

**block table 与 contextLens。** 这两个张量是 PagedAttention 区别于普通 SelfAttention 的标志：

- `blockTables`：形状 `[num_tokens, max_num_blocks_per_query]`，元素是物理 block 编号（必须落在 `[0, num_blocks)` 内）。
- `contextLens`：形状 `[batch]`，记录每个 query 实际要读多少个历史 KV token（放在 Host/CPU，因为算子要据此做 Tiling 与边界判断）。

**slotMapping（槽位映射）。** 在「写入」KV Cache 的算子（`ReshapeAndCache`、`KvCache`）里，新 token 的 K/V 要落到 cache 的哪个位置？这个「目标位置」由 `slotMapping`（或 `KvCache` 里的 `tokenOffset`/`seqLen`）指定。可以把它理解成「把第 t 个新 token 写进第 slotMapping[t] 个 KV 槽位」。

> 读到这里你可能已经发现：本讲涉及两类算子——**读 KV Cache 算注意力**（PagedAttention）与**写/加载 KV Cache**（KvCache / ReshapeAndCache / PagedCacheLoad）。推理时它们配合使用：先写入，再读取。

## 3. 本讲源码地图

本讲主要文件集中在 `src/ops/ops_infer/` 下三个目录，外加公共参数头。

| 文件 | 作用 |
| --- | --- |
| [paged_attention_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp) | 本讲主战场：`PagedAttentionOperation` 的参数校验、动态输入个数、形状推导、Runner 决策树。 |
| [paged_attention_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.h) | 类声明，列出 `AttentionFlags` 与一长串校验钩子。 |
| [kv_cache_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp) | `KvCacheOperation`：最基础的「写 KV Cache」算子，无输出、原地写回。 |
| [kv_cache_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_ops_runner.cpp) | `KvCacheOpsRunner`：用单节点 `KernelGraph` 桥接到 `KVCacheOperation` kernel。 |
| [reshape_and_cache_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/reshape_and_cache/reshape_and_cache_operation.cpp) | `ReshapeAndCacheOperation`：更现代的 KV Cache 写入算子（按 slotMapping 填充），含 NZ / SISO 变体。 |
| [paged_cache_load_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_cache_load/paged_cache_load_operation.cpp) | `PagedCacheLoadOperation`：「反向」从分页 cache 把 K/V 读回成连续张量。 |
| [infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | `PagedAttentionParam`、`KvCacheParam`、`ReshapeAndCacheParam`、`PagedCacheLoadParam` 的权威定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 PagedAttentionOperation：分页注意力的独立算子

#### 4.1.1 概念说明

u4-l4 讲过，`SelfAttentionParam` 用 `calcType = PA_ENCODER` 也能进入 paged 路径。那为什么还要一个**独立**的 [`PagedAttentionOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp)？

二者分工不同：

- `SelfAttention` 是「大而全」的融合算子，试图用一个 Param 覆盖 flash/paged/prefix 等所有形态，校验逻辑互相纠缠。
- `PagedAttentionOperation` 是**专门为分页 decode 场景**设计的独立算子，输入约定对齐 vLLM 风格（`keyCache`/`valueCache`/`blockTables`/`contextLens` 四件套），并原生支持量化、压缩、并行解码（SPEC）、MLA 合并 KV、logN 缩放等长上下文推理常见特性。在最新的 950（A3）芯片上，它直接走 aclnn 桥接 CANN 官方 `aclnnFusedInferAttentionScoreV5` 系列融合算子。

一句话：**PagedAttention 不接收完整的 K、V 张量，而是接收「已经 cache 好的、按 block 分页的」keyCache/valueCache，外加一张 block table 告诉它去哪些物理块里读。**

#### 4.1.2 核心流程

PagedAttention 的核心计算仍是缩放点积注意力，差别只在 K/V 的「来源」与「寻址方式」：

\[
\mathrm{Attention}(Q,K_{cache},V_{cache}) = \mathrm{softmax}\!\left(\frac{Q\,K_{cache}^{\mathsf{T}}}{\sqrt{d_k}}\right)V_{cache}
\]

但 \(K_{cache}\)、\(V_{cache}\) 不再是连续张量，而是通过 block table 间接寻址得到的「稀疏拼接」。执行流程可以概括为：

1. 调用方准备分页 KV：`keyCache`/`valueCache` 形状为 `[num_blocks, block_size, kv_head_num, head_size]`（910B），`blockTables` 指明每个 query 用到哪些物理块。
2. `Setup` 阶段：算子在 Host 侧做大量参数与维度校验（这是 PagedAttention 最重的一块），并据 `contextLens` 做 Tiling、规划 workspace。
3. `Execute` 阶段：Kernel 在 Device 上按 `blockTables[i]` 取出第 `i` 个 query 需要的物理块，拼成该 query 的 K/V 序列，与 query 算注意力，写回 `attnOut`。

**输入张量（基线 5 个，随 Param 动态增减）**：

| 序号 | 名称 | 形状（910B ND） | 含义 |
| --- | --- | --- | --- |
| 0 | query | `[num_tokens, num_head, head_size]` | 各 batch 的 query 在 token 轴合并 |
| 1 | keyCache | `[num_blocks, block_size, kv_head_num, head_size]` | 分页存储的 K |
| 2 | valueCache | `[num_blocks, block_size, kv_head_num, head_size_v]` | 分页存储的 V（MLA 合并时不传） |
| 3 | blockTables | `[num_tokens, max_num_blocks_per_query]` | **块表**：逻辑→物理块号 |
| 4 | contextLens | `[batch]`（Host/CPU） | 每个 query 实际读取的历史 KV token 数 |

输出 `attnOut` 形状 `[num_tokens, num_head, head_size_v]`。

> 注意 310P（Atlas 推理系列）的 KV Cache 是 NZ 排布 `[num_blocks, head_size*num_heads/16, block_size, 16]`，与 910B 的 ND 不同——这就是为什么校验里有 `KVCacheDimCheck310P` 与 `KVCacheDimCheck910B` 两套。

#### 4.1.3 源码精读

**（1）参数定义与开关字段。** [`PagedAttentionParam`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1859-L1963) 延续了 u4-l1/u4-l4 反复出现的「单 Param 覆盖多行为」哲学，关键字段分两类：

- 形态/行为开关（枚举）：`maskType`（6 种）、`quantType`（5 种）、`compressType`、`calcType`（`CALC_TYPE_SPEC` 开启并行解码）、`scaleType`（`SCALE_TYPE_LOGN` 开启 logN 缩放）、`inputLayout`。
- 数值/修饰字段：`headNum`、`kvHeadNum`（GQA）、`qkScale`、`outDataType`、`hasQuantOffset`、`mlaVHeadSize`（>0 开启 MLA 合并 KV）、`qScale`。
- 末尾 `uint8_t rsv[64]`：版本闸门（见 u2-l3）。

**（2）动态输入个数。** 这是 PagedAttention 区别于一般算子的第一个特征：输入数量不是常数，而是随 Param 累加。基线 5 个，再按开关逐一加：

[paged_attention_operation.cpp:408-444](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L408-L444) —— `GetInputNum` 的累加规则（节选关键分支）：

```cpp
uint32_t inputNumBase = IN_TENSOR_NUM;              // 基线 5：q, keyCache, valueCache, blockTables, contextLens
if (param_.maskType != ... UNDEFINED) inputNumBase += 1;   // 多一个 mask
if (param_.batchRunStatusEnable)        inputNumBase += 1; // 动态 batch 标志位
if (param_.quantType == TYPE_DEQUANT_FUSION) {
    inputNumBase += 2;                                     // kDescale, vDescale
    if (param_.hasQuantOffset) inputNumBase += 2;          // kOffset, vOffset
}
if (param_.calcType == CALC_TYPE_SPEC)  inputNumBase += 1; // qSeqLens（并行解码）
...
if (param_.mlaVHeadSize > 0)            inputNumBase--;    // MLA 合并：少一个 valueCache
```

要点：**每开一个特性，就多一个对应的输入张量**；而 MLA 合并 KV 时反而少一个（valueCache 并进 keyCache）。这正是调用方装填 `VariantPack` 时必须遵守的顺序依据——读 PagedAttention 的输入，第一步永远是「先看 Param 开了哪些开关」。

**（3）输出与形状推导。** 输出固定 1 个 `attnOut`：

[paged_attention_operation.cpp:446-472](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L446-L472) —— `GetOutputNum` 返回 1；`InferShapeImpl` 先令 `out[0] = in[0]`（query 的形状），再在 910B 上把最后一维改写为 `headSizeV`（或 MLA 时的 `mlaVHeadSize`），并在 QKV 量化场景把 dtype 改为 `outDataType`：

```cpp
outTensorDescs.at(0) = inTensorDescs.at(0);            // 以 query 形状为基线
if (needQKVQuant) outTensorDescs.at(0).dtype = param_.outDataType;
if (Is910B()) {
    int64_t hiddenSizeValue = param_.mlaVHeadSize > 0 ? param_.mlaVHeadSize
        : inTensorDescs.at(2).shape.dims[lastDim];     // valueCache 的 head_size
    outTensorDescs.at(0).shape.dims[lastOutDim] = hiddenSizeValue;
}
```

直觉解读：PagedAttention 的输出和 query 同形状（`[num_tokens, num_head, head_size_v]`），但最后一维取 V 的头维而非 Q 的头维——这兼容了 Q/K 与 V 头维不等长（如 MLA）的情况。

**（4）极重的参数校验。** PagedAttention 的 `CreateOperation` 在建对象前会跑一长串 `XxxParamCheck`：

[paged_attention_operation.cpp:92-143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L92-L143) —— 950 芯片走 `Ascend950ParamCheck`（更严，要求 BSND、qkScale∈(0,1]、不量化），其余芯片依次跑 `CommonParamCheck → DeviceParamCheck → CompressParamCheck → CalcParamCheck → QuantParamCheck → LogNParamCheck → BNSDParamCheck → MlaParamCheck`。

这些 Check 本质上是在卡「字段组合的合法性子集」。例如 [`DeviceParamCheck`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L145-L195) 明确把动态 batch、head 压缩、MLA、量化限定在 910B（Atlas 800I A2）上；910A（Atlas 800 训练产品）则禁止 SPEC、mask_free、BNSD、logN。读这些校验等于在读「这块芯片支持哪些特性组合」。

**（5）Runner 决策树。** [`CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L1103-L1121) 按芯片分派，与 u4-l4 的 SelfAttention 同构：

```cpp
if (ASCEND_950) return std::make_shared<PagedAttentionAclnnRunner>(param_);  // 桥接 CANN aclnn
int64_t idx = RunnerTypeRegister::GetRunnerTypeIdx("PagedAttentionOpsRunner");
RunnerPool &pool = contextBase->GetRunnerPool(idx);
if (!Is910B()) {
    Runner *r = pool.MallocRunner<PagedAttentionOpsRunner910A, ...>(param_); // 310P/910A 复用 910A 变体
    return r ? shared_ptr(r, deleter) : std::make_shared<PagedAttentionOpsRunner910A>(param_);
}
return std::make_shared<PagedAttentionOpsRunner>(param_);                     // 910B 新建
```

三个要点（承接 u3-l2、u3-l5）：

- **950 走 AclnnRunner**（u3-l3），桥接 CANN 官方融合算子，无需自家 Kernel。
- **非 910B 经 `RunnerPool` 复用** `PagedAttentionOpsRunner910A`：`MallocRunner` 优先从对象池借一个旧实例（只 `SetParam` 换参数），池空才新建，归还靠 `shared_ptr` 自定义删除器。这是把昂贵的 `KernelGraph` 构造摊薄成换参数。
- **910B 直接 `make_shared` 新建** `PagedAttentionOpsRunner`，不进池（decode 场景形状多变，复用收益有限）。

#### 4.1.4 代码实践（源码阅读型）

本算子依赖真实 NPU 与分页 KV 数据，难以在通用环境直接运行，下面做一次「调用链跟踪」实践。

1. **实践目标**：弄清「开一组 Param 开关 → 输入个数变化 → 装填 VariantPack」的对应关系。
2. **操作步骤**：
   - 在 [`paged_attention_operation.cpp:408-444`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L408-L444) 的 `GetInputNum` 里，假设 Param 设为 `maskType=MASK_TYPE_NORM`、`batchRunStatusEnable=true`、其余开关默认。手工累加：`5 + 1(mask) + 1(batchStatus) = 7` 个输入。
   - 再假设把 `quantType` 设为 `TYPE_DEQUANT_FUSION` 且 `hasQuantOffset=true`，重算：`5 + 1 + 1 + 2(descale) + 2(offset) = 11`。
   - 打开 doxygen 规格表 [`atb_document.txt`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/doxygen/atb_document.txt) 中 `PagedAttention` 一节（约 535–565 行），核对每个新增输入的张量含义与排布。
3. **需要观察的现象**：输入个数随开关线性增长；MLA 合并时反而 `-1`；任何一组开关组合都必须先通过 `CreateOperation` 里的 Check 才能建对象。
4. **预期结果**：你能不看源码，给定一组 Param 字段即推算出 `GetInputNum` 与 `VariantPack::inTensors` 的装填顺序。
5. 运行时验证：待本地验证（需 910B/950 NPU 与 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PagedAttention 的 `contextLens` 放在 Host（CPU）而不是 Device（NPU）？

> 参考答案：算子在 `Setup` 阶段（Host 侧）就要依据每个 query 的 `contextLens` 做 Tiling、决定取多少个 block、规划 workspace，Tiling 发生在 Host，故需在 Host 可读；这与 `blockTables`/`keyCache`（Device）不同。这也呼应 u1-l4 讲过的「Tensor 的 hostData / deviceData 双指针」设计。

**练习 2**：`mlaVHeadSize > 0` 时，`GetInputNum` 为什么减 1？`InferShapeImpl` 又如何据此推导输出最后一维？

> 参考答案：MLA 把 valueCache 合并进 keyCache 一起传入，于是少了一个独立的 valueCache 输入（减 1）。`InferShapeImpl` 在 910B 上据此把输出最后一维直接取 `mlaVHeadSize`（而不是去读 valueCache 的最后一维），对应源码 `hiddenSizeValue = param_.mlaVHeadSize`。

---

### 4.2 KV Cache 写入与加载算子族：KvCacheOperation 为核心

PagedAttention 只负责「读」cache 算注意力，cache 里的内容谁来写？答案就是这一小节的算子族。

#### 4.2.1 概念说明

ATB 里「操作 KV Cache」的算子至少有四个，职责互补：

| 算子 | 方向 | 一句话定位 | Param |
| --- | --- | --- | --- |
| `KvCacheOperation` | 写入 | 最基础的「把新 K/V 按层号与偏移追加进 past 缓存」 | `KvCacheParam`（仅 rsv） |
| `ReshapeAndCacheOperation` | 写入 | 按 `slotMapping` 把 K/V 散列写入分页 cache（vLLM 风格） | `ReshapeAndCacheParam` |
| `ReshapeAndCacheWithStride` | 写入 | 带步长的写入变体 | `ReshapeAndCacheWithStrideParam` |
| `PagedCacheLoadOperation` | 读出 | 反向：从分页 cache 把 K/V 读回连续张量 | `PagedCacheLoadParam` |

本讲以 [`KvCacheOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp) 为核心精读，其余作为对照。

`KvCacheOperation` 是这族里**最朴素**的一个：它的 Param `KvCacheParam` 只有一个 `rsv[8]`（见 [infer_op_params.h:428-433](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L428-L433)），没有任何可配字段——因为它的语义完全由输入张量决定。它解决的问题是：**逐层、按 token 偏移把当前步算出的 K/V 追加进历史缓存 `past`。**

#### 4.2.2 核心流程

`KvCacheOperation` 的执行模型非常特别：**它没有输出张量，直接原地写回输入里的 `past` 张量。** 流程：

1. 输入 `new_kv`（新 token 的 K/V）、`layerId`（写到哪一层）、`past`（历史缓存，也是写入目标）、`tokenOffset`（写到 past 的哪个 token 偏移）、`seqLen`（每 batch 的序列长度）。
2. Kernel 把 `new_kv` 的内容，按 `layerId` 定位层、按 `tokenOffset` 定位起始 token，写进 `past` 对应区域。
3. 没有显式输出——调用方直接从同一个 `past` 张量读取更新后的缓存。

这是一个典型的「带副作用的 in-place 算子」，所以 `GetOutputNum` 返回 0。

#### 4.2.3 源码精读

**（1）空 Param 与固定输入。** [`kv_cache_operation.cpp:40-49`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp#L40-L49) —— 输入恒为 5、输出恒为 0：

```cpp
uint32_t KvCacheOperation::GetInputNum() const  { return 5; }   // new_kv, layerId, past, tokenOffset, seqLen
uint32_t KvCacheOperation::GetOutputNum() const { return 0; }   // 原地写回 past，无独立输出
```

对照 PagedAttention 动态增减的输入个数，`KvCacheOperation` 是另一个极端——**完全静态**，因为它的行为没有开关。

**（2）空 InferShapeImpl。** [`kv_cache_operation.cpp:51-57`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp#L51-L57) 只打了一条日志直接返回，因为「没有输出」就没有输出形状可推导：

```cpp
Status KvCacheOperation::InferShapeImpl(...) const {
    ATB_LOG(INFO) << ...;   // 无 outTensorDescs 需要填充
    return NO_ERROR;
}
```

真正的形状约束在 `InferShapeCheckImpl` / `SetupCheckImpl` 里，按芯片分两套（910B 走 `InferShapeDimCheck`，310P 走 `InferShapeDimCheck310P`），见 [kv_cache_operation.cpp:79-123](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp#L79-L123)。约束要点：`past` 的 `hiddenSize` 必须是 16 的倍数（fp16）或 32 的倍数（int8，量化场景）；`new_kv` 的 `hiddenSize` 必须与 `past` 一致；`tokenOffset`/`seqLen` 的 batch 维必须与 `past` 一致。

**（3）单节点 KernelGraph。** [`KvCacheOpsRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_ops_runner.cpp#L18-L53) 在构造函数里直接搭好一张只含一个节点的 `KernelGraph`（承接 u3-l2 的 OpsRunner 组图机制）：

```cpp
kernelGraph_.inTensors.resize(5);          // 5 个输入
kernelGraph_.outTensors.resize(0);         // 无输出
...
kvCacheNode.opDesc = {0, "KVCacheOperation", kvCacheParam};   // 桥接到名为 KVCacheOperation 的 kernel
kvCacheNode.inTensors  = {&newKvTensor, &layerIdTensor, &pastTensor, &tokenOffsetTensor, &seqLenTensor};
kvCacheNode.outTensors = {&pastTensor};    // 关键：输出指向 past 自身（原地写回）
kvCacheNode.inTensorViewFuncs[0] = [](old, &new) {
    if (old.size() == 4) new = {old[0]*old[1], old[2]*old[3]}; // 4 维 new_kv 视作 2 维
};
```

两个细节值得品：

- `outTensors = {&pastTensor}`：节点的「输出」就是输入 `past` 本身，再次印证 in-place 语义；`Execute` 时 Kernel 把数据写进 `past` 的 Device 内存。
- `inTensorViewFuncs[0]`：当 `new_kv` 是 4 维时，用一个「视图函数」把它在逻辑上 reshape 成 2 维喂给 kernel，**不改真实数据、只改形状描述**——这是 ATB Runner 里常见的零拷贝视图技巧。

末尾 [`REG_RUNNER_TYPE(KvCacheOpsRunner)`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_ops_runner.cpp#L57) 把这个 Runner 类型注册进 `RunnerPool`（u3-l5），`REG_OP_PARAM(AtbOps::OpParam::KVCache)` 注册 Param 用于序列化。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「无输出算子」如何在 Runner 层落地为 in-place 写回。
2. **操作步骤**：
   - 阅读 [`kv_cache_ops_runner.cpp:33-42`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_ops_runner.cpp#L33-L42)，确认唯一节点的 `outTensors` 指向 `&pastTensor`。
   - 对照 [`ReshapeAndCacheOperation::GetOutputNum`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/reshape_and_cache/reshape_and_cache_operation.cpp#L137-L145)：`ReshapeAndCache` 的输出是 2（`keyCache`/`valueCache`，或 SISO 时 1 个），它把 cache 也作为输出返回；而 `KvCache` 干脆输出 0。体会两种 API 设计的取舍。
   - 再看 [`PagedCacheLoadOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_cache_load/paged_cache_load_operation.cpp#L93-L105)：输入 6～7 个（keyCache/valueCache/blockTable/contextLens/key/value/+seqStarts），输出 2（key/value）——它是「读出」方向，把分页 cache 还原成连续 K/V。
3. **需要观察的现象**：同是「操作 KV Cache」，写入类算子（KvCache/ReshapeAndCache）与读出类算子（PagedCacheLoad）的输入输出结构完全镜像；写入类内部，KvCache 用「layerId+tokenOffset」寻址，ReshapeAndCache 用「slotMapping」寻址。
4. **预期结果**：你能画一张表，把四个算子的「方向 / 寻址方式 / 输出个数 / 支持芯片」对齐说清。
5. 运行时验证：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`KvCacheOperation::GetOutputNum` 返回 0，那调用方怎么拿到更新后的 KV Cache？

> 参考答案：因为 `past` 既是输入又是写入目标，Kernel 在 `Execute` 时直接把 `new_kv` 的数据写进 `past` 的 Device 内存（in-place）。调用方在下一次访问同一个 `past` 张量指针时，读到的就是已更新的缓存。Runner 层对应表现为节点的 `outTensors = {&pastTensor}`——输出即输入自身。

**练习 2**：`ReshapeAndCacheParam` 的 `kvCacheCfg` 有三个取值（`K_CACHE_V_CACHE` / `K_CACHE_V_BYPASS` / `K_CACHE_V_CACHE_NZ`），分别对应什么场景？

> 参考答案：`K_CACHE_V_CACHE` 是默认——同时传入 key_cache 和 value_cache；`K_CACHE_V_BYPASS` 只传 key_cache（SISO，单输入单输出），用于只需要写 K 的场景（如某些 MLA 前置处理）；`K_CACHE_V_CACHE_NZ` 传入 NZ 格式的 cache，用于 910B 上 NZ 排布的 KV（对齐要求更严，blockSize 需 16 对齐）。它们对应 [`ReshapeAndCacheOperation::CreateRunner`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/reshape_and_cache/reshape_and_cache_operation.cpp#L409-L432) 里不同的 Runner 子类（`ReshapeAndCacheOpsRunner` / `...SISO` / `...A2NZ`）。

---

### 4.3 PagedAttention 与普通 SelfAttention 的关键区别（承接 u4-l4）

把 u4-l4 的 `SelfAttention`（普通/flash 路径）与本讲的 `PagedAttention` 放在一起，是最容易把「分页」这件事吃透的方式。二者的根本差异在 **K/V 的来源与寻址**。

| 维度 | 普通 SelfAttention（flash 路径） | PagedAttention |
| --- | --- | --- |
| K/V 来源 | 调用方直接传入**完整的 K、V 张量**（连续） | 传入**已 cache 好的 keyCache / valueCache**（按 block 分页，物理上不连续） |
| 寻址 | 张量下标直接访问 | 经 **blockTables** 间接寻址：逻辑 token → 物理块号 |
| 长度信息 | 序列长度隐含在张量形状里 | 显式由 **contextLens**（Host）给出每 query 的实际 KV 长度 |
| 典型阶段 | Prefill（query 序列长，K/V 当前算） | Decode（query 仅 1 token，K/V 从历史 cache 读） |
| 输入个数 | 基本固定（Q/K/V + 可选 mask/Cache） | 基线 5，随 Param 开关动态增减 |

源码上的对照也很清晰：

- 普通 SelfAttention 的 `CalcType` 取 `UNDEFINED/ENCODER/DECODER` 走 flash，K/V 是直接输入（见 [infer_op_params.h:1710-1716](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1710-L1716)）。
- PagedAttention 的 `blockTables`、`contextLens` 是**必备**输入（[`paged_attention_operation.cpp:28-29`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp#L28-L29) 的基线 5 里就含这两个），且 `contextLens` 在 Host。

> 补充：`SelfAttentionParam` 也有 `calcType = PA_ENCODER` 的 paged 分支（u4-l4），它和独立的 `PagedAttentionOperation` 在计算目标上一致，但后者是面向 decode / 长上下文 / vLLM 风格调用方的专门实现，支持的变体（压缩、SPEC 并行解码、MLA、logN、多种量化）更丰富，校验也更细。

**关于 prefix encoder 变体。** 本讲主题「KV Cache 与分页」还牵出一个相关概念——**Prefix Caching / Prefix Encoder**。它的思路是：把公共前缀（如 system prompt）的 KV Cache 预先算好并常驻，后续请求复用，避免每次重算。ATB 在 `self_attention/` 目录下提供了 `self_attention_prefix_encoder_ops_runner` 与 `atb_acl_self_attention_prefix_encoder`（见 u4-l4 的 `PREFIX_ENCODER` 分支），本质是「带着前缀 KV 一起算注意力」的融合变体，服务于 910B。它和 PagedAttention 都是在「复用历史 KV」上做文章，区别是 prefix 关注「公共前缀复用」，paged 关注「按 block 灵活组织任意 KV」。

---

## 5. 综合实践

**任务：用一张「KV Cache 生命周期图」把本讲算子串起来。**

1. **实践目标**：把 prefill/decode 全过程中「KV Cache 的写入、读取、复用」与本讲四个算子的调用时机对应起来。
2. **操作步骤**：
   - 画一条时间轴，标出 **prefill** 与若干步 **decode**。
   - 在 prefill 节点标注：用 `ReshapeAndCache`（或 `KvCache`）把初始 K/V 按 slotMapping 写入分页 cache；用 `PagedAttention`（`SelfAttention` 的 flash 路径也可以）算注意力。
   - 在每步 decode 节点标注：先用写入算子把新 token 的 K/V 追加进 cache，再用 `PagedAttention`（带 blockTables/contextLens）读 cache 算注意力。
   - 若需要把某段 cache「搬」出来（如跨层复用、导出），标注用 `PagedCacheLoad` 把分页 cache 读回连续张量。
3. **需要观察的现象**：`past`/`keyCache`/`valueCache` 这几个张量在多次调用间是**同一个 Device 指针**（in-place 持续追加），`blockTables` 随序列增长而追加物理块号，`contextLens` 每步 +1。
4. **预期结果**：你能指着图说清「哪一步调哪个算子、它的输入输出是什么、cache 指针为何不变」。
5. 运行时验证：待本地验证（建议在 910B 上用 `torch_atb` 写一个最小 decode 循环观察 `contextLens` 变化）。

## 6. 本讲小结

- **KV Cache** 让自回归推理避免重算历史；**PagedAttention** 进一步把 KV Cache 按 block 分页、用 block table 寻址，解决长序列/多请求下的显存碎片与浪费。
- [`PagedAttentionOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/paged_attention/paged_attention_operation.cpp) 的输入基线 5 个（query/keyCache/valueCache/blockTables/contextLens），并随 `maskType`/`quantType`/`calcType`/`mlaVHeadSize` 等开关**动态增减**——读它的输入第一步是看 Param 开了哪些特性。
- 它的 `InferShapeImpl` 以 query 形状为基线、末维改写为 V 的 head_size（或 `mlaVHeadSize`）；`CreateRunner` 按芯片分派：950 走 aclnn、非 910B 经 RunnerPool 复用 910A 变体、910B 新建 OpsRunner。
- [`KvCacheOperation`](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/kv_cache/kv_cache_operation.cpp) 是「写入 KV Cache」最朴素的算子：Param 仅 `rsv`、输入恒 5、**输出 0**（in-place 原地写回 `past`），Runner 用单节点 KernelGraph 落地。
- KV Cache 算子族分工：`KvCache`/`ReshapeAndCache`(WithStride) 负责写入（按 layerId+tokenOffset 或 slotMapping 寻址），`PagedCacheLoad` 负责读出（分页→连续）。
- 与普通 SelfAttention 的关键区别集中在 K/V 来源（完整张量 vs 分页缓存指针）、blockTables 间接寻址、contextLens 显式长度三处；prefix encoder 则是「公共前缀 KV 复用」的相关变体。

## 7. 下一步学习建议

- **下一讲 u4-l6（RoPE 与位置编码）**：decode 时新 token 在写入 KV Cache 前通常要先做旋转位置编码，RoPE 与本讲的写入算子在调用链上紧邻，建议连读。
- **u4-l7（MLA 多头潜在注意力）**：本讲反复出现的 `mlaVHeadSize` 字段就是 MLA 合并 KV 的入口，下一阶段会讲清 DeepSeek 风格 MLA 的潜在注意力机制与 `MlaPreprocess`。
- **延伸源码阅读**：想深入 decode 性能，可读 `paged_attention_ops_runner.cpp` 与 `paged_attention_aclnn_runner.cpp`，看 OpsRunner 如何把 PagedAttention 拆成 `KernelGraph` 节点、950 如何桥接 `aclnnFusedInferAttentionScoreV5`；想理解 vLLM 风格调用的对齐，可重点读 `reshape_and_cache_operation.cpp` 的 slotMapping 约束。
