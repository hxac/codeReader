# aclnn 两阶段 API 与基础概念

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 aclnn 两阶段接口（`GetXxxWorkspaceSize` + `aclnnXxx`）各自做什么、为什么拆成两段、workspace 和 executor 在其中扮演什么角色。
2. 以 `aclnnFlashAttentionScore` 为样本，把一个工业级算子的两段调用参数逐个拆解归类（输入 tensor、属性、输出 tensor、出参）。
3. 理解 aclTensor 用 \( (shape, strides, offset) \) 三要素描述内存排布的方式，以及算子内部对非连续输入的处理策略。
4. 掌握 aclnn 返回码分段规律（161xxx 参数错 / 361xxx runtime 错 / 561xxx 内部错），能根据返回码值快速定位问题层次，并用 `aclGetRecentErrMsg` 拿到具体报错文案。

## 2. 前置知识

本讲建立在你已完成第二单元（尤其是 u2-l4「运行算子示例」）的基础上，这里把要用到的旧概念快速复盘，并补充两个新概念。

- **aclnn 接口**：昇腾算子库（ACL NN）暴露给用户的 C 接口，命名形如 `aclnn + 算子名`。每个算子的接口在 `op_api` 目录的 `.h/.cpp` 中声明和实现。
- **aclTensor**：aclnn 世界里的「张量描述符」，由 `aclCreateTensor` 创建，包含地址、shape、strides、offset、dtype、format 等信息。它不拥有内存，只描述内存。
- **stream（aclrtStream）**：NPU 上的任务队列。第二段接口把计算任务异步下发到 stream，需要 `aclrtSynchronizeStream` 等待完成。
- **executor（aclOpExecutor）**：本讲新概念。第一段接口的产物之一，可以理解为「把本次调用的全部上下文打包好的执行计划」——里面记录了要跑哪个 kernel 变体、tiling 结果、中间插入了哪些辅助算子。第二段接口只拿它去下发任务，不再校验参数。
- **workspace**：本讲新概念。除输入/输出外，算子在 NPU 上完成计算所需要的**临时内存**。比如矩阵乘分块计算时存放中间分片的缓冲区就在 workspace 里。

一个直观的类比：第一段接口像「下单前的询价+备料单」（校验参数、推导 shape、算 tiling、给出需要多大临时空间），第二段接口像「开工」（把活儿派给 NPU）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/context/two_phase_api.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/two_phase_api.md) | 两段式接口的官方定义：调用顺序约束、workspace 含义、禁止重复调第二段 |
| [docs/zh/context/data_type.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/data_type.md) | aclDataType 与接口文档简写（FLOAT16、BF16 等）的对照表 |
| [docs/zh/context/non_contiguous_tensor.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/non_contiguous_tensor.md) | 非连续 tensor 的 \( (shape, strides, offset) \) 表示法与两个内存排布示例 |
| [docs/zh/context/aclnn_return_code.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/aclnn_return_code.md) | aclnn 返回码总表：参数错、runtime 错、内部错三大类 |
| [docs/zh/context/compile_and_run_sample.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md) | 以 FA 为例的完整调用样例：CMakeLists 链接库、编译运行、报错信息获取 |
| [attention/flash_attention_score/op_api/aclnn_flash_attention_score.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h) | FA 家族全部两段式接口的声明，是「拆参数」实践的文本依据 |
| [attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp) | FA 两段式接口的实现，本讲用来精读第一段内部的校验/连续化流程 |
| [attention/flash_attention_score/docs/aclnnFlashAttentionScore.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/docs/aclnnFlashAttentionScore.md) | FA 算子的接口说明文档，内含完整可编译的调用示例代码 |

## 4. 核心概念与源码讲解

### 4.1 两阶段 API 与 workspace 机制

#### 4.1.1 概念说明

基于单算子 API（aclnn）执行方式调用算子时，每个算子都拆成两个 C 函数：

- **第一段 `aclnnXxxGetWorkspaceSize`**：吃进全部输入/输出 tensor 和属性，做参数校验、shape 推导、tiling 计算，最终通过两个出参返回 `workspaceSize`（需要多大临时内存）和 `executor`（打包好的执行计划）。
- **第二段 `aclnnXxx`**：只有 4 个固定参数——`workspace` 指针、`workspaceSize`、第一段产出的 `executor`、目标 `stream`。它把任务异步下发到 stream，立即返回。

