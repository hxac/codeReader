# 单元测试体系

> 本讲承接 [u9-l1 CMake 构建系统与多架构产物](u9-l1-cmake-multi-arch.md)。u9-l1 讲清了「同一份源码如何长出多架构产物」，本讲回答「这些产物（以及 Python 工具）如何被自动验证」。本讲是 u9 单元的收尾。

## 1. 本讲目标

asc-tools 是一个「C++ 核心（cpudebug）+ 四个 Python 工具」的双语言项目，因此它的单元测试也必然是双语言的。学完本讲你应该能够：

1. 说清楚 `bash build.sh -t`、`--cpp_utest`、`--python_utest` 三条测试命令各自会触发什么。
2. 解释 `TEST_MOD` 这个 CMake 变量如何控制 `tests/ut` 与 `tests/py_ut` 两个子目录是否加入构建。
3. 读懂 C++ 单元测试的「多产品 × 多可执行文件」组织方式，理解 gtest/mockcpp 的角色。
4. 读懂 Python 单元测试的「pytest 运行 unittest + importlib 注入源码路径 + mock 隔离外部命令」套路。
5. 区分 `--cov`（覆盖率）与 `--asan`（地址消毒）两个编译插桩开关的作用范围与产物。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**单元测试（Unit Test, UT）** 是对项目里最小可测单元（一个函数、一个类）做隔离验证。asc-tools 把 UT 分成两族：

- **C++ UT**：用 Google Test（gtest）框架，验证 cpudebug 的 C++ 实现——主要是 API 校验器（api_check）、注册框架（regfwk）、ACL 桩（acl_stub）。
- **Python UT**：用 Python 标准库 `unittest` 写用例，但用 `pytest` 作为运行器，验证 msobjdump、optype_collector、show_kernel_debug_data 三个纯 Python 工具。

**LLT（Low Level Test，低层测试）** 是 CANN 体系里的术语，本质上就是单元/组件级的自动化测试。你在本仓会反复看到 `run_llt_test`、`run_python_llt_test`、`python_llt_run_and_check.sh` 这些命名，它们的 "llt" 就是这个意思。

**编译插桩（instrumentation）** 指在编译期往产物里注入「额外代码」以换取观测能力。本讲涉及两种：

- **覆盖率插桩**（gcov）：注入计数器，记录「哪些代码行被执行过」，用于度量测试充分度。
- **地址消毒插桩**（AddressSanitizer, ASAN）：注入内存访问检查，运行时发现越界、悬空指针等内存错误。

一个贯穿全讲的术语是 **`TEST_MOD`**：它是 `build.sh` 传给 CMake 的一个字符串变量，取值 `all` / `cpp` / `python`，决定了哪些测试子目录被编译。它是本讲的「分发总开关」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `build.sh` | 唯一编译入口；`-t`/`--cpp_utest`/`--python_utest`/`--cov`/`--asan` 等测试选项都在这里解析并翻译成 CMake 变量 |
| `CMakeLists.txt`（根） | 当 `ENABLE_TEST=ON` 时才 `add_subdirectory(tests)`，把测试挂入构建 |
| `tests/CMakeLists.txt` | 顶层测试 CMake，靠 `TEST_MOD` 决定加入 `ut` 还是 `py_ut` |
| `tests/ut/CMakeLists.txt` | C++ UT 的核心；为每个 NPU 产品生成一个 `tikcpp_utest_<product>` 和 `tikicpulib_utest_<product>` 可执行文件 |
| `tests/ut/main_global.cpp` | gtest 的 `main` 入口 |
| `tests/ut/testcase/acl_stub/test_acl_stub.cpp` | C++ UT 用例样例（ACL 桩） |
| `tests/ut/testcase/tikcpp_api_check/test_vec_binary_check.cpp` | C++ UT 用例样例（向量二元校验器，带参数化测试） |
| `tests/cmake/func.cmake` | 定义 `run_llt_test`（C++）与 `run_python_llt_test`（Python）两个关键函数 |
| `tests/cmake/intf.cmake` | 定义 `intf_llt_pub` 接口库，承载 gtest/mockcpp 链接与 `--cov`/`--asan` 编译开关 |
| `tests/py_ut/CMakeLists.txt` | Python UT 核心；声明三个 `run_python_llt_test` 目标 + 一个合并覆盖率目标 |
| `tests/cmake/tools/python_llt_run_and_check.sh` | 真正执行 `coverage run -m pytest` 的脚本 |
| `tests/cmake/tools/generate_cpp_cov.sh` | 用 lcov + genhtml 生成 C++ 覆盖率 HTML 报告 |
| `tests/py_ut/testcase/msobjdump/test_msobjdump.py` | Python UT 用例样例（msobjdump） |
| `tests/py_ut/testcase/optype_collector/test_optype_collector.py` | Python UT 用例样例（optype_collector） |

## 4. 核心概念与源码讲解

### 4.1 测试入口与 TEST_MOD 分发

#### 4.1.1 概念说明

要跑测试，你不会直接调用 CMake 或 pytest，而是从 `build.sh` 进入。`build.sh` 在这里扮演「翻译层」：把人用的命令行选项（`-t`、`--cov`）翻译成 CMake 能理解的变量（`-DTEST_MOD=all`、`-DENABLE_GCOV=true`），再交给 CMake 决定编译什么、链接什么。

整条链路有三个关键开关变量，全部由 `build.sh` 注入：

