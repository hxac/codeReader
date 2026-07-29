# SystemWorld：编译器与操作系统的桥梁

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `World` trait 在 Typst 架构中扮演的角色，以及为什么 `SystemWorld` 是 CLI 与编译器核心之间的「核心桥梁」。
- 读懂 `SystemWorld` 的字段构成，并把 `World` trait 的 7 个方法逐一对应到这些字段。
- 理解 `SystemFiles` 如何解析**项目根**、**主文件**，以及 stdin 与空输入分别用哪个特殊 `FileId`。
- 解释 `FileLoader` 如何按 `VirtualRoot` 分发到项目目录或包目录加载文件，以及 `DiagnosticWorld::name` 如何把内部 `FileId` 翻译成人类可读的相对路径。
- 画出 `SystemWorld::new` 的构造流程（线程池、inputs、features、时间），并认识 `WorldCreationError` 的错误模型与 watch 模式下的 `reset` / `dependencies` / `scan_fonts` 生命周期。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 编译器是「纯逻辑」，不认识磁盘

Typst 的编译器核心（`typst` / `typst-library` crate）只负责「给我一份文档内容、给我字体、给我今天日期，我就能把它编译成排版结果」。它**刻意不直接读写文件系统、不直接联网下载包**。这样做的好处是：同一份编译器核心，既能被命令行（`typst-cli`）使用，也能被 Web 编辑器、语言服务器（LSP）复用。

那么编译器要读文件时怎么办？它定义了一个接口（trait），让「宿主」来填空：

> 「我（编译器）需要某个文件，你（宿主）把字节给我。」

这个接口就是 `World` trait。`SystemWorld` 就是 `typst-cli` 为这个 trait 写的「操作系统版」实现——它把磁盘、字体、包、系统时间等真实资源，包装成编译器能消费的形态。

### 2.2 三种「路径」必须分清

本讲会反复出现三个层级的路径概念，初学者最容易混淆：

| 概念 | 是什么 | 例子 |
| --- | --- | --- |
| 真实路径（real path） | 操作系统里真实存在的路径 | `/home/me/doc/main.typ` |
| 虚拟路径 `VirtualPath` | 脱离具体磁盘的、跨平台规范化路径 | `/main.typ`（总是正斜杠、无盘符） |
| `FileId` | 把「根 + 虚拟路径」**全局驻留（intern）** 成一个廉价的小整数 | 一个 `NonZeroU16` |

