# 目录结构与源码布局

## 1. 本讲目标

读完前两讲，你已经知道 **orc-rt 是什么**、以及它**怎么构建**。这一讲不写代码，只回答一个问题：

> 当你打开 orc-rt 仓库时，这么多文件和目录分别装着什么？我想看某一块逻辑，该去哪里找？

学完本讲，你应当能够：

- 区分 `include/orc-rt`（C++ API）、`include/orc-rt-c`（C ABI 边界）、`include/orc-rt-utils` 三层头文件的职责。
- 说出 `lib/executor` 下每一个 `.cpp` 大致属于哪一类，并理解 `sps-ci/` 子目录与 `Unix/*.inc` 平台文件的来历。
- 认出 `test/` 下的 unit / regression / tools 三类测试，以及 `tools/ogre` 是什么。
- 拿到一个功能点，能凭目录结构快速定位到对应源码。

本讲是「地图课」——后续每一讲都会反复引用这里的路径，所以请把目录布局先装进脑子里。

## 2. 前置知识

本讲几乎不需要 C++ 知识，但你需要先理解上一讲建立的几个概念：

- **controller / executor 二分**：orc-rt 是运行在 **executor（执行 JIT 代码的进程）** 里的运行时，它要同时给 C++ 调用者用，也要给纯 C 的 ABI 边界用。这就直接解释了为什么头文件要分两层。
- **编译期配置**：上一讲讲过 `config.h.in` 会被渲染成 `config.h`，所以仓库里有一个「模板」头文件，构建产物里才有一个「生成」的头文件。理解这一点，你才能看懂 `include/CMakeLists.txt` 里 `ORC_RT_GENERATED_HEADERS` 的存在。
- **runtimes 构建体系**：orc-rt 通过 LLVM runtimes 框架构建，最终产物是静态库目标 `orc-rt-executor`。本讲会指出哪些目录会编译进这个库、哪些只是测试/工具。

一个常被忽略的细节：orc-rt 仓库根目录并不是 LLVM 主仓库根目录，而是 `llvm-project/orc-rt/`。本讲所有路径都相对于这个子目录，永久链接也都指向 `orc-rt/` 之下。

## 3. 本讲源码地图

下面是本讲会精读的关键文件，它们都是「声明目录结构」的 CMake 文件：

| 文件 | 作用 |
|------|------|
| [CMakeLists.txt:L121-L135](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L121-L135) | 顶层 CMake，用 `add_subdirectory` 把 `docs / include / lib / tools / test` 五大目录串起来 |
| [include/CMakeLists.txt:L1-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L1-L48) | 把所有公共头文件登记成 `ORC_RT_HEADERS` 列表，并安装它们 |
| [lib/executor/CMakeLists.txt:L1-L22](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L1-L22) | 列出编译进 `orc-rt-executor` 静态库的全部 `.cpp` 源文件 |
| [lib/executor/CMakeLists.txt:L26-L39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L26-L39) | 根据日志后端条件性地追加日志实现文件，并定义静态库目标 |
| [test/CMakeLists.txt:L1-L49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L1-L49) | 定义 `check-orc-rt`（回归）与 `check-orc-rt-unit`（单元）两个测试入口 |

先看一眼整个仓库的顶层布局（相对 `orc-rt/` 根目录）：

```
orc-rt/
├── CMakeLists.txt     # 顶层构建入口
├── LICENSE.TXT        # Apache-2.0 WITH LLVM-exception
├── Maintainers.md     # 维护者名单
├── cmake/             # 本项目自带的 CMake 模块
├── docs/              # 文档（含 index.md / Design.md / 构建说明）
├── include/           # 公共头文件：orc-rt(C++) / orc-rt-c(C ABI) / orc-rt-utils
├── lib/               # 实现，只有 lib/executor 一个子目录
├── tools/             # 工具（ogre「空白执行器」）
└── test/              # 测试：unit / regression / tools
```

注意一个重要事实：**`lib/` 下只有一个 `executor/` 子目录**。这印证了上一讲的定位——orc-rt 只在 executor 侧运行，所以整个实现都叫 `executor`。下面三节分别展开 `include/`、`lib/executor/`、`test/tools/docs`。

## 4. 核心概念与源码讲解

### 4.1 头文件分层：C++ API 与 C ABI

#### 4.1.1 概念说明

orc-rt 的调用者有两种：

