# Tiling 机制入门：核切分与 UB 切分

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Tiling（分块）在算子执行链路中的位置和职责：它在 Host 侧运行，决定 Device 侧 kernel 怎么切活干。
2. 掌握两级切分的计算方法：
   - **核切分（Core Tiling）**：把总元素数按 AI Core 数摊开，算出 `blockFactor`（每核处理量）与 `usedCoreNum`（实际用核数）。
   - **UB 切分（UB Tiling）**：受 Unified Buffer 容量约束，算出每轮循环搬进片上内存的元素数 `ubFactor`。
3. 掌握通过 `gert::TilingContext` 获取平台信息（UB 大小、AI Core 数）与输入 shape/dtype 的标准写法。
4. 理解 `SetBlockDim` 与 `SetTilingKey` 各自控制什么：前者决定起多少个核，后者决定 kernel 走哪条模板分支。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。前面几讲我们已经知道：一个标准算子工程里，`op_host` 负责 Host（CPU）侧的交付件，`op_kernel` 负责 AI Core（Device）侧的交付件。Tiling 就是连接两者的「施工调度单」。

- **为什么需要 Tiling**：AI Core 片上存储（Unified Buffer，简称 UB）容量有限（通常几百 KB），而一个张量可能有几百 MB，无法一次性搬进来算。必须把大张量切成小块（Tile），逐块「搬入 → 计算 → 搬出」。官方文档 [docs/zh/develop/aicore_develop_guide.md:107-113](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L107-L113) 对此有一句话定义。
- **AI Core 是多核的**：一颗 Atlas 芯片上有几十个 AI Core（矢量核 AIV），Tiling 的第一级切分就是决定「这堆元素分给几个核、每核多少」。
- **TilingData 是数据契约**：Host 侧 Tiling 函数把切分结果写进一个 POD 结构体（如 `AddExampleTilingData`），框架把它传到 Device 侧，kernel 入口用 `GET_TILING_DATA_WITH_STRUCT` 读出来。这个结构体在上一讲（u3-l1）已经见过，本讲详细讲解它是怎么被「算出来」的。
- **TilingKey 是分支选择器**：同一个算子可能有多套实现（比如 float 一套、int32 一套），Host 侧用 `SetTilingKey` 打一个标记，Device 侧据此选模板分支。
- **两个数学工具**（本讲反复用到）：
  - 向上取整除法 \(\lceil a/b \rceil\)（`CeilDiv`）：保证切分后「不漏元素」。
  - 向下对齐 \(\lfloor a/b \rfloor \cdot b\)（`FloorAlign`）：保证结果是对齐块的整数倍（UB 搬运以 32 字节为一块）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | 本讲主线：Tiling 全部计算逻辑与注册入口 |
| [examples/add_example/op_kernel/add_example_tiling_data.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h) | TilingData 结构体定义（Host 写、Device 读的契约） |
| [examples/add_example/op_kernel/add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h) | TilingKey 声明（schMode 模板参数） |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) | kernel 侧如何消费 totalNum/blockFactor/ubFactor |
| [examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp) | Tiling UT：给定输入断言切分结果 |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) | 官方 Tiling 交付件说明与模板骨架 |
| [common/inc/op_kernel/kernel_utils.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h) | `CeilDiv`/`FloorDiv` 的 kernel 侧同名实现（帮助理解语义） |
| [common/inc/op_kernel/platform.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/platform.h) | `GetUbBlockSize()` 返回 32：UB 对齐块的字节数 |

说明：`add_example_tiling.cpp` 中使用的 `Ops::Base::CeilDiv/FloorDiv/FloorAlign/GetUbBlockSize` 来自 CANN 包安装目录的头文件（该文件顶部 `#include "util/math_util.h"`、`#include "util/platform_util.h"`），不在本仓库内；本仓库 `common/inc/op_kernel/` 下有语义相同的 kernel 侧实现，可作为对照阅读。

## 4. 核心概念与源码讲解

