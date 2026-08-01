# AST 是 CST 的类型化视图

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Typst 里 **CST** 与 **AST** 的关系：AST 不是另一棵独立的树，而是建立在 CST 之上的「类型化视图」。
- 复述 AST 的四大特性：**视图（View）、类型化（Typed）、以 `Markup` 为根、惰性（Lazy）**。
- 掌握 [`AstNode`] trait 的三个核心方法 `from_untyped` / `to_untyped` / `placeholder`，以及默认方法 `span`。
- 理解 `SyntaxNode::cast` / `SyntaxNode::is` 如何把一个无类型的 CST 节点安全地转换成类型化 AST 节点。
- 读懂 `node!` 宏：它如何通过「校验 `kind`」生成一个安全的包装结构体。
- 理解 `placeholder()` 兜底机制——为什么遍历一个结构不合法的 CST 也不会 panic。

## 2. 前置知识

本讲依赖你在前几讲已经建立的概念：

- **CST（具体语法树）**：由 parser 产出，载体是 `SyntaxNode`。它是「无损」的——中序遍历可以精确还原源码文本。CST 节点只携带一个 `SyntaxKind` 标签，但**不提供语义**（详见 u5-l1、u5-l2）。
- **`SyntaxKind`**：lexer 产出的 token 与 parser 构造的节点共用的统一标签枚举（详见 u2-l1）。
- **`SyntaxNode` 的访问器**：`kind()`、`children()`、`leaf_text()` 等（详见 u5-l2）。
- **Span**：每个节点的稳定身份证号（详见 u6-l1）。

一个关键直觉：CST 里所有节点都是同一种类型 `SyntaxNode`，你拿到一个节点后只能问「你的 `kind` 是什么」「你的孩子是谁」，却**无法**问「你是不是一个 raw 块、语言标签是什么、是不是块级」。这类**语义**问题正是 AST 要回答的。AST 用 Rust 的类型系统，把「`kind == SyntaxKind::Raw` 的节点」专门包成一个 `Raw<'a>` 结构体，并在它上面挂 `block()` / `lang()` / `lines()` 等方法。只有当你把一个 `SyntaxNode` **转换（cast）**成 `Raw` 之后，Rust 才允许你调用这些方法。

> 类比：CST 像是一堆「没贴分类标签的纸箱」，AST 则是给某类纸箱贴上「这是 Raw 箱」的标签后，你才知道这个箱子里第一格放的是定界符、第二格放的是语言标签。

## 3. 本讲源码地图

本讲几乎只读一个文件，但会顺带触及 CST 节点的两个访问器：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/ast.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs) | 整个 AST 模块 | 顶部文档（四大特性）、`AstNode` trait、`node!` 宏、`Raw` / `Markup` 等示例节点 |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | CST 节点 `SyntaxNode` | `kind()`、`children()`、`placeholder(kind)` |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs) | crate 门面 | `pub mod ast;`——AST 是少数整个模块对外的部分 |

需要先建立的全局事实：

- `ast` 模块在 [lib.rs:3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L3) 用 `pub mod ast;` **整体公开**（不像 lexer/parser 那样是私有 `mod`）。下游（如 `typst-eval`、IDE）大量直接 `use typst_syntax::ast::*;`。
- 本讲不讨论任何具体节点的「业务语义」（如 `LetBinding` 怎么绑定），那是 u7-l3 的主题；本讲只讲「AST 与 CST 的接口契约」这一层元规则。

## 4. 核心概念与源码讲解

### 4.1 AST 的四大特性

#### 4.1.1 概念说明

[src/ast.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs) 顶部有一段很长的模块文档（`//!`），用四个小标题把 AST 的设计哲学讲得非常清楚。这是理解整个模块的「宪法」，值得逐条记住：

| 特性 | 一句话解释 | 带来的后果 |
| --- | --- | --- |
| **View（视图）** | AST 节点只是 `&SyntaxNode` 的薄包装，例如宏展开后 `struct Raw<'a>(&'a SyntaxNode);` | AST 不复制树、不占额外内存；它「抽象掉」了 CST 里的细节（如 raw 的反引号定界符） |
| **Typed（类型化）** | 每种节点是一个具名 Rust 类型，挂着自己的语义方法 | 只有先「转换」成该类型，才能调用它的方法；类型即安全保证 |
| **以 `Markup` 为根** | 整棵 AST 的入口是 `Markup` 节点，它只提供一个 `exprs()` 方法 | `Expr` 枚举是连接各类型节点的「边」，统一了 markup / math / code 三类表达式 |
| **Lazy（惰性）** | CST → AST 的转换**只在遍历到时**才发生 | 走不到的分支不会付出转换代价；这是「树解释器」求值模型的基础 |

#### 4.1.2 核心流程

理解这四条，可以用一张「视角对照图」：