为什么拆两段？核心动机是**内存管理权交给调用方**：算子内部知道自己需要多少临时空间，但 NPU 设备内存的申请/释放必须由用户代码主导（应用往往有自己的内存池）。于是第一段「报价」，用户按报价从自己的池子里出内存，第二段「施工」。另一个附带好处是第一段把所有可能失败的校验都前置了，第二段几乎不会因参数问题失败，适合被框架反复异步调度。

三条硬性规则（来自官方文档）：

1. 必须先调第一段，拿到 `workspaceSize` 后申请 NPU 内存，再调第二段。
2. 第二段**不能重复调用**——同一个 executor 只能触发一次执行。
3. `workspaceSize` 为 0 时可以不申请内存直接传 `nullptr` 进第二段。

#### 4.1.2 核心流程

标准调用骨架（伪代码）：

```text
初始化: aclInit -> aclrtSetDevice -> aclrtCreateContext -> aclrtCreateStream
构造输入: Host 数据 -> aclrtMalloc + aclrtMemcpy -> aclCreateTensor
第一段:   aclnnXxxGetWorkspaceSize(..., &workspaceSize, &executor)
          若失败: 读返回码 / aclGetRecentErrMsg 排查
备料:     若 workspaceSize > 0: aclrtMalloc(&workspaceAddr, workspaceSize, ...)
第二段:   aclnnXxx(workspaceAddr, workspaceSize, executor, stream)
同步:     aclrtSynchronizeStream(stream)
取回:     aclrtMemcpy 输出 device -> host
清理:     aclDestroyTensor / aclrtFree / 销毁 stream/context/device
```

对应到数据流：

\[ \text{用户参数} \xrightarrow{\text{第一段}} \{\text{校验},\ \text{infershape},\ \text{tiling}\} \rightarrow (\text{workspaceSize},\ \text{executor}) \xrightarrow{\text{用户申请内存}} \text{第二段} \rightarrow \text{stream 上的 kernel 任务} \]

注意：第一段不占用 stream，可以在任务下发前的任意时刻完成；只有第二段真正入队。

#### 4.1.3 源码精读

