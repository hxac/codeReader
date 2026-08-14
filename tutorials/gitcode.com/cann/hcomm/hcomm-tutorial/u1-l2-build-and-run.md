# 源码构建与运行方式

## 1. 本讲目标

上一讲我们认识了 HCOMM 的定位与「控制面 / 数据面」分层架构。这一讲解决一个非常实际的问题：**把这份源码变成一个可安装的软件包**。学完本讲，你应该能够：

1. 读懂 `build.sh` 的参数表，知道 `--pkg`、`--full`、`-u/--ut`、`--noexec` 等选项各自触发什么构建路径。
2. 理解 `build.sh` → `CMakeLists.txt` → 各 `cmake/*.cmake` 模块的两层构建体系，以及 UT / 正式包两条互斥的构建分支。
3. 理解 `version.cmake` 声明的 HCOMM 版本与 CANN 配套版本的绑定关系，知道该配哪个版本的 CANN Toolkit。
4. 在有昇腾环境时完成一次完整构建；在没有硬件时，也能用 `--ut --noexec` 之类的组合验证「代码能编译、测试能构建」。

## 2. 前置知识

在开始之前，用通俗语言解释几个本讲会反复出现的概念：

- **host 侧与 device 侧**：HCOMM 是一个「跨 CPU 和 NPU」的库。跑在服务器 CPU 上、负责通信域管理和 socket 建链的部分叫 **host 侧**；跑在 NPU 内部计算单元（如 AI CPU、CCU）上、真正搬运数据的部分叫 **device 侧**。默认只编译 host 侧，加 `--full`（或 `--aicpu`）才会连 device 侧一起编。
- **CMake**：一个跨平台的构建系统生成器。它本身不编译代码，而是根据 `CMakeLists.txt` 生成 Makefile/Ninja 文件，再由 `cmake --build` 驱动真正的编译。项目常用的「配置 → 编译 → 打包」三段式就是 `cmake -S 源码 -B 构建目录` → `cmake --build` → `make package`。
- **交叉编译**：在 x86 机器上编译出 aarch64（鲲鹏/ARM）可执行文件。HCOMM 通过 CANN Toolkit 自带的 hcc 工具链支持这一点，对应 `--build_aarch` 选项。
- **UT 与 ST**：UT（Unit Test，单元测试）在纯主机环境用 gtest + mockcpp 跑，不需要 NPU；ST（System Test，系统测试）需要真实昇腾设备。这就是为什么无硬件环境只能玩 UT。
- **run 包**：CANN 生态的软件安装包格式，本质是 makeself 自解压脚本，形如 `cann-hcomm_<version>_linux-<arch>.run`，用 `bash xxx.run --full` 安装。
- **ASAN / GCOV**：ASAN（AddressSanitizer）用于检测内存越界等错误；GCOV + lcov 用于生成代码覆盖率报告。二者都是构建期开关。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh) | 一键构建入口：解析命令行参数、定位 CANN 安装路径、拼装 CMake 选项，然后按 UT / ST / 正式包三条路径分发 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt) | 顶层 CMake 配置：定义编译选项、区分 UT 分支与正式构建分支、声明三方依赖、安装头文件与打包 |
| [version.cmake](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/version.cmake) | 版本声明文件：HCOMM 自身版本（9.2.0）以及构建期/运行期对其他 CANN 组件的版本配套要求 |
| [docs/zh/build/build.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/build/build.md) | 官方构建文档：环境依赖清单、CANN 安装步骤、编译/安装/卸载命令、HCCL Test 上板测试流程 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/cmake/package.cmake) | 打包配置：根据 CPU 架构（x86_64/aarch64）生成对应命名的 run 包及安装脚本 |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**build.sh 参数与构建分发**、**CMakeLists.txt 构建体系**、**version.cmake 版本配套**。

### 4.1 build.sh：一键构建入口

#### 4.1.1 概念说明

