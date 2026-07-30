# #[elem]（二）：结构体与构造方法生成

## 1. 本讲目标

本讲承接 u4-l1 的「解析」阶段。u4-l1 已经把用户写的 `#[elem]` 结构体整理成了 `Elem` / `Field` 两个中间结构；本讲进入「代码生成」的第一站——**结构体本身的改写**与**构造方法**。

学完本讲你应该能够：

1. 说出 `#[elem]` 宏输出的整体形状：一个裸 `struct` + 一段 `const _: () = { ... };` 包裹的所有 `impl`。
2. 根据一个字段的属性（`required` / `synthesized` / 普通 settable），手写出它在生成 struct 里的**存储类型**（裸类型 / `Option` / `Settable<Self, I>`）。
3. 手写 `new()` 的签名（参数只来自 required 字段）与函数体里三种字段各自的初始化方式。
4. 解释 `with_X` 链式构造器对三种字段的三种赋值写法，以及为什么 required 字段不生成 `with_`。
5. 说清 `Field` 常量（`Self::columns` 这种句柄）的来历，以及 `const _: () = { ... };` 匿名作用域的作用。

---

## 2. 前置知识

在开始前，请确认你已经理解以下概念（它们都在 u4-l1 中讲过，这里只做一句话回顾）：

- **`Elem` 与 `Field` 中间结构**：宏把用户的 struct 翻译成 `Elem`（元素级信息）和一组 `Field`（字段级信息）。`Field` 上带着 `required` / `synthesized` / `ghost` / `external` / `internal` / `fold` / `positional` / `variadic` / `i`（索引）等布尔与位置标志。
- **字段过滤器链**：`Elem` 上有若干迭代器方法，把字段按用途分流。本讲最关键的是这一条嵌套链：

  \[
  \text{real\_fields} \;(\text{非 external}) \;\supset\; \text{struct\_fields} \;(\text{且非 ghost}) \;\supset\; \text{accessor\_fields} \;(\text{且非 required})
  \]

  即「真实字段 → 进入 struct 的字段 → 拥有 `with_` 方法的字段」逐层收窄。

- **三种字段生命周期**（贯穿本讲的分类法）：
  - **required**：必填，构造时必须给值（`#[required]` 或 `#[variadic]` 自动推导）。
  - **synthesized**：不在构造参数里出现，由后续阶段「合成」填入（`#[synthesized]`）。
  - **普通 settable**：可被 set 规则设置的字段，绝大多数字段属于此类。

- **`foundations` 简写**：`util.rs` 里的 `foundations` 类型展开成 `::typst_library::foundations`，是运行时 trait/类型的统一入口（见 u1-l3）。本讲里出现的 `Settable`、`Field` 等都来自这个前缀。

- **u4-l1 的产物是本讲的输入**：本讲只读 `Elem`/`Field`，不再重新解析用户源码。

> 本讲**不**涉及 vtable 注册（`create_native_elem_impl` / `create_field_impl`，那是 u4-l3）和 `Construct`/`Set`（那是 u4-l4）。本讲只关心「生成出来的 struct 长什么样、怎么 new、怎么 with_」。

---

## 3. 本讲源码地图

本讲全部代码集中在 [`src/elem.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs)：

| 函数 | 行号 | 作用 |
| --- | --- | --- |
| `create` | 249–277 | 总装：拼出 `struct` + `const _: () = { ... }` 包裹的所有 impl |
| `create_struct` | 280–295 | 生成 struct 定义本身（derive、字段列表） |
| `create_field` | 298–307 | 为单个字段选择 struct 里的存储类型（三形态） |
| `create_inherent_impl` | 310–334 | 生成 `impl Elem { ... }`：`new` + `with_` + `Field` 常量 |
| `create_new_func` | 337–360 | 生成 `new()` 构造函数 |
| `create_with_field_method` | 363–382 | 生成 `with_X` 链式构造器 |
| `real_fields` / `struct_fields` / `accessor_fields` | 59–71 | 字段过滤器链（决定哪些字段参与本讲的生成） |

运行时侧参考 [`crates/typst-library/src/foundations/content/field.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs) 中的 `Settable<E, I>`（行 445 起）。

---

## 4. 核心概念与源码讲解

### 4.1 整体生成架构：`create` 与 `const _` 匿名作用域

#### 4.1.1 概念说明

过程宏的最终产物是一段 `TokenStream`，它会**替换**掉用户写的 `#[elem] struct XxxElem { ... }`。这段产物包含两部分：

