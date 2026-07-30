# DOM 到 HTML 字符串的编码

## 1. 本讲目标

本讲是 typst-html 编译主链路的「最后一公里」：把内存里的 HTML DOM 树序列化成一段合法的 HTML 字符串。

学完后你应当能够：

- 说清 `html()` / `html_in_bundle()` 两个入口如何共用 `html_impl`，以及 `<!DOCTYPE html>` 是在哪里注入的。
- 解释 `Writer` 结构体携带的四个字段（`buf` / `level` / `link_resolver` / `pretty`）各自的作用，特别是 `link_resolver` 在文档内跳转与 SVG frame 中的职责。
- 逐行读懂 `write_element`：开标签、属性的「空值简写」、void 与 foreign 自闭合标签、以及 raw / escapable-raw / 普通 children 三类内容分支。
- 掌握 `write_children` 的 pretty 打印算法，说清 `allows_pretty_inside` 与 `wants_pretty_around` 两个判定函数如何决定换行与缩进何时插入。
- 理解 raw 文本元素（`<script>` / `<style>`）为何要走特殊编码路径，以及 `<pre>` 首字符为换行时的规范处理。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在前置讲义中已建立）：

- **HtmlDocument 与 DOM 树**（u2-l1）：编译产物 `HtmlDocument` 内部是一棵以 `HtmlElement` 为节点、用扁平数组 `HtmlOutput` 存储的 DOM 树，根元素由 `root()` 取出。`HtmlDocument` 不实现 `Hash`，因为内省器不可哈希。
- **HtmlNode 四变体**（u2-l1）：`Tag`（内省元数据，不产生输出）/ `Text`（纯文本）/ `Element`（子元素）/ `Frame`（待嵌入的 SVG）。
- **HtmlElement 字段**（u2-l1）：`tag` / `attrs` / `css` / `children` / `parent` / `span` / `pre_span`。其中 `pre_span` 标记编译器生成的 `white-space: pre-wrap` 保护 span。
- **编译主链路**（u3-l1）：`html_document` 把 `Content` 编译成 `HtmlDocument`，本讲的 `html` 则把 `HtmlDocument` 编码成 `String`——编译与编码是分离的两段。
- **HtmlTag / HtmlAttr 的驻留**（u2-l2、u2-l3）：标签名与属性名都是 `PicoStr` 句柄，编码时需用 `.resolve()` 还原成字符串。
- **Display 与内容模型**（u2-l4、u4-l2）：`property::Display::default_for(tag)` 给出 HTML 规范 §15 的 UA 默认 `display`，`tag::is_void` / `is_raw` / `is_escapable_raw` 等是规范 §13.1.2 的语法分类。

本讲**不再依赖 Engine**：编码阶段是纯函数式的，输入 DOM、输出字符串，唯一的「外部依赖」是一个用于解析链接的 `LateLinkResolver`。

## 3. 本讲源码地图

本讲几乎全部聚焦于单个文件 [encode.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs)，它负责「DOM → String」。下表列出本讲涉及的关键函数及其职责：

| 函数 / 类型 | 职责 |
|---|---|
| `HtmlOptions` | 唯一的导出选项：是否 pretty 打印 |
| `html` / `html_in_bundle` | 两个对外入口，分别服务单文档与 bundle |
| `html_impl` | 共享实现：写 DOCTYPE + 根元素 |
| `Writer` | 编码状态机：缓冲区、缩进层级、链接解析器、pretty 开关 |
| `write_indent` | 按 `level` 写换行 + 两个空格缩进（仅 pretty 时） |
| `write_node` | 按 `HtmlNode` 变体分派 |
| `write_text` / `write_escape` | 文本与字符转义 |
| `write_element` | 元素编码核心（标签、属性、自闭合、内容分支） |
| `write_children` | 子节点序列化 + pretty 缩进算法 |
| `allows_pretty_inside` / `wants_pretty_around` | pretty 判定的两个决策函数 |
| `write_raw` / `RawMode` / `find_closing_tag` | raw 文本元素的特殊编码 |
| `write_escapable_raw` | escapable raw（`<textarea>`/`<title>`）编码 |
| `write_frame` | 把 `HtmlFrame` 交给 `typst_svg::svg_in_html` 生成内联 SVG |

辅助函数来自三个兄弟文件，本讲会引用但不再展开：

- [tag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs)：`is_void` / `is_raw` / `is_escapable_raw` / `is_foreign_self_closing` / `is_metadata_content` 等。
- [charsets.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs)：字符有效性判定。
- [property.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs)：`Display::default_for` / `is_tabular`。

---

## 4. 核心概念与源码讲解

### 4.1 编码总入口：html_impl 与 Writer

#### 4.1.1 概念说明

DOM 树建好之后，最后一道工序就是「遍历这棵树，把每个节点拼成 HTML 文本」。typst-html 提供了**两个对外入口**做这件事：

- `html()`：服务**单文档导出**（typst-cli 的 `--format html` 走这条）。
- `html_in_bundle()`：服务 **bundle 导出**（一份输出里塞多个文档/根元素），它的签名直接接收一个「已经追踪好的根元素」和一个外部传入的 `link_resolver`，因为 bundle 需要跨文档解析链接。

两者共用一份 `html_impl` 实现，差异只在「怎么拿到 link_resolver 和根元素」。

#### 4.1.2 核心流程

