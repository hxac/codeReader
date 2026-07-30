# #[elem]（一）：字段模型与属性校验

## 1. 本讲目标

`#[elem]` 是 typst-macros 里最庞大、最复杂的宏。它把一个普通 Rust `struct` 翻译成 Typst 运行时能识别的「元素（Element）」。本讲是 `#[elem]` 系列的第一讲，**只聚焦「解析」阶段**——即宏如何读懂用户写的 struct 与字段属性，并把它整理成一份内部的字段模型。代码生成（生成 struct、构造方法、vtable、Construct/Set）留给后续三讲（u4-l2 ~ u4-l4）。

学完本讲，你应该能够：

1. 说出 `#[elem]` 共享的「解析 → 生成」骨架，并指出本讲落在哪一环。
2. 看懂 `Elem`、`Field`、`Meta` 三个中间数据结构，以及每个字段属性的含义。
3. 解释 `parse_field` 中 **variadic → required → positional** 的优先级推导链。
4. 区分 `real_fields` / `struct_fields` / `accessor_fields` / `construct_fields` / `set_fields` 这五个字段过滤器的语义。
5. 列出会触发 `bail!` 的字段组合（互斥校验规则），以及「公有 ghost 字段 + 自动构造」为何被禁止。

## 2. 前置知识

在进入本讲前，你需要熟悉以下已在前面讲义中建立的概念（本讲不再重复展开）：

- **过程宏的解析-生成骨架**（u1-l2）：`#[elem]` 是属性宏，签名是 `fn elem(stream, item) -> Result<TokenStream>`，`stream` 指 `#[elem(..)]` 括号里的参数，`item` 指被装饰的 `struct`。出错用 `bail!` 造带 span 的 `syn::Error`，入口转成编译错误，绝不 panic。
- **util 工具层**（u1-l3）：属性的「取出 `take_attr` / 是否存在 `has_attr` → 解析 `parse_attr` → 校验 `validate_attrs`」三步法；元数据解析 helper `parse_flag` / `parse_string` / `parse_key_value` / `parse_string_array` 与自定义关键字 `kw`；`determine_name_and_title` 的 kebab/title 推导；`foundations` 路径简写（展开为 `::typst_library::foundations`）。
- **Typst 元素是什么**：元素是内容（`Content`）的一种，如段落、标题、文本、图形。每个元素有一组**字段**，字段分两大类——
  - **必填字段（required）**：构造元素时必须给出，例如 `headings(level: 1)` 里的 `level`。
  - **可设置字段（settable）**：通过 `set` 规则或样式设置，有默认值，例如文本的 `fill`。

  `#[elem]` 的工作就是把这些字段声明翻译成运行时能查询、能构造、能 `set` 的注册信息。

如果你对上面任何一项还不清楚，建议先回看 u1-l2 和 u1-l3 再继续。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/elem.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs) | `#[elem]` 宏的全部实现：解析（`parse`/`parse_field`）、中间结构（`Elem`/`Field`/`Meta`）、字段分类方法、代码生成（本讲略读生成部分）。 |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | 共享工具层。本讲重点用其中的 `BlockWithReturn`（解析 `#[parse({ .. })]` 自定义块）以及属性三件套。 |

## 4. 核心概念与源码讲解

### 4.1 解析-生成骨架与元素元数据

#### 4.1.1 概念说明

`#[elem]` 装饰一个 `struct`，它和本系列前面的宏一样遵循 **「解析中间结构 → `quote!` 生成」** 流水线：

1. `parse`：把 `#[elem(..)]` 括号参数 + 被装饰的 `struct` 解析成一个 `Elem` 中间结构。
2. `create`：根据 `Elem` 生成结构体定义、各类 `impl`、字段 vtable 等。

本讲只讲第 1 步。第 2 步的产物虽然不属于本讲主题，但我们需要知道 `Elem` 里记录了哪些信息，才能理解第 1 步「为什么要采集这些数据」。

`Elem` 中间结构记录元素的「整体画像」：对外名字、标题、引入版本、是否有 `#[scope]`、搜索关键词、文档、可见性、能力（capabilities）列表，以及最重要的——一组 `Field`。元素级（`#[elem(..)]` 括号里）的元数据用一个独立的小结构 `Meta` 承载。

#### 4.1.2 核心流程

`elem` 入口的骨架非常简洁，先解析后生成：

