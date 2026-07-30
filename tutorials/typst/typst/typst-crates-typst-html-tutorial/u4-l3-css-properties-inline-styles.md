# CSS 属性系统与内联样式解析

## 1. 本讲目标

typst-html 在把 Typst 文档编译成 HTML 的过程中，会产生大量「编译器自己决定要加」的 CSS，比如 `white-space: pre-wrap`（防止浏览器折叠空白）、`display: block`（让某个元素按块级渲染）。这些 CSS 并不是直接写进元素的普通属性里，而是先放进一个独立的「CSS 属性列表」，等到最后一步再统一变成 HTML 的 `style="..."` 属性。

本讲带你读懂 `src/css/` 这个子模块，学完后你应当能够：

- 说清 `Properties` 这个「按属性名排序、自动去重」的列表结构，以及它为什么这样设计。
- 掌握 `PropertiesBuilder` 如何把 Typst 的类型（如长度、颜色）序列化成 CSS 字符串，并在失败时优雅降级。
- 理解 `to_inline()` 如何把属性列表拼成一段 `name: value; name: value` 的文本。
- 走通 `resolve_inline_styles` 这个 DOM 遍历函数：它如何把每个元素的 `css` 字段写成 `style` 属性，并在元素已有用户写的 `style` 属性时把它们合并。

本讲承接 [u2-l1](u2-l1-dom-data-model.md)（`HtmlElement` 数据模型）和 [u3-l1](u3-l1-document-compilation-pipeline.md)（编译主链路 `html_document_common`），聚焦主链路中靠后的「内联样式解析」这一步。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **`HtmlElement` 的字段结构**：它有一个 `css` 字段专门存放编译器生成的 CSS，与用户写的普通属性 `attrs` 分开。参见 [u2-l1](u2-l1-dom-data-model.md)。
- **编译主链路的顺序**：`html_document_common` 依次执行 realize → `convert_to_nodes` → `finalize_dom` → `resolve_inline_styles`。参见 [u3-l1](u3-l1-document-compilation-pipeline.md)。
- **门面模式**：私有模块通过 `pub use` 把少数类型暴露出去的手法。参见 [u1-l2](u1-l2-module-structure.md)。
- **EcoVec / EcoString**：typst 用的写时复制（COW）容器，`make_mut()` 在需要修改时会先克隆一份。

几个本讲会用到的通俗概念：

- **CSS 属性（property）**：一段形如 `display: block` 的声明，左边是属性名，右边是值。
- **内联样式（inline style）**：直接写在 HTML 元素上的 `style="..."` 属性，多条声明用 `;` 分隔。浏览器解析时，对同一属性，写在后面的声明会覆盖前面的。
- **驻留/排序去重**：把同名的东西合并到一起，并按名字排序，使输出稳定且可用二分查找。

## 3. 本讲源码地图

本讲涉及三个文件，全部位于 `src/css/` 目录下，是 typst-html 唯一的目录型子模块：

| 文件 | 作用 |
| --- | --- |
| `src/css/mod.rs` | 子模块门面，重导出 `Properties`、`ToCss`、`resolve_inline_styles`，并声明两个私有子模块。 |
| `src/css/encode.rs` | 定义 `Properties`、`PropertiesBuilder`、`Property`、`to_inline()`，以及把 Typst 类型序列化成 CSS 的 `ToCss` trait 与 `CssWriter`。 |
| `src/css/resolve.rs` | 定义 `resolve_inline_styles`，遍历 DOM 把 `css` 字段写成 `style` 属性。 |

此外会引用到两个外部佐证文件：

- `src/dom.rs`：`HtmlElement.css` 字段与 `with_css` 方法。
- `src/document.rs`：`resolve_inline_styles` 在编译主链路中的调用点。
- `src/attr.rs`：预定义的 `style` 属性常量。

## 4. 核心概念与源码讲解

### 4.1 CSS 子模块全貌与样式数据流

#### 4.1.1 概念说明

`src/css/` 子模块只解决一件事：**管理编译器生成的 CSS，并最终把它变成 `style` 属性**。

这里有一个关键区分，初学者容易混淆：

- **用户写的样式**：用户在 Typst 里用 `html.elem("div", style: "color: red")` 等方式手写的 `style`，会进入元素的普通属性表 `attrs`，被当作普通字符串属性对待。
- **编译器生成的样式**：typst-html 在转换过程中自己决定要加的 CSS（如空白保护、display 提升），存放在 `HtmlElement` 专门的 `css` 字段里。

