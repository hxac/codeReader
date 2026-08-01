# Source 文件抽象

## 1. 本讲目标

本讲是「文本与 Source 管理」单元（U8）的第一篇。前面几讲我们已经分别学过：词法器把文本切成 token、解析器把 token 组装成 CST（`SyntaxNode`）、`numberize` 给每个节点盖上编号（`Span`）、`Lines` 给纯文本建立行列索引。这些零件一直各自为政。本讲要把它们**总成**为一个对外的统一类型——`Source`。

学完后你应当能够：

- 理解 `Source` 如何把 `FileId` + 文本 + `Lines` + CST 打包成一个**可哈希、廉价克隆的不可变值对象**，以及为什么这样设计。
- 掌握三种构造方式的区别与用途：`new`（正规流程）、`detached`（测试用，假路径）、`with_root`（复用预建好的语法树，服务增量重解析）。
- 学会用 `root / id / text / lines` 做正向访问，用 `find(span)` / `range(num, sub_range)` 做反向定位——把一个 `Span` 编号映射回真实的字节范围。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个概念：

- **值对象（Value Object）**：一旦创建就不再改变的「数据包」。两个值对象相等当且仅当内容相等。`Source` 就是一个值对象：同一个文件解析两次，得到的两份 `Source` 在语义上完全等价。
- **`Arc`（原子引用计数）**：Rust 里让多个所有者**共享**同一块堆数据的智能指针。克隆一个 `Arc` 只是复制一个指针、增加一个计数，开销极小——这就是「廉价克隆」的来源。
- **`LazyHash`**：typst-utils 提供的包装类型，它会**缓存**被包裹对象的哈希值，第一次算完后后续直接复用，避免重复哈希整棵语法树。`Source` 用它来做到「第一次算哈希可能略贵，之后近乎免费」。

回顾两个前面讲义建立的编号不变量（u6-l2、u5-l3），本讲会反复用到：

1. 父节点的 span 编号**小于**任意子节点的编号。
2. 兄弟节点的编号**从左到右递增**。

这两条不变量是 `find` 能快速反查节点的基石。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，但会少量交叉引用其它文件里被 `Source` 调用的方法：

| 文件 | 作用 |
| --- | --- |
| `src/source.rs` | **本讲主角**。定义 `Source` / `SourceInner`，是整个 crate 对外暴露的「文件抽象」门面。 |
| `src/lib.rs` | 通过 `pub use self::source::Source;` 把 `Source` 挂牌到 crate 根。 |
| `src/node.rs` | `LinkedNode::new` / `find` / `find_number` / `range`——`Source::find` 与 `Source::range` 内部委托给它们。 |
| `src/span.rs` | `Span` 的位布局、`id()` / `number()` / `get()`，以及 `SubRange::to_absolute`——`range` 方法靠它把子区间落地为字节范围。 |
| `src/lines.rs` | `Lines::new` / `text()`——注意 `Source` 的文本**就存在 `Lines` 里**，没有第二份。 |
| `src/path.rs` | `RootedPath` / `VirtualRoot` / `FileId`——`Source::detached` 用它们造一个假路径。 |

## 4. 核心概念与源码讲解

### 4.1 Source 与 SourceInner：不可变的值对象

#### 4.1.1 概念说明

到目前为止，我们手上的几样东西是散的：一段文本、一棵 CST、一组行列索引、一个文件身份 `FileId`。下游（求值器、IDE、诊断系统）想要的是一个**完整的、可比较的「源文件」实体**——给它一个 span，它要能告诉你对应哪段文字；要能放进哈希表当 key（增量编译靠它做缓存命中）；还要能被到处传来传去而不担心复制成本。

`Source` 就是这个实体。它被设计成一个**不可变的值对象**：内部用一个 `Arc<LazyHash<SourceInner>>` 持有全部数据，因此：

