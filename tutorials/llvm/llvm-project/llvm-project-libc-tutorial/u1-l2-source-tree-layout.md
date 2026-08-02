# 源码目录结构总览

## 1. 本讲目标

学完本讲后，你应该能够：

- 在不看文档的情况下，说出 LLVM-libc 每一个顶层目录（`src`、`include`、`config`、`startup`、`test`、`utils`、`hdr`、`lib`、`benchmarks`、`fuzzing` 等）的职责。
- 理解 `src` 目录「按公共头文件名划分子目录」这条最重要的组织约定，并能据此推断任意函数的代码位置。
- 看懂顶层 `CMakeLists.txt` 如何决定「哪些目录被加载、以什么顺序加载」，从而理解整个构建的根入口。
- 当你面对一个陌生的 libc 函数（例如 `memcpy`），仅靠目录约定就能迅速定位它的「实现文件、内部头文件、测试文件、平台注册文件」四类文件。

本讲承接上一讲「LLVM-libc 是什么」。上一讲建立了项目定位，本讲带你走进仓库，画一张可以长期对照的全局地图。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自上一讲）：

- **LLVM-libc** 是 LLVM 项目中用现代 C++ 从零编写的 C 标准库实现，强调模块化、多平台、正确性。
- 它的对外接口是标准 C，内部实现却是 C++。
- 它通过 **runtimes** 构建系统来编译，而不是把 `libc/` 当作一个独立的 CMake 根。

此外，本讲会用到几个通用概念，先做最简解释：

- **entrypoint（入口点）**：LLVM-libc 把每一个对外公开的函数或全局变量（如 `memcpy`、`errno`）都当成一个独立的、可单独构建的最小单元，称为入口点。这是后续进阶讲义的核心概念，本讲只需记住「一个函数 = 一个入口点」即可。
- **CMake 子目录（add_subdirectory）**：CMake 用 `add_subdirectory(目录)` 来「进入」一个子目录并执行其中的 `CMakeLists.txt`。顶层 `CMakeLists.txt` 里 `add_subdirectory` 的书写顺序，就是这些目录被处理的顺序。
- **平台裁剪**：不同操作系统（Linux、Darwin、Windows…）和不同架构（x86_64、aarch64、riscv…）支持的函数集合不同，LLVM-libc 用一组配置文件来表达「这个目标支持哪些函数」。

## 3. 本讲源码地图

本讲主要阅读以下文件：

| 文件 | 作用 |
| --- | --- |
| [docs/dev/source_tree_layout.md](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md) | 官方对目录布局的权威说明，是本讲的主线。 |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt) | 顶层构建根入口，定义了加载各子目录的顺序与全局变量。 |
| [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/config/linux/x86_64/entrypoints.txt) | 平台配置示例，记录「Linux x86_64 支持哪些入口点」。 |
| [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt) | 把入口点聚合成 `libc.a`/`libm.a` 的目标定义。 |

阅读时建议把仓库的目录树开在一旁，边读边对照。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **顶层目录职责**：每个目录是干什么的。
2. **`src` 目录的组织约定**：函数代码按什么规律摆放。
3. **构建根入口**：顶层 `CMakeLists.txt` 如何串起这一切。

### 4.1 顶层目录职责全景

#### 4.1.1 概念说明

LLVM-libc 不是一个把所有代码堆在 `src/` 里的普通项目。它的顶层被刻意拆成十几个目录，每个目录承担一种「关注点」：实现、规范、配置、生成、测试、构建工具、启动代码……这种拆分的目的是让任意一部分都能被独立理解、独立使用，呼应了项目「模块化」的设计目标。

理解目录职责，相当于拿到了仓库的「图例」。有了图例，你之后看任何一个函数，都能知道它的「同类」分别落在哪些抽屉里。

#### 4.1.2 核心流程

官方文档在 `source_tree_layout.md` 里直接画出了一棵顶层目录树。我们可以把它分成几组来记忆：

