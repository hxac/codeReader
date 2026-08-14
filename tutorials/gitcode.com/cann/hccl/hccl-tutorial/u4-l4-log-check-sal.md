# 日志、参数校验与 SAL/ACL 适配

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 HCCL 日志系统的三层结构：预定义日志宏（`HCCL_INFO`/`HCCL_ERROR` 等）、级别判定与缓存（`HcclCheckLogLevel`）、错误转告警开关（ErrToWarn），以及建立在它们之上的分类调试日志 `config_log`。
- 区分四类「横切工具」的职责边界：**日志**（`HCCL_*`）、**返回值校验**（`CHK_RET`/`CHK_PTR_NULL`）、**错误码上报**（`RPT_INPUT_ERR`/`HCOM_ERROR_CODE`）、**外部 API 适配**（`ACLCHECK`/`haclrt*`），并能一眼看出算子代码里某一行属于哪一类。
- 解释 `param_check` 里「跨算子复用的 `HcomCheck*`」与 `op_common` 里「带归约语义的 `Check*`」两套并存校验族的差异，以及它们如何配合「廉价优先、尽早失败」原则。
- 理解 `sal` 字符串工具与 `weak_alias` 解耦机制、`adapter_acl` 如何把 CANN 运行时（ACL）调用统一封装进 HCCL 的错误码/日志世界。

本讲是 Unit 4「公共基础」的收尾，承接 [u2-l3 OpParam 参数结构与入参校验](u2-l3-opparam-and-check.md)：u2-l3 讲的是「校验发生在链路的哪个位置、按什么顺序」，本讲要下沉到**校验与日志这一层用到的具体工具是如何实现的**——这些工具被几乎所有算子统一复用，是阅读任何算子入口代码前必须先认熟的「公共词汇表」。

## 2. 前置知识

- **横切关注点（cross-cutting concern）**：指那些「不属于某个具体业务、却被所有业务代码用到」的基础能力，如日志、参数校验、错误上报。HCCL 把它们集中在 `src/common/` 下，让每个算子复用同一套实现，而不是各自造轮子。
- **`HcclResult` 返回码**：HCCL 几乎所有内部函数都返回 `HcclResult`（成功为 `HCCL_SUCCESS`，否则为 `HCCL_E_PARA`/`HCCL_E_PTR`/`HCCL_E_INTERNAL`/`HCCL_E_RUNTIME`/`HCCL_E_NOT_SUPPORT` 等错误码）。这是 `CHK_*` 宏赖以传播错误的载体。
- **「廉价优先、尽早失败」**：u2-l3 已见过——把代价小的检查（空指针、范围、格式）放在前面，不合格立刻返回，不做后续昂贵操作。本讲的 `CHK_*` 与 `RPT_*` 就是实现这条原则的「螺丝刀」。
- **两仓解耦与弱符号**：回顾 u1-l1/u4-l2，HCCL 与 HCOMM 解耦，跨仓调用经 `dlsym`；对外符号常用 `weak` 属性，允许 HCOMM 或测试桩以强符号覆盖。本讲的 `weak_alias` 宏与 `RPT_*` 弱符号是同一思路在 `common` 层的体现。

> 术语提示：本讲多次出现 `tid`（thread id，线程号）、`module id`（日志模块号，HCCL 为 `5`）、`ACL`（Ascend Computing Language，昇腾计算运行时，提供 `aclrtMemcpy`/`aclrtGetDeviceInfo` 等 device 侧 API）。`aclrt*` 是 CANN 运行时原始接口，HCCL 用 `haclrt*` 包装它们（多一个 `h` 前缀），下文会讲清两者的关系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/common/log.h` | 预定义日志宏（`HCCL_INFO`/`HCCL_ERROR`/`HCCL_RUN_INFO`…）、错误码编码宏（`HCCL_ERROR_CODE`/`HCOM_ERROR_CODE`）、全部 `CHK_*` 校验宏。是本讲的「宏中心」。 |
| `src/common/log.cc` | `HcclCheckLogLevel` 的级别缓存实现、`ProbeLogLevel` 探测、`SetErrToWarnSwitch`/`IsErrorToWarn` 错误转告警开关。 |
| `src/common/config_log.h` / `config_log.cc` | 分类调试日志：`HCCL_CONFIG_INFO` 宏 + `HCCL_ALG`/`HCCL_TASK`/`HCCL_RES` 位掩码；`InitDebugConfigByEnv` 解析 `HCCL_DEBUG_CONFIG`（解析细节见 u4-l3）。 |
| `src/common/adapter_error_manager_pub.h` | `RPT_INPUT_ERR`/`RPT_ENV_ERR` 宏与弱符号 `RptInputErr`/`RptEnvErr`，把错误上报委托给平台错误管理框架。 |
| `src/common/param_check.h` / `param_check.cc` | 跨算子复用的 `HcomCheck*` 校验族（Tag/Count/DataType/ReductionOp/UserRank/GroupName）。 |
| `src/ops/op_common/op_common.cc`（节选） | 每算子使用的 `Check*` 校验族（`CheckCount`/`CheckDataType(needReduce)`/`CheckReduceOp`/`HcclCheckTag`），带归约语义与更丰富的错误上报。 |
| `src/common/sal.h` / `sal.cc` | 字符串/数值转换工具（`SalStrToULong`/`SalStrToDouble`/`IsAllDigit`）与 `weak_alias` 解耦宏。 |
| `src/common/adapter_acl.h` / `adapter_acl.cc` | ACL 适配层：`ACLCHECK` 宏 + `haclrt*`/`LoadBinaryFromFile` 包装函数，把 `aclrt*` 调用统一封装进 HCCL 错误码/日志世界。 |
| `src/ops/all_reduce/all_reduce_op.cc` | 实践对象：`HcclAllReduce`/`AllReduceInitAndCheck`/`CheckAllReduceInputPara`，集中演示上述工具的真实用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 日志系统：日志宏、级别判定、ErrToWarn 与分类调试日志

#### 4.1.1 概念说明

HCCL 有几十个算子，每个算子都要在「入口、关键分支、出错点」打日志。如果每个地方都手写「判断级别 → 拼文件名行号线程号 → 调用底层日志接口」，代码会被样板代码淹没。`log.h` 的做法是提供一组**预定义日志宏**，把样板封装起来，调用方只需写 `HCCL_INFO("xxx val[%d]", v);`。

这套日志系统有三个关键设计：

1. **级别判定先于格式化**：日志宏先用 `HcclCheckLogLevel` 判断当前级别是否启用，未启用就**直接跳过**后续的格式化与 `DlogRecord` 调用，避免无谓开销。判定结果被缓存，不必每次都查。
2. **`UNLIKELY`/`LIKELY` 分支预测提示**：日志是「冷路径」（多数情况下级别未开），用 `UNLIKELY` 告诉编译器把「不打日志」放在快速路径上。
3. **「运行日志（RUN_LOG）」永远输出**：带 `RUN_LOG_MASK` 的日志（`HCCL_RUN_INFO` 等）绕过级别缓存，始终打印，用于必须让用户看到的关键运行信息。

#### 4.1.2 核心流程

一条 `HCCL_INFO(...)` 的执行流程：

```text
HCCL_INFO(fmt, args)
  └─ if (UNLIKELY(HcclCheckLogLevel(HCCL_LOG_INFO)))   // 先判级别（命中缓存）
        └─ HCCL_LOG_PRINT(HCCL, HCCL_LOG_INFO, fmt, args)
              └─ LOG_FUNC(module, level, "[%s:%d] [%u]" fmt, __FILE__, __LINE__, tid, args)
                    └─ DlogRecord(module, level, ...)   // 底层 dlog 接口