```
          CST（唯一真相，parser 产出）              AST（按需的类型化视图）
          ┌──────────────────────────┐              ┌──────────────────────────┐
   文本 → │ 一堆同质 SyntaxNode，     │   cast()     │ 具名结构体 Raw/Text/...   │
          │ 各带一个 SyntaxKind 标签  │ ───────────▶ │ 挂着 block()/lang() 等方法 │
          │ 无损、含定界符与 trivia   │   惰性、按需  │ 抽象掉定界符，只留语义    │
          └──────────────────────────┘              └──────────────────────────┘
                       ↑                                       ↑
                 numberize 盖 Span                        以 Markup 为根
```

关键流程要点：

1. parser 先把文本切成 CST，**顺便**把结构「排版」好，使 AST 模块后续能机械地从子节点顺序里「拆」出语义（文档原话：*the format is prepared ahead-of-time by the parser*）。
2. 调用方拿到 `&SyntaxNode`，按需调用 `cast::<Raw>()` 等转换成具名 AST 节点。
3. 走不到的分支永远不会被转换——这就是「惰性」。
4. 从根 `Markup` 出发，通过 `exprs()` 拿到 `Expr` 枚举迭代器，再由各 AST 节点的方法向下游展开。

#### 4.1.3 源码精读

四大特性的权威出处是顶部文档，逐段对应：

**① View（视图）**——文档明确：AST 节点是 `SyntaxNode` 指针的包装，并解释了为何 raw 节点在 AST 里「看不见」反引号定界符：

> Most AST nodes are wrapper structs around [`SyntaxNode`] pointers. … the AST doesn't include these delimiters because it _abstracts_ over the backticks.

见 [ast.rs:7-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L7-L27)。这段定义了「CST 有 `SyntaxKind` 但无语义，语义通过 AST 拆包获得」的根本分工。

**② Typed（类型化）**——文档给出了安全模型：AST 节点除了实现 `AstNode`，还各自有「真正的」语义方法；而能在本文件之外创建 AST 节点的**唯一**途径是 `AstNode::from_untyped`，`node!` 宏在其中校验 `kind`：

> This is a safe interface because the only way to create an AST node outside this file is to call [`AstNode::from_untyped`]. The `node!` macro implements `from_untyped` by checking the node's kind before constructing it, returning `Some()` only if the kind matches.

见 [ast.rs:29-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L29-L46)。这就是「只要 cast 成功，底层子结构就一定如方法所假设」的类型安全保证的来源。

**③ 以 Markup 为根**——文档说明 `Markup` 只暴露 `exprs()`，而 `Expr` 枚举汇合了 markup / math / code 三类表达式，并把枚举比作图中的「边」、结构体比作「节点」：

> The AST is rooted in the [`Markup`] node, which provides only one method: [`Markup::exprs`]. … You can view enums in this file as edges on a graph.

见 [ast.rs:48-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L48-L61)。对应的源码：`Markup` 节点声明与 `exprs()` 实现在 [ast.rs:226-246](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L226-L246)：

```rust
node! {
    /// The syntactical root capable of representing a full parsed document.
    struct Markup
}

impl<'a> Markup<'a> {
    /// The expressions.
    pub fn exprs(self) -> impl DoubleEndedIterator<Item = Expr<'a>> {
        // ...过滤「语句后的换行」，再 filter_map(Expr::cast_with_space)
    }
}
```

`Expr` 枚举（[ast.rs:248](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L248) 起）确实是把 `Text` / `Raw` / `Strong` / `Equation` / `Math` / `Ident` … 三大类表达式装进同一个枚举，正是「图中的边」。

**④ Lazy（惰性）**——文档用「if 分支里没走到的 raw 块，不会付出拆行/判断块级的代价」举例，并指出这与当前「树解释器」求值模型相互配套：

> Being lazy means that the untyped CST nodes are converted to typed AST nodes only as the tree is traversed.

见 [ast.rs:63-77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L63-L77)。

#### 4.1.4 代码实践

**实践目标**：用眼睛「校验」四大特性，建立直觉。

**操作步骤**：

1. 打开 [src/ast.rs 顶部文档](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1-L78)，通读四个 `##` 小节。
2. 跳到 `Raw` 的声明与实现 [ast.rs:694-726](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L694-L726)，对照「视图 + 类型化」：`Raw` 是 `&SyntaxNode` 的包装（视图），它的 `lines()` / `lang()` / `block()` 是只有转换成 `Raw` 后才能调用的语义方法（类型化）。
3. 注意 `Raw::block()` 的实现：它去查 CST 子节点里的 `RawDelim` 长度是否 ≥3、是否有含换行的 `RawTrimmed`——这正体现了「AST 方法依赖 parser 产出的固定子结构」。

**需要观察的现象**：

