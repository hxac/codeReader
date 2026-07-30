# HtmlAttr 与 HtmlAttrs 属性系统

## 1. 本讲目标

上一篇我们建立了 typst-html 的 DOM 数据骨架（`HtmlNode` / `HtmlElement` / `HtmlOutput`），也看懂了标签名 `HtmlTag` 如何靠字符串驻留变成一个廉价、可 `Copy` 的句柄。本讲沿着同一条思路，把镜头对准 HTML 元素的**属性（attribute）**：

- 一个 `class="btn"`、`href="#top"`、`colspan="2"` 是怎样在 typst-html 内部表示的；
- 属性名如何驻留成 `HtmlAttr`、属性列表 `HtmlAttrs` 提供哪些增删查改操作；
- 当内外层样式对**同名属性**给出不同值时，`Fold` 语义会保留哪一个、丢弃哪一个；
- 预定义的 `attr::*` 常量模块如何让 Rust 代码以零成本、可读的方式引用上百个标准属性名。

学完后，你应该能：读懂 `HtmlAttrs` 的所有方法、准确预测 `fold` 的合并结果、并能解释 `html.elem(attrs: (...))` 的字典是如何被 `cast!` 转成内部结构的。

## 2. 前置知识

本讲默认你已掌握前两篇的内容，这里只做最简回顾：

- **字符串驻留（string interning）**：把高频出现的短字符串（标签名、属性名）换成一个小巧、可 `Copy` 的整数句柄（`PicoStr`），比较和存储都更便宜。`HtmlTag` 就是 `PicoStr` 的 newtype。`HtmlAttr` 与它是同一套机制，本讲会复用这个直觉。
- **`HtmlElement` 的字段**：上一篇讲过它有 `tag`、`attrs`、`css`、`children` 等字段。其中 `attrs: HtmlAttrs` 就是本讲的主角。
- **Typst 的 `Fold` 机制**：可折叠字段（`#[fold]`）在穿过样式链时，会按「内层优先、外层补缺」的规则把多层取值合并成一个最终值。本讲会把它落到 `HtmlAttrs` 的具体合并逻辑上。
- **两个层次的 `Html` 元素**：`HtmlElem`（在 `lib.rs`，是 Typst 的原生元素，产出 `Content`）与 `HtmlElement`（在 `dom.rs`，是最终 DOM 节点）。两者各有一组 `with_attr` 方法，分别服务于「构造 Typst 内容」与「直接拼装 DOM」两个阶段。本讲会明确区分二者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | 定义 `HtmlAttr`、`HtmlAttrs`，以及 `Fold for HtmlAttrs` 和两个 `cast!` 互转 |
| [src/attr.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs) | 用 `HtmlAttr::constant` 把上百个标准属性名预定义为常量 |
| [src/charsets.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs) | 提供 `is_valid_in_attribute_name`，决定哪些字符能进入属性名 |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs) | `HtmlElem.attrs` 字段标记 `#[fold]`，并提供 `with_attr` / `with_optional_attr` |
| [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs) | show 规则里用 `attr::*` 常量 + `with_attr` 给生成的元素挂属性的真实例子 |

`typst-library` 里的 `Fold` trait 定义也会被引用一次，用来解释合并的方向约定。

## 4. 核心概念与源码讲解

### 4.1 HtmlAttr：属性名的驻留句柄

#### 4.1.1 概念说明

HTML 属性名（`class`、`href`、`aria-level`、`colspan`……）和标签名一样，是「少量字符串被反复使用」的典型场景。如果把它们当成普通 `String` 到处传递，每次比较都要逐字符扫描、每次克隆都要分配内存。

typst-html 的做法和上一篇的 `HtmlTag` 完全对称：定义一个 newtype `HtmlAttr(PicoStr)`，把属性名**驻留**成一个 8 字节、可 `Copy`、可 O(1) 整数比较的句柄。于是 `attr.class == attr.href` 只是一次整数相等判断。

#### 4.1.2 核心流程

`HtmlAttr` 有两条创建路径，与 `HtmlTag` 一一对应：

