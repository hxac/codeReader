# deref_target —— 把任意表达式归类

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `DerefTarget` 枚举的七种变体，以及它们分别对应哪些 Typst 源码写法。
- 解释 `deref_target` 如何「沿祖先向上攀升」找到第一个可归类为 `ast::Expr` 的节点。
- 逐分支读懂 `match expr { ... }` 的归类逻辑，理解 `ast::Expr` 模式匹配是如何决定类别的。
- 明白「同一个字符串字面量，在不同父节点下会被归类成 `ImportPath` / `IncludePath` / `Code` 三种不同结果」这一关键设计。
- 对给定的源码片段和光标位置，手动推断出 `deref_target` 的返回类别，并能为它补一个测试用例。

## 2. 前置知识

本讲承接 [u2-l1 从光标到语法树节点](u2-l1-cursor-to-syntax-node.md)，默认你已经掌握：

- **字节偏移（cursor）**：光标在源码中的字节位置。
- **`LinkedNode`**：带上下文（位置、父节点、兄弟节点）的语法树节点；裸 `SyntaxNode` 没有这些信息，无法向上找祖先。
- **`leaf_at(cursor, side)`**：从 `LinkedNode::new(source.root())` 出发，下钻到光标所在的叶子节点。
- **`Side::Before` / `Side::After`**：在两个 token 交界处，决定光标归属前一个还是后一个 token。

补充一个本讲要用到的新概念：

- **`ast::Expr`（表达式）**：Typst 语法树的「带类型视图」。`SyntaxNode` 是无类型的，通过 `node.cast::<ast::Expr>()` 可以把它尝试转换成带类型的表达式枚举 `ast::Expr`（如 `Ident`、`FuncCall`、`FieldAccess`、`Str`、`Label`、`Ref` 等）。能成功转换的节点才叫「一个表达式」。这是本讲一切归类的起点。

一句话回顾定位链：

```
Source::root()  →  LinkedNode::new(...)  →  leaf_at(cursor, side)  →  某个叶子节点
                                                                  （交给本讲的 deref_target）
```

## 3. 本讲源码地图

本讲只聚焦一个文件，并顺带看它的唯一消费者：

| 文件 | 作用 |
|------|------|
| [src/matchers.rs:219-260](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L219-L260) | `deref_target` 函数本体：把任意节点归类为某个 `DerefTarget`。 |
| [src/matchers.rs:262-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L262-L281) | `DerefTarget` 枚举定义：七种可被 IDE 操作的表达式类别。 |
| [src/definition.rs:37-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L37-L83) | 「跳转定义」里消费 `DerefTarget` 的 `match`，帮助你理解每种类别 downstream 怎么用。 |
| [src/lib.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L16) | 把 `DerefTarget` / `deref_target` 通过 `pub use` 摆上公共货架。 |

> 小提示：`deref_target` 在整个 typst-ide 里目前只被 `definition.rs` 一处消费（`tooltip.rs`、`complete.rs` 都不直接用它）。但它被设计成「公共的分类层」——先归类，再让具体功能决定要处理哪些类，这正是它的价值。

---

## 4. 核心概念与源码讲解

### 4.1 DerefTarget —— 表达式的七种 IDE 可操作类别

#### 4.1.1 概念说明

IDE 功能（跳转定义、悬停提示等）面对的「光标下的东西」五花八门：有时是一个变量名，有时是一个函数调用，有时是一段 import 路径，有时是一个标签 `<fig1>` 或引用 `@fig1`。如果每个功能都自己去判断「我现在面对的是哪一种」，代码会重复且脆弱。

`DerefTarget` 就是这层**统一分类**：它把「光标下的表达式」归到七种互斥类别之一。这样下游功能只需要对类别做 `match`，而不必关心原始语法细节。

「deref（解引用）」这个名字的直觉是：**光标指着的这个表达式，最终要被「解析/定位」到哪个真正的目标**。例如 `VarAccess` 要被解析到变量的定义处，`ImportPath` 要被解析到一个文件。

#### 4.1.2 核心流程

七种类别的触发场景一览：

