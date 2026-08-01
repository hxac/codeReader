# 入口函数 realize() 与核心数据类型

## 1. 本讲目标

上一讲（u1-l1）我们从「宏观定位」认识了 realization（具现化）在 Typst 编译流水线中的桥梁作用：它递归地套用样式与 show 规则，把用户可扩展的、任意嵌套的 content 内容树，规整成一张扁平的、全部由后端已知元素组成、且每个元素都带齐了样式的清单。

本讲我们从「入口」走进源码，读完之后你应当能够：

1. 逐字段读懂 `pub fn realize()` 的参数列表、返回类型和函数体初始化逻辑。
2. 说出 `RealizationKind` 的五个变体分别代表什么场景，以及每种 kind 会选用哪张分组规则表。
3. 理解返回值 `Vec<Pair<'a>>` 里的 `Pair<'a> = (&'a Content, StyleChain<'a>)` 到底是什么，以及为什么还需要一个 `Arenas` 来「延长生命周期」。

本讲只看「入口」和「类型」，不展开 `visit()` 内部的调度细节（那是下一讲 u1-l3 的主题）。

## 2. 前置知识

- **Content（内容）**：Typst 里「一切可被排版的东西」的统一表示。一段文本、一个标题、一个序列、一个带样式的元素，在编译器内部都是 `Content`。它在源码里是一个对内部类型的透明包装（`#[repr(transparent)]`），可以把它想成「内容树里的一个节点」。
- **StyleChain（样式链）**：一条由内层向外层叠加的样式栈。具现化时，每个元素都附带一条 `StyleChain`，表示「在这个位置上，有哪些样式对它生效」。
- **show 规则（show rule）**：用户用 `show ...: ...` 写下的变换规则，可以把一个元素替换成另一段内容。realization 的核心工作之一就是应用这些规则。
- **生命周期 `'a`**：Rust 的借用检查概念。本讲会看到 `'a` 专门用来标记「输入 content 与输出 pairs 共享的同一段存活时间」。如果你还不熟悉 Rust 生命周期，可以先把 `'a` 当作一个「必须对齐的存活时间标签」来理解，第 4.3 节会具体解释为什么需要它。

这些概念在 u1-l1 已有铺垫，本讲直接使用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | 本 crate 的主逻辑，包含 `realize()` 入口函数、`State` 结构体、调度循环 `visit()` 等。本讲聚焦其中的 `realize()` 与 `store()`。 |
| `crates/typst-library/src/routines.rs` | 定义了 `RealizationKind`、`FragmentKind`、`Arenas`、`Pair` 这些跨 crate 共享的核心类型。之所以放在这里，是因为 `realize` 是通过 `Routines` 函数指针表分发的。 |
| `crates/typst-library/src/foundations/content/mod.rs` | 定义 `Content` 类型本身，帮助我们理解 `Pair` 里 `&Content` 指向的对象。 |
| `crates/typst-layout/src/flow/mod.rs` | 一个真实的下游调用点，用来验证「realize 在哪里被调用、用什么 kind 调用」。 |

## 4. 核心概念与源码讲解

### 4.1 realize() 函数签名与函数体

#### 4.1.1 概念说明

`realize()` 是整个 crate 对外暴露的唯一入口（除了空格折叠模块 `spaces`）。它把「一棵 content 树 + 一条样式链」转换成「一张扁平的 pairs 清单」。

理解这个函数要从两个层面入手：

1. **契约层面**：它接收什么、返回什么。这是和下游（layout / html / bundle 等）约定的接口。
2. **初始化层面**：函数体本身并不做递归，它只是搭建好一个可变状态 `State`，然后把真正的递归工作交给 `visit()` 和 `finish()`。

#### 4.1.2 核心流程

`realize()` 的执行过程可以概括为三步：

