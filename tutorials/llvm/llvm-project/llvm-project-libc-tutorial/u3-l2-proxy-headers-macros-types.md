# 代理头文件、公共宏与类型

## 1. 本讲目标

在上一篇（u3-l1）中我们看到：对外发布的公共头文件（`stddef.h`、`stdio.h`、`errno.h`……）是**由 YAML 经 hdrgen 生成**的，描述的是「用户安装后看到的接口」。但 LLVM-libc **自己的实现代码**（`src/` 下）在编译期也需要引用 `size_t`、`FILE`、`errno` 值、`NULL` 这些公共类型与宏——而且同一个实现文件，在 Full 构建里要用 libc **内部**的定义，在 Overlay 构建里要回退到**系统**头里的定义。

本讲要解决的问题就是：**实现代码如何用同一条 `#include` 指令，在两种构建模式下拿到「语义相同、来源不同」的类型与宏？** 学完本讲你应当能：

- 理解 `hdr/` 代理头（proxy header）作为「Full / Overlay 切换适配层」的角色与工作原理。
- 认识 `include/llvm-libc-macros/` 与 `include/llvm-libc-types/` 这两类**自包含公共头**，知道它们为何是「已具备安装形态」的成品。
- 看懂 `include/__llvm-libc-common.h` 作为所有公共头**通用前缀**的作用，以及它提供的那些跨 C/C++ 移植宏。
- 能够跟踪一个类型（如 `size_t`）在两种模式下的两条来源路径，并画出切换示意图。

## 2. 前置知识

阅读本讲前，你需要先具备以下认知（由前置讲义建立，本讲直接承接、不重复）：

- **Full 模式 vs Overlay 模式（u1-l4）**：Full 模式（`LLVM_LIBC_FULL_BUILD=ON`）把 LLVM-libc 当作**完整的 libc 替换品**，产出 `libc.a`/`libm.a` 并自生成头文件；Overlay 模式（开关 `OFF`）只产出 `libllvmlibc.a`，借链接顺序**覆盖**系统 libc 中的少数符号，其余回退到系统 libc。这两种模式下，「`FILE` 是什么结构」「`errno` 的值是多少」答案不同——这正是代理头要处理的矛盾。
- **头文件生成管线（u3-l1）**：公共头由 `include/*.yaml` 经 hdrgen 生成，`.h.def` 是可选手写模板，`%%public_api()` 是占位符。本讲讨论的 `__llvm-libc-common.h`、`llvm-libc-macros/`、`llvm-libc-types/` 正是这条管线在生成阶段会去「取料」的公共构件。

还需要理解一个 C 编译常识：**实现代码不能在 `.cpp` 里到处写 `#ifdef LIBC_FULL_BUILD` 来区分类型来源**——那样既冗长又易错。软件设计的标准做法是引入一层**间接（indirection）**，把「去哪里取定义」的决策集中到一处。代理头就是这层间接。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| [docs/dev/source_tree_layout.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/source_tree_layout.md) | 官方对 `hdr/` 与 `include/` 目录职责的权威说明。 |
| [hdr/](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr) | **代理头目录**，被 `src/` 实现代码 include；按 `LIBC_FULL_BUILD` 切换内部/系统来源。 |
| [hdr/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/CMakeLists.txt) | 定义 `add_proxy_header_library` 规则，用 `FULL_BUILD_DEPENDS` 表达模式相关的依赖。 |
| [include/llvm-libc-types/](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types) | **自包含公共类型**成品，一类一文件（`size_t.h`、`FILE.h`…），Full 模式下被代理头采用。 |
| [include/llvm-libc-macros/](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-macros) | **自包含公共宏**成品，一类一文件（`null-macro.h`、`errno` 系列…）。 |
| [include/__llvm-libc-common.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h) | 所有公共头的**通用前缀**，提供跨 C/C++ 的移植宏。 |
| [utils/hdrgen/hdrgen/header.py](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py) | hdrgen 的核心类，决定生成头如何插入公共前缀与各类 include。 |

## 4. 核心概念与源码讲解

### 4.1 代理头 hdr/

#### 4.1.1 概念说明

代理头（proxy header）是 LLVM-libc 给实现代码（`src/`）准备的一层**适配器**。它解决一个很现实的问题：