- **廉价克隆**：`Clone` 只是增加 `Arc` 的引用计数，不复制语法树。
- **可哈希且哈希近乎免费**：`LazyHash` 缓存了 `SourceInner` 的哈希值，放进 `HashMap` / `HashSet` 当 key 非常划算——这正是 Typst 增量编译能用「源文件是否变化」做缓存判断的前提。

文档注释直白地写明了这两点：

```rust
/// Values of this type are cheap to clone and hash.
#[derive(Clone, Hash)]
pub struct Source(Arc<LazyHash<SourceInner>>);
```

[src/source.rs:L16-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L16-L24) — 这是 `Source` 的对外定义，注意它派生了 `Clone` 和 `Hash`。

真正承载数据的是私有结构 `SourceInner`，只有三个字段：

```rust
struct SourceInner {
    id: FileId,
    root: SyntaxNode,
    lines: Lines<String>,
}
```

[src/source.rs:L26-L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L26-L32) — `id`（文件身份）、`root`（CST 根节点）、`lines`（文本与行索引）。

这里有一个关键的**单一真相**设计：`Source` 并没有单独再存一份文本字符串，`text()` 是直接向 `lines` 要的（见 4.3）。文本「唯一真相」住在 `Lines` 里，CST 节点只保存字节长度，二者靠字节范围对齐。这样绝不会出现「文本改了、树没改」的不一致。

#### 4.1.2 核心流程

`Source` 的生命周期可以概括为一条单向流水线：

```text
        ┌──────────┐   parse    ┌──────────┐  numberize  ┌──────────┐
 文本 → │  String  │ ─────────▶ │ 裸 CST   │ ──────────▶ │ 带编号 CST│
        └──────────┘            └──────────┘             └──────────┘
                                                            │
                                       Lines::new(text)     │
                                                            ▼
                                  ┌──────────────────────────────────┐
                                  │ SourceInner { id, root, lines }   │
                                  └──────────────────────────────────┘
                                            │  Arc<LazyHash<..>>
                                            ▼
                                       ┌─────────┐
                                       │ Source  │  （对外门面）
                                       └─────────┘
```

由于内部是 `Arc`，`Source` 本身是 `Copy` 语义上的「共享」——但它没有实现 `Copy`（因为有 `Drop`/引用计数管理），而是实现了廉价的 `Clone`。

#### 4.1.3 源码精读

`Source` 是一个**单字段元组结构体**，唯一字段就是那个 `Arc`：

```rust
pub struct Source(Arc<LazyHash<SourceInner>>);
```

[src/source.rs:L23-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L23-L24) — 单字段、派生 `Clone, Hash`。后续所有方法（`root()`、`text()` 等）都通过 `self.0` 访问内部 `SourceInner`。

`SourceInner` 同样派生 `Clone, Hash`，这使得外层 `Source` 能自动获得哈希能力：

[src/source.rs:L27-L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L27-L32) — 三个字段共同决定一个 `Source` 的「身份」。

最后，`Source` 通过 `lib.rs` 暴露给外部：

[src/lib.rs:L33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L33) — `pub use self::source::Source;`，`source` 模块本身是私有 `mod`，但 `Source` 类型被挂牌到 crate 根。

#### 4.1.4 代码实践

**实践目标**：直观感受 `Source` 的「廉价克隆 + 可哈希」。

**操作步骤**（示例代码，非项目原有代码）：

```rust
// 假设你已能引用 typst_syntax::Source
use typst_syntax::Source;

let s1 = Source::detached("#let x = 1");
let s2 = s1.clone();              // 仅复制一个 Arc 指针
let mut set = std::collections::HashSet::new();
set.insert(s1.clone());
assert!(set.contains(&s2));       // 内容相等 → 哈希相等 → 命中
```

**需要观察的现象**：克隆与插入哈希集都不需要遍历语法树。

**预期结果**：`assert!` 通过。若想看「哈希真的被缓存」，可阅读 typst-utils 里 `LazyHash` 的实现。

