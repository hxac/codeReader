# SVG 输出抽象层 write.rs

## 1. 本讲目标

本讲拆解 typst-svg 的「输出地基」——[src/write.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs)。它本身不渲染任何具体对象（不画形状、不排文字），而是为整个 crate 提供一套**把内存里的结构翻译成合法 SVG 文本**的工具。

学完后你应当能够：

- 说清 `SvgElem` 如何借用「借用 + `Drop`」的 RAII 模式自动闭合 XML 标签，从而杜绝「开了标签忘记关」的低级错误。
- 说清 `LazySvgElem` 的「按需 `init`」为何能避免生成 `<g></g>` 这样的空元素。
- 区分两个容易混淆的 trait：`SvgWrite` 负责「底层怎么往缓冲区写字节」（含数字精度与 XML 转义），`SvgDisplay` 负责「某个值应当格式化成什么字符串」。
- 读懂 `SvgTransform` / `SvgUrl` / `SvgIdRef` 三个适配器，理解它们如何把 typst 的内部类型「翻译」成 SVG 属性值。

> 承接：上一讲（u2-l2）里 `render_group` 反复出现的 `svg.lazy_elem("g")` 与 `svg.init()`，本讲终于给出它们的定义。

## 2. 前置知识

- **RAII 与 `Drop`**：Rust 中对象离开作用域时编译器会自动调用其 `Drop::drop` 方法。typst-svg 利用这一点，让「XML 元素」的生命周期来对应「标签的开闭」。
- **借用与生命周期**：`SvgElem<'a>` 持有一个 `&'a mut XmlWriter`，嵌套子元素时通过更短的生命周期借用父元素的写入器，从而在编译期保证「子标签一定先于父标签关闭」。
- **SVG 基本语法**：熟悉 `<g transform="...">`、`<rect width="..." height="..."/>`、`url(#id)` 引用、`transform="matrix(a,b,c,d,e,f)"` 等基本写法。
- **trait**：理解 Rust trait 如何对「同一组行为」做抽象。本讲有两个核心 trait `SvgWrite` 与 `SvgDisplay`。

## 3. 本讲源码地图

本讲几乎全部围绕单个文件，并少量回看它在 `lib.rs` 中的使用点：

| 文件 | 作用 |
| --- | --- |
| [src/write.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs) | 输出抽象层：`SvgElem` / `LazySvgElem`（元素）、`SvgWrite` / `SvgFormatter`（底层写入）、`SvgDisplay` 及三个适配器（值格式化）。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs) | 在 `render_page` / `render_group` / `svg_header` 等处使用上述抽象；并在此为 `DedupId` 实现 `SvgDisplay`。 |

依赖关系（谁 `use` 谁）：`lib.rs` 与各渲染文件（`shape.rs` / `text.rs` / `paint.rs` / `image.rs`）都 `use crate::write::{...}`；而 `write.rs` 只反向依赖 `crate::DedupId`（定义在 `lib.rs`）。也就是说，`write.rs` 是一个几乎无依赖的底层工具模块。

> 提醒：`write.rs` 里的类型都不属于公开 API（未被 `pub use` 转出），仅供 crate 内部使用。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：`SvgElem`（RAII 元素）、`LazySvgElem`（延迟创建）、`SvgWrite`/`SvgFormatter`（底层写入）、`SvgDisplay` 及三个适配器（值格式化）。

### 4.1 SvgElem：RAII 元素包装与 Drop 自动闭合

#### 4.1.1 概念说明

typst-svg 最终产物是一段 SVG **字符串**。生成它需要不断「开始标签 → 写属性 → 写子标签 → 结束标签」。最朴素的写法是手动配对调用 `start_element` / `end_element`，但这极易出错：一旦某个分支提前 `return` 或忘了关闭，就会产出非法 SVG。

`SvgElem` 的解决办法是 **RAII（Resource Acquisition Is Initialization）**：把「一个 XML 元素」包成一个 Rust 值，构造时调用 `start_element`，析构（`Drop`）时调用 `end_element`。于是「标签的开闭」被绑死在「变量的作用域」上——变量离开作用域，标签自动关闭，编译器替你保证配对。

#### 4.1.2 核心流程

`SvgElem` 的语义可以浓缩成下面这个模型：

