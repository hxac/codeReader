# op_api 层：aclnn 接口的两段式设计

## 1. 本讲目标

上一讲（u2-l1）我们读了 op_host 里的 `*_def.cpp`，知道了算子的「签名」是如何注册的。本讲向下游走一层，进入 `op_api` 目录，精读 `aclnn_ai_infra_scatter_block_update.cpp` 这组文件。学完本讲，你应该能够：

1. 说清 **GetWorkspaceSize 与执行函数两段式接口**各自做什么、为什么这样切分。
2. 掌握 **参数三步检查**（空指针 → 空张量 → dtype 合法性）的固定套路，并能照着写出同样的检查代码。
3. 理解 `l0op::Contiguous` 与 `executor->CreateView` 两种处理非连续输入的手段，以及本算子为什么对 `input` 特意不用 Contiguous。
4. 看懂 `l0op::AiInfraScatterBlockUpdate` 这个 L0 封装如何把算子挂进 executor 的下发列表。

本讲仍以仓库中体量最小的算子 `ai_infra_scatter_block_update` 为标本。它虽然小，但 op_api 层的所有要素都齐了——这正是选它做「解剖标本」的原因。

## 2. 前置知识

本讲会用到下面几个概念，先用通俗语言过一遍：

- **aclnn 接口**：CANN 对外暴露的标准算子调用接口，C 风格函数（`extern "C"`），函数名以 `aclnn` 开头。PyTorch 侧的 csrc 代码最终 dlsym 找到的就是这一层的符号（第 3 单元会看到）。
- **Host 侧 / Device 侧**：Host 指 CPU 侧，Device 指 NPU 侧。参数检查、算子下发计划都在 Host 侧完成；真正的数值计算在 Device 侧执行。
- **workspace**：算子在 Device 侧执行时需要的临时工作内存。比如把非连续输入拷贝成连续输入，拷贝的中间结果就放在 workspace 里。它的大小取决于输入形状，只有算子「规划」完执行方案后才知道。
- **aclOpExecutor（算子执行器）**：本讲的头号主角。它是一个记录容器：aclnn 层每调用一个 L0 算子，就往它里面登记一条「待下发算子」；同时它统计这些算子一共需要多大的 workspace。第一段接口把它造出来并填好，第二段接口拿它去真正下发执行。
- **stream（流）**：昇腾的异步执行队列。算子下发到 stream 后立即返回，真正的执行异步完成——所以第二段接口是「下发」而非「等待完成」。
- **aclnnStatus 错误码**：本讲会遇到 `ACLNN_SUCCESS`（成功）、`ACLNN_ERR_PARAM_NULLPTR`（参数空指针）、`ACLNN_ERR_PARAM_INVALID`（参数非法）、`ACLNN_ERR_INNER_NULLPTR`（内部空指针）、`ACLNN_ERR_INNER_CREATE_EXECUTOR`（创建执行器失败）。
- **L0 算子**：CANN 内部对最底层单算子封装的称呼，代码里体现为 `l0op` 命名空间。aclnn 层（有时也称 L2 层）不直接拼 kernel，而是调 `l0op::Xxx` 把算子登记进 executor。

另外两个贯穿全讲的宏，先记住行为（它们的完整定义在 CANN 的头文件里，本仓库只有 UT 用的副本，见 4.1.3）：

- `CHECK_RET(cond, err)`：条件不成立时打印日志并 `return err`。
- `OP_LOGE(errCode, fmt, ...)`：按错误码打 error 日志。

## 3. 本讲源码地图

本讲的四个主角文件都在 `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/` 下，外加两个「参考资料」：

| 文件 | 层 | 职责 |
|---|---|---|
| `op_api/aclnn_ai_infra_scatter_block_update.h` | aclnn 接口声明 | 声明两段式接口，注释里写了算子功能、每个输入的 shape/dtype 约束 |
| `op_api/aclnn_ai_infra_scatter_block_update.cpp` | aclnn 接口实现 | 参数三步检查、Contiguous/CreateView 连续化、创建 executor、调 L0 算子 |
| `op_api/ai_infra_scatter_block_update.h` | L0 封装声明 | 声明 `l0op::AiInfraScatterBlockUpdate` |
| `op_api/ai_infra_scatter_block_update.cpp` | L0 封装实现 | `OP_TYPE_REGISTER` + `ADD_TO_LAUNCHER_LIST_AICORE` 把算子挂进下发列表 |
| `common/stub/op_api/aclnn_kernels/common/op_error_check.h` | 参考资料 | `OP_CHECK_DTYPE_NOT_SUPPORT` 等检查宏的仓库侧副本（真实定义在 CANN 里） |
| `tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp` | 参考/实践 | 无硬件的 op_api 单测，验证三步检查的报错行为 |

一个命名规律先记下：**带 `aclnn_` 前缀的是对外的两段式接口，不带前缀的同名 `.cpp/.h` 是给 aclnn 层调用的 L0 封装**。每个算子的 op_api 目录基本都是这两对文件。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**aclOpExecutor 与两段式接口** → **参数三步检查** → **非连续输入的处理** → **L0 算子调用**。它们正好对应 `aclnnXxxGetWorkspaceSize` 函数体从上到下的执行顺序。

### 4.1 aclOpExecutor 与两段式接口

#### 4.1.1 概念说明

aclnn 接口从来不是「一个函数干完所有事」，而是固定拆成两段：

- **第一段 `aclnnXxxGetWorkspaceSize(...)`**：同步执行。做参数检查、创建 `aclOpExecutor`、把要执行的算子登记进 executor（这一步会连带触发 tiling 计算，下一讲展开），最后告诉调用方「需要多大的 workspace」，并把填好的 executor 交给调用方。
- **第二段 `aclnnXxx(workspace, workspaceSize, executor, stream)`**：把调用方申请好的 workspace 内存、第一段产出的 executor 和一条 stream 交给运行时，异步下发执行。

为什么这么切？两个原因：

1. **workspace 大小只有规划完才知道**。它依赖输入形状、是否需要 Contiguous 拷贝、tiling 方案等运行期信息，不可能写成编译期常量。调用方必须先拿到尺寸、在 Device 侧申请好内存，才有东西可传。
2. **错误要尽早、同步地暴露**。参数非法、dtype 不支持这类问题在第一段就能发现并返回错误码，调用方根本不会走到下发那一步；第二段则保持极薄，只做下发，适合塞进异步流水线。