为什么要有虚拟路径？因为 Typst 文档里的 `#include "a/b.typ"` 必须在所有平台上解析成同一个含义。Windows 的反斜杠 `\` 会带来歧义，所以 Typst 内部一律用正斜杠的虚拟路径；只有真正要读写磁盘时，才用 `realize` 把虚拟路径「具现」成真实路径。

`FileId` 更进一步：它把 `(根, 虚拟路径)` 这对值驻留成一个可 `Copy` 的小整数，方便编译器到处传递、比较、缓存。其取值范围是 `NonZeroU16`，理论上限为

\[ 2^{16}-1 = 65535 \]

个不同文件（实际几乎用不到这么多）。

### 2.3 两种「根」：项目根 与 包根

一个 `FileId` 总是落在某个**根**下。Typst 用 `VirtualRoot` 区分两种根：

- `VirtualRoot::Project`：当前 Typst 项目的根目录（即 `--root` 或主文件所在目录）。
- `VirtualRoot::Package(PackageSpec)`：某个包的根目录，例如 `@preview/foo:0.1.0` 解压后的目录。

这种「根」的隔离机制保证项目和包互不越权——项目文件不能通过 `../../` 逃出项目根，包与包之间也相互隔离。

> 本讲承接 [u1-l3 命令行参数模型](u1-l3-args-model.md)：那里讲过的 `WorldArgs`（inputs / root / font / package）、`ProcessArgs`（jobs / features）、`Input`（Path / Stdin）正是 `SystemWorld::new` 的入参。本讲回答的是：**这些参数如何被组装成一个可用的 `World`**。

## 3. 本讲源码地图

本讲聚焦 `typst-cli` 的一个文件，但要理解它必须顺带看几处跨 crate 的定义。

| 文件 | 角色 |
| --- | --- |
| [src/world.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | **主角**。定义 `SystemWorld`、`SystemFiles`、`WorldCreationError`，以及 stdin/empty 的特殊 `FileId`。 |
| [src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) | **调用方**。在 `compile` 入口构造 `SystemWorld` 并驱动单次编译。 |
| [src/watch.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs) | **消费方**。watch 主循环里调用 `scan_fonts` / `dependencies` / `reset`。 |
| `crates/typst-library/src/lib.rs` | 定义 `World` trait（编译器核心要求的接口）。 |
| `crates/typst-kit/src/files.rs` | 提供 `FileStore`（带缓存的文件存储）、`FileLoader` trait、`FsRoot`（磁盘根）。 |
| `crates/typst-syntax/src/path.rs` | 定义 `FileId` / `RootedPath` / `VirtualPath` / `VirtualRoot` / `virtualize` / `realize`。 |

> 跨 crate 的引用仅用于理解接口契约，本讲的代码精读与实践仍以 `src/world.rs` 为主。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **World trait 与 SystemWorld 结构** —— 桥梁的角色与字段。
2. **SystemFiles 与项目根/主文件/FileId 解析** —— 一次编译如何定位「入口」。
3. **FileLoader 与 DiagnosticWorld** —— 字节如何流入、`FileId` 如何变回人类路径。
4. **构造流程与生命周期** —— `SystemWorld::new` 的组装与 watch 下的复位。

---

### 4.1 World trait 与 SystemWorld 结构

#### 4.1.1 概念说明

`World` trait 是编译器核心向宿主提出的「需求清单」。核心里到处都是 `W: World` 的泛型约束；只要宿主实现了这 7 个方法，核心就能用它来编译。

`SystemWorld` 是这份清单的「操作系统版」答案：它持有标准库、字体、文件存储、当前时间，把这些真实资源接到编译器上。正因为核心只依赖 trait，CLI 之外的集成（Web、LSP）可以写自己的 `World`，互不干扰——这就是「核心 + 薄壳 CLI」分层（见 [u1-l1](u1-l1-project-positioning.md)）能成立的技术基础。

#### 4.1.2 核心流程

编译器核心在编译过程中，会**按需回调** `World` 的方法：

```text
核心需要「主文件」      →  world.main()        → 返回 FileId
核心需要某文件源码      →  world.source(id)    → 返回 Source
核心需要某文件的原始字节 →  world.file(id)      → 返回 Bytes
核心要查字体元信息       →  world.book()        → 返回 FontBook
核心要加载具体字体       →  world.font(index)   → 返回 Font
核心要算「今天」         →  world.today(offset) → 返回 Datetime
核心要标准库/作用域      →  world.library()     → 返回 Library
```

注意：核心是「拉取者」（pull），`SystemWorld` 是「被动的提供者」。`SystemWorld` 本身不主动编译，它只是把回调回答好。

#### 4.1.3 源码精读

先看 `SystemWorld` 的 4 个字段——它们恰好对应了 `World` 要回答的全部问题：

[src/world.rs:25-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38) —— `SystemWorld` 结构体：`workdir`（工作目录）、`library`（标准库）、`fonts`（**惰性**字体存储）、`files`（文件存储）、`now`（当前时间）。

其中 `fonts` 字段的类型值得注意：

[src/world.rs:31-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L31-L31) —— `LazyLock<FontStore, Box<dyn Fn() -> FontStore + Send + Sync>>`：字体存储用 `LazyLock` 包裹，**直到第一次被访问时**才调用 `discover_fonts` 扫描系统/内嵌/自定义字体。这就是「惰性发现」。

接着看 `World` trait 的实现——7 个方法几乎都是一行委托：

[src/world.rs:117-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L117-L145) —— `impl World for SystemWorld`。注意 `main()` 返回的是 `FileId`（不是路径），`source`/`file` 都委托给 `self.files`，`book`/`font` 委托给 `self.fonts`，`today` 委托给 `self.now`。

为了对照契约，可以看 trait 本身的定义（跨 crate）：

[crates/typst-library/src/lib.rs:60-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L60-L98) —— `pub trait World: Send + Sync`，上面带 `#[comemo::track]` 属性。`comemo` 是 Typst 的增量缓存框架：`track` 让 trait 的所有方法调用都被自动记忆化，相同入参直接返回缓存结果——这是 watch 模式增量编译的关键。

> 小提示：`World` 要求 `Send + Sync`，因为编译器核心内部会用 rayon 并行求值，`World` 必须能安全地跨线程共享。

#### 4.1.4 代码实践

**实践目标**：把 `World` 的 7 个方法和 `SystemWorld` 的字段一一对应起来，验证「实现 = 委托」。

**操作步骤**：

1. 打开 [src/world.rs:117-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L117-L145) 的 `impl World`。
2. 对每个方法，在草稿纸上写下「它把请求转交给哪个字段」。
3. 对照 [src/world.rs:25-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38) 的字段表，确认每个字段都至少被一个方法用到。

**需要观察的现象**：你会发现 `workdir` 字段**没有**出现在 `World` 的 7 个方法里——它只被 `DiagnosticWorld::name` 和 `workdir()` 用到（见 4.3）。这说明 `workdir` 不是给编译器核心用的，而是给「给人看的诊断输出」用的。

