# 公共基础设施：common 目录走读

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `common/` 目录的整体分层：op_api 侧公共头、op_host 侧公共头、公共源码、框架插件源码、stub 桩代码各自服务谁。
2. 掌握 `aclnn_check.h`（架构判断）、`op_api_def.h`（公共常量）、`level2_base.h`（检查函数库）三个 op_api 侧公共头的能力边界。
3. 理解 `allocator_utils`（AiCPU 输出内存管理）和 `op_resource.h`（静态库资源登记宏）这两个"资源与内存"类工具的作用。
4. 掌握 op_host 侧 `tiling_util.h`（架构判断 + shape 归一化）和 `infershape_utils.h`（常量张量判断）的用法，并回顾 `tiling_base.h` / `tiling_templates_registry.h` 与它们的关系。
5. 了解本轮版本（394ba763）日志质量修复对 `common/stub` 下 `op_error_check.h` 的影响，以及新增的 `common/inc/aicpu/cv_aicpu_register.h` 的定位。
6. 在新算子开发中判断"哪些检查和内存操作不需要自己写"，直接复用公共层。

## 2. 前置知识

- **公共层（common layer）**：一个算子工程里反复出现的逻辑——空指针检查、dtype 支持列表判断、芯片架构判断、输出内存回填——如果每个算子都抄一遍，仓库会出现几百份近似代码。公共层就是把这些逻辑抽到一处，所有算子共享。判断标准很简单：**当你发现自己在 copy-paste 上一算子的某个函数时，先来 `common/` 找找**。
- **NpuArch / DAV 编号**：NPU 架构的内部编号，如 `DAV_2201`（对应 ascend910b 一代）、`DAV_3510`（RegBase 新架构，对应 ascend950 一代）。aclnn 层和 tiling 层都经常需要"按架构走不同分支"，而架构判断代码在两侧各有一份封装。
- **Host 侧与 Device 侧**：回顾 u1-l2 / u3-l1，`op_host` 在 CPU 上跑（注册、推导、tiling），`op_kernel` 在 NPU 上跑。公共层也按这个维度分：`common/inc/op_api` 服务 aclnn 接口层，`common/inc/op_host` 服务 tiling/infershape 层。
- **AiCPU 算子**：跑在 AI CPU（通用核）而非 AI Core（向量/矩阵核）上的算子，适合控制流密集型逻辑（如 NMS）。它的输出处理方式与 AiCore 算子不同，需要专门的内存回填工具——这正是 `allocator_utils` 存在的原因。
- **OP_CHECK 系列宏**：来自 CANN 包头文件 `aclnn_kernels/common/op_error_check.h`，提供 `OP_CHECK_NULL`、`OP_CHECK_DTYPE_NOT_SUPPORT` 等一行式检查宏。本仓库在 `common/stub/op_api/aclnn_kernels/common/op_error_check.h` 保留了一份 **stub 桩副本**——当依赖已安装的 CANN 包编译时（`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`），用这份副本代替包内头文件参与编译。`common/inc` 下的公共头是在这些宏之上的**再封装**。本轮 394ba763 提交（日志质量修复）调整了这份桩副本中一处 `OP_LOGE` 的换行拼接写法，详见 4.1.3。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [common/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/CMakeLists.txt) | 把 common 编成一个 OBJECT 库，供所有算子链接 |
| [common/inc/op_api/aclnn_check.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/aclnn_check.h) | op_api 侧芯片架构判断：`IsRegBase()` |
| [common/inc/op_api/op_api_def.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/op_api_def.h) | op_api 侧公共常量：`MAX_SUPPORT_DIMS_NUMS = 8` |
| [common/inc/op_api/level2_base.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/level2_base.h) | op_api 侧检查函数库：非空/shape/dtype 检查、按架构选 dtype 列表 |
| [common/inc/op_api/op_resource.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/op_resource.h) | 静态库合包时的算子资源登记宏 |
| [common/inc/op_api/allocator_utils.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/allocator_utils.h) | AiCPU 算子输出内存管理接口声明 |
| [common/src/common/allocator_utils.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/common/allocator_utils.cpp) | AiCPU 输出内存管理的实现（malloc/回填/释放记账） |
| [common/inc/op_host/tiling_util.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_util.h) / [common/src/op_host/tiling_util.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/op_host/tiling_util.cpp) | op_host（tiling）侧架构判断 `IsRegbaseSocVersion` 与 shape 归一化 `EnsureNotScalar` |
| [common/inc/op_api/infershape_utils.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/infershape_utils.h) | infershape 侧常量张量判断 `IsConstTensor`（u3-l2 已精读，本讲回顾） |
| [common/inc/op_host/tiling_base.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_base.h) | Tiling 流程模板基类（u3-l3 已精读，本讲回顾） |
| [common/inc/op_host/tiling_templates_registry.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_host/tiling_templates_registry.h) | tiling 模板注册表（u3-l4 已精读，本讲回顾） |
| [common/stub/op_api/aclnn_kernels/common/op_error_check.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/stub/op_api/aclnn_kernels/common/op_error_check.h) | CANN 包内 `op_error_check.h` 的桩副本，本轮日志质量修复涉及 |
| [common/inc/aicpu/cv_aicpu_register.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h) | 本轮新增的 AiCPU 算子注册宏（HOSTCPU 常量折叠框架），u8-l4 专门精读 |