1. **C++ 调用者**：直接在 executor 进程里 `#include` 头文件、用 `orc_rt::` 命名空间下的类。这类调用需要完整的 C++ 类型（模板、`std::` 工具、RAII）。
2. **C 调用者 / ABI 边界**：跨语言、跨编译单元、或跨动态库的边界。这类调用只能用不透明的句柄（opaque handle）和纯 C 函数签名，不能暴露 C++ 类。

为了同时服务这两类调用者，`include/` 下分了三个子目录：

- `include/orc-rt/`：**C++ 公共 API**。数量最多，提供 `Session`、`Error`、`ExecutorAddr`、序列化、内存管理等完整的类与模板。
- `include/orc-rt-c/`：**C ABI 边界**。函数名都以 `orc_rt_` 开头，参数用 `orc_rt_SessionRef`、`orc_rt_ErrorRef` 这种不透明指针类型，任何 C 编译器都能链接。
- `include/orc-rt-utils/`：**构建期/工具用头文件**。这一层不参与运行时逻辑，是给构建脚本或辅助工具用的。

此外还有一个只在构建产物里才出现的头文件：`include/orc-rt-c/config.h`。它由模板 [include/orc-rt-c/config.h.in](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/config.h.in) 渲染而来（上一讲已讲），仓库里看不到它，但构建后会被安装。

#### 4.1.2 核心流程

头文件的「登记 + 安装」流程是这样的（伪代码）：

```
include/CMakeLists.txt:
  ORC_RT_HEADERS        = [ orc-rt/*.h, orc-rt-c/*.h, orc-rt/sps-ci/*.h, ... ]  # 手写列表
  ORC_RT_GENERATED_HEADERS = [ <build>/orc-rt-c/config.h ]                      # 渲染产物
  add_library(orc-rt-headers INTERFACE)            # 纯头文件库，不产出 .o
  target_include_directories(... INTERFACE include) # 让消费者能 #include 到这些目录
  set_property(... PUBLIC_HEADER ${ORC_RT_HEADERS} ${ORC_RT_GENERATED_HEADERS})
  install(TARGETS orc-rt-headers ...)              # 安装到系统 include 目录
```

关键点有三条：

1. **`INTERFACE` 库**：`orc-rt-headers` 不是普通静态库，它没有源文件，只负责「把 include 路径和头文件清单传给依赖它的目标」。后面你会看到 `orc-rt-executor` 和所有测试都 `target_link_libraries(... orc-rt-headers)`，就是为了拿到这些 include 路径。
2. **手写清单**：头文件不是自动扫描的，而是在 `ORC_RT_HEADERS` 列表里逐个写死。新增头文件必须手动加进这个列表，否则不会被安装（注释里也说明了「TODO: Switch to filesets when we move to cmake-3.23」，未来可能改成自动扫描）。
3. **生成头文件单独安装**：`config.h` 因为在构建目录里，要单独用 `install(FILES ...)` 装到 `include/orc-rt-c/`。

#### 4.1.3 源码精读

先看顶层 CMake 怎么把五大目录串起来——`include` 是第一个被加入的子目录：

```cmake
add_subdirectory(include)   # 头文件层
add_subdirectory(lib)       # 实现
add_subdirectory(tools)     # 工具
if(ORC_RT_INCLUDE_TESTS)
  add_subdirectory(test)    # 测试（可选）
endif()
```

这段在 [CMakeLists.txt:L129-L135](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L129-L135)。注意 `docs` 在更上面、由 `ORC_RT_INCLUDE_DOCS` 控制，而 `test` 由 `ORC_RT_INCLUDE_TESTS` 控制——也就是说**头文件和实现总是会构建，测试和文档是可选的**。

接着看 `include/CMakeLists.txt` 的头文件清单开头与结尾：

```cmake
set(ORC_RT_HEADERS
    orc-rt-c/Compiler.h
    orc-rt-c/CoreTyspe.h     # 注意：这里是源码里的实际拼写
    orc-rt-c/Error.h
    orc-rt-c/Logging.h
    orc-rt-c/WrapperFunction.h
    orc-rt-c/orc-rt.h
    orc-rt/AllocAction.h
    ...
    orc-rt/sps-ci/AllSPSCI.h
    orc-rt/sps-ci/CallSPSCI.h
    ...
)
```

完整列表见 [include/CMakeLists.txt:L1-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L1-L48)。从这份清单可以直接看出三层头文件的数量对比：

