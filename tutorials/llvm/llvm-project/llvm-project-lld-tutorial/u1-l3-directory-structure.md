# 目录结构与多后端源码组织

## 1. 本讲目标

u1-l1 给了你一张「一个二进制、四个后端」的总表，u1-l2 告诉你怎么把它编出来。但那张表只到 **目录名** 为止——目录里面长什么样？为什么四个后端里到处是同名文件？测试又放在哪？本讲就把目录「拆开」给你看。读完本讲，你应该能够：

- 说清楚 LLD 仓库 **每一个顶层目录**（`ELF/ COFF/ MachO/ wasm/ MinGW/ Common/ include/ tools/ test/ unittests/`）各自负责什么。
- 理解为什么 `include/` 目录下 **只有 `lld/Common/`**，而四个后端 **没有** 公共头——即「公共契约」和「内部实现」是怎么分层的。
- 在 `ELF/ COFF/ MachO/ wasm/` 四个后端目录里 **认出同一张设计图反复出现的共性模块**（Driver / SymbolTable / Symbols / Writer / InputFiles / ICF / MarkLive / LTO / MapFile），并能说出它们各自对应的数据结构名为什么不同（InputSection vs Chunk vs InputChunks）。
- 区分 `test/`（lit + FileCheck 端到端测试）与 `unittests/`（gtest 单元测试，把 LLD 当库用）这两套测试体系的职责。

本讲仍然 **不深入任何一个后端的实现细节**，只建立「目录骨架」。从 u2 起我们才进入 ELF 内部逐行读代码。

> 本讲引用的所有源码、目录、文件名都是真实存在的，行号与永久链接均对应当前 HEAD `8bdbeac21ecc`。

## 2. 前置知识

### 2.1 承接前两讲的两张地图

读本讲前，请先在脑海里调出 u1-l1、u1-l2 的两个结论：

1. **格式—目录—调用名总表**（u1-l1 的 4.2.2 节）：ELF→`ELF/`→`ld.lld`、COFF→`COFF/`→`lld-link`、Mach-O→`MachO/`→`ld64.lld`、WebAssembly→`wasm/`→`wasm-ld`，外加 `MinGW/`（薄包装）、`Common/`（公共层）、`tools/lld/`（分发器）。
2. **构建脚本的纳入方式**（u1-l2）：顶层 `CMakeLists.txt` 用一连串 `add_subdirectory()` **无条件** 把 Common 与四个后端都编进同一个 `lld` 可执行文件，这正是「LLD 始终是全架构交叉链接器」在工程上的落点。

本讲要做的，就是把上面第 1 点那张表 **往下钻一层**：每个目录里到底有哪些 `.cpp/.h` 文件，它们的名字为什么会「撞车」。

### 2.2 一个关键术语：「后端（backend）」

在 LLD 语境里，**后端** 不是指 CPU 架构（如 x86、ARM），而是指 **目标格式（object file format）的处理代码**。所以「ELF 后端」= 处理 ELF 格式的那一堆代码，「COFF 后端」= 处理 COFF 格式的那一堆代码。每个后端内部 **再** 按 CPU 架构细分（放在各自的 `Arch/` 子目录里）。

> 别混淆：CPU 架构（如 `ELF/Arch/X86_64.cpp`）是「后端内部」的进一步划分，不是顶层意义上的「后端」。

### 2.3 阅读本讲你只需要

- 会在命令行里用 `ls` 看目录、会读文件名即可；本讲几乎不需要运行任何东西。
- 记得 u1-l1 的关键概念：Symbol / SymbolTable / InputSection（ELF）/ Chunk（COFF）/ OutputSection / InputFile / Driver / Writer。目录里那些同名文件，正是这些概念在磁盘上的落点。

## 3. 本讲源码地图

本讲主要读「目录结构与构建脚本」，真正的「源码」只用到几个关键点：

| 文件 / 目录 | 作用 |
| --- | --- |
| `CMakeLists.txt`（顶层） | 用 `add_subdirectory()` 把 Common、tools/lld 和四个后端串起来——是「目录职责划分」最权威的证据。 |
| `include/lld/Common/Driver.h` | LLD **唯一** 的公共库接口：声明 `lldMain()`、`Flavor` 枚举，以及把五个后端 `link()` 函数登记成一张分发表的 `LLD_ALL_DRIVERS` 宏。 |
| `README.md` | 顶层说明（u1-l1 已读），本讲只用它的存在性印证仓库根布局。 |
| `ELF/README.md`、`COFF/README.md` | 两个后端的入口说明，内容都只有一句「See docs/NewLLD.rst」，指向设计文档。 |
| `docs/NewLLD.md` | 设计文档。本讲复用 u1-l1 已读的「share design, not code」一条来解释「为什么同名文件反复出现」。 |
| 四个后端目录 + `Common/` + `tools/lld/` + `test/` + `unittests/` | 本讲的主角——靠观察文件名来理解结构。 |

