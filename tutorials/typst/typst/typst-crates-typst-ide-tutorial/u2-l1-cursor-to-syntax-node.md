# 从光标到语法树节点

## 1. 本讲目标

所有 IDE 功能（悬停提示、跳转定义、自动补全……）的第一步都是同一个问题：**用户的光标落在哪个语法树节点上？** 只有先回答这个问题，后续逻辑才有意义。

本讲学完后，你应当能够：

1. 说出 `Source::root()`、`LinkedNode`、`leaf_at`、`Side` 各自的作用与它们之间的协作关系。
2. 理解 `LinkedNode` 为什么要在 `SyntaxNode` 之外再包一层（偏移量、父节点、兄弟节点的上下文）。
3. 用 `leaf_at(cursor, side)` 把一个字节偏移映射到语法树的叶子节点。
4. 解释 `Side::Before` 与 `Side::After` 在两个 token 交界处的歧义消解规则。
5. 明白 `leaf_at` 本身不跳过 trivia（空白、注释），以及调用方如何处理这种情况。

本讲是第二单元（语法树定位与表达式分析）的地基，后续的 `deref_target`、`named_items`、`tooltip`、`definition` 都建立在本讲的「光标 → 节点」映射之上。

## 2. 前置知识

在进入源码前，先用通俗语言理清几个概念。

### 光标是一个「字节偏移」

在编辑器里，光标看起来停在某个字符上。但在底层，Typst 把源码当成一串字节（UTF-8），光标的位置用**字节偏移（byte offset）**表示：从源码开头数到第几个字节。例如源码 `"#let x = 1"`：

```
字节偏移: 0   1   2   3   4   5   6   7   8   9   10
字符:     #   l   e   t       x       =       1
```

偏移 `0` 是文件最开头（`#` 之前），偏移 `1` 正好在 `#` 和 `l` 之间。注意偏移指的是「字符之间的间隙」，可以取到字符串长度（这里是 `10`）。

### 语法树是一棵「节点树」

Typst 把源码解析成一棵语法树（syntax tree），由若干 `SyntaxNode` 组成。每个节点要么是**叶子节点**（leaf，对应一个具体的 token，如 `#`、`let`、`x`），要么是**内部节点**（inner，把若干子节点组合起来，如一整个 `let` 绑定语句）。

一棵 `#let x = 1` 的语法树大致长这样（简化）：

```
Markup（根）
└─ Hash "#"
└─ LetBinding
   ├─ Let "let"
   ├─ Space " "
   ├─ Ident "x"
   ├─ Space " "
   ├─ Eq "="
   ├─ Space " "
   └─ Int "1"
```

### 为什么需要「带上下文」的节点

一个裸的 `SyntaxNode` 只知道自己是什么、文本多长，但**不知道自己在文件里的字节偏移，也不知道自己的父节点和兄弟节点是谁**。而 IDE 功能（比如「向上找祖先表达式」「找前一个兄弟节点」）恰恰需要这些上下文。这就是 `LinkedNode` 存在的原因——我们在第 4.2 节详细讲。

### trivia：被解析器忽略的噪声节点

源码里的空白、换行、注释等，解析器会把它们也建成节点（`Space`、`LineComment` 等），但它们对语义没有影响，称为 **trivia**。处理光标时，trivia 是个需要特别对待的东西（第 4.4 节）。

## 3. 本讲源码地图

