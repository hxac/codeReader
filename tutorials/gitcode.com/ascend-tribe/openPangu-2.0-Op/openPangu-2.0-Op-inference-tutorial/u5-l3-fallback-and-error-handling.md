# u5-l3 Fallback 回退与统一错误处理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「回退（fallback）」在昇腾算子执行语境下的含义：算子在图引擎中除了 AICore kernel 路径之外，还可以注册一条 **host 侧执行路径**，这条路径复用 aclnn 两段式接口完成计算。
2. 对比 `fallback.h`（一阶段：`EXEC_OPAPI_CMD`）与 `fallback_2stages.h`（两阶段：`EXEC_OPAPI_PREPARE_CMD`）两种回退策略的差异，并知道仓库当前只有一阶段被真实算子使用。
3. 掌握 `ops_err.h` / `ops_error.h` 中错误上报宏（`OPS_REPORT_VECTOR_INNER_ERR`、`OPS_REPORT_CUBE_INNER_ERR`、`OPS_ERR_IF`）的统一用法，理解 `E89999` / `E69999` / `EZ9999` 三个错误码的分工。
4. 区分 op_api 层（`OP_LOGE` + `CHECK_RET`）、op_host tiling 层（`OP_CHECK_IF` + `OPS_REPORT_*`）与 op_kernel 层（无日志）三套报错手段的差异与配合，理解 host 侧与设备侧（tiling 下沉）的日志分级切换。

## 2. 前置知识

- **回退（fallback）**：在第 3 单元（u3-l4）我们看到，eager 模式下 torch 调用经 csrc → `EXEC_NPU_CMD_V1` → aclnn 接口下发算子。而在**图模式**下，算子由图引擎调度执行。图引擎执行算子时通常跑 AICore kernel，但也允许算子注册一个 **host 侧执行函数**（`OpExecuteFunc`）：当框架决定以 host 路径执行该算子时，就调用这个函数。本讲的 fallback 框架就是实现这类 host 执行函数的「脚手架」——它的核心技巧是：**不在 host 侧重写计算逻辑，而是把图引擎传入的 `gert::Tensor` 转换成 acl 描述符，再借用 aclnn 两段式接口下发**，一份计算实现两处复用。
- **`gert::Tensor` 与 `aclTensor`**：前者是图引擎（exe_graph 运行时）的张量描述，后者是 aclnn 接口的张量描述。fallback 框架的 `ConvertType` 系列负责二者桥接（与 u3-l2 讲过的 torch_ops_extension 侧桥接同构，但方向不同：那边是 `at::Tensor` → `aclTensor`，这边是 `gert::Tensor` → `aclTensor`）。
- **错误码与日志的关系（DFX）**：DFX 指 Design for X（可测试性、可维护性、可观测性等质量属性）。算子出错时，光有返回值不够，还要留下可检索的日志与错误码。昇腾的日志设施（slog）把「打日志」与「上报错误码」合并成一个宏调用：`OPS_LOG_E` 在输出 ERROR 日志的同时附带错误码上报，而 `OPS_LOG_E_WITHOUT_REPORT` 只打日志不上报。
- **GNU 语句表达式**：`({ ...; ret; })` 这种「花括号包裹、末尾表达式作为整个语句的值」的 GCC 扩展语法。`EXEC_OPAPI_CMD` 宏整体就是一个语句表达式，可以直接写在 `apiRet = EXEC_OPAPI_CMD(...)` 的右边。
- **同名宏陷阱**：`OP_LOGE` 在 CANN 的两套头文件里都存在，但**第一个参数含义不同**——op_api 层（`opdev/op_log.h`）传错误码（如 `ACLNN_ERR_PARAM_NULLPTR`），op_host tiling 层传算子节点名（字符串）。本讲 4.3 节会专门对比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ascendc/src/ops-transformer/common/include/fallback/fallback.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h) | 一阶段回退框架：aclnn 符号查找链、`gert::Tensor`→acl 描述符转换、`EXEC_OPAPI_CMD` 总装宏 |
| [ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h) | 两阶段回退框架：`EXEC_OPAPI_PREPARE_CMD` 只做「准备」，执行段交由框架统一 launch |
| [ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h) | 两阶段的公共数据结构：`OpApiAnyValue`（带删除器的参数）、`OpApiParams`、`ExecuteOpLaunch` 声明 |
| [ascendc/src/ops-transformer/common/include/fallback/fallback_comm.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_comm.h) | 公共声明：引入 exe_graph 运行时上下文与 `ToAclDataType` |
| [ascendc/src/ops-transformer/common/include/err/ops_err.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/err/ops_err.h) | ops-transformer 侧错误上报宏：`E89999` / `E69999` 错误码与 `OPS_ERR_IF` |
| [ascendc/src/utils/inc/error/ops_error.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/error/ops_error.h) | utils 侧镜像文件，宏内容与 ops_err.h 相同，供不同 include 路径使用 |
| [ascendc/src/utils/inc/log/ops_log.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h) | 日志宏家族：D/I/W/E 分级、全量日志、条件日志、host 与设备（tiling 下沉）双实现 |
| [ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp) | 一阶段回退的最小真实样本（本讲精读） |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp) | 旗舰算子的回退样本：45 个参数的总装调用 |
| [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp) | op_api 层错误处理范本（承接 u2-l2） |
| [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp) | tiling 层错误处理范本：`OP_CHECK_IF` 与 `OPS_REPORT_CUBE_INNER_ERR` |
| [ascendc/cmake/device_task.cmake](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/device_task.cmake) | 定义 `DEVICE_OP_TILING_LIB`，把 ops_log.h 切到设备侧日志实现 |
| [ascendc/src/ops-nn/common/inc/error_util.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/inc/error_util.h) | ops-nn 侧的错误工具（对照阅读） |

## 4. 核心概念与源码讲解

本讲四个最小模块：**一阶段回退**、**两阶段回退**、**错误码规范**、**日志体系**。

### 4.1 回退机制（一）：一阶段框架 `EXEC_OPAPI_CMD`

#### 4.1.1 概念说明

一个算子被图引擎调度时，默认走 AICore kernel 路径（u2-l4 讲过的 `__global__ __aicore__` 入口）。但有一类算子存在一个现实问题：**它们的计算逻辑已经在 aclnn 接口（L0 算子）里完整实现过了**。如果图引擎某些场景需要 host 侧执行，再写一份 host 计算就是重复劳动。

fallback 框架的解法是「借道」：

- 图引擎提供一个注册接口 `IMPL_OP(算子名).OpExecuteFunc(host函数).HostInputs({...})`，允许算子登记一个 host 侧执行函数；
- 该函数从执行上下文 `OpExecuteContext` 取出 `gert::Tensor` 与属性，转换成 acl 描述符；
- 通过运行期 `dlopen/dlsym` 找到同名 aclnn 接口，按两段式（GetWorkspaceSize → 执行）下发，**让同一份 L0 实现同时服务 eager 与图两条路径**。