```
html(document, options)
  ├── 用 document 的内省器构造 LateLinkResolver
  ├── Writer::new(link_resolver.track(), options.pretty)
  └── html_impl(writer, document.root())
                       │
html_in_bundle(root, options, link_resolver)
  ├── Writer::new(link_resolver, options.pretty)   // link_resolver 已是 Tracked
  └── html_impl(writer, root)
                       │
                       ▼
html_impl(writer, root):
  1. buf.push_str("<!DOCTYPE html>")   // 无条件注入 DOCTYPE
  2. write_indent()                     // pretty 时换行
  3. write_element(root)                // 递归编码整棵树
  4. pretty 时末尾再补一个换行
  5. 返回 buf
```

#### 4.1.3 源码精读

先看唯一的选项类型，它只有一个布尔字段 [encode.rs:15-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L15-L20)：`pretty` 决定输出是人类可读（带缩进换行）还是紧凑（单行）。

两个入口 [encode.rs:22-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L22-L40)。注意 `html()` 里这一行：

```rust
let link_resolver = LateLinkResolver::new(None, document.introspector().as_ref());
let w = Writer::new(link_resolver.track(), options.pretty);
```

`LateLinkResolver` 负责把「逻辑链接目标」解析成真实的 URL / fragment。`None` 表示没有外部基准路径（bundle 场景才会有）。`link_resolver.track()` 把它转成 comemo 的 `Tracked` 句柄——之所以要 tracked，是因为 `write_frame` 最终调用的 `typst_svg::svg_in_html` 是被 `memoize` 缓存的，缓存键里需要可哈希、可追踪的依赖。

共享实现 [encode.rs:42-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L42-L51)：

```rust
fn html_impl(mut w: Writer, root: &HtmlElement) -> SourceResult<String> {
    w.buf.push_str("<!DOCTYPE html>");
    write_indent(&mut w);
    write_element(&mut w, root)?;
    if w.pretty { w.buf.push('\n'); }
    Ok(w.buf)
}
```

**DOCTYPE 在这里被无条件注入**，无论 `html` 还是 `html_in_bundle` 都会带上 `<!DOCTYPE html>`——这保证输出的 HTML 处于标准模式（standards mode），而非怪异模式（quirks mode）。

`Writer` 是贯穿全程的状态机 [encode.rs:53-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L53-L64)：

```rust
struct Writer<'a> {
    buf: String,                              // 输出缓冲区
    level: usize,                             // 当前缩进层级
    link_resolver: Tracked<'a, LateLinkResolver<'a>>,  // 链接解析
    pretty: bool,                             // 是否 pretty 打印
}
```

四个字段分工明确：`buf` 累积最终字符串；`level` 记录当前嵌套深度（用于算缩进空格数）；`link_resolver` 在编码 Frame 时传给 SVG 生成器以解析文档内跳转锚点；`pretty` 是总开关，但注意它会在 `write_children` 里被**临时改写**（见 4.4）。

缩进工具函数 [encode.rs:78-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L78-L86)：仅在 `pretty` 为真时写一个换行，再加 `level × 2` 个空格。typst-html 固定用**两个空格**做一级缩进。

#### 4.1.4 代码实践

**实践目标**：确认 DOCTYPE 注入与 pretty 开关如何受 CLI 控制。

**操作步骤**：

1. 在 [typst-cli/src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L357) 中找到 `export_html`，确认它构造 `HtmlOptions { pretty: config.pretty }` 后调用 `typst_html::html(document, &options)`。
2. 追溯 `config.pretty` 来自 `CompileCommand` 的 `--pretty` 参数（[args.rs:322-328](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L322-L328)，其 help 文字写明「Affects HTML, SVG, and PDF export」）。

**需要观察的现象 / 预期结果**：写一份极简的 `input.typ`（例如 `Hello`），分别用以下两条命令导出（待本地验证）：

```bash
typst compile --format html input.typ out-compact.html
typst compile --format html --pretty input.typ out-pretty.html
```

两份输出都应以 `<!DOCTYPE html>` 开头；紧凑版是近似一行，pretty 版带换行与缩进。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `html_in_bundle` 要让调用方自己传入 `Tracked<LateLinkResolver>`，而不是像 `html` 那样从 document 内省器构造？

**答案**：bundle 场景下，多个根元素共享同一套跨文档链接解析逻辑，链接目标可能指向**别的文档**，因此基准路径与解析器必须由外层统一管理后传入，而不能让每个根元素各自从自己的内省器里重新构造一份。

**练习 2**：`html_impl` 末尾的 `if w.pretty { w.buf.push('\n'); }` 能否去掉？去掉会怎样？

**答案**：不能随意去掉。它是为了让 pretty 输出的文件以换行结尾（POSIX 文本文件惯例）。去掉后文件最后一行 `</html>` 后没有换行，某些工具（如 `wc -l`、编辑器）会把末行当作「无换行」处理，但不影响浏览器解析。

---

### 4.2 节点分派与文本转义：write_node / write_text / write_escape

#### 4.2.1 概念说明

DOM 树的节点有四种（`HtmlNode` 的四个变体）。编码时需要一个**分派器**根据变体走不同路径。其中文本节点最微妙：HTML 文本里有些字符（如 `<`、`&`）如果不转义会破坏结构，而有些字符（如控制字符）根本无法在 HTML 中表达，需要报错。typst-html 把「是否需要转义」做成一个参数 `escape_text`，让它能被上层（`write_children`）按需控制。