1. 一个改写过的 `struct XxxElem { ... }`（字段类型可能和用户写的不一样）。
2. 一大堆 `impl`：inherent impl（`new`/`with_`/`Field` 常量）、`NativeElement` impl、每个字段的 trait impl、可选的 `Construct`/`Set` impl 等。

关键设计：**struct 本身放在模块顶层，而所有 impl 被包进一个 `const _: () = { ... };` 匿名作用域里。**

#### 4.1.2 核心流程

```
create(element)
  ├── create_struct(element)            → struct XxxElem { ... }   （放 const 外）
  ├── create_inherent_impl(element)     → impl XxxElem { new, with_* } + Field 常量
  ├── create_native_elem_impl(element)  → unsafe impl NativeElement   （u4-l3）
  ├── create_field_impl(...)            → 每字段的 FieldData impl     （u4-l3）
  ├── create_construct_impl(...)        → impl Construct             （u4-l4）
  ├── create_set_impl(...)              → impl Set                   （u4-l4）
  └── create_mathy_impl(...)            → impl Mathy                  （u4-l4）
        │
        ▼ 拼装
  struct XxxElem { ... }            ← 模块顶层
  const _: () = {
      /* 上面除 struct 外的全部 impl */
  };
```

注意上图中只有 `struct` 在 `const` 块**之外**，其余 impl 都在块**内**。

#### 4.1.3 源码精读

总装函数 `create`：

[src/elem.rs:249-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L249-L277) —— 调用各子生成器，再用 `quote!` 拼成最终输出。其中 `#struct_` 在 `const` 块之外，`#inherent_impl` 等在块内。

匿名作用域的包裹点：

[src/elem.rs:265-276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L265-L276) —— 源码里直接附了注释：「We use a const block to create an anonymous scope, as to not leak any local definitions.」（用一个 const 块创建匿名作用域，以免泄漏任何局部定义）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认 struct 与 impl 的「内外」分布。

**操作步骤**：
1. 打开 [src/elem.rs:265-276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L265-L276)。
2. 数清楚：`const _: () = { ... };` 内部插值了几个 `#...`？分别对应哪些子生成器？
3. 确认 `#struct_` 出现在 `const` 之前。

**需要观察的现象**：`#construct_impl`、`#set_impl`、`#mathy_impl` 三个是 `Option<TokenStream>`（由 `element.cannot("Construct").then(...)` 等产生），它们在 `quote!` 里作为 `#construct_impl` 插值时，若为 `None` 则展开为空——这是「按能力开关 impl」的惯用法。

**预期结果**：`const` 块内有 6 个插值位（inherent / native_elem / field_impls / construct / set / mathy），其中后三个可能为空。

> 待本地验证：你可以用 `cargo expand`（需在依赖 typst-macros 的 crate 里）展开某个真实元素，亲眼看到 `const _: () = { ... };` 的形状。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `struct` 要放在 `const` 块**外面**，而 impl 放**里面**？反过来行不行（struct 在里、impl 在外）？

**答案**：`struct` 是用户模块里其他人要按名字引用的公开类型（比如 `TableElem`），必须在模块顶层可见；而 impl（无论是 inherent 还是 trait impl）在 Rust 里是**全局生效**的，写在哪个作用域都一样能对类型生效。如果把 struct 放进 `const _: () = { ... }` 里，它就成了一个匿名作用域内的私有项，外部再也无法命名这个类型，函数签名、变量类型都会找不到它。所以只能 struct 在外、impl 在内。

**练习 2**：`const _: () = { ... };` 里的「局部定义」如果不被这个块包住，最可能造成什么问题？

**答案**：宏内部生成的辅助项（如 `static STORE`、各 `impl` 块里若再有 helper）会以具名 item 泄漏到用户的模块命名空间，可能和用户自己定义的同名项冲突，或污染模块的公开 API。const 块把它们关进匿名作用域，对外只暴露 struct 本身。

---

### 4.2 结构体生成：`create_struct` 与字段存储三形态

#### 4.2.1 概念说明

用户写的字段类型，和最终 struct 里**真正存储**的类型，往往不一样。最典型的例子：一个普通 settable 字段，用户写 `pub columns: TrackSizings`，但生成出来的 struct 里它实际是 `Settable<TableElem, 0>`。这是因为 settable 字段需要支持「未设置 / 直接设置 / 被 set 规则覆盖」三种状态，单靠一个裸 `TrackSizings` 表达不了。

`create_struct` 负责**重写**整个 struct：换 derive、换字段类型、换文档。