```text
SvgElem::new(xml, "g")      // xml.start_element("g")
  ├─ .attr("transform", v)  // 写属性（委托 SvgDisplay 把 v 格式化成字符串）
  ├─ .attr_with(name, |f| …)// 写属性（手写格式化逻辑，拿到 SvgFormatter）
  ├─ .elem("rect")          // 返回一个子 SvgElem（start_element("rect")）
  │        └─ 子 SvgElem drop ─> end_element("rect")
  └─ .with(|this| { … })    // 在闭包里操作自身后继续链式调用
// 外层 SvgElem drop ─> end_element("g")
```

要点：

1. `SvgElem` 只持有一个 `&mut XmlWriter`，本身不存数据，是「零成本」的借用包装。
2. 嵌套用 `.elem(name)`：它返回一个新的、生命周期更短的 `SvgElem`，子元素必然先 drop。
3. 属性有两种写法：`attr`（值实现 `SvgDisplay`，自动格式化）与 `attr_with`（手写闭包，适合需要拼多个数字的场景，如 `viewBox`）。

#### 4.1.3 源码精读

结构体定义，仅持有一个对写入器的可变借用：

[src/write.rs:L8-L10](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L8-L10) — `SvgElem` 只包了一个 `&'a mut XmlWriter`，构造即开标签。

`new` 在构造时调用 `start_element`：

[src/write.rs:L12-L16](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L12-L16) — 构造函数立即 `xml.start_element(name)`，把「创建对象」与「开标签」绑定。

三个核心方法：

[src/write.rs:L18-L20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L18-L20) — `elem(name)` 创建子元素，返回的 `SvgElem<'_>` 生命周期短于 `&mut self`，编译期保证子标签先关。

[src/write.rs:L28-L42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L28-L42) — `attr` 把值（需实现 `SvgDisplay`）委托给 `attr_with`；`attr_with` 调用 XmlWriter 的 `write_attribute_raw`，并把裸缓冲包成 `SvgFormatter` 交给闭包。

[src/write.rs:L44-L47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L44-L47) — `with` 是个链式辅助方法：在闭包里对元素做事后仍返回 `&mut Self`，便于连续写属性再嵌套子元素。

`Drop` 实现——整套设计的「自动关门」：

[src/write.rs:L50-L54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L50-L54) — 析构时调用 `xml.end_element()`。只要 `SvgElem` 离开作用域（包括 `?` 提前返回、`match` 分支结束），标签就一定被关闭。

`lib.rs` 里的真实用法，展示了「`elem` 嵌套 + `attr` + Drop」三件套如何自然地写出一个带 `transform` 的 `<a><rect/></a>` 链接：

[src/lib.rs:L370-L400](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L370-L400) — `render_link` 中 `let mut a = svg.elem("a");` 创建链接元素，`a.elem("rect").attr(...)` 嵌套透明矩形；函数结束时 `a` 先于外层 `svg` 析构，`<rect>` 与 `<a>` 依次关闭。

#### 4.1.4 代码实践

> 对应本讲核心实践任务：用 `elem` / `attr_with` / `with` 手工模拟生成一个带 `transform` 属性的 `<g>` 元素片段，并解释 `Drop` 如何保证标签闭合。

**实践目标**：建立「`SvgElem` 代码 ↔ 输出 SVG 文本」的直觉，体会 RAII 的自动闭合。

**操作步骤（手工推演）**：阅读下面这段「示例代码」（它模仿 `write.rs` 的真实用法，但为聚焦而简化）：

```rust
// 示例代码：非项目原文件，仅用于演示 SvgElem 的用法模型
fn render_demo(svg: &mut SvgElem) {
    let mut g = svg.elem("g");                 // start_element("g")
    g.attr_with("transform", |attr| {          // 手写属性值
        attr.push_str("translate(");
        attr.push_nums([10.0, 20.0]);
        attr.push_str(")");
    });
    g.with(|g| {                               // 在闭包里继续操作 g
        g.elem("rect")                         // start_element("rect")
            .attr("width", 100.0)              // attr -> SvgDisplay for f64
            .attr("height", 50.0);
        // ↑ 返回的子 SvgElem 在这条语句结束时 drop -> end_element("rect")
    });
    // g 在函数末尾 drop -> end_element("g")
}
```

