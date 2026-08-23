# tiling_base 框架：TilingBase 责任链与模板注册

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `TilingBase` 基类的「模板方法」执行框架：七个固定步骤、`GRAPH_SUCCESS / GRAPH_FAILED / GRAPH_PARAM_INVALID` 三态返回值各自的调度语义。
2. 解释 `tiling_templates_registry.h` 中两套注册表（带 socVersion 的 `TilingRegistryNew` 与不带的 `TilingRegistry`）如何用「优先级 + 责任链」在多个候选 tiling 实现之间做运行期选择。
3. 掌握 tilingKey 的十进制位组装编码习惯（`RecursiveSum` + `10^19` 偏移），以及它和 kernel 侧分支、编译产物之间的关系。
4. 了解 `tiling_util`、`data_copy_transpose_tiling` 等公共工具的真实现状（有些在仓库内并无调用者）。
5. 能对照 `sinkhorn_enhance` 的两级 tiling 文件，独立画出一帧请求的 tiling 类执行链（含 `GRAPH_PARAM_INVALID` 回退路径）。

## 2. 前置知识

本讲是 u2-l3（Tiling 入门）的进阶篇，先回顾并补充几个概念：

- **模板方法模式（Template Method）**：父类固定「先做什么、后做什么」的流程骨架，把每一步的具体实现声明为纯虚函数，交给子类填写。`TilingBase::DoTiling()` 就是这个骨架。
- **责任链模式（Chain of Responsibility）**：把多个处理者按优先级排成一条链，请求沿链传递；每个处理者要么处理掉请求，要么说「不是我的」并把机会让给下一个。本框架里「让位」的信号就是 `GRAPH_PARAM_INVALID`。
- **三态返回值**：CANN 的 `ge::graphStatus` 有多个取值，本框架只关心三个：
  - `GRAPH_SUCCESS`：本实现成功完成 tiling，链路终止；
  - `GRAPH_FAILED`：发生不可恢复错误，整个 tiling 流程立即中止；
  - `GRAPH_PARAM_INVALID`：本实现不支持当前输入（shape/layout/数据类型不匹配），换下一个实现再试。
- **静态注册（static registration）**：在 `.cpp` 里用一个宏创建全局静态对象，该对象在动态库被加载时（早于任何函数调用）执行构造函数，从而把「类 → 工厂函数」写进全局单例注册表。u2-l2 讲过的 `OP_ADD` 是原型注册，本讲的 `REGISTER_TILING_TEMPLATE*` 是 tiling 实现注册，套路相同。
- **tilingKey 回顾**（u2-l3/u2-l4）：Host 侧 tiling 写入 `SetTilingKey`，Device 侧 kernel 入口用 `TILING_KEY_IS` 读取并选择分支。同一个算子的不同「kernel 模板」共用一份 `.so`，靠 tilingKey 区分。
- **优先级 map**：`std::map<int32_t, F>` 按 key 升序遍历，所以「优先级数值越小，越先被执行」，也就是优先级越高。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h` | `TilingBase` 抽象基类：七步模板方法、三态契约、平台参数结构体、调试打印工具 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h` | 两套注册表（`TilingRegistryNew` / `TilingRegistry`）、`TilingCases` 优先级表、`RegisterNew` / `Register` 流式注册器、四个注册宏 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h` | tilingKey 十进制位组装：`RecursiveSum`、`GET_TILINGKEY`、`TILINGKEY` 宏 |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_type.h` | 位组装用到的枚举（`AxisEnum` / `DtypeEnum` / `LayoutEnum` / `SparseEnum` 等）及同套编码函数的 `optiling` 命名空间副本 |
| `ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp` | 公共小工具：`IsRegbaseSocVersion`、`EnsureNotScalar`（头文件 `common/include/tiling_base/tiling_util.h`） |
| `ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h` | 转置搬运参数预计算工具 `GetDataCopyTransposeTiling`（自由函数版本） |
| `ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp` | sinkhorn 接入层：框架入口函数 + `IMPL_OP_OPTILING` 注册（「两级 tiling」的第一级） |
| `ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp` | sinkhorn 实现层：`SinkhornTilingBase` 类（继承 `TilingBase`）+ 优先级注册（第二级） |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp` | FA 前向 tiling 入口：真实的多模板责任链调度现场 |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp` | FA 各候选 tiling 模板实现与六个优先级注册（90/94/95/96/97/98） |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h` | FA 的 `FlashAttentionScoreEnhanceCompileInfo` 结构体（`TilingParse` 编译期信息契约） |

## 4. 核心概念与源码讲解

### 4.1 TilingBase 基类：七步模板方法与三态契约

#### 4.1.1 概念说明

u2-l3 里我们读的 `ai_infra_aggregate_hidden_tiling.cpp` 是「一个函数包打天下」的写法：入口函数里顺序做取参、校验、切分、写 TilingData。当算子变复杂（比如 FlashAttention 要支持多种 layout、多种稀疏模式、多种数据类型），单个函数会膨胀成几千行 if-else。

`TilingBase` 解决的就是这个问题：它把一次 tiling 拆成七个固定步骤，用「基类定流程、子类填内容」的方式组织代码；再配合下一节的注册表，允许**同一个算子注册多个 tiling 类**，每个类只负责自己最擅长的那类输入。这就是标题里说的「责任链」。

#### 4.1.2 核心流程

`DoTiling()` 的执行顺序（以实际代码为准）：

```text
DoTiling()
 ├─ 1. GetShapeAttrsInfo()   读输入/输出 shape 与 Attr，做参数校验（失败 → 直接返回）
 ├─ 2. GetPlatformInfo()     读平台信息：AIV/AIC 核数、UB/L1/L0 大小（失败 → 直接返回）
 ├─ 3. IsCapable()           本类是否支持当前输入？
 │       false → 返回 GRAPH_PARAM_INVALID（把机会让给链上的下一个类）
 ├─ 4. DoOpTiling()          计算数据切分，填 TilingData
 ├─ 5. DoLibApiTiling()      计算高阶 API（如 Matmul）的 tiling 参数
 ├─ 6. GetWorkspaceSize()    计算 workspace 大小
 ├─ 7. PostTiling()          SetBlockDim + 把 TilingData 写回 context
 └─ 最后：context_->SetTilingKey(GetTilingKey())
