# 端到端调用链复盘与 wheel 打包

> 所属单元：u3 PyTorch 集成与端到端调用链
> 前置讲义：u3-l1（csrc 适配层）、u3-l2（ops_common 与 EXEC_NPU_CMD_V1）、u3-l3（converter 与图模式）

## 1. 本讲目标

学完本讲，你应该能够：

1. **按顺序说出**从 `torch.ops.custom.npu_lower_triangular_inverse(x)` 这一行 Python 代码出发，到昇腾 NPU 上 AscendC kernel 真正执行，中间经过的 **6 层边界**，以及每一层所在的文件与函数名。
2. **画出 run 包（aclnn / op_host / op_kernel）与 wheel 包（csrc / converter）的职责边界图**，说清楚「谁负责声明、谁负责计算、谁负责桥接」。
3. **掌握分层排查方法**：当遇到「算子调不到」「aclnn 符号找不到」类报错时，知道按什么顺序检查哪 5 个环节，而不是盲目重装环境。
4. **看懂 setup.py 如何把 csrc 与 converter 打成 wheel 包**，理解两条 glob 规则为什么能让新增算子「零改动打包」。

本讲是第 3 单元的收官：前三讲分别讲了 csrc 注册（u3-l1）、ops_common 桥接（u3-l2）、converter 图模式（u3-l3），本讲把它们与第 2 单元讲的 AscendC 三层结构首尾贯通，形成一张完整地图。

## 2. 前置知识

本讲默认你已读过前置讲义，这里只做最简回顾：

- **两包协作**：`inference/ascendc` 产出两样东西。`src/` 下的 AscendC 算子库编译成 **CANN run 包**（安装到 `opp/vendors/` 下，含 `libcust_opapi.so` 等三个动态库），是「发动机」；`torch_ops_extension/` 打成 **omni_custom_ops wheel 包**（Python 包，含 C++ 扩展 `custom_ops_lib` 与 converter），是「方向盘」。必须**先装 run 包、后装 wheel 包**。
- **aclnn 两段式**（u2-l2）：aclnn 接口分两段——`aclnnXxxGetWorkspaceSize` 在 Host 侧同步做参数检查并组装 `aclOpExecutor`；`aclnnXxx` 执行段携 workspace、executor、stream 异步下发。
- **Tiling 施工图**（u2-l3）：op_host 的 tiling 类在 Host 侧算出 `TilingData`（切分参数、核数、workspace 尺寸），序列化后随任务下发，kernel 侧用 `GET_TILING_DATA` 解包。
- **EXEC_NPU_CMD_V1**（u3-l2）：csrc 层的总装宏——运行期 `dlopen/dlsym` 找到 aclnn 符号，先同步调 GetWorkspaceSize，再经 `RunOpApiV2` 异步执行。
- **torch 调度键**（u3-l1）：`TORCH_LIBRARY_IMPL(custom, PrivateUse1, ...)` 挂 NPU 真算，`Meta` 挂形状推导；图模式还需 converter（u3-l3）。

一个形象的比喻贯穿本讲：**你按下方向盘上的按钮（Python 调用），方向盘把指令翻译成标准协议（csrc → aclnn），发动机控制单元查图纸决定怎么切分（OpDef + Tiling），最后气缸做功（kernel 在 AICore 上执行）**。

## 3. 本讲源码地图

本讲以 `npu_lower_triangular_inverse`（下三角矩阵求逆，配套 Delta Rule 线性注意力）为贯穿案例，涉及文件按调用顺序排列：

| 顺序 | 文件 | 所属包/层 | 职责 |
|---|---|---|---|
| 1 | `ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp` | wheel / 注册 | `m.def` 声明 `npu_lower_triangular_inverse` 签名 |
| 2 | `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp` | wheel / csrc | PrivateUse1 与 Meta 两个实现 |
| 3 | `ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h` | wheel / 适配 | `GetOpApiFuncAddr` 符号查找 + `EXEC_NPU_CMD_V1` 宏 |
| 4 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp` | run 包 / op_api | aclnn 两段式接口 |
| 5 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/ai_infra_lower_triangular_inverse.cpp` | run 包 / op_api | l0op 封装：登记进 launcher list |
| 6 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp` | run 包 / op_host | OpDef 原型注册 |
| 7 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_tiling.cpp` | run 包 / op_host | tiling 函数注册与平台信息解析 |
| 8 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_base_tiling.cpp` | run 包 / op_host | 具体 tiling 计算（DoOpTiling/PostTiling） |
| 9 | `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_kernel/ai_infra_lower_triangular_inverse.cpp` | run 包 / op_kernel | AscendC kernel 入口 |
| 10 | `ascendc/torch_ops_extension/setup.py` | wheel / 打包 | 源码收集与 wheel 构建 |
| 11 | `ascendc/torch_ops_extension/omni_custom_ops/__init__.py` | wheel / 挂载 | import 副作用：注册 + 镜像到 torch_npu |
| 12 | `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py` | wheel / converter | torchair 图模式适配 |

## 4. 核心概念与源码讲解

### 4.1 调用链复盘：六层边界一张图

#### 4.1.1 概念说明

「端到端调用链」指从 Python 一行算子调用出发，到 NPU 上 kernel 执行完毕的完整路径。它横跨**两个安装包、两种语言（Python/C++）、两个世界（Host/Device）**，共 6 层边界：

| 层 | 名称 | 所在包 | 物理载体 | 关键动作 |
|---|---|---|---|---|
| L1 | Python 调用层 | wheel | `torch.ops.custom` 命名空间 | 按 schema 查调度表 |
| L2 | csrc 实现层 | wheel | `custom_ops_lib.so` | 构造输出张量、发起 EXEC_NPU_CMD_V1 |
| L3 | ops_common 适配层 | wheel | 同上 | dlsym 找符号 → 同步 GetWorkspaceSize → 异步下发 |
| L4 | aclnn 接口层 | run 包 | `libcust_opapi.so` | 参数检查、组装 executor、登记算子 |
| L5 | op_host 层 | run 包 | `cust_opsproto_rt2.0.so` / `cust_opmaster_rt2.0.so` | 查 OpDef 原型、算 TilingData 施工图 |
| L6 | op_kernel 层 | run 包 | run 包内 kernel 二进制 | AscendC 代码在 AICore 上执行 |

为什么要分层？因为每一层解决不同的「阻抗」：L1~L3 解决 **PyTorch 世界与 ACL 世界的语言与类型差异**；L4 解决 **接口规范化**（统一的 aclnn 两段式契约）；L5 解决 **「怎么算」的计划问题**（形状相关，必须在 Host 侧做）；L6 解决 **「去算」的执行问题**（数据相关，必须在 Device 侧做）。

#### 4.1.2 核心流程

以 `y = torch.ops.custom.npu_lower_triangular_inverse(x)` 为例（x 是 5 维 FP32 张量，最后两维为 32/64/128/256 的方阵）：

```text
L1  torch.ops.custom.npu_lower_triangular_inverse(x)
      │  torch dispatcher 按 schema「npu_lower_triangular_inverse(Tensor x) -> Tensor」
      │  与调度键 PrivateUse1（NPU 设备）路由
      ▼
