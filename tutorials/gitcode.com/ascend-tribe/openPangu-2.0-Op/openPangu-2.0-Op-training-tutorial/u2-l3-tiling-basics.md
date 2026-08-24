# op_host 之 Tiling 入门：TilingContext 与切分策略

## 1. 本讲目标

上一讲（u2-l2）我们读完了 `_def.cpp`：它只声明算子"长什么样"（输入输出、类型、芯片白名单），不涉及任何 shape 数值。本讲进入 op_host 的第二个文件——Tiling（切分），读完本讲你应当能够：

1. 解释 Tiling 存在的意义：在 Host 侧把大 Tensor 切成小块、选定 tilingKey、规划 workspace 与 blockDim。
2. 读懂 `AHInfoParser` 的完整工作流：从 `TilingContext` 取平台信息（AIV/AIC 核数、socVersion）、做五步输入合法性校验、按 S/B/H 三级策略计算切分。
3. 用 `OP_CHECK_IF` 与 `OP_LOGE` 编写防御式参数校验。
4. 说清 Tiling 函数的三个输出契约：`SetTilingKey`、`SetBlockDim`、`SetRawTilingData`（以及 workspace 写回），并理解它们如何被设备侧 Kernel 消费。

本讲仍以 mome 家族的 `ai_infra_aggregate_hidden` 为解剖标本。

## 2. 前置知识

### 2.1 为什么需要 Tiling

昇腾 NPU 上有两层存储：

- **GM（Global Memory）**：容量大（数十 GB），但访问慢，输入输出张量都放在这里。
- **UB（Unified Buffer）**：每个 AI 核私有的片上缓存，容量只有约 1~2 百 KB，但访问极快。Ascend C 的计算指令只能直接吃 UB 里的数据。

一个 `[S=32K, B=8, H=24576]` 的输入有数十亿元素，显然塞不进 UB；同时一颗芯片有几十个 AI 核，不把数据切开就没有并行度。**Tiling（切分）就是在 Kernel 启动前，由 Host 侧代码回答三个问题**：

1. 数据怎么切：每个核处理哪一小块（tile）？
2. 用多少核：`blockDim`（启动的核数）是多少？
3. 走哪条编译分支：`tilingKey` 是多少（它是 Host 写、Device 读的"跨侧信号"，见 u1-l2）？

这三个答案被序列化成一坨字节，随 Kernel 一起下发到设备侧，Kernel 再反序列化出来用。这就是 Tiling 的全部职责——它**不搬运数据、不做计算**，只做"作战规划"。

### 2.2 TilingContext：Host 与框架的接口

`gert::TilingContext` 是 CANN 提供的上下文对象，Tiling 函数通过它读取一切输入信息（节点名、平台信息、每个输入的 desc/shape、可选输入张量、RawTilingData 缓冲区），并通过它写回一切输出（blockDim、workspace、tilingData、tilingKey）。可以把 Tiling 函数理解成一个纯函数：

\[ f(\text{TilingContext 的输入侧}) \rightarrow \{\text{blockDim},\ \text{tilingKey},\ \text{TilingData},\ \text{workspace}\} \]

### 2.3 两个基础概念

- **AIV 与 AIC**：昇腾芯片上有两类核。AIC（AI Cube）擅长矩阵乘（Cube 指令），AIV（AI Vector）擅长逐元素向量运算。`ai_infra_aggregate_hidden` 是纯向量卷积类算子，Kernel 入口标注了 `KERNEL_TYPE_AIV_ONLY`（见 [ai_infra_aggregate_hidden.cpp:27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L27)），所以 Tiling 只按 AIV 核数切分。
- **workspace**：GM 上的一块额外工作空间，供 Kernel 存放中间结果。Tiling 负责声明它的大小。

### 2.4 上取整公式

切分代码里反复出现"除不尽就多算一份"的上取整写法，\( a \) 切成每份 \( b \) 个，需要的份数为：

\[ \text{cnt} = \left\lceil \frac{a}{b} \right\rceil = \frac{a + b - 1}{b} \quad (\text{整数除法}) \]

后面读 `CoreSplit` 时会大量用到。

## 3. 本讲源码地图

