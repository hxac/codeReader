# OpParam 参数结构与入参校验

## 1. 本讲目标

上一讲（u2-l2）我们跟读了 `HcclAllReduce` 的入口：兼容分发 → `AllReduceInitAndCheck`（环境变量解析 + 入参校验）→ `AllReduceOutPlace` → `AllReduceOutPlaceCommon`。本讲继续往下钻一层，聚焦三件事：

1. **OpParam** —— 这是贯穿「算子入口 → Selector → Executor → Template」整条执行链路的「中央数据结构」。每一级代码都从它身上读取信息、往它身上写入结果。你要能看懂它有哪些字段、按什么逻辑组织。
2. **入参校验** —— 用户传进来的指针、count、数据类型、归约算子是怎么被校验的，校验用的 `CHK_RET` / `CHK_PTR_NULL` / `RPT_INPUT_ERR` 等宏分别做什么。
3. **TopoInfoWithNetLayerDetails 与引擎前缀映射** —— 本轮代码演进在 `alg_param.h` 中新增了 `TopoInfoWithNetLayerDetails::hostDpuOnly` 字段（含序列化支持）和 `ENGINE_PREFIX_MAP` / `ENGINE_STR_MAP` 两张映射表，它们是新选择器（代价模型路径）判断「哪些引擎可用」的关键输入。

学完本讲，你应当能够：

- 说出 OpParam 的关键字段（`opType` / `engine` / `opMode` / `DataDes` / `inputPtr`·`outputPtr` / `tag` 等）及其作用；
- 读懂 `FillAllReduceOpParam` 如何把 API 入参装配进 OpParam；
- 解释 `TopoInfoWithNetLayerDetails` 的序列化流程、新增的 `hostDpuOnly` 字段如何判定、以及 `ENGINE_PREFIX_MAP` 如何从算法名前缀反推引擎；
- 区分 HCCL 中两套并存的校验工具（`op_common.cc` 的 `Check*` 与 `param_check.cc` 的 `HcomCheck*`），并掌握 `CHK_RET` / `CHK_PTR_NULL` / `RPT_INPUT_ERR` 校验宏的语义。

## 2. 前置知识

在阅读本讲前，你需要具备以下认知（来自前置讲义）：

- **算子的执行链路骨架**：`HcclAllReduce → AllReduceOutPlace → AllReduceOutPlaceCommon → Selector() → HcclExecOp()`（u2-l2）。
- **算子（op）/ 算法（alg）/ 引擎（engine）三个维度的区别**：engine 分 AICPU / AIV / CCU（u1-l2、u1-l3）。
- **API 参数模型**：所有算子共享 `sendBuf / recvBuf / count / dataType / op / comm / stream` 参数；`count` 是元素个数而非字节（u2-l1）。
- **tag 的作用**：形如 `"AllReduce_<commName>"`，作为同一通信域拓扑/资源的缓存键，使多次调用共享同一份 topoInfo（u2-l2）。
- **RankGraph 分层拓扑**：Server 内（Layer0）快、Server 间（Layer1）慢，这是分级通信的物理依据（u1-l2）。