```

注意一个容易踩坑的细节：基类保护段注释把 `GetPlatformInfo` 编号为第 1 步、`GetShapeAttrsInfo` 编号为第 2 步，但 `DoTiling()` 实际是**先取 shape/attr、后取平台信息**。阅读时以 `DoTiling()` 的调用顺序为准。

三态语义总结：

| 返回值 | 谁产生 | 注册表如何反应 |
| --- | --- | --- |
| `GRAPH_SUCCESS` | 七步全部走完 | 链路终止，tiling 成功 |
| `GRAPH_FAILED` | 任一步校验失败 | 立即中止整个 tiling（不再尝试后续类） |
| `GRAPH_PARAM_INVALID` | `IsCapable()` 为 false，或某步主动返回 | 忽略本类，取下一个优先级的类继续 |

#### 4.1.3 源码精读

执行骨架与三态语义的核心代码：

[tiling_base.h:77-113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L77-L113) —— `DoTiling()` 非虚函数固定七步流程；其中第 91-93 行是责任链的关键：`IsCapable()` 返回 false 就转成 `ge::GRAPH_PARAM_INVALID` 上抛；第 110 行在七步成功后才把子类算出的 `GetTilingKey()` 写进 context。

[tiling_base.h:121-136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L121-L136) —— 七个纯虚函数声明。子类必须全部实现（不需要的步骤返回 `GRAPH_SUCCESS` 即可，例如 sinkhorn 的 `DoLibApiTiling` 是空实现）。`GetTilingKey()` 被标了 `[[nodiscard]]`，因为漏掉它 kernel 侧就选不到正确分支。

[tiling_base.h:138-146](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L138-L146) —— 静态工具 `CalcTschBlockDim`：Cube（AIC）+ Vector（AIV）协同算子里，一个 AIC 通常搭配 \( r = \text{aiv}/\text{aic} \) 个 AIV，因此按 AIC 切了 `sliceNum` 份后，TSCH（任务调度）维的 blockDim 要按 \( \lceil \text{sliceNum}/r \rceil \) 对齐；当 AIC 数为 0、AIV 数为 0 或 AIC 多于 AIV 时退化为直接返回 `sliceNum`。

[tiling_base.h:35-57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L35-L57) —— `AiCoreParams`（运行期平台参数的容器）与 `CompileInfoCommon`（编译期信息结构体）。`CompileInfoCommon` 里的 `socVersion` 字段在下一节 `DoTilingImpl` 里有特殊用途：当 `context->GetPlatformInfo()` 为空（典型场景是 UT 的 faker 上下文）时，框架从这个结构体里取 soc 版本。

[tiling_base.h:209-215](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L209-L215) —— 保护成员：`context_`（tiling 上下文）、`blockDim_`、`workspaceSize_`、`tilingKey_`、`aicoreParams_`。子类的七个步骤就是把数据算出来放进这些字段（或自己的成员），最后由 `PostTiling` 统一写回 context。

[tiling_base.h:196-207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L196-L207) —— `GetTilingDataDebugStr()`：把 RawTilingData 按 `int32_t` 逐个打印，调 tiling 问题时配合 `OP_LOGI` 很有用。

[tiling_base.h:25-29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L25-L29) —— `ASCENDC_OP_TEST` 编译开关：编 UT 时 `ASCENDC_EXTERN_C` 展开为 `extern "C"`，保证 tiling 入口函数符号不被 C++ 名字修饰，UT 侧才能按 C 符号链接（u8 单元会用到）。

一个真实的子类对照——sinkhorn 的 `IsCapable` 与空步骤：

[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:95-98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L95-L98) —— sinkhorn 只有一个 tiling 类，`IsCapable()` 恒返回 true（所有合法性判断放在 `GetShapeAttrsInfo`/`DoOpTiling` 里，失败走 `GRAPH_FAILED`）。

[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:534-544](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L534-L544) —— 该算子不用高阶 API，`DoLibApiTiling()` 直接返回成功；workspace 固定 16MB。

#### 4.1.4 代码实践

**实践目标**：把「七步模板方法」从抽象概念落到具体代码，方法是给 sinkhorn 的七步实现建一张对照表。

**操作步骤**：

1. 打开 [tiling_base.h:77-113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L77-L113)，把七个虚函数调用抄下来作为表格左列。
2. 在 `manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp` 中定位每个函数的实现，记录行号与一句话职责，例如：
   - `GetShapeAttrsInfo` → L157-227：取 x 的 shape/dtype、三个属性（out_flag/eps/num_iters）、按 outFlag_ 决定是否取可选输出；
   - `GetPlatformInfo` → L143-155：取 AIV 核数与 UB 大小放进 `aicoreParams_`；
   - `IsCapable` → L95-98：恒 true；
   - `DoOpTiling` → L491-532：CheckInputShape → CheckOutputShape →（可选输出校验）→ SplitCores → 预计算 reduceMask；
   - `DoLibApiTiling` → L534-537：空；
   - `GetWorkspaceSize` → L539-544：固定 16MB；
   - `PostTiling` → L546-554：`SetBlockDim(needCoreNum)` + `SaveToBuffer` 写回 TilingData。
3. 回答：`DoTiling()` 第 110 行 `SetTilingKey(GetTilingKey())` 对应 sinkhorn 的哪个函数？（L556-561，按 `out_flag` 返回 0 或 1。）

**需要观察的现象**：表格填完后你会发现子类没有任何一个函数直接调用「下一步」——步骤衔接完全由基类 `DoTiling()` 驱动，这正是模板方法的特征。

**预期结果**：得到一张 7 行的「步骤 → sinkhorn 实现行号 → 职责」对照表；并能解释为什么 sinkhorn 把 shape 校验放在 `GetShapeAttrsInfo`/`DoOpTiling` 而不是 `IsCapable`（它只有一个实现，不存在「让位给别的类」的需求，校验失败应当直接报错终止）。

#### 4.1.5 小练习与答案

**练习 1**：`DoTiling()` 中 `GetShapeAttrsInfo` 和 `GetPlatformInfo` 谁先执行？与基类注释里的编号一致吗？

答案：先执行 `GetShapeAttrsInfo`（L83），后执行 `GetPlatformInfo`（L87）；与保护段注释的编号（平台信息标 1、shape 标 2）**不一致**，阅读时应以 `DoTiling()` 实际调用顺序为准。

**练习 2**：子类想表达「这个输入我不处理，让别人来」，有哪两种写法？

答案：(1) `IsCapable()` 返回 false，基类会把它转换成 `GRAPH_PARAM_INVALID`；(2) 在任一步（通常是 `DoOpTiling`）直接 `return ge::GRAPH_PARAM_INVALID`，注册表同样会跳过本类继续下一个优先级（FA 的 DropMask 模板就是这么用的，见 4.2.3）。

**练习 3**：`CalcTschBlockDim(16, 8, 32)` 返回多少？

答案：`ration = 32 / 8 = 4`，返回 `(16 + 4 - 1) / 4 = 4`。含义：8 个 AIC 切了 16 份任务、32 个 AIV 每 4 个服侍 1 个 AIC，TSCH 维只需 4 个块。

### 4.2 tiling_templates_registry：多实现注册与责任链调度

#### 4.2.1 概念说明

有了 `TilingBase` 还不够——框架怎么知道「算子 X 有哪几个 tiling 类、按什么顺序试」？`tiling_templates_registry.h` 回答这个问题，它提供：

- **工厂函数** `TILING_CLASS<T>`：把「类模板参数」变成「返回 `unique_ptr<TilingBase>` 的函数指针」，实现「注册类」而非「注册对象」（每帧请求都要新建对象）。
- **优先级表** `TilingCases`：`map<优先级, 工厂函数>`，`AddTiling` 会拒绝重复优先级。
- **两套注册表**：`TilingRegistryNew`（外层再按 socVersion 分桶，`map<soc_version, map<op_type, TilingCases>>`）与 `TilingRegistry`（只有 `map<op_type, TilingCases>`）。名字带 New 的是「按芯片分实现」的版本——同一算子在 910B 和 910_93 上可以注册完全不同的 tiling 类集合。
- **调度入口** `DoTilingImpl`：责任链的「引擎」，按优先级升序逐个实例化并调 `DoTiling()`，直到出现非 `GRAPH_PARAM_INVALID` 的结果。
- **流式注册器与四个宏**：让 `.cpp` 里一行代码完成注册。

#### 4.2.2 核心流程

一帧请求的调度过程（以带 socVersion 的 `TilingRegistryNew` 为例）：

```text
DoTilingImpl(context)
 ├─ 解析算子名 opType = context->GetNodeType()
 ├─ 解析 soc_version：
 │    ├─ platformInfo 非空（真实环境）→ PlatformAscendC::GetSocVersion()
 │    └─ platformInfo 为空（UT faker 环境）→ CompileInfoCommon::socVersion
 ├─ GetTilingTemplates(opType, soc_version)
 │    返回该算子在该芯片下的 map{priority → 工厂函数}（std::map 升序）
 └─ for (priority 从小到大):
        实例化 tiling 类 → DoTiling()
        ├─ GRAPH_SUCCESS      → 返回成功，结束
        ├─ GRAPH_FAILED       → 返回失败，结束（不再尝试后续类）
        └─ GRAPH_PARAM_INVALID → 打日志，继续下一个 priority
    全部让位 → GRAPH_FAILED（"no valid template is found"）
