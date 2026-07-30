# 项目概览与定位：typst-kit 是什么

## 1. 本讲目标

本讲是整本 typst-kit 学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 **typst-kit 在 typst 工作区（workspace）中的定位**：它是一个「面向 Typst 工具集成的积木库」。
- 理解文档里反复出现的 **single source of truth（唯一真相源）** 这句话到底指什么、为什么这样设计。
- 看懂 typst-kit 与 `typst-cli`、`typst-library`、`typst` 之间的关系：谁依赖谁、各自负责什么。
- 能够打开 `src/lib.rs` 与 `Cargo.toml`，从源码本身（而不是别人的转述）确认上述结论。

本讲不涉及任何具体模块（字体、文件、包加载等）的实现细节，那些是后续讲义的内容。本讲只解决一个问题：**在动手读代码之前，先建立一个准确的全局认知。**

## 2. 前置知识

在读本讲之前，你只需要具备以下基础：

- **Rust 基础**：看得懂 `pub mod`、`struct`、`impl`、`use` 这些关键字，知道 Cargo 的 `Cargo.toml`、`[features]` 是什么。
- **对 Typst 的大致印象**：Typst 是一个用 Rust 写的、现代的排版系统（可以理解为「更现代的 LaTeX」），它可以把 `.typ` 文档编译成 PDF、HTML 等。
- **「trait 即契约」的概念**：在 Rust 里，trait 定义了一组方法签名，相当于一份「合同」。任何想满足这份合同的类型，都必须实现这些方法。

如果你完全没接触过 Typst，也不用担心，本讲会从最基础的定位讲起。下面这个概念是理解整篇讲义的关键：

> **World 是 Typst 编译器需要的「运行环境契约」。**
>
> 想象 Typst 编译器是一个「只会排版、不认识文件系统」的核心引擎。当它要编译一份文档时，它需要外界告诉它：文档内容是什么、有哪些字体可用、现在是几月几号、某个图片的字节是什么……这些「外界提供的能力」被抽象成一个叫 `World` 的 trait。**谁实现了 `World`，谁就能驱动 Typst 编译。**
>
> typst-kit 的存在，就是为了给「实现 `World`」这件事提供现成的、可复用的零件（积木）。

## 3. 本讲源码地图

本讲只看三个文件，它们分别回答三个不同的问题：

| 文件 | 作用 | 本讲用它回答什么问题 |
| --- | --- | --- |
| `src/lib.rs` | typst-kit 的库入口，包含模块级文档和所有公开模块声明 | typst-kit 自己怎么描述自己？它公开了哪些模块？ |
| `Cargo.toml` | typst-kit 的包元数据与依赖、特性（feature）声明 | 别人怎么定位这个包？它的特性开关哲学是什么？ |
| `../typst-cli/src/world.rs` | typst 命令行工具（typst-cli）里实现 `World` 的地方 | typst-kit 的积木到底被谁、怎么拼装起来？ |

这三个文件共同构成一条证据链：**自我描述 → 包定位 → 实际被消费**。本讲会沿着这条链一路读下来。

## 4. 核心概念与源码讲解

### 4.1 模块一：typst-kit 的定位——面向工具集成的「积木库」

#### 4.1.1 概念说明

打开 typst-kit 的源码，第一段文档就给它定了性。这里有两个关键词需要特别理解：

1. **building blocks（积木 / 构建块）**：typst-kit 不提供「完整可运行的 Typst」，而是提供一个个**可独立使用的零件**。比如「从磁盘扫描字体」「从网络下载 Typst 包」「把编译错误美化成彩色终端输出」。你可以只挑你需要的几块积木，拼成自己想要的工具。

2. **single source of truth（唯一真相源）**：这是一句设计意图的宣告。意思是——像「如何查找字体」「如何加载包」这类「大家都要做、但做法容易各搞一套」的事情，typst 希望它们**只在 typst-kit 这一个地方实现一次**，而不是 typst-cli 一套、第三方工具一套、官方文档示例又一套。这样：

   - 行为统一：所有基于 typst-kit 的工具，字体查找、包加载的逻辑都一致。
   - 维护集中：修一个 bug、加一个特性，所有下游工具一起受益。
   - 降低门槛：想做一个 Typst 工具的人，不用从零实现这些繁琐的「与操作系统 / 网络打交道」的逻辑。