- `Raw` 结构体本身没有任何字段存储「是不是块级」「语言是什么」——这些信息**完全**来自对底层 CST 子节点的即时查询，印证了「视图」特性。
- `Raw::lang()` 内部调用的是私有助手 `self.0.try_cast_first()`（见 4.2），说明语义方法也是用同一套 cast 机制去拆子节点的。

**预期结果**：你能用自己的话回答：「为什么 `Raw` 结构体里不存 `block: bool` 字段？」——因为它是 CST 的视图，所有信息都从底层节点现算，避免冗余与不一致。

#### 4.1.5 小练习与答案

**练习 1**：文档说「AST 抽象掉了反引号定界符」。那 CST 里的 `RawDelim` 节点还有用吗？被谁用？

> **答案**：有用。AST 虽然「看不见」它，但 `Raw::block()` 和 `Raw::lang()` 会回头去查 `RawDelim` 的长度（≥3 表示块级）来判断语义。CST 是无损的真相来源，定界符必须保留；AST 只是不把它直接暴露给上层。

**练习 2**：把枚举比作「图中的边」、结构体比作「图中的节点」。请用 `Markup` → `exprs()` → `Expr::Raw(Raw)` → `Raw::lines()` 这条路径验证这个比喻。

> **答案**：`Markup`（结构体/节点）通过 `exprs()` 返回 `Expr`（枚举/边），`Expr` 的某个变体指向 `Raw`（结构体/节点），`Raw` 又通过 `lines()` 返回 `Text`（结构体/节点）。结构体提供「方法返回枚举」，枚举提供「分叉到下一个结构体」，正是「边—节点」交替的图结构。

---

### 4.2 `AstNode` trait 与 `SyntaxNode::cast` / `is`

#### 4.2.1 概念说明

`AstNode` 是**所有** AST 节点共同实现的 trait，定义了 CST ↔ AST 之间的「转换契约」。它只规定三个必须实现的方法，外加一个默认方法：

| 方法 | 方向 | 作用 |
| --- | --- | --- |
| `from_untyped(node: &'a SyntaxNode) -> Option<Self>` | CST → AST | 尝试把无类型节点转成具名 AST 节点；`kind` 不匹配则返回 `None` |
| `to_untyped(self) -> &'a SyntaxNode` | AST → CST | 拿回底层 `SyntaxNode` 引用（用于查 span、文本、孩子等） |
| `placeholder() -> Self` | 兜底 | 返回一个该类型的「占位」假节点，用于结构不合法时不 panic（详见 4.4） |
| `span(self) -> Span`（默认方法） | AST → Span | 直接委托 `self.to_untyped().span()` |

真正干活的是 `from_untyped`：它是一道「类型守门员」。`node!` 宏为每个具名节点生成的 `from_untyped` 就是检查 `node.kind() == SyntaxKind::$name`。

在此之上，`SyntaxNode` 上挂了两个对外的便捷方法 `cast` 与 `is`，让调用方不必每次写 `T::from_untyped(node)`，而是用更顺口的 `node.cast::<T>()` / `node.is::<T>()`。

#### 4.2.2 核心流程

```
   调用方写： node.cast::<Raw>()
        │
        ▼
   SyntaxNode::cast  ──►  T::from_untyped(self)   // T = Raw
        │                         │
        │                         ▼
        │              node! 生成的 from_untyped：
        │              if node.kind() == SyntaxKind::Raw { Some(Raw(node)) }
        │              else { None }
        │                         │
        ▼                         ▼
   node.is::<Raw>()  ◄── cast().is_some()      Option<Raw>
```

要点：

1. `cast` 与 `from_untyped` 是同一件事的两种写法——`cast` 内部就是 `T::from_untyped(self)`（见下文源码）。
2. `is` 又是 `cast` 的布尔包装：`self.cast::<T>().is_some()`。
3. 转换**零成本**：成功时只是把同一个 `&SyntaxNode` 指针塞进新结构体（`Some(Self(node))`），没有任何拷贝或重新解析。

#### 4.2.3 源码精读

**`AstNode` trait 定义**：[ast.rs:99-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L99-L117)

```rust
/// A typed AST node.
pub trait AstNode<'a>: Sized {
    /// Convert a node into its typed variant.
    fn from_untyped(node: &'a SyntaxNode) -> Option<Self>;

    /// A reference to the underlying syntax node.
    fn to_untyped(self) -> &'a SyntaxNode;

    /// The source code location.
    fn span(self) -> Span {
        self.to_untyped().span()
    }

    /// A placeholder for this node type. ...
    fn placeholder() -> Self;
}
```

注意三处细节：trait 带生命周期 `'a`（因为包装的是 borrowed `&'a SyntaxNode`）；`span()` 是带默认实现的「赠送」方法；`placeholder()` 是 must-implement（每个类型都得会造一个假的自己）。

