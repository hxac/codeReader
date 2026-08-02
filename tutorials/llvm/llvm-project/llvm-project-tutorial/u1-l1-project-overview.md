# 项目定位与 monorepo 架构

## 1. 本讲目标

本讲是整套 LLVM 学习手册的第一讲，目标是帮你建立对 LLVM 项目的「全局地图」。读完本讲后，你应该能够：

- 说出 **LLVM 到底是什么**：它不是某一个编译器，而是一套用来**构建**编译器的「基础设施」。
- 理解 **三段式编译器**（前端 → 中间表示 → 后端）的理念，以及 LLVM 在其中扮演的角色。
- 说出这个仓库里 **llvm / clang / mlir / lld / lldb / flang / compiler-rt / libcxx** 等顶层目录各自负责什么。
- 解释为什么 LLVM 采用 **单仓库（monorepo）** 的组织方式，以及 CMake 是如何通过 `LLVM_ENABLE_PROJECTS` 选择构建哪些子项目的。

本讲不要求你写过编译器，也不要求你懂 C++。我们只读两个关键文件：仓库根目录的 `README.md` 和 `llvm/CMakeLists.txt`。

## 2. 前置知识

在读源码之前，先用最朴素的方式建立几个直觉。

### 2.1 什么是「编译器」

编译器是一个**翻译程序**：它把人类写的源代码（如 `.c`、`.cpp`、`.f90`）翻译成机器能执行的目标代码（如 `.o` 或可执行文件）。一个完整的编译器通常包含三件事：

1. **理解源码**（前端）：把文本切分成词、按语法组织成树（AST）。
2. **优化**（中端）：在不改变程序语义的前提下，让程序跑得更快、占用更小。
3. **生成目标代码**（后端）：把优化后的程序翻译成某一类 CPU（如 x86、ARM、RISC-V）能识别的指令。

### 2.2 三段式理念

传统的做法是为每种语言 × 每种平台单独写一个编译器（\(M\) 种语言 × \(N\) 种平台就要 \(M \times N\) 套实现）。三段式架构的核心思想是引入一个**中间表示（Intermediate Representation，IR）**：

\[
\text{源码} \xrightarrow{\text{前端}} \text{IR} \xrightarrow{\text{后端}} \text{机器码}
\]

这样，\(M\) 种语言各自只需要一个前端（产出同一种 IR），\(N\) 种平台各自只需要一个后端（消费同一种 IR）。工作量从 \(M \times N\) 降为 \(M + N\)。而**优化**只需对这一种 IR 做一遍，所有语言、所有平台都能复用。

**LLVM 的定位就在这里**：它提供了一套统一的 IR、一套围绕 IR 的优化器和代码生成器，以及一整套可复用的库，让别人能够方便地「拼装」出自己的编译器。

### 2.3 什么是 monorepo（单仓库）

`monorepo` = `mono`（单一） + `repository`（仓库）。意思是把**多个相互关联的项目放在同一个 Git 仓库里**管理，而不是每个项目一个仓库。

LLVM 的几十个组件（核心、前端、链接器、调试器、各种运行时库）共享同一套构建规则、同一份版本号、同一次代码评审流程，因此它们被打包在同一个仓库 `llvm/llvm-project` 中。

> 名词速查：
> - **IR / 中间表示**：编译器内部用来表示程序的「通用语言」，是前后端之间的桥梁。
> - **bitcode（位码）**：LLVM IR 的二进制存储形式，比文本形式更紧凑。
> - **目标平台（target）**：程序最终运行的平台，如 `X86`、`AArch64`、`RISCV`。
> - **运行时库（runtime）**：程序运行期间依赖的库，例如 C++ 标准库、sanitizer 支持库。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是理解整个仓库的「入口」：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md) | 仓库的「自我介绍」，用最简短的话说明 LLVM 是什么、核心组件有哪些、如何获取与构建。 |
| [llvm/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt) | LLVM 核心子项目的 CMake 构建脚本，**也是整个 monorepo 的「目录清单」**：它定义了有哪些子项目、如何选择构建哪些子项目。 |

此外，本讲会引用顶层目录结构本身（你可以用 `ls` 看到的那些文件夹），它们才是子项目划分的「实物证据」。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 项目概述**：LLVM 是什么、为什么是「基础设施」。
- **4.2 monorepo 子项目划分**：仓库里有哪些子项目、它们各自做什么、CMake 如何管理它们。

---

