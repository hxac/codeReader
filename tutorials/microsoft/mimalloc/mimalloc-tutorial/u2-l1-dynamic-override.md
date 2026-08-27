# 动态覆盖 malloc：LD_PRELOAD 与 alloc-override.c

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「覆盖（override）malloc」到底要解决什么问题：**不修改任何一行业务代码、不重新编译第三方库，让整个进程里的 `malloc` / `free` / `new` / `delete` 全部变成 mimalloc 的实现**。
2. 读懂 `src/alloc-override.c`：它如何定义一组与标准库**同名**的导出符号，并针对 gcc/clang（ELF 别名）、macOS（interpose 段）、MSVC（直接定义 CRT 函数）分成三条编译分支。
3. 掌握 Linux 的 `LD_PRELOAD` 与 macOS 的 `DYLD_INSERT_LIBRARIES` 两种动态覆盖方式，并用 `MIMALLOC_VERBOSE=1` 等手段**验证覆盖确实生效**。
4. 理解另外两种静态覆盖途径——单目标文件 `mimalloc.o` 优先链接、`mimalloc-override.h` 宏替换——以及宏替换方案「跨堆指针混用」风险的源码级原因。

上一讲（u1-l4）我们主动调用 `mi_` API；本讲反过来：**让你的程序以为自己还在调用系统 `malloc`，实际干活的已经是 mimalloc**。这正是 readme 把 mimalloc 称为「drop-in replacement」的含义。

## 2. 前置知识