```text
+ libc
     - benchmarks    # 性能基准测试（主要服务内存函数）
     - cmake         # CMake 构建规则的实现（add_entrypoint_object 等）
     - config        # 各平台/架构的默认配置（支持哪些函数/头文件）
     - docs          # 设计文档与说明文档
     - examples      # 示例程序（如 hello_world）
     - fuzzing       # 模糊测试（目录结构镜像 libc 本身）
     - hdr           # 代理头文件（Full/Overlay 模式间切换定义来源）
     - include       # *.h.def 模板 + 自包含公共头（llvm-libc-macros/types）
     - lib           # 聚合入口点，产出 libc.a / libm.a
     - shared        # 与其它 runtimes（如 compiler-rt）共享的头文件
     - src           # 入口点的真正实现（本讲重点）
     - startup       # 程序启动对象，如 crt1.o
     - test          # 测试（目录结构镜像 libc 本身）
     - utils         # 工具（如 hdrgen 头文件生成器、MPFR 包装等）
```

> 说明：`shared` 与 `examples` 在实际仓库中存在，但官方 `source_tree_layout.md` 没有为它们单列段落。本讲依据实际目录内容补充说明。

按「关注点」分组记忆更牢固：

| 分组 | 目录 | 一句话职责 |
| --- | --- | --- |
| **源码实现** | `src` | 所有入口点的实现代码。 |
| **对外接口** | `include`、`hdr` | 公共头文件的规范与生成入口。 |
| **平台配置** | `config` | 决定「某个目标支持哪些函数/头文件」。 |
| **组装与启动** | `lib`、`startup` | 把实现聚合成库、提供程序启动对象。 |
| **质量保障** | `test`、`fuzzing`、`benchmarks` | 单元测试、模糊测试、性能基准。 |
| **构建与工具** | `cmake`、`utils` | CMake 规则实现 + 各类代码生成/校验工具。 |
| **文档与示例** | `docs`、`examples` | 设计文档与可运行示例。 |
| **跨运行时共享** | `shared` | 与 LLVM 其它 runtime 共用的少量头文件。 |

#### 4.1.3 源码精读

官方文档把目录树画在 [docs/dev/source_tree_layout.md:8-23](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L8-L23)，这是最权威的总览，遇到不确定的目录都应该先回到这里对照。

下面挑几个最容易混淆的目录，结合原文说明：

**`config` —— 平台支持范围的「事实来源」**。[docs/dev/source_tree_layout.md:32-40](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L32-L40) 指出，`config/<platform>/<architecture>/` 下有四个固定名字的文件：

- `entrypoints.txt` —— 该目标支持哪些入口点；
- `exclude.txt` —— 要排除哪些入口点；
- `headers.txt` —— 要生成哪些公共头文件；
- `config.json` —— 该目标的构建选项。

也就是说，「某平台支不支持某个函数」不是写在源码里，而是写在配置文件里。这是 LLVM-libc 可移植性的关键。

**`include` —— 头文件不是手写的，而是生成的**。[docs/dev/source_tree_layout.md:66-75](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L66-L75) 说明 `include` 里有两类东西：一是用来构造公共头文件的 `*.h.def` 模板；二是已经处于「可安装形态」的自包含公共头（主要在 `llvm-libc-macros` 与 `llvm-libc-types` 子目录）。我们在仓库里确实能看到 `assert.h.def`、`errno.h.def`、`math.h.def` 等模板文件，以及 `llvm-libc-macros/`、`llvm-libc-types/` 两个子目录。

**`hdr` —— Full 与 Overlay 的切换开关**。[docs/dev/source_tree_layout.md:59-64](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L59-L64) 解释：`hdr` 里放的是「代理头文件」，被 `src` 里的代码 include。它们会根据当前是 **fullbuild** 还是 **overlay** 模式，选择引入 LLVM-libc 内部的类型/宏定义，还是引入系统的定义。这衔接了上一讲提到的「Overlay 增补 vs Full 替换」。

**`test` —— 镜像 `src` 的结构**。[docs/dev/source_tree_layout.md:101-107](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L101-L107) 举例：`mmap` 的实现住在 `src/sys/mman`，那么它的测试就住在 `test/src/sys/mman`。这条「镜像」约定非常关键，它让我们可以「按实现路径推测试路径」。

#### 4.1.4 代码实践

**实践目标**：用一个简单的 `ls` 命令把目录树「摸」一遍，建立肌肉记忆，而不是只看文档。

**操作步骤**：

