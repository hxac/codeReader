# SyntaxNode 的四种形态

## 1. 本讲目标

在前面的单元里，我们已经知道 Typst 用一棵 **CST（具体语法树）** 作为唯一的、无损的「真相来源」，而承载这棵树上每一个节点的类型就是 `SyntaxNode`。本讲要回答三个问题：

1. 一个 `SyntaxNode` 内部到底长什么样？它为什么不是一个大而全的结构，而是分成 `Leaf / Inner / Error / Warning` 四种「形态」？
2. `Warning`（警告）形态为什么特殊——它如何「半透明」地包裹住另一个节点，又为什么访问长度、文本、子节点时必须「下钻」穿过它？
3. 源码里两个名字很接近的私有助手 `node_ref` 与 `inner_and_span_mut` 分别解决什么问题，为什么后者要同时返回「内部节点」和「span」两样东西？

学完本讲，你应该能够：

- 说出 `SyntaxNode` / `enum Node` / `enum NodeRef` 三者的层次关系。
- 解释 `SyntaxKind` 为什么被刻意留在 `Node` 内部、而不是提到 `SyntaxNode` 顶层。
- 读懂 `Warning` 的包装逻辑，并预测对一个节点调用 `warn()` 之后 `Debug` 输出的样子。

## 2. 前置知识

本讲只依赖你已经建立的两个认知（来自 U1、U2、U4）：

- **CST 节点 = `SyntaxNode`**：parser 产出的整棵树由 `SyntaxNode` 组成，每个节点都带一个 `SyntaxKind` 标签（如 `Markup`、`Strong`、`Text`、`Star`、`Error`）和一个 `Span`（节点在源码中的稳定身份）。
- **trivia、Error 节点也是 CST 的一部分**：parser 不会丢弃空白、注释或错误，而是把它们作为节点留在树里，从而保证「无损还原源码文本」。

另外需要一点 Rust 基础：

- `enum` 的变体可以携带数据（如 `Leaf(EcoString, SyntaxKind)`）。
- `Arc<T>` 是「引用计数、可克隆、可共享」的智能指针；`Arc::make_mut` 实现**写时复制（copy-on-write）**——当引用计数为 1 时原地可变借用，否则克隆一份再改。
- `std::mem::replace(&mut place, value)` 把 `place` 里的旧值「偷」出来，同时塞进 `value`，常用于「换芯」操作。

本讲所有源码都来自单一文件 [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs)。

## 3. 本讲源码地图

| 源码位置 | 作用 |
|---|---|
| `SyntaxNode`（对外公开结构体） | CST 节点的统一外壳，持有 `data: Node` 与 `span: Span` |
| `enum Node`（私有） | 节点的四种内部形态：`Leaf / Inner / Error / Warning` |
| `enum NodeRef`（私有） | 「按引用下钻视图」，只有 `Leaf / Inner / Error` 三种 |
| `SyntaxNode::node_ref` | 循环下钻穿过 `Warning`，返回 `NodeRef` |
| `SyntaxNode::inner_and_span_mut` | 可变下钻，同时返回 `&mut InnerNode` 与 `&mut Span` |
| `InnerNode` / `ErrorNode` / `WarningWrapper`（私有） | 三种形态各自的真实数据载体 |

记住一条主线：**`SyntaxNode` 是对外门面，`Node` 是内部实现，`NodeRef` 是「剥掉 Warning 后」的只读视图。**

## 4. 核心概念与源码讲解

### 4.1 SyntaxNode：统一的外壳与「kind 留在哪里」的取舍

#### 4.1.1 概念说明

`SyntaxNode` 是整个 CST 唯一对外公开的节点类型（在 [lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L25-L27) 中被 `pub use` 挂牌）。但它本身非常「瘦」——只有两个字段：

- `data: Node`：真正的节点内容（可能是叶子、内部节点、错误或警告）。
- `span: Span`：节点的稳定身份，8 字节。

为什么要把内容再藏一层进 `Node` 枚举？因为 CST 里有四种性质截然不同的东西：纯文本 token、带子节点的结构、错误、警告。把它们塞进一个扁平的大结构会浪费内存（很多字段对某种形态毫无意义）。Rust 的 `enum` 天生适合这种「带数据的标签联合体」——每个节点只为自己需要的数据付代价。

