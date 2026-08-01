# 错误与警告诊断

## 1. 本讲目标

在 [u5-l1](u5-l1-syntaxnode-variants.md) 里，我们把 `SyntaxNode` 拆成了 `Leaf / Inner / Error / Warning` 四种形态，并在「综合实践」中把 `diagnosis()` 和 `errors_and_warnings()` 当成黑盒用过——但当时刻意留下了一个伏笔：这两个方法内部到底如何把 CST 里的错误和警告「汇总」出来，并没有展开。本讲就来填这个坑。

具体地，本讲要回答三个问题：

1. **存储**：解析器产生的错误与警告，在 CST 里到底以什么数据结构存放？`ErrorNode` 与 `WarningWrapper` 各自持有哪些字段，为什么 hints（提示）要带一个可选的 `SubRange`？
2. **聚合**：一个父节点如何「预先知道」自己的子树里有没有错误/警告？`Diagnosis` 这个只有两个布尔字段的小结构，为什么能支撑 O(1) 查询与增量重解析的差量更新？
3. **输出**：怎样把分散在树各处的 `ErrorNode`/`WarningWrapper` 收集成一份扁平的、可上报给用户看的 `SyntaxDiagnostic` 列表？`build_diagnostic_hints` 这个小函数在其中扮演什么角色？

学完本讲，你应该能够：

- 区分 CST 内部的诊断存储（`ErrorNode`/`WarningWrapper`）与对外的诊断表示（`SyntaxDiagnostic`）。
- 说出 `Diagnosis` 的「或聚合（`or`）」「有任何一个（`either`）」「两者都有（`both`）」三种语义分别用在哪里。
- 写出一段代码：解析含错误的文本，遍历根节点，分别收集并打印所有错误与警告的消息及 hints。

## 2. 前置知识

本讲承接 u5-l1，请确认你已经掌握：

- **`SyntaxNode` 的四种形态**：`Leaf`（文本 token）、`Inner`（带子节点的结构）、`Error`（错误节点，kind 固定为 `SyntaxKind::Error`）、`Warning`（半透明地包裹另一个节点 + 警告消息）。其中 `Warning` 没有自己的文本/子节点，访问时要靠 `node_ref()` 下钻穿过它。
- **错误与警告也是 CST 节点**：parser（见 [u4-l5](u4-l5-newline-and-error-recovery.md) 的错误恢复）不会丢弃错误，而是把 `Error` 节点留在树里以保「无损还原」；警告同理，是「贴」在已有节点上的便签。
- **`InnerNode` 缓存**：`Inner` 形态的真实载体 `InnerNode` 除 `children` 外，还缓存了 `len`、`descendants`、`diagnosis`、`upper` 四个字段，构造时就算好了。

另外需要一点 Rust 与本 crate 的基础：

- `EcoString` / `EcoVec`：`ecow` 提供的「引用计数、可廉价克隆」的字符串与向量。
- `Arc<T>` 与 `Arc::make_mut`：写时复制的智能指针（u5-l1 已解释）。
- `Span`：节点的稳定身份（8 字节），由 `numberize` 统一编号；详见 U6。
- `DiagSpan` / `SubRange` / `Spanned`：诊断用的「带定位」类型，定义在 [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs)，本讲用到时会顺带说明，完整讲解留给 U6。

本讲所有源码都来自 [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs)，少量引用 [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs)。

## 3. 本讲源码地图

| 源码位置 | 作用 |
|---|---|
| `ErrorNode`（私有结构） | 错误节点的真实载体：`text`（出错文本）、`message`（消息）、`hints`（带可选 `SubRange` 的提示） |
| `SyntaxNode::error` / `hint` / `hint_at` / `with_hints` | 构造错误节点、追加提示的公开 API |
| `WarningWrapper`（私有结构） | 警告包装层的载体：`child`（被包裹的 `Node`）、`sub_range`、`message`、`hints` |
| `SyntaxNode::warn` / `warn_at` | 在已有节点上贴警告的公开 API |
| `Diagnosis`（公开结构） | 两个布尔字段 `{ errors, warnings }`，描述「子树里有没有错误/警告」 |
| `InnerNode.diagnosis` 字段 | 构造时缓存好的聚合诊断，使查询 O(1) |
| `SyntaxDiagnostic`（公开结构） | 对外的扁平诊断：`is_error`、`span`、`message`、`hints` |
| `SyntaxNode::diagnosis` / `errors_and_warnings` | 两个对外查询入口：前者 O(1) 粗判、后者遍历收集 |
| `build_diagnostic_hints`（私有函数） | 把节点内的 hints 转成带 `DiagSpan` 的 `Spanned` 列表 |

记住一条主线：**`ErrorNode`/`WarningWrapper` 是「CST 内部存储」，`SyntaxDiagnostic` 是「对外输出」，`Diagnosis` 是「用来快速判断要不要输出」的缓存信号。**

## 4. 核心概念与源码讲解

### 4.1 ErrorNode：错误如何在 CST 中存储

#### 4.1.1 概念说明

当解析器遇到无法识别的输入（如 `#` 后面没有表达式），它不会抛异常、也不会丢掉这段文本，而是产出一个 **`Error` 节点**留在 CST 里。这个节点的真实数据载体是私有结构 `ErrorNode`，它持有三样东西：

- `text`：引发错误的源码文本（保 CST「无损还原」）。
- `message`：给用户看的错误消息（如 `"expected expression"`）。
- `hints`：附加提示列表，每条提示是一个 `(EcoString, Option<SubRange>)`——消息 + 可选的「精确指向文本中哪个子区间」。

