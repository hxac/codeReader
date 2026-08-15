# 构建与测试：从源码到可执行样例

## 1. 本讲目标

上一讲我们认识了 HIXL 的定位与架构（HIXL Engine + LLM-DataDist + Python 绑定三个组件）。本讲解决一个非常实际的问题：**如何把这份源码变成可以运行的东西，并验证它是对的**。

学完本讲，你应该能够：

1. 准备好 HIXL 源码编译所需的 CANN 环境（Toolkit + ops 包 + 环境变量）。
2. 熟练使用 `build.sh` 的常用参数：`--build-type`、`--asan`、`--examples`、`--host`、`-j<N>` 等。
3. 理解 `build.sh` 背后的 CMake 工程组织方式：顶层 `CMakeLists.txt` 如何根据开关决定编译 `src`、`examples`、`benchmarks` 还是 `tests`。
4. 会用 `tests/run_test.sh` 执行 C++ 与 Python 测试，并能读懂测试失败时的提示信息与日志路径。

## 2. 前置知识

在动手之前，先用通俗语言澄清几个本讲会反复出现的概念：

- **CANN**：昇腾计算架构的软件栈总称（Compute Architecture for Neural Networks）。HIXL 不是独立的裸金属库，它依赖 CANN Toolkit 提供的编译工具链（hcc）、运行时和驱动接口。所以"装好 CANN"是编译 HIXL 的第一前提。
- **Toolkit 包与 ops 包**：Toolkit 是开发套件（编译器、头文件、库）；ops 包提供算子运行时。跑 C++ 样例要 Toolkit + 驱动固件，跑 Python 样例还要 ops 包。
- **host 与 device 两个词**：在本仓库语境中，`src/ops` 下的代码要编译成运行在昇腾 AI CPU 上的 device 产物（`cann-hixl-compat.tar.gz` 内核包），其余代码编译为运行在服务器 CPU 上的 host 产物。默认构建两者都出，`--host` 只出 host 部分。
- **CMake**：C++ 世界的"构建脚本生成器"。`build.sh` 本质上只是帮你拼好参数、调用 `cmake` 和 `make` 的薄封装。
- **ASan（AddressSanitizer）**：编译器内置的内存错误检测器，能查出越界、use-after-free、内存泄漏。`--asan` 打开后编译类型会强制变为 Debug。
- **gtest / unittest**：C++ 单元测试框架 googletest，以及 Python 自带的 `unittest`。本仓库的测试分别基于这两者。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [build.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh) | 源码编译入口脚本：解析参数 → 调用 cmake/make → 打包并搬运产物到 `build_out` |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt) | 顶层 CMake 工程定义：声明所有编译开关，决定挂载哪些子目录 |
| [docs/zh/build.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md) | 官方构建文档：环境准备、依赖版本表、参数说明、本地验证指引 |
| [tests/run_test.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh) | 测试入口脚本：以 `ENABLE_TEST=ON` 重新配置构建，并行跑 C++ 测试与 Python 测试 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**build.sh 编译流程**、**CMake 工程组织**、**tests/run_test.sh 测试执行**。

### 4.1 build.sh：源码编译的入口

#### 4.1.1 概念说明

`build.sh` 是仓库根目录的一键编译脚本。它存在的意义是：把"CMake 需要十几个 `-D` 变量"这件容易写错的事，封装成几个好记的命令行参数。你只需要记住 `bash build.sh --examples`，而不需要记住 `cmake -D ENABLE_EXAMPLES=ON -D ENABLE_BENCHMARKS=ON ...` 这一长串。

它做四件事：

1. 解析命令行参数（`checkopts`）。
2. 创建 `build/` 目录并调用 `cmake` 配置工程。
3. 调用 `make` 编译、`make package` 打包。
4. 把 `.run`/`.deb`/`.rpm` 包搬到 `build_out/` 输出目录。

#### 4.1.2 核心流程