- `src/string/memcpy.cpp` 的签名里有 `size_t`；
- 在 **Full** 构建里，`size_t` 应当来自 LLVM-libc 自己的 [include/llvm-libc-types/size_t.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types/size_t.h)（保证与对外发布的公共头一致）；
- 在 **Overlay** 构建里，`size_t` 必须来自**系统**头（因为 Overlay 下的最终二进制要和系统 libc 共存，类型定义必须与系统 ABI 对齐，否则 u1-l4 讲到的 `FILE` 这类依赖私有布局的场景就会崩）。

实现代码不愿意、也不应该自己去写这种分支。于是约定：**实现代码一律 `#include "hdr/types/size_t.h"`，由这个代理头替你做模式判断**。代理 = 「我替你选来源，你只管用名字」。

官方文档对 `hdr/` 的定位非常明确：

> This directory contains proxy headers which are included from the files in the src directory. These proxy headers either include our internal type or macro definitions, or the system's type or macro definitions, depending on if we are in fullbuild or overlay mode.

#### 4.1.2 核心流程

代理头内部都遵循同一个分派骨架，全部由一个宏 `LIBC_FULL_BUILD` 驱动（该宏在 Full 构建时由 CMake 注入到编译命令行）：

```text
src/*.cpp  --#include "hdr/types/X.h"-->  代理头 X.h
                                              │
                    ┌─────────────────────────┴─────────────────────────┐
              已定义 LIBC_FULL_BUILD                              未定义（Overlay）
                    │                                           │
        #include "include/llvm-libc-types/X.h"        回退到系统头（<stddef.h>、<stdio.h>…）
        （libc 内部自包含定义）                       （与系统 libc ABI 对齐）
```

用一条规则概括其分派语义（其中 \(S(c)\) 表示在当前构建模式下符号 \(c\) 的取值来源）：

\[
S(c) =
\begin{cases}
\text{llvm-libc-types / llvm-libc-macros（内部自包含）} & \text{若 } \texttt{LIBC\_FULL\_BUILD} \text{ 已定义} \\
\text{系统头文件} & \text{否则（Overlay）}
\end{cases}
\]

代理头按被代理对象分成三类，目录结构即分类：

- `hdr/types/X.h`：代理**类型**（`size_t`、`FILE`、`pid_t`…）。
- `hdr/*_macros.h`：代理某公共头里的**宏集合**（`errno_macros.h`、`stdio_macros.h`…）。
- `hdr/func/X.h`：代理某个**函数声明**（`malloc.h`、`free.h`…），见 4.4。
- `hdr/*_overlay.h`：Overlay 专用的系统头包含器（`stdio_overlay.h`、`stdlib_overlay.h`…），只在该模式下生效，Full 模式下 include 它会直接 `#error`。

#### 4.1.3 源码精读

最干净的例子是 `size_t` 代理。整个文件只有 24 行，核心就是中间那段二选一：