1. **运行期 `intern(string)`**：用户输入主入口（`html.elem(attrs: (...))` 的字典 key 经 `cast!` 进入）。逐字符校验合法性，失败返回 `StrResult`。
2. **编译期 `constant(string)`**：`const fn`，失败直接 `panic!`。供 `attr.rs` 把上百个标准属性名预定义为零成本常量。

校验规则由 `charsets::is_valid_in_attribute_name` 决定。注意：**属性名的字符规则比标签名宽松得多**——标签名只允许 ASCII 字母、数字、连字符（白名单），而属性名采用**黑名单**：只禁止少数几个语法上有歧义的字符（空格、引号、`>`、`/`、`=`、控制字符等），其余一律放行，甚至允许 Unicode 字母。

#### 4.1.3 源码精读

`HtmlAttr` 本身只是 `PicoStr` 的一层包装：

```rust
#[derive(Copy, Clone, Eq, PartialEq, Hash)]
pub struct HtmlAttr(PicoStr);
```

定义见 [src/dom.rs:L429-L431](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L429-L431)。派生了 `Copy`/`Eq`/`Hash`，所以属性名可以随意按值复制、用整数哈希、用作 `HashMap` 键。

运行期入口 `intern` 先拒绝空串，再用 `find` 找到第一个非法字符并报错：

```rust
pub fn intern(string: &str) -> StrResult<Self> {
    if string.is_empty() { bail!("attribute name must not be empty"); }
    if let Some(c) = string.chars().find(|&c| !charsets::is_valid_in_attribute_name(c)) {
        bail!("the character {} is not valid in an attribute name", c.repr());
    }
    Ok(Self(PicoStr::intern(string)))
}
```

见 [src/dom.rs:L434-L447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L434-L447)。注意它**不像 `HtmlTag::intern` 那样区分自定义元素**——属性名没有「自定义」概念，只要字符合法即可。

合法字符的黑名单定义在 `charsets.rs`：

```rust
pub const fn is_valid_in_attribute_name(c: char) -> bool {
    match c {
        '\0' | ' ' | '"' | '\'' | '>' | '/' | '=' => false,
        c if is_whatwg_control_char(c) => false,
        c if is_whatwg_non_char(c) => false,
        _ => true,
    }
}
```

见 [src/charsets.rs:L9-L19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L9-L19)。禁用的都是 HTML 语法分隔符：空格和 `/`、`>` 用于分隔标签与属性，`=` 用于分隔名和值，引号用于包裹值，`=` 之后这些字符出现会破坏解析。

编译期入口 `constant` 用手写的 `while` 循环逐字节校验（因为 `const fn` 里不能调迭代器），见 [src/dom.rs:L449-L473](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L449-L473)。

`HtmlAttr` 还提供 `cast!`，让它和 Typst 的 `Str` 类型互转，这是 `html.elem(attrs: (href: "..."))` 能用字符串 key 的关键：

```rust
cast! {
    HtmlAttr,
    self => self.0.resolve().as_str().into_value(),
    v: Str => Self::intern(&v)?,
}
```

见 [src/dom.rs:L498-L502](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L498-L502)。`Str => HtmlAttr` 这条方向会触发上面 `intern` 的校验，所以用户写错属性名会立刻得到清晰的报错。

#### 4.1.4 代码实践

**目标**：验证属性名与标签名字符规则的差异。

**操作步骤（源码阅读型实践）**：

1. 打开 `src/charsets.rs`，对比 `is_valid_in_tag_name` 与 `is_valid_in_attribute_name`。
2. 对下面四个候选属性名，分别判断 `HtmlAttr::intern` 与 `HtmlTag::intern` 是否接受：
   - `data-lang`
   - `aria_level`
   - `Class`（含大写）
   - `自定义`

**需要观察的现象 / 预期结果**：

| 候选 | 作为 `HtmlAttr`（属性名） | 作为 `HtmlTag`（标签名） |
| --- | --- | --- |
| `data-lang` | ✅ 接受 | ✅ 接受（但被当作自定义元素，需满足额外规则） |
| `aria_level` | ✅ 接受（下划线合法） | ❌ 拒绝（下划线不在标签名白名单） |
| `Class` | ✅ 接受（大小写不限） | ✅ 接受（非自定义标签不限制大小写） |
| `自定义` | ✅ 接受（中文字母不在黑名单） | ❌ 拒绝（非 ASCII 字母数字） |