| 子目录 | 头文件数（约） | 内容性质 |
|--------|--------------|---------|
| `orc-rt-c/` | 6 个 + 1 个生成 | C ABI：`CoreTypes.h`、`Error.h`、`Logging.h`、`WrapperFunction.h`、`Session.h`、`Compiler.h`、`orc-rt.h`（总入口） |
| `orc-rt/` | 40+ 个 | C++ API：核心类 + 序列化 + 工具 |
| `orc-rt/sps-ci/` | 6 个 | Controller Interface 的 C++ 声明（与 `lib/executor/sps-ci/` 对应） |

而 `INTERFACE` 库和安装逻辑在 [include/CMakeLists.txt:L56-L74](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L56-L74)，它把 `ORC_RT_HEADERS` 和 `ORC_RT_GENERATED_HEADERS` 一起设为 `PUBLIC_HEADER` 属性，安装时一并拷走。

#### 4.1.4 代码实践

**实践目标**：用肉眼验证「三层头文件 + 一个生成头文件」的分层是否和源码一致。

**操作步骤**：

1. 打开 [include/CMakeLists.txt:L1-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L1-L48)。
2. 统计 `orc-rt-c/` 开头的条目数（应为 6 个：`Compiler.h`、`CoreTypes.h`、`Error.h`、`Logging.h`、`WrapperFunction.h`、`orc-rt.h`；注意列表里第 3 行拼写为 `CoreTyspe.h`，但磁盘上的真实文件名是 `CoreTypes.h`——以 `ls include/orc-rt-c/` 的结果为准）。
3. 再列出 `orc-rt/sps-ci/` 下的条目（应为 6 个）。
4. 查看 [include/CMakeLists.txt:L51-L53](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L51-L53)，确认 `ORC_RT_GENERATED_HEADERS` 指向构建目录下的 `orc-rt-c/config.h`。

**需要观察的现象**：仓库里 `include/orc-rt-c/` 目录下确实有 `config.h.in` 但**没有** `config.h`——后者只在构建后生成。

**预期结果**：三层目录的文件数与 `ORC_RT_HEADERS` 列表一致；`config.h` 是「构建产物」而非源码。如果你 `grep` 不到 `config.h` 而只找到 `config.h.in`，说明理解正确。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `orc-rt-c/` 层要用 `orc_rt_SessionRef` 这种不透明指针，而不是直接暴露 `orc_rt::Session` 类？

**参考答案**：因为 C ABI 要求「任何 C 编译器都能链接、布局稳定」。C++ 类的内存布局、vtable、名称修饰（name mangling）都依赖具体编译器和 ABI，无法跨编译器/跨语言保证。不透明指针把 C++ 对象藏在一层 `void*` 后面，C 端只拿句柄、调用纯 C 函数去操作，由 C++ 实现内部做 `wrap/unwrap` 转换。这样 orc-rt 的 ABI 才稳定。

**练习 2**：如果你新增了一个头文件 `include/orc-rt/Foo.h`，但忘了改 CMake，会发生什么？

**参考答案**：在构建期内通常仍能编译（因为 `target_include_directories` 把整个 `include/` 目录都加进了搜索路径，源码里 `#include "orc-rt/Foo.h"` 找得到）。但 `install` 时它**不会**被拷到安装目录，下游消费者就拿不到这个头文件。所以要同时把它加进 `ORC_RT_HEADERS` 列表。

---

### 4.2 lib/executor 实现目录与平台文件

#### 4.2.1 概念说明

`lib/` 下只有一个 `executor/` 子目录——再次印证 orc-rt 只服务 executor 侧。这里的 `.cpp` 文件就是运行时的全部实现，最终编译成静态库 `orc-rt-executor`。

实现目录里有三件值得注意的事：

1. **`sps-ci/` 子目录**：存放「Controller Interface」的处理器实现。`sps-ci` = Simple Packed Serialization Controller Interface，它把执行端的能力以「符号」的形式暴露给控制端跨进程调用。每个 `XxxSPSCI.cpp` 通常对应一个 `Xxx` 服务。
2. **`Unix/*.inc` 平台文件**：`NativeDylibAPIs.inc` 和 `NativeMemoryAPIs.inc` 不是独立编译单元，而是被 `.cpp` 文件 `#include` 进去的代码片段。它们封装 Unix（POSIX）特定的系统调用（`dlopen`/`mmap` 等），未来若有 Windows 版本，会放在 `lib/executor/Windows/` 下。
3. **条件编译的日志后端**：`Logging.cpp` 是日志主逻辑，`Logging_printf.cpp` 与 `Logging_oslog.cpp` 是两个可选后端，只有当 CMake 选项 `ORC_RT_LOG_BACKEND` 分别为 `printf` 或 `os_log` 时才会被加入编译。