[size_t.h:L11-L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/size_t.h#L11-L21) — 用 `#ifdef LIBC_FULL_BUILD` 把 `size_t` 的来源一分为二：Full 走内部 `include/llvm-libc-types/size_t.h`，Overlay 用 `__need_size_t` 配合编译器自带的 `<stddef.h>` 只取出 `size_t`（避免引入整个系统头）。

注意两条路径都用 `#include`，最终 `size_t` 这个**名字对调用者完全透明**——这正是代理的价值。

`FILE` 代理则展示了「Overlay 需要额外依赖」的情形。[FILE.h:L11-L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/FILE.h#L11-L21)：Full 模式 include 内部 `FILE.h`（前向声明 `typedef struct FILE FILE;`，把布局完全藏在实现里）；Overlay 模式转而 include `hdr/stdio_overlay.h`，由后者去拉系统 `<stdio.h>`，从而拿到系统 libc 的真实 `FILE` 布局。这正好呼应 u1-l4 的核心论断——`fopen` 依赖 `FILE` 的私有布局，所以 `FILE` 在两模式下必须不同，代理头就是这道切换阀。

#### 4.1.4 代码实践

**实践目标**：亲手确认「同一条 include，两种来源」。

**操作步骤**：

1. 打开 [hdr/types/size_t.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/size_t.h)，找到 `#ifdef LIBC_FULL_BUILD`。
2. 用 Grep 在 `src/` 下搜索 `#include "hdr/types/size_t.h"`，任选一处实现（如 `src/__support/block.h`）确认它确实只写了这一行、没有任何模式分支。
3. 打开 Full 分支引用的 [include/llvm-libc-types/size_t.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types/size_t.h)，看清内部定义是 `typedef __SIZE_TYPE__ size_t;`。
4. 对照 Overlay 分支：它不 include 任何 libc 文件，而是 `#define __need_size_t` + `#include <stddef.h>`。

**需要观察的现象**：实现文件里只有一处 include；切换来源的全部逻辑都集中在代理头里。

**预期结果**：你能在脑子里把「实现代码 → 代理头 → 双源」这条链补全。

> 待本地验证：若你已按 u1-l3 完成 runtimes 构建，可在两种模式下分别 `clang -E` 预处理某个 include 了 `hdr/types/size_t.h` 的文件，对比 `size_t` 展开后的来源注释。

#### 4.1.5 小练习与答案

**练习 1**：为什么实现代码不直接 `#include <stddef.h>` 取 `size_t`，而要走代理？
**答案**：因为 Full 模式下 LLVM-libc 不依赖系统头（用 `-nostdinc` 屏蔽），`<stddef.h>` 未必可用且语义不可控；代理头把「来源选择」集中化，保证 Full 用内部定义、Overlay 与系统 ABI 对齐。

**练习 2**：在 Full 模式下误 include 了 `hdr/stdio_overlay.h` 会发生什么？
**答案**：会触发 [stdio_overlay.h:L12-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/stdio_overlay.h#L12-L13) 的 `#error "This header should only be included in overlay mode"`，编译立即失败——这是一种防误用的硬保护。

### 4.2 公共宏 llvm-libc-macros/

#### 4.2.1 概念说明

`include/llvm-libc-macros/` 存放**自包含的公共宏**。这里每个 `.h` 文件对应一类宏（`null-macro.h` 定义 `NULL`、`offsetof-macro.h` 定义 `offsetof`、`float-macros.h` 定义 `FLT_*`、`error-number-macros.h` 定义 `E*` 错误码……），且**已经处于「安装即用」的最终形态**——也就是说，它们就是用户安装 LLVM-libc 后会在公共头里看到的那些宏定义。

「自包含」是关键修饰语：这类头不依赖生成、不依赖 `.h.def` 模板，本身即可被直接 include。它们在两类场合被使用：

1. **作为公共头成品的拼装件**：u3-l1 的 hdrgen 管线生成公共头时，会把 YAML 里登记的宏映射为对 `llvm-libc-macros/<name>.h` 的 include（见 4.2.3）。
2. **作为代理头的 Full 分支来源**：当代理头在 Full 模式下需要某类宏时，就 include 对应的 `llvm-libc-macros/*.h`。

#### 4.2.2 核心流程

```text
        ┌─────────────────────────── 公共宏的两类消费者 ───────────────────────────┐
        │                                                                          │
llvm-libc-macros/<name>.h  ───(a) hdrgen 生成公共头时按 YAML 登记──▶  安装后的 <stdio.h> 等
   （自包含成品）           ───(b) hdr/*_macros.h 代理头在 Full 下──▶  src/ 实现代码
        │
        └─ Overlay 下：代理头不 include 它，而是回退到系统头里的同名宏
```

#### 4.2.3 源码精读

最短的自包含宏是 `NULL`。[null-macro.h:L12-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-macros/null-macro.h#L12-L13) 用 `__need_NULL` 配合编译器自带的 `<stddef.h>` 只取出 `NULL` 定义——注意它复用了和代理头 Overlay 分支一样的「`__need_*`」技巧，因为 `NULL` 本身是编译器内置语义，没必要重造。

`errno` 宏代理则是「宏集合」的典型。[errno_macros.h:L11-L28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/errno_macros.h#L11-L28) 同样以 `#ifdef LIBC_FULL_BUILD` 切分：Full 模式下再按 OS 细分——Linux 走内核头 `<linux/errno.h>` 加自包含的 `error-number-macros.h`，Apple 走 `<sys/errno.h>`，其余走 `generic-error-number-macros.h`；Overlay 模式下则一把 include 系统 `<errno.h>`。

hdrgen 之所以能自动把宏拼进公共头，是因为生成器知道「宏 → `llvm-libc-macros/<header>.h`」的映射规则，见 [header.py:L188-L208](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L188-L208)：`includes()` 方法把每个宏的 `.header` 拼成 `llvm-libc-macros/...` 路径并参与排序输出。

#### 4.2.4 代码实践

**实践目标**：对比同一个宏集合在两种模式下的来源。

**操作步骤**：

1. 打开 [hdr/errno_macros.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/errno_macros.h)。
2. 在 Full 分支里追到自包含宏 [include/llvm-libc-macros/error-number-macros.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-macros/error-number-macros.h)，确认它直接 `#define` 了 `EPERM`、`ENOENT` 等值。
3. 记下 Overlay 分支：它对这些值「什么都没做」，只 include 了系统 `<errno.h>`。

**需要观察的现象**：Full 模式由 LLVM-libc 自己给出错误码数值（保证无系统头可用），Overlay 模式完全信任系统头。

**预期结果**：你能说清「为何 baremetal/GPU 这类无系统 libc 的目标必须走 Full」——它们没有系统 `<errno.h>` 可回退。

#### 4.2.5 小练习与答案

**练习 1**：`llvm-libc-macros/` 下的头与 `hdr/` 下的代理头有何本质区别？
**答案**：前者是**自包含的宏定义成品**（直接给值，与模式无关）；后者是**模式分派器**（在 Full 时去 include 前者或内核头，在 Overlay 时回退系统头）。代理头会「使用」自包含宏，反过来不然。

**练习 2**：为什么 `null-macro.h` 用 `__need_NULL` + `<stddef.h>` 而不是直接写 `#define NULL ((void*)0)`？
**答案**：`NULL` 在 C 与 C++ 里的「正确」定义不同（C 允许 `(void*)0`，C++ 要求整数常量 `0`），交给编译器自带的 `<stddef.h>` 用 `__need_NULL` 取值能自动适配语言与目标，避免手写错。

### 4.3 公共类型 llvm-libc-types/

#### 4.3.1 概念说明

`include/llvm-libc-types/` 存放**自包含的公共类型**，组织原则是「一类一文件」：`size_t.h`、`FILE.h`、`pid_t.h`、`struct_stat.h`……每个文件只定义一个类型，内容极简。它们与 `llvm-libc-macros/` 一样是**已具备安装形态的成品**，在 Full 模式下既被 hdrgen 拼进公共头，也被 `hdr/types/` 代理头采用。

为何要「一类一文件」这种细粒度？因为 hdrgen 生成公共头时希望**只引入真正用到的类型**，避免一个巨型头牵连一堆无关定义。生成器对每个未带 `guard` 的类型都会输出一条 `#include "llvm-libc-types/<name>.h"`，见 [header.py:L188-L208](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L188-L208)。

#### 4.3.2 核心流程

```text
公共类型的三条出路（皆在 Full 模式下生效）
   include/llvm-libc-types/X.h
        │
        ├─(1) hdrgen 生成 <某公共头> 时，按 YAML 中用到的类型自动 include
        ├─(2) hdr/types/X.h 代理头在 Full 分支 include 它，供 src/ 实现使用
        └─(3) CMake 把它声明为 libc.include.llvm-libc-types.X 目标，供依赖传播
```

#### 4.3.3 源码精读

`size_t` 的内部定义只有一行：[size_t.h:L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types/size_t.h#L12) 用 `typedef __SIZE_TYPE__ size_t;`。`__SIZE_TYPE__` 是 Clang/GCC 内置的「本目标上 `size_t` 的正确底层类型」标记，免去手工按 32/64 位挑选 `unsigned long`/`unsigned long long` 的麻烦——这也是「自包含」的底气：不依赖任何系统头，只依赖编译器内置。

`FILE` 则展示了「把私有布局藏起来」的技巧：[FILE.h:L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types/FILE.h#L12) 只写 `typedef struct FILE FILE;`——一个**不透明的前向声明**。真正的 `struct FILE` 内部布局定义在实现侧（`src/__support/File/`），对外只暴露指针。这保证了 `FILE` 的 ABI 稳定且可实现可改，与系统 libc 的「`FILE` 是已知布局的结构体」形成对照（也正是 Overlay 不能用这个内部 `FILE` 的原因）。

CMake 层面，每个公共类型都被包成一个可被 `DEPENDS` 引用的头库目标。以 `size_t` 代理为例：[hdr/types/CMakeLists.txt:L172-L178](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/CMakeLists.txt#L172-L178) 用 `add_proxy_header_library(size_t ...)`，其 `FULL_BUILD_DEPENDS libc.include.llvm-libc-types.size_t` 表示「仅 Full 模式才依赖内部 size_t 类型目标」；注意它**没有**普通 `DEPENDS`，因为 Overlay 分支靠编译器自带 `<stddef.h>` 即可，无需额外目标。对比 `FILE` 代理 [hdr/types/CMakeLists.txt:L379-L388](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/CMakeLists.txt#L379-L388)：它额外有 `DEPENDS libc.hdr.stdio_overlay`（Overlay 下要拉系统 `FILE`）和 `FULL_BUILD_DEPENDS ... libc.include.stdio`（Full 下用生成的 stdio 头）。两个目标的依赖差异，恰好映射了 4.1 讲的来源差异。

#### 4.3.4 代码实践

**实践目标**：体会「一类一文件」与「不透明类型」两件事。

**操作步骤**：

1. 浏览 [include/llvm-libc-types/](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types) 目录，挑 3 个文件（如 `size_t.h`、`FILE.h`、`pid_t.h`）打开，确认每个都只定义一个类型、都不超过几行。
2. 打开 [hdr/types/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/CMakeLists.txt)，对比 `size_t`（无 `DEPENDS`）与 `FILE`（有 `DEPENDS libc.hdr.stdio_overlay`）两条规则的差异。

**需要观察的现象**：自包含、仅依赖编译器内置的类型（`size_t` 依赖 `__SIZE_TYPE__`）在 Overlay 下零额外依赖；而需要与系统 ABI 对齐的不透明类型（`FILE`）则要在 Overlay 下挂一个 overlay 头目标。

**预期结果**：你能从 CMake 的 `DEPENDS`/`FULL_BUILD_DEPENDS` 反推出某类型在两种模式下的来源。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `llvm-libc-types/FILE.h` 只做前向声明，不给出 `struct FILE` 的字段？
**答案**：为了 ABI 稳定与实现自由——对外只暴露 `FILE *`，字段布局是实现的私有细节，可随版本改动而不破坏用户二进制；这也让 Full 模式的 `FILE` 与系统 libc 的 `FILE` 天然不兼容，必须由代理头在两模式间切换。

**练习 2**：`__SIZE_TYPE__` 相比直接写 `unsigned long` 有什么好处？
**答案**：`__SIZE_TYPE__` 由编译器按当前目标（32/64 位、ABI）自动给出 `sizeof` 语义的正确无符号类型，跨平台无需手改，且与编译器其余内建判断（如 `sizeof` 的结果类型）保持一致。

### 4.4 common 前缀 __llvm-libc-common.h

#### 4.4.1 概念说明

[include/__llvm-libc-common.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h) 是所有公共头（以及 `hdr/func/` 这类函数代理）共用的**通用前缀**。它不定义任何 libc 业务类型，只提供两类东西：

1. 一个身份标记：`__LLVM_LIBC__` 被定义为 `1`，可用来在预处理期探测「当前头是否由 LLVM-libc 提供」。
2. 一套**跨 C/C++ 的移植宏**：`__BEGIN_C_DECLS` / `__END_C_DECLS`、`__NOEXCEPT`、`__restrict`、`_Noreturn`、`_Alignas`、`_Alignof`、`_Static_assert`、`_Returns_twice`、`__LLVM_LIBC_CAST` 等。

为什么需要这层前缀？因为公共头要**同时被 C 和 C++ 消费**：用户写 C 程序时它是 C 头；而 LLVM-libc 自己用 C++ 实现，编译期也包含它。C++ 需要 `extern "C"` 包住声明、用 `noexcept`；C89/C99/C11 对 `_Noreturn`、`restrict` 的支持又各不相同。把这些差异统一封装成宏，公共头里就能写「与语言无关」的声明。

#### 4.4.2 核心流程

```text
hdrgen 生成公共头时，第一个 include 永远是 __llvm-libc-common.h
        │
        ├─ 默认模板：header.py 的 include_lines(with_common=True) 自动在最前插入
        └─ 自定义 .h.def 模板：由作者手动写 #include "__llvm-libc-common.h"
                │
                ▼
   公共头正文里大量使用它定义的宏：
   - 每个函数声明都被 __BEGIN_C_DECLS / __END_C_DECLS 包裹
   - 每个函数声明末尾追加 __NOEXCEPT
   - 参数中的 __restrict、返回 _Noreturn 等也来自这里
```

#### 4.4.3 源码精读

身份标记在文件开头：[__llvm-libc-common.h:L9-L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h#L9-L12) 定义头文件守卫并把 `__LLVM_LIBC__` 置 `1`。

C++ 分支里的声明包裹宏：[__llvm-libc-common.h:L16-L20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h#L16-L20) 把 `__BEGIN_C_DECLS` 定义为 `extern "C" {`、`__END_C_DECLS` 定义为 `}`；[__llvm-libc-common.h:L40-L45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h#L40-L45) 把 `__NOEXCEPT` 定义为 `noexcept`（C++11 起）。

切换到 C 分支，同样的宏给出 C 的等价物：[__llvm-libc-common.h:L59-L65](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h#L59-L65) 让 `__BEGIN_C_DECLS`/`__END_C_DECLS` 在纯 C 下**展开为空**（C 不需要 `extern "C"`）；[__llvm-libc-common.h:L87-L92](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h#L87-L92) 在 GNU C 下把 `__NOEXCEPT` 映射为 `__attribute__((__nothrow__))`，否则留空——于是公共头里写一个 `__NOEXCEPT`，在三种语言模式下都能得到「最合适」的写法。

hdrgen 的生成器把它当作不可遗漏的前缀：[header.py:L36](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L36) 定义 `COMMON_HEADER = "__llvm-libc-common.h"`；[header.py:L257-L270](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L257-L270) 的 `include_lines` 在 `with_common=True` 时把它放在所有 include 之前并算好相对路径。注意它的注释说明：**默认模板会隐式插入它，自定义 `.h.def` 模板则必须作者手动写**——[errno.h.def:L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/errno.h.def#L12) 正是手动写的例子。

最后看一个「消费这些宏」的实例：[errno.h.def:L34-L42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/errno.h.def#L34-L42) 在 `%%public_api()` 之后用 `__BEGIN_C_DECLS`/`__END_C_DECLS` 包住 `__llvm_libc_errno` 声明，并在其末尾加 `__NOEXCEPT`——这两个宏的展开完全由 common 前缀按语言决定。

补充：函数代理头也直接使用此前缀，如 [hdr/func/malloc.h:L12-L19](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/func/malloc.h#L12-L19)：Full 分支先 include `hdr/types/size_t.h`（拿类型）和 `include/__llvm-libc-common.h`（拿宏），再用 `__BEGIN_C_DECLS ... malloc(size_t) __NOEXCEPT ... __END_C_DECLS` 给出与公共头一致的声明；Overlay 分支则回退到 `hdr/stdlib_overlay.h` 拉系统 `<stdlib.h>`。可见 common 前缀是「写一条声明」的公共地基。

#### 4.4.4 代码实践

**实践目标**：验证「同一个 `__NOEXCEPT`，三种展开」。

**操作步骤**：

1. 打开 [include/__llvm-libc-common.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/__llvm-libc-common.h)，分别在 `#ifdef __cplusplus` 与其 `#else` 两段里找到 `__NOEXCEPT`、`__BEGIN_C_DECLS` 的定义。
2. 在 [include/errno.h.def](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/errno.h.def) 里找到使用这两个宏的声明。
3. （可选）写一个最小测试文件包含生成后的 `errno.h`，分别以 `clang -x c` 与 `clang -x c++` 用 `clang -E` 预处理，比对 `__NOEXCEPT` 与 `__BEGIN_C_DECLS` 的展开差异。

**需要观察的现象**：C++ 下出现 `extern "C" {` 与 `noexcept`；C 下二者分别变为空与（GNU C 下）`__attribute__((__nothrow__))`。

**预期结果**：你会直观看到「公共头写一份、宏替你适配语言」的效果。

#### 4.4.5 小练习与答案

**练习 1**：为何 hdrgen 给每个函数声明末尾都追加 `__NOEXCEPT`（见 header.py L346）？
**答案**：标准 C 的这些函数本就不抛 C++ 异常；显式标注 `noexcept`（C++）或 `__nothrow__`（GNU C）既能让编译器做更优代码生成，也防止 C++ 调用方误以为可能抛异常。统一由 common 前缀的宏适配各语言。

**练习 2**：自定义 `.h.def` 模板如果忘了写 `#include "__llvm-libc-common.h"` 会怎样？
**答案**：模板正文里用到的 `__BEGIN_C_DECLS`、`__NOEXCEPT` 等宏将无定义（默认模板会自动补，自定义模板不会），导致编译公共头时报「undeclared identifier」之类的错误。因此 hdrgen 文档与代码注释都强调自定义模板必须手动 include 它。

## 5. 综合实践

把本讲四块知识串起来，跟踪 **`size_t` 在两种构建模式下从实现代码到最终定义的完整路径**，并画出切换示意图。

**任务**：

1. 起点：在 `src/` 中任选一处 `#include "hdr/types/size_t.h"`（如 `src/__support/block.h:18`）。
2. **Full 路径**：进入 [hdr/types/size_t.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/size_t.h) 的 `#ifdef LIBC_FULL_BUILD` 分支 → [include/llvm-libc-types/size_t.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/llvm-libc-types/size_t.h) → `typedef __SIZE_TYPE__ size_t;`。再回到 CMake：[hdr/types/CMakeLists.txt:L172-L178](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/CMakeLists.txt#L172-L178) 的 `FULL_BUILD_DEPENDS` 把这条路径接进构建依赖图。
3. **Overlay 路径**：进入同一代理头的 `#else` 分支 → `__need_size_t` + 编译器自带 `<stddef.h>` → 取系统/编译器的 `size_t`。注意此时代理目标**无** `DEPENDS`，因为不依赖任何 libc 内部头。
4. 画一张图，把「实现代码、代理头、`llvm-libc-types/`、`llvm-libc-macros/`（顺带把 `__llvm-libc-common.h` 也标在「生成公共头」一侧）、系统头」放进去，用两条颜色区分 Full / Overlay。
5. 进阶：把 `FILE` 也画一遍，对比它为何在 Overlay 下多了一个 `DEPENDS libc.hdr.stdio_overlay`，并把这与 u1-l4 的 ABI 论断（`fopen` 不能进 Overlay）挂钩。

**验收标准**：你的图能让另一个没读过本讲的人，仅凭「Full 还是 Overlay」一个条件，就预测出 `size_t` 与 `FILE` 各自来自哪里。

## 6. 本讲小结

- **代理头 `hdr/`** 是实现代码与「类型/宏来源」之间的一层适配器，靠 `#ifdef LIBC_FULL_BUILD` 在「内部自包含定义」与「系统头」之间二选一，让 `src/` 只写一条 include。
- **`llvm-libc-macros/` 与 `llvm-libc-types/`** 是两类**自包含公共头成品**（一类一文件、已是安装形态），在 Full 模式下既被 hdrgen 拼进公共头，也被代理头采用。
- **`__llvm-libc-common.h`** 是所有公共头与函数代理的**通用前缀**，定义 `__LLVM_LIBC__` 标记和一整套跨 C/C++ 移植宏（`__BEGIN_C_DECLS`、`__NOEXCEPT`、`__restrict`…），由 hdrgen 自动或在 `.h.def` 中手动插入。
- **CMake 的 `add_proxy_header_library`** 用 `DEPENDS`（两模式都有）与 `FULL_BUILD_DEPENDS`（仅 Full）精确表达来源差异——从依赖列表即可反推某类型在 Overlay 下是否需要系统头。
- **`size_t` 与 `FILE` 的对比**是理解全篇的钥匙：前者自包含、零系统依赖；后者 ABI 敏感，Overlay 下必须挂 overlay 头目标去拉系统布局。

## 7. 下一步学习建议

- 本讲聚焦「实现代码如何取类型/宏」，下一篇 **u5-l1 ctype 函数族**将进入「实现代码如何写逻辑」，你会看到 ctype 入口点如何通过代理头拿到 `size_t` 之外的工具、并把判定下沉到 `__support/ctype_utils.h`。
- 若你想更理解「公共头如何被 hdrgen 用这些成品拼出来」，可重读 **u3-l1**，并用本讲学到的 `llvm-libc-macros/`、`llvm-libc-types/`、`__llvm-libc-common.h` 去「对位」`header.py` 里 `includes()` 与 `public_api()` 的输出。
- 想从「类型 ABI」深入到「OS 层抽象」的读者，可在学完 u5 之后进入 **u8-l1 OSUtil 与 Linux 系统调用封装**，那里会用到更多 `hdr/types/` 下的系统类型代理（`pid_t`、`ssize_t` 等）。