### 4.1 Tiling 的职责与三个交付件

#### 4.1.1 概念说明

Tiling 函数是算子的「施工调度员」：它在 Host 侧被框架回调（aclnn 第一段 GetWorkspaceSize 阶段或 GE 图编译阶段），输入是这次调用的实际 shape/dtype 和当前硬件规格，输出是三样东西：

1. **TilingData 结构体**：切分参数（每核处理多少、每轮循环处理多少），会被拷贝到 Device 侧供 kernel 读取。
2. **BlockDim**：本次执行要起多少个 AI Core。
3. **TilingKey**：让 kernel 侧选择哪套实现分支。

官方开发指南说明 Tiling 一共需要三个交付件（[docs/zh/develop/aicore_develop_guide.md:115-122](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L115-L122)）：`*_tiling.cpp` 放 `op_host`（主切分逻辑），`*_tiling_key.h` 和 `*_tiling_data.h` 放 `op_kernel`（因为 kernel 侧也要 include 它们）。

#### 4.1.2 核心流程

```text
框架回调 AddExampleTilingFunc(context)
  ├─ 1. GetPlatformInfo   → ubSize, coreNum（硬件规格）
  ├─ 2. GetShapeAttrsInfo → totalIdx, dataType（本次调用规格）
  ├─ 3. GetWorkspaceSize  → workspace[0] = 0
  ├─ 4. 核切分：blockFactor = ⌈totalIdx / coreNum⌉
  │          usedCoreNum = ⌈totalIdx / blockFactor⌉
  ├─ 5. UB 切分：ubFactor = FloorAlign(FloorDiv(ubSize/TYPE_SIZE, BUFFER_NUM), 32字节块)
  ├─ 6. context->SetBlockDim(usedCoreNum)
  └─ 7. 按 dtype 设置 tilingKey（float→0, int32→1）→ context->SetTilingKey
```

#### 4.1.3 源码精读

先看契约本身——TilingData 结构体只有三个字段，朴素得像一张便签：

[examples/add_example/op_kernel/add_example_tiling_data.h:19-23](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23) 定义了 `totalNum`（总元素数）、`blockFactor`（每核元素数）、`ubFactor`（每轮 UB 处理元素数）。它必须是 POD 类型，因为框架会把它原样按字节拷贝到 Device。

再看注册入口。Tiling 函数不是被直接调用的，而是通过宏注册进算子实现注册表：

[examples/add_example/op_host/add_example_tiling.cpp:261-L261](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L261) 用 `IMPL_OP_OPTILING(AddExample).Tiling(AddExampleTilingFunc).TilingParse<AddExampleCompileInfo>(TilingParseForAddExample)` 把 Tiling 主函数和 TilingParse 函数挂到 `AddExample` 这个算子名下——与 u3-l1 的 `OP_ADD`、u3-l2 的 `IMPL_OP_INFERSHAPE` 是同一套静态注册风格。其中 `TilingParseForAddExample` 在 [add_example_tiling.cpp:254-257](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L254-L257) 直接返回成功：它是图模式的标准交付件，自动生成 aclnn 时不调用，无逻辑可置空（官方说明见 [docs/zh/develop/aicore_develop_guide.md:130-133](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L130-L133)）。

#### 4.1.4 代码实践

**实践目标**：建立「TilingData 是 Host/Device 共享内存契约」的直观感受。

**操作步骤**：

1. 打开 [add_example_tiling_data.h:19-23](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23)，给结构体临时加一个字段 `int64_t magic = 0;`（示例代码，验证后删掉）。
2. 在 `AddExampleTilingFunc` 中给它赋一个特征值（如 `tiling->magic = 20250815;`）。
3. 在 kernel 侧 [add_example.h:59-61](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L59-L61) 附近用 `AscendC::Printf` 打印 `tilingData->magic`（Printf 用法详见 u8-l1）。
4. 重新编译安装并运行样例，观察 Device 侧能读到 Host 写入的值。

