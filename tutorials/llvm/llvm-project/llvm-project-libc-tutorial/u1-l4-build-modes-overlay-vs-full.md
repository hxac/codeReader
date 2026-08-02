# 构建模式：Overlay 模式 vs Full 模式

## 1. 本讲目标

本讲解决一个关键问题：**同样是构建 LLVM-libc，为什么会有好几种不同的配置方式？它们各自产出什么、又该在什么场景下使用？**

读完本讲，你应当能够：

1. 说清 LLVM-libc 的 **五种构建场景**，并明白它们的差异归根结底是「构建根目录不同 + 一个开关 `LLVM_LIBC_FULL_BUILD` 取值不同」。
2. 理解 **Overlay 模式** 如何利用静态库链接顺序「覆盖」系统 libc 的符号，以及为什么 `fopen` 这类依赖实现私有 ABI 的函数不能被覆盖。
3. 理解 **Full 模式** 作为「独立 libc 替换品」的定位，以及它产出 `libc.a` / `libm.a` / 启动对象的区别。
4. 根据目标环境（宿主增补、新 OS、GPU、交叉编译）选出正确的 CMake 配置命令。

本讲是 [u1-l3 构建与运行入门](u1-l3-build-and-run.md) 的进阶：上一讲你已经跑通过一次 runtimes 构建，本讲带你理解那套命令背后「为什么这么写」。

## 2. 前置知识

在进入本讲前，建议你已经具备以下认知（来自前置讲义）：

- **runtimes 构建体系**：LLVM-libc 的构建根在仓库根的 `runtimes/` 而非 `libc/`，由 `LLVM_ENABLE_RUNTIMES` 路由到各子项目；强行在 `libc/` 下构建会被 `FATAL_ERROR` 拒绝。
- **Full / Overlay 两种产物的存在**：Full 模式产出独立的 `libc.a` / `libm.a`，Overlay 模式产出 `libllvmlibc.a`。
- **entrypoint（入口点）**：每个公开函数是一个离散的构建单元，最终被聚合成静态库。

此外需要一点点链接器常识：

- **静态库（`.a`）是「按需取用」的**：链接器看到 `.o` 文件里某个未解析符号时，才会去静态库里把定义该符号的成员（member）抽出来。命令行上**写在前面**的库/目标，其符号会被优先解析。这就是所谓的 **链接顺序语义（link order semantics）**。
- **ABI（应用二进制接口）**：两个编译单元要能互通，不仅函数名要对得上，参数的内存布局（比如 `struct FILE` 里有哪些字段、字段顺序）也必须一致。否则就是「 ABI 不兼容」。

## 3. 本讲源码地图

本讲主要围绕 **文档** 与 **构建脚本** 展开，几乎不涉及 C/C++ 函数实现：

| 文件 | 作用 |
| --- | --- |
| [docs/build_concepts.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md) | 官方对「五种构建场景」的权威说明与各自的最小 CMake 命令。 |
| [docs/overlay_mode.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md) | Overlay 模式专论：链接顺序覆盖原理、`libllvmlibc.a` 命名理由、使用方式。 |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt) | libc 顶层构建脚本：定义 `LLVM_LIBC_FULL_BUILD` 开关，并据此决定是否启用 startup、hermetic 测试、headers.txt 强制检查。 |
| [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt) | 静态库组装脚本：根据 Full/Overlay 选择产出哪些 `.a`、用哪份入口点列表、是否依赖 startup。 |
| [hdr/types/FILE.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/FILE.h) | `FILE` 类型的代理头：Full 模式用 LLVM 自定义的 `FILE`，Overlay 模式回退到系统 `<stdio.h>`。是理解「fopen 为什么不能 Overlay」的关键证据。 |
| [src/stdio/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/stdio/CMakeLists.txt) | stdio 入口点注册：Overlay 模式下给 stdio 注入 `LIBC_COPT_STDIO_USE_SYSTEM_FILE` 宏。 |

## 4. 核心概念与源码讲解

### 4.1 五种构建场景与构建模式开关

