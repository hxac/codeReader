# Autofuse 测试体系（framework/ascendc_api/e2e）

> 本讲为 update 版本（对应 HEAD `2b9c5c2a`）。相比上一版本，`scripts/test/run_autofuse_test.sh` 的 `build_backend` 与 `codegen_e2e_st` 中 v35（昇腾 950）用例清单大幅扩充，新增了 chebyshev/hermite 特殊函数用例、i0/i0e/i1e 等 Bessel 类用例和一大批 indirect_load SIMD/SIMT/SK 用例；本讲按最新源码重写。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Autofuse 的三个测试模块（`autofuse_framework` / `autofuse_ascendc_api` / `autofuse_e2e`）各自覆盖什么。
2. 追踪一条完整调度链：`build.sh -u/-s --module=...` → `autofuse_module_test_suite()` → `run_autofuse_test.sh -u/-s -m <module>` → 各构建函数 → `ctest`。
3. 理解 v35 平台 `backend_e2e_v2` 用例的两级注册机制：CMake `add_subdirectory` + 脚本里的 `MAKE_TARGET_LIST` 显式登记，缺一不可。
4. 会用 `build.sh` 跑指定模块的 UT/ST，并开启 `-c` 覆盖率。

## 2. 前置知识

- **UT（Unit Test，单元测试）**：验证单个模块（如 optimize、att、codegen）内部逻辑，用 gtest/pytest 编写，不需要真实网络。
- **ST（System Test，系统测试）**：把「图输入 → 调度 → tiling → codegen → 生成 kernel」整条链路跑通，e2e（end-to-end）类用例还会实际执行生成的 kernel 并校验结果。
- **ctest**：CMake 自带的测试驱动器，按 `add_test()` 注册的名字和 `LABELS` 属性筛选运行。本讲会反复看到 `ctest -L st -L build_backend_test1 -R "^(...)$"` 这样的组合过滤。
- **lcov/genhtml**：把 gcc 的 `-fprofile-arcs -ftest-coverage` 插桩产物汇总成 HTML 覆盖率报告的工具。
- **两阶段 e2e 用例**：Autofuse 的 e2e 用例分两步——先用「codegen 生成器」可执行文件生成 kernel 源码，再编译执行生成的 kernel。两步对应两个 ctest 标签 `build_backend_test1` / `build_backend_test2`。
- 本讲依赖 u1-l3 对 `build.sh`「模块 × 实现 × 动作」三维选择体系和 `MODULE_ACTION_HANDLERS` 路由表的认知，不再重复展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [build.sh](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/build.sh) | 顶层入口：解析 `-u/-s/-c/--module`，经路由表转发到 `run_autofuse_test.sh` |
| [scripts/test/run_autofuse_test.sh](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh) | Autofuse 测试的实际编排者：cmake 配置、make 各测试目标、ctest 运行、覆盖率收集 |
| [autofuse/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt) | 测试编译开关（`RUN_TEST`）、覆盖率插桩、`tests/` 与 `v35/` 的装配 |
| [autofuse/tests/v35/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/CMakeLists.txt) | v35 测试子树的装配入口，挂载 `backend_e2e_v2` 等目录 |
| [autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt) | 逐个 `add_subdirectory` 登记每条 backend e2e 用例 |
| [autofuse/tests/v35/st/backend_e2e_v2/backend_e2e.cmake](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/backend_e2e.cmake) | 定义 `backend_e2e_st_test`：为每条用例生成 `_codegen_v2` 与 `_e2e_v2` 两个目标 |
| [autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/CMakeLists.txt) | indirect_load 用例工厂：一个函数批量生成数十条 SIMD/SIMT/SK 用例 |

## 4. 核心概念与源码讲解

### 4.1 测试模块划分

#### 4.1.1 概念说明

Autofuse 的测试在 `build.sh` 层面被组织成三个模块名，本质上是对 `run_autofuse_test.sh -m` 参数的三种取值打包：

