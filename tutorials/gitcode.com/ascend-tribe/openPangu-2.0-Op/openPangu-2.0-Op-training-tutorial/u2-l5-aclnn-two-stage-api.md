# op_api 层：aclnn 两段式接口设计

## 1. 本讲目标

前几讲我们走读了一个算子的 op_def（原型注册）、op_host（Tiling）和 op_kernel（设备侧 Kernel）。本讲补上最后一块拼图——**op_api 层**，即外部世界真正调用算子的入口。学完本讲，你应该能够：

1. 解释 aclnn 两段式接口（`aclnnXxxGetWorkspaceSize` + `aclnnXxx`）各自做什么、为什么这样设计。
2. 拿到任何一个 aclnn 头文件，都能统计出它的输入张量、输入数组、标量属性和输出张量，并分清必选与可选。
3. 说明 op_api 层内部的 L2（aclnn 对外接口）与 L0（内部算子封装）两级分工，以及它们如何与 `_def.cpp`、tiling 解耦又衔接。
4. 读懂 `flash_attention_score_enhance` 算子 op_api 目录下两类文件的职责差异。

## 2. 前置知识

- **四层算子模型（u1-l2/u2-l2 已建立）**：一个算子由 `_def.cpp`（原型注册）、`_tiling.cpp`（Host 侧切分规划）、`op_kernel`（设备侧 Ascend C Kernel）和 op_api（对外 aclnn 接口）组成。本讲只聚焦最后一层。
- **aclnn**：CANN 对外暴露的单算子调用接口命名前缀，C 风格函数，形如 `aclnnFlashAttentionVarLenScoreEnhanceV5`。 acl 是昇腾计算语言（Ascend Computing Language）运行时库，负责设备管理、内存拷贝、任务下发。
- **Host 侧与 Device 侧**：Host 指 CPU 侧（控制流），Device 指 NPU 侧（数据流）。tiling 在 Host 上做，Kernel 在 Device 上跑。
- **stream（aclrtStream）**：异步任务队列。向 stream 提交的计算任务不会立刻执行，需要 `aclrtSynchronizeStream` 等待完成。
- **workspace**：算子执行时需要的 Device 侧临时工作内存。大小只有完成 tiling 规划后才能确定，所以必须先有一段接口把它算出来。
- **executor（aclOpExecutor）**：算子执行器对象。第一段接口把"这次调用要用哪些输入、走哪条计算流程、中间需要哪些临时张量"全部记录进 executor；第二段接口拿着它把任务下发到 stream。
- **`_def.cpp` 中的 Attr**：标量属性（如 `input_layout`、`sparse_mode`）在原型里以 `this->Attr(...)` 声明，运行期由 tiling 通过 `GetAttrs()` 读取——这就是 aclnn 标量参数"透传给 tiling"的通路。

## 3. 本讲源码地图

本讲的标本是 attention 族里唯一带完整 op_api 源码的算子之一 `flash_attention_score_enhance`（FlashAttention 变长增强前向）。它的 op_api 目录只有 4 个文件，恰好构成两级结构：

| 文件 | 层级 | 作用 |
| --- | --- | --- |
| [op_api/aclnn_flash_attention_score_enhance.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h) | L2 对外头文件 | 声明两段式 aclnn 接口，供外部（如 torch_npu 扩展）包含 |
| [op_api/aclnn_flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp) | L2 实现 | 入参校验、布局分析、Contiguous/Pad/Transpose 预处理、调用 L0、后处理、汇总 workspace |
| [op_api/flash_attention_score_enhance.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.h) | L0 内部头文件 | 在 `namespace l0op` 中声明内部算子封装函数 |
| [op_api/flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp) | L0 实现 | 填充可选输入默认值、推导输出、`INFER_SHAPE` + `ADD_TO_LAUNCHER_LIST_AICORE` 把算子挂进 executor |

辅助阅读（证明 aclnn 参数如何抵达 tiling）：

| 文件 | 本讲关注点 |
| --- | --- |
| [op_host/flash_attention_score_enhance_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp) | L382-L395 的 Attr 声明清单 |
| [op_host/flash_attention_score_enhance_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp) | L71-L120 的 `GetAttrs()->GetAttrPointer<char>` 读取 `input_layout` |
| [docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md) | 接口文档：参数表、约束、C++ 调用示例 |

> 提醒：u1-l1 讲过，多数算子目录（如 `ai_infra_aggregate_hidden`）没有 op_api 源码，其 aclnn 符号由已安装的算子包在运行期提供。`flash_attention_score_enhance` 之所以把 op_api 源码放进仓库，正是因为它需要在 L2 做**输入布局预处理**（Pad/Transpose 等），这是通用模板生成不了的自定义逻辑。

## 4. 核心概念与源码讲解

### 4.1 aclnn 两段式接口约定：头文件怎么读

#### 4.1.1 概念说明

为什么一个算子要拆成两个 C 函数？直觉上理解：

- 算子执行前，框架必须知道**需要多大的 Device 临时内存（workspace）**，才能提前申请好内存；
- 而 workspace 大小取决于 tiling 结果——同一个算子，输入 shape 不同、layout 不同，切分方案就不同，临时内存需求也不同；
- 所以 CANN 约定：**第一段接口在 Host 上"预演"一遍计算流程**——校验参数、构造 executor、把要用到的所有子任务记录下来，顺便算出 workspace 大小；**第二段接口只负责执行**——拿着第一段产出的 executor，把任务下发到指定 stream。

这种"规划"与"执行"分离的设计，与 u2-l3 讲的 tiling"作战规划"一脉相承：tiling 规划的是单个 Kernel 怎么切，两段式规划的是整条调用链（可能包含多个 L0 算子）怎么串。

本算子的计算公式（来自 docs）：

\[ \text{Attention} = \text{Dropout}\big(\text{Softmax}\big(\text{Mask}\big(s \cdot (QK^\top + Q_r K_r^\top) + \text{pse}\big),\ \text{mask}\big),\ p\big) \cdot V \]

