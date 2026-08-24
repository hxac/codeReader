# fallback 机制：算子不支持时的两级降级

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 fallback 组件在「算子输入不满足 kernel 约束」时扮演的角色：在 aclnn/Host 层用**若干已存在的 aclnn 算子拆分组合**出等价计算，而不是改 kernel。
2. 读懂它的两个执行形态：`fallback.h` 的**单段式** `EXEC_OPAPI_CMD`（一步做完「准备 + 执行」）与 `fallback_2stages.h` 的**两段式** `EXEC_OPAPI_PREPARE_CMD`（只做准备，执行延迟给框架回调 `ExecuteOpLaunch`）。
3. 纠偏理解 `converted_params`：它记录的不是「做过 cast/pad/transpose 的转换清单」，而是**参数类型转换产出的 acl 对象及其析构器**，用于跨阶段持有与兜底释放。
4. 用 grep 独立鉴定「一个算子是否具备 fallback 能力」，并得出本仓库的关键事实：**fallback 组件当前零调用者，属「已备而未接线」**——这与 u3-l4 的 stub、u9-l2 的 tiling_sink 是同一类结论。

## 2. 前置知识

### 2.1 aclnn 两段式接口（回顾 u2-l5）

任何 aclnn 算子都分两段调用：第一段 `aclnnXxxGetWorkspaceSize` 在 Host 上校验参数、构造 `aclOpExecutor`、汇总 workspace 大小；第二段 `aclnnXxx(workspace, size, executor, stream)` 把任务下发到 stream。本讲的 fallback 组件就是「在算子代码内部再调用别的 aclnn 算子」的胶水层。

### 2.2 dlopen / dlsym 动态符号解析

编译期不链接 `libopapi.so`，运行期用 `dlopen` 打开动态库、`dlsym` 按名字取函数地址。u6-l2 讲过 torch_ops_extension 的 `EXEC_NPU_CMD_V1` 用同一套手法按名解析 aclnn 符号；本讲的 `fallback.h` 是同一模式在 exe_graph 语境下的镜像。

### 2.3 两种张量表示：gert::Tensor 与 aclTensor

- `gert::Tensor`：exe_graph 执行图内部的张量描述（GE 侧），有 `GetAddr()/GetStorageShape()/GetDataType()` 等接口；
- `aclTensor`：aclnn 接口的参数类型（ACL 侧），由 `aclCreateTensor` 创建、`aclDestroyTensor` 销毁。

fallback 的第一件事就是把前者转成后者——这是 `ConvertType` 系列重载的职责。

### 2.4 RAII 与 move-only 包装

`converted_params.h` 用「移动构造置空原对象的 valid 标志 + 析构时统一释放」实现只能移动、不能拷贝的资源持有器，思想同 `std::unique_ptr`。

### 2.5 命名辨析：仓库里四个同名的 fallback（务必分清）