这两类样式一开始是分开存放的。`resolve_inline_styles` 的职责，就是在输出前把它们合并到同一个 `style` 属性里。`resolve.rs` 顶部的文档注释明确说明了这一点：

> Turns CSS properties on all elements in the DOM into inline `style` attributes.
> （把 DOM 中所有元素上的 CSS 属性转换成内联 `style` 属性。）

#### 4.1.2 核心流程

整个样式数据流可以这样描述：

1. **转换阶段（convert）**：`convert.rs` 在生成 `HtmlElement` 时，通过 `with_css(...)` 或 `set_display(...)` 往元素的 `css` 字段里塞属性。此时 `css` 是一个 `Properties` 列表，**还不是** `style` 字符串。
2. **装订阶段（finalize_dom）**：`finalize_dom` 可能新增带样式的节点（如脚注容器），所以样式解析必须排在它之后。
3. **解析阶段（resolve_inline_styles）**：从根元素开始递归遍历整棵 DOM 树，把每个元素非空的 `css` 字段序列化成内联文本，合并进 `style` 属性。
4. **编码阶段（encode）**：`encode.rs` 把最终的 `style` 属性像普通属性一样输出到 HTML 字符串。

用伪代码表示：

```text
convert:   HtmlElement { css: Properties[white-space: pre-wrap], attrs: {...}, ... }
            ↓
resolve:   把 css 拼成 "white-space: pre-wrap"，写入 attrs.style
            ↓
encode:    输出 <span style="white-space: pre-wrap">...</span>
```

#### 4.1.3 源码精读

先看子模块的门面 `src/css/mod.rs`，它只有 5 行，但交代了全部对外接口：