```

注册发生在**动态库加载时**：注册宏展开为一个全局静态 `RegisterNew`/`Register` 对象，其构造函数调用 `tiling<T>(priority, ...)` 把工厂函数塞进单例的 map，时机早于任何一次 tiling 请求。

#### 4.2.3 源码精读

[tiling_templates_registry.h:29-35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L29-L35) —— `TILING_CLASS<T>` 工厂模板与 `TilingClassCase` 函数指针类型：注册的是「如何造一个 tiling 对象」，而不是对象本身。

[tiling_templates_registry.h:37-61](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L37-L61) —— `TilingCases`：优先级表本体。`AddTiling` 第 45-46 行检查同一优先级重复注册并报错，防止两个类无声地互相覆盖。

[tiling_templates_registry.h:97-131](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L97-L131) —— `TilingRegistryNew::DoTilingImpl(context)` 责任链主循环。第 102-116 行是双通道 soc 解析（平台信息优先，编译期信息兜底）；第 118-128 行的 for 循环里，第 122 行 `if (status != ge::GRAPH_PARAM_INVALID) return status;` 一行同时表达「成功返回」与「失败中止」两种终止，只有 PARAM_INVALID 才落到下一轮。

[tiling_templates_registry.h:133-166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L133-L166) —— 第二个重载 `DoTilingImpl(context, priorities)`：调用方显式给定优先级顺序（而非全表升序），供只需要试特定几个实现的场景。

[tiling_templates_registry.h:168-185](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L168-L185) —— `GetTilingTemplates`：两级 map 查找，soc 不存在或算子名不存在都返回空的 `empty_tiling_case_`（配合 OP_LOGE 报错），最终 `DoTilingImpl` 会以「no valid template is found」失败。

[tiling_templates_registry.h:187-217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L187-L217) —— `RegisterNew` 流式注册器：`.tiling<T>(priority, soc_version)` 支持单个 soc 或 `std::vector<int32_t>` 一批 soc，返回 `*this` 可链式调用。

[tiling_templates_registry.h:220-243](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L220-L243) —— 不带 soc 维度的 `TilingRegistry`：结构对称，只是 `registry_map_` 只有一层（第 296 行）。sinkhorn 用的就是它。

[tiling_templates_registry.h:322-347](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L322-L347) —— 四个注册宏：`REGISTER_TILING_TEMPLATE_WITH_SOCVERSION`（多 soc 列表）、`REGISTER_TILING_TEMPLATE_NEW`（单 soc）、`REGISTER_TILING_TEMPLATE`（不带 soc）、`REGISTER_OPS_TILING_TEMPLATE`（不带 soc 的新版，`op_type` 参数不要加引号）。注释第 323 行明确写了优先级语义：**越小优先级越高**。

真实注册现场——sinkhorn 与 FA：

[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:580](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L580) —— sinkhorn 用 `REGISTER_OPS_TILING_TEMPLATE(ManifoldConstrainedHyperConnectionSinkhornEnhance, SinkhornTilingBase, 2000)` 注册唯一一个实现，优先级 2000。

[flash_attention_score_enhance_tiling_general.cpp:5053-5089](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5053-L5089) —— FA 前向按 90/94/95/96/97/98 六个优先级注册六个类，全部限定在 `ASCEND910B` 与 `ASCEND910_93` 两个 soc 上。90 最高优先。

责任链回退的真实样本——FA 的 DropMask 模板：

[flash_attention_score_enhance_tiling_general.cpp:4993-5000](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L4993-L5000) —— 优先级 90 的 `FlashAttentionScoreEnhanceTilingDropMask::DoOpTiling`：若 `needDropMaskOp == 0`（本帧不需要 dropout 预处理），重置参数后直接 `return ge::GRAPH_PARAM_INVALID`，把整帧让给优先级 94 的 VarLen 模板。

[flash_attention_score_enhance_tiling_general.cpp:5002-5028](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5002-L5028) —— 更有意思的是：即使需要 dropmask，这个类算完 `dropmaskParams` 后**仍然返回 `GRAPH_PARAM_INVALID`**——它只负责填「dropout 掩码」这一段参数，其余通用切分交给链上后面的模板完成。

[flash_attention_score_enhance_tiling_general.cpp:584-585](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L584-L585) —— 上面这种「接力」能成立的前提：FA 的 `tilingData` 不是类的成员，而是指向 **context 的 TilingData 缓冲区**的指针。前一个模板写进 context 的 `dropmaskParams`，后一个模板从同一块内存里接着读——责任链共享同一块「黑板」。

[flash_attention_score_enhance_tiling_general.cpp:3384-3412](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L3384-L3412) —— 另一种让位方式：某特化模板的 `IsCapable()` 检查 S2 上限与单 block 数据量，不匹配时打日志并返回 false（基类转成 `GRAPH_PARAM_INVALID`，链继续）。

补充：attention 家族在 [attention/common/op_host/fia_tiling_templates_registry.h:65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h#L65) 还有一份自己的变体注册表 `FiaTilingRegistry`（配套宏 `REGISTER_TILING_TEMPLATE_FIA` 在同文件 [第 185 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h#L185)），思路与本节完全同构，属于家族内部的「抄一份自己维护」。

#### 4.2.4 代码实践

**实践目标**：用 grep 摸清全仓库「谁在用哪套注册表、哪个宏」，建立注册方式的全景感。

**操作步骤**：

1. 在 `ascendc/src/ops-transformer` 下执行：

   ```bash
   grep -rn "REGISTER_OPS_TILING_TEMPLATE\|REGISTER_TILING_TEMPLATE_WITH_SOCVERSION\|REGISTER_TILING_TEMPLATE_NEW\|REGISTER_TILING_TEMPLATE(" --include="*.cpp" | grep -v "define"
   ```

2. 再统计注册表调度入口的两种用法：

   ```bash
   grep -rn "TilingRegistryNew::GetInstance().DoTilingImpl\|TilingRegistry::GetInstance().DoTilingImpl" --include="*.cpp"
   ```

3. 把结果整理成三列表格：算子名 / 注册宏 / 优先级数值，并按优先级排序。

**需要观察的现象**：哪些算子只有一个实现（单宏单优先级，如 sinkhorn 的 2000）？哪些算子有 5~6 个实现排成一条链（FA 前向/反向）？带 socVersion 的注册集中出现在哪些目录（提示：`op_host/arch32`、`op_host/arch35`）？

**预期结果**：你会看到 mhc 家族多用无 soc 的 `TilingRegistry` + `REGISTER_OPS_TILING_TEMPLATE`，attention 家族的 FA/pioneer 多用 `TilingRegistryNew` + `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION`——因为 attention 的实现强依赖芯片代际（arch32 对应 A2 类、arch35 对应 A3 类）。完整输出依赖你的本地仓库，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`TilingRegistryNew` 和 `TilingRegistry` 的本质区别是什么？sinkhorn 用的是哪个？

答案：`TilingRegistryNew` 的 map 多一层 socVersion 键，允许同一算子在不同芯片注册不同的 tiling 类集合；`TilingRegistry` 所有芯片共用一份。sinkhorn 用的是不带 soc 的 `TilingRegistry`（见其 `_tiling_base.cpp` 第 25 行的 `TilingRegistry::GetInstance().DoTilingImpl(context)`）。

**练习 2**：FA 前向注册了优先级 90 和 94 两个类，谁先执行？如果 90 返回 `GRAPH_FAILED`，94 还会执行吗？

答案：先执行 90（`std::map` 按升序遍历，优先级数值小者优先）。若 90 返回 `GRAPH_FAILED`，94 **不会**执行：`DoTilingImpl` 只在 `GRAPH_PARAM_INVALID` 时继续，FAILED 会立即中止整条链并上抛。

**练习 3**：注册宏为什么展开成「全局静态对象」而不是「函数内局部对象」？变量名里的 `VAR_UNUSED##op_type##class_name##priority` 起什么作用？