```text
输入: kind, engine, locator, arenas, content, styles
        │
        ▼
┌─────────────────────────────────────────────┐
│ 1. 根据 kind 选用分组规则表（rules）          │
│    Bundle  → BUNDLE_RULES（空表）            │
│    Document/Fragment → FLOW_RULES           │
│    Par     → PAR_RULES                      │
│    Math    → MATH_RULES                     │
├─────────────────────────────────────────────┤
│ 2. 组装可变状态 State（sink/groupings/...）   │
├─────────────────────────────────────────────┤
│ 3. visit(content)  递归处理内容              │
│    finish()        收尾所有未完成的分组       │
└─────────────────────────────────────────────┘
        │
        ▼
输出: Vec<Pair<'a>>  （s.sink 即为输出）
```

关键点：函数体里**没有显式的递归循环**，递归全部发生在 `visit()` 内部；`realize()` 的职责是「搭舞台 + 收尾 + 把舞台里的 sink 交出去」。

#### 4.1.3 源码精读

先看完整签名与函数体：

[`crates/typst-realize/src/lib.rs:42-74`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L42-L74) —— `realize()` 入口：上面两行是文档注释和计时宏，下面是函数签名与初始化逻辑。

逐段拆解：

```rust
#[typst_macros::time(name = "realize")]
pub fn realize<'a>(
    kind: RealizationKind,
    engine: &mut Engine,
    locator: &mut SplitLocator,
    arenas: &'a Arenas,
    content: &'a Content,
    styles: StyleChain<'a>,
) -> SourceResult<Vec<Pair<'a>>> {
```

- `#[typst_macros::time(name = "realize")]` 给函数挂上计时探针，编译期可统计 realize 阶段的耗时。
- 生命周期 `'a` 是整段代码的「主轴」：`arenas`、`content`、`styles` 都标注了 `'a`，返回值 `Vec<Pair<'a>>` 也用 `'a`。这告诉编译器：**返回的 pairs 里引用的 content 与样式，其存活时间不会超过 `'a`**（详见 4.3 节）。
- 参数含义：
  - `kind`：本次具现化的场景（见 4.2 节）。
  - `engine: &mut Engine`：编译引擎，携带 world、library、introspector、错误 sink、调用路由 route 等。
  - `locator: &mut SplitLocator`：为可定位元素分配全局唯一 `Location`。
  - `arenas: &'a Arenas`：临时内存池，用于延长 show 规则产出的新内容的生命周期（见 4.3 节）。
  - `content: &'a Content`：要被具现化的内容树根节点。
  - `styles: StyleChain<'a>`：套在最外层的样式链。
- 返回 `SourceResult<Vec<Pair<'a>>>`：`SourceResult` 是「可能带源码诊断错误的结果」，成功时就是一张 pairs 清单。

接下来是函数体——搭建 `State`：

```rust
let mut s = State {
    engine,
    locator,
    arenas,
    rules: match kind {
        RealizationKind::Bundle => BUNDLE_RULES,
        RealizationKind::Document { .. } => FLOW_RULES,
        RealizationKind::Fragment { .. } => FLOW_RULES,
        RealizationKind::Par => PAR_RULES,
        RealizationKind::Math => MATH_RULES,
    },
    sink: vec![],
    groupings: ArrayVec::new(),
    outside: matches!(kind, RealizationKind::Document { .. }),
    may_attach: false,
    saw_parbreak: false,
    kind,
};
```

- `rules` 字段通过 `match kind` 选择静态规则表。这些表定义在 [`crates/typst-realize/src/lib.rs:1005-1015`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1005-L1015)，例如 `BUNDLE_RULES` 是空表 `&[]`，`FLOW_RULES` 含 TEXTUAL/PAR/CITES/LIST/ENUM/TERMS 六条规则。**不同 kind 用不同表**，这是本讲要记住的第一个关键事实。
- `sink: vec![]`：输出缓冲。最终返回的就是它。每个元素push 进来时形如 `(content, styles)`，正好是一个 `Pair`。
- `groupings: ArrayVec::new()`：分组栈，栈深有上限（`MAX_GROUP_NESTING = 3`，见 u2-l1）。
- `outside`：是否处于「任何容器/show 规则之外」。**只有 `Document` kind 初始为 `true`**，其余四种都是 `false`。这个标志后续会影响页面样式能否被提升到 page 层（见 u2-l5）。
- `may_attach`、`saw_parbreak`：两个细粒度状态位，影响空格与段落的处理（见 u3-l2）。

