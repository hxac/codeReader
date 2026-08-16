# common 公共库与 fallback 机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `common/` 目录的分层结构（include 按消费者分层 + src 按产物分层），以及它在 CMake 中被编成哪几个库目标。
2. 知道 `common/include` 下每一层（op_host / op_api / op_kernel / op_graph / err / util / fallback / framework / tiling_sink 等）给谁用、放什么东西。
3. 理解 **fallback 机制**：mc2 通信类算子在图模式下，如何通过 `IMPL_OP(...).OpExecuteFunc(...)` 注册的 host 侧执行函数，在运行期 `dlsym` 找到 aclnn 接口，把图执行「回退」到 eager 两段式调用上。
4. 学会在自己开发的算子（如第二单元改造过的 add_example）中正确复用 common 头文件（错误码上报、tiling 工具等），并保持编译通过。

## 2. 前置知识

本讲需要以下背景（前三单元已建立，这里仅做一句话复习）：

- **Host 侧与 Device 侧**：算子代码分两层，op_host 跑在 CPU 上做校验/推导/切分，op_kernel 跑在 NPU 上做计算。
- **aclnn 两段式 API**：`aclnnXxxGetWorkspaceSize`（第一段，校验 + tiling + 算 workspace）和 `aclnnXxx`（第二段，下发任务），详见上一讲 u3-l1。
- **图模式（GE）**：算子以 `REG_OP` 声明的 IR 原型组图执行，调用方看不到 aclnn 接口，详见 u2-l4。
- **动态库符号查找**：`dlopen` 打开一个 `.so`，`dlsym` 按名字找函数地址。这是理解 fallback 的关键 —— C++ 没有「按字符串调函数」的原生能力，fallback 用 `dlsym` 实现了它。
- **OBJECT 库**：CMake 的 `add_library(x OBJECT)` 只编译出 `.o` 文件，由最终目标「拼装」进自己的 `.so`。common 的公共实现就是以 OBJECT 库形式被各算子库吸纳的。

一个形象的比喻：如果把每个算子目录看作一间「作坊」，`common/` 就是全仓库共享的「工具房」—— 里面有量具（tiling 工具）、报警器（错误码上报）、转换插座（tensor 类型转换），还有一台特殊的「备用发电机」（fallback：图执行不通时临时改用 eager 接口供电）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [common/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/CMakeLists.txt) | common 的构建定义：头文件接口库、公共 obj、fallback obj、onnx 插件、tiling_sink 子工程 |
| [common/include/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include) 各子目录 | 公共头文件，按消费者分层（op_host / op_api / op_kernel / op_graph / err / util / fallback / framework / tiling_sink 等） |
| [common/src/op_host/tiling_util.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/op_host/tiling_util.cpp) | host 侧公共 tiling 工具的实现（`EnsureNotScalar`、`IsRegbaseSocVersion` 等） |
| [common/include/err/ops_err.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/err/ops_err.h) | 统一错误码上报宏（E89999 / E69999） |
| [common/include/fallback/fallback.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h) | fallback 核心：符号查找、类型转换、`EXEC_OPAPI_CMD` 宏 |
| [common/include/fallback/fallback_2stages.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback_2stages.h) | 两阶段版 fallback：`EXEC_OPAPI_PREPARE_CMD` 宏 |
| [common/src/fallback/fallback_comm.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/fallback/fallback_comm.cpp) | `ToAclDataType` 等 GE dtype → ACL dtype 转换实现 |
| [common/src/framework/multi_head_attention_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp) | ONNX 插件示例：解析 NPUMultiHeadAttention 节点属性并注册到 GE |
| [mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp) | fallback 的典型消费者：图模式下回退调用 aclnnMatmulAllReduce 系列 |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) | 教学算子 tiling，已经在复用 common 的 `EnsureNotScalar` 和 `GET_TPL_TILING_KEY` |
| [ffn/ffn/op_host/ffn_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp) | 工业级算子复用 `err/ops_err.h` 错误上报的实例 |

## 4. 核心概念与源码讲解

### 4.1 common 公共库：大型算子库的「工具房」

#### 4.1.1 概念说明

