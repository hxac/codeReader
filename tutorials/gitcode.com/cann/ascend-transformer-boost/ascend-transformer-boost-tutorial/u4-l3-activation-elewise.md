# 激活与元素级算子

## 1. 本讲目标

激活函数（GELU/SiLU/ReLU 等）与元素级运算（加、乘、量化、类型转换）是 Transformer 里出现频率最高的两类「逐元素」操作——几乎每个 Block 里都要做几次。本讲带你读懂 ATB 中承载这两类操作的两个算子：`ActivationOperation` 与 `ElewiseOperation`。学完后你应当能够：

- 说清 `ActivationType` 枚举里每一项对应的数学公式，以及 `ActivationParam` 各字段的用途。
- 读懂 `ActivationOperation` 的校验、`InferShapeImpl`（尤其是 SwiGLU 正/反向的特殊形状推导）。
- **讲清 `ActivationOperation` 如何根据 `ActivationType` 选择不同的 Runner 与底层 kernel**——这是本讲的核心实践任务，涉及「Operation 层分派 → Runner 内部组图 → aclnn 适配」三个层次。
- 理解 `ElewiseParam` 用「一个 `ElewiseType` 枚举 + 嵌套子 Param」统一表达 20 种元素级运算的设计，并能根据 `elewiseType` 判断输入输出个数。
- 读懂 `ElewiseOperation` 的 `InferShapeImpl`，特别是二元运算的广播（broadcast）规则与量化/动态量化变体的形状推导。

本讲是 u4（关键 Transformer 算子精讲）的第三篇，承接 u3-l1 讲过的 `OperationBase` 框架基类，以及 u4-l2 讲过的「单 Param + 枚举字段组合表达多种行为」的设计哲学——你会看到激活与元素级算子把这套哲学用到了极致。

## 2. 前置知识

在进入源码前，先把「激活函数」和「元素级运算」的直觉建立起来。

**什么是激活函数？** 神经网络的线性层（矩阵乘）输出仍是线性的，若层层叠加，整个网络等价于一个线性模型，表达力有限。激活函数对每个元素做一个非线性变换，给网络注入非线性。Transformer 里最常用的几个：

- **ReLU**：最简单，负数置 0、正数不变。
- **GELU**：BERT、GPT 系列的标准激活，比 ReLU 更平滑。
- **Swish / SiLU**：LLaMA 等模型常用，形式为 \(x \cdot \sigma(x)\)。
- **SwiGLU**：PaLM、LLaMA-2 等用在 FFN 里的门控激活，把输入切两半再相乘。

**什么是元素级（elementwise）运算？** 对张量里「位置相同」的元素逐个做同一个运算，如逐元素加 \(a_i + b_i\)、逐元素乘 \(a_i \times b_i\)。它不改变张量形状（至多改变 dtype），是构造各种算式的基础积木。ATB 进一步把「类型转换」「量化」「逐元素乘标量」等也归入这一类，用一个 `ElewiseOperation` 统一承载。

**什么是广播（broadcast）？** 两个形状不同的张量做二元运算时，把其中「长度为 1」的维度虚拟地复制扩展，使双方形状对齐再运算。例如形状 `[2,3]` 与 `[1,3]` 相加，后者在第 0 维广播成 `[2,3]`。这与 NumPy / PyTorch 的广播语义一致，后面会在 `InferShapeCommon` 里看到它的代码落点。

如果你还不熟悉 u3-l1 的 `OperationBase`，请先回顾：算子不直接 launch kernel，而是由 `OperationBase` 的冻结骨架统一做校验、Tiling、workspace，子类只通过 `InferShapeImpl` / `CreateRunner` 等钩子插入自己的逻辑。本讲的两个算子正是这样做的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `include/atb/infer_op_params.h` | 定义 `ActivationType` 枚举、`ActivationParam`、`ElewiseParam`（含 `ElewiseType` 枚举与 `QuantParam`/`MulsParam` 子结构） |
| `src/ops/ops_infer/activation/activation_operation.h` | `ActivationOperation` 类声明，列出它重写的钩子 |
| `src/ops/ops_infer/activation/activation_operation.cpp` | `ActivationOperation` 全部实现：工厂函数、IR 映射、校验、`InferShapeImpl`、`CreateRunner` |
| `src/ops/ops_infer/activation/activation_ops_runner.cpp` | `ActivationOpsRunner`：非 950 芯片的 Runner，用单节点 `ActivationOperation` 组装 KernelGraph |
| `src/atb/utils/runner_util.cpp` | `GetActivationNodeOpDesc`：把 `ActivationType` 映射到 KernelGraph 节点的 opDesc |
| `src/ops/ops_infer/activation/activation_aclnn_runner.cpp` | `ActivationAclnnRunner`：950 芯片的 aclnn 适配，按 `ActivationType` 选择不同 CANN 算子 |
| `src/ops/ops_infer/elewise/elewise_operation.cpp` | `ElewiseOperation` 全部实现：工厂函数、IR 映射、输入输出个数、`InferShapeImpl`、`CreateRunner` |
| `src/ops/ops_infer/elewise/elewise_ops_runner.cpp` | `ElewiseOpsRunner`：非 aclnn 后端的 Runner，用单节点 `ElewiseOperation` 组装 KernelGraph |

两个 operation 文件是本讲的「双主角」，两个 ops_runner 与 `runner_util.cpp`、`activation_aclnn_runner.cpp` 用于支撑核心实践任务（看清 Runner/Kernel 的选择链路）。

## 4. 核心概念与源码讲解

### 4.1 激活函数原理与 ActivationType 枚举

#### 4.1.1 概念说明