#### 4.1.2 核心流程

`SyntaxNode` 的内存取舍可以用一句话概括：

```
把 span 提到顶层（高效访问）
把 kind 故意留在 Node 内部（节省约 8 字节）
代价：Warning 层会「重复」一份 kind
```

为什么 kind 不也提到顶层？源码注释写得很直白——提到顶层会让 `SyntaxNode` 变大 8 字节，而这棵树动辄上万个节点，积少成多。代价是 kind 必须存在于 `Node` 的每个变体里（所以 `Node::Warning(_, kind)` 自己也带一份 kind，和它包裹的子节点的 kind 重复）。

#### 4.1.3 源码精读

`SyntaxNode` 的定义与那条关键注释在 [src/node.rs:L17-L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L17-L25)：

```rust
pub struct SyntaxNode {
    /// The underlying node data, potentially with wrapped warning messages.
    data: Node,
    /// The node's span, at the top-level to guarantee efficient access.
    span: Span,
    // We would love to move the `SyntaxKind` up here as well, but keeping it in
    // `Node` saves 8 bytes :/
}
```

- `data` 字段的注释点明：它「可能带包装的警告信息」——这就是本讲后半段 `Warning` 形态的伏笔。
- `span` 注释解释了为什么 span 在顶层：**保证访问高效**（`span()` 只是读一个字段，见 [src/node.rs:L240-L242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L240-L242)）。

而 `kind()` 则要 `match` 四种变体各取一次 kind（[src/node.rs:L216-L223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L216-L223)），注意它**不下钻**，只读最外层那份 kind：

```rust
pub fn kind(&self) -> SyntaxKind {
    match self.data {
        Node::Leaf(_, kind)
        | Node::Inner(_, kind)
        | Node::Error(_, kind)
        | Node::Warning(_, kind) => kind,
    }
}
```

正因为 kind 留在每个变体里，`Warning(_, kind)` 自己也持有一份，`kind()` 才能不穿透 Warning 就直接给出正确答案。

#### 4.1.4 代码实践

**实践目标**：理解「kind 留在 Node 内、span 在顶层」带来的访问差异。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/node.rs:L216-L242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L216-L242)。
2. 对比 `kind()` 与 `span()` 两个访问器的实现复杂度。

**需要观察的现象**：

- `span()` 是单行 `self.span`，零分支；
- `kind()` 是一个 `match`，需要分四种变体提取。

**预期结果**：体会「为了把 span 放顶层、把 kind 留在 Node 里省 8 字节」，访问 span 比访问 kind 更廉价，但两者都是 O(1)。这是一个「以少量访问代价换内存」的典型工程取舍。

#### 4.1.5 小练习与答案

**练习 1**：如果 `SyntaxKind` 也被提到 `SyntaxNode` 顶层（变成第三个字段），`Warning` 还需要在自己的变体里存 kind 吗？为什么源码最终没有这么做？

> **答案**：理论上可以不再存。但提到顶层会让 `SyntaxNode` 整体变大（注释说约 8 字节），而 Warning 为了让 `kind()` 不下钻仍会重复存一份，反而更乱。源码选择「kind 留在 Node 内部」，接受 Warning 层的重复，换取整体更小、mutable 访问更简单（见 `Node` 枚举上方注释）。

**练习 2**：`SyntaxNode` 派生了 `Clone, Eq, PartialEq, Hash`（[src/node.rs:L17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L17)）。克隆一个 `SyntaxNode` 是深拷贝还是廉价操作？

> **答案**：廉价。因为 `Node` 的 `Inner/Error/Warning` 三种变体都把真实数据放在 `Arc<...>` 里，克隆只是增加引用计数；只有 `Leaf` 的 `EcoString` 在超出内联长度时才是引用计数的。所以 `#[derive(Clone)]` 实际几乎是「指针复制」。

---

### 4.2 enum Node：Leaf / Inner / Error / Warning 四种形态

#### 4.2.1 概念说明