**（1）两段式的官方定义**。[docs/zh/context/two_phase_api.md:L5-L12](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/two_phase_api.md#L5-L12) 给出了通用原型：第一段以 `..., uint64_t *workspaceSize, aclOpExecutor **executor` 结尾（两个出参），第二段固定为 `(void *workspace, uint64_t workspaceSize, aclOpExecutor *executor, aclrtStream stream)`。[L14-L23](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/two_phase_api.md#L14-L23) 的说明块补充了 workspace 的定义（算子完成计算所需的临时内存）以及第二段不可重复调用的约束。

**（2）真实算子的两段声明**。以 FA 为例，[aclnn_flash_attention_score.h:L24-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L24-L36) 声明了：

- 第一段 `aclnnFlashAttentionScoreGetWorkspaceSize`：约 20 个参数，涵盖 query/key/value 等 7 个 tensor 输入、prefix 数组、9 个标量属性、4 个输出 tensor，最后是 `workspaceSize` 和 `executor` 两个出参。
- 第二段 `aclnnFlashAttentionScore`：只有 `(workspace, workspaceSize, executor, stream)` 四个参数。

对照这个头文件还能看到一个工程现象：同一个 `.h` 里并排声明了 V1~V5、VarLen、Quant 等十几个版本的两段式接口（如 [L102-L137](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L102-L137) 的 V3、[L144-L190](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L144-L190) 的 V4），每个版本的第一段参数逐版本增多——这正是「新增能力以新版本接口交付、旧版本保持不动」演进策略的直接体现。

**（3）第一段内部做了什么**。看实现 [aclnn_flash_attention_score.cpp:L1102-L1196](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1102-L1196)，第一段的骨架可以拆成五步：

1. **判空**（[L1110-L1111](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1110-L1111)）：`CheckFaParam` 校验必选指针，失败返回 `ACLNN_ERR_PARAM_NULLPTR`。
2. **创建 executor**（[L1118-L1119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1118-L1119)）：`CREATE_EXECUTOR()` 失败返回 `ACLNN_ERR_INNER_CREATE_EXECUTOR`。
3. **空输出早退**（[L1123-L1127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1123-L1127)）：当 b/n1/s1 维为 0（输出全空）时直接置 `workspaceSize = 0` 成功返回——这是"什么都不用算"的合法路径，也解释了为什么 workspace 可能为 0。
4. **校验 + 预处理 + 生成 l0 算子序列**（[L1134-L1155](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1134-L1155)）：format 检查、dtype 检查、shape 分析、Contiguous 连续化（见 4.3 节）、QKV 预处理，然后调用 `l0op::FlashAttentionScore` 在 executor 里记录真正要执行的 l0 算子。
5. **回写输出 + 报价**（[L1176-L1195](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1176-L1195)）：用 `l0op::ViewCopy` 把内部输出搬到用户给的输出 tensor 上，最后 `*workspaceSize = uniqueExecutor->GetWorkspaceSize();` 完成报价，`uniqueExecutor.ReleaseTo(executor)` 把 executor 所有权交给调用方。

**（4）第二段只有一行实质逻辑**。[aclnn_flash_attention_score.cpp:L1198-L1204](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1198-L1204)：注释写着「固定写法，调用框架能力，完成计算」，直接透传给 `CommonOpExecutorRun(workspace, workspaceSize, executor, stream)`。所有定制都在第一段，第二段对所有算子几乎长得一样——这是判断「两段式」结构最可靠的源码特征。

#### 4.1.4 代码实践

**实践目标**：把 `aclnnFlashAttentionScore` 的两段调用参数逐个拆解归类，并走通一次完整调用。

**操作步骤**：

1. 打开 [aclnnFlashAttentionScore.md:L598-L613](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/docs/aclnnFlashAttentionScore.md#L598-L613) 的示例代码，这是仓库自带的完整 FA 调用样例。
2. 对照头文件声明 [aclnn_flash_attention_score.h:L24-L30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L24-L30)，手工填写下面这张参数分类表：

| 类别 | 参数 | 说明 |
| --- | --- | --- |
| 输入 tensor | query, key, value | 主输入 Q/K/V |
| 可选输入 tensor | realShiftOptional, dropMaskOptional, paddingMaskOptional, attenMaskOptional | 传 nullptr 即不启用 |
| 输入数组属性 | prefixOptional（aclIntArray） | 可选前缀索引 |
| 标量属性 | scaleValue, keepProb, preTokens, nextTokens, headNum, inputLayout, innerPrecise, sparseMode | 缩放系数、dropout 概率、稀疏模式等 |
| 输出 tensor | softmaxMaxOut, softmaxSumOut, softmaxOutOut, attentionOutOut | softmax 中间量 + 注意力输出 |
| 出参 | workspaceSize, executor | 第一段的两个"返回值" |

3. 按 [compile_and_run_sample.md:L36-L89](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md#L36-L89) 的 CMakeLists 建工程（注意要链接 `libopapi_math.so`，因为 FA 的 op_api 内部调用了 L0 接口），再按 [L114-L148](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md#L114-L148) 的步骤 `source set_env.sh` 后编译运行。

**需要观察的现象**：

- 第一段调用返回后 `workspaceSize` 的值是多少（FA 常见为非 0，因为内部有分块缓冲）。
- 第二段调用后紧接的 `aclrtSynchronizeStream` 之前输出是否可用（应当不可用，第二段是异步的）。

**预期结果**：程序输出与 [compile_and_run_sample.md:L150-L164](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md#L150-L164) 一致的 `result[x] is: 256.000000` 序列（全 1 输入、特定 mask 配置下的理论值）。若无法在 NPU 上运行，可对照文档核对代码逻辑，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把第二段接口对同一个 executor 连续调用两次，会发生什么？为什么？

**答案**：官方文档 [two_phase_api.md:L17-L23](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/two_phase_api.md#L17-L23) 明确指出会出现异常。executor 中记录的是一次性执行计划（含中间缓冲的分配状态），第二次下发会导致重复执行/状态错乱，属于未定义行为。需要再算一次必须重新走第一段拿新 executor。

**练习 2**：为什么 `aclnnFlashAttentionScore` 的第二段只有 4 个参数，而第一段有 20 多个？

**答案**：第一段的所有参数校验、shape/tiling 计算结果都被打包进了 executor；第二段只需知道「在哪块临时内存（workspace，多大）上、按哪份计划（executor）、派到哪个队列（stream）执行」。这也是源码中第二段实现只有一行 `CommonOpExecutorRun` 透传的原因（[aclnn_flash_attention_score.cpp:L1198-L1204](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1198-L1204)）。

**练习 3**：FA 第一段在什么情况下会返回 `workspaceSize = 0` 且直接成功？

**答案**：当三个输出 tensor 全部为空（`softmaxMaxOut->IsEmpty() && ...`，对应 batch/head/序列维为 0，即没有实际计算量）时，[aclnn_flash_attention_score.cpp:L1121-L1127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1121-L1127) 直接置 0 返回成功。

### 4.2 数据类型与格式约定

#### 4.2.1 概念说明

aclTensor 创建时必须指定 `aclDataType`（如 `ACL_FLOAT16`、`ACL_BF16`）和 format（如 `ACL_FORMAT_ND`）。接口文档为了简洁，用简写表示 dtype：`FLOAT16` 即 `ACL_FLOAT16`、`BF16`/`BFLOAT16` 即 `ACL_BF16`，不区分大小写。这层约定很重要，因为**算子支持的 dtype 白名单是硬约束**：u2-l1 已建立「def 文件的 AddConfig 是能力边界」的认知，落到调用侧就是——传入白名单外的 dtype，第一段会以 `ACLNN_ERR_PARAM_INVALID`（161002）拒绝。

值得注意的是简写表中已经出现了大量低精度类型（[data_type.md:L29-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/data_type.md#L29-L36) 的 `FLOAT8_E5M2`、`FLOAT8_E4M3FN`、`FLOAT4_E2M1` 等），它们是第四单元量化 attention 算子的输入类型，本讲先混个脸熟。

#### 4.2.2 核心流程

调用方视角的 dtype 决策链：

```text
读算子接口文档的 dtype 约束（如 query: FLOAT16/BF16）
  -> 用对应 aclDataType 枚举 aclCreateTensor
  -> 第一段内部做 InputDtypeCheck 之类的校验
  -> 不匹配 => 返回 ACLNN_ERR_PARAM_INVALID (161002)，匹配 => 继续
```

#### 4.2.3 源码精读

**（1）简写对照表**。[data_type.md:L8-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/data_type.md#L8-L36) 是完整的「原始枚举 ↔ 文档简写」映射，全量合法值以 Runtime API 文档为准（[L3](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/data_type.md#L3)）。

**（2）FA 中的 dtype 校验实例**。[aclnn_flash_attention_score.cpp:L1138](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1138) 调用 `InputDtypeCheck(query, key, value, attentionOutOut, realShiftOptional, PSE_TYPE_V1, sinkOptional)`，任何一个 tensor 的 dtype 不在支持列表内即返回 `ACLNN_ERR_PARAM_INVALID`。注意这条校验位于 format 检查之后、shape 分析之前——第一段内部的校验顺序本身就是「先指针、再 format、再 dtype、再 shape」的漏斗。

**（3）调用示例中的 dtype 使用**。示例代码 [aclnnFlashAttentionScore.md:L566-L573](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/docs/aclnnFlashAttentionScore.md#L566-L573) 用 `aclDataType::ACL_FLOAT` 创建 q/k/v，用 `ACL_UINT8` 创建 attenmask——mask 类输入用 uint8 布尔语义是 attention 类算子的常见约定。

#### 4.2.4 代码实践

**实践目标**：体会 dtype 白名单的拦截行为。

**操作步骤**：

1. 复用 4.1.4 的示例工程，把 `q` 的创建改为 `aclDataType::ACL_DOUBLE`（其余不动），重新编译运行。
2. 观察第一段调用的返回码。

**需要观察的现象 / 预期结果**：第一段返回非 0（参数校验失败，161002 一类），`aclGetRecentErrMsg` 会打印具体是哪个 tensor 的 dtype 不支持。FA 不支持 DOUBLE 输入。若手头无 NPU 环境，此实践标注「待本地验证」，可改为在源码 [aclnn_flash_attention_score.cpp:L1138](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1138) 处走读 `InputDtypeCheck` 的实现，列出其允许的 dtype 集合作为替代任务。

#### 4.2.5 小练习与答案

**练习 1**：接口文档里写着某输入支持 `BF16`，代码里应该传什么枚举？

**答案**：`ACL_BF16`。对照 [data_type.md:L25](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/data_type.md#L25)，`ACL_BF16` 的简写是 `BF16` 或 `BFLOAT16`，简写不区分大小写。

**练习 2**：为什么简写表里有一批 FP8/FP6/FP4 类型？它们和第四单元有什么关系？

**答案**：这些是低比特浮点类型，服务于大模型量化训练/推理。第四单元将精读的 `quant_flash_attn` 全量化 attention 算子就以 `FLOAT8_E4M3FN` 等作为 Q/K/V 的输入 dtype，届时 dtype 约束会和量化模式（scale 的 per-tensor/per-axis 粒度）绑定在一起。

### 4.3 非连续 tensor：strides、offset 与自动连续化

#### 4.3.1 概念说明

大部分算子 API 的输入 aclTensor 支持「非连续的 Tensor」：一个 tensor 不必占用一整块紧凑内存，而是用三元组 \( (shape, strides, offset) \) 描述——

- \( shape \)：逻辑形状；
- \( strides \)：每个维度上相邻两个逻辑元素的内存间隔（单位：元素个数）；
- \( offset \)：首元素相对基础地址 `addr` 的偏移。

判断某一维是否连续：该维 stride 为 1 即连续。PyTorch 中 `tensor.transpose(0,1)` 得到的就是典型非连续 tensor（shape 交换了但底层内存没动，strides 跟着交换），而 `tensor.t().contiguous()` 会触发一次真实的数据重排。

为什么算子要支持非连续输入？因为框架（PyTorch/GE）里 view、transpose、slice 操作只改 \( (shape, strides, offset) \) 不搬数据；如果算子一律要求连续，每个 slice/transpose 后都得插入一次昂贵的数据搬运。

#### 4.3.2 核心流程

以 [non_contiguous_tensor.md:L25-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/non_contiguous_tensor.md#L25-L36) 的示例 2 为例，shape=\( (4,3) \)、strides=\( (20,2) \)、offset=22 的 tensor，元素 \( a_{i,j} \) 的内存地址为：

\[ \text{index}(i,j) = offset + i \times strides[0] + j \times strides[1] = 22 + 20i + 2j \]

即从一块 \( 10\times10 \) 的底层内存中，隔行隔列地"挖"出一块逻辑上的 \( 4\times3 \) 矩阵（文档图中深色位置）。示例 1（strides=\( (10,1) \)、offset=22）则是「行内连续、行间跳跃」的部分非连续。

算子内部对非连续输入有两种典型策略：

```text
策略 A（自动连续化）: 在第一段把 Contiguous 类 l0 算子插入 executor，
                      运行时先搬成连续再计算 —— 对用户透明，但多一次拷贝
策略 B（直接报错）:   第一段校验 strides 不满足要求即返回参数错误 —— 用户需自行 .contiguous()
```

#### 4.3.3 源码精读

**（1）FA 采用策略 A**。[aclnn_flash_attention_score.cpp:L685-L706](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L685-L706) 的静态辅助函数 `Contiguous` 对 query/key/value 逐个调用 `l0op::Contiguous(tensor, executor)`：若输入已连续，l0 层通常会直接原样返回；若不连续，则向 executor 中登记一个拷贝动作，并把引用改指向连续化后的新 tensor（注意参数是 `const aclTensor *&`，引用传参以便回写）。可选输入（realShift、dropMask 等，[L707-L714](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L707-L714)）先判空再连续化。这个函数在第一段主流程的 [L1146-L1148](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1146-L1148) 被调用——也就是说**非连续输入的处理发生在第一段（计划期），真正搬运发生在第二段（执行期）**。

**（2）shrink 到 output 的反向问题**。FA 的输出还要从内部连续 tensor 搬回用户给的可能非连续的输出 tensor，靠的是 [L1179-L1191](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1179-L1191) 的三个 `l0op::ViewCopy`——输入侧 Contiguous、输出侧 ViewCopy，合起来保证「任意合法 \( (shape, strides, offset) \) 的输入输出都能用」。

**（3）自动连续化的代价归属**。连续化/ViewCopy 这些辅助算子同样需要临时内存，它们的大小被计入 `uniqueExecutor->GetWorkspaceSize()`（[L1193](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1193)）——这就是「workspace 报价」里不可忽视的一部分：非连续输入越多，workspace 越大、执行期多出的拷贝越多。

#### 4.3.4 代码实践

**实践目标**：构造非连续输入调用 FA，观察行为差异。

**操作步骤**：

1. 在 4.1.4 的示例工程基础上，先把 q/k/v 正常创建并验证结果正确（基线）。
2. 修改 `CreateAclTensor` 的封装：改为用自定义 strides、offset 重新 `aclCreateTensor`（参照 [non_contiguous_tensor.md:L9-L21](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/non_contiguous_tensor.md#L9-L21) 示例 1 的 strides=\( (10,1) \) 形式，在真实 shape 之外多申请一块内存作为底层缓冲，把数据按目标排布写入后再创建 tensor），构造一个「逻辑 shape 不变、底层非连续」的 query。
3. 重新运行，重点对比：返回码、输出数值、第一步 `GetWorkspaceSize` 报出的 workspaceSize。

**需要观察的现象**：

- 返回码是否仍为成功（FA 走自动连续化，预期成功）。
- 非连续版本的 workspaceSize 是否比连续基线更大（多了连续化拷贝缓冲）。
- 输出数值是否与基线一致（语义上必须一致）。

**预期结果**：调用成功、结果一致、workspace 变大。若某些非连续形态（如极端 strides）被 format/stride 校验拦截（[L1134-L1137](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1134-L1137) 的 `CheckFormat` 路径），则返回 `ACLNN_ERR_PARAM_INVALID` 并可用 `aclGetRecentErrMsg` 看到具体是哪个输入的 stride 不满足要求。无 NPU 环境时标注「待本地验证」，替代任务是走读 [L685-L781](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L685-L781) 列出 FA 全部会被自动连续化的 tensor 清单。

#### 4.3.5 小练习与答案

**练习 1**：一个 shape=\( (2,3) \)、strides=\( (3,1) \)、offset=0 的 tensor 是否连续？

**答案**：连续。按行主序紧凑排布时恰好 \( strides[0]=3=shape[1]\times strides[1] \)、\( strides[1]=1 \)，元素 \( index(i,j)=3i+j \) 覆盖 \( [0,6) \) 无空洞。判断标准：最后一维 stride 为 1，且每一维 stride 等于其后所有维度大小之积。

**练习 2**：FA 对非连续输入选择的策略是什么？代价由谁承担？

**答案**：策略 A（自动连续化），见 [aclnn_flash_attention_score.cpp:L692-L706](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L692-L706)。代价是执行期多一次 GM 拷贝、workspace 变大，由本次调用的 workspaceSize（用户申请的临时内存）承担。对性能敏感的场景应在进算子前自行做 `contiguous()`，避免隐藏拷贝。

### 4.4 返回码体系

#### 4.4.1 概念说明

aclnn 接口的返回值类型是 `aclnnStatus`（整数）。返回码按号段分三大类，这个分段规律本身就是排查思路：

| 号段 | 类别 | 含义 | 典型诱因 |
| --- | --- | --- | --- |
| 0 | 成功 | `ACLNN_SUCCESS` | — |
| 161xxx | 参数错 | 调用方传参有问题 | nullptr、dtype 不匹配、shape 不满足约束 |
| 361001 | runtime 错 | 内部调 NPU runtime 接口异常 | 设备状态、内存、stream 问题 |
| 561xxx | 内部错 | API 内部异常 | 算子包未装、json 加载失败、executor 创建失败等 |

排查第一原则：**161xxx 先检查自己的代码，561xxx/361xxx 先怀疑环境与安装**。所有异常码都可以配 `aclGetRecentErrMsg()` 拿到带算子名、参数名的详细文案，不要只盯着数字猜。

#### 4.4.2 核心流程

```text
ret = 第一段(...)
if ret != 0:
    msg = aclGetRecentErrMsg()        # 拿具体文案
    按号段分流:
      161001 (PARAM_NULLPTR)   -> 检查指针，哪个参数忘了建/传了 nullptr
      161002 (PARAM_INVALID)   -> 对照接口文档查 dtype/shape/属性取值
      561003 (FIND_KERNEL)     -> 算子二进制包未安装/版本不配套（回忆 u1-l3 版本配套）
      561112 (OPP_KERNEL_PKG)  -> 没加载到二进制 kernel 库，同上
      361001 (RUNTIME)         -> 检查 device/驱动/stream
```

#### 4.4.3 源码精读

**（1）返回码总表**。[aclnn_return_code.md:L8-L14](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/aclnn_return_code.md#L8-L14) 的表 1 给出五大基础码；[L20-L45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/aclnn_return_code.md#L20-L45) 的表 2 展开 561xxx 内部码。几个高频码值得记住：

- `561001 INFERSHAPE_ERROR` / `561002 TILING_ERROR`：内部 infershape/tiling 阶段出错——通常是输入 shape 触发了未覆盖的分支，报障时应附上完整 shape 信息。
- `561003 FIND_KERNEL_ERROR` 与 `561112 OPP_KERNEL_PKG_NOT_FOUND`：找不到 kernel 二进制，最常见原因是**算子包没装或 CANN/ops 包版本不配套**，直接对应 u1-l3 建立的版本配套约束。
- `561107 OPP_PATH_NOT_FOUND`：环境变量 `ASCEND_OPP_PATH` 未配置，`source set_env.sh` 没做。

**（2）返回码是怎么被抛出来的**。FA 第一段的判空函数 [aclnn_flash_attention_score.cpp:L991-L1016](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L991-L1016)：每个必选指针（query/key/value/inputLayout/executor/workspaceSize/各输出）都用 `OP_CHECK(cond, OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "The xxx cannot be nullptr"), return ...)` 守卫——条件不满足时记日志并返回 161001。而 [L1118-L1119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1118-L1119) 的 `CREATE_EXECUTOR` 失败对应 `ACLNN_ERR_INNER_CREATE_EXECUTOR`（561101）。可以看到：**返回码不是框架统一发的，而是 op_api 实现里逐点显式返回的**——读 op_api 源码时搜 `ACLNN_ERR_` 就能枚举出该算子所有失败路径。

**（3）报错信息的标准获取姿势**。[compile_and_run_sample.md:L166-L183](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md#L166-L183) 给出实机示例：故意传 nullptr 的 query，输出为

```text
aclnnFlashAttentionScoreGetWorkspaceSize failed. ERROR: 161001
[ERROR msg][PID:xxxx] xxx(timestamp) AclNN_Parameter_Error(EZ1001): The query cannot be nullptr.
```

返回码告诉你类别（161001 参数空指针），`aclGetRecentErrMsg` 的文案告诉你具体是 `query` 这个参数——两层信息合起来才能定位。

#### 4.4.4 代码实践

**实践目标**：制造三类典型故障，建立「返回码 → 排查动作」的肌肉记忆。

**操作步骤**：

1. **参数空指针**：把示例里的 `q` 置为 `nullptr` 后调第一段，观察返回码与 `aclGetRecentErrMsg` 输出（预期 161001，文案指出 query）。
2. **参数非法**：把 `headNum` 传 0（与实际 shape 不符），观察是否被 shape 分析阶段拦截（预期 161002 一类，待本地验证具体文案）。
3. **环境类**：临时 `unset ASCEND_OPP_PATH` 后运行，观察内部错误码（对照表 2 判断落在哪个 561xxx）。

**需要观察的现象 / 预期结果**：三组实验分别落在 161xxx、161xxx、561xxx 三个层次；每组的 `aclGetRecentErrMsg` 文案都能指明具体参数或缺失项。实验 3 做完后记得恢复 `source set_env.sh`。无 NPU 环境时全部标注「待本地验证」，替代任务是精读 [aclnn_flash_attention_score.cpp:L991-L1030](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L991-L1030)，列出 CheckFaParam 覆盖的全部指针及其对应错误日志文案。

#### 4.4.5 小练习与答案

**练习 1**：调用返回 561003，最可能的原因是什么？先检查什么？

**答案**：`ACLNN_ERR_INNER_FIND_KERNEL_ERROR`——内部查找 NPU kernel 异常，可能因为算子二进制包未安装（[aclnn_return_code.md:L25](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/aclnn_return_code.md#L25)）。先确认 ops-transformer 的 .run 包已安装、且源码标签与所装 CANN 版本配套（u1-l3 的版本配套表），再检查 `ASCEND_OPP_PATH`。

**练习 2**：为什么说「读 op_api 源码时搜索 `ACLNN_ERR_` 就能枚举该算子的失败路径」？

**答案**：因为返回码是 op_api 实现中逐点显式返回的，例如 FA 的判空守卫每个都绑定了 `ACLNN_ERR_PARAM_NULLPTR`（[aclnn_flash_attention_score.cpp:L996-L1016](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L996-L1016)），executor 创建绑定 561101（[L1118-L1119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1118-L1119)）。每个错误码出现处即一条失败路径。

## 5. 综合实践

**任务：编写并剖析一份「FA 两段式调用实验报告」。**

1. 从 [aclnnFlashAttentionScore.md:L598-L613](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/docs/aclnnFlashAttentionScore.md#L598-L613) 取出完整示例，按 [compile_and_run_sample.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/compile_and_run_sample.md) 建立可编译工程（有 NPU 环境时运行，无则止步于编译或纯走读）。
2. 在示例中插入四类探针实验并记录成表：
   - 基线：正常连续 fp32 输入 → 记录 `workspaceSize`、输出若干元素；
   - dtype 实验：改 `q` 为 `ACL_DOUBLE` → 记录返回码与报错文案；
   - 非连续实验：给 `q` 设置非 1 的 strides（参照 4.3.4）→ 对比 `workspaceSize` 与结果；
   - 空指针实验：`q = nullptr` → 记录 161001 与文案。
3. 为每条实验结果写出「返回码 → 源码中抛出该码的具体行」的对应关系（提示：dtype/shape 类错误多出自 [L1133-L1148](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1133-L1148) 的校验漏斗，空指针出自 [L991-L1016](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L991-L1016)）。

这份报告把本讲四个模块（两段式骨架、dtype 约束、非连续处理、返回码）串成一个可复现的实验闭环，也是后续给任何 aclnn 算子排障的通用模板。无硬件环境下，将「运行观察」替换为「源码走读 + 预期推演」，并明确标注「待本地验证」。

## 6. 本讲小结

- aclnn 是两段式接口：第一段 `GetXxxWorkspaceSize` 集中完成校验、infershape、tiling 并产出 `workspaceSize`（临时内存报价）和 `executor`（执行计划）；第二段 `aclnnXxx` 固定四参数，只是把计划异步下发到 stream，不可重复调用。
- 第一段内部是一个「指针 → format → dtype → shape → 连续化 → l0 算子序列」的校验漏斗，FA 的实现在 `aclnn_flash_attention_score.cpp:1102-1196`；第二段实现只有一行 `CommonOpExecutorRun` 透传。
- aclTensor 用 \( (shape, strides, offset) \) 描述非连续内存；FA 对非连续输入采取自动连续化策略（`l0op::Contiguous` 插入 executor），代价是更大的 workspace 和一次隐藏拷贝。
- dtype 白名单是硬约束（简写对照见 data_type.md），违反时第一段以 161002 拦截；简写表中的 FP8/FP4 系列是后续量化算子讲义的伏笔。
- 返回码按号段分流：161xxx 查自己传参、361001 查 runtime、561xxx 查安装与环境（561003/561112 多为算子包未装或版本不配套）；`aclGetRecentErrMsg` 提供定位到具体参数的文案。

## 7. 下一步学习建议

- 下一讲 u3-l2「common 公共库与 fallback 机制」将走进这些校验/连续化辅助工具背后的公共代码层（`l0op::Contiguous`、`ViewCopy`、日志宏等都来自 common 库），理解大型算子库如何复用这类样板逻辑。
- 建议继续精读 [docs/zh/context/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/context/) 下的姊妹篇：`data_format.md`（format 约定）、`broadcast_relationship.md` 与 `deduction_relationship.md`（类型/维度推导关系），它们与本题的 dtype 校验同属一个知识簇。
- 若想立刻看「多版本两段式接口」的演进实例，通读 [aclnn_flash_attention_score.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h) 中 V1→V5 第一段参数的增量，为 u4-l4 的 FIA 版本演进讲义预热。
