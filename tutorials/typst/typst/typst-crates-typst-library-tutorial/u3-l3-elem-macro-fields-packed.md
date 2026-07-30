# 讲义 u3-l3：elem 宏、字段系统与 Packed

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `#[elem(...)]` 宏把一个普通 Rust `struct` 变成 Typst 元素时，到底生成了哪些代码（struct 本体、`NativeElement`、字段 trait、`Construct`/`Set`、`ContentVtable`）。
- 区分七类字段标注 `#[required]` / `#[default]` / `#[synthesized]` / `#[ghost]` / `#[fold]` / `#[parse]` / `#[external]` 的语义，并知道它们各自落到哪条「字段 trait」上。
- 理解样式化字段为什么被存储成 `Settable<E, I>`（一个 `Option`），以及它如何在「元素自带值」与「样式链值」之间回退。
- 掌握 `Packed<T>` 这一类型擦除包装：它如何与 `Content` 互转、如何通过 `Deref` 还原出具体类型 `T` 的字段，以及为什么大量 trait（如 `Synthesize`）写在 `Packed<HeadingElem>` 上而不是 `HeadingElem` 上。

本讲承接 u3-l2（`Element` / `NativeElement` / 能力 vtable），向下为 u4（样式系统的 `StyleChain` / `Fold` / `Resolve`）打基础。

## 2. 前置知识

阅读本讲前，请确认你已理解 u3-l1、u3-l2 的结论：

- **`Content` 与 `RawContent`**：`Content` 是面向用户的稳定外壳，`#[repr(transparent)]` 包裹底层手写的 `RawContent`（一个 `Arc<dyn Element>` 式的引用计数分配）。一切标记与函数调用的产物都是 `Content`。
- **`Element` / `ContentVtable`**：`Element` 是 `Copy` 的类型擦除句柄，只持一张指向 static `ContentVtable` 的指针，代表「元素类型」而非实例。`ContentVtable` 是 `#[repr(C)]` 的自造虚函数表，既装函数指针也装纯数据。
- **`NativeElement`**：Rust 侧的类型化源头（`const ELEM` + `pack`），强制元素实现 `Construct` 与 `Set`。

本讲要回答的核心问题是：**既然元素在运行期被擦除成 `Content`，那么「字段」在哪里？类型信息又如何被安全地取回来？**

几个术语先对齐：

- **字段（field）**：元素结构体里的一个命名成员，例如 `HeadingElem` 的 `level`、`body`。每个字段在被宏处理后都会拿到一个 `u8` 编号（字段 ID），用于在 vtable 的字段子表里索引。
- **属性（property）**：可被 `set` 规则配置的字段。它在「元素实例」与「样式链」两个地方都可能存在值，是本讲 4.3 的主角。
- **能力（capability）**：元素可选实现的一组 trait，如 `Locatable`、`Tagged`、`Synthesize`、`Refable`。能力的查询机制见 u3-l2，本讲只关注字段相关的内容。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-macros/src/elem.rs` | `#[elem]` 宏的实现：解析 struct、生成所有代码。**注意它属于兄弟 crate `typst-macros`，不在 `typst-library` 内**，但与本讲密不可分。 |
| `src/foundations/content/field.rs` | 字段的类型化抽象：`Field` 访问器、各类字段 trait（`RequiredField`/`SettableField`/`SettableProperty`/`SynthesizedField`/`ExternalField`）、`Settable` 存储、`FieldAccessError`。 |
| `src/foundations/content/packed.rs` | `Packed<T>`：类型擦除后的静态类型包装，本讲的另一半主角。 |
| `src/foundations/content/vtable.rs` | `ContentVtable` 与 `FieldVtable`：字段相关的函数指针与元数据。 |
| `src/foundations/content/element.rs` | `NativeElement` trait、`Construct`/`Set`/`Synthesize`/`ShowSet` 等 trait 定义。 |
| `src/foundations/content/mod.rs` | `Content` 的公开方法，包括 `new` / `to_packed` / `field` / `materialize`。 |
| `src/model/heading.rs` | 真实元素 `HeadingElem`，本讲主要示例。 |
| `src/text/mod.rs` | `TextElem`，集中展示了 `#[ghost]`/`#[fold]`/`#[parse]` 的用法。 |

> 说明：本讲引用的永久链接以 `typst-library` 为 base；`elem.rs` 属于 `typst-macros`，链接会显式指向 `crates/typst-macros/src/elem.rs`。

## 4. 核心概念与源码讲解

### 4.1 `elem` 宏：从一个 struct 到一个元素

#### 4.1.1 概念说明