**预期结果**：得到一张「方法 → 字段」映射表，例如 `book() → self.fonts.book()`、`today() → self.now.today(offset)`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 返回 `FileId` 而不是文件路径字符串？

<details><summary>参考答案</summary>

因为编译器核心内部一律用 `FileId` 标识文件——它廉价（`Copy` 的 `NonZeroU16`）、可比较、可哈希、与平台无关。把「主文件」也表达成 `FileId`，核心就可以用同一套机制去定位主文件和它 `#include` 的任何子文件。至于这个 id 对应磁盘上哪个真实路径，是宿主（`SystemFiles`）的私事，核心不需要知道。

</details>

**练习 2**：`SystemWorld` 的 `fonts` 字段为什么用 `LazyLock` 而不是构造时就立即扫描字体？

<details><summary>参考答案</summary>

字体扫描（遍历系统字体目录、解析内嵌字体、扫描 `--font-path`）耗时且与系统强相关。用 `LazyLock` 把它推迟到「真正第一次被 `book()`/`font()` 访问时」才执行，有两个好处：一是 `typst info`、`typst fonts` 这类命令可以更可控地触发或避开它；二是 watch 模式下可以用 `scan_fonts()` 在计时之外提前强制扫描，避免字体扫描时间被误算进「编译耗时」（见 [src/watch.rs:51-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L51-L57)）。

</details>

---

### 4.2 SystemFiles：项目根、主文件与 FileId 解析

#### 4.2.1 概念说明

`SystemWorld` 自己不直接读磁盘文件，它把这件活外包给 `files: FileStore<SystemFiles>`。这里的 `SystemFiles`（注意：是 CLI 自己定义的，不是 `typst_kit` 里同名那个）是真正知道「项目根在哪、主文件是哪个、包从哪取」的角色。

`SystemFiles` 要回答两个关键问题：

1. **项目根（root）是什么？** —— 一切绝对路径都相对它解析。
2. **主文件（main）是哪个 `FileId`？** —— 编译器从这里开始。

它还要处理三种输入来源：真实文件路径、stdin（`-`）、以及完全没有主文件（例如某些 `typst eval` 场景）。

#### 4.2.2 核心流程

`SystemFiles::new` 的解析流程可以画成这样：

```text
输入 input（Option<&Input>）
  │
  ├─ Input::Path(path) ──► canonicalize(path)
  │        │  失败(NotFound) → WorldCreationError::InputNotFound
  │        ▼ 得到 input_path（绝对路径）
  │
  ├─ 确定 root:
  │     root = world_args.root              // --root 显式指定
  │          .or(input_path.parent())       // 否则取主文件所在目录
  │          .unwrap_or(".")                // 兜底：当前目录
  │     canonicalize(root)
  │        │  失败(NotFound) → WorldCreationError::RootNotFound
  │        ▼ 得到 root（绝对路径）
  │
  └─ 确定 main 的 FileId:
        ├─ 有 input_path → VirtualPath::virtualize(root, input_path) → intern()
        │                    │  逃出根/反斜杠/非UTF-8 → WorldCreationError::InputMalformed
        ├─ Input::Stdin   → *STDIN_ID   （特殊驻留 id）
        └─ 否则           → *EMPTY_ID   （特殊驻留 id）
```

其中 `virtualize` 是「真实路径 → 虚拟路径」的唯一翻译函数，它的核心动作是 `strip_prefix(root)`：如果主文件不在 root 之下，就报 `PathError::Escapes`，对应错误信息「source file must be contained in project root」。

两个特殊 id 用 `FileId::unique` 创建——它与普通 `intern` 的区别在于：`unique` **不去重**，每次调用都新建一个不可由路径反查的「假」id，专门给 stdin 这种「没有磁盘路径的内容」用。

#### 4.2.3 源码精读

先看 `SystemFiles` 的三个字段：

[src/world.rs:185-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L185-L191) —— `main: FileId`（主文件）、`project: FsRoot`（项目根）、`packages: SystemPackages`（包来源）。

接着看构造函数中**确定 root** 的三段式：

[src/world.rs:213-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L213-L225) —— `root` 的优先级：`--root` → 主文件父目录 → `"."`，随后 `canonicalize` 并把 `NotFound` 映射成 `RootNotFound`。

再看 **main 的 FileId** 三分支：

[src/world.rs:227-237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L227-L237) —— 真实路径走 `virtualize` + `intern`；stdin 走 `*STDIN_ID`；无输入走 `*EMPTY_ID`。

两个特殊 id 的定义：

