# MoE 模块：路由与 token 重排算子链路

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一轮 MoE（Mixture of Experts，混合专家）前向中各算子的调用顺序与每一步张量的 shape 来源。
2. 精读 `moe_init_routing` 与 `moe_token_permute` 的 def 文件，列出它们的输入、输出、属性与 dtype 白名单。
3. 理解 `moe_token_permute` 这类「重排型」算子的 tiling 为什么围绕**排序**和**按索引搬运**两个核心问题展开。
4. 掌握本仓库 MoE 算子的一种特殊组织方式：aclnn 实现放在 `op_host/op_api/` 子目录、没有顶层 `op_api/`，以及在 Ascend 950 上「转发调用其他算子」的版本演进手法。

## 2. 前置知识

本讲默认你已读过 u2-l2（op_host 三件套：def / infershape / tiling）和 u4-l1（Flash Attention 家族概览）。在此基础上补充三个 MoE 领域的基础概念：

- **MoE（混合专家）**：把 transformer 的 FFN 层替换为 N 个「专家」网络，每个 token 只送入其中少数几个专家计算。这样模型总参数量可以很大，但每个 token 的实际计算量很小。
- **路由（Routing / Gating）**：一个小的打分网络（gating）为每个 token 输出它对全部专家的偏好分数，再选出 Top-K 个专家。本仓库中这一步由 `moe_gating_top_k_softmax` 等算子完成，产出「每个 token 由哪 K 个专家处理」的索引。
- **重排（Permute）与恢复（Unpermute）**：专家计算通常是批量矩阵乘，希望**同一个专家的 token 在内存中连续排列**。因此要先按专家 ID 把 token 重排（permute），专家计算完成后再按原顺序恢复并按路由概率加权求和（unpermute）。这一对操作是 MoE 前向中额外的、稠密模型没有的数据搬运开销，也是本讲的主角。

一个直觉类比：permute/unpermute 就像「分组排队」——全班同学（token）按选修课（专家）重新排队去上课，上完课再按原学号坐回座位并汇总成绩。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [moe/moe_init_routing/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/README.md) | MoeInitRouting 算子的功能说明、计算公式与参数表 |
| [moe/moe_init_routing/op_host/moe_init_routing_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_host/moe_init_routing_def.cpp) | MoeInitRouting 的算子原型定义（输入/输出/属性注册） |
| [moe/moe_token_permute/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/README.md) | MoeTokenPermute 的功能说明、计算公式、约束与版本转发关系 |
| [moe/moe_token_permute/op_host/moe_token_permute_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_def.cpp) | MoeTokenPermute 的算子原型定义 |
| [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp) | MoeTokenPermute 的 tiling 切分策略（排序 + 索引搬运） |
| [moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp) | aclnnMoeTokenPermuteV2 的两段式接口实现，含跨算子转发 |
| [moe/moe_token_unpermute/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_unpermute/README.md) | MoeTokenUnpermute 的功能说明与计算公式 |

另外，`moe/` 目录下还有 30 余个 MoE 算子（`moe_gating_top_k_softmax`、`moe_init_routing_v2/v3/v4`、`moe_token_permute_grad`、`moe_token_permute_with_ep` 等），本讲只精读链路主干，其余在 5.x 各讲与 u5-l4（分布式 MoE）中展开。

## 4. 核心概念与源码讲解

### 4.1 MoE 一轮前向的算子链路

#### 4.1.1 概念说明

一次 MoE 前向（以 Top-K 路由为例）的数据流是：

```text
hidden_states x            shape: [numTokens, hidden]        ← 上一层输出
      │
      ▼
[1] gating 打分 + TopK      (moe_gating_top_k_softmax 等)
      │   输出 expertIdx     shape: [numTokens, topK]  (INT32)
      │   输出 probs         shape: [numTokens, topK]  (FLOAT)
      ▼
[2] init_routing / token_permute   (本讲主角)
      │   按 expertIdx 排序，把 token 扩散成 numTokens*topK 行
      │   输出 permuteTokens  shape: [numTokens*topK, hidden]
      │   输出 sortedIndices  shape: [numTokens*topK]     (INT32)
      ▼
[3] 专家计算（分组矩阵乘 / ffn）
      │   同一专家的行连续排列，可整体做 GEMM
      ▼
[4] token_unpermute
      │   按 sortedIndices 恢复原始顺序，乘 probs 并按 topK 求和
      │   输出 out            shape: [numTokens, hidden]
      ▼
回到主干网络
```

三个关键观察：