```
main()
 ├── checkopts "$@"          # 解析参数，得到一组开关变量
 ├── g++ -v                  # 打印编译器版本（环境自检）
 ├── mk_dir ${OUTPUT_PATH}   # 创建 build_out/
 └── build()
      ├── mk_dir build/ && cd build/
      ├── cmake -D CMAKE_BUILD_TYPE=... \
                -D ENABLE_EXAMPLES=... \
                -D ENABLE_ASAN=... ... ..
      ├── make -j${THREAD_NUM} && make package
      ├── copy_device_pkg     # 拷贝 device tar 包到 build_out
      └── move_pkg ${PACKAGE_TYPE}  # 搬运 .run/.deb/.rpm
```

几个容易混淆的参数联动关系（读源码可以验证）：

- `--asan` 和 `--cov` 都会把 `CMAKE_BUILD_TYPE` 强制改成 `Debug`（ASan/覆盖率需要带调试信息的构建）。
- `--examples` 不只开 `ENABLE_EXAMPLES`，还同时打开 `ENABLE_BENCHMARKS` 和 `ENABLE_HIXL_TOOL`，即"开发自测三件套"一起编。
- 默认输出目录是 `./build_out`，默认第三方依赖目录是 `./third_party`。

#### 4.1.3 源码精读

**参数默认值全部集中在 checkopts 开头**。[build.sh:75-88](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L75-L88) 一口气初始化了线程数（8）、构建类型（Release）、包类型（run）、样例/基准/ASan/覆盖率开关（全 OFF）等变量。想快速了解一个构建脚本有哪些可调项，先看它的默认值块。

**getopt 长短参数解析**。[build.sh:91-94](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L91-L94) 用 `getopt -a` 同时支持长参数（`--build-type`）和短参数（`-j`），解析失败则打印用法并退出。注意同一个选项同时接受下划线和连字符两种拼写（如 `--build_type` 与 `--build-type`），这是为了兼容不同使用习惯。

**`--examples` 的联动**。[build.sh:133-138](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L133-L138) 中一个 `--examples` 同时置位三个 CMake 变量。所以跑样例不需要再单独打开 benchmarks 开关。

**`--asan`/`--cov` 强制 Debug**。[build.sh:155-163](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L155-L163)：开启任一都会把 `CMAKE_BUILD_TYPE` 覆写为 `Debug`。如果你传了 `--build_type=Release --asan`，最终生效的是 Debug——排错时若发现"Release 参数没生效"，先检查这里。

**cmake 调用点**。[build.sh:231-243](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L231-L243) 把所有 shell 变量翻译成 `-D` CMake 变量传给顶层 CMakeLists。构建产物随后由 [build.sh:245](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L245) 的 `make && make package` 完成编译与打包。

**产物搬运**。[build.sh:181-209](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L181-L209) 的 `move_pkg` 按包类型（run/rpm/deb）在 CPack 临时目录里找对应产物并搬到 `build_out`。`run` 类型对应 `_CPack_Packages/makeself_staging/cann*.run`，即官方文档中提到的 `cann-hixl_${version}_linux-${arch}.run` 安装包。

#### 4.1.4 代码实践

**实践目标**：不实际编译，仅通过脚本文本掌握 `build.sh` 的完整参数表，并验证 `-h` 输出与源码一致。

**操作步骤**：