最后是「搭好舞台后」的递归调用与返回：

```rust
visit(&mut s, content, styles)?;
finish(&mut s)?;

Ok(s.sink)
```

- `visit()` 从 `content` 开始递归，按固定顺序尝试各种变换，把结果推进 `s.sink`。
- `finish()` 收尾：把所有还在栈上、未闭合的分组依次 finish 掉，并对顶层做空格折叠。
- `Ok(s.sink)` 把内部缓冲直接作为返回值交出去——注意这里是**移动（move）**，不是拷贝。

> 小结：`realize()` 自身只有「选表 + 建状态 + 调 visit/finish + 还 sink」四件事，真正的复杂度都在 `visit()` 及其辅助函数里。

#### 4.1.4 代码实践

**实践目标**：在 `realize()` 入口和出口加日志，直观感受一次 realize 调用的「输入 kind」与「输出规模」。

> 注意：`RealizationKind` **没有**派生 `Debug`（见 4.2 节源码），所以不能直接 `{:?}` 打印它；我们用 `match` 把它映射成字符串。这是源码的真实约束，不是偷懒。

**操作步骤**：

1. 打开 `crates/typst-realize/src/lib.rs`，在 `realize()` 函数体最开头（`let mut s = State {` 之前）插入：

   ```rust
   // —— 调试用日志（示例代码，验证完毕请删除）——
   let kind_name = match &kind {
       RealizationKind::Bundle => "Bundle",
       RealizationKind::Document { .. } => "Document",
       RealizationKind::Fragment { .. } => "Fragment",
       RealizationKind::Par => "Par",
       RealizationKind::Math => "Math",
   };
   eprintln!("[realize] entry: kind={}", kind_name);
   ```

2. 在 `Ok(s.sink)` 之前插入：

   ```rust
   eprintln!("[realize] exit:  kind={} -> {} pairs", kind_name, s.sink.len());
   ```

   > 注意 `kind` 之后会被移动进 `State`，所以这里的 `kind_name` 必须在前面那个 `match &kind` 里提前算好（用引用匹配，不会移动 `kind`），不能在出口处再去 match `kind` 本身。

3. 在仓库根目录创建一个最小文档 `hi.typ`：

   ```typ
   #highlight[Hi]
   ```

4. 用 typst CLI 编译（在仓库根目录执行）：

   ```bash
   cargo run -p typst-cli -- compile hi.typ
   ```

**需要观察的现象**：

- 终端会打印若干行 `[realize] entry: kind=...` 和 `[realize] exit: kind=... -> N pairs`。
- 至少能看到一次 `kind=Fragment` 的调用——因为 `#highlight[Hi]` 会触发一次 block 排版，而 block 排版会以 `Fragment` kind 调用 realize（见 4.2.3 节的调用点）。

**预期结果**：

- 由于 Typst 的内省（introspection）机制，realize 可能在一轮编译里被调用**多次**，所以你会看到多组 entry/exit 日志。这是正常的。
- 具体输出几 pairs 属于「待本地验证」——它取决于 show 规则展开后产生了多少个已知元素。但定性上你会看到 `Fragment` 这一行，且 pairs 数量是一个较小的正整数。

5. **验证后请务必删除这两段调试代码**，避免污染源码。

#### 4.1.5 小练习与答案

**练习 1**：`realize()` 的函数体里有递归吗？如果没有，递归发生在哪里？

> **参考答案**：函数体本身没有递归。它只搭建 `State` 并调用 `visit(&mut s, content, styles)` 与 `finish(&mut s)`，真正的递归发生在 `visit()` 内部（对序列、styled 元素、show 规则输出等的递归调用）。

**练习 2**：`s.sink` 是 `Vec<Pair<'a>>`，最后用 `Ok(s.sink)` 返回。为什么这里不会出现生命周期问题？