```text
elem(stream, body)
  ├─ parse(stream, &body)  ──► Elem      （本讲重点）
  └─ create(&element)      ──► TokenStream（u4-l2 起）
```

`parse` 内部做四件事：

1. 解析 `Meta`（元素级元数据）。
2. 用 `determine_name_and_title` 推导 name / title（元素默认会去掉名字里的 `Elem` 后缀）。
3. 断言 struct 必须是命名字段（`struct Foo { .. }`），逐字段调用 `parse_field`，然后按 `internal` 排序并分配索引 `i`。
4. 做一个元素级校验：若有公有 ghost 字段且用户没声明 `Construct` 能力，则报错。

#### 4.1.3 源码精读

入口 `elem` 只有「解析 + 生成」两行，清晰展示流水线：

[src/elem.rs:L15-L18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L15-L18) — 属性宏入口，`stream` 是括号参数，`body` 是被装饰的 `ItemStruct`，先 `parse` 后 `create`。

`Elem` 结构体记录元素的全貌（节选关键字段）：

[src/elem.rs:L21-L42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L21-L42) — 元素名、标题、版本、是否带 `#[scope]`、关键词、文档、可见性、能力列表、字段列表。

元素级元数据 `Meta` 与它的 `Parse` 实现，能看到 `#[elem(..)]` 括号里支持哪些项：

[src/elem.rs:L130-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L130-L152) — `scope`（flag）、`name`（string）、`title`（string）、`since`（key=value，类型 `Since`）、`keywords`（string 数组），以及若干裸标识符作为 `capabilities`（如 `Locatable`、`Construct`）。

> 注意 `capabilities` 没有用任何 helper，而是直接 `Punctuated::<Ident, Token![,]>::parse_terminated`，因为它就是一连串 trait 名（标识符），按出现顺序原样收集。

`parse` 函数把上面几步串起来。重点看三处：名字推导、字段的排序与索引分配、元素级 ghost 校验：

[src/elem.rs:L155-L197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L155-L197) — 解析 `Meta`、推导 name/title、要求命名字段、逐字段 `parse_field`、排序并分配 `i`、ghost + 自动构造校验。

其中排序与索引分配这一段很关键，它决定了每个字段的 `i`：

```rust
fields.sort_by_key(|field| field.internal);
for (i, field) in fields.iter_mut().enumerate() {
    field.i = i as u8;
}
```

`sort_by_key(|f| f.internal)` 中 `internal` 是 `bool`，而 `false < true`，所以**非 internal 字段排前面、internal 字段排后面**，`i` 从 0 开始按此顺序分配。`i` 后面会被当作 const generic（如 `Settable<Self, #i>`）和 vtable 槽位，因此字段顺序与索引是元素注册的底层编号。

元素级 ghost 校验：

[src/elem.rs:L176-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L176-L183) — 若存在「公有（非 internal）ghost 字段」且用户未声明 `Construct` 能力，则报错。

这条规则的直觉是：自动生成的 `Construct` 实现不知道怎么填一个公有 ghost 字段（ghost 字段不出现在 struct 里，详见 4.4），所以要么把 ghost 字段标成 `internal`，要么由用户自己提供 `Construct`。

#### 4.1.4 代码实践

**实践目标**：搞清楚 `#[elem(..)]` 括号里到底能写什么、每项的类型是什么。

**操作步骤**：

1. 打开 [src/elem.rs:L139-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L139-L152) 的 `Meta::parse`。
2. 对照 util.rs 里的 helper，填一张表：

   | 括号项 | 用的 helper | 类型 | 能否缺省 |
   | --- | --- | --- | --- |
   | `scope` | `parse_flag::<kw::scope>` | `bool` | 是 |
   | `name` | `parse_string::<kw::name>` | `Option<String>` | 是 |
   | …（自己补完） | | | |

**需要观察的现象 / 预期结果**：`name` 与 `title` 都接受字符串字面量（不是标识符）；`since` 接受字符串形式的三段版本号（如 `"0.13.0"`）或 `"forever"` / `"unreleased"`；`capabilities` 是任意顺序的裸标识符。结论：括号里的项**顺序敏感**（helper 按固定顺序 peek），这与 `cast!`、`func!` 的元数据解析风格一致。

#### 4.1.5 小练习与答案