[src/world.rs:167-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L167-L183) —— `STDIN_ID` 和 `EMPTY_ID` 都用 `FileId::unique` 创建，根都是 `VirtualRoot::Project`，虚拟路径分别是 `<stdin>` 和 `<empty>`。注释解释了用意：让 stdin/空输入能「住进」项目根，又不和任何真实磁盘文件撞 id。

至于 `virtualize` 为何能拦住「逃出根」的路径，看它的实现（跨 crate）：

[crates/typst-syntax/src/path.rs:208-227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L208-L227) —— 第一行 `path.strip_prefix(root_path)` 就是边界检查：主文件路径必须以 root 为前缀，否则 `PathError::Escapes`。

而 `unique` 与普通 `intern` 的区别：

[crates/typst-syntax/src/path.rs:100-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L100-L153) —— `new`（即 `intern`）会先在全局 interner 里查重复用；`unique` 则跳过查重，直接分配新 id。两者都通过 `Box::leak` 把 `RootedPath` 驻留成 `&'static`，因此 id 在进程生命期内一直有效。

#### 4.2.4 代码实践

**实践目标**：对比「从文件路径编译」与「从 stdin 编译」时，主文件的 `FileId` 与诊断标签有何不同。

**操作步骤**：

1. 准备一个故意带语法错误的最小文档（这样能触发诊断、看到主文件名）：
   ```typst
   #let x =
   ```
   存为 `bad.typ`。
2. 从文件路径编译（输出到 `/dev/null` 只为看诊断）：
   ```bash
   ./target/debug/typst compile bad.typ /dev/null
   ```
3. 从 stdin 编译同样内容：
   ```bash
   printf '#let x =\n' | ./target/debug/typst compile - /dev/null
   ```
4. 再做一个「主文件不在 root 下」的尝试（用 `--root` 把 root 设成别的目录）：
   ```bash
   ./target/debug/typst compile --root /tmp bad.typ /dev/null
   ```
   （`bad.typ` 在当前目录，不在 `/tmp` 下。）

**需要观察的现象**：

- 步骤 2 的诊断里，出错文件被标成 `bad.typ`（相对当前目录的真实路径）。
- 步骤 3 的诊断里，出错文件被标成与 `<stdin>` 相关的标签（因为 `main` 是 `STDIN_ID`，经 `DiagnosticWorld::name` 翻译，详见 4.3）。
- 步骤 4 报错：`source file must be contained in project root`——这正是 `virtualize` 里 `strip_prefix` 失败的结果。

**预期结果**：你会直观看到三种 `main` 的差异——真实文件、stdin、越界文件，分别对应正常编译、`<stdin>` 标签、`InputMalformed(Escapes)` 错误。若你的环境里路径解析表现与描述不同（例如 `--root` 行为受符号链接影响），请以本地实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果不传 `--root`，项目根默认是谁？

<details><summary>参考答案</summary>

默认是**主文件所在目录**（`input_path.parent()`）；如果没有主文件（stdin/空输入），则兜底成当前工作目录 `"."`。见 [src/world.rs:213-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L213-L225)。

</details>

**练习 2**：`STDIN_ID` 为什么用 `FileId::unique` 而不是 `RootedPath::intern`？

<details><summary>参考答案</summary>

`intern` 会按 `(root, vpath)` 去重——如果用 `intern` 创建 `<stdin>`，那么文档里若恰好有一个名为 `<stdin>` 的真实文件（或多次创建），id 就会被复用或撞车。`unique` 保证这个 id 是「独一份」的，不会被任何按路径构造的 id 命中，从而让 stdin 内容安全地寄生在项目根里。见 [crates/typst-syntax/src/path.rs:130-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L130-L153) 的注释。

</details>

**练习 3**：为什么 `virtualize` 要拒绝反斜杠 `\`？

<details><summary>参考答案</summary>

因为 Windows 用 `\` 作路径分隔符，而 Typst 的虚拟路径统一用正斜杠。若允许 `\`，同一个文档在 Windows 与 Linux 上会解析成不同结果，破坏跨平台可复现性。该错误对应 `PathError::Backslash`，显示为「source path must not contain a backslash」。见 [crates/typst-syntax/src/path.rs:618-628](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L618-L628)。

</details>

---

### 4.3 FileLoader 与 DiagnosticWorld：字节流入与路径流出

#### 4.3.1 概念说明

上一节解决了「主文件是哪个 id」；这一节解决两件事：

1. **字节流入**：编译器拿着一个 `FileId` 来要字节时，`SystemFiles`（作为 `FileLoader`）怎么把字节读出来。它要区分三种情况——普通项目文件、包文件、以及 stdin/empty 这两个特殊 id。
2. **路径流出**：编译器报错时，诊断信息里要显示一个「人能看懂」的文件名。但核心手里只有 `FileId`，需要一个反向翻译：`DiagnosticWorld::name(id) -> String`。

`FileLoader` 是 `typst_kit` 提供的 trait，`FileStore` 会把它的 `load` 结果缓存起来，避免重复读盘；`DiagnosticWorld` 也是 `typst_kit` 提供的 trait，它在 `World` 之上加了一个 `name` 方法，专门给诊断输出用。

#### 4.3.2 核心流程

**字节流入**（`FileLoader::load`）的决策树：

```text
load(id):
  ├─ id == EMPTY_ID  → 返回空字节 Bytes::new([])
  ├─ id == STDIN_ID  → read_from_stdin()（一次性读完 stdin）
  └─ 其它:
       根据 id.root() 选根:
         ├─ Project  → self.project (FsRoot) 加载 id.vpath()
         └─ Package(spec) → self.packages.obtain(spec) 得到 FsRoot 再加载