**需要观察的现象**：跟踪每一次 `start_element` 与（由 `Drop` 触发的）`end_element` 的顺序。

**预期结果（手工推演）**：依据 4.3 节的数字格式化规则（`10.0` 是整数 → 输出 `10`；`push_nums` 用空格分隔），推演出的 SVG 片段为：

```xml
<g transform="translate(10 20)"><rect width="100" height="50"/></g>
```

要点解释：

1. `svg.elem("g")` 开始 `<g>`；它的 `Drop` 负责最后的 `</g>`。
2. `attr_with` 拿到 `SvgFormatter`，把 `translate(10 20)` 写进 `transform` 属性。
3. `g.elem("rect")` 开始 `<rect>`；该子元素在同一语句末尾 drop，立即关闭。xmlwriter 会把无内容的元素写成自闭合 `<rect .../>`。
4. 函数返回时，外层 `g` drop，写入 `</g>`。
5. 关键：即使 `with` 的闭包里再 `return` 或 `?`，`<rect>` 与 `<g>` 的关闭仍由 `Drop` 兜底——这正是 RAII 的价值。

> 「待本地验证」：上面 XML 的确切空白、引号样式、自闭合写法取决于 `xmlwriter` 的实现与 `Options`（见 `lib.rs` 的 `xml_options`，默认 `use_single_quote: false`、`Indent::None`）；标签内容与属性值本身由 `write.rs` 决定，是确定的。

**可选的「亲手感受 Drop」**（运行型）：因为 `SvgElem` 不是公开 API，外部无法直接构造；但你可以在一个独立小项目里直接用其底层依赖 `xmlwriter` 复刻同一机制（`cargo add xmlwriter`）：

```rust
// 示例代码：用底层 xmlwriter 模拟 SvgElem 的开/关标签
use xmlwriter::{Indent, Options, XmlWriter};

fn main() {
    let mut xml = XmlWriter::new(Options {
        use_single_quote: false,
        indent: Indent::None,
        attributes_indent: Indent::None,
    });
    xml.start_element("g");
    xml.write_attribute_raw("transform", |buf| buf.extend_from_slice(b"translate(10 20)"));
    xml.start_element("rect");
    xml.write_attribute_raw("width", |buf| buf.extend_from_slice(b"100"));
    xml.write_attribute_raw("height", |buf| buf.extend_from_slice(b"50"));
    xml.end_element(); // </rect>
    xml.end_element(); // </g>
    println!("{}", xml.end_document());
}
```

把这段代码与上面的 `SvgElem` 版本对比：`SvgElem` 用 `Drop` 自动完成了这里必须手动写、且容易漏写的两次 `end_element()`。确切输出字节「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 `render_demo` 里 `g.elem("rect")` 的返回值绑定到一个变量 `let r = g.elem("rect");`，并在 `with` 闭包结束时仍未让 `r` drop，会发生什么？`<rect>` 还能被正确关闭吗？

> **答案**：只要 `r` 仍然存活，`<rect>` 就**不会**关闭；只有在 `r` 之后 drop（通常在 `with` 闭包或函数结束时）才会写入 `</rect>`。这体现了「关闭时机 = 变量作用域」。若 `r` 的存活期长于外层 `g`，借用检查器会直接报错（子借用不能比父借用活得久），从而在编译期阻止「子标签比父标签晚关」的非法结构。

**练习 2**：`SvgElem` 为什么只持有 `&mut XmlWriter` 而不拥有一个 `XmlWriter`？

> **答案**：整个导出过程只有一个 `XmlWriter`（在 `lib.rs` 的导出函数里创建）。`SvgElem` 只是要在它上面「开窗」写入一个元素，不需要所有权；用可变借用既能写入，又能让多个嵌套元素共享同一个底层缓冲，零拷贝、零分配。

---

### 4.2 LazySvgElem：按需 init，避免空元素

#### 4.2.1 概念说明

`SvgElem` 有一个「缺点」：构造即开标签。但渲染中常常**事前不知道某个分组是否真的需要子内容**。例如 `render_group` 里，软 frame（Soft）只是把变换吸收进父级、根本不该产生任何 `<g>`；只有硬 frame（Hard）或有裁剪、有 label 时才需要真正输出 `<g>`。