#### 4.2.2 核心流程

```
write_node(w, node, escape_text):
  match node:
    Tag(_)        → 什么都不做（内省专用，不产出 HTML）
    Text(t, span) → write_text(w, t, span, escape_text)
    Element(e)    → write_element(w, e)            // 不受 escape_text 影响
    Frame(f)      → write_frame(w, f)              // 走 SVG 路径

write_text(w, text, span, escape):
  对每个字符 c:
    if escape || !is_valid_in_normal_element_text(c):
        write_escape(w, c)   // 可能返回 Err(unencodable)
    else:
        buf.push(c)          // 原样写入
```

#### 4.2.3 源码精读

分派器很薄 [encode.rs:88-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L88-L97)。关键点是 `HtmlNode::Tag(_)` 对应 `=> {}`——**Tag 节点在编码阶段被完全丢弃**，它只服务于内省（回忆 u2-l1：Tag 不产生 HTML 输出）。

文本编码 [encode.rs:99-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L99-L109)：

```rust
for c in text.chars() {
    if escape || !charsets::is_valid_in_normal_element_text(c) {
        write_escape(w, c).at(span)?;
    } else {
        w.buf.push(c);
    }
}
```

这里有两个触发转义的条件：

1. `escape` 参数为真——这是由 `write_children` 传入的 `element.pre_span`（见 4.4）。当元素是编译器生成的 `white-space: pre-wrap` 保护 span 时，**所有字符**（包括普通空格）都强制走 `write_escape`，于是空格变成 `&#x20;`、制表符变成 `&#x9;`，防止 HTML 格式化工具破坏空白保护（呼应 u4-l1 的空白保护机制）。
2. 字符本身在普通元素文本中不合法（`&`、`<`、控制字符等）。

字符转义表 [encode.rs:367-382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L367-L382)：

```rust
match c {
    '&'  => "&amp;",
    '<'  => "&lt;",
    '>'  => "&gt;",
    '"'  => "&quot;",
    '\'' => "&apos;",
    c if is_w3c_text_char(c) && c != '\r' => write!(buf, "&#x{:x};", c as u32),
    _ => return Err(unencodable(c)),
}
```

- 五个「语法敏感字符」用**命名实体**（named reference）。
- 其他合法的 W3C 文本字符（含被 `escape` 强制进来的空格 ` `、制表符 `\t`）写成**十六进制数字字符引用**，例如空格 → `&#x20;`。
- `\r` 被显式排除在「合法文本」之外，会落到最后的 `_` 分支触发 `unencodable` 错误（[encode.rs:384-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L384-L388)），因为 HTML 规范把 CR 视作非法的原始字符。
- 既非语法字符、又非合法 W3C 文本字符（如某些 C0 控制字符），直接报 `unencodable`。

判定函数来自 [charsets.rs:40-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L40-L62)：`is_valid_in_normal_element_text` 把 `&`、`<` 判为非法（需转义），`is_w3c_text_char` 进一步排除「非字符」与「控制字符」（ASCII 空白除外）。

#### 4.2.4 代码实践

**实践目标**：预测不同文本的转义结果。

**操作步骤**：阅读 `write_escape` 与 `charsets::is_w3c_text_char`，对下面三段文本（假设 `escape=false`、位于普通元素中）逐字符预测输出：

1. `a < b & c`
2. `tab\there`（含一个制表符）
3. `line1\r\nline2`（含 CR）

**需要观察的现象 / 预期结果**（基于源码推导）：

1. `a &lt; b &amp; c`（`<` 与 `&` 转义，空格与字母原样）。
2. `tab\there` 原样输出——制表符 `\t` 是 ASCII 空白，`is_w3c_text_char` 允许，`is_valid_in_normal_element_text` 也允许（既非 `&` 也非 `<`），所以**不转义**直接写入。注意：这意味着在普通元素里制表符会被浏览器折叠；要保护它得靠 pre-wrap span（`escape=true`）。
3. 报错：`\r` 落到 `write_escape` 的 `_` 分支，返回 `unencodable`。

> 这些是源码层面的推导，实际运行行为可本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `>` 也要转义成 `&gt;`？规范允许文本里出现 `>`。

**答案**：虽然规范允许，但转义 `>` 是一种防御性写法，能避免 `-->`、`]]>` 这类序列在某些上下文（如注释、CDATA）中被误解析，也让人更容易识别标签边界。typst-html 选择**总是**转义 `>`，实现简单且安全。

**练习 2**：`escape=true` 时字母 `a` 会变成什么？这会不会让正常文本变得不可读？

**答案**：会变成 `&#x61;`（数字字符引用）。所以 `escape=true`（即 `pre_span`）**只用于编译器生成的、内容是空白的保护 span**，不会作用在用户的正常文本上——因此不会让正常文本变得不可读。

---

### 4.3 元素编码核心：write_element（属性、自闭合、三类内容分支）

#### 4.3.1 概念说明

`write_element` 是整个编码器最重的函数。一个 HTML 元素的编码要回答四个问题：

1. 开标签怎么写？标签名从驻留的 `HtmlTag` 用 `.resolve()` 还原。
2. 属性怎么写？尤其是**空值属性**——HTML 允许 `<input disabled>` 这种简写，等价于 `<input disabled="">`。
3. 是否自闭合？void 元素（`<img>`、`<br>` 等）和部分 foreign（MathML）元素不能有子节点。
4. 内容怎么写？普通子节点、raw 文本（`<script>`/`<style>`）、escapable raw（`<textarea>`/`<title>`）走三条不同路径。