`Node` 是 `SyntaxNode.data` 的真实类型，是一个私有枚举，定义在 [src/node.rs:L52-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L52-L58)：

```rust
enum Node {
    Leaf(EcoString, SyntaxKind),
    Inner(Arc<InnerNode>, SyntaxKind),
    Error(Arc<ErrorNode>, SyntaxKind),
    Warning(Arc<WarningWrapper>, SyntaxKind),
}
```

四种形态对应 CST 里四类东西：

| 形态 | 载体 | 含义 | 例子 |
|---|---|---|---|
| `Leaf` | `EcoString`（文本） | 终端 token，没有子节点 | `Star: "*"`、`Text: "Head"` |
| `Inner` | `Arc<InnerNode>`（含 children） | 结构性节点，有子节点 | `Markup`、`Heading`、`Strong` |
| `Error` | `Arc<ErrorNode>`（含 text/message/hints） | 解析错误，也是无损节点 | `Error: "expected expression"` |
| `Warning` | `Arc<WarningWrapper>`（含另一个 `Node`） | 半透明地包裹某个节点 + 警告消息 | 「`**` 内没有文字」 |

前三者是「普通节点」，它们的 kind 就是自身的标签；第四者 `Warning` 是「装饰层」，它的 kind 复制自被包裹的节点。

一条重要不变量（见 [src/node.rs:L34-L37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L34-L37) 的注释）：**只有 `Error` 变体的 kind 是 `SyntaxKind::Error`，`Leaf` 与 `Inner` 永远不会是 Error kind。** 这一点在构造器里用 `debug_assert!(!kind.is_error())` 守住。

#### 4.2.2 核心流程

构造四种形态的入口分别对应四个构造器，全部返回一个 `span` 为「detached（未编号）」的新节点：

```
leaf(kind, text)     → Node::Leaf(text, kind)            // 终端 token
inner(kind, children)→ Node::Inner(Arc<InnerNode>, kind) // 结构节点
error(message, text) → Node::Error(Arc<ErrorNode>, Error) // 错误节点，kind 固定为 Error
warn(message)        → 把当前 data 包进 WarningWrapper      // 警告装饰层（见 4.4）
```

注意 `error()` 的参数顺序：**第一个是给人看的消息，第二个才是引发错误的文本**（[src/node.rs:L129-L140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L129-L140) 的 doc 特别提醒了这一点）。

#### 4.2.3 源码精读

三个普通构造器在 [src/node.rs:L109-L140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L109-L140)：

```rust
pub fn leaf(kind: SyntaxKind, text: impl Into<EcoString>) -> Self {
    debug_assert!(!kind.is_error());
    Self { data: Node::Leaf(text.into(), kind), span: Span::detached() }
}

pub fn inner(kind: SyntaxKind, children: Vec<SyntaxNode>) -> Self {
    debug_assert!(!kind.is_error());
    Self { data: Node::Inner(Arc::new(InnerNode::new(children)), kind), span: Span::detached() }
}

pub fn error(message: impl Into<EcoString>, text: impl Into<EcoString>) -> Self {
    Self {
        data: Node::Error(Arc::new(ErrorNode::new(message.into(), text.into())), SyntaxKind::Error),
        span: Span::detached(),
    }
}
```

读法要点：

- `leaf`/`inner` 用 `debug_assert!` 守住「不能是 Error kind」；`error` 则把 kind **写死**为 `SyntaxKind::Error`。
- `inner` 把 `children` 交给 `InnerNode::new`，后者会在构造时顺便算好整棵子树的 `len`、`descendants`、`diagnosis` 三项缓存（[src/node.rs:L654-L668](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L654-L668)）。这些缓存是后续增量重解析（U9）提速的关键，本讲先记住「Inner 在构造时就预先统计好了」。

`Debug` 的打印也按形态分派（[src/node.rs:L621-L630](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L621-L630)）：`Leaf` 打印 `{kind:?}: {text:?}`，`Inner` 打印 `{kind:?}: {len} [子节点...]`，`Error` 与 `Warning` 各有自己的格式。

#### 4.2.4 代码实践

**实践目标**：亲手构造三种普通形态，确认它们的 `kind()` 与 `Debug` 输出符合预期。

