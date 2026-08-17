# u1-l2 环境搭建与源码编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 装好 CANN Runtime 源码编译所需的全部基础依赖（python、gcc、cmake、ccache 等），并安装配套的 CANN toolkit 包。
2. 读懂 `build.sh` 这个编译总入口：它解析了哪些参数、内部又是如何调用 CMake 的。
3. 读懂顶层 `CMakeLists.txt`：第三方依赖如何接入、`src/` 下哪些模块被编译、UT 与打包分别在什么条件下开启。
4. 理解「联网自动下载」与「离线 `--cann_3rd_lib_path`」两套第三方依赖获取方式背后的源码机制。
5. 独立完成一次完整编译，在 `build_out` 下得到 `cann-npu-runtime_<version>_linux-<arch>.run` 软件包，并安装、配置 `set_env.sh` 环境变量完成验证。

## 2. 前置知识

在学习本讲之前，你需要先完成 u1-l1，知道这个仓库分为 acl 接口层、runtime 核心层和驱动适配层。本讲要解决的问题是：**这一大堆源码如何变成一个可以安装、可以替换进 CANN 包的软件包？**

几个初学者可能陌生的概念，先用大白话解释：

- **CMake**：C/C++ 世界的「构建脚本生成器」。它本身不编译代码，而是根据 `CMakeLists.txt` 里的描述，生成对应平台的 Makefile，再由 `make` 去真正编译。本仓顶层入口就是 [CMakeLists.txt](CMakeLists.txt)。
- **run 包**：华为 CANN 软件的自解压安装包，后缀为 `.run`。执行 `./xxx.run --install` 即可安装，底层基于开源工具 makeself 打包。我们编译源码的最终产物就是一个 run 包。
- **交叉编译 / Device 侧**：runtime 库既要在 Host（服务器 CPU，x86_64 或 aarch64）上运行，也有面向 Device（昇腾 NPU 上小系统）的组件。当存在交叉编译工具链（`TOOLCHAIN_DIR`）时，构建系统会额外编译 Device 侧目标。
- **第三方依赖（third_party）**：编译 runtime 需要 absl、boost、protobuf、googletest 等开源库。仓库不直接携带它们，而是在编译时下载或从指定目录读取。
- **`set_env.sh`**：CANN 安装后自带的环境变量脚本，`source` 它之后，`ASCEND_HOME_PATH`、`LD_LIBRARY_PATH` 等变量就位，编译脚本才能找到已安装的 CANN 头文件和库。
- **ccache**：编译缓存工具，重复编译时可以大幅加速，是本仓基础依赖之一。

一句话概括本讲的编译链路：

```
install_deps.sh（装基础工具）→ 安装 CANN 包 + set_env.sh（提供头文件/库）
  → download_3rd_party.py（离线时预下载开源库）
  → bash build.sh（入口：解析参数 → cmake 配置 → cmake --build → make package）
  → CMakeLists.txt（拉第三方、编译 src/ 各模块、CPack 打包）
  → build_out/cann-npu-runtime_<version>_linux-<arch>.run（产物）
  → 安装 run 包替换已装 CANN 中的 Runtime 组件
```

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [README.md](README.md) | 环境、编译、安装、UT 验证的官方说明书，本讲多处的「第一手依据」 |
| [install_deps.sh](install_deps.sh) | 基础依赖自动安装脚本：检测发行版并安装 python/gcc/cmake/ccache 等 |
| [build.sh](build.sh) | 编译总入口：参数解析 + 三步 CMake 调用（configure/build/package） |
| [CMakeLists.txt](CMakeLists.txt) | 顶层 CMake 配置：接入第三方、决定构建类型、进入 `src/`、开启 UT 与打包 |
| [download_3rd_party.py](download_3rd_party.py) | 离线场景下从 README 表格提取 URL，预下载第三方压缩包到 `third_party/` |
| [version.cmake](version.cmake) | 声明包名 `npu-runtime` 与版本号 `9.1.0` |
| [src/CMakeLists.txt](src/CMakeLists.txt) | 按模块逐一 `add_subdirectory`，并在非 UT 构建时引入打包逻辑 |
| [cmake/fetch_cann_cmake.cmake](cmake/fetch_cann_cmake.cmake) | 三级优先级获取 cann-cmake 构建框架（本地目录 → 本地 tar → 在线 git） |
| [cmake/third_party/acl_compat.cmake](cmake/third_party/acl_compat.cmake) | acl-compat 兼容层的获取：本地 tar 优先，否则在线下载 |
| [tests/build_ut.sh](tests/build_ut.sh) | UT 编译入口，含模块名 → 用例路径映射表 `ut_path_map` |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：依赖安装（install_deps.sh）、编译入口（build.sh）、顶层构建逻辑（CMakeLists.txt）、第三方依赖获取、产物安装与环境变量。

### 4.1 基础依赖安装：install_deps.sh

#### 4.1.1 概念说明

