# 环境变量与算法配置系统

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `AlgEnvConfig` 这个「环境变量总容器」里都装了哪些字段，以及 `hcclAlgoConfig` 这张「算子 → 三级算法」映射表是怎么组织的。
- 跟着 `InitEnvConfig()` 的源码，讲清它按什么顺序调用一串 `Parse*` 函数，以及「廉价优先、尽早失败」与「单次初始化守卫」两条设计原则。
- 把 `HCCL_ALGO="allreduce=level0:NA;level1:ring"` 这样的字符串，从读入、切分、校验，一路追到 `g_algEnvConfig.hcclAlgoConfig[opType][level]`，并解释它如何最终被 Selector（u3-l2）读取去影响 `algName`。
- 学会配置 `HCCL_ALGO`、`HCCL_DEBUG_CONFIG`、`HCCL_DETERMINISTIC`、`HCCL_EXEC_TIMEOUT` 等关键运行期开关。

本讲承接 [u4-l1 算法类型 AlgType 与分级选择](u4-l1-algtype.md)：u4-l1 讲的是「算法类型」在源码里的两套枚举（对外的 `HcclAlgoType`、对内的 `AlgTypeLevel0/1/2`）；本讲要回答的是——**用户在 shell 里 `export` 的环境变量，是怎么变成这些枚举值、并最终影响一次算子执行的**。

## 2. 前置知识

- **环境变量（environment variable）**：进程启动时从父进程继承的一组「键=值」字符串。C/C++ 用 `std::getenv("名字")` 读取。HCCL 用一组以 `HCCL_` 开头的环境变量来在不重新编译的前提下调优行为。
- **`HcclAlgoType` 与网络层级**：回顾 u4-l1，HCCL 把物理网络分成多层（节点内 / Server 间 / 超节点间）。`HCCL_ALGO` 用 `level0`/`level1`/`level2` 给每一层指定一个算法族（如 `ring`、`NHR`、`H-D_R`）。
- **Selector 产出的 `algName`**：回顾 u3-l2，算法选择器最终产出一个字符串 `algName`（如 `AicpuAllReduceSoleNHR`），它是后续 executor/template 注册表的查表键。本讲要讲清环境变量如何「向上」影响这个字符串的生成。
- **「廉价优先、尽早失败」**：在 u2-l3 入参校验里已见过这条原则——先把代价小的检查（空指针、格式）放在前面，不合格立刻返回错误，不做后续昂贵操作。本讲的环境变量解析也遵循它。

