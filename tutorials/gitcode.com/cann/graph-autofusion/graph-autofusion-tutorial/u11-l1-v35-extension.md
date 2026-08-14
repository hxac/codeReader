# u11-l1 v35 平台扩展机制

> 本次更新（对应 HEAD `2b9c5c2a`）：补充 v2 版本扩展（`ascir_builtin_ops_v2`、`ascir_api_perf_v2` 等）与新增的 `att/api_perf_register/nddma_model` 精确性能模型子模块，并刷新全部永久链接行号。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `autofuse/v35/` 子目录的内部结构，以及它的主仓五个模块（ascendc / ascir / optimize / att / codegen）一一对应的关系。
2. 解释 v35 代码是如何「挂接」进主仓构建与运行链路的：不是另起一个库，而是把源码追加进共享库 `aihac_codegen`，再用运行期平台字符串选择实现。
3. 区分 v2 后缀文件（如 `ascir_builtin_ops_v2.cpp`、`binary_api_call_v2.h`）与主仓 v1 文件的关系：同名算子、不同平台实现，靠 ASCIR 注册表的「算子类型 → 多平台实现」映射共存。
4. 说明平台开关与构建集成方式：`IS_DIRECTORY` 守卫的 `add_subdirectory(v35)`、`REGISTER_PLATFORM_V2("3510"/"5102", ...)` 静态注册、以及 `sed` 把设备端头文件包成原始字符串字面量的机制。
5. 了解本次更新新增的三块 v35 专属能力的位置：NDDMA 1D 精确性能模型（`nddma_model`）、IndirectLoad SIMD/SIMT 寻址优化、chebyshev/hermite/polygamma 等 v2 特殊函数算子注册链路（后两者各有专门讲义展开）。

## 2. 前置知识

- **SoC 版本与平台字符串**：昇腾不同代际芯片用一个短字符串标识，本仓库中出现的有 `"2201"`（v1，对应主仓默认路径）、`"3510"` 与 `"5102"`（v2，对应昇腾 950 系，即本讲的 "v35" 平台）。Autofuse 在运行期向 runtime 查询当前 SoC 版本字符串，再用它选择平台实现。
- **自注册（self-registration）模式**：每个模块在自己的 .cpp 里定义一个静态全局对象，在 `main` 之前执行构造函数，把「自己」登记进某个全局单例表（ASCIR 注册表、`PlatformFactory`、`AscendCApiRegistry`、`ApiPerfFactory`）。这是贯穿 u5-l1、u6-l2、u8-l3 的同一手法，v35 完全复用。
- **共享库 `aihac_codegen`**：u3-l2 讲过，optimize / att / codegen 的源码合流进这一个共享库。v35 的做法是把它的源码也追加进同一个 target，因此 v35 与主仓在二进制层面是同一个 `.so`，分流发生在运行期。
- **原始字符串字面量（raw string literal）**：形如 `R"===( ... )==="` 的 C++ 语法，让头文件内容可以原样嵌进一个 `std::string`。u5-l3 讲过主仓用 `sed` 生成 `*_str.h`，v35 的 `api_regbase` / `api_cube` 用完全相同的机制生成 `*_reg_base.h` / `*_str.h`。
- **CV 融合（cube-vector fusion）**：v35 上把 cube 类算子（matmul/conv2d）与相邻 vector 算子融合进同一个 kernel，宏 `CV_UB_FUSION` 是设备端代码里区分「是否处于融合上下文」的编译期开关，详细机制见 u11-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [autofuse/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt) | Autofuse 顶层 CMake，含 `add_subdirectory(v35)` 的守卫条件 |
| [autofuse/v35/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/CMakeLists.txt) | v35 总装配：按六个子目录顺序 `add_subdirectory` |
| [autofuse/v35/optimize/platformv2.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/optimize/platformv2.h) / platformv2.cpp | v2 平台类 `PlatformV2`：对齐策略、Pass runner、任务生成器的平台分发中心 |
| [autofuse/optimize/platform/platform_factory.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/optimize/platform/platform_factory.cpp) | 平台工厂：按运行期平台字符串取平台实例 |
| [autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp) | v2 平台全部 ASCIR 算子的注册文件（约 166 个 `REG_ASC_IR`） |
| [autofuse/v35/ascir/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/CMakeLists.txt) | 把 v35/ascir 的源码同时编进 `aihac_codegen` 与 `ascir_builtin_ops` |
| [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp) | v2 平台的 ATT 算子耗时公式注册（名字加 `V2` 后缀入表） |
| [autofuse/v35/att/api_perf_register/nddma_model.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h) / nddma_model.cpp | **本次新增**：NDDMA 1D 精确性能模型 |
| [autofuse/v35/codegen/ascendc_reg_base_api_register.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/ascendc_reg_base_api_register.cpp) | 把 v35 设备端 regbase 头文件内容登记进 `AscendCApiRegistry` |
| [autofuse/v35/ascendc/api_regbase/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt) | regbase 头文件清单 + `sed` 包装生成 `*_reg_base.h` |
| [autofuse/v35/ascendc/api_cube/matmul.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/matmul.h) | v35 cube 类算子代表：mat_mul_v3 设备端入口 |
| [autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h) | **本次重点优化**：IndirectLoad SIMD/SIMT 策略选择（详见 u11-l4） |
| [autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h) | IndirectLoad 的 api_call 生成器（详见 u11-l4） |

## 4. 核心概念与源码讲解

### 4.1 v35 子目录结构

#### 4.1.1 概念说明

`autofuse/v35/` 是为昇腾 950（v35，SoC 字符串 `3510`/`5102`）平台准备的**专属扩展目录**。它不是一个新的编译器，而是主仓 Autofuse 六大模块在 v2 平台上的「增量补丁集合」：凡 v35 与 v1 行为一致的部分直接复用主仓代码，凡有差异的部分（新的设备端 API、新的性能模型参数、新的调度策略）落在 v35 目录里，以 v2 后缀或独立文件的形式存在。