```

而 `HCCL_ERROR(...)` 多一道「错误转告警」闸门：

```text
HCCL_ERROR(fmt, args)
  └─ if (LIKELY(HcclCheckLogLevel(HCCL_LOG_ERROR)))
        └─ HCCL_ERROR_LOG_PRINT(fmt, args)
              └─ if (IsErrorToWarn())   // ErrToWarn 开关打开？
                    用 HCCL_LOG_WARN 打 "ErrToWarn:" 前缀
                  else
                    用 HCCL_LOG_ERROR 正常打 ERROR
```

级别判定 `HcclCheckLogLevel(logType, moduleId)` 的判定顺序：

```text
if (moduleId 含 RUN_LOG_MASK 位)  → 直接返回 true（运行日志永远输出）
else 用缓存的 g_logLevelCache 比较：logType >= cache ? true : false
     （缓存为 INVALID 时，首次调用 ProbeLogLevel 探测并写入）
```

#### 4.1.3 源码精读

**预定义日志宏**——以 `HCCL_INFO` 与 `HCCL_ERROR` 为代表，注意前者用 `UNLIKELY`、后者用 `LIKELY`，且 `HCCL_ERROR` 走带 ErrToWarn 分支的 `HCCL_ERROR_LOG_PRINT`：

[src/common/log.h:99-118](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L99-L118) 定义 `HCCL_INFO`/`HCCL_WARNING`（`UNLIKELY` 判级别后用 `HCCL_LOG_PRINT`），以及 `HCCL_ERROR`（`LIKELY` 判级别后用 `HCCL_ERROR_LOG_PRINT`）。

**ErrToWarn 分支**——`HCCL_ERROR_LOG_PRINT` 根据 `IsErrorToWarn()` 决定把 ERROR 降级为 WARN（前缀 `ErrToWarn:`），用于某些场景不想让 ERROR 触发告警链路：

[src/common/log.h:68-78](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L68-L78) 是 `HCCL_ERROR_LOG_PRINT` 的两分支实现。

**日志前缀拼接**——每条日志自动带上 `[文件名:行号] [线程号]`，便于定位：

[src/common/log.h:63-66](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L63-L66) `HCCL_LOG_PRINT` 用 `__FILE__`/`__LINE__`/`syscall(SYS_gettid)` 拼前缀。

**级别判定与缓存**——`HcclCheckLogLevel` 是所有日志宏的「总闸」；运行日志直接放行，普通日志走 `atomic` 缓存：

[src/common/log.cc:30-44](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.cc#L30-L44) 是 `HcclCheckLogLevel`：先判 `RUN_LOG_MASK`，再用 `g_logLevelCache`（`std::atomic<int32_t>`，初值 `-1` 即 `INVALID`）比较。

[src/common/log.cc:17-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.cc#L17-L28) 是缓存变量 `g_logLevelCache` 与探测函数 `ProbeLogLevel`（优先用 `acllogCheckDebugLevel`，探测 DEBUG→INFO→WARN→ERROR 的最低开启级别）。

**ErrToWarn 开关**——`thread_local` 变量，每线程独立：

[src/common/log.cc:14-53](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.cc#L14-L53) 定义 `thread_local bool g_hcclErrToWarn`、`SetErrToWarnSwitch(bool)` 与 `IsErrorToWarn()`。

**分类调试日志 config_log**——建立在 `log.h` 之上的「按类别开关」机制。普通日志靠「全局级别」控制，而 `HCCL_CONFIG_INFO(config, ...)` 靠「位掩码」控制：只有 `GetDebugConfig() & config` 命中时才打印带 `[configName]:` 前缀的详细日志，否则回退为普通 INFO/DEBUG 日志。这样无需抬高全局级别，就能单独打开「算法（ALG）/任务（TASK）/资源（RESOURCE）」某一类的详尽日志：

[src/common/config_log.h:17-36](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/config_log.h#L17-L36) 定义三个位掩码常量 `HCCL_ALG`/`HCCL_TASK`/`HCCL_RES`，以及 `HCCL_CONFIG_INFO` 宏（命中掩码时带 configName 前缀打印，否则回退）。

[src/common/config_log.cc:15-57](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/config_log.cc#L15-L57) 是 `g_debugConfig` 全局位掩码与 `InitDebugConfigByEnv()`（解析 `HCCL_DEBUG_CONFIG`，支持 `^` 取反模式）。环境变量解析的完整流程已在 [u4-l3](u4-l3-env-config.md) 讲过，这里只需记住：`config_log` 是「日志宏 + 位掩码」的组合，让分类调试日志不必依赖全局级别。

#### 4.1.4 代码实践

1. **实践目标**：验证「级别判定先于格式化」与「运行日志绕过缓存」两点。
2. **操作步骤**：
   - 阅读 [src/common/log.h:99-104](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L99-L104)，确认 `HCCL_INFO` 在 `HcclCheckLogLevel` 返回 false 时不会执行 `HCCL_LOG_PRINT`。
   - 阅读 [src/common/log.cc:30-44](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.cc#L30-L44)，对比「普通 INFO」（走缓存比较）与「`HCCL_RUN_INFO`」（传 `HCCL_LOG_MASK`，因含 `RUN_LOG_MASK` 而直接返回 true）的差别。
3. **需要观察的现象**：在 `HcclCheckLogLevel` 里设断点，分别触发一次 `HCCL_INFO` 和一次 `HCCL_RUN_INFO`，观察前者命中 `g_logLevelCache` 比较分支、后者命中首行 `(moduleId & RUN_LOG_MASK) != 0` 的早返回。
4. **预期结果**：`HCCL_RUN_INFO` 无论全局级别如何都会输出；`HCCL_INFO` 仅在全局级别 ≤ INFO 时输出。
5. **待本地验证**：上述断点观察需在带 NPU 与 dlog 环境的机器上运行；无环境时可做纯源码阅读型实践——画出 `HCCL_INFO` 与 `HCCL_RUN_INFO` 各自的判定路径图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HCCL_INFO` 用 `UNLIKELY(HcclCheckLogLevel(...))`，而 `HCCL_ERROR` 用 `LIKELY(...)`？