ATB 把所有激活函数收敛成**一个算子 `ActivationOperation`**，再用一个 `ActivationType` 枚举区分到底用哪一种激活。这和 u4-l1 的 `LinearParam`、u4-l2 的 `layerType` 是同一种「字段组合代替算子分裂」的设计：所有激活函数的输入输出结构高度相似（都是「逐元素变换」），拆成几十个算子反而冗余。

`ActivationParam` 在 `activationType` 之外，还带了少量「跨激活类型复用」的参数：`scale` 给 Swish 用、`dim` 给 SwiGLU 用、`geluMode` 给 GELU 用。某个字段只对特定的 `activationType` 有意义，其余情况被忽略。

#### 4.1.2 核心流程

设输入张量元素为 \(x\)，\(\sigma(x)=1/(1+e^{-x})\) 为 sigmoid，\(\Phi(x)\) 为标准正态分布的累积分布函数。各激活函数公式如下：

**逐元素激活（输入输出同形状）**：

\[
\text{ReLU}(x) = \max(0, x)
\]

\[
\text{Sigmoid}(x) = \sigma(x) = \frac{1}{1+e^{-x}}
\]

\[
\text{Swish}(x) = x \cdot \sigma(\text{scale} \cdot x)
\]

\[
\text{Tanh}(x) = \tanh(x), \qquad \text{Log}(x) = \ln(x)
\]

**GELU 有两种计算模式**（由 `geluMode` 切换）：

\[
\text{GELU}_{\text{NONE}}(x) = x \cdot \Phi(x) \quad \text{（原公式，依赖 erf）}
\]

\[
\text{GELU}_{\text{TANH}}(x) = 0.5x\left(1 + \tanh\left(\sqrt{\tfrac{2}{\pi}}\left(x + 0.044715x^3\right)\right)\right) \quad \text{（tanh 近似，即 FastGelu）}
\]

**SwiGLU（门控激活，会改变形状）**：把输入沿 `dim`（实际固定为最后一维）切成两半 \(x_1, x_2\)，再相乘：

\[
\text{SwiGLU}_{\text{forward}}(x) = x_1 \cdot \text{Swish}(x_2), \quad \text{其中 } x = [x_1 \| x_2]
\]

正向上输出形状的最后一维是输入的一半；反向（求梯度）则输入两个张量、输出与原 \(x\) 同形状。这一点是 SwiGLU 与其它「逐元素」激活的关键差别，后面 `InferShapeImpl` 会专门处理。

#### 4.1.3 源码精读

[include/atb/infer_op_params.h:79-91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L79-L91)：`ActivationType` 枚举全貌。注意首尾两个哨兵值 `ACTIVATION_UNDEFINED = 0` 与 `ACTIVATION_MAX`，工厂函数会用它们做范围校验（合法值必须严格介于两者之间）。

[include/atb/infer_op_params.h:107-126](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L107-L126)：`ActivationParam` 全貌。四个数据字段 `activationType` / `scale`(默认 1.0) / `dim`(默认 -1) / `geluMode`(默认 TANH_MODE) 各自服务不同激活类型；末尾是 `uint8_t rsv[8]` 预留字段（关于 `rsv` 的版本闸门作用见 u2-l3）。`GeLUMode` 子枚举只有 `TANH_MODE(0)` 与 `NONE_MODE(1)` 两值。

#### 4.1.4 代码实践

**实践目标**：建立「激活类型 → 公式 → Param 字段」的对应关系。

**操作步骤**：

1. 打开 [include/atb/infer_op_params.h:79-91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L79-L91)，对照 4.1.2 的公式表，逐项写出每个枚举值对应的公式。
2. 对每个激活类型，标注它「依赖哪个 Param 字段」：例如 `ACTIVATION_SWISH` 依赖 `scale`、`ACTIVATION_GELU` 依赖 `geluMode`、`ACTIVATION_SWIGLU_FORWARD` 依赖 `dim`。

**需要观察的现象**：你会发现 `scale` 只对 Swish 有意义、`dim` 只对 SwiGLU 有意义，但因为它们共享同一个 Param，所以总是一起被序列化、一起做 `rsv` 校验。

**预期结果**：整理出一张「枚举值 → 公式 → 依赖字段」的三列表。这是纯源码阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`ACTIVATION_FAST_GELU` 与 `ACTIVATION_GELU` + `geluMode=TANH_MODE` 在数学上是什么关系？
**答案**：两者都是 tanh 近似版本的 GELU（即 4.1.2 里的 \(\text{GELU}_{\text{TANH}}\)）。区别在于入口：前者直接用一个枚举值选定近似公式，后者用 `ACTIVATION_GELU` 再配 `geluMode=TANH_MODE`；在 950 的 aclnn 后端，前者走 `aclnnFastGelu`、后者走 `aclnnGeluV2`（见 4.3.3）。

**练习 2**：为什么 SwiGLU 不像其它激活那样「输出与输入同形状」？
**答案**：SwiGLU 先把输入切成两半再相乘，输出维度是输入的一半，本质是一个带门控的降维操作（常用于 FFN 的激活+投影），所以形状会变，需要专门的形状推导。

---

### 4.2 ActivationOperation：校验、输入输出与 InferShape

#### 4.2.1 概念说明

`ActivationOperation` 继承自 `OperationBase`（u3-l1），只重写 `InferShapeCheckImpl` / `InferShapeImpl` / `SetupCheckImpl` / `CreateRunner` / `GetParamJson` 几个钩子。它的核心工作有两件：一是在工厂函数 `CreateOperation` 里做合法性校验（类型是否合法、`dim` 是否合法、950 上是否支持）；二是在 `InferShapeImpl` 里推导输出形状——大多数激活是「透传」，唯独 SwiGLU 正/反向要改形状。

#### 4.2.2 核心流程

算子创建与校验流程（`CreateOperation` 模板特化）：

