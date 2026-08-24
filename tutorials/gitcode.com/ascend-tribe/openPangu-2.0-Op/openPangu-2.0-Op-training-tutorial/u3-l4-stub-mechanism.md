# stub 桩机制：op_tiling 与 op_api 的可替换实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 **桩（stub）** 在本仓库中的存在意义：为什么算子库的源码要能在「没有完整 CANN 内置实现、没有 NPU 硬件」的宿主机上完成编译和单元测试。
2. 区分本仓库的两大类公共桩——`common/stub/op_tiling`（tiling 侧：`tbe_tiling_api`、`op_cache_tiling`、tuning 注册）与 `common/stub/op_api`（aclnn 侧：`aclnn_kernels` 与 `level0` 两层头文件镜像 + `opapi_stub.cpp` 替身实现），并了解第三类由脚本生成的运行时桩（`runtime_stubs.cpp`）与算子本地桩（`ut_stub_*.cpp`）。
3. 读懂桩的三个挂接点：编译期 include 路径遮蔽、链接期替换库（`libopapi_stub.so`）、运行期假实现（`rt*` 接口族）。
4. 在为一个算子补 op_api 单元测试时，知道哪些符号需要自己打桩、桩体写在哪、如何挂进 CMake（「在 UT 中正确链接所需 stub 符号」）。
5. 对「公共库里的代码」保持核查习惯：用 grep 确认每个桩的真实调用者，区分「正在使用」与「已备而未用」。

## 2. 前置知识

本讲承接 u2-l5（aclnn 两段式接口）与 u3-l2（common 公共组件总览），先把几个概念说透：

- **桩（stub）**：一个「签名与真品完全一致、行为退化成最小动作」的替身实现。调用方代码一行不改，编译/链接/运行时命中的却是假实现。它和 mock 的区别：桩通常不校验调用次数，只保证「调用能通过、返回值可用」。
- **三种打桩手段**（本仓库三种都用到了）：
  1. **头文件遮蔽（编译期）**：把 include 搜索路径里排在前的目录放一份同名头文件，让 `#include "xxx.h"` 解析到替身声明。
  2. **链接替换（链接期）**：头文件还用真品的，但链接时用另一个库（如 `libopapi_stub.so`）提供函数体。符号名相同，链接器无所谓真伪。
  3. **运行期假实现**：对 `dlopen`/动态符号（如 `rt*` 运行时接口）直接在可执行文件里写一个同名函数，抢先满足符号解析。
- **承接 u2-l5 的 aclnn 分层**：op_api 内部分两级——L2（`aclnn_` 前缀文件，对外契约）与 L0（`namespace l0op`，补默认值、做布局预处理的内部算子层，如 `l0op::Contiguous`/`l0op::Transpose`）。本讲的 op_api 桩打的就是 L0 层。
- **承接 u1-l4 的构建目标**：全量构建产出 `optiling`（`libcust_opmaster_rt2.0.so`）与 `opapi`（`libcust_opapi.so`）两个共享库；各算子的 `_tiling.cpp` 通过 `target_sources(optiling ...)` 挂进前者，`op_api/*.cpp` 挂进后者。桩目录也挂在这两个目标上。
- **承接 u2-l3 的 Tiling 概念**：tiling 是 Kernel 启动前 Host 侧的「作战规划」，产出 TilingData/tilingKey/workspace。op_tiling 桩替身的就是 tiling 过程中可能调用的 CANN 内置服务（TBE 切分查询、切分结果缓存、调优 bank 查询）。
- **CANN 包 vs 本仓库**：`tbe_tiling_api.h`、`op_cache_tiling.h`、`aclnn_kernels/*.h` 这些名字在 CANN 安装包里也有同名真品。本仓库 stub 目录放的是**镜像/替身**，用于在拿不到真品实现库时依然能编译链接。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/common/stub/op_tiling/tbe_tiling_api.h` | tiling 桩①：`GetTbeTiling` 三个重载的声明 + Conv3D 反传相关的 tiling 结构体（`Conv3dBackpropV2TBETilingData` 等） |
| `ascendc/src/ops-transformer/common/stub/op_tiling/tbe_tiling_api.cpp` | tiling 桩①的替身实现：三个 `GetTbeTiling` 全部「什么都不算，return true」 |
| `ascendc/src/ops-transformer/common/stub/op_tiling/op_cache_tiling.h` | tiling 桩②：切分缓存接口声明（`TilingPrepareForOpCache` / `GenTiling`） |
| `ascendc/src/ops-transformer/common/stub/op_tiling/op_cache_tiling.cpp` | tiling 桩②的替身实现：同样全部 no-op 返回 true |
| `ascendc/src/ops-transformer/common/stub/op_tiling/cache_tiling_data.h` | 切分缓存的数据契约：`BatchmatmulCompileParas` / `BatchmatmulRunParas` / `CacheTilingData` 三个结构体 |
| `ascendc/src/ops-transformer/common/stub/op_tiling/runtime_kb_api.h` | tiling 桩③：调优知识库查询 `RuntimeKb::QueryBank` 声明 |
| `ascendc/src/ops-transformer/common/stub/op_tiling/register/tuning_tiling_registry.h` | tuning 注册框架：`BEGIN_TUNING_TILING_DEF` 反射宏 + `REGISTER_TUNING_TILING_CLASS` 静态注册宏 |
| `ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp` | op_api 桩的**总实现文件**：给 40 多个 `l0op::` 函数提供「返回 self / 返回 true」的替身函数体 |
| `ascendc/src/ops-transformer/common/stub/op_api/level0/add.h` | level0 层 L0 算子桩头文件示例：只声明 `l0op::Add` |
| `ascendc/src/ops-transformer/common/stub/op_api/level0/gather_v2.h` | level0 桩：`GatherV2` / `GatherV2WithImplMode` 两个声明 |
| `ascendc/src/ops-transformer/common/stub/op_api/level0/matmul_v2tov3.h` | level0 桩：`MmCheckHitV3Shape`（仅声明，`opapi_stub.cpp` 未提供函数体） |
| `ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/contiguous.h` | aclnn_kernels 层桩：`ContiguousParam` 结构 + `Contiguous` 等布局预处理声明 |
| `ascendc/src/ops-transformer/common/stub/op_api/CMakeLists.txt` | `ENABLE_TEST` 时把 `opapi_stub.cpp` 编成 `libopapi_stub.so` |
| `ascendc/cmake/variables.cmake` | `OP_TILING_INCLUDE` 里塞入 `common/stub/op_tiling`（编译期遮蔽的挂接点） |
| `ascendc/cmake/func.cmake` | `add_opapi_modules`：UT 模式把 `${UT_PATH}/op_api/stub` 排在 CANN 头文件路径之前 |
| `ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt` | op_api UT 主构建脚本：链接 `opapi_stub`、调用脚本生成 `runtime_stubs.cpp` |
| `ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py` | 生成 `rt*` 运行时接口假实现 + 安装包 `.o/.json` 占位 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/ut_stub_metadata.cpp` | 算子本地桩范例：为单个算子的 UT 手写三个桩 |

