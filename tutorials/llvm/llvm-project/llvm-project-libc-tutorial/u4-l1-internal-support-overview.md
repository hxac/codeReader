# __support 总览与设计哲学

## 1. 本讲目标

在前面的讲义里，我们已经反复看到一个现象：一个入口点（entrypoint）的实现往往「很薄」。比如 `isalpha` 入口点的真正函数体只有两行——边界检查加一次委托。被委托出去的逻辑、用到的 `numeric_limits`、依赖的命名空间宏，全都来自同一个地方：`src/__support`。本讲要回答的核心问题是：

> 这个被所有入口点共同依赖的 `__support` 到底是什么？它里面有哪些东西？为什么 LLVM-libc 需要这样一个「私有标准库」？

学完本讲，你应当能够：

1. 用一句话说清 `__support` 在整个依赖图中的定位，并能解释它为什么**不对应任何公共头文件**。
2. 拿着一张「子模块地图」，在 `__support` 下快速找到浮点、系统调用、`printf` 核心、C++ 工具等各类能力的归属。
3. 看懂 `common.h` 与 `macros/config.h` 提供的基础设施宏（`LIBC_NAMESPACE_DECL`、`LLVM_LIBC_FUNCTION`、`LIBC_INLINE` 等）是如何被注入到每一个入口点的。
4. 解释入口点是如何通过 CMake 的 `DEPENDS` 引用 `__support` 的内部构建目标的。

## 2. 前置知识

本讲假设你已经学过 **u2-l2（实现规范与核心宏：命名空间与 LLVM_LIBC_FUNCTION）**，因此对以下概念不再展开：

- **入口点（entrypoint）**：每个对外公开函数/变量都是一个独立、有名的构建单元。
- **`LIBC_NAMESPACE_DECL`**：把所有内部符号关进带隐藏可见性的 `__llvm_libc` 命名空间。
- **`LLVM_LIBC_FUNCTION`**：用 asm 别名把内部 C++ 实现映射为公开 C 符号。

如果你还记得 **u1-l2** 里的一句话——「`src/__support` 是不对应任何公共头文件的私有工具库特例」——那么本讲就是把这句话彻底讲透。我们先建立直觉，再读源码。

一个有用的类比：如果把 LLVM-libc 比作一家「函数工厂」，每个入口点是一条产线上的成品，那么 `__support` 就是工厂内部的**中央机加工车间**——它不出货给客户（不产生公共头文件、不直接对应标准 C 函数），但每条产线都要从这里取零件、借工具。它就是 libc 写给自己用的「私有标准库」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| [docs/dev/source_tree_layout.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/source_tree_layout.md) | 官方源码目录说明，定义了 `src` 按「公共头文件名分目录」的组织约定，`__support` 是这个约定的特例。 |
| [src/__support/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt) | `__support` 的构建根：用 `add_header_library`/`add_object_library`（而非入口点规则）声明内部目标，并 `add_subdirectory` 串起各子模块。 |
| [src/__support/common.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h) | 「公共内部构造」头文件，定义 `LLVM_LIBC_FUNCTION`/`LLVM_LIBC_VARIABLE` 宏，并聚合各 `macros/` 头文件。 |
| [src/__support/macros/config.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h) | 定义 `LIBC_NAMESPACE_DECL`（带隐藏可见性的命名空间声明）等核心配置宏。 |
| [src/__support/macros/attributes.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h) | 便携属性宏，如 `LIBC_INLINE`、`LIBC_THREAD_LOCAL`。 |
| [src/__support/CPP/README.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md) | 说明 `CPP/` 子目录的设计规则（自包含、命名空间、可包含的头文件范围）。 |
| [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) | 入口点侧示例：`isalpha` 通过 `DEPENDS` 引用 `__support` 的内部目标。 |
| [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp) | 入口点侧示例：`#include` 多个 `src/__support/...` 头文件并使用其能力。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**`__support` 的定位**、**子模块地图**、**基础设施宏**。

### 4.1 __support 定位：所有入口点共享的「私有标准库」

#### 4.1.1 概念说明

回顾 **u1-l2** 与 **u2-l1**：`src` 目录有一条铁律——「每个公共头文件对应一个目录」，即 `string.h` → `src/string/`、`ctype.h` → `src/ctype/`。这条铁律让任何人都能由函数名反推出它的实现、内部头、测试、配置「四件套」。