```

这里的关键设计是：`SystemFiles` 复用了 `typst_kit` 里的 `FsRoot`（磁盘根）和包机制。它其实是 `typst_kit::files::SystemFiles` 的「加强版」——多了 `main` 字段和 stdin/empty 的特判。对比 [crates/typst-kit/src/files.rs:317-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L317-L322) 的 `typst_kit` 版 `FileLoader`，CLI 版多了前两个 `if` 分支。

**路径流出**（`DiagnosticWorld::name`）的翻译规则：

```text
name(id):
  vpath = id.vpath()
  match id.root():
    Project  → 先 realize(项目根) 得到真实路径，
               再 diff_paths(它, workdir) 转成「相对工作目录」的路径；
               若失败则回退成 vpath 本身的字符串（如 <stdin>）
    Package(spec) → "{spec}{vpath}"（形如 @preview/foo:0.1.0/some.typ）
```

`pathdiff::diff_paths` 是纯词法运算（不检查文件是否存在），它把项目根下的绝对路径换算成相对当前工作目录的路径，这样诊断里显示的就是 `doc/main.typ` 而不是 `/home/me/project/doc/main.typ`，更短更友好。

#### 4.3.3 源码精读

先看 `FileLoader` 实现——三个分支：

[src/world.rs:260-270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L260-L270) —— `EMPTY_ID` 返回空、`STDIN_ID` 读 stdin、其余委托给 `self.root(id)?.load(id.vpath())`。`root(id)` 是「按 `VirtualRoot` 选 `FsRoot`」的分流器。

看分流器本身：

[src/world.rs:246-257](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L246-L257) —— `resolve` 把 id 变成真实 `PathBuf`；`root` 按 `id.root()` 在 `self.project` 和 `self.packages.obtain(spec)` 之间二选一。包路径由 `SystemPackages` 负责定位（见 [u3-l2 包存储](u3-l2-package-storage.md)）。

stdin 的读取细节：

[src/world.rs:273-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L273-L282) —— `read_from_stdin` 用 `read_to_end` 一次性读完，并特判 `BrokenPipe`（把坏管道当成功结束，避免 `typst compile - | head` 这类管道提前关闭时报错）。

再看诊断路径翻译：

[src/world.rs:147-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L147-L165) —— `impl DiagnosticWorld for SystemWorld` 的 `name`。注意 `Project` 分支里 `realize(self.root())` 用的 `self.root()` 返回的是**项目根**（`self.files.loader().project.path()`），不是工作目录；只有后续 `diff_paths` 才用到 `workdir`。

为了对照「`name` 是 `World` 之外加的方法」，看 trait 定义：

[crates/typst-kit/src/diagnostics.rs:23-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L23-L27) —— `pub trait DiagnosticWorld: World`，它在 `World` 之上加了 `fn name(&self, id: FileId) -> String`。诊断打印器（codespan）正是靠它把 span 对应的文件名显示出来。

#### 4.3.4 代码实践

**实践目标**：观察 `name()` 如何把同一个 `FileId` 在「项目文件」和「包文件」下显示成不同的字符串。

**操作步骤**：

1. 在项目里建一个子目录文件，并制造错误，看相对路径显示：
   ```bash
   mkdir -p sub && printf '#let x =\n' > sub/bad.typ
   ./target/debug/typst compile sub/bad.typ /dev/null
   ```
2. 如果你本地有可用的包（或参考 [u3-l2](u3-l2-package-storage.md) 准备一个 `@local` 包），在文档里 `#import` 它并触发一个包内错误，观察包文件名是否形如 `@scope/name:ver/path.typ`。
3. 对照 [src/world.rs:147-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L147-L165)，确认步骤 1 走的是 `VirtualRoot::Project` 分支、步骤 2 走的是 `VirtualRoot::Package` 分支。