```
CreateOperation(ActivationParam)
  ├─ OP_PARAM_RSV_CHECK(opParam)                 // rsv 版本闸门
  ├─ activationType 范围校验（必须 > UNDEFINED 且 < MAX）
  ├─ dim 校验：dim 必须 == -1（否则无论哪种类型都拒绝）
  ├─ 若 Ascend950：仅支持 GELU / SWISH / SIGMOID / SWIGLU_FORWARD
  │     并按类型 LoadMethod() 预加载对应 aclnn 算子库
  └─ new ActivationOperation(opParam)
```

输入输出个数与形状推导规则：

| activationType | 输入数 | 输出数 | 输出形状 |
| --- | --- | --- | --- |
| 除 SWIGLU_BACKWARD 外的所有类型 | 1（x） | 1 | out[0] = in[0]（透传） |
| SWIGLU_FORWARD | 1（x） | 1 | out[0] = in[0]，但最后一维减半 |
| SWIGLU_BACKWARD | 2（y_grad, x） | 1 | out[0] = in[1]（即 x 的形状） |

#### 4.2.3 源码精读

[src/ops/ops_infer/activation/activation_operation.cpp:33-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L33-L85)：`CreateOperation` 模板特化。39-43 行做 `activationType` 范围校验；44-52 行是 `dim` 校验——注意它对**所有**类型都要求 `dim == -1`，即便 SwiGLU 也只允许在最后一维切分；53-77 行是 **Ascend950 特判**，只放行 `GELU`/`SWISH`/`SIGMOID`/`SWIGLU_FORWARD` 四种，其余直接 `ERROR_INVALID_PARAM`。

[src/ops/ops_infer/activation/activation_operation.cpp:146-158](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L146-L158)：`GetInputNum` / `GetOutputNum`。除 `SWIGLU_BACKWARD` 是 2 输入外，其余都是 1 输入 1 输出。

[src/ops/ops_infer/activation/activation_operation.cpp:222-239](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L222-L239)：`InferShapeImpl`，本模块核心。225-232 行处理 `SWIGLU_FORWARD`：先把 `dim`（-1）换成正索引（最后一维），再令 `out[0].shape.dims[splitDim] = in[0].shape.dims[splitDim] / 2`（常量 `SPLIT_NUM = 2`），即最后一维减半；233-234 行处理 `SWIGLU_BACKWARD`：`out[0] = in[1]`（x）；235-237 行是其余所有类型的透传 `out[0] = in[0]`。

[src/ops/ops_infer/activation/activation_operation.cpp:160-209](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L160-L209)：`CheckSwigluBackwardInTensor` / `CheckSwigluForwardInTensor`，对 SwiGLU 的输入维度做专门校验。注意正向要求 `Atlas 推理系列产品`（310P）上最后一维必须是 32 的倍数（`HIDDEN_SIZE_DIM_BASE = 32`）。

#### 4.2.4 代码实践

**实践目标**：手算 SwiGLU 正/反向的输出形状，理解它是唯一会改形状的激活。

**操作步骤**：

1. 设 `activationType = ACTIVATION_SWIGLU_FORWARD`，输入 x 形状为 `[2, 8]`（dimNum=2）。
2. 对照 `InferShapeImpl` 的 225-232 行，推算 `splitDim` 与输出形状。
3. 再设 `activationType = ACTIVATION_SWIGLU_BACKWARD`，两个输入 y_grad=`[2,4]`、x=`[2,8]`，推算输出形状。

**需要观察的现象**：正向 `splitDim = -1 + 2 = 1`（最后一维），输出 `dims[1] = 8/2 = 4`，故 out = `[2,4]`；反向 `out[0] = in[1] = x = [2,8]`。

**预期结果**：正向输入 `[2,8]` → 输出 `[2,4]`；反向输入 `(y_grad [2,4], x [2,8])` → 输出 `[2,8]`。运行结果「待本地验证」（需在昇腾环境实际编译运行）。

#### 4.2.5 小练习与答案

**练习 1**：工厂函数里为什么对 `ACTIVATION_RELU` 这类「看似最简单」的激活，在 950 上反而直接拒绝创建？
**答案**：950（A3 推理卡）的 aclnn 后端目前只为 `GELU`/`SWISH`/`SIGMOID`/`SWIGLU_FORWARD` 加载并适配了对应 CANN 算子（见 53-77 行），RELU 等尚未适配，因此 `CreateOperation` 提前拦截、返回 `ERROR_INVALID_PARAM`，避免运行到一半才失败。

**练习 2**：`dim` 字段既然必须恒为 -1，为什么还要存在？
**答案**：它是为未来「支持任意切分维」预留的扩展点；当前实现把能力收敛到「只在最后一维切分」，用 `dim == -1` 这条强约束保证行为确定，同时保留字段以便后续放开时不破坏 Param 二进制布局（配合 `rsv` 一起做版本兼容）。

---

### 4.3 ActivationOperation 如何按 ActivationType 选 Runner/Kernel（核心）

#### 4.3.1 概念说明

本模块对应讲义规格里的核心实践任务：**「在 activation_operation 中找出它如何根据 ActivationType 选择不同 kernel 或 runner」**。答案是分**三个层次**的，理解这三层是本讲的重点：

1. **Operation 层（`CreateRunner`）**：按芯片型号 + `ActivationType` 决定用哪个 Runner 子类。950 走 aclnn 系列 Runner，其它芯片走 `ActivationOpsRunner`。
2. **OpsRunner 内部组图**：非 950 时，`ActivationOpsRunner` 把**所有**激活类型都映射到**同一个** KernelGraph 节点（`opDesc` 名为 `"ActivationOperation"`），具体的激活类型作为参数传给底层 kernel，由 kernel 内部按 `activationType` 分支选择计算逻辑。
3. **AclnnRunner 内部适配（仅 950）**：`ActivationAclnnRunner` 按 `ActivationType` 选择**不同的 CANN aclnn 函数**（如 `aclnnRelu`、`aclnnGeluV2`、`aclnnSwish` 等）。