#### 4.1.2 核心流程

可以用下面这张「分层关系图」来理解 typst-kit 在整个 typst 工作区中的位置：

```
        ┌──────────────────────────────────────────────┐
        │   你的工具 / typst-cli（命令行）              │
        │   职责：实现 World trait，拼装积木            │
        └───────────────────────┬──────────────────────┘
                                │ 取用积木
                                ▼
        ┌──────────────────────────────────────────────┐
        │   typst-kit（本讲的主角）                     │
        │   职责：提供字体/文件/包/诊断/时间… 等积木    │
        │   定位：single source of truth                │
        └───────────────────────┬──────────────────────┘
                                │ 依赖
                                ▼
        ┌──────────────────────────────────────────────┐
        │   typst（核心引擎）/ typst-library（标准库）  │
        │   职责：定义 World 契约、真正做排版           │
        └──────────────────────────────────────────────┘
```

也就是说，typst-kit 处于「中间层」：

- 它**向下**依赖核心的 `typst` / `typst-library`，知道 `World` 契约长什么样；
- 它**向上**被 `typst-cli` 以及任何想做 Typst 集成的人所依赖，提供现成的实现。

#### 4.1.3 源码精读

typst-kit 对自己的定位，写得最清楚的就是 `src/lib.rs` 最顶部的模块级文档。先看核心那一句：

[src/lib.rs:1-4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L1-L4)：typst-kit 的自我定位——「为 Typst 集成提供积木」「字体查找、包加载等事情的 single source of truth」「承载 typst-cli 所用的各种实现」。

把这段话拆开，对应到本讲前面讲的概念：

- *"useful building blocks for Typst integrations"* → 「积木」。
- *"intended as a single source of truth for things like font searching, package loading and more"* → 「唯一真相源」。
- *"it contains various implementations of functionality used in `typst-cli`"* → 它实际承载了命令行工具所用的实现（这正是 4.3 节要验证的）。

紧接着，文档用一大段说明了它的特性开关（feature flags）哲学——这部分细节会在「u1-l2 特性开关体系」里专门讲，本讲只需记住一句话：**typst-kit 默认关闭所有特性，每个特性对应一组额外依赖，按需启用。**

文档之后，`src/lib.rs` 用一连串 `pub mod` 把所有公开模块挂出来：

[src/lib.rs:52-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L52-L60)：typst-kit 全部的公开模块声明。

数一下，一共是 **9 个**公开模块（注意：这里是 9 个，不是 10 个，以源码为准）：

| 模块 | 大致职责 |
| --- | --- |
| `fonts` | 字体发现与懒加载 |
| `files` | 文件 / 源码加载与缓存 |
| `packages` | Typst 包加载 |
| `downloader` | 网络下载 |
| `diagnostics` | 终端诊断美化输出 |
| `datetime` | 当前日期（供可复现构建） |
| `server` | 热重载 HTTP 服务器 |
| `watcher` | 文件监视 |
| `timer` | 性能追踪 |

这 9 个模块就是后续 7 个单元（u2–u8）要逐个深入的内容。本讲你只要记住「它们都存在、都是 `pub`」就够了。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲自从源码确认 typst-kit 的定位，而不是听我转述。

1. **实践目标**：用自己的话写一段不超过 100 字的说明，讲清「typst-kit 解决什么问题、谁会用到它」，并指出它声明导出了哪些公开模块。
2. **操作步骤**：
   - 打开 `src/lib.rs`，阅读顶部（第 1 行到第 31 行附近）的模块级文档注释（以 `//!` 开头的行）。
   - 打开 `src/lib.rs` 末尾的 `pub mod` 区域（第 52–60 行），数清楚共有几个公开模块、分别叫什么名字。