### 4.1 项目概述

#### 4.1.1 概念说明

很多人第一次接触 LLVM，会以为它「就是 clang」或者「就是那个 C++ 编译器」。这是一种误解。

准确地说，**LLVM 是一套「构建编译器」的工具链与可重用库集合**。它的核心是一个名为 `llvm` 的子项目，里面包含了：

- 一套**中间表示（LLVM IR）**及其在内存中的数据结构。
- 一套**优化器**（对 IR 做各种优化变换）。
- 一套**代码生成器**（把 IR 翻译成各种目标平台的机器码）。
- 一系列**命令行工具**（汇编器、反汇编器、位码分析器、位码优化器等）。

围绕这个核心，LLVM 项目还托管了多个**前端**（最著名的是 C/C++ 前端 **clang**）、**链接器（lld）**、**调试器（lldb）**、**运行时库（compiler-rt、libcxx 等）**。这些组件共同构成了一条「从源码到可执行程序」的完整工具链。

正因为 LLVM 把「编译器」拆成了可独立复用的库，**你甚至可以在自己的程序里嵌入 LLVM 的某些能力**（比如只调用它的 IR 优化器，或只调用它的 JIT 执行引擎），而不必关心整个编译流程。

#### 4.1.2 核心流程

如果只看「C/C++ 程序从源码到可执行文件」这条最经典的链路，数据是这样流动的：

```text
            ┌──────────┐    IR    ┌────────────────────────┐  机器码  ┌──────────┐
 C/C++ 源码 │  clang   │ ───────▶ │  LLVM 核心(优化+后端)   │ ───────▶│ 目标文件 │
            │  (前端)  │          │                        │         └──────────┘
            └──────────┘          └────────────────────────┘
                                    lib/IR  lib/Transforms  lib/CodeGen ...
```

要点：

1. **前端**（clang）负责把源码翻译成 **LLVM IR**。
2. **LLVM 核心**拿到 IR 后，先做一系列**优化**（`lib/Transforms`），再做**代码生成**（`lib/CodeGen`）产出机器码。
3. IR 是这条链路的**枢纽**：只要能产出 IR，任何语言都能复用 LLVM 的优化与后端；只要能消费 IR，任何平台都能复用任意前端。

> 三段式带来的好处可以用一句话概括：**前后端解耦，优化可复用**。这正是 LLVM 被称为「基础设施」而非「某个编译器」的原因。这一理念会在 [u2-l1（三段式编译器设计与 IR 的角色）](u2-l1-three-phase-design.md) 中详细展开。

#### 4.1.3 源码精读

我们打开仓库根目录的 `README.md`，它用三段话精准概括了上面所说的一切。

第一段，给 LLVM 下定义——注意「toolkit（工具包 / 基础设施）」这个词：

[README.md:9-11](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L9-L11) — 这段明确写道：仓库里包含的是 LLVM 的源码，它是一个**用于构建高度优化的编译器、优化器和运行时环境**的工具包。这一句就是 LLVM 的「官方定位」。

第二段，介绍**核心子项目（也叫 LLVM 本体）**包含什么：

[README.md:13-17](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L13-L17) — 这里说：核心项目本身就叫 LLVM，它包含处理中间表示、把中间表示转换成目标文件所需的**全部工具、库和头文件**，工具包括汇编器（assembler）、反汇编器（disassembler）、位码分析器（bitcode analyzer）和位码优化器（bitcode optimizer）。这些工具对应的命令分别是 `llvm-mc`、`llvm-objdump`、`llvm-bcanalyzer`、`opt` 等（详见 [u1-l4 核心命令行工具一览](u1-l4-core-tools.md)）。

第三段，说明类 C 语言使用 **Clang 前端**，并指出 Clang 的产出物就是 IR：

[README.md:19-22](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L19-L22) — 这一段告诉我们：C 类语言（C、C++、Objective-C、Objective-C++）使用 Clang 前端，Clang 把它们编译成 **LLVM bitcode（IR 的二进制形式）**，再由 LLVM 转换成目标文件。这正是 4.1.2 流程图里「前端产出 IR」的具体来源。

最后一句还点到了 monorepo 里其他组件的存在：

[README.md:24-25](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L24-L25) — 其他组件还包括 libc++ C++ 标准库、LLD 链接器等等。这里的「等等」背后藏着几十个子项目，正是 4.2 节要展开的内容。

#### 4.1.4 代码实践

