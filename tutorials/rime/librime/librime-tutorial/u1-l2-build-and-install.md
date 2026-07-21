# 构建与安装：CMake 与依赖

## 1. 本讲目标

上一讲我们从 README 建立了 librime 的全局认知：它是一个跨平台、模块化的 C++17 输入法引擎。本讲要解决的问题是——**这堆 C++ 源码如何被编译成可被前端调用的库？**

读完本讲，你应当能够：

- 用一条 `make` 命令完成 librime 的构建与安装，并能看懂这条命令背后调用了哪些 CMake 参数。
- 说清楚 `BUILD_SHARED_LIBS`、`BUILD_TEST`、`ENABLE_LOGGING`、`BUILD_STATIC`、`BUILD_MERGED_PLUGINS` 等关键构建选项各自的开关作用。
- 解释 Boost / Glog / YamlCpp / LevelDb / Marisa / OpenCC / GTest 七个第三方依赖是如何被 CMake 查找、静态/动态选择并链接进最终库的。
- 区分动态库 `rime`、静态库 `rime-static`，以及拆分库 `rime-dict`/`rime-gears`/`rime-levers` 三种产物形态。

## 2. 前置知识

在看构建脚本前，先用大白话建立几个直觉：

- **源码 ≠ 可执行程序**。librime 是一个**库（library）**，它本身不直接做成可双击运行的程序，而是被「前端」（Squirrel/Weasel/ibus-rime 等）链接进去后一起运行。所以本讲的「产物」是 `.so`/`.dylib`/`.dll`（动态库）或 `.a`/`.lib`（静态库），而不是一个 exe。
- **CMake 是「构建脚本生成器」**。CMake 本身不编译代码，它读 `CMakeLists.txt`，按你选的编译器（gcc/clang/MSVC）生成一份具体的构建文件（Unix Makefiles / Ninja / VS 工程等），再交给 `make`/`ninja`/`msbuild` 去真正编译。所以「配置（configure）」和「构建（build）」是两步。
- **静态库 vs 动态库**。静态库在**链接期**被「抄」进最终程序，产物体积大但独立；动态库在**运行期**才被加载，产物小但运行时必须能找到 `.so` 文件。librime 默认产出动态库。
- **可选依赖**。README 把依赖分成「构建依赖」和「运行时依赖」两组：gtest 只在构建期跑测试用（可选），glog 只在开启日志时用（可选），其余 Boost/LevelDB/marisa/OpenCC/yaml-cpp 是必选核心依赖。
- **`find_package` 约定**。CMake 用 `find_package(Xxx)` 在系统里找库，找到后会设置 `Xxx_LIBRARY`、`Xxx_INCLUDE_PATH` 这类变量。librime 为没有提供官方 CMake 模块的几个库（LevelDb/Marisa/Opencc/Glog/YamlCpp/Gflags）自带了 `cmake/Find*.cmake` 查找模块。

## 3. 本讲源码地图

本讲涉及的关键文件，全部围绕「构建系统」这一个主题：

| 文件 | 作用 |
| --- | --- |
| `CMakeLists.txt` | 顶层 CMake 配置：声明项目、定义所有构建选项、查找依赖、决定产物形态、串联子目录 |
| `Makefile` | 给开发者用的「快捷外壳」：把常用的几组 CMake 参数组合成 `make release`/`make debug` 等目标 |
| `deps.mk` | 可选的第三方依赖自建脚本：把 glog/leveldb/marisa/opencc/yaml-cpp/googletest 全部编译成静态库 |
| `cmake/Find*.cmake` | 为第三方库补写的查找模块（如 `FindLevelDb.cmake`、`FindOpencc.cmake`） |
| `cmake/cxx_flag_overrides.cmake` | Windows/MSVC 下的编译器默认开关覆盖（强制静态运行时 `/MT`） |
| `src/rime/build_config.h.in` | 运行期配置模板：CMake 据此生成 `build_config.h`，把选项结果传进 C++ 源码 |
| `src/CMakeLists.txt` | 真正定义 `rime`/`rime-static` 库目标和链接关系的地方 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：①Makefile 快捷外壳；②顶层 CMakeLists 的配置阶段与选项；③依赖查找与链接；④库产物形态；⑤deps.mk 自建静态依赖。

### 4.1 Makefile：make 入口与常用目标

#### 4.1.1 概念说明

`Makefile` 不是构建系统的本体，而是一个**「薄壳」**：它把「配置 + 构建」两步合在一起，并为几种最常见的使用场景（发布版、调试版、静态库、合并插件、跑测试）各定义一个 make 目标，背后统一调用 `cmake` 命令。README 推荐的 `make && sudo make install`（[README.md:60-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L60-L65)）走的正是这个壳。