## 4. 核心概念与源码讲解

### 4.1 stub 总论：三类替身与目录地图

#### 4.1.1 概念说明

先回答「为什么需要桩」。

算子库的宿命是**依赖重**：op_host 的 tiling 可能要查 CANN 内置的切分库（如 BatchMatMul 的缓存切分），op_api 的 L0 层要调 CANN/torch_npu 的基础算子（`Contiguous`、`Transpose`、`Add`……），UT 跑起来还要过 runtime 驱动接口（`rtKernelLaunchEx`、`rtMalloc`……）。这些真品有的在 NPU 环境里、有的在闭源库里。如果编译和测试硬依赖它们，那么：

- CI 机器没装 NPU，UT 编译直接失败；
- tiling 结果依赖硬件查询，同一输入每次结果可能不同，断言没法写。

桩把这两层依赖都换成「可控的假货」：**接口签名保真，行为最小化**。于是同一个算子源文件，在真机上链接真库执行真计算，在 UT 里链接桩库走通逻辑分支——这就是标题里「可替换实现」的含义。

#### 4.1.2 核心流程

一帧 op_api 单元测试从编译到运行，桩在三个时机介入：

```text
编译期（遮蔽）  include 路径前置 stub 目录 → #include 解析到替身声明
     ↓
链接期（替换）  target_link_libraries(... opapi_stub) → l0op:: 符号来自桩库
     ↓
运行期（假实现） 可执行文件里编译进了 runtime_stubs.cpp → rt* 调用不会碰到驱动
```

本仓库桩的完整目录地图：

```text
common/stub/
 ├─ op_tiling/                       ← tiling 侧桩（服务 optiling 库与 tiling UT）
 │   ├─ tbe_tiling_api.h/.cpp        TBE 切分查询替身（Conv3D 反传族）
 │   ├─ op_cache_tiling.h/.cpp       切分缓存接口替身
 │   ├─ cache_tiling_data.h          切分缓存数据结构（纯头文件）
 │   ├─ runtime_kb_api.h/.cpp        调优知识库 QueryBank 替身
 │   └─ register/                    tuning 反射注册宏三件套
 └─ op_api/                          ← aclnn 侧桩（服务 opapi 库与 op_api UT）
     ├─ opapi_stub.cpp               所有 l0op:: 替身函数体（308 行）
     ├─ aclnn_kernels/               布局预处理层头文件镜像（7 个 + common/op_error_check.h）
     └─ level0/                      L0 基础算子头文件镜像（32 个）
```

另有两组不在 `common/stub` 下、但同属桩体系的代码：

- `src/tests/ut/framework_normal/op_api/stub/opdev/`（`platform.cpp`、`nnopbase.cpp`）——UT 框架自己的平台/算子基座替身；
- `generate_opapi_stub.py` 生成的 `runtime_stubs.cpp` 与各算子自带的 `ut_stub_*.cpp`（见 4.4）。

#### 4.1.3 源码精读

**挂接点一：tiling 侧的编译期遮蔽。** CMake 变量 `OP_TILING_INCLUDE` 是所有 tiling 源码（以及 tiling UT 用例，见 [ut.cmake:L53-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L53-L62)）的公共 include 路径，其中显式塞进了 stub 目录：