**待本地验证**：本片段依赖一个能 `use typst_syntax::Source` 的环境。最稳妥的方式见 4.2.4——直接在本仓库的测试模块里运行，避免外部版本不一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Source` 用 `Arc` 而不是直接把 `SourceInner` 内联？

> **答案**：因为 `Source` 需要被克隆（同一份源文件可能在多个地方被引用，如编译缓存、多个诊断任务）。内联会让每次 `clone` 深拷贝整棵 CST，开销随文件变大而膨胀；`Arc` 让克隆退化为一次指针复制 + 计数自增，与文件大小无关。

**练习 2**：`Source` 没有 `Copy`，只有 `Clone`。如果一个函数按值接收 `Source`，调用方还能继续用原来的变量吗？

> **答案**：能，但需要显式 `.clone()`。因为没有 `Copy`，按值传递会发生所有权转移，调用方原变量会失效；显式 `clone` 后两边各持一个 `Arc`，开销可忽略。

---

### 4.2 三种构造方式：new / detached / with_root

#### 4.2.1 概念说明

`Source` 提供三个构造入口，对应三种使用场景：

- **`new(id, text)`**：正规流程。给它一个真实文件身份和文本，它从头跑「解析 → 编号 → 建行索引」全套流水线，产出一个完整可用的 `Source`。这是绝大多数情况下的入口。
- **`detached(text)`**：测试与临时场景。不要求你提供真实路径，它内部用一个固定的假路径 `main.typ`（项目根下）造一个 `FileId`，然后转调 `new`。写单元测试时极其方便。
- **`with_root(id, text, root)`**：**跳过解析**，直接用一棵「已经造好的」语法树来构造。这服务于增量重解析（U9）：当用户只改了几个字符，没必要重解析全文，复用旧树只替换受影响子树即可——`with_root` 让你能带着这棵「半新」的树重新打包成 `Source`。

#### 4.2.2 核心流程

三种构造的分工用伪代码表示：

```text
new(id, text):
    root = parse(text)                    # 1. 词法 + 语法 → 裸 CST
    root.numberize(id, Span::FULL)        # 2. 给每个节点盖编号（Span）
    lines = Lines::new(text)              # 3. 给纯文本建行列索引
    return Source(Arc(LazyHash({id, root, lines})))

detached(text):
    id = RootedPath::new(Project, "main.typ").intern()   # 假路径 → FileId
    return new(id, text)                                  # 复用正规流程

with_root(id, text, root):
    # 不 parse、不 numberize，假设调用方已备好一棵合法的 root
    lines = Lines::new(text)
    return Source(Arc(LazyHash({id, root, lines})))