`aclOpExecutor` 就是横跨两段的「接力棒」：第一段往里写，第二段读它执行。

#### 4.1.2 核心流程

以本算子为例，两段式的完整时序：

```text
调用方（框架 / csrc 层）
   │
   ├─① 调 aclnnAiInfraScatterBlockUpdateGetWorkspaceSize(input, indices, update,
   │        &workspaceSize, &executor)          ← 第一段，Host 侧同步
   │      ├─ L2_DFX_PHASE_1 打点
   │      ├─ CREATE_EXECUTOR() 创建 uniqueExecutor（RAII 智能指针）
   │      ├─ CommonProcess：
   │      │    ├─ 三步参数检查
   │      │    ├─ input   → CreateView（保留 stride，原地写回）
   │      │    ├─ indices → l0op::Contiguous（保证连续）
   │      │    ├─ update  → l0op::Contiguous（保证连续）
   │      │    └─ l0op::AiInfraScatterBlockUpdate(...)  ← 登记进 executor 的下发列表
   │      ├─ *workspaceSize = executor->GetWorkspaceSize()
   │      └─ uniqueExecutor.ReleaseTo(executor)  ← 所有权移交给调用方
   │
   ├─② 按 workspaceSize 在 Device 侧申请 workspace 内存
   │
   └─③ 调 aclnnAiInfraScatterBlockUpdate(workspace, workspaceSize, executor, stream)
            └─ CommonOpExecutorRun(...)          ← 第二段，按 stream 异步下发
```

注意一个细节：第一段里创建的是 `uniqueExecutor`（独占所有权的 RAII 对象）。中途任何一步失败，函数直接 return，`uniqueExecutor` 自动析构，不泄漏；全部成功后才用 `ReleaseTo(executor)` 把所有权交给出参 `executor`。这是标准的「智能指针构造、裸指针交付」模式。

#### 4.1.3 源码精读

先看头文件里两段接口的声明。第一段声明在 [aclnn_ai_infra_scatter_block_update.h:L37-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L37-L39)，出参是 `workspaceSize` 和 `executor`；第二段声明在 [aclnn_ai_infra_scatter_block_update.h:L51-L52](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L51-L52)，入参是第一段的两个产物加 `stream`。头文件注释还写清了每个输入的 shape 与 dtype 约束（见 [aclnn_ai_infra_scatter_block_update.h:L24-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L24-L32)）：`input` 为 `[block_num, block_size, D]`、`indices` 为 `[T, 2]`、`update` 为 `[T, D]`——这份注释就是 4.2 节检查代码的「需求说明书」。

第一段的实现（逐行注释）：

```cpp
// [aclnn_ai_infra_scatter_block_update.cpp:L139-L154]
aclnnStatus aclnnAiInfraScatterBlockUpdateGetWorkspaceSize(
    aclTensor *input, const aclTensor *indices, const aclTensor *update,
    uint64_t *workspaceSize, aclOpExecutor **executor)
{
    L2_DFX_PHASE_1(aclnnAiInfraScatterBlockUpdate, DFX_IN(input, indices, update), DFX_OUT(input));
    // ↑ 第一段打点宏（来自 opdev/op_dfx.h），记录输入输出元信息，用于性能流水分析

    auto uniqueExecutor = CREATE_EXECUTOR();              // 创建 RAII 执行器
    CHECK_RET(uniqueExecutor.get() != nullptr, ACLNN_ERR_INNER_CREATE_EXECUTOR);

    auto ret = aclnnAiInfraScatterBlockUpdateCommonProcess(input, indices, update, uniqueExecutor.get());
    CHECK_RET(ret == ACLNN_SUCCESS, ret);                 // 检查+连续化+登记 L0 算子

    *workspaceSize = uniqueExecutor->GetWorkspaceSize();  // 汇总 workspace 需求
    uniqueExecutor.ReleaseTo(executor);                   // 所有权交给调用方
    return ACLNN_SUCCESS;
}
```

对应链接：[aclnn_ai_infra_scatter_block_update.cpp:L139-L154](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L139-L154)。

第二段的实现薄到只有三行：

```cpp
// [aclnn_ai_infra_scatter_block_update.cpp:L156-L161]
aclnnStatus aclnnAiInfraScatterBlockUpdate(void *workspace, uint64_t workspaceSize, aclOpExecutor *executor,
    aclrtStream stream)
{
    L2_DFX_PHASE_2(aclnnAiInfraScatterBlockUpdate);       // 第二段打点
    return CommonOpExecutorRun(workspace, workspaceSize, executor, stream);
}
```

对应链接：[aclnn_ai_infra_scatter_block_update.cpp:L156-L161](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L156-L161)。`CommonOpExecutorRun` 由 CANN opdev 运行时提供（本仓库无其源码），职责是把第一段登记进 executor 的算子序列在指定 stream 上异步下发。

关于 `CREATE_EXECUTOR` 宏：它的真实定义在 CANN 的 `opdev/make_op_executor.h` 中，本仓库不含该头文件，但 **UT 测试框架留了一份可读的 stub 副本**，能看出它的基本形态：

```cpp
// [tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h:L19]
#define CREATE_EXECUTOR() UniqueExecutor(__func__)
```