L2  custom::npu_lower_triangular_inverse(x)            # csrc/lower_triangular_inverse.cpp
      │  TORCH_CHECK 维度 == 5；at::empty_like 分配输出
      │  EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, x, result)
      ▼
L3  EXEC_NPU_CMD_V1 宏展开                              # csrc_base/ops_common.h
      │  ① GetOpApiFuncAddr 解析 "aclnnAiInfraLowerTriangularInverseGetWorkspaceSize"
      │     与 "aclnnAiInfraLowerTriangularInverse" 两个符号（dlopen/dlsym）
      │  ② ConvertTypes 把 at::Tensor 桥接为 aclTensor*
      │  ③ 同步调 GetWorkspaceSize 段 → 回填 workspaceSize + executor
      │  ④ 按 workspaceSize 分配 NPU workspace
      │  ⑤ OpCommand::RunOpApiV2 异步下发执行段
      ▼
L4  aclnnAiInfraLowerTriangularInverseGetWorkspaceSize  # op_api/aclnn_*.cpp
      │  CREATE_EXECUTOR → CheckParams（空指针/维度/shape/dtype）
      │  l0op::Contiguous(x) 连续化
      │  l0op::AiInfraLowerTriangularInverse(...)：
      │     INFER_SHAPE 查原型 → ADD_TO_LAUNCHER_LIST_AICORE 登记算子
      │  l0op::ViewCopy 把结果拷回 y
      ▼  （执行段：CommonOpExecutorRun 按 stream 下发，运行时接管）
L5  运行时按算子名 "AiInfraLowerTriangularInverse" 查 op_host 注册表
      │  OP_ADD 注册的 OpDef：校验原型（输入 x FLOAT/ND、输出 y）
      │  IMPL_OP_OPTILING 注册的 TilingFunc：
      │     TilingPrepare（解析平台信息→CompileInfo）
      │     → LowerTriangularInverseTilingFunc → DoTilingImpl
      │     → DoOpTiling（matmul tiling、blockDim）→ PostTiling（SaveToBuffer）
      ▼
L6  ai_infra_lower_triangular_inverse kernel            # op_kernel/ai_infra_*.cpp
      │  GET_TILING_DATA 解包施工图
      │  TILING_KEY_IS(0) 匹配分支
      │  LowerTriangularMatrixInversion<float32_t> 在 AIC 上求逆
      ▼
    结果写回 y 的 GM 地址，Host 侧按 stream 同步取回
```

关键点：**L3→L4 的边界是「符号查找」**（跨包边界，靠 dlsym 缝合）；**L4→L5 的边界是「登记与执行分离」**（aclnn 只登记不执行，执行时运行时才查 op_host 注册表）；**L5→L6 的边界是「Host/Device 分界」**（TilingData 序列化下发，GET_TILING_DATA 反序列化）。

#### 4.1.3 源码精读

**L1：签名的源头。** Python 侧能调用 `torch.ops.custom.npu_lower_triangular_inverse`，前提是签名已在 `TORCH_LIBRARY_FRAGMENT(custom)` 中声明：

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:L53-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L53-L53)

这一行声明了 `npu_lower_triangular_inverse(Tensor x) -> Tensor`——单输入单输出、无属性，是全仓库最简单的签名之一。torch dispatcher 拿到调用后，按张量所在设备（NPU → PrivateUse1 键）路由到 L2。

**L2：csrc 实现。**

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:L20-L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L20-L33)

`npu_lower_triangular_inverse` 做三件事：`TORCH_CHECK` 校验 5 维；`at::empty_like(x)` 分配输出（形状与输入一致——求逆不改变形状）；发起 `EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, x, result)`。注意宏的第一个参数**不是字符串**而是标识符——宏内部用 `#aclnn_api` 把它字符串化后去 dlsym。`npu_lower_triangular_inverse_meta` 是 Meta 键实现：只做同样的 `empty_like` 推形状，不碰 NPU，服务图编译期。

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:L37-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L37-L43)

这两个 `TORCH_LIBRARY_IMPL` 把 L2 的两个函数分别挂到 PrivateUse1 与 Meta 调度键上——这就是 u3-l1 讲的「先定义（ops_def_registration）、后实现（算子 csrc）」两步的后一半。

**L3：EXEC_NPU_CMD_V1 宏。**

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L1276-L1287](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L1276-L1287)