> 术语提示：本讲中 `level0/level1/level2/level3` 是 `HCCL_ALGO` 字符串里的层级记号；源码里对应的整数下标 `HCCL_ALGO_LEVEL_0..3` 与总数 `HCCL_ALGO_LEVEL_NUM` 定义在 HCOMM 仓的 `hccl_types.h` 中（本仓 `#include <hccl/hccl_types.h>` 引入）。从 `ParserHcclAlgoLevel` 的映射表与 `ParseAlgoString` 的日志可见共有 4 级，下标为 0~3，即 `HCCL_ALGO_LEVEL_NUM == 4`（数值待确认，但层级数量可由源码逻辑确定）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/common/alg_env_config.h` | 声明 `AlgEnvConfig` 结构、`SetDefaultParams` 默认值、`HcclAlgoTypeMap` 枚举↔字符串字典，以及所有 `Parse*` 函数与 `GetExternalInput*` 读取函数。 |
| `src/common/alg_env_config.cc` | 实现全部解析逻辑。核心是 `InitEnvConfig()` 编排器与 `ParseHcclAlgo()` 算法配置解析链；定义 `thread_local AlgEnvConfig g_algEnvConfig`。 |
| `src/common/config_log.cc` | `InitDebugConfigByEnv()` 解析 `HCCL_DEBUG_CONFIG`，用位掩码（ALG/TASK/RESOURCE）控制调试日志分类。 |
| `src/ops/op_common/selector/auto_selector_base.cc` | `AutoSelectorBase::Select` 通过 `GetExternalInputHcclAlgoConfigAllType()` 把环境变量配置读进 Selector，是「配置 → algName」的接驳点。 |
| `docs/zh/user_guide/hccl_env/HCCL_ALGO.md` | `HCCL_ALGO` 的官方说明（取值、全局/按算子两种写法、约束）。 |

---

## 4. 核心概念与源码讲解

### 4.1 AlgEnvConfig 数据结构与线程局部实例

#### 4.1.1 概念说明

HCCL 有几十个环境变量。如果每个变量都散落在各处「读一次、用一次」，代码会非常混乱，也无法保证「同一进程里所有算子看到一致的配置」。HCCL 的做法是：定义一个**集中式的配置容器** `AlgEnvConfig`，把所有环境变量解析后的结果装进它的字段；进程里任何代码想读配置，都从这个容器读，而不是再去 `getenv`。

这就把「读环境变量」和「用配置」两件事解耦了：

- **写端（解析）**：`InitEnvConfig()` 在算子入口被调用一次，把所有环境变量解析后填进容器。
- **读端（使用）**：selector、executor、template 等通过一组 `GetExternalInput*()` 函数从容器取值。

#### 4.1.2 核心流程

`AlgEnvConfig` 的生命周期可以概括为：

1. 算子入口（如 `HcclAllReduce`）调用 `InitEnvConfig()`。
2. `InitEnvConfig()` 依次调用各 `Parse*` 函数，每个函数读取一个（或一组）环境变量，校验后写入 `AlgEnvConfig` 的对应字段。
3. 后续执行链路通过 `GetExternalInput*()` 读取这些字段，不再触碰环境变量本身。

容器以「每个线程一份」的方式存在：

```cpp
static std::mutex g_algEnvConfigMutex;        // 保护跨线程读写
static thread_local AlgEnvConfig g_algEnvConfig; // 每线程一份配置快照
```

这两行见 [src/common/alg_env_config.cc:27-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L27-L28)：`thread_local` 意味着每个线程有自己独立的 `g_algEnvConfig` 实例，线程 A 改动不会影响线程 B；`g_algEnvConfigMutex` 在需要跨线程一致访问（如读取超时配置）时加锁。

> 为什么用 `thread_local`？回顾 u1-l5，HCCL 单算子程序常采用「单进程多线程、每线程绑一张卡」模型。`thread_local` 让每个线程的算子调用都能独立解析并缓存自己的配置快照，避免线程间争用，也允许不同线程在不同时刻使用不同的环境变量取值。

#### 4.1.3 源码精读

先看 `AlgEnvConfig` 结构本身与它的默认值。字段很多，但可分三类：**展开模式/引擎类**、**链路与重执行类**、**算法覆盖映射**。

[src/common/alg_env_config.h:34-81](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.h#L34-L81) 定义了 `AlgEnvConfig` 结构与 `SetDefaultParams()`，其中最关键的是末尾的算法覆盖映射：

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

这正是文档里「默认自适应、无需手工指定」的代码体现——只要用户没设 `HCCL_ALGO`，所有层都是 `DEFAULT`，交给 Selector 自己挑（u3-l2）。

另一个重要字典是 `HcclAlgoTypeMap`，它把枚举值翻译成人读的字符串，用于日志输出（见 [src/common/alg_env_config.h:83-97](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.h#L83-L97)）：

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

1. 打开 [src/common/alg_env_config.h:34-81](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.h#L34-L81)。
2. 把 `AlgEnvConfig` 的字段分成三组，填入下表（只列代表字段）：

   | 分类 | 字段 | 默认值 | 对应环境变量（先猜，后文对答案） |
   | --- | --- | --- | --- |
   | 引擎/展开模式 | `aicpuUnfold` / `aivMode` / `ccuMSMode` | `false` | HCCL_OP_EXPANSION_MODE |
   | 链路/重执行/确定性 | `interHccsDisable` / `hcclDeterministic` / `hcclRetryConfig[]` | `false` / `0` / `false` | （待对答案） |
   | 算法覆盖映射 | `hcclAlgoConfig` | 全 `DEFAULT` | HCCL_ALGO |

**需要观察的现象**：`SetDefaultParams()` 里每个字段都被显式赋了默认值，没有「忘记初始化」的字段。

**预期结果**：你能用一句话说出「AlgEnvConfig = 一组布尔/数值开关 + 一张 opType→四级算法族的映射表」。

**待本地验证**：无（纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hcclAlgoConfig` 用 `map<HcclCMDType, vector<HcclAlgoType>>` 而不是直接 `map<HcclCMDType, HcclAlgoType>`？

**参考答案**：因为算法是**分级**的——同一个算子，其节点内（level0）、Server 间（level1）、超节点间（level2）可以分别用不同的算法族。`vector` 的每个下标对应一级网络层级，这样才能独立描述每一层的算法选择（回顾 u4-l1 的 `TagAlgType` 三级组合思想）。

**练习 2**：`thread_local AlgEnvConfig g_algEnvConfig;` 与全局单例相比，最大的行为差异是什么？

**参考答案**：`thread_local` 使每个线程持有独立的配置实例。不同线程在算子入口解析到的环境变量取值互不覆盖；而全局单例只有一份，多线程同时写会产生数据竞争，必须全程加锁，性能与正确性都更差。

---

### 4.2 InitEnvConfig 编排与 GetEnv 封装

#### 4.2.1 概念说明

`InitEnvConfig()` 是整个环境变量子系统的**总入口（编排器，orchestrator）**。它本身不解析任何具体变量，只做三件事：

1. 决定**解析顺序**（哪个 `Parse*` 先调用）。
2. 对每个解析结果做**错误上报**（`RPT_ENV_ERR`）与**错误返回**（`CHK_PRT_RET`）。
3. 用 `initialized` 标志保证「耗时的解析只做一次」。

它被每个算子的入口（如 `HcclAllReduce`）在参数校验阶段调用，例如 reduce_scatter、all_to_all_v 等都在入口处 `CHK_RET(InitEnvConfig());`（见仓库内多算子的 `*_op.cc`）。

#### 4.2.2 核心流程

`InitEnvConfig()` 的解析顺序如下（自上而下）：

