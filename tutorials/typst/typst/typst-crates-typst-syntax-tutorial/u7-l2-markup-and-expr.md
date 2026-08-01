# Markup 根与 Expr 枚举

## 1. 本讲目标

上一讲（u7-l1）我们建立了 AST 的「四大特性」，并把「以 `Markup` 为根」当作一句话结论带过。本讲就把这句话拆开精读。学完本讲后，你应当能够：

- 说清 `Markup` 作为整棵 AST 的**唯一根节点**，为什么只对外暴露一个 `exprs()` 方法。
- 逐行读懂 [`Markup::exprs`](src/ast.rs) 里的 **`was_stmt` 过滤**：为什么「紧跟在语句后面的那个空白节点」要被丢弃，丢弃会给渲染带来什么后果。
- 把 `Expr` 枚举理解为「图中的边」：它如何用一个枚举**统一 markup / math / code 三类表达式**，并据此从 `Markup` 分叉到整棵树。
- 解释 **`cast_with_space`** 这个私有助手为什么存在：它和普通 `Expr::from_untyped` 的唯一区别是对 `SyntaxKind::Space` 的处理，并据此对比 `Markup::exprs` / `Math::exprs` / `Code::exprs` 三者的差异。

## 2. 前置知识

本讲是 u7-l1 的直接续篇，默认你已经掌握：

- **AST 是 CST 的类型化视图**：所有 AST 节点都是 `&SyntaxNode` 的透明包装，转换靠 `AstNode::from_untyped` 守门（u7-l1）。
- **`node!` 宏**：靠「结构体名 == `SyntaxKind` 变体名」生成包装结构体 + `AstNode` 实现（u7-l1）。
- **`SyntaxNode::cast`**：对外的便捷转换入口，内部就是 `T::from_untyped(self)`（u7-l1）。
- **`SyntaxKind` 与 `is_stmt`**：`is_stmt()` 对 `LetBinding` / `SetRule` / `ShowRule` / `ModuleImport` / `ModuleInclude` 五种「需要分号或换行来终结的语句」返回 `true`（u2-l2）。
- **Markup 模式的解析**：在 markup 里，空白是**有意义的内容**（会被渲染成间距），所以空白作为 `SyntaxKind::Space` 节点直接挂在 `Markup` 的 children 里，而不是像 code 那样被当成可丢弃的 trivia（u3-l2、u4-l3）。

一个关键直觉：在 Typst 正文里写 `#let x = 1 你好`，`1` 和「你好」之间的那个空格，**只是用来把代码片段和正文隔开**，它不应该在最终文档里产生一段额外的间距。可 CST 是无损的，那个空格一定作为 `Space` 节点存在。于是 `Markup::exprs` 就必须在「保留正文里的空白」和「丢弃语句后的分隔空白」之间做精确区分——这正是本讲的中心问题。

## 3. 本讲源码地图