宏开头用 `GetOpApiFuncAddr` 解析出两个关键符号：`"aclnnAiInfraLowerTriangularInverseGetWorkspaceSize"`（注意是 `#aclnn_api "GetWorkspaceSize"` 字符串拼接）与 `"aclnnAiInfraLowerTriangularInverse"` 本体。若任一为空，紧随其后的 `TORCH_CHECK` 抛出报错——**「符号找不到」类错误正是从这里冒出来的**（详见 4.3 节）。

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L1305-L1329](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L1305-L1329)

这是宏的执行主干：`ConvertTypes` 把 `x`、`result` 及 `workspace_size_addr`、`executor_addr` 一起打包成 tuple（两段共用一份参数）；`call(getWorkspaceSizeFunc, ...)` 同步调第一段，回填 workspace 尺寸与 executor；若尺寸非零则 `at::empty` 分配 NPU workspace；最后构造 `acl_call` 闭包交给 `OpCommand::RunOpApiV2` 在 stream 上异步执行。闭包内先调执行段函数 `opApiFunc(workspace_addr, workspace_size, executor, acl_stream)`，再 `ReleaseConvertTypes` 统一销毁 aclTensor 描述符——Release 必须晚于执行段下发，否则悬垂指针（u3-l2 讲过）。

**L4：aclnn 两段式。**

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp:L63-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp#L63-L81)

