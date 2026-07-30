# 特性开关体系：按需启用功能

## 1. 本讲目标

承接上一讲（u1-l1）对 typst-kit 定位的认识——它是「面向 Typst 工具集成的积木库」。本讲聚焦 typst-kit 一个贯穿全局的设计决策：**重度依赖 feature-flag（特性开关）**。读完本讲，你应当能够：

- 说清楚 typst-kit 为什么默认关闭所有特性（`default = []`），以及这种「按需付费」哲学的好处。
- 逐一列举 13 个特性开关分别启用了哪段代码、引入了哪些外部依赖。
- 理解特性之间如何互相联动（feature→feature 依赖链），以及源码中两种 `cfg` 门禁写法的区别。
- 学会在自己的项目中通过 `features = [...]` 数组精确挑选所需能力，并看懂下游 typst-cli 是如何启用的。

## 2. 前置知识

- **Cargo feature（特性）是什么**：Cargo 允许在 `Cargo.toml` 的 `[features]` 表里定义一组命名开关。每个开关可以同时做三件事：(1) 启用某个 `optional`（可选）依赖；(2) 启用某个依赖的某个特性；(3) 启用本 crate 的其他特性。下游在依赖你时，通过 `features = ["xxx"]` 决定打开哪些开关。这是 Rust 生态实现「条件编译 + 按需拉依赖」的标准手段。
- **`optional = true` 依赖**：在 `[dependencies]` 中标记 `optional` 的依赖，只有当某个 feature 显式启用它（用 `dep:名字`）时才会被编译进来；否则完全不参与构建。
- **`#[cfg(...)]` 条件编译**：Rust 编译器根据 `cfg` 条件决定某段代码是否参与编译。`cfg(feature = "x")` 表示「仅当 x 特性开启时编译这段代码」。
- **上一讲的结论**：typst-kit 是 single source of truth，typst-cli 的 `SystemWorld` 直接复用它提供的 `FontStore`/`FileStore`/`Time` 等类型来实现 World 契约。本讲要回答：这些类型背后的「重型外部依赖」是如何做到「不用的就不编译」的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `crates/typst-kit/Cargo.toml` | 声明所有 optional 依赖（`[dependencies]`）与全部特性开关（`[features]`）。这是特性的**权威定义表**。 |
| `crates/typst-kit/src/lib.rs` | crate 根：模块文档里的「特性清单」说明，以及一条 `cfg_attr`，用于在特性未全开时放宽文档链接检查。 |
| `crates/typst-kit/src/datetime.rs` 等 | 各功能模块：在文件顶部或具体条目上用 `#[cfg(feature = "...")]` 实施门禁。本讲抽取若干代表性例子。 |
| `crates/typst-cli/Cargo.toml` | 下游真实用例：展示一个集成方如何用 `features = [...]` 挑选 typst-kit 的能力，以及「特性透传」写法。 |

## 4. 核心概念与源码讲解

### 4.1 `default = []` 与 optional 依赖：为什么默认全关

#### 4.1.1 概念说明

typst-kit 涉及的能力五花八门：加载内置字体要 `typst-assets`、扫描系统字体要 `fontdb`、解包 `.tar.gz` 包要 `flate2`/`tar`、发 HTTPS 请求要 `ureq`/`native-tls`、起 HTTP 服务器要 `tiny_http`……如果把这些依赖一股脑全列为「必选」，那么任何一个只想用 typst-kit 一小部分功能（比如只要 `Timer` 性能追踪）的下游，都会被迫编译 OpenSSL、fontdb、tiny_http 这些它根本用不到的重型库。

typst-kit 的解法是：**所有带额外依赖的功能都用 feature-flag 包起来，且默认一个都不开**。这样 typst-kit 本体保持「瘦」，下游只为真正用到的能力付出依赖代价。这是一种典型的「按需付费（pay for what you use）」设计。

#### 4.1.2 核心流程

实现这一哲学需要两步配合：

1. **在 `[dependencies]` 里把「会带来额外开销」的依赖标成 `optional`**——它们默认不参与编译。
2. **在 `[features]` 里用 `default = []` 声明默认不启用任何特性**，再为每个能力定义一个开关，开关内部用 `dep:xxx` 把对应的 optional 依赖「点亮」。

伪代码：

