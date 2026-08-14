# 环境变量与算法配置系统

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `AlgEnvConfig` 这个「环境变量总容器」里都装了哪些字段（包括本轮新增的 `useNewSelector`），以及 `hcclAlgoConfig` 这张「算子 → 四级算法」映射表是怎么组织的。
- 跟着 `InitEnvConfig()` 的源码，讲清它按什么顺序调用一串 `Parse*` 函数，以及「廉价优先、尽早失败」与「单次初始化守卫」两条设计原则。
- 把 `HCCL_ALGO="allreduce=level0:NA;level1:ring"` 这样的字符串，从读入、切分、校验，一路追到 `g_algEnvConfig.hcclAlgoConfig[opType][level]`，并解释它如何被 Selector（u3-l2）读取去影响 `algName`。
- 说清本轮两个重要行为变化：**`HCCL_ALGO` 只在 910_93（A3）设备上解析，A5 等其他设备走 costmodel 新流程不再解析它**；**新增 `HCCL_USE_NEW_SELECTOR` 开关**（取值校验、默认值、`IsNewSelectorEnabled()` 如何被 `Selector`/`ReSelector` 用于新旧选择器双路径分发）。
- 学会配置 `HCCL_ALGO`、`HCCL_USE_NEW_SELECTOR`、`HCCL_DEBUG_CONFIG`、`HCCL_DETERMINISTIC`、`HCCL_EXEC_TIMEOUT` 等关键运行期开关。

本讲承接 [u4-l1 算法类型 AlgType 与分级选择](u4-l1-algtype.md)：u4-l1 讲的是「算法类型」在源码里的两套枚举（对外的 `HcclAlgoType`、对内的 `AlgTypeLevel0/1/2`）；本讲要回答的是——**用户在 shell 里 `export` 的环境变量，是怎么变成这些枚举值、并最终影响一次算子执行的**。

## 2. 前置知识

- **环境变量（environment variable）**：进程启动时从父进程继承的一组「键=值」字符串。C/C++ 用 `std::getenv("名字")` 读取。HCCL 用一组以 `HCCL_` 开头的环境变量来在不重新编译的前提下调优行为。
- **`HcclAlgoType` 与网络层级**：回顾 u4-l1，HCCL 把物理网络分成多层（节点内 / Server 间 / 超节点间）。`HCCL_ALGO` 用 `level0`/`level1`/`level2` 给每一层指定一个算法族（如 `ring`、`NHR`、`H-D_R`）。
- **`HcclDevType` 设备类型**：回顾 u4-l2，运行期 `HcclGetDeviceType` 探测出的设备枚举（如 `DEV_TYPE_910_93` 即 A3、`DEV_TYPE_950` 即 A5）。本讲的 `HCCL_ALGO` 设备门控正是基于它。
- **Selector 产出的 `algName`**：回顾 u3-l2 / u3-l1，算法选择器最终产出一个字符串 `algName`（如 `AicpuAllReduceSoleNHR`），它是后续 executor/template 注册表的查表键；本轮起 Selector 存在「旧 ExecuteSelector 优先级遍历」与「新 SelectorEngine 代价模型」两条路径。
- **「廉价优先、尽早失败」**：在 u2-l3 入参校验里已见过这条原则——先把代价小的检查（空指针、格式）放在前面，不合格立刻返回错误，不做后续昂贵操作。本讲的环境变量解析也遵循它。

> 术语提示：本讲中 `level0/level1/level2/level3` 是 `HCCL_ALGO` 字符串里的层级记号；源码里对应的整数下标 `HCCL_ALGO_LEVEL_0..3` 与总数 `HCCL_ALGO_LEVEL_NUM` 定义在 HCOMM 仓的 `hccl_types.h` 中（本仓 `#include <hccl/hccl_types.h>` 引入）。从 `ParserHcclAlgoLevel` 的映射表与 `ParseAlgoString` 的日志可见共有 4 级，下标为 0~3，即 `HCCL_ALGO_LEVEL_NUM == 4`（数值待确认，但层级数量可由源码逻辑确定）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/common/alg_env_config.h` | 声明 `AlgEnvConfig` 结构（含本轮新增 `useNewSelector` 字段）、`SetDefaultParams` 默认值、`HcclAlgoTypeMap` 枚举↔字符串字典，以及所有 `Parse*` 函数、`IsNewSelectorEnabled()` 与 `GetExternalInput*` 读取函数。 |
| `src/common/alg_env_config.cc` | 实现全部解析逻辑。核心是 `InitEnvConfig()` 编排器、`ParseHcclAlgo()` 算法配置解析链、本轮新增的 `ParseNewSelector()` 与 `HCCL_ALGO` 的 910_93 设备门控；定义 `thread_local AlgEnvConfig g_algEnvConfig`。 |
| `src/common/config_log.cc` | `InitDebugConfigByEnv()` 解析 `HCCL_DEBUG_CONFIG`，用位掩码（ALG/TASK/RESOURCE）控制调试日志分类。 |
| `src/ops/op_common/selector/auto_selector_base.cc` | 旧选择器路径：`AutoSelectorBase::Select` 通过 `GetExternalInputHcclAlgoConfigAllType()` 把 `HCCL_ALGO` 配置读进 Selector。 |
| `src/ops/op_common/op_common.cc` | 门面 `Selector()`/`ReSelector()`：本轮在此用 `IsNewSelectorEnabled() && SelectorEngine::IsOpSupported()` 做新旧选择器双路径分发。 |
| `src/ops/op_common/selector/selector_engine.cc` | 新选择器 `SelectorEngine`，其中 `IsOpSupported()` 定义了当前支持新旧路径切换的算子白名单。 |
| `docs/zh/user_guide/hccl_env/HCCL_ALGO.md` | `HCCL_ALGO` 的官方说明（取值、全局/按算子两种写法、约束）。 |

---

## 4. 核心概念与源码讲解

### 4.1 AlgEnvConfig 数据结构与线程局部实例

#### 4.1.1 概念说明

HCCL 有几十个环境变量。如果每个变量都散落在各处「读一次、用一次」，代码会非常混乱，也无法保证「同一进程里所有算子看到一致的配置」。HCCL 的做法是：定义一个**集中式的配置容器** `AlgEnvConfig`，把所有环境变量解析后的结果装进它的字段；进程里任何代码想读配置，都从这个容器读，而不是再去 `getenv`。

这就把「读环境变量」和「用配置」两件事解耦了：

- **写端（解析）**：`InitEnvConfig()` 在算子入口被调用一次，把所有环境变量解析后填进容器。
- **读端（使用）**：selector、executor、template 等通过一组 `GetExternalInput*()` 函数从容器取值。

本轮演进给容器新增了一个字段 `useNewSelector`——它是新选择器（代价模型选择器，详见 Unit 8）在 HCCL 侧的「总开关」。

#### 4.1.2 核心流程

`AlgEnvConfig` 的生命周期可以概括为：

1. 算子入口（如 `HcclAllReduce`）调用 `InitEnvConfig()`。
2. `InitEnvConfig()` 依次调用各 `Parse*` 函数，每个函数读取一个（或一组）环境变量，校验后写入 `AlgEnvConfig` 的对应字段。
3. 后续执行链路通过 `GetExternalInput*()` / `IsNewSelectorEnabled()` 读取这些字段，不再触碰环境变量本身。

容器以「每个线程一份」的方式存在：

```cpp
static std::mutex g_algEnvConfigMutex;        // 保护跨线程读写
static thread_local AlgEnvConfig g_algEnvConfig; // 每线程一份配置快照
```

这两行见 [src/common/alg_env_config.cc:27-28](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L27-L28)：`thread_local` 意味着每个线程有自己独立的 `g_algEnvConfig` 实例，线程 A 改动不会影响线程 B；`g_algEnvConfigMutex` 在需要跨线程一致访问（如读取超时配置）时加锁。

> 为什么用 `thread_local`？回顾 u1-l5，HCCL 单算子程序常采用「单进程多线程、每线程绑一张卡」模型。`thread_local` 让每个线程的算子调用都能独立解析并缓存自己的配置快照，避免线程间争用，也允许不同线程在不同时刻使用不同的环境变量取值。

#### 4.1.3 源码精读

先看 `AlgEnvConfig` 结构本身与它的默认值。字段很多，但可分三类：**展开模式/引擎类**、**链路与重执行类**、**算法覆盖映射**。

[src/common/alg_env_config.h:34-83](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L34-L83) 定义了 `AlgEnvConfig` 结构与 `SetDefaultParams()`。本轮在 `enableEntryLog` 之后新增了一行：

```cpp
bool useNewSelector;   // HCCL_USE_NEW_SELECTOR 解析结果，默认 false
```

（字段声明见 [src/common/alg_env_config.h:41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L41)，默认值 `useNewSelector = false;` 见 [src/common/alg_env_config.h:65](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L65)。）

结构末尾是最关键的**算法覆盖映射**：

```cpp
std::map<HcclCMDType, std::vector<HcclAlgoType>> hcclAlgoConfig;
```

这是一个**二级映射**：

- 外层键 `HcclCMDType`：算子类型（如 `HCCL_CMD_ALLREDUCE`、`HCCL_CMD_ALLGATHER`）。
- 内层 `vector<HcclAlgoType>`：长度为 `HCCL_ALGO_LEVEL_NUM`（4），下标 0/1/2/3 分别对应 level0/level1/level2/level3 的算法族。

`SetDefaultParams()` 把每个算子的每一层都初始化为 `HCCL_ALGO_TYPE_DEFAULT`，含义是「该层由 HCCL 自适应选择」：

```cpp
for (u32 opType = 0; opType < static_cast<u32>(HcclCMDType::HCCL_CMD_MAX); opType++) {
    hcclAlgoConfig[static_cast<HcclCMDType>(opType)]
        = std::vector<HcclAlgoType>(HCCL_ALGO_LEVEL_NUM, HcclAlgoType::HCCL_ALGO_TYPE_DEFAULT);
}
```

（见 [src/common/alg_env_config.h:78-81](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L78-L81)。）这正是文档里「默认自适应、无需手工指定」的代码体现——只要用户没设 `HCCL_ALGO`（或设备不解析它，见 4.4），所有层都是 `DEFAULT`，交给 Selector 自己挑（u3-l2）。

另一个重要字典是 `HcclAlgoTypeMap`，它把枚举值翻译成人读的字符串，用于日志输出（见 [src/common/alg_env_config.h:85-99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L85-L99)）：

```cpp
const std::map<HcclAlgoType, std::string> HcclAlgoTypeMap = {
    {HcclAlgoType::HCCL_ALGO_TYPE_DEFAULT, "default"},
    {HcclAlgoType::HCCL_ALGO_TYPE_RING, "ring"},
    ...
    {HcclAlgoType::HCCL_ALGO_TYPE_NHR, "NHR"},
    {HcclAlgoType::HCCL_ALGO_TYPE_NA, "NA"},
};
```

#### 4.1.4 代码实践

**实践目标**：在不读全文的情况下，凭字段名与默认值建立「AlgEnvConfig 装了什么」的心智模型。

**操作步骤**：

1. 打开 [src/common/alg_env_config.h:34-83](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L34-L83)。
2. 把 `AlgEnvConfig` 的字段分成四组，填入下表（只列代表字段）：

   | 分类 | 字段 | 默认值 | 对应环境变量（先猜，后文对答案） |
   | --- | --- | --- | --- |
   | 引擎/展开模式 | `aicpuUnfold` / `aivMode` / `ccuMSMode` | `false` | HCCL_OP_EXPANSION_MODE |
   | 新选择器开关 | `useNewSelector` | `false` | HCCL_USE_NEW_SELECTOR（本轮新增） |
   | 链路/重执行/确定性 | `interHccsDisable` / `hcclDeterministic` / `hcclRetryConfig[]` | `false` / `0` / `false` | （待对答案） |
   | 算法覆盖映射 | `hcclAlgoConfig` | 全 `DEFAULT` | HCCL_ALGO（仅 910_93 解析） |

**需要观察的现象**：`SetDefaultParams()` 里每个字段都被显式赋了默认值，没有「忘记初始化」的字段；`useNewSelector` 也在其中。

**预期结果**：你能用一句话说出「AlgEnvConfig = 一组布尔/数值开关 + 一张 opType→四级算法族的映射表」，并指出本轮新增的唯一字段是 `useNewSelector`。

**待本地验证**：无（纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hcclAlgoConfig` 用 `map<HcclCMDType, vector<HcclAlgoType>>` 而不是直接 `map<HcclCMDType, HcclAlgoType>`？

