# Tiling 机制：算子切分与多核并行

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 Tiling（切块）到底解决什么问题：为什么 Host 侧要先把数据切好，Device 侧的 AI Core 才能高效并行。
2. 掌握一个 TilingFunc 的标准实现步骤：取平台信息 → 解析 shape/属性 → 计算 workspace → 填充 TilingData → 设置 BlockDim 与 TilingKey → 注册。
3. 理解 TilingData 这份「切分方案书」如何从 Host 侧的结构体一路传到 Kernel 侧的 `GET_TILING_DATA_WITH_STRUCT`。
4. 了解仓库公共层 `common/inc/op_host/tiling_base.h` 提供的 `TilingBaseClass` 模板化 tiling 框架，知道 add_example 的「自由函数式」写法与它的关系。

本讲承接 u3-l1 的全景地图。u3-l1 已经指出 Tiling 分两个阶段（编译期 TilingParse、运行期 TilingFunc），本讲把运行期 TilingFunc 拆开逐行精读。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

**为什么需要 Tiling？** 一块 AI Core 芯片上有几十个核（AI Core / AIV），而一个算子的输入可能有几百万个元素。任何一个核都无法（也不应该）一次把所有数据吞下：

- **核间并行**：把总元素数切成若干份，每个核领一份，这叫「核切分」（block 切分）。
- **核内分批**：每个核内部只有一块很小的 Unified Buffer（UB，统一缓冲区，通常几十到几百 KB），数据必须从 Global Memory（GM，DDR 上的大内存）分批搬进 UB，算完再搬回去，这叫「UB 切分」。

**Tiling 就是 Host 侧提前算好这份「分工方案」**：总共多少元素（`totalNum`）、每个核分多少（`blockFactor`）、每批搬多少（`ubFactor`）、用几个核（BlockDim）。Kernel 启动后只管照单执行，不需要（也无法）在 Device 侧做这些决策——因为 Host 侧才能看到完整的 shape 和硬件规格信息。

**Tiling 的执行时机**：回顾 u3-l1，aclnn 第一段接口（GetWorkspaceSize）执行时，框架会回调算子注册的 TilingFunc。此时 shape 已知、平台信息可查，是做切分决策的最佳时机。TilingFunc 产出的数据会随任务一起下发到 Device。

**几个名词**：

| 名词 | 含义 |
|---|---|
| AIV | AI Core 上的向量核（AI Vector），elementwise 类算子主要跑在 AIV 上 |
| UB | Unified Buffer，核内高速缓存，kernel 计算直接访问的数据必须先搬进来 |
| GM | Global Memory，Device 上的大容量 DDR，aclTensor 的数据放这里 |
| BlockDim | 本次 kernel 启动使用多少个核 |
| TilingKey | 一个 uint64 编码，用来让 Kernel 侧区分「该走哪个实现分支」 |
| double buffer | 搬运与计算重叠的技巧，需要 2 倍 UB 缓冲，所以 UB 要按 buffer 数均分 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp) | 本讲主线：AddExample 的完整 TilingFunc 实现与注册 |
| [examples/add_example/op_kernel/add_example_tiling_data.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example_tiling_data.h) | TilingData 结构体定义，Host 与 Kernel 两侧共享的「契约」 |
| [common/inc/op_host/tiling_base.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h) | 公共层 `TilingBaseClass`，把 tiling 流程模板化为 8 个虚函数钩子 |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.cpp) | Kernel 入口，展示 TilingData 如何被取回（`GET_TILING_DATA_WITH_STRUCT`） |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.h) | Kernel 实现，展示 `blockFactor`/`ubFactor` 如何被消费 |
| [examples/add_example/op_kernel/add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example_tiling_key.h) | TilingKey 模板参数声明（schMode 0/1），u3-l4 会展开 |

