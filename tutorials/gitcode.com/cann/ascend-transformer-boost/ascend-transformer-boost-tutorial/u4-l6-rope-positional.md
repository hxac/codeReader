# RoPE 与位置编码算子

## 1. 本讲目标

Transformer 的自注意力本身是「顺序无关」的——把一句话的词打乱顺序，注意力算出来的结果几乎一样。要让模型知道词与词的先后位置，必须额外注入位置信息。本讲聚焦 ATB 中负责「旋转位置编码（Rotary Position Embedding，RoPE）」的算子族，学完后你应当能够：

- 说清 RoPE 的数学直觉：为什么不给模型加一个「位置向量」，而是把位置信息编进 Q/K 的「旋转角度」里。
- 读懂 `RopeOperation` 的参数、校验逻辑与「按芯片选 Runner」的派发树。
- 理解 `DynamicNTK` 如何在推理序列超过训练长度时动态计算 cos/sin 表，实现长序列外推。
- 解释 `NormRopeReshape` 这一类融合算子把 RMSNorm + RoPE + 写 KV Cache 三件事压进一个 Kernel 的收益来源。

本讲承接 [u3-l1 OperationBase 框架基类]：所有算子都继承 `OperationBase`，只重写 `InferShapeImpl` 与 `CreateRunner` 两个钩子，校验外置于 `atb_ops_info.ini`。也用到了 [u4-l2 归一化算子] 中 RMSNorm 的 epsilon 校验、[u4-l5 KV Cache] 中 slotMapping/ReshapeAndCache 的概念。

## 2. 前置知识

在读源码前，先用最朴素的方式建立三个直觉。

**(1) 为什么是「旋转」位置编码？**

早期方案（如 BERT 的绝对位置编码）是给每个位置加一段学到的或用正弦公式算出的向量。RoPE 换了个思路：它直接把位置 \(m\) 编码成对 Q/K 向量的一个**旋转角度**，位置越靠后旋转角度越大。这样两个 token 的注意力点积 \(q_m^\top k_n\) 只依赖**相对距离** \(m-n\)，天然具备平移不变性，对长序列更友好。

**(2) 旋转是怎么落到多维向量上的？**

把一个 head 维度 \(d\) 的向量看成 \(d/2\) 个二维平面，每个平面做一次角度为 \(m\theta_i\) 的旋转，不同平面用不同频率 \(\theta_i\)。频率 \(\theta_i\) 由一个 base（通常 10000）决定：

\[
\theta_i = \mathrm{base}^{-2(i-1)/d}, \quad i = 1, 2, \dots, d/2
\]

\[
\mathrm{cos}_{m,i} = \cos(m\theta_i), \quad \mathrm{sin}_{m,i} = \sin(m\theta_i)
\]

cos/sin 这两张表就是 ATB `Rope` 算子的两个输入。所谓「旋转系数 `rotaryCoeff`」控制的是**这些二维平面如何分组**——`rotaryCoeff=2` 是最常见的「对半旋转（half）」，`4` 是四等分（quarter），取 `headDim/2` 时是「交错（interleave）」。后续源码会精确对应到这三种模式。

**(3) 半旋转（half）的实数公式。**

设 head 向量 \(x = [x_1,\dots,x_d]\)，前半 \(x^{(1)} = x_{1:d/2}\)、后半 \(x^{(2)} = x_{d/2+1:d}\)，则 half 旋转的输出为：

\[
\begin{aligned}
\mathrm{out}^{(1)}_i &= x^{(1)}_i \cos_{m,i} - x^{(2)}_i \sin_{m,i} \\
\mathrm{out}^{(2)}_i &= x^{(2)}_i \cos_{m,i} + x^{(1)}_i \sin_{m,i}
\end{aligned}
\]

记住这个「乘 cos、交叉乘 sin」的结构，它在源码 Kernel 里就是反复出现的计算核心。

**(4) NTK 外推是什么？**

当推理序列长度超过训练时见过的最大长度，直接套用上面的 \(\theta_i\) 会让旋转角度过大、模型崩坏。NTK-aware 的做法是不线性缩放位置，而是**改写 base**，让高频部分几乎不变、低频部分被平滑拉伸，从而外推到更长序列。`DynamicNTK` 算子就是按当前实际序列长度动态算出这套新 cos/sin 表。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `src/ops/ops_infer/` 下）：

