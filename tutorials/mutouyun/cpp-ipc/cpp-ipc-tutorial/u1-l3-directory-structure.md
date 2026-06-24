# 目录结构与模块地图

## 1. 本讲目标

libipc 是一个「跨平台、跨进程」的库，源码横跨公共接口、核心算法、共享内存、同步原语、平台后端、内存分配六大领域，散落在十几个目录里。第一次打开仓库很容易迷失：到底哪个文件才是「入口」？哪个目录是「跨平台胶水」？哪个又是「纯算法」？

本讲不写任何业务代码，只解决一件事：**建立一张「目录 → 模块」的地图**。学完后你应当能够：

- 闭着眼睛说出 `include/libipc/*.h` 里 8 个公共头文件各自管什么；
- 区分 `src/libipc` 下哪些目录会被**编译成机器码**、哪些只是**头文件**被 `#include`；
- 看懂 `platform/` 目录如何用 `linux / posix / win` 三个子目录做「平台分派」；
- 知道 `test/` 和 `demo/` 各自扮演什么角色，以及它们靠哪些 CMake 开关才会被构建出来。

这张地图是后续所有讲义的「导航底图」——以后每读到一篇讲义，你都能立刻在脑子里定位到对应的目录。

## 2. 前置知识

### 2.1 头文件库（header-only）与编译单元

C++ 项目里有两类源文件：

- **`.cpp` 源文件**：每个 `.cpp` 是一个「翻译单元」，会被编译器单独编译成目标文件（`.o`），最后链接成库（`.a` / `.so` / `.dll`）。
- **`.h` / `.hpp` 头文件**：本身不被单独编译，而是被 `.cpp` 用 `#include` 「粘贴」进去，随宿主 `.cpp` 一起编译。

libipc 大量采用「头文件库」风格：很多核心数据结构（循环队列、空闲链表、无锁栈）整个写在 `.h` 里，靠 C++ 模板在**使用处**才实例化。所以你会看到一些目录里**只有 `.h`、没有 `.cpp`**——这不是遗漏，而是有意为之。这一点直接决定了后面 `src/CMakeLists.txt` 里「哪些目录进编译列表、哪些不进」。

### 2.2 平台分派（platform dispatch）

「跨平台」不是魔法，本质就是：**同一份逻辑接口，在不同操作系统下调用不同的系统 API**。libipc 的做法是：

1. 在 `platform/` 下为每个操作系统建一个子目录（`posix/`、`linux/`、`win/`）；
2. 每个子目录里放**同名**的头文件（如三个目录都有 `mutex.h`、`condition.h`）；
3. 用编译宏（`LIBIPC_OS_*`）判断当前平台，把对应子目录加入「头文件搜索路径」。

于是 `#include "mutex.h"` 在 Linux 下找到的是 `platform/linux/mutex.h`，在 Windows 下找到的是 `platform/win/mutex.h`。调用方代码完全一样，差异被隔离在子目录里。

### 2.3 PIMPL 与不透明句柄

libipc 的公共 API 大量使用一个技巧：把真正的实现细节藏在 `.cpp` 里，对外只暴露一个 `void*` 句柄（`ipc::handle_t = void*`）。这样头文件可以保持稳定、不暴露内部结构。你会看到 `include/libipc/` 里的头文件大多是「接口声明」，真正的「肉」在 `src/libipc/` 的同名 `.cpp` 里。

---

## 3. 本讲源码地图

本讲涉及的「导航锚点」文件如下。它们本身不是业务逻辑，而是**组织业务逻辑的脚手架**：

| 文件 / 目录 | 角色 | 本讲用途 |
| --- | --- | --- |
| `CMakeLists.txt`（顶层） | 仓库总入口的构建脚本 | 看它如何决定「构建什么」 |
| `src/CMakeLists.txt` | 库本体的构建脚本 | 看它如何收集 `src/` 下的源码 |
| `test/CMakeLists.txt` | 测试可执行文件的构建脚本 | 看它如何收集 `test/` 下的测试 |
| `include/libipc/` | 公共 API 头文件目录 | 列出对用户暴露的全部接口 |
| `src/libipc/` | 库内部实现目录 | 区分编译单元与头文件库 |
| `demo/` | 示例程序目录 | 看可运行的最小用法 |
| `3rdparty/` | 第三方依赖目录 | 看库的「零依赖」承诺边界 |