把错误当成普通节点留在树里，是 Typst「无损 CST」哲学的延续：错误文本仍占据它原本的字节位置，下游（增量重解析、IDE）无需特殊处理。

#### 4.1.2 核心流程

错误节点的构造入口是 `SyntaxNode::error`，流程很直接：

```
error(message, text):
    ErrorNode::new(message, text)   // message 在前、text 在后（反直觉！）
    → Node::Error(Arc<ErrorNode>, SyntaxKind::Error)
    → span 初始为 detached，待 numberize 编号
```

两条关键不变量（见 [u5-l1](u5-l1-syntaxnode-variants.md) 4.2）：

1. `Error` 变体的 kind **永远**是 `SyntaxKind::Error`；`leaf`/`inner` 用 `debug_assert!(!kind.is_error())` 拒绝 Error kind，错误只能走 `error()`。
2. 因此下游可以**只凭 kind 判断**一个节点是不是错误，无需关心它的内部数据。

提示（hint）不是构造时给的，而是构造后用 `hint()` / `hint_at()` / `with_hints()` 追加，统一存进 `ErrorNode.hints`。

#### 4.1.3 源码精读

`ErrorNode` 的定义在 [src/node.rs:L920-L930](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L920-L930)：

```rust
struct ErrorNode {
    /// The source text of the node.
    text: EcoString,
    /// The error message.
    message: EcoString,
    /// Additional hints ...
    hints: EcoVec<(EcoString, Option<SubRange>)>,
}
```

构造器 `SyntaxNode::error` 在 [src/node.rs:L129-L140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L129-L140)，doc 注释特意强调了**参数顺序**——第一个是消息、第二个才是引发错误的文本：

```rust
/// Create an error node with a user-presentable message for the given
/// text. Note that the message is the first argument, and the text causing
/// the error is the second argument.
pub fn error(message: impl Into<EcoString>, text: impl Into<EcoString>) -> Self {
    Self {
        data: Node::Error(
            Arc::new(ErrorNode::new(message.into(), text.into())),
            SyntaxKind::Error,
        ),
        span: Span::detached(),
    }
}
```

追加提示的三个公开方法在 [src/node.rs:L167-L199](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L167-L199)。它们都依赖私有助手 `hints_mut`（[src/node.rs:L98-L105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L98-L105)），后者对 `Error` 与 `Warning` 两种形态都返回 hints 列表的可变引用——所以「追加提示」对错误和警告是同一套逻辑：

```rust
pub fn hint(&mut self, hint: impl Into<EcoString>) {
    let hints = self.hints_mut().expect("expected an error or warning");
    hints.push((hint.into(), None));
}
```

`hint()` 把 `SubRange` 填 `None`（提示指向整个节点），`hint_at(range, hint)` 则用 `SubRange::new(start, end)` 精确指向节点文本内的一个子区间（`hint_at` 见 [src/node.rs:L179-L189](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L179-L189)）。这种「整体定位」与「子区间定位」的区分，正是 4.5 节 `build_diagnostic_hints` 要处理的。

谁在调用这些方法？词法器和解析器。例如遇到 `##`（第二个 `#` 在 code 模式非法），词法器在 [src/lexer.rs:L904-L911](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L904-L911) 先 `self.error(...)` 再连追两条 `self.hint(...)`，最终经 `SyntaxNode::error(..).with_hints(hints)` 一次性打包成带提示的错误节点（见 [src/lexer.rs:L128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L128)）。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个带提示的错误节点，确认 `kind()` 与字段都符合预期。

**操作步骤**（可放入依赖 `typst-syntax` 的小程序，或本 crate 的测试）：

```rust
// 示例代码：构造一个带两条提示的错误节点
use typst_syntax::{SyntaxKind, SyntaxNode};

let mut err = SyntaxNode::error("the character `#` is not valid in code", "#");
err.hint("the preceding hash is causing this to parse in code mode");
err.hint("try escaping the preceding hash: `\\#`");

assert_eq!(err.kind(), SyntaxKind::Error);
assert_eq!(err.leaf_text(), "#");   // 出错文本可经 leaf_text 取回
assert_eq!(err.len(), 1);           // "#" 长度为 1
println!("{err:#?}");
```

**需要观察的现象**：

- `kind()` 返回 `SyntaxKind::Error`，符合「Error 变体 kind 恒为 Error」的不变量。
- `leaf_text()` 能取回出错文本 `"#"`（错误节点在 `node_ref` 里是 `NodeRef::Error`，`leaf_text` 返回 `err.text`，见 [src/node.rs:L247-L254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L247-L254)）。
- `Debug` 打印形如 `Error: { text: "#", message: "...", hint: "...", hint: "..." }`（见 `Debug for ErrorNode`，[src/node.rs:L949-L969](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L949-L969)）。

**预期结果**：与上面注释一致。这与 crate 内 `test_debug` 测试对 `parse("##")` 的断言（[src/node.rs:L1514-L1527](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1514-L1527)）同构。

> 说明：以上输出格式参照 `test_debug`；若本地字段顺序等细节有差异，以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`SyntaxNode::error("a", "b")` 的两个参数分别进了 `ErrorNode` 的哪个字段？为什么 doc 要特意提醒顺序？

> **答案**：第一个 `"a"` 进 `message`（给人看的消息），第二个 `"b"` 进 `text`（引发错误的文本）。提醒是因为这与「先文本后含义」的直觉相反——传反了会让用户在错误位置看到一段莫名其妙的文本，而出错文本被当成消息显示。

**练习 2**：能不能用 `SyntaxNode::leaf(SyntaxKind::Error, "x")` 造一个错误节点？

> **答案**：不能。`leaf` 里有 `debug_assert!(!kind.is_error())`（[src/node.rs:L112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L112)），传 `Error` kind 在 debug 构建会 panic。错误节点必须经 `error()` 构造，以保证它一定是 `Node::Error` 形态、且能携带 `message`/`hints`。

---

### 4.2 WarningWrapper：警告如何存储与定位

#### 4.2.1 概念说明

警告（warning）和错误有个本质区别：**错误占据自己的节点位置（如多出的 `#`），而警告是「贴在某个本来合法的节点上」的便签**。比如 `**`（两个星号）在语法上仍是一个合法的 `Strong` 节点，只是里面没有文字，Typst 想提醒你「`**` 没有额外效果」——这时就给这个 `Strong` 贴一条警告，而不破坏它的结构。