> **答案**：生产环境通常把级别设为 INFO 以上、甚至只开 ERROR，因此「`HCCL_INFO` 实际不打印」是大概率事件，用 `UNLIKELY` 让编译器把它排到快速路径之外；反之「`HCCL_ERROR` 需要打印」是大概率事件（出错就该记录），用 `LIKELY` 优化打印分支。两者都是用分支预测提示降低日志开销。

**练习 2**：`IsErrorToWarn()` 为 true 时，原本的 `HCCL_ERROR(...)` 会变成什么级别？为什么用 `thread_local`？

> **答案**：会降级为 `HCCL_LOG_WARN` 并加 `ErrToWarn:` 前缀（见 [log.h:68-78](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L68-L78)）。用 `thread_local` 是因为不同线程可能处于不同上下文（如有的线程在图捕获模式下不希望 ERROR 触发外部告警），让开关按线程独立、互不干扰。

---

### 4.2 校验宏与错误码体系

#### 4.2.1 概念说明

算子代码里随处可见两类「检查」：

- **返回值校验**：调用一个返回 `HcclResult` 的函数，若失败则记录日志并**立刻从当前函数返回该错误码**。这是错误向上传播的主干道，由 `CHK_*` 家族承担。
- **错误码上报**：在判定出「用户入参非法」时，除了自己打日志、返回错误码，还要**把这条错误按平台标准格式上报**给错误管理框架（让上层框架/用户看到结构化的错误说明）。这由 `RPT_*` 家族承担。

两者常常**成对出现**：先用 `RPT_INPUT_ERR` 上报结构化错误，再用 `CHK_PTR_NULL`/`CHK_PRT_RET` 打日志并返回。这就是 u2-l3 提到的「两段式」错误处理。

此外还有一个**错误码编码**机制 `HCCL_ERROR_CODE`/`HCOM_ERROR_CODE`，把「模块号 + 子模块号 + 错误号」压进一个 64 位整数，让日志里的 `errNo[0x...]` 能反查出错误来源。

#### 4.2.2 核心流程

**返回值校验主干 `CHK_RET(call)`**：

```text
HcclResult hcclRet = call;                 // 执行调用
if (hcclRet != HCCL_SUCCESS) {
    if (hcclRet == HCCL_E_AGAIN) WARN(...) // 可重试错误降为告警
    else                          ERROR(...)
    return hcclRet;                         // 向上传播
}
```

**指针校验 `CHK_PTR_NULL(ptr)`**：

```text
if (ptr == nullptr) {
    HCCL_ERROR(... HCCL_ERROR_CODE(HCCL_E_PTR) ... ptr 名字);
    return HCCL_E_PTR;
}
```

**通用三参校验 `CHK_PRT_RET(result, exeLog, retCode)`**：最灵活的积木——`result` 为真则执行 `exeLog`（通常是一条 `HCCL_ERROR`）并 `return retCode`。前面的 `HcomCheck*` 函数里大量用它来「上报后返回自定义码」。

**错误码上报 `RPT_INPUT_ERR(result, error_code, key, value)`**：

```text
if (result 为真 && RptInputErr 弱符号非空)
    RptInputErr(error_code, key, value);   // 委托给平台错误管理
```

注意 `RptInputErr` 是**弱符号**：若链接时没提供错误管理实现，`RptInputErr == nullptr`，宏整体变成空操作——这是一种「可选依赖」的解耦方式。

#### 4.2.3 源码精读

**`CHK_RET`——返回值校验主干**，注意 `HCCL_E_AGAIN` 降级为 WARNING 的细节：

[src/common/log.h:201-212](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L201-L212) 是 `CHK_RET`：捕获返回值，失败时按 `HCCL_E_AGAIN` 与否选择 WARN/ERROR，再 `return hcclRet`。它出现在 `AllReduceInitAndCheck` 的几乎每一行（见 4.4.3）。

**`CHK_PTR_NULL`——空指针校验**，用 `HCCL_ERROR_CODE(HCCL_E_PTR)` 编码错误号，并打印指针的「字符串化名」`#ptr`：