**`SyntaxNode::cast` / `is` 及私有助手**：[ast.rs:119-150](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L119-L150)

```rust
impl SyntaxNode {
    /// Whether the node can be cast to the given AST node.
    pub fn is<'a, T: AstNode<'a>>(&'a self) -> bool {
        self.cast::<T>().is_some()
    }

    /// Try to convert the node to a typed AST node.
    pub fn cast<'a, T: AstNode<'a>>(&'a self) -> Option<T> {
        T::from_untyped(self)
    }

    /// Find the first child that can cast to the AST type `T`.
    fn try_cast_first<'a, T: AstNode<'a>>(&'a self) -> Option<T> {
        self.children().find_map(Self::cast)
    }
    // ... try_cast_last / cast_first / cast_last 同理
}
```

可见 `cast` 就是把活儿全权委托给 `T::from_untyped`。`is` 再包一层 `is_some()`。

这里还藏着四个 **私有**助手（注意它们是 `fn` 而非 `pub fn`，仅供本模块的 AST 方法使用）：

- `try_cast_first` / `try_cast_last`：在子节点里找第一个/最后一个能 cast 成 `T` 的，找不到返回 `None`（[ast.rs:132-139](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L132-L139)）。
- `cast_first` / `cast_last`：在上者基础上，找不到就用 `T::placeholder()` 兜底（[ast.rs:141-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L141-L149)）。

正是 `cast_first` 把「找不到期望子节点」和 `placeholder()` 兜底机制串了起来（见 4.4）。

#### 4.2.4 代码实践

**实践目标**：亲手调用 `cast` / `is`，观察 `Some` / `None` 行为，理解「kind 守门」。

**操作步骤**：在仓库外新建一个最小 Rust 项目（依赖 `typst-syntax`），运行下面这段「示例代码」（**非项目原有代码**）：

```rust
// 示例代码：演示 SyntaxNode::cast / is
use typst_syntax::{Source, SyntaxKind, SyntaxNode};
use typst_syntax::ast::{AstNode, Raw, Text};

// 递归收集所有 kind == target 的 CST 节点（ast 模块没有公开的 descendants 迭代器，
// descendants() 在 node.rs 里返回的是节点计数 usize，故自行用 children() 递归）
fn collect<'a>(node: &'a SyntaxNode, target: SyntaxKind, out: &mut Vec<&'a SyntaxNode>) {
    if node.kind() == target {
        out.push(node);
    }
    for child in node.children() {
        collect(child, target, out);
    }
}

fn main() {
    let src = Source::detached("行内 `typ` 代码");
    let root = src.root();

    let mut raws = Vec::new();
    collect(root, SyntaxKind::Raw, &mut raws);
    let raw_node = raws[0];

    // 1) kind 匹配：cast 成功
    println!("is::<Raw>() = {}", raw_node.is::<Raw>());          // true
    let raw: Raw = raw_node.cast().unwrap();
    println!("block = {}", raw.block());                          // 行内 -> false
    println!("lang  = {:?}", raw.lang());                         // 行内无标签 -> None
    for line in raw.lines() {
        println!("line text = {:?}", line.get());                 // "typ"
    }

    // 2) 故意 cast 错误 kind：Raw 节点 cast 成 Text -> None
    let wrong = raw_node.cast::<Text>();
    println!("cast::<Text>() = {}", wrong.is_some());             // false
}
```

**需要观察的现象**：

- 对 `Raw` 节点调用 `cast::<Raw>()` 得到 `Some`，能继续调用 `Raw` 的语义方法；调用 `cast::<Text>()` 得到 `None`——`kind` 不同，守门员拦下。
- 转换前后是**同一个** `&SyntaxNode`：`raw.to_untyped() as *const _` 与 `raw_node as *const _` 指针相等（零成本包装）。

**预期结果**：`is::<Raw>()` 打印 `true`，`cast::<Text>()` 打印 `false`，`block` 打印 `false`，`lang` 打印 `None`，`line text` 打印 `"typ"`。（具体输出待本地验证，但上述判断依据来自源码的 `kind` 校验与 `Raw` 实现逻辑。）

#### 4.2.5 小练习与答案

**练习 1**：`node.cast::<Raw>()` 和 `Raw::from_untyped(node)` 有区别吗？

> **答案**：没有。`cast` 的函数体就是 `T::from_untyped(self)`，二者完全等价。`cast` 只是把「接收者是节点、目标是类型」的写法写得更顺口（方法链风格）。

**练习 2**：为什么 `try_cast_first` 是私有的，而 `cast` / `is` 是 `pub`？