#### 4.2.2 核心流程

`lib/executor/CMakeLists.txt` 用一个 `set(files ...)` 列表 + 条件追加，组装出静态库的全部源文件：

```
set(files [ 20 个基础 .cpp ])                          # 核心 + 错误 + 资源 + 控制接口
if(LOG_BACKEND == "printf")  list(APPEND Logging_printf.cpp)
if(LOG_BACKEND == "os_log")  list(APPEND Logging_oslog.cpp)
add_library(orc-rt-executor STATIC ${files})
target_link_libraries(orc-rt-executor PUBLIC orc-rt-headers)   # 拿到 include 路径
target_compile_options(... PRIVATE ${ORC_RT_COMPILE_FLAGS})    # RTTI/异常开关，不传染消费者
```

这里的两个细节呼应上一讲：

- `target_link_libraries(... PUBLIC orc-rt-headers)`：`PUBLIC` 意味着不仅 `orc-rt-executor` 自己能用这些头文件，**链接它的下游**也能用——但下游不会继承 `ORC_RT_COMPILE_FLAGS`，因为编译选项是 `PRIVATE`。
- `STATIC`：orc-rt 默认编成静态库，executor 程序直接把它链进去。

至于平台 `.inc` 文件，它们不进 `files` 列表（因为不是独立编译单元），而是在 `.cpp` 里被直接 `#include`：

```
NativeDylibManager.cpp:    #include "Unix/NativeDylibAPIs.inc"
SimpleNativeMemoryMap.cpp: #include "Unix/NativeMemoryAPIs.inc"
```

这种「`.cpp` 内嵌平台代码片段」的写法，让平台差异留在实现内部，对外头文件保持干净。

#### 4.2.3 源码精读

先看 `lib/executor/CMakeLists.txt` 的完整源文件清单（这是本讲最重要的一个文件）：

```cmake
set(files
  BootstrapInfo.cpp
  Environment.cpp
  Error.cpp
  ExecutorProcessInfo.cpp
  InProcessControllerAccess.cpp
  Logging.cpp
  NativeDylibManager.cpp
  RTTI.cpp
  Service.cpp
  Session.cpp
  SimpleNativeMemoryMap.cpp
  SimpleSymbolTable.cpp
  StandaloneMachOUnwindInfoRegistrar.cpp
  ThreadPoolRunner.cpp
  sps-ci/AllSPSCI.cpp
  sps-ci/CallSPSCI.cpp
  sps-ci/MemoryAccessSPSCI.cpp
  sps-ci/NativeDylibManagerSPSCI.cpp
  sps-ci/SimpleNativeMemoryMapSPSCI.cpp
  sps-ci/StandaloneMachOUnwindInfoRegistrarSPSCI.cpp
  )
```

见 [lib/executor/CMakeLists.txt:L1-L22](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L1-L22)。一共 20 个基础文件：14 个在 `lib/executor/` 根下，6 个在 `sps-ci/` 子目录下。

接着是条件追加日志后端，并定义静态库：

```cmake
# The printf logging backend needs a runtime implementation; the none backend
# is header-only and the os_log backend is not yet implemented.
if(ORC_RT_LOG_BACKEND STREQUAL "printf")
  list(APPEND files Logging_printf.cpp)
elseif(ORC_RT_LOG_BACKEND STREQUAL "os_log")
  list(APPEND files Logging_oslog.cpp)
endif()

add_library(orc-rt-executor STATIC ${files})
target_link_libraries(orc-rt-executor
  PUBLIC orc-rt-headers
  )

# Apply RTTI and exceptions compile flags
target_compile_options(orc-rt-executor PRIVATE ${ORC_RT_COMPILE_FLAGS})
```

见 [lib/executor/CMakeLists.txt:L24-L39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L24-L39)。注释特别说明：`none` 后端是纯头文件的（日志被编译掉），所以不需要 `.cpp`；`printf` 需要运行时实现。

再看平台 `.inc` 是怎么被嵌进去的。在 `SimpleNativeMemoryMap.cpp` 第 23 行：

```cpp
#include "Unix/NativeMemoryAPIs.inc"
```

