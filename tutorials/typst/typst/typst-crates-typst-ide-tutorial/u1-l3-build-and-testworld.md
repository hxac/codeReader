# 运行、构建与测试基础设施

## 1. 本讲目标

本讲是「认识 typst-ide」单元的第三篇。前两篇我们知道了 typst-ide 是什么、它的公共 API 长什么样，以及它统一的输入契约 `IdeWorld`。但我们还遗留了一个关键问题：**这个 crate 到底怎么编译、怎么测试？测试时拿什么来充当一个 `IdeWorld`？**

typst-ide 是一个库 crate，没有 `main`，自己不会运行。它所有能力的正确性都靠 `src/tests.rs` 里的一套测试设施来保证。学完本讲，你应当能够：

1. 说出 typst-ide 的 `cargo build` / `cargo test` 方式，以及三个 dev-dependencies 各自的作用。
2. 解释 `TestWorld` 如何同时实现 `World` 与 `IdeWorld`，从而充当一个「最小但可用的 IDE 数据源」。
3. 理解 `TestBase` 为什么用 `singleton!` 做全局懒初始化，以及它复用 `library` / `book` / `fonts` 给测试带来的低成本。
4. 掌握 `with_source` / `with_asset` 的写法，能构造出一个跨文件、带资源的真实测试场景。

## 2. 前置知识

阅读本讲前，你需要知道：

- **`World` trait**：Typst 编译器读取一切外部数据的统一接口，包含 `library()`（标准库）、`book()`（字体簿）、`main()`（主文件 id）、`source(id)`（取源码）、`file(id)`（取二进制资源）、`font(idx)`（取字体）、`today()` 七个方法。这些方法大多被 comemo 的 `track` 机制缓存。
- **`IdeWorld` trait**：上一讲（u1-l2）讲过，它是 `World` 的子 trait，额外提供 `upcast()`（必填）、`packages()`、`files()`（可选）。typst-ide 所有公共函数的第一个参数都是 `&dyn IdeWorld`。
- **`FileId`**：Typst 里标识一个文件的句柄，由「根类型 + 虚拟路径」内部化（intern）得到，便于 comemo 缓存。
- **Rust 的 `Arc` 与 COW（copy-on-write）**：`Arc::make_mut` 在引用计数为 1 时原地可变借用，否则先克隆一份——这是本讲 builder 模式低成本的关键。