**操作步骤**（可放入本 crate 的测试，或新建依赖 `typst-syntax` 的小程序）：

```rust
use typst_syntax::{SyntaxKind, SyntaxNode};

// 示例代码：手动构造三种形态
let star = SyntaxNode::leaf(SyntaxKind::Star, "*");
let text = SyntaxNode::leaf(SyntaxKind::Text, "hi");
let strong = SyntaxNode::inner(
    SyntaxKind::Strong,
    vec![star.clone(), text.clone(), star.clone()],
);
let err = SyntaxNode::error("演示错误", "@");

println!("{star:?}");   // 预期: Star: "*"
println!("{strong:?}"); // 预期: Strong: 6 [Star: "*", Text: "hi", Star: "*"]
println!("{err:?}");    // 预期: Error: "演示错误"
assert_eq!(strong.kind(), SyntaxKind::Strong);
```

**需要观察的现象**：

- `Star: "*"` 形如 `{kind}: {text}`；
- `Strong: 6 [...]` 中 `6` 是三个子节点字节长度之和（`"*"`+`"hi"`+`"*"` = 1+2+1）。

**预期结果**：与上面注释一致。`Error` 节点在文本与提示都为空时打印为 `Error: "消息"`（见 [src/node.rs:L949-L969](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L949-L969) 的 `Debug for ErrorNode` 分支）。

> 说明：以上输出格式参照 crate 内已验证的 `test_debug` 测试（[src/node.rs:L1487-L1544](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1544)）；若本地运行结果有细微差异，以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `leaf` 和 `inner` 用 `debug_assert!`（release 下会消失），而不是 `assert!`？

> **答案**：这是「库内部不变量」——违反它的调用方都在 crate 内部（parser），属于开发期 bug。用 `debug_assert!` 既能在测试/调试时捕获，又不给 release 用户付运行时检查代价。

**练习 2**：构造 `SyntaxNode::error("x", "y")` 后，`node.kind()` 返回什么？为什么不是某个「普通」kind？

> **答案**：返回 `SyntaxKind::Error`。因为 `error()` 把 kind 写死为 `SyntaxKind::Error`，这保证了「Error 变体的 kind 必为 Error」的不变量，下游（如诊断收集）可以只凭 kind 判断节点是不是错误。

---

### 4.3 enum NodeRef 与 node_ref / inner_and_span_mut：下钻视图

#### 4.3.1 概念说明

很多访问器（如 `len()`、`leaf_text()`、`children()`）需要拿到节点的「真实数据」。但顶层 `data` 可能是 `Warning`——而 Warning 自己没有文本/子节点，它的真实数据藏在被包裹的 `child` 里。于是 typst 提供了两个私有助手来「剥掉 Warning」：

- `node_ref(&self) -> NodeRef<'_>`：**只读**下钻，返回一个三变体视图 `NodeRef::Leaf / Inner / Error`（注意：没有 `Warning`，因为它已经下钻穿过了）。
- `inner_and_span_mut(&mut self) -> Option<(&mut InnerNode, &mut Span)>`：**可变**下钻，专门给「内部节点」用，并且同时把 span 也带出来。

#### 4.3.2 核心流程

`node_ref` 的下钻循环：

```
data = &self.data
loop:
    若 Leaf/Inner/Error  → 返回对应的 NodeRef（终止）
    若 Warning           → data = &warn.child（继续下钻，可能多层）
```

正因为 `warn()` 可以连续调用、堆叠多层 Warning，所以必须用 `loop` 而不是 `if`。`NodeRef` 只有三个变体（[src/node.rs:L60-L65](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L60-L65)），因为返回时 Warning 已被剥干净。

#### 4.3.3 源码精读

`node_ref` 在 [src/node.rs:L68-L79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L68-L79)：

```rust
fn node_ref(&self) -> NodeRef<'_> {
    let mut data = &self.data;
    loop {
        match data {
            Node::Leaf(text, _) => break NodeRef::Leaf(text),
            Node::Inner(inner, _) => break NodeRef::Inner(inner),
            Node::Error(err, _) => break NodeRef::Error(err),
            Node::Warning(warn, _) => data = &warn.child,
        }
    }
}
```