编译 runtime 之前，机器上必须先有「编译工具链」：python 3（很多构建脚本用 python 写）、gcc/g++（C/C++ 编译器）、cmake（构建系统）、ccache（编译缓存）、autoconf/gperf/libtool/make（构建辅助工具）。`install_deps.sh` 就是把这些工具一次性装好的自动化脚本，它最大的特点是**先检测、不满足才安装**，并且能识别 debian/rhel/euler/macos 四类系统选择对应的包管理器。

#### 4.1.2 核心流程

```
main()
 ├── detect_os            # 判断发行版 → 选 apt / dnf / yum / brew
 ├── install_python       # 检查 python3 >= 3.7.0，不满足则安装
 ├── install_pip3
 ├── install_gcc          # 检查 gcc >= 7.3.0，不满足则安装
 ├── install_cmake        # 检查 cmake >= 3.16.0，不满足则安装
 ├── install_ccache
 ├── install_autoconf
 ├── install_gperf
 ├── install_libtool
 └── install_make
```

每个 `install_xxx` 函数的套路一致：`command -v xxx` 判断是否已安装 → 用 `version_ge` 比较版本 → 不满足则按发行版分支执行安装命令 → 复检版本，失败则退出。

#### 4.1.3 源码精读

先看版本比较函数——它把 `3.9.0` 这样的版本号按 `.` 切开逐段比较，是所有依赖检查的基础：