「目录即平台」是它的组织原则——读者看到 `autofuse/v35/` 下的路径，就可以按主仓同名模块去理解它的职责。

#### 4.1.2 核心流程

v35 内部只有一层总装配 CMake，按依赖顺序挂六个子目录：

```text
autofuse/v35/
├── CMakeLists.txt        # 总装配：add_subdirectory 六个子目录
├── ascendc/
│   ├── api_regbase/      # v2 设备端 vector 类 API 封装（80+ 个 .h，含本次优化的 indirect_load_*）
│   └── api_cube/         # v2 设备端 cube 类 API（matmul/conv2d 及其 tiling key 头）
├── ascir/
│   ├── generator/        # ascir_builtin_ops_v2.cpp + v2_ascir_att_impl.h + v2_ascir_codegen_impl.h
│   └── reg_func/         # 各算子的 tmp buf sizing（*_v2.cpp，如 polygamma_v2.cpp）
├── optimize/             # PlatformV2、pass_runner_v2、graph_pass、task_generator、un_alignment_strategy
├── att/
│   └── api_perf_register/  # perf_param_v2 + ascir_api_perf_v2 + nddma_model（本次新增）
└── codegen/
    ├── cube_api_call/    # matmul / conv2d 的 api_call 生成器
    ├── micro_api_call/   # v2 微 API（micro_*）调用生成器
    ├── reg_api_call/     # v2 regbase API 调用生成器（binary_v2、cast_v2、indirect_load 等）
    └── vec_func_call/    # vector function 调用与循环（vf_loop）
```

#### 4.1.3 源码精读

v35 的总装配只有 6 行，顺序即依赖顺序（ascendc 最先——它是被 include 的头；att 最后——它依赖 ascir 的类型）：

