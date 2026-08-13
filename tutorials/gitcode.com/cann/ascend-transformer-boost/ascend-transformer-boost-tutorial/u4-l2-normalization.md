# Normalization 算子：LayerNorm 与 RMSNorm

## 1. 本讲目标

归一化（Normalization）是 Transformer 每一层都要做的高频操作，本讲带你读懂 ATB 中两个最核心的归一化算子：`LayerNormOperation` 与 `RmsNormOperation`。学完后你应当能够：

- 说清 LayerNorm 与 RMSNorm 的数学语义差异，以及它们在 Transformer 中的位置。
- 看懂 `LayerNormParam` / `RmsNormParam` 的「三态」结构（NORM / PRENORM / POSTNORM），并能根据 `layerType` + `quantType` 判断一个算子有几个输入、几个输出。
- 读懂 `InferShapeImpl`，能手算给定 Param 与输入形状时的输出形状与 dtype。
- 理解归一化算子的 Runner 分派规则（aclnn 后端 vs ops 后端），并知道 `CohereLayerNorm` 等变体为何存在。

本讲是 u4（关键 Transformer 算子精讲）的第二篇，承接 u3-l1 讲过的 `OperationBase` 框架基类——你会看到归一化算子正是通过重写 `InferShapeImpl` / `CreateRunner` 等钩子，把自己接入了上一讲建立的 `Operation → Runner → KernelGraph → Kernel` 执行链路。

## 2. 前置知识

在进入源码前，先用最朴素的语言把「归一化在做什么」讲清楚。

**为什么要归一化？** 神经网络训练时，每一层的输入分布会随前层权重变化而漂移（内部协变量偏移），导致训练不稳定。归一化把激活值按某个维度拉回均值 0、方差 1 附近，再用可学习参数重新缩放和偏移，使数值落在敏感区间，训练更稳、收敛更快。

**在哪里归一化？** LayerNorm 是「按样本归一化」：对单个样本的某一维度区间计算统计量，与 batch 无关，因而天然适合变长序列（NLP/Transformer）。与之相对的 BatchNorm 按 batch 统计，在变长序列上不好用。

**LayerNorm 与 RMSNorm 的差别。** 二者都归一化最后一维（hidden 维），但统计量不同：

- LayerNorm 先减均值、再除标准差，有缩放参数 \(\gamma\) 和偏移参数 \(\beta\)。
- RMSNorm 不减均值，只用均方根（RMS）做缩放，通常只有 \(\gamma\)、没有 \(\beta\)，计算量更小。LLaMA 等主流大模型普遍采用 RMSNorm。

如果你还不熟悉上一讲的 `OperationBase`，请先回顾：算子不直接 launch kernel，而是由 `OperationBase` 的冻结骨架统一做校验、Tiling、workspace，子类只通过钩子插入「形状推导」「选 Runner」等逻辑。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `include/atb/infer_op_params.h` | 定义 `LayerNormParam`、`RmsNormParam`、`CohereLayerNormParam` 及公共枚举 `QuantType` / `DynamicQuantType` |
| `src/ops/ops_infer/layer_norm/layer_norm_operation.h` | `LayerNormOperation` 类声明，列出它重写的钩子 |
| `src/ops/ops_infer/layer_norm/layer_norm_operation.cpp` | `LayerNormOperation` 的全部实现：工厂函数、校验、`InferShapeImpl`、`CreateRunner` |
| `src/ops/ops_infer/rms_norm/rms_norm_operation.h` | `RmsNormOperation` 类声明 |
| `src/ops/ops_infer/rms_norm/rms_norm_operation.cpp` | `RmsNormOperation` 的全部实现，含平台相关的 `RmsNormTypeCheck` |
| `src/ops/ops_infer/layer_norm/layer_norm_ops_runner.cpp` | `LayerNormOpsRunner`：用单节点 `NormOperation` 组装 KernelGraph |
| `src/ops/ops_infer/cohere_layernorm/cohere_layernorm_operation.cpp` | `CohereLayerNormOperation` 变体（Command R Plus 风格） |

两个 operation 文件是本讲的「双主角」，其余文件用于补充 Param 定义、Runner 组图与变体。

## 4. 核心概念与源码讲解

### 4.1 归一化算子的数学原理

#### 4.1.1 概念说明

