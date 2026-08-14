# 构建与打包：build.sh 与 CMake 体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `build.sh` 的参数解析（`checkopts`）与构建主流程（`build_oam_tools`、`main`）。
2. 理解根目录 `CMakeLists.txt` 如何把 asys、msaicerr、hccl_test、msprof 四个组件组织进同一次 CMake 构建。
3. 说清三方依赖（cann-cmake、protobuf 等）与闭源 bundle 包的下载机制：在线怎么下、离线怎么预置、分支怎么解析。
4. 能独立拼出一组正确的 `build.sh` 参数，完成「编译 + 打 run 包 + 只跑 asys UT」的组合流程。

## 2. 前置知识

- **make / CMake**：CMake 是一个「构建系统生成器」——它本身不编译代码，而是根据 `CMakeLists.txt` 生成 Makefile，再由 `make` 真正编译。`cmake ..` 是配置期（生成 Makefile），`make` 是编译期。
- **CPack**：CMake 自带的打包工具，`make package` 时被触发，可以把 `install()` 规则收集的文件打成 `.run`/`.rpm`/`.deb` 等安装包。
- **makeself / .run 包**：`.run` 是华为昇腾软件常用的自解压安装包格式，底层由 makeself 生成，执行 `bash xxx.run` 即可安装。
- **OBS**：华为的对象存储服务（类似一个文件服务器），oam-tools 的闭源二进制 bundle 就放在上面，构建期按 URL 下载。
- **bundle（闭源包）**：oam-tools 是开源仓，但少数诊断用的闭源库（如 `libascend_ml.so`）不随源码发布，而是构建期从 OBS 拉取一个 tar 包解压到 `bundle/` 目录再一起打进安装包。
- **UT / ST**：Unit Test（单元测试，测函数/类）与 System Test（系统级测试，在本讲语境里包括安装/升级/卸载等包行为验证）。
- **submodule**：本仓在构建期会用 `git clone --depth 1` 拉取 msprof、msprobe 两个「子仓」到 `submodule/` 目录（不是 git 原生 submodule，是脚本模拟的）。

建议先回顾上一讲（u1-l1）：四大组件 asys / msaicerr / msprof / hccl_test 的定位，本讲要解释的就是这套共享构建体系如何把它们打成一个 `.run` 包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh) | 顶层构建入口：解析命令行参数、组装 CMake 参数、驱动 cmake/make/package、调用测试脚本 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt) | 根构建脚本：引入 cmake 模块、按组件 add_subdirectory、处理 bundle 文件装包 |
| [version.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/version.cmake) | 声明包版本（9.1.0）与编译/运行期对 runtime、metadef 的版本依赖 |
| [cmake/fetch_cann_cmake.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/fetch_cann_cmake.cmake) | 拉取 cann-cmake 公共构建函数库（在线 FetchContent / 离线 tar 包二选一） |
| [cmake/install_bundle.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/install_bundle.cmake) | 闭源 bundle 的拉取、分支解析（显式 > git 探测 > master 兜底）与解压 |
| [cmake/build_submodules.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake) | 填充 msprof/msprobe 子仓并构建 msprof 分析 wheel |
| [cmake/download_libs.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py) | 离线预置脚本：提前下载三方库 tar 包、闭源 bundle（双架构）并 clone 子仓 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/package.cmake) | CPack 打 run 包：装包脚本、安装器公共脚本、按架构命名 |
| [scripts/run_tests.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh) | 被 `build.sh -u` 调用的测试总入口，按组件分派 pytest/gtest |

## 4. 核心概念与源码讲解

### 4.1 build.sh：参数解析与构建主流程

#### 4.1.1 概念说明

`build.sh` 是整个仓库唯一需要用户直接执行的构建命令。它解决三个问题：

1. **给用户一个稳定简洁的入口**——用户不需要记住十几个 `-D` CMake 变量，只需 `bash build.sh`。
2. **参数校验前置**——非法参数（如 `--pkg-type=zip`、带特殊字符的 `--bundle_branch`）在 shell 层就被拒绝，不会带病进入 CMake。
3. **串联完整链路**——一次调用完成「cmake 配置 → make 编译 → make package 打包 → （可选）跑测试」。

#### 4.1.2 核心流程

