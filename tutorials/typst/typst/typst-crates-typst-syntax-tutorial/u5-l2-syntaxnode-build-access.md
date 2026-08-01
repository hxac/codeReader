# SyntaxNode 的构造与访问

## 1. 本讲目标

上一讲（u5-l1）讲清了 `SyntaxNode` 的「内部形态」：它是一个 `data + span` 的外壳，私有 `enum Node` 有 `Leaf / Inner / Error / Warning` 四种形态。本讲把视角从**内部结构**转到**对外 API**：怎么把节点「造出来」，怎么把节点信息「读出来」，以及内部节点 `InnerNode` 为增量重解析偷偷缓存了哪些字段。

学完本讲你应当能够：

- 用 `leaf` / `inner` / `error` 三个公开构造器手工拼出一棵合法的 CST 子树；
- 理解 `warn` / `warn_at` 不是「造新节点」，而是「在已有节点外层套一个半透明警告」；
- 区分 `kind` / `len` / `span` / `leaf_text` / `full_text` / `children` 等访问器的语义，尤其是 `leaf_text` 与 `full_text` 的关键差别；
- 说清 `InnerNode` 的五个内部字段（`len` / `descendants` / `diagnosis` / `upper` / `children`）以及为什么要把前四个缓存起来。

## 2. 前置知识

- **u5-l1**：`SyntaxNode` 只有 `data: Node` 与 `span: Span` 两字段；`node_ref()` 会穿透 `Warning` 层返回干净只读视图；`SyntaxKind` 被刻意留在 `Node` 内部以省 8 字节。本讲直接承接这些结论。
- **u2-l1 SyntaxKind**：构造节点时必须给它一个合法的 `SyntaxKind`。`leaf` / `inner` 会用 `debug_assert!` 拒绝 `SyntaxKind::Error`——错误必须走专门的 `error` 构造器。
- **u6 Span**：每个节点都有一个 `Span` 作为稳定身份。**新构造的节点默认带 `Span::detached()`（未编号）**，编号要等到 `Source::new` 调用 `numberize` 时才统一盖上。
- **CST 无损**这个总目标贯穿全讲：构造与访问都要保证整棵树能完整还原源文本，任何被「裁掉」的内容（如 raw 文本首尾空行）也要以 `RawTrimmed` 之类节点保留下来。

## 3. 本讲源码地图

本讲内容几乎全部集中在单个文件：

| 文件 | 作用 |
| --- | --- |
| [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) | `SyntaxNode` 的全部构造器与访问器，以及私有 `InnerNode` 的字段与构造 |

辅助类型同样来自该文件：`ErrorNode`、`WarningWrapper`、`Diagnosis`。涉及到的 `Span::detached()` 与 `SubRange` 定义在 [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs)，本讲只用到它们最浅的一层。

## 4. 核心概念与源码讲解

### 4.1 构造器家族：leaf / inner / error / warn / warn_at

#### 4.1.1 概念说明

CST 的每个节点都由构造器产出。typst-syntax 把「造节点」的能力收口在 `impl SyntaxNode` 的一组公开函数里。按用途分两类：

- **从零造节点**：`leaf`（叶子 / 文本 token）、`inner`（带 children 的结构节点）、`error`（错误节点）。
- **在已有节点上加工**：`warn` / `warn_at`（包一层警告）、`hint` / `hint_at` / `with_hints`（给错误或警告追加用户提示）。

两条不变量务必记住：

1. `leaf` / `inner` 拒绝 `SyntaxKind::Error`（见各自的 `debug_assert!(!kind.is_error())`）；反过来 `error` 只会产出 `SyntaxKind::Error`。错误 kind 是 `Error` 形态的「专属标签」，非 `Error` 形态永不为 `Error` kind。
2. 所有从零造出的节点 `span` 都初始化为 `Span::detached()`——也就是说**手工构造的树一开始是没有编号的**，要等 `numberize` 走一遍才有正式 Span。