**练习 1**：`determine_name_and_title` 传入的 `trim` 闭包是 `Some(|base| base.trim_end_matches("Elem"))`。一个名为 `HeadingElem` 的 struct，最终推导出的 `name`（kebab）和 `title`（Title Case）分别是什么？

**答案**：先 `trim_end_matches("Elem")` 得到 `Heading`，再 kebab 得 `name = "heading"`；`title` 由 `name.to_title_case()` 得 `"Heading"`。

**练习 2**：为什么 `capabilities` 用裸标识符解析，而不是像 `keywords` 那样用字符串数组？

**答案**：`capabilities` 收集的是 **trait 名字**（如 `Locatable`、`Construct`），后续要作为 `dyn #capability` 等代码片段直接拼进生成代码，必须是合法的 Rust 标识符；而 `keywords` 是给搜索引擎/自动补全用的纯文本，允许任意字符串。

---

### 4.2 Field 数据结构：字段的全部属性

#### 4.2.1 概念说明

`Field` 是本讲的主角——它描述**单个字段**的全部信息。理解 `#[elem]` 的核心，就是理解 `Field` 上的每一个布尔标志从哪来、意味着什么、又如何决定字段的存储形态。

字段属性大致分四类：

1. **身份信息**：`i`（索引）、`ident`（Rust 名）、`with_ident`（`with_xxx`）、`vis`、`ty`、`name`（对外 kebab 名）、`docs`。
2. **位置/必填语义**：`positional`、`required`、`variadic`。
3. **行为修饰**：`fold`、`default`、`parse`。
4. **可见性/存在性**：`internal`、`external`、`ghost`、`synthesized`。

一个要点贯穿全讲：`positional`、`required`、`variadic` 这三个标志**并非各自独立**，而是通过一条优先级链派生的（详见 4.3）。很多字段属性是「推出来的事实」，而非用户直接写的。

#### 4.2.2 核心流程

字段属性最终会决定**字段在生成 struct 里的存储类型**。尽管生成代码属于 u4-l2，但这条「三选一」映射能帮你理解为什么要采集这些标志：

| 字段种类 | struct 里的存储类型 | 判定 |
| --- | --- | --- |
| 必填 | `#ident: #ty`（裸类型） | `required` |
| 合成 | `#ident: Option<#ty>` | `synthesized`（且非 required） |
| 可设置 | `#ident: Settable<Self, #i>` | 其余普通字段 |

可以用集合关系表达它们的互斥：一个字段最终落在「required / synthesized / 普通 settable」三种存储形态之一。

#### 4.2.3 源码精读

`Field` 结构体（节选）：

[src/elem.rs:L91-L127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L91-L127) — 每个 `Field` 的全部字段。注释里逐条说明了 `positional`、`required`、`variadic`、`fold`、`internal`、`external`、`ghost`、`synthesized`、`parse`、`default` 的含义。

注意两个非布尔字段：

- `parse: Option<BlockWithReturn>`：存放 `#[parse({ .. })]` 自定义解析块（见 4.5）。
- `default: Option<syn::Expr>`：存放 `#[default(..)]` 的表达式。

存储类型三选一的生成逻辑（本讲略读，仅用于说明标志的下游用途）：

[src/elem.rs:L298-L307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L298-L307) — `create_field`：required 用裸类型、synthesized 包 `Option`、其余用 `Settable<Self, #i>`，三者互斥分支。

#### 4.2.4 代码实践

**实践目标**：给定字段声明，预测它的存储类型。

**操作步骤**：对下面四个字段，写出 `Field` 的 `positional/required/variadic/synthesized` 取值，并预测 struct 里的存储类型（**示例代码**，非项目原代码）：

```rust
#[elem]
pub struct DemoElem {
    #[required] level: u8,
    #[default(0)] fill: Color,
    #[synthesized] ref_: Option<Url>,
    #[variadic] children: Vec<Content>,
}
```

**预期结果**（手推，待本地用 `cargo expand` 验证）：

| 字段 | positional | required | variadic | synthesized | 存储类型 |
| --- | --- | --- | --- | --- | --- |
| `level` | true | true | false | false | `level: u8` |
| `fill` | false | false | false | false | `fill: Settable<Self, 1>` |
| `ref_` | false | false | false | true | `ref_: Option<Url>` |
| `children` | true | true | true | false | `children: Vec<Content>` |

