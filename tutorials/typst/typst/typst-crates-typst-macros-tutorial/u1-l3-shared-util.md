# 共享工具层 util.rs：文档、属性、命名与版本

## 1. 本讲目标

在前两讲里，我们已经知道 typst-macros 是一个过程宏 crate，七个公开宏（`func` / `ty` / `elem` / `scope` / `cast` / `derive_cast` / `time`）共享同一个「解析中间结构 → `quote!` 生成」的骨架。本讲要深入这块骨架的地基——`src/util.rs`。

读完本讲，你应当能够：

1. 理解 `documentation()` 如何把多行 `///` 文档注释拼成一个字符串，以及它对 `stringify!` 整数/浮点数的特殊支持。
2. 掌握属性操作的「取出 → 解析 → 校验」三步法：`has_attr` / `take_attr` / `parse_attr` / `validate_attrs`，以及它们背后的一组解析 helper（`parse_key_value` / `parse_flag` / `parse_string` / `parse_string_array` 等）。
3. 理解 `determine_name_and_title` 如何从 Rust 标识符推导出 Typst 的 kebab-case 名字与 Title Case 标题，并对「写了等于没写」的冗余配置报错。
4. 理解 `foundations` 简写、`kw` 自定义关键字、`Since` 版本三态、`BlockWithReturn` 与 `BareType` 这些被多个宏复用的解析/生成小工具。

一句话概括：**util.rs 是七个宏的公共工具箱**，本讲就是打开这个工具箱，逐件认识里面的工具。

## 2. 前置知识

- **Rust 属性（attribute）**：形如 `#[foo]`、`#[foo(bar)]`、`#[foo = bar]` 的注解。在 `syn` 里分别对应 `Meta::Path`、`Meta::List`、`Meta::NameValue`。本讲大量出现对属性这三类形态的判断。
- **文档注释 `///`**：Rust 把 `/// xxx` 实际编译成 `#[doc = "xxx"]` 属性。所以「提取文档」本质上就是「在属性列表里找 `doc` 属性」。
- **`quote!` 与 `ToTokens`**：把 Rust 语法树片段转成 `TokenStream` 的宏。一个类型只要实现了 `ToTokens`，就能在 `quote! { #x }` 里被直接插值。
- **`syn::Parse`**：一个让类型具备「从 `TokenStream` 解析出自身」能力的 trait。`parse_flag::<kw::scope>(input)?` 这种写法就是靠它驱动的。
- **前两讲内容**：`parse_macro_input!`、`to_compile_error`、`BoundaryStream`、`bail!` 宏等概念，本讲默认你已经熟悉。

> 关键连接：`lib.rs` 里有一行 `#[macro_use] mod util;`（见 [src/lib.rs:5-L6](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L5-L6)）。`#[macro_use]` 的作用是把 `util.rs` 里用 `macro_rules!` 定义的宏（也就是 `bail!`）导出给**之后声明的所有同级模块**（cast/elem/func/scope/time/ty）。这正是「共享工具层」能被七个宏共同使用的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | 本讲的全部主角。包含 `bail!` 宏，以及文档提取、属性操作、命名推导、`foundations` 简写、`kw`、`Since`、`BlockWithReturn`、`BareType`、`oneliner` 等工具。 |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) | 通过 `#[macro_use] mod util;` 把工具暴露给所有宏模块。 |
| [src/elem.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs) | `#[elem]` 宏，是 util 工具的「重度使用者」：`has_attr`/`parse_attr`/`determine_name_and_title`（带 trim）/`documentation`/`validate_attrs`。 |
| [src/func.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs) | `#[func]` 宏，参数解析里密集调用 `has_attr`/`parse_attr`/`documentation`/`validate_attrs`/`Since`。 |
| [src/ty.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs) | `#[ty]` 宏，`Meta::parse` 用 `parse_flag`/`parse_string`/`parse_key_value`/`parse_string_array` 组合出整套元数据解析。 |

## 4. 核心概念与源码讲解

### 4.1 文档提取：documentation 与 oneliner

#### 4.1.1 概念说明

Typst 在运行时会把这些原生函数/类型/元素的文档展示给用户（比如自动生成的文档站点、IDE 提示）。这些文档的来源就是 Rust 源码里的 `///` 注释。`documentation()` 的职责是：**从一组属性里，把所有 `#[doc = "..."]` 的内容提取出来，拼成一个字符串**。

它还有一个不那么显眼的能力：当文档里写了 `#[doc = stringify!(842)]` 这种把宏调用嵌进文档注释的形式时（典型场景是纸张尺寸这类需要把数值写进文档的场合），它能识别出 `stringify!` 并提取里面的整数/浮点数字面量。