- [variables.cmake:L243-L248](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/variables.cmake#L243-L248)：`${OPS_TRANSFORMER_DIR}/common/stub/op_tiling` 被列入 `OP_TILING_INCLUDE`——任何 tiling 源码写 `#include "tbe_tiling_api.h"`，会先在这个目录命中桩版本，而不是 CANN 包里的同名头文件。

**挂接点二：stub 目录自身的 CMake。** 两个桩目录的 CMakeLists 都用 `file(GLOB_RECURSE)` 收集 `.cpp`，再把配置挂到顶层定义的 `optiling` / `opapi` 目标上：

- [stub/op_tiling/CMakeLists.txt:L13-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/CMakeLists.txt#L13-L26)：glob 出 `OP_TILING_FILES`，但 `add_library(optiling SHARED ...)` 那行被注释掉了，只保留对 `optiling` 目标的编译宏与 include 配置——也就是说**当前仓库里这几个桩 `.cpp` 并没有被编进 `libcust_opmaster_rt2.0.so`**（见 4.2.4 的核查）。
- [stub/op_api/CMakeLists.txt:L27-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/CMakeLists.txt#L27-L43)：`if(ENABLE_TEST)` 分支里 `add_library(opapi_stub SHARED ${OP_API_FILES})` 把 `opapi_stub.cpp` 编成独立的 `libopapi_stub.so`。注意 `ENABLE_TEST` 正是 `build.sh -u`（UT 模式）设置的开关——**op_api 桩只在测试构建里存在，正式算子包里没有它**。

**挂接点三：UT 头文件路径前置。** [func.cmake:L629-L636](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake#L629-L636)：`add_opapi_modules` 在 `UT_TEST_ALL OR OP_API_UT` 时设置 `OPAPI_UT_DEPEND_INC = ${UT_PATH}/op_api/stub`，并把它放在 `target_include_directories` 的**第一位**、CANN 包路径之前——opdev 层的头文件（如 `platform.h`）优先解析到 UT 框架的替身版本。

顶层 [CMakeLists.txt:L309-L318](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L309-L318) 遍历算子目录时还有一个防御性判断：目录等于 `common/stub` 时 `continue()` 跳过，避免把桩目录当算子目录处理。

#### 4.1.4 代码实践

**实践：给桩目录做一次「人口普查」。**

1. 实践目标：用只读命令摸清 stub 目录的规模与构成，为后续精读建立索引。
2. 操作步骤（在 `training/ascendc` 目录下执行）：

```bash
# ① 目录构成
find src/ops-transformer/common/stub -type f | sed 's|.*/||; s/[^_]*$//' | sort | uniq -c | sort -rn | head
# ② level0 桩的数量与总行数
ls src/ops-transformer/common/stub/op_api/level0/ | wc -l
wc -l src/ops-transformer/common/stub/op_api/level0/*.h | tail -1
# ③ opapi_stub.cpp 实际包含了几个 level0 头文件（应输出 14）
grep -c '#include "level0/' src/ops-transformer/common/stub/op_api/opapi_stub.cpp
```

3. 需要观察的现象：level0 有 32 个头文件、共 810 行，但 `opapi_stub.cpp` 只 include 了其中 14 个——**「有声明」不等于「有替身函数体」**，这个差距是 4.3 的重点。
4. 预期结果：得到一张「目录 → 文件数 → 是否有实现」的统计表。本实践纯只读，无需 NPU 环境。

#### 4.1.5 小练习与答案

**练习 1**：同样是替身，「头文件遮蔽」和「链接替换」分别作用于哪个构建阶段？本仓库哪个桩用了哪种？
答：遮蔽作用于**编译期**（决定 `#include` 解析到谁的声明），链接替换作用于**链接期**（决定符号引用绑定到谁的函数体）。`common/stub/op_tiling` 进入 `OP_TILING_INCLUDE` 属于遮蔽；`libopapi_stub.so` 被 UT 目标链接属于链接替换。

**练习 2**：为什么 `opapi_stub` 库只在 `ENABLE_TEST` 时才创建，而正式包构建不编它？
答：正式算子包运行在真实 CANN/NPU 环境里，`l0op::` 符号由环境中真实的 `libopapi.so` 等提供；桩只服务于宿主机 UT。若正式包里也带桩，会用假实现顶掉真计算，属于事故。

**练习 3**：`stub/op_tiling/CMakeLists.txt` 里 glob 出的 `OP_TILING_FILES` 现在被谁消费？
答：没有被消费——`add_library(optiling SHARED ${OP_TILING_FILES})` 处于注释状态（[stub/op_tiling/CMakeLists.txt:L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/CMakeLists.txt#L15)），该 CMakeLists 目前只给 `optiling` 目标追加编译宏与 include 路径。读构建脚本和读公共库一样，要以实际生效的代码为准。

### 4.2 op_tiling 桩：tbe_tiling_api 与 op_cache_tiling

#### 4.2.1 概念说明

op_tiling 桩替身的是 tiling 过程中三类「CANN 内置服务」：

1. **TBE 切分查询**（`tbe_tiling_api.h`）：老一代 TBE（Tensor Boost Engine）算子的切分算法以闭源库形式存在，`GetTbeTiling` 系列 API 供混合开发的算子查询 Conv3D 反传这类复杂算子的切分结果。桩里保留了完整的参数/结果结构体，函数体退化为「直接成功」。
2. **切分结果缓存**（`op_cache_tiling.h` + `cache_tiling_data.h`）：BatchMatMul 这类重切分算子会把 tiling 结果缓存复用（`TilingPrepareForOpCache` 准备、`GenTiling` 生成/命中）。
3. **调优知识库**（`runtime_kb_api.h` + `register/`）：调优（tuning）场景下按算子名反射出 TilingData 的字段表，序列化成 JSON 存取（bank），`RuntimeKb::QueryBank` 负责查询。

它们解决的问题是同一个：**这些服务的真身要么闭源、要么依赖运行环境**，UT/宿主机编译拿不到，于是用「结构体保真 + 函数体 no-op」的镜像顶上。

#### 4.2.2 核心流程

以切分缓存为例，真实环境与 UT 环境的对照：

```text
真实环境：
  TilingPrepareForOpCache(context)     ← 读编译期信息，准备缓存上下文（涉及环境文件/硬件）
  GenTiling(op_type, compile_params, run_params, tiling, context)
      ├─ 命中缓存 → 回放缓存的 CacheTilingData（tiling_id 等字段原样复用）
      └─ 未命中   → 调真实切分算法计算并写入缓存
UT 环境（本仓库桩）：
  TilingPrepareForOpCache → 恒 return true（什么都不读）
  GenTiling               → 恒 return true（什么都不算）
  CacheTilingData         → 所有字段保持默认值（绝大多数为 1）
```

桩让这条链路**恒成功、恒定值**：不读环境文件、不查硬件、不产生随机性，同样的输入在任一台 CI 机器上得到同样的行为——这正是「在无硬件环境下重复回放同一结果」的实现方式：把不可控的真实查询替换成确定性常量。代价是它验证不了切分算法本身的正确性，只保证调用方代码能走通分支。

#### 4.2.3 源码精读

**桩①：TBE 切分查询。** 声明侧保留了真品的完整签名与数据结构：

- [tbe_tiling_api.h:L24-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/tbe_tiling_api.h#L24-L49)：`namespace optiling` 下的 `Conv3dBackpropV2TBETilingData`——L0/L1 缓冲参数（`m_l0/k_l0/n_l0`、`db_al1/db_bl1` 等）一个不少，这是「签名保真」的部分。
- [tbe_tiling_api.h:L182-L193](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/tbe_tiling_api.h#L182-L193)：`OpTypeV2` 枚举（Filter 反传/Input 反传/Transpose 三类）与三个 `GetTbeTiling` 重载声明。
- [tbe_tiling_api.cpp:L14-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/tbe_tiling_api.cpp#L14-L40)：三个替身函数体清一色 `(void)参数; return true;`——`Conv3dBackpropV2TBETilingData` 出参**不被填充**，调用方拿到的仍是默认构造值。这就是「行为最小化」。

**桩②：切分缓存。**

- [op_cache_tiling.h:L24-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/op_cache_tiling.h#L24-L32)：声明 `TilingPrepareForOpCache`（`TilingContext` 与 `TilingParseContext` 两个重载）和 `GenTiling(op_type, compile_params, run_params, tiling, context)`。注意文件头注释写的是 `cop_ache_tiling.h`、include guard 是 `OPS_BUILT_IN_OP_TILING_OP_CACHE_TILING_H`——guard 名暴露了它的出身：镜像自 CANN 内置算子（built-in op）的 op_tiling 组件。
- [op_cache_tiling.cpp:L18-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/op_cache_tiling.cpp#L18-L36)：三个函数体全部忽略参数返回 true。
- [cache_tiling_data.h:L23-L41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/cache_tiling_data.h#L23-L41)：`BatchmatmulCompileParas`——编译期开关（bias/左压缩/量化标志等共 17 个字段，bool/float/int 混合）。[L43-L127](https://github.com/gitcode.com/ascend-tribe-openPangu-2b0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/cache_tiling_data.h#L43-L127) 的 `BatchmatmulRunParas` 与 [L129-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/cache_tiling_data.h#L129-L194) 的 `CacheTilingData`（`tiling_id`、`m_al1/n_bl1`、`db_aub/db_bub` 等约 60 个字段，默认值几乎全为 1）共同构成缓存条目的数据契约。

**桩③：调优知识库与注册宏。**

- [runtime_kb_api.h:L18-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/runtime_kb_api.h#L18-L21)：`RuntimeKb::QueryBank(src, src_len, op_type, soc_version, core_num, tiling)` ——按算子名+芯片版本查 bank，返回 `TuningTilingDefPtr`。
- [register/tuning_tiling_registry.h:L40-L82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/register/tuning_tiling_registry.h#L40-L82)：`BEGIN_TUNING_TILING_DEF` / `TUNING_TILING_DATA_FIELD_DEF` / `END_TUNING_TILING_DEF` 三宏——用「每个字段挂一个 `FieldHandler` 静态成员」的老技巧实现字段反射，让 TilingData 结构体能自动 `ToJson/FromJson`。这套手法与 u3-l3 讲过的 tiling_templates_registry 静态注册同源。
- [register/tuning_tiling_registry.h:L84-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/register/tuning_tiling_registry.h#L84-L105)：`TuningTilingClassFactory` + `REGISTER_TUNING_TILING_CLASS(optype, class_name)`——构造全局静态 Helper 对象完成注册，和 u2-l2 的 `OP_ADD`、u3-l3 的模板注册是同一个套路的三次复用。旁边 [tuning_bank_key_registry.h:L17-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/register/tuning_bank_key_registry.h#L17-L19) 文件内注释自称「v1 stub」，v1/V2 两套注册宏并存。

#### 4.2.4 代码实践

**实践：核查 op_tiling 桩的真实消费链，并解释「缓存回放」如何在 UT 里退化为确定性常量。**

1. 实践目标：亲自验证「这些桩当前有没有人用」，并对照结构体字段写出桩化后缓存链路的行为说明。
2. 操作步骤：

```bash
cd ascendc
# ① 全仓库找直接调用者（排除 stub 目录自身）
grep -rn "GetTbeTiling\|TilingPrepareForOpCache\|GenTiling\|QueryBank" \
  src --include='*.cpp' --include='*.h' | grep -v 'common/stub'
# ② 找有哪些 tiling 源文件包含了这些桩头文件
grep -rln 'tbe_tiling_api.h\|op_cache_tiling.h\|cache_tiling_data.h' \
  src/ops-transformer --include='*.cpp' | grep -v common/stub
```

3. 需要观察的现象：两条命令均**无输出**——当前仓库没有任何算子的 tiling 源码调用/包含这些接口。这是 u3-l3 结尾那条纪律的又一次应验：公共库（包括桩）的真实调用关系必须用 grep 核实。
4. 预期结果（书面作业）：结合 [cache_tiling_data.h:L129-L194](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_tiling/cache_tiling_data.h#L129-L194) 写出结论——`CacheTilingData` 的 `tiling_id` 与各级 `m_al1/n_bl1/k_aub/db_bub` 字段默认值全为 1，桩化 `GenTiling` 不写任何字段，因此 UT 中「缓存命中/回放」退化为「返回一份全默认值的确定性结果」；真实环境中这些字段的差异正是「同 shape 回放同一切分」的载体，而 UT 用「恒定默认值」替换了「可变缓存」，获得了可重复性。本实践纯只读，无需 NPU。

#### 4.2.5 小练习与答案

**练习 1**：`GetTbeTiling` 的桩返回 true 但不填充出参 `tbeTilingForV2`，调用方若不检查会怎样？
答：调用方会拿到默认构造的 `Conv3dBackpropV2TBETilingData`（各字段为 0），把它当真实切分结果使用会得到错误的核切分。所以「桩返回成功」只保证流程走通，**不保证数据有效**——这也是为什么真要用这类桩时，UT 断言只能针对调用方自己的逻辑，不能针对切分数值。

**练习 2**：`op_cache_tiling.h` 的 include guard 是 `OPS_BUILT_IN_OP_TILING_OP_CACHE_TILING_H`，从中能推断什么？
答：guard 与文件头注释（`cop_ache_tiling.h`，应为 `op_cache_tiling.h` 的笔误）都指向它镜像自 CANN「built-in op」的 op_tiling 组件，而非本仓库原创。读桩代码时，guard 前缀（`OPS_BUILT_IN_...`、`COMMON_INC_EXTERNAL_...`、`PTA_NPU_...`）是判断「替谁做的镜像」的快捷线索。

**练习 3**：`register/` 下的反射宏（`TUNING_TILING_DATA_FIELD_DEF`）如果真被使用，编译产物里会发生什么？
答：每个标了该宏的字段会生成一个 `FieldHandler` 静态成员对象，程序加载时（早于 `main`）把「类型名+字段名」登记进 `field_info_`，从而支持把 TilingData 与 JSON 互转——这是调优数据落盘/回放的前提。它不改变算子逻辑，只增加元信息。

### 4.3 op_api 桩：level0 / aclnn_kernels 镜像与 opapi_stub.cpp

#### 4.3.1 概念说明

回顾 u2-l5：op_api 层的 L2 文件（`aclnn_*.cpp`）在 `GetWorkspaceSize` 里会先对输入做连续化、Pad、转置等**布局预处理**，这些预处理不是手写循环，而是调用 `namespace l0op` 里的 L0 基础算子（`l0op::Contiguous`、`l0op::Transpose`、`l0op::Pad`……）。这些函数的真身在 CANN/torch_npu 的闭源库里。

op_api 桩由两部分组成：

- **头文件镜像**（`level0/` 32 个 + `aclnn_kernels/` 7 个 + `common/op_error_check.h`）：只含声明（个别含真实的 inline 辅助函数），保证「能 include、签名一致」。
- **替身函数体**（`opapi_stub.cpp`，308 行）：为其中一部分函数提供「返回 self」式实现，编成 `libopapi_stub.so` 供 UT 链接。

#### 4.3.2 核心流程

替身函数体的行为模式高度统一，理解一个就理解全部：

```text
模式 A（tensor → tensor）：return self;      // 原样返回输入指针，假装"处理完了"
模式 B（bool 判定）      ：return true;      // 假装"可以优化/命中"
模式 C（多输出 tuple）   ：把 self 塞进每个槽位返回
```

含义：UT 跑 `aclnnXxxGetWorkspaceSize` 时，`l0op::Contiguous(x, executor)` 直接把 `x` 还回来——**没有发生任何真实搬运**。UT 因此可以专心验证 L2 逻辑（参数校验、分支选择、executor 组装），而不需要数据真的被搬来搬去。这也解释了为什么 op_api UT 不校验计算精度——精度校验属于 ST（第 8 单元）的职责。

#### 4.3.3 源码精读

**声明与实现的对照（以 Add 为例）。**

- [level0/add.h:L21-L24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/add.h#L21-L24)：整个文件只有一个声明 `const aclTensor* Add(const aclTensor* self, const aclTensor* other, aclOpExecutor* executor);`——三参数：两个输入张量 + executor。它模拟的是「逐元素加法」L0 算子。
- [opapi_stub.cpp:L179-L182](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L179-L182)：函数体 `return self;`。声明在 `level0/add.h`，函数体在 `opapi_stub.cpp`——桩的「声明/实现分离」结构。

**真实调用现场（桩存在的理由）。**

- [aclnn_flash_attention_score_enhance.cpp:L687-L691](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L687-L691)：FA 前向的 `GetWorkspaceSize` 开头对 query/key/value 连调三次 `l0op::Contiguous`，随后还有 `l0op::Reshape/Pad/Transpose`（同文件 L746-L786 一带）。全仓库此类调用点数以百计——桩服务的正是它们。
- [opapi_stub.cpp:L86-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L86-L89)：`Contiguous` 的替身同样 `return x;`。

**aclnn_kernels 层的镜像不全是空壳。**

- [aclnn_kernels/contiguous.h:L17-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/contiguous.h#L17-L51)：`ContiguousParam` 结构体完整保留了转置/广播/切片/strided-slice 四种「可优化视图」的判定字段——这是真品布局优化逻辑需要的真实数据结构。[L53-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/aclnn_kernels/contiguous.h#L53-L68) 是 `Contiguous/ViewCopy/PickViewAsContiguous/ReViewToOut` 的声明，注释解释了各自用途（连续化、写回非连续视图等）。
- 对应替身：[opapi_stub.cpp:L113-L123](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L113-L123) 里 `CanOptimizeContiguous/CanOptimizeView` 恒返回 true——「假装一切视图都可优化」，让调用方的快路径分支在 UT 里也能被覆盖到。

**「有声明、无函数体」的桩。**

- [level0/matmul_v2tov3.h:L16-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/matmul_v2tov3.h#L16-L20)：声明 `bool MmCheckHitV3Shape(...)`（matmul v2→v3 走 V3 形状命中检查）。在 `opapi_stub.cpp` 里 grep 不到它的函数体——这类「仅声明」的镜像头只是保证**能编译** include 了它的代码；真被调用时符号须由链接环境里的真实库提供。
- 数量关系：`opapi_stub.cpp` 共 include 14 个 level0 头 + 7 个 aclnn_kernels 头（[opapi_stub.cpp:L16-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L16-L38)），却还手写了若干未 include 头文件的符号（如 [L41-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L41-L44) 的 `TensorMove`，其声明在 [level0/tensor_move.h:L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/tensor_move.h#L23)）——函数体是手工维护的签名副本，改签名要两头同步，这是这类桩的主要维护风险。

**头文件出身的旁证。** level0 各文件的 include guard 前缀不一：`add.h` 是 `OP_API_INC_LEVEL0_OP_ADD_OP_H_`（CANN 风格），`matmul_v2tov3.h`/`ones_like.h` 是 `PTA_NPU_OP_API_INC_...`（PTA = PyTorch Adapter，即 torch_npu 配套头）。可见这批镜像是「哪里缺补哪里」，分别取自 CANN 包与 torch_npu 包。

#### 4.3.4 代码实践

**实践：为三个 level0 桩制作「签名档案卡」。**

1. 实践目标：把 `add.h`、`matmul_v2tov3.h`、`gather_v2.h` 三个桩的签名、模拟的算子、替身行为整理成表——这是读懂任意一个 level0 桩的标准动作。
2. 操作步骤：阅读下面三处源码并填表（答案已给出，先自己填再对照）：
   - [level0/add.h:L21-L24](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/add.h#L21-L24)
   - [level0/matmul_v2tov3.h:L16-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/matmul_v2tov3.h#L16-L20)
   - [level0/gather_v2.h:L15-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/level0/gather_v2.h#L15-L23)
3. 需要观察的现象：三个桩形态各异——有函数体 / 仅声明 / 一头两函数。
4. 预期结果（参考答案）：

| 桩文件 | 声明的函数与签名 | 模拟的算子/功能 | 替身行为 |
| --- | --- | --- | --- |
| `add.h` | `Add(self, other, executor) → const aclTensor*` | 逐元素加法 L0 算子 | 有体：`return self;`（[opapi_stub.cpp:L179-L182](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L179-L182)） |
| `matmul_v2tov3.h` | `MmCheckHitV3Shape(x1, x2, bias, transposeX1, transposeX2, mat2_format, supportSplitK) → bool` | matmul V2 算子能否命中 V3 形状的判定 | **无体**（仅声明，需环境真库提供符号） |
| `gather_v2.h` | `GatherV2(self, axis, indices, executor, batchDims=0, negativeIndexSupport=false)`；`GatherV2WithImplMode(..., implMode, ...)` | 按轴聚合索引（两个变体：带实现模式） | 均有体：`return self;`（[opapi_stub.cpp:L53-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/stub/op_api/opapi_stub.cpp#L53-L65)） |

5. 本实践纯静态阅读，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：`l0op::Add` 桩返回 `self` 而不是新建张量，这在 UT 里意味着什么？如果被测代码把返回值当「新张量」原地修改会怎样？
答：意味着 UT 里 `Add` 是「零成本恒等映射」，executor 里记录的任务是假的。被测代码若把返回值当新张量做原地写，会污染原输入——好在 UT 阶段数据本身也是假的，不会造成真实破坏，但断言时必须清楚「值不可信，只有流程与分支可信」。

**练习 2**：`CanOptimizeContiguous` 桩恒返回 true，会掩盖真环境里的什么问题？
答：会掩盖「某些视图组合实际不可优化、需要走慢路径（真实拷贝）」的分支。UT 里快路径全覆盖、慢路径可能一次都没走到，属于典型的「桩带来的覆盖盲区」。需要慢路径覆盖时就得换真库或写更聪明的桩。

**练习 3**：为什么 `opapi_stub.cpp` 里有些函数体（如 `TensorMove`）对应的头文件并没有被它 include？
答：因为这些函数体是按需手写的签名副本：某个 UT 链接时缺哪个 `l0op::` 符号，就补哪个函数体，不一定顺手 include 其声明头。风险是签名与头文件脱钩——改动头文件声明时 `opapi_stub.cpp` 不会编译报错，只能靠链接期/运行期发现。给这类文件加函数体时，最好同时 include 对应声明头。

### 4.4 UT 中的替身全景：从 libopapi_stub 到算子本地桩

#### 4.4.1 概念说明

前两节讲了「库存桩」。这一节回答学习目标里的最后一项：**一次 op_api UT 到底链接了哪些替身、自己写算子 UT 时桩放在哪**。

一次 op_api UT（如 `transformer_op_api_ut` 可执行文件）的符号来源里有四层替身：

| 层 | 提供者 | 替掉的真品 |
| --- | --- | --- |
| L0 基础算子 | `libopapi_stub.so`（`common/stub/op_api`） | CANN/torch_npu 的 `l0op::` 真实实现 |
| opdev 平台基座 | `framework_normal/op_api/stub/opdev/{platform,nnopbase}.cpp` | CANN opdev 层的平台/算子基类实现 |
| runtime 驱动接口 | 脚本生成的 `runtime_stubs.cpp`（`rt*` 函数族） | NPU 驱动 runtime 库 |
| 算子专属接口 | 各算子 `tests/ut/op_api/ut_stub_*.cpp` | 该算子依赖的私有外部符号 |

而 op_host（tiling）UT 不需要这么多层——它链接的是 faker 上下文（u8-l1/u8-l2 详讲），桩体系里只有 `OP_TILING_INCLUDE` 的遮蔽路径与它相关。

#### 4.4.2 核心流程

op_api UT 可执行文件的组装流程（对照 [framework_normal/op_api/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L33-L118)）：

```text
各算子 test_aclnn_*.cpp ──┐
framework stub/opdev/*.cpp ├→ ${OP_API_MODULE_NAME}_cases_obj（对象库，含 ut_stub_*.cpp）
算子 ut_stub_*.cpp ────────┘
                                  ↓ 与 opapi 源对象合并
                      ${OPAPI_NAME}_ut（SHARED，链接 opapi_stub ← L0 桩在此进入）
                                  ↓
              ${OP_API_UT_EXE} = main + cases_obj + runtime_stubs.cpp（← rt* 桩在此进入）
                                  ↓ 运行前
              generate_opapi_stub.py 还会往 CANN 安装包里写 .o/.json 占位
```

#### 4.4.3 源码精读

**链接点：L0 桩进入 UT。**

- [framework_normal/op_api/CMakeLists.txt:L33-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L33-L41)：`add_library(${OPAPI_NAME}_ut SHARED $<TARGET_OBJECTS:${OPHOST_NAME}_opapi_obj>)`，链接清单里赫然有 `opapi_stub`——被测的 op_api 源码对象与桩库绑在一起，`l0op::Contiguous` 等符号就此解析到桩体。
- [ut.cmake:L126-L143](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L126-L143)：`add_opapi_ut_modules` 把 `${UT_PATH}/op_api/stub/opdev/platform.cpp`、`nnopbase.cpp` 编进用例对象库，并把 `${UT_PATH}/op_api/stub` 放进 include 首位——opdev 层替身就位。

**生成点：rt\* 桩。**

- [framework_normal/op_api/CMakeLists.txt:L73-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L73-L83)：`add_custom_command` 调 `generate_opapi_stub.py` 生成 `runtime_stubs.cpp`，随后它被直接编进 UT 可执行文件（[L85-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L85-L89)）。
- [generate_opapi_stub.py:L449-L464](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py#L449-L464)：`rtGetSocVersion` 按 `-c` 传入的芯片名返回对应字符串（`ascend910_93` → `"Ascend910_9391"`）——**UT 没有 NPU，芯片型号是脚本"冒充"的**。其余几十个 `rt*`（[L128-L421](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py#L128-L421)）几乎全部 `return RT_ERROR_NONE`，少数有语义：如 `rtMalloc` 用 `new uint8_t[size]` 在宿主机内存上模拟设备内存（[L263-L268](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py#L263-L268)）。
- 同脚本 [L37-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py#L37-L105)：往 CANN 安装包的 `binary_info_config.json` 里补条目、创建空的 `{op}_opapi_stub.o/.json` 占位文件——让运行期按名字找算子二进制时不报缺文件（跑完 UT 由 `clean_opapi_stub.py` 清理，见 [L155-L165](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L155-L165)）。

**算子本地桩范例（metadata 算子）。** 这是「在 UT 中正确链接所需 stub 符号」的最佳样本——三个桩对应被测代码的三处外部依赖：

- [ut_stub_metadata.cpp:L16-L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/ut_stub_metadata.cpp#L16-L22)：桩 `aclrtGetResInCurrentThread` 返回 36——被测代码在 [aclnn_ai_infra_attention_pioneer_metadata.cpp:L148-L149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L148-L149) 用它查 Cube/Vector 核资源上限，桩给非零值以覆盖 `resLimit > 0` 分支。注意桩体注释直接写明了覆盖目的——**桩值是按分支覆盖需求定的，不是随手写的**。
- [ut_stub_metadata.cpp:L25-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/ut_stub_metadata.cpp#L25-L49)：桩 `l0op::AiInfraAttentionPioneerMetadata` 原样返回 `metaData`——被测代码 [L162](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L162) 调它生成元数据张量，其真身是 AICPU 算子（声明见 [l0_ai_infra_attention_pioneer_metadata.h:L16-L17](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.h#L16-L17)），UT 环境跑不了 AICPU，只能桩掉。
- [ut_stub_metadata.cpp:L51-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/ut_stub_metadata.cpp#L51-L56)：桩 `CommonOpExecutorRun` 返回 0——这是 u2-l5 讲过的「第二段执行接口」，真身会把任务下发到 stream；桩掉它，UT 的执行段才能在纯主机上走完。
- 挂接方式：[tests/ut/op_api/CMakeLists.txt:L9-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/CMakeLists.txt#L9-L15) 用 `target_sources(${OP_API_MODULE_NAME}_cases_obj PRIVATE .../ut_stub_metadata.cpp)` 把桩编进用例对象库——**新算子补桩时就抄这个挂法**。

#### 4.4.4 代码实践

**实践：追踪一次 op_api UT 的桩符号来源。**

1. 实践目标：把「可执行文件里的 `l0op::Contiguous` 来自哪个 `.so`」查实，体验链接替换。
2. 操作步骤（需要已完成 u1-l4 的容器与编译环境；无 NPU 也可，UT 是主机程序）：

```bash
cd ascendc
bash build.sh -u --opapi -n flash_attention_score_enhance -c ascend910_93   # 编 op_api UT
# 在 build 产物目录找到 transformer_op_api_ut 与 libopapi_stub.so 后：
ldd build/src/tests/ut/framework_normal/op_api/transformer_op_api_ut | grep -i stub   # 待本地验证
nm -D build/libopapi_stub.so | grep -E "Contiguous|Transpose"                          # 待本地验证
```

3. 需要观察的现象：`libopapi_stub.so` 出现在可执行文件的动态依赖里；`nm` 能在其中看到 `l0op::Contiguous` 等符号。
4. 预期结果：若环境未就绪（找不到编译产物路径或链接失败），则记录具体缺项——按 u1-l3 的清单排查 CANN 包路径与 `set_env.sh`；本步骤的输出路径属构建产物，**待本地验证**。纯静态替代方案：`grep -rn "opapi_stub" ascendc/src/tests ascendc/cmake ascendc/src/ops-transformer/common/stub` 找出全部挂接点（应为 2 处：桩的定义与 UT 的链接）。

#### 4.4.5 小练习与答案

**练习 1**：`generate_opapi_stub.py` 生成的 `rtGetSocVersion` 为什么要按 `-c` 的芯片名返回不同字符串？
答：被测的 op_api/tiling 代码会按 socVersion 走不同分支（如 u2-l3 里 `AHInfoParser` 的平台白名单）。UT 没有 NPU，芯片型号由桩冒充；跟随 `-c` 参数保持一致，才能让被测代码走到「为目标芯片编译的那套分支」，否则 UT 测的分支与实际交付的芯片不符。

**练习 2**：如果你给新算子写 op_api UT 时遇到「undefined reference to l0op::Xxx」，按什么顺序排查？
答：① `grep common/stub/op_api` 看桩头文件里有没有 `Xxx` 的声明；② `grep opapi_stub.cpp` 看有没有函数体——没有就补一个（同时 include 声明头）；③ 若是算子私有依赖（如自家 L0 封装），仿照 metadata 在 `tests/ut/op_api/ut_stub_*.cpp` 写本地桩并用 `target_sources` 挂进 `_cases_obj`；④ 若是 `rt*` 类接口，确认 `runtime_stubs.cpp` 是否覆盖（脚本模板里没有的需扩脚本）。

**练习 3**：桩掉的 `CommonOpExecutorRun` 返回 0（成功），这会让 UT 的哪个阶段「空转」？为什么这是可接受的？
答：让 aclnn 两段式的第二段（执行下发）空转——executor 组装好了但任务没真正执行。可接受是因为 op_api UT 的职责是验证第一段的参数校验/布局决策/executor 组装逻辑；数值正确性由 ST 在真机上验证（第 8 单元）。

## 5. 综合实践

**综合实践：产出一份《某算子 op_api UT 桩依赖清单》。**

以 `flash_attention_score_enhance` 为对象（它有全仓库最重的 op_api 预处理链），完成四步：

1. **统计 L0 依赖**：在 [aclnn_flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.cpp#L687-L691) 中 grep 所有 `l0op::` 调用，按函数去重，得到该算子的 L0 依赖集合。
2. **对照桩覆盖**：逐个在 `common/stub/op_api` 里找声明与函数体，标注「桩库已覆盖 / 仅声明无体 / 桩缺失」三种状态，产出一张与 4.3.4 同格式的表。
3. **补一个桩**：若发现某个被调用函数在 `opapi_stub.cpp` 里没有函数体（提示：检查 `MmCheckHitV3Shape` 是否被本算子调用；若否，任选一个「仅声明」状态但你判断 UT 会链接到它的函数），写出应补的桩函数体（示例代码，不修改仓库）：

```cpp
// 示例代码：应补入 opapi_stub.cpp 的桩（模式 A：返回 self）
const aclTensor *Xxx(const aclTensor *self, const aclTensor * /* other */, aclOpExecutor * /* executor */)
{
    return self;
}
```

4. **画出链路图**：把「被测源文件 → libopapi_stub.so → runtime_stubs.cpp → 桩值如何驱动分支覆盖」画成一张时序/依赖图，标注每层替身替换掉的真品。

验收标准：表中每个函数都有可点击的永久链接与状态结论；链路图中四层替身各就各位。全程无需 NPU。

## 6. 本讲小结

- **桩 = 签名保真 + 行为最小化**的替身：编译期靠 include 路径遮蔽（`OP_TILING_INCLUDE` 塞入 `common/stub/op_tiling`）、链接期靠替换库（UT 链接 `libopapi_stub.so`）、运行期靠假实现（`rt*` 全家桶），三层各管一段。
- **op_tiling 桩**（`tbe_tiling_api` / `op_cache_tiling` / `runtime_kb_api` + tuning 注册宏）镜像 CANN 内置的 TBE 切分查询、切分缓存与调优知识库；替身函数体一律 no-op 返回 true，`CacheTilingData` 字段保持默认——UT 中「缓存回放」退化为确定性常量。**当前仓库无任何算子直接调用它们**，且桩 `.cpp` 未被编进 `optiling` 库，属「已备而未用」的基础设施。
- **op_api 桩**分两层：`aclnn_kernels/`（布局预处理，含真实的 `ContiguousParam` 等数据结构）与 `level0/`（L0 基础算子，32 个头文件）；函数体集中在 `opapi_stub.cpp`，行为模式是 `return self / return true`，只在 `ENABLE_TEST` 时编成 `libopapi_stub.so`。
- **「有声明 ≠ 有函数体」**：32 个 level0 头只有 14 个进了 `opapi_stub.cpp`；`MmCheckHitV3Shape` 等是仅声明镜像。部分函数体（如 `TensorMove`）还是未 include 声明头的手写副本，改签名要两头同步。
- **一次 op_api UT 链接四层替身**：`opapi_stub` 库、framework 的 `opdev` 桩、脚本生成的 `runtime_stubs.cpp`（连芯片型号都由 `rtGetSocVersion` 冒充）、算子本地 `ut_stub_*.cpp`（桩值按分支覆盖需求设定，如 metadata 的 36 核）。
- 桩换来「无硬件可测、可重复」，也带来盲区（慢路径覆盖不足、数值不可信）——**UT 验证流程与分支，ST 验证精度**，两者不可互相替代。

## 7. 下一步学习建议

本讲补完了 common 基础设施的最后一块（错误日志 u3-l1 → 公共组件 u3-l2 → tiling_base u3-l3 → stub 本讲）。接下来：

1. **u8-l1（UT 框架 framework_normal 总览）**：本讲反复出现的 `_cases_obj`、faker、executor 将在那里系统展开，tiling UT 与 op_api UT 的组织方式对照着读收获最大。
2. **u8-l2（编写 Tiling 单元测试）**：动手用 `TilingContextPara` 写用例，体会「桩保真签名、faker 伪造上下文、用例只写断言」的分工。
3. **u8-l4（构建与运行 UT/ST）**：实际跑一次 `bash build.sh -u --opapi ...`，用 `ldd/nm` 验证本讲 4.4.4 的链接关系（本讲标注「待本地验证」的项都在这一步兑现）。
4. 源码延伸阅读：`ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/` 下的公共用例工具（`op_api_ut.h` 等），以及 `generate_opapi_stub.py` 全文——它是理解「UT 如何欺骗 CANN 安装包」的最短路径。