```

注意 `Span::FULL` 是编号可用区间的上界常量（`2..2^47`），传给 `numberize` 表示「用整个区间给新树编号」，详见 u6-l2。

#### 4.2.3 源码精读

**`new`**：三步流水线的权威实现。

```rust
pub fn new(id: FileId, text: String) -> Self {
    let _scope = typst_timing::TimingScope::new("create source");
    let mut root = parse(&text);
    root.numberize(id, Span::FULL).unwrap();
    Self(Arc::new(LazyHash::new(SourceInner { id, lines: Lines::new(text), root })))
}
```

[src/source.rs:L35-L41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L35-L41) — `parse` → `numberize` → `Lines::new`，最后打包。`unwrap` 在这里安全：全量编号区间 `Span::FULL` 足够大，正常文本不会编号失败（失败只发生在增量重编号区间过窄时，见 u6-l2）。

`numberize` 的第二个参数 `Span::FULL` 是 crate 内部常量：

[src/span.rs:L86-L87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L86-L87) — `pub(crate) const FULL: Range<u64> = 2..(1 << 47);`，编号 1 留给 detached 哨兵。

**`detached`**：测试利器，造一个假 `FileId` 后转调 `new`。

```rust
pub fn detached(text: impl Into<String>) -> Self {
    Self::new(
        RootedPath::new(VirtualRoot::Project, VirtualPath::new("main.typ").unwrap())
            .intern(),
        text.into(),
    )
}
```

[src/source.rs:L43-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L43-L50) — 路径恒为「项目根下的 `main.typ`」，`intern()` 把它驻留成一个 `FileId`。

涉及的路径类型来自 path.rs：`RootedPath::new` 组合「根 + 虚拟路径」，`intern` 把它换成全局驻留的 `FileId`。

[src/path.rs:L26-L34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L26-L34) — `RootedPath::new` 与 `intern`。`VirtualRoot::Project` 表示「项目根」（对应 `TYPST_ROOT`），见 [src/path.rs:L72-L81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L72-L81)。

**`with_root`**：跳过解析，直接打包——这是增量重解析能高效产出新 `Source` 的关键。

```rust
pub fn with_root(id: FileId, text: String, root: SyntaxNode) -> Self {
    Self(Arc::new(LazyHash::new(SourceInner { id, lines: Lines::new(text), root })))
}
```

[src/source.rs:L52-L55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L52-L55) — 对比 `new` 少了 `parse` 和 `numberize` 两步，前提是调用方传入的 `root` 已经是一棵合法、已编号的树。

> 与编辑的关系：`Source::edit` / `replace`（见 [src/source.rs:L78-L112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L78-L112)）在原地修改文本并触发增量重解析，内部用 `Arc::make_mut` 做写时复制。`with_root` 则是从外部「带着新树」整体替换。两者都是为增量编译服务，深究留待 u9-l1。

#### 4.2.4 代码实践

**实践目标**：在仓库内实际跑通 `detached`，验证三种构造产出的根节点 kind 一致。

**操作步骤**：在本仓库 `crates/typst-syntax` 目录下，运行 crate 自带的测试（无需新建项目）：

```bash
cargo test -p typst-syntax --lib
```

`source.rs` 末尾的 `#[cfg(test)] mod test` 已有一个 `test_source_sub_ranges` 测试（见 [src/source.rs:L162-L182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L162-L182)），它用 `Source::detached` 构造源文件，是「`detached` 用于测试」的最佳范例。

**需要观察的现象**：测试通过，说明 `detached` 产出的 `Source` 完全可用（能 `range`、能取子区间）。

**预期结果**：`test_source_sub_ranges` 通过。

**待本地验证**：具体测试输出以本地 `cargo test` 为准。

#### 4.2.5 小练习与答案

**练习 1**：`detached` 内部转调 `new`，那它和 `new` 的唯一区别是什么？

> **答案**：仅 `FileId` 不同。`detached` 永远用「项目根 / `main.typ`」这个固定假路径驻留出的 id；`new` 由调用方传入任意真实 `FileId`。解析、编号、建行索引的流程完全相同。

**练习 2**：什么场景下必须用 `with_root` 而不能用 `new`？

> **答案**：当你已经持有一棵「想直接复用」的语法树时——典型是增量重解析：用户只改了局部，你重解析了受影响子树并替换进旧树，得到一棵新 `root`。此时若再用 `new` 会把全文重新解析一遍，浪费正是增量重解析要消除的，故用 `with_root` 直接打包这棵半新树。

---

### 4.3 正向访问：root / id / text / lines

#### 4.3.1 概念说明

构造好 `Source` 后，最基本的需求是「正向」取回它的四个组成部分。`Source` 为此提供四个零成本访问器：

- `root()` → `&SyntaxNode`：CST 根节点，遍历语法树的入口。
- `id()` → `FileId`：这份源文件的身份（`Copy` 类型，直接返回值）。
- `text()` → `&str`：完整源文本。
- `lines()` → `&Lines<String>`：行列索引结构，用于 byte↔line↔column↔utf16 换算（详见 u8-l2）。

它们都只是 `&self.0.xxx` 的单行转发，强调 `Source` 是个「透明容器」。

#### 4.3.2 核心流程