> **答案**：`cast` / `is` 是给**外部**调用方的通用转换入口；`try_cast_first` / `cast_first` 等是 AST 节点**内部**实现语义方法时用的「找子节点」助手（如 `Raw::lang` 用它找第一个 `RawDelim`），属于实现细节，不对外暴露。

**练习 3**：`AstNode::span()` 为什么可以只给默认实现而不要求各节点重写？

> **答案**：因为所有 AST 节点都是 `&SyntaxNode` 的包装，`span()` 只需 `self.to_untyped().span()`，逻辑完全一致；而 span 真正存在底层 `SyntaxNode` 上（由 numberize 盖好，见 u6-l2），包装层无需关心。

---

### 4.3 `node!` 宏：声明类型化包装

#### 4.3.1 概念说明

`ast.rs` 里有近百个 AST 节点（`Raw`、`Text`、`Markup`、`Heading`、`LetBinding` …），每个都要做一模一样的事：声明一个 `struct Name<'a>(&'a SyntaxNode)`，再实现 `AstNode`（`from_untyped` 校验 `kind`、`to_untyped` 返还指针、`placeholder` 造假节点）。手写这些会极其重复，于是用一个 `node!` 宏批量生成。

`node!` 宏的核心约定是：**结构体名必须与某个 `SyntaxKind` 变体同名**。例如 `node! { struct Raw }` 会生成检查 `node.kind() == SyntaxKind::Raw` 的 `from_untyped`。正是这条「同名约定」让宏能用同一个名字同时引用「类型名」和「枚举变体名」。

#### 4.3.2 核心流程

宏展开以 `node! { struct Raw }` 为例，产出三块东西：

```
node! { struct Raw }
        │
        ├─▶ 1. 透明包装结构体
        │      #[repr(transparent)] pub struct Raw<'a>(&'a SyntaxNode);
        │
        ├─▶ 2. AstNode 实现
        │      from_untyped:  node.kind() == SyntaxKind::Raw ? Some(Raw(node)) : None
        │      to_untyped:    self.0
        │
        └─▶ 3. placeholder 实现
               static PLACEHOLDER = SyntaxNode::placeholder(SyntaxKind::Raw);
               Raw(&PLACEHOLDER)
```

关键点：

- `#[repr(transparent)]` 保证 `Raw<'a>` 与 `&'a SyntaxNode` 内存布局完全一致——cast 成本为零。
- `from_untyped` 是**唯一**的「类型守门」点；只要它返回 `Some`，下游方法就放心假设子结构正确。
- `placeholder()` 用一个 `static`（进程级单例）假节点，省得每次都造新的（见 4.4）。

#### 4.3.3 源码精读

**`node!` 宏定义**：[ast.rs:152-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L152-L196)

```rust
macro_rules! node {
    ($(#[$attr:meta])* struct $name:ident) => {
        #[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
        #[repr(transparent)]
        $(#[$attr])*
        pub struct $name<'a>(&'a SyntaxNode);

        impl<'a> AstNode<'a> for $name<'a> {
            #[inline]
            fn from_untyped(node: &'a SyntaxNode) -> Option<Self> {
                if node.kind() == SyntaxKind::$name {
                    Some(Self(node))
                } else {
                    Option::None
                }
            }

            #[inline]
            fn to_untyped(self) -> &'a SyntaxNode {
                self.0
            }

            #[inline]
            fn placeholder() -> Self {
                static PLACEHOLDER: SyntaxNode
                    = SyntaxNode::placeholder(SyntaxKind::$name);
                Self(&PLACEHOLDER)
            }
        }
    };
}
```

逐行看点：

- 宏参数 `$(#[$attr:meta])*` 允许在 `struct` 前挂文档注释（如 `/// Raw text ...`），这些注释会原样贴到生成的结构体上。
- `SyntaxKind::$name`：`$name` 同时充当类型名（`struct $name`）和枚举变体名（`SyntaxKind::$name`）——这是「同名约定」的精髓。
- 三处 `#[inline]`：转换极频繁，强提示编译器内联。

**宏的典型用法**：以 `Raw` 为例，[ast.rs:694-697](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L694-L697)

```rust
node! {
    /// Raw text with optional syntax highlighting: `​`...`​`.
    struct Raw
}
```

紧接着在 [ast.rs:699-726](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L699-L726) 用普通的 `impl<'a> Raw<'a> { ... }` 为它挂语义方法。也就是说，**声明靠宏、语义靠手写 impl**——这是 ast.rs 全文件的两段式节奏：`node! { struct X }` 之后常跟一个 `impl<'a> X<'a>`。

#### 4.3.4 代码实践

**实践目标**：用 `cargo expand`（或纯阅读）看清宏展开，验证「同名约定」。

**操作步骤**：

1. 若已安装 `cargo-expand`，运行（**示例命令**，待本地验证可用性）：
   ```bash
   cargo expand -p typst-syntax ast 2>/dev/null | grep -A8 "struct Raw"
   ```