`warn` / `warn_at` 与前三者本质不同：它们接收 `&mut self`，把节点**当前**的 `data` 抽出来当 child，再在外层套一个 `WarningWrapper`。这就是 u5-l1 讲过的「Warning 半透明装饰层」的产生方式。

#### 4.1.2 核心流程

```text
leaf(kind, text)        → Node::Leaf(text, kind),            span = detached
inner(kind, children)   → Node::Inner(InnerNode::new(children), kind), span = detached
error(message, text)    → Node::Error(ErrorNode{message, text}, Error), span = detached
                          ⚠ 参数顺序：先 message，后 text

warn(&mut self, message):
    child ← 取出当前 self.data（原节点完整保留）
    self.data ← Node::Warning(WarningWrapper{ child, sub_range=None, message }, kind)
    # kind 不变（从原节点复制），所以 kind() 仍返回原 kind

warn_at(&mut self, start..end, message):
    同 warn，但 sub_range = Some(SubRange::new(start, end))
    ⚠ 会 assert!(end <= self.len())，越界 panic
```

#### 4.1.3 源码精读

**叶子构造器** [`leaf`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L109-L117)：先断言 kind 不是 error，再把 `(text, kind)` 包成 `Node::Leaf`。这是最轻的节点，没有 children。

**内部节点构造器** [`inner`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L119-L127)：把 children 交给 `InnerNode::new`（见 4.3），再用 `Arc` 包一层存进 `Node::Inner`。`Arc` 意味着 inner 节点是**引用计数的共享数据**，克隆廉价——这是 CST 能被多处引用、能做写时复制的前提。

**错误构造器** [`error`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L129-L140)：注意它的签名是 `error(message, text)`——**第一参数是给人看的错误消息，第二参数才是引发错误的源文本**。kind 写死为 `SyntaxKind::Error`。错误节点同时保留了「出错的文本」，保证 CST 无损。

**警告构造器** [`warn`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L142-L148) 与 [`warn_at`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L150-L165)：两者的关键技巧都是 `std::mem::replace(&mut self.data, Node::Leaf(EcoString::new(), kind))`——用一个**临时占位的空 Leaf** 把原来的 `data` 换出来，再把换出来的真节点塞进 `WarningWrapper.child`。最后 `self.data` 指向新的 `Warning`，`kind` 仍是原 kind。`warn_at` 额外用 `SubRange::new(start, end)` 记录警告针对的是节点文本里的哪一段子区间。

> 顺带一提 [`placeholder`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L201-L213)：它造一个**指定 kind、空文本**的叶子，用作 AST 节点结构不匹配时的「兜底占位」（u7 会用到），同样拒绝 error kind。

#### 4.1.4 代码实践

**实践目标**：亲手用 `leaf` 和 `inner` 造两个节点，用 Debug 输出观察它们的长相。

**操作步骤**（源码阅读型 + 最小构造）：

1. 在仓库外建一个小 crate（或参考 u1-l2 用 `cargo test -p typst-syntax` 在本仓库内运行测试），`Cargo.toml` 加 `typst-syntax` 依赖。
2. 写一段示例代码（**示例代码**，非项目原有）：

```rust
use typst_syntax::{SyntaxKind, SyntaxNode};

fn main() {
    let text = SyntaxNode::leaf(SyntaxKind::Text, "hi");
    let markup = SyntaxNode::inner(SyntaxKind::Markup, vec![text]);
    println!("{:#?}", markup);
}
```

**需要观察的现象**：Debug 输出里 `Markup` 这一行后面跟了一个数字（字节长度），再跟一个子节点列表。

**预期结果**（依据源码与 crate 内 `test_debug` 断言推导，待本地运行确认）：

```text
Markup: 2 [
    Text: "hi",
]
```