`oneliner()` 则做相反方向的压缩：从一段完整文档里抽出「第一句话」，用作短摘要（比如 `#[doc = #oneliner]` 注入到生成的影子类型上）。

#### 4.1.2 核心流程

`documentation` 的处理流程：

1. 遍历所有属性。
2. 只关心 `doc` 这个 NameValue 属性（即 `#[doc = ...]`）。
3. 分两种值：
   - **字符串字面量**（普通注释）：去掉行首一个空格，追加一个换行。
   - **`stringify!(...)` 宏调用**：尝试把内部 token 解析成 `Lit`，只接受 `Int`/`Float`，把数字字符串追加上去。
4. 最后 `trim()` 去掉首尾空白，返回。

`oneliner` 的处理流程（取第一句）：

1. 以空行 `"\n\n"` 切出第一个段落。
2. 逐字符扫描，用括号深度 `depth` 跟踪 `()[]{}`，避免在括号内的句点误判为句末。
3. 遇到「句点 + 深度为 0 + 后续空白」即认为第一句结束（并特判 `e.g.` 不算结束）。
4. 把提取出的片段里的换行替换成空格，压成一行。

#### 4.1.3 源码精读

`documentation` 的主体——遍历属性、匹配 `doc`、分字符串与 `stringify!` 两支：

[src/util.rs:25-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L25-L59) — 这是 `documentation` 的完整实现。注意第 38 行 `full.strip_prefix(' ').unwrap_or(&full)`：Rust 的 `///` 注释会被编译器规范化，每行内容前通常带一个空格，这里只剥掉**一个**前导空格（而不是 `trim_start`），以保留缩进代码块的对齐。

`stringify!` 支持分支，注释里写明了为什么需要它：

[src/util.rs:41-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L41-L54) — 注释「The `stringify!` macro does not expand eagerly so we have some very basic support for int and float expressions here. This is e.g. used for paper sizes.」解释了：宏在文档注释里不会即时展开，所以这里手动解析 token，仅支持 int/float（如纸张尺寸）。

`oneliner` 的逐字符扫描与括号深度跟踪：

[src/util.rs:347-L366](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L347-L366) — 注意第 358 行 `!docs[..i].ends_with("e.g.")` 这个特判，是为了避免把「e.g.」里的句点当成句子结束。

真实调用点——`ty.rs` 里提取类型文档：

[src/ty.rs:70-L76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L70-L76) — `documentation(attrs)` 提取完整文档，随后 `oneliner(docs)` 在 [src/ty.rs:83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L83) 被用于生成短摘要，最终注入到生成的类型上（[src/ty.rs:123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L123) 的 `#[doc = #oneliner]`）。

#### 4.1.4 代码实践

**实践目标**：参照 `documentation()`，亲手实现一个迷你版 `extract_docs`，理解它「拼字符串 + 去行首一个空格」的本质。

**操作步骤**（这是一个**源码阅读 + 手写实现型**实践，无需运行整个 crate）：

1. 阅读上面的 [src/util.rs:25-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L25-L59)。
2. 在你自己的一个 Rust 项目里（或一个 `tests` 目录），写下下面这段**示例代码**（非项目原有代码）：

   ```rust
   // 示例代码：documentation() 的极简版，只处理普通 doc 注释
   fn extract_docs(attrs: &[syn::Attribute]) -> String {
       let mut doc = String::new();
       for attr in attrs {
           if let syn::Meta::NameValue(meta) = &attr.meta
               && meta.path.is_ident("doc")
               && let syn::Expr::Lit(syn::ExprLit { lit: syn::Lit::Str(s), .. }) = &meta.value
           {
               let full = s.value();
               // 只剥掉行首“一个”空格，保留缩进
               let line = full.strip_prefix(' ').unwrap_or(&full);
               doc.push_str(line);
               doc.push('\n');
           }
       }
       doc.trim().to_string()
   }
   ```

3. 给一段带多行 `///` 注释的函数，用 `extract_docs(&item.attrs)` 调用它。

**需要观察的现象**：
- 多行注释会被拼成多行字符串，每行行首的**一个**空格被去掉。
- 如果某行注释前有两个空格（比如缩进的代码块 `///   foo`），第二、三个空格会**被保留**——这正是 `strip_prefix(' ')`（只剥一个）而非 `trim_start()` 的用意。

**预期结果**：拿到的字符串与 Typst 文档站点里看到的段落一致。注意 `stringify!` 的整数/浮点支持在迷你版里被省略了——它只是 `documentation()` 的一个增量能力。