**需要观察的现象**：步骤 1 的诊断里，文件名应是相对当前工作目录的 `sub/bad.typ`（而不是绝对路径）。若你从别的目录用绝对路径调用 typst，`diff_paths` 会尽力换算成相对路径；换算不出时回退成虚拟路径字符串。

**预期结果**：确认 `name()` 的输出与你的工作目录相关——这正是 `workdir` 字段存在的意义。包相关的步骤若本地无网络/无包，可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `read_from_stdin` 要把 `BrokenPipe` 当作成功？

<details><summary>参考答案</summary>

常见用法是 `typst compile - output.pdf | something`，下游管道（如 `head`）可能在 typst 还没写完 stdout 前就关闭了读端，导致写端收到 `BrokenPipe`。把这种情况视为「正常结束」而非错误，能避免误导性的报错。见 [src/world.rs:276-280](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L276-L280)。

</details>

**练习 2**：`name()` 的 `Project` 分支里，`realize` 和 `diff_paths` 各自的职责是什么？

<details><summary>参考答案</summary>

`realize(self.root())` 把虚拟路径「具现」成相对**项目根**的真实绝对路径（`<root>/<vpath>`）；`diff_paths(..., self.workdir())` 再把这个绝对路径换算成相对**当前工作目录**的路径。两步合起来，让诊断显示一个简短、贴近用户视角的相对路径。见 [src/world.rs:153-158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L153-L158)。

</details>

---

### 4.4 构造流程与生命周期：SystemWorld::new 与错误模型

#### 4.4.1 概念说明

前面三节分别看了「字段」「入口解析」「IO 与诊断」。这一节把它们串起来，看 `SystemWorld::new` 如何从命令行参数（[u1-l3](u1-l3-args-model.md)）一次性组装出一个可用的 `World`，以及它暴露给上层（compile / watch）的几个生命周期方法。

`SystemWorld::new` 做四件事：搭线程池、装标准库（含 inputs/features）、确定时间、构造 `SystemFiles`。任何一步失败都会变成 `WorldCreationError`，被上层转成错误信息打印。

#### 4.4.2 核心流程

```text
SystemWorld::new(input, world_args, process_args):
  1. 若指定了 --jobs，配置全局 rayon 线程池（并发编译用）
  2. 组装 library:
       inputs  = world_args.inputs → Dict（--input key=value）
       features = process_args.features → typst::Feature（--features）
       Library::builder().with_inputs(..).with_features(..).build()
  3. 确定 now:
       有 --creation-timestamp → Time::fixed_timestamp（可复现）
                  越界/非法   → WorldCreationError::InvalidTimestamp
       否则                   → Time::system()（真实系统时间）
  4. 构造 SystemFiles（见 4.2，可能产出 InputNotFound/RootNotFound/InputMalformed）
  5. 返回 SystemWorld { workdir, library, fonts(惰性), files, now }
```

构造完成后，`SystemWorld` 还提供三个生命周期方法，主要服务于 watch 模式：

- `reset()`：把文件存储标记为「过期」（下次访问重新读盘），并复位时间（若非固定时间）。watch 每轮重编译前调用。
- `dependencies()`：返回「自上次 reset 以来被访问过的所有文件 id 解析成的真实路径」，watch 用它更新文件监听集合；`deps` 命令也用它写出依赖清单。
- `scan_fonts()`：强制立即扫描字体（否则要等到首次访问），让字体扫描耗时不算进编译计时。

watch 主循环把三者串成了一个固定节拍：

```text
scan_fonts()              # 首轮前预扫字体
compile_once()            # 首次编译
loop:
  watcher.update(dependencies())   # 监听本轮依赖
  watcher.wait()                  # 等待变更
  reset()                         # 复位状态
  compile_once()                  # 重编译
  comemo::evict(10)               # 清理增量缓存
```

#### 4.4.3 源码精读

`SystemWorld::new` 的完整构造：

[src/world.rs:42-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L42-L85) —— 注意四个步骤的顺序：线程池（48-54）→ library（56-68）→ now（70-74）→ 组装结构体（76-84），其中 `files` 通过 `SystemFiles::new(...)?` 构造、`fonts` 用闭包惰性初始化。

inputs 与 features 的组装：

[src/world.rs:56-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L56-L68) —— `world_args.inputs`（来自 `--input key=value`）被收集成 `Dict` 注入标准库；`process_args.features` 经 `From<Feature>` 转换后启用实验特性。

`Feature` 到核心 `Feature` 的映射定义在文件末尾：

[src/world.rs:349-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L349-L357) —— CLI 的 `Feature::Html/Bundle/A11yExtras` 一一映射到 `typst::Feature`。

时间模型与可复现性：

[src/world.rs:70-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L70-L74) —— 默认用 `Time::system()`（真实时间）；传 `--creation-timestamp` 则用固定时间，使编译结果可复现（CI 友好），越界则 `InvalidTimestamp`。