其中 \( Q_r, K_r \) 是可选的 rope（旋转位置编码）分量，pse 是可选位置编码偏移，sink 是额外的注意力"汇"项。注意公式里的每一个分量，都对应 aclnn 签名里的一个参数。

#### 4.1.2 核心流程

标准的 aclnn 两段式调用时序（外部调用者视角）：

```text
调用方                          CANN / 算子库
  │
  │ 1. 准备 aclTensor / aclIntArray（描述 Device 上的数据）
  │
  ├─> 2. aclnnXxxGetWorkspaceSize(输入..., 输出..., &workspaceSize, &executor)
  │       ├─ 参数判空 / dtype / format / shape 校验
  │       ├─ 创建 executor（CREATE_EXECUTOR）
  │       ├─ （可选）Contiguous / Pad / Transpose 等预处理，也录进 executor
  │       ├─ 调 L0 内部算子：INFER_SHAPE + 加入下发列表
  │       └─ *workspaceSize = executor->GetWorkspaceSize(); executor 交还调用方
  │
  ├─> 3. 若 workspaceSize > 0，aclrtMalloc 申请 Device 内存
  │
  ├─> 4. aclnnXxx(workspace, workspaceSize, executor, stream)
  │       └─ CommonOpExecutorRun：按记录依次下发任务到 stream
  │
  ├─> 5. aclrtSynchronizeStream(stream) 等待完成
  └─> 6. 拷回结果、释放资源
```

#### 4.1.3 源码精读

先看对外头文件。第一段接口共 **32 个参数**，注释明确写着"第一段接口……计算workspace大小"：

- [aclnn_flash_attention_score_enhance.h:24-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56)——声明 `aclnnFlashAttentionVarLenScoreEnhanceV5GetWorkspaceSize`。读签名时按"角色"给参数分组：

  | 分组 | 参数 | 个数 |
  | --- | --- | --- |
  | 必选张量输入 | `query`、`key`、`value` | 3 |
  | 可选张量输入（名字带 `Optional`，可传 nullptr） | `queryRope`、`keyRope`、`realShiftOptional`、`dropMaskOptional`、`paddingMaskOptional`、`attenMaskOptional`、`sinkOptional` | 7 |
  | 整型数组输入（`aclIntArray*`） | `prefixOptional`、`actualSeqQLenOptional`、`actualSeqKvLenOptional`、`qStartIdxOptional`、`kvStartIdxOptional` | 5 |
  | 标量属性 | `scaleValue`、`keepProb`、`preTokens`、`nextTokens`、`headNum`、`inputLayout`、`innerPrecise`、`sparseMode`、`pseType`、`softmaxOutLayout`、`sinkNum` | 11 |
  | 张量输出 | `softmaxMaxOut`、`softmaxSumOut`、`softmaxOutOut`、`attentionOutOut` | 4 |
  | 框架出参 | `workspaceSize`（uint64_t*）、`executor`（aclOpExecutor**） | 2 |

- [aclnn_flash_attention_score_enhance.h:61-65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L61-L65)——第二段接口 `aclnnFlashAttentionVarLenScoreEnhanceV5` 只有 4 个参数：`workspace`（Device 内存地址）、`workspaceSize`、`executor`、`stream`。**它不再出现任何算子参数**——所有业务信息都已经封装在 executor 里了。这是两段式设计最直观的证据。

- [docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md:28-30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L28-L30)——文档对两段式的官方描述："必须先调用……GetWorkspaceSize 接口获取计算所需 workspace 大小以及包含了算子计算流程的执行器，再调用……接口执行计算"。

  一个**值得警惕的细节**：文档第 56-59 行的原型把 `sinkNum` 写在 `softmaxOutLayout` 之前，而头文件（L47-L50）的顺序是 `sparseMode, pseType, softmaxOutLayout(char*), sinkNum(int64_t)`；文档第 720-723 行的调用示例甚至没有传 `sinkNum`。头文件与 torch 扩展层的实际调用（见 4.3 节）一致，**应以头文件为准**，文档示例滞后（待本地验证）。这提醒我们：读接口永远以 `.h` 为最终依据。

#### 4.1.4 代码实践

**实践目标**：练习"从头文件反推接口全貌"的能力，并发现文档与源码的差异。

**操作步骤**：

1. 打开 [aclnn_flash_attention_score_enhance.h:24-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56)，逐行数参数，按 4.1.3 的六分组填表；
2. 打开文档参数表（docs 第 80-403 行），对照每个参数的"输入/输出"列和"使用说明"列，标注哪些参数文档标了默认行为（如 `sparseMode` 支持 0~8 不支持 5）；
3. 用 `grep -n "sinkNum\|softmaxOutLayout" docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md` 找出文档原型、参数表、调用示例三处中这两个参数的出现顺序，再与头文件 L45-L50 对比。

**需要观察的现象**：文档原型（第 58-59 行）中 `sinkNum` 在 `softmaxOutLayout` 之前；头文件 L49-L50 中 `softmaxOutLayout` 在前；文档调用示例（第 722-723 行）完全没有 `sinkNum`。

**预期结果**：得出"文档原型与示例互不一致、且都与头文件不同"的结论，确认以头文件为准。本实践纯源码/文档阅读，无需 NPU 环境，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：第二段接口为什么不需要传 `query`/`key`/`value` 这些输入？

**答案**：第一段接口已经把所有输入张量的描述（地址、shape、stride、dtype）连同计算流程记录进了 `executor`。第二段只做一件事——`CommonOpExecutorRun` 按 executor 里的记录下发任务（见 4.2.3），因此只需要 workspace 地址和 stream。

**练习 2**：`aclIntArray*` 类型的 `actualSeqQLenOptional`（变长序列每个 batch 的真实 Q 长度）为什么不能像 `headNum` 一样用 `int64_t` 传？

**答案**：变长场景下 batch 数不固定，Q 长度是一个**长度可变的数组**，需要 `aclIntArray` 这种带长度的容器描述；`headNum` 是单个整数，标量即可。这也解释了 `_def.cpp` 里二者分别以 Input（`actual_seq_qlen`，L158）和 Attr（`head_num`，L386）声明。