1. 在仓库根目录 `libc/` 下执行列目录：

   ```bash
   ls -1F libc/
   ```

2. 观察输出，把它与上面「按关注点分组」的表格逐一对照。
3. 进入几个代表性子目录，看一眼里面的内容：

   ```bash
   ls -1F libc/config/linux/x86_64/   # 应看到 entrypoints.txt/exclude.txt/headers.txt
   ls -1F libc/include/ | head        # 应看到 *.yaml 与 *.h.def 以及 llvm-libc-* 子目录
   ls -1F libc/shared/                # 看看 shared 里有哪些跨运行时头文件
   ```

**需要观察的现象**：

- `config/linux/x86_64/` 下确实只有 `entrypoints.txt`、`exclude.txt`、`headers.txt` 三个文本文件（外加可能的 `config.json`）。
- `include/` 下既有 `ctype.yaml`、`string.yaml` 这类机器可读规范，也有 `assert.h.def`、`errno.h.def` 这类模板。
- `shared/` 下有 `libc_common.h`、`math/`、`rpc.h`、`str_to_integer.h` 等，是少量与其它 runtime 共用的头文件。

**预期结果**：你能指着表格里的每一行，在磁盘上找到对应的目录并说出它的职责。若运行环境受限无法执行命令，可改为在 GitHub 仓库页面浏览同名目录，结论一致。

> 待本地验证：具体子目录内容会随版本演进，但「顶层目录职责」这层结构非常稳定。

#### 4.1.5 小练习与答案

**练习 1**：官方 `source_tree_layout.md` 的目录树里没有 `shared` 和 `examples`，但仓库里却存在。请到仓库里确认这两个目录是否存在，并各用一句话说明它们的用途。

**参考答案**：两者都存在。`examples/` 存放可运行示例（如 `hello_world/` 与 `examples.cmake`），帮助新手跑通第一次构建；`shared/` 存放与 LLVM 其它 runtime（如 compiler-rt）共享的头文件（如 `libc_common.h`、`math/`、`rpc.h`、`str_to_integer.h`）。它们未被官方布局文档单列，但确实属于顶层。

**练习 2**：`config/linux/x86_64/` 下的三个 `.txt` 文件各自承担什么职责？

**参考答案**：`entrypoints.txt` 列出该目标启用的入口点；`exclude.txt` 列出要从启用列表中剔除的入口点；`headers.txt` 列出要生成的公共头文件。

---

### 4.2 `src` 目录的组织约定：按公共头文件名分目录

#### 4.2.1 概念说明

顶层 `src/` 是所有入口点实现的家。它的子目录并不是随意命名的，而是遵循一条极简却极强的约定：

> **每提供一个公共头文件，`src/` 下就有一个同名的子目录。**

例如公共头文件 `ctype.h` 对应 `src/ctype/`，`string.h` 对应 `src/string/`，`math.h` 对应 `src/math/`。这条约定意味着：只要你知道一个函数属于哪个标准头文件，你就能直接推断出它的实现住在哪个目录。

这条约定是本讲最重要的「导航公式」，掌握它之后，定位函数代码的速度会提升一个量级。

#### 4.2.2 核心流程

给定一个标准函数，定位其实现的思维流程是：

```text
1. 这个函数声明在哪个标准头文件里？   例如 memcpy → string.h
2. 去掉 .h，得到目录名。             string.h → string
3. 进入 src/<目录名>/ 查找实现。      src/string/memcpy.cpp
4. 同目录通常还有同名内部头文件。     src/string/memcpy.h
5. 测试按镜像约定查找。              test/src/string/memcpy_test.cpp
6. 平台注册回到 config。             config/linux/x86_64/entrypoints.txt
```

一个函数的「四件套」（实现、内部头、测试、注册）就按这条链路被串起来。

#### 4.2.3 源码精读