> 提示：`positional` / `required` 的推导见 4.3。`children` 因为 `#[variadic]`，会被链式推导为 required=true、positional=true，所以存储是裸 `Vec`。

#### 4.2.5 小练习与答案

**练习 1**：`with_ident` 是怎么来的？为什么需要单独存一份？

**答案**：在 `parse_field` 里用 `format_ident!("with_{ident}")` 生成（见 [src/elem.rs:L216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L216)）。单独存是为了在生成链式构造方法 `with_xxx` 时直接取用，不必每次重新拼。

**练习 2**：`i` 的类型为什么是 `u8`？这暗示了什么限制？

**答案**：`u8` 上限 255，暗示一个元素最多 255 个字段。同时 `i` 用作 const generic（`Settable<Self, #i>`），`u8` 在代码生成里足够紧凑。

---

### 4.3 parse_field：属性解析、优先级推导与互斥校验

#### 4.3.1 概念说明

`parse_field` 是把一个 `syn::Field`（用户写的字段）翻译成内部 `Field` 的核心函数。它做三件事：

1. **取出属性**：用 `has_attr` 逐个检查 `variadic` / `required` / `positional` / `fold` / `internal` / `external` / `ghost` / `synthesized`，用 `parse_attr` 取出 `parse` / `default`。
2. **优先级推导**：根据一条固定链，派生 `required` 与 `positional`。
3. **互斥校验**：拒绝语义上冲突的属性组合。

本模块最重要的概念是**优先级推导链**——它是理解「字段默认行为」的钥匙。

#### 4.3.2 核心流程

优先级推导只需三行，但含义深远：

```rust
let variadic = has_attr(&mut attrs, "variadic");
let required = has_attr(&mut attrs, "required") || variadic;
let positional = has_attr(&mut attrs, "positional") || required;
```

用「或」串起来，形成一条单向包含链：

\[
\text{variadic} \;\Rightarrow\; \text{required} \;\Rightarrow\; \text{positional}
\]

含义是：

- 一个 **variadic** 字段自动也是 **required**、也是 **positional**。
- 一个 **required** 字段自动也是 **positional**。
- 反过来不成立（positional 不必 required，required 不必 variadic）。

这条链直接影响 4.4 里 `create_field_parser` 的取参方式（虽然那是 u4-l4 的内容，但优先级在这里就定死了）：

```text
args.all()?        // variadic
args.expect(..)?   // required
args.find(..)?     // positional（可选的位置参数）
args.named(..)?    // 其余：命名参数
```

互斥校验则有两条规则，都集中在 `parse_field` 末尾，外加元素级的一条（见 4.1）。

#### 4.3.3 源码精读

`parse_field` 全貌：

[src/elem.rs:L199-L246](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L199-L246) — 取属性 → 推导标志 → 构造 `Field` → 互斥校验 → `validate_attrs` 兜底。

开头先做两道合法性检查：字段必须有名字、且名字不能是保留字 `label`：

[src/elem.rs:L200-L206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L200-L206) — `label` 被 Typst 运行时保留，用了直接报错 `invalid field name \`label\``。

优先级推导三行（本讲最关键的三行）：

[src/elem.rs:L209-L211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L209-L211) — `variadic` → `required` → `positional` 的链式推导。

`parse` 与 `default` 用 `parse_attr` 取出（注意 `parse_attr` 返回 `Option<Option<T>>`，`.flatten()` 压成 `Option<T>`）：

[src/elem.rs:L229-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L229-L230) — `parse` 解析为 `BlockWithReturn`（见 4.5），`default` 解析为任意 `syn::Expr`。

字段级互斥校验（两条规则）：

[src/elem.rs:L233-L241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L233-L241) — 规则一：`required && synthesized` ⇒ 报错 `required fields cannot be synthesized`；规则二：`(required || synthesized) && (default.is_some() || fold || ghost)` ⇒ 报错。

可以用两条集合规则概括：

\[
\text{required} \cap \text{synthesized} = \emptyset
\]

\[
(\text{required} \cup \text{synthesized}) \cap (\text{default} \cup \text{fold} \cup \text{ghost}) = \emptyset
\]

直觉解释：