**实践目标**：用一句话向自己复述「LLVM 是什么」，并把它和仓库根目录的 `README.md` 对应起来。

**操作步骤**：

1. 用编辑器或 `cat` 打开仓库根目录的 `README.md`。
2. 找到 4.1.3 引用的三段文字（第 9–25 行附近）。
3. 在自己的笔记里填空：
   - LLVM 是一个 \_\_\_\_\_\_（提示：toolkit / 工具包），用于构建 \_\_\_\_\_\_、\_\_\_\_\_\_ 和 \_\_\_\_\_\_。
   - 核心子项目（也叫 LLVM 本体）包含处理 \_\_\_\_\_\_ 和把它转换成 \_\_\_\_\_\_ 所需的工具、库和头文件。
   - C 类语言使用的前端叫 \_\_\_\_\_\_，它把源码编译成 \_\_\_\_\_\_。

**需要观察的现象**：你会发现 `README.md` 全文非常短（只有四十多行），它**没有**列举所有子项目，而是把细节留给了 `docs/` 和构建脚本。这说明 README 的定位是「入口的入口」。

**预期结果**：你能用自己的话复述「LLVM 是一套编译器基础设施，核心是 IR + 优化 + 代码生成，外加 clang/lld 等配套组件」。

> 说明：本实践是纯阅读型实践，不需要运行任何命令，任何能查看文本文件的环境都能完成。

#### 4.1.5 小练习与答案

**练习 1**：有人说「LLVM 就是一个 C++ 编译器」，这句话哪里不对？

> **参考答案**：LLVM 本身不是某一种语言的编译器，而是一套**可复用的编译器基础设施**。C++ 的编译是由前端 **clang** 完成「源码 → IR」，再交给 LLVM 核心完成「IR → 机器码」。LLVM 还服务于 Rust、Swift 等许多其他语言的前端。

**练习 2**：在 README 提到的核心工具里，「bitcode optimizer」对应哪个我们后续会经常用到的命令？

> **参考答案**：对应 `opt`。它是 LLVM 最重要的优化驱动工具，后续讲义（如 [u1-l4](u1-l4-core-tools.md)、[u4-l1 新 Pass 管理器架构](u4-l1-new-pass-manager.md)）会反复用到它。

---

### 4.2 monorepo 子项目划分

#### 4.2.1 概念说明

如果你在仓库根目录执行 `ls`，会看到几十个并列的文件夹：`llvm`、`clang`、`mlir`、`lld`、`lldb`、`flang`、`compiler-rt`、`libcxx`……这就是 **monorepo** 的直观体现——所有相关子项目**平级地住在同一个仓库里**。

为什么 LLVM 要这样做？核心原因有三：

1. **共享版本与构建规则**：所有组件同步发版、共用同一套 CMake 模块（顶层 `cmake/` 目录），避免了「A 升级了，B 还在用旧版」的混乱。
2. **原子化跨项目改动**：当一个改动同时涉及前端（clang）和核心（llvm）时，可以在**同一次提交**里完成，CI 一次性验证，不会出现中间态的不兼容。
3. **简化依赖**：子项目之间互相引用（例如 flang 依赖 mlir，lldb 依赖 clang）时，源码就在隔壁目录，构建系统可以精确处理依赖关系。

为了管理这些子项目，LLVM 在 CMake 层面区分了两类组件：

- **projects（项目）**：编译时被编进 LLVM 工具链的组件，如 `clang`、`lld`、`mlir`。通过变量 **`LLVM_ENABLE_PROJECTS`** 选择。
- **runtimes（运行时）**：程序**运行**时才需要的库（如 `libcxx`、`compiler-rt`、`openmp`），它们用目标平台自己的编译器来构建，因此单独通过变量 **`LLVM_ENABLE_RUNTIMES`** 管理。

这个区分非常重要，是理解 monorepo 的关键。

#### 4.2.2 核心流程

构建 LLVM 时，选择子项目的流程是这样的（伪代码）：

```text
用户运行 cmake，传入 LLVM_ENABLE_PROJECTS="clang;lld"
    │
    ▼
CMake 读取 LLVM_KNOWN_PROJECTS 清单（这是仓库里「合法子项目」的白名单）
    │
    ▼
对清单里每一个项目：
    │   ├─ 用户启用了它？
    │   │     ├─ 是 → 检查目录 ../<项目名> 是否存在 → 存在则加入构建
    │   │     └─ 否 → 跳过
    │
    ▼
对运行时（LLVM_ENABLE_RUNTIMES）走类似的「白名单 + 目录检查」流程
    │
    ▼
最终生成构建系统（如 build.ninja），只编译被选中的子项目
```