结论：属性名的规则显著比标签名宽松，二者**不能共用**同一个校验函数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_valid_in_attribute_name` 要禁掉 `=` 和空格，却允许 `:`、`-`、`_`？

> **参考答案**：`=` 和空格在 HTML 语法里有结构含义（`name="value"` 的分隔符），出现在名字里会让解析器无法区分「名字结束、值开始」；而 `:`（XML 命名空间）、`-`（如 `data-*`、`aria-*`）、`_` 没有语法歧义，是合法的属性名字符。

**练习 2**：`HtmlAttr::constant` 是 `const fn`，为什么它的校验循环要手写 `while i < bytes.len()` 而不是 `for c in string.chars()`？

> **参考答案**：`const fn`（常量上下文）对能调用的标准库方法有严格限制，`str::chars()` 这类迭代器在 const 上下文里不可用；手写字节循环 + `as_bytes()` 是 const 可用的写法。代价是它按字节校验，依赖 `is_ascii()` 守卫来正确处理多字节字符。

---

### 4.2 HtmlAttrs：属性列表的增删查改

#### 4.2.1 概念说明

一个元素往往有多个属性（`<td colspan="2" rowspan="3">`）。typst-html 用 `HtmlAttrs` 表示「一个元素的属性集合」。它的存储非常朴素：

```rust
pub struct HtmlAttrs(pub EcoVec<(HtmlAttr, EcoString)>);
```

见 [src/dom.rs:L362-L364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L362-L364)。它就是一个 `(属性名, 属性值)` 元组的 `EcoVec`（写时复制的小向量），保留**插入顺序**。注意它**不**强制去重——同一个 key 理论上可以出现多次；真正的去重发生在 `Fold` 合并阶段（见 4.3）。

#### 4.2.2 核心流程

`HtmlAttrs` 提供的操作：

| 方法 | 行为 |
| --- | --- |
| `new()` | 空列表（即 `Default`） |
| `push(attr, value)` | 追加到**末尾** |
| `push_front(attr, value)` | 插入到**开头** |
| `get(attr)` | 线性查找，返回**第一个**匹配值的不可变引用 |
| `get_mut(attr)` | 线性查找，返回可变引用（内部用 `make_mut()` 触发写时复制克隆） |

`get` 用 `find` 返回第一个匹配项，这与 HTML「同名属性取第一个」的浏览器行为一致。

#### 4.2.3 源码精读

`push` / `get` / `get_mut` 的实现都很直白：

```rust
pub fn push(&mut self, attr: HtmlAttr, value: impl Into<EcoString>) {
    self.0.push((attr, value.into()));
}

pub fn get(&self, attr: HtmlAttr) -> Option<&EcoString> {
    self.0.iter().find(|&&(k, _)| k == attr).map(|(_, v)| v)
}