见 [lib/executor/SimpleNativeMemoryMap.cpp:L23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/SimpleNativeMemoryMap.cpp#L23)。`NativeDylibManager.cpp` 同理，在 [lib/executor/NativeDylibManager.cpp:L19](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/NativeDylibManager.cpp#L19) 引入了 `Unix/NativeDylibAPIs.inc`。

最后看一眼 `sps-ci/` 与实现的对应关系——你会发现每个 `XxxSPSCI.cpp` 都对应一个核心服务，命名是高度规整的：

| 核心 `.cpp` | 对应的 `sps-ci/` 处理器 | 含义 |
|-------------|----------------------|------|
| （无，由 Session 提供） | `CallSPSCI.cpp` | 通用 `call_void_void` / `call_main` 入口 |
| `SimpleNativeMemoryMap.cpp` | `SimpleNativeMemoryMapSPSCI.cpp` | 内存分配/保护 |
| `NativeDylibManager.cpp` | `NativeDylibManagerSPSCI.cpp` | 动态库加载/查找 |
| `StandaloneMachOUnwindInfoRegistrar.cpp` | `StandaloneMachOUnwindInfoRegistrarSPSCI.cpp` | 注册 unwind 信息 |
| （聚合） | `AllSPSCI.cpp` + `MemoryAccessSPSCI.cpp` | 把各子模块处理器汇总注册 |

`AllSPSCI` 顾名思义是「把所有 SPS-CI 处理器聚合到一起」的入口，控制器只要拿到这一个符号表就能调到所有执行端能力。

#### 4.2.4 代码实践

**实践目标**：把 `lib/executor/` 下根目录的 14 个 `.cpp`（不含 `sps-ci/`、不含条件日志后端）按职责归入五类，建立「文件名 → 职责」的直觉。这是本讲的主实践任务。

**操作步骤**：

1. 打开 [lib/executor/CMakeLists.txt:L1-L22](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L1-L22)，把根目录下的 14 个 `.cpp` 抄下来。
2. 对每个文件，读它的**顶部注释**（通常一行话说明用途）来归类。例如 `Error.cpp` 开头写明「Contains the implementation of APIs in the orc-rt/Error.h and orc-rt-c/Error.h headers」。
3. 把它们填进下表的「你的归类」列（先自己填，再对答案）。

**需要观察的现象**：绝大多数 `.cpp` 的顶部注释都直接写明它实现了哪个头文件的 API，所以归类有明确依据，不是猜的。

**预期结果**：参考答案见下一节 4.2.5 的分类表。

#### 4.2.5 小练习与答案

**练习（即主实践任务的参考答案）**：把根目录 14 个 `.cpp` 归入「核心 Session / 错误与 RTTI / 资源服务 / 序列化与控制接口 / 平台与日志」五类。

**参考答案**：

| 类别 | 文件 | 依据 |
|------|------|------|
| **核心 Session** | `Session.cpp`、`Service.cpp` | `Session` 是执行端根对象；`Service` 是资源管理抽象，被 Session 拥有 |
| **错误与 RTTI** | `Error.cpp`、`RTTI.cpp` | `Error.cpp` 实现错误处理 API；`RTTI.cpp` 实现自建 RTTI（兼容 `-fno-rtti`） |
| **资源服务** | `SimpleNativeMemoryMap.cpp`、`SimpleSymbolTable.cpp`、`NativeDylibManager.cpp`、`StandaloneMachOUnwindInfoRegistrar.cpp`、`BootstrapInfo.cpp`、`ExecutorProcessInfo.cpp` | 都是具体的 Service：内存、符号表、动态库、unwind 信息；`BootstrapInfo`/`ExecutorProcessInfo` 提供连接时的引导数据与进程信息 |
| **序列化与控制接口** | `InProcessControllerAccess.cpp` | 同进程的 ControllerAccess 实现（跨进程桥）；其余控制接口在 `sps-ci/` 下 |
| **平台与日志** | `Logging.cpp`、`Environment.cpp`、`ThreadPoolRunner.cpp` | `Logging.cpp` 是日志主逻辑；`Environment.cpp` 提供 `secureGetenv`（被日志读取 `ORC_RT_LOG` 环境变量时使用）；`ThreadPoolRunner.cpp` 依赖 OS 线程，是平台相关的并发基础设施 |

> 分类说明：上表把 `Environment.cpp` 和 `ThreadPoolRunner.cpp` 放进「平台与日志」是一种合理但非唯一的归类。`Environment.cpp` 也可视为通用工具（它只提供一个安全读环境变量的函数），`ThreadPoolRunner.cpp` 也可单列「并发」一类。分类的目的是建立定位直觉，不必强求唯一解——重要的是你能凭文件名 + 顶部注释快速找到代码。
>
> 另外注意：`Logging_printf.cpp` 与 `Logging_oslog.cpp` 是**条件编译**的日志后端，没有出现在基础 14 个里，只有当 `ORC_RT_LOG_BACKEND` 选 `printf`/`os_log` 时才会追加编译。

**追问练习**：`sps-ci/` 子目录下的 6 个文件为什么单独放一个子目录，而不是和核心 `.cpp` 平级？

**参考答案**：因为它们属于同一类职责——「把执行端能力以 SPS 序列化的符号形式暴露给控制端」。单独成目录，既让命名规整（`Xxx` + `XxxSPSCI` 一一对应），也方便后续单独理解「控制接口」这一整层（见第 6 单元）。聚合文件 `AllSPSCI.cpp` 也放这里，作为整个子模块的总入口。

---

### 4.3 测试、工具与文档目录

#### 4.3.1 概念说明

orc-rt 的 `test/` 目录分成性质完全不同的三块：

- **`test/unit/`**：单元测试，基于 **GoogleTest**。每个 `XxxTest.cpp` 对应一个被测模块，验证函数级行为。这是数量最多、最贴近源码的测试。
- **`test/regression/`**：回归测试，基于 **lit + FileCheck**（LLVM 的测试基础设施）。用 `.test` 文件描述「运行某工具、检查输出」。
- **`test/tools/`**：测试支持工具。这不是被测对象，而是「给回归测试当样本程序」的小可执行文件（如 `orc-rt-smoke-check`、`orc-rt-log-check`）。

注意区分两个名字相近的目录：

- `tools/`（仓库顶层）：正式工具，目前只有 `ogre`。
- `test/tools/`：测试用辅助工具，不随运行时安装。

而 `docs/` 是 Sphinx 文档源，包含 `index.md`、`Design.md`、`Building-orc-rt.md`、`ErrorHandling.md`、`CodingConventions.md` 等，正是前面几讲反复引用的内容。

#### 4.3.2 核心流程

测试的组织在 `test/CMakeLists.txt` 里一目了然，它定义了两个 lit 测试套件目标：

```
if (ORC_RT_LLVM_TOOLS_AVAILABLE)
  configure_lit_site_cfg(...)            # 配置回归测试的 lit
  add_subdirectory(tools)                # 构建测试支持工具
  add_custom_target(orc-rt-test-depends ...)   # 收集回归依赖
  add_lit_testsuite(check-orc-rt ...) )  # ← 回归测试入口
else()
  message(WARNING "... regression tests disabled.")
endif()

add_subdirectory(unit)                   # 构建单元测试
add_lit_testsuite(check-orc-rt-unit ...) # ← 单元测试入口
```

两个入口的区别很关键：

- **`check-orc-rt`**（回归）：依赖 LLVM 工具链（FileCheck、not 等）可用，由 `ORC_RT_LLVM_TOOLS_AVAILABLE` 把关；不可用时会打印警告并跳过。
- **`check-orc-rt-unit`**（单元）：只要 GoogleTest（`llvm_gtest`）可用就能构建；它带 `EXCLUDE_FROM_CHECK_ALL`，意味着不会自动随 `check-all` 一起跑，需要显式调用。

单元测试本身在 `test/unit/CMakeLists.txt` 里被打包成一个 `CoreTests` 可执行，链接 `orc-rt-executor`：

```
add_orc_rt_unittest(CoreTests
  AllocActionTest.cpp
  BitmaskEnumTest.cpp
  ...                          # 几十个 *Test.cpp
  DISABLE_LLVM_LINK_LLVM_DYLIB
)
target_link_libraries(CoreTests PRIVATE orc-rt-executor)
```

#### 4.3.3 源码精读

先看 `test/CMakeLists.txt` 里两个测试入口的定义：

```cmake
add_lit_testsuite(check-orc-rt "Running the ORC-RT regression tests"
  ${CMAKE_CURRENT_BINARY_DIR}/regression
  DEPENDS ${ORC_RT_TEST_DEPS}
  )
```

这是回归入口，见 [test/CMakeLists.txt:L23-L26](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L23-L26)。

```cmake
add_lit_testsuite(check-orc-rt-unit "Running orc-rt unittest suites"
  ${CMAKE_CURRENT_BINARY_DIR}/unit
  EXCLUDE_FROM_CHECK_ALL
  DEPENDS OrcRTUnitTests)
```

这是单元入口，见 [test/CMakeLists.txt:L45-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L45-L48)。注意 `EXCLUDE_FROM_CHECK_ALL`。

再看单元测试如何聚合——`test/unit/CMakeLists.txt` 把所有 `*Test.cpp` 一次性塞进一个 `CoreTests` 目标：

```cmake
add_orc_rt_unittest(CoreTests
  AllocActionTest.cpp
  ...
  SessionTest.cpp
  ...
  span-test.cpp
  DISABLE_LLVM_LINK_LLVM_DYLIB
)
target_compile_options(CoreTests PRIVATE ${ORC_RT_COMPILE_FLAGS})
target_link_libraries(CoreTests PRIVATE orc-rt-executor)
```

见 [test/unit/CMakeLists.txt:L14-L66](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L14-L66)。从中能看出测试命名的两种风格：

- **PascalCase + `Test` 后缀**：`SessionTest.cpp`、`ErrorTest.cpp`、`SimpleSymbolTableTest.cpp`——对应被测的 C++ 类。
- **kebab-case + `-test` 后缀**：`bind-test.cpp`、`span-test.cpp`、`move_only_function-test.cpp`、`scope_exit-test.cpp`——对应被测的小工具（`include/orc-rt/` 下的 lowercase 头文件）。

这种命名对应关系是「凭测试名找被测代码」的捷径：看到 `move_only_function-test.cpp`，就知道它在测 `include/orc-rt/move_only_function.h`。

最后看测试支持工具的定位。`orc-rt-smoke-check.cpp` 顶部注释直白说明了它的唯一用途：

```cpp
// A minimal regression-test tool. It exists only to smoke-check that the ORC
// runtime regression test-tool infrastructure works end to end: that a tool
// under test/tools is built, placed where lit can find it, and that its output
// can be matched by a regression test.
```

它和 `orc-rt-log-check` 一起定义在 [test/tools/CMakeLists.txt:L4-L10](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/tools/CMakeLists.txt#L4-L10)，注释明确说它们「不随运行时安装」。

而顶层正式工具 `ogre` 的定位则在 [tools/ogre/ogre.cpp:L8-L11](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/tools/ogre/ogre.cpp#L8-L11)：

```cpp
// The ORC Generic Runtime Environment (OGRE) is intended as a canonical
// "blank executor".
```

目前它的 `main` 只是 `return 0;`——一个「空白执行器」骨架，将来会作为标准的 executor 程序模板。

#### 4.3.4 代码实践

**实践目标**：用「测试名 ↔ 被测源码」的对应规律，反向定位被测文件。

**操作步骤**：

1. 打开 [test/unit/CMakeLists.txt:L14-L64](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L14-L64)。
2. 对下面 4 个测试文件，猜出它测试的是哪个头文件/源文件：
   - `TaskGroupTest.cpp`
   - `ExecutorAddressTest.cpp`
   - `move_only_function-test.cpp`
   - `SimpleNativeMemoryMapSPSCITest.cpp`
3. 用 `ls include/orc-rt/` 和 `ls lib/executor/` 验证你的猜测。

**需要观察的现象**：测试文件名与被测文件名几乎一一对应（PascalCase 对应类、kebab-case 对应小写工具头、`...SPSCITest` 对应 `sps-ci/` 下的处理器）。

**预期结果**：

| 测试文件 | 被测对象 |
|---------|---------|
| `TaskGroupTest.cpp` | `include/orc-rt/TaskGroup.h` |
| `ExecutorAddressTest.cpp` | `include/orc-rt/ExecutorAddress.h` |
| `move_only_function-test.cpp` | `include/orc-rt/move_only_function.h` |
| `SimpleNativeMemoryMapSPSCITest.cpp` | `lib/executor/sps-ci/SimpleNativeMemoryMapSPSCI.cpp`（及对应头） |

#### 4.3.5 小练习与答案

**练习 1**：为什么单元测试目标 `CoreTests` 要 `target_link_libraries(CoreTests PRIVATE orc-rt-executor)`，而不用 `PUBLIC`？

**参考答案**：因为 `CoreTests` 是最终的可执行测试程序，没有「下游消费者」会再链接它。`PUBLIC` 和 `PRIVATE` 对最终可执行目标的实际效果几乎一样，但用 `PRIVATE` 更准确地表达了「这个依赖只用于构建我自己，不向外传播」的语义。这和 `orc-rt-executor` 自己 `PUBLIC orc-rt-headers`（要向下游传播 include 路径）形成对比。

**练习 2**：`check-orc-rt`（回归）和 `check-orc-rt-unit`（单元）哪个更可能在没有 LLVM 工具链的环境下跑不起来？为什么？

**参考答案**：`check-orc-rt`（回归）。因为回归测试依赖 lit、FileCheck、`not` 等 LLVM 工具，`test/CMakeLists.txt` 用 `ORC_RT_LLVM_TOOLS_AVAILABLE` 检测它们是否可用，不可用就直接禁用回归测试并打印警告；而单元测试只依赖 GoogleTest（`llvm_gtest`），门槛更低。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「目录导航」：

**任务**：假设你想理解「执行端如何把一段 JIT 内存分配出来并改成可执行权限」，请从目录结构出发，定位到所有相关文件，并画出它们的关系。

**步骤**：

1. 从 4.2 的分类表锁定资源服务相关文件：`lib/executor/SimpleNativeMemoryMap.cpp`（实现）、`include/orc-rt/SimpleNativeMemoryMap.h`（C++ API）。
2. 注意它 `#include "Unix/NativeMemoryAPIs.inc"`（见 [SimpleNativeMemoryMap.cpp:L23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/SimpleNativeMemoryMap.cpp#L23)），定位到平台代码 `lib/executor/Unix/NativeMemoryAPIs.inc`。
3. 因为内存属性要用 `MemProt`/`AllocGroup` 描述，顺藤摸到 `include/orc-rt/MemoryFlags.h`。
4. 想看它如何暴露给控制端，找到 `lib/executor/sps-ci/SimpleNativeMemoryMapSPSCI.cpp` 与 `include/orc-rt/sps-ci/SimpleNativeMemoryMapSPSCI.h`。
5. 想看行为验证，找到 `test/unit/SimpleNativeMemoryMapTest.cpp`（本地调用）和 `test/unit/SimpleNativeMemoryMapSPSCITest.cpp`（跨接口调用）。
6. 把上述文件画成一张关系图：`MemoryFlags.h`（数据）→ `SimpleNativeMemoryMap.{h,cpp}`（Service）→ `Unix/NativeMemoryAPIs.inc`（系统调用）→ `SimpleNativeMemoryMapSPSCI.{h,cpp}`（对外符号）→ `*Test.cpp`（验证）。

**预期产出**：一张覆盖「数据描述 / 实现 / 平台 / 控制接口 / 测试」五个目录的文件关系图。**待本地验证**：如果你本地已构建，可运行 `check-orc-rt-unit` 并观察 `SimpleNativeMemoryMapTest` 是否通过。

## 6. 本讲小结

- orc-rt 仓库顶层分为 `docs / include / lib / tools / test` 五大目录，由顶层 [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L129-L135) 串起；`lib/` 下只有 `executor/`，印证「只在 executor 侧运行」。
- 头文件分三层：`include/orc-rt/`（C++ API，40+ 个）、`include/orc-rt-c/`（C ABI，6 个 + 1 个生成的 `config.h`）、`include/orc-rt-utils/`（工具用）；登记在 [include/CMakeLists.txt:L1-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L1-L48) 的 `ORC_RT_HEADERS` 列表里。
- `lib/executor/` 的 20 个基础 `.cpp` 编译成静态库 `orc-rt-executor`；日志后端 `Logging_printf/oslog.cpp` 按 `ORC_RT_LOG_BACKEND` 条件追加；平台代码以 `Unix/*.inc` 形式被 `#include` 进 `.cpp`。
- `sps-ci/` 子目录是「Controller Interface」层，命名规整：每个 `XxxSPSCI.cpp` 对应一个核心服务，`AllSPSCI.cpp` 是聚合入口。
- 测试分三类：`test/unit/`（GoogleTest 单元）、`test/regression/`（lit/FileCheck 回归）、`test/tools/`（测试支持工具）；入口是 `check-orc-rt-unit` 与 `check-orc-rt`。
- 测试文件名与被测源码一一对应（PascalCase 对应类，kebab-case 对应小写工具头），这是「凭测试名找源码」的捷径。

## 7. 下一步学习建议

你已经掌握了 orc-rt 的「地图」。接下来建议：

- 进入**第 2 单元（核心心智模型）**，先读 [u2-l1 Controller–Executor 架构全景](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md)，把本讲看到的 `Session.cpp`、`sps-ci/`、`InProcessControllerAccess.cpp` 这些散点串成一张架构图。
- 在阅读后续讲义时，随时回看本讲的分类表，确认自己能立刻定位到对应文件。
- 想提前感受「真实扩展点」的读者，可以扫一眼 [lib/executor/StandaloneMachOUnwindInfoRegistrar.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/StandaloneMachOUnwindInfoRegistrar.cpp) 及其 `sps-ci` 配对文件，这是「一个服务 + 一个控制接口」的标准范式，第 11 单元会手把手教你照着写一个。