关键设计：**子项目的目录名就是它的项目名**。CMake 会去相对路径 `../<项目名>` 找源码（`../` 是因为脚本位于 `llvm/` 子目录内）。这就是为什么所有子项目必须平级地放在仓库根目录。

下面这张表列出了 monorepo 里最主要的子项目及其职责（你可以用 `ls` 在仓库根目录逐一对应）：

| 子项目（目录名） | 类型 | 职责一句话 |
| --- | --- | --- |
| `llvm` | 核心 | IR、优化器、代码生成器、各类命令行工具（`opt`/`llc`/`llvm-as`/`lli` 等） |
| `clang` | 前端 | C / C++ / Objective-C / Objective-C++ 前端，产出 LLVM IR |
| `clang-tools-extra` | 工具 | 基于 clang 的额外工具，如 `clang-tidy`、`clangd` |
| `flang` | 前端 | Fortran 前端（依赖 mlir、clang） |
| `mlir` | 中端框架 | 多级中间表示，可扩展的 IR 框架 |
| `lld` | 链接器 | 高性能链接器，支持 ELF / COFF / Mach-O / Wasm |
| `lldb` | 调试器 | 源码级调试器（依赖 clang） |
| `polly` | 优化 | 多面体（polyhedral）循环优化 |
| `bolt` | 优化 | 链接后二进制优化工具 |
| `compiler-rt` | 运行时 | 编译器运行时：sanitizer、内置函数、profile 等 |
| `libcxx`（libc++） | 运行时 | C++ 标准库实现 |
| `libcxxabi` | 运行时 | C++ ABI 运行时库 |
| `libunwind` | 运行时 | 异常处理用的栈展开库 |
| `libc` | 运行时 | LLVM 版的 C 库 |
| `openmp` | 运行时 | OpenMP 并行运行时 |
| `cross-project-tests` | 测试 | 跨多个子项目的集成测试 |

> 小提示：表里「核心 / 前端 / 链接器 / 调试器」属于构建期组件，对应 `LLVM_ENABLE_PROJECTS`；「运行时」类（`compiler-rt`、`libcxx` 等）对应 `LLVM_ENABLE_RUNTIMES`。后续讲义会分别深入这些组件。

#### 4.2.3 源码精读

现在打开 `llvm/CMakeLists.txt`，看看 CMake 是如何把上面的设计**落实成代码**的。这正是 monorepo 的「权威目录清单」。

**① 白名单：仓库里到底有哪些「项目」**

[llvm/CMakeLists.txt:137-148](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L137-L148) — 这一段定义了三件事：

- 第 138 行：`LLVM_ALL_PROJECTS` 列出了当用户写 `LLVM_ENABLE_PROJECTS=all` 时会启用的项目：`bolt;clang;clang-tools-extra;cross-project-tests;lld;lldb;mlir;polly`。
- 第 144 行：`LLVM_EXTRA_PROJECTS` 列出了**额外**的、还没纳入「all」的项目：`flang`、`libc`、`compiler-rt`（注释解释了原因，例如 flang 有更高的 C++ 要求）。
- 第 145–146 行：把两者拼成 **`LLVM_KNOWN_PROJECTS`**——这就是「仓库里所有合法子项目」的总清单。
- 第 147–148 行：定义了缓存变量 **`LLVM_ENABLE_PROJECTS`**，让用户在命令行用 `-DLLVM_ENABLE_PROJECTS=...` 选择要构建哪些。

**② 白名单校验：防止拼写错误**

[llvm/CMakeLists.txt:163-167](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L163-L167) — 这段 `foreach` 遍历用户传入的每一个项目名，如果它既不是 `llvm`、也不在 `LLVM_KNOWN_PROJECTS` 清单里，就直接报致命错误，并提示「你是不是想用 `LLVM_ENABLE_RUNTIMES` 来启用它？」——这条提示正是「项目 vs 运行时」区分的体现。常见的踩坑就是误把 `compiler-rt` 当作 project 传入。

**③ 运行时清单：另一类组件**

[llvm/CMakeLists.txt:173-175](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L173-L175) — 这里定义了默认运行时 `LLVM_DEFAULT_RUNTIMES`（`libcxx;libcxxabi;libunwind;libclc;compiler-rt;openmp`）和全部受支持的运行时 `LLVM_SUPPORTED_RUNTIMES`。注意它们和项目清单是**分开维护**的，因为运行时要用目标编译器构建，流程不同。