本讲涉及的源码分两类：typst-ide 自己的调用方，以及它依赖的 typst-syntax 里的底层实现。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-syntax/src/source.rs` | `Source::root()`：取出源码的语法树根节点（入口）。 |
| `crates/typst-syntax/src/node.rs` | `LinkedNode` 结构体、`Side` 枚举、`leaf_at` / `leaf_before` / `leaf_after` 的全部实现（本讲核心）。 |
| `crates/typst-syntax/src/kind.rs` | `SyntaxKind::is_trivia()`：判定一个节点是不是 trivia。 |
| `crates/typst-ide/src/definition.rs` | `definition()`：跳转定义，展示了 `LinkedNode::new` + `leaf_at` 的标准两行用法。 |
| `crates/typst-ide/src/tooltip.rs` | `tooltip()`：悬停提示，展示了 `leaf_at` 之后立即检查 `is_trivia()` 的模式。 |
| `crates/typst-ide/src/matchers.rs` | 测试辅助函数 `test()`：用 `Side::After` 演示了把光标变成叶子节点的完整写法。 |

> 说明：本讲的底层定义来自 `typst-syntax`，因此永久链接会指向 `crates/typst-syntax/...`；调用示例来自 `typst-ide`，链接指向 `crates/typst-ide/...`。

## 4. 核心概念与源码讲解

### 4.1 Source::root —— 拿到语法树的根

#### 4.1.1 概念说明

IDE 功能的输入通常是一个 `Source`（代表一个已解析的源码文件）加一个光标偏移。要做任何节点定位，第一步都必须先拿到这棵语法树的**根节点**。`Source::root()` 就是干这件事的：它返回文件「无类型语法树（untyped syntax tree）」的根 `SyntaxNode`。

之所以强调「无类型」，是因为 Typst 的语法树分两层：底层的 `SyntaxNode` 只描述结构（哪些 token、怎么嵌套），不带具体语义；上层的 `ast::Expr` 等才是带类型的视图。光标定位这一步只关心结构，所以用底层的 `SyntaxNode` 就够了。

#### 4.1.2 核心流程

```
Source（一个已解析文件）
   │
   │  source.root()
   ▼
&SyntaxNode（语法树根节点，通常是 Markup）
   │
   │  LinkedNode::new(...)            ← 第 4.2 节
   ▼
LinkedNode（带上下文的根节点）
   │
   │  .leaf_at(cursor, side)          ← 第 4.3 节
   ▼