一句话线索：**`CMakeLists.txt` 证明目录怎么连，`include/lld/Common/Driver.h` 证明哪些是公共契约，后端目录里的同名文件证明「设计共享、代码不共享」。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 顶层目录与 include：LLD 的「外壳」与公共契约** —— 谁是公共层，谁是后端，公共头为什么只有一个。
- **4.2 后端目录的共性模块：同一张设计图的四份实现** —— 同名文件反复出现，「输入段」在不同后端叫不同名字。
- **4.3 test 与 unittests：两套测试各管一摊** —— 端到端测试 vs 库接口测试。

---

### 4.1 顶层目录与 include：LLD 的「外壳」与公共契约

#### 4.1.1 概念说明

一个自然的问题是：既然 LLD 是「四个链接器共用一个外壳」，那这层「外壳」和「四个内核」在磁盘上是怎么分的？答案是 LLD 把代码明确分成了三类：

1. **公共层（Common）**：四个后端都要用、且 **确实值得** 共享的底层设施——内存分配、错误处理、命令行分发、计时器、版本号等。代码在 `Common/`，公共头在 `include/lld/Common/`。
2. **分发器（dispatcher）**：那个唯一的 `lld` 可执行文件的 `main()` 所在地，负责根据调用名把控制权交给某个后端。代码在 `tools/lld/`。
3. **四个后端 + 一个薄包装**：`ELF/ COFF/ MachO/ wasm/` 各是一个完整链接器；`MinGW/` 只是 COFF 后端的一层命令行翻译。它们之间 **几乎不共享代码**。

注意 1 和 3 的区别：公共层共享的是「跟目标格式无关」的工具代码；后端之间不共享的是「跟目标格式强相关」的链接逻辑。这正是 u1-l1 引用过的 NewLLD 设计原则——「share the same design but share very little code」。

#### 4.1.2 核心流程：构建脚本怎么把它们连起来

顶层 `CMakeLists.txt` 用两段 `add_subdirectory()` 把这件事写得明明白白：

```
# 第一段：公共层 + 分发器（与目标格式无关）
add_subdirectory(Common)        # ← 四后端共享的基础设施
add_subdirectory(tools/lld)     # ← 唯一可执行文件 lld 的 main()

# 第二段：四个后端 + 薄包装（各自独立成库）
add_subdirectory(COFF)
add_subdirectory(ELF)
add_subdirectory(MachO)
add_subdirectory(MinGW)
add_subdirectory(wasm)
```

参考：[CMakeLists.txt:198-199](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L199)（Common 与 tools/lld）与 [CMakeLists.txt:211-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L211-L215)（COFF/ELF/MachO/MinGW/wasm）。

这段脚本能推出三个结论：

- **Common 排在最前面**，因为后端和分发器都依赖它。
- **四个后端顺序无关**——它们彼此独立，谁也不 include 谁。
- **MinGW 和四个正经后端并列**，但从体量看它根本不是一个「链接器」（下一模块会看到它只有 3 个文件），这里只是沿用了同样的 `add_subdirectory` 机制。

#### 4.1.3 源码精读：include 为什么只有一个 Common

把 `include/` 目录展开，你会看到一个很重要、但初学者容易忽略的事实：**它里面只有一个子目录 `include/lld/Common/`，没有任何 `include/lld/ELF/`、`include/lld/COFF/` 之类的后端公共头**。

也就是说，四个后端 **没有对外公开的头文件**。后端自己的头（如 `ELF/Driver.h`、`COFF/Chunks.h`）都是 **内部头**，只在各自后端内部 `#include`，不对外暴露。对外暴露的公共契约，全部集中在 `include/lld/Common/` 这一层。

这套公共契约的核心就是库入口 `lldMain()` 和它的分发机制。看真实头文件：

[include/lld/Common/Driver.h:16-23](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L16-L23) —— 定义 `Flavor` 枚举：`Gnu / MinGW / WinLink / Darwin / Wasm`，正好对应五个调用入口（`ld.lld`、MinGW 的 `ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld`）。

[include/lld/Common/Driver.h:44-45](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L44-L45) —— 库入口 `lldMain(args, stdoutOS, stderrOS, drivers)`，这是「把 LLD 当库用」时唯一需要调用的函数（u3-l4 会深入）。

[include/lld/Common/Driver.h:61-67](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L61-L67) —— `LLD_ALL_DRIVERS` 宏，它把五个后端的 `link()` 函数登记成一张静态分发表：

```cpp
{lld::WinLink, &lld::coff::link},
{lld::Gnu,     &lld::elf::link},
{lld::MinGW,   &lld::mingw::link},
{lld::Darwin,  &lld::macho::link},
{lld::Wasm,    &lld::wasm::link}
```