一个关键直觉：**OpParam 是「函数参数」到「执行状态」的转换层**。C 接口的参数是分散的、扁平的；进入 HCCL 内部后，这些参数被组装成一个结构体对象，随调用链一路传递、被各级代码读写。理解 OpParam，就是理解 HCCL 内部「数据是怎么流动的」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/inc/alg_param.h` | 定义 OpParam 及其内嵌的 DataDes 联合体、数据类型字节表 `DATATYPE_SIZE_TABLE`、`TopoInfoWithNetLayerDetails`（含 `hostDpuOnly`）、`ENGINE_PREFIX_MAP` / `ENGINE_STR_MAP` 两张新映射表，以及众多配套结构体（AlgResourceCtx 等）。 |
| `src/ops/all_reduce/all_reduce_op.cc` | `AllReduceInitAndCheck`（校验 + rank 信息 + tag）、`CheckAllReduceInputPara`（指针非空校验）、`FillAllReduceOpParam`（装配 OpParam）。 |
| `src/ops/op_common/op_common.cc` | `CheckCount` / `CheckDataType` / `CheckReduceOp` —— AllReduce 入口实际调用的新一代校验函数；另含 `IsHostDpu` / `IsBarrierHostDpu` 等 hostDpuOnly 场景的判定入口。 |
| `src/common/param_check.h` / `src/common/param_check.cc` | `HcomCheck*` 系列可复用校验工具（tag/count/dataType/group/stream）。 |
| `src/ops/op_common/topo/topo_host.cc` | `CalcHostDPUOnly` —— 本轮新增函数，依据拓扑层级与端点位置判定 `hostDpuOnly`。 |
| `src/ops/op_common/selector/selector_engine.cc` | 新选择器 `SelectorEngine` 消费 `hostDpuOnly` 与 `ENGINE_PREFIX_MAP` 的地方（`GetEnginePriority` / `GetEngineByAlgName`），本讲只看接口，u8-l1 详讲。 |
| `src/common/log.h` | `CHK_RET` / `CHK_PTR_NULL` / `CHK_PRT_RET` / `CHK_RET_AND_PRINT_IDE` 校验宏。 |
| `src/common/adapter_error_manager_pub.h` | `RPT_INPUT_ERR` 结构化错误上报宏。 |

## 4. 核心概念与源码讲解

### 4.1 OpParam 结构与 DataDes 联合体

#### 4.1.1 概念说明

`OpParam` 是 HCCL 内部的「算子参数对象」。它定义在 `alg_param.h` 中，注释里写得很直白：

> `struct OpParam { // 不申请ctx，每个算子单独下发`

这句话点出两个设计意图：

1. **不申请 ctx**：OpParam 本身只是「参数容器」，它不持有（也不在构造时申请）任何 device 侧资源上下文（ctx）。真正需要在 device 上常驻的资源（线程、通道、缓存等）由后续阶段申请，并存在 `OpParam::resCtx` 这个指针里。
2. **每个算子单独下发**：每次调用 `HcclAllReduce` 都会在栈上构造一个临时的 `OpParam param;`，装配好后随链路传递，调用结束即销毁。它是一次性、单次调用粒度的对象。

为什么需要这样一个「胖结构体」而不是直接把 C 参数一层层透传？因为执行链路很长（入口 → Selector → Executor → Template → Kernel），如果每层函数签名都带十几个参数，代码会极其冗长且易错。把所有状态收拢进一个 `OpParam`，各级代码按需读写自己关心的字段，是 HCCL 控制复杂度的关键手法。

#### 4.1.2 核心流程

OpParam 的字段可以按职责分成几组：

| 分组 | 代表字段 | 含义 |
| --- | --- | --- |
| 执行上下文 | `hcclComm`、`stream` | 通信域句柄与异步任务流 |
| 缓存键 | `tag`、`algTag`、`fastLaunchTag`、`commModeTag` | 分别作为 topoInfo / 资源 / 快速下发 / 执行模式资源的查找键 |
| 数据缓冲 | `inputPtr`·`inputSize`、`outputPtr`·`outputSize` | 输入输出缓冲地址与字节数 |
| 归约/寻址 | `reduceType`、`root`、`userRank`、`sendRecvRemoteRank` | 归约算子、root rank、本 rank、对端 rank |
| 三个旋钮 | `opMode`、`engine`、`algType` | 执行模式 / 通信引擎 / 算法类型 |
| 数据描述 | `DataDes`（联合体） | count、dataType、outputType、strideCount 等 |
| 算子类型 | `opType` | `HcclCMDType` 枚举，决定走哪个联合体成员 |
| 运行态资源 | `resCtx`、`opThread`、`ctxSize` | device 侧资源上下文指针与算子线程 |
| 模式开关 | `isMc2`、`cacheValid`、`aicpuCacheEnable`、`isCapture` | MC2 自定义算子 / 缓存命中 / aclgraph 等标记 |

其中 **三个旋钮**（`opMode` / `engine` / `algType`）和 **一个联合体**（`DataDes`）是理解 OpParam 的核心，下面重点讲。

`opMode` 是 `OpMode` 枚举（[alg_param.h:152](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L152)）：

```cpp
enum class OpMode { OFFLOAD = 0, OPBASE = 1, ACLGRAPH = 2 };
```

- `OPBASE`：单算子模式（用户直接调用 `HcclAllReduce`）。
- `OFFLOAD`：图模式执行阶段（资源由 GE 预分配，见 u7-l2）。
- `ACLGRAPH`：aclgraph 捕获模式。

#### 4.1.3 源码精读

OpParam 的核心定义在 [src/ops/op_common/inc/alg_param.h:576-662](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L576-L662)。下面是关键片段（已删减注释与部分字段）：

```cpp
struct OpParam { // 不申请ctx，每个算子单独下发
    void* hcclComm;
    char tag[TAG_LENGTH] = "";               // 保存topoInfo的key值
    char algTag[ALG_TAG_LENGTH] = "";        // 保存资源的key值，和算法绑定
    aclrtStream stream;
    void* inputPtr = nullptr;
    u64 inputSize = 0;
    void* outputPtr = nullptr;
    u64 outputSize = 0;
    HcclReduceOp reduceType = HcclReduceOp::HCCL_REDUCE_RESERVED;
    u32 root = INVALID_VALUE_RANKID;
    u32 userRank = INVALID_VALUE_RANKID;
    OpMode opMode;
    HcclDevType deviceType = HcclDevType::DEV_TYPE_COUNT;
    CommEngine engine = CommEngine::COMM_ENGINE_RESERVED;
    AlgType algType;
    union {
        struct {
            u64 count;
            HcclDataType dataType;
            HcclDataType outputType;
            u64 strideCount;
        } DataDes = {0, HCCL_DATA_TYPE_RESERVED, HCCL_DATA_TYPE_RESERVED, 0};
        struct { /* ... */ } all2AllDataDes;
        struct { void* counts; void* displs; HcclDataType dataType; } vDataDes;
        struct { /* ... */ } all2AllVDataDes;
        struct { /* ... */ } all2AllVCDataDes;
        struct { /* ... */ } batchSendRecvDataDes;
    };
    HcclCMDType opType = HcclCMDType::HCCL_CMD_INVALID;
    char algName[OP_ALG_LENGTH] = "";
    // ... 运行态资源与模式开关字段
    void* resCtx = nullptr;
};
```

注意几个细节：

1. **默认值都是「无效值」**：`reduceType` 默认 `HCCL_REDUCE_RESERVED`、`root` 默认 `INVALID_VALUE_RANKID`、`engine` 默认 `COMM_ENGINE_RESERVED`、`opType` 默认 `HCCL_CMD_INVALID`。这是一种防御式编程——任何未被显式赋值的字段都保持可识别的「未设置」状态，便于后续代码判断。

2. **DataDes 是联合体（union）**：这是 OpParam 设计上最巧妙的一点。集合通信算子的数据描述形态多种多样：
   - `DataDes`：普通算子（AllReduce / ReduceScatter / AllGather）—— count + dataType；
   - `all2AllDataDes`：AlltoAll —— 发送/接收各有 count 与类型；
   - `vDataDes`：变长算子（ReduceScatterV）—— 用 `counts` / `displs` 指针数组；
   - `batchSendRecvDataDes`：BatchSendRecv —— 一组 `HcclSendRecvItem`。

   用 union 让这些互斥的描述共享同一块内存，**由 `opType` 字段决定当前哪个成员有效**。例如 AllReduce 时 `opType = HCCL_CMD_ALLREDUCE`，应读 `DataDes.count`；AlltoAll 时 `opType = HCCL_CMD_ALLTOALL`，应读 `all2AllDataDes.sendCount`。

3. **`engine` 在 OpParam 定义时是 RESERVED**：也就是说 OpParam 构造出来时还不知道用哪个引擎，引擎是后面 `HcclGetOpExpansionMode` 才填进去的（见 u2-l4）。这解释了为什么「装配」和「引擎选择」是两个分开的步骤。

数据类型到字节数的映射由 `DATATYPE_SIZE_TABLE` 提供，定义在 [src/ops/op_common/inc/alg_param.h:43-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L43-L61)：

```cpp
constexpr uint32_t DATATYPE_SIZE_TABLE[HCCL_DATA_TYPE_RESERVED]
    = {sizeof(int8_t), sizeof(int16_t), sizeof(int32_t), 2,
       sizeof(float),  sizeof(int64_t),  sizeof(uint64_t), /* ... */ };
```

它是一个以 `HcclDataType` 枚举值为下标的静态查找表，`FillAllReduceOpParam` 正是用它把「元素个数 count」换算成「字节数 size」。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，建立「`opType` 决定读 union 的哪个成员」的直觉。

**操作步骤**：

1. 打开 [src/ops/op_common/inc/alg_param.h:607-642](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L607-L642)，列出 union 中的全部 6 个 struct 成员名。
2. 用 `Grep` 搜索每个成员名（如 `all2AllDataDes`、`vDataDes`、`batchSendRecvDataDes`），看它们分别在哪个算子的 `Fill*OpParam` 里被赋值。
3. 对比 `opType` 字段：搜索 `param.opType = HcclCMDType::` 出现的地方，确认每个算子设置了哪个 `HcclCMDType`。

**需要观察的现象**：每种数据描述形态只被一种 `HcclCMDType` 使用，二者一一对应（或一组对应）。

**预期结果**：例如 AlltoAll 系列算子设置 `opType = HCCL_CMD_ALLTOALL` 并写 `all2AllDataDes` / `all2AllVDataDes`，而 AllReduce 设置 `HCCL_CMD_ALLREDUCE` 并写 `DataDes`。

> 待本地验证：不同算子是否严格遵循「一种 opType 对应一种 union 成员」的约定，可借助 grep 的命中分布自行确认。

#### 4.1.5 小练习与答案

**练习 1**：OpParam 注释写「不申请 ctx」，那 device 侧资源上下文存在哪个字段里？

> **答案**：存在 `resCtx` 指针字段（[alg_param.h:651](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L651)）。OpParam 自身只是参数容器，真正的 device 资源由后续阶段申请后挂到 `resCtx` 上。

**练习 2**：为什么 `DataDes` 要做成 union 而不是一个包含所有字段的普通 struct？

> **答案**：因为不同算子的数据描述互斥（AllReduce 只需要 count+dataType，AlltoAllV 需要 sendCounts/recvCounts/sdispls/rdispls 四个指针数组）。用 union 让它们共享内存、避免结构体臃肿；由 `opType` 在运行期指明当前有效的成员。

**练习 3**：`engine` 字段的默认值是什么？为什么不是 `COMM_ENGINE_AICPU_TS`？

> **答案**：默认 `COMM_ENGINE_RESERVED`。因为引擎选择发生在装配之后（由 `HcclGetOpExpansionMode` 决定，见 u2-l4），OpParam 构造时尚不能确定引擎，所以用 RESERVED 占位，避免误读。

---

### 4.2 FillAllReduceOpParam 装配

#### 4.2.1 概念说明

`FillAllReduceOpParam` 是「装配器」：它接收 C 接口传进来的扁平参数（sendBuf / recvBuf / count / dataType / op / stream），把它们填进一个 `OpParam` 对象。这是 OpParam 在生命周期里第一次被「有目的地赋值」。

它的调用位置在 `AllReduceOutPlaceCommon` 的最开头，**在 Selector 之前、在引擎选择之前**。也就是说：装配阶段只填「用户给的」信息（缓冲、count、类型、归约算子、设备类型），不填「系统算出来的」信息（engine、algType、algName、tag 等留待后续阶段）。

一个关键区别要记住（承接 u2-l2）：

- `param.tag`（`"AllReduce_<commName>"`）是在 **`AllReduceInitAndCheck`** 里用 `sprintf_s` 填的；
- `param.inputPtr / inputSize / outputPtr / outputSize / DataDes / opType / reduceType / deviceType` 是在 **`FillAllReduceOpParam`** 里填的；
- `param.engine` 是在 **`HcclGetOpExpansionMode`** 里填的。

三者分属不同阶段，不要混淆。

#### 4.2.2 核心流程

`FillAllReduceOpParam` 的装配逻辑可以概括为三步：

1. **字节数换算**：用 `DATATYPE_SIZE_TABLE[dataType]` 得到每个元素的字节数 `perDataSize`，再算
   \[ \text{outputSize} = \text{count} \times \text{perDataSize}, \quad \text{inputSize} = \text{outputSize} \]
   （AllReduce 输入输出等长，所以 inputSize = outputSize）。
2. **填执行上下文与缓冲**：`stream`、`reduceType`、`opMode`、`inputPtr`·`inputSize`、`outputPtr`·`outputSize`。
3. **填数据描述与算子类型**：`DataDes.count`、`DataDes.dataType`、`opType = HCCL_CMD_ALLREDUCE`，并查询设备类型填 `deviceType`。

#### 4.2.3 源码精读

装配函数定义在 [src/ops/all_reduce/all_reduce_op.cc:159-187](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L159-L187)：

```cpp
HcclResult FillAllReduceOpParam(
    void* sendBuf, void* recvBuf, uint64_t count, HcclDataType dataType, HcclReduceOp op, const HcclComm comm,
    aclrtStream stream, OpMode opMode, OpParam& param)
{
    (void)comm;
    u32 perDataSize = DATATYPE_SIZE_TABLE[dataType];
    u64 outputSize = count * perDataSize;
    u64 inputSize = outputSize;

    param.stream = stream;
    param.reduceType = op;
    param.opMode = opMode;

    HcclDevType deviceType = HcclDevType::DEV_TYPE_COUNT;
    CHK_RET(HcclGetDeviceType(deviceType));

    // 参数准备
    param.inputPtr = sendBuf;
    param.inputSize = inputSize;
    param.outputPtr = recvBuf;
    param.outputSize = outputSize;
    param.DataDes.count = count;
    param.DataDes.dataType = dataType;
    param.opType = HcclCMDType::HCCL_CMD_ALLREDUCE;
    param.enableDetour = false;
    param.deviceType = deviceType;
    param.reduceType = op;
    return HCCL_SUCCESS;
}
```

逐行说明：

- `perDataSize = DATATYPE_SIZE_TABLE[dataType]`：查表得每个元素字节数。
- `outputSize = count * perDataSize`：元素个数 → 字节数。注意 `count` 是元素个数（u2-l1），这里才换算成字节。
- `inputSize = outputSize`：AllReduce 输入输出等长（u1-l5）。
- `CHK_RET(HcclGetDeviceType(deviceType))`：运行期查询当前 NPU 的设备类型（如 910B / 950），填入 `deviceType`。这一步很重要——后续 CCU FastLaunch、AICPU Task Cache 等特性是否可用都取决于设备类型（u4-l2、u5）。
- `param.DataDes.count = count` / `param.DataDes.dataType = dataType`：填充联合体的 `DataDes` 成员。
- `param.opType = HcclCMDType::HCCL_CMD_ALLREDUCE`：**正是这个字段告诉后续代码「当前 union 有效成员是 DataDes」**。
- `param.reduceType = op`：注意这一行出现了两次（函数开头和结尾各一次），是冗余赋值，但无害——最终值就是用户传入的 `op`。

它的调用点在 [all_reduce_op.cc:189-197](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L189-L197)，位于 `AllReduceOutPlaceCommon` 最开头：

```cpp
HcclResult AllReduceOutPlaceCommon(/* ... */, OpParam& param)
{
    HCCL_INFO("Start to execute AllReduceOutPlace");

    CHK_RET(FillAllReduceOpParam(sendBuf, recvBuf, count, dataType, op, comm, stream, opMode, param));

    CHK_RET(HcclGetOpExpansionMode(comm, param));   // 这里才填 param.engine
    // ... CCU 9.0 老流程 / CCU FastLaunch / AIV Cache / 单卡 / Selector / HcclExecOp
}
```

可以清楚看到装配（`FillAllReduceOpParam`）和引擎选择（`HcclGetOpExpansionMode`）是相邻但独立的两个步骤。

#### 4.2.4 代码实践

**实践目标**：给定一次真实的 `HcclAllReduce` 调用，手工推演 `FillAllReduceOpParam` 执行后 OpParam 各关键字段的值。

**调用**：
```cpp
HcclAllReduce(sendBuf, recvBuf, /*count=*/1024, HCCL_DATA_TYPE_FP32, HCCL_REDUCE_SUM, comm, stream);
```
（假设 `HcclGetDeviceType` 返回 `DEV_TYPE_950`。）

**操作步骤**：

1. 查 `DATATYPE_SIZE_TABLE`（[alg_param.h:43-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L43-L61)）得 FP32（`sizeof(float)`）= 4 字节。
2. 套公式：`outputSize = inputSize = 1024 × 4 = 4096` 字节。
3. 逐字段对照 `FillAllReduceOpParam` 函数体填写。

**预期结果**（`FillAllReduceOpParam` 执行后）：

| 字段 | 值 | 来源 |
| --- | --- | --- |
| `stream` | 调用者传入的 stream | 入参直填 |
| `inputPtr` | `sendBuf` | 入参直填 |
| `inputSize` | `4096`（字节） | `1024 × 4` |
| `outputPtr` | `recvBuf` | 入参直填 |
| `outputSize` | `4096`（字节） | `1024 × 4` |
| `DataDes.count` | `1024` | 入参直填 |
| `DataDes.dataType` | `HCCL_DATA_TYPE_FP32` | 入参直填 |
| `reduceType` | `HCCL_REDUCE_SUM` | 入参 `op` |
| `opType` | `HCCL_CMD_ALLREDUCE` | 硬编码 |
| `opMode` | `OPBASE`（单算子路径传入） | 由 `AllReduceOutPlace` 传入 |
| `deviceType` | `DEV_TYPE_950` | `HcclGetDeviceType` 查询 |
| `enableDetour` | `false` | 硬编码 |
| `engine` | `COMM_ENGINE_RESERVED`（**尚未赋值**） | 待 `HcclGetOpExpansionMode` 填 |
| `tag` | `"AllReduce_<commName>"`（**在 init 阶段已填，不在本函数**） | `AllReduceInitAndCheck` 填 |

**需要观察的现象**：`engine` 此时仍是默认值 RESERVED——这印证了「装配阶段不决定引擎」。

> 待本地验证：上表中的字节换算（FP32=4B、1024→4096B）可直接对照 `DATATYPE_SIZE_TABLE` 与函数体核对，无需运行设备。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `count=1024` 改成 `count=0`，`FillAllReduceOpParam` 会执行吗？

> **答案**：不会。`HcclAllReduce` 入口在调用 `AllReduceInitAndCheck`（进而装配）之前，有早退分支 `CHK_PRT_RET(count == 0, ..., HCCL_SUCCESS)`（[all_reduce_op.cc:37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L37)）。count 为 0 直接返回成功，不进入装配与执行（u2-l2 已讲过这条早退）。

**练习 2**：为什么 `inputSize = outputSize`？哪种算子不满足这个等式？

> **答案**：因为 AllReduce 输入输出等长（u1-l5）。不满足的是 **AllGather**——它的 `recvBuf = sendCount × R`（只拼接不归约），输出是输入的 R 倍；以及 **ReduceScatter**——输出是输入的 1/R。所以它们的 `Fill*OpParam` 里 inputSize 与 outputSize 的计算公式不同。

**练习 3**：`FillAllReduceOpParam` 里 `param.reduceType = op;` 写了两次，是否有副作用？

> **答案**：无副作用。两次都赋同一个值（用户传入的 `op`），最终结果一致，只是代码上的冗余。可视为可清理项，但行为正确。

---

### 4.3 TopoInfoWithNetLayerDetails 序列化与 hostDpuOnly（本轮新增）

#### 4.3.1 概念说明

`TopoInfoWithNetLayerDetails` 是「通信域拓扑上下文」：它在基础 `TopoInfo`（rank、serverNum、deviceType 等标量信息）之上，追加了 `topoLevelNums`（逻辑拓扑层级数）、`level0Topo`（第 0 层形状）、`netLayerDetails`（每层网络实例的规模明细）以及一组布尔特征位（`Level0Nhr`、`is2DieFullMesh`、`level0BigClosRange` 等）。Selector 和 CostTable 正是依据这些特征位判断「当前拓扑下哪些算法可行」。

**本轮代码演进给它带来两个变化**：

1. 新增布尔字段 `hostDpuOnly`（[alg_param.h:227](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L227)）：标记「本通信域的最外层网络只挂在 Host 侧 DPU 上」——即框间（Server 间）通信只能绕经主机 DPU，device 直连链路不存在。
2. `Serialize()` / `DeSerialize()` 同步补写了该字段（[alg_param.h:265](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L265) 与 [alg_param.h:317](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L317)），保证拓扑信息序列化到 device 侧后该标记不丢失。

同文件还新增了两张映射表（[alg_param.h:138-150](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L138-L150)）：

```cpp
// OpExecuteConfig → 算法名前缀映射
// key=前缀, value=引擎; 逆序遍历可实现长前缀优先匹配
static const std::map<std::string, OpExecuteConfig> ENGINE_PREFIX_MAP = {
    {"Aicpu", OpExecuteConfig::AICPU_TS},     {"Aiv", OpExecuteConfig::AIV},     {"CcuMS", OpExecuteConfig::CCU_MS},
    {"CcuSched", OpExecuteConfig::CCU_SCHED}, {"Dpu", OpExecuteConfig::HOSTCPU},
};

// OpExecuteConfig → 字符串(用于日志)
static const std::map<OpExecuteConfig, const char*> ENGINE_STR_MAP = { /* "AICPU_TS" / "AIV" / ... */ };
```

- **`ENGINE_PREFIX_MAP`**：算法名（algName）→ 引擎的反查表。新选择器产出的算法名都带引擎前缀（如 `AicpuAllReduceSoleNHR`、`CcuMSAllReduceSoleMesh`、`Dpu...`），用这张表即可从名字前缀反推它跑在哪个引擎上。注释点明「逆序遍历可实现长前缀优先匹配」——因为 `std::map` 按 key 字典序排列，`CcuMS` 排在 `CcuSched` 前面，逆序遍历会先尝试更长的 `CcuSched`，避免 `CcuMS` 把 `CcuSchedXxx` 误匹配掉。
- **`ENGINE_STR_MAP`**：引擎枚举 → 日志字符串，用于代价模型缓存键（如 `COST_MODEL_TAG + "_AICPU_TS"`）与日志输出。

注意这里的 `OpExecuteConfig` 是**新选择器体系**的引擎枚举（AICPU_TS / AIV / CCU_MS / CCU_SCHED / HOSTCPU），与 OpParam 里旧体系的 `CommEngine` 枚举是两套并存的表述——前者服务于代价模型路径，后者服务于旧 Selector 路径。

#### 4.3.2 核心流程

**（1）hostDpuOnly 的判定**发生在拓扑信息计算阶段（`HcclCalcTopoInfo`，结果以 `param.tag` 为键缓存），由本轮新增的 `CalcHostDPUOnly` 完成，判定逻辑是逐层排除：

```
CalcHostDPUOnly(comm, topoInfo)
 ├─ serverNum == 1？          → 单服务器，无需 DPU，false
 ├─ topoLevelNums == 1？      → 只有框内一层，false
 ├─ 取最外层（topoLevelNums-1）netLayer 的全部 topoInstance
 │   ├─ topoType != CLOS？    → 跳过该实例
 │   ├─ rankNum != userRankSize？→ 本 rank 未与全部 rank 连通，跳过
 │   └─ 遍历该实例的 Endpoint：
 │       ├─ 存在 DEVICE 位置端点 → 框间还有 device 直连，false（直接返回）
 │       └─ 全部为 HOST 位置端点 → hostDPU = true
 └─ hostDPU == true → topoInfo->hostDpuOnly = true
```

**（2）hostDpuOnly 的消费**发生在新选择器 `SelectorEngine::GetEnginePriority`：`hostDpuOnly == true` 时，候选引擎列表被直接收缩为 `{HOSTCPU}`——只有 Host DPU 引擎可用，AICPU/AIV/CCU 都不参与本次代价比较。此外 `cost_table.cc` 的多条算法过滤规则也以 `!topo->hostDpuOnly` 为前提（如 NHR、三层拓扑算法在 hostDpuOnly 场景下被排除）。

**（3）序列化**：`TopoInfoWithNetLayerDetails::Serialize()` 用 `BinaryStream` 逐字段写入，`hostDpuOnly` 被补写进 `level2Ubg` 与 `level0Symmetric` 之间；`DeSerialize()` 按完全相同的顺序读出。这个结构会被整体嵌入 `AlgResourceCtxSerializable::Serialize()`（先写 `topoInfoSeqSize`，再拼接 topoInfo 的序列化字节），随算法资源上下文一起拷贝到 device 侧执行时使用。

#### 4.3.3 源码精读

**（1）字段与序列化**，[src/ops/op_common/inc/alg_param.h:216-339](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L216-L339) 定义了整个结构体。`hostDpuOnly` 字段与其他特征位排在一起（这段代码声明了 `hostDpuOnly` 字段）：

```cpp
struct TopoInfoWithNetLayerDetails : public TopoInfo { // 通信域拓扑ctx
    u32 topoLevelNums = 0;
    Level0Shape level0Topo;
    bool Level0Nhr{false};
    // ... 其他特征位
    bool level2Ubg{false};
    bool hostDpuOnly{false};        // ← 本轮新增：框间仅 Host DPU 可达
    bool level0Symmetric{false};
    // ...
};
```

`Serialize()` 中对应的新增写入（[alg_param.h:265](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L265) 这一行把 `hostDpuOnly` 写入二进制流）：

```cpp
binaryStream << level2Ubg;
binaryStream << hostDpuOnly;    // ← 新增
binaryStream << level0Symmetric;
```

`DeSerialize()` 中对应的新增读取在 [alg_param.h:317](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L317)，顺序与写入严格一致——**序列化与反序列化必须逐字段对齐**，这是手写序列化代码最容易出错的地方，读源码时可重点检查两边顺序是否同步。

**（2）判定函数 `CalcHostDPUOnly`** 定义在 [src/ops/op_common/topo/topo_host.cc:786-865](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L786-L865)（这段代码实现了 hostDpuOnly 的完整判定逻辑），它在 `HcclCalcTopoInfo` 的特征位计算链末尾被调用（[topo_host.cc:782](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L782)）。关键片段：

```cpp
HcclResult CalcHostDPUOnly(HcclComm comm, TopoInfoWithNetLayerDetails* topoInfo)
{
    topoInfo->hostDpuOnly = false;
    // 只有一个server，不使用DPU
    if (topoInfo->serverNum == 1) { return HCCL_SUCCESS; }
    // 只有一层topo，不使用DPU
    if (topoInfo->topoLevelNums == 1) { return HCCL_SUCCESS; }
    // ... 取 netLayers，只校验最外层（topoLevelNums - 1）
    for (/* 每个 topoInstance */) {
        if (topoType != COMM_TOPO_CLOS) { continue; }        // 只看 CLOS 拓扑实例
        if (rankNum != topoInfo->userRankSize) { continue; } // 必须与全部 rank 连通
        for (/* 每个 Endpoint */) {
            if (endPointDesc.loc.locType == ENDPOINT_LOC_TYPE_DEVICE) {
                return HCCL_SUCCESS;  // 框间仍有 device 直连 → 不是 hostDpuOnly
            } else if (endPointDesc.loc.locType == ENDPOINT_LOC_TYPE_HOST) {
                hostDPU = true;       // 端点挂在 Host 上
            }
        }
    }
    if (hostDPU) { topoInfo->hostDpuOnly = true; }
    return HCCL_SUCCESS;
}
```

判定要点：**只检查最外层网络**（框间层）的 CLOS 实例；只要发现任何一个端点位于 device 侧，就说明 NPU 之间还有直通链路，DPU 不是唯一通路；只有当本 rank 与全部 rank 连通、且所有端点都在 Host 侧时，才认定 `hostDpuOnly = true`。

**（3）消费点一：新选择器的引擎优先级**，[src/ops/op_common/selector/selector_engine.cc:68-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L68-L75)（这段代码在 hostDpuOnly 场景下把候选引擎收缩为 HOSTCPU）：

```cpp
std::vector<OpExecuteConfig>
SelectorEngine::GetEnginePriority(TopoInfoWithNetLayerDetails* topoInfo, OpExecuteConfig opExecuteConfig)
{
    // hostDpuOnly 已在 HcclCalcTopoInfo 中计算并缓存
    if (topoInfo != nullptr && topoInfo->hostDpuOnly) {
        HCCL_INFO("[GetEnginePriority] hostDpuOnly=true, return [HOSTCPU].");
        return {OpExecuteConfig::HOSTCPU};
    }
    // ... 正常场景按 CCU_MS → CCU_SCHED → AICPU 等回退链给出候选
}
```

**（4）消费点二：算法名前缀反查引擎**，[src/ops/op_common/selector/selector_engine.cc:45-54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L45-L54)（这段代码用 `ENGINE_PREFIX_MAP` 逆序遍历实现长前缀优先匹配）：

```cpp
OpExecuteConfig SelectorEngine::GetEngineByAlgName(const std::string& algName)
{
    // 逆序遍历: 长前缀优先(CcuSched > CcuMS)
    for (auto it = ENGINE_PREFIX_MAP.rbegin(); it != ENGINE_PREFIX_MAP.rend(); ++it) {
        if (algName.rfind(it->first, 0) == 0) {
            return it->second;
        }
    }
    return OpExecuteConfig::AICPU_TS;  // 无前缀匹配时默认 AICPU_TS
}
```

`algName.rfind(prefix, 0) == 0` 是 C++ 里判断「字符串以 prefix 开头」的惯用写法；配合 `std::map` 的字典序 + 逆序遍历，保证 `CcuSched`（长）先于 `CcuMS`（短）被尝试。

另外，`op_common.cc` 中还有两个复用这一判定的入口（不属于新选择器）：`IsHostDpu`（限 910B 设备，[op_common.cc:3586-3633](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3586-L3633)）与 `IsBarrierHostDpu`（不限设备，供 950 Barrier 新流程使用，[op_common.cc:3637-3672](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3637-L3672)），两者都临时构造一个 `TopoInfoWithNetLayerDetails` 并调用底层 `CheckHostDPUOnly` 完成判定——说明 hostDpuOnly 已经是一个被多条业务线共享的拓扑特征。

#### 4.3.4 代码实践

**实践目标**：把「字段声明 → 序列化 → 判定 → 消费」四个环节串成一条完整的阅读路径，理解一个新拓扑特征位是如何贯穿下去的。

**操作步骤**：

1. 打开 [alg_param.h:216-339](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L216-L339)，在 `Serialize()` 与 `DeSerialize()` 里找到 `hostDpuOnly` 的写入行（L265）与读出行（L317），确认两者在各自函数中的字段顺序一致。
2. 打开 [topo_host.cc:786-865](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_host.cc#L786-L865)，回答：一个 2 台服务器、最外层 CLOS 全连通、但其中一个端点位于 device 侧的集群，`hostDpuOnly` 是 true 还是 false？
3. 打开 [selector_engine.cc:45-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L45-L75)，对照 `ENGINE_PREFIX_MAP` 手工推演：`GetEngineByAlgName("CcuSchedAllReduceSoleMesh")` 逆序遍历时先命中哪个 key？若用正序遍历会误命中哪个 key？

**需要观察的现象**：`ENGINE_PREFIX_MAP` 在 `std::map` 中的实际存储顺序（字典序：Aicpu < Aiv < CcuMS < CcuSched < Dpu），以及逆序遍历如何让 `CcuSched` 先于 `CcuMS` 被匹配。

**预期结果**：

- 步骤 2：false——只要最外层存在 device 侧端点，函数在遍历 Endpoint 时直接 `return HCCL_SUCCESS`，`hostDpuOnly` 保持初始值 false。
- 步骤 3：逆序先命中 `"CcuSched"`，返回 `CCU_SCHED`；若正序遍历，`"CcuMS"` 不是 `"CcuSchedAllReduceSoleMesh"` 的前缀（`CcuM` ≠ `CcuS`），本例不会误命中，但对于以 `"CcuMS"` 为前缀的名字（如 `CcuMSAllReduceSoleMesh`），正序和逆序结果一致——逆序遍历是针对「key 互为前缀」场景（如假想的前缀 `Ccu` 与 `CcuMS`）的通用防御写法。

> 待本地验证：`GetEngineByAlgName` 的推演可直接对照代码与映射表核对；真实集群上 hostDpuOnly 的取值需在多机环境观察 `HCCL_INFO` 日志（"Using host dpu trans." / "Not using hostdpu because ..."）确认。

#### 4.3.5 小练习与答案

**练习 1**：`hostDpuOnly = true` 时，新选择器还会比较 AICPU / AIV / CCU 引擎算法的代价吗？

> **答案**：不会。`SelectorEngine::GetEnginePriority` 在 `hostDpuOnly == true` 时直接返回 `{HOSTCPU}`（[selector_engine.cc:71-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L71-L75)），候选引擎只剩 Host DPU 一个——因为物理上框间只有 Host DPU 一条通路，其他引擎的算法根本无法执行，比较代价没有意义。

**练习 2**：如果给 `TopoInfoWithNetLayerDetails` 新增一个字段却忘了在 `DeSerialize()` 里补读取，会发生什么？

> **答案**：写入与读取的字段错位：`Serialize` 多写了一个字段，而 `DeSerialize` 从错的偏移开始读，之后所有字段全部错位，反序列化出错误的拓扑信息（且往往不报错，属于静默数据损坏）。这就是为什么手写序列化必须保证 `Serialize` 与 `DeSerialize` 逐字段、同顺序对齐——本轮 `hostDpuOnly` 就是两边同步新增的（写 L265 / 读 L317）。

**练习 3**：`ENGINE_PREFIX_MAP` 为什么用 `std::map`（有序）而不是 `std::unordered_map`？

> **答案**：因为匹配依赖 key 的字典序：逆序遍历有序 map 可实现「长前缀优先」（注释明确写了 `CcuSched > CcuMS`），确保带公共前缀的算法名被最具体的前缀匹配。`unordered_map` 的遍历顺序不确定，无法保证这一语义。

---

### 4.4 param_check 与 RPT_INPUT_ERR / CHK_PTR_NULL 校验宏

#### 4.4.1 概念说明

装配之前，HCCL 必须先确认用户传进来的参数是合法的——空指针、越界 count、不支持的数据类型都必须尽早拦截。这一节讲清楚三件事：

1. **校验宏**（`CHK_RET` / `CHK_PTR_NULL` / `CHK_PRT_RET`）：控制「检查 → 失败则立即返回」的流程。
2. **结构化错误上报**（`RPT_INPUT_ERR` + 错误码 `EI0003`）：失败时不仅要返回错误码，还要向错误管理器上报一条结构化诊断信息。
3. **两套并存的校验函数**：`op_common.cc` 的 `CheckCount` / `CheckDataType` / `CheckReduceOp`（AllReduce 入口实际调用）与 `param_check.cc` 的 `HcomCheck*` 系列（按 tag/count/dataType/group/stream 组合的可复用校验）。

一个贯穿始终的原则是 **「廉价优先、尽早失败」**（fail fast）：最便宜的检查（空指针）放在最前面，昂贵的检查（需要查设备/通信域）放在后面；任何一项失败就立即 return，不继续往下走。这样既节省资源，也使错误现场最接近根因。

#### 4.4.2 核心流程

AllReduce 路径上的校验发生在 `AllReduceInitAndCheck` 与 `CheckAllReduceInputPara` 两处，整体顺序（承接 u2-l2，这里展开校验细节）：

```
AllReduceInitAndCheck
 ├─ InitEnvConfig()                      // 环境变量解析（u4-l3）
 ├─ CheckAllReduceInputPara(...)         // 指针非空：stream/comm/sendBuf/recvBuf
 │     └─ RPT_INPUT_ERR(...) + CHK_PTR_NULL(...)
 ├─ HcclGetRankSize / HcclGetRankId      // 查 rank 信息
 ├─ HcclGetCommName → sprintf_s tag      // 生成 tag
 ├─ HcclCheckTag(param.tag)              // tag 长度校验
 ├─ HcomCheckUserRank(rankSize, userRank)// rank 范围校验
 ├─ CheckCount(count)                    // count 上界校验
 ├─ CheckDataType(dataType, true)        // 数据类型校验（needReduce=true）
 └─ CheckReduceOp(dataType, op)          // 归约算子与数据类型组合校验
```

每一行校验都用 `CHK_RET(...)` 包裹，失败即 return。

#### 4.4.3 源码精读

**(1) 校验宏** 定义在 [src/common/log.h:173-227](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/log.h#L173-L227)。

`CHK_RET` —— 检查一个返回 `HcclResult` 的调用，失败则打印调用踪迹并向上 propagate 错误码：

```cpp
#define CHK_RET(call)                                       \
    do {                                                    \
        HcclResult hcclRet = call;                          \
        if (UNLIKELY(hcclRet != HCCL_SUCCESS)) {            \
            if (hcclRet == HCCL_E_AGAIN) {                  \
                HCCL_WARNING("[%s]call trace: hcclRet -> %d", __func__, hcclRet); \
            } else {                                        \
                HCCL_ERROR("[%s]call trace: hcclRet -> %d", __func__, hcclRet);   \
            }                                               \
            return hcclRet;                                 \
        }                                                   \
    } while (0)
```

要点：`HCCL_E_AGAIN`（可重试）只打 WARNING，其余错误打 ERROR；最终都 `return hcclRet` 把错误向上抛。这是 HCCL 全仓统一的「错误传播」机制——内部函数几乎不直接处理错误，而是用 `CHK_RET` 一路抛到入口。

`CHK_PTR_NULL` —— 检查指针非空，空则返回 `HCCL_E_PTR`（[log.h:173](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/log.h#L173)），是 `CheckAllReduceInputPara` 里最常用的检查。

`CHK_PRT_RET(result, exeLog, retCode)` —— 条件分支：若 `result` 为真，执行 `exeLog`（通常是一条日志）后返回 `retCode`。它比 `CHK_RET` 更灵活，可自定义返回码与日志，常用于「业务早退」（如 `count == 0` 返回成功）。

**(2) 结构化错误上报** `RPT_INPUT_ERR` 定义在 [src/common/adapter_error_manager_pub.h:23-28](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/adapter_error_manager_pub.h#L23-L28)：

```cpp
#define RPT_INPUT_ERR(result, error_code, key, value)     \
    do {                                                  \
        if (UNLIKELY(result) && RptInputErr != nullptr) { \
            RptInputErr(error_code, key, value);          \
        }                                                 \
    } while (0)
```

它把一条「错误码 + 键值对」诊断信息上报给错误管理器（`RptInputErr` 函数指针）。`key` 通常是 `{"ccl_op", "value", "parameter", "expect"}` 四元标题，`value` 是 `{"算子名", "实际值", "参数名", "期望值"}`。这种结构化上报使得错误日志可以被工具自动解析，而不仅仅是一行文本。

AllReduce 的指针校验集中在这段，[all_reduce_op.cc:135-157](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L135-L157)：

```cpp
HcclResult
CheckAllReduceInputPara(const HcclComm comm, const void* sendBuf, const void* recvBuf, const aclrtStream stream)
{
    RPT_INPUT_ERR(
        stream == nullptr, "EI0003", std::vector<std::string>({"ccl_op", "value", "parameter", "expect"}),
        std::vector<std::string>({"HcclAllReduce", "nullptr", "stream", "non-null pointer"}));
    CHK_PTR_NULL(stream);
    // ... comm / sendBuf / recvBuf 同样模式
    return HCCL_SUCCESS;
}
```

注意这里 **`RPT_INPUT_ERR` 与 `CHK_PTR_NULL` 成对出现** 的固定模式：先用 `RPT_INPUT_ERR` 上报结构化诊断（错误码 `EI0003` 表示输入参数错误），再用 `CHK_PTR_NULL` 真正执行「空则返回」。这是全仓统一的「上报 + 拦截」二段式写法。

**(3) 业务校验函数** —— `CheckCount` / `CheckDataType` / `CheckReduceOp` 定义在 [src/ops/op_common/op_common.cc:2882-2976](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2882-L2976)，由 `AllReduceInitAndCheck`（[all_reduce_op.cc:128-130](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L128-L130)）调用。三者都遵循「判定 → RPT_INPUT_ERR 上报 → HCCL_ERROR 打日志 → 返回错误码」的范式：

```cpp
HcclResult CheckCount(const u64 count)
{
    if (UNLIKELY(count > SYS_MAX_COUNT)) {
        HCCL_ERROR("[Check][Count]errNo[0x%016llx] count[%llu] is invalid(bigger than MAX count[%llu])", ...);
        return HCCL_E_PARA;
    }
    return HCCL_SUCCESS;
}
```

`CheckDataType(dataType, /*needReduce=*/true)`（[op_common.cc:2893-2928](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2893-L2928)）有一个重要参数 `needReduce`：归约类算子（AllReduce）传入 `true`，会额外禁止无意义归约的类型（如 `UINT8` / `INT128` / FP8 系列）；非归约算子（如 AllGather）传入 `false`，允许更宽的类型集（配套的支持列表见 [GetSupportDataType, op_common.cc:2930-2953](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2930-L2953)）。这解释了为什么 AllReduce 不支持 `FP8` 而 AllGather 可以。

`CheckReduceOp(dataType, op)`（[op_common.cc:2955-2976](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2955-L2976)）专门约束 `HCCL_REDUCE_PROD`（连乘）：连乘只支持 INT8/INT32/INT64/UINT64/FP16/FP32/FP64，否则报错。

**(4) 可复用的 HcomCheck\* 系列** 定义在 [src/common/param_check.cc:18-153](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/param_check.cc#L18-L153)（`param_check.h` 声明见 [param_check.h:18-37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/param_check.h#L18-L37)）。它提供按维度组合的校验重载，例如：

```cpp
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType,
                            const char* group, const void* stream);  // 五参数版
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType,
                            const void* stream);                      // 四参数版
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType); // 三参数版
```

这套 `HcomCheck*` 工具（还有 `HcomCheckTag` / `HcomCheckCount` / `HcomCheckDataType` / `HcomCheckReductionOp` / `HcomCheckGroupName` / `HcomCheckUserRank`）是 **跨算子复用** 的校验库，供不同入口按需组合调用。它和 `op_common.cc` 的 `Check*` 系列功能相近、都使用 `RPT_INPUT_ERR + EI0003` 模式，区别在于 `HcomCheck*` 更通用、按维度正交组合；而 `Check*` 是新一代入口（AllReduce 等新流程）直接调用的版本，额外承担 `needReduce` 等业务语义校验。两者并存，读者遇到时按调用现场理解即可。

`HcomCheckCount`（[param_check.cc:80-89](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/param_check.cc#L80-L89)）与 `CheckCount` 逻辑一致（都判 `count > SYS_MAX_COUNT`）；`HcomCheckDataType`（[param_check.cc:91-100](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/param_check.cc#L91-L100)）则通过查 `HCOM_DATA_TYPE_STR_MAP` 判枚举合法性。

#### 4.4.4 代码实践

**实践目标**：在 AllReduce 入口代码里辨认三类「校验/上报」写法，并为一个假想新算子补一组等价代码。

**操作步骤**：

1. 打开 [src/ops/all_reduce/all_reduce_op.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc)，把 `HcclAllReduce`、`AllReduceInitAndCheck`、`CheckAllReduceInputPara` 三段里的 `HCCL_INFO` / `CHK_RET` / `CHK_PTR_NULL` / `CHK_PRT_RET` / `RPT_INPUT_ERR` 调用各收集一处。
2. 把它们归类：哪几个属于「日志」、哪几个属于「返回值校验（向上抛错）」、哪几个属于「结构化错误上报」。
3. 假设你要新增一个算子 `HcclFoo`，参照 `CheckAllReduceInputPara` 的模式，写出它的指针校验骨架（示例代码，非项目原有）：

```cpp
// 示例代码：为假想算子 HcclFoo 写的指针校验，仿照 CheckAllReduceInputPara
HcclResult CheckFooInputPara(const HcclComm comm, const void* inBuf,
                             const void* outBuf, const aclrtStream stream)
{
    RPT_INPUT_ERR(stream == nullptr, "EI0003",
        std::vector<std::string>({"ccl_op", "value", "parameter", "expect"}),
        std::vector<std::string>({"HcclFoo", "nullptr", "stream", "non-null pointer"}));
    CHK_PTR_NULL(stream);
    RPT_INPUT_ERR(comm == nullptr, "EI0003",
        std::vector<std::string>({"ccl_op", "value", "parameter", "expect"}),
        std::vector<std::string>({"HcclFoo", "nullptr", "comm", "non-null pointer"}));
    CHK_PTR_NULL(comm);
    return HCCL_SUCCESS;
}
```

**需要观察的现象**：每一个「拦截」动作（`CHK_PTR_NULL` / `CHK_RET`）前面是否都紧挨着一条对应的 `RPT_INPUT_ERR` 上报；函数体里校验顺序是否「廉价优先」。

**预期结果**：你会看到 AllReduce 的校验严格遵循「`RPT_INPUT_ERR` 上报 + `CHK_*` 拦截」二段式，且空指针等廉价检查排在查 rank/设备等昂贵操作之前。

> 待本地验证：可对照 grep 结果统计 `RPT_INPUT_ERR` 与 `CHK_PTR_NULL` 在同一段代码中出现的次数是否成对。

#### 4.4.5 小练习与答案

**练习 1**：`CHK_RET(f())` 和 `RPT_INPUT_ERR(...)` 各自的职责是什么？为什么常常成对出现？

> **答案**：`CHK_RET` 负责「调用 `f()`，失败则向上传播错误码」（控制流）；`RPT_INPUT_ERR` 负责「向错误管理器上报一条结构化诊断信息（EI0003 + 键值对）」（诊断信息）。成对出现是为了在拦截非法输入的同时，留下可被工具解析的、带上下文的错误现场。

**练习 2**：`CheckDataType(dataType, true)` 中的 `true` 代表什么？为什么 AllReduce 传 `true` 而 AllGather 传 `false`？

> **答案**：`needReduce=true` 表示该算子要做归约，因此额外禁止 `UINT8` / FP8 等无意义归约类型。AllReduce 要做 SUM 归约，所以传 `true`；AllGather 只拼接不归约，类型集更宽，所以传 `false`。

**练习 3**：`AllReduceInitAndCheck` 里校验顺序是 `CheckAllReduceInputPara`（指针）→ `HcclGetRankSize/RankId`（查通信域）→ `CheckCount/DataType/ReduceOp`（业务校验）。为什么不把业务校验放在最前？

> **答案**：遵循「廉价优先、尽早失败」。指针判空是几纳秒的操作，而 `HcclGetRankSize` 等需要跨进程/查通信域，代价更高。先用廉价检查拦掉明显的非法输入，再付出昂贵查询的代价；且任何一步失败都立即 return，使错误现场最接近根因。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，完成一次「从 API 调用到 OpParam 装配再到校验拦截」的完整推演，并解释一个真实的拓扑特征如何影响引擎选择。

**背景调用**（单进程 2 卡，rank0 调用）：

```cpp
void* sendBuf = nullptr;  // 故意置空
HcclAllReduce(sendBuf, recvBuf, 2048, HCCL_DATA_TYPE_FP16, HCCL_REDUCE_PROD, comm, stream);
```

请按顺序回答：

1. **校验阶段**：这次调用会在哪一步被拦截？走的是哪个函数、哪个宏？上报的错误码是什么？提示：注意 `sendBuf == nullptr` 与 `CheckReduceOp` 对 `REDUCE_PROD + FP16` 的判定——哪一个先发生？
2. **装配阶段（假设指针都合法）**：若 `sendBuf` 合法，`FillAllReduceOpParam` 执行后 `inputSize` / `outputSize` / `DataDes.count` / `DataDes.dataType` / `opType` 分别是多少？（FP16 = 2 字节）
3. **结构理解**：装配完成后，`param.engine` 是什么值？它会在随后的哪一步被赋值（接 u2-l4）？
4. **拓扑特征（新增）**：假设该通信域部署在 2 台服务器上、最外层为全连通 CLOS、且所有端点都挂在 Host 侧 DPU 上（`hostDpuOnly = true`）。若走新选择器路径（u8-l1），`GetEnginePriority` 会返回什么？这意味着哪类引擎不可用？算法名会带哪个前缀？

**参考答案**：

1. `sendBuf == nullptr` 会在 **`CheckAllReduceInputPara`**（[all_reduce_op.cc:147-150](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/all_reduce_op.cc#L147-L150)）被拦截：先 `RPT_INPUT_ERR` 上报 `EI0003`（ccl_op=HcclAllReduce, parameter=sendBuf, expect=non-null pointer），再 `CHK_PTR_NULL(sendBuf)` 返回 `HCCL_E_PTR`。这一步发生在 `AllReduceInitAndCheck` 的最前面，**早于** `CheckReduceOp`，所以 `REDUCE_PROD + FP16` 的组合问题根本不会被检查到（实际上 FP16 在 `CheckReduceOp` 的 prod 支持列表里，本就合法，但即便非法也轮不到它触发）。
2. FP16 = 2 字节，所以 `inputSize = outputSize = 2048 × 2 = 4096` 字节；`DataDes.count = 2048`；`DataDes.dataType = HCCL_DATA_TYPE_FP16`；`opType = HCCL_CMD_ALLREDUCE`。
3. `param.engine` 仍是默认的 `COMM_ENGINE_RESERVED`；它会在 `AllReduceOutPlaceCommon` 中调用 `HcclGetOpExpansionMode(comm, param)` 时被赋值（u2-l4 详解）。
4. `GetEnginePriority` 直接返回 `{OpExecuteConfig::HOSTCPU}`（[selector_engine.cc:71-75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L71-L75)）：AICPU / AIV / CCU 引擎的算法都不参与代价比较，只有 Host DPU 引擎可用；选出的算法名会带 `Dpu` 前缀（对照 `ENGINE_PREFIX_MAP`，`"Dpu"` 映射到 `OpExecuteConfig::HOSTCPU`）。

## 6. 本讲小结

- **OpParam 是中央数据结构**：定义在 [alg_param.h:576-662](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L576-L662)，是「不申请 ctx、每次调用单独构造」的参数容器，贯穿入口 → Selector → Executor → Template 全链路。
- **DataDes 是 union**：由 `opType` 决定当前有效的数据描述成员（AllReduce 用 `DataDes.count/dataType`）；字段默认值统一为「无效值」（RESERVED / INVALID）。
- **三个旋钮分阶段填充**：`opMode` 在入口确定，`deviceType` 在装配时查询，`engine` 在 `HcclGetOpExpansionMode` 时决定——装配阶段不决定引擎。
- **`FillAllReduceOpParam` 做字节数换算**：`perDataSize = DATATYPE_SIZE_TABLE[dataType]`，`inputSize = outputSize = count × perDataSize`（AllReduce 输入输出等长）。
- **本轮新增 `TopoInfoWithNetLayerDetails::hostDpuOnly`**：由 `CalcHostDPUOnly` 依据「多服务器 + 多层拓扑 + 最外层 CLOS 全连通且无 device 端点」判定，序列化/反序列化同步对齐；为 true 时新选择器只保留 HOSTCPU（DPU）引擎。
- **本轮新增 `ENGINE_PREFIX_MAP` / `ENGINE_STR_MAP`**：算法名前缀 → `OpExecuteConfig` 引擎的反查表（逆序遍历实现长前缀优先）与引擎 → 日志字符串表，服务于代价模型路径的引擎推断与缓存键。
- **校验遵循「廉价优先、尽早失败」**：先用 `CHK_PTR_NULL` / `RPT_INPUT_ERR(EI0003)` 拦截空指针，再查 rank/设备，最后做 `CheckCount` / `CheckDataType(needReduce)` / `CheckReduceOp` 业务校验。
- **两套校验工具并存**：`op_common.cc` 的 `Check*`（AllReduce 新流程直接调用，带 `needReduce` 业务语义）与 `param_check.cc` 的 `HcomCheck*`（跨算子按维度正交组合复用），都使用 `RPT_INPUT_ERR + EI0003` 结构化上报。

## 7. 下一步学习建议

本讲止步于「OpParam 装配完成、`engine` 仍是 RESERVED，并认识了拓扑特征位与引擎前缀映射」。下一讲 **u2-l4 通信引擎选择与快速路径** 将接着讲 `AllReduceOutPlaceCommon` 中 `HcclGetOpExpansionMode` 如何填 `param.engine`，以及 CCU FastLaunch、AIV Cache Replay、单卡 `SingleRankProc` 等快速路径的触发条件。

继续深入的建议：

- 想看 `hostDpuOnly` 与 `ENGINE_PREFIX_MAP` 被谁消费，可跳到 **u8-l1（新选择器 SelectorEngine 与双路径分发）**，观察 `GetEnginePriority` → `InitCostModel` → `CostTableGen` → `SelectMinCost` 的完整流程；`cost_table.cc` 中还有多条以 `!topo->hostDpuOnly` 为前提的算法过滤规则。
- 想系统理解拓扑特征位家族（`Level0Nhr`、`is2DieFullMesh`、`netLayerDetails` 等），可读 **u3-l3（拓扑适配与拓扑信息 Topo）**，`CalcHostDPUOnly` 正是特征位计算链（`topo_host.cc`）的新成员。
- 想看 OpParam 在更长链路里如何被读写，可跳到 **u3-l1（op_common 四大组件总览）**，观察 Selector 往 `param.algName` 写值、Executor 读 `param.algName` 的过程。
- 想理解 `CheckDataType(needReduce)` 背后的类型支持矩阵全貌，可读 [op_common.cc:2882-2976](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2882-L2976) 的 `CheckCount` / `CheckDataType` / `GetSupportDataType` / `CheckReduceOp`。
