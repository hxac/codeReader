# 带样式元素 visit_styled 与页面/文档样式

## 1. 本讲目标

在前几讲里，我们已经走过 `visit()` 的 8 步调度流水线（u1-l3）、读懂了 `State` 的全部字段（u2-l1）、也看清了 `visit_show_rules` 如何把 show 规则的输出关进「笼子」（u2-l2）。本讲专门拆开流水线的第 5 步——**带样式元素的递归入口 `visit_styled`**。

学完本讲，你应当能够：

1. 说清 `visit_styled` 在 `visit()` 中的位置、它的四个参数各自代表什么。
2. 理解为什么 `DocumentElem` / `TextElem` / `PageElem` 三类样式会在 `visit_styled` 里被「特殊拦截」，而其它元素的样式只是普通地链入 `StyleChain`。
3. 区分容器内、外对 `set page(...)` 与 `set document(...)` 的不同处理，以及对应的报错条件。
4. 解释 `outside` 标志从「初值」到「被 show 规则压低」再到「被 page 规则重新抬高」的完整生命周期，以及它如何驱动样式提升（style lifting）。
5. 掌握弱断点 `PagebreakElem::shared_weak()` 与边界断点 `shared_boundary()` 的生成时机与差异。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **Content / StyleChain / Pair**：realize 的输入是 `content + styles`，输出是 `Vec<Pair>`，其中 `Pair = (&Content, StyleChain)`（u1-l1、u1-l2）。
- **RealizationKind 五变体**：`Bundle` / `Document` / `Fragment` / `Par` / `Math`，标识当前具现化发生在「整篇文档」「容器内部」「段落内」「数学内」等不同场景（u1-l2）。
- **visit() 的 8 步调度**：当 `visit()` 看到一个 `StyledElem`，会把它的内部子元素和自带样式拆开，交给 `visit_styled`（u1-l3）。
- **State 字段**：`outside`、`may_attach`、`saw_parbreak` 三个布尔标志的含义（u2-l1）。
- **show 规则笼子**：`visit_show_rules` 在递归处理 show 输出前会执行 `s.outside &= content.is::<ContextElem>()`，把 `outside` 压低（u2-l2）。

此外补充两个本讲会用到的背景概念：

- **`StyledElem`**：Typst 在评估（eval）阶段把 `#set ...` / `#show ...` 规则的作用域编码成一种「带样式的包装元素」。它的结构很简单——一个子 `Content` 加一份 `Styles`。realize 的任务之一就是把这些包装层层拆开，把样式摊平进 `StyleChain`。
- **样式提升（style lifting）**：分页排版（paged export）时，页眉页脚、页码等「页面级内容」并不在正文流里，但它们需要继承正文里 `set text(red)` 这样的样式。Typst 的做法是：在 realize 阶段给「确实发生在文档最外层、而非 show 规则产物里」的样式打上 `outside` 标记，排版阶段再据此把这些样式「提升」到页面根级别。本讲的核心之一就是搞清楚这个标记是何时、由谁打上的。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | 本讲主战场。`visit_styled`、`finish_interrupted`、`State.outside` 字段都在这里。 |
| `crates/typst-library/src/foundations/styles.rs` | 定义 `Styles::outside()`（打标记）、`Style::outside()`（读标记）、`Styles::root()`（排版时按标记筛选可提升样式）。 |
| `crates/typst-library/src/layout/page.rs` | `PagebreakElem` 的字段 `weak` / `boundary`，以及两个全局单例 `shared_weak()` / `shared_boundary()`。 |
| `crates/typst-library/src/model/document.rs` | `DocumentInfo::populate` 与 `populate_locale`，负责从 `set document(...)` / `set text(...)` 回填文档元信息。 |
| `crates/typst-library/src/routines.rs` | `RealizationKind` 枚举定义，理解各类分支判断的依据。 |
| `crates/typst-layout/src/pages/mod.rs`、`crates/typst-layout/src/pages/collect.rs` | 下游排版如何消费 `outside` 标记与 weak/boundary pagebreak，用来验证本讲行为。 |

---

## 4. 核心概念与源码讲解

### 4.1 visit_styled：带样式元素的递归入口

#### 4.1.1 概念说明

在 `visit()` 的调度顺序里，序列（`SequenceElem`）递归是第 4 步、**带样式元素（`StyledElem`）递归是第 5 步**。当 `visit()` 拆开一个 `StyledElem` 后，它并不直接把样式链入 `StyleChain` 了事，而是交给专门的 `visit_styled`：