归一化算子的本质是：对输入 \(x\) 的某个维度区间（通常是最后一维 hidden）计算统计量，再用统计量把 \(x\) 压缩到稳定范围，最后乘以可学习缩放 \(\gamma\)（可选加偏移 \(\beta\)）。其中 \(\epsilon\) 是一个小常数，加在分母上防止除零——这个 \(\epsilon\) 在 ATB 的 Param 里是强校验项，不能为 0。

ATB 同时提供 LayerNorm 与 RMSNorm，并各自支持三种「归一化时机」：NORM（纯归一化）、PRENORM（先归一化再加残差）、POSTNORM（先加残差再归一化）。这三种对应 Transformer 残差结构的不同写法，后面会看到它们由同一个 Param 的 `layerType` 字段切换。

#### 4.1.2 核心流程

设对最后一维 \(N\) 个元素做归一化，\(\epsilon\) 为防除零小常数。

**LayerNorm（NORM 模式）**：

\[
\mu = \frac{1}{N}\sum_{i=1}^{N} x_i
\]

\[
\sigma^2 = \frac{1}{N}\sum_{i=1}^{N} (x_i - \mu)^2
\]

\[
y_i = \gamma_i \cdot \frac{x_i - \mu}{\sqrt{\sigma^2 + \epsilon}} + \beta_i
\]

**RMSNorm（NORM 模式，LLaMA 公式）**：不减均值，无 \(\beta\)：

\[
\text{rstd} = \frac{1}{\sqrt{\frac{1}{N}\sum_{i=1}^{N} x_i^2 + \epsilon}}
\]

\[
y_i = x_i \cdot \text{rstd} \cdot \gamma_i
\]

其中 \(\text{rstd}\) 称为「标准差的倒数」。ATB 的 RMSNorm 提供一个 `rstd` 开关，开启时会额外输出这个 \(\text{rstd}\) 张量（用于训练反向），后面会在 `InferShapeImpl` 里看到它的形状推导。

**PRENORM / POSTNORM 的区别**（伪代码）：

```
PRENORM :  y = norm(x);  out = residual + y * zoomScale   // 先归一化后加残差
POSTNORM:  out = norm(residual + x)                       // 先加残差后归一化
```

#### 4.1.3 源码精读

Param 里的 `epsilon` 默认值为 `1e-5`，并在工厂函数里被强校验：一旦绝对值小于阈值 `THRESHOLD = 2e-38` 就直接返回 `ERROR_INVALID_PARAM`。

[include/atb/infer_op_params.h:692-710](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L692-L710)：`LayerNormParam::NormParam` 的字段，可见 `epsilon = 1e-5`、`beginNormAxis`、`beginParamsAxis`、`dynamicQuantType`，末尾是 `uint8_t rsv[20]` 预留字段（关于 `rsv` 的版本闸门作用见 u2-l3）。

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:53-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L53-L68)：`NormParamCheck`，对 `quantType` 合法性与 `epsilon` 阈值做校验，是上面 \(\epsilon\) 防除零约束在代码里的落点。

#### 4.1.4 代码实践

**实践目标**：确认 \(\epsilon\) 的「非零」是硬约束，理解它的防除零作用。

**操作步骤**：

1. 打开 `src/ops/ops_infer/layer_norm/layer_norm_operation.cpp`，找到 `NormParamCheck` 与常量 `THRESHOLD`。
2. 构造一个 `LayerNormParam`，把 `normParam.epsilon` 设为 `0.0f`，模拟调用 `CreateOperation`。
3. 读 `CreateOperation`（见 4.2）确认它会进入 `NormParamCheck` 分支。

**需要观察的现象**：因为 `std::fabs(0.0f) < 2e-38` 成立，函数打印 `Invalid epsilon...` 并返回 `ERROR_INVALID_PARAM`。

**预期结果**：算子创建失败，返回非 0 错误码。运行结果「待本地验证」（需要在昇腾环境实际编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 RMSNorm 比 LayerNorm 计算量更小？
**答案**：RMSNorm 不需要计算均值和减均值这一步，只算均方根；且通常没有偏移 \(\beta\)，少一次加法与一个权重张量。

**练习 2**：`epsilon` 为什么必须非零？
**答案**：当输入恰好全为常数时，方差（或均方根）可能为 0，分母 \(\sqrt{\sigma^2+\epsilon}\) 会除零；\(\epsilon\) 保证分母恒正。

