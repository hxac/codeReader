# 平台检测与后端分派

## 1. 本讲目标

libipc 是一个跨平台库：同一份源码要在 Linux、Windows、FreeBSD（以及 QNX、macOS、Android）上都能编译运行。但共享内存、互斥量、信号量这些底层能力，每个操作系统的系统调用完全不同——Linux 用 `shm_open`/`futex`，Windows 用 `CreateFileMapping`/`Win32`。

本讲要解决的问题是：**libipc 是如何用「一份源码」覆盖「多个平台」的？**

读完本讲，你将能够：

1. 看懂 `detect_plat.h` 如何用预处理宏判断当前是哪个 OS、哪个编译器、哪种 CPU 指令集。
2. 理解 `platform.cpp` 如何依据 OS 宏，在编译期把不同的共享内存后端（`shm_posix.cpp` / `shm_win.cpp`）「拼」进来。
3. 认识 Linux 上引入的 a0（AlephZero）辅助库，以及它为什么必须用 `.c` 文件编译。
4. 理解 `detail.h` 这个 C++ 版本兼容「垫片」层的作用。

本讲是上一单元共享内存 `shm::handle`（u5-l1）的「地基下钻」：u5-l1 讲的是共享内存的公共 API，本讲讲的是这些 API 的实现究竟由哪一段平台代码提供。

## 2. 前置知识

阅读本讲前，你需要了解：

- **预处理宏（preprocessor macro）**：`#if`/`#elif`/`#else`/`#endif` 在编译前生效，可以按条件保留或删除某段代码。`defined(XXX)` 判断某个宏是否被定义。
- **编译器内置宏**：像 `__linux__`、`_WIN32`、`__APPLE__`、`__GNUC__`、`_MSC_VER` 这些是编译器/平台自带的预定义宏，无需你手动 `#define`，它们会被自动设置，用来标识当前环境。
- **C 与 C++ 混合编译**：C 文件（`.c`）用 C 编译器编译，C++ 文件（`.cpp`）用 C++ 编译器编译。C++ 要调用 C 的代码，需要 `extern "C"` 声明；反之同理。
- **PIMPL / 不透明句柄**：u5-l1 已讲过 `shm::handle` 用 `id_t`（即 `void*`）隐藏平台差异——上层只看到统一类型，底层具体用什么实现，正是本讲要揭开的「后端分派」。

如果你对「为什么跨平台库需要分平台」没有概念，可以记住一句话：**底层系统调用没有标准 C++ 答案，只能在编译期分流到各平台各自的实现。**

## 3. 本讲源码地图

本讲涉及四个文件，都在平台层，构成一条「自下而上」的依赖链：

| 文件 | 作用 | 语言 |
|------|------|------|
| [include/libipc/imp/detect_plat.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h) | 平台检测「宪法」：定义 `LIBIPC_OS_*`/`LIBIPC_CC_*`/`LIBIPC_INSTR_*` 等一套统一的平台标识宏 | 头文件库 |
| [src/libipc/platform/detail.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h) | C++ 版本兼容垫片层，并引入 detect_plat.h | C++/头文件 |
| [src/libipc/platform/platform.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.cpp) | 共享内存后端分派入口：按 OS 宏 `#include` 不同的 `.cpp` | C++ |
| [src/libipc/platform/platform.c](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.c) | C 后端分派入口：仅在 Linux 上引入 a0 辅助库的 `.c` 实现 | C |

依赖关系：`detect_plat.h` 是最底层（被所有人 include）；`detail.h` include 了它；`platform.cpp` / `platform.c` 都先 include `detail.h`（从而间接得到平台宏），再做后端分派。

## 4. 核心概念与源码讲解

### 4.1 detect_plat.h：平台检测宏

#### 4.1.1 概念说明

每个编译器都会预定义一批宏来表明「我在为哪个平台编译」。但这些宏名字五花八门：Windows 可能同时定义 `_WIN32`、`_WIN64`、`__WIN32__`；Linux 定义 `__linux__`；GCC 定义 `__GNUC__`；MSVC 定义 `_MSC_VER`。

