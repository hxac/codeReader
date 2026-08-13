# 采样、TopK 与 MoE 融合算子

## 1. 本讲目标

本讲聚焦 ATB 中两类「服务于生成与稀疏计算」的融合算子：

- **采样类算子**：在自回归生成（decode）的最后一步，把模型输出的概率分布「挑选」成一个 token 下标。
- **MoE（Mixture of Experts，混合专家）融合算子**：把「给每个 token 选专家 → 按专家重排 → 各专家分别算 → 合并」这套原本要十几个 kernel 的流程，收敛成几个融合算子。

学完后你应该能够：

1. 说清楚 `Multinomial`、`TopkToppSampling` 这类采样算子在生成解码里的位置，以及它们「输入概率、输出下标」的语义。
2. 看懂 `GroupTopk`、`FusedAddTopkDiv` 这两个「分组选 TopK」算子的参数与形状推导，理解它们为何是 DeepSeek 风格路由的核心。
3. 理解 `Gating` 算子如何把「token→专家」的映射反转为「专家→token」，并区分 TP（张量并行）与 EP（专家并行）两种场景。
4. 理解 `GroupedMatmulWithRouting` 如何用单个 `MoeGmm` 节点完成「多专家分组矩阵乘」，并区分 UP / DOWN 两阶段。
5. 能把上面这些算子按正确顺序串成一次完整的 MoE 前向调用链。

## 2. 前置知识

### 2.1 自回归生成与采样

大模型「生成文本」是逐 token 进行的：每一步根据已生成内容，模型在词表（vocabulary，通常几万个 token）上输出一个概率分布，再从中「挑」一个 token 追加到序列。挑的方式常见的有：

- **贪心（Greedy）**：直接取概率最大的那个 token。
- **Top-K 采样**：只在概率最大的 K 个候选里按概率随机抽。
- **Top-P（nucleus）采样**：把候选按概率从大到小累加，累计概率首次超过 P 的那些候选构成采样集合。
- **多项采样（Multinomial）**：把整个分布当概率，按概率随机抽。

无论哪种，最终都要落到「按概率随机抽一个下标」。这就是本讲采样算子要做的核心动作。

### 2.2 什么是 MoE

传统 Transformer 的每个 FFN 层对所有 token 用同一套权重。**MoE（混合专家）**则不同：它有 \(N\) 个「专家」（expert，每个就是一个小 FFN），对每个 token，先用一个轻量的「门控网络」（gating/router）打分，**只激活其中 top-k 个专家**来计算，再把结果加权合并。

直觉上：

\[
\text{MoE}(x) = \sum_{e \in \text{TopK}(g(x))} g_e(x) \cdot \text{Expert}_e(x)
\]

其中 \(g(x)\) 是门控打分（通常是一次线性层 + softmax/sigmoid）。MoE 的好处是**参数量大但计算量小**——模型总参数可以做到几千亿，但每个 token 只动其中一小部分专家。

### 2.3 MoE 在工程上的麻烦

MoE 前向看似简单，工程实现却很碎：

1. **路由打分**：算每个 token 对每个专家的分数，选 top-k。
2. **重排**：要把「按 token 排列」的数据，重排成「按专家排列」，这样同一专家才能批量算（矩阵乘更喜欢连续的一批）。
3. **分组矩阵乘**：每个专家对分到自己的 token 做一次 matmul。
4. **合并**：把各专家结果按原顺序拼回去，并乘上路由权重。

如果每一步都用一个普通算子，会触发十几次 kernel launch，Host 下发开销（回顾 u1-l1 的 Host Bound）很大。ATB 的做法是把这几步**各自融合**成大算子（`FusedAddTopkDiv`、`Gating`、`GroupedMatmulWithRouting`），把「打分/选专家」「重排」「分组乘」分别收敛到一两个 kernel。

### 2.4 你需要记住的前置结论

