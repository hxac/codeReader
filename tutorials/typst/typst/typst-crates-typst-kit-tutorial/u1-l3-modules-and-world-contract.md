# 模块地图与 World 契约

> 本讲属于「起步与全局认识」单元（u1），承接 u1-l1（typst-kit 的定位）与 u1-l2（特性开关体系）。
> 学完本讲，你将拥有 typst-kit 的「全景地图」，并能说清它如何被拼装成一个真正的 `World`。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 typst-kit 一共有 **哪些公开模块**，以及每个模块大致负责什么。
2. 复述 Typst 编译器与外界打交道的唯一接口——`World` trait——需要回答的 **七个问题（回调）**。
3. 看懂 `typst-cli` 的 `SystemWorld` 是如何「把 typst-kit 的积木拼起来」去实现这七个回调的，并能画出「World 方法 ↔ typst-kit 类型」的对应表。

一句话总结本讲的核心论点：**typst-kit 的存在意义，就是为「实现一个 `World`」提供现成的、可按需启用的零件。**

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

### 2.1 什么是 trait（接口）

Rust 的 `trait` 类似其他语言里的「接口」。它只规定「有哪些方法、方法签名是什么」，不规定具体怎么做。任何一个类型只要把 trait 要求的方法都写出来，就说它「实现了这个 trait」。

### 2.2 Typst 编译器为什么需要 `World`

Typst 编译器（核心引擎在 `typst` / `typst-library` 这两个 crate 里）只负责「把 Typst 源码编译成 PDF / HTML」，但它 **自己不去读磁盘、不联网、不看系统时间**。这些「和外部世界打交道」的事，它统统交给一个叫做 `World` 的 trait 来回答。

这样做的好处是「依赖倒置」：编译器只认 `World` 这个抽象接口，于是无论是命令行工具 `typst-cli`、编辑器插件，还是网页端，只要各自实现一个 `World`，就能驱动同一个编译器。typst-kit 的定位（u1-l1 已讲）正是「帮你实现 `World` 的现成积木」。

### 2.3 什么是「回调（callback）」

「回调」指的是：**不是你去调用编译器，而是编译器在需要时反过来调用你提供的函数。** 例如编译器需要某个文件的内容时，它会调用 `World::source(id)` / `World::file(id)`，由你的实现去把字节读出来交给它。本讲反复出现的「回调」都是这个含义。

---

## 3. 本讲源码地图

本讲只读两个最关键的文件（外加一个用于交叉验证的文件）：