libipc 不想在业务代码里到处写 `#if defined(_WIN32) || defined(__WIN32__) ...`，那样既啰嗦又容易漏。它的做法是：**在 `detect_plat.h` 里把这些五花八门的「原始宏」统一翻译成一套自己的、命名规范的 `LIBIPC_*` 宏**。之后全库只用自己这套宏判断平台。

这就像给各国货币设一个统一汇率表：原始宏是各国货币，`LIBIPC_OS_*` 是统一换算后的内部货币。

#### 4.1.2 核心流程

`detect_plat.h` 按「OS → 编译器 → 指令集 → 字节序 → C++ 版本 → 特性适配」的顺序，逐块用 `#if/#elif/#error` 翻译宏。每块都遵循同一个套路：

```
#if defined(原始宏1) || defined(原始宏2) ...
# define LIBIPC_统一宏
#elif ...
...
#else
# error "This ... is unsupported."   # 都不匹配就编译报错
#endif
```

这套「逐级 `#elif` + 兜底 `#error`」保证了：要么命中一个已知平台，要么编译直接失败——绝不会「静默地选错平台」。

#### 4.1.3 源码精读

**① OS 检测**——这是本讲后续分派的依据。[detect_plat.h:10-32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L10-L32) 把各种 OS 原始宏翻译成 `LIBIPC_OS_WINCE`/`LIBIPC_OS_WIN64`/`LIBIPC_OS_WIN32`/`LIBIPC_OS_FREEBSD`/`LIBIPC_OS_QNX`/`LIBIPC_OS_APPLE`/`LIBIPC_OS_ANDROID`/`LIBIPC_OS_LINUX`/`LIBIPC_OS_POSIX`：

```c
#if defined(WINCE) || defined(_WIN32_WCE)
# define LIBIPC_OS_WINCE
#elif defined(WIN64) || defined(_WIN64) || ...
# define LIBIPC_OS_WIN64
...
#elif defined(__linux__) || defined(__linux)
# define LIBIPC_OS_LINUX
...
```

注意判断顺序：Windows 的 64/32 位要先判断，因为 `_WIN64` 隐含 `_WIN32` 也被定义（MSVC 的历史约定）。随后 [detect_plat.h:34-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L34-L37) 把三个 Windows 变体**再聚合成一个总开关 `LIBIPC_OS_WIN`**，这是后续分派真正用到的宏：

```c
#if defined(LIBIPC_OS_WIN32) || defined(LIBIPC_OS_WIN64) || defined(LIBIPC_OS_WINCE)
# define LIBIPC_OS_WIN
#endif
```

**② 编译器检测**——[detect_plat.h:41-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L41-L54) 区分 MSVC（`LIBIPC_CC_MSVC`）与 GCC/Clang（`LIBIPC_CC_GNUC`/`LIBIPC_CC_CLANG`）。注意 GCC 段里还顺带定义了 MSVC 各版本号常量（2015/2017/2019/2022），供后面按版本启用特性。

**③ 指令集检测**——[detect_plat.h:59-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L59-L74) 判断 CPU 架构（`LIBIPC_INSTR_X64`/`LIBIPC_INSTR_X86`/`LIBIPC_INSTR_ARM64` 等），随后 [detect_plat.h:76-80](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L76-L80) 聚合成 `LIBIPC_INSTR_X86_64` / `LIBIPC_INSTR_ARM`。指令集信息主要用于选择正确的自旋暂停指令（如 `pause`）和缓存行大小——这些与并发性能强相关（u8-l1 会用到）。

**④ 字节序与 C++ 版本**——[detect_plat.h:84-90](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L84-L90) 判断大小端；[detect_plat.h:94-108](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L94-L108) 按标准 `__cplusplus` 值定义 `LIBIPC_CPP_20`/`LIBIPC_CPP_17`/`LIBIPC_CPP_14`，三档都没有则 `#error`。

**⑤ 特性适配宏**——为了让同一份代码在不同编译器/版本下都能用上现代特性，[detect_plat.h:121-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L121-L228) 用 `__has_cpp_attribute`/`__has_builtin` 优雅降级，把 `[[fallthrough]]`、`[[likely]]`、`__builtin_expect` 等包装成统一的 `LIBIPC_FALLTHROUGH`/`LIBIPC_LIKELY` 等宏。例如 `LIBIPC_UNUSED` 在 GCC 下用 `__attribute__((__unused__))`，在 MSVC 下用 `__pragma(warning(suppress:...))`，都没有时就定义为空。