```rust
// Recurse into styled elements.
if let Some(styled) = content.to_packed::<StyledElem>() {
    return visit_styled(s, &styled.child, Cow::Borrowed(&styled.styles), styles);
}
```

之所以要单独开一个函数，是因为有三类样式（document / text 的 locale / page）需要在递归**之前**做特殊处理：它们不是「给某个元素加点外观」，而是会影响**整篇文档的结构**（文档标题、页面切分）。`visit_styled` 的职责就是：先扫一遍这些特殊样式，该回填元信息的回填、该切页的切页、该报错的报错，然后再把（可能被改写过的）样式交给 `visit()` 继续往下走。

#### 4.1.2 核心流程

`visit_styled` 的整体骨架可以分成 6 段：

```text
visit_styled(s, content, local: Styles, outer: StyleChain):
  1. 若 local 为空            → 直接 visit(content, outer)，无事可做
  2. 扫描特殊样式循环          → 处理 Document / Text / Page，可能置 pagebreak=true、s.outside=true、或 bail
  3. 若 s.outside             → 给 local 打 outside() 标记（样式提升的前提）
  4. 生命周期延长              → 把 outer 与（可能新建的）local 放进 arena
  5. 若 pagebreak             → 先发一个 shared_weak() 起始断点（只带「相关」页面样式）
  6. finish_interrupted → visit(content, outer.chain(local)) → finish_interrupted
     若 pagebreak             → 再发一个 shared_boundary() 结束断点
```

注意第 6 步的对称结构：`finish_interrupted` 在内容**前后各调用一次**。它的作用是「凡是被 local 里这些样式打断的进行中分组，都先收尾」。例如一个段落分组正开着，突然遇到一个会打断段落的 block 样式，就得先把段落收掉。这部分逻辑我们在 4.1.3 末尾点一下，深入留到 u2-l7（分组生命周期）。

#### 4.1.3 源码精读

先看函数签名与「空样式」快速通道：

[crates/typst-realize/src/lib.rs:L592-L601](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L592-L601) —— `visit_styled` 接收子元素 `content`、子元素自带的 `local: Cow<'a, Styles>`、以及外层样式链 `outer`。`Cow` 表示 local 可能是借用自 `StyledElem`（只读），也可能在后续被改写成自有（owned）。第 599-601 行：如果 local 实际是空的，直接退化为一次普通的 `visit`，避免无谓的样式链拼接。

接着是第 4 步「生命周期延长」与第 6 步的对称结构：

[crates/typst-realize/src/lib.rs:L662-L689](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L662-L689) —— 第 663 行把 `outer` 也分配进 `bump` arena，第 664-667 行把 `local` 从 `Cow` 统一成 `&Styles`（借用的直接用，自有的分配进 `styles` arena）。这是 u3-l3（生命周期与 arena）会详讲的技巧：show 规则新产出的样式没有足够的生命周期，必须靠 arena 延长到 `'a`。第 680-682 行正是前述「前后各一次 `finish_interrupted`」的对称调用。

`finish_interrupted` 本身只做一件事——遍历 local 中出现的元素类型，对每一个被它打断的内层分组调用 `finish_grouping_while` 收尾：

[crates/typst-realize/src/lib.rs:L813-L831](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L813-L831) —— 注意第 815 行用 `filter_map(|style| style.element())` 去重相邻同类元素，避免对同一个元素类型重复收尾；第 820 行 `(grouping.rule.interrupt)(elem)` 判断「这个元素的样式是否会打断该分组」。本讲只需理解它「让分组在样式边界处正确收尾」，细节留给 u2-l7。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `visit_styled` 在一次完整 realize 中被调用的次数与触发场景。

**步骤**：

1. 在 [crates/typst-realize/src/lib.rs:L592](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L592) 函数体第一行加入临时日志：
   ```rust
   eprintln!("[visit_styled] child = {:?}, local.len = {}, outside = {}", content.func().name(), local.len(), s.outside);
   ```
2. 编译并排版一个带 `#set text(red)` 与 `#highlight[Hi]` 的小文档。
3. 观察日志，统计 `visit_styled` 被命中的次数，并留意 `outside` 字段在不同调用点的取值。

**预期**：凡是由 `#set` / `#show` / 元素构造器产生的 `StyledElem` 都会触发一次 `visit_styled`；`local.len` 反映该作用域里有多少条样式。**注意：这是临时调试修改，验证完请还原，不要提交。** 后文涉及的「加日志」实践同理。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `visit_styled` 要在 `local.is_empty()` 时直接 `return visit(s, content, outer)`，而不是走完后续所有步骤？