`CheckParams` 是第 2 单元讲过的参数三步检查在本算子上的实例：空指针 → 维度（X/Y 都限 5 维）→ shape（末两维必须为 32/64/128/256 的方阵，见 `CheckInputOutShape`）→ dtype（仅 FLOAT）。任何一步失败返回 `ACLNN_ERR_PARAM_INVALID`，第一段即失败，不会走到执行段。

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp:L83-L101](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp#L83-L101)

`CommonProcess` 串起 L4 的核心动作：`l0op::Contiguous(x)` 把非连续输入拷成连续（占 workspace）；`l0op::AiInfraLowerTriangularInverse(x, y, executor)` 登记算子；`l0op::ViewCopy(out, y, executor)` 把算子输出拷回调用方给的 y。l0op 封装的实现在无 aclnn 前缀的同名文件中：

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/ai_infra_lower_triangular_inverse.cpp:L25-L39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/ai_infra_lower_triangular_inverse.cpp#L25-L39)

`INFER_SHAPE(AiInfraLowerTriangularInverse, ...)` 触发输出形状推导；`ADD_TO_LAUNCHER_LIST_AICORE(...)` 把算子名与输入输出登记进 executor 的下发列表——**到此为止没有任何计算发生**，只是把「要执行 AiInfraLowerTriangularInverse(x)→y」这件事记在账上。

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp:L103-L121](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_api/aclnn_ai_infra_lower_triangular_inverse.cpp#L103-L121)

第一段入口 `...GetWorkspaceSize`：`CREATE_EXECUTOR` 创建独占 executor，走 CommonProcess，回填 `*workspaceSize` 并 `ReleaseTo(executor)` 交接给调用方；第二段 `aclnnAiInfraLowerTriangularInverse` 只有一行 `CommonOpExecutorRun(workspace, workspaceSize, executor, stream)`——按流异步下发账上登记的所有算子。**executor 是横跨两段的接力棒**。

**L5：OpDef 与 Tiling。** 执行段下发时，运行时按算子名查 op_host 注册表。先查原型：

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp:L18-L44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_def.cpp#L18-L44)

`OpDef` 声明输入 `x`（REQUIRED / FLOAT / ND）与输出 `y`，`ExtendCfgInfo("opFile.flag", "ai_infra_lower_triangular_inverse.cpp")` 把算子名与 kernel 入口文件关联起来；`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 声明支持的两类 SOC。`OP_ADD` 宏在库加载时静态注册进全局注册表（u2-l1）。

再算施工图。tiling 注册分两个文件——注册文件与实现文件：

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_tiling.cpp:L28-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_tiling.cpp#L28-L36)

`REGISTER_TILING_TEMPLATE("AiInfraLowerTriangularInverse", LowerTriangularInverseBaseTiling, 0)` 把 tiling 模板类按算子名登记（priority 0）；`LowerTriangularInverseTilingFunc` 只是转调 `TilingRegistry::GetInstance().DoTilingImpl(context)`——由注册表轮询已登记模板执行 u2-l3 讲的七步 DoTiling。

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_tiling.cpp:L82-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_tiling.cpp#L82-L84)

`IMPL_OP_OPTILING(AiInfraLowerTriangularInverse).Tiling(...)` 把 tiling 函数挂到算子名上，`.TilingParse<LowerTriangularInverseCompileInfo>(...)` 另挂一个编译期解析函数（把平台核数、UB/L1 尺寸等缓存进 CompileInfo，见同文件 L38-L80 的 `TilingPrepareForLowerTriangularInverse`）。

具体计算在 base_tiling：

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_base_tiling.cpp:L115-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_base_tiling.cpp#L115-L140)

`TilingProcess` 用 matmul 适配器 `mm_` 设置矩阵类型/形状并 `GetTiling` 生成 matmulTiling（本算子是「分块矩阵求逆」，底层靠 Cube 矩阵乘），算出 `tilingKey_ = 0UL` 与 workspace 尺寸。

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_base_tiling.cpp:L175-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/op_tiling/lower_triangular_inverse_base_tiling.cpp#L175-L184)

`PostTiling` 是 host 侧收官：`SaveToBuffer` 把 TilingData 序列化进框架的 RawTilingData，`SetBlockDim(coreNum)` 告诉运行时要起多少个核。这些数据随后随任务一起下发到 Device。

**L6：kernel 执行。**

[ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_kernel/ai_infra_lower_triangular_inverse.cpp:L21-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_kernel/ai_infra_lower_triangular_inverse.cpp#L21-L37)

kernel 入口的参数布局正是 u2-l4 讲的固定契约：`x`、`y` 按 OpDef 的 IO 声明顺序排列，末尾追加 `workspaceGM` 与 `tilingGM`。`GET_TILING_DATA(tilingData, tilingGM)` 在 Device 侧解包 L5 序列化下发的施工图；`TILING_KEY_IS(0UL)` 与 host 侧 `tilingKey_ = 0UL` 对号入座（数值双侧硬编码必须一致）；随后初始化 matmul 适配器 `MT mm`，构造 `LowerTriangularMatrixInversion<float32_t>` 计算对象并 `Init` + `Process` 完成求逆。注意 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 标记这是 AIC（Cube）主导的混合任务——矩阵求逆是典型的 Cube 计算而非向量计算。

#### 4.1.4 代码实践

**实践：编写调用链追踪笔记（源码阅读型，无需硬件）。**

1. **实践目标**：把 4.1.2 的流程图落成一份可复查的追踪笔记，做到「每层有文件、有函数、有行号」。

2. **操作步骤**：
   - 新建一个笔记文件（放在你自己的目录，不要放进仓库），画出六层表格；
   - 对每一层填入：层名 / 包（wheel 或 run）/ 文件路径 / 入口函数名 / 关键行号；
   - 在 L3 与 L4 的交界处，抄录 `EXEC_NPU_CMD_V1` 中 `GetOpApiFuncAddr(#aclnn_api "GetWorkspaceSize")` 拼出的完整符号字符串，并与 `aclnn_ai_infra_lower_triangular_inverse.cpp` 中导出函数名逐字符比对；
   - 在 L5 与 L6 的交界处，抄录 host 侧 `tilingKey_` 的赋值行与 kernel 侧 `TILING_KEY_IS` 的判断值，验证两者一致；
   - 核对 OpDef 的 `Input("x")`/`Output("y")` 顺序与 kernel 入口 `GM_ADDR x, GM_ADDR y` 的参数顺序一一对应。

3. **需要观察的现象**：写完后你会发现整条链上「名字」出现了三次对齐——torch schema 名（`npu_lower_triangular_inverse`）、aclnn 符号名（`aclnnAiInfraLowerTriangularInverse`）、OpDef/kernel 名（`AiInfraLowerTriangularInverse` / `ai_infra_lower_triangular_inverse`）——每层的命名约定不同但互相咬合。

4. **预期结果**：一份 6 行的追踪表，任何一行都能在 30 秒内跳转到对应源码。这是后续排查问题（4.3 节）的地图底稿。

5. 本实践为纯源码阅读，无需运行，结论可直接验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 csrc 中的宏写成 `EXEC_NPU_CMD_V1(aclnn_ai_infra_lower_triangular_inverse, x, result)`（首字母小写），会发生什么？

**答案**：宏内部会去 dlsym 符号 `"aclnn_ai_infra_lower_triangular_inverseGetWorkspaceSize"`，而 run 包导出的符号是 `aclnnAiInfraLowerTriangularInverseGetWorkspaceSize`（大写 A、驼峰），查找失败返回 nullptr，`ops_common.h:1285` 的 `TORCH_CHECK` 抛出 `... not in libopapi.so, or libopapi.sonot found.` 报错。宏参数必须与 aclnn 导出符号**逐字符一致**。

**练习 2**：本算子 aclnn 第一段里 `l0op::AiInfraLowerTriangularInverse` 返回后，计算发生了吗？`l0op::ViewCopy` 又是干什么的？

**答案**：没有发生计算。`ADD_TO_LAUNCHER_LIST_AICORE` 只是把算子登记进 executor 的下发列表，真正执行在第二段 `CommonOpExecutorRun` 异步下发之后。`ViewCopy` 登记一个拷贝任务：因为算子内部输出 `out` 是 executor 分配的临时张量，需要把它拷到调用方传入的 `y` 的地址上，调用方才能看到结果。

**练习 3**：为什么 `PostTiling` 里要调 `SetBlockDim`，而 kernel 侧不需要知道总核数？

**答案**：`SetBlockDim` 告诉运行时为这个任务起多少个核（block），运行时据此把同一个 kernel 调度到多个 AIC 上；kernel 侧通过 `GetBlockIdx()` 得到「我是第几号核」，结合 TilingData 里每核的区间划分（如 coreNum、各核处理量）领取自己的任务，所以不需要单独接收总核数参数——它已在 TilingData 与调度系统中。

### 4.2 双包协作：run 包与 wheel 包

#### 4.2.1 概念说明

整个工程的交付物是**两个包**，职责边界清晰：

| | run 包（CANN-omni_custom_ops-*.run） | wheel 包（omni_custom_ops-*.whl） |
|---|---|---|
| 构建入口 | `bash build.sh`（u1-l2） | `python3 setup.py build bdist_wheel` |
| 内容 | `libcust_opapi.so`（aclnn 接口）+ `cust_opsproto_rt2.0.so`（OpDef 原型）+ `cust_opmaster_rt2.0.so`（tiling 实现）+ kernel 二进制 | `custom_ops_lib.so`（csrc 实现）+ converter `.py` + `__init__.py` |
| 安装位置 | `opp/vendors/omni_custom_transformer/` | Python site-packages |
| 面向对象 | ACL 运行时（C 语言世界） | PyTorch 用户（Python 世界） |
| 类比 | 发动机 + 发动机控制单元 | 方向盘 + 翻译器 |

两包**唯一**的连接点是 L3 的 dlsym：wheel 在运行期按名字从 run 包的 `libcust_opapi.so` 里找 aclnn 符号。这带来两个推论：

- **安装顺序必须是先 run 后 wheel**——wheel 本身不含任何算子实现，run 包缺失时 import 不会报错（延迟到第一次调用才炸）；
- **两包版本可以独立演进**——只要 aclnn 符号名契约不变，升级 run 包不需要重打 wheel。

#### 4.2.2 核心流程

wheel 打包流程：

```text
build_and_install.sh
  ├─ rm -rf build                        # 清理历史产物
  ├─ python3 setup.py build bdist_wheel
  │    ├─ glob 收集源码：
  │    │    ① omni_custom_ops/csrc_base/*.cpp        → ops_common + 注册文件
  │    │    ② omni_custom_ops/*/*/*/csrc/*.cpp        → 各算子 csrc 实现
  │    ├─ NpuExtension 配置昇腾编译参数（ACL 头文件路径、FLOAT8 探测宏）
  │    ├─ find_packages() 收集 Python 包（converter 目录随包发布）
  │    └─ BuildExtension 编译出 custom_ops_lib.so → dist/*.whl
  └─ pip3 install *.whl --force-reinstall
```

运行期挂载流程（`import omni_custom_ops` 的副作用）：

```text
import omni_custom_ops
  ├─ import torch / torch_npu            # 前置：确保挂载目标存在
  ├─ from . import custom_ops_lib        # ① 加载 C++ 扩展：
  │      │                                 TORCH_LIBRARY_FRAGMENT 注册签名（m.def）
  │      │                                 TORCH_LIBRARY_IMPL 挂实现
  │      ▼
  ├─ import 6 个 converter 模块          # ② 注册 torchair fx2ge 转换器（图模式）
  ├─ getattr(torch.ops, 'custom')        # ③ 取得命名空间模块
  └─ for op_name in dir(...):            # ④ setattr(torch_npu, op_name, func)
                                         #    使 torch_npu.npu_xxx 写法可用
```

#### 4.2.3 源码精读

**源码收集的两条 glob 规则。**

[ascendc/torch_ops_extension/setup.py:L49-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L50)

第一条收集公共层（`ops_common.cpp`、`ops_def_registration.cpp`）；第二条的通配符 `omni_custom_ops/*/*/*/csrc` 对应 `omni_custom_ops / ops_transformer / attention / lower_triangular_inverse / csrc` 三级目录（ops-nn 算子则是 `ops_nn / matmul / xxx / csrc`）。**新增算子只要把 csrc 文件放进约定目录，无需改打包脚本**——这是 u3-l1 讲过的约定优于配置。

[ascendc/torch_ops_extension/setup.py:L53-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L53-L58)

所有源码编进**同一个** C++ 扩展 `omni_custom_ops.custom_ops_lib`——不是每个算子一个 so，而是全仓库一个 so，import 一次全部注册。`NpuExtension` 来自 `torch_npu.utils.cpp_extension`，自动带上昇腾平台的编译与链接参数；`extra_compile_args` 里附加 ACL 头文件目录与 FLOAT8 支持宏（见 L42-L47 的探测逻辑）。

[ascendc/torch_ops_extension/setup.py:L65-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L65-L70)

`package_data` 把 `*.py`、`*.so` 声明为包内数据，`find_packages()` 收集所有含 `__init__.py` 的 Python 包——converter 目录因此随 wheel 一起发布。注意 u3-l3 的结论：**converter 文件被打包 ≠ converter 已注册**，注册靠下一处 `__init__.py` 的 import。

**运行期挂载。**

[ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L17-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L17-L27)

包入口的 import 副作用：`from . import custom_ops_lib` 触发 so 加载，so 的静态初始化器执行全部 `TORCH_LIBRARY_FRAGMENT`/`TORCH_LIBRARY_IMPL` 注册——此时 `torch.ops.custom` 命名空间才真正有算子。随后显式 import 6 个 converter 模块（lower_triangular_inverse 在 L23），触发 `register_fx_node_ge_converter` 装饰器执行。u3-l3 盘点过：全仓库 7 份 converter 只有这里 import 的 6 份被激活。

[ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L31-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L31-L47)

镜像逻辑：遍历 `torch.ops.custom` 命名空间下的所有算子，`setattr(torch_npu, op_name, custom_op_func)` 挂到 torch_npu 模块上——这就是 `torch_npu.npu_lower_triangular_inverse(x)` 与 `torch.ops.custom.npu_lower_triangular_inverse(x)` 等价的原因（u1-l4）。若命名空间不存在（如 torch.ops 尚未初始化），只发 warning 降级，不中断 import。

**converter 的图模式入口（回顾）。**

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py:L29-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/converter/lower_triangular_inverse.py#L29-L43)

图模式与 eager 模式在 L2 层分叉：eager 走 PrivateUse1 实现（L3 dlsym），图模式走 converter——torchair 捕获 FX 图后调用此函数，生成名为 `LowerTriangularInverse` 的 GE 图节点（注意 GE 名与 OpDef 名又是一套命名），图执行时由 GE 运行时查 op_host 完成同样的 L4→L6 流程。**同一个算子，两条路到同一个发动机**。

**打包脚本。**

[ascendc/torch_ops_extension/build_and_install.sh:L13-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/build_and_install.sh#L13-L21)

三步：清 build 目录、`setup.py build bdist_wheel` 出包、`pip3 install *.whl --force-reinstall` 安装。产物在 `dist/` 下的 `omni_custom_ops-1.0-*.whl`（版本号 1.0 来自 setup.py L62）。

#### 4.2.4 代码实践

**实践：验证「零改动打包」约定。**

1. **实践目标**：确认 setup.py 的 glob 规则确实能覆盖一个新增的算子 csrc 文件，从而理解为什么新增算子不用改打包脚本。

2. **操作步骤**（无硬件也可完成第 1~3 步）：
   - 在本地临时目录模拟目录结构：`mkdir -p /tmp/pkg_test/omni_custom_ops/ops_transformer/index/my_op/csrc`（注意 `omni_custom_ops/*/*/*/csrc` 需要三级中间目录：`ops_transformer / index / my_op`）；
   - 放一个空的 `my_op.cpp` 进 `csrc/`，另在 `omni_custom_ops/csrc_base/` 下放一个空的 `base.cpp`；
   - 用一行 Python 验证收集结果：
     ```python
     # 示例代码：模拟 setup.py L49-L50 的 glob 行为
     import glob, os
     BASE_DIR = "/tmp/pkg_test"
     files = glob.glob(os.path.join(BASE_DIR, "omni_custom_ops/csrc_base", "*.cpp"))
     files += glob.glob(os.path.join(BASE_DIR, "omni_custom_ops/*/*/*/csrc", "*.cpp"))
     print(sorted(files))
     ```
   - 有昇腾环境时，可进一步在 `torch_ops_extension/` 下运行 `python3 setup.py build bdist_wheel`，观察编译日志中是否包含你的文件（完整流程见 u1-l4）。**待本地验证**（需要 torch/torch_npu 环境）。

3. **需要观察的现象**：glob 输出应同时包含 `csrc_base/base.cpp` 与 `ops_transformer/index/my_op/csrc/my_op.cpp`；如果把 `my_op/csrc` 挪到只有两级中间目录（如 `omni_custom_ops/index/my_op/csrc`），文件将**不会**被收集。

4. **预期结果**：两条 glob 规则的覆盖范围 = 公共层 + 恰好三级目录下的 csrc。这解释了 u3-l1 强调的「csrc 文件必须按约定目录层级放置」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `import omni_custom_ops` 之前脚本调用 `torch_npu.npu_xxx` 会报 `AttributeError`？

**答案**：`torch_npu.npu_xxx` 的挂载是 `__init__.py` L31-L41 的 setattr 循环执行的，而这段代码只有在 `import omni_custom_ops` 时才运行。pip install 只是复制文件，不会执行包入口；不 import 就没有挂载副作用。此时应改用 `torch.ops.custom.npu_xxx`（前提是 C++ 扩展已加载，而这同样需要 import）——总之**不 import 两种写法都不可用**。

**练习 2**：run 包与 wheel 包各自的「注册表」是什么？分别在什么时机生效？

**答案**：run 包有两张表：OpDef 原型表（`OP_ADD` 静态注册，`cust_opsproto_rt2.0.so` 加载时生效，供运行时查原型）与 tiling 模板表（`REGISTER_TILING_TEMPLATE` + `IMPL_OP_OPTILING`，`cust_opmaster_rt2.0.so` 加载时生效，供执行期查 tiling）。wheel 包一张表：torch 调度表（`TORCH_LIBRARY_FRAGMENT`/`IMPL`，`custom_ops_lib.so` 被 import 时生效）。三张表通过「算子名字符串」松耦合：wheel 用 aclnn 符号名找 run 包的接口，运行时用 OpDef 名找原型与 tiling。

**练习 3**：如果把某个算子的 converter 从 `__init__.py` 的 import 列表里删掉，eager 调用会受影响吗？

**答案**：不会。eager 路径只依赖 `custom_ops_lib.so` 的 PrivateUse1 实现，与 converter 无关。受影响的是 torchair 图模式：FX 图捕获到该算子时找不到转换器，会 graph break 回退到 eager 执行该节点（u3-l3），功能仍正确但失去整图优化的性能收益。

### 4.3 问题定位：「算子调不到 / 找不到符号」分层排查

#### 4.3.1 概念说明

「找不到符号」类故障的表象是运行期抛出类似这样的报错：

```text
RuntimeError: aclnnAiInfraLowerTriangularInverse or aclnnAiInfraLowerTriangularInverseGetWorkspaceSize
not in libopapi.so, or libopapi.sonot found.
```

它来自 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L1285-L1287](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L1285-L1287) 的 `TORCH_CHECK`——wheel 侧在 run 包的所有候选库里都没 dlsym 到目标符号。**报错文案提到 libopapi.so 只是兜底库的名字，根因几乎都在自定义算子库没被找到或没装上**。

分层排查的哲学：调用链有 6 层，报错只发生在其中一层，但根因可能在更早的层。按「从环境到符号」的顺序检查，每一步都有明确的命令与预期。

#### 4.3.2 核心流程

dlsym 之前，wheel 侧先要决定「去哪些目录找库」。搜索顺序由 `GetOpApiFuncAddr` 决定：

```text
GetOpApiFuncAddr("aclnnAiInfraLowerTriangularInverse")
  ① ASCEND_CUSTOM_OPP_PATH 环境变量（冒号分隔多路径）
       每个路径 + "/op_api/lib/" + libcust_opapi.so
  ② ASCEND_OPP_PATH/vendors/config.ini 的 load_priority 列表（逗号分隔）
       每个条目 + "/op_api/lib/" + libcust_opapi.so
  ③ CANN 特性库：libopapi_math / nn / cv / transformer / legacy .so
  ④ 内置 libopapi.so
  全部未命中 → nullptr → TORCH_CHECK 报错
```

据此，若 dlsym 找不到 aclnn 符号，**按顺序检查以下 5 个环节**：

| 环节 | 检查什么 | 命令示例 | 判定 |
|---|---|---|---|
| 1. run 包装没装 | vendors 下有无本仓库的库 | `ls $ASCEND_OPP_PATH/vendors/omni_custom_transformer/op_api/lib/` | 应存在 `libcust_opapi.so` |
| 2. 环境变量注入没注入 | OPP 路径变量是否设置 | `env \| grep ASCEND` 后确认 `ASCEND_OPP_PATH` 非空（装完 run 包应 source 其 `set_env.bash`） | 变量缺失 → ②③ 步全部跳过 |
| 3. load_priority 登记没登记 | config.ini 是否含本包 | `grep load_priority $ASCEND_OPP_PATH/vendors/config.ini` | 条目中应出现 `omni_custom_transformer` |
| 4. 符号导没导出 | 库里有没有这个函数 | `nm -D libcust_opapi.so \| grep LowerTriangularInverse` | 应看到 `aclnnAiInfraLowerTriangularInverse...` 两个符号；**用 `-n` 裁剪构建时未选中的算子符号本来就不在包里** |
| 5. wheel 侧拼没拼对 | csrc 宏参数与符号名比对 | 对照 csrc 的 `EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, ...)` 与 nm 输出 | 逐字符一致（大小写敏感） |

若 1~5 都通过仍报错，再补充检查：Python 侧是否 `import omni_custom_ops`（挂载缺失时 torch 报的是另一种错：`NoSuchOp` / `AttributeError`，据此可先区分大类故障）。

#### 4.3.3 源码精读

**搜索路径的构造。**

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L505-L526](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L505-L526)

`get_custom_lib_path` 读 `ASCEND_CUSTOM_OPP_PATH` 环境变量，按冒号拆成多路径，每条拼上 `/op_api/lib/`——这是优先级最高的自定义库搜索链（环节 2 的左半）。

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L528-L571](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L528-L571)