但这条铁律有**一个特例**：`src/__support`。

`__support` 不对应任何标准 C 头文件，它里面也没有任何对外公开的函数。它的全部职责是——**为所有入口点沉淀可复用的内部能力**。换句话说，它是 LLVM-libc 写给自己用的「私有标准库」：字符判定、整数解析、浮点位运算、系统调用封装、`printf` 核心、内存分配器、C++ 工具子集……这些「跨多个函数族反复要用到的公共逻辑」都被收拢在这里，避免在每个入口点里重复造轮子。

#### 4.1.2 核心流程

要理解 `__support` 的定位，关键在于看清它在构建系统里**不是入口点**。入口点用 `add_entrypoint_object` 注册（产出可被聚合成 `libc.a`/`libm.a` 的目标），而 `__support` 用的是另外两种规则：

```
add_header_library(...)   # 纯头文件库：只传播 include 路径/编译选项，不编译出 .o
add_object_library(...)   # 对象库：有 .cpp，编译出 .o，但不会单独进公共归档
```

这两种规则的产物都是**内部构建目标**（点分全限定名，如 `libc.src.__support.ctype_utils`），它们只能被其它入口点或其它内部目标通过 `DEPENDS` 引用，自身永远不直接成为公开符号。这正是「私有」二字的构建系统含义。

入口点与 `__support` 的依赖关系可以画成：

```
        公共符号层            内部能力层（不产生公共符号）
   ┌─────────────────┐      ┌──────────────────────────┐
   │  src/ctype/     │      │  src/__support/          │
   │   isalpha  ─────┼──DEPENDS──► ctype_utils.h       │
   │   (entrypoint)  │      │                CPP/limits│
   └─────────────────┘      │                common.h  │
                            │                macros/…  │
                            └──────────────────────────┘
```

#### 4.1.3 源码精读

先看官方目录说明对 `src` 的描述，确立「按公共头文件分目录」这条主线：