另外两个目录本讲只点到为止：`common/src/framework/`（ONNX/TF 插件源码，u6-l3 专门讲；本轮 psroi_poolingV2 插件有一处日志文案调整）和 `common/stub/` 整体（当依赖已安装的 CANN 包编译时，代替包内 aclnn 符号的桩实现）。

## 4. 核心概念与源码讲解

### 4.1 common 目录总览：公共层如何被构建和链接

#### 4.1.1 概念说明

回顾 u1-l2 的仓库地图：`common/` 是与 `image/`、`objdetect/` 平级的公共代码目录。它不对应任何算子，而是被所有算子工程共享。理解公共层要回答三个问题：

1. **公共代码怎么编？** —— 单独编成一个 OBJECT 库。
2. **算子怎么用上它？** —— 两条路径：纯头文件（`#include` 即用，如 `aclnn_check.h`）和链接库（如 `tiling_util.cpp` 编进 `tiling_util_obj`）。
3. **公共层分几块？** —— 按"服务谁"分四块：op_api 侧（aclnn 检查）、op_host 侧（tiling/infershape 工具）、AiCPU 侧（输出内存）、打包侧（静态库资源登记）。本轮版本又新增了第五块的地基：aicpu 侧注册宏（`common/inc/aicpu/`，服务 HOSTCPU 常量折叠，u8-l4 精读）。

#### 4.1.2 核心流程

公共层的构建与消费关系：

```
common/CMakeLists.txt
    └── add_library(common_obj OBJECT)          ← OBJECT 库，编一次，多处链接
        └── file(GLOB src/op_host/*.cpp)        ← 目前 src/op_host 下只有 tiling_util.cpp
        └── target_include_directories(... common/inc ...)  ← 头文件搜索路径

算子工程消费公共层的两种方式：
  方式 A（纯头文件）：#include "op_api/aclnn_check.h"   ← static inline，无链接需求
  方式 B（链接库）  ：#include "op_host/tiling_util.h"  ← 需要 common_obj 参与链接
```

#### 4.1.3 源码精读