> **参考答案**：因为 `s.sink` 的元素类型 `Pair<'a>` 里的引用都标注了 `'a`，而 `'a` 由函数的输入参数（`arenas`、`content`、`styles`）共同确定。把 `s.sink` 移动出去时，编译器已经保证它内部引用的对象至少能活到 `'a` 结束，所以直接 move 即可。

**练习 3**：`outside` 字段的初值由 `matches!(kind, RealizationKind::Document { .. })` 决定。哪几种 kind 会让 `outside` 初始为 `true`？

> **参考答案**：只有 `Document`。`Bundle`、`Fragment`、`Par`、`Math` 都是 `false`。这意味着「页面样式可提升」这一特性默认只在文档级具现化时开启。

---

### 4.2 RealizationKind 与 FragmentKind：五种具现化场景

#### 4.2.1 概念说明

同一个 `realize()` 函数要服务于多种不同的排版场景。比如：

- 给整篇文档做顶层具现化时，要处理 `set document`、`set page` 这类只能出现在顶层的规则。
- 给一个 `block` 容器内部做具现化时，这些顶层规则就应当报错。
- 给一段数学公式内部做具现化时，连「符号元素」的处理方式都和普通文本不同。

`RealizationKind` 就是用一个枚举来告诉 `realize()`：「你现在是处在哪一种场景里」，从而决定选用哪张规则表、是否允许页面样式、是否做数学相关的特殊变换。

而 `FragmentKind` 是 `RealizationKind::Fragment` 的「附带产物」：当一个容器的内容全部是行内（inline）内容时，realize 会把结果标记为 `Inline`，让下游排版跳过强制成段的处理。

#### 4.2.2 核心流程

`RealizationKind` 与规则表的对应关系（这是本节最该记住的映射）：

| kind 变体 | 携带的可变引用 | 选用的规则表 | 典型场景 |
| --- | --- | --- | --- |
| `Bundle` | 无 | `BUNDLE_RULES`（空表） | 打包导出（bundle） |
| `Document { info }` | `&mut DocumentInfo` | `FLOW_RULES` | 整篇文档的顶层具现化 |
| `Fragment { kind }` | `&mut FragmentKind` | `FLOW_RULES` | block / html.div 等容器内部 |
| `Par` | 无 | `PAR_RULES` | 段落内部（嵌套具现化） |
| `Math` | 无 | `MATH_RULES` | 数学公式内部 |

`FragmentKind` 只有两个值：

- `Inline`：内容全是行内元素，不需要被强制包成段落。
- `Block`：内容含块级元素，行内内容已被强制收进段落。

它的赋值发生在 `finish()` 里：当发现 fragment 的内容「完全行内或中性」时，会把传入的 `&mut FragmentKind` 改写成 `Inline`。

#### 4.2.3 源码精读

`RealizationKind` 的定义（注意它**没有**派生 `Debug`）：

[`crates/typst-library/src/routines.rs:153-169`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L153-L169) —— `RealizationKind` 枚举定义，五个变体分别对应五种具现化场景。

```rust
pub enum RealizationKind<'a> {
    Bundle,
    Document { info: &'a mut DocumentInfo },
    Fragment { kind: &'a mut FragmentKind },
    Par,
    Math,
}
```

要点：

- `Document` 和 `Fragment` 各自携带一个 `&'a mut ...` 可变引用。这是因为它们需要在具现化过程中**回填信息**：`Document` 要把 `set document` 解析出的元数据写进 `DocumentInfo`；`Fragment` 要把「是否全行内」的结论写进 `FragmentKind`。
- 这种「调用方传入一个可变引用，被调用方负责填充」的设计，让 realize 可以把结果直接写回调用方的栈变量，而不必另外定义返回结构。

`FragmentKind` 的定义（它**派生了** `Debug`，所以可以 `{:?}`）：

