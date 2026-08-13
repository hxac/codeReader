# RoPE 与位置编码算子

## 1. 本讲目标

本讲聚焦 ATB 中与「位置编码」相关的三个算子。读完本讲，你应当能够：

- 说清 **旋转位置编码（RoPE）** 的直觉与数学含义，并知道它为什么能让注意力只依赖「相对位置」。
- 读懂 `RopeOperation` 的参数（`RopeParam`）、输入输出约定、形状校验与双后端（aclnn / ops）Runner 分派。
- 理解 `DynamicNTKOperation` 如何为长序列「外推」动态生成 cos/sin 位置表，以及它和 `RopeOperation` 的配合关系。
- 理解 `NormRopeReshapeOperation` 这一融合算子把 `RMSNorm + RoPE + ReshapeAndCache` 三步合一带来的性能收益来源。

本讲依赖 u3-l1（`OperationBase` 骨架）与 u3-l2（Runner 体系），请先建立「`Operation → Runner → KernelGraph → Kernel`」的调用链心智。

## 2. 前置知识

### 2.1 为什么需要位置编码

Transformer 的自注意力机制本身是「顺序无关」的：把句子里两个词调换位置，注意力分数的计算过程几乎不变。为了让模型感知词的先后顺序，需要在送入注意力之前给 Q、K 注入位置信息，这就是**位置编码（Positional Encoding）**。

早期做法（如原始 Transformer、BERT）是把一个位置向量「加」到词向量上。RoPE 用的是另一种思路——**旋转**。

### 2.2 RoPE 的直觉

把 Q、K 中每一对相邻的元素看作二维平面的一个向量，按它所在的位置 \(m\) 旋转一个角度。位置越靠后，旋转角度越大。两个向量做内积（注意力分数）时，夹角只取决于它们的「位置差」，于是注意力天然只依赖相对位置——这正是我们想要的。

用数学语言描述，对于位置 \(m\)、第 \(i\) 个二维子空间的旋转角 \(\theta_i\)，有：

\[
\theta_i = \text{base}^{-2i/d}, \quad i = 0,1,\dots,d/2-1
\]

\[
\begin{pmatrix} q'_{2i} \\ q'_{2i+1} \end{pmatrix}
=
\begin{pmatrix} \cos(m\theta_i) & -\sin(m\theta_i) \\ \sin(m\theta_i) & \cos(m\theta_i) \end{pmatrix}
\begin{pmatrix} q_{2i} \\ q_{2i+1} \end{pmatrix}
\]

等价地，写成按通道整体运算的形式：

\[
q' = q \odot \cos(m\theta) + \mathrm{rotate}(q) \odot \sin(m\theta)
\]

其中 \(\odot\) 是逐元素乘，`rotate` 是一种「把向量半部分交换并取负」的重排操作（具体形态见 4.1）。关键性质是：

\[
\langle R_m q,\ R_n k \rangle = \langle q,\ R_{n-m} k \rangle
\]

即内积只依赖相对位置 \(n-m\)。

### 2.3 术语速查

| 术语 | 含义 |
|------|------|
| RoPE | Rotary Position Embedding，旋转位置编码 |
| cos / sin | 预计算的位置编码表，按位置与维度提供旋转角度 |
| rotaryCoeff | 旋转系数，决定 `rotate` 的配对方式（2/4/headDim） |
| NTK | Neural Tangent Kernel，这里指一种频率缩放外推方法（见 4.2） |
| 融合算子 | 把多个算子合并成一个 Kernel 下发，减少 launch 与中间显存读写 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [include/atb/infer_op_params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h) | 三个 Param（`RopeParam`、`DynamicNTKParam`、`NormRopeReshapeParam`）的定义 |
| [src/ops/ops_infer/rope/rope_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp) | `RopeOperation` 的形状推导、校验与 Runner 分派 |
| [src/ops/ops_infer/rope/rope_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_ops_runner.cpp) | 非 950 芯片走 `OpsRunner`，组一张单节点 `KernelGraph` |
| [src/ops/ops_infer/rope/rope_aclnn_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_aclnn_runner.cpp) | 950 芯片桥接 CANN 官方算子 `aclnnApplyRotaryPosEmbV2` |
| [src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp) | `DynamicNTKOperation`，动态生成 NTK 外推的 cos/sin |
| [src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp) | `NormRopeReshapeOperation`，三合一融合算子 |
| [src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_ops_runner.cpp) | 融合算子的 Runner，组一张 `RmsNormAndRopeAndReshapeAndCacheOperation` 节点 |

整体关系：`DynamicNTK` 负责生成 cos/sin，`Rope` 用它们旋转 Q/K；而 `NormRopeReshape` 把「归一化 + 旋转 + 写 KV Cache」三步融合成一个 Kernel。

## 4. 核心概念与源码讲解

### 4.1 RopeOperation：旋转位置编码算子

#### 4.1.1 概念说明

`RopeOperation` 是 ATB 对 RoPE 的标准实现：输入 Q、K 和预先算好的 cos、sin 表，输出旋转后的 `qEmbedded`、`kEmbedded`。它的设计哲学与 u4-l1 的 `LinearParam` 一脉相承——**一个 Param 覆盖多种行为**，靠 `rotaryCoeff` 这一个旋钮表达不同的旋转配对方式，而不是拆成多个算子。