2. 若没有该工具，则纯手工展开：对照 `node!` 宏定义 [ast.rs:165-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L165-L196)，把 `$name` 替换成 `Raw`，默写出展开后的 `struct Raw` 与 `impl AstNode for Raw`。
3. 在 ast.rs 里随便挑 3 个 `node! { struct X }`（如 `Text` [ast.rs:581](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L581)、`Markup` [ast.rs:226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L226)、`Heading`），确认它们的名字都能在 `SyntaxKind` 枚举里找到同名变体。

**需要观察的现象**：

- 展开后每个 `from_untyped` 都是「`if node.kind() == SyntaxKind::<同名> { Some } else { None }`」的同一个模板，只是名字不同。
- 没有 `node!` 的「手写」AST 节点（本文件里极少数非 `node!` 声明的类型，如某些枚举）不享受这套自动守门，需要自行实现 `AstNode`。

**预期结果**：你能解释「为什么新增一个 AST 节点只要写一行 `node! { struct Foo }`（前提是 `SyntaxKind::Foo` 已存在）」——因为宏自动生成了结构体、转换守门与 placeholder。

#### 4.3.5 小练习与答案

**练习 1**：如果某个 AST 节点的名字与 `SyntaxKind` 变体**不同名**（比如想叫 `RawBlock` 对应 `SyntaxKind::Raw`），`node!` 宏还能用吗？

> **答案**：不能直接用。`node!` 的 `from_untyped` 写死了 `SyntaxKind::$name`，要求类型名与变体名一致。若必须不同名，就得像 ast.rs 里个别类型那样**手写** `AstNode` 实现，在 `from_untyped` 里显式写出真正的 `SyntaxKind` 变体。这也是为什么 ast.rs 的类型名几乎都与 `SyntaxKind` 变体一一对应。

**练习 2**：宏里为什么给结构体派生 `Copy`？这会带来什么使用上的特点？

> **答案**：因为内部只是一个 `&SyntaxNode` 引用（`Copy` 的），整体也是 `Copy`。所以 AST 节点按值传递、随意复制都几乎无成本（就是一个指针拷贝）。方法签名里常见 `fn block(self)`（按值接收 `self`）而非 `&self`，正是利用了这一点。

---

### 4.4 `placeholder`：错误结构下的不 panic 兜底

#### 4.4.1 概念说明

CST 可能处于「不合法」状态——要么有语法错误，要么被 `typst-ide` 的增量编辑改坏了结构。此时某个 AST 方法期望的子节点可能**不存在**或**类型不对**。Typst 的设计目标是：**遍历这样的 CST 也绝不 panic**。

为此 ast.rs 顶部加了一组严格的 lint，直接禁止最常见的 panic 来源：

```rust
#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unreachable)]
```

见 [ast.rs:80-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L80-L85)。文档原话：*instead of panicking in that case, we return placeholder nodes.*

`placeholder()` 就是这套安全网的兜底：当一个方法找不到期望的子节点时，不 `unwrap`，而是返回一个**该类型的假节点**。这个假节点由 `SyntaxNode::placeholder(kind)` 制造——一个空的、span 为 detached 的叶子节点。

#### 4.4.2 核心流程

```
   AST 方法想取「第一个类型为 T 的子节点」
            │
            ▼
   cast_first::<T>()  =  try_cast_first().unwrap_or_else(T::placeholder)
            │                              │
            │              ┌───────────────┴────────────────┐
            ▼              ▼                                  ▼
   子节点存在且 kind 匹配     子节点缺失/不匹配
   → 返回真实的 T            → T::placeholder()
                                    │
                                    ▼
                          node! 生成的 placeholder：
                          static PLACEHOLDER = SyntaxNode::placeholder(SyntaxKind::T);
                          T(&PLACEHOLDER)
```

要点：

1. 兜底发生在 `cast_first` / `cast_last` 这类「我确信这里该有个 X 子节点」的私有助手里（[ast.rs:141-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L141-L149)）。
2. 假节点是一个进程级 `static`，所有调用共享同一份，零分配。
3. 假节点的 `kind` 是正确的（`SyntaxKind::T`），但内容为空、span 为 detached——所以后续方法在它上面查询会得到「空」「detached」之类的中性结果，而不会崩。

#### 4.4.3 源码精读

**禁止 panic 的 lint**：[ast.rs:80-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L80-L85)

```rust
// The AST should never panic when traversing a CST, even if the CST is in an
// invalid structure, ... We provide an alternative to panicking with the
// `AstNode::placeholder()` method.
#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unreachable)]
```

`#![deny(...)]` 是模块级强制：在本文件里用 `.unwrap()` / `.expect()` / `panic!()` / `unreachable!()` 会直接编译失败。这把「不 panic」从口头约定升级成了编译期硬约束。