| CMake 变量 | 由哪个选项触发 | 作用 |
| --- | --- | --- |
| `ENABLE_TEST` | `-t` 或 `--cpp_utest` 或 `--python_utest` | 让根 CMake 把 `tests/` 挂入构建（`add_subdirectory(tests)`） |
| `TEST_MOD` | `-t`→`all`，`--cpp_utest`→`cpp`，`--python_utest`→`python` | 在 `tests/CMakeLists.txt` 里决定加 `ut` 还是 `py_ut` |
| `ENABLE_GCOV` / `ENABLE_ASAN` | `--cov` / `--asan` | 打开覆盖率 / 地址消毒插桩 |

注意：`-t`（全量）会同时设 `ENABLE_TEST=ON` 和 `TEST_MOD=all`；而 `--cpp_utest` / `--python_utest`（部分）只设 `ENABLE_TEST=ON`，`TEST_MOD` 由 `build_test_part` 函数稍后再补。

#### 4.1.2 核心流程

`build.sh` 处理一次测试请求的流程可以画成：

```
用户命令                build.sh 处理                           CMake 层
─────────              ─────────────                          ─────────
-t                →  TEST="all"                           →  -DENABLE_TEST=ON -DTEST_MOD=all
--cpp_utest       →  TEST_PART="cpp_utest"                →  -DENABLE_TEST=ON（TEST_MOD=cpp 在 build_test_part 里补）
--python_utest    →  TEST_PART="python_utest"             →  -DENABLE_TEST=ON（TEST_MOD=python 在 build_test_part 里补）
[可叠加] --cov    →  COV="true"                           →  -DENABLE_GCOV=true
[可叠加] --asan   →  ASAN="true"                          →  -DENABLE_ASAN=true
```

无论哪种测试，`build.sh` 都会：

1. 强制把 `BUILD_TYPE` 改成 `Debug`（测试需要带调试符号）。
2. 调 `cmake_config` + `build all`（或 `build_test_part`）。
3. C++ 可执行文件靠 CMake 的 `POST_BUILD` 钩子**编译完立即自动运行**（见 4.2）；Python 测试靠自定义 target 触发脚本运行（见 4.3）。

`build.sh` 还做了互斥校验，避免误用：`-t` 不能和 `--pkg`、`--msot`、`--build-type` 组合；`--cpp_utest`/`--python_utest` 不能和 `-t` 组合。

#### 4.1.3 源码精读

先看 `build.sh` 怎么解析测试选项。它维护了一份受支持的长选项清单，`--cpp_utest`、`--python_utest`、`--cov`、`--asan` 都在其中：

`build.sh` 的测试选项声明与帮助文本，列出 `-t/--test`（全量）、`--cpp_utest`（仅 C++）、`--python_utest`（仅 Python）、`--cov`（覆盖率）、`--asan`（地址消毒）：
[build.sh:L56-L71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L56-L71)

`set_options` 函数里，每个选项映射到一个 shell 变量。注意 `-t` 走 `TEST="all"`，而 `--cpp_utest`/`--python_utest` 走 `TEST_PART`（部分测试），二者变量不同：
[build.sh:L366-L388](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L366-L388)

互斥校验：`-t` 不能和 `--pkg`、`--msot` 一起用，部分测试选项也不能和全量一起用：
[build.sh:L320-L344](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L320-L344)

`build_test_part` 函数负责把「部分测试」翻译成 `TEST_MOD`，是理解 `--cpp_utest`/`--python_utest` 的关键：
[build.sh:L461-L469](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L461-L469)

`main` 函数里把这些 shell 变量真正拼成 CMake 的 `-D` 参数；可以看到 `-t` 会注入 `ENABLE_TEST=ON` 与 `TEST_MOD=all`，而部分测试只注入 `ENABLE_TEST=ON`（`TEST_MOD` 已由 `build_test_part` 处理），同时 `--cov`/`--asan` 分别注入 `ENABLE_GCOV`/`ENABLE_ASAN`：
[build.sh:L854-L870](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L854-L870)

最后，`main` 根据是全量测试、部分测试还是打包，分派到不同的构建目标：
[build.sh:L887-L893](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L887-L893)

CMake 侧的「总开关」在根 `CMakeLists.txt`：只有 `ENABLE_TEST` 为真，`tests/` 才会被加入构建——这是测试代码不会污染正式产物的护栏：
[CMakeLists.txt:L68-L70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L68-L70)

进入 `tests/CMakeLists.txt` 后，`TEST_MOD` 正式完成「加哪个子目录」的分发——这是本讲的命名来源：