ops-transformer 有数百个算子，如果每个算子都自己写一遍「shape 为标量时转成 `{1}`」「dtype 上报错误码」「按平台判断是否 Regbase 芯片」这类逻辑，仓库会充满重复代码且难以统一修复。`common/` 就是抽取这些跨算子共性得到的公共库。

它的组织遵循两条正交的轴线：

1. **include 按「消费者」分层**：头文件放在哪个子目录，取决于它给算子的哪一层用 —— `op_host/` 给 tiling/infershape 用，`op_api/` 给 aclnn 接口层用，`op_kernel/` 给 AscendC 核函数用，`op_graph/` 给图模式注册用。
2. **src 按「产物」分层**：实现代码放在哪个子目录，取决于它被编进哪个库 —— `src/op_host/` 进公共 obj，`src/fallback/` 进 fallback obj，`src/framework/` 编 onnx 插件，`src/tiling_sink/` 是独立子工程。

#### 4.1.2 核心流程

common 被使用的整体链路：

```text
build.sh 触发 cmake
   └─ common/CMakeLists.txt
        ├─ ops_transformer_utils_tiling_headers / _proto_headers   (INTERFACE 库，只导出 common/include 头文件搜索路径)
        ├─ ${COMMON_NAME}_obj            (OBJECT 库，编 src/op_host/*.cpp)
        ├─ ${COMMON_NAME}_fallback_obj   (OBJECT 库，编 src/fallback/*.cpp)
        ├─ add_subdirectory(src/framework)  (onnx 插件源)
        └─ ENABLE_TILING_SINK 时以 ExternalProject 编 src/tiling_sink

各算子的 op_host / op_graph 目标
   └─ 链接 ${COMMON_NAME}_obj / ${COMMON_NAME}_fallback_obj 的 .o
      并把 common/include 加入头文件搜索路径
   └─ 算子源码 #include "op_host/tiling_util.h" / "err/ops_err.h" / "fallback/fallback.h" ...
```

要点：

