# HTML 空白保护机制

## 1. 本讲目标

Typst 的排版引擎和浏览器的 HTML 渲染引擎，对「空白字符（空格、制表符、换行）」的理解完全不同。如果不做处理，把 Typst 内容直接转成 HTML，浏览器会按自己的规则把连续空格折叠成一个、吞掉行首行尾空格，导致排版结果面目全非。

本讲深入 `src/convert.rs`，讲清 typst-html 用来对抗浏览器空白折叠的整套机制。学完后你应当能够：

- 说清为什么需要保护空白，以及 CSS `white-space` 折叠规则会带来哪些问题。
- 区分 `Whitespace::Normal` 与 `Whitespace::Pre` 两种模式的触发条件。
- 解释 `handle_text` 在第一遍里如何「即时」保护制表符与连续空白。
- 解释 `Converter` 的尾随空白追踪（`TrailingWhitespace`）与 `flush_whitespace` 的触发时机。
- 解释 `protect_spaces` / `Protector` 状态机在第二遍里如何用 `Collapsing` / `Supportive` / `Space` 三态决定单个空格是否需要保护。
- 说出 `pre_wrap()` 生成的 `<span style="white-space:pre-wrap">` 外壳，以及 `pre_span` 标记在最终编码阶段的作用。

## 2. 前置知识

### 2.1 浏览器的空白折叠规则