---

### 4.2 LayerNormOperation：Param 三态与校验

#### 4.2.1 概念说明

`LayerNormOperation` 对应的 `LayerNormParam` 用「一个 Param + `layerType` 枚举」统一表达三种归一化结构，而不是拆成三个算子。这与 u4-l1 讲过的 `LinearParam`「字段组合代替算子分裂」是同一种设计哲学。

`layerType` 取三种值（外加 UNDEFINED）：`LAYER_NORM_NORM` / `LAYER_NORM_PRENORM` / `LAYER_NORM_POSTNORM`，每种对应 Param 里一个独立的子结构 `NormParam` / `PreNormParam` / `PostNormParam`。`layerType` 决定使用哪个子结构，进而决定校验规则、输入输出个数与 Runner。

#### 4.2.2 核心流程

算子创建流程（`CreateOperation` 模板特化）：

```
CreateOperation(LayerNormParam)
  ├─ OP_PARAM_RSV_CHECK(opParam / normParam / preNormParam / postNormParam)  // rsv 版本闸门
  ├─ 按 layerType 分派 → NormParamCheck / PreNormParamCheck / PostNormParamCheck
  ├─ 若是 Ascend950：只允许 NORM + UNQUANT，并 LoadMethod() 预加载 aclnn
  └─ new LayerNormOperation(opParam)
```

构造函数里还会按 `layerType` + `quantType` + `dynamicQuantType` 拼出一个 IR key 字符串（如 `LayerNormOperationNormQuant`），从 `AtbOperationIrCfg` 取回外置的 dtype/format 白名单（即 `OperationIr`，u3-l1 已讲）。

#### 4.2.3 源码精读

[include/atb/infer_op_params.h:676-761](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L676-L761)：`LayerNormParam` 全貌。注意 `LayerNormType` 枚举（682-688 行）与三个并列子结构，最外层还有 `uint8_t rsv[8]`。

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:104-155](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L104-L155)：`CreateOperation` 模板特化。114-132 行按 `layerType` 三分支校验；134-148 行是 **Ascend950（A3 推理卡）特判**——只允许 `NORM` + 无量化，否则直接拒绝；143 行调用 `LayerNormAclnnRunner::LoadMethod()` 预加载 aclnn 算子库。

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:70-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L70-L102)：`PreNormParamCheck` / `PostNormParamCheck`。注意 PRENORM 明确「不支持量化」（72 行），POSTNORM 允许 `QUANT_INT8`。

[src/ops/ops/ops_infer/layer_norm/layer_norm_operation.cpp:157-182](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L157-L182)：构造函数拼 IR key。例如 `NORM + INT8 + SymmetricDynamicQuant` 会拼成 `LayerNormOperationNormSymmetricDynamicQuant`，181 行据此取 `operationIr_`。

> 公共枚举回顾（u2-l3 已讲）：`QuantType` 取 `QUANT_UNQUANT(0)` / `QUANT_INT8(2)`（见 [include/atb/infer_op_params.h:49-56](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L49-L56)）；`DynamicQuantType` 取 `UNDEFINED/SYMMETRIC/ASYMMETRIC`（[include/atb/infer_op_params.h:64-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L64-L68)），其中 `ASYMMETRIC` 当前不支持。

#### 4.2.4 代码实践

**实践目标**：根据 Param 字段预测算子能否在 Ascend950 上创建。

**操作步骤**：

1. 设想两份 Param：A 为 `LAYER_NORM_NORM + QUANT_UNQUANT`；B 为 `LAYER_NORM_PRENORM + QUANT_UNQUANT`。
2. 对照 `CreateOperation` 的 134-148 行 Ascend950 特判，判断两者在 950 上能否创建。

**需要观察的现象**：A 通过；B 命中 135-137 行，打印 `Ascend950 only supports LAYER_NORM_NORM` 并返回 `ERROR_INVALID_PARAM`。

**预期结果**：A 成功、B 失败。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`LayerNormParam` 为什么把三种结构放在同一个 Param 里，而不是定义三个 Param？
**答案**：三者校验逻辑、输入输出个数、Runner 选择高度相似，用 `layerType` 切换可避免代码重复，也方便后续扩展（如新增一种归一化时机只需加一个枚举值与子结构）。