四个访问器都遵循同一模式：**借用**内部 `SourceInner` 的对应字段并返回引用（`id` 因为是 `Copy` 直接返回值）：

```text
root()  → &self.0.root      # 借用 CST
id()    →  self.0.id         # Copy，返回值
text()  → self.0.lines.text()  # 注意：向 lines 要文本！
lines() → &self.0.lines      # 借用行索引
```

值得强调的是 `text()`：它返回的是 `self.0.lines.text()`，而不是某个独立字段。这印证了 4.1 的「单一真相」——文本只存在 `Lines` 里。

#### 4.3.3 源码精读

四个访问器逐行对应字段：

```rust
pub fn root(&self) -> &SyntaxNode { &self.0.root }
pub fn id(&self) -> FileId { self.0.id }
pub fn text(&self) -> &str { self.0.lines.text() }
pub fn lines(&self) -> &Lines<String> { &self.0.lines }
```

[src/source.rs:L57-L76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L57-L76) — `root`/`id`/`text`/`lines` 四个正向访问器。

注意 `text()` 委托给 `Lines::text`：

[src/lines.rs:L31-L40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L31-L40) — `Lines::new` 同时存下文本与行元数据，`text()` 返回内部文本的借用。

而 `Lines` 本身也是一个 `Arc<LinesInner>`（[src/lines.rs:L12-L19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L12-L19)），与 `Source` 的「共享 + 廉价克隆」理念一脉相承。

此外 `Source` 还实现了两个 trait 方便使用：

[src/source.rs:L145-L155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L145-L155) — `Debug` 只打印文件路径（不打印整棵树），`AsRef<str>` 让 `&source` 能当字符串切片用。

#### 4.3.4 代码实践

**实践目标**：验证 `text()` 确实来自 `lines`，且与传入文本一致。

**操作步骤**（示例代码）：

```rust
use typst_syntax::Source;

let src = Source::detached("= Hello");
assert_eq!(src.text(), "= Hello");                  // text() 与原文一致
assert_eq!(src.text(), src.lines().text());          // text() 就是 lines().text()
assert_eq!(src.id(), src.id());                      // FileId 是 Copy，可反复取
println!("{:?}", src);                               // Debug 只显示路径 main.typ
```

**需要观察的现象**：`src.text()` 与 `src.lines().text()` 指向同一份字符串。

**预期结果**：两个断言通过；`Debug` 输出形如 `Source(main.typ)`。

**待本地验证**：建议放进 `source.rs` 的测试模块或一个临时 `#[test]` 运行。

#### 4.3.5 小练习与答案

**练习 1**：`SourceInner` 有 `root` 和 `lines` 两个字段，却没有 `text` 字段。如果将来要新增一个 `text` 字段冗余存一份文本，会有什么隐患？

> **答案**：会破坏「单一真相」。编辑文本时（`edit`/`replace`）必须同时维护两份文本，一旦漏改就会出现「`text()` 与 `lines().text()` 不一致」的 bug。当前设计让 `text()` 直接转发到 `lines`，从结构上杜绝了这种不一致。

**练习 2**：`id()` 返回 `FileId` 而非 `&FileId`，为什么可以这样？

> **答案**：因为 `FileId` 内部是 `NonZeroU16`（[src/path.rs:L94-L98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L94-L98)），实现了 `Copy`。返回值比返回引用更省事、无生命周期束缚，且零开销。

---

### 4.4 反向定位：find 与 range

#### 4.4.1 概念说明

前面三组方法是「正向」的：从 `Source` 取它的组成。而诊断系统、IDE 跳转需要的是「反向」：给我一个 `Span`（或一个 `SpanNumber`），告诉我它对应源文件里的哪段文字、哪个节点。

`Source` 提供两个反向入口：

