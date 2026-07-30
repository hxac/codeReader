# 块级/行内/数学片段的递归编译

## 1. 本讲目标

上一讲我们打开了 `convert_to_nodes` 这个「黑盒」，看到它把一段具象化后的 `Content` 流式翻译成 `HtmlNode`。但你一定会注意到一个关键现象：当 `handle_html_elem` 遇到一个嵌套的 HTML 元素（比如 `html.div`）时，它并没有直接就地翻译元素体，而是调用了三个外部函数之一来「递归编译」这一段内容——这就是本讲的主角 **fragment（片段）编译器**。

`src/fragment.rs` 提供了三个入口，分别对应三种编译语境：

- `html_block_fragment`：块级语境（如 `<div>`、`<p>` 的内容）
- `html_inline_fragment`：行内语境（如 `<span>`、`<a>` 的内容）
- `html_math_fragment`：数学语境（MathML 元素的内容）

学完本讲，你应当能够：

1. 说清这三个 fragment 入口的**签名差异**与各自承担的语境职责。
2. 解释**为什么只有 block 片段被 comemo 缓存**，而 inline / math 片段不缓存。
3. 理解 `SmartQuoter` 这个可变的智能引号状态如何**在行内语境中跨元素共享**，以及它为什么与 memoize 天然冲突。
4. 掌握 `realize_fragment` 这个公共具象化助手，以及数学片段为何要走一条**不走它**的特殊路径。
5. 理解 `route.check_html_depth()` 如何在递归编译中**防止栈溢出**。

## 2. 前置知识

本讲建立在 u3-l3（`convert_to_nodes` 内容转换器）之上。在继续前，请确保你熟悉以下概念：

- **`ConversionLevel`**：上一讲引入的「翻译层级」枚举，有两个变体——`Block`（自带一个本地 `SmartQuoter`）和 `Inline(&mut SmartQuoter)`（借用外层的引号机）。它正是 block / inline 两种语境在类型层面的体现。
- **`realize`（具象化）**：把抽象的 Typst `Content`（含 show 规则、函数调用）展开成一串「已具象、可直接处理」的元素序列（`Pair` 列表）。它在 u3-l1 讲过；本讲中 `realize_fragment` 是它的一个针对 fragment 的封装。
- **`comemo::memoize`**：Typst 全仓库使用的记忆化（缓存）机制。被 `#[comemo::memoize]` 标注的函数，其结果会按**入参的哈希值**缓存；只要入参相同就跳过重算。它的前提是：**结果必须完全由入参决定**（纯函数）。
- **`Route`（调用路由）**：编译期追踪「我现在嵌套到第几层」的结构，用一条链表记录调用深度，用于检测无限递归。

一个直觉性的比喻：`convert_to_nodes` 是一条装配流水线，把零件（元素）逐个焊到 HTML 树上。但当流水线遇到一个「盒子里的盒子」（嵌套 HTML 元素）时，它不会用同一条流水线硬塞，而是**新开一条子流水线**来处理盒子内部——子流水线的「配置」取决于这个盒子是块级、行内还是数学语境。fragment 函数就是这些「子流水线」的启动器。

## 3. 本讲源码地图

本讲涉及的核心文件：

| 文件 | 作用 |
|------|------|
| `src/fragment.rs` | **本讲主战场**。定义三个 fragment 入口、`realize_fragment` 公共助手，以及各自的缓存/状态策略。 |
| `src/convert.rs` | fragment 的**调用方**。`handle_html_elem`、`handle_block`、`handle_box` 三处在需要递归编译时调用对应 fragment；还定义了 `ConversionLevel` 枚举。 |
| `crates/typst-library/src/engine.rs` | `Route` 与 `check_html_depth` 的定义，是递归深度保护的来源。 |
| `crates/typst-library/src/routines.rs` | `RealizationKind` 枚举（`Fragment` / `Math` 等），决定具象化时是否做段落分组。 |
| `crates/typst-library/src/text/smartquote.rs` | `SmartQuoter` 结构，智能引号的共享可变状态本体。 |

## 4. 核心概念与源码讲解

### 4.1 `html_block_fragment`：独立上下文与 comemo 缓存

#### 4.1.1 概念说明

`html_block_fragment` 处理的是「块级语境」的内容——也就是默认 `display: block` 的 HTML 元素（如 `<div>`、`<section>`）的内部，以及 Typst 的 `block` 元素。

块级语境有两个关键特征：