[`crates/typst-library/src/routines.rs:171-180`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L171-L180) —— `FragmentKind` 枚举，只有 `Inline` / `Block` 两个值。

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
pub enum FragmentKind {
    Inline,
    Block,
}
```

`finish()` 中对 `FragmentKind` 的回写逻辑（仅当内容「完全行内或中性」时触发）：

```rust
if is_fully_inline_or_neutral(s) {
    if let RealizationKind::Fragment { kind } = &mut s.kind {
        **kind = FragmentKind::Inline;
    }
    ...
}
```

这段代码在 [`crates/typst-realize/src/lib.rs:788-810`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L810) 的 `finish()` 函数中。注意 `kind` 字段是 `&mut FragmentKind`，所以这里要 `**kind` 两次解引用：一次穿透 `s.kind`（匹配出的可变引用），一次穿透那个引用本身。

最后看一个真实的下游调用点，验证「Fragment kind 在哪里被使用」：

[`crates/typst-layout/src/flow/mod.rs:150-159`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L150-L159) —— layout 层在排版一个 flow 之前，以 `Fragment` kind 调用 realize。

```rust
let mut kind = FragmentKind::Block;
let arenas = Arenas::default();
let children = (engine.library.routines.realize)(
    RealizationKind::Fragment { kind: &mut kind },
    &mut engine,
    &mut locator,
    &arenas,
    content,
    styles,
)?;
```

可以看到调用方的典型写法：先在栈上声明 `let mut kind = FragmentKind::Block;` 和 `let arenas = Arenas::default();`，再把它们的可变引用/共享引用传进 realize。realize 返回后，`kind` 可能已被改写成 `Inline`，`children` 就是 `Vec<Pair>`。**`arenas` 必须在 `children` 被使用完毕之前一直存活**——这是下一节的主题。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，巩固「kind → 规则表」的映射，并定位一个真实的 realize 调用点。

**操作步骤**：

1. 在 `crates/typst-realize/src/lib.rs` 中找到四张规则表 `BUNDLE_RULES`、`FLOW_RULES`、`PAR_RULES`、`MATH_RULES`（约在 1005–1015 行），用纸笔抄下每张表里包含哪些规则名。
2. 回到 4.1.3 节里 `realize()` 的 `match kind { ... }`，对照确认：`Document` 和 `Fragment` 都选用 `FLOW_RULES`，而 `Bundle` 选用空表。
3. 打开 `crates/typst-layout/src/flow/mod.rs`，定位 4.2.3 节给出的调用点（约 152 行），确认它使用的是 `RealizationKind::Fragment`。

**需要观察的现象 / 预期结果**：

- `BUNDLE_RULES = &[]`（空），意味着 bundle 具现化不做任何分组，所有元素直接落到 sink。
- `FLOW_RULES` 元素最多（含 TEXTUAL/PAR/CITES/LIST/ENUM/TERMS）。
- `PAR_RULES` 比 `FLOW_RULES` 少了 `&PAR`（因为在段落内部不需要再把内容收进段落）。
- `MATH_RULES` 既没有 `TEXTUAL` 也没有 `PAR`（数学里行内文本/段落分组逻辑不同）。

> 本实践为「源码阅读型实践」，不涉及运行；若想运行验证，可参考 4.1.4 节的方式给规则表选择处加日志。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RealizationKind::Document` 要携带 `&mut DocumentInfo`，而 `Par` 和 `Math` 不携带任何引用？

> **参考答案**：`Document` 场景需要把 `set document` 规则解析出的标题、作者等元数据回填到一个外部变量里，所以需要可变引用；`Par` 和 `Math` 是嵌套具现化场景，不需要回填这类信息，因此是单元变体。

**练习 2**：`FragmentKind` 派生了 `Debug`，`RealizationKind` 没有。这对 4.1.4 节的日志实践有什么影响？

> **参考答案**：因为 `RealizationKind` 不实现 `Debug`，不能直接 `eprintln!("{:?}", kind)`，必须自己 `match` 出一个字符串；而 `FragmentKind` 可以直接 `{:?}` 打印（如果你想在 `Fragment` 分支里顺带打印内部 `kind` 的值）。

**练习 3**：在 4.2.3 的调用点里，为什么 `kind` 初始化为 `FragmentKind::Block` 而不是 `Inline`？

> **参考答案**：默认假设容器内容是块级的（更常见、更保守）。只有当 `finish()` 检测到内容「完全行内或中性」时，才会把它改写成 `Inline`，从而让下游走更轻量的行内排版路径。这是一种「默认安全、符合条件再优化」的策略。