**参考答案**：因为算法是**分级**的——同一个算子，其节点内（level0）、Server 间（level1）、超节点间（level2）可以分别用不同的算法族。`vector` 的每个下标对应一级网络层级，这样才能独立描述每一层的算法选择（回顾 u4-l1 的 `TagAlgType` 三级组合思想）。

**练习 2**：`thread_local AlgEnvConfig g_algEnvConfig;` 与全局单例相比，最大的行为差异是什么？`IsNewSelectorEnabled()` 直接 `return g_algEnvConfig.useNewSelector;`（不加锁）安全吗？

**参考答案**：`thread_local` 使每个线程持有独立的配置实例，不同线程解析到的取值互不覆盖；全局单例只有一份，多线程同时写会产生数据竞争。`IsNewSelectorEnabled()` 读的正是当前线程自己的那份 `thread_local` 实例，本线程内该字段只在 `InitEnvConfig`（已持锁）里被写，因此线程内读取是安全的、无需再加锁。

---

### 4.2 InitEnvConfig 编排与 GetEnv 封装

#### 4.2.1 概念说明

`InitEnvConfig()` 是整个环境变量子系统的**总入口（编排器，orchestrator）**。它本身不解析任何具体变量，只做三件事：

1. 决定**解析顺序**（哪个 `Parse*` 先调用）。
2. 对每个解析结果做**错误上报**（`RPT_ENV_ERR`）与**错误返回**（`CHK_PRT_RET`）。
3. 用 `initialized` 标志保证「耗时的解析只做一次」。

它被每个算子的入口（如 `HcclAllReduce`）在参数校验阶段调用，例如 reduce_scatter、all_to_all_v 等都在入口处 `CHK_RET(InitEnvConfig());`（见仓库内多算子的 `*_op.cc`）。

#### 4.2.2 核心流程

`InitEnvConfig()` 的解析顺序如下（自上而下，对应 [src/common/alg_env_config.cc:176-368](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L176-L368)）：

```
ParseOpExpansion()      // HCCL_OP_EXPANSION_MODE —— 不受 initialized 守卫，每次都解析
if (initialized) return // 单次守卫：以下只解析一次
ParseDeterministic()    // HCCL_DETERMINISTIC
ParseIntraLinkType()    // HCCL_INTRA_PCIE_ENABLE / HCCL_INTRA_ROCE_ENABLE
ParseEntryLogEnable()   // HCCL_ENTRY_LOG_ENABLE
ParseNewSelector()      // HCCL_USE_NEW_SELECTOR —— 本轮新增（4.4 重点）
ParseInterLinkType()    // HCCL_INTER_HCCS_DISABLE
ParseRetryEnable()      // HCCL_OP_RETRY_ENABLE
ParseExecTimeout()      // HCCL_EXEC_TIMEOUT
ParseMultipleDimensionSplitRatio()  // HCCL_ALG_MULTIPLE_DIMENSION_SPLIT_RATIO
ParseHcclAlgo()         // HCCL_ALGO —— 本讲重点（4.3），且仅 910_93 设备执行（4.4）
InitDebugConfigByEnv()  // HCCL_DEBUG_CONFIG —— 本讲重点（4.5）
ParseDfsConfig()        // HCCL_DFS_CONFIG
initialized = true
```

每个 `Parse*` 后面都紧跟一对 `RPT_ENV_ERR(...) + CHK_PRT_RET(...)`，这是「**解析失败 → 上报 + 打日志 + 返回错误码**」的统一两段式。

#### 4.2.3 源码精读

先看 `GetEnv` 封装——所有 `Parse*` 都通过它读环境变量，而不是直接 `std::getenv`：

[src/common/alg_env_config.cc:30-41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L30-L41) 把「未设置 / 空字符串」统一归一成一个哨兵值 `"EmptyString"`：