永久链接前缀（本讲所有链接均基于此 HEAD）：

```
https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/
```

---

## 4. 核心概念与源码讲解

### 4.1 include 公共 API：用户唯一需要 include 的地方

#### 4.1.1 概念说明

`include/` 是整个仓库里**唯一**会被安装到用户机器、被用户代码 `#include` 的目录（见顶层 CMake 的 `install(DIRECTORY "include/")`）。换句话说，它是 libipc 对外承诺的「公共契约」。你写的应用只要 `#include "libipc/ipc.h"`，就拿到了全部能力。

`include/libipc/` 顶层只有 8 个头文件，每一个对应一个「对外能力域」：

| 头文件 | 能力域 | 一句话职责 |
| --- | --- | --- |
| `ipc.h` | 通道收发 | `channel`/`route`、`chan_impl`、`chan_wrapper`、`send/recv` 主接口 |
| `def.h` | 基础类型 | `byte_t`、`uint_t`、`relat/trans/wr` 策略标签、超时与尺寸常量 |
| `buffer.h` | 消息容器 | `buff_t`（即 `buffer`），承载一条消息的数据与析构回调 |
| `shm.h` | 共享内存 | `ipc::shm::handle`，直接操作一块跨进程共享内存 |
| `mutex.h` | 互斥锁 | 跨进程健壮互斥量 |
| `condition.h` | 条件变量 | 配合 mutex 的等待/通知 |
| `semaphore.h` | 信号量 | 跨进程计数信号量 |
| `rw_lock.h` | 读写锁 | 自旋读写锁 + `yield/sleep` 退避工具 |

此外 `include/libipc/` 下还有两个**子目录**，属于「公共但偏底层」的工具：

- `concur/`：目前只有 `intrusive_stack.h`（无锁侵入式栈），是无锁基础设施。
- `mem/`：内存分配器公共头（`memory_resource.h`、`bytes_allocator.h`、`block_pool.h`、`central_cache_*.h`、`container_allocator.h`、`new.h`）。

还有一个 `imp/` 子目录（17 个头文件，如 `detect_plat.h`、`log.h`、`fmt.h`、`expected.h` 等），是库内部使用的基础设施工具。它虽然放在公共 `include/` 下，但**普通用户不需要直接 include**——你可以把它当成「库自己的 STL」。

> 判断「公共 vs 内部」的经验法则：如果你在一个新项目里只想发消息收消息，只需 `#include "libipc/ipc.h"`（它会级联拉入 `def.h`/`buffer.h`/`shm.h`）。`mem/`、`concur/`、`imp/` 留给进阶讲义再碰。

#### 4.1.2 核心流程

公共头文件之间的依赖呈「自底向上」的金字塔：

```text
imp/        ← 基础设施（export、log、detect_plat、byte ...）
  ↑
def.h       ← 类型与策略标签（最底层业务概念）
  ↑
buffer.h    ← 消息容器，依赖 def.h
shm.h       ← 共享内存句柄，依赖 def.h
mutex.h / condition.h / semaphore.h / rw_lock.h  ← 同步原语，依赖 imp
  ↑
ipc.h       ← 顶层入口，聚合 def/buffer/shm，提供 channel/route
```

关键点：`ipc.h` 处于金字塔顶端，是「门面（facade）」。你 `#include` 它，就等于把整座金字塔都拉了进来。

#### 4.1.3 源码精读

先看 `ipc.h` 如何聚合下层头文件——这是「公共 API 顶层入口」的凭证：

- [include/libipc/ipc.h:1-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L1-L8)：`ipc.h` 顶部依次 include 了 `export.h`、`def.h`、`buffer.h`、`shm.h`，自己不做底层定义，只做聚合。

再看 `def.h` 如何定义「最底层类型与常量」——这是整个金字塔的地基：

- [include/libipc/def.h:13-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L13-L24)：定义 `byte_t` 与 `uint_t<N>`（按位宽选择整数类型），全库的类型基石。

- [include/libipc/def.h:28-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L28-L39)：定义全局常量，如 `default_timeout = 100`（毫秒）、`data_length = 64`（单条消息分片大小）、`large_msg_limit`、`large_msg_align = 1024`、`central_cache_default_size = 1MB`。后面 U3 讲分片、U7 讲内存缓存时，这些常量会反复出现。

