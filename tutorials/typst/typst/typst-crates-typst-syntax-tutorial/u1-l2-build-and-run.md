# 构建、依赖与运行方式

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局认知：`typst-syntax` 是 Typst 编译前端，负责把源码文本解析成语法树，同时维护 CST/AST、Span 定位、FileId、增量重解析和语法高亮。这一讲我们要回答一个最实际的问题：

> **这个 crate 到底怎么构建、怎么测试、怎么在自己的程序里跑起来？**

学完本讲你应该能够：

- 看懂 `typst-syntax` 的 `Cargo.toml`，理解它和仓库根 `Cargo.toml`（workspace）的关系。
- 说出每一项外部依赖（`ecow`、`unscanny`、`unicode-*`、`rustc-hash` 等）大致解决什么问题。
- 用 `cargo` 单独构建和测试这个 crate（`cargo build -p typst-syntax` / `cargo test -p typst-syntax`）。
- 在一个最小 Rust 程序里调用 `Source::detached("= Hello")`，拿到语法树的根节点并打印它的 `kind`。
- 说清 `parse`、`Source::new`、`Source::detached` 三者各自的职责与调用关系。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个本讲会用到的概念。

### 2.1 Cargo workspace（工作空间）

一个大型 Rust 项目往往包含很多 crate。Cargo 允许把多个 crate 放在同一个 **workspace** 里统一管理：版本号、作者、license、外部依赖的版本、编译 profile、lint 规则等都可以在仓库根的 `Cargo.toml` 的 `[workspace]` 段集中声明一次，子 crate 用 `{ workspace = true }` 引用即可。

好处是：避免每个 crate 重复维护相同信息，也保证整个 monorepo（单一仓库）里所有 crate 用同一版本的依赖，不会「依赖版本打架」。

Typst 仓库就是这样一个 workspace，`typst-syntax` 是其中的一个成员 crate。

### 2.2 「解析一次」最起码需要什么

要把一段 Typst 源码文本变成可查询的语法树，最低限度需要：

1. 一段文本（`&str` / `String`）。
2. 一个解析函数，把文本变成 CST 的根节点（`SyntaxNode`）。
3. （可选）一个文件身份 `FileId`，用来给节点分配 `Span`，方便后续定位。
4. （可选）一个行索引结构 `Lines`，用来做「字节偏移 ↔ 行列」的换算。

`Source` 这个类型正是把 1～4 打包在一起的不可变值，是本 crate 对外最常用的入口之一。

### 2.3 上一讲的关键结论回顾

- **CST 是唯一真相来源**，`SyntaxNode` 是它的载体；AST 是从 CST 按需转换的类型化视图。
- **`SyntaxMode` 有三种**：`Markup`（正文）、`Math`（公式）、`Code`（`#` 后的代码）。
- **`lib.rs` 是 crate 门面**，用 `pub mod` / `mod` / `pub use` 控制对外暴露哪些类型。

本讲会用到 `lib.rs` 暴露的 `parse`、`Source`、`SyntaxKind` 三个名字。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用它 |
| --- | --- | --- |
| `Cargo.toml`（本 crate） | 声明 `typst-syntax` 自身的 `[package]` 与 `[dependencies]` | 看它依赖了什么 |
| `Cargo.toml`（仓库根） | workspace 配置：`[workspace.package]`、`[workspace.dependencies]`、profile、lints | 理解 `{ workspace = true }` 的来源 |
| `src/lib.rs` | crate 门面，模块声明与 `pub use` | 找到 `parse` / `Source` / `SyntaxKind` 的导出处 |
| `src/parser.rs` | 语法分析器，提供 `parse` / `parse_code` / `parse_math` 三个入口 | 看最底层的 `parse` 函数 |
| `src/source.rs` | `Source` 文件抽象，封装「文本 + 行索引 + 语法树」 | 看 `Source::new` / `Source::detached` |

