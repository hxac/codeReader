# 项目概览与定位

## 1. 本讲目标

本讲是整本 LLD 学习手册的第一篇。读完本讲后，你应该能够：

- 说清楚 **LLD 是什么**：它是 LLVM 项目中的高性能模块化链接器，用来替代各平台自带的系统链接器（GNU ld、MSVC link.exe、ld64 等）。
- 理解 LLD 在 **LLVM 工具链中的定位**，以及它最核心的几条特性：快、可作为库嵌入、始终是交叉链接器。
- 了解 LLD 同时支持的 **四种目标格式**（ELF / COFF / Mach-O / WebAssembly）以及它们对应的后端目录。
- 掌握阅读 LLD 源码时最该先读的两份文档 `docs/index.md` 与 `docs/NewLLD.md`，并从中提取出后续学习要用到的 **关键概念**（如 Symbol、SymbolTable、InputSection、Driver、Writer）。

本讲不要求你懂 C++ 模板，也不要求你已经写过链接器。我们只读文档和顶层目录，目的是先建立一张「全局地图」，后面的讲义再带你逐层下钻。

## 2. 前置知识

在开始前，先用最朴素的语言把「链接器」这件事讲清楚。

### 2.1 什么是编译，什么是链接

当你写下一段 C 程序并执行 `clang hello.c -o hello` 时，背后其实发生了好几步：

1. **预处理**：展开 `#include`、`#define`。
2. **编译**：把每个 `.c` 源文件翻译成一个 **目标文件**（object file，Unix 下扩展名通常是 `.o`，Windows 下是 `.obj`）。
3. **链接**：把若干个目标文件、静态库（`.a` / `.lib`）、动态库（`.so` / `.dll`）**拼装成一个可执行文件或共享库**。

第 3 步就是 **链接器（linker）** 干的活。你可以把它理解成一个「组装车间」：

- 它读入一堆零件（目标文件、库）；
- 它把每个零件里用到的「符号」（函数名、全局变量名）**对上号**，决定谁定义了谁、谁引用了谁；
- 它把所有零件按规则排列到输出文件里，分配地址；
- 它把每处「这里要调用函数 foo」的占位改写成 foo 的真实地址（这一步叫 **重定位 relocation**）；
- 最后把结果写到磁盘上。

### 2.2 什么是「目标格式」

目标文件、可执行文件、共享库，本质上都是按某种 **二进制布局规范** 组织的字节流，这种规范就叫 **目标格式（object file format）**。不同操作系统用不同的格式：

| 操作系统 | 主要目标格式 | 传统系统链接器 |
| --- | --- | --- |
| Linux / 各种 Unix | ELF | GNU ld / gold |
| Windows | PE / COFF | MSVC `link.exe` |
| macOS | Mach-O | `ld64` |
| （跨平台） | WebAssembly（wasm） | — |

LLD 的特别之处在于：**同一个可执行文件就能处理上面这四种格式**。这是本讲后面要重点讲的事。

### 2.3 阅读本讲你只需要

- 一台装了 `clang` / `gcc` 和 `readelf` 的 Linux 环境（用于可选的动手实验）。
- 会用命令行、能看懂简单的英文文档。
- 知道「函数」「全局变量」这种基础编程概念。

> 本讲引用的所有源码都是真实文件，行号与永久链接均对应当前 HEAD `8bdbeac21ecc`。

## 3. 本讲源码地图

本讲主要读文档与顶层目录，涉及的「源码」其实是项目自身的说明文件：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | LLD 仓库的最顶层说明，告诉你这是什么、许可证、基准测试数据出处。 |
| `docs/index.md` | LLD 的「门面文档」：定位、特性清单、性能对比、构建与使用方法。是初学者最该先读的一篇。 |
| `docs/NewLLD.md` | LLD 的 **设计与内部原理** 文档。官方说它「有点过时但核心概念仍然有效」，是阅读源码前必须建立的概念地图。 |
| 顶层目录（`ELF/`、`COFF/`、`MachO/`、`wasm/`、`MinGW/`、`Common/`、`include/`、`tools/`、`test/`、`unittests/`） | 用于直观感受「一个二进制、四个后端」的目录组织。本讲只看目录名，深入留到 u1-l3。 |