Typst 的元素本质上是一段「带字段的数据 + 一张描述自己行为的 vtable」。如果我们手写，每个元素都要重复一大段模板代码：实现 `NativeElement`、为每个字段写 trait、构造 `ContentVtable`、写 `Construct`/`Set`……

`#[elem(...)]` 宏就是消除这些样板胶水的机器。你只声明一个普通 struct 和它的字段标注，宏在编译期替你生成全部机械代码。理解宏生成了什么，是理解「字段从哪来」的关键。

#### 4.1.2 核心流程

宏的入口把工作分成两步：**解析**（`parse`）与**生成**（`create`）。生成的产物可以粗略分为五块：

1. **改写后的 struct 本体**（`create_struct`）：字段类型会被替换——`#[required]` 字段保持原类型；`#[synthesized]` 字段被包成 `Option<T>`；普通可设置字段被换成 `Settable<Self, I>`；`#[ghost]` 字段则根本不出现在 struct 里。
2. **inherent impl**：`new()` 构造器、`with_x()` builder 方法，以及为每个字段生成一个 `const IDENT: Field<Self, I> = Field::new();` 常量（样式访问的「钥匙」）。
3. **`NativeElement` impl**：组装并擦除静态 `ContentVtable`。
4. **每个字段的 trait impl**（`create_field_impl`）：根据标注选择实现 `RequiredField` / `SettableField` / `SettableProperty` / `SynthesizedField` / `ExternalField` 之一。
5. **`Construct` / `Set` impl**（仅当元素没有自己实现时）：从 `Args` 解析参数、填充字段。

所有生成代码被包进一个匿名作用域 `const _: () = { ... };`，避免局部定义泄露到模块顶层。

#### 4.1.3 源码精读

先看宏的总装函数 [`create`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L249-L277)：它依次拼出 struct、各类 impl，并用 `const _: () = { ... }` 包裹——**这一行说明宏生成的全部实现对外不可见，调用者只看得到 struct 与它的公开方法**。

字段顺序有一个关键细节。解析阶段会把字段按 `internal` 排序，**然后**才分配编号：

```rust
// crates/typst-macros/src/elem.rs:171-174（示例引用，非教程新增）
fields.sort_by_key(|field| field.internal);
for (i, field) in fields.iter_mut().enumerate() {
    field.i = i as u8;
}
```

这意味着 `#[internal]` 字段会被排到编号末尾，**字段的 ID 由排序后的位置决定，而非源码书写顺序**。例如 `HeadingElem` 在源码里把 `numbers`（`#[internal]`）写在中间，但它在运行期的字段 ID 是靠后的。

再看 struct 本体的字段类型改写——这是理解「字段去哪了」的核心，见 [`create_field`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L298-L307)：

```rust
// crates/typst-macros/src/elem.rs:300-306（示例引用）
if field.required {
    quote! { #vis #ident: #ty }                          // 原类型
} else if field.synthesized {
    quote! { #vis #ident: ::std::option::Option<#ty> }   // 包成 Option<T>
} else {
    quote! { #vis #ident: #foundations::Settable<Self, #i> } // 换成 Settable
}
```