`build.sh` 是整个项目对外的唯一构建入口。它存在的意义是：把「找 CANN 安装路径、设置编译器工具链、拼 CMake 参数、编译、打包、跑测试」这一长串易错步骤固化成一个脚本。用户只需要记住 `bash build.sh --pkg` 这一条命令。

#### 4.1.2 核心流程

`build.sh` 的执行流程可以概括为：

```text
1. 初始化变量（构建目录、CPU 数、各开关默认值）
2. 逐个解析命令行参数（case 匹配，改写对应变量）
3. 把所有变量拼成 CUSTOM_OPTION（一串 -D key=value）
4. 探测 ASCEND_CANN_PACKAGE_PATH：
   命令行 -p > 环境变量 ASCEND_HOME_PATH > 环境变量 ASCEND_OPP_PATH
   > 默认安装路径 ~/Ascend/ascend-toolkit/latest 或 /usr/local/Ascend/...
5. source CANN 的 set_env.sh 导出环境变量
6. 三选一分发：
   ENABLE_UT=on  -> build_ut（编 UT + ctest 跑用例 + 可选覆盖率）
   ENABLE_ST=on  -> build_st（编指定 ST 任务 + ctest）
   其余          -> build_hcomm（配置 + 编译 + make package 打 run 包）
```

关键目录约定（脚本开头的变量）：

| 变量 | 路径 | 用途 |
| --- | --- | --- |
| `BUILD_DIR` | `./build` | 正式包的 CMake 构建目录 |
| `BUILD_UT_DIR` | `./build_ut` | UT 构建目录 |
| `BUILD_ST_DIR` | `./build_st` | ST 构建目录 |
| `BUILD_OUTPUT_DIR` | `./build_out` | 最终 run 包产物输出目录 |
| `LOGS_PATH` | `./logs` | ctest 运行日志 |

#### 4.1.3 源码精读

**参数解析入口**：脚本用一个 `while + case` 循环处理所有选项，每个选项只负责改写一个 shell 变量，例如 `--noexec` 与 `--experimental`：

[build.sh:348-355](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L348-L355) 这两行把 `ENABLE_NO_EXEC` 和 `ENABLE_EXPERIMENTAL` 置为开启，分别用于「跳过测试执行」和「编译 experimental 实验性组件」。

ST 任务可以按名选择，比如 `--legacy_alg_testcase`：

[build.sh:439-443](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L439-L443) 把任务名追加进 `ST_TASKS` 数组，后面 `build_st` 会把数组拼成 `;` 分隔的字符串传给 CMake 的 `-DST_TASKS`。

**CANN 安装路径探测**：这是新手最容易踩坑的地方——找不到 CANN 就直接报错退出：

[build.sh:498-511](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L498-L511) 按优先级依次尝试命令行 `-p`、环境变量 `ASCEND_HOME_PATH`、`ASCEND_OPP_PATH`、默认安装路径；一个都没有就提示设置 `--package-path` 并退出。注意非 root 用户的默认路径是 `${HOME}/Ascend/...`，root 用户是 `/usr/local/Ascend/...`（见 [build.sh:42-48](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L42-L48)）。

**CMake 选项拼装**：所有 shell 变量最终被翻译成一串 `-D` 参数：

[build.sh:513-530](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L513-L530) 把包路径、构建类型（Release/Debug）、device/aarch/experimental 开关、打包类型（run/rpm/deb/all）、测试开关等全部注入 CMake 缓存变量。

**三路分发**：脚本末尾的 if-elif 决定了本次到底干什么：

[build.sh:537-561](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L537-L561) 优先级是 UT > ST > cb_test_verify > 正式包。也就是说 `bash build.sh --pkg -u` 实际执行的是 UT 构建，而不是打正式包。

**`--noexec` 的生效点**在 `run_ctest` 里：

[build.sh:76-81](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L76-L81) 如果开了 noexec，ctest 直接跳过，只保留「编译测试用例」这一步——这正是无 NPU 环境验证代码可编译的手段。