[src/common/log.h:173-181](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L173-L181) 是 `CHK_PTR_NULL`。

**`CHK_PRT_RET`——通用积木**，是「自定义错误码 + 自定义日志」场景的基础：

[src/common/log.h:184-190](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L184-L190) 是 `CHK_PRT_RET(result, exeLog, retCode)`。对比 `CHK_PRT`（[log.h:244-254](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L244-L254)）只打日志、不 return，用于「记录但继续往下走」的场景。

**`CHK_RET_AND_PRINT_IDE`——带通信域标识的校验**，在出错时额外打印 `identifier`（通常是 `param.tag`），便于在海量日志里定位是哪个通信域出错：

[src/common/log.h:215-227](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L215-L227) 是 `CHK_RET_AND_PRINT_IDE`。`HcclAllReduce` 正是用它包裹主执行调用（见 4.4.3）。

**错误码编码**——把模块号(5)、子模块号、错误号压进一个 64 位整数，子模块区分 HCCL/HCOM/CLTM/CUSTOM_OP：

[src/common/log.h:142-147](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L142-L147) 是 `HCCL_ERROR_CODE`/`HCOM_ERROR_CODE`，对应常量 `HCCL_MODULE_ID = 5`（[log.h:88-89](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h#L88-L89)）。

**错误码上报宏与弱符号**——`RPT_INPUT_ERR` 委托给弱符号 `RptInputErr`，`RPT_ENV_ERR` 委托给 `RptEnvErr`：

[src/common/adapter_error_manager_pub.h:18-35](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_error_manager_pub.h#L18-L35) 声明两个弱符号并定义 `RPT_INPUT_ERR`/`RPT_ENV_ERR`。`RptInputErr` 不为空时才真正上报，体现「错误上报是可选依赖」。

#### 4.2.4 代码实践

1. **实践目标**：用一个真实调用点看清「`RPT_INPUT_ERR` 上报 + `CHK_PTR_NULL` 校验返回」的两段式配合。
2. **操作步骤**：打开 [src/ops/all_reduce/all_reduce_op.cc:135-157](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L135-L157)（`CheckAllReduceInputPara`），观察 `stream`/`comm`/`sendBuf`/`recvBuf` 四个入参，每个都是「先 `RPT_INPUT_ERR(...)` 上报 `EI0003`，紧接 `CHK_PTR_NULL(...)` 返回 `HCCL_E_PTR`」。
3. **需要观察的现象**：若 `stream == nullptr`，会先触发 `RptInputErr("EI0003", {ccl_op,value,parameter,expect}, {"HcclAllReduce","nullptr","stream","non-null pointer"})`，再由 `CHK_PTR_NULL` 打 `HCCL_ERROR` 并 `return HCCL_E_PTR`。
4. **预期结果**：错误会同时出现在「平台错误管理通道（结构化 EI0003）」和「HCCL 日志（errNo 0x...）」两个地方，且函数立即返回，不再继续。
5. **待本地验证**：错误管理通道的具体呈现形式依赖部署环境的错误管理组件；无该组件时 `RptInputErr` 为空，只剩日志通道，这是弱符号解耦的正常表现。

#### 4.2.5 小练习与答案

**练习 1**：`CHK_RET(call)` 与 `CHK_PRT_RET(result, exeLog, retCode)` 有何本质区别？分别在什么场景用？

> **答案**：`CHK_RET` 专门处理「返回 `HcclResult` 的调用」，自动捕获返回值并原样向上传播，调用方不用关心具体错误码；`CHK_PRT_RET` 是通用三参积木，由调用方自行提供判定条件 `result`、日志 `exeLog`、返回码 `retCode`，适合「返回值不是 `HcclResult`」（如 `sprintf_s` 返回 `int`）或「想返回一个与调用返回值不同的错误码」的场景。

**练习 2**：为什么 `RPT_INPUT_ERR` 里要判断 `RptInputErr != nullptr`？

> **答案**：`RptInputErr` 是 `__attribute__((weak))` 弱符号。若 HCCL 被链接进一个没有提供错误管理实现的环境（如某些精简测试桩），该符号为空指针；此时直接调用会崩溃。加 `!= nullptr` 判断后，宏整体退化为空操作，保证「错误上报是可选能力」而不影响核心流程。

---

### 4.3 param_check：两套并存的校验函数族

#### 4.3.1 概念说明

有了 4.2 的校验宏，就可以把「校验某个字段是否合法」封装成函数复用。HCCL 里**并存两套**校验函数族（u2-l3 已点出，本讲展开实现）：

| 校验族 | 位置 | 命名 | 特点 |
| --- | --- | --- | --- |
| **`HcomCheck*`** | `src/common/param_check.{h,cc}` | `HcomCheckTag`/`HcomCheckCount`/`HcomCheckDataType`/`HcomCheckReductionOp`/`HcomCheckUserRank`/`HcomCheckGroupName` | **跨算子正交复用**：只校验字段本身的合法性，不带算子语义；用 `HCOM_ERROR_CODE` 编码；任何算子都能直接调。 |
| **`Check*`** | `src/ops/op_common/op_common.cc` | `CheckCount`/`CheckDataType(dataType, needReduce)`/`CheckReduceOp`/`HcclCheckTag` | **带算子/归约语义**：`CheckDataType` 多一个 `needReduce` 形参，能区分「AllReduce 不支持 FP8、AllGather 支持」；每个校验配 `RPT_INPUT_ERR` 给出更丰富的「期望值」说明。 |

两套并存的原因：`HcomCheck*` 是「与具体算子无关的最小工具」，方便正交复用（如 `HcclAllReduce` 入口校验里仍用 `HcomCheckUserRank`）；`Check*` 是「面向算子入口的增强版」，把「这个算子到底支持哪些数据类型/归约」的知识封装进去。

#### 4.3.2 核心流程

以 `AllReduceInitAndCheck` 的校验段为例（4.4.3 给出真实代码），校验按「廉价优先」顺序排成一条链：

```text
HcomCheckUserRank(rankSize, userRank)   // 跨算子族：rank 范围
CheckCount(count)                        // 每算子族：count 上限
CheckDataType(dataType, needReduce=true) // 每算子族：带归约语义
CheckReduceOp(dataType, op)              // 每算子族：PROD 只支持部分类型
```

每个 `Check*`/`HcomCheck*` 内部统一遵循「检查 → 失败则 `RPT_INPUT_ERR` 上报 + `HCCL_ERROR` 打码 + 返回 `HCCL_E_PARA`/`HCCL_E_NOT_SUPPORT`」的模式，失败码再由调用处的 `CHK_RET` 向上传播。

`CheckDataType(dataType, needReduce)` 的分流逻辑是其核心：

```text
if (needReduce):                          // 归约类算子（AllReduce/ReduceScatter）
    黑名单含 UINT8/UINT16/UINT32/INT128/HIF8/FP8* → 不支持
else:                                     // 非归约类算子（AllGather/Broadcast）
    仅要求 INT8 ≤ dataType < RESERVED 且 ≠ INT128 → FP8 等也支持
```

#### 4.3.3 源码精读

**`HcomCheck*` 族**（跨算子）——以 `HcomCheckCount` 与 `HcomCheckDataType` 为例，注意它们用 `HCOM_ERROR_CODE` 编码、不带 `needReduce` 语义：

[src/common/param_check.cc:80-100](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.cc#L80-L100) 是 `HcomCheckCount`（超 `SYS_MAX_COUNT` 报 `HCCL_E_PARA`）与 `HcomCheckDataType`（查 `HCOM_DATA_TYPE_STR_MAP` 白名单，不支持报 `HCCL_E_NOT_SUPPORT`）。

[src/common/param_check.cc:113-142](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.cc#L113-L142) 是带 group/stream 重载的 `HcomCheckOpParam` 组合校验，演示「逐项 `RPT_INPUT_ERR` + `CHK_PRT_RET`」的两段式写法——每个字段（tag/count/dataType）都先上报结构化错误，再打日志返回。

**`Check*` 族**（每算子，带语义）——`CheckDataType(needReduce)` 是两套族差异的集中体现：

[src/ops/op_common/op_common.cc:2883-2918](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2883-L2918) 是 `CheckDataType`：`needReduce=true` 走黑名单（排除 FP8/UINT 等），`needReduce=false` 走范围判定（允许 FP8）。失败时用 `RPT_INPUT_ERR` 上报，并把 `GetSupportDataType(needReduce)` 作为「期望值」一并输出，便于用户自查。

[src/ops/op_common/op_common.cc:2945-2966](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2945-L2966) 是 `CheckReduceOp`：仅当 `op == HCCL_REDUCE_PROD` 时额外校验 `dataType` 是否在 PROD 支持清单内（PROD 不支持 INT16/BFP16 等）。

[src/ops/op_common/op_common.cc:2872-2881](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2872-L2881) 是 `CheckCount`（每算子版，与 `HcomCheckCount` 几乎等价，但归属 `op_common`，便于和 `CheckDataType`/`CheckReduceOp` 成组调用）。

**声明对照**——`HcomCheck*` 在 `param_check.h` 集中声明：

[src/common/param_check.h:18-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/param_check.h#L18-L37) 列出全部 `HcomCheck*` 声明（注意它们带多个重载，如 `HcomCheckOpParam` 有 3 参/4 参/5 参版本，适配不同算子的入参集合）。

#### 4.3.4 代码实践

1. **实践目标**：验证 `needReduce` 形参如何让同一套数据类型校验对 AllReduce 与 AllGather 表现不同。
2. **操作步骤**：
   - 阅读 [src/ops/op_common/op_common.cc:2883-2918](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2883-L2918)，记录 `needReduce=true` 的黑名单与 `needReduce=false` 的范围判定。
   - 在 [src/ops/all_reduce/all_reduce_op.cc:129](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L129) 看到 AllReduce 调 `CheckDataType(dataType, true)`；对照 AllGather（`src/ops/all_gather/all_gather_op.cc`）会调 `CheckDataType(dataType, false)`。
3. **需要观察的现象**：用 `HCCL_DATA_TYPE_FP8E4M3` 调 AllReduce 会因 `needReduce=true` 命中黑名单返回 `HCCL_E_NOT_SUPPORT`；同样的类型调 AllGather 则通过。
4. **预期结果**：AllReduce 报 `data type[HCCL_DATA_TYPE_FP8E4M3] not supported`，AllGather 正常执行。
5. **待本地验证**：需上板运行两个算子样例复现；无环境时可做源码阅读型实践——对照 `GetSupportDataType(true)` 与 `GetSupportDataType(false)` 两个清单（[op_common.cc:2920-2943](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2920-L2943)），手列各自支持的数据类型集合。

#### 4.3.5 小练习与答案

**练习 1**：既然 `CheckCount` 与 `HcomCheckCount` 做的事几乎一样，为什么还要保留两套？

> **答案**：历史与职责分层。`HcomCheck*` 是 `common` 层最通用的工具，任何模块（含非算子代码）都能复用；`Check*` 归在 `op_common`，是为算子入口「成组配套」准备的（和 `CheckDataType(needReduce)`/`CheckReduceOp` 放在一起，调用处可一眼看清这个算子做了哪些校验）。两者并存是渐进式重构的产物，新代码倾向用 `Check*`，老代码与跨模块复用仍用 `HcomCheck*`。

**练习 2**：`CheckReduceOp(dataType, op)` 在什么情况下才会真正去校验 `dataType`？

> **答案**：仅当 `op == HCCL_REDUCE_PROD` 时（见 [op_common.cc:2951](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2951)）。因为 SUM/MIN/MAX 对数据类型几乎没有额外限制，而 PROD（连乘）在部分低精度/整数类型上未实现，需要单独限定支持清单（INT8/INT32/INT64/UINT64/FP16/FP32/FP64）。

---

### 4.4 适配层：SAL 字符串工具与 ACL 包装

#### 4.4.1 概念说明

`src/common/` 还提供两个「适配层」，把外部世界的接口翻译成 HCCL 内部统一的风格：

- **SAL（字符串/数值工具 + 弱符号别名）**：`sal.{h,cc}` 提供安全的字符串↔数值转换（`SalStrToULong`/`SalStrToDouble`/`IsAllDigit`），以及一个关键宏 `weak_alias`——它用 GCC `__attribute__((weak, alias))` 给一个符号起「别名」，使 HCCL 既能自己提供默认实现，又允许 HCOMM 或测试桩用强符号覆盖。这是 u4-l2 里 `HcclGetDeviceType ↔ __HcclGetDeviceType` 弱符号覆盖、以及两仓解耦的底层机制之一。
- **adapter_acl（ACL 运行时适配）**：CANN 运行时提供大量 `aclrt*` 接口（内存拷贝、设备信息查询、拓扑查询等），它们返回 `aclError`、用 ACL 自己的错误码体系。HCCL 不希望算子代码直接调 `aclrt*`，而是统一调 `haclrt*` 包装函数——这些包装把 `aclError` 翻译成 `HcclResult`、补上 `HCCL_ERROR` 日志、并用 `#ifndef AICPU_COMPILE` 保证同一份源码在 host 侧与 AICPU 侧都能编译。

#### 4.4.2 核心流程

**`ACLCHECK(cmd)` 是 ACL 世界的 `CHK_RET`**：

```text
aclError ret = cmd;                 // 执行 ACL 调用
if (ret != ACL_SUCCESS) {
    HCCL_ERROR("acl interface return err %s:%d, retcode: %d", __FILE__, __LINE__, ret);
    if (ret == ACL_ERROR_RT_MEMORY_ALLOCATION) HCCL_ERROR("memory allocation error ...");
    return HCCL_E_RUNTIME;          // 翻译成 HCCL 错误码
}
```

**`haclrt*` 包装函数的统一骨架**（以 `haclrtMemcpy` 为例）：

```text
#ifndef AICPU_COMPILE            // host 侧才真正执行；AICPU 侧编译为空壳直接 SUCCESS
    CHK_PTR_NULL(dst); CHK_PTR_NULL(src);     // 复用 4.2 的指针校验
    ... 调 aclrtMemcpy，失败 → HCCL_ERROR + return HCCL_E_RUNTIME
#endif
return HCCL_SUCCESS;
```

**`weak_alias` 机制**：

```text
#define weak_alias(name, aliasname) \
    extern __typeof(name) aliasname __attribute__((weak, alias(#name)));
// 给 name 起一个弱别名 aliasname：默认指向 name，但可被强符号覆盖
```

#### 4.4.3 源码精读

**`ACLCHECK` 宏——ACL 调用的统一校验**：

[src/common/adapter_acl.h:22-32](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_acl.h#L22-L32) 是 `ACLCHECK`：把 `aclError != ACL_SUCCESS` 翻译为 `HCCL_E_RUNTIME` 并打日志，对内存分配失败额外提示。`AllReduceEntryLog` 里获取 deviceId/streamId 就用它（[all_reduce_op.cc:242-244](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L242-L244)）。

**`haclrtGetPairDeviceLinkType`——拓扑查询包装**，演示 `CHK_RET` + `ACLCHECK` + `HcclGetDeviceType` 的混用：

[src/common/adapter_acl.cc:42-74](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_acl.cc#L42-L74) 把 ACL 的 `aclrtGetDevicesTopo` 原始返回值翻译成 HCCL 的 `LinkTypeInServer` 枚举（HCCS/HCCS_SW/SIO/PXI），是拓扑适配（u3-l3）获取物理链路类型的数据源之一。注意整体被 `#ifndef AICPU_COMPILE` 包裹。

**`haclrtMemcpy`——内存拷贝包装**，是「包装函数 + `AICPU_COMPILE` 守卫 + 指针校验 + 图捕获模式切换」最完整的样例：

[src/common/adapter_acl.cc:150-208](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_acl.cc#L150-L208) 先 `CHK_PTR_NULL` 校验、`count==0` 早退，再切换 `aclmdlRICaptureThreadExchangeMode`（图捕获模式下的线程模式切换），调 `aclrtMemcpy`，失败翻译为 `HCCL_E_RUNTIME`。

**`LoadBinaryFromFile`——二进制加载包装**，演示 `realpath` 安全校验与 `CHK_PRT_RET` 自定义返回码：

[src/common/adapter_acl.cc:124-148](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/adapter_acl.cc#L124-L148) 用 `realpath` 防路径注入，加载失败返回 `HCCL_E_OPEN_FILE_FAILURE`。

**SAL 字符串工具——安全转换**，所有失败都打 `[Transform]` 日志并返回 `HCCL_E_PARA`：

[src/common/sal.cc:45-66](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/sal.cc#L45-L66) 是 `SalStrToULong`：用 `std::stoull` 配合 `try/catch` 捕获 `invalid_argument`/`out_of_range`，再额外判 `> INVALID_UINT` 防溢出。

[src/common/sal.cc:68-88](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/sal.cc#L68-L88) 是 `IsAllDigit`：先 `CHK_PTR_NULL`，再逐字符 `isdigit`（允许首位 `-`），用于校验环境变量值是否为整数。

**`weak_alias` 宏——弱符号别名**：

[src/common/sal.h:30-31](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/sal.h#L30-L31) 定义 `weak_alias`，用 `__attribute__((weak, alias(#name)))` 为 `name` 创建可被强符号覆盖的别名 `aliasname`。环境变量解析（u4-l3）与设备类型探测（u4-l2）里的弱符号覆盖都依赖这一机制。

**入口处综合样例**——`HcclAllReduce` 把本讲四类工具都用上了：

[src/ops/all_reduce/all_reduce_op.cc:23-52](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L23-L52) 是 `HcclAllReduce` 入口：第 27 行 `HCCL_INFO`（日志）、第 33/41/44/47/49 行 `CHK_RET`/`CHK_RET_AND_PRINT_IDE`（返回值校验）、第 37 行 `CHK_PRT_RET`（自定义码校验）。其中 `AllReduceInitAndCheck`（[all_reduce_op.cc:107-133](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L107-L133)）内部依次 `CHK_RET(InitEnvConfig())` → `CheckAllReduceInputPara`（含 `RPT_INPUT_ERR` 上报）→ `CheckCount`/`CheckDataType(true)`/`CheckReduceOp`（每算子校验族）。

#### 4.4.4 代码实践

1. **实践目标**：体会「`haclrt*` 包装 = 翻译 + 守卫 + 校验」三件事，理解为何算子代码不直接调 `aclrt*`。
2. **操作步骤**：
   - 阅读 [src/common/adapter_acl.cc:150-208](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc#L150-L208) 的 `haclrtMemcpy`，列出它相对直接调 `aclrtMemcpy` 多做的三件事：①指针校验与 `count==0` 早退；②`aclmdlRICaptureThreadExchangeMode` 图捕获模式切换；③`aclError→HcclResult` 翻译与日志。
   - 全局搜索 `aclrtMemcpy(`（注意带左括号）与 `haclrtMemcpy(`，对比调用频次：HCCL 业务代码几乎只调 `haclrt*` 包装，原始 `aclrt*` 被隔离在 `adapter_acl.cc` 内。
3. **需要观察的现象**：算子/template 代码里出现的是 `haclrtMemcpy`，而非 `aclrtMemcpy`；所有 `aclrt*` 调用都收敛在 `adapter_acl.cc`。
4. **预期结果**：确认「ACL 适配层」是一堵墙——外部 ACL 接口只能经 `haclrt*`/`ACLCHECK` 进入 HCCL，便于统一错误码、日志与编译守卫。
5. **待本地验证**：可用 `grep -rn "aclrtMemcpy(" src/`（排除 `adapter_acl.cc`）确认业务代码无直接调用；这是纯源码阅读型实践，无需 NPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `adapter_acl.cc` 里几乎每个函数都被 `#ifndef AICPU_COMPILE` 包起来？

> **答案**：HCCL 的同一份源码会被编译成两种产物：host 侧（运行在 CPU 上、可调 `aclrt*`）与 AICPU 侧（运行在 NPU 的 AICPU 上、不能调这些 host 运行时接口）。`#ifndef AICPU_COMPILE` 让这些包装函数在 AICPU 编译时退化为「直接 `return HCCL_SUCCESS`」的空壳，保证一份源码两种目标都能编过（对应 u1-l4 讲的同一份源码多形态构建）。

**练习 2**：`weak_alias(name, aliasname)` 创建的「弱别名」与 4.2 里 `RptInputErr` 的「弱符号」有何异同？

> **答案**：两者都用 `__attribute__((weak))`。区别在于：`RptInputErr` 是一个**定义可能缺失**的弱符号（没人提供就为 `nullptr`，调用前需判空）；`weak_alias` 创建的是一个**有默认实现**的弱别名——它默认指向 `name`，但 HCOMM 或测试桩可以用一个同名强符号覆盖它（如用强符号 `HcclGetDeviceType` 覆盖弱别名 `__HcclGetDeviceType`）。前者解决「可选依赖」，后者解决「可覆盖的默认实现」。

---

## 5. 综合实践

把本讲四类横切工具串起来，完成规格里的综合任务：

**任务**：在 [src/ops/all_reduce/all_reduce_op.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.cc) 中，收集 `HcclAllReduce` 与 `AllReduceInitAndCheck`、`CheckAllReduceInputPara` 里所有 `HCCL_INFO`/`CHK_RET`/`CHK_PTR_NULL`/`CHK_PRT_RET`/`RPT_INPUT_ERR` 调用，按下表分类，然后为一个**假想的新算子入口** `HcclFoo`（FP32、需要归约、单 stream）补充一组同风格的日志与入参校验代码。

**第一步：分类收集**（参考答案）：

| 调用点（all_reduce_op.cc） | 类别 | 工具 |
| --- | --- | --- |
| `HcclAllReduce` 第 27 行 `HCCL_INFO("Start to run execute HcclAllReduce")` | 日志 | `HCCL_INFO`（4.1） |
| 第 33 行 `CHK_RET(IsOutPlaceDevice(isOutPlace))` | 返回值校验 | `CHK_RET`（4.2） |
| 第 37 行 `CHK_PRT_RET(count == 0, HCCL_WARNING(...), HCCL_SUCCESS)` | 自定义码校验（早退） | `CHK_PRT_RET`（4.2） |
| 第 41/44/49 行 `CHK_RET(AllReduceInitAndCheck(...))` 等 | 返回值校验 | `CHK_RET`（4.2） |
| 第 47 行 `CHK_RET_AND_PRINT_IDE(AllReduceOutPlace(...), param.tag)` | 带标识的返回值校验 | `CHK_RET_AND_PRINT_IDE`（4.2） |
| `AllReduceInitAndCheck` 第 112/115/117/... 行一串 `CHK_RET(...)` | 返回值校验 | `CHK_RET`（4.2） |
| `CheckAllReduceInputPara` 第 139-141 行 `RPT_INPUT_ERR(stream == nullptr, "EI0003", ...)` | 错误码上报 | `RPT_INPUT_ERR`（4.2） |
| `CheckAllReduceInputPara` 第 142 行 `CHK_PTR_NULL(stream)` | 空指针校验 | `CHK_PTR_NULL`（4.2） |
| 第 128-130 行 `CheckCount`/`CheckDataType(true)`/`CheckReduceOp` | 入参业务校验 | `Check*` 族（4.3） |

可以看到：**日志**用于「正常流程留痕」，**返回值校验（`CHK_*`）**用于「把下层失败向上传播」，**错误码上报（`RPT_INPUT_ERR`）**用于「把用户入参错误结构化上报」，**业务校验（`Check*`）**封装「这个算子支持哪些入参」。

**第二步：为假想算子 `HcclFoo` 补代码**（示例代码，非项目原有代码）：

```cpp
// 示例代码：仿照 HcclAllReduce 的风格，为假想算子 HcclFoo 写入口
HcclResult HcclFoo(
    void* sendBuf, void* recvBuf, uint64_t count, HcclDataType dataType,
    HcclReduceOp op, HcclComm comm, aclrtStream stream)
{
    HCCL_INFO("Start to run execute HcclFoo");                 // 日志：入口留痕

    OpParam param;
    CHK_RET(InitEnvConfig());                                   // 返回值校验：环境变量

    // 错误码上报 + 空指针校验：两段式，逐个入参
    RPT_INPUT_ERR(stream == nullptr, "EI0003",
        std::vector<std::string>({"ccl_op", "value", "parameter", "expect"}),
        std::vector<std::string>({"HcclFoo", "nullptr", "stream", "non-null pointer"}));
    CHK_PTR_NULL(stream);
    CHK_PTR_NULL(sendBuf);
    CHK_PTR_NULL(recvBuf);

    // 业务校验：count/数据类型（Foo 需要归约，故 needReduce=true）/归约算子
    CHK_RET(CheckCount(count));
    CHK_RET(CheckDataType(dataType, /*needReduce=*/true));
    CHK_RET(CheckReduceOp(dataType, op));

    CHK_RET(AllReduceEntryLog(sendBuf, recvBuf, count, dataType, op, stream, param.tag, "HcclFoo"));
    // ... 后续 Selector/HcclExecOp 链路（本讲不展开）
    return HCCL_SUCCESS;
}
```

**自检要点**：
1. 入口第一行是否为 `HCCL_INFO` 留痕？
2. 每个用户传入的指针/句柄是否都有 `RPT_INPUT_ERR` + `CHK_PTR_NULL` 两段式保护？
3. 业务参数是否用 `Check*` 族（而非散落的 `if`）？
4. 所有可失败调用是否都用 `CHK_RET` 包裹、错误能向上传播到 `HcclFoo` 的返回值？

> 说明：上述 `HcclFoo` 仅为演示横切工具用法而构造，仓库中并不存在；`InitEnvConfig`/`AllReduceEntryLog`/`Check*` 均为真实函数，可按本讲给出的链接核对签名。

## 6. 本讲小结

- HCCL 日志系统是「宏 + 级别缓存 + ErrToWarn」三层：`HCCL_INFO/ERROR` 等宏先经 `HcclCheckLogLevel` 判级别（命中 `atomic` 缓存），运行日志（`HCCL_RUN_INFO`，带 `RUN_LOG_MASK`）绕过缓存永远输出；`config_log` 在其上叠加「位掩码分类调试」，无需抬高全局级别即可单独打开 ALG/TASK/RESOURCE 详尽日志。
- 横切工具分四类、各司其职：**日志**（`HCCL_*`）、**返回值校验**（`CHK_RET`/`CHK_PTR_NULL`/`CHK_PRT_RET`/`CHK_RET_AND_PRINT_IDE`，定义在 `log.h`）、**错误码上报**（`RPT_INPUT_ERR`/`RPT_ENV_ERR` + `HCOM_ERROR_CODE`，弱符号委托平台错误管理）、**业务校验**（`Check*`/`HcomCheck*` 族）。
- 校验函数两套并存：`common/param_check` 的 `HcomCheck*` 跨算子正交复用、不带算子语义；`op_common` 的 `Check*` 带归约语义（`CheckDataType(needReduce)` 决定 AllReduce 不支持 FP8 而 AllGather 支持），并配 `RPT_INPUT_ERR` 给出「期望值」说明。
- 适配层把外部世界翻译成 HCCL 风格：`sal` 提供安全字符串转换与 `weak_alias` 弱符号别名机制；`adapter_acl` 用 `ACLCHECK` 与 `haclrt*` 包装把 `aclError`/`aclrt*` 收敛进 HCCL 的 `HcclResult`/日志世界，并用 `#ifndef AICPU_COMPILE` 保证一份源码 host/AICPU 双形态编译。
- 这些工具被几乎所有算子统一复用，是阅读任何算子入口（如 `HcclAllReduce`）前必须先认熟的公共词汇表；算子入口的典型写法是「`HCCL_INFO` 留痕 → `RPT_INPUT_ERR`+`CHK_PTR_NULL` 两段式指针校验 → `Check*` 业务校验 → `CHK_RET` 串联后续链路」。

## 7. 下一步学习建议

- **向「使用方」延伸**：回到 [u2-l2 单算子入口与兼容分发](u2-l2-op-entry-dispatch.md) 与 [u2-l3 OpParam 参数结构与入参校验](u2-l3-opparam-and-check.md)，用本讲的工具视角重新读一遍 `HcclAllReduce`，体会「校验顺序 = 廉价优先」是如何由 `CHK_*`/`Check*` 串起来的。
- **向「跨仓解耦」延伸**：本讲的 `weak_alias` 与 `RPT_*` 弱符号是「弱符号解耦」的入门；[u6-l1 dlsym 动态加载机制](u6-l1-dlsym-mechanism.md) 会讲更强的 `dlsym` 解耦（`DEFINE_WEAK_FUNC`/`INIT_SUPPORT_FLAG`），可对比阅读。
- **向「测试」延伸**：这些校验与日志工具如何在测试里被复用、被桩函数覆盖，见 [u7-l4 测试体系——UT 与 ST](u7-l4-testing.md)；UT 里常用强符号覆盖弱符号（如覆盖 `HcclGetDeviceType`）来构造不同设备类型的测试场景。
- **源码精读建议**：通读一遍 [src/common/log.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/log.h) 的全部 `CHK_*` 宏，再带着这份「词汇表」去读任意一个算子的 `_op.cc`，会发现入口代码的「骨架」高度一致。