#### 4.1.1 概念说明

官方文档 [build_concepts.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md) 把 LLVM-libc 的构建归纳为 **五种场景**：

1. **Overlay Mode（增补系统 libc）**：与系统 libc 共存，只覆盖 LLVM-libc 已实现的少数函数，其余回退到系统库。产出 `libllvmlibc.a`。这是大多数贡献者首选，因为构建和测试最快。
2. **Full Build Mode（独立库）**：把 LLVM-libc 当作完整的 libc 替换品，产出独立的 `libc.a` 和 `libm.a`，用于新 OS 或生成 sysroot。
3. **Bootstrap Build（自举构建）**：先用宿主编译器编译出 Clang 等工具，再用这套新 Clang 去构建 libc，保证「编译器与库互相匹配」。
4. **Cross-compiler Build（交叉编译）**：为目标架构（如在 x86_64 上为 aarch64）构建 libc，需要交叉编译器或 toolchain 文件。
5. **Bootstrap Cross-compiler（新环境自举）**：从零开始（比如只有 Linux 内核头文件）为目标生成一整套编译器 + sysroot。

初学者很容易被「五种」吓到，但其实它们只沿两个正交维度变化：

- **维度 A：构建根目录**。Overlay / Full / Cross 都从 `runtimes/` 配置（`cmake -S runtimes`）；Bootstrap 从 `llvm/` 配置（`cmake -S llvm`），因为它要先编译 Clang。
- **维度 B：`LLVM_LIBC_FULL_BUILD` 开关**。`OFF`（默认）即 Overlay，`ON` 即 Full。

也就是说：**Overlay 与 Full 是两种「产物形态」，而 Bootstrap / Cross 是两种「构建流程」**，二者可以组合（比如「Bootstrap + Full」就是官方推荐的、用新 Clang 构建完整 libc 的方式）。

#### 4.1.2 核心流程

判断「我该用哪种构建」的伪代码：

```text
if 想给已有系统程序换上更快的 strlen / round 等「纯算法」函数:
    → Overlay（LLVM_LIBC_FULL_BUILD=OFF，构建根 runtimes/）
elif 想要一个完全独立的 libc（新 OS / sysroot / GPU）:
    → Full（LLVM_LIBC_FULL_BUILD=ON）
    if 还想用「与新库匹配的最新 Clang」来编译:
        → Bootstrap（构建根 llvm/，LLVM_ENABLE_PROJECTS=clang）
    if 目标架构 ≠ 宿主架构:
        → Cross（指定 CMAKE_TARGET_TRIPLE 或 toolchain 文件）
```

#### 4.1.3 源码精读

开关 `LLVM_LIBC_FULL_BUILD` 在顶层 CMakeLists 中定义，并且对 GPU 目标会**自动默认为 ON**——因为 GPU 环境根本没有「系统 libc」可以覆盖：

[libc/CMakeLists.txt:149-154](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L149-L154) —— GPU 目标把 `default_to_full_build` 置为 `ON`，并作为 `LLVM_LIBC_FULL_BUILD` 选项的默认值。换句话说，**GPU 之下没有 Overlay 可言，它天生就是 Full 模式**。

这个开关一旦确定，会牵动整棵构建树的多处分支。我们挑三处最能体现「Full 比 Overlay 重」的地方：

1. **headers.txt 强制检查**。[libc/CMakeLists.txt:392-396](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L392-L396)：Full 模式下若找不到 `headers.txt` 直接 `FATAL_ERROR`。因为 Full 模式必须自己生成全部公共头文件，而 Overlay 可以借用系统头文件。

2. **startup 目录只在 Full 模式加入**。[libc/CMakeLists.txt:440-444](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L440-L444)：只有 Full 模式（且非 SPIRV 目标）才会 `add_subdirectory(startup)`。Overlay 模式下程序入口 `_start` 由系统 libc 提供，不需要 LLVM-libc 自己造启动对象。

