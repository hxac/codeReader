# 类型化属性类型系统深入

## 1. 本讲目标

u2-l5 讲清了 `html.div` 这类「类型化构造函数」是如何由 `typst-assets` 的规范数据批量**长出来**的，并在结尾留了一个扣子：`create_param_info` 与 `construct` 都调用了 `AttrType::convert(attr.ty)`，而「`AttrType` 的完整类型体系留待 u6-l3 展开」。本讲就来兑现这个承诺。

学完本讲，你应该能够：

- 说清 `AttrType` 这个枚举如何充当**属性类型的动态分派中心**：把规范里的静态类型描述（`data::Type`）翻译成一组可执行的「查询接口」（`input` / `castable` / `cast`），并按 `Presence / Native / Strings / Union / List` 五种变体分别处理。
- 解释 `IntoAttr` trait 与 `NativeType` 如何复用 Typst 标准的 `FromValue` 转换机制，只额外负责「把一个 Rust 值写成合法 HTML 属性字符串」这最后一步。
- 区分 HTML 里**形态各异的「布尔」编码**：`Presence`（存在即真、假则省略属性）与 `TrueFalseBool` / `YesNoBool` / `OnOffBool`（真假都写字面字符串 `"true"/"false"`、`"yes"/"no"`、`"on"/"off"`）的本质差别。
- 手动追踪 `ListType::cast`，说清楚它如何把一个 Typst 数组用空格或逗号拼接成属性字符串，并且**主动拒绝含有分隔符的数组项**——以及为什么必须拒绝。
- 理解 `Duration`、`Datetime` 以及 `IconSize` / `HorizontalDir` / `ImageCandidate` / `SourceSize` 等特殊类型如何按 WHATWG/HTML 规范格式序列化。

## 2. 前置知识

本讲是 u2-l5 的直接续篇，请先确认以下概念：

- **类型化 HTML API 的生成链**（u2-l5）：`define() → FUNCS → create_func_data → create_param_info → construct`。所有类型化函数共用同一个 `construct` 实现，差异只来自各元素捕获的规范数据 `data::ElemInfo`。本讲不再重复这条链，只放大其中「属性类型如何被解释」这一环。
- **`data` 与 `data::Type`**：u2-l5 约定 `use typst_assets::html as data;`。`data::ElemInfo` 描述一个元素，`data::Type` 描述其某个属性的类型。`data::Type` 是一个**递归枚举**（可嵌套 `Union` / `List`），权威定义在隔壁 crate `typst-assets`（不在本 crate 链接范围内），本讲只依据 `typed.rs` 如何消费它来讲解。
- **`HtmlAttrs` 与 `HtmlAttr::constant`**（u2-l3）：`construct` 最终把每个属性压成 `(HtmlAttr, EcoString)` 对存入 `HtmlAttrs`；`HtmlAttr::constant` 是编译期驻留属性名的 `const fn`。
- **`FromValue` / `Reflect` / `cast!` 宏**：Typst 标准的类型转换基础设施。`FromValue` 把一个动态 `Value` 转成具体 Rust 类型；`Reflect` 描述一个类型「接受什么样的输入」（`input()` 返回 `CastInfo`，用于生成参数签名与文档、「能否被转换」`castable()`）；`cast! { Ty, v: bool => Self(v) }` 宏为 `Ty` 自动生成「从 Typst `bool` 转换」的 `FromValue` 实现。
- **CSS 序列化 `ToCss`**（u4-l4）：`Color::to_css(())` 会把颜色按 CSS 文本输出，本讲的 `impl IntoAttr for Color` 直接复用它。