| build.sh 模块名 | 映射到 `-m` 的值 | 覆盖范围 |
|---|---|---|
| `autofuse_framework` | `framework` | 编译器框架各模块的 UT/ST：att、optimize、common、codegen（test_main）、Python 模块 |
| `autofuse_ascendc_api` | `ascendc_api` | AscendC API 层：`test_ascendc_api`、v35 的 `test_ascendc_api_v35`，以及 ascir/codegen/backend/kernel_tool 一串 ST |
| `autofuse_e2e` | `e2e` | 端到端：`codegen_e2e_st`（生成期望 kernel 源码比对 + 编译执行） |

注意路由表里 `autofuse_e2e` 只登记了 `all_st` 而没有 `all_ut`——e2e 没有独立的 UT 入口，若组合不存在会在 build.sh 层被跳过（u1-l3 已讲过的「合法组合登记」机制）。

#### 4.1.2 核心流程

```text
build.sh -u --module=autofuse_framework -j 8 -c
  └─ MODULE_ACTION_HANDLERS["autofuse_framework:all_ut"] = autofuse_module_test_suite
       ├─ action=all_ut  → test_option="-u"
       ├─ module=autofuse_framework → test_module="framework"
       └─ bash run_autofuse_test.sh -u -m framework -j 8 -c
```

#### 4.1.3 源码精读

路由表登记了三个 autofuse 模块到同一个处理函数 `autofuse_module_test_suite`：