```text
main "$@"
  ├─ checkopts "$@"            # 1. 解析参数、设默认值、确定 ASCEND_HOME_PATH
  ├─ 线程数钳制                 # 2. THREAD_NUM 不得超过 CPU 核数
  ├─ is_python_only_component?  # 3. 若 -u 且组件是 asys/msaicerr（纯 Python），
  │    └─ generate_asys_chip_handler  #    跳过重量级构建，只生成 chip_handler.py
  ├─ build_oam_tools            # 4. 组装 CMAKE_ARGS → cmake .. → make → make package → 搬包
  └─ ENABLE_UT=on 时            # 5. source setenv.bash → bash scripts/run_tests.sh --component …
```

关键点：**纯 Python 组件的快车道**。asys 和 msaicerr 没有编译产物，跑它们的 UT 不需要编译整个 C++ 工程，`build.sh` 会跳过 cmake/make/cpack 全流程，只执行 `cmake -P src/asys/asys.cmake` 生成一个模板文件 `chip_handler.py`（这个机制在 u2-l4 会详细讲）。

#### 4.1.3 源码精读

**参数定义与默认值。** 所有开关都有默认值，且 `BUNDLE_BRANCH` 被显式清空——注释解释了原因：环境里的同名变量会「越权」透传给 CMake：

- [build.sh:64-79](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L64-L79)：`checkopts()` 开头初始化 `THREAD_NUM=8`、`BUILD_TYPE=Release`、`PACKAGE_TYPE=run`、`TEST_COMPONENT=all` 等默认值。
- [build.sh:92](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L92)：用 `getopt -a -o j:hvuO: -l help,verbose,cov,...` 一次性声明全部短/长选项，这是 shell 参数解析的标准做法（`-a` 允许长选项单破折号）。
- [build.sh:135-138](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L135-L138)：`--pkg-type` 只接受 run/rpm/deb，非法值直接 `usage && exit 1`。
- [build.sh:153-162](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L153-L162)：`--bundle_branch` 用正则 `^[A-Za-z0-9._/-]+$` 做白名单校验。注释很值得读：`CMAKE_ARGS` 后续是**非引号展开**拼进命令行的，分支名若含空格或 shell 元字符会被二次解析，所以此处选择「拒绝」而不是「加引号」（加引号会让引号成为传给 CMake 的字面量）。

**CMake 参数组装与编译打包。**

- [build.sh:282-296](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L282-L296)：把 shell 变量翻译成 `-DCMAKE_INSTALL_PREFIX=...`、`-DENABLE_UT=...`、`-DPACKAGE_TYPE=...` 等 CMake 变量，装进 `CMAKE_ARGS`。
- [build.sh:301-303](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L301-L303)：只有用户显式传了 `--bundle_branch` 才追加 `-DOAM_BUNDLE_BRANCH=`，否则留给 CMake 配置期做 git 探测。
- [build.sh:306-321](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L306-L321)：打包三步——先 `rm -f cann*.run ...` 清掉历史产物（避免旧包被误当本次产物）；再 `make -jN && clean_cpack_staging && make package`；最后**只搬当前 `PACKAGE_TYPE` 后缀**的包到 `build_out/`（用 `compenv -G` 判断存在性，而不是盲目 `mv`）。

**主流程与纯 Python 快车道。**

- [build.sh:329-334](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L329-L334)：`is_python_only_component()` 用 `case` 判断组件是否为 asys/msaicerr。
- [build.sh:350-354](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L350-L354)：`-u` + 纯 Python 组件时置 `skip_build=on`，只调 `generate_asys_chip_handler`。
- [build.sh:371-385](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L371-L385)：把 `--component`/`--ut`/`--st`/`--cov` 翻译成 `run_tests.sh` 的参数并调用。这就是「`build.sh -u --component asys` 只跑 asys 测试」的实现处。

#### 4.1.4 代码实践