#### 4.3.2 核心流程

```
write_element(w, element):
  1. 写 '<' + tag.resolve()
  2. 遍历 attrs:
       写 ' ' + attr.resolve()
       若 value 非空: 写 '="' + (转义后的 value) + '"'
       若 value 为空:   省略 ="..."，只留属性名（简写）
  3. 若 is_foreign_self_closing(tag): 写 '/'
  4. 写 '>'
  5. 若 is_void 或 foreign_self_closing:
        断言无 children，直接返回（不写闭标签）
  6. 若 (pre|textarea) 且内容以换行开头: 额外补一个 '\n'   # 规范 §13.1.2.5
  7. 内容分支:
        is_raw           → write_raw
        is_escapable_raw → write_escapable_raw
        否则有 children  → write_children
  8. 写 '</' + tag.resolve() + '>'
```

#### 4.3.3 源码精读

开标签与属性 [encode.rs:112-134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L112-L134)：

```rust
w.buf.push('<');
w.buf.push_str(&element.tag.resolve());

for (attr, value) in &element.attrs.0 {
    w.buf.push(' ');
    w.buf.push_str(&attr.resolve());
    // 空值用简写：<elem attr>  等价于  <elem attr="">
    if !value.is_empty() {
        w.buf.push('=');
        w.buf.push('"');
        for c in value.chars() {
            if charsets::is_valid_in_attribute_value(c) {
                w.buf.push(c);
            } else {
                write_escape(w, c).at(element.span)?;
            }
        }
        w.buf.push('"');
    }
}
```

**属性空值简写**是本模块的一个重点：当 `value.is_empty()` 时，只写属性名而不写 `=""`。例如布尔属性 `disabled`、`checked` 会输出成 `<input disabled>` 而非 `<input disabled="">`，更贴近手写 HTML 的习惯。属性值的转义与文本不同：它用 `is_valid_in_attribute_value` 判定（[charsets.rs:25-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L25-L36)），把 `&` 和 `"` 都判为非法——因为属性值被双引号包裹，`"` 会提前结束属性。

自闭合与 void 处理 [encode.rs:136-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L136-L147)：

```rust
if tag::is_foreign_self_closing(element.tag) {
    w.buf.push('/');          // MathML 的 mprescripts/mspace 写成自闭合 <x/>
}

w.buf.push('>');

if tag::is_void(element.tag) || tag::is_foreign_self_closing(element.tag) {
    if !element.children.is_empty() {
        bail!(element.span, "HTML void elements must not have children");
    }
    return Ok(());            // 不写闭标签
}
```

- void 元素集合见 [tag.rs:125-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L125-L142)（`area`/`base`/`br`/`col`/`embed`/`hr`/`img`/`input`/`link`/`meta`/`source`/`track`/`wbr`），它们**不允许有子节点**，若 DOM 里挂了子节点会直接报错。
- foreign 自闭合（[tag.rs:163-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L163-L165)）目前只有 MathML 的 `mprescripts` 和 `mspace`，它们额外加一个 `/` 写成 `<mspace />` 形式。

`<pre>`/`<textarea>` 的换行修正 [encode.rs:149-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L149-L152)：

```rust
if matches!(element.tag, tag::pre | tag::textarea) && starts_with_newline(element) {
    w.buf.push('\n');
}
```

这是 HTML 规范 §13.1.2.5 的要求：`<pre>` 和 `<textarea>` 紧跟开标签的第一个换行会被解析器忽略。如果内容本来就以换行开头，编码器就**补一个额外的换行**，保证用户意图的空行不被吃掉。`starts_with_newline`（[encode.rs:204-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L204-L214)）会跳过前导的 Tag 节点，看第一段文本是否以 `\n`/`\r` 起头。

最后是三类内容分支 [encode.rs:154-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L154-L164)：raw（`<script>`/`<style>`）走 4.5 的 `write_raw`；escapable raw（`<textarea>`/`<title>`）走 `write_escapable_raw`；其余元素若有子节点则走 `write_children`。无论哪条分支，最后都统一写闭标签 `</tag>`。

#### 4.3.4 代码实践

**实践目标**：理解属性空值简写与 void 校验。

**操作步骤**：

1. 在源码中确认：`HtmlAttrs.0` 是 `EcoVec<(HtmlAttr, EcoString)>`（[dom.rs:362-364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L362-L364)），属性按**插入顺序**编码、不去重。
2. 想象一个 `HtmlElement { tag: input, attrs: [(type, "text"), (disabled, "")] }`，手写它的开标签输出。

**需要观察的现象 / 预期结果**：输出应为 `<input type="text" disabled>`——`disabled` 因空值走简写，不带 `=""`。又因 `input` 是 void 元素，整体没有闭标签。

**练习（验证型）**：如果把一个子节点挂到 `<img>` 上（void 元素），追踪 `write_element` 会在哪一行报什么错？

**预期**：在 [encode.rs:142-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L142-L145) 报 `"HTML void elements must not have children"`，并附带 `element.span` 定位。

#### 4.3.5 小练习与答案

**练习 1**：属性值里出现双引号 `"`（例如 `value = a"b`）会被如何编码？