**正式包构建三步曲**：

[build.sh:237-267](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L237-L267) `build_hcomm` 先导出 `TOOLCHAIN_DIR` 指向 CANN 里的 hcc 工具链，然后依次执行 `cmake -S ../ -B .`（配置）、`cmake --build . -j`（编译）、`make package`（打包成 run 包）。

#### 4.1.4 代码实践

**实践目标**：不真正编译，先做一次「参数干跑」，确认你对参数表的理解。

1. 操作步骤：
   - 执行 `bash build.sh -h` 查看 usage 输出。
   - 执行 `bash build.sh --pkg-type=foo`，观察报错。
   - 执行 `ASCEND_HOME_PATH= bash build.sh --pkg`（假设机器上没装 CANN），观察路径探测失败的报错信息。
2. 需要观察的现象：usage 中列出的选项分组（编译类/打包类/测试类/签名类）；非法 `--pkg-type` 被拒；无 CANN 时停在 "Please set the toolkit package installation directory"。
3. 预期结果：能够把 usage 里的每个选项和 4.1.3 中对应 shell 变量一一对应起来。
4. 若无环境执行，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`bash build.sh --pkg -u --noexec` 最终会生成 `./build_out` 下的 run 包吗？

答案：不会。脚本末尾的分支优先级是 UT > ST > 正式包，`-u` 使 `ENABLE_UT=on`，走的是 `build_ut` 路径；`--noexec` 只影响 UT 里的 ctest 是否执行用例，不改变分支选择。run 包只在都不开测试时由 `build_hcomm` 的 `make package` 生成。

**练习 2**：为什么 `build_ut` 里要 `unset LD_LIBRARY_PATH`（[build.sh:155](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L155)）？

答案：UT 用了大量 mock/stub 替换真实依赖，如果系统里残留的 `LD_LIBRARY_PATH` 指向别的库（比如已安装的 CANN 或其他软件的旧版本动态库），链接/运行时可能加载到非预期版本，导致用例误报。主动清空可以保证 UT 使用构建目录内受控的库。

**练习 3**：想在 16 核之外再多开编译线程，用什么参数？

答案：`-j<N>`，例如 `bash build.sh --pkg -j32`。它改写 `CPU_NUM`，后续 `cmake --build . -j ${CPU_NUM}` 和 ctest 的 `-j` 都用它。

### 4.2 CMakeLists.txt：构建体系与产物

#### 4.2.1 概念说明

`CMakeLists.txt` 是构建的「总装配图」。它决定：编译哪些子目录、依赖哪些三方库和 CANN 组件、头文件安装到哪、run 包里装什么。理解它的关键是一个分叉：**`ENABLE_TEST` 分支（编 UT/ST）与 `BUILD_OPEN_PROJECT` 分支（编正式产品）是互斥的两条装配线**。

#### 4.2.2 核心流程

```text
顶层 CMakeLists.txt
├─ 定义 option 开关：BUILD_OPEN_PROJECT(默认ON) / ENABLE_BUILD_DEVICE / ENABLE_BUILD_AARCH / ENABLE_EXPERIMENTAL
├─ 决定 PRODUCT_SIDE（默认 host）；--full 时由环境变量 TOOLCHAIN_DIR 决定是否编 device
├─ ENABLE_BUILD_AARCH -> 用 CANN 自带 hcc 交叉编译器编 aarch64 包
├─ ENABLE_TEST 分支：
│    enable_testing() + 引入 gtest/mockcpp/json/rdma-core + add_subdirectory(test)
└─ BUILD_OPEN_PROJECT 分支（正式包）：
     引入 version.cmake / package.cmake 等模块
     find_cann_package 逐个查找 runtime、securec、tsdclient 等 12 个 CANN 组件
     add_subdirectory(src) + add_subdirectory(python)
     声明头文件安装规则（hccl/ hcomm/ ccu/ 三层）+ pack_built_in() 打包
```

#### 4.2.3 源码精读

**顶层开关定义**：