1. **它是自包含的（self-contained）**：块级元素内部的智能引号状态、空白保护逻辑与外部相对独立。一个 `<div>` 里的引号开合，不该影响到 `<div>` 外面的引号判断。
2. **它的输入是「干净的」**：调用方只需要提供 `Content` 和样式，不需要传入任何可变借用（如 `&mut SmartQuoter`）。

正因为「自包含 + 输入干净」，block 片段非常适合做**记忆化缓存**：相同的内容在相同样式下，编译出的 HTML 节点必然相同。于是 `html_block_fragment` 被包在 `#[comemo::memoize]` 里。

#### 4.1.2 核心流程

block 片段采用 typst 仓库里**标准的 memoize 三明治结构**（与 `html_document` 完全同构）：

```
html_block_fragment（公开入口，&mut Engine）
   │  把 Engine 拆解成一堆 Tracked/TrackedMut 标量参数
   ▼
html_block_fragment_impl（#[comemo::memoize]，缓存边界）
   │  重新组装一个本地 Engine，复用世界/库/内省器
   ├── Route::extend(route)         // 新建一段路由
   ├── route.check_html_depth()     // 检查递归深度
   ├── realize_fragment(...)        // 具象化 body
   └── convert_to_nodes(..., ConversionLevel::Block, ...)  // 翻译为节点
```

注意几个细节：

- **拆 Engine 的原因**：`comemo::memoize` 要求入参可哈希、可比较，但 `Engine` 是个含可变借用的大结构，不能直接哈希。所以公开入口把 `Engine` 拆成 `world`、`library`、`introspector`、`traced`、`sink`、`route` 这些「可追踪（Tracked）」的句柄，逐个传给被 memoize 的 impl。
- **`locator: Locator`（owned）**：block 入口接收一个**完整的 `Locator`**（而非 `&mut SplitLocator`），在 impl 内部用 `LocatorLink::new` + `Locator::link(&link).split()` 派生出一棵**全新的定位子树**。这正是「自包含上下文」的体现——块级内容拥有独立的定位空间。
- **`ConversionLevel::Block`**：传给 `convert_to_nodes`，意味着这次翻译会自己 `new` 一个 `SmartQuoter`，不与外界共享。

#### 4.1.3 源码精读

公开入口：拆解 `Engine` 并转发。注意它接收的是 owned `Locator`（第 21 行），而 inline 入口接收的是 `&mut SplitLocator`——这是两者的第一个签名差异。

[fragment.rs:17-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L17-L37) —— `html_block_fragment` 公开入口：把 `&mut Engine` 拆成若干 `Tracked` 句柄，转发给带缓存的内层实现。

被 memoize 的 impl：重新拼装 `Engine`，新建路由段、检查深度、具象化、翻译。

[fragment.rs:39-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L39-L77) —— `html_block_fragment_impl`：缓存边界。`Route::extend(route)` 新建一段路由；`convert_to_nodes` 用 `ConversionLevel::Block` 翻译。

其中检查深度的那一行：

[fragment.rs:66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L66) —— 在具象化之前先 `check_html_depth()`，超出最大 HTML 嵌套深度就直接报错（见 4.5）。

谁会调用 block 片段？主要有两个调用点：

- `handle_html_elem` 中，当元素标签的默认 display 是 `Block` 时（[convert.rs:197-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L197-L204)），走 `html_block_fragment`，并在结束后把 converter 的 `quoter` 重置为新的（[convert.rs:210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L210)），呼应「块级元素重置行内状态」的语义。
- `handle_block` 处理 Typst 的 `block` 元素时（[convert.rs:414-420](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L414-L420)），也调用 `html_block_fragment`。

#### 4.1.4 代码实践

**实践目标**：验证 block 片段是 memoize 缓存的，且每次调用会新建独立 `SmartQuoter`。

**操作步骤（源码阅读型）**：

1. 打开 `src/fragment.rs`，定位 `#[comemo::memoize]` 注解（第 40 行），确认它标注在 `html_block_fragment_impl` 上而非公开入口上。
2. 对照 `src/document.rs` 中 `html_document_impl` 的写法，确认二者是同一种「外层拆 Engine + 内层 memoize」的三明治模式。
3. 在 `src/convert.rs` 的 `convert_to_nodes` 中找到第 73 行 `ConversionLevel::Block => &mut SmartQuoter::new()`，确认：**只要传 `Block`，就会临时 `new` 一个全新的 quoter**，绝不借用外层。

**需要观察的现象**：