如果用普通 `SvgElem`，无论是否需要都会生成 `<g></g>` 空标签，既浪费体积也不优雅。`LazySvgElem` 解决的就是「**先把元素挂起来，等确定要写内容时再真正创建**」。

#### 4.2.2 核心流程

```text
svg.lazy_elem("g")     // 暂不开标签，initialized = false
  ├─ .init()           // 首次调用：start_element("g")，initialized = true
  │     （之后所有 .init() 都是幂等的，不会重复开标签）
  ├─ .lazy()           // 不强制初始化，直接拿父元素（用于「可能不需要 g」时的回退）
  └─ drop              // 仅当 initialized == true 才 end_element("g")
```

两个关键方法：

- `init()`：**首次**调用时 `start_element`，之后幂等；始终返回内部 `SvgElem`，供你继续写属性/子元素。
- `lazy()`：不触发初始化，直接返回内部 `SvgElem`。当你「这个 `<g>` 可能根本不需要、把内容直接挂到父级也行」时用它。

#### 4.2.3 源码精读

结构体多了一个 `initialized: bool` 标志：

[src/write.rs:L57-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L57-L61) — 持有父元素引用、是否已初始化、待创建的元素名。

`init` 的「懒」就体现在 `if !self.initialized` 判断上：

[src/write.rs:L68-L75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L68-L75) — 只有第一次调用才真正 `start_element`，因此后续多次 `svg.init().attr(...)` 不会重复开标签。

[src/write.rs:L77-L81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L77-L81) — `lazy()` 直接返回内部元素而不初始化，用于「跳过本层 `<g>`、把子内容直接接到父级」。

`Drop` 同样是「有条件关闭」：

[src/write.rs:L84-L90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L84-L90) — 只有真正 `init` 过（`initialized == true`）才 `end_element`，否则什么都不输出，完美避免空 `<g></g>`。

`lib.rs` 里 `render_group` 是它的典型消费者——软 frame 路径只调用 `svg.lazy()`（不 init），硬 frame / 有 label / 有 clip 才 `svg.init()`：

[src/lib.rs:L328-L360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L328-L360) — 软 frame 分支只 `state.pre_concat(...)` 后用 `svg.lazy()` 透传；硬 frame 才 `svg.init()` 写 `transform`，有 clip 时再 `svg.init().attr("clip-path", ...)`。多次 `init()` 幂等，所以同一段代码里放心重复调用。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：体会 `LazySvgElem` 如何在「不确定要不要 `<g>`」时既不丢失能力、又不制造垃圾标签。

**操作步骤**：

1. 打开 [src/lib.rs:L328-L360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L328-L360) 的 `render_group`。
2. 假设一个软 frame 组：无 label、无 clip、变换为 `T`、内含一个 `<rect>`。逐行走代码：它走了 `FrameKind::Soft` 分支，**没有**调用 `init()`，最后 `self.render_frame(svg.lazy(), ...)`。
3. 再假设一个硬 frame 组（有 clip）：它会先 `svg.init()`（写 `transform`，若有），再 `svg.init().attr("clip-path", ...)`。

**需要观察的现象**：两条路径下，`<g>` 标签「开/不开」「关/不关」的差异。

**预期结果**：

- 软 frame 路径：`LazySvgElem` 全程 `initialized == false`，`Drop` 不输出任何字符——最终 SVG 里**没有多余的 `<g>`**，`<rect>` 直接挂在父级，变换被吸收进父级 transform。
- 硬 frame 路径：第一次 `init()` 开 `<g>`，后续 `init()` 幂等不再开；`Drop` 时输出 `</g>`。