#### 4.2.2 核心流程

`create_struct` 对每个进入 struct 的字段（即 `struct_fields()`，排除 external 与 ghost）调用 `create_field`，按字段属性选择三种存储形态之一：

| 字段类别 | 判定条件 | struct 里的存储类型 | 直觉 |
| --- | --- | --- | --- |
| required | `field.required` | 裸类型 `#ty` | 必填，构造时直接给值，无需包装 |
| synthesized | `field.synthesized` | `::std::option::Option<#ty>` | 暂时缺失，后续合成填入，用 `Option` 表达「可能还没有」 |
| 普通 settable | 其余（非 required、非 synthesized） | `Settable<Self, #i>` | 需表达「未设/已设/被覆盖」，用专门的 `Settable` 容器 |

其中 `#i` 是字段在排序后的索引（u4-l1 中 `fields.sort_by_key(internal)` 后分配），它作为 **const generic 参数**编进类型 `Settable<Self, #i>`，让每个 settable 字段拥有独一无二的类型——这是后续 vtable 槽位定位的基础（u4-l3 详讲）。

> 为什么 `required` 和 `synthesized` 不用 `Settable`？因为它们都不参与「set 规则」语义：required 是一次性必填值，synthesized 是宏/运行时内部填充值，二者都不需要 set 规则的「叠加/覆盖」能力，所以用最简单的裸类型或 `Option` 即可。这也呼应了 u4-l1 的互斥校验：`required ∪ synthesized` 与 `default ∪ fold ∪ ghost` 互斥——它们根本不进 settable 通道。

#### 4.2.3 源码精读

struct 定义生成：

[src/elem.rs:280-295](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L280-L295) —— 注意几个细节：

- `let debug = element.cannot("Debug").then(|| quote! { Debug, });` —— **derive 列表由宏接管**：永远 derive `Hash`、`Clone`；只有当元素**没有**自定义 `Debug` 能力时才追加 `Debug`（否则会与手写的 `Debug` impl 冲突）。用户在原 struct 上写的 `#[derive(...)]` 会被丢弃。
- `#[doc = #oneliner]` —— struct 的 rustdoc 被 `oneliner(docs)` 生成的一行摘要覆盖（完整文档另存进 vtable）。
- `#(#fields,)*` —— 字段来自 `element.struct_fields().map(create_field)`，**ghost 字段不进 struct**，external 字段也不进（external 仅存于文档与 vtable）。

单个字段的存储类型选择：

[src/elem.rs:298-307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L298-L307) —— 三个 `if/else if/else` 分支精确对应上表的三种形态。注意 `Settable` 形态里用到了 `#i`，另两种没有。

字段过滤器（决定哪些字段进 struct）：

[src/elem.rs:64-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L64-L66) —— `struct_fields = real_fields 中且非 ghost`；而 [src/elem.rs:59-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L59-L61) 的 `real_fields = 非 external`。

运行时侧的 `Settable` 定义，佐证它就是一层 `Option`：

[crates/typst-library/.../field.rs:445-456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L445-L456) —— `pub struct Settable<E, const I: u8>(Option<E::Type>)`，`new()` 返回 `Self(None)`，`set()` 写入 `Some(value)`。可见 settable 字段在 struct 里本质上也是一个「可空」槽，但它额外携带 const generic 索引 `I` 与 trait 约束 `SettableProperty<I>`，从而能接入 set 规则系统。

#### 4.2.4 代码实践

**实践目标**：用一个真实元素验证三形态。