- `COMMON_NAME` 在 [cmake/variables.cmake:11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake#L11) 定义为 `common_${PKG_NAME}`，即库名带上包名前缀。
- 算子目标对 common obj 的吸纳发生在 [cmake/custom_build.cmake:704-712](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/custom_build.cmake#L704-L712)，用的是 `$<TARGET_EXISTS:...>` 守卫——common 存在才链，因此裁剪构建时不会因缺目标而报错。

#### 4.1.3 源码精读

**（1）CMake 侧：三个层次的产物**

[common/CMakeLists.txt:19-35](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/CMakeLists.txt#L19-L35) 建立了两个 INTERFACE（接口）库，本身不产出代码，只把 `common/include` 和 CANN 实验性头文件路径「广播」给所有链接者，同时统一定义日志子模块名（`OP_TILING` / `OP_PROTO`）：

```cmake
add_library(ops_transformer_utils_tiling_headers INTERFACE)
target_include_directories(ops_transformer_utils_tiling_headers INTERFACE
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include> ...)
```

[common/CMakeLists.txt:52-108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/CMakeLists.txt#L52-L108) 用 `GLOB` 把 `src/op_host/*.cpp` 编进 `${COMMON_NAME}_obj`、把 `src/fallback/*.cpp` 编进 `${COMMON_NAME}_fallback_obj`，两个都是 OBJECT 库；随后 [第 110 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/CMakeLists.txt#L110) `add_subdirectory(src/framework)` 把 onnx 插件源（含本讲会用到的 multi_head_attention 插件）交给框架层构建。tiling_sink 则在 [第 135-161 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/CMakeLists.txt#L135-L161) 以 `ExternalProject_Add` 独立成子工程（用 CANN toolchain 交叉编译，说明它是要下放到 device 侧运行的代码）。

**（2）include 分层：给谁用什么**

| 子目录 | 消费者 | 代表内容 |
| --- | --- | --- |
| `include/op_host/` | tiling / infershape | `tiling_util.h`、`tiling_base.h`、`tiling_templates_registry.h`、`tiling_key.h`、`data_copy_transpose_tiling.h` |
| `include/op_api/` | aclnn 接口层 | `op_api_def.h`（维度上限、dtype 约定常量） |
| `include/op_kernel/` | AscendC 核函数 | `mma.h`、`simd.h`、`layout.h`、各种 `*_iterator.h`（GM/UB/L1/L0C 迭代器）、`common_func.h` 等，合计约 2500 行设备侧工具 |
| `include/op_graph/` | 图模式注册 | `op_transformer_proto_extend.h`（如 MaskedSoftmaxWithRelPosBias 的 `REG_OP` 声明） |
| `include/err/` | 所有 host 侧代码 | `ops_err.h` 错误码上报宏 |
| `include/util/` | op_api 层 | `tensor_util.h`（Resize/广播辅助） |
| `include/fallback/` | mc2 算子 op_graph | `fallback.h`、`fallback_2stages.h`、`fallback_comm.h` |
| `include/framework/` | onnx 插件 | `onnx_common.h` |
| `include/tiling_sink/` | tiling sink 机制 | `tiling_aicpu_task.h` 等 |
| `include/common/`、`include/static/`、`include/external/` | 杂项公共 | `aicpu_op_def.h`、`static_space.h`、外部 aclnn 头适配层 |

**（3）可复用工具举例一：`EnsureNotScalar`（tiling 工具）**

[common/include/op_host/tiling_util.h:20-30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/op_host/tiling_util.h#L20-L30) 声明了三个小而常用的工具：

```cpp
bool IsRegbaseSocVersion(const gert::TilingParseContext *context);
bool IsRegbaseSocVersion(const gert::TilingContext *context);
const gert::Shape &EnsureNotScalar(const gert::Shape &inShape);
```

`EnsureNotScalar` 的语义是：标量输入在 CANN 内部表示为 0 维 shape，很多 tiling 逻辑按「至少 1 维」写，所以统一把空 shape 归一成 `{1}`。它的实现在 [common/src/op_host/tiling_util.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/op_host/tiling_util.cpp)（该文件正是 `${COMMON_NAME}_obj` GLOB 进来的实现源）。

而教学算子 add_example 已经在用它 —— [examples/add_example/op_host/add_example_tiling.cpp:16-21](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L16-L21) 的 include 列表里就有两个 common 头：

```cpp
#include "log/log.h"
#include "util/math_util.h"
#include "op_host/tiling_util.h"              // ← common 公共库
#include "op_host/tiling_templates_registry.h" // ← common 公共库
```

[第 59-66 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L59-L66) 取输入 shape 时对 x、y、z 三次调用 `EnsureNotScalar`；[第 128-135 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L128-L135) 设置 tiling key 时用的 `GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0)` 宏同样来自 common 的 `tiling_templates_registry.h`（其中定义了按优先级注册多个 tiling 实现类的 `TilingCases`，见 [common/include/op_host/tiling_templates_registry.h:34-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/op_host/tiling_templates_registry.h#L34-L60)）。**也就是说：你在第二单元编译 add_example 时，就已经在无感地复用 common 库了。**

**（4）可复用工具举例二：统一错误上报（err 层）**

[common/include/err/ops_err.h:21-33](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/err/ops_err.h#L21-L33) 定义了仓库统一的内部错误上报宏：

```cpp
constexpr int32_t OP_TRANSFORMER_MODULE_ID = 63;

#define OPS_INNER_ERR_STUB(ERR_CODE_STR, OPS_DESC, FMT, ...) \
    do {                                                     \
        OpLogSub(OP_TRANSFORMER_MODULE_ID, DLOG_ERROR, OPS_DESC, FMT, ##__VA_ARGS__); \
        REPORT_INNER_ERR_MSG(ERR_CODE_STR, FMT, ##__VA_ARGS__);                         \
    } while (0)

#define OPS_REPORT_VECTOR_INNER_ERR(OPS_DESC, ...) OPS_INNER_ERR_STUB("E89999", OPS_DESC, __VA_ARGS__)
#define OPS_REPORT_CUBE_INNER_ERR(OPS_DESC, ...)   OPS_INNER_ERR_STUB("E69999", OPS_DESC, __VA_ARGS__)
```

它做两件事：打一条带模块号 63 的 ERROR 日志，同时向 CANN 错误上报框架登记错误码（向量类算子用 E89999，矩阵乘类用 E69999）。工业级算子的用法见 [ffn/ffn/op_host/ffn_tiling.cpp:413-418](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/ffn/ffn/op_host/ffn_tiling.cpp#L413-L418)：

```cpp
OP_CHECK_IF(xDataTypeSize == 0,
            OPS_REPORT_VECTOR_INNER_ERR(context->GetNodeName(), "get x dtype size is 0"),
            return ge::GRAPH_FAILED);
```

对比 add_example 里裸用的 `OP_LOGE(context, "invalid dtype")`（[add_example_tiling.cpp:86-88](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L86-L88)）：教学算子只打日志，正式算子则统一走 `OPS_REPORT_VECTOR_INNER_ERR`，让错误能被 `aclGetRecentErrMsg` 体系检索到（呼应 u3-l1 的返回码一节）。

**（5）可复用工具举例三：onnx 插件框架（framework 层）**

`common/src/framework/` 下集中放置了若干 ONNX 算子插件，如 [multi_head_attention_onnx_plugin.cpp:21-69](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L21-L69)：`ParseParamsMultiHeadAttention` 从 ONNX `NodeProto` 中逐个提取 `attn_head_num`、`attn_dim_per_head` 等 6 个属性（缺一即失败），写入 GE 算子的属性；[第 72-81 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L72-L81) 用 `REGISTER_CUSTOM_OP("MultiHeadAttention").FrameworkType(ONNX).OriginOpType({...}).ParseParamsFn(...)` 把 `ai.onnx::11~18::NPUMultiHeadAttention` 一批域名映射到该解析函数。这是「多个算子共用的框架胶水代码」放进 common 的又一实例（细节在 u6-l3 展开，本讲只认识它的位置）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「算子编译时 common 头文件已在搜索路径上」，并体验引入一个新 common 工具的最小改动。

**操作步骤**（基于 u2 改造过的 add_example，未改造过也可直接做）：

1. 浏览分层：`ls common/include/` 及其各子目录，对照上面 4.1.3 (2) 的表格，挑出 3 个你未来可复用的工具（建议：`err/ops_err.h`、`op_host/tiling_util.h`、`op_host/tiling_templates_registry.h`）。
2. 打开 `examples/add_example/op_host/add_example_tiling.cpp`，找到 dtype 校验分支（约 86-89 行的 `if (supportedDtype.count(dataType) == 0)`）。
3. 在文件头部补充 include：`#include "err/ops_err.h"`（与已有的两个 common include 并排）。
4. 把 `OP_LOGE(context, "invalid dtype");` 替换为：
   ```cpp
   OPS_REPORT_VECTOR_INNER_ERR(context->GetNodeName(), "invalid dtype %d", static_cast<int>(dataType));
   ```
5. 重新编译 host 侧：`bash build.sh --ophost --ops=add_example`。

**需要观察的现象**：编译应直接通过——因为 [cmake/obj_func.cmake:441](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L441) 已把 `${OPS_TRANSFORMER_DIR}/common/include` 加进 op_host 目标的搜索路径，`#include "err/ops_err.h"` 无需任何 CMake 改动。若在 UT 或运行态故意传一个不支持的 dtype（如 bfloat16），日志中会出现带 `E89999` 的上报（运行态现象**待本地验证**，需 NPU 或 simulator 环境）。

**预期结果**：零 CMake 改动、一处 include、一处宏替换，即完成一次「复用 common 工具」的升级；这就是公共库分层设计的直接收益。

#### 4.1.5 小练习与答案

**练习 1**：为什么 common 的实现用 OBJECT 库（`${COMMON_NAME}_obj`）而不是普通 SHARED 库？

**答案**：OBJECT 库只产出 `.o`，由各算子的最终 `.so`（如 libopapi_transformer.so、op_graph 库）直接吸纳拼装。这样公共代码与算子代码进同一个链接单元，符号可见性、版本配套都由算子包自己管理，避免多装一个独立 `.so` 带来的部署与版本依赖问题；同时 `$<TARGET_EXISTS:...>` 守卫让裁剪构建不因目标缺失而失败。

**练习 2**：`common/include/op_kernel/` 与 `common/include/op_host/` 都是「公共头」，为什么必须分成两个目录而不是合在一起？

**答案**：两者的编译环境和消费者完全不同：op_host 头在 host 编译器（x86/ARM CPU 侧 gcc）下编译、依赖 tiling API 与 metadef 头；op_kernel 头在 AscendC 设备编译器下编译、面向 UB/MTE/Matrix 指令。分开目录后，CMake 可以只把对应子目录加进对应目标的 include 路径，避免设备侧代码误引 host 头（反之亦然），编译裁剪也更干净。

**练习 3**：判断对错：「`EnsureNotScalar` 是 add_example 自己实现的，因为它就在 add_example 目录里被调用。」

**答案**：错。它由 `common/include/op_host/tiling_util.h` 声明、`common/src/op_host/tiling_util.cpp` 实现，add_example 只是通过 `#include "op_host/tiling_util.h"` 复用——这正是本讲想建立的「工具房」意识。

### 4.2 fallback 机制：图执行回退到 aclnn 两段式调用

#### 4.2.1 概念说明

mc2 目录下的通信-计算融合算子（matmul_all_reduce、all_gather_matmul、moe_distribute_dispatch 等）有个现实矛盾：

- 它们在**图模式**下以 `REG_OP` 算子节点出现在计算图里，执行由 GE 调度；
- 但它们的核心实现（矩阵乘、量化、通信编排）已经以 **aclnn eager 接口**的形式存在于 `libopapi_transformer.so` / `libopapi.so` 等动态库中。

为同一套逻辑再写一份「图专用 kernel」显然浪费。仓库的解法是 **fallback（回退）**：为图算子注册一个 host 侧执行函数（`OpExecuteFunc`），该函数在运行期：

1. 从图执行上下文 `gert::OpExecuteContext` 取出输入 `gert::Tensor` 和属性；
2. 把 `gert::Tensor` 转换为 `aclTensor`（eager 接口的入参类型）；
3. 通过 `dlopen`/`dlsym` 按名字找到 `aclnnXxxGetWorkspaceSize` 与 `aclnnXxx` 两个符号；
4. 完整走一遍 u3-l1 讲过的两段式调用，workspace 由图框架的 `host_api_ctx` 分配。

于是图模式的算子「回退」到了 eager 实现，一套代码两种入口。仓库中约有 20 个 mc2 算子带有 `op_graph/fallback_*.cpp` 文件。

#### 4.2.2 核心流程

```text
GE 图执行到 MatmulAllReduce 节点
   └─ 调用 IMPL_OP(MatmulAllReduce).OpExecuteFunc(MatmulAllreduceExecuteFunc) 注册的函数
        ├─ 1. host_api_ctx->GetInputTensor(i)          取 gert::Tensor 输入
        ├─ 2. ConvertMmType(x, transpose, enableNZ)     gert::Tensor → aclTensor（处理转置/NZ 格式）
        ├─ 3. attrs->GetBool/GetInt/GetStr              读图算子属性（group、comm_turn 等）
        ├─ 4. 按 quant 类型 / comm 模式分支             选择要调用的 aclnn 接口名
        └─ 5. EXEC_OPAPI_CMD(aclnnMatmulAllReduceV3, ...)
             ├─ GetOpApiFuncAddr("aclnnMatmulAllReduceV3GetWorkspaceSize")
             │    查找顺序: libcust_opapi.so → libopapi_transformer.so → libopapi.so → libaclnn_*.so
             ├─ ConvertTypes(__VA_ARGS__, &wsSize, &executor)  参数打包 + 类型转换
             ├─ call(GetWorkspaceSizeFunc, ...)          第一段：校验/tiling/算 workspace
             ├─ host_api_ctx->MallocWorkspace(wsSize)    workspace 由图框架出
             └─ opApiFunc(workspace, size, executor, stream) 第二段：下发执行
                 └─ ReleaseConvertTypes(...) 释放 aclTensor/aclIntArray 等中间对象
```

符号查找顺序（步骤 5 的展开）体现了一个重要优先级：**自定义算子包（cust）优先于本仓库的 transformer 库，再优先于 CANN 内置 opapi 库**——用户覆盖官方实现的能力由此而来。

#### 4.2.3 源码精读

**（1）符号查找：三层库 + 兜底**

[common/include/fallback/fallback.h:81-94](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L81-L94) 先给出三个库名常量；核心查找函数 [GetOpApiFuncAddr，第 131-158 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L131-L158)：

```cpp
inline void *GetOpApiFuncAddr(const char *apiName)
{
    static auto custOpApiHandler = GetOpApiLibHandler(GetCustOpApiLibName());        // libcust_opapi.so
    if (custOpApiHandler != nullptr) { ... }
    static auto transformerOpApiHandler = GetOpApiLibHandler(GetTransformerOpApiLibName()); // libopapi_transformer.so
    ...
    static auto opApiHandler = GetOpApiLibHandler(GetOpApiLibName());                // libopapi.so
    ...
    return GetAclnnArrdByApiName(apiName);  // 兜底: 再扫 libaclnn_ops_infer.so 等一组库
}
```

注意 `static` 修饰：句柄和函数地址每个进程只解析一次，之后调用零开销。兜底的 `GetAclnnArrdByApiName`（[第 114-129 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L114-L129)）会依次扫 `libaclnn_ops_infer.so`、`libaclnn_ops_train.so` 等 6 个 CANN 内置库。

**（2）类型转换：gert::Tensor → aclTensor**

[ConvertType(const gert::Tensor \*, ...)，fallback.h:202-237](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L202-L237) 完成两种张量描述的翻译：取设备地址、查 dtype 映射表（[GetConvertType，第 175-200 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L175-L200)，覆盖 FP16/BF16/INT4/FP8 等新类型）、从 storage shape 计算连续 strides，最后用 `dlsym` 得到的 `aclCreateTensor` 构造 aclTensor。矩阵乘场景专用的 [ConvertMmType，第 273-323 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L273-L323) 额外处理了转置（交换末两维的 strides 与 viewShape）和 NZ 格式。dtype 枚举层面的转换则在 [common/src/fallback/fallback_comm.cpp:35-51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/fallback/fallback_comm.cpp#L35-L51) 的 `ToAclDataType` 中以一张有序表实现。

配套的 `Release` 系列重载（[fallback.h:325-364](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L325-L364)）负责调用 `aclDestroyTensor` 等释放函数，`ConvertedParams` RAII 类（[第 419-458 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L419-L458)）保证异常路径也不泄漏。

**（3）`EXEC_OPAPI_CMD` 宏：一条语句完成两段式调用**

[common/include/fallback/fallback.h:470-527](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L470-L527) 是整个机制的枢纽（GNU statement expression，`({ ... })` 有返回值）。它依次：解析三个符号 → `ResetCacheThreadLocal` 清线程缓存 → `ConvertTypes` 打包参数 → 调第一段 `GetWorkspaceSize` → `host_api_ctx->MallocWorkspace` 申请 workspace → 调第二段下发 → 释放中间对象与 workspace。两阶段拆分的变体 `EXEC_OPAPI_PREPARE_CMD` 定义在 [common/include/fallback/fallback_2stages.h:90-127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback_2stages.h#L90-L127)，它把第一段与第二段拆开（参数与销毁器收集进 `OpApiParams` 交给框架托管），配套的执行端实现在 [common/src/fallback/fallback_comm_2stages.cpp:22-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/fallback/fallback_comm_2stages.cpp#L22-L27)（`ExecuteOpLaunch` 从 `OpExecuteLaunchContext` 取回参数与 workspace 地址再调第二段）。这正呼应 u3-l1 的设计动机：**把内存申请权交给调用方**——这里调用方是图框架。

**（4）消费者：matmul_all_reduce 的 fallback 文件**

[mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp:15-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp#L15-L27) include 了 `fallback/fallback.h` 并定义参数结构体；[GetMatmulPara，第 29-55 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp#L29-L55) 从 `OpExecuteContext` 取输入并用 `ConvertMmType` 转成 aclTensor（注释还点出一个坑：字段必须是非 const 的 `aclTensor*`，否则 `Release` 重载匹配不上会内存泄漏）。执行函数主体按「量化类型 × 通信模式」组合分发到不同版本的 aclnn 接口，如 [第 217-230 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp#L217-L230)：

```cpp
switch (quant_para.type) {
    case Mc2QuantType::K_NONE_QUANT:
        if (isCommMode) {
            return EXEC_OPAPI_CMD(aclnnMatmulAllReduceV3, mm_para.x1_acl, mm_para.x2_acl, ...);
        } else {
            if (x3 != nullptr) {
                return EXEC_OPAPI_CMD(aclnnMatmulAllReduceV2, ...);
            }
            return EXEC_OPAPI_CMD(aclnnMatmulAllReduce, ...);
        }
```

文件末尾 [第 285 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_graph/fallback_matmul_all_reduce.cpp#L285) 一行完成注册：

```cpp
IMPL_OP(MatmulAllReduce).OpExecuteFunc(MatmulAllreduceExecuteFunc);
```

CMake 侧，这些 `fallback_*.cpp` 由 [cmake/obj_func.cmake:692-695](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L692-L695) 的 `add_fallback_modules` 收进 `${OPGRAPH_NAME}_fallback_obj`，并与 common 的 `${COMMON_NAME}_fallback_obj`（提供 `fallback_comm.cpp` 等实现）一起链接进 op_graph 产物（见 [cmake/symbol.cmake:102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/symbol.cmake#L102)）。

#### 4.2.4 代码实践

**实践目标**：不改代码，通过源码走读 + 符号观察验证「fallback 在运行期才查找 aclnn 符号」这一论断。

**操作步骤**：

1. `grep -rn "IMPL_OP.*OpExecuteFunc" mc2 --include=*.cpp | head` —— 列出所有注册了 fallback 执行函数的算子（约 20 个）。
2. 任选两个（如 matmul_all_reduce 与 moe_distribute_dispatch），分别找到其 `EXEC_OPAPI_CMD` 调用，记录它们回退到的 aclnn 接口名。
3. 阅读 [fallback.h:470-486](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/fallback/fallback.h#L470-L486)，注意 `GetOpApiFuncAddr(#aclnn_api "GetWorkspaceSize")` 中 `#` 运算符把宏参数拼成字符串——这就是「按名字找函数」的实现方式。
4. （有编译产物时）在你的构建输出目录里对 op_graph 库执行 `nm -D <libopgraph*.so> | grep -i matmulallreduce`，确认它**没有**静态引用 `aclnnMatmulAllReduce` 符号（即运行期 dlsym，而非链接期绑定）。无构建环境时可跳过，改用 `grep -rn "dlsym" common/include/fallback/fallback.h` 指认查找机制。

**需要观察的现象**：步骤 1 列出的算子全部位于 `mc2/` 域；步骤 3/4 能确认符号解析发生在运行期且带 cust 优先的查找顺序。

**预期结果**：你能画出「GE 节点 → OpExecuteFunc → ConvertMmType → EXEC_OPAPI_CMD → aclnn 两段式」这条完整调用链，并解释为什么 fallback 算子不需要自己写 op_kernel 也能参与图执行。`nm` 观察点**待本地验证**（需要先完成一次 `--opgraph` 编译）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetOpApiFuncAddr` 里查找顺序是 `libcust_opapi.so` → `libopapi_transformer.so` → `libopapi.so`，而不是反过来？

**答案**：这是用户覆盖优先的原则。cust 库是用户自定义算子包，transformer 库是本仓库（进阶算子）的实现，`libopapi.so` 是 CANN 内置基础库。把用户包放在最前，允许用户对同一算子名提供自己的实现来覆盖官方行为；若顺序颠倒，用户包永远轮不到被查找。

**练习 2**：fallback 中 workspace 是谁分配的？和 u3-l1 讲的「调用方分配」矛盾吗？

**答案**：由 `host_api_ctx->MallocWorkspace(workspace_size)` 分配，`host_api_ctx` 是 GE 图执行上下文。不矛盾——两段式 API 的契约本来就是「workspace 由调用方出」，只是 eager 场景调用方是用户代码，fallback 场景调用方换成了图框架。这正是两段式设计的价值：执行环境换了，接口契约不变。

**练习 3**：`fallback_matmul_all_reduce.cpp` 中 `MatmulParas` 的注释说字段为什么不能写成 `const aclTensor *`？

**答案**：因为 `EXEC_OPAPI_CMD` 结束时要通过 `Release` 系列重载释放中间对象，重载按类型匹配 `Release(aclTensor *)`；若字段是 `const aclTensor *`，会匹配到模板 `Release(T value)`（什么都不做的版本），`aclDestroyTensor` 永远不会被调，造成内存泄漏。

## 5. 综合实践

任务：为本仓库撰写一页 `common` 复用指南（文档形式，不修改源码）。

1. **盘点**：`ls common/include/` 遍历全部子目录，从每个子目录挑 1 个头文件，用一句话概括其用途，形成一张分层表。
2. **验证复用**：按 4.1.4 的步骤，在 add_example 的 tiling 中引入 `OPS_REPORT_VECTOR_INNER_ERR` 并重新 `bash build.sh --ophost --ops=add_example`，记录改动行数与编译结果。
3. **画链路**：以 matmul_all_reduce 为例，手绘（或用文本框图）从 GE 图节点到 `aclnnMatmulAllReduceV3` 两段式调用的完整链路，标出每一步所在的文件与行号。
4. **回答思考题**：如果一个新的 mc2 算子想要 fallback 支持，它最少要交付哪些内容？（提示：一个 `op_graph/fallback_xxx.cpp`，内含参数提取 + `EXEC_OPAPI_CMD` + `IMPL_OP(X).OpExecuteFunc(...)` 注册；实现本身则以 aclnn 接口形式存在于 op_api 库。）

完成后你应同时掌握 common 的「静态复用」（头文件/工具）与「动态复用」（fallback 运行期桥接）两种形态。

## 6. 本讲小结

- `common/` 是全仓库共享的公共库：include 按消费者分层（op_host/op_api/op_kernel/op_graph/err/util/fallback/framework/tiling_sink），src 按产物分层（obj/fallback_obj/framework/tiling_sink）。
- CMake 中 common 以 OBJECT 库（`common_${PKG_NAME}_obj` 等）被各算子目标吸纳拼装，`common/include` 已默认在算子编译的头文件搜索路径上，复用公共头通常**零 CMake 改动**。
- 教学算子 add_example 已经在复用 common：`EnsureNotScalar`、`GET_TPL_TILING_KEY` 均来自 `common/include/op_host/`；工业级算子（如 ffn）则统一用 `err/ops_err.h` 的 E89999 上报内部错误。
- fallback 机制让 mc2 通信算子在图模式下复用 eager aclnn 实现：`IMPL_OP(...).OpExecuteFunc(...)` 注册 host 执行函数，内部经 `ConvertMmType` 转参、`dlsym` 按 cust → transformer → 内置 opapi 的顺序找符号，再走两段式调用。
- workspace 归属不变是关键理解点：两段式 API 的「调用方分配 workspace」契约，在 fallback 场景中调用方是图框架（`host_api_ctx`）。

## 7. 下一步学习建议

- 下一讲 u3-l3 将进入 `torch_extension/`：看 aclnn 算子如何被进一步包装成 PyTorch 友好接口——那是「复用」链条的最后一环（kernel → aclnn → torch API）。
- 若你对 fallback 的动机感兴趣，可提前浏览 [mc2/matmul_all_reduce/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/README.md) 与第五单元 mc2 相关讲义，理解通信-计算融合算子为何大量采用这一模式。
- 想深挖 common 设备侧工具的读者，可以在学完 u4-l3（FA 的 tiling 与多 SoC 适配）后回头阅读 `common/include/op_kernel/` 下的 iterator 与 `mma.h`，它们是 attention 类 kernel 的基础设施。