> 结论：`LazySvgElem` = 「先把位置占住，按需转正」，转正前对输出零影响。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init()` 要设计成「幂等」？如果它每次调用都重新 `start_element`，会出什么问题？

> **答案**：因为调用方往往在多个独立 `if` 分支里各自调用 `svg.init().attr(...)`（先判 `transform`、再判 `label`、再判 `clip`），彼此无法沟通「前面是否已经开过标签」。幂等保证了「无论调几次，`<g>` 只开一次」。若不幂等，就会产出 `<g><g><g>...</g></g></g>` 这样的嵌套空标签。

**练习 2**：`lazy()` 与 `init()` 返回的都是 `&mut SvgElem`，它们对输出的影响区别在哪？

> **答案**：`init()` 会在首次调用时**真正创建** `<g>`（开标签、`Drop` 时关标签）；`lazy()` **永不创建**本层 `<g>`，只是把控制权交还给父元素，后续写入直接落到父级。前者「我决定要这一层」，后者「这一层可有可无，跳过它」。

---

### 4.3 SvgWrite 与 SvgFormatter：底层写入、数字格式化、XML 转义

#### 4.3.1 概念说明

`SvgElem` 解决「标签结构」，但属性值里那些数字、字符串具体怎么变成字节？这就是 `SvgWrite` 的工作。它是一个面向「写入目标」的 trait，定义了往缓冲区写字符串、字符、数字、整数等基本操作，并内置两类关键处理：

- **数字精度控制**：把 `f64` 舍入到固定精度（减小体积、提升确定性），整数走更快路径且不带 `.0`。
- **XML 转义**：属性值里的 `&`、`<` 必须转义成 `&amp;`、`&lt;`，否则含特殊字符的链接 URL 会让 SVG 非法。

`SvgFormatter` 是 `SvgWrite` 的具体实现之一，它包着一个 `&mut Vec<u8>` 或 `&mut EcoString`，专门用于在 `attr_with` 的闭包里拼装属性值。

#### 4.3.2 核心流程

```text
attr_with("viewBox", |attr: &mut SvgFormatter| {
    attr.push_nums([0.0, 0.0, w, h]);   // 调用 SvgWrite::push_nums
})
   └─ push_nums → 逐个 push_num
        push_num(num):
          num = round(num * 1e9) / 1e9     // 精度统一
          if num 是整数: push_int           // 走 itoa，无 ".0"
          else:          ryu 格式化         // 最短浮点表示
   └─ SvgFormatter::push_str(s) → escape_str(s, |chunk| buf.push)
        escape_str: 扫描 & 和 <，分段写出，命中的字符替换为实体
```

数字舍入的精度由常量决定：

\[ \text{ROUNDING\_FACTOR} = 10^{9}, \qquad \text{out} = \frac{\operatorname{round}(\text{num} \times 10^{9})}{10^{9}} \]

即保留到小数点后 9 位，足以覆盖 SVG 视觉精度，同时把 `0.1 + 0.2 = 0.30000000000000004` 这类浮点噪声归一成 `0.3`。

#### 4.3.3 源码精读

`SvgWrite` trait 的方法集合：

[src/write.rs:L92-L142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L92-L142) — 定义 `push_str`（抽象）、`push_char`、`push_num`、`push_int`、`push_nums`、`push`。其中 `push_num`/`push_int`/`push_nums` 都有默认实现，子类型只需实现 `push_str`。

`push_num` 的精度与整数快路径：

[src/write.rs:L104-L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L104-L120) — 先乘 \(10^9\) 舍入再除回；若结果等于某整数则用 `itoa`（快且无 `.0`），否则用 `ryu`（最短浮点）。

[src/write.rs:L123-L126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L123-L126) — `push_int` 用 `itoa` 格式化整数为字符串，再交 `push_str`。

[src/write.rs:L129-L136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L129-L136) — `push_nums` 把多个数字用单空格分隔输出（SVG 里 `viewBox`、`matrix(...)`、坐标序列都靠它）。

`SvgFormatter` 及其两个 `SvgWrite` 实现：

[src/write.rs:L144-L164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L144-L164) — `SvgFormatter` 持有缓冲；为 `Vec<u8>` 与 `EcoString` 两种缓冲各实现一份 `push_str`，写入前都先经 `escape_str` 转义。

转义逻辑（只转义 `&`、`<`，因为 `xmlwriter` 的 raw 写入器只处理了引号）：

[src/write.rs:L170-L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L170-L185) — `escape_str` 用「找到下一个需转义字节 → 先整段拷贝未转义部分 → 再写字符实体」的方式，尽量大块拷贝以减少调用次数。

[src/write.rs:L188-L211](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L188-L211) — `EscapedChar` 枚举只有 `Amp`/`Lt` 两种，分别映射 `&amp;`/`&lt;`。

`lib.rs` 中 `svg_header_with_custom_attrs` 是 `attr_with` + `push_nums`/`push_num` 的真实用例：

[src/lib.rs:L456-L466](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L456-L466) — `viewBox` 用 `attr_with` + `push_nums([0,0,w,h])`；`width`/`height` 用 `push_num` 写数字后再 `push_str("pt")` 拼单位。

#### 4.3.4 代码实践（推演型）

**实践目标**：能准确预测 `push_num` 对不同输入的输出。

**操作步骤**：对以下输入手工套用 [src/write.rs:L104-L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L104-L120) 的逻辑，写出 `push_num` 的输出字符串。

| 输入 `num` | 舍入后 | 是否整数 | 输出 |
| --- | --- | --- | --- |
| `100.0` | `100.0` | 是 | `100` |
| `50.5` | `50.5` | 否 | `50.5` |
| `0.30000000000000004`（`0.1+0.2`） | `0.3` | 否 | `0.3` |
| `1.0000000001` | `1.0`（被舍到 9 位） | 是 | `1` |

**需要观察的现象**：整数路径省略 `.0`；浮点噪声被舍入消除。

**预期结果**：见上表最右列。再推演 `push_nums([0.0, 0.0, 595.0, 842.0])`（一个 A4 页面的 `viewBox`）→ 输出 `0 0 595 842`。

> 结论：整数优先、浮点最短、坐标空格分隔——这套规则让 SVG 里的数字既精确又紧凑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `escape_str` 只转义 `&` 和 `<`，不转义 `>`、`"`、`'`？