- **动态链接与符号解析**：现代操作系统上，`printf`、`malloc` 这些库函数的名字（符号）要到程序**加载时**才被「解析」成真实地址。ELF 系统（Linux/BSD）的动态链接器按固定顺序查找全局符号：**可执行文件自身 → `LD_PRELOAD` 指定的库 → 按依赖顺序排列的共享库（libc 等）**，第一个提供该符号者胜出。这个「后来者可以顶替先来者」的机制叫**符号抢占（symbol interposition）**。`LD_PRELOAD` 就是把 mimalloc 插到 libc 前面的官方开关。
- **别名（alias）属性**：GCC/Clang 的 `__attribute__((alias("mi_malloc")))` 可以让符号 `malloc` 与 `mi_malloc` 指向**同一个机器码地址**——不是「调用转发」，而是零开销的「第二个名字」。它有一个硬性限制：别名与目标必须在**同一个翻译单元**（translation unit，一次编译器输入，通常即一个 `.c` 加上它 include 的所有内容）内。这个限制直接决定了 `alloc-override.c` 的组织方式（见 4.2）。
- **macOS 的 interpose 机制**：macOS 的动态链接器（dyld）默认使用「两级命名空间」，库符号绑定的抢占规则与 ELF 不同。官方提供的挂钩是 `__DATA,__interpose` 段：在库里放一张 `{replacement, target}` 对表，dyld 加载时会把对 `target` 的调用改绑到 `replacement`。`DYLD_INSERT_LIBRARIES` 是预加载库的环境变量，对应 Linux 的 `LD_PRELOAD`。
- **`__attribute__((constructor))`**：GCC/Clang 提供的属性，被标记的函数会在 `main` 之前由动态链接器自动执行。mimalloc 用它完成「进程装载即初始化」（见 4.1）。
- **承接 u1-l2 / u1-l4**：cmake 选项 `MI_OVERRIDE`（默认 ON）控制是否编译覆盖符号；release 构建产物是 `out/release/libmimalloc.so`，非 release 构建库名带 `-debug` 等后缀；`MIMALLOC_VERBOSE` / `MIMALLOC_SHOW_STATS` 环境变量与 `mi_option_*` 编程接口等价。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/alloc-override.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c) | **本讲主角**：定义与标准库同名的导出符号（`malloc`/`free`/`strdup`/C++ `new` 等），按平台分三条分支。它不是独立编译单元，而是被 `alloc.c` include。 |
| [include/mimalloc-override.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-override.h) | 第三种覆盖途径：用宏把 `malloc` **文本替换**成 `mi_malloc`，供用户程序自己 include。 |
| [test/main-override.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.c) | 官方覆盖验证程序：用 `malloc` 分配、用 `mi_is_in_heap_region` 验证指针确实出自 mimalloc。 |
| [test/CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt) | 安装后的示例工程，用四个目标分别演示动态/静态两种覆盖途径。 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | `mi_malloc` 等入口所在；`alloc-override.c` 与 `free.c` 都被它 include 进同一翻译单元。 |
| [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt) | `MI_OVERRIDE` 选项 → `MI_MALLOC_OVERRIDE` 宏的编译期链路。 |
| [src/prim/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c) | 平台选择器 + `__attribute__((constructor))`：让库在 `main` 前自动初始化。 |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) / [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | 进程初始化与选项打印——`MIMALLOC_VERBOSE=1` 时版本横幅从哪里打印出来。 |
| [src/page-map.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c) / [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | `mi_is_in_heap_region` 的实现；`mi_free` 对「外来指针」的处理（宏替换风险的源码依据）。 |
| [readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md) | 「Overriding Standard Malloc」一节是三种途径的官方文档。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**alloc-override.c 的同名符号定义（4.1）**、**从 cmake 开关到符号表的编译期链路与别名技巧（4.2）**、**三种覆盖途径全景与验证程序 main-override.c（4.3）**。

### 4.1 同名符号：让全进程的 malloc 都变成 mimalloc

#### 4.1.1 概念说明

「覆盖」的核心思路朴素得令人惊讶：**动态链接器按名字找函数，那我只要导出一个同名函数，并让自己排在 libc 前面就行了**。

mimalloc 的 `libmimalloc.so` 里导出了一个货真价实的 `malloc`。在 Linux 上预加载它之后：

- 你的代码里 `malloc(78)` 的调用点并不关心实现是谁，链接器解析符号时先搜到 mimalloc 的 `malloc`，绑定完成。
- 不只是你的代码：libc 内部经由 PLT 调用 `malloc` 的路径（例如某些 `strdup` 实现）同样会被抢占——它们也走全局符号解析。
- 因此整个进程（包括你没源码的第三方 `.so`）的 malloc 家族调用都汇入 mimalloc，且**不需要重新编译任何东西**。

但只有 `malloc`/`free`/`calloc`/`realloc` 四个名字还不够：各平台还有一批「绕开 malloc 的旁门」——`strdup` 在某些系统上不走 `malloc` 而走更底层的内部调用、glibc/musl 存在 `__libc_malloc` 私有接口、macOS 有 `malloc_size`/`vfree`、Windows CRT 有 `_malloc_base`/`_expand`。`alloc-override.c` 的全部篇幅，本质上就是在**逐个堵住这些旁门**。

#### 4.1.2 核心流程

以 Linux 上 `LD_PRELOAD` 一个普通程序为例，完整时序是：

```text
1. 内核装载可执行文件，动态链接器(ld.so)启动
2. ld.so 读取 LD_PRELOAD，把 libmimalloc.so 映射进内存，
   并排在全局符号查找顺序中 libc 之前
3. 解析符号：所有对 malloc/free/... 的引用 → 绑定到 mimalloc 的同名符号
4. 执行预加载库的 ELF constructor：
   mi_process_attach (src/prim/prim.c)
     └→ _mi_auto_process_init (src/init.c)
          ├→ mi_process_init()          # 选项/统计/OS/page map/主堆，初始化
          └→ _mi_options_post_init()    # 此时 stderr 可用了：
                若 MIMALLOC_VERBOSE=1 → mi_options_print()
                打印 "v3.5.0..." 版本横幅 + 全部选项 + 构建配置   ← 覆盖成功的证据!
5. main() 开始执行，其中所有 malloc 调用都已是 mi_malloc
6. 进程退出，destructor mi_process_detach
     └→ _mi_auto_process_done → mi_process_done
          └→ 若 MIMALLOC_SHOW_STATS=1 / VERBOSE=1 → 打印统计报表
```

注意第 4 步：**版本横幅打印在 `main` 之前**。这是判断「预加载是否成功」最干净的信号——这个输出只能来自被预加载的 mimalloc 库本身，业务程序对它一无所知。

#### 4.1.3 源码精读

**（1）最核心的四行定义。** 文件的「通用分支」（非 macOS interpose、非 MSVC，Linux/BSD 走这里）：

- [src/alloc-override.c:L212-L225](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L212-L225) —— 用 `mi_decl_export`（默认可见性导出）定义 `malloc` / `calloc` / `realloc` / `free` / `strdup` / `strndup`，函数体由 `MI_FORWARD*(...)` 宏展开生成（见下）。注意 L218-L219 的注释：原则上 `strdup` 不必覆盖（它通常内部调 `malloc`），但**某些系统的 `strdup` 不走 `malloc` 而走更原始的调用**，所以干脆一并定义。

```c
// On all other systems forward allocation primitives to our API
mi_decl_export void* malloc(size_t size)              MI_FORWARD1(mi_malloc, size)
mi_decl_export void* calloc(size_t size, size_t n)    MI_FORWARD2(mi_calloc, size, n)
mi_decl_export void* realloc(void* p, size_t newsize) MI_FORWARD2(mi_realloc, p, newsize)
mi_decl_export void  free(void* p)                    MI_FORWARD0(mi_free, p)
```

**（2）`MI_FORWARD`：别名让 `malloc` 与 `mi_malloc` 是同一个地址。**

- [src/alloc-override.c:L29-L49](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L29-L49) —— GCC/Clang（非 Apple）分支把 `MI_FORWARD(fun)` 定义为 `__attribute__((alias(#fun), used, visibility("default")))`：`malloc` 成为 `mi_malloc` 的**别名符号**，二者地址相同，调用 `malloc` 没有任何一层转发开销。GCC ≥ 9 时额外用 `copy(fun)` 把目标函数的属性也复制过来（所以 L31-L32 先用 `#pragma` 关掉 `-Wattributes` 警告）。其他编译器则退化为普通函数体内调用 `return fun(x);`。

**（3）堵住 glibc/musl 的私有旁门。**

- [src/alloc-override.c:L385-L397](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L385-L397) —— 在 `__linux__` 下额外定义 `__libc_malloc` / `__libc_calloc` / `__libc_realloc` / `__libc_free` / `__libc_memalign` 等一整族。注释写明这是「glibc 与 musl 发行版所需」：直接调用这些私有接口的程序或库也能被接管。

**（4）macOS 分支：interpose 段。**

- [src/alloc-override.c:L51-L62](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L51-L62) —— 当构建为共享库且开启 `MI_OSX_INTERPOSE` 时，改用 interpose：源码注释解释这是为了让 `DYLD_INSERT_LIBRARIES` **无需** `DYLD_FORCE_FLAT_NAMESPACE=1` 即可工作。
- [src/alloc-override.c:L63-L93](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L63-L93) —— `struct mi_interpose_s {replacement, target}` 放进 `__DATA, __interpose` 段，表里逐项登记 `malloc`→`mi_malloc`、`calloc`→`mi_calloc`……macOS 专有的 `malloc_size` 走 `mi_malloc_size_checked`（先检查指针是否真在 mimalloc 堆内再报尺寸）。注意 L88-L92 对 `free` 的特殊处理：**有些代码从系统默认 zone 分配、却用普通 `free` 释放**，所以 interpose 的 `free` 绑到 `mi_cfree`（checked free，先查归属再释放）而不是 `mi_free`。
- [src/alloc-override.c:L94-L99](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L94-L99) —— 按 SDK 版本号条件追加 `strndup`（10.7+）与 `aligned_alloc`（10.15+）。

**（5）MSVC 分支。**

- [src/alloc-override.c:L127-L155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L127-L155) —— Windows 上直接以 CRT 的签名与 SAL 标注定义 `_expand`、`_msize`、`_free_base`、`free`、`malloc`、`_malloc_base` 等（`_CRT_HYBRIDPATCHABLE` 标注配合官方的 mimalloc-redirect.dll 重定向方案，本讲不展开，见 readme「Dynamic Override on Windows」一节）。

**（6）main 之前发生的事（verbose 横幅的来源）。**

- [src/prim/prim.c:L30-L46](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L30-L46) —— GCC/Clang 下用 `__attribute__((constructor))`（Clang 还指定优先级 101，注释说明它先于普通 constructor 执行）定义 `mi_process_attach`，调用 `_mi_auto_process_init`；对应的 destructor 调 `_mi_auto_process_done`。
- [src/init.c:L505-L513](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L505-L513) —— `_mi_auto_process_init`：先 `mi_process_init()` 完成初始化，再 `_mi_options_post_init()`（此时 stderr 才可安全使用）。
- [src/options.c:L206-L209](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L206-L209) 与 [src/options.c:L214-L240](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L214-L240) —— verbose 开启时调用 `mi_options_print()`，其实现先打印 `v%i.%i.%i` 版本横幅（如 `v3.5.0`，附带构建类型），随后逐行打印每个 `option '...'` 的当前值。这就是实践中我们要找的「版本信息」。
- [src/init.c:L596-L648](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L596-L648) —— 进程退出路径 `mi_process_done_once`：L634-L639 在 `show_stats` 或 `verbose` 开启时合并并打印统计报表。

#### 4.1.4 代码实践

**实践目标**：对一个**完全没有链接 mimalloc** 的普通 C 程序做 `LD_PRELOAD` 覆盖，并用 `MIMALLOC_VERBOSE=1` 从输出中找到 mimalloc 的版本横幅，证明覆盖成功。

**操作步骤**：

1. 按 u1-l2 完成 release 构建（产物为 `out/release/libmimalloc.so`）。
2. 写一个与 mimalloc 毫无关系的最小程序 `myprogram.c`（示例代码，非项目文件）：

   ```c
   // myprogram.c —— 只用标准库，编译时不链接 mimalloc
   #include <stdlib.h>
   #include <string.h>
   #include <stdio.h>
   int main(void) {
     for (int i = 0; i < 100000; i++) {
       char* s = strdup("hello mimalloc");   // 覆盖后实际是 mi_strdup
       void* p = malloc(64);                 // 覆盖后实际是 mi_malloc
       free(p); free(s);
     }
     printf("done\n");
     return 0;
   }
   ```

3. 编译：`gcc -o myprogram myprogram.c`（注意：**不要**加任何 mimalloc 头文件或库）。
4. 运行：

   ```bash
   env MIMALLOC_VERBOSE=1 LD_PRELOAD=$PWD/out/release/libmimalloc.so ./myprogram
   ```

5. 换用 debug 构建再看统计（debug 库名带后缀，见 u1-l2）：

   ```bash
   env MIMALLOC_SHOW_STATS=1 MIMALLOC_VERBOSE=1 \
     LD_PRELOAD=$PWD/out/debug/libmimalloc-debug.so ./myprogram
   ```

**需要观察的现象**：

- 第 4 步输出中，在 `done` **之前**出现形如 `v3.5.0` 的版本行，其后跟着一串 `option '...': ...` 行（如 `option 'show_errors'`、`option 'purge_delay'`）以及 `debug level` / `secure level` 等构建配置行，还有 `process init: ...` 一类的 verbose 消息。
- 第 5 步在进程退出前多出完整的统计报表（bin / pages / process 段，格式解读见 u1-l4）。

**预期结果**：`myprogram` 本身不可能打印任何 mimalloc 信息（它没链接 mimalloc），因此版本横幅与统计报表**只能来自被预加载的 `libmimalloc.so`**——这正是覆盖成功的直接证据。另外可对照 readme 的官方示例（[readme.md:L482-L490](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L482-L490)），与本实践一一对应。verbose 输出的确切行数与内容随构建配置不同而不同，具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `alloc-override.c` 除了 `malloc` 还要定义 `strdup` 和 `__libc_malloc`？
**答案**：`strdup` 在某些 libc 上不经过 `malloc` 而直接调用更底层的内部接口（源码 L218-L219 的注释），只覆盖 `malloc` 拦不住它；`__libc_malloc` 一族则是 glibc/musl 的私有接口，有程序或库直接调用，同样必须「堵门」才能保证全进程无泄漏地被接管。

**练习 2**：`LD_PRELOAD` 之后，进程里还有没有可能存在「系统 libc 的 malloc」被调用？
**答案**：常规情况下没有——ELF 全局符号解析让 mimalloc 的 `malloc` 抢占所有经 PLT 的调用，包括 libc 内部对自己的调用。但两个边缘要注意：一是静态链接进可执行文件的 libc 代码（若程序本身静态链接 libc）不经过动态符号解析；二是某些程序用 `dlsym(RTLD_NEXT, "malloc")` 之类手段刻意绕开抢占。这也是 `mimalloc-override.h` 宏方案风险的来源之一（见 4.3）。

**练习 3**：为什么 `MIMALLOC_VERBOSE=1` 的横幅出现在 `main` 之前？
**答案**：mimalloc 的共享库带一个 `__attribute__((constructor))` 函数 `mi_process_attach`（[src/prim/prim.c:L41-L43](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L41-L43)），动态链接器装载完预加载库后、进入 `main` 前就执行它，一路调到 `_mi_options_post_init()` 打印版本与选项（[src/init.c:L505-L513](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L505-L513)）。

### 4.2 从 cmake 开关到符号表：MI_OVERRIDE 链路与别名技巧

#### 4.2.1 概念说明

覆盖代码并非无条件编译。它由一条「**cmake 选项 → C 宏 → 源码 `#if` 分支**」的链路控制（u1-l2 已见过的模式）：

```text
cmake 选项 MI_OVERRIDE (默认 ON, CMakeLists.txt L10)
  └→ 为 mimalloc / mimalloc-static / mimalloc-obj 三个目标
     定义私有宏 MI_MALLOC_OVERRIDE   (CMakeLists.txt L1014-L1024)
       └→ src/alloc.c include "alloc-override.c"
          其中 #if defined(MI_MALLOC_OVERRIDE) 才展开全部覆盖定义
             (alloc-override.c L13)
```

而 `alloc-override.c` 的文件名虽以 `.c` 结尾，却**从不被单独编译**——文件开头就写着「必须从 `alloc.c` include」。原因是 4.1 提到的别名技巧：`__attribute__((alias("mi_malloc")))` 要求别名与目标处于同一翻译单元，而 `mi_malloc` 定义在 `alloc.c`、`mi_free` 定义在 `free.c`。于是 `alloc.c` 把两个文件都 include 进来，凑成一个足够大的翻译单元。这也是 u1-l3 讲过的「翻译单元合并」的第三个受益者（另两个是快路径内联与链接期优化）。

#### 4.2.2 核心流程

把整条链路按时间顺序排开：

1. **配置期**：cmake 读 `MI_OVERRIDE`（默认 ON），打印状态行，并给每个库目标挂上 `-DMI_MALLOC_OVERRIDE`；同时（GCC/Clang）追加 `-fno-builtin-malloc`。
2. **编译期**：`src/alloc.c` 顶部定义 `MI_IN_ALLOC_C`，随后 `#include "alloc-override.c"` 与 `"free.c"`；`alloc-override.c` 检测到 `MI_MALLOC_OVERRIDE` 后按平台选择分支展开，生成与 `mi_malloc` 同地址的 `malloc` 等导出符号。
3. **链接期**：这些符号以默认可见性（`visibility("default")`，经 `mi_decl_export`）进入 `libmimalloc.so` 的动态符号表（`.dynsym`），成为可被抢占绑定的「候选」。
4. **运行期**：`LD_PRELOAD`（或静态优先链接）让候选变成「胜者」。

`-fno-builtin-malloc` 的用意：一旦本库自己定义了 `malloc`，编译器若仍把它当内建函数，就可能做出不符合预期的变换（例如把「malloc + 清零」合并、消除它认为冗余的调用）；关掉内建假设，保证覆盖行为与源码一致（[CMakeLists.txt:L641-L645](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L641-L645)）。

#### 4.2.3 源码精读

- [src/alloc-override.c:L8-L13](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L8-L13) —— 双重门禁：没定义 `MI_IN_ALLOC_C` 直接 `#error`（防止有人把它当独立源文件编译）；`MI_MALLOC_OVERRIDE` 未定义时整个文件内容为空。
- [src/alloc.c:L20-L23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L20-L23) —— 唯一合法入口：定义 `MI_IN_ALLOC_C` → include `alloc-override.c` → include `free.c` → `#undef`。
- [src/free.c:L8](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L8) —— `free.c` 有同样的 `#error` 守卫，错误信息直说了原因：「以便别名能从 alloc-override 生效」。
- [CMakeLists.txt:L10](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L10) 与 [CMakeLists.txt:L247-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L247-L249) —— `MI_OVERRIDE` 选项定义与配置期状态输出。
- [CMakeLists.txt:L1014-L1024](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L1014-L1024) —— 分别给共享库、静态库、单目标文件三个目标定义 `MI_MALLOC_OVERRIDE`（所以 `mimalloc.o` 也具备覆盖能力，见 4.3）。
- [include/mimalloc.h:L62-L66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L62-L66) —— `mi_decl_export` 在构建共享库时展开为 `__attribute__((visibility("default")))`，保证符号不被 `-fvisibility=hidden` 之类配置藏起来。
- 顺带一提，[src/alloc-override.c:L228-L230](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L228-L230) 用 `#pragma GCC visibility push(default)` 把后面覆盖的 C++ `new`/`delete` 运算符也显式导出——C++ 运算符默认不导出，而它们同样要参与抢占（`new`/`delete` 覆盖细节属于 u2-l2）。

#### 4.2.4 代码实践

**实践目标**：不等程序运行，直接从**动态符号表**确认 `libmimalloc.so` 导出了 `malloc` 等同名符号，且它们与 `mi_malloc` 是同一个地址（别名生效的物证）。

**操作步骤**：

1. 构建后查看导出符号（示例命令，非项目文件）：

   ```bash
   nm -D out/release/libmimalloc.so | grep -E ' (malloc|free|calloc|realloc|strdup|__libc_malloc)$'
   ```

2. 观察 `malloc` 与 `mi_malloc` 两行的地址列是否完全相同：

   ```bash
   nm -D out/release/libmimalloc.so | grep -E ' (malloc|mi_malloc)$'
   ```

3. 再构建一个关闭覆盖的版本做对照：

   ```bash
   cmake -B out/no-override -DMI_OVERRIDE=OFF && cmake --build out/no-override
   nm -D out/no-override/libmimalloc.so | grep -cE ' (malloc|free)$'
   ```

**需要观察的现象**：第 1 步应列出 `malloc`、`free`、`calloc`、`realloc`、`strdup`、`__libc_malloc` 等；第 2 步两行地址一致；第 3 步计数为 0（或无匹配行）。

**预期结果**：开启 `MI_OVERRIDE` 时同名符号存在且 `malloc` 与 `mi_malloc` 地址相同（别名）；关闭后这些符号从动态符号表消失。不同工具链的 `nm` 输出列格式略有差异，具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果直接把 `src/alloc-override.c` 加进 cmake 的源文件列表单独编译，会发生什么？
**答案**：编译直接失败。文件第 8-L10 行的 `#error` 会在未定义 `MI_IN_ALLOC_C` 时报出「必须从 alloc.c include」；即使绕过守卫，别名属性也因找不到同翻译单元的 `mi_malloc`/`mi_free` 定义而无法生成。

**练习 2**：为什么「覆盖开关」要做两级——cmake 的 `MI_OVERRIDE` 和 C 宏 `MI_MALLOC_OVERRIDE`，而不是只用一个？
**答案**：分工不同：`MI_OVERRIDE` 是**构建系统层**的开关，决定要不要给目标挂宏、要不要加 `-fno-builtin-malloc` 等配套编译选项（还能按目标分别控制共享库/静态库/单目标文件）；`MI_MALLOC_OVERRIDE` 是**源码层**的门禁，决定 `#if` 分支是否展开。两级解耦后，同一份源码既能编出「带覆盖」也能编出「纯 API 库」（`-DMI_OVERRIDE=OFF`），供不需要抢占、只想显式调 `mi_` 函数的场景使用。

**练习 3**：`-fno-builtin-malloc` 是加给谁的？不加可能出什么问题？
**答案**：加给 mimalloc 库自身的编译单元（[CMakeLists.txt:L641-L645](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L641-L645) 只 `list(APPEND mi_cflags ...)`）。不加的话，编译器会把库内对 `malloc` 的引用当作标准内建处理，可能做出假定其标准语义的优化，使覆盖后的实际行为偏离源码意图。

### 4.3 三种覆盖途径与验证程序 main-override.c

#### 4.3.1 概念说明

readme「Overriding Standard Malloc」一节把覆盖分成动态与静态两大类、共三条实用途径：

| 途径 | 做法 | 平台 | 改动量 | 主要风险/限制 |
| --- | --- | --- | --- | --- |
| ① 动态预加载（推荐） | `LD_PRELOAD=libmimalloc.so`（macOS：`DYLD_INSERT_LIBRARIES=libmimalloc.dylib`） | Linux/BSD/macOS | 零改动、零重编译 | 受安全策略限制（如 macOS shell 场景，[readme.md:L492-L502](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L492-L502)） |
| ② 静态单目标文件 | 把 `mimalloc.o` 放在链接命令**最前面**：`gcc -o myprogram mimalloc.o myfile1.c ...` | Unix（Windows 需 `/MT`） | 改链接命令 | 无（符号解析天然优先） |
| ③ 宏替换头 | 每个源文件 `#include <mimalloc-override.h>`，把 `malloc` 宏定义成 `mi_malloc` | 全平台 | 改每个源文件 | **跨堆指针混用**：未受控的外部库仍走系统分配器 |

途径② 的原理：链接器解析符号时，**命令行上显式目标文件里的定义优先于库文件（archive）中的成员**；而链接出的可执行文件自带 `malloc` 定义，按 ELF 全局查找顺序（可执行文件最先）又压过 `libc.so`——于是全进程的 `malloc` 都指向 mimalloc（官方说明见 [readme.md:L546-L558](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L546-L558)）。u1-l2 讲过的 `src/static.c` 生成 `mimalloc.o`，正是为这条路服务的。

途径③ 的风险值得从源码层面讲透。`mimalloc-override.h` 只影响**包含它的那些翻译单元**：

- [include/mimalloc-override.h:L11-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-override.h#L11-L16) —— 头文件自己的文档注释就发出警告：要小心外部代码「不至于意外混用来自不同分配器的指针」。
- [include/mimalloc-override.h:L20-L28](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-override.h#L20-L28) —— 实现就是一组纯粹的 `#define`：`malloc(n)` → `mi_malloc(n)`、`free(p)` → `mi_free(p)`、`strdup` → `mi_strdup` 等，外加微软扩展（`_expand`、`_recalloc`…，L30-L40）与 POSIX 变体（L43-L66）。

风险场景：你的源码 include 了这个头（`free` 变成 `mi_free`），但某个**预编译的第三方库或系统函数**返回的指针来自**系统 malloc 的堆**。你把该指针交给 `free`（实际是 `mi_free`）时，mimalloc 在 page map 里查不到它的归属页：

- [src/free.c:L251-L256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251-L256) —— `mi_free` 先经 `mi_validate_ptr_page_nonnull` 校验，查不到页就直接返回（不释放）。
- [src/free.c:L172-L214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L172-L214) —— 校验函数查 page map 得到 NULL 时的处理：debug 构建打印 `invalid pointer` 错误信息（L205-L209），release 构建则**静默忽略这次 free**（内存从此无人释放，也不会归还给系统分配器）。

也就是说混用不一定会立刻崩溃，而是**悄悄泄漏**（release）或报无效指针（debug）——这正是 readme L560-L565 强调「所有源码都必须在你掌控之下才可靠」的源码依据。

#### 4.3.2 核心流程

`test/CMakeLists.txt` 用四个可执行目标把三条途径各演示一遍（这是**安装 mimalloc 之后**的独立示例工程，通过 `find_package(mimalloc)` 引入库）：

```text
dynamic-override       main-override.c   + libmimalloc.so   → 配合 LD_PRELOAD（途径①）
static-override-obj    main-override.c   + mimalloc.o       → 目标文件优先链接（途径②）
static-override-static main-override-static.c + libmimalloc.a + 宏头（途径③）
static-override        main-override.c   + libmimalloc.a    → 静态库覆盖（②的弱化版，可能失效）
```

#### 4.3.3 源码精读

- [test/CMakeLists.txt:L23-L36](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L23-L36) —— L24 的注释点明 `dynamic-override` 要配合 `LD_PRELOAD` 才真正生效（只是链接共享库并不会自动抢占）；L32-L33 注释解释 `static-override-obj` 为什么可靠：「目标文件中的符号优先于库文件中的符号」。
- [test/CMakeLists.txt:L39-L51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L39-L51) —— 途径③示例 `static-override-static`（用 `main-override-static.c`，见 [test/main-override-static.c:L11](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override-static.c#L11) include 宏头）；`static-override` 的注释则诚实警告：静态库若在命令行上排得太靠后，覆盖可能失效。
- [test/main-override.c:L1-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.c#L1-L16) —— 验证程序的骨架。三个关键点：
  1. L6 `// #include <mimalloc-override.h>` 被注释掉——本程序验证的是**符号级覆盖**（①②），不需要宏替换；
  2. L9 `mi_version();` 注释写明用途是「确保 mimalloc 库被链接」——对动态库来说链接器可能因「没有引用」而丢弃它，主动调用一个 `mi_` 函数是最简单的强制定位手段（Windows 上同理用 `/include:mi_version`，见 readme L517-L520）；
  3. L10-L16 用 `malloc(78)` 分配后立刻用 `mi_is_in_heap_region(p1)` 断言指针确实出自 mimalloc 的堆——失败则打印错误并返回 1。这是「符号级验证」的核心手法。
- [test/main-override.c:L34-L38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.c#L34-L38) —— 交叉混用测试：`mi_malloc` 的指针交给 `free`、`malloc` 的指针交给 `mi_free`。覆盖成功时两者指向同一实现，交叉调用必须天衣无缝；若覆盖失败（仍走系统 malloc），`mi_free` 释放系统指针就会触发 4.3.1 描述的无效指针路径。
- [test/main-override.c:L47](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override.c#L47) —— 最后 `mi_stats_print(NULL)` 打印统计退出。
- [src/page-map.c:L208-L210](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L208-L210) —— `mi_is_in_heap_region` 的实现只有一行：指针能通过 page map（含安全检查 `_mi_safe_ptr_page`）反查出所属页就返回 true。它的能力来自 u1-l3 介绍的 page map：mimalloc 管理的内存都登记在册，系统分配器的内存不在册。声明在 [include/mimalloc.h:L446](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L446)。

> **平台注意**：`main-override.c` L11 的 `_expand(p1, 100)` 是 Windows CRT 扩展接口。在本讲写作时验证的 Ubuntu/glibc 上，`/usr/include/malloc.h` 只声明了 `malloc/calloc/realloc/free/memalign/valloc/pvalloc/malloc_usable_size` 等符号，**并没有 `_expand`**；而 mimalloc 恰好只在 MSVC 分支（[src/alloc-override.c:L128-L135](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c#L128-L135)）定义它。所以在 Linux 上照抄该文件不可移植，下面实践中我们把它换成 `realloc`。

#### 4.3.4 代码实践

**实践目标**：仿照 `main-override.c` 写一个跨平台的「覆盖验证器」，用 `mi_is_in_heap_region` + 交叉释放双重证明「符号级覆盖已生效」。

**操作步骤**：

1. 写验证程序 `verify-override.c`（示例代码，改写自 test/main-override.c，去掉了平台相关的 `_expand`）：

   ```c
   // verify-override.c —— 链接 mimalloc 后验证 malloc 是否被符号级覆盖
   #include <stdlib.h>
   #include <stdio.h>
   #include <string.h>
   #include <mimalloc.h>

   int main(void) {
     mi_version();                       // 强制链接器保留 mimalloc（同 main-override.c L9）

     void* p1 = malloc(78);              // 系统接口分配
     if (!mi_is_in_heap_region(p1)) {    // 若为假，说明 malloc 没被覆盖
       printf("FAIL: malloc did not allocate in the mimalloc heap region\n");
       return 1;
     }
     char* s = strdup("hello");          // strdup 旁门也应被覆盖
     if (!mi_is_in_heap_region(s)) { printf("FAIL: strdup\n"); return 1; }

     void* p3 = realloc(p1, 132);        // realloc 同样要在堆内
     if (p3 != NULL && !mi_is_in_heap_region(p3)) { printf("FAIL: realloc\n"); return 1; }

     free(s);                            // 交叉释放：系统接口释放 mi_ 分配的指针
     mi_free(p3 == NULL ? p1 : p3);      // mi_ 接口释放系统接口分配的指针
     puts("PASS: malloc/free overridden by mimalloc");
     mi_stats_print(NULL);
     return 0;
   }
   ```

2. 以**途径②**编译运行（`mimalloc.o` 放最前，需先构建单目标文件）：

   ```bash
   gcc -o verify-override out/release/mimalloc.o verify-override.c -I include
   ./verify-override
   ```

3. 再以**途径①**验证：用普通方式编译一个**不链接** mimalloc 的版本，去掉 `mi_` 调用后与 `LD_PRELOAD` 组合（此时 `mi_is_in_heap_region` 不可用，以 `MIMALLOC_VERBOSE=1` 横幅作为证据，即 4.1.4 的做法）。

**需要观察的现象**：第 2 步输出 `PASS: ...` 且退出码 0；随后打印统计报表。若覆盖未生效（例如把 `mimalloc.o` 挪到 `verify-override.c` 之后、或用 `MI_OVERRIDE=OFF` 的库），第一处 `mi_is_in_heap_region` 就应失败。

**预期结果**：途径② 下 `malloc/strdup/realloc` 的返回值全部落在 mimalloc 堆区域内，交叉释放不报错——这正是 `main-override.c` 官方验证逻辑想证明的命题。第 3 步与 4.1.4 结论互相印证。步骤 2 的链接顺序实验（把 `.o` 挪后是否失效）依赖具体链接器行为，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`dynamic-override` 这个测试程序已经链接了 `libmimalloc.so`，为什么 test/CMakeLists.txt 的注释还说「要用 LD_PRELOAD 才能真正覆盖」？
**答案**：链接共享库只是让程序**可以调用** mimalloc 的 `mi_` 函数；但程序里的 `malloc` 引用默认仍解析到 libc——动态符号查找顺序是「可执行文件 → 预加载库 → 依赖库」，`libmimalloc.so` 作为普通依赖排在 libc 一侧，胜负关系不确定。只有 `LD_PRELOAD` 把它提升到 libc 之前，`malloc` 才稳定指向 mimalloc。程序中的 `mi_is_in_heap_region` 检查正是用来发现「链接了但没覆盖」这种情况的。

**练习 2**：途径③（宏替换）在什么前提下是安全的？给出一个会出问题的具体场景。
**答案**：安全前提是**进程中所有会分配/释放内存的代码都经过宏替换**（全部源码受你控制，且没有第三方预编译库）。出问题的场景：程序 dlopen 一个预编译 `.so`，它内部用系统 `malloc` 生成一个字符串返回给你；你的代码里 `free(s)` 被宏替换成 `mi_free(s)`——该指针在 mimalloc 的 page map 中查无此页，release 构建下这次 free 被静默忽略（[src/free.c:L204-L210](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L204-L210)），内存既不归 mimalloc 也不会被系统释放，形成永久泄漏；debug 构建则报告 invalid pointer。

**练习 3**：`mi_version()` 在 `main-override.c` 里只有一行且返回值被丢弃，为什么不能删？
**答案**：它是「锚点调用」。如果整个程序没有任何对 mimalloc 符号的引用，链接器（尤其对共享库）会认为该依赖未被使用而不加载它，覆盖自然无从谈起。调用一次 `mi_version()` 强制产生引用，确保 `libmimalloc.so` 进入进程。readme 在 Windows 一节给出的等价手段是链接选项 `/include:mi_version`（[readme.md:L517-L520](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L517-L520)）。

## 5. 综合实践

**任务：给一个真实程序做一次完整的「mimalloc 覆盖验收」，收集三份互相印证的证据。**

1. **符号表证据（编译期）**：构建 release 版 mimalloc，用 `nm -D` 确认 `libmimalloc.so` 导出 `malloc/free/calloc/realloc/strdup/__libc_malloc`，且 `malloc` 与 `mi_malloc` 地址相同（4.2.4 的步骤）。
2. **运行期证据**：选一个你机器上现成的、非静态链接的命令行程序（比如自己编译的 `myprogram`，或系统里的某个小工具），分别执行：

   ```bash
   ./myprogram                                   # 基线：系统 malloc
   env MIMALLOC_VERBOSE=1 \
     LD_PRELOAD=$PWD/out/release/libmimalloc.so ./myprogram
   env MIMALLOC_SHOW_STATS=1 MIMALLOC_VERBOSE=1 \
     LD_PRELOAD=$PWD/out/debug/libmimalloc-debug.so ./myprogram
   ```

   记录：版本横幅出现在 `main` 之前的哪个阶段；统计报表里 `malloc` 调用数是否与你程序的分配次数量级吻合。
3. **语义证据**：编译运行 4.3.4 的 `verify-override`（途径②），确认 `malloc`/`strdup`/`realloc` 的指针全部通过 `mi_is_in_heap_region` 检查、交叉释放成功。

完成后写一段 150 字左右的结论，回答：三份证据分别排除了哪种「假成功」？（提示：①排除「库没编出覆盖符号」；②排除「符号有了但没被抢占绑定」；③排除「绑定了但语义对不上」。） macOS 上请把第 2 步换成 `DYLD_INSERT_LIBRARIES` 并注意 readme 提到的 shell 安全限制；涉及具体程序的输出差异**待本地验证**。

## 6. 本讲小结

- 覆盖的本质是**符号游戏**：`alloc-override.c` 导出一组与标准库同名的符号（`malloc`/`free`/`strdup`/`__libc_malloc`/C++ `new`…），靠 ELF 的 `LD_PRELOAD`（或 macOS `__interpose` 段、MSVC 的 CRT 定义）在符号解析时抢占 libc。
- GCC/Clang 下 `MI_FORWARD` 用 `__attribute__((alias(...)))` 让 `malloc` 与 `mi_malloc` **同地址**，覆盖零转发开销；别名要求同一翻译单元，所以 `alloc-override.c` 和 `free.c` 都被 `#include` 进 `alloc.c`，由 `MI_IN_ALLOC_C` 宏守卫。
- 编译期链路：cmake `MI_OVERRIDE`（默认 ON）→ 目标私有宏 `MI_MALLOC_OVERRIDE` → 源码 `#if` 分支，配套 `-fno-builtin-malloc` 防止编译器按内建 `malloc` 的语义做优化。
- 预加载库用 `__attribute__((constructor))` 在 `main` 前完成初始化，`MIMALLOC_VERBOSE=1` 时打印 `v3.5.0` 版本横幅——这是验证覆盖生效的最干净信号。
- 三条覆盖途径各有其位：`LD_PRELOAD`（零改动、推荐）、`mimalloc.o` 放链接命令最前（目标文件定义优先于库）、`mimalloc-override.h` 宏替换（全平台但要防**跨堆指针混用**——`mi_free` 对查不到归属页的指针在 release 下会静默忽略，造成隐性泄漏）。
- 验证覆盖的组合拳：`nm -D` 看符号表 + `MIMALLOC_VERBOSE/SHOW_STATS` 看运行时输出 + `mi_is_in_heap_region` 做语义断言（官方示范见 `test/main-override.c`）。

## 7. 下一步学习建议

下一讲 **u2-l2（C++ 集成：new/delete 覆盖与 mi_stl_allocator）**顺着本讲的 C++ 伏笔展开：`alloc-override.c` 中那些 `_Znwm`/`_ZdlPv` 修饰名是什么、`mimalloc-new-delete.h` 如何替换全局 `new`/`delete`、以及 `mi_stl_allocator` 怎么让 STL 容器直接用 mimalloc。如果你想先歇一口气，建议重读 [src/alloc-override.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-override.c) 的 macOS 分支与 [src/prim/osx/alloc-override-zone.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/osx/alloc-override-zone.c)（malloc zone 机制），体会同一问题在不同操作系统上的三种解法；等进入单元三后再回头看本讲的 `mi_is_in_heap_region`，你会对它背后的 page map 有更深的理解。
