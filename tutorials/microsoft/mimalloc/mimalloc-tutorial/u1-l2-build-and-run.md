# 构建与运行：cmake、debug/secure 模式与单文件编译

## 1. 本讲目标

上一讲我们知道了 mimalloc 是什么、它靠 free list 分片拿到了高性能。本讲解决一个非常实际的问题：**把它编译出来**。学完本讲你应该能够：

1. 用 cmake 独立完成 release、debug、secure 三种构建，并说出三种构建产物文件名的差异及其由来。
2. 解释 `MI_OVERRIDE`、`MI_SECURE`、`MI_DEBUG`、`MI_GUARDED` 等构建开关分别控制什么，它们如何从 cmake 选项一步步变成 C 宏。
3. 说出一次构建会产出哪四类目标（共享库、静态库、单目标文件、测试程序），以及 `src/static.c` 为什么要把整个库合并成一个 `.o` 文件。
4. 会用 `test/CMakeLists.txt` 这个官方示例工程验证「安装后如何链接 mimalloc」。

## 2. 前置知识

### 2.1 cmake 的 out-of-source 构建

mimalloc 用 [cmake](https://cmake.org) 作为构建系统。cmake 的常见用法是「源码目录」和「构建目录」分离：

```text
mimalloc/               ← 源码目录（CMakeLists.txt 在这里）
└── out/release/        ← 构建目录（cmake 生成的中间文件、最终库文件都在这里）
```

在构建目录里执行 `cmake ../..`，意思是「以两级之上的源码目录为根进行配置」；随后 `make`（或 `cmake --build .`）真正执行编译。官方文档给出的正是这套流程（见 [readme.md:184-196](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L184-L196)）。

### 2.2 三种库产物形态

| 形态 | Linux 产物 | 链接方式 | 特点 |
|---|---|---|---|
| 共享库 shared | `libmimalloc.so` | 运行时动态加载 | 可用 `LD_PRELOAD` 注入，体积一份 |
| 静态库 static | `libmimalloc.a` | 编译期打包进可执行文件 | 部署简单，但链接顺序有讲究 |
| 单目标文件 object | `mimalloc.o` | 直接作为一个 `.o` 参与链接 | 静态覆盖 malloc 最可靠的方式 |

### 2.3 构建期开关 vs 运行期选项

上一讲提过 `MIMALLOC_SHOW_STATS` 这类**运行期**环境变量。本讲讲的是另一类：**构建期开关**（如 `MI_SECURE=ON`）。区别在于：

- 构建期开关在 `cmake` 配置时就被翻译成 C 宏（例如 `-DMI_SECURE=4`），直接影响哪些代码被编译进去，改了就要重新编译。
- 运行期选项在进程启动时解析，不重新编译就能调整。

本讲的关键就是看懂「cmake 选项 → C 宏 → 源码中 `#if` 分支」这条链。

### 2.4 术语回顾（承接 u1-l1）

上一讲建立的术语在本讲会用到：**free list**（空闲块链表）、**size class**（尺寸分类）、**mimalloc page**（约 64KiB 的页）、**reserve/commit**（虚拟内存的预留与提交）。构建开关 `MI_SECURE` 会影响 free list 指针是否加密、页尾是否加 guard page——这些机制本身在单元九详述，本讲只关心它们由哪个开关打开。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt) | 根构建脚本：定义全部选项、推导 C 宏、创建四个构建目标、决定库文件命名 |
| [src/static.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c) | 单文件翻译单元：`#include` 所有 `.c` 源文件，把整个库合成一个目标文件 |
| [test/CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt) | 安装后的示例工程：演示动态/静态/单目标文件四种链接与覆盖方式 |
| [test/main.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main.c) | 一个最小但完整的 `mi_` API 示例程序（含堆、对齐分配、统计输出） |
| [cmake/mimalloc-config-version.cmake](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/cmake/mimalloc-config-version.cmake) | 版本号 3.5.0 的定义处，决定 `libmimalloc.so.3` 里的 `3` |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | 内部头文件，`MI_DEBUG` 宏在这里被消费成三级断言 |
| [readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md) | 官方构建说明（Building 一节），本讲实践的命令出处 |

## 4. 核心概念与源码讲解

### 4.1 构建总览：一次配置产出四类目标

#### 4.1.1 概念说明

很多库只生成一种产物，mimalloc 一次 `cmake` 配置默认同时生成**四类**目标：共享库、静态库、单目标文件、测试程序。这不是冗余，而是对应四种真实的使用场景（后面单元二的「覆盖 malloc」三种途径都依赖这里的产物形态）。

理解本模块只需要抓住三个问题：

1. 源文件清单是什么？
2. 四个目标分别叫什么、产物在哪？
3. 为什么同一份源码要有这么多形态？

#### 4.1.2 核心流程

