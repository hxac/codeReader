# OpParam 参数结构与入参校验

## 1. 本讲目标

上一讲（u2-l2）我们跟读了 `HcclAllReduce` 的入口：兼容分发 → `AllReduceInitAndCheck`（环境变量解析 + 入参校验）→ `AllReduceOutPlace` → `AllReduceOutPlaceCommon`。本讲继续往下钻一层，聚焦两件事：

1. **OpParam** —— 这是贯穿「算子入口 → Selector → Executor → Template」整条执行链路的「中央数据结构」。每一级代码都从它身上读取信息、往它身上写入结果。你要能看懂它有哪些字段、按什么逻辑组织。
2. **入参校验** —— 用户传进来的指针、count、数据类型、归约算子是怎么被校验的，校验用的 `CHK_RET` / `CHK_PTR_NULL` / `RPT_INPUT_ERR` 等宏分别做什么。

学完本讲，你应当能够：

- 说出 OpParam 的关键字段（`opType` / `engine` / `opMode` / `DataDes` / `inputPtr`·`outputPtr` / `tag` 等）及其作用；
- 读懂 `FillAllReduceOpParam` 如何把 API 入参装配进 OpParam；
- 区分 HCCL 中两套并存的校验工具（`op_common.cc` 的 `Check*` 与 `param_check.cc` 的 `HcomCheck*`），并掌握 `CHK_RET` / `CHK_PTR_NULL` / `RPT_INPUT_ERR` 校验宏的语义。

## 2. 前置知识

在阅读本讲前，你需要具备以下认知（来自前置讲义）：

- **算子的执行链路骨架**：`HcclAllReduce → AllReduceOutPlace → AllReduceOutPlaceCommon → Selector() → HcclExecOp()`（u2-l2）。
- **算子（op）/ 算法（alg）/ 引擎（engine）三个维度的区别**：engine 分 AICPU / AIV / CCU（u1-l2、u1-l3）。
- **API 参数模型**：所有算子共享 `sendBuf / recvBuf / count / dataType / op / comm / stream` 参数；`count` 是元素个数而非字节（u2-l1）。
- **tag 的作用**：形如 `"AllReduce_<commName>"`，作为同一通信域拓扑/资源的缓存键，使多次调用共享同一份 topoInfo（u2-l2）。

一个关键直觉：**OpParam 是「函数参数」到「执行状态」的转换层**。C 接口的参数是分散的、扁平的；进入 HCCL 内部后，这些参数被组装成一个结构体对象，随调用链一路传递、被各级代码读写。理解 OpParam，就是理解 HCCL 内部「数据是怎么流动的」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/inc/alg_param.h` | 定义 OpParam 及其内嵌的 DataDes 联合体、数据类型字节表 `DATATYPE_SIZE_TABLE`、众多配套结构体（TopoInfo、AlgResourceCtx 等）。 |
| `src/ops/all_reduce/all_reduce_op.cc` | `AllReduceInitAndCheck`（校验 + rank 信息 + tag）、`CheckAllReduceInputPara`（指针非空校验）、`FillAllReduceOpParam`（装配 OpParam）。 |
| `src/ops/op_common/op_common.cc` | `CheckCount` / `CheckDataType` / `CheckReduceOp` —— AllReduce 入口实际调用的新一代校验函数。 |
| `src/common/param_check.h` / `src/common/param_check.cc` | `HcomCheck*` 系列可复用校验工具（tag/count/dataType/group/stream）。 |
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

`opMode` 是 `OpMode` 枚举（[alg_param.h:138](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L138)）：

```cpp
enum class OpMode { OFFLOAD = 0, OPBASE = 1, ACLGRAPH = 2 };
```

- `OPBASE`：单算子模式（用户直接调用 `HcclAllReduce`）。
- `OFFLOAD`：图模式执行阶段（资源由 GE 预分配，见 u7-l2）。
- `ACLGRAPH`：aclgraph 捕获模式。

#### 4.1.3 源码精读

OpParam 的核心定义在 [src/ops/op_common/inc/alg_param.h:559-645](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L559-L645)。下面是关键片段（已删减注释与部分字段）：

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

数据类型到字节数的映射由 `DATATYPE_SIZE_TABLE` 提供，定义在 [src/ops/op_common/inc/alg_param.h:43-61](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L43-L61)：

```cpp
constexpr uint32_t DATATYPE_SIZE_TABLE[HCCL_DATA_TYPE_RESERVED]
    = {sizeof(int8_t), sizeof(int16_t), sizeof(int32_t), 2,
       sizeof(float),  sizeof(int64_t),  sizeof(uint64_t), /* ... */ };