> 如果无法本地编译 `syn`，明确标注「待本地验证」即可，重点是理解「文档 = `doc` 属性的拼接」这一模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `documentation` 用 `strip_prefix(' ')` 而不是 `trim_start()`？  
**答案**：`trim_start()` 会剥掉**所有**前导空格，这会破坏文档里缩进代码块的对齐。`strip_prefix(' ')` 只剥掉 `///` 规范化时自动添加的那一个空格，保留用户故意写的额外缩进。

**练习 2**：`stringify!` 分支只支持 `Int`/`Float`，如果有人在文档里写 `#[doc = stringify!(true)]` 会怎样？  
**答案**：第 50 行的 `_ => None` 分支会命中，`value` 为 `None`，整个 `if let` 链不成立，这段内容会被**静默跳过**（不进文档）。所以 bool 等其它字面量不会被提取。

---

### 4.2 属性操作三件套：取出、解析、校验

#### 4.2.1 概念说明

Typst 的宏大量使用自定义属性来配置行为，比如 `#[variadic]`、`#[default(...)]`、`#[parse({ ... })]`、`#[named]` 等。util.rs 提供了一组围绕属性的小函数，组成一个清晰的「三步法」：

1. **取出（take）**：`take_attr` 从属性列表里**移除**并返回某个属性；`has_attr` 在它之上做「是否存在」的布尔判断。注意是「移除」——这很关键，因为取出之后剩下的属性列表要交给下一步校验。
2. **解析（parse）**：`parse_attr` 把取出的属性按三种 `Meta` 形态（`Path`/`List`/`NameValue`）分别处理，支持泛型化的目标类型 `T: Parse`。
3. **校验（validate）**：`validate_attrs` 检查「还剩什么属性没被消费」，凡是既不是 `doc` 也不是 `derive` 的，一律报「unrecognized attribute」。

此外还有一组解析 helper，专门用来解析**宏参数里**（即 `#[func(name = "x", scope)]` 这类括号内的元数据）的内容：`parse_key_value`、`parse_flag`、`parse_string`、`parse_string_array`、`parse_key_value_array`、`eat_comma`，以及内部的 `Array<T>` 和 `quote_option`。

#### 4.2.2 核心流程

属性的「取出 → 解析 → 校验」流水线（以 `elem.rs` 的 `parse_field` 为例）：

```text
字段上的所有属性 attrs
   │
   ├── has_attr(&mut attrs, "variadic")   ──► 布尔标志（同时把该属性从 attrs 移除）
   ├── has_attr(&mut attrs, "required")
   ├── parse_attr(&mut attrs, "parse")?   ──► Option<BlockWithReturn>（带参数的属性）
   ├── parse_attr::<Expr>(&mut attrs, "default")?
   │
   └── validate_attrs(&attrs)?            ──► 此时只剩 doc/derive，否则报错
```

宏参数（`Meta` 结构体）的解析流水线（以 `ty.rs` 为例）：

```text
parse_flag::<kw::scope>(input)?          ──► bool          （标志：有/无）
parse_string::<kw::name>(input)?         ──► Option<String>（name = "..."）
parse_key_value::<kw::since, Since>(input)? ──► Option<Since>
parse_string_array::<kw::keywords>(input)? ──► Vec<String> （keywords = ["a","b"]）
```

这些 helper 内部都遵循一个统一模式：**先 peek 关键字在不在，在就消费「关键字 + `=` + 值 + 可选逗号」**，不在就返回默认值。

#### 4.2.3 源码精读

`take_attr` ——按名字找到属性并 `remove`（关键：是移除，不是只读）：

[src/util.rs:82-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L82-L91) — 第 90 行 `attrs.remove(i)` 是「消费式取出」的核心。`has_attr`（[src/util.rs:61-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L61-L64)）只是对它做 `is_some()` 包装。

`parse_attr` ——泛型化的「三种 Meta 形态」分发：

[src/util.rs:66-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L66-L80) — 三分支：`Meta::Path`（裸标志）返回 `None`；`Meta::List`（带括号参数）调用 `list.parse_args()` 解析成 `T`；`Meta::NameValue`（`key = value` 形式）在这里被认为「not valid here」而报错。注意返回类型是 `Option<Option<T>>`：外层 `Option` 表示「属性存不存在」，内层 `Option` 表示「属性存在时里面有没有参数」。

`validate_attrs` ——只放行 `doc` 和 `derive`：

[src/util.rs:93-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L93-L102) — 遍历剩余属性，第 96 行 `!is_ident("doc") && !is_ident("derive")` 决定了只有这两类属性「免检」。

宏参数解析的基石 `parse_key_value`：

[src/util.rs:113-L126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L113-L126) — `parse_flag`、`parse_string`、`parse_string_array` 全都建立在它（和 `parse_key_value_array`）之上。第 117 行用 `input.peek(|_| K::default())` 做关键字探测——`K::default()` 是 syn 自定义关键字的惯用写法，用来生成一个「占位关键字 token」供 peek 判断。