```text
cmake ../.. 配置阶段
  ├─ 读 CMakeLists.txt 顶部的一堆 option(...)          ← 用户开关
  ├─ 组装 mi_sources（19 个 .c 文件）
  ├─ 按开关推导 mi_defines（-DMI_SECURE=4 这类 C 宏）
  ├─ 决定库名 mi_libname（mimalloc / mimalloc-debug / ...）
  └─ 创建目标：mimalloc(共享) mimalloc-static(静态) mimalloc-obj(单.o) + 测试
make 阶段
  └─ 按目标分别编译/归档/链接，产出 .so / .a / .o / 可执行文件
```

#### 4.1.3 源码精读

**源文件清单**：整个库由 19 个 `.c` 文件组成，在根 CMakeLists 一开头就列清楚了：

[CMakeLists.txt:90-109](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L90-L109) 定义了 `mi_sources` 列表，从 `src/alloc.c`（分配主链路）、`src/arena.c`（内存区管理）到 `src/prim/prim.c`（OS 抽象层）——这张清单就是单元三到单元七的阅读目录，本讲先混个眼熟即可。

**四个构建开关默认全开**：

[CMakeLists.txt:30-33](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L30-L33) 声明 `MI_BUILD_SHARED`、`MI_BUILD_STATIC`、`MI_BUILD_OBJECT`、`MI_BUILD_TESTS` 四个选项，默认全部 `ON`。所以默认一次构建会得到：`libmimalloc.so`、`libmimalloc.a`、`mimalloc.o` 和一串 `mimalloc-test-*` 可执行文件。

**三个主目标**：