[build.sh:31-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/build.sh#L31-L42) 定义 `SUPPORTED_MODULES` 与 `MODULE_ACTION_HANDLERS`，其中 `autofuse_framework:all_ut/all_st`、`autofuse_ascendc_api:all_ut/all_st`、`autofuse_e2e:all_st` 六个合法组合全部指向 `autofuse_module_test_suite`。

[build.sh:606-644](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/build.sh#L606-L644) 是翻译器 `autofuse_module_test_suite`：把 `all_ut/all_st` 翻译成 `-u/-s`，把 `autofuse_framework/autofuse_ascendc_api/autofuse_e2e` 翻译成 `-m framework/ascendc_api/e2e`，透传 `-j`、`--ascend_install_path`、`--ascend_3rd_lib_path`，并在 `ENABLE_COVERAGE=on` 时追加 `-c`，最后 `bash run_autofuse_test.sh "${test_args[@]}"` 完成转发。

#### 4.1.4 代码实践

1. **实践目标**：确认三个模块名与 `-m` 取值的映射关系。
2. **操作步骤**：运行 `sh build.sh --help`，再打开 `build.sh` 对照第 606–644 行。
3. **需要观察的现象**：help 中 `--module=<name>` 的说明与 `SUPPORTED_MODULES` 数组内容。
4. **预期结果**：能口述「`--module=autofuse_e2e` 只能配 `-s`，不能配 `-u`」。待本地验证（本讲义编写环境未实际执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `autofuse_e2e` 没有登记 `all_ut`？
**答案**：e2e 用例全部是「生成 kernel → 编译 → 执行比对」的系统级用例，归属 ST；run_autofuse_test.sh 的 `build_ut` 分支（`-m e2e` 会落到 `*)` 无效输入）也没有 e2e 路径，所以路由表干脆不登记这个组合，build.sh 层会跳过。

**练习 2**：`--module=autofuse_framework` 和 `--module=autofuse_ascendc_api` 最终传给 run_autofuse_test.sh 的参数有什么区别？
**答案**：只有 `-m` 的值不同（`framework` vs `ascendc_api`），其余（`-u/-s`、`-j`、`-c`、路径参数）完全一致；区别在 run_autofuse_test.sh 内部 `build_ut`/`build_st` 的 `case` 分支挑选的构建函数集合不同。

### 4.2 run_autofuse_test.sh 调度逻辑

#### 4.2.1 概念说明

`run_autofuse_test.sh` 是 Autofuse 测试的真正编排者。它的结构是典型的「解析选项 → cmake 配置 → make 目标 → ctest 运行」四段式，核心调度点是两个 `case` 语句：`build_ut`（按 `-m` 分发 UT）与 `build_st`（按 `-m` 分发 ST）。

#### 4.2.2 核心流程

```text
main()
  ├─ checkopts "$@"            # 解析 -u/-s/-c/-m/-j/--ascend_install_path 等
  ├─ set_test_ld_library_path  # 拼接测试用 LD_LIBRARY_PATH
  ├─ build_ascgen-dev          # cmake 配置 + make pyautofuse（基础设施）
  ├─ 若 ENABLE_UT=on  → build_ut   （case ${MODEL_NAME}）
  ├─ 若 ENABLE_ST=on  → build_st   （case ${MODEL_NAME}）
  └─ 若 ENABLE_COV=on → get_coverage（lcov + genhtml）
```

`build_st` 中与三个模块相关的分支：

- `-m e2e` → `codegen_e2e_st`
- `-m backend` → `build_backend`（e2e 的实际执行者，也被 `ascendc_api` 复用）
- `-m framework` → `build_st_att` + `build_st_common` + `build_st_optimize` + `py_module_st`
- `-m ascendc_api` → `build_test_ascir_st` + `build_st_codegen` + `build_backend` + `build_kernel_tool`

#### 4.2.3 源码精读

[scripts/test/run_autofuse_test.sh:65-143](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L65-L143) 是选项解析 `checkopts`：默认 `ENABLE_UT/ENABLE_ST/ENABLE_COV=off`、`THREAD_NUM=8`、`MODEL_NAME="all"`；并在第 86–88 行做了一件对 v35 至关重要的事——若 `${AUTOFUSE_PATH}/v35` 目录存在则把 `RUN_V35_TESTS` 置为 `on`，这是 v2 用例能否被调度的总开关。

[scripts/test/run_autofuse_test.sh:1118-1173](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L1118-L1173) 是 `build_st` 的分发 `case`：`e2e` 分支只调 `codegen_e2e_st`；`backend` 分支调 `build_backend`；`ascendc_api` 分支串起 ascir st、codegen st、backend、kernel tool 四件事；任何一步失败都会 `exit 1` 短路退出。

[scripts/test/run_autofuse_test.sh:1175-1207](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L1175-L1207) 是 `main`：先 `build_ascgen-dev` 打底（cmake 配置 + `make pyautofuse`），随后按开关依次进入 UT、ST、覆盖率阶段——这解释了为什么跑任何一组测试前都要先经历一次较长的 cmake 配置。

[autofuse/CMakeLists.txt:130-155](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt#L130-L155) 是 CMake 侧的装配开关：`RUN_TEST` 有定义时给全局加 `-fprofile-arcs -ftest-coverage` 插桩；`${CODE_ROOT_DIR}/v35` 目录存在才 `add_subdirectory(v35)`；`RUN_TEST EQUAL 1` 且 `tests/` 存在才 `add_subdirectory(tests)`——所以测试目标只在测试配置下生成，正常打包构建不含测试代码。

#### 4.2.4 代码实践

1. **实践目标**：画出一条 `-s -m e2e` 请求的完整调用链。
2. **操作步骤**：
   - 通读 `run_autofuse_test.sh` 的 `main` → `build_st` → `codegen_e2e_st`；
   - 在 `codegen_e2e_st`（第 543–652 行）里找到两轮 `make` + 两轮 `ctest`：先 `make $MAKE_TARGET_LIST_CODEGEN` 跑生成器（标签 `codegen_e2e_st_test1`），再 `make $MAKE_TARGET_LIST` 编译执行 kernel（标签 `codegen_e2e_st_test2`）。
3. **需要观察的现象**：两轮 ctest 各自的 `-L` 标签不同。
4. **预期结果**：调用链为 `main → build_st(e2e) → codegen_e2e_st → make+ctest(test1) → make+ctest(test2)`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`-m codegen` 与 `-m e2e` 的 ST 有什么重叠？
**答案**：`codegen` 分支 = `build_st_codegen` + `codegen_e2e_st` + `py_module_st`，即 codegen 的 ST 包含了完整的 e2e；`e2e` 是只想跑端到端时的瘦身入口。

**练习 2**：为什么 `RUN_V35_TESTS` 的判定放在 `checkopts` 里而不是硬编码？
**答案**：v35 是平台增量目录，某些裁剪场景可能不存在；用 `[ -d ... ]` 探测可以在目录缺失时自动退化为纯 v1 测试集，避免脚本引用不存在的 make 目标而失败。

### 4.3 backend_e2e_v2 用例组织

#### 4.3.1 概念说明

`backend_e2e_v2` 是 v35（昇腾 950）平台的端到端用例集，目录在 [autofuse/tests/v35/st/backend_e2e_v2/](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt)，当前约有 169 个条目，本次增量新增了 chebyshev_polynomial_t/u/v/w、hermite_polynomial_h/he、shifted_chebyshev 系列、i0/i0e/i1e、log_ndtr、poly_gamma 等特殊函数用例，以及数十条 indirect_load SIMD/SIMT/SK 用例。

理解它的关键是「**两级注册**」：

1. **CMake 级**：每条用例一个子目录，子目录里的 `CMakeLists.txt` 调用 `backend_e2e_st_test(用例名 ...)` 生成 `_codegen_v2`（host 生成器）与 `_e2e_v2`（设备 kernel 测试）两个目标，并被父级 `CMakeLists.txt` 用 `add_subdirectory` 挂载；
2. **脚本级**：用例名必须再显式追加进 `run_autofuse_test.sh` 的 `build_backend` 函数里的 `MAKE_TARGET_LIST`（带 `_e2e_v2` 后缀），否则 CMake 会构建它，但 ctest 正则不会选中它。

**新增用例目录不会被自动发现**——这是本模块最重要的结论，漏做第 2 步是用例「编译通过却没跑」的最常见原因。

#### 4.3.2 核心流程

```text
build_backend()（run_autofuse_test.sh）
  ├─ MAKE_TARGET_LIST = v1 用例清单
  ├─ 若 RUN_V35_TESTS=on：追加 *_e2e_v2 用例清单（含 chebyshev/hermite/indirect_load...）
  ├─ MAKE_TARGET_LIST_CODEGEN = sed 's/e2e/codegen/g'  # 推导生成器目标名
  ├─ build_backend_test_regex() 把清单拼成 ctest 的 -R "^(名1|名2|...)$" 正则
  ├─ 第一轮：make *_codegen_v2 → ctest -L st -L build_backend_test1 -R 正则
  └─ 第二轮：make *_e2e_v2      → ctest -L st -L build_backend_test2 -R 正则
```

其中每条用例在 CMake 侧又是一个两阶段结构：

```text
backend_e2e_st_test(test_name ...)
  ├─ add_executable(test_name_codegen_v2 <CODEGEN 源>)        # host 生成器
  │    └─ add_test(... LABELS "st;build_backend_test1;...")
  ├─ add_custom_target(test_name_generated_sources_v2)         # 运行生成器产出 kernel 源码
  └─ add_executable(test_name_e2e_v2 <生成源 + TEST_SRC>)      # 设备侧测试（依赖上面的产物）
       └─ add_test(... LABELS "st;build_backend_test2;...")
```

#### 4.3.3 源码精读

[autofuse/tests/v35/CMakeLists.txt:125-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/CMakeLists.txt#L125-L127) 挂载三个 v35 测试子树：`ut/ascendc`、`st/backend_e2e_v2`、`st/codegen/e2e_v2`——这是 `backend_e2e_v2` 进入构建的第一级。

[autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt:109-118](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt#L109-L118) 是本次新增特殊函数用例的挂载点：`shifted_chebyshev_polynomial_t/u/v/w_store_test`、`chebyshev_polynomial_t/u/v/w_store_test`、`hermite_polynomial_h/he_store_test` 共 10 个子目录在此 `add_subdirectory`（第 149 行还挂载了 `indirect_load_store_test`）。

[autofuse/tests/v35/st/backend_e2e_v2/backend_e2e.cmake:1-88](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/backend_e2e.cmake#L1-L88) 定义 `do_backend_e2e_st_test`：第 26–27 行按用例名拼出 `${TEST_NAME}_codegen_v2` 与 `${TEST_NAME}_e2e_v2` 两个目标名；第 66–67 行给生成器打上 `st;build_backend_test1` 标签；第 69–73 行用 `add_custom_target` 让生成器在用例工作目录里跑、以 `BYPRODUCTS` 声明生成的 kernel 源文件；第 75–83 行把生成源编进 `_e2e_v2` 可执行文件（链接 `tikicpulib_ascend950pr_9599` CPU 仿真库）并打上 `st;build_backend_test2` 标签。末尾的 `backend_e2e_st_test` 宏只是自动填 `WORKDIR`。

以新增的 chebyshev 用例为例，[autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/CMakeLists.txt:1-8](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/CMakeLists.txt#L1-L8) 声明四要素：CODEGEN 生成器源、KERNEL_SRC（三个待生成文件）、TEST_SRC（校验程序），并额外把 v35 的 `api_regbase` 头目录加进 include 路径。

indirect_load 用例展示了另一种组织法——**用例工厂**：[autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/CMakeLists.txt:1-33](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/indirect_load_store_test/CMakeLists.txt#L1-L33) 定义 `add_indirect_load_e2e_case(名, rank, axis, ...)`，通过 `IL_RANK/IL_AXIS/IL_TILING_KEY/IL_SELECTED_TEMPLATE` 等编译期宏把同一份生成器源参数化成不同用例；第 135–286 行一口气实例出 `indirect_load_rank2_axis1_simd`、`indirect_load_rank3_axis1_pow2_simt`、`indirect_load_rank4_axis2_sk` 等几十条用例，第 288–289 行还 `include` 两个外部 case 清单文件继续扩容。这对应 u11-l4 讲过的 SIMD/SIMT/SK 三种寻址模式的回归矩阵。

脚本侧，[scripts/test/run_autofuse_test.sh:727-902](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L727-L902) 的 `build_backend` 在 `RUN_V35_TESTS=on` 时把全部 `_e2e_v2` 用例名追加进 `MAKE_TARGET_LIST`——第 774–810 行是 indirect_load 家族、第 874–901 行是 i0/i0e/i1e、bessel、chebyshev、hermite 等特殊函数家族，与 CMake 子目录一一对应。[scripts/test/run_autofuse_test.sh:903-905](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L903-L905) 用 `sed 's/e2e/codegen/g'` 推导生成器目标名、用 `build_backend_test_regex`（第 654–664 行定义）拼成 `^(名1|名2|...)$` 正则，随后两轮 `make + ctest` 分别消费 `build_backend_test1`/`build_backend_test2` 标签。

#### 4.3.4 代码实践

1. **实践目标**：验证「两级注册」缺一不可，掌握新增 v2 用例的完整登记面。
2. **操作步骤**：
   - 在仓库中 `ls autofuse/tests/v35/st/backend_e2e_v2/ | grep chebyshev`，找到 8 个 chebyshev 用例目录；
   - 打开 `backend_e2e_v2/CMakeLists.txt` 确认每个目录都被 `add_subdirectory`；
   - 在 `run_autofuse_test.sh` 的 `build_backend` 中搜索 `chebyshev_polynomial_t_store_test_e2e_v2`，确认它出现在 `RUN_V35_TESTS=on` 分支的 `MAKE_TARGET_LIST` 里；
   - 做一次反向检查：目录存在但脚本清单里搜不到的用例（例如 `bf16_sigmoid_test`，其 `add_subdirectory` 在 CMakeLists 中被注释），说明「CMake 挂载」与「脚本调度」确实独立。
3. **需要观察的现象**：同名条目在 CMakeLists.txt 与 run_autofuse_test.sh 两处都能找到；被注释的用例只在其中一处出现。
4. **预期结果**：能写出新增一条 v2 e2e 用例的登记清单——① 建用例子目录并写 `backend_e2e_st_test(...)`；② 父 CMakeLists `add_subdirectory`；③ run_autofuse_test.sh `MAKE_TARGET_LIST` 追加 `<名>_e2e_v2`。待本地验证（需要完整构建环境）。

#### 4.3.5 小练习与答案

**练习 1**：`sed 's/e2e/codegen/g'` 是怎么把 `xxx_e2e_v2` 变成生成器目标名的？
**答案**：`backend_e2e.cmake` 里生成器目标名固定为 `${TEST_NAME}_codegen_v2`，而用例名本身常含 `e2e` 字样（如 `add_abs_test_e2e_v2`），`sed` 把其中 `e2e` 替换为 `codegen` 后恰好得到 `add_abs_test_codegen_v2`，与 CMake 侧命名约定严丝合缝——也因此**用例名里如果有多处 `e2e` 会被全部替换**，命名时必须小心。

**练习 2**：为什么 indirect_load 用例要用「工厂函数」而不是像 chebyshev 那样一目录一用例？
**答案**：indirect_load 的变化维度是 rank/axis/dtype/模板选择（SIMD/SIMT/SK）的正交组合，几十条用例共享同一份生成器与测试源，用编译期宏 `IL_*` 参数化可以零拷贝复用代码；chebyshev 每条用例的 kernel 结构不同（模板参数 n 的处理方式不同），独立目录更清晰。

**练习 3**：`ctest -R` 正则和 `-L` 标签为什么要同时用？
**答案**：标签保证只跑 `st` 类且属于本轮（test1 或 test2）的用例，正则把范围进一步收紧到 `MAKE_TARGET_LIST` 显式登记的用例——这正是「两级注册」里脚本级登记的执行机制：没进清单的用例即使被构建也不会被 ctest 选中。

### 4.4 UT/ST/coverage 选项

#### 4.4.1 概念说明

三个开关 `-u`（UT）、`-s`（ST）、`-c`（coverage）可以组合使用。覆盖率的实现链路是：CMake 在 `RUN_TEST` 有定义时给全部代码加 `-fprofile-arcs -ftest-coverage` 插桩 → 测试正常跑完 → `get_coverage` 用 lcov 汇总 `.gcda/.gcno`、剔除第三方与系统头 → genhtml 出报告。

#### 4.4.2 核心流程

```text
build.sh -u -c --module=autofuse_framework -j 8
  └─ run_autofuse_test.sh -u -m framework -j 8 -c
       ├─ build_ascgen-dev（cmake -D RUN_TEST=1 ...，触发 CMake 加覆盖率插桩）
       ├─ build_ut(framework)：att_ut → optimize_ut → test_common → test_main → py_module_ut
       └─ get_coverage：lcov -c → lcov -r（过滤）→ genhtml → cov/coverage_report/
```

#### 4.4.3 源码精读

[build.sh:66-70](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/build.sh#L66-L70) 是三个开关的 help 说明：`-u/--ut`、`-s/--st`、`-c/--coverage`，`--module` 默认所有受支持模块。

[autofuse/CMakeLists.txt:130-133](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/CMakeLists.txt#L130-L133) 在 `DEFINED RUN_TEST` 时给 C/C++ 全局加 `-fprofile-arcs -ftest-coverage`——这是覆盖率数据的来源，也意味着只要走测试配置，即使不传 `-c`，产物也带着插桩开销。

[scripts/test/run_autofuse_test.sh:504-541](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L504-L541) 是 `get_coverage`：先按 lcov 主版本（第 515–524 行，来自第 27 行 source 进来的 `support_multiple_versions_of_lcov.sh`）挑选容错参数与并行参数；第 526–531 行对整个 `build/` 目录采集生成 `cov/tmp.info`；第 534–539 行剔除 CANN 安装目录、`output/`、`third_party/`、`/usr/*`、metadef base 等非本仓代码；第 540 行 `genhtml` 落盘到 `cov/coverage_report/`。

[scripts/test/run_autofuse_test.sh:1204-1206](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L1204-L1206) 在 `main` 末尾按 `ENABLE_COV` 触发 `get_coverage`——覆盖率永远在 UT/ST 全部通过之后才收集，测试失败时 `exit 1` 短路，不会产出误导性的报告。

#### 4.4.4 代码实践

1. **实践目标**：掌握三条最常用的测试命令。
2. **操作步骤**（均需已安装 CANN Toolkit 并 source 环境，且务必带 `-j 8` 防 OOM）：
   - 跑 framework 的 UT：`sh build.sh -u --module=autofuse_framework -j 8`
   - 跑 e2e 的 ST：`sh build.sh -s --module=autofuse_e2e -j 8`
   - 带覆盖率跑：`sh build.sh -u -c --module=autofuse_framework -j 8`
3. **需要观察的现象**：前两条命令的日志里分别出现 `build_ut start, mode = framework.` 与 `build_backend execute success!`；第三条最后出现 `Generating coverage statistics...` 并生成 `cov/coverage_report/index.html`。
4. **预期结果**：三条命令分别触发 `build_ut` 的 framework 分支、`codegen_e2e_st`、以及 UT + lcov 报告。待本地验证（需要昇腾环境与 lcov/genhtml 工具链，help 中明确要求先装好且 gcc/g++ 版本匹配）。

#### 4.4.5 小练习与答案

**练习 1**：`-c` 为什么必须和 `-u` 或 `-s` 一起用？
**答案**：`-c` 只负责在测试跑完后调 `get_coverage` 收集数据；不跑测试就没有 `.gcda` 运行时数据，报告只能是零覆盖。build.sh 的 help 也写明「Without explicit test selection, run supported tests for the selected module」。

**练习 2**：`get_coverage` 为什么要剔除 `${ASCEND_INSTALL_PATH}/*` 等路径？
**答案**：lcov 对 `build/` 整目录采集会把链接进测试的系统库、第三方库（gtest、protobuf、CANN 头）的覆盖率一并算进来，既慢又稀释本仓指标；`lcov -r` 的过滤清单把这些噪声从 `coverage.info` 里删掉，报告只反映 graph-autofusion 自身代码。

**练习 3**：为什么 `DISABLE_COMPILATION_WERROR=ON` 会被 run_autofuse_test.sh 主动 export（第 29–30 行）？
**答案**：脚本注释写明是遗留 TODO——测试配置下仍有部分告警未清理，临时关掉 `-Werror` 让测试编译能通过；这是「已知债务」的显式标记，不是可以效仿的常态做法。

## 5. 综合实践

**任务：给 v35 新增一条 backend_e2e_v2 用例并跑通它。**

以仓库中已有的 `hermite_polynomial_h_store_test` 为模板，完成一次「纸上新增」＋「真实调度」：

1. **建目录与四件套**：在 `autofuse/tests/v35/st/backend_e2e_v2/` 下新建 `my_op_store_test/`，写 `CMakeLists.txt` 调用 `backend_e2e_st_test(my_op_store_test CODEGEN my_op_store_backend_generator.cpp KERNEL_SRC my_op_store_test_kernel.cpp my_op_store_test_tiling.cpp autofuse_tiling_data.h TEST_SRC test_e2e_my_op_store_kernel.cpp)`（参考 [chebyshev_polynomial_t_store_test/CMakeLists.txt:1-8](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/chebyshev_polynomial_t_store_test/CMakeLists.txt#L1-L8) 的写法）。
2. **CMake 挂载**：在 [backend_e2e_v2/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/CMakeLists.txt#L109-L118) 的 add_subdirectory 列表末尾加一行。
3. **脚本登记**：在 [run_autofuse_test.sh:727-902](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/scripts/test/run_autofuse_test.sh#L727-L902) `RUN_V35_TESTS=on` 分支的清单里追加 `my_op_store_test_e2e_v2`。
4. **调度验证**：运行 `sh build.sh -s --module=autofuse_e2e -j 8`，观察日志：第一轮 make 应出现 `my_op_store_test_codegen_v2` 目标，第一轮 ctest（标签 `build_backend_test1`）应包含它；第二轮同理出现 `my_op_store_test_e2e_v2`。
5. **反思题**：如果只做了第 1、2 步忘了第 3 步，构建会怎样？跑测试会怎样？
   **答案**：`make` 不会构建该用例（不在 `MAKE_TARGET_LIST`），ctest 的 `-R "^(...)$"` 正则也不含它——用例「静默消失」，既不报错也不运行，这正是两级注册要牢记的原因。

本实践串联了本讲全部四个模块：模块命名（4.1）→ 调度链路（4.2）→ 用例两级注册（4.3）→ build.sh 的 `-s` 入口与 `-j` 约束（4.4）。

## 6. 本讲小结

- `build.sh` 把三个模块名（`autofuse_framework/autofuse_ascendc_api/autofuse_e2e`）经路由表转发给 `autofuse_module_test_suite`，本质是给 `run_autofuse_test.sh` 拼 `-u/-s -m <值>` 参数。
- `run_autofuse_test.sh` 是四段式编排（checkopts → cmake 配置 → make 目标 → ctest），UT/ST 各有一个按 `MODEL_NAME` 分发的 `case`，任何一步失败立即 `exit 1`。
- v35 用例的总开关是 `RUN_V35_TESTS`：由 `autofuse/v35` 目录是否存在决定，控制 `_e2e_v2` 用例清单是否并入 `codegen_e2e_st` 与 `build_backend`。
- `backend_e2e_v2` 用例是「两级注册」：CMake 侧 `add_subdirectory` + `backend_e2e_st_test` 生成 `_codegen_v2/_e2e_v2` 双目标双标签，脚本侧 `MAKE_TARGET_LIST` 显式登记并由 `sed` 推导与 `ctest -R` 正则消费；本次新增的 chebyshev/hermite 特殊函数用例与 indirect_load 用例工厂都遵循该机制。
- 覆盖率由 `RUN_TEST` 配置下的 `-fprofile-arcs -ftest-coverage` 插桩供数，`get_coverage` 用 lcov 采集、`lcov -r` 剔除第三方后由 genhtml 落盘 `cov/coverage_report/`。
- 常用三条命令：`sh build.sh -u --module=autofuse_framework -j 8`（framework UT）、`sh build.sh -s --module=autofuse_e2e -j 8`（e2e ST）、加 `-c` 出覆盖率报告。

## 7. 下一步学习建议

- 下一讲 u12-l2 转向 SuperKernel 组件的测试与 golden 校验，可与本讲对比「Python 包 + C++ AOT」双路径测试与 Autofuse「ctest 标签驱动」的差异。
- 若想动手加深理解，建议按第 5 节综合实践真实新增一条用例，并阅读 [backend_e2e.cmake](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/v35/st/backend_e2e_v2/backend_e2e.cmake#L1-L88) 中 `_codegen_v2` 链接的库列表（optimize/att/codegen 等），体会 e2e 用例其实就是 u6–u8 讲的编译器流水线的可执行快照。
- 结合 u12-l3 的编码红线与跨特性检查，理解为什么「新增用例」这类改动也要求同时评估 CMake 与脚本两侧的一致性。