答案：全局静态对象的构造函数在动态库加载时执行，早于任何 tiling 调用，保证注册表就绪；局部对象永远不会被构造。把算子名、类名、优先级拼进变量名是为了在同一编译单元/不同编译单元多次使用宏时生成不重复的变量名，避免重定义冲突。

### 4.3 tiling_key 编码：十进制位组装与编译产物的关系

#### 4.3.1 概念说明

u2-l3 见过最朴素的 tilingKey：aggregate_hidden 用 0=BF16、1=FP16，sinkhorn 用 0=训练路径、1=推理路径——一条轴、几个值，直接定义常量即可。但当 kernel 的「变化维度」多起来（UB 切哪根轴、分核切哪根轴、数据类型、layout、稀疏模式……），简单常量不够用了。

`tiling_key.h` 给出的编码习惯是**十进制位组装（decimal digit packing）**：把若干个取值小于 10 的枚举当作十进制数的各个「位」拼成一个 uint64，再加一个 \(10^{19}\) 的巨大偏移。好处：

1. 一个整数同时携带多个维度的选择，可读可解码（逐位除 10 取余即可还原）；
2. `10^19` 偏移让它和旧式小整数 key（0/1/2…）一眼区分开；
3. 全部是 `constexpr`，编译期就能算好，kernel 侧 switch 分支的 case 值也是同一个表达式。