| 文件 | 角色 |
| --- | --- |
| [`crates/typst-kit/src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs) | typst-kit 的「目录页」：用 `pub mod` 列出全部公开模块。 |
| [`crates/typst-cli/src/world.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | typst-cli 对 `World` 的具体实现 `SystemWorld`——把 typst-kit 积木拼起来的「成品」。 |
| `crates/typst-library/src/lib.rs`（交叉验证用） | `World` trait 的 **权威定义** 所在处，本讲引用它来核对七个回调。 |

> 说明：本讲「以源码为准」统计模块数量。下面的 `pub mod` 实际只有 **9 个**，因此本讲全文统一写作「九个公开模块」。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，层层递进：

- **4.1** typst-kit 的九个公开模块（看「积木盒」里有什么）
- **4.2** World 契约：编译一份文档需要回答的七个问题（看「插槽」）
- **4.3** 组装积木：`SystemWorld` 如何把积木插进插槽（看「成品」）

---

### 4.1 typst-kit 的九个公开模块

#### 4.1.1 概念说明

typst-kit 把能力按主题拆成了若干个 `pub mod`。每个模块都是一块相对独立的「积木」：

- 有的积木直接服务于 `World`（字体、文件、包、时间）；
- 有的积木服务于 `World` 之外的「工具链」能力（下载、热重载服务器、文件监视、性能追踪、诊断美化）。

要先建立这张全景图，后面理解 `SystemWorld` 的拼装才会顺理成章。

#### 4.1.2 核心流程

可以把九个模块粗略分成两组：

```
┌─ 直接服务于 World 的「数据源」积木 ─────────────┐
│  fonts      字体元数据 + 字体字节（→ book/font）│
│  files      文件/源码加载与缓存（→ source/file）│
│  packages   第三方包解析（→ files 的包分支）     │
│  datetime   当前日期（→ today）                 │
└──────────────────────────────────────────────────┘

┌─ 服务于「周边工具链」的积木 ────────────────────┐
│  downloader    网络下载（被 packages 间接使用） │
│  server        本地 HTTP 热重载服务器            │
│  watcher       文件系统监视（typst watch 用）    │
│  timer         性能耗时追踪（导出 tracing JSON） │
│  diagnostics   终端诊断美化输出                  │
└──────────────────────────────────────────────────┘
```

注意：这种分组不是源码里的硬性分类，而是为了帮助理解「为什么有些模块会出现在 `SystemWorld` 的字段里，有些却不会」。本讲 4.3 会用源码佐证这一点。

#### 4.1.3 源码精读

typst-kit 全部的 `pub mod` 声明集中在 `lib.rs` 末尾，一眼可数：

```rust
pub mod datetime;
pub mod diagnostics;
pub mod downloader;
pub mod files;
pub mod fonts;
pub mod packages;
pub mod server;
pub mod timer;
pub mod watcher;
```

📌 [crates/typst-kit/src/lib.rs:52-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L52-L60) ——这是 typst-kit 的「目录页」，九行 `pub mod` 即九个公开模块。

每个模块文件顶部的文档注释，进一步说明了它的职责（节选）：

| 模块 | 文件首行文档 | 对应能力 |
| --- | --- | --- |
| `datetime` | `Date and time manipulation.` 并注明 `provides the necessary building pieces for World::today` | 为 `World::today` 提供日期 |
| `fonts` | `Font loading and management.` 提供 `embedded` / `scan` / `system` 三种字体发现 | 为 `World::book` / `World::font` 提供字体 |
| `files` | `File loading and management.` | 为 `World::source` / `World::file` 提供文件 |
| `packages` | `Package loading.` | 解析第三方包，喂给 files 的包分支 |
| `downloader` | `Web requests with optional progress reporting.` 所有「可能触发下载」的功能都走 `Downloader` trait | 被包下载等间接使用 |
| `diagnostics` | `Diagnostic pretty-printing.` | 把编译诊断美化成终端输出 |
| `server` | `A minimal hot-reloading HTTP server.` | `typst watch` 的浏览器热重载 |
| `watcher` | `File system watching.` 用于实现 `typst watch` | 文件监视 |
| `timer` | `Recording and writing of performance timing files.` | 性能耗时导出 |

📌 `datetime` 模块文档明确写道它是为 `World::today` 而生，可作为「模块 ↔ World 回调」对应关系的直接佐证：[crates/typst-kit/src/datetime.rs:1-4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L1-L4)。

> ⚠️ 容易混淆的一点：`lib.rs` 的特性文档里还有一个 `bundle` 特性（[L11](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L11)），它关联的是 **外部的 `typst-bundle` crate**，**并不是** typst-kit 里的一个模块。所以「特性」和「模块」是两套东西，别因为特性列表很长就把模块数数错。特性开关体系详见 u1-l2。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手数出模块数量，并完成「模块职责」速查表。
2. **操作步骤**：
   - 打开 [crates/typst-kit/src/lib.rs:52-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L52-L60)。
   - 逐个点开每个 `pub mod` 对应的文件，阅读它的首行 `//!` 文档注释。
3. **需要观察的现象**：九个模块的文件首行各有一句「它负责什么」的文档。
4. **预期结果**：你应当得到与上表一致的九行记录，并能说出每个模块的一句话职责。
5. 待本地验证：若你在不同提交上阅读，行号可能变动，但 `pub mod` 总数应仍为九。

#### 4.1.5 小练习与答案

**练习 1**：下面哪些是 typst-kit 的「公开模块」，哪些只是「特性」？
`fonts`、`bundle`、`embedded-fonts`、`timer`、`watcher`、`vendor-openssl`。

> **答案**：模块是 `fonts`、`timer`、`watcher`（在 `lib.rs` 用 `pub mod` 声明）；`bundle`、`embedded-fonts`、`vendor-openssl` 是特性（feature flag），不是模块。

**练习 2**：`downloader` 模块自己并不出现在 `World` 的回调里，为什么 typst-kit 还要提供它？

> **答案**：因为 `packages` 在解析来自 Typst Universe 的包时需要联网下载，而 `downloader` 是「所有可能触发下载的功能」的统一出口（见其模块文档），它服务于包加载这条链路，而不是直接回答 `World` 的某个回调。

---

### 4.2 World 契约：编译一份文档需要回答的七个问题

#### 4.2.1 概念说明

`World` 是 typst-library 定义的 trait，它是「编译器与外界之间唯一的契约」。一个 `World` 实现需要回答 **七个问题**——也就是七个必须实现的方法。只要把这七个问题答好，编译器就能正常工作。

> 注意区分两个概念：`World`（trait，本节主角）和 `WorldExt`（给 `World` 加的「扩展方法」，比如根据 span 取字节范围）。`WorldExt` 不是本讲重点，但它解释了为什么你会看到 `World` 之外还有相关代码：[crates/typst-library/src/lib.rs:139-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L139-L156)。

#### 4.2.2 核心流程

把编译过程想象成「编译器向 `World` 提问」的过程，七个问题大致按下面顺序被问到：

```
编译开始
  │
  ├─ Q1 library()  : 「标准库是什么？」（拿到 std 的函数、样式默认值）
  ├─ Q2 book()     : 「你有哪些字体？」（拿到字体元数据目录）
  ├─ Q3 main()     : 「主文件是哪个？」（拿到入口 FileId）
  │
  ├─ Q4 source(id) : 「这个 .typ 文件的内容是？」（按需读源码）
  ├─ Q5 file(id)   : 「这个文件（图片/数据等）的字节是？」
  ├─ Q6 font(i)    : 「第 i 号字体的字体对象是？」（按需真正加载字体）
  │
  └─ Q7 today(off) : 「今天的日期是？」（仅当文档里用到 datetime 时）
编译结束
```

其中 Q4–Q6 都带一个参数（文件 id 或字体下标），是真正的「回调」：编译器需要时才来问，而不是一开始就全问一遍。这种「按需提问」是 typst 增量编译和懒加载的基础。

#### 4.2.3 源码精读

`World` trait 的权威定义如下（只保留方法签名与文档，省略部分注释）：

```rust
pub trait World: Send + Sync {
    /// The standard library.
    fn library(&self) -> &LazyHash<Library>;

    /// Metadata about all known fonts.
    fn book(&self) -> &LazyHash<FontBook>;

    /// Get the file id of the main source file.
    fn main(&self) -> FileId;

    /// Try to access the specified file location as a source file.
    fn source(&self, id: FileId) -> FileResult<Source>;

    /// Try to access the specified file.
    fn file(&self, id: FileId) -> FileResult<Bytes>;

    /// Try to access the font with the given index in the font book.
    fn font(&self, index: usize) -> Option<Font>;

    /// Get the current date.
    fn today(&self, offset: Option<Duration>) -> Option<Datetime>;
}
```

📌 [crates/typst-library/src/lib.rs:60-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L60-L98) ——七个方法就是 `World` 的全部契约。注意末尾还有一条 `today` 的约定：`offset` 为 `None` 时应返回本地日期，为 `Some(utc_offset)` 时应返回该 UTC 偏移下的日期；返回 `None` 则文档里的 `datetime` 函数会报错。

补充两个细节，帮助你读懂签名里的类型：

- `FileId`：编译器内部用来指代「某个文件」的稳定标识，和真实磁盘路径解耦。`main()` 返回的就是主文件的 `FileId`。
- `FileResult<T>`：本质是 `Result<T, FileError>`，表示「读文件可能失败」。这就是为什么 `source` / `file` 返回的是 `FileResult<...>` 而不是裸值。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把七个回调的「输入 / 输出 / 何时被问」记牢。
2. **操作步骤**：打开 [crates/typst-library/src/lib.rs:60-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L60-L98)，对每个方法各读一遍它上方的 `///` 文档。
3. **需要观察的现象**：每个方法上方的注释都在解释「编译器期望它返回什么」。
4. **预期结果**：你能不看代码，复述出这七个方法的名字、参数与返回类型。
5. 待本地验证：不同版本可能新增回调；以你本地该文件的最新行数为准。

#### 4.2.5 小练习与答案

**练习 1**：`book()` 返回的 `FontBook` 和 `font(index)` 返回的 `Font` 有什么分工？

> **答案**：`book()` 返回的是「所有字体的 **元数据** 目录」（字体名、字重、字形等），编译器先用它来「按名字挑出该用哪个字体、记住其下标」；`font(index)` 才是「真正把第 index 号字体的 **字节/字体对象** 加载出来」。前者是目录，后者是取货。

**练习 2**：`today()` 为什么返回 `Option<Datetime>` 而不是 `Datetime`？

> **答案**：因为 `World` 允许「不知道当前日期」（返回 `None`）。此时文档里调用 `datetime` 函数会报错。这种设计让实现者可以拒绝提供时间（例如某些受限环境）。

---

### 4.3 组装积木：SystemWorld 如何把 typst-kit 拼成 World

#### 4.3.1 概念说明

`typst-cli` 提供了一个现成的 `World` 实现——`SystemWorld`。它的字段几乎全是从 typst-kit 借来的类型，于是实现 `World` 的七个回调时，大多只是一行「转发」。本节就是要让你看清「积木是怎么插进插槽的」，这是理解 typst-kit 价值的最好证据。

#### 4.3.2 核心流程

`SystemWorld` 的拼装思路：

```
构造 SystemWorld::new()
  │
  ├─ library  ← typst::Library::builder()...build()   （来自 typst-library，不是 typst-kit）
  ├─ fonts    ← typst_kit::fonts::FontStore            （由 typst-cli 的 discover_fonts 填充）
  ├─ files    ← typst_kit::files::FileStore<SystemFiles>
  │              其中 SystemFiles 持有 FsRoot + SystemPackages
  └─ now      ← typst_kit::datetime::Time              （system() 或 fixed_timestamp()）

实现 impl World for SystemWorld：
  library()  → &self.library      （直接返回字段，非 typst-kit）
  book()     → self.fonts.book()  （转发 FontStore）
  main()     → self.files.loader().main
  source(id) → self.files.source(id)        ┐ 都转发给 FileStore
  file(id)   → self.files.file(id)          ┘
  font(i)    → self.fonts.font(index)       （转发 FontStore）
  today(off) → self.now.today(offset)       （转发 Time）
```

关键观察：**七个回调里有六个转发给 typst-kit 的类型**（`FontStore` / `FileStore` / `Time`），只有 `library()` 用的是 `typst::Library` 本身。这就是 u1-l1 所说「typst-kit 是 CLI World 的积木来源」的落地证据。

#### 4.3.3 源码精读

先看 `SystemWorld` 的结构体字段，四个核心字段里有三个是 typst-kit 类型：

```rust
pub struct SystemWorld {
    workdir: Option<PathBuf>,
    library: LazyHash<Library>,
    fonts: LazyLock<FontStore, Box<dyn Fn() -> FontStore + Send + Sync>>,
    files: FileStore<SystemFiles>,
    now: Time,
}
```

📌 [crates/typst-cli/src/world.rs:25-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38) ——注意 `fonts: ... FontStore`、`files: FileStore<SystemFiles>`、`now: Time` 三个字段直接来自 typst-kit；而 `library: LazyHash<Library>` 用的是 typst 引擎自己的 `Library`。

文件顶部的 `use` 语句进一步证明哪些 typst-kit 类型被「接线」进来了：

```rust
use typst_kit::datetime::Time;
use typst_kit::diagnostics::DiagnosticWorld;
use typst_kit::files::{FileLoader, FileStore, FsRoot};
use typst_kit::fonts::FontStore;
use typst_kit::packages::SystemPackages;
```

📌 [crates/typst-cli/src/world.rs:16-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L16-L20) ——这条 import 列表把 typst-kit 的 `datetime`、`diagnostics`、`files`、`fonts`、`packages` 五个模块接进了 `SystemWorld`。

> ⚠️ 注意对比：`downloader`、`server`、`watcher`、`timer` 这四个模块 **没有** 出现在这份 import 里。它们服务的是 `World` 之外的「周边工具链」（下载、热重载、监视、耗时追踪），所以不会作为字段出现在 `SystemWorld` 结构体中——这正好印证了 4.1 的分组。

再看 `impl World for SystemWorld`，七个回调的实现基本都是一行转发：

```rust
impl World for SystemWorld {
    fn library(&self) -> &LazyHash<Library> { &self.library }
    fn book(&self) -> &LazyHash<FontBook> { self.fonts.book() }
    fn main(&self) -> FileId { self.files.loader().main }
    fn source(&self, id: FileId) -> FileResult<Source> { self.files.source(id) }
    fn file(&self, id: FileId) -> FileResult<Bytes> { self.files.file(id) }
    fn font(&self, index: usize) -> Option<Font> { self.fonts.font(index) }
    fn today(&self, offset: Option<Duration>) -> Option<Datetime> { self.now.today(offset) }
}
```

📌 [crates/typst-cli/src/world.rs:117-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L117-L145) ——除 `library()` 外，其余六个方法体都只是「把请求转交给某个 typst-kit 类型」。

最后看构造函数 `SystemWorld::new` 里这些字段是如何被填充的（节选关键部分）：

```rust
let library = {
    let inputs: Dict = /* ... */;
    let features = /* ... */;
    Library::builder().with_inputs(inputs).with_features(features).build()
};

let now = match world_args.creation_timestamp {
    Some(time) => Time::fixed_timestamp(time).map_err(...)?,
    None => Time::system(),
};

Ok(Self {
    workdir: std::env::current_dir().ok(),
    library: LazyHash::new(library),
    fonts: LazyLock::new(Box::new(|| {
        crate::fonts::discover_fonts(&world_args.font)   // typst-cli 的封装
    })),
    files: FileStore::new(SystemFiles::new(input, world_args)?),
    now,
})
```

📌 [crates/typst-cli/src/world.rs:42-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L42-L85) ——三处要点：
>
> - `library` 由 `typst::Library::builder()` 构建（typst-library 提供），与 typst-kit 无关；
> - `now` 在用户指定时间戳时用 `Time::fixed_timestamp`（可复现构建），否则用 `Time::system()`（系统当前时间）——二者都是 typst-kit 的 `datetime::Time`；
> - `fonts` 通过 `LazyLock` 延迟到首次访问才真正调用 `discover_fonts`（懒加载，详见 u2-l1）；`discover_fonts` 本身是 typst-cli 的封装（见 [crates/typst-cli/src/fonts.rs:13-14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L13-L14)），内部用 typst-kit 的 `FontStore` 组装字体。

#### 4.3.4 代码实践（源码阅读型·本讲主任务）

1. **实践目标**：亲手完成「World 方法 ↔ typst-kit 类型」对应表。
2. **操作步骤**：
   - 对照 [crates/typst-cli/src/world.rs:117-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L117-L145) 的 `impl World`。
   - 对每一个方法，回到 [结构体字段定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38)，找出它转发的字段，再判断该字段的类型来自哪个 typst-kit 模块。
3. **需要观察的现象**：六个方法转发给 typst-kit 类型，唯独 `library()` 不转发。
4. **预期结果（参考答案表）**：

   | `World` 方法 | 返回类型 | `SystemWorld` 实现 | 来自哪个 typst-kit 模块 / 类型 |
   | --- | --- | --- | --- |
   | `library()` | `&LazyHash<Library>` | `&self.library` | **非 typst-kit**：`typst::Library`（typst-library） |
   | `book()` | `&LazyHash<FontBook>` | `self.fonts.book()` | `fonts` 模块 / `FontStore` |
   | `main()` | `FileId` | `self.files.loader().main` | `files` 模块 / `FileStore`、`SystemFiles` |
   | `source(id)` | `FileResult<Source>` | `self.files.source(id)` | `files` 模块 / `FileStore` |
   | `file(id)` | `FileResult<Bytes>` | `self.files.file(id)` | `files` 模块 / `FileStore` |
   | `font(index)` | `Option<Font>` | `self.fonts.font(index)` | `fonts` 模块 / `FontStore` |
   | `today(offset)` | `Option<Datetime>` | `self.now.today(offset)` | `datetime` 模块 / `Time` |

5. 待本地验证：若你本地源码新增/调整了回调，请以实际签名为准更新此表。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `library()` 不像其他六个方法那样转发给 typst-kit？

> **答案**：因为「标准库本体」由 typst 引擎自身（`typst-library`）提供——`Library::builder()` 就定义在那里。typst-kit 只负责「从外界取数据」（字体、文件、包、时间），标准库不属于「外界数据」，所以 `library()` 直接持有并返回 `typst::Library`，不需要 typst-kit 参与。

**练习 2**：`SystemWorld::new` 里为什么把 `fonts` 包在 `LazyLock` 里？

> **答案**：为了让字体发现「懒加载」——只有在编译器真正第一次访问字体（调用 `book()` 或 `font()`）时，才去执行较慢的字体扫描（`discover_fonts`）。如果某次编译根本不需要字体，就完全省掉扫描开销。这一机制的细节将在 u2-l1（FontStore 与懒加载）深入讲解。

**练习 3**：如果有人想替换「从哪里下载包」的策略，应该改 typst-kit 的哪个模块？它会出现在 `SystemWorld` 的字段里吗？

> **答案**：应改 `downloader` 模块（实现自定义 `Downloader`），它被 `packages` 模块在解析 Universe 包时间接使用。由于下载能力不属于 `World` 的七个回调，所以它 **不会** 作为字段出现在 `SystemWorld` 结构体里——这正好说明「并非所有 typst-kit 模块都直接服务于 `World`」。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「全局接线图」任务：

> **任务**：假设你要为一个 **网页端编辑器** 实现一个最小的 `World`（不读本地磁盘、不联网下载包、字体都内置）。请基于本讲学到的「七个回调 + 九个模块」，回答以下问题，并画出你的「World 方法 ↔ 数据来源」表。
>
> 1. 你的七个回调分别打算从哪里取数据？（哪些可以直接复用 typst-kit，哪些必须自己写？）
> 2. 你会启用 typst-kit 的哪些特性、放弃哪些特性？
> 3. 你的 `World` 与 `SystemWorld` 最大的区别会在哪几个回调上？

**参考思路**（不是唯一答案）：

- `library()`：仍可直接用 `typst::Library::builder()`，与 typst-cli 一致。
- `book()` / `font()`：网页端不扫描系统字体，可只用 typst-kit 的 `fonts::embedded`（启用 `embedded-fonts`），放弃 `scan-fonts`。
- `main()` / `source()` / `file()`：网页端文件来自内存或虚拟文件系统，可自行实现 `files::FileLoader`（见 u3-l1），不启用 `system-files`。
- `today()`：可复用 typst-kit 的 `datetime::Time`（启用 `datetime`），或干脆返回 `None`。
- 放弃：`system-packages` / `universe-packages` / `system-downloader` / `server` / `watcher` / `timer` 等与「本地工具链」强相关的特性。

做完后，把你的结论与 [SystemWorld 的对应表](#434-代码实践源码阅读型本讲主任务) 对比，体会「typst-kit 是积木盒，`World` 是插槽，`SystemWorld` 只是其中一种拼法」。

---

## 6. 本讲小结

- typst-kit 在 `lib.rs` 用九行 `pub mod` 声明了 **九个公开模块**：`datetime`、`diagnostics`、`downloader`、`files`、`fonts`、`packages`、`server`、`timer`、`watcher`。
- 这九个模块可分为两组：**直接服务 `World` 的数据源**（fonts/files/packages/datetime）与 **服务周边工具链**（downloader/server/watcher/timer/diagnostics）。
- `World` trait 是编译器与外界唯一的契约，包含 **七个回调**：`library` / `book` / `main` / `source` / `file` / `font` / `today`。
- `typst-cli` 的 `SystemWorld` 把 typst-kit 拼成 `World`：七个回调里有 **六个转发给 typst-kit 类型**（`FontStore`/`FileStore`/`Time`），只有 `library()` 来自 `typst::Library`。
- `downloader`/`server`/`watcher`/`timer` 不出现在 `SystemWorld` 的字段里，因为它们服务于 `World` 之外的工具链能力——这正是「积木按需启用」的体现（结合 u1-l2 的特性开关理解）。
- 记住一句话：**typst-kit 的存在意义，就是为「实现一个 `World`」提供现成的、可按需启用的零件。**

---

## 7. 下一步学习建议

本讲建立了「模块 ↔ World 回调」的全景地图，接下来建议按数据流深入「直接服务 `World`」的子系统：

1. **u2 字体加载子系统**：从 `FontStore` + `OnceLock` 的懒加载机制入手（你在本讲看到的 `LazyLock::new(discover_fonts)` 的内幕），理解 `book()` 与 `font()` 背后的实现。
2. **u3 文件与源码加载子系统**：深入 `FileStore` / `FileLoader`，理解 `source()` / `file()` 是如何缓存与按需读盘的。
3. 暂时不必碰 `downloader` / `server` / `watcher` / `timer`——等你看完 u2、u3、u4（包加载）后，再回头看这些「周边工具链」积木会更有体感。

> 建议阅读顺序：u1-l3（本讲）→ u2-l1 → u3-l1 → u4-l1，先把「`World` 的数据从哪来」这条主线打通。