最后看 `ipc.h` 如何把句柄收发接口集中在一个模板 `chan_impl<Flag>` 里：

- [include/libipc/ipc.h:20-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L20-L48)：`chan_impl<Flag>` 声明了 `connect / send / recv / disconnect / clear_storage` 等全部静态接口——这就是「公共 API 的全部动作清单」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手把 8 个公共头文件归类到「能力域」。
2. **操作步骤**：
   - 打开 `include/libipc/` 目录，列出 8 个 `.h` 文件。
   - 对每个文件，读它的**顶部注释或第一个 namespace 内的声明**，判断它属于「收发」「内存」「同步」「类型」哪一类。
3. **需要观察的现象**：你会发现 `ipc.h` 并不自己定义 `buffer`，而是 `#include "libipc/buffer.h"` 后写 `using buff_t = buffer;`（见 [include/libipc/ipc.h:10-13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L10-L13)）。
4. **预期结果**：你能填出本节那张 8 行的能力域表格，且理解 `ipc.h` 是聚合入口。
5. 运行结果：本任务为纯阅读，无需编译，**「待本地验证」仅指你可自行打开文件核对**。

#### 4.1.5 小练习与答案

**练习 1**：如果你只想在两个进程间收发字符串，最少需要 `#include` 哪个头文件？

> 答案：只需 `#include "libipc/ipc.h"`。它会级联拉入 `def.h`、`buffer.h`、`shm.h`，足够你用 `ipc::channel` 收发。

**练习 2**：`ipc::handle_t` 的底层类型是什么？为什么用这种类型？

> 答案：是 `void*`（见 [include/libipc/ipc.h:12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L12)）。用 `void*` 是 PIMPL 思路——对外只给一个不透明指针，真正的实现结构藏在 `.cpp` 里，避免暴露内部细节、也保证 ABI 稳定。

---

### 4.2 src 核心与内部分层：库真正的「肉」在哪里

#### 4.2.1 概念说明

`include/` 只负责「承诺」，`src/` 才负责「兑现」。库的全部运行时逻辑——消息分片、无锁队列、共享内存映射、锁与信号量——都写在 `src/libipc/` 下。

`src/libipc/` 内部又分成几个职责清晰的层次。为了便于记忆，我们把它画成「核心 + 五个分层」：

| 层 | 目录 | 职责 | 是否含 `.cpp`（会被编译） |
| --- | --- | --- | --- |
| **核心** | `src/libipc/`（顶层） | 收发主链路、队列抽象、无锁算法、等待器 | 是（`ipc.cpp`、`buffer.cpp`、`shm.cpp`） |
| **循环数组** | `src/libipc/circ/` | 循环数组底座与连接位图 | 否（纯头文件） |
| **内存** | `src/libipc/mem/` | 自研分配器子系统 | 是 |
| **同步** | `src/libipc/sync/` | mutex/condition/semaphore/waiter 的实现 | 是 |
| **平台** | `src/libipc/platform/` | 操作系统后端分派 | 是（`platform.cpp/.c`） |
| **工具** | `src/libipc/utility/` | id_pool、pimpl、concept 等小工具 | 否（纯头文件） |
| **基础设施实现** | `src/libipc/imp/` | fmt/codecvt/system/nameof 的实现 | 是 |

> 一条关键观察：`circ/` 和 `utility/` **没有任何 `.cpp`**——它们是「头文件库」。这正好呼应前置知识 2.1：模板化的算法写在头文件里，在使用处实例化。`src/CMakeLists.txt` 的源码收集方式会严格体现这一点。

#### 4.2.2 核心流程

源码从「散落在目录」到「变成一个 `libipc.a`」要经过 `src/CMakeLists.txt` 的两道收集工序：