**需要观察的现象**：Host 写的值原样出现在 kernel 打印里——结构体本身不序列化、不转义，按内存布局整体拷贝。

**预期结果**：打印输出 `20250815`。注意：由于结构体变了，Host 侧（tiling.cpp）与 Device 侧（add_example.h）用的是同一个头文件，两侧自动同步——这就是为什么该头文件放在 `op_kernel` 目录却同时被 `op_host` 引用。完整运行需重新 `--pkg` 编译安装（见 u1-l2）。若无硬件环境，本实践「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AddExampleTilingData` 里的字段从 `int64_t` 改成 `int32_t`，kernel 侧需要同步修改吗？

**答案**：不需要显式修改——kernel 侧 include 的是同一个头文件，类型变更自动对两侧同时生效。但如果 kernel 侧有代码把这些字段参与 `int64_t` 运算，需注意隐式截断风险。

**练习 2**：`TilingParseForAddExample` 为什么可以是空实现？什么场景下需要写实际逻辑？

**答案**：TilingParse 是图模式标准交付件，用于在模型加载期解析静态编译信息；自动生成 aclnn 的路径不调用它，无逻辑时置空返回 `GRAPH_SUCCESS` 即可。手写 aclnn 且需要缓存平台信息（核数、UB 大小）到 CompileInfo 时才需要填充，官方模板的注释版示例见 [docs/zh/develop/aicore_develop_guide.md:137-155](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L137-L155)。

### 4.2 通过 TilingContext 获取平台信息与 shape

#### 4.2.1 概念说明

Tiling 的所有输入都来自一个参数：`gert::TilingContext* context`。可以把把它理解为框架递给 Tiling 函数的「上下文工具箱」，本讲用到的能力有：

- `GetPlatformInfo()`：拿到平台描述，进而查 **UB 大小** 和 **AI Core（AIV）数量**——切分的两个分母。
- `GetInputShape(i)` / `GetOutputShape(i)`：拿到第 i 个输入/输出的 shape——切分的分子。
- `GetInputDesc(i)->GetDataType()`：拿到 dtype——决定 TilingKey 与元素宽度。
- `GetTilingData<T>()`：拿到可写的 TilingData 缓冲。
- `SetBlockDim()` / `SetTilingKey()` / `GetWorkspaceSizes()`：写出切分结论。

#### 4.2.2 核心流程

```text
GetPlatformInfo(context):
  platformInfoPtr = context->GetPlatformInfo()
  ascendcPlatform = PlatformAscendC(platformInfoPtr)
  coreNum = ascendcPlatform.GetCoreNumAiv()          # 矢量核数
  ascendcPlatform.GetCoreMemSize(UB, ubSize)         # UB 字节容量

GetShapeAttrsInfo(context):
  shapeX = EnsureNotScalar(GetInputShape(0)->GetStorageShape())
  校验 x/y/z 三者都是 4 维（DIMS_LIMIT = 4）
  totalIdx = shapeX.GetShapeSize()                   # 元素总数
  校验 dtype ∈ {DT_FLOAT, DT_INT32}
