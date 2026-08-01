# 端到端体验：从文本到语法树

## 1. 本讲目标

前几讲我们已经分别认识了 typst-syntax 的定位（u1-l1）、构建方式（u1-l2）和模块地图（u1-l3）。本讲要把这些零散的知识串成**一条完整的数据流**：一段 Typst 源码文本，是如何一步步变成一个可查询的 `Source` 对象的。

学完本讲你应该能够：

1. 说出 [`Source::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) 内部依次调用了哪三步（`parse` → `numberize` → `Lines::new`），以及每一步的产物是什么。
2. 会用 `Source` 提供的 `root()` / `text()` / `id()` / `lines()` 四个访问器拿到解析结果。
3. 初步理解「给节点编号（numberize）」为什么能让语法树在编辑后保持稳定，为后续 Span 系统（U6）和增量重解析（U9）铺垫直觉。

本讲是「全局链路」的收尾，学完后你就具备了进入词法（U3）、语法（U4）等单模块深入阅读所需的宏观视角。

---

## 2. 前置知识

本讲假设你已经读过 u1-l1 和 u1-l3，对下面几个术语有基本印象。这里再用一句话复习，并把它们和本讲的链路对应起来：

- **Token（词法记号）**：源码被切成的一个个最小单元，例如 `#`、`let`、`x`、`=`、`1`。它是词法分析的产物，本讲里只是「路过」，详细在 U3。
- **CST（具体语法树，Concrete Syntax Tree）**：保留源码全部信息（包括空白、注释）的树，载体是 `SyntaxNode`。本讲里它就是 `parse` 的产物、`numberize` 的输入。
- **Span（源码区间标识）**：给 CST 里**每个节点**分配的一个稳定编号，用来在「节点」和「它在源码里的位置」之间建立映射。本讲里它是 `numberize` 的产物。
- **字节偏移（byte index）与行列（line/column）**：源码本质是一串 UTF-8 字节；「第几行第几列」是给人看的位置。`Lines` 就是这两套坐标系之间的转换表。

一个关键直觉：**Typst 不用「字节范围」来标识节点，而是用「编号」**。原因是编辑源码时字节范围会整体漂移，而编号可以做到「只动受影响的局部」。这个直觉贯穿本讲和后续的 Span、增量重解析两讲。

> 名词提醒：本讲里「根节点 root」有时指 `Source::root()` 返回的 CST 根（一个 `SyntaxNode`），有时指文件系统的「根路径」。下文会明确区分，不会混淆。

---

## 3. 本讲源码地图

本讲沿着一条主干线推进，涉及以下几个源文件：

| 文件 | 在本讲里的角色 | 关键代码 |
| --- | --- | --- |
| `src/source.rs` | **总装线**：`Source::new` 把三步串起来，并提供对外访问器 | `Source::new`、`root/text/id/lines`、`find/range` |
| `src/parser.rs` | 第一步 `parse`：文本 → 裸 CST | `parse` / `parse_code` / `parse_math` |
| `src/node.rs` | 第二步 `numberize`：给 CST 节点盖编号 | `SyntaxNode::numberize`、`InnerNode::numberize` |
| `src/lines.rs` | 第三步 `Lines::new`：建立行列索引 | `Lines::new`、`lines`、`lines_from` |
| `src/span.rs` | numberize 使用的编号空间 `Span::FULL` | `Span::FULL` |
| `src/lib.rs` | crate 门面：把上述类型挂牌导出 | `pub use` 列表 |

数据流一句话概括：

```
文本 String
  ──parse──▶ 裸 SyntaxNode（还没有编号）
  ──numberize──▶ 带编号的 SyntaxNode（每个节点都有 Span）
  ──Lines::new──▶ 行列索引
  ──打包──▶ Source（不可变、可哈希、可克隆）
```

---

## 4. 核心概念与源码讲解

### 4.1 Source::new —— 端到端的总装线

#### 4.1.1 概念说明

`Source` 是 typst-syntax 对外暴露的「一个源码文件」的完整抽象。在前置讲义里我们知道它内部用 `Arc<LazyHash<SourceInner>>` 打包了「文件身份 + 文本 + 行索引 + 语法树」，因此克隆和哈希都很廉价。

但 `Source` 不会自己长出来。真正把一段文本「加工」成 `Source` 的工厂方法就是 [`Source::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41)。它是本讲的「总装线」：依次把 parse、numberize、Lines::new 三个工序的结果组装进 `SourceInner`。

理解 `Source::new` 的意义在于：**它定义了三步的执行顺序和依赖关系**。后面四个小节都是在拆解它的每一行。

#### 4.1.2 核心流程

`Source::new(id, text)` 的执行流程（伪代码）：

```
输入：id   —— 文件身份 FileId（见 u1-l1）
输入：text —— 源码字符串

1. root = parse(text)              # 第一步：文本 → 裸 CST
2. root.numberize(id, Span::FULL)  # 第二步：给每个节点盖编号，写入 Span
3. lines = Lines::new(text)        # 第三步：建立行列索引
4. 把 { id, root, lines } 打包成 SourceInner
5. 用 Arc<LazyHash<..>> 包一层，返回 Source
```

注意第二步 `numberize` 是**就地修改** `root`（`root` 是 `mut`），它把编号写进每个节点的 `span` 字段。第三步 `Lines::new` 与语法树无关，只依赖原始 `text`，所以它和前两步其实是独立的——这也解释了为什么编辑文本时 `Lines` 可以单独增量重建（见 U8）。

#### 4.1.3 源码精读

总装线的真实代码极其精简，只有四行：

[文件：src/source.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) —— `Source::new`：依次调用 `parse` → `numberize` → `Lines::new`，再打包成 `SourceInner`。

```rust
pub fn new(id: FileId, text: String) -> Self {
    let _scope = typst_timing::TimingScope::new("create source");
    let mut root = parse(&text);
    root.numberize(id, Span::FULL).unwrap();
    Self(Arc::new(LazyHash::new(SourceInner { id, lines: Lines::new(text), root })))
}
```

要点解读：

- `parse(&text)` 返回一棵「裸」`SyntaxNode`，此时它**还没有 Span 编号**。
- `root.numberize(id, Span::FULL)` 在 `Span::FULL` 这个编号空间里给整棵树分配编号；返回 `Result`，`.unwrap()` 表示「全量编号几乎不会失败」（失败条件见 4.3 节）。
- 注意 `text` 被**移动**进了 `Lines::new(text)`，所以 `SourceInner` 里只保存一份文本（由 `Lines` 持有），`root` 树里只存字节长度而不存文本副本——这是省内存的关键设计。
- `LazyHash` 保证 `Source` 的哈希值按需计算且只算一次；`Arc` 保证克隆是廉价的引用计数。

被打包的 `SourceInner` 结构见 [文件：src/source.rs:28-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L28-L32)，三个字段 `id` / `root` / `lines` 正好对应本讲三步的产物。而 `Source` 本身只是一个包裹 `Arc<LazyHash<SourceInner>>` 的 Newtype，见 [文件：src/source.rs:16-24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L16-L24)。

> 拓展：除了 `new`，`Source` 还有两个构造器：`detached` 用一个假路径 `main.typ` 便于测试（它内部就是调用 `new`）；`with_root` 接收一棵**已经建好的**语法树，跳过 parse/numberize，用于增量重解析时复用旧树。这两者在 u1-l2 已有介绍。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「文本 → Source」的端到端过程，并确认根节点的 kind。

**操作步骤**（在仓库内进行，可随用随删）：

1. 打开 `src/source.rs`，在文件末尾已有的 `#[cfg(test)] mod test` 模块里**临时**新增一个测试：

```rust
#[test]
fn walkthrough_new() {
    // 这一行等价于 Source::new，只是用了便于测试的 detached
    let source = Source::detached("#let x = 1");

    // 验证三步产物都齐了
    println!("root kind = {:?}", source.root().kind());   // 第二步产物：带编号的 CST 根
    println!("text      = {:?}", source.text());          // 原始文本（第三步 Lines 持有）
    println!("id        = {:?}", source.id());            // 第零步：文件身份
    println!("lines     = {}", source.lines().len_lines()); // 第三步产物：行数
}
```

2. 运行测试并打印输出：

```bash
cargo test -p typst-syntax walkthrough_new -- --nocapture
```

3. 观察完毕后，**还原源码**（本讲强调不改源码）：

```bash
git checkout -- src/source.rs
```

**需要观察的现象**：根节点的 kind 应为 `Markup`（因为 `parse` 默认按 Markup 模式解析顶层）；`text` 正是你传入的字符串；`lines` 行数为 1（整段只有一行）。

**预期结果**：四行输出依次类似 `Markup`、`"#let x = 1"`、一个 `FileId`、`1`。如果某项不符，说明你对链路的理解有偏差，回头对照 4.1.2 的流程图。

> 说明：上述测试写在 `source.rs` 自带的测试模块里，是为了保证 `Source`、`SyntaxNode` 等类型在作用域内、且依赖完整可编译。运行后用 `git checkout` 还原即可，不会污染源码。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Source::new` 里的 `root.numberize(...)` 这一行注释掉，`source.root().span()` 还能用吗？为什么？

> **参考答案**：能取到 `span()`（它返回节点上存储的 `Span` 字段，永远存在），但取到的是默认的「 detached（未编号）」Span。后续用 `source.find(span)` 反查节点时会失败，因为编号查找依赖 numberize 写入的真实编号。

**练习 2**：为什么 `Lines::new(text)` 必须接收 `text` 的所有权（`String`），而不能只借一个 `&str`？

> **参考答案**：`SourceInner` 需要长期持有文本，而 `Lines` 正是 `SourceInner` 里唯一持有文本的地方（`root` 树只存长度不存文本）。因此 `Lines` 必须拥有文本所有权，才能让 `Source` 在 `Source::new` 返回后依然合法地引用这段文本。

---

### 4.2 parse —— 文本到裸 CST

#### 4.2.1 概念说明

`parse` 是链路的第一步：把一串字符变成一棵 `SyntaxNode` 树。此时的树**只有结构、没有编号**——每个节点的 `span` 字段都是默认值。所以本节称之为「裸 CST」。

`parse` 有三个兄弟入口，分别对应 Typst 的三种语法模式（Markup / Code / Math，见 u1-l1）：

- `parse(text)`：顶层 Markup，根 kind 为 `Markup`。
- `parse_code(text)`：纯代码，根 kind 为 `Code`。
- `parse_math(text)`：纯数学，根 kind 为 `Math`。

`Source::new` 只用 `parse`（因为一个 `.typ` 文件的顶层永远是 Markup）；另外两个入口主要给测试和局部重解析用。

#### 4.2.2 核心流程

三个入口的结构完全对称，以 `parse` 为例：

```
1. 用 (text, 起始偏移=0, SyntaxMode::Markup) 构造一个 Parser
2. 调用 markup_exprs 递归下降地解析一串 markup 表达式
3. finish_into(SyntaxKind::Markup)：把解析事件收尾、包装成根节点
4. 返回 SyntaxNode（裸，未编号）
```

解析器内部是「事件式（marker）+ 递归下降」的架构（借鉴 rust-analyzer），细节属于 U4 的内容。本讲你只需要知道：**`parse` 的输出是一棵结构正确但尚未盖编号的 CST**。

对于 `#let x = 1` 这段文本，`parse` 产出的树大致是（具体嵌套层级以你本地运行的 4.1.4 / 4.2.4 输出为准）：

```
Markup                          ← 根，parse 用 Markup 模式
└─ LetBinding                   ← #let 触发的代码绑定
   ├─ Hash         "#"
   ├─ Let          "let"        ← 关键字，代码模式下由 keyword() 识别
   ├─ Space        " "
   ├─ Ident        "x"
   ├─ Space        " "
   ├─ Eq           "="
   ├─ Space        " "
   └─ Numeric      "1"          ← 数字字面量
```

可以看到：同一棵 CST 里既有「结构节点」（`Markup`、`LetBinding`），也有「叶子记号」（`Hash`、`Ident`、`Numeric`），还有表示空白的 `Space` 叶子——这正是 CST「无损」的体现。

#### 4.2.3 源码精读

[文件：src/parser.rs:16-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L16-L21) —— `parse`：构造 Parser → 解析 markup 表达式序列 → 收尾为 `Markup` 根节点。

```rust
pub fn parse(text: &str) -> SyntaxNode {
    let _scope = typst_timing::TimingScope::new("parse");
    let mut p = Parser::new(text, 0, SyntaxMode::Markup);
    markup_exprs(&mut p, true, syntax_set!(End));
    p.finish_into(SyntaxKind::Markup)
}
```

三个入口的差异只在「模式」和「根 kind」上，见 [文件：src/parser.rs:24-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L24-L37)：`parse_code` 用 `SyntaxMode::Code` + `SyntaxKind::Code`，`parse_math` 用 `SyntaxMode::Math` + `SyntaxKind::Math`。

一个细节：每个入口都创建了 `typst_timing::TimingScope`，这意味着 parse 阶段是可以被性能采样的——这在后续排查编译耗时（尤其是增量编译）时很有用。

#### 4.2.4 代码实践

**实践目标**：用递归遍历把 `parse` 产出的裸 CST 完整打印出来，直观感受「无损语法树」长什么样。

**操作步骤**：

1. 在 `src/source.rs` 的测试模块里临时新增：

```rust
#[test]
fn dump_cst() {
    let source = Source::detached("#let x = 1");

    // 注意：SyntaxNode 没有公开的 descendants() 迭代器，
    // 遍历整棵树要自己用公开的 children() 递归。
    fn walk(node: &crate::SyntaxNode, depth: usize) {
        let indent = "  ".repeat(depth);
        let text = node.leaf_text(); // 叶子/错误节点返回文本，内部节点返回空串
        if text.is_empty() {
            println!("{}{:?}", indent, node.kind());
        } else {
            println!("{}{:?} {:?}", indent, node.kind(), text);
        }
        for child in node.children() {
            walk(child, depth + 1);
        }
    }

    walk(source.root(), 0);
}
```

2. 运行并查看输出：

```bash
cargo test -p typst-syntax dump_cst -- --nocapture
```

3. 还原源码：`git checkout -- src/source.rs`。

**需要观察的现象**：输出的缩进树应体现 `Markup → LetBinding → 各叶子` 的层级；每个叶子后面都跟着它覆盖的文本片段；`Markup`、`LetBinding` 这类内部节点后面没有文本（因为 `leaf_text()` 对它们返回空）。

**预期结果**：大致如 4.2.2 那棵树。如果你的输出里 `LetBinding` 下还有额外的包裹节点（例如把 `1` 包起来的表达式节点），以本地输出为准——这取决于 parser 的具体包装策略，本讲不展开。

> 关键说明：`SyntaxNode` 有一个名为 `descendants()` 的方法，但它是 `pub(super)` 且返回的是**节点数量（usize）**而非迭代器，外部代码无法直接用来遍历。所以本实践用公开的 `children()` 自己写递归——这是从 crate 外部遍历 CST 的正确姿势。相关方法见 [文件：src/node.rs:278-283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L278-L283)（`children`）、[文件：src/node.rs:216-223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L216-L223)（`kind`）、[文件：src/node.rs:247-254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L247-L254)（`leaf_text`）。

#### 4.2.5 小练习与答案

**练习 1**：分别用 `parse`、`parse_code`、`parse_math` 解析同一个字符串 `"1 + 2"`，它们的根 kind 分别是什么？

> **参考答案**：分别是 `Markup`、`Code`、`Math`。这正对应三种 SyntaxMode，证明三个入口只是「换了模式 + 换了根包装」。

**练习 2**：为什么 `Source::new` 只调用 `parse`，而不调用 `parse_code`？

> **参考答案**：一个 `.typ` 源文件的顶层语法就是 Markup（正文 + 标记）；代码和数学都是「嵌套」在 Markup 里的（`#` 后是代码，`$ $` 里是数学），由 parser 在解析过程中自动切换模式。所以顶层入口只需要 `parse`。

---

### 4.3 numberize —— 给每个节点盖上编号

#### 4.3.1 概念说明

`parse` 产出的裸 CST 有一个问题：每个节点的 `span` 字段都是默认值，**无法区分**。如果下游（求值器、IDE、增量重解析）想引用「这个特定的节点」，就缺少一个稳定的标识。

`numberize` 解决的就是这个问题：它对整棵树做一次遍历，给**每个节点**分配一个唯一的编号，并写进节点的 `span` 字段。这个编号就是 Span 的核心（Span 的位布局细节留到 U6，本讲只关心「编号」这个抽象）。

为什么用「编号」而不是「字节范围」？因为编辑源码时字节范围会整体平移，而编号可以设计成**留有余地**的，从而在局部编辑时无需给全树重新编号——这正是增量重解析（U9）能加速的关键。

#### 4.3.2 核心流程

numberize 采用「区间划分 + 取中点」的策略，在一个给定的编号区间 `[start, end)` 内分配：

```
输入：一棵子树、文件 id、可用的编号区间 within = [start, end)

1. descendants = 本子树要编号的节点总数（含自己）
2. space   = end - start
3. stride  = space / (2 * descendants)        # 尽量挤进左半区，右半区留作未来插入的余量
4. 给「自己」分配：编号 = start + stride 的中点，占用 [start, start+stride)
5. 把剩余区间 [start+stride, end) 按 children 的 descendants 数量按比例切分
6. 对每个 child 递归 numberize(child, 它分到的子区间)
```

第 3 步是精髓：**故意只用左半区**。这样当将来在两个已有节点之间插入一个新节点时，右半区还有连续的空号可用，不必搬动已有编号。用公式表达步长：

\[ \text{stride} = \left\lfloor \frac{\text{space}}{2 \cdot \text{descendants}} \right\rfloor \]

而每个节点最终拿到的编号，是其占用子区间的中点：

\[ \text{number} = \left\lfloor \frac{\text{start} + \text{end}_{\text{本节点}}}{2} \right\rfloor \]

这种分配方式天然保证了两条**编号不变量**（在源码注释里明确写出，见下文精读），这两条不变量是后续 `find_number` 快速二分定位的前提。

#### 4.3.3 源码精读

`numberize` 分两层。外层是 `SyntaxNode::numberize`，负责处理叶子/内部节点的分派：

[文件：src/node.rs:517-531](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L517-L531) —— `SyntaxNode::numberize`：对叶子直接取区间中点；对内部节点交给 `InnerNode::numberize` 递归。

```rust
pub(super) fn numberize(&mut self, id: FileId, within: Range<u64>) -> NumberingResult {
    if within.start >= within.end {
        Err(Unnumberable)
    } else if let Some((inner, span)) = self.inner_and_span_mut() {
        inner.numberize(span, id, None, within)
    } else {
        self.span = Span::from_number(id, SpanNumber((within.start + within.end) / 2));
        Ok(())
    }
}
```

内层 `InnerNode::numberize` 才是上面流程的实现，注意 stride 的计算和「先编号自己、再按比例分给 children」的逻辑：

[文件：src/node.rs:672-719](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L672-L719) —— `InnerNode::numberize`：计算 stride → 编号自身 → 按子树大小切分区间 → 递归 children。

关键片段：

```rust
let space = within.end - within.start;
let mut stride = space / (2 * descendants as u64);   // 挤进左半区
if stride == 0 {
    stride = space / self.descendants as u64;        // 余量不够时退而求其次
    if stride == 0 { return Err(Unnumberable); }     # 区间塞不下：编号失败
}
// 先编号自己（取中点），再把剩余区间按 children 的 descendants 分给各 child
```

这段揭示了 `numberize` 可能失败（返回 `Unnumberable`）的两个条件：可用区间为空（`within.start >= within.end`），或者区间太小塞不下所有节点（`stride == 0`）。`Source::new` 用整个 `Span::FULL` 空间编号一棵正常规模的树，几乎不会触发失败，所以敢 `.unwrap()`。

编号空间 `Span::FULL` 的定义在 [文件：src/span.rs:87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L87)：`pub(crate) const FULL: Range<u64> = 2..(1 << 47);`，即可用编号从 2 到 \(2^{47}\)，是一个极其宽敞的空间，足以容纳「挤左半区 + 留余量」的策略。

两条编号不变量在 `find_number` 的文档注释里写得很清楚：

[文件：src/node.rs:1124-1128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1124-L1128) —— 编号顺序保证：父节点编号 < 所有子节点编号；兄弟节点编号从左到右递增。

> 待确认：本讲不展开 Span 的 64 位位布局（FileId 占多少位、number 占多少位），那是 U6 的主题。本讲只需把 number 当作「节点在一个巨大编号空间里的唯一坐标」即可。

#### 4.3.4 代码实践

**实践目标**：用 numberize 产出的编号，亲手验证「父 < 子、兄弟递增」两条不变量。

**操作步骤**：

1. 在 `src/source.rs` 测试模块里临时新增（`Span` 已在测试模块顶部导入）：

```rust
#[test]
fn check_numbering_invariants() {
    let source = Source::detached("#let x = 1");
    let root = source.root();

    // 根的编号应小于任一子节点
    let root_num = root.span().number();
    for child in root.children() {
        assert!(
            root_num < child.span().number(),
            "父节点编号必须小于子节点"
        );
        // 同一层兄弟之间从左到右递增
        let mut prev = 0u64;
        for c in child.children() {
            assert!(c.span().number() > prev, "兄弟编号应递增");
            prev = c.span().number();
        }
    }
    println!("root number = {root_num}, invariants hold");
}
```

2. 运行：`cargo test -p typst-syntax check_numbering_invariants -- --nocapture`
3. 还原：`git checkout -- src/source.rs`

**需要观察的现象**：断言全部通过，打印出根节点的编号（一个很大的整数，远大于 2，因为 `Span::FULL` 起点是 2 且 stride 很大）。

**预期结果**：测试通过，输出形如 `root number = <某个大整数>, invariants hold`。若断言失败，说明你对不变量的理解或代码有误。

#### 4.3.5 小练习与答案

**练习 1**：为什么 numberize 故意「只把编号挤进区间的左半区」？

> **参考答案**：为了给未来的节点插入留出连续的空号。当增量编辑在两个已有节点之间插入新节点时，可以直接在右半区的空隙里给它分配编号，而不必重新编号整个子树。这让 Span 在编辑后保持稳定。

**练习 2**：在什么情况下 `numberize` 会返回 `Err(Unnumberable)`？

> **参考答案**：当可用编号区间为空（`start >= end`），或区间太小、连每个节点分到 1 个编号都不够（`stride == 0`）时。全量解析用 `Span::FULL`（约 \(2^{47}\)）编号正常规模的树几乎不会遇到，所以 `Source::new` 直接 `.unwrap()`。

---

### 4.4 Lines::new —— 建立行列索引

#### 4.4.1 概念说明

第三步 `Lines::new` 解决的是另一个维度的问题：**字符位置 ↔ 行列位置**的双向转换。

源码在内存里是一串 UTF-8 字节。但人和 IDE 需要的是「第几行第几列」。而且 Typst 还要兼容以 UTF-16 计数的客户端（如 LSP 协议、旧版 JS），所以需要三套坐标系之间的换算：

- **字节偏移（byte index）**：Rust 字符串的原生坐标，UTF-8 字节计数。
- **字符/列（column）**：第几个 Unicode 字符，从行首算起。
- **UTF-16（utf16 index）**：按 UTF-16 码元计数，给 IDE 用。

`Lines` 就是一张预先算好的「换算表」：它把文本按换行符切成行，记录每一行起始的字节偏移和 UTF-16 偏移，之后任何坐标转换都能用二分查找快速完成。

#### 4.4.2 核心流程

`Lines::new` 的流程：

```
输入：text（文本，持有所有权）

1. lines(text)：扫描文本，找出所有换行符位置
   - 预置「第 0 行起点 = (byte 0, utf16 0)」
   - 用 lines_from 逐个换行符产出每行的起点偏移
   - 注意 \r\n（CRLF）算一个换行
2. 把 { lines 数组, text } 打包进 Arc<LinesInner>
3. 返回 Lines（引用计数，克隆廉价）
```

关键点：`Lines` **不依赖语法树**，只依赖原始文本。这也是为什么编辑文本时可以只重建 `Lines`（U8 的增量行重建）而不碰 CST。

#### 4.4.3 源码精读

`Lines::new` 本身非常薄，真正的扫描在私有函数 `lines` 里：

[文件：src/lines.rs:32-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L32-L35) —— `Lines::new`：调用 `lines(text)` 算出行元数据，与文本一起打包。

```rust
pub fn new(text: T) -> Self {
    let lines = lines(text.as_ref());
    Lines(Arc::new(LinesInner { lines, text }))
}
```

`lines` 函数预置第 0 行，再把后续行串起来：

[文件：src/lines.rs:245-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L245-L249) —— `lines`：先放第 0 行起点，再链上 `lines_from` 的产出。

真正的扫描逻辑在 `lines_from`，它用 `unscanny::Scanner` 边走边累计 UTF-16 偏移，遇到换行符就记一行起点，并特别处理 `\r\n`：

[文件：src/lines.rs:252-276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L252-L276) —— `lines_from`：扫描换行符、累计 utf16 偏移、处理 CRLF。

每行的元数据结构很简单，只有两个偏移：

[文件：src/lines.rs:22-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L22-L28) —— `Line`：记录该行起始的 `byte_idx` 与 `utf16_idx`。

有了这张表，`byte_to_line` 就能对行起点数组做二分查找定位行号（[文件：src/lines.rs:67-74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L67-L74)），其余转换都建立在此基础上。本讲只看建表过程，转换的细节留到 U8。

#### 4.4.4 代码实践

**实践目标**：用一段含多字节字符和换行的文本，观察 `Lines` 如何切分、行列如何换算。

**操作步骤**：

1. 在 `src/lines.rs` 自带的 `#[cfg(test)] mod tests` 里临时新增：

```rust
#[test]
fn show_lines() {
    // ä 是 2 字节；💛 是 4 字节；含 \n 与 \r\n
    let lines = Lines::new("ä\nc\r\nd");
    println!("len_lines = {}", lines.len_lines());
    for (i, _) in (0..lines.len_lines()).enumerate() {
        let byte = lines.line_to_byte(i).unwrap();
        println!("line {i} starts at byte {byte}");
    }
    // 字节偏移 1（ä 的第二个字节之后）应在第 0 行
    println!("byte 1 -> line {:?}", lines.byte_to_line(1));
    // 字节偏移 3（c 之后）应在第 1 行（因为 'ä'(2) + '\n'(1) = 3）
    println!("byte 3 -> line {:?}", lines.byte_to_line(3));
}
```

2. 运行：`cargo test -p typst-syntax lines::tests::show_lines -- --nocapture`
3. 还原：`git checkout -- src/lines.rs`

**需要观察的现象**：`len_lines` 应为 3（`ä\nc\r\nd` 被 `\n` 和 `\r\n` 切成三段：`ä`、`c`、`d`）；各行的字节起点反映出多字节字符占用的字节数。

**预期结果**：`len_lines = 3`；第 0 行起于 byte 0、第 1 行起于 byte 3（`ä`=2 + `\n`=1）、第 2 行起于 byte 5（再加 `c`=1 + `\r`=1 + `\n`=1，注意 CRLF 整体算一个换行边界）。`byte_to_line(1)` 与 `byte_to_line(3)` 分别得到 `Some(0)` 和 `Some(1)`。**待本地验证**：多字节字符的具体字节计费请以本地输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Lines` 要同时记录每行的 `byte_idx` 和 `utf16_idx`，而不是只记一个？

> **参考答案**：Rust 内部用 UTF-8（需要 byte_idx），而 LSP 等 IDE 协议用 UTF-16 计数（需要 utf16_idx）。同时记录两者，就能在两套坐标系之间做 O(log 行数) 的二分换算，而不必每次线性扫描。对于含emoji/CJK 的文本，UTF-8 和 UTF-16 计数差异很大，二者缺一不可。

**练习 2**：编辑源码后，`Lines` 和 CST 哪个更容易重建？为什么？

> **参考答案**：`Lines` 更容易。它只依赖纯文本，编辑后只需重算受影响行的起点（见 U8 的增量行重建）；而 CST 的重建要重新词法+语法分析（即便有增量重解析也比行重建复杂）。这也是 `Source::edit` 里先更新 `lines` 再 reparse root 的原因。

---

### 4.5 用 Source 访问解析结果

#### 4.5.1 概念说明

前三步把产物都装进了 `Source`，但 `Source` 对外暴露的不是裸字段，而是一组**只读访问器**和两个**反查方法**。理解这套接口，你才能把 `Source` 真正用起来（这也是 IDE、诊断、高亮等下游模块使用 typst-syntax 的方式）。

本节作为收尾，把「查询」补全：正向用 `root/text/id/lines` 拿产物，反向用 `find/range` 从 Span 或编号定位回节点和字节范围。

#### 4.5.2 核心流程

```
正向访问（拿产物）：
  source.root()  -> &SyntaxNode   # 已编号的 CST 根
  source.text()  -> &str          # 原始文本（来自 Lines）
  source.id()    -> FileId        # 文件身份
  source.lines() -> &Lines        # 行列索引

反向定位（从标识回到节点/范围）：
  source.find(span)  -> Option<LinkedNode>   # Span → 带父指针的节点
  source.range(num, sub_range) -> Option<Range<usize>>  # 编号 → 字节范围
```

`find` 是 IDE 高亮、跳转的基础：拿到一个 Span，立刻定位到它在树里的具体节点。`range` 则把节点（或节点内的子区间）翻译成字节范围，用于在编辑器里画下划线。

#### 4.5.3 源码精读

四个正向访问器都在一段紧凑的代码里：

[文件：src/source.rs:57-76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L57-L76) —— `root/id/text/lines`：分别返回 CST 根、文件身份、文本（委托给 `Lines::text`）、行列索引。

注意 `text()` 其实是 `self.0.lines.text()`——文本存在 `Lines` 里，`Source` 没有独立的文本字段，这与 4.1.3 提到的「文本只存一份」一致。

反向定位的两个方法：

[文件：src/source.rs:117-122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L117-L122) —— `find`：先校验 Span 的 FileId 是否属于本文件，再委托 `LinkedNode::find`（内部走 4.3 节那两条不变量做快速查找）。

[文件：src/source.rs:129-142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L129-L142) —— `range`：用 `find_number` 定位节点，再可选地把 `SubRange`（节点内相对偏移）换算成绝对字节范围。

这些类型之所以能被外部使用，是因为 `lib.rs` 用 `pub use` 把它们挂牌到了 crate 根：

[文件：src/lib.rs:24-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L24-L36) —— `pub use`：把 `Lines`、`SyntaxNode`/`LinkedNode`、`parse` 系列、`Source`、各种 Span 类型统一导出，构成对外 API。

#### 4.5.4 代码实践

**实践目标**：完整体验「正向取节点 → 取它的 Span → 反向 find 回节点 → 取字节范围」的闭环。

**操作步骤**：

1. 在 `src/source.rs` 测试模块里临时新增：

```rust
#[test]
fn find_round_trip() {
    let source = Source::detached("= head <label>");
    // 正向：取某个叶子节点（偏移 2，即 'h' 处）的 span
    use crate::{LinkedNode, Side};
    let leaf = LinkedNode::new(source.root())
        .leaf_at(2, Side::After)
        .unwrap();
    let span = leaf.span();
    println!("leaf text = {:?}", leaf.text()); // 这里用 leaf.get().leaf_text() 也行

    // 反向：用同一个 span 找回节点
    let found = source.find(span).expect("应能在本文件内找到");
    println!("found kind = {:?}", found.get().kind());

    // 反向：用编号取字节范围
    let range = source.range(span.into_number_or(...), None);
    // 上面 range 的参数较繁琐，更简单的做法见下方说明
}
```

> 说明：`Source::range` 的第一参数是 `SpanNumber`，从 `Span` 取编号的细节属于 U6。本实践更推荐直接复用仓库**已有**的测试 `test_source_sub_ranges`（见 [文件：src/source.rs:157-182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L157-L182)），运行它能直接看到 `find`/`range`/`SubRange` 的效果：

```bash
cargo test -p typst-syntax test_source_sub_ranges -- --nocapture
```

2. 还原你新增的测试：`git checkout -- src/source.rs`（已有的 `test_source_sub_ranges` 无需改动）。

**需要观察的现象**：`find` 用一个 span 成功定位回原节点；已有测试 `test_source_sub_ranges` 验证了 `SubRange::new(1, 3)` 能从 `"head"` 取出 `"ea"`，体现了「Span 编号 + 子区间 → 字节范围」的反查能力。

**预期结果**：你新增的 round-trip 测试打印出叶子的 kind（如 `Text`）和文本（如 `"head"`）；已有测试通过。

#### 4.5.5 小练习与答案

**练习 1**：`source.find(span)` 第一步为什么要先比较 `span.id()` 和 `self.id()`？

> **参考答案**：一个 Span 编码了它所属文件的 FileId。如果传入的 span 属于别的文件，它在本文件的树里根本不存在，直接返回 `None`，避免无意义的遍历，也防止跨文件误匹配。

**练习 2**：`source.text()` 和 `source.root().full_text()` 都能得到全文，它们有何区别？

> **参考答案**：`text()` 直接返回 `Lines` 里存的原始文本引用，O(1)；`full_text()` 是从 CST 树遍历叶子拼接出来的，对内部节点要遍历整棵子树，较慢。所以拿全文应该用 `text()`。这也再次说明文本「真相」存在 `Lines` 里。

---

## 5. 综合实践

把本讲四步串起来，完成下面这个端到端小任务：

**任务**：解析一段两行的 Typst 文档，验证整条链路，并用三类坐标系描述一个节点。

输入文本：

```
= 标题
#let x = 1
```

要求：

1. 用 `Source::detached` 解析它（内部走完 parse → numberize → Lines::new）。
2. 遍历 CST，找到 `Heading`（标题）节点，打印它的 kind 与 `leaf_text()`（应为空，因为它是内部节点）。
3. 打印 `source.lines().len_lines()`，确认行数为 2（注意中文「标题」是多字节字符，但不影响按换行符切行）。
4. 取 `Heading` 节点的 span，用 `source.find(span)` 反查，确认能找回同一个节点。
5. 用 `source.lines().byte_to_line(<标题的某个字节偏移>)` 说明：**同一个位置可以用「字节偏移」「行列」「span 编号」三种方式表达**，而 `Source` + `Lines` 提供了它们之间的换算。

**提示**：

- 临时把代码写进 `source.rs` 的测试模块，用 `cargo test -p typst-syntax <名字> -- --nocapture` 运行，结束后 `git checkout -- src/source.rs` 还原。
- 遍历用 4.2.4 的 `walk` 递归；`Heading` 节点的 kind 是 `Heading`（标题由 `=` 触发）。
- 中文字符会让 byte 偏移大于「字符序号」，这正是 `Lines` 同时记录 byte/utf16 的意义所在——注意观察这个差异。

**预期结果**：你能用一句话说清「文本经过 parse、numberize、Lines::new 三步变成 Source，而 Source 既能正向给 CST/文本/行索引，又能反向从 Span 定位回节点与字节范围」——这就是 typst-syntax 端到端的全貌。

---

## 6. 本讲小结

- `Source::new` 是端到端总装线：依次调用 `parse`（文本→裸 CST）→ `numberize`（给节点盖编号）→ `Lines::new`（建立行列索引），三步产物打包进 `SourceInner`。
- `parse` 产出的裸 CST **只有结构、没有编号**；它有三个模式入口（Markup/Code/Math），`Source::new` 只用 Markup 入口。
- `numberize` 用「区间划分 + 取中点 + 挤左半区」的策略给每个节点分配稳定编号，保证「父<子、兄弟递增」两条不变量，这是 Span 稳定性和后续快速定位的基础。
- `Lines::new` 只依赖纯文本，预计算每行的字节/UTF-16 起点，提供字节↔行↔列↔UTF-16 的换算；它和 CST 互相独立。
- `Source` 对外提供正向访问器（`root/text/id/lines`）和反向定位（`find/range`），文本的「唯一真相」存在 `Lines` 里，CST 树只存字节长度。
- 从外部遍历 CST 要用公开的 `children()` 递归，而不是 `pub(super)` 的 `descendants()`（后者只返回节点计数）。

---

## 7. 下一步学习建议

本讲把「全局链路」走通后，接下来的进阶层会**逐层深入**这条链路里的每一环：

1. **先打词汇表基础（U2）**：深入学习 `SyntaxKind` 枚举与 `SyntaxSet` 位集，这是读懂 lexer/parser 输出的前提。
2. **再拆第一环——词法（U3）**：研究 `Lexer` 如何把字符切成 token，理解 Markup/Code/Math 三模式词法的差异。
3. **然后是第二环——语法（U4）**：研究 `Parser` 的事件式/marker 架构如何把 token 组装成 CST。
4. **关于编号（本讲的 numberize）的完整细节**：留到 U6（Span 系统）和 U9（增量重解析），届时你会看到 Span 的 64 位位布局，以及 numberize 的「留余量」策略如何在编辑后大显神威。

建议在进入 U2 之前，先把本讲的 4.1～4.2 节的实践（dump_cst）跑一遍，亲手看到一棵真实的 CST——这会让你在后续读词法/语法源码时始终有「全局画面」。