```text
① aux_source_directory(.../src/libipc        SRC_FILES)   ┐
   aux_source_directory(.../src/libipc/sync   SRC_FILES)   │ 只收 .cpp：
   aux_source_directory(.../src/libipc/platform SRC_FILES) │ → 这些目录产生机器码
   aux_source_directory(.../src/libipc/imp    SRC_FILES)   │
   aux_source_directory(.../src/libipc/mem    SRC_FILES)   ┘

② file(GLOB HEAD_FILES                       )            ┐ 收 .h（给 IDE 看 + 安装）：
   include/libipc/*.h, src/libipc/*.h, circ/*.h,          │ → 含 circ、utility 等纯头文件目录
   platform/*.h, utility/*.h                              ┘

③ add_library(ipc STATIC ${SRC_FILES} ${HEAD_FILES})      → 产出 libipc.a
```

注意两件事：

1. `aux_source_directory` **只收集当前目录**（非递归），且只收 `.cpp`。所以 `platform/` 只收了顶层的 `platform.cpp`、`platform.c`，而 `platform/linux`、`platform/posix`、`platform/win` 这些**子目录是纯头文件**，靠 `#include` 路径引入，不单独编译。
2. `circ/` 和 `utility/` 不在 `aux_source_directory` 列表里——因为它们没有 `.cpp`。它们只出现在 `HEAD_FILES` 的 GLOB 里，作为头文件被 `ipc.cpp` 等翻译单元 `#include`。

#### 4.2.3 源码精读

先看「哪些目录进编译列表」——这是判断「核心 vs 头文件库」的硬证据：

- [src/CMakeLists.txt:5-9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L5-L9)：5 次 `aux_source_directory` 把 `src/libipc`、`sync`、`platform`、`imp`、`mem` 的 `.cpp` 都累加进 `SRC_FILES`。注意这里**没有 `circ`、没有 `utility`**——证明它们是头文件库。

再看「头文件如何被收集」：

- [src/CMakeLists.txt:11-18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L11-L18)：`file(GLOB HEAD_FILES ...)` 收集 `include/libipc/*.h`、`src/libipc/*.h`、`circ/*.h`、`platform/*.h`、`utility/*.h` 等。这些头文件随库一起加入 `add_library`，主要作用是让 IDE/安装脚本能看见它们。

然后看「核心层到底有哪些关键文件」。`src/libipc/` 顶层的文件可以这样理解：

| 文件 | 角色 |
| --- | --- |
| `ipc.cpp` | 收发主链路（消息分片、大消息外存、等待退避），是库最大的实现文件 |
| `buffer.cpp` | `buff_t` 的析构器与数据管理实现 |
| `shm.cpp` | `ipc::shm::handle` 的引用计数与清理 |
| `policy.h` | 用 `wr<>` 标签挑选具体队列类型的策略层 |
| `queue.h` | 在共享内存上封装元素数组的队列抽象 |
| `prod_cons.h` | 无锁循环队列的 4 种生产-消费者算法（单播/广播 × 单/多） |
| `waiter.h` | channel 等待/通知核心（condition + mutex + quit 标志） |

最后看头文件搜索路径的配置，它解释了「为什么 `#include "mutex.h"` 能找到平台版本」：

- [src/CMakeLists.txt:44-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L44-L49)：`target_include_directories` 把 `include/` 设为 `PUBLIC`（对用户可见），把 `src/` 设为 `PRIVATE`（仅库内部可见），并在 UNIX 平台额外把 `src/libipc/platform/linux` 加入私有路径——这就是平台分派的落点之一（另一部分在 U5 详讲）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：用构建脚本验证「哪些目录产生机器码、哪些是头文件库」。
2. **操作步骤**：
   - 打开 [src/CMakeLists.txt:5-9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L5-L9)，记下被 `aux_source_directory` 收集的 5 个目录。
   - 分别 `ls` 这 5 个目录，确认每个里至少有一个 `.cpp`。
   - 再 `ls src/libipc/circ` 和 `src/libipc/utility`，确认它们**只有 `.h`**。
3. **需要观察的现象**：`circ/` 里只有 `elem_array.h`、`elem_def.h`；`utility/` 里只有 `.h` 文件。它们不出现在步骤 1 的列表中。
4. **预期结果**：你能画出一张「`src/libipc/` 子目录 → 是否编译」的对照表，与 4.2.1 的表格一致。
5. 运行结果：纯阅读与列表，无需编译（**待本地验证**指你可自行 `ls` 核对）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `circ/` 和 `utility/` 没有出现在 `aux_source_directory` 列表里？