```toml
[dependencies]
fontdb = { version = "...", optional = true }   # 默认不编译

[features]
default = []                                     # 默认全关
scan-fonts = ["dep:fontdb", ...]                 # 开 scan-fonts 才点亮 fontdb
```

#### 4.1.3 源码精读

`default = []` 这一行就是「默认全关」的源头：

- [Cargo.toml 第 49 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L49) —— `default = []`：默认特性集合为空。

`[dependencies]` 里大量 `optional = true` 标记，是「按需依赖」的物质基础：

- [Cargo.toml 第 13–41 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L13-L41) —— 注意 `typst-assets`、`fontdb`、`chrono`、`flate2`、`tar`、`ureq`、`native-tls`、`tiny_http`、`notify` 等都带 `optional = true`；而 `typst-library`、`ecow`、`serde`、`url` 等核心依赖**不**带 `optional`，说明它们是任何集成都必备的基础。

`lib.rs` 的模块文档把这一哲学点得很明确——「会带来额外依赖的功能被重度 feature-flag 化，默认全部关闭」：

- [src/lib.rs 第 6–9 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L6-L9) —— 模块文档说明设计意图。

#### 4.1.4 代码实践

1. **实践目标**：亲眼验证「默认情况下 typst-kit 不编译任何可选功能」。
2. **操作步骤**：在 typst-kit crate 目录下执行 `cargo build --no-default-features`。
3. **需要观察的现象**：构建成功，且编译日志里不会出现 `fontdb` / `chrono` / `tiny_http` 等可选 crate 的编译过程。
4. **预期结果**：编译通过。因为 `default = []`，没有任何 optional 依赖被点亮。
5. **待本地验证**：具体编译耗时与日志行因机器而异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `typst-library` 没有标 `optional = true`，而 `typst-assets` 标了？

**答案**：`typst-library` 定义了 World 契约和编译核心类型，是任何 typst-kit 集成都绕不开的基础，因此必选；`typst-assets` 只为「内置字体」这一项功能服务，不用内置字体的下游不该为它付费，所以设为 optional，由 `embedded-fonts` 特性按需点亮。

**练习 2**：如果删掉 `default = []` 这一行，typst-kit 的行为会变吗？

**答案**：不会有实质变化——Cargo 在没有 `default` 键时默认就是空集。显式写 `default = []` 是一种「明确表达设计意图」的文档化写法，提醒读者「我们刻意默认全关」。

### 4.2 特性总览：逐个开关启用了什么能力与依赖

#### 4.2.1 概念说明

typst-kit 共定义了 13 个特性开关。每个开关都遵循同一个模式：**开启某段功能代码 + 引入这段代码所需的最小依赖集合**。理解这些开关，就是理解 typst-kit 的「能力菜单」。

需要特别强调：**特性的权威定义在 `Cargo.toml` 的 `[features]` 表里**；`src/lib.rs` 顶部文档也列了一份说明清单，但它只是给人看的注释，且与 `[features]` 的条目顺序略有出入（见 4.2.4 的实践）。一切**以 `Cargo.toml` 为准**。

#### 4.2.2 核心流程

下表逐个列出每个特性：它门禁了哪段代码、点亮了哪些依赖、是否连带启用其他特性。

| 特性 | 启用的能力（被 `cfg` 门禁的代码） | 点亮的依赖 | 连带启用的特性 |
|---|---|---|---|
| `bundle` | 与 `typst-bundle` 相关的功能（如 `server.rs` 中 bundle 相关分支） | `dep:typst-bundle` | 无 |
| `embedded-fonts` | `fonts::embedded()`：内置字体（Libertinus、New Computer Modern 等） | `dep:typst-assets`、`typst-assets/fonts` | 无 |
| `scan-fonts` | `fonts::scan()`、`fonts::system()`：扫描目录与系统字体 | `dep:fontdb`、`fontdb/memmap`、`fontdb/fontconfig`、`dep:dirs` | 无 |
| `system-files` | `files::SystemFiles`：从标准位置加载项目/包文件 | （无直接依赖） | → `system-packages` |
| `system-packages` | `packages::SystemPackages`、`FsPackages::system_data/system_cache` | `dep:dirs` | → `universe-packages` |
| `universe-packages` | `packages::UniversePackages`：从 Universe 下载、解包、查索引 | `dep:flate2`、`dep:tar`、`dep:fastrand` | 无 |
| `datetime` | 整个 `datetime` 模块（`Time` 类型，服务 `World::today`） | `dep:chrono` | 无 |
| `emit-diagnostics` | 整个 `diagnostics` 模块（`emit()` 终端诊断输出） | `dep:codespan-reporting` | 无 |
| `system-downloader` | `downloader::SystemDownloader`（基于 ureq+native-tls 的 HTTPS 客户端） | `dep:env_proxy`、`dep:native-tls`、`dep:ureq`、`dep:openssl` | 无 |
| `watcher` | 整个 `watcher` 模块（基于 notify 的文件监视） | `dep:notify`、`dep:same-file` | 无 |
| `timer` | 整个 `timer` 模块（性能追踪） | （无） | 无 |
| `http-server` | 整个 `server` 模块（热重载 HTTP 服务器） | `dep:tiny_http`、`dep:infer`、`dep:percent-encoding` | 无 |
| `vendor-openssl` | 在非 Windows/macOS 上静态链接 OpenSSL | `openssl/vendored` | 无 |