[CMakeLists.txt:10-14](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L10-L14) 定义四个 option，其中只有 `BUILD_OPEN_PROJECT` 默认 ON。注意 `ENABLE_EXPERIMENTAL` 对应 `build.sh --experimental`，控制是否编 experimental 目录下的社区实验性组件（见 [CMakeLists.txt:257-259](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L257-L259)，只在 nic_plugin 目录存在时生效）。

**host/device 与交叉编译判定**：

[CMakeLists.txt:17-26](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L17-L26) 默认 `PRODUCT_SIDE=host`；只有当环境里有 `TOOLCHAIN_DIR` 且开了 `ENABLE_BUILD_DEVICE` 才算 device 构建。而 `TOOLCHAIN_DIR` 正是由 `build.sh` 的 `build_hcomm` 函数导出的（[build.sh:241](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/build.sh#L241)）——这就是 shell 层与 CMake 层协作的一个细节。

[CMakeLists.txt:41-49](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L41-L49) 交叉编译场景：把编译器强制切到 CANN Toolkit 内的 `aarch64-target-linux-gnu-g++/gcc`，实现在 x86 机器上编出 aarch64 包。

**两条互斥装配线**：

[CMakeLists.txt:101-112](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L101-L112) `ENABLE_TEST` 分支：开启 testing、引入 mockcpp/gtest/rdma-core 三方件、只编译 `test/` 子目录——UT 构建根本不编 `src/` 产品代码本体（产品代码在测试里以源码方式编入用例工程）。

[CMakeLists.txt:113-144](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L113-L144) `BUILD_OPEN_PROJECT` 分支：引入 `version.cmake`、`package.cmake`，下载 json/openssl/rdma-core 等三方件，然后 `find_cann_package` 一次性查找 runtime、securec、mmpa、tsdclient 等 12 个 CANN 组件（缺一个配置就失败），最后把 `src/` 和 `python/` 加入编译。

**头文件安装规则**：这一段正好印证上一讲的「头文件分层」：

[CMakeLists.txt:151-192](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L151-L192) 把 `include/hccl/` 下的通信域/资源头文件装到 `hccl/` 目录，把 `hcomm_primitives.h`、`hcomm_res.h`、`hcomm_channel.h` 等 L2/L3 层新接口头文件装到 `hcomm/` 目录。

[CMakeLists.txt:212-243](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L212-L243) 把 `include/ccu/` 下 16 个 CCU 编程模型头文件与 `pkg_inc/hcomm/ccu/ccu_primitives_impl.h` 也安装进包里，供 CCU 通信算子开发者使用。

**架构相关打包**：

[cmake/package.cmake:56-67](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/cmake/package.cmake#L56-L67) `pack_built_in` 检测 `CMAKE_SYSTEM_PROCESSOR` 是 x86_64 还是 aarch64，后续据此生成 `cann-hcomm_<version>_linux-<arch>.run` 命名——这就是 build.md 里说的 `./build_out` 产物的来源。

#### 4.2.4 代码实践

**实践目标**：弄清「一个头文件从源码树到安装包」的路径。

1. 操作步骤：
   - 在 [CMakeLists.txt:151-255](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L151-L255) 中找到 `include/hccl/hccl_comm.h`、`include/hcomm_primitives.h`、`include/ccu/ccu_primitives.hpp` 三个文件各自被装到哪个 `DESTINATION`。
   - 回答：如果用户代码要 `#include "hcomm/hcomm_channel.h"`，需要链接 run 包安装后的哪个 include 子目录？
2. 需要观察的现象：三套安装规则按 `hccl/`、`hcomm/`、`hcomm/ccu/`、`hccl/`(pkg_inc) 四类目的地分组。
3. 预期结果：能画出「源码路径 → 安装路径」对照表；理解 pkg_inc（包间接口）与 include（对外接口）安装位置不同。
4. 本实践为纯源码阅读，可直接完成，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ENABLE_TEST=ON` 时 CMake 不编译 `add_subdirectory(src)`？

答案：UT 装配线（[CMakeLists.txt:101-112](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L101-L112)）的目标是编出测试可执行文件，测试工程通过自己的 CMake 以特定方式（含 stub/mock）编入被测源码，如果同时再链一份产品库会造成符号冲突与依赖膨胀，因此两条线互斥。

**练习 2**：`-j` 并行度在 CMake 层还有一层控制，在哪里？

答案：[CMakeLists.txt:70-93](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L70-L93) 用 `nproc` 取 CPU 核数并乘以 2 设置 `CMAKE_JOB_POOL_COMPILE/LINK`，与 build.sh 的 `-j` 叠加控制编译/链接作业池。

**练习 3**：`CMAKE_CXX_FLAGS_RELEASE` 被显式置空（[CMakeLists.txt:62-63](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L62-L63)），推测原因。

答案：CMake 默认会给 Release 注入 `-O3 -DNDEBUG`，项目置空后改由 CANN 的 cmake 基础设施（`init_cann_project` / `add_cann_target_options`，[CMakeLists.txt:56-57](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L56-L57)）统一注入优化与告警选项，避免默认 flags 与之冲突。

### 4.3 version.cmake：源码版本与 CANN 配套关系

#### 4.3.1 概念说明

HCOMM 不是独立发行的：它编出的 run 包会**替换**已安装 CANN Toolkit 里的 HCOMM 组件。因此「哪个 HCOMM 源码配哪个 CANN」必须严格配套。`version.cmake` 用 CANN cmake 基础设施提供的函数把这套配套关系声明成数据，构建时会据此做依赖检查。

#### 4.3.2 核心流程

```text
set_cann_package(hcomm VERSION "9.2.0")          # 本包版本 9.2.0
set_cann_build_dependencies(<组件> "CUR_MAJOR_MINOR_VER")   # 编译期依赖
set_cann_run_dependencies(<组件> "CUR_MAJOR_MINOR_VER")     # 运行期依赖
```

其中 `CUR_MAJOR_MINOR_VER` 是一个占位语义，表示「与 HCOMM 当前主次版本一致的 CANN 版本」。构建时 [CMakeLists.txt:138](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L138) 的 `check_cann_pkg_build_deps("hcomm")` 会校验本机 CANN 组件版本是否满足这些声明，不满足则构建失败。

#### 4.3.3 源码精读

[version.cmake:11-19](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/version.cmake#L11-L19) 声明：HCOMM 版本为 9.2.0；编译期依赖 runtime、metadef、bisheng-compiler、asc-devkit 四个组件；运行期依赖 runtime、metadef。全部要求与 9.2.x 同主次版本。

配套关系还体现在官方文档里：build.md 指出源码分支标签与 CANN 版本的对应关系要查 [release-management 仓库](https://gitcode.com/cann/release-management)，且发布版仅支持 CANN 8.5.0 及后续版本（[docs/zh/build/build.md:59-61](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/build/build.md#L59-L61)）。

此外，上板测试还有一个容易忽略的配套坑：自编译包里的 device 侧 tar.gz 子包没有签名头，必须用 Ascend HDK 25.5.T2.B001 及以上版本的 `npu-smi` 关闭驱动安全验签后才能加载（[docs/zh/build/build.md:170-186](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/build/build.md#L170-L186)）。

#### 4.3.4 代码实践

**实践目标**：确认你本地 CANN 与当前 HCOMM 源码版本是否配套。

1. 操作步骤：
   - `cat /usr/local/Ascend/cann/<arch>-linux/ascend_toolkit_install.info` 查看 CANN 版本（非 root 用户路径在 `${HOME}/Ascend`）。
   - 与 `version.cmake` 中的 `9.2.0` 比对主次版本号。
   - 若不一致，按 build.md 指引从 master 镜像或官网下载配套版本，或切换到配套的 HCOMM tag：`git clone -b ${tag_version} https://gitcode.com/cann/hcomm.git`。
2. 需要观察的现象：版本主次号是否一致；不一致时构建在 `check_cann_pkg_build_deps` 处会报什么错。
3. 预期结果：能独立完成「查配套表 → 选源码分支 → 装 CANN」的环境对齐流程。
4. 无 CANN 环境时标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：编译期依赖和运行期依赖的区别在本文件中如何体现？举例说明。

答案：`set_cann_build_dependencies` 列出的 bisheng-compiler、asc-devkit 只在编译时需要（编译器与开发套件），装好 run 包后的运行环境不需要它们；而 `set_cann_run_dependencies` 列出的 runtime、metadef 在目标机运行时仍必须存在。

**练习 2**：你在 CANN 8.5.0 环境下编译 master 分支的 HCOMM，最可能发生什么？

答案：master 分支声明版本 9.2.0，与 8.5.0 主次版本不一致，`check_cann_pkg_build_deps` 依赖检查不通过，配置阶段失败。应按 release-management 配套表换用与 8.5.0 配套的 HCOMM tag，或升级 CANN。

## 5. 综合实践

**任务：完成一次「构建路径对比」实验并产出构建档案。**

在有 CANN 环境的机器（或容器）上：

1. 按本讲 4.3.4 对齐 CANN 与源码版本，`source set_env.sh`。
2. 执行 `bash build.sh --pkg`，记录：配置阶段打印的 `ASCEND_CANN_PACKAGE_PATH`/`ENABLE_BUILD_DEVICE` 等状态行、编译耗时、`./build_out` 下生成的 run 包完整文件名。
3. 执行 `bash build.sh --pkg --full`，对比多出的 device 侧编译步骤（注意它需要 `TOOLCHAIN_DIR` 指向的 hcc 工具链存在）。
4. 执行 `bash build.sh -u --noexec`，确认它走 `build_ut` 路径、ctest 被跳过，并对比 `build_ut/` 与 `build/` 两个构建目录内容的差异（UT 目录下是测试可执行文件，无 run 包产物）。
5. 把上述记录整理成一张表：命令 / 走的构建函数 / 关键 -D 选项 / 产物路径。

没有昇腾硬件时，第 2、3 步标记「待本地验证」，第 4 步若因缺少 CANN 组件无法完成也标记「待本地验证」，但表中的「命令 → 构建函数 → -D 选项」三列可以纯读源码填出。

## 6. 本讲小结

- `build.sh` 是唯一构建入口：解析参数 → 探测 CANN 路径 → 拼 `-D` 选项 → 按「UT > ST > 正式包」优先级分发。
- UT（`-u`）与正式包（默认）走两条互斥的 CMake 装配线：UT 只编 `test/` 且用 gtest+mockcpp，正式包走 `BUILD_OPEN_PROJECT` 分支编 `src/` + `python/` 并打 run 包。
- `--noexec` 让「编译测试」与「执行测试」解耦，是无硬件环境验证可编译性的关键开关。
- 默认只编 host 侧；`--full`/`--aicpu` 加编 device 侧，`--build_aarch` 用 CANN 自带 hcc 工具链交叉编译 aarch64 包。
- 产物是 `./build_out/cann-hcomm_<version>_linux-<arch>.run`，安装后会替换 CANN Toolkit 中的 HCOMM 组件。
- `version.cmake` 声明 HCOMM 9.2.0 与 CANN 组件（runtime、metadef、bisheng-compiler、asc-devkit）的版本配套，构建前必须对齐。

## 7. 下一步学习建议

下一讲（u1-l3「目录结构与代码地图」）将深入 `include/`、`pkg_inc/`、`src/` 三棵目录树，把本讲在 CMakeLists 里看到的头文件安装规则与实际源码组织对应起来。在此之前，建议你打开 `build.sh` 的 usage 和 `cmake/` 目录下的 `config.cmake`、`func.cmake` 通读一遍，感受 shell 层与 CMake 层的分工。