本讲几乎只读一个文件，但会对照 CST 节点的访问器与分类方法：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/ast.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs) | 整个 AST 模块 | `Markup` 结构体与 `exprs()`、`enum Expr`、`cast_with_space`、`Math::exprs` / `Code::exprs` 对照 |
| [src/kind.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs) | `SyntaxKind` 与分类方法 | `is_stmt()` 判定哪五种节点算「语句」 |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | CST 节点 `SyntaxNode` | `children()`——`Markup::exprs` 迭代的对象 |
| [src/parser.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 解析器 | markup 模式如何把空白当成有意义子节点排进 `Markup`（u4-l3 已讲，本讲只引用结论） |

> 提醒：`ast` 模块在 [lib.rs:3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L3) 用 `pub mod ast;` 整体公开，所以 `Markup`、`Expr`、`AstNode` 都能被下游直接 `use`。但 `cast_with_space` 是私有 `fn`，本讲讲它是为了理解 `exprs()` 的内部机制，外部无法也不应直接调用。

---

## 4. 核心概念与源码讲解

### 4.1 Markup：整棵 AST 的唯一根

#### 4.1.1 概念说明

[ast.rs 顶部文档](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L48-L61) 有一节标题就叫「The AST is rooted in the `Markup` node」。它说：整棵 AST 的入口是 `Markup` 节点，而 `Markup` **只提供一个方法 `Markup::exprs`**，返回主枚举 `Expr` 的迭代器。

为什么设计得这么「瘦」？因为 Typst 的源码在最外层**永远是 markup（正文）**——哪怕你写的是一整篇代码，它也是「正文里嵌了一段代码」。所以把 `Markup` 当根、用 `Expr` 当分叉枚举，就能用一套类型覆盖整篇文档，不必为「文档根」再造一堆包装枚举。文档原话是：这「decrease the amount of wrapper enums needed in the AST」。

`Markup` 本身由 `node!` 宏生成，和 `Raw`、`Text` 一样只是一个 `&SyntaxNode` 的透明包装：

```rust
node! {
    /// The syntactical root capable of representing a full parsed document.
    struct Markup
}
```

参见 [ast.rs:226-L229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L226-L229)。宏展开后等价于 `struct Markup<'a>(&'a SyntaxNode);`，并在 `from_untyped` 里校验 `node.kind() == SyntaxKind::Markup`（u7-l1 已讲过 `node!` 的展开）。`parse()` 返回的根节点 kind 正是 `Markup`，所以 `src.root().cast::<Markup>()` 一定能成功。

#### 4.1.2 核心流程

从一段文本到「拿到可遍历的 AST」只有三步：

1. `parse(text)`（parser.rs）→ 返回根 `SyntaxNode`（kind = `Markup`）。
2. `root.cast::<Markup>()`（ast.rs）→ 把根包成 `Markup<'a>`。
3. `markup.exprs()`（ast.rs）→ 返回一个**惰性**迭代器，逐个产出 `Expr<'a>`。

关键在第 3 步：`exprs()` 返回的是 `impl DoubleEndedIterator`，**遍历到才转换**（Lazy 特性），不预先把整棵树转成 AST。

#### 4.1.3 源码精读

`Markup` 唯一的方法签名如下（[ast.rs:231-L245](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L231-L245)）：

```rust
impl<'a> Markup<'a> {
    /// The expressions.
    pub fn exprs(self) -> impl DoubleEndedIterator<Item = Expr<'a>> {
        let mut was_stmt = false;
        self.0
            .children()
            .filter(move |node| {
                // Ignore newline directly after statements without semicolons.
                let kind = node.kind();
                let keep = !was_stmt || node.kind() != SyntaxKind::Space;
                was_stmt = kind.is_stmt();
                keep
            })
            .filter_map(Expr::cast_with_space)
    }
}
```

这段代码做两件事，对应两个串联的迭代器适配器：

- `.filter(...)`：实现 `was_stmt` 过滤，决定哪些子节点**进入**下一步（详见 4.2）。
- `.filter_map(Expr::cast_with_space)`：把保留下来的 `&SyntaxNode` 转成 `Expr`，转换失败的（如注释）被丢弃（详见 4.4）。

`self.0.children()` 是 `SyntaxNode::children()`，返回该节点的直接子节点迭代器（u5-l2）。

#### 4.1.4 代码实践

**目标**：确认 `Markup` 是根，且 `exprs()` 是它唯一的遍历入口。

**操作步骤**：

1. 在仓库内运行只读检查（不修改任何源码），确认 `Markup` 上只有一个公开方法：

   ```bash
   # 查看 ast.rs 里 impl<'a> Markup<'a> 块内定义了多少 pub fn
   ```

   你可以用编辑器跳转到 [ast.rs:231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L231)，会看到 `impl<'a> Markup<'a>` 块里**只有一个** `pub fn exprs`。

2. 写一个最小程序（**示例代码**，需自行放到依赖 `typst-syntax` 的项目里运行）：

   ```rust
   use typst_syntax::ast::{AstNode, Markup};
   use typst_syntax::parse;

   fn main() {
       let root = parse("#let x = 1 你好");
       let markup: Markup = root.cast::<Markup>().unwrap();
       println!("root kind = {:?}, cast 成功", root.kind());
       for expr in markup.exprs() {
           println!("{:?}", expr.to_untyped().kind());
       }
   }
   ```

**需要观察的现象**：`root kind` 打印为 `Markup`，`cast::<Markup>()` 返回 `Some`。

**预期结果**（基于源码推导，**待本地验证**）：循环会逐行打印 `Expr` 对应的 `SyntaxKind` 名——你会看到 `LetBinding`，但 `1` 和「你好」之间的那个空格**是否**出现，正是 4.2 要回答的问题。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Markup` 不像 `Raw` 那样有 `block()` / `lang()` 之类的语义方法？
**答案**：`Markup` 是「文档根」，它的唯一职责是把文档拆成一串表达式；文档级别没有「是不是块级」「语言标签」这类属性，那些是**单个表达式**（如 `Raw`）才有的语义，所以 `Markup` 只需要一个 `exprs()`。

**练习 2**：如果 `parse()` 返回的根节点 kind 不是 `Markup`，`root.cast::<Markup>()` 会怎样？
**答案**：返回 `None`。因为 `node!` 宏生成的 `from_untyped` 会校验 `node.kind() == SyntaxKind::Markup`，不匹配就返回 `Option::None`（u7-l1）。

---

### 4.2 was_stmt 过滤：丢弃「语句后面的空白」

#### 4.2.1 概念说明

这是本讲最关键、也最容易被忽略的一行代码。问题来自一个真实的渲染需求：

> 在正文里写 `#let x = 1 你好` 时，`1` 后面那个空格**只是分隔符**，不应该在渲染出的文档里产生一段间距；但正文里正常的换行与空格（如 `你好 世界`）又**必须保留**为间距。

CST 是无损的，两种空格在树里都是 `SyntaxKind::Space` 节点，**长得一模一样**。唯一的区分线索是**上下文**：紧跟在「语句」后面的那个 Space 才是分隔符。于是 `Markup::exprs` 用一个可变布尔量 `was_stmt`（「上一个保留下来的节点是不是语句」）来做这件事。

#### 4.2.2 核心流程

`was_stmt` 过滤的核心是一个带状态的闭包，对每个子节点依次判定 `keep`（要不要保留）：

```
对 self.0.children() 中的每个 node（从左到右）：
    kind  = node.kind()
    keep  = (非 was_stmt) 或 (kind 不是 Space)     # 关键判定
    was_stmt = kind.is_stmt()                       # 更新状态，供下一个节点用
    若 keep 则把 node 传给下一步
```

判定逻辑的真值表（`was_stmt` 指**上一个**节点是否为语句）：

| was_stmt（上一节点） | 当前 kind | keep | 新 was_stmt |
| :---: | :---: | :---: | :---: |
| `false` | 任意 | `true` | `kind.is_stmt()` |
| `true` | `Space` | **`false`（丢弃）** | `false` |
| `true` | 非 `Space` | `true` | `kind.is_stmt()` |

读表的要点：**只有「上一个节点是语句」且「当前节点恰好是 Space」时才丢弃**；而且丢完这一个 Space 后 `was_stmt` 立刻被重置为 `false`，所以**连续的第二个 Space 会被保留**（它代表真正的段落间距）。

「语句」由 [`is_stmt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L329-L338)（[kind.rs:329-L338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L329-L338)）定义，只认这五种需要「分号或换行」来终结的节点：

```rust
/// Does this node need termination through a semicolon or linebreak?
pub fn is_stmt(self) -> bool {
    matches!(
        self,
        Self::LetBinding
            | Self::SetRule
            | Self::ShowRule
            | Self::ModuleImport
            | Self::ModuleInclude
    )
}
```

注意：`if` / `while` / `for` 在 Typst 里是**表达式**（`Conditional` / `WhileLoop` / `ForLoop`），不在 `is_stmt` 里；`return` 和解构赋值也不在内。所以 `#if x {..} 后面的空格不会被过滤`——这五种「语句」才触发过滤。

#### 4.2.3 源码精读

聚焦 [ast.rs:234-L243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L234-L243) 这段闭包：

```rust
let mut was_stmt = false;
self.0
    .children()
    .filter(move |node| {
        // Ignore newline directly after statements without semicolons.
        let kind = node.kind();
        let keep = !was_stmt || node.kind() != SyntaxKind::Space;
        was_stmt = kind.is_stmt();
        keep
    })
```

- `let mut was_stmt = false;`：初始为 `false`——文档第一个节点前没有「上一个语句」，所以开头的空格不会被误杀。
- `move`：把 `was_stmt` 的所有权搬进闭包，使闭包成为有状态的 `FnMut`。
- `keep = !was_stmt || node.kind() != SyntaxKind::Space;`：短路求值——只要「上一节点不是语句」就直接 `keep=true`，根本不必判断是不是 Space。
- `was_stmt = kind.is_stmt();`：**用当前节点的 kind 更新状态**，注意这里用的是当前 `kind`，不是 Space 的判定结果。

源码注释一语道破意图：「Ignore newline directly after statements without semicolons.」（忽略没有分号结尾的语句**之后**的换行）。`Space` 节点在 markup 里最多含一个换行（见 [Space 的 node! 文档](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L593-L597)「Has at most one newline」），所以「单个换行」也会被打包成 Space 而被这条规则丢弃。

> 小提示：`exprs()` 声明返回 `impl DoubleEndedIterator`（双向迭代器）。但这个 `filter` 闭包**有状态**，反向迭代时 `was_stmt` 会「倒着」更新，语义与正向不同。类型上能双向、但这条过滤逻辑是为**正向遍历**设计的——日常使用也都是正向。

#### 4.2.4 代码实践

**目标**：直观看到「语句后的 Space 被丢弃，正文里的 Space 被保留」。

**操作步骤**：运行下面这段**示例代码**，对比「原始 children」与「`exprs()` 过滤后」两份输出。

```rust
use typst_syntax::ast::{AstNode, Markup};
use typst_syntax::parse;

fn main() {
    let text = "#let x = 1 你好 世界";
    let root = parse(text);
    let markup: Markup = root.cast().unwrap();

    println!("== 原始 children（CST 无损视角）==");
    for child in root.children() {
        println!("{:?}", child.kind());
    }

    println!("== Markup::exprs()（AST 视角，was_stmt 过滤后）==");
    for expr in markup.exprs() {
        println!("{:?}", expr.to_untyped().kind());
    }
}
```

**需要观察的现象**：

- 第一份输出里你会看到 `LetBinding`、`Space`、`Text`、`Space`、`Text`（CST 把每个空格都如实存下）。
- 第二份输出里 `LetBinding` 后面**紧跟的那个 `Space` 消失了**，但「你好」与「世界」之间的 `Space` 仍然保留。

**预期结果**（基于源码推导，**待本地验证**）：

```
== 原始 children（CST 无损视角）==
LetBinding
Space
Text
Space
Text
== Markup::exprs()（AST 视角，was_stmt 过滤后）==
LetBinding
Text
Space
Text
```

差别只在 `LetBinding` 之后的那一个 `Space`：它作为「语句后的分隔空白」被 `was_stmt` 过滤掉了。

#### 4.2.5 小练习与答案

**练习 1**：把输入改成 `#let x = 1\n\n你好`（语句后跟**两个**换行），`exprs()` 会输出什么？
**答案**：两个换行会被 parser 组合成一个 `Parbreak`（段落分隔）而非 `Space`，而 `Parbreak` 不是 `Space`，过滤条件 `kind != Space` 不成立 → `keep=true`，于是**保留**。这也符合直觉：两个换行代表「另起一段」，是正文语义而非分隔符。

**练习 2**：为什么 `was_stmt` 的初值是 `false` 而不是 `true`？
**答案**：文档开头的空格前并没有「上一个语句」。若初值为 `true`，文档第一个节点如果是 `Space` 就会被误丢弃。初值 `false` 保证只有「确实跟在语句后面」的 Space 才被过滤。

**练习 3**：`#return x 你好` 里的空格会被过滤吗？
**答案**：不会。`FuncReturn`（`return`）不在 `is_stmt()` 的五种里，所以 `was_stmt` 不会被置为 `true`，紧跟的 Space 被保留。只有 `let` / `set` / `show` / `import` / `include` 五种语句后的 Space 才会被丢弃。

---

### 4.3 enum Expr：统一三类表达式的「边」

#### 4.3.1 概念说明

[`Expr`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L248-L375) 是整个 AST 里最重要的枚举（[ast.rs:248-L375](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L248-L375)）。文档对它的定位是：

> [`Expr`] is important because it contains the majority of expressions that Typst will evaluate. Not just markup, but also math and code expressions.

它把 markup（`Text` / `Strong` / `Heading` / `ListItem` …）、math（`Math` / `MathText` / `MathFrac` / `MathAttach` …）和 code（`LetBinding` / `Binary` / `FuncCall` / `Closure` …）三类表达式**塞进同一个枚举**。文档还给了一个很妙的比喻：

> You can view enums in this file as **edges on a graph**: areas where the tree has paths from one type to another.

也就是：结构体（`Raw`、`LetBinding` …）是图的「节点」，而 `Expr` 这样的枚举是图的「边」——从一个类型通往多个可能类型的分叉口。`Markup::exprs()` 返回 `Expr`，就完成了从「文档根」向「各种表达式」的第一次分叉；之后每个表达式的语义方法又会返回别的枚举（如 `LetBinding::kind()` 返回另一个枚举），一层层展开整棵树。

#### 4.3.2 核心流程

`Expr` 的转换由 `AstNode` trait 的手写实现承担（因为它要把**多种** `SyntaxKind` 都映射进来，不能像普通节点那样靠 `node!` 宏「一个 kind 对一个结构体」）。流程是：

```
拿到一个 &SyntaxNode（已被 was_stmt 过滤保留）
    ↓ Expr::cast_with_space(node)
    ↓ 内部 match node.kind()
Space        → Some(Expr::Space(...))     # cast_with_space 特有
其它匹配项   → 转给 Expr::from_untyped    # 巨型 match，逐 kind 映射
未匹配       → None（被 filter_map 丢弃）
```

`from_untyped` 是一个覆盖几乎所有 `SyntaxKind` 的巨型 `match`（[ast.rs:386-L458](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L386-L458)），形如：

```rust
SyntaxKind::Text   => Some(Self::Text(Text(node))),
SyntaxKind::Strong => Some(Self::Strong(Strong(node))),
SyntaxKind::LetBinding => Some(Self::LetBinding(LetBinding(node))),
// ... 几十个分支 ...
_ => Option::None,
```

每个分支都是「把 `&SyntaxNode` 包进对应的具名结构体」。最后 `_ => Option::None` 兜底——注释、`End`、`Error` 等不属于 `Expr` 的 kind 会返回 `None`，被 `filter_map` 丢弃。

#### 4.3.3 源码精读

`Expr` 的变体可以按三类归位（节选自 [ast.rs:250-L375](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L250-L375)）：

| 来源模式 | 代表变体 | 说明 |
| --- | --- | --- |
| Markup | `Text` / `Space` / `Strong` / `Emph` / `Raw` / `Heading` / `ListItem` / `EnumItem` / `TermItem` / `Link` / `Ref` … | 正文里的内容元素 |
| Math | `Math` / `MathText` / `MathIdent` / `MathFrac` / `MathAttach` / `MathDelimited` / `MathCall` … | 公式里的元素（`Equation` 是 `$...$` 外壳，`Math` 是其内部） |
| Code | `LetBinding` / `Binary` / `Unary` / `FuncCall` / `Closure` / `SetRule` / `ShowRule` / `Conditional` / `ForLoop` / `Ident` / `Int` / `Str` … | `#` 后或 `{ }` 代码块里的表达式 |

一个值得注意的细节：`Expr` 派生了 `Debug, Copy, Clone, Hash`（[ast.rs:249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L249)）。`Copy` 是因为每个变体都只持有一个 `&SyntaxNode`（一个指针），复制代价极低——这也是「AST 是视图」带来的红利。

#### 4.3.4 代码实践

**目标**：用 `match` 体验 `Expr` 作为「分叉边」的感觉。

**操作步骤**：对解析结果做一次分派（**示例代码**）：

```rust
use typst_syntax::ast::{AstNode, Expr, Markup};
use typst_syntax::parse;

fn main() {
    let root = parse("正文 *粗体* #let x = 1");
    let markup: Markup = root.cast().unwrap();
    for expr in markup.exprs() {
        match expr {
            Expr::Text(_)    => println!("遇到正文文本"),
            Expr::Strong(_)  => println!("遇到粗体"),
            Expr::LetBinding(_) => println!("遇到 let 语句"),
            other => println!("其它: {:?}", other.to_untyped().kind()),
        }
    }
}
```

**需要观察的现象**：只有 `match` 命中具体变体后，你才能拿到 `Strong` / `LetBinding` 等具名结构体，进而调用它们的语义方法（如 `strong` 的子节点、`let` 的绑定模式）。这印证了「枚举是边、结构体是节点」。

**预期结果**：依次打印类似「遇到正文文本 / 遇到粗体 / 遇到 let 语句」。注意 `*粗体*` 前后的空格如何出现取决于 4.2 的过滤——这里它们前面不是语句，所以会作为 `Expr::Space` 落入 `other` 分支。**待本地验证**精确的打印顺序。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Expr` 不能用 `node!` 宏生成？
**答案**：`node!` 宏的约定是「结构体名 == 单个 `SyntaxKind` 变体名」，只能处理**一对一**映射。而 `Expr` 要把**几十种** `SyntaxKind` 都映射到自己的不同变体，是一对多，必须手写 `AstNode::from_untyped` 的巨型 `match`。

**练习 2**：`Expr::from_untyped` 对 `SyntaxKind::Space` 返回什么？为什么？
**答案**：返回 `None`（[ast.rs:389](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L389) 有注释「Skipped unless using `cast_with_space`」）。这是为了让「不想保留空白」的调用方（如 `Code::exprs`）直接用 `cast` 就能把 Space 过滤掉；想保留的调用方（`Markup` / `Math`）则改用 `cast_with_space`。这是下一节的主题。

---

### 4.4 cast_with_space：为何 Space 要特殊处理

#### 4.4.1 概念说明

现在矛盾出现了：

- `Expr::from_untyped` 对 `Space` 返回 `None`（默认丢弃空白）。
- 但 `Markup::exprs` 又**必须保留**正文里的空白（否则文档里的间距全没了）。

解决办法是一个私有助手 [`cast_with_space`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L377-L384)（[ast.rs:377-L384](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L377-L384)）：它和 `from_untyped` 几乎一样，**唯一**的区别是先把 `Space` 单独映射成 `Some(Expr::Space(...))`，其余 kind 才转给 `from_untyped`。

```rust
impl<'a> Expr<'a> {
    fn cast_with_space(node: &'a SyntaxNode) -> Option<Self> {
        match node.kind() {
            SyntaxKind::Space => Some(Self::Space(Space(node))),
            _ => Self::from_untyped(node),
        }
    }
}
```

`Space` 也是 `node!` 宏生成的具名结构体（[ast.rs:593-L597](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L593-L597)），文档注明「Has at most one newline in markup, as more indicate a paragraph break」（markup 里最多含一个换行，多了就是段落分隔）。

#### 4.4.2 核心流程

`Markup::exprs` 把两个机制**串起来**用，缺一不可：

```
self.0.children()
  │
  ├─ .filter(was_stmt 闭包)        # 1. 丢弃「语句后的那个 Space」（4.2）
  │
  └─ .filter_map(Expr::cast_with_space)  # 2. 把剩下的节点转成 Expr；
                                         #    用 cast_with_space 而非 cast，
                                         #    是为了保留正文里的 Space
```

- 第 1 步决定「语句后的分隔空白」要丢。
- 第 2 步决定「其余正文空白」要留（用 `cast_with_space`），同时把不属于 `Expr` 的节点（注释等）丢掉（返回 `None`）。

#### 4.4.3 源码精读：三种 `exprs` 的对比

把 `Markup::exprs` 与 `Math::exprs`、`Code::exprs` 并排放，差别一目了然：

| 入口 | 是否用 `cast_with_space`（保留 Space） | 是否有 `was_stmt` 过滤 | 含义 |
| --- | :---: | :---: | --- |
| [`Markup::exprs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L231-L245) | ✅ | ✅ | 正文里空白有意义要保留，但语句后的分隔空白要丢 |
| [`Math::exprs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L887-L891) | ✅ | ❌ | 公式里空白（轻度）有意义要保留，但公式内**没有顶层语句**，无需过滤 |
| [`Code::exprs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1561-L1566) | ❌（用普通 `SyntaxNode::cast`） | ❌ | 代码里语句间空白是无关 trivia，直接全部丢弃 |

对照源码：

```rust
// Math::exprs —— 只用 cast_with_space，无 was_stmt（ast.rs:889-L890）
pub fn exprs(self) -> impl DoubleEndedIterator<Item = Expr<'a>> {
    self.0.children().filter_map(Expr::cast_with_space)
}

// Code::exprs —— 用普通 cast，Space 被 from_untyped 返回 None 丢弃
// （ast.rs:1563-L1565）
pub fn exprs(self) -> impl DoubleEndedIterator<Item = Expr<'a>> {
    self.0.children().filter_map(SyntaxNode::cast)
}
```

关键洞见：

1. **`Code::exprs` 根本不需要 `cast_with_space`**：代码块里的空白本来就是 trivia，全部丢弃即可，所以用普通 `SyntaxNode::cast`（等价于走 `Expr::from_untyped`，对 Space 返回 `None`）。也正因如此，`Code::exprs` **不需要** `was_stmt` 过滤——它压根不保留任何 Space，自然没有「语句后空格」的问题。
2. **`Math::exprs` 需要 `cast_with_space` 但不需要 `was_stmt`**：数学公式里的空白有（轻度）排版意义要保留；但公式内部不存在 `let` / `set` 这类「需要终结符的顶层语句」，所以没有「语句后分隔空白」要处理。
3. **只有 `Markup::exprs` 两者都要**：正文既要保留空白、又得处理嵌入语句后的分隔空白，于是同时挂上 `was_stmt` 过滤和 `cast_with_space`。

一句话总结：`cast_with_space` 回答「**要不要**保留空白」，`was_stmt` 过滤回答「保留之后，**哪一个**空白其实是分隔符该丢掉」。

#### 4.4.4 代码实践

**目标**：验证 `Code::exprs` 确实丢弃了所有空白，与 `Markup::exprs` 形成对照。

**操作步骤**：用 `parse_code` 解析一段**不含大括号**的代码内容（大括号 `{ }` 在 Typst 里是 `CodeBlock`，而 `parse_code` 的根直接就是 `Code`），再遍历 `Code::exprs`（**示例代码**）：

```rust
use typst_syntax::ast::{AstNode, Code};
use typst_syntax::parse_code;

fn main() {
    // parse_code 以 Code 模式解析，根 kind 为 Code；直接喂「代码内容」即可
    let root = parse_code("let x = 1; x");
    let code: Code = root.cast().unwrap(); // 根就是 Code，直接 cast
    for expr in code.exprs() {
        println!("{:?}", expr.to_untyped().kind());
    }
}
```

> 说明：`parse_code` 的根 kind 是 `Code`，可直接 `cast::<Code>()`。若你改用 `parse("#{ let x = 1; x }")`，则根是 `Markup`，需先在 `exprs()` 里取到 `Expr::CodeBlock` 再调 `.body()` 拿到 `Code`。两种入口的根结构不同，**待本地验证**。

**需要观察的现象**：输出里**没有任何 `Space`**——所有空白都被普通 `cast` 丢弃，只剩 `LetBinding`、`Ident`（即 `x`）等表达式节点。

**预期结果**：与 4.2 中 `Markup::exprs`「保留正文 Space」形成鲜明对比——代码模式里空白一律不保留。

#### 4.4.5 小练习与答案

**练习 1**：如果不引入 `cast_with_space`，而直接在 `Expr::from_untyped` 里把 `Space` 也映射成 `Some(Expr::Space(...))`，会有什么后果？
**答案**：`Code::exprs` 用的是普通 `cast`（即 `from_untyped`），它就会**不再丢弃**代码里的空白，把所有 trivia 空格都当表达式吐出来——这违背了「代码里空白是无关 trivia」的语义。所以 Typst 选择让 `from_untyped` 默认丢弃 Space，再用 `cast_with_space` 在需要保留的 `Markup` / `Math` 处「开个口子」。

**练习 2**：`Math::exprs` 为什么不需要 `was_stmt` 过滤？
**答案**：`was_stmt` 过滤的目的是丢掉「嵌入语句后的分隔空白」。数学公式（`$...$` 内部）里不存在 `let` / `set` / `show` / `import` / `include` 这类需要终结符的顶层语句（`is_stmt` 对数学节点都返回 `false`），所以不会出现「语句后空格」这种情况，自然不需要这道过滤。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「AST 探查器」小任务。

**任务**：写一个函数，输入一段 Typst 正文文本，打印出它经 `Markup::exprs()` 后的「表达式序列摘要」，并对其中**至少两种** `Expr` 变体调用各自的语义方法。

**要求**：

1. 用 `parse()` 得到根，`cast::<Markup>()` 后遍历 `exprs()`。
2. 对每个 `Expr`，打印它的 `SyntaxKind`（用 `expr.to_untyped().kind()`），证明你能区分 markup/math/code 三类变体。
3. 至少对 `Expr::Strong`（调用其 children 相关方法）和 `Expr::LetBinding`（调用 `binding` / `init` 之类方法，参考 u7-l3）各做一次语义访问。
4. 故意构造一个「语句后跟空格再跟正文」的输入（如 `#let x = 1 Hello *world*`），在你的输出里**确认**那个空格被 `was_stmt` 过滤掉了（即 `LetBinding` 后直接是 `Text`，没有 `Space`）。

**参考思路**（**示例代码**，部分方法名需对照 ast.rs 实际签名）：

```rust
use typst_syntax::ast::{AstNode, Expr, Markup};
use typst_syntax::parse;

fn summarize(text: &str) {
    let markup: Markup = parse(text).cast().unwrap();
    for expr in markup.exprs() {
        let kind = expr.to_untyped().kind();
        print!("{:?}\t", kind);
        match expr {
            Expr::LetBinding(lb) => {
                // LetBinding 的语义方法见 u7-l3，这里只演示「能拿到具名结构体」
                println!("(let 绑定)");
            }
            Expr::Strong(s) => {
                // 演示对具名结构体做语义访问：数一下它有几个子节点
                let n = s.to_untyped().children().count();
                println!("(strong, 子节点数 = {})", n);
            }
            _ => println!(),
        }
    }
}

fn main() {
    summarize("#let x = 1 Hello *world*");
}
```

**验收标准**：你能向别人解释清楚——

- 为什么 `Markup::exprs()` 的输出里没有 `LetBinding` 之后的那个 `Space`（`was_stmt` 过滤）。
- 为什么同样的遍历换成 `Code::exprs()` 就**一个 Space 都没有**（普通 `cast` 丢弃 Space）。
- `Expr` 枚举是如何作为「边」，让你从 `Markup` 一次性分流到 `Strong`、`LetBinding` 等不同具名节点的。

---

## 6. 本讲小结

- **`Markup` 是整棵 AST 的唯一根**：由 `node!` 生成，只对外暴露一个 `exprs()` 方法，返回 `Expr` 迭代器；文档最外层永远是 markup，所以一套类型就能覆盖整篇文档。
- **`was_stmt` 过滤是本讲核心**：`Markup::exprs` 用一个带状态闭包 `was_stmt` 丢弃「紧跟在语句（`let` / `set` / `show` / `import` / `include`）后面的那一个 `Space`」，因为那只是分隔符、不该被渲染成间距；正文里其余空白照常保留。
- **`enum Expr` 是「图中的边」**：它把 markup / math / code 三类表达式塞进同一个枚举，从 `Markup` 向几十种具名节点完成第一次分叉；因为是一对多映射，必须手写 `AstNode::from_untyped` 的巨型 `match`，不能用 `node!` 宏。
- **`cast_with_space` 解决「空白留不留」**：`Expr::from_untyped` 默认对 `Space` 返回 `None`（丢弃），而私有助手 `cast_with_space` 额外把 `Space` 映射成 `Some(Expr::Space)`（保留）。
- **三种 `exprs` 的差异**：`Markup::exprs` = `was_stmt` 过滤 + `cast_with_space`（留正文空白、丢语句后空白）；`Math::exprs` = 仅 `cast_with_space`（留空白、无语句需过滤）；`Code::exprs` = 普通 `cast`（空白全丢，故也无需 `was_stmt`）。
- **设计哲学**：`cast_with_space` 回答「要不要保留空白」，`was_stmt` 过滤回答「保留之后哪一个空白其实是分隔符」——两者职责分离，组合出 markup 的精确空白语义。

## 7. 下一步学习建议

- 下一讲 **u7-l3「典型 AST 节点剖析与扩展」** 会以 `Raw` / `Text` / `LetBinding` 为例，讲清「AST 方法依赖 parser 产出的固定子结构」这一假设，并演示如何为新 `SyntaxKind` 添加 AST 节点（`node!` + 手写 `impl`）。本讲你已经在 `match` 里碰到 `LetBinding` / `Strong`，下一讲就教你读懂它们的方法实现。
- 想更牢地掌握 `was_stmt` 的判定依据，可回头对照 [kind.rs 的 `is_stmt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L329-L338)（u2-l2）与 markup 模式的解析流程（u4-l3），理解「为什么这五种节点需要终结符」。
- 建议再读一遍 [ast.rs 顶部文档第 48–61 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L48-L61)「The AST is rooted in the `Markup` node」一节，现在你应该能完全看懂它说的「`Expr` decreases the amount of wrapper enums」与「enums as edges on a graph」两句话的分量。
