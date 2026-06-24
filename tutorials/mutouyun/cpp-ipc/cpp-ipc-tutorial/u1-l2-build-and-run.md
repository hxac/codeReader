# 构建、安装与首次运行

## 1. 本讲目标

上一讲我们认识了 cpp-ipc（libipc）是什么、为什么用它。本讲的目标是把它**真正跑起来**。读完本讲你应该能够：

- 看懂 libipc 顶层 `CMakeLists.txt` 里那几个 `LIBIPC_BUILD_*` 开关分别控制什么；
- 理解 `src/CMakeLists.txt` 是如何把一堆 `.cpp`/`.h` 收集成一个叫 `ipc` 的库目标，并自动链接 `pthread`/`rt` 的；
- 知道 `demo/` 目录下的各个示例是怎么被构建出来的，以及它们最终产物落在哪个目录；
- 学会用 vcpkg 一行命令安装 libipc；
- 在本机编译并运行 `send_recv` 的 `send` 与 `recv` 两个进程，亲眼看到一次跨进程通信。

本讲只关心**怎么把库构建出来并跑起来**，不深入库的内部实现——那是后面单元的事。

## 2. 前置知识

在动手之前，你需要了解几个 C++ 项目里常见的概念。不熟悉也没关系，下面用最通俗的话解释：

- **构建（build）**：把人写的 `.cpp` 源码翻译成机器能运行的程序或库的过程。C++ 是编译型语言，不能像 Python 那样直接运行源码。
- **CMake**：一个「构建脚本生成器」。它本身不编译代码，而是根据你写的 `CMakeLists.txt` 生成具体的构建文件（Linux 上通常是 `Makefile`，Windows 上可以是 Visual Studio 工程或 Ninja 文件），再交给 `make` / `ninja` / MSBuild 去真正编译。
- **静态库 vs 动态库（共享库）**：
  - 静态库（Linux 上是 `libxxx.a`，Windows 上是 `xxx.lib`）：在**链接期**就把库的代码塞进你的程序里，运行时不依赖外部库文件。
  - 动态库（Linux 上是 `libxxx.so`，Windows 上是 `xxx.dll`）：程序运行时才去加载库，多个程序可以共享同一份库文件。
- **目标（target）**：CMake 里的一个构建单元，可以是一个库（`add_library`）或一个可执行程序（`add_executable`）。本讲里最重要的目标是名为 `ipc` 的库目标。
- **进程间通信（IPC）**：两个独立运行的程序之间交换数据。libipc 用**共享内存**来实现，所以一个 demo 会被拆成两个独立的进程（一个发、一个收）来验证通信。

## 3. 本讲源码地图

本讲涉及的「源码」主要是构建脚本，它们决定了库怎么被编译出来。下表列出本讲会逐行讲解的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`CMakeLists.txt`](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt) | 顶层构建脚本：定义项目名、版本、五个构建开关、C++ 标准、编译/链接选项，并按开关决定要不要构建测试和 demo。 |
| [`src/CMakeLists.txt`](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt) | 库本身的构建脚本：收集 `src/libipc` 下的源码与头文件，定义 `ipc` 库目标（静态/动态），设置包含目录与链接库，生成安装配置。 |
| [`demo/send_recv/CMakeLists.txt`](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/CMakeLists.txt) | `send_recv` 示例的构建脚本：编译出可执行文件并链接 `ipc` 库。 |
| [`demo/send_recv/main.cpp`](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp) | `send_recv` 示例的主程序：根据命令行参数决定是「发送」还是「接收」，是本讲实践要运行的程序。 |
| [`.github/workflows/c-cpp.yml`](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/.github/workflows/c-cpp.yml) | CI 配置：展示了官方在 Linux 上的真实构建命令，是本讲实践命令的依据。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①顶层 CMake 选项、②src 构建目标与链接、③demo 构建子目录、④vcpkg 安装方式。

### 4.1 顶层 CMake 选项

#### 4.1.1 概念说明