- block 片段的结果只依赖入参（content/styles/whitespace 等），因此 memoize 能命中。
- `quoter` 是 converter 的局部可变状态，不进入缓存 key，也不出现在返回值里。

**预期结果**：你能用自己的话解释——「block 片段之所以可缓存，是因为它的所有可变状态（quoter、trailing 空白）都是临时的、不跨调用泄漏的」。

#### 4.1.5 小练习与答案

**练习 1**：`html_block_fragment` 的公开入口为什么不是 `#[comemo::memoize]`，而是另起一个 `_impl` 函数来缓存？

**参考答案**：公开入口的第一个参数是 `&mut Engine`，不可哈希、不可比较，无法作为 memoize 的 key。把 Engine 拆成若干 `Tracked`/`TrackedMut` 句柄后，每个句柄都是可哈希的，才能成为合法的缓存入参。这也是 typst 全仓库统一采用的 memoize 模式。

**练习 2**：`handle_html_elem` 在调用完 `html_block_fragment` 之后做了一行 `*converter.quoter = SmartQuoter::new();`（convert.rs:210）。既然 block 片段内部本就有独立 quoter，为什么外层 converter 还要再重置一次？

**参考答案**：外层 converter（比如正在翻译一个段落的那个 converter）可能已经累积了一些行内引号状态；现在中间插进来一个块级元素，相当于在行内流里「断开」了语境，块级之后的引号理应从零开始判断，所以把外层 quoter 复位。源码注释也指出这段目前难以被测试覆盖（convert.rs:206-209）。

---

### 4.2 `html_inline_fragment`：共享智能引号与放弃缓存

#### 4.2.1 概念说明

`html_inline_fragment` 处理「行内语境」——默认 `display` 为行内（`inline`/`inline-block`/`contents` 等）的 HTML 元素（如 `<span>`、`<a>`、`<em>`）的内部，以及 Typst 的 `box` 元素。

行内语境与块级语境的根本区别在于：**行内内容是「流」的一部分**。考虑这样一个 Typst 段落（伪代码示意，非可运行代码）：

```
He said, "hello <em>wor<code>ld</code></em>" loudly.
```

这里 `"hello ... ld"` 这对引号横跨了 `<em>` 和 `<code>` 两个嵌套元素。当翻译到 `<em>` 内部时，**必须知道引号已经在前文打开过**，才能正确判断当前遇到的引号是「开」还是「闭」。也就是说：行内片段必须与它周围的行内内容**共享同一个智能引号状态**。

这个共享状态就是 `SmartQuoter`——一个含 `depth` 和 `kinds` 两个可变字段的小结构，记录「当前打开了哪些引号、分别是单引还是双引」。它通过 `&mut SmartQuoter` 在调用方与 fragment 之间传递。

#### 4.2.2 核心流程

```
html_inline_fragment（公开入口，&mut Engine, &mut SplitLocator, &mut SmartQuoter, ...）
   ├── engine.route.increase()          // 就地把当前路由段 +1
   ├── route.check_html_depth()         // 检查深度
   ├── realize_fragment(...)            // 具象化（与 block 用同一个助手）
   └── convert_to_nodes(..., ConversionLevel::Inline(quoter), ...)
   └── engine.route.decrease()          // 归还时 -1
```

关键点：

- **没有 `#[comemo::memoize]`**：函数直接就是公开函数本身，没有缓存壳。
- **借用 `&mut SplitLocator`**：与父级共享同一条定位流，而不是新开子树（因为行内内容属于同一个流式上下文）。
- **借用 `&mut SmartQuoter`**：这是核心——引号状态可变地流过调用链。

#### 4.2.3 源码精读

先看 inline 入口上方那段至关重要的文档注释，它**亲口解释了为什么不缓存**：

[fragment.rs:82-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L82-L86) —— 作者原话：行内内容需要与周围内容共享 smartquoting 状态，而「可变状态与 memoize 天然冲突」；何况每个小片段都缓存的话粒度也太细了。

入口实现本身：

[fragment.rs:87-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L87-L111) —— `html_inline_fragment`：`increase`/`decrease` 就地调整路由深度，`convert_to_nodes` 用 `ConversionLevel::Inline(quoter)` 把外层 quoter 透传进去。

共享的 `SmartQuoter` 本体定义在 typst-library：

[smartquote.rs:99-106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L99-L106) —— `SmartQuoter` 用 `depth: u8` + `kinds: u32` 的位栈记录引号开合，最多支持 32 层嵌套。