**练习 3**：如果调用第一段接口时三个必选输出 `softmaxMaxOut`/`softmaxSumOut`/`attentionOutOut` 传了 nullptr，会发生什么？

**答案**：`CheckFaParam`（见 4.2.3）用 `CHECK_RET(x != nullptr, ACLNN_ERR_PARAM_NULLPTR)` 逐个判空，直接返回 `ACLNN_ERR_PARAM_NULLPTR`（错误码 161001），不会创建 executor。注意 `softmaxOutOut` 不在判空清单里——因为当前实现中它实际未被使用（见 4.2.3 的"三个 ViewCopy"）。

### 4.2 第一段 GetWorkspaceSize：校验、预处理与 executor 构建

#### 4.2.1 概念说明

第一段接口是 aclnn 的"大脑"，本算子的 L2 实现有 1200 行，几乎全部逻辑都在这里。它解决三件事：

1. **防御**：参数判空、dtype 一致性、format 合法性、layout 合法性——错误越早暴露，定位成本越低；
2. **适配**：把用户给的"任意合法输入"改造成 Kernel"最喜欢的形状"——不连续的先 `Contiguous`，D 维不按 16/128 对齐的先 `Pad`，stride 超限的先 `Transpose`。这些预处理本身就是一串 L0 基础算子，同样录进 executor；
3. **汇总**：把自定义 L0 算子挂进 executor，取出 workspace 总需求，交还 executor 给调用方。

一个关键认知：**executor 里录的不只是一个算子，而是一条小型任务流水线**。第二段执行时，Contiguous → Pad → Transpose → 自定义 FA 算子 → Transpose → Slice → ViewCopy 会按序跑在同一个 stream 上。这就是 op_api 层存在的核心价值——它让 Kernel 只需支持"最优布局"，脏活累活留给 L2。

#### 4.2.2 核心流程

`aclnnFlashAttentionVarLenScoreEnhanceV5GetWorkspaceSize` 的执行流程（对照源码行号）：

```text
入参判空 CheckFaParam (L1011)
  ↓
L2_DFX_PHASE_1 打点 (L1016)
  ↓
CREATE_EXECUTOR 创建执行器 (L1044)
  ↓
输出全空？→ workspaceSize=0 提前返回 (L1048-L1052)
  ↓
inputLayout 必须是 "TND" (L1054-L1059)
  ↓
CheckFormat：禁 FRACTAL_NZ 格式 (L1062)
  ↓
InputDtypeCheck：q/k/v 同 dtype 等 (L1078)
  ↓
AnalysisInput → AnalysisAxis：解析 b/n1/n2/s1/s2/d 轴，
  决定 needPad/needTranspose/needReshape (L1081)
  ↓
Contiguous：全量输入连续化 (L1091)
  ↓
PreprocessQKV：Reshape/Pad/Transpose (L1107)
  ↓
isSupportMultiInput：rope 双输入约束 (L1109)
  ↓
l0op::FlashAttentionScoreEnhance(...) 录入自定义算子 (L1127-L1159)
  ↓
取 outs[0]/[1]/[3]（softmaxOut 暂不使用）
  ↓
Postprocess：输出 Transpose/Slice 去 pad/Reshape (L1166)
  ↓
ViewCopy 把 L0 输出拷到用户输出张量 (L1174-L1180)
  ↓
*workspaceSize = executor->GetWorkspaceSize() (L1182)
  ↓
ReleaseTo(executor) 交还调用方 (L1183)
```

第二段接口只有一个动作：

```text
aclnnFlashAttentionVarLenScoreEnhanceV5 (L1187)
  ↓
L2_DFX_PHASE_2 打点 (L1192)
  ↓
return CommonOpExecutorRun(workspace, workspaceSize, executor, stream) (L1194)
```

#### 4.2.3 源码精读

**（1）入口防御与两个"短路"出口**

- [aclnn_flash_attention_score_enhance.cpp:978-1009](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L978-L1009)——函数签名，与头文件逐参数一致。
- [aclnn_flash_attention_score_enhance.cpp:874-895](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L874-L895)——`CheckFaParam` 对 9 个关键指针做 `CHECK_RET` 判空，任何缺失返回 `ACLNN_ERR_PARAM_NULLPTR`。
- [aclnn_flash_attention_score_enhance.cpp:1044-1052](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1044-L1052)——`CREATE_EXECUTOR()` 创建执行器；随后第一个短路：若三个实体输出全为空张量（`IsEmpty()`），直接 `*workspaceSize = 0` 并 `ReleaseTo(executor)` 返回成功——空输入空输出，无需任何计算。这与 u4 将讲到的 kernel 侧 empty_tensor 处理是同一思想在两层的体现。
- [aclnn_flash_attention_score_enhance.cpp:1054-1059](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1054-L1059)——第二个短路：`strcmp(inputLayout, "TND") != 0` 即报 `ACLNN_ERR_PARAM_INVALID`。**这个 V5 版 aclnn 只放行 TND（变长紧凑排布）**；4.3 节会看到 L0 层其实支持 BSH/SBND/SBH/BNSD/TND 五种——L2 是对 L0 能力的收窄。

**（2）布局分析：决定要不要 Pad/Transpose**

- [aclnn_flash_attention_score_enhance.cpp:71-77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L71-L77)——`enum class InputLayout`：BSND/SBH/BNSD/BSH/TND 五种排布的枚举。
- [aclnn_flash_attention_score_enhance.cpp:180-236](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L180-L236)——`AnalysisAxis`：按 `dimNum + inputLayout 字符串`分派到 5 个解析函数，把用户 shape 归一成语义轴 `b/n1/n2/s1/s2/d/dk/dv`（n1=headNum 来自属性，H1=N1*D），并校验 `qD==kD`、`kD>=vD`。
- [aclnn_flash_attention_score_enhance.cpp:545-654](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L545-L654)——`AnalysisInput` 的对齐决策：D 不按 16/128 对齐时置 `needPad` 并计算 `padNum/padNumv`（L586-L593）；`alignedH1Size` 超过 `MAX_STRIDE_S1=65535` 时置 `needTranspose`（L602-L607 调 `SetShapeInfoForBshBsnd`，其内部 L240-L249 完成判断）。这些 bool 标志随后驱动预处理。