- **required vs synthesized**：required 字段必须由调用方在构造时给出；synthesized 字段是构造过程中「合成」出来的（不来自参数）。两者对「值从哪来」的回答相反，自然互斥。
- **required/synthesized vs default/fold/ghost**：`default`（默认值）、`fold`（折叠累加）、`ghost`（不占存储的纯样式属性）都是**可设置字段**的修饰；而 required/synthesized 不是可设置字段，挂这些修饰没有意义。

最后是兜底的 `validate_attrs(&attrs)?`（util.rs）：

[src/util.rs:L94-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L94-L102) — 所有 Typst 自定义属性都已被前面的 `has_attr`/`parse_attr` 主动消费，幸存下来除 `doc`/`derive` 外的属性即为拼写错误，报 `unrecognized attribute`。

#### 4.3.4 代码实践

**实践目标**：逐字段标注标志，并指出哪些组合会触发 `bail!`。

**操作步骤**（**示例代码**）：

```rust
#[elem]
pub struct MixedElem {
    #[required] a: i64,
    #[synthesized] #[default(0)] b: i64,        // ?
    #[required] #[fold] c: i64,                  // ?
    #[ghost] #[internal] d: i64,
    #[variadic] #[named] e: Vec<i64>,            // ?
}
```

逐字段判断：

| 字段 | 触发 `bail!`？ | 规则 |
| --- | --- | --- |
| `a` | 否 | 纯 required，合法 |
| `b` | **是** | `synthesized && default.is_some()` 命中规则二 |
| `c` | **是** | `required && fold` 命中规则二 |
| `d` | 否 | ghost + internal 合法（且因 internal 不触发元素级 ghost 校验） |
| `e` | 否 | `#[variadic]` 把它推成 required=true，`#[named]` 是字段属性吗？—— 注意 `named` **不是** elem 支持的字段属性，会被 `validate_attrs` 报 `unrecognized attribute: named`（`named` 属于 `#[func]`，不是 `#[elem]`） |

**需要观察的现象 / 预期结果**：把上述 struct 喂给 `#[elem]`，应在 `b`、`c` 处分别得到 `required and synthesized fields cannot be default, fold, or ghost`，在 `e` 处得到 `unrecognized attribute: named`。**待本地验证**（可在 typst 仓库里临时加一个测试元素后 `cargo build` 观察）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `required = has_attr("required") || variadic`，而不是让用户必须同时写 `#[variadic] #[required]`？

**答案**：variadic 字段（如 `children`）语义上必然是「必填的位置参数序列」——它从剩余所有位置参数收集，没有「可选」一说。把这条规律内建进宏，省去用户重复标注，也避免「写了 `#[variadic]` 却忘写 `#[required]`」造成语义不一致。

**练习 2**：一个 `#[synthesized]` 字段能不能同时 `#[fold]`？为什么？

**答案**：不能。`synthesized && fold` 命中规则二。直觉上 `fold` 是「把多次 set 的值累加」，只对可设置字段有意义；synthesized 字段不由 set 规则控制，自然不能 fold。

---

### 4.4 字段分类方法：五重过滤器

#### 4.4.1 概念说明

`Elem` 上有五个字段过滤方法。它们是**嵌套的集合过滤器**，决定了每个字段参与哪些生成代码（struct 定义、访问器方法、`Construct`、`Set`）。理解这五个过滤器，就理解了字段属性如何「流向」不同的代码生成环节。

这是本讲的第二个重点——如果说 `parse_field` 负责「采集」，那五个过滤器就负责「分发」。

#### 4.4.2 核心流程

五个过滤器的嵌套关系（`⊂` 表示子集）：

```text
real_fields          = 所有非 external 字段
  └─ struct_fields   = real 且 非 ghost
       └─ accessor_fields  = struct 且 非 required

construct_fields     = real 且 (有 parse 块 或 (非 synthesized 且 非 internal))
  └─ set_fields      = construct 且 非 required
```

用一个表格说明每个过滤器的语义和它的「下游用途」：

| 过滤器 | 条件 | 决定什么 |
| --- | --- | --- |
| `real_fields` | `!external` | 字段真实存在（external 字段只是文档占位，不参与生成） |
| `struct_fields` | real + `!ghost` | 哪些字段写进生成的 struct（ghost 字段不占存储） |
| `accessor_fields` | struct + `!required` | 哪些字段生成 `with_xxx` / 访问器方法（required 字段的访问方式不同） |
| `construct_fields` | real + (`parse` 或 `!synthesized && !internal`) | `Construct` 实现里要解析哪些字段 |
| `set_fields` | construct + `!required` | `Set` 实现里要处理哪些字段（required 不能 set） |

