# fused_infer_attention_score：推理场景与版本演进

## 1. 本讲目标

上一讲（u4-l2）我们精读了 flash_attention_score 的 op_api 层，理解了训练侧 attention 的「校验漏斗 + 预处理流水线 + l0 base 调用」。本讲把镜头转向**推理侧**，以 `fused_infer_attention_score`（下文简称 FIA）为样本，学完后你应当能够：

1. 说清 KV Cache 场景下推理 attention 的输入布局：为什么 key/value 是 `aclTensorList`、`blockTable` 和 `blockSize` 如何把 KV 分页管理、`queryPaddingSize`/`kvPaddingSize` 又是什么。
2. 看懂 V1～V5 多版本 aclnn 接口共存的代码组织：入口垫片（shim）→ 内部统一入口 `InnerFusedInferAttentionScoreGetWorkspaceSize` → l0 base `l0op::FusedInferAttentionScore` 的三层漏斗，以及「新硬件只认新版本」的版本演进策略。
3. 了解 tiling 侧按场景（FAI/IFA/PFA）与按 SoC 架构（arch22/35/38）的双重路由，以及 `checkers`、`fia_tiling_info` 这类把巨型校验逻辑拆成可插拔小类的工程化手段。

## 2. 前置知识

- **KV Cache（缓存）**：大模型逐 token 生成时，历史 token 的 K/V 不会再变，因此把它们缓存在显存里，每步只对新 query 与全部历史 KV 做注意力，避免重算。推理 attention 算子的许多输入（`actualSeqLengthsKv`、`blockTable`）都是为管理这块缓存服务的。
- **PagedAttention（分页注意力）**：不同请求的序列长度差异很大，若为每个请求预留「最长序列」的连续显存会大量浪费。借鉴操作系统虚拟内存的思路，把 KV Cache 切成固定大小的 block（块大小为 `blockSize`），用一个 `blockTable`（页表）记录每个请求依次用了哪些 block。这样 KV 张量在物理上可以不连续、逻辑上按页表拼接。
- **左填充（left padding）**：推理框架有时把 padding 放在序列左侧，此时需要 `queryPaddingSize`/`kvPaddingSize` 告诉算子每个 batch 的填充长度，算子据此跳过无效前缀。
- **aclnn 两阶段 API 与 l0/L2 分层**：已在 u2-l4、u3-l1、u4-l2 讲过。回忆要点：第一段 `GetWorkspaceSize` 做校验/infershape/tiling 并打包 `executor`；第二段固定四参数异步下发。L2 层是对外 C 接口，l0 层（`l0op::` 命名空间）是与算子一一对应的 base 实现，多个 L2 版本入口共用同一个 l0 base。
- **GQA（grouped-query attention）**：query 头数 N 可以多于 KV 头数（`numKeyValueHeads`），若干 query 头共享一组 KV，是推理加速的常用手段——这解释了为什么接口里同时有 `numHeads` 和 `numKeyValueHeads`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention/fused_infer_attention_score/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/README.md) | 算子产品支持情况、功能与约束说明、示例索引 |
| [op_api/aclnn_fused_infer_attention_score.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp) | V1 入口垫片：硬件门禁 + 弃用告警 + 参数补空转发 inner |
| [op_api/aclnn_fused_infer_attention_score_v2.cpp ～ _v5.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp) | V2～V5 各版本入口，做少量预处理后统一调用 inner |
| [op_api/aclnn_fused_infer_attention_score_inner.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp) | 统一内部入口：空指针校验、Contiguous 流水线、调用 l0 base、ViewCopy 回写 |
| [op_api/fused_infer_attention_score_base_aclnn.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp) | l0 base：全量参数的 `l0op::FusedInferAttentionScore`，做 INFER_SHAPE 与 AICore 下发 |
| [op_host/fused_infer_attention_score_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fused_infer_attention_score_tiling.cpp) | host 侧 tiling：场景路由（FAI/IFA/PFA）与 SoC 架构分发 |
| [op_host/checkers/paged_attention_checker.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/paged_attention_checker.cpp) | blockTable/blockSize 等 PagedAttention 参数的语义校验 |
| [op_host/checkers/left_padding_checker.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp) | queryPaddingSize/kvPaddingSize 的校验 |
| [op_host/fia_tiling_info.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fia_tiling_info.h) | 校验器共享的参数信息结构 `FiaTilingInfo` 与统一参数命名 |
| [docs/aclnnFusedInferAttentionScore.md / …V5.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV5.md) | 各版本接口文档（参数表与约束说明） |

## 4. 核心概念与源码讲解

FIA 的计算公式与训练侧 FA 相同：

\[
Attention(Q,K,V)=Softmax\left(\frac{QK^T}{\sqrt{d}}\right)V
\]

区别不在数学，而在**输入的组织方式**：训练侧 Q/K/V 都是规整的大张量；推理侧 Q 每步只有 1 个（或少量）token，K/V 则是不断增长的缓存，且可能被分页、被左填充、被量化成 INT8/INT4。FIA 的接口复杂度几乎全部来自这里。