3. **需要观察的现象**：你会看到文档里明确出现 "building blocks"、"single source of truth"、"used in `typst-cli`" 这样的措辞；模块声明区则是一眼可数的若干个 `pub mod`。
4. **预期结果**：你写出的 100 字说明里，应当包含三个要点——①它是为 Typst 工具集成准备的零件库；②它把字体查找、包加载等公共逻辑集中实现一次（single source of truth）；③它公开了 9 个模块（fonts / files / packages / downloader / diagnostics / datetime / server / watcher / timer）。
5. 本实践为纯阅读，**不涉及编译运行**，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：「single source of truth」在这里具体指什么？为什么 typst 要专门设一个 crate 来当这个「真相源」？

> **参考答案**：指「字体查找、包加载这类公共逻辑，只在 typst-kit 这一处实现一次」。专门设一个 crate，是为了让 typst-cli 以及所有第三方工具共用同一份实现，从而行为统一、维护集中、避免各搞一套。

**练习 2**：数一数 `src/lib.rs` 里有多少个 `pub mod` 声明。如果你之前听说 typst-kit 有「10 个模块」，请以源码为准更正。

> **参考答案**：实际是 9 个：`datetime`、`diagnostics`、`downloader`、`files`、`fonts`、`packages`、`server`、`timer`、`watcher`。一切以源码为准。

**练习 3**：为什么说 typst-kit 处于 typst 工作区的「中间层」？它向「下」依赖什么、向「上」被谁依赖？

> **参考答案**：它向下依赖 `typst` / `typst-library`（需要知道 World 契约与核心类型），向上被 `typst-cli` 及任何 Typst 集成工具依赖（提供字体、文件、包等现成实现）。

---

### 4.2 模块二：Cargo.toml 的描述与「默认全关」哲学

#### 4.2.1 概念说明

`Cargo.toml` 里有两样东西最值得在概览阶段关注：

1. **`description` 字段**：这是这个包对外的「一句话自我介绍」，能在 crates.io、`cargo search` 里被看到。它从外部视角确认了 typst-kit 的定位。
2. **`[features]` 段，尤其是 `default = []`**：这是 typst-kit 最具辨识度的设计——**默认情况下，所有可选功能都关闭**。

为什么要默认全关？因为 typst-kit 的能力跨度很大：从「读个日期」到「发 HTTPS 请求下载包」再到「起一个 HTTP 服务器」，每种能力都会拉进不同的重型依赖（比如 `ureq`、`native-tls`、`tiny_http`、`notify`）。如果默认全开，那么只想「拿个字体查找功能」的下游，也被迫编译一堆用不到的网络 / GUI 相关依赖，既慢又臃肿。所以 typst-kit 选择：**默认啥都不给，你需要哪块积木，就在 `features` 里点名要哪块。**

#### 4.2.2 核心流程

特性开关的工作方式可以这样理解：

```
你在 Cargo.toml 里写：
    typst-kit = { version = "...", features = ["embedded-fonts", "datetime"] }
        │
        ▼
Cargo 看到 features = ["embedded-fonts"]
        │
        ▼
触发 [features] 里的定义：embedded-fonts = ["dep:typst-assets", "typst-assets/fonts"]
        │
        ▼
把原本 optional = true 的 typst-assets 依赖真正启用，
并打开它的 fonts 子特性 → embedded() 函数变得可用
```

关键点：每个 feature 都对应「启用某些原本标注为 `optional = true` 的依赖」。依赖不启用，对应模块里的某些函数要么不编译、要么根本不存在，调用就会直接编译报错。这是一种**用编译期开关来精简依赖**的典型 Rust 实践。

> 本节只建立直觉，所有 feature 的逐条详解见后续讲义「u1-l2 特性开关体系」。

#### 4.2.3 源码精读

先看包的描述字段：

[Cargo.toml:1-3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L1-L3)：包名 `typst-kit` 与描述 `"Common utilities for Typst tooling."`。

这句 "Common utilities for Typst tooling."（面向 Typst 工具的通用实用工具）和 `lib.rs` 里 "building blocks for Typst integrations" 是同一个意思的两种说法——一个对外（crates.io），一个对内（源码文档）。两者互相印证 typst-kit 的定位。

再看特性开关的「默认全关」：

[Cargo.toml:48-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L48-L49)：`[features]` 段开头，`default = []` —— 没有任何默认启用的特性。

最后看一个具体的 feature 定义，体会「feature → 启用 optional 依赖」的机制：