这张表是本模块最有说服力的证据：**五个后端的 `link()` 函数签名完全一致**（都符合 `Driver` 这个函数指针类型 [include/lld/Common/Driver.h:25-26](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L25-L26)），所以才能塞进同一张分发表。这是「设计共享」的最小体现——它们共享的是 **入口契约**，而不是任何内部实现。

> 串起来记：`Common/` 是公共层、`tools/lld/` 是分发器、四个后端各自独立、`include/lld/Common/` 是唯一的公共契约。这四句话就是 LLD 的「外壳骨架」。`tools/lld/` 怎么根据 `argv[0]` 选 flavor，是 u1-l4 的主题。

#### 4.1.4 代码实践：用 `ls` 画出顶层骨架

这是一道纯观察型练习，不需要编译。

1. **实践目标**：亲手验证「三类代码」的划分。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -d */`，对照本讲 4.1.1 节的三类划分，把每个目录归入「公共层 / 分发器 / 后端 / 薄包装 / 测试」。
   - 执行 `find include -type f`，确认 `include/` 下只有 `lld/Common/` 一个子目录。
3. **要观察的现象**：`include/` 里找不到任何 `ELF/`、`COFF/`、`MachO/`、`wasm/` 目录。
4. **预期结果**：得到一张「目录 → 类别」的归类表；并写下「为什么后端没有公共头」的一句话答案（参考答案见 4.1.5）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Common/` 排在 `add_subdirectory` 的第一个，而四个后端的顺序无所谓？

> **参考答案**：因为 Common 是四个后端和分发器都依赖的公共基础设施，必须先就绪；而四个后端之间互不依赖（谁也不 include 谁），所以它们的相对顺序不影响构建结果。（依据 [CMakeLists.txt:198-199](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L199) 与 [CMakeLists.txt:211-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L211-L215)）

**练习 2**：如果有人想从外部 `#include "lld/ELF/Driver.h"` 来调用 ELF 后端，会遇到什么问题？正确的做法是什么？

> **参考答案**：会找不到该头文件——因为 `include/` 下根本没有 `lld/ELF/` 目录，ELF 后端的头都是内部头，不对外安装（顶层 CMake 也只 `install` `include/lld` 下的 `*.h/*.inc`）。正确做法是只用公共契约 `lld::lldMain()`，并通过 `LLD_ALL_DRIVERS`（或 `LLD_HAS_DRIVER(elf)`）声明要链接哪个后端的 `link()` 函数。（依据 [include/lld/Common/Driver.h:44-67](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L44-L67)）

---

### 4.2 后端目录的共性模块：同一张设计图的四份实现

#### 4.2.1 概念说明

打开 `ELF/` 和 `COFF/` 两个目录，你会立刻发现一件「怪事」：里面有一大堆 **同名文件**——`Driver.cpp`、`SymbolTable.cpp`、`Symbols.cpp`、`Writer.cpp`、`InputFiles.cpp`、`ICF.cpp`、`MarkLive.cpp`、`LTO.cpp`、`MapFile.cpp`……难道它们是重复代码？

不是。这是 u1-l1 引用过的 NewLLD 设计原则的直接产物：

[docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37) —— 原文：每个格式都实现成 **native linker**，「share the same design but share very little code」（共享设计、几乎不共享代码）。

也就是说，**四个后端是同一张设计图的四份独立实现**。设计图规定了「一个链接器需要有：驱动、符号表、符号、写盘器、输入文件、垃圾回收、ICF、LTO、map 文件……」，于是四个后端都按这张图各写一套，文件名自然就撞了。但每个文件里的类、算法、数据结构都是 **针对自己的目标格式重新写过的**，彼此不共享一行代码。

这也是为什么两个后端的 README 都简陋到只有一句话——它们都把你指向同一份设计文档：