- **`find(span) -> Option<LinkedNode<'_>>`**：把一个 `Span` 反查成带父指针、带绝对偏移的 `LinkedNode`，返回的引用与 `Source` 同生命周期（`'_` 绑定到 `&self`）。
- **`range(num, sub_range) -> Option<Range<usize>>`**：把一个 `SpanNumber`（外加可选的 `SubRange` 子区间）反查成字节范围。这是外部代码把编号换算成字节范围的标准途径（因为 `LinkedNode::find_number` 是 `pub(crate)`，外部不能直接调）。

`find` 和 `range` 共同回答「编号 → 位置」的问题，但层次不同：`find` 给你**节点对象**（能继续遍历子树），`range` 只给你**字节范围**（轻量、够用即可）。

#### 4.4.2 核心流程

**`find` 的流程**：

```text
find(span):
    若 span.id() != 本文件 id  → 返回 None（span 不属于这个文件）
    否则 → LinkedNode::new(root).find(span)
              └─ 据 span.get() 分派：
                 · Detached        → None
                 · Number{num}     → find_number(num)   # 依编号单调性二分剪枝
                 · Range{range}    → find_range(start,end)
```

**`range` 的流程**：

```text
range(num, sub_range):
    node = LinkedNode::new(root).find_number(num)?     # 先按编号定位节点
    overall = node.range()                             # 节点的字节范围
    若有 sub_range:
        range = sub_range.to_absolute(overall.start)   # 子区间相对起点落地
        断言 range.end <= overall.end                  # 不能超出节点范围
        返回 range
    否则:
        返回 overall
```

`find_number` 之所以能高效剪枝，靠的就是 4.1 提到的两条编号不变量。设要找的目标编号为 \( t \)，当前节点编号为 \( n \)：

- 若 \( t < n \)，因为父节点编号小于所有子孙，目标不可能在本子树内，**整块剪掉**。
- 用「下一个兄弟的编号」当作本子树的编号上界：若下一个兄弟编号已大于 \( t \)，才需要进入本子树细找，否则跳过。

这把「在整棵树里线性找编号」降成了接近对数级别的查找。

#### 4.4.3 源码精读

**`find`**：先校验文件归属，再委托 `LinkedNode::find`。

```rust
pub fn find(&self, span: Span) -> Option<LinkedNode<'_>> {
    if span.id() != Some(self.id()) {
        return None;
    }
    LinkedNode::new(self.root()).find(span)
}
```

[src/source.rs:L114-L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L114-L122) — 文件 id 不符直接返回 `None`，避免在错误的树里瞎找。

其中 `span.id()` 解码 `Span` 高 16 位得到 `FileId`：

[src/span.rs:L157-L167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L157-L167) — `id()` 把高 16 位还原成 `Option<FileId>`（detached 时高 16 位为 0，返回 `None`）。

委托目标 `LinkedNode::find` 据 `SpanKind` 分派到 `find_number` / `find_range`：

[src/node.rs:L1115-L1122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1115-L1122) — `find` 是分派枢纽。`find_number` 的剪枝实现在 [src/node.rs:L1124-L1155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1124-L1155)，其中用 `children.peek()` 取下一个兄弟编号当上界、`number < target.0` 做早退。

注意 `find` 返回的 `LinkedNode<'_>` 生命周期绑定到 `&self`，即**借用 `Source`**——所以 `Source::find` 才能把 `self.root()` 的引用安全地交给 `LinkedNode`。

**`range`**：先 `find_number` 定位，再用 `SubRange` 收窄。

```rust
pub fn range(
    &self,
    num: SpanNumber,
    sub_range: Option<SubRange>,
) -> Option<Range<usize>> {
    let overall = LinkedNode::new(self.root()).find_number(num)?.range();
    if let Some(sub_range) = sub_range {
        let range = sub_range.to_absolute(overall.start);
        assert!(range.end <= overall.end);
        Some(range)
    } else {
        Some(overall)
    }
}
```

[src/source.rs:L129-L142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L129-L142) — `find_number(num)?.range()` 拿到节点整体字节范围；`SubRange` 把它收窄到子区间。