特别注意 `construct_fields` 的特殊规则——它的注释解释了一个常用模式：

> 有 `#[parse]` 且是 `internal` 的字段被允许，因为「从输入解析数据并存进字段」是高频写法。

#### 4.4.3 源码精读

五个过滤器全部集中在 `impl Elem` 块里：

[src/elem.rs:L57-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L57-L88) — 五个过滤方法的定义。注意它们用 `filter` 链式复用，`real_fields` 是其他几个的公共基础。

逐个看关键条件：

- `real_fields`：[L59-L61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L59-L61) 过滤掉 `external`。
- `struct_fields`：[L64-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L64-L66) 在 real 上再过滤掉 `ghost`（ghost 不进 struct）。
- `accessor_fields`：[L69-L71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L69-L71) 在 struct 上再过滤掉 `required`。
- `construct_fields`：[L78-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L78-L82) 特殊规则——有 `parse` 块就收，否则要 `!synthesized && !internal`。
- `set_fields`：[L85-L87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L85-L87) 在 construct 上再过滤掉 `required`。

> 一个推论：`external` 字段只进文档和 vtable（`create_native_elem_impl` 会对它生成 `ExternalFieldData`），但**不进 struct、不进 construct/set**。这正符合「external = 文档占位，真实实现在别处」的定位。

#### 4.4.4 代码实践

**实践目标**：给定一组字段，列出每个过滤器包含哪些字段。

**操作步骤**：对下面五个字段（**示例代码**）：

```rust
#[elem]
pub struct FilterElem {
    #[required]            a: i64,
    #[default(0)]          b: i64,
    #[ghost] #[internal]   c: i64,
    #[synthesized]         d: i64,
    #[external]            e: i64,
}
```

填出每个过滤器包含的字段：

**预期结果**：

| 过滤器 | 包含字段 |
| --- | --- |
| `real_fields` | a, b, c, d（e 是 external，排除） |
| `struct_fields` | a, b, d（c 是 ghost，排除） |
| `accessor_fields` | b, d（再排除 required 的 a） |
| `construct_fields` | a, b（c 被 internal 排除且无 parse；d 被 synthesized 排除；e 早就被 real 排除） |
| `set_fields` | b（再排除 required 的 a） |

**需要观察的现象**：`c`（ghost+internal）既不在 struct 里，也不参与 construct/set，但它仍是 `real`，会拿到 `i` 索引并出现在 vtable 里；`e`（external）几乎只活在文档/vtable。**待本地验证**（可用 `cargo expand` 展开一个真实元素对照）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `construct_fields` 要单独放过「有 `#[parse]` 的 internal 字段」？

**答案**：因为「从输入解析数据、存进一个 internal 字段」是 Typst 里非常常见的模式——internal 字段不对外暴露，但构造时需要算出来存着。若一律排除 internal，这种模式就写不出来，所以加了 `field.parse.is_some()` 这个例外。

**练习 2**：一个 `#[ghost]` 字段会不会出现在 `struct_fields` 里？为什么？

**答案**：不会。`struct_fields` 在 `real_fields` 基础上 `filter(!field.ghost)`。ghost 字段的设计就是「不占 struct 存储、纯样式属性」，所以不进 struct 定义——这也呼应了 4.1 里「公有 ghost + 自动构造」会被禁止（因为 struct 里没有它，自动构造没法填）。

---

### 4.5 BlockWithReturn 与 `#[parse]` 自定义解析块

#### 4.5.1 概念说明

最后这个最小模块讲一个工具类型 `BlockWithReturn`。它住在 util.rs，服务 `#[elem]` 的 `#[parse({ .. })]` 字段属性——让用户**自定义某个字段如何从 `args` 解析**，覆盖宏的默认取参逻辑。

`#[parse({ .. })]` 的语法是一段「语句序列 + 末尾表达式」：

```ignore
#[parse({
    let raw = args.expect("raw text")?;
    RawElem::new(raw)
})]
body: Content,
```

即 `stmt; stmt; expr` 的形式，最后一个表达式是该字段的值。`BlockWithReturn` 就是用来解析这种结构的。

#### 4.5.2 核心流程

`BlockWithReturn::parse` 的逻辑：