也就是说，「选 kernel」这件事在 ops 后端是「一个 kernel + 参数分支」，在 aclnn 后端是「按类型选不同 CANN 算子」——两条完全不同的策略。

#### 4.3.2 核心流程

Runner/Kernel 选择的三层决策树：

```
ActivationOperation::CreateRunner(param)
 ├─ Ascend950?
 │   ├─ SWIGLU_FORWARD → SwigluForwardAclnnRunner
 │   ├─ GELU           → GeluAclnnRunner
 │   └─ 其它(SWISH/SIGMOID) → ActivationAclnnRunner
 │        └─ 内部 MakeAdaptersByType() 按 activationType 选 aclnnXxx 函数
 └─ 其它芯片 → ActivationOpsRunner
      └─ 内部 GetActivationNodeOpDesc() 把所有类型映射到单个 "ActivationOperation" 节点
           （激活类型作为 param 传入，kernel 内部分支）
```

#### 4.3.3 源码精读

**第一层：Operation 层分派。**

[src/ops/ops_infer/activation/activation_operation.cpp:285-301](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L285-L301)：`CreateRunner`。288 行判断 `ASCEND_950`：289-291 行 `SWIGLU_FORWARD` 返回 `SwigluForwardAclnnRunner`；292-294 行 `GELU` 返回 `GeluAclnnRunner`；295-298 行的 `else` 兜住 `SWISH`/`SIGMOID`，返回 `ActivationAclnnRunner`；300 行是**所有非 950 芯片**的统一出口，返回 `ActivationOpsRunner`。这一层只决定「用哪个 Runner 类」。

**第二层：OpsRunner 内部——所有类型收敛到单个节点。**