**实践目标**：在不编译任何东西的前提下，摸清 `build.sh` 的全部参数面。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   bash build.sh -h
   ```

2. 把输出的「Default Build Pkg Options」与「Test Options」两段抄下来，与 [build.sh:27-61](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L27-L61) 的 `usage()` 逐行对照。
3. 再故意传一个非法值验证校验路径：

   ```bash
   bash build.sh --pkg-type=zip   # 应打印 usage 并报 Invalid value
   bash build.sh --bundle_branch='master;rm'  # 应报 only [A-Za-z0-9._/-] allowed
   ```

**需要观察的现象**：`-h` 输出与源码 `usage()` 一致；两条非法命令都以非 0 退出且不会进入任何 cmake 调用。

**预期结果**：你能列出全部参数：`-h/-v/-j<N>/-O<N>/--make_clean/--build-type/--pkg-type/--pkg/--ascend_install_path/--cann_3rd_lib_path/--bundle_branch/-u/--noexec/--cov/--component/--ut/--st`（还有一个 `-h` 未列出但 `getopt` 里存在的 `--asan`，可在 [build.sh:164-166](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L164-L166) 找到）。本实践无环境依赖，可直接本地验证；若你的 shell 环境异常无法执行，则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`build.sh -u --component asys` 与 `build.sh -u --component msprof` 走的构建路径有何本质区别？

> **答案**：asys 是纯 Python 组件，命中 [build.sh:350-354](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L350-L354) 的 `skip_build=on` 快车道，跳过 cmake/make/package，只生成 `chip_handler.py` 后直接进测试；msprof 是 C++ 组件，必须完整走 `build_oam_tools()` 编译链接后才跑 gtest。

**练习 2**：为什么 [build.sh:79](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L79) 要显式 `BUNDLE_BRANCH=""`？

> **答案**：若环境里恰好有同名环境变量，`-n "${BUNDLE_BRANCH}"` 会误判为「用户显式指定」，把环境变量的值透传给 CMake，既覆盖配置期的 git 自动探测，又绕过 `--bundle_branch` 解析处的字符白名单校验。显式清空保证只有命令行参数能生效。

**练习 3**：`build.sh` 打完包后产物在哪里？

> **答案**：在仓库根目录的 `build_out/` 下。见 [build.sh:315-318](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L315-L318)，`make package` 产生的 `cann*.run` 被 `mv` 到 `BUILD_OUT_PATH`（即 `build_out`）。

### 4.2 CMakeLists.txt：四个组件如何被组织进一次构建

#### 4.2.1 概念说明

根 `CMakeLists.txt` 是「总装配线」：它不做具体编译，而是依次完成环境探测（找 CANN 安装目录）、公共设施引入（cann-cmake、protobuf、bundle）、组件挂载（`add_subdirectory`）、文件装包规则（`install()`），最后交给 CPack 打包。四个组件在这里被纳进同一个 CMake 工程，共享同一套三方依赖和打包配置——这就是上一讲说的「四大组件共享一套构建体系」的落点。

#### 4.2.2 核心流程

根 CMakeLists 的执行顺序（配置期）：

```text
1. fetch_cann_cmake.cmake     → 拿到 cann-cmake 公共函数库
2. init_cann_project / 版本信息 → version.cmake 声明 9.1.0 与依赖
3. 探测 ASCEND_DIR             → 环境变量优先，其次 root/普通用户默认路径
4. build_submodules.cmake      → 填充 msprof/msprobe 子仓、构建 msprof wheel
5. install_bundle.cmake        → 拉取并解压闭源 bundle 到 bundle/
6. protobuf 等三方依赖         → add_cann_third_party(protobuf)
7. add_subdirectory(src/asys / msaicerr / hccl_test / operator_cmp)
8. add_subdirectory(src/msprof/collector/dvvp/msprofbin / acp)
9. bundle 内闭源文件的 install() 规则（aml、built-in）
10. cmake/package.cmake        → pack_built_in() 定义 run 包
```

#### 4.2.3 源码精读

**版本与依赖声明。** [version.cmake:11-16](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/version.cmake#L11-L16)：`set_cann_package(oam-tools VERSION "9.1.0")` 声明包名与版本，`set_cann_build_dependencies(runtime ">=9.0")` 等声明编译期/运行期对 CANN runtime 和 metadef 的版本要求——这些由 cann-cmake 提供的函数消费，用于构建期和安装期的版本兼容检查（具体在 u6-l3 展开）。

**CANN 安装目录探测。** [CMakeLists.txt:41-58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L41-L58)：优先取环境变量 `ASCEND_HOME_PATH`（`build.sh` 的 `checkopts` 已保证其存在），否则按 root/普通用户分别落到 `/usr/local/Ascend/...` 或 `~/Ascend/...`，并优先选择 `ascend-toolkit/latest` 子路径。探测结果存入 `ASCEND_CANN_PACKAGE_PATH`，随后的 `link_directories`（[CMakeLists.txt:66-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L66-L70)）把它加入链接搜索路径——**这就是 oam-tools 编译期依赖本机已装 CANN 的原因**。

**组件挂载。**

- [CMakeLists.txt:107-110](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L107-L110)：`add_subdirectory(src/asys)`、`src/msaicerr`、`src/hccl_test`、`src/operator_cmp` 四个子目录进入构建。asys/msaicerr 虽是纯 Python，其子目录 CMake 主要负责「装文件」而非编译。
- [CMakeLists.txt:118-119](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L118-L119)：msprof 的 C++ collector 部分（msprofbin、acp）在此挂载；msprof 的 Python 分析 wheel 则由第 4 步的 `build_submodules.cmake` 单独构建（采集与分析解耦的架构体现，详见 u4-l1）。
- [CMakeLists.txt:121-129](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L121-L129)：仅当 `ENABLE_COV` 或 `ENABLE_UT` 开启时才引入 boost/mockcpp/gtest 并挂载 `test/` 目录——测试依赖不污染正常构建。

**bundle 闭源文件的装包规则（示例）。**

- [CMakeLists.txt:194-201](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L194-L201)：对 `libascend_ml.so`、`libascend_ml_detect.so` 两个必需库做 configure 期存在性检查，缺失即 `FATAL_ERROR`——注释写明设计意图：「与其产出运行期才崩的残缺包，不如构建期就大声失败」。
- [CMakeLists.txt:202-208](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L202-L208)：`file(GLOB ... libascend_ml*.so)` + `install(FILES ... DESTINATION ${CMAKE_SYSTEM_PROCESSOR}-linux/lib64)` 把闭源库按 CPU 架构子目录装包（安装后位于 CANN 包的 `x86_64-linux/lib64/` 或 `aarch64-linux/lib64/`）。
- [CMakeLists.txt:290-302](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L290-L302)：`built-in` 目录里的预置算子以 `install(DIRECTORY ... FILE_PERMISSIONS OWNER_READ GROUP_READ)` 装入 `opp/built-in/op_impl/ai_core`，文件权限显式 440 与主线 CANN 包保持一致。

**打包收尾。** [CMakeLists.txt:308-310](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L308-L310)：`include(cmake/package.cmake)` 后调用 `pack_built_in()`，由它定义 run 包的名字规则、安装脚本目录等（见 4.3.3）。

#### 4.2.4 代码实践

**实践目标**：建立「改一个参数 → 追踪它在 CMake 侧落到哪」的能力。

**操作步骤**：

1. 从 `build.sh` 的 `CMAKE_ARGS`（[build.sh:282-296](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L282-L296)）里任选一个变量，例如 `-DENABLE_UT`。
2. 在 `CMakeLists.txt` 里搜索它出现的位置（如 [CMakeLists.txt:121](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L121) 的 `if (ENABLE_COV OR ENABLE_UT)`），说明它控制了什么（引入测试三方库 + 挂载 test/ 目录）。
3. 对 `-DPACKAGE_TYPE`、`-DENABLE_GCOV`、`-DOAM_BUNDLE_BRANCH` 各重复一次。

**需要观察的现象**：每个 shell 层参数都能在 CMake 层找到唯一的消费点；`-DOAM_BUNDLE_BRANCH` 在根 CMakeLists 里搜不到，因为它是在 `install_bundle.cmake` 里消费的。

**预期结果**：得到一张「build.sh 参数 → CMake 变量 → 消费位置」三列对照表。本实践是纯源码阅读，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 oam-tools 的构建机器上通常要先装好 CANN（或指定 `--ascend_install_path`）？

> **答案**：根 CMakeLists 的 `link_directories` 把 `ASCEND_CANN_PACKAGE_PATH`（本机 CANN 安装目录）及其 `lib64`、`devlib` 加入链接路径（[CMakeLists.txt:65-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L65-L70)），msprof、hccl_test 等 C++ 组件要链接其中的 runtime 等库；`version.cmake` 还声明了 `runtime ">=9.0"` 的编译依赖。

**练习 2**：`test/` 目录什么时候进入构建？

> **答案**：仅当 `ENABLE_UT` 或 `ENABLE_COV` 为真（即 `build.sh` 传了 `-u` 或 `--cov`）时，见 [CMakeLists.txt:121-129](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L121-L129)，同时会引入 boost、mockcpp、gtest_shared 三个测试用三方库。

**练习 3**：闭源库 `libascend_ml.so` 缺失时构建会怎样？

> **答案**：CMake 配置期直接 `FATAL_ERROR`（[CMakeLists.txt:198-200](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CMakeLists.txt#L198-L200)），不会生成 Makefile，更不会打出运行期缺库的安装包。

### 4.3 cmake 模块：三方库下载、闭源 bundle 与打包

#### 4.3.1 概念说明

`cmake/` 目录是构建体系的「零件库」，本讲聚焦四类：

1. **公共函数库获取**（`fetch_cann_cmake.cmake`）：oam-tools 复用 CANN 社区的 cann-cmake 仓库里的打包/三方库函数，配置期自动拉取。
2. **三方库管理**（`dependencies.cmake`、`third_party/`、`download_libs.py`）：protobuf、gtest、mockcpp 等以源码 tar 包形式下载后编译。
3. **闭源 bundle**（`install_bundle.cmake` + `download_libs.py`）：从 OBS 按分支+架构拉取闭源包。
4. **子仓与打包**（`build_submodules.cmake`、`package.cmake`）：拉 msprof/msprobe 源、构建 wheel、定义 run 包。

#### 4.3.2 核心流程

**bundle 分支解析**是本模块最精巧的机制，规则是「显式指定 > git 探测 > master 兜底」：

```text
oam_resolve_bundle_branch:
  1. -DOAM_BUNDLE_BRANCH 非空?           → 直接用（显式）
  2. git for-each-ref 枚举全部远端分支
     对每个分支名:
       去掉 remote 前缀 (origin/ → 空)
       master 原样保留; 形如 9.1.0 / 9.1.0-beta.3 → 归一化为 9.1.0
       只保留白名单 {master, 9.1.0} 内的候选
       git rev-list --count <ref>..HEAD  # HEAD 领先该分支多少个提交
     取「领先提交数最小」的分支            → 血缘最近的发布线（git 探测）
  3. 都失败 → master 兜底