错误模型：

[src/world.rs:285-297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L285-L297) —— `WorldCreationError` 的 5 个变体覆盖了「输入找不到 / 输入路径非法 / 根目录找不到 / 时间戳非法 / 其它 IO 错」。

[src/world.rs:299-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L299-L326) —— `Display` 实现把每个变体翻译成给用户看的中文/英文消息，其中 `InputMalformed` 还会进一步细分 `Escapes`/`Backslash`/`Invalid`/`Utf8` 四种子情况。

生命周期方法：

[src/world.rs:97-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L97-L114) —— `dependencies()` 委托给 `FileStore::dependencies()`（返回被访问过的 id，再用 loader 解析成路径）；`reset()` 委托给 `FileStore::reset()` 与 `Time::reset()`；`scan_fonts()` 用 `LazyLock::force` 立即触发字体扫描。

`FileStore::dependencies` 与 `reset` 的语义（跨 crate）：

[crates/typst-kit/src/files.rs:81-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L81-L116) —— `dependencies` 返回「自上次 reset 起被访问过的文件」；`reset` 不删除缓存而是把 slot 标记为 stale，下次访问时原地重新加载（这对增量编译性能更友好，因为 `Source` 对象可以被原地编辑）。

上层如何消费这些方法，看 watch 主循环：

[src/watch.rs:55-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L55-L83) —— 完整展示了「预扫字体 → 编译 → 循环(update依赖 → wait → reset → 编译 → evict)」的节拍。

而 `compile` 命令构造 `SystemWorld` 的入口：

[src/compile.rs:41-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L41-L47) —— `SystemWorld::new(Some(&command.args.input), &command.args.world, &command.args.process)`，错误用 `eco_format!` 转成字符串冒泡。

#### 4.4.4 代码实践

**实践目标**：亲手触发 `WorldCreationError` 的不同变体，并观察 `--jobs` / `--input` 如何进入 `SystemWorld`。

**操作步骤**：

1. 触发 `InputNotFound`：
   ```bash
   ./target/debug/typst compile nope.typ /dev/null
   ```
   预期：`input file not found (searched at ...)`。
2. 触发 `InvalidTimestamp`（用一个明显越界的时间戳）：
   ```bash
   ./target/debug/typst compile --creation-timestamp 99999999999999999999 hello.typ /dev/null 2>&1 || true
   ```
   预期：`creation timestamp out of range`。（具体能否触发取决于解析阶段，若该值在参数解析期就被拒，则以本地报错为准——标注「待本地验证」。）
3. 注入 `--input` 并在文档里读取，验证 inputs 真的进了 library：
   ```bash
   printf 'Hello #sys.inputs.name \n' | ./target/debug/typst compile --input name=World - out.pdf
   ```
   预期：生成的 PDF 含「Hello World」。
4. 用 `--jobs 1` 编译，观察可正常完成（验证线程池配置路径，不报错即可）。

**需要观察的现象**：步骤 1、3 的行为最稳定；步骤 2 可能因 clap 的参数解析提前拦截而表现为别的错误信息。步骤 3 是验证「inputs 注入」最直接的方式。

**预期结果**：确认 `WorldCreationError` 的消息与 [src/world.rs:299-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L299-L326) 的 `Display` 实现一致；确认 `--input` 的值能在文档里通过 `sys.inputs` 读到。

#### 4.4.5 小练习与答案

**练习 1**：为什么 watch 模式下每轮重编译前要调用 `reset()`，而不是直接新建一个 `SystemWorld`？

<details><summary>参考答案</summary>

新建 `SystemWorld` 会丢掉所有缓存的 `Source` 对象，导致增量编译退化成全量编译。`reset()` 只是把这些文件 slot 标记为 stale（「下次访问时重新读盘」），`FileStore` 仍保留原有 `Source` 并在原地更新内容，从而让 comemo 的增量缓存能继续命中。见 [crates/typst-kit/src/files.rs:101-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L101-L116)。

</details>

**练习 2**：`dependencies()` 返回的路径集合是如何被 watch 使用的？如果某轮编译少 import 了一个文件，会发生什么？

<details><summary>参考答案</summary>

watch 用 `watcher.update(world.dependencies())` 把「本轮实际访问过的文件」设为监听对象（见 [src/watch.rs:70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L70)）。如果某轮不再 import 某文件，它就不在新的依赖集合里，watcher 会停止监听它——这是合理的，因为编辑一个不再被引用的文件不应触发重编译。

</details>

**练习 3**：`now` 字段为什么存在 `SystemWorld` 里，而不是每次 `today()` 调用时现取系统时间？

<details><summary>参考答案</summary>