它的 `quote` 方法在每次遇到智能引号时被调用，会根据「前一个字符」和「当前栈顶状态」决定开/闭/撇号，并**修改自身**（push/pop）：

[smartquote.rs:116-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L116-L155) —— `SmartQuoter::quote`：这是「零前瞻」的引号判定，`&mut self` 表明它每次调用都会改变状态。

正是这个 `&mut self`，决定了它**无法成为 memoize 的入参**：两次「内容完全相同」的 inline 调用，如果前文引号状态不同，产出的引号字符就不同——结果不纯，缓存即错误。

调用点：`handle_html_elem` 的 else 分支（[convert.rs:220-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L220-L228)），以及 `handle_box` 处理 `box` 元素时（[convert.rs:344-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L344-L351)）——两处都把 `converter.quoter` 以 `&mut` 形式传下去。

#### 4.2.4 代码实践

**实践目标**：理解「相同内容、不同前文引号状态 → 不同输出」，从而体会为何 inline 不能缓存。

**操作步骤（源码阅读型）**：

1. 读 `convert.rs` 的 `handle` 中 `SmartQuoteElem` 分支（[convert.rs:121-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L121-L135)），确认它调用 `converter.quoter.quote(...)`，并传入「前一个字符」`last_char(&converter.output)`。
2. 读 `SmartQuoter::quote`（smartquote.rs:116-155）的判定逻辑：当 `opened == Some(double)` 且前字符非空白时，会 `self.pop()` 并返回**闭引号**；否则 `self.push()` 返回**开引号**。

**需要观察的现象**：

- 同样一个 `"`（智能双引号），如果栈顶已经有一对未闭合的双引（`opened == Some(true)`），它就被判定为「闭」；否则被判定为「开」。
- 这意味着 `SmartQuoter` 的输出**依赖调用历史**，是一个有状态的机器。

**预期结果**：你能构造一个口头例子说明——`"a"` 与 `"a"` 两段相同文本，放在不同引号上下文里，编译出的 HTML 引号字符可能一个是 `“`（开）一个是 `”`（闭）。这正是「状态污染」使缓存失效的本质。

> 待本地验证：如果你想亲眼看到差异，可编写两段 Typst，分别在前后包一层已开/未开的引号环境，导出 HTML 后对比引号字符。本讲不假设已运行。

#### 4.2.5 小练习与答案

**练习 1**：`html_inline_fragment` 用 `engine.route.increase()` / `decrease()` 来记录深度，而 block 片段用 `Route::extend(route)`。为什么 inline 不能也用 `extend`？

**参考答案**：`increase`/`decrease` 是对**当前拥有的 `&mut Route`** 的就地加减，要求函数能拿到可变的 route。inline 入口直接持有 `&mut Engine`，所以能就地改 `engine.route`。block 的 impl 是被 memoize 的，其 `route` 入参是 `Tracked<Route>`（不可变追踪句柄），无法就地改，只能用 `Route::extend` 另建一段新路由。两条路殊途同归，都让深度 +1。

**练习 2**：作者在注释里说「即便不考虑可变状态，每个小片段都缓存的粒度也太细了」。请从缓存命中率的角度解释这句话。

**参考答案**：行内片段通常很小（一句话、一个短语），且高度依赖上下文。即便抛开 quoter 不谈，把它们逐一 memoize 会产生海量小缓存条目，查询/存储开销可能超过重算收益，命中率却很低（因为相同的小片段在相同样式下重复出现的概率有限）。block 片段体积大、上下文独立，缓存收益更高，所以只缓存它。

---

### 4.3 `html_math_fragment`：数学专用实现路径

#### 4.3.1 概念说明

`html_math_fragment` 处理 MathML 元素（如 `<math>`、`<mfrac>`、`<mrow>`）的内部内容。它的签名与 `html_inline_fragment` **完全相同**（同样借 `&mut SplitLocator` 和 `&mut SmartQuoter`），但走了一条**不同的具象化路径**。

差别在于：普通文本具象化会做**段落分组（paragraph grouping）**——把零散的行内内容归拢成 `<p>` 段落；而数学公式内部不该被强行分组（一个分数里的符号不能被包进 `<p>`）。所以数学片段使用 `RealizationKind::Math`，让 realize 跳过段落分组逻辑。

#### 4.3.2 核心流程

```
html_math_fragment（与 inline 同签名）
   ├── engine.route.increase()
   ├── route.check_html_depth()
   ├── (engine.library.routines.realize)(RealizationKind::Math, ...)   // 直调 realize，不走 realize_fragment！
   └── convert_to_nodes(..., ConversionLevel::Inline(quoter), ...)
   └── engine.route.decrease()
```

