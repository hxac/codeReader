# u4-l2 FA 算子 op_api 层源码精读

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂一个工业级 aclnn 接口文件的完整结构——从头文件声明、入口校验、预处理，到 inner 调用与两阶段分发。
2. 掌握 FA 这类复杂算子的「参数校验漏斗」写法：空指针 → format → dtype → shape/取值约束，以及每层对应的错误码与日志宏。
3. 理解多版本接口（V1/V2/V3/V4/VarLen/Quant）如何共用同一个 base（`l0op::FlashAttentionScore`）实现，从而体会「版本演进不改内核」的工程手法。
4. 能追踪一个参数（如 `scale_value`）从 C API 入口一路到算子属性的完整路径。

## 2. 前置知识

本讲建立在 u2、u3 和 u4-l1 之上，先用三段话把要用的旧知识串起来：

- **两阶段 API**（u3-l1）：每个 aclnn 算子拆成 `aclnnXxxGetWorkspaceSize`（第一段：校验 + infershape + tiling + 算 workspace）和 `aclnnXxx`（第二段：把打包好的执行计划异步下发到 stream）。FA 的第一段里做的事远比 add_example 多，但骨架相同。
- **五层范式中的 op_api 层**（u1-l2）：`op_api` 目录承载对外暴露的 C 接口，编译产物是 `libopapi_transformer.so` 的一部分；它向下通过 executor 机制把算子挂到 op_host 注册的信息库上。本讲还会遇到「第四个文件」——`op_api` 目录下除 `aclnn_*.h/.cpp` 外还有一对 `flash_attention_score.h/.cpp`，这是 base 实现层（代码里称 l0 层）。
- **executor 与 aclTensor**（u3-l1）：`aclOpExecutor` 是「执行计划打包器」，op_api 层每做一步预处理（Contiguous/Reshape/Pad/Transpose）都是往 executor 的任务清单里追加一个算子调用，而不是立刻执行；`aclTensor` 是带 (shape, strides, offset) 的张量描述符。第一段结束时 `executor->GetWorkspaceSize()` 汇总所有中间张量与主算子的临时内存需求。

还需要两个新术语：

- **l0 / L2 分层**：本文件里 `l0op::FlashAttentionScore` 被称为 l0（level-0）接口，指「最贴近算子原型的一层封装」；`aclnnXxx` 则是 L2 层（用户直调层）。日志宏 `L0_DFX` / `L2_DFX_PHASE_1` / `L2_DFX_PHASE_2` 的命名也来自这个分层。
- **DFX 宏**：打点诊断宏，用于接口出入参的性能与问题定位日志，是每个入口的「固定写法」，不影响业务逻辑。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention/flash_attention_score/op_api/aclnn_flash_attention_score.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h) | 对外 C 接口声明：FA 家族 V1/V2/V3/V4、VarLen V1~V5、Quant 共 13 组两阶段接口，`extern "C"` 包裹 |
| [attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp) | 本讲主角（2214 行）：所有 L2 入口实现 + 匿名命名空间内的校验/分析/预处理工具函数 |
| [attention/flash_attention_score/op_api/flash_attention_score.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.h) | l0 层 base 函数声明（`l0op::FlashAttentionScore`，C++ 接口，带默认参数 `isMaxWorkspace`） |
| [attention/flash_attention_score/op_api/flash_attention_score.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.cpp) | l0 层 base 实现：补齐可选输入、int 数组转张量、`INFER_SHAPE` + `ADD_TO_LAUNCHER_LIST_AICORE` 挂接 op_host |

一个直观的比例感：`aclnn_*.cpp` 里约 1000 行是工具函数与校验，约 1200 行是 13 个 L2 入口的模板化重复；真正的「算子调用」逻辑集中在 `flash_attention_score.cpp` 的 190 行里。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**op_api 层与头文件声明约定**、**参数校验漏斗**、**预处理流水线与 l0 base 调用**、**两阶段分发与多版本共用 base**。

### 4.1 op_api 层的职责与头文件声明约定

#### 4.1.1 概念说明

op_api 层是算子库的「前台」。它面对的是 PyTorch 插件、ONNX 插件、用户 C 程序这些直接调 aclnn 的调用方；它对内要做的不是计算本身，而是三件事：