pub fn get_mut(&mut self, attr: HtmlAttr) -> Option<&mut EcoString> {
    self.0.make_mut().iter_mut().find(|&&mut (k, _)| k == attr).map(|(_, v)| v)
}
```

见 [src/dom.rs:L366-L394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L366-L394)。

几个值得注意的细节：

- 比较都是 `k == attr`，即 `HtmlAttr` 之间的整数相等比较——这正是驻留带来的好处。
- `get_mut` 先调 `self.0.make_mut()`。因为 `EcoVec` 是写时复制的（背后类似 `Arc`），共享时必须先克隆才能拿到可变借用。`make_mut()` 负责这个「必要时克隆」的语义。
- 还有 `push_front`（[src/dom.rs:L377-L380](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L377-L380)）用 `insert(0, ...)`，供需要把某属性排到最前时使用（例如优先级敏感的场景）。

`HtmlAttrs` 也有 `cast!`，实现与 `Dict` 的双向互转——这是 `html.elem(attrs: (colspan: "2"))` 能用字典字面量的关键：

```rust
cast! {
    HtmlAttrs,
    self => self.0.into_iter()
        .map(|(key, value)| (key.resolve().as_str().into(), value.into_value()))
        .collect::<Dict>().into_value(),
    values: Dict => Self(values.into_iter().map(|(k, v)| {
        let attr = HtmlAttr::intern(&k)?;
        let value = v.cast::<EcoString>()?;
        Ok((attr, value))
    }).collect::<HintedStrResult<_>>()?),
}
```

见 [src/dom.rs:L412-L427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L412-L427)。

- **`Dict => HtmlAttrs`**：遍历字典，把每个 key 经 `HtmlAttr::intern` 校验（所以非法属性名在这里报错），把每个 value cast 成 `EcoString`（属性值必须是字符串）。
- **`HtmlAttrs => Dict`**：反方向，把属性名 `resolve` 回字符串作为 key。

#### 4.2.4 代码实践

**目标**：跟踪一个真实的 `HtmlAttrs` 构造过程——表格单元格的 `colspan`/`rowspan`。

**操作步骤（源码阅读型实践）**：

1. 打开 `src/rules.rs` 的 `show_cell`（[src/rules.rs:L669-L685](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L669-L685)）。
2. 阅读它如何先 `HtmlAttrs::new()`，再视情况 `attrs.push(attr::colspan, ...)` / `attrs.push(attr::rowspan, ...)`，最后 `.with_attrs(attrs)` 挂到元素上。

关键代码：

```rust
let mut attrs = HtmlAttrs::new();
if let Some(colspan) = span(cell.colspan.get(styles)) { attrs.push(attr::colspan, colspan); }
if let Some(rowspan) = span(cell.rowspan.get(styles)) { attrs.push(attr::rowspan, rowspan); }
HtmlElem::new(tag).with_body(Some(cell.clone().pack())).with_attrs(attrs).pack()
```

**需要观察的现象 / 预期结果**：

- 当单元格没有合并时，`attrs` 保持空，元素不产生 `colspan`/`rowspan`。
- 当只有横向合并时，列表里只有 `(colspan, "N")` 一个元素。
- 注意这里用的是 `HtmlElem`（Typst 元素层）的 `with_attrs`，而不是 `HtmlElement`（DOM 层）的方法。`with_attrs` 是 `#[elem]` 宏为 `attrs` 字段生成的标准 setter。

**待本地验证**：编译一个带合并单元格的 Typst 表格并导出 HTML，确认生成的 `<td>` 上确实带上了正确的 `colspan`/`rowspan`。

#### 4.2.5 小练习与答案

**练习 1**：如果同一个 `HtmlAttrs` 里被 `push` 了两次 `class`，`get(attr::class)` 会返回哪一个？后续编码会不会输出两个 `class`？

> **参考答案**：`get` 用 `find` 返回**第一个**。是否输出两个取决于编码阶段——单次 `push` 构造的列表本身不去重，所以理论上可能输出 `class="a" class="b"`。但在正常流程中，跨样式层的合并会经过 `Fold` 去重（见 4.3），单个 `HtmlAttrs` 内出现重名 key 一般是构造错误。浏览器对重名属性也是取第一个，所以即便出现也不会崩。

**练习 2**：为什么 `get_mut` 要先调用 `self.0.make_mut()`，而 `get` 不需要？

> **参考答案**：`get` 只读，直接借用 `&self.0` 即可；`get_mut` 要写，而 `EcoVec` 是写时复制的，可能与其他所有者共享底层缓冲，必须先用 `make_mut()` 在「共享时克隆、独占时直接借用」的语义下拿到可变句柄，否则会违反借用规则或破坏其它副本。

---

### 4.3 Fold：内外层同名属性的合并语义

#### 4.3.1 概念说明

`HtmlElem.attrs` 字段被标记为 `#[fold]`：

```rust
/// The element's HTML attributes.
#[fold]
pub attrs: HtmlAttrs,
```

见 [src/lib.rs:L70-L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L70-L72)。这意味着当属性来自多层样式（比如外层 `set` 规则给的默认属性 + 内层 `html.elem` 显式给的属性）时，Typst 不会简单覆盖，而是调用 `Fold::fold` 把它们合并成一个最终列表。

理解 `Fold` 的关键是一句方向约定。在 `typst-library` 里：

```rust
pub trait Fold {
    /// Fold this inner value with an outer folded value.
    fn fold(self, outer: Self) -> Self;
}
```