真实调用点——`func.rs` 解析函数参数的属性：

[src/func.rs:234-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L234-L245) — 这里典型地展现了三步法：先用 `documentation(&attrs)` 提取文档，再用 `has_attr` 取出 `named`/`variadic`/`external` 标志，用 `parse_attr` 解析 `default`（并给默认值 `Default::default()`），最后 `validate_attrs(&attrs)?` 兜底。

#### 4.2.4 代码实践

**实践目标**：理解 `validate_attrs` 为什么**只**放行 `doc` 和 `derive` 这两类属性。这是本讲的核心思考题之一。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/elem.rs:208-L243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L208-L243)，观察 `parse_field` 的开头：它先把 `variadic`/`required`/`positional`/`fold`/`internal`/`external`/`ghost`/`synthesized` 这些属性用 `has_attr` **一个个取走**，再用 `parse_attr` 取走 `parse`/`default`。
2. 注意每一步都是对**同一个可变 `attrs`** 操作——`has_attr`/`parse_attr`/`take_attr` 会把匹配到的属性从 `attrs` 里**移除**。
3. 走到 `validate_attrs(&attrs)?` 时，所有 Typst 自定义属性都已被消费干净，正常情况下 `attrs` 里**只会剩下** `doc`（文档注释）和 `derive`（派生宏）。
4. 想一想：如果用户写了一个拼错的属性，比如 `#[requird]`（拼错 required），会发生什么？

**需要观察的现象与预期结果**：
- 由于 `requird` 不被任何 `has_attr`/`parse_attr` 匹配，它会一直留在 `attrs` 里，直到 `validate_attrs` 发现它既不是 `doc` 也不是 `derive`，于是触发 `bail!(ident, "unrecognized attribute: requird")`，在编译期报错。
- **这就是「只放行 doc 和 derive」的原因**：所有 Typst 自己定义的属性都是「主动消费型」——宏在解析时会用 `take`/`has`/`parse` 把它们一个个拿走；能「幸存」到 `validate_attrs` 阶段的，理应只剩下宏不该管的通用属性（文档注释、派生宏）。任何幸存的 Typst 属性都意味着「拼写错误或位置错误」，必须报错。
- `doc` 和 `derive` 不被消费，是因为它们是「对编译器/其它宏有意义」的通用属性，本宏不应擅自删除。

#### 4.2.5 小练习与答案

**练习 1**：`parse_attr` 的返回类型为什么是 `Result<Option<Option<T>>>`？两个 `Option` 各代表什么？  
**答案**：外层 `Option` 来自 `take_attr`——表示「这个属性存不存在」。内层 `Option` 来自 `match attr.meta`——`Meta::Path`（裸标志，无参数）映射为 `None`，`Meta::List`（有参数）映射为 `Some(解析结果)`。所以：外层 `None`＝没写这个属性；外层 `Some(None)`＝写了但没带参数；外层 `Some(Some(t))`＝写了且带了参数。

**练习 2**：如果用户在字段上同时写了 `#[required]` 和一个未知属性 `#[frob]`，哪个错误会先被报出？  
**答案**：`required` 会被 `has_attr` 正常消费，`frob` 不被消费而留到最后，由 `validate_attrs` 报 `unrecognized attribute: frob`。两者不会冲突——因为 `has_attr` 是按名字精确匹配的。

---

### 4.3 命名推导：determine_name_and_title

#### 4.3.1 概念说明

每个 Typst 函数/类型/元素都有两个面向用户的「名字」：

- **name**：kebab-case 的标识名，如 `text`、`page-break`、`strike`。这是用户在 Typst 代码里写出来的名字。
- **title**：Title Case 的展示名，如 `Text`、`Page Break`、`Strike`。用于文档、错误信息。

用户可以用 `#[func(name = "foo", title = "Foo")]` 显式指定，但大多数情况下不必指定——宏会从 Rust 标识符**自动推导**。`determine_name_and_title` 就是统一处理「显式指定 vs 自动推导」的逻辑，并附带一个贴心的「冗余检查」：如果你手写的值恰好等于自动推导的值，它会报错提示你「name was specified unnecessarily」，避免无意义的重复。

#### 4.3.2 核心流程

```text
输入：specified_name: Option<String>、specified_title: Option<String>、ident、trim

1. name 的推导：
   default = trim(ident.to_string()).to_kebab_case()
   若 specified_name == Some(default) → 报错「name specified unnecessarily」
   name = specified_name.unwrap_or(default)

2. title 的推导：
   default = name.to_title_case()
   若 specified_title == Some(default) → 报错「title specified unnecessarily」
   title = specified_title.unwrap_or(default)

返回 (name, title)
```