> 注意：本讲只读这几个文件的「入口部分」，深入解析器内部、`Source::edit` 的增量重解析等留到进阶/专家层讲义。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 `Cargo.toml` 与 workspace 依赖配置
- 4.2 `parse` 解析入口
- 4.3 `Source::detached` 与 `Source::new`

### 4.1 Cargo.toml 与 workspace 依赖配置

#### 4.1.1 概念说明

打开 `typst-syntax` 自己的 `Cargo.toml`，你会发现它**非常短**：`[package]` 段几乎全是 `{ workspace = true }`，`[dependencies]` 段也只有十来行，而且每一项也都是 `{ workspace = true }`。

这是因为所有「真正的值」（版本号、作者、license、各依赖的确切版本）都集中写在仓库根的 `Cargo.toml` 里。子 crate 只声明「我要用 workspace 里定义的那一项」，这就是 `{ workspace = true }` 的含义。

#### 4.1.2 核心流程

依赖解析的流程可以概括为：

1. Cargo 读到子 crate 的 `Cargo.toml`，发现 `ecow = { workspace = true }`。
2. Cargo 转向仓库根 `Cargo.toml` 的 `[workspace.dependencies]`，查到 `ecow = { version = "0.2.6", features = ["serde"] }`。
3. 把这个完整定义「展开」到子 crate 上，等价于子 crate 直接写了 `ecow = { version = "0.2.6", features = ["serde"] }`。

编译 profile（`[profile.dev]` 等）和 lint 规则（`[workspace.lints.clippy]`）同理，都在根 `Cargo.toml` 集中定义，子 crate 用 `[lints] workspace = true` 引用。

#### 4.1.3 源码精读

先看本 crate 的 `[dependencies]` 段，这是我们最关心的部分：