注意第三步：math 片段**直接调用** `engine.library.routines.realize`，传入 `RealizationKind::Math`，而不是调用 `realize_fragment`（后者固定用 `RealizationKind::Fragment`）。这是它和 block/inline 的最大区别。

#### 4.3.3 源码精读

math 入口的文档注释点明了用意：

[fragment.rs:113-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L113-L115) —— 「Uses math realization so that paragraph grouping doesn't occur.」（用数学实现，避免段落分组）。

入口实现：注意第 129 行直接调 `realize`，第 130 行传 `RealizationKind::Math`：

[fragment.rs:117-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L117-L147) —— `html_math_fragment`：签名与 inline 完全一致，但具象化走 `RealizationKind::Math`；翻译层仍用 `ConversionLevel::Inline(quoter)`，说明数学内部仍共享外层引号状态。

`RealizationKind` 各变体的语义（对照理解为什么 math 要单列）：

[routines.rs:153-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L153-L169) —— `RealizationKind`：`Document`/`Fragment`/`Par`/`Math`/`Bundle` 五种实现语境。`Fragment` 会做段落分组（产出 `FragmentKind::Inline` 或 `Block`），`Math` 则不会。

调用点：`handle_html_elem` 中，当标签属于 MathML 命名空间时（[convert.rs:211-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L211-L219)），走 `html_math_fragment`。判定函数是 `tag::mathml::is_mathml`（[tag.rs:594](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L594)）。

#### 4.3.4 代码实践

**实践目标**：通过对比 `RealizationKind` 理解「段落分组」对数学公式的危害。

**操作步骤（源码阅读型）**：

1. 读 `RealizationKind::Fragment` 的文档（routines.rs:161-164），注意它「要求一个可变引用，会在内容完全行内时被设为 `FragmentKind::Inline`」——这说明 Fragment 实现会**判断并可能插入段落**。
2. 读 `RealizationKind::Math`（routines.rs:167-168），它没有这类要求，专用于数学。
3. 假设性推演：如果把 `html_math_fragment` 改成调用 `realize_fragment`（即用 `Fragment` 实现），一个 `<mfrac><mi>x</mi><mi>y</mi></mfrac>` 内部的 `x`、`y` 可能被错误地包进 `<p>`，破坏 MathML 结构。

**需要观察的现象**：`Math` 与 `Fragment` 在「是否分组」上的差异，是 math 片段必须单列一条路径的根本原因。

**预期结果**：你能说清——「math 片段复用 inline 的引号共享与不缓存策略，但替换了具象化种类，以避免段落分组污染公式结构」。MathML 节点的真正生成（`<mfrac>` 等）由 `mathml.rs` 负责，留待 u5-l5 详讲。

#### 4.3.5 小练习与答案

**练习 1**：`html_math_fragment` 和 `html_inline_fragment` 签名一模一样，为什么不直接复用 inline 函数、仅多传一个「是否数学」的布尔参数？

**参考答案**：分离成两个函数让意图更清晰，并且数学路径「不走 `realize_fragment`、直调 realize 并传 `RealizationKind::Math`」这一差异被显式编码在函数体里，而不是靠布尔分支散落各处。两个函数各自短小、职责单一，可读性更好。

**练习 2**：math 片段传给 `convert_to_nodes` 的是 `ConversionLevel::Inline(quoter)`。这说明数学公式内部的智能引号也会参与外层共享。这在数学语境下合理吗？

**参考答案**：合理。MathML 中也存在文本节点（如 `<mi>`、`<mo>`、`<mtext>`），其中可能含撇号或引号；让它们与周围行内内容共享 quoter，能保证开闭判断一致。数学语境在「翻译层」上表现得像行内内容，只在「具象化层」上特殊（不分组）。

---

### 4.4 `realize_fragment`：块级/行内的公共具象化助手

#### 4.4.1 概念说明

block 片段和 inline 片段在「具象化」这一步用的是**同一个**私有助手 `realize_fragment`。它是对 `engine.library.routines.realize` 的薄封装，固定传入 `RealizationKind::Fragment`，并忽略 realize 回填的 `FragmentKind`。

为什么 block 和 inline 共用？因为「是否分组」这件事，对二者来说处理是统一的——`Fragment` 实现会自动判断内容是否纯行内，并在需要时把非行内内容强制塞进段落。block 和 inline 的真正差异体现在**翻译层**（`ConversionLevel`）和**状态共享**（quoter）上，而非具象化层。