[src/ops/ops_infer/activation/activation_ops_runner.cpp:22-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_ops_runner.cpp#L22-L46)：`ActivationOpsRunner` 构造函数。26-28 行只建 **1 个节点**，其 `opDesc` 由 `RunnerUtil::GetActivationNodeOpDesc(param_)` 生成；其余逻辑只是按是否 `SWIGLU_BACKWARD` 决定挂 1 个还是 2 个输入张量。

[src/atb/utils/runner_util.cpp:20-43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_util.cpp#L20-L43)：`GetActivationNodeOpDesc`。22-33 行用一张 `typeTable` 把 `infer::ActivationType` 翻译成底层 `AsdOps::OpParam::Activation::ActivationType`；37-41 行把这些参数（含 `activationType`/`scale`/`dim`/`approx`）打包；42 行返回 `{0, "ActivationOperation", param}`——**无论哪种激活类型，节点名都是 `"ActivationOperation"`**。这个字符串就是 u3-l4 讲过的注册衔接点，对应 Kernel 层 `REG_OPERATION` 注册的算子名；激活类型作为 `param.activationType` 传到 kernel，由 kernel 内部 `switch` 分支选择实际计算（即「单 kernel + 参数分支」策略）。

**第三层：AclnnRunner 内部——按类型选不同 CANN 算子（仅 950）。**

[src/ops/ops_infer/activation/activation_aclnn_runner.cpp:53-145](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_aclnn_runner.cpp#L53-L145)：`ActivationAclnnRunner::MakeAdaptersByType`。这里是一个 `switch(param.activationType)`，每个分支返回一个三元组 `{名字, getWs lambda, exec lambda}`，分别对应不同的 aclnn 函数：

| activationType | aclnn 函数 | 备注 |
| --- | --- | --- |
| `ACTIVATION_FAST_GELU` | `aclnnFastGelu` | tanh 近似 |
| `ACTIVATION_GELU` | `aclnnGeluV2` | `geluMode` 映射为 `approx` 参数（NONE→0，TANH→1） |
| `ACTIVATION_LOG` | `aclnnLog` | 自然对数 |
| `ACTIVATION_RELU` | `aclnnRelu` | — |
| `ACTIVATION_SIGMOID` | `aclnnSigmoid` | — |
| `ACTIVATION_SWISH` | `aclnnSwish` | 额外传入 `scale` 标量 |
| 其它 | `unsupported` | 返回错误码 |

[src/ops/ops_infer/activation/activation_aclnn_runner.cpp:246-289](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_aclnn_runner.cpp#L246-L289)：`LoadAclnnFunctions`，用 `std::call_once` 一次性把上述 6 对 aclnn 函数指针从动态库里 `dlsym` 加载进来，供 `MakeAdaptersByType` 选用。注意这是「运行时按需加载」——只有用到某类激活时才触发（见 `CreateOperation` 的 53-77 行 LoadMethod 调用）。

> 名词解释：**aclnn** 是 CANN 提供的「两段式」算子调用接口（先 `GetWorkspaceSize` 在 Host 规划，再 `Execute` 在 Device 下发），ATB 的 `AclnnRunner` 就是它的适配层（详见 u3-l3）。`aclOpExecutor` 是 aclnn 在 Host 规划阶段产出的执行器，可被缓存复用以缓解 Host Bound。

#### 4.3.4 代码实践

**实践目标**：完整追踪 `ActivationType` 到底层 kernel 的三层选择链路（本讲核心实践）。

**操作步骤**：

1. 选定一个激活类型，例如 `ACTIVATION_GELU`，以及一个非 950 芯片（如 910B）。
2. 从 [activation_operation.cpp:285-301](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L285-L301) 出发，确认它在 910B 上走 `ActivationOpsRunner`（300 行）。
3. 进入 [activation_ops_runner.cpp:22-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_ops_runner.cpp#L22-L46)，确认它建了 1 个节点，`opDesc` 来自 `GetActivationNodeOpDesc`。
4. 进入 [runner_util.cpp:20-43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_util.cpp#L20-L43)，确认节点名是 `"ActivationOperation"`、`ACTIVATION_GELU` 被翻译成底层的 `ACTIVATION_GELU` 枚举值随 param 下传。
5. 再选 `ACTIVATION_GELU` + 950 芯片重走一遍：这次 `CreateRunner` 走 292-294 行返回 `GeluAclnnRunner`（注意 GELU 在 950 上有专属 Runner，不走通用的 `ActivationAclnnRunner`）。

**需要观察的现象**：同一份 `ACTIVATION_GELU`，在 910B 上落到一个名为 `"ActivationOperation"` 的融合 kernel 节点（由 kernel 内部按类型分支计算）；在 950 上则落到 `GeluAclnnRunner` 直接调 CANN 的 `aclnnGeluV2`。两条路径、同一语义。

**预期结果**：你能画出一张「`ActivationType` × 芯片 → Runner → 底层」的对照表。这是纯源码追踪实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么非 950 芯片把所有激活类型收敛到一个 `"ActivationOperation"` kernel，而 950 却为它们各配一个 aclnn 函数？
**答案**：非 950（如 910A/910B/310P）走 ATB 自研的 AscendC 融合 kernel，一个 kernel 内部用 `switch(activationType)` 覆盖所有类型，省 kernel 数、便于在图算子里复用；950 走 CANN 官方 aclnn 后端，每种激活对应一个官方预编译算子（`aclnnRelu`/`aclnnGeluV2`/...），ATB 只做适配桥接，因此按类型分别调用。这是「自研融合」与「官方算子适配」两条路线的差异（与 u3-l3、u4-l1 的 aclnn 分派同源）。

**练习 2**：`ACTIVATION_GELU` 在 950 上为什么用 `GeluAclnnRunner` 而不是通用的 `ActivationAclnnRunner`？
**答案**：`ActivationAclnnRunner::MakeAdaptersByType` 里其实也支持 `ACTIVATION_GELU`（67-79 行），但 `CreateRunner` 在 292-294 行对 GELU 做了更优先的分派，使用专属的 `GeluAclnnRunner`。这是一种「特化优先于通用」的常见优化——专属 Runner 可以针对 GELU 的 `geluMode` 等参数做更精准的处理。

---

### 4.4 ElewiseParam：ElewiseType 枚举与输入输出个数

#### 4.4.1 概念说明

`ElewiseOperation` 是 ATB 的「元素级运算大杂烩」：它用一个 `ElewiseType` 枚举收纳了 **20 种**元素级运算——从最基础的加减乘除、三角函数、类型转换，到量化/反量化/动态量化。这与 `ActivationOperation` 的思路一脉相承，但覆盖面更广：激活只做「非线性变换」，而 Elewise 还包括「线性运算」「比较运算」「量化运算」。

`ElewiseParam` 用嵌套子结构组织参数：`QuantParam` 服务于各种量化、`MulsParam` 服务于「乘标量」、`outTensorType` 服务于类型转换。和激活一样，某个子结构只对特定的 `elewiseType` 有意义。

#### 4.4.2 核心流程

按输入张量个数，20 种 `ElewiseType` 可分成三类：

| 类别 | 输入数 | 包含的 ElewiseType |
| --- | --- | --- |
| 一元（1 输入） | 1 | CAST、MULS、COS、SIN、NEG、QUANT、LOGICAL_NOT、TANH、DYNAMIC_QUANT |
| 二元（2 输入，支持广播） | 2 | ADD、SUB、MUL、REALDIV、LOGICAL_AND、LOGICAL_OR、LESS、GREATER、EQUAL |
| 三元（3 输入） | 3 | QUANT_PER_CHANNEL、DEQUANT_PER_CHANNEL |

输出个数规则：

- 绝大多数 `ElewiseType`：1 个输出。
- `DYNAMIC_QUANT`：`asymmetric=false` 时 2 个输出（量化结果 + scale），`asymmetric=true` 时 3 个输出（再加 offset）。

算子创建流程（`CreateOperation`）：

```
CreateOperation(ElewiseParam)
  ├─ 若 Ascend950：DYNAMIC_QUANT / QUANT_PER_CHANNEL 预加载对应 aclnn
  ├─ OP_PARAM_RSV_CHECK（opParam / mulsParam / quantParam 三道 rsv 闸门）
  ├─ DEQUANT_PER_CHANNEL 仅 910B 支持
  ├─ QUANT 仅 910B 或 950 支持
  ├─ DYNAMIC_QUANT 不支持 asymmetric
  ├─ elewiseType 范围校验（必须 > UNDEFINED 且 < MAX）
  ├─ ElewiseAclnnRunner::LoadMethod()
  └─ new ElewiseOperation(opParam)
```

#### 4.4.3 源码精读

[include/atb/infer_op_params.h:354-421](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L354-L421)：`ElewiseParam` 全貌。360-383 行是 `ElewiseType` 枚举（注意首尾哨兵 `ELEWISE_UNDEFINED`/`ELEWISE_TYPE_MAX`）；386-397 行是 `QuantParam`（`inputScale`/`asymmetric`/`inputOffset`）；400-407 行是 `MulsParam`（`varAttr`）；416 行 `outTensorType` 给 CAST 用。

[src/ops/ops_infer/elewise/elewise_operation.cpp:40-95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L40-L95)：`CreateOperation` 模板特化。注意 60-62 行对**三层**结构都做了 `OP_PARAM_RSV_CHECK`（`opParam`、`mulsParam`、`quantParam`），这是比 `ActivationParam` 更严格的版本闸门；63-78 行是多项芯片/类型互斥校验。

[src/ops/ops_infer/elewise/elewise_operation.cpp:143-181](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L143-L181)：`GetInputNum`（用一张表返回 1/2/3）与 `GetOutputNum`（`DYNAMIC_QUANT` 按 `asymmetric` 返回 2 或 3，其余返回 1）。

[src/ops/ops_infer/elewise/elewise_operation.cpp:97-139](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L97-L139)：构造函数里的 `opIniTable`，把每个 `ElewiseType` 映射到 `atb_ops_info.ini` 中的 IR key 字符串。注意 310B（`Atlas 200I A2`）与 950 各有专属变体名，如 `ElewiseOperationCastAtlas200I500A2`、`ElewiseOperationQuantAscend950`；131-132 行还会为 `DYNAMIC_QUANT + asymmetric` 拼出 `...Asymmetric` 后缀的 key。这是 u3-l1 讲过的「外置 IR 配置」在 Elewise 上的落地。

#### 4.4.4 代码实践

**实践目标**：根据 `elewiseType` 预测输入输出个数与所需的 Param 子结构。

**操作步骤**：

1. 选三个类型：`ELEWISE_MULS`、`ELEWISE_ADD`、`ELEWISE_DYNAMIC_QUANT`（`asymmetric=true`）。
2. 对照 [elewise_operation.cpp:143-181](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L143-L181)，写出各自的输入数与输出数。
3. 标注每个类型「实际会读哪个 Param 子结构」：例如 `MULS` 读 `mulsParam.varAttr`、`DYNAMIC_QUANT` 读 `quantParam.asymmetric`。

**需要观察的现象**：`MULS` → 1 入 1 出（读 `mulsParam`）；`ADD` → 2 入 1 出（不读特殊子结构）；`DYNAMIC_QUANT + asymmetric` → 1 入 3 出（读 `quantParam`）。

**预期结果**：整理出三行对照表。这是 `VariantPack` 装填的直接依据（u1-l4、u1-l6）。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ELEWISE_MULS`（乘标量）被归为「一元」运算，而不是像 `ELEWISE_MUL`（乘张量）那样的「二元」运算？
**答案**：MULS 是「张量 × 标量」，标量直接放在 `mulsParam.varAttr` 里、不占输入张量槽位，所以只有 1 个张量输入；MUL 是「张量 × 张量」，第二个操作数是一个完整张量，占第 2 个输入槽位。

**练习 2**：`GetOutputNum` 对 `DYNAMIC_QUANT` 的输出个数取决于 `asymmetric`，这反映了什么语义？
**答案**：动态量化对每个 token 算一个 scale（对称量化）或 scale+offset（非对称量化）。对称时输出「量化结果 + scale」共 2 个；非对称时多一个 offset，共 3 个。`asymmetric` 标志直接决定是否产出 offset 张量。

---

### 4.5 ElewiseOperation 的 InferShape、广播与量化变体

#### 4.5.1 概念说明

`ElewiseOperation` 的 `InferShapeImpl` 比激活复杂，因为要处理三类情况：一元透传、二元广播、量化/类型转换导致的 dtype 变化。基线规则仍是 `out[0] = in[0]`，但二元运算要走广播推导（`InferShapeCommon`），量化运算要改 dtype（如 fp16→int8），动态量化还要额外推导 scale/offset 输出的形状。

此外，`ElewiseOperation` 的 Runner 选择与激活同构：950 上部分类型走 aclnn，其余走 `ElewiseOpsRunner`（单节点 `"ElewiseOperation"`）。本模块把形状推导与 Runner 选择一起收尾。

#### 4.5.2 核心流程

`InferShapeImpl` 的分支结构：

```
InferShapeImpl
 ├─ 基线：out[0] = in[0]
 ├─ 一元透传类（MULS/COS/SIN/NEG/LOGICAL_NOT/TANH）：直接返回
 └─ switch(elewiseType)
     ├─ CAST              → 按 outTensorType 改 dtype
     ├─ QUANT             → fp16→int8（950 走 InferShapeImplQuantAscend950）
     ├─ QUANT_PER_CHANNEL → int8
     ├─ DEQUANT_PER_CHANNEL → int8→fp16
     ├─ ADD/MUL/REALDIV/SUB/LESS/GREATER/EQUAL → InferShapeCommon（广播）
     ├─ LOGICAL_AND/OR    → InferShapeCommon + dtype 改 int8
     └─ DYNAMIC_QUANT     → int8 + scale(float, dimNum--) [+ offset]
```

二元广播规则（`InferShapeCommon`，与 NumPy 一致）：取两个输入中较大的 `dimNum`，从末位向前逐维对齐；对应维「相等」或「其中之一为 1」即合法，输出该维取较大值；否则报「cannot broadcast」。比较类（LESS/GREATER/EQUAL）的输出 dtype 强制为 `ACL_INT8`。

Runner 选择（`CreateRunner`）：

```
CreateRunner(elewiseType)
 ├─ 950 + DYNAMIC_QUANT      → AclnnDynamicQuantRunner
 ├─ 950 + QUANT_PER_CHANNEL  → AclnnAscendQuantRunner
 ├─ 950 + (CAST/MULS/COS/SIN/ADD/MUL/SUB/REALDIV/LESS/GREATER/LOGICAL_NOT/QUANT)
 │     → ElewiseAclnnRunner
 └─ 其余                     → ElewiseOpsRunner（单节点 "ElewiseOperation"）
```

#### 4.5.3 源码精读

[src/ops/ops_infer/elewise/elewise_operation.cpp:183-230](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L183-L230)：`InferShapeImpl`。186 行先做基线 `out[0] = in[0]`；189-193 行让一元透传类直接返回；194-229 行 `switch` 分派到各专用推导函数。

[src/ops/ops_infer/elewise/elewise_operation.cpp:412-449](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L412-L449)：`InferShapeCommon`，二元广播核心。425-426 行取较大 `dimNum`；428-441 行的 `while` 循环从末位向前逐维对齐，434 行 `if (dim0 != 1 && dim1 != 1 && dim0 != dim1)` 即广播的合法性判据；438 行 `dimOut = max(dim0, dim1)`；443-446 行把比较类输出 dtype 置为 `ACL_INT8`。

[src/ops/ops_infer/elewise/elewise_operation.cpp:362-410](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L362-L410)：`InferShapeImplDynamicQuant`。383 行 `out[0].dtype = ACL_INT8`（量化结果）；384-386 行 `out[1] = in[0]`、`dtype = ACL_FLOAT`、`shape.dimNum--`（scale 是「逐 token」的，砍掉最后一维 hidden）；387-389 行若 `asymmetric` 再加 `out[2] = out[1]`（offset）。这里还嵌入了芯片相关的最后一维上限校验（`MAX_VALUE1=26624` 等，见 488-510 行）。

[src/ops/ops_infer/elewise/elewise_operation.cpp:512-535](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L512-L535)：`CreateRunner`。515-521 行 950 上 `DYNAMIC_QUANT`/`QUANT_PER_CHANNEL` 走专属 aclnn Runner；522-533 行用一个 `unordered_set aclnnOpType` 列出 950 上走通用 `ElewiseAclnnRunner` 的 12 种类型；534 行其余全部走 `ElewiseOpsRunner`。

[src/ops/ops_infer/elewise/elewise_ops_runner.cpp:67-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_ops_runner.cpp#L67-L102)：`ElewiseOpsRunner::SetOuttensor`，组图核心。69 行 `GetOpElwiseType()` 把 `ElewiseParam::ElewiseType` 翻译成底层 `AsdOps::OpParam::Elewise::ElewiseType`；101 行 `elewiseNode.opDesc = {0, "ElewiseOperation", elsewiseParam}`——与激活完全同构：**所有元素级类型都收敛到单个 `"ElewiseOperation"` 节点**，类型作为参数下传，由 kernel 内部分支。88-99 行按类型把 `varAttr`/`inputScale`/`inputOffset`/`outTensorType` 等填进底层 param。

[src/ops/ops_infer/elewise/elewise_operation.cpp:474-486](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L474-L486)：`GetEmptyInTensorPermissions`，重写自 `OperationBase`。它允许三元运算（`QUANT_PER_CHANNEL`/`DEQUANT_PER_CHANNEL`）的第 3 个输入为空张量（offset 可选），这是 u3-l1 讲过的「可空输入」标记在 Elewise 上的体现。

#### 4.5.4 代码实践

**实践目标**：手算一个二元广播运算与一个动态量化的输出形状。

**操作步骤**：

1. **广播用例**：`ELEWISE_ADD`，输入 a = `[2, 1, 4]`，b = `[1, 3, 4]`。对照 [elewise_operation.cpp:412-449](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L412-L449) 逐维推算输出形状。
2. **动态量化用例**：`ELEWISE_DYNAMIC_QUANT`，`asymmetric=false`，输入 x = `[2, 5, 8]`（fp16）。对照 [elewise_operation.cpp:362-410](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L362-L410) 推算两个输出的形状与 dtype。

**需要观察的现象**：广播用例三维分别为 `max(2,1)=2`、`max(1,3)=3`、`max(4,4)=4`，输出 `[2,3,4]`；动态量化 `out[0]=[2,5,8]` dtype=`ACL_INT8`，`out[1]=[2,5]`（`dimNum--` 砍掉最后一维）、dtype=`ACL_FLOAT`。

**预期结果**：广播输出 `[2,3,4]`；动态量化输出 2 个，形状如上。运行结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`ELEWISE_LESS` 这类比较运算的输出为什么 dtype 是 `ACL_INT8` 而不是 `ACL_BOOL`？
**答案**：ATB 底层 kernel 与硬件更倾向于用 int8 表示布尔结果（0/1），`InferShapeCommon` 在 443-446 行把比较类输出 dtype 强制为 `ACL_INT8`，`LOGICAL_AND/OR` 同理（217-223 行）。这是一种与底层实现对齐的约定。

**练习 2**：动态量化的 scale 输出为什么要 `dimNum--`（去掉最后一维）？
**答案**：与 u4-l2 的归一化动态量化完全同理——scale 是「逐 token」的量化系数，对 x 的每个 token（每个 hidden 向量）算一个标量，因此维度比 x 少最后一维（hidden 维），保留 batch/seq 维度。这也和 `GetOutputNum` 里「逐 token 输出」的语义一致。

---

## 5. 综合实践

本任务把本讲全部内容串起来，对应讲义规格里的总实践：**「在 activation_operation 中找出它如何根据 ActivationType 选择不同 kernel 或 runner」**，并扩展到 Elewise。

**任务背景**：你要为一个新接入的 Transformer 模型排查「为什么同一个 GELU 激活，在两张不同型号的昇腾卡上走的代码路径不一样」。为此需要在不跑代码的前提下，凭源码画出完整的「类型 → Runner → 底层」选择链路。

**操作步骤**：

1. 阅读以下四段核心代码：
   - [activation_operation.cpp:285-301](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_operation.cpp#L285-L301)（`CreateRunner`，第一层）
   - [runner_util.cpp:20-43](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/runner_util.cpp#L20-L43)（`GetActivationNodeOpDesc`，第二层 ops 后端）
   - [activation_aclnn_runner.cpp:53-145](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_aclnn_runner.cpp#L53-L145)（`MakeAdaptersByType`，第三层 aclnn 后端）
   - [elewise_operation.cpp:512-535](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/elewise/elewise_operation.cpp#L512-L535)（Elewise 的 `CreateRunner`，对照练习）
2. 画一张表，行是 `ActivationType`（至少覆盖 RELU、GELU、SWISH、SWIGLU_FORWARD、SIGMOID 五项），列是「950 上的 Runner」「950 上的底层」「非 950 的 Runner」「非 950 的底层」。
3. 再为 Elewise 选两个类型（如 `ELEWISE_ADD`、`ELEWISE_DYNAMIC_QUANT`），分别写出它们在 950 与非 950 上走的 Runner。
4. 用一句话总结：ops 后端与 aclnn 后端在「选 kernel」策略上的根本差异。

**参考答案要点**：

| ActivationType | 950 Runner | 950 底层 | 非 950 Runner | 非 950 底层 |
| --- | --- | --- | --- | --- |
| RELU | （创建即被拒） | — | ActivationOpsRunner | `"ActivationOperation"` 单节点 + 类型分支 |
| GELU | GeluAclnnRunner | `aclnnGeluV2` | ActivationOpsRunner | `"ActivationOperation"` 单节点 |
| SWISH | ActivationAclnnRunner | `aclnnSwish` | ActivationOpsRunner | `"ActivationOperation"` 单节点 |
| SWIGLU_FORWARD | SwigluForwardAclnnRunner | aclnn swiglu | ActivationOpsRunner | `"ActivationOperation"` 单节点 |
| SIGMOID | ActivationAclnnRunner | `aclnnSigmoid` | ActivationOpsRunner | `"ActivationOperation"` 单节点 |

- Elewise：`ELEWISE_ADD` 在 950 走 `ElewiseAclnnRunner`、非 950 走 `ElewiseOpsRunner`（单节点 `"ElewiseOperation"`）；`ELEWISE_DYNAMIC_QUANT` 在 950 走专属 `AclnnDynamicQuantRunner`、非 950 走 `ElewiseOpsRunner`。
- 一句话总结：**ops 后端是「一个融合 kernel + 参数分支」覆盖所有类型，aclnn 后端是「按类型分别桥接 CANN 官方算子」**——前者靠自研 kernel 的内部 switch，后者靠适配层选不同的 aclnn 函数。

**预期结果**：你能独立画出上表并说清两种策略的差异。若想进一步验证，可在昇腾环境用 torch_atb（u2-l2）分别构造 `ACTIVATION_GELU` 的 Param 与张量实际执行，并开 `ATB_LOG` 观察日志里打印的 Runner 类名（`create Gelu AclnnRunner` 或 `ActivationOpsRunner called`），但运行结果「待本地验证」。

## 6. 本讲小结

- 激活与元素级是 Transformer 里最高频的两类「逐元素」操作，ATB 分别用 `ActivationOperation` 与 `ElewiseOperation` 两个算子承载，各自用一个枚举（`ActivationType` / `ElewiseType`）区分具体运算，是「单 Param + 枚举字段」设计哲学的极致体现。
- `ActivationType` 覆盖 ReLU/GELU/SwiSH/Sigmoid/SwiGLU 等；`ActivationParam` 用 `scale`(Swish)、`dim`(SwiGLU)、`geluMode`(GELU) 三个字段服务不同类型，末尾 `rsv[8]` 做版本闸门。
- `ActivationOperation::InferShapeImpl` 对绝大多数类型是透传 `out[0]=in[0]`，唯独 SwiGLU 正向（最后一维减半）、反向（`out[0]=in[1]`）要改形状，这是激活里唯一的非透支情况。
- **核心实践结论**：`ActivationType` 选 kernel 是三层决策——`CreateRunner` 选 Runner 子类（950 走 aclnn、其它走 ops）；ops 后端把所有类型收敛到单个 `"ActivationOperation"` kernel 节点、靠参数分支；aclnn 后端（950）按类型选不同 CANN 算子（`aclnnRelu`/`aclnnGeluV2`/`aclnnSwish` 等）。
- `ElewiseType` 收纳 20 种元素级运算，按输入数分一元/二元/三元三类；二元运算走 NumPy 风格广播（`InferShapeCommon`），比较/逻辑类输出 dtype 强制 int8；量化与动态量化变体会改 dtype 并额外产出 scale（逐 token，砍最后一维）/offset。
- Elewise 的 Runner 选择与激活同构：950 上 12 种通用类型走 `ElewiseAclnnRunner`、动态量化和逐通道量化走专属 aclnn Runner，其余走 `ElewiseOpsRunner`（同样是单节点 `"ElewiseOperation"` + 参数分支）。

## 7. 下一步学习建议

- **进入注意力算子（u4-l4）**：激活与元素级常作为 FFN/Attention 内部的子步骤。下一讲讲 `SelfAttention` 融合算子，你会看到 attention 内部如何复用 softmax（本质也是逐元素归约）等机制。
- **横向对比「字段组合型」算子**：本讲的 `ActivationType` / `ElewiseType` 与 u4-l1 的 `LinearParam`、u4-l2 的 `layerType` 一脉相承，建议三讲对照阅读，体会 ATB「单 Param 覆盖 N 种行为」的统一风格。
- **深入 aclnn 适配层**：若想看清 950 上 `ActivationAclnnRunner` 的两段式调用（`GetWorkspaceSize` + `Execute`）与 executor 缓存复用，可重读 u3-l3（AclnnRunner 与 CANN 算子适配），并结合本讲 [activation_aclnn_runner.cpp:201-244](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/activation/activation_aclnn_runner.cpp#L201-L244) 的三个钩子函数。
- **用图算子把它们串起来**：学完本讲后，可用 u5 的 `GraphOpBuilder` 把 `Linear + Activation(GELU) + Elewise(ADD残差)` 串成一个图算子，观察「统一调度、复用 workspace、缓解 Host Bound」带来的收益（这正是 ATB 把这些基础算子做「单节点收敛」的目的）。