3. **hermetic（自封闭）测试与 Full 绑定**。[libc/CMakeLists.txt:173](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L173)：`LIBC_ENABLE_HERMETIC_TESTS` 直接取 `LLVM_LIBC_FULL_BUILD` 的值。因为 hermetic 测试要求「完全不依赖系统 libc」，只有 Full 模式才做得到。

#### 4.1.4 代码实践

**实践目标**：在没有真正编译的前提下，学会从一份 CMake 命令反推「它属于哪种场景」。

**操作步骤**：阅读 [build_concepts.md:12-66](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L12-L66)，对照下表把五段示例命令归类。

| 命令特征 | 场景 |
| --- | --- |
| `-S runtimes` + `LLVM_LIBC_FULL_BUILD=OFF` | Overlay |
| `-S runtimes` + `LLVM_LIBC_FULL_BUILD=ON` + `compiler-rt` | Full |
| `-S llvm` + `LLVM_ENABLE_PROJECTS="clang"` | Bootstrap |
| 带 `CMAKE_TARGET_TRIPLE` 或 toolchain 文件 | Cross |

**需要观察的现象 / 预期结果**：你会确认「构建根 + FULL_BUILD 开关」这两个信号足以区分绝大多数命令，Bootstrap 与 Cross 只是额外加料。

> 待本地验证：如果你本地有 llvm-project 检出，可分别用 Overlay 与 Full 命令 configure 一次，对比 `build/projects/libc/lib/` 下生成的 `.a` 文件名差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GPU 目标不提供 Overlay 模式？

**参考答案**：GPU（AMDGPU/NVPTX）运行环境里不存在「系统 libc」可以被覆盖，libc 必须自给自足，因此 [CMakeLists.txt:150-152](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L150-L152) 把 `default_to_full_build` 设为 `ON`，强制走 Full 路径。

**练习 2**：一个命令同时出现 `-S llvm` 和 `LLVM_LIBC_FULL_BUILD=ON`，它对应五种场景里的哪一种？

**参考答案**：Bootstrap + Full 的组合（用自举出的 Clang 构建完整 libc）。

---

### 4.2 Overlay 模式：链接顺序覆盖与 ABI 限制

#### 4.2.1 概念说明

Overlay 是「叠加」之意：LLVM-libc 不替换系统 libc，而是**叠加在它之上**。其核心机制在 [overlay_mode.md:5-12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L5-L12) 一句话讲透：

> 链接顺序语义被用来优先选取 `libllvmlibc.a` 中的符号（如果它在那儿有的话），其余符号则取自系统 libc。

也就是说，把 `libllvmlibc.a` 放在系统 `libc.a` **之前**，链接器在解析未定义符号时会先从前者抽取；前者没有的符号（比如 LLVM-libc 尚未实现的函数），才会继续往后找，自然「回退」到系统 libc。用户的程序依旧使用系统 libc 的头文件。

这种机制有一个硬约束：**只有不依赖实现私有 ABI 的函数才能进 `libllvmlibc.a`**。文档点名 `strlen`、`round` 这类「纯算法」函数可以放进去；而 `fopen` 及其一族**不能**放，因为它们依赖 `FILE` 数据结构的实现私有定义。

为什么 `FILE` 是问题？因为 `FILE` 在不同 libc（glibc / musl / LLVM-libc）里是**不同的结构体布局**。`fopen` 返回一个 `FILE *`，如果这个指针指向的是 LLVM-libc 自己的 `FILE` 布局，而调用方随后用 glibc 的 `fread` / `fclose` 去操作它，就会按 glibc 的字段偏移读写——轻则数据错乱，重则崩溃。`strlen` 没有这个问题：它只接收 `const char *`，不依赖任何私有结构。

#### 4.2.2 核心流程

Overlay 链接的伪代码：

```text
clang main.o -lllvmlibc   # ① 链接器先扫 libllvmlibc.a
              ...         # ② 再扫系统 libc（由编译器默认补 -lc）
# 解析 main.o 中的未定义符号 strlen：
#   → libllvmlibc.a 里有 libc.src.string.strlen → 抽出，用 LLVM-libc 版本
# 解析 main.o 中的未定义符号 fopen：
#   → libllvmlibc.a 里【故意不放】 → 继续往后 → 系统 libc 提供 fopen
```

