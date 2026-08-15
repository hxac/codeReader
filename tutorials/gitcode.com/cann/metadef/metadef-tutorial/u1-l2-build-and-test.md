# 源码构建与测试运行

## 1. 本讲目标

学完本讲后，你应该能够：

1. 使用 `bash build.sh` 完成 metadef 的本地一键编译，并理解脚本内部做了什么。
2. 使用 `bash tests/run_test.sh -u` 构建并运行单元测试，看懂 ctest 的输出。
3. 说出顶层 `CMakeLists.txt` 中各子目录（`inc`、`base`、`tests`）的组织方式，以及单元测试可执行文件（如 `ut_metadef`）是如何被 CMake 定义出来的。

本讲是后续所有讲义的前置条件：无论后面读哪一块源码，能自己编译、能跑测试，才有验证猜想的手段。

## 2. 前置知识

在开始之前，先通俗地理解几个概念：

- **CMake**：C++ 项目常用的构建系统生成器。它本身不编译代码，而是根据 `CMakeLists.txt` 生成 `Makefile`，再由 `make` 执行真正的编译。你可以把它理解为「构建脚本的配置文件」。
- **ASCEND_HOME_PATH 环境变量**：指向本机 CANN 软件包安装目录。metadef 编译时要链接 CANN 提供的 securec、runtime、platform 等库，所以必须先 `source /usr/local/Ascend/cann/set_env.sh`（详见 [docs/zh/build.md:L16-L25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md#L16-L25)，这一段讲解了如何配置环境变量）。
- **gtest / ctest**：gtest 是 Google 的 C++ 单元测试框架，metadef 的测试用例都用它写成；ctest 是 CMake 自带的测试驱动器，负责批量运行测试程序并汇总结果。
- **UT**：Unit Test（单元测试）的缩写。metadef 文档和脚本里大量出现 `ut` 这个词，例如 `ENABLE_METADEF_UT` 开关、`ut_metadef` 目标。
- **ASan/LSan**：AddressSanitizer/LeakSanitizer，编译期插桩的内存错误检测工具。metadef 的单测默认开启，能发现越界、泄漏等问题。
- **`.run` 软件包**：CANN 生态自解压安装包格式，编译产物最终会被打成 `cann-metadef_<version>_linux.<arch>.run`。

环境依赖要求（GCC >= 7.3、Python3 >= 3.9、CMake >= 3.16 等）在 [docs/zh/build.md:L40-L62](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md#L40-L62) 有完整清单，可以用 `bash scripts/check_env.sh` 一键检查环境。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh) | 一键编译入口：解析参数 -> 校验 CANN 环境 -> cmake + make -> 打包 `.run` 安装包 |
| [tests/run_test.sh](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh) | 一键单测入口：以 UT 模式重新 cmake，编译 4 个 ut 目标并用 ctest 运行 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt) | 顶层 CMake 配置：环境检查、编译选项、`inc`/`base`/`tests` 子目录接入、安装与打包规则 |
| [tests/CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/CMakeLists.txt) | 测试子工程入口：给 UT 加 ASan 编译参数、构造各依赖库的 stub |
| [tests/ut/CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/CMakeLists.txt) | 按 base / exe_meta_device / sc_check / register 四个目录组织单测 |
| [tests/ut/base/CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt) | 定义 `ut_metadef` 可执行目标并注册到 ctest |
| [docs/zh/build.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md) | 官方构建文档：环境准备、依赖安装、编译与安装步骤 |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：`build.sh`、`tests/run_test.sh`、`CMakeLists.txt`。它们的关系可以用一句话概括：

```
build.sh / run_test.sh  ──传入开关──▶  cmake（读 CMakeLists.txt）──生成──▶  Makefile ──▶  make 编译 / ctest 跑测试
```

### 4.1 build.sh：一键编译入口

#### 4.1.1 概念说明

`build.sh` 是仓根目录的编译总入口。它解决的问题是：metadef 的 cmake 配置需要十几个 `-D` 参数（编译类型、安装路径、三方库路径、UT 开关等），普通开发者不可能每次手敲，于是用 shell 脚本封装成「一条命令编译 + 打包」。

#### 4.1.2 核心流程

`build.sh` 的执行流程（对应 `main` 函数）：

1. `checkopts` 解析命令行参数，设置默认值。
2. 检查 `ASCEND_HOME_PATH` 环境变量，没有则报错退出。
3. 创建 `output/` 和 `build_out/` 目录。
4. `build_metadef`：进入 `build/` 目录，依次执行 `cmake ..` -> `make all` -> `make install` -> `make package`。
5. `copy_pkg` 把生成的 `.run` 包移动到 `build_out/`。

伪代码：

```
main():
    checkopts(argv)                 # 解析 -j/-v/--build-type 等参数
    assert ASCEND_HOME_PATH 存在     # 否则 exit 1
    build_metadef():
        cmake -D GE_ONLY=on -D ENABLE_OPEN_SRC=True ... ..
        make all -j${THREAD_NUM}
        make install
        make package                # 产出 cann-metadef_*.run
```

#### 4.1.3 源码精读

**参数解析与默认值**。[build.sh:L100-L109](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L100-L109) 中 `checkopts` 先设置所有默认值：线程数取本机 CPU 数（`grep -c ^processor /proc/cpuinfo`）、`ENABLE_METADEF_UT=off`（默认不编单测）、`CMAKE_BUILD_TYPE=Release`。随后用 `getopt` 逐个解析 `-j`、`-v`、`--asan`、`--build-type` 等选项。

支持的完整选项列表见 [build.sh:L27-L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L27-L51) 的 `usage` 函数，常用的有：

| 选项 | 含义 |
| --- | --- |
| `-j<N>` | 编译线程数，默认 8 |
| `-v` | 显示详细编译命令 |
| `--build-type=Debug` | 切换 Debug 构建（默认 Release） |
| `--asan` | 开启 AddressSanitizer |
| `--output_path=<PATH>` | 安装输出目录，默认 `./output` |

**环境变量强校验**。[build.sh:L192-L197](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L192-L197) 检查 `ASCEND_HOME_PATH`，不存在就直接报错退出。这是新手最常踩的坑——没有 source CANN 环境脚本时编译必然失败。

**cmake 参数拼装与编译**。[build.sh:L235-L269](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L235-L269) 是 `build_metadef` 函数：先把十几个 `-D` 开关拼成 `CMAKE_ARGS`（如 `ENABLE_OPEN_SRC=True`、`BUILD_WITHOUT_AIR=True`、`ASCEND_INSTALL_PATH`、`CANN_3RD_LIB_PATH` 等），然后在 `build/` 目录里依次执行：

```bash
cmake ${CMAKE_ARGS} ..
make all -j${THREAD_NUM}
make install
make package
```

这四步正是 CMake 项目的标准生命周期：配置 -> 编译 -> 安装 -> 打包。注意 `ENABLE_METADEF_UT` 默认是 `off`，所以普通 `build.sh` 编译不包含单元测试。

**产物位置**。编译成功后 `.run` 包被 [build.sh:L215-L232](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L215-L232) 的 `copy_pkg` 移到 `build_out/` 目录，命名形如 `cann-metadef_<version>_linux.<arch>.run`（见 [docs/zh/build.md:L89-L98](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md#L89-L98)）。

#### 4.1.4 代码实践

1. **实践目标**：确认本地环境满足编译条件，并读懂 build.sh 的帮助信息。
2. **操作步骤**：
   1. `source /usr/local/Ascend/cann/set_env.sh`（路径按实际安装位置调整，非 root 用户通常是 `$HOME/Ascend`）。
   2. `echo $ASCEND_HOME_PATH`，确认输出非空。
   3. `bash scripts/check_env.sh`，查看所有检查项的状态。
   4. `bash build.sh -h`，对照 [build.sh:L27-L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L27-L51) 确认每个选项都有出处。
3. **需要观察的现象**：check_env.sh 输出中 `[PASS]`/`[WARNING]`/`[ERROR]` 三类状态；`-h` 打印的选项列表与本讲表格一致。
4. **预期结果**：所有关键项为 `[PASS]`，`$ASCEND_HOME_PATH` 非空。若有 `[ERROR]`，按 [docs/zh/build.md:L40-L62](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/build.md#L40-L62) 安装对应依赖。
5. 如果当前没有 Ascend 环境，本步骤**待本地验证**，可以先记住「先 source set_env.sh 再 build」这个顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果不 source CANN 环境变量直接执行 `bash build.sh`，会在哪一行报错？为什么？

答案：在 [build.sh:L192-L197](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L192-L197) 处报 `No environment variable 'ASCEND_HOME_PATH' was found` 并 `exit 1`。因为 metadef 编译需要链接 CANN 包内的 securec、runtime、platform 等库，`ASCEND_HOME_PATH` 是定位这些库的唯一线索。

**练习 2**：想调试 metadef，应该加什么编译参数？

答案：`bash build.sh --build-type=Debug`。该值经 [build.sh:L142-L147](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L142-L147) 校验后写入 `CMAKE_BUILD_TYPE`，再通过 [build.sh:L240-L258](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L240-L258) 传给 cmake。

**练习 3**：为什么 `bash build.sh` 编译完成后找不到任何单测可执行文件？

答案：因为 [build.sh:L103](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L103) 默认 `ENABLE_METADEF_UT="off"`，而顶层 CMake 只在 `ENABLE_METADEF_UT` 打开时才会 `add_subdirectory(tests)`（见 4.3.3 节）。跑单测要用 `tests/run_test.sh`。

### 4.2 tests/run_test.sh：单元测试的构建与运行

#### 4.2.1 概念说明

`tests/run_test.sh` 是单元测试的一键入口。它和 `build.sh` 是平行的两套 cmake 调用，区别在于：`run_test.sh` 会把 `ENABLE_METADEF_UT` 置为 `on`、把 `GE_ONLY` 置为 `off`，从而让顶层 CMake 把 `tests/` 子目录纳入编译，并额外构建 4 个单测目标；编译完再用 `ctest` 批量执行它们。

#### 4.2.2 核心流程

```
run_test.sh -u:
    checkopts: ENABLE_METADEF_UT=on, GE_ONLY=off
    若 UT 或 COV 开启: 构建目录改为 build_gcov/, CMAKE_BUILD_TYPE=GCOV
    cmake -D ENABLE_METADEF_UT=on ... ..
    make ut_metadef ut_register ut_exe_meta_device ut_sc_check -j8
    ctest -L ut --test-dir build_gcov --output-log build_gcov/ctest_ut.log
    若开启 -c: lcov + genhtml 生成覆盖率报告 cov/html
```

#### 4.2.3 源码精读

**`-u` 选项的语义**。[tests/run_test.sh:L59-L63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L59-L63) 中 `-u | --ut` 分支把 `ENABLE_METADEF_UT` 置 `on`，同时把 `GE_ONLY` 置 `off`——单测需要链接更完整的代码，不能只编 GE 子集。注意 [tests/run_test.sh:L165-L167](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L165-L167) 还有一个兜底：即使不传 `-u`，`main` 里也会强制把 `ENABLE_METADEF_UT` 设为 `on`，即这个脚本「天然就是跑单测的」。

**独立的构建目录**。[tests/run_test.sh:L130-L133](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L130-L133) 在 UT/COV 模式下把构建目录切到 `build_gcov/`、构建类型设为 `GCOV`，与 `build.sh` 用的 `build/` 目录完全隔离——这样普通编译产物不会被带 ASan/覆盖率插桩的测试产物污染。

**编译 4 个单测目标**。[tests/run_test.sh:L150-L152](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L150-L152) 直接 `make ut_metadef ut_register ut_exe_meta_device ut_sc_check`。这 4 个目标分别对应 `tests/ut/` 下 4 个子目录（见 4.3.3 节），其中 `ut_metadef` 覆盖 `tests/ut/base/` 的全部用例，是最大的一批。

**ctest 驱动执行**。[tests/run_test.sh:L175-L184](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L175-L184) 设置 `ASAN_OPTIONS=detect_container_overflow=0` 后执行：

```bash
ctest --verbose -j ${THREAD_NUM} -L ut --test-dir ${BUILD_PATH} --no-tests=error \
      --output-log ${BUILD_PATH}/ctest_ut.log
```

关键点：`-L ut` 只运行打了 `ut` 标签的测试（标签在 CMake 里注册，见 4.3.3 节）；`--no-tests=error` 表示一个测试都没找到也算失败，防止「空跑当成功」；完整日志落在 `build_gcov/ctest_ut.log`。失败时脚本会打印 `!!! UT FAILED, PLEASE CHECK YOUR CHANGES !!!` 并以非零码退出。

**覆盖率（可选）**。[tests/run_test.sh:L186-L201](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L186-L201) 在 `-c` 模式下用 lcov 收集 metadef/opp_registry/exe_graph 等目标的覆盖率，生成 `cov/html` 报告，前提是本机装有 lcov、gcov、genhtml。

#### 4.2.4 代码实践

1. **实践目标**：完整跑通一次单元测试，并读懂一段真实的 gtest 输出。
2. **操作步骤**：
   1. 确保 `ASCEND_HOME_PATH` 已设置、依赖已安装（`bash scripts/check_env.sh` 通过）。
   2. 执行 `bash tests/run_test.sh -u`（首次会下载并编译 gtest/protobuf 三方库，耗时较长）。
   3. 结束后打开 `build_gcov/ctest_ut.log`，找到任意一个测试用例的输出段落。
3. **需要观察的现象**：终端末尾的 ctest 汇总表（`Passed`/`Failed` 列），以及 log 中 gtest 打印的 `[ RUN ]` / `[ OK ]` / `[ PASSED ]` 字样。
4. **预期结果**：形如 `100% tests passed, 0 tests failed out of 4` 的汇总；log 中每个用例以 `[ RUN ]` 开始、`[ OK ]` 结束。任选一个成功或失败的用例，记录它的名字（格式通常是 `TestSuiteName.TestCaseName`），并用一句话解释它验证了什么——用例名通常能直接对应到被测函数，例如 `TensorImpl.TestSetShape` 对应 Tensor 的 SetShape 接口。
5. 若当前环境无法编译（无 Ascend 包），**待本地验证**；可以先读 4.3.3 节的 `add_test` 定义，理解测试是怎么注册的。

#### 4.2.5 小练习与答案

**练习 1**：`run_test.sh -u` 和 `build.sh` 用的构建目录分别是哪个？为什么分开？

答案：`build/` 与 `build_gcov/`（见 [tests/run_test.sh:L130-L135](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L130-L135)）。因为单测编译带 `-fsanitize=address -fsanitize=leak` 和覆盖率插桩（见 [tests/CMakeLists.txt:L11-L14](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/CMakeLists.txt#L11-L14)），产物与发布版不兼容，必须隔离。

**练习 2**：如果 ctest 一个测试都没执行但脚本退出了，脚本能发现吗？

答案：能。`ctest` 加了 `--no-tests=error`（[tests/run_test.sh:L177-L178](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L177-L178)），零测试会被视为错误，随后脚本打印失败提示并 `exit 1`。

**练习 3**：想看代码覆盖率报告，应该用什么命令？产物在哪？

答案：`bash tests/run_test.sh -c`（前提装好 lcov/genhtml）。执行后 lcov 针对 metadef、opp_registry、exe_graph 等目标收集数据（[tests/run_test.sh:L192-L200](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L192-L200)），HTML 报告生成在仓库根目录 `cov/html/`。

### 4.3 CMakeLists.txt：工程的整体组织

#### 4.3.1 概念说明

顶层 `CMakeLists.txt` 是整个工程的「目录树说明书」：它声明编译选项、检查环境、决定哪些子目录参与编译、定义安装与打包规则。读 C++ 项目先读顶层 CMake，是快速建立全局认知的最短路径。

#### 4.3.2 核心流程

顶层 CMake 的逻辑顺序：

```
1. cmake_minimum_required(3.16) + init_cann_project
2. 环境检查: ASCEND_INSTALL_PATH / CANN_3RD_LIB_PATH 缺失则 FATAL_ERROR
3. 打印所有关键变量（方便排障）
4. 编译选项: GCC/Clang 下 -Wall -Wextra -Werror（告警即报错）
5. ENABLE_OPEN_SRC 时查找依赖: securec / unified_dlog / mmpa / runtime / platform / error_manager
   （UT 模式额外拉 protobuf + gtest 三方库）
6. add_subdirectory(inc) -> add_subdirectory(base)
   仅当 ENABLE_METADEF_UT/ST 时 add_subdirectory(tests)
7. 安装 libexe_graph 等 4 个目标 + inc/ 与 pkg_inc/ 下的头文件
8. 打包规则（cmake/package.cmake）
```

#### 4.3.3 源码精读

**环境硬检查**。[CMakeLists.txt:L27-L33](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L27-L33) 在开发者环境下若 `ASCEND_INSTALL_PATH` 或 `CANN_3RD_LIB_PATH` 未定义，直接 `FATAL_ERROR` 并给出文档链接。这对应了 build.sh 里对 `ASCEND_HOME_PATH` 的检查——两层防线保证配置阶段就失败，而不是编译到一半才报找不到头文件。

**告警即报错**。[CMakeLists.txt:L69-L71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L69-L71) 对 GCC/Clang 开启 `-Wall -Wextra -Werror`。这意味着你给 metadef 提交的代码有任何编译告警都会直接编译失败——这是基础库对代码质量的强制约束。随后 [CMakeLists.txt:L73-L85](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L73-L85) 仅为 Clang 放宽了部分告警。

**依赖查找**。[CMakeLists.txt:L87-L106](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L87-L106) 在 `ENABLE_OPEN_SRC` 时用 `find_cann_package` 查找 securec、unified_dlog、runtime、platform、error_manager 等库；UT 模式下还会 [拉取 protobuf 和 gtest 三方库](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L91-L95)——这解释了为什么首次跑单测特别慢（要下载编译三方库），而 build.sh 的 `--cann_3rd_lib_path` 说明里也提到后续构建会跳过已编译的三方库。

**子目录组织——本讲最核心的一段**。[CMakeLists.txt:L109-L114](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L109-L114)：

```cmake
add_subdirectory(inc)
add_subdirectory(base)

if (ENABLE_METADEF_UT OR ENABLE_METADEF_ST)
    add_subdirectory(tests)
endif()
```

这印证了上一讲（u1-l1）讲的目录分工：`inc/` 是对外接口、`base/` 是实现源码，两者始终一起编译；而 `tests/` 只在测试开关打开时才进入构建树。四个编译产物目标（`exe_graph`、`opp_registry`、`rt2_registry_static`、`metadef`）在 [CMakeLists.txt:L116-L121](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L116-L121) 统一安装，`inc/` 与 `pkg_inc/` 下的头文件则在 [CMakeLists.txt:L123-L129](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L123-L129) 被拷贝到安装目录的 `metadef/` 下。

**tests 子工程：stub 替身**。[tests/CMakeLists.txt:L11-L25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/CMakeLists.txt#L11-L25) 做了两件事：一是给测试代码加 ASan/LSan 编译参数；二是用 `stub_module` 把 slog、mmpa、platform、runtime 等宿主环境依赖替换成桩实现——单测跑在 x86 主机上，没有真实昇腾设备，这些底层接口必须用假实现顶替。这就是单元五会详细讲的 stub 机制的入口。

**ut 目标的定义**。[tests/ut/CMakeLists.txt:L11-L14](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/CMakeLists.txt#L11-L14) 把单测分为 4 个子目录：`base`、`exe_meta_device`、`sc_check`、`register`，分别产出 `ut_metadef`、`ut_exe_meta_device`、`ut_sc_check`、`ut_register` 四个可执行文件——正是 run_test.sh 里 `make` 的那 4 个目标。以 `ut_metadef` 为例，[tests/ut/base/CMakeLists.txt:L21-L28](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L21-L28) 用 `file(GLOB_RECURSE)` 把 `tests/ut/base/testcase/*.cc` 全部编进一个可执行文件；最后 [tests/ut/base/CMakeLists.txt:L70-L71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L70-L71) 用 `add_test` 把它注册进 ctest 并打上 `ut;ut_metadef` 标签，gtest 结果还会输出成 XML 报告。这也意味着：**往 `tests/ut/base/testcase/` 里加一个 `.cc` 文件，无需改 CMake 就会自动进入 `ut_metadef`**——单元五的测试实践正是利用这一点。

#### 4.3.4 代码实践

1. **实践目标**：不实际编译，纯靠读 CMake 画出「一个新增测试文件如何变成 ctest 里的一个用例」的链路。
2. **操作步骤**：
   1. 从 [tests/ut/base/CMakeLists.txt:L21-L23](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L21-L23) 出发，确认 `UT_FILES` 的 glob 规则。
   2. 顺藤摸瓜：`add_executable(ut_metadef ...)` -> `add_test(NAME ut_metadef ...)` -> ctest `-L ut`。
   3. 再往上追一层：`tests/ut/CMakeLists.txt` -> `tests/CMakeLists.txt` -> 顶层 `if (ENABLE_METADEF_UT ...) add_subdirectory(tests)` -> run_test.sh 的 `-D ENABLE_METADEF_UT=on`。
3. **需要观察的现象**：每层的「开关」是谁打开的（answer：run_test.sh）、glob 是否需要改 CMake（answer：不需要，`CONFIGURE_DEPENDS` 会自动跟踪新文件）。
4. **预期结果**：能画出一条完整链路图：

```
run_test.sh -u  ──(-D ENABLE_METADEF_UT=on)──▶  顶层 CMakeLists.txt L112-114
  ──add_subdirectory(tests)──▶  tests/CMakeLists.txt（ASan + stub）
  ──▶  tests/ut/CMakeLists.txt（4 个子目录）
  ──▶  tests/ut/base/CMakeLists.txt：GLOB testcase/*.cc → ut_metadef 可执行文件
  ──add_test + LABELS "ut"──▶  ctest -L ut 执行
```

5. 本实践为源码阅读型实践，不需要运行环境即可完成。

#### 4.3.5 小练习与答案

**练习 1**：顶层 CMakeLists.txt 中 `inc`、`base`、`tests` 三个子目录的加入条件有什么不同？

答案：`inc` 和 `base` 无条件加入（[CMakeLists.txt:L109-L110](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L109-L110)），因为接口与实现是发布必需品；`tests` 仅在 `ENABLE_METADEF_UT OR ENABLE_METADEF_ST` 时加入（[CMakeLists.txt:L112-L114](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L112-L114)），普通发布编译不含测试。

**练习 2**：metadef 产出的 4 个库目标叫什么？安装头文件来自哪两个目录？

答案：`exe_graph`、`opp_registry`、`rt2_registry_static`、`metadef`（[CMakeLists.txt:L116-L121](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L116-L121)）；头文件来自 `inc/` 和 `pkg_inc/`（[CMakeLists.txt:L123-L129](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L123-L129)），都被拷到安装目录 `metadef/` 下。这些名字在单元三、单元四会反复出现。

**练习 3**：为什么 metadef 的 CI 里经常出现「某个告警导致编译失败」？对应哪行配置？

答案：因为 [CMakeLists.txt:L69-L71](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt#L69-L71) 对 GCC/Clang 启用了 `-Wall -Wextra -Werror`，任何告警都会升级为错误。作为被众多组件依赖的基础库，这是保证代码质量的硬约束。

## 5. 综合实践

**任务：完成一次「编译 -> 单测 -> 定位一个用例」的完整闭环。**

1. 准备环境：`source <install_path>/cann/set_env.sh`，确认 `echo $ASCEND_HOME_PATH` 非空，`bash scripts/check_env.sh` 全部关键项 `[PASS]`。
2. 执行 `bash build.sh -j$(nproc)`，观察输出中的 `CMAKE_ARGS is: ...` 一行，对照 [build.sh:L240-L258](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/build.sh#L240-L258) 逐个说出每个 `-D` 参数的作用；成功后在 `build_out/` 找到 `.run` 包。
3. 执行 `bash tests/run_test.sh -u`，等待 4 个 ut 目标编译并跑完 ctest。
4. 打开 `build_gcov/ctest_ut.log`，任选一个测试用例（建议挑名字里含 `Tensor` 或 `Shape` 的，与单元二呼应），记录：
   - 用例完整名（`TestSuite.TestCase`）；
   - 它属于哪个 ut 目标（用 log 分段或 [tests/ut/CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/CMakeLists.txt) 的目录划分判断）；
   - 用一句话说明该用例验证的行为。
5. 反向验证链路：把你在第 4 步找到的用例文件路径，沿 4.3.4 的链路图向上追溯到 run_test.sh 的 `-u` 参数，确认每一环都讲得通。

若本地无 Ascend 环境无法执行，第 2、3 步标注「待本地验证」，但第 4 步可改为：直接在 `tests/ut/base/testcase/` 目录下阅读任一测试文件（如 `tensor_unittest.cc`），完成同样的三项记录。

## 6. 本讲小结

- `build.sh` 是编译打包入口：校验 `ASCEND_HOME_PATH` -> `cmake` + `make all/install/package` -> 产出 `build_out/cann-metadef_*.run`；默认不编译单测。
- `tests/run_test.sh -u` 以 `ENABLE_METADEF_UT=on` 重新配置到独立的 `build_gcov/` 目录，编译 `ut_metadef/ut_register/ut_exe_meta_device/ut_sc_check` 四个目标并用 `ctest -L ut` 执行，日志在 `build_gcov/ctest_ut.log`。
- 顶层 CMake 无条件编译 `inc` + `base`，仅在测试开关打开时纳入 `tests`；GCC/Clang 下 `-Wall -Wextra -Werror` 使告警即编译失败。
- 测试子工程用 `stub_module` 把 slog/mmpa/platform/runtime 替换成桩实现，让单测摆脱真实硬件依赖，并默认开启 ASan/LSan。
- `ut_metadef` 通过 `GLOB_RECURSE` 自动收集 `tests/ut/base/testcase/*.cc`，新增测试文件无需修改 CMake。
- 三个最小模块的关系：shell 脚本是「参数封装层」，CMake 才是真正的构建描述，读 CMake 是理解工程结构的捷径。

## 7. 下一步学习建议

下一讲（u1-l3《目录结构与头文件布局》）将深入 `inc/external`、`pkg_inc`、`base` 的内部分工，弄清「一个对外接口从声明到实现」的文件对应关系。在那之前，建议你动手做两件事：

1. 打开顶层 [CMakeLists.txt](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CMakeLists.txt) 的 `add_subdirectory` 指到的 `base/` 目录，浏览一层子目录名，与本讲的产物目标（metadef、opp_registry、exe_graph）对上号。
2. 翻看 `tests/ut/base/testcase/` 下的文件名列表，你会发现大量与 `inc/external` 头文件同名的测试（如 `ascend_string_unittest.cc`）——这就是单元二精读源码时配套的验证材料。