**答案**：`"` 不是 `is_valid_in_attribute_value` 允许的字符（见 charsets.rs），于是走 `write_escape`，输出 `&quot;`，最终属性值写作 `value="a&quot;b"`，安全地嵌在双引号里。

**练习 2**：为什么 void 元素即便没有 children 也要在代码里做 `if !children.is_empty()` 检查？上游不是应该保证吗？

**答案**：这是一种防御性断言。上游（rules、convert）通常不会给 void 元素挂子节点，但 DOM 可被 `html.elem` 手写或被 `root_mut` 改动，编码器作为「最后一道关卡」，必须在产出非法 HTML 之前把这种情况变成明确的编译错误，而不是静默输出错误结构。

---

### 4.4 缩进与换行：write_children 与 pretty 打印

#### 4.4.1 概念说明

pretty 打印的目标是让输出对人类友好：块级元素之间换行、嵌套层级用缩进表达。但 HTML 的空白是**有语义**的——在行内元素之间随意插入换行/空格会改变渲染（行内元素之间的空白会变成一个可见空格）。因此 typst-html 不能无脑给所有子节点加换行，而要精确判断：

- 这个元素的**内部**是否允许加换行？（`allows_pretty_inside`）
- 某个**子元素**前后是否应该加换行？（`wants_pretty_around`）

这两个判定共同决定换行何时插入。这是本讲最精巧的部分。

#### 4.4.2 核心流程

```
write_children(w, element):
  pretty        = w.pretty                          # 先保存当前总开关
  pretty_inside = allows_pretty_inside(element.tag)
                 && 某个孩子是「想要 pretty 的 Element」或 Frame
  w.pretty     &= pretty_inside                     # 临时关掉非 pretty-inside 的 pretty
  indent        = w.pretty                          # 第一个孩子是否缩进
  level        += 1
  遍历孩子 c:
    Tag        → 跳过（continue）
    Element    → pretty_around = w.pretty && wants_pretty_around(c)
    Text/Frame → pretty_around = false
    若 take(indent) || pretty_around:  write_indent()   # 在 c 之前写换行+缩进
    write_node(c, escape_text = element.pre_span)
    indent = pretty_around                            # 下一个孩子是否缩进取决于当前孩子
  level -= 1
  write_indent()                                      # 闭标签前的换行
  w.pretty = pretty                                   # 恢复总开关
```

#### 4.4.3 源码精读

主体 [encode.rs:169-202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L169-L202)：

```rust
fn write_children(w: &mut Writer, element: &HtmlElement) -> SourceResult<()> {
    let pretty = w.pretty;
    let pretty_inside = allows_pretty_inside(element.tag)
        && element.children.iter().any(|node| match node {
            HtmlNode::Element(child) => wants_pretty_around(child),
            HtmlNode::Frame(_) => true,
            _ => false,
        });

    w.pretty &= pretty_inside;
    let mut indent = w.pretty;

    w.level += 1;
    for c in &element.children {
        let pretty_around = match c {
            HtmlNode::Tag(_) => continue,
            HtmlNode::Element(child) => w.pretty && wants_pretty_around(child),
            HtmlNode::Text(..) | HtmlNode::Frame(_) => false,
        };

        if core::mem::take(&mut indent) || pretty_around {
            write_indent(w);
        }
        write_node(w, c, element.pre_span)?;
        indent = pretty_around;
    }
    w.level -= 1;

    write_indent(w);
    w.pretty = pretty;
    Ok(())
}
```

读懂这段的关键是 `indent` 这个游标与 `mem::take` 的配合：

- 进入循环前 `indent = w.pretty`，所以**第一个真实孩子**前面一定会写一次缩进（只要 pretty 还开着）。
- `mem::take(&mut indent)` 取出 `indent` 的旧值并把 `indent` 清零。换句话说：「上一个孩子是否触发了缩进」。
- 一个孩子前面写缩进的条件是：**上一个孩子想要 pretty_around**，**或**当前孩子自己想要 pretty_around。
- 循环末尾 `indent = pretty_around`，把「当前孩子是否想要 pretty」传给下一个孩子。

这套逻辑保证：**只有当相邻的块级元素之间才换行**；纯文本和行内元素前后不会插入换行，从而不破坏行内渲染。

两个决策函数。`allows_pretty_inside` [encode.rs:331-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L331-L348) 判断「在某元素内部加换行是否安全」：

```rust
fn allows_pretty_inside(tag: HtmlTag) -> bool {
    if tag::mathml::is_mathml(tag) && !tag::mathml::is_token(tag) {
        return true;
    }
    let Some(display) = property::Display::default_for(tag) else { return false };
    (display == property::Display::Block && tag != tag::pre)
        || display.is_tabular()
        || display == property::Display::ListItem
        || tag == tag::head
}
```

- MathML 的非 token 元素（容器类）允许。
- 否则查 UA 默认 `display`：`Block`（且非 `<pre>`，因为 `<pre>` 对空白敏感）、表格类（`is_tabular`）、`ListItem`、以及 `<head>` 允许。
- **行内元素（`display: inline`）一律返回 false**——这正是「不在行内元素里乱加换行」的根源。

`wants_pretty_around` [encode.rs:350-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L350-L365) 判断「某子元素前后是否该换行」：

```rust
fn wants_pretty_around(element: &HtmlElement) -> bool {
    match element.tag {
        tag::mathml::math => {
            element.attrs.get(attr::mathml::display).is_some_and(|v| v == "block")
        }
        t if tag::mathml::is_mathml(t) => true,
        tag::pre => true,
        t if tag::is_metadata_content(t) => true,
        t => allows_pretty_inside(t),
    }
}
```