Option<LinkedNode>（光标所在的那片叶子）
```

#### 4.1.3 源码精读

`Source::root()` 的实现极其简单——它只是返回内部存储的根节点引用：

[crates/typst-syntax/src/source.rs:57-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L57-L60) — 取出文件的无类型语法树根节点，类型是 `&SyntaxNode`。

这个 `&SyntaxNode` 就是后续所有遍历的起点。

#### 4.1.4 代码实践

**实践目标**：亲手拿到一个 `Source` 的根节点，确认它的种类。

**操作步骤**：在 typst-ide 的测试模块里加一个临时测试（测试基础设施 `TestWorld` 在 `u1-l3` 已介绍）：

```rust
#[test]
fn explore_root() {
    use typst::syntax::Source;
    use crate::tests::{FilePos, TestWorld, WorldLike};

    let owned = TestWorld::new("#let x = 1");
    let world = owned.acquire();
    let world = world.borrow();
    let (source, _) = 0isize.resolve(world);

    let root = source.root();
    println!("root kind = {:?}", root.kind());
}
```

**需要观察的现象**：打印出的根节点 kind。

**预期结果**：根节点的 kind 应为 `Markup`（顶层 markup 模式）。具体枚举名以本地运行为准（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用一个全局的「当前文件」对象，而要把 `Source` 作为参数传进每个 IDE 函数？

**参考答案**：因为 IDE 要同时处理多个打开的文件（主文件、被 import 的文件等），且每次调用可能针对不同文件的不同光标。把 `Source` 作为显式参数，能让函数无副作用、可测试，也方便复用（比如跨文件跳转时切换到目标文件的 `Source`）。

---

### 4.2 LinkedNode —— 带上下文的语法节点

#### 4.2.1 概念说明

如前置知识所说，裸的 `SyntaxNode` 不带「我在文件里的偏移」「我的父节点是谁」这些信息。而 IDE 需要频繁做这类操作：

- 「这个节点在源码里的字节范围是多少？」（用于高亮、跳转）
- 「它的父节点是谁？再往上的祖先是哪个表达式？」（`deref_target` 会用）
- 「它前一个/后一个兄弟节点是什么？」（`named_items` 会用）

`LinkedNode` 就是给 `SyntaxNode` 套上一层**上下文外壳**：它额外保存了父节点的引用、自己在父节点 children 数组里的索引、以及自己在源码里的字节偏移。一旦拿到一个 `LinkedNode`，就能随时向上、向下、向兄弟节点移动。

一个重要提示，直接写在 `LinkedNode` 的文档注释里：**所有「兄弟」和「叶子」导航类访问器（如 `prev_sibling`、`next_sibling`、`prev_leaf` 等）都会跳过 trivia**。但注意：`leaf_at` 不在此列——它可以返回 trivia 节点（第 4.4 节细讲）。

#### 4.2.2 核心流程

`LinkedNode` 内部存了四个字段：

| 字段 | 含义 |
| --- | --- |
| `node: &SyntaxNode` | 被包裹的底层节点 |
| `parent: Option<Rc<Self>>` | 父节点（根节点的 parent 为 `None`） |
| `index: usize` | 在父节点 children 数组里的下标 |
| `offset: usize` | 本节点在源码里的字节偏移 |

构造一个 `LinkedNode` 的唯一公开入口是 `LinkedNode::new(root)`：从根节点开始，偏移设为 `0`、无父节点、索引为 `0`。之后再通过 `children()`、`leaf_at()` 等方法向下遍历时，每个子 `LinkedNode` 会自动算好自己的偏移和 parent 链。

#### 4.2.3 源码精读

`LinkedNode` 的结构定义与「跳过 trivia」的关键文档注释：

[crates/typst-syntax/src/node.rs:1061-1077](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1061-L1077) — `LinkedNode` 在 `SyntaxNode` 之外额外保存 parent、index、offset，提供上下文；注释强调兄弟/叶子导航器会跳过 trivia。

`LinkedNode::new` 是唯一的构造入口：

[crates/typst-syntax/src/node.rs:1080-1083](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1080-L1083) — 从根节点构造 `LinkedNode`，偏移为 0、parent 为 `None`、index 为 0。

配合 `range()` 方法可以读出节点在源码里的字节范围（本讲后面实践会用到）：

```rust
pub fn range(&self) -> Range<usize> {
    self.offset..self.offset + self.node.len()
}
```

（位于 node.rs 同一 `impl` 块，紧邻 `new` 之后。）

#### 4.2.4 代码实践

**实践目标**：从根 `LinkedNode` 出发，观察它的偏移和范围。

**操作步骤**：把第 4.1.4 的测试改成从 `LinkedNode::new` 开始：

```rust
#[test]
fn explore_linked_root() {
    use typst::syntax::{LinkedNode, Side};
    use crate::tests::{FilePos, TestWorld, WorldLike};

    let owned = TestWorld::new("#let x = 1");
    let world = owned.acquire();
    let world = world.borrow();
    let (source, _) = 0isize.resolve(world);

    let root = LinkedNode::new(source.root());
    println!("offset={} range={:?} kind={:?}", root.offset(), root.range(), root.kind());
}
```

**需要观察的现象**：根节点的偏移和范围。

**预期结果**：偏移为 `0`，范围为 `0..10`（整段源码的字节长度）。

#### 4.2.5 小练习与答案

**练习 1**：`LinkedNode` 的 `parent` 字段为什么用 `Option<Rc<Self>>`（引用计数指针），而不是简单的 `Option<&'a Self>`？

**参考答案**：因为遍历语法树时，你可能同时持有「当前节点」和「它的父节点」等多个 `LinkedNode`，它们需要共享同一个父对象的生命周期。用 `Rc` 可以让多个子节点指向同一个父节点副本，而不必受限于借用检查器对单一借用来源的约束，从而能自由地克隆、向上回溯。

**练习 2**：假设你已经有一个 `LinkedNode`，调用 `root.parent()` 会得到什么？

**参考答案**：`None`。因为只有 `LinkedNode::new` 构造的根节点才有 parent 为 `None`；任何通过 `children()`/`leaf_at()` 得到的节点都会有非 `None` 的 parent。对一个根节点调用 `parent()` 正好返回 `None`，常被用来判断「是否已到达顶层」。