```cpp
std::string GetEnv(std::string IdName)
{
    constexpr size_t MAX_ENV_VALUE_SIZE = 1024;
    char envValue[MAX_ENV_VALUE_SIZE] = {0};
    char* mmSysGetEnvValue = envValue;
    mmSysGetEnvValue = std::getenv(IdName.c_str());
    if (mmSysGetEnvValue != nullptr && mmSysGetEnvValue[0] != '\0') {
        return std::string(mmSysGetEnvValue);
    } else {
        return "EmptyString";   // 统一的「未设置」哨兵
    }
}
```

这样下游只需 `if (GetEnv("X") == "EmptyString")` 一句即可判断「变量是否有效」，避免到处写 `nullptr` 检查。注意：它把空字符串 `""` 也视为未设置。本轮还把这个函数的声明补进了头文件（[src/common/alg_env_config.h:192](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L192)），供其他模块（如 costmodel 流程）复用。

接着看编排器主体。`InitEnvConfig()` 开头先解析 `HCCL_OP_EXPANSION_MODE`，且**故意放在 `initialized` 守卫之前**：

[src/common/alg_env_config.cc:178-194](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L178-L194)：

```cpp
HcclResult InitEnvConfig()
{
    std::lock_guard<std::mutex> lock(g_algEnvConfigMutex);
    // 解析算子展开模式
    HcclResult ret = ParseOpExpansion();   // ① 每次调用都重新解析
    RPT_ENV_ERR(...);  CHK_PRT_RET(ret != HCCL_SUCCESS, ..., ret);

    if (g_algEnvConfig.initialized) {      // ② 守卫：其余解析只在首次执行
        return HCCL_SUCCESS;
    }
    ...
```

（守卫之后一路解析到 [src/common/alg_env_config.cc:365](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L365) 的 `g_algEnvConfig.initialized = true;`。）

这是一个**关键设计细节**：`ParseOpExpansion()` 决定本次算子的「展开模式 / 引擎」（AICPU/AIV/CCU），它对每次算子调用都可能不同（且依赖 `hcclDeterministic` 等已解析字段），因此不受 `initialized` 守卫保护、每次都重新解析；而其余变量（链路、超时、算法配置等）一次解析即可复用，用 `initialized` 守卫跳过重复工作。

`ParseOpExpansion()` 的内部分支（[src/common/alg_env_config.cc:839-915](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L839-L915)）按字符串值把一组布尔开关置位，例如：

```cpp
if (opExpansionModeEnv == "CCU_MS")       { g_algEnvConfig.ccuMSMode = true; ... }
else if (opExpansionModeEnv == "AIV")     { g_algEnvConfig.aivMode = true; ... }
else if (opExpansionModeEnv == "AI_CPU")  { g_algEnvConfig.aicpuUnfold = true; ... }
```

值得注意的是它同样有**设备差异化**逻辑：未显式设置时，`DEV_TYPE_910_93` 设备默认打开 `aicpuUnfold`（[src/common/alg_env_config.cc:858-863](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L858-L863)）。这些布尔位后续会被 `HcclGetOpExpansionMode`（u2-l4）读取，决定 `OpParam.engine`。

每个解析步骤的错误处理是统一的，以本轮新增的 `HCCL_USE_NEW_SELECTOR` 为例（[src/common/alg_env_config.cc:239-250](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L239-L250)）：

```cpp
ret = ParseNewSelector();
RPT_ENV_ERR(ret != HCCL_SUCCESS, "EI0001", ..., {"HCCL_USE_NEW_SELECTOR", "must be 0 or 1"});  // 上报 EI0001
CHK_PRT_RET(ret != HCCL_SUCCESS,
            HCCL_ERROR("[Init][EnvVarParam]... parse HCCL_USE_NEW_SELECTOR failed..."), ret);
```

`RPT_ENV_ERR` 把错误登记到 CANN 错误管理器（带错误码 `EI0001`、出错的值/变量名/期望值），`CHK_PRT_RET` 打印日志并返回错误码。这套两段式贯穿全部解析步骤，是「尽早失败」原则的落地。

#### 4.2.4 代码实践

**实践目标**：验证「`ParseOpExpansion` 每次执行，其余 `Parse*` 只执行一次」这一守卫行为，并数清守卫之下到底有几个解析步骤。

**操作步骤**：

1. 打开 [src/common/alg_env_config.cc:176-368](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L176-L368)。
2. 在 `ParseOpExpansion()` 调用处（L180）与 `if (g_algEnvConfig.initialized)`（L192）之间画一条分隔线。
3. 数一数：分隔线之上有几个 `Parse*`？之下有几个？

**需要观察的现象**：线上方只有 `ParseOpExpansion` 一个；线下方共 11 个解析调用（`ParseDeterministic`、`ParseIntraLinkType`、`ParseEntryLogEnable`、`ParseNewSelector`、`ParseInterLinkType`、`ParseRetryEnable`、`ParseExecTimeout`、`ParseMultipleDimensionSplitRatio`、`ParseHcclAlgo`、`InitDebugConfigByEnv`、`ParseDfsConfig`），最后是 `initialized = true`。其中 `ParseHcclAlgo` 被包在 `if (deviceType == DEV_TYPE_910_93)` 里，非 910_93 设备实际不执行。

**预期结果**：分隔线上方 1 个、下方 11 个（1 个有设备条件）。由此可推断「展开模式」是唯一每次都重新解析的项，而 `HCCL_ALGO` 是唯一带设备门控的项。

**待本地验证**：要确认运行时实际只调用一次，可在某个 `Parse*`（如 `ParseDeterministic`）函数体内临时加一行 `HCCL_INFO("ParseDeterministic called");`，运行两次 AllReduce，观察日志只打印一次（修改源码仅用于本地观察，勿提交）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `ParseIntraLinkType()` 返回 `HCCL_E_PARA`，`InitEnvConfig()` 会怎么走？

**参考答案**：紧跟其后的 `RPT_ENV_ERR` 登记错误码 `EI0001`（含实际值、变量名、期望值），`CHK_PRT_RET` 打印 `HCCL_ERROR` 日志并使 `InitEnvConfig` 立即返回该错误码，后续 `Parse*` 不再执行。算子入口的 `CHK_RET(InitEnvConfig())` 会把错误继续向上传播，本次算子调用失败。

**练习 2**：为什么 `GetEnv` 把空字符串和「未设置」都映射成 `"EmptyString"`？

**参考答案**：为了给所有 `Parse*` 提供一个统一的「未提供有效值」哨兵。下游用一个 `== "EmptyString"` 判断就能同时处理「变量不存在」和「变量为空」两种情况，避免每处都写 `nullptr` 与长度检查，也符合「未设置即用默认值」的语义。

---

### 4.3 HCCL_ALGO 解析全流程：从字符串到分级算法映射

#### 4.3.1 概念说明

`HCCL_ALGO` 是最常用、也最复杂的调优变量。它支持两种写法：

- **全局配置**（对所有算子生效）：`export HCCL_ALGO="level0:NA;level1:ring"`
- **按算子配置**（只对指定算子生效）：`export HCCL_ALGO="allreduce=level0:NA;level1:ring/allgather=level0:NA;level1:H-D_R"`

其中 `level0` 固定为 `NA`（节点内不由 HCCL 自决），`level1` 是 Server 间算法（`ring`/`NHR`/`H-D_R`/`pipeline` 等），`level2` 是超节点间算法。这正是 [docs/zh/user_guide/hccl_env/HCCL_ALGO.md](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/docs/zh/user_guide/hccl_env/HCCL_ALGO.md) 描述的内容。

解析这条字符串的难点在于：它有**两层分隔符**——`/` 分隔不同算子，`;` 分隔不同层级，`:` 分隔层级名与算法名，`=` 分隔算子名与配置。HCCL 用一组递归的 `Split*` 函数层层剥开。

> 注意（承接 4.4）：本轮起这条解析链**只在 910_93（A3）设备上被调用**；A5 等其他设备不再解析 `HCCL_ALGO`，算法偏好改由 costmodel 新流程承接（详见 4.4 与 Unit 8）。

#### 4.3.2 核心流程

以 `"allreduce=level0:NA;level1:ring"` 为例，解析链如下：