见 [typst-library/.../styles.rs:L891-L894](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L891-L894)。`self` 是**内层**（更靠近元素、更具体的取值），`outer` 是**外层**（更宽作用域的取值）。整体约定是「**内层优先，外层补缺**」——这与 Typst 的 `bool`（内层真则真）、`Option<T>`（内层有则用内层）等实现一致。

#### 4.3.2 核心流程

`HtmlAttrs` 的 `fold` 把这条约定翻译成「属性列表」的合并规则：

1. 保留**内层** `self` 的全部属性，顺序不变；
2. 遍历**外层** `outer` 的每个属性：如果内层**已经有**同名属性，跳过；否则把它追加到内层末尾。

因此对同名属性，**内层胜出**；外层只在「内层没给」的属性上起到补充作用。最终列表里每个属性名唯一（fold 完成了去重）。

#### 4.3.3 源码精读

```rust
impl Fold for HtmlAttrs {
    fn fold(mut self, outer: Self) -> Self {
        // TODO: We might want to use a data structure where this is more
        // efficient (while keeping small attribute lists efficient, too), but
        // for now, this is okay.
        self.0.reserve(outer.0.len());
        for pair in outer.0 {
            if !self.0.iter().any(|&(attr, _)| attr == pair.0) {
                self.0.push(pair);
            }
        }
        self
    }
}
```

见 [src/dom.rs:L397-L410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L397-L410)。逐行解读：

- `mut self`：拿到内层列表的可变句柄，最终结果以它为基础。
- `self.0.reserve(outer.0.len())`：一次性预留容量，避免循环里多次重分配（纯优化）。
- `for pair in outer.0`：遍历外层每一个 `(attr, value)`。
- `if !self.0.iter().any(|&(attr, _)| attr == pair.0)`：检查内层是否已存在同名 `attr`。`any` 是线性扫描，注释里的 TODO 正是指这个 O(n²) 行为——属性列表通常很短，所以暂时可接受。
- `self.0.push(pair)`：仅当内层缺这个 key 时才追加。
- 返回 `self`：合并后的列表，内层在前、补缺的外层在后。

把内层放在前面还有一个隐含好处：HTML 浏览器对重名属性「取第一个」，即便后续某处真的又加了同名属性，内层（更具体的意图）也会被浏览器采纳。fold 让这件事在数据层就先去重了，双保险。

另外，`Fold` 要求满足结合律：`fold(fold(a, b), c) == fold(a, fold(b, c))`（见 [styles.rs:L889-L890](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L889-L890) 的文档）。这个实现确实满足：无论按哪种顺序折叠多层样式，最终都是「保留最内层优先级 + 按层由内到外补缺」。

#### 4.3.4 代码实践

**目标**：当内层和外层对**同名属性**给出不同值时，预测并验证最终保留哪一个，逐行追踪 `fold` 说明理由。

**操作步骤（源码追踪型实践）**：

设内层 `inner` 来自元素自身直接给的属性，外层 `outer` 来自更宽的样式层：

```
inner = [(class, "inner-val"), (id, "main")]
outer = [(class, "outer-val"), (href, "#top")]
```

调用 `inner.fold(outer)`，按下表逐行走一遍：

| 步骤 | 处理的 `pair` | 内层已有同名？ | 动作 | 当前 `self.0` |
| --- | --- | --- | --- | --- |
| 初始 | — | — | — | `[(class,"inner-val"), (id,"main")]` |
| 1 | `(class,"outer-val")` | 是（`class` 已存在） | 跳过 | 不变 |
| 2 | `(href,"#top")` | 否 | `push` | `[(class,"inner-val"), (id,"main"), (href,"#top")]` |

**需要观察的现象 / 预期结果**：

- `class` 的内层值 `"inner-val"` **被保留**，外层的 `"outer-val"` **被丢弃**——内层胜出。
- `href` 是内层没有的，作为补充追加到末尾。
- `id`（内层独有）原样保留。

**结论（一句话）**：同名属性，**内层覆盖外层**；不同名属性，外层补到内层之后。理由在于 `if !self.0.iter().any(...)` 这一行——只要内层已有该 key，外层对应项就被这个 `if` 挡住、不会 `push`。