**（3）预处理与后处理：L0 基础算子编排**

- [aclnn_flash_attention_score_enhance.cpp:672-734](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L672-L734)——`Contiguous`：对 q/k/v 及全部可选输入逐个调 `l0op::Contiguous`。文件头部 [L13-L17](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L13-L17) 的 `#include "aclnn_kernels/contiguous.h"`（以及 pad/reshape/slice/transpose）说明这些是 **CANN 自带的 L0 基础算子**，不是本仓库代码。
- [aclnn_flash_attention_score_enhance.cpp:736-813](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L736-L813)——`PreprocessQKV`：按 `needReshape → needPad → needTranspose → SBH 特判回 Reshape` 的顺序改造输入（`l0op::Reshape/l0op::Pad/l0op::Transpose`）。
- [aclnn_flash_attention_score_enhance.cpp:815-872](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L815-L872)——`Postprocess`：对 L0 输出做逆变换——`Transpose` 还原排布（L834-L838），`Slice` 切掉 Pad 出来的 D 尾巴（L840-L861），最后 `Reshape` 成用户输出的原始 shape（L863-L870）。**前后处理互为镜像**，这是 L2 适配层的典型写法。

**（4）核心调用：把自定义算子录进 executor**

- [aclnn_flash_attention_score_enhance.cpp:1127-1159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1127-L1159)——调用 `l0op::FlashAttentionScoreEnhance`（本仓库 L0 实现）。注意三处细节：`dScaleQ/K/V` 传 `nullptr`（L1140-L1142）、`seed/offset/outDtype` 传 `0`（L1154-L1156）、`inputLayout` 传的是 `shapeInfo.l0InputLayoutStr` 而非用户原字符串（L1150）——**L2 按预处理结果可能改写 layout**（例如 BSH 超 stride 限后被改写为 BNSD，见 L243）。
- [aclnn_flash_attention_score_enhance.cpp:1161-1180](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1161-L1180)——L0 返回 4 个输出，取 `outs[0]/[1]/[3]`；L1163 注释直言 `// l0SoftmaxOutOut not used now`，因此 ViewCopy 只有 0、1、3 三次——把 L0 输出拷贝到用户提供的输出张量描述里。
- [aclnn_flash_attention_score_enhance.cpp:1182-1184](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1182-L1184)——`*workspaceSize = uniqueExecutor->GetWorkspaceSize()` 后 `ReleaseTo(executor)` 把执行器所有权移交给调用方。workspace 是**整条流水线**（预处理 + FA + 后处理）的总需求，不是单个算子的。

**（5）第二段：一行执行**

- [aclnn_flash_attention_score_enhance.cpp:1187-1195](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1187-L1195)——第二段接口只有一句有效代码：`return CommonOpExecutorRun(workspace, workspaceSize, executor, stream);`，注释称"固定写法，调用框架能力，完成计算"。

#### 4.2.4 代码实践

**实践目标**：用"加日志"的方式验证你对第一段执行路径的理解（源码阅读型实践，不改源码也能做变体）。

**操作步骤**：

1. 通读 [aclnn_flash_attention_score_enhance.cpp:1011-1185](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1011-L1185)，在纸上按 4.2.2 的流程图标出每一步的行号；
2. 回答三个定位题（只允许看代码作答）：
   - 若想打印"是否做了 Pad、padNum 是多少"，现有代码里已有一条现成日志，它在哪一行？
   - `PreprocessQKV` 中 Pad 只发生在 `padNum != 0` 或 `padNumv != 0` 时吗？（提示：L767 与 L773 是两个独立 if）
   - 为什么 `Postprocess` 用 `Slice` 而不是直接 `Reshape` 去掉 D 维的 pad？
3. （可选，有编译环境时）仿照 L645 的 `OP_LOGD` 风格，在自己的 fork 里给 L1054 的 TND 检查前加一条 `OP_LOGD` 打印 `inputLayout`，重新编译算子包验证日志输出。

**需要观察的现象**：第 2 题的三个答案都能在 100 行窗口内找到；第 3 题若执行，日志应在任何非法 layout 报错之前出现。

**预期结果**：第 2 题参考答案——(a) [L645-L652](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L645-L652) 的 `Analysis input success` 日志已打印 needReshape/needPad/padNum/padNumv/needTranspose/needPadValue 六项；(b) 是，Pad 对 q/k 与 value 分别判断，二者 pad 量可能不同（`padNum` 与 `padNumv`）；(c) 因为 pad 后的 D 维数据在内存上真实存在，`Reshape` 无法凭空丢弃元素，必须 `Slice` 截取前 `d` 列再 `Reshape`。第 3 题运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `workspaceSize` 要由第一段接口算出、而第二段不重新计算？

**答案**：第二段拿到的 executor 是第一段"预演"的产物，所有子算子及其 workspace 需求已定；重新计算意味着要重新走一遍 tiling 与构建流程，两段式设计就失去了意义。同时调用方需要在两段之间自行 `aclrtMalloc`，必须先拿到大小。

**练习 2**：`StrideLimited()`（[L101-L108](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L101-L108)）判断的是哪两款芯片？它为真时才做 Pad/Transpose 预处理，说明什么？

**答案**：`ASCEND910B` 与 `ASCEND910_93`（A2 与 A3）。说明 Pad/Transpose 适配是**按芯片代际条件触发**的——这两代芯片对 stride 和对齐更敏感；其他芯片直接跳过 `PreprocessQKV`（L742-L744 提前返回）。这与 u1-l3 讲的 A2/A3/A5 镜像代际划分对应。

**练习 3**：`PreprocessQKV` 与 `Postprocess` 为什么必须成对出现？只做前者会发生什么？