另外，`add_example_tiling.cpp` 顶部 include 的 `util/math_util.h`、`op_host/tiling_util.h` 中，`Ops::Base::CeilDiv`（向上取整除法）、`FloorDiv`（向下取整除法）、`FloorAlign`（向下对齐）、`GetUbBlockSize`（获取 UB 最小对齐粒度）等工具来自 CANN toolkit 安装目录下的公共头文件，不在本仓库内（本仓库的 [common/inc/op_host/tiling_util.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_util.h#L21-L31) 只提供 `EnsureNotScalar`、`IsRegbaseSocVersion` 等少量函数）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **Tiling 总体流程与注册**：TilingFunc 在哪被调用、怎么注册。
2. **TilingFunc 四步精读**：平台信息、shape 解析、切分计算、TilingKey。
3. **TilingData 的 Host → Device 传递**：结构体如何变成 Kernel 眼里的数据。
4. **公共层 `TilingBaseClass`**：模板化 tiling 框架。

### 4.1 Tiling 总体流程与注册

#### 4.1.1 概念说明

TilingFunc 是一个由框架回调的 Host 侧函数，签名固定接收 `gert::TilingContext*`。它不直接做计算，只做「决策」：算出切分参数，写进 context 挂载的 TilingData 缓冲区，并通过 `SetBlockDim` 告诉框架要起多少个核、通过 `SetTilingKey` 告诉框架（和 Kernel）选哪条实现分支。

整条链路的位置关系（承接 u3-l1 的全景图）：

```text
aclnnAddExampleGetWorkspaceSize (op_api 第一段)
        │  框架按 OpDef 路由
        ▼
op_host: InferShape / InferDataType   ← u3-l2 已讲
        ▼
op_host: AddExampleTilingFunc         ← 本讲：切分决策
        │  GetTilingData<AddExampleTilingData>() 写入切分参数
        │  SetBlockDim(usedCoreNum) / SetTilingKey(tilingKey)
        ▼
第二段接口下发任务 → Kernel 启动 usedCoreNum 个核
        ▼
op_kernel: GET_TILING_DATA_WITH_STRUCT 取回参数 → 照方案执行
```

#### 4.1.2 核心流程

注册分两半，Host 侧与 Kernel 侧各持有一份「同名契约」：

- Host 侧：`IMPL_OP_OPTILING(AddExample).Tiling(...).TilingParse<...>(...)` 把 tiling 函数挂到 AddExample 这个算子名上。
- Kernel 侧：`REGISTER_TILING_DEFAULT(AddExampleTilingData)` 注册同名结构体，使 `GET_TILING_DATA_WITH_STRUCT` 能正确解包。

#### 4.1.3 源码精读

注册入口只有一行，在 tiling 文件的最底部：

[examples/add_example/op_host/add_example_tiling.cpp:L264-L267](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L264-L267) —— `IMPL_OP_OPTILING(AddExample)` 把命名空间 `optiling` 下的实现与算子名 AddExample 绑定；`.Tiling(AddExampleTilingFunc)` 注册运行期切分函数；`.TilingParse<AddExampleCompileInfo>(TilingParseForAddExample)` 注册编译期解析函数（这里 `AddExampleCompileInfo` 是空结构体，解析函数直接返回成功，因为 AddExample 没有需要在编译期固化的静态信息）。

[examples/add_example/op_host/add_example_tiling.cpp:L259-L262](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L259-L262) —— `TilingParseForAddExample` 的空实现。对比 u3-l1 提到的 resize_bilinear_v2：那里 TilingParse 会在模型加载期读取核数与 UB 大小存进 CompileInfo，供运行期使用；AddExample 无此需求，所以留空。这就是「同一个注册宏、两种用法」的实例。

#### 4.1.4 代码实践

**实践目标**：确认注册链路真实存在，并理解 `.Tiling(...)` 与 `.TilingParse<...>(...)` 是两个独立钩子。

**操作步骤**：

1. 打开 `examples/add_example/op_host/add_example_tiling.cpp`，定位第 266 行的注册语句。
2. 在仓库内全局搜索 `IMPL_OP_OPTILING(AddExample)`，确认它只出现一次（重复注册会冲突）。
3. 再搜索 `IMPL_OP_OPTILING(`，观察其他算子（如 resize_bilinear_v2）是否也用同样句式结尾。

**需要观察的现象**：每个有独立 tiling 实现的算子，其 op_host 目录下都有且仅有一条 `IMPL_OP_OPTILING` 注册语句。

**预期结果**：能列出至少 3 个同样使用该句式的算子文件。这一步纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：TilingFunc 和 TilingParse 各在什么时机执行？

**答案**：TilingFunc 在运行期、每次算子下发任务前被框架回调（aclnn 第一段接口阶段），此时能看到具体 shape；TilingParse 在模型加载/编译期执行一次，用于固化静态平台信息到 CompileInfo。AddExample 的 TilingParse 为空，因为它不需要静态信息。

**练习 2**：如果删掉 `IMPL_OP_OPTILING` 这一行，会发生什么？

**答案**：AddExample 的 OpDef 仍然注册（def 文件还在），框架能找到算子，但找不到 tiling 实现，运行期走到 tiling 阶段会失败，算子无法下发执行。

### 4.2 TilingFunc 四步精读：平台、shape、切分、TilingKey

#### 4.2.1 概念说明

`AddExampleTilingFunc` 是本讲主角。它把工作拆成四个子函数，每个子函数职责单一，这是仓库推荐的 tiling 写法：

1. `GetPlatformInfo`：问硬件「你有几个核、UB 多大」。
2. `GetShapeAttrsInfo`：问输入「你有多少元素、什么 dtype」。
3. `GetWorkspaceSize`：申报算子需要的额外 workspace（AddExample 不需要，填 0）。
4. 主函数里计算切分参数并填充 `AddExampleTilingData`，最后 `SetBlockDim` + `SetTilingKey`。

#### 4.2.2 核心流程

主函数的执行顺序（伪代码）：

```text
AddExampleTilingFunc(context):
    ubSize, coreNum = GetPlatformInfo(context)     # 失败则报错返回
    totalIdx, dtype = GetShapeAttrsInfo(context)   # 失败则报错返回
    GetWorkspaceSize(context)                      # workspace = 0

    tiling = context->GetTilingData<AddExampleTilingData>()
    memset(tiling, 0)                              # 清零，避免脏数据

    # 核切分：优先用满所有核
    tiling->totalNum    = totalIdx
    tiling->blockFactor = CeilDiv(totalIdx, coreNum)      # 每核元素数（向上取整）
    usedCoreNum         = CeilDiv(totalIdx, blockFactor)  # 实际需要的核数

    # UB 切分：每批搬运的元素数
    tiling->ubFactor    = FloorAlign(FloorDiv(ubSize/4, 6), ubBlockSize)

    context->SetBlockDim(usedCoreNum)
    context->SetTilingKey(按 dtype 选择 0 或 1)
```

其中核切分与 UB 切分的数学关系可以写成：

\[ \text{blockFactor} = \left\lceil \frac{\text{totalNum}}{\text{coreNum}} \right\rceil, \qquad \text{usedCoreNum} = \left\lceil \frac{\text{totalNum}}{\text{blockFactor}} \right\rceil \]

注意 `usedCoreNum` 要重算一遍而不是直接用 `coreNum`：当 `totalNum` 不能整除时，最后一部分核可能只分到很少元素甚至 0 个元素，`CeilDiv(totalIdx, blockFactor)` 会自动「收缩」核数。极端例子：`totalNum=1`、`coreNum=50` 时 `blockFactor=1`、`usedCoreNum=1`，只起 1 个核，避免 49 个空核白跑。

UB 切分公式：

\[ \text{ubFactor} = \mathrm{FloorAlign}\left( \left\lfloor \frac{ \lfloor \text{ubSize} / \text{typeSize} \rfloor }{ \text{bufferNum} } \right\rfloor,\ \text{ubBlockSize} \right) \]

直观解释：UB 总字节数先除以每个元素的类型大小（`TYPE_SIZE = 4`，float/int32 都是 4 字节）得到「UB 能装多少个元素」；再除以 `BUFFER_NUM = 6`——因为 2 个输入 + 1 个输出、每个队列使能 double buffer 要 2 块，共 3 × 2 = 6 块 UB tensor 必须同时装得下；最后向下对齐到 `ubBlockSize`（UB 最小访问粒度，由 `GetUbBlockSize(context)` 查询），不对齐的搬运会付出额外代价。

#### 4.2.3 源码精读

**第一步：平台信息。**

[examples/add_example/op_host/add_example_tiling.cpp:L79-L97](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L79-L97) —— `GetPlatformInfo` 从 `context->GetPlatformInfo()` 拿到平台信息指针，构造 `platform_ascendc::PlatformAscendC` 包装对象（这是 CANN toolkit 提供的平台查询类），再用 `GetCoreNumAiv()` 取 AIV 核数、`GetCoreMemSize(CoreMemType::UB, ubSize)` 取 UB 字节数。两个值都做了非零校验，失败记 `OP_LOGE` 日志并返回 `GRAPH_FAILED`——这是 tiling 函数的标准容错姿势。

**第二步：shape 与 dtype。**

[examples/add_example/op_host/add_example_tiling.cpp:L112-L153](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L112-L153) —— `GetShapeAttrsInfo` 依次取输入 x（index 0）、输入 y（index 1）、输出 z（index 0）的 `GetStorageShape()`，经过 `EnsureNotScalar` 把标量 shape 统一成 `{1}`（见 [L61-L67](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L61-L67)），然后做两层校验：三维张量都必须是 4 维（`DIMS_LIMIT = 4`），dtype 必须是 `DT_FLOAT` 或 `DT_INT32`。通过后 `totalIdx = inputShapeX.GetShapeSize()` 拿到总元素数。

注意这里的校验与 u3-l1 讲过的 def 文件白名单是互补关系：def 白名单在更低层拦截不支持的组合，tiling 里的校验则面向具体实现策略（本实现假定 4 维）。

**第三步：workspace。**

[examples/add_example/op_host/add_example_tiling.cpp:L164-L171](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L164-L171) —— `GetWorkspaceSize` 通过 `context->GetWorkspaceSizes(1)` 取到长度为 1 的 workspace 数组并填入 `WS_SYS_SIZE = 0`。AddExample 是纯 elementwise 算子，数据在 GM↔UB 之间直来直去，不需要中间结果暂存区，所以 workspace 为 0。这与 u2-l1 讲过的「workspace 为 0 可跳过申请」正好呼应：样例里 aclnnAddExample 申请的 workspace 大小就是从这里来的。

**第四步：主函数填充 TilingData 并设置 key。**

[examples/add_example/op_host/add_example_tiling.cpp:L189-L247](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L189-L247) —— `AddExampleTilingFunc` 主体。几个关键点：

- [L208-L213](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L208-L213)：`context->GetTilingData<AddExampleTilingData>()` 拿到框架分配的 TilingData 指针，先 `memset_s` 清零——不清零的话，未赋值字段会带着上一次调用的脏值进 kernel。（顺带一提：本版本的一次日志质量修复把这里 `memset_s` 失败分支的日志文案从 `"set tiling data error"` 改成了更规范的 `"Failed to set tiling data"`，属于纯文案调整，不影响逻辑与行号。）
- [L215-L219](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L215-L219)：核切分，注释明确写了策略「优先做核切分，尽量用更多的核并行计算」。
- [L221-L225](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L221-L225)：UB 切分，`BUFFER_NUM = 6` 的注释解释了 6 的来历（2 输入 + 1 输出 × double buffer）。
- [L227-L228](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L227-L228)：`SetBlockDim(usedCoreNum)`——这就是最终 kernel 启动的核数。
- [L230-L244](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L230-L244)：按 dtype 设置 TilingKey，float 走 `GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0)`（值为 0），int32 走 `MODE_1`（值为 1）。宏定义见 [examples/add_example/op_kernel/add_example_tiling_key.h:L21-L28](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28)，`GET_TPL_TILING_KEY` 本体由 toolkit 头 `ascendc/host_api/tiling/template_argument.h` 提供。TilingKey 如何被 kernel 侧消费，是 u3-l4 的主题，这里只需知道「float 和 int32 会走不同的模板实例化分支」。

#### 4.2.4 代码实践

**实践目标**：让 TilingFunc 把切分决策「说」出来——在每个核的元素数计算完成后打印日志，重新编译运行，观察 TilingData 三个字段的实际取值。

**操作步骤**：

1. 编辑 `examples/add_example/op_host/add_example_tiling.cpp`，在 `context->SetBlockDim(usedCoreNum);`（第 228 行）之前插入一行日志（示例代码）：

   ```cpp
   // 示例代码：打印 tiling 决策，便于观察切分结果
   OP_LOGI(context, "AddExample tiling: totalNum=%ld, blockFactor=%ld, usedCoreNum=%ld, ubFactor=%ld",
           tiling->totalNum, tiling->blockFactor, usedCoreNum, tiling->ubFactor);
   ```

   注意 `OP_LOGI` 是 INFO 级日志，需要提高日志级别才能看到（例如设置环境变量 `ASCEND_GLOBAL_LOG_LEVEL=0` 并配合 `ASCEND_SLOG_PRINT_TO_STDOUT=1`，具体名称以所用 CANN 版本文档为准，待本地验证）。

2. 按 u1-l3/u1-l4 的流程重新编译并安装算子包：

   ```bash
   ./build.sh --pkg --soc Ascend910B1 --ops add_example
   # 安装产物 run 包，source 环境后执行安装脚本
   ```

3. 运行样例：

   ```bash
   ./build.sh --run_example add_example eager
   ```

4. 在日志中查找 `AddExample tiling:` 开头的行，抄下四个数值。

**需要观察的现象**：

- 样例输入为 `8×16×2×2`（见 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/examples/test_aclnn_add_example.cpp) 中的 shape 构造），即 `totalNum = 8×16×2×2 = 512`。
- 设芯片 AIV 核数为 N，则 `blockFactor = ⌈512/N⌉`、`usedCoreNum = ⌈512/blockFactor⌉`。以 50 核为例：`blockFactor = ⌈512/50⌉ = 11`，`usedCoreNum = ⌈512/11⌉ = 47`——注意 47 < 50，正是「收缩核数」效应。
- `ubFactor` 是由 UB 大小算出的一个较大定值，与输入 shape 无关；当 `blockFactor < ubFactor` 时，kernel 内 `loopCount` 为 1，一批就搬完。