[common/CMakeLists.txt:L16-L34](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/CMakeLists.txt#L16-L34) 把 `src/op_host/*.cpp` 全部收进一个 OBJECT 库，并把 `common/inc` 加入头文件搜索路径——这就是算子里能直接写 `#include "op_host/tiling_util.h"` 的原因：

```cmake
if(BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG)
  npu_op_library(${COMMON_NAME}_obj TILING)
else()
  add_library(${COMMON_NAME}_obj OBJECT)
endif()

file(GLOB CPP_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/src/op_host/*.cpp")
```

末尾 [common/CMakeLists.txt:L38](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/CMakeLists.txt#L38) 的 `add_subdirectory(stub)` 只在依赖已安装 CANN 包编译时启用（`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`），此时 `common/stub/` 提供包内 aclnn 符号（contiguous、transpose、cast 等 L0 算子桩）的替身，见 [common/stub/opapi_stub.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/stub/opapi_stub.cpp)。

stub 目录里还有一份对包内公共检查头的镜像：[common/stub/op_api/aclnn_kernels/common/op_error_check.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/stub/op_api/aclnn_kernels/common/op_error_check.h)。本轮 394ba763（日志质量修复）改动了其中 `IsNullptr(const aclFloatArray*, ...)` 的日志写法——[L72-L80](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/stub/op_api/aclnn_kernels/common/op_error_check.h#L72-L80) 把原来用反斜杠续行、中间夹大量空白的多行日志字符串改成规范的单行文案：

```cpp
static inline bool IsNullptr(const aclFloatArray* floatArr, const char* name)
{
    if (floatArr == nullptr) {
        OP_LOGE(ACLNN_ERR_PARAM_NULLPTR,
                "Expected a value of type List[float] for argument %s but instead found type null.", name);
        return true;
    }
    return false;
}
```

修改前该字符串写作 `"...found type \\\n            null."`——续行符加缩进会被一并拼进日志，打印出夹杂大片空白的错乱文案。这是本轮日志质量扫描在全仓清理的典型模式（grid_sample、grid_sampler2d_grad、add_example 等算子文件中的同类写法一并修复），阅读这些文件的 `OP_LOGE` 时不必再为断行文案困惑。

此外，本轮还在 `common/inc/aicpu/` 下新增了 [cv_aicpu_register.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/aicpu/cv_aicpu_register.h)，提供 `OPS_CV_REGISTER_CPU_KERNELV2` 注册宏——它在 host 构建下经 weak 符号 `RegistCpuKernelV2` 注册、device 构建下回退到 `REGISTER_CPU_KERNEL`。这是 HOSTCPU 常量折叠框架的地基，本讲只需知道"common 多了一个 aicpu 子目录"，机制留待 u8-l4。

#### 4.1.4 代码实践

**实践：数一数 common 里"有源码的头"和"纯声明头"各有哪些。**

1. 实践目标：分清哪些公共设施是 include 即用、哪些需要链接 common_obj。
2. 操作步骤：在仓库根目录执行 `ls common/inc/op_api common/inc/op_host common/src/op_host common/src/common common/inc/aicpu`；再对照 `common/CMakeLists.txt` 里 `file(GLOB ...)` 收集的目录范围。
3. 需要观察的现象：`src/` 下只有两个 .cpp（`tiling_util.cpp`、`allocator_utils.cpp`），其余全是头文件；`inc/aicpu` 是本轮新增的目录。
4. 预期结果：`aclnn_check.h`、`level2_base.h`、`infershape_utils.h` 等是头文件即用（static 函数/inline）；`tiling_util.h`、`allocator_utils.h` 是声明，实现分别在两个 .cpp 中。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aclnn_check.h` 里的函数全部是 `static inline`，而 `tiling_util.cpp` 的函数不是？
**答案**：`aclnn_check.h` 被几十个算子的 aclnn 文件各自 include，若非 static，每个编译单元都会产生同名外部符号，链接时报"重复定义"；static inline 把符号限制在各自编译单元内。`tiling_util.cpp` 则只编一份（common_obj），所有算子链接同一份实现，避免代码膨胀。

**练习 2**：新增一个公共 tiling 工具函数应该放哪？
**答案**：声明加到 `common/inc/op_host/tiling_util.h`，实现加到 `common/src/op_host/tiling_util.cpp`——`common/CMakeLists.txt` 用 `file(GLOB src/op_host/*.cpp)` 收集源码，新文件自动参与编译，无需改构建脚本。

**练习 3**：为什么 `common/stub/op_api/aclnn_kernels/common/op_error_check.h` 要镜像一份 CANN 包内头文件？
**答案**：`BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG` 模式下编译环境未必有包内私有头，stub 副本保证算子源码在"依赖已安装包"与"全源码"两种编译模式下都能编过；代价是包内头更新时副本要人工同步——本轮日志修复就同时落在了这份副本上。

---

### 4.2 op_api 侧公共层：aclnn_check.h、op_api_def.h 与 level2_base.h

#### 4.2.1 概念说明

op_api 侧公共层解决 aclnn 实现文件（回顾 u2-l2 的三段结构）里反复出现的三类需求：

1. **芯片架构判断**（`aclnn_check.h`）：`IsRegBase()` 判断当前是否 RegBase 新架构（`DAV_3510`），决定走免 5HD 转换的新路径还是老路径。这是本仓库被引用最多的公共函数。
2. **公共常量**（`op_api_def.h`）：如最大维度数 `MAX_SUPPORT_DIMS_NUMS = 8`，避免各算子各写一个魔法数字。
3. **检查函数库**（`level2_base.h`）：把"3 个张量非空""输入输出 shape 一致""dtype 在支持列表内"等高频检查组合成可复用函数，并按架构挑选 dtype 支持列表。

#### 4.2.2 核心流程

`IsRegBase()` 的判断逻辑可以形式化为：

\[ \text{IsRegBase} = \mathbb{1}\left[\text{curArch} \in \{\text{DAV\_3510}\}\right] \]

它本质是把"RegBase 架构集合"这个**会随芯片演进扩充的事实**收敛到一处：将来 ascend950 后续型号加入时，只改 `aclnn_check.h` 里一个 `std::set`，所有算子自动跟随。`level2_base.h` 的 `GetDtypeSupportListV2/V3` 则把"同一算子在不同架构支持不同 dtype"的分支也收敛成公共函数：

```
GetDtypeSupportListV2(l1, l2):
    curArch = GetCurrentPlatformInfo().GetCurNpuArch()
    若 curArch ∈ {DAV_2201} ∪ RegBase集合 → 返回 l1（新架构支持列表）
    否则                                    → 返回 l2（其他芯片支持列表）
```

#### 4.2.3 源码精读

**架构判断**：[common/inc/op_api/aclnn_check.h:L23-L34](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/aclnn_check.h#L23-L34) 提供两个重载——无参版自查当前芯片，带参版判断指定架构：

```cpp
static inline bool IsRegBase()
{
    const static std::set<NpuArch> regbaseArch = {NpuArch::DAV_3510};
    auto curArch = GetCurrentPlatformInfo().GetCurNpuArch();
    return regbaseArch.find(curArch) != regbaseArch.end();
}
```

**公共常量**：[common/inc/op_api/op_api_def.h:L20-L23](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/op_api_def.h#L20-L23) 只定义了一个常量 `MAX_SUPPORT_DIMS_NUMS = 8`（张量最大维度数），被 `level2_base.h` 的维度检查使用。

**检查函数库**：[common/inc/op_api/level2_base.h:L36-L57](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/level2_base.h#L36-L57) 是典型的组合式非空检查——每个 `OP_CHECK_NULL` 失败即 `return false`，函数名直接编码"几个输入几个输出"：

```cpp
static bool CheckNotNull3Tensor(const aclTensor* t0, const aclTensor* t1, const aclTensor* t2)
{
    OP_CHECK_NULL(t0, return false);
    OP_CHECK_NULL(t1, return false);
    OP_CHECK_NULL(t2, return false);
    return true;
}
```

[common/inc/op_api/level2_base.h:L85-L93](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/level2_base.h#L85-L93) 的 `CheckSameShape1In1Out` 组合了 shape 一致性与维度上限两项检查：

```cpp
static bool CheckSameShape1In1Out(const aclTensor* self, const aclTensor* out)
{
    OP_CHECK_SHAPE_NOT_EQUAL(self, out, return false);
    OP_CHECK_MAX_DIM(self, MAX_SUPPORT_DIMS_NUMS, return false);
    return true;
}
```

[common/inc/op_api/level2_base.h:L175-L184](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/level2_base.h#L175-L184) 的 `GetDtypeSupportListV2` 按架构挑 dtype 列表：

```cpp
static const std::initializer_list<DataType>& GetDtypeSupportListV2(const std::initializer_list<op::DataType>& l1,
                                                                    const std::initializer_list<op::DataType>& l2)
{
    auto curArch = GetCurrentPlatformInfo().GetCurNpuArch();
    if (curArch == NpuArch::DAV_2201 || IsRegBase(curArch)) {
        return l1;
    } else {
        return l2;
    }
}
```

**真实消费方**：`aclnn_resize.cpp` 与 `aclnn_grid_sampler2d.cpp` 主要消费 `aclnn_check.h`（见 4.2.4 实践）；`level2_base.h` 的消费方是 upsample/grid_sampler 反向等一族算子，如 [image/grid_sampler2_d_grad/op_host/op_api/aclnn_grid_sampler2d_backward.cpp:L200](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/grid_sampler2_d_grad/op_host/op_api/aclnn_grid_sampler2d_backward.cpp#L200) 一行完成 5 个张量的非空检查（该文件本轮有一处日志文案修复，行号整体下移一行）：

```cpp
CHECK_RET(CheckNotNull2In1Out(gradOutput, input, grid, inputGrad, gridGrad), ACLNN_ERR_PARAM_NULLPTR);
```

注意一个诚实的观察：**公共检查函数是按需引入的**——`aclnn_resize.cpp` 自己写了 `CheckNotNull`（因为参数含 `aclFloatArray` 和 `char*`，与公共签名不匹配），只从公共层借 `IsRegBase`。公共层覆盖"高频同构"场景，个性化检查仍在算子本地。

#### 4.2.4 代码实践

**实践：在两个 aclnn 文件中定位公共层调用点（本讲核心实践的前半部分）。**

1. 实践目标：确认 `IsRegBase` 在真实算子中的三种典型用法。
2. 操作步骤：
   - 打开 [image/resize_bilinear_v2/op_api/aclnn_resize.cpp:L241-L249](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L241-L249)，观察 `GetExtendPathFlag` 用 **带参版** `IsRegBase(curArch)` 参与新架构集合的并集判断；
   - 打开 [image/resize_bilinear_v2/op_api/aclnn_resize.cpp:L272-L284](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L272-L284)，观察 `IsRegBase()` **无参版** 决定是否走"免 5HD 转换、直接调 L0 算子"的分支；
   - 打开 [image/grid_sample/op_api/aclnn_grid_sampler2d.cpp:L63-L76](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/grid_sample/op_api/aclnn_grid_sampler2d.cpp#L63-L76)，观察 `IsRegBase()` 用于判定 RegBase 架构是否支持 bilinear 直通路径。
3. 需要观察的现象：同一函数在两个算子里承担同一职责——**架构能力探测**，而非数据校验。
4. 预期结果：能说出三处调用分别控制什么分支（resize 的 extendFlag / resize 的 RegBase 直通路径 / grid_sample 的 bilinear 支持）。

#### 4.2.5 小练习与答案

**练习 1**：`IsRegBase()` 内的 `std::set` 为什么要加 `const static`？
**答案**：`static` 使这个集合只在首次调用时构造一次（函数级 static 局部变量），后续调用零开销；`const` 防止误改。对每个请求都会调用的 aclnn 检查路径来说，避免每次重建容器很重要。

**练习 2**：你的新算子有 2 个输入张量 + 1 个 `aclIntArray` 属性 + 1 个输出，非空检查能直接用 `level2_base.h` 的 `CheckNotNull3Tensor` 吗？
**答案**：不能直接用。`CheckNotNull3Tensor` 只接受 `aclTensor*`，`aclIntArray*` 类型不同；要么像 `aclnn_resize.cpp` 那样在算子本地写一个 4 参的 `CheckNotNull`（用 `OP_CHECK_NULL` 宏），要么向 `level2_base.h` 贡献一个新的公共函数。

**练习 3**：`GetDtypeSupportListV3`（[common/inc/op_api/level2_base.h:L186-L203](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/level2_base.h#L186-L203)）的 default 分支为什么"暂且默认当做 1971 处理"而不是报错？
**答案**：架构枚举会随新芯片扩充，公共函数赶不上所有新值；把未知架构按能力最接近的分支处理，保证新芯片上算子"先能跑"，具体支持范围仍由 def 文件与 binary.json 兜底（见 u3-l4/u3-l5）。这是一种"公共层宽松、注册层严格"的分工。

---

### 4.3 资源与内存：allocator_utils（AiCPU 输出内存）与 op_resource.h（静态库资源登记）

#### 4.3.1 概念说明

这一组公共设施解决"内存和资源从哪来、到哪去"：

- **allocator_utils**：AiCPU 算子的**输出内存回填**工具。AiCore 算子的输出由用户在 aclnn 接口外预分配（u2-l1）；但 AiCPU 算子（跑在 CPU 侧）输出 shape 可能依赖计算结果（如 NMS 保留框数量运行时才知道），需要在算子内部 malloc 一块内存，把 shape 和 data 挂到结果摘要上交还框架。这块"malloc 了谁、何时 free"需要记账，否则就是内存泄漏。
- **op_resource.h**：静态合包宏。仓库默认编动态库算子包；当需要把算子编成静态库合并进大包时（`scripts/util/build_opp_kernel_static.py` 流程），必须把每个算子的 tiling/infershape 注册资源、kernel 二进制资源汇总成一张表。`EXTERN_OP_RESOURCE`/`AUTO_GEN_OP_RESOURCE` 两个宏就是这张表条目的"声明+取值"样板。

#### 4.3.2 核心流程

AiCPU 输出内存的生命周期（以 NMS 为例）：

```
AiCPU 算子计算完毕
  → UpdateOutputDataTensor(dims, type, data, ...)      ← 公共函数
      1. ParamCheck：dims 非空、tensor/数据指针非空
      2. GetInputDataSize：dims 连乘 × dtype 字宽 = data_size（带溢出检查）
      3. malloc shape 缓冲 + malloc data 缓冲
      4. 把两个指针写入 ResultSummary（挂在输出 tensor 上交还框架）
      5. 指针登记进全局集合 g_allocated_ptr              ← 记账
  → 框架后续消费输出
  → DeleteOutputDataPtr(ptr)                            ← free 并从集合移除
  → CheckOutputDataPtr(ptr)                             ← 校验指针确系本模块分配
```

关键设计：**用全局 `std::unordered_set<uint64_t> g_allocated_ptr` 做分配记账**，free 前先查账，杜绝释放野指针。

#### 4.3.3 源码精读

**记账集合与入口**：[common/src/common/allocator_utils.cpp:L19-L34](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/common/allocator_utils.cpp#L19-L34)——匿名 namespace 里的全局集合 + 参数检查：

```cpp
namespace {
std::unordered_set<uint64_t> g_allocated_ptr;
}
...
uint32_t CpuKernelAllocatorUtils::ParamCheck(const std::vector<int64_t>& dims, const void* data_ptr,
                                             Tensor*& output_result_tensor)
{
    if (dims.empty()) { ... return KERNEL_STATUS_PARAM_INVALID; }
    KERNEL_CHECK_NULLPTR(output_result_tensor, KERNEL_STATUS_PARAM_INVALID, "output_result_tensor nullptr");
    KERNEL_CHECK_NULLPTR(data_ptr, KERNEL_STATUS_PARAM_INVALID, "data_ptr nullptr");
    return KERNEL_STATUS_OK;
}
```

**回填主流程**：[common/src/common/allocator_utils.cpp:L36-L108](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/common/allocator_utils.cpp#L36-L108) 的 `UpdateOutputDataTensor` 计算大小、malloc、memcpy、写 `ResultSummary`、登记指针。其中 [L70-L78](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/common/allocator_utils.cpp#L70-L78) 是把结果"挂到输出张量"的关键：

```cpp
aicpu::FWKAdapter::ResultSummary* result_summary =
    reinterpret_cast<aicpu::FWKAdapter::ResultSummary*>(output_result_tensor->GetData());
result_summary->raw_data_size = data_size;
result_summary->shape_data_size = shape_buff_size;
```

**查账释放**：[common/src/common/allocator_utils.cpp:L130-L153](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/common/allocator_utils.cpp#L130-L153)——`CheckOutputDataPtr` 先查集合再认账，`DeleteOutputDataPtr` 查到才 `free`：

```cpp
uint32_t CpuKernelAllocatorUtils::DeleteOutputDataPtr(const uint64_t data_ptr)
{
    auto find_data_ptr = g_allocated_ptr.find(data_ptr);
    if (find_data_ptr != g_allocated_ptr.end()) {
        free(reinterpret_cast<void*>(data_ptr));
        g_allocated_ptr.erase(find_data_ptr);
    } else {
        KERNEL_LOG_EVENT("DeleteOutputDataPtr invalid [%lu].", data_ptr);
    }
    return KERNEL_STATUS_OK;
}
```

**真实消费方**：[image/non_max_suppression_v3/op_kernel_aicpu/non_max_suppression_v3_aicpu.cpp:L239-L241](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/non_max_suppression_v3/op_kernel_aicpu/non_max_suppression_v3_aicpu.cpp#L239-L241)——NMS 的保留框数量运行时才知道，算子算完后用公共函数把动态 shape 的结果交还框架：

```cpp
auto ret = CpuKernelAllocatorUtils::UpdateOutputDataTensor(output_shape, DT_INT32, indices_data.get(), ...);
KERNEL_CHECK_FALSE((ret == KERNEL_STATUS_OK), KERNEL_STATUS_INNER_ERROR, "UpdateOutputDataTensor failed.")
```

**静态库资源宏**：[common/inc/op_api/op_resource.h:L18-L38](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/op_resource.h#L18-L38) 的 `EXTERN_OP_RESOURCE` 声明某算子的注册资源/二进制资源符号，`AUTO_GEN_OP_RESOURCE` 把它们拼成 `{算子名, {注册资源三元组, kernel资源, tuning资源}}` 的表项。诚实说明：这两个宏在本仓库算子代码里**没有直接调用点**，它们的消费者是 `scripts/util/build_opp_kernel_static.py` 生成的合包代码（见该脚本 [L285-L311](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/scripts/util/build_opp_kernel_static.py#L285-L311) 中生成的 `KernelResource()` 系列函数与 `OP_BINARY_RES` 类型定义）。日常开发单算子时不需要碰它。

#### 4.3.4 代码实践

**实践：走读 NMS 的输出内存链路（源码阅读型，无需 NPU 环境）。**

1. 实践目标：理解"输出 shape 动态"的算子如何交还结果。
2. 操作步骤：
   - 从 [common/inc/op_api/allocator_utils.h:L21-L29](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/allocator_utils.h#L21-L29) 的类声明出发，记住 5 个接口（ParamCheck / UpdateOutputDataTensor / CheckOutputDataPtr / DeleteOutputDataPtr / GetInputDataSize）；
   - 进入 `non_max_suppression_v3_aicpu.cpp` 的 L239 附近，向上找 `output_shape` 是怎么算出来的、向下找函数返回后谁消费 `ResultSummary`；
   - 回到 `allocator_utils.cpp`，追踪 `g_allocated_ptr` 中一个指针的"登记 → 查账 → 释放"全路径。
3. 需要观察的现象：UpdateOutputDataTensor 内部对空张量（data_size == 0）有专门分支（L75-L81），只挂 shape 不 malloc data。
4. 预期结果：能回答"AiCPU 算子的输出内存是谁 malloc 的、何时 free、如何防止释放野指针"三个问题。

#### 4.3.5 小练习与答案

**练习 1**：`GetInputDataSize` 里连续调用 `KERNEL_CHECK_ASSIGN_64S_MULTI` 是在防什么？
**答案**：防 64 位乘法溢出。维度连乘 `num_elements` 与"元素数 × 单元素字宽"两次乘法都可能溢出 int64，该宏在溢出时直接返回参数错误，避免用溢出后的小值 malloc 导致后续越界写。

**练习 2**：为什么不把 AiCPU 输出内存改成 `std::unique_ptr` 自动管理？
**答案**：因为内存的生命周期跨越算子边界——malloc 发生在算子进程内，而 free 由框架在消费完输出之后的另一个时机触发，中间隔着 `ResultSummary` 的裸指针交接。智能指针无法跨这条 C 风格边界传递所有权，所以采用"全局集合记账 + 显式 Delete"的方案。

---

### 4.4 op_host 侧公共层：tiling_util 与 infershape_utils

#### 4.4.1 概念说明

op_host 侧公共层服务 TilingFunc 和 InferShape 的编写（u3-l2、u3-l3 已深入机制，本讲只看公共层抽走了什么）：

- **tiling_util**：两个小而高频的工具。
  - `IsRegbaseSocVersion(context)`：tiling 侧的架构判断——与 `aclnn_check.h` 的 `IsRegBase` 判断同一个集合 `{DAV_3510}`，但取架构的途径不同（从 `gert::TilingContext`/`TilingParseContext` 的平台信息，而非 aclnn 层的 `GetCurrentPlatformInfo()`）。
  - `EnsureNotScalar(inShape)`：shape 归一化——把标量（0 维）统一当成 `{1}` 处理，让后续 `GetDim(0)` 之类的维度访问不会越界。
- **infershape_utils**：`IsConstTensor`——判断输入是否常量张量，支撑 resize 类算子"读 size 张量的值推输出 shape"（u3-l2 精读过）。
- **tiling_base.h / tiling_templates_registry.h**：回顾 u3-l3/u3-l4——前者把 tiling 流程模板化为 8 个钩子的基类，后者提供按 soc_version + priority 的多候选注册表。它们与 `tiling_util` 的分工是：**base/registry 管"流程骨架"，util 管"零散工具"**。

#### 4.4.2 核心流程

一个典型算子（如 grid_sample）在 op_host 侧对公共层的消费时序：

```
TilingParse（编译期）
  → GetPlatformInfo / GetCoreNumAiv / GetCoreMemSize(UB)   ← 取平台参数
  → compileInfo->regBase = Ops::Cv::OpTiling::IsRegbaseSocVersion(context)   ← 公共：架构判断
Tiling（运行期）
  → inputShape = Ops::Cv::OpTiling::EnsureNotScalar(x->GetStorageShape())   ← 公共：shape 归一化
  → 按 regBase / shape 选策略，SetTilingKey / SetBlockDim
```

#### 4.4.3 源码精读

**架构判断（tiling 版）**：[common/src/op_host/tiling_util.cpp:L24-L43](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/op_host/tiling_util.cpp#L24-L43)——内部同样持有一个 `{DAV_3510}` 集合，对 `TilingParseContext` 和 `TilingContext` 各暴露一个重载，都用 `PlatformAscendC` 包装平台信息取架构：

```cpp
bool IsRegbaseSocVersion(const gert::TilingContext* context)
{
    auto ascendcPlatform = platform_ascendc::PlatformAscendC(context->GetPlatformInfo());
    auto arch = ascendcPlatform.GetCurNpuArch();
    return IsRegbaseSocVersion(arch);
}
```

**shape 归一化**：[common/src/op_host/tiling_util.cpp:L22-L51](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/op_host/tiling_util.cpp#L22-L51)——`EnsureNotScalar` 在标量时返回静态的 `{1}` shape：

```cpp
static const gert::Shape g_vec_1_shape = {1};

const gert::Shape& EnsureNotScalar(const gert::Shape& inShape)
{
    if (inShape.IsScalar()) {
        return g_vec_1_shape;
    }
    return inShape;
}
```

**真实消费方一（grid_sample，本讲实践后半部分的对象）**：[image/grid_sample/op_host/grid_sample_tiling.cpp:L342](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/grid_sample/op_host/grid_sample_tiling.cpp#L342) 在编译期解析阶段把架构结论存进 CompileInfo，供运行期 tiling 取用：

```cpp
compileInfo->regBase = Ops::Cv::OpTiling::IsRegbaseSocVersion(context);
```

**真实消费方二（roi_pooling_with_arg_max）**：[objdetect/roi_pooling_with_arg_max/op_host/arch35/roi_pooling_with_arg_max_tiling.cpp:L146](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_pooling_with_arg_max/op_host/arch35/roi_pooling_with_arg_max_tiling.cpp#L146) 在取输入 shape 后立刻归一化，再安全地按 NCHW 取维：

```cpp
gert::Shape inputFMShape = Ops::Cv::OpTiling::EnsureNotScalar(inputFM->GetStorageShape());
```

**infershape 侧**：[common/inc/op_api/infershape_utils.h:L23-L32](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/op_api/infershape_utils.h#L23-L32) 的 `IsConstTensor` 被 resize/col2im/crop 等十余个算子的 infershape 引用（`grep infershape_utils` 可验证）。

一个值得注意的横向对比：`examples/add_example/op_host/add_example_tiling.cpp:L61` 和 `experimental/objdetect/roi_align_grad/op_host/roi_align_grad_tiling.cpp:L34` 各自**复制了一份本地 `EnsureNotScalar`**，而不是用公共版——公共层是"存量代码逐步收敛"的，新写 tiling 时应直接 `#include "op_host/tiling_util.h"`（`scripts/opgen/template/add` 的新算子模板已是这样做的）。

#### 4.4.4 代码实践

**实践：完成本讲核心实践的后半部分——grid_sample 的 op_host 侧公共调用点。**

1. 实践目标：把"公共层调用点"的盘点从 aclnn 层延伸到 tiling 层。
2. 操作步骤：
   - 打开 [image/grid_sample/op_host/grid_sample_tiling.cpp:L333-L350](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/image/grid_sample/op_host/grid_sample_tiling.cpp#L333-L350)，找到 `Ops::Cv::OpTiling::IsRegbaseSocVersion(context)`；
   - 在文件内搜索 `EnsureNotScalar`，看 grid_sample 是否用到 shape 归一化（若没有，思考为什么：4 维输入天然不是标量）；
   - 执行 `grep -rn "OpTiling::" image/grid_sample/op_host/`，列出全部公共层调用。
3. 需要观察的现象：`regBase` 被存进 `compileInfo` 后，在运行期 TilingFunc 里如何影响 TilingKey 或策略选择（顺着 `compileInfo->regBase` 搜索使用点，如该文件 L244-L250 的 bilinear 直通分支）。
4. 预期结果：得出结论——grid_sample 在 op_host 侧只消费 `tiling_util` 的架构判断，shape 归一化因输入恒为 4 维而无需使用。

#### 4.4.5 小练习与答案

**练习 1**：`IsRegBase()`（op_api 侧）与 `IsRegbaseSocVersion()`（op_host 侧）为什么是两份代码而不是一份？
**答案**：两层获取架构信息的 API 不同——aclnn 层用 `GetCurrentPlatformInfo()`（来自 `opdev/platform.h`），tiling 层只能从 `gert::TilingContext/TilingParseContext` 的平台信息经 `PlatformAscendC` 包装获取，且两侧链接的库也不同（op_api 链 opapi 运行库，op_host 链 tiling_api）。能共享的只有"架构集合 = {DAV_3510}"这个事实，目前两侧各持一份集合，扩充新架构时需要两处同步——这是阅读公共层时应意识到的维护约定。

**练习 2**：`EnsureNotScalar` 返回的是 `const gert::Shape&`（引用），返回静态局部 `g_vec_1_shape` 安全吗？
**答案**：安全。`g_vec_1_shape` 是 namespace 级 static 对象，生命周期覆盖整个进程，返回其引用不会悬空；同时函数签名保证调用方不能修改它。若返回的是局部非 static 对象的引用才是悬空错误。

**练习 3**：为什么 `scripts/opgen/template/add`（新算子生成模板）默认 include 了 `tiling_util.h` 和 `tiling_templates_registry.h`？
**答案**：模板代表官方推荐的起手式——新算子的 tiling 大概率需要架构判断（RegBase 分支）与模板注册（多候选降级），预置 include 提醒开发者直接复用公共层，避免再复制本地副本（如 add_example 旧代码里那样本地的 `EnsureNotScalar`）。

## 5. 综合实践

**任务：为两个算子各产出一份《公共层依赖清单》。**

以 `aclnn_resize.cpp`（image/resize_bilinear_v2/op_api/）和 `aclnn_grid_sampler2d.cpp`（image/grid_sample/op_api/）为对象，再把 grid_sample 的 op_host（grid_sample_tiling.cpp）纳入盘点：

1. 对每个文件执行 `grep -n "common\|IsRegBase\|IsRegbase\|level2_base\|OpTiling::" <文件>`，列出所有命中行。
2. 为每个调用点标注：来自哪个公共头（`aclnn_check.h` / `tiling_util.h` / ...）、承担什么职责（架构判断 / 非空检查 / dtype 检查 / shape 归一化）、控制了哪个分支。
3. 参考答案要点（可对照检验）：
   - `aclnn_resize.cpp`：① L25 include aclnn_check.h；② L244 `IsRegBase(curArch)` 参与 extendFlag 判断；③ L272 `IsRegBase()` 决定 RegBase 直通路径。
   - `aclnn_grid_sampler2d.cpp`：① L26 include aclnn_check.h；② L71 `IsRegBase()` 判定 RegBase 是否支持 bilinear；③ L215 `IsRegBase(curArch)` 决定非 bilinear 模式的降级路径。
   - `grid_sample_tiling.cpp`：L342 `IsRegbaseSocVersion(context)` 把 regBase 写入 CompileInfo。
4. 最后回答总结问题：**哪些检查和内存操作已经由公共层封装、不必自己写？**——架构判断（两个侧各一份）、组合式非空/shape/dtype 检查（level2_base）、按架构挑 dtype 列表（GetDtypeSupportListV2/V3）、shape 标量归一化（EnsureNotScalar）、常量张量判断（IsConstTensor）、AiCPU 动态输出的 malloc/回填/free 记账（allocator_utils）。**哪些仍需算子本地写？**——与公共签名不匹配的个性化参数检查（如含 aclFloatArray/char* 的非空检查）、算子特有的语义校验（如 resize 的 scales 与输出 H/W 匹配检查）。

## 6. 本讲小结

- `common/` 按服务对象分四块：op_api 侧检查层（aclnn_check / op_api_def / level2_base / infershape_utils）、op_host 侧 tiling 工具（tiling_util，src 编入 common_obj OBJECT 库）、AiCPU 输出内存管理（allocator_utils）、静态合包资源宏（op_resource.h，仅打包脚本消费）；本轮又新增了 aicpu 注册宏目录（cv_aicpu_register.h，HOSTCPU 常量折叠地基，u8-l4 精读）。
- `IsRegBase()` 是全仓库被引用最多的公共函数：op_api 侧与 op_host 侧各有一份实现（取架构的 API 不同），共享"RegBase = {DAV_3510}"这一事实，扩充架构时两侧需同步。
- `level2_base.h` 用组合函数（CheckNotNullN/CheckSameShape.../GetDtypeSupportListV2）封装高频同构检查，但按需引入——参数形态不匹配时算子仍本地写检查。
- `allocator_utils` 用全局 `g_allocated_ptr` 集合为 AiCPU 动态输出内存记账：UpdateOutputDataTensor 分配并挂 ResultSummary，DeleteOutputDataPtr 查账后 free，CheckOutputDataPtr 防野指针。
- `tiling_util` 的 `EnsureNotScalar` 把标量 shape 归一化为 `{1}`，新算子模板已默认引入；存量代码（add_example、roi_align_grad）还有本地副本，属待收敛的历史遗留。
- 本轮 394ba763 日志质量修复落在 common 的三处：stub 副本 `op_error_check.h` 的多行拼接日志改为单行、`psroi_poolingV2_onnx_plugin.cpp` 的顿号改英文逗号、新增 `cv_aicpu_register.h`——读写日志文案时注意这一轮的全仓清理。

## 7. 下一步学习建议

本讲补全了 op_host 公共层，第三单元（算子工程主链路）到此完整。接下来：

- **u4-l1（Ascend C Kernel 基础）**：跨到 Device 侧，看 kernel 如何消费 tiling 阶段算好的切分方案。
- 若对框架适配感兴趣，可提前看 `common/src/framework/` 下的 ONNX 插件（u6-l3 会精读 `onnx_common.h` 与插件源码组织）。
- 若计划贡献算子，建议顺手读 `scripts/opgen/template/add/`——新算子模板对公共头的默认引用就是官方推荐的复用姿势，为 u8-l2 自定义算子开发做准备；对 AiCPU 注册宏与常量折叠感兴趣的可预习 `common/inc/aicpu/cv_aicpu_register.h`（u8-l4）。