1. 在仓库根目录执行 `bash build.sh -h`，观察打印的帮助信息。
2. 打开源码对照：帮助信息由 [build.sh:20-49](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L20-L49) 的 `usage()` 函数生成。
3. 自己整理一张参数表（参数、含义、默认值），再与 [docs/zh/build.md:217-232](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md#L217-L232) 的官方表格逐行核对。

**需要观察的现象**：`-h` 不会触发任何编译动作（`usage` 后立即 `exit 0`）。

**预期结果**：整理出的参数表应至少包含 `-j<N>`（默认 8）、`--build_type`（默认 Release）、`--examples`（默认 OFF）、`--asan`（默认 OFF）、`--host`、`--cov`、`--pkg-type`（默认 run）等条目，与文档表格一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档说"若源码未改动或修改不涉及 `src/ops` 下的代码，建议加 `--host`"？

**答案**：`--host` 会跳过 `src/ops` 的 device 子工程编译，转而复用本地缓存或中心仓的 `cann-hixl_<version>_device.tar.gz`（见 [build.sh:139-141](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L139-L141) 与文档 [docs/zh/build.md:236-238](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md#L236-L238)）。device 产物没变时重编它是纯浪费时间，所以日常开发加 `--host` 更快。

**练习 2**：同时传 `--build_type=Release` 和 `--asan`，最终 CMake 收到的 `CMAKE_BUILD_TYPE` 是什么？

**答案**：`Debug`。`--asan` 分支（[build.sh:160-163](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L160-L163)）会无条件覆写 `CMAKE_BUILD_TYPE="Debug"`，与之前传入的值无关（只要 `--asan` 出现在它之后解析，而 while 循环保证参数都会被处理到 asan 分支时覆写）。

**练习 3**：编译成功后，`.run` 安装包出现在哪个目录？由哪段代码负责搬运？

**答案**：`build_out/` 目录。由 `move_pkg run` 分支（[build.sh:200-207](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/build.sh#L200-L207)）从 `_CPack_Packages/makeself_staging/` 搬运过去。

### 4.2 CMake 工程组织：顶层 CMakeLists.txt

#### 4.2.1 概念说明

`build.sh` 只是外壳，真正决定"编什么"的是顶层 [CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt)。它的核心逻辑是一组分诊规则：**根据开关变量决定挂载（`add_subdirectory`）哪些子目录**。

理解了这张"开关→子目录"映射表，你就能回答诸如"为什么我编译完找不到样例"（没开 `--examples`）、"为什么测试构建里没有 host 安装规则"（`ENABLE_TEST` 分支不挂 `src/`）这类问题。

#### 4.2.2 核心流程

顶层 CMakeLists 的决策树：

```
init_cann_project() + include(variables/dependencies/version)
│
├── ENABLE_TEST = ON ?  ──→ 只挂 tests/（测试构建，跳过 device 子工程）
│
└── ENABLE_TEST = OFF ?
      ├── 挂 src/（主库：hixl engine + llm_datadist + python 绑定）
      ├── BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG ? → 挂打包规则 package.cmake
      ├── ENABLE_EXAMPLES   ? → 挂 examples/
      ├── ENABLE_BENCHMARKS ? → 挂 benchmarks/
      ├── ENABLE_HIXL_TOOL  ? → 挂 scripts/tools/hixl_tool/
      └── HIXL_BUILD_HOST_ONLY ?
            ├── ON  → fetch_hixl_device_package.cmake 复用 device 包
            └── OFF → 检查 hcc 工具链存在 → add_cann_device_project(hixl)
```

#### 4.2.3 源码精读

**八个编译开关的声明**。[CMakeLists.txt:17-23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L17-L23) 用 `option()` 声明了 `ENABLE_TEST`、`ENABLE_EXAMPLES`、`ENABLE_BENCHMARKS`、`ENABLE_HIXL_TOOL`、`ENABLE_ASAN`、`ENABLE_GCOV`、`HIXL_BUILD_HOST_ONLY` 等开关，全部默认 OFF。这正好与 `build.sh` 传下来的 `-D` 变量一一对应——两个文件的开关名是一套词汇表。

**测试构建与正常构建互斥**。[CMakeLists.txt:34-50](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L34-L50)：`ENABLE_TEST=ON` 时**只**挂 `tests/`，不挂 `src/`；反之挂 `src/` 并按需挂 examples/benchmarks。这解释了为什么 `build.sh` 和 `run_test.sh` 使用两个不同的构建目录（`build/` 与 `build_test/`）——同一目录下两种配置会互相污染缓存。

**device 构建与工具链检查**。[CMakeLists.txt:53-66](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L53-L66)：非测试构建时，若不是 host-only，会先检查 `${ASCEND_INSTALL_PATH}/toolkit/toolchain/hcc` 是否存在，不存在直接 `FATAL_ERROR`。**这就是"CANN 环境变量没加载"时最常见的报错来源**——`ASCEND_INSTALL_PATH` 是 source `set_env.sh` 后才有的。注释也说明了 UT 构建用 stub 替代真实内核，因此跳过 device 包。

#### 4.2.4 代码实践

**实践目标**：建立"编译开关 → 产物"的映射直觉，能根据报错定位环境问题。

**操作步骤**：

1. 读 [CMakeLists.txt:34-50](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L34-L50)，把每个 `add_subdirectory` 对应的开关和目录写成一张表。
2. 在**未** source CANN 环境变量的 shell 中执行 `bash build.sh`，观察报错。
3. 再 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`（按你的实际安装路径，参考 [docs/zh/build.md:117-129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md#L117-L129)）后重试。

**需要观察的现象**：未加载环境变量时，cmake 阶段报 `CANN toolchain path does not exist: .../toolkit/toolchain/hcc` 的致命错误；加载后该检查通过。

**预期结果**：正常环境下 `bash build.sh` 最终打印 `Build success!` 和 `hixl package success!`，`build_out/` 下出现 `cann-hixl_*.run`。若你的机器没有昇腾环境，此步骤记录阻塞原因即可（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run_test.sh` 的构建目录是 `build_test/` 而不是复用 `build/`？

**答案**：因为测试构建使用 `ENABLE_TEST=ON`，此时顶层 CMakeLists 只挂 `tests/` 而不挂 `src/`（[CMakeLists.txt:34-35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L34-L35)），与正常发布的配置完全不同；CMake 缓存目录混用会导致配置互相覆盖。

**练习 2**：`bash build.sh` 默认（不带 `--examples`）会编译出样例可执行文件吗？

**答案**：不会。`ENABLE_EXAMPLES` 默认 OFF，顶层 CMakeLists 只有在该开关为 ON 时才 `add_subdirectory(examples)`（[CMakeLists.txt:41-43](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L41-L43)）。要跑样例必须显式加 `--examples`。

### 4.3 tests/run_test.sh：测试的构建与执行

#### 4.3.1 概念说明

HIXL 的本地验证不依赖你手动 `build.sh` 出的产物，而是自成一体：`tests/run_test.sh` 会用 `ENABLE_TEST=ON` 重新配置一次构建（在 `build_test/` 目录），把测试代码与 stub（桩实现）链成独立的测试可执行文件，然后运行它们。这样做的好处是：单测可以在没有真实昇腾设备的构建环境里编译，底层硬件调用全部被桩替代。

它同时管理 C++（gtest）和 Python（unittest）两类测试，并支持覆盖率统计。

#### 4.3.2 核心流程

```
main()
 ├── checkopts "$@"              # 解析 -t/-s/-c/--asan/-f 等
 ├── check_changed_files         # 若改动仅涉及 docs/examples/README 等则跳过测试（exit 200）
 └── run()
      ├── mk_dir build_test/ 与 build_out/report/
      ├── build()                # cmake -D ENABLE_TEST=ON ... && make
      ├── C++ 阶段（ENABLE_CPP_TEST=ON）
      │    ├── 对每个 suite 并行启动: <test_bin> --gtest_output=xml:report/<suite>_test.xml
      │    ├── 每个 suite 配一个 600s 超时监视进程
      │    └── 任一 suite 失败 → 打印 "!!! CPP TEST FAILED ... !!!" 并 exit 1
      ├── Python 阶段（ENABLE_PY_TEST=ON）
      │    ├── 拷贝 wrapper/hixl 的 .so 到 src/python 对应包目录
      │    ├── coverage run -m unittest discover python
      │    └── 失败 → "!!! PY TEST FAILED ... !!!"
      └── 覆盖率阶段（ENABLE_GCOV=ON）
           └── lcov 采集 + genhtml 生成 HTML 报告（cov/ 目录）
```

C++ 测试套件（suite）共五个：`llm_datadist`、`adxl`、`channel_pool`、`hixl`、`fabric_mem`。注意 `adxl` 套件实际会跑 `adxl` 和 `channel_pool` 两个二进制。

#### 4.3.3 源码精读

**参数解析与 suite 白名单**。[tests/run_test.sh:72-83](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L72-L83) 的 `set_test_suite` 用 case 列表限定 `-s` 只接受五个 suite 名，传错直接报错退出。`select_cpp_suites`（[tests/run_test.sh:85-95](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L85-L95)）处理特例：`-s adxl` 展开为 `adxl channel_pool` 两个套件；不带 `-s` 则全量五个。

**-t 与 -s 的组合语义**。[tests/run_test.sh:97-124](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L97-L124) 的 `apply_test_selection`：`-t` 省略时默认 cpp+py 全跑；单独 `-s <suite>`（不带 `-t`）只跑该 C++ 套件并跳过 Python；`-t py` 配 `-s` 则直接拒绝（C++ 套件名对 Python 测试无意义）。

**测试构建用独立配置**。[tests/run_test.sh:306-314](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L306-L314) 以 `ENABLE_TEST=ON` 调用 cmake，构建类型为 `DT`，落在 `build_test/`。构建成功后打印 `build success!`——这是文档中"预期结果"的第一个检查点。

**C++ 测试二进制定位**。[tests/run_test.sh:363-381](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L363-L381) 的 `get_cpp_test_bin` 维护 suite 名到构建产物的映射，例如 `hixl` 套件对应 `build_test/tests/cpp/hixl/hixl_test`。想直接用 gtest 过滤器复跑单个用例（如 `hixl_test --gtest_filter=xxx`），就从这里找二进制路径。

**并行执行与 600 秒超时看门狗**。[tests/run_test.sh:388-429](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L388-L429)：每个 suite 以后台任务启动，重定向到 `report/<suite>.log`，同时派生一个监视子进程 sleep 600 秒，超时则先 SIGTERM 再 SIGKILL 杀掉测试进程并落盘 `.timeout` 文件。这防止某个用例死锁拖垮整个流水线。

**失败标志与日志路径**。[tests/run_test.sh:446-450](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L446-L450)：任一 suite 退出码非 0，脚本打印红色的 `!!! CPP TEST FAILED, PLEASE CHECK YOUR CHANGES !!!` 并给出 `log:` 路径——排查时先 `cat` 这个日志。Python 侧对应 [tests/run_test.sh:475-480](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L475-L480)。

**Python 测试的环境拼装**。[tests/run_test.sh:457-466](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L457-L466)：把构建出的 `llm_datadist_wrapper.so`、`hixl.so` 等拷进 `src/python/` 包目录，再临时设置 `PYTHONPATH` 与 `LD_LIBRARY_PATH` 指向 `build_test/tests/depends/` 下的一组桩库，最后用 `coverage run -m unittest discover python` 发现并执行用例。这也说明 Python 测试跑的是**桩环境**，不需要真实 NPU。

**文档类改动自动跳过**。[tests/run_test.sh:235-304](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L235-L304) 的 `check_changed_files` 配合 `-f <file>` 使用：若改动清单里只有 `docs/`、`examples/`、`README.md` 等非代码文件，则跳过整个测试（`exit 200`），常用于 CI 省 时。

#### 4.3.4 代码实践

**实践目标**：跑通 hixl 套件的 C++ 单测（或记录无硬件/无 CANN 环境时的阻塞原因），并学会从日志定位失败用例。

**操作步骤**：

1. 安装依赖：`pip3 install -r requirements.txt`（见 [docs/zh/build.md:244-249](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md#L244-L249)）。
2. source CANN 环境变量。
3. 执行单套件测试：
   ```bash
   bash tests/run_test.sh -s hixl
   ```
4. 观察输出的 `Run (parallel): .../build_test/tests/cpp/hixl/hixl_test --gtest_output=xml:...` 行，确认测试二进制与日志路径。

**需要观察的现象**：

- 构建阶段打印 `build success!`。
- C++ 测试输出 `===== Output: ... =====` 后跟 gtest 的用例统计（`PASSED` / `FAILED` 数量）。
- 脚本末尾没有红色 `!!! CPP TEST FAILED ... !!!`。

**预期结果**：脚本正常结束、退出码 0；`build_out/report/hixl_test.xml` 与 `build_out/report/hixl.log` 生成。若失败，按提示 `cat build_out/report/hixl.log` 查看，修复后用 `bash tests/run_test.sh -t cpp -s hixl` 复跑。若你的机器没有 CANN 环境，cmake 阶段会报找不到工具链（见 4.2.3），此为预期阻塞原因，记录下来即可（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`bash tests/run_test.sh -s adxl` 实际会运行几个测试二进制？分别是哪些？

**答案**：两个。`select_cpp_suites` 把 `adxl` 展开为 `(adxl channel_pool)`（[tests/run_test.sh:86-90](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L86-L90)），对应 `build_test/tests/cpp/adxl/adxl_test` 和 `build_test/tests/cpp/adxl/channel_pool_test`。

**练习 2**：hixl 套件的某个用例失败后，最快的复现方式是什么？

**答案**：先看 `build_out/report/hixl.log` 找到失败的用例名，然后直接运行测试二进制并加 gtest 过滤器：`./build_test/tests/cpp/hixl/hixl_test --gtest_filter=<用例名>`（二进制路径来自 [tests/run_test.sh:374-376](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L374-L376)；注意需要按脚本同样方式设置 `LD_LIBRARY_PATH` 指向 `build_test/tests/depends/` 下的桩库目录）；或用 `bash tests/run_test.sh -t cpp -s hixl` 整套复跑。

**练习 3**：`-s` 不带 `-t` 时 Python 测试还会跑吗？为什么？

**答案**：不会。`apply_test_selection` 的 `all` 分支中，只要指定了 `TEST_SUITE` 就把 `ENABLE_PY_TEST` 置为 off（[tests/run_test.sh:102-106](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L102-L106)），语义是"用户明确只想跑这个 C++ 套件"。

## 5. 综合实践

**任务：完成一次"环境检查 → 编译 → 测试 → 记录"的完整闭环，产出一份个人构建笔记。**

1. **环境检查**：按 [docs/zh/build.md:97-115](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/build.md#L97-L115) 执行 `npu-smi info` 与查看 `ascend_toolkit_install.info`，确认驱动与 Toolkit 状态；source 环境变量脚本。
2. **编译**：执行 `bash build.sh --examples -j16`，确认 `build_out/` 下生成 `cann-hixl_*.run`，并确认 examples 与 benchmarks 的可执行文件已编出（在构建目录下 `find build -name "*example*" -type f -executable | head`）。
3. **测试**：执行 `bash tests/run_test.sh -s hixl`，保存 `build_out/report/hixl.log`。
4. **对照实验**：再用 `bash build.sh --host` 编译一次，对比两次编译耗时，验证 4.1.5 练习 1 的结论。
5. **产出**：把以上每一步的命令、耗时、产物路径、失败与排查过程整理成一页笔记。若任一步因缺少昇腾硬件/CANN 环境而阻塞，如实记录阻塞点对应的源码位置（例如工具链检查在 [CMakeLists.txt:59-61](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L59-L61)），这本身就是有价值的学习产出。

## 6. 本讲小结

- `build.sh` 是 cmake/make 的参数化薄封装：默认值集中在 `checkopts` 开头，`--asan`/`--cov` 会强制 Debug 构建，`--examples` 联动打开样例、基准与工具三个开关。
- 顶层 `CMakeLists.txt` 按"开关 → `add_subdirectory`"组织工程：`ENABLE_TEST=ON` 只挂 `tests/`，正常构建挂 `src/` 并按需挂 examples/benchmarks；非 host-only 构建前会检查 CANN hcc 工具链路径。
- 编译产物最终落在 `build_out/`：`cann-hixl_*.run` 安装包，以及可选的 `cann-hixl_*_device.tar.gz` device 包。
- `tests/run_test.sh` 用独立的 `build_test/` 目录做测试构建，C++ 五个 suite 并行执行、每个有 600 秒超时看门狗，日志在 `build_out/report/`；Python 测试通过桩库 + `PYTHONPATH` 在无 NPU 环境运行。
- 排查口诀：编译失败先查 CANN 环境变量与第三方依赖；C++ 测试失败看红色提示后的 `log:` 路径，再定位到具体 suite 用 `-t cpp -s <suite>` 复跑。

## 7. 下一步学习建议

环境就绪后，下一讲（u1-l3「第一个 HIXL 程序：quickstart 样例精读」）将带你实际运行 `hixl_example_quickstart`：以 server/client 双进程模型走通"初始化 → 内存注册 → 建链 → READ 传输 → 校验"的完整调用序列。在进入下一讲前，建议先浏览 [examples/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md) 了解样例清单，并确认本讲的 `--examples` 编译已产出样例可执行文件。