两个值得品味的细节：

- **`timer = []` 与 `system-files = ["system-packages"]` 本身不点亮任何依赖**。`timer` 之所以为空，是因为它依赖的 `typst-timing` 本就是必选依赖（`Cargo.toml` 第 18 行，非 optional），无需再点亮。这说明「特性」不必非得带依赖，它可以纯粹用作「源码门禁开关」。
- **`system-downloader` 没有被 `universe-packages` 连带启用**，尽管 `UniversePackages` 下载包时需要一个 `Downloader`。原因见 4.3.3 的精读——`Downloader` 是 trait，实现由调用方注入，typst-kit 不强制你用它自带的 `SystemDownloader`。

#### 4.2.3 源码精读

完整特性表（本讲的「主战场」）：

- [Cargo.toml 第 48–92 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L48-L92) —— 全部 13 个特性定义。逐行对照上表阅读。

几个代表性条目：

- `embedded-fonts` 点亮 `typst-assets` 并启用其 `fonts` 特性：[Cargo.toml 第 54–55 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L54-L55)。注意 `dep:typst-assets`（点亮依赖本身）与 `typst-assets/fonts`（启用该依赖的 `fonts` 子特性）是两种不同语法。
- `scan-fonts` 一次性点亮 `fontdb` 并启用其 `memmap`、`fontconfig` 两个子特性：[Cargo.toml 第 57–59 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L57-L59)。
- `system-files` 只做「特性转发」，不带任何 `dep:`：[Cargo.toml 第 61–62 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L61-L62)。
- `timer = []` 空依赖：[Cargo.toml 第 84–85 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L84-L85)。

`src/lib.rs` 顶部的文档清单（给人看的特性说明）：

- [src/lib.rs 第 10–31 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L10-L31) —— 逐条列出特性及其启用的功能。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：体会「`Cargo.toml` 才是权威」，并锻炼细致的源码阅读。
2. **操作步骤**：打开 [Cargo.toml 第 48–92 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L48-L92) 的 `[features]` 表，再对照 [src/lib.rs 第 10–31 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L10-L31) 的文档清单，逐项核对。
3. **需要观察的现象**：你会发现 `lib.rs` 的文档里 `emit-diagnostics` 与 `datetime` 两条说明的顺序被打乱了——`datetime` 那一行被夹在了 `emit-diagnostics` 说明的中间（见 `lib.rs` 第 22–24 行：第 22 行与第 24 行合起来才是 `emit-diagnostics` 的完整描述，中间插了第 23 行的 `datetime`）。
4. **预期结果**：`[features]` 表共 13 个开关（含 `vendor-openssl`），逻辑清晰；而文档清单只是注释，存在轻微排版错乱，但不影响编译。结论：**以 `Cargo.toml` 的 `[features]` 为权威**。
5. **待本地验证**：无（纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：哪个特性「不点亮任何依赖，只是个纯开关」？为什么它能这么做？

**答案**：`timer`。因为它依赖的 `typst-timing` 已经是 typst-kit 的必选依赖（非 optional），无需再用特性去点亮；该特性存在的唯一作用是作为源码里的 `cfg` 门禁，决定 `timer` 模块是否参与编译。

**练习 2**：`dep:typst-assets` 和 `typst-assets/fonts` 分别代表什么？去掉 `dep:typst-assets` 只留 `typst-assets/fonts` 行不行？