---

### 4.3 leaf_at —— 把光标落到叶子上

#### 4.3.1 概念说明

有了带上下文的根节点，下一步就是：给定光标偏移，找到光标实际指向的那个**叶子节点**。这就是 `leaf_at` 的职责。

为什么是「叶子」而不是任意节点？因为 IDE 的悬停、定义跳转，最终关心的都是具体的 token（一个标识符、一个字符串、一个标签……），而 token 总是叶子节点。内部节点只是结构容器。所以定位到叶子后，再由 `deref_target`（下一讲）决定它属于哪类表达式。

#### 4.3.2 核心流程

`leaf_at` 本身只是一个分发器，真正的查找由两个内部函数完成：

```
leaf_at(cursor, side)
   │
   ├─ side == Before  →  leaf_before(cursor)   取「光标之前」的叶子
   └─ side == After   →  leaf_after(cursor)    取「光标之后」的叶子
```

两个函数都是从根开始**递归向下**，根据光标落在哪个子节点的字节范围里，就进入那个子节点继续找，直到抵达叶子。

关键差别在于边界条件（`cursor` 等于某个节点的起点或终点时归属谁），这是第 4.4 节的主题。这里先看递归骨架：

- `leaf_before`：对叶子，若 \(\text{cursor} \le \text{offset} + \text{len}\) 则命中；对内部节点，进入满足 \(\text{offset} < \text{cursor} \le \text{offset}+\text{len}\) 的那个子节点。
- `leaf_after`：对叶子，若 \(\text{cursor} < \text{offset} + \text{len}\) 则命中；对内部节点，进入满足 \(\text{offset} \le \text{cursor} < \text{offset}+\text{len}\) 的那个子节点。

换句话说，`leaf_after` 把「正好在起点」的位置归给该节点，`leaf_before` 把「正好在终点」的位置归给该节点。

#### 4.3.3 源码精读

`leaf_at` 的分发：

[crates/typst-syntax/src/node.rs:1367-1373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1367-L1373) — `leaf_at` 根据 `side` 调用 `leaf_before` 或 `leaf_after`。注意只有 `leaf_at` 是 `pub`，两个内部函数是私有的。

`leaf_before` 的递归实现：

[crates/typst-syntax/src/node.rs:1328-1347](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1328-L1347) — 递归下钻到「光标之前」的叶子；主条件用半开半闭区间 `(offset, offset+len]`，并有一个边界特例 `offset == cursor && i+1 == count`，用于光标正好落在最后一个子节点起点时不返回 `None`。

`leaf_after` 的递归实现：

[crates/typst-syntax/src/node.rs:1349-1365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1349-L1365) — 递归下钻到「光标之后」的叶子；主条件用半开半闭区间 `[offset, offset+len)`，即光标正好在节点起点时归该节点。

在 typst-ide 里，跳转定义 `definition()` 正是这两行的标准用法：

[crates/typst-ide/src/definition.rs:34-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L34-L35) — `definition()` 先 `LinkedNode::new(source.root())` 建根，再 `.leaf_at(cursor, side)` 落到叶子，把结果交给 `deref_target` 分类。

#### 4.3.4 代码实践

**实践目标**：用 `leaf_at` 定位到一个具体叶子，打印它的 kind 与 leaf_text。

**操作步骤**：

```rust
#[test]
fn explore_leaf_at() {
    use typst::syntax::{LinkedNode, Side};
    use crate::tests::{FilePos, TestWorld, WorldLike};

    let owned = TestWorld::new("#let x = 1");
    let world = owned.acquire();
    let world = world.borrow();
    let (source, cursor) = 5isize.resolve(world); // 偏移 5：正好在标识符 x 上

    let leaf = LinkedNode::new(source.root())
        .leaf_at(cursor, Side::After)
        .unwrap();
    println!("kind={:?} text={:?}", leaf.kind(), leaf.leaf_text());
}
```