#### 4.4.2 核心流程

```
realize_fragment(engine, locator, arenas, content, styles)
   └── (engine.library.routines.realize)(
           RealizationKind::Fragment { kind: &mut FragmentKind::Block },  // 固定 kinds
           engine, locator, arenas, content, styles,
       ) -> Vec<Pair>
```

注意它把 `kind` 写死为 `&mut FragmentKind::Block`，并且**不读取** realize 回填的结果——注释明说「我们忽略 `FragmentKind`，因为我们对两者统一处理」。

#### 4.4.3 源码精读

[fragment.rs:149-168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L149-L168) —— `realize_fragment`：固定用 `RealizationKind::Fragment`，`kind` 写死为 `FragmentKind::Block` 且忽略回填。

`FragmentKind` 枚举本身（realize 据此回填，但本助手丢弃它）：

[routines.rs:171-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L171-L180) —— `FragmentKind`：`Inline`（内容纯行内）/`Block`（含非行内，已被强制分组）。typst-html 选择忽略它，统一按「可能含块」来翻译。

为什么敢忽略？因为后续 `convert_to_nodes` + `ConversionLevel` 会正确处理任意混合的节点序列——无论 realize 是否插入了段落，翻译器都能逐个 `handle`。所以 `FragmentKind` 这个信号对 typst-html 没有用处（它对 PDF 分页路径才有意义）。

#### 4.4.4 代码实践

**实践目标**：确认 block 与 inline 共用 `realize_fragment`，而 math 不用它。

**操作步骤（源码阅读型）**：

1. 在 `fragment.rs` 中搜索 `realize_fragment(` 的调用点：应只有两处——`html_block_fragment_impl`（第 69 行）和 `html_inline_fragment`（第 100 行）。
2. 确认 `html_math_fragment`（第 129 行）**没有**调用 `realize_fragment`，而是直接调 `realize` 并传 `RealizationKind::Math`。

**需要观察的现象**：三个片段中，两个共用具象化助手，一个走独立路径——这条「二对一」的分界线，恰好对应「是否需要段落分组」。

**预期结果**：你能画出调用关系：`realize_fragment` ← block、inline；`realize(Math)` ← math。

#### 4.4.5 小练习与答案

**练习 1**：`realize_fragment` 把 `kind` 写死成 `FragmentKind::Block`。既然要忽略回填值，为什么还要传一个 `&mut` 引用进去？

**参考答案**：`RealizationKind::Fragment` 的定义要求传入一个 `&mut FragmentKind`（routines.rs:164），realize 会通过它回填「实际是否纯行内」。这是 realize 接口的硬性要求，调用方必须提供一个可写位置。typst-html 虽然不关心回填值，但仍需提供这个 `&mut` 来满足接口，于是就地造一个 `FragmentKind::Block` 临时变量传进去。

**练习 2**：如果未来 typst-html 想利用 `FragmentKind`（比如纯行内时跳过某些块级优化），需要改哪里？

**参考答案**：在 `realize_fragment` 里不再忽略回填，而是把 `FragmentKind` 返回给调用方；block/inline 入口据此分支处理。但目前的设计是「统一忽略」，体现了「先正确、后优化」的取舍。

---

### 4.5 `check_html_depth`：递归深度保护

#### 4.5.1 概念说明

三个片段都是**递归**的：编译一个 HTML 元素时，会调用片段去编译它的子元素；子元素若是又一个 HTML 元素，又会触发片段……如果用户写了一个无限自我引用的 show 规则（比如 `show: it => html.div(it)`），或者结构嵌套过深，就会无限递归直至**栈溢出**崩溃。

`Route`（调用路由）是一条记录「我现在嵌套到第几层」的链表。每进入一个片段，深度 +1；每退出，深度 -1。`check_html_depth()` 在进入片段体之前检查：当前深度是否已超过允许上限 `MAX_HTML_DEPTH`（72）。超过就**主动报错**，而非让栈溢出默默崩溃。

#### 4.5.2 核心流程

深度追踪的两种写法（殊途同归，都让深度 +1）：

- **block（被 memoize 的 impl）**：`Route::extend(route)` 新建一段长度为 1 的路由段。
- **inline / math（直接持 `&mut Engine`）**：`engine.route.increase()` 把当前段长度 +1，退出时 `decrease()` -1。

检查动作三者一致：

