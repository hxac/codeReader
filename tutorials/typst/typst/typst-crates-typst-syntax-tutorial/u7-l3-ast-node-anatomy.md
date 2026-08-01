# 典型 AST 节点剖析与扩展

## 1. 本讲目标

上一讲（u7-l2）我们看清了 AST 的入口：`Markup` 是唯一的根，`Expr` 枚举是「图中的边」，把 markup/math/code 三类表达式分叉到几十种具名节点。本讲要回答的是一个更落地的问题：**这些具名节点内部长什么样？它们的方法是怎么从 CST 里把语义「抠」出来的？**

读完本讲，你应当能够：

1. 读懂任意一个 AST 节点的 `impl` 块，说出它假设的 CST 子节点顺序。
2. 掌握 `node!` 宏生成节点的两步扩展模式：先 `node!` 声明外壳，再 `impl` 写访问方法。
3. 区分 AST 取子节点的三类范式：`leaf_text`（叶子）、`cast_first/cast_last`（取一个）、`children().filter_map(cast)`（取全部）。
4. 理解当 CST 结构损坏时，`placeholder()` 如何兜底而绝不 panic。
5. 在新增一个语法构造时，列出 kind.rs / parser.rs / ast.rs / highlight.rs / set.rs 需要联动的改动点。

## 2. 前置知识

本讲假设你已经学完 u7-l1（AST 是 CST 的类型化视图）和 u7-l2（Markup 根与 Expr 枚举）。下面三个结论会反复用到：

- **AST 节点是透明包装**：宏展开后形如 `struct Raw<'a>(&'a SyntaxNode);`，不复制树，零成本。
- **类型即守门员**：只有 `AstNode::from_untyped` 校验 kind 成功，才能构造某节点，进而调用它的语义方法——这是「我们知道它底下一定有期望的子结构」的根本保证。
- **AST 方法依赖 parser 产出的固定子结构**：解析器在构建 CST 时就「安排」好了子节点顺序，AST 只是按这个约定去取。本讲的核心就是看清这个约定。

如果你对 CST 的 `SyntaxNode`（Leaf/Inner/Error/Warning 四种形态）、`leaf_text()`、`children()` 还不熟悉，建议先复习 u5-l1 与 u5-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ast.rs` | 本讲主战场：`AstNode` trait、`node!` 宏、以及 `Text`/`Heading`/`Raw`/`LetBinding`/`Closure` 等节点的 `impl`。文件顶部还有一段权威的模块文档，正是用 `Raw` 当贯穿示例。 |
| `src/kind.rs` | `SyntaxKind` 枚举定义。AST 节点名与 kind 变体名一一对应（如 `Raw` 节点 ↔ `SyntaxKind::Raw`），这是 `node!` 宏能成立的基础。 |
| `src/parser.rs` | 生成 CST 的地方。我们要对照 `heading()`、`let_binding()` 看 parser 给 AST 预备了怎样的子节点顺序。 |
| `src/node.rs` | 提供 `SyntaxNode::placeholder()`、`leaf_text()`、`children()` 等基础设施。 |

## 4. 核心概念与源码讲解

### 4.1 node! 宏：AST 节点是如何诞生的

#### 4.1.1 概念说明

绝大多数 AST 节点（`Text`、`Heading`、`Raw`、`LetBinding`……）的「外壳」都是一样的：一个包着 `&'a SyntaxNode` 的新类型结构体，外加一段几乎照抄的 `AstNode` 实现。如果手写，几十个节点会产生大量重复样板。`node!` 宏就是为了消除这份重复：**只需声明「结构体名等于某个 `SyntaxKind` 变体名」，宏就自动生成结构体与 `AstNode` 实现**。

这个命名约定（`struct Raw` ↔ `SyntaxKind::Raw`）是宏的灵魂——`from_untyped` 里只需写 `node.kind() == SyntaxKind::$name` 一行，就能完成所有同名节点的类型守门。

#### 4.1.2 核心流程

`node!(struct Foo)` 展开后做三件事：

1. **生成透明结构体**：`#[repr(transparent)] pub struct Foo<'a>(&'a SyntaxNode);`，并派生 `Debug/Copy/Clone/Eq/PartialEq/Hash`。`Copy` 是因为内部只是一个指针，复制廉价。
2. **实现 `AstNode`**：
   - `from_untyped`：比对 `node.kind() == SyntaxKind::Foo`，匹配返回 `Some(Self(node))`，否则 `None`——这就是「守门员」。
   - `to_untyped`：交还内部的 `&SyntaxNode`。
   - `placeholder`：用一个 `static` 的占位节点构造同类型假节点。
