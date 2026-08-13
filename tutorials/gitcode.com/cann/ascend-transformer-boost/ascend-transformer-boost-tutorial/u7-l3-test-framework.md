# 测试框架与算子测试

## 1. 本讲目标

ATB 有 70 多个算子、跨多代昇腾芯片，靠人工逐个写 C++ 调用脚本来验证既不现实也难以维护。本讲要解决的正是「如何系统化、数据驱动地测试一个算子」。

学完本讲，你应当能够：

- 说出 `tests/` 下各子目录（`framework`、`apitest`、`unittest`、`infratest`、`cinterface`、`high_level_test`）各自承担什么测试职责，遇到不同类型的 bug 知道去哪里写用例。
- 读懂 `operation_funcs.cpp` 里「JSON → Param → `CreateOperation`」的反序列化机制，理解 `g_funcMap` 这个算子名到工厂函数的注册表是如何把 CSV 里一行字符串变成一个可执行算子的。
- 看懂 CSV 驱动的精度/性能测试主流程（`CsvOpsTest.run_one_case` 的 Create/InferShape/Setup/Execute 四段式），以及 Python `unittest` 风格的 `OperationTest` 基类如何用 `golden_compare` 做精度比对。
- 为一个新算子编写 JSON 驱动的功能测试用例骨架（注册函数 + CSV 行 + golden 函数）。

## 2. 前置知识

本讲建立在前面几讲的认知之上，如果你对下面这些概念已经清楚，可以直接进入正文：

- **两段式执行**（u1-l6）：一个 `Operation` 的生命周期是 `Setup`（Host 校验 + Tiling，产出 `workspaceSize`）→ `Execute`（带 workspace 异步下发 Device）。测试框架正是按这两段切分并分别计时的。
- **Param 与 rsv 闸门**（u2-l3、u6-l4）：每个算子有一个 `XxxParam` POD 结构，末尾必有 `rsv[]` 预留字段；`CreateOperation` 入口会逐字节校验 `rsv` 必须全 0，否则返回 `ERROR_INVALID_PARAM`。理解这一点你才能看懂测试注册函数末尾那段「拷贝 rsv」的重复代码。
- **Operation 是抽象类**（u1-l6、u3-l1）：用户不直接 `new`，而是用模板工厂 `CreateOperation(param, &op)` 创建。测试框架需要一个**按算子名 + JSON 字符串**创建算子的「字符串入口」，这是 `operation_funcs.cpp` 存在的根本原因。
- **torch_atb 桥接**（u2-l2）：Python 侧通过 pybind11 / `torch.classes` 调到 C++。测试框架的 C++ 层 `OperationTorch` 就是一个 TorchScript 自定义类，让 Python 能直接 `set_param` / `execute`。

一个关键直觉先建立起来：**ATB 测试是「数据 + 引擎」分离的**。用例（要测什么形状、什么 dtype、什么参数）写在 CSV 里，引擎（怎么创建算子、怎么跑、怎么比对）写在 `CsvOpsTestTool` 与 `operation_funcs.cpp` 里。新增一个用例通常只改 CSV，不动代码。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|------|------|
| [docs/测试框架指南.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md) | 官方测试框架使用文档，CSV 字段、编译运行命令的权威来源 |
| [tests/CMakeLists.txt](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/CMakeLists.txt) | 测试总入口，按编译开关条件性纳入各子测试 |
| [tests/framework/c++/atb_torch/operation/operation_funcs.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp) | **本讲核心**：JSON → Param 反序列化 + `g_funcMap`/`g_update_funcMap` 注册表 |
| [tests/framework/c++/atb_torch/operation/operation_torch.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_torch.h) | `OperationTorch` 桥接类，把 C++ `Operation` 暴露给 Python |
| [tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py) | CSV 驱动测试引擎，`run_one_case` 四段式 + 精度/性能分析 |
| [tests/framework/python/CsvOpsTestTool/data_generation.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/data_generation.py) | `DataGen` 基类与每个算子的 `golden` 参考实现 |
| [tests/apitest/opstest/python/operations/operation_test.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/operation_test.py) | Python `unittest` 风格测试基类 `OperationTest` |

## 4. 核心概念与源码讲解

### 4.1 测试目录体系：按「测什么」分层

#### 4.1.1 概念说明

打开 `tests/` 你会看到一堆子目录，初学者很容易迷失。划分的依据其实只有一个问题：**你要测的是哪一层？** ATB 的代码从上到下是「Python 绑定 → C++ Operation → Runner → Kernel」，测试也按这个纵切面 + 场景维度组织成几大类。

先看总入口 `tests/CMakeLists.txt`：

```cmake
add_subdirectory(framework)                      # 测试框架本体，永远编
if(USE_UNIT_TEST OR USE_ALL_TEST)
    add_subdirectory(unittest)                   # C++ GTest 单测
    add_subdirectory(cinterface)                 # C 接口测试
endif()
```