[CMakeLists.txt:812-823](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L812-L823) 创建共享库目标 `mimalloc`：注意 `VERSION ${mi_version}` / `SOVERSION ${mi_version_major}` / `OUTPUT_NAME ${mi_libname}` 三个属性——在 Linux 上它们最终生成 `libmimalloc.so → libmimalloc.so.3 → libmimalloc.so.3.5` 这样一条符号链接链。版本号 `3.5` 来自 [cmake/mimalloc-config-version.cmake:1-4](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/cmake/mimalloc-config-version.cmake#L1-L4)，当前是 v3.5.0。

[CMakeLists.txt:867-880](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L867-L880) 创建静态库目标 `mimalloc-static`，产出 `libmimalloc.a`；注意它强制 `POSITION_INDEPENDENT_CODE ON`，这样静态库也能被链接进共享库。

[CMakeLists.txt:891-922](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L891-L922) 创建单目标文件目标 `mimalloc-obj`，输入只有 `src/static.c` 一个文件，编译出 `static.c.o` 后再用 `cmake -E copy` 复制为构建目录下的 `mimalloc.o`（见 L909-911）。为什么一个 `.c` 能代表整个库？答案在 4.4 节。

#### 4.1.4 代码实践

1. **实践目标**：完成第一次 release 构建，亲眼确认四类产物。
2. **操作步骤**：
   ```bash
   git clone https://github.com/microsoft/mimalloc.git
   cd mimalloc
   mkdir -p out/release && cd out/release
   cmake ../..          # 配置阶段，注意阅读 STATUS 输出
   make                 # 或 cmake --build . -j
   ```
3. **需要观察的现象**：配置结束时 cmake 打印的摘要块（`Library name`、`Version`、`Build type`、`Compiler defines`、`Build targets` 等行），它把你这次构建的全部关键决定都摊开在了屏幕上。
4. **预期结果**：`out/release/` 下出现 `libmimalloc.so*`（符号链接链）、`libmimalloc.a`、`mimalloc.o` 以及 `mimalloc-test-api` 等测试可执行文件；摘要中 `Library name : mimalloc`、`Build type : release`。具体文件清单待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如何让构建只产出共享库、不产出静态库和单目标文件？
**答案**：配置时加 `-DMI_BUILD_STATIC=OFF -DMI_BUILD_OBJECT=OFF`。这三个开关在 [CMakeLists.txt:30-33](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L30-L33)，彼此独立。

**练习 2**：`libmimalloc.so.3` 里的 `3` 是从哪来的？
**答案**：来自 `SOVERSION ${mi_version_major}`（[CMakeLists.txt:814](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L814)），而 `mi_version_major` 定义为 `3`（[cmake/mimalloc-config-version.cmake:1-4](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/cmake/mimalloc-config-version.cmake#L1-L4)）。

**练习 3**：不看本讲义，说出 19 个源文件里你已经听过名字的至少 3 个，以及各自的职责（可回看 4.1.3 第一条引用）。
**答案**：`src/alloc.c`——分配入口；`src/arena.c`——大块内存区管理；`src/prim/prim.c`——OS 原语抽象层入口。说出任意三个并职责大致正确即可。

---

### 4.2 构建类型与库名：release/debug/secure 是怎么定的

#### 4.2.1 概念说明

很多初学者的困惑是：「我明明没传 `-DCMAKE_BUILD_TYPE`，为什么构建出来的库有时叫 `libmimalloc.so`、有时叫 `libmimalloc-debug.so`？」答案是 mimalloc 在 CMakeLists 里做了两层**根据构建目录名自动推断**的便利逻辑。理解它，你就能解释 readme 里那些目录命名约定（`out/release`、`out/debug`、`out/secure`）并不是随意的。

#### 4.2.2 核心流程

```text
用户没有显式指定 CMAKE_BUILD_TYPE 时：
  构建目录名以 debug/asan/tsan/ubsan/valgrind 结尾？
    ├─ 是 → 默认 Debug
    └─ 否 → 默认 Release

用户没有显式指定 MI_SECURE 时：
  构建目录名以 secure（不区分大小写）结尾？
    ├─ 是 → 自动 MI_SECURE=ON
    └─ 否 → 维持 OFF

最终库名 = mimalloc
         + (secure 构建加 "-secure")
         + (非 release 系构建类型追加 "-<buildtype>")   ← debug 构建得到 mimalloc-debug
```

#### 4.2.3 源码精读

**第一层：按目录名默认构建类型**：

[CMakeLists.txt:140-148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L140-L148) 在未指定 `CMAKE_BUILD_TYPE` 时检查 `CMAKE_BINARY_DIR`（即构建目录路径）：若匹配 `.*((D|d)ebug|asan|tsan|ubsan|valgrind)$` 就默认 `Debug`，否则默认 `Release`。所以 `mkdir -p out/debug && cd out/debug && cmake ../..` 即使不传 `-DCMAKE_BUILD_TYPE=Debug` 也会得到 debug 构建；而 `out/release` 不匹配任何模式，落入 Release 默认值。

**第二层：按目录名自动开启 secure**：

[CMakeLists.txt:155-158](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L155-L158) 在未设置 `MI_SECURE` 时，若构建目录名以 `(S|s)ecure` 结尾就自动 `MI_SECURE=ON`。这正是 readme 中 `mkdir -p out/secure && cd out/secure && cmake -DMI_SECURE=ON ../..` 写法的底层依据（readme 的显式传参与目录推断二者取其一即可）。

**库名拼接规则**：

[CMakeLists.txt:759-776](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L759-L776) 是库名的完整决策逻辑：基础名 `mimalloc`；secure 构建追加 `-secure`（L760-762）；带 Valgrind/ASAN 追踪时再追加 `-valgrind`/`-asan`；最后若构建类型小写后**不是** `release|relwithdebinfo|minsizerel|none` 之一，就把构建类型追加到名字里（L771-775）。于是：

| 构建方式 | 共享库名 | 静态库名 | 单目标文件名 |
|---|---|---|---|
| release | `libmimalloc.so` | `libmimalloc.a` | `mimalloc.o` |
| debug | `libmimalloc-debug.so` | `libmimalloc-debug.a` | `mimalloc-debug.o` |
| secure（release 类型） | `libmimalloc-secure.so` | `libmimalloc-secure.a` | `mimalloc-secure.o` |

readme 中对应的文字佐证：debug 构建命名为 `libmimalloc-debug.so` 见 [readme.md:198-208](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L198-L208)，secure 构建命名为 `libmimalloc-secure.so` 见 [readme.md:210-219](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L210-L219)。

**为什么 debug 构建要改名？** 因为它是为排查问题而生、带大量断言和额外检查的版本（见 4.3 节），性能明显更差。文件名带 `-debug` 后缀，可以和正式版安装在同一目录并存而不冲突，也防止你误把它链进生产程序。

#### 4.2.4 代码实践

1. **实践目标**：验证「目录名影响构建类型」这条隐藏规则，并记录 release 与 debug 两种构建的产物文件名差异。
2. **操作步骤**：
   ```bash
   # release（目录名不含关键字 → 默认 Release）
   mkdir -p out/release && cd out/release && cmake ../.. && make
   
   # debug（方式一：目录名推断；方式二：显式指定，二者等价）
   cd ../..
   mkdir -p out/debug && cd out/debug
   cmake ../..                      # 依赖目录名推断
   # 或者：cmake -DCMAKE_BUILD_TYPE=Debug ../..
   make
   
   # 对照组：中性目录名，不传任何参数
   cd ../..
   mkdir -p out/foo && cd out/foo && cmake ../..    # 观察它默认成了什么类型
   ```
3. **需要观察的现象**：三个目录各自的 cmake 摘要中 `Build type` 一行；`ls` 查看 `out/release` 与 `out/debug` 下的库文件名。
4. **预期结果**：`out/release` 与 `out/foo` 都是 `release` 类型、产出 `libmimalloc.so` / `libmimalloc.a` / `mimalloc.o`；`out/debug` 是 `debug` 类型、产出 `libmimalloc-debug.so` / `libmimalloc-debug.a` / `mimalloc-debug.o`。若把第三个目录命名为 `out/secure`，还应看到自动启用的 secure 提示。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：目录分别命名为 `out/tsan` 和 `out/tsan-test`，各执行 `cmake ../..`（不传任何变量），分别默认什么构建类型？
**答案**：`out/tsan` → Debug；`out/tsan-test` → Release。因为推断正则 `.*((D|d)ebug|asan|tsan|ubsan|valgrind)$`（[CMakeLists.txt:141-147](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L141-L147)）匹配的是目录名**结尾**，`tsan-test` 以 `test` 结尾、不匹配任何关键词，于是落入 Release 默认值。这个对比提醒我们：命名构建目录时关键词必须放在末尾。

**练习 2**：`-DCMAKE_BUILD_TYPE=RelWithDebInfo` 构建出的库叫什么名字？
**答案**：`libmimalloc.so`（不加后缀）。因为 `relwithdebinfo` 在「不追加构建类型后缀」的白名单里（[CMakeLists.txt:771-775](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L771-L775)），它被视为 release 系构建，同时会额外定义 `MI_BUILD_RELEASE` 宏。

**练习 3**：为什么 mimalloc 要给 debug 库改文件名，而大多数普通项目不改？
**答案**：因为 mimalloc 的 debug 构建会启用内部不变量断言和 guarded 分配（见 4.3 节），性能差距大、用途完全不同；改名让 debug/release 两个版本可并存、可显式选择，避免误链接。这是分配器这类「被所有程序依赖的基础库」特有的工程考量。

---

### 4.3 关键构建开关：MI_OVERRIDE、MI_SECURE、MI_DEBUG、MI_GUARDED

#### 4.3.1 概念说明

CMakeLists.txt 顶部定义了几十个选项（用 `cmake ../.. -LH` 可全部列出，见 [readme.md:220](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L220)）。初学阶段只需掌握四个最核心的：

| 开关 | 默认值 | 控制什么 |
|---|---|---|
| `MI_OVERRIDE` | `ON` | 是否定义与 `malloc`/`free` 同名的导出符号（覆盖标准分配器） |
| `MI_SECURE` | `OFF` | 安全缓解措施：元数据 guard page、free list 加密、double-free 检测 |
| `MI_DEBUG` | `DEFAULT` | 断言级别：OFF / ON / INTERNAL / FULL / DEFAULT |
| `MI_GUARDED` | `OFF` | 在部分对象后面放置 OS 级 guard page（按采样率） |

它们共同的模式是：**cmake 选项 → `list(APPEND mi_defines ...)` 加一个 C 宏 → 源码里 `#if` 选择分支**。

#### 4.3.2 核心流程

```text
MI_OVERRIDE=ON  →  给三个目标定义 MI_MALLOC_OVERRIDE 宏
                 + 编译器加 -fno-builtin-malloc（阻止编译器内联/优化掉 malloc 调用）

MI_SECURE=ON    →  宏 MI_SECURE=4
MI_SECURE=FULL  →  宏 MI_SECURE=5（每个 mimalloc page 尾部再加 guard page，代价高）

MI_DEBUG=DEFAULT → Debug 构建 → INTERNAL；其他 → OFF
MI_DEBUG=ON/INTERNAL/FULL → 宏 MI_DEBUG=1/2/3（三级断言逐级增强）
MI_DEBUG 生效时  →  自动打开 MI_GUARDED → 宏 MI_GUARDED=1
```

#### 4.3.3 源码精读

**MI_OVERRIDE——覆盖 malloc 的总开关**：

[CMakeLists.txt:10](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L10) 定义 `MI_OVERRIDE` 默认 `ON`，即默认构建出来的库就「长着」`malloc`、`free` 这些标准名字的函数。它的两个落地动作：其一，[CMakeLists.txt:641-645](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L641-L645) 为 GCC/Clang 加 `-fno-builtin-malloc`，防止编译器把 `malloc(16)` 当内建函数优化掉、绕过覆盖；其二，[CMakeLists.txt:1014-1024](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L1014-L1024) 给共享/静态/单目标三个目标统一加上 `MI_MALLOC_OVERRIDE` 宏，源码中由它激活 `alloc-override.c` 的定义（具体机制是单元二 u2-l1 的主题）。

**MI_SECURE——安全模式**：

[CMakeLists.txt:7-8](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L7-L8) 声明它是三值选项 `OFF/ON/FULL`；[CMakeLists.txt:284-290](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L284-L290) 把它翻译成宏：`ON` → `MI_SECURE=4`，`FULL` → `MI_SECURE=5`。`FULL` 的注释写明其额外代价是在**每个 mimalloc page 尾部**放 guard page——上一讲说过页约 64KiB、数量众多，所以这个模式可能明显变慢。安全模式的具体防护手段（free list 加密、double-free 检测）在单元九 u9-l1 精读。

**MI_DEBUG——三级断言**：

[CMakeLists.txt:21-22](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L21-L22) 声明五档取值；[CMakeLists.txt:373-394](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L373-L394) 完成翻译，其中最关键的是 `DEFAULT` 的解析规则（L373-379）：Debug 构建自动取 `INTERNAL`，其他构建取 `OFF`。宏的消费端在 [include/mimalloc/internal.h:347-365](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L347-L365)：

```c
#if (MI_DEBUG)                 // MI_DEBUG>=1：mi_assert 生效
#define mi_assert(expr)  ((expr) ? (void)0 : _mi_assert_fail(...))
#if (MI_DEBUG>1)               // MI_DEBUG>=2：内部不变量断言也生效
#define mi_assert_internal    mi_assert
#if (MI_DEBUG>2)               // MI_DEBUG>=3：昂贵断言也生效
#define mi_assert_expensive   mi_assert
```

也就是说源码里到处写的 `mi_assert_internal(page->capacity <= page->reserved)` 这类检查，在 release 构建里被预处理器直接削成空宏、零开销；在 debug 构建里则真的执行并在失败时打印断言位置（`_mi_assert_fail` 不用堆内存即可打印，避免自食其果）。

**MI_GUARDED——守卫页采样**：

[CMakeLists.txt:13](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L13) 声明默认 `OFF`；但 [CMakeLists.txt:396-403](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L396-L403) 有一条联动规则：只要 `MI_DEBUG` 生效就自动打开 `MI_GUARDED`，并定义宏 `MI_GUARDED=1`。这就是「debug 构建默认带 guard page 采样」的出处。运行时采样频率由 `MIMALLOC_GUARDED_SAMPLE_RATE` 控制——根构建的测试在 `MI_GUARDED` 打开时正是以采样率 1 运行测试的（[CMakeLists.txt:965-966](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L965-L966)）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「开关 → cmake 摘要 → C 宏」这条链的每一环。
2. **操作步骤**：
   ```bash
   cd out/debug && cmake ../.. -LH | head -60     # 列出所有选项及说明
   ```
   然后重新配置并阅读摘要：
   ```bash
   cmake ../.. 2>&1 | grep -E "MI_GUARDED|MI_DEBUG|debug level"
   # 再试一个显式开关：
   cmake ../.. -DMI_DEBUG=FULL 2>&1 | grep -i "debug level"
   ```
3. **需要观察的现象**：debug 构建的摘要中出现 `Enable MI_GUARDED (since MI_DEBUG is enabled)` 与 `Set debug level to internal assertion and invariant checking (MI_DEBUG=INTERNAL)` 两行；换成 `-DMI_DEBUG=FULL` 后变成 `full assertion and internal invariant checking ... expensive`。
4. **预期结果**：确认 4.3.2 的推导链条与实际输出一致；进一步可在 `out/debug/CMakeFiles/mimalloc.dir/flags.make`（make 生成器）里找到 `-DMI_DEBUG=2`、`-DMI_GUARDED=1` 这样的宏定义（文件名与内容随生成器不同而异，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：release 构建里 `mi_assert_internal(x)` 展开成什么？
**答案**：空。因为 release 构建 `MI_DEBUG` 解析为 `OFF`，宏 `MI_DEBUG` 未定义（值为 0），`#if (MI_DEBUG>1)` 不成立，[internal.h:355-359](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L355-L359) 把它定义为空宏。

**练习 2**：`MI_SECURE=FULL` 和 `MI_SECURE=ON` 的区别是什么？代价各如何？
**答案**：`ON` → 宏 `MI_SECURE=4`，启用常规缓解（元数据 guard page、free list 加密、double-free 检测）；`FULL` → `MI_SECURE=5`，在此之上为**每个 mimalloc page** 尾部加 guard page，CMake 注释明确标注 may be expensive（[CMakeLists.txt:284-290](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L284-L290)）。

**练习 3**：为什么 `MI_OVERRIDE=ON` 时要加 `-fno-builtin-malloc`？
**答案**：GCC/Clang 会把 `malloc` 当内建函数做优化（如合并、消除调用），优化后的调用可能绕过库里的覆盖符号。加 `-fno-builtin-malloc`（[CMakeLists.txt:641-645](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L641-L645)）保证每次 `malloc` 都是真的函数调用，才能被 mimalloc 的同名符号接管。

---

### 4.4 src/static.c：单文件编译与静态覆盖

#### 4.4.1 概念说明

`src/static.c` 只有 40 多行，几乎全是 `#include`，却是理解「mimalloc 如何静态替换 malloc」的钥匙。它解决的问题：**Unix 链接器解析符号时，命令行上靠前的目标文件（`.o`）中的定义优先于库文件（`.a`）中的定义**。如果把整个 mimalloc 打成一个 `.o` 放在链接命令最前面，程序里所有 `malloc`/`free` 调用都会绑定到它， libc 里的同名函数根本没机会出场。

如果不用单目标文件、直接链 `libmimalloc.a` 行不行？经常可以，但不可靠——CMake 无法完全控制链接命令中 libc 与 mimalloc 的先后顺序。所以官方提供了这个「最可预测」的形态。

#### 4.4.2 核心流程

```text
src/static.c（一个翻译单元）
  ├─ #include "alloc.c"        ← 而 alloc.c 内部又包含 alloc-override.c 和 free.c
  ├─ #include 其余 17 个 .c
  └─ 编译 → static.c.o → 复制为 mimalloc.o

用户程序链接：gcc main.c mimalloc.o ...   ← mimalloc.o 在前
  └─ 链接器先在 mimalloc.o 里找到 malloc/free 定义 → libc 的版本被忽略
```

#### 4.4.3 源码精读

**设计意图的官方注释**：

[src/static.c:19-22](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L19-L22) 写得非常清楚：为一个静态覆盖创建包含整个库的单一目标文件，**只要它被最先链接，就会覆盖所有标准库分配函数（在 Unix 上）**。

**把整个库「叠」进一个翻译单元**：

[src/static.c:23-41](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L23-L41) 依次 `#include` 了全部实现文件。注意 L23 的注释：`alloc.c` 自身又包含 `alloc-override.c`（定义 `malloc`/`free` 等同名覆盖符号）和 `free.c`——所以 `MI_OVERRIDE=ON` 时的覆盖入口也一并进入了这个 `.o`。L33 的注释说明 `page.c` 亦内含 `page-queue.c`。文件末尾 L42-44 还条件编译了 macOS 的 zone 覆盖实现。

这种「`.c` 包 `.c`」的写法在日常工程里是坏味道，但在分配器这里有明确动机：所有符号在同一个目标文件内，链接优先级最高、且不会因为链接器按需提取 `.a` 成员而导致某些覆盖符号被落下。readme 也提供了不用 cmake 的对应用法：直接把 `src/static.c` 当作项目里的一个源文件编译，只需把 mimalloc 的 `include` 目录加进头文件搜索路径（[readme.md:252-255](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L252-L255)）。

**官方测试如何使用它**：

根构建里就有一个活例子：[CMakeLists.txt:975-982](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L975-L982) 构建的可执行文件 `mimalloc-test-stress-static` 用的正是 `mimalloc-obj` 目标（注释：override statically with an object file），并在运行时打开 `MIMALLOC_VERBOSE=1` 和 `MIMALLOC_SHOW_STATS=1`，让你能直接看到程序用的是 mimalloc。

#### 4.4.4 代码实践

1. **实践目标**：确认 `mimalloc.o` 里真的住着 `malloc`/`free` 的定义。
2. **操作步骤**（接 4.2.4 的 release 构建）：
   ```bash
   cd out/release
   ls -la mimalloc.o
   nm mimalloc.o | grep -E "T (malloc|free|calloc|realloc)$"
   ```
3. **需要观察的现象**：`nm` 输出中 `malloc`、`free` 等符号的标志为 `T`（表示该符号定义在本文件的 text 段），而不是 `U`（undefined，未定义待外部解析）。
4. **预期结果**：能看到若干 `T` 型的标准分配函数符号——这正是 `MI_OVERRIDE=ON`（默认）加 `MI_MALLOC_OVERRIDE` 宏的产物。不同平台与编译器下符号名可能有修饰（如 macOS 前缀下划线），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`mimalloc.o` 与 `libmimalloc.a` 都由同一批源文件生成，本质区别是什么？
**答案**：`.a` 是归档文件，链接器**按需**从中提取成员——只有当程序显式引用了某成员的符号它才被链接；`.o` 是单个目标文件，放在链接命令前面时其中**所有**符号（包括 `malloc` 覆盖）都整体参与符号解析且优先级高于库文件。这正是 [test/CMakeLists.txt:32-34](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L32-L34) 注释所说「目标文件中的符号优先于库文件中的符号」。

**练习 2**：为什么 `static.c` 里 L23 包含的是 `alloc.c` 而不是直接包含 `alloc-override.c`？
**答案**：`alloc.c` 内部已经包含了 `alloc-override.c` 与 `free.c`（见 [static.c:23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L23) 的注释）。再直接包含会造成同一翻译单元内的重复定义。`static.c` 只需按依赖顺序包含顶层实现文件。

**练习 3**：不用 cmake，如何把 mimalloc 编进自己的项目？
**答案**：把 `src/static.c` 当成项目中的一个普通源文件一起编译，并把 mimalloc 仓库的 `include` 目录加到头文件搜索路径（[readme.md:252-255](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L252-L255)）。不过这样就失去了 cmake 构建期开关的保护，需要自己传对宏。

---

### 4.5 test/CMakeLists.txt 与 test/main.c：安装之后的官方用法样例

#### 4.5.1 概念说明

仓库里有两个 CMakeLists：根目录的负责**构建库本身**，`test/CMakeLists.txt` 则是一个**独立的示例工程**——它模拟「你已经 `sudo make install` 安装了 mimalloc，现在要在自己的项目里使用」的场景。它用 `find_package` 找到已安装的库，然后一口气演示了四种链接/覆盖方式，是官方钦定的最佳实践参考。

而 `test/main.c` 是一个不到 50 行的完整示例程序，把 `mi_` API 的典型用法走了一遍，非常适合作为你的第一个 mimalloc 程序的模板。

#### 4.5.2 核心流程

```text
test/CMakeLists.txt（独立工程，假设 mimalloc 已安装）
  ├─ find_package(mimalloc CONFIG REQUIRED)       ← 从安装目录找库
  ├─ dynamic-override        ← 链共享库，运行时用 LD_PRELOAD 覆盖
  ├─ static-override-obj     ← 链 mimalloc.o 单目标文件（最可靠的静态覆盖）
  ├─ static-override-static  ← 链静态库 + mimalloc-override.h 宏替换
  ├─ static-override         ← 直接链静态库（依赖链接顺序，不保证可靠）
  └─ test-wrong              ← 故意写错内存，用于验证检测能力
```

#### 4.5.3 源码精读

**find_package 与四种覆盖方式**：

[test/CMakeLists.txt:18-20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L18-L20) 用 `find_package(mimalloc CONFIG REQUIRED)` 定位已安装的 mimalloc——这依赖根构建里 [CMakeLists.txt:822-823](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L822-L823) 安装的导出配置。

[test/CMakeLists.txt:25-29](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L25-L29) 创建 `dynamic-override` 目标：链接共享库 `mimalloc`，注释点明要真正在运行时覆盖 malloc/free 需要配合 `LD_PRELOAD`（这是下一讲 u2-l1 的主角）。

[test/CMakeLists.txt:32-36](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L32-L36) 创建 `static-override-obj`：注意它的源文件列表里直接放上了 `${MIMALLOC_OBJECT_DIR}/mimalloc.o`——把 4.4 节讲的单目标文件编进可执行文件，注释明说这是「可靠的」静态覆盖方式，因为目标文件符号优先于库文件。

[test/CMakeLists.txt:39-51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L39-L51) 给出另外两种静态玩法：`static-override-static` 配合 `mimalloc-override.h` 头文件把 `malloc` 宏替换成 `mi_malloc`；`static-override` 直接链静态库并注明「如果库在命令行上链接得太晚会失效」——诚实展示了这种方式的局限。

**test/main.c——最小完整示例**：

[test/main.c:25-45](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main.c#L25-L45) 的 `main` 依次演示：小对象与大对象分配释放（`mi_malloc(16)`、`mi_malloc(1000000)`）、一等堆的创建与整堆销毁（`test_heap` 内的 `mi_heap_new`/`mi_heap_destroy`，见 L5-12）、对齐分配（`mi_malloc_aligned`），最后 `mi_collect(true)` 强制回收并 `mi_stats_print(NULL)` 打印统计。需要说明：`main.c` 是独立示例，**不是**根构建的自动化测试目标（自动化测试用的是 `test-api.c`、`test-stress.c` 等，见根 [CMakeLists.txt:953-972](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L953-L972)），所以我们要手动编译它——这正好练习「怎么链 mimalloc」。

#### 4.5.4 代码实践

1. **实践目标**：手动编译并运行 `test/main.c`，得到你自己的第一份 mimalloc 统计输出。
2. **操作步骤**（接前面的构建，任选一条链）：
   ```bash
   cd mimalloc                      # 仓库根目录
   # 方式 A：链静态库（最简单，不依赖运行时库路径）
   gcc -Iinclude -o /tmp/midemo test/main.c out/release/libmimalloc.a -lpthread
   # 方式 B：链共享库
   gcc -Iinclude -o /tmp/midemo test/main.c -Lout/release -lmimalloc
   LD_LIBRARY_PATH=out/release /tmp/midemo
   # 方式 A 则直接运行：
   /tmp/midemo
   ```
3. **需要观察的现象**：终端输出的统计报表——包含各 size class 的 bin 分布、`malloc`/`reuse` 计数、进程峰值内存等段落。
4. **预期结果**：程序正常退出并打印类似 `heap stats` 的多行统计；其中能看到 16 字节级别的小对象分配与 1000000 字节的巨大对象分配各归入不同统计类别。输出格式与数值取决于版本与平台，待本地验证。若用 `out/debug` 下的 `libmimalloc-debug.a` 重编译，输出可能包含更细的检查信息。

#### 4.5.5 小练习与答案

**练习 1**：`test/CMakeLists.txt` 里的 `static-override`（直接链静态库）为什么被注释为「可能不工作」？
**答案**：见 [test/CMakeLists.txt:45-46](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/CMakeLists.txt#L45-L46)：如果 mimalloc 静态库在链接命令行上排在 C 运行时库之后，libc 里的 `malloc` 定义会先被解析，覆盖失效；而 CMake 对链接顺序的控制力有限。

**练习 2**：`test/main.c` 中 `mi_heap_new()` + `mi_heap_destroy()` 与成对的 `mi_malloc`/`mi_free` 相比，管理内存的方式有何本质不同？
**答案**：前者是**一等堆**（first-class heap）用法——在专门堆里分配的对象可以随 `mi_heap_destroy` 一次性全部回收，无需逐个 free（见 [test/main.c:5-12](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main.c#L5-L12)，L11 被注释掉的逐个释放代码正好反衬这一点）。这是 v3 的重要特性，单元七 u7-l3 会专门讲。

**练习 3**：运行 `/tmp/midemo` 前设置 `MIMALLOC_SHOW_STATS=1` 与不设置，输出有何区别？
**答案**：本例中**没有**区别——因为程序显式调用了 `mi_stats_print(NULL)`（[test/main.c:44](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main.c#L44)），统计一定会打印。`MIMALLOC_SHOW_STATS=1` 的作用是在**进程退出时**自动打印统计，二者是两条独立通路。如果删掉 L44 这行再对比，差异就会显现。

## 5. 综合实践

**任务：构建矩阵 + 产物对照表。** 把本讲所有知识串成一张表。

1. 在仓库根目录依次完成三种构建：
   ```bash
   mkdir -p out/release && cd out/release && cmake ../.. && make && cd ../..
   mkdir -p out/debug   && cd out/debug   && cmake -DCMAKE_BUILD_TYPE=Debug ../.. && make && cd ../..
   mkdir -p out/secure  && cd out/secure  && cmake -DMI_SECURE=ON ../.. && make && cd ../..
   ```
2. 对每个构建目录，用 `ls` 和 4.2 节的命名规则填出下表（release 一行已示例）：

   | 构建目录 | 共享库 | 静态库 | 单目标文件 | cmake 摘要中的关键 define |
   |---|---|---|---|---|
   | out/release | `libmimalloc.so` | `libmimalloc.a` | `mimalloc.o` | `MI_SECURE` 不出现，`MI_DEBUG` 关闭 |
   | out/debug | ？ | ？ | ？ | ？ |
   | out/secure | ？ | ？ | ？ | ？ |

3. 用同一个 `test/main.c` 分别链 release 与 debug 静态库编译两份可执行文件，运行并对比：统计输出是否一致？debug 版是否多出断言/检查类信息？故意把 `main.c` 里某次 `mi_free(p1)` 改成两次 `mi_free(p1)`（改完记得还原），分别在两个版本下运行，观察哪个版本报错、报错信息长什么样。
4. 运行构建自带的测试套件收尾：
   ```bash
   cd out/release && ctest --output-on-failure
   ```
   预期能看到 `test-api`、`test-stress`、`test-stress-static`（带 `MIMALLOC_VERBOSE=1`）等条目通过。

**验收标准**：表格三行全部填对并能解释命名差异的来源（回看 4.2.3）；能说出 double-free 在哪个版本被抓住、抓住它的大致是哪类机制（提示：debug 构建的断言与 guarded，或 secure 构建的检查——具体报错文本待本地验证）。

## 6. 本讲小结

- 一次 cmake 配置默认产出四类目标：共享库 `mimalloc`、静态库 `mimalloc-static`、单目标文件 `mimalloc-obj`（来自 `src/static.c`）和一组测试，由 `MI_BUILD_*` 四个开关控制。
- mimalloc 会根据**构建目录名**自动推断构建类型（`debug/asan/tsan/...` 结尾 → Debug）与 secure 模式（`secure` 结尾 → `MI_SECURE=ON`）；非 release 系构建会把构建类型追加进库名，于是有了 `libmimalloc-debug.so`、`libmimalloc-secure.so`。
- 构建开关的本质是一条链：cmake `option` → `mi_defines` 里的 C 宏（`MI_SECURE=4`、`MI_DEBUG=2`、`MI_GUARDED=1`）→ 源码 `#if` 分支；release 构建中所有断言被预处理器削成空宏，零开销。
- `MI_OVERRIDE` 默认开启，使库自带 `malloc`/`free` 同名符号，并配合 `-fno-builtin-malloc` 防止编译器绕过覆盖。
- `src/static.c` 把 19 个实现文件叠进一个翻译单元，产出 `mimalloc.o`；把它放链接命令最前即可获得最可靠的静态 malloc 覆盖——因为目标文件的符号优先于库文件。
- `test/CMakeLists.txt` 是安装后的官方示例工程，演示四种链接/覆盖方式；`test/main.c` 是可手动编译运行的完整 `mi_` API 示例。

## 7. 下一步学习建议

你已经能把 mimalloc 编出来并手动链接使用，但对绝大多数真实程序来说「不改代码就用上 mimalloc」才是刚需。下一讲 **u2-l1 动态覆盖 malloc：LD_PRELOAD 与 alloc-override.c** 将承接本讲的 `MI_OVERRIDE` 开关与 `libmimalloc.so` 产物，讲透 `src/alloc-override.c` 里同名符号的导出机制与 `LD_PRELOAD` 注入原理。之后再进入 **u1-l3 目录结构与代码地图**，为单元三的核心数据结构阅读做准备。若想提前热身，可在本讲构建的 `out/release` 目录里运行 `mimalloc-test-stress-dynamic` 相关测试（根 CMakeLists L985-1007），亲眼看看官方如何验证动态覆盖。