**④ 真正的「启用」逻辑：目录即项目**

[llvm/CMakeLists.txt:295-306](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L295-L306) — 这是 monorepo 机制的**核心代码**。对每个已知项目：

- 第 298 行：用相对路径 `${CMAKE_CURRENT_SOURCE_DIR}/../${proj}`（即仓库根目录下的 `<项目名>`）定位源码目录。
- 第 299–301 行：如果该目录**不存在**，直接报致命错误。这就是「目录名 = 项目名」这一约定的强制保障。
- 第 302–306 行：把目录路径记录到 `LLVM_EXTERNAL_<项目名>_SOURCE_DIR` 变量，供后续构建逻辑使用。

读到这里你就明白了：**monorepo 不只是一个组织习惯，而是被 CMake 用 `../<项目名>` 这种相对路径写死在构建逻辑里的**。子项目必须在仓库根目录下平级存在，否则构建就会失败。

#### 4.2.4 代码实践

**实践目标**：亲手核对 monorepo 的子项目划分，验证「目录名 = 项目名」这条规则，并理解 `LLVM_ENABLE_PROJECTS` / `LLVM_ENABLE_RUNTIMES` 的区分。

**操作步骤**：

1. 在仓库根目录列出顶层条目：
   ```bash
   ls -1
   ```
2. 把输出和 4.2.3 里的 `LLVM_ALL_PROJECTS`（`bolt clang clang-tools-extra cross-project-tests lld lldb mlir polly`）逐个比对，确认每个项目名都能对应到一个**真实存在的顶层目录**。
3. 再比对 `LLVM_EXTRA_PROJECTS`（`flang libc compiler-rt`）和 `LLVM_DEFAULT_RUNTIMES`（`libcxx libcxxabi libunwind libclc compiler-rt openmp`），同样确认对应目录存在。
4. 准备一张纸（或文本文件），画一张简单的「依赖关系图」，例如：
   ```text
                 ┌──── lld (链接)
   llvm (核心) ──┼──── lldb (调试, 依赖 clang)
                 │
                 └──── flang (依赖 mlir, clang)

   clang ──产出 IR──▶ llvm

   运行时: compiler-rt / libcxx / ...  (程序运行时依赖)
   ```
   并用一句话标注每个项目职责（可参考 4.2.2 的表格）。

**需要观察的现象**：

- `ls` 的输出里，**每一个 `LLVM_KNOWN_PROJECTS` 名字都对应一个顶层目录**，例如 `clang`、`lld`、`mlir`。
- 仓库里还有一些目录（如 `runtimes/`、`cmake/`、`utils/`、`third-party/`）**不在**项目清单里——它们是构建基础设施，不是被构建的「子项目」。
- `compiler-rt` 既出现在顶层目录、又出现在运行时清单里，这印证了它应当用 `LLVM_ENABLE_RUNTIMES` 而非 `LLVM_ENABLE_PROJECTS` 启用。

**预期结果**：你得到一张与真实目录一一对应的子项目关系图，并理解了「为什么 CMake 能用 `../<项目名>` 找到每个子项目」。

> 待本地验证：第 2、3 步的「目录存在性」核对在你本地的仓库副本中执行即可；不同版本可能新增或移除少量子项目，以你本地的 `llvm/CMakeLists.txt` 清单为准。

#### 4.2.5 小练习与答案

**练习 1**：如果有人在命令行写 `-DLLVM_ENABLE_PROJECTS=compiler-rt`，会发生什么？为什么？

> **参考答案**：在当前版本的 `llvm/CMakeLists.txt` 中，`compiler-rt` 作为 project 启用已被标记为 **deprecated（弃用）**，会打印一条警告，提示改用 `-DLLVM_ENABLE_RUNTIMES=compiler-rt`（参见 [llvm/CMakeLists.txt:218-222](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L218-L222)）。原因是运行时库需要用目标平台的编译器来构建，应当走 runtimes 流程而非 projects 流程。

**练习 2**：为什么子项目必须平级地放在仓库根目录，而不能放在各自独立的仓库里？