`get_default_custom_lib_path` 读 `ASCEND_OPP_PATH`，打开 `$ASCEND_OPP_PATH/vendors/config.ini`，逐行找 `load_priority=` 行，按逗号拆出 vendor 名列表，再拼成 `vendors/<name>/op_api/lib/`——这是第二优先级（环节 2 右半 + 环节 3）。run 包安装时写入的正是这个 config.ini。

**符号查找本体。**

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L578-L595](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L578-L595)

三个小函数：`GetOpApiLibName` 返回内置库名 `libopapi.so`，`GetCustOpApiLibName` 返回自定义库名 `libcust_opapi.so`，`GetOpApiFuncAddrInLib` 就是 `dlopen`（`RTLD_LAZY`）+ `dlsym` 的封装，失败时打 warning 日志（`ASCEND_LOGW`）。**dlsym 失败本身不抛错**，只是返回 nullptr 往上传播。

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L622-L660](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L622-L660)

`GetOpApiFuncAddr` 主体的前两段：先轮询 ① 的自定义路径（`g_custom_lib_path`，L624-L641），再轮询 ② 的 vendors 路径（`g_default_custom_lib_path`，L643-L660），任何一步 dlsym 命中即返回。之后还有 CANN 特性库与内置库兜底（L662 起）。自定义库**先于** CANN 内置库查找——这就是「自定义算子可以覆盖内置同名算子」的实现机制（u3-l2）。