```
route.check_html_depth().at(content.span())?;
//   └─ 若 within(MAX_HTML_DEPTH) 为假 → 返回错误「maximum HTML depth exceeded」
```

#### 4.5.3 源码精读

三个片段里的检查点（注意它们都在 `realize` 之前）：

- block：[fragment.rs:66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L66)
- inline：[fragment.rs:97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L97)
- math：[fragment.rs:126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L126)

`check_html_depth` 与各深度上限的定义：

[engine.rs:340-385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L340-L385) —— 各类最大嵌套深度（`MAX_HTML_DEPTH = 72`）及对应的 `check_*_depth` 方法。HTML 深度（72）介于 show 规则（64）与函数调用（80）之间；注释说明这样设置是为了让不同类错误有明确的优先级。

深度计算逻辑（`within` 沿路由链表累加 `len`）：

[engine.rs:404-428](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L404-L428) —— `Route::within`：递归地把各段的 `len` 求和，与上限比较；并用一个 `upper` 原子变量做缓存优化，避免每次都遍历整条链。

`extend` 与 `increase`/`decrease` 的定义：

[engine.rs:294-302](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L294-L302) —— `Route::extend`：以 `outer` 链上原路由，新建一段 `len: 1`。

[engine.rs:325-333](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L325-L333) —— `increase`/`decrease`：就地加减当前段 `len`。

#### 4.5.4 代码实践

**实践目标**：理解深度上限如何把「无限递归」转化为「友好报错」。

**操作步骤（源码阅读型）**：

1. 读 `check_html_depth`（engine.rs:377-384），看清错误信息与提示：「maximum HTML depth exceeded; hint: try to reduce the amount of nesting of your HTML」。
2. 对比同文件里的 `check_show_depth`（engine.rs:354-363）与 `check_call_depth`（engine.rs:388-393），理解 typst 用**不同上限**区分不同递归来源，并用注释（engine.rs:336-339）解释优先级。

**需要观察的现象**：三类深度上限各不相同（show=64、html/layout=72、call=80），且 HTML 检查在**每个片段入口**都执行一次。

**预期结果**：你能解释——一个自引用的 `show: it => html.div(it)` 规则会在大约 72 层嵌套时被 `check_html_depth` 拦下，给出明确错误，而不是栈溢出崩溃。

#### 4.5.5 小练习与答案

**练习 1**：为什么深度上限要区分 show/html/call 三种，而不是统一一个数？

**参考答案**：为了让报错信息更准确。当 show 规则递归和函数调用递归同时存在时，较低的对应上限会先触发，从而把错误归因到「最可能的原因」。例如 show 规则上限最低（64），所以 show 自引用问题会优先报「maximum show rule depth exceeded」，提示更对题。

**练习 2**：`within` 里那个 `upper` 原子变量（`AtomicUsize`）起什么作用？

**参考答案**：它是一个「已知安全的上限」缓存。如果某次发现整条链的深度远小于阈值，就把这个宽松值记进 `upper`；后续查询若 `len + upper <= depth` 就直接返回 true，免去遍历整条 `outer` 链的开销。用 `Relaxed` 内存序是因为只需原子性、不需跨线程同步可见性。

---

## 5. 综合实践

本讲的中心任务是**对比 `html_block_fragment` 与 `html_inline_fragment` 的签名和缓存策略，并解释智能引号状态为何不能用 memoize 保存**。请完成下面的源码阅读型综合练习。

### 实践目标

把本讲五个模块串起来，建立「语境 → 签名 → 状态策略 → 缓存策略 → 深度保护」的完整因果链。

### 操作步骤

1. **填表对比**。打开 `src/fragment.rs`，按下表逐项核对两个函数（遇「待确认」请自行读源确认）：

   | 维度 | `html_block_fragment` | `html_inline_fragment` |
   |------|----------------------|------------------------|
   | 定位器入参类型 | `Locator`（owned） | `&mut SplitLocator`（借用） |
   | 是否接收 quoter | 否（内部自建） | 是（`&mut SmartQuoter`） |
   | `ConversionLevel` | `Block` | `Inline(quoter)` |
   | 路由深度写法 | `Route::extend(route)` | `increase()`/`decrease()` |
   | 是否 `#[comemo::memoize]` | 是 | 否 |
   | 具象化助手 | `realize_fragment` | `realize_fragment` |