`HostInputs` 声明「哪些输入张量的**数据**需要被 host 函数读取」——这一点从源码可以实证（见 4.1.3）。

至于**何时触发回退**（哪些场景图引擎会选择 host 路径而非 kernel 路径），这是 CANN 图运行时的调度行为，仓库内没有显式开关，标注「待确认」；仓库侧的职责只是把 host 执行函数注册好。

#### 4.1.2 核心流程

一阶段 `EXEC_OPAPI_CMD` 的执行流程（在**一次函数调用内一口气完成**准备与执行）：

```text
EXEC_OPAPI_CMD(aclnnXxx, args...)
  ├─ 1. 静态查符号：ResetCacheThreadLocal / aclnnXxxGetWorkspaceSize / aclnnXxx
  │      查找顺序（GetOpApiFuncAddr）：
  │      a. ASCEND_CUSTOM_OPP_PATH 各路径下的 libcust_opapi.so
  │      b. ASCEND_OPP_PATH/vendors/config.ini 中 load_priority 各厂商的 libcust_opapi.so
  │      c. CANN 内置库集合（libaclnn_ops_infer.so 等 6 个）
  ├─ 2. ResetCacheThreadLocal()          —— 清理线程本地缓存
  ├─ 3. ConvertTypes(args, &workspaceSize, &executor) —— 参数打包成 tuple
  ├─ 4. call(GetWorkspaceSize函数)        —— 同步执行第一段
  ├─ 5. workspaceSize > 0 时 MallocWorkspace
  └─ 6. 闭包内：opApiFunc(workspace, size, executor, stream) —— 第二段下发
         随后 ReleaseConvertTypes(...) 释放描述符、FreeWorkspace
```

#### 4.1.3 源码精读

**符号查找链**——先环境变量，再 vendors 配置，最后 CANN 内置库，任何一级命中即返回：

- [fallback.h:L227-L248](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L227-L248)：`GetOpApiFuncAddr` 先遍历 `ASCEND_CUSTOM_OPP_PATH` 指定的自定义算子库目录，逐个尝试 `libcust_opapi.so`。
- [fallback.h:L249-L269](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L249-L269)：找不到再读 `ASCEND_OPP_PATH/vendors/config.ini` 的 `load_priority` 厂商列表，仍找不到则交给 `GetAclnnArrdByApiName` 去 CANN 内置库兜底。
- [fallback.h:L210-L225](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L210-L225)：`GetAclnnArrdByApiName` 按顺序轮询 `libaclnn_ops_infer.so`、`libaclnn_ops_train.so` 等 6 个内置库。

这套顺序与 u3-l2 讲过的 torch_ops_extension 侧查找链同构：**自定义 vendors 永远优先于 CANN 内置库**，保证本仓库的算子实现压过同名内置实现。

**总装宏**——只保留关键骨架（原始宏约 70 行）：

```cpp
// 节选自 fallback.h EXEC_OPAPI_CMD（示例代码，有删节）
#define EXEC_OPAPI_CMD(aclnn_api, ...)                                  \
  ({                                                                    \
    static auto ret = GRAPH_SUCCESS;                                    \
    do {                                                                \
      /* 1. 静态查符号，找不到直接 GRAPH_FAILED */                         \
      static const auto getWorkspaceSizeFuncAddr =                      \
          GetOpApiFuncAddr(#aclnn_api "GetWorkspaceSize");              \
      static const auto opApiFuncAddr = GetOpApiFuncAddr(#aclnn_api);   \
      if (getWorkspaceSizeFuncAddr == nullptr || opApiFuncAddr == nullptr) { \
        OP_LOGE("aclnnfallback", "... not found.");                     \
        ret = GRAPH_FAILED;                                             \
        break;                                                          \
      }                                                                 \
      /* 2. 参数打包：业务参数 + workspaceSize 输出 + executor 输出 */      \
      uint64_t workspace_size = 0;                                      \
      aclOpExecutor *executor = nullptr;                                \
      auto converted_params =                                           \
          ConvertTypes(__VA_ARGS__, &workspace_size, &executor);        \
      /* 3. 第一段：同步执行 GetWorkspaceSize */                          \
      auto workspace_status = call(getWorkspaceSizeFunc, converted_params); \
      if (workspace_status != 0) { ret = GRAPH_FAILED; break; }         \
      /* 4. 按需分配 workspace */                                        \
      void *workspace_addr = nullptr;                                   \
      if (workspace_size > 0) {                                         \
        workspace_addr = host_api_ctx->MallocWorkspace(workspace_size); \
      }                                                                 \
      /* 5. 闭包内执行第二段，随后统一释放 */                               \
      auto acl_stream = host_api_ctx->GetStream();                      \
      auto acl_call = [...]() -> int {                                  \
        opApiFunc(workspace_addr, workspace_size, executor, acl_stream); \
        ReleaseConvertTypes(converted_params);   // 描述符销毁必须晚于执行段 \
        host_api_ctx->FreeWorkspace();                                 \
        return ...;                                                     \
      };                                                                \
      ret = acl_call();                                                 \
    } while (false);                                                    \
    (ret);   /* 语句表达式的值 */                                         \
  })
```

对应原文 [fallback.h:L599-L667](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L599-L667)。三个易错点：

1. `static const auto xxxFuncAddr` 只在**首次**执行时查符号（static 局部变量只初始化一次），后续调用零查找开销；
2. `ReleaseConvertTypes` 在执行段**之后**调用（[fallback.h:L653](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L653)）——aclnn 第二段虽然已下发，但 executor 内部仍持有这些描述符的指针，过早销毁会造成悬垂（与 u3-l2 的结论一致）；
3. 整个宏是 `({...})` 语句表达式，末尾 `(ret)` 就是赋给 `apiRet` 的值。

**真实算子接入**——仓库共 3 个算子使用一阶段框架，全部在 attention 族：