`2` 是 `Markup` 的字节长度（`"hi"` 占 2 字节），由 `InnerNode::new` 在构造时累加得到（详见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：调用 `SyntaxNode::leaf(SyntaxKind::Error, "x")` 会怎样？
**答案**：在 debug 构建里触发 `debug_assert!(!kind.is_error())` 失败；错误必须用 `SyntaxNode::error(message, text)` 构造。

**练习 2**：`error` 的两个参数哪个是消息、哪个是文本？为什么要把文本也存下来？
**答案**：第一参数 `message` 是给人看的错误消息，第二参数 `text` 是引发错误的源文本。存文本是为了保证 CST 无损——错误节点也要覆盖源码里那段出错的字符，使整棵树仍能完整还原原文。

---

### 4.2 访问器：kind / len / span / leaf_text / full_text / children

#### 4.2.1 概念说明

构造完节点后，最常做的就是把节点信息「读出来」。typst-syntax 提供了一组只读访问器。这里最容易踩坑的是 **`leaf_text` 与 `full_text` 的区别**：

- `leaf_text()`：叶子返回自己的文本，错误节点返回出错文本，**内部节点返回空串**。它是 O(1) 的「就地取文本」。
- `full_text()`：**无论哪种节点，都遍历整棵子树把叶子文本拼接出来**。对内部节点这是递归遍历，可能较贵，但能拿到节点覆盖的完整源文本。

直觉上：`leaf_text` 只看「这个节点自己手里有没有字」，`full_text` 才是「把这棵子树还原成原文」。文档里对 `full_text` 明确写了「If this is an inner node, it will traverse the tree to build the text which may be expensive」。

#### 4.2.2 核心流程

各访问器对四种形态（穿透 Warning 后）的返回值：

| 访问器 | Leaf | Inner | Error | （外层有 Warning 时） |
| --- | --- | --- | --- | --- |
| `kind()` | 该 kind | 该 kind | `Error` | **原 kind**（不下钻） |
| `len()` | 文本字节数 | `InnerNode.len` | 错误文本字节数 | 下钻取被包节点的 len |
| `span()` | `self.span` | `self.span` | `self.span` | 顶层 span |
| `leaf_text()` | 文本 | **空串** | 错误文本 | 下钻 |
| `full_text()` | 文本 | 遍历拼接 | 错误文本 | 遍历拼接 |
| `children()` | 空迭代器 | `inner.children` | 空迭代器 | 下钻 |

注意 `kind()` 是少数**不下钻**的方法：Warning 层会复制一份 kind（u5-l1 提到的「省 8 字节的代价」），所以即便外层包了 Warning，`kind()` 直接返回这一份复制的 kind，仍是原节点的 kind。

#### 4.2.3 源码精读

[`kind()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L215-L223) 直接 `match` 四种形态各自携带的 kind，不调用 `node_ref()`，因此 Warning 也直接命中。

[`len()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L230-L237) 走 `node_ref()`（穿透 Warning），对 Inner 取**缓存值** `inner.len`，对 Leaf / Error 取文本长度。这就是为什么 4.3 里 `InnerNode` 要把 `len` 缓存起来——查询长度不必累加所有子节点。

[`leaf_text()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L244-L254) 用了一个 `static EMPTY: EcoString` 作为 Inner 时的返回值——所有内部节点共享同一个空串，不分配内存。

[`full_text()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L256-L275)：Leaf / Error 直接 clone 文本；Inner / Warning 走 `traverse`，对每个访问到的节点，若是 Leaf/Error 就把文本 `push_str` 进 buffer，并返回它的 `children()` 让遍历继续下钻。