---

### 4.3 Pair、Arenas 与生命周期的延长

#### 4.3.1 概念说明

`realize()` 的返回类型是 `Vec<Pair<'a>>`，其中 `Pair` 是一个类型别名：

```rust
pub type Pair<'a> = (&'a Content, StyleChain<'a>);
```

也就是说，**输出清单里的每一项，都是一个「指向某段 content 的引用 + 这段 content 在该位置生效的样式链」**。这正是下游排版（layout）所需要的最小信息：知道要排什么元素，以及它带什么样式。

但这里藏着一个 Rust 特有的难题：show 规则会在具现化过程中**凭空产生新的 content**（比如用户写的 `show heading: it => [大标题]` 会产生新的文本内容）。这些新 content 是临时的、函数内部的，而返回值却要用引用指向它们——如果它们在函数返回时就被销毁，引用就会悬空。

解决办法就是 `Arenas`：一个由调用方持有、在整个具现化（以及后续排版）期间都存活的「临时内存池」。新产出的 content 被放进 arena，从而把它的存活时间「延长」到与输入 content 一致（即 `'a`）。

#### 4.3.2 核心流程

```text
realize 执行过程中产生的 content/styles
        │
        ├── 来自输入：本来就是 &'a Content（无需转移）
        │
        └── 来自 show 规则等新生成：
                │
                ▼
        State::store(content)
                │  调用 self.arenas.content.alloc(content)
                ▼
        返回 &'a Content（生命周期被 arena 延长到 'a）
                │
                ▼
        与 styles 组成 Pair，push 进 s.sink
                │
                ▼
        随 Ok(s.sink) 返回给调用方
```

关键约束（来自 `Arenas` 的文档注释）：**「Arenas 必须在 realize 返回的 content 被处理完毕之前一直保持存活。」** 这就是为什么 4.2.3 节调用点里 `let arenas = Arenas::default();` 写在最外层、且在 `children` 用完之前都不会被销毁。

#### 4.3.3 源码精读

先看 `Content` 类型本身，理解 `Pair` 引用的对象是什么：

[`crates/typst-library/src/foundations/content/mod.rs:81-84`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L81-L84) —— `Content` 是对内部 `RawContent` 的透明包装，代表内容树中的一个节点。

```rust
#[derive(Clone, PartialEq, Hash)]
#[repr(transparent)]
pub struct Content(raw::RawContent);
```

`Content` 是 `Clone` 的，意味着它本身可以被复制；但 realize 为了效率，**输出里只放引用 `&'a Content`，而不是克隆一份拥有所有权的内容**。

再看 `Pair` 与 `Arenas` 的定义：

[`crates/typst-library/src/routines.rs:182-196`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L182-L196) —— `Arenas` 三个字段各司其职，`Pair` 是 content 引用与样式链的二元组。

```rust
#[derive(Default)]
pub struct Arenas {
    /// A typed arena for owned content.
    pub content: typed_arena::Arena<Content>,
    /// A typed arena for owned styles.
    pub styles: typed_arena::Arena<Styles>,
    /// An untyped arena for everything that is `Copy`.
    pub bump: bumpalo::Bump,
}

/// A pair of content and a style chain that applies to it.
pub type Pair<'a> = (&'a Content, StyleChain<'a>);
```

`Arenas` 有三个字段：

- `content`：typed arena，存放 show 规则新产出的 `Content`。
- `styles`：typed arena，存放新构造的 `Styles`（例如正则 show 规则的「撤销标记」`Revocation`）。
- `bump`：untyped arena（`bumpalo::Bump`），存放一切 `Copy` 类型的小对象（如 `StyleChain` 的中间节点、`Pair` 切片等）。

接着看 realize 内部是如何把新内容「装进 arena、延长生命周期」的：

[`crates/typst-realize/src/lib.rs:204-220`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L204-L220) —— `State::store` 把一段拥有所有权的 `Content` 放进 content arena，换回 `&'a Content`。