**答案**：`dep:typst-assets` 表示「点亮 typst-assets 这个可选依赖本身」；`typst-assets/fonts` 表示「启用 typst-assets 依赖内部的 `fonts` 子特性」。在现代 Cargo（1.60+ 的 feature resolver）里，写 `typst-assets/fonts` 其实也会顺带启用这个可选依赖，因此严格说 `dep:typst-assets` 有些冗余；但显式写出两者是更清晰、避免歧义的写法，typst-kit 选择同时保留。

### 4.3 特性门禁机制：依赖链与两种 cfg 写法

#### 4.3.1 概念说明

定义特性只是第一步；真正让「关掉特性 = 不编译这段代码」生效的，是源码里散布的 `#[cfg(feature = "...")]` 门禁。typst-kit 用了两种写法：

- **整模块门禁**：在文件顶部用**内层属性** `#![cfg(feature = "x")]`。特性关闭时，整个模块的内容被全部剔除，模块变成「空模块」。`datetime`、`watcher`、`timer`、`server`、`diagnostics` 五个模块用这种写法。
- **条目级门禁**：在单个函数/结构体/`use` 上用 `#[cfg(feature = "x")]`。特性关闭时只去掉这些条目，模块其余部分照常编译。`fonts`、`files`、`packages`、`downloader` 用这种写法。

此外，特性之间还能形成**依赖链**：特性 A 可以在定义里启用特性 B，于是「开 A」会连带「开 B」。typst-kit 里最典型的链是 `system-files → system-packages → universe-packages`。

#### 4.3.2 核心流程

**特性依赖链的传播**：

```
开启 system-files
   └─> 连带开启 system-packages
          └─> 连带开启 universe-packages
                 └─> 点亮 flate2、tar、fastrand
```

也就是说，一个下游如果只写了 `features = ["system-files"]`，最终会自动得到 `system-packages`、`universe-packages` 以及 `flate2`/`tar`/`fastrand` 三个依赖。这种「传递启用」让下游不必手动列举整条链。

> 注意方向是**单向**的：开 `system-files` 会带上 `system-packages`，但反过来开 `system-packages` 不会带上 `system-files`。

**两种 cfg 写法的选择逻辑**（经验法则）：

```
若整个文件只为一个特性服务           →  用 #![cfg(feature)] 整模块门禁
若文件里既有「常驻代码」也有「可选代码」 →  用 #[cfg(feature)] 条目级门禁
```

#### 4.3.3 源码精读

**（a）特性依赖链**——在 `[features]` 里，一个特性名的右侧列表可以包含另一个特性名，表示「启用本特性时一并启用它」：

- `system-files = ["system-packages"]`：[Cargo.toml 第 61–62 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L61-L62)
- `system-packages = ["dep:dirs", "universe-packages"]`：[Cargo.toml 第 64–66 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L64-L66)
- `universe-packages = ["dep:flate2", "dep:tar", "dep:fastrand"]`：[Cargo.toml 第 68–70 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L68-L70)

三行连读，就构成了上面那条三级链。

**（b）整模块门禁**——以 `datetime.rs` 为例，文件第一条有效属性就是内层 `#![cfg(...)]`：

- [src/datetime.rs 第 6 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L6) —— `#![cfg(feature = "datetime")]`：特性关闭时整个 `datetime` 模块被剔除。其余四个同样写法：[src/watcher.rs 第 5 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L5)、[src/timer.rs 第 6 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L6)、[src/server.rs 第 3 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L3)、[src/diagnostics.rs 第 3 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L3)。

一个关键问题：模块被整文件门禁后，`lib.rs` 里 `pub mod datetime;` 怎么还能编译通过？因为当文件内容被 cfg 全部剔除时，`datetime` 退化成一个**空模块**——空模块在 Rust 里完全合法，`pub mod datetime;` 照常有效，只是里面没有任何可用的条目。这也解释了 `lib.rs` 里那段 `cfg_attr`：

- [src/lib.rs 第 34–50 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L34-L50) —— 当 12 个内容特性没有「全部同时开启」时，放宽 `rustdoc::broken_intra_doc_links` 检查。因为模块文档里写了诸如 `[datetime::Time::today]` 的链接，一旦对应模块被门禁为空，这些链接就会失效；在「不是全开」的常见场景下放宽这个 lint，文档构建才不会吵闹报错。

