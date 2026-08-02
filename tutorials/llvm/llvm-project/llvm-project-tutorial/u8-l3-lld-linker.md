# LLD 链接器架构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 LLD 在 LLVM 工具链中的定位——它为什么是「一个可执行文件里装了四个链接器」。
- 解释 LLD 如何按目标格式（ELF / COFF / Mach-O / Wasm）派发到各自的驱动。
- 以 ELF 驱动为主线，复述链接器的核心三段流程：**解析输入 → 符号决议 → 合并段并写出文件**。
- 在源码中定位派发入口、符号表、Writer，并理解它们如何协作产出可执行文件。
- 用 `ld.lld`、`readelf`、`nm` 完成一次端到端链接，并解读符号表与段布局。

## 2. 前置知识

本讲依赖你已经建立的两块认知：

- **目标文件从哪里来**（u6-l4）：LLVM 后端经 MC 层把指令发射成 `.s` 汇编或 `.o` 目标文件（ELF/COFF/Mach-O/Wasm 之一）。链接器正是这些 `.o` 的「消费者」——它读入一批目标文件与库，把它们拼成一个可执行文件或共享库。请记住 MC 层提到的术语：**triple**（决定对象格式）、**对象文件格式**、**目标（Target）**。
- **LTO 与 IRMover**（u8-l2）：当输入是 LLVM 位码（bitcode）而非传统 `.o` 时，链接器会触发链接时优化。LLD 复用 LLVM 的 LTO API（`lto::LTO`）把位码编译成真正的目标文件，再走常规链接流程；跨模块搬运靠 `IRMover`。理解这一点，你就能明白本讲流水线里为什么会专门有一步 `compileBitcodeFiles`。

此外你需要一个朴素直觉：**链接器解决的是「分散在各目标文件里的符号如何互相找到对方」**。例如 `main.o` 调用了 `printf`，但 `printf` 的代码在另一个文件或 `libc` 里，链接器负责把这二者连起来。本讲讲的「符号解析」就是这件事的正式说法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lld/README.md](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/README.md) | 一句话定位 LLD 为「modular cross platform linker」。 |
| [lld/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/CMakeLists.txt) | 顶层构建脚本，揭示 ELF/COFF/MachO/MinGW/wasm 五个平级子目录。 |
| [lld/tools/lld/lld.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/tools/lld/lld.cpp) | `lld` 可执行文件的薄壳入口，仅负责派发。 |
| [lld/include/lld/Common/Driver.h](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/include/lld/Common/Driver.h) | 定义 `Flavor` 枚举与「flavor → link 函数指针」的注册宏。 |
| [lld/Common/DriverDispatcher.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp) | 通用派发逻辑：从 `argv[0]` 推断 flavor，挑出对应驱动。 |
| [lld/ELF/Driver.h](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.h) 与 [lld/ELF/Driver.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.cpp) | ELF 驱动：`elf::link` 入口、`linkerMain` 编排、`link` 模板主流水线。 |
| [lld/ELF/SymbolTable.h](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/SymbolTable.h) 与 [lld/ELF/Symbols.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Symbols.cpp) | 全局符号表与 `resolve` 决议逻辑。 |
| [lld/ELF/Writer.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Writer.cpp) | 把决议后的段排序、分配地址、写出目标文件。 |

> 约定：本讲所有永久链接均指向固定 HEAD `2a4acc46`，行号据此版本核实。

## 4. 核心概念与源码讲解

本讲分三个最小模块：**4.1 多格式派发**、**4.2 符号解析**、**4.3 段合并与输出**。前两个对应大纲中的「LLD 多格式驱动」与「符号解析与段合并」。

### 4.1 LLD 多格式驱动

#### 4.1.1 概念说明

LLD 的 README 把它定义为：

> a modular cross platform linker which is built as part of the LLVM compiler infrastructure project.