其中 `LinkedNode::range()` 由「绝对偏移 + 节点长度」算出字节区间：

[src/node.rs:L1100-L1103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1100-L1103) — `self.offset..self.offset + self.node.len()`。

`SubRange::to_absolute` 把「相对节点起点的偏移」加上 `overall.start` 落地为绝对字节范围：

[src/span.rs:L365-L371](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L365-L371) — `to_absolute(offset)`：`start + offset` 与 `end + offset`。

> `SubRange` 本身存的是相对偏移（[src/span.rs:L333-L372](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L333-L372)），构造时要求 `start < end`（非空）且会对超出 \( 2^{32}-1 \) 的值饱和。这部分细节在 u6-l3 已讲透，本讲只关注它如何被 `Source::range` 使用。

#### 4.4.4 代码实践

**实践目标**：用 `Source::detached` 解析文本，取某个叶子的 `span`，再用 `source.find(span)` 找回 `LinkedNode`，比对文本——这是本讲的核心实践，也是 `test_source_sub_ranges` 测试的简化版。

**操作步骤**（示例代码，可直接放进 `source.rs` 的 `#[cfg(test)] mod test` 运行）：

```rust
use crate::{LinkedNode, Side, Source};

let text = "= head <label>";
let source = Source::detached(text);

// 1) 正向：在偏移 2 处（"= " 之后）用 Side::After 定位到右侧叶子 "head"
let head_span = LinkedNode::new(source.root())
    .leaf_at(2, Side::After)        // 期望落在 Text 节点 "head" 上
    .unwrap()
    .span();

// 2) 反向：用 span 找回 LinkedNode，并比对文本
let found = source.find(head_span).unwrap();
let recovered = &source.text()[found.range()];
assert_eq!(recovered, "head");

// 3) 同一个编号 + 子区间，用 range() 取出 "ea"（"head" 的第 1..3 字符）
let num = crate::SpanNumber(head_span.number());
assert_eq!(&text[source.range(num, Some(crate::SubRange::new(1, 3).unwrap())).unwrap()], "ea");
```

**需要观察的现象**：

1. `leaf_at(2, Side::After)` 命中的叶子，其文本正是 `"head"`。
2. `find(head_span)` 找回的 `LinkedNode` 的 `range()` 对应字节区间 `[2..6)`（即 `"head"`）。
3. `range(num, Some(SubRange(1,3)))` 给出 `"ea"` 对应的区间。

**预期结果**：三条断言全部通过。这与仓库已有测试 `test_source_sub_ranges`（[src/source.rs:L162-L182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L162-L182)）的结论一致。

**待本地验证**：上述 `number()` 是 `pub(crate)`，需在 crate 内部测试模块编译运行；外部代码通常通过 `WorldExt::range`（在 typst-eval 里）间接调用 `Source::range`，不会直接碰 `SpanNumber`。

#### 4.4.5 小练习与答案

**练习 1**：`find` 为什么在委托 `LinkedNode::find` 之前要先检查 `span.id() != Some(self.id())`？

> **答案**：`Span` 的编号只在**单个文件内**唯一，不同文件可能复用相同的编号区间。若不校验文件归属，一个指向文件 B 的 span 可能在文件 A 的树里「碰巧」命中一个编号相同的节点，给出完全错误的结果。校验 id 是保证「跨文件不串味」的必要前置。

**练习 2**：`range` 方法里 `assert!(range.end <= overall.end)`，如果删掉这个断言会有什么风险？

> **答案**：`SubRange` 存的是相对偏移，理论上由调用方保证不越界；但一旦传入错误的 `SubRange`（end 超过节点长度），`to_absolute` 会算出一个超出节点、甚至超出文本的字节范围，下游 `&text()[range]` 会 panic（越界切片）或返回错误文本。这个断言是一道防御性校验，把错误提前暴露在 `Source::range` 内部。