| 算子 | 回退文件 | 注册点 | `EXEC_OPAPI_CMD` 调用 |
| --- | --- | --- | --- |
| AiInfraEsaSelectTopk | fallback_ai_infra_esa_select_topk.cpp | [L193-L195](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L193-L195) | [L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L176) |
| AiInfraSparseFlashAttentionGqa | fallback_ai_infra_sparse_flash_attention_gqa.cpp | [L338-L340](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/fallback_ai_infra_sparse_flash_attention_gqa.cpp#L338-L340) | [L312](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/fallback_ai_infra_sparse_flash_attention_gqa.cpp#L312) |
| AiInfraFusedInferAttentionSink | fallback_ai_infra_fused_infer_attention_sink.cpp | [L362-L367](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L362-L367) | [L308](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L308) |

以最小样本 esa_select_topk 为例看 host 执行函数的固定套路：

- [fallback_ai_infra_esa_select_topk.cpp:L155-L172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L155-L172)：`AiInfraEsaSelectTopkHostExecuteFunc` 依次取输入张量、输出张量、属性、可选输入，每一步用 `OPS_CHECK(cond, OPS_LOG_E(...), return GRAPH_FAILED)` 守卫（`OPS_CHECK` 定义见 4.4.3）。
- [fallback_ai_infra_esa_select_topk.cpp:L128-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L128-L153)：`EsaFillActualSeqInfo` 用 `GetData<int64_t>()` **直接读取三个 actual_seq 张量的数值**，拷进 `std::vector<int64_t>`，再作为 `std::vector` 参数传给 aclnn 接口（由 [fallback.h:L277-L285](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L277-L285) 的 `ConvertType(const std::vector<int64_t>&)` 重载转成 `aclIntArray*`）。
- [fallback_ai_infra_esa_select_topk.cpp:L193-L195](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L193-L195)：注册——`HostInputs({ACTUAL_Q_LEN_INDEX, ACTUAL_K_LEN_INDEX, ACTUAL_CMPK_LEN_INDEX})` 声明的三个索引，**恰好就是** `EsaFillActualSeqInfo` 读数据的三个张量。这就是 `HostInputs` 的语义：告诉框架哪些输入的数据要落到 host 可读内存。gqa 版注册了 3 个 HostInputs（[fallback_ai_infra_sparse_flash_attention_gqa.cpp:L338-L340](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/fallback_ai_infra_sparse_flash_attention_gqa.cpp#L338-L340)），其 L162/L170/L179 三处 `GetData<int64_t>()` 一一对应；FIA Sink 版注册 2 个（[fallback_ai_infra_fused_infer_attention_sink.cpp:L362-L367](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L362-L367)）。

大算子样本可看 [fallback_ai_infra_fused_infer_attention_sink.cpp:L299-L357](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L299-L357)：host 函数里还做了一段**参数适配**——`sparseMode` 落在 `[10, 14]` 区间时减 10 并清零 `innerPrecise`，用 `OPS_LOG_I` 留痕（L302-L305），然后以 45 个参数总装调用 `EXEC_OPAPI_CMD`。

#### 4.1.4 代码实践

**实践 A：检索 fallback 的真实调用点（本讲主实践的前半，纯源码阅读，无需硬件）**

1. **实践目标**：亲手确认「哪些算子接入了 fallback、接入点在哪」，形成一张可复查的调用点清单。
2. **操作步骤**：
   ```bash
   cd inference/ascendc
   # 1) 找出所有 include fallback 头文件的源文件
   grep -rn "fallback/fallback" --include="*.cpp" --include="*.h" src/ | grep -v common/include
   # 2) 找出所有宏调用点
   grep -rn "EXEC_OPAPI_CMD\|EXEC_OPAPI_PREPARE_CMD" --include="*.cpp" src/
   # 3) 找出所有注册点
   grep -rn "OpExecuteFunc" --include="*.cpp" src/
   ```
3. **需要观察的现象**：第 1、2 条命令都只应命中 3 个 `fallback_*.cpp` 文件；`EXEC_OPAPI_CMD` 命中 3 处算子调用 + 1 处宏定义；第 3 条命中 3 处 `IMPL_OP(...).OpExecuteFunc(...)`。
4. **预期结果**：与 4.1.3 表格完全一致（esa_select_topk / sparse_flash_attention_gqa / fused_infer_attention_sink）。把每行的「文件:行号」抄成清单，这就是你要交付的调用点记录。
5. 本实践为静态检索，可直接完成，无需「待本地验证」标注。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EXEC_OPAPI_CMD` 里查 `aclnnXxx` 符号的 `static const auto` 变量不会因为第一次失败而「缓存失败」？

<details>参考答案：实际上**会**。`static` 局部变量只初始化一次，若首次 `GetOpApiFuncAddr` 返回 `nullptr`，后续调用都会跳过重新查找，直接走 `GRAPH_FAILED` 分支（因为 `if (... == nullptr)` 每次都会判断）。这隐含一个运维要求：如果运行途中才安装自定义算子包，进程内已失败的符号查找不会自愈，需要重启进程。</details>

**练习 2**：`HostInputs({2, 3, 4})` 里的数字是什么含义？如果删掉这个声明，esa_select_topk 的回退函数会发生什么？

<details>参考答案：数字是算子输入张量的**索引**（对应 OpDef 中 Input 的声明顺序，`ACTUAL_Q_LEN_INDEX = 2` 等，见 [fallback_ai_infra_esa_select_topk.cpp:L28-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L28-L37)）。删掉后框架不会把这三个张量的数据准备到 host 可读位置，`EsaFillActualSeqInfo` 里的 `GetData<int64_t>()` 将读到无效数据或空指针，导致 `aclIntArray` 内容错误甚至崩溃。</details>

### 4.2 回退机制（二）：两阶段框架 `EXEC_OPAPI_PREPARE_CMD`

#### 4.2.1 概念说明

一阶段框架把「准备（算 workspace、建 executor、转换参数）」和「执行（launch 下发）」塞在一个函数里同步完成。这对单算子调用没问题，但图引擎若想**统一调度一批算子的执行时机**（例如集中管理 workspace、流水化下发），就需要把两个阶段拆开：

- **Prepare 阶段**：host 函数只做参数转换与第一段 `GetWorkspaceSize`，把转换结果、executor、执行函数指针打包登记到执行上下文，然后返回；
- **Launch 阶段**：由框架在合适的时机统一驱动（两阶段框架预置了一个所有算子共用的 launch 函数 `ExecuteOpLaunch`）。

拆分的附带收益是**参数生命周期托管**：一阶段的 `converted_params` 释放责任在宏内部的闭包里；两阶段用「指针 + 删除器」把每个已转换参数登记进 `OpApiParams`，交给 `host_api_ctx->SetOpApiParamsWithDefaultDeleter` 托管，框架保证执行完毕后统一销毁——host 函数作者不再需要操心释放时机。

**仓库现状**：两阶段框架是**预置能力**——`EXEC_OPAPI_PREPARE_CMD` 在仓库内没有任何算子调用（见 4.2.4 实践的检索验证），三个算子全部走一阶段。

#### 4.2.2 核心流程

两种策略的差异对照：

| 维度 | 一阶段 `EXEC_OPAPI_CMD` | 两阶段 `EXEC_OPAPI_PREPARE_CMD` |
| --- | --- | --- |
| 宏所在文件 | fallback.h | fallback_2stages.h |
| 准备与执行 | 同一次调用内完成 | 只做 Prepare，Launch 交给框架 |
| workspace | 宏内 `MallocWorkspace` 立即分配 | 只 `SetWorkspaceSizes({size})` 申报尺寸 |
| 参数释放 | 闭包内手动 `ReleaseConvertTypes` | `OpApiParams` + 默认删除器托管 |
| 执行函数 | 宏内 lambda 直接调用 | 登记 `op_api_func` 指针，由 `ExecuteOpLaunch` 统一调用 |
| 仓库内使用 | 3 个算子 | 0 个（预置） |

两阶段 Prepare 的流程：

```text
EXEC_OPAPI_PREPARE_CMD(aclnnXxx, args...)
  ├─ 1. 静态查符号（同一阶段）
  ├─ 2. new OpApiParams；登记 op_api_func 执行指针
  ├─ 3. ConvertTypes(...) 打包参数
  ├─ 4. CollectConvertedTypes：为每个 ACL 描述符参数
  │      生成 {指针, 删除器} 登记进 op_api_params->converted_params
  ├─ 5. host_api_ctx->SetOpApiParamsWithDefaultDeleter(op_api_params)  ← 托管
  ├─ 6. call(GetWorkspaceSize函数)
  └─ 7. host_api_ctx->SetWorkspaceSizes({workspace_size})             ← 申报
```

#### 4.2.3 源码精读

- [fallback_comm_2stages.h:L34-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L34-L46)：`OpApiAnyValue` 是「指针 + 删除器」的对子；`OpApiParams` 聚合了 `converted_params`、`executor` 与 `op_api_func` 三样。注意头文件注释（L40-L41）明确说明：这个结构体**由算子仓定义、算子感知，GE 框架不感知**——框架只拿它当不透明数据，配合默认删除器兜底释放。
- [fallback_comm_2stages.h:L48-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L48-L49)：`ExecuteOpLaunch` 声明，注释说明它是**与算子类型无关的共用二阶段 launch 函数**——所有算子共享同一个二阶段注册入口。它在仓库内只有声明没有实现（实现由 CANN/GEE 运行时提供）。
- [fallback_2stages.h:L34-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L34-L40)：`Collect` 对 `aclTensor*` 的重载——把指针和对应的 `aclDestroyTensor` 包装成 lambda 删除器登记进 params 向量；标量等无需释放的类型由模板兜底重载登记 `{nullptr, nullptr}`（[fallback_2stages.h:L74-L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L74-L79)）。
- [fallback_2stages.h:L94-L130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L94-L130)：`EXEC_OPAPI_PREPARE_CMD` 全貌。与一阶段逐行对照着读：差异点全部集中在「登记而不是执行」——L107 `new OpApiParams`、L119 `SetOpApiParamsWithDefaultDeleter`（托管）、L127 `SetWorkspaceSizes({workspace_size})`（申报而非分配）。
- [fallback_2stages.h:L25-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L25-L26)：它同时 include 了 `fallback.h` 与 `fallback_comm_2stages.h`，说明两阶段是在一阶段的原语（`ConvertTypes`、`GetOpApiFuncAddr`、`call`）之上叠加的新编排，而不是替代品。

#### 4.2.4 代码实践

**实践 B：验证两阶段框架「预置未用」（纯源码阅读）**

1. **实践目标**：用检索证据确认 `EXEC_OPAPI_PREPARE_CMD` 与 `ExecuteOpLaunch` 在仓库内没有算子级调用点，理解「框架预置」与「实际接入」的区别。
2. **操作步骤**：
   ```bash
   cd inference/ascendc
   grep -rn "EXEC_OPAPI_PREPARE_CMD" src/          # 应只命中宏定义处
   grep -rn "ExecuteOpLaunch" src/                  # 应只命中声明处
   grep -rn "fallback_2stages" --include="*.cpp" src/   # 应零命中（无 .cpp 引用）
   ```
3. **需要观察的现象**：三条命令分别只命中 `fallback_2stages.h` 内部的 1 处定义、1 处声明、若干 include 链；没有任何 `op_host/*.cpp` 使用它们。
4. **预期结果**：确认两阶段框架是给后续演进而预留的公共能力。读完 `EXEC_OPAPI_PREPARE_CMD` 与 `EXEC_OPAPI_CMD` 后，在自己的笔记里写下一句话结论：「一阶段 = 同步总装，两阶段 = 登记托管」。
5. 静态检索可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：一阶段宏里 `ReleaseConvertTypes` 为什么必须放在闭包内、执行段之后？两阶段是如何消除这个手工时序要求的？

<details>参考答案：aclnn 第二段（`opApiFunc`）虽然返回了，但 `executor` 及下发队列仍引用这些 acl 描述符，提前销毁会悬垂；所以释放必须晚于执行段。两阶段把每个描述符连同删除器登记进 `OpApiParams`，交给 `SetOpApiParamsWithDefaultDeleter` 托管，由框架在真正 launch 完成后统一销毁，宏作者不再手写释放语句。</details>

**练习 2**：如果要把 fused_infer_attention_sink 从一阶段迁到两阶段，调用侧代码要改哪几处？

<details>参考答案：把 [fallback_ai_infra_fused_infer_attention_sink.cpp:L308](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L308) 的 `EXEC_OPAPI_CMD` 换成 `EXEC_OPAPI_PREPARE_CMD`，include 从 `fallback.h` 换/增为 `fallback_2stages.h`；参数列表不变（宏签名一致）；错误检查保持 `OPS_CHECK(apiRet != GRAPH_SUCCESS, ...)` 不变。此外还需依赖框架侧提供 `ExecuteOpLaunch` 的实现与注册入口——这正是仓库尚未接入的原因之一（框架联动部分仓库不可见，待确认）。</details>

### 4.3 错误码规范：三层返回值约定与统一上报宏

#### 4.3.1 概念说明

算子三层各有自己的返回值「货币」，错误处理的第一原则是**不要把货币搞混**：

| 层 | 返回值类型 | 成功 / 失败取值 | 报错手段 |
| --- | --- | --- | --- |
| op_api（aclnn） | `aclnnStatus` | `ACLNN_SUCCESS` / `ACLNN_ERR_*` | `OP_LOGE(错误码, fmt)` + `CHECK_RET` |
| op_host（tiling/infershape） | `ge::graphStatus` | `GRAPH_SUCCESS` / `GRAPH_FAILED`（注册表轮询还有第三态 `GRAPH_PARAM_INVALID`，见 u5-l1） | `OP_CHECK_IF` + `OP_LOGE(节点名, fmt)` + `OPS_REPORT_*_INNER_ERR` |
| fallback host 函数 | `graphStatus` | 同上 | `OPS_CHECK` + `OPS_LOG_E` |
| op_kernel | `void`（无返回值） | —— | **无日志宏**（设备核上没有日志基础设施） |

本仓库统一错误上报的「官方出口」是 `ops_err.h` 里两个宏：

- `OPS_REPORT_VECTOR_INNER_ERR` → 上报错误码 **`E89999`**（Vector 算子内部错误）；
- `OPS_REPORT_CUBE_INNER_ERR` → 上报错误码 **`E69999`**（Cube 算子内部错误）；
- 条件报错宏 `OPS_ERR_IF(COND, LOG_FUNC, EXPR)` = 条件成立时先执行日志函数、再执行善后表达式。

错误码由宏名首字母+数字构成（E8 对应 Vector、E6 对应 Cube 核相关的错误族，9999 是该族内的通用内部错误码——从宏命名即可读出这层对应关系）。它们最终交给 CANN 的 `OPS_INNER_ERR_STUB`（定义在 CANN 头文件 `log/inner/dfx_base.h`，不在本仓库内）完成「打 ERROR 日志 + 上报错误码」的联动。

还有一个仓库组织上的事实：`ops_err.h`（ops-transformer/common/include/err/）与 `ops_error.h`（src/utils/inc/error/）是**内容相同的双胞胎文件**，区别只在 include 路径与所包含的日志头不同（前者 include CANN 的 `log/log.h`，后者 include 仓库自己的 `log/ops_log.h`），分别服务于不同的编译目标——算子目录下的 tiling 代码用相对路径引前者（如 scatter tiling 的 `#include "../../../common/include/err/ops_err.h"`），utils 体系的目标（如 tiling sink）把 `utils/inc/error` 加进搜索路径后以 `#include "error/ops_error.h"` 引后者（fallback_ai_infra_esa_select_topk.h 就是这么引的，见 [fallback_ai_infra_esa_select_topk.h:L15-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.h#L15-L17)）。

#### 4.3.2 核心流程

一次「参数检查失败」在两层的标准传播路径：

```text
op_api 层：
  CheckXxx() 检查失败
    → OP_LOGE(ACLNN_ERR_PARAM_INVALID, "...")   ← 打日志（带 aclnn 错误码）
    → return false
  → CHECK_RET(false, ACLNN_ERR_PARAM_INVALID)   ← 翻译成 aclnnStatus 返回
  → 第一段 GetWorkspaceSize 返回非 0 → 调用方（csrc/fallback）感知失败

op_host tiling 层：
  DoOpTiling 内检查失败
    → OP_CHECK_IF(cond, OP_LOGE(nodeName, "..."), return ge::GRAPH_FAILED)  ← 打日志
    → 或 OPS_REPORT_CUBE_INNER_ERR(nodeName, "...")                          ← 打日志+E69999 上报
    → return ge::GRAPH_FAILED → 框架感知 tiling 失败，算子不下发
```

#### 4.3.3 源码精读

**宏定义本体**（两个文件内容一致，以 ops_err.h 为例）：

- [ops_err.h:L22-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/err/ops_err.h#L22-L27)：三个宏的全部定义——`OPS_REPORT_VECTOR_INNER_ERR` 固定上报 `"E89999"`，`OPS_REPORT_CUBE_INNER_ERR` 固定上报 `"E69999"`，`OPS_ERR_IF` 委托给 `OPS_LOG_STUB_IF`。对照镜像文件 [ops_error.h:L21-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/error/ops_error.h#L21-L26)，逐字符相同。
- 需要注意：`OPS_INNER_ERR_STUB` 与 `OPS_LOG_STUB_IF` 都是 CANN 提供的底层宏（随 `log/log.h` / `log/inner/dfx_base.h` 引入），仓库只做了一层「固定错误码 + 起个好记的名字」的封装。

**op_api 层范本**（承接 u2-l2 的三步检查，这里看错误处理细节）：

- [aclnn_ai_infra_scatter_block_update.cpp:L42-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L42-L57)：`NotNull` 检查——每个指针用 `OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "...")` 报错后 `return false`。注意这里 `OP_LOGE` 的**第一个参数是错误码**（来自 CANN `opdev/op_log.h` 风格）。
- [aclnn_ai_infra_scatter_block_update.cpp:L59-L74](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L59-L74)：空张量检查用 `ACLNN_ERR_PARAM_INVALID`——同一个宏家族，错误码区分「参数为空指针」与「参数取值非法」。
- [aclnn_ai_infra_scatter_block_update.cpp:L76-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L76-L89)：dtype 检查——`OP_CHECK_DTYPE_NOT_SUPPORT` 一行完成「不在支持列表 → 报错返回」，另有一处手写的 dtype 一致性检查示范了带参数的报错格式串。
- [aclnn_ai_infra_scatter_block_update.cpp:L91-L103](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L91-L103)：三步检查的编排处——每个 `bool CheckXxx()` 用 `CHECK_RET(ok, 对应错误码)` 翻译成 `aclnnStatus`。`CHECK_RET` 来自 CANN 头文件 `aclnn_kernels/common/op_error_check.h`（[L14](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L14) 引入），语义等价于 `if (!(cond)) { return expr; }`（算子文档里给出了它的参考定义，见 [docs/AiInfraScatterBlockUpdate.md:L115-L120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/AiInfraScatterBlockUpdate.md#L115-L120)）。
- [aclnn_ai_infra_scatter_block_update.cpp:L122-L134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L122-L134)：内部错误用 `ACLNN_ERR_INNER_NULLPTR` / `ACLNN_ERR_INNER_CREATE_EXECUTOR`——**参数错（PARAM_*）与内部错（INNER_*）必须分开**，这是调用方区分「自己传错了」与「算子实现出问题了」的唯一依据。

**op_host tiling 层范本**：

- [ai_infra_scatter_block_update_tiling.cpp:L20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L20)：引入 `ops_err.h` 的方式。
- [ai_infra_scatter_block_update_tiling.cpp:L111-L114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L111-L114)：`OP_CHECK_IF(cond, OP_LOGE(...), return ge::GRAPH_FAILED)` 三段式——条件、日志、善后，一行完成。注意此处 `OP_LOGE` 的**第一个参数是节点名**（`context_->GetNodeName()`），与 op_api 层同名宏的第一参数含义完全不同（两个宏来自不同的 CANN 头文件）。
- [ai_infra_scatter_block_update_tiling.cpp:L411-L412](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L411-L412) 与 [L418-L441](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L418-L441)：统一错误码上报实战——`PostTiling` 检查 workspace 指针、`TilingFunc4ScatterBlockUpdate` 检查 context、`TilingPrepare4ScatterBlockUpdate` 检查平台信息与编译信息，共 5 处全部使用 `OPS_REPORT_CUBE_INNER_ERR`（scatter 是 Cube/AIV 混合算子，按仓库惯例这里统一走 Cube 码上报；纯 Vector 算子应使用 `OPS_REPORT_VECTOR_INNER_ERR`）。

**ops-nn 对照**（拓宽视野）：ops-nn 侧另有一套自己的封装 [error_util.h:L31-L51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/inc/error_util.h#L31-L51)：`VECTOR_INNER_ERR_REPORT_TILIING` 同样上报 `"E89999"`，但额外用 `REPORT_INNER_ERR_MSG` 携带算子信息；`OP_LOGE_IF` 还加了 `static_assert` 强制条件必须是 bool。可见「E8/E6 错误码 + 条件检查宏」是全仓库的共同约定，只是封装层次各家略有差异。

#### 4.3.4 代码实践

**实践 C：为 my_add 补齐统一错误码（本讲主实践的核心）**

假设你按 u6-l3 的规划正在开发假想算子 `my_add`（输入 x、y，输出 z，支持 FP16/BF16），初稿的参数检查是「裸 return」风格。下面按仓库规范把它补齐。

1. **实践目标**：把裸 `return false` 改造成「日志 + 统一错误码 + 正确返回值类型」的仓库标准风格，体会在 op_api 与 op_host 两层写检查的差异。

2. **操作步骤**：

   ① op_api 层（`aclnn_my_add.cpp`，示例代码）——对照 4.3.3 的 scatter 范本：

   ```cpp
   // 示例代码：op_api 层 my_add 参数检查（风格仿 aclnn_ai_infra_scatter_block_update.cpp）
   static const std::initializer_list<op::DataType> MY_ADD_DTYPE_SUPPORT_LIST = {
       op::DataType::DT_FLOAT16, op::DataType::DT_BF16};

   bool CheckMyAddNotNull(const aclTensor *x, const aclTensor *y, const aclTensor *out)
   {
       if (x == nullptr) {
           OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "x tensor is nullptr");   // 日志带 aclnn 错误码
           return false;
       }
       if (y == nullptr) {
           OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "y tensor is nullptr");
           return false;
       }
       if (out == nullptr) {
           OP_LOGE(ACLNN_ERR_PARAM_NULLPTR, "out tensor is nullptr");
           return false;
       }
       return true;
   }

   bool CheckMyAddDtypeValid(const aclTensor *x, const aclTensor *y, const aclTensor *out)
   {
       OP_CHECK_DTYPE_NOT_SUPPORT(x, MY_ADD_DTYPE_SUPPORT_LIST, return false);
       OP_CHECK_DTYPE_NOT_SUPPORT(y, MY_ADD_DTYPE_SUPPORT_LIST, return false);
       if (x->GetDataType() != out->GetDataType()) {   // 手写一致性检查：报错必须带双方取值
           OP_LOGE(ACLNN_ERR_PARAM_INVALID, "out dtype %s should be the same as x dtype %s.",
                   op::ToString(out->GetDataType()).GetString(),
                   op::ToString(x->GetDataType()).GetString());
           return false;
       }
       return true;
   }

   aclnnStatus CheckMyAddParams(const aclTensor *x, const aclTensor *y, const aclTensor *out)
   {
       CHECK_RET(CheckMyAddNotNull(x, y, out), ACLNN_ERR_PARAM_NULLPTR);
       CHECK_RET(CheckMyAddDtypeValid(x, y, out), ACLNN_ERR_PARAM_INVALID);
       return ACLNN_SUCCESS;
   }
   ```

   ② op_host 层（`my_add_tiling.cpp`，示例代码）——用统一错误码宏替换裸返回：

   ```cpp
   // 示例代码：改造前（裸 return，不合格——没有日志、没有错误码、无诊断信息）
   // if (context_ == nullptr) { return ge::GRAPH_FAILED; }

   // 示例代码：改造后（仓库标准风格）
   #include "../../../common/include/err/ops_err.h"   // 引入统一上报宏

   ge::graphStatus MyAddBaseTiling::GetInputShape()
   {
       OP_CHECK_IF(context_ == nullptr,
           OPS_REPORT_CUBE_INNER_ERR("[MyAdd]", "context is null"),   // 日志 + E69999 上报
           return ge::GRAPH_FAILED);

       auto xDesc = context_->GetInputDesc(0);
       OP_CHECK_NULL_WITH_CONTEXT(context_, xDesc);                    // 判空检查宏：失败自带日志并返回

       OP_CHECK_IF(xDesc->GetDataType() != ge::DT_FLOAT16 &&
                   xDesc->GetDataType() != ge::DT_BF16,
           OP_LOGE(context_->GetNodeName(), "x dtype only supports FP16/BF16"),
           return ge::GRAPH_FAILED);
       return ge::GRAPH_SUCCESS;
   }
   ```

   ③ 自查每个检查点是否满足四要素：**有日志（带上下文标识）、有错误码（参数错 or 内部错选对）、返回值类型正确（aclnnStatus / graphStatus）、日志里带实际取值**。

3. **需要观察的现象**：改造后重新触发同样的非法输入（如传 FP32 的 x），日志里应出现带节点名/算子名的 ERROR 行；tiling 层的错误还应带 `E69999` 错误码前缀。
4. **预期结果**：所有失败路径都可从日志直接定位「哪个参数、什么取值、错在哪条规则」。若没有昇腾环境无法运行，可对照 u6-l1 的 UT 框架写 tiling 用例触发失败路径断言 `GRAPH_FAILED`——运行验证「待本地验证」。
5. 本实践代码为示例代码，my_add 算子本身是假想的，不需真实编译通过；重点是风格与宏的选择。

#### 4.3.5 小练习与答案

**练习 1**：`OP_LOGE` 在 op_api 层和 tiling 层的第一个参数分别是什么？为什么同名却不同义？

<details>参考答案：op_api 层传 aclnn 错误码（如 `ACLNN_ERR_PARAM_NULLPTR`），宏来自 CANN 的 `opdev/op_log.h`；tiling 层传算子节点名字符串（如 `context_->GetNodeName()`），宏来自图侧头文件。两者是不同头文件里各自定义的同名宏。阅读算子代码时必须先看 include 才能确定 `OP_LOGE` 的语义，否则会误判参数含义。</details>

**练习 2**：`ACLNN_ERR_PARAM_NULLPTR`、`ACLNN_ERR_PARAM_INVALID`、`ACLNN_ERR_INNER_NULLPTR` 三个错误码分别应在什么场景使用？

<details>参考答案：调用方传入了空指针 → `ACLNN_ERR_PARAM_NULLPTR`；参数非空但取值非法（空张量、dtype 不支持、shape 不匹配）→ `ACLNN_ERR_PARAM_INVALID`；参数都合法，但算子内部执行中产生的指针为空（Contiguous/CreateView/执行器创建失败）→ `ACLNN_ERR_INNER_NULLPTR`。区分参数错与内部错，调用方才能判断「改我的调用」还是「提算子工单」。</details>

**练习 3**：为什么 scatter 的 tiling 里 5 处框架级检查用 `OPS_REPORT_CUBE_INNER_ERR` 而不是 `OP_CHECK_IF + OP_LOGE`？

<details>参考答案：这 5 处（workspace 指针、context、平台信息、编译信息）不是「用户参数错」，而是**框架内部状态异常**——按 4.3.1 的约定应走统一错误码上报（打日志的同时上报 E69999），便于在系统级错误看板里聚合检索；`OP_CHECK_IF + OP_LOGE` 只打日志不上报。用户参数类的检查（dtype、shape 范围）才用后者。</details>

### 4.4 日志体系：ops_log.h 与 host/device 日志分级

#### 4.4.1 概念说明

`ops_log.h` 是仓库自有的日志宏入口，它把 CANN 的底层日志设施（slog / dfx_base）包装成一组统一命名。核心设计有三点：

1. **分级**：D（debug 调试）/ I（info 提示）/ W（warn 告警）/ E（error 错误）/ EVENT（事件）五级，另有 FULL 后缀的全量日志（超长文本自动分行）。
2. **打日志与报错分离**：`OPS_LOG_E` 在打 ERROR 日志的**同时**上报 `EZ9999` 错误码；`OPS_LOG_E_WITHOUT_REPORT` 只打不上报——当你已经用 `OPS_REPORT_*_INNER_ERR` 上报过、或只想留痕不想进错误统计时用后者。
3. **host 与设备双实现**：整个头文件被 `#ifdef DEVICE_OP_TILING_LIB` 一分为二——普通 host 编译走 `OPS_LOG_STUB_*`（CANN 完整日志设施，带模块名、错误码上报）；tiling 下沉到设备执行时（u5-l4 的 tiling sink，编译时由 `device_task.cmake` 注入 `DEVICE_OP_TILING_LIB` 宏）切换为 `dlog_debug/dlog_info/...` 直接调用，格式简化为 `[函数名]消息`。

而 **op_kernel 层完全没有日志**——这不是遗漏：AICore/AIV 核上没有 slog 运行时，kernel 的 `void` 入口也无处安放日志返回值。设备侧问题定位靠 host 侧 tiling/接口日志 + DFX 打点（op_api 层的 `L2_DFX_PHASE_1/2` 就是性能打点宏）。理解「哪一层能说话、说什么话」就是本模块所说的「日志分级」。

#### 4.4.2 核心流程

宏家族一览：

| 家族 | 宏 | 用途 |
| --- | --- | --- |
| 基础 | `OPS_LOG_D/I/W/E/EVENT` | 常规分级输出；E 带错误码上报 |
| 基础变体 | `OPS_LOG_E_WITHOUT_REPORT` | 只打 ERROR 不上报 |
| 全量 | `OPS_LOG_(D/I/W)_FULL` | 超长日志自动分行 |
| 条件 | `OPS_LOG_(D/I/W/E/EVENT)_IF(COND, OP_DESC, EXPR, ...)` | 条件成立才打日志并执行 EXPR |
| 判空 | `OPS_LOG_E_IF_NULL(OP_DESC, PTR, EXPR)` | 指针为空 → 打日志 + 上报 EZ9999 + EXPR |
| 检查 | `OPS_CHECK(COND, LOG_FUNC, EXPR)` | 条件成立 → LOG_FUNC + EXPR（fallback 代码专用风格） |

#### 4.4.3 源码精读

- [ops_log.h:L23-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L23-L41)：整个双实现分支的骨架。`#ifndef DEVICE_OP_TILING_LIB` 分支（host 侧）里 `OPS_LOG_E` 映射到 `OPS_INNER_ERR_STUB("EZ9999", ...)`（[L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L27)），这就是「E 级日志自动带错误码上报」的出处；`#else` 分支（设备侧）里同名宏直接调 `dlog_error(0, "[%s]" fmt, __func__, ...)`（[L37-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L37-L38)），用 `__func__` 替代模块名。
- [ops_log.h:L43-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L43-L48)：全量日志系列，注释明确说明「输出超长日志，若日志超长，则会被分为多行输出」——tiling 参数 dump 这类场景用它。
- [ops_log.h:L57-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L57-L62)：`OPS_LOG_E_IF_NULL` 把「判空 + 双动作（打日志 + 上报）+ 善后」固化成一个宏，且用 `__builtin_expect(..., 0)` 标注了冷分支提示编译器优化。
- [ops_log.h:L64-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/utils/inc/log/ops_log.h#L64-L68)：`OPS_CHECK`——fallback 的 host 执行函数通篇使用它（如 [fallback_ai_infra_esa_select_topk.cpp:L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L63)），与 tiling 层的 `OP_CHECK_IF` 三段式结构相同，只是名字与来源不同。
- [device_task.cmake:L23-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/device_task.cmake#L23-L29)：`DEVICE_OP_TILING_LIB` 的注入现场——tiling sink 目标编译时同时定义 `DEVICE_OP_LOG_BY_DUMP`、`OPS_UTILS_LOG_SUB_MOD_NAME`（以目标名作为日志子模块名）。[L34-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/device_task.cmake#L34-L37) 还把 `utils/inc`、`utils/inc/error`、`utils/inc/log` 加入头文件搜索路径——这正是 4.3.1 提到 `ops_error.h` 能以 `"error/ops_error.h"` 形式被引用的机制。
- **D 级日志的正面用法**：tiling 里两处 `OP_LOGD` 示范了「成功路径的参数留痕」——[ai_infra_scatter_block_update_tiling.cpp:L269-L273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L269-L273) 打印输入 shape/stride，[L343-L348](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L343-L348) 打印分核结果。注意这里用的是图侧 `OP_LOGD(nodeName, fmt)`（与 4.3 的 `OP_LOGE(nodeName, ...)` 同族），不是 `OPS_LOG_D`——又一层命名近似的宏家族，读代码时以 include 辨认。
- **kernel 无日志的验证**：对 `op_kernel` 目录全量检索 `OP_LOG[DEIW](` 零命中——设备 kernel 代码中不存在任何日志调用，与概念说明一致。

#### 4.4.4 代码实践

**实践 D：观察日志分级效果（读代码 + 可选运行）**

1. **实践目标**：体会 D/I/W/E 四级日志在实际代码中的分布规律，掌握「调高日志级别排查 tiling 问题」的手段。
2. **操作步骤**：
   ```bash
   cd inference/ascendc
   # 1) 统计各层日志宏的使用密度
   grep -rn "OP_LOGD\|OP_LOGI\|OP_LOGW\|OP_LOGE" --include="*.cpp" \
        src/ops-transformer/index/ai_infra_scatter_block_update/ | awk -F'OP_LOG' '{print $2}' | cut -c1 | sort | uniq -c
   # 2) 确认 kernel 层无日志
   grep -rn "OP_LOG" --include="*.cpp" --include="*.h" \
        src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ ; echo "exit=$?"
   # 3)（有环境时）调高日志级别后跑 scatter 的 ST
   export ASCEND_GLOBAL_LOG_LEVEL=1   # 0=debug 1=info 2=warning 3=error，以本机 CANN 文档为准
   pytest src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/ -x
   ```
3. **需要观察的现象**：第 1 步应看到 E（错误检查）最多、D（参数留痕）次之；第 2 步 `exit=1`（grep 无命中）；第 3 步日志里应能看到 scatter tiling 打出的 shape 与分核参数行。
4. **预期结果**：错误路径日志（E）在任何级别可见；成功路径的 tiling 详情（D）只在 debug 级别可见。环境变量名与取值以所用 CANN 版本文档为准，运行效果「待本地验证」。
5. 无硬件时完成第 1、2 步即可交付。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `OPS_LOG_E` 会自动上报错误码，还需要 `OPS_LOG_E_WITHOUT_REPORT` 这个「不上报」版本？

<details>参考答案：有些 ERROR 场景只需要本地留痕，不应进入错误码统计——最典型的是「已经用 `OPS_REPORT_*_INNER_ERR` 上报过一次」的场合（避免同一故障重复计数），或「错误已在别处归因，此处只是补充上下文」。ops-nn 的 [error_util.h:L31-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/common/inc/error_util.h#L31-L37) 正是这个组合用法：`OPS_LOG_E_WITHOUT_REPORT` + 独立的 `REPORT_INNER_ERR_MSG`。</details>

**练习 2**：tiling 下沉（`DEVICE_OP_TILING_LIB`）后，同一行 `OPS_LOG_I(...)` 代码的输出有什么变化？为什么需要这个变化？

<details>参考答案：host 侧经 `OPS_LOG_STUB_I` 输出带模块名、支持错误码设施的完整日志；设备侧改为 `dlog_info(0, "[%s]" fmt, __func__, ...)`，仅带函数名、走设备侧 dlog 通道。因为 tiling 代码此时运行在设备的 AICPU 环境里，host 的完整 slog 设施（含错误码上报链路）不可用，必须换轻量实现；同一份源码靠编译宏切换两种宿主环境。</details>

## 5. 综合实践

**给 my_add 建立完整的「错误处理 + 回退」检查单**，把本讲四个模块串起来。假想算子 my_add（FP16/BF16 逐元素加）已按 u6-l3 完成九件套骨架，现在补质量设施：

1. **op_api 层**（对应 4.3）：实现 `CheckMyAddNotNull / CheckMyAddDtypeValid / CheckMyAddParams` 三步检查，错误码按「参数空指针 → `ACLNN_ERR_PARAM_NULLPTR`；dtype 非法 → `ACLNN_ERR_PARAM_INVALID`；内部失败 → `ACLNN_ERR_INNER_NULLPTR`」选取，每条错误日志携带实际 dtype/shape 取值（参照实践 C 的示例代码）。
2. **op_host tiling 层**（对应 4.3）：在 `GetPlatformInfo`、`GetInputShape`、`PostTiling` 中，用户参数类检查用 `OP_CHECK_IF + OP_LOGE(nodeName, ...)`；框架状态类检查（context/platformInfo/compileInfo/workspace 指针）统一换成 `OPS_REPORT_CUBE_INNER_ERR`；在 `PostTiling` 末尾仿照 [ai_infra_scatter_block_update_tiling.cpp:L343-L348](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L343-L348) 用 `OP_LOGD` 打印全部 TilingData 字段。
3. **回退路径**（对应 4.1/4.2）：仿照 [fallback_ai_infra_esa_select_topk.cpp:L155-L195](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/fallback_ai_infra_esa_select_topk.cpp#L155-L195) 写 `fallback_my_add.cpp` 骨架：host 执行函数取输入输出与属性（每步 `OPS_CHECK` 守卫）→ `EXEC_OPAPI_CMD(aclnnMyAdd, x, y, out)` → `IMPL_OP(MyAdd).OpExecuteFunc(...)`。my_add 没有需要读数据的输入，`HostInputs` 留空数组即可；用一句话说明为什么（答案：没有任何张量的**数值**要在 host 侧读取，只有 shape/dtype/attr，这些由上下文直接提供）。
4. **验收自查表**（完成后逐项打勾）：
   - [ ] 每个失败路径都有日志，且日志带节点名或算子名标识；
   - [ ] 参数错与内部错使用不同的错误码/宏；
   - [ ] tiling 的框架级检查使用 `OPS_REPORT_*_INNER_ERR` 上报统一错误码；
   - [ ] kernel 层没有引入任何日志宏；
   - [ ] fallback 的 `EXEC_OPAPI_CMD` 参数顺序与 aclnn 接口签名完全一致（45 参的 FIA 样本就是靠人工对齐的，见 [fallback_ai_infra_fused_infer_attention_sink.cpp:L308-L353](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fallback_ai_infra_fused_infer_attention_sink.cpp#L308-L353)）。
   - [ ] 无硬件环境下，用 u6-l1 的 UT 框架为至少一条失败路径写断言 `GRAPH_FAILED` 的用例（运行「待本地验证」）。

交付物：三段示例代码 + 自查表勾选结果 + 实践 A 的 fallback 调用点清单。

## 6. 本讲小结

- **回退**是算子在 AICore kernel 之外的 host 侧执行路径：通过 `IMPL_OP(...).OpExecuteFunc(...).HostInputs({...})` 注册，host 函数把 `gert::Tensor` 转成 acl 描述符后借道 aclnn 两段式接口，让一份 L0 实现服务两条执行路径；仓库内 3 个注意力算子接入。
- **一阶段 `EXEC_OPAPI_CMD`** 在一次调用内完成「查符号（自定义 vendors → load_priority → CANN 内置三级链）→ 参数转换 → GetWorkspaceSize → 分配 workspace → 执行段 → 统一释放」；**两阶段 `EXEC_OPAPI_PREPARE_CMD`** 只做登记（`OpApiParams` + 默认删除器托管 + workspace 申报），执行交框架统一 launch——仓库内预置未用。
- `HostInputs` 声明的索引与 host 函数中 `GetData<T>()` 读数据的张量一一对应，这是「哪些张量数据必须落到 host 可读内存」的契约。
- 错误码规范：op_api 用 `ACLNN_ERR_PARAM_*`（参数错）/ `ACLNN_ERR_INNER_*`（内部错）配 `CHECK_RET`；op_host 用 `OPS_REPORT_VECTOR_INNER_ERR`（E89999）/ `OPS_REPORT_CUBE_INNER_ERR`（E69999）统一上报框架级错误；`ops_err.h` 与 `ops_error.h` 是内容相同、include 路径不同的双胞胎。
- 同名宏陷阱：`OP_LOGE` 在 op_api 层（错误码在前）与 tiling 层（节点名在前）语义不同；`OPS_LOG_E` 打日志同时上报 EZ9999，`OPS_LOG_E_WITHOUT_REPORT` 只打不报。
- 日志分级：op_kernel 层完全无日志（设备核无 slog 设施）；host 侧完整日志设施；tiling 下沉目标经 `DEVICE_OP_TILING_LIB` 编译宏切换到 `dlog_*` 设备实现。

## 7. 下一步学习建议

- 下一讲 **u5-l4 Tiling Sink 与 AICPU 执行通道** 将解释 `DEVICE_OP_TILING_LIB` 宏的完整来龙去脉——tiling 计算如何下沉到设备执行、`device_op_impl_registry` 如何注册设备侧算子实现，与本讲 4.4 的日志双实现直接衔接。
- 建议顺带阅读 [ascendc/src/ops-transformer/common/include/fallback/fallback.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/common/include/fallback/fallback.h) 的 `ConvertedParams` RAII 类（L548-L587），它与 u3-l2 的「参数必须在异步回调里统一释放」结论互为印证。
- 学完 u5 全部四讲后，进入 **u6-l1 UT 单测框架**：为错误路径写断言 `GRAPH_FAILED` 的用例，把本讲的检查逻辑纳入回归保护。