即：以当前函数名为参数构造一个 `UniqueExecutor`（链接：[make_op_executor.h:L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h#L19)）。同一文件里 `ADD_TO_LAUNCHER_LIST_AICORE` 的 stub 直接返回 `ACLNN_SUCCESS`（[make_op_executor.h:L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h#L23)）——这正是 op_api 单测不需要真硬件也能跑通「第一段全流程」的原因之一（见 4.4.3）。

#### 4.1.4 代码实践

**实践目标**：把两段式的「分工」从文字变成自己画的图，并从源码验证每个环节。

**操作步骤**：

1. 打开 [aclnn_ai_infra_scatter_block_update.h:L20-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L20-L52)，把两段接口的参数逐个抄到笔记上，标注每个参数是「入」还是「出」、由谁生产。
2. 对照 4.1.2 的时序图，检查图中每个箭头在 `aclnn_ai_infra_scatter_block_update.cpp` 的 L139-L161 里都有对应代码行，把行号标到图上。
3. 回答两个问题（写在笔记里）：
   - `uniqueExecutor` 为什么要用 `ReleaseTo` 交出所有权，而不是直接 `*executor = uniqueExecutor.get()`？
   - 如果调用方不检查第一段返回值、第一段失败后仍调用第二段，会发生什么？

**需要观察的现象**：无运行环境时，本实践是源码阅读型；重点观察「第一段厚、第二段薄」的代码形态。

**预期结果**：能说出——直接 `get()` 会让 RAII 对象出作用域时析构掉 executor，调用方拿到悬空指针；`ReleaseTo` 是放弃管理权的正确方式。第一段失败后 executor 未产出（或已析构），第二段拿到的指针无效，行为未定义——所以调用方（第 3 单元的 csrc 层）必须先检查第一段返回值。**待本地验证**：若你有昇腾环境，可在第三步故意传一个空指针观察第一段报错、第二段不执行的现象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 workspace 大小不写成固定值，而要在第一段动态计算？

**答案**：workspace 需求取决于运行期信息——输入形状、tiling 结果、Contiguous 是否产生中间张量等。不同 shape 的请求需要的 workspace 不同，只有第一段把执行方案规划完（登记完所有 L0 算子）才能汇总出 `GetWorkspaceSize()`，所以必须以出参形式动态返回。

**练习 2**：`L2_DFX_PHASE_1` / `L2_DFX_PHASE_2` 是做什么的？删掉它们算子还能正常算吗？

**答案**：它们是 CANN opdev 提供的 DFX（可维护性/可观测性）打点宏，分别在两段入口记录元信息，用于性能流水分析，不参与数值计算。删掉不影响计算结果，但会丢失性能定位能力，实践中不应删除。

**练习 3**：第二段函数只有一行有效代码，这样设计的好处是什么？

**答案**：所有逻辑（检查、连续化、算子登记）都集中在第一段同步完成并可同步报错；第二段只做「按 stream 异步下发」，开销极小、可安全放进异步流水线，同时让两段之间的唯一状态载体就是 executor，接口边界清晰。

### 4.2 参数三步检查

#### 4.2.1 概念说明

算子是给上层框架调的，上层可能传任何东西：空指针、空张量、不支持的 dtype。三步检查就是把这些非法输入**挡在 Host 侧同步阶段**，一旦发现立即打日志、返回错误码，绝不带病下发到 Device。本算子的三步是：

1. **NotNull**：指针层面是否为空——`input == nullptr` 这类。
2. **EmptyTensor**：张量是否为空（shape 某维为 0，元素数为 0）。
3. **DtypeValid**：数据类型是否在支持列表内，且相互之间约束满足（如 input 与 update 同 dtype）。

三步的**顺序不能换**：第 2、3 步要调用 `input->IsEmpty()`、`input->GetDataType()`，都是对指针解引用——必须先确认指针非空。这是「先判空、再解引用」的防御式编程次序。

检查依据来自两处：头文件注释里的 shape/dtype 约束（4.1.3 已引），以及 dtype 支持列表常量。

#### 4.2.2 核心流程

```text
aclnnAiInfraScatterBlockUpdateCommonProcess(input, indices, update, executor)
   └─ CheckAiInfraScatterBlockUpdateParams
        ├─ ① NotNull：任一为 nullptr → OP_LOGE(ACLNN_ERR_PARAM_NULLPTR) + return false
        ├─ ② EmptyTensor：任一 IsEmpty() → OP_LOGE(ACLNN_ERR_PARAM_INVALID) + return false
        └─ ③ DtypeValid：
             ├─ input  ∈ {FLOAT, FLOAT16, BF16, INT8}   否则报 ACLNN_ERR_PARAM_INVALID
             ├─ indices ∈ {INT64, INT32}                否则报 ACLNN_ERR_PARAM_INVALID
             └─ input.dtype == update.dtype             否则报 ACLNN_ERR_PARAM_INVALID
   （全部通过后才进入 4.3 的连续化处理）
```

错误码使用约定（也是仓库的统一风格）：

| 错误码 | 触发场景 |
|---|---|
| `ACLNN_ERR_PARAM_NULLPTR` | 调用方传了空指针（第①步） |
| `ACLNN_ERR_PARAM_INVALID` | 空 tensor、dtype 不支持、dtype 不匹配（第②③步） |
| `ACLNN_ERR_INNER_NULLPTR` | 内部产生的对象为空，如 Contiguous 返回 nullptr（见 4.3） |
| `ACLNN_ERR_INNER_CREATE_EXECUTOR` | CREATE_EXECUTOR 失败（见 4.1） |

#### 4.2.3 源码精读

先看 dtype 支持列表的定义（[aclnn_ai_infra_scatter_block_update.cpp:L33-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L33-L39)）：

```cpp
static const std::initializer_list<DataType> SCATTER_INPUT_DTYPE_SUPPORT_LIST = {
    DataType::DT_FLOAT, DataType::DT_FLOAT16,
    DataType::DT_BF16, DataType::DT_INT8};

static const std::initializer_list<DataType> SCATTER_INDEX_DTYPE_SUPPORT_LIST = {
    DataType::DT_INT64, DataType::DT_INT32};
```

三步检查的实现（节选关键部分）：

```cpp
// 第①步 NotNull —— [aclnn_ai_infra_scatter_block_update.cpp:L42-L57]
bool CheckAiInfraScatterBlockUpdateNotNull(aclTensor *input, const aclTensor *indices, const aclTensor *update)
{
    if (input == nullptr) {
        OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "input tensor is nullptr");
        return false;
    }
    // indices、update 同样写法……
    return true;
}

// 第②步 EmptyTensor —— [aclnn_ai_infra_scatter_block_update.cpp:L59-L74]
bool CheckAiInfraScatterBlockUpdateEmptyTensor(aclTensor *input, const aclTensor *indices, const aclTensor *update)
{
    if (input->IsEmpty()) {                       // 能安全解引用，因为第①步已保证非空
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "input tensor is empty");
        return false;
    }
    // ……
}

// 第③步 DtypeValid —— [aclnn_ai_infra_scatter_block_update.cpp:L76-L89]
bool CheckAiInfraScatterBlockUpdateDtypeValid(aclTensor *input, const aclTensor *indices, const aclTensor *update)
{
    OP_CHECK_DTYPE_NOT_SUPPORT(input, SCATTER_INPUT_DTYPE_SUPPORT_LIST, return false);
    OP_CHECK_DTYPE_NOT_SUPPORT(indices, SCATTER_INDEX_DTYPE_SUPPORT_LIST, return false);
    if (input->GetDataType() != update->GetDataType()) {
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "update dtype %s should be the same as input dtype %s.",
                op::ToString(update->GetDataType()).GetString(), op::ToString(input->GetDataType()).GetString());
        return false;
    }
    return true;
}
```

三个函数由总装函数按序调用（[aclnn_ai_infra_scatter_block_update.cpp:L91-L103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L91-L103)）：

```cpp
aclnnStatus CheckAiInfraScatterBlockUpdateParams(aclTensor *input, const aclTensor *indices, const aclTensor *update)
{
    // 1. 检查参数是否为空指针
    CHECK_RET(CheckAiInfraScatterBlockUpdateNotNull(input, indices, update), ACLNN_ERR_PARAM_NULLPTR);
    // 2. 检查是否为空tensor（本算子不支持空tensor）
    CHECK_RET(CheckAiInfraScatterBlockUpdateEmptyTensor(input, indices, update), ACLNN_ERR_PARAM_INVALID);
    // 3. 检查输入的数据类型是否在支持的数据类型范围之内
    CHECK_RET(CheckAiInfraScatterBlockUpdateDtypeValid(input, indices, update), ACLNN_ERR_PARAM_INVALID);
    return ACLNN_SUCCESS;
}
```

其中 `OP_CHECK_DTYPE_NOT_SUPPORT` 宏在仓库的 stub 副本里可以看到展开逻辑（真实定义在 CANN 的 `aclnn_kernels/common/op_error_check.h`）：

```cpp
// [common/stub/op_api/aclnn_kernels/common/op_error_check.h:L145-L150]
#define OP_CHECK_DTYPE_NOT_SUPPORT(tensor, supportList, retExpr) \
  if (!CheckType(tensor->GetDataType(), supportList)) { \
    OP_LOGE(ACLNN_ERR_PARAM_INVALID, "Tensor %s not implemented for %s, should be in dtype support list %s.", \
            #tensor, op::ToString(tensor->GetDataType()).GetString(), op::ToString(supportList).GetString()); \
    retExpr; \
  }
```

即「dtype 不在列表 → 打日志 → 执行第三个参数传入的语句（这里是 `return false`）」。链接：[op_error_check.h:L145-L150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/common/op_error_check.h#L145-L150)。这个 stub 文件里还有 `OP_CHECK_SHAPE_NOT_EQUAL`、`OP_CHECK_BROADCAST` 等一族现成的检查宏（[op_error_check.h:L140-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/common/op_error_check.h#L140-L257)），写新算子时按需取用。

这套检查不是摆设——UT 里专门有对应报错用例。例如传空指针的用例（[test_aclnn_ai_infra_scatter_block_update.cpp:L252-L262](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp#L252-L262)）直接调第一段接口、断言返回值不等于 `ACLNN_SUCCESS`；indices 传 FP32 的非法 dtype 用例在 [test_aclnn_ai_infra_scatter_block_update.cpp:L308-L322](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp#L308-L322)。

#### 4.2.4 代码实践

**实践目标**：为假想算子 `my_add`（两个输入 `x`、`y`，逐元素相加）写出与 scatter 算子同风格的三步检查。

**操作步骤**：

1. 新建一个练习文件（放在仓库外的任意目录即可，不要改动源码），仿照 4.2.3 的结构编写（**示例代码**）：

```cpp
// 示例代码：my_add 的三步检查骨架（仅练习用，非仓库原有代码）
static const std::initializer_list<DataType> MY_ADD_DTYPE_SUPPORT_LIST = {
    DataType::DT_FLOAT, DataType::DT_FLOAT16, DataType::DT_BF16};

bool CheckMyAddNotNull(const aclTensor *x, const aclTensor *y)
{
    if (x == nullptr) {
        OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "x tensor is nullptr");
        return false;
    }
    if (y == nullptr) {
        OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "y tensor is nullptr");
        return false;
    }
    return true;
}

bool CheckMyAddEmptyTensor(const aclTensor *x, const aclTensor *y)
{
    if (x->IsEmpty()) {
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "x tensor is empty");
        return false;
    }
    if (y->IsEmpty()) {
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "y tensor is empty");
        return false;
    }
    return true;
}

bool CheckMyAddDtypeValid(const aclTensor *x, const aclTensor *y)
{
    OP_CHECK_DTYPE_NOT_SUPPORT(x, MY_ADD_DTYPE_SUPPORT_LIST, return false);
    OP_CHECK_DTYPE_NOT_SUPPORT(y, MY_ADD_DTYPE_SUPPORT_LIST, return false);
    if (x->GetDataType() != y->GetDataType()) {
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "y dtype %s should be the same as x dtype %s.",
                op::ToString(y->GetDataType()).GetString(), op::ToString(x->GetDataType()).GetString());
        return false;
    }
    return true;
}

aclnnStatus CheckMyAddParams(const aclTensor *x, const aclTensor *y)
{
    CHECK_RET(CheckMyAddNotNull(x, y), ACLNN_ERR_PARAM_NULLPTR);
    CHECK_RET(CheckMyAddEmptyTensor(x, y), ACLNN_ERR_PARAM_INVALID);
    CHECK_RET(CheckMyAddDtypeValid(x, y), ACLNN_ERR_PARAM_INVALID);
    return ACLNN_SUCCESS;
}
```

2. 对照 UT 的异常用例（`Ascend910B_input_nullptr`、`Ascend910B_dtype_mismatch`、`Ascend910B_indices_unsupported_dtype`）给自己出题：`my_add` 应该补哪些对应的异常用例？

**需要观察的现象**：写作过程中体会「每个检查函数只管一类问题、每个分支都带 `OP_LOGE` + 错误码、总装函数用 `CHECK_RET` 串联」的模式。

**预期结果**：得到一份错误码风格与 scatter 完全一致的检查代码；能列出至少 3 个 my_add 的异常用例（x 为 nullptr、x 为空张量、x 传 INT32）。**待本地验证**：编译运行需要完整 CANN 环境。

#### 4.2.5 小练习与答案

**练习 1**：把第②步（EmptyTensor）挪到第①步（NotNull）之前，会发生什么？

**答案**：`input->IsEmpty()` 会在 `input == nullptr` 时解引用空指针，直接崩溃（未定义行为），而不是优雅返回 `ACLNN_ERR_PARAM_NULLPTR`。所以「先判空再解引用」的次序不可动摇。

**练习 2**：`indices` 为什么只支持 INT32/INT64，传 FP32 会被哪一行拒绝？

**答案**：indices 是索引张量，语义上必须是整数。FP32 会被 `CheckAiInfraScatterBlockUpdateDtypeValid` 中的 `OP_CHECK_DTYPE_NOT_SUPPORT(indices, SCATTER_INDEX_DTYPE_SUPPORT_LIST, return false)`（[aclnn_ai_infra_scatter_block_update.cpp:L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L81)）拒绝，宏展开后打日志并 `return false`，最终第一段返回 `ACLNN_ERR_PARAM_INVALID`。

**练习 3**：`input` 支持 INT8，但 `update` 也必须是 INT8 吗？

**答案**：是的。第③步最后一段显式要求 `input->GetDataType() == update->GetDataType()`（[aclnn_ai_infra_scatter_block_update.cpp:L83-L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L83-L87)）。update 要原样写进 input，dtype 不一致就没有明确的转换语义，索性在入口拒绝。

### 4.3 非连续输入的处理：l0op::Contiguous 与 executor->CreateView

#### 4.3.1 概念说明

**什么是连续（contiguous）张量**：shape 为 \((d_0, d_1, \ldots, d_{n-1})\) 的张量，若其 stride 恰为 \( s_i = \prod_{j=i+1}^{n-1} d_j \) 且存储偏移为 0，就说它连续——元素在内存里按行主序紧挨着排布。转置、切片、expand 等操作都会产出非连续张量（stride 不满足上式，或偏移非 0）。

**为什么要处理**：AscendC kernel 为了搬数高效，普遍假设输入在 GM（Global Memory）里按一维线性地址排布。非连续输入要么先拷贝成连续，要么 kernel 自己按 stride 算地址。

本算子对两类输入用了**两种不同手段**，这是全文件最值得咀嚼的设计：

- `indices`、`update`：只读输入，用 **`l0op::Contiguous`**——已连续则原样返回，不连续则产生一份连续拷贝（这份中间张量由 executor 管理，占 workspace）。拷贝不影响语义，因为它们只被读。
- `input`：**既是要读的输入，更是原地写入的目标**。如果对它 Contiguous，非连续时会得到一份拷贝，kernel 把 update 写进拷贝里，用户手里的原张量纹丝不动——语义直接错了。所以改用 **`executor->CreateView`**：不拷贝任何数据，只是基于原张量造一个新的 `aclTensor` 描述，完整保留 viewShape、storageShape、viewStrides、viewOffset，让 kernel 侧自己按 stride 偏移定位到真实地址写回。

源码里的注释把动机说得很直白（L111-L112）：「input不执行Contiguous，通过CreateView保留stride/storageShape/offset信息，在算子内部通过stride偏移计算地址」。

#### 4.3.2 核心流程

```text
              ┌─ IsContiguous(input)?
input ────────┤   是 → l0op::Contiguous(input)：连续时原样返回，不拷贝
              │   否 → executor->CreateView(input, viewShape, storageShape,
              │        viewStrides, viewOffset)   ← 零拷贝，仅新建张量描述
indices ──────┴── l0op::Contiguous(indices)  → 不连续时产生连续拷贝（进 workspace）
update  ───────── l0op::Contiguous(update)   → 不连续时产生连续拷贝（进 workspace）

三个张量随后一起传给 l0op::AiInfraScatterBlockUpdate(...)
kernel 按 indices 的 (block, slot) 定位，把 update 的行写到 inputView
按 stride 计算出的真实地址 —— 用户原 input 被原地更新
```

#### 4.3.3 源码精读

`CommonProcess` 中连续化与 L0 调用的完整代码（[aclnn_ai_infra_scatter_block_update.cpp:L105-L137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L105-L137)）：

```cpp
static aclnnStatus aclnnAiInfraScatterBlockUpdateCommonProcess(aclTensor *input, const aclTensor *indices,
    const aclTensor *update, aclOpExecutor *executor)
{
    auto ret = CheckAiInfraScatterBlockUpdateParams(input, indices, update);   // 4.2 的三步检查
    CHECK_RET(ret == ACLNN_SUCCESS, ret);

    // input不执行Contiguous，通过CreateView保留stride/storageShape/offset信息，
    // 在算子内部通过stride偏移计算地址
    aclTensor *inputView;

    if (IsContiguous(input)) {
        inputView = const_cast<aclTensor*>(l0op::Contiguous(const_cast<aclTensor*>(input), executor));
    } else {
        inputView = executor->CreateView(input, input->GetViewShape(),          // 零拷贝视图
            input->GetStorageShape(), input->GetViewStrides(), input->GetViewOffset());
    }

    CHECK_RET(inputView != nullptr, ACLNN_ERR_INNER_NULLPTR);

    // 将输入indices转换成连续的tensor
    auto indicesContiguous = l0op::Contiguous(indices, executor);
    CHECK_RET(indicesContiguous != nullptr, ACLNN_ERR_INNER_NULLPTR);

    // 将输入update转换成连续的tensor
    auto updateContiguous = l0op::Contiguous(update, executor);
    CHECK_RET(updateContiguous != nullptr, ACLNN_ERR_INNER_NULLPTR);

    // 调用L0算子AiInfraScatterBlockUpdate进行原地更新计算
    auto result = l0op::AiInfraScatterBlockUpdate(inputView, indicesContiguous, updateContiguous, executor);
    CHECK_RET(result != nullptr, ACLNN_ERR_INNER_NULLPTR);

    return ACLNN_SUCCESS;
}
```

几个细节：

- `inputView` 分支在 **L115-L120**（[链接](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L115-L120)）：注意已连续时走的也是 `l0op::Contiguous`——对连续张量它就是「原样返回」，不发生拷贝；所以 `input` 这条路**任何情况下都不拷贝数据**。
- `l0op::Contiguous` 来自 CANN 的 `aclnn_kernels/contiguous.h`（本文件 L23 include，[链接](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L23)），`IsContiguous` 是 opdev 提供的判定函数；两者源码都在 CANN 中，本仓库不含。
- `CreateView` 返回的 `aclTensor*` 带 const 转换处理（L116 的 `const_cast`）：`Contiguous` 返回 `const aclTensor*`，而 `inputView` 之后要作为**可写**的第一参数传给 L0 算子，所以剥掉 const。
- 三个中间结果都判空并返回 `ACLNN_ERR_INNER_NULLPTR`——区别于 4.2 的参数错误：这里失败不是调用方传错了，而是运行时内部生成对象失败。
- Contiguous 产生的拷贝张量统一挂在 `executor` 上（每个 Contiguous 调用都传入 executor），生命周期由 executor 管理，随第二段执行完一起释放——这也解释了为什么它们会占用 workspace、并影响 `GetWorkspaceSize()` 的返回值。

#### 4.3.4 代码实践

**实践目标**：用 PyTorch 构造连续/非连续张量，对照代码判断各自会走哪个分支，理解「谁会被拷贝、谁不会」。

**操作步骤**（**示例代码**，可在任意有 PyTorch 的机器上跑，无需 NPU）：

```python
# 示例代码：观察张量的连续性（仅练习用，非仓库原有代码）
import torch

a = torch.randn(4, 8, 64)          # 连续：stride == (512, 64, 1)
b = a.transpose(0, 1)              # 非连续：stride == (64, 512, 1)
c = a[1:]                          # 非连续：偏移非 0（viewOffset != 0）
d = a.clone()                      # 连续
for t in (a, b, c, d):
    print(t.is_contiguous(), t.stride(), t.storage_offset())
```

1. 运行并记录四个张量的 `is_contiguous / stride / storage_offset`。
2. 对每个张量回答：若把它作为 `input` 传给 scatter 算子，走 `IsContiguous` 分支还是 `CreateView` 分支？作为 `indices`/`update` 传入呢？
3. 再回答：`b` 作为 `input` 时，workspace 里会为它产生拷贝吗？作为 `update` 时呢？

**需要观察的现象**：`b`、`c` 的 stride 不满足连续定义、`c` 的 offset 非零。

**预期结果**：`b`、`c` 作 `input` 走 `CreateView`（零拷贝），作 `indices`/`update` 走 `Contiguous`（产生拷贝、占 workspace）；`a`、`d` 三种角色都零拷贝。**待本地验证**：NPU 上的真实行为需在昇腾环境装好 run 包后，用 ST 测试（`tests/st/test_ai_infra_scatter_block_update.py`）传入非连续输入验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `input` 的处理改成无条件 `l0op::Contiguous(input, executor)`，非连续输入时算子行为会怎样？

**答案**：Contiguous 会为非连续的 `input` 生成一份连续拷贝，kernel 把 update 写进这份拷贝；executor 管理的中间张量执行后释放，用户手里的原 `input` 完全没有被更新，「原地写入」的语义被破坏。所以必须用 `CreateView` 保留 stride/offset，让 kernel 直写原内存。

**练习 2**：`indices` 和 `update` 只是只读输入，为什么可以放心 Contiguous？

**答案**：它们只被读取，拷贝一份连续版本不影响任何写回语义，代价只是一次搬运和相应 workspace；而 kernel 拿到连续布局后可以用最简单高效的线性寻址。这是一个典型的「用空间换简单性」的取舍。

**练习 3**：`CreateView` 的五个参数分别是什么？为什么一个都不能省？

**答案**：原张量 `input`、viewShape（逻辑形状）、storageShape（底层存储形状）、viewStrides（各维步长）、viewOffset（存储起点偏移）。少了 viewStrides/viewOffset 就无法还原非连续张量的真实地址，少了 viewShape/storageShape 就无法区分「逻辑视图」与「物理存储」，kernel 侧的 stride 偏移计算也就无从谈起。

### 4.4 L0 算子调用：从 aclnn 到下发列表

#### 4.4.1 概念说明

aclnn 层做完检查和连续化后，并不自己拼 kernel 参数，而是调用 `l0op::AiInfraScatterBlockUpdate(...)`——这个函数在**不带 aclnn 前缀**的那对文件里，是算子的 **L0（Level 0）封装**。它的任务只有一个：把「算子名 + 输入输出张量」登记（register）进 executor 的 **launcher list（待下发算子列表）**。

真正的下发发生在第二段：`CommonOpExecutorRun` 遍历 executor 里登记的所有算子，逐个走「查 OpDef 原型 → 选 tiling → 启动 kernel」的流程（tiling 是下一讲 u2-l3 的主角）。所以可以这样理解分工：

| 层 | 文件 | 干什么 |
|---|---|---|
| L2 / aclnn 层 | `aclnn_*.cpp` | 检查、连续化、创建 executor、调 L0 |
| L0 封装层 | `*_api/ai_infra_*.cpp`（无 aclnn 前缀） | 把算子登记进 executor 的下发列表 |
| op_host 层 | `*_def.cpp` / `*_tiling.cpp` | 算子原型注册、tiling 计算（u2-l1 / u2-l3） |
| op_kernel 层 | `op_kernel/*` | AscendC kernel 真正算数（u2-l4） |

本算子是**原地（in-place）算子**：`input` 既是输入又是输出，没有独立的输出张量。这在 L0 封装里体现为 `input` 同时出现在 `OP_INPUT` 和 `OP_OUTPUT` 中。

#### 4.4.2 核心流程

```text
l0op::AiInfraScatterBlockUpdate(inputView, indicesContiguous, updateContiguous, executor)
   ├─ L0_DFX(AiInfraScatterBlockUpdate, input, indices, update)   ← L0 层打点
   ├─ ADD_TO_LAUNCHER_LIST_AICORE(
   │      AiInfraScatterBlockUpdate,
   │      OP_INPUT(input, indices, update),    ← 三个输入
   │      OP_OUTPUT(input))                    ← 输出就是 input（原地更新）
   │    └─ 失败 → OP_CHECK_ADD_TO_LAUNCHER_LIST_AICORE 打日志、返回 nullptr
   └─ return input;   ← 把 input 作为 L0 层的"输出张量"返回给 aclnn 层
```

`ADD_TO_LAUNCHER_LIST_AICORE` 中的 AICore 指昇腾的 AI 核（执行向量/矩阵计算的核），与之并列的还有 AICPU 通道（`ADD_TO_LAUNCHER_LIST_AICPU`，用于 AICPU 算子，第 4 单元会见到）。

#### 4.4.3 源码精读

L0 封装的完整实现（[ai_infra_scatter_block_update.cpp:L22-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.cpp#L22-L37)）：

```cpp
namespace l0op {
OP_TYPE_REGISTER(AiInfraScatterBlockUpdate);        // 注册算子类型标识

const aclTensor *AiInfraScatterBlockUpdate(
    aclTensor *input, const aclTensor *indices, const aclTensor *update, aclOpExecutor *executor)
{
    L0_DFX(AiInfraScatterBlockUpdate, input, indices, update);   // L0 层打点

    auto ret1 = ADD_TO_LAUNCHER_LIST_AICORE(AiInfraScatterBlockUpdate, OP_INPUT(input, indices, update),
        OP_OUTPUT(input));                                         // 登记进 AICore 下发列表
    OP_CHECK_ADD_TO_LAUNCHER_LIST_AICORE(ret1 != ACLNN_SUCCESS, return nullptr,
        "AiInfraScatterBlockUpdate ADD_TO_LAUNCHER_LIST_AICORE failed.");

    return input;                                                  // 原地算子：输出即 input
}
}
```

配套的声明在 [ai_infra_scatter_block_update.h:L17-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.h#L17-L21)，只声明 `l0op` 命名空间里的这一个函数。注意头文件保护宏是 `OP_API_INC_LEVEL0_OP_...`（[L11-L12](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.h#L11-L12)），从命名也能看出官方把它定位为 "LEVEL0 OP"。

两个宏的行为佐证：

- `OP_CHECK_ADD_TO_LAUNCHER_LIST_AICORE(cond, retExpr, errMsg)`：仓库 stub 副本见 [op_error_check.h:L239-L243](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/common/op_error_check.h#L239-L243)——条件成立（即登记失败）时打日志并执行 `return nullptr`。
- `OP_TYPE_REGISTER`：为算子注册类型标识。UT stub 里 `ADD_TO_LAUNCHER_LIST_AICORE` 的 DSA 兄弟宏引用了 `KERNEL_NAME##OpTypeId()`（[make_op_executor.h:L27-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h#L27-L32)），可反推 `OP_TYPE_REGISTER` 正是生成这类类型标识的宏。

回到 aclnn 侧的调用点（[aclnn_ai_infra_scatter_block_update.cpp:L132-L134](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L132-L134)）：传入的是 `inputView` / `indicesContiguous` / `updateContiguous` 三个处理后的张量；返回 nullptr 则报 `ACLNN_ERR_INNER_NULLPTR`。

还有一个值得体会的设计：UT stub 里 `ADD_TO_LAUNCHER_LIST_AICORE` 被替换成「直接返回 `ACLNN_SUCCESS`」（[make_op_executor.h:L23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h#L23)），不下发真 kernel。正因如此，`tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp` 里的用例（如 FP32 基本用例 [L41-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp#L41-L55)）才能在没有 NPU 的机器上跑通第一段全流程并断言返回 `ACLNN_SUCCESS`。

#### 4.4.4 代码实践

**实践目标**：写出 `my_add` 的 L0 封装 `ai_infra_my_add.cpp`，体会「aclnn 层厚、L0 层薄」的分层。

**操作步骤**：

1. 仿照 4.4.3 写出（**示例代码**）：

```cpp
// 示例代码：my_add 的 L0 封装骨架（仅练习用，非仓库原有代码）
#include "ai_infra_my_add.h"
#include "aclnn_kernels/common/op_error_check.h"
#include "opdev/make_op_executor.h"
#include "opdev/op_dfx.h"
#include "opdev/op_executor.h"
#include "opdev/op_log.h"

using namespace op;

namespace l0op {
OP_TYPE_REGISTER(MyAdd);

const aclTensor *MyAdd(aclTensor *x, const aclTensor *y, aclOpExecutor *executor)
{
    L0_DFX(MyAdd, x, y);

    auto ret1 = ADD_TO_LAUNCHER_LIST_AICORE(MyAdd, OP_INPUT(x, y),
        OP_OUTPUT(x));   // 若 my_add 也设计成原地写 x，则输出填 x；
                         // 若另设输出 z，则签名为 (x, y, z, executor)，OP_OUTPUT(z)
    OP_CHECK_ADD_TO_LAUNCHER_LIST_AICORE(ret1 != ACLNN_SUCCESS, return nullptr,
        "MyAdd ADD_TO_LAUNCHER_LIST_AICORE failed.");

    return x;
}
}
```

2. 思考并写下结论：如果 `my_add` 改成「三参数、独立输出 `z`」的非原地设计，L0 封装和上一节 `CommonProcess` 里的调用各需要改哪几处？（提示：签名加 `aclTensor *z`、`OP_OUTPUT(z)`、`return z`；aclnn 侧 `CommonProcess` 需要先为 `z` 建张量描述再传入。）
3. 对照 scatter 的 L0 文件数一遍行数：不算头文件版权注释，核心逻辑不到 15 行。再回头数 aclnn 文件的核心逻辑行数，感受分层厚度差。

**需要观察的现象**：L0 封装里没有任何 if/for 业务逻辑，只有「打点 → 登记 → 检查 → 返回」四拍子。

**预期结果**：得到与仓库风格一致的 L0 封装；能说清原地与非原地两种设计在 `OP_INPUT`/`OP_OUTPUT` 与返回值上的差异。**待本地验证**：真实编译需 CANN 环境与 OpDef（`MyAdd` 的 `*_def.cpp`，参照 u2-l1）配合。

#### 4.4.5 小练习与答案

**练习 1**：`ADD_TO_LAUNCHER_LIST_AICORE` 是把算子「立即执行」了吗？

**答案**：不是。它只把算子名与输入输出描述**登记**进 executor 的下发列表（这正是 "add to launcher list" 的字面含义）。真正执行发生在第二段 `CommonOpExecutorRun` 按流下发时；登记阶段还会连带为算子确定 tiling（下一讲）。

**练习 2**：本算子 `OP_OUTPUT(input)` 中输出与输入是同一个张量，这说明什么？aclnn 层哪一步专门为这件事做了铺垫？

**答案**：说明这是原地（in-place）更新算子，直接写回用户输入的内存。铺垫就是 4.3 的 `CreateView` 分支——保留 stride/storageShape/offset 的零拷贝视图，保证 kernel 写的就是用户原张量的真实地址。

**练习 3**：为什么 op_api 的 UT（无 NPU）也能验证 `aclnnXxxGetWorkspaceSize` 返回 `ACLNN_SUCCESS`？

**答案**：因为 UT 框架把 `ADD_TO_LAUNCHER_LIST_AICORE` 等 CANN 宏替换成了 stub（直接返回 `ACLNN_SUCCESS`，见 `tests/ut/framework_normal/op_api/stub/opdev/make_op_executor.h:L23`），第一段里的检查、Contiguous 视图构造、登记等 Host 侧逻辑都能在 CPU 上走通，只有真正碰硬件的下发被「假装成功」。

## 5. 综合实践

把四个模块串起来，完成本讲的指定实践任务：**为假想算子 `my_add` 编写完整的 aclnn 两段式骨架**（全部为**示例代码**，放在仓库外练习，不要写入源码树）。

**任务清单**：

1. `my_add.h / my_add` 头文件：仿照 [aclnn_ai_infra_scatter_block_update.h:L37-L52](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L37-L52)，声明 `aclnnMyAddGetWorkspaceSize` 与 `aclnnMyAdd` 两段接口，并为每个参数写注释（x、y：shape 任意、FP16/BF16/FLOAT）。
2. 三步检查：直接复用 4.2.4 写好的 `CheckMyAddNotNull / CheckMyAddEmptyTensor / CheckMyAddDtypeValid / CheckMyAddParams`。
3. `aclnnMyAddCommonProcess`：仿照 4.3.3 的 `CommonProcess`——`x`、`y` 都做 `l0op::Contiguous`（my_add 若设计成读入两输入，非原地），最后调 `l0op::MyAdd(xContiguous, yContiguous, executor)` 并判空。想一想：**如果 x 要原地更新，这里该换成什么？**（答案：走 4.3 的 `CreateView` 分支。）
4. 两段接口：仿照 4.1.3 的 L139-L161——第一段 `CREATE_EXECUTOR` → `CommonProcess` → `GetWorkspaceSize` → `ReleaseTo`；第二段只留 `CommonOpExecutorRun`。
5. L0 封装：复用 4.4.4 的 `ai_infra_my_add.cpp/.h`。
6. 自查清单（逐条打勾）：
   - [ ] 检查顺序是 NotNull → Empty → Dtype？
   - [ ] 每个失败分支都有 `OP_LOGE` + 正确档位的错误码（参数错用 `ACLNN_ERR_PARAM_*`，内部错用 `ACLNN_ERR_INNER_*`）？
   - [ ] 第一段失败路径不会泄漏 executor（靠 `uniqueExecutor` RAII）？
   - [ ] 第二段除打点外只有一行 `CommonOpExecutorRun`？
   - [ ] L0 封装的 `OP_INPUT`/`OP_OUTPUT` 与返回值和你的原地/非原地设计一致？
7. （可选，需昇腾环境）参照 [test_aclnn_ai_infra_scatter_block_update.cpp:L252-L262](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_api/test_aclnn_ai_infra_scatter_block_update.cpp#L252-L262) 的写法给 `my_add` 补一个 `x == nullptr` 的 UT 用例，用 `bash build.sh -u --opapi`（参数含义见 u1-l2）跑 op_api 单测验证。**待本地验证**。

**预期结果**：一份约 120 行、结构与 scatter 算子逐段同构的 `aclnn_my_add` 骨架，此后照着它往仓库里加真实算子时，op_api 这层就是「填空题」。

## 6. 本讲小结

- aclnn 接口是**两段式**：第一段 `GetWorkspaceSize` 同步完成检查、连续化、创建并填充 `aclOpExecutor`、汇总 workspace 需求；第二段拿 workspace + executor + stream 交给 `CommonOpExecutorRun` 异步下发，薄到只有一行。
- **参数三步检查**按 NotNull → EmptyTensor → DtypeValid 的固定次序展开，次序不可换（先判空才能解引用）；错误码区分参数错（`ACLNN_ERR_PARAM_*`）与内部错（`ACLNN_ERR_INNER_*`）。
- 非连续输入有两种处理：只读输入用 `l0op::Contiguous`（必要时拷贝、占 workspace）；**原地写入的目标必须用 `executor->CreateView` 保留 stride/storageShape/offset，零拷贝直写原内存**——这是 scatter 类原地算子的关键设计。
- aclnn 层通过 `l0op::Xxx` 调用 **L0 封装**；L0 封装用 `ADD_TO_LAUNCHER_LIST_AICORE` 把「算子名 + OP_INPUT/OP_OUTPUT」登记进 executor 的下发列表，真正的执行在第二段按流发生。
- op_api 目录的文件对子有规律：`aclnn_*` 前缀 = 对外两段式接口；无前缀同名文件 = `l0op` 命名空间的 L0 封装。
- op_api 的 UT 用 stub 替身（如 `ADD_TO_LAUNCHER_LIST_AICORE` 直接返回成功）实现无硬件验证，异常路径（nullptr、非法 dtype）都有对应断言用例。

## 7. 下一步学习建议

第一段接口里 `ADD_TO_LAUNCHER_LIST_AICORE` 登记算子后，executor 还需要为算子确定 **tiling**（切块方案）才能算出 workspace、生成 kernel 的启动参数——这正是下一讲 **u2-l3《op_host 之 Tiling：TilingData 与 TilingBaseClass 七步框架》** 的内容，届时 `GetWorkspaceSize()` 返回值的来源就完全闭环了。

在此之前，建议先做两件热身事：

1. 横向对比 2~3 个算子的 aclnn 文件（如 `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/` 与 `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_api/`），观察参数更多时三步检查如何扩展（如 shape 匹配、广播检查会用到 stub 文件里那一族 `OP_CHECK_*` 宏）。
2. 通读 `common/stub/op_api/aclnn_kernels/common/op_error_check.h` 的全部检查宏，建立「写新算子时手头有哪些现成检查件」的清单。