与编译产物的关系：**一个算子编出的 kernel `.so` 里包含所有分支/模板实例，tilingKey 并不参与「选择哪份产物」，而是在运行期于 kernel 入口处选择执行哪条分支**（u2-l4 讲过的 `TILING_KEY_IS`）。Host 侧 `SetTilingKey` 写什么，Device 侧就走哪条路——两侧必须用同一套编码，这正是把编码函数放进公共头文件的原因。

#### 4.3.2 核心流程

编码公式（`kBase = 10`）：

\[
\text{RecursiveSum}(a_0, a_1, \ldots, a_n) = a_0 + 10 \cdot a_1 + 10^2 \cdot a_2 + \cdots + 10^n \cdot a_n
\]

\[
\text{tilingKey} = 10^{19} + \text{RecursiveSum}(\text{ub2}, \text{ub1}, \text{block}, \text{dtype}, \text{layout}, \text{sparse})
\]

即**第一个参数是最低位**。解码是逆过程：`key` 去掉偏移后逐位 `÷10 取余`，第 i 位余数对应第 i 个维度。

#### 4.3.3 源码精读

[tiling_key.h:24-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L24-L33) —— `RecursiveSum` 用 C++17 折叠式的可变参数模板递归实现十进制位组装：`templateId + kBase * RecursiveSum(templateIds...)`，递归终止于返回 0 的无参重载。

[tiling_key.h:35-47](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L35-L47) —— 文档注释：FlashAttentionScoreEnhance/GradEnhance 从低位到高位依次是 Ub0、Ub1、Block、DataType、Format、Sparse 六个十进制位；Ub0/Ub1 表示 UB 核内切分的轴（最多切两根，不切填 `AXIS_NONE`）。注释还给出使用示例。

[tiling_key.h:49-53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L49-L53) —— `TILINGKEYOFFSET = 10^19` 与 `GET_TILINGKEY(...)`：在组装结果上加偏移。