注意 `trim` 是个可选的字符串预处理函数：`elem.rs` 用它把标识符末尾的 `Elem` 去掉（`ParBreakElem` → `ParBreak` → `par-break`），而 `ty.rs`/`func.rs` 传 `None`（不预处理）。

#### 4.3.3 源码精读

`determine_name_and_title` 的完整实现：

[src/util.rs:169-L194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L169-L194) — 第 177 行 `trim.unwrap_or(|s| s)` 是「没有 trim 就用恒等函数」的惯用写法。第 178 行 `.to_kebab_case()` 和第 186 行 `.to_title_case()` 来自 `heck` crate。两个 `bail!`（第 180、188 行）是冗余校验。

`elem.rs` 带 `trim` 的调用——去掉 `Elem` 后缀：

[src/elem.rs:157-L162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L157-L162) — `Some(|base| base.trim_end_matches("Elem"))`，所以 Rust 里的 `struct TextElem` 会被推导成 name=`text`、title=`Text`。

`ty.rs` 不带 `trim` 的调用：

[src/ty.rs:72-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L72-L73) — 传 `None`，所以 `struct Str` 直接推导成 name=`str`、title=`Str`。

#### 4.3.4 代码实践

**实践目标**：手工推演 `determine_name_and_title` 的输出，理解 kebab/title 推导与冗余报错。

**操作步骤**（推演型实践）：

对下面几个「输入」，预测 `(name, title)` 或是否会报错：

| 输入 | `specified_name` | `specified_title` | `ident` | `trim` |
|------|------------------|-------------------|---------|--------|
| A | `None` | `None` | `PageBreak` | `None` |
| B | `None` | `None` | `ParBreakElem` | `Some(去掉 "Elem")` |
| C | `Some("page-break")` | `None` | `PageBreak` | `None` |

**需要观察的现象与预期结果**（待本地验证你可用 `heck` crate 在小程序里复现）：

- **A**：`default = "page-break".to_kebab_case()` = `"page-break"`；未指定 → name=`"page-break"`。title 默认 = `"page-break".to_title_case()` = `"Page Break"`。结果：`("page-break", "Page Break")`。
- **B**：先 trim → `"ParBreak"` → kebab → `"par-break"` → name=`"par-break"`；title = `"Par Break"`。结果：`("par-break", "Par Break")`。这正是元素命名「`XxxElem` 自动变成 `xxx`」的关键。
- **C**：`default` 推导出来恰好是 `"page-break"`，而你又显式写了 `Some("page-break")`——两者相等，第 179 行命中，**报错**「name was specified unnecessarily」。冗余检查在工作。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `title` 的默认值是从**最终 `name`** 推导（`name.to_title_case()`），而不是从 `ident` 推导？  
**答案**：因为 name 可能被用户显式覆盖。如果 title 从 ident 推导，就会出现「name 被改了但 title 没跟着改」的不一致。从最终 name 推导，保证 title 始终是 name 的 Title Case 形式。

**练习 2**：`elem.rs` 为什么需要 `trim` 去掉 `Elem` 后缀，而 `func.rs`/`ty.rs` 不需要？  
**答案**：Typst 约定元素结构体命名为 `XxxElem`（如 `TextElem`），但用户面对的名字不应带 `Elem`；而函数和类型本身不遵循这种命名约定，标识符就是要直接用，所以传 `None`。

---

### 4.4 代码生成辅助：foundations 简写与 kw 自定义关键字

#### 4.4.1 概念说明

这一组小工具服务于「代码生成」和「宏参数解析」两端：

- **`foundations`**：一个零字段的 struct，却实现了 `ToTokens`，在 `quote!` 里出现时会展开成 `::typst_library::foundations`。它是为了**少写这一长串路径**而存在的简写——你会看到生成的代码里到处是 `#foundations::NativeElement`、`#foundations::Settable` 这种写法。
- **`kw`**：一个模块，里面用 `syn::custom_keyword!` 定义了一组「自定义关键字」（`name`/`title`/`since`/`scope`/`cast`/`constructor`/`keywords`/`parent`/`ext`/`contextual`/`span`）。它们的用途是在解析宏参数时做**精确的关键字匹配**——`parse_flag::<kw::scope>(input)?` 表示「我期望在这里看到一个 `scope` 关键字」。

#### 4.4.2 核心流程

`foundations` 的展开：

```text
quote! { #foundations::NativeElement }
   ↓ ToTokens
::typst_library::foundations::NativeElement
```

`kw` 的使用：

```text
parse_flag::<kw::scope>(input)?      // 期望匹配关键字 scope
parse_string::<kw::name>(input)?     // 期望匹配 name = "..."
```