如果你对 `World` / `IdeWorld` 的关系还不太清楚，建议先读 u1-l2 再回来。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml) | crate 的依赖声明。`[dependencies]` 决定运行时能力，`[dev-dependencies]` 决定测试时能拿到哪些字体与资源。 |
| [`src/tests.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs) | 测试设施的全部实现：`TestWorld`、`TestBase`、`with_source` / `with_asset`，以及供各模块测试复用的 `WorldLike` / `FilePos` / `EXAMPLE_CLOSURE` 等小工具。 |

> 说明：`src/lib.rs` 里用 `#[cfg(test)] mod tests;` 把 `tests.rs` 只在测试构建中编译。因此本讲讲到的所有类型（`TestWorld` 等）在正式发布的库里并不存在——它们纯粹是为测试服务的脚手架。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：构建与 dev-dependencies、`TestWorld` 数据结构、`TestBase` 共享懒初始化、`with_source` / `with_asset` 多文件构造。

### 4.1 构建方式与 dev-dependencies

#### 4.1.1 概念说明

typst-ide 是一个普通的 Cargo 库 crate，遵循 workspace 统一管理。它没有特殊的二进制入口，因此：

- 构建：在仓库根目录或本 crate 目录执行 `cargo build`。
- 测试：执行 `cargo test`。这会编译 `src/tests.rs` 以及散落在 `tooltip.rs` / `complete.rs` / `definition.rs` 等文件 `#[cfg(test)]` 模块里的所有测试。

测试要能跑起来，光有源码字符串是不够的——IDE 的悬停、补全需要**真实的字体**（用来算文本度量、做字体补全），有时还需要**真实的二进制资源**（图片、bib 文件等）。这些「重资源」由 dev-dependencies 提供，只在测试时引入，不会进发布产物。

#### 4.1.2 核心流程

```text
cargo test
   │
   ├── 编译 src/lib.rs（含 #[cfg(test)] mod tests;）
   │        └── 编译 tests.rs → 得到 TestWorld / TestBase 等脚手架
   │
   ├── 链接 dev-dependencies
   │        ├── typst-assets (features=["fonts"])   → 内置一批字体
   │        ├── typst-dev-assets                    → 额外开发资源（字体、图片、bib 等）
   │        └── once_cell                           → 懒初始化辅助
   │
   └── 运行 #[test] 函数（散落在各源文件的 cfg(test) 模块）
            └── 每个测试构造一个 TestWorld 喂给 autocomplete/tooltip/...
```

#### 4.1.3 源码精读

dev-dependencies 声明非常短：

dev-dependencies（[Cargo.toml:28-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml#L28-L31)）——三个测试专用依赖：

- `typst-assets`（带 `features = ["fonts"]`）：打包了一批默认字体。这是测试能算出排版结果的前提。
- `typst-dev-assets`：仅在开发/测试时存在的资源集合，提供更多字体以及图片、`.bib` 等。它通过 `typst_dev_assets::get_by_name("tiger.jpg")` 这样的名字来取（见后文 `with_asset_at`）。
- `once_cell`：常见的懒加载全局辅助依赖。

对照运行时依赖（[Cargo.toml:15-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml#L15-L26)）可以发现一个分工信号：`typst` / `typst-eval` 支撑补全、悬停、定义、分析；`typst-html` / `typst-layout` 专为双向跳转（jump）服务；而 `typst-assets` / `typst-dev-assets` 只出现在 dev 段，说明字体与资源是「测试营养品」，不是库本身的能力。

#### 4.1.4 代码实践

**实践目标**：确认 dev-dependencies 确实只在测试时生效，并观察字体从哪里来。

**操作步骤**：

1. 在 typst 仓库根目录执行 `cargo build -p typst-ide`（只编译库，不编译测试模块）。
2. 再执行 `cargo build -p typst-ide --tests`（连测试模块一起编译）。

**需要观察的现象**：

- 第 1 步不应为了字体而拉取 `typst-assets` 的字体数据；第 2 步才会编译测试相关代码与 dev-deps。
- 在 `tests.rs` 中搜索 `typst_assets::fonts()` 与 `typst_dev_assets::fonts()`，确认字体来源就是这两个 dev-deps。

**预期结果**：dev-dependencies 不污染发布产物；测试构建会把内置字体链进来。

> 如果你的网络/缓存受限，`typst-dev-assets` 可能在构建时抓取资源失败。这是构建环境问题，不是本讲代码问题——可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `typst-assets` 放在 dev-dependencies 而不是 dependencies？typst-ide 的悬停/补全在真实 LSP 里也需要字体吗？

**参考答案**：typst-ide 自身只做语法分析、值推断和补全生成，不负责打包字体——真实 LSP（如 tinymist）会自带字体管理。测试里需要字体，是为了让 `World` 的 `book()` / `font()` 有内容、让 `typst::compile` 能算出排版产物，从而验证依赖渲染结果的 tooltip / jump。所以字体是「测试用」，归 dev-deps。

**练习 2**：`once_cell` 出现在 dev-dependencies，但 `tests.rs` 顶部 import 的是 `typst::utils` 里的 `singleton!`。这两者可能是什么关系？

**参考答案**：`once_cell` 很可能是 `singleton!` 这类「全局唯一、懒初始化」工具的底层实现依赖之一。`tests.rs` 不直接写 `once_cell::sync::Lazy`，而是通过 `singleton!(Type, expr)` 这个更高层的宏来获得 `&'static` 实例；`once_cell` 作为 dev-dep 为这套机制提供支持。

---

### 4.2 TestWorld —— IDE 测试的最小 World

#### 4.2.1 概念说明

typst-ide 的每个公共函数都吃一个 `&dyn IdeWorld`。测试不可能真的去连一个编辑器，于是 `tests.rs` 提供了一个**最小但完整**的实现：`TestWorld`。它：

- 内部只存「主源码」+「额外源码/资源」+「一个指向共享基础数据的引用」。
- 同时实现 `World`（满足编译器取数）和 `IdeWorld`（满足 IDE 枚举候选）。
- 用 `Arc` + COW 让构造、克隆都极其廉价。

一句话：`TestWorld` 是把「一段 Typst 源码」包装成一个能被 typst-ide 全部能力消费的 world。

#### 4.2.2 核心流程

```text
TestWorld::new(text)
   │  main = Source::new(main_id, text)      ← 主文件固定叫 "main.typ"
   │  files = 空 Arc<TestFiles>             ← 暂无额外文件
   │  base  = singleton!(TestBase, ...)     ← 拿到全局唯一的共享基础（懒初始化）
   ▼
对外既能当 World（library/book/main/source/file/font/today）
又能当 IdeWorld（upcast/files/packages）
```

`World` 的七个方法里，`library` / `book` / `font` 直接转发给共享的 `base`；`main` 返回主文件 id；`source` 先查主文件再查额外源码；`file` 查额外资源；`today` 固定返回 `None`。

`IdeWorld` 的三个方法：`upcast` 返回 `self`；`files` 枚举主文件 + 所有额外源码与资源；`packages` 返回一个写死的示例包列表。

#### 4.2.3 源码精读

先看数据结构（[src/tests.rs:17-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L17-L23)）：

```rust
#[derive(Clone)]
pub struct TestWorld {
    pub main: Source,
    files: Arc<TestFiles>,
    base: &'static TestBase,
}
```

三个字段：主源码、额外文件（`Arc` 包裹便于 COW）、指向全局共享 `TestBase` 的 `&'static` 引用。`#[derive(Clone)]` 只是 bump 两个 `Arc` 的引用计数，所以克隆几乎免费。

构造函数 `new`（[src/tests.rs:30-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L30-L37)）——注意 `singleton!` 让 `TestBase` 全进程只建一次：

```rust
pub fn new(text: &str) -> Self {
    let main = Source::new(Self::main_id(), text.into());
    Self {
        main,
        files: Arc::new(TestFiles::default()),
        base: singleton!(TestBase, TestBase::default()),
    }
}
```

`main_id`（[src/tests.rs:67-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L67-L73)）把主文件路径固定为虚拟项目根下的 `main.typ`，同样用 `singleton!` 保证 id 全局唯一、稳定。

`World` 实现（[src/tests.rs:76-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L76-L113)）的关键几行：`library` / `book` 转发 `base`，`source` 先比对主文件 id 再查 `files.sources`：

```rust
fn library(&self) -> &LazyHash<Library> { &self.base.library }
fn book(&self)    -> &LazyHash<FontBook> { &self.base.book }
fn source(&self, id: FileId) -> FileResult<Source> {
    if id == self.main.id() {
        Ok(self.main.clone())
    } else if let Some(source) = self.files.sources.get(&id) {
        Ok(source.clone())
    } else {
        Err(FileError::NotFound(id.vpath().get_without_slash().into()))
    }
}
```

> `LazyHash<T>` 是 typst 提供的「廉价克隆 + 缓存哈希」包装，常用于 comemo 需要按值缓存的对象（如 `Library`、`FontBook`）。

`IdeWorld` 实现（[src/tests.rs:115-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L115-L142)）：

```rust
impl IdeWorld for TestWorld {
    fn upcast(&self) -> &dyn World { self }
    fn files(&self) -> Vec<FileId> {
        std::iter::once(self.main.id())
            .chain(self.files.sources.keys().copied())
            .chain(self.files.assets.keys().copied())
            .collect()
    }
    fn packages(&self) -> &[(PackageSpec, Option<EcoString>)] { /* 写死的 example 包 */ }
}
```

这正是上一讲（u1-l2）「可选增强」在测试里的落地：`files()` 枚举所有文件（让 `file_completions` 有东西补），`packages()` 给一个固定的 `@preview/example:0.1.0`（让 `package_completions` 有东西补）。

#### 4.2.4 代码实践

**实践目标**：用最少的代码确认 `TestWorld` 同时满足两个 trait，并体会「只传一段字符串」就能跑测试的便利。

**操作步骤**：阅读 `tooltip.rs` 里的测试入口 `test`（[src/tooltip.rs:328-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L328-L334)）：

```rust
fn test(world: impl WorldLike, pos: impl FilePos, side: Side) -> Response {
    let world = world.acquire();
    let world = world.borrow();
    let (source, cursor) = pos.resolve(world);
    let doc = typst::compile::<PagedDocument>(world).output.ok();
    tooltip(world, doc.as_ref(), &source, cursor, side)
}
```

再读一个最简测试用例（[src/tooltip.rs:336-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L336-L341)）：

```rust
fn test_tooltip() {
    test("#let x = 1 + 2", -1, Side::After).must_be_none();
    test("#let x = 1 + 2", 5, Side::After).must_be_code("3");
}
```

**需要观察的现象**：`test` 的第一参数既可以是 `&str`（如 `"#let x = 1 + 2"`），也可以是 `&TestWorld`——这是 `WorldLike` trait 的功劳（见 4.4.3）。`"#let x = 1 + 2"` 这段字符串经 `TestWorld::new` 变成 world，再被 `typst::compile` 编译、`tooltip` 分析。

**预期结果**：光标在 `x` 上悬停得到 `Tooltip::Code("3")`，因为 `1 + 2` 在 trace 中被求值为 `3`。

#### 4.2.5 小练习与答案

**练习 1**：`TestWorld::new` 不接收任何字体或资源参数，为什么 `tooltip` 测试还能编译并算出排版结果？

**参考答案**：因为字体来自 `base`（共享的 `TestBase`），而 `TestBase::default()` 会从 `typst_assets::fonts()` 和 `typst_dev_assets::fonts()` 加载字体。所以即便 `new` 只传一段文字，底下的 `World::book()` / `font()` 仍返回真实字体，`typst::compile` 才能跑通。

**练习 2**：`files` 字段为什么用 `Arc<TestFiles>` 而不是直接 `TestFiles`？

**参考答案**：为了 COW。`TestWorld` 派生了 `Clone`，多份克隆共享同一份 `TestFiles`（只增引用计数，零拷贝）；而 `with_source` / `with_asset` 通过 `Arc::make_mut` 在需要修改时才真正复制。这让 builder 链式调用既安全又廉价。

---

### 4.3 TestBase —— 共享基础设施的懒初始化

#### 4.3.1 概念说明

每次 `TestWorld::new` 都重新加载全部字体、重建标准库，会让成百上千个测试慢得无法接受。`TestBase` 解决的就是这个问题：把**与具体测试无关、可全局复用**的重数据——标准库 `library`、字体簿 `book`、字体列表 `fonts`——抽出来，**全进程只构建一次**，所有 `TestWorld` 共享同一份。

#### 4.3.2 核心流程

```text
首次调用 singleton!(TestBase, TestBase::default())
   │
   ├── 触发 TestBase::default()
   │      ├── fonts = typst_assets::fonts()  ++ typst_dev_assets::fonts()  → Font::iter 展开
   │      ├── library = LazyHash::new(library())   ← 扩展过的标准库（页面尺寸已调）
   │      └── book = LazyHash::new(FontBook::from_fonts(&fonts))
   │
   └── 返回 &'static TestBase（之后所有 new 复用同一地址）
```

`library()` 函数里还做了一件对排版测试很重要的事：把页面宽度设为 120pt、四边 margin 设 10pt，使**正文区恰好 100pt 宽**；页面高度自动、字号 10pt，这样排版出来的尺寸大多是整齐的整数，方便断言。

#### 4.3.3 源码精读

`TestBase` 结构（[src/tests.rs:152-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L152-L156)）：

```rust
struct TestBase {
    library: LazyHash<Library>,
    book: LazyHash<FontBook>,
    fonts: Vec<Font>,
}
```

`Default` 实现负责加载字体、构造库与字体簿（[src/tests.rs:158-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L158-L171)）：

```rust
impl Default for TestBase {
    fn default() -> Self {
        let fonts: Vec<_> = typst_assets::fonts()
            .chain(typst_dev_assets::fonts())
            .flat_map(|data| Font::iter(Bytes::new(data)))
            .collect();
        Self {
            library: LazyHash::new(library()),
            book: LazyHash::new(FontBook::from_fonts(&fonts)),
            fonts,
        }
    }
}
```

注意两个 dev-deps 的字体流被 `chain` 拼接，再 `flat_map(Font::iter)`——一个字体文件可能含多个字重/字形，`Font::iter` 把它们逐个展开。

扩展标准库（[src/tests.rs:174-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L174-L187)）设置了便于断言的页面参数：

```rust
fn library() -> Library {
    let mut lib = typst::Library::builder().with_features(Features::all()).build();
    lib.styles.set(PageElem::width,  Smart::Custom(Abs::pt(120.0).into()));
    lib.styles.set(PageElem::height, Smart::Auto);
    lib.styles.set(PageElem::margin,  Smart::Custom(Margin::splat(/*10pt*/)));
    lib.styles.set(TextElem::size,    TextSize(Abs::pt(10.0).into()));
    lib
}
```

正文字号 10pt、正文区宽 100pt，意味着文本行能放下的字符数、元素尺寸都趋于整齐，这正是 `Features::all()` 之外测试库所做的唯一「定制」。

> 复用机制：`singleton!` 返回的 `&'static TestBase` 被 `TestWorld.base` 持有。因此哪怕你 `TestWorld::new(...)` 一千次，`TestBase::default()` 的字体加载与库构造只发生一次——这是注释里「This is cheap because the shared base … is lazily initialized just once」的含义（[src/tests.rs:26-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L26-L29)）。

#### 4.3.4 代码实践

**实践目标**：验证 `TestBase` 确实只初始化一次（即「共享 library 和 book」）。

**操作步骤**：

1. 阅读 `new`（[src/tests.rs:30-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L30-L37)）与 `library`（[src/tests.rs:174-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L174-L187)）。
2. （可选，待本地验证）在 `TestBase::default` 末尾临时加一行 `eprintln!("base built");`，运行 `cargo test -p typst-ide test_tooltip`，观察「base built」打印次数。

**需要观察的现象**：尽管有大量测试各自 `TestWorld::new`，「base built」应当只出现一次（或极少数几次，取决于 singleton 的线程模型）。

**预期结果**：证实 `library` / `book` / `fonts` 被所有 `TestWorld` 共享，构造测试的开销主要在 `Source::new` 这类轻量操作上。

> 本实践会临时改动测试源码。请勿提交该改动；验证后立即还原。若不便改动，可直接做源码阅读型实践：跟踪 `singleton!` 在 `new`、`main_id` 两处返回同一全局实例即可。

#### 4.3.5 小练习与答案

**练习 1**：页面宽设为 120pt、margin 设为 10pt。正文区宽度是多少？为什么要选这个值？

**参考答案**：正文区宽 \(120 - 2 \times 10 = 100\)pt。选这个值是因为字号也是 10pt，配合 `Features::all()` 后排版结果多为整齐的整数，便于在 tooltip / layout 相关断言里给出稳定的期望值。

**练习 2**：`library` 和 `book` 都用 `LazyHash` 包裹。`book` 之外另存了 `fonts: Vec<Font>`，为什么 `book` 不够用？

**参考答案**：`FontBook` 是「字体簿/索引」，用于按名字查询字体（返回下标），供补全与字体匹配用；而 `font(idx)` 要按下标返回真正的 `Font` 对象用于排版。两者职责不同，所以 `TestBase` 同时持有 `book`（查询）与 `fonts`（按下标取实际字体数据）。

---

### 4.4 with_source 与 with_asset —— 多文件测试场景构造

#### 4.4.1 概念说明

只靠主文件 `main.typ` 不足以测试 typst-ide 的很多能力——跨文件 `#import`、`#include`、`#image("...")`、`#bibliography("...")` 都依赖额外的源码文件或二进制资源。`with_source` 和 `with_asset` 就是往 `TestWorld` 里**追加**这些文件的方法。它们采用 builder 模式：`self` 进、`self` 出，可以链式调用。

#### 4.4.2 核心流程

```text
TestWorld::new("#import \"other.typ\": x; #x")
   │  仅含主文件 main.typ
   │
   ├── .with_source("other.typ", "#let x = 1")
   │      → 用 RootedPath 把 "other.typ" intern 成 FileId
   │      → Arc::make_mut 克隆（若被共享）后插入 sources
   │
   ├── .with_asset("works.bib")
   │      → = with_asset_at("works.bib", "works.bib")
   │      → 虚拟路径与 dev-asset 名同名
   │      → typst_dev_assets::get_by_name("works.bib") 取字节，插入 assets
   │
   └── .with_asset_at("assets/tiger.jpg", "tiger.jpg")
          → 虚拟路径 "assets/tiger.jpg"，资源名 "tiger.jpg"
          → 插入 assets
```

关键点：`with_asset_at(path, filename)` 把两个概念分开——`path` 是文件在虚拟项目里的位置（决定 `FileId`），`filename` 是去 `typst_dev_assets` 取真实数据时用的名字。

#### 4.4.3 源码精读

`with_source`（[src/tests.rs:40-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L40-L47)）：

```rust
pub fn with_source(mut self, path: &str, text: &str) -> Self {
    let id = RootedPath::new(VirtualRoot::Project, VirtualPath::new(path).unwrap())
        .intern();
    let source = Source::new(id, text.into());
    Arc::make_mut(&mut self.files).sources.insert(id, source);
    self
}
```

`RootedPath::new(VirtualRoot::Project, ...)` 把路径挂在「虚拟项目根」下，`.intern()` 内部化成稳定 `FileId`。`Arc::make_mut` 保证：若该 `TestFiles` 仅当前 world 独占，就原地修改；否则先克隆再改——这就是 COW 让链式 builder 安全的机制。

`with_asset`（[src/tests.rs:50-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L50-L53)）是 `with_asset_at` 的便捷形式（路径与资源名相同）：

```rust
pub fn with_asset(self, filename: &str) -> Self {
    self.with_asset_at(filename, filename)
}
```

`with_asset_at`（[src/tests.rs:56-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L56-L64)）通过 `typst_dev_assets::get_by_name` 取真实资源字节：

```rust
pub fn with_asset_at(mut self, path: &str, filename: &str) -> Self {
    let id = RootedPath::new(VirtualRoot::Project, VirtualPath::new(path).unwrap())
        .intern();
    let data = typst_dev_assets::get_by_name(filename).unwrap();
    let bytes = Bytes::new(data);
    Arc::make_mut(&mut self.files).assets.insert(id, bytes);
    self
}
```

存放结构 `TestFiles`（[src/tests.rs:145-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L145-L149)）就是两张 `FxHashMap`：

```rust
struct TestFiles {
    assets: FxHashMap<FileId, Bytes>,
    sources: FxHashMap<FileId, Source>,
}
```

这正好对应 `World::source`（查 `sources`）和 `World::file`（查 `assets`）两个方法。

> 真实用例可参见 `complete.rs` 的导入补全测试（[src/complete.rs:1916-1928](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1916-L1928)）：它用 `with_source` 同时建了 `main.typ`、`second.typ`、`other.typ` 三个文件，验证在 `main.typ` 与 `second.typ` 不同光标处的 import 项补全差异。

**测试如何消费 TestWorld**（顺带认识两个小工具，u8-l1 会深入）：`WorldLike`（[src/tests.rs:190-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L190-L210)）让测试入参既能传 `&str`（自动 `TestWorld::new`）也能传 `&TestWorld`；`FilePos`（[src/tests.rs:214-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L214-L234)）让光标既能用一个 `isize`（主文件偏移，负数从末尾倒数）也能用 `(&str, isize)`（指定文件名 + 偏移）。负数光标的换算在 `cursor`（[src/tests.rs:238-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L238-L244)）里：

```rust
fn cursor(source: &Source, cursor: isize) -> usize {
    if cursor < 0 {
        source.text().len().checked_add_signed(cursor + 1).unwrap()
    } else {
        cursor as usize
    }
}
```

即 `-1` 表示字符串最后一个位置（末尾后一位）。这就是 `test("#let x = 1 + 2", -1, ...)` 中 `-1` 的含义。

#### 4.4.4 代码实践

**实践目标**：用 `with_source` 构造一个跨文件 import 场景，并解释它如何复用共享的 `library` 和 `book`。

**操作步骤**：在 `src/tooltip.rs` 的 `#[cfg(test)]` 模块里，仿照现有测试风格新增一个测试（示例代码，非项目原有代码）：

```rust
// 示例代码：演示跨文件 import 测试场景的构造
#[test]
fn test_cross_file_import_world() {
    let world = TestWorld::new("#import \"other.typ\": x\n#x")
        .with_source("other.typ", "#let x = 1");
    // 在 x 上悬停（用 FilePos 的 (path, cursor) 形式定位主文件里的 #x）
    test(&world, ("main.typ", 24), Side::After).must_be_code("1");
}
```

> 这里借用了 `tooltip.rs` 里既有的 `test` / `must_be_code`（[src/tooltip.rs:328-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L328-L334)）。光标偏移 `24` 对应主文件第二个 `#x` 的 `x` 字符；具体下标请以本地实际文本为准（待本地验证）。

**需要观察的现象与思考**：

1. `with_source("other.typ", "#let x = 1")` 把 `other.typ` 加进 `files.sources`；`#import "other.typ": x` 时，`World::source(other.typ 的 id)` 会命中这张表并返回该源码。
2. 该 world **没有**自带 `library` / `book` / `fonts`——它们全部来自 `base`，而 `base` 是 `singleton!(TestBase, ...)` 返回的同一个全局实例。因此本测试与任何其他测试共用同一份标准库和字体簿。
3. 由于 `library` 是 `&'static` 共享的，`TestWorld` 之间不会互相干扰：每个 world 只拥有自己的 `main` + `files`，公共部分只读共享。

**预期结果**：在 `#x`（来自 `other.typ` 的 `x`，值为 1）上悬停应得到 `Tooltip::Code("1")`；构造过程没有重新加载任何字体。

> 若不想新增测试，可改为源码阅读型实践：对照 `complete.rs:1916-1928` 的真实测试，逐行解释每个 `with_source` 在 `files.sources` 中插入了什么、`test(&world, ("second.typ", 23))` 的光标为何落在 `second.typ` 里。

#### 4.4.5 小练习与答案

**练习 1**：`with_asset_at("assets/tiger.jpg", "tiger.jpg")` 的两个参数分别是什么含义？如果写反会怎样？

**参考答案**：第一个 `path`（`"assets/tiger.jpg"`）是资源在虚拟项目文件系统里的位置，决定 `FileId`——`#image("assets/tiger.jpg")` 就是用这个路径去找它。第二个 `filename`（`"tiger.jpg"`）是去 `typst_dev_assets::get_by_name` 取真实字节时用的资源名。写反会导致：要么虚拟路径变成 `tiger.jpg`（与 `#image` 调用对不上而 `NotFound`），要么用 `"assets/tiger.jpg"` 当资源名去 dev-assets 里查（查不到而 `unwrap` panic）。

**练习 2**：为什么 `with_source` / `with_asset` 用 `Arc::make_mut` 而不是 `&mut self`？

**参考答案**：因为它们是 builder 风格（`self` 进 `self` 出），且 `TestWorld` 是 `Clone` 的——可能有多处持有同一个 `Arc<TestFiles>`。`Arc::make_mut` 在引用计数为 1 时原地改、大于 1 时先克隆，从而在保证「不改影响其他克隆」的同时，独占时零开销。`&mut self` 做不到对共享数据的受控复制。

**练习 3**：`IdeWorld::files()` 的实现把 main、所有 sources、所有 assets 的 id 都列出来。哪个补全功能会直接消费它？

**参考答案**：`file_completions`（即「路径补全」）会枚举 `files()` 返回的 `FileId`，把它们作为可补全的文件路径呈现给用户。这正是 `with_source` / `with_asset` 往 world 里加文件后，路径补全就有候选可补的原因。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个贯穿任务：

**任务**：阅读并解释 `complete.rs` 里的导入补全测试（[src/complete.rs:1916-1928](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1916-L1928)），再自行构造一个等价的「最小」版本。

```rust
let world = TestWorld::new("#import \"other.typ\": ")
    .with_source("second.typ", "#import \"other.typ\": th")
    .with_source("other.typ", "#let this = 1; #let that = 2");
```

请回答并验证：

1. **构建与依赖**：这个测试运行时，字体从哪两个 dev-deps 来？（答：`typst-assets` 与 `typst-dev-assets`，由 `TestBase::default` 加载。）
2. **TestWorld 结构**：构造完成后，`files.sources` 里有几个条目？分别是哪些 `FileId`？（答：两个——`second.typ` 与 `other.typ`；主文件 `main.typ` 不在 `files.sources`，而由 `main` 字段直接持有。）
3. **TestBase 复用**：`library` 和 `book` 是这个 world 自己构建的吗？（答：不是，它们来自 `singleton!` 返回的全局 `TestBase`，全进程只建一次。）
4. **with_source 行为**：为什么能在 `main.typ` 和 `second.typ` 两个不同文件的光标处分别测试 import 补全？（答：因为 `with_source` 把它们都加进了 `files.sources`，`FilePos` 的 `(&str, isize)` 形式能按文件名 `resolve` 出对应 `Source`。）
5. **动手**：在 `complete.rs` 的测试模块里复制该测试，把断言改成 `.must_include(["this"])` 并单独运行 `cargo test -p typst-ide test_autocomplete_import_items`，观察是否通过。（待本地验证；请勿提交临时改动。）

完成该任务意味着你已经能：看懂 dev-deps 的作用、读懂 `TestWorld` 的字段与 trait 实现、理解 `TestBase` 的共享懒初始化、并用 `with_source` 构造跨文件场景。

## 6. 本讲小结

- typst-ide 用标准 `cargo build` / `cargo test` 构建；测试专用的重资源（字体、图片、bib）由 dev-dependencies `typst-assets`、`typst-dev-assets`、`once_cell` 提供，不进发布产物。
- `TestWorld` 是一个最小但完整的 `World + IdeWorld` 实现：主源码 + 额外文件 + 指向共享 `TestBase` 的 `&'static` 引用，克隆与构造都极廉价。
- `TestBase` 把标准库 `library`、字体簿 `book`、字体列表 `fonts` 抽出来，经 `singleton!` 全进程只构建一次，使成百上千个测试复用同一份重数据。
- 测试标准库特意把页面设为 120pt 宽、10pt margin（正文区 100pt）、字号 10pt，使排版结果多为整齐整数，便于断言。
- `with_source` / `with_asset`(/`with_asset_at`) 用 COW 的 builder 模式往 world 里追加源码与二进制资源，支持跨文件 `import` / `include` / `image` / `bibliography` 等场景的测试。
- `WorldLike` / `FilePos` / `cursor` 让测试入参既能写裸字符串与整数偏移，也能写完整的 `&TestWorld` 与 `(path, cursor)`；负数光标从字符串末尾倒数（`-1` 为最末位）。

## 7. 下一步学习建议

到这里，你已经能构造测试场景、能运行 typst-ide 的测试了。接下来的学习有两条推荐路径：

- **深入 IDE 基石（第 2 单元）**：本讲只是「把数据喂进去」。真正决定补全/悬停/跳转行为的是语法树定位、表达式归类、作用域收集与值推断。建议下一篇读 u2-l1「从光标到语法树节点」，理解 `leaf_at` 与 `Side` 如何把一个光标变成语法树节点。
- **延后阅读测试体系细节（u8-l1）**：本讲顺带提到的 `WorldLike` / `FilePos` / `ResponseExt` 链式断言会在「测试体系与断言扩展」里系统讲解，到时可回头对照本讲的示例。