- `<math display="block">` 才换行（块级公式），行内公式不换行。
- 其他 MathML 元素、`<pre>`、metadata 元素（`<meta>`/`<link>`/`<title>` 等，见 [tag.rs:170-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L170-L182)）都换行。
- 兜底：直接复用 `allows_pretty_inside`——块级元素换行，行内元素不换行。

注意 `allows_pretty_inside` 是「纯规范驱动」的（基于 display），而 `wants_pretty_around` 在其之上叠加了一些**主观偏好**（如 `<pre>` 一定换行、metadata 一定换行），代码注释也点明了这一点。

#### 4.4.4 代码实践：追踪 `<ul><li>..</li></ul>` 的两种输出

**实践目标**：用本节的算法，精确推导 pretty=true 与 pretty=false 下 `<ul><li>..</li></ul>`（`ul` 内一个 `li`，`li` 内是文本 `..`）的输出差异。

**操作步骤（源码级手推）**：

设 DOM 为 `ul → [li → [Text("..")]]`，`pre_span=false`。

**pretty=true 时**：

1. `html_impl` 写 `<!DOCTYPE html>` + 换行，进入根元素……最终调到 `write_children(ul)`。
2. 在 `write_children(ul)`：
   - `allows_pretty_inside(ul)`：`ul` 的 `default_for` 是 `Block`（[property.rs:122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L122)），且非 `pre` → `true`。
   - 孩子里有 `li` 元素，`wants_pretty_around(li)` 兜底到 `allows_pretty_inside(li)`：`li` 的 `default_for` 是 `ListItem`（[property.rs:123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L123)），命中 `== ListItem` → `true`。所以 `pretty_inside = true`。
   - `w.pretty &= true`（仍为 true），`indent = true`，`level` 升到 1。
   - 处理孩子 `li`：`pretty_around = true`；`mem::take(indent)` 返回 `true` → **写换行 + 2 空格**；写 `<li>...</li>`（li 内部见下一步）；`indent = true`。
   - 循环结束，`level` 回到 0，`write_indent` 写**换行 + 0 空格**（`</ul>` 前）。
3. 在 `write_element(li)` → `write_children(li)`：
   - `allows_pretty_inside(li)` = `true`，但孩子是 `Text("..")`，`.any(...)` 对 Text 返回 `false`，所以 `pretty_inside = true && false = false`。
   - `w.pretty &= false` → **pretty 临时关闭**，`indent = false`。
   - 处理文本 `..`：`pretty_around = false`，`take(indent=false)` 返回 false，`false || false` → **不写缩进**；`write_text` 直接写 `..`。
   - 循环结束，`write_indent` 因 pretty 关闭而不输出；`w.pretty` 恢复为 true。

所以 `li` 的文本 `..` 紧贴在 `<li>` 之后、`</li>` 之前，**不换行**。最终（省略外层 html/body）：

```
<ul>
  <li>..</li>
</ul>
```

**pretty=false 时**：`write_indent` 全程是空操作，`w.pretty &= pretty_inside` 也无意义（本来就是 false）。所有节点紧挨着输出：

```
<ul><li>..</li></ul>
```

**需要观察的现象 / 预期结果**：换行与缩进**只在「想要 pretty_around 的子元素」之前**插入；纯文本子节点绝不触发换行。`<li>` 想要 pretty_around，所以它前面换行；`li` 内的文本不想要，所以 `..` 与 `<li>` 同行。

> 以上是依据 encode.rs 源码逐行推导的结果。可用 `typst compile --format html [--pretty] input.typ` 本地验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：把 `<ul>` 换成行内元素 `<span>`（内含一个 `<strong>` 子元素），pretty=true 时还会换行吗？

**答案**：不会。`allows_pretty_inside(span)`：`span` 的 `default_for` 是 `Inline`（[property.rs:204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L204)），不满足 Block/tabular/ListItem/head 任一条件 → `false`。于是 `pretty_inside=false`，`w.pretty` 被临时关闭，`<span><strong>..</strong></span>` 全部同行输出。这避免了在行内元素间插入会改变渲染的空白。

**练习 2**：`write_children` 里 `write_node(w, c, element.pre_span)` 把 `pre_span` 当作 `escape_text` 传入。如果一个 `<span>` 的 `pre_span=true`，它里面的文本 `a b` 会怎样编码？

**答案**：`escape=true`，所以 `write_text` 对每个字符调 `write_escape`：`a`→`&#x61;`、空格→`&#x20;`、`b`→`&#x62;`，输出 `&#x61;&#x20;&#x62;`。这正是空白保护 span 防止格式化工具破坏空格的手段（呼应 u4-l1）。实际中 `pre_span` 只设在内容为空白的编译器生成 span 上。

---

### 4.5 特殊内容的编码路径：raw 文本与 Frame（SVG 嵌入）

#### 4.5.1 概念说明

有三类元素的内容不能用普通的 `write_children` 处理：

