# LinkedNode 带父指针遍历

## 1. 本讲目标

本讲聚焦 `src/node.rs` 中的 `LinkedNode`。学完后你应当能够：

- 理解 `LinkedNode` 相比裸 `SyntaxNode` 多提供了什么：**父链**与**绝对字节偏移**。
- 用 `LinkedNode::new(root)` 开始一次遍历，并通过 `offset()` / `range()` / `index()` 读取节点在源文件中的位置。
- 掌握 `find_number` 如何利用「父节点编号小于子节点、兄弟从左到右递增」的编号不变量，做**二分式**的快速定位。
- 学会用 `leaf_at(cursor, Side)` 在任意字节光标处定位叶子，并区分 `Side::Before` 与 `Side::After` 的边界语义。

本讲是 CST 数据结构（U5）的收尾，同时承接 U6（Span 编号系统）中的 `numberize`，把「编号」从抽象概念落到一个可用的查询 API 上。

## 2. 前置知识

在学习本讲前，请确认你已经掌握：

- **CST 与 `SyntaxNode`**：CST 是无损的语法树，节点由 `SyntaxNode` 承载，分 Leaf / Inner / Error / Warning 四种形态（见 u5-l1、u5-l2）。
- **Span 与编号（numberize）**：Typst 不用字节范围标识节点，而是在 `numberize` 时给每个节点分配一个唯一的 48 位编号，嵌入 8 字节的 `Span` 中（见 u6-l1、u6-l2）。
- **trivia**：可跳过的零碎片段，主要是空白（`Space`）和各类注释。判定口径集中在 `SyntaxKind::is_trivia`（见 u2-l2）。

### 一个关键直觉：裸 CST 的两点不便

一个 `SyntaxNode` 只知道「自己是谁、自己多长」，并不知道自己：

1. 在整个源文件里的**绝对字节位置**是多少；
2. 自己的**父节点**和**兄弟节点**是谁。

如果你想做 IDE 的「光标处高亮」「点击跳转」或「把一个 span 反查回字节范围」，光有裸 CST 是不够的。`LinkedNode` 正是为了补上这两点而设计的——它在裸节点之外包了一层薄薄的「上下文」，记录父链与偏移，因此得名 *linked*（带链接的）node。

## 3. 本讲源码地图

本讲几乎全部内容来自 `src/node.rs`，只在解释编号不变量时少量引用 `src/span.rs` 与 `src/source.rs`。

| 文件 | 本讲用到的部分 | 作用 |
|------|----------------|------|
| `src/node.rs` | `LinkedNode`、`LinkedChildren`、`Side`、`leaf_at` 系列、`find*` 系列 | 本讲全部核心 |
| `src/span.rs` | `Span` 的编号不变量注释、`Span::number`、`SpanKind` | 解释 `find_number` 为何能快 |
| `src/source.rs` | `Source::find` / `Source::range` | 展示 `LinkedNode` 在真实反查里的用法 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **`LinkedNode`：给裸节点加上父链与绝对偏移**
2. **`LinkedChildren`：自动累加偏移的双向迭代器**
3. **`leaf_at` + `Side`：按字节光标定位叶子**
4. **`find` / `find_number` / `find_range`：用 span 编号做二分式定位**

### 4.1 LinkedNode：给裸节点加上父链与绝对偏移

#### 4.1.1 概念说明

`LinkedNode<'a>` 是一个**借用视图**：它借用了树里的某个 `&'a SyntaxNode`，并附带三块「上下文」信息。你可以把它理解成：

> 在 CST 上「点亮」一个节点，同时记住它从根走到这里的路径。

有了父链，你就能向上走（`parent()`）、找兄弟（`prev_sibling()` / `next_sibling()`）；有了绝对偏移，你就能回答「这个节点在源文件里是第几到第几个字节」。

#### 4.1.2 核心流程：四个字段

`LinkedNode` 只持有四个字段：

```text
node:   &'a SyntaxNode     // 指向树中真实节点（借用，不拥有）
parent: Option<Rc<Self>>   // 父节点（用 Rc 共享，多孩子复用同一份）
index:  usize              // 本节点在父节点的 children 数组里的下标
offset: usize              // 本节点在源文件里的绝对字节偏移
```

注意两点设计：