#### 4.1.2 核心流程

```text
make            # 默认目标 all → release
                 │
                 ├── release: cmake 配置(Release) + cmake --build
                 ├── debug:   cmake 配置(Debug) + ALSO_LOG_TO_STDERR=ON
                 ├── librime-static: BUILD_SHARED_LIBS=OFF + BUILD_STATIC=ON
                 ├── merged-plugins: BUILD_MERGED_PLUGINS=ON
                 ├── test:    先 release，再 ctest --output-on-failure
                 └── install: cmake --build $(build) --target install
```

每个目标内部都是同一个套路：先用 `cmake . -B$(build) -DCMAKE_INSTALL_PREFIX=$(prefix) -DCMAKE_BUILD_TYPE=... -D<选项>=...` 做配置，再用 `cmake --build $(build)` 做实际编译。

#### 4.1.3 源码精读

Makefile 第一件事是**按操作系统推算安装前缀 `prefix`**。Linux 默认装到 `/usr`，macOS 默认装到仓库下的 `dist/`：

[Makefile:6-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L6-L32) ——按 `uname` 区分 macOS / Termux / FreeBSD / OpenBSD / Linux，给 `prefix` 不同默认值。这里还能看到 macOS 特有的 `SDKROOT`、`MACOSX_DEPLOYMENT_TARGET` 处理，以及可选的 universal 构建（`CMAKE_OSX_ARCHITECTURES=arm64;x86_64`）。

接着是**自动并行**：

[Makefile:34-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L34-L36) ——如果没有定义 `NOPARALLEL`，就把 `MAKEFLAGS` 加上 `-j<n>`，其中 `n` 取 `nproc` 的 CPU 核数加一。这就是为什么直接敲 `make` 就能自动吃满多核。

再看最具代表性的 `release` 目标：

[Makefile:74-80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L74-L80) ——`release` 等价于：

```bash
cmake . -Bbuild \
  -DCMAKE_INSTALL_PREFIX=$(prefix) \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_MERGED_PLUGINS=OFF \
  -DENABLE_EXTERNAL_PLUGINS=ON
cmake --build build
```

注意它关掉了 `BUILD_MERGED_PLUGINS`、开起了 `ENABLE_EXTERNAL_PLUGINS`——即「插件不合并进主库，而是单独编译成可被外部加载的共享库」。这与 `merged-plugins` 目标恰恰相反。

`debug` 目标（[Makefile:90-97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L90-L97)）多开了 `-DALSO_LOG_TO_STDERR=ON`，方便调试时把 glog 日志同时打到终端。

跑测试的目标值得单独看一眼：

[Makefile:111-115](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L111-L115) ——`test` 依赖 `release`（即先做一次发布版构建），然后进到构建目录里执行 `ctest --output-on-failure`。`-debug` 变体同理，只是依赖换成 `debug`。

#### 4.1.4 代码实践

**实践目标**：看懂「敲一个 `make` 实际触发了什么」。

**操作步骤**（阅读型实践，不必真跑）：

1. 打开 `Makefile`，确认 `all: release`（[Makefile:46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L46)），即默认目标就是发布版构建。
2. 用 `make -n release`（`-n` 表示「只打印要执行的命令、不真正执行」）把 release 的完整命令打印出来。

**需要观察的现象**：终端会依次输出 `cmake . -Bbuild ...` 和 `cmake --build build` 两行命令（以及并行参数）。

**预期结果**：你看到的命令应与 [Makefile:74-80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L74-L80) 里的内容一字不差（`prefix` 会被替换成你系统的值，Linux 上是 `/usr`）。**待本地验证**：实际打印内容取决于你机器上 `nproc` 的取值。

#### 4.1.5 小练习与答案

**练习 1**：如果我只想得到一个静态库 `librime.a`，应该用哪个 make 目标？它设置了哪些 CMake 选项？

> **答案**：用 `make librime-static`（[Makefile:66-72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L66-L72)）。它设置 `-DBUILD_STATIC=ON`（依赖也用静态库）和 `-DBUILD_SHARED_LIBS=OFF`（本库产静态库），其余沿用 Release。

**练习 2**：`make test` 与 `make debug` 在构建产物上的最大差别是什么？

> **答案**：`make test` 走 Release 构建（无调试符号、开了优化），跑完后再 `ctest`；`make debug` 走 Debug 构建（带调试符号、关闭优化）且把 glog 日志同时打到 stderr。两者都能用于排查问题，但前者贴近发布形态，后者贴近开发调试。

### 4.2 CMakeLists.txt：配置阶段与构建选项