| 文件 | 关键内容 | 本讲角色 |
| --- | --- | --- |
| [op_host/ai_infra_aggregate_hidden_tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L1-L159) | TilingData 定义、入参结构体、`AHInfoParser`/`AiInfraAggregateHiddenTiling` 类声明 | 主讲文件 1 |
| [op_host/ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L1-L483) | 校验、切分算法、输出契约、注册 | 主讲文件 2 |
| [op_kernel/ai_infra_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L24-L42) | Kernel 入口按 tilingKey 分支 | 验证 tilingKey 契约 |
| [op_kernel/ai_infra_aggregate_hidden_common.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L56-L59) | Kernel 用 `GetBlockIdx()` 消费 TilingData | 验证 TilingData 契约 |
| [src/utils/inc/log/log.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/utils/inc/log/log.h#L21-L34) | `OP_LOGE`/`OP_LOGI`/`OP_CHECK_IF` 宏 | 校验风格依据 |
| [tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L33-L86) | Tiling 单元测试 | 实践验证工具 |

（下文相对路径均省略前缀 `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/`。）

## 4. 核心概念与源码讲解

### 4.1 tiling.h 骨架：TilingData 定义与跨侧数据契约

#### 4.1.1 概念说明

Tiling 计算出的切分参数（每块多大、切几份、尾块多大）必须传到设备侧 Kernel 手里。传递载体是一段**序列化字节流（RawTilingData）**，其结构由 `BEGIN_TILING_DATA_DEF` / `TILING_DATA_FIELD_DEF` 宏声明的结构体决定。这是 op_host 与 op_kernel 之间最重要的数据契约：**Host 按 struct 声明顺序写入，Device 按同一结构读出**，任何一侧改字段顺序或类型，另一侧必须同步（设备侧结构体由构建期生成的 `kernel_tiling/kernel_tiling.h` 提供，仓库中不存在该文件，Kernel 入口通过 [ai_infra_aggregate_hidden.cpp:16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L16) 引用它）。

#### 4.1.2 核心流程

tiling.h 的内容分五块，自上而下：

```
① tilingKey 宏（0=BF16, 1=FP16）          —— 与 kernel 侧同名宏保持一致
② 输入描述辅助结构体                        —— TilingRequiredParaInfo / TilingOptionalParaInfo
③ TilingData 结构体 + 注册宏               —— 跨侧数据契约
④ CompileInfo 空结构体                     —— 本算子无编译期缓存信息
⑤ 三个类                                   —— AhParaInfo(原始参数) / AHTilingInfo(汇总结果)
                                              / AHInfoParser(解析校验) / AiInfraAggregateHiddenTiling(写出)
```

其中宏 `BEGIN_TILING_DATA_DEF` 会为结构体自动生成 `set_xxx()` 设置器和 `SaveToBuffer()`/`GetDataSize()` 序列化方法——这就是为什么 tiling.h 里看不到任何手写成员函数，而 tiling.cpp 的 `DoTiling` 里却能调用 `tilingData_.set_ifMask(...)`。

#### 4.1.3 源码精读

**① tilingKey 枚举宏**（[ai_infra_aggregate_hidden_tiling.h:19-20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L19-L20)）：定义 `AGGREGATE_HIDDEN_BF16=0`、`AGGREGATE_HIDDEN_HALF=1`。kernel 侧在 [ai_infra_aggregate_hidden.cpp:21-22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L21-L22) **重复定义了同名同值宏**——两侧靠数值约定对齐，这是阅读时容易踩的坑：改一侧不改另一侧，编译不报错、运行走错分支。

**② TilingData 结构体与注册**（[ai_infra_aggregate_hidden_tiling.h:43-59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L43-L59)）：

```cpp
BEGIN_TILING_DATA_DEF(AiInfraAggregateHiddenTilingData)
TILING_DATA_FIELD_DEF(uint8_t, ifMask);
TILING_DATA_FIELD_DEF(int64_t, hSize);
TILING_DATA_FIELD_DEF(int64_t, bSize);
TILING_DATA_FIELD_DEF(int64_t, sSize);
TILING_DATA_FIELD_DEF(int64_t, baseH);   // 每块 H 的大小
// ... baseB/baseS/三个 Tail/三个 Cnt 共 13 个字段
END_TILING_DATA_DEF;

REGISTER_TILING_DATA_CLASS(AiInfraAggregateHidden, AiInfraAggregateHiddenTilingData)
```

13 个字段分三组：`ifMask`（是否传入 mask）、原始规模 `hSize/bSize/sSize`、切分参数 `baseX`（每份大小）/`baseXTail`（最后一份大小）/`baseXCnt`（份数）。`REGISTER_TILING_DATA_CLASS` 把算子名与结构体绑定，工具链据此生成设备侧同名结构体。

**③ Kernel 侧如何消费**（[ai_infra_aggregate_hidden_common.h:56-59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L56-L59)）：

```cpp
this->baseHIdx = GetBlockIdx() % this->tilingData_->baseHCnt;
this->baseBIdx = (GetBlockIdx() / this->tilingData_->baseHCnt) % this->tilingData_->baseBCnt;
this->baseSIdx =
    (GetBlockIdx() / (this->tilingData_->baseHCnt * this->tilingData_->baseBCnt)) % this->tilingData_->baseSCnt;
```

每个核用自己的 `GetBlockIdx()`（即 blockDim 中的编号）对 `Cnt` 做除法取余，反推出自己负责的 `(H 块号, B 块号, S 块号)` 坐标——Host 写、Device 读的契约在这里闭环。

**④ 两个信息聚合体**：[AhParaInfo（L65-70）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L65-L70) 保存从 context 取到的四个张量的 desc/shape/tensor 指针；[AHTilingInfo（L73-95）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L73-L95) 是"解析+校验+切分"全部完成后的结果汇总，供 `DoTiling` 写出。两者分离使"计算"与"写回"解耦。

#### 4.1.4 代码实践

**实践目标**：建立"TilingData 字段 → Kernel 消费点"的对照表，验证跨侧契约。

**操作步骤**：

1. 打开 [ai_infra_aggregate_hidden_tiling.h:43-57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L43-L57)，抄下 13 个字段。
2. 在 `op_kernel/` 目录下用 `grep -rn "tilingData_->" .` 找出每个字段在设备侧的使用位置。
3. 按"字段名 → kernel 中被谁使用 → 用途"三列填表。

**需要观察的现象**：`hSize/bSize/sSize` 在 kernel 里主要用于地址偏移计算（定位本核 tile 在 GM 中的起点），`baseXxx/baseXxxCnt` 用于循环边界与核间分工，`ifMask` 决定是否走置零逻辑。

**预期结果**：13 个字段中大多数都能在 `ai_infra_aggregate_hidden_common.h` 中找到消费点（本讲已验证 `baseHCnt/baseBCnt/baseSCnt` 三处，L56-59）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TILING_DATA_FIELD_DEF(int64_t, hSize)` 的类型改成 `int32_t`，只改 host 侧会发生什么？

**答案**：host 侧编译通过（序列化按新类型写入 4 字节），但设备侧结构体（构建期生成）仍是 `int64_t`，会按 8 字节读——字段错位、数值错乱。契约要求两侧同步修改并重新编译整个算子包。

**练习 2**：`AiInfraAggregateHiddenCompileInfo` 是空结构体（[tiling.h:62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L62)），它有什么用？

**答案**：它是 `TilingParse<CompileInfo>` 的模板参数（见 4.4 节注册代码），用于图编译期缓存算子的编译信息（比如某些算子会把 attr 存进去，下次 Tiling 直接取）。本算子没有需要缓存的信息，所以留空，但注册语法要求必须给一个类型。

### 4.2 AHInfoParser：平台信息获取与五步合法性校验

#### 4.2.1 概念说明

Tiling 计算切分之前必须先回答：跑在什么芯片上（核数多少）？输入合法吗？`AHInfoParser` 就是这个"先问清楚再算"的阶段。它的校验规则正是 README 规格的代码化——u2-l1 里我们读过的约束（B∈[1,8]、S≤32K、H∈[384,24576]、W=3、类型一致）在这里逐条落地，这是"文档 ↔ 代码互证"的最佳样本。

防御式编程统一使用 `OP_CHECK_IF` 宏（[log.h:28-34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/utils/inc/log/log.h#L28-L34)）：

```cpp
#define OP_CHECK_IF(condition, log, return_expr) \
    do {                                         \
        if (condition) {                         \
            log;                                 \
            return_expr;                         \
        }                                        \
    } while (0)
```

一行完成"条件不满足 → 打错误日志 → 提前返回失败"，`OP_LOGE`（[log.h:25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/utils/inc/log/log.h#L25)）会以错误码 `EZ9999` 上报内部错误，框架据此终止本次执行并把日志透出给用户。

#### 4.2.2 核心流程

入口 `ParseAndCheck`（[ai_infra_aggregate_hidden_tiling.cpp:398-417](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L398-L417)）串起十步，任一步失败立即返回 `GRAPH_FAILED`：

```
ParseAndCheck
 ├─ 1. GetOpName        取节点名（后续日志用它标识算子）
 ├─ 2. GetNpuInfo       平台信息：AIV/AIC 核数、socVersion 白名单、workspace/RawTilingData 可用性
 ├─ 3. CheckInputValid  input  [S,B,H]：非空 / dtype∈{FP16,BF16} / 三维 / S≤32K / B≤8 / H∈[384,24576]
 ├─ 4. CheckWeightValid weight [W,H]：非空 / dtype 与 input 一致 / 二维 / W==3 / H 与 input 一致
 ├─ 5. CheckMaskValid   mask   [B,S]（可选）：存在时须为 BOOL / 二维 / B、S 与 input 一致
 ├─ 6. CheckOutputValid output [S,B,H]：非空 / dtype 与 input 一致 / 三维 / S、B、H 逐维等于 input
 ├─ 7. GetTilingKey     按 input dtype 定 tilingKey（BF16→0 / FP16→1）
 ├─ 8. CoreSplit        切分计算（下一模块精读）
 ├─ 9. GenerateInfo     把全部结果搬进 AHTilingInfo
 └─ 10. DumpTilingInfo  OP_LOGD 打印切分结果（调试用，默认级别不输出）
```

#### 4.2.3 源码精读

**① GetNpuInfo：平台探测**（[ai_infra_aggregate_hidden_tiling.cpp:79-102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L79-L102)）：

```cpp
platformInfo_ = context_->GetPlatformInfo();
OP_CHECK_IF(platformInfo_ == nullptr, OP_LOGE(opName_, "GetPlatformInfo is nullptr."), return ge::GRAPH_FAILED);

auto ascendcPlatform = platform_ascendc::PlatformAscendC(platformInfo_);
aivNum_ = static_cast<int64_t>(ascendcPlatform.GetCoreNumAiv());
uint32_t aicNum = ascendcPlatform.GetCoreNumAic();
OP_CHECK_IF(aicNum == 0 || aivNum_ == 0, OP_LOGE(opName_, "num of core obtained is 0."), return GRAPH_FAILED);
socVersion_ = ascendcPlatform.GetSocVersion();
if ((socVersion_ != platform_ascendc::SocVersion::ASCEND910B) &&
    (socVersion_ != platform_ascendc::SocVersion::ASCEND910_93)) {
    OP_LOGE(opName_, "SOC Version[%d] is not support.", (int32_t)socVersion_);
    return GRAPH_FAILED;
}
```

这段做了四件事：从 context 取平台信息；用 `PlatformAscendC` 包装后查询 AIV/AIC 核数（`aivNum_` 是后面切分的分母）；运行期校验 socVersion 白名单——注意这与 `_def.cpp` 的 `AICore().AddConfig` 是**双层适配**：AddConfig 决定编译期给哪些芯片生成产物，这里是运行期再拦一道。最后预检 `GetWorkspaceSizes(1)` 与 `GetRawTilingData()` 缓冲区非空，避免 `DoTiling` 阶段才发现写不进去。

**② CheckInputValid：五步校验范式**（[ai_infra_aggregate_hidden_tiling.cpp:104-152](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L104-L152)）。以 input 为例，五个 Check 函数共享同一套"取参数 → 查存在 → 查类型 → 查维数 → 查数值"模板。数值检查部分（[L129-149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L129-L149)）把 shape 三个维度存进成员变量并对照常量：

```cpp
sSize_ = ...GetDim(DIM_IDX_ZERO);
OP_CHECK_IF(sSize_ > S_SIZE_LIMIT, OP_LOGE(opName_, "the max of S size only support 32K, but now is %d.", sSize_),
    return ge::GRAPH_FAILED);
bSize_ = ...GetDim(DIM_IDX_ONE);
OP_CHECK_IF(bSize_ > B_SIZE_LIMIT, OP_LOGE(opName_, "the max of batch size only support 8, but now is %d.", bSize_),
    return ge::GRAPH_FAILED);
hSize_ = ...GetDim(DIM_IDX_TWO);
OP_CHECK_IF(hSize_ > H_SIZE_UP_LIMIT, ...);   // 上限 24576
OP_CHECK_IF(hSize_ < H_SIZE_DOWN_LIMIT, ...); // 下限 384
```

约束常量集中定义在文件头部（[L58-63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L58-L63)）：`S_SIZE_LIMIT=32*1024`、`B_SIZE_LIMIT=8`、`H_SIZE_UP_LIMIT=24576`（即 192×128）、`H_SIZE_DOWN_LIMIT=384`（即 192×2）、`W_SIZE_LIMIT=3`、`H_SIZE_FULL=4096`（UB 全载时 H 的最大值，下一模块的主角）。

**③ 可选输入的处理**：`CheckMaskValid`（[L195-230](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L195-L230)）用 `GetOptionalInputTensor(MASK_INDEX)` 取可选输入，指针为空说明用户没传，`ifMask_` 置 0 跳过全部校验；非空则置 1 并校验 BOOL 类型、二维、`[B,S]` 与 input 对应维一致。`ifMask` 随后进入 TilingData，Kernel 据此决定是否读 mask 并做置零。

**④ 一个值得注意的写法**：`ParseAndCheck` 里（[L404-405](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L404-L405)）：

```cpp
if (ge::GRAPH_SUCCESS != CheckInputValid() || ge::GRAPH_SUCCESS != CheckWeightValid() ||
    ge::GRAPH_SUCCESS != CheckMaskValid() || CheckOutputValid()) {
```

前三项用 `GRAPH_SUCCESS !=` 比较，第四项 `CheckOutputValid()` 直接当布尔用。行为上仍正确——`GRAPH_SUCCESS` 的值为 0，失败值非 0，"非 0 即真"恰好触发 `return GRAPH_FAILED`——但风格不一致，是很好的"阅读时保持怀疑"训练点（见练习 3）。

#### 4.2.4 代码实践

**实践目标**：掌握 `OP_CHECK_IF` 校验分类法，并学会用 UT 触发一条校验分支。

**操作步骤**：

1. 统计 [ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L1-L483) 中 `OP_CHECK_IF` 与 `OP_LOGE` 的出现次数，按四类归档：空指针检查、类型检查、维数/shape 检查、平台/环境检查。
2. 打开 [test_ai_infra_aggregate_hidden_tiling.cpp:33-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L33-L58)，复制用例 1，把 `B` 改成 9、期望结果改成 `ge::GRAPH_FAILED`，得到一个"非法输入"用例（示例代码）：

```cpp
// 示例代码：非法 B 触发 B_SIZE_LIMIT 校验（期望失败）
TEST_F(AiInfraAggregateHiddenTiling, ai_infra_aggregate_hidden_bf16_b_over_limit_fail)
{
    int64_t S = 4096;
    int64_t B = 9;  // 超过 B_SIZE_LIMIT=8
    // ...构造同用例 1，期望 ge::GRAPH_FAILED
}
```

3. 用 `bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost` 编译并运行 UT（详见 u8 单元；无 NPU 环境时至少保证编译通过）。

**需要观察的现象**：非法用例返回 `GRAPH_FAILED`，运行日志中出现 `"the max of batch size only support 8, but now is 9."`——这正是 [tiling.cpp:138-140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L138-L140) 那条日志。

**预期结果**：现有合法用例全部 `GRAPH_SUCCESS` 且 `expectTilingKey` 为 0（BF16）或 1（FP16）；新增非法用例失败且错误信息可对到具体校验行。**待本地验证**（本讲义写作环境无 NPU 与 CANN 工具链）。

#### 4.2.5 小练习与答案

**练习 1**：`GetNpuInfo` 里为什么既要查 `aicNum == 0` 又要查 `aivNum_ == 0`？本算子不是 AIV-Only 吗？

**答案**：查 `aivNum_ == 0` 是因为切分以 AIV 核数为分母，为 0 会导致后续除零；查 `aicNum` 是一种通用的平台健全性探测（两数任一为 0 通常意味着平台信息异常/未正确初始化），属于防御式写法，与本算子是否用 Cube 无关。

**练习 2**：README 说 H 取值范围是 `192*2 ~ 192*128`，tiling 代码只检查了 `[384, 24576]` 上下界。两者一致吗？

**答案**：数值范围一致（192×2=384，192×128=24576），但 README 用 192 的倍数表述隐含"H 应为 192 对齐"的设计意图，而 tiling **没有**检查 `hSize % 192 == 0`。即代码比文档宽松——这是给算子加严校验的常见入手点（见综合实践第 2 问）。

**练习 3**：把 [L404-405](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L404-L405) 的 `|| CheckOutputValid()` 改成 `|| ge::GRAPH_SUCCESS != CheckOutputValid()`，行为会变吗？

**答案**：不会。因为 `GRAPH_SUCCESS == 0`、失败值非 0，两种写法在"成功→假、失败→真"上等价。改写只是统一风格、提高可读性（避免读者误以为漏写比较是 bug）。

### 4.3 CoreSplit：H→B→S 三级切分与 blockDim

#### 4.3.1 概念说明

`CoreSplit` 回答"数据怎么切、用多少核"。本算子的策略是**三级级联切分**，优先级 H > B > S：

1. **先切 H**：H 是连续内存方向（input `[S,B,H]` 最后一维），按 `H_SIZE_FULL=4096`（UB 全载上限）切成若干份，让每份能整体装进 UB；
2. **再切 B**：若切完 H 还有富余核数，把 Batch 维也摊到不同核；
3. **最后切 S**：只有当 B 已经小到单核独占一个 batch（`baseB_==1`）仍有富余核时，才切序列维。

切分追求的目标是 `blockDim`（\( = \text{baseHCnt} \times \text{baseBCnt} \times \text{baseSCnt} \)）尽量等于 AIV 核数——**核满载、无空转**，同时每份数据量不超过 UB 容量。

#### 4.3.2 核心流程

```
输入: hSize_, bSize_, sSize_, aivNum_      约束: 每核 H 块 ≤ 4096, blockDim ≤ aivNum_

第 1 级 切 H:
  baseHCnt = ⌈hSize / 4096⌉
  特判: 若 (aivNum, baseHCnt) ∈ {(48,5), (40,6), (40,3)} 则上调 baseHCnt 至 {6, 8, 4}（使 aivNum 整除）
  hSize ≤ 4096        → baseH = hSize, baseHTail = hSize            (不切)
  hSize > 4096 未特判 → baseH = 4096, baseHTail = hSize mod 4096 (整除时取 4096)
  hSize > 4096 且特判 → baseH = hSize / baseHCnt (均分), baseHTail = hSize - baseH×(baseHCnt-1)

第 2 级 切 B:
  coreNumH = aivNum / baseHCnt          (每个 H 份还能配几个核)
  baseB = ⌈bSize / coreNumH⌉
  baseBTail = bSize mod baseB (整除时取 baseB)

第 3 级 切 S (仅当 baseB == 1, 即 B 已摊薄到单 batch):
  baseSCnt = coreNumH / bSize
  baseS = min(⌈sSize / baseSCnt⌉, ...) 再反算 baseSCnt = ⌈sSize / baseS⌉
  baseSTail = sSize - baseS×(baseSCnt-1)
否则: baseS = sSize, baseSCnt = 1 (不切 S)

blockDim = baseHCnt × baseBCnt × baseSCnt,  其中 baseBCnt = ⌈bSize / baseB⌉
```

"Tail（尾块）"存在的原因：各维长度往往不能被份数整除，最后一份会比其他份小，Kernel 循环时最后一块要按 `baseXTail` 算边界，避免越界读写。

#### 4.3.3 源码精读

**① H 切分与特判**（[ai_infra_aggregate_hidden_tiling.cpp:293-326](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L293-L326)）：

```cpp
baseHCnt_ = (hSize_ + H_SIZE_FULL - NUM_ONE) / H_SIZE_FULL;  // ⌈hSize/4096⌉
int64_t flagChange = NUM_ZERO;
if (aivNum_ == FOURTY_EIGHT && baseHCnt_ == NUM_FIVE) { baseHCnt_ = NUM_SIX; flagChange = NUM_ONE; }
if (aivNum_ == FOURTY && baseHCnt_ == NUM_SIX)        { baseHCnt_ = NUM_EIGHT; flagChange = NUM_ONE; }
if (aivNum_ == FOURTY && baseHCnt_ == NUM_THREE)      { baseHCnt_ = NUM_FOUR; flagChange = NUM_ONE; }

if (hSize_ <= H_SIZE_FULL) {
    baseH_ = hSize_;
    baseHTail_ = baseH_;
} else {
    baseH_ = H_SIZE_FULL;
    baseHTail_ = hSize_ % baseH_ == NUM_ZERO ? baseH_ : hSize_ % baseH_;
    if (flagChange == NUM_ONE) {  // 特判改过份数 → 改为按份数均分
        baseH_ = hSize_ / baseHCnt_;
        baseHTail_ = hSize_ % baseHCnt_ == NUM_ZERO ? baseH_ : hSize_ - (baseH_ * (baseHCnt_ - NUM_ONE));
    }
}
```

三组特判的共同动机：让 `aivNum` 能被 `baseHCnt` **整除**。以 `aivNum=48, baseHCnt=5` 为例：不调整则 `coreNumH = 48/5 = 9`，最多用 5×9=45 个核、3 核空转；调成 6 后 `coreNumH = 8`，6×8=48 核满载。`flagChange` 标记份数被动过，此时改用"按份数均分"（`hSize/baseHCnt`）代替"按 4096 装满"，否则最后一块可能超限。

**② B、S 两级与 blockDim**（[L329-350](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L329-L350)）：

```cpp
int64_t coreNumH = aivNum_ / baseHCnt_;             // 每个 H 份分到的核数
baseB_ = (bSize_ + coreNumH - NUM_ONE) / coreNumH;  // 每个 B 块的 batch 数
// baseBTail_ ...
baseS_ = sSize_;  baseSTail_ = baseS_;  baseSCnt_ = NUM_ONE;   // 默认不切 S
if (baseB_ == NUM_ONE) {  // B 已摊薄，剩余核数用于切 S
    baseSCnt_ = coreNumH / bSize_;
    baseS_ = baseSCnt_ > sSize_ ? NUM_ONE : (sSize_ + baseSCnt_ - NUM_ONE) / baseSCnt_;
    baseSCnt_ = baseSCnt_ > sSize_ ? sSize_ : (sSize_ + baseS_ - 1) / baseS_;
    baseSTail_ = sSize_ % baseSCnt_ == NUM_ZERO ? baseS_ : sSize_ - (baseS_ * (baseSCnt_ - NUM_ONE));
}
baseBCnt_ = (bSize_ + baseB_ - NUM_ONE) / baseB_;
blockDim_ = baseHCnt_ * baseBCnt_ * baseSCnt_;
```

注意 S 切分里先按核数估一个 `baseS`，再反算 `baseSCnt = ⌈sSize/baseS⌉` 并处理 `baseSCnt > sSize` 的退化情况（S 比核数还少时每核最多分 1 个序列）。

#### 4.3.4 代码实践

**实践目标**：不跑代码，纯手工执行一遍 `CoreSplit`，检验对算法的理解。

**操作步骤**：设 `aivNum_=48`、`S=4096`、`B=4`、`H=4096`（对应 UT 用例 1 的量级），逐步代入 [L293-350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L293-L350)。

**推导过程**（可对照 `DumpTilingInfo` 的 [OP_LOGD 输出，L379-396](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L379-L396) 本地验证）：

| 量 | 计算 | 结果 |
| --- | --- | --- |
| baseHCnt | ⌈4096/4096⌉ | 1 |
| baseH / baseHTail | hSize ≤ 4096 | 4096 / 4096 |
| coreNumH | 48/1 | 48 |
| baseB / baseBTail | ⌈4/48⌉ | 1 / 1 |
| baseSCnt | 48/4（因 baseB==1） | 12 |
| baseS | ⌈4096/12⌉ | 342 |
| baseSCnt（反算） | ⌈4096/342⌉ | 12 |
| baseSTail | 4096−342×11 | 334 |
| baseBCnt | ⌈4/1⌉ | 4 |
| **blockDim** | 1×4×12 | **48（核满载）** |

**预期结果**：每核处理 `[B=1, S≈342, H=4096]` 约 140 万个元素，48 个核刚好用满。

#### 4.3.5 小练习与答案

**练习 1**：为什么切 S 的条件是 `baseB_ == NUM_ONE` 而不是"核数还有富余"？

**答案**：`baseB_==1` 表示每个核已经独占一个 batch（B 维切无可切）。若 `baseB_>1` 说明 B 维还没摊薄（核数少于 B 所需的块数），此时再切 S 会让同一个 batch 的数据分散到多个核，增加跨核访存与同步复杂度，所以策略上优先保证 B 维完整落在一个核内。

**练习 2**：`baseHTail_` 在什么情况下等于 `baseH_`？

**答案**：两种情况：`hSize ≤ 4096` 不切 H 时（L317-318）；或 `hSize` 恰好被整除时（`hSize % baseH == 0`，尾块与主块等大，L321 与 L324 两处都做了这个判断）。

**练习 3**：若 `sSize=5`、`baseSCnt` 初算为 12（`baseSCnt > sSize`），代码会怎么退化处理？

**答案**：[L343-344](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L343-L344) 先把 `baseS` 钳到 1、`baseSCnt` 钳到 `sSize=5`，即每个序列单独一核，只用 5 个核（blockDim 会小于 aivNum，剩余核空转）——宁可空转也不让一个核跨序列处理。

### 4.4 DoTiling：输出契约与 Tiling 函数注册

#### 4.4.1 概念说明

`AHInfoParser` 算完的一切都汇总在 `AHTilingInfo` 里，`AiInfraAggregateHiddenTiling::DoTiling` 负责把它们**写回 TilingContext**。这是 Host 侧 Tiling 的"输出面"，共四项契约：

| 输出 | API | 消费方 | 本算子取值 |
| --- | --- | --- | --- |
| 核数 | `SetBlockDim` | 调度器（按此启动 N 个核） | `baseHCnt×baseBCnt×baseSCnt` |
| 工作空间 | `GetWorkspaceSizes(1)` 写 `workSpaces[0]` | 内存管理（预分配 GM） | 固定 100 MB（[L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L66)） |
| 切分数据 | `SaveToBuffer` + `SetDataSize` 写 RawTilingData | Kernel 入口 `GET_TILING_DATA_WITH_STRUCT` | 13 个字段序列化 |
| 编译分支 | `SetTilingKey` | 编译器/Kernel 入口 `TILING_KEY_IS` | 0（BF16）或 1（FP16） |

#### 4.4.2 核心流程

```
TilingForAiInfraAggregateHidden(context)          ← CANN 按 IMPL_OP_OPTILING 注册回调此处
 ├─ context 判空
 ├─ AHInfoParser(context).ParseAndCheck(ahInfo)   ← 失败即返回 GRAPH_FAILED
 └─ AiInfraAggregateHiddenTiling(context).DoTiling(&ahInfo)
     ├─ SetBlockDim(blockDim)
     ├─ workSpaces[0] = 100MB
     ├─ tilingData_.set_xxx(...) × 13
     ├─ SaveToBuffer(RawTilingData) + SetDataSize
     └─ SetTilingKey(tilingKey)
```

#### 4.4.3 源码精读

**① DoTiling 主体**（[ai_infra_aggregate_hidden_tiling.cpp:426-460](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L426-L460)），关键三段：

```cpp
context_->SetBlockDim(static_cast<uint32_t>(tilingInfo->blockDim));   // ① 核数

size_t *workSpaces = context_->GetWorkspaceSizes(1);                  // ② workspace
workSpaces[0] = DEFAULT_WORKSPACE_SIZE;                               //    100MB

tilingData_.set_ifMask(tilingInfo->ifMask);                           // ③ 切分数据
// ...共 13 个 set_xxx
tilingData_.SaveToBuffer(context_->GetRawTilingData()->GetData(),
                         context_->GetRawTilingData()->GetCapacity());
context_->GetRawTilingData()->SetDataSize(tilingData_.GetDataSize());

context_->SetTilingKey(tilingInfo->tilingKey);                        // ④ 编译分支
```

`GetWorkspaceSizes(1)` 表示"我要 1 段 workspace"，返回可直接写 `workSpaces[0]`。`SaveToBuffer` 把结构体按声明顺序序列化进 RawTilingData 缓冲区，`SetDataSize` 告诉框架实际字节数——Kernel 侧的 `GET_TILING_DATA_WITH_STRUCT`（[ai_infra_aggregate_hidden.cpp:30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L30)）据此反序列化。有趣的是，本算子 Kernel 入口虽然带 `workspace` 形参，但 Kernel 实现里并未引用它（在 `op_kernel/` 目录检索 `workspace` 只出现在入口签名）——100 MB 属于保守预留，预留过大会浪费 GM 但不影响正确性。

**② 入口函数与防御**（[L463-475](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L463-L475)）：

```cpp
ge::graphStatus TilingForAiInfraAggregateHidden(gert::TilingContext *context)
{
    OP_CHECK_IF(context == nullptr,
        OPS_REPORT_VECTOR_INNER_ERR("AiInfraAggregateHidden", "Tiling context is null."),
        return ge::GRAPH_FAILED);
    AHTilingInfo ahInfo;
    AHInfoParser ahInfoParser(context);
    if (ahInfoParser.ParseAndCheck(ahInfo) != ge::GRAPH_SUCCESS) {
        return ge::GRAPH_FAILED;
    }
    AiInfraAggregateHiddenTiling ahTiling(context);
    return ahTiling.DoTiling(&ahInfo);
}
```

最外层的判空用 `OPS_REPORT_VECTOR_INNER_ERR`（来自 `err/ops_err.h`，[tiling.cpp:18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L18) 引入）而不是 `OP_LOGE`，因为它同时向错误向量上报，供框架聚合展示。

**③ 注册**（[L478-480](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L478-L480)）：

```cpp
IMPL_OP_OPTILING(AiInfraAggregateHidden)
    .Tiling(TilingForAiInfraAggregateHidden)
    .TilingParse<AiInfraAggregateHiddenCompileInfo>(TilingPrepareForAiInfraAggregateHidden);
```

`IMPL_OP_OPTILING(算子名)` 把 Tiling 函数绑定到 u2-l2 里 `OP_ADD(AiInfraAggregateHidden)` 注册的原型上——**四层靠算子名对齐**在此再次得到印证。`TilingParse` 挂接图编译期的准备函数，本算子的实现是直接返回成功的空函数（[L420-423](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L420-L423)）。

#### 4.4.4 代码实践

**实践目标**：用 UT 断言验证输出契约中"workspace"一项，体会契约的可测试性。

**操作步骤**：

1. 看 UT 用例 1 的期望值（[test_ai_infra_aggregate_hidden_tiling.cpp:53-55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L53-L55)）：`expectTilingKey = 0`、`expectWorkspaces = {100 * 1024 * 1024}`。
2. 在本地临时把 [tiling.cpp:435](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L435) 的 `workSpaces[0] = DEFAULT_WORKSPACE_SIZE;` 改成 `workSpaces[0] = 1024;`（仅本地实验，勿提交）。
3. 重新编译运行该 UT（`bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost`）。
4. 改回原值。

**需要观察的现象**：改动后用例在 workspace 断言处失败，错误信息指出期望 `100*1024*1024`、实际 `1024`。

**预期结果**：证明 UT 的 `ExecuteTestCase` 确实对四项输出契约中的 tilingKey 与 workspace 做了逐项断言。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`SetTilingKey(0)` 之后，编译器如何"知道"该编译哪个分支？

**答案**： Ascend C 的机制是：`_def.cpp` + tilingKey 共同决定二进制变体。工具链会为每个用到的 tilingKey 生成/选择一份 Kernel 二进制，运行时 `SetTilingKey` 的值决定加载哪份；Kernel 入口里 `TILING_KEY_IS(0)` / `TILING_KEY_IS(1)` 的分支与之一一对应。

**练习 2**：`DoTiling` 里 `GetWorkspaceSizes(1)` 的参数 `1` 是什么意思？能传 `2` 吗？

**答案**：参数是需要的 workspace 段数，返回可写 `workSpaces[0..n-1]` 的数组。传 `2` 语法上允许（框架会准备两段），但本算子只用一段，多要的一段纯属浪费；`GetNpuInfo` 阶段（[L94-96](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L94-L96)）也按 `1` 预检，两处必须一致。

**练习 3**：`TilingPrepareForAiInfraAggregateHidden` 为什么是空实现还要注册？

**答案**：`TilingParse` 钩子在**图编译期**（比 Tiling 更早）执行，常见用途是解析 attr、填充 CompileInfo 缓存。本算子没有编译期要做的事，但注册本身确立了"Tiling 函数 + 编译期钩子 + CompileInfo 类型"的完整绑定关系，保留空实现使后续扩展（例如缓存 attr）不必改注册结构。

## 5. 综合实践

**任务**：在 tiling 走读基础上完成三问，全部围绕 [ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L1-L483)。

**第 1 问：把 `H_SIZE_FULL` 从 4096 改成 2048，切分如何变化？**

按 4.3.4 的手工推演方法（同样取 `aivNum_=48, S=4096, B=4, H=4096`）：

| 量 | H_SIZE_FULL=4096 | H_SIZE_FULL=2048 | 变化 |
| --- | --- | --- | --- |
| baseHCnt | 1 | ⌈4096/2048⌉ = 2 | ×2 |
| baseH / baseHTail | 4096 / 4096 | 2048 / 2048（整除） | 减半 |
| coreNumH | 48 | 24 | 减半 |
| baseB / baseBTail | 1 / 1 | ⌈4/24⌉ = 1 / 1 | 不变 |
| baseSCnt | 12 | 24/4 = 6 | 减半 |
| baseS / baseSTail | 342 / 334 | ⌈4096/6⌉=683 / 4096−683×5=681 | ≈×2 |
| blockDim | 48 | 2×4×6 = 48 | **不变** |

结论：**总核数与单核总元素量不变**（每核约 140 万元素），变的是每核 tile 的形状——H 方向减半、S 方向翻倍。这正是"UB 能装多少决定 H 块上限"的直接体现：`H_SIZE_FULL` 的语义就是"UB 全载时 H 的最大值"（[L63 注释](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L63)）。同时注意特判表 `(48,5)→6` 等可能命中不同分支（如 `H=10000` 时旧值 baseHCnt=⌈10000/4096⌉=3 命中 `(40,3)→4`，新值 ⌈10000/2048⌉=5 命中 `(48,5)→6`），实际影响需结合具体核数。验证方式：本地改常量 → 跑 UT → 打开 `DumpTilingInfo` 的 DEBUG 日志核对逐项数值。**待本地验证**。

**第 2 问：为 `CheckInputValid` 补充"B 维度超过 8 时报错"的分支。**

先读代码：**这条分支已经存在**——[tiling.cpp:138-140](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L138-L140)：

```cpp
OP_CHECK_IF(bSize_ > B_SIZE_LIMIT,
    OP_LOGE(opName_, "the max of batch size only support 8, but now is %d.", bSize_),
    return ge::GRAPH_FAILED);
```

所以实践改为两步：(a) 用 4.2.4 的非法用例（`B=9`，期望 `GRAPH_FAILED`）验证该分支确实生效；(b) 补一个**尚缺的下界校验**——README 约定 B 取值 1~8，但代码没查 `B < 1`；若 `bSize_=0`，`CoreSplit` 中 `bSize_ % baseB_` 等计算会出现除零/未定义行为。补法（示例代码，紧跟在现有 B 检查之后）：

```cpp
// 示例代码：补充 B 维度下界校验
OP_CHECK_IF(bSize_ < NUM_ONE,
    OP_LOGE(opName_, "the batch size must be at least 1, but now is %d.", bSize_),
    return ge::GRAPH_FAILED);
```

编译验证：`bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost`（无 NPU 环境时至少通过编译；用 `B=0` 的用例验证报错日志）。**待本地验证**。

**第 3 问：tilingKey 0/1 分别对应哪种数据类型？给出依据。**

**答案**：`0 → bfloat16（BF16）`，`1 → float16（FP16）`。依据链（三处互证）：

1. 宏定义：[tiling.h:19-20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L19-L20) `AGGREGATE_HIDDEN_BF16=0`、`AGGREGATE_HIDDEN_HALF=1`（HALF 是 CANN 对 fp16 的惯用称呼）。
2. Host 侧赋值：[`GetTilingKey`（tiling.cpp:282-289）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289) 默认 `BF16(0)`，当 `inputType_ == ge::DT_FLOAT16` 时改设 `HALF(1)`。
3. Kernel 侧分支：[ai_infra_aggregate_hidden.cpp:29-40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L29-L40) `TILING_KEY_IS(AGGREGATE_HIDDEN_BF16)` 分支实例化 `KernelAiInfraAggregateHidden<bfloat16_t>`，`TILING_KEY_IS(AGGREGATE_HIDDEN_HALF)` 分支实例化 `KernelAiInfraAggregateHidden<half>`。
4. 旁证：UT 用例 1/2 分别以 `DT_BF16`/`DT_FLOAT16` 输入断言 `expectTilingKey` 为 0/1（[test_ai_infra_aggregate_hidden_tiling.cpp:53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L53) 与 [L81](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L81)）。

## 6. 本讲小结

- Tiling 是 Kernel 启动前的"作战规划"：从 `TilingContext` 读入 shape 与平台信息，产出 blockDim、tilingKey、TilingData 序列化字节流与 workspace 四项契约，不搬运数据也不做计算。
- `AHInfoParser::ParseAndCheck` 按"取平台信息 → 五步校验 → 定 tilingKey → 切分 → 汇总"十步串行，任一步失败立即 `GRAPH_FAILED`；校验规则与 README 规格逐条对应，风格统一为 `OP_CHECK_IF` + `OP_LOGE`。
- `CoreSplit` 采用 H→B→S 三级级联切分：H 按 UB 全载上限 4096 切并做核数整除特判，B 次之，S 仅在 B 摊薄后切；`blockDim = baseHCnt×baseBCnt×baseSCnt`，目标是 AIV 核满载。
- tilingKey 是 Host 写、Device 读的跨侧信号：0=BF16、1=FP16，两侧靠**同名同值宏**对齐（改一侧必须同步另一侧）。
- TilingData 结构体由宏声明并生成序列化代码，`REGISTER_TILING_DATA_CLASS` 绑定算子名；Kernel 用 `GetBlockIdx()` 对 `Cnt` 字段取余反推自己负责的 tile，契约闭环。
- `IMPL_OP_OPTILING(算子名)` 把 Tiling 函数挂到 `_def.cpp` 注册的原型上，再次体现"四层靠算子名对齐"。

## 7. 下一步学习建议

Tiling 把切分参数送到了设备侧，下一讲 **u2-l4（op_kernel 入门）** 将打开黑盒的另一半：精读 [ai_infra_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L24-L42) 与 `ai_infra_aggregate_hidden.h`，看 `KernelAiInfraAggregateHidden<dtype>` 模板类如何用本讲的 TilingData 完成 Init（绑 GM 地址、配 UB 队列）与 Process（CopyIn→Compute→CopyOut 流水）。读完 u2-l4 后，还可以回头把本讲 `H_SIZE_FULL` 与 kernel 里 UB 队列的 `TQue` 分块大小对上，理解 4096 这个数字的最终出处。之后再进入 u3-l1（utils 错误日志体系）了解 `OP_LOGE` 背后的完整机制。