随后按 <BASE_URL>/<branch>/cann-oam-tools-release-<arch>.tar.gz 拼 URL 下载
```

直觉解释：你的 HEAD 通常刚从某条发布线切出来，HEAD 领先该线的提交数最小；领先其它线的提交数会大得多。用「最小领先数」即可定位血缘最近的分支。

**离线预置**（`download_libs.py`）则把同一套 URL 规则在 Python 里复刻一遍，提前把所有 tar 包和子仓下载到本地，供无外网的联编机器使用（配合 `--cann_3rd_lib_path` 指向预置目录）。

#### 4.3.3 源码精读

**cann-cmake 获取。** [cmake/fetch_cann_cmake.cmake:2-27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/fetch_cann_cmake.cmake#L2-L27)：三条路径二选一/三选一——本地 `third_party/cann-cmake` 目录优先；否则若 `CANN_3RD_LIB_PATH` 下有带 SHA256 校验的离线 tar 包就用离线包；最后才 `git clone https://gitcode.com/cann/cmake.git`（tag `master-037`）。拿到后 include 其 `function/prepare.cmake`，后续的 `set_cann_package`、`add_cann_third_party`、`npu_op_package` 等函数都来自它。

**bundle 拉取与分支探测。**

- [cmake/install_bundle.cmake:19-25](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/install_bundle.cmake#L19-L25)：OBS 基址与分支白名单（注释说明截至当前仅 master、9.1.0 两条路径可下载，其余返回 403）。
- [cmake/install_bundle.cmake:39-45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/install_bundle.cmake#L39-L45)：`oam_resolve_bundle_branch()` 函数开头——显式 `OAM_BUNDLE_BRANCH` 优先返回。
- [cmake/install_bundle.cmake:96-115](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/install_bundle.cmake#L96-L115)：对每个候选远端分支执行 `git rev-list --count <ref>..HEAD` 算领先提交数，取最小者——git 探测的核心三行。

**子仓填充。** [cmake/build_submodules.cmake:33-77](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L33-L77)：`oam_populate_submodule()` 函数——目标目录非空则视为已就绪直接复用；否则若 `CANN_3RD_LIB_PATH` 下有同名目录就 `copy_directory`（离线预置路径），都没有才 `git clone --depth 1` 浅克隆。空目录残壳会被重新填充，避免把「取源失败」推迟成难定位的构建报错。

**离线预置脚本。**

- [cmake/download_libs.py:30-40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L30-L40)：与 install_bundle.cmake 逐常量对齐的 OBS 基址、分支白名单、发布线正则和双架构列表——注释强调「须与 install_bundle.cmake 保持一致」，因为两边要拼出完全相同的 URL。
- [cmake/download_libs.py:222-258](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L222-L258)：`__main__` 部分的三方库 URL 清单：protobuf 25.1、makeself 2.5.0、abseil-cpp、googletest 1.14.0、mockcpp 2.7，全部来自 `gitcode.com/cann-src-third-party` 的 release 附件；外加按分支拼出的双架构 bundle 和 msprobe/msprof 两个 git 仓。
- [cmake/download_libs.py:115-129](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L115-L129)：`write_bundle_branch_metadata()` 给下载到的 bundle tar 旁写 `<tar>.branch` 元数据。原因：预置包文件名不含分支信息（各分支同名），联编的 install_bundle.cmake 命中本地预置包后靠这个文件校验分支，不匹配就在配置期报错，防止离线构建静默混入其它分支的闭源包。

**run 包定义。** [cmake/package.cmake:61-100](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/package.cmake#L61-L100)：`pack_built_in()` 先探测架构（x86_64 / aarch64），再把 `scripts/package/oam_tools/scripts` 下的安装/卸载脚本 install 到包内 `share/info/oam_tools/script`，并从 cann-cmake 带来一组安装器公共脚本（版本检查 awk、shell 环境接口等）——这些脚本就是 `.run` 包安装时执行的东西（u6-l3 展开）。

#### 4.3.4 代码实践

**实践目标**：手工推演一次 bundle 分支解析，验证你对「显式 > git 探测 > master」规则的理解。

**操作步骤**：

1. 在仓库根目录执行 `git for-each-ref --format='%(refname:short)' refs/remotes`，列出所有远端分支。
2. 对每个形如 `9.1.0`、`9.1.0-beta.N` 或 `master` 的分支执行 `git rev-list --count <ref>..HEAD`，记录领先提交数。
3. 按 [cmake/install_bundle.cmake:80-115](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/install_bundle.cmake#L80-L115) 的规则（去前缀 → 归一化 → 白名单过滤 → 取最小领先数）手工算出探测结果。
4. 拼出最终下载 URL：`https://cann-3rd.obs.cn-north-4.myhuaweicloud.com/cann/oam-tools-diag/<分支>/cann-oam-tools-release-<你的架构>.tar.gz`。

**需要观察的现象**：多数远端分支会被正则或白名单过滤掉；`rev-list --count` 的输出是一个非负整数。

**预期结果**：得出一个确切的分支名（本仓 HEAD 在 master 线上，通常探测结果为 master，领先数 0）。本实践只依赖 git 只读命令，可直接本地验证；若仓库远端信息缺失导致 `for-each-ref` 为空，则探测回退 master——这正是兜底逻辑。

#### 4.3.5 小练习与答案

**练习 1**：离线机器无法访问 OBS，如何构建 oam-tools？

> **答案**：先在有网的机器上运行 `python3 cmake/download_libs.py`（可用 `--bundle_branch` 指定分支），把下载产物（三方库 tar、双架构 bundle tar 及其 `.branch` 元数据、msprof/msprobe 仓）带到离线机器，构建时用 `bash build.sh --cann_3rd_lib_path=<预置目录>` 让 CMake 优先从本地取，见 [cmake/download_libs.py:208-215](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L208-L215) 与 [cmake/build_submodules.cmake:49-60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/build_submodules.cmake#L49-L60)。

**练习 2**：`download_libs.py` 为什么只给「本轮确实下载成功」的 bundle tar 写 `.branch` 元数据？

> **答案**：见 [cmake/download_libs.py:115-124](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L115-L124) 的注释：若只按「文件是否存在」写元数据，目录里残留的旧分支 tar 会被误标成本轮分支，联编时反而错误通过分支校验。所以只标确实取到的，且对没取到的删除陈旧元数据（[cmake/download_libs.py:277-284](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L277-L284)）。

**练习 3**：`download_single_file` 对 URL 做了什么安全限制？

> **答案**：只允许 `https://` 开头的 URL（拒绝 `file:/` 等本地 scheme），见 [cmake/download_libs.py:167-169](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L167-L169)；另外 `.git` 结尾的 URL 走 git clone 分支而非 wget（[cmake/download_libs.py:146-161](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/cmake/download_libs.py#L146-L161)）。

## 5. 综合实践

**任务**：编写一个 `my_build.sh`（示例代码，放在仓库外任意目录均可，不要提交进仓库），调用 `build.sh` 完成「Release 编译 + 打 run 包 + 只跑 asys UT」的组合流程，并在无昇腾设备的环境下至少验证参数拼接正确。

参考骨架（示例代码）：

```bash
#!/bin/bash
# my_build.sh —— 组合流程：编译 + 打 run 包 + 只跑 asys UT
set -euo pipefail

REPO=/path/to/oam-tools          # 改成你的仓库路径
cd "${REPO}"

# 步骤 1：只编译 + 打 run 包（不出测试）
bash build.sh --build-type=Release --pkg-type=run -j8

# 步骤 2：只跑 asys 的 UT（asys 是纯 Python 组件，走快车道，不会重复编译）
bash build.sh -u --component asys --ut
```

**验证要点**：

1. `set -euo pipefail` 保证第一步失败时不会继续第二步。
2. 第二条命令命中 [build.sh:350-354](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L350-L354) 的纯 Python 快车道——即使没有昇腾设备和 CANN 环境，asys UT 也可能直接跑起来（取决于 pytest 依赖）；若本机连 python3/pytest 都没有，则把「能观察到 `skip oam_tools build for python-only component: asys` 这行日志」作为最低验证目标，其余标注「待本地验证」。
3. 对照 [scripts/run_tests.sh:26-35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L26-L35) 的 `TEST_CASES` 表，确认 `asys_ut` 对应 pytest，说明你的参数最终会落到 `test/ut/asys` 下的 pytest 用例。

**进阶**（可选）：给 `my_build.sh` 加一个 `--dry-run` 开关，只 `echo` 将要执行的 build.sh 命令而不真正执行，用来在完全无构建环境（如纯阅读源码的笔记本）上验证脚本逻辑。

## 6. 本讲小结

- `build.sh` 是唯一构建入口：`checkopts` 解析参数并做白名单校验，`build_oam_tools` 组装 `CMAKE_ARGS` 驱动 `cmake → make → make package`，产物（`cann*.run`）最终落到 `build_out/`。
- asys/msaicerr 是纯 Python 组件，`-u --component asys|msaicerr` 走快车道跳过整个 C++ 构建，只生成 `chip_handler.py` 后直接进测试。
- 根 `CMakeLists.txt` 是总装配线：探测本机 CANN 安装目录、拉取 cann-cmake 公共函数库、按 `add_subdirectory` 挂载四个组件、用 `install()` 规则收集闭源 bundle 文件，最后由 `cmake/package.cmake` 定义 run 包。
- 闭源 bundle 从 OBS 按分支+架构下载，分支解析规则为「显式 `--bundle_branch` > git 血缘探测（最小领先提交数）> master 兜底」，并有白名单与 `.bundle_branch` 元数据双重防错。
- 三方库（protobuf、gtest、mockcpp 等）来自 `gitcode.com/cann-src-third-party` 的 release 附件；`cmake/download_libs.py` 把在线构建需要的所有下载在 Python 里复刻一遍，用于离线预置。

## 7. 下一步学习建议

下一讲（u1-l3 目录结构与入口文件地图）会把视角从「怎么构建」转到「构建的原料」：四个组件的入口文件（`src/asys/asys.py`、`src/msaicerr/msaicerr.py` 等）以及安装后释放到 CANN `tools/` 目录的布局。建议先自己浏览 `src/` 下一级目录，再带着两个问题去读：`build.sh` 里那条 `cmake -P src/asys/asys.cmake` 生成的 `chip_handler.py` 用在哪里？`.run` 包安装后 `asys` 命令为什么能直接执行？
