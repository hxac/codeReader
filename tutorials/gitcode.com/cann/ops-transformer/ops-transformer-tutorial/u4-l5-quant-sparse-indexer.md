# 量化与稀疏：quant attention 与 lightning indexer

## 1. 本讲目标

本讲是 Attention 模块精读的最后一讲，进入「全量化 + 稀疏」这条面向超长序列、超低精度场景的技术线。学完本讲，读者应该能够：

1. 说清楚 CANN 量化体系中的各种量化粒度（pertensor/perchannel/pertoken/pergroup/perblock）以及「全量化」「伪量化」「MX 量化」三个组合概念。
2. 理解量化模式如何直接影响算子接口设计——以 `quant_flash_attn` 为例，看懂 `q_descale/k_descale/v_descale` 这一组「量化参数输入」的 shape/dtype 约束是如何按量化模式分派的。
3. 理解 `sparse_mode` 的语义（0~9 共 10 种掩码模式），以及稀疏 attention 相比 dense attention 的收益来源。
4. 理解 `lightning_indexer` 系列 TopK 筛选算子在稀疏训练（如 NSA，Native Sparse Attention 类架构）中的位置：先用 indexer 挑出每个 token 最相关的 Top-k 个 block，再用稀疏 attention 只算这些 block。
5. 能画出一条「indexer 筛选 → 稀疏/量化 attention 计算」的完整数据流，并标注每一步使用的算子。

## 2. 前置知识

本讲默认读者已掌握前几讲的内容，特别是：

- **aclnn 两阶段 API**（u3-l1）：算子调用分 GetWorkspaceSize 与 Run 两段，参数校验集中在第一段。
- **op_host 三件套**（u2-l2）：def 文件注册算子原型，tiling 计算执行计划，checker 做参数合法性校验（u4-l4 讲过 FIA 的 checkers 目录，本讲的 quant_flash_attn 有同款设计）。
- **FIA 的 KV Cache 与 PagedAttention**（u4-l4）：blockTable 分页管理 KV，layout 有 BSND/TND/PA 系列多种。

在此基础上补充两个新概念：

- **量化（Quantization）**：把高精度浮点（FP16/BF16/FP32）数据压缩成低 bit（INT8/FLOAT8 甚至 FLOAT4）表示的计算过程。低 bit 矩阵乘（cube 计算）吞吐更高、带宽更省，但必须伴随一个「缩放系数 scale」记录每一组数据被压缩了多少倍，计算完成后用 scale 还原，才能保证结果近似正确。这个 scale 在算子接口里就体现为 `descale` 输入（de-quantization scale，反量化缩放因子）。
- **稀疏注意力（Sparse Attention）**：标准 attention 中每个 query 要和全部 key 计算，复杂度为 \( O(S_q \cdot S_{kv}) \)。但在因果（causal）、滑窗（band）、树形（tree mask）等场景下，大量位置本该被 mask 掉，算而不用是浪费。「稀疏」有两层含义：一是**结构化 mask 稀疏**（sparse_mode 描述的 0~9 号掩码模式，矩阵形状可预测）；二是**动态 TopK 稀疏**（lightning_indexer 这类算子在线挑出每个 token 最重要的 k 个 block，矩阵形状由计算结果决定）。前者是「算子内部跳过无效区域」，后者是「算子外部先筛选再计算」。

一个术语提醒：文档里「全量化」指左、右矩阵都量化（activation 和 weight 都量化），不是「完全量化」的意思。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/context/quant_mode_introduction.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/quant_mode_introduction.md) | 量化模式概念文档：5 种量化粒度 + 全量化/伪量化/MX 组合定义 |
| [docs/zh/context/sparse_mode_introduction.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md) | sparseMode 概念文档：0~9 号掩码模式的含义与约束 |
| [attention/quant_flash_attn/op_host/quant_flash_attn_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_def.cpp) | QuantFlashAttn 算子原型定义：FP8 输入 + descale 组 + quant_compute_mode 属性 |
| [attention/quant_flash_attn/op_host/qfa_tiling_info.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info.h) | tiling 信息结构体：QfaQuantMode 枚举、layout 枚举、shape 限值 |
| [attention/quant_flash_attn/op_host/qfa_tiling_info_parser.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info_parser.cpp) | 从 TilingContext 解析出 QfaTilingInfo，含 quant_mode 解析 |
| [attention/quant_flash_attn/op_host/checkers/quant_checker.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp) | 量化参数组专用 checker：descale 的 dtype/shape/layout 按量化模式分派校验 |
| [attention/quant_flash_attn/op_host/quant_flash_attn_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_tiling.cpp) | tiling 主入口：Parse → Check → Registry 三段式流程 |
| [attention/quant_flash_attn/op_host/qfa_tiling_shape.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_shape.cpp) | 「layout → 轴序列 → 期望 shape」的通用 shape 校验器 |
| [attention/lightning_indexer/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md) | LightningIndexer 算子说明：Top-k 计算公式与参数表 |
| [attention/lightning_indexer/op_host/lightning_indexer_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp) | LightningIndexer 算子原型：sparse_count/sparse_mode 属性、sparse_indices 输出 |
| [attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp) | tiling 中对 sparse_count、sparse_mode、输出 shape 的校验 |