[Cargo.toml:L15-L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/Cargo.toml#L15-L26) — 列出 `typst-syntax` 的全部直接依赖，每项都用 `{ workspace = true }` 引用 workspace 的统一定义。

逐项理解它们的作用（这是本讲要建立的关键词汇表）：

| 依赖 | 解决什么问题 |
| --- | --- |
| `typst-timing` | Typst 内部的性能计时（解析时会用 `TimingScope` 打点） |
| `typst-utils` | Typst 通用工具，例如 `LazyHash`（让 `Source` 廉价哈希） |
| `ecow` | 「生态字符串」`EcoString` 等紧凑、可克隆的字符串/向量类型 |
| `rustc-hash` | 高性能哈希（`FxHashMap`/`FxHashSet`），替代默认 `HashMap` |
| `serde` | 序列化框架（解析 `typst.toml` 包清单、给 AST 提供序列化能力） |
| `toml` | 解析 `typst.toml` 包清单文件 |
| `unicode-ident` | 判定标识符首字符 / 后续字符是否合法（XID_Start / XID_Continue） |
| `unicode-math-class` | 数学符号的 Unicode 数学类别（`MathClass`） |
| `unicode-script` | 判定字符所属文字系统（如数学标识符是否为希腊字母） |
| `unicode-segmentation` | Unicode 文本分段（用于换行、词边界判定） |
| `unscanny` | 轻量扫描器，词法分析器 `Lexer` 的游标基础 |

> 你会发现这些依赖和 u1-l1 讲过的职责一一对应：`unscanny` + `unicode-*` 支撑词法分析；`serde` + `toml` 支撑包清单；`typst-utils` 的 `LazyHash` 支撑 `Source` 的廉价克隆与哈希。

再看 `[package]` 段如何全部「外包」给 workspace：

[Cargo.toml:L1-L13](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/Cargo.toml#L1-L13) — `name` 和 `description` 是写死的（因为这是本 crate 独有的），其余 `version`、`rust-version`、`edition`、`license` 等全部 `{ workspace = true }`。

这些值的真身在仓库根 `Cargo.toml`：

[Cargo.toml:L6-L16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/Cargo.toml#L6-L16) — `[workspace.package]` 集中定义版本 `0.15.1`、`rust-version = "1.92"`、`edition = "2024"`、license 等。

[Cargo.toml:L18-L36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/Cargo.toml#L18-L36) — `[workspace.dependencies]` 中 Typst 自家各 crate 的版本与 path，`typst-syntax = { path = "crates/typst-syntax", version = "0.15.1" }` 也在其中。

而上面表格里外部依赖的确切版本，例如：

- 根 `Cargo.toml` 第 56 行：`ecow = { version = "0.2.6", features = ["serde"] }`
- 根 `Cargo.toml` 第 144 行：`unscanny = "0.1"`
- 根 `Cargo.toml` 第 139 行：`unicode-ident = "1.0"`

另外注意 workspace 还配置了让开发更快的 profile：

[Cargo.toml:L156-L157](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/Cargo.toml#L156-L157) — `[profile.dev.package."*"]` 把**所有第三方依赖**在 debug 构建下也开到 `opt-level = 2`。这样本 crate 自己的代码仍快速重编译，而像 `unscanny` 这类依赖又能跑得够快，是 monorepo 里很实用的权衡。

#### 4.1.4 代码实践

**实践目标**：亲手构建一次 `typst-syntax`，并查看它真正的依赖来源，验证「`{ workspace = true }` 会被展开」。

**操作步骤**（在仓库根目录执行）：

1. 单独构建本 crate（`-p` 指定 package）：
   ```sh
   cargo build -p typst-syntax
   ```
2. 查看依赖树（只看一层直接依赖）：
   ```sh
   cargo tree -p typst-syntax --depth 1
   ```
3. 想确认某个依赖的确切版本来源，例如 `ecow`：
   ```sh
   cargo tree -p typst-syntax --workspace -i ecow
   ```

**需要观察的现象**：

- `cargo build -p typst-syntax` 能成功，不需要编译整个 Typst CLI。
- `cargo tree` 输出里能看到 `ecow`、`unscanny`、`unicode-ident` 等依赖的具体版本号（来自根 `Cargo.toml`）。

**预期结果**：构建成功，依赖树与本节表格列出的依赖一致。如果网络拉取 crate 失败，属于环境问题，不是代码问题。

#### 4.1.5 小练习与答案

**练习 1**：本 crate 的 `Cargo.toml` 里没有写 `edition = "2024"`，但编译时用的就是 2024 edition，为什么？

> **答案**：因为 `edition = { workspace = true }`，真正的值在根 `Cargo.toml` 的 `[workspace.package]` 里（第 10 行 `edition = "2024"`）。

**练习 2**：如果要把 `ecow` 升级到 `0.3`，应该改哪个文件？

> **答案**：改仓库根 `Cargo.toml` 的 `[workspace.dependencies]`（第 56 行）一处即可，所有 `{ workspace = true }` 引用它的 crate 都会跟着升级。

---

### 4.2 parse 解析入口

#### 4.2.1 概念说明

`parse` 是整个 crate 最底层的解析入口：输入一段文本，输出 CST 的根节点 `SyntaxNode`。它不关心文件身份、不关心行号，只做「文本 → 语法树」这一件事。

注意 `parse` 产出的根节点此时**还没有 Span 编号**（numberize 还没跑），所以它只是「裸树」。给它编号、套上 `Source` 外壳是下一步（4.3）的事。

#### 4.2.2 核心流程

`parse` 的执行流程非常短：

1. 创建一个 `TimingScope`（性能计时打点，名字是 `"parse"`）。
2. 用文本和初始偏移 `0`、模式 `SyntaxMode::Markup` 构造一个 `Parser`。
3. 调用 `markup_exprs(&mut p, true, ...)` 递归下降地解析一串 markup 表达式。
4. 调用 `p.finish_into(SyntaxKind::Markup)` 把解析事件收尾，包装成根 kind 为 `Markup` 的 `SyntaxNode` 返回。

也就是说，`parse` 默认把整段文本当作**顶层正文（Markup）**来解析——这正是 Typst 文档主体的语法模式。

#### 4.2.3 源码精读

看三个并列的入口函数，理解它们的「对称结构」：

[src/parser.rs:L16-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L16-L21) — `pub fn parse(text: &str) -> SyntaxNode`，以 `SyntaxMode::Markup` 解析，根 kind 包装为 `SyntaxKind::Markup`。

[src/parser.rs:L24-L29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L24-L29) — `pub fn parse_code(text: &str) -> SyntaxNode`，以 `SyntaxMode::Code` 解析，根 kind 为 `SyntaxKind::Code`。

[src/parser.rs:L32-L37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L32-L37) — `pub fn parse_math(text: &str) -> SyntaxNode`，以 `SyntaxMode::Math` 解析，根 kind 为 `SyntaxKind::Math`。

> 三个函数的结构完全一致，区别只在 `SyntaxMode`（决定词法/语法规则）和根 `SyntaxKind`（决定 CST 根节点的标签）。这正好对应 u1-l1 讲过的三种语法模式。

这三个函数在 `lib.rs` 里被整体导出，所以外部直接 `typst_syntax::parse(...)` 就能调用：

[src/lib.rs:L28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L28) — `pub use self::parser::{parse, parse_code, parse_math};` 把三个入口暴露到 crate 根。

#### 4.2.4 代码实践

**实践目标**：直接用 `parse` 跑一次解析，观察根节点的 kind。

**操作步骤**（在仓库根目录）：

1. 运行本 crate 的内联测试，确认环境正常：
   ```sh
   cargo test -p typst-syntax --lib
   ```
2. 想亲手验证 `parse` 行为，可以在仓库内临时加一个测试函数（实践完可以删掉，**不要提交**），例如在 `src/source.rs` 的 `#[cfg(test)] mod test` 里加：
   ```rust
   #[test]
   fn practice_parse_root_kind() {
       let root = crate::parse("= Hello");
       assert_eq!(root.kind(), crate::SyntaxKind::Markup);
       eprintln!("{:#?}", root);
   }
   ```
   再运行 `cargo test -p typst-syntax practice_parse_root_kind -- --nocapture`。

**需要观察的现象**：`root.kind()` 等于 `SyntaxKind::Markup`；`Debug` 打印能看到一棵树，根节点是 `Markup`，下面挂着标题 `Heading` 等子节点。

**预期结果**：断言通过，打印出的树根节点 kind 为 `Markup`。若不想改源码，也可改用下一节 4.3 的外部项目方式来调用 `parse`，效果相同。

#### 4.2.5 小练习与答案

**练习 1**：`parse("= Hello")` 和 `parse_code("= Hello")` 产出的根节点 kind 分别是什么？

> **答案**：分别是 `SyntaxKind::Markup` 和 `SyntaxKind::Code`。因为 `=` 在 Markup 模式下是标题标记，在 Code 模式下则不是合法的赋值语法（会变成错误节点），但根 kind 仍由入口函数固定包装决定。

**练习 2**：为什么 `parse` 产出的根节点「还没有 Span」？

> **答案**：`parse` 只负责构造 CST 树形结构；给每个节点分配编号（`numberize`）是 `Source::new` 里才做的（见 4.3）。分离这两步是为了让 `parse` 可以被增量重解析等场景复用。

---

### 4.3 Source::detached 与 Source::new

#### 4.3.1 概念说明

`Source` 是本 crate 对外最常用的类型之一。它把「文件身份 + 全文文本 + 行索引 + 语法树」打包成一个**不可变、可廉价克隆与哈希**的值。下游（求值、IDE、高亮）几乎都从 `Source` 出发。

但很多时候我们只想快速试一下解析、写个测试，并不真的关心文件路径。这时就用 `Source::detached`：它内部帮你拼一个「假的」项目根路径 `main.typ`，然后转交给 `Source::new`。

#### 4.3.2 核心流程

`Source::new(id, text)` 的流程：

1. 计时打点 `"create source"`。
2. `let mut root = parse(&text);` —— 先得到裸 CST。
3. `root.numberize(id, Span::FULL).unwrap();` —— 给整棵树分配带 `FileId` 的 `Span` 编号。
4. 构造 `Lines::new(text)` —— 建立字节/行/列/UTF-16 索引。
5. 把 `id`、`lines`、`root` 装进 `SourceInner`，再用 `Arc<LazyHash<...>>` 包一层得到 `Source`。

`Source::detached(text)` 只是把第 1 步需要的 `id` 用一个固定路径造出来：

```
RootedPath::new(VirtualRoot::Project, VirtualPath::new("main.typ").unwrap()).intern()
```

也就是「项目根下的 `main.typ`」。然后调用 `Source::new(id, text.into())`。

#### 4.3.3 源码精读

先看 `Source` 与其内部表示：

[src/source.rs:L23-L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L23-L32) — `pub struct Source(Arc<LazyHash<SourceInner>>)`，`SourceInner` 含 `id: FileId`、`root: SyntaxNode`、`lines: Lines<String>` 三个字段。`#[derive(Clone, Hash)]` 让 `Source` 可以廉价克隆（`Arc`）并被 `LazyHash` 缓存哈希。

> 这里的 `Arc<LazyHash<...>>` 就是 4.1 里依赖 `typst-utils`（提供 `LazyHash`）的原因。`LazyHash` 只在第一次需要时计算哈希并缓存，配合 `Arc` 实现了「克隆和哈希都很便宜」——这对 Typst 的增量编译（记忆化缓存以 `Source` 为 key）至关重要。

接着是构造器三件套：

[src/source.rs:L36-L41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) — `Source::new(id, text)`：依次 `parse` → `numberize` → `Lines::new`，是「正规」构造入口。

[src/source.rs:L44-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L44-L50) — `Source::detached(text)`：用固定路径 `main.typ` 造一个 `FileId`，再转交 `Source::new`。文档明确说「usually for testing」。

[src/source.rs:L53-L55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L53-L55) — `Source::with_root(id, text, root)`：跳过 `parse`，直接用调用者已经建好的语法树（不调用 `numberize`，调用方需自行保证已编号）。增量重解析等高级场景会用它。

访问方法：

[src/source.rs:L57-L76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L57-L76) — `root()` / `id()` / `text()` / `lines()` 四个只读访问器，分别返回 CST 根、文件 id、全文文本、行索引。

最后看一个现成的测试，体会 `detached` 的典型用法：

[src/source.rs:L162-L181](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L162-L181) — `test_source_sub_ranges`：用 `Source::detached("= head <label>")` 建源，再结合 `LinkedNode` 与 `SubRange` 做子区间定位。这正是 `detached` 的典型场景：测试里不需要真实文件路径。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：在仓库外新建一个最小 Rust 程序，用 `Source::detached("= Hello")` 解析文本，打印根节点的 `kind`，验证它等于 `SyntaxKind::Markup`。

**前置条件**：本仓库 `rust-version = "1.92"`、`edition = "2024"`，请先用 [rustup](https://rustup.rs/) 安装足够新的稳定版 Rust 工具链。

**方式 A：仓库内直接跑测试（最可靠，推荐先做）**

在仓库根执行：
```sh
cargo test -p typst-syntax --lib
```
这会编译并运行 `typst-syntax` 内联的所有 `#[test]`（包括上面那个 `test_source_sub_ranges`），等价于验证了 `Source::detached` 的可用性。这条命令不依赖 crates.io 是否发布了对应版本，是最稳的第一步。

**方式 B：仓库外新建独立项目**

```sh
# 1. 在仓库之外新建项目
cargo new syntax-demo
cd syntax-demo
```

把 `syntax-demo/Cargo.toml` 的 `[dependencies]` 改为通过 path 指向你克隆的 typst 仓库里的本 crate（这样 `typst-syntax` 自身的 workspace 依赖也能正确解析）：

```toml
[package]
name = "syntax-demo"
version = "0.1.0"
edition = "2024"

[dependencies]
# 把路径换成你本机 typst 仓库里 typst-syntax 的实际路径
typst-syntax = { path = "../typst/crates/typst-syntax" }
```

> 说明：`typst-syntax` 内部的 `typst-timing`、`typst-utils` 等都是 `{ workspace = true }` 的 path 依赖，Cargo 会以 `typst-syntax` 所在的 workspace 为准来解析它们，因此指向完整克隆仓库的 path 依赖可以正常工作。
>
> 如果想用 crates.io 上的发布版本（`typst-syntax = "0.15"`），需确认对应版本已发布且与你本地的工具链兼容；此处的版本可用性**待本地验证**。

把 `syntax-demo/src/main.rs` 写成：

```rust
// 示例代码：调用 typst-syntax 解析一段 Typst 文本
use typst_syntax::{Source, SyntaxKind};

fn main() {
    // detached：内部用一个假路径 main.typ 造 FileId，适合测试/演示
    let source = Source::detached("= Hello");

    // root() 返回 CST 根节点的引用
    let root = source.root();
    println!("root kind   = {:?}", root.kind());
    println!("file id     = {:?}", source.id());
    println!("whole text  = {:?}", source.text());

    // 断言根节点是 Markup（对应顶层正文模式）
    assert_eq!(root.kind(), SyntaxKind::Markup);
    println!("OK: 根节点 kind 确实是 Markup");
}
```

运行：
```sh
cargo run
```

**需要观察的现象**：

- `root kind` 打印为 `Markup`。
- `whole text` 打印为 `"= Hello"`。
- 断言通过，程序输出 `OK: 根节点 kind 确实是 Markup`。

**预期结果**：终端大致输出：
```
root kind   = Markup
file id     = FileId(...)
whole text  = "= Hello"
OK: 根节点 kind 确实是 Markup
```

> 若在「方式 B」中遇到依赖解析问题，回退到「方式 A」即可完成验证。本实践不假装已经替你运行过——上面输出是你应当看到的结果。

#### 4.3.5 小练习与答案

**练习 1**：`Source::detached("= Hello")` 和直接 `parse("= Hello")` 得到的根节点，`kind` 相同吗？区别在哪？

> **答案**：`kind` 相同，都是 `SyntaxKind::Markup`。区别是：`Source::detached` 多做了 `numberize`（给每个节点分配带 `FileId` 的 `Span`）和建立 `Lines` 行索引，因此它的节点可以被 `source.find(span)` 定位；而裸 `parse` 的节点没有编号。

**练习 2**：为什么 `Source` 要用 `Arc<LazyHash<SourceInner>>` 包裹，而不是直接持有 `SourceInner`？

> **答案**：`Arc` 让 `Source` 可以被廉价克隆并共享同一份不可变数据（下游多处持有同一源文件很常见）；`LazyHash` 让哈希只算一次并缓存。Typst 的增量编译会把 `Source` 作为缓存 key，廉价克隆 + 廉价哈希是性能必需。

**练习 3**：`Source::with_root` 和 `Source::new` 的关键区别是什么？

> **答案**：`Source::new` 自己调用 `parse` 和 `numberize`；`Source::with_root` 接受一个**已经构造好**的 `SyntaxNode`，不再 `parse`、也不再 `numberize`（调用方需自行保证已编号）。它服务于增量重解析等「复用既有语法树」的场景。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务：

**任务**：写一个最小的「Typst 源码体检器」程序，它做三件事：

1. 接收一段硬编码的 Typst 文本，例如：
   ```typst
   = Fibonacci
   The sequence is $F_n = F_(n-1) + F_(n-2)$.
   #let count = 8
   ```
2. 用 `Source::detached` 解析它，打印：
   - 根节点 `kind`；
   - 全文长度（提示：`source.text().len()`）；
   - 文件总行数（提示：`source.lines()` 提供行索引，可用 `source.lines().count()` 之类的方式查看；具体 API 见 `src/lines.rs`，本讲不展开）。
3. 用同一个 `source.root()`，遍历它的直接子节点（提示：`root.children()`），打印每个直接子节点的 `kind`，观察 Markup 根下挂了哪些构造（标题、文本、公式、`#` 代码等）。

**参考实现骨架**（示例代码，需自行补全）：

```rust
// 示例代码
use typst_syntax::Source;

fn main() {
    let text = "= Fibonacci\nThe sequence is $F_n = F_(n-1) + F_(n-2)$.\n#let count = 8\n";
    let source = Source::detached(text);

    let root = source.root();
    println!("root kind    = {:?}", root.kind());
    println!("text length  = {}", source.text().len());

    println!("direct children kinds:");
    for child in root.children() {
        println!("  - {:?}", child.kind());
    }
}
```

**需要观察的现象**：

- `root kind` 为 `Markup`。
- 直接子节点里能看到 `Heading`（标题）、`Text`（正文）、`Equation`（公式，由 `$...$` 触发）、`Hash`/`LetBinding`（`#let` 代码）等不同 kind。
- 由此直观体会到：`Source` 在 `parse` 之上，额外提供了文件身份、行索引和一个已编号、可查询的语法树入口。

> 行数相关的具体方法名请以 `src/lines.rs` 为准（本讲不展开 Lines 内部，留到 u8-l2）。如果某项 API 名称对不上，标注「待本地验证」并去源码里确认，不要猜。

## 6. 本讲小结

- `typst-syntax` 的 `Cargo.toml` 极简，几乎所有字段都是 `{ workspace = true }`，真正的值集中在仓库根 `Cargo.toml` 的 `[workspace.package]` 与 `[workspace.dependencies]`。
- 关键外部依赖各有分工：`unscanny`（词法游标）、`unicode-*`（字符判定）、`ecow`（紧凑字符串）、`rustc-hash`（快速哈希）、`serde`+`toml`（包清单）、`typst-utils`（`LazyHash`）、`typst-timing`（性能打点）。
- 单独构建/测试本 crate 用 `cargo build -p typst-syntax` / `cargo test -p typst-syntax`，无需编译整个 Typst CLI。
- `parse(text)` 是最底层入口，以 `SyntaxMode::Markup` 解析，返回根 kind 为 `Markup` 的裸 CST（未编号）；`parse_code` / `parse_math` 结构对称，只是模式与根 kind 不同。
- `Source` 把「`FileId` + 文本 + `Lines` + 已编号的语法树」打包成廉价克隆/哈希的不可变值；`Source::new` 是正规构造，`Source::detached` 用假路径 `main.typ` 便于测试，`Source::with_root` 用于复用既有语法树。
- `Source::detached("= Hello").root().kind()` 等于 `SyntaxKind::Markup`——这是本讲最该亲手验证的一条结论。

## 7. 下一步学习建议

下一讲 **u1-l3 源码目录与模块地图** 会把 `lib.rs` 里 `mod` / `pub use` 的全部模块逐一展开，画出从文本到 AST、再到 `Source` 的完整数据流与依赖关系图。建议：

- 先把本讲的「方式 A / 方式 B」实践跑通，确保你有一个能调用 `typst-syntax` 的环境。
- 在阅读 u1-l3 之前，可以自己打开 `src/lib.rs`，尝试用 `pub mod` / `mod` / `pub use` 三类声明把 14 个源码文件分成「对外公开」「内部私有」「选择性导出」三组，带着你的分组结论去对照下一讲。
- 之后再进入进阶层 U2（`SyntaxKind` 与 `SyntaxSet`），那是理解词法和语法解析细节的词汇基础。