3. 之后开发者用独立的 `impl Foo<'a> { ... }` 块写真正的语义方法。

#### 4.1.3 源码精读

先看 `AstNode` trait 定义的契约（[src/ast.rs:100-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L100-L117)），三个必须方法 `from_untyped` / `to_untyped` / `placeholder` 加一个默认方法 `span()`。

`SyntaxNode` 上的便捷转换入口都委托给 trait（[src/ast.rs:120-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L120-L149)）。注意四个私有助手：`try_cast_first/try_cast_last` 返回 `Option`，而 `cast_first/cast_last` 在找不到时回退到 `T::placeholder()`——这是「绝不 panic」的关键。

`node!` 宏本体（[src/ast.rs:152-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L152-L196)）。关键就是这一行守门：

```rust
fn from_untyped(node: &'a SyntaxNode) -> Option<Self> {
    if node.kind() == SyntaxKind::$name {
        Some(Self(node))
    } else {
        Option::None
    }
}
```

以及 `placeholder()` 生成的静态假节点：

```rust
fn placeholder() -> Self {
    static PLACEHOLDER: SyntaxNode
        = SyntaxNode::placeholder(SyntaxKind::$name);
    Self(&PLACEHOLDER)
}
```

`SyntaxNode::placeholder` 的实现见 [src/node.rs:200-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L200-L213)：造一个文本为空、span 为 detached、kind 为指定值的叶子节点（且禁止用 `Error` kind 造占位）。

ast.rs 顶部的模块文档（[src/ast.rs:1-78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1-L78)）正是用 `Raw` 作为贯穿示例讲解这套机制的，非常值得一读。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `cast` 的「同名 kind 守门」行为。

**操作步骤**（示例代码，需本地验证）：

```rust
// 示例代码：放在仓库外的小项目，或 cargo test -p typst-syntax 的临时用例里
use typst_syntax::{Source, SyntaxKind};
use typst_syntax::ast::{AstNode, Text, Heading};

let src = Source::detached("= Hello");
let root = src.root();

// 在 root 的直接子节点里找
for child in root.children() {
    // 同名守门：Text 节点 cast 成 Text 成功，Heading cast 成 Text 失败
    println!("kind={:?}  cast::<Text>={}", child.kind(), child.cast::<Text>().is_some());
    println!("kind={:?}  cast::<Heading>={}", child.kind(), child.cast::<Heading>().is_some());
}
```

**需要观察的现象**：`=` 引导出的节点 kind 是 `Heading`，所以 `cast::<Text>()` 返回 `None`、`cast::<Heading>()` 返回 `Some`；正文 `Hello` 的 kind 是 `Text`，结果相反。

**预期结果**：同名 kind 匹配才返回 `Some`。若运行环境未就绪，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `node!` 生成的结构体要加 `#[repr(transparent)]`？
**答案**：因为它只包了一个 `&'a SyntaxNode`，`transparent` 保证 ABI 与裸指针一致，便于 `unsafe` 场景与未来可能的类型转换，也向编译器表明它没有额外字段。

**练习 2**：如果有个节点的 `from_untyped` 不能只靠「同名 kind」判断（例如 `Expr`、`Pattern` 这种聚合枚举），还能用 `node!` 宏吗？
**答案**：不能。`node!` 只适用于「结构体名 == 单一 kind 变体」的简单节点。`Expr`、`Pattern` 是枚举，需要手写 `AstNode` 的 `from_untyped`，用一个大型 `match node.kind()` 分派到各变体（见 [src/ast.rs:386-402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L386-L402)）。

---

### 4.2 取子节点的三类范式：从 Text 到 Heading

#### 4.2.1 概念说明

`node!` 只生成「外壳」。节点真正的语义藏在它的 `impl` 方法里，而这些方法本质上都在做一件事：**按 parser 预订的子节点顺序，从底层 CST 里把需要的东西取出来**。纵观 ast.rs，取子节点只有三类范式：

| 范式 | 典型方法 | 适用场景 |
| --- | --- | --- |
| 读自身文本 | `self.0.leaf_text()` | 叶子节点（Text、Link、RawLang） |
| 取一个指定类型的子节点 | `cast_first` / `cast_last` / `try_cast_first` | 有唯一 body 的结构节点（Strong、Heading） |
| 取全部/按条件筛选 | `children().filter_map(cast)` / `children().find(...)` | 有多个同类子节点（Raw 的行）或需按 kind 定位（Heading 的 marker） |