```

它是一个以 `HcclDataType` 枚举值为下标的静态查找表，`FillAllReduceOpParam` 正是用它把「元素个数 count」换算成「字节数 size」。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，建立「`opType` 决定读 union 的哪个成员」的直觉。

**操作步骤**：

1. 打开 [src/ops/op_common/inc/alg_param.h:590-625](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L590-L625)，列出 union 中的全部 6 个 struct 成员名。
2. 用 `Grep` 搜索每个成员名（如 `all2AllDataDes`、`vDataDes`、`batchSendRecvDataDes`），看它们分别在哪个算子的 `Fill*OpParam` 里被赋值。
3. 对比 `opType` 字段：搜索 `param.opType = HcclCMDType::` 出现的地方，确认每个算子设置了哪个 `HcclCMDType`。

**需要观察的现象**：每种数据描述形态只被一种 `HcclCMDType` 使用，二者一一对应（或一组对应）。

**预期结果**：例如 AlltoAll 系列算子设置 `opType = HCCL_CMD_ALLTOALL` 并写 `all2AllDataDes` / `all2AllVDataDes`，而 AllReduce 设置 `HCCL_CMD_ALLREDUCE` 并写 `DataDes`。

> 待本地验证：不同算子是否严格遵循「一种 opType 对应一种 union 成员」的约定，可借助 grep 的命中分布自行确认。

#### 4.1.5 小练习与答案

**练习 1**：OpParam 注释写「不申请 ctx」，那 device 侧资源上下文存在哪个字段里？

> **答案**：存在 `resCtx` 指针字段（[alg_param.h:634](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L634)）。OpParam 自身只是参数容器，真正的 device 资源由后续阶段申请后挂到 `resCtx` 上。

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

装配函数定义在 [src/ops/all_reduce/all_reduce_op.cc:159-187](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L159-L187)：

```cpp
HcclResult FillAllReduceOpParam(
    void* sendBuf, void* recvBuf, uint64_t count, HcclDataType dataType, HcclReduceOp op,
    const HcclComm comm, aclrtStream stream, OpMode opMode, OpParam& param)
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

它的调用点在 [all_reduce_op.cc:195](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L195)，位于 `AllReduceOutPlaceCommon` 最开头：

```cpp
HcclResult AllReduceOutPlaceCommon(/* ... */, OpParam& param)
{
    HCCL_INFO("Start to execute AllReduceOutPlace");
    CHK_RET(FillAllReduceOpParam(sendBuf, recvBuf, count, dataType, op, comm, stream, opMode, param));
    CHK_RET(HcclGetOpExpansionMode(comm, param));   // 这里才填 param.engine
    // ... CCU FastLaunch / AIV Cache / 单卡 / Selector / HcclExecOp
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

1. 查 `DATATYPE_SIZE_TABLE`（[alg_param.h:43-61](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L43-L61)）得 FP32（`sizeof(float)`）= 4 字节。
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

> **答案**：不会。`HcclAllReduce` 入口在调用 `AllReduceInitAndCheck`（进而装配）之前，有早退分支 `CHK_PRT_RET(count == 0, ..., HCCL_SUCCESS)`（[all_reduce_op.cc:37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L37)）。count 为 0 直接返回成功，不进入装配与执行（u2-l2 已讲过这条早退）。

**练习 2**：为什么 `inputSize = outputSize`？哪种算子不满足这个等式？

> **答案**：因为 AllReduce 输入输出等长（u1-l5）。不满足的是 **AllGather**——它的 `recvBuf = sendCount × R`（只拼接不归约），输出是输入的 R 倍；以及 **ReduceScatter**——输出是输入的 1/R。所以它们的 `Fill*OpParam` 里 inputSize 与 outputSize 的计算公式不同。

**练习 3**：`FillAllReduceOpParam` 里 `param.reduceType = op;` 写了两次，是否有副作用？

> **答案**：无副作用。两次都赋同一个值（用户传入的 `op`），最终结果一致，只是代码上的冗余。可视为可清理项，但行为正确。

---

### 4.3 param_check 与 RPT_INPUT_ERR / CHK_PTR_NULL 校验宏

#### 4.3.1 概念说明

装配之前，HCCL 必须先确认用户传进来的参数是合法的——空指针、越界 count、不支持的数据类型都必须尽早拦截。这一节讲清楚三件事：

1. **校验宏**（`CHK_RET` / `CHK_PTR_NULL` / `CHK_PRT_RET`）：控制「检查 → 失败则立即返回」的流程。
2. **结构化错误上报**（`RPT_INPUT_ERR` + 错误码 `EI0003`）：失败时不仅要返回错误码，还要向错误管理器上报一条结构化诊断信息。
3. **两套并存的校验函数**：`op_common.cc` 的 `CheckCount` / `CheckDataType` / `CheckReduceOp`（AllReduce 入口实际调用）与 `param_check.cc` 的 `HcomCheck*` 系列（按 tag/count/dataType/group/stream 组合的可复用校验）。

一个贯穿始终的原则是 **「廉价优先、尽早失败」**（fail fast）：最便宜的检查（空指针）放在最前面，昂贵的检查（需要查设备/通信域）放在后面；任何一项失败就立即 return，不继续往下走。这样既节省资源，也使错误现场最接近根因。

#### 4.3.2 核心流程

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

#### 4.3.3 源码精读

**(1) 校验宏** 定义在 [src/common/log.h:173-227](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L173-L227)。

`CHK_RET` —— 检查一个返回 `HcclResult` 的调用，失败则打印调用踪迹并向上 propagate 错误码（[log.h:201-212](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L201-L212)）：

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

`CHK_PTR_NULL` —— 检查指针非空，空则返回 `HCCL_E_PTR`（[log.h:173](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L173)），是 `CheckAllReduceInputPara` 里最常用的检查。

`CHK_PRT_RET(result, exeLog, retCode)` —— 条件分支：若 `result` 为真，执行 `exeLog`（通常是一条日志）后返回 `retCode`。它比 `CHK_RET` 更灵活，可自定义返回码与日志，常用于「业务早退」（如 `count == 0` 返回成功）。

**(2) 结构化错误上报** `RPT_INPUT_ERR` 定义在 [src/common/adapter_error_manager_pub.h:23-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_error_manager_pub.h#L23-L28)：

```cpp
#define RPT_INPUT_ERR(result, error_code, key, value)     \
    do {                                                  \
        if (UNLIKELY(result) && RptInputErr != nullptr) { \
            RptInputErr(error_code, key, value);          \
        }                                                 \
    } while (0)