产物命名也刻意「啰嗦」：[overlay_mode.md:16-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L16-L22) 解释了为什么叫 `libllvmlibc.a`（重复的 `lib`）——为了避免和系统 `libc.a` 混淆，并让用户在混用多个 libc 时一眼分辨。

#### 4.2.3 源码精读

**（1）`FILE` 类型的两路来源 —— Overlay 限制的最直接证据。**

[hdr/types/FILE.h:12-20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/FILE.h#L12-L20) 是一个「代理头」，它根据 `LIBC_FULL_BUILD` 宏在两套 `FILE` 定义之间切换：

- Full 模式：`#include "include/llvm-libc-types/FILE.h"` —— 用 **LLVM-libc 自己的** `FILE` 布局。
- Overlay 模式：`#include "hdr/stdio_overlay.h"` —— 最终 include 系统 `<stdio.h>`，用 **系统 libc 的** `FILE` 布局。

这正说明：Overlay 模式下 LLVM-libc 内部代码也必须按系统的 `FILE` 布局来理解这个类型，否则就和系统库对不上。

**（2）stdio 入口点在 Overlay 下的「借用系统 FILE」机制。**

[src/stdio/CMakeLists.txt:26-29](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/stdio/CMakeLists.txt#L26-L29) 在**非 Full 构建**时，给所有 stdio 入口点注入编译宏 `LIBC_COPT_STDIO_USE_SYSTEM_FILE`。这个宏的作用是让 stdio 的内部实现去调用系统 libc 提供的 `FILE` 操作，而不是 LLVM-libc 自己的 `File` 类。这是 LLVM-libc 为「想在 Overlay 下也提供部分 stdio 函数」所开的口子，但它依然受限于系统 `FILE` 的 ABI。

**（3）Overlay 的入口点列表。**

[lib/CMakeLists.txt:4-13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L13) 显示：Overlay 分支（`else()`）只构建一个名为 `llvmlibc` 的归档，其入口点取自 `TARGET_LLVMLIBC_ENTRYPOINTS`。而 [config/linux/x86_64/entrypoints.txt:1627-1631](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L1627-L1631) 表明 `TARGET_LLVMLIBC_ENTRYPOINTS` 是 libc / libm / libmvec 三份列表的并集——也就是说，是否真正 ABI 无关，最终靠的是单个函数实现是否依赖私有结构（如 `FILE`），而不是靠列表本身去筛。

#### 4.2.4 代码实践

**实践目标**：亲手把 Overlay 库链到一个最小程序上。

**操作步骤**：

1. 按 [overlay_mode.md:29-37](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L29-L37) 的命令 configure，再 `ninja libc` 产出 `libllvmlibc.a`（位于 `build/projects/libc/lib/`，见 [overlay_mode.md:51-52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L51-L52)）。
2. 写一个调用了 `strlen`（LLVM-libc 有）和 `printf`（回退系统）的小程序 `demo.c`。
3. 按 [overlay_mode.md:90-94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L90-L94) 的配方链接：`clang demo.c -L<libllvmlibc.a 目录> -lllvmlibc`。

**需要观察的现象**：程序正常编译运行；`strlen` 用的是 LLVM-libc 的实现，`printf` 仍来自系统 libc。可用 `nm demo | grep strlen` 或 `objdump -d` 进一步确认（**待本地验证**具体反汇编结果）。

#### 4.2.5 小练习与答案

**练习 1**：`round`（数学函数）能进 `libllvmlibc.a`，`fopen` 不能。除了 `FILE` 之外，再举一个可能阻碍某函数进入 Overlay 的实现私有类型。

**参考答案**：例如 `DIR`（目录流，对应 `opendir`/`readdir`）也是实现私有结构，不同 libc 布局不同；同理 `struct __sFILE` 之类的内部类型都会构成 ABI 依赖。

**练习 2**：为什么 `libllvmlibc.a` 这个名字要带重复的 `lib`？

**参考答案**：见 [overlay_mode.md:16-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L16-L22)：为了避免和系统 `libc.a` 混淆，并在用户混用多个 libc 时明确标识「这是 LLVM 的 libc 静态归档」。

---

### 4.3 Full 模式：独立 libc 替换品

#### 4.3.1 概念说明

Full 模式把 LLVM-libc 当成**唯一的 libc**：不再叠加在系统库上，而是完全取而代之。它的典型用途是：

- 为一个**新操作系统**提供完整的 C 库；
- 为某个目标生成 **sysroot**；
- 在 **GPU / baremetal** 等没有系统 libc 的环境里独立运行。

与 Overlay 相比，Full 模式有三点本质区别：

1. **产物不同**：Full 产出 `libc.a`、`libm.a`（数学库单独成档）、必要时还有 `libmvec.a` 和启动对象 `crt1.o`；Overlay 只产出 `libllvmlibc.a`。
2. **必须自带头文件**：不能再借用系统头文件，所以 [CMakeLists.txt:392-396](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L392-L396) 在 Full 下强制要求 `headers.txt`。
3. **必须自带启动代码**：Full 下 [CMakeLists.txt:440-444](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L440-L444) 才会构建 `startup/` 目录，生成 `_start` 等入口。

#### 4.3.2 核心流程

Full 模式链接一个程序时，**全程不碰系统 libc**：

```text
clang -nostdinc -nostdlib main.o \
      crt1.o          # ① LLVM-libc 提供的程序入口 _start
      libc.a libm.a   # ② 全部符号来自 LLVM-libc
      <clang resource dir>  # ③ 仅借用编译器自带的头文件（如 stdint.h）
```

`-nostdinc` / `-nostdlib` 屏蔽掉系统的头文件与库，随后手工补上启动对象、`libc.a`、`libm.a`。这正是 [u1-l3](u1-l3-build-and-run.md) Hello World 的链接原理，也是 Full 模式的灵魂。

#### 4.3.3 源码精读

**（1）Full 模式组装三份归档。**

[lib/CMakeLists.txt:4-13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L13) 的 Full 分支：归档名是 `c` / `m` / `mvec`（加上 `lib` 前缀和 `.a` 后缀即 `libc.a` / `libm.a` / `libmvec.a`），分别由 `TARGET_LIBC_ENTRYPOINTS`、`TARGET_LIBM_ENTRYPOINTS`、`TARGET_LIBMVEC_ENTRYPOINTS` 三份列表填充。对比 Overlay 分支只有一个 `llvmlibc`，差异一目了然。

**（2）Full 归档额外依赖头文件与 startup。**

[lib/CMakeLists.txt:23-38](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L23-L38)：每个 Full 归档都 `target_link_libraries(... PUBLIC libc-headers)`（把生成的公共头作为使用要求），并在存在 `libc-startup` 目标时 `add_dependencies`。Overlay 分支则没有这两行——因为它既不负责头文件，也不负责启动。

**（3）Linux 下还造一个空的 `libpthread.a`。**

[lib/CMakeLists.txt:76-94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L76-L94)：Full + Linux 时用 `${CMAKE_AR} cqs` 造一个**空**的 `libpthread.a`。原因是很多老式构建脚本仍会 `-lpthread`，而 LLVM-libc 把线程功能直接做进了 `libc.a`，于是用一个空归档来「骗过」这类链接命令，避免找不到 `-lpthread` 报错。

**（4）startup 的安装也受 Full 门控。**

[lib/CMakeLists.txt:97-107](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L97-L107)：`install-libc` 这一总安装目标的依赖里，`startup_target`（`libc-startup`）与 `header_install_target` 都只在 Full 模式下被加入。这从安装层面再次确认「startup 与公共头是 Full 独有的职责」。

#### 4.3.4 代码实践

**实践目标**：从构建产物层面直观感受 Full 与 Overlay 的差异。

**操作步骤**：

1. 用 [build_concepts.md:37-41](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L37-L41) 的 Full 命令 configure（注意带上 `compiler-rt`，用于接入 Scudo 分配器）。
2. `ninja libc` 后，到 `build/projects/libc/lib/` 列出文件。
3. 对比一次 Overlay 构建（`LLVM_LIBC_FULL_BUILD=OFF`）在同一目录下的产物。

**需要观察的现象 / 预期结果**：Full 构建下应能看到 `libc.a`、`libm.a`，以及（Linux 上）空的 `libpthread.a`；Overlay 构建下只有一个 `libllvmlibc.a`。同时 Full 构建产物附近还应出现 startup 相关对象（如合并出的 `crt1.o`）。具体文件清单**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Full 模式下为什么要把 `libm` 单独成一个归档，而不是和 `libc` 合并？

**参考答案**：这是为了与 POSIX/传统约定对齐——历史上数学库 `libm.a` 独立于 `libc.a`，许多现有链接命令仍显式 `-lm`。Full 模式作为「完整 libc 替换品」，需要复现这套产物布局，[lib/CMakeLists.txt:4-13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L13) 中 `m` 与 `c` 分列正是为此。

**练习 2**：为什么 Overlay 模式下不需要 `crt1.o`？

**参考答案**：Overlay 下程序入口 `_start`、TLS 初始化等都由**系统 libc** 的启动对象提供，LLVM-libc 只覆盖少数函数符号。[CMakeLists.txt:440-444](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L440-L444) 只在 Full 下 `add_subdirectory(startup)`，因此 Overlay 不产 `crt1.o`。

---

### 4.4 Bootstrap 与交叉编译

#### 4.4.1 概念说明

前两种场景（Overlay / Full）描述「产出什么」，Bootstrap 与 Cross 描述「怎么产出」。它们可以和 Full/Overlay 自由组合，但官方文档给出的典型组合是：

- **Bootstrap Build**：构建根改为 `llvm/`（而不是 `runtimes/`），先编译 Clang 等项目，再用这个**新编译出的 Clang** 去构建 libc。目的是得到「编译器与运行时库版本一致」的工具链，让 libc 用上 Clang 的最新特性（理论上性能最好）。见 [build_concepts.md:43-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L43-L54)。

- **Cross-compiler Build**：为目标架构构建 libc（在 x86_64 宿主上为 aarch64 等），需要交叉编译器或 CMake toolchain 文件。见 [build_concepts.md:56-60](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L56-L60)。

- **Bootstrap Cross-compiler**：从零开始（可能只有内核头文件），为目标既造出编译器、又造出 sysroot。这是「搭一个全新环境」的常见路径。见 [build_concepts.md:62-66](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L62-L66)。

#### 4.4.2 核心流程

```text
# Overlay + 独立运行时（最轻量贡献者流程）
cmake -S runtimes -B build -DLLVM_ENABLE_RUNTIMES="libc" \
      -DLLVM_LIBC_FULL_BUILD=OFF ...

# Full + compiler-rt（独立 libc）
cmake -S runtimes -B build -DLLVM_ENABLE_RUNTIMES="libc;compiler-rt" \
      -DLLVM_LIBC_FULL_BUILD=ON ...

# Bootstrap（先造 Clang，再造 libc；构建根是 llvm/）
cmake -S llvm -B build -DLLVM_ENABLE_PROJECTS="clang" \
      -DLLVM_ENABLE_RUNTIMES="libc;compiler-rt" ...

# Cross（指定目标三元组或 toolchain 文件）
cmake -S runtimes -B build ... -DCMAKE_TARGET_TRIPLE=aarch64-linux-gnu \
      -DCMAKE_C_COMPILER=<交叉 clang> ...
```

注意 Bootstrap 与非 Bootstrap 的关键差别：构建根从 `runtimes/` 换成 `llvm/`，并把 `clang` 放进 `LLVM_ENABLE_PROJECTS`，而 libc 仍作为 runtime（不是 project）出现——见 [overlay_mode.md:69-74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L69-L74)。

#### 4.4.3 源码精读

[overlay_mode.md:60-66](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L60-L66) 把 Bootstrap 的动机说得很直白：它产出「ToT（top-of-tree）Clang + 运行时库」相互同步的工具链，确保 LLVM-libc 能用到最新 Clang 特性，从而获得最佳性能。代价是构建时间显著变长（要先把 Clang 编出来）。

交叉编译层面，libc 顶层 CMake 通过 `LIBC_TARGET_TRIPLE`（[CMakeLists.txt:145](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L145)）与 `LLVMLibCArchitectures` 模块（[CMakeLists.txt:146](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L146)）识别目标 OS 与架构，进而挑选 `config/<os>/<arch>/` 下的平台配置。这也是为什么交叉编译时必须正确传入目标三元组——它决定了 libc 加载哪一份 `entrypoints.txt` / `headers.txt`。

#### 4.4.4 代码实践

**实践目标**：把「Bootstrap + Full」组合的命令写对。

**操作步骤**：基于 [build_concepts.md:50-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L50-L54) 与 [overlay_mode.md:69-74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L69-L74)，拼出一条命令：构建根 `llvm/`、启用 `clang` 项目、libc 与 compiler-rt 作为 runtime、并显式 `LLVM_LIBC_FULL_BUILD=ON`。

**需要观察的现象 / 预期结果**：configure 阶段会先规划构建 Clang，再构建 libc；`ninja libc` 耗时远长于纯 Overlay。具体耗时**待本地验证**。

> 说明：Bootstrap 全流程编译耗时很长（几十分钟到数小时），不建议在配置一般的机器上无目的尝试；理解命令含义即可。

#### 4.4.5 小练习与答案

**练习 1**：Bootstrap 构建里，libc 是 `LLVM_ENABLE_PROJECTS` 还是 `LLVM_ENABLE_RUNTIMES`？为什么？

**参考答案**：是 **runtimes**。见 [overlay_mode.md:69-74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L69-L74)：注释明确「libc is listed as runtime and not as a project」。因为 runtime 会在「新 Clang 编出来之后」再用新 Clang 构建，这正是 Bootstrap 想要的；若放进 projects，它会和 Clang 一起被宿主编译器构建，失去自举意义。

**练习 2**：交叉编译时，目标三元组主要通过什么机制影响 libc 的行为？

**参考答案**：通过 [CMakeLists.txt:145-146](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L145-L146) 的 `LIBC_TARGET_TRIPLE` 与 `LLVMLibCArchitectures` 模块，解析出目标 OS/架构，从而加载 `config/<os>/<arch>/` 下对应的 `entrypoints.txt` / `headers.txt` / `config.json`，决定该平台支持哪些函数、生成哪些头文件。

---

## 5. 综合实践

本实践把本讲三块内容（Overlay、Full、Bootstrap/Cross）串起来，作为本讲的收尾任务。

### 任务

1. **写出 Overlay 模式的最小 CMake 配置命令**（要求：构建根 `runtimes/`、只启用 libc、显式 `LLVM_LIBC_FULL_BUILD=OFF`），并指出它会产出哪个静态归档、位于构建树的哪个子目录。

2. **写出 Full 模式的最小 CMake 配置命令**（要求：构建根 `runtimes/`、启用 libc 与 compiler-rt、`LLVM_LIBC_FULL_BUILD=ON`），并列出它会比 Overlay 多产出哪些产物（至少举两类）。

3. **解释关键问题**：在 Overlay 模式下，为什么 `fopen` 这类函数不能放进 `libllvmlibc.a`？请结合本讲引用的真实源码作答。

### 参考答案要点

**第 1 题**：参见 [build_concepts.md:25-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L25-L28) 与 [overlay_mode.md:29-37](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L29-L37)：

```sh
cmake -S runtimes -B build -G Ninja \
      -DLLVM_ENABLE_RUNTIMES="libc" \
      -DLLVM_LIBC_FULL_BUILD=OFF \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
```

产出 `libllvmlibc.a`，位于 `build/projects/libc/lib/`（见 [overlay_mode.md:51-52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L51-L52)）。

**第 2 题**：参见 [build_concepts.md:37-41](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/build_concepts.md#L37-L41)：

```sh
cmake -S runtimes -B build -G Ninja \
      -DLLVM_ENABLE_RUNTIMES="libc;compiler-rt" \
      -DLLVM_LIBC_FULL_BUILD=ON \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
```

比 Overlay 多产出：① 独立的 `libc.a` 与 `libm.a`（[lib/CMakeLists.txt:4-13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L13)）；② 启动对象（`startup/` 仅在 Full 加入，见 [CMakeLists.txt:440-444](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L440-L444)）；③ Linux 下额外的空 `libpthread.a`（[lib/CMakeLists.txt:76-94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L76-L94)）；④ 自生成的公共头文件（Full 强制要求 `headers.txt`，见 [CMakeLists.txt:392-396](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L392-L396)）。

**第 3 题**：`fopen` 依赖 `FILE` 这一**实现私有 ABI**。证据见 [hdr/types/FILE.h:12-20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/FILE.h#L12-L20)：Full 模式用 LLVM-libc 自己的 `FILE` 布局，Overlay 模式回退到系统 `<stdio.h>` 的 `FILE`。如果 `fopen` 进入 `libllvmlibc.a` 并被链接进程序，它返回的 `FILE *` 指向的是某种固定布局；而程序其余部分（如系统的 `fread`/`fclose`）按 glibc 的 `FILE` 布局去解引用，就会因 ABI 不兼容而错乱或崩溃。`strlen`、`round` 这类不依赖私有结构的纯算法函数没有这个问题，所以可以安全覆盖（见 [overlay_mode.md:5-12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/overlay_mode.md#L5-L12)）。即便 Overlay 下 stdio 通过 [src/stdio/CMakeLists.txt:26-29](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/stdio/CMakeLists.txt#L26-L29) 的 `LIBC_COPT_STDIO_USE_SYSTEM_FILE` 借用系统 `FILE`，它也始终受限于系统 `FILE` 的 ABI，无法像纯算法函数那样独立覆盖。

## 6. 本讲小结

- LLVM-libc 的构建可归为 **五种场景**，但本质只沿「构建根（`runtimes/` vs `llvm/`）」与「`LLVM_LIBC_FULL_BUILD` 开关」两个维度变化。
- **Overlay 模式** 利用静态库链接顺序，用 `libllvmlibc.a` 覆盖系统 libc 中的少数符号，其余回退；只放**不依赖实现私有 ABI** 的函数（如 `strlen`、`round`）。
- **Full 模式** 是完整 libc 替换品，产出 `libc.a` / `libm.a` / 启动对象 / 自生成头文件，强制要求 `headers.txt`，并额外造空 `libpthread.a` 兼容老链接命令。
- `fopen` 不能进 Overlay 的根因是 `FILE` 结构的 ABI 不兼容，证据见代理头 [hdr/types/FILE.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/types/FILE.h) 的两路切换。
- **Bootstrap** 把构建根换成 `llvm/` 并先编 Clang，得到版本同步的工具链；**Cross** 通过目标三元组切换 `config/<os>/<arch>/` 配置；二者可与 Full/Overlay 组合。
- GPU 目标没有「系统 libc」可覆盖，[CMakeLists.txt:150-152](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L150-L152) 强制其默认走 Full 模式。

## 7. 下一步学习建议

- 想搞清楚「Full 模式自生成头文件」到底怎么从 YAML 变成 `.h`？进入 **u3-l1 头文件生成管线**。
- 想理解 Full 模式下 `crt1.o` / `do_start` 如何把控制权从内核交到 `main`？进入 **u8-l2 程序启动流程**。
- 想了解 Overlay 与 Full 在「代理头」上的完整切换机制（不止 `FILE`）？进入 **u3-l2 代理头文件、公共宏与类型**。
- 想动手贡献一个新函数并搞清它该进哪份 `entrypoints.txt`？进入 **u2-l4 平台配置体系** 与 **u11-l3 贡献一个完整新函数**。