**练习 3**：`find` 返回 `LinkedNode<'_>`，这个 `'_` 绑定到谁？如果我先把 `source` 丢掉，还能继续用返回的 `LinkedNode` 吗？

> **答案**：`'_` 绑定到 `&self`，即借用 `Source`。`LinkedNode` 内部持有 `&SyntaxNode`，而 `SyntaxNode` 存活在 `Source`（的 `Arc`）里，所以 `LinkedNode` 的有效期不超过 `Source` 的借用期。若 `source` 被丢弃，借用失效，编译器会拒绝你继续使用该 `LinkedNode`——这是 Rust 借用检查提供的安全性保证。

## 5. 综合实践

设计一个把本讲四块知识串起来的小任务：**实现一个「span 报告器」**。

给定任意一段 Typst 文本和一个字节偏移，完成：

1. 用 `Source::detached` 构造源文件（练习 4.2 的构造）。
2. 用 `LinkedNode::new(source.root()).leaf_at(offset, Side::After)` 找到光标处的叶子，取它的 `span`（练习 4.3/4.4 的正向遍历）。
3. 用 `source.id()` 打印文件身份；用 `source.find(span).unwrap().range()` 打印该叶子在全文中的字节范围；再从 `source.text()` 切出该范围比对（练习 4.4 的反向定位）。
4. 用 `span.id()` 与 `span.number()` 打印这个 span 的内部编码，验证 `span.id() == Some(source.id())`。

**思考题**：把同一段文本用 `Source::detached` 解析两次得到 `s1`、`s2`，`s1` 里某个叶子的 `span`，能用 `s2.find(span)` 找到吗？为什么？（提示：`detached` 用固定假路径，两份 `FileId` 相等；编号方案 `Span::FULL` 也相同，故编号一致——理论上能找到。）

**待本地验证**：上述结论建议用代码实测确认。

## 6. 本讲小结

- `Source = Arc<LazyHash<SourceInner>>` 是把 `FileId` + 文本 + `Lines` + CST 打包成的**不可变值对象**，廉价克隆、哈希近乎免费，是 Typst 增量编译缓存的基础。
- 三种构造各有分工：`new` 走完整「parse → numberize → 建行索引」流水线；`detached` 用固定假路径 `main.typ` 便于测试；`with_root` 跳过解析、复用预建树，服务增量重解析。
- 文本是**单一真相**：`Source` 不单独存文本，`text()` 转发到 `lines().text()`，从结构上杜绝文本与树的不一致。
- 正向访问 `root / id / text / lines` 都是单行字段转发，强调 `Source` 是透明容器。
- 反向定位有两个入口：`find(span)` 返回带父指针的 `LinkedNode`（先校验文件 id 再委托 `LinkedNode::find`），`range(num, sub_range)` 返回字节范围（`find_number` 定位 + `SubRange::to_absolute` 收窄）。
- `find_number` 的高效剪枝依赖「父编号 < 子编号、兄弟从左到右递增」两条编号不变量，把线性查找降到接近对数级。

## 7. 下一步学习建议

本讲把 `Source` 当作「成品」来用，刻意没展开它内部的两个复杂机制：

- **`Lines` 的行列索引与编码转换**：`text()` 背后的 `Lines` 如何在 byte↔line↔column↔utf16 之间双向换算？请继续本单元的 **u8-l2（Lines 行列与编码转换）**。
- **文本编辑与行重建**：`Source::edit` / `replace` 如何用前缀/后缀 diff 求最小编辑、增量重算行索引？见 **u8-l3（文本编辑与行重建）**。
- **增量重解析**：`with_root` 和 `edit` 都为增量重解析铺路，而 `reparse` 本身的算法见 **u9-l1（增量编译与 reparse 入口）** 与 **u9-l2（try_reparse 核心算法）**。

建议按 u8-l2 → u8-l3 → U9 的顺序学习，把「文本如何变」与「树如何跟着变」补全。