`RopeParam` 的定义非常精简：

```cpp
// include/atb/infer_op_params.h:1608-1617
struct RopeParam {
    int32_t rotaryCoeff = 4;   // 旋转系数，对半旋转是2，支持配置2、4或headDim / 2
    int32_t cosFormat = 0;     // 训练用参数，支持配置0或1
    uint8_t rsv[8] = {0};      // 预留参数（版本闸门）
};
```

`rotaryCoeff` 的取值决定了 `rotate` 函数如何把 head 维度重排：

- **`rotaryCoeff = 2`（half）**：Llama / GPT-NeoX 风格。把 head 维一分为二，前半与后半交叉配对。这是最常用的模式（CSV 测试里 LLaMA2-7B/13B 用 `{"rotaryCoeff": 2}`）。
- **`rotaryCoeff = 4`（quarter）**：四等分配对。
- **`rotaryCoeff = headDim/2`（interleave）**：GPT-J 风格，相邻两元素 `(x_{2i}, x_{2i+1})` 配对。

这三种模式在后端会被翻译成字符串 `"half"` / `"quarter"` / `"interleave"`（见 4.1.3 aclnn 路径）。

#### 4.1.2 核心流程

`RopeOperation` 继承自 `OperationBase`，沿用 u3-l1 讲过的「两段式」骨架：

1. **创建**（`CreateOperation`）：校验 `rsv`、在 950 上预加载 aclnn 算子并检查 `cosFormat`。
2. **InferShape 阶段**：5 个输入张量、2 个输出张量；输出形状直接透传输入（RoPE 不改变形状）。
3. **校验阶段**（`InferShapeCheckImpl` → `DimCheck` → `ParamCheck` → `HiddenSizeCheck`）：逐层卡死维度、旋转向量与隐层大小关系。
4. **CreateRunner**：按芯片分派——950 走 `RopeAclnnRunner`，其它芯片走 `RunnerPool` 复用的 `RopeOpsRunner`。
5. **Execute**：由 Runner 把任务下发到 Device。

执行的数据流（伪代码）：

```
inputs:  [qLayer, kLayer, cos, sin, seqLen]   # 5 个
outputs: [qEmbedded, kEmbedded]               # 2 个

qEmbedded[i] = qLayer[i] * cos + rotate(qLayer[i]) * sin
kEmbedded[i] = kLayer[i] * cos + rotate(kLayer[i]) * sin
```

#### 4.1.3 源码精读

**输入输出个数**——恒定 5 入 2 出，与 Param 无关（不像 SelfAttention 那样随字段动态变化）：

```cpp
// rope_operation.cpp:25-26
static const int32_t IN_TENSOR_NUM = 5;
static const int32_t OUT_TENSOR_NUM = 2;
```