> 答案：因为它们是「头文件库」——内容全是模板/inline 的 `.h`，没有 `.cpp`。它们在使用处（如 `ipc.cpp` `#include` 它们）才被实例化编译，所以不需要、也不能被 `aux_source_directory` 单独收集。

**练习 2**：`ipc.cpp` 与 `include/libipc/ipc.h` 是什么关系？

> 答案：`ipc.h` 是「接口声明」（`chan_impl<Flag>` 的静态函数签名），`ipc.cpp` 是「接口实现」（这些函数的真正函数体）。这正是 2.3 节讲的 PIMPL：头文件给契约，`.cpp` 兑现契约。

---

### 4.3 platform 平台抽象层：跨平台是如何拼出来的

#### 4.3.1 概念说明

`platform/` 是 libipc 实现「Linux / Windows / FreeBSD 一套代码三处运行」的核心机关。它的内部结构是：

```text
src/libipc/platform/
├── platform.cpp      ← C++ 后端分派入口（按 OS include 不同 shm 后端）
├── platform.c        ← C 后端（Linux 上引入 a0 辅助库）
├── detail.h          ← C++ 版本/编译器兼容宏
├── gnuc/             ← GCC 专用（如 demangle）
├── linux/            ← Linux 专用（mutex/condition/sync_obj_impl + a0 辅助库）
├── posix/            ← POSIX 通用后端（shm_posix.cpp、semaphore_impl.h …）
└── win/              ← Windows 后端（shm_win.cpp、mutex.h、semaphore.h …）
```

要点：

- `posix/` 是**通用后端**，覆盖 Linux / FreeBSD / macOS 等一切符合 POSIX 的系统；`linux/` 是 Linux **增强后端**，用了 Linux 独有的 robust mutex 与 `a0` 辅助库；`win/` 是 Windows 后端。
- 三个子目录里有**同名文件**（如都有 `mutex.h`、`condition.h`、`get_wait_time.h`），靠编译宏 + include 路径切换到底「用哪一个」。这就是前置知识 2.2 说的「同名头文件 + 平台分派」。
- 三个后端子目录都是**纯头文件**（除了 `posix/shm_posix.cpp` 和 `win/shm_win.cpp` 这两个共享内存实现）。所以它们不单独编译，而是被 `platform.cpp` 按平台 `#include` 进去。

#### 4.3.2 核心流程

平台选择发生在两个层面：

```text
【编译期：选目录】
  顶层 CMake / src CMake 根据操作系统
    → 把 platform/linux 或 platform/win 加入 include 路径
    → 于是 #include "mutex.h" 命中对应平台的版本

【编译期：选文件】
  platform.cpp 内部用 #if 判断 OS 宏
    → Linux/FreeBSD：#include "posix/shm_posix.cpp"
    → Windows      ：#include "win/shm_win.cpp"
  platform.c 内部
    → Linux：引入 a0 辅助库的 robust mutex 实现
```

因此，**同一份 `ipc.cpp` 调用 `mutex::lock()`，在不同平台会链接到完全不同的实现**，而调用方代码一行都不用改。

#### 4.3.3 源码精读

- [src/CMakeLists.txt:44-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L44-L49)：注意最后一行 `$<$<BOOL:UNIX>:.../src/libipc/platform/linux>`——在 UNIX 系统下，额外把 `platform/linux` 加入私有 include 路径。这是平台分派在构建侧的体现（Windows 侧的路径选择在 U5 详讲）。

- `src/libipc/platform/posix/`：里面 `shm_posix.cpp`、`semaphore_impl.h`、`mutex.h`、`condition.h`、`system.h`、`get_wait_time.h` 是 POSIX 通用实现。`shm_posix.cpp` 是少数带 `.cpp` 的后端文件之一。

- `src/libipc/platform/linux/`：里面 `mutex.h`、`condition.h`、`sync_obj_impl.h`、`get_wait_time.h` 加上 `a0/` 子目录。Linux 版用 robust mutex（崩溃可恢复），细节留到 U6/U8。

- `src/libipc/platform/win/`：里面 `shm_win.cpp`、`mutex.h`、`condition.h`、`semaphore.h`、`codecvt.h`、`to_tchar.h`、`get_sa.h`、`system.h`、`demangle.h`。Windows 版多了字符集转换（`codecvt.h`/`to_tchar.h`）和安全属性（`get_sa.h`）这类 Windows 专属胶水。