```
ParseOpExpansion()      // HCCL_OP_EXPANSION_MODE —— 不受 initialized 守卫，每次都解析
if (initialized) return // 单次守卫：以下只解析一次
ParseDeterministic()    // HCCL_DETERMINISTIC
ParseIntraLinkType()    // HCCL_INTRA_PCIE_ENABLE / HCCL_INTRA_ROCE_ENABLE
ParseEntryLogEnable()   // HCCL_ENTRY_LOG_ENABLE
ParseInterLinkType()    // HCCL_INTER_HCCS_DISABLE
ParseRetryEnable()      // HCCL_OP_RETRY_ENABLE
ParseExecTimeout()      // HCCL_EXEC_TIMEOUT
ParseMultipleDimensionSplitRatio()  // HCCL_ALG_MULTIPLE_DIMENSION_SPLIT_RATIO
ParseHcclAlgo()         // HCCL_ALGO —— 本讲重点（4.3）
InitDebugConfigByEnv()  // HCCL_DEBUG_CONFIG —— 本讲重点（4.4）
ParseDfsConfig()        // HCCL_DFS_CONFIG
initialized = true
```

每个 `Parse*` 后面都紧跟一对 `RPT_ENV_ERR(...) + CHK_PRT_RET(...)`，这是「**解析失败 → 上报 + 打日志 + 返回错误码**」的统一两段式。

#### 4.2.3 源码精读

先看 `GetEnv` 封装——所有 `Parse*` 都通过它读环境变量，而不是直接 `std::getenv`：

[src/common/alg_env_config.cc:30-41](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L30-L41) 把「未设置 / 空字符串」统一归一成一个哨兵值 `"EmptyString"`：

```cpp
std::string GetEnv(std::string IdName) {
    char* mmSysGetEnvValue = std::getenv(IdName.c_str());
    if (mmSysGetEnvValue != nullptr && mmSysGetEnvValue[0] != '\0') {
        return std::string(mmSysGetEnvValue);
    } else {
        return "EmptyString";   // 统一的「未设置」哨兵
    }
}
```

这样下游只需 `if (GetEnv("X") == "EmptyString")` 一句即可判断「变量是否有效」，避免到处写 `nullptr` 检查。注意：它把空字符串 `""` 也视为未设置。

接着看编排器主体。`InitEnvConfig()` 开头先解析 `HCCL_OP_EXPANSION_MODE`，且**故意放在 `initialized` 守卫之前**：

[src/common/alg_env_config.cc:176-194](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L176-L194)：

```cpp
HcclResult InitEnvConfig() {
    std::lock_guard<std::mutex> lock(g_algEnvConfigMutex);
    HcclResult ret = ParseOpExpansion();   // ① 每次调用都重新解析
    RPT_ENV_ERR(...);  CHK_PRT_RET(ret != HCCL_SUCCESS, ..., ret);

    if (g_algEnvConfig.initialized) {      // ② 守卫：其余解析只在首次执行
        return HCCL_SUCCESS;
    }
    ...
    g_algEnvConfig.initialized = true;     // ③ 解析完成后置位
    return HCCL_SUCCESS;
}
```

这是一个**关键设计细节**：`ParseOpExpansion()` 决定本次算子的「展开模式 / 引擎」（AICPU/AIV/CCU），它对每次算子调用都可能不同（且依赖 `hcclDeterministic` 等已解析字段），因此不受 `initialized` 守卫保护、每次都重新解析；而其余变量（链路、超时、算法配置等）一次解析即可复用，用 `initialized` 守卫跳过重复工作。

`ParseOpExpansion()` 的内部分支（[src/common/alg_env_config.cc:796-872](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L796-L872)）按字符串值把一组布尔开关置位，例如：

```cpp
if (opExpansionModeEnv == "CCU_MS")       { g_algEnvConfig.ccuMSMode = true; ... }
else if (opExpansionModeEnv == "AIV")     { g_algEnvConfig.aivMode = true; ... }
else if (opExpansionModeEnv == "AI_CPU")  { g_algEnvConfig.aicpuUnfold = true; ... }
```

这些布尔位后续会被 `HcclGetOpExpansionMode`（u2-l4）读取，决定 `OpParam.engine`。

每个解析步骤的错误处理是统一的，以 `HCCL_DETERMINISTIC` 为例（[src/common/alg_env_config.cc:196-208](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L196-L208)）：

```cpp
ret = ParseDeterministic();
RPT_ENV_ERR(ret != HCCL_SUCCESS, "EI0001", ...);  // 上报环境变量错误码 EI0001
CHK_PRT_RET(ret != HCCL_SUCCESS,
            HCCL_ERROR("[Init][EnvVarParam]... parse HCCL_DETERMINISTIC failed..."), ret);
```

`RPT_ENV_ERR` 把错误登记到 CANN 错误管理器（带错误码 `EI0001`、出错的值/变量名/期望值），`CHK_PRT_RET` 打印日志并返回错误码。这套两段式贯穿全部解析步骤，是「尽早失败」原则的落地。

#### 4.2.4 代码实践