[`children()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L277-L283)：走 `node_ref()`，Inner 返回 `inner.children.iter()`，Leaf / Error 用 `[].iter()` 返回空迭代器。这就是外部遍历 CST 的标准入口（参见 u1-l4 的 `descendants()` 示例）。

#### 4.2.4 代码实践

**实践目标**：用一个 inner 节点直观感受 `leaf_text` 与 `full_text` 的差别。

**操作步骤**：接着 4.1.4 的 `markup` 节点（`inner(Markup, [leaf(Text,"hi")])`），加入：

```rust
println!("leaf_text = {:?}", markup.leaf_text()); // 内部节点 → 空
println!("full_text = {}", markup.full_text());    // 遍历拼接 → "hi"
for child in markup.children() {
    println!("child kind={:?} leaf_text={:?}", child.kind(), child.leaf_text());
}
```

**需要观察的现象**：`markup.leaf_text()` 返回空串，而 `full_text()` 拿到了 `"hi"`；遍历 `children()` 时，那个 `Text` 子节点的 `leaf_text()` 反而是 `"hi"`。

**预期结果**（依据源码推导，待本地运行确认）：

```text
leaf_text = ""
full_text = hi
child kind=Text leaf_text="hi"
```

**结论**：要拿内部节点的完整文本必须用 `full_text()`；`leaf_text()` 仅对叶子/错误节点有意义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kind()` 不像 `len()` 那样穿透 Warning，而是直接返回复制的 kind？
**答案**：因为 `WarningWrapper` 在包装时已经把原 kind 复制了一份存进 `Node::Warning(_, kind)`（见 4.1.3 的 `warn`）。复制 kind 是为了「省 8 字节」（u5-l1）而付出的代价，既然已经复制了，`kind()` 直接读它即可，无需下钻，访问最快。

**练习 2**：一个 `Markup` inner 节点 `leaf_text()` 返回什么？为什么？
**答案**：返回空串。`leaf_text()` 对 `Inner` 形态固定返回 `static EMPTY`，因为内部节点本身不直接持有文本，文本在它的叶子后代里。

---

### 4.3 InnerNode 的内部字段与缓存

#### 4.3.1 概念说明

`Inner` 形态的实际载荷是私有结构 `InnerNode`。它有五个字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `len` | `usize` | 该节点（含全部后代）覆盖的源文本**字节长度** |
| `descendants` | `usize` | 整棵子树的**节点总数，含自己** |
| `diagnosis` | `Diagnosis` | 这棵子树里**是否含错误 / 警告**（两个布尔） |
| `upper` | `u64` | 该节点编号区间的**上界**（给 Span 编号用） |
| `children` | `Vec<SyntaxNode>` | 真正的子节点序列 |

其中 `children` 是真实数据，`len` / `descendants` / `diagnosis` / `upper` 都是**缓存**。为什么要缓存？因为 typst-syntax 支持**增量重解析**（U9）：编辑源码后，它只想重解析受影响的一小段，再把改动「拼回」原树。拼回时只需用 `update_parent` / `replace_children` **局部修正祖先链上这几个缓存值**，而不必每次都从根重算整棵树的长度与后代数。`upper` 则配合 `numberize` 给新插入的子节点在编号区间里找空隙重编号。

#### 4.3.2 核心流程

`InnerNode::new` 在构造时一次性把缓存算好：

```text
InnerNode::new(children):
    len         ← Σ child.len()
    descendants ← 1 + Σ child.descendants()
    diagnosis   ← 各 child.diagnosis() 的「或」聚合
    upper       ← 0              # 暂时占位，numberize 时才赋真值
    children    ← children
```

后续改动不重新跑 `new`，而是用差量更新：[`update_parent`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L847-L858) 用 `self.len + new_len - prev_len` 修正长度与后代数，并用 `Diagnosis::any(&self.children)` 重算诊断；`replace_children` 则在替换一段子节点后重算这几个缓存并对新子节点重编号。

#### 4.3.3 源码精读

[`struct InnerNode`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L638-L652) 的字段定义与上面表格一一对应，注释明确说明 `descendants` 是「including this node」、`diagnosis` 是「this node or any of its children」、`upper` 是「numbering range 的上界」。

[`InnerNode::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L654-L668)：一个循环里同时累加 `len`、`descendants`，并用 `diagnosis.or(child.diagnosis())` 把子树的诊断向上聚合。注意 `descendants` 从 `1` 起步（算上自己），再累加每个子树的 `child.descendants()`。

[`Diagnosis`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L871-L903) 是 `{ errors: bool, warnings: bool }`，提供 `or`（按位或聚合）、`either`（任一为真）、`both`（同时为真）、`any`（遍历一片节点聚合）。`InnerNode.diagnosis` 缓存的就是子树的 `or` 聚合结果，使得 [`SyntaxNode::diagnosis()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301) 对 inner 节点能 O(1) 回答「这棵子树有没有错误」。

> 可见性提示：`InnerNode` 是私有结构，`descendants()` 等方法是 `pub(super)`，**外部代码无法直接调用**。外部只能通过 `SyntaxNode` 的公开访问器（`len`、`children`、`diagnosis`、`full_text` 等）间接读到这些缓存的效果。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `InnerNode::new` 与 crate 内测试，手工推算一棵小树的 `len` / `descendants` / `diagnosis`，理解缓存含义。

**操作步骤**（源码阅读型）：

1. 想象用 `inner(Markup, [leaf(Text,"hi")])` 造一个节点，按 `InnerNode::new` 的循环手算：
   - `len = child.len() = 2`（`"hi"` 2 字节）；
   - `descendants = 1 + child.descendants() = 1 + 1 = 2`（Markup 自己 + Text 一个叶子）；
   - `diagnosis = default().or(leaf 的 diagnosis) = {errors:false, warnings:false}`（Text 叶子无错）。
2. 打开 [src/node.rs 的 `test_debug`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1544)，看 `parse("**")` 产出的 `Strong: 2 [ Star, Markup: 0, Star ]`：注意 `Markup: 0` 的 `0` 就是它（空内容）的 `len` 缓存值。

**需要观察的现象**：Debug 输出里每个 inner 节点名字后面紧跟的数字，正是它的 `len` 缓存；这个数字等于其全部叶子文本的字节总和。

**预期结果**：对 `Markup` 包 `Text("hi")`，Debug 显示 `Markup: 2`；对空 `Markup`（如 `parse("**")` 里那个），显示 `Markup: 0`。

#### 4.3.5 小练习与答案

**练习 1**：一棵 `Strong` 节点含三个子节点 `Star, Markup(Text("hi")), Star`，它的 `descendants` 缓存值是多少？
**答案**：`1（Strong 自己）+ Star(1) + Markup(1+1=2) + Star(1) = 5`。`Markup` 子树自身是 2（Markup + Text），所以总数 5。

**练习 2**：为什么 `InnerNode` 要缓存 `diagnosis`，而不是每次遍历子树现算？
**答案**：为了让 [`SyntaxNode::diagnosis()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301) 与增量重解析都能 O(1) 判断「这棵子树有没有错误/警告」。缓存值在 `InnerNode::new` 时一次算好，改动时用 `update_parent` / `replace_children` 局部修正，避免全树重算。

---

## 5. 综合实践：手工组装一个 `*hi*` 的 Strong 节点树

**任务**：不用 parser，纯靠 `leaf` / `inner` 拼出一棵等价于 `parse("*hi*")` 的子树，并用访问器验证。

**背景**：由 u4-l3 与 crate 内 `test_debug` 可知，`Strong` 节点的真实结构是 `Strong → [Star, Markup, Star]`，其中 `Markup` 再包裹正文 `Text`（注意：不是裸 `Star + Text + Star`，中间多一层 `Markup`）。

**操作步骤**（**示例代码**，非项目原有）：

```rust
use typst_syntax::{SyntaxKind, SyntaxNode};

fn main() {
    // 三片叶子
    let star1 = SyntaxNode::leaf(SyntaxKind::Star, "*");
    let text  = SyntaxNode::leaf(SyntaxKind::Text, "hi");
    let star2 = SyntaxNode::leaf(SyntaxKind::Star, "*");

    // Markup 包住正文文本
    let markup = SyntaxNode::inner(SyntaxKind::Markup, vec![text]);

    // Strong = Star + Markup + Star
    let strong = SyntaxNode::inner(SyntaxKind::Strong, vec![star1, markup, star2]);

    println!("kind        = {:?}", strong.kind());
    println!("len         = {}", strong.len());
    println!("full_text   = {}", strong.full_text());
    println!("leaf_text   = {:?}", strong.leaf_text()); // 内部节点 → 空
    println!("children    = {}", strong.children().count());
    println!("--- debug ---\n{:#?}", strong);
}
```

**需要观察的现象**：

1. `strong.full_text()` 还原出完整原文 `"*hi*"`；
2. `strong.leaf_text()` 却是空串（因为 `Strong` 是 inner，文本在叶子后代里）——这正是 `leaf_text` 与 `full_text` 的关键差别；
3. Debug 输出里 `Strong: 4` 的 `4` = `Star(1) + Markup(2) + Star(1)`，是 `InnerNode` 的 `len` 缓存；
4. 结构与 `parse("*hi*")` 的输出一致（可与 `println!("{:#?}", typst_syntax::parse("*hi*"))` 对比）。

**预期结果**（依据源码与 crate 内 `test_debug` 的断言推导，待本地运行确认）：

```text
kind        = Strong
len         = 4
full_text   = *hi*
leaf_text   = ""
children    = 3
--- debug ---
Strong: 4 [
    Star: "*",
    Markup: 2 [
        Text: "hi",
    ],
    Star: "*",
]
```

> 想确认这份「手写树」与 parser 真的等价？把它和 `typst_syntax::parse("*hi*")` 的 root 用 [`spanless_eq`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L436-L458)（忽略 span 比较两棵树）对照即可——它会逐层比较 kind 与文本。

## 6. 本讲小结

- 节点构造收口在三个从零造节点的函数：`leaf`（叶子）、`inner`（带 children）、`error`（错误）。前两者拒绝 `Error` kind，错误只能走 `error(message, text)`（先消息后文本）。
- `warn` / `warn_at` 不造新节点，而是用 `mem::replace` 把原 `data` 抽出来包进一层 `WarningWrapper`，kind 保持不变——这就是 u5-l1 所说「Warning 半透明层」的产生方式。
- 访问器里最容易混淆的是 `leaf_text`（inner 返回空，O(1)）与 `full_text`（遍历拼接，可能较贵）；`kind()` 是少数不下钻的方法，因为它读的是 Warning 复制出来的 kind。
- `Inner` 形态的载荷是私有 `InnerNode`，有五个字段：真实数据 `children`，加上 `len` / `descendants` / `diagnosis` / `upper` 四个缓存。
- 这些缓存让 `len()`、`diagnosis()` 能 O(1) 回答，并支撑增量重解析的差量更新（`update_parent` / `replace_children`），是 U9 的基础。

## 7. 下一步学习建议

- **u5-l3 LinkedNode 带父指针遍历**：本讲的 `children()` 只能「往下」遍历；下一讲引入 `LinkedNode`，它带上父指针与绝对偏移，还能用 `leaf_at(cursor, Side)` 做光标定位，是 IDE / LSP 场景的关键。
- **u5-l4 错误与警告诊断**：本讲只触及 `warn` / `error` 的构造，下一讲系统讲 `ErrorNode` / `WarningWrapper` 如何汇总成 `SyntaxDiagnostic` 列表上报。
- **想提前理解缓存的价值**：可先扫一眼 U9 的 [`replace_children`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L737-L845)，看它如何只修正受影响区间的 `len` / `descendants` / `diagnosis` 并就地重编号，体会「缓存 + 差量更新」的设计动机。