```

它把一条「错误码 + 键值对」诊断信息上报给错误管理器（`RptInputErr` 函数指针）。`key` 通常是 `{"ccl_op", "value", "parameter", "expect"}` 四元标题，`value` 是 `{"算子名", "实际值", "参数名", "期望值"}`。这种结构化上报使得错误日志可以被工具自动解析，而不仅仅是一行文本。

AllReduce 的指针校验集中在这段，[all_reduce_op.cc:135-157](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L135-L157)：

```cpp
HcclResult CheckAllReduceInputPara(const HcclComm comm, const void* sendBuf,
                                   const void* recvBuf, const aclrtStream stream)
{
    RPT_INPUT_ERR(stream == nullptr, "EI0003",
        std::vector<std::string>({"ccl_op", "value", "parameter", "expect"}),
        std::vector<std::string>({"HcclAllReduce", "nullptr", "stream", "non-null pointer"}));
    CHK_PTR_NULL(stream);
    RPT_INPUT_ERR(comm == nullptr, "EI0003", /* ... */ {"HcclAllReduce", "nullptr", "comm", "non-null pointer"});
    CHK_PTR_NULL(comm);
    // ... 同样模式校验 sendBuf / recvBuf
    return HCCL_SUCCESS;
}
```

注意这里 **`RPT_INPUT_ERR` 与 `CHK_PTR_NULL` 成对出现** 的固定模式：先用 `RPT_INPUT_ERR` 上报结构化诊断（错误码 `EI0003` 表示输入参数错误），再用 `CHK_PTR_NULL` 真正执行「空则返回」。这是全仓统一的「上报 + 拦截」二段式写法。

**(3) 业务校验函数** —— `CheckCount` / `CheckDataType` / `CheckReduceOp` 定义在 [src/ops/op_common/op_common.cc:2872-2966](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2872-L2966)，由 `AllReduceInitAndCheck` 调用。三者都遵循「判定 → RPT_INPUT_ERR 上报 → HCCL_ERROR 打日志 → 返回错误码」的范式：

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

`CheckDataType(dataType, /*needReduce=*/true)`（[op_common.cc:2883-2918](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2883-L2918)）有一个重要参数 `needReduce`：归约类算子（AllReduce）传入 `true`，会额外禁止无意义归约的类型（如 `UINT8` / `FP8` 系列——这些类型做 SUM 没有定义良好的语义）；非归约算子（如 AllGather）传入 `false`，允许更宽的类型集。这解释了为什么 AllReduce 不支持 `FP8` 而 AllGather 可以。

`CheckReduceOp(dataType, op)`（[op_common.cc:2945-2966](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2945-L2966)）专门约束 `HCCL_REDUCE_PROD`（连乘）：连乘只支持 INT8/INT32/INT64/UINT64/FP16/FP32/FP64，否则报错。

**(4) 可复用的 HcomCheck\* 系列** 定义在 [src/common/param_check.cc:18-153](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.cc#L18-L153)（`param_check.h` 声明见 [param_check.h:18-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.h#L18-L37)）。它提供按维度组合的校验重载，例如：

```cpp
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType,
                            const char* group, const void* stream);  // 五参数版
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType,
                            const void* stream);                      // 四参数版