| 变体 | 触发场景（典型写法） | 这个类别「指向」什么 |
|------|----------------------|----------------------|
| `VarAccess` | `#x`、`#a.b`、数学里的 `x` | 一个变量或字段访问，需要查它的定义/值 |
| `Callee` | `#foo(...)`、`#set text(...)` 的括号区 | 被调用的那个函数本身（`foo` / `text`） |
| `ImportPath` | `#import "foo.typ"` 里的 `"foo.typ"` | 要导入的模块/文件 |
| `IncludePath` | `#include "foo.typ"` 里的 `"foo.typ"` | 要包含的文件 |
| `Code` | markup 里以 `#` 写出的、未被上面规则覆盖的表达式（如 `#1`、`#(1,2)`） | 一段代码表达式（供悬停求值等） |
| `Label` | `<fig1>` | 一个文档标签 |
| `Ref` | `@fig1` | 对某标签的引用 |

注意：归类结果是**互斥**的——同一个光标位置只会得到其中一种；若不属于任何一种，函数返回 `None`。

#### 4.1.3 源码精读

枚举定义在 [src/matchers.rs:262-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L262-L281)，每个变体都包着一个 `LinkedNode`，即归类后「该操作要作用的具体节点」：

```rust
pub enum DerefTarget<'a> {
    VarAccess(LinkedNode<'a>),   // 变量 / 字段访问
    Callee(LinkedNode<'a>),      // 被调用的函数
    ImportPath(LinkedNode<'a>),  // import 的路径
    IncludePath(LinkedNode<'a>), // include 的路径
    Code(LinkedNode<'a>),        // 任意代码表达式
    Label(LinkedNode<'a>),       // <label>
    Ref(LinkedNode<'a>),         // @ref
}
```

它通过 [src/lib.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L16) 对外发布：

```rust
pub use self::matchers::{DerefTarget, NamedItem, deref_target, named_items};
```

下游（如跳转定义）据此分派，见 [src/definition.rs:37-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L37-L83)：`VarAccess`/`Callee` 去查命名项或标准库；`ImportPath`/`IncludePath` 去解析模块并跳文件；`Ref` 去查引用目标；而 `Label`、`Code` 等落入 `_ => {}`（当前定义功能暂不处理它们）。这说明 `DerefTarget` 是一个「宁可分得细、由消费者按需取用」的分类层。

#### 4.1.4 代码实践

**实践目标**：建立「类别 → 写法」的直觉。

**操作步骤**：阅读上表，然后在脑海里（或纸面上）为下面五个片段各挑一个光标位置，写出你预期的类别。

1. `#foo()` —— 把光标放在 `foo` 上。
2. `#foo()` —— 把光标放在 `(` 上。
3. `#import "x.typ"` —— 把光标放在 `"x.typ"` 上。
4. `<fig1>` —— 把光标放在 `fig1` 上。
5. `@fig1` —— 把光标放在 `fig1` 上。

**需要观察的现象 / 预期结果**：

| 片段 | 光标处 | 预期类别 |
|------|--------|----------|
| `#foo()` | `foo` | `VarAccess` |
| `#foo()` | `(` | `Callee` |
| `#import "x.typ"` | `"x.typ"` | `ImportPath` |
| `<fig1>` | `fig1` | `Label` |
| `@fig1` | `fig1` | `Ref` |