```

其中「标量归一化」值得注意：0 维标量的 shape 会被 `EnsureNotScalar` 转成 `{1}`，避免下游按 0 维取 size 出错——这与 u3-l2 讲过的标量 shape 差异一脉相承。

#### 4.2.3 源码精读

[examples/add_example/op_host/add_example_tiling.cpp:76-94](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L76-L94) 是获取平台信息的完整实现：用 `PlatformAscendC` 包装平台指针后，`GetCoreNumAiv()` 取矢量核数、`GetCoreMemSize(CoreMemType::UB, ubSize)` 取 UB 容量。两处 `OP_CHECK_IF` 保证取到 0 时立刻报错返回，而不是带着 0 继续算出错误的切分。

[examples/add_example/op_host/add_example_tiling.cpp:58-64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L58-L64) 是标量归一化：`IsScalar()` 命中时返回静态常量 `g_vec_1_shape = {1}`（定义在 [add_example_tiling.cpp:45](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L45)），否则原样返回入参引用。

[examples/add_example/op_host/add_example_tiling.cpp:109-150](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L109-L150) 是 shape/dtype 获取与校验：第 129-133 行校验三个张量都必须是 4 维（`DIMS_LIMIT = 4`，定义在 [add_example_tiling.cpp:32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L32)）；第 136 行用 `GetShapeSize()` 得到 `totalIdx`；第 139-147 行把 dtype 白名单写成 `std::set` 再 `count` 判断——注意这个白名单与 def 文件（u3-l1）声明的 `DataType` 列表、kernel 的模板分支是三层独立的闸门，扩 dtype 时三处都要改（u3-l1 综合实践已演练过）。

#### 4.2.4 代码实践

**实践目标**：观察「平台信息由框架注入，Tiling 只消费」。

**操作步骤**：

1. 阅读 [tests/ut/op_host/test_add_example_tiling.cpp:30-46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L30-L46)：UT 通过 `gert::TilingContextPara` 构造了一个假的 Tiling 上下文，其中第 43-46 行直接指定了 `64`（核数）、`262144`（UB 大小）、`4096`（tiling data 上限）。
2. 注意第 44-45 行的注释：实际拿到的 UB 比 262144 **少 256 字节**。
3. 运行该 UT：`bash build.sh -u --ophost --ops=add_example`（命令依据 [docs/zh/install/compile.md:236-262](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L236-L262)，`--ophost` 表示只跑 Host 侧 UT，无需真实硬件）。

**需要观察的现象**：UT 断言 `expectTilingData = "2048 32 10912"` 通过——下一节我们会手工推导这三个数。

**预期结果**：两个用例（float/int32）全部 PASSED。若环境缺少 CANN 头文件则无法编译，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetCoreNumAiv()` 而不是某个「总核数」接口？

**答案**：AI Core 分矢量核（AIV）和矩阵核（AIC/Cube）。AddExample 是纯矢量算子，只会在矢量核上调度，所以切分的分母是 AIV 数；Cube 类算子（如 matmul）才会关注 AIC 数量。

**练习 2**：`GetShapeAttrsInfo` 里 dtype 白名单和 def 文件里的 `DataType` 声明重复吗？

**答案**：不重复，是两道不同时机的闸门。def 文件在编译期生成参数校验（aclnn 第一段会拒绝非法 dtype，返回参数错误）；tiling 里的白名单是运行期防御，且 tiling 需要根据 dtype 决定 TilingKey 和元素宽度（`TYPE_SIZE`），二者职责不同。

### 4.3 核切分：blockFactor 与 SetBlockDim

#### 4.3.1 概念说明

核切分回答的问题是：**总共 `totalIdx` 个元素、手头 `coreNum` 个核，每个核分多少？**

直觉做法是整除，但 `totalIdx` 往往不能被 `coreNum` 整除，直接整除会漏掉余数。所以用向上取整除法：

\[ \text{blockFactor} = \left\lceil \frac{\text{totalIdx}}{\text{coreNum}} \right\rceil \]

这样每核「最多」处理 `blockFactor` 个，最后一核可能不满。随后反向算实际用核数：

\[ \text{usedCoreNum} = \left\lceil \frac{\text{totalIdx}}{\text{blockFactor}} \right\rceil \]

为什么不让 `usedCoreNum` 直接等于 `coreNum`？因为小任务下 `blockFactor` 取整后变大（例如 100 元素、64 核时 `blockFactor = 2`，只需 50 核），反向计算避免起空核。**「优先做核切分，尽量用更多的核并行计算」**——源码注释原话。

#### 4.3.2 核心流程

```text
tiling->totalNum    = totalIdx                        # 总量记账
tiling->blockFactor = CeilDiv(totalIdx, coreNum)      # 每核配额
usedCoreNum         = CeilDiv(totalIdx, blockFactor)  # 反推实耗核数
context->SetBlockDim(usedCoreNum)                     # 告诉框架起几个核
```