**参考答案**：后续步骤（打 `outside` 标记、分配进 arena、发 pagebreak）都依赖 local 里**确实有样式**。空样式意味着这个 `StyledElem` 是个空包装，没有任何副作用，直接当普通元素处理既正确又省去一次 arena 分配和一次 `finish_interrupted`。

**练习 2**：`finish_interrupted` 为什么要在 `visit(content, …)` 的前后**各**调用一次？

**参考答案**：前一次收尾「在进入新样式作用域之前」就被打断的分组（比如开着的段落遇到 block 样式）；后一次收尾「在离开该作用域之后」因为 local 里某些样式（如再次出现的 block 样式）可能又开启了需要被打断的新分组。对称调用保证样式作用域的边界两侧分组状态都自洽。

---

### 4.2 三类特殊样式的拦截：Document / Text / Page

#### 4.2.1 概念说明

`visit_styled` 第 2 段是一个 `for style in local.iter()` 循环，逐条审视 local 里的样式。绝大多数样式（`text`、`par`、`heading` 等）在这个循环里**什么都不做**——它们会原样链入 `StyleChain`，由下游排版消费。循环只对三类「绑定到特定元素」的样式开特例：

- **`DocumentElem::ELEM`**：来自 `#set document(...)`。它定义的是整篇文档的元信息（标题、作者、关键词、日期），不属于任何一个正文元素，必须在 realize 阶段就提取出来回填到 `DocumentInfo`。
- **`TextElem::ELEM`**：来自 `#set text(...)` 中的 `lang` / `region`。Typst 用它来推断文档的 locale（用于 PDF 元数据、日期格式化等），且只取**第一个顶层** set 规则。
- **`PageElem::ELEM`**：来自 `#set page(...)`。它定义页面几何（纸张、边距、页眉页脚），等价于一次「隐式分页」——样式作用域的开始与结束都要插入 pagebreak。

理解这三类的共同点是关键：**它们都影响文档级或页面级结构，而不是局部外观**，所以不能简单地塞进 `StyleChain` 等下游处理。

#### 4.2.2 核心流程

循环对每条样式取出它绑定的元素 `style.element()`（取不到就 `continue`），然后按下面三分支处理：

```text
for style in local.iter():
  elem = style.element()  // 取不到 → continue
  match elem:
    DocumentElem::ELEM:
      若 kind 是 Document { info }  → info.populate(local)        # 正常回填
      否则若 kind 不是 Bundle       → bail "document set rules are not allowed inside of containers"
      若 local 改了 format 且非 Bundle → bail "setting the document format is only supported in the bundle target"
    TextElem::ELEM:
      若 kind 是 Document { info }  → info.populate_locale(local) # 推断 locale
      # 其它 kind：什么都不做（text 样式照常链入）
    PageElem::ELEM:
      match kind:
        Bundle                       → 忽略
        Document + 目标是 Paged      → pagebreak = true; s.outside = true   # 「突破笼子」
        Document + 目标是 Html       → 警告 "page set rule was ignored during HTML export"
        Document + 目标是 Bundle     → 忽略
        _（Fragment/Par/Math）       → bail "page configuration is not allowed inside of containers"
```

两条 `bail` 正是「容器内外对 page/document set 规则不同处理」的体现：在容器（`Fragment`）、段落（`Par`）、数学（`Math`）里设置 page 或 document 是非法的，因为这些 kind 根本没有「页面」或「整篇文档」的概念。

#### 4.2.3 源码精读

完整的三分支循环：

[crates/typst-realize/src/lib.rs:L603-L654](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L603-L654) —— 第 605-606 行取出每条样式绑定的元素；第 607-624 行是 `DocumentElem` 分支；第 625-629 行是 `TextElem` 分支；第 630-653 行是 `PageElem` 分支。

**DocumentElem 分支**的两个关键点：第 609-610 行只在 `RealizationKind::Document` 时调用 `info.populate(local)` 回填元信息；第 611-616 行对除 `Bundle` 外的其它 kind 报错「document set rules are not allowed inside of containers」。`Bundle` 是例外，因为 bundle 本身就是为了把多份内容打包成文档/资源，允许内部出现 document set 规则。第 617-624 行额外禁止在非 Bundle 场景设置 `format`（文档格式只能在 bundle 目标里设）。

回填逻辑本身在 `DocumentInfo::populate`：