1. 用 `syn::Block::parse_within` 解析整段语句序列。
2. `stmts.pop()` 取出最后一条作为返回表达式 `expr`。
3. 其余作为 `prefix`（前置语句）。

`prefix` 和 `expr` 在 u4-l4 的 `create_field_parser` 里被拼成 `let #ident = { #prefix; #expr }` 形式注入 `Construct`/`Set` 的方法体。本讲只需理解「它如何被解析成两段」。

#### 4.5.3 源码精读

`BlockWithReturn` 的定义与解析：

[src/util.rs:L235-L248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L235-L248) — `prefix: Vec<syn::Stmt>` + `expr: syn::Stmt`；`parse` 用 `Block::parse_within` 取出全部语句，再 `pop` 末尾作为 `expr`，空则报错 `expected at least one expression`。

字段里如何取出它：

[src/elem.rs:L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L229) — `parse: parse_attr(&mut attrs, "parse")?.flatten()`，`parse_attr` 把 `#[parse(..)]` 括号内容按 `BlockWithReturn` 解析。

下游如何消费（u4-l4 详讲，这里只点一下）：

[src/elem.rs:L640-L642](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L640-L642) — `create_field_parser` 若发现 `field.parse.is_some()`，直接返回 `(prefix, expr)`，跳过默认的 `args.all/expect/find/named` 分支。这就是「自定义解析覆盖默认取参」的入口。

#### 4.5.4 代码实践

**实践目标**：理解 `#[parse({ .. })]` 如何被切成 `prefix` + `expr`。

**操作步骤**：对下面字段（**示例代码**）：

```rust
#[parse({
    let raw: String = args.expect("text")?;
    raw.to_uppercase()
})]
text: String,
```

手动模拟 `BlockWithReturn::parse`：

- `Block::parse_within` 得到两条语句：`let raw ...;` 和 `raw.to_uppercase()`。
- `pop` 末尾 → `expr = raw.to_uppercase()`。
- 剩下 → `prefix = [let raw: String = args.expect("text")?;]`。

**预期结果**：`prefix` 有 1 条语句，`expr` 是末尾表达式。生成代码里会变成 `let text = { let raw ...; raw.to_uppercase() };`。

#### 4.5.5 小练习与答案

**练习 1**：如果 `#[parse({ })]` 括号里什么都不写，会怎样？

**答案**：`Block::parse_within` 返回空 `Vec`，`stmts.pop()` 返回 `None`，触发 `input.error("expected at least one expression")` 编译错误。即 `#[parse]` 块必须至少有一条返回表达式。

**练习 2**：`parse_attr` 返回 `Result<Option<Option<T>>>`，外层 `Option` 和内层 `Option` 分别表示什么？为什么 `parse_field` 里用 `.flatten()`？

**答案**：外层 `Option` 表示「属性存不存在」（不存在就是 `None`）；内层 `Option` 表示「属性存在但没有括号参数」（`#[parse]` 无参 vs `#[parse(..)]` 有参，对应 `Meta::Path` 分支返回 `None`）。`.flatten()` 把 `Option<Option<T>>` 压成 `Option<T>`——对 `parse` 而言，无参的 `#[parse]` 等同于没写（必须带 `{ .. }` 才有意义）。详见 [src/util.rs:L67-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L67-L80)。

## 5. 综合实践

把本讲的字段模型、优先级推导、互斥校验、五重过滤器串起来。

**任务**：设计一个含四个字段的元素 `DemoElem`，字段分别带 `#[required]`、`#[default]`（普通 settable）、`#[ghost] #[internal]`、`#[synthesized]`。完成下列三件事：

1. **写出 struct 声明**（**示例代码**）：

   ```rust
   #[elem]
   pub struct DemoElem {
       /// 必填字段。
       #[required]
       level: u8,

       /// 普通 settable，带默认值。
       #[default(Color::BLACK)]
       fill: Color,

       /// 内部 ghost：不占存储的纯样式属性。
       #[ghost]
       #[internal]
       secret: Option<Color>,

       /// 合成字段：构造时算出来，不来自参数。
       #[synthesized]
       checksum: u64,
   }
   ```