kernel 侧（消费端）用 `blockFactor` 切 GM 地址空间：第 `n` 号核负责从 `blockFactor * n` 开始、长度不超过 `blockFactor` 的一段。

#### 4.3.3 源码精读

[examples/add_example/op_host/add_example_tiling.cpp:212-216](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L212-L216) 是核切分的全部三行：`totalNum` 记账、`CeilDiv` 算配额、再反推 `usedCoreNum`。`CeilDiv` 的实现语义可在本仓库 kernel 侧对照阅读：[common/inc/op_kernel/kernel_utils.h:44-50](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h#L44-L50)，即 `(a + b - 1) / b` 并对 `b == 0` 做防御。

[examples/add_example/op_host/add_example_tiling.cpp:224-225](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L224-L225) 把 `usedCoreNum` 写回框架：`SetBlockDim` 决定本次 kernel 启动占用多少个核（Block 维度）。

消费端在 [examples/add_example/op_kernel/add_example.h:59-65](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L59-L65)：`remainderLength` 用「总量 − 前面核已处理的量」算出本核剩余量，与 `blockFactor` 取小得到本核实际长度 `blockLength_`；随后三个 `SetGlobalBuffer` 都以 `blockFactor * GetBlockIdx()` 为偏移把本核的 GM 窗口开在自己的那段数据上。核号从 1 开始（`GetBlockIdx() - 1` 参与运算）。

用 UT 数据验算一遍：`totalIdx = 32×4×4×4 = 2048`，`coreNum = 64`：

\[ \text{blockFactor} = \lceil 2048/64 \rceil = 32,\quad \text{usedCoreNum} = \lceil 2048/32 \rceil = 64 \]

正好是 `expectTilingData` 的第二个数 `32`。

#### 4.3.4 代码实践

**实践目标**：观察核切分随输入规模的变化规律（本讲主实践）。

**操作步骤**：

1. 方式 A（UT 断言）：复制 [tests/ut/op_host/test_add_example_tiling.cpp:26-51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L26-L51) 的用例，新增 `add_example_2`，把输入 shape 从 `{32,4,4,4}` 改成 `{2,4,4,4}`（totalIdx = 128）与 `{1,2,2,2}`（totalIdx = 8），核数仍为 64。
2. 手工推导预期的 `expectTilingData` 字符串（totalNum、blockFactor、ubFactor 三个数）。
3. 运行 `bash build.sh -u --ophost --ops=add_example` 查看断言结果。
4. 方式 B（日志）：在 [add_example_tiling.cpp:216](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L216) 后加一行 `OP_LOGI(context, "totalIdx=%ld, blockFactor=%ld, usedCoreNum=%ld, ubFactor=%ld", totalIdx, tiling->blockFactor, usedCoreNum, tiling->ubFactor);`（示例代码），重新编译安装后运行样例并开 Host 日志开关观察。

**需要观察的现象**：

- totalIdx = 128 时：`blockFactor = 2`，`usedCoreNum = 64`；
- totalIdx = 8 时：`blockFactor = 1`，`usedCoreNum = 8`——只有 8 个核有活干，其余核不启动；
- ubFactor 不随 totalIdx 变化（它只由 UB 容量决定，见 4.4）。

**预期结果**：UT 三个用例全部通过；日志中三个数满足上述关系。UT 数值可离线推导验证；日志方式「待本地验证」（需重新编译安装）。

#### 4.3.5 小练习与答案

**练习 1**：`totalIdx = 100`、`coreNum = 64`，求 `blockFactor` 与 `usedCoreNum`。

**答案**：`blockFactor = ⌈100/64⌉ = 2`；`usedCoreNum = ⌈100/2⌉ = 50`。前 50 核各处理 2 个元素，共 100，其余 14 核空闲不被启动。

**练习 2**：如果删掉 `usedCoreNum` 反推这一步、直接 `SetBlockDim(coreNum)`，功能上会出错吗？

**答案**：不会算错——kernel 侧用 `remainderLength` 做了防护（[add_example.h:59-60](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L59-L60)），空核的 `remainderLength ≤ 0`，`blockLength_` 取到非正值后循环不会执行。但会浪费调度资源：多启动了没有任何工作量的核。反推是「能省则省」的优化，不是正确性必需。

### 4.4 UB 切分：ubFactor、workspace 与 TilingKey

#### 4.4.1 概念说明

核切分之后，单个核的工作量 `blockFactor` 仍可能远大于 UB 容量（例如 910B 的 UB 约 256 KB，而 `blockFactor` 可达百万级）。第二级切分回答：**一轮循环里搬多少元素进 UB？**

约束链是：

1. UB 总容量 `ubSize` 字节，除以元素宽度 `TYPE_SIZE`（float/int32 都是 4 字节）得到可容纳的元素数；
2. kernel 同时持有 3 块 UB tensor（2 输入 + 1 输出），且使用**双缓冲**（`BUFFER_NUM = 2`，搬运与计算重叠），共占 \(3 \times 2 = 6\) 块——所以再除以 `BUFFER_NUM = 6`；
3. UB 搬运按 32 字节一块对齐，结果要向下对齐到 32 字节块的整数倍（`GetUbBlockSize()` 返回 32，见 [common/inc/op_kernel/platform.h:46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/platform.h#L46)）。

写成公式：

\[ \text{ubFactor} = \mathrm{FloorAlign}\left( \left\lfloor \frac{\lfloor \text{ubSize} / \text{TYPE\_SIZE} \rfloor}{\text{BUFFER\_NUM}} \right\rfloor,\ 32\text{字节块折算的元素数} \right) \]

注意用向下对齐而不是向上：向上可能超出 UB 容量，向下只是少用一点点空间，安全。

同节还剩两个小职责：

- **Workspace**：算子执行所需的 Device 侧额外内存。Add 不需要，`GetWorkspaceSize` 写 0（[add_example_tiling.cpp:30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L30) 的 `WS_SYS_SIZE = 0`）。复杂算子会在这里按 tiling 结果计算中间缓冲大小。
- **TilingKey**：按 dtype 打标记，float → `MODE_0`（key = 0）、int32 → `MODE_1`（key = 1），kernel 侧据此选择模板实例（u1-l4 已见过 `if constexpr` 分发）。

#### 4.4.2 核心流程

```text
ubCanUse   = (int64_t)ubSize                          # 字节
ubBlockSize = GetUbBlockSize(context)                  # 32 字节
ubFactor   = FloorAlign(FloorDiv(ubCanUse / TYPE_SIZE, BUFFER_NUM), ubBlockSize)

workspace: currentWorkspace[0] = 0
tilingKey: DT_FLOAT → GET_TPL_TILING_KEY(MODE_0)；DT_INT32 → MODE_1
           context->SetTilingKey(tilingKey)
```

kernel 侧每轮循环处理 `ubFactor` 个元素，循环次数为 \(\lceil \text{blockLength} / \text{ubFactor} \rceil\)，尾轮只处理剩余量（`DataCopyPad` 负责非对齐尾块）。

#### 4.4.3 源码精读

[examples/add_example/op_host/add_example_tiling.cpp:40-43](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L40-L43) 定义了两个关键常量：`BUFFER_NUM = 6`（注释说明：2 输入 + 1 输出、双缓冲，共 6 块 UB tensor）与 `TYPE_SIZE = 4`（float/int32 均为 4 字节——这也解释了为什么这个教学样例只支持这两种等宽类型）。

[examples/add_example/op_host/add_example_tiling.cpp:218-222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L218-L222) 是 UB 切分公式的一行式实现，从内到外依次是：字节转元素（`/ TYPE_SIZE`）、6 块均分（`FloorDiv(..., BUFFER_NUM)`）、按 32 字节块向下对齐（`FloorAlign(..., ubBlockSize)`）。`FloorDiv` 即普通整除，语义对照 [common/inc/op_kernel/kernel_utils.h:52-59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h#L52-L59)。

[examples/add_example/op_host/add_example_tiling.cpp:161-168](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L161-L168) 是 workspace 设置：`GetWorkspaceSizes(1)` 取到长度为 1 的数组，写入 0。

[examples/add_example/op_host/add_example_tiling.cpp:227-240](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L227-L240) 按 dtype 设置 TilingKey。key 值来自 [add_example_tiling_key.h:21-25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L25)：用 `ASCENDC_TPL_ARGS_DECL` 声明了一个名为 `schMode` 的模板参数，取值列表就是 `MODE_0(0)` 和 `MODE_1(1)`，`GET_TPL_TILING_KEY` 把「参数名 = 某取值」编码成一个 uint64 key。UT 断言印证：float 用例 `expectTilingKey = 0`，int32 用例 `expectTilingKey = 1`（[test_add_example_tiling.cpp:47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L47)、[:70](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L70)）。

消费端 [examples/add_example/op_kernel/add_example.h:67-69](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L67-L69) 用 `ubFactor * sizeof(T)` 给三个队列各分配 UB 缓冲——Host 算出的 `ubFactor` 与 Device 的 `InitBuffer` 尺寸必须严丝合缝，任何一侧算错都会导致 UB 越界。

最后手工验算 UT 的 `ubFactor = 10912`：UB 实际值 \(262144 - 256 = 261888\) 字节，\(261888 / 4 = 65472\) 个元素，\(\lfloor 65472 / 6 \rfloor = 10912\)，\(10912\) 恰是 32 字节（8 个 float）的整数倍，对齐后不变。三个数 `2048 32 10912` 与 `expectTilingData` 完全吻合。

#### 4.4.4 代码实践

**实践目标**：验证 UB 切分只依赖硬件与算子缓冲布局，与输入规模无关；并体会 `BUFFER_NUM` 对 ubFactor 的压低作用。

**操作步骤**：

1. 在 4.3.4 的两个新 UT 用例（totalIdx = 128 和 8）中，保持核数 64、UB 262144 不变，直接沿用 `"2048 32 10912"` 的后两个数推导预期字符串：分别为 `"128 2 10912 "` 与 `"8 1 10912 "`。
2. 运行 UT 验证。
3. （可选，示例代码）把 [add_example_tiling.cpp:41](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L41) 的 `BUFFER_NUM` 临时改成 3（相当于不用双缓冲），手工推导新 ubFactor：\(\lfloor 65472/3 \rfloor = 21824\)，对齐 8 的倍数不变 → `21824`；改 UT 期望值后重跑。
4. （选做）把 kernel 侧 [add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) 的 `BUFFER_NUM` 同步改为 3 再上板运行，对比正确性与性能。

**需要观察的现象**：

- 输入规模变化时 `ubFactor` 始终是 10912（同一芯片上）；
- `BUFFER_NUM` 从 6 改 3 后 `ubFactor` 变为 21824——块数越少、单块越大；
- kernel 侧若不同步改 `BUFFER_NUM`，Host 与 Device 对 UB 布局的假设不一致，可能越界或结果错乱。

**预期结果**：UT 按新期望值通过。步骤 1-2 可离线推导 + UT 验证；步骤 3-4 涉及源码修改与上板，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：某芯片 UB 实际可用 520192 字节，元素为 float16（`TYPE_SIZE = 2`），缓冲布局同 AddExample（6 块），求 `ubFactor`。

**答案**：\(520192/2 = 260096\) 个元素；\(\lfloor 260096/6 \rfloor = 43349\)；float16 的 32 字节块 = 16 个元素，\(43349 = 16 \times 2709 + 5\)，向下对齐得 \(43344\)。

**练习 2**：为什么 `ubFactor` 用 `FloorAlign`（向下对齐）而核切分的 `blockFactor` 用 `CeilDiv`（向上取整）？

**答案**：方向由「越界的代价」决定。`ubFactor` 超过 UB 容量会直接越界崩溃，宁少勿多，向下对齐；`blockFactor` 少算了会漏元素导致结果不完整，宁多勿少，向上取整（多余的量由 kernel 的 `remainderLength` 防御吸收）。

**练习 3**：`SetBlockDim(64)` 和 `SetTilingKey(1)` 分别影响什么？

**答案**：`SetBlockDim` 决定本次 kernel 启动的核数（Block 维度大小），是调度量；`SetTilingKey` 是传给 kernel 的分支标记，Device 侧入口据此用 `if constexpr` 选择 int32 模板实例，是代码路径选择。一个管「用多少个核」，一个管「跑哪份代码」。

## 5. 综合实践

**任务：给 AddExample 写一份「切分计算器」并验证。**

1. 通读 [add_example_tiling.cpp:185-243](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L185-L243) 的主函数，把其中「核切分 + UB 切分」抽象成一张手算表格：列分别为 `totalIdx`、`coreNum`、`ubSize`、`blockFactor`、`usedCoreNum`、`ubFactor`。
2. 填入四组数据自行计算：(2048, 64, 261888)、(128, 64, 261888)、(100, 64, 261888)、(2048, 40, 197120)。
3. 为第三组（totalIdx = 100）补一个 UT 用例：shape 用 `{5,5,4,1}`（共 100 元素），dtype float，核数 64、UB 262144，按你的手算结果填写 `expectTilingData`。
4. 运行 `bash build.sh -u --ophost --ops=add_example` 对账。
5. 若有硬件，再任选一组参数改 [examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp) 的输入 shape 上板验证结果正确性（正确性不随切分参数改变，这正是 tiling 的意义：切分只影响性能，不影响语义）。

参考答案（前三组）：(2048,64,261888) → 2048/32/10912；(128,64,261888) → 128/2/10912；(100,64,261888) → 100/2/50 核、ubFactor 10912。第四组：\(197120/4 = 49280\)，\(\lfloor 49280/6 \rfloor = 8213\)，对齐 8 → 8208；`blockFactor = ⌈2048/40⌉ = 52`，`usedCoreNum = ⌈2048/52⌉ = 40`。

## 6. 本讲小结

- Tiling 运行在 Host 侧，由框架经 `gert::TilingContext` 回调，产出 TilingData、BlockDim、TilingKey 三样东西交给 Device 侧 kernel。
- 切输入来自两个方向：平台信息（`GetCoreNumAiv`、`GetCoreMemSize(UB)`）给分母，输入 shape/dtype（`GetInputShape`、`GetInputDesc`）给分子和白名单。
- 核切分：`blockFactor = ⌈totalIdx/coreNum⌉`，再反推 `usedCoreNum` 避免起空核，经 `SetBlockDim` 生效；kernel 按 `blockFactor × GetBlockIdx()` 划分 GM 窗口。
- UB 切分：`ubFactor` 由 UB 容量、元素宽度、UB tensor 块数（2 输入 + 1 输出 × 双缓冲 = 6）与 32 字节对齐共同决定，与输入规模无关。
- 取整方向的选择原则是「越界代价」：向上取整防漏元素，向下对齐防内存越界。
- `SetTilingKey` 是 kernel 分支选择器（float→0、int32→1），与 def 声明、kernel 模板构成三道彼此独立的类型闸门。

## 7. 下一步学习建议

下一讲（u4-l2）将深入 TilingData 与 TilingKey 的进阶机制：tiling data 结构体在 host/device 间如何按二进制布局传递、tiling key 的编码方式，以及 `tiling_templates_registry.h` 提供的模板化 tiling 注册与复用。建议提前浏览 [common/inc/op_host/tiling_templates_registry.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_templates_registry.h)，并回顾本讲 `ASCENDC_TPL_ARGS_DECL` 的用法——它就是下一讲的伏笔。之后 u5 单元会把视角切到 kernel 侧，讲解 kernel 如何消费本讲算出的切分参数驱动 CopyIn-Compute-CopyOut 流水。