1. **raw 文本元素** `<script>` / `<style>`：它们的内容是「原样文本」，HTML 解析器对其中的 `<`、`&` **不做转义**，直到遇到对应的 `</script>` / `</style>`。如果内容里出现了自己的闭标签序列，会提前结束元素——这是经典的 XSS 注入点，必须检测。
2. **escapable raw 文本元素** `<textarea>` / `<title>`：内容也是原始文本，但**会处理字符引用**（`&amp;` 会被解码），所以只需要转义 `&`/`<` 等，但不能有子元素。
3. **Frame 节点** `HtmlNode::Frame`：这不是 HTML 元素，而是 typst 排版产出的 `Frame`，需要交给 `typst-svg` 转成内联 SVG 字符串再拼进去。

#### 4.5.2 核心流程

```
write_raw(w, element):            # <script>/<style>
  text = collect_raw_text(element)     # 只允许 Text 孩子；非 W3C 字符报错
  if find_closing_tag(text, tag):      # 内容里出现 </script 等 → 报错
      bail
  mode = pretty ? RawMode::of(element, text) : RawMode::Keep
  match mode:
    Keep   → 原样写
    Wrap   → 换行 + text + 缩进
    Indent → level+1，每行各自缩进

write_escapable_raw(w, element):  # <textarea>/<title>
  walk_raw_text(element, |piece, span| write_text(w, piece, span, escape=false))

write_frame(w, frame):            # HtmlNode::Frame
  svg = typst_svg::svg_in_html(frame.inner, frame.text_size, w.pretty,
                               frame.id, frame.css.to_inline(), frame.anchors, w.link_resolver)
  pretty ? 逐行缩进后写入 : 原样写入
```

#### 4.5.3 源码精读

raw 文本编码 [encode.rs:216-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L216-L250)。第一步 `collect_raw_text`（[encode.rs:257-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L257-L268)）遍历孩子，**只接受 Text 节点**（遇到 Element/Frame 在 `walk_raw_text` 里报错，[encode.rs:270-286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L270-L286)），并把任何非 W3C 文本字符判为不可编码。

闭标签检测 [encode.rs:288-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L288-L301) 实现了规范 §13.1.2.6：在文本里找 `</` 后跟标签名（大小写不敏感）、再跟一个合法终止符（`\t \n \f \r 空格 > /`）的序列。找到就报错并提示该序列出现在 raw 文本中，防止内容意外截断元素。

`RawMode` 决定 pretty 时如何排版 [encode.rs:303-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L303-L329)：

```rust
fn of(element: &HtmlElement, text: &str) -> Self {
    match element.tag {
        tag::script if /* type 缺省或为 text/javascript */ => {
            // 模板字符串可跨行，缩进会改变 JS 语义
            if text.contains('`') { Self::Wrap } else { Self::Indent }
        }
        tag::style => Self::Indent,
        _ => Self::Keep,
    }
}
```

- `<script>`（普通 JS）：若内容含反引号 `` ` ``（模板字面量），缩进可能改变字符串内容，于是只 `Wrap`（首尾换行、不逐行缩进）；否则 `Indent`（逐行缩进）。
- `<style>`：CSS 对缩进不敏感，逐行缩进 `Indent`。
- 非 pretty 时统一 `Keep`（原样）。

escapable raw 编码 [encode.rs:252-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L252-L255) 复用 `write_text` 但强制 `escape=false`——因为这类元素的内容会被解析器当作「可含字符引用的原始文本」，typst-html 选择不主动转义（交给上层保证内容合法）。

Frame 编码 [encode.rs:390-414](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L414) 把活儿交给 `typst_svg::svg_in_html`：

```rust
let svg = typst_svg::svg_in_html(
    &frame.inner, frame.text_size, w.pretty, frame.id.as_deref(),
    &eco_format!("{}", frame.css.to_inline()), &frame.anchors, w.link_resolver,
);
```

它把 Frame 的排版数据（`inner`）、字号（`text_size`，用于 em 缩放）、可选 SVG id、已序列化为内联 CSS 的 `frame.css`、以及跳转锚点 `anchors` 一并传给 SVG 生成器。注意 `link_resolver` 在这里被消费——这正是 4.1 里强调的「link_resolver 的核心用途」：解析 Frame 内部跳转点与文档主体的链接关系。pretty 模式下，生成出的 SVG 会**按行重新缩进**（[encode.rs:402-410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L402-L410)），这样无论 SVG 在外层 HTML 的什么缩进位置生成，缓存结果都能正确对齐（注释点明：SVG 的生成是缓存的，与外层缩进解耦）。

#### 4.5.4 代码实践

**实践目标**：理解 raw 文本的闭标签防护。

**操作步骤**：阅读 `find_closing_tag`（[encode.rs:291-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L291-L301)）。它扫描文本中的 `</`，后跟一个与标签名大小写不敏感匹配的片段，再要求下一个字符落在终止符集合 `\t \n \f \r 空格 > /` 中。考虑一段放进 `<script>` 的文本：

```
let s = "</script>"; alert(1);
```

**需要观察的现象 / 预期结果**：扫描到 `</script` 时，紧随其后的字符是 `>`，而 `>` 恰在终止符集合中 → 命中 → `write_raw` 报错 `"HTML raw text element cannot contain its own closing tag"`，并在 hint 里指出 `</script` 出现在 raw 文本中。

这正是防止用户内容提前闭合 `<script>` 的安全机制。注意：若写成 `</scriptx>`（标签名后是 `x`），由于 `scriptx` 与 `script` 不等长匹配，则不会命中——这体现「标签名精确匹配 + 终止符」双重条件（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `<script>` 内容含反引号时只 `Wrap` 而不 `Indent`？

