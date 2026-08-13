# MLA 多头潜在注意力

## 1. 本讲目标

本讲聚焦 ATB 中支持 DeepSeek 风格 **MLA（Multi-head Latent Attention，多头潜在注意力）** 的算子族。读完本讲，你应当能够：

- 说清 **MLA 为什么要把 KV「压成一个潜在向量」**，并明白它和标准多头注意力（MHA）、多查询注意力（MQA）、分组查询注意力（GQA）在「KV 压缩维度」上的根本区别。
- 读懂 `MultiLatentAttentionOperation` 的参数、强校验、形状推导、以及 prefill / decode 两段式 Runner 分派。
- 理解 `MlaPreprocessOperation` 这一融合算子如何把「RMSNorm 量化 + 三个 matmul + RoPE + 写 KV Cache」打成一个大算子，以及它为何需要 24 个输入。
- 了解 `RingMLAOperation` 这一多卡「环式注意力」变体，以及它两轮迭代（FIRST_RING → DEFAULT）的调用约定。

本讲依赖 u4-l4（Self-Attention 融合算子）建立的注意力与 KV Cache 心智，并复用 u3-l1（`OperationBase` 骨架）与 u3-l2（Runner 体系）的「`Operation → Runner → KernelGraph → Kernel`」调用链。

## 2. 前置知识

### 2.1 KV Cache 为什么是大模型推理的显存大头

自回归生成时，每生成一个新词都要让当前 Query 关注历史所有词的 Key、Value。为避免重算，推理框架会把历史 K、V 缓存下来，这就是 **KV Cache**。

标准多头注意力（MHA）每个头都缓存独立的 K、V：若头数为 \(n_h\)、每头维度为 \(d_h\)，则每缓存一个 token 需要

\[
\text{MHA 缓存} = 2 \cdot n_h \cdot d_h \quad \text{（K 与 V 各一份，共 }2\text{ 份）}
\]

以一个 128 头、每头 128 维的模型为例，每 token 仅 KV Cache 就要 \(2 \times 128 \times 128 = 32768\) 个元素。在长序列、大 batch 下，KV Cache 很快成为显存大头，进而限制能同时服务的请求数。

### 2.2 MQA / GQA / MLA：三条压缩 KV 的路线

为压缩 KV Cache，业界有三条递进路线：

| 方案 | 做法 | KV 缓存量（每 token） | 代表 |
|------|------|------------------------|------|
| MHA  | 每头独立 K、V | \(2 n_h d_h\) | 原始 Transformer |
| MQA  | 所有头共享同一组 K、V | \(2 d_h\) | vLLM 早期 |
| GQA  | 头分组，每组共享 K、V | \(2 n_g d_h\) | Llama 2/3 |
| MLA  | 把 K、V **联合压缩成一个低维潜在向量**，再上采样 | \(d_c + d_{\text{rope}}\) | DeepSeek-V2/V3 |

MQA/GQA 通过「减少头的份数」压缩，会损失精度；MLA 走的是另一条路——**降维再升维**：用一个下投影矩阵把拼接后的 KV 压成维数为 \(d_c\) 的潜在向量 \(c_{kv}\)，只缓存 \(c_{kv}\)；注意力计算时再用上投影矩阵恢复出 K、V。由于 \(d_c\) 远小于 \(2 n_h d_h\)，显存大幅下降，又因为信息被「有损但可学习地」压缩、再由上投影还原，精度损失比 MQA/GQA 小得多。

### 2.3 MLA 的两个关键设计：权重吸收 与 解耦 RoPE

MLA 有两个让它在工程上成立的关键设计，它们直接对应到本讲源码里的维度常量：

1. **权重吸收（weight absorption）**。注意力分数本质是 \(Q \cdot K^\top\)。若 \(K = c_{kv} \cdot W_{uk}^\top\)，那么 \(Q \cdot K^\top = Q \cdot W_{uk} \cdot c_{kv}^\top\)。可以把 \(W_{uk}\) 提前「吸收」进 Query 一侧，于是推理时不再需要显式上投影出 K，**Query 直接和压缩向量 \(c_{kv}\) 做注意力**。这就解释了源码里 Query 的头维度恰好等于压缩维度 \(d_c = 512\)，而不是每头独立的 \(d_h\)。

2. **解耦 RoPE（decoupled RoPE）**。旋转位置编码 RoPE 是位置敏感的，无法被低秩投影无损吸收。DeepSeek 的做法是把 K 拆成两段：一段是不带位置编码的「nope」部分（走压缩 \(c_{kv}\)，维度 512），一段是单独走 RoPE 的「rope」部分（维度 64）。于是每个 token 的 KV Cache 实际缓存

\[
\text{MLA 缓存} = \underbrace{d_c}_{\text{潜在 }c_{kv}} + \underbrace{d_{\text{rope}}}_{\text{解耦 }k_{\text{rope}}} = 512 + 64 = 576
\]

这 576 与头数 \(n_h\) 无关——这正是 MLA 相对 MHA 的核心收益来源。源码里反复出现的 `INNER_DIM_512`、`INNER_DIM_64` 就是 \(d_c\) 与 \(d_{\text{rope}}\)，而 576 是二者之和（完整有效头维度）。