2. **逐字段标注标志**，并给出每个过滤器是否包含它：

   | 字段 | i（排序后） | positional | required | variadic | fold | ghost | internal | external | synthesized | real | struct | accessor | construct | set |
   | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
   | `level` | 0 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ |
   | `fill` | 1 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
   | `secret` | 3 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
   | `checksum` | 2 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |

   > `i` 的推导：非 internal 的 `level`/`fill`/`checksum` 排在前（按源码顺序 0/1/2），internal 的 `secret` 排最后（3）。
   > `accessor`：`checksum` 是 synthesized，仍属 `struct` 且非 required，所以**在** accessor 里。
   > `construct`：`checksum`(synthesized) 和 `secret`(internal 且无 parse) 都被排除。

3. **指出哪些组合会触发 `bail!`**，并改造上面的元素来逐一触发：

   - **required + synthesized**：把 `level` 同时加 `#[synthesized]` → 命中规则一 `required fields cannot be synthesized`。
   - **required + default/fold/ghost**：给 `level` 再加 `#[default(0)]` 或 `#[fold]` 或 `#[ghost]` → 命中规则二。
   - **synthesized + default/fold/ghost**：给 `checksum` 加 `#[default(0)]` → 命中规则二。
   - **公有 ghost + 无 Construct**：把 `secret` 的 `#[internal]` 去掉（变公有 ghost），且不在 `#[elem(..)]` 里声明 `Construct` 能力 → 命中 [src/elem.rs:L176-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L176-L183) 的元素级校验。

   **待本地验证**：在 typst 仓库 `crates/typst-library` 里临时新建上述元素，分别 `cargo build` 观察每条 `bail!` 输出的 span 与文案。

**预期收获**：完成本任务后，你应该能在看到任意 `#[elem]` 字段声明时，立刻在脑中填出它的全部标志与所属过滤器——这正是后续 u4-l2（结构体与构造方法生成）、u4-l3（vtable）、u4-l4（Construct/Set）的输入。

## 6. 本讲小结

- `#[elem]` 遵循共享的 **解析（`parse`）→ 生成（`create`）** 骨架；本讲只覆盖解析，它把 `#[elem(..)]` 元数据 + struct 字段整理成 `Elem` / `Field` 中间结构。
- `Field` 的 `positional`/`required`/`variadic` 不是独立配置，而是由一条链式推导：`variadic ⇒ required ⇒ positional`，这条链固定了字段取参的优先级。
- 字段属性有两类互斥校验：`required ∩ synthesized = ∅`，以及 `(required ∪ synthesized) ∩ (default ∪ fold ∪ ghost) = ∅`；元素级还有「公有 ghost 字段必须配 `Construct` 能力」。
- `Elem` 的五个字段过滤器（`real` ⊃ `struct` ⊃ `accessor`，`construct` ⊃ `set`）决定每个字段流向哪部分生成代码；`external` 几乎只活文档/vtable，`ghost` 不进 struct。
- `BlockWithReturn` 把 `#[parse({ stmt; stmt; expr })]` 切成 `prefix` + `expr`，是「自定义字段解析覆盖默认取参」的解析基础；它由 `parse_attr` 取出，存进 `Field::parse`。
- 字段经 `sort_by_key(internal)` 排序后分配 `i`（非 internal 在前、internal 在后），`i` 是后续 const generic 与 vtable 槽位的底层编号。

## 7. 下一步学习建议

本讲产出的 `Elem`/`Field` 模型是后续三讲的输入。建议接着学：

- **u4-l2 #[elem]（二）：结构体与构造方法生成** —— 看 `create` / `create_struct` / `create_field` 如何根据本讲的 `required`/`synthesized`/普通三种标志决定存储类型，以及 `new()` 与 `with_xxx` 链式构造器如何生成。
- **u4-l3 #[elem]（三）：NativeElement 与字段 vtable** —— 看 `i` 索引如何变成 `RequiredFieldData`/`SettableFieldData` 等 vtable 槽位，以及 `fold` 与 `RefableProperty` 的条件生成。
- **u4-l4 #[elem]（四）：Construct/Set 与能力系统** —— 看 `create_field_parser` 如何消费本讲的 `Field`（尤其是 `#[parse]` 的 `prefix`/`expr`），把字段解析成 `args.all/expect/find/named`。

建议同时翻开 `crates/typst-library` 里任意一个真实元素（如 `HeadingElem`、`TextElem`），对照本讲的字段模型，亲手标注它的每个字段——这是把「读懂模型」变成「会用模型」的最好练习。