记住一个总线索：**`docs/index.md` 回答「LLD 是什么、怎么用」，`docs/NewLLD.md` 回答「LLD 内部是怎么设计的」**。本讲的三个最小模块正是围绕这两份文档展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 LLD 项目定位与特性** —— 它是什么、为什么存在。
- **4.2 支持的目标格式与后端** —— 一个二进制怎么处理四种格式。
- **4.3 NewLLD 设计文档的关键概念** —— 读源码前要先记住的那张「概念地图」。

---

### 4.1 LLD 项目定位与特性

#### 4.1.1 概念说明

LLD 的全称是 **LLVM Linker**。它是 LLVM 编译器基础设施项目的一个子项目，定位非常明确：**做一个能直接替换各平台系统链接器、而且跑得快得多的链接器**。

> 所谓「drop-in replacement（直接替换）」，是指它尽量接受和原系统链接器 **一样的命令行参数、一样的链接脚本**，这样你不用改构建脚本，只要换个链接器就能用上。

最顶层 `README.md` 第一句话就把这层关系点明了：

> 「This directory and its subdirectories contain source code for the LLVM Linker, a modular cross platform linker which is built as part of the LLVM compiler infrastructure project.」
> —— 这句话的关键词是 **modular（模块化）** 和 **cross platform（跨平台）**。