[tiling_key.h:58-60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_key.h#L58-L60) —— `TILINGKEY(ub2, ub1, block, dtype, layout, sparse)` 宏：直接传六个枚举名即可得到 key。

位组装所用的枚举定义在 [tiling_type.h:23-99](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_type.h#L23-L99)（`optiling` 命名空间）：`AxisEnum`（B/N2/G/S1/S2/D/NONE=9，供 Ub0/Ub1/Block 三位使用）、`DtypeEnum`、`LayoutEnum`（BSND/SBND/BNSD/TND/NTD_TND）、`SparseEnum`（ALL/NONE/ANY/CAUSAL/BAND/PREFIX 等 10 种）等。**每个枚举值都必须小于 10**——这是十进制位组装的硬约束，也是 `AxisEnum::NONE` 取 9 而不是 -1 的原因。

[tiling_type.h:101-138](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_type.h#L101-L138) —— 同一套 `RecursiveSum`/`GET_TILINGKEY`/`TILINGKEY` 在 `optiling` 命名空间下的副本。它与 `tiling_key.h`（`Ops::Transformer::OpTiling` 命名空间）内容重复，各自服务不同的 include 习惯，阅读时注意别混用命名空间。

两个对照样本：

[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:49-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L49-L50) 与 [第 556-561 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L556-L561) —— 朴素风格：`TILING_KEY_GENERALIZED = 0`（训练/Transpose 模板）、`TILING_KEY_INFER = 1`（推理/DataCopyPad 模板），`GetTilingKey()` 按 `out_flag` 二选一。只有一条变化轴时没必要位组装。

[flash_attention_score_enhance_tiling_general.cpp:5047-5050](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L5047-L5050) —— FA 实际用的 `GET_TPL_TILING_KEY(0,0,...,0)`（20 个参数）来自 CANN 高阶 API 的模板选择框架，本仓库只使用、未见其定义（定义在 CANN 安装包的头文件中，**待确认**具体位置）；它与本节的 `GET_TILINGKEY` 是同一思想的更长位数版本。

#### 4.3.4 代码实践

**实践目标**：手工算一遍十进制位组装，确认你真的理解「第一个参数是最低位」。

**操作步骤**：

1. 查 [tiling_type.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_type.h#L23-L99) 中的枚举值：`AxisEnum::S2=4`、`AxisEnum::S1=3`、`AxisEnum::N2=1`、`DtypeEnum::FLOAT32=1`、`LayoutEnum::BSND=0`、`SparseEnum::ALL=0`。
2. 手算 `TILINGKEY(S2, S1, N2, FLOAT32, BSND, ALL)`（不带偏移的部分）。
3. 写一段最小 C++ 验证（**示例代码**，非仓库原有）：

   ```cpp
   // compile: g++ -std=c++17 -c demo.cpp
   #include <cstdint>
   #include <cstdio>
   constexpr uint64_t kBase = 10;
   constexpr uint64_t RecursiveSum() { return 0; }
   template <typename T, typename... Args>
   constexpr uint64_t RecursiveSum(T t, Args... rest) {
       return static_cast<uint64_t>(t) + kBase * RecursiveSum(rest...);
   }
   int main() {
       // S2=4, S1=3, N2=1, FLOAT32=1, BSND=0, ALL=0
       printf("%llu\n", RecursiveSum(4, 3, 1, 1, 0, 0));  // 期望 1134
       return 0;
   }
   ```

**需要观察的现象**：输出应为 `1134`，即 \(4 + 10\times3 + 100\times1 + 1000\times1\)。把参数顺序对调（如 `(0,0,BSND...)` 换成高位在前）结果会完全不同，体会「参数顺序 = 位权」。

**预期结果**：加上偏移后完整 key 为 \(10^{19} + 1134 = 10000000000000001134\)。手算与程序输出一致即通过；运行结果**待本地验证**（本环境只保证公式推导）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GET_TILINGKEY` 要加 \(10^{19}\) 偏移？

答案：为了与旧式小整数 tilingKey（0/1/2 这类）在数值空间上彻底隔开——见到 19 位以上的 key 就知道是位组装编码；同时避免不同编码体系意外撞值。`uint64_t` 最大约 \(1.8\times10^{19}\)，\(10^{19}\) 偏移恰好仍在表示范围内但已接近上限，所以最多容纳 19 个十进制位。

**练习 2**：`AxisEnum` 的枚举值为什么都小于 10？`NONE` 为什么是 9？

答案：每个维度只占一个十进制位，值域必须是 0~9，否则会「进位」污染相邻位。`NONE=9` 表示「该轴不参与切分」，用位段内的最大安全值（9）做哨兵，既满足小于 10 的约束又不易与真实轴编号混淆。

**练习 3**：tilingKey 改变时需要重新编译算子包吗？

答案：不需要重新编译产物的「份数」——所有分支都编在同一个 kernel `.so` 里；tilingKey 是运行期信号，Host 写、Device 入口读。但**新增一个 key 取值**意味着 kernel 侧要新增对应分支/模板并重新编译，同时 Host 侧 `GetTilingKey()` 能产出这个新值，两侧必须同步（回顾 u2-l4 的「跨侧契约」）。

### 4.4 tiling_util 与 data_copy_transpose_tiling：小工具的真实现状

#### 4.4.1 概念说明

`common/src/tiling_base/tiling_util.cpp` 和 `common/include/tiling_base/data_copy_transpose_tiling.h` 是 tiling_base 目录下的两个公共小工具。本模块除了讲它们做了什么，更重要的教训是：**公共目录里的代码不等于都被使用**——学会用 grep 验证一个工具的真实调用情况，是读公共库的必备素养。

#### 4.4.2 核心流程

- `IsRegbaseSocVersion(context)`：从 `TilingContext`/`TilingParseContext` 取平台信息 → 构造 `PlatformAscendC` → 取 `SocVersion` → 判断是否 regbase（新架构）芯片。**当前实现恒返回 false**（预留钩子）。
- `EnsureNotScalar(shape)`：若 shape 是标量（0 维），返回固定的 `{1}` 形状，否则原样返回——避免对标量张量调 `GetDim(0)` 越界。
- `GetDataCopyTransposeTiling(dstShape, srcShape, typeSize, tiling)`：按 BNSD 四维语义把源/目标 shape 预展开成一组乘积字段（如 `shapeSHValue = S*H`、`shapeBHValue = B*H`），供设备侧 DataCopy 转置时直接查表用，避免核内重复乘法。

#### 4.4.3 源码精读

[tiling_util.h:24-28](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_util.h#L24-L28) —— 三个工具函数的声明：两个 `IsRegbaseSocVersion` 重载 + `EnsureNotScalar`。

[tiling_util.cpp:24-27](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L24-L27) —— 核心判断 `IsRegbaseSocVersion(SocVersion)` 当前**固定 `return false`**：为未来的 regbase 架构（A3 类新编程范式）预留的开关，当前所有 soc 都走非 regbase 路径。

[tiling_util.cpp:29-41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L29-L41) —— 两个重载分别从 `TilingParseContext` 与 `TilingContext` 取平台信息再委托上面的静态函数。

[tiling_util.cpp:43-49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_base/tiling_util.cpp#L43-L49) —— `EnsureNotScalar`：三行实现，标量换成静态的 `{1}` shape（返回静态对象的引用避免悬空）。

[data_copy_transpose_tiling.h:25-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/data_copy_transpose_tiling.h#L25-L50) —— 自由函数 `GetDataCopyTransposeTiling`：把 B/N/S/H 与若干预乘积写进 `optiling::CopyTransposeTiling`（结构体定义在同目录 `data_copy_transpose_tiling_def.h`）。注意它在 `optiling` 命名空间，且与本框架的 `TilingBase` 无继承关系，只是放在同一目录的独立工具。

**诚实的现状核查**（用 grep 验证过）：

- `EnsureNotScalar` 与这个自由函数版 `GetDataCopyTransposeTiling` 在仓库内**目前均无调用者**（只有声明/定义处命中）。
- FA 里 [flash_attention_score_enhance_tiling_general.cpp:2367](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/arch32/flash_attention_score_enhance_tiling_general.cpp#L2367) 调用的 `transposeTilingData.GetDataCopyTransposeTiling(...)` 是**成员函数**（参数是 4 个 int64），与公共头里的自由函数只是同名，不是同一实体。

结论：这两个文件当前属于「预留/历史」性质，复用前先确认调用关系，别被名字误导。

#### 4.4.4 代码实践

**实践目标**：亲手验证「公共工具是否被使用」，养成不轻信目录名直觉的习惯。

**操作步骤**：

```bash
cd ascendc/src
grep -rn "EnsureNotScalar" --include="*.cpp" --include="*.h" .
grep -rn "GetDataCopyTransposeTiling" --include="*.cpp" --include="*.h" .
grep -rn "IsRegbaseSocVersion" --include="*.cpp" --include="*.h" . | grep -v "tiling_util"
```

**需要观察的现象**：第一条命令只有定义与声明两处命中；第二条命令除定义外还有 FA 的成员函数调用（同名不同物）；第三条观察 `IsRegbaseSocVersion` 是否有业务调用方。

**预期结果**：与 4.4.3 的「现状核查」一致——两个工具在仓库内无真实调用者（`IsRegbaseSocVersion` 的调用情况以你的 grep 输出为准，**待本地验证**）。如果未来某次提交开始调用它们，说明对应机制（regbase 切换、标量兜底、转置预计算）被启用了。

#### 4.4.5 小练习与答案

**练习 1**：`IsRegbaseSocVersion` 现在恒返回 false，那写它有什么意义？

答案：这是典型的「预留扩展点」：调用方代码可以先写成 `if (IsRegbaseSocVersion(context)) { 新路径 } else { 旧路径 }`，等 regbase 架构落地时只需改这一个函数的实现，所有调用点自动切换，避免到时候大面积改代码。

**练习 2**：`EnsureNotScalar` 为什么返回静态对象的引用而不是按值返回？

答案：函数签名返回 `const gert::Shape&`（引用），若返回局部对象会产生悬空引用；对「标量 → `{1}`」这个固定映射使用 `static const` 对象（`tiling_util.cpp` 第 22 行的 `g_vec_1_shape`）既安全又零开销。

**练习 3**：公共头里的自由函数 `GetDataCopyTransposeTiling` 和 FA 里的同名调用是什么关系？

答案：没有关系。前者是 `optiling` 命名空间下接收 `ge::Shape` 的 inline 自由函数（仓库内暂无调用者）；后者是 FA 的 `transposeTilingData` 成员函数（接收 4 个 int64），只是恰好同名。阅读时必须看签名与所属作用域，不能只看函数名。

## 5. 综合实践

**实践目标**：把本讲三个机制（两级 tiling 文件组织、责任链调度、PARAM_INVALID 回退）串成一张可复述的执行链图，并厘清 `flash_attention_score_enhance_tiling_common.h` 与框架的真实关系。

### 任务一：画出 sinkhorn 一帧请求的 tiling 类执行链

先读两个文件，再照下面的骨架补全（含回退路径）：

- 接入层：[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp:23-36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling_base.cpp#L23-L36) —— `TilingForSinkhorn` 一行委托给 `TilingRegistry::GetInstance().DoTilingImpl(context)`，随后 `IMPL_OP_OPTILING(...).Tiling(TilingForSinkhorn).TilingParse<SinkhornCompileInfo>(...)` 把入口挂到 CANN 框架（回顾 u2-l3 的注册方式）。
- 实现层：[manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp:79-141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L79-L141) —— `SinkhornTilingBase` 类定义；[第 580 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_tiling.cpp#L580) 注册优先级 2000。

执行链参考图（补全方框内容）：

```text
CANN 框架按算子名调用 TilingForSinkhorn(context)          [_tiling_base.cpp:23]
  └─ TilingRegistry::GetInstance().DoTilingImpl(context)   [tiling_templates_registry.h:245]
      ├─ opType = "ManifoldConstrainedHyperConnectionSinkhornEnhance"
      ├─ GetTilingTemplates(opType) → map{ 2000 → SinkhornTilingBase 工厂 }
      └─ 遍历（升序）：
          [2000] new SinkhornTilingBase(context) → DoTiling()
            ├─ GetShapeAttrsInfo   取 x shape/dtype + 3 个属性 + 输出 shape
            ├─ GetPlatformInfo     AIV 核数、UB 大小 → aicoreParams_
            ├─ IsCapable           恒 true
            ├─ DoOpTiling          形状校验 + SplitCores + reduceMask 预计算
            ├─ DoLibApiTiling      空实现（返回 SUCCESS）
            ├─ GetWorkspaceSize    固定 16MB
            ├─ PostTiling          SetBlockDim + TilingData SaveToBuffer
            └─ SetTilingKey(out_flag==0 ? 1 : 0) → GRAPH_SUCCESS，链终止
          ── 回退路径（本算子当前不会触发，但框架支持）──
          若上一步返回 GRAPH_PARAM_INVALID：
            继续尝试 map 中下一个更大的 priority 数值；
          全部让位 → "no valid template is found" → GRAPH_FAILED
```

**观察与验证**：sinkhorn 只注册了一个类且 `IsCapable` 恒 true，所以回退路径在本算子是「潜在路径」。要观察真实回退，去看 FA：入口 [flash_attention_score_enhance_tiling.cpp:287-306](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L287-L306) 在空输入早退后调用 `TilingRegistryNew::DoTilingImpl`（第 304 行），链首 DropMask(90) 算完 dropmask 参数后返回 `GRAPH_PARAM_INVALID`（4.2.3 已精读），接力给 VarLen(94)→SameAB(95)→S1s2Bn2gs1(96)→S1Bn2gs1(97)→B(98)。把这条真实链也画进你的图里作为对照。

**预期结果**：两张链图（sinkhorn 单实现链 + FA 多实现接力链），并能口头复述「PARAM_INVALID 是链的接力棒，SUCCESS/FAILED 是链的终止符」。

### 任务二：说明 flash_attention_score_enhance_tiling_common.h 与框架的对应关系

打开 [flash_attention_score_enhance_tiling_common.h:24-32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling_common.h#L24-L32)，回答：它属于 tiling_base 框架吗？

参考答案要点：

1. **它不属于框架本体**。该头文件只定义了 `FlashAttentionScoreEnhanceCompileInfo` 结构体（aivNum/aicNum/ubSize/l1Size/l0cSize/l2CacheSize/socVersion），是 `TilingParse` 阶段的「编译期信息契约」，与 `tiling_base.h` 里的 `CompileInfoCommon`（[L45-57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h#L45-L57)）角色相同但字段略异——FA 用自己的结构体。
2. **它与框架的真正连接点有两个**：(a) [flash_attention_score_enhance_tiling.cpp:308-325](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L308-L325) 的 `TilingPrepareForFlashAttentionScoreEnhance` 在图编译阶段把平台探测结果写进该结构体；(b) [第 304 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_host/flash_attention_score_enhance_tiling.cpp#L304) 的 `TilingRegistryNew::GetInstance().DoTilingImpl(context)` 才是责任链框架的接入调用——当真实环境下 `GetPlatformInfo()` 非空时走平台信息解析 soc；UT faker 环境下为空时，框架会改从 `CompileInfoCommon` 形态的编译信息里取 socVersion（`TilingRegistryNew::DoTilingImpl` 第 102-107 行的分支）。
3. **结论**：文件名带 `tiling_common` 容易让人以为它是「tiling 公共框架的一部分」，实际它只是 FA 的编译期信息结构；框架本体在 `common/include/tiling_base/`。这也是本讲反复强调的方法论：以代码内容与调用关系定位职责，而不是以文件名臆断。

### 可选上机验证（有 NPU/UT 环境时）

运行 u8 单元将详述的 UT 流程：`bash build.sh -u -n manifold_constrained_hyper_connection_sinkhorn_enhance -c ascend910_93 --ophost`（命令形态参考 u1-l4，具体参数以 build.sh 帮助为准），在 tiling UT 的日志中寻找 `"Do general op tiling success priority=..."` 或 `"Ignore general op tiling priority=..."` 字样——它们正是 `DoTilingImpl` 循环里 [tiling_templates_registry.h:123-126](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_base/tiling_templates_registry.h#L123-L126) 打出的责任链轨迹。**待本地验证**。

## 6. 本讲小结

- `TilingBase` 用模板方法把一次 tiling 固定为七步（GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling → SetTilingKey），子类只填内容不管流程；实际调用顺序以 `DoTiling()` 为准，与注释编号不同。
- 三态返回值是责任链的调度语言：`GRAPH_SUCCESS` 成功终止、`GRAPH_FAILED` 立即中止、`GRAPH_PARAM_INVALID` 让位给下一个优先级实现；`IsCapable()` 返回 false 是产生第三态的标准方式，任意步骤直接返回它也合法。
- `tiling_templates_registry.h` 提供「工厂函数 + 优先级 map + 静态注册宏」三件套，分带 socVersion（`TilingRegistryNew`）与不带（`TilingRegistry`）两套；`std::map` 升序遍历决定「优先级数值越小越先执行」；注册发生在 so 加载期的全局静态对象构造中。
- FA 演示了责任链的高级用法：优先级 90 的 DropMask 模板只填共享 context TilingData 中的 dropmask 段就返回 `GRAPH_PARAM_INVALID`，把接力棒交给 94~98 的通用模板——多个 tiling 类合作完成一帧。
- tilingKey 的编码习惯是十进制位组装（`RecursiveSum`，第一个参数最低位）加 \(10^{19}\) 偏移，每位对应一个枚举（轴/ dtype/layout/稀疏），枚举值必须小于 10；简单算子（sinkhorn）仍可直接用 0/1 常量。tilingKey 不决定编译产物份数，而是运行期在同一个 kernel `.so` 内选分支的信号。
- `tiling_util.cpp` 与 `data_copy_transpose_tiling.h` 是小工具且当前在仓库内基本无调用者（`IsRegbaseSocVersion` 恒 false 属预留钩子）；读公共库要用 grep 核实真实调用关系，警惕同名不同物（FA 的成员函数 `GetDataCopyTransposeTiling`）。

## 7. 下一步学习建议

- **下一讲 u3-l4（stub 桩机制）**：本讲留下了一个伏笔——`DoTilingImpl` 在 `GetPlatformInfo()` 为空时如何从 `CompileInfoCommon` 取 socVersion？这正是 UT faker 上下文的行为，下一讲讲 `common/stub` 的 op_tiling/op_api 桩时会把这条链补完整。
- **按家族纵向深入**：想看责任链的「满配」现场，直接读 u4-l2/u4-l3（FA 前向 tiling 与 kernel），对照本讲的 90/94/95/96/97/98 六级链理解「特化模板排队、通用模板兜底」的设计；arch35 的 AttentionPioneer（u4-l8）则是带 socVersion 注册表的另一个样本。
- **继续阅读的源码**：`attention/common/op_host/fia_tiling_templates_registry.h`（家族自制注册表变体）；`flash_attention_score_grad_enhance` 的 `op_host/arch32/` 下多个 `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` 调用点，观察反向算子如何复用同一套框架。
- **动手建议**：在 u9-l4 综合实战里新建算子时，试着不用框架（像 aggregate_hidden 那样一个函数写完）与用框架（继承 `TilingBase` + 注册宏）各写一遍 tiling，体会在什么规模下框架开始「回本」。