[Cargo.toml:54-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L54-L55)：`embedded-fonts` 这个 feature 会启用 `typst-assets`（及其 `fonts` 子特性），从而让 `fonts::embedded()` 可用。

可以看到，`typst-assets` 在 `[dependencies]` 里被标成了 `optional = true`（见 `Cargo.toml` 第 14 行），只有当 `embedded-fonts` 被启用时它才会被真正拉入。

#### 4.2.4 代码实践

这同样是一个**阅读型实践**。

1. **实践目标**：从 `Cargo.toml` 中确认「默认全关」哲学，并理解 feature 如何串起依赖。
2. **操作步骤**：
   - 在 `Cargo.toml` 中找到 `[features]` 段（约第 48 行起），确认 `default = []`。
   - 挑一个 feature，比如 `embedded-fonts`，看它等号右边引用了哪些 `dep:xxx`。
   - 再回到 `[dependencies]` 段（第 13 行起），找到那些 `xxx` 依赖，确认它们都标注了 `optional = true`。
3. **需要观察的现象**：你会清楚地看到「feature 名 → 一组 `optional` 依赖」的对应关系；并且几乎所有重型依赖（`ureq`、`native-tls`、`tiny_http`、`notify`、`fontdb`、`chrono` 等）都是 `optional = true`。
4. **预期结果**：你能口头复述——「typst-kit 默认不启用任何特性；启用某个特性，本质上是把它对应的那组 optional 依赖打开。」
5. 本实践为纯阅读，无需「待本地验证」。

> 想更进一步的同学，可以在一个新建的 Cargo 项目里 `typst-kit = { features = ["embedded-fonts", "datetime"] }` 并 `cargo build`，观察编译能通过；再尝试调用一个需要 `scan-fonts` 才存在的函数，观察编译报错。这一步属于「动手编译」，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：typst-kit 的 `description` 字段写的是什么？它和 `lib.rs` 文档里的哪句话表达的是同一个意思？

> **参考答案**：`description = "Common utilities for Typst tooling."`，与 `lib.rs` 里 "useful building blocks for Typst integrations" 同义——都在说它是「面向 Typst 工具集成的通用积木」。

**练习 2**：为什么 `default = []`？如果改成默认全开，会有什么坏处？

> **参考答案**：为了避免下游被迫编译用不到的重型依赖（网络、TLS、HTTP 服务器、文件监视等）。默认全开会让「只想用一小块功能」的用户也背上全部依赖，编译变慢、产物变大。

**练习 3**：`embedded-fonts = ["dep:typst-assets", "typst-assets/fonts"]` 这一行起了什么作用？

> **参考答案**：启用 `embedded-fonts` 特性时，会把 `optional` 依赖 `typst-assets` 真正启用，并打开它的 `fonts` 子特性，从而使 `fonts::embedded()` 能够加载内置字体。

---

### 4.3 模块三：typst-kit 与 typst-cli、typst-library 的关系

#### 4.3.1 概念说明

前两节讲了 typst-kit 「怎么说自己」和「包怎么定位自己」。这一节用最有力的证据来收尾：**看看真实的 typst-cli 是怎么把 typst-kit 当积木拼起来的。**

先理清三个名字：

- **`typst`**：核心编译引擎，定义了 `World` trait（「我需要哪些能力才能排版」）以及真正的排版算法。
- **`typst-library`**：Typst 的标准库（标准函数、标准元素等）。typst-kit 直接依赖它。
- **`typst-cli`**：官方命令行工具（`typst compile`、`typst watch` 那些）。它要驱动编译，就必须实现一个 `World`。

而 typst-cli 实现 `World` 时，绝大部分「与操作系统打交道」的能力——字体、文件、包、时间——**都是从 typst-kit 拿的**。这就是「single source of truth」在代码层面的真实含义：typst-cli 不自己从零写一套字体扫描，而是 `use typst_kit::fonts::FontStore;` 直接用。

#### 4.3.2 核心流程

typst-cli 里有一个结构体叫 `SystemWorld`，它就是 `World` trait 的实现。它的字段几乎全是 typst-kit 提供的类型，组装过程大致如下：