```rust
impl<'a> State<'a, '_, '_, '_> {
    /// Lifetime-extends some content.
    fn store(&self, content: Content) -> &'a Content {
        self.arenas.content.alloc(content)
    }
    ...
}
```

`store` 的核心就是一行 `self.arenas.content.alloc(content)`。`typed_arena::Arena::alloc` 的类型签名保证了它返回的引用与 arena 同生命周期；而 arena 又来自 `&'a Arenas`，所以最终拿到的引用就是 `&'a Content`——**生命周期被「提升」到了 `'a`**。于是即便这段 content 是某个 show 规则在函数深处临时算出来的，它也能安全地出现在最终的 `Vec<Pair<'a>>` 里。

> 说明：`State` 有 `'a / 'x / 'y / 'z` 四个生命周期参数，本讲只关心 `'a`（输入输出 content 的存活时间）。其余三个是因为「`&mut` 引用的不变性」被迫引入的，属于进阶话题，留到 u3-l3 专门讲解。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，确认「输出里的 `&Content` 可能指向输入，也可能指向 arena」，并理解 `store` 的作用。

**操作步骤**：

1. 在 `crates/typst-realize/src/lib.rs` 中搜索 `s.store(`，统计它被调用了多少处（例如 `visit_kind_rules`、`visit_show_rules`、各 `finish_*` 函数里都有）。
2. 对每一处调用，判断：这里被 store 的 content 是「从哪里来的」？是 show 规则的输出？还是某条 kind 规则构造的新元素？
3. 阅读 `Arenas` 的文档注释（`crates/typst-library/src/routines.rs:182-184`），把那句「Must be kept live while the content returned from realization is processed」翻译成自己的话。
4. 回看 4.2.3 节的 layout 调用点，确认 `arenas` 的声明位置确实覆盖了 `children` 的整个使用范围。

**需要观察的现象 / 预期结果**：

- 你会发现 `s.sink.push((content, styles))` 里的 `content` 有两种来源：要么是直接来自输入树（如序列/标签元素，本就是 `&'a Content`），要么是通过 `s.store(...)` 从 arena 拿到的引用。两者都能统一成 `&'a Content`，所以才能装进同一个 `Vec<Pair<'a>>`。
- 如果调用方在 `children` 还没用完时就 drop 了 `arenas`，理论上会触发 use-after-free——Rust 的借用检查器通过 `'a` 在编译期就阻止了这种错误。

> 本实践为「源码阅读型实践」。若想运行验证，可以在 `store()` 里加 `eprintln!("[store] {}", content.elem().name());`，观察哪些元素是「新生成并存进 arena」的。

#### 4.3.5 小练习与答案

**练习 1**：`Pair<'a>` 里的 `&'a Content` 可能指向两处不同的地方，分别是哪两处？

> **参考答案**：① 指向输入 content 树里原本就存在的节点（输入参数 `content: &'a Content` 及其子节点，它们本就是 `'a`）；② 指向 `Arenas::content` arena 里的节点（由 `State::store` 在具现化过程中分配，生命周期被延长到 `'a`）。

**练习 2**：如果把 `Arenas` 的 `content` 字段改成普通 `Vec<Content>`，并用 `vec.push(content)` 然后 `&vec.last().unwrap()` 来获取引用，会带来什么问题？

> **参考答案**：`Vec` 在扩容时会移动所有元素，导致之前返回的引用全部失效（借用检查器会拒绝 `&vec[..]` 与 `vec.push` 同时存在）。而 `typed_arena::Arena` 的设计保证了已分配对象的地址永不移动（earlier allocations are never moved），因此可以稳定地返回长生命周期引用。

**练习 3**：为什么 `Arenas` 要同时准备 `typed_arena` 和 `bumpalo::Bump` 两种 arena，而不是只用一种？

> **参考答案**：`typed_arena::Arena<T>` 是类型安全的、专门为某一类对象（`Content` / `Styles`）延长生命周期；而 `bumpalo::Bump` 是 untyped、适合批量分配各种 `Copy` 小对象且能整体回收复用（例如 `store_slice` 用 `BumpVec` 以便复用空间）。两者分工不同，分别满足「类型安全的对象持有」与「高效的临时小对象分配」两种需求。