[ELF/README.md:1](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/README.md#L1) 和 [COFF/README.md:1](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/COFF/README.md#L1) —— 内容都是 `See docs/NewLLD.rst`（小提示：这里写的是 `.rst`，但实际文件早已改为 `docs/NewLLD.md`，是一处历史遗留的小笔误）。

#### 4.2.2 核心流程：用一张大表看「共性 vs 差异」

把四个后端目录里反复出现的模块铺成一张表（基于实际 `ls` 结果），就能一眼看出「哪些是共性、哪些是各自的特色」：

| 模块职责 | ELF | COFF | MachO | wasm | 说明 |
| --- | --- | --- | --- | --- | --- |
| 驱动 Driver | `Driver.{cpp,h}` + `DriverUtils.cpp` | 同左 | 同左 | `Driver.cpp`（无 `.h`） | 解析命令行、串联全流程；u2 起详解 |
| 符号表 | `SymbolTable.{cpp,h}` | 同左 | 同左 | 同左 | 名字→Symbol 的映射 + 冲突解决 |
| 符号 | `Symbols.{cpp,h}` | 同左 | 同左 | 同左 | Defined/Undefined/Lazy 等 |
| 写盘器 Writer | `Writer.{cpp,h}` | 同左 | 同左 | 同左 + `WriterUtils.{cpp,h}` | 分配地址、写出输出文件 |
| 输入文件 | `InputFiles.{cpp,h}` | 同左 | 同左 | 同左 | 读目标文件/库/bitcode |
| **输入段（数据结构名不同！）** | `InputSection.{cpp,h}` | **`Chunks.{cpp,h}`** | `InputSection.{cpp,h}` | **`InputChunks.{cpp,h}`** | 见 4.2.3 重点 |
| 垃圾回收 --gc-sections | `MarkLive.{cpp,h}` | 同左 | 同左 | 同左 | 从根集合做可达性传播 |
| 等价代码折叠 ICF | `ICF.{cpp,h}` | 同左 | 同左 | **无** | 合并相同只读段；wasm 不做 |
| 链接时优化 LTO | `LTO.{cpp,h}` | 同左 | 同左 | 同左 | 把 bitcode 编译成大对象 |
| map 文件 | `MapFile.{cpp,h}` | `MapFile` + `LLDMapFile` | `MapFile.{cpp,h}` | `MapFile.{cpp,h}` | 输出 `-M` 链接图 |
| 命令行选项 | `Options.td` | 同左 | 同左 | 同左 | TableGen 描述的选项表 |
| 配置 | `Config.h` | 同左 | 同左 | 同左 | 全局配置结构体 |
| 重定位 | `Relocations.{cpp,h}` | （在 `Chunks`/`Writer` 里） | `Relocations.{cpp,h}` | `Relocations.{cpp,h}` | 各格式重定位处理 |
| 合成段 | `SyntheticSections.{cpp,h}` | （散落在 `Chunks`/`DLL`/`PDB`） | `SyntheticSections.{cpp,h}` | `SyntheticSections.{cpp,h}` | GOT/PLT/动态表等 |
| 架构后端 | `Target.{cpp,h}` + `Arch/` | **无独立 Target** | `Target.{cpp,h}` + `Arch/` | **无独立 Target** | 见 4.2.3 |

读这张表的方法：**横向看共性**（Driver/SymbolTable/Symbols/Writer/InputFiles/MarkLive/LTO/MapFile/Options.td/Config.h 这十项，四个后端几乎都有），**纵向看差异**（输入段叫什么名字、有没有 ICF、有没有独立 Target）。

> 两个「轻量后端」要单独记：`MinGW/` 整个目录只有 `Driver.cpp`、`Options.td`、`CMakeLists.txt` 三个文件——它不实现链接，只把 GCC 风格的命令行翻译后 **转交给 COFF 后端**（详见 u8-l3）。这就是 u1-l1 总表里「薄包装」三个字的真身。

#### 4.2.3 源码精读：三个最关键的「同名却不同」的点

**（a）「输入段」在不同后端叫不同名字**

这是四后端最直观的差异。同样是「输出文件里的一块数据」这个概念：

- ELF 直接用输入节，叫 **`InputSection`**（`ELF/InputSection.{cpp,h}`）；
- COFF 抽象成 **`Chunk`**（`COFF/Chunks.{cpp,h}`），NewLLD.md 解释原因是 ELF 合成数据少、COFF 多（[docs/NewLLD.md:179-199](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L179-L199)）；
- Mach-O 也叫 `InputSection`（`MachO/InputSection.{cpp,h}`），但还多了一层 `ConcatOutputSection`；
- wasm 叫 **`InputChunks`**（`wasm/InputChunks.{cpp,h}`），并多一个 `InputElement.h`。

**概念相同，名字不同，实现各自独立**——这就是「共享设计、不共享代码」的典型样本。手册后续会在 u5-l1 讲 ELF 的 `InputSection`、u8-l1 讲 COFF 的 `Chunk`。

**（b）架构后端 Target：只有 ELF 和 MachO 有 `Arch/` 子目录**

「CPU 架构相关」的代码（每条重定位指令怎么写、PLT/GOT 长什么样）放哪？各后端做法不同：

- ELF：有 `ELF/Target.{cpp,h}` 抽象基类 + `ELF/Arch/` 子目录，里面 **每种 CPU 一个文件**：`X86_64.cpp`、`AArch64.cpp`、`ARM.cpp`、`RISCV.cpp`、`PPC64.cpp`、`Mips.cpp`、`LoongArch.cpp`……共十余个（u6-l1 详解）。
- MachO：类似，`MachO/Target.{cpp,h}` + `MachO/Arch/`，但只有 `ARM64.cpp`、`ARM64Common.cpp`、`ARM64_32.cpp`、`X86_64.cpp` 四个（macOS 平台 CPU 种类少）。
- COFF / wasm：**没有** 独立的 `Target.cpp/h` 和 `Arch/` 目录——架构相关逻辑直接写进了 `Chunks`/`Writer`/`Relocations` 等文件里。

> 这正好呼应 u1-l1 列的 CPU/ABI 范围：ELF 后端支持的架构最多，所以它的 `Arch/` 目录最大。

**（c）COFF 后端多出来的「Windows 专有」模块**

COFF 目录里有一批其他后端没有的文件，对应 Windows 平台的特有需求：

- `PDB.{cpp,h}`：Windows 专有的 PDB 调试信息（u8-l1）。
- `DLL.{cpp,h}`：DLL 的导入/导出表。
- `DebugTypes.{cpp,h}` + `TypeMerger.h`：CodeView 类型合并。
- `COFFLinkerContext.{cpp,h}`：COFF 后端的全局上下文聚合（对应 ELF 的 `Ctx`，见 u3-l1）。

这些文件的存在说明：**共性模块之上，每个后端还会按自己的平台加一堆专有模块**。读源码时，先抓共性骨架（4.2.2 表的前十行），再按需看各后端的专有部分，就不会迷路。

#### 4.2.4 代码实践：制作「四后端核心模块对照表」

这是本讲规格要求的核心实践——一道目录阅读 + 制表型练习。

1. **实践目标**：凭自己观察仓库，制作一张表，列出 ELF / COFF / MachO / wasm 各自的 Driver、SymbolTable、Writer、InputFiles 对应的 **文件名**，并用一句话说明 Common 目录为何被四个后端共享。
2. **操作步骤**：
   - 分别 `ls ELF/ COFF/ MachO/ wasm/`，记录四个后端里上述四个模块对应的文件名。
   - `ls Common/` 和 `ls include/lld/Common/`，看看公共层提供了哪些被四个后端都要用的东西（如 `Memory`、`ErrorHandler`、`DriverDispatcher`）。
3. **要观察的现象**：四个后端的 Driver/SymbolTable/Writer/InputFiles 文件名高度雷同，但「输入段」文件名各不相同（InputSection / Chunks / InputChunks）。
4. **预期结果**：得到一张类似下表的对照表（参考答案，可直接核对）：

   | 模块 | ELF | COFF | MachO | wasm |
   | --- | --- | --- | --- | --- |
   | Driver | `Driver.cpp/.h` + `DriverUtils.cpp` | `Driver.cpp/.h` + `DriverUtils.cpp` | `Driver.cpp/.h` + `DriverUtils.cpp` | `Driver.cpp` |
   | SymbolTable | `SymbolTable.cpp/.h` | `SymbolTable.cpp/.h` | `SymbolTable.cpp/.h` | `SymbolTable.cpp/.h` |
   | Writer | `Writer.cpp/.h` | `Writer.cpp/.h` | `Writer.cpp/.h` | `Writer.cpp/.h` + `WriterUtils.cpp/.h` |
   | InputFiles | `InputFiles.cpp/.h` | `InputFiles.cpp/.h` | `InputFiles.cpp/.h` | `InputFiles.cpp/.h` |

5. **「Common 为何被共享」的一句话答案**：

   > 因为 `Common/` 提供的是 **与目标格式无关** 的底层基础设施（内存分配、错误处理、命令行分发、计时器、版本号、字符串保存），四个后端都需要、且这部分代码抽象成本低、收益高，所以值得共享；而后端之间不共享的是 **与目标格式强相关** 的链接逻辑，那部分抽象「得不偿失」（依据 [docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ELF/InputSection.h` 和 `COFF/Chunks.h` 干的是类似的活，却没有一个公共基类？

> **参考答案**：因为各目标格式差异巨大，为它们抽象一个公共「段」基类「得不偿失」——抽象层带来的复杂度与运行时开销，超过了代码复用的收益。所以 LLD 让每个后端各写一套原生实现（ELF 叫 InputSection、COFF 叫 Chunk、wasm 叫 InputChunks），共享的是 **设计思想** 而非代码。（依据 [docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37) 与 [docs/NewLLD.md:179-199](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L179-L199)）

**练习 2**：在 `ELF/Arch/` 和 `MachO/Arch/` 两个目录里，哪个的 CPU 文件更多？为什么？

> **参考答案**：`ELF/Arch/` 多得多（十余个：X86_64、AArch64、ARM、RISCV、PPC64、Mips、LoongArch、Hexagon、SPARCV9、SystemZ……），而 `MachO/Arch/` 只有 ARM64/ARM64_32/X86_64 等四个。因为 ELF 后端要服务 Linux 上的一大片 CPU 架构，而 Mach-O 后端只服务 macOS 平台，CPU 种类少得多。（依据实际目录列表，并呼应 [docs/index.md:20-24](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L20-L24)）

**练习 3**：`MinGW/` 目录为什么没有 `SymbolTable.cpp`、`Writer.cpp`？

> **参考答案**：因为 MinGW 不是一个独立的链接器，而是 COFF 后端的一层 **薄包装**——它只负责把 GCC 风格的命令行选项翻译成 COFF 后端能接受的形态，然后把真正的链接工作 **转交给 COFF 后端**。符号表、写盘这些重活都由 COFF 后端承担，所以 MinGW 目录里只有 `Driver.cpp`、`Options.td`、`CMakeLists.txt` 三个文件。（依据实际目录列表，详见 u8-l3）

---

### 4.3 test 与 unittests：两套测试各管一摊

#### 4.3.1 概念说明

LLD 有 **两套** 测试体系，名字相近、目录不同、目的不同，初学者很容易混。先把结论摆出来：

| 维度 | `test/` | `unittests/` |
| --- | --- | --- |
| 测试框架 | **lit** + **FileCheck**（端到端） | **gtest**（C++ 单元测试） |
| 测试内容 | 真的编译 `.s/.c` → 用 `ld.lld`/`lld-link`/... 链接 → 检查输出 | 把 LLD 当 **库** 调用 `lldMain()`，检查返回值 |
| 子目录组织 | 按后端分：`test/ELF`、`test/COFF`、`test/MachO`、`test/wasm`、`test/MinGW` | 按使用场景分：`unittests/AsLibELF`、`unittests/AsLibAll` |
| 主要回答的问题 | 「这个选项/特性产生的 **二进制** 对不对？」 | 「把 LLD 当库 **嵌入** 我的程序，行为对不对？」 |
| 入口命令 | `check-lld`（lit 套件） | 同样由 `check-lld` 经 `test/Unit/` 转入运行 |

一句话：`test/` 测 **链接行为**，`unittests/` 测 **库接口**。

#### 4.3.2 核心流程：两套测试怎么跑起来

顶层 `CMakeLists.txt` 在开启测试（`LLVM_INCLUDE_TESTS`）时，把两套都纳入：

- `add_subdirectory(unittests)` → 编出 gtest 单元测试二进制（`LLDUnitTests` 目标）。
- `add_subdirectory(test)` → 配置 lit 套件，目标名 `check-lld`。

参考：[CMakeLists.txt:201-208](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L201-L208)。

两者的「桥」是 `test/Unit/` 这个小目录。它只有 `lit.cfg.py` 和 `lit.site.cfg.py.in` 两个文件——作用是 **让 gtest 二进制也能在 lit 框架里被跑起来**。也就是说，`unittests/` 里编译出的 C++ 单元测试，最终是通过 `test/Unit/` 这层 lit 包装、和 `test/ELF` 等端到端测试 **一起** 由 `check-lld` 统一运行的。`test/CMakeLists.txt` 里 `set(LLD_TEST_DEPS lld LLDUnitTests ...)`（[test/CMakeLists.txt:42](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L42)）也印证了：lit 套件同时依赖 `lld` 可执行文件 **和** `LLDUnitTests` 这两套产物。

```
                 check-lld  (lit 总入口)
                 /                   \
        test/ELF, test/COFF, ...    test/Unit/  ← lit 包装
        (端到端: 链接二进制)              │
                                   跑 gtest 二进制
                                   (来自 unittests/)
```

#### 4.3.3 源码精读：看一个端到端测试和一个库测试各长什么样

**（a）端到端测试（test/）**

`test/ELF/` 下的测试文件大多是 `.s` 汇编文件（如 `aarch64-abs16.s`、`aarch64-branch-to-branch.s`），每个文件顶部有 `RUN:` 和 `CHECK:` 行：先用 `llvm-mc` 汇编成 `.o`，再用 `ld.lld` 链接，最后用 `FileCheck` 核对输出。这些测试 **重度依赖** 一大堆 LLVM 工具——`test/CMakeLists.txt` 把它们都列成了依赖（[test/CMakeLists.txt:43-83](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L43-L83)）：`FileCheck`、`llvm-mc`、`llvm-readelf`、`llvm-objdump`、`llvm-nm`、`llvm-ar`、`split-file`……这也解释了 u1-l2 提到的「lit 测试离不开 LLVM 工具链」。怎么读懂、怎么新增这种测试，是 u9-l1 的主题。

**（b）库测试（unittests/）**

`unittests/` 下是 C++ 源文件，直接 `#include` 公共头并调用 `lldMain`。两个子目录的分工很清楚：

- `unittests/AsLibAll/AllDrivers.cpp`：用 `LLD_ALL_DRIVERS` 声明 **全部五个** 后端驱动都链接进来，逐一调用验证。
- `unittests/AsLibELF/`：只链接 **ELF** 驱动，里面有 `SomeDrivers.cpp`（验证「没链接的后端调用应失败」）、`OutputStream.cpp`、`ROCm.cpp`，以及 `Inputs/kernel1.o`、`Inputs/kernel2.o` 这样的真实输入夹具。

这正好是 4.1.3 节那张 `LLD_ALL_DRIVERS` 分发表的真实使用范例——库测试就是在验证「把 LLD 当库用、按需挑选后端」这条路走得通。深入留到 u3-l4（把 LLD 当库）与 u9-l1（测试体系）。

#### 4.3.4 代码实践：定位一个端到端测试，区分它与库测试

1. **实践目标**：亲眼看清两套测试的形态差异。
2. **操作步骤**：
   - 在 `test/ELF/` 里任挑一个 `.s` 文件（例如 `test/ELF/aarch64-abs16.s`），打开它，找到顶部的 `RUN:` 行和 `CHECK:` 行，看它怎么调 `ld.lld` 和 `FileCheck`。
   - 打开 `unittests/AsLibAll/AllDrivers.cpp`，对比它是不是 **没有** `RUN:`/`CHECK:`，而是直接在 C++ 里调用 `lld::lldMain`。
3. **要观察的现象**：一个用「命令行 + 文本核对」测链接产物，一个用「C++ 断言」测库函数返回值。
4. **预期结果**：你能用一句话说清两者区别——`test/` 测「链接出的二进制对不对」，`unittests/` 测「把 LLD 当库嵌入程序时接口对不对」。

#### 4.3.5 小练习与答案

**练习 1**：`test/Unit/` 目录里没有真正的测试用例（只有 `lit.cfg.py`），它为什么存在？

> **参考答案**：`test/Unit/` 是 lit 与 gtest 之间的 **桥**。`unittests/` 编出的是 gtest 二进制（`LLDUnitTests`），为了让它也能被 `check-lld` 这个 lit 总入口统一运行，就在 `test/Unit/` 放一层 lit 配置来包装它。所以它自己不写测试，只负责「把 gtest 结果接进 lit」。（依据 [CMakeLists.txt:201-208](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L201-L208) 与 [test/CMakeLists.txt:31-42](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L31-L42)）

**练习 2**：如果你要验证「`--gc-sections` 是否丢掉了某个未引用函数」，应该加测试到 `test/` 还是 `unittests/`？为什么？

> **参考答案**：应该加到 `test/`（具体是 `test/ELF`，因为是 ELF 的选项）。因为这是一个 **链接行为** 问题——需要真的链接出一个二进制、再检查它里面有没有那个段。这正是 lit + FileCheck 端到端测试的职责。`unittests/` 只适合测「库接口/返回值」这类不涉及具体链接产物的问题。（依据 [test/CMakeLists.txt:43-83](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L43-L83)）

---

## 5. 综合实践

本讲的综合实践把「目录骨架 → 共性模块 → 测试归属」三件事串起来：**给 LLD 画一张「源码地图」，并能把任意一个文件归位**。

**实践目标**

- 凭观察仓库，产出一张完整的「LLD 源码地图」：顶层目录职责 + 四后端共性模块对照。
- 对任意给定的源码文件（如 `ELF/MarkLive.cpp`、`COFF/PDB.cpp`、`unittests/AsLibELF/SomeDrivers.cpp`），能立刻说出它属于哪一层、管什么事。

**操作步骤**

1. 先 `ls -d */` 拿到顶层目录，按本讲 4.1.1 节的「公共层 / 分发器 / 后端 / 薄包装 / 测试」给每个目录归类，填出下表：

   | 顶层目录 | 类别 | 一句话职责 |
   | --- | --- | --- |
   | `Common/` | 公共层 | 四后端共享的基础设施（内存/错误/分发/计时） |
   | `include/lld/Common/` | 公共契约 | 唯一的公共头，定义 `lldMain` 与分发宏 |
   | `tools/lld/` | 分发器 | 唯一可执行文件的 `main`，按调用名选后端 |
   | `ELF/ COFF/ MachO/ wasm/` | 后端 | 四个独立链接器，各自完整实现 |
   | `MinGW/` | 薄包装 | 把 GCC 风格命令行翻译后转交 COFF |
   | `test/` | 端到端测试 | lit + FileCheck，按后端分子目录 |
   | `unittests/` | 库测试 | gtest，把 LLD 当库用 |

2. 再用本讲 4.2.4 节那张「四后端核心模块对照表」作为第二层，把骨架细化到文件名。
3. 做一个「归位」自测：随机挑 5 个文件（建议至少包含一个 `ELF/Arch/*.cpp`、一个 `COFF/*.h`、一个 `test/ELF/*.s`、一个 `unittests/*.cpp`、一个 `Common/*.cpp`），对每个文件说出：它在哪一层？对应哪个共性模块（或专有模块）？属于哪套测试？
4. 用一句话回答「为什么 `Common/` 被四个后端共享，而后端之间不共享代码」（参考 4.2.4 节第 5 步的答案）。

**需要观察的现象**

- 顶层目录能干净地分成「公共层 / 分发器 / 后端 / 薄包装 / 测试」五类，没有归属模糊的目录。
- 四后端在 Driver/SymbolTable/Symbols/Writer/InputFiles/MarkLive/LTO/MapFile 上文件名高度雷同，但在「输入段」「架构 Target」上差异明显。
- 端到端测试（`.s` + `RUN`/`CHECK`）和库测试（C++ 调 `lldMain`）形态完全不同。

**预期结果**

- 得到一张两层地图：第一层是顶层目录职责，第二层是后端共性模块对照。
- 「归位」自测的 5 个文件全部答对。
- 「共享 vs 不共享」的一句话答案与 4.2.4 节一致：公共层共享的是 **与格式无关** 的基础设施（抽象成本低、收益高），后端不共享的是 **与格式强相关** 的链接逻辑（抽象得不偿失）。

> 本实践全程不需要编译，纯靠 `ls`、`cat`/`Read` 和观察即可完成。如果你本地已经按 u1-l2 构建出了 `ld.lld`，可以额外跑一遍 `check-lld` 感受两套测试一起运行的过程，但这不是本实践的必需步骤。

## 6. 本讲小结

- LLD 的代码分成三类：**公共层**（`Common/` + `include/lld/Common/`）、**分发器**（`tools/lld/`）、**四个后端 + 一个薄包装**（`ELF/ COFF/ MachO/ wasm/ MinGW/`）。构建脚本用 `add_subdirectory()` 把它们串起来，Common 最先，后端互相独立（[CMakeLists.txt:198-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L215)）。
- `include/` 下 **只有 `lld/Common/`**，四个后端没有公共头；唯一的公共契约是 `lldMain()` 和把五个后端 `link()` 登记成一张分发表的 `LLD_ALL_DRIVERS` 宏（[include/lld/Common/Driver.h:44-67](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L44-L67)）。
- 四个后端是 **同一张设计图的四份独立实现**：Driver / SymbolTable / Symbols / Writer / InputFiles / MarkLive / LTO / MapFile / Options.td / Config.h 这些文件名反复出现，但代码互不共享（[docs/NewLLD.md:28-37](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/NewLLD.md#L28-L37)）。
- 「输入段」概念相同、名字不同：ELF/MachO 叫 `InputSection`、COFF 叫 `Chunk`、wasm 叫 `InputChunks`；只有 ELF 和 MachO 有 `Arch/` 子目录承载多 CPU 架构。
- `MinGW/` 只是 COFF 的薄包装（仅 3 个文件）；COFF 还多出 `PDB/DLL/DebugTypes/COFFLinkerContext` 等 Windows 专有模块。
- 两套测试分工明确：`test/`（lit + FileCheck，按后端分子目录）测 **链接行为**，`unittests/`（gtest，`AsLibELF`/`AsLibAll`）测 **库接口**，两者通过 `test/Unit/` 的 lit 包装统一由 `check-lld` 运行。

## 7. 下一步学习建议

本讲只建立了「目录骨架」，还没真正读任何后端的 C++ 实现。建议按下面的顺序继续：

1. **先读分发机制**：阅读 u1-l4《单一可执行文件与 flavor 分发机制》，看 `tools/lld/lld.cpp` 的 `main` 怎么根据 `argv[0]`（`ld.lld`/`lld-link`/`ld64.lld`/`wasm-ld`）选出 `Flavor`，再交给 `Common/DriverDispatcher.cpp` 调用对应后端的 `link()`。这一篇把本讲 4.1.3 节那张 `LLD_ALL_DRIVERS` 分发表彻底落到代码上。
2. **再进 ELF 主线**：从 u2-l1《link() 入口与 Ctx 上下文对象》开始，沿着 `ELF/Driver.cpp` 的 `link()` → `linkerMain` → `link<ELFT>` → `Writer` 走完一遍链接全过程。你会发现本讲 4.2.2 表里的 `Driver.cpp/SymbolTable.cpp/Writer.cpp` 终于「活」了起来。
3. **横向对比留到后面**：COFF 的 `Chunks`、Mach-O 的 `OutputSegment`、wasm 的 `InputChunks` 等差异，在第八单元（u8）会专门对照讲解；在那之前，建议先以 ELF 为主线把共性模块吃透。