> **答案**：注释（[L166-L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L166-L169)）写明：`xmlwriter` 的 raw attribute writer 已经处理了引号转义；在「属性值」语境下，`>` 单独出现并不违法，`&` 和 `<` 才是会启动实体/标签解析的危险字符。所以 `SvgFormatter` 只补齐 `xmlwriter` 没覆盖的那两个，避免重复转义。

**练习 2**：`push_num` 为什么要在格式化前先做 `round(num * 1e9) / 1e9`？

> **答案**：两个目的——(1) **确定性**：消除同一逻辑坐标因不同计算路径产生的微小浮点差异，让两次导出同一文档得到字节级稳定的输出；(2) **体积**：把 `0.30000000000000004` 这种长尾压成 `0.3`，显著减小文件体积。

---

### 4.4 SvgDisplay：值格式化与三个适配器

#### 4.4.1 概念说明

如果说 `SvgWrite` 是「怎么写字节」，那 `SvgDisplay` 就是「**某个类型的值应当被写成什么样的字符串**」。它只要求实现一个方法 `fmt(&self, f: &mut impl SvgWrite)`。任何能被塞进 `attr(name, value)` 的 `value` 都必须实现 `SvgDisplay`。

typst-svg 还提供三个「**适配器（newtype）**」：`SvgTransform`、`SvgUrl`、`SvgIdRef`。它们的作用是给一个已有类型「**临时套上一层 SVG 语义**」，避免污染原类型。例如 `Transform` 是个数学矩阵，它本身不该知道「SVG 里 `transform` 属性长什么样」；于是用 `SvgTransform(Transform)` 包一下，专门为这层包装实现 `SvgDisplay`。

#### 4.4.2 核心流程

```text
svg.attr("transform", SvgTransform(ts))
        │  value: impl SvgDisplay
        └─> SvgTransform::fmt(f)
              ├─ 仅缩放   → "scale(sx[, sy])"
              ├─ 仅平移   → "translate(tx[, ty])"
              └─ 一般情况 → "matrix(sx,ky,kx,sy,tx,ty)"   // 注意列优先顺序

svg.attr("clip-path", SvgUrl(id))   →  "url(#G1A2B...)"
svg.attr("href", SvgIdRef(name))    →  "#anchor-name"
```

`SvgTransform` 的矩阵输出顺序是 SVG 约定的列优先：SVG 的 `matrix(a,b,c,d,e,f)` 表示

\[
\begin{bmatrix} a & c & e \\ b & d & f \\ 0 & 0 & 1 \end{bmatrix},
\]

对应 typst `Transform{sx, ky, kx, sy, tx, ty}`，因此代码按 `[sx, ky, kx, sy, tx, ty]` 输出。

#### 4.4.3 源码精读

`SvgDisplay` trait 与几个基础实现：