---

## 5. 综合实践

**任务**：用一篇讲义里学到的「入口 + 类型」知识，亲手观测一次完整的 realize 调用，并建立「文档内容 → 触发的 kind → 输出规模」的直觉。

**操作步骤**：

1. 在 `realize()` 入口与出口加上 4.1.4 节给出的两段日志（注意用 `match &kind` 提前算好 `kind_name`）。
2. 准备四个最小文档，分别侧重不同场景：

   ```typ
   # hi1.typ —— 纯文本
   Hello Typst.
   ```

   ```typ
   # hi2.typ —— 行内样式（会触发 Fragment）
   #highlight[Hi]
   ```

   ```typ
   # hi3.typ —— 列表（会触发 LIST 分组）
   - Item one
   - Item two
   ```

   ```typ
   # hi4.typ —— 数学（会触发 Math kind）
   $a^2 + b^2 = c^2$
   ```

3. 分别编译它们，收集日志：

   ```bash
   cargo run -p typst-cli -- compile hi1.typ
   cargo run -p typst-cli -- compile hi2.typ
   cargo run -p typst-cli -- compile hi3.typ
   cargo run -p typst-cli -- compile hi4.typ
   ```

4. 整理一张表格，记录每个文档触发了哪些 `kind`、各自的 `pairs` 数量。

**预期结果**：

- 你会观察到 realize 的调用是「嵌套」的：一个 `Fragment`（或 `Document`）调用内部，可能还会再触发 `Par`、`Math` 等嵌套调用。
- `pairs` 数量会因内省迭代而出现重复的 entry/exit 行——这正好印证了 realize 在一轮编译里可能被多次调用。
- 具体数字「待本地验证」，但你应当能解释每一条日志来自 4.x 节里的哪段逻辑。

5. **完成后务必删除所有调试日志**，不要修改任何功能代码。

## 6. 本讲小结

- `realize()` 的函数体只做三件事：根据 `kind` 选规则表、搭建 `State`、调用 `visit()` 与 `finish()`，最后把 `s.sink` 作为 `Vec<Pair<'a>>` 返回。
- `RealizationKind` 有五个变体（`Bundle` / `Document` / `Fragment` / `Par` / `Math`），每个变体对应一张分组规则表；`Document` 与 `Fragment` 还分别携带 `&mut DocumentInfo` 和 `&mut FragmentKind` 用于回填信息。
- `FragmentKind` 只有 `Inline` / `Block` 两值，当 fragment 内容完全行内时，`finish()` 会把它改写成 `Inline`。
- `Pair<'a> = (&'a Content, StyleChain<'a>)` 是输出清单的基本单元；其中的 `&Content` 可能指向输入树，也可能指向 `Arenas`。
- `Arenas` 用 `typed_arena` 与 `bumpalo::Bump` 延长 show 规则新产出内容的生命周期到 `'a`，且必须在返回结果被处理完毕前一直存活。
- `outside` 标志初始仅对 `Document` kind 为 `true`，决定了页面样式能否被提升到 page 层。

## 7. 下一步学习建议

本讲只打开了 `realize()` 的「大门」，还没有进入调度循环内部。下一讲 **u1-l3「visit() 调度流水线总览」** 将带你看清 `visit()` 是如何按固定顺序依次尝试「TagElem 直推 → kind 规则 → show 规则 → 序列递归 → styled 递归 → 分组 → 过滤 → 落入 sink」的，这是理解整个 realize 子系统行为的关键。

在进入 u1-l3 之前，建议你：

- 回顾本讲 4.1.3 节里 `visit()` 的两行调用 `visit(&mut s, content, styles)?;` 和 `finish(&mut s)?;`，带着「visit 到底对单个元素做了哪些尝试」的问题去读下一讲。
- 可选预习：快速浏览 `crates/typst-realize/src/lib.rs` 中 `fn visit(...)` 的函数体（约 242–294 行），先自己猜测每个 `if` 分支的作用，再去 u1-l3 对答案。