**（c）条目级门禁**——以 `fonts.rs` 为例，`embedded()` 与 `scan()/system()` 被分别挂在不同特性上：

- `embedded()` 挂在 `embedded-fonts`：[src/fonts.rs 第 134–142 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L134-L142)
- `system()` 与 `scan()` 挂在 `scan-fonts`：[src/fonts.rs 第 147–157 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L147-L157)、[src/fonts.rs 第 162–166 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L162-L166)

注意 `fonts.rs` 本身没有整模块门禁——`FontStore`、`FontSlot` 等核心类型是无条件编译的（它们不依赖重型外部库），只有「需要外部依赖」的发现函数才被条目级门禁。这正是「常驻代码 + 可选代码共存」的典型场景。

`packages.rs` 还展示了「条件 `use`」——一整块导入语句被门禁：

- [src/packages.rs 第 11–18 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L11-L18) —— 仅当 `universe-packages` 开启时，才导入 `Downloader`、`serde::Deserialize`、`tar`/`flate2` 相关符号。

**（d）为什么 `system-downloader` 不被 `universe-packages` 连带启用？** 因为 `UniversePackages` 需要的不是一个具体类型，而是一个 `Downloader` trait 实现，且它由调用方**注入**：

- [src/packages.rs 第 52–58 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L52-L58) —— `SystemPackages::new(downloader: impl Downloader)`：下载器是参数，不是硬绑定。因此你可以提供自己的下载实现（比如离线/测试用的假下载器），typst-kit 不强迫你为 `system-downloader` 的 ureq/native-tls/OpenSSL 付费。这是特性「正交解耦」的精彩一例。

**（e）补充：cfg 还能按编译目标门禁**，不只按特性：

- [src/fonts.rs 第 154–155 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L154-L155) —— `#[cfg(any(target_os = "windows", target_os = "macos"))]`：只在 Windows/macOS 上才调用 `load_adobe_fonts`。配套地，`Cargo.toml` 用 `[target.'cfg(...)'.dependencies]` 让 `openssl` 只在非苹果/非 Windows 平台成为依赖：[Cargo.toml 第 45–46 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L45-L46)。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到特性依赖链的「传递启用」效果。
2. **操作步骤**：在 typst-kit 目录执行 `cargo build --no-default-features --features system-files`，再执行 `cargo tree -e features --no-default-features --features system-files`（或观察构建日志）。
3. **需要观察的现象**：尽管你只显式指定了 `system-files`，但 `system-packages`、`universe-packages` 被自动启用，`flate2`、`tar`、`fastrand`、`dirs` 被拉入编译。
4. **预期结果**：构建成功，且依赖树里出现上述被连带启用的 crate。
5. **待本地验证**：`cargo tree` 的具体输出格式与版本号因环境而异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `datetime.rs` 用整模块门禁，而 `fonts.rs` 用条目级门禁？

**答案**：`datetime` 模块的全部内容（`Time` 类型及相关逻辑）都依赖 `chrono`，没有「不依赖 chrono 的常驻部分」，适合整模块门禁；`fonts.rs` 则既有不依赖重型库的常驻核心（`FontStore`、`FontSlot`、`FontSource` trait），又有依赖 `fontdb`/`typst-assets` 的可选发现函数，只能用条目级门禁精确切除后者。

**练习 2**：假如你只开了 `universe-packages` 却没开 `system-downloader`，`UniversePackages` 还能工作吗？

**答案**：能——只要你提供一个自实现的 `Downloader` 传给 `SystemPackages::new`。`UniversePackages` 与 `SystemDownloader` 是解耦的：前者只认 `Downloader` trait，后者只是 typst-kit 提供的一种可选实现。这也正是 `universe-packages` 不连带启用 `system-downloader` 的设计原因。

### 4.4 下游按需启用：features 数组与特性透传

#### 4.4.1 概念说明

定义好了特性，最终要看「下游怎么用」。在 Cargo 里，下游在自己的 `Cargo.toml` 里用 `[dependencies]` 段的 `features = [...]` 数组来挑选上游特性。typst-cli 是 typst-kit 最重要的下游真实样本，它既展示了「精确挑选子集」，又展示了「特性透传（forwarding）」——把 CLI 自己对外暴露的特性，原样转发成 typst-kit 的特性。