背景知识：`attention/` 目录下与本讲相关的算子家族还有 `sparse_flash_attention`（稀疏 attention 计算）、`quant_lightning_indexer`（量化版 indexer）、`quant_sparse_flash_mla`（量化 + 稀疏 MLA）、`dense/sparse_lightning_indexer_*_kl_loss`（indexer 训练损失）等，读者可用 `ls attention/ | grep -E "quant|sparse|indexer"` 自行浏览全景。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**量化模式**、**稀疏 attention**、**indexer 筛选**。

### 4.1 量化模式

#### 4.1.1 概念说明

量化解决的问题是：矩阵乘（cube 类算子）在高精度下计算，单位时间吞吐低、数据搬运量大。如果把输入从 FP16 压到 INT8/FLOAT8，理论上吞吐翻倍、带宽减半。代价是必须引入缩放系数 scale 来记录压缩幅度。

**量化模式（量化粒度）** 决定「多少个数据共用一个 scale」。粒度越细，精度越高，但 scale 张量本身越大、反量化计算越多。CANN 文档定义了 5 种基本粒度（以左矩阵 shape (m, k)、右矩阵 shape (k, n)，k 为 reduce 轴为例）：

| 模式 | 简称 | 量化对象 | scale shape |
| --- | --- | --- | --- |
| pertensor | T | 左/右矩阵 | (1,) —— 整个张量一个 scale |
| perchannel | C | 右矩阵（权重） | (n,) —— 每个输出通道一个 |
| pertoken | K | 左矩阵（激活） | (m,) —— 每个 token 一个 |
| pergroup | G | 左/右矩阵 | (m, k/gs) —— reduce 轴上按 gs 分组 |
| perblock | B | 左/右矩阵 | (m/bs, k/bs) —— 所有轴上按块分组 |

在此之上有三个组合概念：

- **全量化**：左、右矩阵都量化，如 T-C、K-C、G-B、T-CG、B-B 模式——本讲的 `quant_flash_attn` 就是全量化 attention（q/k/v 全部 FP8 输入）。
- **伪量化**：只量化权重（C 模式），激活仍高精度。
- **MX 量化**（Microscaling Formats）：OCP 标准的低精度表示，是 pergroup 的特例——scale 类型为 FLOAT8_E8M0 且 group size 为 32（qfa 代码中按 block size 64 的两倍冗余存储，见后文源码）。

静态量化（scale 预先算好，推理权重常用）与动态量化（scale 在线计算，激活和训练场景常用）是另一个正交维度。

#### 4.1.2 核心流程

量化 attention 一次前向的数据流：

```text
高精度 q/k/v
    │  量化（pergroup/per-token-head 等粒度，产出 FP8 数据 + descale 张量）
    ▼
FP8 q/k/v + q_descale + k_descale + v_descale
    │  QuantFlashAttn kernel
    │    1. Q@K^T 用 FP8 cube 计算（P' = 低比特分数矩阵）
    │    2. 用 q_descale × k_descale 还原量纲，进 softmax（FP32）
    │    3. softmax 结果 P 可再量化（p_scale，per-tensor 一个 scale）
    │    4. P@V 用 FP8 cube 计算，再用 v_descale（和 p_scale）反量化
    ▼
BF16 attn_out（输出反量化回高精度）
```

关键点：descale 张量的 **shape 由量化粒度决定**，dtype 由量化体系决定（MxFP8 用 FLOAT8_E8M0，GQA FP8 用 FP32）。这就把「量化模式」从概念文档落到了算子接口上——同一个算子，不同 quant_mode 下 descale 的形状完全不同，tiling 阶段必须按模式分别校验。

#### 4.1.3 源码精读