**练习 2**：构造函数里拼 IR key 的作用是什么？
**答案**：把「算子变体」映射到 `atb_ops_info.ini` 里对应的一段规格约束（dtype/format 白名单），让 `OperationBase` 骨架能用统一的 `CheckIniMatch` 做通用校验，而不必在每个算子里重复写。

---

### 4.3 LayerNormOperation：输入输出个数与 InferShapeImpl

#### 4.3.1 概念说明

`layerType` + `quantType` + `dynamicQuantType` 三个字段共同决定 `LayerNormOperation` 有几个输入、几个输出。这是 `VariantPack` 该装几个 `Tensor` 的依据（u1-l4、u1-l6）。归一化算子的输入个数变化主要由「是否量化」「是否带残差」驱动：量化会额外引入 scale/offset 输入；PRENORM/POSTNORM 会额外引入残差输入与残差输出。

#### 4.3.2 核心流程

输入个数表（来自 `GetInputNum`）：

| layerType | quantType | dynamicQuantType | 输入数 | 输入顺序 |
| --- | --- | --- | --- | --- |
| NORM | UNQUANT | — | 3 | x, gamma, beta |
| NORM | INT8 | UNDEFINED | 5 | x, gamma, beta, scale, offset |
| NORM | INT8 | SYMMETRIC/ASYMMETRIC | 3 | x, gamma, beta（scale/offset 改为输出） |
| PRENORM | UNQUANT | — | 4 | x, residual, gamma, beta |
| POSTNORM | UNQUANT | — | 4 | x, residual, gamma, beta |
| POSTNORM | INT8 | — | 6 | x, residual, gamma, beta, scale, offset |

输出个数表（来自 `GetOutputNum`）：

| layerType | quantType | dynamicQuantType | 输出数 | 输出含义 |
| --- | --- | --- | --- | --- |
| NORM | UNQUANT | — | 1 | y |
| NORM | INT8 | UNDEFINED | 1 | y(int8) |
| NORM | INT8 | SYMMETRIC | 2 | y(int8), scale |
| NORM | INT8 | ASYMMETRIC | 3 | y(int8), scale, offset |
| PRENORM | — | — | 2 | y, residualOut |
| POSTNORM | UNQUANT | — | 1 | y |
| POSTNORM | INT8 | — | 2 | y(int8), residualOut?（见下） |

`InferShapeImpl` 的输出推导规则（基线规则：`out[0] = in[0]`，即与输入 x 同形状）：

1. **NORM + INT8**：把 `out[0].dtype` 改成 `ACL_INT8`。
   - 若 `dynamicQuantType == UNDEFINED`：只有 1 个输出，结束。
   - 否则（动态量化）：`out[1] = in[0]` 但 `dimNum--`（砍掉最后一维）、`dtype = ACL_FLOAT`，这是「逐 token 的 scale」。
   - 若是 `ASYMMETRIC`：再加 `out[2] = out[1]`（offset，同 scale 形状）。
2. **PRENORM**：`out[1] = in[0]`（残差通路透传）。
3. **POSTNORM + INT8**：`out[0].dtype = ACL_INT8`，`out[1] = in[0]`。

#### 4.3.3 源码精读

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:186-205](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L186-L205)：`GetInputNum`，逐分支返回上表的输入数。

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:207-229](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L207-L229)：`GetOutputNum`。

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:231-257](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L231-L257)：`InferShapeImpl`，是本模块的核心。234 行 `outTensorDescs.at(0) = inTensorDescs.at(0)` 即「基线规则」；235-246 行处理 NORM+INT8 的 dtype 与动态量化 scale/offset；240-241 行的 `outTensorDescs.at(1).shape.dimNum--` 正是「砍掉最后一维」的代码落点。

注意一个细节：gamma 在不同 `layerType` 下的位置不同。`InferShapeCheckImpl` 在 [src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:262-264](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L262-L264) 用三元运算符取 gamma：NORM 取 `in[1]`，PRE/POSTNORM 取 `in[2]`（因为前面多了一个 residual）。

#### 4.3.4 代码实践

**实践目标**：手算一个具体 Param 的输入输出个数与输出形状（对应讲义总实践任务的前半部分）。

**操作步骤**：

