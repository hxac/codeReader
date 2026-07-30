# #[elem]（四）：Construct/Set 与能力系统

## 1. 本讲目标

本讲是 `#[elem]` 系列的第四讲，也是最后一讲「行为生成」。前三讲（u4-l1~u4-l3）解决了「字段长什么样、struct 怎么写、字段如何注册进虚表」，本讲回答最后一个问题：

> 用户在 Typst 里写下 `#rect(width: 10cm)[hi]` 这样的代码时，元素实例是怎么被「造出来」的？元素又凭什么能被 show 规则、查询（query）、定位（locate）等机制识别？

学完本讲，你应当能够：

1. 说清 `impl Construct` 与 `impl Set` 各自负责什么、参与哪些字段，以及为什么它们是 `NativeElement` 的**强制超 trait**。
2. 读懂 `create_field_parser` 的四条默认分支（`all/expect/find/named`），并能说明 `#[parse(...)]` 自定义块如何**整段覆盖**这些默认行为。
3. 理解 `create_capable_func` 如何用 `FORBIDDEN` 名单过滤能力、用 `NonNull::dangling()` 的**悬垂指针**安全取出一个能力的虚表地址。
4. 区分三类「内省能力」`Locatable/Unqueriable/Tagged` 的校验规则，以及 `Mathy` 这个特例。