1. **shape 的膨胀与收缩**：链路中 token 数先从 `numTokens` 膨胀为 `numTokens * topK`（一个 token 复制给 K 个专家），最后又收缩回 `numTokens`（加权求和）。
2. **permute 与 unpermute 靠同一张「地图」对齐**：permute 输出的 `sortedIndices` 记录了「重排后的第 i 行来自哪个原始位置」，unpermute 消费这份地图完成逆操作。
3. **链路算子都不做矩阵乘**：本讲的算子全部是「排序 + 按索引搬运数据」的访存型算子，这与 u4 精读的 attention（计算型）形成对照——它们的性能瓶颈在搬运与排序，而不是浮点计算。

#### 4.1.2 核心流程

以 `numTokens = 4`、`topK = 2`、`hidden = H` 为例走一遍 permute 的语义（对应下文 4.3 的公式）：

```text
tokens: 4 行                indices: 4x2
  t0                         [e2, e0]      ← t0 由专家2、专家0处理
  t1                         [e2, e1]
  t2                         [e1, e0]
  t3                         [e0, e2]

把 indices 展平为 8 个 (expertId, 序号)：e2,e0,e2,e1,e1,e0,e0,e2
按 expertId 稳定排序（argSort）得到 sortedIndicesFirst
同一专家的行聚在一起 → permuteTokens: 8 行（每行 = tokens[i // topK] 的拷贝）
sortedIndices 记录逆映射，供 unpermute 恢复
```

#### 4.1.3 源码精读

链路中每一步的「官方定义」写在两个算子的 README 里。

MoeInitRouting 的定位是「承接 gating 结果做路由处理」，README 给出的计算公式为：