**待本地验证**：构造一个外层 `set` 给 `html.elem(attrs: (class: "outer"))`、内层 `html.elem("div", attrs: (class: "inner"))` 的最小文档，导出 HTML 后检查 `<div>` 的 `class` 是否为 `"inner"`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `fold` 实现里的 `!self.0.iter().any(...)` 改成无条件 `self.0.push(pair)`，会出现什么问题？

> **参考答案**：外层属性会被无条件追加，导致最终列表出现同名重复属性（如两个 `class`）。虽然浏览器取第一个，但内层不再「干净地胜出」，且语义上违背了「内层优先、外层补缺」的 fold 约定，也失去了自动去重能力。

**练习 2**：注释里提到想换一个更高效的数据结构。为什么 typst-html 目前仍用 `EcoVec` + 线性扫描？

> **参考答案**：单个 HTML 元素的属性通常很少（几个到十几个），线性扫描在小规模下常数因子极小，且 `EcoVec` 写时复制、缓存友好、内存紧凑。`HashMap`/`BTreeMap` 之类的结构在小数据量下反而更慢、更占内存。注释的 TODO 表明作者知道大规模下 O(n²) 是隐患，但当前规模下「先用简单结构」是合理的工程取舍。

**练习 3**：`Fold` 要求结合律。请说明 `HtmlAttrs::fold` 为何满足 `fold(fold(a,b),c) == fold(a,fold(b,c))`。

> **参考答案**：两种顺序的最终结果都是「以 a 为最高优先级，其次 b，最后 c，逐层补缺」。具体地：`fold(fold(a,b),c)` = a 后接 (b 中 a 没有的) 再接 (c 中 a、b 都没有的)；`fold(a,fold(b,c))` 同样等于 a 后接 (b 中 a 没有的) 再接 (c 中 a、b 都没有的)，二者逐项一致。

---

### 4.4 attr 常量模块：预定义属性名的零成本引用

#### 4.4.1 概念说明

在 Rust 代码里要给一个元素挂 `class` 属性，如果每次都写 `HtmlAttr::intern("class")`，既啰嗦又把校验推迟到运行期。`src/attr.rs` 用编译期常量把上百个标准属性名预先定义好：

```rust
pub const class: HtmlAttr = HtmlAttr::constant("class");
pub const href: HtmlAttr = HtmlAttr::constant("href");
pub const colspan: HtmlAttr = HtmlAttr::constant("colspan");
```

这样 `attr::class` 就是一个零成本的 `HtmlAttr`，写起来像普通名字、比较起来是一次整数比较、且校验在编译期就完成了。

#### 4.4.2 核心流程