1. 把「用户视角的松散参数」（各种 Optional 可为 null、char* 布局字符串、double 标量）翻译成「算子原型视角的规整参数」（每个输入都是 aclTensor、标量进属性）。
2. 把不满足硬件约束的输入（非连续、未对齐、stride 超限）用通用小算子（Contiguous/Pad/Transpose/Reshape/Slice/ViewCopy）修整成满足约束的形态——代价是更多 workspace 和隐藏拷贝（u3-l1 讲过这个 trade-off）。
3. 把整套「预处理 + 主算子 + 后处理」打包进 executor，报出总 workspace，第二段一次性异步执行。

#### 4.1.2 核心流程

```text
头文件声明约定（每个算子两段、参数以 const 指针传入、末尾固定两个出参）
    aclnnXxxGetWorkspaceSize(..., uint64_t *workspaceSize, aclOpExecutor **executor)
    aclnnXxx(void *workspace, uint64_t workspaceSize, aclOpExecutor *executor, const aclrtStream stream)
```

头文件有三条值得注意的约定：

- 所有接口包在 `extern "C"` 里，保证 C 编译器不做名字改写，动态库符号稳定。
- 每个「第一段」注释都带 `@domain aclnn_ops_train`，标注算子域，供文档生成与检索。
- Optional 输入在声明上与必选输入无区别（都是 `const aclTensor *`），是否可传 null 由接口文档约定——这正是后面校验层存在的理由。

#### 4.1.3 源码精读

V1 接口的两阶段声明，参数从 4 个必选张量一直到两个出参共 22 个；第二段固定四参数：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.h:24-36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L24-L36)

这段声明了 `aclnnFlashAttentionScoreGetWorkspaceSize`（第一段，末尾 `workspaceSize`/`executor` 两个出参）和 `aclnnFlashAttentionScore`（第二段）。注意 `scaleValue` 是 `double`，而 `inputLayout` 是 `char *`——L2 层接口保留了 C 语言的朴素类型。

对比 V4 的声明可以直观看到「版本演进 = 参数面扩张」：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.h:144-181](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L144-L181)

V4 在 V1 基础上新增了 `queryRopeOptional/keyRopeOptional`（MSA 场景）、`dScaleQ/K/VOptional`（量化反缩放）、`sinkOptional`、`outDtype`、`softmaxOutLayout`、`seed/offset`（dropout）等参数——但第二段仍是同样的一行四参数（L186-190）。

l0 层 base 的声明则是 C++ 风格、全参数版（31 个参数 + executor + 默认参数 `isMaxWorkspace`）：