——见 [lld/README.md:1-9](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/README.md#L1-L9)。

两个关键词：

- **modular（模块化）**：一个 `lld` 二进制里同时装了 **ELF、COFF、Mach-O、WebAssembly** 四套链接器，彼此代码隔离，却共享同一套错误处理、内存管理、命令行解析等公共设施。
- **cross platform（跨平台）**：它能在 Linux 上链 ELF、在 Windows 上链 COFF（`lld-link`）、在 macOS 上链 Mach-O（`ld64.lld`），由同一个可执行文件承担。

关键设计是 **flavor（风味）**：调用者用什么名字（`argv[0]`）或什么 `-flavor` 参数来启动 `lld`，就决定了它今天扮演哪个链接器。这是一种「同名异构」的多态——同一份二进制，四种人格。

#### 4.1.2 核心流程

派发的判据有两种，优先级从高到低：

1. **`-flavor` 选项**：如 `lld -flavor gnu ...`，直接指定。这是为向后兼容保留的，官方不推荐。
2. **`argv[0]`（命令名）**：这是主流方式。常见的对应关系是——
   - `ld.lld` → ELF（Unix）
   - `ld64` / `ld64.lld` → Mach-O（macOS）
   - `lld-link` → COFF（Windows）
   - `ld-wasm` / `wasm-ld` → WebAssembly

派发流程伪代码：

```
argv[0] ──取文件名──> "ld.lld"
        ──按 "-" 切分逐段匹配──> flavor = Gnu
        ──(Gnu 且 -m 指向 PE 目标?)──> flavor = MinGW   # 特例
        ──在驱动表里查 flavor──> 得到 lld::elf::link 函数指针
        ──调用该函数──> 进入对应格式驱动
```

判别结果是一个 `Flavor` 枚举值（`Gnu`/`MinGW`/`WinLink`/`Darwin`/`Wasm`），用它去一张「flavor → 函数指针」表里查到真正的链接入口。

#### 4.1.3 源码精读

**（1）Flavor 枚举与驱动函数指针类型**

`Flavor` 是派发的「语言」；每个具体驱动对外暴露一个统一签名的 `link` 函数：

[lld/include/lld/Common/Driver.h:16-31](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/include/lld/Common/Driver.h#L16-L31) 定义了枚举、函数指针类型 `Driver` 和 `{flavor, 函数指针}` 配对结构 `DriverDef`。注意 `Driver` 是一个 **裸函数指针**，不是类——派发的本质就是「查表后调用一个函数」。

**（2）把四个链接器注册成一张表**

宏 `LLD_ALL_DRIVERS` 直接把 flavor 与各驱动的 `link` 函数绑死成数组：

```cpp
#define LLD_ALL_DRIVERS                                                        \
  { {lld::WinLink, &lld::coff::link}, {lld::Gnu, &lld::elf::link},             \
    {lld::MinGW, &lld::mingw::link}, {lld::Darwin, &lld::macho::link}, {       \
      lld::Wasm, &lld::wasm::link } }
```

见 [lld/include/lld/Common/Driver.h:61-67](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/include/lld/Common/Driver.h#L61-L67)。这张表就是「flavor → link 函数」的权威映射。配套的 `LLD_HAS_DRIVER(name)` 宏（[同文件 L51-L57](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/include/lld/Common/Driver.h#L51-L57)）则声明对应命名空间里的 `link` 符号，以便库使用者按需只链接部分驱动。

**（3）薄壳入口 `lld_main`**

可执行文件的 `main` 极薄：它把参数交给通用入口 `unsafeLldMain`，自己只处理「是否在 lit 测试中需要多次重跑」这类杂务：

[lld/tools/lld/lld.cpp:69-93](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/tools/lld/lld.cpp#L69-L93)。其中 L69–L73 的四行 `LLD_HAS_DRIVER(coff/elf/mingw/macho/wasm)` 正是把五个驱动声明进当前编译单元，L90 把 `LLD_ALL_DRIVERS` 这张表传给 `unsafeLldMain`。文件头注释（[L13-L21](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/tools/lld/lld.cpp#L13-L21)）一句话总结了「单可执行文件、四链接器、按 argv[0] 派发」的设计意图。

**（4）真正的派发逻辑**

`getFlavor` 用一张字符串映射表把名字翻译成 flavor：

```cpp
static Flavor getFlavor(StringRef s) {
  return StringSwitch<Flavor>(s)
      .CasesLower({"ld", "ld.lld", "gnu"}, Gnu)
      .CasesLower({"wasm", "ld-wasm"}, Wasm)
      .CaseLower("link", WinLink)
      .CasesLower({"ld64", "ld64.lld", "darwin"}, Darwin)
      .Default(Invalid);
}
```

见 [lld/Common/DriverDispatcher.cpp:31-38](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L31-L38)。

`parseProgname`（[L83-L95](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L83-L95)）把 `argv[0]`（如 `lld-gnu`）按 `-` 切分，逐段丢进 `getFlavor`，命中即返回。`parseFlavorWithoutMinGW`（[L98-L125](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L98-L125)）先看有没有 `-flavor`，没有就回落到 `parseProgname`。

一个精妙的特例是 `parseFlavor`（[L127-L137](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L127-L137)）：当 flavor 是 `Gnu` 时，它还会检查 `-m <emulation>` 是否指向 PE 目标（如 `i386pep`，见 [L46-L49](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L46-L49)），若是则改判为 `MinGW`——因为 MinGW 在 Windows 上也以 `ld.lld` 之名被调用，需靠目标三元组二次区分。

最后 `whichDriver`（[L139-L150](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L139-L150)）据 flavor 在表里查函数指针；`unsafeLldMain`（[L157-L175](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/Common/DriverDispatcher.cpp#L157-L175)）拿到指针后直接 `d(argsV, ...)` 调用——这一行（L163）就是「跨过派发、进入真正链接器」的分水岭。

**（5）五个平级子目录**

构建脚本把四种格式的驱动并列挂出：

[lld/CMakeLists.txt:211-215](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/CMakeLists.txt#L211-L215) 依次 `add_subdirectory(COFF/ELF/MachO/MinGW/wasm)`，外加 L198 的 `Common` 公共库。这正是「模块化」在工程结构上的体现：每格式一个独立目录，各自有 Driver/SymbolTable/Writer，共享 `lld/Common`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到同一个 `lld` 二进制按不同名字表现出不同行为。
2. **操作步骤**：
   - 用 `which ld.lld lld-link ld64.lld wasm-ld` 查看它们是否都指向同一个二进制（通常都是 `lld` 的符号链接或拷贝）。
   - 分别运行 `ld.lld --version` 与 `lld-link --version`，对比输出。
3. **需要观察的现象**：`ld.lld` 报告 "compatible with GNU linkers"，而 `lld-link` 报告的是 Microsoft COFF 链接器风格。
4. **预期结果**：同一个可执行文件，因 `argv[0]` 不同而进入不同驱动。
5. **若本地无构建产物**：明确标注「待本地验证」。也可改为**源码阅读型实践**——在 `lld.cpp` 的 `lld_main` 加一行日志打印 `args[0]`，再阅读 `DriverDispatcher.cpp` 的 `parseProgname` 确认这条名字如何变成 flavor。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 LLD 选择「一个二进制装四个链接器」，而不是像 GNU 那样 `ld` 与 MinGW 的链接器各自独立？
  - **参考答案**：共享 `lld/Common` 的错误处理、内存分配、命令行基础设施，减少重复代码；同时通过 flavor 派发让安装更简单（一套二进制 + 几个符号链接即可覆盖多平台）。代价是二进制略大、各格式驱动之间需保持接口一致。

- **练习 2**：若把可执行文件命名为 `myld` 直接运行，会发生什么？
  - **参考答案**：`parseProgname` 按 `-` 切分 `myld`，逐段送入 `getFlavor` 都不命中，返回 `Invalid`；`whichDriver` 找不到对应驱动，返回一个总返回 `false` 的空函数，`unsafeLldMain` 据此报错退出。所以必须用约定俗成的名字调用。

---

### 4.2 符号解析

#### 4.2.1 概念说明

派发进入 ELF 驱动后，链接器要解决的核心问题是**符号解析（symbol resolution）**：每个目标文件都有一张符号表，列出它「定义了哪些符号」「引用了哪些符号」。当多个文件被一起链接时，同名符号可能重复出现，链接器必须为每个名字**选出唯一一个定义**，并让所有引用都指向它。

为此 LLD 维护一张全局 **SymbolTable（符号表）**。它的职责在头文件里说得很清楚：

> SymbolTable is a bucket of all known symbols, including defined, undefined, or lazy symbols (the last one is symbols in archive files whose archive members are not yet loaded).

见 [lld/ELF/SymbolTable.h:27-52](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/SymbolTable.h#L27-L52)。

这里出现五类符号，初学者需辨清：

| 符号种类 | 含义 | 典型来源 |
| --- | --- | --- |
| **Defined（已定义）** | 真正有代码/数据的符号 | `.o` 里的函数、全局变量 |
| **Undefined（未定义）** | 被引用但本文件未提供 | 调用了外部函数 |
| **Lazy（惰性）** | 定义存在于静态库（archive）成员中，但还没被加载 | `libfoo.a` 里的 `.o` |
| **Shared（共享）** | 定义在动态链接库 `.so` 中 | `libfoo.so` |
| **Common（公共）** | 类似 C 的 tentative 定义（未初始化全局变量） | `int x;`（非 `static`） |

#### 4.2.2 核心流程

符号解析的核心动作是 `resolve`。每当一个输入文件被解析、遇到一个符号，就调用：

```
addSymbol(newSym):
    sym = symtab.insert(newSym.name)   # 按名查/建 Symbol 对象（若不存在则新建）
    sym.resolve(newSym)                 # 用新符号与已有符号「决斗」，胜者留下
```

关键设计是 **就地覆盖（in-place overwrite）**：`Symbol` 对象一旦在表里创建就不再更换指针，而是把「更优定义」的字段直接覆盖到同一个对象上（`newSym.overwrite(*this)`）。这样所有持有该 `Symbol*` 的引用方都不需要更新指针。

决议的优先级（直觉版）：

\[ \text{Defined} > \text{Shared} > \text{Common} > \text{Lazy} > \text{Undefined} \]

但有两类交互特别重要：

- **Undefined vs Lazy**：当遇到一个未定义引用，而某静态库里有惰性定义时，链接器会**从库里抽出（extract）那个成员**，把惰性符号升级为真正的已定义符号。这就是「链接器自动从 `.a` 里找实现」的原理。
- **重复定义**：两个普通（强）符号都定义了同名符号，会报「duplicate symbol」错误；但强符号可覆盖弱符号（`STB_WEAK`）。

#### 4.2.3 源码精读

**（1）`addSymbol`：插入 + 决议两步走**

```cpp
template <typename T> Symbol *addSymbol(const T &newSym) {
  Symbol *sym = insert(newSym.getName());
  sym->resolve(ctx, newSym);
  return sym;
}
```

见 [lld/ELF/SymbolTable.h:48-52](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/SymbolTable.h#L48-L52)。`insert` 按名字在 `symMap` 里查（[L95-L99](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/SymbolTable.h#L95-L99)），命中返回已有对象，未命中则新建并记入 `symVector`。注意类注释（[L30-L38](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/SymbolTable.h#L30-L38)）特别说明「defined 优于 undefined」「lazy 与 undefined 冲突时会抽出 archive 成员」——这正是 `resolve` 的行为契约。

**（2）`resolve(Defined)`：择优覆盖**

```cpp
void Symbol::resolve(Ctx &ctx, const Defined &other) {
  if (other.visibility() != STV_DEFAULT) {
    uint8_t v = visibility(), ov = other.visibility();
    setVisibility(v == STV_DEFAULT ? ov : std::min(v, ov));
  }
  if (shouldReplace(ctx, other))
    other.overwrite(*this);
}
```

见 [lld/ELF/Symbols.cpp:643-650](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Symbols.cpp#L643-L650)。先处理可见性（visibility）合并，再用 `shouldReplace` 判断「新定义是否优于现有定义」（比较绑定强度、是否更具体等），通过则 `overwrite` 把字段抄进当前对象。注意 `*this` 指针不变——这就是就地覆盖。

**（3）`resolve(LazySymbol)`：触发 archive 抽取**

这是最值得读的一段，见 [lld/ELF/Symbols.cpp:652-687](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Symbols.cpp#L652-L687)。当现有符号是未定义、且新来的是惰性符号时：

- 先排除弱引用的特殊处理（弱未定义不抽取库成员，见 L675-L681 的注释）；
- 随后调用 `other.extract(ctx)`（L684）——这一步把静态库里对应的 `.o` 成员真正读进来，把它定义的所有符号都加入符号表，从而让这个原本「惰性」的定义变成「已定义」。

这正是命令行 `-u`、入口符号（`--entry`）能从 archive 中拉出实现的底层机制。`resolve(Undefined)`（[L423 起](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Symbols.cpp#L423)）与 `resolve(SharedSymbol)`（[L689 起](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Symbols.cpp#L689)）同理，按 (现有, 新来) 的组合各自处理。

#### 4.2.4 代码实践

1. **实践目标**：观察 archive 抽取与重复符号错误这两类典型行为。
2. **操作步骤**：
   - 写两个 `.c`：`a.c` 里 `main` 调用 `foo()`；把 `foo` 的实现放进 `b.c`，用 `llvm-ar rcs libb.a b.o` 打成静态库。
   - 用 `ld.lld` 链接 `a.o` 并在命令行末尾给出 `libb.a`，观察链接成功。
   - 再写一个 `c.c` 也定义 `foo`（强符号），与 `b.c` 同时参与链接，观察报错。
3. **需要观察的现象**：第一种情况链接器自动从 `libb.a` 抽出 `b.o`；第二种情况报 `duplicate symbol: foo`。
4. **预期结果**：分别对应 `resolve(LazySymbol)` 的 `extract` 与 `Driver.cpp` 中 `reportDuplicate`（见 4.3.3 的 `ctx.duplicates` 处理）。
5. **若本地无法构建**：标注「待本地验证」。可改为阅读 [Driver.cpp 中 `handleUndefined`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.cpp#L3273-L3275) 这段，理解入口符号如何驱动 archive 抽取。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `Symbol` 用就地覆盖而不是「替换指针」？
  - **参考答案**：符号对象可能被多处持有引用（输入段的重定位、其他符号的别名等）。就地覆盖让指针永远有效，避免遍历全表更新引用；代价是 `Symbol` 必须能容纳所有种类的字段（用一个联合/最大尺寸结构）。

- **练习 2**：弱符号（weak symbol）在解析中有什么特殊待遇？
  - **参考答案**：弱定义可被强定义覆盖而不报重复错误；弱未定义引用**不会**触发 archive 抽取（见 `resolve(LazySymbol)` 中 `isWeak()` 分支），运行时若找不到定义则解析为 0。

---

### 4.3 段合并与输出文件生成

#### 4.3.1 概念说明

符号决议完成后，链接器知道「哪些代码和数据要保留」，接下来要把它们**组装成输出文件**。这一步要回答三个问题：

1. **要保留哪些段？** 并非所有输入段都会进入输出——未被引用的（比如垃圾回收掉的）会被丢弃。
2. **输入段如何归并到输出段？** 比如 100 个 `.o` 各自的 `.text` 要合并成输出文件里一个 `.text`。
3. **每个输出段放在文件的什么位置？** 即地址分配（address assignment），还要处理对齐、程序头（program header / PT_LOAD）等。

LLM 的 ELF 驱动把这三件事编排成一条 `link` 流水线，最终交给 `Writer` 落盘。

#### 4.3.2 核心流程

`link` 是一个模板函数（按 `ELFT` 即 32/64 位与大/小端特化），其主干可归纳为：

```
link(args):
  1. parseFiles(files)              # 解析所有输入文件，填符号表（触发 4.2 的 resolve）
  2. compileBitcodeFiles()          # 若有 bitcode 输入，跑 LTO 产出真 .o（衔接 u8-l2）
  3. aggregate sections             # 把各输入段汇拢到 ctx.inputSections
  4. markLive()                     # 垃圾回收：从入口/GC roots 标记存活段
  5. processSectionCommands()       # 按链接脚本把输入段归入输出段
     addOrphanSections()            # 脚本没管的「孤儿段」按默认规则归位
  6. (可选) ICF                     # 等价代码折叠：合并完全相同的函数体
  7. writeResult(ctx)               # 交给 Writer：排序→分配地址→写文件
```

`Writer::run` 则负责最后的物理布局：

```
Writer::run():
  finalizeSections()     # 填充合成段（.got/.plt/字符串表）、排序输出段、分配地址
  maybeCompress()        # 可选：压缩调试段
  assignFileOffsets()    # 把虚拟地址映射到文件偏移
  setPhdrs()             # 生成程序头表（PT_LOAD 等）
  checkSections()        # 校验大小/对齐约束
  openFile() + write()   # 落盘
```

#### 4.3.3 源码精读

**（1）ELF 驱动入口 `elf::link`**

```cpp
bool link(ArrayRef<const char *> args, ...) {
  auto *context = new Ctx;
  ...
  LinkerScript script(ctx);
  ctx.script = &script;
  ctx.symtab = std::make_unique<SymbolTable>(ctx);
  ...
  ctx.driver.linkerMain(args);
  return errCount(ctx) == 0;
}
```

见 [lld/ELF/Driver.cpp:118-140](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.cpp#L118-L140)。注意这里创建了贯穿整个链接过程的三大对象：`Ctx`（链接上下文，集中持有所有状态）、`LinkerScript`（解析 `SECTIONS` 命令）、`SymbolTable`。返回值是「是否有错误」，与 4.1 派发层 `unsafeLldMain` 中 `r = !d(...)` 的取反约定对应。

**（2）`linkerMain`：解析参数 → 装配 → 调用 link**

`linkerMain`（[L633-L713](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.cpp#L633-L713)）是编排中枢。关键几步：

- L634-L635：用 `ELFOptTable`（[Driver.h:21-25](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.h#L21-L25)）解析命令行，选项表来自 `Options.td` 经 `llvm-tblgen` 生成的 `Options.inc`（见 [Driver.h:28-33](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.h#L28-L33) 的 `OPT_xxx` 枚举）。
- L701-L708：`initLLVM()` → `createFiles(args)`（读取输入文件列表）→ `inferMachineType()`（推断目标架构）→ `setConfigs()` → `checkOptions()`。
- L712：`invokeELFT(link, args)`——这是按推断出的 `ELFT`（ELF32LE/ELF32BE/ELF64LE/ELF64BE 之一）实例化模板 `link`，进入真正的主流水线。

**（3）`link` 主流水线关键节点**

进入模板函数 `link`（[L3245 起](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Driver.cpp#L3245)）：

- L3260：`parseFiles(ctx, files)`——逐个解析输入文件，这里会回调 `SymbolTable::addSymbol`，触发 4.2 的全部符号决议；遇到 archive 还会动态抽取成员。
- L3314-L3318：若有 bitcode 输入，先把 LTO 可能引用的 libcall 符号加入链接。
- L3325-L3341：并行做 `postParse`，并汇报 `ctx.duplicates` 里的重复符号错误（`reportDuplicate`）——这正是 4.2 实践中「两个强符号同名」报错的落点。
- L3402：`compileBitcodeFiles<ELFT>(skipLinkedOutput)`——**衔接 u8-l2 的 LTO**：把位码编译成真实目标文件，补充回 `ctx.objectFiles`。
- L3454-L3472：「Aggregate sections」——把所有输入文件里的段汇拢进 `ctx.inputSections`，异常处理段（`.eh_frame`）单独进 `ctx.ehInputSections`。
- L3525-L3529：`splitSections` 拆分可合并段，`markLive<ELFT>(ctx)` 做垃圾回收（从 `_start`/入口等根出发标记可达段）。
- L3558-L3564：`processSectionCommands()` 按 `SECTIONS` 脚本把输入段归入输出段，`addOrphanSections()` 处理脚本未提及的「孤儿段」（默认按段名归入同名输出段）。
- L3581-L3584：若开启 ICF（Identical Code Folding），执行 `doIcf` 合并等价函数体。
- L3597：`writeResult<ELFT>(ctx)`——交给 Writer。

**（4）`writeResult` → `Writer::run`：落盘**

```cpp
template <class ELFT> void elf::writeResult(Ctx &ctx) {
  Writer<ELFT>(ctx).run();
}
```

见 [lld/ELF/Writer.cpp:100-102](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Writer.cpp#L100-L102)。`Writer::run`（[L301-L355](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Writer.cpp#L301-L355)）把前面准备好的输出段真正写出来：

- L306：`finalizeSections()`（定义在 [L1814](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Writer.cpp#L1814)）——填充 `.got`/`.plt`/字符串表等合成段，调用 `sortSections()`（[L1279](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/lld/ELF/Writer.cpp#L1279)）排定输出段顺序，再分配地址。
- L312-L313：可选压缩调试段。
- L324：`assignFileOffsets()`——把虚拟地址映射成文件偏移。
- L328：`setPhdrs()`——生成程序头表（决定加载器如何把段 mmap 进内存）。
- L339-L340：`checkSections()` 校验段大小/对齐是否合法。
- L349：`openFile()`，随后写出字节。

至此，磁盘上多出一个可执行文件或共享库。

#### 4.3.4 代码实践

1. **实践目标**：用 `readelf`/`nm` 解读链接器「合并段、分配地址」的结果。
2. **操作步骤**：写一个最小程序 `main.c`（`int main(){return 0;}`），`clang -c main.c` 得到 `main.o`，再 `ld.lld -o a.out main.o -lc`（或直接 `clang -fuse-ld=lld main.c` 让 clang 调用 ld.lld）。然后：
   - `readelf -S a.out` 看输出段表（`.text`/`.data`/`.bss` 等）。
   - `readelf -l a.out` 看程序头（`PT_LOAD` 段）。
   - `nm a.out` 看最终符号表，确认每个符号都有了具体地址。
3. **需要观察的现象**：多个输入的 `.text` 合并成一个输出 `.text`；程序头把可加载段按权限（读/写/执行）分组；符号地址落在对应段范围内。
4. **预期结果**：能解释「输出段 = 多个输入同名段合并」「PT_LOAD 决定运行时内存映像」。
5. **若本地无法链接 libc**：标注「待本地验证」。可退化为源码阅读实践：在 `Writer::run` 的 `finalizeSections` 与 `assignFileOffsets` 之间想象数据流——段集合先有顺序与大小、再有虚拟地址、最后才有文件偏移。

#### 4.3.5 小练习与答案

- **练习 1**：垃圾回收（`markLive`）和 ICF 都会减少输出体积，它们有何区别？
  - **参考答案**：`markLive` 从 GC 根出发做可达性分析，丢弃**完全没人引用**的段；ICF 则针对**被引用但函数体逐字节相同**的多个函数，让它们共享一份代码。前者去掉死代码，后者合并重复活代码。

- **练习 2**：为什么「分配地址」与「分配文件偏移」要分成两步（`finalizeSections` 里给地址，`assignFileOffsets` 给偏移）？
  - **参考答案**：虚拟地址取决于段的对齐与程序头布局（运行时内存映像），文件偏移则还受文件内紧凑排布与 `p_align` 约束影响。先把内存布局定死，再据此映射到文件，逻辑更清晰、也便于处理「一个 PT_LOAD 对应文件中一段连续区域」这类约束。

---

## 5. 综合实践

把本讲三个模块串成一个完整任务：**亲手走一遍「源码 → 目标文件 → 链接 → 可执行文件」并解读每一步**。

1. **准备多文件工程**：
   - `main.c`：`extern int add(int,int); int main(){ return add(2,3); }`
   - `add.c`：`int add(int a,int b){ return a+b; }`
   - 把 `add.c` 打成静态库：`clang -c add.c && llvm-ar rcs libadd.a add.o`。
2. **编译与链接**：
   - `clang -c main.c`
   - `ld.lld -o prog main.o -L. -ladd`（让链接器从 `libadd.a` 里找 `add`）。
3. **解读派发**：运行前先想——`ld.lld` 这个名字如何让 LLD 进入 ELF 驱动？回看 4.1 的 `parseProgname`。
4. **解读符号解析**：用 `nm main.o` 看到 `add` 是 `U`（未定义）；链接后 `nm prog` 中 `add` 有了地址。解释：这正是 `resolve(LazySymbol)` 触发 `extract`，把 `libadd.a` 里的 `add.o` 抽出来的结果（4.2）。
5. **解读段合并**：`readelf -S prog`，确认 `main.o` 与 `add.o` 的 `.text` 合并进了同一个输出 `.text`；`readelf -l prog` 看 `PT_LOAD` 如何把段组织成运行时映像（4.3）。
6. **（可选）引入 LTO**：把 `add.c` 编成位码 `clang -flto -c add.c`，重新链接并观察 `compileBitcodeFiles` 这一步如何把位码变成可链接的目标（衔接 u8-l2）。

> 若本地无完整工具链，请将每步的「预期现象」标注为「待本地验证」，并把重点放在阅读 `Driver.cpp` 的 `link` 流水线与 `Writer.cpp` 的 `run` 上——即把上面 6 步在源码里逐行对上号。

## 6. 本讲小结

- LLD 是 LLVM 的「modular cross platform linker」，**一个二进制装了 ELF/COFF/Mach-O/Wasm 四套链接器**，靠 `argv[0]` 或 `-flavor` 派发，映射表是 `LLD_ALL_DRIVERS`。
- 派发的本质是「查 `Flavor` → 找函数指针 → 调用」，跨过 `unsafeLldMain` 里那一行 `d(argsV, ...)` 就进入了真正的格式驱动。
- ELF 驱动以 `elf::link` 为入口，经 `linkerMain` 编排（解析参数、读文件、推断架构），再以 `invokeELFT(link, args)` 进入模板化的主流水线。
- **符号解析**由全局 `SymbolTable` 承担：`addSymbol` = `insert` + `resolve`，采用就地覆盖；`resolve(LazySymbol)` 会从静态库抽取成员，这是「自动找库实现」的原理。
- **段合并与输出**在 `link` 流水线里完成：汇拢段 → `markLive` 垃圾回收 → 按脚本归并输出段 → 可选 ICF → `writeResult` 交 `Writer` 排序、分配地址、写文件。
- LLD 在性能上比传统 GNU ld 快得多，得益于大量并行（`parallelForEach`）、惰性 archive 抽取与紧凑的内存布局；它也是 ThinLTO 等现代特性的承载者。

## 7. 下一步学习建议

- **深入某一格式驱动**：本讲以 ELF 为主线，建议你对照阅读 `lld/COFF/Driver.cpp` 或 `lld/MachO/Driver.cpp`，体会四套驱动「同构不同体」的设计。
- **链接脚本（Linker Script）**：`processSectionCommands` 与 `addOrphanSections` 的背后是 `lld/ELF/LinkerScript.cpp`，学习 `SECTIONS` 命令能让你掌控段布局与地址分配。
- **合成段与重定位**：`Writer::finalizeSections` 里创建的 `.got`/`.plt` 与目标相关的重定位应用（`lld/ELF/Target.cpp` / `Arch/`）是后端知识的延伸，可结合 u6-l4 的 MC 层一起读。
- **回到 LTO**：结合 u8-l2，细读 `compileBitcodeFiles` 调用的 `lld/ELF/LTO.cpp`，理解链接器如何驱动 LLVM 的 LTO API。
- **测试**：LLD 的行为规格几乎全写在 `lld/test/ELF/*.test`（lit + FileCheck）里，挑几个与符号解析、GC、ICF 相关的测试阅读，是验证理解的最佳途径（与 u9-l1 测试体系相呼应）。