#### 4.2.1 概念说明

`CMakeLists.txt` 是构建系统的真正大脑。它分两个阶段工作：

- **配置阶段**（`cmake . -Bbuild`）：读选项、查依赖、生成 `build_config.h`、规划安装路径、登记子目录。这一步产出构建文件（如 `build/Makefile`）。
- **构建阶段**（`cmake --build build`）：由上一步产出的构建文件驱动编译器真正编译链接。

本小节聚焦配置阶段里的**项目声明**与**选项表**。

#### 4.2.2 核心流程

```text
CMakeLists.txt 配置顺序
  1. 声明项目：project(rime)、C++17、版本号 1.17.0、SOVERSION 1
  2. 声明一长串 option() 开关
  3. 设置静态/动态选择变量（Xxx_STATIC = BUILD_STATIC）
  4. find_package(...) 逐个查找依赖（见 4.3）
  5. configure_file() 生成 build_config.h
  6. add_subdirectory(plugins) / add_subdirectory(src) / add_subdirectory(tools) / add_subdirectory(test)
```

#### 4.2.3 源码精读

项目声明在最顶部：

[CMakeLists.txt:4-11](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L4-L11) ——要求 CMake ≥ 3.12，项目名 `rime`，C++ 标准 17，版本 `1.17.0`，`SOVERSION 1`，并用 `add_definitions(-DRIME_VERSION="...")` 把版本号以宏的形式注入源码。

接下来是**全量构建选项表**：

[CMakeLists.txt:18-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L18-L31) ——这是本讲最需要记住的一段。整理成表：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `BUILD_SHARED_LIBS` | ON | librime 本体编译成动态库（ON）还是静态库（OFF） |
| `BUILD_MERGED_PLUGINS` | ON | 把插件**合并**进主库（ON）还是单独编译（OFF） |
| `BUILD_STATIC` | OFF | 把第三方依赖也当静态库链接 |
| `BUILD_DATA` | OFF | 安装时附带 `data/preset` 预设数据 |
| `BUILD_SAMPLE` | OFF | 编译 `sample/` 示例插件 |
| `BUILD_TEST` | ON | 编译并启用测试（需要 GTest） |
| `BUILD_SEPARATE_LIBS` | OFF | 把 librime 拆成 rime-dict/gears/levers 多个库（见 4.4） |
| `ENABLE_LOGGING` | ON | 启用 glog 日志（可关闭以去掉 glog 依赖） |
| `ALSO_LOG_TO_STDERR` | OFF | 日志同时打到 stderr |
| `ENABLE_ASAN` | OFF | 开启 AddressSanitizer（仅 Unix） |
| `INSTALL_PRIVATE_HEADERS` | OFF | 安装内部私有头文件（外部构建插件时需要） |
| `ENABLE_EXTERNAL_PLUGINS` | OFF | 允许从 `RIME_PLUGINS_DIR` 运行时加载外部插件 |
| `ENABLE_THREADING` | ON | 部署器（deployer）启用线程 |
| `ENABLE_TIMESTAMP` | ON | 给方案产物嵌入时间戳 |

注意 `BUILD_TEST` 默认是 **ON**——这意味着即使你只想要库，默认配置也会要求系统里存在 GTest。在资源受限环境（如 CI、容器）常需要显式 `-DBUILD_TEST=OFF` 来跳过。

配置阶段还会**把选项结果转成 C++ 头文件**：

[CMakeLists.txt:160-162](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L160-L162) ——用 `configure_file` 把模板 [src/rime/build_config.h.in](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/build_config.h.in) 渲染成 `build/src/rime/build_config.h`。模板里的 `#cmakedefine RIME_ENABLE_LOGGING` 这类写法：只有当 CMake 变量 `RIME_ENABLE_LOGGING` 被设置时，渲染结果才是 `#define RIME_ENABLE_LOGGING`，否则整行被注释掉。这样 C++ 源码就能用 `#ifdef RIME_ENABLE_LOGGING` 来按需编译日志相关代码。

最后是**子目录串联**：

[CMakeLists.txt:264-288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L264-L288) ——`plugins` 和 `src` 总是被加入；但 `tools`、`test`、`sample` 三个子目录**只有在 `BUILD_SHARED_LIBS=ON` 时才会加入**（`test` 还要额外满足 `BUILD_TEST`，`sample` 还要满足 `BUILD_SAMPLE`）。也就是说：静态库构建默认不会产出控制台工具和测试可执行文件。

#### 4.2.4 代码实践

**实践目标**：亲手切换 `BUILD_TEST` 选项，观察 test 目标是否生成。

**操作步骤**：