**答案**：前者把用户输入改造成 Kernel 友好形态（可能 Pad 大、Transpose 换序），L0 输出的 shape/排布因此是"改造后"的；不做后处理，用户拿到的 `attentionOutOut` 会是 Pad 过、排布错乱的形状，与文档承诺的输出 shape 不符。二者互为镜像（Reshape/Pad/Transpose ↔ Transpose/Slice/Reshape）。

### 4.3 op_api 内部分工：L2 与 L0 两级文件及与 def/tiling 的对接

#### 4.3.1 概念说明

本仓库的 op_api 目录实际是**两级结构**：

- **L2（aclnn 文件，`aclnn_` 前缀）**：对外契约层。文件名 = 头文件名，符号是 C 接口，被 torch_npu 扩展、用户的 C++ 程序直接链接。职责：参数校验、布局适配、组装流水线、汇总 workspace。
- **L0（内部文件，无 `aclnn_` 前缀）**：算子封装层。C++ 函数（在 `namespace l0op` 中），职责：给可选输入补默认值、把 `aclIntArray` 转成 `aclTensor`、分配输出、**触发 InferShape**、把算子加入 AICore 下发列表。L0 不知道 stream/workspace 的存在——它只往 executor 里"记录"。

为什么叫 L0/L2？CANN 生态里基础算子（add/transpose/pad）称为 level0，二级组合算子称为 level2。本仓库的自定义 FA 算子也按 L0 风格封装（头文件宏 `OP_API_INC_LEVEL0_OP_...` vs `OP_API_INC_LEVEL2_ACLNN_...`，见两个头文件的 include guard），于是 L2 aclnn 可以像搭积木一样把"自定义 L0 算子 + CANN 自带 L0 基础算子"编排在同一条流水线里。

至于**解耦作用**：op_api 层向下游（tiling/kernel）只传递 `_def.cpp` 声明的输入、输出与 Attr。tiling 从不感知 aclnn 的参数顺序、可选参数默认值、Pad/Transpose 预处理的存在——它只看到"一个符合原型的算子调用"。新增一种 aclnn 包装（或改参数顺序）完全不用动 tiling/kernel。

#### 4.3.2 核心流程

L0 函数 `l0op::FlashAttentionScoreEnhance` 的内部步骤：

```text
OP_TYPE_REGISTER 注册类型名 (L18)
  ↓
L0_DFX 打点 (L54)
  ↓
可选张量为 nullptr → executor->AllocTensor 造空占位 (L88-L108)
  ↓
aclIntArray（prefix/seqLen/startIdx）→ ConvertToTensor 转 INT64 张量 (L110-L158)
  ↓
 AllocTensor 分配 4 个输出；fp8 输入时按 outDtype 选 fp16/bf16 (L160-L172)
  ↓
INFER_SHAPE(FlashAttentionScoreEnhance, OP_INPUT(...), OP_OUTPUT(...), OP_ATTR(...)) (L174)
  ↓
ADD_TO_LAUNCHER_LIST_AICORE(同名算子, 同样的输入/输出/属性) (L213)
  ↓
返回 {softmaxMaxOut, softmaxSumOut, softmaxOutOut, attentionOutOut} (L251)
```

与 tiling 的对接通路：

```text
aclnn 标量参数 (scaleValue/keepProb/headNum/inputLayout/sparseMode/...)
  ↓ 成为 OP_ATTR 的一部分
_def.cpp: this->Attr("input_layout").AttrType(REQUIRED).String() 等 (L382-L395)
  ↓ 运行期进入 TilingContext 的属性集
tiling.cpp: context->GetAttrs()->GetAttrPointer<char>(INPUTLAYOUT_ATTRS_INDEX) (L78)
  ↓
tiling 按 layout 分支切分、写 TilingData/tilingKey（u2-l3 已讲）
```

#### 4.3.3 源码精读

**（1）L0 头文件：两级之间的契约**