- [moe/moe_init_routing/README.md:L16-L29](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/README.md#L16-L29)：算子功能描述——「根据 aclnnMoeGatingTopKSoftmax 的计算结果做 routing 处理」，并给出三步公式：对 `(expertIdx, rowIdx)` 做键值排序、由排序结果生成逆映射 `expandedRowIdx`、按 `rowIdx % numRows` 扩散特征 `expandedX`。这正对应上面流程图中的「膨胀 + 分组」。

MoeTokenUnpermute 的公式则在恢复端完成「加权求和收缩」：

- [moe/moe_token_unpermute/README.md:L18-L32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_unpermute/README.md#L18-L32)：`T[k] = T[S[k]]`（按地图取回）、`T[k] = T[k] * P[i][j]`（乘路由概率）、\( O[i] = \sum_{k=i \cdot topK}^{(i+1) \cdot topK - 1} T[k] \)（每个 token 的 K 份结果求和）。

#### 4.1.4 代码实践

**实践目标**：不看答案，先把链路 shape 表填出来，再用源码验证。

**操作步骤**：

1. 假设 `numTokens = 4096`、`topK = 8`、`hidden = 4096`、`numExperts = 128`。
2. 在纸上写出流程图中每个中间张量的 shape 与 dtype。
3. 对照 [moe/moe_init_routing/README.md:L33-L94](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/README.md#L33-L94) 的参数表（x、rowIdx、expertIdx、activeNum、expandedXOut、expandedRowIdxOut、expandedExpertIdxOut）检查你的答案。

**需要观察的现象**：`expandedXOut` 的行数是 `numTokens * topK = 32768`，而不是 `numTokens`；`activeNum` 属性表示「最多处理多少行，超出部分无效」——它给了推理框架一个限制实际计算量的旋钮。

**预期结果**：三个输出的行数一致（均为活跃的 expanded 行数），`expandedRowIdxOut` 与 `expandedExpertIdxOut` 都是 INT32。

#### 4.1.5 小练习与答案

**练习 1**：如果 `topK = 1`，permute/unpermute 还必要吗？

**答案**：仍然需要「按专家分组」这一步（否则无法做批量 GEMM），但可以退化为一次按 `expertId` 的稳定排序，且 unpermute 不再需要求和（topK=1 时求和退化为一行），`moe_token_unpermute/README.md` 中 probs 为 None 的分支描述的正是这种情况。

**练习 2**：`sortedIndices` 为什么必须由 permute 输出、由 unpermute 消费，而不能由 unpermute 自己重新算一遍？

**答案**：因为排序必须是**稳定**的且两次实现要严格互逆；自己重算要求重放完全相同的排序逻辑（包括并列专家 ID 时的次序），既浪费一次全量排序，又引入两边实现不一致的风险。把它作为 permute 的输出、unpermute 的输入，是「一次排序、两处使用」的合同式设计。

### 4.2 moe_init_routing：路由的起点

#### 4.2.1 概念说明

`MoeInitRouting` 是「老一代」路由算子：输入已经是 gating 算子的产物（每个 token 的 Top-K 专家索引），它负责把 token 按 expertId 排序、扩散并生成三份输出。它在 def 文件层面展示了两个 u2-l2 未见过的新东西：

- **同一算子在不同 SoC 上挂接不同 kernel 实现**（`opFile.value` 有两个取值）。
- **动态 shape 能力开关**（`DynamicCompileStaticFlag` / `DynamicRankSupportFlag` / `DynamicShapeSupportFlag`）。

#### 4.2.2 核心流程

def 文件的注册流程（承接 u2-l2 的「静态户口」比喻）：

```text
Input(x, row_idx, expert_idx)  →  dtype/format 白名单 + AutoContiguous
Output(expanded_x, expanded_row_idx, expanded_expert_idx)
Attr(active_num)               →  必填 Int
为 ascend910b / ascend910_93 注册 membaseCfg（opFile = moe_init_routing）
为 ascend950       注册 regbaseCfg（opFile = moe_init_routing_apt）
OP_ADD(MoeInitRouting)         →  注入算子信息库
```

#### 4.2.3 源码精读

- [moe/moe_init_routing/op_host/moe_init_routing_def.cpp:L23-L40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_host/moe_init_routing_def.cpp#L23-L40)：注册三个输入。`x` 是 token 特征（FLOAT16/BF16/FLOAT），`row_idx` 指示每个位置对应的原始行（INT32），`expert_idx` 是 gating 算子的输出（INT32）。注意 dtype 列表是「按变体排列」的：三个条目对应 x 的三种 dtype，而 row_idx/expert_idx 恒为 INT32——这与 add_example 中「dtype 列表与 tiling key 一一对应」是同一机制。
- [moe/moe_init_routing/op_host/moe_init_routing_def.cpp:L41-L56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_host/moe_init_routing_def.cpp#L41-L56)：注册三个输出（expanded_x 与 x 同 dtype；两个索引输出恒为 INT32）和必填属性 `active_num`。
- [moe/moe_init_routing/op_host/moe_init_routing_def.cpp:L58-L71](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_host/moe_init_routing_def.cpp#L58-L71)：本讲最值得注意的一段——构造了两个 `OpAICoreConfig`：`membaseCfg` 通过 `ExtendCfgInfo("opFile.value", "moe_init_routing")` 把 host 与 kernel 入口连接起来，注册到 ascend910b/ascend910_93；`regbaseCfg` 挂接另一个 kernel 文件 `moe_init_routing_apt`，注册到 ascend950。同时三个 `DynamicXXXFlag` 打开了动态 shape/动态 rank 支持。**同一个算子名，两代硬件各用一套 kernel 实现**，这是 u1-l4 讲过的 arch 目录隔离在 def 层的另一种表达方式（通过 ExtendCfgInfo 而非 arch 子目录）。

顺带一个工程事实：本仓库中搜索不到 `aclnnMoeInitRouting`（V1）两段式入口的实现源码（v2/v3/v4 的实现在 `moe_init_routing_v2/v3/v4/op_host/op_api/` 下），V1 的 aclnn 符号可能由 CANN 基础软件包提供——**待确认**。这也提醒我们：README 的「调用说明」代表产品交付能力，与仓库内源码范围并不总是一一对应（u2-l1 已见过同样结论）。

#### 4.2.4 代码实践

**实践目标**：读懂「dtype 变体列表」在 def 中的排列规则。

**操作步骤**：

1. 打开 def 文件 L23-L28，数一数 `x` 的 DataType 列表长度（3 个）与 Format 列表长度（3 个）。
2. 再看 L29-L34 的 `row_idx`：DataType 列表也是 3 个，但三个值都是 `ge::DT_INT32`。
3. 回答：为什么 `row_idx` 明明只有一种 dtype，也要写三遍？

**需要观察的现象 / 预期结果**：框架按「列表下标」匹配同一组变体：第 i 个变体 = (x 的第 i 个 dtype, row_idx 的第 i 个 dtype, …)。`row_idx` 写三遍是为了占住三个下标位置，使所有输入/输出的变体列表等长。这是 def 文件的一个机械但必须遵守的书写约定（待本地验证：可尝试删去两个条目后用 `bash build.sh --ophost --ops=moe_init_routing` 编译，预期在注册或校验阶段报错）。

#### 4.2.5 小练习与答案

**练习 1**：`active_num` 属性和 4.3 将见到的 `num_out_tokens` 属性语义上有何联系？

**答案**：两者都是「截断输出规模」的旋钮：`active_num` 限制 expanded 后有效行数，`num_out_tokens` 限制 permute 输出的 token 数。事实上在 Ascend 950 的转发路径上，`numOutTokens` 直接映射为 `aclnnMoeInitRoutingV2` 的 `activeNum`（见 4.5.3），说明两代接口本质在做同一件事。

**练习 2**：为什么 `expert_idx` 的取值范围被限制在 ±2^24（README 约束说明）？

**答案**：排序 kernel 内部把 expertId 与 rowIdx 打包成单个键值对参与键值排序，INT32 键中留给 expertId 的有效位约 24 bit，超出会侵入 rowIdx 的位段造成排序错乱（README 原文说「否则可能会存在精度问题」）。这是「数值约束源于 kernel 内部编码方式」的典型例子。

### 4.3 moe_token_permute / unpermute：重排与恢复

#### 4.3.1 概念说明

`MoeTokenPermute` 是新一代重排算子：输入直接是 gating 产出的 `indices`（不需要单独的 row_idx），输出「按专家分组后的 token」与「逆映射索引」。它与 `MoeTokenUnpermute` 成对使用，构成 MoE 前向中「排队—上课—回座」的完整循环。相比 `MoeInitRouting`，它的接口更精简（2 输入 2 输出），但 dtype 支持更宽（含 INT8 量化 token）。

#### 4.3.2 核心流程

paddedMode 为 `false`（目前唯一支持的模式）时的语义：

\[ sortedIndicesFirst = argSort(indices) \]
\[ sortedIndicesOut = argSort(sortedIndicesFirst) \]
\[ permuteTokens[sortedIndicesFirst[i]] = tokens[i // topK] \]

翻译成一句话：先对展平后的 indices 做一次稳定排序得到正向映射，再做一次 argSort 得到逆向映射（sortedIndicesOut），然后按正向映射把每个 token 的行拷贝到目标位置。注意 `tokens[i // topK]`——展平后第 i 个 slot 属于第 `i // topK` 个 token，这就是「一个 token 复制 K 份」的数学表达。

#### 4.3.3 源码精读

- [moe/moe_token_permute/op_host/moe_token_permute_def.cpp:L23-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_def.cpp#L23-L36)：注册输入 `tokens`（BF16/FLOAT16/FLOAT/INT8）与 `indices`（INT32 或 INT64——注意变体列表两两一组：BF16+INT64、BF16+INT32、FP16+INT64……）。两个输入都开了 `AutoContiguous()`。
- [moe/moe_token_permute/op_host/moe_token_permute_def.cpp:L37-L48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_def.cpp#L37-L48)：注册输出 `permute_tokens`（与 tokens 同 dtype）和 `sorted_indices`（恒为 INT32）。
- [moe/moe_token_permute/op_host/moe_token_permute_def.cpp:L49-L52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_def.cpp#L49-L52)：两个可选属性——`num_out_tokens`（默认 0，即不丢弃任何 token）和 `padded_mode`（默认 false）。然后 `AddConfig` 只注册了 ascend910b 和 ascend910_93，**没有 ascend950**——这不是疏漏：950 上该算子根本不跑自己的 kernel，而是转发到 `MoeInitRoutingV2/V3`（见 4.5），所以无需在本算子注册 950 的 AICore 配置。
- [moe/moe_token_permute/README.md:L106-L114](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/README.md#L106-L114)：README 的约束说明明确写出 Ascend 950 上的参数映射关系（token→x、indices→expertIdx、numOutTokens→activeNum……），是「接口兼容层」的官方文档表达。

恢复端 `MoeTokenUnpermute` 的输入输出在 [moe/moe_token_unpermute/README.md:L18-L32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_unpermute/README.md#L18-L32)：消费 `permutedTokens` 与 `sortedIndices`（即 permute 的两个输出），可选输入 `probs`，输出 `[numTokens, hidden]`。

#### 4.3.4 代码实践

**实践目标**（本讲综合实践的预热）：列出两个 def 文件的输入输出张量清单。

**操作步骤**：

1. 打开 [moe_token_permute_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_def.cpp) 与 [moe_init_routing_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_host/moe_init_routing_def.cpp)。
2. 用 `grep -n 'this->Input\|this->Output\|this->Attr'` 分别提取两张清单。
3. 填写下表（答案在 4.3.5）：

| 算子 | 输入 | 输出 | 属性 |
| --- | --- | --- | --- |

**需要观察的现象**：两个算子的「核心三件套」——特征张量、专家索引、逆映射索引——名字不同但语义同构。

**预期结果**：MoeInitRouting 有 row_idx 显式输入，MoeTokenPermute 没有（隐含为 `i // topK`），这正是新一代接口更简洁的原因。

#### 4.3.5 小练习与答案

**练习 1**：完成 4.3.4 的表格。

**答案**：

| 算子 | 输入 | 输出 | 属性 |
| --- | --- | --- | --- |
| MoeInitRouting | x、row_idx、expert_idx | expanded_x、expanded_row_idx、expanded_expert_idx | active_num（必填） |
| MoeTokenPermute | tokens、indices | permute_tokens、sorted_indices | num_out_tokens（可选，默认 0）、padded_mode（可选，默认 false） |

**练习 2**：`permute_tokens` 的第 0 维大小由什么决定？

**答案**：由 tiling 阶段计算——展平后的 indices 元素总数 `totalLength = n * topK`（n 为 tokens 行数），再经 `num_out_tokens` 截断：`numOutTokens = min(max(numOutTokens, 0), totalLength)`，且默认（≤0）时等于 `totalLength`。def 层只声明 dtype，shape 属于 infershape/tiling 的职责（见 4.4.3 的 `CheckOutShape`）。

### 4.4 moe_token_permute 的 tiling 精读

#### 4.4.1 概念说明

u2-l2 见过的 add_example tiling 是「教学级」：参数写死、单核循环。`MoeTokenPermuteTilingBase` 则展示了工业级 tiling 的完整形态。它解决两个子问题：

1. **排序怎么切**：对 `totalLength` 个 (key, value) 排序，元素多时要用多核归并排序（VBS = 各核局部排序，VMS = 核间归并，MergeSort Out = 最终输出归并）。
2. **数据怎么搬**：排完序后按索引把 token 行从 GM 搬到 GM（IndexCopy），受 UB 容量约束决定一次搬多少个 token。

同时它示范了 `TilingBaseClass` 这个公共基类的七步 tiling 模板——比 add_example 的裸函数式 tiling 更工程化。

#### 4.4.2 核心流程

七步 tiling 模板（注释即源码注释）：

```text
GetPlatformInfo  → 取 AIV 核数、UB 大小
GetShapeAttrsInfo → 取输入 shape、属性，算出 n/topK/cols/totalLength，校验输出 shape
DoOpTiling       → 四个子 tiling：
                   Tiling4VBSCompute      各核局部排序的切分
                   Tiling4VMSMiddleCompute 核间中间归并
                   Tiling4SortOutCompute  最终归并输出
                   Tiling4IndexCopyCompute 按索引搬运 token 的切分
DoLibApiTiling   → （本算子未用高阶 API，直接返回）
GetTilingKey     → 单核排序=1 / 多核排序=2，可叠加 +2（切D模式）、+4（numOutTokens 截断）
GetWorkspaceSize → 排序空间 + 多核同步空间 + 固定余量
PostTiling       → SetBlockDim、保存 tiling data、设置 schedule_mode=1
```

多核排序的切分策略有一个漂亮的约束：归并排序的归并树是 4 叉的，所以参与排序的核数被规整为 4 的幂：

\[ needCoreNum = 4^{\lceil \log_4 \lceil totalLength / sortLoopMaxElement \rceil \rceil} \]

其中 `sortLoopMaxElement` 由 UB 大小反推：每核一次能装进 UB 参与排序的元素数。

#### 4.4.3 源码精读

- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L151-L169](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L151-L169)：`MoeTokenPermuteTilingBase` 继承 `Ops::Transformer::OpTiling::TilingBaseClass`（common 库提供的基类，呼应 u3-l2），覆写七个步骤方法。这是工业算子的推荐写法：基类统一编排调用顺序，子类只填业务逻辑。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L203-L227](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L203-L227)：`GetPlatformInfo`——取平台 AIV 核数与 UB 大小。注意一个「小输入快速路径」：当 indices 元素数 ≤ 32 时强制单核（`aivNum = 1`），免得多核排序的同步开销反而更慢。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L279-L351](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L279-L351)：`GetShapeAttrsInfo`——从输入 shape 推出 `n`（tokens 行数）、`topK`（indices 第 1 维，1D 时为 1）、`cols`（hidden 维乘积）、`totalLength = n * topK`；校验 topK ≤ 512、totalLength < 16777215（即 2^24-1，与 4.2.5 练习 2 的键编码约束同源）；末尾调用 `CheckOutShape` 用这些值反向校验调用方传入的输出 shape（输出第 0 维必须等于 numOutTokens 等）。`numOutTokens` 的截断逻辑也在这里：`(numOutTokens <= 0) ? numOutTokens + totalLength : numOutTokens`，再 clamp 到 `[0, totalLength]`。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L516-L531](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L516-L531)：`Tiling4VBSCompute`——按 `totalLength <= sortLoopMaxElement` 决定 tiling key：单核模式（1）或多核模式（2）。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L478-L493](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L478-L493)：多核切分核心——`needCoreNum` 先按每核容量向上取整，再规整到 4 的幂（归并树 4 叉），再与物理核数取 min；随后对齐到 32 元素边界做负载均衡修正。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L550-L602](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L550-L602)：`Tiling4IndexCopyCompute`——搬运切分。先由 UB 容量估算「一次能装多少个 token 行」（`onceUbTokenNums`，保留 BUFFER_NUM=2 双缓冲，呼应 u2-l3）；当单个 token 行太大（UB 装不下两个缓冲的整行）时切换到「切 D 模式」：一行拆成多次搬（`oneTokenMoveTimes`），并给 tiling key 叠加 `SPILT_D_MODE`（+2）。这就是 u2-l2 说的「tiling key 按 dtype 分支」的推广：这里按**执行形态**分支，kernel 侧据 key 选择不同的处理模板。
- [moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp:L440-L462](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/moe_token_permute_tiling.cpp#L440-L462)：workspace 计算（排序空间 `totalLength * 6 * sizeof(float)` + 多核同步空间 + 16MB 固定余量）与 `PostTiling`。注意第 459 行注释「涉及核间同步的算子必须设置 schedule_mode 为 1，独占全核」——归并排序需要核间同步，必须独占 AIV，这是多核排序算子的重要工程约束。

#### 4.4.4 代码实践

**实践目标**：亲手算一遍 tiling 关键量，理解「UB 大小如何决定切分」。

**操作步骤**：

1. 设 `n = 4096`（token 数）、`topK = 8`、`cols = 4096`、token dtype 为 BF16（2 字节）、平台 UB = 196608 字节（A2/B2 典型值，待以你本机 `aclrtGetPlatformInfo` 结果为准）。
2. 计算 `totalLength`、`oneTokenBtypeSize`、`oneTokenBtypeSizeAlign32`（32 字节对齐后）。
3. 判断 UB 减去 `MAX_INDICES_NUM * 4`（512×4=2048 字节）后能否装下 `2 × oneTokenBtypeSizeAlign32`，据此判断走「整行模式」还是「切 D 模式」。
4. 对照 L568-L602 的公式算出 `onceUbTokenNums`。

**需要观察的现象**：`cols = 4096`、BF16 时一行 8192 字节，远超 UB 的百分之一，`onceUbTokenNums` 会是个位数甚至触发切 D 模式。

**预期结果**：`totalLength = 32768`；一行 8192 字节对齐后仍是 8192；`ubLeft ≈ 194560 >= 16384`，走整行模式，`onceUbTokenNums ≈ 196608 / (8192*2 + 8*2*4) ≈ 11`（按公式精确计算，此处为估算值，待本地验证）。可见 hidden 越大，一次搬运的 token 越少——「大模型隐层维度直接吃掉搬运并行度」。

#### 4.4.5 小练习与答案

**练习 1**：为什么排序核数要规整为 4 的幂而不是 2 的幂或任意核数？

**答案**：kernel 使用的归并指令/流程按 4 路归并设计（常量 `MRG_LIST_NUM = 4`、`CeilLog4`），4 叉归并树要求叶子（各核局部排序结果）数量为 4 的幂才能对称地逐层归并；`Tiling4VMSMiddleCompute` 中「队列数 ≤ 4 则无需中间归并」也印证了 4 是一个归并层级。

**练习 2**：tiling key 在本算子中叠加了哪些「标志位」？各自含义是什么？

**答案**：基础值 1=单核排序、2=多核排序；`+2`（SPILT_D_MODE）表示 token 行过大需要拆行搬运；`+4`（ENABLE_NUMOUTTOKENS）表示输出被 `num_out_tokens` 截断。key 是一个把多种执行形态压缩进一个整数的位标志，kernel 侧据此选择编译期变体。

### 4.5 无顶层 op_api 的组织方式与跨算子转发

#### 4.5.1 概念说明

u1-l2 讲过「缺层有明确语义」。`moe_token_permute` 与 `moe_init_routing` 的目录结构给出一个新变体：**没有顶层 `op_api/` 目录，aclnn 实现放在 `op_host/op_api/` 子目录里**（可用 `ls moe/moe_token_permute/op_host/op_api` 验证，内含 `aclnn_moe_token_permute.cpp/.h` 与 v2 版本）。效果等价——两段式接口依然可用——但目录归属不同。这说明「五层范式」是惯例而非铁律，构建系统（u1-l4 的 add_modules_sources 按命名约定收集源文件）才是真正的契约。

更有意思的是**跨算子转发**：在 Ascend 950 上，`aclnnMoeTokenPermute(V2)` 并不执行本算子的 kernel，而是转调用 `MoeInitRoutingV2/V3`，再用 ViewCopy 把结果拷回自己的输出。这是「接口面稳定、实现面换轨」的版本演进手法——对上层框架完全透明。

#### 4.5.2 核心流程

`aclnnMoeTokenPermuteV2GetWorkspaceSize`（第一段）的分发逻辑：

```text
if (!IsRegbase())                      → 旧形态（membase）平台：
                                          走本算子自己的 inner 实现（aclnnInnerMoeTokenPermute...）
else if (SocVersion != ASCEND950)      → 950 以外的 regbase 平台：
                                          BuildMoeInitRoutingV2Executor：转发 l0op::MoeInitRoutingV2
else                                   → ASCEND950：
                                          PrepareIndicesForV3 + l0op::MoeInitRoutingV3
                                          （含中间张量分配与结果 Slice/ViewCopy）
```

其中 `IsRegbase()` 区分新一代（regbase，寄存器基座）与旧一代（membase）硬件形态；`l0op::` 前缀是 base 层组合 API（u4-l2 讲过的 l0/L2 分层），在 executor 上以「组计划、不下发」的方式串联子算子，最后统一产出 workspaceSize 与 executor。

#### 4.5.3 源码精读

- [moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp:L292-L320](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp#L292-L320)：`aclnnMoeTokenPermuteV2GetWorkspaceSize` 入口。先做非空与 shape 校验（两段式第一段的「校验漏斗」，u3-l1），随后 L306 判断 `IsRegbase()`：非 regbase 走本算子 inner 实现；L312 判断非 950 的 regbase 平台转 `BuildMoeInitRoutingV2Executor`；950 则走 V3 路径。
- [moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp:L158-L182](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp#L158-L182)：`BuildMoeInitRoutingV2Executor` 全貌——`l0op::Contiguous` 把输入连续化（u3-l1 讲过的自动连续化策略），调用 `l0op::MoeInitRoutingV2(tokens, indices, numOutTokens, 0, 0, 0, 0, false, ...)` 拿到 expandedXOut/expandedRowIdxOut 两个中间结果，再用两次 `l0op::ViewCopy` 拷到本接口的输出张量。参数映射正是 README L109-L114 文档化的那六条。
- [moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp:L267-L275](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp#L267-L275)：950 路径最终调用 `l0op::MoeInitRoutingV3(tokensContiguous, indicesContiguous, nullptr, nullptr, fullTokenNum, 0, ...)`——新一代硬件统一收敛到 init_routing 家族的最新实现。

对比 4.2.3：def 文件在 950 上不注册本算子的 AICore config、aclnn 又转发给 init_routing——**两层证据互相印证**，说明 950 上 MoeTokenPermute 是纯「兼容壳」。这类「壳 + 转发」模式在多代 SoC 并存的算子库中非常常见，也解释了 `moe/` 目录为何同时存在 init_routing 的 v2/v3/v4 与 token_permute 的 v1/v2 两套并行的接口族。

#### 4.5.4 代码实践

**实践目标**：验证「950 转发」在代码与文档两处的一致性。

**操作步骤**：

1. 在 [aclnn_moe_token_permute_v2.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/op_host/op_api/aclnn_moe_token_permute_v2.cpp) 中搜索 `ASCEND950` 与 `MoeInitRoutingV`，记录每个出现位置的行号与作用。
2. 打开 [moe/moe_token_permute/README.md:L106-L114](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/README.md#L106-L114)，比对文档写的映射关系与 `BuildMoeInitRoutingV2Executor` 的实参顺序是否一致。
3. 再看 v1 实现文件 `op_host/op_api/aclnn_moe_token_permute.cpp`（如存在转发逻辑，同样摘录）。

**需要观察的现象**：文档说「token 等同于 x 输入」，代码里正是 `l0op::MoeInitRoutingV2(tokensContiguous, indicesContiguous, numOutTokens, ...)` 的前两个实参。

**预期结果**：文档六条映射与代码实参一一对应；此类「README 即转发合同」的写法让使用者在遇到 950 特有报错（例如 activeNum 越界）时能直接换算到真正执行的算子参数上排查。

#### 4.5.5 小练习与答案

**练习 1**：`l0op::ViewCopy(expandedXOut, permuteTokensOut, ...)` 什么时候是「真拷贝」，什么时候可以零开销？

**答案**：当两个张量的内存排布（地址、shape、stride、offset）可视为同一视图时框架可优化为零拷贝；否则在 executor 中插入一次数据搬运（占用 workspace/带宽）。转发路径上输出张量由本接口的调用方分配、中间结果由 MoeInitRoutingV2 产出，两者通常不同，因此一般是一次真实拷贝——这是「壳接口」的固有代价。

**练习 2**：为什么 `aclnnMoeTokenPermute` 在 910b/910_93 上不也转发给 init_routing，统一实现？

**答案**：910b/910_93 上本算子有自己的、针对该代硬件调优过的 kernel（def 中注册的 `moe_init_routing`/独立实现 + 4.4 的 tiling），直接执行更高效；转发会平白多一层 Contiguous/ViewCopy。转发只在「目标平台没有本算子实现、但语义可由另一个已有算子覆盖」时使用（950 场景）。这是性能与维护成本的折中。

## 5. 综合实践

**任务**：产出一张「MoE 前向链路全景图 + 双算子 def 对照表」。

1. **建表**：按 4.3.4 的方法，用 grep 从两个 def 文件提取输入/输出/属性清单（含 dtype 白名单），做成对照表。
2. **画图**：画出 `gating → moe_init_routing（或 moe_token_permute）→ 专家计算 → moe_token_unpermute` 的流程图，每个张量标注 shape 表达式（用 `numTokens/topK/hidden/numExperts` 符号表示）与 dtype，并标注：
   - 哪些张量的 shape 在膨胀（×topK）、哪些在收缩（÷topK）；
   - `sortedIndices`/`expandedRowIdx` 这张「地图」由谁生产、谁消费；
   - Ascend 950 上 permute 实际执行的算子名（提示：4.5）。
3. **验证**（可选，需 NPU 环境）：阅读 [moe/moe_token_permute/examples/test_aclnn_moe_token_permute.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/examples/test_aclnn_moe_token_permute.cpp) 与 [moe/moe_init_routing/examples/test_aclnn_moe_init_routing.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/examples/test_aclnn_moe_init_routing.cpp)，把图中张量与示例代码里的变量一一对应；无 NPU 环境则完成纯源码走读并标注「待本地验证」。

**验收标准**：仅凭你的图，一个没读过 MoE 代码的同事能说出「token 数在哪一步翻倍、在哪一步恢复、靠什么恢复」。

## 6. 本讲小结

- MoE 前向 = **路由（gating）→ 重排（init_routing / token_permute）→ 专家计算 → 恢复（token_unpermute）**；token 数先 ×topK 膨胀再收缩，靠 permute 输出的索引地图对齐。
- `MoeInitRouting` 是老一代路由算子（3 入 3 出 + active_num），def 中通过两个 `OpAICoreConfig` + `ExtendCfgInfo("opFile.value", ...)` 为 910b/910_93 与 950 挂接**不同的 kernel 实现**。
- `MoeTokenPermute` 是新一代重排算子（2 入 2 出），语义是「argSort + 按映射搬运」；其 tiling 围绕**多核 4 叉归并排序**（核数规整为 4 的幂、schedule_mode=1 独占全核）与 **UB 约束下的 token 搬运切分**（整行模式 / 切 D 模式）展开，tiling key 是多种执行形态的位标志。
- 本算子展示了 `TilingBaseClass` 七步 tiling 模板（平台信息 → shape/属性 → 切分 → key → workspace → 落盘），比 add_example 的裸函数式更工程化。
- MoE 算子的 aclnn 实现可放在 `op_host/op_api/` 子目录（无顶层 op_api）；Ascend 950 上 `aclnnMoeTokenPermute(V2)` 是「兼容壳」，经 `l0op::MoeInitRoutingV2/V3` 转发实现，README 的参数映射表即转发合同。
- 排序类数值约束（expertIdx < 2^24、topK ≤ 512）源自 kernel 内部的键编码与容量设计——读约束要读到 kernel 的编码方式。

## 7. 下一步学习建议

- **下一讲 u5-l2（FFN 模块）**：本链路中「专家计算」那一格的内容——ffn 算子如何把多层子计算融合进单个 kernel，以及 swin 变体的场景差异。
- **u5-l3 / u5-l4（mc2 模块）**：多卡专家并行（EP）时，permute/unpermute 之间还要插入 `moe_distribute_dispatch/combine` 通信算子，是本讲链路的分布式扩展。
- **源码延伸阅读**：`moe/moe_init_routing_v2/op_host/op_api/aclnn_moe_init_routing_v2.cpp`（被转发目标的真身）、`moe/moe_token_unpermute/op_host/moe_token_unpermute_tiling.cpp`（恢复端的 tiling，与 permute 对称）、`moe/moe_token_permute/op_kernel/moe_sort_base.h` 等排序 kernel 头文件（tiling 合同的 device 消费端）。