按 [CSS Text Module § white-space-rules](https://www.w3.org/TR/css-text-3/#white-space-rules)，默认 `white-space: normal` 下浏览器会：

- 把所有「空白序列」（含空格、制表符、换行）当成一个**可折叠段**。
- 把整段折叠成**一个空格**（或换行处折叠为零）。
- 折叠掉块级元素**行首**与**行尾**的空白。

举例：源码里的 `a    b`（四个空格）会被渲染成 `a b`；`a\tb`（含制表符）同样被压成一个空格。这对要求精确间距的排版是灾难。

> 解决办法有两种：要么把要保留的空白包进 `<span style="white-space: pre-wrap">`（下文称 **pre-wrap 外壳**），要么把元素本身放到 `white-space: pre` 上下文里（如 `<pre>`）。typst-html 两者都用。

### 2.2 本讲承接的概念

本讲建立在 u2-l1（`HtmlNode` / `HtmlElement` 数据模型）和 u3-l3（`convert_to_nodes` / `handle` / `ConversionLevel`）之上。回顾要点：

- `convert_to_nodes` 是把具象化后的 `Content` 序列逐个译成 `HtmlNode` 的流式翻译器，内部用一个 `Converter` 状态机。
- `ConversionLevel::Block` 自带独立的智能引号状态与空白保护；`ConversionLevel::Inline` 与外层共享。
- `Whitespace` 是挂在 `Converter` 上的一个旋钮，决定空白是否需要保护——这就是本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs) | **主战场**。包含 `Whitespace` 枚举、`convert_to_nodes` 的两遍调度、`handle_text`、`Converter`、`TrailingWhitespace`、`protect_spaces`、`Protector`、`pre_wrap`。 |
| [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | `HtmlElement.pre_span` 字段——标记「这是编译器自己生成的 pre-wrap 外壳」。 |
| [src/tag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs) | 内容模型分类函数 `is_whitespace_collapsing` / `is_replaced` / `is_raw` / `is_escapable_raw`，决定 `Protector` 对各类元素的判定。 |
| [src/encode.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs) | 最终编码：`pre_span` 为真时，把空格/制表符写成 `&#x20;` / `&#x9;` 转义序列。 |

## 4. 核心概念与源码讲解

typst-html 的空白保护是一条**两遍流水线**，理解这条主线就抓住了全篇：

1. **第一遍（边转换边保护）**：在 `handle_text` 和 `Converter::push` 里，把**制表符**和**连续空白**立即包进 pre-wrap 外壳。单个「正常位置」的空格先放过去，留作裸的 `Text(" ")` 节点。
2. **第二遍（整块回扫）**：`protect_spaces` 用 `Protector` 状态机扫一整遍块级上下文，依据左右邻居判定每个裸的单个空格是否会被折叠，需要保护的再补上 pre-wrap 外壳。

下面按最小模块逐个拆解。

---

### 4.1 Whitespace 模式与两遍调度

#### 4.1.1 概念说明

并非所有上下文都需要保护空白。`<pre>` 元素本身就带 `white-space: pre`，浏览器不会折叠它内部的空白；`<script>` / `<style>`（raw 文本）和 `<textarea>` / `<title>`（可转义 raw 文本）也不走普通空白规则。对这些上下文，再额外包 pre-wrap 外壳纯属浪费。

因此 typst-html 用一个枚举 `Whitespace` 区分两种处理模式：

- `Normal`：默认模式，需要对抗折叠（启用两遍保护）。
- `Pre`：原样输出，不做任何保护。

#### 4.1.2 核心流程

`Whitespace` 的值在 `handle_html_elem` 里被决定：当父级已是 `Pre`，或当前元素标签是 `pre` / raw / escapable-raw 时，子内容切到 `Pre`，否则保持 `Normal`。随后该值贯穿整个 `Converter`，决定 `handle_text`、`push`、`flush_whitespace`、`protect_spaces` 的行为。`protect_spaces`（第二遍）只在 **`Block` + `Normal`** 时运行——行内上下文不单独跑第二遍，而是等外层块级上下文的第二遍递归进来处理。

#### 4.1.3 源码精读

`Whitespace` 枚举及其权威注释，把整个机制的设计意图说得很清楚——制表符与连续空白「转换时」即包 span，单个空格「单独一遍」处理：

[src/convert.rs:33-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L33-L57) —— 定义 `Normal`（折叠对抗）与 `Pre`（原样输出）两态。

`handle_html_elem` 里切换模式的关键判断：父级 `Pre`、或标签是 `pre` / raw / escapable-raw 时强制 `Pre`：

[src/convert.rs:176-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L176-L185) —— 决定子内容的 `Whitespace` 模式。

`convert_to_nodes` 的两遍调度：先逐个 `handle`，结束后仅在 `Block + Normal` 时调 `protect_spaces`：

[src/convert.rs:60-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L60-L90) —— 入口函数，第 84–87 行是第二遍的触发点。

分类函数 `is_raw` / `is_escapable_raw` 决定了哪些标签会触发 `Pre`：

[src/tag.rs:145-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L145-L152) —— `script`/`style` 为 raw，`textarea`/`title` 为 escapable-raw。

#### 4.1.4 代码实践

**实践目标**：确认「`Pre` 模式短路一切保护」。

**操作步骤**：

1. 在 Typst 里写一段被 `<pre>` 包裹、含连续空格和制表符的内容：

   ```typst
   #html.elem("pre")["a   b\tc"]
   ```

2. 编译为 HTML：`typst compile doc.typ doc.html`（命令与确切输出待本地验证）。
3. 在输出的 HTML 里搜索 `pre-wrap`。

**需要观察的现象**：`<pre>` 内部的连续空格和制表符应**原样**出现，且**没有** `<span style="white-space:pre-wrap">` 外壳。

**预期结果**：因为 `pre` 标签让子内容进入 `Whitespace::Pre`，`handle_text` 在第 279–282 行直接原样输出，既不分割也不保护。这与「非 `<pre>` 下连续空格必然被包进 pre-wrap」形成对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `protect_spaces` 只在 `Block + Normal` 下运行，行内上下文却不单独跑？
**答案**：行内上下文的单个空格是否会被折叠，取决于它在**整段行内流**里左右遇到什么元素（可能跨多个行内子元素）。这需要跨越元素层级的「前瞻/后顾」，只有从块级上下文自顶向下递归（`Protector.visit_nodes` 会递进行内元素）才能正确判断；行内片段自己没有完整视野，故交给外层块级的第二遍统一处理。

---

### 4.2 Converter：转换状态机与 push/finish

#### 4.2.1 概念说明

`Converter` 是 `convert_to_nodes` 在单次转换过程中持有的可变状态机。它把「逐个元素处理」与「空白追踪/保护」两件事耦合在同一个结构里，这样每 `push` 一个节点时都能即时维护尾随空白信息。它的关键字段有三个：`output`（已产出的节点数组）、`trailing`（当前尾随空白记录）、`whitespace`（当前模式）。

#### 4.2.2 核心流程

`Converter` 的核心是 `push` 方法——**所有**节点产出都经过它。`push` 在写入节点前后维护 `trailing`：

- 若推入的是单个 `" "` 或 `"\t"` 文本节点 → 更新或新建 `TrailingWhitespace` 记录。
- 若推入的是普通节点（非 `Tag`）→ 先 `flush_whitespace()`，把已积累的、需要保护的尾随空白落袋为安。
- `Tag` 节点（内省元数据，不产生 HTML 输出）既不触发 flush 也不算空白，直接透传。

转换结束时 `finish()` 再做一次 `flush_whitespace()`，确保末尾的多字符空白也被保护。

#### 4.2.3 源码精读

`Converter` 结构体定义，注意 `trailing: Option<TrailingWhitespace>`：

[src/convert.rs:513-520](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L513-L520) —— 转换状态机的字段。

`push` 方法——节点产出的唯一入口，负责在写入前后维护 `trailing`：

[src/convert.rs:538-557](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L538-L557) —— 关键逻辑：纯空格/制表符节点更新 `trailing`，其余非 Tag 节点先 flush。

`finish` 收尾时再 flush 一次：

[src/convert.rs:532-535](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L532-L535) —— 保证末尾的多字符空白被保护。

#### 4.2.4 代码实践

**实践目标**：理解 `Tag` 节点「透明」穿过 `push` 的设计。

**操作步骤**：阅读 `push` 的 `else if !matches!(node, HtmlNode::Tag(_))` 分支，再对照 u2-l1 中 `HtmlNode::Tag` 的定义（它只承载内省元数据，不出现在最终 HTML 里）。

**需要观察的现象/预期结果**：`Tag` 节点既不会被当成空白，也不会触发 `flush_whitespace`，因此两个被 `Tag` 隔开的真实节点之间的 `trailing` 状态不会被 `Tag` 干扰。这保证了内省标记的插入不会破坏空白追踪的连续性。

#### 4.2.5 小练习与答案

**练习 1**：如果把一个普通文本节点 `"x"` 推入一个正持有 `trailing`（`single=false`）的 `Converter`，会发生什么？
**答案**：`push` 走 `else if !matches!(node, HtmlNode::Tag(_))` 分支，先调 `flush_whitespace()`——因为 `single=false`，会把已积累的尾随空白节点收拢并用 `pre_wrap` 包起来，再写入 `"x"`。

---

### 4.3 TrailingWhitespace 与 flush_whitespace：尾随空白追踪

#### 4.3.1 概念说明

`TrailingWhitespace` 记录「`output` 末尾是否拖着一串待定的空白节点」，核心是两个字段：

- `single: bool`——这串空白是否**恰好只有一个 ASCII 空格**。
- `from: usize`——这串空白在 `output` 里的起始下标。

为什么要区分 `single`？因为单个普通空格是否需要保护，要看它**两侧**的 DOM 邻居（左右都得是不折叠的上下文才算安全），这需要第二遍的前瞻/后顾；而制表符、连续空白无论邻居是谁都会被折叠，可以**立刻**保护。所以 `flush_whitespace` 故意**跳过** `single=true` 的情形，把单个空格留到第二遍处理。

#### 4.3.2 核心流程

`push` 维护 `trailing` 的规则（结合 4.2）：

| 推入的节点 | `trailing` 原状态 | 动作 |
|------------|------------------|------|
| `" "` | 空 | 新建 `trailing{single: true}` |
| `"\t"` | 空 | 新建 `trailing{single: false}`（制表符天生非 single） |
| `" "` 或 `"\t"` | 已有 | `ws.single = false`（第二个空白节点使其变成多字符串） |
| 普通（非 Tag）节点 | 任意 | 先 `flush_whitespace()`，再写入 |

`flush_whitespace` 只处理 `single == false` 的尾随空白：把 `output[from..]` 切下来，用 `pre_wrap` 包成一个 `<span>` 元素塞回原位。`single == true` 的情形被模式匹配跳过（且 `.take()` 会顺手清空记录）。

#### 4.3.3 源码精读

`TrailingWhitespace` 结构体：

[src/convert.rs:522-528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L522-L528) —— `single` 与 `from` 两字段。

`flush_whitespace`——只保护 `single:false` 的尾随空白，单个空格交给第二遍：

[src/convert.rs:571-584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L571-L584) —— 注意 `let ... { single: false, from } = self.trailing.take()`，模式只匹配多字符空白；对 `single:true`，`.take()` 仍会清空记录但不保护。

#### 4.3.4 代码实践

**实践目标**：用伪数据走一遍 `trailing` 状态迁移。

**操作步骤**：假设依次推入 `Text("a")`、`Text(" ")`、`Text(" ")`、`Text("b")`，逐步步进 `trailing`：

1. `Text("a")`：非空白 → flush（无 trailing）→ 写入。`trailing=None`。
2. `Text(" ")`：新建 `trailing{single:true, from=1}`。
3. `Text(" ")`：已有 → `single=false`。
4. `Text("b")`：非空白 → flush：`single=false` → 把下标 1..（两个空格）切下，包成 pre-wrap span，再写入 `"b"`。

**预期结果**：输出为 `[Text("a"), Element(pre_wrap[Text(" "), Text(" ")]), Text("b")]`。两个连续空格在第一遍就被即时保护。

#### 4.3.5 小练习与答案

**练习 1**：单个制表符 `Text("\t")` 为何一进 `push` 就注定被立即保护，而单个空格 `Text(" ")` 却要等到第二遍？
**答案**：`push` 对首个空白节点用 `single: text == " "` 初始化——制表符使 `single=false`，空格使 `single=true`。`flush_whitespace` 只保护 `single=false`。原因：制表符无论如何都会被浏览器折叠成一个空格，必须保护；而单个空格在「两侧都是普通内容」时本就不会被折叠，是否保护取决于上下文，故推迟到 `protect_spaces`。

**练习 2**：`flush_whitespace` 里 `self.trailing.take()` 放在 `if let` 的条件中，若 `trailing` 是 `single:true`，记录会被清掉吗？
**答案**：会。`.take()` 在求值条件时即执行（仅当 `whitespace==Normal`），把 `trailing` 置为 `None` 并返回旧值；旧值不匹配 `single:false` 时 if 体不执行，但记录已被清空，那枚裸的单空格节点留在 `output` 里等待第二遍裁决。

---

### 4.4 handle_text：第一遍，即时保护制表符与连续空白

#### 4.4.1 概念说明

`handle_text` 处理一个 `TextElem` 的文本字符串（也会被智能引号、`SmartQuote` 等复用）。它逐字符扫描，识别四类「特殊字符」：ASCII 空格（`Space`）、制表符（`Tab`）、换行（`Newline`，CR/LF）、Unicode 默认可忽略字符（`Ignorable`）。对它们分别处理：

- 换行 → 产出 `<br>`（`Normal` 模式）。
- 制表符 → 产出单个 `Text("\t")`，由 `push`/`flush` 即时保护。
- 连续空格/制表符 → 拆成单个 `Text(" ")` / `Text("\t")`，`trailing` 变 `single=false`，即时保护。
- **恰好被两个普通字符夹住的单个空格** → 视为天然安全，保留在原文本串里不拆分（不产生裸节点，第二遍也不会看到它）。
- 其余位置的单个空格（行首、行尾、紧邻特殊字符） → 拆成裸 `Text(" ")`（`single=true`），推迟到第二遍。

#### 4.4.2 核心流程

```
handle_text(text):
  若 Whitespace::Pre → 整段原样 push，结束
  逐字符 c：
    kind = Kind::of(c)        # None 表示普通字符
    若 c 是单个空格 且 前一字符普通 且 后一字符普通：
        continue（留在原串，天然安全）
    否则若 kind 是特殊字符：
        先把 [emitted..i] 的普通文本 push 出去
        按 kind 处理：Space→push ' '，Tab→push '\t'，
                      Newline→push <br>，Ignorable→push 该字符
  收尾：把剩余普通文本 push 出去
```

关键：被「前后都是普通字符」夹住的单个空格走 `continue`，既不拆分也不单独保护；其余空格都拆成裸节点，交给 `push`（多字符/制表符立刻保护）或第二遍（单个边界空格）。

#### 4.4.3 源码精读

`Kind` 枚举与 `Kind::of` 分类函数：

[src/convert.rs:253-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L253-L277) —— 四类特殊字符的判定。

`Pre` 模式直接原样返回：

[src/convert.rs:279-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L279-L282) —— `Pre` 上下文不做任何处理。

「天然安全的单个空格」优化——前后皆普通字符时 `continue`：

[src/convert.rs:293-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L293-L300) —— 注意它要求 `prev_kind` 为 `Some(None)`（前一字符存在且普通）且 `after` 存在且普通，故行首/行尾/紧邻特殊字符的空格都不满足。

特殊字符的拆分与分发（含 `<br>`、`\t`、CR+LF 合并）：

[src/convert.rs:302-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L302-L323) —— Newline 里对 `\r\n` 做了特殊跳过（交给随后的 `\n` 变 `<br>`）。

`handle` 调度链中对 `SpaceElem`（Typst 的空格元素）的处理——直接变成一个 `Text(" ")`，同样进入 `push` 的尾随追踪：

[src/convert.rs:102-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L102-L103) —— Typst 的空格元素被映射为单个 ASCII 空格文本节点。

#### 4.4.4 代码实践

**实践目标**：手工预测 `handle_text` 对三种输入的拆分结果。

**操作步骤**：对下面三段文本，按 4.4.2 的流程推导会产出哪些节点。

| 输入文本 | 推导 | 第一遍产出 |
|----------|------|-----------|
| `"a\tb"` | 制表符不是 `Space`，跳过「天然安全」优化；拆出 `Text("\t")`（`single=false`） | `Text("a")`、pre-wrap(`Text("\t")`)、`Text("b")` |
| `"a  b"`（两空格） | 第一个空格后一字符是空格（特殊）→ 不满足优化 → 拆；第二个空格前一字符是空格 → 不满足 → 拆；`trailing` 变 `single=false` | `Text("a")`、pre-wrap(`Text(" ")`,`Text(" ")`)、`Text("b")` |
| `"a b"`（一空格） | 空格前后都是普通字符 → `continue`，留在原串 | 单个 `Text("a b")`，不拆分、不保护 |

**预期结果**：制表符与连续空格在第一遍即被 pre-wrap 包裹；被普通字符夹住的单个空格保留在文本串里，天然安全。（上述产出基于源码逻辑推导，确切的 Typst 文本元素化结果待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：文本 `"a "`（末尾一个空格）会产出什么节点？它会立即被保护吗？
**答案**：末尾空格的前一字符 `'a'` 普通，但「后一字符」不存在（`text[i+1..].chars().next()` 为 `None`），不满足「天然安全」条件，故被拆成裸 `Text(" ")`，`trailing{single:true}`。它在第一遍**不会**被保护（`flush_whitespace` 跳过 `single:true`），留到第二遍由 `protect_spaces` 依据右邻居裁决（块末尾属折叠边界，通常会被保护）。

**练习 2**：为什么 `\r\n` 里的 `\r` 不会被单独处理？
**答案**：见第 312–318 行，当遇到 `\r` 且紧随其后是 `\n` 时，跳过 `\r`（`emitted += 1; continue`），让随后的 `\n` 统一变成一个 `<br>`，避免产生两个换行。

---

### 4.5 protect_spaces 与 Protector：第二遍，保护单个空格

#### 4.5.1 概念说明

第一遍过后，`output` 里可能残留一些裸的 `Text(" ")`（单个空格，`single=true` 未被保护）。它们是否会被浏览器折叠，取决于**两侧 DOM 邻居**：若两边都是「支撑空格」的内容（普通文字、行内元素、替换元素 `<img>` 等），空格安全；若任一侧是「折叠空格」的边界（块级元素、`<br>`、块首块尾），空格会被吞掉。

`Protector` 是一个三态状态机，在一次遍历里同时完成「后顾」与「前瞻」：

- **`Collapsing`**：左侧上下文会折叠空格（块首、刚遇到块级元素/`<br>`）。
- **`Supportive`**：左侧上下文支撑空格（刚遇到普通文字/行内/替换元素）。
- **`Space(&mut node)`**：刚刚经过一个待定的单个空格，且它的左侧是支撑的——还不知道右侧，先攥着不保护。

#### 4.5.2 核心流程

`protect_spaces` 入口创建 `Protector::Collapsing`，调用 `visit_nodes` 遍历整个块级节点数组，结束时再调一次 `collapsing()`（处理块尾边界）。`visit_nodes` 对每个节点的处理：

```
Tag         → 忽略
Text(" ")   → 按当前状态：
               Collapsing → 立即保护该空格，转 Supportive
               Supportive → 转 Space(该空格)（攥住，等右侧）
               Space(prev)→ 保护 prev，转 Space(该空格)
Text(其他含可见字符) → supportive()（普通文字支撑空格）
Element     → 若 is_whitespace_collapsing(tag)（块级/<br>）→ collapsing()
              若 is_replaced(tag)（<img> 等）→ supportive()
              否则（行内元素）且非 pre_span → 递归进 children
Frame       → supportive()（SVG 帧像图片一样支撑空格）
```

`collapsing()`：若当前攥着 `Space(node)`，则保护它（它右侧是折叠边界），状态归 `Collapsing`。
`supportive()`：直接转 `Supportive`——若攥着 `Space`，说明该空格左右都支撑，**丢弃不保护**（安全）。

关键：「攥住」机制让一个空格同时看到左右两侧：遇到时记下（若左支撑），离开时根据右侧决定保护与否。

#### 4.5.3 源码精读

`protect_spaces` 入口：

[src/convert.rs:587-595](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L587-L595) —— 新建 `Protector`、遍历、收尾 `collapsing()`。

`Protector` 三态枚举：

[src/convert.rs:597-602](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L597-L602) —— `Collapsing` / `Supportive` / `Space`。

`visit_nodes` 主体——节点分发与状态迁移：

[src/convert.rs:610-650](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L610-L650) —— 注意对元素：块级/`<br>` 调 `collapsing`、替换元素调 `supportive`、行内元素递归且跳过 `pre_span` 自产外壳。

`collapsing` 与 `supportive`：

[src/convert.rs:652-664](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L652-L664) —— `collapsing` 保护攥住的 `Space`；`supportive` 丢弃攥住的 `Space`（左右皆支撑，安全）。

`tag.rs` 里两个关键分类，决定元素走哪条分支：

[src/tag.rs:480-492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L480-L492) —— `is_replaced`（`audio`/`canvas`/`img`/`input`/`video` 等）→ 支撑空格。

[src/tag.rs:498-502](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L498-L502) —— `is_whitespace_collapsing`（默认 `display:block` 或 `<br>`）→ 折叠边界。

#### 4.5.4 代码实践

**实践目标**：用状态机追踪一个「空格夹在块级与行内之间」的例子。

**操作步骤**：假设块级 `output` 为 `[Element(<div>), Text(" "), Element(<span>...), Text(" ")]`（末尾再无节点）。从 `Collapsing` 起步逐个走：

1. `<div>`：`is_whitespace_collapsing` → `collapsing()`（无攥住的 Space）→ `Collapsing`。
2. `Text(" ")`：`Collapsing` → 立即保护该空格 → `Supportive`。
3. `<span>`：非折叠、非替换、行内 → 递归其 children；递归回来后状态由 children 决定（假设内部是普通文字）→ `Supportive`。
4. `Text(" ")`：`Supportive` → 转 `Space(此空格)`，攥住。
5. 遍历结束，`protect_spaces` 调 `collapsing()`：攥着 `Space` → 保护它（块尾是折叠边界）。

**预期结果**：两个单个空格都被保护。第 1 个因为它紧贴块级 `<div>`（左侧折叠），第 2 个因为它在块尾（右侧折叠）。

#### 4.5.5 小练习与答案

**练习 1**：`output = [Text("a"), Text(" "), Text("b")]`（单个空格夹在两段普通文字之间），第二遍会保护这个空格吗？
**答案**：不会。`Text("a")` 含可见字符 → `supportive()` → `Supportive`；`Text(" ")` → 转 `Space`（攥住）；`Text("b")` → `supportive()` → 转 `Supportive`，**丢弃**攥住的 `Space`。收尾 `collapsing()` 时已无 `Space`。空格左右都支撑，本就不会被折叠，故不保护——这正是延迟到第二遍的意义。

**练习 2**：为什么 `visit_nodes` 遇到 `pre_span` 为真的元素就直接跳过、不递归？
**答案**：`pre_span` 标记的是第一遍自产的 pre-wrap 外壳，其内部空白已经被外壳的 `white-space:pre-wrap` 保护，且外壳本身不是普通的行内文本上下文。若再递归进去用 `Protector` 判定，既无必要也可能误伤（把外壳里的空格再包一层）。所以用 `!element.pre_span` 守卫跳过自产外壳。

---

### 4.6 pre_wrap 与 pre_span：保护外壳及其编码

#### 4.6.1 概念说明

无论第一遍还是第二遍，保护的落点都是同一个函数 `pre_wrap`：它把一串空白节点包进一个带 `white-space: pre-wrap` 的 `<span>`，并把这个 `<span>` 的 `pre_span` 字段置为 `true`。

`pre_span`（见 `HtmlElement`）有两重意义：

1. **转换期**：`Protector` 据此跳过自产外壳（见 4.5），避免重复保护。
2. **编码期**：`encode.rs` 在写出 `pre_span` 元素的文本时，会把空格/制表符写成 HTML 字符引用（`&#x20;` / `&#x9;`）。这对浏览器渲染并非必需（`white-space:pre-wrap` 已足够），但能防止 HTML 格式化工具把连续空白「整理」掉而破坏保护。

#### 4.6.2 核心流程

```
pre_wrap(nodes):
  span = <span> with css "white-space: pre-wrap", children = nodes
  span.pre_span = true
  return span

protect_space(node):  # 第二遍保护单个空格
  node = pre_wrap([node.clone()])

flush_whitespace():    # 第一遍保护多字符空白
  output[from..] 切下 → pre_wrap(nodes) → 塞回原位

编码期（encode.rs）:
  write_children 把 element.pre_span 透传给 write_node → write_text(escape=pre_span)
  escape=true 时每个字符走 write_escape：
    空格 → &#x20;   制表符 → &#x9;
```

#### 4.6.3 源码精读

`pre_wrap` 构造外壳并打上 `pre_span` 标记：

[src/convert.rs:672-683](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L672-L683) —— 用 `css::Properties::new().with("white-space", "pre-wrap")` 设样式，`elem.pre_span = true`。

`protect_space`——把单个空格节点原地替换成 pre_wrap 外壳：

[src/convert.rs:667-670](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L667-L670) —— 第二遍保护单个空格的落点。

`HtmlElement.pre_span` 字段及其文档说明（为何要把空格/制表符写成转义序列）：

[src/dom.rs:198-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L198-L205) —— `pre_span` 的语义：让格式化工具不会破坏保护。

编码期：`write_children` 把 `element.pre_span` 作为 `escape_text` 透传：

[src/encode.rs:193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L193) —— `write_node(w, c, element.pre_span)`，pre_span 决定子文本是否强制转义。

`write_text` 与 `write_escape`：`escape=true` 时每个字符都走转义；空格/制表符属 W3C 文本字符，走 `&#x{hex};` 分支：

[src/encode.rs:100-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L100-L109) —— `escape || !is_valid_in_normal_element_text(c)` 触发转义。

[src/encode.rs:368-382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L368-L382) —— 空格/制表符匹配 `is_w3c_text_char(c) && c != '\r'`，输出 `&#x20;` / `&#x9;`。

> 配套确认：`charsets::is_w3c_text_char` 对制表符（控制字符但属 ASCII 空白）和空格都返回 `true`，故二者都会被写成字符引用——见 [src/charsets.rs:53-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L53-L62)。

#### 4.6.4 代码实践

**实践目标**：观察 `pre_span` 对最终 HTML 文本的影响。

**操作步骤**：

1. 编译一段会产生连续空格的内容（例如 `#html.elem("p")["x   y"]`）为 HTML（确切输出待本地验证）。
2. 打开生成的 HTML，定位到对应的 `<span style="white-space:pre-wrap">`。
3. 查看该 span 内部的空格是以原始空格还是 `&#x20;` 形式出现。

**需要观察的现象**：pre-wrap 外壳内部的空格应被写成 `&#x20;`（连续三个 `&#x20;`），而非裸空格。

**预期结果**：这正是 `pre_span=true` 让 `write_text` 强制转义的效果。它保证了即使用外部工具美化 HTML，连续空格也不会被折叠或裁剪。

#### 4.6.5 小练习与答案

**练习 1**：如果去掉 `pre_wrap` 里的 `elem.pre_span = true`，会在哪两个阶段产生问题？
**答案**：(1) 转换期——`Protector.visit_nodes` 会把自产外壳当成普通行内元素递归进去，可能对其内部空格重复保护；(2) 编码期——`write_text` 的 `escape` 会是 `false`，空格/制表符以裸字符写出，外部 HTML 格式化工具可能折叠它们，使保护失效。

**练习 2**：`white-space: pre-wrap` 已经能阻止浏览器折叠，为何还要把空格写成 `&#x20;`？
**答案**：浏览器渲染确实只需 `pre-wrap`。但 typst-html 的 HTML 输出可能被 prettier/htmlformatter 之类的工具二次处理，这类工具按「普通」空白规则整理文本，会把连续裸空格折叠或删除。写成字符引用让这些工具无法识别为可折叠空白，从而保住保护。注释里明确说「ensures that formatters won't mess up the output」。

---

## 5. 综合实践

**任务**：用一个同时含**连续空格**和**制表符**的例子，完整跑通两遍保护，说明哪些空白在 `handle_text`（第一遍）就被包进 pre-wrap，哪些要等到 `protect_spaces`（第二遍）。

### 步骤 1：编写 Typst 源

```typst
#html.elem("p")[
  #("a\tb")
  #("c   d")
  #("e f g")
  #("tail ")
]
```

> 说明：用 `#("...")` 字符串字面量是为了尽量精确控制文本内容（Typst 对标记模式下的空白有自己的折叠规则，字符串字面量内 `\t` 是制表符、多个空格被保留）。实际经过 Typst 文本元素化后的确切内容待本地验证。

### 步骤 2：编译并定位保护外壳

```bash
typst compile --format html doc.typ doc.html
grep -o 'white-space:pre-wrap' doc.html | wc -l   # 统计 pre-wrap 外壳数量
grep -o '&#x[0-9a-f]*;' doc.html                  # 查看转义序列
```

（命令与确切计数待本地验证。）

### 步骤 3：用源码逻辑解释观察到的现象

逐项推导每个片段（基于本讲源码）：

| 片段 | 第一遍 `handle_text` 行为 | 是否第一遍即保护 |
|------|--------------------------|------------------|
| `"a\tb"` | 制表符不满足「天然安全」（仅限 `Space`）→ 拆出 `Text("\t")`，`single=false` | **是**，`flush_whitespace` 包成 pre-wrap（含 `&#x9;`） |
| `"c   d"` | 三个连续空格，每个都不满足「前后皆普通」（中间几个前后都是空格）→ 拆成三个 `Text(" ")`，`trailing` 变 `single=false` | **是**，三个空格一起包进一个 pre-wrap |
| `"e f g"` | 每个空格前后都是普通字符 → `continue`，留在原文本串 | **否**，且第二遍也不会动它（不是裸 `Text(" ")` 节点，天然安全） |
| `"tail "` | 末尾空格「后一字符」不存在 → 拆成裸 `Text(" ")`，`single=true` | **否**，留到第二遍；因它在块尾（折叠边界），`protect_spaces` 收尾的 `collapsing()` 会保护它 |

### 步骤 4：验证你的判断

- 预测 `grep` 出的 pre-wrap 外壳数量应与「制表符段 + 连续空格段 + 末尾单空格段」一致（`e f g` 不产生外壳）。
- 预测转义序列里既有 `&#x9;`（制表符）也有 `&#x20;`（空格）。

把本地实际输出与本表对照；若有出入，优先检查 Typst 对字符串字面量外、标记层级的空白折叠（那是 Typst 的文本元素化，发生在 `handle_text` 之前）。

## 6. 本讲小结

- typst-html 用**两遍流水线**对抗浏览器空白折叠：第一遍在 `handle_text`/`push` 即时保护**制表符与连续空白**，第二遍 `protect_spaces` 依据 DOM 邻居保护**会被折叠的单个空格**。
- `Whitespace::Normal` 启用保护、`Pre` 原样输出；`Pre` 由 `<pre>` / raw / escapable-raw 标签或继承自父级触发，第二遍只在 `Block + Normal` 运行。
- `Converter.push` 是节点产出的唯一入口，用 `TrailingWhitespace{single, from}` 追踪尾随空白；`single` 区分「单个空格（推迟）」与「多字符/制表符（立即保护）」。
- `handle_text` 的「前后皆普通字符的单个空格」优化让天然安全的空格留在文本串里、不产生裸节点；其余空格拆成裸 `Text(" ")` 等待第二遍。
- `Protector` 三态（`Collapsing`/`Supportive`/`Space`）用「攥住当前空格看左右」的方式在一次遍历里完成前瞻与后顾；块级/`<br>` 是折叠边界，普通文字/行内/替换元素/Frame 支撑空格。
- `pre_wrap` 统一生成 `<span style="white-space:pre-wrap">` 外壳并打 `pre_span=true`；`pre_span` 既让 `Protector` 跳过自产外壳，又让编码期把空格/制表符写成 `&#x20;`/`&#x9;` 以防格式化工具破坏。

## 7. 下一步学习建议

- **u4-l2（display 属性与块级/行内提升）**：本讲的 `Protector` 大量依赖 `is_whitespace_collapsing`（本质是 `Display::default_for`）。下一讲讲清 `Display` 枚举与 `make_block_level`/`make_inline_level`，你会明白为什么某些元素被判定为「折叠边界」、某些被判定为「替换元素支撑空格」。
- **u4-l3（CSS 属性系统与内联样式解析）**：`pre_wrap` 用 `css::Properties::new().with("white-space","pre-wrap")` 写入元素 `css` 字段，这些样式最终如何变成 `style="..."` 属性，由 `resolve_inline_styles` 完成。
- **u5-l1（DOM 到 HTML 字符串的编码）**：本讲提到的 `pre_span` → `&#x20;`/`&#x9;` 转义、`write_escape` 的完整路径在编码讲义里系统展开。
- 建议再读一遍 `src/convert.rs` 第 522–695 行，把 `TrailingWhitespace`、`flush_whitespace`、`Protector`、`pre_wrap` 四段连起来通读，体会「同一套 pre-wrap 外壳，两个时机调用」的设计简洁性。