[src/write.rs:L213-L251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L213-L251) — 定义 `fmt`，并为 `&T`、`char`、`&str`、`EcoString`、`ResolvedPicoStr`、`f64` 提供实现，分别委托到 `push_char`/`push_str`/`push_num`。

`SvgTransform`——按变换结构选择最短写法：

[src/write.rs:L257-L290](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L257-L290) — 先取 6 个分量的 pt 值；`is_only_scale()` 走 `scale(...)`（`sx==sy` 时只写一个数），`is_only_translate()` 走 `translate(...)`（`ty==0` 时只写 `tx`），否则回退到 `matrix(...)`。这是一种「**能用短形式就不用 matrix**」的体积优化。

`SvgUrl`——把 `DedupId` 包成 `url(#...)` 引用：

[src/write.rs:L293-L301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L293-L301) — 输出 `url(#` + id + `)`，用于 `fill="url(#g...)"`、`clip-path="url(#c...)"` 这类对 `<defs>` 资源的引用。

`SvgIdRef`——两个实现，分别处理「DedupId」与「普通字符串」：

[src/write.rs:L304-L318](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L304-L318) — 对 `DedupId` 输出 `#` + id；对任意 `AsRef<str>` 输出 `#` + 字符串，用于 `href="#anchor"`、`xlink:href="#..."` 等。

`DedupId` 自身的 `SvgDisplay`（在 `lib.rs`）：把 `u128` 哈希编成大写十六进制、再去掉前导零：

[src/lib.rs:L529-L551](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L529-L551) — 先 `push_char(kind)`（命名空间字符），再把 16 字节哈希逐字节转两位大写十六进制，最后 `trim_start_matches('0')` 去前导零。这就是 `SvgUrl`/`SvgIdRef` 里 id 的实际长相。

#### 4.4.4 代码实践（推演型）

**实践目标**：能根据 `Transform` 的取值预测 `SvgTransform` 的输出形式。

**操作步骤**：对下列 `Transform`（记号 `T{sx,sy,kx,ky,tx,ty}`，单位 pt）套用 [src/write.rs:L259-L289](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L259-L289)，写出输出。

| 变换 | 走哪条分支 | 输出 |
| --- | --- | --- |
| `T{2,2,0,0,0,0}`（纯等比缩放） | `is_only_scale`，`sx==sy` | `scale(2)` |
| `T{2,3,0,0,0,0}`（纯非等比缩放） | `is_only_scale`，`sx!=sy` | `scale(2 3)` |
| `T{1,1,0,0,10,0}`（纯水平平移） | `is_only_translate`，`ty==0` | `translate(10)` |
| `T{1,1,0,0,10,20}`（纯二维平移） | `is_only_translate`，`ty!=0` | `translate(10 20)` |
| `T{1,1,0.5,1,0,0}`（含错切） | 都不满足 | `matrix(1 1 0.5 1 0 0)` |

**需要观察的现象**：只要能写成短的 `scale`/`translate`，就绝不用更长的 `matrix`；同一变换有多种合法写法时，typst-svg 总是挑最短的那种。

**预期结果**：见上表。再推演 `SvgUrl(DedupId('g', 0x0000...0001))`（假设哈希最低位为 1、其余为 0）→ `url(#g1)`（`g` 是 kind 字符，`1` 是去掉前导零后的大写十六进制）。

> 注：真实 `DedupId` 的哈希来自 `typst_utils::hash128`，几乎不会这么小，这里仅为演示 `trim_start_matches('0')` 的效果。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接给 `Transform` 实现 `SvgDisplay`，而要套一个 `SvgTransform` newtype？

> **答案**：分离关注点。`Transform` 是 typst 排版层的通用数学类型，被很多 crate 共用，它不该「知道」SVG 的属性语法。用 newtype `SvgTransform` 把「SVG 展示语义」局部化在 typst-svg 内部，既不污染公共类型，又能为同一类型提供多种展示形式（如果将来需要）。

**练习 2**：`SvgUrl(DedupId(...))` 与 `SvgIdRef(DedupId(...))` 输出只差一个 `url(` 包裹，为什么要分成两个类型？