[tests/CMakeLists.txt:15-18](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/CMakeLists.txt#L15-L18) 说明：`framework/` 是基础设施（编出 `libatb_test_framework.so`），所有其它测试都依赖它；而 `unittest/`、`cinterface/` 这类需要 GoogleTest 的 C++ 测试，只有显式打开 `USE_UNIT_TEST` 才编。

#### 4.1.2 核心流程：六个测试目录的定位

下表把整个 `tests/` 收敛成一张地图（语言与定位一栏是理解的关键）：

| 目录 | 语言 | 定位 | 典型内容 |
|------|------|------|---------|
| `framework/` | C++ + Python | **测试引擎**，不含具体用例 | `operation_funcs.cpp`（注册表）、`OperationTorch`（桥接）、`CsvOpsTestTool`（CSV 引擎）、`DataGen`（golden 基类） |
| `apitest/opstest/` | CSV + Python | **算子级**功能/精度/性能测试 | `csv/`（79 个 CSV 用例文件）、`python/operations/`（按算子名的 Python 测试）、`cpp/` |
| `apitest/kernelstest/` | Python | **Kernel 级**测试，直接测 `asdops`/`mixops` Kernel | `mix/test_flash_attention.py`、`matmul/test_pp_matmul_f16.py` 等 |
| `apitest/fuzztest/` | Python + C++ | **模糊测试**，自动生成随机用例找崩溃 | `generate_operation_fuzz_test.py` 自动生成用例 |
| `apitest/torch_atb_test/` | Python | `torch_atb` Python 绑定层测试 | — |
| `unittest/` | C++ (GTest) | **框架内部单测**，不依赖 NPU 也能跑一批 | `core/`（`test_svector`、`test_allocator`、`test_op_param_funcs`）、`ops/`、`kernels/` |
| `infratest/` | Python | **基础设施**测试（Tiling cache、内存分配、日志） | `test_setup_cache.py`、`test_mem_alloc_algorithm.py`、`test_svector.py` |
| `cinterface/` | C++ | **C 接口**测试（`atb_infer.h` 的 C 风格导出） | `mla_c_interface_test.cpp`、`paged_cache_load_c_interface_test.cpp` |
| `high_level_test/` | — | 高层/组合算子测试，按算子名分目录 | `ElewiseOperation/`、`SelfAttentionOperation/` 等 |

一个重要的区分点：**`unittest` 测的是框架本身的正确性**（`SVector` 越界、`Allocator` 对齐、`OperationBase` 的钩子），很多用例不真正 launch kernel，编译产物是 `atb_unittest` 可执行文件，链接 GoogleTest：

```cmake
add_executable(atb_unittest ${CORE_SOURCE} ${NORMAL_SOURCE} ${OPS_SOURCE})
target_link_libraries(atb_unittest PRIVATE atb_test_utils tbe_adapter -lgtest -lgtest_main -lc_sec)
```

见 [tests/unittest/CMakeLists.txt:23-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/unittest/CMakeLists.txt#L23-L31)。而 `apitest/opstest` 测的是**算子端到端精度**，必须跑在真实 NPU 上，依赖 `libatb_test_framework.so`。

#### 4.1.3 源码精读：编译开关与 build.sh 的对应关系

顶层 [CMakeLists.txt:21-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L21-L33) 用一串 `option()` 声明了所有测试开关：

```cmake
option(BUILD_TEST_FRAMEWORK "BUILD_TEST_FRAMEWORK" OFF)   # 编 libatb_test_framework.so
option(USE_UNIT_TEST  "USE_UNIT_TEST"  OFF)               # C++ GTest 单测
option(USE_CSV_OPS_TEST "USE_CSV_OPS_TEST" OFF)           # CSV 算子测试
option(USE_INFRA_TEST  "USE_INFRA_TEST"  OFF)             # 基础设施测试
option(USE_FUZZ_TEST   "USE_FUZZ_TEST"   OFF)             # 模糊测试
...
```

你不必手动拨这些 `-D`，`scripts/build.sh` 把它们封装成了好记的子命令。`build.sh` 顶部列出了全部目标：

```bash
BUILD_OPTION_LIST="help default testframework unittest kernelunittest pythontest torchatbtest kernelpythontest csvopstest fuzztest infratest hitest alltest clean gendoc customizeops"
```

见 [scripts/build.sh:41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L41)。几个最常用的对应关系（[scripts/build.sh:870-894](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L870-L894)）：

| build.sh 子命令 | 等价 CMake 开关 | 作用 |
|------|------|------|
| `bash scripts/build.sh testframework` | `-DBUILD_TEST_FRAMEWORK=ON` | 编 ATB 核心库 **+** 测试框架动态库，是跑任何算子测试的前提 |
| `bash scripts/build.sh unittest` | `-DUSE_UNIT_TEST=ON` | 编 + 跑 C++ GTest 单测 |
| `bash scripts/build.sh csvopstest` | `-DUSE_CSV_OPS_TEST=ON` | 跑 CSV 算子测试 |
| `bash scripts/build.sh alltest` | 一组开关全开 | 全量测试 |

> 注意：`testframework` 只**编译**测试框架产物，不执行用例；`csvopstest`/`unittest` 等才会真正**运行**。这是「构建」与「运行」两个阶段的区别。

#### 4.1.4 代码实践

**实践目标**：建立「不同 bug → 不同测试目录」的直觉。

**操作步骤**：

1. 列出 `tests/unittest/core/` 下的文件名，注意它们大多以 `test_` 开头且对应框架内部概念（`test_svector`、`test_allocator`、`test_op_param_funcs`、`test_aclnn_executor_cache`）。
2. 列出 `tests/apitest/opstest/csv/` 下的文件名，注意它们以**算子名**命名（`elewise.csv`、`linear.csv`）。
3. 打开 `tests/apitest/fuzztest/generate_operation_fuzz_test.py` 的文件名，理解它是「生成器」而非固定用例。

**需要观察的现象**：`unittest` 测的是「机制」（数据结构、资源池、参数宏），`opstest` 测的是「算子语义」（这个形状 + 这个参数，输出对不对），`fuzztest` 测的是「鲁棒性」（随机参数会不会崩）。

**预期结果**：你能用一句话回答「我发现 `SVector` 越界没抛异常，该去哪写用例？」——答案应是 `unittest/core/test_svector.cpp`（或新增同类），而不是 `opstest`。

#### 4.1.5 小练习与答案

**练习 1**：`BUILD_TEST_FRAMEWORK` 和 `USE_UNIT_TEST` 有什么区别？为什么 `tests/CMakeLists.txt` 里 `framework` 无条件编译，而 `unittest` 要包在 `if` 里？

> **答案**：`BUILD_TEST_FRAMEWORK` 编译的是**测试引擎**（`libatb_test_framework.so` + Python 工具），是所有算子测试的公共依赖，故无条件纳入；`USE_UNIT_TEST` 编译的是**具体 C++ 单测可执行文件**，依赖 GoogleTest 第三方库且需要先 `fn_build_3rdparty_for_test`，只在真正要跑单测时才开启，以避免无谓的编译开销与依赖。

**练习 2**：如果你要验证 `aclnn` 的 executor 缓存（容量 16）是否真的命中，应该用哪个目录的测试做参考？

> **答案**：`tests/unittest/core/test_aclnn_executor_cache.cpp`——它测的是框架内部的缓存机制，属于 `unittest` 范畴，而非算子语义。

---

### 4.2 operation_funcs：JSON 驱动的算子反序列化与注册

#### 4.2.1 概念说明

CSV 用例的 `OpParam` 字段是一串 JSON 字符串（如 `{"elewiseType":8}`），而 C++ 创建算子需要的是强类型的 `atb::infer::ElewiseParam` 结构体。**这两者之间的翻译官就是 `operation_funcs.cpp`。**

它的存在回答了一个工程问题：`CreateOperation` 是个模板函数，签名是 `CreateOperation<XxxParam>(param, &op)`——类型在编译期就定死了。但 CSV 测试是运行时解析的，只有一个算子名字符串和一个 JSON 串。所以需要一个**运行时的分派表**：算子名 → 一个「把 JSON 反序列化成对应 Param 并调用 `CreateOperation`」的函数。

这就是 `g_funcMap` 的本质：一张 `算子名 → 工厂函数` 的注册表。

#### 4.2.2 核心流程

为每个算子写一个 `XxxOperationCreate` 函数，套路高度统一：

```
输入：nlohmann::json paramJson, atb::Operation **op
  1. 默认构造一个 atb::infer::XxxParam param;        // 字段都是带默认值的 POD
  2. 对每个业务字段：
       if (paramJson.contains("字段名")) {            // 缺省字段自动取 Param 默认值
           param.字段 = paramJson["字段名"].get<类型>();
       }
  3. 枚举字段要显式转型：
       param.maskType = SelfAttentionParam::MaskType(paramJson["maskType"].get<int32_t>());
  4. rsv 预留字段单独拷贝（如果 JSON 里给了）：
       for (i in rsv) param.rsv[i] = paramJson["rsv"].at(i).get<int8_t>();
  5. return CreateOperation(param, op);              // 进入正式的算子创建 + rsv 闸门校验
```

然后在文件末尾，把所有这些函数登记进 `g_funcMap`，键名就是 CSV 里 `OpName` 字段的值。运行时入口 `CreateOperation(opName, param, &op)` 查表分派。

#### 4.2.3 源码精读

**(1) 工厂函数签名与一个完整样例**

函数指针类型定义在 [operation_funcs.cpp:26](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L26)：

```cpp
using OperationCreateFunc = std::function<atb::Status(const nlohmann::json &paramJson, atb::Operation **op)>;
```

以最简单的 `LinearOperationCreate` 为例（[operation_funcs.cpp:475-509](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L475-L509)）：

```cpp
static atb::Status LinearOperationCreate(const nlohmann::json &paramJson, atb::Operation **op)
{
    atb::infer::LinearParam param;                       // 步骤1：默认构造
    if (paramJson.contains("transposeA")) {              // 步骤2：逐字段 contains 回填
        param.transposeA = paramJson["transposeA"].get<bool>();
    }
    if (paramJson.contains("transposeB")) {
        param.transposeB = paramJson["transposeB"].get<bool>();
    }
    if (paramJson.contains("hasBias")) {
        param.hasBias = paramJson["hasBias"].get<bool>();
    }
    if (paramJson.contains("outDataType")) {
        param.outDataType = aclDataType(paramJson["outDataType"].get<int32_t>());  // 枚举/类型需显式转
    }
    if (paramJson.contains("matmulType")) {
        param.matmulType = atb::infer::LinearParam::MatmulType(paramJson["matmulType"].get<int>());
    }
    // ... quantMode 等
    if (paramJson.contains("rsv")) {                     // 步骤4：rsv 单独拷贝
        for (size_t i = 0; i < paramJson["rsv"].size(); i++) {
            param.rsv[i] = paramJson["rsv"].at(i).get<int8_t>();
        }
    }
    return CreateOperation(param, op);                   // 步骤5：进入正式创建
}
```

注意几个要点（这些是给新算子写注册函数时的「铁律」）：

- **缺省即默认**：用 `contains` 守卫每个字段，CSV 没写的字段就保留 `XxxParam` 构造时的默认值。这就是为什么测试文档说「JSON 用例缺省字段自动取默认值」。
- **枚举必须显式转型**：JSON 里 `maskType` 是个整数，C++ 里是强类型枚举 `MaskType`，必须写 `MaskType(json.get<int32_t>())`，否则编译不过。这一点承接 u2-l3 讲的「嵌套枚举」。
- **`rsv` 走另一条路**：这里只是「拷贝」rsv，真正的「全 0 校验」发生在第 5 步调用的 `CreateOperation(param, op)` 内部（u6-l4 讲的 rsv 版本闸门）。所以测试框架允许你在 JSON 里**故意填非 0 的 rsv** 来构造反例，验证闸门是否拦截。

**(2) 嵌套 Param 的递归解析**

复杂算子的 Param 有嵌套子结构。看 `ElewiseOperationCreate` 如何处理 `mulsParam` 和 `quantParam`（[operation_funcs.cpp:963-1004](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L963-L1004)）：

```cpp
param.elewiseType = paramJson["elewiseType"].get<atb::infer::ElewiseParam::ElewiseType>();  // 必填字段
if (paramJson.contains("mulsParam")) {
    const auto &mulsParam = paramJson["mulsParam"];           // 取子 JSON 对象
    if (mulsParam.contains("varAttr")) {
        param.mulsParam.varAttr = mulsParam["varAttr"].get<float>();
    }
    if (mulsParam.contains("rsv")) { /* 逐字节拷贝 */ }
}
if (paramJson.contains("quantParam")) {
    const auto &quantParam = paramJson["quantParam"];
    if (quantParam.contains("inputScale")) { param.quantParam.inputScale = ...; }
    // ... inputOffset / asymmetric / rsv
}
```

规律：**嵌套 Param 就是嵌套一层 `contains` 守卫**，每一层子结构都有自己的 `rsv`。`LayerNormOperationCreate`（[operation_funcs.cpp:1157-1233](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L1157-L1233)）把这套用到了极致：它根据 `layerType`（NORM/PRENORM/POSTNORM）只解析对应的那一个子 Param，其它跳过。

**(3) 注册表与运行时分派**

所有工厂函数在文件末尾登记进 `g_funcMap`（[operation_funcs.cpp:2528-2617](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L2528-L2617)），节选：

```cpp
std::map<std::string, OperationCreateFunc> g_funcMap = {
    {"AllReduceOperation",         &AllReduceOperationCreate},
    {"ElewiseOperation",           &ElewiseOperationCreate},
    {"LinearOperation",            &LinearOperationCreate},
    {"SelfAttentionOperation",     &SelfAttentionOperationCreate},
    {"PagedAttentionOperation",    &PagedAttentionOperationCreate},
    // ... 约 90 个算子
};
```

键名就是 CSV `OpName` 列的值。运行时入口在 [operation_funcs.cpp:2619-2635](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L2619-L2635)：

```cpp
atb::Status CreateOperation(const std::string &opName, const std::string &param, atb::Operation **operation)
{
    nlohmann::json paramJson = nlohmann::json::parse(param);   // 字符串 → JSON
    auto it = g_funcMap.find(opName);
    if (it == g_funcMap.end()) {
        ATB_LOG(ERROR) << "not support opName:" << opName;
        return atb::ERROR_INVALID_PARAM;                       // 未注册的算子名
    }
    try {
        return it->second(paramJson, operation);               // 分派到具体工厂函数
    } catch (const std::exception &e) {
        ATB_LOG(ERROR) << opName << " parse json fail, error:" << e.what();
    }
    return atb::ERROR_INVALID_PARAM;
}
```

这就是整条链的「字符串入口」。头文件只暴露这两个重载（[operation_funcs.h:15-16](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.h#L15-L16)）：

```cpp
atb::Status CreateOperation(const std::string &opName, const std::string &param, atb::Operation **operation);
atb::Status UpdateOperationParam(const std::string &opName, const std::string &param, atb::Operation *operation);
```

注意它**遮蔽**了 `atb/operation.h` 里的模板版 `CreateOperation<Param>`——这里是按名字 + 字符串分派的运行时版本，两者靠参数类型（`string` vs `Param`）区分重载。

**(4) Update 的第二张表**

部分算子（`Sort`、`Fill`、`TopkToppSampling`、`LaserAttention` 等）支持运行时换参数（对应 u1-l6 的 `UpdateOperationParam`），它们登记在第二张表 `g_update_funcMap`（[operation_funcs.cpp:2639-2652](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L2639-L2652)）。为了复用反序列化代码，这些算子把「JSON → Param」抽成一个独立的 `XxxParamFromJson` 自由函数，`Create` 和 `Update` 都调它，例如 `SortParamFromJson`（[operation_funcs.cpp:637-660](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L637-L660)）。

#### 4.2.4 代码实践

**实践目标**：读懂注册套路，为「给新算子加测试」扫清代码层面的障碍。

**操作步骤**：

1. 在 `operation_funcs.cpp` 中搜索 `SelfAttentionOperationCreate`（[operation_funcs.cpp:511-587](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L511-L587)），数一数它 `contains` 了几个字段，体会复杂算子参数之多。
2. 对比 `KvCacheOperationCreate`（[operation_funcs.cpp:756-765](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_funcs.cpp#L756-L765)）：它的 Param 几乎只有 `rsv`，所以整个函数只剩 rsv 拷贝 + `CreateOperation`——这是最简形态。
3. 在 `g_funcMap` 里确认 `SelfAttentionOperation` 这个键名存在（CSV 里的 `OpName` 必须与此**完全一致**，大小写敏感）。

**需要观察的现象**：所有注册函数长得几乎一样，差异只在「有哪些字段、哪些是枚举、有没有嵌套子 Param」。这正是它能用「模板化思路」批量生成的原因。

**预期结果**：你能在 30 秒内说清「CSV 里写 `OpName=LinearOperation`、`OpParam={"hasBias":true}` 后，框架内部发生了什么」——答案是：查 `g_funcMap` 找到 `LinearOperationCreate` → `contains("hasBias")` 命中 → 回填 `param.hasBias=true` → 调模板版 `CreateOperation(LinearParam, &op)` → rsv 校验 → 返回算子。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `paramJson.contains(...)` 守卫对测试用例设计很重要？如果直接写 `param.hasBias = paramJson["hasBias"].get<bool>();` 会怎样？

> **答案**：CSV 用例经常只填部分字段，其余取 Param 默认值。`contains` 守卫让缺省字段安全地保留默认值；若直接 `get`，当 JSON 里没有该键时 `nlohmann::json` 会抛异常（或用默认构造的空值），导致用例非预期失败。注意例外：少数「必填」字段（如 `ElewiseOperationCreate` 里的 `elewiseType`、`PagedAttentionOperationCreate` 里的 `headNum`）没有 `contains` 守卫，缺失即抛异常被上层 catch 成 `ERROR_INVALID_PARAM`。

**练习 2**：`g_funcMap` 的键名（如 `"SelfAttentionOperation"`）和哪些地方必须保持一致？

> **答案**：① CSV 用例的 `OpName` 列；② `data_generation.py` 里的类名（`class SelfAttentionOperation(DataGen)`，因为 golden 是靠 `eval('data_generation.' + op_name + '.golden')` 动态调用的）；③ Python 测试里 `torch.classes.OperationTorch.OperationTorch(op_name)` 传入的名字。这是「注册名一致」铁律在测试侧的三个落点。

**练习 3**：如何在 CSV 里构造一个「rsv 非法」的反例？

> **答案**：在 `OpParam` 的 JSON 里加上 `"rsv":[1,0,0,...]`（非 0），并把 `ExpectedError` 列设为 `C:ERROR_INVALID_PARAM`。`XxxOperationCreate` 会把非 0 rsv 拷进 Param，随后 `CreateOperation` 内部的 rsv 闸门检测到非 0，返回 `ERROR_INVALID_PARAM`，框架在 Create 阶段（前缀 `C:`）比对期望错误码，判用例通过。

---

### 4.3 CSV 驱动的精度与性能测试

#### 4.3.1 概念说明

`operation_funcs.cpp` 解决了「怎么把字符串变成算子」，但它本身不跑测试。真正驱动「读 CSV → 建算子 → 生成输入 → 跑 → 算 golden → 比对 → 计时」全流程的，是 Python 层的 `CsvOpsTestTool`。

这里有一个测试领域的核心概念：**golden（参考实现）**。ATB 算子跑在 NPU 上，你怎么知道它算得对？办法是用一个「可信实现」（通常是 PyTorch 在 CPU 上的等价运算）算一遍同样的输入，得到 `golden_output`，再和 NPU 的 `actual_output` 比对。浮点数不可能完全相等，所以用容差（`atol`/`rtol`）判定。

CSV 测试把这三件事彻底数据化了：
- **输入规格**（形状、dtype、格式）→ CSV 的 `InShape`/`InDType`/`InFormat` 列；
- **参数** → `OpParam` 列的 JSON；
- **期望结果** → `ExpectedError`（正例 `NO_ERROR`，反例 `阶段:错误码`）+ `data_generation.py` 里的 `golden` 函数。

#### 4.3.2 核心流程：run_one_case 的四段式

`CsvOpsTest.run_one_case` 把一个用例的执行严格切成 Create → InferShape → Setup → Execute 四段，每段都比对 `ExpectedError` 并单独计时：

```
对 CSV 的每一行（一个用例）：
  1. Create  (阶段前缀 "C")：调 operation_funcs 的字符串 CreateOperation
            → 若失败且期望正是 C 段错误，判通过（反例），return
  2. 生成输入张量（按 DataGenType: random/zero/one/customize + DataGenRange）
  3. case_preprocess（DataGen 钩子，多数算子为空）
  4. InferShape (前缀 "I")：只传 TensorDesc 推输出形状，不碰数据
  5. 按推导形状生成输出张量
  6. Setup    (前缀 "S")：Host 校验 + Tiling，得到 workspaceSize，记录 SetupTime
  7. case_postprocess（DataGen 钩子）
  8. Execute  (前缀 "E")：带 workspace 异步下发 + 同步，记录 ExecuteTime/SyncTime
  9. 若非 -sv（skip verify）：analyse_result() 与 golden 比对精度
```

这四段对应 u1-l6 讲的 Operation 两段式（Setup/Execute），只是测试框架拆得更细，把 Create 和 InferShape 也单独拎出来，目的是能精确测试每个阶段的错误码与耗时。

#### 4.3.3 源码精读

**(1) 四段式主循环**

[atb_csv_ops_test.py:338-373](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py#L338-L373) 是 `run_one_case` 的核心（节选关键行）：

```python
def run_one_case(self, index, times, output_file):
    ...
    if self.file_data.loc[self.index, 'Result'] != 'succ':
        self.create_op_result = self.torch_operation_setup()          # 阶段 C: 创建算子
        if (not self.get_json_result(self.create_op_result, "C", times)):
            return                                                    # 反例：期望 C 段失败 → 通过并返回
    self.api_data_reset(output_file)
    self.generate_input_tensors()                                     # 按 DataGenType 生成输入
    case_preprocess_func = 'data_generation.' + self.operation_name + '.case_preprocess'
    eval(case_preprocess_func)(self.op_param_str, self.operation, self.input_tensor_list)

    infershape_result = self.operation.infer_shape(self.input_tensor_list)   # 阶段 I
    if (not self.get_json_result(infershape_result, "I", times)):
        return
    self.generate_output_tensors(infershape_result)
    setup_result = self.operation.setup(self.input_tensor_list, self.output_tensor_list)  # 阶段 S
    if (not self.get_json_result(setup_result, "S", times)):
        return
    ...
    execute_result = self.operation.execute_sync(                     # 阶段 E
        self.input_tensor_list, self.output_tensor_list, self.workspace_size)
    if (not self.get_json_result(execute_result, "E", times)):
        return
    if not self.args.skip_verify:
        self.analyse_result()                                         # 精度比对
```

`self.operation` 是一个 `OperationTorch`（C++ 桥接对象），它的 `infer_shape` / `setup` / `execute_sync` 方法逐一对应 4.3.2 的四段。`get_json_result` 负责把每段返回的 JSON（含 `result` 错误码、`setup_time`、`execute_time`）和 `ExpectedError` 比对，并回填到结果表（[atb_csv_ops_test.py:313-336](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py#L313-L336)）。

> **`ExpectedError` 的阶段前缀**就是从这里来的：`C:` 表示期望在 Create 阶段失败、`I:` 期望 InferShape 失败、`S:` 期望 Setup 失败、`NO_ERROR` 表示正例要一路跑通。这承接测试框架指南里 [docs/测试框架指南.md:142-150](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md#L142-L150) 的错误码格式表。

**(2) golden 的动态调用**

精度比对的前提是先算出 golden。`generate_golden_output_tensors` 用 `eval` 按算子名动态调用 `data_generation.py` 里的 `golden` 静态方法（[atb_csv_ops_test.py:375-386](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py#L375-L386)）：

```python
def generate_golden_output_tensors(self):
    golden_input_tensors = []
    for tensor in self.input_tensor_list:
        ...
        golden_input_tensors.append(tensor.cpu().to(dtype))      # golden 在 CPU 算
    golden_tensor_gen_func = 'data_generation.' + self.operation_name + '.golden'
    golden_output_tensors = eval(golden_tensor_gen_func)(golden_input_tensors, self.op_param_str)
    for i in range(len(golden_output_tensors)):
        self.golden_output_tensor_list.append(golden_output_tensors[i])
```

这就是为什么 `data_generation.py` 里的**类名必须等于算子名**——引擎靠字符串拼接 + `eval` 找到 golden 函数。这也是 4.2.5 练习 2 答案里「注册名一致」的第二落点。

**(3) golden 函数怎么写**

看 `data_generation.py` 的 `DataGen` 基类与一个真实 golden（[data_generation.py:280-353](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/data_generation.py#L280-L353)）。基类约定了几个静态方法：

```python
class DataGen:
    @staticmethod
    def customize(shapes, i, datatype, format, data_gen_ranges, op_params) -> torch.Tensor:
        return DataGen.random(...)           # 输入数据生成，可覆盖

    @staticmethod
    def golden(in_tensors, op_params) -> [torch.Tensor]:
        pass                                 # 参考输出，必须覆盖；在 CPU 上算

    @staticmethod
    def get_op_type(op_params) -> OpTypes:
        return OpTypes.NA                    # 决定用哪种精度标准（见下）

    @staticmethod
    def performance_threshold(op_params):
        return {"SetupTime(us)": 1000000, "ExecuteTime(us)": 1000000, "SyncTime(us)": 1000000}
```

注释（[data_generation.py:356-361](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/data_generation.py#L356-L361)）明确：「类名用算子名、继承 `DataGen`、`golden` 必须覆盖、`golden` 数据不得在 NPU 上生成」。一个最简真实样例是测试指南给的 ElewiseOperation（[docs/测试框架指南.md:170-189](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md#L170-L189)）：

```python
class ElewiseOperation(DataGen):
    @staticmethod
    def golden(in_tensors, op_params):
        elewise_type = json.loads(op_params)["elewiseType"]
        if elewise_type == 8:  # ELEWISE_ADD
            return [in_tensors[0] + in_tensors[1]]
    @staticmethod
    def get_op_type(op_params):
        elewise_type = json.loads(op_params)["elewiseType"]
        if elewise_type in [8, 2, 3, 4, 5, 9, 10, 15]:
            return OpTypes.COMPUTE_FLOAT
```

**(4) 精度比对与性能阈值**

精度判定不是简单的 `allclose`，而是按 `get_op_type` 返回的 `OpTypes` 分档（`COMPUTE_FLOAT`、`COMPUTE_QUANT`、`VECTOR_FUSION`、`CV_FUSION` 等），不同档位用不同的容差/统计方法。引擎里有 `__error_percent`、`__precision_eb_percent` 等多个比对函数（[atb_csv_ops_test.py:406-490](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py#L406-L490)），例如量化算子用「误差比例」、`hifloat8` 用 ULP 误差、普通浮点用相对误差。`OpTypes.NA` 表示旧式精度标准。

性能测试则复用同一条四段式链路，只是把每段时间和 `performance_threshold` 比。CSV 里 `TestType=Performance` 的用例，配合运行参数 `-t 400`（执行 400 次取统计）即可输出 `SetupTime`/`ExecuteTime`/`SyncTime`，详见测试指南 [docs/测试框架指南.md:237-243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md#L237-L243)。

#### 4.3.4 代码实践

**实践目标**：跑通一个 CSV 用例（若本地有 NPU），或至少读懂一条用例从 CSV 到结果的完整路径。

**操作步骤**：

1. 打开 `tests/apitest/opstest/csv/elewise.csv` 第 1 行（正例 `ElewiseCastf2half`）和文档里的反例行（[docs/测试框架指南.md:154-164](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md#L154-L164)）。
2. 对照本节四段式流程，在纸上标出这行 CSV 的每个字段分别被哪一段消费（如 `InDType` → 生成输入、`OpParam` → Create 阶段、`ExpectedError` → 每段比对、`SocVersion` → 运行前芯片过滤）。
3. 若本地已编译测试框架并 `source output/atb/set_env.sh`，运行单条用例验证：

```shell
python tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py \
    -i tests/apitest/opstest/csv/elewise.csv -n 1
```

**需要观察的现象**：正例用例 `Result` 列为 `succ`、`ActualError` 为 `NO_ERROR`，并填入 `SetupTime`/`ExecuteTime`；反例（如 `bool` dtype 的加法）在 `I` 阶段返回 `ERROR_INVALID_TENSOR_INI_MATCH`，与 `ExpectedError=I:ERROR_INVALID_TENSOR_INI_MATCH` 匹配，同样判 `succ`。

**预期结果**：理解「正例和反例用的是同一套引擎，区别只在 `ExpectedError`」。

**待本地验证**：上述运行命令需要在配备昇腾 NPU、已安装 PyTorch/TorchNPU 并执行过 `bash scripts/build.sh testframework` 的环境下才能成功；无 NPU 环境时，本实践退化为「源码阅读型」，重点是步骤 1-2 的字段映射。

#### 4.3.5 小练习与答案

**练习 1**：为什么 golden 必须在 CPU 上算，不能在 NPU 上算？

> **答案**：golden 的作用是「独立可信参考」。若用 NPU 上的同一套算子算 golden，等于「用被测对象验证自己」，发现不了 bug。CPU 上的 PyTorch 运算（或 numpy/scipy）是独立实现，两者交叉验证才有意义。这就是 `DataGen` 注释强调「golden 数据不应在 npu 上生成」的原因。

**练习 2**：`get_op_type` 返回 `OpTypes.COMPUTE_QUANT` 和 `COMPUTE_FLOAT` 会影响什么？

> **答案**：决定精度比对用哪一档容差与统计方法。量化算子（int8）的误差分布与浮点完全不同，引擎对 `COMPUTE_QUANT` 用「误差比例」（超阈值的元素占比），对 `COMPUTE_FLOAT` 用相对误差/最大绝对误差。选错档位会导致要么误报、要么漏检。

**练习 3**：性能测试和功能测试在引擎层面是两套代码吗？

> **答案**：不是。它们共用 `run_one_case` 同一条四段式链路，差异由 CSV 的 `TestType` 列和运行参数 `-t`（执行次数）、`-tt`（按测试类型过滤）控制。功能测试关心 `Result= succ`，性能测试额外关心 `SetupTime/ExecuteTime` 是否低于 `performance_threshold`。这也是「数据驱动」的体现——改用例类型不动引擎。

---

### 4.4 Python unittest 风格测试：OperationTest 基类

#### 4.4.1 概念说明

CSV 适合「大量、参数化、形状组合」的用例，但有些场景用 CSV 表达很别扭：需要自定义复杂数据生成、需要多步前后处理、或想用 `unittest` 的 `setUp/tearDown`。为此 ATB 提供了第二条测试路径——**直接写 Python 测试类**，放在 `tests/apitest/opstest/python/operations/<算子名>/` 下。

这条路径的入口是基类 `OperationTest`，它封装了「建算子 → 设参 → 执行 → golden 比对」的套路，子类只需实现 `golden_calc` 和若干 `test_xxx` 方法。

#### 4.4.2 核心流程

```
子类继承 operation_test.OperationTest：
  1. 实现 golden_calc(self, in_tensors) → 返回期望输出列表
  2. 在 test_xxx 里：
       a. 构造输入张量（torch.randn(...).npu().half() 等）
       b. 调 self.execute(OP_NAME, PARAM, [in_tensors])
            ↓ execute 内部：
              - torch.classes.OperationTorch.OperationTorch(op_name)  建桥接对象
              - set_param(json.dumps(PARAM))                          设 JSON 参数
              - execute(in_tensors)                                   跑（Setup+Execute+同步）
              - golden_calc(in_tensors)                               算参考
              - golden_compare → torch.allclose(rtol, atol)           比对
```

注意：这里的 `OperationTorch` 与 CSV 路径用的是**同一个 C++ 桥接类**，只是 Python 侧的封装层不同（`OperationTest.execute` vs `CsvOpsTest.run_one_case`）。底层都走 `operation_funcs.cpp` 的 `g_funcMap`。

#### 4.4.3 源码精读

**(1) 加载桥接库与设备识别**

[operation_test.py:28-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/operation_test.py#L28-L29) 加载测试框架动态库，把 C++ 自定义类 `OperationTorch` 注入 Python：

```python
LIB_PATH = os.path.join(ATB_HOME_PATH, "lib/libatb_test_framework.so")
torch.classes.load_library(LIB_PATH)
```

随后 `get_soc_version()`（[operation_test.py:35-62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/operation_test.py#L35-L62)）通过 `torch.npu.get_device_name()` 把冗长的设备名归一化成 `Ascend910B`/`Ascend910A`/`Ascend310P`/`Ascend950` 等，供用例按芯片跳过（如某用例 `if get_soc_version() != 'Ascend910B': return`）。

**(2) execute 与 golden_compare**

`OperationTest.execute`（[operation_test.py:66-79](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/operation_test.py#L66-L79)）把整条链压成一行 `self.operation.execute(in_tensors)`：

```python
def execute(self, op_name, op_param, in_tensors):
    self.operation = torch.classes.OperationTorch.OperationTorch(op_name)  # 建对象
    if isinstance(op_param, dict):
        self.operation.set_param(json.dumps(op_param))                    # dict → JSON 串
    elif isinstance(op_param, str):
        self.operation.set_param(op_param)
    out_tensors = self.operation.execute(in_tensors)                      # Setup+Execute+同步
    golden_out_tensors = self.golden_calc(in_tensors)                     # 子类提供的参考
    self.__golden_compare_all(out_tensors, golden_out_tensors)            # 比对
```

精度比对用 `torch.allclose`，默认容差 `rtol=atol=0.02`（[operation_test.py:155-162](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/operation_test.py#L155-L162)）：

```python
def golden_compare(self, out_tensor, golden_out_tensor, rtol=0.02, atol=0.02):
    result = torch.allclose(out_tensor.cpu(), golden_out_tensor.cpu(), rtol=rtol, atol=atol)
    ...
    return result
```

> 对比 4.3：CSV 路径的精度判定分档更细（按 `OpTypes` 用不同统计方法），而 Python `OperationTest` 路径用统一的 `allclose(0.02, 0.02)`。这说明两条路径定位不同：CSV 做「批量、严格、分档」的回归，Python 类做「灵活、快速」的场景验证。

**(3) 一个真实子类**

`Split` 算子的 Python 测试（[tests/apitest/opstest/python/operations/split/test_split.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/apitest/opstest/python/operations/split/test_split.py)）是最干净的样例：

```python
OP_NAME = "SplitOperation"
PARAM = {"splitDim": -1, "splitNum": 2}

class TestAddOperation(operation_test.OperationTest):
    def golden_calc(self, in_tensors):
        return torch.chunk(in_tensors[0], chunks=2, dim=-1)      # PyTorch 参考实现

    def test_2d_half(self):
        intensor0 = torch.rand(4096, 22016).npu().half()
        x = self.execute(OP_NAME, PARAM, [intensor0])

    def test_2d_bf16(self):
        if not operation_test.get_soc_version() == 'Ascend910B':  # 按芯片跳过
            return True
        intensor0 = torch.rand(4096, 22016).npu().bfloat16()
        x = self.execute(OP_NAME, PARAM, [intensor0])
```

可以看到，写一个 Python 用例只需要三件事：声明 `OP_NAME`/`PARAM`、实现 `golden_calc`、写 `test_xxx` 构造输入并调 `execute`。`execute` 内部自动完成设参、执行、golden 比对、断言。

`OperationTorch` 还提供 `execute_out`（输出张量由调用方提供，用于 in-place 算子）、`execute_with_param`（带 VariantPack 运行参数）、`update_param`（换参数重跑）等变体（见 [operation_torch.h:29-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/atb_torch/operation/operation_torch.h#L29-L39)），`OperationTest` 对应封装了 `execute_out`/`execute_inplace`/`execute_update_param` 等方法。

#### 4.4.4 代码实践

**实践目标**：掌握 Python 路径的最小写法，能仿写一个新算子的 Python 测试。

**操作步骤**：

1. 阅读测试指南附录的 Elewise 示例（[docs/测试框架指南.md:272-300](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/测试框架指南.md#L272-L300)），注意它的目录约定：`tests/apitest/opstest/python/operations/my_elewise_op/test_my_op.py`，且需 `sys.path.append("../")` 后 `import operation_test`。
2. 对照 `test_split.py`，确认三要素（`OP_NAME`、`golden_calc`、`test_xxx`）齐全。
3. 若本地已编译并设好环境，运行单个 Python 测试文件：

```shell
source output/atb/set_env.sh
python tests/apitest/opstest/python/operations/split/test_split.py
```

**需要观察的现象**：`unittest` 默认会收集所有 `test_` 开头的方法依次执行；`test_2d_bf16` 在非 910B 芯片上会打印「only supports Ascend910B」并 `return True` 跳过。

**预期结果**：每个 `test_xxx` 内部完成一次 NPU 执行 + CPU golden 比对，`allclose` 通过则用例 PASS。

**待本地验证**：运行命令需要 NPU 环境；无 NPU 时本实践为源码阅读型，重点是掌握三要素写法。

#### 4.4.5 小练习与答案

**练习 1**：`OperationTest.execute` 里的 `set_param(json.dumps(op_param))` 和 CSV 路径里 `operation_funcs.cpp` 的关系是什么？

> **答案**：`set_param` 把 JSON 串存到 `OperationTorch` 对象里；真正创建算子时，C++ 侧会调 `operation_funcs.cpp` 的字符串版 `CreateOperation(opName, param, &op)`，查 `g_funcMap` 分派到对应 `XxxOperationCreate` 完成反序列化。两条 Python 路径（CSV 与 unittest）最终汇流到同一个 C++ 注册表。

**练习 2**：什么场景下应该选 Python `OperationTest` 而非 CSV？

> **答案**：当输入数据需要复杂生成逻辑（如 attention 的 mask、KV Cache 的 blockTable）、或需要多步前后处理、或想复用 `unittest` 的断言/夹具时，Python 类更灵活。反之，纯粹穷举「形状 × dtype × 参数」组合的大规模回归，CSV 更紧凑。

---

## 5. 综合实践

**任务**：为新算子 `FooOperation`（假设它有一个 `FooParam`，含 `int32_t axis`、`float scale`、`FooParam::Mode mode` 枚举、末尾 `uint8_t rsv[8]`）编写一套完整的 JSON 驱动测试骨架。这个任务把本讲三个模块串起来：注册（4.2）→ CSV 用例（4.3）→ golden（4.3）。

> 说明：这是**骨架编写型**实践。`FooOperation` 是为练习虚构的算子名（**示例算子，非项目真实算子**），重点是让你走通「新增一个算子测试要改哪几处」的流程，而非真实可编译代码。所有路径不要真的去改源码仓库。

**步骤 1：在 `operation_funcs.cpp` 增加注册函数**（参照 `LinearOperationCreate` 套路）

```cpp
// 示例代码：仿照 operation_funcs.cpp:475 的 LinearOperationCreate 写法
static atb::Status FooOperationCreate(const nlohmann::json &paramJson, atb::Operation **op)
{
    atb::infer::FooParam param;                                  // 步骤1：默认构造
    if (paramJson.contains("axis")) {                            // 步骤2：逐字段 contains 回填
        param.axis = paramJson["axis"].get<int32_t>();
    }
    if (paramJson.contains("scale")) {
        param.scale = paramJson["scale"].get<float>();
    }
    if (paramJson.contains("mode")) {                            // 枚举字段：显式转型
        param.mode = atb::infer::FooParam::Mode(paramJson["mode"].get<int32_t>());
    }
    if (paramJson.contains("rsv")) {                             // 步骤4：rsv 单独拷贝（反例构造用）
        for (size_t i = 0; i < paramJson["rsv"].size(); i++) {
            param.rsv[i] = paramJson["rsv"].at(i).get<uint8_t>();
        }
    }
    return CreateOperation(param, op);                           // 步骤5：进入正式创建 + rsv 闸门
}
```

**步骤 2：登记进 `g_funcMap`**（键名 = CSV 的 `OpName`）

```cpp
// 在 operation_funcs.cpp:2528 的 g_funcMap 里追加一行
{"FooOperation", &FooOperationCreate},
```

**步骤 3：在 `data_generation.py` 增加 golden 类**（类名 = 算子名）

```python
# 示例代码：仿照 data_generation.py 的 ElewiseOperation 写法
class FooOperation(DataGen):
    @staticmethod
    def golden(in_tensors, op_params):
        p = json.loads(op_params)
        out = in_tensors[0] * p.get("scale", 1.0)     # CPU 参考实现，示例语义为「乘 scale」
        return [out]

    @staticmethod
    def get_op_type(op_params):
        return OpTypes.COMPUTE_FLOAT                   # 浮点计算类 → 浮点精度标准
```

**步骤 4：在 `tests/apitest/opstest/csv/` 新建 `foo.csv`**，写正例和反例各一条（`|` 分隔，对照 elewise.csv 表头）：

```csv
CaseNum|CaseName|OpName|OpParam|InNum|InDType|InFormat|InShape|OutNum|OutDType|OutFormat|OutShape|DataGenType|DataGenRange|InTensorFile|OutTensorFile|ExpectedError|TestType|TestLevel|FromModel|SocVersion
1|FooBasic|FooOperation|{"axis":1,"scale":2.0,"mode":0}|1|float16|nd|4,8|1|float16|nd|4,8|random|0,1|||NO_ERROR||||Ascend910B
2|FooBadRsv|FooOperation|{"scale":1.0,"rsv":[1,0,0,0,0,0,0,0]}|1|float16|nd|4,8|1|float16|nd|4,8|random|0,1|||C:ERROR_INVALID_PARAM||||Ascend910B
```

**步骤 5：自检清单**（这是本实践的核心产出）：

- [ ] 注册函数里每个字段都用了 `contains` 守卫（除必填字段外）？
- [ ] 枚举 `mode` 是否写了显式 `Mode(...)` 转型？
- [ ] `rsv` 拷贝循环的元素类型与 `FooParam::rsv` 的元素类型一致（`uint8_t` 还是 `int8_t`）？
- [ ] `g_funcMap` 的键名 `"FooOperation"` 与 CSV 的 `OpName` 列、`data_generation.py` 的类名三者**完全一致**（大小写敏感）？
- [ ] 反例的 `ExpectedError` 用了正确的阶段前缀（`C:` 表示期望在 Create 阶段失败）？
- [ ] `golden` 函数在 CPU 上计算、返回 list、且不使用 NPU？

**预期结果**：经过这五步，`FooOperation` 就具备了被 CSV 引擎和 Python `OperationTest` 两条路径测试的能力。正例用例应一路跑通四段、与 golden 的 `allclose` 通过；反例用例应在 Create 阶段因 rsv 非 0 被 `ERROR_INVALID_PARAM` 拦截，与期望错误码匹配判通过。

**待本地验证**：步骤 1-2 修改的是 C++ 源码，需 `bash scripts/build.sh testframework` 重新编译 `libatb_test_framework.so` 才生效；步骤 4 的 CSV 运行需 NPU 环境。无 NPU 时，重点完成步骤 5 的自检清单。

## 6. 本讲小结

- **测试是分层的**：`framework/` 是引擎、`apitest/opstest` 是算子级（CSV + Python 双轨）、`unittest` 是框架内部 GTest、`infratest` 是基础设施、`cinterface` 是 C 接口、`kernelstest`/`fuzztest` 分别测 Kernel 与鲁棒性。选对目录是写用例的第一步。
- **`operation_funcs.cpp` 是 JSON 与 Param 的翻译官**：每个算子一个 `XxxOperationCreate`，用 `contains` 守卫逐字段回填、枚举显式转型、rsv 单独拷贝，最后调模板版 `CreateOperation`；所有函数登记进 `g_funcMap`，由字符串版 `CreateOperation(opName, param, &op)` 运行时分派。
- **CSV 测试是数据驱动的四段式**：`CsvOpsTest.run_one_case` 按 Create/InferShape/Setup/Execute 切分，每段比对 `ExpectedError`（带阶段前缀）并计时；精度靠 `data_generation.py` 里按算子名动态 `eval` 的 `golden` 函数，按 `OpTypes` 分档比对。
- **Python `OperationTest` 是轻量灵活的另一条路径**：子类只需实现 `golden_calc` 和 `test_xxx`，`execute` 内部自动完成设参、执行、`allclose(0.02, 0.02)` 比对；与 CSV 共用同一个 `OperationTorch` C++ 桥接。
- **注册名一致是铁律**：CSV 的 `OpName`、`g_funcMap` 的键名、`data_generation.py` 的类名、Python 测试传入的名字必须完全一致，这是「字符串入口」分派正确的前提。
- **正例与反例同引擎**：区别只在 `ExpectedError` 字段——`NO_ERROR` 是正例要跑通，`C:/I:/S:` + 错误码是反例期望在对应阶段失败；rsv 非 0 是构造反例的常用手段，验证 u6-l4 的版本闸门。

## 7. 下一步学习建议

- **动手跑一遍**：在有 NPU 的环境执行 `bash scripts/build.sh testframework` 后 `source output/atb/set_env.sh`，挑一个简单算子（如 `elewise.csv`）跑 `-n 1`，亲眼看到 `Result= succ` 与各阶段耗时，把本讲的抽象流程落地。
- **向下游深入**：本讲的精度比对只讲了用法，若想理解「为什么不同 `OpTypes` 用不同容差」，可阅读 `tests/framework/python/CsvOpsTestTool/atb_csv_ops_test.py` 里 `__error_percent`、`__precision_eb_percent`、ULP 误差等比对函数的完整实现，并结合 `tests/apitest/opstest/python/pythontools/new_standard_precison.py`。
- **向基础设施深入**：若关心框架内部机制的正确性（而非算子语义），转向 `tests/unittest/core/` 下的 GTest 用例，如 `test_op_param_funcs.cpp`（验证 `OPERATION_PARAM_FUNCS` 宏与 rsv 闸门）、`test_aclnn_executor_cache.cpp`（验证 u3-l3 讲的 executor 缓存）、`test_graph_operation.cpp`（验证 u5-l2 的图算子）。
- **衔接性能与调试**：本讲的 `SetupTime/ExecuteTime/SyncTime` 已经是 Host 侧耗时，结合 u7-l2（日志与 Profiling）的 `ProfStats`，可以定位一个算子是卡在 Host 下发还是 Device 计算，呼应 u1-l1 的 Host Bound 主题。
- **贡献一个真实用例**：参照综合实践的五步法，为仓库里一个**尚无完整 CSV 用例**的算子补一套正反例，这是从「读懂测试」到「参与测试」的最短路径。