注意：`#[ghost]` 字段不在这个分支里，因为 `struct_fields()` 已经过滤掉了 ghost（见 [`elem.rs:64-66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L64-L66)）。也就是说 **ghost 字段在 struct 里没有存储位置，它只活在样式链里**。

最后，宏把元素的能力（`#[elem(Locatable, Tagged, ...)]` 里列出的 trait）转化成两类：内省类（`Locatable`/`Unqueriable`/`Tagged`）填进 `IntrospectionCapabilities` 布尔位；其余非禁止类填进 capability 查询表。详见 u3-l2，这里只需记住：**能力清单写在 `#[elem(...)]` 的括号里**。以 [`HeadingElem` 的能力声明](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L76-L86)为例：

```rust
// src/model/heading.rs:76-86（示例引用）
#[elem(
    since = "forever",
    Locatable, Tagged, Synthesize, Count,
    ShowSet, LocalName, Refable, Outlinable
)]
pub struct HeadingElem { ... }
```

#### 4.1.4 代码实践：阅读宏展开结果

1. **实践目标**：直观看到 `#[elem]` 到底生成了什么。
2. **操作步骤**：
   - 安装 nightly 工具链（`cargo +nightly ...` 需要它；若环境无 nightly，跳到「源码阅读型」分支）。
   - 在仓库根执行 `cargo +nightly rustc -p typst-library -- -Zunpretty=expanded 2>/dev/null | grep -A 80 "pub struct HeadingElem"`，或在你的 IDE 里对 `HeadingElem` 上的 `#[elem(...)]` 使用「展开宏」功能。
   - 若无法运行：直接阅读 [`create_native_elem_impl`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L385-L466)，对照下面的观察清单。
3. **需要观察的现象**：
   - 生成的 `HeadingElem` struct 里，`body`（required）仍是 `Content`，`numbers`（synthesized）变成了 `Option<EcoString>`，其余字段变成了 `Settable<HeadingElem, N>`。
   - 存在一个 `impl HeadingElem { pub const level: Field<HeadingElem, _> = Field::new(); ... }` 常量块。
   - 存在 `unsafe impl NativeElement for HeadingElem { const ELEM: Element = ... }`。
4. **预期结果**：你能在展开产物里逐条找到上述五块生成物。
5. **若无法运行命令**：标注「待本地验证」，改为源码阅读——在 `elem.rs` 中按 `create_*` 函数顺序通读一遍即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么宏生成的代码要用 `const _: () = { ... };` 包起来，而不是直接写在模块顶层？

> **答案**：宏内部会生成许多中间类型/impl（例如字段 trait impl、`Construct`/`Set` impl）。包进匿名作用域可以避免这些定义污染外部命名空间，调用者只能看到 `struct` 与它的公开方法。

**练习 2**：如果一个字段在源码里写在第 1 位但标注了 `#[internal]`，它的字段 ID 会是几？

> **答案**：因为 [`elem.rs:171`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L171) 先按 `internal` 排序再分配 ID，`#[internal]` 字段会被排到末尾，所以它的 ID 会是「非 internal 字段数量」及其之后的值，而不是 0。

### 4.2 字段标注体系：七种字段各不同

#### 4.2.1 概念说明

字段标注决定了一个字段「如何存储、如何取值、如何参与构造」。Typst 把字段分成几类，每类对应一个 trait，并由此生成不同的 `FieldVtable`（类型擦除的字段操作表）。理解这七类标注，就能预测任意字段在运行期的行为。

#### 4.2.2 核心流程

字段标注 → 字段 trait → `FieldVtable` 的对应关系如下表：

| 标注组合 | 存储 | trait | vtable 来源 | `has` 语义 |
| --- | --- | --- | --- | --- |
| `#[required]` | 原类型，存于 struct | `RequiredField` | `RequiredFieldData::vtable()` | 恒为 `true` |
| `#[required] #[variadic]` | 容器，存于 struct | `RequiredField` | `vtable_variadic()` | 恒为 `true` |
| `#[synthesized]` | `Option<T>`，存于 struct | `SynthesizedField` | `SynthesizedFieldData::vtable()` | `is_some()` |
| `#[external]` | 不存储，仅文档 | `ExternalField` | `ExternalFieldData::vtable()` | 恒为 `false` |
| `#[ghost]`（+ 可选 `#[default]`/`#[fold]`） | **不存于 struct**，只活在样式链 | `SettableProperty`（手写） | `SettablePropertyData::vtable()` | 恒为 `false` |
| 普通（可设置，可选 `#[default]`/`#[fold]`/`#[parse]`） | `Settable<E,I>`，存于 struct | `SettableField`（+ blanket 出 `SettableProperty`） | `SettableFieldData::vtable()` | `is_set()` |

每个 `FieldVtable` 都含一组字段：`name`/`docs`/`def_site`（元数据），`positional`/`required`/`variadic`/`settable`/`synthesized`（用于构造器参数信息与文档），以及若干函数指针：`has`、`get`、`get_with_styles`、`get_from_styles`、`materialize`、`eq`。

#### 4.2.3 源码精读

以 `HeadingElem` 的字段为例，覆盖 required / default / synthesized / internal 四类：

```rust
// src/model/heading.rs:106（示例引用）—— 普通可设置字段，无 default
pub level: Smart<NonZeroUsize>,

// src/model/heading.rs:115-116 —— 带 default 的可设置字段
#[default(NonZeroUsize::ONE)]
pub depth: NonZeroUsize,

// src/model/heading.rs:154-156 —— 内部 + 合成字段（先 None，后由 synthesize 填充）
#[internal]
#[synthesized]
pub numbers: EcoString,

// src/model/heading.rs:235-236 —— required 字段（标题正文）
#[required]
pub body: Content,
```

- `level` 没有任何标注 → 普通可设置字段，存储为 `Settable<HeadingElem, _>`，默认值取 `Default::default()`（`Smart` 的默认是 `Auto`）。
- `depth` 带了 `#[default(NonZeroUsize::ONE)]` → 默认值由宏注入。见 [`create_field_impl`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L477-L480)：若给了 `#[default(expr)]` 就用 `|| expr`，否则用 `Default::default`。
- `numbers` 是 `#[internal] #[synthesized]` → 既不出现在文档，也以 `Option<EcoString>` 存储，初始为 `None`，在 `Synthesize` 阶段被填上（见 4.2 后半段）。
- `body` 是 `#[required]` → 必须由构造器提供，`has` 恒真。

再看 required 字段的 vtable，见 [`RequiredFieldData::vtable`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L79-L102)。关键几行：

```rust
// src/foundations/content/field.rs:91-99（示例引用）
required: true,
settable: false,
default: None,                  // required 字段没有默认值
has: |_| true,                  // 永远「存在」
get: |elem| Some((E::FIELD.get)(elem).clone().into_value()),
```

对比 synthesized 字段的 vtable，见 [`SynthesizedFieldData::vtable`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L160-L186)：`has` 变成 `|elem| (E::FIELD.get)(elem).is_some()`，并且 `eq` 恒为 `true`（**合成字段不参与相等比较**，因为它们是「派生」出来的，比较它们会让两个语义相同的标题被判不等）。

`#[ghost]`、`#[fold]`、`#[parse]` 这三类在 `HeadingElem` 里不出现，集中在 `TextElem`。看 [`TextElem` 的几个 ghost 字段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L180-L294)：

```rust
// src/text/mod.rs:290-294（示例引用）—— ghost + fold + default + 自定义 parse
#[parse(args.named_or_find("size")?)]
#[fold]
#[default(TextSize(Abs::pt(11.0).into()))]
#[ghost]
pub size: TextSize,
```

ghost 字段的存储说明：宏的 `struct_fields()` 过滤掉 ghost（[`elem.rs:64-66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L64-L66)），所以 `TextElem` 的 struct 里根本没有 `size` 字段——它只在样式链里流动。这也解释了一条编译期校验：**若元素含公开 ghost 字段，就必须自己实现 `Construct`**，否则报错，见 [`elem.rs:176-183`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L176-L183)。原因很好理解：自动生成的 `Construct` 会试图把字段写进 struct，但 ghost 字段没有存储位置。

`#[parse(...)]` 的作用是覆盖默认的参数解析。默认解析逻辑见 [`create_field_parser`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L639-L656)：variadic 用 `args.all()`，required 用 `args.expect(name)`，位置可选用 `args.find()`，命名用 `args.named(name)`。一旦给了 `#[parse({ ... })]`，整段解析代码就被你写的块替换。上面 `size` 的 `#[parse(args.named_or_find("size")?)]` 让它能同时接受命名参数 `size:` 和一个位置长度。

最后看 `numbers` 这个合成字段如何被「填值」。在 [`impl Synthesize for Packed<HeadingElem>`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L286) 里：

```rust
// src/model/heading.rs:277-283（示例引用）
self.numbers = Some(numbers.plain_text());   // 合成字段被填上
let elem = self.as_mut();
elem.level.set(Smart::Custom(elem.resolve_level(styles)));
elem.supplement.set(Smart::Custom(Some(Supplement::Content(supplement))));
```

注意 `self.numbers`、`self.level`、`self.supplement` 这种「像普通字段一样」的访问，其实发生在 `&mut Packed<HeadingElem>` 上——这正是 4.4 要讲的 `Packed` 的 `Deref` 魔法。

#### 4.2.4 代码实践：给 HeadingElem 字段做分类标注

1. **实践目标**：把 `HeadingElem` 的每个字段归入上表的一类，并预测其 vtable 行为。
2. **操作步骤**：
   - 打开 [`src/model/heading.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L87-L237)，逐字段看标注。
   - 在一张表里列出：`level` / `depth` / `offset` / `numbering` / `numbers` / `supplement` / `outlined` / `bookmarked` / `hanging_indent` / `body` 各属于哪类、默认值是什么、是否参与相等比较。
3. **需要观察的现象**：`numbers`（synthesized）不参与相等比较；`body`（required）`has` 恒真且无默认值；其余多为带 `#[default]` 的普通可设置字段。
4. **预期结果**：得到一张 10 行的字段分类表，能对每个字段说出「存储类型、默认值来源、has 语义」。
5. **待本地验证**：可选——用 4.1.4 的展开方法，确认 `numbers` 在 struct 里是 `Option<EcoString>`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `#[synthesized]` 字段的 `eq` 函数恒返回 `true`？

> **答案**：合成字段（如 `HeadingElem::numbers`、解析后的 `level`）是由其它字段在 `Synthesize` 阶段派生出来的，属于「缓存/快照」性质。若让它参与相等比较，两个内容相同但尚未合成的标题会被判不等，破坏内容树的稳定性。代码见 [`field.rs:183-184`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L183-L184)。

**练习 2**：`TextElem` 有大量 `#[ghost]` 字段却没有报「cannot have public ghost fields and an auto-generated constructor」错误，为什么？

> **答案**：`TextElem` 自己实现了 `Construct`（`#text(...)` 返回的是带样式的正文而非新建元素），所以宏在 [`elem.rs:176-183`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L176-L183) 的校验里放行。该校验只在没有 `Construct` 能力时才触发。

### 4.3 `Settable` 与 `SettableProperty`：样式化字段的双面

#### 4.3.1 概念说明

普通可设置字段（既非 required，也非 ghost）有一种「双面性」：

- 一面在**元素实例**上：调用方可以直接在构造时塞一个值进去，例如 `#heading(outlined: false)`。
- 一面在**样式链**上：通过 `set` 规则配置，例如 `#set heading(numbering: "1.")`。

`Settable<E, I>` 就是元素实例那一面的存储——它本质是个 `Option<E::Type>`：`None` 表示「没在元素上直接设值，去问样式链」；`Some(v)` 表示「元素自带了一个值」。`SettableProperty` 则是「这个字段作为属性」的抽象，描述它如何在样式链里被读写、如何取默认值、是否需要折叠。

#### 4.3.2 核心流程

读取一个可设置字段 `f` 的值时（带样式链 `styles`），逻辑如下（对应 [`Settable::get_cloned`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L495-L507)）：

```text
若字段是 fold 字段：
    取样式链里的值 s = styles.get_cloned(Field)
    若元素自带值 v：
        返回 fold(v, s)        # 把元素值「折叠」进样式值
    否则：
        返回 s
否则（普通字段）：
    若元素自带值 v：返回 v
    否则：返回 styles.get_cloned(Field)   # 回退到样式链
```

对 fold 字段，可写成：

\[
\text{resolved}(f) =
\begin{cases}
\mathrm{fold}(v_{\text{elem}},\; v_{\text{styles}}) & \text{若元素自带 } v_{\text{elem}} \text{ 且字段可折叠}\\[4pt]
v_{\text{elem}} & \text{若元素自带值且不折叠}\\[4pt]
v_{\text{styles}} & \text{否则（回退样式链）}
\end{cases}
\]

其中 \( v_{\text{styles}} \) 本身已是样式链沿栈折叠/解析后的结果（详见 u4）。

默认值用 `OnceLock` 惰性缓存：第一次取默认值时算出来并存进静态 slot，之后直接 `clone`——避免反复构造昂贵对象，但对廉价类型（`needs_drop` 为假）则每次现算，省掉惰性初始化开销。见 [`SettableProperty::default`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L337-L350)。

样式访问的「钥匙」是 `Field<E, I>`：一个零大小的访问器，宏为每个字段生成 `const IDENT: Field<Self, I> = Field::new();`。要读样式就写 `styles.get_cloned::<E, I>(Field::new())` 或等价地 `styles.get(HeadingElem::level)`。

#### 4.3.3 源码精读

`Settable` 的定义极其简单——它就是一个带约束的 `Option`，见 [`field.rs:444-447`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L444-L447)：

```rust
// src/foundations/content/field.rs:444-447（示例引用）
#[derive(Copy, Clone, Hash)]
pub struct Settable<E: NativeElement, const I: u8>(Option<E::Type>)
where
    E: SettableProperty<I>;
```

`get_cloned` 是双面回退的核心，见 [`field.rs:495-507`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L495-L507)。注意第一分支处理 fold：它先取样式链值，再把元素自带的值折叠进去。

`SettableField` 与 `SettableProperty` 是两层关系。普通可设置字段实现 `SettableField`（描述「元素上怎么存」），然后通过一个 blanket impl 自动获得 `SettableProperty`（描述「作为属性怎么用」），见 [`field.rs:353-361`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L353-L361)：

```rust
// src/foundations/content/field.rs:353-361（示例引用）
impl<T, const I: u8> SettableProperty<I> for T
where
    T: SettableField<I>,
{
    type Type = <Self as SettableField<I>>::Type;
    const FIELD: SettablePropertyData<Self, I> =
        <Self as SettableField<I>>::FIELD.property;
}
```

而 `#[ghost]` 字段因为没有 `SettableField` 实现，需要**手动**实现 `SettableProperty`——这正是宏里 ghost 走 `create_field_impl` 单独分支的原因，见 [`elem.rs:536-551`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L536-L551)。

最后看「物化」（materialize）：在 show 规则执行前，编译器会把样式链的值「烘焙」进元素，让每个可设置字段都有确定值。普通可设置字段的 materialize 见 [`SettableFieldData::vtable` 里的 `materialize`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L314-L318)：仅当字段未设置时才从样式链填值；而 ghost 字段的 materialize 是空操作（它没有元素侧存储可填）。公开入口 [`Content::materialize`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L232-L236) 遍历所有字段调用它。

#### 4.3.4 代码实践：追踪一个字段的取值回退

1. **实践目标**：理解 `Settable::get_cloned` 的双面回退。
2. **操作步骤**：
   - 阅读 [`HeadingElem::resolve_level`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L240-L245)：它调用 `self.level.get(styles)`。
   - 思考两种来源：用户写 `#heading(level: 3)[...]`（元素自带值）vs `#set heading(level: 2)`（样式链值）。
3. **需要观察的现象**：`level` 是 `Smart<NonZeroUsize>`，默认 `Auto`。当 `get(styles)` 返回 `Auto` 时，`resolve_level` 回退到 `offset + depth` 计算。
4. **预期结果**：能口述「元素自带值优先于样式链值；都没有时用默认值」。
5. **待本地验证**：可选——构造一个 `HeadingElem`，不设 `level`，打印 `resolve_level` 在不同 `set heading(level: ..)` 下的结果。

#### 4.3.5 小练习与答案

**练习 1**：fold 字段为什么不能实现 `RefableProperty`（无法按引用取值）？

> **答案**：fold 字段的最终值是把「元素自带值」与「样式链值」相加/相乘得到的新值，并不直接存在于任何一处存储里，必须现算出一个新对象。因此只能 `get_cloned`，不能返回引用。宏在 [`elem.rs:530-534`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L530-L534) 仅在非 fold 时生成 `RefableProperty` impl。

**练习 2**：默认值的 `OnceLock` 缓存对 `bool` 这类廉价类型会生效吗？

> **答案**：不会。[`SettableProperty::default`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L340-L344) 先判断 `needs_drop::<T>()`：`bool` 不需要 drop，属于廉价类型，直接每次调用 `(Self::FIELD.default)()` 现算，省掉惰性初始化开销；只有需要 drop 的昂贵类型才走缓存。

### 4.4 `Packed<T>`：类型擦除后的安全还原

#### 4.4.1 概念说明

元素一旦打包进 `Content`，就丢失了 Rust 侧的静态类型——`Content` 不知道里面装的是 `HeadingElem` 还是 `TextElem`。但很多地方我们又需要拿回具体类型去访问字段（比如 `self.body`、`self.resolve_level(styles)`）。

`Packed<T>` 就是「**已知内部一定是类型 `T` 的 `Content`**」的静态包装。它零成本（`repr(transparent)`），提供受检的向下转型，并通过 `Deref` 让你像访问普通结构体字段一样访问它。

#### 4.4.2 核心流程

`Packed<T>` 与 `Content` 的关系是一条双向通道：

```text
   T  ──Packed::new──▶  Packed<T>  ──pack──▶  Content
                                                  │
   T  ◀──unpack────  Packed<T>  ◀──from_owned─── ┤ (受检转型)
                       ▲                          │
                       │ from_ref / from_mut      │
                       └───── Option ─────────────┘
```

转型的安全性靠一个前置检查保证：先 `content.is::<T>()`（vtable 指针相等，u3-l2 讲过这是无动态分发的廉价比较），通过后才 `transmute`。因为 `Packed<T>` 是 `#[repr(transparent)]` 包裹 `Content` + `PhantomData`，两者内存布局完全一致，transmute 合法。

#### 4.4.3 源码精读

`Packed` 的定义见 [`packed.rs:12-18`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/packed.rs#L12-L18)：

```rust
// src/foundations/content/packed.rs:12-18（示例引用）
#[derive(Clone)]
#[repr(transparent)]
pub struct Packed<T: NativeElement>(
    /// Invariant: Must be of type `T`.
    Content,
    PhantomData<T>,
);
```

受检向下转型以 `from_ref` 为例，见 [`packed.rs:28-36`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/packed.rs#L28-L36)：先 `content.is::<T>()`，再 transmute。`from_mut` / `from_owned` 同理，见 [`packed.rs:39-60`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/packed.rs#L39-L60)。

最关键的是 `Deref`：它让你写 `self.body` 就能取到字段，见 [`packed.rs:121-128`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/packed.rs#L121-L128)：

```rust
// src/foundations/content/packed.rs:121-128（示例引用）
impl<T: NativeElement> Deref for Packed<T> {
    type Target = T;
    fn deref(&self) -> &Self::Target {
        // Safety: Packed<T> guarantees that the content is of element type `T`.
        unsafe { (self.0).0.data::<T>() }
    }
}
```

`(self.0).0` 拿到内部的 `RawContent`，再调 `data::<T>()` 取出 `&T`。`data::<T>()` 本身是 `unsafe`（它不检查类型），见 [`raw.rs:177-185`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L177-L185)——安全性正是由 `Packed` 的不变式「内部一定是 `T`」兜底。

公开的受检 API 在 `Content` 上，见 [`mod.rs:256-268`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L256-L268)：

```rust
// src/foundations/content/mod.rs:256-268（示例引用）
pub fn to_packed<T: NativeElement>(&self) -> Option<&Packed<T>> { Packed::from_ref(self) }
pub fn to_packed_mut<T: NativeElement>(&mut self) -> Option<&mut Packed<T>> { Packed::from_mut(self) }
pub fn into_packed<T: NativeElement>(self) -> Result<Packed<T>, Self> { Packed::from_owned(self) }
```

这就是实践任务里「`Packed<TextElem>` 与 `Content` 之间转换」的答案：

- **`T`/`Packed<T>` → `Content`**：`TextElem::new(text).pack()`（`pack` 来自 `NativeElement`，内部是 [`Content::new`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L88-L90)）；或 `Packed::new(elem).pack()`。
- **`Content` → `Packed<T>`**：`content.to_packed::<TextElem>()` 返回 `Option<&Packed<TextElem>>`，转型失败（不是文本元素）时得到 `None`。

最后回答一个关键问题：**为什么 `Synthesize`/`ShowSet`/`Count`/`Refable`/`Outlinable` 这些能力 impl 都写在 `Packed<HeadingElem>` 上，而不是 `HeadingElem` 上？** 看 vtable 模块开头 [`vtable.rs:16-21`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L16-L21) 的说明：vtable 的所有方法接收的都是 `Packed<E>`，因为运行期拿到的就是擦除后的 content，能力查询（`can::<C>()`）拼出的也是 `&Packed<E>` 的胖指针（见 u3-l2 与 [`elem.rs:683-693`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L683-L693) 的 `dangling as *const dyn capability`）。因此能力 trait 必须以 `Packed<E>` 为 `Self`，才能被正确地以 trait 对象方式调用。例如 [`impl Synthesize for Packed<HeadingElem>`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L286) 与 [`impl ShowSet for Packed<HeadingElem>`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L288-L309)。

#### 4.4.4 代码实践：模拟 `Packed<TextElem>` ↔ `Content` 往返

1. **实践目标**：用源码阅读 + 写伪代码的方式，彻底厘清 `Packed` 与 `Content` 的互转。
2. **操作步骤**：
   - 阅读 `TextElem` 的 `text` 字段（required），见 [`text/mod.rs:857-859`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L857-L859)。
   - 写出下面这段伪代码（**示例代码**，非项目原有）：

     ```rust
     // 示例代码：演示互转，非 typst-library 原有代码
     use typst_library::foundations::{Content, NativeElement};
     use typst_library::text::TextElem;

     // 1) T -> Content（打包）
     let content: Content = TextElem::new("Hi".into()).pack();

     // 2) Content -> &Packed<TextElem>（受检向下转型）
     match content.to_packed::<TextElem>() {
         Some(packed) => {
             // 3) 通过 Deref 直接访问 required 字段 text
             let s: &str = &packed.text;
             assert_eq!(s, "Hi");
         }
         None => { /* 不是文本元素 */ }
     }

     // 4) Packed<T> -> Content（再打包回去）
     let content2: Content = TextElem::new("Hi".into()).pack();
     let packed = content2.to_packed::<TextElem>().unwrap();
     let _again: Content = packed.clone().pack();
     ```

   - 对照 [`Packed::pack`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/packed.rs#L62-L65) 与 [`Content::to_packed`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L256-L258)，确认每一步都有对应源码。
3. **需要观察的现象**：`packed.text` 能编译通过，是因为 `Packed<TextElem>: Deref<Target = TextElem>`，而 `text` 是 `TextElem` 的字段。
4. **预期结果**：能画出 `T ⇄ Packed<T> ⇄ Content` 的双向箭头，并指出每个箭头对应的函数名与是否受检。
5. **待本地验证**：上述伪代码需在能引用 `typst-library` 的环境里编译；若仅阅读源码，能口述转换链即可。

#### 4.4.5 小练习与答案

**练习 1**：`Packed<T>::from_ref` 里，转型前为什么要先调 `content.is::<T>()`？`is::<T>()` 内部是如何判断的？

> **答案**：`data::<T>()` 是 `unsafe` 且不检查类型的，必须先用 `is::<T>()` 确认内部确实是 `T` 才能安全 transmute。`is::<T>()` 比较的是「content 的元素 vtable 指针」与 `T::ELEM` 的指针是否相等（见 [`raw.rs:226-228`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L226-L228)），因为 vtable 由 `static` 变量支撑，指针相等即类型相等，无需动态分发。

**练习 2**：为什么 `Synthesize` 等 trait 实现在 `Packed<HeadingElem>` 而不是 `HeadingElem` 上？

> **答案**：运行期能力调用（`can::<C>()` 经 fat 指针拼出 `&dyn C`）以 `Packed<E>` 为对象类型，vtable 的方法签名也都接收 `&Packed<E>`（见 [`vtable.rs:16-21`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L16-L21)）。若 impl 写在 `HeadingElem` 上，就无法被拼成 trait 对象调用。

## 5. 综合实践

**任务**：以 `HeadingElem` 为标本，把本讲四个最小模块串起来，画出一张「字段从声明到运行期取值」的完整流转图。

请按以下步骤完成（纯源码阅读型，无需运行）：

1. **宏侧**：打开 [`HeadingElem`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L76-L237) 的定义，对照 [`elem.rs` 的 `create_field_impl`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L469-L572)，给 `body` / `numbers` / `level` 三个字段分别写出：宏会为它实现哪个字段 trait、生成哪种 `FieldVtable`。
2. **存储侧**：画出宏改写后 `HeadingElem` struct 的真实形态（哪些字段是原类型、哪些是 `Option<T>`、哪些是 `Settable<..>`）。
3. **样式侧**：以 `level` 字段为例，写出 `self.level.get(styles)` 的取值回退路径（元素自带值 → 样式链 → 默认值），并指出 [`resolve_level`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L240-L245) 如何使用结果。
4. **Packed 侧**：说明 [`impl Synthesize for Packed<HeadingElem>`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L286) 里 `self.numbers = Some(...)`、`elem.level.set(...)` 这几行，分别用到了 `Packed` 的哪个能力（`Deref` / `DerefMut` / `AsMut`）。

**验收标准**：能用自己的话讲清楚——「`#[elem]` 宏改写了字段存储 → 字段 trait 决定 vtable 行为 → `Settable` 在元素与样式链间回退 → `Packed` 让运行期安全取回类型化字段」这条主线。

## 6. 本讲小结

- `#[elem(...)]` 宏在编译期生成 struct 改写、`NativeElement`、字段 trait、`Construct`/`Set`、静态 `ContentVtable` 五块代码，并用 `const _: () = { .. }` 隔离。
- 字段标注分七类：`#[required]` 存原类型且必填；`#[default]` 注入默认值；`#[synthesized]` 存 `Option<T>` 且不参与相等比较；`#[ghost]` 不入 struct 只活样式链（故需自定义 `Construct`）；`#[fold]` 让取值折叠而非替换；`#[parse]` 覆盖参数解析；`#[external]` 仅存于文档。
- 每类标注对应一个字段 trait（`RequiredField`/`SettableField`/`SettableProperty`/`SynthesizedField`/`ExternalField`）与一种 `FieldVtable`；字段 ID 在按 `internal` 排序后分配。
- `Settable<E,I>` 是「元素侧 `Option`」，取值时优先元素自带值、回退样式链、再回退默认值；fold 字段会把元素值与样式链值合并；默认值对昂贵类型用 `OnceLock` 缓存。
- `Packed<T>` 是 `repr(transparent)` 的静态包装，靠 `is::<T>()` 受检后 transmute 与 `Content` 互转；通过 `Deref` 到 `T` 让你像访问普通字段一样访问元素字段。
- 能力 trait（`Synthesize`/`ShowSet`/…）之所以写在 `Packed<HeadingElem>` 而非 `HeadingElem` 上，是因为 vtable 与能力调用都以 `Packed<E>` 为对象类型。

## 7. 下一步学习建议

- **下一讲 u4-l1（样式系统）**：本讲的 `Settable::get_cloned` 多次提到「回退样式链」，u4 将深入 `Styles` / `StyleChain` 的栈式查询、`Fold` / `Resolve` trait 的完整语义，以及 `Smart::Auto` 默认机制。
- **延伸阅读**：
  - [`FieldVtable`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L330-L376) 全部字段，理解类型擦除后字段操作如何被统一调度。
  - 一个含 `#[fold]` 字段的完整生命周期：从 [`TextElem::size`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L290-L294) 到 u4 中 `TextSize::fold` 的乘法折叠。
  - u3-l4（`func` 宏与 `Args`）可与本讲对照，理解 `#[elem]` 与 `#[func]` 在参数解析上的异同。