1. 设 Param：`layerType = LAYER_NORM_NORM`，`normParam.quantType = QUANT_INT8`，`normParam.dynamicQuantType = DYNAMIC_QUANT_SYMMETRIC`。
2. 设输入 x 形状为 `[2, 4, 8]`（dimNum=3）。
3. 对照 4.3.2 的两张表与推导规则，先写下你的预测：几个输入？几个输出？每个输出的形状与 dtype？

**需要观察的现象**：根据 `GetInputNum`，NORM+INT8+SYMMETRIC → 3 个输入；根据 `GetOutputNum` → 2 个输出；根据 `InferShapeImpl`：`out[0]` = `[2,4,8]` 但 dtype=`ACL_INT8`，`out[1]` = `[2,4]`（`dimNum--`）、dtype=`ACL_FLOAT`。

**预期结果**：输入 3 个、输出 2 个，输出形状如上。请把你的手算结果与源码逐一核对。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么动态量化场景下 scale 输出要 `dimNum--`（去掉最后一维）？
**答案**：scale 是「逐 token」的量化系数——对 x 的每个 token（每个 hidden 向量）算一个标量，因此维度比 x 少最后一维（hidden 维），其余 batch/seq 维度保留。

**练习 2**：`out[0] = in[0]` 这条基线规则意味着归一化算子的输出形状与输入完全一致，这对吗？
**答案**：对。归一化是元素级（按最后一维统计、逐元素缩放）操作，不改变张量形状，只可能改变 dtype（量化时变 int8）。

---

### 4.4 RmsNormOperation：Param 扩展、平台校验与 InferShape

#### 4.4.1 概念说明

`RmsNormOperation` 与 `LayerNormOperation` 结构同构（同样的 `layerType` 三态、同样的量化字段），但 `RmsNormParam` 多了几个 RMSNorm 专属维度：`precisionMode`（高精度/高性能）、`modelType`（LLaMA/Gemma 公式）、`rstd`（是否输出标准差倒数）。这些字段之间有互斥约束，且受芯片型号限制，因此 RMSNorm 的校验比 LayerNorm 更复杂。

#### 4.4.2 核心流程

创建流程：

```
CreateOperation(RmsNormParam)
  ├─ OP_PARAM_RSV_CHECK（4 个子结构）
  ├─ 若 Ascend950：预加载三类 aclnn 算子库
  ├─ EpsilonCheck（四个子结构的 epsilon 都不能为 0）
  ├─ RmsNormTypeCheck（按芯片型号限制 layerType × quantType 组合）
  ├─ 拒绝 NORM + DYNAMIC_QUANT_ASYMMETRIC
  └─ new RmsNormOperation(opParam)
```

`RmsNormTypeCheck` 是平台相关的关键函数：910A、310B、950 各自有不同的 `layerType × quantType` 白名单，组合不合法时返回详细的 `ExternalError`（含 errorDesc/errorData/solutionDesc 三段式错误信息）。

输出推导规则（基线仍为 `out[0] = in[0]`）：

1. **NORM + INT8**：`out[0].dtype = ACL_INT8`；若动态量化，`out[1] = in[0]` 去 last dim 且 `ACL_FLOAT`；ASYMMETRIC 再加 `out[2] = out[1]`。
2. **NORM + rstd**：额外输出 `out[rstd] = in[x]`、`dtype=ACL_FLOAT`，并把「gamma 覆盖到的那些维度」在 rstd 输出里置为 1。例如 x 为 `[B,S,H]`、gamma 为 `[H]`，则 rstd 形状为 `[B,S,1]`。
3. **PRENORM + INT8**：`out[0].dtype=INT8`，`out[1] = in[0]`。
4. **PRENORM + UNQUANT**：`out[1] = in[0]`（残差透传）。
5. **POSTNORM + INT8**：`out[1] = in[0]`，`out[0].dtype=INT8`。

#### 4.4.3 源码精读

[include/atb/infer_op_params.h:798-830](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L798-L830)：`RmsNormParam::NormParam`，注意 `rstd`、`precisionMode`、`modelType`、`dynamicQuantType` 四个字段及它们的注释里的互斥关系（如 rstd 不支持与量化/precisionMode/modelType 同时开启）。