理解了这三类范式，你就能读懂 ast.rs 里几乎任何一个节点。

#### 4.2.2 核心流程

- **叶子范式**：节点本身就是一个 token，没有子节点，语义就是它的文本。用 `leaf_text()` 取出后做轻量后处理（去前缀、去引号等）。
- **取一个范式**：parser 保证某类型子节点唯一存在，用 `cast_first()` 直接取（找不到自动回退 `placeholder`），或用 `try_cast_first()` 取 `Option`。
- **筛选范式**：子节点数量不定，或与其它类型混排，用 `children()` 迭代配合 `filter_map(SyntaxNode::cast)`（留指定类型）或 `find(|n| n.kind() == ...)`（按 kind 定位）。

#### 4.2.3 源码精读

**叶子范式**——`Text`（[src/ast.rs:581-591](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L581-L591)）：

```rust
impl<'a> Text<'a> {
    pub fn get(self) -> &'a EcoString {
        self.0.leaf_text()
    }
}
```

文本即语义，直接返回。再看稍复杂的叶子 `LineComment`（[src/ast.rs:198-209](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L198-L209)），取文本后 `strip_prefix("//")` 去掉注释符号。

**取一个范式**——`Strong`（[src/ast.rs:670-680](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L670-L680)）：`pub fn body(self) -> Markup { self.0.cast_first() }`。`cast_first` 会跳过所有不能 cast 成 `Markup` 的子节点（如 `*` 定界符），找到唯一的正文。

**筛选范式**——`Heading`（[src/ast.rs:792-811](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L792-L811)）同时演示了「取一个」和「按 kind 定位」：

```rust
impl<'a> Heading<'a> {
    pub fn body(self) -> Markup<'a> {
        self.0.cast_first()           // 取一个 Markup 子节点（正文）
    }
    pub fn depth(self) -> NonZeroUsize {
        self.0.children()
            .find(|node| node.kind() == SyntaxKind::HeadingMarker)  // 找 marker
            .and_then(|node| node.len().try_into().ok())            // marker 长度 = 等号数
            .unwrap_or(NonZeroUsize::ONE)                            // 兜底
    }
}
```

对应的 `SyntaxKind` 见 [src/kind.rs:59-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L59-L62)：`Heading` 是结构节点，`HeadingMarker` 是 `=`/`==` 引导 token。

#### 4.2.4 代码实践

**实践目标**：写出 `Heading` 期望的 CST 子节点顺序，并解释结构不符时的 placeholder 兜底。

**操作步骤**：

1. 读 parser 的 `heading()`（[src/parser.rs:171-178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178)）：

   ```rust
   fn heading(p: &mut Parser) {
       p.with_nl_mode(AtNewline::Stop, |p| {
           let m = p.marker();
           p.assert(SyntaxKind::HeadingMarker);   // 子节点①：HeadingMarker
           markup(p, false, false, ...);           // 子节点②：Markup（正文）
           p.wrap(m, SyntaxKind::Heading);
       });
   }
   ```

2. 由此写出 Heading 的期望子节点顺序：`[HeadingMarker, Markup]`。
3. 对照 `Heading::body()`（`cast_first` 取 Markup）与 `Heading::depth()`（`find` 取 HeadingMarker）验证二者刚好对上。

**需要观察的现象**：`body()` 用 `cast_first` 而非 `try_cast_first`——若 CST 损坏导致没有 Markup 子节点，`cast_first` 不会 panic，而是返回 `Markup::placeholder()`（一个空的静态 Markup 节点）。

**预期结果**：即便对一个被错误编辑、丢失正文的 Heading 节点调用 `body()`，也只得到空 Markup，下游求值安全跳过。

#### 4.2.5 小练习与答案

**练习 1**：`Heading::body()` 用 `cast_first()`，而 `Heading::depth()` 用 `children().find(...)`。为什么 depth 不也用 cast？
**答案**：因为 `HeadingMarker` 没有对应的 AST 节点类型（它是纯 token，无需语义方法），无法 `cast::<HeadingMarker>`。所以只能按 `kind()` 定位。

**练习 2**：假如 parser 的 bug 导致一个 Heading 节点下根本没有 `HeadingMarker` 子节点，`depth()` 会返回什么？
**答案**：`find` 返回 `None`，链式 `and_then`/`unwrap_or` 落到 `NonZeroUsize::ONE`，即深度 1。这正是 placeholder/兜底思想：绝不 panic，给一个中性默认值。