- **`parent` 用 `Rc<LinkedNode>`**：一个父节点会被多个孩子同时引用（每个孩子都指向同一个父），用引用计数 `Rc` 让「克隆一个 LinkedNode」变成廉价的指针递增，而不必深拷贝路径。所以 `LinkedNode` 实现了 `Clone`，但克隆很便宜。
- **生命周期 `'a` 绑在根节点的借用上**：整个 `LinkedNode` 体系都不能比根 `SyntaxNode` 活得更久。如果需要把节点拿到借用作用域之外（例如某些回调里），就要用 [`get()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1085-L1088) 取出 `&'a SyntaxNode`，而不是依赖 `Deref`（`Deref` 会缩短生命周期）。

#### 4.1.3 源码精读

结构定义与文档：[src/node.rs:1061-1077](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1061-L1077)。这段代码定义了上面四个字段。文档里有一行**特别重要的提醒**：

> **Note that all sibling and leaf accessors skip over trivia!**

意思是：后面那些「找兄弟」「找叶子」的导航型访问器（`prev_sibling`、`next_leaf` 等）会**跳过 trivia**；但 `leaf_at` 这个按光标定位的原语**不跳过 trivia**（可能返回空白叶子）。这两类行为的差别是本讲的常见陷阱，4.3 节会专门讲。

构造与基本访问器：[src/node.rs:1079-1113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1079-L1113)。要点：

- `LinkedNode::new(root)` 从根节点开始，`parent` 为 `None`、`index` 与 `offset` 都为 0。
- `range()` 返回 `offset..offset + node.len()`——这就是本节点在源文件中的字节区间，它直接用 `SyntaxNode::len()`（CST 只存字节长度，见 u5-l2）算出。

此外，`LinkedNode` 还实现了 [`Deref<Target = SyntaxNode>`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1403-L1411)，所以你可以直接在 `LinkedNode` 上调用 `SyntaxNode` 的方法，比如 `linked.kind()`、`linked.span()`、`linked.leaf_text()`、`linked.len()`——它们会自动解引用到底层节点。

向上与找兄弟的访问器：[src/node.rs:1219-1290](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1219-L1290)。以 [`prev_sibling()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1227-L1239) 为例，它从父节点的 children 数组里、本下标之前，**倒序**往前找，遇到第一个**非 trivia** 的节点就返回，同时手工累减 `offset`（因为要给那个兄弟也算出正确的绝对偏移）。注意这里遍历的是裸 children（含 trivia），但用 `is_trivia()` 过滤掉——这正是「跳过 trivia」的实现方式。

#### 4.1.4 代码实践

实践目标：亲手构造一个 `LinkedNode`，验证 `offset()` / `range()` / `index()` 的含义。

操作步骤（在仓库外新建一个最小 Rust 项目，依赖 `typst-syntax`，或在本仓库用 `cargo test -p typst-syntax` 运行你新增的测试）：

```rust
// 示例代码：可作为 typst-syntax 的一个 #[cfg(test)] 测试
use typst_syntax::{LinkedNode, Source};

let source = Source::detached("#set text(12pt)");
let root = LinkedNode::new(source.root());
println!("root kind = {:?}, range = {:?}", root.kind(), root.range());

// 走到第一个子节点，看它的 offset / index
let first = root.children().next().unwrap();
println!(
    "first child kind={:?} index={} offset={} range={:?}",
    first.kind(),
    first.index(),
    first.offset(),
    first.range(),
);
```

预期结果（待本地验证）：`root` 的 `kind` 为 `Markup`，`range` 为 `0..15`（整段文本长度）；第一个子节点是某个代码结构节点，`index` 为 0，`offset` 为 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `parent` 字段用 `Rc<LinkedNode>` 而不是 `&LinkedNode` 或 `Box<LinkedNode>`？

> 参考答案：因为同一个父节点会被它的多个孩子各自引用（每个孩子都需要指回父），用 `&` 会引发严重的借用冲突（父的生命周期与孩子们互相纠缠），用 `Box` 则每个孩子深拷贝一份路径、浪费且语义错误。`Rc` 让多个孩子**共享**同一份父节点引用，克隆廉价，且不需要借用检查器介入。

**练习 2**：`LinkedNode` 实现了 `Deref<Target = SyntaxNode>`，但文档仍单独提供了 `get()` 方法。两者何时不可互换？

> 参考答案：`Deref::deref` 返回的引用生命周期可能被缩短到当前局部借用，导致无法把节点引用传到更外层；而 `get()` 显式返回 `&'a SyntaxNode`，生命周期与根节点一致，适合需要把引用「带出去」的场景（如存入结构体、跨闭包传递）。

---

### 4.2 LinkedChildren：自动累加偏移的双向迭代器

#### 4.2.1 概念说明

要遍历一个节点的孩子，直接调 `SyntaxNode::children()` 只能得到裸的 `&SyntaxNode` 切片迭代器——**没有偏移、没有父链**。`LinkedNode::children()` 返回的 `LinkedChildren` 解决了这个问题：它在遍历时，给每个孩子都「升级」成一个完整的 `LinkedNode`，自动算好 `parent`、`index` 和 `offset`。

#### 4.2.2 核心流程

`LinkedChildren` 内部维护两个游标：

```text
front: 下一个孩子的起始字节偏移（正向遍历时用）
back:  最后一个孩子之后的字节偏移（反向遍历时用）
```

- **正向 `next()`**：取出一个孩子，把当前 `front` 作为它的 `offset`，然后 `front += node.len()`。
- **反向 `next_back()`**：先 `back -= node.len()`，再用 `back` 作为该孩子的 `offset`。

这样无论从哪个方向遍历，每个孩子得到的 `offset` 都正确（反向遍历时，靠 `back` 倒推偏移）。它还实现了 `DoubleEndedIterator` 与 `ExactSizeIterator`，所以 `.rev()`、`.len()` 都可用。

> 注意：`LinkedChildren` **不跳过 trivia**——它原样吐出每个孩子（含空白、注释）。这是「原始遍历」，与「导航型访问器跳过 trivia」是两套口径。

#### 4.2.3 源码精读

迭代器结构：[src/node.rs:1419-1429](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1419-L1429)。可以看到 `parent: Rc<LinkedNode>>` 与 `front` / `back` 两个偏移游标。

正向迭代：[src/node.rs:1431-1449](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1431-L1449)。`next()` 里把 `self.front` 赋给孩子的 `offset`，再把 `parent` 用 `Rc::clone` 共享给孩子。

反向迭代：[src/node.rs:1451-1462](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1451-L1462)。`next_back()` 先扣 `back` 再赋值，保证反向序也能算对偏移。

而 `LinkedNode::children()` 的入口：[src/node.rs:1106-1113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1106-L1113)，它把 `self` 克隆成 `Rc` 当作孩子们的 `parent`，并用本节点的 `offset` 初始化 `front`。

#### 4.2.4 代码实践

实践目标：用 `LinkedChildren` 打印一棵子树里每个孩子的 `index` 与 `range`，体会偏移的自动累加。

```rust
// 示例代码
use typst_syntax::{LinkedNode, Source};

let source = Source::detached("#set text(12pt)");
let root = LinkedNode::new(source.root());

// 递归打印前两层：每个孩子的 index、kind、range
for child in root.children() {
    println!("[{}] {:?} @ {:?}", child.index(), child.kind(), child.range());
    for grand in child.children() {
        println!("    [{}] {:?} @ {:?}", grand.index(), grand.kind(), grand.range());
    }
}
```

需要观察的现象：同一父节点下，正向遍历时孩子们的 `range.start`（即 `offset`）恰好首尾相接、单调递增，等于各自 `len()` 的累加。

预期结果（待本地验证）：根 `Markup` 下的孩子按字节顺序排列，相邻孩子的 `offset` 之差正好等于前一个孩子的 `len()`。

#### 4.2.5 小练习与答案

**练习 1**：若改用 `root.children().rev()` 反向遍历，孩子们得到的 `offset` 会与正向遍历一致吗？

> 参考答案：一致。正向用 `front` 正向累加，反向用 `back` 倒推，两种方式对**同一个孩子**算出的绝对偏移是完全相同的；区别只是**遍历顺序**反过来。

**练习 2**：`LinkedChildren` 会跳过空白和注释吗？这与 `prev_sibling()` 的行为有何不同？

> 参考答案：不会，`LinkedChildren` 原样返回每个孩子（含 trivia）。而 `prev_sibling()` / `next_sibling()` 是导航型访问器，内部用 `is_trivia()` 过滤掉 trivia，只返回「有意义的」兄弟。前者是「原始遍历」，后者是「语义导航」。

---

### 4.3 leaf_at + Side：按字节光标定位叶子

#### 4.3.1 概念说明

很多场景需要回答一个问题：**「光标在第 N 个字节时，落在哪个叶子 token 上？」**——比如 IDE 的悬停提示、补全、语法高亮刷新。`leaf_at(cursor, side)` 就是这个原语。

难点在于光标恰好在两个叶子的**边界**上时该返回谁。`Side` 枚举用两个变体给出明确的语义：

```text
Side::Before  → 倾向「光标左边」那个叶子（以该光标为结束边界的叶子）
Side::After   → 倾向「光标右边」那个叶子（以该光标为起始边界的叶子）
```

> 通俗记忆：`Before` 取「光标刚离开的」叶子，`After` 取「光标刚进入的」叶子。当光标落在叶子**中间**时，两者都返回这同一个叶子；差别只在**边界**处。

#### 4.3.2 核心流程

`leaf_at` 是一个分派函数，按 `side` 调用两个私有递归：

```text
leaf_at(cursor, Side::Before) → leaf_before(cursor)
leaf_at(cursor, Side::After)  → leaf_after(cursor)
```

两者都是「从当前节点向下钻」的递归：

- **`leaf_before(cursor)`**：若当前是叶子且 `cursor <= offset + len`（光标在本叶子的结束边界或之前），返回自己；否则在孩子里找「光标落入其范围」的那个继续钻。
- **`leaf_after(cursor)`**：若当前是叶子且 `cursor < offset + len`（光标严格在本叶子结束边界之前），返回自己；否则在孩子里找。

注意 `<=` 与 `<` 的差别：这正是 `Before` / `After` 语义差别的根源（见 4.3.5 的练习）。

> 重要：`leaf_at` 是**底层光标原语**，它**不跳过 trivia**——可能返回一个空白叶子。这与文档里「leaf accessors 跳过 trivia」的提醒看似矛盾，其实不矛盾：那条提醒针对的是 `prev_leaf` / `next_leaf` / `leftmost_leaf` / `rightmost_leaf` 这些**导航型**访问器；`leaf_at` 不在其列。

#### 4.3.3 源码精读

`Side` 枚举：[src/node.rs:1292-1297](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1292-L1297)。

`leaf_at` 分派：[src/node.rs:1367-1373](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1367-L1373)。

[`leaf_before`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1329-L1347)：叶子的判定条件是 `cursor <= self.offset + self.len()`（注意 `<=`），向下钻的条件是 `(offset < cursor && cursor <= offset + len) || (offset == cursor && i + 1 == count)`——后半句处理「光标正好压在某个孩子起点」的边界，倾向于把它判给前一个孩子（`Before` 语义）。

[`leaf_after`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1350-L1365)：叶子的判定条件是 `cursor < self.offset + self.len()`（注意 `<`），向下钻的条件是 `offset <= cursor && cursor < offset + len`，倾向于把光标判给后一个孩子（`After` 语义）。

项目自带的对照测试很能说明问题：[src/node.rs:1598-1616](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1598-L1616)。对 `"#set text(12pt, red)"`，光标在偏移 7（落在 `text` 内部），`Before` 与 `After` 都返回 `text`（因为光标在叶子中间，无歧义）。

而在 [src/node.rs:1618-1642](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1618-L1642) 中，对 `"#let x = 10"` 的偏移 9（恰好在空格 `..9` 与 `10` 的 `9..` 边界上），`Before` 返回空格（`leaf_text() == " "`），`After` 返回 `10`——这就是边界处两者的差别。

#### 4.3.4 代码实践（本讲主实践）

实践目标：用 `LinkedNode::new(root)` 遍历 `"= head <label>"`，调用 `leaf_at` 在偏移 2 处定位叶子，并用 `range()` 输出其字节范围。这是 `source.rs` 测试里真实用过的用例。

操作步骤：把下面的代码作为 `typst-syntax` 的一个测试运行（`cargo test -p typst-syntax leaf_at_practice -- --nocapture`）。

```rust
// 示例代码：本讲实践
use typst_syntax::{LinkedNode, Side, Source};

let text = "= head <label>";
//        字节: 0:'=' 1:' ' 2:'h' 3:'e' 4:'a' 5:'d' 6:' ' 7:'<' ...
let source = Source::detached(text);
let root = LinkedNode::new(source.root());

// 偏移 2 正好在「空格(1..2)」与「head(2..6)」的边界上
let before = root.leaf_at(2, Side::Before).unwrap();
let after  = root.leaf_at(2, Side::After).unwrap();

println!("Before @2 -> kind={:?} text={:?} range={:?}",
    before.kind(), before.leaf_text(), before.range());
println!("After  @2 -> kind={:?} text={:?} range={:?}",
    after.kind(), after.leaf_text(), after.range());
```

需要观察的现象：

- `Side::After` 在偏移 2 返回 `head` 叶子，`range()` 为 `2..6`（`"head"` 占 4 个字节）。
- `Side::Before` 在偏移 2 返回它左边的空白叶子，`range()` 为 `1..2`。

预期结果（与 [src/source.rs:170-171](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L170-L171) 一致）：`Side::After` 取出的叶子 span，正是 `Source::range` 反查得到 `"head"` 的那个节点。这也印证了 `leaf_at` 是「光标 → 节点」、`Source::range` 是「节点 → 字节范围」的正反呼应。

#### 4.3.5 小练习与答案

**练习 1**：`leaf_before` 用 `cursor <= offset + len`，`leaf_after` 用 `cursor < offset + len`。为什么这一个等号之差，就造成了 `Before`/`After` 的不同？

> 参考答案：当 `cursor` 恰好等于某个叶子的结束边界（`offset + len`）时，`<=` 让 `leaf_before` 认为「光标仍在该叶子范围内」从而返回它；而 `<` 让 `leaf_after` 认为「光标已离开该叶子」从而继续向右找到下一个叶子。于是同一个边界光标，`Before` 取左叶子、`After` 取右叶子。

**练习 2**：`leaf_at` 可能返回一个空白（`Space`）叶子吗？这违反文档里「leaf accessors 跳过 trivia」的提醒吗？

> 参考答案：可能。`leaf_at` 是底层光标原语，递归时不做 trivia 过滤，所以会返回空白叶子。这不违反文档提醒——那条提醒针对 `prev_leaf`/`next_leaf`/`leftmost_leaf`/`rightmost_leaf` 这些导航型访问器；`leaf_at` 不属于「跳过 trivia」的一类。需要无 trivia 的叶子时，应在 `leaf_at` 之后再链式调用 `prev_leaf()` / `next_leaf()`。

---

### 4.4 find / find_number / find_range：用 span 编号做二分式定位

#### 4.4.1 概念说明

如果说 `leaf_at` 解决的是「光标 → 节点」，那么 `find*` 系列解决的是「**span → 节点**」——给我一个 `Span`，在树里把它定位回来。这是 IDE 跳转、诊断高亮的核心能力，也是 `Source::find` / `Source::range` 的底层实现。

`find_number` 之所以能**很快**，靠的不是遍历整棵树，而是 span 编号的**两条单调不变量**（来自 `span.rs` 顶部文档，[src/span.rs:52-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L52-L61)）：

1. **父节点的编号 < 任一子节点的编号**；
2. **兄弟节点的编号从左到右递增**。

把两者合起来：对兄弟序列 `[A, B, C]`，A 及 A 的所有后代的编号都 `< B`，C 及 C 的所有后代的编号都 `> B`。这意味着给定一个目标编号，我们可以在每一层「用下一个兄弟的编号当哨兵」判断要不要进入某个孩子——本质上是一种**树上的二分式剪枝**。

#### 4.4.2 核心流程

`find` 是对外分派入口，按 `Span` 的种类分流：

```text
find(span) match span.get():
    Detached              → None（detached span 不指向任何文件）
    Number { num }        → find_number(num)   // 编号查找
    Range { start, end }  → find_range(start, end) // 字节范围查找
```

**`find_number(target)`** 的递归逻辑：

```text
1. 若本节点编号 == target，直接返回自己。
2. 若本节点是 inner 且 编号 < target（目标必在子树里）：
   a. 取孩子迭代器，并 peekable 看下一个兄弟；
   b. 对每个孩子：若「没有下一个兄弟」或「下一个兄弟的编号 > target」，
      才进入该孩子递归——因为下一个兄弟是「本孩子整个子树编号的上界」。
3. 否则返回 None。
```

关键：第 2b 步用 `children.peek()` 拿到**下一个兄弟的编号作为子树上界**，从而避免进入「目标肯定不在里面」的子树——这正是单调不变量带来的剪枝。

**`find_range(start, end)`** 的逻辑更直接：若本节点的字节区间正好等于 `[start, end)`，返回自己；否则找到**唯一一个完全覆盖** `[start, end)` 的孩子，向下钻。

#### 4.4.3 源码精读

`find` 分派：[src/node.rs:1115-1122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1115-L1122)。

`find_number`：[src/node.rs:1124-1155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1124-L1155)。注意它的文档注释明确写了依赖的两条不变量；`children.peek().is_none_or(|next| next.span().number() > target.0)` 这一行就是「用下一个兄弟当上界」的剪枝。注意它用的是 `self.children()`（`LinkedChildren`）而非裸 children，注释解释这是为了「保持身处 `LinkedNode` 上下文」（[src/node.rs:1138-1141](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1138-L1141)）。

`find_range`：[src/node.rs:1157-1169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1157-L1169)。

编号不变量的来源——`numberize`：[src/node.rs:516-531](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L516-L531)（顶层入口）与 [src/node.rs:672-719](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L672-L719)（`InnerNode::numberize`）。后者先给自己分配一个编号（取区间中点），再**把剩余区间从左到右依次切分给各孩子**——「先父后子、从左到右」的分配顺序，正是两条不变量的直接成因（详见 u6-l2）。

真实反查用法：[`Source::find`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L117-L122) 先校验 `span` 的文件 id 与本文件一致，再 `LinkedNode::new(self.root()).find(span)`；[`Source::range`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L129-L142) 则用 `find_number(num)?.range()` 把编号反查成字节范围（可叠加 `SubRange` 取子区间，见 u6-l3）。

#### 4.4.4 代码实践

实践目标：验证 `find_number` 的「正向取 span → 反查回节点」往返一致，并观察编号单调不变量。

```rust
// 示例代码
use typst_syntax::Source;

let source = Source::detached("#set text(12pt, red)");

// 1. 正向：用一个光标定位节点，记下它的 span
let leaf = typst_syntax::LinkedNode::new(source.root())
    .leaf_at(7, typst_syntax::Side::After)
    .unwrap();
let span = leaf.span();
println!("found leaf text={:?}, span.number={}", leaf.leaf_text(), span.number());

// 2. 反向：用 span 反查回节点
let back = source.find(span).unwrap();
println!("round-trip text={:?}", back.leaf_text());

// 3. 不变量：父节点编号 < 子节点编号
let parent = back.parent().unwrap();
assert!(parent.span().number() < back.span().number());
println!("parent number={} < child number={}", parent.span().number(), back.span().number());
```

需要观察的现象：

- 第 1 步与第 2 步的 `leaf_text` 一致（往返成功）。
- 第 3 步断言通过：父节点编号确实小于子节点编号。

预期结果（待本地验证）：往返一致；编号单调性断言成立。若光标定位到 `text`，则 `leaf_text()` 应为 `"text"`。

#### 4.4.5 小练习与答案

**练习 1**：`find_number` 里，为什么对每个孩子都要先 `children.peek()` 看下一个兄弟的编号，而不是直接对所有孩子都递归？

> 参考答案：因为「下一个兄弟的编号」是该孩子**整个子树编号的上界」——如果下一个兄弟的编号已经 `<= target`，那么目标必然不在（也不能在）当前孩子的子树里（否则会违反「兄弟递增」不变量）。先 peek 再决定是否递归，是在每层做剪枝，避免无谓地深入，这才让查找接近二分式而非全树遍历。

**练习 2**：`find` 对 `SpanKind::Range` 走 `find_range`，它与 `find_number` 的定位依据有何本质不同？

> 参考答案：`find_number` 依据**编号的单调性**（结构信息）做剪枝查找；`find_range` 依据**字节范围的包含关系**（几何信息），逐层找到唯一一个完全覆盖目标区间的孩子向下钻。前者依赖 `numberize` 保证的编号序，后者依赖 CST 节点的字节长度。

**练习 3**：如果某个 `Span` 是 `detached`（`Span::detached()`），`find` 会返回什么？为什么？

> 参考答案：返回 `None`。因为 `detached` span 不指向任何文件（`SpanKind::Detached`），`find` 直接返回 `None`，不会进入任何查找分支。这也是 `Source::find` 之外的一道防线。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一个**「光标 → span → 节点 → 字节范围」**的小工具：给定一段源码与一个光标位置，先用 `leaf_at` 找到光标处的叶子，取它的 `span`，再用 `Source::find` 把它反查回来，最后用 `range()` 输出字节范围，并顺带打印它的父节点与左兄弟的 kind。

```rust
// 示例代码：综合实践（可作为 typst-syntax 的测试）
use typst_syntax::{LinkedNode, Side, Source};

fn locate(source: &Source, cursor: usize, side: Side) {
    let leaf = LinkedNode::new(source.root())
        .leaf_at(cursor, side)
        .expect("no leaf at cursor");
    let span = leaf.span();

    // 反查
    let back = source.find(span).expect("span not found");
    let range = back.range();

    println!("cursor={cursor} side={side:?}");
    println!("  leaf kind={:?} text={:?}", back.kind(), back.leaf_text());
    println!("  range={range:?}");
    println!("  parent kind={:?}", back.parent_kind());
    println!("  prev sibling kind={:?}", back.prev_sibling_kind());
}

let source = Source::detached("= head <label>");
// 偏移 2 是 'h'，落在 head 叶子的起点
locate(&source, 2, Side::After);
// 再换一段，定位标签部分
let source2 = Source::detached("#set text(12pt, red)");
locate(&source2, 7, Side::After);
```

要求：

1. 解释为何 `leaf_at` 取出的叶子经 `Source::find(span)` 能精确反查回同一个节点。
2. 对照输出，验证「父节点编号 < 子节点编号」。
3. 思考：如果把 `Side::After` 换成 `Side::Before`，在偏移 2 处会得到哪个叶子？为什么？

> 思路提示：往返能成功，是因为 `leaf_at` 返回的是树里真实存在的节点，其 `span` 是 `numberize` 分配的真实编号，`find`/`find_number` 据此按单调不变量一定能定位回来。`Before` 在偏移 2 处会得到左边的空白叶子，因为 `leaf_before` 用 `cursor <= end`，把边界光标判给了「刚离开的」左叶子。

## 6. 本讲小结

- `LinkedNode` 在裸 `SyntaxNode` 之上补了**父链**（`Rc<LinkedNode>`，多孩子共享、克隆廉价）与**绝对字节偏移**，并通过 `Deref` 透传 `SyntaxNode` 的全部方法。
- `LinkedChildren` 是一个**双向 + 精确长度**迭代器，用 `front` / `back` 两个游标保证正反向遍历都能算对偏移；它**不跳过 trivia**。
- 导航型访问器（`prev_sibling` / `next_leaf` / `leftmost_leaf` / `rightmost_leaf`）**跳过 trivia**，而 `leaf_at` 是底层光标原语、**不跳过 trivia**——这是常见的易混淆点。
- `leaf_at(cursor, Side)` 中，`Before`（`<=`）取光标左边的叶子、`After`（`<`）取右边的叶子；光标在叶子中间时两者一致，差别只在边界。
- `find_number` 利用「父编号 < 子编号、兄弟递增」两条不变量，用「下一个兄弟当子树上界」做剪枝，实现近似二分式的 span 反查；`Source::find` / `Source::range` 正是它的对外封装。

## 7. 下一步学习建议

- **进入 U7（AST）**：`LinkedNode` 与 `leaf_at` 是 IDE / 语言服务的基础设施，下一单元会把 CST 的类型化视图 AST 讲透，看 `AstNode` 如何在 `LinkedNode`/`SyntaxNode` 之上再加一层语义。
- **回顾 U6**：如果你对 `find_number` 依赖的编号序还不够笃定，建议结合 u6-l2 的 `numberize` 源码再读一遍「区间中点分配 + 从左到右切分」的过程，理解不变量是如何被构造出来的。
- **延伸阅读**：`Source::find` / `Source::range`（`src/source.rs`）把本讲的 `LinkedNode` 能力暴露成对外 API；可以追踪一个真实调用方（如 IDE 的悬停/跳转），看 `leaf_at` + `find` 如何组合成完整交互。