**答案**：JS 的模板字面量（`` `...` ``）保留其中的换行与缩进作为字符串内容。若编码器逐行给模板字面量内部加缩进，会改变字符串的实际值，进而改变脚本语义。因此检测到反引号就退化为只首尾换行、不逐行缩进的 `Wrap` 模式。

**练习 2**：`write_frame` 在 pretty 模式下为什么要「逐行重新缩进」生成好的 SVG，而不是在生成 SVG 时就带上正确缩进？

**答案**：因为 SVG 的生成结果是被 `typst_svg` 缓存的（且 `svg_in_html` 不感知它在 HTML 中的嵌套深度）。把缩进作为「生成后」的后处理，可以让同一份缓存的 SVG 在不同外层缩进位置都能正确对齐，做到缓存与排版解耦。

---

## 5. 综合实践

把本讲的知识串起来，做一次「从 DOM 到字符串」的完整手工编码。

**任务**：给定下面这棵简化的 DOM（省略 html/body 外壳，所有 `pre_span=false`），分别在 `pretty=true` 和 `pretty=false` 下写出 `html_impl` 产出的字符串（DOCTYPE 之后的部分）。

```
HtmlElement { tag: ul, children: [
    Element(HtmlElement { tag: li, children: [Text("第一项")] }),
    Element(HtmlElement { tag: li, children: [Text("第二 <项>")] }),
]}
```

**要求**：

1. 对每个 `li`，先用 `allows_pretty_inside` / `wants_pretty_around` 判断是否换行。
2. 对文本 `"第二 <项>"`，用 `write_text` 规则决定 `<` 与 `>` 的转义。
3. 写出两种模式下的最终字符串。

**参考答案（基于源码推导）**：

- `ul`：`allows_pretty_inside(ul)=true`，两个 `li` 都 `wants_pretty_around=true`，所以 `pretty_inside=true`。
- 每个 `li`：`allows_pretty_inside(li)=true`，但孩子是 Text，`pretty_inside=false`，故 `li` 内部 pretty 关闭，文本紧贴 `<li>`。
- 文本 `"第二 <项>"`：`<` 和 `>` 触发 `write_escape` → `&lt;`、`&gt;`，中文字符合法原样写入。

**pretty=true**：

```
<ul>
  <li>第一项</li>
  <li>第二 &lt;项&gt;</li>
</ul>
```

**pretty=false**：

```
<ul><li>第一项</li><li>第二 &lt;项&gt;</li></ul>
```

可本地用如下 Typst 源验证（待本地验证）：

```typst
#html.elem("ul")[
  #html.elem("li")[第一项]
  #html.elem("li")[第二 <项>]
]
```

> 注意：直接用 `html.elem` 不会自动包 `<html>/<body>`；若想得到完整文档可改用 typst 原生列表语法（`- 第一项`）。本综合实践聚焦 `<ul>` 片段的编码行为。

## 6. 本讲小结

- typst-html 的编码由 `html()` / `html_in_bundle()` 两个入口共用 `html_impl`，后者**无条件注入 `<!DOCTYPE html>`**，再递归编码根元素。
- `Writer` 是携带 `buf` / `level` / `link_resolver` / `pretty` 四字段的状态机；`link_resolver` 把逻辑链接解析为真实 URL，并在编码 Frame 时传给 SVG 生成器。
- `write_node` 按 `HtmlNode` 四变体分派，**`Tag` 节点不产出任何 HTML**（仅服务内省）；`write_text` 依据 `escape` 参数与字符合法性决定是否转义。
- `write_element` 处理开标签、**属性空值简写**（`disabled` 不写 `=""`）、void/foreign 自闭合（自闭合无闭标签、void 有子节点则报错），并按 raw / escapable-raw / 普通 children 三分支编码内容。
- `write_children` 的 pretty 算法用 `indent` 游标配合 `mem::take`，只在「想要 pretty_around 的子元素」前后换行；`allows_pretty_inside`（规范驱动，基于 display）与 `wants_pretty_around`（叠加偏好）共同决定换行位置，避免在行内元素间插入会改变渲染的空白。
- raw 文本元素（`<script>`/`<style>`）走 `write_raw`，会用 `find_closing_tag` 检测内容里是否含自身闭标签以防注入；Frame 节点走 `write_frame`，交给 `typst_svg::svg_in_html` 生成内联 SVG 并按行重新缩进。

## 7. 下一步学习建议

- 本讲聚焦「DOM → String」的**结构化编码**，但文本里的字符哪些可编码、哪些要转义，细节在 [charsets.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs) 与 `write_escape`。下一讲 **u5-l2「字符集约束与 HTML 转义」** 会专门讲透字符有效性规则与不可编码字符的报错路径。
- Frame 编码依赖的 `typst_svg::svg_in_html` 与 `text_size`/`anchors` 的来源，可结合 **u6-l1「html.frame 与 SVG 嵌入」** 一起读，理解 Frame 从布局到 SVG 的完整链路。
- 若对「链接如何被解析成 fragment ID」感兴趣，`link_resolver` 的数据来源在 **u5-l4「链接锚点与文档内跳转」**（`create_link_anchors`），可与本讲的 `write_frame` 对读。
- 想理解 `pre_span` 为什么存在、空白保护 span 是如何被生成的，回顾 **u4-l1「HTML 空白保护机制」**；本讲的 `escape=pre_span` 正是它的编码端落点。