> **参考答案**：因为 `llvm/CMakeLists.txt` 用相对路径 `../<项目名>` 来定位每个子项目源码（见 4.2.3 ④ 的 298 行），如果目录不存在就会构建失败。monorepo 让所有子项目天然满足这一路径约定，同时带来统一版本、原子化跨项目改动、简化依赖等好处（见 4.2.1）。

**练习 3**：`LLVM_ALL_PROJECTS` 和 `LLVM_KNOWN_PROJECTS` 有什么区别？

> **参考答案**：`LLVM_ALL_PROJECTS` 是当用户写 `LLVM_ENABLE_PROJECTS=all` 时会被启用的项目集合；`LLVM_KNOWN_PROJECTS = LLVM_ALL_PROJECTS + LLVM_EXTRA_PROJECTS`，是仓库里**所有合法子项目**的总清单（包括尚未纳入「all」的 flang、libc、compiler-rt）。`LLVM_ENABLE_PROJECTS` 的取值必须在 `LLVM_KNOWN_PROJECTS` 范围内。

---

## 5. 综合实践

把本讲两个模块串起来，完成下面这个贯穿性小任务。

**任务**：为这个 monorepo 制作一张「一页速查表」（一个 Markdown 或纯文本文件即可），需要包含：

1. **顶部一句话定位**：用你自己的话写「LLVM 是什么」（参考 4.1）。
2. **三段式流程图**：画出「源码 →（clang）→ IR →（LLVM 核心）→ 机器码」并标注 IR 的枢纽位置。
3. **核心子项目表**：列出 `llvm / clang / mlir / lld / lldb / flang / compiler-rt / libcxx` 这 8 个子项目，每个用一句话写职责，并标注它属于「project」还是「runtime」（参考 4.2.2 表格）。
4. **构建提示**：写出启用 clang 与 lld 这两个子项目时，CMake 命令里应该用哪个变量（提示：`LLVM_ENABLE_PROJECTS`）。

**自检标准**：

- 速查表里的项目名都能在 `ls` 的输出里找到对应目录。
- 你能向一个完全没接触过 LLVM 的人解释清楚「IR 为什么是前后端的桥梁」。
- 你能说出 `LLVM_ENABLE_PROJECTS` 和 `LLVM_ENABLE_RUNTIMES` 的区别。

完成后，这张速查表就是你后续阅读所有讲义时的「地图」。

## 6. 本讲小结

- **LLVM 是编译器「基础设施」**，而不是某一个具体语言的编译器；它由 IR、优化器、代码生成器和一组工具库组成。
- **三段式架构**（前端 → IR → 后端）让前后端解耦、优化可复用，是 LLVM 设计的核心理念。
- 整个项目以 **monorepo** 形式组织，`llvm / clang / mlir / lld / lldb / flang` 等子项目**平级**地住在仓库根目录。
- 子项目被 CMake 分成两类：**projects**（用 `LLVM_ENABLE_PROJECTS` 选择，如 clang/lld）与 **runtimes**（用 `LLVM_ENABLE_RUNTIMES` 选择，如 compiler-rt/libcxx）。
- `llvm/CMakeLists.txt` 里的 `LLVM_KNOWN_PROJECTS` 是仓库的权威子项目清单，并通过相对路径 `../<项目名>` 把「目录名 = 项目名」这条约定写进了构建逻辑。

## 7. 下一步学习建议

本讲建立了「全局地图」，但还没有真正进入任何一个目录内部。建议按以下顺序继续：

1. **下一讲 [u1-l2 源码目录结构与组织方式](u1-l2-directory-structure.md)**：深入 `llvm/` 目录内部，了解 `lib/`、`include/`、`tools/`、`examples/`、`test/` 的标准布局，学会快速定位某个功能对应的源码。
2. **[u1-l3 构建系统：CMake 与编译流程](u1-l3-build-system.md)**：动手用 CMake 配置一次最小化构建，把本讲提到的 `LLVM_ENABLE_PROJECTS` 等变量真正跑起来。
3. **[u1-l4 核心命令行工具一览](u1-l4-core-tools.md)**：认识 `opt`、`llc`、`llvm-as`、`lli` 等工具，建立「源码 → IR → 目标码」的端到端命令直觉。
4. 若想尽早感受 IR 的魅力，可穿插阅读 [u2-l2 阅读与编写 LLVM IR](u2-l2-read-write-ir.md)。

> 阅读源码的小提示：随时回到本讲的「子项目表」和速查表，确认你当前所在的目录在整个工具链里的位置——这是避免在庞大的 monorepo 中「迷路」的最有效方法。