libipc 是一个库，不是只有一个 `main()` 的单一程序。它的构建产物有「库本体」和「可选的测试 / 示例」两类。为了让你按需构建，项目在顶层 `CMakeLists.txt` 里用 `option()` 提供了一组开关。`option(名字 "说明" 默认值)` 的意思是：定义一个布尔开关，你可以在 `cmake` 命令行里用 `-D名字=ON/OFF` 覆盖默认值。**默认全部是 `OFF`**，也就是说，开箱即用只会构建出库本身，不会自动编译测试和示例——这能加快首次构建速度，也避免在只想用库时拉起一堆依赖。

#### 4.1.2 核心流程

构建 libipc 的整体流程可以这样描述：

1. 在项目根目录运行 `cmake`，通过 `-D` 传入你想开启的开关（例如 `-DLIBIPC_BUILD_DEMOS=ON`）。
2. CMake 读取顶层 `CMakeLists.txt`，设置项目名 `cpp-ipc`、版本 `1.4.1`、C++ 标准为 17。
3. 无论开关如何，**总是会**执行 `add_subdirectory(src)` 来构建库本体。
4. 只有当 `LIBIPC_BUILD_TESTS=ON` 时，才拉起 gtest 并构建 `test/`。
5. 只有当 `LIBIPC_BUILD_DEMOS=ON` 时，才逐个构建 `demo/` 下的子目录。
6. 生成具体构建文件（Makefile 等），你再用 `make` 真正编译。

```
cmake -D...=.  ──►  顶层 CMakeLists.txt
                        │
                        ├─ add_subdirectory(src)          ← 永远构建库
                        ├─ if LIBIPC_BUILD_TESTS → test/   ← 可选
                        └─ if LIBIPC_BUILD_DEMOS → demo/   ← 可选
                                          │
                                          ▼
                              make  ──►  lib/libipc.a, bin/send_recv ...
```

#### 4.1.3 源码精读

先看项目名、版本与 C++ 标准的声明：

[CMakeLists.txt:1-2](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L1-L2)：声明项目名为 `cpp-ipc`、版本 `1.4.1`，这个版本号后面会被 `src/CMakeLists.txt` 复用为库版本。

[CMakeLists.txt:4-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L4-L8)：这就是本模块的核心——五个 `option()` 开关：

- `LIBIPC_BUILD_TESTS`：构建 libipc 自己的单元测试；
- `LIBIPC_BUILD_DEMOS`：构建所有示例（chat / msg_que / send_recv / service）；
- `LIBIPC_BUILD_SHARED_LIBS`：构建动态库（`.so`/`.dll`），默认 `OFF` 即静态库；
- `LIBIPC_USE_STATIC_CRT`：Windows 上用静态 CRT（`/MT`）而非默认的动态 CRT（`/MD`）；
- `LIBIPC_CODECOV`：开启单元测试覆盖率统计。

[CMakeLists.txt:11](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L11)：强制 C++ 标准为 17（`CMAKE_CXX_STANDARD 17`）。这呼应了 README 里「推荐支持 C++17 的编译器」。

[CMakeLists.txt:12-15](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L12-L15)：Release 构建附加 `-DNDEBUG`，非 MSVC 再加 `-O2` 优化。

[CMakeLists.txt:45-46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L45-L46)：把库和可执行文件的**全局**输出目录都设到 `${CMAKE_BINARY_DIR}/bin`。注意 `src/CMakeLists.txt` 还会对库目标做更精细的覆盖（见 4.2.3），所以静态库实际落在 `lib/` 而非 `bin/`。

[CMakeLists.txt:52](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L52)：`add_subdirectory(src)`——**无条件**构建库本体，这是整个项目的基石。

[CMakeLists.txt:65-76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L65-L76)：当 `LIBIPC_BUILD_DEMOS=ON` 时，分别 `add_subdirectory` 各个 demo。注意这里的平台分支：`chat`、`msg_que`、`send_recv` 三个在所有平台都构建；而 service 类 demo 按 `if (MSVC)` 区分——Windows 下构建 `demo/win_service/`，其它平台（Linux/FreeBSD）构建 `demo/linux_service/`。