5 个输入依次是 `qLayer`、`kLayer`、`cos`、`sin`、`seqLen`（这与 [rope_ops_runner.cpp:48-52](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_ops_runner.cpp#L48-L52) 的命名一一对应）。

**InferShapeImpl——形状透传**：RoPE 只旋转数值、不改变张量形状，所以两个输出分别等于两个对应输入的描述：

```cpp
// rope_operation.cpp:79-85
Status RopeOperation::InferShapeImpl(const SVector<TensorDesc> &inTensorDescs,
                                     SVector<TensorDesc> &outTensorDescs) const
{
    outTensorDescs.at(0) = inTensorDescs.at(0);   // qEmbedded = qLayer
    outTensorDescs.at(1) = inTensorDescs.at(1);   // kEmbedded = kLayer
    return NO_ERROR;
}
```

**ParamCheck——旋转向量校验**：`rotaryCoeff` 只能取 2、4 或「等于 cos 的第二维」（即 headDim/2 对应 interleave）；同时要求 cos/sin 的第二维能被 `rotaryCoeff` 整除：

```cpp
// rope_operation.cpp:115-125
if (param_.rotaryCoeff != ROTARY_COEFF_TWO && param_.rotaryCoeff != ROTARY_COEFF_FOUR &&
    param_.rotaryCoeff != inTensorDescs.at(PARAM_COS).shape.dims[1]) {
    // 只支持 rotaryCoeff 为 2、4 或 headDim
    return ERROR_INVALID_PARAM;
}
if (inTensorDescs.at(PARAM_COS).shape.dims[1] % param_.rotaryCoeff != 0 ||
    inTensorDescs.at(PARAM_SIN).shape.dims[1] % param_.rotaryCoeff != 0) {
    // cos/sin 的 dim[1] 必须能被 rotaryCoeff 整除
    return ERROR_INVALID_PARAM;
}
```

这正是为什么测试 CSV 里 `wrongParam1` 用 `{"rotaryCoeff": 3}` 期望得到 `ERROR_INVALID_PARAM`（[rope.csv 第 17 行](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/csv/rope.csv#L17)）。

**DimCheck——维度与隐层约束**：Q/K 既支持 2 维 `[ntoken, hiddenSize]`，也支持 4 维 `[s,b,headNum,headSize]`；cos/sin 必须是 2 维且形状一致；非 950 芯片还限制 headSize 在 \[16, 4096\] 之间。`HiddenSizeCheck` 进一步要求 `hiddenSizeQ` 是 `hiddenSizeK` 的整数倍、`hiddenSizeK` 是 `headDim` 的整数倍：

```cpp
// rope_operation.cpp:202-216
if (hiddenSizeQ % hiddenSizeK != 0) {
    // hiddenSizeQ 必须是 hiddenSizeK 的整数倍
    return ERROR_INVALID_TENSOR_SIZE;
}
int64_t headDim = inTensorDescs.at(PARAM_COS).shape.dims[1];
if (hiddenSizeK % headDim != 0) {
    // hiddenSizeK 必须是 headDim 的整数倍
    return ERROR_INVALID_TENSOR_DIM;
}
```

这条链路解释了 Param 文档注释里那句「`hiddenSizeQ` 必须是 `hiddenSizeK` 的整数倍且满足 `hiddenSizeQ = headDim * headNum`」。

**CreateRunner——双后端分派**：950（A3）芯片桥接 CANN aclnn 算子，其它芯片走自研 ops 后端并通过 `RunnerPool` 复用（u3-l5）：

```cpp
// rope_operation.cpp:220-238
std::shared_ptr<Runner> RopeOperation::CreateRunner(Context &context) const
{
    if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
        return std::make_shared<RopeAclnnRunner>(param_);
    }
    // ...非 950：从 RunnerPool 复用 RopeOpsRunner，失败则新建
    int64_t runnerTypeIdx = RunnerTypeRegister::GetRunnerTypeIdx("RopeOpsRunner");
    RunnerPool &pool = contextBase->GetRunnerPool(runnerTypeIdx);
    Runner *runner = pool.MallocRunner<RopeOpsRunner, infer::RopeParam>(param_);
    ...
}
```

**ops 后端组图**：`RopeOpsRunner` 把算子表达为一张只含单个节点的 `KernelGraph`，节点名 `"RopeOperation"` 即是 MKI 注册名（u3-l4 讲过的衔接点）；`RopeKqView` 把 4 维 Q/K「压平」成 2 维供 Kernel 使用；`mkiInferShapePreFunc` 把第 5 个输入 seqLen 的 dtype 设成 UINT32：

```cpp
// rope_ops_runner.cpp:58-70
kernelGraph_.nodes.resize(1);
auto &ropeNode = kernelGraph_.nodes.at(0);
AtbOps::OpParam::Rope ropeParam;
ropeParam.rotaryCoeff = param_.rotaryCoeff;
ropeParam.cosFormat = param_.cosFormat;
ropeNode.opDesc = {0, "RopeOperation", ropeParam};
ropeNode.inTensors = {&qLayer, &kLayer, &cos, &sin, &seqLen};
ropeNode.outTensors = {&qEmbedded, &kEmbedded};
ropeNode.inTensorViewFuncs = {&RopeKqView, &RopeKqView};
ropeNode.mkiInferShapePreFunc = [](Mki::LaunchParam &launchParam) {
    launchParam.GetInTensor(4).desc.dtype = Mki::TENSOR_DTYPE_UINT32;
};
```

**aclnn 后端——三种旋转模式的翻译**：950 路径把 `rotaryCoeff` 翻译成 CANN `aclnnApplyRotaryPosEmbV2` 能识别的 `rotaryMode` 字符串，并把 Q/K 的 2 维/4 维布局映射为 `TND`/`BSND`：

```cpp
// rope_aclnn_runner.cpp:204-216
std::string rotaryMode = "half";
if (param_.rotaryCoeff == ROTARY_COEFF_HALF) {        // 2
    rotaryMode = "half";
} else if (param_.rotaryCoeff == ROTARY_COEFF_QUARTER) { // 4
    rotaryMode = "quarter";
} else {
    rotaryMode = "interleave";
}
RotaryLayout layout = RotaryLayout::BSND;
if (...dimNum == ACLNN_TND_DIM_NUM) { layout = RotaryLayout::TND; }
```

随后调用 CANN 两段式接口（GetWorkspaceSize + Execute），与 u3-l3 讲的 aclnn 协议完全一致：

```cpp
// rope_aclnn_runner.cpp:218-227
aclnnStatus ret = RopeAclnnRunner::aclnnGetWorkspaceSizeFunc_(
    queryRef, keyRef, cos, sin,
    static_cast<int64_t>(layout),
    (char *)rotaryMode.c_str(),
    &(this->atbVariantPack_.workspaceBufferSize),
    &rawExecutorPtr);
```

> 这也解释了 `CreateOperation` 里 950 分支为何强校验 `cosFormat == 0`（[rope_operation.cpp:50-53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L50-L53)）：CANN 官方算子不接受非 0 的 cosFormat。

#### 4.1.4 代码实践

**实践目标**：用测试框架的 golden 函数理解 RoPE 的逐元素计算，再对照一个真实测试用例验证输入输出约定。

**操作步骤（源码阅读型实践）**：

1. 打开 [tests/apitest/opstest/python/operations/rope/test_rope_operation.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/rope/test_rope_operation.py)，阅读 `TestRopeOperation.golden_calc`（25-50 行）与 `rotate_half`（25-27 行）。
2. 对照 CSV 用例 `rope1`（[rope.csv 第 1 行](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/csv/rope.csv#L1)）：`{"rotaryCoeff": 4}`，Q/K 形状 `4,4096`，cos/sin 形状 `4,128`，seqLen 形状 `1`。
3. 用 golden 函数手算一个 token 的输出，确认 `q0 = q0*cos0 + rotate_half(q0)*sin0` 这个公式成立。

**需要观察的现象 / 预期结果**：

- golden 把 head 维 `chunk(2, -1)` 切成 `q0, q1` 两半，cos/sin 也各切两半，分别旋转后再 `concat` 回去——这正是 `rotaryCoeff=4`（quarter）的配对方式。
- `wrongParam1` 用 `rotaryCoeff=3` 期望 `ERROR_INVALID_PARAM`，对应 `ParamCheck` 的校验。
- `wrongDim5` 让 Q 的 `ntokens` 与 K 不一致（`4,4098` vs `4,4096`），期望 `ERROR_INVALID_TENSOR_SIZE`，对应 [rope_operation.cpp:153-160](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L153-L160) 的 `ntokens` 一致性检查。

若需实际运行：本仓库的 opstest 是 NPU 上执行的，本地无昇腾环境时**待本地验证**。但你可以用上面 golden 的 NumPy/PyTorch 逻辑在 CPU 上复现 RoPE 结果，与 ATB 推理结果对比。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RopeOperation` 的 `InferShapeImpl` 是「输出直接等于输入」，而 `SelfAttentionOperation` 的输出形状要重新计算合轴？

> **答案**：RoPE 只对每个位置的向量做线性旋转，不改变 head 数、headDim、token 数，所以形状不变；SelfAttention 会把多头的结果在最后一维合并成 `headNum×vHeadSize`，所以需要重新推导。

**练习 2**：一个 LLaMA2-7B 模型想用 ATB 的 RoPE，`rotaryCoeff` 该填多少？为什么？

> **答案**：填 `2`。LLaMA 系列采用 GPT-NeoX 风格的「半旋转」，对应 half 模式；CSV 里所有 `FromModel=LLaMA2-7B/13B` 的用例都是 `{"rotaryCoeff": 2}`。

**练习 3**：950 芯片上 `cosFormat` 为什么必须为 0？

> **答案**：950 走 `RopeAclnnRunner`，桥接的是 CANN 官方算子 `aclnnApplyRotaryPosEmbV2`，该算子不接受非 0 的 cosFormat，所以 `CreateOperation` 在 950 分支提前拦截并返回 `ERROR_INVALID_PARAM`（[rope_operation.cpp:50-53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L50-L53)）。

---

### 4.2 DynamicNTKOperation：长序列外推的位置表生成

#### 4.2.1 概念说明

`RopeOperation` 需要「预先算好」的 cos/sin 表。当推理序列长度超过训练长度时，cos/sin 里没有对应位置的频率值，模型外推效果变差。**NTK-aware Scaling** 的思路是：不去改位置索引，而是缩放旋转频率的 base，让高频分量保持、低频分量被「拉长」，从而让模型在更长序列上仍能有效 attend。

`DynamicNTKOperation` 就是「按当前序列长度动态生成 NTK 外推后的 cos/sin 表」的算子。它不直接旋转 Q/K，而是产出 cos、sin 两个表，再交给 `RopeOperation` 使用。

`DynamicNTKParam` 同样极简：

```cpp
// include/atb/infer_op_params.h:191-198
struct DynamicNTKParam {
    aclDataType outDataType = ACL_DT_UNDEFINED;  // 选择输出数据类型
    uint8_t rsv[12] = {0};
};
```

#### 4.2.2 核心流程

输入 3 个、输出 2 个：

```
inputs:  [positionIds, InvFreqIn, seqlens]   # 3 个
outputs: [cos, sin]                          # 2 个，dtype = outDataType
```

- `positionIds`（1 维）：每个 token 的位置编号。
- `InvFreqIn`（2 维 `[batch, head_dim/2]`）：逆频率向量 \(\theta_i\)，注意输入尺寸是 `head_dim/2`。
- `seqlens`（1 维）：每个 batch 的序列长度，用于判断是否触发 NTK 缩放。
- 输出 `cos`/`sin` 形状为 `[numPositions, head_dim]`（注意最后一维是 `InvFreqIn.dim1 * 2`，即完整的 head_dim）。

数学上，输出的 cos/sin 是位置与频率的外积：

\[
\text{cos}[m, 2i] = \text{cos}[m, 2i+1] = \cos(m \cdot \theta_i^{\text{NTK}})
\]

NTK 缩放会根据 `seqlens` 是否超过训练长度，对 \(\theta_i\) 的 base 做动态放大。

#### 4.2.3 源码精读

**输入输出个数与边界常量**：

```cpp
// dynamic_ntk_operation.cpp:20-27
static const uint32_t IN_TENSOR_SEQLENS = 2;
static const uint32_t OUT_TENSOR_NUM = 2;
static const uint32_t IN_TENSOR_NUM = 3;
static const uint32_t MAX_BATCH_SIZE = 16;
static const uint32_t MAX_HEAD_DIM = 2048;
static const uint32_t MAX_NUM_TOKENS = 256000;
```

**CreateOperation——输出类型约束**：`outDataType` 只能是 `ACL_FLOAT16` 或 `ACL_BF16`，且 bf16 仅 910B 支持：

```cpp
// dynamic_ntk_operation.cpp:36-43
if (opParam.outDataType != ACL_FLOAT16 && opParam.outDataType != ACL_BF16) {
    return ERROR_INVALID_PARAM;
}
if (opParam.outDataType == ACL_BF16 && !GetSingleton<Config>().Is910B()) {
    return ERROR_INVALID_PARAM;   // bf16 仅 Atlas 800I A2
}
```

这对应测试 CSV 里 `outputType=27`（bf16）的用例只在 `SocVersion=Ascend910B` 上运行，而 `outputType=2`（非 fp16/bf16）期望 `ERROR_INVALID_PARAM`。

**InferShapeImpl——输出形状推导**：输出第一维取 `positionIds` 的长度，第二维是 `InvFreqIn.dim1 * 2`（补全完整 head_dim）：

```cpp
// dynamic_ntk_operation.cpp:118-124
outTensorDescs.at(0).shape.dims[0] = inTensorDescs.at(0).shape.dims[0];        // positionIds 数量
outTensorDescs.at(0).shape.dims[1] = inTensorDescs.at(1).shape.dims[1] * 2;    // InvFreqIn.dim1 * 2
outTensorDescs.at(1) = outTensorDescs.at(0);   // sin 与 cos 同形状
```

**InferShapeDimCheck——容量边界**：batch 不超过 16、token 总数不超过 256000、headDim 不超过 2048 且必须 32 对齐：

```cpp
// dynamic_ntk_operation.cpp:78-93
int64_t ntokens = inTensorDescs.at(0).shape.dims[0];
if (ntokens > MAX_NUM_TOKENS) { ... }            // ≤ 256000
int64_t headDim = inTensorDescs.at(1).shape.dims[1] * 2;
if (headDim > MAX_HEAD_DIM) { ... }              // ≤ 2048
if (headDim % ALIGNMENT != 0) { ... }            // 32 对齐
```

**CreateRunner**：单一后端 `DynamicNTKOpsRunner`，无 aclnn 分流：

```cpp
// dynamic_ntk_operation.cpp:161-165
std::shared_ptr<Runner> DynamicNTKOperation::CreateRunner(Context &context) const
{
    (void)context;
    return std::make_shared<DynamicNTKOpsRunner>(param_);
}
```

> 小结：`DynamicNTK` 与 `Rope` 是「上下游」关系——前者生产 cos/sin，后者消费它们旋转 Q/K。这种「位置表生成」与「位置编码施加」分离的设计，让外推策略（NTK、YaRN 等）可以独立替换而不影响 RoPE 主体。

#### 4.2.4 代码实践

**实践目标**：通过 CSV 用例理解 `DynamicNTKOperation` 的输入输出形状与数据类型约束。

**操作步骤（源码阅读型实践）**：

1. 打开 [tests/apitest/opstest/csv/dynamic_ntk.csv](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/csv/dynamic_ntk.csv)。
2. 对照 `CumsumBaseCase1`（第 1 行）：输入 `[128; 1,32; 1]`，输出 `[128,64; 128,64]`，`outputType=27`(bf16)，SocVersion=Ascend910B。
3. 验证：`InvFreqIn.dim1=32`，则输出第二维 `= 32*2 = 64`，与 `OutShape` 的 `128,64` 吻合。
4. 观察 `CumsumWrongCase5`（第 11 行）：输入 `seqlens` 形状 `1,32;1` 写成 `2,32;1`（batch 与 positionIds 第一维不一致），期望 `ERROR_INVALID_TENSOR_DIM`，对应 [dynamic_ntk_operation.cpp:72-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp#L72-L77) 的 batch 一致性校验。

**需要观察的现象 / 预期结果**：

- 输出形状的第二维恒等于 `InvFreqIn 第二维 × 2`。
- `outputType` 不传或传非法值（如 `2`）会触发 `ERROR_INVALID_PARAM`。
- bf16（27）只在 910B 通过，其它 SocVersion 期望失败。

**待本地验证**：以上为依据源码与 CSV 的推断，实际运行需昇腾 NPU 环境。

#### 4.2.5 小练习与答案

**练习 1**：`InvFreqIn` 的第二维是 `head_dim/2`，为什么输出 cos 的第二维要乘 2？

> **答案**：RoPE 的旋转频率 \(\theta_i\) 只需 `head_dim/2` 个（每个频率对应一个 2 维旋转子空间），但 cos/sin 表要与完整的 head_dim 对齐逐元素相乘，所以每个 \(\theta_i\) 对应的 cos/sin 值要在相邻两维重复，最终表长 `head_dim = InvFreqIn.dim1 * 2`。

**练习 2**：为什么 `DynamicNTK` 和 `Rope` 要拆成两个算子，而不是合并？

> **答案**：分离关注点——位置表生成（受外推策略影响，随序列长度动态变化）与旋转施加（只依赖 cos/sin 与 Q/K）正交。拆开后，外推策略（NTK/YaRN/线性缩放）可独立替换，且 `Rope` 能复用同一套 Kernel；同时推理框架可以只算一次 cos/sin 再对多层复用。

---

### 4.3 NormRopeReshapeOperation：RMSNorm + RoPE + ReshapeAndCache 三合一融合

#### 4.3.1 概念说明

在大模型推理的每一层，K（以及 Q）通常要经历三步：先做 RMSNorm 归一化，再做 RoPE 旋转，最后把结果写入分页 KV Cache（ReshapeAndCache，参见 u4-l5）。如果按「单算子」实现，这三步是三个独立的 Kernel launch，中间结果要多次在 Device 显存与片上缓存之间搬移。

`NormRopeReshapeOperation` 把这三步**融合成一个 Kernel**：输入归一化后的特征、gamma、待旋转的 keyRope、cos、sin、slotMapping（写缓存寻址）、keycachein（缓存本体），直接输出写好旋转结果的 keycacheout。它仅支持 Atlas 800I A2（910B）推理产品。

`NormRopeReshapeParam`：

```cpp
// include/atb/infer_op_params.h:2703-2716
struct NormRopeReshapeParam {
    uint32_t precisionMode = 0;   // 精度模式
    uint32_t rotaryCoeff = 2;     // 算子内 Rope 部分的旋转系数
    float epsilon = 1e-5;         // 归一化时加在分母上防止除零
    uint8_t rsv[16] = {0};
};
```

#### 4.3.2 核心流程

输入 7 个、输出 1 个：

```
inputs:  [x, gamma, keyRope, cos, sin, slotMapping, keycachein]   # 7 个
outputs: [keycacheout]                                            # 1 个
```

执行逻辑（伪代码）：

```
# 1. RMSNorm：对 x 按最后一维归一化，乘 gamma
normed = x / rms(x + epsilon) * gamma        # rms = sqrt(mean(x^2) + epsilon)

# 2. RoPE：对 keyRope 做旋转
rotated = keyRope * cos + rotate(keyRope) * sin

# 3. ReshapeAndCache：把 (normed 的非 rope 部分 + rotated) 按 slotMapping 写入 keycachein
keycacheout = reshape_and_cache(keycachein, concat(normed, rotated), slotMapping)
```

> 注意第 7 个输入 `keycachein` 既是输入又是输出基底——`InferShapeImpl` 直接让 `out[0] = in[6]`（透传），实际是 in-place 写回缓存（与 u4-l5 讲的 `KvCacheOperation` 同样的 in-place 模式）。

#### 4.3.3 源码精读

**CreateOperation——epsilon 与芯片强校验**：epsilon 不能小于 float 最小正值（防除零），且只允许 910B：

```cpp
// norm_rope_reshape_operation.cpp:42-49
if (std::fabs(opParam.epsilon) < THRESHOLD) {
    ATB_LOG(ERROR) << "Invalid epsilon, it's recommended to init a nonzero value for eps.";
    return ERROR_INVALID_PARAM;
}
if (!GetSingleton<Config>().Is910B()) {
    ATB_LOG(ERROR) << "NormRopeReshapeOperation only supports Atlas 800I A2 inference products";
    return ERROR_INVALID_PARAM;
}
```

其中 `THRESHOLD = std::numeric_limits<float>::min()`（即约 `1.17549e-038`，[norm_rope_reshape_operation.cpp:30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape_operation.cpp#L30)）。这与 u4-l2 里 RMSNorm 的 epsilon 校验一致——epsilon 是归一化分母的防除零项。

**输入输出个数**：恒定 7 入 1 出：

```cpp
// norm_rope_reshape_operation.cpp:66-74
uint32_t NormRopeReshapeOperation::GetInputNum() const  { return IN_TENSOR_COUNT_SEVEN; }  // 7
uint32_t NormRopeReshapeOperation::GetOutputNum() const { return OUT_TENSOR_COUNT_ONE; }   // 1
```

**InferShapeImpl——输出透传第 7 个输入**：

```cpp
// norm_rope_reshape_operation.cpp:76-81
outTensorDescs.at(OUT_TENSOR_ZERO) = inTensorDescs.at(IN_TENSOR_SIX);   // out[0] = keycachein
```

**CheckDim910B——维度严格约束**：7 个输入的维度分别是 3/1/2/2/2/1/4（x/gamma/keyRope/cos/sin/slotMapping/keycachein）：

```cpp
// norm_rope_reshape_operation.cpp:124-130
if (inTensorDescs.at(0).shape.dimNum != 3 ||   // x: 3 维
    inTensorDescs.at(1).shape.dimNum != 1 ||   // gamma: 1 维
    inTensorDescs.at(2).shape.dimNum != 2 ||   // keyRope: 2 维
    inTensorDescs.at(3).shape.dimNum != 2 ||   // cos: 2 维
    inTensorDescs.at(4).shape.dimNum != 2 ||   // sin: 2 维
    inTensorDescs.at(5).shape.dimNum != 1 ||   // slotMapping: 1 维
    inTensorDescs.at(6).shape.dimNum != 4) {   // keycachein: 4 维
```

**关键形状关系**：`keycachein` 的第 4 维必须等于「x 的第 3 维 + keyRope 的第 2 维」——这正反映了「归一化部分 + 旋转部分」拼接后写入缓存的结构：

```cpp
// norm_rope_reshape_operation.cpp:168-174
if (keycacheInTensorDesc.shape.dims[DIM_THREE] !=
    xTensorDesc.shape.dims[DIM_TWO] + keyRopeTensorDesc.shape.dims[DIM_ONE]) {
    // keycachein 的第4维 == x 的第3维 + keyRope 的第2维
    return false;
}
```

**UB 容量校验**：因为融合 Kernel 把多个中间张量放在 Unified Buffer（UB）里，所以有一个 UB 容量上限校验（`11 * x.dim[2] * 2 + keycachein.dim[3] * 2 < 196352`）：

```cpp
// norm_rope_reshape_operation.cpp:85-90
if (ELEVEN * inTensorDescs.at(0).shape.dims[DIM_TWO] * FLOAT16SIZE +
    inTensorDescs.at(6).shape.dims[DIM_THREE] * FLOAT16SIZE >= MAXUBSIZE) {
    return ERROR_INVALID_TENSOR_DIM;
}
```

其中 `MAXUBSIZE = 196352` 是 910B AI Core 单核 UB 的字节容量。这条校验本身就在暗示：融合算子把中间结果尽量留在片上 UB，避免落回显存。

**CreateRunner——单一 ops 后端组图**：组一张只含 `RmsNormAndRopeAndReshapeAndCacheOperation` 节点的图，节点名直接说明它融合了哪三件事：

```cpp
// norm_rope_reshape_ops_runner.cpp:44-49
kernelGraph_.nodes.resize(1);
auto &normRopeReshapeNode = kernelGraph_.nodes.at(0);
normRopeReshapeNode.opDesc = {0, "RmsNormAndRopeAndReshapeAndCacheOperation", normRopeReshapeParam};
normRopeReshapeNode.inTensors = {&xTensor, &gammaTensor, &keyRopeTensor, &cosTensor, &sinTensor,
                                 &slotMappingTensor, &keycacheInTensor};
normRopeReshapeNode.outTensors = {&keycacheOutTensor};
```

#### 4.3.4 代码实践

**实践目标**：对比 `RopeOperation`（单算子）与 `NormRopeReshapeOperation`（融合算子），定位融合带来的性能优势来源。

**操作步骤（源码阅读型实践）**：

1. 先回顾「单算子组合」的三步 K 写缓存路径：`RmsNormOperation`（u4-l2）→ `RopeOperation`（本讲 4.1）→ `ReshapeAndCache`（u4-l5）。每一步都是独立的 `Operation`，各自有 Setup/Execute、各自的 KernelGraph 节点、各自从显存读写中间张量。
2. 打开 [norm_rope_reshape_ops_runner.cpp:44-49](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_ops_runner.cpp#L44-L49)，确认融合算子只有 **1 个 KernelGraph 节点**、**1 次 Execute**。
3. 阅读 UB 容量校验（[norm_rope_reshape_operation.cpp:85-90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L85-L90)），理解为什么融合算子要把中间张量留在片上 UB。
4. 阅读 `tests/apitest/kernelstest/mix/test_rms_norm_and_rope_and_reshape_and_cache.py`（可在仓库中搜索定位），对比其与 `test_rope.py` 的 golden 实现，体会融合后中间结果不再单独可见。

**需要观察的现象 / 预期结果**（这是本讲的核心实践，要求你回答「融合优势来源」）：

- **减少 Kernel launch**：单算子三步 = 3 次算子 Execute + 3 次 KernelGraph 下发；融合后 = 1 次。每次 launch 都有 Host→Device 的下发开销（u1-l1 讲的 Host Bound），融合直接把这部分摊薄到 1/3。
- **中间张量不落显存**：单算子组合里，RMSNorm 的输出、RoPE 的输出要写回 Device 显存再被下一步读入；融合算子把它们留在 AI Core 的 UB/寄存器里，省掉「写显存 + 读显存」两轮带宽（UB 容量校验正是为此而设）。
- **减少 workspace 与中间显存占用**：融合后不需要为中间张量分配独立的 Device 显存。

**待本地验证**：若需量化收益，可在 910B 上用 `prof` 工具（见 u7-l2）分别测三步组合与融合算子的单层耗时对比。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `NormRopeReshapeOperation` 的输出形状是「透传第 7 个输入」而不是像 `RopeOperation` 那样透传前两个输入？

> **答案**：融合算子的真正「结果」是写回 KV Cache（keycachein），它是 in-place 更新，所以输出就是缓存本体 `in[6]`；而 `RopeOperation` 的输出是两个独立的旋转后张量，透传 Q/K。两者输出语义不同。

**练习 2**：融合算子里的 `rotaryCoeff` 默认是 2，单算子 `RopeParam` 默认是 4，这种默认值差异说明什么？

> **答案**：融合算子面向的是「RMSNorm + RoPE + 写 Cache」的解码路径，常见于采用 half 旋转的 LLaMA 类模型，故默认 2；单算子 `RopeOperation` 要兼容更多模型（含 quarter 模式的 ChatGLM 等），默认 4 更保守。两者都是可配的，默认值只是各自高频场景的取舍。

**练习 3**：`NormRopeReshapeOperation` 为什么只支持 910B？

> **答案**：融合 Kernel 高度依赖 910B AI Core 的 UB 容量（196352 字节）与 Cube/Vector 协同能力，把 RMSNorm、RoPE、ReshapeAndCache 三段流水塞进单核片上存储；其它芯片的 UB 容量或指令组合不足以支撑这种融合，所以 `CreateOperation` 直接拦截非 910B（[norm_rope_reshape_operation.cpp:46-49](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L46-L49)）。

## 5. 综合实践

**任务**：画一张「大模型单层 K 通路」的算子编排图，分别给出「单算子组合方案」与「融合算子方案」，并标注每条边的张量与每次 Kernel launch。

**步骤**：

1. **单算子组合方案**：按顺序串联 `DynamicNTKOperation`（生成 cos/sin）→ `RmsNormOperation`（u4-l2，归一化 x）→ `RopeOperation`（旋转 keyRope）→ `ReshapeAndCache`（u4-l5，写 keycachein）。标出每个算子的输入输出张量、形状，统计共几次 Execute / 几次显存读写。
2. **融合算子方案**：用 `DynamicNTKOperation`（cos/sin 仍需单独生成）+ `NormRopeReshapeOperation`（一步完成 norm+rope+cache）。统计 Execute 次数与显存读写次数。
3. **对比结论**：写出融合方案在 launch 次数、中间显存读写、workspace 占用三方面的具体节省。
4. **适用性判断**：在你的图上标注——融合方案仅 910B 可用，单算子组合方案芯片覆盖更广（rope.csv 显示 910B/910A/310P 均可）。当目标芯片不是 910B 时，只能回退到单算子组合。

**预期产出**：一张含两种方案的对比图 + 一段不少于 3 条的性能优势说明（直接回答本讲实践任务：「融合算子相比单算子组合的性能优势来源」）。

> 提示：优势来源可从本讲 4.3.4 的三个结论（减少 launch、中间张量不落显存、减少 workspace）展开，并结合 UB 容量校验说明「融合之所以可行，是因为 910B 片上存储能容纳中间结果」。

## 6. 本讲小结

- **RoPE 的本质**是按位置旋转 Q/K 的二维子空间，使注意力分数只依赖相对位置；`RopeOperation` 用一个 `rotaryCoeff` 旋钮（2/4/headDim）表达 half/quarter/interleave 三种配对方式，对应 aclnn 后端的 `"half"`/`"quarter"`/`"interleave"`。
- `RopeOperation` 恒为 **5 入 2 出**（qLayer/kLayer/cos/sin/seqLen → qEmbedded/kEmbedded），形状透传；校验链 `DimCheck → ParamCheck → HiddenSizeCheck` 卡死维度、旋转系数整除性与隐层倍数关系；950 走 `RopeAclnnRunner`，其它芯片走 `RopeOpsRunner`（RunnerPool 复用）。
- **`DynamicNTKOperation`** 是 `Rope` 的上游：3 入 2 出（positionIds/InvFreqIn/seqlens → cos/sin），按当前序列长度动态生成 NTK 外推的位置表，输出第二维 = `InvFreqIn.dim1 × 2`；把「位置表生成」与「旋转施加」解耦，便于独立替换外推策略。
- **`NormRopeReshapeOperation`** 是 910B 专属的三合一融合算子（RMSNorm + RoPE + ReshapeAndCache），7 入 1 出，输出 in-place 写回 keycachein；其核心收益是**减少 Kernel launch、中间张量留在片上 UB 不落显存、省去中间 workspace**，UB 容量校验（196352 字节）正是融合可行的硬件前提。
- 三个算子都沿用 `OperationBase → Runner → KernelGraph → Kernel` 的统一链路，差异仅在 Param、输入输出个数与 Runner 分派策略。
- 位置编码族体现了 ATB 一贯的设计哲学：**单 Param 覆盖多行为**（`rotaryCoeff`）+ **算子职责正交解耦**（生成 vs 施加）+ **关键路径融合**（NormRopeReshape）。

## 7. 下一步学习建议

- **横向看归一化**：回到 u4-l2（LayerNorm/RMSNorm），对比 `RmsNormOperation` 与本讲融合算子里的 RMSNorm 部分，理解「单算子 vs 融合」在 InferShape 与校验上的差异。
- **纵向看 KV Cache**：结合 u4-l5（PagedAttention 与 KV Cache），把 `NormRopeReshape` 里的 `slotMapping`、`keycachein` 与 `ReshapeAndCache` 算子串联，建立完整的「写缓存」心智。
- **深入 Kernel**：若想理解融合 Kernel 内部如何把三段流水塞进一个 AscendC Kernel，可进入 `src/kernels` 下 `RmsNormAndRopeAndReshapeAndCache` 的四件套（u3-l4），阅读其 Tiling 与 CopyIn/Compute/CopyOut。
- **MLA 衔接**：下一讲 u4-l7 讲 MLA（多头潜在注意力），DeepSeek 风格的 MLA 对 RoPE 有特殊处理（`mlaVHeadSize`、`qKaHeadNum` 等），本讲的 RoPE 基础是理解 MLA 预处理（`MlaPreprocess`）的前提。
- **性能验证**：学完 u7-l2（日志与 Profiling）后，回到本讲综合实践，用 `prof` 工具实测融合算子与单算子组合的耗时差异，量化 launch 与显存带宽的节省。