[attention/flash_attention_score/op_api/flash_attention_score.h:16-29](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.h#L16-L29)

「L2 版本接口参数少、l0 base 参数全」是这个分层的关键：L2 各版本把自己的参数集填进 l0 全参数签名中用不到的位置传 `nullptr`/`0`。注意一个源码细节：l0 层参数名拼写是 `preTockens/nextTockens`（Tock），与 L2 层的 `preTokens/nextTokens`（Tens）不同，阅读时不要当成两个不同概念。

#### 4.1.4 代码实践

1. **实践目标**：建立「L2 声明 ↔ l0 声明」的参数映射表。
2. **操作步骤**：打开头文件，分别抄下 V1（L24-30）、V2（L62-87）、V4（L144-181）的参数列表，再抄下 `l0op::FlashAttentionScore` 的参数列表（[flash_attention_score.h:18-28](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.h#L18-L28)），画一张三列对照表，标出每个 L2 参数落到 l0 的第几个参数、缺省时传什么。
3. **需要观察的现象**：三个版本的 `scaleValue/keepProb/preTokens/nextTokens/headNum/inputLayout/innerPrecise/sparseMode` 在 l0 签名中的位置是否完全一致；V1 比 V2 少的 `pseType` 在 V1 的 L2 实现里被填成什么（提示：见 4.3.3 的调用处，是一个 `PSE_TYPE_V1` 常量）。
4. **预期结果**：得到一张映射表，能看出「新增版本只是往 l0 的同一签名里多填几个非空参数」。
5. 本实践纯源码阅读，无需运行，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aclnnXxx`（第二段）不像第一段那样再传一遍所有算子参数？

**答案**：因为第一段已经把所有参数绑定进了 `aclOpExecutor`——executor 是「打包好的执行计划」。第二段只需拿 workspace 和 stream 把计划异步下发，这也解释了为什么第二段不可重复调用、且要求与第一段在同一上下文（u3-l1 已建立此概念）。

**练习 2**：头文件里 Optional 输入与必选输入在类型上无差别，调用方怎么知道 `attenMaskOptional` 可以传 null？

**答案**：靠参数名后缀 `Optional` 与接口文档（`docs/aclnnFlashAttentionScore.md`）约定；类型系统不区分，所以 op_api 实现里必须逐指针判空（见 4.2）。这也解释了为什么文档与代码要成对维护。

### 4.2 参数校验漏斗：从空指针到 dtype/format/shape

#### 4.2.1 概念说明

工业级接口的第一职责是「把错误挡在设备侧之外」。本文件把校验组织成一个漏斗，从粗到细：

```text
① 空指针检查（CheckFaParam）        —— 所有必选指针非空
② 空输出短路                        —— 输出全空则直接成功返回
③ format 检查（CheckFormat）        —— 特定 SoC 下禁 FRACTAL_NZ
④ dtype 检查（InputDtypeCheck）     —— q/k/v 同 dtype、pse/sink 约束
⑤ shape/轴分析（AnalysisInput/AnalysisAxis）—— layout 合法性、D 维约束、对齐策略
⑥ 组合约束（isSupportMultiInput 等）—— rope 场景下与其他参数的互斥
```

每层失败都返回对应的 `ACLNN_ERR_PARAM_NULLPTR` / `ACLNN_ERR_PARAM_INVALID`（u3-l1 讲过 161xxx 号段），并通过 `OP_LOGE`/`OP_LOGE_FOR_INVALID_*_WITH_REASON` 宏打出结构化错误信息。

#### 4.2.2 核心流程

校验的执行顺序在入口函数里清晰可见（见 4.4.3 的 L1110-L1140）：先 `CheckFaParam`，再空输出短路，再（按需）`CheckFormat`、`InputDtypeCheck`、`AnalysisInput`。这个顺序有讲究：空指针不检查就无法安全解引用；空输出短路放在最前可以零成本放过 b/s 为 0 的退化请求；format/dtype 都是 O(1) 检查放在 shape 分析（最重）之前。

#### 4.2.3 源码精读

空指针漏斗的第一层，注意 `OP_CHECK(cond, OP_LOGE(...), return ...)` 三段式宏——条件、日志、返回值写在一行里：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:991-1023](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L991-L1023)

这段逐个检查 query/key/value/inputLayout/executor/workspaceSize/softmaxMaxOut/softmaxSumOut/attentionOutOut 非空。对比 u2 的 add_example（其 op_api 层几乎没有显式校验，靠框架 def 白名单兜底），能看出「教学算子靠框架、工业算子靠自查」的分层差异。

dtype 检查的代表性写法——q/k/v 三者必须同 dtype，并用 `OP_LOGE_FOR_INVALID_DTYPES_WITH_REASON` 打出「参数名 + 实际 dtype 列表 + 原因」三段结构化信息：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:410-472](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L410-L472)

`InputDtypeCheck` 中值得注意的四个分支：① q/k/v 互查（L418-426）；② `StrideLimited()` 时 dtype 只能是 FLOAT/FLOAT16/BF16（L428-435）——这是把「硬件 stride 上限」翻译成「dtype 白名单」的例子；③ `pseType` 为 2/3（inner pse alibi）时 pse 必须是 FP32 且不可为 null（L436-450）；④ 普通 pse 必须与输出同 dtype、sink 必须是 FP32（L451-470）。

`StrideLimited()` 本身是一个典型的「SoC 分支」函数：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:103-109](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L103-L109)

只有 `DAV_2201`（对应受限的 SoC 代际）返回 true，后续的 format 检查、pad/transpose 预处理都以它为总开关——这就是 u1-l4 讲的「多 SoC 适配」在 op_api 层的形态。

shape 轴分析：`AnalysisAxis` 按输入 layout 字符串分发到 5 个解析函数，把用户 shape 折算成统一的 `AxesInfo{b, n1, n2, s1, s2, d, dk, dv}`：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:173-228](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L173-L228)

注意 L189-213 的分发条件同时约束「dim 数 × layout 字符串」组合（如 3 维才能配 BSH），不匹配即报 `ACLNN_ERR_PARAM_INVALID`；L214-226 进一步约束 `d == dk`、`d >= dv`（MQA/GQA 场景 value 头维不能超过 q/k）。

#### 4.2.4 代码实践

1. **实践目标**：亲手体验校验漏斗的拦截行为与错误码。
2. **操作步骤**：在 NPU 环境上（参考 u2-l4 的 `bash build.sh --run_example` 流程，但换成 flash_attention_score 的示例，或用 u3-l3 的 torch_extension 调 `npu_fusion_attention`）分别构造三组非法输入：(a) query 传 FP32、key 传 FP16；(b) `inputLayout` 传 "BSH" 但 query 是 4 维；(c) `headNum` 传 0。观察三组的返回码与日志。
3. **需要观察的现象**：`aclGetRecentErrMsg`（u3-l1 讲过）打出的信息里应分别出现 dtypes/values 相关的结构化原因。
4. **预期结果**：三组均在第一段（GetWorkspaceSize）被拦截，返回 `ACLNN_ERR_PARAM_INVALID`（161002），第二段根本不会被调用；日志中的 reason 文本与 4.2.3 引用的三个宏调用一一对应。无 NPU 环境时，可改为纯源码阅读：把三组非法值代入上述三个函数，手工推导各自命中哪个 `return`——**待本地验证**。
5. 若无法运行，明确标注「待本地验证」后完成推导即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么空输出短路（L1123-1127）放在所有校验之前而不是之后？

**答案**：b/s 维为 0 时输出张量也是空的，算子无事可做；先短路可以避免对退化输入做昂贵的 shape 分析与预处理，直接 `*workspaceSize = 0` 返回成功。这是「便宜检查在前」原则的极端形式。

**练习 2**：`OP_LOGE` 与 `OP_LOGE_FOR_INVALID_DTYPES_WITH_REASON` 的区别是什么？为什么后者更好？

**答案**：前者只打一条自由文本日志；后者把「算子名、参数名、实际值、失败原因」拆成结构化字段，能被 `aclGetRecentErrMsg` 精确取回并呈现给用户，减少排障时「看日志猜参数」的成本。新代码应优先用带 `WITH_REASON` 后缀的变体。

### 4.3 预处理流水线与 l0 base 调用

#### 4.3.1 概念说明

校验通过后，op_api 层要把「用户给的自然输入」修整成「硬件想要的输入」。FA 的修整由三步流水线完成，全部通过向 executor 追加通用小算子实现（不立即执行）：

1. **Contiguous**：非连续输入转连续（u3-l1 的自动连续化策略）。
2. **PreprocessQKV**：按 `FaShapeInfo` 中分析出的标志位做 Reshape / Pad / Transpose——例如 D 维未按 16/128 对齐时补零、`alignedH1Size > 65535` 时把 BSH 转成 BNSD 以避开 stride 上限。
3. **调用 l0 base**：把修整后的张量交 `l0op::FlashAttentionScore`，拿到 4 个输出（softmaxMax/softmaxSum/softmaxOut/attentionOut）。
4. **Postprocess + ViewCopy**：把 l0 输出（可能是 padded/transposed 形态）还原成用户要的输出 shape，再拷回用户提供的输出张量。

`FaShapeInfo` 是这条流水线的「施工图」：`AnalysisInput` 负责填图（needPad/needTranspose/needReshape/needPadValue 四个标志 + padNum/padNumv + perm 等），流水线按图施工。

#### 4.3.2 核心流程

```text
AnalysisInput(填 FaShapeInfo)
    ↓
Contiguous(q/k/v 及所有非空可选输入)          —— 每个都追加一个 Contiguous 算子到 executor
    ↓
PreprocessQKV:
    if needReshape:  q/k/v = Reshape(...)      —— 拆成 (B,S,N,D) 四维
    if needPad:      q/k = Pad(+padNum), v = Pad(+padNumv)
    if needTranspose: q/k/v = Transpose(perm_in)   —— BSH/SBH → BNSD
    (SBH 且 pad 且未 transpose: 再 Reshape 回三维)
    ↓
l0FlashAttentionScoreOuts = l0op::FlashAttentionScore(全参数)
    ↓
Postprocess: 还原输出 (Reshape → Transpose(perm_out) → Slice 去掉 pad → Reshape 到用户 shape)
    ↓
ViewCopy × 3: l0 输出 → 用户的 softmaxMaxOut / softmaxSumOut / attentionOutOut
```

#### 4.3.3 源码精读

先看 V1 入口里从校验到 l0 调用的主干（这是本讲最核心的一段）：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:1102-1155](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1102-L1155)

逐段说明：L1110 空指针漏斗；L1112-1116 第一段 DFX 打点；L1118 `CREATE_EXECUTOR()` 创建执行计划；L1123-1127 空输出短路；L1134-1140 format/dtype/shape 三层校验（本入口把 V1 的 `pseType` 固定为 `PSE_TYPE_V1 = 1`）；L1146-1148 连续化；L1150 预处理。**`scale_value` 的路径就在这里**：它是 L1105 的入口参数 `scaleValue`，不经过任何校验或变换，原样传到 L1152-1155 的 l0 调用第 21 个实参位置。

l0 base 的实现——本讲追踪的终点。`scaleValue` 在这里被 `static_cast<float>` 后通过 `OP_ATTR` 进入算子属性：

[attention/flash_attention_score/op_api/flash_attention_score.cpp:156-179](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.cpp#L156-L179)

`INFER_SHAPE` 宏触发 op_host 注册的 shape 推导（属性也在此刻参与推导，例如 TND 布局下需要靠 `headNum` 属性拆出 n1）；`ADD_TO_LAUNCHER_LIST_AICORE` 把主算子追加进 executor 的下发清单。两次 `static_cast<float>(scaleValue)`（L162 与 L177）说明：**C API 的 double 精度在进入算子属性时被截断为 float**——这是追踪参数时容易忽略的隐式行为。

l0 base 的前半段还做了两类翻译工作，值得一看：

[attention/flash_attention_score/op_api/flash_attention_score.cpp:58-91](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.cpp#L58-L91)

L58-81：所有为 null 的可选张量输入用 `executor->AllocTensor` 补一个「空壳」张量——因为 op_host 的算子原型要求每个输入都存在（对照 u2-l2：def 文件里注册的输入没有 Optional 语义，可选项是在 aclnn 层消解的）。L83-91：`aclIntArray`（host 侧 int64 数组）通过 `ConvertToTensor` 转成 DT_INT64 张量——属性/输入里不允许裸数组，只能走张量。

输出的后处理与搬运（入口的后三分之一）：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:1176-1194](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1176-L1194)

`Postprocess` 把 l0 输出还原成用户布局（内部依次做 Reshape/Transpose/Slice，见 [L930-L989](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L930-L989)），随后三个 `l0op::ViewCopy` 把中间输出拷入用户给的输出张量；最后 L1193 `*workspaceSize = uniqueExecutor->GetWorkspaceSize()` 汇总报价，L1194 `ReleaseTo(executor)` 把 unique_ptr 管理的计划移交出来——这就是第一段交给用户的「执行计划」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：画出 `scale_value` 从 API 入口到算子属性的完整调用链，并仿照 FA 的校验风格为 u2 改造过的算子补 dtype 校验。

**操作步骤**：

1. 调用链追踪（纯阅读，可直接完成）：
   - 起点：[aclnn_flash_attention_score.h:27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.h#L27) 声明中的 `double scaleValue`；
   - 第二站：[aclnn_flash_attention_score.cpp:1105](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1105) 入口形参；
   - 第三站：[aclnn_flash_attention_score.cpp:1152-1155](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1152-L1155) 原样传给 l0 调用；
   - 第四站：[flash_attention_score.h:25](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.h#L25) l0 声明形参；
   - 终点一：[flash_attention_score.cpp:162](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.cpp#L162) `INFER_SHAPE` 的 `OP_ATTR(static_cast<float>(scaleValue), ...)`；
   - 终点二：[flash_attention_score.cpp:177](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/flash_attention_score.cpp#L177) `ADD_TO_LAUNCHER_LIST_AICORE` 的同名属性。
   - 把这 6 站画成流程图，并在终点标注「double → float 隐式截断」；再往下追一步：用 Grep 在 `op_host/flash_attention_score_tiling.cpp` 里找 `scale_value` 属性被 tiling 读取的位置（可选）。
2. dtype 校验迁移（需要编译环境，无 NPU 也可编译验证）：
   - 打开 u2-l2/l3 中你改造过的 `examples/add_example`（若未改造，用原版亦可）；
   - 仿照本讲 4.2.3 的 `InputDtypeCheck`，在 add_example 的 aclnn 层（`libopapi` 生成路径下或其 eager 示例调用前）增加一段校验：要求 x/y/out 三者 dtype 一致，不一致时用 `OP_LOGE_FOR_INVALID_DTYPES_WITH_REASON` 打日志并返回 `ACLNN_ERR_PARAM_INVALID`；
   - 重新 `bash build.sh --ophost --opapi --ops=add_example` 编译；
   - 构造一个 fp16 + fp32 混合输入的调用，观察返回码。

**需要观察的现象**：调用链图上 `scaleValue` 全程无变换（除终点截断）；add_example 混合 dtype 调用在第一段返回 161002，且 `aclGetRecentErrMsg` 能取到结构化原因。

**预期结果**：得到一张 6 站调用链图；校验代码编译通过且拦截生效。若 add_example 的 aclnn 层为自动生成、无处插入校验，则退而在示例程序 `test_aclnn_add_example.cpp` 里加同样的检查并写明「示例代码」——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PreprocessQKV` 开头有 `if (!StrideLimited()) return ACLNN_SUCCESS;`（L825-827）？那不受限的 SoC 上 D 维不对齐怎么办？

**答案**：pad/transpose 预处理是为特定代际硬件的 stride/对齐限制服务的（如 `MAX_STRIDE_S1 = 65535`）；不受限 SoC 的 kernel 可直接消化非对齐输入，无需 host 侧修整，跳过可省掉大量隐藏拷贝和 workspace。这体现了「预处理逻辑跟着硬件代际走」而不是一刀切。

**练习 2**：l0 base 里为什么要给所有 null 可选输入 `AllocTensor` 补空壳？

**答案**：op_host 的算子原型（def 文件，u2-l2）按固定输入列表注册，`INFER_SHAPE`/`ADD_TO_LAUNCHER_LIST_AICORE` 需要逐位置传入张量描述符。「可选」语义只存在于 L2 aclnn 接口层，进入 l0 层前必须消解为确定的张量。

**练习 3**：第一段最后为什么必须调用 `uniqueExecutor.ReleaseTo(executor)`？漏掉会怎样？

**答案**：executor 由入口里的 `unique_ptr` 局部持有，`ReleaseTo` 把所有权移交给用户出参指针。漏掉则函数返回时 unique_ptr 析构销毁执行计划，用户拿到的 `*executor` 是悬空指针，第二段调用会崩溃或未定义行为。

### 4.4 两阶段分发与多版本接口共用 base 的组织

#### 4.4.1 概念说明

看懂了 V1 入口的 90 行主干，剩下的 12 个入口基本是「同一模板的参数填空」。这就是本讲的第三个重点：**版本演进的工程组织**。FA 家族在同一对文件里维护 13 组 L2 接口（FA V1-V4、VarLen V1-V5、Quant），它们全部收敛到唯一的 `l0op::FlashAttentionScore` base。新版本 = 新的 L2 入口（更多参数、更严校验）+ 复用同一个 base + 复用同一套工具函数。

#### 4.4.2 核心流程

```text
aclnnFlashAttentionScoreV1GetWorkspaceSize ─┐
aclnnFlashAttentionScoreV2GetWorkspaceSize ─┤
aclnnFlashAttentionScoreV3GetWorkspaceSize ─┼─→ 同一套工具函数（CheckFaParam/InputDtypeCheck/
aclnnFlashAttentionScoreV4GetWorkspaceSize ─┤    AnalysisInput/Contiguous/PreprocessQKV/Postprocess）
aclnnFlashAttentionVarLenScoreV1~V5        ─┤
aclnnQuantFlashAttentionScore              ─┘          ↓
                                            l0op::FlashAttentionScore（唯一 base）
                                                     ↓
                                            INFER_SHAPE + ADD_TO_LAUNCHER_LIST_AICORE
                                                     ↓
                                            op_host 信息库（def/infershape/tiling）
```

第二段则完全模板化：13 个 `aclnnXxx` 函数体都只有两行——DFX 打点 + `CommonOpExecutorRun`。

#### 4.4.3 源码精读

第二段的全部内容，注释里的「固定写法」就是字面意思：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:1198-1204](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1198-L1204)

`CommonOpExecutorRun` 是框架能力（aclnn_base 提供）：按 executor 里记录的下发清单，把所有算子（预处理小算子 + FA 主算子 + 后处理）依序异步下发到 stream。所有版本的差异都已经在第一段固化进 executor，所以第二段无版本差异。

多版本共用 base 的证据——V2 入口调用的还是同一个 `l0op::FlashAttentionScore`，只是把 V2 新增的 `qStartIdxOptional/kvStartIdxOptional` 和真正的 `pseType` 填了进去：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:1367-1372](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1367-L1372)