> **答案**：它们对应 SVG 里两种不同的引用语法——`fill`/`clip-path` 等属性需要 `url(#id)` 函数式引用，而 `href`/`xlink:href` 只需要裸 `#id` 片段。分成两个 newtype 让调用处用类型名直接表达语义（`SvgUrl` = 「引用一个定义资源」，`SvgIdRef` = 「指向一个锚点 id」），避免靠人记忆「这里到底要不要包 `url()`」。

---

## 5. 综合实践

把本讲四个最小模块串起来：手工「执行」一遍 `render_group` 对一个**带裁剪的硬 frame 组**的处理，写出它最终生成的 SVG 片段。

**设定**：一个硬 frame 组，其累积变换为「平移 (10, 20) pt」、带一个 clip path（去重后得到 `DedupId('c', H)`，其十六进制去前导零后形如 `C4F2...`），组内只含一个 `width=100, height=50` 的 `<rect>`。

**任务**：

1. 画出 `render_group` 中各调用与 `LazySvgElem` 状态（`initialized`）的变化时间线。
2. 标出每一次 `start_element` / `end_element`（含由 `Drop` 触发的）出现的时机。
3. 写出最终 SVG 片段。

**参考答案**：

1. `svg.lazy_elem("g")`（`initialized=false`）→ 硬 frame 分支 `svg.init()`（开 `<g>`，`initialized=true`）→ `transform` 非单位矩阵，`svg.init().attr("transform", SvgTransform(...))`（`init` 幂等，仅写属性）→ 有 clip：`svg.init().attr("clip-path", SvgUrl(id))`（写 `url(#C4F2...)`）→ `render_frame` 内部 `svg.elem("rect").attr("width",100).attr("height",50)`（开 `<rect>`，语句末尾 drop 关闭）→ 函数返回，`LazySvgElem` drop（`initialized=true` 故 `end_element`）。
2. `start_element` 三次：`<g>`（由 `init`）、`<rect>`（由 `elem`）；其中 `<g>` 只开一次（幂等）。`end_element` 两次：`</rect>`（子 `SvgElem` drop）、`</g>`（`LazySvgElem` drop）。
3. 最终片段（手推）：

```xml
<g transform="translate(10 20)" clip-path="url(#C4F2...)"><rect width="100" height="50"/></g>
```

> 这一条路径就同时用到了 `LazySvgElem`（按需开 `<g>`）、`SvgElem`+`Drop`（自动关 `<rect>`）、`attr`+`SvgDisplay`（`SvgTransform`/`SvgUrl`）、以及 `push_num` 的整数快路径（`10`/`20`/`100`/`50`）。

## 6. 本讲小结

- `SvgElem` 是基于 `&mut XmlWriter` 的 RAII 包装：构造开标签、`Drop` 关标签，把「标签配对」变成编译期保证。
- `LazySvgElem` 用 `initialized` 标志实现「按需 `init`」，在不确定是否需要某层元素时，避免输出空 `<g></g>`；`init` 幂等，`lazy()` 可跳过本层。
- `SvgWrite` 是底层写入 trait，核心是 `push_num` 的精度归一（\(10^9\) 舍入 + 整数快路径）与 `escape_str` 对 `&`/`<` 的转义；`SvgFormatter` 是它在 `attr_with` 闭包里的具体实现。
- `SvgDisplay` 是「值 → 字符串」的格式化 trait，是 `attr(name, value)` 对 `value` 的要求。
- `SvgTransform`/`SvgUrl`/`SvgIdRef` 三个 newtype 适配器把 typst 内部类型临时套上 SVG 语义，且 `SvgTransform` 会挑选最短的 `scale`/`translate`/`matrix` 写法以压缩体积。

## 7. 下一步学习建议

`write.rs` 是后续所有渲染模块的「笔」。接下来建议：

- 进入第 3 单元「矢量原语」，先读 [src/path.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs) 的 `SvgPathBuilder`——它大量使用 `SvgWrite` 的 `push_num`/`push_str` 来拼装 SVG path 数据字符串，是 `SvgDisplay` 思路的延续。
- 再读 [src/shape.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs) 的 `render_shape`，看它如何用本讲的 `SvgElem`/`attr`/`SvgTransform` 把一个 `Shape` 写成 `<path>` 元素。
- 留意后续讲义中 `attr_with` + `SvgFormatter` 在颜色、渐变（`paint.rs`）里的高频使用——本讲为它们准备了全部底层工具。