> 一句话定位：u2-l5 回答了「函数从哪来」，本讲回答「**当用户给某个属性传了一个值，这个值是怎么被校验、又被写成什么字符串的**」。整条路径是 `Value --FromValue--> Rust 类型 --into_attr--> EcoString`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/typed.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs) | **本讲唯一主角**：`AttrType` 枚举及其分派、`IntoAttr` trait、`NativeType`/`StringsType`/`UnionType`/`ListType` 四个内层类型、以及 `Duration`/`Datetime`/`IconSize`/`HorizontalDir`/`ImageCandidate`/`SourceSize` 等序列化实现 |
| `crates/typst-assets/src/…` | `data::Type` / `data::ElemInfo` / `data::ATTR_STRINGS` 的权威定义（隔壁 crate，仅供对照，不在链接范围内） |
| [`src/dom.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | 提供 `HtmlAttrs`、`HtmlAttr::constant`，是 `construct` 落地的容器 |
| [`src/encode.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs) | 属性空值简写（`Presence` 产出的 `""` 在此变成布尔属性），见 u5-l1 |
| [`src/css/encode.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs) | `Color::to_css` 的实现，本讲 `impl IntoAttr for Color` 直接复用，见 u4-l4 |

## 4. 核心概念与源码讲解

### 4.1 AttrType 与 NativeType：属性类型的动态分派中心

#### 4.1.1 概念说明

HTML 规范给每个属性规定了一个「类型」。比如 `contenteditable` 接受 `true`/`false`，`colspan` 接受非负整数，`class` 接受一串空格分隔的 token，`datetime` 接受一个符合规范的时刻字符串。`typst-assets` 把这些类型信息编码成一个递归枚举 `data::Type`，但它只是**数据**——一堆 inert 的描述，自己不会「做事」。

`typed.rs` 里的 `AttrType` 才是这些数据的**解释器**：它同样是一个枚举，但每个变体都附带「如何描述自己接受的输入」「如何判断一个值能否被接受」「如何把值转换并序列化成属性字符串」这三项能力。换句话说，`AttrType` 把「类型」从「描述」升级成了「可执行的行为」。

二者之间的桥梁是 `AttrType::convert(data::Type) -> AttrType`：它把规范侧的静态类型，逐变体翻译成执行侧的动态类型。这是本讲一切逻辑的入口。

#### 4.1.2 核心流程

`AttrType` 顶层有五个变体，正好对应 `data::Type` 的五大类：

```
data::Type（规范数据，静态）            AttrType（执行表示，动态）
─────────────────────────────          ──────────────────────────────
Presence                          ──>  Presence        布尔存在性：真→写出空值，假→省略属性
Int / Str / Color / Datetime ...  ──>  Native          由一个具体 Rust 类型 T: IntoAttr 支撑
Strings(start, end)               ──>  Strings         枚举：接受一组固定字符串字面量之一
Union(variants)                   ──>  Union           联合：接受若干类型中的任意一种
List(inner, sep, shorthand)       ──>  List            列表：用 sep 拼接数组（可带单值简写）
```

三个统一查询接口（都按变体分派）：

| 方法 | 返回 | 用途 |
| --- | --- | --- |
| `input()` | `CastInfo` | 描述「接受什么样的输入」，供 `create_param_info` 生成函数签名与文档（u2-l5） |
| `castable(&Value)` | `bool` | 判断一个动态值是否可被本类型接受 |
| `cast(Value)` | `Result<Option<EcoString>>` | 真正转换 + 序列化；`Some(s)` 表示写出 `s`，`None` 表示**不写出该属性** |

`cast` 返回 `Option` 是一个关键设计：`None` 不是错误，而是「这个属性应当被省略」的信号——`Presence` 在传 `false` 时就返回 `None`，从而让 `construct` 根本不把这个属性压进 `HtmlAttrs`。

#### 4.1.3 源码精读

`AttrType` 枚举与文档注释，五种变体一一对应上面五类：

[typed.rs:162-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L162-L171) —— `AttrType` 枚举定义；文档注释指向 `data::Type` 以了解各变体含义。

把规范类型翻译成执行类型的 `convert`，是一个 `const fn`，内部用 `match` 逐变体映射：

[typed.rs:176-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L176-L207) —— `AttrType::convert`。绝大多数规范类型经 `Self::of::<T>()` 落到 `Native` 变体；只有 `Strings` / `Union` / `List` 三类带子结构，分别构造 `StringsType` / `UnionType` / `ListType`。

注意 `convert` 是 `const fn`，`of` 也是 `const fn`（[typed.rs:211-213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L211-L213)）：这意味着整个翻译过程不分配、可在常量上下文求值；实际运行中它被 `UnionType::iter` / `ListType::inner` 递归调用，用于展开嵌套类型。

三个查询接口的分派，结构完全平行——按五种变体各走一条路：

[typed.rs:216-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L216-L248) —— `input` / `castable` / `cast`。`Presence` 直接用 `bool` 的 `Reflect` 能力；其余四种各自委托给内层类型。

支撑 `Native` 变体的是 `NativeType`——一个「三个函数指针」组成的微型虚表：

[typed.rs:385-404](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L385-L404) —— `NativeType` 持有 `input` / `cast` / `castable` 三个 `fn` 指针；`of::<T: IntoAttr>()` 泛型地组装它们。`cast` 闭包先把 `value.cast::<T>()`（走 `FromValue`），再把得到的 `T` 调 `into_attr()` 序列化。这就是 `Value → Rust 类型 → EcoString` 两段式的落点。

最后看 `AttrType::cast` 在 `construct` 里的实际用法——这是 u2-l5 留下的扣子被解开的地方：

[typed.rs:118-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L118-L160) —— `construct`。对每个命名参数：取出属性规范 `attr`，`AttrType::convert(attr.ty)` 得到执行类型，`ty.cast(value).at(span)` 转换；`Ok(Some(s))` 压进 `attrs`，`Ok(None)` 静默丢弃（属性被省略），`Err` 收集成诊断报错。`.at(span)` 保证类型错误能定位到用户源码。

#### 4.1.4 代码实践

1. **实践目标**：把 `AttrType` 在脑中建成一张「分派表」，确认所有类型最终都归约到五种变体之一。
2. **操作步骤**：
   - 打开 [`typed.rs` 的 `convert`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L176-L207)，把每个 `data::Type` 变体与它映射到的 `AttrType` 变体列成两列对照表。
   - 注意哪些 `data::Type` 变体共享同一个 Rust 支撑类型（如 `Int`/`NonNegativeInt`/`PositiveInt` 分别落到 `i64`/`u64`/`NonZeroU64`）。
3. **需要观察的现象**：除了 `Presence`，所有「标量」类型都走 `Self::of::<T>()` 即 `Native` 分支；只有 `Strings`/`Union`/`List` 携带子结构。
4. **预期结果**：你会得到一张 ~25 行的对照表，其中 `Native` 占绝大多数，印证「`Native` 是主力、其余四种是特化」。
5. 本实践为纯源码阅读，可完全确定，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`AttrType::cast` 为什么返回 `Option<EcoString>` 而不是直接 `EcoString`？`None` 由哪种变体、在什么情况下产生？

> **答案**：`None` 表示「这个属性不应被写出」。`Presence` 变体在用户传 `false` 时返回 `None`（见 `value.cast::<bool>().map(|b| b.then(EcoString::new))`），`construct` 据此跳过该属性，最终 HTML 里根本不出现它。

**练习 2**：`convert` 与 `of` 为什么都标成 `const fn`？这对运行时行为有什么影响？

> **答案**：`const fn` 允许在编译期求值且不分配堆内存；`of::<T>()` 只是把三个 `fn` 指针填进结构体，天然可常量求值。运行中 `convert` 被 `UnionType::iter` / `ListType::inner` 递归调用来展开嵌套类型，`const` 保证了这条递归路径零分配、低成本。

### 4.2 IntoAttr 与原生类型序列化

#### 4.2.1 概念说明

`Native` 变体背后的所有具体类型（`Str`、`i64`、`Color`、`Datetime`……）都实现一个共同 trait：`IntoAttr`。它只声明一个方法 `fn into_attr(self) -> EcoString`——「把自己写成合法的 HTML 属性字符串」。

`IntoAttr` 有一个关键的超类型 bound：`FromValue`。这把职责切得很干净：

- **`FromValue`**（Typst 标准）负责「这个动态 `Value` 能不能、以及如何变成 Rust 类型 `T`」——校验合法性、给出带 span 的错误。
- **`IntoAttr`**（typst-html 专用）只负责「拿到合法的 `T` 之后，怎么写成属性字符串」。

`NativeType::of::<T>()` 的 `cast` 闭包正是把这两步串起来：`value.cast::<T>()?` 走 `FromValue`，`this.into_attr()` 走 `IntoAttr`。于是「校验」复用了整个 Typst 类型系统的成熟实现，typst-html 只需补上「最后一公里的字符串化」。

#### 4.2.2 核心流程

```
用户传入 Value
   │
   ├── FromValue（标准）──> T            校验 + 转换，失败带 span 报错
   │
   └── IntoAttr（本 crate）──> EcoString  合法值的字符串化
```

各原生类型的 `into_attr` 多是平凡序列化：

| Rust 类型 | 序列化为 | 备注 |
| --- | --- | --- |
| `Str` | 原字符串 | 最常见 |
| `char` | 该字符 | `eco_format!("{self}")` |
| `i64` / `u64` / `NonZeroI64` / `NonZeroU64` | 十进制整数 | 走 `Display` |
| `f64` | Rust 浮点 `Display` | 注释指出 HTML 浮点字面量与之兼容 |
| `PositiveF64` | 内部 `f64` | 先 `.get()` 再走 `f64` |
| `Color` | CSS 颜色文本 | 复用 `to_css(())`（见 u4-l4） |

#### 4.2.3 源码精读

`IntoAttr` trait 的定义，注意 `FromValue` 超类型：

[typed.rs:406-410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L406-L410) —— `pub trait IntoAttr: FromValue`，唯一方法 `into_attr(self) -> EcoString`。

最简单的实现 `Str`，直接转交：

[typed.rs:412-416](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L412-L416) —— `impl IntoAttr for Str`，`self.into()`。

整数族四个实现结构一致，以 `i64` 为例：

[typed.rs:512-534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L512-L534) —— `i64` / `u64` / `NonZeroI64` / `NonZeroU64`，都用 `eco_format!("{self}")`。其中 `NonNegativeInt`→`u64`、`PositiveInt`→`NonZeroU64` 由 `convert` 落点（`NonZeroI64` 的实现当前未被某个 `data::Type` 变体直接引用，留作完备性储备）。

浮点的注释点明了「为何直接用 Rust 的 `Display`」：

[typed.rs:536-548](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L536-L548) —— `f64` 与 `PositiveF64`。注释：HTML 浮点字面量允许 Rust `Display` 产生的一切形式。

颜色直接复用 CSS 序列化器，并有一条 TODO 注释：

[typed.rs:550-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L550-L555) —— `impl IntoAttr for Color`，`self.to_css(())`。注释 `TODO: Warnings are currently ignored.` 表示颜色转 CSS 时本可能发出的警告（u4-l4 的优雅降级）在这里被静默丢弃。

#### 4.2.4 代码实践

1. **实践目标**：验证「校验」与「字符串化」是分离的两段，且错误来自 `FromValue` 而非 `into_attr`。
2. **操作步骤**：
   - 阅读 [`NativeType::of`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L394-L403) 的 `cast` 闭包，确认它是 `value.cast::<T>()?` 紧跟 `Ok(Some(this.into_attr()))`。
   - 设想用户给一个整数型属性（如 `colspan`）传了字符串 `"abc"`：`cast::<i64>()` 会在哪一步失败？错误是否带 span？
3. **需要观察的现象**：失败发生在 `cast::<T>()?`（`FromValue`），`into_attr` 根本不会被调用；错误经 `construct` 的 `.at(span)` 定位到源码。
4. **预期结果**：类型不匹配报「expected integer, found string」之类的标准 Typst 错误，而非「序列化失败」。
5. 本实践为源码阅读型，可完全确定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IntoAttr` 要绑定 `FromValue` 作为超类型，而不是让 `NativeType::of` 各自处理校验？

> **答案**：为了让「值合法性校验」复用 Typst 整套成熟的 `FromValue` / `Reflect` 机制（含 `cast!` 宏、带 span 的标准错误），typst-html 只专注于「合法值 → 属性字符串」这一步。这避免了在每个属性类型上重写一套校验逻辑。

**练习 2**：`impl IntoAttr for Color` 调 `to_css(())`，为什么参数是空元组 `()`？

> **答案**：`ToCss::to_css` 接受一个上下文参数（u4-l4），颜色序列化不需要额外上下文，故传 `()`。它复用了把 Typst 颜色转成 CSS 文本（`#hex` 或 `rgb()`）的同一段逻辑。

### 4.3 Presence、None 变体与三种布尔编码

#### 4.3.1 概念说明

本模块集中处理「真假值如何在 HTML 属性里表达」。这是最容易被初学者混淆的部分，因为 **HTML 里『布尔』并不只有一种写法**，typst-html 用四个不同的 `AttrType`/`IntoAttr` 实现把它们严格区分开：

1. **`Presence`（布尔存在性）**：对应 HTML 的「布尔属性」，如 `disabled`、`hidden`、`readonly`。规则是「属性存在即为真」。typst-html 的做法：传 `true` → 写出**空值** `disabled=""`（编码期再变成简写 `disabled`）；传 `false` → 返回 `None`，**整个属性被省略**。
2. **`TrueFalseBool`**：真→`"true"`，假→`"false"`（如 `contenteditable="true"`）。**假也写字面字符串**，不省略属性。
3. **`YesNoBool`**：真→`"yes"`，假→`"no"`。
4. **`OnOffBool`**：真→`"on"`，假→`"off"`。

后三者是 newtype `pub struct X(pub bool)`，各用 `cast!` 从 Typst `bool` 构造，差别仅在 `into_attr` 输出的字面量。它们与 `Presence` 的本质区别在于：**`Presence` 的假会删掉属性，而这三种的假会写出一个具体的字符串**。

此外还有几个「字面量」类型：`AutoValue`→`"auto"`、`NoneValue`→`"none"`，以及两个特别的 None 变体——`NoneEmpty`（从 `none` 转，输出空串 `""`）和 `NoneUndefined`（从 `none` 转，输出 `"undefined"`）。它们都从 Typst 的 `none` 转换，差别只在输出字符串，用于建模 HTML 里「空 / undefined」语义各异的属性。

#### 4.3.2 核心流程

```
Presence:        bool ──> true  => Some("")    （编码期变布尔属性简写）
                         false => None          （属性被省略）

TrueFalseBool:   bool ──> "true" / "false"      （总是写字面量）
YesNoBool:       bool ──> "yes"  / "no"
OnOffBool:       bool ──> "on"   / "off"

AutoValue:       auto ──> "auto"
NoneValue:       none ──> "none"
NoneEmpty:       none ──> ""          （空串）
NoneUndefined:   none ──> "undefined"
```

#### 4.3.3 源码精读

`Presence` 在 `AttrType::cast` 里被特判（不走 `Native`），一行表达式交代了「真→空串、假→`None`」：

[typed.rs:240-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L240-L242) —— `Presence` 分支：`value.cast::<bool>().map(|b| b.then(EcoString::new))`。`b.then(EcoString::new)` 在 `b == true` 时返回 `Some("")`，否则 `None`。

三种布尔编码结构完全对称，以 `TrueFalseBool` 为例：

[typed.rs:421-432](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L421-L432) —— `TrueFalseBool`：newtype + `cast!` 从 `bool` + `IntoAttr` 输出 `"true"`/`"false"`。

另外两种：

[typed.rs:437-448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L437-L448) —— `YesNoBool`，输出 `"yes"`/`"no"`。

[typed.rs:453-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L453-L464) —— `OnOffBool`，输出 `"on"`/`"off"`。

字面量类型与两个特殊 None 变体：

[typed.rs:466-476](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L466-L476) —— `AutoValue`→`"auto"`、`NoneValue`→`"none"`。

[typed.rs:479-504](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L479-L504) —— `NoneEmpty`（`none`→`""`）与 `NoneUndefined`（`none`→`"undefined"`）。两者都用 `cast! { X, _: NoneValue => X }` 从 Typst `none` 构造，差别仅在 `into_attr` 的输出字符串。

#### 4.3.4 代码实践

1. **实践目标**：在脑中钉死「`Presence` 的假省略属性」与「三种 Bool 的假写字面量」的差别。
2. **操作步骤**：
   - 对照 [`Presence` 的 cast](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L239-L241) 与 [`TrueFalseBool::into_attr`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L428-L432)。
   - 假设 `disabled` 是 `Presence` 型、`contenteditable` 是 `TrueFalseBool` 型：分别给两者传 `false`，预测 HTML 输出。
3. **需要观察的现象**：`Presence`+`false` → 属性消失（`construct` 因 `Ok(None)` 不压入 `attrs`）；`TrueFalseBool`+`false` → 写出 `contenteditable="false"`。
4. **预期结果**：前者输出中无该属性，后者输出含字面 `"false"`。
5. **待本地验证**：以上属性名所属的具体 `data::Type` 须以 `typst-assets` 规范数据为准；若需实测，可在 Typst 中分别构造并用 `--format html` 编译后查看输出 HTML。

#### 4.3.5 小练习与答案

**练习 1**：`Presence` 和 `TrueFalseBool` 都接受 Typst `bool`，为什么需要分成两套？

> **答案**：因为它们对应的 HTML 属性语义不同。`Presence` 对应「布尔属性」（存在即真），假意味着「不要这个属性」。`TrueFalseBool` 对应 `contenteditable` 这类「属性值本身就是 `true`/`false` 字符串」的属性，假也要显式写出 `"false"`。混用会导致 HTML 语义错误。

**练习 2**：`NoneEmpty` 和 `NoneUndefined` 都从 `none` 转换，区分它们的意义是什么？

> **答案**：HTML 不同属性对「空/缺失」的字符串表示不同——有的用空串，有的用字面 `"undefined"`。两者都接受 Typst `none`（用户侧统一写 `none`），但 `into_attr` 分别产出 `""` 与 `"undefined"`，精准匹配各自属性的规范要求。

### 4.4 ListType：列表型属性的分隔、简写与含分隔符拒绝

#### 4.4.1 概念说明

HTML 有两类「token 列表」属性（WHATWG 规范定义）：[空格分隔 token](https://html.spec.whatwg.org/#space-separated-tokens)（如 `class`、`rel`）与[逗号分隔 token](https://html.spec.whatwg.org/#comma-separated-tokens)（如 `<input accept>`）。在 Typst 里，这类属性最自然的表达是一个**数组**。

`ListType` 负责把一个 Typst 数组编码成用指定分隔符拼接的字符串。它有三个字段：

- `inner: &'static data::Type` —— 数组每个元素的类型（通常是 `Str` 或某个枚举）。
- `separator: char` —— 分隔符，按规范是空格 `' '` 或逗号 `','`。
- `shorthand: bool` —— 是否允许「单值简写」：若为真，除了数组，也接受单个 `inner` 类型的值（不必包成单元素数组）。

`ListType` 最关键的设计是**主动拒绝含有分隔符的数组项**。因为编码方式就是用 `separator` 把各项 `join` 起来，如果某个项自身含有分隔符，拼接后就会产生歧义（一个项被误读成两个）。所以 `cast` 会在拼接前逐项检查，发现某项的字符串化结果 `contains(separator)` 就直接 `bail!` 报错，而不是产出会被误解析的 HTML。

#### 4.4.2 核心流程

```
ListType::cast(value):
  ├─ 若 value 是 Array:
  │     对每个元素 item:
  │       s = inner.cast(item)            逐元素转换
  │       若 s 含 separator  ──> bail!    拒绝（防歧义）
  │       非首项前插入 separator           （逗号额外补一个空格）
  │     返回 Some(拼接串)
  │
  ├─ 否则若 shorthand 且 value 是单值:
  │     返回 Some(inner.cast(value))      单值简写，无分隔符
  │
  └─ 否则 ──> 报错（不符合 input 描述）
```

`input()` 的设计配合了 `shorthand`：开简写时返回 `Array::input() + inner.input()`（`CastInfo` 的 `+` 表示「接受两者之一」的联合），于是函数签名/文档会同时提示「可传数组或单个字符串」；不开简写时只接受数组。

#### 4.4.3 源码精读

`ListType` 结构与字段含义，文档注释链向 WHATWG 两类 token 规范：

[typed.rs:314-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L314-L322) —— `struct ListType { inner, separator, shorthand }`。

`input()` 与 `castable()` 如何随 `shorthand` 切换：

[typed.rs:325-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L325-L335) —— 开简写时 `input` 是 `Array::input() + self.inner().input()`（联合），`castable` 额外允许单值。

承重的 `cast`，含「拒绝分隔符」与「逗号补空格」两处细节：

[typed.rs:337-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L337-L375) —— `ListType::cast`。重点三处：

- L344-L358 `if item.as_str().contains(self.separator)` —— 含分隔符即拒，错误信息把分隔符命名为 `"space"` / `"comma"` 或 `"'{}'"`（其他字符），并附 hint 说明「该数组属性会被编码成 X 分隔串」。
- L360-365 拼接逻辑：非首项前插入 `separator`；若 `separator == ','` 额外 `push(' ')`，即逗号分隔串规范化为 `", "`。
- L369-372 简写分支：单值直接转换、不加分隔符。

> 关于 L343 的 `ty.cast(item)?.unwrap()`：列表元素类型（如 `Str`）的 `cast` 总返回 `Some`，故 `unwrap` 安全；`Presence` 这类可能返回 `None` 的类型不会作为列表元素出现。

#### 4.4.4 代码实践（本讲核心实践）

本实践用「纸上追踪」验证 `ListType::cast` 的行为——这部分**可完全依据源码确定**，无需运行；随后给出一个可选的本地验证步骤。

1. **实践目标**：手动追踪一个空格分隔、带简写的 `ListType`（`separator = ' '`、`shorthand = true`、`inner = Str`）在三种输入下的输出，并解释「含分隔符拒绝」的必要性。
2. **操作步骤（纸上追踪）**：对下列三个输入，逐行对照 [`ListType::cast`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L337-L375) 写出 `out` 的演变或错误：
   - 输入 A：Typst 数组 `("foo", "bar")`
   - 输入 B：Typst 数组 `("foo", "bar baz")`
   - 输入 C：Typst 单值 `"foo"`（非数组）
3. **需要观察的现象 / 预期结果**（直接给出，便于核对）：
   - **A `("foo","bar")`**：走数组分支。i=0：`"foo"` 不含空格、`i==0` 不插分隔符，`out="foo"`。i=1：`"bar"` 不含空格、`i>0` 插入 `' '`（非逗号不再补空格），`out="foo bar"`。返回 `Some("foo bar")`。
   - **B `("foo","bar baz")`**：i=0 同上 `out="foo"`；i=1：`"bar baz".contains(' ')` 为真 → `bail!("array item may not contain a space")`，附带 hint。**报错，不产出任何 HTML**。
   - **C `"foo"`**：`Array::castable` 为假，但 `shorthand && Str.castable` 为真 → 走简写分支，返回 `Some("foo")`（无分隔符）。
4. **追问**：把分隔符换成逗号 `','`，输入 `("a","b")` 会得到什么？
   - **答案**：i=0 `out="a"`；i=1 插入 `','` 再（因是逗号）补 `' '`，`out="a, b"`。即逗号分隔串被规范化为 `", "`。
5. **本地验证（待本地验证）**：在 Typst 中找一个空格分隔的列表型属性（如 `class`，须以 `typst-assets` 规范数据确认其确为 `List(_, ' ', true)`），写 `#html.div(class: ("foo", "bar"))[Hi]` 并用 `--format html` 编译，核对输出是否为 `class="foo bar"`；再试 `class: ("foo", "bar baz")`，确认编译器报「array item may not contain a space」。若该属性并非列表型，请改用其他经文档确认为列表型的属性做同样验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ListType::cast` 必须拒绝含有分隔符的项，而不是自动转义？

> **答案**：HTML 的空格/逗号分隔 token 没有转义机制——分隔符就是分隔符，无法「逃逸」。若允许项中含分隔符，拼接串会被浏览器重新切分成错误的 token 数量。typst-html 选择在编译期 `bail!` 报错，把问题暴露给作者，而不是产出静默错误的 HTML。

**练习 2**：`shorthand` 关闭时，给列表属性传单个字符串会发生什么？

> **答案**：`input()` 只描述 `Array`、`castable` 对单值返回假，于是落到 `cast` 的 `else` 分支，按 `input().error(&value)` 报「expected array」类错误。即「必须用数组包起来」。

### 4.5 UnionType、StringsType 与 HTML 规范格式序列化

#### 4.5.1 概念说明

本模块收尾两类「复合类型」与一组「需要特殊格式化」的标量。

**`UnionType`** 建模「接受若干类型中的任意一种」。比如某属性可接受整数或字符串 `"auto"`。它持有一组 `data::Type`，`cast` 时按顺序找到第一个 `castable` 的成员，把转换委托给它；都不匹配则报「联合」错误。

**`StringsType`** 建模「枚举」：属性只接受一组**固定的字符串字面量**之一（如 `dir` 只接受 `"ltr"`/`"rtl"`/`"auto"`）。这些字面量不是写在 `typed.rs` 里，而是统一存放在 `typst-assets` 的 `data::ATTR_STRINGS` 表中，`StringsType` 只保存一个 `[start, end)` 切片范围来索引它。`input()` 据此生成文档/自动补全里的候选值列表；`cast` 时值必须是 `Str` 且等于其中某个字面量，原样返回。

**HTML 规范格式序列化**：有一类属性要求值符合 WHATWG/HTML 的特定字符串格式，无法用「直接 `Display`」表达，故各自定制 `IntoAttr`：

- `Duration` → [valid duration string](https://html.spec.whatwg.org/#valid-duration-string)，如 `"2w 3d 4h 5m 6s"`。
- `Datetime` → [valid date / time / global date-and-time string](https://html.spec.whatwg.org/#valid-date-string)，如 `"2026-07-30"`、`"14:30"`、`"2026-07-30T14:30"`。
- `IconSize` → `<link sizes>` 的 `"WxH"`（如 `"16x16"`）。
- `HorizontalDir` → 水平方向 `"ltr"`/`"rtl"`（拒绝纵向 `ttb`/`btt`）。
- `ImageCandidate` → `<img srcset>` 的候选项（`src` 配 `width` 或 `density`）。
- `SourceSize` → `<img sizes>` 的源尺寸（`condition` 配 CSS `size`）。

后两者从 Typst `Dict` 构造，校验后拼成规范字符串。

#### 4.5.2 核心流程

```
UnionType::cast(value):
  for 每个成员 ty:
    if ty.castable(value): return ty.cast(value)   首个匹配者负责转换
  报联合错误

StringsType::cast(value):
  if value 是 Str 且等于某个候选: 原样返回
  否则报「期望之一」错误

Duration.into_attr:  分解 [w,d,h,m,s] ─> 拼成 "2w 3d 4h 5m 6s"（秒恒输出）
Datetime.into_attr:  按 Date/Time/Datetime 变体 ─> "YYYY-MM-DD" / "HH:MM[:SS]" / 二者用 T 连接
```

#### 4.5.3 源码精读

`UnionType` 持有一组类型，逐个尝试：

[typed.rs:289-312](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L289-L312) —— `UnionType(&'static [data::Type])`。`iter()` 把每个 `data::Type` 经 `AttrType::convert` 还原成可执行类型；`cast` 取首个 `castable` 者委托，否则 `self.input().error(&value)`。

`StringsType` 用切片索引全局字符串表：

[typed.rs:250-286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L250-L286) —— `StringsType { start, end }`。`strings()` 返回 `&data::ATTR_STRINGS[start..end]`（元素是 `(值, 描述)` 对）；`input()` 把它们展成 `CastInfo::Union` 供文档/补全；`castable` 要求 `Value::Str` 且匹配某值；`cast` 匹配则原样返回该字符串。

`Duration` 按 WHATWG 规范拼接，秒恒输出：

[typed.rs:557-589](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L557-L589) —— `impl IntoAttr for Duration`。`decompose()` 得到 `[weeks,days,hours,minutes,seconds]`；用 `part!` 宏按非零分量依次写出 `Nw`/`Nd`/`Nh`/`Nm`/`Ns`，分量间以空格连接；`seconds > 0 || out.is_empty()` 保证「秒」段恒在（全零时输出 `"0s"`）。

`Datetime` 按变体分派到内部 `datetime` 子模块：

[typed.rs:591-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L591-L600) —— `impl IntoAttr for Datetime`，按 `Date`/`Time`/`Datetime` 三变体分别格式化。

[typed.rs:602-625](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L602-L625) —— `datetime` 子模块：`date` 写 `{:04}-{:02}-{:02}`；`time` 写 `{:02}:{:02}`，秒数 `> 0` 才追加 `:{:02}`；`datetime` 用 `'T'` 连接日期与时间（[valid global date-and-time-string](https://html.spec.whatwg.org/#valid-global-date-and-time-string)）。

其余四个特殊序列化器：

[typed.rs:628-669](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L628-L669) —— `HorizontalDir`（校验水平、委托 `Dir::into_attr` 输出 `ltr`/`rtl`/`ttb`/`btt`）与 `IconSize`（`"{x}x{y}"`）。

[typed.rs:671-706](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L671-L706) —— `ImageCandidate`：从 `Dict` 取 `src`/`width`/`density`，校验 `src` 非空且不以逗号开头结尾、`width` 与 `density` 互斥，拼成 [image-candidate-string](https://html.spec.whatwg.org/#image-candidate-string)（如 `"pic.jpg 2w"` 或 `"pic.jpg 2d"`）。

[typed.rs:708-728](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L708-L728) —— `SourceSize`：从 `Dict` 取 `condition` 与 `size`（`Length`），输出 `(condition) <css-size>`，`size` 经 `to_css(())` 转 CSS 文本（附带「暂不支持无法用 Typst 长度表达的 CSS 长度」的 hint）。

#### 4.5.4 代码实践

1. **实践目标**：体会「规范格式」与「平凡 `Display`」的差别，理解为何这些类型需要定制 `IntoAttr`。
2. **操作步骤**：
   - 对照 [`Duration::into_attr`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L557-L589) 与 [`datetime` 子模块](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L602-L625)。
   - 设想一个 `time::Duration` 分解为 `[0,0,0,0,0]`（全零），追踪 `out` 的值；再设想 `[0,1,30,0,0]`（1 天 30 分钟），写出结果。
3. **需要观察的现象 / 预期结果**：
   - 全零：所有分量都跳过，但 `seconds > 0 || out.is_empty()` 中 `out.is_empty()` 为真 → 写出 `"0s"`。
   - `[0,1,30,0,0]`：`days=1`→`out="1d"`；`hours=0` 跳过；`minutes=30`→`out="1d 30m"`；`seconds=0` 但 `out` 非空 → 跳过。结果 `"1d 30m"`。
   - Datetime 的 `Time(14:30:00)`：`time` 写 `"14:30"`，秒为 0 不追加 → `"14:30"`；`Time(14:30:45)` → `"14:30:45"`。
4. **预期结果**：与上文一致；可见「秒恒定输出」「秒为零省略」等规则都源于规范，不能简单 `Display`。
5. 本实践为源码阅读型，可完全确定。

#### 4.5.5 小练习与答案

**练习 1**：`StringsType` 为什么只保存 `start`/`end` 两个下标，而不直接持有字符串切片？

> **答案**：所有枚举字面量集中存放在 `typst-assets` 的 `data::ATTR_STRINGS` 全局表里（多个属性共享同一张表），`StringsType` 只用 `[start, end)` 切片引用其中一段。这避免了在每个属性的类型描述里重复存储字面量，也使规范数据更紧凑、`'static` 生命周期更易保证。

**练习 2**：`UnionType::cast` 与 `StringsType::cast` 在「值不匹配」时，错误信息为何不同？

> **答案**：`Union` 的 `input()` 是各成员 `CastInfo` 的联合，错误会列出「期望的若干类型」；`Strings` 的 `input()` 是固定字面量的 `CastInfo::Union`，错误会列出「期望的若干具体字符串」。两者都调用 `self.input().error(&value)`，错误形态由各自的 `input()` 决定。

## 5. 综合实践

把本讲五种变体串成一次「预测 HTML 输出」的综合训练。

**任务**：选定一个含有多种类型属性的元素（例如带布尔存在性、`true/false` 布尔、列表、颜色等属性的元素），按下表对每个属性「先据本讲的类型系统预测其序列化结果，再用 Typst 编译核对」。

| 属性类型（假设） | 用户传入 | 据本讲预测的属性串 | 依据 |
| --- | --- | --- | --- |
| `Presence` | `true` | `属性=""`（编码期变简写） | 4.3，`Some("")` |
| `Presence` | `false` | （属性被省略） | 4.3，`None` |
| `TrueFalseBool` | `false` | `属性="false"` | 4.3 |
| `List(_, ' ', true)` | `("a","b")` | `属性="a b"` | 4.4 |
| `List(_, ' ', true)` | `("a","b c")` | 编译报错 | 4.4，含分隔符拒绝 |
| `Color` | `rgb("ff0000")` | `属性="#ff0000"` | 4.2，复用 `to_css` |

**操作步骤**：

1. 阅读本讲各模块，把上表「预测」列自行填出，再与给出值核对。
2. 在 Typst 中用对应的类型化函数构造该元素（属性名须以 `typst-assets` 规范数据或 Typst 生成的 HTML 文档为准；**待本地验证**具体属性名与类型）。
3. 用 `--format html` 编译，逐项比对输出与预测，重点关注 `Presence`+`false` 是否真的省略、列表含分隔符是否真的报错。
4. 若某属性的实际类型与假设不同，记录差异并修正预测——这正好检验你对 `AttrType` 五变体的辨识。

**目标**：练完后，看到任何一个类型化 HTML 属性，你都能仅凭其 `data::Type` 判断它会走哪条分派路径、传某值会被写成什么或为何报错。

## 6. 本讲小结

- `AttrType` 是属性类型的**动态分派中心**：`convert` 把静态的 `data::Type` 翻译成五个可执行变体 `Presence / Native / Strings / Union / List`，统一暴露 `input` / `castable` / `cast` 三个查询接口；`cast` 返回 `Option<EcoString>`，`None` 表示「省略该属性」。
- `Native` 变体由 `NativeType`（三个 `fn` 指针的虚表）支撑，`IntoAttr: FromValue` 把职责切成「校验（复用 Typst `FromValue`）」与「字符串化（`into_attr`）」两段。
- HTML 的「布尔」有四种写法：`Presence`（假则省略属性）与 `TrueFalseBool` / `YesNoBool` / `OnOffBool`（假也写字面量），外加 `Auto`/`None`/`NoneEmpty`/`NoneUndefined` 等字面量类型。
- `ListType` 把 Typst 数组用空格或逗号拼接成 token 串，**主动拒绝含分隔符的项**（HTML token 无转义机制），逗号分隔串规范化为 `", "`；`shorthand` 允许单值简写。
- `UnionType` 取首个匹配成员委托转换；`StringsType` 用 `[start,end)` 切片索引全局 `ATTR_STRINGS` 表建模枚举。
- `Duration` / `Datetime` / `IconSize` / `HorizontalDir` / `ImageCandidate` / `SourceSize` 按 WHATWG/HTML 规范格式定制 `IntoAttr`，无法用平凡 `Display` 表达（如 Duration 的「秒恒输出」、Datetime 的「秒为零则省略」）。

## 7. 下一步学习建议

- **横向对照 CSS 序列化**：本讲 `impl IntoAttr for Color` 复用了 u4-l4 的 `ToCss`。建议重读 [u4-l4](u4-l4-typst-to-css-conversion.md)，对比「属性字符串化（`IntoAttr`）」与「CSS 值字符串化（`ToCss`）」两套独立但偶有交集的序列化体系。
- **回顾整条导出链**：属性经本讲写入 `HtmlAttrs` 后，最终在 u5-l1 的 `encode.rs` 被写成 HTML 文本（空值简写、转义都在那里）。建议带着一个 `Presence` 属性重读 [u5-l1](u5-l1-dom-to-html-encoding.md)，把「`Some("")` → 布尔属性简写」这最后一步补全。
- **动手扩展（进阶）**：若想加深理解，可尝试在本地 fork 中为 `typed.rs` 增加一个 `trace` 日志，在 `AttrType::cast` 分派时打印变体名与输入值，编译若干 Typst 文档观察不同属性分别命中哪条路径（注意：本讲义要求不修改源码，此为课下练习）。