1. 先做一次「默认」配置（BUILD_TEST 默认 ON）：
   ```bash
   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
   ```
2. 查看是否注册了测试：`cmake --build build --target help 2>&1 | grep -E 'rime_test|test'`。
3. 再做一次「关闭测试」的配置到另一个目录：
   ```bash
   cmake -S . -B build-notest -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=OFF
   ```
4. 同样执行 `cmake --build build-notest --target help 2>&1 | grep -E 'rime_test'`。

**需要观察的现象**：第 2 步应能看到名为 `rime_test` 的目标；第 4 步应查不到。

**预期结果**：与 [CMakeLists.txt:281-283](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L281-L283) 一致——`add_subdirectory(test)` 被包在 `if(BUILD_TEST)` 里，关掉后 test 子目录根本不会被处理，`rime_test` 目标自然消失。**待本地验证**：若系统未装 GTest，第 1 步会因 `find_package(GTest REQUIRED)`（[CMakeLists.txt:118-124](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L118-L124)）失败，这时第 3 步的 `BUILD_TEST=OFF` 反而是「能成功配置」的前提。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `make debug` 需要同时设 `BUILD_MERGED_PLUGINS=OFF` 和 `ENABLE_EXTERNAL_PLUGINS=ON`？

> **答案**：调试时通常希望插件是**独立**编译的共享库，可以单独替换、单独加断点；合并进主库（`BUILD_MERGED_PLUGINS=ON`）反而会让插件代码与主库耦合，不便于调试。

**练习 2**：`build_config.h` 是手写的还是生成的？它在哪里被消费？

> **答案**：是 CMake 在配置阶段用 `configure_file` 由 `build_config.h.in` 生成的（[CMakeLists.txt:160-162](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L160-L162)），落在 `${PROJECT_BINARY_DIR}/src/rime/build_config.h`。它把构建期的开关（如是否启用日志、数据目录名）转成 C++ 宏，源码用 `#ifdef` 消费它。

### 4.3 第三方依赖的查找与链接

#### 4.3.1 概念说明

librime 依赖七个第三方库（见 README 的依赖清单）。CMake 用 `find_package` 在系统里定位它们。难点在于：其中六个没有标准的 CMake config 文件，所以 librime 在 `cmake/` 下自带了一套 `Find*.cmake` 查找模块。

#### 4.3.2 核心流程

```text
find_package 的两条路径
  A. 用系统/CMake 自带模块（Boost、Threads、GTest）
  B. 用 librime 自带的 cmake/Find*.cmake
       └── 通过设置 CMAKE_MODULE_PATH 让 find_package 能找到它们
              （CMakeLists.txt:40 把 ${PROJECT_SOURCE_DIR}/cmake 加入模块路径）

找到后统一约定输出变量：
  Xxx_FOUND / Xxx_LIBRARY / Xxx_INCLUDE_PATH
```

#### 4.3.3 源码精读

先看模块路径注册：

[CMakeLists.txt:40-41](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L40-L41) ——把仓库下的 `cmake/` 目录加入 `CMAKE_MODULE_PATH`，并把仓库根加入 `CMAKE_PREFIX_PATH`。这样 `find_package(LevelDb)` 才会去 `cmake/FindLevelDb.cmake` 找查找逻辑。

**静态/动态选择**是在查找之前用一组变量控制的：

[CMakeLists.txt:52-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L52-L58) ——把 `BUILD_STATIC` 复制成 `Xxx_STATIC`。自定义查找模块会读这些变量来改变 `CMAKE_FIND_LIBRARY_SUFFIXES`，从而优先找 `.a`（静态）还是 `.so`（动态）。以 LevelDb 为例：