`syn::custom_keyword!(scope)` 宏会生成一个名为 `scope` 的类型，它实现了 `Parse` 和 `Token`，并且 `Default`。配合 util.rs 里 `input.peek(|_| K::default())` 这种写法，就能在解析流里探测「下一个 token 是不是这个关键字」。

#### 4.4.3 源码精读

`foundations` 简写的定义——一个 struct + 一个 `ToTokens` impl：

[src/util.rs:217-L225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L217-L225) — 第 218 行的 `#[expect(non_camel_case_types)]` 是为了压制「类型名 `foundations` 不符合 CamelCase」的编译警告——这里故意用小写，因为它在 `quote!` 里更像一个路径片段。注意它实现了 `ToTokens` 而非 `Parse`：它是用来**生成**代码的，不是用来**解析**的。

`kw` 模块——所有自定义关键字的集中定义：

[src/util.rs:270-L282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L270-L282) — 这一组关键字覆盖了所有宏会用到的「命名参数键」。新增一个宏配置项时，往往就要在这里加一个 `custom_keyword!`。

`foundations` 在生成代码里的真实使用（`elem.rs` 注册元素）：

[src/elem.rs:440-L445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L440-L445) — `unsafe impl #foundations::NativeElement for #ident` 展开后就是 `unsafe impl ::typst_library::foundations::NativeElement for ...`。`foundations` 简写让这段代码可读得多。

#### 4.4.4 代码实践

**实践目标**：理解 `foundations` 简写如何把 `quote!` 里的路径缩短，以及 `kw` 如何驱动参数解析。

**操作步骤**（源码阅读型实践）：

1. 在 [src/elem.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs) 里用搜索功能找 `#foundations::`，数一数它出现了多少次。
2. 想象一下：如果把所有 `#foundations::` 都替换回完整的 `::typst_library::foundations::`，代码会变得多啰嗦——这就是这个简写存在的价值。
3. 对照 [src/ty.rs:56-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L56-L66)，看 `kw::scope`/`kw::name`/`kw::title`/`kw::since`/`kw::keywords` 这五个关键字是如何分别绑定到 `parse_flag`/`parse_string`/`parse_key_value`/`parse_string_array` 上的——**同一个关键字可以搭配不同的 helper**，取决于它的值类型。

**预期结果**：你会清楚地看到「关键字（kw）＝ 参数的键」「helper（parse_*）＝ 值的解析方式」这两层正交的设计。

#### 4.4.5 小练习与答案

**练习 1**：`foundations` 为什么是 struct 而不是一个 `macro_rules!`？  
**答案**：因为它需要在 `quote!` 的 `#foundations::NativeElement` 这种位置作为**表达式/路径片段**被插值，这要求它实现 `ToTokens`。struct + `ToTokens` impl 是达成「插值时自动展开成固定路径」最直接的方式；用宏的话就无法在 `#` 插值位置使用。

**练习 2**：如果要让 `#[func]` 支持一个新的配置项 `reversible`（一个标志），你需要改 util.rs 里的哪一处？  
**答案**：在 `kw` 模块里加一行 `syn::custom_keyword!(reversible);`，然后在 `func.rs` 的 `Meta::parse` 里加 `reversible: parse_flag::<kw::reversible>(input)?`。

---

### 4.5 类型与元数据解析：BlockWithReturn、BareType 与 Since

#### 4.5.1 概念说明

这三件工具是「解析」而非「生成」用的，分别处理三种特殊输入：

- **`BlockWithReturn`**：解析形如 `#[parse({ 语句1; 语句2; 返回表达式 })]` 的属性——一串语句外加最后一个返回表达式。它把这种结构拆成 `prefix: Vec<Stmt>`（前面的语句）和 `expr: Stmt`（最后的返回）。元素字段的 `#[parse(...)]` 自定义解析逻辑就靠它。
- **`BareType`**：解析一个裸的 `type Name;` 声明（带分号、无定义体）。这是 `#[ty]` 宏支持「先声明类型、再由宏生成定义」时需要的输入形态。
- **`Since`**：表示某个功能「何时引入」。它有三个状态：`Forever`（0.1.0 之前就有）、`Version([major,minor,patch])`（某个具体版本引入）、`Unreleased`（尚未在任何正式版本发布）。它能从字符串解析（`"forever"`/`"unreleased"`/`"0.13.0"`），也能在 `quote!` 里展开成运行时 `foundations::Since` 枚举。

#### 4.5.2 核心流程

`BlockWithReturn` 的解析：