**实践目标**：验证「`ParseOpExpansion` 每次执行，其余 `Parse*` 只执行一次」这一守卫行为。

**操作步骤**：

1. 打开 [src/common/alg_env_config.cc:176-347](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L176-L347)。
2. 在 `ParseOpExpansion()` 调用处（约 L180）与 `if (g_algEnvConfig.initialized)`（约 L192）之间画一条分隔线。
3. 数一数：分隔线之上有几个 `Parse*`？之下有几个？

**需要观察的现象**：线上方只有 `ParseOpExpansion` 一个；线下方有约 9 个 `Parse*` 调用，最后是 `initialized = true`。

**预期结果**：分隔线上方 1 个、下方 9 个。由此可推断「展开模式」是唯一每次都重新解析的项。

**待本地验证**：要确认运行时实际只调用一次，可在某个 `Parse*`（如 `ParseDeterministic`）函数体内临时加一行 `HCCL_INFO("ParseDeterministic called");`，运行两次 AllReduce，观察日志只打印一次（修改源码仅用于本地观察，勿提交）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `ParseIntraLinkType()` 返回 `HCCL_E_PARA`，`InitEnvConfig()` 会怎么走？

**参考答案**：紧跟其后的 `RPT_ENV_ERR` 登记错误码 `EI0001`（含实际值、变量名、期望值），`CHK_PRT_RET` 打印 `HCCL_ERROR` 日志并使 `InitEnvConfig` 立即返回该错误码，后续 `Parse*` 不再执行。算子入口的 `CHK_RET(InitEnvConfig())` 会把错误继续向上传播，本次算子调用失败。

**练习 2**：为什么 `GetEnv` 把空字符串和「未设置」都映射成 `"EmptyString"`？

**参考答案**：为了给所有 `Parse*` 提供一个统一的「未提供有效值」哨兵。下游用一个 `== "EmptyString"` 判断就能同时处理「变量不存在」和「变量为空」两种情况，避免每处都写 `nullptr` 与长度检查，也符合「未设置即用默认值」的语义。

---

### 4.3 HCCL_ALGO 解析全流程：从字符串到三级算法映射

#### 4.3.1 概念说明

`HCCL_ALGO` 是最常用、也最复杂的调优变量。它支持两种写法：

- **全局配置**（对所有算子生效）：`export HCCL_ALGO="level0:NA;level1:ring"`
- **按算子配置**（只对指定算子生效）：`export HCCL_ALGO="allreduce=level0:NA;level1:ring/allgather=level0:NA;level1:H-D_R"`

其中 `level0` 固定为 `NA`（节点内不由 HCCL 自决），`level1` 是 Server 间算法（`ring`/`NHR`/`H-D_R`/`pipeline` 等），`level2` 是超节点间算法。这正是 [docs/zh/user_guide/hccl_env/HCCL_ALGO.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/hccl_env/HCCL_ALGO.md) 描述的内容。

解析这条字符串的难点在于：它有**两层分隔符**——`/` 分隔不同算子，`;` 分隔不同层级，`:` 分隔层级名与算法名，`=` 分隔算子名与配置。HCCL 用一组递归的 `Split*` 函数层层剥开。

#### 4.3.2 核心流程

以 `"allreduce=level0:NA;level1:ring"` 为例，解析链如下：

```
ParseHcclAlgo()                       读 HCCL_ALGO
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

入口 `ParseHcclAlgo()` 很薄（[src/common/alg_env_config.cc:349-359](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L349-L359)）：

```cpp
std::string hcclAlgo = GetEnv("HCCL_ALGO");
if (hcclAlgo != "EmptyString") {
    CHK_RET(SetHcclAlgoConfig(hcclAlgo));
    HCCL_INFO("HCCL_ALGO set by environment to [%s]", hcclAlgo.c_str());
} else {
    HCCL_INFO("HCCL_ALGO is not set");   // 全 DEFAULT，自适应
}
```

`SetHcclAlgoConfig()`（[src/common/alg_env_config.cc:361-380](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L361-L380)）负责去空格、切 `/`、校验、再按「全局 vs 按算子」分流：

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

`CheckAlgoConfigValid()`（[src/common/alg_env_config.cc:575-597](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L575-L597)）有一条重要约束：**全局配置和按算子配置不能混用**，且全局配置只能有一段。它通过「每段是否含 `=`」区分两种模式：

```cpp
if (found != std::string::npos) { anySpecificConfig = true; }  // 含 '='
else                           { anyCommonConfig = true; }      // 不含 '='
if (anyCommonConfig && anySpecificConfig) { HCCL_ERROR("should not set both algo config way"); ... }
```

层级切分由两个递归函数完成，结构对称：

- `SplitHcclOpType`（按 `/` 切算子段，[src/common/alg_env_config.cc:557-572](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L557-L572)）
- `SplitHcclAlgoLevel`（按 `;` 切层级段，[src/common/alg_env_config.cc:599-622](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L599-L622)），且校验层级数不超过 `HCCL_ALGO_LEVEL_NUM`。

最底层是 `ParserHcclAlgoLevel()`（[src/common/alg_env_config.cc:464-512](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L464-L512)），把 `"level1:ring"` 拆成 `(level=HCCL_ALGO_LEVEL_1, algo=HCCL_ALGO_TYPE_RING)`：

```cpp
const std::map<std::string, u32> hcclAlgoLevelMap = {
    {"level0", HCCL_ALGO_LEVEL_0}, {"level1", HCCL_ALGO_LEVEL_1},
    {"level2", HCCL_ALGO_LEVEL_2}, {"level3", HCCL_ALGO_LEVEL_3}};