因此警告的载体 `WarningWrapper` 不是独立节点，而是**半透明包装层**（u5-l1 已建立这个概念）：

- `child: Node`：被包裹的真实节点数据。
- `message`：警告消息。
- `sub_range: Option<SubRange>`：可选的「把警告指向节点文本里的某个子区间」。
- `hints`：附加提示，结构与 `ErrorNode.hints` 完全一致。

为什么警告也需要 `sub_range`？因为有时要警告的并不是整个节点，而是节点文本里的一小段——比如块级 raw 文本的语言标签写错位置时，词法器会用 `warn_at` 把警告精确钉在那段标签上（见 [src/lexer.rs:L443-L468](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L443-L468)）。

#### 4.2.2 核心流程

贴警告用 `SyntaxNode::warn`（整体定位）或 `warn_at`（子区间定位），核心是 u5-l1 讲过的「换芯」：

```
warn(message):
    kind = self.kind()                          // 复制当前 kind
    child = replace(&mut self.data, 占位Leaf)    // 把旧 data「偷」出来当孩子
    self.data = Node::Warning(WarningWrapper{child, None, message, []}, kind)
```

`warn_at(start..end, message)` 多一步：用 `SubRange::new(start, end)` 把范围存进 `sub_range`。注意它有 `assert!(end <= self.len())`（[src/node.rs:L159](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L159)），因为 `SubRange::new` 本身不检查是否超出节点长度。

警告可以叠加（连续 `warn` 形成多层洋葱），也可以「贴在错误上」——一个 `Warning` 包裹一个 `Error`，这时 `diagnosis()` 会同时报告 `errors: true, warnings: true`（见 4.3）。

#### 4.2.3 源码精读

`WarningWrapper` 的定义与说明性注释在 [src/node.rs:L971-L991](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L971-L991)：

```rust
/// A warning message wrapped around a node in the tree.
///
/// Warnings transparently wrap another node and do not have spans or text
/// of their own. …
struct WarningWrapper {
    /// The wrapped node data.
    child: Node,
    /// A relative sub-range for targeting text not grouped by an existing span.
    sub_range: Option<SubRange>,
    /// The warning message.
    message: EcoString,
    /// Additional hints ...
    hints: EcoVec<(EcoString, Option<SubRange>)>,
}
```

`warn` 与 `warn_at` 的实现，关键就是 `std::mem::replace`「偷芯」，见 [src/node.rs:L142-L165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L142-L165)：

```rust
pub fn warn(&mut self, message: impl Into<EcoString>) {
    let kind = self.kind();
    let child = std::mem::replace(&mut self.data, Node::Leaf(EcoString::new(), kind));
    let warn = Arc::new(WarningWrapper::new(child, None, message.into()));
    self.data = Node::Warning(warn, kind);
}
```