```text
输入 token 流： stmt1; stmt2; final_expr
   │ 用 syn::Block::parse_within 解析成 Vec<Stmt>
   │ pop() 出最后一个作为 expr
   └─► { prefix: [stmt1, stmt2], expr: final_expr }
        若一条语句都没有 → 报错 "expected at least one expression"
```

`Since` 的三态解析（`FromStr`）：

```text
"forever"      ──► Forever
"unreleased"   ──► Unreleased
"0.13.0"       ──► 按点分 3 段 → Version([0, 13, 0])
其它            ──► Err(())  （随后由 Parse impl 转成友好的编译错误）
```

`Since` 的代码生成（`ToTokens`）：

```text
Forever           ──► #foundations::Since::Forever
Version([a,b,c])  ──► #foundations::Since::Version([a, b, c])
Unreleased        ──► #foundations::Since::Unreleased
```

#### 4.5.3 源码精读

`BlockWithReturn` 的定义与解析：

[src/util.rs:227-L248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L227-L248) — 第 242 行 `syn::Block::parse_within(input)` 解析一串语句；第 243 行 `stmts.pop()` 把最后一条当作返回表达式；若为空则第 244 行报错。`elem.rs` 在 [src/elem.rs:640](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L640) 用 `if let Some(BlockWithReturn { prefix, expr }) = &field.parse` 来消费它。

`BareType` 的定义与解析：

[src/util.rs:250-L268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L250-L268) — 依次解析可选外层属性、`type` 关键字、标识符、分号。注意第 251 行的 `#[expect(dead_code)]`——某些字段目前未被读取，但保留在结构里以备将来或为了 `Parse` 的完整性。`ty.rs` 在 [src/ty.rs:14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L14) 用 `let mut bare: BareType;` 处理这种输入分支。

`Since` 三态枚举：

[src/util.rs:284-L292](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L284-L292) — `Forever`、`Version([u32; 3])`、`Unreleased` 三个变体，每个都有文档注释说明含义。

`Since` 的字符串解析 `FromStr`：

[src/util.rs:299-L318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L299-L318) — 第 307-312 行用 `splitn(3, '.')` 把 `"0.13.0"` 拆成三段 `u32`，组装成 `[major, minor, patch]`。

`Since` 作为宏参数的解析（`Parse`）与友好报错：

[src/util.rs:320-L332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L320-L332) — 先解析成 `LitStr`，再 `value().parse()` 走 `FromStr`；失败时第 324-329 行用 `bail!` 给出提示「use `"unreleased"` for an unreleased version」。

`Since` 的代码生成（`ToTokens`）：

[src/util.rs:334-L345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L334-L345) — 三个变体分别映射到运行时 `foundations::Since::*`，注意它复用了 `#foundations` 简写。

真实调用点——`func.rs` 与 `elem.rs` 都用 `parse_key_value::<kw::since, Since>(input)?` 解析版本（见 [src/func.rs:131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L131)、[src/elem.rs:145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L145)）。

#### 4.5.4 代码实践

**实践目标**：跟踪一个 `#[func(since = "0.13.0")]` 从字符串到生成代码的完整旅程。

**操作步骤**（源码阅读型实践）：

1. 假设用户写了 `#[func(since = "0.13.0")] fn foo() { ... }`。
2. 在 [src/func.rs:131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L131)，`parse_key_value::<kw::since, Since>(input)?` 会被调用：`kw::since` 匹配关键字 `since`，`Since` 决定值的解析方式。
3. 进入 [src/util.rs:320-L332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L320-L332)：先把 `"0.13.0"` 解析成 `LitStr`，再走 `FromStr`（[src/util.rs:307-L313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L307-L313)）得到 `Since::Version([0, 13, 0])`。
4. 在生成阶段，`Since::ToTokens`（[src/util.rs:334-L345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L334-L345)）把它展开成 `::typst_library::foundations::Since::Version([0, 13, 0])`，写进 `NativeFuncData`。

**需要观察的现象与预期结果**：
- 字符串 `"0.13.0"` 经历：`LitStr` → `FromStr` → `Since::Version` → `ToTokens` → 运行时枚举字面量。
- 若用户写错成 `since = "0.13"`（只有两段），`splitn(3,'.')` 仍会执行但 `&[major, minor, patch]` 的模式匹配会失败 → `Err(())` → `bail!` 报「invalid version; use `"unreleased"` for an unreleased version」（待本地验证确切报错文案）。

#### 4.5.5 小练习与答案

**练习 1**：`BlockWithReturn` 为什么要求「至少一条语句」？最后那条语句和前面的有什么不同？  
**答案**：因为它建模的是「一段逻辑 + 一个返回值」，最后一条语句是返回表达式（`expr`），前面的都是副作用语句（`prefix`）。如果没有语句，就没有返回值，无法构成这个模型，所以报「expected at least one expression」。