对照 V1 的调用（L1152-1155）：V1 处传 `nullptr` 的位置，V2 传了实参；V1 固定 `PSE_TYPE_V1` 的位置，V2 透传用户的 `pseType`。base 签名是「最大参数并集」，各版本向下填充。

VarLen 版本则展示了「同 base 不同入口约束」：VarLen V1 在进入分析前强制 `inputLayout == "TND"`：

[attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp:1232-1237](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp#L1232-L1237)

这类「版本特有的前置约束」只出现在各自入口里，不污染 base——base 只关心「一组合法规整的参数怎么发算子」。

#### 4.4.4 代码实践

1. **实践目标**：验证「13 个入口共用一个 base」并量化模板重复度。
2. **操作步骤**：在本仓库根目录执行 `grep -n "l0op::FlashAttentionScore(" attention/flash_attention_score/op_api/aclnn_flash_attention_score.cpp`，统计调用点数量（应为 9 处 GetWorkspaceSize 中的直接调用，其余入口经重载/转发——以实际 grep 结果为准）；再执行 `grep -n "CommonOpExecutorRun" 同文件`，统计第二段模板数量。然后任选 V4 入口（L1520 起）与 V1 入口逐行 diff（可用 `sed -n` 截取两段后人工比对），列出差异行。
3. **需要观察的现象**：V4 与 V1 的入口主干（校验→连续化→预处理→l0 调用→后处理→ViewCopy→报 workspace）步骤完全同构，差异集中在参数个数与个别校验（如 V4 多了 `isSupportMultiInput` 对 rope 输入的组合校验，L1026-1100）。
4. **预期结果**：一张「V1 vs V4 差异清单」，能据此说出「给 FA 加一个 V5 需要动哪些代码」（头文件一组声明 + cpp 一个入口 + 文档，base 与 kernel 通常不动）。
5. 本实践为源码阅读型，可直接完成；grep 计数与行号以本地输出为准。

#### 4.4.5 小练习与答案

**练习 1**：这种「多版本共用 base」的组织方式，相比「每个版本独立实现一份」有什么优劣？

**答案**：优点是修一处全家族受益（如 4.3.3 的空壳补齐逻辑、预处理流水线只需维护一份）、行为一致性好、新增版本成本低。代价是 base 签名变成 31 参数的「最大并集」，可读性差（大量 nullptr 占位），且所有版本共享同一份退化风险——base 出 bug 会波及全部 13 组接口。

**练习 2**：为什么版本演进要新开 `aclnnFlashAttentionScoreV2` 而不是直接改 V1 的签名？

**答案**：`extern "C"` 动态库符号一旦发布就是二进制契约：改签名等于让已链接旧符号的程序崩溃。新版本接口新增符号、旧版本保留实现，调用方按需迁移——这是 C ABI 生态通行的兼容策略，也解释了头文件里 V1~V5 并存的原因。

## 5. 综合实践

**任务：给 u2 的 add_example 做一次「FA 化」改造，写一份《aclnn 接口实现评审报告》。**

把三个环节串起来：

1. **对照阅读**：把 `aclnn_flash_attention_score.cpp` 的 V1 入口（L1102-1196）与 add_example 的 eager 示例（`examples/add_example/examples/test_aclnn_add_example.cpp`）并排打开，列出 FA 入口做了而 add_example 没做的 5 件事（提示：空指针漏斗、空输出短路、dtype 互查、Contiguous、ViewCopy）。
2. **动手改造**：从 4.3.4 的 dtype 校验出发，再为 add_example 增加「空输出短路」与「非连续输入自动 Contiguous」两项（Contiguous 可在示例程序里用 `aclnnContiguous` 或先拷贝到连续缓冲实现，标注「示例代码」）。
3. **验证**：`bash build.sh --ophost --opapi --ops=add_example` 编译；在 NPU/simulator 上分别用「混合 dtype」「空 shape」「转置后的非连续输入」三种用例验证拦截与兼容行为；无硬件则对每种用例手工推导命中哪行 `return` 并标注「待本地验证」。
4. **输出报告**：一页 Markdown，包含 5 项差距清单、你补的三段代码、三组用例的预期 vs 实际返回码，以及一段「如果 add_example 要像 FA 一样支持 3 个 SoC 代际，op_api 层还需要引入什么机制」的讨论（提示：`StrideLimited()` 式的 SoC 分支总开关）。

## 6. 本讲小结

- op_api 层是算子库的「前台」：翻译参数（Optional→空壳张量、aclIntArray→张量、double→float 属性）、修整输入（Contiguous/Pad/Transpose/Reshape）、打包执行计划（executor）并报出 workspace，本身不做计算。
- 参数校验是五层漏斗：空指针 → 空输出短路 → format → dtype → shape/组合约束，每层配 `OP_LOGE_FOR_INVALID_*_WITH_REASON` 结构化日志与 161xxx 返回码，原则是「便宜检查在前、重检查在后」。
- FA 的预处理流水线（Contiguous → PreprocessQKV → l0 base → Postprocess → ViewCopy）全部通过向 executor 追加通用小算子实现，由 `FaShapeInfo` 四个标志位驱动，且整体挂在 `StrideLimited()` 这类 SoC 总开关之下——多 SoC 适配在 op_api 层的形态就是这样的运行期分支。
- `scale_value` 的完整路径：C API double 形参 → l0 调用原样透传 → `OP_ATTR(static_cast<float>(...))` 进入算子属性，供 op_host 的 infershape/tiling 读取；除终点截断外全程无变换。
- 版本演进的工程模式：13 组 L2 接口共用唯一的 `l0op::FlashAttentionScore` base（31 参数最大并集），新版本 = 头文件新增一组 `extern "C"` 声明 + 一个入口填空 + 特有校验；第二段全部模板化为 `CommonOpExecutorRun` 两行。
- 工业算子靠自查（显式校验），教学算子靠框架（def 白名单）——add_example 与 FA 的差距正是这两层防线的取舍差异。

## 7. 下一步学习建议

下一讲 **u4-l3「FA 的 tiling 策略与多 SoC 架构适配」** 将顺着本讲的终点往下走：`OP_ATTR(scaleValue)` 进入 op_host 后，tiling 如何结合 UB/核数约束选分块，以及 `op_host/op_kernel` 下 `arch22/arch35` 目录如何隔离不同代际实现。建议先做两个热身：① 用 Grep 在 `attention/flash_attention_score/op_host/flash_attention_score_tiling.cpp` 中找到读取 `scale_value`、`headNum` 等属性的位置，与本讲 4.3.4 的调用链图接上；② 重读 u2-l2 的 tiling 四类产出（tiling data/tiling key/blockDim/workspace），带着「FA 的 tiling 为什么比 add_example 复杂几个数量级」的问题进入下一讲。之后 u4-l4 将把同样的读法迁移到推理侧的 `fused_infer_attention_score`，检验你对 base/inner 分层手法的掌握。