#### 4.4.2 核心流程

**直接挑选**：下游依赖 typst-kit 时直接列出所需特性。

```toml
[dependencies.typst-kit]
workspace = true
features = ["embedded-fonts", "datetime", "timer"]   # 只挑这三个
```

**特性透传**：下游 crate 自己也定义一个同名特性，其定义体写成 `["typst-kit/xxx"]`，于是「开下游的 xxx」等于「开 typst-kit 的 xxx」。这样终端用户只需面向下游一个 crate 谈特性，不必关心 typst-kit。

```toml
# 在 typst-cli 里
[features]
http-server = ["typst-kit/http-server"]   # 用户开 CLI 的 http-server → 自动开 typst-kit 的同名特性
```

#### 4.4.3 源码精读

typst-cli 直接为 typst-kit 挑选了一组特性（注意它挑选的是「全功能 CLI」需要的能力，并非全开）：

- [crates/typst-cli/Cargo.toml 第 58–71 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L58-L71) —— `features = [...]` 列出 `bundle`、`embedded-fonts`、`scan-fonts`、`system-packages`、`universe-packages`、`datetime`、`emit-diagnostics`、`system-downloader`、`watcher`、`timer` 共 10 个。可对照 4.2 的总表理解每一项为何被需要。

typst-cli 自身也定义了若干特性，其中三个是「透传」给 typst-kit 的：

- [crates/typst-cli/Cargo.toml 第 88–101 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L88-L101) —— 注意 `embedded-fonts = ["typst-kit/embedded-fonts"]`、`http-server = ["typst-kit/http-server"]`、`vendor-openssl = ["typst-kit/vendor-openssl"]`，这三条都把 CLI 的特性映射到 typst-kit 的同名特性。

一个有意思的对照：typst-cli 的 `default = ["embedded-fonts", "http-server"]`（第 89 行）意味着默认构建 CLI 时自带内置字体和 HTTP 服务器；而 `vendor-openssl` 默认关闭，只有发布静态构建时才打开。这正体现了「特性让同一份代码产出不同形态的二进制」。

#### 4.4.4 代码实践

1. **实践目标**：体会「features 数组精确控制能力」与「关掉特性 → 函数不存在」的编译期反馈。
2. **操作步骤**：
   - 新建一个 Cargo 项目，以 path 依赖引入本仓库的 typst-kit，只开 `embedded-fonts` 与 `datetime`：

     ```toml
     # 在新项目的 Cargo.toml
     [dependencies]
     typst-kit = { path = "<指向本仓库的>/crates/typst-kit", default-features = false, features = ["embedded-fonts", "datetime"] }
     ```

   - 写一段代码确认编译通过，例如调用 `typst_kit::fonts::embedded()` 或引用 `typst_kit::datetime` 模块中的类型。
   - 再在新项目里写一行 `let _ = typst_kit::fonts::system();`（它需要 `scan-fonts`），执行 `cargo build`。
3. **需要观察的现象**：第二步编译通过；第三步编译失败，错误大意是找不到 `fonts::system` 函数（因为它被 `scan-fonts` 门禁，而你没有开启该特性）。
4. **预期结果**：第三步报类似 `cannot find function 'system' in module 'fonts'` 的编译错误；把 `features` 改为 `["embedded-fonts", "datetime", "scan-fonts"]` 后即可编译通过。
5. **待本地验证**：path 依赖的具体相对路径要按你放置新项目的位置调整；编译错误的确切文案以本地编译器输出为准。

#### 4.4.5 小练习与答案

**练习 1**：typst-cli 在 `[dependencies.typst-kit]` 里开了 `system-packages` 和 `universe-packages`，但没开 `system-files`。根据 4.3 的依赖链，单开 `system-packages` 会让 `system-files` 生效吗？

**答案**：不会。特性启用是「单向」的：`system-files` 依赖 `system-packages`，所以开 `system-files` 会带上 `system-packages`；但反过来开 `system-packages` 不会带上 `system-files`。因此被 `#[cfg(feature = "system-files")]` 门禁的代码（如 `files::SystemFiles`）在 typst-cli 当前特性配置下是否编译进来，取决于 CLI 是否另开了 `system-files`——理解依赖链的方向很重要（可结合 u3 单元的 FileStore/FileLoader 进一步看清文件子系统的门禁全貌）。