- [flash_attention_score_enhance.h:16-29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.h#L16-L29)——`namespace l0op` 中声明内部函数，返回 `std::array<const aclTensor *, 4>`（四个输出），最后一个参数是 `aclOpExecutor *executor`。对比 aclnn 头文件：**L0 没有 workspaceSize 出参、没有 stream**——它只是"记录者"。

**（2）L0 实现：默认值填充与类型归一**

- [flash_attention_score_enhance.cpp:88-108](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L88-L108)——7 个可选张量为 nullptr 时用 `executor->AllocTensor(...)` 造空张量占位（如 attenMask 造 `DT_BOOL` 空张量）。这样下游 InferShape/Kernel 看到的永远是"齐的"输入列表，不必到处判空。
- [flash_attention_score_enhance.cpp:110-158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L110-L158)——5 个 `aclIntArray` 逐个 `ConvertToTensor(..., DataType::DT_INT64)` 转成 Device 张量并强制 `FORMAT_ND`（连续三行 `SetStorageFormat/SetViewFormat/SetOriginalFormat`）。**这就是 `_def.cpp` 里 `prefix`/`actual_seq_qlen` 等声明为 Input（INT64 张量）而非 Attr 的原因**——进入算子世界前，数组必须张量化。
- [flash_attention_score_enhance.cpp:160-172](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L160-L172)——4 个输出全部由 `executor->AllocTensor` 分配（注意 `softmaxMaxOut`/`softmaxSumOut` 固定 `DT_FLOAT`——它们是反向算子要消费的中间量）；输入为 fp8/hifloat8 时按 `outDtype` 属性决定输出是 fp16 还是 bf16。

**（3）两个关键宏：InferShape 与下发**

- [flash_attention_score_enhance.cpp:174-211](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L174-L211)——`INFER_SHAPE(FlashAttentionScoreEnhance, OP_INPUT(20 个输入), OP_OUTPUT(4 个输出), OP_ATTR(14 个属性))`：按算子名找到 `_def.cpp` 注册的原型与 `flash_attention_score_enhance_infershape.cpp` 里的推导实现，完成输出 shape 推导。失败则 `OP_LOGE` 并返回四个 nullptr。
- [flash_attention_score_enhance.cpp:213-250](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L213-L250)——`ADD_TO_LAUNCHER_LIST_AICORE`：把算子连同完全一致的输入/输出/属性加入 executor 的下发列表。**真正执行时，框架才按 def 的芯片配置找到 tiling 与 kernel**。两个宏的参数列表必须与 `_def.cpp` 的声明严格对应——这就是四层靠算子名与参数顺序对齐的具体落点。
- 顺带一提：L0 签名里的 `preTockens/nextTockens`（[L40-L41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/flash_attention_score_enhance.cpp#L40-L41)，拼写沿用历史笔误）与 `_def.cpp` L384-L385 的 `"pre_tockens"/"next_tockens"` 完全一致——改名必须四层同步的又一例证。

**（4）透传给 tiling 的证据链**

- [flash_attention_score_enhance_def.cpp:382-395](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L382-L395)——14 个 Attr 声明。逐条对照 aclnn 标量参数：`scale_value`(默认 1.0)、`keep_prob`(1.0)、`pre_tockens`/`next_tockens`(INT_MAX)、`head_num`(**REQUIRED**)、`input_layout`(**REQUIRED** String)、`inner_precise`(0)、`sparse_mode`(0)、`pse_type`(1)、`seed`(0)、`offset`(0)、`out_dtype`(0)、`softmax_out_layout`("")、`sink_num`(0)。aclnn 的 11 个公开标量是它的子集（seed/offset/out_dtype 由 L2 写死为 0）。
- [flash_attention_score_enhance_tiling.cpp:71-120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L71-L120)——tiling 侧 `CheckParams`：`context->GetAttrs()->GetAttrPointer<char>(INPUTLAYOUT_ATTRS_INDEX)` 取出 `input_layout` 字符串，随后按首字母 `B`/`T`/`S` 分支校验 q/k/v shape 一致性。**aclnn 的 `inputLayout` 参数 → Attr `input_layout` → tiling 的 `GetAttrPointer`**，透传链闭环。`sparse_mode` 同理（在 tiling 分文件中被读取，用于稀疏模式分支，见 u4-l2/u4-l5）。

**（5）上层如何消费 aclnn：torch 扩展层的一幕预览**

- [npu_flash_attention_score_enhance.cpp:75-105](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/attention/flash_attention_score_enhance/csrc/npu_flash_attention_score_enhance.cpp#L75-L105)——torch_ops_extension 用一条 `EXEC_NPU_CMD_V1(aclnnFlashAttentionVarLenScoreEnhanceV5, ...)` 宏按 31 个参数顺序（与头文件 L24-L54 完全一致，含 `softmax_layout_char`、`sink_num`）动态解析并调用两段式接口。宏内部替你完成了"第一段→申请 workspace→第二段"的全套动作——这正是 u1-l2 说"torch 扩展按名动态解析 aclnn 符号"的实景。

#### 4.3.4 代码实践

**实践目标**：亲手验证"aclnn 标量参数 → def Attr → tiling 读取"的透传链。

**操作步骤**：

1. 打开 [flash_attention_score_enhance_def.cpp:382-395](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_def.cpp#L382-L395)，抄下 14 个 Attr 的名字、类型、必选性、默认值；
2. 在 op_host 目录执行：

   ```bash
   grep -rn "GetAttrPointer" ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/ | head -20
   ```

3. 对每条 grep 结果，确认它读取的属性索引常量（如 `INPUTLAYOUT_ATTRS_INDEX`）对应 def 里的哪个 Attr（索引常量通常定义在各 tiling 头文件中，顺序与 def 声明顺序一致）；
4. 用表格记录三列：aclnn 参数名 | def Attr 名 | tiling 读取位置（文件:行）。

**需要观察的现象**：`input_layout`、`sparse_mode`、`head_num`、`sink_num` 至少四个属性能在 tiling 侧找到 `GetAttrPointer` 或 `GetAttrInt`/等价读取；数组型输入（actual_seq_qlen 等）则**不会**出现在 Attr 读取里，而是走 `GetInputShape`/张量路径。

**预期结果**：得到一张约 6-8 行的透传对照表，并得出结论："aclnn 的标量走 Attr 通路、数组走张量化后的 Input 通路"。纯静态阅读可完成；grep 命令在仓库根目录（training/）下执行。

#### 4.3.5 小练习与答案

**练习 1**：L2 为什么要把 `seed/offset/outDtype` 写死为 0 传给 L0，而不暴露给自己的调用方？

**答案**：V5 这版公开接口当前不需要这些能力（无 dropout 随机种子控制、无 fp8 输出 dtype 切换的需求），L0 保留参数是为了内部完整性与其他内部调用方复用。L2 是对外能力的"收窄阀门"——同类例子还有 layout 只放行 TND。

**练习 2**：`INFER_SHAPE` 和 `ADD_TO_LAUNCHER_LIST_AICORE` 两个宏的参数列表为什么长得几乎一样？删掉其中一个会怎样？

**答案**：前者按算子名 + 输入输出属性触发输出 shape 推导（对应 op_host 的 infershape 文件），后者把算子加入实际下发列表（进而触发 tiling 与 kernel）。二者面向框架的不同阶段，但都以"同一份原型描述"为键，所以参数一致。删掉 `INFER_SHAPE`，输出张量的 shape 推不出来；删掉 `ADD_TO_LAUNCHER_LIST_AICORE`，executor 里没有这个算子，第二段执行时什么都不会算（静默错误）。

**练习 3**：`ai_infra_aggregate_hidden`（mome 族）没有 op_api 源码也能被 torch 调用，二者矛盾吗？

**答案**：不矛盾。aggregate_hidden 输入规整、无需 Pad/Transpose 预处理，其 aclnn 接口由 CANN 的 op_build 工具在配置期从 `_def.cpp` 自动生成进 autogen/ 目录（u1-l4 讲过）；FA enhance 因为要手写 L2 适配逻辑，才把 op_api 源码显式放进仓库。有无 op_api 源码是"是否需要自定义 L2 逻辑"的区别，不是"是否暴露 aclnn"的区别。

## 5. 综合实践

**任务**：为 `aclnnFlashAttentionVarLenScoreEnhanceV5` 编写一份**接口签名摘要表**，并写一个 torch_npu 风格的 Python 调用示例脚本，标出哪些参数会透传给 tiling。

### 步骤一：接口签名摘要表

依据 [aclnn_flash_attention_score_enhance.h:24-56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56)（参数顺序与类型）与 [docs 参数表](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md#L76-L403)（含义）整理（"透传 tiling"列依据 4.3 节证据链）：

| # | 参数名 | 类型 | 必选/可选 | 含义 | 透传给 tiling？ |
| --- | --- | --- | --- | --- | --- |
| 1 | query | aclTensor* | 必选 | 查询矩阵，TND 排布 [T,N1,D] | 作为输入张量（shape 参与） |
| 2 | queryRope | aclTensor* | 可选 | query 的 rope 分量（BF16） | 是（张量化后 Input） |
| 3 | key | aclTensor* | 必选 | 键矩阵 [T,N2,D] | 作为输入张量 |
| 4 | keyRope | aclTensor* | 可选 | key 的 rope 分量 | 是 |
| 5 | value | aclTensor* | 必选 | 值矩阵 [T,N2,Dv] | 作为输入张量 |
| 6 | realShiftOptional | aclTensor* | 可选 | pse 位置编码偏移 | 是（Input `real_shift`） |
| 7 | dropMaskOptional | aclTensor* | 可选 | dropout 掩码（UINT8） | 是（Input `drop_mask`） |
| 8 | paddingMaskOptional | aclTensor* | 可选 | padding 掩码 | 是（Input `padding_mask`） |
| 9 | attenMaskOptional | aclTensor* | 可选 | 注意力掩码（BOOL/UINT8，1=不参与） | 是（Input `atten_mask`） |
| 10 | sinkOptional | aclTensor* | 可选 | sink 汇项（FP32，[headNum]） | 是（Input `sink`） |
| 11 | prefixOptional | aclIntArray* | 可选 | prefix 稀疏每 batch 的 N 值 | 是（转 INT64 张量后 Input） |
| 12 | actualSeqQLenOptional | aclIntArray* | 必填数组 | 每 batch 的真实 Q 长度 | 是（转张量后 Input） |
| 13 | actualSeqKvLenOptional | aclIntArray* | 必填数组 | 每 batch 的真实 KV 长度 | 是（转张量后 Input） |
| 14 | qStartIdxOptional | aclIntArray* | 可选 | 外切场景 Q 全局起始索引 | 是（转张量后 Input） |
| 15 | kvStartIdxOptional | aclIntArray* | 可选 | 外切场景 KV 全局起始索引 | 是（转张量后 Input） |
| 16 | scaleValue | double | 可选(1.0) | softmax 缩放系数 | **是（Attr `scale_value`）** |
| 17 | keepProb | double | 可选(1.0) | dropout 保留比例 (0,1] | **是（Attr `keep_prob`）** |
| 18 | preTokens | int64_t | 可选 | 滑窗左边界（稀疏） | **是（Attr `pre_tockens`）** |
| 19 | nextTokens | int64_t | 可选 | 滑窗右边界 | **是（Attr `next_tockens`）** |
| 20 | headNum | int64_t | **必选** | query 头数 N1 | **是（Attr `head_num`，REQUIRED）** |
| 21 | inputLayout | char* | **必选** | 输入排布，本接口仅支持 "TND" | **是（Attr `input_layout`，REQUIRED）** |
| 22 | innerPrecise | int64_t | 可选(0) | 精度开关（暂未使用） | 是（Attr `inner_precise`） |
| 23 | sparseMode | int64_t | 可选(0) | 稀疏模式 0-8（不支持 5；带 rope 不支持 6） | **是（Attr `sparse_mode`）** |
| 24 | pseType | int64_t | 可选(1) | pse 的 mul/add 顺序（0-3） | 是（Attr `pse_type`） |
| 25 | softmaxOutLayout | char* | 可选("") | ""→NTD 输出；"same_as_input"→TND | 是（Attr `softmax_out_layout`） |
| 26 | sinkNum | int64_t | 可选(0) | Param Sink token 数=sinkNum*64（0-8） | 是（Attr `sink_num`） |
| 27 | softmaxMaxOut | aclTensor* | 必选输出 | softmax max 中间量 [T,N,8]/[N,T,8] FP32（反向要用） | 输出张量 |
| 28 | softmaxSumOut | aclTensor* | 必选输出 | softmax sum 中间量（反向要用） | 输出张量 |
| 29 | softmaxOutOut | aclTensor* | 输出 | softmax 矩阵（当前实现未使用） | — |
| 30 | attentionOutOut | aclTensor* | 必选输出 | 最终注意力输出 [T,N,Dv] | 输出张量 |
| 31 | workspaceSize | uint64_t* | 出参 | Device 工作区大小 | — |
| 32 | executor | aclOpExecutor** | 出参 | 算子执行器 | — |

> 注意第 24/25 行顺序：**头文件是 `pseType → softmaxOutLayout(char*) → sinkNum(int64_t)`**，与上表行序一致；docs 原型把后两者写反了，勿混。

### 步骤二：torch_npu 风格调用脚本（示例代码）

仓库里 torch 侧的暴露名是 `custom::npu_flash_attention_score_enhance`，schema 带默认值，见 [ops_def_registration.cpp:19-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L19-L24) 与 [npu_flash_attention_score_enhance.cpp:530-538](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/attention/flash_attention_score_enhance/csrc/npu_flash_attention_score_enhance.cpp#L530-L538)。仿照 aggregate_hidden README 的单算子示例风格（[README.md:61-84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L61-L84)）：

```python
# 示例代码：调用 flash_attention_score_enhance 前向算子（TND 变长场景）
# 前置：已安装编译好的算子包（u1-l4）并 pip install torch_ops_extension 的 wheel
import torch
import torch_npu
import numpy as np
import omni_training_custom_ops  # 导入后 torch.ops.custom 命名空间生效

# 变长场景：1 个 batch，实际 Q/KV 长度均为 1024（TND 中 T = sum(SeqLen)）
t, n, d = 1024, 1, 128
query = torch.randn(t, n, d).to(torch.bfloat16).npu()
key   = torch.randn(t, n, d).to(torch.bfloat16).npu()
value = torch.randn(t, n, d).to(torch.bfloat16).npu()
actual_seq_qlen  = [1024]   # → aclIntArray → 张量化为 Input actual_seq_qlen
actual_seq_kvlen = [1024]

attention_out, softmax_max, softmax_sum = torch.ops.custom.npu_flash_attention_score_enhance(
    query, key, value,
    head_num=n,                # → Attr head_num（REQUIRED）→ tiling 读取
    input_layout="TND",        # → Attr input_layout（REQUIRED）→ tiling 用它选切分分支
    pse=None, padding_mask=None, atten_mask=None,
    sink_tensor=None, query_rope=None, key_rope=None,
    scale=1.0 / (d ** 0.5),    # → Attr scale_value
    keep_prob=1.0,             # → Attr keep_prob
    pre_tokens=2147483647,     # → Attr pre_tockens
    next_tokens=2147483647,    # → Attr next_tockens
    inner_precise=0,           # → Attr inner_precise
    prefix=None,
    actual_seq_qlen=actual_seq_qlen,
    actual_seq_kvlen=actual_seq_kvlen,
    sparse_mode=0,             # → Attr sparse_mode → tiling 稀疏分支
    sink_num=0,                # → Attr sink_num
    pse_type=1,                # → Attr pse_type
    softmaxOutLayout="same_as_input",  # → Attr softmax_out_layout
    q_start_idx=None, kv_start_idx=None)

print(attention_out.shape)   # 预期 [1024, 1, 128]（TND）
print(softmax_max.shape)     # 预期 [1024, 1, 8]（FP32）
print(softmax_sum.shape)     # 预期 [1024, 1, 8]（FP32）
# 反向提示：softmax_max/softmax_sum 是 flash_attention_score_grad_enhance 的输入，
# 训练链路里必须保存（见 u4-l1/u4-l4）。
```

### 步骤三：运行与验证

1. 在装好算子包与 wheel 的 NPU 容器中运行脚本；无 NPU 环境时，用 `python -m py_compile` 校验脚本语法，并对照 [npu_flash_attention_score_enhance.cpp:217-242](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/attention/flash_attention_score_enhance/csrc/npu_flash_attention_score_enhance.cpp#L217-L242) 的 Autograd forward 形参逐个核对关键字参数名；
2. 把传入 `sparse_mode` 改为 5（文档声明不支持），或把 `input_layout` 改为 "BSH"，观察第一段接口的报错信息（预期 `ACLNN_ERR_PARAM_INVALID`，报错文案见 [L1055](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L1054-L1059) 的 "Layout %s is not TND"）；
3. 记录输出 shape 与摘要表 27/28/30 行的预期是否一致。运行结果**待本地验证**（本讲义写作环境无 NPU）。

## 6. 本讲小结

- aclnn 采用**两段式约定**：第一段 `GetWorkspaceSize` 在 Host 上校验参数、构建 executor（把整条任务流水线录进去）并算出 workspace 总量；第二段只有 4 个参数，靠 `CommonOpExecutorRun` 把 executor 记录的任务下发到 stream。
- 第一段是"大脑"：判空 → TND 白名单 → dtype/format/轴分析 → Contiguous/Reshape/Pad/Transpose 预处理 → 调 L0 → Transpose/Slice/Reshape 后处理 → ViewCopy 到用户输出 → 汇总 workspace。**预处理本身也是 L0 基础算子，与自定义算子同录一个 executor**。
- op_api 目录分两级：`aclnn_*` 文件是 L2 对外契约（C 符号、32 参签名）；无前缀文件是 L0 内部封装（`namespace l0op`，负责可选参数补默认值、IntArray 张量化、`INFER_SHAPE` + `ADD_TO_LAUNCHER_LIST_AICORE`）。
- **op_api 对 tiling/kernel 的解耦**体现在：tiling 只见 def 原型——标量参数经 Attr（`input_layout`/`sparse_mode`/`head_num`…，def L382-L395）由 `GetAttrs()->GetAttrPointer` 读取，数组参数经 `ConvertToTensor` 张量化后走 Input；aclnn 侧改参数顺序、加预处理都不波及 tiling/kernel。
- 读接口**以头文件为准**：docs 的原型与示例在 `sinkNum`/`softmaxOutLayout` 顺序上滞后于头文件；`softmaxOutOut` 在当前 V5 实现中未被使用（"not used now"）。
- `EXEC_NPU_CMD_V1`（torch_ops_extension）按名动态解析 aclnn 符号并自动完成两段调用，是 Python 侧无感使用两段式接口的桥梁。

## 7. 下一步学习建议

本讲补齐了单算子四层结构的最后一块，u2 单元到此收官。接下来：

1. **第 3 单元（公共组件）**：先读 [u3-l1]（utils 错误日志）——本讲反复出现的 `OP_LOGE/OP_LOGD/CHECK_RET` 正来自那里；再读 [u3-l3]（tiling_base 框架），理解多实现 tiling 的责任链。
2. **第 4 单元（Attention 族）**：[u4-l1] 将回到本讲的算子，从文档与 `_def.cpp` 总览 FA 前向的输入输出规模；[u4-l2]/[u4-l3] 深入它的 tiling 与 kernel——届时你会看到本讲 `input_layout`/`sparse_mode` 透传下去后如何决定 tilingKey 与 kernel 分支。
3. **第 6 单元（torch_ops_extension）**：[u6-l2] 精读 `EXEC_NPU_CMD_V1` 背后的 Autograd Function 与 `TORCH_LIBRARY_IMPL` 注册机制，把本讲 4.3.3 第（5）点的一幕展开。
4. 想立即动手的读者：把综合实践的脚本扩展成带 `atten_mask` 与 `sparse_mode=2`（下三角）的用例，对照 docs 约束说明（第 512-521 行）预测行为后上机验证。