**需要观察的现象**：光标 5（`x` 的起点）配 `Side::After` 时命中的节点。

**预期结果**：kind 为 `Ident`，leaf_text 为 `"x"`。

#### 4.3.5 小练习与答案

**练习 1**：`leaf_at` 返回 `Option<LinkedNode>`。在什么情况下它会返回 `None`？

**参考答案**：当光标落在整棵树没有任何叶子覆盖的范围内时返回 `None`。最典型的是光标超出文件范围（偏移大于源码长度）。对于正常的、位于源码内的偏移，几乎总能命中某个叶子。

**练习 2**：为什么 `leaf_before` / `leaf_after` 是私有函数，只暴露 `leaf_at`？

**参考答案**：因为「该用 Before 还是 After」是一个**语义选择**，应由调用方根据交互场景决定（比如点击位置相对插入点的偏向）。把两个内部函数封装起来、只暴露带 `Side` 参数的统一入口，可以避免调用方误用裸接口，也让 API 表达「请显式声明你的偏向」这一意图。

---

### 4.4 Side 与 trivia —— 歧义消解与噪声处理

#### 4.4.1 概念说明

这里讲两个落到叶子后必须面对的现实问题。

**第一个问题：光标正好卡在两个 token 的交界处，该算谁的？**

考虑 `#let`：`#` 占字节 `[0,1)`，`let` 占 `[1,4)`。如果光标正好在偏移 `1`，它既是 `#` 的终点，又是 `let` 的起点。`Side` 枚举就是用来消解这种歧义的：

- `Side::Before` 表示「光标偏向这个字节索引的**之前**」——所以归属**前一个** token（`#`）。
- `Side::After` 表示「光标偏向这个字节索引的**之后**」——所以归属**后一个** token（`let`）。

这正好对应 `leaf_before` 的「终点归属」和 `leaf_after` 的「起点归属」。

**第二个问题：如果命中的叶子是 trivia（空白、注释）怎么办？**

`leaf_at` 会**原样返回** trivia 节点，不做跳过。这是与「兄弟/叶子导航器会跳过 trivia」不同的地方。因此调用方需要自己决定如何对待 trivia——大多数 IDE 功能对 trivia 没有有意义的信息，选择直接放弃。

#### 4.4.2 核心流程

先看 `Side` 的定义：

```rust
pub enum Side {
    Before,
    After,
}
```

交界处的归属规则（设光标 `c` 落在 token A `[a_s, a_e)` 与 token B `[a_e, b_e)` 的交界 `a_e` 上）：

| 光标 c 的位置 | `Side::Before` → `leaf_before` | `Side::After` → `leaf_after` |
| --- | --- | --- |
| c == a_e（A 与 B 的交界） | 命中 A（终点归属） | 命中 B（起点归属） |
| a_s < c < a_e（A 内部） | 命中 A | 命中 A |

当 A、B 不同时，交界处就是 `Before` 与 `After` **结果不同**的位置——这正是本讲实践任务要找的位置。

`leaf_at` 能命中 trivia，调用方典型处理是「命中 trivia 就放弃」，以 `tooltip()` 为例：

```
leaf = LinkedNode::new(root).leaf_at(cursor, side)?
if leaf.kind().is_trivia(): return None     # 命中空白/注释 → 没有悬停信息
... 后续 named_param / font / label / ... 分发
```

#### 4.4.3 源码精读

`Side` 枚举：

[crates/typst-syntax/src/node.rs:1292-1297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1292-L1297) — 只有 `Before`、`After` 两个变体，标注光标相对字节索引的偏向。

typst-ide 的 `tooltip()` 展示了「拿到叶子后立即检查 trivia」的标准模式：

[crates/typst-ide/src/tooltip.rs:31-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L31-L34) — `tooltip()` 把 `LinkedNode::new` + `leaf_at` 写成一行，紧接着用 `leaf.kind().is_trivia()` 过滤掉空白/注释节点。

`is_trivia()` 的判定标准：