#### 4.1.4 代码实践

**实践目标**：找出本机编译时 `detect_plat.h` 到底命中了哪些 `LIBIPC_*` 宏。

**操作步骤**：

1. 在仓库根目录写一个最小测试文件 `/tmp/probe.cpp`（示例代码，**不要**写进项目目录）：

   ```cpp
   #include "libipc/imp/detect_plat.h"
   #include <cstdio>
   int main() {
   #if defined(LIBIPC_OS_LINUX)
       std::puts("OS = LINUX");
   #elif defined(LIBIPC_OS_WIN)
       std::puts("OS = WIN");
   #elif defined(LIBIPC_OS_FREEBSD)
       std::puts("OS = FREEBSD");
   #endif
   #if defined(LIBIPC_CC_GNUC)
       std::puts("CC = GCC/Clang");
   #elif defined(LIBIPC_CC_MSVC)
       std::puts("CC = MSVC");
   #endif
   #if defined(LIBIPC_CPP_17)
       std::puts("C++ = 17+");
   #endif
   }
   ```

2. 在 Linux 上编译运行（需把 `include/` 加入头文件路径）：

   ```bash
   g++ -std=c++17 -Iinclude /tmp/probe.cpp -o /tmp/probe && /tmp/probe
   ```

**需要观察的现象**：程序打印出 `OS = LINUX`、`CC = GCC/Clang`、`C++ = 17+` 三行。

**预期结果**：在 Linux + GCC/Clang + C++17 环境下，`LIBIPC_OS_LINUX`、`LIBIPC_CC_GNUC`、`LIBIPC_CPP_17` 三个宏被定义。换到 Windows MSVC 则会打印 `OS = WIN`、`CC = MSVC`。（本机若非上述环境，输出相应改变。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LIBIPC_OS_WIN64` 的判断要写在 `LIBIPC_OS_WIN32` 之前？

**答案**：因为 MSVC 在编译 64 位程序时会**同时**定义 `_WIN64` 和 `_WIN32`（`_WIN32` 表示「Windows NT 系」而非「32 位」）。若把 32 位判断放前面，64 位环境会被错误归类为 `LIBIPC_OS_WIN32`，先判 64 位才能命中正确分支。

**练习 2**：如果在一台 libipc 不认识的全新 OS 上编译，会发生什么？

**答案**：OS 检测块的所有 `#elif` 都不命中，走到 [detect_plat.h:30-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/detect_plat.h#L30-L31) 的 `#error "This OS is unsupported."`，编译直接失败并给出明确错误信息，而不是生成错误代码。

---

### 4.2 platform.cpp：共享内存后端分派

#### 4.2.1 概念说明

u5-l1 讲的 `ipc::shm::handle`，它的 `acquire`/`get`/`release` 等函数最终要落到具体的系统调用上。但 POSIX 用 `shm_open`+`mmap`，Windows 用 `CreateFileMapping`+`MapViewOfFile`，两套 API 毫无共性可言。

libipc 的做法是：**写两份独立实现文件**（`posix/shm_posix.cpp` 和 `win/shm_win.cpp`），它们对外导出**完全相同的函数签名**（都定义在 `namespace ipc::shm` 下），再用 `platform.cpp` 这个「分派器」在编译期只 `#include` 其中一份。这样上层调用 `ipc::shm::acquire(...)` 时，链接到的实现就是当前平台的那一份。

#### 4.2.2 核心流程

`platform.cpp` 本身几乎没有「代码」，它就是一个**文本拼接器**：

```
platform.cpp
  ├── 先 include detail.h（拿到 LIBIPC_OS_* 宏）
  ├── 若 LIBIPC_OS_WIN      → #include "win/shm_win.cpp"
  ├── 若 LINUX/QNX/FREEBSD  → #include "posix/shm_posix.cpp"
  └── 否则                   → #error
```