**报错点。**

[ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h:L1285-L1287](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_common.h#L1285-L1287)

`TORCH_CHECK(getWorkspaceSizeFuncAddr != nullptr && opApiFuncAddr != nullptr, ...)`——所有搜索链走完仍为空，在这里转成 Python 可见的 `RuntimeError`。文案中的库名取自 `GetOpApiLibName()`（即 libopapi.so），**容易误导人去查 CANN 安装**；理解了搜索顺序就知道真凶是前面几层。

另一个有用的定位信息：`GetOpApiFuncAddrInLib` 的 `ASCEND_LOGW` 会在 dlsym 失败时输出 `dlsym xxx from xxx failed`——打开昇腾日志（`ASCEND_GLOBAL_LOG_LEVEL`，具体级别名待确认）可以看到每一级查找的失败记录，直接指明「走到哪一级断了」。

#### 4.3.4 代码实践

**实践：模拟排查 dlsym 失败（在有昇腾环境的机器上执行；无环境则做纸面推演）。**

1. **实践目标**：亲手制造并定位一次「符号找不到」故障，把 5 环节检查表变成肌肉记忆。

2. **操作步骤**（有环境时）：
   - **制造故障**：临时改掉 vendors 登记名——`sudo mv $ASCEND_OPP_PATH/vendors/config.ini /tmp/config.ini.bak`（或临时 `unset ASCEND_OPP_PATH` 后重开 shell）；
   - **触发报错**：运行最小脚本：
     ```python
     # 示例代码
     import torch, torch_npu, omni_custom_ops
     x = torch.randn(2, 3, 4, 64, 64).npu()
     y = torch.ops.custom.npu_lower_triangular_inverse(x)
     ```
   - **按表排查**：依次执行环节 1~5 的命令，记录每步输出；预期在环节 2 或 3 就能定位到根因；
   - **恢复现场**：`mv /tmp/config.ini.bak $ASCEND_OPP_PATH/vendors/config.ini`（或重新 source set_env），重跑脚本确认恢复；
   - 附加实验：`nm -D $ASCEND_OPP_PATH/vendors/omni_custom_transformer/op_api/lib/libcust_opapi.so | grep -i LowerTriangularInverse`，记下导出的完整符号列表。

3. **需要观察的现象**：故障脚本抛出的 RuntimeError 文案与本节开头一致；`nm -D` 输出中应能看到 `aclnnAiInfraLowerTriangularInverseGetWorkspaceSize` 与 `aclnnAiInfraLowerTriangularInverse` 两条 T 型导出符号；恢复后脚本正常返回与 x 同形状的结果。

4. **预期结果**：故障可在环节 2/3 定位（环境变量或 load_priority 缺失）；若你用的是 `build.sh -n 'ai_infra_lower_triangular_inverse'` 裁剪包，环节 4 会看到「只有部分算子符号」——这解释了为什么裁剪包调不了未编译的算子。

5. **待本地验证**：本实践需要昇腾硬件与已安装的 run/wheel 包；无环境时请完成纸面推演——对 5 个环节各写一句「如果这里断了，报错/日志会是什么样」。

#### 4.3.5 小练习与答案

**练习 1**：报错文案说 `not in libopapi.so`，为什么不建议第一时间去查 CANN 的 libopapi.so？

**答案**：因为 `GetOpApiFuncAddr` 的搜索顺序是「自定义库（ASCEND_CUSTOM_OPP_PATH / vendors load_priority）→ CANN 特性库 → 内置 libopapi.so」，自定义算子符号只可能在前两级命中；libopapi.so 是最后兜底的内置库，文案只是用它的名字泛指「所有候选库都没找到」。第一时间应查 run 包安装与环境变量。

**练习 2**：同事用 `bash build.sh -n 'ai_infra_scatter_block_update'` 打了个只含一个算子的裁剪 run 包，然后抱怨 `npu_lower_triangular_inverse` 调不通。问题出在哪个环节？

**答案**：环节 4。`-n` 参数让 CMake 只编译指定算子（u1-l2），生成的 `libcust_opapi.so` 里只有 ScatterBlockUpdate 的 aclnn 符号，没有 `aclnnAiInfraLowerTriangularInverse*`。环境变量、load_priority、csrc 拼写都没问题，`nm -D` 一看便知。解决：用 `-n` 传入多个算子名或不带 `-n` 全量构建。

**练习 3**：如何区分「符号找不到」与「算子未挂载」两类故障？

**答案**：看报错位置。「算子未挂载」在 Python 层就失败：`torch.ops.custom.npu_xxx` 报 `NoSuchOp`（签名未注册，wheel 没装或没 import omni_custom_ops）、`torch_npu.npu_xxx` 报 `AttributeError`（镜像未挂载，没 import 包）。「符号找不到」是 `RuntimeError` 且文案含 aclnn 名字与库名（TORCH_CHECK 抛出）——说明 L1/L2 已通、断在 L3→L4 边界，此时才用 5 环节表排查。

## 5. 综合实践

**综合任务：制作你自己的《npu_lower_triangular_inverse 全链路排查手册》。**

把本讲三个模块串成一份可交付的文档，包含三部分：

1. **追踪表**（4.1.4 的成果）：六层边界表，每层标注包名、文件、函数、行号与永久链接；额外补一列「该层典型报错」，例如 L2 的 `TORCH_CHECK(x.dim() == 5, ...)` 维度错、L4 的 `ACLNN_ERR_PARAM_INVALID` shape 错（末两维不在 32/64/128/256 之列）。
2. **双包图**（4.2）：一张 run 包 / wheel 包对照图，标出唯一的 dlsym 连接点，以及「先 run 后 wheel」的安装顺序约束和三条命名对齐关系（torch schema 名 / aclnn 符号名 / OpDef 与 kernel 名）。
3. **排查剧本**（4.3）：5 环节检查表 + 每环节一条命令 + 每环节一句预期输出，末尾附「报错分类速查」：NoSuchOp / AttributeError / RuntimeError(符号) / ACLNN_ERR_PARAM_INVALID 分别指向哪一层。

验收标准：拿着这份手册，一个没读过源码的同事遇到任意一类故障，能在 10 分钟内定位到层。全部内容可离线完成（源码阅读型），有环境时可补充 4.3.4 的实测记录。

## 6. 本讲小结

- **六层边界**：Python 调用层（torch.ops.custom / torch_npu 镜像）→ csrc 实现层（PrivateUse1/Meta）→ ops_common 适配层（EXEC_NPU_CMD_V1）→ aclnn 接口层（两段式 + executor 接力）→ op_host 层（OpDef 查表 + Tiling 施工图）→ op_kernel 层（GET_TILING_DATA + TILING_KEY_IS + Process）；前 3 层在 wheel 包，后 3 层在 run 包。
- **两包缝合点唯一**：L3 的 `dlopen/dlsym`——搜索顺序为 `ASCEND_CUSTOM_OPP_PATH` → vendors `load_priority` → CANN 特性库 → 内置 libopapi.so，自定义库优先于内置库；因此必须先装 run 包后装 wheel 包，且两包可独立升级。
- **wheel 打包零改动约定**：setup.py 用两条 glob（`csrc_base/*.cpp` 与 `omni_custom_ops/*/*/*/csrc/*.cpp`）收集源码编进单一 `custom_ops_lib.so`；新增算子只需按目录约定放文件。
- **挂载靠 import 副作用**：`__init__.py` 加载 so（注册签名与实现）、import 6 个 converter（激活图模式）、setattr 镜像到 torch_npu——不 import 一切皆无。
- **分层排查方法论**：先按报错类型分类（NoSuchOp → L1、AttributeError → 挂载、RuntimeError 含 aclnn → L3/L4、PARAM_INVALID → L4 检查），符号类故障再按 5 环节表从环境到符号逐层收敛；`nm -D` 与昇腾 warning 日志是两大利器。
- **命名三次对齐**：torch schema 名、aclnn 符号名（宏参数逐字符匹配）、OpDef/kernel 名（`tilingKey_` 与 `TILING_KEY_IS` 数值对齐）——任何一处拼写不一致都会静默或显式失败。

## 7. 下一步学习建议

本讲完成了第 3 单元（PyTorch 集成）的收官，你已具备全链路视野。接下来两条路：

1. **横向深入算子族（推荐先走）**：进入第 4 单元 u4-l4《因果卷积与 Delta Rule 递推》，那里会用到本讲的 npu_lower_triangular_inverse 的「雇主」——`ai_infra_chunk_gated_delta_rule_recurrence`（分块门控递推），你将看到 lower_triangular_inverse 在完整线性注意力流水线中被消费的方式；随后可挑战旗舰算子 u4-l1 FusedInferAttentionSink。
2. **纵向深入公共机制**：若你对 L5 的 tiling 注册表轮询与多模板机制感兴趣，可直接跳到 u5-l1《公共 Tiling 框架深入》，本讲 L5 只展示了 priority 0 的单模板（`REGISTER_TILING_TEMPLATE(..., 0)`），那里讲多模板竞争与 TilingKey 编码体系。

无论走哪条路，建议随身携带本讲综合实践产出的排查手册——第 4 单元精读大型算子时，你会频繁需要「从现象跳回某一层源码」的能力。