```
ParseHcclAlgo()                       读 HCCL_ALGO（仅 910_93 被调用）
  └─ SetHcclAlgoConfig("allreduce=level0:NA;level1:ring")
       ├─ 去空格
       ├─ SplitHcclOpType(按 '/')      → ["allreduce=level0:NA;level1:ring"]
       ├─ CheckAlgoConfigValid          发现含 '=' → anySpecificConfig（按算子模式）
       └─ SetSpecificAlgType(...)
            ├─ opStringName="allreduce" → HcclCMDType::HCCL_CMD_ALLREDUCE
            ├─ remainAlgoConfig="level0:NA;level1:ring"
            └─ ParseAlgoString(...)
                 ├─ SplitHcclAlgoLevel(按 ';') → ["level0:NA", "level1:ring"]
                 └─ 对每段 ParserHcclAlgoLevel:
                      "level0:NA"   → algType[LEVEL0]=NA
                      "level1:ring" → algType[LEVEL1]=RING
            写入 hcclAlgoConfig[HCCL_CMD_ALLREDUCE] = [NA, RING, DEFAULT, DEFAULT]
```

最终落到 `g_algEnvConfig.hcclAlgoConfig`，再经 `GetExternalInputHcclAlgoConfigAllType()` 被 Selector 读取。

#### 4.3.3 源码精读

入口 `ParseHcclAlgo()` 很薄（[src/common/alg_env_config.cc:370-380](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L370-L380)）：

```cpp
std::string hcclAlgo = GetEnv("HCCL_ALGO");
if (hcclAlgo != "EmptyString") {
    CHK_RET(SetHcclAlgoConfig(hcclAlgo));
    HCCL_INFO("HCCL_ALGO set by environment to [%s]", hcclAlgo.c_str());
} else {
    HCCL_INFO("HCCL_ALGO is not set");   // 全 DEFAULT，自适应
}
```

`SetHcclAlgoConfig()`（[src/common/alg_env_config.cc:382-401](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L382-L401)）负责去空格、切 `/`、校验、再按「全局 vs 按算子」分流：

```cpp
algoConfig.erase(std::remove(..., ' '), ...);          // 去空格
CHK_RET(SplitHcclOpType(algoConfig, algoPerOptype));   // 按 '/' 切
CHK_RET(CheckAlgoConfigValid(algoPerOptype, anyCommonConfig, anySpecificConfig));
if (anyCommonConfig) {
    CHK_RET(SetCommonAlgType(algoPerOptype));          // 无 '=' → 全局
} else {
    CHK_RET(SetSpecificAlgType(algoPerOptype));        // 含 '=' → 按算子
}
```

`CheckAlgoConfigValid()`（[src/common/alg_env_config.cc:596-618](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L596-L618)）有一条重要约束：**全局配置和按算子配置不能混用**，且全局配置只能有一段。它通过「每段是否含 `=`」区分两种模式：

```cpp
if (found != std::string::npos) { anySpecificConfig = true; }  // 含 '='
else                           { anyCommonConfig = true; }      // 不含 '='
if (anyCommonConfig && anySpecificConfig) { HCCL_ERROR("should not set both algo config way"); ... }
```

层级切分由两个递归函数完成，结构对称：

- `SplitHcclOpType`（按 `/` 切算子段，[src/common/alg_env_config.cc:578-593](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L578-L593)）
- `SplitHcclAlgoLevel`（按 `;` 切层级段，[src/common/alg_env_config.cc:620-643](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L620-L643)），且校验层级数不超过 `HCCL_ALGO_LEVEL_NUM`。

最底层是 `ParserHcclAlgoLevel()`（[src/common/alg_env_config.cc:485-533](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L485-L533)），把 `"level1:ring"` 拆成 `(level=HCCL_ALGO_LEVEL_1, algo=HCCL_ALGO_TYPE_RING)`：

```cpp
const std::map<std::string, u32> hcclAlgoLevelMap = {
    {"level0", HCCL_ALGO_LEVEL_0}, {"level1", HCCL_ALGO_LEVEL_1},
    {"level2", HCCL_ALGO_LEVEL_2}, {"level3", HCCL_ALGO_LEVEL_3}};
const std::map<std::string, HcclAlgoType> hcclAlgoTypeMap = {
    {"ring", HCCL_ALGO_TYPE_RING}, {"NHR", HCCL_ALGO_TYPE_NHR},
    {"H-D_R", HCCL_ALGO_TYPE_HDR}, {"NA", HCCL_ALGO_TYPE_NA}, ...};
```

`ParseAlgoString()`（[src/common/alg_env_config.cc:535-576](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L535-L576)）还做了两项保护：初始化整段为 `DEFAULT`、检测同一层级是否被重复配置（重复则报错）。最后用 `HcclAlgoTypeMap` 打出可读日志。

写回容器有两套：

- `SetCommonAlgType`（[src/common/alg_env_config.cc:422-430](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L422-L430)）：把同一段算法循环写入**所有** `HcclCMDType`。
- `SetSpecificAlgType`（[src/common/alg_env_config.cc:432-483](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L432-L483)）：按算子名映射（如 `"allreduce"→HCCL_CMD_ALLREDUCE`）只写指定算子；其中 `"others"` 是通配，对未单独配置的算子统一赋值。

**配置如何被 Selector 消费**——这是连接 u3-l2 的关键。在旧选择器路径的 `AutoSelectorBase::Select` 里，一进来就读取整张映射表并透传给各分支：