所有只读访问器都建立在它之上。例如 `len()`（[src/node.rs:L230-L237](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L230-L237)）和 `children()`（[src/node.rs:L277-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L277-L283)）都先 `node_ref()` 再 match 三变体——**调用方完全感知不到 Warning 的存在**。

可变版本 `inner_and_span_mut` 在 [src/node.rs:L81-L96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L81-L96)，它返回 `Option`（叶子/错误节点返回 `None`），并且刻意「同时返回内部节点和 span」：

```rust
fn inner_and_span_mut(&mut self) -> Option<(&mut InnerNode, &mut Span)> {
    let mut data = &mut self.data;
    loop {
        match data {
            Node::Leaf(_, _) | Node::Error(_, _) => break None,
            Node::Inner(inner, _) => {
                break Some((Arc::make_mut(inner), &mut self.span));
            }
            Node::Warning(warn, _) => data = &mut Arc::make_mut(warn).child,
        }
    }
}
```

它用 `Arc::make_mut` 做**写时复制**：若该子树被多方共享就先克隆一份，再返回可变引用。函数上方的注释（[src/node.rs:L81-L85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L81-L85)）解释了「为什么要把 span 一起带出来」——如果只返回 `&mut InnerNode`，它就从 `self` 借走了可变借用，调用方就再也拿不到 `&mut self.span` 了；把两者打包返回，就绕开了借用冲突。

#### 4.3.4 代码实践

**实践目标**：通过跟踪一条调用链，理解「访问器 → node_ref → 三变体 match」的层层转发。

**操作步骤**（源码阅读型实践）：

1. 在 [src/node.rs:L230-L237](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L230-L237) 阅读 `len()`。
2. 在 [src/node.rs:L277-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L277-L283) 阅读 `children()`。
3. 思考：对一个「被 Warning 包裹的 Strong 节点」调用 `children()`，会返回什么？

**需要观察的现象**：两个方法都只有一行 `match self.node_ref()`，对 `NodeRef::Inner(inner)` 取 `inner.children.iter()`。

**预期结果**：被 Warning 包裹的 Strong 节点调用 `children()`，会**穿透 Warning**，直接返回真实 Strong 的子节点（`Star`、`Text`、`Star`）。这正是「半透明」的体现——结构遍历者看不到 Warning 层。

#### 4.3.5 小练习与答案

**练习 1**：`NodeRef` 为什么没有 `Warning` 变体？

> **答案**：因为 `node_ref()` 在循环里已经把所有 Warning 层都下钻穿透了，返回时不可能再是 Warning。少一个变体让下游 match 更简单，也强制了「调用方无需关心 Warning」的抽象。

**练习 2**：`inner_and_span_mut` 为什么用 `Arc::make_mut` 而不是直接 `&mut inner.children`？

> **答案**：`Inner` 变体里存的是 `Arc<InnerNode>`，`Arc` 本身不可变。`Arc::make_mut` 在引用计数为 1 时给可变借用，否则克隆一份——这是实现「`SyntaxNode` 可廉价克隆共享、又能局部修改」的关键。

---

### 4.4 Warning 包装层：半透明包裹与下钻访问

#### 4.4.1 概念说明

`Warning` 是四种形态里最特殊的一种。它不是「另一种节点」，而是「**贴在某个已有节点上的警告便签**」：

- 它**没有自己的文本或子节点**，真实内容全在被包裹的 `child: Node` 里（[src/node.rs:L978-L985](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L978-L985)）。
- 它**复制了一份 kind**，使其等于被包裹节点的 kind——这样 `kind()` 不下钻也能给出正确答案，下游（AST、高亮）看到的仍是一个合法的 `Strong`/`Markup`/…，只是额外带了一条警告。
- 它**可以叠加**：对一个节点连续调用两次 `warn()`，就会形成两层 Warning「洋葱」。

这种设计叫「半透明包裹」：从 `kind()` 看它像被包裹节点，从 `len()/children()/leaf_text()` 看需要 `node_ref()` 下钻。

#### 4.4.2 核心流程

`warn()` 的「换芯」过程（[src/node.rs:L142-L148](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L142-L148)）：