`tests/CMakeLists.txt` 的全部分发逻辑只有两个 `if`：`TEST_MOD` 为 `all` 或 `cpp` 时加 `ut`（C++ 测试），为 `all` 或 `python` 时加 `py_ut`（Python 测试）：
[tests/CMakeLists.txt:L25-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/CMakeLists.txt#L25-L31)

> ⚠️ 注意一个细节：`TEST_MOD` 是字符串比较（`STREQUAL`）。若你手动 `cmake -DTEST_MOD=Cpp`（大小写或拼写错误），两个 `if` 都不成立，`ut` 和 `py_ut` 都不会被加入——构建会「成功」但一个测试都不跑。这是排查「为什么我的测试没执行」时的第一个检查点。

#### 4.1.4 代码实践

**实践目标**：在不真正编译的前提下，验证你对 `TEST_MOD` 分发的理解。

**操作步骤**：

1. 打开 `tests/CMakeLists.txt`，对照 `build.sh` 的 `build_test_part`（L461-L469）和 `main`（L854-L862）。
2. 用下表手动推演每种命令下 `ENABLE_TEST` 和 `TEST_MOD` 的取值，以及 `tests/` 会加入哪个子目录：

   | 命令 | `ENABLE_TEST` | `TEST_MOD` | 加入 `ut`？ | 加入 `py_ut`？ |
   | --- | --- | --- | --- | --- |
   | `bash build.sh -t` | ON | all | 是 | 是 |
   | `bash build.sh --cpp_utest` | ON | cpp | 是 | 否 |
   | `bash build.sh --python_utest` | ON | python | 否 | 是 |
   | `bash build.sh --pkg` | 未设 | 未设 | 否 | 否 |

**需要观察的现象**：`--cpp_utest` 与 `--python_utest` 互斥语义体现在「二者各自设 `TEST_MOD` 为不同值」，而不是靠显式的冲突报错——如果你同时传 `--cpp_utest --python_utest`，`set_options` 会先设 `TEST_PART=cpp_utest` 再覆盖成 `python_utest`（`check_param_test_part` 只拦截「部分」与「全量 `-t`」的组合，不拦截两个「部分」之间的组合）。

**预期结果**：你应当能解释「为什么 `--cpp_utest` 跑不到 Python 测试」——因为 `TEST_MOD=cpp` 时 `py_ut` 那个 `if` 不成立。实际编译运行的产物路径（`build_out/`）**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果只想给 Python 工具加一个新用例并快速验证，应该用 `build.sh` 的哪条命令？为什么不用 `-t`？

> **参考答案**：用 `bash build.sh --python_utest`。因为它把 `TEST_MOD` 限定为 `python`，只构建并运行 `tests/py_ut`，跳过耗时的 C++ 多产品编译；`-t` 会额外编译 5 个产品的 C++ UT，慢得多。

**练习 2**：用户报告「我跑了 `bash build.sh -t`，但日志里没有任何 `pytest` 输出」，请列出两条最可能的原因。

> **参考答案**：(1) CANN 环境变量未配置（`set_env` 找不到 CANN 包会直接 `exit 1`，根本到不了构建阶段）；(2) `tests/py_ut` 下的某个 `run_python_llt_test` target 因为依赖的源码目录缺失而没触发 `python_llt_run_and_check.sh`。优先检查 `ASCEND_HOME_PATH` 是否已 `source set_env.sh`。

---

### 4.2 C++ 单元测试体系（tests/ut）

#### 4.2.1 概念说明

C++ UT 验证的是 cpudebug 的 C++ 实现。它有两个特点直接来自 u9-l1 讲过的「多架构产物」：

1. **每个 NPU 产品各编译一份测试**。因为 cpudebug 的行为由架构宏（`__CCE_AICORE__`、`__DAV_…`）条件编译决定，同一份校验器源码在不同产品上「长出不同形态」，所以必须为每个产品分别编译、分别跑。
2. **测试可执行文件编译完立即自动运行**。靠 gtest 的 `main` + CMake 的 `POST_BUILD` 钩子实现，无需你手动 `./tikcpp_utest_ascend910`。

它用两个框架：

- **Google Test（gtest）**：提供 `TEST_F`、`EXPECT_EQ`、参数化测试 `TEST_P` 等断言宏，是测试主体。
- **mockcpp**：C++ 的 mock 框架（类似 Java 的 Mockito），用于在测试里替换掉难以构造真实环境的依赖。它通过 `tests/third_party/mockcpp.cmake` 在首次构建时从 OBS 下载源码并编译成 `libmockcpp.a`。

#### 4.2.2 核心流程

`tests/ut/CMakeLists.txt` 用两组 `foreach` 循环生成两族可执行文件：

```
TIKCPP_PRODUCT_TYPE_LIST = ascend910, ascend310p, ascend910B1_AIC, ascend910B1_AIV, ascend310B1
                            │
                            ▼  foreach product_type:
              add_executable(tikcpp_utest_${product_type})
                  ├─ 源码: api_check/*.cpp + tikcpp_api_check 用例 + case_common 用例（按产品条件加入）
                  ├─ 宏:   每产品一套 __CCE_AICORE__/__NPU_ARCH__/__DAV_…（条件编译出不同形态）
                  ├─ 依赖: cpudebug_<deps_product> + mockcpp_static
                  └─ run_llt_test → POST_BUILD 自动运行

TIKICPULIB_PRODUCT_TYPE_LIST = ascend910, ascend310p, ascend910B1_AIC, ascend910B1_AIV
                            │
                            ▼  foreach product_type:
              add_executable(tikicpulib_utest_${product_type})
                  ├─ 源码: regfwk/*.cpp + acl_stub/*.cpp + regfwk 用例(test_stub_base/reg/print) + acl_stub 用例
                  ├─ 宏:   同上
                  └─ run_llt_test → POST_BUILD 自动运行
```

两族可执行文件的职责分工：

| 可执行文件族 | 覆盖的 cpudebug 子目录 | 典型用例文件 |
| --- | --- | --- |
| `tikcpp_utest_<product>` | `src/api_check`（API 校验器）+ `testcase/tikcpp_case_common`（ELF 解析、kernel utils） | `test_vec_binary_check.cpp`、`test_data_copy_check.cpp`、`test_kernel_elf_parser.cpp` |
| `tikicpulib_utest_<product>` | `src/regfwk`（stub 注册）+ `src/acl_stub`（ACL 桩） | `test_stub_base.cpp`、`test_stub_reg.cpp`、`test_acl_stub.cpp` |

每产品注入的架构宏（与 u9-l1 的正式产物一一对应）：

| 产品 | `__CCE_AICORE__` | `__NPU_ARCH__` | `__DAV_` 标记 |
| --- | --- | --- | --- |
| ascend910 | 100 | 1001 | `__DAV_C100__` |
| ascend310p | 200 | 2002 | `__DAV_M200__` |
| ascend910B1_AIC | 220 | 2201 | `__DAV_C220__` + `__DAV_C220_CUBE__` + `__DAV_CUBE__` |
| ascend910B1_AIV | 220 | 2201 | `__DAV_C220__` + `__DAV_C220_VEC__` |
| ascend310B1 | 300 | 3002 | `__DAV_M300__` |

#### 4.2.3 源码精读

两个产品列表决定测试覆盖的架构范围：
[tests/ut/CMakeLists.txt:L13-L15](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L13-L15)

全局编译定义：所有 C++ UT 都在 `ASCENDC_CPU_DEBUG=1`（CPU 域调试模式）下编译，并用 C++17 标准：
[tests/ut/CMakeLists.txt:L17-L20](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L17-L20)

第一族可执行文件的核心：`foreach` 遍历每个产品，`add_executable` 用 `$<STREQUAL:...>` 生成器表达式按产品条件地加入不同用例源码（如 `tikcpp_case_common` 只在 ascend910/310p 加入）：
[tests/ut/CMakeLists.txt:L67-L91](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L67-L91)

每个产品对应一套架构宏。以 ascend910 与 ascend910B1（AIC 大核 / AIV 向量核）为例，可见 910B1 用同一套 `__CCE_AICORE__=220` 但靠 `__DAV_C220_CUBE__` / `__DAV_C220_VEC__` 区分核类型——这正是 u9-l1「同一架构宏、不同核形态」的复现：
[tests/ut/CMakeLists.txt:L94-L130](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L94-L130)

链接阶段，每个测试都链接 `cpudebug` + 三个 stub 库（`cpudebug_cceprint`/`cpudebug_npuchk`/`cpudebug_stubreg`），并强制 `--no-as-needed` 确保 stub 注册符号被保留：
[tests/ut/CMakeLists.txt:L176-L184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L176-L184)

`run_llt_test` 调用为该可执行文件注册「编译后自动运行」的钩子（其内部实现见 4.4.3）：
[tests/ut/CMakeLists.txt:L189-L192](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L189-L192)

第二族可执行文件同理，覆盖 regfwk 与 acl_stub：
[tests/ut/CMakeLists.txt:L224-L263](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/CMakeLists.txt#L224-L263)

gtest 的 `main` 入口极简，就是初始化并跑所有用例。每个可执行文件都链接这个 `main_global.cpp`：
[tests/ut/main_global.cpp:L12-L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/main_global.cpp#L12-L16)

用例样例 1（ACL 桩）：用 `TEST_F` 定义 fixture，断言 ACL 接口返回 `ACL_SUCCESS`、`aclDataTypeSize` 返回正确字节数：
[tests/ut/testcase/acl_stub/test_acl_stub.cpp:L11-L27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/acl_stub/test_acl_stub.cpp#L11-L27)

用例样例 2（向量二元校验器）：注意文件开头的 `#define private public` / `#define protected public`——这是 UT 常见手法，用于访问被测类的私有成员；`TearDown` 里调 `AscendC::CheckSyncState()` 在每条用例后检查 npu check 同步状态是否有残留违例：
[tests/ut/testcase/tikcpp_api_check/test_vec_binary_check.cpp:L16-L26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_vec_binary_check.cpp#L16-L26)

> 💡 **桩文件（stub）**：`tests/ut/common/` 下有 `dlog_stub.cpp`、`alog_stub.cpp`、`tik_pv_wrapper.cpp`、`k3_pvwrap.cpp` 等。它们是为测试编译的「假实现」，替代掉真实日志库、pvmodel 仿真器等无法在 UT 环境拉起的依赖。每个产品用 `$<STREQUAL:...>` 选择对应的桩（如 910B1 用 `k3_pvwrap.cpp`，910 用 `tik_pv_wrapper.cpp`）。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，理解「一个 C++ 校验器测试」的完整构造，并定位自己新增用例时应改哪些文件。

**操作步骤**：

1. 打开 `tests/ut/testcase/tikcpp_api_check/test_vec_binary_check.cpp`，找到 `TestBinaryApiCheckParams` 结构体与 `TestBinaryApiCheckSuite`（带 `WithParamInterface` 的参数化测试套件）。
2. 追踪它 `#include` 的 `api_check_test_utils.h`（同目录），理解 `AscToolsUt::MakeTensor` / `LogicPos` 如何构造测试用 Tensor——这些工具复用自 [u4-l1](u4-l1-base-check-framework.md) 讲过的校验基类。
3. 对照 `tests/ut/CMakeLists.txt` L67-L91，确认 `test_vec_binary_check.cpp` 属于 `${ASCENDC_API_CHECK_CASE_SRC_FILES}`（由 `file(GLOB ... testcase/tikcpp_api_check/*.cpp)` 收集），会被编进 `tikcpp_utest_ascend910`、`tikcpp_utest_ascend310p` 等所有产品的第一族可执行文件。

**需要观察的现象**：注意 `test_vec_binary_check.cpp` 在 `SetUp` 里设 `g_coreType = AIV_TYPE`、`TearDown` 里恢复成 `MIX_TYPE`——这说明同一个校验器在向量核（AIV）和混合核（MIX）语境下行为可能不同，测试用 fixture 生命周期来隔离这种全局状态。

**预期结果**：你能说清「若要新增一个 `Add` 校验器的测试，应该新建 `tests/ut/testcase/tikcpp_api_check/test_add_check.cpp`，无需改 CMakeLists（因为 `file(GLOB *.cpp)` 自动收集），它会自动进入所有产品的 `tikcpp_utest`」。实际编译运行 **待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tikicpulib_utest` 的产品列表（`TIKICPULIB_PRODUCT_TYPE_LIST`）比 `tikcpp_utest` 少一个 `ascend310B1`？

> **参考答案**：这是项目当前的覆盖选择，反映 `regfwk`/`acl_stub` 的 stub 注册逻辑在 310B1 上没有额外的、需要独立验证的形态（310B1 是 `__DAV_M300__` 轻量核，与 310p 的 `__DAV_M200__` 同属 M 系列，行为可由 ascend310p 覆盖）。它是一个工程取舍而非硬约束。

**练习 2**：`#define private public` 这种写法在 UT 里很常见，它有什么副作用？

> **参考答案**：它破坏了封装，让测试能直接读写类的私有成员；副作用是：(1) 测试与类的内部实现强耦合，重构会频繁改测试；(2) 可能改变内存布局（如 `sizeof`），影响某些依赖布局的代码；(3) 它属于「实现测试」而非「接口测试」，应在确需探测内部状态时才用。

---

### 4.3 Python 单元测试体系（tests/py_ut）

#### 4.3.1 概念说明

Python UT 验证的是 msobjdump、optype_collector、show_kernel_debug_data 三个纯 Python 工具。它与 C++ UT 的思路截然不同：

- **框架**：用例用 Python 标准库 `unittest.TestCase` 编写，但运行器是 `pytest`（更丰富的输出、并发、fixture）。`pytest` 能无缝发现并运行 `unittest` 风格的用例。
- **覆盖率**：Python UT **默认就开覆盖率**——每条用例都通过 `python3 -m coverage run -m pytest` 运行，无需额外加 `--cov`（`--cov` 的 C++ 部分 gcov 才需要显式开）。
- **路径注入**：被测源码不在 Python 的安装包里，每个 `test_xxx.py` 在文件头手动把源码目录插到 `sys.path` 最前面，再用 `importlib.import_module` 动态导入。
- **隔离外部命令**：这三个工具都重度依赖外部命令（`readelf`、`llvm-objcopy`、`addr2line` 等）和文件系统。UT 用 `unittest.mock.patch` 把这些外部调用替换成假返回值，使测试能在没有真实算子产物的环境下跑。

#### 4.3.2 核心流程

Python UT 的执行链比 C++ 长，因为要把「pytest + coverage」两个外部工具串起来：

```
build.sh --python_utest
   └─ CMake: TEST_MOD=python → add_subdirectory(py_ut)
        └─ tests/py_ut/CMakeLists.txt: 三个 run_python_llt_test 目标
             └─ run_python_llt_test()  (tests/cmake/func.cmake)
                  └─ 生成自定义 target，依赖 <TARGET>.timestamp
                       └─ POST_BUILD 命令调:
                            tests/cmake/tools/python_llt_run_and_check.sh
                               └─ python3 -m coverage run --source=<src> -m pytest <test_path>
                                    └─ 产出 .coverage.<TARGET>
```

三个测试目标及其覆盖范围：

| 目标名 | 被测源码目录 | 用例目录 |
| --- | --- | --- |
| `tikcpp_utest_msobjdump` | `utils/msobjdump/msobjdump/` | `testcase/msobjdump/` |
| `tikcpp_utest_optype_collector` | `utils/optype_collector/optype_collector/` | `testcase/optype_collector/` |
| `tikcpp_utest_show_kernel_debug_data` | `utils/show_kernel_debug_data/show_kernel_debug_data/` | `testcase/show_kernel_debug_data/` |

每个用例文件的统一开头套路（三件套）：算出源码目录 → 插到 `sys.path` 头部 → `importlib.import_module` 导入被测模块。

#### 4.3.3 源码精读

`tests/py_ut/CMakeLists.txt` 声明三个 `run_python_llt_test` 目标。注意每个目标都传了 `SRC_FILES_DIR`（被测源码，用于覆盖率统计）、`TEST_FILES_DIR`（用例目录）、`EXPORT_PYTHONPATH`（注入的 stub 路径）、`COVERAGERC_DIR`（覆盖率配置，这里都留空）：
[tests/py_ut/CMakeLists.txt:L15-L49](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/CMakeLists.txt#L15-L49)

`run_python_llt_test` 函数把宏参数拼成对 `python_llt_run_and_check.sh` 的调用，并用一个 `.timestamp` 文件作为 target 的 output（CMake 要求自定义命令有 output 文件才能纳入依赖图）：
[tests/cmake/func.cmake:L66-L148](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/func.cmake#L66-L148)

真正干活的脚本：用 `coverage run -m pytest` 跑用例，设置 1200 秒超时（`LLT_KILL_TIME`），并把每目标的覆盖率写到独立的 `.coverage.<module>` 文件（避免互相覆盖）：
[tests/cmake/tools/python_llt_run_and_check.sh:L85-L112](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/tools/python_llt_run_and_check.sh#L85-L112)

msobjdump 用例的「三件套」开头：算出 `utils/msobjdump` 路径、插 `sys.path`、`importlib` 导入 `msobjdump.msobjdump_main`：
[tests/py_ut/testcase/msobjdump/test_msobjdump.py:L25-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/msobjdump/test_msobjdump.py#L25-L31)

mock 隔离的典型例子：`test_dump_elf_aclnn` 用 4 个 `@patch` 装饰器把 `get_symbols_in_file`、`get_section_headers_in_file` 等真实调 `readelf` 的函数替换成返回固定文本，从而在没有真实 ELF 文件的前提下验证 `run_obj_dump` 的解析逻辑：
[tests/py_ut/testcase/msobjdump/test_msobjdump.py:L84-L90](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/msobjdump/test_msobjdump.py#L84-L90)

optype_collector 用例的 fixture 套路：`setUp` 用 `tempfile.mkdtemp` 建临时目录伪造一个 CANN 安装树，`tearDown` 用 `shutil.rmtree` 清理，保证用例之间互不污染：
[tests/py_ut/testcase/optype_collector/test_optype_collector.py:L38-L44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/optype_collector/test_optype_collector.py#L38-L44)

optype_collector 用例还演示了「真正起子进程」的端到端测试：`test_wrapper_reports_wrong_custom_soc_from_ascend_custom_opp_path` 用 `subprocess.run` 真的执行 `optype_collector.sh` 这个包装脚本，验证它的退出码与输出——这是少数不 mock、走真实进程的用例：
[tests/py_ut/testcase/optype_collector/test_optype_collector.py:L310-L343](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/optype_collector/test_optype_collector.py#L310-L343)

> 💡 show_kernel_debug_data 的用例因为要测 TLV 二进制解析（见 [u7-l2](u7-l2-dump-tlv-format.md)），开头会从 `dump_parser` 模块里一次性导入一大批类（`DumpTensor`、`PrintStruct`、`BlockInfo`…），把它们提升为模块级名字，方便用例直接构造二进制输入：
> [tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py:L24-L46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/show_kernel_debug_data/test_show_kernel_debug_data.py#L24-L46)

#### 4.3.4 代码实践

**实践目标**：体验「mock 隔离」如何让一个本来依赖 `readelf` 的测试变成纯内存测试。

**操作步骤**：

1. 打开 `tests/py_ut/testcase/msobjdump/test_msobjdump.py`，定位 `test_unpack_buff_content_by_type`（L43-L50）。这是最简单的用例：直接用 `struct.pack` 构造 4 字节输入，断言 `_unpack_buff_content_by_type` 解出整数 7——完全不依赖任何外部命令。
2. 再看 `test_file_action_only_parses_a_suffix_as_archive`（L56-L71）：它用 `tempfile.TemporaryDirectory` 建临时目录、`open(...).close()` 建空文件，用 `patch.object` 把 `get_o_file` 替换成透传 lambda，验证 `FileAction` 只把 `.a`（而非 `.axx`）当归档文件处理。

**需要观察的现象**：这两个用例的共同点是「输入全靠代码现场构造，外部依赖全靠 mock」。这说明 Python UT 的设计哲学是「让测试与真实算子产物解耦」——你不需要真的有一个编译好的 ELF 就能验证 msobjdump 的解析逻辑。

**预期结果**：你能总结出新增一个 msobjdump 用例的模板：(1) 在 `test_msobjdump.py` 里加 `def test_xxx(self):`；(2) 需要外部命令时用 `@patch(...)`；(3) 用 `self.assertEqual` / `self.assertIn` 断言。用例文件会被 `file(GLOB)` 自动收集，无需改 CMakeLists。直接 `python3 -m pytest tests/py_ut/testcase/msobjdump/test_msobjdump.py -v` 即可单跑（前提是 `utils/msobjdump` 已在 `sys.path`，文件头已处理）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Python UT「默认开覆盖率」，而 C++ UT 必须显式加 `--cov`？

> **参考答案**：Python 的 `coverage` 工具是通过「运行时 import 注入」工作的，几乎零额外编译成本，所以 `python_llt_run_and_check.sh` 默认就用 `coverage run` 启动用例；而 C++ 的 gcov 需要 `-fprofile-arcs -ftest-coverage` 重编译所有源码并改变链接（加 `-lgcov`），开销大且会影响 ASAN 等其他插桩，因此做成 `--cov` 显式开关。

**练习 2**：每个 `test_xxx.py` 都自己算源码路径并 `importlib.import_module`，而不是 `import msobjdump`。这种写法的好处是什么？

> **参考答案**：被测工具没有 `pip install` 到 site-packages，源码就在仓库的 `utils/` 下。手动把源码目录插到 `sys.path` 头部并按「包.模块」全限定名导入，能：(1) 不污染全局 Python 环境；(2) 保证 import 的是仓库当前版本的源码而非某处已安装的旧版；(3) 让 UT 与 CMake 的源码目录参数（`SRC_FILES_DIR`）解耦。

---

### 4.4 质量度量：覆盖率（--cov）与 ASAN（--asan）

#### 4.4.1 概念说明

`--cov` 和 `--asan` 是两个「编译插桩」开关，都叠加在测试命令上（如 `bash build.sh -t --cov --asan`）。它们改变的是「源码怎么被编译」，而不是「测什么」。

**覆盖率（Coverage）** 度量「测试执行了多少比例的可执行代码」，是评估测试充分度的指标。其数学定义为：

\[
\text{Coverage} = \frac{\text{ExecutedLines}}{\text{TotalInstrumentedLines}}
\]

asc-tools 对两种语言用不同工具：

| 语言 | 插桩机制 | 工具 | 触发 |
| --- | --- | --- | --- |
| C++ | gcc 的 gcov：编译时插计数器 | `lcov` 收集 + `genhtml` 生成 HTML | `--cov`（设 `ENABLE_GCOV`） |
| Python | `coverage` 运行时注入 | `coverage combine` + `coverage html` | **默认开启**，无需 `--cov` |

**ASAN（AddressSanitizer）** 是 gcc/clang 的内存错误检测器，编译时插桩，运行时拦截每次内存访问，能发现：堆/栈/全局变量越界、use-after-free、double-free、内存泄漏等。**ASAN 只对 C++ UT 生效**——Python UT 跑在解释器进程里，ASAN 对它无意义。

#### 4.4.2 核心流程

两个开关都在 `tests/cmake/intf.cmake` 的接口库 `intf_llt_pub` 上落地，再被所有 C++ 测试 target 继承：

```
ENABLE_GCOV=true  ──► intf_llt_pub 编译/链接加 -fprofile-arcs -ftest-coverage + -lgcov
                        └─ run_llt_test 检测到 ENABLE_GCOV → 创建 collect_coverage_data 目标
                              └─ 调 generate_cpp_cov.sh → lcov -c → lcov -r（过滤系统头）→ genhtml
                                    └─ 产出 build/cov_report/index.html

ENABLE_ASAN=true  ──► intf_llt_pub 编译/链接加 -fsanitize=address -static-libasan ...
                        └─ run_llt_test 检测到 ENABLE_ASAN → POST_BUILD 运行时:
                              LD_PRELOAD=libasan.so + ASAN_OPTIONS=detect_leaks=0:halt_on_error=0 ./tikcpp_utest_xxx
```

Python 覆盖率的合并则在 `tests/py_ut/CMakeLists.txt` 单独定义了一个 `python_llt_coverage` 目标：它依赖三个测试目标，等它们各自产出 `.coverage.tikcpp_utest_*` 后，用 `coverage combine` 合并，再 `coverage html` 出总报告。

#### 4.4.3 源码精读

`intf_llt_pub` 接口库是插桩的落点。`ENABLE_GCOV` 加 `-fprofile-arcs -ftest-coverage`（编译+链接）和 `-lgcov`（链接 gcov 运行时）；`ENABLE_ASAN` 加一组 `-fsanitize=address/undefined/leak` 及其静态库。所有 C++ 测试 target 链接 `intf_llt_pub` 即继承这些开关：
[tests/cmake/intf.cmake:L23-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/intf.cmake#L23-L43)

`run_llt_test` 函数在 `ENABLE_ASAN` 分支里：先按 GCC 大版本号定位 `libasan.so` 路径，设 `LD_PRELOAD` 让 ASAN 运行时优先加载；再设 `ASAN_OPTIONS=detect_leaks=0:halt_on_error=0`（注释明确警告「出现 ASAN 告警会使 UT 失败」，故关掉泄漏检测与立即 halt，让错误以报告形式呈现而非进程崩溃）：
[tests/cmake/func.cmake:L13-L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/func.cmake#L13-L32)

同一个 `run_llt_test` 函数在 `ENABLE_GCOV` 分支里：创建一个全局唯一的 `collect_coverage_data` target，调用 `generate_cpp_cov.sh`，参数是「构建目录、覆盖率文件、HTML 输出目录、CANN 路径」，并让该 target 依赖每个测试 target（保证先跑测试、产生 `.gcda` 计数文件，再收集）：
[tests/cmake/func.cmake:L47-L63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/func.cmake#L47-L63)

C++ 覆盖率脚本三步走：`generate_coverage`（`lcov -c` 采集 + `lcov -r` 过滤掉 CANN 包、build 目录、tests 目录自身）、`filter_coverage`（再过滤 `/usr/include`）、`generate_html`（`genhtml` 出网页）。脚本对 lcov ≥ 2.0 还会加 `--ignore-errors mismatch,empty,...` 以兼容新版工具：
[tests/cmake/tools/generate_cpp_cov.sh:L24-L41](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/cmake/tools/generate_cpp_cov.sh#L24-L41)

Python 覆盖率合并目标：`coverage combine` 三个 `.coverage.tikcpp_utest_*` → `coverage html`，输出到 `build_out/coverage_result/`：
[tests/py_ut/CMakeLists.txt:L51-L65](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/CMakeLists.txt#L51-L65)

#### 4.4.4 代码实践

**实践目标**：理解 `--cov` 与 `--asan` 的产物差异，并知道去哪里找报告。

**操作步骤**：

1. 对照源码填下表，明确两个开关各自的「作用语言、开关变量、产物路径」：

   | 开关 | 作用语言 | CMake 变量 | 产物 |
   | --- | --- | --- | --- |
   | `--cov` | C++（gcov）+ Python（默认） | `ENABLE_GCOV` | C++: `build/cov_report/index.html`；Python: `build_out/coverage_result/index.html` |
   | `--asan` | 仅 C++ | `ENABLE_ASAN` | 无独立文件，错误直接打印到测试 stdout |

2. 阅读 `generate_cpp_cov.sh` 的 `lcov -r` 行，记录它过滤掉了哪些路径（CANN 包、`/home/jenkins/opensource`、build/output 目录、`tests/*` 自身）。思考：为什么要过滤掉 `tests/*`？

**需要观察的现象**：`ASAN_OPTIONS=detect_leaks=0` 是有意关闭泄漏检测的。原因可从 `func.cmake` L21 的注释推断——cpudebug 的 fork 多核模型（见 [u3-l1](u3-l1-fork-execution-model.md)）会让子进程「故意不释放」某些共享资源，这会被 ASAN 误报为泄漏。

**预期结果**：你应当能解释「`--cov` 与 `--asan` 通常不同时用」——gcov 的计数器插桩与 ASAN 的内存检查插桩叠加会让产物臃肿、运行变慢，且两者关注的维度（覆盖度 vs 内存安全）一般分开评估。实际产物的具体百分比与行号 **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：覆盖率公式里分母是 `TotalInstrumentedLines`（插桩后的可执行行），而不是源文件的「物理行数」。这两者有何区别？为什么覆盖率工具用前者？

> **参考答案**：物理行数包含空行、注释、纯声明等不可执行行；插桩后的「可执行行」只统计真正会产生计数的语句（赋值、调用、分支等）。用前者会让覆盖率被大量注释稀释、显得偏低且不稳定；用后者才能真实反映「测试覆盖了多少会被运行的代码」。

**练习 2**：如果你给 cpudebug 加了一段新代码，跑 `bash build.sh -t` 全绿，但 `bash build.sh -t --cov` 显示这段新代码覆盖率 0%，这说明什么？该如何补救？

> **参考答案**：说明新代码「能编译、能链接、现有测试没触发它」——可能是没人为新逻辑写用例，或新代码是某架构宏条件分支、当前产品的测试没覆盖到该分支。补救：在 `tests/ut/testcase/` 对应目录补一条用例，或确认它所属的产品已被 `TIKCPP_PRODUCT_TYPE_LIST` 包含并确实运行。

**练习 3**：为什么 `ASAN_OPTIONS` 要设 `halt_on_error=0`？

> **参考答案**：`halt_on_error=0` 让 ASAN 在发现首个错误后不立刻终止进程，而是继续运行、尽量收集更多错误，便于一次跑出完整报告；若设为 1，遇到第一个越界就崩溃，可能掩盖后续错误，不利于一次性定位所有问题。

## 5. 综合实践

把本讲的四个模块串起来，完成一个「为 optype_collector 新增并验证一条用例」的小任务。

**背景**：[u8-l1](u8-l1-optype-collector.md) 讲过 optype_collector 的冲突检测会区分「自定义 vs 内置」「自定义 vs 自定义」两类冲突。现在请你为「自定义 vs 自定义冲突」补一条最小用例。

**操作步骤**：

1. **定位入口**：读 `tests/py_ut/testcase/optype_collector/test_optype_collector.py` 的 `test_detects_custom_builtin_and_custom_custom_conflicts`（L439-L464），理解它如何用 `_vendor_config`、`_custom_opp_config`、`_write_json` 伪造两个互相冲突的自定义包，再用 `_run_main(["--detect-conflicts", ...])` 触发，断言返回码为 1 且输出含冲突描述。

2. **照葫芦画瓢**：在该文件里新增一条 `test_detects_only_custom_custom_conflict`，只造两个自定义包（不造内置包），让它们共用一个 OpType 名（如 `"MyConflictOp"`），断言：
   - 返回码为 1；
   - 输出含 `"Custom package conflicts with another custom package"`；
   - 输出**不含** `"Custom package conflicts with built-in OpTypes"`。

3. **验证 TEST_MOD 分发**：不跑全量，只用 `bash build.sh --python_utest`（即 `TEST_MOD=python`）。对照 `tests/CMakeLists.txt` L25-L31 解释：为什么这条命令也会同时编译/链接 C++ 的 cpudebug？（提示：Python 测试 target 虽然 `TEST_MOD=python` 时不加 `ut`，但根 CMakeLists 仍会构建 `cpudebug` 主库——只是不构建 C++ 测试可执行文件。）

4. **单跑用例**：如果想跳过 CMake 直接验证这条用例，可在仓库根执行：
   ```bash
   cd utils/optype_collector && python3 -m pytest \
     ../../tests/py_ut/testcase/optype_collector/test_optype_collector.py::TestOpTypeCollector::test_detects_only_custom_custom_conflict -v
   ```
   （依赖文件头已注入的 `sys.path`。）

**预期结果**：新用例通过；你能用一句话说清「`-t` / `--cpp_utest` / `--python_utest` 三者编译产物的差异」以及「为什么我加的 `.py` 用例不用改任何 CMake 文件」。**完整构建与运行待本地验证**（需 CANN 环境）。

## 6. 本讲小结

- `build.sh` 是测试的唯一入口：`-t` 触发全量（`TEST_MOD=all`），`--cpp_utest`/`--python_utest` 触发部分（`TEST_MOD=cpp`/`python`），`--cov`/`--asan` 是可叠加的插桩开关。
- `TEST_MOD` 是 C++/Python 测试的总分发器，在 `tests/CMakeLists.txt` 用两个 `if` 决定加入 `ut` 还是 `py_ut`；根 `CMakeLists.txt` 则用 `ENABLE_TEST` 决定是否挂入整个 `tests/`。
- C++ UT（`tests/ut`）用 gtest + mockcpp，按 NPU 产品（910/310p/910B1 AIC/AIV/310B1）× 两族可执行文件（`tikcpp_utest`/`tikicpulib_utest`）组织，每产品注入对应架构宏，编译完由 `run_llt_test` 的 `POST_BUILD` 钩子自动运行。
- Python UT（`tests/py_ut`）用 `unittest` 写、`pytest` 跑、`coverage` 默认计覆盖率，靠 `sys.path` 注入 + `importlib` 导入源码、`unittest.mock.patch` 隔离 `readelf`/`llvm-objcopy` 等外部命令。
- `--cov` 对 C++ 走 gcov + lcov + genhtml（产出 `cov_report/`），对 Python 默认走 `coverage`（`python_llt_coverage` 目标合并三份 `.coverage.*` 出 `coverage_result/`）。
- `--asan` 只对 C++ UT 有效，靠 `intf_llt_pub` 注入 `-fsanitize=address`，`run_llt_test` 在运行时配 `LD_PRELOAD` + `ASAN_OPTIONS=detect_leaks=0:halt_on_error=0`。

## 7. 下一步学习建议

本讲是 u9（构建/打包/测试）单元的收尾。建议接下来：

1. **进入 u10 扩展实践**：[u10-l1 扩展 API 校验器与二次开发](u10-l1-extend-api-check.md) 会教你新增一个 C++ 校验器——届时你会真正用到本讲的「新增 `tests/ut/testcase/tikcpp_api_check/*.cpp` 用例」流程来验证你的扩展。
2. **若关注测试质量**：在你本地对某个工具（如 msobjdump）跑一次 `bash build.sh --python_utest`，打开 `build_out/coverage_result/index.html`，找出覆盖率最低的源码文件，尝试补一条用例提升它——这是检验你是否真正读懂 4.3 的最好方式。
3. **若关注 CI 集成**：阅读 `scripts/run_presmoke.sh`（[u10-l2](u10-l2-contributing-workflow.md) 会讲到），看 presmoke 流水线是如何调用本讲的 `build.sh -t` 来做门禁的。