---

### 4.3 Raw 节点深度剖析：lines / lang / block

#### 4.3.1 概念说明

`Raw` 是 ast.rs 模块文档钦定的「贯穿示例」，也是最能体现「AST 抽象掉 CST 细节」的节点。一个 raw 文本 `` ```typ\nprint(1)\n``` `` 在 CST 里是一堆 `RawDelim`（反引号定界符）、可选 `RawLang`（语言标签）、若干 `Text`（正文）和 `RawTrimmed`（被裁剪的空白）混排；而 AST 的 `Raw` 只暴露三个语义方法：

- `lines()`：正文按行迭代。
- `lang()`：可选的语言标签。
- `block()`：是块级还是行内。

定界符、裁剪空白这些 CST 细节被抽象掉了，只在判定 block 时被「偷看」一眼。

回顾 u3-l3：raw 几乎完全在**词法阶段**就组装成完整 CST 子树，parser 只是 `eat` 掉。所以 Raw 的子结构是 lexer 决定的。

#### 4.3.2 核心流程

CST 中一个 `Raw` 节点的子节点排列（块级带语言标签时）：

```
Raw
├─ RawDelim    (开头的 ``` )
├─ RawLang     (typ，仅块级才有)
├─ RawTrimmed  (被裁掉的首行换行/缩进)
├─ Text        (正文一行)
├─ ...
├─ RawTrimmed  (被裁掉的末行缩进/换行)
└─ RawDelim    (结尾的 ``` )
```

行内 raw `` `x` `` 则：开头 `RawDelim` 长度为 1，没有 `RawLang`。三个方法分别从中取所需：

- `lines()`：遍历所有子节点，过滤出 `Text`（用 `cast`）。
- `lang()`：先取首个 `RawDelim`，若其长度 ≥3（块级）才继续找 `RawLang`。
- `block()`：首个 `RawDelim` 长度 ≥3 **且** 存在含换行的 `RawTrimmed`（即正文跨行）。

#### 4.3.3 源码精读

`Raw` 的完整实现（[src/ast.rs:694-726](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L694-L726)）：

```rust
impl<'a> Raw<'a> {
    pub fn lines(self) -> impl DoubleEndedIterator<Item = Text<'a>> {
        self.0.children().filter_map(SyntaxNode::cast)  // 筛选范式：留 Text
    }

    pub fn lang(self) -> Option<RawLang<'a>> {
        let delim: RawDelim = self.0.try_cast_first()?;  // 取首个 RawDelim
        if delim.0.len() < 3 { return Option::None; }    // 行内无语言标签
        self.0.try_cast_first()                            // 再取首个 RawLang
    }

    pub fn block(self) -> bool {
        self.0.try_cast_first()
            .is_some_and(|delim: RawDelim| delim.0.len() >= 3)         // 块级定界符
            && self.0.children().any(|e| {                              // 且有跨行
                e.kind() == SyntaxKind::RawTrimmed
                    && e.leaf_text().chars().any(is_newline)
            })
    }
}
```

配套的叶子节点 `RawLang`（[src/ast.rs:728-738](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L728-L738)）和 `RawDelim`（[src/ast.rs:740-743](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L740-L743)）。注意 `RawLang::get()` 也是叶子范式：`self.0.leaf_text()`。

`lang()` 有一个微妙之处：它对 `RawDelim` 和 `RawLang` 都用了 `try_cast_first`。这两次调用看似都取「第一个」，但它们找的是**不同类型**——第一次跳过非 `RawDelim` 找定界符，第二次跳过 `RawDelim` 找语言标签，互不干扰。这正是「按类型 cast」的好处。

相关 kind 定义见 [src/kind.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L43-L50)（`Raw`/`RawLang`/`RawDelim`/`RawTrimmed`）。

#### 4.3.4 代码实践

**实践目标**：用 `lines()`/`lang()`/`block()` 对照行内与块级 raw 的差异。

**操作步骤**（示例代码，待本地验证）：

```rust
// 示例代码
use typst_syntax::{Source, SyntaxKind};
use typst_syntax::ast::{AstNode, Raw};

fn find_raw(node: &typst_syntax::SyntaxNode, out: &mut Vec<Raw>) {
    if let Some(r) = node.cast::<Raw>() { out.push(r); }
    for c in node.children() { find_raw(c, out); }
}

let src = Source::detached("`x` and ```typ\nprint(1)\n```");
let mut raws = Vec::new();
find_raw(src.root(), &mut raws);

for r in raws {
    println!("block={} lang={:?} lines={:?}",
        r.block(),
        r.lang().map(|l| l.get().as_str()),
        r.lines().map(|t| t.get().as_str()).collect::<Vec<_>>());
}
```

**需要观察的现象**：第一个 raw 是行内（`block=false`、`lang=None`、一行 `x`）；第二个是块级（`block=true`、`lang=Some("typ")`、正文按行返回，首尾的换行与缩进被 `RawTrimmed` 吃掉，不出现在 `lines()` 里）。

**预期结果**：`lines()` 只产出 `Text` 子节点，定界符与裁剪空白被正确抽象掉。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `block()` 不能只看 `RawDelim` 长度 ≥3，还要额外检查「存在含换行的 `RawTrimmed`」？
**答案**：因为 `` ```单行``` `` 用了三个反引号但没有跨行，Typst 视其为行内 raw。只有定界符 ≥3 **且** 内容真正跨行才算块级。这条规则源自 u3-l3 讲的 lexer 的 `blocky_raw` 判定。

**练习 2**：`Raw::lines()` 返回的迭代器元素类型是 `Text<'a>` 而非 `&'a str`，这样设计有什么好处？
**答案**：`Text<'a>` 仍是 AST 节点，调用方还能进一步拿到它的 `span()`、`to_untyped()` 等信息，便于诊断与 IDE 跳转；若直接返回 `&str` 就丢失了定位。

---

### 4.4 LetBinding：多分支与「复用同一子节点」技巧

#### 4.4.1 概念说明

`Heading`/`Strong` 这类节点结构简单，取子节点一目了然。`LetBinding` 则展示了 AST 方法在结构复杂、形态多样时如何巧妙地复用 CST。Typst 的 `let` 有两种语法：

- 普通：`let x = 1`、`let _ = 1`、`let (x, y) = (1, 2)`
- 闭包语法糖：`let f(x) = body`，等价于 `let f = (x) => body`

`LetBinding::kind()` 要区分这两种，`LetBinding::init()` 要在每种形态下都正确取出初始化表达式。难点在于：不同形态下「init 是第几个子节点」并不固定，AST 用了一个巧妙的「复用」技巧来应对。

#### 4.4.2 核心流程

先看 parser 的 `let_binding()`（[src/parser.rs:789-817](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L789-L817)）产出的子结构：

- 总是先有 `Let` 关键字 token。
- 若是 `let 标识符(...)`：把 `标识符(参数)` 整体包成一个 `Closure` 子节点（闭包语法糖）。
- 否则：解析一个 `Pattern`（可能是裸 `Ident`、`Underscore`、`Parenthesized`、`Destructuring`）。
- 之后可选 `=` 与初始化表达式。

于是 `kind()` 用「第一个能 cast 成 `Pattern` 的子节点」来分派；`init()` 则分三种情况取初始化表达式。

#### 4.4.3 源码精读

`kind()` 与 `init()` 的实现（[src/ast.rs:2308-2329](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L2308-L2329)）：

```rust
impl<'a> LetBinding<'a> {
    pub fn kind(self) -> LetBindingKind<'a> {
        match self.0.cast_first() {                          // 第一个 Pattern 子节点
            Pattern::Normal(Expr::Closure(closure)) => {     // 是闭包 → 语法糖形态
                LetBindingKind::Closure(closure.name().unwrap_or_else(Ident::placeholder))
            }
            pattern => LetBindingKind::Normal(pattern),
        }
    }

    pub fn init(self) -> Option<Expr<'a>> {
        match self.kind() {
            LetBindingKind::Normal(Pattern::Normal(_) | Pattern::Parenthesized(_)) => {
                self.0.children().filter_map(SyntaxNode::cast).nth(1)  // init 是第 2 个 Expr 子节点
            }
            LetBindingKind::Normal(_) => self.0.try_cast_first(),       // init 是第 1 个 Expr 子节点
            LetBindingKind::Closure(_) => self.0.try_cast_first(),      // init 就是闭包本身
        }
    }
}
```

理解 `init()` 的三个分支是本节的关键，它利用了「pattern 节点本身能不能被 cast 成 `Expr`」这一性质：

- **`let x = 1`**：pattern 是裸 `Ident(x)`，它**能** cast 成 `Expr`，于是成为第 0 个 Expr 子节点，init `1` 是第 1 个 → 用 `nth(1)`。这就是「复用」：同一个 `x` 子节点既是 pattern 又充当第 0 个 Expr，省得再算偏移。
- **`let _ = 1` / `let (x,y) = ...`**：pattern 是 `Underscore`/`Destructuring`，**不能** cast 成 `Expr`，于是第 0 个 Expr 子节点直接就是 init → 用 `try_cast_first`。为区分这两种情况，`Pattern::Normal`/`Parenthesized` 被单独分到 `nth(1)` 分支。
- **`let f(x) = body`**：闭包语法糖。parser 把 `f(x) = body` 包成一个 `Closure` 子节点，而这个 `Closure` **就是** init（因为 `let f(x)=body` ≡ `let f = (x)=>body`，闭包即是绑定值）。所以 `try_cast_first` 取到的第 0 个 Expr 子节点（Closure）正是 init。

辅助的 `Pattern` 枚举与它的手写 `AstNode`（[src/ast.rs:2172-2207](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L2172-L2207)）——注意 `Pattern` 是聚合枚举，`_ => node.cast().map(Self::Normal)` 这条 fallback 把任意 Expr（含 `Ident`）归为 `Normal`。`Closure` 节点的 `name()` 方法（[src/ast.rs:2108-2125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L2108-L2125)）文档明确写道「只在 `let f(x) = y` 语法糖时存在」，印证了上面的判断。

#### 4.4.4 代码实践

**实践目标**：对照两种 `let` 形态，验证 `kind()` 与 `init()` 的取值。

**操作步骤**（示例代码，待本地验证）：

```rust
// 示例代码
use typst_syntax::Source;
use typst_syntax::ast::{AstNode, LetBinding, LetBindingKind};

fn first_let(node: &typst_syntax::SyntaxNode) -> Option<LetBinding> {
    if let Some(l) = node.cast::<LetBinding>() { return Some(l); }
    node.children().find_map(first_let)
}

for code in ["#let x = 1", "#let f(x) = x + 1"] {
    let src = Source::detached(code);
    let lb = first_let(src.root()).unwrap();
    println!("{}  => kind={:?}  init_kind={:?}",
        code,
        matches!(lb.kind(), LetBindingKind::Closure(_)),
        lb.init().map(|e| format!("{:?}", e)));
}
```

**需要观察的现象**：`#let x = 1` 的 `kind()` 是 `Normal`，`init()` 是字面量 `1`；`#let f(x) = x+1` 的 `kind()` 是 `Closure`（名字 `f`），`init()` 是那个 `Closure` 节点（内部含参数与 body）。

**预期结果**：闭包语法糖下，init 指向 Closure 本身，而非 body 表达式——印证「`let f(x)=body` ≡ `let f = (x)=>body`」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `init()` 的第一个分支用 `nth(1)` 而不是 `nth(0)`？
**答案**：因为对 `let x = 1`，pattern `x` 是一个 `Ident`，本身就能 cast 成 `Expr`，占了第 0 个位置；真正的 init `1` 排在第 1 个。AST 故意复用 pattern 这个子节点作为第 0 个 Expr，省去单独跳过它的逻辑。

**练习 2**：`let _ = 1` 走的是 `init()` 的哪个分支？为什么？
**答案**：走 `LetBindingKind::Normal(_) => self.0.try_cast_first()` 分支。因为 `_` 的 kind 是 `Underscore`，不能 cast 成 `Expr`，所以第 0 个 Expr 子节点直接就是 init `1`，用 `try_cast_first` 取第一个即可。

---

### 4.5 扩展实战：新增一个 AST 节点的完整改动链

#### 4.5.1 概念说明

前三节我们都在「读」AST。这一节讲「写」：当你给 Typst 增加一个新的语法构造（比如一种新的语句或表达式），需要联动修改哪些文件？核心思想是——**AST 方法依赖 parser 产出的固定子结构**，所以新增节点是一个跨文件的「契约签订」过程：先在 kind.rs 立名，再让 parser 按约定造结构，最后在 ast.rs 按约定取数据，并让高亮与集合跟上。

#### 4.5.2 核心流程

新增一个语法构造的典型改动链：

1. **kind.rs**：新增一个 `SyntaxKind` 变体（结构节点）。注意它会被 `node!` 宏当作「同名节点」引用，所以命名要兼顾。若它还需要进入 parser 的 `SyntaxSet` 决策（如可作为某类表达式），变体判别值必须 < 128（见 u2-l3 的 u128 位集限制），否则不能放进集合。
2. **lexer.rs / parser.rs**：若是全新的 token，先在 lexer 加识别；然后在 parser 写一个解析函数，用 `marker()` → eat 各子节点 → `wrap(m, NewKind)` 产出固定子结构。**这一步决定了 AST 方法能取到什么**。
3. **ast.rs**：用 `node! { struct NewNode }` 声明外壳，再写 `impl NewNode` 按第 2 步预订的子节点顺序实现语义方法。若它要能出现在 `Markup::exprs()` / 代码表达式里，还要在 `Expr::from_untyped` 的巨型 `match`（[src/ast.rs:386-402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L386-L402)）里加一支分派。
4. **highlight.rs**：为新 kind 在 `match` 里加一支映射到合适的 `Tag`（颜色类别），否则编辑器里不着色。
5. **set.rs**：若新构造需要参与「是否属于某类」的集合判定（如 `STMT`、`CODE_EXPR`），在预定义集合里补上。
6. **测试**：在对应文件的 `#[cfg(test)] mod tests` 里加解析用例，断言 CST 形状与 AST 方法返回值。

#### 4.5.3 源码精读

以现有的 `heading` 为「模板」看这条链是怎么串起来的：

- kind.rs 立名：`Heading` 与 `HeadingMarker`（[src/kind.rs:59-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L59-L62)）。
- parser 造结构：`heading()`（[src/parser.rs:171-178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178)）用 `assert(HeadingMarker)` + `markup(...)` + `wrap(m, Heading)` 产出 `[HeadingMarker, Markup]`。
- ast.rs 取数据：`Heading::body()` 与 `depth()`（[src/ast.rs:792-811](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L792-L811)）恰好按上面的子结构取。
- 分派进 Expr：`SyntaxKind::Heading => Some(Self::Heading(Heading(node)))`（[src/ast.rs:402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L402) 这一支）。

ast.rs 顶部的「绝不 panic」约定（[src/ast.rs:80-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L80-L85)）用 `#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unreachable)]` 在编译期强制——这意味着你写新节点方法时**不能**用 `unwrap()`/`expect()`，必须用 `cast_first`（自带 placeholder 兜底）或 `try_cast_first` + 显式处理。

#### 4.5.4 代码实践

**实践目标**：设计一个假想的新语法构造，列出改动清单与测试用例（源码阅读 + 设计型实践，不要求真正合入源码）。

**操作步骤**：

1. 假设想新增「脚注引用」`[^id]` 作为一个顶层 markup 表达式 `FootnoteRef`。
2. 按改动链填写下表（示例答案）：

   | 文件 | 改动要点 |
   | --- | --- |
   | kind.rs | 新增 `FootnoteRef`（结构节点）与 `FootnoteRefMarker`（`[^` 引导 token）。注意判别值 <128 才能进集合。 |
   | lexer.rs | 识别 `[^` 产出 `FootnoteRefMarker`，识别 `]` 收尾。 |
   | parser.rs | 新增 `footnote_ref()`：`assert(FootnoteRefMarker)` → 解析 id 文本 → `wrap(m, FootnoteRef)`，产出 `[FootnoteRefMarker, Text]`。 |
   | ast.rs | `node!{ struct FootnoteRef }` + `impl { fn target() -> &'a str { ... } }`；在 `Expr::from_untyped` 加 `SyntaxKind::FootnoteRef => Some(Self::FootnoteRef(...))`。 |
   | highlight.rs | 给 `FootnoteRef` 加一支 `Tag`（如复用现有标签/引用色）。 |
   | set.rs | 若需归入某类表达式集合，补进对应 `syntax_set!`。 |

3. 写一条解析测试（示例代码）：

   ```rust
   // 示例代码：断言 CST 形状
   let src = Source::detached("[^note]");
   // 找到 FootnoteRef 节点，断言其子节点依次为 FootnoteRefMarker、Text("note")
   ```

**需要观察的现象**：自己核对「parser 产出的子节点顺序」与「AST 方法假设的子节点顺序」是否一致——这是整条链正确性的命门。

**预期结果**：能清楚说出每处改动的理由，并指出 AST 方法依赖 parser 产出的固定子结构这一核心契约。

#### 4.5.5 小练习与答案

**练习 1**：为什么在 ast.rs 写新方法时不能用 `unwrap()`？
**答案**：因为 ast.rs 顶部 `#![deny(clippy::unwrap_used, ...)]`（[src/ast.rs:85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L85)）把它定为编译错误。设计原则是「即便遍历结构损坏的 CST 也绝不 panic」，改用 `cast_first`（回退 placeholder）或 `try_cast_first` + 兜底。

**练习 2**：如果新增的结构节点判别值 ≥128，会有什么后果？
**答案**：它无法被加入 `SyntaxSet`（u128 位集只容纳判别值 <128 的 kind，见 u2-l3）。若 parser 不需要用集合判定它，则无影响；若需要（例如要表达「它是某类表达式」），就必须调整枚举顺序使其 <128，或改用其它判定方式。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个贯穿任务：

**任务**：选定 `Heading` 节点，完成「读源码 → 写结构 → 验兜底」三步。

1. **读源码**：阅读 `Heading` 的 `impl`（[src/ast.rs:797-811](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L797-L811)）与 parser 的 `heading()`（[src/parser.rs:171-178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178)）。
2. **写结构**：写出 `Heading` 期望的 CST 子节点顺序，并标注 `body()` 与 `depth()` 分别取哪个子节点。
   - 参考答案：子节点顺序为 `[HeadingMarker, Markup]`；`body()` 用 `cast_first` 取那个 `Markup`，`depth()` 用 `children().find(...)` 取 `HeadingMarker` 并读其字节长度。
3. **验兜底**：解释若一个 Heading 节点被错误编辑、丢失了正文 Markup，调用 `body()` 会发生什么。
   - 参考答案：`cast_first` 找不到 Markup 时回退到 `Markup::placeholder()`（一个空静态节点），下游得到空正文而非 panic；`depth()` 若连 HeadingMarker 也丢了则回退到 `NonZeroUsize::ONE`。
4. **扩展练习**：仿照 4.5，如果要把 Heading 的级别上限从「不限」改成「最多 6 级」（多余 `=` 视为文本），列出你猜测需要改动的文件与函数（提示：改 lexer 的位置敏感判定或 parser 的 `heading()`，以及可能的 AST 语义说明）。

> 注：步骤 4 是开放设计题，重点在于能否识别出「lexer 产生 marker / parser 决定结构 / AST 假设结构」的分工，不要求给出可运行答案。

## 6. 本讲小结

- AST 节点是用 `node!` 宏生成的 `&'a SyntaxNode` 透明包装，宏靠「结构体名 == `SyntaxKind` 变体名」的约定自动完成 kind 守门与 `placeholder` 生成。
- 取子节点只有三类范式：叶子用 `leaf_text`、唯一子节点用 `cast_first`/`cast_last`（自带 placeholder 兜底）、多个或混排用 `children().filter_map(cast)` 或 `children().find(...)`。
- `Raw` 是「AST 抽象掉 CST 细节」的典范：`lines`/`lang`/`block` 三个方法从 `RawDelim`/`RawLang`/`Text`/`RawTrimmed` 混排中提炼语义，定界符只在判定 block 时被偷看。
- `LetBinding` 展示了复杂节点的取数据技巧：`init()` 利用「pattern 能否 cast 成 `Expr`」巧妙复用同一子节点，分三种形态正确取初始化表达式。
- 新增语法构造是一条跨文件契约链：kind.rs 立名 → parser.rs 造固定结构 → ast.rs 按结构取数据 → highlight.rs/set.rs 联动，且 ast.rs 用 `#![deny(...)]` 强制「绝不 panic、改用 placeholder 兜底」。

## 7. 下一步学习建议

本讲结束，AST 单元（U7）的核心——「AST 节点如何从 CST 抽取语义」——已讲透。接下来：

- **若关心「文本与编辑」**：进入 U8 的 `Source`/`Lines`（u8-l1～u8-l3），看 AST/CST 所依托的文本如何被索引与增量编辑。
- **若关心「正确性与性能」**：进入 U9 的增量重解析（u9-l1～u9-l3），看 `reparse` 如何只重建受影响子树——它会反过来用到本讲强调的「parser 产出固定子结构」契约。
- **若想做二次开发**：直接读 u10-l4（二次开发指南与测试），把本讲的扩展流程放进更大的工程语境。
- **建议继续阅读的源码**：通读 ast.rs 中你感兴趣的节点 `impl`（如 `Closure`、`Named`、`Conditional`、`ForLoop`），用本讲的三类范式自验它们取子节点的逻辑。