const std::map<std::string, HcclAlgoType> hcclAlgoTypeMap = {
    {"ring", HCCL_ALGO_TYPE_RING}, {"NHR", HCCL_ALGO_TYPE_NHR},
    {"H-D_R", HCCL_ALGO_TYPE_HDR}, {"NA", HCCL_ALGO_TYPE_NA}, ...};
```

`ParseAlgoString()`（[src/common/alg_env_config.cc:514-555](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L514-L555)）还做了两项保护：初始化整段为 `DEFAULT`、检测同一层级是否被重复配置（重复则报错）。最后用 `HcclAlgoTypeMap` 打出可读日志。

写回容器有两套：

- `SetCommonAlgType`（[src/common/alg_env_config.cc:401-409](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L401-L409)）：把同一段算法循环写入**所有** `HcclCMDType`。
- `SetSpecificAlgType`（[src/common/alg_env_config.cc:411-462](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L411-L462)）：按算子名映射（如 `"allreduce"→HCCL_CMD_ALLREDUCE`）只写指定算子；其中 `"others"` 是通配，对未单独配置的算子统一赋值。

**配置如何被 Selector 消费**——这是连接 u3-l2 的关键。在 `AutoSelectorBase::Select` 里，一进来就读取整张映射表并透传给各分支：

[src/ops/op_common/selector/auto_selector_base.cc:17-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L17-L28)：

```cpp
SelectorStatus AutoSelectorBase::Select(OpParam& opParam, TopoInfoWithNetLayerDetails* topoInfo,
                                        std::string& selectAlgName) const {
    std::map<HcclCMDType, std::vector<HcclAlgoType>> configAlgMap
        = GetExternalInputHcclAlgoConfigAllType();          // 读 HCCL_ALGO 解析结果
    ...
    ret = SelectCcuMsAlgo(topoInfo, opParam, configAlgMap, selectAlgName);
    ...
    ret = SelectAicpuAlgo(topoInfo, opParam, configAlgMap, selectAlgName);  // 透传给各 Select*Algo
}
```

`GetExternalInputHcclAlgoConfigAllType()`（[src/common/alg_env_config.cc:395-399](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L395-L399)）返回的就是 `g_algEnvConfig.hcclAlgoConfig` 的拷贝。`configAlgMap` 会被传进 `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAicpuAlgo` / `SelectAivAlgo` 等钩子（u3-l2）。各具体 selector 在分支决策时参考它——例如当 `configAlgMap[ALLREDUCE][LEVEL1] == RING` 时，会倾向选择 Ring 族的 `algName` 而非 NHR 族；同时该算法族也会影响拓扑形状计算（如 `Level1Nhr` 标志的设置），从而间接决定最终 `algName`（如 `AicpuAllReduceSoleNHR` 这类命名里的算法后缀）。**这就是「环境变量 → algName」的完整接驳路径**。

#### 4.3.4 代码实践

**实践目标**：把 `HCCL_ALGO="allreduce=level0:NA;level1:ring"` 这个字符串，手动跑一遍解析链，预测最终 `hcclAlgoConfig` 的内容，并解释它如何影响 Selector。

**操作步骤**：

1. 假设你已在 shell 里 `export HCCL_ALGO="allreduce=level0:NA;level1:ring"`。
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
4. 打开 [src/ops/op_common/selector/auto_selector_base.cc:17-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L17-L28)，找到 `configAlgMap = GetExternalInputHcclAlgoConfigAllType();` 这一行，说明它如何把上一步的结果交给 Selector。

**需要观察的现象**：`level1:ring` 最终变成 `hcclAlgoConfig[HCCL_CMD_ALLREDUCE][HCCL_ALGO_LEVEL_1] = HCCL_ALGO_TYPE_RING`，其余层为 `DEFAULT`；这个值经 `configAlgMap` 进入 Selector 的各 `Select*Algo`。

**预期结果**：

- 第 2 步表格：切段 `["allreduce=level0:NA;level1:ring"]`；`anySpecificConfig=true`；`opStringName="allreduce"→HCCL_CMD_ALLREDUCE`；切 `;` 得 `["level0:NA","level1:ring"]`；`algType[LEVEL0]=NA`、`algType[LEVEL1]=RING`。
- 第 3 步：`[NA, RING, DEFAULT, DEFAULT]`。
- 第 4 步：`configAlgMap` 是 `hcclAlgoConfig` 的拷贝，Selector 据此把 AllReduce 的 Server 间算法约束为 Ring 族，影响最终 `algName` 的算法后缀。

**待本地验证**：在上板环境运行一次 AllReduce，并在 `ParseAlgoString`（[src/common/alg_env_config.cc:551-553](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L551-L553)）的 `HCCL_INFO` 日志中确认输出 `config level0:NA, level1:ring, ...`。

#### 4.3.5 小练习与答案

**练习 1**：若用户误写 `HCCL_ALGO="level0:NA;level1:ring/allreduce=level0:NA;level1:NHR"`（全局段和按算子段混用），会发生什么？

**参考答案**：`CheckAlgoConfigValid` 检测到 `anyCommonConfig` 与 `anySpecificConfig` 同时为真，打印 `[CheckAlgoConfigValid]should not set both algo config way` 并返回 `HCCL_E_PARA`，`InitEnvConfig` 立即失败，算子调用报错。用户必须只用一种写法。

**练习 2**：`SetSpecificAlgType` 里的 `"others"` 关键字有什么作用？

**参考答案**：`"others"` 是按算子配置中的通配项——凡是没有被单独 `opName=...` 配置的算子，都会采用 `"others"` 指定的算法。这样用户可以「为多数算子设一个默认算法，再为个别算子单独覆盖」，而不必逐一列举所有算子。

**练习 3**：为什么说 `configAlgMap` 影响的是 `algName`，而不是直接替换它？

**参考答案**：`algName` 是一个复合字符串（引擎前缀 + 算子 + 编排 + 拓扑 + 搬运轮数，见 u3-l2），由 Selector 综合拓扑形状、数据量、设备类型**生成**。`configAlgMap` 只约束其中的「算法族」维度（如 Ring 还是 NHR），Selector 仍需结合拓扑与引擎决定具体的 `algName`。因此环境变量是「约束/偏好」，Selector 是「最终决策者」。

---

### 4.4 关键运行期开关：HCCL_DEBUG_CONFIG / HCCL_DETERMINISTIC / HCCL_EXEC_TIMEOUT

#### 4.4.1 概念说明

除了 `HCCL_ALGO`，还有三个高频调优变量值得单独理解：

- **`HCCL_DEBUG_CONFIG`**：控制**调试日志的分类**。HCCL 的调试日志分 ALG（算法选择）、TASK（任务下发）、RESOURCE（资源计算）几类，全开会非常啰嗦。这个变量用**位掩码（bitmask）**按需开关各类日志。
- **`HCCL_DETERMINISTIC`**：控制**确定性计算**。分布式训练里，同样的输入有时希望得到完全一致的输出（便于复现 bug）。该变量分三级：关闭 / 确定性（不保序）/ 严格（确定性 + 规约保序）。
- **`HCCL_EXEC_TIMEOUT`**：算子**执行超时**（秒），超时后上报，防止通信挂死时无限等待。

此外还有 `HCCL_OP_RETRY_ENABLE`（重执行）、`HCCL_DFS_CONFIG`（参数一致性校验等）等，解析套路一致，本节择重点讲。

#### 4.4.2 核心流程

**HCCL_DEBUG_CONFIG 的位掩码逻辑**有点特别——它支持「取反模式」：

- 普通写法 `HCCL_DEBUG_CONFIG="ALG,TASK"`：只打开 ALG 和 TASK 两类（其余关）。
- 取反写法 `HCCL_DEBUG_CONFIG="^RESOURCE"`：第一个字符是 `^`，表示「除了 RESOURCE 关闭，其余全开」。

位掩码的数学含义：用一个 `u64` 的不同位代表不同分类，按位或（`|`）打开、按位与取反（`& ~mask`）关闭。

**HCCL_DETERMINISTIC 的三态**用一个 `u8` 字段 `hcclDeterministic` 表示，对应枚举：

\[
\text{hcclDeterministic} \in \{0=\text{DISABLE},\ 1=\text{ENABLE},\ 2=\text{STRICT}\}
\]

其中 STRICT（规约保序）对设备有额外要求，并非所有芯片都支持。

**HCCL_EXEC_TIMEOUT** 是一个最多两位小数的非负数，超时单位为秒。

#### 4.4.3 源码精读

`HCCL_DEBUG_CONFIG` 由独立的 `config_log.cc` 解析（注意：它不在 `AlgEnvConfig` 结构里，而是用一个独立的 `static u64 g_debugConfig`）：

[src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/config_log.cc#L19-L57)：

```cpp
u64 g_debugConfig = 0ULL;
bool invert = (env[0] == '^');
g_debugConfig = invert ? ~0ULL : 0ULL;          // 取反模式初值：全1；普通模式：全0
char* configValue = (env[0] == '^') ? env + 1 : env;
...
if (strcasecmp(subConfig, "ALG") == 0)     { mask = HCCL_ALG; }
else if (strcasecmp(subConfig, "TASK") == 0){ mask = HCCL_TASK; }
else if (strcasecmp(subConfig, "RESOURCE") == 0){ mask = HCCL_RES; }
...
g_debugConfig = invert ? (g_debugConfig & (~mask)) : (g_debugConfig | mask);
```

关键在于 `invert` 分支：取反模式初值 `~0ULL`（所有位为 1，即所有类默认开），遇到用户列出的项则用 `& ~mask` 把对应位清零（即「关闭用户列出的」）；普通模式初值 `0`，遇到项则用 `| mask` 置位（即「打开用户列出的」）。这就是「`^` 表示反向选择」的位运算实现。

`HCCL_DETERMINISTIC` 的解析见 [src/common/alg_env_config.cc:993-1033](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L993-L1033)，它把字符串映射成三级枚举：

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

注意 STRICT 分支会调用 `HcclGetDeviceType`（u4-l2）校验芯片是否支持规约保序，不支持则返回 `HCCL_E_NOT_SUPPORT`——这又是「尽早失败」的体现。三级枚举定义在 [src/common/alg_env_config.h:28-32](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.h#L28-L32)。

`HCCL_EXEC_TIMEOUT` 的解析见 [src/common/alg_env_config.cc:75-110](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L75-L110)，体现「校验先于使用」：

```cpp
if (!IsValidNumberFormat(execTimeOutEnv, timeoutSize)) { ... return HCCL_E_PARA; }  // 格式校验
if (SalStrToDouble(execTimeOutEnv, execTimeOut) != HCCL_SUCCESS) { ... }            // 转换
if (execTimeOut > static_cast<double>(UINT32_MAX)) { ... }                          // 范围校验
g_algEnvConfig.execTimeOutSet = true;
g_algEnvConfig.execTimeout = execTimeOut;
```

它先用 `IsValidNumberFormat`（限制最多 2 位小数）做廉价格式校验，再转换、再范围校验，最后才写入 `execTimeout` 与配套的 `execTimeOutSet` 布尔位。读取端 `GetExternalInputExecTimeout`（[src/common/alg_env_config.cc:112-121](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L112-L121)）通过 `execTimeOutSet` 判断「用户是否设置过」，未设置则返回 `false` 让调用方走默认逻辑。

最后，所有这些字段都有一一对应的 `GetExternalInput*()` 读取函数（见 [src/common/alg_env_config.cc:1107-1146](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L1107-L1146)），例如 `GetExternalInputHcclDeterministic()`、`GetExternalInputHcclEnableEntryLog()`。下游模块（selector/executor/template）只认这些 getter，绝不直接读环境变量——这是「集中式配置」的纪律。

#### 4.4.4 代码实践

**实践目标**：理解 `HCCL_DEBUG_CONFIG` 的位掩码与取反模式，并能预测给定取值下哪些日志类会被打开。

**操作步骤**：

1. 假设三种取值，分别预测 `g_debugConfig` 的语义（哪些类开、哪些关）：

   | 取值 | invert | 初值 | 结果语义 |
   | --- | --- | --- | --- |
   | `"ALG"` | false | `0` | 只开 ALG |
   | `"ALG,TASK"` | false | `0` | 开 ALG、TASK，其余关 |
   | `"^RESOURCE"` | true | `~0ULL` | 关 RESOURCE，其余全开 |

2. 打开 [src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/config_log.cc#L19-L57)，对照 `invert` 与 `g_debugConfig = invert ? (... & ~mask) : (... | mask)` 验证你的预测。
3. 思考：如果要「关闭所有调试日志」，应该怎么设置？（提示：默认未设置时 `g_debugConfig = 0`，即全关。）

**需要观察的现象**：取反模式与普通模式互为镜像——普通模式从「全关」开始按需打开，取反模式从「全开」开始按需关闭。

**预期结果**：表格三行预测成立；关闭全部日志的最简单做法是「不设置 `HCCL_DEBUG_CONFIG`」（走默认 `0`）。

**待本地验证**：在上板环境分别 `export HCCL_DEBUG_CONFIG="ALG"` 与 `"^RESOURCE"`，运行同一算子，对比日志详略程度。

#### 4.4.5 小练习与答案

**练习 1**：`HCCL_DETERMINISTIC=strict` 在不支持的设备上会发生什么？

**参考答案**：`ParseDeterministic` 的 STRICT 分支调用 `HcclGetDeviceType` 检测，若设备不属于 `910B`/`910_93`/OutPlace 设备，则打印错误并返回 `HCCL_E_NOT_SUPPORT`，`InitEnvConfig` 失败，算子调用报错。

**练习 2**：为什么 `HCCL_EXEC_TIMEOUT` 要配套一个 `execTimeOutSet` 布尔位，而不直接用 `execTimeout == 0` 表示「未设置」？

**参考答案**：因为 `0` 是一个合法的超时值（理论上可表示「不等待」），若用它兼作「未设置」标志会产生歧义。引入独立的 `execTimeOutSet` 布尔位可以精确区分「用户显式设了 0」与「用户没设置」，让读取端 `GetExternalInputExecTimeout` 能正确返回「是否设置」的语义。

---

## 5. 综合实践

把本讲三条主线串起来，完成一个「**环境变量端到端追踪**」任务。

**场景**：你需要为一次 AllReduce 调优——要求 Server 间强制使用 Ring 算法，同时打开算法选择阶段的调试日志以便观察 Selector 的决策，并把执行超时设为 600 秒。

**任务**：

1. **写出三个 export 命令**（基于本讲学到的取值语法）：
   ```bash
   export HCCL_ALGO="allreduce=level0:NA;level1:ring"
   export HCCL_DEBUG_CONFIG="ALG"
   export HCCL_EXEC_TIMEOUT="600"
   ```
2. **追踪 `HCCL_ALGO` 的完整路径**（结合 4.3）：从 `GetEnv` → `SetHcclAlgoConfig` → `SplitHcclOpType` → `CheckAlgoConfigValid` → `SetSpecificAlgType` → `ParseAlgoString` → `ParserHcclAlgoLevel`，最终落到 `hcclAlgoConfig[HCCL_CMD_ALLREDUCE] = [NA, RING, DEFAULT, DEFAULT]`，再经 `GetExternalInputHcclAlgoConfigAllType()` 进入 `AutoSelectorBase::Select` 的 `configAlgMap`，约束 Selector 选择 Ring 族的 `algName`。
3. **解释另两个变量的生效点**：`HCCL_DEBUG_CONFIG` 在 `InitDebugConfigByEnv`（[src/common/config_log.cc:19-57](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/config_log.cc#L19-L57)）解析为 `g_debugConfig` 的 ALG 位，供日志宏判断是否打印算法选择日志；`HCCL_EXEC_TIMEOUT` 在 `ParseExecTimeout`（[src/common/alg_env_config.cc:75-110](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L75-L110)）解析为 `execTimeout=600`，由执行层读取作为超时阈值。
4. **画一张数据流图**，包含：shell 环境变量 → `GetEnv` → `Parse*` → `AlgEnvConfig` 字段 → `GetExternalInput*()` → 下游消费者（Selector / 日志宏 / 执行层）。

**验收标准**：你能指着图上的每一段，说出对应的源码函数名与文件行号；并能解释为什么「改环境变量不用重新编译」（因为全部经由运行期 `getenv` + 解析，没有任何编译期绑定）。

**待本地验证**：上述 export 仅在装有 NPU 与 CANN 工具包的上板环境才能真正运行 AllReduce 并观察日志；在无卡环境，本实践为「源码阅读 + 数据流推导」型。

## 6. 本讲小结

- HCCL 用一个集中式容器 `AlgEnvConfig`（`thread_local` 每线程一份，配 `g_algEnvConfigMutex` 加锁）收纳所有环境变量解析结果，写端是 `InitEnvConfig()`，读端是一组 `GetExternalInput*()` getter——下游绝不直接 `getenv`。
- `InitEnvConfig()` 是编排器，按固定顺序调用 `Parse*`；其中 `ParseOpExpansion()` 不受 `initialized` 守卫保护、每次算子调用都重新解析，其余变量只解析一次；每个步骤用 `RPT_ENV_ERR + CHK_PRT_RET` 两段式做「尽早失败」。
- `HCCL_ALGO` 支持全局 / 按算子两种写法（不可混用），经 `Split*` 层层切分（`/` → `;` → `:`）后落入 `hcclAlgoConfig[opType][level]`，再经 `GetExternalInputHcclAlgoConfigAllType()` 被 `AutoSelectorBase::Select` 读为 `configAlgMap`，作为 Selector 选择 `algName` 时的算法族约束。
- `HCCL_DEBUG_CONFIG` 用 `u64` 位掩码（ALG/TASK/RESOURCE）控制调试日志分类，支持 `^` 取反模式（初值 `~0ULL` 按需关闭）；它独立存放在 `config_log.cc` 的 `g_debugConfig`，不在 `AlgEnvConfig` 内。
- `HCCL_DETERMINISTIC` 分 DISABLE/ENABLE/STRICT 三级（STRICT 需设备支持规约保序）；`HCCL_EXEC_TIMEOUT` 用配套布尔位 `execTimeOutSet` 精确区分「未设置」与「设为 0」。
- 所有 `Parse*` 都遵循「廉价优先」：先用 `GetEnv`/`IsValidNumberFormat` 做轻量校验，再查设备、转换数值、范围检查，最后才写入字段。

## 7. 下一步学习建议

- **进入 Unit 5（通信引擎模板）**：本讲的 `aicpuUnfold`/`aivMode`/`ccuMSMode` 等展开模式开关，正是 [u5-l1 AICPU 模板](u5-l1-aicpu-template-kernel.md)、[u5-l3 AIV 模板](u5-l3-aiv-template.md)、[u5-l4 CCU 模板](u5-l4-ccu-template.md) 的入口条件，建议结合阅读。
- **回顾 u2-l4 通信引擎选择**：`ParseOpExpansion` 解析的展开模式，由 `HcclGetOpExpansionMode` 映射成 `OpParam.engine`（AICPU_TS/AIV/CCU），可对照 [u2-l4](u2-l4-engine-selection.md) 把「环境变量 → 引擎」这条链补全。
- **扩展阅读**：浏览 `docs/zh/user_guide/hccl_env/` 下其余环境变量文档（如 `HCCL_OP_EXPANSION_MODE.md`），对照本讲的 `Parse*` 函数逐个印证，建立完整的变量—解析函数—字段—getter 对应表。