本讲算子全部继承 `OperationBase`（见 u3-l1），遵循 `Operation → Runner → KernelGraph → Kernel` 的统一链路（见 u3-l2）。每个算子的 `Operation` 子类只重写 `InferShapeImpl` 与 `CreateRunner` 两个钩子，形状推导规则与参数校验是本讲精读的重点。本讲涉及的算子**几乎都只支持 Atlas 800I A2 / A3（即 910B / 950）推理产品**，`CreateOperation` 入口会做芯片检查。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ops/ops_infer/multinomial/multinomial_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp) | 多项采样算子：输入概率分布，按概率随机抽 `numSamples` 个下标 |
| [src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp) | 高级采样算子：把 Top-K / Top-P / 多项采样 / 指数采样等多种策略收敛成一个算子 |
| [src/ops/ops_infer/group_topk/group_topk_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/group_topk/group_topk_operation.cpp) | 分组 TopK：把专家分成若干组，组内取最大再选 top-k 组 |
| [src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp) | DeepSeek 风格融合路由算子：Sigmoid+Add+GroupTopk+Gather+ReduceSum+RealDiv+Muls 一步完成 |
| [src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_ops_runner.cpp) | 上面的 Runner：组图为单个 `FusedAddTopkDiv` kernel 节点 |
| [src/ops/ops_infer/gating/gating_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp) | MoE 门控重排算子：把 token→专家 反转为 专家→token，区分 TP/EP |
| [src/ops/ops_infer/gating/gating_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_ops_runner.cpp) | Gating 的 Runner：按 TP/EP 分别组图 |
| [src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp) | MoE 专家计算算子：用各专家权重做分组矩阵乘，区分 UP/DOWN |
| [src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_runner.cpp) | 上面的 Runner：组图为单个 `MoeGmm` kernel 节点 |
| [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | 所有 Param 结构定义（`MultinomialParam`、`GatingParam`、`GroupTopkParam`、`GroupedMatmulWithRoutingParam`、`FusedAddTopkDivParam`） |

## 4. 核心概念与源码讲解

### 4.1 采样算子：Multinomial 与 TopkToppSampling

#### 4.1.1 概念说明

采样算子回答的问题是：「给定一串概率，随机给我一个下标」。它出现在**生成解码的最末端**：Attention + FFN 算完得到 logits，做 softmax 得到概率分布后，由采样算子挑出下一个 token 的下标。

ATB 提供两个层次的采样算子：

- **`Multinomial`（多项采样）**：最基础的版本。输入一个二维概率张量，对最后一维做多项采样，输出 `numSamples` 个被选中的下标。它只做「按概率抽」，**不做 top-k 截断**，所以调用方需自行保证输入已是想要的分布（注释明确提醒「用户需确保对最后一个轴进行归一化操作」）。
- **`TopkToppSampling`**：进阶版本，把 Top-K、Top-P、多项采样、指数采样（exponential）等多种解码策略**融合进一个算子**，还支持批量（batch）与 logprobs 输出。它是 vLLM 风格高性能解码的对应物。

本模块以 `Multinomial` 为主线精读（它最简洁、最能体现「输入概率→输出下标」的语义），并对 `TopkToppSampling` 做结构介绍。

#### 4.1.2 核心流程

`Multinomial` 的执行流程：

1. **建算子**（`CreateOperation`）：检查芯片必须是 910B/A3；检查 `numSamples ≤ 64`；检查 `rsv` 预留字段全 0。
2. **形状推导**（`InferShapeImpl`）：输出形状 = 输入形状，但第二维改成 `numSamples`，dtype 固定为 `ACL_INT32`（输出是下标）。
3. **校验**（`SetupCheckImpl`）：输入必须 2 维；`numSamples` 不能超过输入最后一维大小；输出形状须与推导一致。
4. **执行**（`CreateRunner` → `MultinomialOpsRunner`）：在 Device 上按概率抽样。

`TopkToppSampling` 的关键差异是它用一个枚举 `topkToppSamplingType` 选择策略，不同策略对应**不同的输入张量个数**和**不同的 IR 配置名**，这是「单 Param 覆盖 N 种行为」设计哲学（回顾 u4-l1）的又一实例。

#### 4.1.3 源码精读

**Multinomial 的关键常量与参数校验**：采样数上限硬编码为 64。

[multinomial_operation.cpp:L21-L37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L21-L37) —— `MAX_NUMSAMPLES = 64`，`ParamCheck` 拒绝超过 64 的采样数。

```cpp
static const uint64_t MAX_NUMSAMPLES = 64;
...
bool ParamCheck(const infer::MultinomialParam &opParam) {
    if (opParam.numSamples > MAX_NUMSAMPLES) { ... return false; }
    return true;
}
```

**芯片门槛**：`CreateOperation` 在 new 对象前卡死芯片类型，非 910B 直接返回 `ERROR_INVALID_PARAM`。

[multinomial_operation.cpp:L47-L53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L47-L53) —— 仅 Atlas 800I A2/A3 推理产品可用。

**形状推导**：这是采样算子最该记住的语义——输出是「下标」，所以 dtype 被强制为 `ACL_INT32`，且第二维变成采样数。

[multinomial_operation.cpp:L80-L90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L80-L90) —— `InferShapeImpl`：`out[0]` 形状透传，`dims[1] = numSamples`，`dtype = ACL_INT32`。

```cpp
outTensorDescs.at(0) = inTensorDescs.at(0);
outTensorDescs.at(0).shape.dims[1] = param_.numSamples;
outTensorDescs.at(0).shape.dimNum = OUT_TENSOR_DIM_NUM;   // 2
outTensorDescs.at(0).dtype = ACL_INT32;
```

**Setup 校验**：`numSamples` 不得超过输入最后一维（不能从 N 个候选里抽超过 N 个）。

[multinomial_operation.cpp:L97-L109](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L97-L109) —— `SetupCheckImpl` 检查 `numSamples > lastDim` 即报错。

**Param 定义**：只有 `numSamples`、`randSeed` 两个真参数加 `rsv[8]` 版本闸门。

[infer_op_params.h:L227-L240](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L227-L240) —— `MultinomialParam`，注释明确「用户需确保对最后一个轴进行归一化操作」。

**TopkToppSampling 的多策略分派**：根据枚举选不同的 IR 配置名，体现「一算子多形态」。

[topk_topp_sampling_operation.cpp:L60-L77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp#L60-L77) —— `GetOperationIrForTopkToppSampling` 按 `topkToppSamplingType` 分派 `BATCH_TOPK_EXPONENTIAL_SAMPLING`、`BATCH_TOPK_MULTINOMIAL_SAMPLING`、`SINGLE_TOPK_SAMPLING` 等不同 IR 名。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码与 Param，理解采样算子「输入概率→输出 INT32 下标」的契约，并能写出正确的输入输出张量描述。

**操作步骤**（源码阅读型实践）：

1. 打开 [multinomial_operation.cpp:L80-L90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L80-L90)，确认输出 dtype 是 `ACL_INT32`、第二维是 `numSamples`。
2. 假设词表大小 32000、batch=2，构造一个输入 TensorDesc：形状 `[2, 32000]`、dtype `ACL_FLOAT`（概率）。
3. 设 `MultinomialParam{numSamples=1, randSeed=42}`，推算输出 TensorDesc 应为 `[2, 1]`、dtype `ACL_INT32`。
4. 打开 [topk_topp_sampling_operation.cpp:L90-L95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp#L90-L95)，对比 `GetInputNum` 在不同 `topkToppSamplingType` 下返回 2/3/4 的差异。

**需要观察的现象**：若你把输入 dtype 写成 `ACL_INT32`（而不是概率的浮点），`Setup` 校验阶段会被 `OperationIr` 的 dtype 白名单拦截（回顾 u3-l1 的 `CheckIniMatch`）。

**预期结果**：输出张量每个元素是一个落在 `[0, 32000)` 的整数下标。`randSeed` 固定时结果可复现。

> 待本地验证：本实践为源码阅读型，未在 NPU 上实际运行；上述「randSeed 固定则可复现」需在 910B 设备上验证。

#### 4.1.5 小练习与答案

**练习 1**：`Multinomial` 的输入要求是 2 维，如果传入 1 维或 3 维张量会怎样？
**答案**：`DimNumCheck` 会返回 `ERROR_INVALID_TENSOR_DIM`，因为代码要求 `dimNum` 必须等于 `INPUT_TENSOR_DIM_NUM = 2`（见 [multinomial_operation.cpp:L135-L144](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L135-L144)）。

**练习 2**：为什么 `Multinomial` 把输出 dtype 强制设成 `ACL_INT32`？
**答案**：因为输出是「被选中的下标」，下标本质是整数索引，不是概率值；用 `INT32` 既表达语义又节省存储。

**练习 3**：`TopkToppSampling` 的 `SINGLE_TOPK_SAMPLING` 与 `BATCH_TOPK_MULTINOMIAL_SAMPLING` 在输入张量个数上有什么区别？
**答案**：前者输入 2 个（probs + topk），后者输入 3 个（多一个 topp 相关张量，见 [topk_topp_sampling_operation.cpp:L20-L22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/topk_topp_sampling/topk_topp_sampling_operation.cpp#L20-L22) 的 `SINGLE_TOPK_IN_TENSOR_NUM=2` 与 `BATCH_TOPK_MULTI_IN_TENSOR_NUM=3`）。

---

### 4.2 TopK 路由融合算子：GroupTopk 与 FusedAddTopkDiv

#### 4.2.1 概念说明

DeepSeek-V2/V3 的 MoE 用了一种「**分组限制路由**」（grouped-limited routing）：把全部专家分成 `groupNum` 组，先在每组内选代表，再在组与组之间选 top-k 组。这样可以避免某些「热门专家」被过度激活，起到负载均衡的作用。本模块两个算子就是为这种路由服务的：

- **`GroupTopk`**：基础版。输入 `[tokenNum, expertNum]` 的打分，按组选 top-k 组，**非选中组的数据全部置零**，输出形状不变（in-place 风格的掩码）。
- **`FusedAddTopkDiv`**：融合版，是 DeepSeek 路由的「主力算子」。它一步完成了 `Sigmoid + Add（加共享专家偏置）+ GroupTopk + Gather + ReduceSum + RealDiv + Muls` 一整套运算，直接输出**选中的 top-k 路由权重**和**对应的专家下标**。它的存在意义就是「把路由阶段七八个 kernel 融成一个」，大幅减少 kernel launch。

#### 4.2.2 核心流程

**GroupTopk** 流程：

1. 把 `expertNum` 个专家分成 `groupNum` 组（每组 `expertNum/groupNum` 个）。
2. 每组按 `groupMultiFlag` 取值：`UNDEFINED` 取组内最大；`SUM_MULTI_MAX` 取组内 `n` 个最大值求和。
3. 选出得分最高的 `k` 个组，把**不在 top-k 组里的数据置零**。
4. 输出形状与输入相同。

**FusedAddTopkDiv** 流程（看 Param 注释最直接）：

> Sigmoid+Add+GroupTopk+Gather+ReduceSum，RealDiv，Muls。

即：对门控打分做 sigmoid，加上共享专家（shared expert）的偏置 `addNum`，做分组 TopK 选出 `k` 个专家，再对权重做归一化（ReduceSum 后 RealDiv）和缩放（Muls 的 `scale`）。输出两个张量：`y`（`ACL_FLOAT`，路由权重，第二维 = `k`）和 `indices`（`ACL_INT32`，选中的专家下标，第二维 = `k`）。

#### 4.2.3 源码精读

**GroupTopk 的 Param 与分组语义**：

[infer_op_params.h:L2524-L2580](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2524-L2580) —— `GroupTopkParam` 含 `groupNum`、`k`、`groupMultiFlag`（`UNDEFINED` / `SUM_MULTI_MAX`）、`n`（组内取值个数）。注释明确「非前 k 个组的数据全部置零」。

**GroupTopk 形状推导**：输出 = 输入（掩码不改变形状）。

[group_topk_operation.cpp:L87-L92](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/group_topk/group_topk_operation.cpp#L87-L92) —— `InferShapeImpl`：`out[0] = in[0]`。

**GroupTopk 的分组约束校验**：`expertNum` 必须能被 `groupNum` 整除，`k ≤ groupNum`。

[group_topk_operation.cpp:L151-L178](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/group_topk/group_topk_operation.cpp#L151-L178) —— 校验 `groupNum ∈ [1, expertNum]`、`k ∈ [1, groupNum]`、`expertNum % groupNum == 0`。

**FusedAddTopkDiv 的 Param**：把整套路由参数打包，`activationType` 目前只支持 `ACTIVATION_SIGMOID`。

[infer_op_params.h:L2719-L2800](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2719-L2800) —— `FusedAddTopkDivParam` 含 `groupNum`、`groupTopk`、`n`、`k`、`activationType`、`isNorm`、`scale`、`enableExpertMapping`。

**FusedAddTopkDiv 参数校验**：`groupTopk ≤ groupNum`，`activationType` 只能是 `ACTIVATION_SIGMOID`。

[fused_add_topk_div_operation.cpp:L26-L53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L26-L53) —— `ParamCheck` 把非法组合一次性卡死。

**FusedAddTopkDiv 输入个数随 `enableExpertMapping` 变化**：默认 2 个输入（打分 x + 偏置 addNum），开启专家映射后多 2 个（mappingNum + mappingTable），用于物理专家↔逻辑专家转换。

[fused_add_topk_div_operation.cpp:L68-L74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L68-L74) —— `GetInputNum` 在 `enableExpertMapping` 时返回 4，否则 2。

**FusedAddTopkDiv 形状推导**：输出两个张量，形状第二维都是 `k`，dtype 分别是 `ACL_FLOAT`（权重）和 `ACL_INT32`（下标）。

[fused_add_topk_div_operation.cpp:L92-L104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L92-L104) —— `out[0].dtype=ACL_FLOAT, dims[1]=k`；`out[1].dtype=ACL_INT32, dims[1]=k`。

**Runner 组图：一整个融合 kernel**：注意 `kernelGraph_.nodes.resize(1)`——无论多么复杂的融合逻辑，Runner 端只组了**一个**节点，真正的融合发生在 Kernel 层。

[fused_add_topk_div_ops_runner.cpp:L54-L68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_ops_runner.cpp#L54-L68) —— 单节点 `FusedAddTopkDivOperation`，参数从 ATB Param 搬到 AtbOps Kernel Param。

```cpp
kernelGraph_.nodes.resize(1);
KernelGraphNode &fusedAddTopkDivNode = kernelGraph_.nodes.at(0);
AtbOps::OpParam::FusedAddTopkDiv fusedAddTopkDivParam;
// ... 把 param_.groupNum/k/n 等搬进 Kernel Param ...
fusedAddTopkDivNode.opDesc = {0, "FusedAddTopkDivOperation", fusedAddTopkDivParam};
```

**RunnerPool 复用**：`FusedAddTopkDivOpsRunner` 走 RunnerPool 复用（回顾 u3-l5），`CreateRunner` 先从池里 `MallocRunner`，失败才新建。

[fused_add_topk_div_operation.cpp:L123-L138](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L123-L138) —— 通过 `RunnerTypeRegister::GetRunnerTypeIdx("FusedAddTopkDivOpsRunner")` 拿到分桶池，`MallocRunner` 复用对象、`shared_ptr` 自定义删除器归还。

#### 4.2.4 代码实践

**实践目标**：对比 `GroupTopk` 与 `FusedAddTopkDiv`，理解「单步掩码」与「一步融合出权重+下标」的差别。

**操作步骤**（源码阅读型实践）：

1. 打开 [group_topk_operation.cpp:L87-L92](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/group_topk/group_topk_operation.cpp#L87-L92)，确认 `GroupTopk` 输出形状 = 输入形状（它是掩码，不抽取出 top-k）。
2. 打开 [fused_add_topk_div_operation.cpp:L92-L104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L92-L104)，确认 `FusedAddTopkDiv` 输出第二维 = `k`（它直接抽取出选中的 k 个）。
3. 在纸上假设 256 个专家、`groupNum=8`、`k=4`，分别写出两个算子的输出形状：`GroupTopk` 输出 `[tokenNum, 256]`；`FusedAddTopkDiv` 输出 `[tokenNum, 4]`（权重）+ `[tokenNum, 4]`（下标）。

**需要观察的现象**：`GroupTopk` 不改变专家维大小，只是把非选中组置零；`FusedAddTopkDiv` 直接把专家维从 256 收缩到 k=4。这正是「掩码」与「抽取」两种风格的区别。

**预期结果**：能清楚说出两者输出形状与语义的差异。

> 待本地验证：本实践为源码阅读型。

#### 4.2.5 小练习与答案

**练习 1**：`GroupTopkParam::groupMultiFlag` 取 `SUM_MULTI_MAX` 时，参数 `n` 起什么作用？
**答案**：`n` 表示「每组内取 n 个最大值再求和」作为该组的代表得分，取值范围 `[1, expertNum/groupNum]`（见 [group_topk_operation.cpp:L166-L171](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/group_topk/group_topk_operation.cpp#L166-L171)）。

**练习 2**：为什么 `FusedAddTopkDiv` 的 `CreateRunner` 用 RunnerPool，而 `GroupedMatmulWithRouting`（下一节）用 `std::make_shared` 直接新建？
**答案**：RunnerPool 复用的是「构造昂贵、参数可替换」的 Runner 对象（含 KernelGraph 等），适合频繁同型调用；是否走池取决于该 Runner 是否注册并设计为可复用。`FusedAddTopkDivOpsRunner` 实现了 `SetParam` 且注册了类型（见 [fused_add_topk_div_ops_runner.cpp:L74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_ops_runner.cpp#L74) 的 `REG_RUNNER_TYPE`），故走池。

**练习 3**：`FusedAddTopkDiv` 的 `addNum` 输入张量在数学上对应什么？
**答案**：对应 DeepSeek 里「共享专家」（shared expert）的偏置项——`Add` 这一步把共享专家的贡献加到各路由专家的打分上，即 Param 注释里的 `Sigmoid+Add+...` 中的 Add。

---

### 4.3 MoE 门控算子：Gating（TP / EP 双场景）

#### 4.3.1 概念说明

路由算子选出「每个 token 用哪几个专家」后，下一步要**按专家把 token 重排**，让同一专家处理的 token 连续排在一起，分组矩阵乘才能高效。

`Gating` 算子做的就是这件事。它的 Param 注释说得很清楚：

> 主要功能为将 token 和专家的映射关系反转为专家与 token 的映射关系。算子输入为 MoE 模型每个 token 选中专家的索引，算子输出为 MoE 模型每个专家对应的 token 的索引。

注意：`Gating` **不做打分、不做 softmax**——打分与选专家是上游（如 `FusedAddTopkDiv`）的事。`Gating` 的输入已经是「选好的专家下标」，它只负责**反排 + 累积计数**。

`Gating` 还区分两种并行场景：

- **TP（张量并行，Tensor Parallelism）**：`deviceExpert` 为空。所有专家都参与，输出 3 个张量。
- **EP（专家并行，Expert Parallelism）**：`deviceExpert` 非空，给出「当前 device 上有哪些专家」。只有本设备的专家参与，输出 4 个张量（多一个 `validIndex`）。

#### 4.3.2 核心流程

`Gating` 的输入是 2 个一维张量：

- `inTensor0`：所有 token 选中的专家下标展平，长度 = `tokenNum × topkExpertNum`（每个 token 选了 `topkExpertNum` 个专家）。
- `inTensor1`：配套的下标数组（与 inTensor0 等长，用于还原原始位置）。

输出（TP，3 个）：

- `tokenIndex`：按专家分组重排后的 token 序列，长度 = 输入长度。
- `cumSum`：每个专家分到多少 token 的累积和，长度 = `cumSumNum`（专家总数）。
- `originalIndex`：重排后每个位置对应的原始 token 下标，长度 = 输入长度。

输出（EP，多 1 个）：

- `validIndex`：本设备有效的路由数量指示，长度 = 1。

`cumSum` 是后续 `GroupedMatmulWithRouting` 的关键输入之一（它告诉矩阵乘「每个专家要处理连续的哪一段 token」），所以 `Gating` 与 `GroupedMatmulWithRouting` 是配套使用的。

#### 4.3.3 源码精读

**Param 定义与 TP/EP 区分**：

[infer_op_params.h:L436-L483](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L436-L483) —— `GatingParam` 含 `topkExpertNum`、`cumSumNum`（专家总数，`[0,200]`）、`cumSumInt64`（cumSum 是否用 INT64）、`deviceExpert`（EP 时填本设备专家列表，元素须唯一且在 `[0, cumSumNum)`）。

**CreateOperation 的多重校验**：`cumSumNum` 范围、`topkExpertNum` 与 `cumSumNum` 的关系、EP 时 `deviceExpert` 元素唯一性与范围。

[gating_operation.cpp:L29-L82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L29-L82) —— 用 `std::unordered_set` 检查 `deviceExpert` 元素不重复；EP 仅 910B 支持。

**输出个数随场景变化**：TP 输出 3 个，EP 输出 4 个。

[gating_operation.cpp:L100-L103](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L100-L103) —— `GetOutputNum`：`deviceExpert.empty() ? 3 : 4`。

**形状推导**：`tokenIndex` / `originalIndex` 长度 = 输入长度（`topkDim0`）；`cumSum` 长度 = 专家数（`cumSumNum` 或 `deviceExpert.size()`）；EP 的 `validIndex` 长度 = 1。

[gating_operation.cpp:L105-L130](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L105-L130) —— `InferShapeImpl`：TP 时 `cumSum.dims[0] = cumSumNum==0 ? 1 : cumSumNum`；EP 时 `cumSum.dims[0] = deviceExpert.size()`，并额外填 `validIndex.dims[0]=1`。

**Runner 按 TP/EP 分别组图**：两条路径都只组 1 个 `GatingOperation` 节点，差别在输出张量接线数量。

[gating_ops_runner.cpp:L31-L49](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_ops_runner.cpp#L31-L49) —— `SetupKernelGraph` 按 `deviceExpert.empty()` 分流到 `SetupKernelGraphGating`（3 输出）或 `SetupKernelGraphGatingExpertParallelism`（4 输出）。

[gating_ops_runner.cpp:L64-L69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_ops_runner.cpp#L64-L69) —— TP 路径：节点 `opDesc = {"GatingOperation", gatingParam_}`，注意输出列表第 4 个填了 `&nullTensor_` 占位（Runner 内部统一 4 输出槽，TP 时第 4 个为空）。

**Runner 端 Param 转译**：ATB 的 `GatingParam` 字段被映射成 Kernel 层更通用的 `headNum`/`headSize` 语义。

[gating_ops_runner.cpp:L23-L27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_ops_runner.cpp#L23-L27) —— 构造函数里 `gatingParam_.headNum = topkExpertNum`、`headSize = cumSumNum`。

#### 4.3.4 代码实践

**实践目标**：理清 `Gating` 输入输出张量的个数与形状随 TP/EP 切换的变化。

**操作步骤**（源码阅读型实践）：

1. 打开 [gating_operation.cpp:L95-L103](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L95-L103)，记录：输入恒为 2，输出 TP=3 / EP=4。
2. 打开 [gating_operation.cpp:L105-L130](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L105-L130)，列出 4 个输出张量的形状推导规则。
3. 假设 `tokenNum=128`、`topkExpertNum=2`、`cumSumNum=64`，TP 场景下：`inTensor0` 长度 = `128×2=256`；`cumSum` 长度 = 64。

**需要观察的现象**：把 `deviceExpert` 从空改成 `{0, 16, 32}` 后，`GetOutputNum` 从 3 变 4，`cumSum` 长度从 64 变 3（只统计本设备 3 个专家）。

**预期结果**：能复述 TP/EP 两种场景下 4 个输出张量的含义与长度。

> 待本地验证：本实践为源码阅读型。

#### 4.3.5 小练习与答案

**练习 1**：`Gating` 的 `inTensor0` 长度为什么是 `tokenNum × topkExpertNum`，而不是 `tokenNum`？
**答案**：因为每个 token 选中了 `topkExpertNum` 个专家，所以「(token, 选中专家)」二元组的总数是 `tokenNum × topkExpertNum`，`inTensor0` 把它们展平存放（见 [gating_operation.cpp:L165-L169](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L165-L169) 的整除校验）。

**练习 2**：EP 场景相比 TP 多输出的 `validIndex` 是干什么用的？
**答案**：EP 下只有本设备专家列表里的路由才有效，`validIndex`（长度 1）用来指示本设备实际有效的路由数量，供下游在跨设备通信/计算时裁剪无效部分（见 [gating_operation.cpp:L124-L127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L124-L127)）。

**练习 3**：为什么说 `Gating` 与 `GroupedMatmulWithRouting` 是配套的？
**答案**：`Gating` 输出的 `cumSum`（每专家 token 计数）和重排后的 `tokenIndex` 正是 `GroupedMatmulWithRouting` 需要的 `expertCount` 与路由下标输入——前者告诉矩阵乘「每段多长」，后者告诉它「按什么顺序取 token」。

---

### 4.4 MoE 专家计算算子：GroupedMatmulWithRouting

#### 4.4.1 概念说明

路由、重排都做完后，最后一步是真正的「**每个专家对自己分到的 token 做矩阵乘**」。`GroupedMatmulWithRouting` 就是这个算子。

它对应 MoE FFN 的两段结构：

- **UP（上投影）**：把 hidden 维放大（如 `hidden → intermediate`），随后接激活（如 SiLU）。UP 阶段输入是 `tokenNum` 个 token，输出是 `tokenNum × topK` 个（每个 token 被复制到 topK 个专家）。
- **DOWN（下投影）**：把 intermediate 维缩回 hidden，并把 topK 个专家的结果加权合并。DOWN 阶段输入是 `tokenNum × topK`，输出缩回 `tokenNum`。

所以 UP 把第 0 维乘 `topK`，DOWN 把第 0 维除 `topK`——这就是它形状推导的核心。它还支持**量化**（`outDataType` 非 `ACL_DT_UNDEFINED` 时走 int8 权重 + 反量化，多 2 个 scale 输入）。

#### 4.4.2 核心流程

输入（非量化，4 个）：

- `acTensor`（inTensor0）：激活 `[tokenNum(或 tokenNum×topK), hidden_in]`。
- `expertWeight`（inTensor1）：各专家权重 `[num_experts, hidden_out, hidden_in]`（transposeB=true 时）。
- `expertCount`（inTensor2）：每个专家分到的 token 数 `[num_experts]`（即 Gating 的 cumSum）。
- `expertIndex`（inTensor3）：路由下标。

输入（量化，6 个）：再多 `nScale`（inTensor4）与 `mScale`（inTensor5）。

输出：1 个张量。

形状推导：

- UP：`out.dims[0] = in.dims[0] × topK`
- DOWN：`out.dims[0] = in.dims[0] / topK`
- `out.dims[1] = hidden_out`（由权重第二维决定）

Runner 把它组图为**单个 `MoeGmm`（Mixture-of-Experts Grouped MatMul）kernel 节点**，融合在 Kernel 层完成。

#### 4.4.3 源码精读

**Param 定义**：`GroupedMatmulType`（UP/DOWN）、`transposeB`、`topK`、`outDataType`（量化开关）。

[infer_op_params.h:L2583-L2624](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2583-L2624) —— `GroupedMatmulWithRoutingParam`，仅 Atlas 800I A2 支持。

**CreateOperation 的硬约束**：仅 910B；`topK ∈ [2, 10]`；`outDataType ∈ {UNDEFINED, FP16, BF16}`。

[grouped_matmul_with_routing_operation.cpp:L52-L78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L52-L78) —— 一连串 `if` 把非法参数挡在 new 对象之前。

**输入个数随量化开关变化**：非量化 4 个，量化 6 个。

[grouped_matmul_with_routing_operation.cpp:L92-L98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L92-L98) —— `GetInputNum`：`outDataType != UNDEFINED ? 6 : 4`。

**形状推导：UP 乘 topK、DOWN 除 topK**：这是本算子最关键的形状规则。

[grouped_matmul_with_routing_operation.cpp:L105-L120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L105-L120) —— UP 分支 `dims[0] *= topK`；DOWN 分支 `dims[0] /= topK`；`dims[1]` 取权重的输出 hidden 维。

```cpp
if (param_.groupedMatmulType == GROUPED_MATMUL_UP) {
    outTensorDescs.at(0).shape.dims[0] = inTensorDescs.at(0).shape.dims[0] * param_.topK;
} else {
    outTensorDescs.at(0).shape.dims[0] = inTensorDescs.at(0).shape.dims[0] / param_.topK;
}
outTensorDescs.at(0).shape.dims[1] = OperationUtil::GetYTensorN(inTensorDescs.at(1), param_.transposeB);
```

**维度硬上限校验**：专家数 `[128, 256]`、token 数 `[128, 512]`、hidden 各维都有明确区间，且 hidden 须 32 对齐。

[grouped_matmul_with_routing_operation.cpp:L190-L226](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L190-L226) —— `ExpertCountTensorCheck`（专家数 128–256）、`ExpertIndexTensorCheck`（token 数 128–512）。

[grouped_matmul_with_routing_operation.cpp:L162-L165](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L162-L165) —— hidden 须 32 对齐（`ALIGHMENT_NUMBER = 32`）。

**Runner 组图：单 MoeGmm 节点 + 量化分支**：和 `FusedAddTopkDiv` 一样，Runner 只组 1 个节点；量化时把 `outDataType` 翻译成 Kernel 的 `DEQ_BF16/DEQ_FP16`，并把 weight 的 NZ 排布做 view reshape。

[grouped_matmul_with_routing_runner.cpp:L43-L93](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_runner.cpp#L43-L93) —— `SetupKernelGraph`：`moeGmmNode.opDesc = {0, "MoeGmmOperation", opParam}`，量化时 `inTensors` 多挂 `weightscale`、`activatescale`。

[grouped_matmul_with_routing_runner.cpp:L54-L67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_runner.cpp#L54-L67) —— 把 `groupedMatmulType`、`transposeB`、`topK`、`hiddenSize` 搬进 `AtbOps::OpParam::MoeGmm`，体现「ATB Param → Kernel Param」的转译层（回顾 u3-l4 的注册名衔接）。

#### 4.4.4 代码实践

**实践目标**：通过形状推导规则，验证 UP/DOWN 两阶段首维的 `×topK` / `/topK` 变换是否自洽。

**操作步骤**（源码阅读型实践）：

1. 打开 [grouped_matmul_with_routing_operation.cpp:L105-L120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L105-L120)，确认 UP 乘、DOWN 除。
2. 假设 `tokenNum=256`、`topK=4`、`hidden=5120`、`intermediate=256`：
   - UP 输入 `[256, 5120]` → 输出 `[256×4=1024, 256]`。
   - 中间接激活（不在本算子内）。
   - DOWN 输入 `[1024, 256]` → 输出 `[1024/4=256, 5120]`，回到原始 tokenNum。
3. 检查 [grouped_matmul_with_routing_runner.cpp:L61-L63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_runner.cpp#L61-L63)，确认 `opParam.topK` 与 `opParam.moeGmmMode` 来自 ATB Param，理解 UP/DOWN 复用同一 Kernel、仅靠 mode 区分。

**需要观察的现象**：UP 后首维膨胀 4 倍，DOWN 后首维缩回，两者首维相乘/相除恰好抵消，保证 token 数守恒。

**预期结果**：能写出 UP/DOWN 两步的输入输出形状，并解释为何首维这样变化（每个 token 要被 topK 个专家各算一次）。

> 待本地验证：本实践为源码阅读型；真实维度上限（专家 128–256、token 128–512）需在 910B 上验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 UP 阶段输出首维是 `tokenNum × topK`，而 DOWN 阶段输入首维也是 `tokenNum × topK`？
**答案**：UP 把每个 token 复制到它选中的 topK 个专家分别计算，所以 token 数膨胀为 `tokenNum × topK`；DOWN 把这 topK 份结果合并回每个 token，所以首维又缩回 `tokenNum`。两阶段首维互为逆运算。

**练习 2**：`outDataType = ACL_DT_UNDEFINED` 与 `ACL_BF16` 在输入张量个数上的差别是什么？
**答案**：前者（非量化）4 个输入，后者（量化）6 个输入——多出 `nScale`（权重反量化 scale）和 `mScale`（激活反量化 scale），见 [grouped_matmul_with_routing_operation.cpp:L92-L98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L92-L98)。

**练习 3**：`GroupedMatmulWithRouting` 对 hidden 维有什么硬性约束？
**答案**：hidden 维必须 32 对齐（`ALIGHMENT_NUMBER = 32`），且 UP/DOWN 各自有 `[32, 5120]` / `[32, 256]` 等区间限制，专家数须在 `[128, 256]`、token 数在 `[128, 512]`（见 [grouped_matmul_with_routing_operation.cpp:L162-L165](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L162-L165) 与 L190-L226）。

---

## 5. 综合实践：梳理一次 MoE 前向的完整调用链

**实践目标**：把本讲四个 MoE 算子（`FusedAddTopkDiv`、`Gating`、`GroupedMatmulWithRouting`）与采样算子串成一条端到端调用链，回答「一次 MoE 前向（Gating→TopK 路由→GroupedMatmul）需要调用哪些 ATB 算子、按什么顺序」。

**背景设定**：DeepSeek 风格 MoE 层，输入 `x` 形状 `[tokenNum, hidden]`，专家数 256，每 token 选 `topK=4` 个专家，分组 `groupNum=8`。

**操作步骤**：

1. **第 1 步——路由打分（gate Linear）**：用普通 `Linear` 算子（u4-l1）把 `x` 从 `hidden` 投影到 `expertNum=256`，得到门控 logits `[tokenNum, 256]`。

2. **第 2 步——融合路由（FusedAddTopkDiv）**：把 logits 与共享专家偏置 `addNum` 送入 `FusedAddTopkDiv`，它一步完成 Sigmoid+Add+GroupTopk+归一化，输出：
   - `y`：路由权重 `[tokenNum, k=4]`（`ACL_FLOAT`）；
   - `indices`：选中的专家下标 `[tokenNum, 4]`（`ACL_INT32`）。
   参见 [fused_add_topk_div_operation.cpp:L92-L104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/fused_add_topk_div/fused_add_topk_div_operation.cpp#L92-L104)。

3. **第 3 步——按专家重排（Gating）**：把 `indices`（展平成长度 `tokenNum×topkExpertNum` 的一维张量）送入 `Gating`，得到：
   - `tokenIndex`：按专家分组的 token 序列；
   - `cumSum`：每专家 token 计数（长度 256）——这正是下一步的 `expertCount`；
   - `originalIndex`：用于最终合并时还原顺序。
   参见 [gating_operation.cpp:L105-L130](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/gating/gating_operation.cpp#L105-L130)。

4. **第 4 步——专家上投影（GroupedMatmulWithRouting UP）**：用 UP 模式，输入 `x`（按 `tokenIndex` 重排后）、各专家 UP 权重、`expertCount=cumSum`、路由下标，输出 `[tokenNum×4, intermediate]`。参见 [grouped_matmul_with_routing_operation.cpp:L108-L110](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L108-L110)。

5. **第 5 步——激活**：用 `Activation` 算子（u4-l3，SiLU）作用于上投影结果。

6. **第 6 步——专家下投影（GroupedMatmulWithRouting DOWN）**：用 DOWN 模式，输出首维缩回 `[tokenNum, hidden]`，完成各专家结果合并。参见 [grouped_matmul_with_routing_operation.cpp:L111-L114](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/grouped_matmul_with_routing/grouped_matmul_with_routing_operation.cpp#L111-L114)。

7. **（生成场景）第 7 步——采样**：若这是生成任务的最后一层，把输出经 LM Head 投影到词表后，用 `TopkToppSampling` 或 `Multinomial` 采样出下一个 token 下标。参见 [multinomial_operation.cpp:L80-L90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/multinomial/multinomial_operation.cpp#L80-L90)。

**用一张表总结调用链**：

| 顺序 | 算子 | 作用 | 关键输入 → 关键输出 |
| --- | --- | --- | --- |
| 1 | `Linear`（gate） | 门控打分 | `x` → logits `[tokenNum, 256]` |
| 2 | `FusedAddTopkDiv` | 融合路由+选专家 | logits + addNum → 权重 `y` + 下标 `indices` |
| 3 | `Gating` | 按专家重排 | `indices` → `tokenIndex` + `cumSum` |
| 4 | `GroupedMatmulWithRouting`(UP) | 专家上投影 | 重排 x + 专家权重 + cumSum → `[tokenNum×4, inter]` |
| 5 | `Activation` | 激活 | SiLU |
| 6 | `GroupedMatmulWithRouting`(DOWN) | 专家下投影合并 | → `[tokenNum, hidden]` |
| 7 | `TopkToppSampling`/`Multinomial` | 采样下一个 token | 词表概率 → token 下标 |

**需要观察的现象**：

- 数据在第 2 步从「按 token」视角切到「按专家」视角（重排），在第 6 步又切回「按 token」视角（合并）。
- `FusedAddTopkDiv` 与两个 `GroupedMatmulWithRouting` 的 Runner 都只组了**一个** kernel 节点——这正是「融合算子减少 kernel launch」收益的来源（回顾 u1-l1 的 Host Bound）。
- `Gating` 的 `cumSum` 直接喂给 `GroupedMatmulWithRouting` 的 `expertCount`，两者是配套契约。

**预期结果**：能画出上述 7 步的数据流图，标注每一步的张量形状，并说明哪些算子是「融合」的、融合掉了哪些原始 kernel。

> 待本地验证：本综合实践为源码阅读+调用链梳理型，未在 NPU 上端到端运行；各算子的芯片支持（多限 910B/A3）与维度上限需在实际环境确认。

## 6. 本讲小结

- **采样算子**（`Multinomial`、`TopkToppSampling`）位于生成解码末端，语义是「输入概率分布、输出 `ACL_INT32` 下标」；`TopkToppSampling` 用一个枚举收敛 Top-K/Top-P/多项/指数等多种解码策略。
- **`GroupTopk`** 是「分组选 top-k 组、非选中组置零」的掩码算子，输出形状不变；**`FusedAddTopkDiv`** 是 DeepSeek 路由主力，一步融合 `Sigmoid+Add+GroupTopk+归一化`，直接输出路由权重 `y`（`ACL_FLOAT`）和专家下标 `indices`（`ACL_INT32`），第二维均为 `k`。
- **`Gating`** 不做打分，只做「token→专家」到「专家→token」的反排，输出 `tokenIndex`、`cumSum`（每专家计数）、`originalIndex`，EP 场景多一个 `validIndex`；它的 `cumSum` 是 `GroupedMatmulWithRouting` 的 `expertCount` 输入。
- **`GroupedMatmulWithRouting`** 用单个 `MoeGmm` kernel 节点完成多专家分组矩阵乘：UP 模式首维 `×topK`、DOWN 模式首维 `/topK`；支持量化（多 2 个 scale 输入）。
- 所有这些算子的 Runner 端都把复杂逻辑收敛成**一两个** `KernelGraphNode`，真正的融合发生在 Kernel 层——这就是「融合算子缓解 Host Bound」的具体落地。
- 本讲算子几乎都仅支持 Atlas 800I A2 / A3（910B / 950），`CreateOperation` 入口的芯片与参数校验非常严格（采样数上限、topK 区间、专家数区间、hidden 对齐等）。

## 7. 下一步学习建议

- **横向对照其它「单 Param 覆盖多行为」算子**：本讲的 `GroupedMatmulWithRoutingParam`（UP/DOWN）、`TopkToppSamplingParam`（多策略）与 u4-l1 的 `LinearParam`、u4-l2 的 Norm `layerType` 同属一套设计哲学，建议回看加深理解。
- **深入 Runner 组图机制**：本讲多个 Runner 都展示了「ATB Param → Kernel Param」的转译与 `REG_RUNNER_TYPE` / `REG_OP_PARAM` 注册，这正是 u3-l2「Runner→KernelGraph→Kernel」链路的具体样例，建议结合 u3-l2 复习。
- **进入通信与图算子**：MoE 的 EP（专家并行）场景天然涉及跨卡通信，下一单元 u5-l1（通信算子与 HCCL）会讲 All-to-All 等集合通信，是 MoE 多卡部署的配套知识。
- **自定义算子开发**：若想新增一个类似 `FusedAddTopkDiv` 的融合路由算子，可参考 u6 单元（自定义 Kernel 与框架集成），本讲的 Runner 组图与 Param 校验模式就是模板。