2. **追踪一条嵌套调用链**。假设有如下结构（伪代码示意，非可运行 Typst）：

   ```
   <div>                          // 块级 → html_block_fragment
     <p>                          // 块级 → html_block_fragment（quoter 重置）
       hello <em>"world"</em>     // em 行内 → html_inline_fragment（共享 p 的 quoter）
     </p>
   </div>
   ```

   按以下顺序追踪：

   - 顶层 converter 用 `ConversionLevel::Block` 进入，自建 quoter A。
   - 遇到 `<div>`：`handle_html_elem` 判定 display 为 Block → 调 `html_block_fragment`（命中/未命中缓存皆可），其内部又自建 quoter B（与 A 无关）。
   - 遇到 `<p>`：再次 `html_block_fragment`；返回后 `handle_html_elem` 执行 `*converter.quoter = SmartQuoter::new()`，把 quoter B 复位为 C。
   - 遇到 `<em>`：判定为行内 → `html_inline_fragment`，把 quoter C 以 `&mut` 传入；`"world"` 里的智能引号在 C 上开合。
   - 退出 `<em>` 后，quoter C 仍保留引号状态，供 `<p>` 内后续行内内容继续使用。

3. **解释关键问题**：为什么不能把 `html_inline_fragment` 也 memoize，把 `SmartQuoter` 作为入参缓存？

### 需要观察的现象

- block 片段在嵌套链中**多次**出现，且彼此独立；inline 片段把**同一个** quoter 沿调用链传递。
- quoter 的状态在行内流中**累积**，跨元素共享；一旦遇到块级元素就被**重置**。

### 预期结果

你应当能用自己的话给出这样的结论：

> `SmartQuoter` 是有状态的——`quote()` 每次调用都会 push/pop 自身。memoize 要求「相同入参 → 相同结果」，但 inline 片段的结果依赖**调用历史**（前文开了几个引号），而非仅依赖当前 content。把 `&mut SmartQuoter` 当缓存 key 既不可哈希（它是可变借用），也不正确（同样的内容在不同引号上下文里产出不同引号字符）。因此 inline/math 片段放弃缓存，而 block 片段因为状态完全自包含、不外泄，可以安全缓存。

> 待本地验证：若想用真实编译验证「相同内容不同引号上下文 → 不同输出」，可构造两段含智能引号的 Typst，分别置于已开/未开的引号环境中导出 HTML 对比。本讲不假设已运行。

## 6. 本讲小结

- `fragment.rs` 提供三个递归编译入口：`html_block_fragment`（块级）、`html_inline_fragment`（行内）、`html_math_fragment`（数学），分别对应三种 HTML 语境。
- **block 片段**用标准 memoize 三明治结构缓存，接收 owned `Locator`，内部自建 `SmartQuoter`，是自包含上下文。
- **inline 片段**不缓存，借用 `&mut SplitLocator` 和 `&mut SmartQuoter`，让智能引号状态在行内流中跨元素共享。
- **math 片段**签名同 inline，但直接用 `RealizationKind::Math` 具象化（跳过 `realize_fragment`），避免段落分组破坏 MathML 结构。
- `realize_fragment` 是 block/inline 共用的具象化助手，固定用 `RealizationKind::Fragment` 并忽略回填的 `FragmentKind`。
- 智能引号状态「可变且依赖历史」是 inline/math 不能 memoize 的根本原因；`SmartQuoter` 的 `&mut self` 与缓存纯函数前提天然冲突。
- 三个片段都在进入时调用 `route.check_html_depth()`（上限 72），把无限递归转化为友好报错而非栈溢出。

## 7. 下一步学习建议

本讲把「片段如何递归编译」讲透了，但刻意留下了两个接口没展开：

1. **数学片段产出的 MathML 节点从哪来**：`html_math_fragment` 只负责把数学内容翻译成 `HtmlNode`，真正的 `<mfrac>`/`<mroot>` 等结构由 `mathml.rs` 生成，并在文档头部注入 `EQUATION_CSS_STYLES`。这部分留待 **u5-l5（数学公式到 MathML 的转换）** 精读。
2. **块级/行内的 display 提升如何决定走哪个片段**：`handle_html_elem` 用 `Display::default_for(elem.tag)` 三分派，背后是 `property.rs` 的 display 属性系统。建议接着读 **u4-l2（display 属性与块级/行内提升）**，理解「为什么 `<div>` 走 block、`<span>` 走 inline」的判定源头。

如果你对缓存机制本身感兴趣，可跳读 **u6-l4（缓存与 comemo memoization）**，它从架构层面讨论「为何 `HtmlDocument` 不实现 `Hash`」「memoize 边界该放在哪一层」等更宏观的取舍。