```
1. kind = self.kind()                       // 记下当前 kind
2. child = replace(&mut self.data, 占位Leaf) // 把旧 data「偷」出来
3. warn = WarningWrapper { child, 消息 }     // 用旧 data 当孩子
4. self.data = Node::Warning(warn, kind)     // 顶层换成 Warning，kind 保留
```

下钻访问的流程（`node_ref` 等）：遇到 `Warning` 就 `data = &warn.child` 继续循环，直到碰到 `Leaf/Inner/Error`。

#### 4.4.3 源码精读

`WarningWrapper` 的定义与注释在 [src/node.rs:L971-L991](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L971-L991)，注释直接点明了它的全部设计意图：

> Warnings transparently wrap another node and do not have spans or text of their own. … their child cannot be directly found or mutated, only affected _through_ the warning, usually via the `SyntaxNode::node_ref` and `SyntaxNode::inner_and_span_mut` methods.

也就是说，Warning 的孩子**不能直接被发现或修改**，只能通过这两个下钻助手「穿过」警告去影响。`diagnosis()` 是少数会「察觉」Warning 的访问器（[src/node.rs:L285-L301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301)）：它在拿到 `node_ref` 的诊断后，若顶层是 `Warning`，就把 `warnings` 标成 `true`——这就是 `Diagnosis { errors, warnings }` 两个字段的意义。

`warn_at` 是 `warn` 的「带定位」版本（[src/node.rs:L150-L165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L150-L165)）：多了一个 `SubRange`，用来把警告指向节点文本里的某个子区间（典型场景见 U6 的 `SubRange`）。

`Debug for WarningWrapper`（[src/node.rs:L1010-L1041](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1010-L1041)）用 `debug_set`（`{...}`）而非 `debug_struct`，这样可以把被包裹的孩子作为集合的一个元素直接打印出来，直观看到「Warning 里包着一个 Strong」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手构造一个 `SyntaxNode::inner`，对它调用 `warn()`，用 `Debug` 观察警告如何包裹住原节点。

**操作步骤**：

```rust
use typst_syntax::{SyntaxKind, SyntaxNode};

// 示例代码：构造一个 Strong，再贴一条警告
let mut node = SyntaxNode::inner(
    SyntaxKind::Strong,
    vec![
        SyntaxNode::leaf(SyntaxKind::Star, "*"),
        SyntaxNode::leaf(SyntaxKind::Text, "hi"),
        SyntaxNode::leaf(SyntaxKind::Star, "*"),
    ],
);
println!("warn 前: {node:?}");

node.warn("演示警告");
println!("warn 后: {node:#?}");

// kind 仍是 Strong（未下钻）；len 仍是 6（下钻穿过 Warning）
assert_eq!(node.kind(), SyntaxKind::Strong);
assert_eq!(node.len(), 6);
```

**需要观察的现象**：

- `warn 前` 输出形如 `Strong: 6 [Star: "*", Text: "hi", Star: "*"]`。
- `warn 后` 输出形如（参照已验证的 `test_debug("**")` 用例，[src/node.rs:L1528-L1543](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1528-L1543)）：

```
Warning: {
    message: "演示警告",
    Strong: 6 [
        Star: "*",
        Text: "hi",
        Star: "*",
    ],
}
```

- 断言成立：`kind()` 不下钻仍返回 `Strong`；`len()` 下钻后仍返回 6。

**预期结果**：Warning 用 `{ message: ..., <被包裹节点> }` 的集合形式显示，原 Strong 完整地出现在花括号里；外部接口（kind/len）完全维持原值，证明 Warning 是「半透明」的。

> 说明：以上 `warn 后` 的精确格式参照 crate 内 `test_debug` 测试中 `parse("**")` 的 Warning 输出推导；若本地实现细节（如字段顺序）有细微差异，以本地实际输出为准。

#### 4.4.5 小练习与答案

**练习 1**：对同一个节点连续调用两次 `node.warn("a")` 和 `node.warn("b")`，`node.kind()` 返回什么？`node_ref()` 需要下钻几层？