| 出处 | 含义 | 与本讲关系 |
| --- | --- | --- |
| `common/include/fallback/` 目录 | 本讲组件：host 侧动态调用 aclnn 的宏与工具 | 主体 |
| [ascendc/cmake/variables.cmake:L230](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/variables.cmake#L230) 引用的 `${TOP_DIR}/asl/ops/cann/ops/built-in/op_fallback` | CANN 包内置的 op_fallback 组件 include 路径 | 本讲组件声明的 `ToAclDataType` 等符号的实现方在 CANN 侧，本仓库不可见其源码（待确认） |
| tiling 责任链的 `GRAPH_PARAM_INVALID` 让位（u3-l3） | 多个 tiling 实现间的回退调度 | 机制无关，勿混 |
| [test_attention_pionner_tiling.cpp:L3345-L3348](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/tests/ut/op_host/test_attention_pionner_tiling.cpp#L3345-L3348) 注释里的 `tempKVN=0 fallback` | pioneer tiling 内部某分支的兜底路径 | 仅为 UT 注释，与组件无关 |

## 3. 本讲源码地图

| 文件 | 作用 | 一句话定位 |
| --- | --- | --- |
| [common/include/fallback/fallback.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L1-L491) | 单段式主体：符号解析链、ConvertType 参数转换、`EXEC_OPAPI_CMD` 宏 | 「一步到位」版调用器 |
| [common/include/fallback/fallback_2stages.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L1-L133) | 两段式：`EXEC_OPAPI_PREPARE_CMD` 只做准备，执行交给框架二阶段 | 「延迟执行」版调用器 |
| [common/include/fallback/converted_params.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/converted_params.h#L1-L78) | `ConvertedParams<Tuple>` move-only RAII 包装 | 参数元组的「保管员」 |
| [common/include/fallback/fallback_comm.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm.h#L1-L43) | 公共声明：`ToAclDataType`（实现在 CANN 侧） | 通信/公共头 |
| [common/include/fallback/fallback_comm_2stages.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L1-L56) | 二阶段协议结构：`OpApiAnyValue`、`OpApiParams`、`ExecuteOpLaunch` 声明 | 一二阶段的「交接合同」 |

辅助证据文件（用于鉴定接线状态）：

- [ascendc/src/ops-transformer/common/CMakeLists.txt:L55-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L55-L56)：common 只编译 `src/tiling_base/*.cpp` 与 `src/*.cpp`——fallback 是纯头文件组件，且没有任何目标编译包含它的翻译单元。
- [ascendc/cmake/ut.cmake:L59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L59)：UT 构建把 `common/include` 挂上 include 路径，所以未来任何算子想 `#include "fallback/fallback.h"` 在 UT 下都能编过。
- [ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h:L292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L292) 与 [L965](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965)：同款 `GetOpApiFuncAddrInLib`/`EXEC_NPU_CMD_V1`，是 fallback.h 的「torch 语境姐妹篇」，用于对照。

## 4. 核心概念与源码讲解

### 4.1 fallback 组件全景：触发场景与符号解析链

#### 4.1.1 概念说明

一个融合算子的 kernel 通常只支持一个「约束锥」：特定 dtype、特定 shape 范围、特定排布（回顾 u2-l1：aggregate_hidden 只接受 B∈[1,8]、H 为 192 的倍数等）。当用户输入落在锥外时，有两条路：

1. 直接报错拒绝（本仓库绝大多数 tiling 的 `OP_CHECK_IF` 走这条）；
2. **fallback**：在 aclnn/Host 层把输入「整形」成 kernel 接受的形态（cast、pad、transpose、按维度拆分），用若干**已存在的** aclnn 算子组合出等价计算，再把结果整理回用户要的形态。

路 2 需要一个前提能力：**在算子 Host 代码里以字符串名字调用任意 aclnn 算子**。fallback 组件就是这套「按名调用 + 参数类型转换 + 生命周期管理」的模板代码。它把「拆分组合」的具体策略留给各算子自己写，只固化公共骨架——类似 u3-l3 tiling_base 固化七步流程的思路。

#### 4.1.2 核心流程

一次「按名调用 aclnn」的符号解析是三级瀑布：

```text
GetOpApiFuncAddr("aclnnXxx")
  ├─ ① libcust_opapi.so（自定义算子包的 opapi 库，优先）
  ├─ ② libopapi.so（CANN 内置 opapi 库）
  └─ ③ 逐个尝试 6 个 aclnn 细分库：
       libaclnn_ops_infer.so / libaclnn_ops_train.so / libaclnn_math.so
       libaclnn_rand.so     / libaclnn_sparse.so    / libaclnn_fft.so
          ↓ 全部失败
       OP_LOGE 报错，返回 nullptr
```

拿到函数地址后，一次完整调用的时序（单段式）：

```text
EXEC_OPAPI_CMD(aclnnXxx, 实参...)
  → ResetCacheThreadLocal()                 # 清 PTA 线程级缓存
  → ConvertTypes(实参..., &ws_size, &executor)  # GE 类型 → ACL 类型
  → 调 aclnnXxxGetWorkspaceSize(...)         # 一阶段
  → host_api_ctx->MallocWorkspace(ws_size)   # 由执行上下文分配 workspace
  → host_api_ctx->GetStream() 取流
  → aclnnXxx(workspace, ws_size, executor, stream)  # 二阶段，立即执行
  → ReleaseConvertTypes(...)                 # 统一销毁转换出的 acl 对象
  → host_api_ctx->FreeWorkspace()
```

#### 4.1.3 源码精读

**库名列出与 dlopen/dlsym 封装**：[fallback.h:L81-L107](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L81-L107) 定义两个库名常量 `libopapi.so`/`libcust_opapi.so`，并用 `GetOpApiLibHandler`（dlopen，`RTLD_LAZY` 懒加载）与 `GetOpApiFuncAddrInLib`（dlsym，失败打 `OP_LOGW`）完成最底层的取址。注意它们是 `static` 局部缓存——进程内只 dlopen 一次。

**6 个 aclnn 细分库兜底**：[fallback.h:L109-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L109-L124) 的 `GetAclnnArrdByApiName`（Arrd 是 Addr 的笔误）按固定顺序在 6 个库里轮询找符号，全部落空才 `OP_LOGE`。这说明 fallback 调用的目标算子不限于本仓库自装的——CANN 内置的 Cast、Pad、Transpose、MatmulV2 等都可作为「组合积木」。

**主解析链**：[fallback.h:L126-L145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L126-L145) 的 `GetOpApiFuncAddr` 先查自定义库、再查内置库、最后走 6 库兜底；配合 [L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L79) 的 `GET_OP_API_FUNC(apiName)` 宏（拼接 `_##apiName` 函数指针类型并转型），所有 `aclCreateTensor`/`aclDestroyTensor` 等 ACL 基础函数也走同一链路获取。

**与 torch 扩展层的镜像关系**：`ops_common.h` 里有几乎同名的 `GetOpApiFuncAddrInLib`（[ops_common.h:L292](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L292)）与 `GetOpApiFuncAddr`（[L325](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L325)）——同一套「cust 优先、内置兜底」的解析模式被复制到两个语境：fallback.h 服务 exe_graph 的 `host_api_ctx`，ops_common.h 服务 torch 扩展的 at::Tensor 世界（u6-l2）。

#### 4.1.4 代码实践：鉴定「谁在用 fallback」

**实践目标**：用 grep 审计全仓库对 fallback 组件的真实引用，得到「是否接线」的硬结论。

**操作步骤**（在仓库 `training/` 目录下执行）：

```bash
# 1. 谁 include 了 fallback 头文件？（排除讲义目录与组件自身）
grep -rn "fallback/fallback\.h\|fallback/fallback_2stages\.h\|fallback/converted_params\.h\|fallback/fallback_comm" \
     ascendc/src ascendc/CMakeLists.txt ascendc/cmake/

# 2. 两个执行宏在何处出现？
grep -rn "EXEC_OPAPI_CMD\|EXEC_OPAPI_PREPARE_CMD" ascendc/

# 3. ToAclDataType / ExecuteOpLaunch 在本仓库有无实现（.cpp 定义）？
grep -rn "aclDataType ToAclDataType\|graphStatus ExecuteOpLaunch" ascendc/src/
```

**需要观察的现象**：命令 1、2 的命中全部落在 `common/include/fallback/` 五个头文件**自身**（fallback_2stages.h include fallback.h 等）；命令 3 只命中 `fallback_comm.h`/`fallback_comm_2stages.h` 里的**声明**，无任何 `.cpp` 定义。

**预期结果**（本次已实际验证）：**本仓库没有任何算子引用 fallback 组件，`EXEC_OPAPI_CMD`/`EXEC_OPAPI_PREPARE_CMD` 使用次数为 0**。结合 common/CMakeLists.txt 只编译 `src/tiling_base/*.cpp` 与 `src/*.cpp`（[common/CMakeLists.txt:L55-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L55-L56)，另见非 UT 分支 [L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L87)），可下结论：**fallback 是随 common 携带的纯头文件组件，当前不参与任何编译产物，属「已备而未接线」**——与 u3-l4 stub 桩、u9-l2 tiling_sink 的处境相同。这也直接给出「识别算子是否具备 fallback 能力」的判据：看它的 op_api/op_host 源码是否 include `fallback/fallback.h`（或出现 EXEC_OPAPI 宏）；本仓库当前答案是「无」。

#### 4.1.5 小练习与答案

**练习 1**：为什么符号解析要先查 `libcust_opapi.so` 再查 `libopapi.so`？
**答案**：cust 库是用户自装算子包（u1-l4 的 run 包释放到 opp/vendors 后生成）的 aclnn 载体，内置库是 CANN 出厂算子。同名算子时自定义实现应覆盖内置实现，自定义优先保证「我自己装的算子优先被调用」，与 u6-l2 EXEC_NPU_CMD_V1「自装 run 包优先、CANN 内置兜底」的顺序一致。

**练习 2**：6 个 aclnn 细分库兜底的存在说明什么设计意图？
**答案**：说明 fallback 的「组合积木」不限于当前编译的算子包，而覆盖 CANN 全量算子（infer/train/math/rand/sparse/fft 六大类）。拆分组合时可以自由取用 Cast、Transpose、Matmul 等基础算子，不必自己实现。

**练习 3**：既然零调用者，这个组件为什么还留在仓库里？
**答案**：common 是从完整算子仓裁剪来的公共底座（u3-l2），fallback 头是未来给 op_api 层做「输入不满足约束时的降级组合」预留的模板件；`ut.cmake` 已把 `common/include` 挂进 UT include 路径，一旦某算子 include 它即可编译。读公共组件必须 grep 核实接线，不能因「存在」推断「在用」。

### 4.2 参数转换层：ConvertType 家族与 converted_params 的真实含义

#### 4.2.1 概念说明

调用 aclnn 接口时，实参类型必须是 ACL 世界的（`aclTensor*`、`aclIntArray*`、`aclScalar*`…），而 fallback 的调用方手里往往是 GE 世界的 `gert::Tensor*` 与 C++ 原生类型。`ConvertType` 重载家族负责「按实参类型自动择路转换」，转换产物打包成 tuple——这就是 `converted_params` 的字面来源。

**纠偏**（本讲最重要的一点）：大纲里说 converted_params「记录转换信息（cast/pad/transpose 等）」并不准确。代码中它记录的是**转换产出的 acl 对象本身及其释放方式**，是一份「资源持有清单」，不是「变换日志」。cast/pad 这类数值变换并不发生在 ConvertType 里——它们是调用方先用其它 aclnn 算子完成的；ConvertType 唯一显式支持的「变换」是 `ConvertMmType` 的 transpose 视图与 NZ 格式透传（见 4.2.3），且只改 stride/shape **元数据**，不搬数据。

#### 4.2.2 核心流程

```text
ConvertTypes(args...)                    # 对每个实参按类型分派：
  ├─ aclTensor*          → 原样返回（恒等重载）
  ├─ gert::Tensor*       → aclCreateTensor(shape, 连续strides, dtype, ND, addr)
  ├─ vector<const gert::Tensor*>& → aclTensorList
  ├─ vector<int64_t>     → aclCreateIntArray（空向量转 nullptr！）
  ├─ T 标量              → 原样返回（模板恒等重载）
  └─ （仅 ConvertMmType 额外提供 transpose/NZ 变体）
结果 = std::tuple<...>                    # 即 converted_params
```

连续张量 stride 的计算公式（倒序累乘）：

\[ \text{strides}[i] = \prod_{j>i} \text{shape}[j], \qquad \text{strides}[\text{last}] = 1 \]

#### 4.2.3 源码精读

**两个恒等重载**：[fallback.h:L147-L160](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L147-L160)：`ConvertType(aclTensor*)` 直接透传；`ConvertType(const std::vector<int64_t>&)` 调 `aclCreateIntArray` 造数组对象，**空向量返回 nullptr**——这正好对接 aclnn 可选 IntArray 参数传 nullptr 的惯例（u2-l5）。

**dtype 映射**：[fallback.h:L162-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L162-L192) 的 `GetConvertType` 用 if-else 链把 `DT_FLOAT/DT_BF16/DT_BOOL/DT_INT64/...` 映射到 `ACL_FLOAT/ACL_BF16/...`，**未列出的类型一律落到默认 `ACL_FLOAT16`**——一个静默降级点，读码时要警惕：如果 GE 侧出现新 dtype 而此处未更新，不会报错而是被当成 fp16。注意另有 `ToAclDataType`（[fallback.h:L286](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L286) 调用），它声明于 [fallback_comm.h:L33-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm.h#L33-L36) 且是 `extern "C"`，实现不在本仓库（由 CANN 包提供，待确认）——同一个头文件里并存「本文件 if-else 版」与「外部库版」两套映射。

**主力转换 gert::Tensor→aclTensor**：[fallback.h:L194-L231](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L194-L231) 依次取设备地址、映射 dtype、抄 shape、按上式算连续 strides，最后 `aclCreateTensor(shape, strides, offset=0, ACL_FORMAT_ND, storageShape=shape, addr)`。两个隐含假设：**视张量为连续**（不读真实 stride）、**offset 恒 0**。因此非连续/带偏移的 GE 张量不能走这个入口。

**ConvertMmType 的 transpose 视图**：[fallback.h:L270-L320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L270-L320) 是组件里唯一带「变换开关」的转换：`transpose=true` 时交换最后两维的 stride 与 viewShape（[L298-L310](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L298-L310)），即用「负视角」表达 \( X^T \) 而不搬数据——这正是矩阵乘 fallback 中「把 B 当 Bᵀ 用」的零拷贝技巧；`enable_NZ` 且源格式为 `FORMAT_FRACTAL_NZ` 时保留 NZ 格式标签（[L311-L314](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L311-L314)）。storageShape 仍传原始 shape（[L315-L316](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L315-L316)）：物理存储不动，只有逻辑视图变了。

**批量转换与统一释放**：[fallback.h:L368-L399](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L368-L399) 的 `ConvertTypes`（变参打包成 tuple）、`ReleaseConvertTypes`（按 tuple 逐元素调对应 `Release` 重载销毁）、`call`（借 `std_utils::index_sequence` 展包调用函数指针）。[L38-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L38-L52) 手写了 C++14 `index_sequence` 的 C++11 降级版，说明该组件面向的老编译器基线。

**ConvertedParams RAII 包装**：[converted_params.h:L36-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/converted_params.h#L36-L75) 把 tuple 包成 move-only 对象：移动时把源对象 `validParams_` 置 false（[L40-L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/converted_params.h#L40-L43)），析构时仅当 `validParams_` 为 true 才 `ReleaseConvertTypes`（[L60-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/converted_params.h#L60-L65)）——保证「参数所有权跨函数转移时不 double-free」。注意：当前 `EXEC_OPAPI_CMD` 用的是裸 tuple + 手工释放，这个包装类留给需要把参数持有到两阶段之间的调用方（同属已备未用）。

#### 4.2.4 代码实践：手算 stride，验证转换逻辑

**实践目标**：不依赖 NPU，通过手算验证 `ConvertType` 与 `ConvertMmType` 的 stride/视图逻辑。

**操作步骤**：

1. 设 GE 张量 shape = `[2, 3, 4]`。按公式手算连续 strides：`strides[2]=1`，`strides[1]=4*1=4`，`strides[0]=3*4=12`，得 `[12, 4, 1]`。对照 [fallback.h:L217-L221](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L217-L221) 的循环，确认代码产出一致。
2. 再算 `ConvertMmType(t, transpose=true)`：交换最后两维 → `strides=[12, 1, 4]`，`viewShape=[2, 4, 3]`，storageShape 仍 `[2, 3, 4]`。对照 [L298-L316](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L298-L316)。
3. 用 numpy 做交叉验证（CPU 即可，示例代码）：

```python
# 示例代码：验证 transpose 视图等价于物理转置
import numpy as np
x = np.arange(24).reshape(2, 3, 4)
xt = x.transpose(0, 2, 1)          # 逻辑视图 [2,4,3]
print(xt.shape)                    # (2, 4, 3)
print(xt.copy()[1, 2, 0] == x[1, 0, 2])  # True：零拷贝换视角
```

**需要观察的现象**：numpy 的 `transpose` 同样只改元数据不搬数据；`xt.strides` 与手算值（按 8 字节元素折算）一致。

**预期结果**：手算、代码循环、numpy 三方一致，确认「transpose 视图 = 只换 strides/viewShape 的零拷贝表达」。

#### 4.2.5 小练习与答案

**练习 1**：`ConvertType(gert::Tensor*)` 假设张量连续且 offset=0，若传入非连续张量会发生什么？
**答案**：不会报错，但按连续 strides 算出的 aclTensor 描述与真实内存布局不符，后续 aclnn 算子会按错误偏移读数据，产生静默的数值错乱。调用方必须先保证连续（或走 ConvertMmType 的受控视图）。这与 u2-l2 `AutoContiguous` 要求框架保证连续是同一类契约。

**练习 2**：为什么 `GetConvertType` 的 else 分支默认返回 `ACL_FLOAT16` 而不是报错？
**答案**：这是一处「静默降级」的宽松处理（也可能是历史包袱）：未知 dtype 也能造出合法 aclTensor 让调用继续。代价是 dtor 新类型时会拿 fp16 解释数据。二次开发若新增 dtype 支持，务必同步补这条 if-else 链——这是一个隐蔽的改动点。

**练习 3**：`ConvertScalarType`（[fallback.h:L252-L262](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L252-L262)）有什么限制？
**答案**：`typeid(value) == typeid(float)` 只认 float，其余类型返回 nullptr——即该辅助函数当前只能造 float 标量；int64/bool 标量需调用方自行走 `aclCreateScalar`。

### 4.3 单段式 EXEC_OPAPI_CMD：一步完成两段调用

#### 4.3.1 概念说明

`EXEC_OPAPI_CMD(aclnn_api, ...)` 是「立即执行」形态：在**一个宏展开块**里串起「符号解析 → 参数转换 → 一阶段 GetWorkspaceSize → workspace 分配 → 二阶段下发 → 资源释放」全流程。它适合 fallback 策略简单、无需与框架调度协商的场景——调用点顺序执行若干次组合算子即可。

它与 u6-l2 的 `EXEC_NPU_CMD_V1`（[ops_common.h:L965](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965)）是同构姐妹：后者在 torch 扩展里把 at::Tensor 转 aclTensor 后同样走「两段式 + workspace + stream」，差异只在宿主语境——EXEC_OPAPI_CMD 的一切资源来自 `host_api_ctx`（exe_graph 执行上下文提供的 workspace 分配器与 stream），而 EXEC_NPU_CMD_V1 来自 torch/aclrt 环境。

#### 4.3.2 核心流程

见 4.1.2 的时序图。补充关键点：

- workspace 与 stream 都问 `host_api_ctx` 要（[fallback.h:L459](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L459)、[L466](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L466)）——`host_api_ctx` 在本仓库无声明，属 CANN exe_graph 侧的上下文指针（`op_execute_context.h` 引入，待确认）；
- 释放时机被安排在**二阶段函数返回之后**的 lambda 内（`ReleaseConvertTypes` + `FreeWorkspace`），因为 aclnn 二阶段只是异步下发，executor/参数在下发完成前必须存活。

#### 4.3.3 源码精读

**宏主体**：[fallback.h:L430-L486](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L430-L486)。分段看：

- [L434-L443](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L434-L443)：静态缓存三个符号地址（`ResetCacheThreadLocal`、`aclnnXxxGetWorkspaceSize`、`aclnnXxx`），任一缺失即 `OP_LOGE` + 置 `GRAPH_FAILED` 后 break——**只影响本次**（见下）；
- [L444-L451](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L444-L451)：先 `ResetCacheThreadLocal()` 清 PTA（PyTorch-Ascend）线程级 executor 缓存，再把「用户实参 + workspace 指针 + executor 指针」一起 `ConvertTypes` 打包——这就是**一阶段实参表的组装方式**：宏自动在尾部追加两个出参；
- [L452-L465](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L452-L465)：调一阶段；成功后按返回的 workspace_size 向 `host_api_ctx` 申请内存；
- [L466-L483](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L466-L483)：构造 lambda `acl_call`——把二阶段函数指针定型为 `int(*)(void*, uint64_t, aclOpExecutor*, const aclrtStream)` 并立即执行，随后释放参数与 workspace；整个宏用 GNU 语句表达式 `({ ... })` 返回 `ret`，可写在 `if`/赋值右边当普通函数用。

**两个阅读陷阱**：

1. `static auto ret = GRAPH_SUCCESS;`（[L432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L432)）：ret 是 static 的，若首次因符号缺失失败，后续调用在走成功路径前会先带着上次的旧值；且多线程共用同一 static 有数据竞争隐患。这是模板件的粗糙处，若未来接线需注意（同类细节在 u3-l1 读 utils 时也提示过「公共件并非完美」）。
2. lambda 按值捕获了 `host_api_ctx` 与 `converted_params`，但 lambda 是**立即同步执行**的——捕获只为打包参数，不存在延迟执行；延迟形态在 4.4。

#### 4.3.4 代码实践：对照 EXEC_NPU_CMD_V1

**实践目标**：并排阅读两个「按名调 aclnn」的宏，提炼公共骨架与语境差异。

**操作步骤**：

1. 打开 [fallback.h:L430-L486](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L430-L486) 与 [ops_common.h:L965](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965) 起的 `EXEC_NPU_CMD_V1`；
2. 按五行维度填表：符号解析来源 / 参数转换入口 / workspace 分配方式 / stream 来源 / 释放时机；
3. 回答：为什么两边都要在尾部自动追加 `workspace_size_addr` 与 `executor_addr` 两个出参？

**需要观察的现象**：两者的一阶段组装结构完全同构（`ConvertTypes(实参..., &ws, &exec)` + `call(getWorkspaceSizeFunc, ...)`）。

**预期结果**：得到类似下表（「待本地验证」项留给读者在真机环境核对）：

| 维度 | EXEC_OPAPI_CMD | EXEC_NPU_CMD_V1 |
| --- | --- | --- |
| 语境 | exe_graph host 执行上下文 | torch 扩展（csrc） |
| 输入类型 | gert::Tensor* 等 GE 类型 | at::Tensor |
| workspace | host_api_ctx->MallocWorkspace | aclrtMalloc 系（u6-l2） |
| stream | host_api_ctx->GetStream() | 当前 torch_npu stream |
| 尾追加参 | &ws_size, &executor | 同左（两段式 aclnn 的通用出参契约） |

**第 3 问答案**：aclnn 一阶段的标准签名就是「业务参数 + uint64_t* workspaceSize + aclOpExecutor** executor」——两个出参是 aclnn 协议的一部分，宏必须替调用方补齐才能拼出正确的一阶段实参表（u2-l5 的接口契约在宏层的体现）。

#### 4.3.5 小练习与答案

**练习 1**：为什么一阶段与二阶段之间不能立刻释放转换出的 aclTensor？
**答案**：一阶段只是把参数描述与任务记入 executor；二阶段 `aclnnXxx` 下发时仍要读这些描述（且 executor 内部可能引用它们）。释放必须排在二阶段返回之后，所以宏把 `ReleaseConvertTypes` 放进执行 lambda 的末尾。

**练习 2**：宏里 `ResetCacheThreadLocal` 的作用是什么？
**答案**：清掉 ACL 层线程本地的 executor/参数缓存，避免上一次调用残留的缓存状态污染本次 fallback 组合调用（多个 aclnn 连续调用时尤甚）。它与其他 ACL 基础函数一样经 `GetOpApiFuncAddr("ResetCacheThreadLocal")` 动态获取（[L434](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L434)）。

**练习 3**：如果目标算子符号在三个库里都找不到，宏的行为是？
**答案**：`GetOpApiFuncAddr` 打 OP_LOGE 返回 nullptr，宏在 [L437-L442](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L437-L442) 判断后置 `GRAPH_FAILED` 并 break，整条语句表达式返回失败——调用方应检查返回值决定是否再降级。

### 4.4 两段式 EXEC_OPAPI_PREPARE_CMD 与 ExecuteOpLaunch：延迟执行协议

#### 4.4.1 概念说明

单段式的问题：它自己分配 workspace、自己取 stream、自己立即执行——这在「算子只是自己想调个子算子」时没问题，但在 **exe_graph 统一调度**的语境下，workspace 要由框架按整个图的需求汇总分配、执行要挂到框架的 launch 阶段。于是有了两段式（本讲标题的「两级」在代码里的真实落点，即 two stages）：

- **一阶段 `EXEC_OPAPI_PREPARE_CMD`**：做与单段式相同的前半段（解析符号、转换参数、调 GetWorkspaceSize），但不执行——把「二阶段函数指针 + executor + 全部转换参数」打包进 `OpApiParams` 交给 `host_api_ctx` 保管，并只登记 workspace 需求；
- **二阶段 `ExecuteOpLaunch`**：框架在 launch 时机回调该函数（声明于 fallback_comm_2stages.h），从 context 里取回 `OpApiParams` 完成下发与释放。注释明确说明它「函数实现可以与算子类型无关，所有算子使用同一个二阶段注册接口」——即一份通用二阶段代码服务所有走 fallback 的算子。

#### 4.4.2 核心流程

```text
一阶段（Host 准备期）
  EXEC_OPAPI_PREPARE_CMD(aclnnXxx, 实参...)
    → new OpApiParams{ executor=nullptr, op_api_func=符号地址, converted_params=[] }
    → ConvertTypes(实参..., &ws_size, &op_api_params->executor)
    → CollectConvertedTypes: 逐元素 Collect(参数) → OpApiAnyValue{指针, 析构器}
    → host_api_ctx->SetOpApiParamsWithDefaultDeleter<OpApiParams>(op_api_params)
    → 调 GetWorkspaceSize
    → host_api_ctx->SetWorkspaceSizes({workspace_size})     # 只登记，不分配

二阶段（框架 launch 期）
  ExecuteOpLaunch(context)
    → 从 context 取 OpApiParams
    → op_api_func(workspace, size, executor, stream) 下发
    → 逐个执行 OpApiAnyValue.deleter 释放参数
```

`OpApiParams` 是一、二阶段之间的「交接合同」——注释原文：定义在算子仓、由算子感知、GE 框架不感知（框架只当它是不透明数据搬运）。

#### 4.4.3 源码精读

**协议结构体**：[fallback_comm_2stages.h:L34-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L34-L46) 定义三件东西：

- `OpApiAnyValue{void* pointer; OpApiAnyValueDeleter deleter;}`——「资源 + 怎么删」的最小单元，把类型各异的 acl 对象统一擦成 void*；
- `OpApiFunc = int(*)(void*, uint64_t, aclOpExecutor*, const aclrtStream)`——二阶段函数签名；
- `OpApiParams{converted_params; executor; op_api_func}`——整包交接物。

**通用二阶段入口声明**：[fallback_comm_2stages.h:L48-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L48-L49) 声明 `ge::graphStatus ExecuteOpLaunch(gert::OpExecuteLaunchContext*)`。与 `ToAclDataType` 一样，其实现不在本仓库（待确认，应由 CANN 侧或接线方提供）。

**Collect 家族：把析构器绑进参数**：[fallback_2stages.h:L34-L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L34-L79) 为每种 acl 类型写了一个 `Collect` 重载（如 `Collect(aclTensor*, params)` 在 [L34-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L34-L40)），每个都 `emplace_back(OpApiAnyValue{p, [](void* param){ aclDestroyTensor(static_cast<aclTensor*>(param)); }})`——**用 lambda 捕获具体销毁函数、擦除类型**。标量等无需释放的类型走模板重载填 `{nullptr, nullptr}`（[L74-L79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L74-L79)）。[L81-L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L81-L92) 的 `CollectConvertedTypes` 用 index_sequence 展包逐个 Collect。这正是 4.2 纠偏结论的证据：**converted_params 里存的是「对象 + 析构器」，不是变换日志**。

**一阶段宏**：[fallback_2stages.h:L94-L130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L94-L130)。与单段式的三点关键差异：

1. `executor_addr` 直接指向 `op_api_params->executor`（[L113](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L113)）——一阶段产出的 executor 直接落在交接结构里；
2. [L119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L119) `SetOpApiParamsWithDefaultDeleter<OpApiParams>(op_api_params)` 把整包交给 `host_api_ctx` 托管（框架用默认 deleter `delete` 它；内部的 acl 对象由各 `OpApiAnyValue.deleter` 负责）；
3. [L127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L127) 收尾只 `SetWorkspaceSizes({workspace_size})` 登记需求，**没有 MallocWorkspace、没有执行**。

注意该文件 include 了 `mc2_log.h`（[L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L27)）——来自 CANN 的 mc2 通信组件 include 路径（[variables.cmake:L225](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/variables.cmake#L225) 附近配置），再次印证该组件的编译依赖在 CANN 包侧。

#### 4.4.4 代码实践：画一/二阶段交接图

**实践目标**：以 `OpApiParams` 为中心，画出两段式 fallback 的数据交接图，并对比单段式资源生命周期的差异。

**操作步骤**：

1. 白纸画三个泳道：一阶段宏 / `host_api_ctx`（托管区）/ 二阶段 `ExecuteOpLaunch`；
2. 在一阶段泳道标出四个动作：new OpApiParams → ConvertTypes（executor 直填结构体）→ Collect 打包 deleter → SetWorkspaceSizes 登记；
3. 在托管区画 `OpApiParams` 的三字段内容示意（converted_params 是 N 个 `{指针, 析构lambda}`）；
4. 在二阶段泳道标出：取回 → 调 `op_api_func(workspace, size, executor, stream)` → 逐个跑 deleter；
5. 对照 [fallback.h:L457-L465](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback.h#L457-L465)（单段式 MallocWorkspace + 立即执行），在图旁写一句结论：**两段式把「分配权」与「执行时机」上交框架，单段式自管**。

**需要观察的现象**：两段式里没有任何一处出现 `MallocWorkspace`/`GetStream`/执行调用——资源动作全部消失，只剩登记。

**预期结果**：交接图能清楚回答「一阶段结束后、二阶段触发前，谁替 aclTensor 续命」——是 `OpApiAnyValue` 里的析构 lambda 列表。此实践为纯源码阅读型，无需 NPU（本仓库当前无调用点，无法真机运行，标注：待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`OpApiAnyValue` 为什么用 `void*` + 函数指针，而不是 `std::variant` 或继承体系？
**答案**：二阶段执行方（`ExecuteOpLaunch` 的通用实现）要不感知具体算子类型；`{void*, deleter}` 是最小、最 C 友好的类型擦除（结构体还是 extern "C" 包裹的），跨 so/编译器边界最稳。代价是类型安全靠各 Collect 重载保证。

**练习 2**：`SetOpApiParamsWithDefaultDeleter<OpApiParams>` 的「DefaultDeleter」删的是什么？aclTensor 是谁删的？
**答案**：DefaultDeleter 只负责 `delete OpApiParams` 这个外层结构体；内部的 aclTensor/aclIntArray 由 `converted_params` 中各自的 `OpApiAnyValue.deleter` 删——两层析构职责分离。

**练习 3**：若同一算子在一阶段之后、二阶段执行前又发起了一次 fallback 调用，`static auto ret`（[fallback_2stages.h:L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_2stages.h#L95)）会有问题吗？
**答案**：会。ret 是 static 局部变量，同一线程重入时上次的结果会被覆盖/继承初值语义混乱，且非线程安全。每次展开宏共享同一 static 是这两个宏共同的已知粗糙点，接线使用时应改造为栈变量（阅读陷阱，见 4.3.3）。

## 5. 综合实践

**任务**：为「假想的 aggregate_hidden B 超限 fallback」写一份完整设计草图，并完成接线状态审计。

**背景**：u2-l1 已确认 aggregate_hidden 约束 B∈[1,8]（tiling 侧 `OP_CHECK_IF` 硬校验，超限直接 `GRAPH_FAILED`）。假设产品要求 B=16 也能跑，且不愿改 kernel。

**第一步：流程图**（文字版，读者自行转画）：

```text
用户输入 x[S,16,H], w[W,H], mask
        │
        ▼
aclnn 层发现 B=16 > kernel 上限 8          ← 触发条件
        │
        ▼
转入 fallback（示例设计，非仓库现有代码）
  ① 按第 1 维切成 x0[S,8,H] 与 x1[S,8,H]      （aclnnSplit / 切片视图）
  ② mask 同步切两份
  ③ EXEC_OPAPI_CMD(aclnnAiInfraAggregateHidden, x0, w, mask0, y0)
  ④ EXEC_OPAPI_CMD(aclnnAiInfraAggregateHidden, x1, w, mask1, y1)
  ⑤ 沿 B 维拼回 y[S,16,H]                      （aclnnConcat）
        │
        ▼
返回用户，全程 kernel 无感
```

**第二步：说明 converted_params 在其中扮演的角色**（用本讲纠偏后的正确语义）：③④ 每次调用都会在宏内部 `ConvertTypes` 产出各自的 aclTensor/aclIntArray 元组；单段式在二阶段返回后立即 `ReleaseConvertTypes`；若改用两段式，则每次调用各留下一包 `OpApiParams{对象+析构器}` 交框架托管，launch 时统一执行。切分/拼接本身是**另外两个 aclnn 算子调用**，不发生在 ConvertType 里——ConvertType 只做类型桥接与（ConvertMmType 的）transpose 视图。

**第三步：触发示例**：具体输入 `x` shape = `[1024, 16, 384]`（S=1024、B=16、H=384 满足 192 对齐）、`w` = `[3, 384]`、dtype BF16。当前仓库实际行为：tiling 的 B 上限校验直接报 `GRAPH_FAILED`（拒绝），**不会**走上述 fallback——因为该算子根本没有 fallback 接线。

**第四步：审计验证**（呼应 4.1.4）：重跑三条 grep，确认「零调用者」结论依旧成立；再检查 [common/CMakeLists.txt:L55-L56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L55-L56) 确认 fallback 未编入任何目标。最终交付：一图、一段 converted_params 说明、一个触发示例、一份「识别算子是否具备 fallback 能力」的三步判据（① 源码 grep include fallback 头；② grep EXEC_OPAPI 宏；③ 查 CMake 是否有包含它的编译目标）。

**注意**：第一步的切分/拼接方案是**示例代码级的设计草图**，仓库中不存在对应实现；若真要实现，还需处理 mask 语义、非连续切片的 ConvertType 连续性假设（4.2 练习 1）等细节，并真机验证（待本地验证）。

## 6. 本讲小结

- fallback 组件解决「kernel 约束锥外的输入」：在 aclnn/Host 层用已有 aclnn 算子**拆分组合**出等价计算，kernel 无感；公共骨架（按名调用、类型转换、生命周期）被固化成头文件模板。
- 按名调用的底座是三级符号解析瀑布：`libcust_opapi.so` → `libopapi.so` → 6 个 aclnn 细分库，配合 dlopen/dlsym 与 `static` 进程级缓存；与 torch 扩展层的 `EXEC_NPU_CMD_V1` 是同一模式的镜像。
- 两种执行形态：单段式 `EXEC_OPAPI_CMD` 一步做完两段 aclnn 调用（自管 workspace/stream）；两段式 `EXEC_OPAPI_PREPARE_CMD` 只做准备，把 `OpApiParams{函数指针+executor+参数+析构器}` 交 `host_api_ctx` 托管，执行延迟给通用回调 `ExecuteOpLaunch`——「两级降级」在代码里的真实落点是「两段式」。
- **纠偏**：`converted_params` 记录的是类型转换产出的 acl 对象及其析构器（资源持有清单），不是 cast/pad/transpose 的变换日志；唯一的显式元数据变换是 `ConvertMmType` 的 transpose 视图（交换末两维 strides/viewShape，零拷贝）与 NZ 格式透传。
- 仓库里有四个同名 fallback，务必分清：本讲组件、CANN 内置 op_fallback（`ToAclDataType`/`ExecuteOpLaunch` 的实现方，待确认）、tiling 责任链回退、pioneer UT 注释的分支兜底。
- **硬结论**：全仓库零调用者、未编入任何构建目标——fallback 属「已备而未接线」，与 stub、tiling_sink 同类；鉴定方法是用 grep 而非看目录。另有两个阅读陷阱：宏内 `static auto ret` 的重入/线程隐患、`GetConvertType` 未知 dtype 静默降级为 fp16。

## 7. 下一步学习建议

- 下一讲 u9-l4 是全手册收官的综合实战「新增一个自定义训练算子的完整流程」：把 u1–u9 的四层结构、tiling、kernel、UT、torch 适配全部串起来，届时可回头评估「你的新算子要不要预留 fallback 入口」。
- 想看 fallback 真正接线后的样子，可去 CANN 官方算子仓（ ascend/cann-ops 仓库）搜索 `EXEC_OPAPI_CMD`/`EXEC_OPAPI_PREPARE_CMD` 的真实调用点，观察成熟算子如何写拆分组合策略（外部仓库，待确认）。
- 结合本仓库纵向对照：重读 u6-l2 的 `EXEC_NPU_CMD_V1`（torch 语境同构实现）与 u9-l2 的 tiling_sink（另一种「host 职责转移」思路），体会「同一问题在不同执行语境下的三种解法」。
- 延伸阅读 [common/include/fallback/fallback_comm_2stages.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/fallback/fallback_comm_2stages.h#L1-L56) 注释中「定义在算子仓、由算子感知、GE 框架不感知」的设计表述，思考它与 u4-l9 metadata「生产者消费者各持一份镜像定义」在跨模块契约上的共通风险。