> 是否如你所料？为什么 `foo` 上是 `VarAccess` 而括号区才是 `Callee`？答案在 4.3 节揭晓。运行验证见第 5 节。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DerefTarget` 要拆出 `ImportPath` 和 `IncludePath` 两个变体，而不是统一用一个 `Path`？
**答案**：因为 import 与 include 在 downstream 行为不同——import 解析得到的是一个**模块**（之后还能查 `file_id` 跳转），而 include 是把文件内容**插入**当前文档。拆成两类让消费者能精确分派（见 [src/definition.rs:65-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L65-L71)，二者当前虽共用一段，但语义边界清晰，便于将来分化）。

**练习 2**：`Callee` 变体里装的是「整个调用表达式」还是「被调用的函数名节点」？
**答案**：是**被调用的函数名节点**（更准确地说是 callee 子节点）。见 4.3 节，函数会通过 `find(call.callee().span())` 把节点定位到 callee 上，而不是整个 `FuncCall`。

---

### 4.2 deref_target —— 向上攀升到最近的 ast::Expr

#### 4.2.1 概念说明

`leaf_at` 给我们的只是一个**叶子节点**——它可能是一个标识符 `foo`，但也可能是一个孤立的小符号：`.`、`(`、`)`、`@`、`<` 等等。这些「碎片」本身不足以判断「用户想操作的表达式是什么」。

解决办法：从叶子节点开始**沿祖先链一路向上**，直到遇到第一个能被当作「一个完整表达式」(`ast::Expr`) 的节点，再用它的类型来归类。这就是 `deref_target` 的第一步，也是最关键的一步。

#### 4.2.2 核心流程

向上攀升的算法用伪代码描述：

```
输入：leaf（光标处的叶子节点）
ancestor ← leaf
循环：
    若 ancestor 能 cast 成 ast::Expr  → 跳出循环，ancestor 即「最近的表达式」
    否则 ancestor ← ancestor.parent()
    若 parent 不存在（已到根）        → 返回 None（这里没有可归类的表达式）