```
SystemWorld {
    library: typst::Library            ← 用 typst 核心构建（标准库 + 输入变量）
    fonts:   typst_kit::fonts::FontStore       ← 来自 typst-kit
    files:   typst_kit::files::FileStore<...>  ← 来自 typst-kit
    now:     typst_kit::datetime::Time         ← 来自 typst-kit
}

impl World for SystemWorld {
    library() → &library
    book()     → fonts.book()        // 字体目录
    main()     → files 里的主文件 id
    source()   → files.source(id)    // 源码
    file()     → files.file(id)      // 二进制文件
    font()     → fonts.font(idx)     // 具体字体
    today()    → now.today(offset)   // 当前日期
}
```

也就是说，`World` trait 的 7 个方法里，有 6 个都是直接转发给 typst-kit 提供的类型。typst-kit 的「积木」属性在这里体现得淋漓尽致。

#### 4.3.3 源码精读

最直接的证据，是 typst-cli 在 `world.rs` 顶部的那几行 `use`——它点名要了 typst-kit 的哪些东西：

[../typst-cli/src/world.rs:16-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L16-L20)：typst-cli 从 typst-kit 导入 `Time`、`DiagnosticWorld`、`FileLoader/FileStore/FsRoot`、`FontStore`、`SystemPackages`。

这正是「typst-cli 用 typst-kit 当积木」的铁证：日期、诊断、文件、字体、包，全部来自 typst-kit。

再看 `SystemWorld` 结构体本身：

[../typst-cli/src/world.rs:24-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L24-L38)：`SystemWorld` 的字段——`fonts: LazyLock<FontStore, ...>`、`files: FileStore<SystemFiles>`、`now: Time`，全是 typst-kit 的类型。

最后看它如何实现 `World` trait：

[../typst-cli/src/world.rs:117-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L117-L145)：`impl World for SystemWorld`，7 个方法几乎都是一行转发给 typst-kit 的类型。

把这三段连起来读，你会得到一个无可辩驳的结论：**typst-cli 的 `World`，主体就是把 typst-kit 的几个类型粘在一起。** typst-kit 确实如它自己所宣称的那样，"contains various implementations of functionality used in `typst-cli`"。

#### 4.3.4 代码实践

1. **实践目标**：亲手把 `World` trait 的每个方法和「实现它所用的 typst-kit 类型」对应起来，体会积木的拼装方式。
2. **操作步骤**：
   - 打开 `../typst-cli/src/world.rs` 的 `impl World for SystemWorld`（第 117–145 行）。
   - 准备一张两列的表格：左列写 `World` 方法名，右列写「它的实现用了哪个 typst-kit 类型 / 字段」。
   - 逐个方法填表，例如 `book()` → `self.fonts.book()`（`FontStore`）。
3. **需要观察的现象**：你会发现除了 `library()` 之外，其余方法（`book / main / source / file / font / today`）的实现体都只有一两行，且都指向 `self.fonts` / `self.files` / `self.now` 这些 typst-kit 字段。
4. **预期结果**：得到一张类似下表的映射关系：

   | `World` 方法 | 实现所用的 typst-kit 类型 | 字段 |
   | --- | --- | --- |
   | `library()` | （非 typst-kit，由 `typst::Library` 构建） | `library` |
   | `book()` | `fonts::FontStore` | `fonts` |
   | `main()` | `files::FileStore`（经 loader） | `files` |
   | `source(id)` | `files::FileStore` | `files` |
   | `file(id)` | `files::FileStore` | `files` |
   | `font(idx)` | `fonts::FontStore` | `fonts` |
   | `today(offset)` | `datetime::Time` | `now` |

5. 本实践为纯阅读，无需「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`SystemWorld` 的哪几个字段直接来自 typst-kit？分别属于 typst-kit 的哪个模块？

> **参考答案**：`fonts: FontStore`（来自 `fonts` 模块）、`files: FileStore<SystemFiles>`（来自 `files` 模块）、`now: Time`（来自 `datetime` 模块）。

**练习 2**：在 `impl World for SystemWorld` 的 7 个方法里，哪一个**不是**靠 typst-kit 的类型实现的？为什么？

> **参考答案**：`library()`。它返回的是 `typst::Library`，是在 `SystemWorld::new` 里用 `Library::builder()` 直接构建的，不依赖 typst-kit。