| 文件 | 作用 |
|------|------|
| `rope/rope_operation.cpp` / `.h` | RoPE 主算子：参数校验、形状推导、按芯片派发 Runner |
| `rope/rope_ops_runner.cpp` | 非 950 芯片走 `OpsRunner`，组 1 个 `RopeOperation` Kernel 节点 |
| `rope/rope_aclnn_runner.cpp` | 950 芯片走 aclnn，桥接 CANN 的 `aclnnApplyRotaryPosEmbV2` |
| `dynamic_ntk/dynamic_ntk_operation.cpp` | 动态 NTK：按实际序列长算 sin/cos 表，用于长序列外推 |
| `dynamic_ntk/dynamic_ntk_ops_runner.cpp` | 组 1 个 `DynamicNTKOperation` Kernel 节点 |
| `norm_rope_reshape/norm_rope_reshape_operation.cpp` | 融合算子：RMSNorm + RoPE + ReshapeAndCache（仅 910B） |
| `norm_rope_reshape/norm_rope_reshape_ops_runner.cpp` | 组 1 个 `RmsNormAndRopeAndReshapeAndCacheOperation` 融合节点 |
| `include/atb/infer_op_params.h` | 三个 Param 结构定义 |

此外，融合算子的真实 Kernel 在 Kernel 层 `src/kernels/mixkernels/rms_norm_and_rope_and_reshape_and_cache/`，RoPE 单算子 Kernel 在 `src/kernels/mixkernels/rope/`，这是 u3-l4 讲过的「四件套」落点。

## 4. 核心概念与源码讲解

### 4.1 RoPE 旋转位置编码：原理与 RopeOperation

#### 4.1.1 概念说明

`RopeOperation` 是 ATB 对标准 RoPE 的实现。它的任务是：给定 query（x1）、key（x2），以及预先算好的 cos（x3）、sin（x4）两张角度表，对 x1、x2 做位置旋转，输出旋转后的 queryEmbedded、keyEmbedded。这两份「带位置信息的 Q/K」再喂给 SelfAttention，注意力点积就自动带上了相对位置。

注意一个设计要点：RoPE **只旋转 Q 和 K，不旋转 V**。因为位置信息只需要进入 \(q^\top k\) 的点积，V 不参与位置匹配。

#### 4.1.2 核心流程

`RopeOperation` 的执行链路完全沿用 [u3-l1] 的两段式骨架：

1. **创建**：`CreateOperation<RopeParam>` 先做 `OP_PARAM_RSV_CHECK` 版本闸门校验，950 芯片还会预加载 aclnn 符号并强制 `cosFormat==0`。
2. **Setup（Host）**：`InferShapeCheckImpl` → `DimCheck`（维度）→ `ParamCheck`（rotaryCoeff、cosFormat 合法性）→ `HiddenSizeCheck`（Q/K/cos 维度整除关系）。
3. **形状推导**：`InferShapeImpl` 最朴素——`out[0]=in[0]`、`out[1]=in[1]`，旋转不改变形状。
4. **CreateRunner（首次 Setup 时延迟创建并复用）**：950 → `RopeAclnnRunner`；其它芯片 → 从 `RunnerPool` 复用 `RopeOpsRunner`。
5. **Execute（Device）**：Runner 内部把算子表达为 KernelGraph 的 1 个节点并下发。

#### 4.1.3 源码精读

**Param 定义**——只有两个字段加 `rsv` 版本闸门（u2-l3 讲过 `rsv` 必须全 0）：

[RopeParam 定义 — include/atb/infer_op_params.h:1608-1617](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1608-L1617) — `rotaryCoeff` 默认 4、`cosFormat` 默认 0、`rsv[8]` 预留。

**输入输出个数**——固定 5 入 2 出，这是 VariantPack 装填的依据：

[输入输出个数 — rope_operation.cpp:69-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L69-L77) — `IN_TENSOR_NUM=5`、`OUT_TENSOR_NUM=2`。对应 5 个输入依次是 query、key、cos、sin、seqLen；2 个输出是旋转后的 query、key。

**形状推导**——纯透传，旋转不改变张量形状：