> For every public header file provided by llvm-libc, there exists a corresponding directory in the `src` directory.（[docs/dev/source_tree_layout.md:82-95](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/source_tree_layout.md#L82-L95)）

这段确立了主约定；而 `__support` 正是这条约定的、也是唯一的系统性特例——它不属于任何头文件目录，而是所有头文件目录的「公共后盾」。

接着看 `__support` 自己的构建根。它的开头就先把两个最底层的子目录挂上来，然后用 `add_header_library` 声明 `libc_errno`（`errno` 抽象）这种纯头文件内部库：

[src/__support/CMakeLists.txt:1-11](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L1-L11) —— 先 `add_subdirectory(CPP)` 与 `add_subdirectory(macros)`，再用 `add_header_library` 声明 `libc_errno` 头文件库。注意这里用的是 `add_header_library` 而非入口点规则，产物是内部目标 `libc.src.__support.libc_errno`。

再拿一个最朴素的入口点 `isalpha` 来印证「入口点 → `__support`」这条依赖边。入口点的 CMake 注册里写得很直白：

[src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) —— `isalpha` 入口点的 `DEPENDS` 列出了两个 `__support` 内部目标：`libc.src.__support.CPP.limits` 和 `libc.src.__support.ctype_utils`。这就是入口点「取零件」的方式。

而在 C++ 源码层面，这个入口点几乎是「只做转接」：

[src/ctype/isalpha.cpp:9-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L24) —— 入口点 `#include` 了三个 `src/__support/...` 头文件（`CPP/limits.h`、`common.h`、`ctype_utils.h`），函数体只做边界检查，然后把真正的判定委托给 `internal::isalpha`（来自 `ctype_utils.h`）。真正的算法活在 `__support` 里。

> 小结：`__support` 不产生公共符号、不对应公共头文件，它用 `add_header_library`/`add_object_library` 声明内部目标，由入口点通过 `DEPENDS` 引用——这就是「私有标准库」的全部含义。

#### 4.1.4 代码实践

**实践目标**：亲手验证「入口点靠 `DEPENDS` 引用 `__support`」这条边，并体会入口点之「薄」。

**操作步骤**：

1. 打开 [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt)，数一数有多少个 `add_entrypoint_object` 在 `DEPENDS` 里写了 `libc.src.__support.` 开头的目标。
2. 打开 [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp)，把它的 `#include` 拆成两类：一类是「入口点自己的内部头」（`src/ctype/isalpha.h`），一类是「来自 `__support` 的头」。
3. 对照步骤 2 里列出的 `__support` 头，回到步骤 1 的 `DEPENDS`，看 C++ 源码里 include 的每一个 `__support` 头是否都能在 `DEPENDS` 里找到对应的目标。

**需要观察的现象**：

- C++ 里 include 了 `src/__support/CPP/limits.h`，CMake 里就一定有 `libc.src.__support.CPP.limits`；include 了 `src/__support/ctype_utils.h`，CMake 里就一定有 `libc.src.__support.ctype_utils`。两侧一一对应，不会「凭空 include」。
- `isalpha` 的函数体里**没有任何**字符分类的算法细节，全部委托给 `__support`。

**预期结果**：你会得到一张「`isalpha.cpp` 的 `__support` 依赖表」，每一行都是「C++ include ⇄ CMake DEPENDS」的成对关系。这正是入口点「薄」、`__support`「厚」的实证。

> 如果当前没有可用的构建环境，无法运行 `ninja`，这一步属于纯源码阅读型实践，无需编译即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__support` 里的目标用 `add_header_library`/`add_object_library`，而不是像 `isalpha` 那样用 `add_entrypoint_object`？

> **参考答案**：`add_entrypoint_object` 产出的是「可能进入公共归档（`libc.a`/`libm.a`/`libllvmlibc.a`）并被公开为 C 符号」的构建单元；而 `__support` 的设计目标恰恰是「不产生公共符号、不对应公共头文件」，只服务内部复用。用 `add_header_library`（纯头文件，只传播 include 路径与编译选项）和 `add_object_library`（有 `.cpp` 但只在被依赖时才编译进调用方）正是为了表达「内部目标」这一语义。

**练习 2**：如果一个入口点的 `.cpp` 里 `#include "src/__support/CPP/span.h"`，但它的 `CMakeLists.txt` 的 `DEPENDS` 里忘了写对应目标，会发生什么？

> **参考答案**：构建系统无法把 `span.h` 所在的目录加入该入口点的 include 搜索路径（`DEPENDS` 同时承担「构建顺序」与「头文件路径/编译选项的传播」两重职责，见 u2-l3），因此编译时会报 `span.h: No such file or directory` 之类的「找不到头文件」错误。这正说明 C++ include 与 CMake `DEPENDS` 必须保持一致。

---

### 4.2 子模块地图：__support 里都有什么

#### 4.2.1 概念说明

`__support` 是一个相当大的目录，里面既有**按主题分子目录**的能力包（`CPP`、`FPUtil`、`OSUtil`、`printf_core`、`threads`、`File`、`math` 等），也有**铺在根目录上的一组扁平工具头**（`ctype_utils.h`、`error_or.h`、`str_to_integer.h`、`block.h`、`freelist.h` 等）。

区分这两类有个简单经验法则：

- **子目录**通常对应一个「较大的能力域」，往往有自己的 `CMakeLists.txt`，内部还会再按 OS/架构细分（如 `OSUtil/linux/`、`FPUtil/x86_64/`）。
- **扁平头**通常是「单个具体算法或小工具」，一个文件一个内部目标，互相之间通过 `DEPENDS` 组合。

本节不逐个深入（那是后续 u4-l2、u4-l3、u6、u7、u8、u9 各讲的事），而是给你一张**总览地图**，让你在遇到任何函数时，能猜到「它的公共逻辑大概沉在 `__support` 的哪一块」。

#### 4.2.2 核心流程

`__support` 的构建根用一串 `add_subdirectory` 把各能力子目录挂进来。注意它们的出现顺序并非随意——有的子目录之间有依赖，构建系统需要先声明：

[src/__support/CMakeLists.txt:452-477](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L452-L477) —— 这里依次 `add_subdirectory` 了 `FPUtil`、`OSUtil`、`StringUtil`、`GPU`、`RPC`、`net`、`threads`、`File`、`printf_core`、`HashTable`、`fixed_point`、`time`、`wchar`、`wctype`、`math`、`builtins`（以及条件性的 `mathvec`、`regex`）。

其中有一处**带注释的顺序约束**很能说明问题：

[src/__support/CMakeLists.txt:459-463](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L459-L463) —— 注释明确写道「Thread support is used by other File. So, we add the threads before File.」，所以 `threads` 必须排在 `File` 之前。这种「用注释钉死顺序」恰恰说明子模块之间是真实互相依赖的，而非随意罗列。

#### 4.2.3 源码精读

把 `__support` 的内容整理成下面这张地图（子目录部分）。注意每行的「典型内部目标名」就是你在入口点 `DEPENDS` 里会写到的那个点分名：

| 子目录 | 职责（一句话） | 典型内部目标名 / 关键头 |
|--------|----------------|------------------------|
| `CPP/` | 自包含的 C++ 标准库子集，让 libc 用现代 C++ 却不依赖 libstdc++/libc++ | `libc.src.__support.CPP.span`、`...CPP.limits`、`...CPP.bit` |
| `FPUtil/` | 浮点位运算（`FPBits`）与按架构特化的硬件 intrinsic | `...FPUtil.fp_bits`、`...FPUtil.sqrt`（见 u6-l2） |
| `OSUtil/` | 跨 OS 的系统调用抽象层，内部按 OS/架构分目录 | `...OSUtil.osutil`、`...OSUtil.linux.syscall`（见 u8-l1） |
| `printf_core/` | `printf`/`scanf` 家族共享的「解析器-写入器-转换器」核心 | `...printf_core.parser`、`...printf_core.writer`（见 u7-l2） |
| `File/` | 建立在 `OSUtil` 之上的 `FILE` 模型与带缓冲 I/O | `...File.file`（见 u8-l3） |
| `threads/` | 同步原语与线程对象（`raw_mutex`、`futex_utils`、`thread`） | `...threads.raw_mutex`、`...threads.thread`（见 u9-l2） |
| `math/` | 可被数学入口点复用的内联实现 | `...math.round`（见 u6-l1） |
| `GPU/`、`RPC/`、`net/` | GPU 目标运行时支持、远程过程调用、网络相关 | `...GPU.*`、`...RPC.*` |
| `StringUtil/`、`HashTable/`、`time/`、`wchar/`、`wctype/`、`fixed_point/`、`builtins/`、`mathvec/`、`regex/` | 字符串工具、哈希表、时间、宽字符、定点数、编译器内置、数学向量、正则等专项能力 | 各自子目录目标 |

除了子目录，根目录上还铺着一组**扁平工具头**，它们大多是「单个算法/小工具」：

| 扁平头 | 职责（一句话） | 在哪讲深入 |
|--------|----------------|-----------|
| `ctype_utils.h` | 与编码无关的字符分类判定（`isalpha`/`isdigit`/`tolower`…） | u5-l1 |
| `error_or.h` | 「值或错误」的内部结果类型 | u4-l3 |
| `libc_errno.h` | 公开 `errno` 写入的跨平台抽象 | u4-l3 |
| `str_to_integer.h` / `integer_to_string.h` | 字符串↔整数互转的公共算法 | u7-l1 |
| `str_to_float.h` / `float_to_string.h` | 字符串↔浮点互转 | u7 系列 |
| `block.h` / `freelist.h` / `freetrie.h` / `freelist_heap.h` | 内存分配器的块/空闲链/堆 | u9-l1 |
| `common.h` / `macros/*` | 基础设施宏（本讲 4.3） | 本讲 |
| `big_int.h` / `uint128.h` / `hash.h` / `memory_size.h` / `intrusive_list.h` / `weak_avl.h` | 大整数、128 位运算、哈希、容量单位、侵入式链表、弱 AVL 树等数据结构 | 散见各讲 |

> 这张表不需要背下来。它的价值是：当你在某个入口点看到 `DEPENDS` 里出现 `libc.src.__support.X` 时，能立刻回到这张表，定位到「这个能力属于哪个最小模块、在哪一讲深入」。

最后，以 `OSUtil` 为例看一下子目录内部「按 OS 再细分」的模式——这是 `__support` 跨平台能力的通用手法：

[src/__support/OSUtil/CMakeLists.txt:1-15](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/OSUtil/CMakeLists.txt#L1-L15) —— `OSUtil/CMakeLists.txt` 先用 `LIBC_TARGET_OS` 选出当前 OS 子目录（如 `linux/`），再把该 OS 的实现目标取个别名 `osutil`，供上层统一引用。`FPUtil` 按 `x86_64/aarch64/riscv/arm/generic` 分目录是同一个套路（详见 u6-l2、u8-l1）。

#### 4.2.4 代码实践

**实践目标**：亲手为四个核心子目录写出职责说明，并找到「入口点通过 `DEPENDS` 引用它们」的真实例子。

**操作步骤**：

1. 用目录浏览工具列出 `src/__support/` 下的子目录与扁平头（就是本讲源码探查时做的事）。
2. 针对下面四个子目录，各读 1～2 个代表头文件的顶部注释，用一句话写下它们的职责：
   - `CPP/`：建议读 [src/__support/CPP/README.md:1-3](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md#L1-L3)
   - `FPUtil/`：建议读 `src/__support/FPUtil/FPBits.h` 顶部注释
   - `OSUtil/`：建议读 `src/__support/OSUtil/syscall.h` 顶部注释
   - `printf_core/`：建议读 `src/__support/printf_core/parser.h` 顶部注释
3. 任选一个数学入口点（如 `src/math/generic/round.cpp`）或 stdlib 入口点（如 `src/stdlib/` 下任意一个），在其 `CMakeLists.txt` 的 `DEPENDS` 里找出形如 `libc.src.__support.FPUtil.*` 或 `libc.src.__support.printf_core.*` 的条目，证实「入口点经 `DEPENDS` 引用 `__support`」。

**需要观察的现象**：

- 四个子目录的顶部注释各自点明了能力范围，你写下的「一句话职责」应与官方注释大意一致，而不是泛泛而谈。
- 数学/格式化类入口点的 `DEPENDS` 里几乎一定会出现 `FPUtil` 或 `printf_core` 的内部目标名。

**预期结果**：得到一张「子目录 → 一句话职责 → 引用它的入口点」三列表，至少四行。

> 待本地验证：步骤 3 的具体入口点与目标名取决于你查阅的函数，若不便检索，可只完成步骤 1～2 的源码阅读部分。

#### 4.2.5 小练习与答案

**练习 1**：`OSUtil` 与 `FPUtil` 都在内部按「平台/架构」再分子目录。请说出它们各自按什么维度细分，以及为什么必须细分。

> **参考答案**：`OSUtil` 按 **OS**（`linux/darwin/freebsd/windows/fuchsia/baremetal/gpu/uefi`）细分，因为系统调用的编号、约定、入口各 OS 不同；`FPUtil` 按 **CPU 架构**（`x86_64/aarch64/riscv/arm/generic`）细分，因为浮点的硬件 intrinsic（如 `sqrt`、FMA、位域插入）是架构强相关的。两者都必须细分，是因为它们封装的恰恰是「最贴近硬件/内核」的那一层能力，而那一层天然随平台变化；其上则可以共用一份平台无关算法。

**练习 2**：`__support/CMakeLists.txt` 里为什么要在 `File` 之前先 `add_subdirectory(threads)`？如果反过来会怎样？

> **参考答案**：因为 `File` 模块（带缓冲 I/O 的 `FILE` 实现）依赖 `threads` 提供的同步原语（缓冲区的并发访问需要锁）。CMake 的 `add_subdirectory` 顺序决定了目标「先声明、后可被引用」；若把 `threads` 放到 `File` 之后，`File` 在配置阶段引用 `threads` 的目标时会因目标尚未定义而失败。源码里那句注释「Thread support is used by other File」就是为这个顺序约束做的说明。

---

### 4.3 基础设施宏：common.h 与 macros/config.h

#### 4.3.1 概念说明

如果说前面的 `CPP`、`FPUtil` 是「具体能力」，那么本节讲的是**贯穿所有 `__support` 代码与所有入口点的「地基」**：一组被无处不在地 `#include` 的宏头文件。

最关键的一个是 `common.h`——它的文件头注释自称「Common internal constructs（公共内部构造）」。它做了两件事：

1. **聚合**一组最基础的宏头（`macros/attributes.h`、`macros/config.h`、架构/编译器属性头等），让入口点只 include 一个 `common.h` 就拿到全套基础宏。
2. **定义** `LLVM_LIBC_FUNCTION` / `LLVM_LIBC_VARIABLE` 这两个「把内部实现变成公开 C 符号」的核心宏（这是 u2-l2 的主角）。

另一个是 `macros/config.h`，它定义了 `LIBC_NAMESPACE_DECL`——所有内部符号都要被它包裹，以获得隐藏可见性与符号隔离。

理解这一节的意义在于：**`__support` 不只是「放算法的地方」，它还掌管着「写一个入口点要用到的全部底层约定」**。

#### 4.3.2 核心流程

这套基础设施的工作方式是「CMake 注入 + 头文件约束」：

```
   CMake 在编译每个目标时，通过 -DLIBC_NAMESPACE=__llvm_libc_<ver>
                       │
                       ▼
   common.h 顶部：#ifndef LIBC_NAMESPACE  →  #error
   （强制：没有这个注入就拒绝编译，确保命名空间一定由构建系统统一定义）
                       │
                       ▼
   macros/config.h：定义 LIBC_NAMESPACE_DECL
        =  [[gnu::visibility("hidden")]] LIBC_NAMESPACE
                       │
                       ▼
   入口点/.cpp：namespace LIBC_NAMESPACE_DECL { ... }
   LLVM_LIBC_FUNCTION(int, isalpha, (int c)) { ... }
```

要点：`LIBC_NAMESPACE` 不是源码里写死的，而是**由 CMake 注入的编译期定义**；`common.h` 用 `#error` 强制它必须被注入；`macros/config.h` 把它包装成带隐藏可见性的 `LIBC_NAMESPACE_DECL`。

#### 4.3.3 源码精读

先看 `common.h` 顶部的「强制注入」检查——这是整个命名空间体系的守门人：

[src/__support/common.h:9-20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L9-L20) —— 第 12～14 行 `#ifndef LIBC_NAMESPACE` / `#error "LIBC_NAMESPACE macro is not defined."`：如果 CMake 没有注入 `LIBC_NAMESPACE`，编译立刻失败。随后第 16～20 行 include 了 `macros/attributes.h`、`macros/config.h` 以及架构、编译器属性头——这就是「聚合全套基础宏」的含义。

接着看 `LLVM_LIBC_FUNCTION` 宏的入口（细节在 u2-l2 已详述，这里只点出它住在 `common.h`）：

[src/__support/common.h:82-84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L82-L84) —— `LLVM_LIBC_FUNCTION(...)` 用 `GET_FIFTH` 数参数个数，分派到 `IMPL_3`（三参数，别名默认为函数名字符串化）或 `IMPL_4`（四参数，显式给别名）。完整的 asm 别名机制见 u2-l2。

再看 `LIBC_NAMESPACE_DECL` 的定义，它就住在 `macros/config.h`：

[src/__support/macros/config.h:57-71](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h#L57-L71) —— 在 Clang 下，`LIBC_NAMESPACE_DECL` 被定义为 `[[gnu::visibility("hidden")]] LIBC_NAMESPACE`；注释解释：给命名空间内所有声明加隐藏可见性，可避免跨翻译单元引用时产生动态重定位（dynamic relocation），这对正确性有时是必要的。GCC 分支因有告警问题暂不加该属性（TODO #98548）。

最后看一组「无处不在的小宏」住在 `macros/attributes.h`：

[src/__support/macros/attributes.h:27-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h#L27-L30) —— `LIBC_INLINE` 就是 `inline`，`LIBC_INLINE_ASM` 是 `__asm__ __volatile__`，`LIBC_UNUSED` 是 `__attribute__((unused))`。在 `ctype_utils.h` 里你会看到大量 `LIBC_INLINE constexpr`，用的就是这个宏，目的是「便携」——把这些编译器特性抽象成统一名字，方便在不同编译器（含 MSVC）下替换。

这套宏之所以重要，是因为 CMake 在声明 `common` 这个内部目标时，把它们打包成「一个 include 就能拿到的地基」：

[src/__support/CMakeLists.txt:95-107](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L95-L107) —— `common` 头文件库把 `common.h`、`endian_internal.h` 以及一组 `macros/*.h`（属性、配置、架构、编译器）一起列为 `HDRS`，于是任何 `DEPENDS` 里写了 `libc.src.__support.common` 的目标，都能一次性 include 到全套基础宏。

#### 4.3.4 代码实践

**实践目标**：追踪一个基础设施宏从「定义」到「被入口点使用」的完整路径。

**操作步骤**：

1. 在 [src/__support/macros/attributes.h:27](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h#L27) 找到 `LIBC_INLINE` 的定义。
2. 在 [src/__support/ctype_utils.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h) 中搜索 `LIBC_INLINE`，看它被用在多少个函数声明上（如 [L244](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244) 的 `isalpha`）。
3. 回到 [src/__support/CMakeLists.txt:155-159](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L155-L159)，注意 `ctype_utils` 这个内部目标**没有显式 `DEPENDS`**——这意味着 `LIBC_INLINE` 所在的 `macros/attributes.h` 是被 `ctype_utils.h` 自己直接 `#include` 的（见 `ctype_utils.h` 第 12～13 行）。
4. 思考：为什么 `ctype_utils.h` 要自己 include 这些宏头，而不是依赖 CMake 的 `DEPENDS` 自动传播？

**需要观察的现象**：

- `LIBC_INLINE` 这种「最基础」的宏，被 `__support` 内部头文件**直接 include**，不经过 `DEPENDS` 也能用。
- 而 `common.h` 这种「聚合包」则提供给了入口点经 `DEPENDS` 一次性取用。

**预期结果**：你会理解 `__support` 的宏头有两种消费方式——「内部头直接 include」与「入口点经 `DEPENDS` 取聚合包」，两者并存。

> 待本地验证：这一步纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `common.h` 第 12～14 行的 `#error` 检查删掉，会发生什么？为什么它必须存在？

> **参考答案**：`LIBC_NAMESPACE` 是由 CMake 在编译每个目标时通过 `-D` 注入的编译期宏（默认 `__llvm_libc`，可带版本后缀）。如果没有这个 `#error` 守门，一旦某个目标因为配置错误没拿到这个注入，`LIBC_NAMESPACE_DECL` 就会展开成未定义符号，错误会在很远的下游（链接期或宏展开期）以难以理解的形式暴露。`#error` 把错误**前移到编译最早期**，并给出明确提示，是典型的「快速失败」防御式设计。

**练习 2**：`LIBC_NAMESPACE_DECL` 在 Clang 下带 `[[gnu::visibility("hidden")]]`，而 GCC 下没有。结合注释，说明隐藏可见性对内部符号有什么好处。

> **参考答案**：隐藏可见性让命名空间内的内部符号不进入动态符号表，跨翻译单元引用它们时**不必经过 GOT（全局偏移表）做间接跳转**，从而避免动态重定位。注释指出这「有时是正确性所必需的」——对于 libc 这种极度追求调用效率、且内部符号绝不应被外部覆盖的实现，减少间接跳转既是性能优化也是安全性收益。GCC 分支暂未加，是因为它会触发告警（TODO #98548 待修），属于已知的临时妥协。

**练习 3**：`common.h` 自称「Common internal constructs」，它和 `macros/config.h` 是什么关系？

> **参考答案**：`common.h` 是**聚合层**，它 `#include` 了 `macros/config.h`（以及 `macros/attributes.h`、架构/编译器属性头等）；`macros/config.h` 是**定义层**，真正定义 `LIBC_NAMESPACE_DECL` 等核心宏。CMake 又把 `common.h` 连同这些 `macros/*.h` 一起打包成 `common` 内部目标（`__support/CMakeLists.txt` 第 95～107 行），所以下游只需 `DEPENDS libc.src.__support.common` 即可一次性拿到全套基础宏。三者是「定义 → 聚合 → 打包分发」的关系。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「`__support` 侦察任务」。

**任务背景**：假设你要给一个新入口点 `isvowel`（判断是否为元音字母）写实现，需要从 `__support` 借能力。在动手前，先做一次完整的依赖侦察。

**要求**：

1. **定位（对应 4.1）**：说明 `isvowel` 的实现应该放在哪个目录（提示：它属于字符分类，应跟随 `ctype.h`），并解释为什么它的公共逻辑不该放在 `src/ctype/isvowel.cpp` 里，而应下沉到 `src/__support/`。

2. **取能力（对应 4.2）**：浏览 `src/__support/`，判断 `isvowel` 需要复用哪些现有能力。至少应考虑到：
   - 是否能直接复用 [src/__support/ctype_utils.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h) 里已有的 `isalpha`/`islower`/`isupper`？
   - 边界检查需要的 `cpp::numeric_limits` 来自哪个内部目标（`libc.src.__support.CPP.limits`）？
   - 仿照 [src/ctype/isalpha.cpp:16-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L16-L24) 的写法，写出 `isvowel` 入口点的骨架（标注「示例代码」）。

3. **接线（对应 4.3）**：为 `isvowel` 写出 `add_entrypoint_object` 的 `DEPENDS`（参考 [src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22)），并说明：
   - 为什么必须 `DEPENDS` 这两个 `__support` 目标；
   - `LLVM_LIBC_FUNCTION` 和 `LIBC_NAMESPACE_DECL` 分别由哪两个 `__support` 头提供（`common.h` 与 `macros/config.h`）。

**交付物**：一份「`isvowel` 的 `__support` 依赖说明书」，包含：实现位置决策、复用的现有能力清单、入口点骨架代码（标注示例）、CMake `DEPENDS` 清单及逐条理由。

> 这是一个纯源码阅读 + 设计型实践，无需编译；若你有构建环境，可进一步把骨架真的加进去并用 `ninja libc.test.src.ctype.isvowel_test.__unit__` 跑一遍（测试编写见 u10-l1）。

## 6. 本讲小结

- **`__support` 是所有入口点共享的「私有标准库」**：它不对应任何公共头文件、不产生公开 C 符号，用 `add_header_library`/`add_object_library` 声明内部目标，由入口点经 `DEPENDS` 引用。
- **入口点之所以「薄」**，是因为真正的算法/位运算/系统调用/格式化核心都下沉到了 `__support`；`isalpha` 把判定委托给 `ctype_utils.h` 的 `internal::isalpha` 就是典型例证。
- **`__support` 分两类内容**：按主题分的能力子目录（`CPP`、`FPUtil`、`OSUtil`、`printf_core`、`threads`、`File`、`math`…，常再按 OS/架构细分）与铺在根目录的扁平工具头（`ctype_utils.h`、`error_or.h`、`str_to_integer.h`、`block.h`…）。
- **`common.h` 是基础宏的聚合层**：它 `#include` 了 `macros/attributes.h`、`macros/config.h` 等，并用 `#error` 强制 `LIBC_NAMESPACE` 必须由 CMake 注入。
- **`macros/config.h` 定义 `LIBC_NAMESPACE_DECL`**（带隐藏可见性的命名空间），`common.h` 定义 `LLVM_LIBC_FUNCTION`/`LLVM_LIBC_VARIABLE`——它们共同构成「写一个入口点」的底层约定。
- **C++ 源码 include 与 CMake `DEPENDS` 必须一一对应**：include 了哪个 `__support` 头，`DEPENDS` 里就要有对应的内部目标，否则编译期找不到头文件。

## 7. 下一步学习建议

本讲建立了 `__support` 的全景认知，接下来应按「由近及远」深入它的具体子模块：

1. **u4-l2（CPP 子集工具库）**：第一个深入对象。读 `src/__support/CPP/` 下的 `limits.h`、`span.h`，理解 libc 为什么必须自带一套 C++ 工具，以及它的命名空间与包含规则。
2. **u4-l3（错误处理：error_or 与 errno）**：接着读 `error_or.h` 与 `libc_errno.h`，掌握内部错误传播与公开 `errno` 抽象——这是几乎所有「会失败的」入口点（`open`、`malloc`、`strtol`…）的共同基础。
3. **之后**可按兴趣跳转：浮点走 u6（`FPUtil`）、数值转换与格式化走 u7（`str_to_integer`、`printf_core`）、系统调用与启动走 u8（`OSUtil`、startup）、内存与并发走 u9（`block`/`freelist`、`threads`）。每一条线都是本讲地图里某一格的展开。

建议在进入上述任一讲之前，先回头确认：你能在 `src/__support/` 下**迅速定位**到对应子目录或扁平头，并能说出「它对应地图表的哪一行」——能做到这一点，本讲的目标就达成了。