**练习 3**：如果说「typst-kit 是 typst-cli 实现 World 的积木来源」，请用本节看到的源码给出一处最直接的证据。

> **参考答案**：`world.rs` 第 16–20 行的 `use typst_kit::{datetime::Time, diagnostics::DiagnosticWorld, files::{...}, fonts::FontStore, packages::SystemPackages};` —— typst-cli 直接从 typst-kit 导入日期、文件、字体、包等核心类型来组装 `SystemWorld`。

---

## 5. 综合实践

本讲的综合实践，是把前面三个模块串起来的一次「证据链」阅读。请完成下面这个小任务：

**任务**：假设有同学问你「typst-kit 到底是个什么东西，凭什么说它是 single source of truth？」请你只用三个文件——`src/lib.rs`、`Cargo.toml`、`../typst-cli/src/world.rs`——各找出**一条**源码证据，组织成一段回答（200 字以内），要求：

1. 用 `src/lib.rs` 的一句话证明 typst-kit 对自己的定位（building blocks / single source of truth）。
2. 用 `Cargo.toml` 的 `description` 或 `default = []` 说明它的对外定位与「按需启用」的设计。
3. 用 `typst-cli/src/world.rs` 的 `use typst_kit::...` 或 `impl World` 证明「这些积木确实被官方 CLI 用作了 World 的实现」。

**操作步骤**：

1. 重读 `src/lib.rs:1-4`，抄下最关键的那句英文原文。
2. 打开 `Cargo.toml`，记下 `description` 与 `default = []`。
3. 打开 `typst-cli/src/world.rs:16-20` 与 `117-145`，记下 typst-cli 从 typst-kit 导入并转发的方法。
4. 把这三条证据拼成一段连贯的中文解释。

**预期结果**：你的回答能让一个没读过源码的人明白——typst-kit 是把「字体 / 文件 / 包 / 时间」等 Typst 工具都需要的公共能力集中实现一次的积木库，默认按需启用，并且已经被官方 typst-cli 实际用作 `World` 的实现来源。这便是「single source of truth」的全部含义。

> 本任务为纯阅读与写作，不涉及编译运行，无需「待本地验证」。

## 6. 本讲小结

- **typst-kit 是「面向 Typst 工具集成的积木库」**：它不提供完整可运行的 Typst，而是提供字体、文件、包、诊断、时间等可独立复用的零件。
- **它是 single source of truth**：字体查找、包加载等公共逻辑只在 typst-kit 实现一次，供 typst-cli 和所有第三方工具共用，保证行为统一、维护集中。
- **它默认关闭所有特性**：`default = []`，每个 feature 对应一组 `optional` 依赖，按需启用，避免下游背上不必要的重型依赖。
- **它公开 9 个模块**：`datetime`、`diagnostics`、`downloader`、`files`、`fonts`、`packages`、`server`、`timer`、`watcher`（以源码为准）。
- **它处于工作区的中间层**：向下依赖 `typst` / `typst-library`（需要 World 契约），向上被 `typst-cli` 依赖（提供 World 的实现）。
- **它的「积木」属性有源码铁证**：typst-cli 的 `SystemWorld` 字段（`FontStore` / `FileStore` / `Time`）与 `impl World` 的方法几乎全部来自 typst-kit。

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按手册顺序继续：

1. **下一讲 u1-l2《特性开关体系：按需启用功能》**：本讲只是顺带提了 `default = []`，下一讲会逐条讲清 `embedded-fonts` / `scan-fonts` / `system-packages` / `universe-packages` / `system-downloader` / `watcher` / `http-server` 等 feature 各自启用什么能力、引入哪些依赖。
2. **再下一讲 u1-l3《模块地图与 World 契约》**：会把本讲末尾出现的 `World` trait 讲得更系统，并给出 9 个模块与 World 回调的完整对应关系。
3. **想直接看积木如何被消费**：可以随时打开 `../typst-cli/src/world.rs` 反复对照——它是理解 typst-kit 用途最好的「使用说明书」。
4. **进入具体实现**：完成 u1 的三讲后，从 u2《字体加载子系统》开始逐个模块深入源码。