[crates/typst-html/src/css/mod.rs:1-5](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/mod.rs#L1-L5) —— 重导出 `Properties`、`ToCss`、`resolve_inline_styles`，并把 `encode`、`resolve` 声明为私有模块。

这里体现了「模块私有、类型公开」的门面手法：`encode` 和 `resolve` 两个文件对外不可见，但它们定义的 `Properties`、`ToCss`、`resolve_inline_styles` 通过 `pub use` 暴露到了 crate 根。

再看 `css` 字段在 `HtmlElement` 上的定义：

[crates/typst-html/src/dom.rs:189-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L189-L190) —— `pub css: css::Properties`，文档注释强调它「Currently only used for generated styles」（当前仅用于编译器生成的样式）。

这个注释非常关键：它告诉我们 `css` 字段是给编译器用的内部通道，和用户写的 `style` 属性不是一回事。

最后看 `resolve_inline_styles` 在主链路中的位置：

[crates/typst-html/src/document.rs:188-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L188-L190) —— 在 `finalize_dom` 之后调用 `css::resolve_inline_styles(output.root_mut())`。

紧跟的注释写道：「Since `finalize_dom` might have inserted more DOM nodes that have styles, the styles must be resolved last.」（因为 `finalize_dom` 可能插入了更多带样式的 DOM 节点，所以样式必须最后解析。）这解释了为什么解析必须排在整个 DOM 成型之后。

#### 4.1.4 代码实践

**实践目标**：在真实 Typst 文档中观察编译器生成的 CSS 是如何出现的。

**操作步骤**：

1. 准备一个最小 Typst 文件 `demo.typ`，内容包含一个会产生空白保护的输入（例如一段含制表符或连续空格的文字，这会触发 `pre-wrap`，详见 [u4-l1](u4-l1-whitespace-protection.md)）：

   ```typst
   #set page(width: auto)
   文本A	  文本B
   ```

2. 用 typst CLI 编译为 HTML：

   ```bash
   typst compile --format html demo.typ demo.html
   ```

3. 打开 `demo.html`，搜索 `white-space`。

**需要观察的现象**：输出 HTML 中应能看到类似 `<span style="white-space: pre-wrap">…</span>` 的片段。

**预期结果**：该 `style` 属性的值正是由本讲讲的 `css` 字段经 `resolve_inline_styles` 写入的。

**待本地验证**：具体哪些字符会触发 `pre-wrap`、以及最终的 HTML 片段形态，需以你本地的实际编译输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `resolve_inline_styles` 必须在 `finalize_dom` 之后执行，而不能在 `convert_to_nodes` 刚结束时执行？

**参考答案**：因为 `finalize_dom` 会往 DOM 里插入新的、带样式的节点（例如脚注容器）。如果在它之前解析样式，这些后插入节点的 `css` 字段就不会被处理，导致它们最终没有 `style` 属性。

**练习 2**：用户的 `style: "color: red"` 和编译器生成的 `white-space: pre-wrap`，在 DOM 成型之前分别存在哪里？

**参考答案**：用户写的 `style` 是普通属性，存在 `HtmlElement.attrs` 里（属性名为 `attr::style`）；编译器生成的 CSS 存在 `HtmlElement.css` 字段里（一个 `Properties` 列表）。两者在 `resolve_inline_styles` 阶段才合并。

### 4.2 Properties：有序去重的属性列表

#### 4.2.1 概念说明

`Properties` 是 typst-html 用来表示「一组 CSS 属性」的核心结构。你可以把它想成一个**始终按属性名排序、且不会有重名项**的列表。

为什么要求排序去重？原因有二：

- **确定性**：无论属性按什么顺序被塞进来，最终序列化出的 `style` 文本都一样。这对缓存正确性（comemo memoization，见 [u6-l4](u6-l4-caching-comemo-memoization.md)）和稳定的 diff 非常重要。
- **高效**：保持有序后，查找、插入、删除都能用二分查找，时间复杂度为 \( O(\log n) \)；而判断「是否已有同名属性」也变得简单。

`Property` 是列表里的一个元素，就是一对 `(name, value)`：

[crates/typst-html/src/css/encode.rs:116-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L116-L124) —— `Property` 结构体，`name` 是 `&'static str`（静态字符串），`value` 是 `EcoString`。

注意 `name` 用的是 `&'static str` 而不是 typst-html 在标签/属性系统里常用的驻留句柄 `HtmlTag`/`HtmlAttr`（见 [u2-l2](u2-l2-htmltag-interning.md)、[u2-l3](u2-l3-htmlattr-attrs-system.md)）。源码里有一条 TODO 注释「Use something similar to `HtmlAttr`」，说明这部分目前还没有做驻留优化，但因为 CSS 属性名都是编译期已知的少量静态字符串，影响不大。

#### 4.2.2 核心流程

`Properties` 的内部就是一个 `EcoVec<Property>`：

[crates/typst-html/src/css/encode.rs:16-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L16-L18) —— `Properties(EcoVec<Property>)`，派生了 `Eq/PartialEq/Hash` 等，说明它可以参与哈希与相等比较（这对 memoize 缓存是必需的）。

核心方法 `push` 的算法是一个经典的「二分插入或覆盖」：

```text
push(name, value):
    在列表里按 name 二分查找
    若找到（Ok）   → 用新值覆盖该位置的旧 Property（实现「去重 + 更新」）
    若没找到（Err）→ 在插入点 idx 处插入新 Property（保持有序）
```

用数学语言描述查找的位置：对于一个已按名字升序排列的序列 \( a_1 \le a_2 \le \dots \le a_n \)，二分查找返回的位置 \( i \) 满足

\[
\text{要么 } a_i = \text{name} \text{（命中，覆盖）}, \quad \text{要么 } i \text{ 是保持有序的插入点（插入）}.
\]

这样无论走哪个分支，列表都保持「有序且无重名」。

`to_inline()` 则负责把整张列表序列化成 `style` 属性的值：

```text
to_inline():
    对每个 Property (name, value)，按顺序拼接
    多条之间用 "; " 分隔
    每条形如 "name: value"
    → "name1: value1; name2: value2"
```

#### 4.2.3 源码精读

先看 `push` 的二分插入去重：

[crates/typst-html/src/css/encode.rs:31-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L31-L39) —— `binary_search_by_key` 按属性名查找；`Ok(idx)` 时覆盖，`Err(idx)` 时 `insert`。

注意第 34 行 `binary_search_by_key(&property.name, |p| p.name)`：比较的键就是 `Property.name`，即属性名字符串本身。这就是「按名排序、按名去重」的实现。

再看 `remove`，逻辑对称：

[crates/typst-html/src/css/encode.rs:42-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L42-L46) —— 二分查找到则删除，找不到则什么都不做。`set_display` 的 `None` 分支正是用它来清除 `display` 属性的。

接着是 builder 风格的 `with`：

[crates/typst-html/src/css/encode.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L48-L52) —— 消费 `self`，调用 `push` 后返回 `self`，便于链式调用。例如 `pre_wrap` 里就是 `.with_css(Properties::new().with("white-space", "pre-wrap"))`。

最关键的是 `to_inline()`：

[crates/typst-html/src/css/encode.rs:54-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L54-L65) —— 返回一个 `impl Display`，遍历 `self.iter()`，第一条之前不加分隔符，之后每条前加 `"; "`，每条写成 `"{name}: {value}"`。

这里 `self.iter()` 能用，是因为 `Properties` 实现了 `Deref`，把 `&self` 解引用成 `&[Property]`：

[crates/typst-html/src/css/encode.rs:68-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L68-L74) —— `Deref<Target = [Property]>`，让 `Properties` 可以直接当切片用。

最后看一个真实使用 `Properties` 的例子——空白保护里的 `pre_wrap`：

[crates/typst-html/src/convert.rs:675-683](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L675-L683) —— 构造一个 `<span>`，用 `.with_css(Properties::new().with("white-space", "pre-wrap"))` 把 `white-space` 属性写进 `css` 字段，并标记 `pre_span = true`。

注意这里**没有任何 `style` 字符串拼接**——只是把一个 `Properties` 对象交给 `with_css`。把 `Properties` 变成 `style="white-space: pre-wrap"` 的工作，要等到第 4.4 节的 `resolve_inline_styles`。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `Properties` 的去重与排序行为，加深对二分插入的理解。

**操作步骤**：在纸上（或写一段 Rust 伪代码）模拟对一个空 `Properties` 连续执行：

```text
push("white-space", "pre-wrap")
push("display", "block")
push("white-space", "normal")   // 同名，应覆盖
push("color", "red")
```

**需要观察的现象**：每一步后列表的内部顺序。

**预期结果**：由于按名字字符串排序，最终列表顺序为 `color`、`display`、`white-space`（按字典序），且 `white-space` 的值为最后一次的 `normal`（被覆盖）。对应的 `to_inline()` 输出为：

```text
color: red; display: block; white-space: normal
```

注意它与你 `push` 的顺序无关，这就是「有序去重」带来的确定性。

#### 4.2.5 小练习与答案

**练习 1**：如果同一个属性名 `push` 了两次，最终保留哪一个值？为什么？

**参考答案**：保留后 `push` 的值。因为二分查找命中（`Ok`）后会用新 `Property` 覆盖旧位置，等价于「同名属性的最后一次写入生效」。

**练习 2**：`to_inline()` 输出里多条声明之间的分隔符是什么？第一条声明前面会有分隔符吗？

**参考答案**：分隔符是 `"; "`（分号加一个空格）。第一条声明前不会加分隔符——代码用 `if i > 0` 判断，只有从第二条起才在前面写 `"; "`。

### 4.3 PropertiesBuilder 与 ToCss 序列化网关

#### 4.3.1 概念说明

4.2 节的 `push` 接收的是**已经序列化好的字符串值**（`impl Into<EcoString>`），比如 `"pre-wrap"`、`"block"`。但很多时候，编译器手里拿到的是 **Typst 的强类型值**，比如一个 `Length`（长度）、一个 `Color`（颜色），需要先转换成 CSS 字符串才能存进 `Properties`。

`PropertiesBuilder` 就是这座桥梁：它接收 `impl ToCss` 的 Typst 类型，内部用 `CssWriter` 把它序列化成 CSS 文本，再交给 `Properties`。更重要的是，它在序列化失败时（比如 Typst 支持、但 CSS 不支持的构造）**能优雅降级**——发出一条警告，并跳过这条属性，而不是让整个导出崩溃。

> 说明：`ToCss` trait 与 `CssWriter` 的完整细节（长度如何生成 `calc()`、颜色如何选 hex 还是 `rgb()` 等）是 [u4-l4](u4-l4-typst-to-css-conversion.md) 的主题。本节只需把 `ToCss` 理解成「能把 Typst 类型写成 CSS 字符串、可能失败并报告警告」的能力。

#### 4.3.2 核心流程

`PropertiesBuilder` 内部持有两样东西：一个警告接收器 `sink`（实现了 `WarningSink`），以及一个正在构建的 `Properties`。

它的 `push` 流程：

```text
push(name, value: impl ToCss):
    1. 新建一个 CssWriter，把 sink 传进去
    2. writer.emit(value)  → 把 value 序列化进 writer.buf
       （若 value 无法表达为 CSS，writer 会把 error 标记为 true，并向 sink 发警告）
    3. 若没有出错（!writer.error）→ 把 name 和序列化结果 push 进 Properties
       若出错 → 跳过这条属性（不加入列表）
```

这样，哪怕某个颜色/渐变无法转成 CSS，导出仍能继续，用户只是收到一条「xxx was ignored during HTML export」的警告，且对应属性被默默丢弃。

#### 4.3.3 源码精读

`PropertiesBuilder` 的定义与构造：

[crates/typst-html/src/css/encode.rs:76-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L76-L91) —— 字段是 `sink: S` 和 `props: Properties`；`new(sink)` 初始化为空列表。注意它要求泛型 `S: WarningSink`（见 [crates/typst-html/src/css/encode.rs:86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L86)），这正是「序列化失败可发警告」的入口。

`Properties::build` 是创建 builder 的便捷入口：

[crates/typst-html/src/css/encode.rs:26-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L26-L29) —— `build<S: WarningSink>(sink: S)` 返回一个绑定了该 `sink` 的 builder。

核心的序列化 `push`：

[crates/typst-html/src/css/encode.rs:93-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L93-L101) —— 创建 `CssWriter`，调用 `writer.emit(value)`；只有 `!writer.error` 时才把结果 `push` 进 `props`。

这里的 `writer.error` 标志由 `CssWriter::fail` 设置（见 [crates/typst-html/src/css/encode.rs:172-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L172-L176)），`fail` 会同时向 `sink` 发一条「<what> was ignored during HTML export」的警告。比如 `Paint::emit` 遇到渐变时就会 `w.fail("gradient")`（见 [crates/typst-html/src/css/encode.rs:386-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L386-L394)），于是渐变会被跳过并产生警告。

builder 风格的 `with` 与收尾的 `finish`：

[crates/typst-html/src/css/encode.rs:103-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L103-L113) —— `with` 消费 self 并链式 `push`；`finish(self) -> Properties` 把内部列表交出去并完成警告传播。

最后，理解 `ToCss` trait 的形状即可（完整实现见 u4-l4）：

[crates/typst-html/src/css/encode.rs:300-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L300-L311) —— `ToCss` 提供两个方法：`emit(&self, w: &mut CssWriter)` 写入 writer；便捷方法 `to_css` 用一个临时 writer 直接得到字符串。

#### 4.3.4 代码实践

**实践目标**：通过阅读代码，理解「序列化失败 → 发警告 → 跳过属性」的降级路径。

**操作步骤**：

1. 打开 [crates/typst-html/src/css/encode.rs:386-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L386-L394)，阅读 `impl ToCss for Paint`。
2. 追踪当传入一个渐变（`Paint::Gradient`）时：`w.fail("gradient")` 做了什么（跳转到 [crates/typst-html/src/css/encode.rs:167-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L167-L176)）。
3. 再回到 builder 的 `push`（[crates/typst-html/src/css/encode.rs:93-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L93-L101)），确认 `writer.error == true` 时这条属性不会进入列表。

**需要观察的现象**：`fail` 如何同时完成「设置 error 标志」与「向 sink 发警告」两件事。

**预期结果**：一个无法转成 CSS 的渐变，会让对应的 CSS 属性被静默跳过，并向用户发出一条「gradient was ignored during HTML export」警告，导出本身不会失败。

#### 4.3.5 小练习与答案

**练习 1**：`Properties::push`（4.2 节）和 `PropertiesBuilder::push`（本节）接收的值类型有什么不同？分别适合什么场景？

**参考答案**：前者接收 `impl Into<EcoString>`（已是字符串），适合值本身就是简单字面量（如 `"block"`）的场景；后者接收 `impl ToCss`（Typst 强类型），适合需要把长度/颜色等复杂类型序列化、且需要在失败时发警告降级的场景。

**练习 2**：为什么 builder 要持有 `sink: S`，而不是序列化失败时直接 `panic`？

**参考答案**：因为 HTML 导出对无法表达的样式应「尽可能继续」，而不是中断整个编译。`sink` 让 builder 把问题以警告形式报告给用户，并跳过该属性，从而实现优雅降级。

### 4.4 resolve_inline_styles：把 css 写成 style 属性

#### 4.4.1 概念说明

前面三节都是在「准备数据」：编译器把生成的 CSS 存进每个元素的 `css` 字段。本节讲的 `resolve_inline_styles` 才是真正「落袋」的一步——它遍历整棵 DOM 树，把每个元素非空的 `css` 字段变成 HTML 的 `style` 属性。

这里有一个关键的**合并语义**：如果某个元素已经被用户写了一个 `style` 属性（比如 `html.elem("div", style: "color: red")`），那么解析时不会丢弃用户的值，而是把编译器生成的 CSS 和用户原有的 `style` **拼接**在一起。

而且拼接有**固定顺序**：

- **编译器生成的 CSS 在前**；
- **用户原有的 `style` 在后**，中间用 `"; "` 分隔。

这个顺序不是随便定的。由于 CSS 对同一属性「后写覆盖先写」，把用户的 `style` 放在后面，意味着**当两者冲突时用户的样式优先**——这符合用户预期。

`resolve.rs` 的文档注释还提到一个前瞻性信息：本步骤「将来会被更高级的 CSS 处理取代」（will be supplanted by more advanced CSS handling），所以代码现在就按独立的一遍遍历来组织，为将来留出替换空间。

#### 4.4.2 核心流程

入口 `resolve_inline_styles(root)` 从根元素开始，调用 `visit_elem` 递归处理。对每个元素：

```text
visit_elem(elem):
    若 elem.css 非空：
        generated = elem.css.to_inline()        # 编译器 CSS → 字符串
        若 elem.attrs 里已有 style 属性：
            若用户 style 非空：generated += "; "
            generated += 用户 style              # 用户 style 追加在后
            把 generated 写回 style 属性
        否则：
            新增一个 style 属性，值为 generated
    递归：对每个类型为 Element 的孩子，调用 visit_elem
```

要点：

1. **判定靠 `css.is_empty()`**：没有编译器 CSS 的元素（绝大多数普通元素）会被完全跳过，零开销。
2. **合并靠属性名 `style`**：用户写的 `style` 在普通属性表里，属性名常量是 `attr::style`。
3. **递归只对 `Element` 孩子**：`HtmlNode` 还有 `Tag`（内省元数据，不出 HTML）、`Text`、`Frame` 等变体（见 [u2-l1](u2-l1-dom-data-model.md)），其中只有 `Element` 才可能有 `css` 字段需要递归处理。

#### 4.4.3 源码精读

入口函数非常薄：

[crates/typst-html/src/css/resolve.rs:11-13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/resolve.rs#L11-L13) —— `resolve_inline_styles(root: &mut HtmlElement)` 直接转调 `visit_elem(root)`。文档注释解释了为什么单独开一遍遍历：为将来更高级的 CSS 处理预留结构。

核心的合并逻辑在 `visit_elem`：

[crates/typst-html/src/css/resolve.rs:15-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/resolve.rs#L15-L38) —— 逐行拆解：

- 第 16 行 `if !elem.css.is_empty()`：只有编译器真的生成了 CSS 才进入处理，否则跳过。
- 第 19 行 `eco_format!("{}", elem.css.to_inline())`：调用 4.2 节的 `to_inline()` 把 `Properties` 序列化成字符串（如 `"white-space: pre-wrap"`）。这里用 `eco_format!` 而不是 `to_css`/`to_string`，是因为 `to_inline()` 返回的是 `impl Display`。
- 第 20 行 `elem.attrs.get_mut(attr::style)`：在用户的普通属性表里查找名为 `style` 的属性。`attr::style` 是预定义常量：

  [crates/typst-html/src/attr.rs:185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs#L185) —— `pub const style: HtmlAttr = HtmlAttr::constant("style");`

- 第 21-23 行：若用户已有非空的 `style`，先在 `generated` 末尾补一个 `"; "` 分隔符；若用户的 `style` 是空串则不补（避免开头出现多余分隔符）。
- 第 26 行 `generated.push_str(style)`：把用户原有的 `style` 内容追加到**编译器 CSS 之后**。
- 第 27 行 `*style = generated`：把合并后的字符串写回 `style` 属性。
- 第 28-30 行 `else` 分支：元素本来没有 `style` 属性，则用 `elem.attrs.push(attr::style, generated)` 新增一个。
- 第 33-37 行：递归处理孩子。`elem.children.make_mut()` 会触发 EcoVec 的写时复制——如果这棵子树被多处共享，先克隆一份再修改，保证不破坏其他引用。循环里只对 `HtmlNode::Element(elem)` 递归。

注意一个细节：合并发生在**普通属性层**（`attrs`），合并完成后 `css` 字段本身并没有清空——只是输出阶段会去读 `attrs.style`。这与 `css` 字段「仅用于编译器生成样式」的定位一致。

为了看清「`css` 字段是怎么被填上的」，可以回看两个写入点：

[crates/typst-html/src/convert.rs:499-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L499-L510) —— `set_display` 把 `display` 属性 `push` 进 `element.css`（或 `frame.css`），`None` 时 `remove`。

[crates/typst-html/src/dom.rs:237-240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L237-L240) —— `with_css` 是 builder 方法，整体替换 `css` 字段；`pre_wrap` 和行内 `<span>` 都是通过它设置样式的。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：跟踪一个带 `white-space: pre-wrap` 的元素，说清它的 `css` 字段如何在 `resolve_inline_styles` 中被写成 `style` 属性，并与用户已有的 `style` 合并。

**操作步骤**：

1. **触发 pre-wrap**。准备 `demo.typ`：

   ```typst
   #set page(width: auto)
   文本A	  文本B
   ```

   编译：

   ```bash
   typst compile --format html demo.typ demo.html
   ```

2. **观察输出**。在 `demo.html` 里找到形如 `<span style="white-space: pre-wrap">…</span>` 的片段。这个 `<span>` 是 `convert.rs` 的 `pre_wrap` 函数生成的（见 [4.2.3](#423-源码精读) 的源码链接）。

3. **跟踪写入路径**。对照源码还原这段 `style` 的来历：
   - `pre_wrap`（[convert.rs:675-683](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L675-L683)）把 `Properties::new().with("white-space", "pre-wrap")` 写进该 `<span>` 的 `css` 字段。
   - 主链路最后调用 `resolve_inline_styles`（[document.rs:188-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L188-L190)）。
   - `visit_elem`（[resolve.rs:15-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/resolve.rs#L15-L38)）发现该 `<span>` 的 `css` 非空，调用 `to_inline()` 得到 `"white-space: pre-wrap"`；又发现该 `<span>` 没有用户 `style` 属性，于是走 `else` 分支 `push` 一个新的 `style` 属性。

4. **理解合并语义**。设想（或手工构造）一个**同时**有编译器 CSS 和用户 `style` 的元素：若某元素 `css = ["white-space": "pre-wrap"]`、且用户已写 `style="color: red"`，则 `visit_elem` 会按「编译器在前、用户在后」拼成：

   ```text
   white-space: pre-wrap; color: red
   ```

   因为 `color: red` 在后，若两者涉及同一属性，用户值生效。

**需要观察的现象**：第 2 步里 `pre-wrap` 出现在 `style` 属性中；第 4 步里合并后编译器 CSS 排在用户 `style` 之前。

**预期结果**：编译器生成的 `white-space: pre-wrap` 经 `to_inline()` 变成字符串，再由 `resolve_inline_styles` 写入 `style` 属性；若元素已有用户 `style`，则编译器 CSS 拼在前、用户值拼在后并用 `"; "` 分隔。

**待本地验证**：第 2 步中触发 `pre-wrap` 的具体字符与最终 HTML 片段形态，请以本地实际编译输出为准；第 4 步的「同时具备两类样式」场景在普通 Typst 输入下不易直接触发，建议作为源码阅读型推理理解。

#### 4.4.5 小练习与答案

**练习 1**：当一个元素既有编译器生成的 `white-space: pre-wrap`，用户又写了 `style="white-space: normal"`，最终 `style` 属性的值是什么？浏览器实际生效的是哪一个？

**参考答案**：合并后值为 `"white-space: pre-wrap; white-space: normal"`。由于用户的 `white-space: normal` 写在后面，浏览器实际生效的是 `normal`（后写覆盖先写）。这正是把用户 `style` 放在拼接末尾的设计意图。

**练习 2**：`visit_elem` 的递归循环里，`elem.children.make_mut()` 的 `make_mut()` 起什么作用？为什么需要它？

**参考答案**：`children` 是 `EcoVec`（写时复制容器）。`make_mut()` 在需要可变借用时，若该数组被多处共享则先克隆一份，保证修改不影响其他引用者。递归修改孩子元素必须拿到可变引用，所以要先 `make_mut()`。

**练习 3**：为什么递归只对 `HtmlNode::Element` 进行，而不处理 `Text`、`Frame`、`Tag` 等孩子？

**参考答案**：因为 `visit_elem` 的职责是「把元素的 `css` 字段合并进它的 `style` 属性」，而这一合并只对 `HtmlElement` 有意义——`HtmlElement` 才有 `attrs`（普通属性表）可以去写 `style`。`Text` 是纯文本、没有属性表；`Tag` 是不出现在 HTML 输出里的内省元数据；`Frame` 虽然也有 `css` 字段（见 `set_display` 对 frame 的设置），但它不经过 `visit_elem`，而是在编码阶段由 `write_frame` 直接读取：`frame.css.to_inline()` 的结果作为 `style` 参数传给 `typst_svg::svg_in_html`（见 [crates/typst-html/src/encode.rs:390-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L400)，详见 [u6-l1](u6-l1-html-frame-svg-embedding.md)）。所以 `visit_elem` 的孩子递归只关心 `Element`。

## 5. 综合实践

把本讲的四个模块串起来，完成一次完整的「CSS 生命周期」追踪。

**任务**：写一个会产生编译器 CSS 的 Typst 文档，编译为 HTML，然后对照源码画出「从 Typst 输入到最终 `style` 属性」的完整数据流图。

**建议步骤**：

1. 准备 `demo.typ`，内容包含会触发编译器 CSS 的结构，例如：

   ```typst
   #set page(width: auto)
   = 标题
   含  连续空格的文字
   #box[行内盒子]
   ```

   编译：

   ```bash
   typst compile --format html demo.typ demo.html
   ```

2. 在 `demo.html` 中找出所有带 `style=` 的元素，记录它们的 `style` 值。
3. 对每个 `style` 值，判断它的来源：
   - 是 `pre_wrap` 产生的 `white-space: pre-wrap`？
   - 是 `set_display` 产生的 `display: ...`？
   - 是行内 `<span>` 的 `display: inline-block`（见 [convert.rs:360-366](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L360-L366)）？
4. 画一张数据流图，标注每条 CSS 经过的关键函数：
   `with_css`/`set_display` → `Properties.push`（二分去重）→ `resolve_inline_styles` → `visit_elem` → `to_inline()` → 写入 `attrs.style` → `encode` 输出。

**交付物**：一张数据流图 + 一段说明，解释为什么这些 `style` 值的属性顺序与你输入的顺序无关（提示：`Properties` 的有序去重）。

**待本地验证**：具体的 `style` 值和触发条件，以本地编译输出为准。

## 6. 本讲小结

- `src/css/` 是 typst-html 唯一的目录型子模块，由 `mod.rs` 做门面，重导出 `Properties`、`ToCss`、`resolve_inline_styles`，`encode` 与 `resolve` 为私有子模块。
- 编译器生成的 CSS 与用户写的 `style` 一开始是分开的：前者存于 `HtmlElement.css`（一个 `Properties`），后者是普通属性 `attrs` 里的 `style`。
- `Properties` 是「按属性名排序、自动去重」的列表，靠 `push` 的二分查找实现「命中则覆盖、未命中则插入」，保证输出确定、可哈希。
- `PropertiesBuilder` 配合 `ToCss`/`CssWriter` 把 Typst 强类型值序列化成 CSS，序列化失败时通过 `WarningSink` 发警告并跳过该属性，实现优雅降级。
- `to_inline()` 把 `Properties` 拼成 `name: value; name: value` 形式的字符串。
- `resolve_inline_styles` 在 `finalize_dom` 之后遍历 DOM，把每个元素的 `css` 字段写成 `style` 属性；与用户既有 `style` 合并时，编译器 CSS 在前、用户值在后（后写覆盖先写，用户优先）。

## 7. 下一步学习建议

- 想了解 `ToCss`/`CssWriter` 如何把长度、颜色等具体类型序列化成 CSS（含 `calc()` 生成与 hex/`rgb()` 选择），请阅读 [u4-l4 Typst 类型到 CSS 类型的转换](u4-l4-typst-to-css-conversion.md)。
- 想了解 `css` 字段最主要的两个来源——空白保护与 display 提升——请阅读 [u4-l1 HTML 空白保护机制](u4-l1-whitespace-protection.md) 和 [u4-l2 display 属性与块级/行内提升](u4-l2-display-block-inline-promotion.md)。
- 想了解 `resolve_inline_styles` 在编译主链路中的精确位置，可重温 [u3-l1 文档编译主链路 html_document](u3-l1-document-compilation-pipeline.md)。
- 阅读建议：直接打开 `src/css/resolve.rs`（仅 38 行）通读一遍，再带着本讲的合并语义去对照 `src/css/encode.rs` 中 `Properties` 的实现，印象会非常深刻。