**预期结果**：日志数值满足上述公式；算子输出不变（只加日志不影响计算）。若无法在本地获得运行环境，以上数值推导可作为「待本地验证」的预判。

**进阶玩法**：把 `tiling->blockFactor = Ops::Base::CeilDiv(totalIdx, coreNum);` 临时改为 `tiling->blockFactor = totalIdx;`（只用 1 个核），重新编译运行，样例结果应仍然正确（正确性由 kernel 内的 `remainderLength` 逻辑保证，见 4.3.3），但可预期端到端耗时变长——这直观体现了「核切分换并行度」。改完记得还原。

#### 4.2.5 小练习与答案

**练习 1**：`totalNum = 1000`，`coreNum = 40`，求 `blockFactor` 与 `usedCoreNum`。

**答案**：`blockFactor = ⌈1000/40⌉ = 25`，恰好整除，`usedCoreNum = ⌈1000/25⌉ = 40`。若 `totalNum = 1001`，则 `blockFactor = ⌈1001/40⌉ = 26`，`usedCoreNum = ⌈1001/26⌉ = 39`——向上取整让每核多分一点后，反而少用一个核。

**练习 2**：为什么 `ubFactor` 公式里除以 6 而不是 3？

**答案**：AddExample 有 2 个输入队列 + 1 个输出队列共 3 个 TQue，每个队列使能 double buffer（`BUFFER_NUM = 2`，见 [add_example.h:L27](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.h#L27)），搬运和计算要能重叠进行，所以 3 × 2 = 6 块 UB tensor 必须同时驻留 UB，每块只能分到 1/6。

**练习 3**：如果把 [L225](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_host/add_example_tiling.cpp#L225) 中的 `FloorAlign(..., ubBlockSize)` 的对齐去掉，可能出什么问题？

**答案**：`ubFactor` 不再是 UB 最小访问粒度（ubBlockSize）的整数倍，`DataCopy` 搬运非对齐长度时性能下降甚至需要走 `DataCopyPad` 的补齐路径，还可能引发队列 buffer 分配失败。对齐是拿少量空间浪费换搬运效率的常规取舍。

### 4.3 TilingData 的 Host → Device 传递

#### 4.3.1 概念说明

TilingData 是 Host 与 Device 之间的一份「纯数据契约」：Host 侧把切分参数写进一个普通 C 结构体，框架把它随任务下发到 GM；Kernel 侧每个核启动后用同一个结构体类型把这块内存解包回来。**两侧 include 同一个头文件**（`add_example_tiling_data.h`），保证内存布局完全一致——这就是 u3-l1 总结过的「TilingData↔GET_TILING_DATA 跨侧约定」的具体形态。

#### 4.3.2 核心流程

```text
Host:  AddExampleTilingFunc 写入 AddExampleTilingData{totalNum, blockFactor, ubFactor}
          │  框架序列化，随任务下发
          ▼
Device(GM): tiling 数据块（kernel 第 5 个参数 GM_ADDR tiling）
          │  每个核启动时执行 GET_TILING_DATA_WITH_STRUCT 解包
          ▼
Kernel: tilingData->blockFactor / ubFactor 驱动 Init 与 Process
```

#### 4.3.3 源码精读

[examples/add_example/op_kernel/add_example_tiling_data.h:L19-L23](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23) —— 整个结构体只有三个 `int64_t` 字段，且给了默认值 0。注意它放在 `op_kernel` 目录而非 `op_host`，因为 Device 侧编译单元必须能 include 它（Host 侧也可以跨目录 include，`add_example_tiling.cpp` 第 24 行正是 `#include "../op_kernel/add_example_tiling_data.h"`）。复杂算子的 TilingData 会嵌套更多字段，但「Host/Kernel 共享同一头文件」的约定不变。

[examples/add_example/op_kernel/add_example.cpp:L36-L57](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.cpp#L36-L57) —— kernel 入口函数第 5 个参数 `GM_ADDR tiling` 就是下发到 GM 的 tiling 数据地址。第 40 行 `REGISTER_TILING_DEFAULT(AddExampleTilingData)` 注册结构体类型；第 42 行 `GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling)` 把 GM 数据解包为局部变量 `tilingData`。随后按模板参数 `schMode`（即 Host 侧 `SetTilingKey` 设置的值，由编译系统实例化出不同入口）分发到 `AddExample<float>` 或 `AddExample<int32_t>` 实现。

[examples/add_example/op_kernel/add_example.h:L57-L70](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.h#L57-L70) —— Kernel 消费 tiling 的方式，值得逐行看：

- 第 59 行：`remainderLength = totalNum - blockFactor * (GetBlockIdx() - 1)`——`GetBlockIdx()` 是当前核的编号（从 1 开始），算出「轮到我这核时还剩多少元素」。
- 第 60 行：`blockLength_ = min(remainderLength, blockFactor)`——非最后一个核取满 `blockFactor`，最后一个核取剩余量。这解释了为什么 tiling 侧的切分只需给出统一的 `blockFactor`，尾块处理由 kernel 自己完成。
- 第 63-65 行：`SetGlobalBuffer((__gm__ T*)x + blockFactor * GetBlockIdx(), blockLength_)`——注意这里用 `blockFactor * GetBlockIdx()` 计算本核的数据起点，配合第 59 行用 `(GetBlockIdx() - 1)` 算余量，两处下标约定（起点不含 0 号偏移、余量含 1 号起算）是成对出现的，改动任何一处都会引入 off-by-one。
- 第 67-69 行：`pipe.InitBuffer(..., ubLength_ * sizeof(T))`——用 `ubFactor` 给三个队列分配 UB 空间，正是 Host 侧「UB 除以 6」预留的预算。

[examples/add_example/op_kernel/add_example.h:L113-L123](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/examples/add_example/op_kernel/add_example.h#L113-L123) —— `Process()` 中 `loopCount = ⌈blockLength_ / ubLength_⌉`，把本核的任务再按 `ubFactor` 分批，每批走 CopyIn → Compute → CopyOut 三段流水。核间切分（blockFactor）与核内分批（ubFactor）在此汇合，构成完整的两级切分。

#### 4.3.4 代码实践

**实践目标**：从源码层面验证「尾块处理」逻辑，不动手编译。

**操作步骤**：

1. 假设 `totalNum = 512`、`blockFactor = 11`（对应 4.2.4 的 50 核场景），手工推演第 47 个核（`GetBlockIdx() = 47`）的 `remainderLength`、`blockLength_`、`loopCount`。
2. 对照 `add_example.h` 第 59-60 行与第 116 行写出计算过程。
3. 用 4.2.4 实践中打印出的真实 `blockFactor` 替换 11，重算真实末核的三个值。

**需要观察的现象**：末核的 `blockLength_` 明显小于 `blockFactor`，且当 `blockFactor` 是 `ubFactor` 的整数倍时，只有末核的 `loopCount` 内 `currentNum` 与 `ubLength_` 不同。

**预期结果**：以 512/11 为例，末核（第 47 核）`remainderLength = 512 - 11×46 = 6`，`blockLength_ = 6`，`loopCount = 1`，最后一批 `currentNum = 6`。此为纸面推导，可与 4.2.4 打印的日志互相印证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TilingData 结构体头文件放在 `op_kernel` 目录？

**答案**：因为结构体必须同时被 Host（写入）和 Device（解包）两个编译单元可见。Device 侧编译 kernel 时只能访问 `op_kernel` 目录下的文件，所以契约头放在 kernel 侧，Host 侧跨目录 include。若放在 op_host，kernel 编译单元就找不到它。

**练习 2**：kernel 里 `GetBlockIdx()` 从 1 开始，而 GM 偏移用 `blockFactor * GetBlockIdx()`，余量却用 `blockFactor * (GetBlockIdx() - 1)`，两者为什么不一致？

**答案**：这是该实现的既有约定：偏移计算相当于把第 1 核对应数据段 `[0, blockFactor)` 记在 `blockFactor * 1` 的「队列语义」下（TQue 编号习惯从 1 起），而余量计算回到元素语义。两处必须严格配对使用；阅读其他算子 kernel 时要先确认其下标约定，不能想当然。如果你在自己实现的算子里统一用 `GetBlockIdx() - 1` 偏移也是可以的，只要起点与长度配套。

### 4.4 公共层框架：TilingBaseClass

#### 4.4.1 概念说明

add_example 用的是「自由函数 + 子函数拆分」的写法，而仓库公共层 `common/inc/op_host/tiling_base.h` 提供了另一种「模板方法模式」的写法：基类 `TilingBaseClass` 把 tiling 流程固化为 `DoTiling()` 主干，子类只需覆写 8 个钩子函数。配合 `tiling_templates_registry.h` 的 `REGISTER_TILING_TEMPLATE` 系列宏，还能按优先级注册多个候选实现，框架依次尝试（`GRAPH_PARAM_INVALID` 表示「我不支持，换下一个」）。resize_bilinear_v2 等正式算子走的就是这条路，u3-l4 将展开。

#### 4.4.2 核心流程

`DoTiling()` 主干（模板方法）：

```text
DoTiling():
    GetShapeAttrsInfo()   → 失败即返回
    GetPlatformInfo()     → 失败即返回
    IsCapable()?          → false 则返回 GRAPH_PARAM_INVALID（让位给下一个候选类）
    DoOpTiling()          → 算子自身的切分
    DoLibApiTiling()      → 高阶 API 的 tiling
    GetWorkspaceSize()
    PostTiling()          → 保存 tiling 数据
    SetTilingKey(GetTilingKey())
    DumpTilingInfo()      → DEBUG 级打印 tiling 内容
```

#### 4.4.3 源码精读

[common/inc/op_host/tiling_base.h:L59-L101](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h#L59-L101) —— `DoTiling()` 的返回值语义写在注释里：`GRAPH_SUCCESS` 成功、`GRAPH_FAILED` 中止、`GRAPH_PARAM_INVALID` 表示本类不支持需要继续尝试其他 Tiling 类。这个三态协议是多策略 tiling（u3-l4 的 TilingKey 多分支）在类层面的基础。

[common/inc/op_host/tiling_base.h:L107-L123](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h#L107-L123) —— 8 个纯虚钩子：`IsCapable`/`GetPlatformInfo`/`GetShapeAttrsInfo`/`DoOpTiling`/`DoLibApiTiling`/`GetTilingKey`/`GetWorkspaceSize`/`PostTiling`。对照 4.2 的自由函数四步，会发现 add_example 的子函数拆分正是在模仿这套骨架——学会了 add_example，就等于预习了 `TilingBaseClass` 子类的写法。

[common/inc/op_host/tiling_base.h:L125-L141](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h#L125-L141) —— `DefaultTilingInfoDump()` 把 RawTilingData 按 `uint32_t` 逐个打印成十六进制/十进制串，超 640 字符自动分段。这正是本讲「观察 tiling 字段」实践的官方版本：把日志级别开到 DEBUG，tiling 内容会自动 dump 出来，无需手写 `OP_LOGI`。

[common/inc/op_host/tiling_base.h:L35-L57](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h#L35-L57) —— `AiCoreParams`/`CompileInfoCommon` 两个 POD 结构，集中描述平台资源（UB/L1/L0A/L0B/L0C、AIV/AIC 核数等），是 TilingParse 阶段固化下来的信息载体。

#### 4.4.4 代码实践

**实践目标**：用官方 dump 通道替代手写日志，观察 tiling 原始字节。

**操作步骤**：

1. 阅读 `DefaultTilingInfoDump()`，理解它打印的是 `GetRawTilingData()` 的原始 `uint32_t` 视图。
2. 推导：`AddExampleTilingData` 共 3 个 `int64_t` = 24 字节 = 6 个 `uint32_t`，所以 dump 会输出 6 个数（每个 int64 拆成低/高 32 位两个数）。
3. 若在 u4 阶段使用 `TilingBaseClass` 风格实现自定义算子，在调试期打开 DEBUG 日志（`ASCEND_GLOBAL_LOG_LEVEL` 设为 DEBUG 档，具体档位值以 CANN 文档为准，待本地验证），观察 dump 输出。

**需要观察的现象**：dump 行以 `Start to dump tiling info. tilingkey:...` 开头，后跟 6 个逗号分隔的整数；`totalNum=512` 时第 1、2 个数应为 512 和 0。

**预期结果**：能把手写 `OP_LOGI` 的字段值与 dump 的原始 32 位视图一一对应起来，加深「结构体 = 一块按约定布局的内存」的理解。add_example 未继承 `TilingBaseClass`，此实践在 u3-l4/u8 的模板化算子上做（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`DoTiling()` 返回 `GRAPH_PARAM_INVALID` 与 `GRAPH_FAILED` 有何区别？

**答案**：`GRAPH_FAILED` 表示 tiling 过程出错，整个流程中止；`GRAPH_PARAM_INVALID` 表示当前这个 Tiling 类不支持该输入场景（`IsCapable()` 为假），框架会继续尝试下一个注册的候选类。这是「多策略依次降级」的机制。

**练习 2**：add_example 没有用 `TilingBaseClass`，两种写法各有什么取舍？

**答案**：自由函数式（add_example）结构直白、上手快，适合教学与简单算子；模板类式（`TilingBaseClass` + 注册宏）把公共流程与调试设施（自动 dump、context 管理）固化在基类，适合多策略、多芯片、需要按优先级降级的正式算子。两者的决策产物（TilingData、BlockDim、TilingKey）完全一致。

## 5. 综合实践

**任务：给 AddExample 做一次「tiling 决策全景观察」。**

综合本讲四个模块，完成以下闭环：

1. **理论推演**：设输入 shape 为 `8×16×2×2`（totalNum=512）、芯片 AIV 核数 50、UB 为 196608 字节、`ubBlockSize` 为 32（数值为假设，用于推演），手工算出 `blockFactor`、`usedCoreNum`、`ubFactor`，并推演第 1 核、中间核、末核各自的 `blockLength_` 与 `loopCount`。
2. **代码修改**：在 `AddExampleTilingFunc` 的 `SetBlockDim` 之前加一条 `OP_LOGI`（4.2.4 给出的示例代码），打印四个决策值。
3. **编译运行**：按 u1-l4 流程 `./build.sh --pkg --soc <soc> --ops add_example`，安装 run 包，`./build.sh --run_example add_example eager`，开启 INFO 日志，抄录实际打印值，与推演值对比（核数、UB 大小按实际环境替换）。
4. **对照 kernel**：拿实际 `blockFactor`/`ubFactor`，对照 `add_example.h` 的 `Init`/`Process` 逐行解释第 47 核（或实际末核）搬了几批数据、每批多少元素。
5. **还原因状**：删除日志行，确认源码回到原始状态（本仓库禁止把练习改动留在工作区）。

产出物：一页推演记录 + 一段日志摘录 + 一段对末核执行路径的文字解释。若本地无 NPU 环境，第 1、4 步的纸面推演仍可完成，第 3 步标注「待本地验证」。

## 6. 本讲小结

- **Tiling 的本质是 Host 侧提前做切分决策**：核间并行靠 `blockFactor` + `SetBlockDim`，核内分批靠 `ubFactor`（UB 容量 ÷ 元素大小 ÷ buffer 块数再对齐），两级切分共同决定 kernel 的执行形态。
- **TilingFunc 的标准步骤**：`GetPlatformInfo`（核数/UB）→ `GetShapeAttrsInfo`（元素数/dtype 校验）→ `GetWorkspaceSize`（AddExample 为 0）→ 填充 TilingData → `SetBlockDim` + `SetTilingKey`，最后经 `IMPL_OP_OPTILING(...).Tiling(...).TilingParse<...>(...)` 注册。
- **TilingData 是 Host/Device 共享头文件定义的纯数据契约**：kernel 第 5 个参数 `GM_ADDR tiling` 经 `REGISTER_TILING_DEFAULT` + `GET_TILING_DATA_WITH_STRUCT` 解包，末核尾块由 kernel 用 `remainderLength` 自行处理。
- **TilingKey 是实现分支选择器**：AddExample 按 dtype 设 0（float）/1（int32），kernel 侧 `if constexpr` 按模板参数分发——这是 u3-l4 多策略机制的伏笔。
- **公共层 `TilingBaseClass` 把 tiling 流程模板化为 8 个钩子**，返回三态（SUCCESS/FAILED/PARAM_INVALID）支持多候选降级，并提供 DEBUG 级 tiling dump 设施。
- **常用的整数技巧**：`CeilDiv` 收缩核数、`FloorAlign` 对齐 UB 粒度、double buffer 使 buffer 块数翻倍——这些小工具在所有算子的 tiling 里反复出现。

## 7. 下一步学习建议

下一讲 **u3-l4（TilingKey 多策略与多架构适配）** 将深入 `tiling_templates_registry.h` 的模板注册宏与 `resize_bilinear_v2` 的 arch35 多架构 tiling，讲清楚一个算子如何用 TilingKey 在「同一份代码」里区分几十种 dtype/shape/芯片组合。建议提前浏览：

- [common/inc/op_host/tiling_templates_registry.h:L318-L341](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_templates_registry.h#L318-L341) 的 `REGISTER_TILING_TEMPLATE` 系列宏。
- `image/resize_bilinear_v2/op_host/arch35/` 下的多架构 tiling 文件，对照本讲的四步骨架找同构之处。
- 若想先看 kernel 侧如何按 key 分发，可预习 u4-l1 的 `add_example.cpp` 模板分发（本讲 4.3.3 已初见）。