[src/ops/op_common/selector/auto_selector_base.cc:17-48](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/auto_selector_base.cc#L17-L48)：

```cpp
SelectorStatus AutoSelectorBase::Select(...) const {
    std::map<HcclCMDType, std::vector<HcclAlgoType>> configAlgMap
        = GetExternalInputHcclAlgoConfigAllType();          // 读 HCCL_ALGO 解析结果
    ...
    ret = SelectCcuMsAlgo(topoInfo, opParam, configAlgMap, selectAlgName);
    ...
    ret = SelectCcuScheduleAlgo(topoInfo, opParam, configAlgMap, selectAlgName);
    ...
    // 后续 AIV / AICPU 分支同样透传 configAlgMap
}
```

`GetExternalInputHcclAlgoConfigAllType()`（[src/common/alg_env_config.cc:416-420](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L416-L420)）返回的就是 `g_algEnvConfig.hcclAlgoConfig` 的拷贝。`configAlgMap` 会被传进 `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAicpuAlgo` / `SelectAivAlgo` 等钩子（u3-l2）。各具体 selector 在分支决策时参考它——例如当 `configAlgMap[ALLREDUCE][LEVEL1] == RING` 时，会倾向选择 Ring 族的 `algName` 而非 NHR 族。**这就是「环境变量 → algName」的完整接驳路径**（旧选择器路径）。

#### 4.3.4 代码实践

**实践目标**：把 `HCCL_ALGO="allreduce=level0:NA;level1:ring"` 这个字符串，手动跑一遍解析链，预测最终 `hcclAlgoConfig` 的内容，并解释它如何影响 Selector。

**操作步骤**：

1. 假设你在 **910_93（A3）设备**上 `export HCCL_ALGO="allreduce=level0:NA;level1:ring"`。
2. 按本节 4.3.2 的流程图，逐步填写下表（每一步的中间结果）：

   | 步骤 | 函数 | 中间结果 |
   | --- | --- | --- |
   | 读变量 | `GetEnv("HCCL_ALGO")` | `"allreduce=level0:NA;level1:ring"` |
   | 去空格、切 `/` | `SplitHcclOpType` | `[______]`（填切段） |
   | 校验模式 | `CheckAlgoConfigValid` | `anySpecificConfig = ____` |
   | 取算子名 | `SetSpecificAlgType` | `opStringName = ____` → `HCCL_CMD_ALL____` |
   | 切 `;` | `SplitHcclAlgoLevel` | `[______, ______]` |
   | 解析每层 | `ParserHcclAlgoLevel` | `algType[LEVEL0]=__`, `algType[LEVEL1]=__` |

3. 写出最终 `hcclAlgoConfig[HCCL_CMD_ALLREDUCE]` 这个 vector 的 4 个元素值。
4. 再回答：若把同样的 export 放到 **A5（`DEV_TYPE_950`）设备**上，上表还走得通吗？（对照 4.4 的设备门控。）
5. 打开 [src/ops/op_common/selector/auto_selector_base.cc:17-48](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/auto_selector_base.cc#L17-L48)，找到 `configAlgMap = GetExternalInputHcclAlgoConfigAllType();` 这一行，说明它如何把解析结果交给 Selector。

**需要观察的现象**：在 910_93 上，`level1:ring` 最终变成 `hcclAlgoConfig[HCCL_CMD_ALLREDUCE][HCCL_ALGO_LEVEL_1] = HCCL_ALGO_TYPE_RING`，其余层为 `DEFAULT`，并经 `configAlgMap` 进入 Selector 的各 `Select*Algo`；在 A5 上整个解析链根本不会被调用。

**预期结果**：

- 第 2 步表格：切段 `["allreduce=level0:NA;level1:ring"]`；`anySpecificConfig=true`；`opStringName="allreduce"→HCCL_CMD_ALLREDUCE`；切 `;` 得 `["level0:NA","level1:ring"]`；`algType[LEVEL0]=NA`、`algType[LEVEL1]=RING`。
- 第 3 步：`[NA, RING, DEFAULT, DEFAULT]`。
- 第 4 步：走不通。`InitEnvConfig` 中的设备门控只对 `DEV_TYPE_910_93` 调用 `ParseHcclAlgo()`，A5 会走 else 分支打出 `HCCL_ALGO not parsed on deviceType[...], A5 uses costmodel flow.` 的 INFO 日志。
- 第 5 步：`configAlgMap` 是 `hcclAlgoConfig` 的拷贝，旧选择器据此把 AllReduce 的 Server 间算法约束为 Ring 族，影响最终 `algName` 的算法后缀。

**待本地验证**：在上板环境（910_93）运行一次 AllReduce，并在 `ParseAlgoString`（[src/common/alg_env_config.cc:572-574](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L572-L574)）的 `HCCL_INFO` 日志中确认输出 `config level0:NA, level1:ring, ...`；在 A5 上运行则应看到 `HCCL_ALGO not parsed` 日志。

#### 4.3.5 小练习与答案

**练习 1**：若用户误写 `HCCL_ALGO="level0:NA;level1:ring/allreduce=level0:NA;level1:NHR"`（全局段和按算子段混用），会发生什么？

**参考答案**：`CheckAlgoConfigValid` 检测到 `anyCommonConfig` 与 `anySpecificConfig` 同时为真，打印 `[CheckAlgoConfigValid]should not set both algo config way` 并返回 `HCCL_E_PARA`，`InitEnvConfig` 立即失败，算子调用报错。用户必须只用一种写法。注意这个报错只可能出现在 910_93 设备上——其他设备压根不解析 `HCCL_ALGO`，错误配置会被静默忽略（仅打 INFO 日志）。

**练习 2**：`SetSpecificAlgType` 里的 `"others"` 关键字有什么作用？

**参考答案**：`"others"` 是按算子配置中的通配项——凡是没有被单独 `opName=...` 配置的算子，都会采用 `"others"` 指定的算法。这样用户可以「为多数算子设一个默认算法，再为个别算子单独覆盖」，而不必逐一列举所有算子。

**练习 3**：为什么说 `configAlgMap` 影响的是 `algName`，而不是直接替换它？

**参考答案**：`algName` 是一个复合字符串（引擎前缀 + 算子 + 编排 + 拓扑 + 搬运轮数，见 u3-l2），由 Selector 综合拓扑形状、数据量、设备类型**生成**。`configAlgMap` 只约束其中的「算法族」维度（如 Ring 还是 NHR），Selector 仍需结合拓扑与引擎决定具体的 `algName`。因此环境变量是「约束/偏好」，Selector 是「最终决策者」。

---

### 4.4 本轮新增：ParseNewSelector 开关与 HCCL_ALGO 的设备门控

#### 4.4.1 概念说明

本轮（costmodel 提交）给环境变量子系统带来两个直接相关的变化，它们共同服务于「新选择器 SelectorEngine + 代价模型」体系（详见 Unit 8）：

1. **`HCCL_USE_NEW_SELECTOR` 开关**：控制 `Selector`/`ReSelector` 走「新选择器（按代价模型挑最小代价算法）」还是「旧选择器（ExecuteSelector 按优先级遍历）」。这是新旧两条选择路径的**唯一入口开关**，且对下游完全透明——两条路径的产出物都是同一个 `algName` 字符串。
2. **`HCCL_ALGO` 的设备门控**：`HCCL_ALGO` 这套「算法族偏好」语法只服务旧选择器；A5 类设备走 costmodel 新流程后不再解析它，避免两套算法偏好机制在新流程里叠加生效。

设备语义回顾（u4-l2）：`DEV_TYPE_910_93` 是 A3（Atlas 800T A3 / SuperCluster 系列），`DEV_TYPE_950` 是 A5。源码注释明确写着「解析算法配置（仅A3设备），A5 走 costmodel 新流程」。

#### 4.4.2 核心流程

**`HCCL_USE_NEW_SELECTOR` 的状态机**：

```
未设置（EmptyString） → useNewSelector = false（默认关），日志提示 default [0]
"0"                    → useNewSelector = false
"1"                    → useNewSelector = true
其他任何值              → HCCL_E_PARA → EI0001 上报 → InitEnvConfig 失败
```

**Selector 双路径分发**（`op_common.cc` 的 `Selector()` 与 `ReSelector()` 中同一套判定）：

```
IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(opType)
   ├─ 是 → SelectorEngine::Global()->Run(...)   // 新选择器：代价模型选最小代价算法
   └─ 否 → ExecuteSelector().Run(...)           // 旧选择器：注册表优先级遍历
```

其中 `IsOpSupported` 是算子白名单：当前仅 `HCCL_CMD_ALLREDUCE` / `HCCL_CMD_REDUCE_SCATTER` / `HCCL_CMD_ALLGATHER` 三个算子支持新路径。所以**即使开关打开，白名单外的算子（如 Broadcast、AlltoAll）也自动回退旧路径**——这是「渐进式迁移」的典型手法：开关 + 白名单双重收窄新路径的影响面。

**`HCCL_ALGO` 的设备门控**：

```
HcclGetDeviceType(deviceType)
   ├─ deviceType == DEV_TYPE_910_93 → ParseHcclAlgo()（解析 HCCL_ALGO 进 hcclAlgoConfig）
   └─ 其他设备（含 A5/DEV_TYPE_950）→ 跳过解析，打 INFO 日志
                                      "HCCL_ALGO not parsed on deviceType[...], A5 uses costmodel flow."
```

#### 4.4.3 源码精读

**（1）`ParseNewSelector()` 的取值校验**（[src/common/alg_env_config.cc:817-837](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L817-L837)）：

```cpp
HcclResult ParseNewSelector()
{
    std::string useNewSelectorEnv = GetEnv("HCCL_USE_NEW_SELECTOR");
    if (useNewSelectorEnv == "EmptyString") {
        HCCL_INFO("HCCL_USE_NEW_SELECTOR set by default to [0]");   // 默认关
        return HCCL_SUCCESS;
    }
    if (useNewSelectorEnv != "0" && useNewSelectorEnv != "1") {
        HCCL_ERROR("[Parser][NewSelector]environmental variable HCCL_USE_NEW_SELECTOR [%s] is invalid, ...",
            useNewSelectorEnv.c_str());
        return HCCL_E_PARA;                                          // 非法值 → 失败
    }
    g_algEnvConfig.useNewSelector = false;
    if (useNewSelectorEnv == "1") {
        g_algEnvConfig.useNewSelector = true;
    }
    HCCL_INFO("HCCL_USE_NEW_SELECTOR set by environment to [%u]", g_algEnvConfig.useNewSelector);
    return HCCL_SUCCESS;
}
```

三个要点：

- **默认值**：未设置时字段保持 `SetDefaultParams()` 里的 `false`，即新选择器默认**关闭**。
- **取值校验**：只接受字符串 `"0"` 或 `"1"`；其他值（如 `true`、`2`、`on`）返回 `HCCL_E_PARA`，经 `InitEnvConfig` 的 `RPT_ENV_ERR + CHK_PRT_RET` 两段式（[src/common/alg_env_config.cc:239-250](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L239-L250)）使**本次算子调用直接失败**。注意错误日志文案里虽然写着 "set by default to [0]"，但实际行为是报错返回而非回退默认——读源码要以返回值为准。
- **读取接口**：`IsNewSelectorEnabled()`（[src/common/alg_env_config.cc:1212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1212)，声明在 [src/common/alg_env_config.h:180](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L180)）直接返回线程局部字段：`return g_algEnvConfig.useNewSelector;`。

**（2）Selector / ReSelector 的双路径分支**。门面函数 `Selector()` 在完成拓扑计算后做分发（[src/ops/op_common/op_common.cc:102-108](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L102-L108)）：

```cpp
// 算法选择，选择完后顺便param.algTag设置了，资源的保存是以算子+算法为单位
if (IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(param.opType)) {
    CHK_RET(SelectorEngine::Global()->Run(comm, param, topoInfo.get(), algName));   // 新路径
} else {
    std::shared_ptr<ExecuteSelector> collAlgSelector = std::make_shared<ExecuteSelector>(ExecuteSelector());
    CHK_RET(collAlgSelector->Run(param, topoInfo.get(), algName));                    // 旧路径
}
```

资源回退时的 `ReSelector()`（[src/ops/op_common/op_common.cc:587-593](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L587-L593)）使用**完全相同**的判定，保证重选算法时不会意外从新路径切回旧路径（或反之）。

白名单定义在 [src/ops/op_common/selector/selector_engine.cc:34-43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L34-L43)：

```cpp
bool SelectorEngine::IsOpSupported(HcclCMDType opType)
{
    // 本迭代新选择器仅支持 AllReduce/ReduceScatter/AllGather, 其他算子走老流程
    static const std::set<HcclCMDType> supportedOps = {
        HcclCMDType::HCCL_CMD_ALLREDUCE,
        HcclCMDType::HCCL_CMD_REDUCE_SCATTER,
        HcclCMDType::HCCL_CMD_ALLGATHER,
    };
    return supportedOps.count(opType) > 0;
}
```

**（3）`HCCL_ALGO` 的设备门控**（[src/common/alg_env_config.cc:311-332](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L311-L332)）：

```cpp
// 解析算法配置（仅A3设备）, A5走costmodel新流程
HcclDevType deviceType;
CHK_RET(HcclGetDeviceType(deviceType));          // 运行期探测设备类型（u4-l2）
if (deviceType == HcclDevType::DEV_TYPE_910_93) {
    ret = ParseHcclAlgo();                        // 4.3 的完整解析链
    RPT_ENV_ERR(...);  CHK_PRT_RET(...);
} else {
    HCCL_INFO("[Init][EnvVarParam] HCCL_ALGO not parsed on deviceType[%u], A5 uses costmodel flow.", ...);
}
```

注意门控条件写的是「**等于 910_93 才解析**」，因此被跳过的不只是 A5——所有非 910_93 设备（910B、950、960 等）都不会解析 `HCCL_ALGO`；日志文案里的 "A5" 只是点出了这条约束的主要动机。另外，设备探测用的是 `HcclGetDeviceType`（带线程级缓存，u4-l2），所以这里的额外开销可以忽略。

一个容易混淆的澄清：**`HCCL_USE_NEW_SELECTOR` 开关本身没有设备门控**——任何设备上都可以设置它；被设备门控的是 `HCCL_ALGO`（旧算法偏好语法）。两个变化是独立的：前者决定「谁来做选择」（新/旧选择器），后者决定「旧的算法偏好语法在哪里还生效」。

#### 4.4.4 代码实践

**实践目标**：分别在 910_93（A3）与 A5 类设备语义下解释 `HCCL_ALGO` 的解析差异，并说明 `HCCL_USE_NEW_SELECTOR` 的取值校验、默认值行为，以及 `IsNewSelectorEnabled()` 如何被 Selector 使用。

**操作步骤**：

1. **梳理取值表**。阅读 [src/common/alg_env_config.cc:817-837](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L817-L837)，填写下表：

   | `HCCL_USE_NEW_SELECTOR` 取值 | `useNewSelector` 终值 | `ParseNewSelector` 返回值 | 对算子调用的影响 |
   | --- | --- | --- | --- |
   | 未设置 | `false` | `HCCL_SUCCESS` | 走旧选择器 |
   | `"0"` | ? | ? | ? |
   | `"1"` | ? | ? | ? |
   | `"true"` | ? | ? | ? |

2. **追踪一次开关生效路径**。从 `ParseNewSelector`（L817）→ 字段 `useNewSelector` → `IsNewSelectorEnabled()`（L1212）→ [op_common.cc:103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L103) 的 `if`，再到 [selector_engine.cc:34-43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L34-L43) 的白名单，画出这条「环境变量 → 分支判定」的调用链。
3. **解释设备差异**。假设两个环境分别设置 `export HCCL_ALGO="allreduce=level0:NA;level1:ring"` 与 `export HCCL_USE_NEW_SELECTOR=1`：
   - 910_93（A3）：两个变量各自如何被解析？
   - A5（`DEV_TYPE_950`）：`HCCL_ALGO` 是否进入 `hcclAlgoConfig`？`HCCL_USE_NEW_SELECTOR` 是否仍然生效？

**需要观察的现象**：步骤 3 中，A5 上 `HCCL_ALGO` 的值不会出现在 `hcclAlgoConfig`（保持全 `DEFAULT`），日志出现 `HCCL_ALGO not parsed on deviceType[5], ...`（具体枚举数值以源码为准，**待本地验证**）；而 `HCCL_USE_NEW_SELECTOR=1` 在 A5 上依然会把 AllReduce/ReduceScatter/AllGather 引入 SelectorEngine 新路径。

**预期结果**：

- 步 1 表格：`"0"` → `false`/`HCCL_SUCCESS`/走旧选择器；`"1"` → `true`/`HCCL_SUCCESS`/白名单内算子走新选择器；`"true"` → 字段保持 `false`，但返回 `HCCL_E_PARA`，`InitEnvConfig` 失败，**算子调用报错**（不会静默回退）。
- 步骤 3：910_93 上两个变量都被正常解析（新开关 + 旧算法偏好可并存，但注意若新开关打开且算子在白名单内，选择决策交给 SelectorEngine）；A5 上 `HCCL_ALGO` 被跳过，新开关照常生效。

**待本地验证**：以上行为推导自源码；实际日志（尤其是设备枚举的数值打印）需在装有对应型号 NPU 的上板环境确认。无卡环境下，本实践为「源码阅读 + 分支推导」型。

#### 4.4.5 小练习与答案

**练习 1**：`HCCL_USE_NEW_SELECTOR=1` 时调用 `HcclBroadcast`，会走新选择器吗？

**参考答案**：不会。`Selector()` 的判定是 `IsNewSelectorEnabled() && SelectorEngine::IsOpSupported(param.opType)` 的**逻辑与**：开关虽开，但 `IsOpSupported` 的白名单只含 AllReduce/ReduceScatter/AllGather，Broadcast 不在其中，短路后走旧的 `ExecuteSelector::Run`。这就是「开关 + 白名单」的双重收窄设计。

**练习 2**：为什么 `ReSelector()`（资源不足回退后重选算法）里要重复同一段 `IsNewSelectorEnabled() && IsOpSupported(...)` 判定，而不是封装成一个全局「当前用哪个选择器」的变量？

**参考答案**：两个入口使用同一判定表达式，保证**首选与重选路径一致**——首次用新选择器的算子，回退重选时也用新选择器，不会因为重选换路径而得到风格不一致的 `algName`（例如重选时被旧选择器的优先级规则覆盖用户的代价模型偏好）。直接每次重新求值（而非缓存到全局变量）也避免了多处状态同步的问题：`IsNewSelectorEnabled` 读线程局部字段，开销极小。

**练习 3**：有用户在 A5 集群上配置了 `HCCL_ALGO` 却感觉不生效，日志里也没有报错。可能的原因是什么？

**参考答案**：`HCCL_ALGO` 仅在 `DEV_TYPE_910_93` 设备上解析；A5 会走 else 分支并只打一条 INFO 级日志（`HCCL_ALGO not parsed on deviceType[...], A5 uses costmodel flow.`），不报错。用户的算法偏好应改用 A5 的 costmodel 新流程的配置方式（`HCCL_ALGO` 在新流程下的解析由 alg_parse/`UpdateCostModelWithAlgo` 承接，详见 [u8-l3](u8-l3-algo-dims-and-parse.md)）。

---

### 4.5 关键运行期开关：HCCL_DEBUG_CONFIG / HCCL_DETERMINISTIC / HCCL_EXEC_TIMEOUT

#### 4.5.1 概念说明

除了 `HCCL_ALGO` 与 `HCCL_USE_NEW_SELECTOR`，还有三个高频调优变量值得单独理解：

- **`HCCL_DEBUG_CONFIG`**：控制**调试日志的分类**。HCCL 的调试日志分 ALG（算法选择）、TASK（任务下发）、RESOURCE（资源计算）几类，全开会非常啰嗦。这个变量用**位掩码（bitmask）**按需开关各类日志。
- **`HCCL_DETERMINISTIC`**：控制**确定性计算**。分布式训练里，同样的输入有时希望得到完全一致的输出（便于复现 bug）。该变量分三级：关闭 / 确定性（不保序）/ 严格（确定性 + 规约保序）。
- **`HCCL_EXEC_TIMEOUT`**：算子**执行超时**（秒），超时后上报，防止通信挂死时无限等待。

此外还有 `HCCL_OP_RETRY_ENABLE`（重执行）、`HCCL_DFS_CONFIG`（参数一致性校验等）等，解析套路一致，本节择重点讲。

#### 4.5.2 核心流程

**HCCL_DEBUG_CONFIG 的位掩码逻辑**有点特别——它支持「取反模式」：

- 普通写法 `HCCL_DEBUG_CONFIG="ALG,TASK"`：只打开 ALG 和 TASK 两类（其余关）。
- 取反写法 `HCCL_DEBUG_CONFIG="^RESOURCE"`：第一个字符是 `^`，表示「除了 RESOURCE 关闭，其余全开」。

位掩码的数学含义：用一个 `u64` 的不同位代表不同分类，按位或（`|`）打开、按位与取反（`& ~mask`）关闭。

**HCCL_DETERMINISTIC 的三态**用一个 `u8` 字段 `hcclDeterministic` 表示，对应枚举：

\[
\text{hcclDeterministic} \in \{0=\text{DISABLE},\ 1=\text{ENABLE},\ 2=\text{STRICT}\}
\]

其中 STRICT（规约保序）对设备有额外要求（支持 A2/A3/A5 OutPlace 场景），并非所有芯片都支持。

**HCCL_EXEC_TIMEOUT** 是一个最多两位小数的非负数，超时单位为秒。

#### 4.5.3 源码精读

`HCCL_DEBUG_CONFIG` 由独立的 `config_log.cc` 解析（注意：它不在 `AlgEnvConfig` 结构里，而是用一个独立的 `static u64 g_debugConfig`）：

[src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/config_log.cc#L19-L57)：

```cpp
u64 g_debugConfig = 0ULL;
bool invert = (env[0] == '^');
g_debugConfig = invert ? ~0ULL : 0ULL;          // 取反模式初值：全1；普通模式：全0
char* configValue = (env[0] == '^') ? env + 1 : env;
...
if (strcasecmp(subConfig, "ALG") == 0)      { mask = HCCL_ALG; }
else if (strcasecmp(subConfig, "TASK") == 0){ mask = HCCL_TASK; }
else if (strcasecmp(subConfig, "RESOURCE") == 0){ mask = HCCL_RES; }
...
g_debugConfig = invert ? (g_debugConfig & (~mask)) : (g_debugConfig | mask);
```

关键在于 `invert` 分支：取反模式初值 `~0ULL`（所有位为 1，即所有类默认开），遇到用户列出的项则用 `& ~mask` 把对应位清零（即「关闭用户列出的」）；普通模式初值 `0`，遇到项则用 `| mask` 置位（即「打开用户列出的」）。这就是「`^` 表示反向选择」的位运算实现。

`HCCL_DETERMINISTIC` 的解析见 [src/common/alg_env_config.cc:1036-1076](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1036-L1076)，它把字符串映射成三级枚举：

```cpp
if (hcclDeterministicEnv == "STRICT") {
    // 规约保序：先校验设备是否支持（910B/910_93/OutPlace 设备）
    ...
    g_algEnvConfig.hcclDeterministic = static_cast<u8>(DeterministicEnableLevel::DETERMINISTIC_STRICT);
} else if (hcclDeterministicEnv == "TRUE") {
    g_algEnvConfig.hcclDeterministic = static_cast<u8>(DeterministicEnableLevel::DETERMINISTIC_ENABLE);
} else {
    g_algEnvConfig.hcclDeterministic = static_cast<u8>(DeterministicEnableLevel::DETERMINISTIC_DISABLE);
}
```

注意 STRICT 分支会调用 `HcclGetDeviceType`（u4-l2）校验芯片是否支持规约保序，不支持则返回 `HCCL_E_NOT_SUPPORT`——这又是「尽早失败」的体现。三级枚举定义在 [src/common/alg_env_config.h:28-32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.h#L28-L32)。

`HCCL_EXEC_TIMEOUT` 的解析见 [src/common/alg_env_config.cc:75-110](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L75-L110)，体现「校验先于使用」：

```cpp
if (!IsValidNumberFormat(execTimeOutEnv, timeoutSize)) { ... return HCCL_E_PARA; }  // 格式校验
if (SalStrToDouble(execTimeOutEnv, execTimeOut) != HCCL_SUCCESS) { ... }            // 转换
if (execTimeOut > static_cast<double>(UINT32_MAX)) { ... }                          // 范围校验
g_algEnvConfig.execTimeOutSet = true;
g_algEnvConfig.execTimeout = execTimeOut;
```

它先用 `IsValidNumberFormat`（限制最多 2 位小数）做廉价格式校验，再转换、再范围校验，最后才写入 `execTimeout` 与配套的 `execTimeOutSet` 布尔位。读取端 `GetExternalInputExecTimeout`（[src/common/alg_env_config.cc:112-121](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L112-L121)）通过 `execTimeOutSet` 判断「用户是否设置过」，未设置则返回 `false` 让调用方走默认逻辑。

最后，所有这些字段都有一一对应的 `GetExternalInput*()` 读取函数（见 [src/common/alg_env_config.cc:1150-1212](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L1150-L1212)），例如 `GetExternalInputHcclDeterministic()`、`GetExternalInputHcclEnableEntryLog()`。下游模块（selector/executor/template）只认这些 getter，绝不直接读环境变量——这是「集中式配置」的纪律。

#### 4.5.4 代码实践

**实践目标**：理解 `HCCL_DEBUG_CONFIG` 的位掩码与取反模式，并能预测给定取值下哪些日志类会被打开。

**操作步骤**：

1. 假设三种取值，分别预测 `g_debugConfig` 的语义（哪些类开、哪些关）：

   | 取值 | invert | 初值 | 结果语义 |
   | --- | --- | --- | --- |
   | `"ALG"` | false | `0` | 只开 ALG |
   | `"ALG,TASK"` | false | `0` | 开 ALG、TASK，其余关 |
   | `"^RESOURCE"` | true | `~0ULL` | 关 RESOURCE，其余全开 |

2. 打开 [src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/config_log.cc#L19-L57)，对照 `invert` 与 `g_debugConfig = invert ? (... & ~mask) : (... | mask)` 验证你的预测。
3. 思考：如果要「关闭所有调试日志」，应该怎么设置？（提示：默认未设置时 `g_debugConfig = 0`，即全关。）

**需要观察的现象**：取反模式与普通模式互为镜像——普通模式从「全关」开始按需打开，取反模式从「全开」开始按需关闭。

**预期结果**：表格三行预测成立；关闭全部日志的最简单做法是「不设置 `HCCL_DEBUG_CONFIG`」（走默认 `0`）。

**待本地验证**：在上板环境分别 `export HCCL_DEBUG_CONFIG="ALG"` 与 `"^RESOURCE"`，运行同一算子，对比日志详略程度。

#### 4.5.5 小练习与答案

**练习 1**：`HCCL_DETERMINISTIC=strict` 在不支持的设备上会发生什么？

**参考答案**：`ParseDeterministic` 的 STRICT 分支调用 `HcclGetDeviceType` 检测，若设备不属于 `910B`/`910_93`/OutPlace 设备，则打印错误并返回 `HCCL_E_NOT_SUPPORT`，`InitEnvConfig` 失败，算子调用报错。

**练习 2**：为什么 `HCCL_EXEC_TIMEOUT` 要配套一个 `execTimeOutSet` 布尔位，而不直接用 `execTimeout == 0` 表示「未设置」？

**参考答案**：因为 `0` 是一个合法的超时值（理论上可表示「不等待」），若用它兼作「未设置」标志会产生歧义。引入独立的 `execTimeOutSet` 布尔位可以精确区分「用户显式设了 0」与「用户没设置」，让读取端 `GetExternalInputExecTimeout` 能正确返回「是否设置」的语义。

---

## 5. 综合实践

把本讲四条主线串起来，完成一个「**环境变量端到端追踪 + 新旧选择器切换**」任务。

**场景**：你在 910_93（A3）集群上为一次 AllReduce 调优——要求 Server 间强制使用 Ring 算法、打开算法选择阶段的调试日志、把执行超时设为 600 秒，并对比开关新选择器前后 Selector 的决策路径。

**任务**：

1. **写出四个 export 命令**（基于本讲学到的取值语法）：
   ```bash
   export HCCL_ALGO="allreduce=level0:NA;level1:ring"
   export HCCL_DEBUG_CONFIG="ALG"
   export HCCL_EXEC_TIMEOUT="600"
   export HCCL_USE_NEW_SELECTOR="0"     # 先走旧路径；第二轮改为 "1" 对比
   ```
2. **追踪 `HCCL_ALGO` 的完整路径**（结合 4.3）：从 `GetEnv` → `SetHcclAlgoConfig` → `SplitHcclOpType` → `CheckAlgoConfigValid` → `SetSpecificAlgType` → `ParseAlgoString` → `ParserHcclAlgoLevel`，最终落到 `hcclAlgoConfig[HCCL_CMD_ALLREDUCE] = [NA, RING, DEFAULT, DEFAULT]`，再经 `GetExternalInputHcclAlgoConfigAllType()` 进入 `AutoSelectorBase::Select` 的 `configAlgMap`，约束 Selector 选择 Ring 族的 `algName`。注意这一切的前提是设备门控放行（910_93）。
3. **解释另三个变量的生效点**：`HCCL_DEBUG_CONFIG` 在 `InitDebugConfigByEnv`（[src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/config_log.cc#L19-L57)）解析为 `g_debugConfig` 的 ALG 位；`HCCL_EXEC_TIMEOUT` 在 `ParseExecTimeout`（[src/common/alg_env_config.cc:75-110](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L75-L110)）解析为 `execTimeout=600`；`HCCL_USE_NEW_SELECTOR` 经 `ParseNewSelector`（[src/common/alg_env_config.cc:817-837](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_env_config.cc#L817-L837)）写入 `useNewSelector`，由 `IsNewSelectorEnabled()` 在 [op_common.cc:103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L103) 决定走 `SelectorEngine` 还是 `ExecuteSelector`。
4. **第二轮实验**：把 `HCCL_USE_NEW_SELECTOR` 改为 `"1"` 再跑一次。AllReduce 在白名单内，应走新选择器；再跑一次 Broadcast 验证它仍走旧路径（白名单外回退）。
5. **画一张数据流图**，包含：shell 环境变量 → `GetEnv` → `Parse*` → `AlgEnvConfig` 字段 → `GetExternalInput*()` / `IsNewSelectorEnabled()` → 下游消费者（Selector 双路径 / 日志宏 / 执行层）。

**验收标准**：你能指着图上的每一段，说出对应的源码函数名与文件行号；能解释为什么「改环境变量不用重新编译」（全部经由运行期 `getenv` + 解析，没有任何编译期绑定）；并能说清 `HCCL_ALGO`（设备门控、服务旧选择器）与 `HCCL_USE_NEW_SELECTOR`（无设备门控、决定选择路径）两个变量相互独立、各管一事。

**待本地验证**：上述 export 仅在装有 NPU 与 CANN 工具包的上板环境才能真正运行 AllReduce 并观察日志；在无卡环境，本实践为「源码阅读 + 数据流推导」型。

## 6. 本讲小结

- HCCL 用一个集中式容器 `AlgEnvConfig`（`thread_local` 每线程一份，配 `g_algEnvConfigMutex` 加锁）收纳所有环境变量解析结果，写端是 `InitEnvConfig()`，读端是一组 `GetExternalInput*()` getter 与本轮新增的 `IsNewSelectorEnabled()`——下游绝不直接 `getenv`。
- `InitEnvConfig()` 是编排器，按固定顺序调用 `Parse*`；`ParseOpExpansion()` 不受 `initialized` 守卫保护、每次算子调用都重新解析，其余变量只解析一次；每个步骤用 `RPT_ENV_ERR + CHK_PRT_RET` 两段式做「尽早失败」。
- **本轮变化一**：新增 `HCCL_USE_NEW_SELECTOR` 开关（默认关、只认 `"0"/"1"`、非法值直接报错），`Selector()` 与 `ReSelector()` 用 `IsNewSelectorEnabled() && SelectorEngine::IsOpSupported()` 做新旧选择器双路径分发，白名单目前仅 AllReduce/ReduceScatter/AllGather，其余算子自动回退旧路径。
- **本轮变化二**：`HCCL_ALGO` 只在 910_93（A3）设备上解析（`ParseHcclAlgo` 被设备门控包裹），其他设备（含 A5）跳过并打 INFO 日志，A5 的算法偏好由 costmodel 新流程承接；两个变化相互独立——前者决定「谁来做选择」，后者决定「旧算法偏好语法在哪里生效」。
- `HCCL_ALGO` 支持全局 / 按算子两种写法（不可混用），经 `Split*` 层层切分（`/` → `;` → `:`）后落入 `hcclAlgoConfig[opType][level]`，再经 `GetExternalInputHcclAlgoConfigAllType()` 被旧选择器的 `AutoSelectorBase::Select` 读为 `configAlgMap`，作为选择 `algName` 时的算法族约束。
- `HCCL_DEBUG_CONFIG` 用 `u64` 位掩码（ALG/TASK/RESOURCE）控制调试日志分类，支持 `^` 取反模式（初值 `~0ULL` 按需关闭）；它独立存放在 `config_log.cc` 的 `g_debugConfig`，不在 `AlgEnvConfig` 内。
- 所有 `Parse*` 都遵循「廉价优先」：先用 `GetEnv`/`IsValidNumberFormat` 做轻量校验，再查设备、转换数值、范围检查，最后才写入字段。

## 7. 下一步学习建议

- **回顾 u2-l4 通信引擎选择**：`ParseOpExpansion` 解析的展开模式，由 `HcclGetOpExpansionMode` 映射成 `OpParam.engine`（AICPU_TS/AIV/CCU），可对照 [u2-l4](u2-l4-engine-selection.md) 把「环境变量 → 引擎」这条链补全。
- **进入 Unit 5（通信引擎模板）**：本讲的 `aicpuUnfold`/`aivMode`/`ccuMSMode` 等展开模式开关，正是 [u5-l1 AICPU 模板](u5-l1-aicpu-template-kernel.md)、[u5-l3 AIV 模板](u5-l3-aiv-template.md)、[u5-l4 CCU 模板](u5-l4-ccu-template.md) 的入口条件，建议结合阅读。
- **进入 Unit 8（代价模型选择器）**：本讲 4.4 打开的新选择器大门，通向 [u8-l1 SelectorEngine 与双路径分发](u8-l1-selector-engine.md)、[u8-l2 CostModel 与 CostTable](u8-l2-cost-model-and-table.md)、[u8-l3 算法三维命名与 HCCL_ALGO 解析 alg_parse](u8-l3-algo-dims-and-parse.md)——后者会讲 `HCCL_ALGO` 在新流程下的全新解析方式（含 `not()` 排除语法），与本讲的旧语法形成对照。
- **扩展阅读**：浏览 `docs/zh/user_guide/hccl_env/` 下其余环境变量文档（如 `HCCL_OP_EXPANSION_MODE.md`），对照本讲的 `Parse*` 函数逐个印证，建立完整的变量—解析函数—字段—getter 对应表。