官方文档对这条约定有明确表述。[docs/dev/source_tree_layout.md:82-94](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md#L82-L94) 写道：

> For every public header file provided by llvm-libc, there exists a corresponding directory in the `src` directory. The name of the directory is same as the base name of the header file. For example, the directory corresponding to the public `math.h` header file is named `math`.

把这条约定在仓库里验证一遍。`src/` 下的子目录名几乎就是一个标准头文件名清单：

```text
src/ctype/      ← ctype.h
src/string/     ← string.h
src/math/       ← math.h
src/stdio/      ← stdio.h
src/stdlib/     ← stdlib.h
src/unistd/     ← unistd.h
src/sys/        ← sys/*.h（如 sys/mman.h → src/sys/mman/）
...
```

以 `memcpy` 为例，按约定走一遍：

- `memcpy` 声明在 `string.h` → 目录名 `string` → 实现 [src/string/memcpy.cpp](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/src/string/memcpy.cpp)，内部头 [src/string/memcpy.h](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/src/string/memcpy.h)。
- 测试按镜像约定 → [test/src/string/memcpy_test.cpp](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/test/src/string/memcpy_test.cpp)。
- 平台注册 → [config/linux/x86_64/entrypoints.txt:85](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/config/linux/x86_64/entrypoints.txt#L85)，原文片段：

  ```text
  # string.h entrypoints
  libc.src.string.memccpy
  libc.src.string.memchr
  libc.src.string.memcmp
  libc.src.string.memcpy     ← 这一行把 memcpy 注册到该平台
  ```

注意这个注册名的格式 `libc.src.string.memcpy`：它本身就是一条「目录路径」`src/string/memcpy`，把命名空间点和目录斜杠对应起来。这意味着配置文件里的入口点名也遵守同一条组织约定。

此外，`src/` 下还有一个特殊的 [src/__support/](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/src/__support) 子目录。它**不对应任何公共头文件**，而是所有入口点共享的「内部工具库」（如 `CPP/`、`FPUtil/`、`OSUtil/` 等）。以 `__` 双下划线开头，表明它是私有的、不对外暴露的实现细节。后续进阶讲义会专门讲它。

#### 4.2.4 代码实践

**实践目标**：用目录约定，从「函数名」一路推导到「四件套」的完整路径，不依赖搜索。

**操作步骤**（以 `memcpy` 为例，你也可以换 `memset`、`isalpha` 等再练一遍）：

1. 判断函数所属头文件：`memcpy` 属于 `string.h`。
2. 推断实现目录：`src/string/`。
3. 列出该目录里和 `memcpy` 相关的文件：

   ```bash
   ls -1 libc/src/string/ | grep memcpy
   ```

   预期看到 `memcpy.cpp` 与 `memcpy.h`。

4. 按镜像约定推断测试位置并列出：

   ```bash
   ls -1 libc/test/src/string/ | grep memcpy
   ```

   预期看到 `memcpy_test.cpp`。

5. 回到配置文件确认它在该平台被注册：

   ```bash
   grep -n "memcpy" libc/config/linux/x86_64/entrypoints.txt
   ```

   预期看到一行类似 `libc.src.string.memcpy`。

**需要观察的现象**：实现、内部头、测试三个文件名都以函数名 `memcpy` 为前缀；配置文件里的注册名恰好是「点分目录路径」。

**预期结果**：你得到这样一份清单（与上一节源码精读一致）：

| 角色 | 路径 |
| --- | --- |
| 实现 | `src/string/memcpy.cpp` |
| 内部头 | `src/string/memcpy.h` |
| 测试 | `test/src/string/memcpy_test.cpp` |
| 平台注册 | `config/linux/x86_64/entrypoints.txt` 第 85 行 |

> 待本地验证：第 4 步「镜像测试目录」对绝大多数函数成立，但个别复杂模块（如带平台子目录的 `stdio`）可能存在 `linux/` 等下级目录，届时需在子目录内继续查找。

#### 4.2.5 小练习与答案

**练习 1**：`isalpha` 函数声明在哪个头文件？按约定它的实现应在哪个目录？

**参考答案**：`isalpha` 声明在 `ctype.h`，因此实现应在 `src/ctype/`（实际文件为 `src/ctype/isalpha.cpp`）。

**练习 2**：入口点注册名 `libc.src.string.memcpy` 与目录路径 `src/string/memcpy` 是什么关系？为什么这种一致性很有用？

**参考答案**：注册名就是目录路径把斜杠换成点。一致性意味着：只要会找代码，就会读写配置；反之看到配置里某个入口点名，也能立刻反推出它的实现目录。代码导航与配置管理共用同一套坐标。

**练习 3**：`src/__support/` 为什么不遵循「按头文件名分目录」的约定？

**参考答案**：因为它不对应任何公共头文件，而是所有入口点共享的私有工具库；用 `__` 前缀表明它是内部实现细节，不会对外暴露。

---

### 4.3 构建根入口：runtimes 与顶层 CMakeLists.txt

#### 4.3.1 概念说明

有了目录职责和 `src` 约定，还差最后一块拼图：**这些目录是怎么被「组装」进一次构建的？** 答案在顶层 `libc/CMakeLists.txt`。

但要先强调一个反直觉的事实：**你不能把构建直接根在 `libc/` 目录里**。顶层 CMakeLists 一开头就用 `FATAL_ERROR` 明确拒绝了这种方式，要求构建必须根在 LLVM 的 **runtimes** 目录。这是 LLVM runtimes 构建体系的要求（上一讲提到的 `LLVM_ENABLE_RUNTIMES` 机制），本讲只需记住这个事实，具体构建命令留给下一讲「构建与运行入门」。

#### 4.3.2 核心流程

顶层 `CMakeLists.txt` 在配置阶段做的事，可以概括为一条主线：

```text
1. 拒绝「根在 libc/」的构建，要求走 runtimes。
2. 设置全局变量：C++ 标准、LIBC_SOURCE_DIR、内部命名空间等。
3. 加载平台配置：config/<os>/<arch>/ 下的 entrypoints.txt / headers.txt / exclude.txt。
4. 按依赖顺序 add_subdirectory 进入各子目录。
5. 最终在 lib/ 把入口点聚合成 libc.a / libm.a。
```

第 4 步的「顺序」不是任意的：被依赖的目录必须先加载。例如 `lib/` 和 `test/` 放在最后，因为它们要消费前面所有目录里定义的组件。

#### 4.3.3 源码精读

**「不能根在 libc/」的硬性约束**。[CMakeLists.txt:12-16](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L12-L16) 写道：

```cmake
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  message(FATAL_ERROR "Builds rooted in the libc directory are not supported. "
    "Builds should be rooted in the runtimes directory instead. ...")
endif()
```

这段代码的作用是：如果有人试图 `cmake libc/` 直接配置，立刻报错中止，并提示去用 runtimes 目录。

**全局变量的设定**。文件里设置了几个影响全局的量：

- 默认 C++ 标准。[CMakeLists.txt:46](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L46)：`set(CMAKE_CXX_STANDARD 17)` —— 整个 libc 用 C++17 编写。
- 源码根目录。[CMakeLists.txt:51](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L51)：`set(LIBC_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR})` —— 后续所有路径都以它为基准。
- 内部命名空间。[CMakeLists.txt:58](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L58) 起设置 `default_namespace` 为 `__llvm_libc`（并可能带上版本后缀），所有内部符号都裹在这个命名空间里，避免与系统 libc 同名符号冲突。

**加载平台配置**。[CMakeLists.txt:381-397](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L381-L397) 把 `entrypoints.txt`、`headers.txt`、`exclude.txt` 通过 CMake 的 `include()` 引入，从而得到三个入口点列表（`TARGET_LIBC_ENTRYPOINTS`、`TARGET_LIBM_ENTRYPOINTS` 等）。这些列表就是「这个目标到底要构建哪些函数」的依据。

**按依赖顺序加载子目录**。[CMakeLists.txt:429-455](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L429-L455) 是本模块最关键的一段，原文节选：

```cmake
add_subdirectory(include)
add_subdirectory(shared)
add_subdirectory(config)
add_subdirectory(hdr)
add_subdirectory(src)
add_subdirectory(utils)

if(LLVM_LIBC_FULL_BUILD AND NOT LIBC_TARGET_ARCHITECTURE_IS_SPIRV)
  # startup 依赖库组件，因此放在库实现目录之后
  add_subdirectory(startup)
endif()

# lib 和 test 放在最后，因为它们要消费前面所有目录里的组件
add_subdirectory(lib)
if(LLVM_INCLUDE_TESTS)
  add_subdirectory(test)
  add_subdirectory(fuzzing)
endif()

add_subdirectory(benchmarks)
```

读懂这段就能回答几个常见疑问：

- 为什么 `startup` 有条件加载？因为它只在 **Full build**（且非 SPIRV）时才需要，Overlay 模式不需要自带的启动对象（注释见 [CMakeLists.txt:436-440](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L436-L440)）。
- 为什么 `lib`、`test` 放最后？注释直说：它们会用到前面所有目录里定义的组件，必须等这些都就绪。
- `src` 在 `lib` 之前，正解释了「入口点实现先就位，再由 `lib/` 聚合成静态库」的因果关系。

**最终的聚合**。`lib/CMakeLists.txt` 把入口点列表交给 `add_entrypoint_library` 规则，产出 `libc.a`、`libm.a`（Full 模式）或 `libllvmlibc.a`（Overlay 模式）。从 [lib/CMakeLists.txt:1-13](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt#L1-L13) 可看到它根据 `LLVM_LIBC_FULL_BUILD` 选择归档名：

```cmake
if(LLVM_LIBC_FULL_BUILD)
  list(APPEND libc_archive_names c m mvec)        # → libc.a libm.a libmvec.a
else()
  list(APPEND libc_archive_names llvmlibc)        # → libllvmlibc.a
endif()
```

这就把本讲的三条线索（目录职责 → `src` 约定 → 构建根入口）收束到了「最终产物」上。

#### 4.3.4 代码实践

**实践目标**：不运行构建，仅靠阅读 `CMakeLists.txt`，画出「目录加载顺序图」，并解释每一处条件分支。

**操作步骤**：

1. 打开 [libc/CMakeLists.txt 第 429-455 行](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt#L429-L455)。
2. 把每个 `add_subdirectory(...)` 按出现顺序抄下来，标注它是「无条件」还是「有条件」（条件是什么）。
3. 对 `startup`、`test`、`fuzzing`、`docs` 四个有条件的目录，分别写出触发条件。
4. 思考：为什么 `src` 必须在 `lib` 之前？

**需要观察的现象**：`include → shared → config → hdr → src → utils` 是固定的先序；`startup`、`lib`、`test`、`fuzzing`、`benchmarks`、`docs` 的加载都带有或显式或隐式的条件。

**预期结果**：得到一张类似下面的顺序图（条件以 Full 构建为例）：

```text
include → shared → config → hdr → src → utils
   → startup (仅 LLVM_LIBC_FULL_BUILD 且非 SPIRV)
   → lib
   → test, fuzzing (仅 LLVM_INCLUDE_TESTS)
   → benchmarks
   → docs (仅 LIBC_INCLUDE_DOCS)
```

「`src` 必须在 `lib` 之前」的原因：`lib/` 要把 `src/` 里定义的入口点聚合成静态库，依赖必须先就绪。这正是注释里「tests and libraries potentially draw from the components present in all of the other directories」的含义。

> 待本地验证：不同 CMake 选项（如 `LLVM_LIBC_FULL_BUILD=OFF`、`LLVM_INCLUDE_TESTS=OFF`）会改变实际加载的目录集合，可在一次真实配置后用 `grep` 生成的 `build.ninja` 验证哪些目录被纳入。

#### 4.3.5 小练习与答案

**练习 1**：为什么顶层 `CMakeLists.txt` 一开头就要 `FATAL_ERROR` 拒绝「根在 `libc/`」？

**参考答案**：因为 LLVM-libc 必须通过 LLVM 的 **runtimes** 构建体系来编译（构建根在 `runtimes` 目录，由 `LLVM_ENABLE_RUNTIMES=libc` 驱动）。直接以 `libc/` 为根会缺少 runtimes 提供的编译器、交叉编译、多目标运行时目录等基础设施，所以被显式禁止。

**练习 2**：`add_subdirectory` 的顺序能随便调换吗？举一个「调换会出问题」的例子。

**参考答案**：不能。`lib/` 依赖 `src/` 等目录里定义的入口点目标，若把 `lib` 放到 `src` 之前，CMake 会因为找不到这些目标而报错。同理 `test/` 依赖几乎所有其它目录，所以被放在最后。

**练习 3**：Full 模式和 Overlay 模式产出的归档文件名分别是什么？由哪段逻辑决定？

**参考答案**：Full 模式产出 `libc.a`/`libm.a`/`libmvec.a`，Overlay 模式产出 `libllvmlibc.a`。由 [lib/CMakeLists.txt:1-13](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt#L1-L13) 中对 `LLVM_LIBC_FULL_BUILD` 的 `if/else` 决定归档名列表。

## 5. 综合实践

把本讲三个模块串起来，完成一次「函数四件套定位」小任务。

**任务**：任选一个标准库函数（推荐 `memset`，结构与 `memcpy` 相似；也可选 `isalpha`、`strlen` 等），在不使用全文搜索的前提下，仅凭目录约定，写出它的：

1. **实现文件**路径（`.cpp`）；
2. **内部头文件**路径（`.h`）；
3. **测试文件**路径；
4. **平台注册位置**（在哪个 `config` 文件、大致第几行）。

**步骤建议**：

1. 先问自己：这个函数声明在哪个标准头文件？（例如 `memset` → `string.h`）
2. 套用 4.2 的「导航公式」，推出实现、内部头、测试三处路径。
3. 到 `config/linux/x86_64/entrypoints.txt` 里，按 `libc.src.<目录>.<函数>` 的格式找到注册行。
4. 用 `ls`/`grep` 验证你的推断（验证用命令见 4.2.4）。

**以 `memset` 为参考答案**（可先自己做再对照）：

| 角色 | 路径 |
| --- | --- |
| 实现 | `src/string/memset.cpp` |
| 内部头 | `src/string/memset.h` |
| 测试 | `test/src/string/memset_test.cpp` |
| 平台注册 | `config/linux/x86_64/entrypoints.txt`（与 `memcpy` 同属 `# string.h entrypoints` 段，注册名形如 `libc.src.string.memset`） |

**进阶思考**：把这个函数画成一张「四件套关系图」——以函数名为中心，向四个方向分别连到实现、内部头、测试、配置。这张图就是你在本讲建立的最实用的心智模型，后续每一篇讲义都会反复用到它。

## 6. 本讲小结

- LLVM-libc 顶层目录按「关注点」拆分：`src` 实现、`include`/`hdr` 头文件规范与生成、`config` 平台裁剪、`lib`/`startup` 组装与启动、`test`/`fuzzing`/`benchmarks` 质量保障、`cmake`/`utils` 构建与工具、`docs`/`examples` 文档与示例、`shared` 跨运行时共享。
- `src/` 遵循「按公共头文件名分目录」的约定：`ctype.h` → `src/ctype/`，`string.h` → `src/string/`，`math.h` → `src/math/`，依此类推。这是定位任意函数代码的「导航公式」。
- `test/` 镜像 `src/` 的结构：实现住在 `src/X`，测试就住在 `test/src/X`。
- 配置文件里的入口点注册名（如 `libc.src.string.memcpy`）就是把目录路径的斜杠换成点，与代码组织共用同一套坐标。
- 顶层 `CMakeLists.txt` 是构建根入口：它拒绝「根在 libc/」，设定全局变量，加载平台配置，再按依赖顺序 `add_subdirectory` 串起所有目录，最终在 `lib/` 聚合成 `libc.a`/`libm.a`（Full）或 `libllvmlibc.a`（Overlay）。
- `src/__support/` 是一个特例：它不对应任何公共头文件，而是所有入口点共享的私有工具库。

## 7. 下一步学习建议

本讲建立的是「地图」，下一讲就该「上路」了。建议按以下顺序继续：

1. **下一讲 u1-l3《构建与运行入门：runtimes 构建与 Hello World》**：亲手跑通一次 runtimes 构建，把本讲讲的目录真正「编译」成 `libc.a`，并运行 `examples/hello_world`。
2. **随后 u1-l5《第一个入口点全流程：以 isalpha 为例》**：进入 `src/ctype/`，用一个最简单的函数把「YAML 规范 → 实现 → CMake 注册 → 测试」整条链路打通，巩固本讲的目录约定。
3. **进阶时回头精读**：[docs/dev/source_tree_layout.md](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/dev/source_tree_layout.md) 和 [libc/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt)，随着你对 entrypoint、配置、头文件生成的理解加深，这两个文件会每次读出更深的含义。

建议在进入下一篇讲义前，先完成本讲的「综合实践」——能独立画出某个函数的四件套关系图，再往下走。