```

关键点：

- **包含自身**：循环条件先判断当前 `ancestor` 本身，所以如果叶子本身就是一个表达式（如 `foo` 这个 `Ident`），立刻命中、无需上溯。
- **互斥终止**：要么命中一个 `ast::Expr`，要么走到根返回 `None`——绝不会无限循环。
- **为什么必须向上**：`.`、`(` 这类碎片不是表达式；要判断它们属于哪个表达式，只能往上找包裹它们的那个表达式节点。

#### 4.2.3 源码精读

攀升循环见 [src/matchers.rs:221-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L221-L224)：

```rust
// Move to the first ancestor that is an expression.
let mut ancestor = node;
while !ancestor.is::<ast::Expr>() {
    ancestor = ancestor.parent()?.clone();
}
```

说明：

- `ancestor.is::<ast::Expr>()` 等价于「这个节点能否 `cast::<ast::Expr>()` 成功」，即它是不是某个表达式变体。
- `ancestor.parent()?`：若已到根（没有父节点），`?` 让函数直接返回 `None`——这正是「光标处没有可归类表达式」的情形（例如光标落在 `#import` 的 `import` 关键字上，攀升到 `ModuleImport` 也不是 `Expr`，再往上到 `Markup` 也不是，最终 `None`）。

攀升完成后，把 `ancestor` 转成强类型的 `ast::Expr`，交给 4.3 节的 `match`（[src/matchers.rs:227-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L227-L228)）：

```rust
let expr_node = ancestor;
let expr = expr_node.cast::<ast::Expr>()?;
```

#### 4.2.4 代码实践

**实践目标**：体会「叶子本身是表达式 vs. 需要上溯」两种情况。

**操作步骤**：对片段 `#a.b`：

1. 光标落在 `a` 上（字节偏移 1）：`leaf_at` 得到的叶子是 `Ident(a)`。
2. 光标落在 `.` 上（字节偏移 2）：`leaf_at` 得到的叶子是 `.` 这个标点。

**需要观察的现象**：
- 情况 1：`Ident` 本身就是 `Expr::Ident`，循环第一轮就命中，`ancestor = Ident(a)`。
- 情况 2：`.` 不是表达式 → 上溯到父节点 `FieldAccess` → `FieldAccess` 是表达式，命中，`ancestor = FieldAccess(a.b)`。

**预期结果**：两种光标最终都进入 `VarAccess` 分支（见 4.3），但装进 `DerefTarget::VarAccess(_)` 的节点不同——一个是 `Ident(a)`，一个是整个 `FieldAccess`。这正是「向上攀升」的意义：把碎片还原到它所属的表达式。

> 待本地验证：可在第 5 节的综合实践里加一条断言确认两种光标都返回 `VarAccess`。

#### 4.2.5 小练习与答案

**练习 1**：若光标落在 `#import` 的 `import` 关键字上，`deref_target` 返回什么？为什么？
**答案**：返回 `None`。因为 `import` 关键字节点不是 `ast::Expr`，它的父节点 `ModuleImport` 也不是 `ast::Expr`（`ModuleImport` 是语句而非表达式），继续上溯到 `Markup`、根也不是，最终 `parent()?` 触发 `None`。

**练习 2**：攀升循环里为什么用 `ancestor.parent()?.clone()` 而不是直接 `ancestor = ancestor.parent()?`？
**答案**：因为 `parent()` 返回的是 `&LinkedNode`（借用），而 `ancestor` 需要持有一个独立的 `LinkedNode` 作为循环变量（后续要 `.cast()`、`.find()` 等）。`.clone()` 把借用提升为拥有。`LinkedNode` 的克隆在 typst 里是廉价的（共享底层语法树）。

---

### 4.3 ast::Expr 模式匹配 —— 七分支归类逻辑

#### 4.3.1 概念说明

攀升拿到 `expr: ast::Expr` 后，进入一个 `match expr { ... }`，按表达式变体逐个归类。这是 `deref_target` 的「分类核心」。理解它的最好方式是**逐分支看**，并留意两个精妙之处：

1. **`Callee` 不是「整个调用」，而是定位回 callee 子节点**：对 `FuncCall` / `SetRule`，函数会再用 `find(span())` 在子树里找到被调用的那个函数名节点。
2. **同一个 `Str` 有三种命运**：取决于它的父节点是 `ModuleImport`、`ModuleInclude`，还是别的——这正好是本讲主题里强调的「同一个 Str 在不同父节点下归类不同」。

#### 4.3.2 核心流程

分支总览（顺序即源码顺序）：

```
match expr {
    Label           → DerefTarget::Label            // <fig1>
    Ref             → DerefTarget::Ref              // @fig1
    FuncCall(call)  → Callee(find(call.callee()))   // foo( ... ): 指向 foo
    SetRule(set)    → Callee(find(set.target()))    // set text(...): 指向 text
    Ident | FieldAccess | MathIdent | MathFieldAccess
                    → VarAccess                     // x / a.b / 数学 x / 数学 a.b
    Str             → 看父节点:
                        ModuleImport  → ImportPath
                        ModuleInclude → IncludePath
                        其它          → Code
    _ (兜底)        → 若 expr.hash() 或节点是 MathIdent/Error → Code
                      否则                                          → None
}
```

#### 4.3.3 源码精读

完整 `match` 见 [src/matchers.rs:229-259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L229-L259)。下面按分支解读。

**(a) Label / Ref**（[L230-L231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L230-L231)）：

```rust
ast::Expr::Label(_) => DerefTarget::Label(expr_node),
ast::Expr::Ref(_) => DerefTarget::Ref(expr_node),
```

`<fig1>` 与 `@fig1` 各自整体就是一个表达式节点，直接归类，装的就是这个节点本身。

**(b) Callee：函数调用与 set 规则**（[L233-L238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L233-L238)）：

```rust
ast::Expr::FuncCall(call) => {
    DerefTarget::Callee(expr_node.find(call.callee().span())?)
}
ast::Expr::SetRule(set) => {
    DerefTarget::Callee(expr_node.find(set.target().span())?)
}
```

注意 `expr_node` 这里是整个 `FuncCall` / `SetRule` 节点。`.find(span())` 在它的子树里按 `Span` 找到 callee/target 子节点（即 `foo` / `text`），再包成 `Callee`。**触发条件**：光标的最近表达式祖先必须是整个调用——也就是光标落在参数区/括号区（如 `(`、`)`、或某个实参的碎片上），攀升到 `FuncCall` 才会进这一分支。这也解释了 4.1.4 的悬念：光标在 `foo` 上时叶子是 `Ident`，第一轮攀升就命中 `Ident` → 走 `VarAccess`；只有光标在括号区，攀升到 `FuncCall` 才得到 `Callee`。

**(c) VarAccess：标识符与字段访问**（[L239-L242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L239-L242)）：

```rust
ast::Expr::Ident(_)
| ast::Expr::FieldAccess(_)
| ast::Expr::MathIdent(_)
| ast::Expr::MathFieldAccess(_) => DerefTarget::VarAccess(expr_node),
```

四种「变量读取」形态统一归为 `VarAccess`：普通标识符 `x`、字段访问 `a.b`、数学标识符、数学字段访问。这一类是跳转定义/补全最常打交道的对象。

**(d) Str：同一个字符串的三种命运**（[L243-L252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L243-L252)）——本讲的点睛之笔：

```rust
ast::Expr::Str(_) => {
    let parent = expr_node.parent()?;
    if parent.kind() == SyntaxKind::ModuleImport {
        DerefTarget::ImportPath(expr_node)
    } else if parent.kind() == SyntaxKind::ModuleInclude {
        DerefTarget::IncludePath(expr_node)
    } else {
        DerefTarget::Code(expr_node)
    }
}
```

一个字符串字面量 `"foo.typ"`，单看 `ast::Expr` 它就是 `Str`，无法区分用途；必须**看它的父节点**：

- 父节点是 `ModuleImport`（出现在 `#import "foo.typ"`）→ `ImportPath`。
- 父节点是 `ModuleInclude`（出现在 `#include "foo.typ"`）→ `IncludePath`。
- 其它（如 `#let s = "foo.typ"` 或代码块里的字符串）→ `Code`。

这就是「**相同的 AST 节点，因上下文不同而归类不同**」——`Str` 的 `ast::Expr` 形态完全一样，是父节点决定了它的 IDE 语义。

**(e) 兜底分支**（[L253-L258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L253-L258)）：

```rust
_ if expr.hash()
    || matches!(expr_node.kind(), SyntaxKind::MathIdent | SyntaxKind::Error) =>
{
    DerefTarget::Code(expr_node)
}
_ => return None,
```

未被前面命中的表达式（如 `Func`、`Array`、`Dict`、`Parenthesized`、各种字面量 `Int/Float/Bool/None` 等）来到这里：

- `expr.hash()` 为真（即该表达式是以 `#` 写在 markup 中的「带哈希前缀」表达式，如 `#1`、`#none`、`#(1, 2)`），或节点本身是 `MathIdent` / `Error` → 归为 `Code`，供悬停等进一步处理。
- 否则 → 返回 `None`，表示该处不属于任何可操作的类别。

> 设计直觉：在 markup 里以 `#` 显式引入的表达式，对 IDE 是「值得一看的代码」；而纯代码块内部那些未被特殊规则覆盖的字面量（如 `{ 1 }` 里的 `1`），当前则不归类。`MathIdent` / `Error` 的额外放宽是为了在数学环境和语法错误下仍尽量给出可用结果（best-effort）。

#### 4.3.4 代码实践

**实践目标**：对五个典型片段手动推断 `deref_target` 类别，再与源码逻辑对照。

**操作步骤**：逐个片段，先确定光标处叶子，再想清楚「攀升到哪个表达式祖先」，最后落到哪个 `match` 分支。

**需要观察的现象 / 预期结果**：

| 片段 | 光标（字节偏移，`Side::After`） | 叶子 | 攀升到的表达式 | 分支 | 类别 |
|------|--------------------------------|------|----------------|------|------|
| `#foo()` | 2（`o` 上） | `Ident(foo)` | `Ident` | (c) | `VarAccess` |
| `#foo()` | 4（`(` 上） | `(` 标点 | `FuncCall` | (b) | `Callee` |
| `#import "x"` | 9（`x` 上） | `Str("x")` | `Str`，父=`ModuleImport` | (d) | `ImportPath` |
| `<label>` | 3（`b` 上） | `Label` | `Label` | (a) | `Label` |
| `@ref` | 2（`e` 上） | `Ref` | `Ref` | (a) | `Ref` |

补充一个对照：`#a.b` 光标在 `.`（偏移 2）上 → 叶子是 `.` → 攀升到 `FieldAccess` → 分支 (c) → `VarAccess`。

**预期结果**：与源码逻辑一致。`#import "x"` 正是 4.3.3 (d) 的直接体现：`Str` + 父节点 `ModuleImport` ⇒ `ImportPath`。

> 想真正跑起来验证？把这些断言写成测试，见第 5 节综合实践（构建/运行待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`#set text(size: 12pt)` 中，把光标放在 `size` 上 vs. 放在 `text` 后的 `(` 上，分别得到什么类别？
**答案**：
- 光标在 `size` 上：叶子 `Ident(size)` → 攀升到 `Ident`（第一轮命中）→ 分支 (c) `VarAccess`。（`size` 是 set 规则里的具名参数键名。）
- 光标在 `(` 上：叶子是 `(` → 攀升：父 `Args` 不是 `Expr`，再上溯到 `SetRule` 是 `Expr` → 分支 (b) `Callee`，并通过 `find(set.target().span())` 指向 `text`。

**练习 2**：`#let s = "x.typ"` 中光标在 `"x.typ"` 上，类别是什么？为什么和 `#import "x.typ"` 不同？
**答案**：`Code`。因为这里的 `Str` 其父节点是 `LetBinding`（更准确地说是 `=` 右边的表达式上下文），既不是 `ModuleImport` 也不是 `ModuleInclude`，落到 (d) 的 `else` 分支 → `Code`。同样的 `Str` 节点，因父节点不同而归类不同——这就是 (d) 段的核心设计。

**练习 3**：兜底分支里为什么要单独列出 `SyntaxKind::MathIdent`？前面 (c) 不是已经处理了 `MathIdent` 吗？
**答案**：正常的 `MathIdent` 确实在 (c) 被归为 `VarAccess`，不会到达兜底。兜底里的 `MathIdent` / `Error` 是一种**防御性放宽**：当表达式因为某些边界情况（例如解析为 `Error` 节点、或在数学环境下节点形态异常）没有命中前面的变体时，仍尽量把它当作 `Code` 给出可用结果，而不是直接 `None`。这体现了 typst-ide 一贯的 best-effort 风格。

---

## 5. 综合实践

**任务**：为 `deref_target` 新增一个测试，验证本讲推断的类别表。把它加进 `src/matchers.rs` 末尾已有的 `#[cfg(test)] mod tests` 模块里，复用该模块现有的 `WorldLike` / `FilePos` 解析套路。

**实践目标**：把「手动推断」变成「可执行断言」，并加深对攀升 + 分类的理解。

**操作步骤**：

1. 打开 [src/matchers.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs) 的 `tests` 模块，注意顶部已有的 `use std::borrow::Borrow;` 和 `use crate::tests::{FilePos, TestWorld, WorldLike};`。
2. 在模块里新增下面这个测试（它仿照同文件 `test` 辅助函数的写法，把 `deref_target` 的结果转成类别名字符串，便于断言）：

```rust
// 示例代码：建议加入 src/matchers.rs 的 tests 模块
#[test]
fn test_deref_target_classes() {
    use typst_syntax::Side;

    // 复用 tests 模块已有的 WorldLike / FilePos 解析套路，
    // 把 deref_target 的结果转成一个类别名，便于断言。
    fn classify(world: impl WorldLike, pos: impl FilePos) -> Option<&'static str> {
        let world = world.acquire();
        let world = world.borrow();
        let (source, cursor) = pos.resolve(world);
        let leaf = typst_syntax::LinkedNode::new(source.root())
            .leaf_at(cursor, Side::After)?;
        match crate::deref_target(leaf)? {
            crate::DerefTarget::VarAccess(_) => Some("VarAccess"),
            crate::DerefTarget::Callee(_) => Some("Callee"),
            crate::DerefTarget::ImportPath(_) => Some("ImportPath"),
            crate::DerefTarget::IncludePath(_) => Some("IncludePath"),
            crate::DerefTarget::Code(_) => Some("Code"),
            crate::DerefTarget::Label(_) => Some("Label"),
            crate::DerefTarget::Ref(_) => Some("Ref"),
        }
    }

    // (片段, 光标字节偏移) → 期望类别
    assert_eq!(classify("#foo()", 2), Some("VarAccess"));     // 光标在 foo 上
    assert_eq!(classify("#foo()", 4), Some("Callee"));        // 光标在 ( 上
    assert_eq!(classify("#import \"x\"", 9), Some("ImportPath")); // 光标在 "x" 上
    assert_eq!(classify("<label>", 3), Some("Label"));        // 光标在 label 上
    assert_eq!(classify("@ref", 2), Some("Ref"));             // 光标在 ref 上
    assert_eq!(classify("#a.b", 2), Some("VarAccess"));       // 光标在 . 上 → FieldAccess
    assert_eq!(classify("#let s = \"x.typ\"", 11), Some("Code")); // 普通 Str → Code
    assert_eq!(classify("#import", 3), None);                 // 光标在 import 关键字上 → None
}
```

3. 运行测试（在 typst-ide crate 下）：

```bash
cargo test -p typst-ide test_deref_target_classes
```

**需要观察的现象 / 预期结果**：所有断言通过。特别注意最后两条——`#let s = "x.typ"` 的普通字符串归为 `Code`（印证 4.3.3 (d) 的「父节点非 import/include」），而 `#import` 关键字上的光标返回 `None`（印证 4.2 攀升到根也无 `Expr`）。

> 待本地验证：本教程运行在受限的 typst-ide 子目录内，无法直接执行 `cargo test`（`typst-ide` 通过路径依赖 `typst` 等同级 crate，需要完整 monorepo 才能构建）。请在完整的 typst 仓库里运行上述命令验证断言。

**延伸思考**：把 `classify("#foo()", 4)` 里的光标从 4 改成 2 失败、从 2 改成 4 也失败——这正好说明「光标位置一变，类别可能从 `VarAccess` 跳到 `Callee`」。再试着把光标放到 `#foo()` 的 `)` 上（偏移 5），猜猜是 `Callee` 还是 `VarAccess`，然后用断言验证你的猜测。

## 6. 本讲小结

- `DerefTarget` 是 typst-ide 的**统一表达式分类层**：把光标下的表达式归到 `VarAccess` / `Callee` / `ImportPath` / `IncludePath` / `Code` / `Label` / `Ref` 七类之一，或返回 `None`。
- `deref_target` 分两步：先**沿祖先向上攀升**到第一个能 `cast::<ast::Expr>()` 的节点（[L221-L224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L221-L224)），再对 `ast::Expr` 做**模式匹配**归类（[L229-L259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L229-L259)）。
- `Callee` 不是整个调用，而是用 `find(callee().span())` **定位回被调用的函数名节点**；光标在 `foo` 上是 `VarAccess`，只有光标在括号区攀升到 `FuncCall` 才得到 `Callee`。
- **同一个 `Str` 因父节点不同而有三种归类**：父为 `ModuleImport` → `ImportPath`，父为 `ModuleInclude` → `IncludePath`，否则 → `Code`。这是「AST 节点相同、上下文决定语义」的典型设计。
- 兜底分支用 `expr.hash()`（`#` 前缀表达式）与 `MathIdent` / `Error` 把剩余可处理情况归为 `Code`，其余返回 `None`，体现 best-effort 哲学。
- 目前 `deref_target` 仅被 `definition.rs` 消费，但它被设计成公共分类层——消费者按需挑选要处理的类别（如定义功能暂不处理 `Label` / `Code`）。

## 7. 下一步学习建议

- **下一讲 [u2-l3 named_items —— 收集作用域内可见命名项](u2-l3-named-items.md)**：`deref_target` 只负责「光标处是什么类别」，而 `named_items`（同文件 [src/matchers.rs:9-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L9-L176)）负责「光标处能看到哪些名字」。两者配合，正是跳转定义 `VarAccess` 分支的查询基础。
- **之后看 [u2-l4 analyze —— 推断表达式可能的值](u2-l4-analyze-values.md)**：`deref_target` 把节点分类后，`analyze_expr` 会对 `VarAccess` / `Code` 这类节点真正求值。
- **想看归类结果如何被消费**：直接读 [src/definition.rs:37-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L37-L83)，它对 `VarAccess/Callee`、`ImportPath/IncludePath`、`Ref` 的分派是理解 `DerefTarget` 设计意图的最佳注脚。