### 4.1 KV Cache 与 PagedAttention：推理 attention 的输入布局

#### 4.1.1 概念说明

FIA 的 README 明确了它的定位：**一个算子同时覆盖全量（prompt/prefill，对应 PromptFlashAttention）与增量（decode，对应 IncreFlashAttention）两种推理场景**，输入 dtype 支持 FP16/BF16/INT8/INT4（见 [README.md:L14-L34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/README.md#L14-L34)）。

围绕 KV Cache，接口中有四个关键参数：

| 参数 | 含义 |
| --- | --- |
| `key` / `value` | `aclTensorList`（张量列表），每个 batch 元素一条 KV 缓存；非连续场景下 batch 间形状可不同 |
| `blockTable` | 页表，INT32 二维张量，形状 \((B, \text{maxBlockNumPerSeq})\)，记录每个请求的 KV 缓存块编号 |
| `blockSize` | 每个 KV 块内的 token 数（属性参数），文档约束最小 128、最大 512、须为 128 的倍数 |
| `queryPaddingSize` / `kvPaddingSize` | 每个请求 Q 侧 / KV 侧的左填充长度，一维张量 |

注意 `blockTable` 是**功能开关**：host 侧 tiling 直接用「blockTable 是否存在」判断是否走 PagedAttention 分支（下文 4.4.3 会看到 `isPageAttention` 的判定代码）。文档还提醒：blockTable 中填充的是 blockid，框架**不校验其合法性**，需用户自行保证（[aclnnFusedInferAttentionScore.md:L821-L826](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScore.md#L821-L826)）。

#### 4.1.2 核心流程

一个 decode 步（Q_S=1、开启 PagedAttention）的数据流：

```text
query(B,1,N,D)
key/value: 按 blockTable 索引的逻辑序列 (B, blockNum, blockSize, N, D)
   │
   ├─ blockTable[b][i] = 第 b 个请求第 i 段 KV 所在的物理块号
   ├─ actualSeqLengthsKv[b] = 第 b 个请求的有效 KV 长度
   └─ kvPaddingSize[b] = 第 b 个请求 KV 侧左填充长度
   ▼
按页表 gather 各 block → 拼成逻辑 KV 序列 → softmax(QK^T/√d)·V
   ▼
attentionOut(B,1,N,D)
```

#### 4.1.3 源码精读

**（1）def 文件中的 KV Cache 参数注册。** def 是算子的「静态户口」。FIA 的 key/value 被声明为 `ParamType(DYNAMIC)`（动态输入，即 tensor list），并且 `IgnoreContiguous()`——因为分页 KV 天然允许 batch 间非同形，不能像普通输入那样自动连续化：

[attention/fused_infer_attention_score/op_host/fused_infer_attention_score_def.cpp:L61-L88](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fused_infer_attention_score_def.cpp#L61-L88) —— 注册 key 输入：DYNAMIC 参数类型、百级长度的 dtype 排列组合（FP16/BF16/INT8/INT4 混合场景枚举）、ND 格式、`IgnoreContiguous()` 关闭自动连续化。

文件末尾的 ascend950（A5）配置展示了「静态编译 + 全动态」的注册方式，并经 `ExtendCfgInfo` 挂接 kernel 入口文件名：

[attention/fused_infer_attention_score/op_host/fused_infer_attention_score_def.cpp:L2165-L2181](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fused_infer_attention_score_def.cpp#L2165-L2181) —— `DynamicCompileStaticFlag(true)`、`DynamicShapeSupportFlag(true)`、`ExtendCfgInfo("opFile.value", "fused_infer_attention_score_apt")` 连接 host 与 kernel 入口，`AddConfig("ascend950", aicore_config_95)` 注册到 A5。

**（2）op_api 层判断「缓存场景」的方式。** inner 文件中有一个极简的判定函数：

```cpp
bool IsCacheScene(const aclTensor *blockTableOptional)
{
    return blockTableOptional != nullptr && blockTableOptional->GetViewShape().GetShapeSize() != 0;
}
```

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L153-L157](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L153-L157) —— blockTable 非空且元素数不为 0 即认为开启了 KV Cache 分页场景；该标志后续决定 KV tensorList 走「归一化视图」还是「强制连续化」路径（见 4.3.3）。

**（3）语义校验在 checkers 里，不在 op_api 里。** 例如 blockTable 的 dtype 必须是 INT32、必须二维且各维为正：

[attention/fused_infer_attention_score/op_host/checkers/paged_attention_checker.cpp:L30-L74](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/paged_attention_checker.cpp#L30-L74) —— `PagedAttentionChecker::CheckBlockTableDtype` 强制 `DT_INT32`；`CheckBlockTableShapeSize` 要求维度数为 2、元素数非 0，否则用 `OP_LOGE_FOR_INVALID_SHAPEDIM_WITH_REASON` 输出结构化错误。

`kvPaddingSize`/`queryPaddingSize` 的校验则在 LeftPaddingChecker：要求 shape size 为 1 的一维张量，且开启 kv padding 时 `actualSeqLengthsKv` 不能为空：

[attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp:L52-L70](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp#L52-L70) —— `queryPaddingSize`/`kvPaddingSize` 的 shape 校验（shape size 必须为 1、维度必须为 1）。

[attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp:L110-L114](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp#L110-L114) —— 交叉约束：kv_padding_size 存在时 actualSeqLengthsKv 不能为空。

> **一个重要的分层结论**：op_api（aclnn 层）只做指针/连续性这类「通用卫生检查」，而 blockTable、padding、量化等**业务语义校验集中在 op_host 的 tiling/checkers 里**。这与 u4-l2 讲的 FA（校验大多在 op_api）不同——FIA 参数太多，把语义校验下沉到 host 侧按特性拆分维护。

#### 4.1.4 代码实践

1. **实践目标**：建立「KV Cache 参数 → 文档约束 → 源码校验位置」的对照能力。
2. **操作步骤**：
   - 打开 [docs/aclnnFusedInferAttentionScore.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScore.md) 的参数表（`blockTable` 在 L298 附近，`queryPaddingSize` L309、`kvPaddingSize` L321、`blockSize` L417），以及约束说明 L821-L837（PagedAttention 约束）。
   - 再打开 [docs/aclnnFusedInferAttentionScoreV5.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV5.md)，看 L1024-L1240 的「特性参数组」表——V5 把参数按 PA/Mask/量化等**参数组**重新组织并给出组间交叉约束。
   - 整理一张 KV Cache 参数含义表（参数名 / 方向 / dtype / shape 约束 / 校验代码位置）。
3. **需要观察的现象**：V5 文档参数是 `xxxOptional` 风格（显式可空），V1 不是；V5 的 KV 排布明确支持 BnBsH、BnNBsD、NZ 三种分页格式。
4. **预期结果**：你会发现自己整理的「校验代码位置」一列，多数落在 `op_host/checkers/` 而不是 `op_api/`——这正是 4.1.3 的分层结论。表格中「校验位置」若在 op_api 层找不到，属正常现象，不是你漏看了。
5. 运行环节无需 NPU，属于源码阅读型实践，结论可直接核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 key/value 在 def 文件里用 `IgnoreContiguous()` 而 query 用 `AutoContiguous()`？
**答案**：query 是规整张量，框架自动拷贝为连续即可；key/value 是 tensor list 且在 PagedAttention/非连续场景下 batch 间形状可以不同（batch 只能为 1 的 tensorlist 例外），自动连续化会掩盖「仅首维非连续」这类可零拷贝处理的情况，因此交给 op_api 层按场景手动处理（见 4.3.3 的 `NormalizeFAICacheTensorList`）。

**练习 2**：若用户传入的 blockTable 里有一个非法 blockid（超出 KV 池范围），算子会在哪一层报错？
**答案**：不会报错。文档 [aclnnFusedInferAttentionScore.md:L821](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScore.md#L821) 明确说明当前不对 blockid 合法性校验，checkers 只校验 blockTable 的 dtype/shape，越界访问属于未定义行为，需用户自行保证页表正确。

**练习 3**：`IsCacheScene` 里为什么除判空外还要判 `GetShapeSize() != 0`？
**答案**：调用方可能传入一个非空指针但 shape 为 0 的「占位 blockTable」。shape size 为 0 意味着没有实际页表内容，不应触发分页路径，所以视为非缓存场景，走普通连续 KV 的处理分支。

### 4.2 版本演进：V1～V5 入口的「垫片式」组织

#### 4.2.1 概念说明

docs 目录下有 V1～V5 五份接口文档，op_api 目录下有对应的五个入口文件。它们不是五份拷贝，而是：

- **V1（`aclnnFusedInferAttentionScore`）**：最早的入口，参数较少（无 MLA rope、无 per-tensor KV 反量化等）。功能仍可用，但已在 950 上退役并计划 2026 年 12 月弃用。
- **V2/V3/V4**：逐步追加参数（KV 反量化 scale/offset、shared prefix、rope 拆分 MLA、outDtype 等），A2/A3 上继续支持。
- **V5**：面向 Ascend 950（arch35）的当前主推版本，参数组化文档、支持非连续 KV 直通（TensorV2）。

与 u4-l2 的 FA 家族「13 个 L2 接口共用唯一 base」同构：**版本演进 = 新增一个薄入口 + 老参数补空，公共逻辑一律下沉**。FIA 下沉的终点是 `InnerFusedInferAttentionScoreGetWorkspaceSize`（op_api 内部统一入口）与 `l0op::FusedInferAttentionScore`（l0 base）。

#### 4.2.2 核心流程

```text
aclnnFusedInferAttentionScore(V1)      ─┐
aclnnFusedInferAttentionScoreV2        ─┤  各自做少量版本特有预处理
aclnnFusedInferAttentionScoreV3        ─┼──────────────┐
aclnnFusedInferAttentionScoreV4        ─┤              ▼
aclnnFusedInferAttentionScoreV5        ─┘   InnerFusedInferAttentionScoreGetWorkspaceSize
                                           （校验 + Contiguous 流水线）
                                                   ▼
                                       l0op::FusedInferAttentionScore（l0 base，全量 60+ 参数）
                                                   ▼
                                       INFER_SHAPE + ADD_TO_LAUNCHER_LIST_AICORE
```

第二段（执行段）五个版本完全相同：都直接调 `InnerFusedInferAttentionScore` → `CommonOpExecutorRun`。

#### 4.2.3 源码精读

**（1）V1 垫片：硬件门禁 + 弃用告警 + 补空转发。** V1 入口只有 94 行，全部是「翻译」工作：

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp:L45-L55](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp#L45-L55) —— 两层门禁：若当前 NPU 架构是 `DAV_3510`（Ascend 950），直接返回 `ACLNN_ERR_RUNTIME_ERROR`（V1～V4 在 950 上不再支持）；否则首次调用打印弃用告警，提示 2026 年 12 月迁移到 V5。`static bool isFirstCall` 保证告警只打一次。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp:L60-L66](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp#L60-L66) —— 把 V1 缺失的十几个新参数（key/value 反量化、shared prefix、rope、qStartIdx/kvStartIdx 等）全部用 `nullptr`/`0` 补空，再转发给 `InnerFusedInferAttentionScoreGetWorkspaceSize`。**这就是版本共存的全部秘密：新版本参数在老接口里永远是"空"。**

**（2）V5：当前主推版本的额外职责。** V5 入口在调用 inner 之前多做两件事：

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp:L186-L203](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp#L186-L203) —— `CheckTensorContiguous`：检查 key/value tensorList 首元素、keyRope、KV 反量化 scale 是否连续。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp:L250-L254](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp#L250-L254) —— 用**弱符号** `NnopbaseSupportTensorV2() __attribute__((weak))` 探测运行环境的 opbase 包是否支持 TensorV2（非连续 tensor 直通）：非连续且旧 opbase → 报错拦截；新 opbase → 放行，交给 inner 的归一化路径零拷贝处理。这是「源码同一份、跨 CANN 版本自适应」的典型手法。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp:L284-L288](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp#L284-L288) —— V5 第二段直接透传 `aclnnInnerFusedInferAttentionScore`，与其他版本完全一致。

**（3）版本演进的产品化证据。** README 的调用说明把 A2/A3 指向 V4 示例、950 指向 V5 示例：

[attention/fused_infer_attention_score/README.md:L171-L181](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/README.md#L171-L181) —— A2/A3 用 `test_aclnn_fused_infer_attention_score_v4_gqa_noquant.cpp`，950 用 `examples/arch35/` 下的 V5 示例，与 u2-l4 讲过的「ascend950 优先用 arch35 专属示例」呼应。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「V2/V3/V4/V5 → inner」的调用收敛。
2. **操作步骤**：
   - 在仓库根目录执行（源码阅读型，不编译）：
     ```bash
     grep -n "InnerFusedInferAttentionScoreGetWorkspaceSize" \
       attention/fused_infer_attention_score/op_api/*.cpp
     ```
   - 再分别打开 V2（[L193 附近](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v2.cpp#L193)）、V3（L224）、V4（L207）查看它们传给 inner 的实参列表，数一数各自补了多少个 `nullptr`/`0`。
   - 对比 V2 与 V5 的函数原型（各自 `.h` 文件），列出 V5 比 V2 多出的参数名。
3. **需要观察的现象**：五个 `.cpp` 中只有 V1 和 V2～V5 各自一处调用 inner；版本越新，转发前做的特有预处理越多（V2 基本透传，V5 有 TensorPreProcess/弱符号探测）。
4. **预期结果**：V5 比 V2 多出 queryRope/keyRope/keyRopeAntiquantScale/dequantScaleQuery/learnableSink/qStartIdx/kvStartIdx/keyAntiquantMode/valueAntiquantMode/queryQuantMode/pseType 等参数（以你实际对照为准）。
5. 本实践无需 NPU，「预期结果」待你本地核对；参数清单以头文件声明为准，不要以本讲义为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 V1 在 950 上直接返回错误，而不是静默转发给 V5 的实现？
**答案**：V1 参数集是 V5 的真子集，静默转发意味着旧语义（如缺少 MLA/量化参数的组合）要靠 inner 里大量补空分支兜底，行为难以验证；明确报错 + 弃用告警 + 文档迁移指引是更安全的产品策略（见 `aclnn_fused_infer_attention_score.cpp:L45-L48` 与 L51-L54 的告警文案）。

**练习 2**：弱符号 `NnopbaseSupportTensorV2` 的判空为什么能探测 opbase 版本？
**答案**：该符号声明为 weak：若链接到的 opbase 动态库中导出了它，符号地址非空（新版本）；否则保持空地址（旧版本）。编译期一份代码即可适配两种运行环境，这是 C/C++ 二进制兼容的常用技巧（声明见 [aclnn_fused_infer_attention_score_v5.cpp:L59-L60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_v5.cpp#L59-L60)，使用见 L250-L254）。

### 4.3 base/inner 分层：60+ 参数的公共实现如何复用

#### 4.3.1 概念说明

FIA 的完整参数表超过 60 项。如果每个版本入口都各自实现一遍校验与下发，五份代码会立刻失控。仓库的做法是把职责切成两层：

- **inner 层**（`aclnn_fused_infer_attention_score_inner.cpp`）：面向 aclnn 的「总装车间」。做空指针校验、输出空张量短路、全量输入 Contiguous 流水线、调 l0 base、把 l0 输出 ViewCopy 回用户张量、汇总 workspace。
- **l0 base 层**（`fused_infer_attention_score_base_aclnn.cpp` 中的 `l0op::FusedInferAttentionScore`）：与算子同名的最小实现。做 IntArray→Tensor 转换、输出张量分配、`INFER_SHAPE`、`ADD_TO_LAUNCHER_LIST_AICORE`。它不知道任何 aclnn 版本的存在。

对比 u4-l2 的 FA：结构完全同构（L2 多入口 → inner/base 单实现），FIA 只是参数规模更大、且把「KV 非连续处理」做得更精细。

#### 4.3.2 核心流程

inner 的第一段（`InnerFusedInferAttentionScoreGetWorkspaceSize`）流程：

```text
CREATE_EXECUTOR
  → 空指针校验（query/key/value/attentionOut/workspaceSize/executor，
     softmaxLseFlag 为真时追加 softmaxLse，inputLayout 判空）
  → attentionOut 为空张量？→ 是：workspaceSize=0，直接成功返回
  → 对约 27 个输入逐个 MakeTensorContiguous（Contiguous + SetTensorFormatToND）
  → ProcessKVForL0Input：KV tensorList 按「缓存场景 + FAI 路由候选 + TensorV2」
     决定 NormalizeFAICacheTensorList（零拷贝视图）或强制连续化
  → l0op::FusedInferAttentionScore（全量参数）
  → ViewCopy(attentionOut)、可选 ViewCopy(softmaxLse)
  → GetWorkspaceSize + ReleaseTo(executor)
```

#### 4.3.3 源码精读

**（1）inner 的空指针校验与空输出短路。**

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L483-L504](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L483-L504) —— 创建 executor 后，用 `OP_CHECK_NULL` 依次检查必填指针；`softmaxLse` 是否必填取决于 `softmaxLseFlag`（条件必填，比 FA 的静态必填更灵活）。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L510-L514](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L510-L514) —— 输出 `attentionOut` 是空张量时直接置 `workspaceSize = 0` 返回成功，与 u3-l1 讲过的「空输出短路」一致：算子无活可干，也不必让框架报错。

**（2）KV tensorList 的两种处理路径。** 这是 FIA 预处理中最有推理特色的一段：

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L419-L452](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L419-L452) —— `ProcessKVForL0Input`：先校验 key/value 两个 tensorList 大小一致；若「opbase 支持 TensorV2 + 是 FAI 路由候选 + 是缓存场景」三个条件同时成立，走 `NormalizeFAICacheTensorList`（保留 stride 信息的零拷贝视图），否则退回 `MakeTensorListContiguous`（每个元素真实拷贝成连续）。KV Cache 通常很大，能不拷就不拷——这条分支就是为省下整块 KV 拷贝设计的。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L255-L299](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L255-L299) —— `IsFirstAxisOnlyNonContiguous`：逐维检查 view strides，判断张量是否「仅第 0 维非连续」（后缀各维 stride 连续）。仅首维非连续在 KV Cache 里极常见（对齐 padding 造成），可用 `executor->CreateView` 零拷贝修正 storage shape。

**（3）调用 l0 base 与结果回写。**

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L599-L615](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L599-L615) —— 以全量参数调用 `l0op::FusedInferAttentionScore`，返回 `(attentionOutOut, softmaxLseOut)` 二元组；任一为空即失败。

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L617-L628](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L617-L628) —— `l0op::ViewCopy` 把 l0 输出拷回用户提供的（可能非连续的）输出张量；随后 `GetWorkspaceSize()` 汇总临时内存，`ReleaseTo(executor)` 把执行计划交还给调用方。

**（4）l0 base：INFER_SHAPE 与下发。**

[attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp:L105-L130](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp#L105-L130) —— base 层只做最基础的空检查，然后把 5 个 `aclIntArray*`（actualSeqLengths 等）经 `ConvertIntArrayToTensor` 转成设备张量——数组类参数在图模式下必须是张量输入，这一步是 eager 接口向算子原型的「翻译」。

[attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp:L142-L158](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp#L142-L158) —— `INFER_SHAPE(FusedInferAttentionScore, OP_INPUT(...31 项...), OP_OUTPUT(...), OP_ATTR(...16 项...))`：触发 u2-l2 讲过的 infershape，由 def 注册的推导函数计算输出形状。注意 `scaleValue` 被显式转成 `static_cast<float>`（与 u4-l2 追踪过的精度截断一致）。

[attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp:L160-L176](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp#L160-L176) —— `ADD_TO_LAUNCHER_LIST_AICORE`：把算子（连同全部输入/属性）挂到 executor 的 AICore 下发列表，真正的 tiling 与 kernel 选择在执行期由框架完成。

**（5）第二段的完全模板化。**

[attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp:L631-L637](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L631-L637) —— 内部第二段只有 `L2_DFX_PHASE_2` 打点 + `CommonOpExecutorRun` 一行，五个外部版本共用，无任何版本分支。

#### 4.3.4 代码实践

1. **实践目标**：走通一条「V1 接口 → inner → l0 base → 下发」的完整调用链。
2. **操作步骤**：
   - 从 [aclnn_fused_infer_attention_score.cpp:L60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score.cpp#L60) 的 `InnerFusedInferAttentionScoreGetWorkspaceSize` 调用出发，跳到 inner 的 L599 `l0op::FusedInferAttentionScore`，再跳到 base 的 L142 `INFER_SHAPE`，画出三级调用图并标注每级的「新增职责」。
   - 特别跟踪 `kvPaddingSize` 这个参数：它在 V1 入口的哪里被补成 `nullptr`？在 inner 的哪一行被 Contiguous？在 base 的 `OP_INPUT` 列表第几个位置？（提示：[inner L574-L577](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L574-L577)）
3. **需要观察的现象**：`kvPaddingSize` 在 V1 里恒为 nullptr，因此 V1 用户不可能触发 LeftPaddingChecker 的 kv padding 分支——版本差异直接体现为「哪些 checker 分支永远走不到」。
4. **预期结果**：一张三级调用图 + kvPaddingSize 的完整传递路径标注。纯源码阅读，无需 NPU，可立即完成。
5. 若对某些宏（如 `ADD_TO_LAUNCHER_LIST_AICORE`）的展开有疑问，可在 `common/include` 或 CANN 头文件中搜索其定义（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`ProcessKVForL0Input` 为什么要同时满足三个条件才走零拷贝路径？
**答案**：①opbase 支持 TensorV2（旧 opbase 无法处理带 stride 的输入）；②`IsFAIRoutingCandidate`（只有 TND 布局、D≤256、blockSize 16 对齐且 ≤512 等一组合法场景，见 [inner L159-L238](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/aclnn_fused_infer_attention_score_inner.cpp#L159-L238) 的逐条过滤）；③是缓存场景（blockTable 有效）。任一不满足都退回「拷贝成连续」的保守路径，用正确性换性能兜底。

**练习 2**：`ConvertIntArrayToTensor` 为什么只对非空数组调用？空数组如何处理？
**答案**：数组为 `nullptr` 表示用户未启用该可选功能（如 actualSeqLengths 缺省），转成的 tensor 也保持 `nullptr`，def 里该输入是 OPTIONAL，infershape/tiling 会按缺省语义处理；非空时才 `executor->ConvertToTensor` 转成 INT64 张量并把格式改为 ND（[base_aclnn.cpp:L24-L38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_api/fused_infer_attention_score_base_aclnn.cpp#L24-L38)）。

**练习 3**：inner 里 `MakeTensorContiguous` 被调用了约 27 次，为什么不像 key/value 一样封装成批量函数？
**答案**：key/value 是 tensorList，元素个数运行期才知道，必须批量循环；其余输入是单个 tensor，逐个显式调用让每次失败的错误日志都能带上具体参数名（"query"、"pseShift"……），排查体验更好；这也是 u4-l2 讲过的「便宜检查在前、精确日志在后」思想的延续。

### 4.4 tiling 的场景路由与 checkers 工程化拆分

#### 4.4.1 概念说明

FIA 的 host 侧 tiling 面临两个维度的组合爆炸：

- **场景维度**：同一个算子要服务 prefill（PFA 路线）、decode（IFA 路线）和 split-fuse（FAI 路线）等不同执行策略；
- **架构维度**：arch22（A2/A3）、arch35（950）、arch38 的 tiling 实现差异大，仓库用 `op_host/arch22`、`arch35`、`arch38` 子目录物理隔离。

同时，由于参数超过 60 个，如果把全部校验写进 tiling 主函数，单文件会膨胀到无法维护。`checkers/` 目录把校验按「特性」拆成 16 个小类（paged_attention、left_padding、mask、pse、rope、dequant、post_quant、learnable_sink、system_prefix、softmax_lse……），每个类继承统一的 `BaseChecker` 接口，共享 `FiaTilingInfo` 这一参数信息结构。

#### 4.4.2 核心流程

tiling 入口的分发逻辑：

```text
DoOpTilingFusedInferAttentionScore(context)
  ├─ NpuArch == DAV_3510（950）  → TilingFusedInferAttentionScoreV4（arch35 实现）
  ├─ NpuArch == DAV_5102        → TilingFusedInferAttentionScoreArch38
  └─ 其他（A2/A3）              → TilingFusedInferAttentionScore（基线实现）
        ├─ RouteToFia(context) 为真 → 转发 TilingFusedInferAttentionScoreV3
        ├─ 校验 QKV / inputLayout / 输出 shape
        ├─ IsUsingFAI → TilingProcess4SplitFuse（split-fuse 场景）
        ├─ IsUsingIFA → TilingProcess4IFA（增量 decode 场景）
        └─ 否则       → TilingProcess4PFA（全量 prefill 场景）
```

checker 的四段式接口（每个校验器都要实现）：

```text
CheckSinglePara             —— 单参数自身合法性（dtype/shape/取值范围）
CheckParaExistence          —— 参数存在性（该特性启用时必填项是否齐全）
CheckCrossFeature           —— 与其他特性的交叉约束（如 kvPaddingSize × PA）
CheckMultiParaConsistency   —— 多参数间一致性（如 query 与 KV 的头数关系）
```

#### 4.4.3 源码精读

**（1）tiling 入口的三路场景分发。**

[attention/fused_infer_attention_score/op_host/fused_infer_attention_score_tiling.cpp:L2235-L2292](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fused_infer_attention_score_tiling.cpp#L2235-L2292) —— `TilingFusedInferAttentionScore`：先尝试 `RouteToFia` 转发 V3 实现；随后注意 L2252 的关键一行——`isPageAttention` 直接由「blockTable 输入是否存在」决定（`GetOptionalInputShape(BLOCK_TABLE_INDEX) != nullptr`），印证 4.1 的结论：blockTable 就是 PagedAttention 的开关。最后按 `IsUsingFAI`/`IsUsingIFA` 分发到 SplitFuse/IFA/PFA 三条 tiling 流水线。

**（2）SoC 架构分发。**

[attention/fused_infer_attention_score/op_host/fused_infer_attention_score_tiling.cpp:L2295-L2312](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fused_infer_attention_score_tiling.cpp#L2295-L2312) —— `DoOpTilingFusedInferAttentionScore`：取平台信息判断 `NpuArch`，950（DAV_3510）走 V4 实现、DAV_5102 走 arch38 实现、其余走基线实现。与 op_api 层「V1～V4 在 950 上拒绝执行」形成闭环：**950 的 aclnn 入口只有 V5，而 V5 的 tiling 由这里分发到专属实现**。

**（3）checkers 的基类契约。**

[attention/fused_infer_attention_score/op_host/checkers/base_checker.h:L27-L45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/base_checker.h#L27-L45) —— `BaseChecker` 抽象类定义四个纯虚的 `Check*` 方法，并提供建造三模式开关的构造函数 `BaseChecker(enableNonQuant, enableFullQuant, enableAntiQuant)`——同一个 checker 在非量化/全量化/反量化三种模式下校验规则不同。

**（4）FiaTilingInfo：校验器与 tiling 的共享语言。**

[attention/fused_infer_attention_score/op_host/fia_tiling_info.h:L19-L62](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/fia_tiling_info.h#L19-L62) —— 统一定义每个参数的日志用名（`BLOCK_TABLE_NAME = "block_table"` 等）：所有 checker 的错误信息共用这套名字，用户看到的报错与文档参数名严格一致。这是大型算子「报错体验工程化」的细节。

#### 4.4.4 代码实践

1. **实践目标**：体会「按 SoC 编译裁剪」与「按特性拆分校验」两种工程手段。
2. **操作步骤**：
   - 查看 `ls attention/fused_infer_attention_score/op_host/arch22 attention/fused_infer_attention_score/op_host/arch35 attention/fused_infer_attention_score/op_host/arch38`，对比三个目录的文件构成。
   - 在 checkers 目录执行 `grep -l "PageAttention\|blockTable" *.cpp`，找出所有与 PagedAttention 有交叉约束的校验器（应不止 paged_attention_checker 一个，left_padding_checker、mask_checker 等都有 PA 交叉分支）。
   - 挑 [left_padding_checker.cpp:L208-L260](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/op_host/checkers/left_padding_checker.cpp#L208-L260) 的四个 `Check*` 实现各读一遍，画出每个方法内部又调用了哪些私有 `CheckFeature*`。
3. **需要观察的现象**：一个特性（如 left padding）的校验散落在 4 个公开方法 + 若干私有方法中，且大量分支以 `isPageAttention`、`enableAntiQuant` 等标志为条件——**特性的正交组合**是这类代码的主复杂度来源。
4. **预期结果**：一份「checker 类 × 负责特性 × 交叉特性」的清单表。纯源码阅读实践。
5. 若想进一步验证 arch35 与基线 tiling 的行为差异，需在 950 环境编译运行（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`TilingFusedInferAttentionScore` 里 `isPageAttention` 只看 blockTable 输入是否存在，而 op_api 的 `IsCacheScene` 还要求 `GetShapeSize() != 0`，两者矛盾吗？
**答案**：不矛盾，二者处于不同阶段、用途不同。op_api 的判定决定**预处理路径**（KV 是否按分页归一化），更精细以避免误拷贝；tiling 的判定决定**tiling 流水线选择与校验分支**，blockTable 存在即意味着用户声明了 PA 语义。若传入非空指针但 shape 为 0，op_api 视为非缓存场景做普通连续化，tiling 侧的 paged checker 对空 shape 会直接报错拦截（`CheckBlockTableShapeSize` 要求 shapeSize 非 0）。

**练习 2**：为什么不把 V4（arch35）的 tiling 直接写进基线函数里用 if 分支，而要独立成函数？
**答案**：三个原因：①不同架构的可用 UB、核数、指令集差异大，tiling 策略本质不同，混写会让单函数膨胀成数千行；②arch 目录 + 独立入口函数让「新增一个 SoC」变成「新增一个目录 + 一个分发分支」，回归风险小（呼应 u4-l3 的多 SoC 主题）；③`DoOpTiling...` 的分发在编译产物层面也可以按 SoC 裁剪，未目标架构的实现不参与链接。

**练习 3**：如果为 FIA 新增一个特性参数（例如某种新的 mask 类型），按 checkers 的设计应交付什么？
**答案**：新增一个继承 `BaseChecker` 的 `XxxChecker`（实现四个 `Check*`），在 `fia_tiling_info.h` 中登记参数名常量与 `FiaTilingInfo` 字段，再在 tiling 主流程合适位置挂载调用；与该特性有交叉的老 checker（如 mask_checker、paged_attention_checker）里补 `CheckCrossFeature` 分支。不需要改动任何 aclnn 版本入口以外的公共代码。

## 5. 综合实践

**任务：为「V1 与 V5 的 KV Cache 能力差异」写一份评审报告。**

1. 通读 [docs/aclnnFusedInferAttentionScore.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScore.md) 与 [docs/aclnnFusedInferAttentionScoreV5.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV5.md)，整理 KV Cache 相关参数（blockTable、blockSize、queryPaddingSize、kvPaddingSize、actualSeqLengthsKv、key/value 布局）在两个版本中的差异表。
2. 对表中每一项，在源码中定位对应实现/校验位置：入口垫片（`aclnn_fused_infer_attention_score*.cpp`）、inner（Contiguous 路径）、tiling（`isPageAttention` 分支）、checkers（具体 `Check*` 函数），形成「文档 → 源码」双向可追溯的引用（文件 + 行号）。
3. 回答两个开放问题（各写 100 字左右，附源码证据）：
   - V5 引入弱符号探测与零拷贝 KV 归一化，主要收益是什么？在什么条件下会退化回拷贝路径？
   - 如果要在 950 上用 V1 接口调用，会在哪一行代码、以什么错误码失败？

## 6. 本讲小结

- FIA 是一个算子覆盖 prefill（PFA）与 decode（IFA）两类推理场景的融合注意力算子；`blockTable` 是否存在就是 PagedAttention 的开关，`blockSize` 定义页大小，`queryPaddingSize`/`kvPaddingSize` 描述左填充。
- V1～V5 五个 aclnn 入口是「垫片」：各自做版本特有预处理（V1 补空转发、V5 弱符号探测），统一收敛到 `InnerFusedInferAttentionScoreGetWorkspaceSize` 与 `l0op::FusedInferAttentionScore`，第二段完全模板化；V1～V4 在 Ascend 950 上被入口层直接拒绝。
- inner 层负责通用卫生检查与 Contiguous 流水线，其中 KV tensorList 有一条「仅首维非连续 → CreateView 零拷贝」的快路径；l0 base 负责 IntArray→Tensor 翻译、INFER_SHAPE 与 AICore 下发。
- 业务语义校验（blockTable dtype/shape、padding、量化等）不在 op_api，而是集中在 op_host 的 `checkers/`：16 个继承 `BaseChecker` 四段式接口的小类 + 共享的 `FiaTilingInfo`，按特性正交拆分。
- tiling 入口先按 NpuArch 分发（950→V4 实现、DAV_5102→arch38、其余→基线），基线内部再按场景分发 SplitFuse/IFA/PFA，与 op_api 层的版本门禁互为闭环。
- 报错文案中的参数名由 `fia_tiling_info.h` 统一登记，保证日志、文档、接口三处命名一致——大型算子可维护性的重要细节。

## 7. 下一步学习建议

- 下一讲 u4-l5 将进入量化与稀疏：`quant_flash_attn`、lightning_indexer 与 `docs/zh/context/quant_mode_introduction.md`。FIA def 文件里成百行的 dtype 排列组合（INT8/INT4/FP8）正是量化模式矩阵的投影，建议先回头扫一眼 def 文件中 `antiquantScale` 等量化参数的注册方式。
- 若想继续深挖推理 attention，可对照阅读 [attention/prompt_flash_attention](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/prompt_flash_attention/README.md) 与 [attention/incre_flash_attention](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/incre_flash_attention/README.md) 的 README——FIA 的 tiling 正是路由到这两条产品线的实现。
- 有 NPU 环境的读者，可以用 `bash build.sh --run_example fused_infer_attention_score`（配 `--soc`）跑通 [examples/test_aclnn_fused_infer_attention_score_v4_gqa_noquant.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/fused_infer_attention_score/examples/test_aclnn_fused_infer_attention_score_v4_gqa_noquant.cpp)，观察一次真实的 GQA + KV Cache 调用。