[install_deps.sh:L28-L43](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/install_deps.sh#L28-L43)
这段代码实现 `version_ge 当前版本 要求版本`：逐段比较，当前段大于要求段即满足（返回 0），小于即不满足，全部相等也算满足。

再看操作系统检测：

[install_deps.sh:L45-L80](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/install_deps.sh#L45-L80)
这段代码用 `uname -s` 和 `/etc` 下的发行版特征文件区分 debian（有 `/etc/debian_version`，用 apt）、rhel（有 `/etc/redhat-release`，优先 dnf 否则 yum）、euler（`/etc/os-release` 里 NAME 为 openEuler/EulerOS）、macos（Darwin，用 brew）；都不匹配则提示手动安装并退出。

最后看安装总调度：

[install_deps.sh:L445-L464](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/install_deps.sh#L445-L464)
`main` 按固定顺序完成 detect_os 与九项依赖的检查安装，任何一步失败脚本都会因为开头的 `set -euo pipefail` 立即中止。

对应的依赖版本要求在 README 中有明文列表：

[README.md:L64-L75](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L64-L75)
README 列出基础依赖：python >= 3.7.0（官方建议升到 >= 3.9.0）、pip3、gcc >= 7.3.0 且 <= 13、cmake >= 3.16.0、ccache、autoconf、gperf、libtool、make、libc6-dev/glibc-devel。注意脚本里的 `req_ver="3.7.0"`（install_deps.sh L85 附近）与 README 一致，但 README 额外提示 python3.7/3.8 已 EOL。

#### 4.1.4 代码实践

1. **实践目标**：确认你的编译机依赖是否齐全，缺口由脚本补上。
2. **操作步骤**：
   ```bash
   # 方式一：自动安装（需要 sudo 权限）
   bash install_deps.sh

   # 方式二：手动核对（Ubuntu/Debian 示例，来自 README）
   sudo apt install python3 python3-pip python3-dev gcc-9 g++-9 libc6-dev cmake ccache autoconf gperf libtool libtool-bin make

   # 逐项自查版本
   python3 --version; gcc --version | head -1; cmake --version | head -1; ccache --version | head -1
   ```
3. **需要观察的现象**：脚本输出中每一节 `==== 检查XXX ====` 后面跟的是「已安装/版本满足要求」还是触发了安装命令。
4. **预期结果**：脚本末尾打印「所有依赖安装完成！」；`gcc --version` >= 7.3.0、`cmake --version` >= 3.16.0。本讲在无昇腾设备的 CI 容器中未实际执行安装，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`install_deps.sh` 里 `install_gcc` 要求的最低版本是多少？如果当前 gcc 是 7.2.0 会发生什么？

答案：要求 >= 7.3.0（见 [install_deps.sh:L136-L150](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/install_deps.sh#L136-L150) 中 `req_ver="7.3.0"`）。gcc 7.2.0 时 `version_ge` 判定不满足，脚本会按发行版分支安装新 gcc（如 debian 装 gcc-9 并用 `update-alternatives` 切换默认版本），装完复检，仍不满足则 `exit 1`。

**练习 2**：为什么 `run_command` 要把命令输出捕获后统一打印，而不是让命令直接输出？

答案：见 [install_deps.sh:L14-L26](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/install_deps.sh#L14-L26)。它用 `output=$("$@" 2>&1)` 捕获 stdout+stderr，成功时不刷屏、失败时统一打印「失败的命令 + 错误输出 + 退出码」，让排错信息集中、日志干净。

### 4.2 编译总入口：build.sh

#### 4.2.1 概念说明

`build.sh` 是整个仓库的编译入口，你在 README 里看到的 `bash build.sh` 就是指向它。它本质上只做三件事：

1. **解析命令行参数**（`checkopts` 函数）：如线程数、构建类型、第三方库路径、是否开 ASAN/覆盖率。
2. **拼装 CMake 参数并调用 CMake**（`build_rts` 函数）：configure → build → package 三步。
3. **一个 CI 优化**：如果传入的变更文件列表只涉及文档/测试等目录，可以直接跳过编译。

理解它之后，「编译 runtime」对你来说就不再是一个黑盒命令。

#### 4.2.2 核心流程

```
bash build.sh [--cann_3rd_lib_path=PATH] [-jN] [--build-type=Release|Debug] [--asan] [--cov] ...
        │
        ▼
main() ──► checkopts()          # 1. 设默认值 2. getopt 解析 3. 覆盖默认值
        │
        ▼
build_rts() ──► mk_dir build/ build_out/
        │
        ├── cmake -S ../ -B . <一堆 -D 参数>     # 配置
        ├── cmake --build . -j${THREAD_NUM}      # 编译
        └── make package -j${THREAD_NUM}         # 打包 → build_out/*.run
```

#### 4.2.3 源码精读

先看脚本头部的路径约定——两个最重要的输出目录在这里定义：

[build.sh:L12-L15](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L12-L15)
这段代码设置 `set -e`（任何命令失败立即退出），把仓库根目录记为 `BASEPATH`，并约定：`build_out` 是最终产物目录（run 包落这里），`build` 是 CMake 的构建目录（过程件）。

再看参数默认值：

[build.sh:L50-L70](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L50-L70)
这段代码是 `checkopts` 的开头，确定各参数默认值：线程数取 `/proc/cpuinfo` 里的处理器个数；第三方库路径默认 `output/third_party`；构建类型默认 `Release`；包类型默认 `run`。关键是 `ASCEND_INSTALL_PATH` 的三级取值逻辑——**环境变量 `ASCEND_INSTALL_PATH` > 环境变量 `ASCEND_HOME_PATH` > `/usr/local/Ascend/cann`**。这就是为什么先 `source set_env.sh`（它会导出 `ASCEND_HOME_PATH`）能让 build.sh 自动找到已安装的 CANN。

参数解析用的是 bash 标准的 `getopt`：

[build.sh:L81-L85](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L81-L85)
这段代码用 `getopt -a` 声明短选项（`-j`、`-h`、`-v`、`-f`）和全部长选项（`--asan`、`--cov`、`--cann_3rd_lib_path:` 等，冒号表示该选项带参数），解析失败则打印用法并退出。随后的 while/case 循环（[build.sh:L87-L170](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L87-L170)）逐项把选项写入对应变量，例如 `--cann_3rd_lib_path` 在 [build.sh:L131-L134](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L131-L134) 中被 `realpath` 转成绝对路径后存入 `ASCEND_3RD_LIB_PATH`。

最核心的是编译三步曲：

[build.sh:L260-L300](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L260-L300)
`build_rts` 先创建 `build`、`build_out` 两个目录，然后：
- L265-L279 把所有选项拼成 `CMAKE_ARGS`（注意 `-DENABLE_OPEN_SRC=True` 标记开源构建，`-DCMAKE_INSTALL_PREFIX=${OUTPUT_PATH}` 指向 build_out，`-DCANN_3RD_LIB_PATH` 传入第三方库路径）；
- L282 `cmake -S ../ -B .` 以仓库根为源码目录、`build` 为二进制目录做配置（等价于老写法的 `cd build && cmake ..`）；
- L288 `cmake --build . -j${THREAD_NUM}` 并行编译；
- L294 `make package` 触发 CPack 打包，生成 run 包；
- 任一步返回非 0 都打印失败信息并返回错误码。

`main` 的编排逻辑：

[build.sh:L302-L322](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L302-L322)
`main` 先 `checkopts` 解析参数；若通过 `-f` 传入了变更文件列表且全部属于 docs/example/tests/.claude/.opencode/Markdown（见 [build.sh:L173-L251](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L173-L251) 的 `check_changed_files`），则直接 `exit 200` 跳过编译——这是给 CI 省时间的机制；否则打印 `g++ -v` 记录编译器信息后进入 `build_rts`。

#### 4.2.4 代码实践

1. **实践目标**：不真正编译，先读懂并验证 build.sh 的参数解析行为。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   bash build.sh -h          # 只打印帮助，立即退出
   grep -c ^processor /proc/cpuinfo   # 查看你机器的 CPU 核数
   ```
   然后做一个无害实验：`bash build.sh --cann_3rd_lib_path=/tmp/not_exist_dir -j2`，观察它在哪一步报错。
3. **需要观察的现象**：`-h` 输出的选项列表与本讲 4.2.3 引用的 usage 函数（[build.sh:L18-L47](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L18-L47)）逐条对应；错误实验中能看到 `CMAKE_ARGS=...` 的完整回显，随后在 cmake 配置或第三方查找阶段失败。
4. **预期结果**：能说出 `-j`、`--build-type`、`--cann_3rd_lib_path`、`--asan`、`--build_host_only` 各自的作用。完整编译在本讲环境中未执行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 强调编译前要 `source /usr/local/Ascend/cann/set_env.sh`？从 build.sh 源码找出依据。

答案：`set_env.sh` 会导出 `ASCEND_HOME_PATH`；[build.sh:L64-L70](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L64-L70) 中，当用户没显式传 `--ascend_install_path` 且没设 `ASCEND_INSTALL_PATH` 时，`ASCEND_HOME_PATH` 会被用作 `ASCEND_INSTALL_PATH`，CMake 用它定位已安装 CANN 的头文件与库。此外 [build.sh:L73-L78](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L73-L78) 还会在 `ASCEND_HOME_PATH/toolkit/toolchain/hcc` 存在时导出 `TOOLCHAIN_DIR`，用于 Device 侧编译。

**练习 2**：`build` 和 `build_out` 两个目录的区别是什么？

答案：见 [build.sh:L14-L15](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L14-L15)。`build`（BUILD_PATH）是 CMake 的构建目录，存放过程件（对象文件、临时库）；`build_out`（OUTPUT_PATH）通过 `-DCMAKE_INSTALL_PREFIX` 指定为安装/打包前缀，`make package` 产出的 `cann-npu-runtime_<version>_linux-<arch>.run` 最终放在这里（README [L206-L208](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L206-L208)）。

**练习 3**：如果只想编译 Host 侧目标（不编 Device 侧），该传什么参数？它在源码里如何生效？

答案：传 `--build_host_only`。[build.sh:L112-L115](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L112-L115) 把 `ENABLE_BUILD_DEVICE` 置为 `OFF`，该值经 `-DENABLE_BUILD_DEVICE` 传给 CMake，在顶层 [CMakeLists.txt:L74-L79](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L74-L79) 中只有 `ENABLE_BUILD_DEVICE` 为真且存在 `TOOLCHAIN_DIR` 环境变量时才会开启 Device 工程。

### 4.3 顶层构建逻辑：CMakeLists.txt

#### 4.3.1 概念说明

`build.sh` 只是「外壳」，真正描述「编什么、怎么编」的是顶层 [CMakeLists.txt](CMakeLists.txt)。它定义了整个工程的骨架：

- 引入 cann-cmake 构建框架（提供 `add_cann_third_party`、`set_cann_cpack_config` 等函数）；
- 决定构建类型（Release/Debug）与是否编译 UT；
- 接入第三方依赖；
- 进入 `src/` 编译所有子模块；
- 满足条件时配置 CPack 打包。

#### 4.3.2 核心流程

```
CMakeLists.txt
 ├── 1. 引入 cann-cmake 框架（fetch_cann_cmake.cmake）
 ├── 2. set_runtime_params：工程基础参数
 ├── 3. 构建类型：ENABLE_COV/ENABLE_UT → Debug，否则 Release
 ├── 4. 第三方：acl_compat（兼容层）+ json + csec + protobuf
 ├── 5. version.cmake：包名 npu-runtime、版本 9.1.0、版本头生成
 ├── 6. add_subdirectory(src)：编译全部子模块
 ├── 7. ENABLE_COV 或 ENABLE_UT 时：追加 gtest/mockcpp/boost + add_subdirectory(tests)
 ├── 8. 有 TOOLCHAIN_DIR 且 ENABLE_BUILD_DEVICE 时：开启 Device 侧工程
 └── 9. 非 UT/Cov 构建：set_cann_cpack_config 配置 CPack 打包
```

#### 4.3.3 源码精读

看工程初始化：

[CMakeLists.txt:L11-L20](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L11-L20)
这段代码要求 cmake >= 3.14（注意与 README 依赖表里的 >= 3.16.0 略有出入，README 是对系统 cmake 的要求，这里是对 CMake 脚本语法的最低要求）；先 include `fetch_cann_cmake.cmake` 再 `project(rts)`；随后 `init_cann_project()`、`add_cann_target_options()` 来自 cann-cmake 框架（后述 4.4）；最后把仓库根目录缓存为 `RUNTIME_DIR` 并 include [cmake/func.cmake](cmake/func.cmake) 里的 `set_runtime_params` 宏（设置 `CMAKE_SKIP_RPATH`、python 路径等基础参数，见 [cmake/func.cmake:L166-L177](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/func.cmake#L166-L177)）。

看构建类型与第三方接入：

[CMakeLists.txt:L22-L44](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L22-L44)
这段代码先确定默认构建类型——开了覆盖率（ENABLE_COV）或 UT（ENABLE_UT）就用 Debug，否则 Release；然后接入第三方依赖：include acl_compat（昇腾老版本 acl 兼容头文件与库，细节见 4.4），再用框架提供的 `add_cann_third_party` 声明 json、csec（即 libboundscheck 安全函数库）、protobuf 三个依赖。

看版本与源码入口：

[CMakeLists.txt:L46-L51](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L46-L51)
这段代码 include [version.cmake](version.cmake)，其中只有一行关键语句（[version.cmake:L12](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/version.cmake#L12)）：`set_cann_package(npu-runtime VERSION "9.1.0")`，声明包名与版本号——你最终在 `build_out` 看到的 `cann-npu-runtime_9.1.0...run` 里的版本字符串就来自这里；`check_cann_pkg_build_deps` 校验构建依赖；`add_subdirectory(src)` 则进入源码世界。

`src/CMakeLists.txt` 决定了每个模块的编译顺序：

[src/CMakeLists.txt:L12-L31](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/CMakeLists.txt#L12-L31)
这段代码按依赖关系逐一 `add_subdirectory`：aicpu_sched、queue_schedule、mmpa（跨平台适配层）、log/trace/error_manager/msprof/adump（维测组件）、tsd、platform、runtime（核心）、acl/aclrt_impl、acl/aclrt（对外 API）、acl_tdt_queue、acl_tdt_channel、tprt——这正是 u1-l1 讲过的「acl 层 → runtime 核心层 → 维测组件」在构建系统中的体现。

UT 与打包的条件分支：

[CMakeLists.txt:L53-L83](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L53-L83)
这段代码有三个关键分支：① `ENABLE_COV` 或 `ENABLE_UT` 时才把 gtest、mockcpp、boost 等 UT 依赖加入构建，并 `add_subdirectory(tests)`——所以 `bash build.sh` 默认不会编 UT，UT 要用 `tests/build_ut.sh`（其 `--ut`/`--target` 与模块路径映射见 [tests/build_ut.sh:L17-L35](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/tests/build_ut.sh#L17-L35) 的 `ut_path_map`/`ut_name_map`）；② 存在 `TOOLCHAIN_DIR` 环境变量且 `ENABLE_BUILD_DEVICE` 为真时开启 Device 交叉编译工程；③ 非覆盖率/UT 构建时调用 `set_cann_cpack_config` 配置 CPack——`make package` 能产出 run 包的能力就来自这里（打包细节封装在 cann-cmake 框架中，`src/CMakeLists.txt` 末尾的 [L42-L43](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/CMakeLists.txt#L42-L43) 也 include 了本仓的 [cmake/package.cmake](cmake/package.cmake)）。

#### 4.3.4 代码实践

1. **实践目标**：把「命令行参数 → CMake 变量 → 构建行为」这条链亲手对上号。
2. **操作步骤**：
   ```bash
   # 只做配置不编译（快），观察 cmake 输出
   cd <仓库根目录>
   mkdir -p /tmp/rt-cfg && cd /tmp/rt-cfg
   cmake -S <仓库根目录> -B . -DENABLE_OPEN_SRC=True -DCMAKE_BUILD_TYPE=Release \
         -DCANN_3RD_LIB_PATH=<你的第三方路径或output/third_party>
   # 配置成功后查看缓存里关键变量的取值
   grep -E "ENABLE_UT|ENABLE_BUILD_DEVICE|CMAKE_BUILD_TYPE|CANN_3RD_LIB_PATH" CMakeCache.txt
   ```
   如果没有第三方依赖可先跳过配置，改用纯阅读：在 [CMakeLists.txt](CMakeLists.txt) 中搜索 `ENABLE_UT`、`ENABLE_BUILD_DEVICE`，画出条件分支表。
3. **需要观察的现象**：`CMakeCache.txt` 中 `ENABLE_UT` 为空/OFF 时构建类型是 Release；`ENABLE_BUILD_DEVICE` 的值与是否设置 `TOOLCHAIN_DIR` 环境变量的关系。
4. **预期结果**：能回答「为什么 `bash build.sh` 默认不会编译 tests/ 目录」。完整配置依赖第三方库，本讲环境中**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`add_subdirectory(src)` 之后，最终生成的动态库对应哪些模块？

答案：由 [src/CMakeLists.txt:L12-L31](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/CMakeLists.txt#L12-L31) 决定，涵盖 runtime 核心（`src/runtime`）、acl 对外接口（`src/acl/aclrt` 等）、维测组件（`src/dfx` 下的 log/trace/error_manager/msprof/adump）、mmpa、tsd、platform、tprt 等；它们最终被 CPack 组装进 run 包。

**练习 2**：为什么 UT 依赖（gtest、mockcpp、boost）不在主构建里常驻？

答案：见 [CMakeLists.txt:L53-L72](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/CMakeLists.txt#L53-L72)。只有 `ENABLE_COV` 或 `ENABLE_UT` 打开时才 `add_cann_third_party` 这些测试框架并进入 `tests/`，这样日常出包构建更轻、更快，也减少对测试依赖的下载要求。

### 4.4 第三方依赖获取：联网与离线两套机制

#### 4.4.1 概念说明

README 列出了 14 个开源第三方依赖（abseil、boost、protobuf、googletest、makeself、cann-cmake 等）。仓库对它们的获取策略是「**本地优先、在线兜底**」：

- **联网环境**：直接 `bash build.sh`，CMake 的 ExternalProject/FetchContent 机制会自动下载。
- **离线环境**：先在有网的机器上执行 `python download_3rd_party.py`，把压缩包下载到 `third_party/` 目录，再 `bash build.sh --cann_3rd_lib_path=third_party`。

这一机制有两处典型源码：cann-cmake 框架的获取（fetch_cann_cmake.cmake）和 acl-compat 的获取（acl_compat.cmake）。

#### 4.4.2 核心流程

```
CMake 需要第三方组件 X
   │
   ├─ ① CANN_3RD_LIB_PATH 下已有解压好的 X 目录？ ──► 直接使用（最快）
   ├─ ② CANN_3RD_LIB_PATH 下有 X 的 tar.gz？     ──► 校验后本地解压使用
   └─ ③ 都没有                                    ──► 在线下载（git/https）
```

#### 4.4.3 源码精读

先看 cann-cmake 构建框架的三级获取：

[cmake/fetch_cann_cmake.cmake:L12-L33](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/fetch_cann_cmake.cmake#L12-L33)
这段代码实现三级优先：① `${CANN_3RD_LIB_PATH}/cann-cmake` 目录存在则直接 include 其 `function/prepare.cmake`；② 存在本地 `cmake-master-049.tar.gz`（带 SHA256 校验）则 FetchContent 解压使用；③ 否则从 `https://gitcode.com/cann/cmake.git` 拉取 tag `master-049`。顶层 `CMakeLists.txt` 里那些 `add_cann_third_party`、`set_cann_cpack_config`、`init_cann_project` 函数全部来自这个框架——这正是 README 第三方表中「cann-cmake master-049」一行存在的意义。

再看 acl-compat 的「本地 glob 优先、在线 URL 兜底」：

[cmake/third_party/acl_compat.cmake:L17-L23](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/third_party/acl_compat.cmake#L17-L23)
这段代码用 `file(GLOB)` 在 `CANN_3RD_LIB_PATH` 下查找本机的 `acl-compat_*_linux-<架构>.tar.gz`，找到就作为离线源；找不到则回退到华为云 OBS 地址在线下载。下载后由紧随其后的 `_copy_acl_headers_and_libs` 目标（[cmake/third_party/acl_compat.cmake:L37-L45](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/third_party/acl_compat.cmake#L37-L45)）把头文件和库拷到构建目录的 `include_acl`/`lib_acl`。

最后看离线下载脚本如何拿到 URL 列表：

[download_3rd_party.py:L42-L56](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/download_3rd_party.py#L42-L56)
这段代码是个很聪明的做法：**直接解析 README.md 的第三方依赖表格**——找到「| 开源软件」开头的表格，用正则 `\]\((https?://[^\)]+)\)` 抽取每行 Markdown 链接里的下载 URL。

[download_3rd_party.py:L66-L82](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/download_3rd_party.py#L66-L82)
`main` 在当前目录创建 `third_party/`，把所有 URL 对应文件下载进去（已存在则跳过，见 [L25-L31](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/download_3rd_party.py#L25-L31) 的 `download_file`）。所以 README 说「脚本将自动下载至当前新建的 third_party 目录中」。

README 中对应的官方说明：

[README.md:L186-L208](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L186-L208)
README 明确了两条路径：联网直接 `bash build.sh`；离线先 `python download_3rd_party.py` 再 `bash build.sh --cann_3rd_lib_path=third_party`，并说明产物为 `build_out` 下的 `cann-npu-runtime_<version>_linux-<arch>.run`。

#### 4.4.4 代码实践

1. **实践目标**：在不安装任何东西的情况下，验证 `download_3rd_party.py` 的 URL 解析逻辑。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   python3 - <<'EOF'
   # 示例代码：单独运行 parse_readme_for_urls，观察能抽到哪些 URL
   import importlib.util
   spec = importlib.util.spec_from_file_location("d3p", "download_3rd_party.py")
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   urls = m.extract_urls("README.md")
   print(f"共解析到 {len(urls)} 个下载地址：")
   for u in urls: print(" -", u)
   EOF
   ```
3. **需要观察的现象**：输出的 URL 数量与 README 第三方表格行数（14 个开源软件，其中 acl-compat 有 x86_64/aarch64 两个地址）是否对得上；每个文件名是否都能在表格里找到。
4. **预期结果**：解析到的 URL 全部指向 README 表格中的下载地址。真正执行 `python3 download_3rd_party.py` 会产生实际网络流量并新建 `third_party/` 目录，**待本地验证**（本讲环境未执行下载）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `--cann_3rd_lib_path` 传相对路径可能出问题？

答案：CMake 的部分机制（如 INTERFACE_INCLUDE_DIRECTORIES）不接受相对路径。本仓 `.devcontainer/README.md` 明确提示「如果第三方依赖在其他路径，必须使用绝对路径：`bash build.sh --cann_3rd_lib_path=$(pwd)/third_party`」；且 [build.sh:L131-L134](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/build.sh#L131-L134) 已经用 `realpath` 把它转成绝对路径兜底。

**练习 2**：构建框架 cann-cmake 的 tag 是什么？如果离线目录里既没有解压目录也没有 tar 包会发生什么？

答案：tag 是 `master-049`（[cmake/fetch_cann_cmake.cmake:L12](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/fetch_cann_cmake.cmake#L12)）。都没有时会走 [L25-L28](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/cmake/fetch_cann_cmake.cmake#L25-L28) 的 `GIT_REPOSITORY https://gitcode.com/cann/cmake.git` 在线拉取——离线环境若网络不通，配置阶段就会失败。

### 4.5 产物安装与环境变量：从 run 包到可用环境

#### 4.5.1 概念说明

编译产出 run 包后，需要把它安装到 CANN 目录，并用 `set_env.sh` 让环境变量生效。要理解一个关键点：**runtime run 包不是独立软件，而是「替换件」**——它会替换已安装 CANN 开发套件包中的 Runtime 相关软件。所以完整链路是：先装官方 CANN toolkit 包 → 编译本仓 → 安装自编译的 runtime run 包覆盖官方 Runtime。

#### 4.5.2 核心流程

```
① 安装 CANN toolkit 包（提供头文件/库/驱动接口，驱动固件仅运行样例时需要）
      ./Ascend-cann-toolkit_<version>_linux-<arch>.run --install --install-path=<path>
② source set_env.sh（导出 ASCEND_HOME_PATH、LD_LIBRARY_PATH 等）
③ bash build.sh（产出 build_out/cann-npu-runtime_<version>_linux-<arch>.run）
④ 安装自编译 run 包替换官方 Runtime
      cd build_out && ./cann-npu-runtime_<version>_linux-<arch>.run --full --install-path=<path>
⑤ 验证：npu-smi info + cat ascend_toolkit_install.info
```

#### 4.5.3 源码精读

CANN toolkit 包安装命令来自 README：

[README.md:L100-L110](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L100-L110)
README 给出 toolkit 包的标准安装方式：`chmod +x` 加执行权限后 `--install --install-path=${install_path}`，root 默认装到 `/usr/local/Ascend`。

环境变量配置：

[README.md:L175-L184](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L175-L184)
README 说明按安装路径选择 `source /usr/local/Ascend/cann/set_env.sh`（默认路径）或 `source ${install_path}/cann/set_env.sh`（指定路径）。source 之后 `ASCEND_HOME_PATH` 生效，上一节 build.sh 的默认值逻辑才能找到 CANN。

run 包安装与「替换」语义：

[README.md:L235-L246](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L235-L246)
README 说明安装命令 `./cann-npu-runtime_<version>_linux-<arch>.run --full --install-path=${install_path}`，并明确「安装完成之后，用户编译生成的 Runtime 软件包会替换已安装 CANN 开发套件包中的 Runtime 相关软件」。

环境验证：

[README.md:L142-L156](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L142-L156)
README 提供两个验证手段：`npu-smi info` 能正常显示设备信息说明驱动正常（仅编译不需要，运行样例需要）；`cat /usr/local/Ascend/cann/<arch>-linux/ascend_toolkit_install.info` 可查 CANN 版本。

补充：若不想污染本机环境，可以用容器方式构建，见 [.devcontainer/README.md](.devcontainer/README.md)（VS Code Dev Container，基于 Ubuntu 22.04 + GCC 9 + CMake 3.22，容器启动时自动下载第三方依赖并软链到 `output/third_party`）。

#### 4.5.4 代码实践

1. **实践目标**：走通「安装 → 环境变量 → 验证」闭环（无昇腾设备时只做前半段检查）。
2. **操作步骤**：
   ```bash
   # 1. 安装 CANN toolkit（已安装可跳过）
   chmod +x Ascend-cann-toolkit_${cann_version}_linux-${arch}.run
   ./Ascend-cann-toolkit_${cann_version}_linux-${arch}.run --install --install-path=${install_path}

   # 2. 环境变量生效
   source /usr/local/Ascend/cann/set_env.sh
   echo $ASCEND_HOME_PATH        # 应输出 /usr/local/Ascend/cann（默认路径时）

   # 3. 安装自编译 run 包（需先完成第 5 节综合实践的编译）
   cd build_out
   ./cann-npu-runtime_<version>_linux-<arch>.run --full --install-path=${install_path}

   # 4. 验证
   npu-smi info                                  # 有设备时
   cat /usr/local/Ascend/cann/<arch>-linux/ascend_toolkit_install.info
   ```
3. **需要观察的现象**：`echo $ASCEND_HOME_PATH` 是否有值；run 包安装日志中是否提示替换了 Runtime 组件；`npu-smi info` 是否列出 NPU 设备。
4. **预期结果**：环境变量就位、run 包安装成功、`npu-smi info` 正常显示设备信息。本讲编写环境无昇腾设备，`npu-smi info` 与 run 包安装**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：只编译 runtime 包不运行样例，哪些安装步骤可以省略？

答案：驱动/固件与 CANN ops 算子包都可以省略。README 在 [L90-L94](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L90-L94) 和 [L112-L114](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L112-L114) 两处标注「可选，仅运行样例依赖；若仅编译 runtime 包，可跳过本操作步骤」。

**练习 2**：安装自编译 run 包后，如何确认它真的替换了官方 CANN 里的 Runtime？

答案：思路是版本对比——run 包版本来自 [version.cmake:L12](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/version.cmake#L12) 的 `VERSION "9.1.0"`（master 分支源码），安装后可查看安装目录下 `ascend_toolkit_install.info`（README [L149-L156](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L149-L156)）中 version 字段，并对比安装目录 `lib64` 下 runtime 相关动态库（如 `libruntime.so`、`libascendcl.so`）的修改时间是否为刚安装的时间。

## 5. 综合实践

**任务：完成一次「依赖检查 → 编译 → 产物确认 → 安装验证」的完整闭环，并把每一步的证据记录下来。**

前置：一台 Ubuntu/openEuler 机器（有无 NPU 均可完成 1-3 步；第 4 步安装验证建议在有 NPU 的环境做）。

```bash
# 步骤 1：依赖体检（本讲 4.1）
bash install_deps.sh > deps.log 2>&1 && tail -3 deps.log
python3 --version && gcc --version | head -1 && cmake --version | head -1

# 步骤 2：准备环境变量（本讲 4.5）
source /usr/local/Ascend/cann/set_env.sh
echo "ASCEND_HOME_PATH=$ASCEND_HOME_PATH"

# 步骤 3：获取第三方依赖并编译（本讲 4.2/4.4）
#   联网环境直接: bash build.sh -j$(nproc)
python3 download_3rd_party.py          # 离线/内网环境先在有网机器执行
ls third_party/ | wc -l                # 记录下载了几个包
bash build.sh --cann_3rd_lib_path=$(pwd)/third_party -j$(nproc)

# 步骤 4：确认产物（本讲 4.2/4.5）
ls -lh build_out/                      # 记录产物文件名
# 期望看到形如: cann-npu-runtime_9.1.0_linux-x86_64.run（版本/架构按实际）

# 步骤 5：安装并验证（有 NPU 的环境）
cd build_out && ./cann-npu-runtime_<version>_linux-<arch>.run --full
npu-smi info
```

**验收清单**（建议整理成一张表贴在你的学习笔记里）：

| 检查项 | 命令 | 期望结果 |
|---|---|---|
| 依赖齐全 | `tail -3 deps.log` | 「所有依赖安装完成！」 |
| 环境变量 | `echo $ASCEND_HOME_PATH` | 指向 CANN 安装目录 |
| 第三方依赖 | `ls third_party/` | 与 README 表格数量一致 |
| 编译成功 | build.sh 输出 | `build success!` 与 `build finished` |
| 产物存在 | `ls build_out/` | `cann-npu-runtime_<version>_linux-<arch>.run` |
| 安装替换成功 | `npu-smi info` + install.info | 设备信息正常、版本一致 |

若某一步失败，回到本讲对应小节排查：依赖问题看 4.1，参数问题看 4.2，第三方下载问题看 4.4。完整编译耗时与产物名称随机器和版本变化，本讲撰写环境未执行全量编译，**待本地验证**。

## 6. 本讲小结

- **编译链路一条线**：`install_deps.sh`（基础工具）→ CANN 包 + `set_env.sh`（`ASCEND_HOME_PATH` 生效）→ `build.sh`（cmake configure → build → package）→ `build_out/*.run` → 安装替换官方 Runtime。
- **build.sh 是外壳**：`checkopts` 定默认值（线程数取 CPU 核数、Release、run 包、安装路径三级取值），`build_rts` 用 `cmake -S ../ -B .` + `cmake --build` + `make package` 三步完成编译打包。
- **CMakeLists.txt 是骨架**：cann-cmake 框架提供构建函数；`ENABLE_UT/ENABLE_COV` 决定是否进入 tests/ 与 Debug 构建；`TOOLCHAIN_DIR` + `ENABLE_BUILD_DEVICE` 决定是否交叉编译 Device 侧；`src/CMakeLists.txt` 按依赖顺序逐一编译 acl/runtime/dfx 各模块。
- **第三方依赖「本地优先、在线兜底」**：`CANN_3RD_LIB_PATH` 下的解压目录/ tar 包优先，缺失时才联网下载；离线环境用 `download_3rd_party.py` 预下载——它的 URL 直接解析自 README 的依赖表格。
- **run 包是替换件**：自编译产物安装后会替换 CANN 套件中的 Runtime 组件，验证靠 `npu-smi info` 与 `ascend_toolkit_install.info`。

## 7. 下一步学习建议

- 下一讲 u1-l3「仓库目录结构与代码地图」将带你逐目录认识 `src/acl`、`src/runtime`、`src/dfx`、`example`、`tests` 的职责，建议编译成功后对照 `build` 目录里的中间产物一起看。
- 编译成功后，建议先跑通 [example/README.md](example/README.md) 目录下的一个基础样例（如 device 相关样例），把「编译 → 安装 → 运行」全链路闭环。
- 想验证自己的构建环境是否支持测试，可以按 README「本地验证」章节执行 `bash tests/build_ut.sh --ut=acl --target=ascendcl_utest -c --cann_3rd_lib_path=<绝对路径>`，并阅读 [tests/build_ut.sh:L17-L51](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/tests/build_ut.sh#L17-L51) 的 `ut_path_map` 弄清模块名与用例路径的映射。
- 若本机环境受限，可尝试 [.devcontainer/README.md](.devcontainer/README.md) 提供的 Dev Container 方式，在容器内完成构建与 UT。