[crates/typst-syntax/src/kind.rs:367-378](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/kind.rs#L367-L378) — `Space`、`LineComment`、`BlockComment`、`Parbreak`、`Shebang` 这几种被判定为 trivia。

`definition()` 的测试则生动展示了 `Side` 的实际选择——在标识符 `x` 上用 `Side::After`：

[crates/typst-ide/src/definition.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L158) — 测试 `test("#let x; #x", -2, Side::After)`：负数 `-2` 表示从字符串末尾倒数，配合 `Side::After` 精确落在最后的标识符 `x` 上，从而能跳转到 `#let x` 的定义处（范围 `5..6`）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：给定源码与一个光标，打印 `leaf_at` 在 `Side::Before` 与 `Side::After` 两种取值下得到的节点 kind 与 leaf_text，并找出一处两者不同的位置。

**操作步骤**：在 typst-ide 的任意测试模块（例如 `src/definition.rs` 的 `#[cfg(test)] mod tests`）里加入下面的测试，然后运行 `cargo test -p typst-ide explore_sides -- --nocapture`。

```rust
#[test]
fn explore_sides() {
    use typst::syntax::{LinkedNode, Side};
    use crate::tests::{FilePos, TestWorld, WorldLike};

    let owned = TestWorld::new("#let x = 1");
    let world = owned.acquire();
    let world = world.borrow();

    // 偏移 1：'#' 与 'let' 的交界处
    let (_, cursor) = 1isize.resolve(world);
    let root = LinkedNode::new(world.main.root());

    for side in [Side::Before, Side::After] {
        let leaf = root.leaf_at(cursor, side).unwrap();
        println!(
            "cursor={cursor} side={side:?} kind={:?} leaf_text={:?}",
            leaf.kind(),
            leaf.leaf_text(),
        );
    }
}
```

> 提示：这里直接用 `world.main.root()` 取根节点（`TestWorld.main` 是公开字段，见 `src/tests.rs`），省去再解析一次。

**需要观察的现象**：同一个光标 `1`，两种 side 打印出的 kind 与 leaf_text 是否不同。

**预期结果**：

```
cursor=1 side=Before kind=Hash  leaf_text="#"
cursor=1 side=After  kind=Let   leaf_text="let"
```

`Side::Before` 命中 `#`（Hash），`Side::After` 命中 `let`（Let）——这就是「两者不同」的交界位置。

**进阶观察**：再把光标改成 `4`（`let` 与其后空格的交界）。此时 `Side::After` 会命中 `Space`（trivia！）。你可以接着模仿 `tooltip.rs` 的写法，在拿到叶子后判断 `leaf.kind().is_trivia()`，验证 trivia 节点确实会被 `leaf_at` 原样返回。这部分精确输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`tooltip.rs` 为什么要先 `if leaf.kind().is_trivia() { return None; }`，而不是让 `leaf_at` 直接跳过 trivia？

**参考答案**：因为「是否需要跳过 trivia」是**调用方的策略**，不是定位本身的责任。`leaf_at` 的职责是忠实地返回光标处的叶子（哪怕是空白）；不同的 IDE 功能对 trivia 的处理可能不同（例如某些场景可能想把光标吸附到最近的非 trivia 节点）。把判断放在调用方，让 `leaf_at` 保持单一、可预测的语义。

**练习 2**：在 `#let x = 1` 中，分别用哪种 `Side` 能在光标 `5`（`x` 的起点）和光标 `6`（`x` 的终点）都定位到标识符 `x`？

**参考答案**：光标 `5` 是 `x` 的起点，需要 `Side::After`（起点归属）才能命中 `x`；光标 `6` 是 `x` 的终点，需要 `Side::Before`（终点归属）才能命中 `x`。这也解释了为什么 `tooltip.rs` 的测试里会出现 `test("#let x = 1 + 2", 5, Side::After)` 和 `test("#let x = 1 + 2", 6, Side::Before)` 两种写法——它们都在定位同一个 `x`。

## 5. 综合实践

**任务**：写一个「光标 → 叶子」扫描器，把源码每个字节偏移在两种 `Side` 下的定位结果打印成一张表，标出 `Before` 与 `After` 结果不同的位置，并对 trivia 做标记。

**目标**：把本讲的 `Source::root`、`LinkedNode`、`leaf_at`、`Side`、`is_trivia` 串成一个完整的小工具，直观建立对「交界处歧义」和「trivia 不被跳过」的肌肉记忆。

**参考实现**（加到 typst-ide 测试模块中，用 `cargo test -p typst-ide cursor_leaf_table -- --nocapture` 运行）：

```rust
#[test]
fn cursor_leaf_table() {
    use typst::syntax::{LinkedNode, Side};
    use crate::tests::{FilePos, TestWorld, WorldLike};

    let text = "#let x = 1";
    let owned = TestWorld::new(text);
    let world = owned.acquire();
    let world = world.borrow();
    let root = LinkedNode::new(world.main.root());

    for cursor in 0..=text.len() {
        let before = root.leaf_at(cursor, Side::Before);
        let after = root.leaf_at(cursor, Side::After);
        let fmt = |n: Option<LinkedNode>| match n {
            Some(n) => format!("{:?}'{}'(trivia={})", n.kind(), n.leaf_text(), n.kind().is_trivia()),
            None => "None".to_string(),
        };
        let differ = before.as_ref().map(|b| b.span())
            != after.as_ref().map(|a| a.span());
        println!(
            "cursor={cursor:>2} {} {} {}",
            fmt(before),
            fmt(after),
            if differ { "<-- 不同" } else { "" }
        );
    }
}
```

**需要观察的现象**：

1. 表格里每个交界处（如 1、4、5、6…）会被标出 `<-- 不同`，且 `Before` 指向前一个 token、`After` 指向后一个 token。
2. 当某侧命中空白时，会打印 `trivia=true`，证明 `leaf_at` 不跳过 trivia。

**预期结果**：你能从表中清楚地看到「`Before` 在交界处退一格、`After` 在交界处进一格」的规律，以及 trivia 节点确实会出现。完整表格的具体每一行**待本地验证**。

## 6. 本讲小结

- IDE 一切功能的起点都是「光标 → 语法树节点」：`Source::root()` 给出根 `SyntaxNode`，`LinkedNode::new()` 给它套上偏移/父节点/兄弟节点的上下文。
- `LinkedNode` 弥补了裸 `SyntaxNode` 缺乏位置与拓扑上下文的不足，是后续向上找祖先、找兄弟的基础设施。
- `leaf_at(cursor, side)` 把字节偏移递归映射到一个叶子节点，内部分发到 `leaf_before` / `leaf_after`。
- `Side::Before` 在交界处归属前一个 token（终点归属），`Side::After` 归属后一个 token（起点归属）；调用方根据交互场景选择。
- `leaf_at` 不跳过 trivia（空白、注释），`tooltip()` 用 `is_trivia()` 显式过滤——「是否跳过」是调用方策略。
- typst-ide 的标准两行写法是：`let leaf = LinkedNode::new(source.root()).leaf_at(cursor, side)?;`，见于 `definition()`、`tooltip()` 等。

## 7. 下一步学习建议

本讲只解决了「光标落在哪个节点上」。拿到叶子后，下一步要回答的是「这个节点属于哪类可被 IDE 操作的表达式」。建议继续：

1. **u2-l2 deref_target**：学习如何从叶子沿祖先向上找到第一个表达式节点，并归类为 `VarAccess` / `Callee` / `ImportPath` / `Label` / `Ref` 等。它是 `definition`、`tooltip` 的直接下游。
2. **u2-l3 named_items**：学习如何在「光标处」沿祖先与前置兄弟收集作用域内的命名项（`let` 绑定、`import` 项、循环绑定、闭包参数）。
3. 阅读建议：先读 `crates/typst-ide/src/definition.rs` 的 `definition()`（最完整的两行入口示例），再读 `crates/typst-syntax/src/node.rs` 中 `leaf_before`/`leaf_after` 的递归实现，对照本讲的边界规则理解每一行条件判断。