参考：[README.md:1-9](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/README.md#L1-L9)

#### 4.1.2 核心流程：LLD 在工具链里的位置

用一个流程图式文字来表示 clang + LLD 的工作流：

```
源文件 (.c/.cpp)
   │  clang -c 编译
   ▼
目标文件 (.o)  ──┐
静态库 (.a)    ──┤   clang -fuse-ld=lld
共享库 (.so)   ──┼────────────────────►  LLD (ld.lld)  ──►  可执行文件 / 共享库
LLVM bitcode  ──┘                        （链接）
```

注意：在 Unix 上，链接器 **几乎从不被直接调用**，而是由编译器驱动（clang / gcc）在背后代为调用。你只要告诉 clang「请用 lld 来链接」（通过 `-fuse-ld=lld`），clang 就会把合适的参数传给 `ld.lld`。

#### 4.1.3 源码精读：特性清单

LLD 自我宣称的特性都在 `docs/index.md` 的 Features 小节。我们逐条对照真实文档来读。

定位总纲（开头三句）：

[docs/index.md:1-6](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L1-L6) —— 中文要点：LLD 是 LLVM 项目出品的链接器，是系统链接器的 **drop-in replacement（直接替换品）**，速度快得多，并且为工具链开发者提供了有用的特性。

完整的特性清单在 Features 小节，整理成下表（每条都来自原文）：

[docs/index.md:13-48](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L13-L48)

| 特性 | 原文要点 | 给初学者的解释 |
| --- | --- | --- |
| **直接替换 GNU ld** | accepts the same command line arguments and linker scripts as GNU | 不用改构建脚本就能换上。 |
| **非常快** | more than twice as fast as the GNU gold linker | 多核机器上常比 gold 快两倍以上。 |
| **支持众多 CPU/ABI** | AArch64, AMDGPU, ARM, Hexagon, LoongArch, MIPS, PowerPC, RISC-V, SPARC V9, x86-32/64 | 一个链接器管一大片架构。 |
| **始终是交叉链接器** | always a cross-linker ... no build-time option to enable/disable each target | 不管你怎么编译 LLD，它都支持全部目标；天然适合交叉编译。 |
| **可作为库嵌入** | embed LLD in your program ... call `lld::lldMain` | 不必起子进程，直接在程序里调用链接。 |
| **代码体积小** | 21k lines of C++ (2017 数据) vs gold 198k | 没有抽象层，所以又快又好读。 |
| **默认支持 LTO** | Link-time optimization is supported by default | 传 `-flto` 给 clang 即可全程序优化。 |
| **为 21 世纪调过默认值** | stack marked non-executable by default | 默认栈不可执行，安全性更高。 |

这八条就是 LLD 的「卖点清单」。其中**快、可作为库、始终是交叉链接器**这三条，是后续讲义会反复出现的设计动机，建议重点记住。

#### 4.1.4 代码实践：从文档反查「怎么确认我用的是 LLD」

这是一道「源码阅读 + 命令验证」型练习。

1. **实践目标**：搞清楚「如何确认一个输出文件确实是由 LLD 链接出来的」。
2. **操作步骤**：
   - 打开 [docs/index.md:107-111](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L107-L111)（「Using LLD」小节末尾）。
   - 文档明确写：LLD 会把自己的名字和版本写进输出文件的 `.comment` 段；如果输出里包含字符串 `Linker: LLD`，就说明用了 LLD。
3. **要观察的现象**：在本地执行（待本地验证）

   ```bash
   clang hello.c -fuse-ld=lld -o hello
   readelf --string-dump .comment hello
   ```

4. **预期结果**：`readelf` 的输出里能看到形如 `Linker: LLD x.y.z` 的字符串。
5. 如果你本地没装 `ld.lld`，那么「阅读文档得出结论」本身就已经完成了这道练习——你已经从源文档里找到了答案。

#### 4.1.5 小练习与答案

**练习 1**：`docs/index.md` 说 LLD 是「drop-in replacement for the GNU linkers」，这句话对使用者意味着什么？

> **参考答案**：意味着 LLD 尽量复用 GNU ld 的命令行参数和链接脚本语法，使用者无需修改现有的 `Makefile` / `CMakeLists.txt` / 链接脚本，只要把链接器换成 `ld.lld`（例如给 clang 加 `-fuse-ld=lld`）就能直接工作。

**练习 2**：为什么 LLD 强调「stack 默认不可执行」是一种安全改进？

> **参考答案**：可执行栈会让攻击者更容易利用栈溢出漏洞（把恶意代码塞进栈里并跳过去执行）。默认把栈标记为不可执行（NX），等于关掉了一条常见的攻击路径。LLD 把它设为默认值，是为现代安全要求调优的体现。

---

### 4.2 支持的目标格式与后端

#### 4.2.1 概念说明

绝大多数链接器只服务于一种格式：GNU ld 只懂 ELF，MSVC link 只懂 COFF，ld64 只懂 Mach-O。**LLD 的独特之处是同一个二进制里同时塞了四个链接器**，分别处理四种格式：

- **ELF**（Unix，Linux 主力）
- **PE/COFF**（Windows）
- **Mach-O**（macOS）
- **WebAssembly**

`docs/index.md` 在开头就给出了这四种格式，并按「成熟度」排了序：

> 「The linker supports ELF (Unix), PE/COFF (Windows), Mach-O (macOS) and WebAssembly in descending order of completeness. Internally, LLD consists of several different linkers.」

参考：[docs/index.md:7-11](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L7-L11)

注意「Internally, LLD consists of several different linkers」这句——这是理解整个项目结构的钥匙：**它不是一个能处理多种格式的链接器，而是四个链接器共用一个外壳**。

#### 4.2.2 核心流程：四种格式 ↔ 四个目录

这一点直接体现在仓库的顶层目录上。下表把「目标格式」和「源码目录」「对应的可执行文件名」对应起来：

| 目标格式 | 源码目录 | 传统调用名（LLD 的符号链接名） |
| --- | --- | --- |
| ELF | `ELF/` | `ld.lld` |
| PE/COFF | `COFF/` | `lld-link` |
| Mach-O | `MachO/` | `ld64.lld` |
| WebAssembly | `wasm/` | `wasm-ld` |
| （特殊）GNU 风格的 Windows 交叉编译 | `MinGW/` | （薄包装，转交给 COFF 后端） |
| 公共基础设施 | `Common/`、`include/lld/Common/` | （被四个后端共享） |
| 单一可执行文件入口 / 分发器 | `tools/lld/` | `lld`（根据调用名分发） |

> 「四个后端 + 一个公共层 + 一个分发器」就是 LLD 源码组织的全貌。深入到每个目录内部、看它们各自有哪些共性模块（Driver / SymbolTable / Writer / ...），是 u1-l3 的任务，这里只要记住这张总表。

关于 ELF 后端支持的 CPU/ABI 范围，文档列得很细，值得原样记下（生产级与可用的区分对未来排错很有用）：

[docs/index.md:20-24](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L20-L24) —— 列出 AArch64、AMDGPU、ARM、Hexagon、LoongArch、MIPS 32/64（大/小端）、PowerPC、PowerPC64、RISC-V、SPARC V9、x86-32、x86-64，并标注其中 AArch64、ARM(≥v4)、LoongArch、PowerPC、PowerPC64、RISC-V、x86-32、x86-64 为生产级，MIPS「看起来也不错」。

#### 4.2.3 源码精读：为什么是「共享设计但不共享代码」

这是 `NewLLD.md` 第一条设计决策，必须读懂，否则你会困惑「为什么 ELF 和 COFF 里有那么多名字一样的类却不复用」。

[docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37) —— 原文要点：

- 把每种格式都实现成 **native linker（原生链接器）**；
- 它们 **共享设计思想（share the same design），但几乎不共享代码（share very little code）**；
- 原因：各种目标格式差别大到「为了抽象它们而加的中间层」并不划算，反而增加复杂度和运行时开销；去掉抽象层大大简化了实现。

用一句话总结这张图：

```
   设计哲学（Driver / SymbolTable / Writer / InputFile ...）  ← 共享
        │                │                │             │
       ELF             COFF            MachO          wasm      ← 各自独立实现
   (InputSection)    (Chunk)          ...           ...        ← 数据结构都不同
```

正因为如此，本手册 **以 ELF 为主线** 讲解（第二单元起），再在第八单元横向对比 COFF / Mach-O / wasm。

#### 4.2.4 代码实践：手工绘制「格式—目录—调用名」对照表

这是一道纯目录阅读型练习，不需要编译。

1. **实践目标**：凭观察仓库结构，验证「一个二进制四个后端」的说法。
2. **操作步骤**：
   - 在仓库根目录查看有哪些后端目录（`ELF/ COFF/ MachO/ wasm/ MinGW/`）。
   - 查阅 `docs/index.md` 的「Using LLD」小节 [docs/index.md:91-106](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L91-L106)，确认 LLD 安装后的可执行名是 `ld.lld`，并理解它由编译器驱动代为调用。
3. **要观察的现象**：每个格式都对应一个独立目录；公共代码集中在 `Common/` 与 `include/`。
4. **预期结果**：你能不看本讲，自己默写出 4.2.2 节那张表。

#### 4.2.5 小练习与答案

**练习 1**：如果有人问「为什么 LLD 里 `ELF/InputSection.h` 和 `COFF/Chunks.h` 干的是类似的活，却不抽一个公共基类」，你怎么用 NewLLD.md 的话回答？

> **参考答案**：因为各目标格式差异巨大，抽象公共层「得不偿失」——抽象层带来的复杂度与运行时开销，超过了代码复用的收益。所以 LLD 选择让每个后端各写一套原生实现，共享的是设计思想而非代码。（依据 [docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37)）

**练习 2**：文档说四种格式是「in descending order of completeness」，请说出排序。

> **参考答案**：ELF（最完整）> PE/COFF > Mach-O > WebAssembly。其中 COFF 端「完整，包括 Windows 调试信息（PDB）支持」。（依据 [docs/index.md:7-11](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L7-L11)）

---

### 4.3 NewLLD 设计文档的关键概念

#### 4.3.1 概念说明

`docs/NewLLD.md` 是阅读 LLD 源码前 **必须先读** 的文档。它在开头就说明了我们为什么要在第 1 讲就读它——作者把链接器拆解成少数几个 **关键数据结构（Important Data Structures）** 和 **三个主要角色（three actors）**，并说：

> 「the linker can be understood as the interactions between them. Once you understand their functions, the code of the linker should look obvious to you.」

参考：[docs/NewLLD.md:133-138](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L133-L138)

换句话说，**只要记住这几个概念，后面读源码就不会迷路**。这就是本模块要建立的那张「概念地图」。

#### 4.3.2 核心流程：三条设计原则 + 一组关键数据结构 + 三个角色

把 NewLLD.md 的设计部分归纳成一张总图：

```
┌──────────────── 三条高层设计原则（Key Concepts）────────────────┐
│  1. Native linkers：原生实现，共享设计不共享代码              │
│  2. Speed by design：少做事 > 高效地做事（惰性求值）          │
│  3. Efficient archive handling：记住全部符号，按需即时提取   │
└────────────────────────────────────────────────────────────────┘
         │ 指导着下面这些数据结构与角色的实现
         ▼
┌───── 关键数据结构（Important Data Structures）─────┐
│  Symbol(Defined/Undefined/Lazy)                   │
│  SymbolTable   (字符串 → Symbol 的哈希表 + 冲突解决)│
│  InputSection (ELF) / Chunk (COFF)                │
│  OutputSection (InputSection/Chunk 的容器)         │
└───────────────────────────────────────────────────┘
         │ 被下面三个角色驱动
         ▼
┌────────────── 三个角色（three actors）──────────────┐
│  InputFile ：读入文件，创建并持有 Symbol / Section  │
│  Driver    ：解析命令行、建符号表、串联整个流程     │
│  Writer    ：分配地址、把结果写到输出文件           │
└─────────────────────────────────────────────────────┘
```

下面把每个概念对照原文讲清楚。

#### 4.3.3 源码精读

**（a）三条设计原则**

[docs/NewLLD.md:19-102](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L19-L102)

1. **Speed by design（靠设计来提速）**：原文最经典的一句是「do less rather than do it efficiently」（少做事，胜过把事做得更高效）。具体表现为 **惰性求值**——「不到必须时不读 section 内容和重定位」；对每个符号只查一次哈希表，之后直接用指针这种「handle」。这条原则解释了为什么 LLD 源码里到处是「先记下指针、后面再用」的写法。

2. **Efficient archive handling（高效的归档/静态库处理）**：传统 Unix 链接器只记住「未定义符号集合」，按命令行顺序扫描，遇到归档就从中抽出能解决这些未定义符号的成员；这导致「库放在目标文件之前就什么都不抽」、以及「互相依赖的库需要 `--start-group/--end-group` 反复扫描」的问题。LLD 的做法是 **记住所有符号**，一旦发现某个未定义符号能由之前见过的归档成员解决，就 **立刻** 抽取并链接。因此 `--start-group/--end-group` 在 LLD 里基本是 no-op（u4-l4 会深入）。

**（b）需要记住的「量级」**

[docs/NewLLD.md:104-131](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L104-L131) —— 链接一个约 2 GB 的 Chrome（带调试信息）时，LLD 要读：

- 17,000 个文件
- 1,800,000 个段（section）
- 6,300,000 个符号（symbol）
- 13,000,000 个重定位（relocation）

最终 **15 秒** 产出 2 GB 可执行文件。

文档还给了关于性能直觉的关键提醒：符号字符串总量 450 MB，全部插入哈希表要 1.5 秒；因此「给每个符号随手多加一次哈希查询，就会让链接器慢 10%」。这解释了为什么 LLD 对符号操作极其抠细节，却对「文件数量」不那么在意。

**（c）关键数据结构**

[docs/NewLLD.md:139-204](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L139-L204) 整理如下：

| 概念 | 中文要点 | 后续在哪深入 |
| --- | --- | --- |
| **Symbol** | 每个唯一符号名只有 **一个** Symbol 实例；分 Defined / Undefined / Lazy 三类；用 **placement new** 就地替换，使旧指针自动指向解析结果。 | u4-l1 |
| **SymbolTable** | 字符串 → Symbol 的哈希表，外加 **按符号类型解决冲突** 的逻辑（Defined vs Undefined vs Lazy）。 | u4-l2 |
| **Chunk**（仅 COFF） | COFF 里表示「输出中的一块数据」，知道自己的大小、如何拷贝到 mmap 输出、如何应用重定位。对应 ELF 的 InputSection。 | u8-l1 |
| **InputSection**（仅 ELF） | ELF 直接把输入节当作内部数据类型，职责与 COFF Chunk 类似。 | u5-l1 |
| **OutputSection** | InputSection / Chunk 的 **容器**；每个 InputSection/Chunk 至多属于一个 OutputSection。 | u5-l1 |

**（d）三个角色**

[docs/NewLLD.md:205-231](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L205-L231) —— Driver 是总指挥，它的职责原文写得非常清楚：

1. 处理命令行选项；
2. 创建符号表；
3. 为每个输入文件创建 InputFile，把符号放进符号表；
4. 检查是否还有未解析的未定义符号；
5. 创建 Writer；
6. 把符号表交给 Writer，写出结果。

这六步，几乎就是后续讲义第二单元 ELF 主线的提纲。

> 小提示：NewLLD.md 里把 LTO 也点了一下——「把 LLVM bitcode 当作普通目标文件来解析符号，全部解析成功后再用 LLVM 把所有 bitcode 编译成一个大对象，最后把 bitcode 符号替换成 ELF/COFF 符号」。详见 [docs/NewLLD.md:233-243](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L233-L243)，深入留到 u7-l3。

#### 4.3.4 代码实践：把 NewLLD.md 浓缩成「概念卡片」

这是一道阅读理解 + 整理型练习，目的是让你把文档真正「吃进去」。

1. **实践目标**：用自己的话，把 NewLLD.md 的设计部分浓缩成不超过 10 行的笔记。
2. **操作步骤**：
   - 通读 [docs/NewLLD.md:15-231](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L15-L231)。
   - 用三个小标题分别写出：「三条设计原则」「四个关键数据结构」「三个角色及其职责」。
3. **要观察的现象**：你会发现后续每一篇讲义的标题，几乎都能在这张笔记里找到对应。
4. **预期结果**：得到一张类似 4.3.2 节总图的个人笔记。如果你写不出某一条，就回去重读对应行号。

#### 4.3.5 小练习与答案

**练习 1**：用 NewLLD.md 里「placement new 就地替换 Symbol」的机制，解释「为什么指向某个未定义符号的指针，在符号被解析成已定义符号后会自动指向新结果」。

> **参考答案**：因为符号表保证「每个唯一的符号名只对应一个 Symbol 实例」，解析时不是新建对象再改指针，而是用 placement new 在 **同一块内存上** 用更好的 Symbol 覆盖旧对象。所以原本指向那块内存的指针，读出来的自然是新的（已定义）符号。（依据 [docs/NewLLD.md:155-166](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L155-L166)）

**练习 2**：为什么 NewLLD.md 说「不要给每个符号随手加一次哈希查询」？

> **参考答案**：因为符号数量巨大（量级在百万级），符号字符串总量可达数百 MB，把它们全部插入哈希表本身就要秒级。每多一次「随手」的哈希查询，就会显著拖慢整个链接器（原文举例：慢 10%）。所以 LLD 强调对每个符号只查一次、之后用指针作为 handle。（依据 [docs/NewLLD.md:124-128](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L124-L128)）

**练习 3**：列出 Driver 的六项职责（按顺序）。

> **参考答案**：①处理命令行选项 → ②创建符号表 → ③为每个输入文件创建 InputFile 并把符号放入符号表 → ④检查没有遗留的未定义符号 → ⑤创建 Writer → ⑥把符号表交给 Writer 写出结果。（依据 [docs/NewLLD.md:222-231](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L222-L231)）

---

## 5. 综合实践

本讲的综合实践把「定位 / 特性 / 文档」三件事串起来：**亲手用 LLD 链接一个程序，并从输出和文档里双向验证 LLD 的特性**。

**实践目标**

- 用 `clang -fuse-ld=lld` 链接一个 hello world；
- 用 `readelf` 从输出文件中证明它确实由 LLD 产生；
- 对照 `docs/index.md`，列出 LLD 相对 GNU ld 的三条核心优势。

**操作步骤**

1. 准备一个最简单的 C 程序 `hello.c`：

   ```c
   #include <stdio.h>
   int main(void) { printf("hello, lld\n"); return 0; }
   ```

2. 用 LLD 链接（需要本地已安装 `ld.lld`；待本地验证）：

   ```bash
   clang hello.c -fuse-ld=lld -o hello
   ```

3. 检查 `.comment` 段，确认链接器身份：

   ```bash
   readelf --string-dump .comment hello
   ```

4. 对照 [docs/index.md:13-48](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L13-L48)，挑出你认为最重要的「相对 GNU ld 的三条优势」，并各用一句话写出依据。

**需要观察的现象**

- 步骤 2 能成功生成可执行文件 `hello`，运行 `./hello` 输出 `hello, lld`。
- 步骤 3 的输出中包含 `Linker: LLD ...` 字样（这是文档 [docs/index.md:107-111](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L107-L111) 明确承诺的标记）。

**预期结果**

- 二进制确实由 LLD 链接（`.comment` 段含 `Linker: LLD`）。
- 三条优势的参考答案（你的表述可以不同）：
  1. **快**：多核上常比 GNU gold 还快两倍以上（[docs/index.md:17-19](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L17-L19) 与性能表 [docs/index.md:56-63](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L56-L63)）。
  2. **可作为库嵌入**：直接在程序里调用 `lld::lldMain`，免去外部链接器依赖（[docs/index.md:29-33](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L29-L33)）。
  3. **始终是交叉链接器**：不管怎么编译都支持全部目标架构，适合交叉编译工具链（[docs/index.md:25-28](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L25-L28)）。

> 如果你本地没有 `ld.lld`，无法执行步骤 2/3，请把本实践降级为「文档阅读型」：直接从 `docs/index.md` 与 `docs/NewLLD.md` 中找出上述答案并写下来即可——这同样达成本讲的学习目标。

**附：性能对比的直观感受**

`docs/index.md` 给出的性能表里，以 `clang dbg`（1.67 GiB 输出）为例：GNU ld 用时 104.03s，而 `lld w/threads` 仅 5.28s。加速比约为

\[
\text{加速比} = \frac{104.03}{5.28} \approx 19.7\times
\]

再用 `chromium dbg`（1.14 GiB）算一次：

\[
\text{加速比} = \frac{209.05}{16.70} \approx 12.5\times
\]

可见在大程序上 LLD 相对 GNU ld 快了一个数量级，这正是「LLD 很快」这条特性最直观的证据（数据来源 [docs/index.md:56-63](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L56-L63)）。

## 6. 本讲小结

- **LLD 是 LLVM 项目的高性能模块化链接器**，目标是直接替换各平台的系统链接器（[README.md:1-9](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/README.md#L1-L9)）。
- 它最核心的三条特性是 **快、可作为库嵌入、始终是交叉链接器**（[docs/index.md:13-48](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L13-L48)）。
- **同一个二进制里装了四个链接器**，分别处理 ELF / COFF / Mach-O / WebAssembly，对应 `ELF/ COFF/ MachO/ wasm/` 四个目录（[docs/index.md:7-11](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L7-L11)）。
- 四个后端 **共享设计、几乎不共享代码**，没有抽象公共层（[docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37)）。
- 阅读源码前要先把 `docs/NewLLD.md` 的「关键数据结构（Symbol / SymbolTable / InputSection·Chunk / OutputSection）」与「三个角色（InputFile / Driver / Writer）」记牢（[docs/NewLLD.md:133-231](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L133-L231)）。
- 想确认输出是否由 LLD 链接，看 `.comment` 段里有没有 `Linker: LLD`（[docs/index.md:107-111](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L107-L111)）。

## 7. 下一步学习建议

本讲只建立了「LLD 是什么」的全局印象，还没有真正碰 C++ 源码。建议按下面的顺序继续：

1. **先补齐工程基础**：阅读 u1-l2《构建与运行 LLD》，亲手用 CMake 把 `ld.lld` 编出来；再读 u1-l3《目录结构与多后端源码组织》，把 4.2.2 节那张总表细化到「每个后端目录里都有哪些共性文件」。
2. **再读分发机制**：阅读 u1-l4《单一可执行文件与 flavor 分发机制》，理解 `tools/lld/lld.cpp` 怎么根据调用名把控制权交给四个后端之一。这一篇会把你本讲看到的「一个二进制四个后端」彻底落到代码上。
3. **配合官方文档**：继续精读 `docs/NewLLD.md` 的后半部分（Important Data Structures 之后），并把它和 u2「ELF 链接主线」对照——你会发现第二单元讲的 `link<ELFT>` 流水线，正是本讲 4.3.2 节那张「Driver 六步」的展开版。