[CMakeLists.txt:78-81](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L78-L81)：`install` 把 `include/` 目录整体安装到系统的 `include`，这样 `#include <libipc/ipc.h>` 才能被找到。

#### 4.1.4 代码实践

这是一个「阅读型 + 动手型」结合的实践。

1. **实践目标**：搞清楚每个开关的默认值，并实际在命令行里开启它们。
2. **操作步骤**：
   - 打开顶层 `CMakeLists.txt` 第 4–8 行，记录五个开关的默认值（都是 `OFF`）。
   - 在项目根目录运行：

     ```bash
     cmake -DCMAKE_BUILD_TYPE=Release -DLIBIPC_BUILD_DEMOS=ON .
     ```

     （这里的命令格式与官方 CI [c-cpp.yml:17](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/.github/workflows/c-cpp.yml#L17) 一致，只是把 `LIBIPC_BUILD_TESTS` 换成了 `LIBIPC_BUILD_DEMOS`。）
3. **需要观察的现象**：CMake 输出里会出现 `-- Configuring done`、`-- Generating done`，并生成 `Makefile`；同时控制台会打印各 demo 子目录正在被处理。
4. **预期结果**：项目根目录下出现 `Makefile`、`CMakeCache.txt` 等生成文件。此时还没有编译，编译在下一步 `make` 完成。
5. 本地是否一定成功取决于你是否已安装 CMake ≥ 3.10 和一个 C++17 编译器；若环境齐全则预期成功，否则请先安装依赖。

#### 4.1.5 小练习与答案

**练习 1**：为什么项目默认把 `LIBIPC_BUILD_TESTS` 和 `LIBIPC_BUILD_DEMOS` 都设成 `OFF`？

**参考答案**：因为多数使用者只想拿到库本体去链接自己的程序，并不需要测试和示例。默认 `OFF` 可以加快首次配置与编译速度，也避免在没有 gtest 等依赖的环境下因拉起 `test/` 而失败。需要时用 `-D...=ON` 显式开启即可。

**练习 2**：在 Windows 下用 MSVC 构建，想把 CRT 链接方式从默认的 `/MD`（动态 CRT）改成 `/MT`（静态 CRT），该用哪个开关？

**参考答案**：使用 `LIBIPC_USE_STATIC_CRT=ON`。它对应 [CMakeLists.txt:32-36](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L32-L36) 的逻辑：把各编译标志里的 `/MD`→`/MT`、`/MDd`→`/MTd`。

### 4.2 src 构建目标与链接

#### 4.2.1 概念说明

`src/CMakeLists.txt` 的唯一职责是：把 `src/libipc/` 下散落的源码收集起来，定义成一个叫 **`ipc`** 的库目标，并告诉 CMake「编译这个库需要哪些头文件目录、要链接哪些系统库」。理解这一点很关键——后面所有 demo（如 `send_recv`）链接的就是这个名为 `ipc` 的目标，而不是某个写死的文件路径。

由于 libipc 用到了线程（`pthread`）和实时扩展（`rt`，用于 `shm_open` 等共享内存接口），在 Linux/类 Unix 上必须链接这两个系统库，否则会报「未定义引用」。CMake 用「生成器表达式」让这两条链接只在非 Windows、非 QNX 平台生效。

#### 4.2.2 核心流程

1. 用 `aux_source_directory` 把 5 个子目录的 `.cpp` 文件追加到 `SRC_FILES` 列表。
2. 用 `file(GLOB ...)` 把多个目录下的 `.h`/`.inc` 收集到 `HEAD_FILES`（头文件本身不编译，但放进目标里方便在 IDE 里查看）。
3. 根据 `LIBIPC_BUILD_SHARED_LIBS` 决定是 `SHARED` 还是 `STATIC`，从而生成动态库或静态库。
4. 设置输出目录、版本号、包含目录、链接库。
5. 生成安装配置（`cpp-ipc-targets.cmake`、`cpp-ipc-config.cmake` 等），让别的项目能 `find_package` 找到它。

#### 4.2.3 源码精读

[src/CMakeLists.txt:1](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L1)：`project(ipc)`——注意这个项目名就是库目标名 `ipc`，后面 `target_link_libraries(... ipc)` 引用的就是它。

[src/CMakeLists.txt:5-9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L5-L9)：用 `aux_source_directory` 收集 5 个目录的源文件：`src/libipc`（核心 `ipc.cpp`/`shm.cpp`/`buffer.cpp` 等）、`sync`、`platform`、`imp`、`mem`。这正是 U1-L3「目录结构」里那些分层的源码。

[src/CMakeLists.txt:20-29](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L20-L29)：静态/动态库二选一。开启动态库时，会定义两个宏：
- `LIBIPC_LIBRARY_SHARED_BUILDING__`（`PRIVATE`）：**编译本库时**用，表示「正在导出符号」；
- `LIBIPC_LIBRARY_SHARED_USING__`（`INTERFACE`）：**使用本库的人**会继承，表示「正在导入符号」。

这对宏配合头文件里的 `__declspec(dllexport/dllimport)` 或 GCC 可见性属性，保证 Windows DLL 的符号正确导出/导入。默认（静态库）则不需要这些。

[src/CMakeLists.txt:32-36](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L32-L36)：覆盖库目标的输出目录——`ARCHIVE`（静态库 `.a`/`.lib`）和 `LIBRARY`（动态库 `.so`）都进 `${CMAKE_BINARY_DIR}/lib`，`RUNTIME`（Windows 的 `.dll`）进 `bin`。所以默认静态构建会得到 `lib/libipc.a`。

[src/CMakeLists.txt:39-42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L39-L42)：库版本 `VERSION 1.4.1`、`SOVERSION 3`。`SOVERSION` 是动态库的主版本号，会体现在 `libipc.so.3` 这样的文件名里，用于运行时兼容性判断。

[src/CMakeLists.txt:44-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L44-L49)：包含目录。
- `PUBLIC` 暴露 `include/`：无论是编译本库还是别人用它，都需要 `include/libipc/*.h`，所以用带 `BUILD_INTERFACE`/`INSTALL_INTERFACE` 的生成器表达式区分「构建时」和「安装后」的路径。
- `PRIVATE` 暴露 `src/` 和（仅 Unix）`src/libipc/platform/linux`：只在编译本库内部时需要，使用者看不到。

[src/CMakeLists.txt:51-55](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L51-L55)：**本模块的关键**——链接系统库：
- `pthread`：除 QNX 外的类 Unix 平台都需要（线程支持）；
- `rt`：除 Windows 和 QNX 外都需要（POSIX 实时扩展，提供 `shm_open`/`shm_unlink` 等共享内存 API）。
- 这两条用 `$<$<NOT:...>:pthread>` 这种生成器表达式，确保在 Windows 上不会去链接不存在的 `pthread`/`rt`。

> 小提示：如果你在自己机器上手动编译（不走 CMake）报了 `undefined reference to 'shm_open'` 之类的错，多半就是忘了加 `-lrt -lpthread`，CMake 已经替你处理好了。

[src/CMakeLists.txt:57-83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L57-L83)：安装与 `find_package` 支持。`install(TARGETS ... EXPORT cpp-ipc-targets ...)` 把库导出为一个命名空间 `cpp-ipc::` 下的目标，并生成 `cpp-ipc-config.cmake` 和版本文件。这样别的项目 `find_package(cpp-ipc)` 后就能用 `cpp-ipc::ipc` 来链接。

#### 4.2.4 代码实践

1. **实践目标**：确认库目标名是 `ipc`，并理解它的依赖。
2. **操作步骤**：在已经 `cmake` 配置好的项目根目录执行：

   ```bash
   make -j        # -j 表示并行编译，加速
   ```

   然后查看产物：

   ```bash
   ls -la lib/    # 应能看到 libipc.a（默认静态库）
   ```
3. **需要观察的现象**：`make` 输出会显示正在编译 `ipc.cpp`、`shm.cpp`、`buffer.cpp` 等源文件，最后 `ar` 或链接器生成库文件。
4. **预期结果**：`lib/` 下出现 `libipc.a`（静态库默认产物）。这一步与官方 CI [c-cpp.yml:18-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/.github/workflows/c-cpp.yml#L18-L19) 的 `make -j` 一致。
5. 若改用 `-DLIBIPC_BUILD_SHARED_LIBS=ON` 重新配置并编译，则 `lib/` 下应出现 `libipc.so`（及带版本后缀的 `libipc.so.3`、`libipc.so.1.4.1`），运行使用它的程序时需把 `lib/` 加入 `LD_LIBRARY_PATH`（见 CI 的 `LD_LIBRARY_PATH=./lib`）。

#### 4.2.5 小练习与答案

**练习 1**：demo 程序链接库时写的是 `target_link_libraries(${PROJECT_NAME} ipc)`，这里的 `ipc` 指的是什么？

**参考答案**：指的是 `src/CMakeLists.txt` 里 `project(ipc)` / `add_library(${PROJECT_NAME} ...)` 定义的**库目标名** `ipc`，而不是某个具体文件。CMake 会自动把它解析成静态库 `libipc.a` 或动态库 `libipc.so`，并连带传递 `PUBLIC` 的包含目录和链接依赖（`pthread`/`rt`）。

**练习 2**：为什么 `pthread`、`rt` 的链接用 `$<$<NOT:...>:pthread>` 而不是直接写 `pthread`？

**参考答案**：因为 Windows 上根本没有 `pthread`/`rt` 这两个系统库（Windows 用自己的线程和内存映射 API）。生成器表达式让这两条链接只在需要的平台生效，从而同一份 `CMakeLists.txt` 能跨平台构建。

### 4.3 demo 构建子目录

#### 4.3.1 概念说明

光有库没法验证它好不好用，所以项目带了一组 demo（示例）。每个 demo 是一个独立的小程序，放在 `demo/<名字>/` 子目录里，各有自己的 `CMakeLists.txt`。它们共享一个约定：通过 `target_link_libraries(自己 ipc)` 链接主库，并把库的头文件目录通过 `ipc` 目标的 `PUBLIC` 包含目录自动继承下来。

本讲的主角是 `send_recv`——它是最简单的「一发一收」示例，被专门用来验证跨进程通信是否真的通了。

#### 4.3.2 核心流程

1. 顶层 `LIBIPC_BUILD_DEMOS=ON` 后，`add_subdirectory(demo/send_recv)` 进入该子目录。
2. 子目录 `CMakeLists.txt` 用 `file(GLOB)` 收集当前目录的 `.cpp`/`.h`。
3. `add_executable(send_recv ...)` 生成可执行文件 `send_recv`。
4. `target_link_libraries(send_recv ipc)` 链接主库。
5. 可执行文件落到 `bin/`（顶层设置的 `EXECUTABLE_OUTPUT_PATH`）。
6. 运行 `./bin/send_recv send <size> <interval>` 发送、`./bin/send_recv recv <interval>` 接收。

#### 4.3.3 源码精读

[demo/send_recv/CMakeLists.txt:1](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/CMakeLists.txt#L1)：定义 demo 的目标名为 `send_recv`。

[demo/send_recv/CMakeLists.txt:3-4](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/CMakeLists.txt#L3-L4)：把 `3rdparty` 加入包含目录（demo 里用到了第三方小工具头文件）。

[demo/send_recv/CMakeLists.txt:6-11](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/CMakeLists.txt#L6-L11)：收集源文件、生成可执行文件、链接 `ipc`。注意它**没有**手动写 `include_directories(.../include)`——因为 `ipc` 目标的 `PUBLIC` 包含目录会自动传递过来，这是 CMake「传递式依赖」的好处。

接下来看主程序，理解它怎么用：

[demo/send_recv/main.cpp:17-26](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L17-L26)：`do_send(size, interval)`——创建一个名为 `"ipc"`、角色为 `ipc::sender` 的 `ipc::channel`，循环构造一个长度为 `size` 的全 `'A'` 字符串，调用 `ipc.send(buffer, 0)` 发送，然后按 `interval` 毫秒休眠。

[demo/send_recv/main.cpp:28-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L28-L40)：`do_recv(interval)`——同样创建名为 `"ipc"` 的 `ipc::channel`，但角色是 `ipc::receiver`。两个进程用**同名字符串 `"ipc"`** 才能连上同一条通道。它循环调用 `ipc.recv(interval)` 接收，打印收到的字节数。

[demo/send_recv/main.cpp:44-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L44-L71)：`main` 解析命令行：
- `send_recv send <size> <interval>`：进入发送模式，`argv[2]` 是每条消息字节数，`argv[3]` 是发送间隔毫秒；
- `send_recv recv <interval>`：进入接收模式，`argv[2]` 是 `recv` 的等待超时毫秒。

所以验证通信需要开**两个终端**：一个跑 send，一个跑 recv。`argc < 3` 时直接返回 `-1`，所以参数给少了会静默退出。

> 这个 demo 里 `channel` 的具体 API（`ipc::sender`/`ipc::receiver`、`send`/`recv`/`buff_t`）会在 U1-L4「你的第一个 IPC 程序」里详细讲，本讲只关心怎么把它跑起来。

#### 4.3.4 代码实践

这是本讲的核心动手实践——**真正跑一次跨进程通信**。

1. **实践目标**：编译出 `send_recv` 并用两个进程验证收发。
2. **操作步骤**：
   - 配置（开启 demo）：

     ```bash
     cmake -DCMAKE_BUILD_TYPE=Release -DLIBIPC_BUILD_DEMOS=ON .
     ```
   - 编译：

     ```bash
     make -j
     ```
   - 确认可执行文件已生成：

     ```bash
     ls -la bin/send_recv
     ```
   - **终端 A**（先启动接收方，它会等待）：

     ```bash
     ./bin/send_recv recv 1000
     ```
   - **终端 B**（再启动发送方，每 500ms 发一条 64 字节消息）：

     ```bash
     ./bin/send_recv send 64 500
     ```
3. **需要观察的现象**：
   - 终端 A 持续打印 `recv waiting... 1`、`recv waiting... 2` ……，收到消息后打印 `recv size: 64`；
   - 终端 B 每隔 500ms 打印 `send size: 65`（64 字节内容 + 1 字节结尾符）。
4. **预期结果**：终端 B 发送的 64 字节消息被终端 A 正确接收并打印 `recv size: 64`，证明两个独立进程通过 libipc 成功交换了数据。按 `Ctrl+C` 可触发 [main.cpp:47-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L47-L54) 的信号处理，把 `is_quit__` 置真并 `disconnect()`，两个进程优雅退出。
5. 若你当前环境无法同时开两个交互终端（例如在某些自动化沙箱里），可改为后台运行：先 `./bin/send_recv recv 1000 > recv.log 2>&1 &`，再 `./bin/send_recv send 64 500`，之后查看 `recv.log` 确认收到 `recv size: 64`。具体输出形态待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`send_recv` 的发送方和接收方凭什么能连上「同一条通道」？

**参考答案**：它们在构造 `ipc::channel` 时传了**相同的名字字符串 `"ipc"`**（见 [main.cpp:18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L18) 与 [main.cpp:29](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L29)）。libipc 用这个名字定位/创建底层共享内存，名字相同即连到同一块共享内存，从而通信。

**练习 2**：把 `./bin/send_recv`（不带任何参数）直接运行会发生什么？为什么？

**参考答案**：程序直接返回 `-1` 退出，没有任何输出。因为 [main.cpp:45](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L45) 有 `if (argc < 3) return -1;`，参数不足会立即结束。正确用法必须给出模式与数值参数。

### 4.4 vcpkg 安装方式

#### 4.4.1 概念说明

除了手动用 CMake 从源码构建，libipc 还被收录进了微软的 [vcpkg](https://github.com/microsoft/vcpkg) 包管理器。vcpkg 是一个 C/C++ 的包管理工具（类似 Python 的 pip、Node 的 npm），你用它装一次，多个项目就能共享同一个编译好的库，不用每个项目都自己 `add_subdirectory` 源码。这对「只想用、不想看源码」的使用者最友好。

README 在中英文里都明确标注了这一行：`vcpkg install cpp-ipc`。

#### 4.4.2 核心流程

1. 先按 vcpkg 官方文档克隆并 bootstrap vcpkg（`./bootstrap-vcpkg.sh`）。
2. 执行 `vcpkg install cpp-ipc`，vcpkg 会自动拉取 cpp-ipc 源码、用 CMake 编译、把产物安装到 vcpkg 的统一目录。
3. 在你自己的项目里通过 CMake 的「工具链」方式（`-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake`）让 CMake 找到它。
4. `find_package` 或直接链接 `cpp-ipc::ipc` 即可。

#### 4.4.3 源码精读

本模块没有专门的源码文件，依据来自 README 的安装说明：

[README.md:18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L18)：英文版安装说明——`vcpkg install cpp-ipc`，并附上 vcpkg 仓库中 `ports/cpp-ipc` 的徽章链接。

[README.md:58](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L58)：中文版安装说明，同样是 `vcpkg install cpp-ipc`。

vcpkg 的 `ports/cpp-ipc` 配置本质上是把本讲的 `CMakeLists.txt` 用一套标准化参数重新跑了一遍，并产出带 `cpp-ipc::ipc` 命名空间的导出目标——这正是 [src/CMakeLists.txt:64-68](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L64-L68) 里 `install(EXPORT cpp-ipc-targets ... NAMESPACE cpp-ipc:: ...)` 提供的接口。所以「vcpkg 安装」和「手动安装」在底层是同一套机制。

#### 4.4.4 代码实践

1. **实践目标**：用 vcpkg 安装 libipc，并在一个最小项目里链接它。
2. **操作步骤**（需要你已装好 vcpkg；若没有可跳过，本实践偏环境依赖）：

   ```bash
   # 在 vcpkg 根目录
   ./vcpkg install cpp-ipc
   ```

   然后写一个最小 `CMakeLists.txt`（**示例代码**，不是项目原有文件）：

   ```cmake
   cmake_minimum_required(VERSION 3.10)
   project(my_app)
   set(CMAKE_CXX_STANDARD 17)
   find_package(cpp-ipc CONFIG REQUIRED)
   add_executable(my_app main.cpp)
   target_link_libraries(my_app PRIVATE cpp-ipc::ipc)
   ```

   配置时带上工具链文件：

   ```bash
   cmake -DCMAKE_TOOLCHAIN_FILE=<你的vcpkg>/scripts/buildsystems/vcpkg.cmake .
   cmake --build .
   ```
3. **需要观察的现象**：vcpkg 输出 `cpp-ipc:x64-linux` 安装成功；`find_package` 能找到 `cpp-ipc::ipc` 目标。
4. **预期结果**：你的 `my_app` 成功链接并编译，无需把 cpp-ipc 源码放进自己项目。
5. 若网络或 vcpkg 环境受限，此步骤可能失败，属正常情况，请改用手动 CMake 构建（4.3.4）。

#### 4.4.5 小练习与答案

**练习 1**：`vcpkg install cpp-ipc` 装好的库，和手动 `cmake/make/install` 装好的库，本质上有何联系？

**参考答案**：两者底层都跑的是本仓库的 `CMakeLists.txt` / `src/CMakeLists.txt`，都会产出带 `cpp-ipc::ipc` 命名空间的导出目标（见 `install(EXPORT ... NAMESPACE cpp-ipc::)`）。vcpkg 只是替你自动化了「下载源码 → 配置 → 编译 → 安装到统一目录」这套流程，并管理版本。

**练习 2**：什么时候你会选 vcpkg 安装，什么时候选手动从源码构建？

**参考答案**：只把 libipc 当作一个**依赖库来用**、且环境里已有 vcpkg 时，选 `vcpkg install` 最省事；如果你要**阅读/修改 libipc 源码、调试内部实现、或学习它的构建脚本**（比如本手册的目的），就应手动从源码构建并打开 `LIBIPC_BUILD_DEMOS`/`LIBIPC_BUILD_TESTS`，这样能拿到完整的源码树和可调试产物。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「从零到通信成功」的小任务：

1. 在项目根目录执行一次完整的配置 + 编译，**同时**开启 demo 和测试：

   ```bash
   cmake -DCMAKE_BUILD_TYPE=Release -DLIBIPC_BUILD_DEMOS=ON -DLIBIPC_BUILD_TESTS=ON .
   make -j
   ```
2. 编译完成后，核对产物：`lib/` 下应有 `libipc.a`，`bin/` 下应有 `send_recv` 和 `test-ipc`。
3. 参考 CI（[c-cpp.yml:21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/.github/workflows/c-cpp.yml#L21)），运行一次单元测试确认库本身工作正常：

   ```bash
   ./bin/test-ipc
   ```
4. 再用 `send_recv` 跑一次跨进程通信（开两个终端：`recv 1000` 与 `send 64 500`），确认能收到 `recv size: 64`。
5. 最后回答一个综合问题：这一整套流程里，`option()` 开关、`add_subdirectory`、`target_link_libraries(... ipc)` 分别在哪个环节发挥作用？（提示：开关决定构建什么，`add_subdirectory` 决定进入哪个目录，`target_link_libraries` 让 demo 用上库。）

通过这个任务，你会同时经历「配置选项 → 收集源码成库 → 链接构建 demo → 运行验证」的完整闭环，把本讲的四个模块全部走一遍。

## 6. 本讲小结

- libipc 用 CMake 构建，顶层 `CMakeLists.txt` 提供 `LIBIPC_BUILD_TESTS`/`LIBIPC_BUILD_DEMOS`/`LIBIPC_BUILD_SHARED_LIBS`/`LIBIPC_USE_STATIC_CRT`/`LIBIPC_CODECOV` 五个开关，**默认全 OFF**，开箱只构建库本体。
- `src/CMakeLists.txt` 把 `src/libipc` 下五个子目录的源码收集成名为 **`ipc`** 的库目标，默认是静态库（`lib/libipc.a`），并在非 Windows 平台自动链接 `pthread` 与 `rt`。
- 库的包含目录用 `PUBLIC` 暴露 `include/`，所以 demo 只需 `target_link_libraries(自己 ipc)` 即可继承头文件路径，无需手动指定。
- demo 放在 `demo/<名字>/` 子目录，`LIBIPC_BUILD_DEMOS=ON` 后由顶层逐个 `add_subdirectory`；`send_recv` 是最简单的收发示例，靠同名字符串 `"ipc"` 让两个进程连上同一条通道。
- 产物路径有规律：库进 `lib/`、可执行文件进 `bin/`；这与官方 CI 命令（`make -j`、`./bin/test-ipc`、`LD_LIBRARY_PATH=./lib`）完全对应。
- 除手动构建外，还可 `vcpkg install cpp-ipc` 一键安装，底层跑的是同一套 CMake 脚本，产出 `cpp-ipc::ipc` 导出目标。

## 7. 下一步学习建议

现在你已经能把 libipc 构建出来并跑通 `send_recv` 了，接下来的学习建议：

- **本单元剩余讲义**：建议接着读 U1-L3「目录结构与模块地图」，把 `src/libipc` 下 `circ`/`platform`/`mem`/`sync` 等子目录的职责摸清楚，为后面理解内部实现打基础；然后读 U1-L4「你的第一个 IPC 程序」，正式学习 `ipc::channel`/`route` 的 API 用法（本讲里 `send_recv` 用到的 `sender`/`receiver`/`send`/`recv` 会在那里展开）。
- **如果想深入构建系统**：可以仔细对比 `src/CMakeLists.txt` 的 `install(EXPORT ...)` 与 vcpkg 的 `ports/cpp-ipc`，理解一个 C++ 库如何被「打包」成可被 `find_package` 复用的形式。
- **如果想立刻动手**：试着在本讲基础上把 `LIBIPC_BUILD_SHARED_LIBS=ON` 重新构建一次动态库版本，运行 demo 时体会 `LD_LIBRARY_PATH` 的作用，这能帮你理解静态库与动态库在运行期依赖上的差别。