**练习 2**：`Since` 同时实现了 `Parse`、`FromStr`、`ToTokens` 三个 trait，各自在什么环节起作用？  
**答案**：`Parse` 在「从宏参数 token 流解析」时起作用（[src/util.rs:320-L332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L320-L332)）；`FromStr` 被 `Parse` 内部复用，负责「字符串 → 枚举」的核心逻辑（[src/util.rs:299-L318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L299-L318)）；`ToTokens` 在「生成代码」时起作用，把枚举展开成运行时字面量（[src/util.rs:334-L345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L334-L345)）。

## 5. 综合实践

把本讲的工具串起来，做一次「以读者视角模拟一个宏的元数据解析」。

**任务**：假设你要为 `#[elem]` 解析下面这个（简化版）元素定义的**头部信息**：

```rust
/// A box that places its children inline.
#[elem(since = "0.12.0", keywords = ["inline", "box"])]
pub struct BoxElem {
    #[required]
    body: Content,
}
```

请逐项回答，util.rs 的哪个工具负责它：

1. 提取 `/// A box that places its children inline.` 这段文档 → 用什么？
2. 解析 `since = "0.12.0"` → 涉及哪几个函数/类型？
3. 解析 `keywords = ["inline", "box"]` → 用什么？
4. 把 `BoxElem` 推导成 name=`box`、title=`Box` → 用什么？这里 `trim` 参数是什么？
5. 在 `body` 字段上，`#[required]` 怎么被消费？最后谁保证没有遗留的未知属性？

**参考答案**：

1. `documentation(&attrs)`（[src/util.rs:25-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L25-L59)）；顺带 `oneliner` 会从中抽出短摘要。
2. `parse_key_value::<kw::since, Since>(input)?` → `Since::Parse` → `Since::FromStr` → 得到 `Since::Version([0,12,0])`。
3. `parse_string_array::<kw::keywords>(input)?`（内部走 `parse_key_value_array` + `Array<LitStr>`）。
4. `determine_name_and_title(..., Some(|base| base.trim_end_matches("Elem")))`（[src/elem.rs:157-L162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L157-L162)），`trim` 把 `BoxElem` → `Box`。
5. `has_attr(&mut attrs, "required")` 消费它（[src/elem.rs:210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L210)）；`validate_attrs(&attrs)?`（[src/elem.rs:243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L243)）保证没有遗留未知属性。

完成这个练习，你就把 util.rs 的五大模块在「真实宏解析」里的协作关系串通了一遍。

## 6. 本讲小结

- **util.rs 是七个宏共享的工具箱**，靠 `lib.rs` 的 `#[macro_use] mod util;` 把 `bail!` 等暴露给所有模块。
- **`documentation()`** 把 `#[doc = ...]` 拼成字符串，`strip_prefix(' ')` 只剥一个空格以保留缩进；额外支持 `stringify!` 的 int/float；`oneliner()` 反向抽取第一句。
- **属性三步法**：`take_attr`（消费式取出）→ `has_attr`/`parse_attr`（判断/解析）→ `validate_attrs`（兜底校验，只放行 `doc` 和 `derive`，因为所有 Typst 自定义属性都应是「主动消费型」）。
- **宏参数解析 helper**（`parse_key_value`/`parse_flag`/`parse_string`/`parse_string_array`）+ **`kw` 自定义关键字**，组成「键 × 值类型」正交的解析矩阵。
- **`determine_name_and_title`** 用 `heck` 做 kebab/title 推导，支持 `trim` 预处理（元素去 `Elem` 后缀），并对冗余显式配置报错。
- **`foundations` 简写** + **`Since` 三态**（`Forever`/`Version`/`Unreleased`）+ **`BlockWithReturn`/`BareType`** 分别服务于「路径简写」「版本元数据」「自定义解析块/裸类型」三类需求。

## 7. 下一步学习建议

掌握了工具层之后，下一讲（u2-l1）将从**最简单**的 `#[time]` 宏入手，看它如何用 `Meta` 解析 + 语句注入完成「最简单的代码注入」。之后会依次进入 `#[ty]`、`cast!`、`#[derive(Cast)]`，把本讲学到的 `documentation`/`determine_name_and_title`/`Since`/`parse_*` 放到真实宏里检验。

建议继续阅读的源码顺序：

1. [src/time.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs) —— 最小宏，先建立「parse + create」的整体直觉。
2. [src/ty.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs) —— 看 `Meta::parse` 如何把本讲的 `parse_flag`/`parse_string`/`parse_key_value`/`parse_string_array` 组合成完整的元数据解析。
3. 重读本讲的 [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs)，带着真实宏的上下文，你会对这些工具有更立体的理解。