> 顺带一提：MQA（所有头共享一份 KV，`kvHeadNum = 1`）在本讲里仍然出现，因为 MLA 的压缩向量天然是「所有头共享」的，源码校验里强制 `kvHeadNum == 1` 正是这一点的体现。

### 2.4 术语速查

| 术语 | 含义 |
|------|------|
| MLA | Multi-head Latent Attention，多头潜在注意力 |
| 潜在向量 \(c_{kv}\) | KV 联合下投影得到的低维压缩向量（本讲 \(d_c = 512\)） |
| nope / rope | 不带 / 带 RoPE 的分量，对应 512 维与 64 维 |
| \(d_c'\) | Query 侧压缩维度（DeepSeek 中 1536） |
| 权重吸收 | 把上投影 \(W_{uk}\) 并入 Query，使 Query 直接对 \(c_{kv}\) 做注意力 |
| NZ 排布 | 昇腾的一种分块存储格式（NzCache），配合 Cube 算子做量化推理 |
| Ring MLA | 多卡环式注意力，把长序列切分到多卡轮流计算并在线归并 softmax |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | `MlaPreprocessParam`、`MultiLatentAttentionParam`、`RingMLAParam` 三个参数结构 |
| [src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp) | `MultiLatentAttentionOperation`：校验、形状推导、Runner 分派 |
| [src/ops/ops_infer/multi_latent_attention/multi_latent_attention_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_ops_runner.cpp) | decode 侧 Runner，组一张 `MLAOperation` 节点的 KernelGraph |
| [src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp) | `MlaPreprocessOperation`：融合预处理算子（RMSNormQuant+matmul×3+RoPE+ReshapeAndCache） |
| [src/ops/ops_infer/mla_preprocess/atb_acl_mla_preprocess.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/atb_acl_mla_preprocess.cpp) | aclnn 适配层，列出全部 24 个输入的张量顺序 |
| [src/ops/ops_infer/ring_mla/ring_mla_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/ring_mla/ring_mla_operation.cpp) | `RingMLAOperation`：多卡环式注意力变体 |
| [example/op_demo/mla_preprocess/README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/mla_preprocess/README.md) | MLA 预处理 C++ demo 的场景与数据规格 |
| [example/op_demo/ring_mla/README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/ring_mla/README.md) | Ring MLA 两轮迭代的调用约定与数据规格 |

整体关系：`MlaPreprocess` 负责「把 hidden 压成潜在向量、写进分页 KV Cache、产出对齐的 Query」，`MultiLatentAttention` 负责「用 Query 对潜在 KV Cache 做注意力」，`RingMLA` 则是 `MultiLatentAttention` 在多卡长序列场景的环式版本。

## 4. 核心概念与源码讲解

### 4.1 MLA 的潜在注意力机制：从维度常量读懂压缩

#### 4.1.1 概念说明

在进入具体算子前，先把 MLA 的维度账算清楚。这至关重要——本讲三个算子的全部校验逻辑，本质上都在核对这几个维度是否吻合。结合 2.3 节，MLA 推理时每个 token 的 KV Cache 只存两个东西：

- 潜在压缩向量 \(c_{kv}\)，维度 \(d_c = 512\);
- 解耦 RoPE 的 \(k_{\text{rope}}\)，维度 \(d_{\text{rope}} = 64\)。

而 Query 经权重吸收后也拆成 nope / rope 两段，分别与这两部分做注意力。于是完整的有效头维度是

\[
d_h^{\text{eff}} = d_c + d_{\text{rope}} = 512 + 64 = 576
\]

对比标准 MHA 每头 \(2 d_h\) 的缓存，MLA 把「与头数相关」变成了「与头数无关」，这就是它省显存的根。

#### 4.1.2 核心流程

一次 MLA decode（单步解码）的数据流可以概括为：

```text
输入:
  query      [numTokens, headNum, 512]   # nope 段（已吸收 W_uk）
  queryRope  [numTokens, headNum, 64]    # rope 段
  kvCache    [numBlocks, blockSize, kvHeadNum, 512]   # 分页的潜在 c_kv
  kvCacheRope[numBlocks, blockSize, kvHeadNum, 64]    # 分页的 k_rope
  blockTables[numBatch, maxBlocks]       # 分页表：逻辑块 -> 物理块
  contextLens[numBatch]                  # 每个请求实际 KV 长度

计算:
  score = (concat(query, queryRope)) · (concat(c_kv, k_rope))ᵀ * qkScale   # 带分页寻址
  attn  = softmax(score) · c_kv        # V 也来自同一个潜在向量
输出:
  output     [numTokens, headNum, 512]   # 取 V 的头维度（=512）
```

关键点：**K 和 V 都来自同一个潜在向量 \(c_{kv}\)**（缓存里只有一份 512），这正是 MLA「潜在」二字的由来。

#### 4.1.3 源码精读

上面的维度数字并非凭空杜撰，全部落在 `multi_latent_attention_operation.cpp` 的匿名命名空间常量与校验函数里。先看常量表：

[文件:multi_latent_attention_operation.cpp:31-50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L31-L50) — 定义 `INNER_DIM_512`、`INNER_DIM_64` 等核心维度常量，它们就是上文 \(d_c\) 与 \(d_{\text{rope}}\)。

再看 `QKVDimCheck`，它用这些常量强制 query / kvCache 的 nope 段必须是 512、rope 段必须是 64：

[文件:multi_latent_attention_operation.cpp:368-377](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L368-L377) — 校验 `query` 与 `kvCache` 末维为 512（nope），`queryRope` 与 `kvCacheRope` 末维为 64（rope），把 MLA 的维度契约钉死在算子层。

#### 4.1.4 代码实践

**实践目标**：用源码中的维度常量，自己复算一遍 MLA 与 MHA 的 KV 缓存对比。

**操作步骤**：

1. 打开 `multi_latent_attention_operation.cpp`，记下 `INNER_DIM_512`、`INNER_DIM_64` 与 head 校验里出现的 `headNum ∈ {8,16,32,64,128}`。
2. 取一个典型配置 \(n_h = 128\)、\(d_h = 128\)（DeepSeek-V3 风格）。
3. 分别按 2.1、2.3 节的公式计算 MHA 与 MLA 每 token 的 KV 缓存量。

**需要观察的现象 / 预期结果**：

- MHA：\(2 \times 128 \times 128 = 32768\) 个元素/token。
- MLA：\(512 + 64 = 576\) 个元素/token。
- 比值 \(32768 / 576 \approx 56.9\) 倍——这就是 MLA 对长序列推理显存收益的数量级。

> 待本地验证：以上是纸面计算；若要量化真实显存，需结合 `blockSize = 128`（`DECODER_BLOCK_SIZE`）和分页池实际占用，在真机 profiling。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MLA 的 Query 头维度是 512（等于 \(d_c\)），而不是和 \(k_{\text{rope}}\) 一样小的 64？

**参考答案**：因为做了权重吸收——把上投影 \(W_{uk}\) 并入 Query，使 Query 直接和潜在向量 \(c_{kv}\)（维度 512）做内积。Query 必须与 \(c_{kv}\) 同维才能相乘，所以 nope 段维度等于压缩维度 \(d_c\)。

**练习 2**：若把 `kvHeadNum` 设为 2 会怎样？

**参考答案**：`ParamCheck` 会返回 `ERROR_INVALID_PARAM` 并打印「kvHeadNum should be 1, only support MQA」。潜在向量天然是所有头共享的，所以 MLA 只支持 `kvHeadNum == 1`。

---

### 4.2 MultiLatentAttentionOperation：decode 与 prefill 两段式

#### 4.2.1 概念说明

`MultiLatentAttentionOperation` 是 MLA 注意力主体的 ATB 算子。和 u4-l4 的 `SelfAttentionOperation` 一样，它沿用「**单 Param 覆盖多种形态**」的设计哲学，靠几个枚举字段表达全部形态，而不是拆成多个算子：

- `calcType`：区分 **decode**（`CALC_TYPE_UNDEFINED`/`SPEC`/`RING`/`SPEC_AND_RING`）与 **prefill**（`CALC_TYPE_PREFILL`）。decode 是逐 token 增量，prefill 是一次性算整段 prompt。
- `cacheMode`：决定 KV Cache 的存储形态——`KROPE_CTKV`（分离 cache，nope 与 rope 分开存）、`INT8_NZCACHE`（int8 量化的 NZ 排布高性能 cache）、`NZCACHE`（非量化 NZ cache）、`KVCACHE`（拼接 cache，目前不支持）。
- `maskType`：注意力掩码类型（causal、mask free、滑动窗口 SWA 等）。

它继承 `OperationBase`，只重写 `InferShapeImpl` 与 `CreateRunner` 两个必填钩子，外加若干 `*CheckImpl` 校验钩子（见 u3-l1）。

#### 4.2.2 核心流程

算子从创建到执行的关键路径：

```text
CreateOperation(Param)          # 工厂入口：芯片校验 + ParamRangeCheck + (Prefill?PrefillCheck:ParamCheck) + rsv 校验
   └─> new MultiLatentAttentionOperation(Param)   # 构造时按字段拼 opIrKeyStr 选 IR 规格
GetInputNum / GetOutputNum      # 由 maskType/calcType/cacheMode/maskUseStatusType 决定张量个数
InferShapeImpl                  # out[0]=query 但 dtype 取 queryRope；ring 模式额外产 lse
CreateRunner                    # prefill -> OpsRunnerPrefill；其余 -> OpsRunner
   └─> OpsRunner.SetupKernelGraph  # 组一张单节点 "MLAOperation" KernelGraph 下发
```

输入张量个数是动态的：基线 6 个（query/queryRope/kvCache/kvCacheRope/blockTables/contextLens），按 `maskType`、`calcType`、`cacheMode`、`maskUseStatusType` 逐项递增。

#### 4.2.3 源码精读

工厂入口先卡死芯片（**仅 Atlas 800I A2/A3，即 910B 系列推理产品**），再分 prefill / decode 走不同校验：

[文件:multi_latent_attention_operation.cpp:59-89](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L59-L89) — `CreateOperation` 模板特化：先 `Is910B()` 拦截非目标芯片，按 `calcType` 分派到 `ParamPrefillCheck` 或 `ParamCheck`，最后 `OP_PARAM_RSV_CHECK` 做 `rsv` 版本闸门校验（见 u2-l3）。

`ParamCheck` 是 decode 侧的「能力子集」校验，逐字段约束组合：

[文件:multi_latent_attention_operation.cpp:91-134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L91-L134) — 强制 `headNum ∈ {8,16,32,64,128}`、`kvHeadNum == 1`（只支持 MQA）、`cacheMode != KVCACHE`，并约束 `maskType` 与 `calcType` 的合法组合（例如 decode 的 `CALC_TYPE_SPEC` 不支持 `SWA_NORM` 掩码）。

构造函数按 Param 字段拼接 IR 规格键 `opIrKeyStr`，这是 u3-l1 介绍的「dtype/format 白名单外置 ini」机制：

[文件:multi_latent_attention_operation.cpp:205-236](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L205-L236) — 形如 `MultiLatentAttentionOperation` + `Mask`? + `Qlens`? + `IsRing`? + `Int8Nz`/`Nz`? + `Prefill`? + `MaskUseStatus`?，体现「字段组合 → 单一 IR 键」。

`GetInputNum` 把「动态输入个数」写成了清晰的增量累加：

[文件:multi_latent_attention_operation.cpp:240-263](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L240-L263) — 基线 `IN_TENSOR_NUM = 6`，按 `maskType`、`calcType==PREFILL`、`calcType==SPEC*`、`cacheMode==INT8_NZCACHE`（+2 个 descale）、`maskUseStatusType` 逐项 `++`。这是 `VariantPack` 装填张量个数的依据。

`InferShapeImpl` 揭示了一个反直觉点：**输出 dtype 取自 `queryRope` 而非 `query`**：

[文件:multi_latent_attention_operation.cpp:272-286](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L272-L286) — `out[0] = in[0]`（query 的形状），但 `out[0].dtype = in[1].dtype`；ring 模式额外产出一个 `lse`（log-sum-exp，用于在线归并），其 dim2 被置为 1。

最后是 Runner 分派，prefill 与 decode 走两条不同路径：

[文件:multi_latent_attention_operation.cpp:786-793](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_operation.cpp#L786-L793) — `CALC_TYPE_PREFILL` 造 `MultiLatentAttentionOpsRunnerPrefill`，其余造 `MultiLatentAttentionOpsRunner`。

decode 侧 Runner 把整个注意力表达成**一张单节点 KernelGraph**，节点 opDesc 名为 `"MLAOperation"`：

[文件:multi_latent_attention_ops_runner.cpp:71-94](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multi_latent_attention/multi_latent_attention_ops_runner.cpp#L71-L94) — 组一个 `MLAOperation` 节点，把 `AtbOps::OpParam::MLA`（含 headSize/tor/kvHead/isRing/windowSize/maskType）作为参数挂上，inTensors 接 query/queryRope/kvCache/kvCacheRope/blockTables/mask/qkDescale/pvDescale。这就是 u3-l2 所述 `Runner → KernelGraph → Kernel` 链路的最后一段。

#### 4.2.4 代码实践

**实践目标**：读懂 `GetInputNum` 的增量逻辑，推断一组给定 Param 下的实际输入张量个数与顺序。

**操作步骤**：

1. 设想一个 decode + ring + int8 量化的配置：`calcType = CALC_TYPE_SPEC_AND_RING`、`cacheMode = INT8_NZCACHE`、`maskType = MASK_TYPE_MASK_FREE`、`maskUseStatusType = MASK_USE_STATUS_TYPE_BATCH_MASK`。
2. 对照 `GetInputNum` 逐行累加基线 6。
3. 对照 `multi_latent_attention_ops_runner.cpp` 的 `SetupKernelGraph`（第 32–96 行）写出每个输入张量对应的语义（query/queryRope/kvCache/...）。

**需要观察的现象 / 预期结果**：

- 6（基线）+ 1（mask，因 `maskType != UNDEFINED`）+ 1（qSeqlen，因 `SPEC_AND_RING`）+ 2（qDescale/kDescale，因 `INT8_NZCACHE`）+ 1（maskUseStatus，因 `BATCH_MASK`）= **11 个输入**。
- ring 模式输出个数为 2（output + lse），见 `GetOutputNum`。

> 待本地验证：可在真机用 u2-l1 的 C++ 调用骨架，按上述 Param 构造 `MultiLatentAttentionParam` 并打印 `op->GetInputNum()` 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `InferShapeImpl` 里输出 dtype 取 `inTensorDescs.at(1)`（queryRope）而不是 `at(0)`（query）？

**参考答案**：query 的 nope 段在 int8 量化（`INT8_NZCACHE`）时是 int8，但最终注意力输出应当是浮点；queryRope 段始终是浮点（fp16/bf16），用它作为输出 dtype 可保证输出类型正确，避免把 int8 类型透传到输出。

**练习 2**：`calcType = CALC_TYPE_PREFILL` 与 decode 在输入张量上的最大区别是什么？

**参考答案**：prefill 没有 `blockTables`（用 `vNope` + `qSeqlen` 替换），且额外多一个 `vNope` 输入（见 `GetInputNum` 第 247–249 行的 `intensorNumBase++`）；而 decode 靠 `blockTables` + `contextLens` 做分页寻址读取已缓存的潜在 KV。

---

### 4.3 MlaPreprocessOperation：MLA 预处理融合算子

#### 4.3.1 概念说明

`MultiLatentAttention` 接收的是「已经压成潜在向量、已经写进 KV Cache、已经对齐好的 Query」。那么**谁来把上一层 hidden_states 变成这些输入**？答案就是 `MlaPreprocessOperation`。

它是一个**大融合算子**，把 MLA 注意力之前的全部预处理步骤打成一个 Kernel：

```text
hidden ──RMSNorm+Quant──> ──matmul(W_dqkv)──> 拆成 q压缩 / kv压缩 / rope 三段
                                                  │
            ┌─────────────────────────────────────┤
            ▼                                     ▼
   q压缩 ──matmul(W_uq)──> Query(nope, 512)    kv压缩 ──matmul(W_uk)──> 写入 kvCache(512)
            │                                     │
            └──────── RoPE(cos/sin) ──────────────┴──> QueryRope(64) / 写入 kvCacheRope(64)
                                                  （按 slotmapping 写分页 cache）
```

这正呼应了 `MlaPreprocessParam` 注释里的「融合 rmsNormQuant、matmul、rope、reshapeAndCache」。它的「输入极多（24 个）」就是因为这些矩阵（`wdqkv`/`wuq`/`wuk`）、归一化参数（gamma/beta）、量化参数（scale/offset）、RoPE 表（cos/sin）、cache 指针（kvCache/kvCacheRope/slotmapping）都要一次性喂进来。

#### 4.3.2 核心流程

```text
CreateOperation(MlaPreprocessParam)
   ├─ 芯片校验 Is910B()
   ├─ cacheMode / quantMode 校验（不支持 PER_TOKEN_ASYMM 与 UNQUANT）
   ├─ LoadMethod() 探测 aclnn 算子是否可用
   └─ new MlaPreprocessOperation(param, isAclnnFuncLoaded)
GetInputNum() = 24（固定）           # 见 DimCheck 的 inTensorShapes 表
GetOutputNum() = 2（KVCACHE）/ 4（分离 cache）  # qOut0/kvCacheOut0 + qOut1/kvCacheOut1
InferShapeImpl()                     # qOut0=[token,head,512], qOut1=[token,head,64]
CreateRunner()                       # aclnn 路径 vs ops(Split) 路径
```

`CreateRunner` 里有一个值得注意的「自适应后端」逻辑：算子会根据 `CheckAclnnKernel` 的结果，在 aclnn（CANN 官方融合算子）与 ops（ATB 自有 Kernel）之间动态选择，以支持「泛化 hiddenSize」和「跳过 rmsNormQuant」两种非标准场景。

#### 4.3.3 源码精读

24 个输入的顺序最完整地记录在 aclnn 适配层的函数签名里，它本质上就是 MLA 预处理的「接线图」：

[文件:atb_acl_mla_preprocess.cpp:22-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/atb_acl_mla_preprocess.cpp#L22-L32) — 24 个输入依次为 input/gamma0/beta0/quantScale0/quantOffset0/wdqkv/deScale0/bias0/gamma1/beta1/quantScale1/quantOffset1/wuq/deScale1/bias1/gamma2/cos/sin/wuk/kvCache/kvCacheRope/slotmapping/ctkvScale/qNopeScale，可直接对照 4.3.1 的数据流图。

工厂入口先校验，再探测 aclnn 可用性：

[文件:mla_preprocess_operation.cpp:53-87](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp#L53-L87) — `CreateOperation` 同样要求 `Is910B()`，拒绝 `PER_TOKEN_QUANT_ASYMM`/`UNQUANT` 两种量化模式，然后 `MlaPreprocessAclnnRunner::LoadMethod()` 探测 aclnn 算子是否加载成功，把结果以 `isAclnnFuncLoaded` 传入构造函数。

`InferShapeImpl` 用代码写明了 MLA 的「拆分输出」语义：qOut0 是 512 维 nope，qOut1 是 64 维 rope，二者拼起来正是 576 维有效头：

[文件:mla_preprocess_operation.cpp:124-158](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp#L124-L158) — 默认 qOut 末维 576；分离 cache（`KROPE_CTKV`/`INT8_NZCACHE`/`NZCACHE`）时拆成 qOut0(512) + qOut1(64)，并把 kvCacheRope 透传为 kvCacheOut1；int8 量化时 qOut0.dtype 还改为 `ACL_INT8`。

`CreateRunner` 的「自适应后端」决策树是本算子最有工程意味的部分：

[文件:mla_preprocess_operation.cpp:464-480](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp#L464-L480) — 若 `useAclnnKernel_` 为真（泛化 hiddenSize 或跳过 rmsNorm 时需要），造 `MlaPreprocessAclnnRunner`；否则 `KVCACHE` 走 `MlaPreprocessOpsRunner`，其余（分离 cache）走 `MlaPreprocessOpsRunnerSplit`。

决定走哪条路径的判定在 `CheckAclnnKernel`：

[文件:mla_preprocess_operation.cpp:422-462](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp#L422-L462) — 若 hiddenSize 非 7168（泛化）或输入不需 rmsNormQuant，则需要 aclnn kernel；若 aclnn 加载失败又确实需要，则返回错误，提示「把 hiddenSize 改回 7168 以使用 atb kernel」。这解释了为何 README demo 的 input 维度固定是 `[tokenNum, 7168]`。

最后看校验里那张「24 输入形状表」，它是对 4.3.1 数据流的最权威注解：

[文件:mla_preprocess_operation.cpp:326-351](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/mla_preprocess/mla_preprocess_operation.cpp#L326-L351) — 每个输入标注了语义（如 `deScale0` 应为 `[2112]`、`bias1` 应为 `[headNum*192]`、`cos/sin` 应为 `[tokenNum, 64]`），把 MLA 各阶段产物的维度逐一钉死。

#### 4.3.4 代码实践

**实践目标**：把 `MlaPreprocess` 的 24 个输入逐一对应到 MLA 数学流程的某个阶段，建立「输入 → 数学含义」的映射表。

**操作步骤**：

1. 打开 `atb_acl_mla_preprocess.cpp` 第 22–32 行，把 24 个输入参数名抄下来。
2. 对照 4.3.1 的数据流图，给每个输入归类：归一化参数（gamma0/beta0/gamma1/beta1/gamma2）、量化参数（quantScale*/quantOffset*/deScale*/bias*）、投影矩阵（wdqkv/wuq/wuk）、RoPE 表（cos/sin）、cache 与寻址（kvCache/kvCacheRope/slotmapping）、scale（ctkvScale/qNopeScale）。
3. 对照 `mla_preprocess/README.md` 的「数据规格」表，确认每个输入的真实 dtype 与排布（注意 `wdqkv`/`wuq`/`kvCache` 是 NZ 排布、int8 量化）。

**需要观察的现象 / 预期结果**：

- 能画出一张「24 输入 → 5 个计算阶段」的对应表，例如 `wdqkv + deScale0 + bias0 + gamma0/beta0/quantScale0/quantOffset0` 共同完成「RMSNorm 量化 + 第一次 matmul」。
- 明白 `slotmapping`（`[tokenNum]` 的 int32）的作用：告诉算子把每个 token 的潜在 KV 写进分页 cache 的哪个槽位——这与 u4-l5 的 `ReshapeAndCache` 同源。

> 待本地验证：在 Atlas A2/A3 上 `cd example/op_demo/mla_preprocess && bash build.sh` 编译运行 `mlapo_ds_demo`（DeepSeek 场景，per_tensor 非对称量化 + rope 拆分），观察其按 README 设置的 Param 与 24 个输入。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MlaPreprocess` 的输出在分离 cache 模式下是 4 个而不是 2 个？

**参考答案**：分离 cache（`KROPE_CTKV` 等）把 nope 与 rope 两段拆开：qOut0(512)/qOut1(64) 分别是 Query 的 nope/rope 段，kvCacheOut0(512)/kvCacheOut1(64) 分别是写入 cache 的潜在 \(c_{kv}\) 与 \(k_{\text{rope}}\)。这 4 个输出正好作为 `MultiLatentAttention` 的 query/queryRope/kvCache/kvCacheRope 输入，两个算子由此衔接。

**练习 2**：`CheckAclnnKernel` 在什么情况下会强制走 aclnn 后端？如果此时 aclnn 没加载成功会怎样？

**参考答案**：当 hiddenSize 不是 7168（需要泛化 hiddenSize）或输入不需要 rmsNormQuant（`doRmsNorm_ == false`）时强制走 aclnn。若此时 aclnn 加载失败：泛化场景返回 `ERROR_INVALID_TENSOR_DIM_NUM` 并提示改回 7168 用 atb kernel；跳过 rmsNorm 场景返回 `ERROR_INVALID_TENSOR_DTYPE`。

---

### 4.4 RingMLAOperation：多卡环式注意力变体

#### 4.4.1 概念说明

当序列极长、单卡装不下完整 KV 时，**Ring Attention（环式注意力）** 把序列切分到多张卡，每张卡只持有一段 KV，轮流计算自己 Query 对这段 KV 的局部注意力，并用 **softmax 的 log-sum-exp（lse）** 在卡间在线归并，最终每张卡都得到正确的全局注意力输出。`RingMLAOperation` 就是把这一机制套用到 MLA 上：Q/K/V 仍是 MLA 的潜在形式（nope 128 + rope 64 = 192 的合头维度），但计算按「环」组织。

它的 Param `RingMLAParam` 与前面两个不同：多了 `calcType`（区分**首卡** `CALC_TYPE_FIRST_RING` 与**非首末卡** `CALC_TYPE_DEFAULT`）、`kernelType`（高精度）、`inputLayout`（仅 BSND），以及一个 64 字节的 `rsv`。

#### 4.4.2 核心流程

环式注意力天然是**两轮迭代**的（第一轮无前序结果，第二轮起带前序 output/lse）：

```text
首卡（CALC_TYPE_FIRST_RING）:
  输入: queryNope/queryRope/keyNope/keyRope/value/mask/seqLen   (7 个，无 prevOut/prevLse)
  输出: output + softmaxLse

非首末卡（CALC_TYPE_DEFAULT）:
  输入: 上述 7 个 + prevOut + prevLse                             (9 个)
  输出: output + softmaxLse（与上一轮的 lse 在线归并）
```

注意它的 Q/K 在这里是「拆分但未做权重吸收」的形态：`queryNope[*, headNum, 128]` + `queryRope[*, headNum, 64]`，合头维度 192，所以 `qkScale = 1/sqrt(192)`（见 demo README）。

#### 4.4.3 源码精读

工厂入口与 Param 校验（`RingMLA` 只支持高精度 kernel、BSND 排布）：

[文件:ring_mla_operation.cpp:110-137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/ring_mla/ring_mla_operation.cpp#L110-L137) — `CreateOperation` 同样 `Is910B()` 拦截，调用 `ParamCheck` 校验 `calcType ∈ {DEFAULT, FIRST_RING}`、`headNum ≥ kvHeadNum`、`inputLayout == BSND`、`kernelType == HIGH_PRECISION`。

构造函数依据 `calcType` 决定是否「输入 lse」，从而决定输入张量个数：

[文件:ring_mla_operation.cpp:140-150](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/ring_mla/ring_mla_operation.cpp#L140-L150) — `CALC_TYPE_DEFAULT` 时 `isInputSoftmaxLse_ = true`，IR 键追加 `InputSoftmaxLse`。

`GetInputNum` 直接体现两轮差异：

[文件:ring_mla_operation.cpp:155-161](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/ring_mla/ring_mla_operation.cpp#L155-L161) — 首卡 7 个输入（`BASE_IN_TENSOR_NUM`），非首卡多 2 个（`RING_OPTIONAL_IN_TENSOR_NUM`，即 prevOut/prevLse）。

`InferShapeImpl` 揭示 output 取 V 的头维度（128），而 lse 是 `[headNum, qNTokens]` 的 float 张量：

[文件:ring_mla_operation.cpp:412-433](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/ring_mla/ring_mla_operation.cpp#L412-L433) — output 复用 query 形状但末维换成 value 的 headSize；非首卡时 lse 直接复用输入 prevLse（原地在线归并），首卡时新建 `[headNum, qNTokens]` 的 float lse。

`RingMLAParam` 的完整字段：

[文件:infer_op_params.h:3255-3313](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L3255-L3313) — 含 `CalcType`（DEFAULT/FIRST_RING）、`KernelType`（高精度）、`MaskType`（NO_MASK/TRIU）、`headNum/kvHeadNum/qkScale/inputLayout` 与 64 字节 `rsv`。

#### 4.4.4 代码实践

**实践目标**：理解 Ring MLA 的两轮迭代调用约定，能在源码与 demo 之间双向印证。

**操作步骤**：

1. 阅读 `example/op_demo/ring_mla/README.md` 的「第一轮 / 第二轮」两段，记录两轮 `calcType` 与输入张量的差异。
2. 对照 `ring_mla_operation.cpp` 的 `GetInputNum`（155–161 行）与构造函数（140–150 行），确认源码逻辑与 README 描述一致。
3. 注意 README 里 `qkScale = 1/sqrt(192)`，结合 demo 里 `NOPE_HEAD_SIZE=128`、`ROPE_HEAD_SIZE=64`，复算为何是 192。

**需要观察的现象 / 预期结果**：

- 第一轮 `calcType = CALC_TYPE_FIRST_RING`，7 输入（无 prevOut/prevLse）；第二轮改为 `CALC_TYPE_DEFAULT`，9 输入（把第一轮的 output/softmaxLse 作为 prevOut/prevLse 喂回）。
- \(128 + 64 = 192\)，故 `qkScale = 1/\sqrt{192}`——合头维度对应 nope 与 rope 两段之和。

> 待本地验证：在 Atlas A2/A3 上 `cd example/op_demo/ring_mla && bash build.sh`，先跑首轮（需改 demo 的 calcType 为 `FIRST_RING`）拿到 output/softmaxLse，再改为 `DEFAULT` 跑第二轮。注意 README 提示「CANN 包版本需与源码 release 版本对应」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Ring MLA 需要 `softmaxLse` 这个额外输出，而普通 `MultiLatentAttention`（非 ring）不需要？

**参考答案**：环式注意力把序列分散到多卡，每卡只算局部 softmax。要把多段局部注意力正确合并成全局注意力，必须借助 log-sum-exp（lse）做在线归并（数学上 \(\text{softmax}\) 的合并需要每段的最大值与归一化因子，lse 正是为此保留）。非 ring 场景单卡一次算完，无需归并，故不需要 lse。

**练习 2**：Ring MLA 的 Q/K 为什么是 nope(128) + rope(64) = 192 的合头，而 `MultiLatentAttention` 的 query 是 512？

**参考答案**：Ring MLA 走的是「拆分但未做权重吸收」的标准 MLA 形态，K/V 显式按头展开（nope 段每头 128 = V 的 head_size），与 RoPE 段（64）合起来 192；而 `MultiLatentAttention` 的 decode 走的是「权重吸收后」的形态，query nope 段被吸收成与压缩向量同维（512），直接对 \(c_{kv}\) 做注意力。两者是 MLA 在不同实现路径下的不同维度形态。

---

## 5. 综合实践

**任务**：用本讲三个算子，画出 DeepSeek 风格 MLA 在 ATB 中的**完整 decode 前向数据流**，并标注每一步的输入输出维度与负责的算子。

要求完成以下子任务：

1. **预处理阶段**：以 `[tokenNum, 7168]` 的 hidden 为起点，标出 `MlaPreprocess` 产出的 4 个输出（qOut0/qOut1/kvCacheOut0/kvCacheOut1）及其维度，并指出 `slotmapping` 如何把它们写入分页 KV Cache。
2. **注意力阶段**：把上一步的输出直接接到 `MultiLatentAttention` 的输入（query/queryRope/kvCache/kvCacheRope），标注 blockTables/contextLens 的来源，写出 output 的维度与 dtype（注意 dtype 取自 queryRope）。
3. **多卡扩展**：说明若改用 `RingMLA`，同样的 Q/K/V 会以怎样的形态（128+64=192 合头）参与计算，以及为何需要 lse 在线归并。
4. **维度自洽检查**：在整条链路上验证「576 = 512 + 64」这个 MLA 有效头维度始终自洽（preprocess 拆成 512/64，attention 各自处理 512/64，输出再合成）。

**验收标准**：

- 能产出一张包含「算子 → 输入张量（含维度）→ 输出张量（含维度）」的表格。
- 能解释为何 MLA 每 token 的 KV 缓存是 576 而非 MHA 的 \(2 n_h d_h\)，并给出数量级对比。
- 能指出三个算子共同的硬约束：**仅 Atlas 800I A2/A3（910B）推理产品支持**（三个 `CreateOperation` 都有 `Is910B()` 拦截）。

> 这是一个「源码阅读 + 维度推演」型综合实践，无需真机即可完成主体；若需验证数值，可在 A2/A3 上运行 `mla_preprocess` 与 `ring_mla` 两个 demo（待本地验证）。

## 6. 本讲小结

- **MLA 的本质**是 KV 联合压缩：把每头的 K、V 压成一个与头数无关的潜在向量 \(c_{kv}\)（512 维）外加解耦 RoPE 的 \(k_{\text{rope}}\)（64 维），每 token 仅缓存 576 个元素，相较 MHA 的 \(2 n_h d_h\) 大幅省显存。
- **权重吸收**让 Query 直接对 \(c_{kv}\) 做注意力（query nope 维度 = 512）；**解耦 RoPE** 把位置信息单独走 64 维 rope 段，规避低秩投影与 RoPE 的不兼容。
- `MultiLatentAttentionOperation` 用 `calcType`/`cacheMode`/`maskType` 等枚举组合表达 decode/prefill、量化/非量化、ring 等全部形态，强校验极重（`headNum ∈ {8..128}`、`kvHeadNum == 1`、仅 910B）。
- `MlaPreprocessOperation` 是把「RMSNorm 量化 + matmul×3 + RoPE + 写分页 KV Cache」融合成一个 Kernel 的大算子，24 个输入覆盖全部矩阵、归一化、量化、RoPE 与 cache 寻址参数，并自适应选择 aclnn/ops 后端。
- `RingMLAOperation` 是 MLA 的多卡环式变体，以 nope(128)+rope(64)=192 合头形态参与计算，靠 `softmaxLse` 在卡间在线归并，分首卡（`FIRST_RING`，7 输入）与非首卡（`DEFAULT`，9 输入带 prevOut/prevLse）两轮迭代。
- 三个算子都仅支持 **Atlas 800I A2/A3 推理产品**，都通过 `OperationBase → Runner → KernelGraph → Kernel` 链路落地，且都带 `rsv` 版本闸门。

## 7. 下一步学习建议

- **横向对比**：回到 u4-l4（Self-Attention）与 u4-l5（PagedAttention/KV Cache），把 MHA/GQA/PagedAttention 与本讲的 MLA 放在一起，画一张「KV 缓存量 vs 精度 vs 实现复杂度」的对比表，巩固对注意力家族的整体认知。
- **深入 Kernel**：本讲到 Runner 组 `KernelGraph` 为止。若想看 `MLAOperation` 节点最终如何在 AI Core 上执行，可进入 `src/kernels/mixkernels/ring_mla/` 与 MLA 相关 Kernel 目录，结合 u3-l4（Kernel 层与 MKI 框架）理解 Tiling 与三段式流水。
- **工程集成**：若要在自己的推理框架里用上 MLA，参考 u2-l1（C++ demo）与 u2-l2（Python torch_atb），按 `MlaPreprocess → MultiLatentAttention` 的顺序串起两段式调用；多卡长序列场景再叠加 `RingMLA` 的两轮迭代。
- **后续单元**：本讲是 u4（关键 Transformer 算子）的收官。接下来 u5 将进入通信算子与图算子机制——Ring MLA 的卡间归并正是通信算子（HCCL）与注意力融合的典型场景，可作为衔接 u5 的切入点。