[src/ops/ops_infer/rms_norm/rms_norm_operation.cpp:75-125](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rms_norm/rms_norm_operation.cpp#L75-L125)：`RmsNormTypeCheck`。79 行 `Is910A()`、92 行 `Is310B()` 分别走不同的合法组合校验，体现了「同一算子在不同芯片上能力不同」。

[src/ops/ops_infer/rms_norm/rms_norm_operation.cpp:290-329](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rms_norm/rms_norm_operation.cpp#L290-L329)：`InferShapeImpl`。305-313 行是 rstd 输出的形状推导核心——先复制 x，再把 gamma 维度对应的下标置 1：

```cpp
for (size_t i = 0; i < gammaDimNum; i++) {
    outTensorDescs[OUT_TENSOR_RSTD].shape.dims[xDimNum - gammaDimNum + i] = 1;
}
```

[src/ops/ops_infer/rms_norm/rms_norm_operation.cpp:473-486](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rms_norm/rms_norm_operation.cpp#L473-L486)：`CheckRstd`，说明 `rstd` 仅在 910B（Atlas 800I A2 推理产品）且非量化时支持。

#### 4.4.4 代码实践

**实践目标**：手算 rstd 输出的形状（RMSNorm 相比 LayerNorm 独有的推导）。

**操作步骤**：

1. 设 Param：`layerType = RMS_NORM_NORM`，`normParam.quantType = QUANT_UNQUANT`，`normParam.rstd = true`。
2. 设 x 形状 `[3, 5, 7]`，gamma 形状 `[7]`（即 `gammaDimNum = 1`）。
3. 对照 305-313 行的循环，推算 rstd 输出形状。

**需要观察的现象**：循环把 `xDimNum - gammaDimNum + i = 3-1+0 = 2` 这一维置 1，其余不变；dtype 为 `ACL_FLOAT`。所以 rstd 输出形状为 `[3, 5, 1]`。同时 `GetOutputNum` 因 `rstd=true` 返回 2。

**预期结果**：两个输出，`out[0]=[3,5,7]`（原 dtype），`out[1]=[3,5,1]`（float，即 rstd）。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`HIGH_PERFORMANCE_MODE` 为什么只支持 float16 输入？
**答案**：高性能模式用 float16 做中间累计，输入也必须是 float16 才能避免额外类型转换，从而真正拿到性能收益（见 `DtypeCheck`，[rms_norm_operation.cpp:415-417](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rms_norm/rms_norm_operation.cpp#L415-L417)）。

**练习 2**：rstd 输出为什么要把 gamma 对应维度置 1？
**答案**：rstd 是「每个 token 一个标量」，与 hidden 维（gamma 所在维）无关，因此该维被压缩为 1，保留 batch/seq 维度信息，供反向计算复用。

---

### 4.5 Runner 分派、变体与统一组图

#### 4.5.1 概念说明

归一化算子的 `InferShapeImpl` 只是「Host 侧形状推导」，真正的 NPU 计算由 Runner 完成。两个算子的 `CreateRunner` 都遵循同一条分派规律：**Ascend950（A3 推理卡）走 aclnn 后端（直接调 CANN 融合算子），其它芯片走 ops 后端（用 KernelGraph 自己拼）**。这与 u4-l1 讲过的 Linear、u3-l3 讲过的 AclnnRunner 是同一套机制。

此外，ATB 还提供了 `CohereLayerNorm` 变体——它是 Command R Plus 模型专用的归一化（把最后一维归一化到 \([0,1]\)），公式与标准 LayerNorm 不同，因此独立成一个算子，而不是塞进 `LayerNormParam`。

#### 4.5.2 核心流程

Runner 分派规律：

```
CreateRunner(param)
  ├─ Ascend950 + (NORM + UNQUANT 或 RMS 对应分支) → AclnnRunner 子类
  └─ 其它芯片                                       → OpsRunner 子类（KernelGraph）
```

ops 后端的组图方式非常简洁：**一个 `NormOperation` 节点**，通过不同的 in/out 张量挂接来表达 NORM/PRENORM/POSTNORM 与量化变体。也就是说，「不同变体 = 同一个底层 kernel 节点 + 不同张量接线」，这与 u3-l2 讲的 KernelGraph 思想一致。

#### 4.5.3 源码精读

[src/ops/ops_infer/layer_norm/layer_norm_operation.cpp:348-357](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_operation.cpp#L348-L357)：`LayerNormOperation::CreateRunner`。351-354 行 Ascend950 + NORM + UNQUANT 返回 `LayerNormAclnnRunner`，否则 356 行返回 `LayerNormOpsRunner`。

[src/ops/ops_infer/rms_norm/rms_norm_operation.cpp:488-508](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/rms_norm/rms_norm_operation.cpp#L488-L508)：`RmsNormOperation::CreateRunner`。950 上按 NORM/PRENORM 与量化分派到 `RmsNormAclnnRunner` / `RmsNormQuantAclnnRunner` / `AddRmsNormAclnnRunner`，否则 507 行返回 `RmsNormOpsRunner`。

[src/ops/ops_infer/layer_norm/layer_norm_ops_runner.cpp:80-102](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/layer_norm/layer_norm_ops_runner.cpp#L80-L102)：`BuildLayerNormGraph`，组图核心。注意它只有 1 个节点：

```cpp
layerNormNode.opDesc = {0, "NormOperation", layerNormParam};
layerNormNode.inTensors  = {&inputXTensor, &gammaTensor, &betaTensor};
layerNormNode.outTensors = {&resultTensor, &meanTensor, &variencetTensor};
```

3 个输入（x/gamma/beta）、3 个输出（结果 + mean/variance 两个内部张量）。这里的 `"NormOperation"` 字符串就是 u3-l4 讲过的注册衔接点——它对应 Kernel 层 `REG_OPERATION` 注册的算子名。PRENORM/POSTNORM/量化变体复用同名节点，只改 in/out 接线（见同文件的 `BuildPreLayerNormGraph` 等）。

[src/ops/ops_infer/cohere_layernorm/cohere_layernorm_operation.cpp:57-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/cohere_layernorm/cohere_layernorm_operation.cpp#L57-L80)：`CohereLayerNormOperation`。它只有 2 输入（x, gamma）、1 输出，`InferShapeImpl` 仍是 `out[0]=in[0]`；构造函数 41 行限定 `Is910B()`，即仅 910B 支持。其 Param `CohereLayerNormParam` 见 [include/atb/infer_op_params.h:2661-2673](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L2661-L2673)，只有一个 `epsilon` 字段。

#### 4.5.4 代码实践

**实践目标**：理解「同一底层节点 + 不同接线」如何表达多种变体。

**操作步骤**：

1. 打开 `src/ops/ops_infer/layer_norm/layer_norm_ops_runner.cpp`，对比 `BuildLayerNormGraph`（80-102 行）与 `BuildPreLayerNormGraph`（同文件，搜索函数名定位）。
2. 记录两者的 `opDesc` 字符串与 in/out 张量列表差异。

**需要观察的现象**：两者的 `opDesc` 都是 `"NormOperation"`（同一个底层 kernel），区别只在 in/out 张量接线——PRENORM 多挂了 residual 输入与残差输出。

**预期结果**：你应当得出结论：归一化的多种变体在 ops 后端被收敛到同一个 `NormOperation` kernel，变体差异完全由 Param 标志位（如 `inRes`、`outRes`）和张量接线表达，这正是融合算子降低 kernel 数量的体现。运行结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Ascend950 优先用 aclnn 后端而不是 ops 后端？
**答案**：aclnn 后端直接调用 CANN 预编译的高性能融合算子，省去 Host 侧组图与 Tiling 开销，能更好缓解 Host Bound（u3-l3 已讲 AclnnRunner 的 executor 缓存复用机制）。

**练习 2**：`CohereLayerNorm` 为什么不做成 `LayerNormParam` 的一个 `layerType`？
**答案**：它的归一化公式本质不同（归一化到 \([0,1]\)，而非零均值单位方差），输入只有 x/gamma 两项、没有 beta，校验与 Runner 都和标准 LayerNorm 差异较大；独立成算子更清晰，也避免让 `LayerNormParam` 承载语义冲突的字段。

---

## 5. 综合实践

本任务把本讲全部内容串起来，对应讲义规格里的总实践：**「阅读两个 normalization operation，写出它们 InferShapeImpl 的输出推导规则」**。

**任务背景**：你要为一个新接入的 Transformer 模型选归一化算子，需要在不跑代码的前提下，凭 Param 与输入形状预测输出，以正确分配显存。

**操作步骤**：

1. 阅读两个核心文件：
   - `src/ops/ops_infer/layer_norm/layer_norm_operation.cpp` 的 `InferShapeImpl`（231-257 行）
   - `src/ops/ops_infer/rms_norm/rms_norm_operation.cpp` 的 `InferShapeImpl`（290-329 行）
2. 用一张表分别写出 LayerNorm 与 RMSNorm 的 `InferShapeImpl` 输出推导规则（提示：按 4.3.2、4.4.2 的格式）。
3. 用以下三组测试用例验证你的规则表，写出每个输出的形状与 dtype：
   - **用例 A**：LayerNorm，`NORM + QUANT_INT8 + DYNAMIC_QUANT_UNDEFINED`，x = `[1, 6, 16]`。
   - **用例 B**：RMSNorm，`NORM + QUANT_UNQUANT + rstd=true`，x = `[1, 6, 16]`，gamma = `[16]`。
   - **用例 C**：LayerNorm，`PRENORM + QUANT_UNQUANT`，x = `[2, 3, 32]`。
4. 给每个用例补上「几个输入、几个输出」的判断（调用 `GetInputNum`/`GetOutputNum` 的逻辑）。

**参考答案要点**：

- 用例 A：输入 5 个（x/gamma/beta/scale/offset），输出 1 个 `[1,6,16]`、dtype=`ACL_INT8`。
- 用例 B：输入 2 个（x/gamma），输出 2 个：`out[0]=[1,6,16]`（原 dtype）、`out[1]=[1,6,1]`（rstd，`ACL_FLOAT`）。
- 用例 C：输入 4 个（x/residual/gamma/beta），输出 2 个：`out[0]=[2,3,32]`、`out[1]=[2,3,32]`（残差透传）。

**预期结果**：你能不看源码、仅凭自己整理的规则表推出上述答案；然后回源码核对。若想进一步验证，可在昇腾环境用 torch_atb（u2-l2）构造对应 Param 与张量实际执行，但运行结果「待本地验证」。

## 6. 本讲小结

- 归一化是 Transformer 每层必做的高频操作：LayerNorm 减均值除标准差、带 \(\gamma\)/\(\beta\)；RMSNorm 只用均方根、通常只有 \(\gamma\)，更轻量，\(\epsilon\) 用于防除零且被强校验。
- `LayerNormParam` / `RmsNormParam` 都用「`layerType` 三态（NORM/PRENORM/POSTNORM）+ 量化字段」组合表达多种结构，而不是拆成多个算子；`layerType` 决定用哪个子 Param、走哪套校验。
- 输入输出个数由 `layerType × quantType × dynamicQuantType`（RMSNorm 还加 `rstd`/`hasBias`）共同决定，`GetInputNum`/`GetOutputNum` 是 `VariantPack` 装填的依据。
- `InferShapeImpl` 的基线规则是 `out[0]=in[0]`，再按量化（改 int8、加逐 token 的 scale/offset，砍最后一维）、PRENORM（残差透传）、RMSNorm rstd（gamma 维置 1）做增量修改。
- Runner 分派规律：Ascend950 走 aclnn 后端、其它芯片走 ops 后端；ops 后端把 NORM/PRENORM/POSTNORM 与量化变体都收敛到同一个 `NormOperation` kernel 节点，靠张量接线与 Param 标志位区分。
- `CohereLayerNorm` 是 Command R Plus 风格的独立归一化变体（归一化到 \([0,1]\)），仅 910B 支持，因公式与标准 LayerNorm 不同而独立成算子。

## 7. 下一步学习建议

- **横向对比其它「字段组合型」算子**：本讲的 `layerType` 三态与 u4-l1 的 `LinearParam` 是同一种设计哲学，建议重读 u4-l1，体会 ATB「单 Param 覆盖 N 种行为」的统一风格。
- **进入激活与元素级算子（u4-l3）**：归一化之后通常是激活函数（GELU/SiLU）。下一讲会讲 `ActivationOperation` 与 `ElewiseOperation`，它们常与归一化在图算子里串联使用（见 u5 图算子机制）。
- **深入 Runner 与 Kernel**：若想看清 ops 后端的 `NormOperation` 节点最终如何落到 AscendC kernel，可预习 u3-l4（Kernel 层与 MKI 框架），并在 `src/kernels` 下搜索 `NormOperation` 的注册实现。
- **尝试组合**：学完 u4-l3 后，用 u5 的 `GraphOpBuilder` 把 `RmsNorm + Linear + Activation` 串成一个图算子，观察「统一调度、复用 workspace」带来的收益。