注意它是用 `#include` 直接「吃掉」一个 `.cpp` 文件——这相当于把那个 `.cpp` 的全部内容原样粘贴到 `platform.cpp` 里一起编译。因为受 `#if` 保护，最终只会粘贴进一份。

#### 4.2.3 源码精读

`platform.cpp` 全文只有几行，却完成了整个共享内存后端的选择。[platform.cpp:2-9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.cpp#L2-L9)：

```cpp
#include "libipc/platform/detail.h"          // 引入平台宏
#if defined(LIBIPC_OS_WIN)
#include "libipc/platform/win/shm_win.cpp"   // Windows 后端
#elif defined(LIBIPC_OS_LINUX) || defined(LIBIPC_OS_QNX) || defined(LIBIPC_OS_FREEBSD)
#include "libipc/platform/posix/shm_posix.cpp" // POSIX 后端
#else/*IPC_OS*/
#   error "Unsupported platform."
#endif
```

要点解读：

- 第 2 行先 include `detail.h`，间接引入 `detect_plat.h`，从而拿到 `LIBIPC_OS_*` 宏（这是判断的前提）。
- 第 3-4 行：Windows 走 `shm_win.cpp`（基于 `CreateFileMapping`）。
- 第 5-6 行：Linux/QNX/FreeBSD 三者共享同一套 POSIX 实现 `shm_posix.cpp`（基于 `shm_open`/`mmap`）——它们都遵循 POSIX 共享内存规范，所以能复用一份代码。
- 第 8 行：其余平台直接 `#error`，保证不会「漏网」。

被分派的两份后端，函数签名完全一致。以 `acquire` 为例，POSIX 版在 [shm_posix.cpp:47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L47) 是 `id_t acquire(char const * name, std::size_t size, unsigned mode)`；Windows 版 [shm_win.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp) 导出同名同参的 `acquire`。两者内部数据结构不同（POSIX 用 `fd_`，Windows 用 `HANDLE h_`），但对外的 `id_t`（u5-l1 的不透明句柄）一致——这正是分派能成立的基础：**统一接口，各自实现。**

#### 4.2.4 代码实践

**实践目标**：追踪本机（Linux）上 `platform.cpp` 到底选择了哪个 shm 后端，并验证 POSIX 后端确实被编译进来。

**操作步骤**：

1. 确认本机平台宏：按 4.1.4 的实验，确认打印出 `OS = LINUX`，即 `LIBIPC_OS_LINUX` 被定义。
2. 回到 `platform.cpp` 第 5 行，可知 `defined(LIBIPC_OS_LINUX)` 为真，故命中 `#include "libipc/platform/posix/shm_posix.cpp"`。
3. 进一步在 `shm_posix.cpp` 里看它实际调用的系统调用：`shm_open`（[shm_posix.cpp:47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L47) 起的 `acquire` 函数体）、`ftruncate`、`mmap`、`shm_unlink`（函数顶部已 include `<sys/mman.h>`、`<fcntl.h>`）。
4. （可选）用预处理器查看实际拼接结果：

   ```bash
   g++ -std=c++17 -Iinclude -Isrc -E -dI src/libipc/platform/platform.cpp 2>/dev/null \
     | grep -E 'shm_posix|shm_win' | head
   ```

**需要观察的现象**：第 4 步的预处理输出里，能看到 `shm_posix.cpp` 被 include 进来，而 `shm_win.cpp` 因为 `#if` 为假被完全剔除（不会出现）。

**预期结果**：Linux 上只有 `posix/shm_posix.cpp` 进入编译单元，`win/shm_win.cpp` 被条件编译排除。这印证了「编译期分派」。若无法运行 `-E`，可标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Linux、QNX、FreeBSD 能共用同一份 `shm_posix.cpp`，而 Windows 必须单独一份？

**答案**：Linux/QNX/FreeBSD 都遵循 POSIX 标准，提供 `shm_open`/`mmap`/`shm_unlink` 这套统一的共享内存 API；而 Windows 没有 POSIX 共享内存概念，用的是 `CreateFileMapping`/`MapViewOfFile`，API 完全不同，无法复用，故必须单独写一份 `shm_win.cpp`。

**练习 2**：`platform.cpp` 用 `#include "xxx.cpp"` 这种「包含实现文件」的写法，和把两份后端分别编译成单独的 `.cpp` 再二选一，相比有什么好处和坏处？

**答案**：好处是简单——只用一个 `.cpp` 入口，靠 `#if` 决定内容，不需要在构建脚本（CMake）里按平台列不同的源文件清单。坏处是「include 实现」违背了头文件/实现分离的惯例，会让 IDE 和新手困惑，且被 include 的 `.cpp` 不能再被单独加入编译列表（否则重复定义）。libipc 用 `aux_source_directory` 只收集 `platform.cpp` 这一个入口、不收集后端 `.cpp`，避免了重复编译。

---

### 4.3 platform.c 与 a0 辅助库

#### 4.3.1 概念说明

除了共享内存，libipc 还需要**跨进程健壮互斥量**（robust mutex）——即一个进程持锁时崩溃，另一个进程能感知并恢复锁（这是 u6-l2/u8-l2 的主题）。Linux 上实现 robust mutex 最可靠的底层是 **futex**（快速用户态互斥）。

libipc 没有自己从零写 futex 互斥量，而是**直接内嵌（vendor）了一个名为 a0（AlephZero）的开源 C 库**。这个库专门为 IPC 设计了一个 robust、进程共享、基于 futex 的互斥量 `a0_mtx_t`。它放在 [src/libipc/platform/linux/a0/](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0) 目录下。

a0 是一个**纯 C 库**（`.c`/`.h` 文件），而 libipc 主体是 C++。把 C 代码混进 C++ 项目有个坑：如果用 C++ 编译器去编 C 代码，某些 C 写法会出问题。所以 libipc 用了一个专门的 `.c` 文件 `platform.c` 来「收编」a0，让它被 **C 编译器**编译。

#### 4.3.2 核心流程

```
platform.c（由 CMake 用 C 编译器编译，因为后缀是 .c）
  ├── 先 include detail.h（拿到 LIBIPC_OS_*；该头对 C 也可用）
  ├── 若 LIBIPC_OS_LINUX  → #include 五个 a0 的 .c 实现文件
  │                        （err.c / mtx.c / strconv.c / tid.c / time.c）
  ├── 若 WIN / QNX / FREEBSD → 什么都不做（空）
  └── 否则 → #error
```

为什么 a0 的 `.c` 要由 `platform.c` 来 include，而不是各自单独编译？因为这样它们彼此之间的内部链接、静态变量、`extern` 声明都处在一个编译单元里，C 库作者原本就是按「一起编译」设计的，保持其原始结构最稳妥。

#### 4.3.3 源码精读

[platform.c:2-12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.c#L2-L12) 全文：

```c
#include "libipc/platform/detail.h"
#if defined(LIBIPC_OS_WIN)
#elif defined(LIBIPC_OS_LINUX)
#include "libipc/platform/linux/a0/err.c"
#include "libipc/platform/linux/a0/mtx.c"
#include "libipc/platform/linux/a0/strconv.c"
#include "libipc/platform/linux/a0/tid.c"
#include "libipc/platform/linux/a0/time.c"
#elif defined(LIBIPC_OS_QNX) || defined(LIBIPC_OS_FREEBSD)
#else/*IPC_OS*/
#   error "Unsupported platform."
#endif
```

要点：

- 第 3 行 Windows 分支**留空**：Windows 不用 a0，它的健壮锁用 Win32 原生 abandoned mutex（u6-l2）。
- 第 4-9 行 Linux 分支把五个 a0 实现文件 include 进来，其中最关键的是 `mtx.c`（互斥量实现）。
- 第 10 行 QNX/FreeBSD 也留空：它们走 pthread robust mutex（见 `posix/mutex.h`），不用 a0。

为什么 a0 只在 Linux 上用？因为 a0 的互斥量直接调用 Linux 内核的 futex 系统调用（[mtx.c:10](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L10) `#include <linux/futex.h>`、第 15 行 `#include <syscall.h>`），这是 Linux 专属。a0 的头文件 [a0/mtx.h:19-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L19-L38) 明确注释了这个互斥量「类似 `pthread_mutex_t`，但固定了进程共享、robust、错误检查、优先级继承等属性」，且时间用 `CLOCK_BOOTTIME`。

C/C++ 混合的关键证据：a0 的每个头文件都用 `extern "C"` 包裹（如 [a0/err.h:6-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/err.h#L6-L8)），这样 libipc 的 C++ 代码（如 `linux/mutex.h` 里的 `robust_mutex` 类，[linux/mutex.h:16-17](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L16-L17) `#include "a0/mtx.h"`）就能正确链接到这些 C 符号。

> 提醒：互斥量本身的实现细节（EOWNERDEAD 恢复、robust list 等）属于 u6-l2/u8-l2，本讲只关注「a0 这个 C 库是如何被分派、收编进编译的」。

#### 4.3.4 代码实践

**实践目标**：验证 `platform.c` 确实把 a0 的 C 代码编译进了 libipc，且是用 C 编译器编译的。

**操作步骤**：

1. 在 Linux 上用 u1-l2 的方法构建库（`-DLIBIPC_BUILD_DEMOS=ON`）。
2. 构建完成后，检查静态库里是否含 a0 的符号：

   ```bash
   nm <build>/lib/libipc.a | grep -E 'a0_mtx_(lock|unlock)' | head
   ```

3. 若想确认它是 C 编译而非 C++ 编译，可看符号名：C 符号是未修饰的 `a0_mtx_lock`，C++ 符号会被「名字修饰」（mangle）成类似 `_ZN...` 的乱码。

**需要观察的现象**：第 2 步能看到 `a0_mtx_lock`、`a0_mtx_unlock` 等符号，且名字是**未修饰的纯 C 名字**（没有 C++ mangling），说明它们由 C 编译器产出。

**预期结果**：`nm` 列出未修饰的 `a0_mtx_*` 符号，证明 a0 的 C 实现经由 `platform.c` 被编入 `libipc.a`。若环境没有 `nm` 或未构建库，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `platform.c` 文件后缀是 `.c` 而不是 `.cpp`？改成 `.cpp` 会怎样？

**答案**：因为 a0 是纯 C 库，要用 C 编译器编译才能保持其语义（C 与 C++ 在类型转换、关键字、链接约定上有差异）。CMake 按文件后缀自动选择编译器：`.c` 用 C 编译器。若改成 `.cpp`，a0 的 `.c` 会被当 C++ 编译，可能触发类型错误或符号名被 mangle，导致链接失败。

**练习 2**：a0 库只在 `LIBIPC_OS_LINUX` 分支里被引入，QNX/FreeBSD/Windows 都不引入。那这三个平台的健壮锁靠什么？

**答案**：Windows 靠 Win32 原生互斥量的 abandoned 状态检测；QNX/FreeBSD 靠 pthread 的 robust mutex 属性（见 `posix/mutex.h`，用 `pthread_mutex_t`）。它们不需要 a0，所以对应分支留空。

---

### 4.4 detail.h：C++ 版本兼容层

#### 4.4.1 概念说明

`detail.h` 是平台层内部使用的「公共头」，它做两件事：

1. **引入 `detect_plat.h`**：让所有平台层代码只需 include 一个 `detail.h` 就拿到全部平台宏。
2. **抹平 C++14 / C++17 的语法差异**：libipc 最低支持 C++14，但代码里想用一些 C++17 才有的便利写法（如结构化绑定）。`detail.h` 提供了一组「垫片宏」和「降级实现」，在 C++17 下用标准写法，在 C++14 下退化等价写法。

这样业务代码就能统一用 `IPC_STBIND_(a, b, expr)` 这种宏，不用关心当前是 C++14 还是 17。

#### 4.4.2 核心流程

`detail.h` 的逻辑分两段，用 `#if defined(__cplusplus)` 和 `__cplusplus >= 201703L` 两道开关切分：

```
detail.h
  ├── #include "libipc/imp/detect_plat.h"   （C/C++ 都执行）
  ├── 若是 C++：
  │     ├── IPC_STBIND_ / IPC_CONSTEXPR_  按 C++17 与否给两套定义
  │     └── 在 namespace ipc::detail 里：
  │           ├── C++17：直接 using std::unique_ptr / unique_lock / max / min ...
  │           └── C++14：自定义等价的 deduction guide / max / min 模板
```

#### 4.4.3 源码精读

**① 引入平台宏**——[detail.h:4](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L4) `#include "libipc/imp/detect_plat.h"`，这是 `platform.cpp`/`platform.c` 能拿到 `LIBIPC_OS_*` 的根源。

**② 结构化绑定垫片**——[detail.h:25-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L25-L39)：

```cpp
#if __cplusplus >= 201703L
#define IPC_STBIND_(A, B, ...) auto [A, B] = __VA_ARGS__   // C++17 结构化绑定
#define IPC_CONSTEXPR_   constexpr
#else
#define IPC_STBIND_(A, B, ...) \
    auto tp___ = __VA_ARGS__; auto A = std::get<0>(tp___); auto B = std::get<1>(tp___)
#define IPC_CONSTEXPR_ inline                                 // C++14 退化为 inline
#endif
```

`IPC_STBIND_(a, b, foo())` 的意思是「把 `foo()` 的返回值拆成 `a`、`b` 两个变量」。C++17 用原生结构化绑定一行搞定；C++14 没有这个语法，就用 `std::get<0>/<1>` 从 tuple 里取，等价但啰嗦。上层代码写一次，两个版本都能编。

**③ 智能指针/算法的降级别名**——[detail.h:47-94](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L47-L94)：C++17 直接 `using std::unique_ptr` 等；C++14 因为类模板参数推导（CTAD）不完善，手写了等价的工厂函数。例如 C++14 的 `unique_ptr` 工厂 [detail.h:62-65](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L62-L65)：

```cpp
template <typename T>
constexpr auto unique_ptr(T* p) noexcept {
    return std::unique_ptr<T> { p };
}
```

还有带圆括号的 `(max)`/`(min)` [detail.h:84-92](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L84-L92)，圆括号是为了避免与某些系统头文件里的 `max`/`min` 宏冲突（经典 Windows `windows.h` 问题）。

注意 [detail.h:7](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/detail.h#L7) `#if defined(__cplusplus)` 的保护：`detail.h` 也被 `platform.c`（C 文件）include，C++ 段在 C 下会被整段跳过，只保留第 4 行的 `detect_plat.h` 引入——所以 C 文件也能安全使用它。

#### 4.4.4 代码实践

**实践目标**：体会 `IPC_STBIND_` 在 C++14 与 C++17 下的等价性。

**操作步骤**：

1. 写一个最小程序（示例代码）：

   ```cpp
   #include "libipc/platform/detail.h"
   #include <tuple>
   #include <cstdio>
   int main() {
       auto tp = std::make_tuple(1, 2);
       IPC_STBIND_(a, b, tp);
       std::printf("%d %d\n", a, b);
   }
   ```

2. 分别用 C++14 和 C++17 编译运行：

   ```bash
   g++ -std=c++14 -Iinclude -Isrc /tmp/sbind.cpp -o /tmp/sbind14 && /tmp/sbind14
   g++ -std=c++17 -Iinclude -Isrc /tmp/sbind.cpp -o /tmp/sbind17 && /tmp/sbind17
   ```

**需要观察的现象**：两个版本都能编译通过，且都打印 `1 2`。

**预期结果**：`IPC_STBIND_` 在 C++14 下展开成 `std::get` 取值、在 C++17 下展开成结构化绑定，行为完全一致——这就是兼容层的价值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `detail.h` 里要 `#if defined(__cplusplus)` 把整段 C++ 代码包起来？

**答案**：因为 `detail.h` 被 `platform.c`（C 文件）include，C 编译器看不懂 `namespace`、`template`、`std::` 等 C++ 语法。`#if defined(__cplusplus)` 保证 C 编译时只保留第 4 行的 `#include "detect_plat.h"`，跳过所有 C++ 内容。

**练习 2**：`IPC_CONSTEXPR_` 在 C++17 下是 `constexpr`，在 C++14 下为什么退化成 `inline`？

**答案**：C++14 的 `constexpr` 规则比 C++17 严格（例如对 static 成员的 inline/constexpr 处理不同）。为了在两个版本下都安全地定义「可内联的常量/函数」，C++17 直接用 `constexpr`，C++14 退化为 `inline` 以避免潜在的链接或语义问题。

---

## 5. 综合实践

**任务**：画一张「平台检测 → 后端分派」的全景图，把本讲的四个模块串起来。

具体要求：

1. 在一张图里画出从「编译器预定义宏」（如 `__linux__`、`_WIN32`）开始，经 `detect_plat.h` 翻译成 `LIBIPC_OS_LINUX`/`LIBIPC_OS_WIN`，再到 `platform.cpp` 选择 `shm_posix.cpp`/`shm_win.cpp`、`platform.c`（仅 Linux）引入 a0 的完整链路。
2. 在图上标注：**本机（Linux）实际走的是哪条路径**，对应的 shm 后端用到的系统调用（`shm_open`/`mmap`）是什么、a0 引入了哪几个 `.c` 文件。
3. 写一段 200 字以内的说明：如果要把 libipc 移植到一个**新的 POSIX 兼容 OS**（假设它支持 `shm_open` 但不支持 futex），你需要改哪几个文件、各改什么。

**参考答案要点**（用于自查）：

- 新 OS 在 `detect_plat.h` 的 OS 检测块加一条 `#elif` 定义一个新的 `LIBIPC_OS_XXX`。
- `platform.cpp` 的 POSIX 分支 `#elif` 条件里加上 `|| defined(LIBIPC_OS_XXX)`，让它复用 `shm_posix.cpp`。
- `platform.c` 因为新 OS 不支持 futex，**不能**走 a0 分支；需让它的健壮锁改用 pthread robust（参考 `posix/mutex.h`），即在 `platform.c` 给新 OS 留空（像 QNX/FreeBSD 那样），并在互斥量头文件分派时让它用 pthread 版本。
- `detail.h` 一般无需改动（除非新 OS 编译器有特殊行为）。

## 6. 本讲小结

- `detect_plat.h` 是全库的平台「宪法」：把各编译器五花八门的原始宏统一翻译成 `LIBIPC_OS_*`/`LIBIPC_CC_*`/`LIBIPC_INSTR_*`，并用 `#elif + #error` 保证「要么命中已知平台，要么编译失败」。
- `platform.cpp` 是共享内存后端的**编译期分派器**：靠 `#if defined(LIBIPC_OS_*)` 在编译期只 `#include` 一份后端（Windows→`shm_win.cpp`，Linux/QNX/FreeBSD→`shm_posix.cpp`），两份后端对外接口一致、内部实现各异。
- `platform.c` 专门收编 **a0（AlephZero）C 库**：仅在 Linux 上引入五个 a0 的 `.c` 文件（含 futex 互斥量 `mtx.c`），用 C 编译器编译，为 libipc 提供跨进程健壮锁；其他平台留空。
- `detail.h` 是平台层的公共头兼 **C++14/17 兼容垫片**：引入 `detect_plat.h`，并用 `IPC_STBIND_`/`IPC_CONSTEXPR_` 等宏与降级别名抹平版本差异；`#if defined(__cplusplus)` 让它能被 C 文件安全 include。
- 整套机制的本质是「**统一接口，各自实现，编译期分流**」：上层只见 `ipc::shm::acquire` 这样的统一 API，平台差异被 `#if` 完全隔离在平台层内部。

## 7. 下一步学习建议

本讲解开了「共享内存 API 由哪段平台代码实现」，但还没展开那段实现本身的细节。建议按以下顺序继续：

1. **u5-l3 POSIX 共享内存后端**：深入 `shm_posix.cpp`，看 `shm_open`/`ftruncate`/`mmap`/`shm_unlink` 的具体用法、嵌入式引用计数的放置位置。
2. **u5-l4 Windows 共享内存后端**：对比 `shm_win.cpp` 的 `CreateFileMapping`/`MapViewOfFile`，理解 `Global\\` 前缀跨会话命名。
3. **u6-l2 跨进程健壮互斥量**：本讲提到的 a0 `mtx.c` 在那里详细展开，看 `EOWNERDEAD` 恢复、robust list 机制。
4. 如果对 a0 库本身感兴趣，可读 [src/libipc/platform/linux/a0/README.md](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/README.md) 了解它的设计哲学（robust、futex、共享内存 IPC）。