真实使用场景：解析 `**` 时，parser 在 [src/parser.rs:L137-L151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 先正常建好 `Strong` 节点，发现它长度只有 2（两个星号、无文字）后，再对这个节点 `warn("no text within stars")` 并追加一条 hint：

```rust
p.wrap(m, SyntaxKind::Strong);
if had_closing && p[m].len() == 2 {
    p[m].warn("no text within stars");
    let hint = "using multiple consecutive stars (e.g. **) has no additional effect";
    p[m].hint(hint);
}
```

这就是「先建结构、后贴警告」的典型范式——CST 结构与诊断解耦。

#### 4.2.4 代码实践

**实践目标**：复现 parser 对 `**` 贴警告的效果，观察 Warning 如何不破坏 `kind()`/`len()`。

**操作步骤**（源码阅读 + 小验证）：

1. 阅读 [src/parser.rs:L137-L151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151) 的 `strong` 函数，确认「贴警告」发生在 `wrap` 之后。
2. 运行下面这段示例代码（依赖 `typst-syntax`）：

```rust
// 示例代码：复现 "**" 的警告
use typst_syntax::Source;

let src = Source::detached("**");
let root = src.root();
println!("{root:#?}");
// 结构遍历不受 Warning 影响：root.kind() 仍是 Markup
assert_eq!(root.kind(), typst_syntax::SyntaxKind::Markup);
```

**需要观察的现象**：`{root:#?}` 的输出里会出现一个 `Warning: { message: "no text within stars", hint: "...", Strong: 2 [...] }`——警告用集合形式 `{...}` 包住了完整的 `Strong` 子树，而根节点 `kind()` 仍是 `Markup`。

**预期结果**：与 crate 内 `test_debug` 对 `parse("**")` 的断言（[src/node.rs:L1528-L1543](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1528-L1543)）一致。

#### 4.2.5 小练习与答案

**练习 1**：对一个 `Error` 节点调用 `warn("x")`，得到的节点 `kind()` 是什么？`diagnosis()` 会报告什么？

> **答案**：`kind()` 仍是 `SyntaxKind::Error`——因为 `warn` 复制了当前 kind（即 `Error`）作为 Warning 层的 kind。`diagnosis()` 会报告 `errors: true, warnings: true`：`node_ref()` 下钻到 `Error` 给出 `errors: true`，顶层是 `Warning` 又把 `warnings` 置真（见 4.3.3）。这正是「警告可以贴在错误上」的体现。

**练习 2**：`warn_at(0..3, "msg")` 与 `warn("msg")` 的区别是什么？为什么 `warn_at` 要额外 `assert!(end <= self.len())`？

> **答案**：`warn_at` 额外存了一个 `SubRange`，把警告精确钉在节点文本的 `[0,3)` 子区间；`warn` 则让警告覆盖整个节点。`assert` 是因为 `SubRange::new` 只保证 `start < end`，不检查是否超出节点长度——超范围会让 `SubRange::to_relative` 切文本时越界，所以由 `warn_at` 这个调用方负责守边界。

---

### 4.3 Diagnosis：错误/警告的或/与聚合与缓存

#### 4.3.1 概念说明

`Diagnosis` 是一个非常小的公开结构，只有两个布尔字段：`errors: bool` 与 `warnings: bool`，回答一个问题：**「这个节点或它的子树里，有没有错误？有没有警告？」** 它本身不存消息，只存「有没有」的信号。

这个信号有两个用途：

1. **快速粗判**：`SyntaxNode::diagnosis()` 用它能 O(1) 回答「这棵子树干不干净」，避免每次都全树遍历。
2. **剪枝与增量更新**：收集诊断时跳过 `either() == false` 的子树；增量重解析（U9）替换子树后用它做差量更新。

#### 4.3.2 核心流程

`Diagnosis` 提供三种布尔运算，对应三种提问：

| 方法 | 表达式 | 语义 | 典型用途 |
|---|---|---|---|
| `either()` | \( e \lor w \) | 有错误**或**警告吗？ | 收集诊断时的剪枝条件 |
| `both()` | \( e \land w \) | 同时有错误**和**警告吗？ | 增量重解析的优化分支 |
| `or(other)` | 字段各自 \(\lor\) | 「我」与「子节点」的并集 | 自底向上聚合 |

聚合发生在 `InnerNode::new` 构造时：父节点的 `diagnosis` = 所有子节点 `diagnosis` 的字段各自 `or` 起来。由于每个子节点的 `diagnosis` 已经包含了它自己的整棵子树（递归构造），所以一次自底向上的 `or` 就让父节点「预先知道」整棵子树干不干净。这就是 `diagnosis` 能被缓存、查询 O(1) 的原因。

「或聚合」的数学含义：对子节点集合 \(\{c_1,\dots,c_n\}\)，父节点的两个字段为

\[
e_{\text{parent}} = \bigvee_{i} e_{c_i}, \qquad w_{\text{parent}} = \bigvee_{i} w_{c_i}
\]

注意是**字段各自独立**地或，不是把两个字段合并成一个。

#### 4.3.3 源码精读

`Diagnosis` 的定义与四个方法在 [src/node.rs:L871-L903](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L871-L903)：

```rust
#[derive(Debug, Clone, Copy, Default, Eq, PartialEq, Hash)]
pub struct Diagnosis {
    pub errors: bool,
    pub warnings: bool,
}

impl Diagnosis {
    /// Whether there were errors or warnings.
    pub fn either(self) -> bool { self.errors | self.warnings }
    /// Whether there were both errors and warnings.
    pub fn both(self) -> bool { self.errors & self.warnings }
    /// Apply the `OR` of both fields separately.
    pub fn or(mut self, other: Self) -> Self {
        self.errors |= other.errors;
        self.warnings |= other.warnings;
        self
    }
    fn any(slice: &[SyntaxNode]) -> Self {
        slice.iter().map(SyntaxNode::diagnosis).fold(Self::default(), Self::or)
    }
}
```

聚合发生在 `InnerNode::new`（[src/node.rs:L656-L668](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L656-L668)），`diagnosis = diagnosis.or(child.diagnosis())` 就是上面公式的代码化身：

```rust
fn new(children: Vec<SyntaxNode>) -> Self {
    let mut diagnosis = Diagnosis::default();
    for child in &children {
        // ...
        diagnosis = diagnosis.or(child.diagnosis());
    }
    Self { len, descendants, diagnosis, upper: 0, children }
}
```

对外查询 `SyntaxNode::diagnosis()` 在 [src/node.rs:L285-L301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301)。它分两步：先用 `node_ref()` 取真实形态的缓存值，再处理「顶层是 Warning」的特殊情况：

```rust
pub fn diagnosis(&self) -> Diagnosis {
    let diagnosis = match self.node_ref() {
        NodeRef::Leaf(_) => Diagnosis::default(),
        NodeRef::Inner(inner) => inner.diagnosis,           // O(1) 读缓存
        NodeRef::Error(_) => Diagnosis { errors: true, warnings: false },
    };
    match &self.data {
        Node::Warning(_, _) => Diagnosis { warnings: true, errors: diagnosis.errors },
        _ => diagnosis,
    }
}
```

读法要点：

- `Leaf` → 两者皆假（叶子不会有诊断）。
- `Inner` → 直接返回构造时算好的缓存 `inner.diagnosis`，**O(1)**。
- `Error` → `errors: true`。若它被 `Warning` 包裹，第二个 `match` 会再补上 `warnings: true`，得到「两者皆真」——呼应 4.2 练习 1。
- 顶层 `Warning` → 强制 `warnings: true`，`errors` 沿用 `node_ref` 下钻的结果。

`both()` 的用武之地在增量重解析：`replace_children` 替换一段子节点后，若新替换进来的子树「既有错误又有警告」（`replaced_diagnosis.both()`），就可以直接采用它的诊断而无需重算兄弟节点（[src/node.rs:L786](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L786)）。这是 U9 会展开的优化。

#### 4.3.4 代码实践

**实践目标**：验证「父节点 `diagnosis` 是子节点的或聚合」，并体会 O(1) 查询。

**操作步骤**（源码阅读型实践）：

1. 在 [src/node.rs:L285-L301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301) 阅读 `diagnosis()`，确认它对 `Inner` 只是 `return inner.diagnosis`（一个字段读取，没有遍历）。
2. 思考：对一个含有错误的 `Markup` 根节点，调用 `root.diagnosis()` 会遍历子树吗？

**需要观察的现象**：`diagnosis()` 对 `Inner` 分支没有任何循环或递归——它读的是构造时预算好的缓存。

**预期结果**：`root.diagnosis()` 不遍历子树，直接返回 `errors: true`（因为构造 `Markup` 时已把含错误的子节点的诊断或聚合上来了）。这就是「快速粗判」的含义：想知道一棵子树干不干净，不必先收集出全部诊断。

#### 4.3.5 小练习与答案

**练习 1**：`either()` 和 `both()` 的区别是什么？分别用布尔式写出。

> **答案**：`either()` = `errors | warnings`（\(e \lor w\)），即「有错误**或**警告」；`both()` = `errors & warnings`（\(e \land w\)），即「同时**有错误且有警告**」。`either` 用于「要不要进一步处理这棵子树」的剪枝，`both` 用于增量更新里判断「能否直接采用新子树的诊断」。

**练习 2**：为什么 `InnerNode` 要在构造时就把 `diagnosis` 算好缓存，而不是查询时现算？

> **答案**：两个原因。其一，查询频率高——`errors_and_warnings` 收集时要对每个内部节点判 `either()` 来剪枝（见 4.4），缓存让它 O(1)。其二，增量重解析（U9）会频繁替换局部子树并需要更新父节点诊断，缓存 + 差量更新（`update_parent` 里 `Diagnosis::any(&self.children)`，[src/node.rs:L857](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L857)）比每次全树重算便宜得多。

---

### 4.4 SyntaxDiagnostic：把节点汇总成可上报诊断

#### 4.4.1 概念说明

`ErrorNode` 和 `WarningWrapper` 是 CST **内部**的存储形态，散落在树的各处、且形态各异（错误是独立节点，警告是包装层）。但要给用户看、或上报给 IDE（LSP），我们需要一份**扁平的、统一的**诊断列表——这就是 `SyntaxDiagnostic`：

```rust
pub struct SyntaxDiagnostic {
    pub is_error: bool,                              // 是错误还是警告
    pub span: DiagSpan,                              // 诊断指向哪里
    pub message: EcoString,                          // 主消息
    pub hints: EcoVec<Spanned<EcoString, DiagSpan>>, // 带定位的提示
}
```

注意几个转换细节：

- `ErrorNode` 和 `WarningWrapper` 各有一个 `diagnostic(span)` 方法，把自己的字段「翻译」成一个 `SyntaxDiagnostic`：错误填 `is_error: true`，警告填 `is_error: false`。
- 警告的 `span` 会带上它的 `sub_range`（用 `DiagSpan::from_span(span, sub_range)`），从而保留「指向子区间」的精确定位；错误没有 `sub_range`，直接 `span.into()`。
- 二者的 `hints` 都经同一个私有函数 `build_diagnostic_hints` 转换（见 4.5）。

`SyntaxDiagnostic` 的 doc 还点明它的归宿（[src/node.rs:L905-L907](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L905-L907)）：它主要在求值阶段被进一步转换成 `SourceDiagnostic`——也就是说，syntax crate 只产出「语法层诊断」，语义层（typst 编译器后半段）会在其基础上再补充类型/求值错误。

#### 4.4.2 核心流程

对外收集的入口是 `SyntaxNode::errors_and_warnings()`，返回 `(Vec<SyntaxDiagnostic>, Vec<SyntaxDiagnostic>)`——第一个是错误列表，第二个是警告列表。它借助私有助手 `traverse` 做了一次「**带剪枝的中序遍历**」：

```
对树中每个节点 node：
    若是 Inner 且 inner.diagnosis.either() == true → 递归进它的 children（可能命中错误/警告）
    若是 Inner 但 either() == false                 → 剪枝，不递归（整棵子树干净）
    若是 Leaf                                       → 无事发生
    若是 Error                                      → errors.push(err.diagnostic(node.span))
    若是 Warning                                    → warnings.push(warn.diagnostic(node.span))
                                                      再下钻进 warn.child 继续循环（不放过被警告包住的错误）
```

两条要点：

1. **剪枝**：靠 4.3 的 `diagnosis.either()` 跳过干净子树，整棵树若没诊断就几乎零成本。
2. **下钻**：遇到 `Warning` 时，推入警告后**继续内层循环** `data = &warn.child`——这样「警告包着错误」时两者都能被收集，且 Warning 自身不会被重复推入。

#### 4.4.3 源码精读

`SyntaxDiagnostic` 的定义在 [src/node.rs:L905-L918](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L905-L918)。两个 `diagnostic(span)` 方法是「翻译器」——错误版在 [src/node.rs:L938-L946](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L938-L946)，警告版在 [src/node.rs:L999-L1007](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L999-L1007)，对比可见 `is_error` 与 `span` 的差异：

```rust
// ErrorNode::diagnostic
fn diagnostic(&self, span: Span) -> SyntaxDiagnostic {
    SyntaxDiagnostic {
        is_error: true,
        span: span.into(),                       // 无 sub_range
        message: self.message.clone(),
        hints: build_diagnostic_hints(span, &self.hints),
    }
}

// WarningWrapper::diagnostic
fn diagnostic(&self, span: Span) -> SyntaxDiagnostic {
    SyntaxDiagnostic {
        is_error: false,
        span: DiagSpan::from_span(span, self.sub_range),  // 带 sub_range
        message: self.message.clone(),
        hints: build_diagnostic_hints(span, &self.hints),
    }
}
```

收集逻辑 `errors_and_warnings` 在 [src/node.rs:L303-L327](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L303-L327)，关键是那个内层 `loop` 对四种形态的分派：

```rust
pub fn errors_and_warnings(&self) -> (Vec<SyntaxDiagnostic>, Vec<SyntaxDiagnostic>) {
    let mut errors = Vec::new();
    let mut warnings = Vec::new();
    self.traverse(|node| {
        let mut data = &node.data;
        loop {
            match data {
                Node::Inner(inner, _) if inner.diagnosis.either() => {
                    break inner.children.iter();        // 有诊断 → 递归
                }
                Node::Leaf(_, _) | Node::Inner(_, _) => break [].iter(), // 剪枝
                Node::Error(err, _) => {
                    errors.push(err.diagnostic(node.span));
                    break [].iter();
                }
                Node::Warning(warn, _) => {
                    warnings.push(warn.diagnostic(node.span));
                    data = &warn.child;                 // 下钻，继续 loop
                }
            }
        }
    });
    (errors, warnings)
}
```

它依赖的 `traverse`（[src/node.rs:L533-L548](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L533-L548)）是一个「中序、可剪枝」的遍历器：闭包对每个节点返回「要递归哪些子节点」，返回空切片即可剪枝——这正是上面 `break [].iter()` 的作用。

`diagnosis()` 与 `errors_and_warnings()` 是一对互补的公开 API（前者见 4.3，后者见本节）：`diagnosis()` 的 doc（[src/node.rs:L285-L290](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L290)）也明确说——可以用它先判断 `errors_and_warnings` 会不会返回空，避免无谓遍历。两者都在 [lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L25-L27) 经 `pub use` 对外公开。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：解析含错误与警告的文本，遍历根节点收集所有 `SyntaxDiagnostic`，分别打印错误与警告的消息及 hints。

**操作步骤**（依赖 `typst-syntax` 的小程序，或本 crate 的测试）：

```rust
// 示例代码：收集并分类打印诊断
use typst_syntax::Source;

fn report(text: &str) {
    let src = Source::detached(text);
    let root = src.root();

    // 1) 先 O(1) 粗判
    let d = root.diagnosis();
    println!("--- {text:?} ---  errors={} warnings={}", d.errors, d.warnings);

    // 2) 再收集扁平列表
    let (errors, warnings) = root.errors_and_warnings();
    for e in &errors {
        println!("  [ERROR] {}", e.message);
        for h in &e.hints {
            println!("           hint: {}", h.v);
        }
    }
    for w in &warnings {
        println!("  [WARN ] {}", w.message);
        for h in &w.hints {
            println!("           hint: {}", h.v);
        }
    }
}

fn main() {
    report("##");  // 词法错误：第二个 # 在 code 模式非法，带 2 条 hints
    report("**");  // 警告：星号内无文字，带 1 条 hint
}
```

**需要观察的现象**：

- `report("##")`：`diagnosis()` 报告 `errors=true`；`errors_and_warnings()` 收到 1 条错误，消息 `"the character `#` is not valid in code"`，带 2 条 hints。
- `report("**")`：`diagnosis()` 报告 `warnings=true`（`errors=false`）；收到 1 条警告，消息 `"no text within stars"`，带 1 条 hint。

**预期结果**（消息与提示文本均来自 crate 内 `test_debug`，[src/node.rs:L1514-L1543](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1514-L1543)）：

```
--- "##" ---  errors=true warnings=false
  [ERROR] the character `#` is not valid in code
           hint: the preceding hash is causing this to parse in code mode
           hint: try escaping the preceding hash: `\#`
--- "**" ---  errors=false warnings=true
  [WARN ] no text within stars
           hint: using multiple consecutive stars (e.g. **) has no additional effect
```

> 说明：以上消息文本参照 `test_debug` 与 parser/lexer 源码推导；若本地 Typst 版本调整了措辞，以本地实际输出为准（行为：`##` 产错误+提示、`**` 产警告+提示，不变）。

#### 4.4.5 小练习与答案

**练习 1**：`errors_and_warnings` 遇到 `Inner` 节点时，什么情况下会剪枝（不递归进子节点）？

> **答案**：当 `inner.diagnosis.either()` 为假时——即这棵子树既无错误也无警告。match 的守卫 `Node::Inner(inner, _) if inner.diagnosis.either()` 不成立，落到 `Node::Leaf(_, _) | Node::Inner(_, _) => break [].iter()` 分支，返回空迭代器剪枝。这让干净的大段 CST（如纯文本正文）在收集诊断时被整段跳过。

**练习 2**：为什么遇到 `Warning` 时，推入警告后还要 `data = &warn.child` 继续循环，而不是 `break`？

> **答案**：因为警告可能包裹着一个错误（「警告贴在错误上」）。如果推入警告就 `break`，被包住的错误就漏掉了。继续下钻能保证「警告 + 它包住的错误」都被收集；而 Warning 自身已经在推入时计过一次，下钻进 `child` 后走的是 child 的形态分支，不会重复推入同一条警告。

---

### 4.5 build_diagnostic_hints：提示的范围转换

#### 4.5.1 概念说明

`ErrorNode.hints` 与 `WarningWrapper.hints` 的元素类型是 `(EcoString, Option<SubRange>)`——一条消息 + 一个可选的「相对子区间」。但 `SyntaxDiagnostic.hints` 要求的元素类型是 `Spanned<EcoString, DiagSpan>`——一条消息 + 一个**诊断 span**。这二者之间的转换，就是私有函数 `build_diagnostic_hints` 的职责。

为什么需要转换？因为节点内存储的 `SubRange` 是「相对于本节点文本」的偏移，而对外诊断需要的是「绝对定位」的 `DiagSpan`。`build_diagnostic_hints` 把「父节点 span + 相对 SubRange」组合成一个带绝对定位的 `DiagSpan`；若提示没有 `SubRange`（整体定位），则生成一个 **detached（不指向任何位置）** 的 `Spanned`——提示仍然显示给用户，只是不画下划线。

#### 4.5.2 核心流程

对每条 `(message, sub_range)`：

```
有 SubRange(sr) → Spanned::new(message, DiagSpan::from_span(parent_span, Some(sr)))  // 精确指向子区间
无 SubRange     → Spanned::detached(message)                                          // 不指向具体位置
```

注意 `parent_span` 是「携带该错误/警告的那个 `SyntaxNode` 的 span」，由 `errors_and_warnings` 在调用 `err.diagnostic(node.span)` / `warn.diagnostic(node.span)` 时传入（见 4.4.3），再由 `diagnostic()` 转交给 `build_diagnostic_hints`。

#### 4.5.3 源码精读

`build_diagnostic_hints` 在 [src/node.rs:L1043-L1059](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1043-L1059)，全貌很短：

```rust
fn build_diagnostic_hints(
    parent_span: Span,
    hints: &EcoVec<(EcoString, Option<SubRange>)>,
) -> EcoVec<Spanned<EcoString, DiagSpan>> {
    hints
        .iter()
        .map(|(message, sub_range)| {
            let msg = message.clone();
            match *sub_range {
                Some(sr) => Spanned::new(msg, DiagSpan::from_span(parent_span, Some(sr))),
                None => Spanned::detached(msg),
            }
        })
        .collect()
}
```

读法要点：

- `DiagSpan::from_span(parent_span, Some(sr))` 把「父 span + 子区间」压进一个 16 字节的 `DiagSpan`（`SubRange` 的 `start`/`end` 被打包进 `extra` 字段，详见 [src/span.rs:L259-L264](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L259-L264)）。下游可用 `DiagSpan::get()` 还原出 `DiagSpanKind::Number { .., sub_range }`（[src/span.rs:L287-L318](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L287-L318)）。
- `Spanned::detached(msg)`（[src/span.rs:L396-L399](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L396-L399)）生成一个 span 为 detached 的提示——这正是用 `hint()`（无范围）追加的提示的归宿：消息会显示，但不绑定到具体字节区间。
- 这个函数被错误的 `diagnostic()` 和警告的 `diagnostic()` **共用**——再次体现「错误与警告的提示机制完全一致」。

#### 4.5.4 代码实践

**实践目标**：理解「带范围的提示」与「不带范围的提示」在最终诊断里的区别。

**操作步骤**（源码阅读型实践）：

1. 阅读 crate 内 `test_debug_sub_range`（[src/node.rs:L1546-L1596](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1546-L1596)），它对 `<unclosed` 这个错误节点先用 `hint_at(0..1, "greater")`、`hint_at(3..8, "open!")` 加了**带范围**提示，再用 `hint_at(0..9, "full text")` 在外层警告上加提示。
2. 对照 `Debug for ErrorNode`（[src/node.rs:L949-L969](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L949-L969)）：带范围的提示打印成 `hint @("选中文本"): "消息"`，不带范围的打印成 `hint: "消息"`。

**需要观察的现象**：`hint @("<")` 中的 `"<"` 正是 `SubRange(0..1)` 经 `to_relative` 从出错文本 `"<unclosed"` 里切出的子串——这印证了「`SubRange` 是相对偏移」。

**预期结果**：能解释为什么同是 `hint_at`，打印时有的带 `@("...")`、有的不带——因为前者有 `SubRange`、后者（经某些路径）可能没有；最终在 `SyntaxDiagnostic` 里，前者成为带 `DiagSpan` 的 `Spanned`，后者成为 `Spanned::detached`。

> 待本地验证：`test_debug_sub_range` 的精确打印格式可在本仓库用 `cargo test -p typst-syntax test_debug_sub_range` 运行确认。

#### 4.5.5 小练习与答案

**练习 1**：用 `hint("m")` 追加的提示，经 `build_diagnostic_hints` 后，其 `Spanned.span` 是什么？用 `hint_at(0..2, "m")` 追加的呢？

> **答案**：`hint("m")` 没有 `SubRange`，转换后是 `Spanned::detached("m")`，其 span 是 detached（不指向任何位置）。`hint_at(0..2, "m")` 有 `SubRange(0..2)`，转换后是 `Spanned::new("m", DiagSpan::from_span(parent_span, Some(SubRange(0..2))))`，指向父节点文本内的 `[0,2)` 子区间。

**练习 2**：为什么 `build_diagnostic_hints` 需要 `parent_span` 作为参数，而不能只用 `SubRange`？

> **答案**：因为 `SubRange` 只是「相对于本节点文本」的偏移，本身不携带「在哪个文件、哪个节点」的信息。必须和父节点的 `parent_span`（含 `FileId` 与节点编号）组合，才能得到一个能定位到具体源码位置的绝对 `DiagSpan`。`SubRange` 负责「节点内的相对位置」，`parent_span` 负责「节点本身的绝对身份」，缺一不可。

---

## 5. 综合实践

把本讲的「存储 → 聚合 → 输出」串起来，完成下面这个诊断分析小任务：

1. 用 `Source::detached` 解析一段同时含错误与警告的文本，例如：

   ```typst
   #if true **
   ```

   （`#if` 是合法的条件表达式开头；`**` 在其后的正文位置会被解析为空的 `Strong` 从而触发「no text within stars」警告。若你不确定该输入的确切诊断，可改用 `Source::detached("##\n- **")` 这类组合，或分别解析。）

2. 对根节点调用 `diagnosis()`，先打印 `errors`/`warnings` 两个布尔字段（4.3）。

3. 调用 `errors_and_warnings()`，按 4.4.4 的循环分别打印错误与警告的 `message` 及每条 `hint.v`（4.5）。

4. **解释**下面三个现象，把三个模块的知识用上：
   - 为什么 `diagnosis()` 是 O(1) 的？（答：读 `InnerNode.diagnosis` 缓存，构造时由子节点 `or` 聚合而来。）
   - 为什么 `errors_and_warnings()` 不会漏掉「警告包着的错误」？（答：遇 `Warning` 推入后继续 `data = &warn.child` 下钻。）
   - 为什么有的 hint 在 IDE 里会画下划线、有的不会？（答：带 `SubRange` 的经 `build_diagnostic_hints` 成为带 `DiagSpan` 的 `Spanned`，能定位；无 `SubRange` 的成为 `Spanned::detached`，只显示文字。）

**预期**：你能用一句话说清「`ErrorNode`/`WarningWrapper` 是存储、`Diagnosis` 是缓存信号、`SyntaxDiagnostic` 是输出」三者分工，并能从一段真实文本跑出分类正确的诊断列表。

> 提示：若想确保拿到「既有错误又有警告」的样本，最稳妥的是分开解析 `Source::detached("##")`（错误）与 `Source::detached("**")`（警告），分别套用上面的收集循环——这正是 4.4.4 主实践的做法。

## 6. 本讲小结

- 错误在 CST 里以 `ErrorNode`（私有）存储，持 `text`/`message`/`hints`；构造入口 `SyntaxNode::error(message, text)` 的参数顺序是「先消息后文本」，kind 恒为 `SyntaxKind::Error`。
- 警告以 `WarningWrapper`（私有）做半透明包装，持 `child`/`sub_range`/`message`/`hints`；由 `warn()`/`warn_at()` 在已有节点上「换芯」贴上，不破坏原节点的 `kind()`/`len()`。
- `Diagnosis { errors, warnings }` 是两个布尔字段的「有没有」信号：`either()`（\(e\lor w\)）用于剪枝、`both()`（\(e\land w\)）用于增量更新、`or()` 做字段各自独立的或聚合。它在 `InnerNode::new` 构造时自底向上缓存，使 `diagnosis()` 查询 O(1)。
- `SyntaxDiagnostic` 是对外的扁平诊断（`is_error`/`span`/`message`/`hints`），由 `ErrorNode::diagnostic` 与 `WarningWrapper::diagnostic` 各自翻译而来；`errors_and_warnings()` 用「带剪枝的中序遍历」收集，遇 Warning 下钻以防漏掉被包住的错误。
- `build_diagnostic_hints` 把节点内的 `(消息, Option<SubRange>)` 提示转成带 `DiagSpan` 的 `Spanned`：有 `SubRange` 则结合父 span 精确定位，无则生成 `Spanned::detached`（只显示文字、不画下划线）。

## 7. 下一步学习建议

- **U6「源码定位 Span 系统」**：本讲反复出现的 `Span`、`DiagSpan`、`SubRange`、`Spanned` 都定义在 `src/span.rs`，下一单元会完整讲解 `Span` 的 8 字节位布局、`numberize` 的编号不变量，以及 `SubRange` 如何在节点内指向子区间——这将让你彻底理解「警告的 `sub_range` 如何变成绝对位置」。
- **U9「增量重解析」**：本讲提到 `Diagnosis.both()` 与 `update_parent`/`replace_children` 配合做诊断的差量更新。学完 U9 你会看到，替换局部子树后，父节点的 `diagnosis` 缓存是如何被高效重算的。
- **阅读建议**：把 crate 内的 `test_debug`（[src/node.rs:L1487-L1544](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1544)）与 `test_debug_sub_range`（[src/node.rs:L1546-L1596](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1546-L1596)）作为「错误/警告节点及其提示」的权威样例反复对照，前者展示 `##`/`**` 的诊断结构，后者展示 `hint_at`/`warn_at` 的子区间定位效果。