> **答案**：`kind()` 仍返回被最内层包裹节点的原始 kind（因为每层 Warning 都复制了同一份 kind）。`node_ref()` 需要 loop 两轮——每轮剥掉一层 Warning，第三轮才到达真实节点。

**练习 2**：为什么 `Warning` 要复制一份 kind，而不是「下钻去拿孩子的 kind」？

> **答案**：为了让 `kind()` 保持 O(1) 且无需下钻。下游如 AST 投射、语法高亮只关心「这是个 Strong」，不需要知道它是否贴了警告；若每次 `kind()` 都要 loop 穿透可能的 Warning 层，既慢又容易出错。复制的代价只是一字节，换来的是干净的抽象边界。

---

## 5. 综合实践

把本讲的四种形态与下钻逻辑串起来，完成下面这个「诊断收集」小任务：

1. 用 `typst_syntax::parse("**")` 解析（这是 `test_debug` 用过的真实输入，会自然产生一个 Warning 包裹的 Strong）。
2. 对返回的 root 调用 `diagnosis()`，打印 `errors` 与 `warnings` 两个字段（[src/node.rs:L285-L301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L285-L301)）。
3. 调用 `errors_and_warnings()`（[src/node.rs:L304-L327](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L304-L327)），分别打印收集到的错误与警告消息。
4. 解释：为什么 `diagnosis().warnings == true`，而 `root.kind()` 仍然是 `Markup`、且遍历 `children()` 时会「穿过」Warning 看到 Strong 子树？

**预期**：你会看到 `diagnosis()` 报告「无错误、有警告」，`errors_and_warnings()` 的警告列表里有一条「no text within stars」之类的消息，同时 CST 的结构遍历完全不受 Warning 影响。这道题综合了 4.2（形态）、4.3（下钻）与 4.4（半透明）三部分。

> 关于 `errors_and_warnings()` 的细节（`SyntaxDiagnostic`、`build_diagnostic_hints`）属于下一讲 u5-l4「错误与警告诊断」，本实践只需把它当成「收集所有错误/警告」的黑盒使用。

## 6. 本讲小结

- `SyntaxNode` 是对外门面，只有 `data: Node` 和 `span: Span` 两个字段；`span` 提到顶层是为了访问高效，`SyntaxKind` 刻意留在 `Node` 内部以节省约 8 字节，代价是 Warning 层会重复一份 kind。
- `enum Node` 有四种形态：`Leaf`（文本 token）、`Inner`（带子节点的结构）、`Error`（错误，kind 固定为 `Error`）、`Warning`（半透明包裹另一节点）。前三种遵守「非 Error 变体永不为 Error kind」的不变量。
- `enum NodeRef` 是「剥掉 Warning」后的只读三变体视图，由 `node_ref()` 用 `loop` 下钻得到；`len()`/`leaf_text()`/`children()` 等访问器都建立在它之上，调用方感知不到 Warning。
- `inner_and_span_mut()` 是可变下钻助手，用 `Arc::make_mut` 做写时复制，并刻意同时返回 `&mut InnerNode` 与 `&mut Span` 以避开借用冲突。
- `Warning` 没有自己的文本/子节点/独立 kind，只复制 kind、贴上消息（与可选 `SubRange`）；它可叠加，访问时必须下钻，但 `kind()` 不下钻——这就是「半透明」。

## 7. 下一步学习建议

- **u5-l2「SyntaxNode 的构造与访问」**：继续深入 `InnerNode` 内部字段（`len`/`descendants`/`diagnosis`/`upper`）以及 `descendants()`、`leaf_text()` 等访问方法在遍历 CST 时的实际用法。
- **u5-l4「错误与警告诊断」**：本讲只点到 `diagnosis()` 与 `errors_and_warnings()` 的接口，下一讲会完整讲解 `ErrorNode`/`WarningWrapper` 如何汇总成 `SyntaxDiagnostic`，以及 `Diagnosis` 的「或/与」聚合语义。
- 阅读建议：把本讲的 `test_debug`（[src/node.rs:L1487-L1544](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1544)）作为「四种形态 Debug 输出」的权威样例反复对照，它同时覆盖了 Leaf/Inner/Error/Warning 四种打印格式。