**（1）概念文档中的量化粒度定义**。[quant_mode_introduction.md:19-48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/quant_mode_introduction.md#L19-L48) 逐条列出 T/C/K/G/B 五种粒度及各自的 scale shape；[quant_mode_introduction.md:50-59](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/quant_mode_introduction.md#L50-L59) 定义全量化、伪量化与 MX 量化三个组合概念，其中 MX 明确为「FLOAT8_E8M0 + group size 32 的 pergroup 特例」。

**（2）量化模式如何变成接口**。看 QuantFlashAttn 的 def 文件——输入 q/k/v 全部是 FP8_E4M3，且每个都伴随一个 descale 输入：

```cpp
this->Input("q")
    .ParamType(REQUIRED)
    .DataType({ge::DT_FLOAT8_E4M3FN, ge::DT_FLOAT8_E4M3FN})
    ...
this->Input("q_descale")
    .ParamType(REQUIRED)
    .DataType({ge::DT_FLOAT8_E8M0, ge::DT_FLOAT})
    ...
```

见 [quant_flash_attn_def.cpp:27-56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_def.cpp#L27-L56)：这段注册了 FP8 的 q/k/v 三输入与 E8M0/FP32 双 dtype 的 q/k/v_descale 三输入——dtype 列表里的两个值正对应两种量化体系（MxFP8 与 GQA FP8）。输出则是固定的 BF16（[quant_flash_attn_def.cpp:102-106](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_def.cpp#L102-L106)），即「输入量化、输出反量化」。而量化模式本身是一个**必选属性**：

```cpp
this->Attr("quant_compute_mode")
    .AttrType(REQUIRED)
    .Int();
```

见 [quant_flash_attn_def.cpp:112-114](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_def.cpp#L112-L114)。目前支持的两个取值定义在枚举里：

```cpp
enum class QfaQuantMode : uint32_t {
    A8C8_QKV_MXFP8_P_FP8_E4M3_PER_TENSOR_SOFTMAX_FP32 = 1,
    A8C8_QK_FP8_E4M3_PER_TOKEN_HEAD_V_FP8_E4M3_PER_HEAD_P_FP8_E4M3_PER_TENSOR_SOFTMAX_FP32 = 6
};
```

见 [qfa_tiling_info.h:150-153](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info.h#L150-L153)。**请直接读枚举名，它就是一份自解释的量化配方**：模式 1 = q/k/v 用 MxFP8（pergroup，E8M0 scale），softmax 后的 P 用 FP8_E4M3 per-tensor 量化，softmax 累加用 FP32；模式 6 = q/k 用 FP8 per-token-head 粒度（每个 token 每个头一个 scale），v 用 FP8 per-head 粒度，P 仍 per-tensor。此外 [qfa_tiling_info.h:324-331](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info.h#L324-L331) 还定义了 PER_CHANNEL/PER_TOKEN/PER_TOKEN_HEAD/PER_GROUP/PER_BLOCK 等 8 个模式常量，是粒度体系在代码侧的完整清单。

**（3）tiling 主入口的三段式流程**。[quant_flash_attn_tiling.cpp:40-67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/quant_flash_attn_tiling.cpp#L40-L67) 中 `TilingQuantFlashAttn` 的骨架是：

```text
QfaInfoParser.Parse(qfaInfo)   // 从 context 抽出全部输入/属性到 QfaTilingInfo
QfaChecker.Process(qfaInfo)    // 分发到 16 个 checker（含 QuantChecker）
FiaTilingRegistry.DoTilingImpl // 按架构/场景路由到真正的 tiling 实现
```

这与 u4-l4 讲过的 FIA「checkers 目录 + TilingInfo」工程手法完全一致，说明该模式已在量化算子中复用。

**（4）quant_mode 的解析与校验**。[qfa_tiling_info_parser.cpp:259-279](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info_parser.cpp#L259-L279) 的 `GetQuantMode()` 只接受 1 和 6 两个值，其它取值在 tiling 阶段直接报错（注意这是比 def dtype 白名单更晚的第二道关卡）。

**（5）descale shape 按量化模式分派——本讲最核心的源码**。[quant_checker.cpp:346-396](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L346-L396) 的 `CheckQDescaleShapeMxFp8` 展示了 MxFP8 模式下 q_descale 的期望 shape：

```cpp
// 文档(descale_shape匹配关系表): MxFP8, layout_q=TND
//   4D: (Q_T, Q_N, D/64, 2)              prefill场景，layout_q_descale=TND
//   5D: (KV_N, Q_T, G, D/64, 2)          decode场景，layout_q_descale=N2TGD
//   其中 G = Q_N / KV_N
int64_t dPerGroup = (D + 63) / 64; // MxFP8 block size = 64
```

注意 `(D + 63) / 64` 和尾维的 `2`：每个 64 元素组配 2 个 E8M0 scale（上下界双 scale 冗余）。同一个函数还校验「4D 必须 TND（prefill）、5D 必须 N2TGD（decode）」的 layout 与场景绑定关系。对比 GQA FP8 模式：[quant_checker.cpp:398-408](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L398-L408) 中 q_descale 退化为简单的 2D `(N1, T)`——**per-token-head 粒度的 scale 天然和「头数 × token 数」同形**。k/v descale 的分派同理：MxFP8 下 k_descale 按 kv layout 取 4D/5D/6D 三种形状（[quant_checker.cpp:426-459](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L426-L459)），GQA FP8 下 v_descale 是 1D `(N2,)`（[quant_checker.cpp:530-540](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L530-L540)）——正是 per-head 粒度「每个 KV 头一个 scale」的直接体现。

**（6）dtype 与 layout 的模式绑定**。[quant_checker.cpp:40-45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L40-L45) 的 `DESCALE_DTYPE_TABLE` 把两种 quant_mode 分别锁定到 E8M0 与 FP32；[quant_checker.cpp:725-730](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L725-L730) 的 `QFA_LAYOUT_CONSTRAINT_TABLE` 则规定了每种模式下 kv/out/descale 的合法 layout 集合（如 MxFP8 的 layout_kv 只能是 TND/PA_BNBD/PA_NZ）。

**（7）通用 shape 校验器 qfa_tiling_shape.cpp**。上述期望 shape 最终交给 `QfaTilingShapeCompare` 比对，其核心是一张「layout → 轴序列」映射表（[qfa_tiling_shape.cpp:24-33](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_shape.cpp#L24-L33)，覆盖 BSND/BNSD/TND/NTD/PA_BBND/PA_BNBD/PA_NZ/N2TGD/NT 等 layout），以及按 layout 生成期望 shape 的 switch（[qfa_tiling_shape.cpp:155-193](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_shape.cpp#L155-L193)）与逐轴可配置比较符（`==/>=/<=/!=`，甚至 `IGNORE_INPUT` 通配，见 [qfa_tiling_shape.cpp:54-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_shape.cpp#L54-L61)）。这就是大纲中所说「qfa_tiling_shape.cpp 对量化 shape 的处理」——它把「每种量化模式 × 每种 layout 的 shape 约束」统一抽象成数据驱动的查表比较。

#### 4.1.4 代码实践

**实践目标**：亲手从源码中归纳出「quant_mode → descale 形状」对照表，验证对量化粒度的理解。

**操作步骤**：

1. 阅读 [docs/zh/context/quant_mode_introduction.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/quant_mode_introduction.md)，复述 T/C/K/G/B 五种粒度的 scale shape。
2. 打开 [qfa_tiling_info.h:150-153](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/qfa_tiling_info.h#L150-L153)，把两个枚举名拆解成「哪个输入、什么精度、什么粒度」三列信息。
3. 在 `quant_checker.cpp` 中用编辑器搜索 `CheckQDescaleShapeMxFp8`、`CheckKDescaleShapeGqaFp8`、`CheckVDescaleShapeGqaFp8` 三个函数，整理出下表（答案见 4.1.5 练习 1）。

**需要观察的现象**：两种 quant_mode 下同名输入（如 v_descale）的期望 shape 维数差异极大（6D vs 1D），且 descale 维度里出现了数据张量没有的轴（`D/64`、`2`）——这些多出来的轴就是量化粒度在形状上的「脚印」。

**预期结果**：能得到一张 6 行左右的对照表，并能解释 MxFP8 模式 descale 尾维 `2` 的含义。本实践为纯源码阅读型，无需 NPU 环境，结果**待本地验证**（表中 shape 均直接摘自源码注释，可信）。

#### 4.1.5 小练习与答案

**练习 1**：填写下表并说明每行的「粒度脚印」。

| quant_mode | q_descale 期望 shape | k_descale 期望 shape | v_descale 期望 shape | descale dtype |
| --- | --- | --- | --- | --- |
| 1（MxFP8，TND） | ? | ? | ? | ? |
| 6（GQA FP8，PA_BNBD） | ? | ? | ? | ? |

**答案**（依据 [quant_checker.cpp:351-354](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L351-L354)、[432-434](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L432-L434)、[499-501](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L499-L501)、[404-407](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L404-L407)、[467-473](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L467-L473)、[536-538](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L536-L538)）：

| quant_mode | q_descale | k_descale | v_descale | dtype |
| --- | --- | --- | --- | --- |
| 1 | 4D `(Q_T, Q_N, D/64, 2)` | 4D `(KV_T, KV_N, D/64, 2)` | 4D `(KV_T/64, KV_N, D, 2)` | FLOAT8_E8M0 |
| 6 | 2D `(N1, T)` | 3D `(Bn, N2, Bs)` | 1D `(N2)` | FLOAT32 |

粒度脚印：模式 1 的 `D/64`、`/64` 尾维是 pergroup（组大小 64、双 scale 冗余）；模式 6 的 `(N1, T)` 是 per-token-head、`(N2,)` 是 per-head。

**练习 2**：为什么模式 6（GQA FP8）强制要求 block_table 必选？

**答案**：见 [quant_checker.cpp:286-297](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L286-L297) 的 `CheckParaExistenceGqaFp8`：注释写明「GQA_FP8_FULLQUANT: block_table 必选（GQA 强制 PA 场景）」，对应 [quant_checker.cpp:469-472](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L469-L472) 中 kv layout 被限定为 PA_BNBD——该模式面向 KV Cache 分页管理的 decode 推理，没有 block_table 就无法定位 KV 页。

**练习 3**：`p_scale` 的 shape 约束是什么？它对应概念文档中的哪种量化粒度？

**答案**：见 [quant_checker.cpp:241-269](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L241-L269)：仅支持 FLOAT32、shape 必须是 `(1,)`——即 softmax 概率矩阵 P 的 per-tensor（T 量化）scale，与枚举名中的 `P_FP8_E4M3_PER_TENSOR` 呼应。

### 4.2 稀疏 attention（sparse_mode）

#### 4.2.1 概念说明

`sparse_mode` 是 attention 类算子共有的一个整型属性，描述 attention 掩码（attenMask）的结构化稀疏模式。它解决的问题是：causal（因果）、滑窗（band）、prefix、树形等场景下，\( QK^T \) 的大量位置注定被 mask 为 −∞，如果算子能**根据模式推导出哪些块整块无效**，就可以整块跳过，省下计算和访存——这就是「结构化稀疏」的收益，且数值结果与稠密计算完全一致（不算被 mask 的部分）。

其工作原理（见 [sparse_mode_introduction.md:20-26](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L20-L26)）：attenMask 为 True 的位置会遮蔽 \( QK^T \) 对应元素，即 softmax 前置 −∞：

\[ \mathrm{Attn}(Q,K,V)_i = \frac{\sum_j \mathrm{softmax}\!\left(\frac{Q_i K_j^T}{\sqrt{d}} + M_{ij}\right) V_j}{}, \quad M_{ij} = \begin{cases} -\infty & \text{mask}(i,j)=\text{True} \\ 0 & \text{否则} \end{cases} \]

10 种模式的速查表（完整含义见 [sparse_mode_introduction.md:7-18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L7-L18)）：

| sparse_mode | 含义 | 备注 |
| --- | --- | --- |
| 0 | defaultMask：不传 mask / causal / band 由 preTokens+nextTokens 组合表达 | 最通用 |
| 1 | allMask：传完整 mask 矩阵 | 无稀疏收益，仅接口统一 |
| 2 | leftUpCausal：左上顶点起的下三角 | 压缩三角矩阵 |
| 3 | rightDownCausal：右下顶点起的下三角 | 训练最常用 |
| 4 | band：滑窗（preTokens/nextTokens 交集） | 右下起点 |
| 5 | prefix 非压缩 | varlen 不支持 |
| 6 | prefix 压缩（下三角 + 矩形） | |
| 7 | varlen 外切 + rightDownCausal | 长序列多卡切分 |
| 8 | varlen 外切 + leftUpCausal | 长序列多卡切分 |
| 9 | treeMask：推测解码树形掩码 | 全量化场景仅支持 MLA |

注意区分两件事：**sparse_mode 是「怎么 mask」的描述**（结构稀疏，形状可预测）；4.3 节的 lightning_indexer 是「mask 哪些块由数据决定」的动态稀疏。二者可以叠加（indexer 选出的 indices 再交给稀疏 attention，配合 sparse_mode 表达因果性）。

#### 4.2.2 核心流程

一个带 sparse_mode 的 attention 调用流程：

```text
调用方
  ├─ 选择 sparse_mode（如训练 decoder 选 3）
  ├─ mode=2/3/4：传入压缩下三角矩阵（2048×2048 通用模板）
  ├─ mode=0：不传 mask，或用 preTokens/nextTokens 组合表达 causal/band
  └─ mode=5/6：额外传 prefix 输入
       ▼
算子 tiling 阶段
  ├─ 校验 sparse_mode 与其它参数的组合合法性
  └─ 依据模式推导「整块可跳过」的区域 → 影响 tiling 分块与循环边界
       ▼
kernel 阶段
  └─ 只对有效分块做 Q@K^T + softmax + PV，无效块直接跳过
```

稀疏收益的直观量化：causal 模式下约有 \(\tfrac{1}{2}\) 的 \( S_q \times S_{kv} \) 位置无效，band 滑窗模式下有效比例约为 \(\tfrac{w}{S_{kv}}\)（w 为窗宽）——序列越长，跳过比例越高。

#### 4.2.3 源码精读

**（1）模式总表与 mask 原理**。[sparse_mode_introduction.md:7-18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L7-L18) 是 10 种模式的权威定义；[sparse_mode_introduction.md:28-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L28-L61) 详细展开 mode=0 的四种子场景（无 mask / causal / band / 负数 preTokens 或 nextTokens 的偏置 band），说明 mode=0 是靠 `preTokens`（往前看几个 token）与 `nextTokens`（往后看几个 token）两个参数在运行时拼出掩码形状的。mode=7/8（外切场景，[sparse_mode_introduction.md:118-153](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L118-L153)）则解决长序列在多卡间切 query 后每张卡看到的 mask 不再是规整三角的问题——这是「稀疏模式与分布式训练交叉」的典型设计。

**（2）sparse_mode 落到算子属性**。lightning_indexer 的 def 中：

```cpp
this->Attr("sparse_count").AttrType(OPTIONAL).Int(2048); // 2048:默认值，筛选前2048
this->Attr("sparse_mode").AttrType(OPTIONAL).Int(3);       // 3:默认值，只计算下三角
```

见 [lightning_indexer_def.cpp:63-64](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L63-L64)：默认 sparse_mode=3（rightDownCausal），即筛选阶段就只在因果下三角内挑 Top-k。README 参数表也确认 sparse_mode 只支持 0 和 3 两个取值（[lightning_indexer README.md:168-181](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L168-L181)）。

**（3）tiling 阶段的合法性校验**。[lightning_indexer_tiling.cpp:317-327](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp#L317-L327)：

```cpp
OP_CHECK_IF((!((*opParamInfo_.sparseCount > 0) && (*opParamInfo_.sparseCount <= SPARSE_LIMIT)) &&
             *opParamInfo_.sparseCount % 1024 != 0) || (*opParamInfo_.sparseCount > 8192),
            ... "Sparse_count must > 0 and <= 8192."
            " And when sparse_count > 2048, sparse_count must be an interger multiple of 1024"),
            return ge::GRAPH_FAILED);
OP_CHECK_IF(!((*opParamInfo_.sparseMode == 0) || (*opParamInfo_.sparseMode == SPARSE_MODE_LOWER)),
            ... "Sparse_mode must be 0 or 3"), return ge::GRAPH_FAILED);
```

这段把 sparse_count 的取值范围（≤8192，>2048 时须为 1024 的倍数）和 sparse_mode 白名单（0 或 3）拦在 tiling 第一段。同一文件 [lightning_indexer_tiling.cpp:328-329](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp#L328-L329) 还强制 `pre_tokens` 只能取默认 INT64_MAX（即「全保留」，窗口语义交给 sparse_mode 表达）。

**（4）量化与稀疏的交叉约束**。mode=9（treeMask）的备注「非量化支持 GQA 和 MLA 场景，全量化仅支持 MLA」见 [sparse_mode_introduction.md:18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L18)——量化会改变 softmax 数值路径，与复杂 mask 的组合需要单独验证，因此能力面收窄。这是「量化 × 稀疏」两大机制在能力矩阵上互相制约的实例。

#### 4.2.4 代码实践

**实践目标**：建立「场景 → sparse_mode → 需要传什么 mask」的选型直觉。

**操作步骤**：

1. 通读 [sparse_mode_introduction.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md)，为每种模式画一个 4×6 的小矩阵草图，涂黑被遮蔽区域（文档中每节均附示意图链接）。
2. 在仓库中执行 `grep -rn "sparse_mode" attention/flash_attention_score/op_host --include=*.cpp | head`，观察 FA 主算子对 sparse_mode 的引用位置（本步骤只做定位，不逐行精读）。
3. 给出三个场景的选型：① 标准 decoder 层训练；② 滑动窗口为 4096 的长序列推理；③ 推测解码的树形注意力。

**需要观察的现象**：mode=2/3/4 共用「2048×2048 压缩下三角矩阵」这一约定（文档多处出现），说明 mask 输入是模板化的，模式差异体现在算子如何解读它。

**预期结果**：① 选 3（rightDownCausal）；② 选 0 配 preTokens/nextTokens，或 4（band）；③ 选 9（treeMask）。选型依据即文档各节定义，**待本地验证**（若在真实模型中对齐，还需结合框架侧传参）。

#### 4.2.5 小练习与答案

**练习 1**：mode=0 下不传 attenMask 时，preTokens/nextTokens 还生效吗？

**答案**：不生效。见 [sparse_mode_introduction.md:32-33](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L32-L33)：「如果 attenMask 未传入则不做 mask 操作……忽略 preTokens 和 nextTokens 取值」。preTokens/nextTokens 只在传了下三角或 band 形 mask 时用于界定计算窗口。

**练习 2**：mode=7（varlen 外切）为什么要求用户自己保证「外切前是 mode=3 场景」？

**答案**：见 [sparse_mode_introduction.md:118-134](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L118-L134)：长序列切 query 后，某张卡上最后（或第一）一块 mask 退化为 band 类型，需要用户按 band 规则配置 preTokens/nextTokens 并保证 `preTokens >= last_Skv`、`last_Sq-last_Skv <= nextTokens <= 0` 等约束，否则会出现精度问题——框架无法自动推断切分后的正确窗口参数，只能把约束交给调用方。

### 4.3 indexer 筛选（lightning_indexer 与 TopK 稀疏）

#### 4.3.1 概念说明

结构化稀疏（sparse_mode）只能利用「形状可预测」的掩码；而 NSA（Native Sparse Attention）一类稀疏训练架构的核心思想是：**哪些上下文块重要由数据本身决定**。做法是给模型加一组低维「indexer」投影（\( Q_{index}, K_{index} \)），先用廉价计算对全部上下文块打分，为每个 token 保留 Top-k 个最重要的 block，再只对这些 block 做精细 attention。这样注意力计算量从 \( O(S_q \cdot S_{kv}) \) 降到 \( O(S_q \cdot k) \)，k ≪ \( S_{kv} \)。

`lightning_indexer` 就是「打分 + TopK 筛选」这一步的融合算子。其计算公式（见 [lightning_indexer README.md:16-24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L16-L24)）：

\[ Indices=\text{Top-}k\left\{[1]_{1\times g}@\left[(W@[1]_{1\times S_{k}})\odot\text{ReLU}\left(Q_{index}@K_{index}^T\right)\right]\right\} \]

直觉解读：\( Q_{index}@K_{index}^T \) 对上下文每个位置打分，ReLU 截断负分；\( W \)（形状 \( g\times 1 \)）把 GQA 同组 \( g \) 个 query 头的分数加权合成组级分数；最后 Top-k 选出每个 token 最重要的 k 个位置，输出 **INT32 索引** `sparse_indices`（以及可选的分数值 `sparse_values`）。下游的 `sparse_flash_attention` / `sparse_flash_mla` 等算子拿这些索引做真正的稀疏 attention。

围绕它形成了完整家族（`ls attention/` 可见）：`lightning_indexer_v2`（新版本）、`quant_lightning_indexer(_v2)`（FP8 量化版）、`*_kl_loss*`（indexer 分数的 KL 蒸馏损失，训练用）、`*_metadata`（辅助元数据算子）、`*_grad`（反向）。训练一条线（indexer + kl_loss + grad）、推理一条线（quant + metadata）都齐了。

#### 4.3.2 核心流程

一次稀疏训练前向中 indexer 相关算子链：

```text
Q/K（高精度或已量化）
    │ lightning_indexer（本讲主角）
    │   Q_index@K_index^T → ReLU → W 组内加权 → Top-k（sparse_count）
    ▼
sparse_indices [B, Q_S, 1, sparse_count]（INT32，每 token 的 Top-k 位置）
sparse_values（可选，对应分数）
    │ sparse_flash_attention / sparse_flash_mla（消费 indices 的稀疏 attention）
    │   只对 indices 指定的 block 做 Q@K^T + softmax + PV
    ▼
attn_out
    │（训练时）dense/sparse_lightning_indexer_kl_loss
    │   稀疏分数与稠密分数的 KL 蒸馏，保证 indexer 学得准
    ▼
loss → lightning_indexer_grad 反传更新 indexer 投影
```

收益模型：设上下文长 \( S_{kv} \)、保留 k 个块，稀疏 attention 的计算量约为稠密的 \( k / S_{kv} \)；indexer 打分本身只有一次低维（\( d=128 \)、单 KV 头）矩阵乘，远小于多头全量 attention，因此整体是净赚的。

#### 4.3.3 源码精读

**（1）TopK 公式与参数**。[lightning_indexer README.md:16-24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L16-L24) 给出上述计算公式与各符号含义（g 为 GQA 组大小、d=128、\( S_k \) 为上下文长度）；[README.md:156-167](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L156-L167) 定义 sparse_count：topK 阶段保留的 block 数量，支持 [1, 2048] 连续取值以及 3072/4096/5120/6144/7168/8192 这些档位，默认 2048。

**（2）算子原型的输入输出**。[lightning_indexer_def.cpp:23-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L23-L60) 注册了 query/key/weights 三个必选输入（BF16/FP16，weights 额外支持 FP32）、actual_seq_lengths_query/key 与 block_table 三个可选输入（varlen 与 PagedAttention 衔接，注意 layout_key 支持 `PA_BSND`——indexer 可以直接从分页 KV Cache 读索引 key），以及两个输出：

```cpp
this->Output("sparse_indices")
    .ParamType(REQUIRED)
    .DataTypeList({ge::DT_INT32})
    ...
this->Output("sparse_values")
    .ParamType(REQUIRED)
    .DataType({ge::DT_BF16, ge::DT_FLOAT16, ...})
```

见 [lightning_indexer_def.cpp:53-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L53-L60)：`sparse_indices` 是 INT32 索引张量，BSND layout 下 shape 为 `[B, Q_S, K_N, sparse_count]`（K_N 仅支持 1，见 README 约束）。属性区（[lightning_indexer_def.cpp:61-67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L61-L67)）包含 layout_query/layout_key/sparse_count/sparse_mode/pre_tokens/next_tokens/return_values 七个属性；SoC 注册覆盖 ascend910b、ascend910_93 与 ascend950（[lightning_indexer_def.cpp:75-90](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L75-L90)，其中 950 上 key 允许 0 轴非连续）。

**（3）tiling 对「筛选数量」的强校验**。[lightning_indexer_tiling.cpp:974-988](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp#L974-L988) 校验输出张量最后一维必须与 sparse_count 相等——`The last dim of sparse_indices and sparse_count are ... they must be same`。这体现了 TopK 算子的接口特征：**输出形状由属性（保留几个）而非输入形状决定**，调用方必须按 sparse_count 预分配输出。[lightning_indexer_tiling.cpp:317-323](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp#L317-L323)（4.2.3 节已引用）则限定了 sparse_count 的取值集合：>0 且 ≤8192，超过 2048 时必须是 1024 的整数倍——这类「离散档位」约束通常源于 kernel 内 TopK 归并实现的分块宽度。

**（4）与量化体系的会师**。`attention/` 下同时存在 `quant_lightning_indexer` 与 `quant_sparse_flash_mla`，即 4.1 节的量化模式与 4.3 节的 TopK 稀疏在产品线上是可组合的：indexer 侧量化降低打分开销，attention 侧量化降低主计算开销。sparse_mode=9 的「全量化仅支持 MLA」备注（4.2.3 节）也说明这条组合线的能力边界仍在演进中。

#### 4.3.4 代码实践

**实践目标**：读懂 lightning_indexer 的「输入 → TopK → 输出」形状契约，为综合实践的数据流图做准备。

**操作步骤**：

1. 读 [lightning_indexer README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md) 的功能说明与参数表，记下 query/key/weights 与 sparse_indices 的 shape 公式。
2. 读 [lightning_indexer_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp)，对照属性默认值（sparse_count=2048、sparse_mode=3）。
3. 假设 B=2、Q_S=1024、K_S=8192、Q_N=32、K_N=1、D=128、GQA 组大小 g=4、sparse_count=2048，手算：weights 的 shape、sparse_indices 的 shape、以及稀疏 attention 相对稠密 attention 的计算量比例。

**需要观察的现象**：sparse_indices 最后一维就是 sparse_count；K_N 恒为 1（indexer 是「组级」打分，不区分 KV 头）。

**预期结果**：weights 为 `[B, Q_S, Q_N]` = [2, 1024, 32]；sparse_indices 为 `[2, 1024, 1, 2048]`；计算量比例约 2048/8192 = 25%。本实践为纸面推导，**待本地验证**（如需实跑，可在 NPU 环境参照 `lightning_indexer/examples/test_aclnn_lightning_indexer.cpp` 构造输入）。

#### 4.3.5 小练习与答案

**练习 1**：`return_values` 属性控制什么？什么场景下必须打开？

**答案**：见 [lightning_indexer_def.cpp:67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_def.cpp#L67) 与 [README.md:196-207](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L196-L207)：控制是否输出 `sparse_values`（Top-k 对应的分数值），默认 False，仅在训练且 layout_key 不为 PA_BSND 时支持——训练时 KL 蒸馏损失（`*_kl_loss` 系列）需要这些分数。

**练习 2**：为什么 lightning_indexer 的 key 输入支持 `PA_BSND`（分页）layout，而 query 不支持？

**答案**：稀疏筛选针对的是**上下文**（KV 侧）——推理时上下文存放在分页 KV Cache 中，indexer 必须能直接按 block_table 读页才能与 PagedAttention 推理链路衔接；而 query 是当前步新产生的数据，自然连续存放，无需分页。见 [README.md:66-68](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L66-L68) 对 layout_key 三种取值及 block_table 参数的说明。

**练习 3**：`lightning_indexer` 与 `sparse_flash_attention` 的输出/输入是如何咬合的？

**答案**：lightning_indexer 输出 INT32 的 `sparse_indices`（每 token 的 Top-k 位置），sparse_flash_attention 以这类索引为输入，仅对索引指向的上下文块执行注意力计算；训练时再由 `*_kl_loss` 算子对 indexer 分数做蒸馏约束。三者的 shape 契约都由各自的 tiling 校验把关（如 [lightning_indexer_tiling.cpp:974-988](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/op_host/lightning_indexer_tiling.cpp#L974-L988) 的最后一维 = sparse_count 校验）。

## 5. 综合实践

**任务**：绘制一幅「量化 dense attention」与「TopK 稀疏 attention」双管线数据流示意图，并标注每一步使用的算子与关键 shape。

**要求**：

1. **管线 A（全量化 dense）**：高精度 q/k/v →（量化，标注粒度）→ `quant_flash_attn`（标注 quant_mode=1 与 6 两条支路的 descale 形状差异，引用 [quant_checker.cpp:346-396](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/quant_flash_attn/op_host/checkers/quant_checker.cpp#L346-L396) 的期望 shape）→ BF16 attn_out；如需稀疏掩码，标注 sparse_mode 取值（参考 [sparse_mode_introduction.md:7-18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/sparse_mode_introduction.md#L7-L18)，注意 mode=9 在全量化下仅支持 MLA）。
2. **管线 B（TopK 稀疏）**：q/k → `lightning_indexer`（标注公式三步：Q@K^T 打分 → ReLU + W 组内加权 → Top-k，引用 [README.md:16-24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/lightning_indexer/README.md#L16-L24)）→ `sparse_indices [B, Q_S, 1, sparse_count]` → `sparse_flash_attention`（或 `sparse_flash_mla`）→ attn_out；训练分支补 `*_kl_loss` 与 `*_grad`。
3. 在图上用虚线标出两线可组合的位置（`quant_lightning_indexer` / `quant_sparse_flash_mla`），并用一句话说明组合的收益。
4. 为图中每个「量化相关分支」注明对应的源码文件与函数名（如 q_descale shape 校验 → `QuantChecker::CheckQDescaleShapeMxFp8`）。

**验收标准**：图完成后，随机遮住某个 descale 张量，你能凭量化粒度推出它的 shape；随机指一个 sparse_mode 取值，你能说出它需要调用方传什么 mask、忽略什么参数。绘图工具不限（纸笔、mermaid、draw.io 均可）；源码引用以本讲给出的永久链接为准。

## 6. 本讲小结

- 量化粒度（pertensor/perchannel/pertoken/pergroup/perblock）决定 scale 张量的形状；「全量化」= 左右矩阵都量化，MX 量化 = E8M0 scale 的 pergroup 特例。
- 量化模式直接塑造算子接口：`quant_flash_attn` 用必选属性 `quant_compute_mode`（仅 1=MxFP8、6=GQA FP8）分派，q/k/v_descale 的 dtype、shape、layout 全部按模式查表校验（`quant_checker.cpp`），通用比较逻辑抽象在 `qfa_tiling_shape.cpp` 的 layout→轴序列映射中。
- `sparse_mode`（0~9）描述结构化掩码稀疏：算子按模式跳过整块无效区域，数值与稠密计算等价；mode=7/8 支撑长序列多卡外切，mode=9 服务推测解码，且量化与稀疏的能力面存在交叉约束。
- `lightning_indexer` 是动态 TopK 稀疏的筛选算子：\( \text{Top-}k\{ W \odot \text{ReLU}(Q_{index}K_{index}^T) \} \)，输出 INT32 索引交给稀疏 attention 消费；其家族覆盖 v2、量化版、KL 蒸馏损失与反向，构成完整的稀疏训练/推理管线。
- 稀疏（少算）与量化（低精度算）是两条正交的加速轴，在本仓库中以 `quant_sparse_flash_mla`、`quant_lightning_indexer` 等算子实现会师。

## 7. 下一步学习建议

本讲完成了 attention 模块（第四单元）的学习。建议后续：

1. **横向进入 MoE/FFN/mc2 模块**（第五单元，u5-l1 起）： attention 之外的三条大模型业务线，重点看 `moe_token_permute` 这类「无 op_api 图模式算子」与 attention 算子在组织方式上的差异。
2. **深入稀疏家族源码**：本讲只读了 lightning_indexer 的 host 侧；可继续精读 `attention/sparse_flash_attention` 的 op_host，看稀疏 attention 如何用 sparse_indices 驱动 kernel 的分块循环（device 侧）。
3. **对照量化算子的 torch 侧封装**：结合 u3-l3 的 torch_extension 知识，看 `torch_extension/cann_ops_transformer/ops.py` 中量化/稀疏算子的 Python 入口如何组织 descale 等参数。
4. **回看能力矩阵**：重读 `docs/zh/op_list.md`，用本讲概念检验「哪些 attention 算子支持 FP8、哪些支持稀疏」，训练自己从产品支持表反推源码结构的能力。