本讲全部源码集中在 [`src/elem.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs)，运行时契约跨到 `typst-library`。

## 2. 前置知识

阅读本讲前，请确认你已掌握前三讲的内容：

- **u4-l1**：`Field` 的属性体系（`positional/required/variadic/fold/internal/external/ghost/synthesized/parse/default`）与五个字段过滤器（`real_fields ⊃ struct_fields ⊃ accessor_fields`，以及 `construct_fields`、`set_fields`）。
- **u4-l2**：struct 的三态存储（required 用裸类型、synthesized 用 `Option<T>`、settable 用 `Settable<Self, I>`），`new()` 与 `with_X` builder。
- **u4-l3**：`unsafe impl NativeElement` 如何把元素注册进 `ContentVtable`，字段如何按六路分支（external/variadic/required/synthesized/ghost/settable）选虚表槽位。

本讲还会用到两个运行时概念，先建立直觉：

- **胖指针（fat pointer）**：Rust 里 `*const dyn Trait` 并不是一个地址，而是**两个地址**拼在一起——一个指向数据，一个指向虚表（vtable）。本讲「取能力虚表」的关键就是只读虚表那个字，不碰数据那个字。
- **TypeId**：`std::any::TypeId::of::<T>()` 是一个类型在编译期算出的唯一指纹。Typst 用它来判断「某个元素到底支持哪个能力 trait」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-macros/src/elem.rs` | 本讲主战场：`create_construct_impl`、`create_set_impl`、`create_field_parser`、`create_capable_func`、`create_introspection_capabilities`、`create_mathy_impl` 全在这里 |
| `crates/typst-macros/src/util.rs` | `BlockWithReturn`——把 `#[parse(stmt; expr)]` 切成「前缀语句 + 返回表达式」 |
| `crates/typst-library/src/foundations/content/element.rs` | 运行时契约：`NativeElement`、`Construct`、`Set` 三个 trait 的定义 |
| `crates/typst-library/src/foundations/content/vtable.rs` | `IntrospectionCapabilities` 结构体，以及 `ContentVtable::new` 接收 `capable` 闭包的位置 |
| `crates/typst-utils/src/fat.rs` | `fat::vtable`——从胖指针里读出虚表地址（解释悬垂指针为何安全的依据） |
| `crates/typst-library/src/introspection/counter.rs` | 真实示例 `CounterDisplayElem`：同时覆盖 `Construct`、带 `Unqueriable+Locatable` |
| `crates/typst-library/src/text/deco.rs` | 真实示例 `StrikeElem`：`Locatable+Tagged` 内省能力 + `#[fold]` 字段 |

## 4. 核心概念与源码讲解

### 4.1 Construct 与 Set：元素的「构造」与「设样式」两条流水线

#### 4.1.1 概念说明

当 Typst 源码里出现一个元素调用 `#rect(width: 10cm, stroke: red)[内容]` 时，运行时会把它拆成**两个阶段**处理：

1. **Set 阶段**：把「跟样式有关」的具名参数（`width`、`stroke`……）解析成一组 `Styles`，挂到样式链上。
2. **Construct 阶段**：拿到 Set 阶段**剩下的**参数（通常是位置参数，比如方括号里的内容），真正 `new` 出一个元素实例，包成 `Content` 返回。

注意顺序与分工：`Construct` 的文档明确写着「This is passed only the arguments that remain after execution of the element's set rule.」也就是说 **Set 先跑、吃掉它的参数、把剩下的交给 Construct**。

这两个阶段在运行时对应两个 trait，而且都是 `NativeElement` 的**强制超 trait**：

> `NativeElement: Debug + Clone + Hash + Construct + Set + Send + Sync + 'static`

这意味着**每个元素都必须能构造、能设样式**。那么 `#[elem]` 宏的责任就是：除非用户明确表示「我自己来写」，否则一律自动生成这两个 impl。

#### 4.1.2 核心流程

宏在 `create` 函数里用一句「能力探测」决定要不要自动生成：

```
若元素 capabilities 列表里「没有」Construct → 自动生成 impl Construct
若元素 capabilities 列表里「没有」Set       → 自动生成 impl Set
若元素 capabilities 列表里有 Mathy          → 额外生成 impl Mathy
```

这里出现一个容易混淆的点：`#[elem(...)]` 括号里的能力名，**身兼两职**——

- 对于 `Synthesize`、`Show`、`PlainText` 这类，它表示「我这个元素实现了这个能力，请帮我注册虚表」（见 4.3）。
- 对于 `Construct`、`Set`、`Mathy`，它是一个**开关信号**：「这个 impl 我自己写，宏别插手」或「请帮我加这个特殊 impl」。

两条流水线的字段来源也不同：

- **Construct** 遍历 `construct_fields()`：所有「真实字段」中，要么带 `#[parse]` 块，要么「不是 synthesized 且不是 internal」的都参与构造。`required` 字段在这里被解析（必填）。
- **Set** 遍历 `set_fields()`：在 `construct_fields` 基础上**再排除 required**——因为必填字段是构造期的位置参数，没有 set 规则。

二者的包含关系是：

```
construct_fields ⊇ set_fields   （set_fields = construct_fields − required）
```

#### 4.1.3 源码精读

先看「是否自动生成」的开关，位于 `create` 函数中（u4-l2 已介绍过 `create` 的整体输出，这里聚焦条件分支）：

> [src/elem.rs:258-L261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L258-L261)：`cannot("Construct")` / `cannot("Set")` 决定是否生成对应 impl；`can("Mathy")` 决定是否额外生成 Mathy impl。

`can`/`cannot` 的实现非常直白，就是在线性扫描 `capabilities` 列表：

> [src/elem.rs:44-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L44-L55)：`can` 遍历 capabilities 查名字，`cannot` 取反。

接着看两个字段过滤器的定义与注释：

> [src/elem.rs:73-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L73-L88)：`construct_fields` 允许「internal + 带 parse」的字段，注释说明这是一种常见模式——从输入解析数据再存进字段；`set_fields` 在其基础上再排除 required。

现在进入 `create_construct_impl` 的主体。它的输出是一个完整的 `impl Construct`，函数体由两段拼成：

1. `setup`：为每个 `construct_fields` 字段生成「前缀语句 + `let 字段名 = 解析表达式;`」。
2. `fields`：为每个 `struct_fields` 字段生成结构体字面量里的初始化片段（required 用简写、synthesized 填 `None`、settable 用 `Settable::from(...)`）。

> [src/elem.rs:574-L608](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L574-L608)：`create_construct_impl`。注意签名 `fn construct(engine, args) -> SourceResult<Content>` 与运行时 trait 完全一致，结尾 `Ok(Content::new(Self { ... }))`。

`create_set_impl` 的结构几乎对称，区别在于每个字段被包进 `if let Some(value) = ... { styles.set(...); }`——因为 set 规则的参数都是**可选的**（用户可给可不给）：

> [src/elem.rs:610-L636](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L610-L636)：`create_set_impl`。返回 `Styles`，逐字段 `styles.set(Self::字段常量, value)`。

运行时契约在这里（注意 `Construct`/`Set` 都是 `Self: Sized` 的普通 trait，不是对象安全 trait）：

> [crates/typst-library/.../element.rs:247-L263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L247-L263)：`Construct::construct` 与 `Set::set` 的签名，以及上面的 `NativeElement` 超 trait 约束。

真实案例：`CounterDisplayElem` 标了 `Construct`，于是宏**不**自动生成 Construct impl，转而使用用户手写的版本（该版本直接 `bail!`——这个元素禁止用户手动构造）：

> [crates/typst-library/.../counter.rs:685-L707](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L685-L707)：`#[elem(Construct, Unqueriable, Locatable)]` 配合手写的 `impl Construct`，永远报错「cannot be constructed manually」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「能力名是开关」的机制——给一个元素加上 `Construct` 能力，观察宏是否还自动生成 Construct impl。

**操作步骤**（源码阅读型实践，不需要真编译）：

1. 打开 [`src/elem.rs:258-L261`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L258-L261)，确认 `construct_impl = element.cannot("Construct").then(...)`。
2. 找到运行时 `NativeElement` 的超 trait 列表 [`element.rs:234-L238`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L234-L238)，注意 `Construct + Set` 是强制的。
3. 阅读 [`counter.rs:685-L707`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L685-L707)，回答下面的问题。

**需要观察的现象 / 预期结果**：

- 假设 `CounterDisplayElem` 标了 `Construct` 却**没有**手写 `impl Construct`，编译会发生什么？答：因为宏因 `cannot("Construct")` 为假而**不**生成 Construct impl，又因 `NativeElement: Construct` 是强制超 trait，会报「未实现 trait `Construct`」的编译错误（E0277）。所以「标了 Construct」必须搭配「自己写 impl」，二者是契约关系。
- 反过来，若一个普通元素**不**标 Construct，宏会自动生成——为什么不会和什么冲突？答：因为此时用户没有手写，宏生成的就是唯一一份 impl。

**待本地验证**：以上推理基于源码静态分析，若你想亲见编译错误，可在 typst 仓库内删掉 `counter.rs` 第 703-707 行的手写 impl 后 `cargo check -p typst-library`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_fields` 要在 `construct_fields` 基础上排除 `required` 字段？

**参考答案**：`required` 字段是构造期的**必填位置参数**（由 `args.expect` 取出），它没有「设样式」语义；set 规则只针对可配置的样式属性，而这些属性都是可选的（`Option<T>`）。把 required 放进 Set 会让 `args.expect`（缺失即报错）与 `if let Some(...)`（期望可选）的类型对不上。

**练习 2**：下面这段是宏为某 settable 字段在 `impl Set` 里生成的代码，请解释 `value` 为什么一定是 `Option<T>`：
```rust
if let Some(value) = args.named("width")? {
    styles.set(Self::width, value);
}
```

**参考答案**：`args.named` 返回 `SourceResult<Option<T>>`，`?` 解包掉 `SourceResult` 后剩 `Option<T>`，正好喂给 `if let Some(value)`。Set 的所有默认分支（`find`/`named`）都产 `Option<T>`，而产裸 `T` 的 `expect`、产 `Vec` 的 `all` 只会出现在 required/variadic 字段里——而它们已被 `set_fields` 排除，所以 Set 上下文里永远是 `Option`。

**练习 3**：`create_construct_impl` 里 synthesized 字段被初始化为 `None`（见 `fields` 段），但 synthesized 字段又不在 `construct_fields` 里。这两点矛盾吗？

**参考答案**：不矛盾。`construct_fields` 控制「**是否解析参数**」，synthesized 不从参数来，所以不参与 `setup`；但 `struct_fields` 控制「**结构体字面量里要不要这一项**」，synthesized 是真实结构体字段（存 `Option<T>`），所以必须在字面量里出现，构造期先填 `None`，等后续 `Synthesize` 阶段再填实。

---

### 4.2 字段参数解析器 create_field_parser：四种默认分支与 #[parse] 覆盖

#### 4.2.1 概念说明

4.1 里 `setup`/`handlers` 都调用了 `create_field_parser(field)`，它返回一个二元组 `(prefix, value)`：

- `prefix`：一段要在取值**之前**执行的语句（通常是 `let` 绑定一些中间变量）。
- `value`：一个**表达式**，其求值结果就是这个字段的值。

这个函数是「字段属性 → 取参代码」的翻译器。绝大多数字段走**四条默认分支**之一；当默认分支不够用时，用户可以用 `#[parse(...)]` **整段覆盖**。

#### 4.2.2 核心流程

默认分支按优先级判定（注意是 `if/else if` 链，**短路**）：

```
若 variadic         → args.all()?       // 把剩余所有位置参数收进 Vec
否则 若 required    → args.expect(name)?// 必填，缺失即报错
否则 若 positional  → args.find()?      // 可选位置参数
否则                → args.named(name)? // 具名参数（可选）
```

对应的 `Args` 方法语义（运行时）：

| 方法 | 返回类型 | 含义 |
| --- | --- | --- |
| `args.all::<T>()` | `Vec<T>` | 消费所有剩余位置参数 |
| `args.expect::<T>(what)` | `T` | 必填，缺失报错 |
| `args.find::<T>()` | `Option<T>` | 取首个可选位置参数 |
| `args.named::<T>(name)` | `Option<T>` | 取具名参数 |
| `args.eat::<T>()` | `Option<T>` | 取并**消费**首个可选位置参数（常用于 parse 块内部） |

当字段带 `#[parse(...)]` 时，这四条分支**全部跳过**，`prefix` 和 `value` 直接来自用户写的内容。`#[parse(...)]` 的语法是「一串以 `;` 分隔的语句，最后一条是返回表达式」——注意**没有花括号 `{}`**，括号里直接写语句。这由 `util.rs` 的 `BlockWithReturn` 解析。

#### 4.2.3 源码精读

`create_field_parser` 的完整逻辑很短：

> [src/elem.rs:638-L656](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L638-L656)：先看 `field.parse` 是否存在；存在就直接返回用户写的 `(prefix, expr)`，否则按四分支选默认表达式。

`#[parse(...)]` 的解析器 `BlockWithReturn` 用 `syn::Block::parse_within` 把括号内切成语句序列，再 `pop()` 出最后一条当 `expr`，剩下的当 `prefix`：

> [src/util.rs:235-L248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L235-L248)：`BlockWithReturn`。注意它解析的是「块内语句」，所以 `#[parse(...)]` 括号里**不写** `{}`。

来看真实用法。最简单的形式是单个表达式（无 `;`），此时 `prefix` 为空，`expr` 就是整个表达式。`grid` 的字段就是一例：

> [crates/typst-library/.../grid/mod.rs:219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L219)：`#[parse(args.named("row-gutter")?.or_else(|| gutter.clone()))]`——单表达式，先取 `row-gutter`，没有就退回 `gutter`。

带前缀语句的形式（多行、有 `;`）用于需要中间变量的场景，比如 pdf attach 元素：

> [crates/typst-library/.../pdf/attach.rs:38-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs#L38-L44)：先用 `let Spanned { v: path, span } = args.expect(...)?` 拆出一个带 span 的值，再 `let resolved = ...`，最后表达式返回解析结果。这里的 `let` 语句会进 `prefix`。

#### 4.2.4 代码实践

**实践目标**：给定一个带 `#[parse(...)]` 的字段，手写出宏在 `impl Construct` 的 `setup` 段里会生成的代码。

**操作步骤**：

1. 假设有这样一个字段（**注意：不带花括号**，这是正确语法）：
   ```rust
   /// 示例字段
   #[parse(
       let doubled = args.expect::<i64>("value")? * 2;
       doubled
   )]
   pub count: i64,
   ```
2. 走一遍 [`create_field_parser`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L638-L656)：因为 `field.parse` 非空，`prefix = [let doubled = ... * 2]`、`expr = doubled`。
3. 套进 [`create_construct_impl`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L577-L584) 的 `quote!{ #prefix; let #ident = #value; }` 模板。

**预期结果**（生成的 setup 片段）：
```rust
let doubled = args.expect::<i64>("value")? * 2;
let count = doubled;
```
注意 `prefix` 语句之间用 `;` 连接（见 `quote!{ #(#prefix);* }`），随后跟一句 `let count = doubled;`，于是局部变量 `count` 拥有了用户期望的「翻倍」值。

> 关于规格里写的 `#[parse({ let x = …; x })]`（带花括号）：实际源码与 `BlockWithReturn` 都表明正确写法**不带花括号**。若误加 `{}`，`Block::parse_within` 会把整个 `{ ... }` 当成**一条块表达式语句**，`pop()` 后 `expr` 是那个块、`prefix` 为空，行为与预期不符。所以务必用 `#[parse(let x = …; x)]` 这种形式。

#### 4.2.5 小练习与答案

**练习 1**：一个 `#[variadic]` 字段会走哪条默认分支？为什么它不可能出现在 `impl Set` 里？

**参考答案**：走 `args.all()?`（第一条分支）。它不可能出现在 Set 里，因为 `variadic` 自动蕴含 `required`（见 u4-l1 的 `required = has_attr("required") || variadic`），而 `set_fields` 排除了所有 required 字段。

**练习 2**：一个既不是 required、也不是 positional、也没标 `#[parse]` 的 settable 字段会走哪条分支？它在 Typst 语法里长什么样？

**参考答案**：走最后一条 `args.named(name)?`，即具名参数。在 Typst 里写作 `#elem(name: value)`。这是「可设样式的具名字段」最常见形态（如 `rect(width: 10cm)` 的 `width`）。

**练习 3**：为什么 `args.find()?`（可选位置参数）和 `args.named(name)?`（具名参数）都返回 `Option<T>`，而 `args.expect(name)?` 返回裸 `T`？

**参考答案**：`find`/`named` 描述的是「**可能没有**」的参数（用户可省略），所以用 `Option` 表达缺失；`expect` 描述的是「**必须存在**」的必填参数，缺失就直接通过 `?` 传播一个错误（返回 `SourceResult`），成功则拿到确定的 `T`。这种「用类型表达是否可缺省」的设计贯穿整个 `Args` API。

---

### 4.3 能力系统 create_capable_func：FORBIDDEN 名单与 dangling 指针取虚表

#### 4.3.1 概念说明

除了 `Construct`/`Set`，元素还能挂很多**能力**（capability），比如 `Synthesize`（show 规则前合成字段）、`Show`（自定义呈现）、`PlainText`（抽纯文本）、`Locatable`（可被 query/locate）等。运行时拿到一个被擦除类型的 `Content` 后，需要回答一个问题：

> 「这个元素到底实现了哪些能力？给我对应能力的虚表，我好调用它的方法。」

由于 `Content` 已经丢了具体类型，没法用普通 `dyn Trait` 多态，Typst 的做法是：**编译期为每个元素生成一个 `capable` 闭包**，输入一个 `TypeId`（「我想查的能力」），输出「这个能力虚表的地址」或 `None`。

`create_capable_func` 就是生成这个闭包的函数。它有两个关键设计：

1. **FORBIDDEN 名单**：有些能力**不**走这条 `TypeId` 查询通道——要么因为不是对象安全（`Debug`/`PartialEq`/`Hash`），要么因为已经由别的代码路径处理了（`Construct`/`Set`/`Repr`/`LocalName`/三类内省能力）。这些名字会被先过滤掉。
2. **悬垂指针取虚表**：要拿到 `dyn Capability` 的虚表地址，需要一个该具体类型的胖指针；但闭包里并没有一个真正的实例。Typst 用 `NonNull::dangling()` 造一个**不指向有效内存、但对齐且非空**的指针，再 cast 成 `*const dyn Capability`，最后用 `fat::vtable` 读出虚表字——这之所以安全，是因为「读虚表」操作根本不碰数据指针。

#### 4.3.2 核心流程

能力虚表在 `ContentVtable` 里的接线位置（u4-l3 讲过 `create_native_elem_impl`，这里聚焦 `capable_func` 这一项）：

```
ContentVtable::new::<Ident>(
    name, title, since, docs,
    DefSite { path: file!(), key: ... },
    &[各字段虚表切片],
    field_id 闭包,
    capable_func 闭包,     // ← 本节产物
    introspection,         // ← 4.4 产物
    || &STORE,
)
```

`capable_func` 闭包的形状是 `|capability: TypeId| -> Option<NonNull<()>>`，内部对每个「未被 FORBIDDEN」的能力生成一条 `if`，命中就返回该能力的虚表地址。

胖指针与虚表的关系（关键直觉）：

一个 `*const dyn Trait` 在内存里是两个字：

```
┌──────────────┬──────────────┐
│  data 指针   │ vtable 指针  │
└──────────────┴──────────────┘
```

`fat::vtable(ptr)` 只是把这两个字重新解释成 `{ data, vtable }` 结构体，然后**返回 `vtable` 那个字**——它从不解引用 `data`。所以 `data` 是不是有效内存、是不是悬垂，都无所谓。

#### 4.3.3 源码精读

`FORBIDDEN` 名单与闭包生成：

> [src/elem.rs:658-L702](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L658-L702)：`create_capable_func`。注释把 FORBIDDEN 分成两类——「Not object safe」与「Introspection capabilities ... handled otherwise」。

逐段拆解：

1. **名单定义** [L662-L675](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L662-L675)：列出 10 个被禁的能力。
2. **过滤** [L678-L681](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L678-L681)：`relevant` 只保留不在名单里的能力。
3. **逐能力生成 if** [L683-L693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L683-L693)：比较 `TypeId`，命中则用 `fat::vtable(dangling as *const dyn #capability)` 取虚表。注意安全注释就在这里。
4. **闭包外壳** [L695-L701](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L695-L701)：先造 `dangling = NonNull::<Packed<#ident>>::dangling().as_ptr()`，再串起所有 `if`。

被查的能力是 `dyn #capability`，而 `dangling` 是 `*const Packed<#ident>`，这个 cast 要求 `Packed<#ident>: #capability`——这正是 Typst 的约定：能力 trait 实现在 `Packed<元素>` 上。例如：

> [crates/typst-library/.../heading.rs:248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248)：`impl Synthesize for Packed<HeadingElem>`。能力挂在 `Packed<T>` 而非裸 `T` 上。

`fat::vtable` 的实现——**只读指针自身的字节，不碰所指内存**：

> [crates/typst-utils/src/fat.rs:42-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/fat.rs#L42-L54)：`vtable` 函数把胖指针 `transmute_copy` 成 `FatPointer { data, vtable }` 并返回 `vtable`。

`FatPointer` 的内存布局：

> [crates/typst-utils/src/fat.rs:56-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/fat.rs#L56-L64)：`#[repr(C)] struct FatPointer { data, vtable }`，即「数据指针在前、虚表指针在后」。

为什么这是悬垂指针安全的依据，可以形式化为：调用 `fat::vtable` 时，被读取的内存仅是**指针变量自身**那两个字（它们就存在栈上的指针变量里），而非指针**指向**的堆/栈对象。因此 `data` 字是否指向合法对象不影响正确性。

#### 4.3.4 代码实践

**实践目标**：依据代码注释，解释 `create_capable_func` 里用 `NonNull::dangling()` 取虚表指针为何是安全的。

**操作步骤**：

1. 阅读 [elem.rs:683-L693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L683-L693) 中的安全注释：`// Safety: The vtable function doesn't require initialized data, so it's fine to use a dangling pointer.`
2. 对照 [fat.rs:42-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/fat.rs#L42-L54) 的 `vtable` 实现，确认它**只** `transmute_copy` 指针自身、**不解引用** `data`。
3. 用下面的文字组织你的解释。

**预期结果（参考解释）**：

- `dangling` 是 `NonNull::<Packed<Ident>>::dangling()` 得到的指针——它保证**非空、且按 `Packed<Ident>` 对齐**，但**不指向任何已初始化的有效对象**。
- 把它 cast 成 `*const dyn #capability` 时，编译器构造一个胖指针：`data = dangling`（无效也无妨）、`vtable = <Packed<Ident> as #capability> 的真实虚表`。这个 `vtable` 字是编译期就已确定的真实地址。
- `fat::vtable` 用 `transmute_copy` 把胖指针的两个机器字重解释为 `FatPointer { data, vtable }`，然后返回 `vtable`。整个过程**只读取指针变量自身的字节**，从不执行 `*data` 这样的解引用。
- 因此即便 `data` 是悬垂的，也不会触发未定义行为；拿到的 `vtable` 是真实可用的能力虚表地址。

**待本地验证**：可在 typst 仓库内 `cargo expand -p typst-library`（需安装 `cargo-expand`）查看某个带 `Synthesize` 能力的元素（如 `HeadingElem`）展开后的 `capable` 闭包，确认其中确有 `TypeId::of::<dyn Synthesize>` 这条分支。

#### 4.3.5 小练习与答案

**练习 1**：`Construct` 和 `Set` 为什么被列在 `FORBIDDEN` 里，而不是走 `TypeId` 查询？

**参考答案**：它们不是对象安全的（`Self: Sized`，且需要按值返回 `Self` 相关类型），无法做成 `dyn Construct`。更重要的是，它们的 impl 由 4.1 的 `create_construct_impl`/`create_set_impl` 直接生成、由 `NativeElement` 超 trait 静态绑定调用，根本不需要运行时按 `TypeId` 查虚表。

**练习 2**：`Debug`、`PartialEq`、`Hash` 在 `create_native_elem_impl` 里是怎么被「otherwise handled」的（提示：看 u4-l2 的 `create_struct`）？

**参考答案**：`create_struct` 里固定 `#[derive(Hash, Debug, Clone)]`（`Debug` 除非元素自己声明了 `Debug` 能力就不加），所以这三个由标准 `derive` 在编译期解决，运行时直接用派生实现，不需要查能力虚表。

**练习 3**：`capable` 闭包里为什么要先算一个 `dangling`，再在**每条** `if` 里复用它，而不是每条 `if` 各自 `dangling`？

**参考答案**：闭包被调用时只会有**一条** `if` 命中并 `return`，但编译期不知道是哪一条，所以 `dangling` 必须在所有 `if` 之前**统一**算好，供任意一条分支使用。把它提到闭包顶部既避免重复，也保证无论命中哪条分支都拿到同一个合法的悬垂指针。

---

### 4.4 内省能力与 Mathy：create_introspection_capabilities / create_mathy_impl

#### 4.4.1 概念说明

有一组能力不通过 `capable` 闭包查询，而是被打包进一个独立的 `IntrospectionCapabilities` 结构体（三个布尔），交给 `ContentVtable` 单独存放。它们关系到「这个元素能不能被定位、能不能被用户查询、要不要在 PDF 里打标签」：

- `Locatable`：元素会注册进 introspector，可被 `locate`/`query` 找到位置。
- `Unqueriable`：标记为「对用户不可查询」（用户用 `query` 查不到，但内部仍能定位）。
- `Tagged`：在 PDF 文件里打标签。

这三者有一条校验规则：**`Unqueriable` 必须搭配 `Locatable`**——一个连位置都没有的元素，谈「不可查询」没有意义。

此外还有一个特例 `Mathy`：标了 `Mathy` 的元素表示「这是数学语境里的元素」，宏会额外生成一个空的 `impl Mathy for Packed<Self>`（标志位式 trait，无方法）。它之所以单独处理，是因为它既不走 `capable` 闭包，也不在内省三件套里，而是直接生成一个 impl。

#### 4.4.2 核心流程

```
解析阶段：Meta.capabilities 收集所有能力名（包括 Locatable/Unqueriable/Tagged/Mathy）
   │
   ├─ create_capable_func 过滤掉 FORBIDDEN（含 Locatable/Unqueriable/Tagged）
   │
   ├─ create_introspection_capabilities：
   │     找 Locatable/Unqueriable/Tagged → 校验 Unqueriable 需 Locatable
   │     → 产出 IntrospectionCapabilities { locatable, unqueriable, tagged }
   │
   └─ create_mathy_impl（仅当 can("Mathy")）：
         产出 impl Mathy for Packed<Self> {}
```

注意分工：`Locatable`/`Unqueriable`/`Tagged` 同时出现在**两条**路径里——它们被 `FORBIDDEN` 挡在 `capable` 闭包外，却由 `create_introspection_capabilities` 单独处理。这就是 FORBIDDEN 注释里「introspection capabilities that are handled otherwise」的含义。

#### 4.4.3 源码精读

`create_introspection_capabilities` 先用闭包 `find_cap` 在 capabilities 里找三个名字，再校验 `Unqueriable` 必须配 `Locatable`，最后把三个布尔打包：

> [src/elem.rs:704-L727](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L704-L727)：注意 [L711-L715](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L711-L715) 的校验——`if let Some(unqueriable) = unqueriable && locatable.is_none()` 就 `bail!`。

运行时的承载结构：

> [crates/typst-library/.../vtable.rs:318-L326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L318-L326)：`IntrospectionCapabilities { locatable, unqueriable, tagged }`，每个字段的文档注释说明了语义。

`create_mathy_impl` 极短，就是生成一个空 impl：

> [src/elem.rs:729-L733](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L729-L733)：`impl ::typst_library::math::Mathy for Packed<#ident> {}`。

`Mathy` trait 本身就是一个空标志 trait：

> [crates/typst-library/src/math/mod.rs:111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L111)：`pub trait Mathy {}`。它的存在（而非方法）才是数学元素身份的判据。

真实示例二则：

- `StrikeElem`（删除线）只带 `Locatable + Tagged`，`unqueriable` 为 false，校验通过：

  > [crates/typst-library/src/text/deco.rs:154-L169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs#L154-L169)：`#[elem(title = "Strikethrough", since = "forever", Locatable, Tagged)]`，字段 `stroke` 带 `#[fold]`。

- `CounterDisplayElem` 带 `Unqueriable + Locatable`，因为同时有 `Locatable`，校验通过；它生成的 `IntrospectionCapabilities` 为 `{ locatable: true, unqueriable: true, tagged: false }`（见 [counter.rs:685-L701](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L685-L701)）。

`Mathy` 的真实示例——数学对齐点：

> [crates/typst-library/src/math/mod.rs:114-L115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L114-L115)：`#[elem(title = "Alignment Point", since = "forever", Mathy)] pub struct AlignPointElem {}`，触发 `create_mathy_impl`。

#### 4.4.4 代码实践

**实践目标**：根据一个元素的 `#[elem(...)]` 能力列表，预测 `create_introspection_capabilities` 的输出与是否会 `bail!`。

**操作步骤**：

1. 对下面三个假设元素，分别列出它们的 `IntrospectionCapabilities` 三元组，并判断是否触发 [L711-L715](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L711-L715) 的 `bail!`：
   - A：`#[elem(Unqueriable)]`
   - B：`#[elem(Tagged)]`
   - C：`#[elem(Unqueriable, Locatable)]`
2. 与 [vtable.rs:318-L326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L318-L326) 的字段顺序对照。

**预期结果**：

| 元素 | locatable | unqueriable | tagged | 是否 bail! |
| --- | --- | --- | --- | --- |
| A `#[elem(Unqueriable)]` | false | true | false | **是**：Unqueriable 无 Locatable |
| B `#[elem(Tagged)]` | false | false | true | 否 |
| C `#[elem(Unqueriable, Locatable)]` | true | true | false | 否 |

注意 `find_cap` 用的是 `ident == name` 的字符串比较，与能力的**书写顺序无关**——C 里 Locatable 写在后面也能被找到。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Locatable`/`Unqueriable`/`Tagged` 要从 `capable` 闭包里 `FORBIDDEN` 出去，单独用一个布尔结构体表示？

**参考答案**：因为它们是**高频查询的纯布尔标志**（布局阶段会反复问「这元素能不能定位」），用一个紧凑的 `IntrospectionCapabilities` 结构体存放，查询时是一次内存读取 + 布尔判断，比「每次都构造胖指针、比 `TypeId`、取虚表」便宜得多。这是性能驱动的特化路径。

**练习 2**：`Mathy` 走 neither `capable` 闭包 nor 内省结构体，而是直接生成 `impl Mathy for Packed<Self> {}`。这与 4.3 里「能力挂在 `Packed<T>` 上」的约定矛盾吗？

**参考答案**：不矛盾，恰恰一致。`Mathy` 也是一个实现在 `Packed<T>` 上的能力 trait，只是它**没有方法**（标志 trait），因此需要的是「类型层面能不能当成 `Mathy` 用」（比如泛型约束、或被 `dyn Mathy` 收集），由一个空 impl 就够了；运行时若要查它，仍可走 `capable` 闭包（`Mathy` 不在 FORBIDDEN 里），所以两种机制互补、不冲突。

**练习 3**：若用户写 `#[elem(Locatable, Tagged, Locatable)]`（重复 Locatable），会发生什么？

**参考答案**：`find_cap` 用 `iter().find(...)` 找到第一个即返回，重复项被忽略，最终 `locatable = true`。代码里**没有**对重复能力名的校验，所以不会 `bail!`，只是冗余。这与 u1-l3 讲过的 `validate_attrs`「只放行 doc/derive、其余报拼写错误」是不同层面——能力名在 `Meta` 里由 `Punctuated::<Ident, Token![,]>::parse_terminated` 直接收集，不做去重或合法性校验（除内省那条规则外）。

---

## 5. 综合实践

把本讲四块知识串起来，做一个「手工展开」练习。

**任务**：给定下面这个虚构元素（**示例代码**，非项目原有）：

```rust
// 示例代码：用于手工推演宏输出
#[elem(Locatable)]
pub struct NoteElem {
    /// 必填：笔记内容
    #[required]
    body: Content,

    /// 可选具名：背景色
    #[default(Some(Color::WHITE))]
    fill: Option<Color>,

    /// 自定义解析：从输入读 raw 文本再包装
    #[parse(
        let raw = args.eat::<Content>()?;
        raw.unwrap_or_default()
    )]
    #[ghost]
    extra: Content,
}
```

请回答（可参考 [`create`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L248-L277) 与 4.1~4.4 的源码链接）：

1. **是否生成 `impl Construct` / `impl Set`？** 为什么？
   - 答：都生成。因为 capabilities 里既没有 `Construct` 也没有 `Set`（`cannot("Construct")`、`cannot("Set")` 均为真）。`Mathy` 也没有，故不生成 Mathy impl。

2. **`impl Set` 里会有哪些字段的 handler？**
   - `set_fields = construct_fields − required`。`body` 是 required，被排除；`extra` 虽带 `#[parse]` 但 `ghost` 字段属于 `real_fields` 且 `parse.is_some()` 故进 `construct_fields`、又非 required 故进 `set_fields`；`fill` 同样进。所以 Set 里会出现 `fill` 与 `extra` 两条 handler。`extra` 因为有 `#[parse]`，其 `value` 是用户写的 `raw.unwrap_or_default()`（注意它是 `Content`，非 `Option`——这是个**故意留的坑**，见下）。

3. **`impl Construct` 的 `setup` 段长什么样？** 对 `extra` 字段写出大致片段。
   - 按 [`create_construct_impl` L577-L584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L577-L584) 与 [`create_field_parser`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L638-L656)：
     ```rust
     // body（required，默认分支 args.expect）
     let body = args.expect("body")?;
     // fill（具名，默认分支 args.named）
     let fill = args.named("fill")?;
     // extra（#[parse] 覆盖）
     let raw = args.eat::<Content>()?;
     let extra = raw.unwrap_or_default();
     ```

4. **找出一个会让本元素编译失败的「坑」。**
   - 提示：4.1 里讲过 Set 的 handler 是 `if let Some(value) = #value { ... }`。`extra` 的 `value` 是 `raw.unwrap_or_default()`，类型是 `Content`（不是 `Option`），套进 `if let Some(value) = <Content>` 会类型不匹配→编译错误。这说明：**出现在 Set 里的 `#[parse]` 表达式必须产 `Option<T>`**。修正方法是把 parse 改成 `args.eat::<Content>()?`（直接返回 `Option<Content>`），让 `value` 本身就是 Option。

5. **`IntrospectionCapabilities` 是什么？`capable` 闭包里有几条 `if`？**
   - `{ locatable: true, unqueriable: false, tagged: false }`。`capable` 闭包里 0 条——因为 `Locatable` 被 FORBIDDEN，而本元素没有其他非禁能力。

**预期结果**：通过这道题，你把「能力开关 → 是否生成 impl」「字段过滤器 → 参与的字段」「`#[parse]` 覆盖与默认分支」「内省三布尔与 FORBIDDEN」四件事全部走了一遍。

## 6. 本讲小结

- `Construct` 与 `Set` 是 `NativeElement` 的**强制超 trait**；宏默认自动生成二者，除非用户在 `#[elem(...)]` 里列出 `Construct`/`Set` 表示「我自己写」。
- `Construct` 用 `construct_fields`（含 internal+parse 的特例），`Set` 用 `set_fields`（= construct_fields − required）；Set 的 handler 形如 `if let Some(value) = ... { styles.set(...); }`，因此其字段取值必须是 `Option<T>`。
- `create_field_parser` 是「字段属性 → 取参代码」的翻译器：默认按 `variadic→required→positional→named` 四分支短路；带 `#[parse(...)]`（**不带花括号**）则整段覆盖，由 `BlockWithReturn` 切成 `(prefix 语句, expr)`。
- `create_capable_func` 用 `FORBIDDEN` 名单（非对象安全 + 已被别处处理）过滤能力，再为每个剩余能力生成一条 `TypeId` 比较；取虚表用 `NonNull::dangling()` 悬垂指针——安全因为 `fat::vtable` 只读指针自身的虚表字、从不解引用 data。
- `Locatable/Unqueriable/Tagged` 三内省能力不走 `capable` 闭包，而是被打包成 `IntrospectionCapabilities` 三布尔，且 `Unqueriable` 必须搭配 `Locatable`；`Mathy` 是特例，单独生成空 `impl Mathy for Packed<Self>`。

## 7. 下一步学习建议

本讲完成了 `#[elem]` 从「字段解析 → struct 生成 → 虚表注册 → 行为生成」的完整闭环。建议接下来：

1. **阅读 u4-l5（架构总览）**：把 `#[elem]` 与 `#[func]`、`#[ty]`、`#[scope]` 放到同一张「parse → create」架构图下，理解 `def_site_key`、`LazyLock` 延迟初始化、`foundations` 路径简写等横切关注点。
2. **动手用 `cargo expand`**：在 typst 仓库内展开一个真实元素（如 `StrikeElem` 或 `HeadingElem`），对照本讲四个函数，逐行核对宏的真实输出，这是把「读源码」变成「会写宏」的最快路径。
3. **延伸阅读运行时侧**：打开 [`crates/typst-library/src/foundations/content/vtable.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs)，看 `ContentVtable` 如何消费本讲产出的 `capable` 闭包与 `IntrospectionCapabilities`，以及 `fat::vtable` 取出的虚表地址最终如何被 `from_raw_parts` 重新拼成可调用的胖指针——这能帮你把编译期代码生成与运行时调度彻底打通。