**`AstNode::placeholder` 的宏实现**：在 `node!` 宏里（[ast.rs:189-193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L189-L193)）

```rust
fn placeholder() -> Self {
    static PLACEHOLDER: SyntaxNode
        = SyntaxNode::placeholder(SyntaxKind::$name);
    Self(&PLACEHOLDER)
}
```

注意 `static PLACEHOLDER` 定义在函数体内——Rust 允许函数局部 `static`，它仍是进程级单例，但名字被限制在函数作用域，避免上百个节点各起一个全局静态名冲突。

**底层 `SyntaxNode::placeholder`**：[node.rs:201-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L201-L213)

```rust
/// Create a dummy node of the given kind.
///
/// Panics if `kind` is [`SyntaxKind::Error`].
#[track_caller]
pub const fn placeholder(kind: SyntaxKind) -> Self {
    if kind.is_error() {
        panic!("cannot create error placeholder");
    }
    Self {
        data: Node::Leaf(EcoString::new(), kind),
        span: Span::detached(),
    }
}
```

它造的是一个**空文本叶子节点**，`kind` 由参数指定，`span` 为 `detached`。它拒绝 `Error` kind——因为占位是用来「假装成一个合法结构节点」的，而 `Error` 是另一套（错误节点，见 u5-l4）。

#### 4.4.4 代码实践

**实践目标**：触发 placeholder 兜底，观察「不 panic」且返回中性结果。

**操作步骤**：用 `SyntaxNode::placeholder` 手造一个 `Raw` 假节点，直接 cast 成 `Raw` 并调用方法。这是一段「示例代码」（**非项目原有代码**）：

```rust
// 示例代码：直接构造占位节点，验证 placeholder 兜底
use typst_syntax::{SyntaxKind, SyntaxNode};
use typst_syntax::ast::{AstNode, Raw};

fn main() {
    // 手工造一个 Raw kind 的占位节点（空文本、detached span）
    let fake: &SyntaxNode = &SyntaxNode::placeholder(SyntaxKind::Raw);

    // kind 匹配，cast 成功
    let raw: Raw = fake.cast().unwrap();

    // 在「空」假节点上调用语义方法——不 panic，返回中性结果
    println!("block = {}", raw.block());              // 无子节点 -> false
    println!("lang  = {:?}", raw.lang());             // 找不到 RawDelim -> None
    println!("lines count = {}", raw.lines().count());// 无 Text 子节点 -> 0
    println!("span   = {:?}", raw.span());            // detached
}
```

> 注意：上例用 `&SyntaxNode::placeholder(...)` 取引用仅为演示；真实路径是 `cast_first` 在找不到子节点时由 `node!` 生成的 `placeholder()` 提供同类假节点。

**需要观察的现象**：

- 即使底层节点没有任何子节点，`Raw::block()` / `lang()` / `lines()` 也**不 panic**：`lang()` 因 `try_cast_first()` 返回 `None` 而得 `None`；`block()` 因找不到 `RawDelim` 得 `false`；`lines()` 迭代出 0 项。
- span 为 `detached`——这是占位节点的标志，下游可据此识别「这不是真实源码位置」。

**预期结果**：程序正常退出，打印 `block = false`、`lang = None`、`lines count = 0`，且 `span` 显示为 detached。（具体输出待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SyntaxNode::placeholder` 要在 `kind.is_error()` 时 panic，而 ast.rs 又整体禁止 panic？

> **答案**：两者不矛盾。占位节点的用途是「假装成一个合法的结构节点」给 AST 方法用，调用方绝不会用 `Error` kind 去造占位（错误节点是独立的 `ErrorNode` 形态，见 u5-l4）。这个 panic 是**编程错误**（用错了 API）的防线，而非「遍历坏 CST」的防线——后者才是 ast.rs 那组 lint 要杜绝的。且 `placeholder` 是 `const fn`，这类误用甚至可能在编译期就被触发。

**练习 2**：`cast_first` 用 `unwrap_or_else(T::placeholder)`，而 `try_cast_first` 返回 `Option`。AST 方法作者在什么情况下该用哪个？

> **答案**：当方法确信「按语法这里**必定**有一个 X 子节点」（如 `Raw` 的定界符），用 `cast_first`，让结构异常时静默兜底，保证不 panic；当「这个子节点**可能没有**」是合法语义（如 `Raw::lang` 的可选语言标签），用 `try_cast_first`，用 `Option` 把「有无」如实传递给调用方。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「迷你 AST 探查器」任务：

**任务**：写一个小程序，解析 ` "= 标题\n行内 `rust` 代码"`，从根 `Markup` 出发，遍历 `exprs()`，对每个 `Expr` 变体打印一行 `<变体名>: <前若干字符>`，并在遇到 `Raw` 时额外打印它的 `block()` / `lang()` / 各行文本。

**要求体现的本讲知识点**：

1. **以 Markup 为根 + 惰性**：从 `src.root().cast::<Markup>()`（或 `Markup::from_untyped`）出发，调用 `exprs()` 才开始按需转换。
2. **类型化**：用 `match` 对 `Expr` 枚举分派，只有匹配到 `Expr::Raw(raw)` 才能调用 `raw.block()` 等 `Raw` 专属方法。
3. **cast / is**：对每个 CST 子节点用 `cast` 尝试转成目标类型，处理 `Some` / `None`。
4. **不 panic**：故意把输入改成有语法错误的版本（如未闭合的反引号 `` `rust ``），确认你的探查器**不会 panic**，最多遇到 `Expr::Raw` 时 `lang()` 返回 `None` 或行数为 0（placeholder 兜底或真实结构缺失）。