[crates/typst-library/src/model/document.rs:L353-L375](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/document.rs#L353-L375) —— 它逐个检查 `title` / `author` / `description` / `keywords` / `date`，只要 local 里 `has` 对应字段就写入 `self`。注意它读的是 `StyleChain`（第 608 行用 `StyleChain::new(&local)` 构造），因为 set 规则的值要通过样式链查询。

**TextElem 分支**只做 locale 推断：

[crates/typst-library/src/model/document.rs:L378-L391](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/document.rs#L378-L391) —— 第 379-381 行：如果 `locale` 已经被用户显式设过（`is_custom()`），就不再覆盖；否则从 `TextElem::lang` 与 `region` 推断。这解释了为什么只取第一个顶层的 `set text(lang:)`——后续调用会因 `is_custom()` 而 return。

**PageElem 分支**是本讲重头戏之一。第 633 行用 `outer.get(TargetElem::target)` 判断当前导出目标（`Target::Paged` / `Html` / `Bundle`）：

[crates/typst-realize/src/lib.rs:L630-L653](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L630-L653) —— Paged 目标下，第 637-639 行做两件事：置 `pagebreak = true`（稍后在第 5 步发起始弱断点），并把 `s.outside = true`（让本作用域的样式能被提升）。这正是「pagebreak 突破 show 规则笼子」的语义——即便这段 `set page(...)` 写在某个 show 规则的输出里，它也会把后续内容抬回到文档最外层。Html 目标下（第 640-645 行）只是发一个警告并忽略，因为 HTML 没有分页概念。除 `Bundle`/`Document` 外的 kind（即 `Fragment`/`Par`/`Math`，对应容器内）走第 648-652 行的 `bail`。

#### 4.2.4 代码实践（可运行）

**目标**：亲手触发「容器内 `set page(...)` 报错」与「容器内 `set document(...)` 报错」，对照源码确认 bail 分支。

**步骤**：

1. 写一个 `bad.typ`：
   ```typst
   #block[
     #set page(paper: "a4")
     不允许在容器里设置页面。
   ]
   ```
2. 运行 `typst compile bad.typ`（或在你的构建环境里等价的命令）。
3. 把 `set page` 换成 `#set document(title: "X")` 再编译一次。

**预期**：第一次报 `page configuration is not allowed inside of containers`（对应 lib.rs:648-652 的 `_ => bail!`）；第二次报 `document set rules are not allowed inside of containers`（对应 lib.rs:611-616 的 `else if !matches!(s.kind, RealizationKind::Bundle)`）。如果报错文案与源码一致，说明你正确锁定了 bail 分支。**待本地验证**：具体报错措辞以你本地的 Typst 版本为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Bundle` kind 下出现 `set document(...)` 不报错，而 `Fragment` kind 下会报错？

**参考答案**：`Bundle` 的职责正是把若干内容打包成文档与资源，它的语义允许内部出现 document 级配置（包括 `format`）；而 `Fragment` 是「容器内部的一次嵌套具现化」，它对应的 `block` / `html.div` 等容器没有「整篇文档」的概念，document 元信息无处回填，所以必须报错。

**练习 2**：在 HTML 导出（`Target::Html`）下写 `#set page(paper: "a3")` 会发生什么？为什么这么设计？

**参考答案**：不会报错，而是发出警告 `page set rule was ignored during HTML export`，且样式被忽略（lib.rs:640-645）。因为 HTML 不分页，页面几何没有意义；但用户可能误把分页文档的样式带进了 HTML 模板，所以用警告而非静默忽略来提示。

**练习 3**：`info.populate_locale` 为什么在 `self.locale.is_custom()` 时直接 return？

**参考答案**：用户若已用 `#set document(locale: ...)` 显式指定了 locale，应优先尊重用户意图，不被正文里 `#set text(lang:)` 的首条规则覆盖；只有未显式指定时，才退而用 text 的 lang/region 推断。

---

### 4.3 outside 标志与样式提升（outside）

#### 4.3.1 概念说明

`State.outside` 是一个布尔标志，回答的问题是：**「当前正在具现化的内容，是否处于文档最外层、而非任何 show 规则的产物里？」**

这个问题之所以重要，是因为**样式提升**。分页排版时，页眉、页脚、页码、脚注这些「页面级内容」并不在正文流里，它们由 `PageElem` 的 `header` / `footer` 等字段提供。但这些页面级内容希望继承正文里的 `set text(red)`——也就是说，一条写在大纲最外层的 text 样式，应当能「提升」到页面根级别。

可如果这条 `set text(red)` 是某个 show 规则**内部**产出的，提升它就不合理了——show 规则的输出本就是局部的、可能被反复触发的。因此 realize 需要一种方式区分「真外层样式」与「show 规则产物里的样式」，这就是 `outside` 标志。

`outside` 标志最终落到每一条 `Style` 上：`Styles::outside()` 方法把一整份样式里的每条样式打上 `outside = true`；排版阶段 `Styles::root()` 再按这个标记筛选可提升的样式。

#### 4.3.2 核心流程

`outside` 标志的完整生命周期有三处转折：

```text
① 初值（在 realize() 构造 State 时）：
     outside = matches!(kind, RealizationKind::Document { .. })
   → 只有整篇文档具现化一开始就在「外层」；Fragment/Par/Math 一开始就非外层。

② 被 show 规则压低（在 visit_show_rules 里，递归处理 show 输出前）：
     prev_outside = s.outside
     s.outside &= content.is::<ContextElem>()   // 进入 show 输出 → outside 变 false
     …递归处理…
     s.outside = prev_outside                     // 离开 show 输出 → 恢复
   → 含义：show 规则的产物默认不「外层」；唯有 ContextElem 例外（它本身就是为了「突破」而设）。

③ 被 page 规则抬高（在 visit_styled 的 PageElem 分支里）：
     s.outside = true
   → 含义：即便是在 show 输出里，set page(...) 也会把后续内容重新抬回外层（"突破笼子"）。
```

当 `s.outside` 为真时，`visit_styled` 第 3 步把当前 local 整体打上 `outside` 标记：

```text
if s.outside { local = Cow::Owned(local.into_owned().outside()); }
```

这个标记随后被排版阶段消费：`Styles::root()` 在筛选页面根样式时只保留 `style.outside() && (initial || style.liftable())` 的条目。

#### 4.3.3 源码精读

**① 初值**——在 `realize()` 构造 `State` 时：

[crates/typst-realize/src/lib.rs:L64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L64) —— `outside: matches!(kind, RealizationKind::Document { .. })`。字段注释见 [lib.rs:L101-L103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L101-L103)：「Whether we are currently not within any container or show rule output. This is used to determine page styles during layout.」

**② 被 show 规则压低**——在 `visit_show_rules` 里（u2-l2 已提过笼子，这里看具体代码）：

[crates/typst-realize/src/lib.rs:L417-L424](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L417-L424) —— 第 418 行 `s.outside &= content.is::<ContextElem>()`：只有当当前元素是 `ContextElem` 时 `outside` 才保持为真，否则被清零。`ContextElem`（即用户写的 `context`/`styke` 等上下文块）是故意设计的「逃生口」，让 show 规则里的内容也能声明「我在外层」。

**③ 打标记**——回到 `visit_styled`：

[crates/typst-realize/src/lib.rs:L656-L660](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L656-L660) —— 若 `s.outside` 为真，把 local 转成 owned 并调用 `.outside()`。注意这一步**把 Cow 从 Borrowed 变成 Owned**，这就是为什么参数声明成 `Cow<'a, Styles>` 而非 `&Styles`——只有需要打标记时才付出一次克隆的代价。

`Styles::outside()` 的实现就是把每条样式的 `outside` 字段置真：

[crates/typst-library/src/foundations/styles.rs:L93-L102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L93-L102) —— 对 `Property` 和 `Recipe` 都置 `outside = true`（`Revocation` 没有 outside 概念，跳过）。底层字段定义见 [styles.rs:L329-L330](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L329-L330) 的 `Property.outside: bool`，读取方法见 [styles.rs:L280-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L280-L286) 的 `Style::outside()`。

**下游消费**——排版阶段如何用这个标记筛选可提升样式：

[crates/typst-library/src/foundations/styles.rs:L141-L165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L141-L165) —— `Styles::root()` 第 159-161 行的过滤条件 `style.outside() && (initial || style.liftable())`。其上方 L114-L140 的文档注释把三条筛选规则讲得很清楚（`outside` / `initial` / `liftable` 的含义），强烈建议读一遍。而页面排版入口在拿到 realize 结果前，会先把外部样式整体打上 outside 标记：

[crates/typst-layout/src/pages/mod.rs:L150-L153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L150-L153) —— `let styles = styles.to_map().outside();` 把文档根的初始样式标记为 outside，保证页面级内容能继承。

#### 4.3.4 代码实践（源码阅读型）

**目标**：追踪 `s.outside` 在一次「show 规则内含 set page」的文档中如何变化，验证「突破笼子」。

**步骤**：

1. 在三处转折点加日志：
   - lib.rs:64 `realize` 构造 State 后，打印初值。
   - lib.rs:418 `visit_show_rules` 压低前后，打印 `prev_outside` 与新值。
   - lib.rs:638 `visit_styled` PageElem 分支抬高后，打印新值。
2. 编译如下文档（其中 show heading 的输出里含 `set page`，仅用于触发路径观察，实际语义以本地版本为准）：
   ```typst
   #set heading[标题]
   ```
   （更稳妥的触发方式见 4.4 的综合实践；此处只需观察日志中三个转折点的 `outside` 取值变化。）
3. 对照「初值 true → 进入 show 变 false → 命中 page 规则又变 true」这条曲线。

**预期**：你能观察到 `outside` 至少经历一次 `true → false`（进 show）和一次 `false → true`（命中 page 规则）的翻转，正好对应「笼子」与「突破」。**待本地验证**：具体能否在 show 输出里直接写 `set page` 取决于 Typst 版本，若编译期就被拦下，请改用顶层 `set page` 观察 `outside` 始终为 true 的路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `realize()` 的初值是 `outside = matches!(kind, RealizationKind::Document { .. })`，而 `Fragment` / `Par` / `Math` 都是 false？

**参考答案**：只有 `Document` kind 对应整篇文档的根具现化，它的最外层样式天然「在外层」，可被提升到页面根。`Fragment`（容器内）、`Par`（段落内）、`Math`（数学内）都处于某个更外层的作用域内部，不存在「页面根」的概念，所以初值为 false。

**练习 2**：`visit_show_rules` 里为什么是 `s.outside &= content.is::<ContextElem>()` 而不是直接 `s.outside = false`？

**参考答案**：`&=` 保留了「当前本就非外层」的情况（保持 false）；而对 `ContextElem`，`is::<ContextElem>()` 为 true，使 `outside` 维持原值——这是故意留给 `context` 块等结构「声明自己处于外层」的逃生口。直接赋 false 会堵死这条合法的提升通道。

**练习 3**：`Styles::root()` 的过滤条件里 `initial || liftable()` 是什么意思？举例说明。

**参考答案**：`initial` 指该样式在子内容一开始就已生效（对页面而言，就是引入该页的 pagebreak 处已生效），这类样式即使不可提升也该保留（例如 `text(red, page(..))` 让页脚变红）。`liftable` 指 set 规则产生的样式可在后续被提升（`set text(red)` 是 liftable，而 `text(red)[…]` 构造器样式不是）。两者满足其一，且必须是 `outside`，才被保留到页面根。详见 [styles.rs:L125-L140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L125-L140) 的注释。

---

### 4.4 pagebreak 的「突破笼子」：shared_weak 与 shared_boundary

#### 4.4.1 概念说明

当 `visit_styled` 的 PageElem 分支把 `pagebreak` 置为 true 后，第 5 步与第 6 步末尾会各发一个 pagebreak，把这段 `set page(...)` 的作用域「夹」起来。Typst 提供了两个全局共享的 pagebreak 单例来承担这两个角色：

- **`PagebreakElem::shared_weak()`**：一个 `weak = true` 的 pagebreak，用作作用域**起始**断点。`weak` 的含义是「若当前页已经为空，就跳过这次分页」——它不会凭空制造一个空白页。
- **`PagebreakElem::shared_boundary()`**：一个 `weak = true` **且** `boundary = true` 的 pagebreak，用作作用域**结束**断点。`boundary` 是比 `weak` 更弱的版本：它不仅不强制空白页，还**不让自己的样式作用到可能产生的空页上**（因为它的样式对应的是 `set page` 之前的状态）。

这两个单例用 `singleton!` 宏做成全局共享的 `&'static Content`，避免每次都重新分配——因为 pagebreak 在分页文档里极其常见。

#### 4.4.2 核心流程

`visit_styled` 发 pagebreak 的逻辑（接 4.1 的第 5、6 步）：

```text
若 pagebreak:
  # ① 起始断点（内容之前）
  relevant = local.as_slice().trim_end_matches(|st| st.element() != Some(PageElem::ELEM))
            # 砍掉 local 末尾的非 page 样式，只保留「到末尾 page 样式为止」的前缀
  visit(s, PagebreakElem::shared_weak(), outer.chain(relevant))

  finish_interrupted(s, local)
  visit(s, content, outer.chain(local))
  finish_interrupted(s, local)

  # ② 结束断点（内容之后）
  visit(s, PagebreakElem::shared_boundary(), *outer)   # 注意：用 *outer，不带 local
```

两个细节值得记住：

1. **起始断点带「相关」样式，结束断点不带**。起始断点用 `outer.chain(relevant)` 把页面样式挂上去，好让下一页用上新几何；结束断点用 `*outer`（即 set page 之前的样式），且因为是 boundary，它的样式本就不会被下游采用。
2. **`trim_end_matches` 的方向**。它砍掉的是「末尾的非 page 样式」。设想 local = `[page-A, text-red, page-B]`，那么 relevant = `[page-A, text-red, page-B]`（末尾是 page-B，不被砍）；若 local = `[page-A, text-red]`，relevant = `[page-A]`（砍掉末尾的 text-red，因为它是 page 作用域里「在最后一个 page 样式之后」的尾随样式，不应作为起始页的初始样式）。

#### 4.4.3 源码精读

两个单例的定义：

[crates/typst-library/src/layout/page.rs:L581-L594](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/page.rs#L581-L594) —— `shared_weak()` 是 `with_weak(true)`；`shared_boundary()` 是 `with_weak(true).with_boundary(true)`。两者都用 `singleton!(Content, …)` 缓存为全局静态实例。

`weak` 与 `boundary` 字段的语义：

[crates/typst-library/src/layout/page.rs:L553-L579](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/page.rs#L553-L579) —— `weak`（L554-L557）：为 true 时若当前页已空则跳过；`boundary`（L571-L578，标了 `#[internal]`）：是「比 weak 更弱」的版本，不仅不强制空页，还不把自己的样式强加给可能产生的空页。

`visit_styled` 里发 pagebreak 的两段：

[crates/typst-realize/src/lib.rs:L669-L689](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L669-L689) —— 第 669-672 行的注释解释了为什么要 `trim`：「For the starting pagebreak we only want the styles before and including the interruptions, not trailing styles that happen to be in the same Styles list」。第 674-677 行计算 `relevant` 并发起始弱断点；第 687-688 行发结束 boundary 断点，明确用 `*outer` 且注释说明「the styles of this are ignored during layout, so it doesn't really matter what we use here」。

下游排版如何区分对待两者：

[crates/typst-layout/src/pages/collect.rs:L38-L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/collect.rs#L38-L66) —— 第 41 行 `let strong = !pagebreak.weak.get(styles)` 判断是否强分页；第 58-60 行 `if !pagebreak.boundary.get(styles) { initial = styles; }`——**只有非 boundary 的 pagebreak 才把自己的样式作为下一页的 initial 样式**，boundary 断点不更新 initial，正好印证了「结束断点的样式不该被采用」。

#### 4.4.4 代码实践（可运行）

**目标**：在顶层用 `set page` 改变纸张，观察 realize 输出里夹着的两个 pagebreak，验证「起始带样式、结束不带」。

**步骤**：

1. 写 `pages.typ`：
   ```typst
   第一页（默认）。
   #set page(paper: "a4")
   第二页（A4）。
   ```
2. 在 lib.rs:677 与 lib.rs:688 两行各加一条日志，打印「发起始 weak 断点，带 relevant 样式」与「发结束 boundary 断点，用 outer」。
3. 编译并观察日志出现次数与顺序。

**预期**：每遇到一次 `set page` 作用域，就成对出现「起始 weak（带 page 样式）→ …正文… → 结束 boundary（不带）」两条日志。结合 collect.rs 的行为，起始断点让正文切到 A4 新页，结束断点则不把任何样式传染给后续可能出现的空页。**待本地验证**：日志的精确频次取决于 Typst 内部对该作用域的包装方式。

#### 4.4.5 小练习与答案

**练习 1**：为什么起始断点用 `shared_weak()` 而不是普通的强 pagebreak？

**参考答案**：`set page(...)` 改的是「接下来内容的页面几何」，不应因为换了纸张就强制插入一个空白页。`weak` 保证「若当前页已空则跳过」，避免在文档开头或紧跟强分页之后产生多余空页。

**练习 2**：结束断点为什么用 `shared_boundary()`（boundary=true）而不是再发一个 `shared_weak()`？

**参考答案**：结束断点携带的样式是 `*outer`，即 `set page` **之前**的状态。若用普通 weak 断点，下游 collect.rs 第 58-60 行会把这份「旧样式」更新为下一页的 initial，从而错误地让作用域之后的页面回退到旧几何。`boundary=true` 让 collect.rs 跳过 `initial = styles` 的赋值，避免这种污染。

**练习 3**：`trim_end_matches(|style| style.element() != Some(PageElem::ELEM))` 在 local = `[page-A, text-red, page-B, text-blue]` 时返回什么？为什么？

**参考答案**：返回 `[page-A, text-red, page-B]`。`trim_end_matches` 砍掉末尾所有满足谓词（「元素不是 PageElem」）的条目，末尾的 `text-blue` 不是 page 样式故被砍；`page-B` 是 page 样式，谓词为 false，停止砍除。结果保留了「直到最后一个 page 样式为止」的前缀，符合「起始断点只带 page 相关样式」的意图。

---

## 5. 综合实践

把本讲三条主线（容器内外的报错差异、`outside` 标志的起伏、pagebreak 的成对生成）串成一个对照实验。

**任务**：准备两个 Typst 文档，预测并验证它们在 `visit_styled` 内部的行为差异。

文档 A（容器内，应报错）：

```typst
#block[
  #set page(paper: "a4")
  这里在容器里设置页面。
]
```

文档 B（顶层，应正常分页并触发 outside 提升）：

```typst
第一页。

#set page(paper: "a4")
#set text(red)

第二页，文字应为红色，且 red 因 outside 而可被页面级内容继承。
```

**操作步骤**：

1. 在 `visit_styled` 的关键位置加日志（验证完务必还原）：
   - 函数入口（lib.rs:592 附近）：打印 `content.func().name()`、`local.len()`、`s.outside`。
   - PageElem 分支命中处（lib.rs:637-639 附近）：打印「pagebreak=true, outside 抬高」。
   - DocumentElem / TextElem 分支命中处（lib.rs:609 / lib.rs:627 附近）：打印「populate / populate_locale 被调用」。
   - 两个 pagebreak 发出点（lib.rs:677 / lib.rs:688 附近）：分别打印「起始 weak」「结束 boundary」。
2. 先编译文档 A：预期在 lib.rs:648-652 的 `_ => bail!` 处直接报 `page configuration is not allowed inside of containers`，日志应显示进入了 PageElem 分支但走到了 bail。
3. 再编译文档 B：预期日志依次出现「入口 outside=true」→「PageElem 分支抬高 outside」→「起始 weak（带 a4 样式）」→ 正文 visit →「结束 boundary」。
4. 对照 [collect.rs:L38-L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/collect.rs#L38-L66) 与 [styles.rs:L141-L165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L141-L165)，解释为什么文档 B 里的 `set text(red)` 能让页脚等页面级内容也变红。

**预期结果**：你能用一张时序图把文档 B 一次 realize 中 `outside` 的取值变化、两次 pagebreak 的发出顺序、以及它们各自携带的样式画清楚；并能在文档 A 上指认触发 bail 的具体源码行。若日志与上述预测不符，以本地实际行为为准并思考差异原因（**待本地验证**）。

---

## 6. 本讲小结

- `visit_styled` 是 `visit()` 第 5 步，专门处理 `StyledElem`：先扫描三类特殊样式，再决定是否打标记、是否发 pagebreak，最后对称地 `finish_interrupted` + `visit` + `finish_interrupted`。
- `DocumentElem` / `TextElem`(locale) / `PageElem` 三类样式被特殊拦截：document 元信息回填 `DocumentInfo`、text 的 lang/region 推断 locale、page 触发分页与「突破笼子」。
- 容器内（`Fragment`/`Par`/`Math`）写 `set page(...)` 或 `set document(...)` 会 `bail`；`Bundle` 是唯一允许 document 级配置的其它 kind；HTML 目标对 page set 只警告不报错。
- `outside` 标志经历「初值（仅 Document 为 true）→ 进 show 规则被压低（ContextElem 例外）→ 命中 page 规则被抬高」三段生命周期；它通过 `Styles::outside()` 打标记、`Styles::root()` 读标记来驱动样式提升。
- `shared_weak()` 用作 `set page` 作用域的起始断点（带「相关」页面样式、weak 不强制空页）；`shared_boundary()` 用作结束断点（boundary 不更新下游 initial 样式）。
- `finish_interrupted` 在内容前后各跑一遍，确保被 local 样式打断的进行中分组在作用域边界正确收尾。

---

## 7. 下一步学习建议

- **u2-l6 / u2-l7（分组规则框架与生命周期）**：本讲多次提到 `finish_interrupted` 与分组收尾，但刻意没展开。接下来应读 `GroupingRule`、`GroupingEffect`，以及 `finish_innermost_grouping` / `finish_grouping_while`，搞清「被打断的分组」具体是怎么收尾的。
- **u3-l2（过滤规则与边界元素）**：`visit_styled` 产出的 pagebreak 进入 sink 后，会经过 `visit_filter_rules` 与下游 `pages/collect.rs`。结合本讲的 weak/boundary 概念去读过滤规则会更顺。
- **u3-l3（生命周期与 arena）**：本讲点到为止的 `s.arenas.bump.alloc(outer)` 与 `s.arenas.styles.alloc(owned)`，在那一讲有完整解释——为什么需要四个生命周期参数、为什么用 bump arena。
- **延伸阅读**：直接打开 [styles.rs:L114-L165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L114-L165) 的 `Styles::root()` 文档注释，它是理解「样式提升」整套机制最权威的说明，本讲的 `outside` 标志最终就是为它服务的。