[cmake/FindLevelDb.cmake:3-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/cmake/FindLevelDb.cmake#L3-L15) ——先找头文件 `leveldb/db.h`；若 `LevelDb_STATIC` 为真，就把查找后缀改成 `.a`（Unix）或 `.lib`（Windows），再 `find_library`。这就是「`BUILD_STATIC=ON` 时依赖也走静态」的实现机制。

**依赖查找顺序**（[CMakeLists.txt:108-150](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L108-L150)）按「是否可选 / 是否需要特殊处理」排列：

| find_package | 是否必选 | 备注 |
| --- | --- | --- |
| `Threads` | 必选 | CMake 内置模块，提供 `CMAKE_THREAD_LIBS_INIT` |
| `YamlCpp` | 必选（`REQUIRED`） | 自带 Find 模块 |
| `LevelDb` | 必选 | 自带 Find 模块 |
| `Marisa` | 必选 | 自带 Find 模块 |
| `Opencc` | 必选 | 自带 Find 模块；额外定位 `Opencc_DICT_DIR`（见下） |
| `GTest` | 仅 `BUILD_TEST` 时必选 | 内置模块 |
| `Boost` | 必选，要求 `regex` 组件 | Linux 下要求 ≥1.74，其他平台 ≥1.77（[CMakeLists.txt:65-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L65-L69)） |
| `Glog` / `Gflags` | 仅 `ENABLE_LOGGING` 时 | glog 必选、gflags 可选 |

Boost 的处理比较特别，因为要按平台定版本并要求 `regex` 组件：

[CMakeLists.txt:65-74](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L65-L74) ——Linux 要求 Boost ≥ 1.74.0 且带 `COMPONENTS regex`，其他平台放宽到 ≥ 1.77.0 但不强制组件。找到后定义 `BOOST_DLL_USE_STD_FS`，让 `boost::dll`（用于插件加载）使用标准库的 `std::filesystem`。

日志相关的可选依赖集中在 `if(ENABLE_LOGGING)` 块里：

[CMakeLists.txt:76-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L76-L106) ——依次找 `Gflags`（可选）和 `Glog`（必选），并根据 `Glog_STATIC` 给出不同的可见性宏；最后设 `RIME_ENABLE_LOGGING=1`，这个变量正是渲染 `build_config.h` 时 `RIME_ENABLE_LOGGING` 宏的来源。注意：**关掉 `ENABLE_LOGGING` 后，glog/gflags 完全不必装**，这是去掉运行时日志依赖的官方途径。

OpenCC 还多了一步——定位它的字典目录（繁简转换规则文件所在），因为测试要用：

[cmake/FindOpencc.cmake:19-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/cmake/FindOpencc.cmake#L19-L26) ——从 `opencc/opencc.h` 的安装路径推断出 `<prefix>/share/opencc`，并在其中找 `t2s.json`，作为 `Opencc_DICT_DIR`。测试目录会把这个目录编译进 `rime_test`（[test/CMakeLists.txt:12-17](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/CMakeLists.txt#L12-L17)），找不到就 `FATAL_ERROR`。

最后，这些 `*_LIBRARY` 变量在 `src/CMakeLists.txt` 里被分组拼装成最终链接列表：

[src/CMakeLists.txt:46-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L46-L58) ——按「核心 / 字典 / 齿轮」三类把依赖分组：`rime_core_deps` = Boost + Glog + YamlCpp + 线程；`rime_dict_deps` = LevelDb + Marisa；`rime_gears_deps` = ICU + OpenCC。这种分组是「拆分库」形态（4.4）能够按需链接的基础。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `find_package` 调用，理解「找不到依赖时报什么错」。

**操作步骤**（阅读 + 故障演练型）：

1. 在一个**没有安装 leveldb 开发包**的环境里执行配置：
   ```bash
   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=OFF
   ```
2. 阅读报错信息。

**需要观察的现象**：CMake 报 `FATAL_ERROR: Could not find leveldb library.`。

**预期结果**：这条报错正是 [cmake/FindLevelDb.cmake:20-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/cmake/FindLevelDb.cmake#L20-L23) 里 `LevelDb_FIND_REQUIRED` 为真时的输出。装上 leveldb 开发包后（如 Debian 系 `apt install libleveldb-dev`）再配置即可通过。**待本地验证**：具体包名取决于发行版；若你已装齐依赖，此步会直接成功，可跳过故障演练，改为在 `build/CMakeCache.txt` 里搜索 `LevelDb_LIBRARY` 查看它实际定位到的库文件路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 librime 要自带 `cmake/FindLevelDb.cmake`，而不是直接 `find_package(leveldb CONFIG REQUIRED)`？

> **答案**：leveldb 等几个库没有提供 CMake config 文件（`leveldbConfig.cmake`），CMake 也没有内置它们的 Module 模块。自带 `Find*.cmake` 是用「Module 模式」搜索，能在各大发行版的系统包布局下找到 `.h` 和 `.a/.so`。

**练习 2**：要彻底移除 librime 对 glog 的运行时依赖，应该怎么做？

> **答案**：配置时加 `-DENABLE_LOGGING=OFF`（[CMakeLists.txt:25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L25)、[CMakeLists.txt:76-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L76-L106)）。这样不会执行 `find_package(Glog)`，`build_config.h` 里也不会定义 `RIME_ENABLE_LOGGING`。

### 4.4 库产物：动态库 / 静态库 / 拆分库

#### 4.4.1 概念说明

配置完成后，`src/CMakeLists.txt` 负责真正定义库目标。librime 有三种产物形态，由 `BUILD_SHARED_LIBS` 和 `BUILD_SEPARATE_LIBS` 两个开关组合决定。

#### 4.4.2 核心流程

```text
BUILD_SHARED_LIBS=ON  (默认)
 ├─ BUILD_SEPARATE_LIBS=OFF (默认) → 一个 librime.so，所有源码都编进来
 └─ BUILD_SEPARATE_LIBS=ON       → 拆成 rime / rime-dict / rime-gears / rime-levers 四个 .so
                                    (+ 可选 rime-plugins)

BUILD_SHARED_LIBS=OFF
 └─ 一个 librime.a（静态库），把全部源码静态归档
```

注意一个细节：`tools`/`test`/`sample` 子目录只在 `BUILD_SHARED_LIBS=ON` 时才加入构建（[CMakeLists.txt:278-288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L278-L288)）。静态库构建默认不产出可执行工具。

#### 4.4.3 源码精读

产物形态的「分叉点」在顶层文件：

[CMakeLists.txt:252-262](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L252-L262) ——据此设置 `rime_library` 变量为 `rime`（动态）或 `rime-static`（静态），并定义编译宏 `RIME_BUILD_SHARED_LIBS`。下游 `src/CMakeLists.txt` 读这个变量来决定建哪种目标。

动态库目标的定义：

[src/CMakeLists.txt:82-99](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L82-L99) ——`add_library(rime ${rime_src})` 创建动态库；关键属性有三：

- `DEFINE_SYMBOL "RIME_EXPORTS"`：编译 librime 本体时定义 `RIME_EXPORTS`，配合头文件里的 `RIME_API` 宏，让符号在 Windows 下被导出（跨平台 DLL 导出的惯用法）。
- `VERSION ${rime_version}` / `SOVERSION ${rime_soversion}`：Linux 上会生成 `librime.so.1.17.0` + 软链 `librime.so.1` + `librime.so`。
- `LIBRARY_OUTPUT_DIRECTORY .../lib` / `RUNTIME_OUTPUT_DIRECTORY .../bin`：产物落在 `build/lib`（或 Windows 下 `build/bin`）。

静态库目标定义在 `else` 分支：

[src/CMakeLists.txt:165-172](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L165-L172) ——`add_library(rime-static STATIC ${rime_src})`，输出名强制为 `rime`、前缀 `lib`，即产物 `librime.a`，落在 `build/lib`。

「拆分库」形态（`BUILD_SEPARATE_LIBS=ON`）则额外定义三个目标，分别承载不同子目录源码：

[src/CMakeLists.txt:100-164](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L100-L164) ——`rime-dict`（algo + dict 源码，链 LevelDb+Marisa）、`rime-gears`（gear 源码，链 ICU+OpenCC）、`rime-levers`（lever 源码）。它们互相依赖：gears 依赖 dict 依赖 rime。这种拆分让下游可以只链接自己需要的部分，但日常使用绝大多数人用默认的单库形态。

源码本身如何被分组，决定了上述目标能装进什么：

[src/CMakeLists.txt:1-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L1-L36) ——用 `aux_source_directory` 把 `.`、`rime`、`rime/algo`、`rime/config`、`rime/dict`、`rime/gear`、`rime/lever` 各子目录的 `.cc` 收集成变量，再组合成 `rime_core_module_src`/`rime_dict_module_src`。默认（不拆分）时把它们全部合成一个 `rime_src`。

#### 4.4.4 代码实践

**实践目标**：构建并定位库产物文件路径。

**操作步骤**：

1. 执行 `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=OFF && cmake --build build`。
2. 在构建目录里找库文件：
   ```bash
   ls -la build/lib
   ```
3. 再做一次静态库构建到另一目录：
   ```bash
   cmake -S . -B build-static -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC=ON
   cmake --build build-static
   ls -la build-static/lib
   ```

**需要观察的现象**：

- 第 2 步：Linux 上应看到 `librime.so.1.17.0`、`librime.so.1`、`librime.so`（软链关系）。
- 第 3 步：应看到 `librime.a`，且 `build-static/bin` 下不会有 `rime_api_console` 等工具（因为静态库构建不加入 tools 子目录）。

**预期结果**：与 [src/CMakeLists.txt:82-99](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L82-L99)（动态库带 VERSION/SOVERSION）和 [CMakeLists.txt:278-288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L278-L288)（静态库不建 tools）一致。**待本地验证**：macOS 产物为 `librime.1.17.0.dylib` 系列；Windows 产物为 `rime.dll`/`rime.lib` 落在 `bin`。

#### 4.4.5 小练习与答案

**练习 1**：`librime.so.1.17.0`、`librime.so.1`、`librime.so` 三者是什么关系？谁来生成？

> **答案**：三者是同一份动态库的不同「名字」：`.so.1.17.0` 是真实文件（`VERSION`），`.so.1` 是指向它的软链（`SOVERSION`），`.so` 又是指向 `.so.1` 的软链（开发期链接用）。由 [src/CMakeLists.txt:87-88](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L87-L88) 的 `VERSION`/`SOVERSION` 属性生成。

**练习 2**：为什么 `rime-gears` 要依赖 `rime-dict`，而 `rime-dict` 又依赖 `rime`？

> **答案**：因为源码本身有分层依赖关系——gear 里的 Translator 要查 dict 里的词典，dict 里的 Dictionary 要用 core 里的 Config/组件机制。拆分库（[src/CMakeLists.txt:115-143](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L115-L143)）必须按 `gears → dict → rime` 的顺序链接，才能保证符号在运行时被正确解析。

### 4.5 deps.mk：自建静态依赖（可选）

#### 4.5.1 概念说明

有时候你的系统里没有现成的 leveldb/marisa 等开发包，或者你想做一次「完全静态、可移植」的构建。`deps.mk` 就是为此准备的：它把六个第三方库的源码（预先放在 `deps/` 目录下）逐一用 CMake 编译成**静态库**并安装到本地 `prefix`，供随后 librime 的 `BUILD_STATIC=ON` 构建使用。

#### 4.5.2 核心流程

```text
make deps        # 触发 deps.mk
  for dep in [glog googletest leveldb marisa-trie opencc yaml-cpp]:
      cd deps/<dep>
      cmake . -B<build> -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF ... -DCMAKE_INSTALL_PREFIX=$(prefix)
      cmake --build <build> --target install   # 装到 $(prefix)/{lib,include,...}

随后:
  make librime-static   # BUILD_STATIC=ON，CMake 会从 $(prefix) 找到这些静态依赖
```

#### 4.5.3 源码精读

依赖清单在最上方：

[deps.mk:13](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L13) ——`rime_deps = glog googletest leveldb marisa-trie opencc yaml-cpp`。注意 `Boost` 不在其中——Boost 体量太大，通常用系统包或单独准备（macOS 上见 [Makefile:9-12](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L9-L12) 的 `BOOST_ROOT` 处理）。

每个依赖目标都是「进目录 → cmake 配置 → cmake build + install」三步。以 leveldb 为例：

[deps.mk:52-59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L52-L59) ——关掉 benchmark 和 tests，按 Release 编译成静态库，安装到 `$(prefix)`。其余几个目标（[deps.mk:34-87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L34-L87)）套路一致，只是各自关掉自己项目的 tests/tools/contrib 等。

入口在 `Makefile` 里：

[Makefile:55-59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L55-L59) ——`make deps` 转去执行 `deps.mk`；`make deps/leveldb` 这种写法还能单独构建某一个依赖。

#### 4.5.4 代码实践

**实践目标**：理解 `make deps` 的产物去向，不一定要真跑（编译六个库较慢）。

**操作步骤**（阅读型）：

1. 阅读 [deps.mk:10-11](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L10-L11)，确认 `prefix` 默认是仓库根目录 `$(rime_root)`。
2. 推断 `make deps` 后静态库会装到哪里。

**需要观察的现象 / 预期结果**：每个依赖的 `--target install` 会把 `.a` 文件和头文件装到 `<仓库根>/lib`、`<仓库根>/include` 等目录。这正是顶层 [CMakeLists.txt:167](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L167) 里 `link_directories(${PROJECT_SOURCE_DIR}/lib)` 能让随后的 librime 构建找到它们的原因。**待本地验证**：如需真正运行，需先把各依赖源码放入 `deps/`（通常作为 git submodule 或手动下载）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `deps.mk` 里 opencc 和 glog 都显式 `-DBUILD_SHARED_LIBS:BOOL=OFF`，而 marisa-trie 没有？

> **答案**：opencc（[deps.mk:71-77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L71-L77)）和 glog 默认会建动态库，需显式关掉；marisa-trie 默认就产静态库，但额外加了 `CMAKE_POSITION_INDEPENDENT_CODE=ON`（[deps.mk:61-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L61-L69)），让它的 `.a` 可以被链进 librime 的动态库（PIC 代码要求）。

**练习 2**：`make clean`（Makefile 里的）和 `deps.mk` 里的 `clean` 各自清什么？

> **答案**：`Makefile` 的 `clean`（[Makefile:61-62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/Makefile#L61-L62)）删的是 `build/` 目录（librime 自己的构建产物）；`deps.mk` 的 `clean`（[deps.mk:19-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/deps.mk#L19-L32)）分 `clean-dist`（删安装到 prefix 的产物）和 `clean-src`（删各依赖的构建目录）。

## 5. 综合实践

把本讲内容串起来，完成一次「从零到装好」的端到端构建，并回答产物相关问题。

**任务**：在一个干净环境里构建并安装 librime，记录每一步产出的文件，并解释它们对应到本讲的哪个构建选项。

**操作步骤**：

1. **配置**（默认动态库、关测试以减少依赖）：
   ```bash
   cmake -S . -B build \
     -DCMAKE_BUILD_TYPE=Release \
     -DCMAKE_INSTALL_PREFIX=$PWD/dist \
     -DBUILD_TEST=OFF \
     -DENABLE_LOGGING=ON
   ```
2. **构建**：`cmake --build build`。
3. **定位库与工具产物**：`ls build/lib build/bin`。
4. **安装**：`cmake --build build --target install`，然后 `find dist -type f | sort`。
5. **回答三个问题**（写在你的学习笔记里）：
   - `dist/lib` 下的库文件名体现了哪两个 CMake 属性？（答：4.4 的 VERSION/SOVERSION）
   - `dist/include` 下装了哪些头文件？为什么 `src/rime/*.h` 默认不在其中？（提示：[CMakeLists.txt:229-245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L229-L245) 的 `INSTALL_PRIVATE_HEADERS`）
   - 如果想把这次构建改成静态库，要改哪个选项？`build/bin` 下的工具还会生成吗？（答：4.4 的 `BUILD_SHARED_LIBS=OFF`，且 tools 不会生成）

**预期结果**：`dist/lib` 下应有 `librime.so*`（Linux）；`dist/bin` 下应有 `rime_api_console`、`rime_deployer`、`rime_dict_manager`、`rime_patch`（见 [tools/CMakeLists.txt:40-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/CMakeLists.txt#L40-L42)）；`dist/include` 下应有 `rime_api.h` 等公共头文件。**待本地验证**：实际产物清单依赖你是否启用了测试、是否装了 GTest/OpenCC 字典等。

## 6. 本讲小结

- librime 用 **CMake** 做构建系统；`Makefile` 是一层「快捷外壳」，`make`（= `make release`）= `cmake 配置 + cmake --build` 两步。
- 关键构建选项集中在 [CMakeLists.txt:18-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L18-L31)：`BUILD_SHARED_LIBS` 决定动/静态，`BUILD_TEST` 决定是否需要 GTest，`ENABLE_LOGGING` 决定是否依赖 glog，`BUILD_STATIC` 决定第三方依赖是否走静态。
- 七个第三方依赖通过 `find_package` 查找；其中 LevelDb/Marisa/Opencc/Glog/YamlCpp/Gflags 由 librime 自带的 `cmake/Find*.cmake` 模块定位，静态/动态选择由 `Xxx_STATIC` 变量驱动。
- 配置阶段用 `configure_file` 把选项渲染成 [src/rime/build_config.h.in](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/build_config.h.in) → `build_config.h`，让 C++ 源码能用 `#ifdef` 按需编译。
- 库产物有三种形态：默认单动态库 `rime`、静态库 `rime-static`、拆分库 `rime-dict`/`rime-gears`/`rime-levers`；动态库带 `VERSION`/`SOVERSION`，落在 `build/lib`。
- `tools`/`test`/`sample` 子目录只在 `BUILD_SHARED_LIBS=ON` 时才参与构建——静态库构建默认不产出可执行工具。
- 可选的 `make deps`（`deps.mk`）能把六个第三方库从源码编译成静态库，用于完全自包含的静态构建。

## 7. 下一步学习建议

本讲结束后，你已经能从源码产出 `librime.so` 和一批命令行工具。下一讲（**u1-l3 源码目录与分层架构**）建议你带着「刚编出来的库到底由哪些源码组成」的问题，去梳理 `src/rime` 下的 `algo`/`config`/`dict`/`gear`/`lever` 子目录职责——这正是 [src/CMakeLists.txt:1-7](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L1-L7) 用 `aux_source_directory` 收集的那几个目录。

随后可以：

- 直接跑 **u1-l5** 用 `rime_api_console` 体验一次输入流程，前提是先按本讲把 librime 构建出来。
- 阅读 [cmake/RimeConfig.cmake](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/cmake/RimeConfig.cmake) 和 `rime.pc.in`，理解前端项目如何反过来「查找并使用」你刚装好的 librime。
- 想做插件开发的话，留意 `BUILD_SAMPLE`、`INSTALL_PRIVATE_HEADERS`、`ENABLE_EXTERNAL_PLUGINS` 这三个选项，它们会在 **u9-l6 插件开发实战** 里反复出现。