> 一个看目录就能记住的规律：**谁有 `.cpp`，谁就是「会被实际编译的后端实现」**——共享内存这块，POSIX 走 `posix/shm_posix.cpp`，Windows 走 `win/shm_win.cpp`。mutex/condition/semaphore 则全是头文件，靠宏在编译期分派。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：找出「本机平台会启用哪些后端文件」。
2. **操作步骤**：
   - 如果你用 Linux：确认 [src/CMakeLists.txt:44-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/CMakeLists.txt#L44-L49) 把 `platform/linux` 加入了路径。
   - 打开 `src/libipc/platform/platform.cpp`，找到按 OS 宏 `#include` 共享内存后端的那几行（它会 `#include "posix/shm_posix.cpp"` 或 `#include "win/shm_win.cpp"`）。
   - 打开 `src/libipc/platform/platform.c`，看它在 Linux 下如何引入 `a0` 辅助库。
3. **需要观察的现象**：同一份 `platform.cpp`，在不同 `#if` 分支下 include 不同的后端 `.cpp`。
4. **预期结果**：你能说清「我在 Linux 上跑时，共享内存实际由 `posix/shm_posix.cpp` 提供，互斥量由 `linux/mutex.h`（robust 版）提供」。
5. 运行结果：纯阅读，无需编译（**待本地验证**指你可自行打开文件确认 `#if` 分支）。

#### 4.3.5 小练习与答案

**练习 1**：`platform/linux`、`platform/posix`、`platform/win` 三个目录里为什么会有同名文件（如都有 `mutex.h`）？它们会冲突吗？

> 答案：这是「同名头文件 + 平台分派」设计。构建时只会把**当前平台**对应的目录加入 include 路径（如 UNIX 加 `linux`），所以编译器每次只看到一份 `mutex.h`，不会冲突。调用方写 `#include "mutex.h"` 即可，由构建系统决定具体命中的是哪一份。

**练习 2**：`platform.cpp` 和三个后端子目录里的 `.cpp` 是「分别独立编译」的吗？

> 答案：不是。三个后端子目录里只有 `posix/shm_posix.cpp` 和 `win/shm_win.cpp` 两个 `.cpp`，且它们是**被 `platform.cpp` 用 `#include` 拉进去**的（作为翻译单元的一部分），而不是各自独立编译。`aux_source_directory` 只收集了 `platform/` 顶层，所以真正参与编译的是 `platform.cpp`/`platform.c`，后端 `.cpp` 借它的 `#if` 分支「搭车」编译。

---

### 4.4 test 与 demo 的用法：如何验证与如何学习

#### 4.4.1 概念说明

`test/` 和 `demo/` 是两个面向「人」的目录，但职责完全不同：

- **`test/`：自动化测试**。用 Google Test 写的单元测试，回答「这块代码的行为对不对」。它由 `LIBIPC_BUILD_TESTS` 开关控制，依赖 `3rdparty/gtest`。
- **`demo/`：可运行示例**。一组最小可执行程序，回答「这个库到底怎么用」。它由 `LIBIPC_BUILD_DEMOS` 开关控制，**无第三方依赖**，是初学者最好的入口。

`test/` 的内部结构：

```text
test/
├── CMakeLists.txt          ← 测试可执行文件 test-ipc 的构建脚本
├── test_ipc_channel.cpp    ← channel/route 收发测试
├── test_buffer.cpp         ← buff_t 测试
├── test_shm.cpp            ← 共享内存测试
├── test_mutex.cpp / test_condition.cpp / test_semaphore.cpp / test_locks.cpp
├── imp/                    ← 基础设施工具测试（12 个）
├── mem/                    ← 内存分配器测试（6 个）
├── concur/                 ← 并发结构测试（intrusive_stack）
└── archive/                ← 已归档的旧测试（被构建脚本显式排除）
```

`demo/` 的内部结构：

```text
demo/
├── send_recv/   ← 最简：一个进程 send、一个进程 recv（U1-L4 的主角）
├── msg_que/     ← 消息队列用法
├── chat/        ← 多客户端双向聊天（U8-L4）
├── linux_service/{service,client}/  ← Linux 后台服务请求-响应
└── win_service/{service,client}/    ← Windows 服务版本
```

#### 4.4.2 核心流程

测试与示例的构建与否，完全由顶层 `CMakeLists.txt` 的两个开关决定：

```text
顶层 CMakeLists.txt:
  option(LIBIPC_BUILD_TESTS OFF)  ─┐
  option(LIBIPC_BUILD_DEMOS OFF)  ─┤ 默认都是 OFF
                                   │
  if(LIBIPC_BUILD_TESTS)           │ → 开了才会 add_subdirectory(test)
  if(LIBIPC_BUILD_DEMOS)           │ → 开了才会 add_subdirectory(demo/*)
```

测试收集时还有个细节：`test/CMakeLists.txt` 用 `list(FILTER ... EXCLUDE REGEX "archive")` 把 `archive/` 目录**显式排除**，所以归档的旧测试不会进入新的 `test-ipc` 可执行文件。

#### 4.4.3 源码精读

先看顶层如何用开关控制 test/demo 的构建：

- [CMakeLists.txt:54-63](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L54-L63)：`if (LIBIPC_BUILD_TESTS)` 块内才会 `add_subdirectory(3rdparty/gtest)` 与 `add_subdirectory(test)`。开关默认 OFF，所以默认构建不含测试。

- [CMakeLists.txt:65-76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L65-L76)：`if (LIBIPC_BUILD_DEMOS)` 块内按平台 `add_subdirectory` 各个 demo（Windows 用 `win_service`，其余用 `linux_service`）。

再看测试如何收集源码并排除归档：

- [test/CMakeLists.txt:19-30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/CMakeLists.txt#L19-L30)：`file(GLOB SRC_FILES ...)` 收集 `test/test_*.cpp` 及 `imp/`、`mem/`、`concur/` 子目录，再用 `list(FILTER SRC_FILES EXCLUDE REGEX "archive")` 把 `archive/` 排除，最后链接 `gtest gtest_main ipc`。

最后看一个真实示例如何使用公共 API——这是「目录用法」的活样本：

- [demo/send_recv/main.cpp:10](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L10)：示例只需 `#include "libipc/ipc.h"` 一个公共头。
- [demo/send_recv/main.cpp:17-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L17-L40)：`do_send` 用 `ipc::channel {"ipc", ipc::sender}` 连通道并 `send`；`do_recv` 用 `ipc::channel {"ipc", ipc::receiver}` 连同一条通道并 `recv`。两个进程靠**同名字符串 `"ipc"`** 连上同一条共享内存通道——这正是 U1-L2 跑通的 demo。

#### 4.4.4 代码实践（可运行型，承接 U1-L2）

1. **实践目标**：从「目录」走到「可运行程序」，验证你对 `demo/` 的理解。
2. **操作步骤**：
   - 用 U1-L2 的方式构建，确保开启 `-DLIBIPC_BUILD_DEMOS=ON`。
   - 在两个终端分别运行：`./send_recv recv 1000` 与 `./send_recv send 64 500`。
   - 观察 recv 端的 `recv waiting...` 与收到时的 `recv size:` 输出。
3. **需要观察的现象**：send 端打印 `send size: 65`（64 字节 + 1 个结尾符），recv 端在若干次 `waiting` 后打印 `recv size: 64`。
4. **预期结果**：两个进程通过同名通道 `"ipc"` 成功收发，证明 `demo/send_recv` 是一条端到端可用的最小链路。
5. 运行结果：**待本地验证**（取决于你本机是否已按 U1-L2 构建成功）。

#### 4.4.5 小练习与答案

**练习 1**：为什么默认构建（不传任何 `-D` 开关）不会编译 `test/` 和 `demo/`？

> 答案：因为顶层 [CMakeLists.txt:4-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L4-L8) 把 `LIBIPC_BUILD_TESTS` 和 `LIBIPC_BUILD_DEMOS` 都设为默认 `OFF`，只有显式 `-DLIBIPC_BUILD_TESTS=ON` / `-DLIBIPC_BUILD_DEMOS=ON` 时才会 `add_subdirectory` 进入对应目录。这是为了让「只想用库」的用户开箱即得一个干净的 `libipc.a`，不被测试与示例拖慢编译。

**练习 2**：`test/archive/` 里的旧测试会影响新的 `test-ipc` 吗？

> 答案：不会。[test/CMakeLists.txt:28-30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/CMakeLists.txt#L28-L30) 用 `list(FILTER ... EXCLUDE REGEX "archive")` 把路径里含 `archive` 的文件从 `SRC_FILES` 和 `HEAD_FILES` 里滤掉，所以归档旧测试不参与编译。

---

## 5. 综合实践

把本讲四个最小模块串成一张「目录 → 模块」地图。请按下面步骤完成：

1. **画分层骨架**：在一张纸上（或 Markdown 里）画出仓库根目录下的 5 个一级目录——`include/`、`src/`、`test/`、`demo/`、`3rdparty/`——并标注各自职责（公共 API / 内部实现 / 自动化测试 / 可运行示例 / 第三方依赖）。
2. **细化 src**：在 `src/libipc/` 下展开「核心 / circ / mem / sync / platform / utility / imp」七层，每层标注：①职责；②是否含 `.cpp`（是否产生机器码）；③1–2 个关键文件（如核心层写 `ipc.cpp`、`prod_cons.h`）。
3. **细化 platform**：展开 `platform/` 的 `posix / linux / win / gnuc` 四个子目录，标注各自代表的后端，并指出哪个目录是「纯头文件」、哪个带 `.cpp`。
4. **连构建线**：用箭头标出 `顶层 CMakeLists.txt` 如何通过 `LIBIPC_BUILD_TESTS/DEMOS` 开关决定是否进入 `test/`、`demo/`；并标出 `src/CMakeLists.txt` 用 `aux_source_directory` 收集了哪 5 个目录。
5. **自检**：在地图上随机指一个目录，说出它属于哪一层、是否被编译、对应公共 API 还是内部实现。如果答得上来，本讲就过关了。

> 这张图建议保存下来。后续 U2（公共 API 细节）、U3（收发主链路）、U5（共享内存）、U6（同步原语）、U7（内存分配）每开一篇，都先回到这张图定位目录，能省下大量「这个文件在哪」的时间。

## 6. 本讲小结

- `include/libipc/` 是**唯一对外暴露**的目录，8 个顶层 `.h` 覆盖收发（`ipc.h`）、类型（`def.h`）、消息容器（`buffer.h`）、共享内存（`shm.h`）与同步原语（`mutex/condition/semaphore/rw_lock`），`ipc.h` 是聚合入口。
- `src/libipc/` 是库的「肉」，分核心 / circ / mem / sync / platform / utility / imp 七层；其中 `circ/` 和 `utility/` 是**头文件库**（无 `.cpp`），靠模板在使用处实例化。
- `src/CMakeLists.txt` 用 `aux_source_directory` 收 5 个含 `.cpp` 的目录、用 `file(GLOB HEAD_FILES)` 收头文件——「在不在 `aux_source_directory` 列表里」就是判断「是否产生机器码」的硬证据。
- `platform/` 用 `posix / linux / win` 三个**同名头文件**子目录 + 编译宏 + include 路径实现平台分派；调用方代码不变，链接到的实现随平台切换。
- `test/`（依赖 gtest，`LIBIPC_BUILD_TESTS` 控制）负责自动化测试，`demo/`（无依赖，`LIBIPC_BUILD_DEMOS` 控制）负责可运行示例，二者默认都不构建；`test/archive/` 的旧测试被显式排除。
- `demo/send_recv` 是最小可运行样本：两个进程靠同名通道字符串 `"ipc"` 连上同一条共享内存通道完成收发。

## 7. 下一步学习建议

本讲建立的是「地图」，下一讲 **U1-L4「你的第一个 IPC 程序：route/channel 收发」** 会带你真正在地图上「跑起来」——用 `ipc::channel` / `ipc::route` 写出第一个跨进程收发程序，理解 sender/receiver 模式与 connect/send/recv/disconnect 生命周期。

如果你想在进入 U1-L4 前再巩固本讲，建议：

- 通读 `demo/send_recv/main.cpp` 全文（只有 70 行），对照本讲的公共 API 表格，找出每行用到的类型/函数来自哪个头文件。
- 翻一遍 `src/libipc/ipc.cpp` 的**函数名**（先不读实现），感受「核心层」到底实现了哪些动作，为 U3 的主链路精读做铺垫。