**练习 2**：为什么 typst-cli 要把 `http-server` 做成「透传特性」而不是直接写死在 `[dependencies.typst-kit].features` 里？

**答案**：因为 CLI 想让「是否启用 HTTP 服务器」成为一个**面向最终用户的开关**（默认开，可关）。透传写法让用户在编译 typst-cli 时能通过 `--no-default-features` 或自定义 features 关掉它，从而省下 `tiny_http` 等依赖；如果写死在依赖段，用户就无法关闭了。

## 5. 综合实践

**任务：为「无网、无字体的最小 Typst 渲染集成」挑选特性子集，并验证你的选择。**

背景：假设你要写一个嵌入式工具，用 typst-kit 在**离线**环境编译 Typst 文档，且只使用调用方**自行注入**的字体（既不扫系统字体，也不用内置字体，也不下载任何包）。请完成：

1. 从 4.2 的总表中圈出你**需要**的特性（提示：你可能仍想要 `datetime` 来满足 `World::today`，想要 `emit-diagnostics` 来打印错误；但 `scan-fonts`、`embedded-fonts`、`system-downloader`、`universe-packages` 都应可省）。给出你的最终 features 列表，并说明每条理由。
2. 推断：在你选的特性下，typst-kit 会被拉入哪些外部依赖？（对照 4.2 表的「点亮的依赖」列求并集。）
3. 验证思路：在 typst-kit 目录用 `cargo build --no-default-features --features "<你的列表>"` 检查能否编译通过；再用 `cargo tree` 核对你推断的依赖集合是否一致。
4. 反思：如果之后这个工具需要支持「用户能从 Typst Universe 拉包」，你的 features 列表最少要加哪几项？`system-downloader` 是否必须加，取决于你是否使用 typst-kit 自带的下载器？（回到 4.3.3 的 (d) 点。）

预期：你应能给出一个类似 `["datetime", "emit-diagnostics", "timer"]` 的最小集（具体是否还需要文件类特性，可结合后续 u3 单元判断），并清楚说出每个被排除的特性「为什么可以不要」。若步骤 3 的实际编译结果与你的预期不符，记下差异并回到 4.2/4.3 复核——这是检验你是否真懂特性开关的最好方式。具体编译输出待本地验证。

## 6. 本讲小结

- typst-kit 贯彻「默认全关」哲学：`default = []`，所有带额外依赖的功能都被 feature-flag 包裹，下游按需付费、不被无关重型库拖累。
- `[features]` 表是特性的**权威定义**，共 13 个开关；每个开关 = 一段被门禁的功能代码 + 所需的最小依赖集合（部分开关如 `timer` 不带依赖，纯作源码开关）。
- 特性之间可形成依赖链（`system-files → system-packages → universe-packages`），开启上层会传递启用下层；但启用是**单向**的，方向不能反推。
- 源码里有两种 cfg 门禁：整模块 `#![cfg(feature)]`（datetime/watcher/timer/server/diagnostics）与条目级 `#[cfg(feature)]`（fonts/files/packages/downloader）；选择取决于该文件有无「常驻代码」。
- `lib.rs` 用 `cfg_attr` 在「特性未全开」时放宽 `broken_intra_doc_links`，因为门禁为空的模块会让文档链接失效。
- 下游通过 `features = [...]` 挑选能力；typst-cli 还用 `["typst-kit/xxx"]` 做「特性透传」，把面向用户的开关映射到 typst-kit。

## 7. 下一步学习建议

特性开关只是「菜单」，真正「上菜」的是各模块的实现。建议按以下顺序深入：

- **u1-l3 模块地图与 World 契约**：把本讲的「特性 ↔ 模块」对应关系，升级为「模块 ↔ World trait 方法」的全景图，看清这些被门禁的积木如何拼成一个 World。
- **u2 字体加载子系统**：从 `embedded-fonts` 与 `scan-fonts` 切入，精读 `FontStore`、`FontSource` trait 与懒加载机制——你会看到本讲提到的 `fonts::embedded()` / `fonts::scan()` 的完整内部实现。
- 若你更关心资源加载链，可直接跳到 **u3（文件子系统）** 与 **u4（包加载）**，结合本讲的 `system-files → system-packages → universe-packages` 依赖链，体会「特性依赖链」与「运行时加载优先级链」之间的呼应。