**操作步骤**：
1. 打开 [`crates/typst-library/src/model/table.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/table.rs) 的 `TableElem` 定义（行 137 起）。
2. 对它的每个字段，按 u4-l1 的属性推导规则，判断属于 required / synthesized / 普通 settable 哪一类：
   - `children: Vec<TableChild>` 带 `#[variadic]` → 自动 required + positional。
   - `grid: Arc<CellGrid>` 带 `#[internal] #[synthesized]` → synthesized。
   - `columns: TrackSizings`（无属性）→ 普通 settable。
   - `gutter` 带 `#[external]` → 不进 struct。
3. 预测这四个字段在生成 struct 里的存储类型。

**需要观察的现象 / 预期结果**：

| 字段 | 类别 | 生成 struct 里的存储类型 |
| --- | --- | --- |
| `children` | required | `children: Vec<TableChild>`（裸类型） |
| `grid` | synthesized | `grid: Option<Arc<CellGrid>>`（包 Option） |
| `columns` | 普通 settable | `columns: Settable<TableElem, #i>`（i 由排序决定） |
| `gutter` | external | **不出现在 struct 里** |

> 待本地验证：用 `cargo expand` 展开 `TableElem`，确认 `grid` 真的变成了 `Option<...>`、`gutter` 确实消失、`columns` 变成了 `Settable<...>`。

#### 4.2.5 小练习与答案

**练习 1**：如果一个字段同时是 `#[ghost]`，它会出现在生成的 struct 里吗？为什么？

**答案**：不会。`struct_fields()` 在 `real_fields()` 基础上再过滤掉 `ghost`（[src/elem.rs:64-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L64-L66)）。ghost 字段只存在于文档与 vtable 中，struct 里没有它的存储——这正是「ghost（幽灵）」之名的由来。

**练习 2**：为什么 `create_field` 里只有 settable 分支用到了字段索引 `#i`，而 required / synthesized 分支不用？

**答案**：因为只有 settable 字段的存储类型是 `Settable<Self, #i>`，这个 const generic 索引让每个 settable 字段成为独一无二的单态化类型，从而在编译期就能把「字段身份」编进类型系统，供 vtable（u4-l3）按索引定位槽位。required 用裸类型、synthesized 用 `Option`，都不需要这种「按字段编号区分类型」的能力，所以不携带 `#i`。

---

### 4.3 构造方法 `new()`：`create_new_func`

#### 4.3.1 概念说明

每个元素都会获得一个 `pub fn new(...) -> Self`。它的设计哲学是：**只有 required 字段需要调用方提供，其余字段由 `new` 内部给出默认初始状态。** 因此 `new` 的参数列表只包含 required 字段，函数体则负责把**所有** struct 字段都初始化好。

#### 4.3.2 核心流程

```
new( <所有 required 字段, 形如 ident: ty> ) -> Self {
    Self {
        <required 字段>:   直接用同名参数 ident,
        <synthesized 字段>: None,
        <普通 settable 字段>: Settable::new(),   // 即空的 Settable(None)
        ...
    }
}
```

两个独立的映射：
- **参数列表**：遍历 `struct_fields()` 再 `filter(required)`，每个生成 `ident: ty`。
- **函数体字段初始化**：遍历**全部** `struct_fields()`（不只 required），按三态分别生成初始化表达式。

| 字段类别 | `new()` 函数体里的初始化 |
| --- | --- |
| required | `#ident`（取自传入参数，简写） |
| synthesized | `#ident: None` |
| 普通 settable | `#ident: Settable::new()` |

#### 4.3.3 源码精读

[src/elem.rs:337-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L337-L360) —— `create_new_func`：

- 行 338–341：`params` 只取 `struct_fields().filter(required)`，map 成 `#ident: #ty`。**只有 required 字段成为参数。**
- 行 343–352：`fields` 遍历**全部** `struct_fields()`，按 `required / synthesized / 其余` 三分支生成初始化项。
- 行 354–359：最终拼出 `pub fn new(#(#params),*) -> Self { Self { #(#fields,)* } }`。

注意 `Settable::new()` 在运行时侧就是 `Self(None)`（见 4.2.3 引用的 [field.rs:453-456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L453-L456)），即一个「未设置」的空槽。

#### 4.3.4 代码实践

**实践目标**：手写一个最小元素的 `new()`。

给定（示例代码，非项目原有）：

```rust
// 示例代码：假设的元素定义
#[elem]
pub struct BoxElem {
    #[required]
    pub body: Content,          // required
    #[default(Color::BLACK)]
    pub fill: Color,            // 普通 settable，带默认值
    #[synthesized]
    pub size: Abs,              // synthesized
}
```

**操作步骤**：
1. 判断每个字段类别：`body` → required；`fill` → 普通 settable（注意 `#[default]` 不改变类别，仍是 settable）；`size` → synthesized。
2. 写出 `new` 的参数列表：只有 `body`。
3. 写出函数体三字段初始化。

**预期结果**（手写展开）：

```rust
// 示例代码：宏生成的 new()
pub fn new(body: Content) -> Self {
    Self {
        body,                       // required：取参数
        fill: Settable::new(),      // 普通 settable：空槽（默认值在别处）
        size: None,                 // synthesized：None
    }
}
```

**需要思考**：`fill` 带 `#[default(Color::BLACK)]`，为什么 `new()` 里却是 `Settable::new()` 而不是 `Color::BLACK`？

> 解答：`#[default]` 提供的是「当字段未被任何 set 规则设置时」的回退值，它被记录在字段的 vtable 数据里（u4-l3 的 `create_field_impl` 会把 `default` 注入 `FieldData`），而不是在 struct 实例里。`new()` 创建的是一个「全未设置」的实例，读取时若发现 `Settable` 为空，才去查 vtable 里的默认值。这样设计让「实例默认值」与「set 规则默认值」共用一条查找链。

#### 4.3.5 小练习与答案

**练习 1**：如果 `BoxElem` 还有第四个字段 `#[variadic] pub items: Vec<Item>`，`new()` 的参数列表会变成什么？

**答案**：`#[variadic]` 会自动推导出 `required`（见 u4-l1 的优先级链），所以 `items` 也是 required，会进入参数列表。`new` 签名变成 `pub fn new(body: Content, items: Vec<Item>) -> Self`。

**练习 2**：为什么 `new` 的函数体要初始化**全部** struct 字段，而不只是 required 字段？

**答案**：Rust 要求构造 `Self { ... }` 时必须给**每一个**字段赋值，否则编译失败。required 之外的 synthesized 与 settable 字段虽然不在参数里，但也必须在 `Self { ... }` 里被初始化（分别给 `None` 与 `Settable::new()`），否则 struct 无法被构造。

---

### 4.4 链式构造器 `with_X`：`create_with_field_method`

#### 4.4.1 概念说明

`new()` 只能传 required 字段，那 settable / synthesized 字段怎么在 Rust 侧便捷地设置？答案是宏为它们生成的**链式 builder**：每个这样的字段获得一个 `with_X(value) -> Self` 方法，消费并返回 `Self`，可串连：

```rust
BoxElem::new(body).with_fill(Color::RED).with_size(Abs::pt(10.0))
```

#### 4.4.2 核心流程

`with_X` 只为 `accessor_fields()` 生成，即 `struct_fields()` 中**非 required** 的字段。三类字段的赋值写法不同：

| 字段类别 | 是否生成 `with_X` | 函数体赋值语句 |
| --- | --- | --- |
| required | ❌ 不生成（已在 `new` 参数里） | — |
| synthesized | ✅ 生成 | `self.#ident = Some(#ident)` |
| 普通 settable | ✅ 生成 | `self.#ident.set(#ident)` |

方法签名固定为 `fn with_{ident}(mut self, ident: ty) -> Self`，参数名与字段同名，类型为字段的**原始类型** `#ty`（不是 `Settable` 也不是 `Option`——包装由赋值语句完成）。

#### 4.4.3 源码精读

[src/elem.rs:363-382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L363-L382) —— `create_with_field_method`：

- 行 364：`let Field { vis, ident, with_ident, name, ty, .. } = field;`，其中 `with_ident` 在 u4-l1 的 `parse_field` 里被预生成为 `format_ident!("with_{ident}")`（见 [src/elem.rs:216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L216)）。
- 行 367–373：三分支选择赋值表达式：required 直接覆写裸字段、synthesized 包 `Some`、settable 调 `.set()`。
- 行 375–381：拼出带文档注释的 `fn with_X(mut self, X: ty) -> Self { ...; self }`。

谁会获得 `with_` 方法？由 `create_inherent_impl` 里的 `element.accessor_fields().map(create_with_field_method)` 决定（见下节）。`accessor_fields` 的定义：

[src/elem.rs:69-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L69-L71) —— `struct_fields 中且非 required`。所以 required 字段被排除在外。

#### 4.4.4 代码实践

**实践目标**：为 4.3 的 `BoxElem` 手写 `with_` 方法。

**操作步骤**：对 `fill`（settable）和 `size`（synthesized）各写一个；`body`（required）不写。

**预期结果**（手写展开，示例代码）：

```rust
// 示例代码：宏生成的 with_ 方法
impl BoxElem {
    /// Builder-style setter for the `fill` field.
    pub fn with_fill(mut self, fill: Color) -> Self {
        self.fill.set(fill);   // settable：调 Settable::set
        self
    }

    /// Builder-style setter for the `size` field.
    pub fn with_size(mut self, size: Abs) -> Self {
        self.size = Some(size); // synthesized：包 Some 后直接赋值
        self
    }
}
```

**需要观察的现象**：
- `body`（required）**没有** `with_body`——它只能通过 `new(body)` 提供。
- 参数类型是 `Color` / `Abs`（原始类型），不是 `Settable<...>` 或 `Option<...>`。

> 待本地验证：在 typst-library 里搜索 `TableElem::new(` 的调用点，观察它如何链式 `.with_columns(..).with_align(..)` 拼装，验证 settable 字段的 builder 用法。

#### 4.4.5 小练习与答案

**练习 1**：为什么 synthesized 字段的 `with_` 用 `self.size = Some(size)`，而 settable 用 `self.fill.set(fill)`？

**答案**：synthesized 字段在 struct 里的存储类型是 `Option<#ty>`（4.2），所以赋值时要包一层 `Some`，直接整体替换。settable 字段的存储类型是 `Settable<Self, I>`，它是一个有 `.set()` 方法的容器（[field.rs:459-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L459-L461)），调 `.set()` 把内部从 `None` 置为 `Some(value)`，且保留了 settable 容器的类型身份（用于 set 规则系统）。两者的赋值写法不同，根因是它们的存储类型不同。

**练习 2**：如果一个字段既是 required 又想用 builder 风格设置，宏会怎么做？

**答案**：宏不会为 required 字段生成 `with_` 方法（`accessor_fields` 过滤掉了 required）。required 字段只能通过 `new(..)` 的参数提供。这是设计上的有意限制：required 表示「构造时必须明确给出」，没有「省略后用默认」的语义，所以不提供 builder 兜底。

---

### 4.5 `Field` 常量与 inherent impl：`create_inherent_impl`

#### 4.5.1 概念说明

除了 `new` 和 `with_`，元素还需要一种**在类型层面引用某个字段**的能力——比如 `styles.set(Self::columns, value)` 里的 `Self::columns`。这个 `columns` 不是字段本身，而是一个**常量句柄** `Field<Self, I>`，它携带字段名、索引等元信息，是 set 规则、查询、introspection 等系统的统一「字段凭证」。

`create_inherent_impl` 把三样东西装进 inherent impl：
1. `new`（来自 `create_new_func`）。
2. 所有 `with_` 方法（来自 `create_with_field_method`）。
3. 每个 real field 的 `Field` 常量。

#### 4.5.2 核心流程

```
impl BoxElem {
    pub fn new(...) -> Self { ... }
    pub fn with_fill(...) -> Self { ... }
    pub fn with_size(...) -> Self { ... }
}
#[expect(non_upper_case_globals)]
impl BoxElem {
    pub const body:   Field<Self, 0> = Field::new();
    pub const fill:   Field<Self, 1> = Field::new();
    pub const size:   Field<Self, 2> = Field::new();
}
```

注意它生成了**两个**独立的 `impl BoxElem { ... }` 块：一个装方法，一个装常量。

**Field 常量的覆盖范围**：遍历的是 `real_fields()`（非 external），**不是** `struct_fields()`。也就是说：ghost 字段、internal 字段、synthesized 字段都会获得 `Field` 常量；唯独 **external 字段没有**（因为 external 只存在于文档/vtable，没有真实字段身份）。

> 这里有个微妙点：`ghost` 字段虽然不进 struct（4.2 练习 1），但它**仍会**获得一个 `Field` 常量——因为 `real_fields` 不过滤 ghost。这与「ghost 只在文档/vtable 里」是一致的：`Field` 常量正是 vtable 那一侧的句柄。

#### 4.5.3 源码精读

[src/elem.rs:310-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L310-L334) —— `create_inherent_impl`：

- 行 313：装入 `new_func`。
- 行 314：`element.accessor_fields().map(create_with_field_method)` —— 装入所有 `with_` 方法。
- 行 316–322：`style_consts` —— 对 `element.real_fields()` 每个字段生成一条 `pub const #ident: Field<Self, #i> = Field::new();`。
- 行 324–333：拼出**两个** impl 块：第一个含 `new` + `with_`；第二个带 `#[expect(non_upper_case_globals)]`，含所有 Field 常量。

为什么需要 `#[expect(non_upper_case_globals)]`？

[src/elem.rs:329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L329) —— Rust 约定常量用 `SCREAMING_SNAKE_CASE`（如 `BODY`），但这些 Field 常量直接复用了字段的 kebab/lower 名（如 `body`、`column_gutter`），以便用户写 `Self::columns` 时与字段名一致。这与字段名风格冲突，会触发 `non_upper_case_globals` 告警，故用 `#[expect(...)]` 显式抑制。

`with_ident` 的预生成位置（佐证 `with_X` 名字来源）：

[src/elem.rs:216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L216) —— `with_ident: format_ident!("with_{ident}")`，在 u4-l1 的 `parse_field` 阶段就准备好了。

#### 4.5.4 代码实践

**实践目标**：手写 `BoxElem` 的完整 inherent impl（含 Field 常量）。

沿用 4.3 的 `BoxElem`（字段：`body` required、`fill` settable、`size` synthesized），假设排序后索引为 `body=0, fill=1, size=2`（均为非 internal，顺序即声明顺序）。

**操作步骤**：
1. 列出 `real_fields()`：三个字段都非 external，全部入选。
2. 列出 `accessor_fields()`：去掉 required 的 `body`，剩 `fill`、`size`。
3. 写出两个 impl 块。

**预期结果**（示例代码）：

```rust
// 示例代码：宏生成的 inherent impl
impl BoxElem {
    pub fn new(body: Content) -> Self { /* 见 4.3 */ }

    pub fn with_fill(mut self, fill: Color) -> Self { self.fill.set(fill); self }
    pub fn with_size(mut self, size: Abs) -> Self { self.size = Some(size); self }
}

#[expect(non_upper_case_globals)]
impl BoxElem {
    pub const body: ::typst_library::foundations::Field<Self, 0>
        = ::typst_library::foundations::Field::new();
    pub const fill: ::typst_library::foundations::Field<Self, 1>
        = ::typst_library::foundations::Field::new();
    pub const size: ::typst_library::foundations::Field<Self, 2>
        = ::typst_library::foundations::Field::new();
}
```

**需要观察的现象**：
- `body` 虽是 required、没有 `with_body`，却**有** `Field` 常量（因为常量遍历 `real_fields`，不过滤 required）。
- 三个常量的 const generic 索引 `0/1/2` 与字段在 struct 里的索引一致。
- 方法块与常量块分离，常量块单独挂了 `#[expect(non_upper_case_globals)]`。

#### 4.5.5 小练习与答案

**练习 1**：`TableElem` 里有一个 `#[external] pub gutter: TrackSizings` 字段。它会获得 `Field` 常量吗？会有 `with_gutter` 方法吗？

**答案**：都没有。
- Field 常量遍历 `real_fields()`，它过滤掉了 external（[src/elem.rs:59-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L59-L61)），所以 `gutter` 不获得常量。
- `with_` 方法走 `accessor_fields ⊂ struct_fields ⊂ real_fields`，external 在最外层就被排除，所以也没有 `with_gutter`。
- 这完全契合 external 的语义：它只在 Typst 侧的文档与 vtable 里「有名」，在 Rust struct 里无实体。

**练习 2**：为什么把 Field 常量放进**单独的** impl 块，并单独挂 `#[expect(non_upper_case_globals)]`，而不是和 `new`/`with_` 混在一个块里？

**答案**：因为 `#[expect(non_upper_case_globals)]` 只对常量的命名风格告警有意义。如果把它挂到包含方法的 impl 块上，作用范围会不必要地扩大（虽然方法名不受这条 lint 影响，但保持属性的作用域最小、最精确是好习惯）；分开两个块也让「方法」与「字段句柄常量」这两类成员在生成代码里一目了然，便于阅读和定位。这是代码可维护性的考量，而非语法强制。

---

## 5. 综合实践

把 4.2–4.5 串起来，完成一次完整的「手写展开」。

**任务**：给定下面这个假想的元素定义（示例代码，综合了本讲所有字段类别），**完整手写**宏生成的 struct 定义、`new`、`with_` 方法与 Field 常量。

```rust
// 示例代码：综合实践用元素
#[elem]
pub struct QuoteElem {
    #[required]
    pub body: Content,

    /// Whether to draw quotation marks.
    #[default(true)]
    pub quotes: bool,

    #[internal]
    #[synthesized]
    pub loc: Location,
}
```

**要求**：

1. 写出生成的 `struct QuoteElem { ... }`，三个字段各自的存储类型必须正确（注意 `loc` 是 synthesized、`quotes` 是 settable、`body` 是 required）。
2. 写出 `new(body: Content) -> Self` 的完整函数体。
3. 写出所有 `with_` 方法（哪些字段有、哪些没有）。
4. 写出 Field 常量块（含 `#[expect(...)]` 与三个常量，索引按声明顺序）。
5. 指出这段生成代码中，哪些部分会被包进 `const _: () = { ... };`，哪些不会。

**参考答案**（示例代码）：

```rust
// (1) struct —— 在 const 块【外】
#[doc = "…oneliner…"]
#[derive(Hash, Clone)]
#[expect(rustdoc::broken_intra_doc_links)]
pub struct QuoteElem {
    pub body: Content,                                    // required：裸类型
    pub quotes: ::typst_library::foundations::Settable<Self, 1>, // settable
    pub loc: ::std::option::Option<Location>,             // synthesized：Option
}
// 注：body 索引为 0，quotes 为 1，loc 为 2（按声明顺序，均非 internal）

const _: () = {
    // (2)+(3)+(4) 全部 impl —— 在 const 块【内】
    impl QuoteElem {
        pub fn new(body: Content) -> Self {
            Self {
                body,                    // required：取参数
                quotes: ::typst_library::foundations::Settable::new(), // settable：空槽
                loc: None,               // synthesized：None
            }
        }
        pub fn with_quotes(mut self, quotes: bool) -> Self { self.quotes.set(quotes); self }
        pub fn with_loc(mut self, loc: Location) -> Self { self.loc = Some(loc); self }
        // 注意：没有 with_body（required 不生成 with_）
    }

    #[expect(non_upper_case_globals)]
    impl QuoteElem {
        pub const body: ::typst_library::foundations::Field<Self, 0>
            = ::typst_library::foundations::Field::new();
        pub const quotes: ::typst_library::foundations::Field<Self, 1>
            = ::typst_library::foundations::Field::new();
        pub const loc: ::typst_library::foundations::Field<Self, 2>
            = ::typst_library::foundations::Field::new();
    }

    // 此外还有 NativeElement / 各 FieldData / Construct / Set 等 impl（本讲不展开，见 u4-l3、u4-l4）
};
```

**自查清单**：
- `body`：required → struct 里裸类型、进 `new` 参数、无 `with_`、**有** Field 常量。
- `quotes`：settable → struct 里 `Settable`、不在 `new` 参数、`with_quotes` 用 `.set()`、有 Field 常量。
- `loc`：synthesized → struct 里 `Option`、不在 `new` 参数、`with_loc` 用 `= Some(...)`、`new` 里初始化为 `None`、有 Field 常量。
- struct 在 `const` 外，所有 impl 在 `const` 内。

---

## 6. 本讲小结

- `create` 的输出分两段：裸 `struct` 在模块顶层，**所有** impl 被包进 `const _: () = { ... };` 匿名作用域，以免辅助定义泄漏进用户模块命名空间。
- 字段在 struct 里有**三种存储形态**：required → 裸类型；synthesized → `Option<ty>`；普通 settable → `Settable<Self, I>`（携带 const generic 索引 `I`）。
- `create_struct` **接管 derive**（恒有 `Hash`/`Clone`，按需 `Debug`），且 `struct_fields` 过滤掉了 ghost 与 external，二者不出现在 struct 里。
- `new()` 的**参数只来自 required 字段**；函数体按三态分别用「取参数 / `None` / `Settable::new()`」初始化全部字段。
- `with_X` 链式构造器只为 `accessor_fields`（非 required 的 struct 字段）生成，赋值按 settable（`.set()`）与 synthesized（`= Some(...)`）两种写法区分。
- **Field 常量**（`Self::columns` 这种 `Field<Self, I>` 句柄）为所有 `real_fields`（非 external，含 ghost/synthesized/required）生成，单独放在带 `#[expect(non_upper_case_globals)]` 的 impl 块里。

---

## 7. 下一步学习建议

本讲完成了「struct 与构造方法」的生成，产出了一个可被 `new`/`with_` 构造、可按 `Self::field` 引用的 Rust 类型。但这只是元素生成的「皮」——还没有把它注册成 Typst 运行时认识的元素。

- **u4-l3（#[elem] 三：NativeElement 与字段 vtable）** 将讲解 `create_native_elem_impl` 如何用 vtable 把元素注册进运行时，以及 `create_field_impl` 如何为每个字段按属性生成 `RequiredField`/`SettableField`/`SynthesizedField`/`ExternalField`/`SettableProperty` 的 trait impl 与 `FIELD` 常量。本讲里那个 const generic 索引 `I` 如何对应到 vtable 槽位，将在那里揭晓。
- **u4-l4（#[elem] 四：Construct/Set 与能力系统）** 将讲解 `create_construct_impl` / `create_set_impl` 如何从 `Args` 解析字段值并填入 struct（即 synthesized 字段「被合成」、settable 字段「被 set」的真正发生地），以及 `#[parse({ ... })]` 自定义解析块如何改写默认取参逻辑。

建议在进入 u4-l3 前，先回到本讲的「综合实践」自测：能否不查答案地写出三种存储形态与 `new`/`with_` 的展开。这是理解 vtable 注册与字段 trait impl 的前置直觉。