[InferShapeImpl — rope_operation.cpp:79-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L79-L85) — `out[0]=in[0]`、`out[1]=in[1]`。

**参数校验**——`rotaryCoeff` 只允许取 2、4 或 `cos.dims[1]`（即 headDim/2），且 cos/sin 最后一维必须能被 `rotaryCoeff` 整除；`cosFormat` 只能是 0 或 1：

[ParamCheck — rope_operation.cpp:113-132](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L113-L132) — 三选一的 rotaryCoeff 校验 + 整除校验 + cosFormat 校验。

**维度校验**——query/key 支持 2 维（`[ntoken, hiddenSize]`）或 4 维（`[B,S,*,*]`），cos/sin 恒为 2 维，seqLen 恒为 1 维；非 950 芯片要求 head 维度落在 `[16, 4096]`：

[DimCheck — rope_operation.cpp:134-188](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L134-L188) — 维度数约束、Q/K 的 ntokens 必须一致、cos/sin 形状必须相同、head_size 上下界。

**Runner 派发树**——这是本算子最关键的决策点，950 与非 950 走两条完全不同的后端（与 u4-l1 的 Linear 同构）：

[CreateRunner — rope_operation.cpp:220-238](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_operation.cpp#L220-L238) — 950 直接 `make_shared<RopeAclnnRunner>`；其它芯片经 `RunnerPool` 复用 `RopeOpsRunner`，池分配失败再降级到新建。

**非 950 后端：组 1 个 Kernel 节点。** `RopeOpsRunner::SetupKernelGraph` 把算子建成单节点的 KernelGraph，节点 opDesc 字符串 `"RopeOperation"` 正是 u3-l4 讲过的「注册衔接点」（REG_OPERATION 名 = opDesc 字符串）。注意 `mkiInferShapePreFunc` 把第 5 个输入 seqLen 的 dtype 强制设为 `UINT32`：

[SetupKernelGraph — rope_ops_runner.cpp:40-73](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_ops_runner.cpp#L40-L73) — 单节点组图，inTensors={q,k,cos,sin,seqLen}，`RopeKqView` 把 4 维 Q/K 折叠成 2 维视图。

**950 后端：桥接 aclnn。** `RopeAclnnRunner` 把 ATB 的 `rotaryCoeff` 映射成 aclnn 的字符串模式，把 layout 映射成枚举，这是理解「rotaryCoeff 到底对应哪种旋转」的最直接证据：

[rotaryMode 映射 — rope_aclnn_runner.cpp:204-216](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_aclnn_runner.cpp#L204-L216) — `rotaryCoeff==2 → "half"`、`==4 → "quarter"`、其它（即 `headDim/2`）→ `"interleave"`；4 维输入 → BSND，2 维输入 → TND。

最终调用的是 CANN 的 `aclnnApplyRotaryPosEmbV2`，符号在 `LoadMethod` 里按名加载：

[LoadMethod — rope_aclnn_runner.cpp:57-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rope/rope_aclnn_runner.cpp#L57-L68) — 加载 `aclnnApplyRotaryPosEmbV2GetWorkspaceSize` 与 `aclnnApplyRotaryPosEmbV2` 两个符号。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「rotaryCoeff 三种取值分别对应 half/quarter/interleave」，并理解 2 维输入被 Reshape 成 3 维的过程。

**操作步骤**：

1. 打开 `src/ops/ops_infer/rope/rope_aclnn_runner.cpp`，定位 `SetAclNNWorkspaceExecutor` 中的 `rotaryMode` 分支（约 204–211 行）。
2. 对照 `rope_operation.cpp` 的 `ParamCheck`（113–132 行）：确认 `rotaryCoeff` 允许的三种取值 2、4、`cos.dims[1]`。
3. 看 `BuildAclnnVariantPack` 中 `isBSND4D==false` 的分支（约 88–97 行）：query 从 `[ntoken, hiddenSize]` 被改写成 `[ntoken, headNum, headDim]`，即把 hiddenSize 拆成 headNum × headDim。这就是 ATB「2 维输入」与 aclnn「TND 3 维」之间的桥接。

**需要观察的现象**：

- `rotaryMode` 的取值完全由 `param_.rotaryCoeff` 决定，与输入张量形状无关。
- query/key 的 Reshape 只改 `desc.shape`，不改 `deviceData` 指针——这是 u3-l3 讲过的「描述改写、零拷贝」手法。

**预期结果**：你能用自己的话说出「`rotaryCoeff=2` 即最常见的对半旋转，等价于 aclnn 的 half 模式」。

**待本地验证**：在有 NPU 的环境上，分别用 `rotaryCoeff=2` 和 `=4` 调用 Rope 算子，对比输出的数值差异（仅当本地具备昇腾环境时）。

#### 4.1.5 小练习与答案

**练习 1**：如果调用方把 `cosFormat` 设成 2，会在哪一步报错？为什么 950 芯片更严？

答案：会在 `CreateOperation`（950，50–53 行）或 `ParamCheck`（非 950，126–129 行）返回 `ERROR_INVALID_PARAM`。950 在建对象前就强制 `cosFormat==0`，因为 aclnn 后端 `aclnnApplyRotaryPosEmbV2` 只接受单一 cos 排布。

**练习 2**：为什么 `RopeOperation` 的 `InferShapeImpl` 只是 `out=in` 透传？

答案：RoPE 是逐元素旋转，对每个元素做「乘 cos、交叉乘 sin」，不改变张量的形状与数据类型，因此输出与输入的 TensorDesc 完全一致。

**练习 3**：`CreateRunner` 里非 950 分支为什么要 `dynamic_cast<ContextBase*>` 并访问 `RunnerPool`？

答案：为了复用已构造的 `RopeOpsRunner` 对象（含其 KernelGraph），把昂贵的组图开销摊薄为「只换参数」。`MallocRunner` 失败时才降级 `make_shared` 新建，这是 u3-l5 讲过的 RunnerPool 复用机制。

### 4.2 DynamicNTK：长序列动态外推

#### 4.2.1 概念说明

`DynamicNTKOperation` 解决的是「推理时序列比训练时更长」的外推问题。它的产出**不是**旋转后的 Q/K，而是 **sin/cos 两张角度表本身**——按当前 batch 的实际序列长度动态计算，再喂给 `Rope` 算子。

NTK-aware 的核心思想是改写旋转频率的 base。当实际长度 \(L\) 超过训练长度 \(L_{\text{train}}\) 时，记缩放比相关项，新的等效 base 大致为：

\[
\mathrm{base}' = \mathrm{base} \cdot \left(\mathrm{scale} \cdot \ln\!\left(\frac{L}{L_{\text{train}}}\right) + 1\right)^{d/(d-2)}
\]

效果是：高频维度（决定局部精细位置）几乎不变，低频维度（决定长程相对位置）被拉伸，从而平滑外推。`DynamicNTK` 把这套按 batch 内 `seqlens` 动态计算的 cos/sin 直接算出来，省去调用方手算。

#### 4.2.2 核心流程

1. **输入**：`positionIds`（每个 token 的位置序号，1 维）、`InvFreqIn`（基础逆频率 \(\theta_i\)，形状 `[batch, headDim/2]`）、`seqlens`（每个 batch 的实际长度，1 维）。
2. **计算**：按 `seqlens` 决定是否触发 NTK 缩放，结合 `positionIds` 与 `InvFreqIn` 算出每个 token 的角度，再取 sin/cos。
3. **输出**：`sin`、`cos`，形状 `[ntokens, headDim]`（注意是完整 headDim，由 `InvFreqIn.dims[1] * 2` 还原）。
4. **下游**：这两张表作为 `Rope` 算子的 cos/sin 输入。

#### 4.2.3 源码精读

**Param 与创建校验**——只有一个 `outDataType`（必须 fp16 或 bf16，bf16 仅 910B）：

[CreateOperation — dynamic_ntk_operation.cpp:30-50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp#L30-L50) — `outDataType` 合法性校验 + rsv 闸门。

**形状推导**——输出第 0 维取 `positionIds` 的 token 数，第 1 维是 `InvFreqIn.dims[1] * 2`（半频率还原成完整 headDim）：

[InferShapeImpl — dynamic_ntk_operation.cpp:115-125](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp#L115-L125) — 输出 `[ntokens, headDim]`，dtype/format 由 `outDataType` 与 `ACL_FORMAT_ND` 决定。

**容量约束**——batch ≤ 16、ntokens ≤ 256000、headDim ≤ 2048 且必须 32 对齐：

[InferShapeDimCheck — dynamic_ntk_operation.cpp:70-95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_operation.cpp#L70-L95) — 四项硬上界与 32 对齐校验。

**组图**——同样是单节点 KernelGraph，opDesc 字符串 `"DynamicNTKOperation"`，`outType` 字段把 fp16/bf16 编码成 0/1：

[DynamicNTKOpsRunner 构造与组图 — dynamic_ntk_ops_runner.cpp:21-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/dynamic_ntk/dynamic_ntk_ops_runner.cpp#L21-L46) — 单节点 `DynamicNTKOperation`，`asdopsParam_.outType = outDataType==ACL_FLOAT16 ? 0 : 1`。

#### 4.2.4 代码实践

**实践目标**：理清「DynamicNTK 产出 cos/sin → 喂给 Rope」这条调用链的形状衔接。

**操作步骤**：

1. 读 `InferShapeImpl`（115–125 行），确认输出第 1 维 = `InvFreqIn.dims[1] * 2`。
2. 回顾 `RopeOperation` 的 `DimCheck`（rope_operation.cpp 178–185 行）：cos/sin 形状必须相同，且与 query/key 的 head 维度匹配。
3. 画出数据流：`positionIds + InvFreqIn + seqlens → DynamicNTK → (sin, cos) → Rope(cos,sin 输入)`。

**预期结果**：你能解释为什么 DynamicNTK 的输出第 1 维要 ×2——因为它把 `headDim/2` 个基础频率还原成完整 headDim 维度的 cos/sin 表，供下游使用。

**待本地验证**：在不同 `seqlens` 下对比 DynamicNTK 输出的 cos 表数值变化，观察 NTK 缩放触发点。

#### 4.2.5 小练习与答案

**练习 1**：DynamicNTK 与 Rope 是替代关系还是配合关系？

答案：配合关系。DynamicNTK 负责「按实际长度动态算 cos/sin 表」，Rope 负责「用 cos/sin 表旋转 Q/K」。前者是后者的输入预处理。

**练习 2**：为什么 headDim 必须 32 对齐？

答案：NPU 上 Cube/Vector 访存按 32 字节（fp16 即 16 个元素）对齐效率最高，Kernel 实现按 32 对齐切分 Tiling，因此强制 headDim 是 32 的倍数（见 `InferShapeDimCheck` 90–93 行）。

### 4.3 NormRopeReshape：RMSNorm + RoPE + ReshapeAndCache 融合算子

#### 4.3.1 概念说明

`NormRopeReshapeOperation` 是一个**融合算子**：它把 decode 阶段三个连续步骤——RMSNorm 归一化、RoPE 旋转、把结果写入分页 KV Cache（ReshapeAndCache，u4-l5 讲过的 slotMapping 寻址）——压进**一个 Kernel**。注意它只处理 Key 通路（输出 `keycacheout`），且**仅 Atlas 800I A2（910B）支持**。

为什么值得融合？这是本讲的核心实践议题，先给结论再在 4.3.4 展开：单独三个算子会让中间结果在 HBM（显存）与片上之间来回搬运、且要 launch 三次 Kernel；融合后中间数据留在片上 UB，只 launch 一次，既省访存又省 Host 下发开销。

#### 4.3.2 核心流程

1. **创建校验**：`OP_PARAM_RSV_CHECK` + epsilon 非零校验（沿用 u4-l2 RMSNorm 的防除零逻辑）+ 强制 910B。
2. **输入**：7 个——`x`（待归一化的隐藏态）、`gamma`（RMSNorm 缩放）、`keyRope`（待旋转的 key）、`cos`、`sin`、`slotMapping`（写 cache 的槽位映射）、`keycachein`（分页 cache 输入）。
3. **形状推导**：`out[0] = in[6]`（输出形状跟 keycachein 一致，因为是 in-place 写回 cache）。
4. **Runner**：组 1 个名为 `RmsNormAndRopeAndReshapeAndCacheOperation` 的融合节点，对应 Kernel 层 `src/kernels/mixkernels/rms_norm_and_rope_and_reshape_and_cache/`。

#### 4.3.3 源码精读

**Param 定义**——`precisionMode`、`rotaryCoeff`（默认 2，half）、`epsilon`（默认 1e-5）、`rsv[16]`：

[NormRopeReshapeParam — include/atb/infer_op_params.h:2703-2716](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2703-L2716) — 融合了 RMSNorm 的 epsilon 与 RoPE 的 rotaryCoeff。

**创建校验**——epsilon 绝对值不能小于 float 最小正值（防除零），且强制 910B：

[CreateOperation — norm_rope_reshape_operation.cpp:36-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L36-L56) — `fabs(epsilon) < THRESHOLD` 拒建 + `Is910B()` 限制。

**输入输出个数**——7 入 1 出：

[GetInputNum/GetOutputNum — norm_rope_reshape_operation.cpp:66-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L66-L74) — `IN_TENSOR_COUNT_SEVEN=7`、`OUT_TENSOR_COUNT_ONE=1`。

**形状推导**——输出等于第 7 个输入（keycachein），因为融合算子把归一化+旋转后的结果 in-place 写回 cache：

[InferShapeImpl — norm_rope_reshape_operation.cpp:76-81](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L76-L81) — `out[0] = in[6]`。

**UB 容量约束**——这是一个很有意思的校验：`11 * x.dims[2] * 2 + keycachein.dims[3] * 2 < 196352`。196352 是 910B 的 UB（Unified Buffer，片上缓存）字节上限。它说明融合 Kernel 把 x 的多份中间量与 keycache 一起搬进 UB 计算，必须装得下：

[InferShapeCheckImpl UB 约束 — norm_rope_reshape_operation.cpp:83-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_operation.cpp#L83-L98) — UB 容量公式 + 维度与对齐校验。

**组融合节点**——Runner 把全部 7 入 1 出接到一个 Kernel 节点，opDesc 字符串 `"RmsNormAndRopeAndReshapeAndCacheOperation"`：

[BuildNormRopeReshapeGraph — norm_rope_reshape_ops_runner.cpp:27-50](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/norm_rope_reshape/norm_rope_reshape_ops_runner.cpp#L27-L50) — 单节点融合图，7 个 inTensor 接到同一节点。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：对比 `Rope`（单算子）与 `NormRopeReshape`（融合算子），说清融合的性能优势来源。

**操作步骤**：

1. 假设 decode 阶段对 Key 通路要做「RMSNorm → RoPE → 写 KV Cache」三步。若用单算子组合，需要分别创建 `RmsNormOperation`、`RopeOperation`、`ReshapeAndCache`（u4-l5）三个 Operation。
2. 阅读单算子 `RopeOperation` 的 `CreateRunner`（rope_operation.cpp 220–238 行）：每个单算子各自组 1 个 Kernel 节点、各自 Setup/Execute，各自有 1 次 Kernel launch。
3. 阅读 `NormRopeReshapeOpsRunner::BuildNormRopeReshapeGraph`（27–50 行）：三个步骤被压成 1 个节点 `RmsNormAndRopeAndReshapeAndCacheOperation`。
4. 对照 UB 容量约束（83–98 行）：`11*x.dims[2]*2 + keycache.dims[3]*2 < 196352`，说明中间结果被留在片上 UB。

**需要观察的现象与预期结果**：

融合算子相比单算子组合的三大性能优势来源：

| 优势 | 单算子组合 | NormRopeReshape 融合 |
|------|-----------|----------------------|
| **访存往返** | RMSNorm 输出写回 HBM → RoPE 读回再写出 → 写 cache 再读写，中间结果多次往返 HBM | 中间数据留在片上 UB，省去 2 次 HBM 读写往返（见 UB 容量约束） |
| **Kernel launch** | 3 次 launch，每次都有 Host 下发开销 | 1 次 launch，缓解 Host Bound（u1-l1 讲过的核心命题） |
| **写 cache** | 需要单独的 ReshapeAndCache 算子 + slotMapping 寻址 | 旋转完直接按 slotMapping 写回 keycachein，零额外算子 |

简言之：**融合的收益 = 省访存 + 省 launch + 省算子**，三者的根因都是「把多个 Kernel 之间的中间数据从 HBM 搬运变成片上直传」。代价是适用范围收窄（仅 910B、维度需 16 对齐、UB 装得下）。

**待本地验证**：在 910B 上分别测单算子组合与融合算子的端到端 decode 耗时，量化收益。

#### 4.3.5 小练习与答案

**练习 1**：`NormRopeReshapeOperation` 为什么把 `out[0]` 设成 `in[6]`（keycachein）而不是 `in[0]`（x）？

答案：因为它的语义是「归一化 + 旋转后 in-place 写回分页 KV Cache」，输出就是被更新过的 cache 本身，形状与 keycachein 完全一致；x 只是被消费的输入，不作为输出透传。

**练习 2**：UB 容量约束（83–90 行）若被违反会怎样？为什么系数是 11？

答案：违反则返回 `ERROR_INVALID_TENSOR_DIM`，因为 Kernel 无法把所需的中间数据全部装入 UB。系数 11 反映 Kernel 内部对 x 的某一维度需要同时缓存多份中间量（如 x、归一化中间态、cos/sin 展开等）的具体实现，是经验性常数，精确含义需对照 Kernel 源码（待确认）。

## 5. 综合实践

把本讲三个算子串起来，画一张「带长序列外推的 decode Key 通路」数据流图，并回答：

1. 当推理序列超过训练长度时，`positionIds + InvFreqIn + seqlens` 经 `DynamicNTK` 产出新的 sin/cos。
2. 若部署在 910B 且追求极致性能，会用 `NormRopeReshape` 一次性完成 RMSNorm + RoPE + 写 cache；若在其它芯片或需要分别调试，则用 `RmsNormOperation` + `RopeOperation`（cos/sin 来自步骤 1）+ `ReshapeAndCache` 三件套。
3. 在图中标注每条边的张量形状（参考三个算子的 `InferShapeImpl`），并标出哪些数据流经 HBM、哪些留在片上 UB。

**进阶**：在 `atb_ops_info.ini` 中找到三个算子的输入输出 dtype/format 约束（`[RopeOperation]`、`[DynamicNTKOperation]`、`[NormRopeReshapeOperation]` 三段），核对它们与 `GetInputNum`/`GetOutputNum` 是否一致，体会 u3-l1 讲过的「IR 配置外置」机制。

## 6. 本讲小结

- RoPE 把位置 \(m\) 编码成对 Q/K 的旋转角度，使注意力点积天然依赖相对距离；cos/sin 两张表是旋转的角度查表，只旋转 Q/K 不旋转 V。
- `RopeOperation` 固定 5 入 2 出，`InferShapeImpl` 纯透传；`rotaryCoeff` ∈ {2, 4, headDim/2} 对应 aclnn 的 half/quarter/interleave，950 走 aclnn、其它芯片走 OpsRunner + RunnerPool 复用。
- `DynamicNTK` 不旋转 Q/K，而是按 batch 实际 `seqlens` 动态算出 NTK 外推后的 sin/cos 表（输出 `[ntokens, headDim]`），再喂给 `Rope`。
- `NormRopeReshape` 把 RMSNorm + RoPE + 写 KV Cache 融合成单 Kernel（仅 910B），收益来自省访存（中间数据留片上 UB）、省 launch、省算子三方面。
- 三个算子都遵循 u3-l1 的两段式骨架与「单节点 KernelGraph」组图方式，opDesc 字符串是衔接 Kernel 层注册名的关键。

## 7. 下一步学习建议

- **继续算子线**：下一讲 [u4-l7 MLA 多头潜在注意力] 会用到本讲的 RoPE 概念（DeepSeek 风格 MLA 对 RoPE 有特殊处理），建议接着读。
- **下沉 Kernel**：想看 RoPE 的旋转到底怎么在 AI Core 上算，去 `src/kernels/mixkernels/rope/` 读 AscendC Kernel 的 CopyIn/Compute/CopyOut 三段式（u3-l4 基础），重点看它如何用 TQue 双缓冲掩盖 cos/sin 查表的访存延迟。
- **融合算子范式**：`NormRopeReshape` 是 ATB「把 decode 通路常见三件套融成一个 Kernel」的典型，类似思路可看 `src/kernels/mixkernels/` 下其它融合 Kernel，为 u6 自定义算子开发积累范式。