- `attr.rs` 顶部用 `#![allow(non_upper_case_globals)]` 关掉「常量名应大写」的 lint，让 `attr::class` 这种小写名字成立，与 HTML 属性名一一对应、可读性极佳。
- 每个 `pub const` 都调 `HtmlAttr::constant`（`const fn`），所以这些都是真正的编译期常量，没有运行期开销。
- 文件末尾还有一个 `pub mod mathml` 子模块，集中定义 MathML 专用的属性（`display`、`scriptlevel`、`lspace` 等），见 [src/attr.rs:L199-L224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs#L199-L224)。
- 与 Rust 关键字同名的属性名（`as`、`async`、`for`、`loop`、`type` 等）用原始标识符 `r#as`、`r#for` 表示，见 [src/attr.rs:L63-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs#L63-L64) 等。

#### 4.4.3 源码精读

常量定义的开头几行展示了模式：

```rust
pub const abbr: HtmlAttr = HtmlAttr::constant("abbr");
pub const accept: HtmlAttr = HtmlAttr::constant("accept");
pub const class: HtmlAttr = HtmlAttr::constant("class");
pub const href: HtmlAttr = HtmlAttr::constant("href");
```

见 [src/attr.rs:L8-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs#L8-L195)（属性常量区段）。借助上一篇讲过的 `PicoStr` 机制，`HtmlAttr::constant("class")` 与 `HtmlAttr::intern("class")` 产生的句柄数值相等（运行期 `intern` 会先尝试复用编译期编码），所以「常量路径」与「用户输入路径」创建的同名属性可以直接用 `==` 比较。

这些常量在 show 规则里被大量使用。看标题映射的例子：当 Typst 标题级别超出 HTML 的 `<h1>`~`<h6>` 范围时，typst-html 用一个 `<div>` 加上 ARIA 角色来补救：

```rust
HtmlElem::new(tag::div)
    .with_body(Some(realized))
    .with_attr(attr::role, "heading")
    .with_attr(attr::aria_level, eco_format!("{}", level + 1))
    .pack()
```

见 [src/rules.rs:L251-L256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L251-L256)。这里 `attr::role` 和 `attr::aria_level` 直接以常量形式引用，语义一目了然。

另一个典型例子是链接，用 `with_optional_attr` 仅在有 `href` 时才挂属性：

```rust
Ok(HtmlElem::new(tag::a)
    .with_optional_attr(attr::href, href)
    .with_body(Some(elem.body.clone()))
    .pack())
```

见 [src/rules.rs:L196-L199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L196-L199)。`with_optional_attr` 定义在 [src/lib.rs:L117-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L117-L123)：当值为 `None` 时原样返回 `self`，省去手写 `if let`。

> 区分层级提醒：上面两处 `with_attr` / `with_optional_attr` 是 **`HtmlElem`**（Typst 元素，产出 `Content`）的方法，定义在 `lib.rs`；而 `dom.rs` 的 `HtmlElement::with_attr`（[src/dom.rs:L231-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L231-L234)）服务于**直接拼装最终 DOM 节点**。show 规则处在「构造 Typst 内容」阶段，所以用的是 `HtmlElem` 那一组。

#### 4.4.4 代码实践

**目标**：用 `attr::*` 常量手动构造一个带属性的元素，体会常量引用的可读性。

**操作步骤（源码阅读 + 思维实验）**：

1. 在 `src/attr.rs` 里找到 `role`、`aria_level`、`href`、`class` 四个常量的定义行。
2. 假设你要生成 `<a href="#top" class="btn">返回顶部</a>`，对照源码写出等价的 Rust 构造代码：

```rust
// 示例代码（非项目原有，仅作说明）
HtmlElem::new(tag::a)
    .with_attr(attr::href, "#top")
    .with_attr(attr::class, "btn")
    .with_body(Some(Content::text("返回顶部")))
    .pack()
```

**需要观察的现象 / 预期结果**：

- 由于 `attr::href` 等是编译期常量，这段代码不会触发任何运行期属性名校验；非法名字（如手写 `HtmlAttr::constant("bad name")`）会在**编译期 panic**。
- 与之相对，用户在 Typst 脚本里写 `html.elem("a", attrs: (href: "#top"))`，校验在运行期由 `cast!` 触发的 `intern` 完成。

**待本地验证**：在 `rules.rs` 里搜索 `attr::` 的所有用法，统计哪些常量被用到，体会「常量表」如何让 show 规则代码保持简洁。

#### 4.4.5 小练习与答案

**练习 1**：`attr.rs` 顶部为什么要有 `#![allow(non_upper_case_globals)]`？

> **参考答案**：Rust 默认要求常量名用大写蛇形（`CLASS`）。但这里希望常量名与 HTML 属性名一一对应（`attr::class`、`attr::href`），小写更可读、更不容易写错。这条 lint allow 正是为了允许这种小写常量命名风格。

**练习 2**：`r#for` 和 `attr::for` 在引用时有何区别？为什么要用 `r#for`？

> **参考答案**：`for` 是 Rust 关键字，不能直接作为标识符。用原始标识符 `r#for` 可以绕过关键字限制，使得 `attr::r#for` 这个常量在代码里能被引用，而它 `resolve()` 出来的字符串仍是正常的 HTML 属性名 `"for"`。

**练习 3**：为什么说 `attr::class` 与 `HtmlAttr::intern("class")` 创建的句柄「数值相等」？

> **参考答案**：`PicoStr` 的运行期 `intern` 会先尝试复用编译期 `constant` 已经注册的编码（详见上一篇 u2-l2 关于 `try_constant` 的讲解）。由于 `"class"` 已被 `attr::class` 在编译期注册，运行期再 `intern("class")` 会命中同一编码，两者内部 `PicoStr` 相等，因此 `==` 直接成立。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从用户字典到内部属性列表」的完整追踪。

**任务背景**：用户在 Typst 里写

```typ
#html.elem("a", attrs: (href: "#top", class: "btn"))[返回顶部]
```

请按下列顺序追踪并回答：

1. **字典 → `HtmlAttrs`**：`attrs: (...)` 这个 Typst 字典，是通过哪段 `cast!` 代码转成 `HtmlAttrs` 的？（提示：[src/dom.rs:L412-L427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L412-L427)）其中字典的 key `"href"`、`"class"` 分别经过哪两个函数变成 `HtmlAttr`？value 又被 cast 成什么类型？

2. **`#[fold]` 合并**：假设存在更外层给了 `attrs: (class: "default")`，最终 `class` 取哪个值？请用 4.3 的逐行追踪法说明（内层 `class:"btn"` vs 外层 `class:"default"`）。

3. **常量对照**：如果这段逻辑改在 Rust 的 show 规则里手写（类似 `LINK_RULE`），会用到 `attr.rs` 里的哪几个常量？为什么 show 规则偏好用常量而非 `HtmlAttr::intern`？

**参考要点**：

1. 经 `cast! { HtmlAttrs, ... values: Dict => ... }`。key 经 `HtmlAttr::intern(&k)?`（运行期校验），value 经 `v.cast::<EcoString>()?`。最终得到 `HtmlAttrs` 内部是 `[(href,"#top"), (class,"btn")]`（顺序取决于字典迭代顺序）。
2. 内层 `class:"btn"` 胜出，外层 `class:"default"` 被丢弃。因为 `fold` 里 `if !self.0.iter().any(|&(attr, _)| attr == pair.0)` 发现内层已有 `class`，外层这一项被跳过。
3. 会用到 `attr::href`、`attr::class`（以及标签 `tag::a`）。show 规则偏好常量是因为：零成本（编译期完成校验和驻留）、可读性好（`attr::href` 比 `HtmlAttr::intern("href").unwrap()` 清晰）、且不会在每次调用时重复校验。

## 6. 本讲小结

- `HtmlAttr(PicoStr)` 把属性名驻留成可 `Copy`、可 O(1) 比较的句柄，与 `HtmlTag` 完全对称。
- 属性名字符规则是**黑名单**（`charsets::is_valid_in_attribute_name`），比标签名的白名单宽松得多，二者不可共用。
- `HtmlAttrs` 是 `EcoVec<(HtmlAttr, EcoString)>`，提供 `push`/`push_front`/`get`/`get_mut`，保留插入顺序；`get` 取第一个匹配。
- `cast!` 让 `HtmlAttrs` 与 Typst `Dict` 双向互转，这是 `html.elem(attrs: (...))` 能用字典字面量的根基。
- `Fold for HtmlAttrs` 的语义是「**内层优先、外层补缺**」：同名属性内层胜出，外层只补内层没有的，合并后每个属性名唯一。
- `src/attr.rs` 用 `HtmlAttr::constant` 把上百个标准属性名预定义为编译期常量，配 `with_attr`/`with_optional_attr` 让 show 规则代码简洁、零成本、编译期校验。

## 7. 下一步学习建议

- 接下来建议阅读 **u2-l4（预定义标签常量与内容模型分类）**，看 `tag.rs` 如何用同样的常量手法组织标签名，并理解 `is_void`/`is_raw` 等内容模型分类——这些分类会在后续编码阶段影响属性和子节点的处理方式。
- 如果想立刻看到属性系统的「下游」，可以提前翻看 **u5-l1（DOM 到 HTML 字符串的编码）**，了解 `HtmlAttrs` 最终如何被序列化成 `key="value"` 文本、空值如何简写。
- 想深入 `Fold` 在样式链中如何被反复调用的读者，可回到 `typst-library` 的 `foundations/styles.rs` 阅读 `Fold` trait 及其多个标准实现（`bool`、`Option<T>`、`Stroke` 等），对照体会「内层优先、外层补缺」的一致约定。