- [autofuse/v35/CMakeLists.txt:L1-L6](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/CMakeLists.txt#L1-L6)：这段代码按 `api_regbase → api_cube → ascir → codegen → optimize → att` 的顺序 `add_subdirectory`，与主仓 `autofuse/CMakeLists.txt` 中 `graph_metadef → ascendc → ascir → optimize → att → codegen` 的装配顺序互为镜像（v35 把 codegen 提前只是目录列举顺序，实际都汇入同一 target）。

v35 被「有条件地」挂进主仓构建：

- [autofuse/CMakeLists.txt:L146-L148](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt#L146-L148)：这段代码用 `if (IS_DIRECTORY "${CODE_ROOT_DIR}/v35")` 守卫 `add_subdirectory(v35)`——v35 目录存在才参与编译。这是「平台扩展可整体裁剪」的构建层落点：交付不含 950 支持的版本时，删掉或排除该目录即可，CMake 不会报错。

#### 4.1.4 代码实践

**实践目标**：建立 v35 目录与主仓模块的「同名对应」直觉。

**操作步骤**（源码阅读型，无需环境）：

1. 在仓库根目录执行 `ls autofuse` 与 `ls autofuse/v35`，对比两份目录清单。
2. 对每个 v35 子目录，在主仓找同名/同职责目录：`v35/ascendc/api_regbase ↔ autofuse/ascendc/api`、`v35/ascir ↔ autofuse/ascir`、`v35/optimize ↔ autofuse/optimize`、`v35/att ↔ autofuse/att`、`v35/codegen ↔ autofuse/codegen`。
3. 执行 `git diff --stat 00627d97..2b9c5c2a -- autofuse/v35 | tail -5`，观察本次更新集中在哪些子目录（预期：`ascendc/api_regbase`（indirect_load 系列）、`ascir/generator`（新增特殊函数注册）、`att/api_perf_register`（新增 nddma_model）、`codegen/reg_api_call`）。

**需要观察的现象**：v35 五个子目录的名字都能在主仓找到对应；diff 集中在四个子目录，恰好对应本次更新的三块新能力加上 api_call 适配。

**预期结果**：得到一张「v35 子目录 → 主仓模块 → 本次变更」三列对照表。若在仓库中执行，命令结果可直接复制进笔记（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：v35 目录下没有 `graph_metadef`、`compiler`、`common`，为什么？

**答案**：图 IR 容器（graph_metadef）、Python 绑定与编译编排（compiler）、公共工具（common）是平台无关的，v2 平台直接复用主仓实现；v35 只放「因平台而异」的增量——设备端 API 形态、tiling/性能参数、调度策略与代码生成分支。

**练习 2**：`autofuse/v35/ascendc` 下为什么分成 `api_regbase` 和 `api_cube` 两个子目录？

**答案**：对应芯片上两类计算单元的 API 形态差异：`api_regbase` 是 vector（寄存器基）类算子封装，如 elementwise、广播、本次优化的 indirect_load；`api_cube` 是 cube（矩阵乘）类算子封装，如 matmul/conv2d，二者在 tiling、模板参数（`DTYPE_X1/X2/Y/BIAS`）、执行流水（AIC vs AIV）上完全不同，因此分目录组织，各自的 CMake 也生成不同的接口库（`ascendc_api_regbase_extend` / `ascendc_api_cube_extend`）。

---

### 4.2 与主仓模块的对应关系

#### 4.2.1 概念说明

v35 代码如何「变成」主仓的一部分？答案是**构建期合流、运行期分流**：

- **构建期合流**：v35 各子目录的 CMake 用 `target_sources(aihac_codegen PRIVATE ${SOURCES})` 把源码追加进主仓共享库 `aihac_codegen`（u3-l2 讲过它就是 optimize/att/codegen 的合集）。没有任何 v35 专属的 `.so` 产物。
- **运行期分流**：所有「选哪套实现」的决策都推迟到运行期，由四张全局注册表按平台字符串或入表名字分流：
  1. `PlatformFactory`（optimize 侧）：`"3510"/"5102"` → `PlatformV2`；
  2. ASCIR 注册表（ascir 侧）：`Impl(v2_soc_versions, ...)` 把 v2 实现挂到同名算子名下，与 v1 实现并存；
  3. `ApiPerfFactory`（att 侧）：v2 公式以 `api_name + "V2"` 的名字入表；
  4. `AscendCApiRegistry`（codegen 侧）：v35 设备端头文件以 `*_reg_base.h` 文件名入表，按需内嵌。

#### 4.2.2 核心流程

以一次 v35 平台上的 Autofuse 编译为例，平台相关决策的传播路径：

```text
runtime 查询 SoC → PlatformContext::GetCurrentPlatformString 得 "3510"
        │
        ├─ optimize: PlatformFactory::GetPlatform() → PlatformV2
        │       └─ 决定 Pass 序列、对齐策略、任务生成器顺序（IndirectLoad 最先）
        │
        ├─ ascir: GetAscIrImpl("3510", 算子类型)
        │       └─ 取出 XxxAscIrAttImplV2 / XxxAscIrCodegenImplV2（v2 后缀实现类）
        │
        ├─ att: 算子耗时查询命中 "XxxApiV2"（v2 性能公式，参数来自 perf_param_v2.cpp）
        │       └─ Nddma 节点先尝试 nddma_model 精确模型，不合法则回退 legacy 公式
        │
        └─ codegen: api_call 生成 v2 调用语句；AscendCApiRegistry
                按头文件名取出 v35 设备端定义（*_reg_base.h / *_str.h）拼进 kernel 源码
```

#### 4.2.3 源码精读

**入口：平台工厂按字符串取实例。**

- [autofuse/optimize/platform/platform_factory.cpp:L29-L52](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/optimize/platform/platform_factory.cpp#L29-L52)：这段代码是 `PlatformFactory::GetPlatform()`——先经 `ge::PlatformContext::GetInstance().GetCurrentPlatformString(...)` 拿到运行期平台字符串（其定义在 [autofuse/common/platform_context.cpp:L72](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/platform_context.cpp#L72)，底层向 runtime 读取 SoC 信息），再从 `platform_name_to_creators_` 表中取 creator 构造平台实例，并缓存在 `platform_name_to_instances_`。找不到时仅打日志返回 `nullptr`——工厂本身对「有哪些平台」一无所知，注册全部发生在平台自己的 .cpp 里。

**optimize 侧：PlatformV2 的静态注册与任务生成顺序。**

- [autofuse/v35/optimize/platformv2.cpp:L114-L118](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/optimize/platformv2.cpp#L114-L118)：这段代码定义 `REGISTER_PLATFORM_V2` 宏并两次实例化——把 `"3510"` 注册为默认启用（`is_default_enabled=true`）、`"5102"` 注册为非默认。静态对象 `registrar_v2` 在 `main` 之前完成入表，这正是 u6-l2 讲过的「Pass 定义与 Pass 注册物理分离」在平台维度的翻版。
- [autofuse/v35/optimize/platformv2.cpp:L87-L107](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/optimize/platformv2.cpp#L87-L107)：这段代码是 `PlatformV2::GenerateTasks`——v2 平台的任务生成器顺序：**IndirectLoad 最先**，随后 Split→Cube→Concat→Transpose→Reduce，全部为空时才用 Recomputation 兜底。对比 u6-l4 讲过的 v1 顺序（Split→Concat→Transpose→Reduce→Recompute），v2 在最前面插入了 IndirectLoad 与 Cube 两个专属生成器，这正是「平台扩展改变调度主链路」的实证。
- [autofuse/v35/optimize/platformv2.h:L17-L29](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/optimize/platformv2.h#L17-L29)：这段代码声明 `PlatformV2 : public BasePlatform`，重载的六个虚函数（分区、对齐策略、Pass runner、模板生成器、BackendSpec、任务生成）就是平台差异的完整清单——主仓 `Optimizer`（u6-l1）只面向 `BasePlatform` 编程，对 v1/v2 无感知。

**ascir 侧：v2 算子注册与同名共存。**

- [autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp:L53](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L53)：这行定义 `v2_soc_versions{"3510", "5102"}`，是本文件所有注册共用的平台绑定向量。
- [autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp:L55-L61](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L55-L61)：这段代码以 `Square` 为样板展示了 v2 注册三要素：`.Input/.Output` 约定接口、`.ComputeType` 声明计算类别、`.Impl(v2_soc_versions, {AttImplCreator, CodegenImplCreator, dtype 约束})` 绑定平台实现。按 u5-l1 的结论，同名算子在注册表中走 `AppendSocImpl` 合并——`Square` 的 v1 实现与这里的 v2 实现共存于同一条目，查询时按平台字符串取出对应一份。
- [autofuse/v35/ascir/CMakeLists.txt:L1-L19](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascir/CMakeLists.txt#L1-L19)：这段代码是「构建期合流」的直接证据：`file(GLOB_RECURSE SOURCES "*.cpp")` 抓到的全部 v2 注册源码，被**同时**追加给 `aihac_codegen`（运行库）与 `ascir_builtin_ops`（主仓的算子注册目标），且 include 路径同时含主仓 `ascir` 与 `v35/ascir` 两级——v2 实现类直接 include 主仓的注册框架头。

**att 侧：v2 性能公式以 `V2` 后缀入表。**

- [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:L64-L67](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L64-L67)：这段代码定义 `ApiPerfRegisterV2` 辅助函数——把性能公式以 `api_name + "V2"` 的名字注册进 `ApiPerfFactory`，与 v1 的同名公式（不带后缀）在同一张表里按名字区分。配合 [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:L499-L511](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L499-L511) 的 `REGISTER_EVAL_FUNC_TAG(kStore, V2, ascir_v2::StoreApiV2)` 系列批量注册，v2 平台的 ATT 查询（u7-l1 的 `EvalCosts`）就能命中带 `V2` 后缀的公式与 `perf_param_v2.cpp` 的参数表。

**codegen 侧：v35 设备端定义按文件名入表。**

- [autofuse/v35/codegen/ascendc_reg_base_api_register.cpp:L15-L35](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/ascendc_reg_base_api_register.cpp#L15-L35)：这段代码声明一个匿名命名空间里的 `Register` 类并在构造函数里把几十个 `*_reg_base.h`（sed 生成的原始字符串头）读入 `std::string`——本次更新在此新增了 `i0/i0e/i1e`、`log_ndtr/next_after/polygamma`、`chebyshev_polynomial_t/u/v/w`、`hermite_polynomial_h/he` 等特殊函数条目。
- [autofuse/v35/codegen/ascendc_reg_base_api_register.cpp:L386](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/codegen/ascendc_reg_base_api_register.cpp#L386)：这行 `AscendCApiRegistry::GetInstance().RegisterApi(api_to_file)` 把「头文件名 → 内容」的大表一次性登记进 u8-l3 讲过的注册表单例；静态 `Register` 对象保证在库加载时完成。codegen 生成 kernel 时按图中算子 `LoadApiHeaderFiles` 声明的文件名来此取内容，v1/v2 的头文件名不冲突，天然分流。

#### 4.2.4 代码实践

**实践目标**：亲手验证「构建期合流、运行期分流」的两端。

**操作步骤**：

1. 在 [autofuse/v35/att/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/CMakeLists.txt#L1-L8) 与 `autofuse/v35/codegen/CMakeLists.txt` 中确认 `target_sources(aihac_codegen PRIVATE ...)` 语句——所有 v35 源码最终都进了这同一个 target。
2. 执行 `grep -rn "REGISTER_PLATFORM" autofuse/v35/optimize autofuse/optimize/platform/v1`，列出全仓的平台注册点（预期共 3 处：v1 一处、v2 两处）。
3. 执行 `grep -c "REG_ASC_IR" autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp` 与 `grep -c "REG_ASC_IR" autofuse/ascir/generator/ascir_builtin_ops_v1.cpp`，对比两平台注册算子数量。

**需要观察的现象**：v35 各子目录 CMake 都引用 `aihac_codegen` 这个 target 名而非自建 target；平台注册点数量与平台字符串一一对应；v2 注册的算子数量明显多于或不少于 v1（数量以本地执行结果为准）。

**预期结果**：能写出「v35 源码 → aihac_codegen → 运行期四张表分流」的完整链路说明（grep 计数待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ascir_builtin_ops_v2.cpp` 里的 `Square` 注册不会覆盖主仓 v1 的 `Square`？

**答案**：u5-l1 讲过 ASCIR 注册表对同名算子按 `AppendSocImpl` 合并而非覆盖——`REG_ASC_IR(Square).Impl(v2_soc_versions, ...)` 只是把 v2 实现追加到 `Square` 条目的平台实现列表中；查询接口（如 `GetAscIrCodegenImpl(platform, type)`）按平台字符串选取，两套实现共存。

**练习 2**：`PlatformV2::GenerateTasks` 里 `CubeFusionCaseGenerator` 出现在 v1 没有的位置，这说明了什么设计原则？

**答案**：说明「平台扩展不改主流程代码，只改平台自己的分发实现」——`Optimizer`（u6-l1）调用的是 `BasePlatform::GenerateTasks` 虚函数，v1/v2 各自决定有哪些 case generator、什么顺序。新增 cube 融合能力时只需改 `platformv2.cpp`，主仓 `optimize/optimize.cpp` 一行不动，符合「平台差异收敛在平台类」的隔离原则。

**练习 3**：v2 性能公式为什么要加 `V2` 后缀入表，而不是沿用 v1 的名字直接覆盖？

**答案**：因为 `ApiPerfFactory` 是跨平台共享的一张全局表，而 v1/v2 公式必须共存（同一个 `.so` 同时服务两类平台）；加后缀是最朴素的命名空间手段，查询侧按平台选择带或不带后缀的名字，避免运行期分支侵入公式本身。

---

### 4.3 v2 版本扩展

#### 4.3.1 概念说明

「v2 后缀文件」指 v35 目录下文件名带 `_v2` / `V2` 的源码，如 `ascir_builtin_ops_v2.cpp`、`binary_api_call_v2.h`、`polygamma_v2.cpp`、`ascir_api_perf_v2.cpp`、`perf_param_v2.cpp`。它们与主仓 v1 文件的关系是：

| 维度 | v1（主仓） | v2（v35） |
|------|-----------|-----------|
| 注册的算子名 | 相同（`Square`、`Add`……） | 相同——同名算子、平台不同实现 |
| 实现类名 | `XxxAscIrCodegenImpl` | `XxxAscIrCodegenImplV2` |
| 绑定平台 | `{"2201"}` | `{"3510", "5102"}` |
| 性能公式入表名 | `XxxApi` | `XxxApiV2` |
| 设备端 API | `autofuse/ascendc/api/*.h` → `*_str.h` | `autofuse/v35/ascendc/api_regbase/*.h` → `*_reg_base.h` |
| codegen 调用生成器 | `autofuse/codegen/api_call/...` | `v35/codegen/reg_api_call/..._v2.h` 等 |

本次更新（`00627d97..2b9c5c2a`）在 v2 线上落了三块新能力：

1. **v2 特殊函数算子注册链路**：`ascir_builtin_ops_v2.cpp` 新增 12 个 `REG_ASC_IR`（I0/I0e/I1e、LogNdtr、NextAfter、PolyGamma、ChebyshevPolynomialT/U/V/W、HermitePolynomialH/He），配套 `ascendc_reg_base_api_register.cpp` 新增对应 `*_reg_base.h` 条目与 `v2_ascir_codegen_impl.h` 新增实现类——完整链路见 u11-l5。
2. **NDDMA 1D 精确性能模型**：全新的 `nddma_model.h/cpp` 子模块，用搬运字节数、GM/UB stride、dtype 与核数刻画 NDDMA 搬运耗时——本模块在 4.3 源码精读展开，u11-l3 深入。
3. **IndirectLoad SIMD/SIMT 寻址优化**：`indirect_load_simd.h`（约 400 行改写）、`indirect_load_simt.h`、`indirect_load_simd_policy.h`（新增 `IndirectLoadSimdAddressMode` 等策略类型）与 `reg_indirect_load_api_call.cpp`（约 336 行增强）——见 u11-l4。

#### 4.3.2 核心流程

以本次新增的 NDDMA 精确模型为例（它是 v2 扩展「att 侧增量」的典型样本）。ATT 为图节点估算耗时时，对 Nddma 类节点先尝试新模型、失败则回退：

```text
Nddma 节点进入性能建模
  │
  ├─ 门禁检查：NodeInfo::is_cv_ub_fusion 为真（kUBFuse Codegen 路径）？
  │      └─ 是 → 记录 kCodegenMismatch，回退 legacy GetDmaPerf
  │
  ├─ BuildNddmaDescriptor：从 TensorShapeInfo 构造原始 descriptor
  │      （repeats→output_dims、gm_strides→input_strides、strides→output_strides，
  │        vectorized_axis 定轴序，dtype/block_dim 另行传入）
  │      └─ rank 不为 1 / 静态值非法 → 记录原因，回退 legacy
  │
  ├─ EvaluateNddmaModel：按 dtype_size 选参数组，构造 cycles 表达式
  │      cycles = low(block_dim<=2) 或 high(block_dim>2) 两条多项式，
  │      动态 os 用 g = max(0, min(1, os-1)) 在两个 os 分支间插值
  │
  └─ 输出单次调用 AIV_MTE2 cycles；全局 pipe head 仍由 PipePerfExpr 统一叠加
```

1D 特征与公式（摘自模型头文件注释）：

\[ B = output\_dims[0] \times dtype\_size,\quad s = \min(input\_strides[0],\ 128) \]

\[ low = L_0 + L_B B + L_s s + L_{Bs} B s \]

\[ high = C_0 + C_1 s + C_2 s^2 + B\,(E_0 + E_1 s + E_2 s^2) \]

#### 4.3.3 源码精读

**新增特殊函数算子的 v2 注册（本次 +104 行）。**

- [autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp:L171-L196](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L171-L196)：这段代码注册 `I0/I0e/I1e`（第一类修正贝塞尔函数族）——每个算子都是「输入约束 + 输出约束 + AttImplCreator + CodegenImplCreator + dtype 列表」的标准五件套，与 4.2.3 的 `Square` 样板完全同构，说明新增算子的边际成本极低。
- [autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp:L373-L428](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L373-L428)：这段代码注册 `ChebyshevPolynomialT/U/V/W` 与 `HermitePolynomialH/He`（切比雪夫/厄米特正交多项式族）；注意它们与更早的 `ShiftedChebyshevPolynomial*`（L342-L369）是不同算子——同族不同变体各自独立注册。
- [autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h:L1380](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L1380)：这行声明 `ChebyshevPolynomialTAscIrCodegenImplV2`，是上述注册引用的 codegen 实现类之一——本次更新该头文件增加了约 295 行，全部是这类「一个算子一个 Impl 类」的机械扩展（基类 `AscIrCodegenV2` 与 `GetApiCallName()/LoadApiHeaderFiles()` 接口见 [autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h:L142-L145](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h#L142-L145) 的同类样板 `SimtFloatUnaryAscIrCodegenImplV2`）。

**NDDMA 精确模型（本次全新子模块）。**

- [autofuse/v35/att/api_perf_register/nddma_model.h:L18-L77](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L18-L77)：这段注释是模型的完整「设计说明书」：明确当前只注册 `NDDMA_1D_MULTICORE_V1`（raw rank=1）；raw rank 2～5 保留完整 descriptor 后回退 legacy 模型、**不会因连续轴合并而伪装成 1D**；`kUBFuse` Codegen 路径因使用 `{curAivM, curAlignN}` 与固定 2D stride、和 raw descriptor 不等价，经 `is_cv_ub_fusion` 门禁回退。同时给出 Codegen 映射表（repeats→output_dims 等）与上面两条多项式。
- [autofuse/v35/att/api_perf_register/nddma_model.h:L72](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L72)：这行定义 `enum class NddmaFallbackReason`——把回退原因枚举化（kNoDescriptor / kRankUnsupported / kSchemaMismatch / kDtypeUnsupported / kStrideInvalid / kCodegenMismatch / kNoRegisteredModel），配合 `LogNddmaFallback` 每次记录一个稳定 reason，便于板上定位「为什么没走上新模型」。
- [autofuse/v35/att/api_perf_register/nddma_model.h:L107-L112](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/nddma_model.h#L107-L112)：这两行声明模型的两级入口 `BuildNddmaDescriptor` 与 `EvaluateNddmaModel`——前者从 `TensorShapeInfo` 构造原始 descriptor（不从 legacy 标量 stride 反推），后者按 dtype 选参数、构造静态/动态 cycles 表达式。
- [autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp:L30-L53](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp#L30-L53)：这段代码是调用侧粘合层 `TryNewNddmaModel`——先查 `is_cv_ub_fusion` 门禁（不匹配即 kCodegenMismatch 回退），再 Build→Evaluate，成功则把 descriptor 存进 `node_detail.nddma_descriptor`（u7-l1 讲过的 `NodeDetail::NddmaDescriptorInfo` 透传通道）。它把新模型「外挂」在 legacy Nddma 公式之前，任何回退路径都原样保留，风险被限制在增量分支内。

**IndirectLoad 优化（概览，详见 u11-l4）。**

- [autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h:L277](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/indirect_load_simd_policy.h#L277)：这行定义 `enum class IndirectLoadSimdAddressMode`——本次优化为 SIMD 路径引入的寻址模式枚举，是策略层的核心新类型；同文件 L392 的 `IndirectLoadSimdModeTraits` 把模式映射到寄存器 trait。
- [autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h:L27](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/reg_api_call/reg_indirect_load_api_call.h#L27)：这行声明 `IndirectLoadRegApiCall final : public ApiCall`——消费上述策略、生成地址计算与访存代码的 api_call 生成器，对应 .cpp 本次增强约 336 行。

#### 4.3.4 代码实践

**实践目标**：以「PolyGamma」为例，追踪一个本次新增的 v2 算子横跨 v35 四个子目录的完整落点。

**操作步骤**（源码阅读型）：

1. `grep -n "PolyGamma" autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp`——找到注册块（约 L284-L295），记下它引用的 Att/Codegen 实现类名。
2. `grep -rn "PolyGamma" autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h autofuse/v35/ascir/reg_func/polygamma_v2.cpp autofuse/v35/att/api_perf_register/ascir_api_perf_v2.cpp`——确认三处落点：codegen 实现类、tmp buf sizing（reg_func）、性能公式。
3. `grep -n "polygamma_reg_base" autofuse/v35/codegen/ascendc_reg_base_api_register.cpp` 与 `grep -n "polygamma" autofuse/v35/ascendc/api_regbase/CMakeLists.txt`——确认设备端定义的入表与头文件清单。
4. 对照 [autofuse/v35/ascendc/api_regbase/polygamma.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/polygamma.h)（本次 +9 行）阅读设备端封装本体。

**需要观察的现象**：一个算子的名字出现在 v35 的至少 5 个文件里（builtin_ops 注册、codegen_impl、reg_func、api_perf、reg_base_api_register + CMake 清单），且每一处的修改模式与既有算子（如 bessel 族）完全一致。

**预期结果**：写出「新增一个 v2 算子的五处落点清单」，这正是 u11-l5 综合实践的输入（grep 行号以本地执行为准，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`polygamma_v2.cpp` 与主仓 `autofuse/ascir/reg_func/reduce.cpp` 是什么关系？

**答案**：二者同属 u5-l2 讲过的 reg_func 家族——codeen 实现类 `CalcTmpBufSize()` 的外置实现体，返回 `vector<TmpBufDesc>` 给 ATT 与 buffer_allocate。区别只在平台：`*_v2.cpp` 被 `v35/ascir/CMakeLists.txt` 的 `GLOB_RECURSE` 抓走、编进 v2 实现类，主仓 `reduce.cpp` 服务 v1 平台的 `Reduce`。

**练习 2**：NDDMA 新模型为什么不直接替换 legacy 公式，而要设计成一整套回退路径？

**答案**：因为新模型目前只覆盖 raw rank=1、默认 Codegen 路径；rank 2～5 无正式模型、`kUBFuse` 路径的 shape 语义与 raw descriptor 不等价。保留 legacy 作为兜底，配合 `NddmaFallbackReason` 枚举记录每一次回退原因，既保证未覆盖场景行为不变（编码红线的确定性要求），又给后续扩展留下「在归一化阶段构造 effective view」的清晰接缝（头文件注释的「扩展约束」段）。

**练习 3**：`ShiftedChebyshevPolynomialT`（旧）与本次新增的 `ChebyshevPolynomialT` 有何区别，为什么要分开注册？

**答案**：二者是数学上不同的多项式变体（平移切比雪夫 vs 标准切比雪夫），输入输出约束与设备端实现（`shifted_chebyshev_polynomial_utils.h` vs 本次新增的 `chebyshev_polynomial_utils.h`）都不同；ASCIR 注册表按算子名索引，语义不同的算子必须独立注册，各自绑定 `v2_soc_versions` 平台实现。

---

### 4.4 平台开关与构建

#### 4.4.1 概念说明

v35 的「启用」分三层，理解这三层就理解了平台开关的全貌：

1. **构建层**：`autofuse/CMakeLists.txt` 的 `IS_DIRECTORY` 守卫决定 v35 源码是否参与编译——这是唯一的编译期开关，且默认「目录在即编译」。
2. **注册层**：编译成功后，`REGISTER_PLATFORM_V2("3510", v2, true)` 与 `("5102", V2_1, false)` 的第三参 `is_default_enabled` 控制「平台信息查询失败时是否默认启用该平台」——`3510` 是默认平台，`5102` 不是。
3. **运行层**：`PlatformContext` 向 runtime 查询当前 SoC 字符串，`PlatformFactory` 据此取 `PlatformV2` 或 `PlatformV1`；ASCIR/ATT/Registry 三张表按同一字符串或 `V2` 后缀名分流。

另外，v35 设备端头文件要进 kernel 源码，还依赖一条**构建期文本加工管线**：`sed` 把每个 `.h` 包成原始字符串字面量，供 `#include` 进 `std::string` 后入表。

#### 4.4.2 核心流程

```text
构建期：
  autofuse/CMakeLists.txt
    └─ if (IS_DIRECTORY v35) → add_subdirectory(v35)
         ├─ ascendc/api_regbase：foreach 头文件 → sed 加 R"===( ... )===" 包装
         │      → 生成 <name>_reg_base.h（BUILD 目录）
         │      → 接口库 ascendc_api_regbase_extend
         ├─ ascendc/api_cube：同样管线 → <name>_str.h + ascendc_api_cube_extend
         ├─ ascir / codegen / optimize / att：target_sources(aihac_codegen ...)
         └─ 产物：全部编进 libaihac_codegen.so（无 v35 专属 .so）

运行期（进程启动时）：
  静态对象初始化（main 之前）：
    registrar_v2   → PlatformFactory 注册 "3510"
    registrar_V2_1 → PlatformFactory 注册 "5102"
    Register       → AscendCApiRegistry 登记 api_to_file 大表
    （ascir/att 的注册表同样在此阶段填充）
  首次编译请求：
    PlatformContext 查 SoC → "3510" → PlatformV2 → v2 Pass 序列 / 任务生成顺序
```

#### 4.4.3 源码精读

- [autofuse/CMakeLists.txt:L146-L148](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt#L146-L148)：这段代码是构建层开关——`if (IS_DIRECTORY "${CODE_ROOT_DIR}/v35") add_subdirectory(v35)`。与 u1-l2 讲过的 `BUILD_AUTOFUSE`（组件级开关）层级不同：那个控制整个 Autofuse 组件对外的编译与否，这个控制 Autofuse **内部** v35 平台扩展的裁剪，且无须用户传任何 CMake 变量。
- [autofuse/v35/ascendc/api_regbase/CMakeLists.txt:L1-L93](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L1-L93)：这份清单列出全部参与包装的 regbase 头文件（本次更新在其中追加了 `polygamma.h` 等条目），是「v2 设备端算子能力」的权威花名册。
- [autofuse/v35/ascendc/api_regbase/CMakeLists.txt:L95-L122](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L95-L122)：这段代码是文本加工管线——`foreach` 对每个头文件执行 `cat ${header} | sed '1i\R"===(' | sed '$a\)===" '` 生成 `<name>_reg_base.h`，再用 `add_library(ascendc_api_regbase_extend INTERFACE)` 把生成目录暴露成接口库。[autofuse/v35/ascendc/api_cube/CMakeLists.txt:L21-L40](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/CMakeLists.txt#L21-L40) 对 cube 头执行完全相同的管线（产物后缀 `_str.h`），并同样收口为 `ascendc_api_cube_extend` 接口库、由 [autofuse/v35/codegen/CMakeLists.txt:L12-L15](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/CMakeLists.txt#L12-L15) 链给 `aihac_codegen`——与 u5-l3 讲的主仓 `*_str.h` 机制一字不差，v35 是纯复用。
- [autofuse/v35/optimize/platformv2.cpp:L117-L118](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/optimize/platformv2.cpp#L117-L118)：这两行是注册层的「默认平台」语义——`REGISTER_PLATFORM_V2("3510", v2, true)` 与 `("5102", V2_1, false)`：同为 v2 平台类，`3510`（昇腾 950）允许在拿不到明确平台信息时作为默认，`5102` 必须显式匹配才启用。对比主仓 [autofuse/optimize/platform/v1/platformv1.cpp:L95-L98](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/optimize/platform/v1/platformv1.cpp#L95-L98) 的 `REGISTER_PLATFORM_V1("2201", v1, false)`，可见「谁是默认平台」是随芯片代际演进而迁移的。
- [autofuse/v35/ascendc/api_cube/matmul.h:L81-L108](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_cube/matmul.h#L81-L108)：这段代码展示 v35 设备端代码里的**编译期平台开关**——`mat_mul_v3` 模板入口在 `CV_UB_FUSION` 宏定义与否两种情况下有不同的函数签名（融合上下文多一个 `AutoFusionVector::Params *param` 参数），L94 的 `#if !(defined(__NPU_ARCH__) && (__NPU_ARCH__ == 5102))` 则直接按硬件架构号裁剪代码路径。也就是说：构建层管「目录级」开关，设备端宏管「语句级」开关，二者配合完成同一份源码对 `3510`/`5102` 及融合/非融合场景的适配。

#### 4.4.4 代码实践

**实践目标**：验证三层开关的每一层都能在源码中指认。

**操作步骤**：

1. **构建层**：打开 [autofuse/CMakeLists.txt:L146-L148](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/CMakeLists.txt#L146-L148)，把 `IS_DIRECTORY` 守卫抄进笔记；然后在本地构建一次后检查 `build` 目录下是否存在 `autofuse/v35` 的对象文件目录（`find build -path "*v35*" -name "*.o" | head`）。
2. **注册层**：`grep -rn "REGISTER_PLATFORM" autofuse/ --include=*.cpp`，把三处注册（平台名、后缀、is_default_enabled）整理成表。
3. **文本加工层**：构建后执行 `find build -name "*_reg_base.h" | head -5` 并 `head -3` 其一，观察 `R"===(` 首行包装；再对照 [autofuse/v35/ascendc/api_regbase/CMakeLists.txt:L103-L114](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_regbase/CMakeLists.txt#L103-L114) 的 `add_custom_command` 理解它的来源。
4. **（可选，需完整 CANN 环境）**：`sh build.sh --pkg -j 8` 后在安装产物 `lib64` 下确认只有一个 `libaihac_codegen.so`、没有 v35 专属动态库。

**需要观察的现象**：构建树里 v35 的对象文件与主仓混排在同一 target 下；`*_reg_base.h` 只存在于构建目录（源码目录没有）；注册表 grep 恰好命中三处。

**预期结果**：一张「三层开关 × 证据文件」对照表。构建类命令需本地 CANN 环境支持，标注为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果想让一个发行版完全不包含 950 支持，最小改动是什么？

**答案**：去掉或排除 `autofuse/v35/` 目录即可——`IS_DIRECTORY` 守卫会让 CMake 静默跳过 `add_subdirectory(v35)`，无需改任何 CMake 变量或源码；代价是 `3510/5102` 平台运行时在 `PlatformFactory` 中查不到 creator 而返回 `nullptr`（打 WARN 日志）。

**练习 2**：为什么 `*_reg_base.h` 生成在构建目录而不是源码目录？

**答案**：它是构建产物（对源头的 .h 做文本加工的结果），遵循「源码目录只放手写文件」的卫生原则；`add_custom_command` 以原头文件为 `DEPENDS`，源头一改即自动重新生成，避免手工同步。这也与 u5-l3 主仓 `*_str.h` 的做法一致。

**练习 3**：`is_default_enabled` 的实际消费点在哪里？

**答案**：它被写进 `BackendSpec`（见 [autofuse/v35/optimize/platformv2.cpp:L55-L87](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/optimize/platformv2.cpp#L55-L87) 中 `ret->is_default_enabled = config_.is_default_enabled`），供下游在平台信息不可得时决定是否按默认平台放行——`3510` 为 true 意味着查询失败时仍按 v2 行为兜底，`5102` 为 false 意味着必须显式匹配。

## 5. 综合实践

**任务：绘制「v35 专属扩展全景对照表」并验证一条端到端挂接链。**

1. **列表**：以 `ls -R autofuse/v35` 为素材，按五大模块（ascendc/ascir/optimize/att/codegen）整理 v35 专属扩展清单，至少覆盖：api_cube（matmul/conv2d）、api_regbase（含本次优化的 indirect_load_simd/simt/policy、新增特殊函数头）、ascir（builtin_ops_v2、reg_func_v2、v2_ascir_codegen_impl）、optimize（PlatformV2、pass_runner_v2、un_alignment_strategy、IndirectLoad 任务生成器）、att（perf_param_v2、ascir_api_perf_v2、**nddma_model**）、codegen（cube/micro/reg/vec_func 四类 api_call）。
2. **连线**：为「Nddma 算子」画一条从注册到落地的完整链路：`ascir_builtin_ops_v2.cpp` 的 `REG_ASC_IR(Nddma)`（L510-L521 附近）→ `PlatformV2::BroadcastTypes()` 返回 `{Broadcast, Nddma}`（platformv2.cpp:L110-L112）→ att 侧 `TryNewNddmaModel` → nddma_model 双入口 → codegen 侧 `reg_nddma_api_call` 与设备端 `datacopy_nddma.h`。
3. **对比**：任选三个 v2 后缀文件（如 `binary_api_call_v2.h`、`compare_v2.cpp`、`reduce_v2.cpp`）与主仓同名 v1 文件 diff，归纳 v2 与 v1 在接口签名、dtype 集合、stride 处理上的差异模式。
4. **验证**：用 4.4.4 的方法确认构建层合流（无 v35 专属 .so）与 `*_reg_base.h` 生成管线；有环境时跑 `sh build.sh --pkg -j 8` 收尾。

产出物：一张 Markdown 对照表 + 一条标注了文件与行号的链路图。此实践同时是 u11-l2（cube 算子与 cv 融合）、u11-l3（nddma 模型）、u11-l4（IndirectLoad）、u11-l5（特殊函数注册链路）四篇讲义的导览图。

## 6. 本讲小结

- `autofuse/v35/` 是昇腾 950（SoC 字符串 `3510`/`5102`）的平台增量目录，五个子目录与主仓 ascendc/ascir/optimize/att/codegen 一一对应，遵循「目录即平台」。
- v35 的挂接策略是「构建期合流、运行期分流」：源码经 `target_sources(aihac_codegen ...)` 编进同一共享库，靠 `PlatformFactory`、ASCIR 注册表、`ApiPerfFactory`（`V2` 后缀名）、`AscendCApiRegistry`（`*_reg_base.h` 文件名）四张表在运行期按平台分流。
- v2 后缀文件与 v1 的关系是「同名算子、不同平台实现」：实现类加 `V2` 后缀、绑定 `v2_soc_versions`、经 `AppendSocImpl` 与 v1 共存，查询按平台字符串取用。
- 平台开关分三层：`IS_DIRECTORY` 守卫的 `add_subdirectory(v35)`（构建层）、`REGISTER_PLATFORM_V2` 的 `is_default_enabled`（注册层，`3510` 默认启用）、运行期 SoC 字符串查询（运行层）；设备端另有 `CV_UB_FUSION`、`__NPU_ARCH__` 等语句级宏。
- 本次更新在 v2 线上新增三块能力：12 个特殊函数算子的完整注册链路（chebyshev/hermite/polygamma 等）、全新的 NDDMA 1D 精确性能模型（`nddma_model`，带枚举化回退门禁）、IndirectLoad SIMD/SIMT 寻址优化——分别由 u11-l5、u11-l3、u11-l4 展开。
- v35 设备端头文件经与主仓相同的 `sed` 原始字符串管线（`*_reg_base.h` / `*_str.h`）进入 `AscendCApiRegistry`，是 u5-l3 机制的纯复用。

## 7. 下一步学习建议

- 下一讲 **u11-l2「Cube 类算子（matmul/conv2d）与 cv 融合」**：深入本讲只点到为止的 `api_cube` 与 `mat_mul_v3` 模板分支、CV tiling wrapper 复用编译与 dtype 感知融合。
- 若关注性能建模，先读 **u7-l1/u7-l2** 打好 ATT 基础，再进 **u11-l3「v35 NDDMA 1D 精确性能模型」**精读 `nddma_model.cpp` 的参数表与表达式构造。
- 若关注代码生成，**u8-l3**（api_call 体系）之后进 **u11-l4「IndirectLoad SIMD/SIMT」**看策略如何驱动生成器。
- 想动手加算子的读者，以 **u11-l5「v2 特殊函数算子注册链路」**为模板，对照本讲 4.3.4 的五处落点清单练习。
