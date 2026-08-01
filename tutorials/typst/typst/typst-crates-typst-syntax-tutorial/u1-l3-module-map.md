# 源码目录与模块地图

## 1. 本讲目标

本讲是 typst-syntax 的「全局地图」。读完 u1-l1、u1-l2 之后，你已经知道这个 crate 是做什么的、怎么构建它；但你打开 `src/` 目录时，会看到 14 个 `.rs` 文件，一时间不知道该从哪里读起。

本讲要解决的就是这个问题。学完后你应当能够：

1. 说出 `src/` 下每一个源码文件的**一句话职责**，并能按「词法 / 语法 / 数据结构 / 定位 / 文件管理 / 增量 / 输出」分类。
2. 看懂 [`lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1-L50) 里 `pub mod` / `mod` / `pub use` 三层声明各自的作用，回答「哪些类型对外可见、哪些只在 crate 内部用」。
3. 画出从一段源码文本到 `Source` 的**完整数据流**：`文本 → token → CST → AST → Source`。
4. 画一张**模块依赖关系图**，并能解释为什么 `Source` 要依赖 `lines` 与 `reparser` 这两个模块。

本讲只做「地图绘制」，不深入任何一个模块的算法细节——那是后面 U2～U10 各讲的任务。

## 2. 前置知识

本讲假设你已经读过 u1-l1（项目定位）和 u1-l2（构建与运行）。下面几个词在本讲会反复出现，先做个简短回顾：

- **Token（词法单元）**：词法分析器从文本里切出来的最小单位，例如一个关键字 `let`、一个数字字面量 `1`、一段空白。
- **CST（Concrete Syntax Tree，具体语法树）**：保留所有细节（包括空白、注释、错误节点）的语法树，typst-syntax 里用 `SyntaxNode` 表示。它是「无损」的，是工具链（高亮、增量重解析、IDE）的唯一真相来源。
- **AST（Abstract Syntax Tree，抽象语法树）**：在 CST 之上构建的、类型化、按需转换的视图，服务于下游求值。
- **Span**：给语法树里每个节点分配的一个稳定标识，用来把「节点」和「它在源码里的位置 / 它在输出文档里的产物」关联起来。
- **FileId**：把一个文件路径「驻留」（intern）成一个 16 位整数，避免到处复制字符串。
- **SyntaxMode**：三种解析模式 `Markup` / `Math` / `Code`，决定同一串字符被怎么切。

另外，本讲会用到 Rust 的模块可见性概念，复习三个关键词即可：

- `pub mod foo;` —— 把模块 `foo` 整体公开，crate 外能访问 `foo` 里的所有 `pub` 项。
- `mod foo;` —— 模块 `foo` 是**私有**的，只有 crate 内部能访问。
- `pub use foo::Bar;` —— 把 `foo` 模块里的类型 `Bar` **重新导出**到 crate 根，外部可以直接写 `typst_syntax::Bar` 而不用写 `typst_syntax::foo::Bar`。

## 3. 本讲源码地图

本讲只精读两个文件，其余文件靠 [`README.md`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L1-L41) 的官方描述来定位职责。

| 文件 | 作用 |
|------|------|
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1-L50) | crate 门面：声明所有模块，决定可见性，重新导出公共类型。**本讲精读。** |
| [`README.md`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L1-L41) | 用一段话给出每个文件的职责。**本讲精读。** |

其余 12 个模块文件只引用其 `use` 语句与 README 描述，用于绘制依赖图，不在本讲展开算法。

> 一个要先澄清的事实：README 第 27 行把文件身份相关的文件写成 `path.rs, file.rs, package.rs`，但当前仓库里**并不存在 `src/file.rs`**（用 `ls src/*.rs` 可见一共 14 个文件，没有 `file.rs`）。路径驻留与虚拟文件系统的功能现在全部集中在 [`path.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L1-L15) 里。这是 README 文档相对代码稍有滞后的地方，阅读时以 `lib.rs` 的 `mod` 声明为准。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 crate 门面 `lib.rs`，再逐一认领 14 个文件，然后串成数据流，最后画依赖图。

### 4.1 lib.rs：crate 的门面与三层可见性

#### 4.1.1 概念说明

一个 Rust crate 的根文件（`lib.rs`）就像是这栋楼的「大堂指示牌」：它本身通常不写多少业务逻辑，但它决定了三件事：

1. 这栋楼里**有几层、几间房**（`mod` 声明 = 模块树）。
2. 哪些房间**对外开门**（`pub mod`），哪些只供楼内人使用（私有 `mod`）。
3. 哪些人/物**在大堂挂牌**，访客不必上楼就能找到（`pub use` 重新导出）。

typst-syntax 的 `lib.rs` 只有 50 行，但完整地表达了上面三层意思。理解了它，你就掌握了「从外部看这个 crate 长什么样」的全部入口。

#### 4.1.2 核心流程

`lib.rs` 的内容可以分成 4 段：

```text
1. 文档注释        //! Parser and syntax tree for Typst.
2. 模块声明        pub mod / mod           （模块树 + 可见性）
3. 公共重导出      pub use self::xxx::{...} （把内部类型挂牌到 crate 根）
4. 私有内部引用    use self::xxx::{...}     （crate 根自己用的私有别名）
5. 类型定义        pub enum SyntaxMode {..} （定义在根上的枚举）
```

模块声明分两类：

- **公开模块**（`pub mod`）：`ast`、`package`。crate 外可以直接 `use typst_syntax::ast::Markup`。
- **私有模块**（`mod`）：`highlight`、`kind`、`lexer`、`lines`、`node`、`parser`、`path`、`reparser`、`set`、`source`、`span`。一共 11 个。它们不对外暴露模块路径，但其中**被点名重导出的类型**仍然能从外部使用。

于是出现一个关键设计模式：**模块私有，但类型通过 `pub use` 选择性挂牌**。例如 `span` 模块是私有的，但你照样能写 `typst_syntax::Span`，因为 `lib.rs` 里有一行 `pub use self::span::{Span, ...};`。这样做的好处是：对外 API 表面积可控、稳定；内部模块结构可以自由重构而不破坏外部调用方。

#### 4.1.3 源码精读

整段门面就在这里：

[`src/lib.rs:1-17`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1-L17) —— 模块树与可见性：

- 第 3 行 `pub mod ast;` 和第 4 行 `pub mod package;`：两个**对外公开**的模块。
- 第 6–16 行：11 个**私有**模块（`highlight / kind / lexer / lines / node / parser / path / reparser / set / source / span`）。

[`src/lib.rs:18-36`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L18-L36) —— 公共重导出（`pub use`），把私有模块里的类型挂牌到 crate 根，这就是外部能直接用 `typst_syntax::Source`、`typst_syntax::parse`、`typst_syntax::Span` 的原因。

[`src/lib.rs:38-39`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L38-L39) —— 私有内部引用（注意没有 `pub`）：

- `use self::lexer::Lexer;` —— `Lexer` 类型只在本 crate 内部用，不对外暴露。
- `use self::parser::{reparse_block, reparse_markup};` —— 这两个是增量重解析用的内部函数，也不对外暴露。

它们被「私有地」引到 crate 根，是为了让 `reparser` 模块能通过 `crate::reparse_block` 这样的短路径访问（见 4.4）。

[`src/lib.rs:41-50`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L41-L50) —— 直接定义在根上的 `pub enum SyntaxMode`。它定义在 `lib.rs` 而不是某个子模块，是因为它太基础（lexer / parser / kind 都要用），放根上最方便。

#### 4.1.4 代码实践

**实践目标**：亲手验证「模块私有，但类型可挂牌」这条规则。

**操作步骤**：

1. 在仓库内运行（只读查看，不修改源码）：
   ```bash
   # 列出 crate 根上所有 pub 项
   grep -nE '^pub (mod|use|enum|fn|struct)' crates/typst-syntax/src/lib.rs
   ```
2. 对照输出，把结果分成三栏：`pub mod`（公开模块）、`pub use`（挂牌类型）、`pub enum/...`（根上定义）。
3. 思考：`Lexer` 出现在 `use self::lexer::Lexer;`（第 38 行），它**没有** `pub`，说明什么？

**需要观察的现象**：

- `grep` 会列出 2 个 `pub mod`、若干 `pub use`、1 个 `pub enum SyntaxMode`。
- `Lexer` 不在 `pub use` 列表里，因此它是**内部私有**的——外部代码写 `typst_syntax::Lexer` 会编译失败。

**预期结果**：你能从输出里直接读出「对外暴露了哪些名字」，并与本节叙述一致。如果你想在仓库外写一个小程序实际编译验证「`Lexer` 不可访问」，可以参考 u1-l2 的最小项目搭建方式（本讲不重复）。

#### 4.1.5 小练习与答案

**练习 1**：`kind` 模块是私有的（`mod kind;`），但外部却能用 `typst_syntax::SyntaxKind`。为什么？

**答案**：因为 [`lib.rs:19`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L19) 有 `pub use self::kind::SyntaxKind;`。模块路径私有，但类型被「挂牌」重导出到 crate 根，所以外部通过根路径就能访问。

**练习 2**：第 38–39 行的 `use self::lexer::Lexer;` 和 `use self::parser::{reparse_block, reparse_markup};` 为什么不带 `pub`？去掉它们行不行？

**答案**：不带 `pub` 是因为 `Lexer` / `reparse_block` / `reparse_markup` 是内部实现细节，不该对外暴露。去掉它们**不会**导致编译错误直接出现在 `lib.rs`，但会让 `reparser.rs` 里 `crate::parse`、`crate::reparse_markup` 这类短路径解析失败——`reparser` 本身就用到了 `reparse_block` 和 `reparse_markup`（见 4.4）。所以这两行是给 crate 内部其他模块提供「短别名」的桥。

---

### 4.2 14 个源码文件的职责地图

#### 4.2.1 概念说明

typst-syntax 一共 14 个 `.rs` 文件：1 个门面 `lib.rs` + 13 个被它声明的子模块。`README.md` 用一段话给了每个文件的定位。本节把 README 的英文描述翻译成中文，并补充分类，让你建立「文件 → 职责 → 所属层」的映射。

#### 4.2.2 核心流程

把 13 个子模块按在数据流里的角色分成 7 组，更易记忆：

| 分组 | 文件 | 一句话职责（据 README + 源码） |
|------|------|------|
| ① 词法 | `lexer.rs` | 词法基础：把字符串切成 token。 |
| ② 语法 | `parser.rs` | 主解析器：把 token 组装成由 `SyntaxNode` 嵌套构成的 CST。 |
| ② 增量语法 | `reparser.rs` | 增量重解析算法：只重解析必要的最小片段，支撑增量编译。 |
| ③ CST 数据结构 | `node.rs` | CST 的底层数据结构（`SyntaxNode` 及其遍历类型）。 |
| ③ 定位 | `span.rs` | 源码 span 定义：高效地指向某个语法节点（用于诊断等）。 |
| ④ AST | `ast.rs` | CST 与 AST 之间的转换层（CST → 用于求值的 AST）。 |
| ⑤ 词汇 | `kind.rs` | `SyntaxKind` 枚举：所有 token / 节点种类。 |
| ⑤ 词汇 | `set.rs` | `SyntaxKind` 的位集（bit-set）数据结构。 |
| ⑥ 输出 | `highlight.rs` | 从 CST 提取语法高亮信息（可输出为 HTML）。 |
| ⑦ 文件身份 | `path.rs` | 把项目/包路径驻留为唯一 FileId，并在虚拟文件系统中解析（**不负责真正打开文件**）。 |
| ⑦ 包清单 | `package.rs` | `typst.toml` 包清单的解析（与 `path.rs` 配套，被 `path.rs` 引用）。 |
| ⑧ 文件管理 | `source.rs` | `Source`：把 FileId + 文本 + 行索引 + 语法树打包成一个文件抽象。 |
| ⑧ 行索引 | `lines.rs` | （README 未单列）`Source` 内部用来做 byte ↔ line/column ↔ UTF-16 转换的加速结构。 |

门面 `lib.rs` 本身不归入任何组，它是「大堂指示牌」。

两个值得注意的点：

1. README 把文件身份写成 `path.rs, file.rs, package.rs`，但**当前代码里没有 `file.rs`**，相关功能都在 `path.rs`。`lines.rs` 在 README 里没有单独描述，但它真实存在并被 `source.rs` 直接依赖。
2. README 末尾还提到两点重要背景（[`README.md:31-40`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L31-L40)）：解析器结构大量借鉴 **rust-analyzer**；增量重解析算法来自一篇**学位论文**（Section 4）。这两点是后续 U4、U9 讲义的伏笔。

#### 4.2.3 源码精读

[`README.md:12-29`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L12-L29) —— 逐个文件的官方职责说明，是本节这张表的事实来源。

[`README.md:31-40`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/README.md#L31-L40) —— 两个外部参考链接：rust-analyzer 的语法文档、Martin 关于增量编译的学位论文。

要确认「`lines.rs` 真实存在且 `Source` 真的依赖它」，看 [`source.rs:9`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L9) 的 `use crate::lines::Lines;` 和 [`source.rs:31`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L31) 的 `lines: Lines<String>` 字段即可。

#### 4.2.4 代码实践

**实践目标**：用只读命令核对「14 个文件」与 README 描述的对应关系。

**操作步骤**：

```bash
# 1. 列出 src 下所有 .rs 文件，数一数是不是 14 个
ls crates/typst-syntax/src/*.rs | wc -l
ls crates/typst-syntax/src/*.rs

# 2. 看 README 给每个文件的一行描述
sed -n '12,29p' crates/typst-syntax/README.md
```

**需要观察的现象**：

- 第 1 条命令应输出 `14`；列表里应包含 `lib.rs` 和 13 个模块文件，但**没有 `file.rs`**。
- 第 2 条命令输出的 README 描述里，却写着 `path.rs, file.rs, package.rs`。

**预期结果**：你会亲眼看到「README 提到 `file.rs`，但源码里没有」这个文档滞后现象。结论：以 `lib.rs` 的 `mod` 声明为权威，README 的清单可作为语义参考但不能当文件清单用。

#### 4.2.5 小练习与答案

**练习 1**：`highlight.rs` 消费的是 CST 还是 AST？依据是什么？

**答案**：两者都用。[`highlight.rs:1`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L1) 写着 `use crate::{LinkedNode, SyntaxKind, SyntaxNode, ast};`——它以 `SyntaxNode`（CST）为主遍历，同时借用 `ast` 模块做类型判断。README 也说它是「从 CST 提取高亮信息」。

**练习 2**：`package.rs` 被谁依赖？为什么它和 `path.rs` 关系密切？

**答案**：[`path.rs:14`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L14) 有 `use crate::package::PackageSpec;`，即 `path` 依赖 `package`。因为路径驻留要区分「项目路径」和「包路径」，包路径里需要 `PackageSpec`（命名空间/名称/版本）来定位某个包，所以两者配套出现。

---

### 4.3 数据流：从文本到 Source 的完整链路

#### 4.3.1 概念说明

光记住文件名还不够，还要知道它们**在运行时按什么顺序协作**。typst-syntax 的主线是一条单向数据流：拿到一段文本，最终产出一个可查询的 `Source`。理解这条主线，你就能在脑子里给每个文件找到「它在流水线上的工位」。

#### 4.3.2 核心流程

`Source::new` 是这条流水线的总装入口，它内部按顺序做了四步（见 4.3.3）：

```text
            ┌─────────────┐
   text ───▶│  Lexer      │  lexer.rs：文本 → token 流
            └─────┬───────┘
                  │ tokens
                  ▼
            ┌─────────────┐
            │  Parser     │  parser.rs：token → 裸 CST（SyntaxNode）
            └─────┬───────┘
                  │ 一棵尚未编号的 SyntaxNode 树
                  ▼
            ┌─────────────┐
            │ numberize   │  node.rs + span.rs + path.rs：
            └─────┬───────┘  给每个节点分配带 FileId 的 Span
                  │ 已编号的 CST
                  ▼
            ┌─────────────┐
            │  Lines::new │  lines.rs：给文本建 byte↔line↔column 索引
            └─────┬───────┘
                  │
                  ▼
            ┌─────────────┐
            │   Source    │  source.rs：FileId + 文本 + Lines + CST 打包
            └─────────────┘

   旁路视图：ast.rs    从 CST 按需转出类型化 AST（求值用）
   旁路工具：highlight.rs  从 CST 抽取高亮 Tag
   旁路工具：reparser.rs  编辑后只重解析最小片段
```

要点：

1. **Lexer → Parser → numberize → Lines → Source** 是主链路，前一步的产物就是后一步的输入。
2. `ast.rs`、`highlight.rs`、`reparser.rs` 是「旁路」：它们消费已经建好的 CST，不参与首次构建主链路（`reparser` 只在「编辑」时才介入，见 4.4）。
3. **AST 不是单独存的另一棵树**，而是 CST 的惰性视图——需要时才转换。这点 u1-l1 已强调过。

#### 4.3.3 源码精读

`Source::new` 把上面四步压缩在两行里，是整条数据流最浓缩的证据：

[`src/source.rs:36-41`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) —— `Source::new`：

- 第 38 行 `let mut root = parse(&text);`：调用 `parser.rs` 的 `parse`，内部先用 `lexer.rs` 切词，得到一棵**裸 CST**。
- 第 39 行 `root.numberize(id, Span::FULL).unwrap();`：调用 `node.rs` 的 `numberize`，借助 `span.rs` 的 `Span` 与 `path.rs` 的 `FileId`，给每个节点编号。
- 第 40 行 `Lines::new(text)`：建行索引。
- 最后把 `id / lines / root` 装进 `SourceInner`，包成 `Arc<LazyHash<...>>`。

`SourceInner` 的字段就是「打包内容」的最直接体现：

[`src/source.rs:28-32`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L28-L32) —— 三个字段 `id: FileId`、`root: SyntaxNode`、`lines: Lines<String>`，正好对应数据流图最右端的 `Source` 节点。

> 关于 `numberize`：它在 u1-l2 里被提到过「赋编号」，本讲只把它当作「主链路的一步」看待，它的中序遍历算法留到 U6 讲义展开。

#### 4.3.4 代码实践

**实践目标**：在源码里「走完一遍」主链路的四步调用。

**操作步骤**：

```bash
# 1. 定位 parse 的定义（parser.rs 的入口）
grep -nE 'pub fn parse\b|pub fn parse_code|pub fn parse_math' crates/typst-syntax/src/parser.rs

# 2. 定位 numberize 的定义
grep -nE 'fn numberize' crates/typst-syntax/src/node.rs

# 3. 定位 Lines::new
grep -nE 'pub fn new' crates/typst-syntax/src/lines.rs
```

**需要观察的现象**：

- 第 1 条会找到 `parse / parse_code / parse_math` 三个入口（与 [`lib.rs:28`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L28) 的 `pub use` 对应）。
- 第 2 条会找到 `node.rs` 里 `numberize` 的实现。
- 第 3 条会找到 `lines.rs` 的 `Lines::new`。

**预期结果**：你能在 `parser.rs / node.rs / lines.rs` 三个文件里分别找到主链路的四步中的三步实现（`parse` 内部还会再调 `Lexer`），从而在源码层面印证 4.3.2 的流水线图。`Lexer` 是 `parse` 内部的细节，本讲不展开，U3 会专门讲。

#### 4.3.5 小练习与答案

**练习 1**：在 `Source::new` 里，如果跳过 `numberize`（注释掉第 39 行），`Source` 还能正常构造吗？语义上会缺失什么？

**答案**：从类型上看 `Source` 仍可构造（字段都齐了），但 `root` 里的节点**没有有效的 Span 编号**。下游一旦想用 `source.find(span)` 把某个 span 反查回字节范围、或把诊断定位到节点，就会失效或得到错误结果。所以 `numberize` 是「让 CST 可被定位」的关键一步。

**练习 2**：`ast.rs` 在这条主链路的哪一步被调用？

**答案**：主链路里**没有**显式调用 `ast.rs`。AST 是按需从 CST 转换的惰性视图（u1-l1 已说明），只有当下游（求值器）真正调用 `SyntaxNode::cast::<某AST类型>()` 时才会触发。所以它不在 `Source::new` 的流程里，而是 4.3.2 图里的「旁路视图」。

---

### 4.4 模块依赖关系图：Source 为何依赖 lines 与 reparser

#### 4.4.1 概念说明

前三节是「从文件看职责」，本节换成「从 `use` 语句看依赖」。一个模块文件顶部的 `use crate::...` 就是在声明「我需要哪些兄弟模块」。把这些 `use` 汇总起来，就能画出一张真实的模块依赖图。

回答本讲的最后一个核心问题——**为什么 `Source` 要依赖 `lines` 与 `reparser`**——直接看 [`source.rs:9-14`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L9-L14) 即可：

- `use crate::lines::Lines;` —— `Source` 内部存了一个 `Lines<String>` 字段，提供 byte↔line↔column↔UTF-16 查询。没有它，就没法把「字节偏移」翻译成「第几行第几列」，IDE/LSP 也就没法显示光标位置。
- `use crate::reparser::reparse;` —— `Source::edit` / `Source::replace` 在文本被修改时，调用 `reparse` 做**增量重解析**，而不是从头 `parse` 一遍。这是增量编译的性能关键。

换句话说：`lines` 给 `Source` 提供了「位置查询能力」，`reparser` 给 `Source` 提供了「高效更新能力」。两者都是 `Source` 作为「可编辑、可查询的文件抽象」所必需的。

#### 4.4.2 核心流程

下面这张依赖图是依据每个文件顶部的 `use crate::...` 语句**真实汇总**出来的（箭头表示「依赖于」）：

```text
                       lib.rs (根：定义 SyntaxMode，重导出一切)
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
      kind.rs          package.rs          path.rs
   (依赖 SyntaxMode)   (← lexer 的 is_ident) (← package 的 PackageSpec)
        │                                       │
        ▼                                       ▼
      set.rs                                  span.rs
   (← kind)                                (← path 的 FileId)
        │
        │   ┌──────────── node.rs ────────────┐
        │   │  ← kind(ModeAfter), span,       │
        │   │    path(FileId), SyntaxMode     │
        │   └──────────────┬──────────────────┘
        │                  │
        ▼                  ▼
      lexer.rs  ←──── ast(NonDecimalBase)
   (← ast, kind, node)    ast.rs
        │            (← package, span, kind,
        │              node, lexer 的 is_ident/is_newline)
        ▼
     parser.rs
  (← lexer, set, kind, node, ast)
        │
        ▼
     reparser.rs
  (← span, kind, node, lexer 的 is_newline,
     parser 的 parse/reparse_block/reparse_markup)
        │
        │   ┌── lines.rs ──┐    (← lexer 的 is_newline)
        │   └──────────────┘
        ▼                 ▼
     highlight.rs       source.rs ◄── 本讲焦点
  (← node, kind, ast)   (← lines, reparser, span, node,
                          path, parser)
```

读图的三条规律：

1. **越靠下越「上层」**：`source.rs` 几乎依赖所有人，它是把所有零件组装起来的总成。
2. **`kind` / `package` 是叶子**：它们几乎不被兄弟模块拖累（`kind` 只依赖根上的 `SyntaxMode`；`package` 只依赖 `lexer` 的一个字符判定函数），是最适合先读的底层。
3. **存在「反向」依赖**：`lexer` 依赖 `ast`（`NonDecimalBase`），`ast` 又依赖 `lexer` 的 `is_ident`。这不是循环依赖，因为它们引用的是对方「被 `pub use` 挂牌到 crate 根的符号」，Rust 允许模块间这种互相引用。

#### 4.4.3 源码精读

**为什么 `Source` 依赖 `lines` 与 `reparser`**——直接看它的 import：

[`src/source.rs:9-14`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L9-L14)：

- 第 9 行 `use crate::lines::Lines;` —— 位置查询能力。
- 第 10 行 `use crate::reparser::reparse;` —— 增量更新能力。
- 第 11–14 行还依赖 `span`（`Span/SpanNumber/SubRange`）、`node`（`LinkedNode/SyntaxNode`）、`path`（`FileId/RootedPath/...`）、`parser`（`parse`）。

`Lines` 字段确实出现在 `SourceInner` 里：[`src/source.rs:31`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L31) 的 `lines: Lines<String>`。

`reparse` 在哪里被 `Source` 调用？在 `Source::edit` 里（编辑文本时触发增量重解析）：

[`src/source.rs:78-96`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L78-L96) —— `Source::replace` 的文档注释明确写着「Returns the range in the new source that was ultimately reparsed」，它内部最终会走到 `edit`，而 `edit` 依赖 `reparser::reparse` 做最小重解析。

`reparser` 自己又依赖了谁——[`src/reparser.rs:3-5`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L3-L5)：`Span, SyntaxKind, SyntaxNode, is_newline, parse, reparse_block, reparse_markup`。注意它用到 `reparse_block` 和 `reparse_markup`，这正是 [`lib.rs:39`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L38-L39) 那行私有 `use` 把它们引到 crate 根的原因——方便 `reparser` 用短路径 `crate::reparse_markup` 访问。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手从源码里「挖」出模块依赖关系，画出一张标注了 pub/私有的依赖图，并解释 `Source` 依赖 `lines` 与 `reparser` 的原因。

**操作步骤**：

1. 提取所有文件顶部的 `crate::` 依赖：
   ```bash
   grep -nE '^use crate' crates/typst-syntax/src/*.rs
   ```
2. 把输出整理成「文件 → 依赖了哪些兄弟模块」的清单。提示：`crate::Span`、`crate::SyntaxKind` 这类经过 `pub use` 挂牌的类型，要能反查出它们来自哪个模块（`Span`→`span`、`SyntaxKind`→`kind`、`is_newline`/`is_ident`→`lexer`、`FileId`→`path`、`parse`/`reparse_markup`→`parser`）。
3. 在图上用两种颜色/记号区分：
   - **公开模块**：`ast`、`package`（来自 `pub mod`）。
   - **私有模块**：其余 11 个（来自 `mod`）。
4. 重点圈出 `source.rs` 的依赖边，回答下面两个问题。

**需要观察的现象**：

- `source.rs` 的 `use crate::...` 会包含 `lines::Lines` 和 `reparser::reparse`，证明它依赖这两个模块。
- `reparser.rs` 的 `use crate::...` 里会出现 `parse, reparse_block, reparse_markup`，说明增量重解析复用了 parser 的能力。
- 整张图里 `kind`、`package`、`set` 等几乎没有「重依赖」，是底层叶子。

**预期结果**：你得到一张与本节 4.4.2 一致的依赖图；并能用自己的话解释：

- **`Source` 为什么依赖 `lines`？** 因为 `Source` 要对外提供「字节偏移 ↔ 行/列/UTF-16」的查询（`source.lines()`），这些查询由 `Lines` 结构承担；编辑文本后也要靠 `Lines` 增量重建行索引。
- **`Source` 为什么依赖 `reparser`？** 因为 `Source::edit` / `replace` 在文本变更时调用 `reparser::reparse` 做**最小范围的增量重解析**，避免每次都全量 `parse`，这是增量编译的性能基础。

> 若想进一步验证，可在仓库内执行 `cargo build -p typst-syntax` 确认依赖关系能通过编译（仅验证，不修改源码）。增量重解析的实际行为（返回「真正重解析的范围」）留到 U9 讲义用 `Source::edit` 动手观察。

#### 4.4.5 小练习与答案

**练习 1**：`reparser.rs` 为什么需要 `parse`、`reparse_block`、`reparse_markup` 这三个来自 `parser` 的函数？

**答案**：增量重解析不是另起炉灶，而是**复用 parser 的能力**对「受影响的局部文本」重新解析。当增量算法判定某段可以局部重解析时，就调用 `reparse_markup`（markup 序列）或 `reparse_block`（单个块）重新生成子树；如果局部重解析失败，则回退到全量 `parse`。所以它必须依赖 `parser`。

**练习 2**：`lines.rs` 依赖 `lexer.rs` 的什么？为什么？

**答案**：[`lines.rs:6`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L6) 是 `use crate::is_newline;`。`Lines` 要把文本按行切分，必须知道哪些字符算「换行」，而换行判定函数 `is_newline` 正是 `lexer.rs` 提供并经 `lib.rs` 挂牌导出的公共工具。两者共用同一套 Unicode 换行定义，保证词法与行索引的换行判定一致。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这张「模块地图作业」。

**任务**：为 typst-syntax 制作一份**一页速查表（cheat sheet）**，要求包含以下四块内容，全部基于真实源码，不得臆造。

1. **文件清单**：列出 `src/` 下全部 14 个文件，每个配一句话职责（中文），并标注它属于 4.2.2 表里的哪一组（词法 / 语法 / CST / 定位 / AST / 词汇 / 输出 / 文件身份 / 文件管理）。
2. **可见性表**：用 `grep -nE '^pub mod|^mod' crates/typst-syntax/src/lib.rs` 的输出，列出哪些模块是 `pub`、哪些是私有；再列出 3 个「模块私有但类型被 `pub use` 挂牌」的例子（如 `SyntaxKind`、`Span`、`Source`）。
3. **数据流图**：临摹 4.3.2 的流水线，但在每一步旁边**标上对应的源码文件名和关键函数**（`parse` / `numberize` / `Lines::new` / `SourceInner`）。
4. **依赖图 + 焦点解释**：临摹 4.4.2 的依赖图，用记号标出 `source.rs` 的所有依赖边；并在图下方用两三句话写清「`Source` 为什么依赖 `lines` 与 `reparser`」。

**验收标准**：

- 速查表里出现的每一个文件名，都能用 `ls crates/typst-syntax/src/*.rs` 找到。
- 速查表里出现的每一处「某模块依赖某模块」，都能用 `grep -nE '^use crate' crates/typst-syntax/src/<文件>.rs` 复核。
- 对「`Source` 依赖 `lines` 与 `reparser`」的解释，必须同时提到「位置查询」和「增量重解析」两个理由。

完成这份速查表后，你就拥有了一张可以贴在墙上、随时回看 typst-syntax 全景的地图，后续阅读任何一篇讲义都不会迷路。

## 6. 本讲小结

- typst-syntax 的 `src/` 一共 **14 个 `.rs` 文件**：1 个门面 `lib.rs` + 13 个子模块；README 提到的 `file.rs` 在当前代码里**不存在**，其功能并入 `path.rs`。
- [`lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L1-L50) 用三层声明管理可见性：`pub mod`（公开 `ast`、`package`）、私有 `mod`（其余 11 个）、`pub use`（把私有模块里的类型挂牌到 crate 根，如 `Source`、`Span`、`SyntaxKind`）。
- 主数据流是 **`文本 → Lexer → Parser(裸CST) → numberize(带Span的CST) → Lines → Source`**，浓缩在 [`Source::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) 里；`ast` / `highlight` / `reparser` 是消费 CST 的旁路。
- 模块依赖图由各文件顶部的 `use crate::...` 决定；`source.rs` 是「总成」，几乎依赖所有人。
- **`Source` 依赖 `lines`** 是为了提供 byte↔line↔column↔UTF-16 位置查询；**依赖 `reparser`** 是为了在文本编辑时做最小范围的增量重解析。两者共同让 `Source` 成为「可查询、可高效更新」的文件抽象。
- README 点名两个外部参考：解析器借鉴 **rust-analyzer**，增量重解析来自一篇**学位论文**——这是后续 U4、U9 讲义的线索。

## 7. 下一步学习建议

本讲只是「看地图」，接下来要开始「逐层下钻」：

1. **先掌握贯穿全 crate 的词汇表**：进入 U2，精读 [`kind.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) 的 `SyntaxKind` 枚举与 [`set.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs) 的 `SyntaxSet` 位集——它们是 lexer、parser、highlight 共用的「公共语言」。
2. **如果你想先看一次端到端的运行效果**：可以跳到 u1-l4，用 `Source::detached` 实际解析一段文本并遍历 CST，把本讲的数据流图「跑」一遍。
3. **如果你对工程入口感兴趣**：复习 u1-l2 的 `parse` / `Source::detached`，那是把本讲地图「用起来」的最短路径。
4. 暂时**不要**深入 `node.rs` 的 `numberize`、`span.rs` 的位编码、`reparser.rs` 的算法——它们分别留给 U6、U9 专门讲义，现在只需知道它们在依赖图里的位置即可。