为了保证「同一次编译内时间一致」——若每次都现取，文档里多次调用 `datetime` 可能拿到不同的值，导致输出不稳定。把 `now` 存进 world，并在非固定时间模式下于每次 `reset()` 时才刷新，既保证单次编译一致性，又让 watch 多轮之间能更新时间。见字段注释 [src/world.rs:34-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L34-L37)。

</details>

## 5. 综合实践

把本讲的四块知识串成一个完整任务：搭建一个最小多文件项目，观察 `SystemWorld` 从构造到依赖追踪的全过程。

**任务**：在空目录里创建如下结构：

```text
proj/
├── main.typ
└── lib.typ
```

`main.typ` 内容：

```typst
#import "lib.typ": greeting
#greeting

#if sys.inputs.mode == "draft" [Draft build] else [Final build]
```

`lib.typ` 内容：

```typst
#let greeting = [Hello from lib!]
```

**操作**：

1. 进入 `proj/`，分别用两种方式编译，对照 4.2 的 `main` 解析：
   ```bash
   # 方式 A：文件路径
   ../target/debug/typst compile main.typ a.pdf
   # 方式 B：stdin（把 main.typ 喂给 stdin）
   ../target/debug/typst compile --root . - b.pdf < main.typ
   ```
   观察两者是否都能正确解析 `#import "lib.typ"`（stdin 模式下，相对 import 相对谁解析？结合 `STDIN_ID` 的 `VirtualRoot::Project` 思考）。
2. 用 `--deps` 导出依赖，对照 4.4 的 `dependencies()`：
   ```bash
   ../target/debug/typst compile --deps - --deps-format json main.typ /dev/null
   ```
   预期 `inputs` 里包含 `main.typ` 和 `lib.typ` 两个真实路径（顺序任意）。
3. 启动 watch，修改 `lib.typ`，验证它能触发重编译：
   ```bash
   ../target/debug/typst watch main.typ out.pdf
   # 在另一个终端编辑 proj/lib.typ，观察 typst 是否自动重编译
   ```
   对照 [src/watch.rs:68-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L68-L83) 理解：正是因为 `lib.typ` 出现在 `dependencies()` 里，watcher 才会监听它。
4. 注入 input 验证 library 组装：
   ```bash
   ../target/debug/typst compile --input mode=draft main.typ draft.pdf
   ```
   预期输出含「Draft build」。

**验收点**：你能用一句话解释「为什么改 `lib.typ` 能触发 watch 重编译」——因为它被编译器通过 `source()` 访问，从而进入 `FileStore` 的依赖集合，被 `SystemWorld::dependencies()` 吐出，最终被 watcher 监听。这条链路正是本讲四个模块的汇合点。

## 6. 本讲小结

- `World` trait 是编译器核心向宿主提出的接口契约；`SystemWorld` 是 `typst-cli` 的「操作系统版」实现，把磁盘、字体、包、时间接给核心。它是 CLI 与核心之间的**核心桥梁**。
- `SystemWorld` 的 5 个字段（`workdir`/`library`/`fonts`/`files`/`now`）分别支撑了 `World` 的 7 个方法和诊断输出；其中 `fonts` 是 `LazyLock` 惰性发现。
- `SystemFiles` 负责解析**项目根**（`--root` → 主文件父目录 → `.`）和**主文件 `FileId`**（真实路径经 `virtualize`+`intern`；stdin/空输入用 `unique` 的 `STDIN_ID`/`EMPTY_ID`）。
- 文件加载按 `VirtualRoot` 分流到 `FsRoot`（项目）或 `SystemPackages`（包）；`DiagnosticWorld::name` 再把 `FileId` 反向翻译成相对工作目录的人类路径。
- `SystemWorld::new` 四步组装（线程池 / library / 时间 / SystemFiles），失败归一到 `WorldCreationError`；`reset`/`dependencies`/`scan_fonts` 是 watch 增量编译的生命周期节拍。

## 7. 下一步学习建议

- **继续主线**：进入 [u2-l2 编译配置与单次编译](u2-l2-compile-config.md)，看 `SystemWorld` 构造好之后，`compile_once` 如何驱动它完成一次「编译 → 导出 → 诊断」。
- **横向扩展**：若想深入「包从哪来」，跳到 [u3-l2 包存储与解析](u3-l2-package-storage.md)，看 `SystemPackages` 如何实现本讲里反复出现的 `packages.obtain(spec)`。
- **源码延伸**：想了解 `World` trait 的另一面（增量记忆化），可阅读 `crates/typst-library/src/lib.rs` 的 `#[comemo::track]` 与 `comemo` 文档；想了解文件 slot 的状态机，可精读 `crates/typst-kit/src/files.rs` 的 `FileSlot`。