**参考思路**（伪代码，**示例代码**）：

```rust
use typst_syntax::{Source, SyntaxKind};
use typst_syntax::ast::{AstNode, Expr, Markup, Raw};

let src = Source::detached("= 标题\n行内 `rust` 代码");
let markup: Markup = src.root().cast().unwrap();      // 根节点 → Markup
for expr in markup.exprs() {                           // 惰性迭代
    match expr {
        Expr::Heading(h) => println!("Heading"),
        Expr::Text(t)    => println!("Text: {:?}", t.get()),
        Expr::Raw(raw) => {                            // 类型化后才可调用
            println!("Raw: block={} lang={:?}", raw.block(), raw.lang());
            for line in raw.lines() { println!("  line: {:?}", line.get()); }
        }
        other => println!("other: {:?}", std::mem::discriminant(&other)),
    }
}
```

> 提示：`Expr` 没有公开的「变体名」方法，上面用 `discriminant` 仅作占位；若想打印变体名，可自行写一个大 `match`，或借助 `Debug`（`Expr` 派生了 `Debug`）。

**验收标准**：能说清楚——为什么在 `match` 之前你拿不到 `raw.block()`（因为 `&SyntaxNode` 上没有这个方法，必须先 cast 成 `Raw`）；以及为什么输入有错时程序仍能跑完（因为 ast.rs 用 `#![deny(...)]` 禁止 panic + placeholder 兜底）。

## 6. 本讲小结

- **AST 是 CST 的类型化视图**，不是另一棵树：所有 AST 节点都是 `&SyntaxNode` 的零成本透明包装（`#[repr(transparent)]`），语义信息现算自底层 CST 子节点。
- **四大特性**：View（视图，薄包装）、Typed（类型化，cast 后才能调方法）、以 `Markup` 为根（`exprs()` → `Expr` 枚举当「边」）、Lazy（惰性，遍历到才转换）。
- **`AstNode` trait** 是 CST↔AST 转换契约：`from_untyped`（带 `kind` 守门）、`to_untyped`（返还指针）、`placeholder`（兜底），外加默认方法 `span`。
- **`SyntaxNode::cast` / `is`** 是对外的便捷转换入口：`cast` 内部就是 `T::from_untyped(self)`，`is` 是 `cast().is_some()`；私有助手 `cast_first` / `try_cast_first` 等服务于 AST 内部「找子节点」。
- **`node!` 宏**靠「结构体名 == `SyntaxKind` 变体名」的约定，批量生成包装结构体 + `AstNode` 实现 + `placeholder`；声明靠宏、语义方法靠手写 `impl`。
- **不 panic 是编译期硬约束**：`#![deny(clippy::unwrap_used, ...)]` 配合 `placeholder()` 兜底，保证遍历任何（哪怕结构损坏的）CST 都安全返回中性结果。

## 7. 下一步学习建议

- 下一讲 **u7-l2「Markup 根与 Expr 枚举」** 会深入本讲提到的「以 `Markup` 为根」：精读 `Markup::exprs` 的过滤逻辑（为何过滤语句后换行）、`Expr` 枚举如何统一三类表达式、以及 `cast_with_space` 的特殊处理。
- 之后 **u7-l3「典型 AST 节点剖析与扩展」** 会以 `Raw` / `Text` / `LetBinding` 为例，讲清「AST 方法依赖 parser 产出的固定子结构」这一假设，并演示如何为新 `SyntaxKind` 添加 AST 节点（`node!` + 手写 `impl`）。
- 想验证本讲理解，可回头对照 u5-l2（`SyntaxNode` 的 `children` / `leaf_text`）与 u6-l1（`Span`），它们是 cast 之后调用 `to_untyped().span()` / 遍历子节点的基础。
- 建议继续阅读 [ast.rs 顶部文档](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1-L78) 原文，它是整个 AST 模块设计哲学的一手说明。