HcclResult HcomCheckOpParam(const char* tag, const u64 count, const HcclDataType dataType); // 三参数版
```

这套 `HcomCheck*` 工具（还有 `HcomCheckTag` / `HcomCheckCount` / `HcomCheckDataType` / `HcomCheckReductionOp` / `HcomCheckGroupName` / `HcomCheckUserRank`）是 **跨算子复用** 的校验库，供不同入口按需组合调用。它和 `op_common.cc` 的 `Check*` 系列功能相近、都使用 `RPT_INPUT_ERR + EI0003` 模式，区别在于 `HcomCheck*` 更通用、按维度正交组合；而 `Check*` 是新一代入口（AllReduce 等新流程）直接调用的版本，额外承担 `needReduce` 等业务语义校验。两者并存，读者遇到时按调用现场理解即可。

`HcomCheckCount`（[param_check.cc:80-89](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.cc#L80-L89)）与 `CheckCount` 逻辑一致（都判 `count > SYS_MAX_COUNT`）；`HcomCheckDataType`（[param_check.cc:91-100](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.cc#L91-L100)）则通过查 `HCOM_DATA_TYPE_STR_MAP` 判枚举合法性。

#### 4.3.4 代码实践

**实践目标**：在 AllReduce 入口代码里辨认三类「校验/上报」写法，并为一个假想新算子补一组等价代码。

**操作步骤**：

1. 打开 [src/ops/all_reduce/all_reduce_op.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc)，把 `HcclAllReduce`、`AllReduceInitAndCheck`、`CheckAllReduceInputPara` 三段里的 `HCCL_INFO` / `CHK_RET` / `CHK_PTR_NULL` / `CHK_PRT_RET` / `RPT_INPUT_ERR` 调用各收集一处。
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

#### 4.3.5 小练习与答案

**练习 1**：`CHK_RET(f())` 和 `RPT_INPUT_ERR(...)` 各自的职责是什么？为什么常常成对出现？

> **答案**：`CHK_RET` 负责「调用 `f()`，失败则向上传播错误码」（控制流）；`RPT_INPUT_ERR` 负责「向错误管理器上报一条结构化诊断信息（EI0003 + 键值对）」（诊断信息）。成对出现是为了在拦截非法输入的同时，留下可被工具解析的、带上下文的错误现场。

**练习 2**：`CheckDataType(dataType, true)` 中的 `true` 代表什么？为什么 AllReduce 传 `true` 而 AllGather 传 `false`？

> **答案**：`needReduce=true` 表示该算子要做归约，因此额外禁止 `UINT8` / `FP8` 等无意义归约类型。AllReduce 要做 SUM 归约，所以传 `true`；AllGather 只拼接不归约，类型集更宽，所以传 `false`。

**练习 3**：`AllReduceInitAndCheck` 里校验顺序是 `CheckAllReduceInputPara`（指针）→ `HcclGetRankSize/RankId`（查通信域）→ `CheckCount/DataType/ReduceOp`（业务校验）。为什么不把业务校验放在最前？

> **答案**：遵循「廉价优先、尽早失败」。指针判空是几纳秒的操作，而 `HcclGetRankSize` 等需要跨进程/查通信域，代价更高。先用廉价检查拦掉明显的非法输入，再付出昂贵查询的代价；且任何一步失败都立即 return，使错误现场最接近根因。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，完成一次「从 API 调用到 OpParam 装配再到校验拦截」的完整推演，并解释一个真实的失败场景。

**背景调用**（单进程 2 卡，rank0 调用）：

```cpp
void* sendBuf = nullptr;  // 故意置空
HcclAllReduce(sendBuf, recvBuf, 2048, HCCL_DATA_TYPE_FP16, HCCL_REDUCE_PROD, comm, stream);
```

请按顺序回答：

1. **校验阶段**：这次调用会在哪一步被拦截？走的是哪个函数、哪个宏？上报的错误码是什么？提示：注意 `sendBuf == nullptr` 与 `CheckReduceOp` 对 `REDUCE_PROD + FP16` 的判定——哪一个先发生？
2. **装配阶段（假设指针都合法）**：若 `sendBuf` 合法，`FillAllReduceOpParam` 执行后 `inputSize` / `outputSize` / `DataDes.count` / `DataDes.dataType` / `opType` 分别是多少？（FP16 = 2 字节）
3. **结构理解**：装配完成后，`param.engine` 是什么值？它会在随后的哪一步被赋值（接 u2-l4）？

**参考答案**：

1. `sendBuf == nullptr` 会在 **`CheckAllReduceInputPara`**（[all_reduce_op.cc:147-150](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L147-L150)）被拦截：先 `RPT_INPUT_ERR` 上报 `EI0003`（ccl_op=HcclAllReduce, parameter=sendBuf, expect=non-null pointer），再 `CHK_PTR_NULL(sendBuf)` 返回 `HCCL_E_PTR`。这一步发生在 `AllReduceInitAndCheck` 的最前面，**早于** `CheckReduceOp`，所以 `REDUCE_PROD + FP16` 的组合问题根本不会被检查到（实际上 FP16 在 `CheckReduceOp` 的 prod 支持列表里，本就合法，但即便非法也轮不到它触发）。
2. FP16 = 2 字节，所以 `inputSize = outputSize = 2048 × 2 = 4096` 字节；`DataDes.count = 2048`；`DataDes.dataType = HCCL_DATA_TYPE_FP16`；`opType = HCCL_CMD_ALLREDUCE`。
3. `param.engine` 仍是默认的 `COMM_ENGINE_RESERVED`；它会在 `AllReduceOutPlaceCommon` 中调用 `HcclGetOpExpansionMode(comm, param)` 时被赋值（u2-l4 详解）。

## 6. 本讲小结

- **OpParam 是中央数据结构**：定义在 [alg_param.h:559-645](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L559-L645)，是「不申请 ctx、每次调用单独构造」的参数容器，贯穿入口 → Selector → Executor → Template 全链路。
- **DataDes 是 union**：由 `opType` 决定当前有效的数据描述成员（AllReduce 用 `DataDes.count/dataType`）；字段默认值统一为「无效值」（RESERVED / INVALID）。
- **三个旋钮分阶段填充**：`opMode` 在入口确定，`deviceType` 在装配时查询，`engine` 在 `HcclGetOpExpansionMode` 时决定——装配阶段不决定引擎。
- **`FillAllReduceOpParam` 做字节数换算**：`perDataSize = DATATYPE_SIZE_TABLE[dataType]`，`inputSize = outputSize = count × perDataSize`（AllReduce 输入输出等长）。
- **校验遵循「廉价优先、尽早失败」**：先用 `CHK_PTR_NULL` / `RPT_INPUT_ERR(EI0003)` 拦截空指针，再查 rank/设备，最后做 `CheckCount` / `CheckDataType(needReduce)` / `CheckReduceOp` 业务校验。
- **两套校验工具并存**：`op_common.cc` 的 `Check*`（AllReduce 新流程直接调用，带 `needReduce` 业务语义）与 `param_check.cc` 的 `HcomCheck*`（跨算子按维度正交组合复用），都使用 `RPT_INPUT_ERR + EI0003` 结构化上报。

## 7. 下一步学习建议

本讲止步于「OpParam 装配完成、`engine` 仍是 RESERVED」。下一讲 **u2-l4 通信引擎选择与快速路径** 将接着讲 `AllReduceOutPlaceCommon` 中 `HcclGetOpExpansionMode` 如何填 `param.engine`，以及 CCU FastLaunch、AIV Cache Replay、单卡 `SingleRankProc` 等快速路径的触发条件。

继续深入的建议：

- 想看 OpParam 在更长链路里如何被读写，可跳到 **u3-l1（op_common 四大组件总览）**，观察 Selector 往 `param.algName` 写值、Executor 读 `param.algName` 的过程。
- 想系统理解 `deviceType` 如何影响特性可用性，可读 **u4-l2（设备类型与能力识别）**。
- 想理解 `CheckDataType(needReduce)` 背后的类型支持矩阵全貌，可读 [op_common.cc:2883-2943](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2883-L2943) 的 `CheckDataType` 与 `GetSupportDataType`。
