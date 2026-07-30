# #[elem]（三）：NativeElement 与字段 vtable

## 1. 本讲目标

u4-l2 把 `#[elem]` 的产物推进到了「一个可以被 `new` / `with_` 构造、可以用 `Self::field` 引用的 Rust 类型」。但这个类型到目前为止还只是「一段普通 Rust 代码」——Typst 运行时并不认识它。本讲解决的就是**注册**这件事：把上一步生成的类型，连同它的每一个字段，登记进运行时的「虚表（vtable）」体系。

换句话说，u4-l2 生成的是「元素的肉体」，本讲生成的是「元素的身份证 + 每个字段的存取能力」。

学完本讲你应该能够：

1. 说出 `create_native_elem_impl` 生成的 `unsafe impl NativeElement` 的整体形状：一个 `const ELEM: Element`，由 `Element::from_vtable(&VTABLE)` 构造，而 `VTABLE` 是一个 `static` 的 `ContentVtable`。
2. 解释 `field_id` 闭包如何把「字段名字符串」映射到「字段索引 `u8`」，以及字段 vtable 切片 `&[FieldVtable]` 如何按属性为每个字段选择不同的 `XxxFieldData::<Self, i>::vtable()`。
3. 根据 u4-l1 / u4-l2 的字段标志，手写 `create_field_impl` 为一个 required 字段和一个普通 settable 字段分别生成的 `impl` 与 `FIELD` 常量。
4. 说清 `with_fold`、`RefableProperty`、`OnceLock` slot 三者的「条件生成」逻辑，并能解释**为什么带 `#[fold]` 的字段不再实现 `RefableProperty`**。
5. 概述 `create_capable_func` 如何用 `FORBIDDEN` 名单与「悬垂指针取虚表」的技巧把能力（capability）登记进元素。

> 本讲**不**展开 `create_construct_impl` / `create_set_impl` 里「从 `Args` 解析字段值」的细节（那是 u4-l4），只关心「元素与字段如何被注册成运行时可识别的元数据」。

---

## 2. 前置知识

开始前，请确认你已掌握以下概念（均在 u4-l1 / u4-l2 讲过，这里一句话回顾）：

- **`Elem` / `Field` 中间结构**：`Field` 上携带 `i`（字段索引，`u8`）、`required` / `synthesized` / `ghost` / `external` / `internal` / `fold` / `positional` / `variadic` / `name` / `docs` / `default` 等标志。本讲只读这些标志，不再重新解析。
- **const generic 索引 `I`**：u4-l2 里 settable 字段在 struct 中存为 `Settable<Self, I>`，那个 `I` 就是 `field.i`。本讲会看到这个 `I` 同样是字段在 vtable 切片里的**槽位编号**——这是连接「存储」与「注册」的关键。
- **`Field::new()` 句柄**：u4-l2 为每个 `real_field` 生成了 `const IDENT: Field<Self, I> = Field::new();`（如 `Self::columns`）。本讲会看到这个零大小句柄如何与字段 vtable 配合。
- **`foundations` 简写**：`util.rs` 里的 `foundations` 展开成 `::typst_library::foundations`（见 u1-l3）。本讲大量出现的 `NativeElement`、`ContentVtable`、`RequiredField`、`SettableField` 等都在这个前缀下。
- **什么是 vtable**：Rust 在生成 trait 对象（`dyn Trait`）时，会在背后维护一张「虚函数表」，存放每个方法的函数指针。Typst 没有直接用编译器自带的 trait 对象，而是**手写了一张自定义 vtable**（`ContentVtable`），原因下一节解释。

> 阅读提示：本讲的「宏侧」代码全在 `src/elem.rs`，「运行时侧」契约类型在 `crates/typst-library/src/foundations/content/` 下。宏只是**按契约填表**，真正干活的是运行时；理解契约能让你反过来理解宏为什么这样生成。

---

## 3. 本讲源码地图

宏侧全部集中在 [`src/elem.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs)：

| 函数 | 行号 | 作用 |
| --- | --- | --- |
| `create` | [249–277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L249-L277) | 总装，决定生成哪些 impl |
| `create_inherent_impl` | [309–334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L309-L334) | 生成 `Field::new()` 句柄常量（u4-l2 已讲，本讲回顾其角色） |
| `create_native_elem_impl` | [384–466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L384-L466) | **本讲主角一**：生成 `unsafe impl NativeElement`，组装元素 vtable |
| `create_field_impl` | [468–572](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L468-L572) | **本讲主角二**：为每个字段按属性生成 trait impl 与 `FIELD` 常量 |
| `create_capable_func` | [658–702](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L658-L702) | 生成能力查询闭包（含 `FORBIDDEN` 名单与悬垂指针取虚表） |
| `create_introspection_capabilities` | [704–727](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L704-L727) | 生成 `Locatable` / `Unqueriable` / `Tagged` 三态元数据 |

运行时侧契约（同仓库 `typst-library`，按需参考）：

| 类型 | 位置 | 作用 |
| --- | --- | --- |
| `Element` / `from_vtable` | [element.rs:20–32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L20-L32) | 元素句柄，内部是 `Static<ContentVtable>` 指针 |
| `NativeElement` trait | [element.rs:230–244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L230-L244) | 宏生成的 `unsafe impl` 目标，要求 `const ELEM: Element` |
| `LazyElementStore` | [element.rs:216–228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L216-L228) | 每个元素独有的懒初始化存储（`scope` / `params`） |
| `ContentVtable` | [vtable.rs:80–141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L80-L141) | 自定义元素虚表，含元数据 + 方法指针 + 字段子虚表切片 |
| `RequiredField` 等 6 个字段 trait | [field.rs:53–437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L53-L437) | 每个字段要实现的 trait，提供 `FIELD` 常量与 `vtable()` |

---

## 4. 核心概念与源码讲解

### 4.1 元素注册：create_native_elem_impl 与 vtable

#### 4.1.1 概念说明

运行时面对的是**类型擦除**后的内容（`Content` / `RawContent`）：它不知道自己内部装的是 `TextElem` 还是 `ImageElem`，只知道「我有一张虚表指针」。这张虚表就是 `ContentVtable`——它把「这个元素叫什么名字、有哪些字段、怎么构造、怎么 set、支持哪些能力、怎么 clone/hash/debug」全部以**函数指针 + 静态数据**的形式固化下来。

为什么 Typst 不直接用 Rust 自带的 `dyn Trait`？运行时 [`vtable.rs` 顶部注释](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L1-L15)给了两条理由：

1. 自定义 vtable 能存放一个**字段子虚表的切片**（`&[FieldVtable]`），而标准 trait 对象做不到。
2. 自定义 vtable 既能存方法指针，也能存**纯数据**（名字、文档、版本），访问这些数据不必走动态派发。

第三条隐含的好处：因为 vtable 指针背后是 `static` 变量，运行时可以直接**比较两个裸 vtable 指针**来判断两个内容是不是同一种元素（`is` 判断无需动态派发）。

宏的职责就是：**为每个 `#[elem]` 类型生成一张这样的静态 vtable，并用 `from_vtable` 包成 `Element` 常量**。这就是 `create_native_elem_impl` 干的事。

#### 4.1.2 核心流程

`create_native_elem_impl` 先收集拼装 vtable 所需的全部零件，再用 `quote!` 一次性组装：

```
1. def_site_key = 元素 ident 的字符串（用于 IDE 跳转定位）
2. since      → Some(版本) 或 None
3. fields     → 字段 vtable 切片：逐字段选 XxxFieldData::<Self, i>::vtable()（见 4.2）
4. field_id   → 名字→索引 闭包（见 4.2）
5. capable_func        → 能力查询闭包（见 4.5）
6. introspection       → 三态元数据（见 4.5）
7. 一串 .with_*() 开关 → keywords/Repr/PartialEq/LocalName/scope（按能力与配置条件出现）
8. 组装：ContentVtable::new(..上述零件..).with_keywords().with_repr()....erase()
9. from_vtable(&VTABLE) → const ELEM: Element
```

最终生成的代码骨架（简化）如下。注意三个 `static`：`STORE`（懒存储）、`VTABLE`（虚表本体），以及它们被包在 `Element::from_vtable({ ... })` 的块表达式里。

```rust
unsafe impl ::typst_library::foundations::NativeElement for TextElem {
    const ELEM: ::typst_library::foundations::Element =
        ::typst_library::foundations::Element::from_vtable({
            static STORE: ::typst_library::foundations::LazyElementStore
                = ::typst_library::foundations::LazyElementStore::new();
            static VTABLE: ::typst_library::foundations::ContentVtable =
                ::typst_library::foundations::ContentVtable::new::<TextElem>(
                    "text", "Text", None, "...docs...",
                    ::typst_utils::DefSite { path: file!(), key: "text" },
                    &[ /* 字段 vtable 切片 */ ],
                    /* field_id 闭包 */,
                    /* capable_func 闭包 */,
                    /* introspection */,
                    || &STORE,
                ).with_keywords(&[...]).with_repr()...   // 条件链
                 .erase();
            &VTABLE
        });
}
```

#### 4.1.3 源码精读

整个 `unsafe impl` 的 `quote!` 块在 [`elem.rs:439-465`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L439-L465)。几个要点：

- `def_site_key` 在 [行 389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L389) 取 `ident.to_string()`（元素级只用类型名；字段级会变成 `Elem::field`，见 4.3）。配合 `file!()` 拼成 [`DefSite`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L450)，供 IDE「跳转到定义」。
- `STORE` / `VTABLE` 两个 `static`（[行 442-444](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L442-L444)）放在 `from_vtable({ ... })` 的块表达式里，最后 `&VTABLE` 作为 `from_vtable` 的参数（[行 462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L462)）。
- `ContentVtable::new` 的实参顺序在运行时 [`vtable.rs:146-181`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L146-L181) 固定。注意它内部把 `construct` / `set` 槽位硬连到 `<E as Construct>::construct` / `<E as Set>::set`（[vtable.rs:167-168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L167-L168)）——**这意味着元素必须实现 `Construct` 与 `Set`**。宏在 `create` 里用 `element.cannot("Construct")` / `element.cannot("Set")` 判断：若用户没把这两者列为「能力」（即没手写），就自动生成（u4-l4）；否则用户自己提供。这正是「vtable 硬连接」与「能力开关」的交汇点。
- `.erase()`（[行 461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L461)）把带类型参数的 `ContentVtable<Packed<E>>` 转换成类型擦除的 `ContentVtable`，这样所有元素共用同一种 vtable 类型，存进 `Element(Static<ContentVtable>)`（见运行时 [`element.rs:20-21`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L20-L21) 与 [`from_vtable`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L29-L32)）。
- 一串 `.with_*()` 是条件链：`with_keywords`（[`elem.rs:432-433`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L432-L433) 在 keywords 非空时出现）、`with_repr` / `with_partial_eq` / `with_local_name`（[行 434-436](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L434-L436)，对应 `#[elem(..)]` 里声明的能力 `Repr` / `PartialEq` / `LocalName`）、`with_scope`（[行 437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L437)，`scope` 标志位，表示有 `#[scope]` 宏配套）。

> **为什么 `store` 是 `|| &STORE` 闭包，而不是直接写 `&STORE`？** 运行时 [`vtable.rs:134-140`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L134-L140) 的注释解释了：`LazyElementStore` 内部带 `OnceLock`（内部可变性），若在 `const` 里直接存引用会触发「常量中存在内部可变性」的错误；用函数指针绕开这个限制。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解 `Element` 句柄与 vtable 的「一一对应」关系，以及能力开关如何折射到 vtable 字段。
2. **步骤**：
   - 打开运行时 [`element.rs:23-108`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L23-L108)，浏览 `Element` 上的方法（`name` / `title` / `docs` / `construct` / `set` / `can` 等），注意它们几乎都是「从 vtable 取一个字段」。
   - 回到宏 [`elem.rs:456-460`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L456-L460)，数一下有几条 `.with_*()`。
3. **观察**：`Element` 的每个方法都对应 `ContentVtable` 的一个字段；宏的每条 `.with_*()` 都是在「默认值之上打补丁」。
4. **预期结果**：能说出「宏生成的 `const ELEM` 里，哪些槽位由 `ContentVtable::new` 默认填充、哪些由 `.with_*()` 覆盖」。

#### 4.1.5 小练习与答案

**练习 1**：若一个元素没有声明 `Repr` 能力，`ContentVtable` 的 `repr` 槽位会是什么值？
**答**：`ContentVtable::new` 默认把 `repr` 设为 `None`（[vtable.rs:178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L178)）；只有当 `element.can("Repr")` 为真时，宏才追加 `.with_repr()`（[elem.rs:434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L434)）把它改成 `Some(...)`。运行时若取到 `None`，就退回到「名字 + 字段」的通用 `Repr`。

**练习 2**：为什么 `const ELEM` 必须用 `Element::from_vtable(&VTABLE)` 而不是直接 `Element(VTABLE)`？
**答**：`Element` 内部是 `Static<ContentVtable>`（一个指针包装，[element.rs:20-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L20-L21)），`from_vtable`（[行 29-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L29-L32)）把这个 `&'static ContentVtable` 引用转成 `Static`，从而 `Element` 变成可 `Copy`/`Hash`/`Eq` 的轻量句柄，比较元素类型只需比指针。

---

### 4.2 字段定位：field_id 闘包与字段 vtable 切片的五路分支

#### 4.2.1 概念说明

注册完元素本身，还要告诉运行时「这个元素有哪些字段、每个字段怎么读写」。`ContentVtable` 用两样东西描述字段（见 [`vtable.rs:95-100`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L95-L100)）：

- `fields: &'static [FieldVtable]`——一个**字段子虚表切片**，按下标 `u8` 取；
- `field_id: fn(name: &str) -> Option<u8>`——一个把**字段名字符串翻译成下标**的闭包。

二者配合就能实现 `content.field("columns")`：先用 `field_id("columns")` 得到下标 `i`，再用 `fields[i]` 取到那张字段子虚表，调用上面的 `get` / `has` / `eq` 等函数指针。

为什么要单独搞一个 `field_id` 闭包，而不是在切片里线性查找名字？运行时 [`vtable.rs:97-100`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L97-L100) 注释说：这样 Rust 编译器能为这段字符串匹配生成**优化后的代码**（宏把 `match` 展开成静态 arms，便于编译期优化），而不必在运行时遍历切片。

#### 4.2.2 核心流程

`create_native_elem_impl` 里生成这两样东西的逻辑（[`elem.rs:397-427`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L397-L427)）：

**A. 字段 vtable 切片**（`fields`，[行 397-412](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L397-L412)）：遍历**非 internal** 字段，按属性五选一（实际是六个分支）挑一个 `XxxFieldData::<Self, i>::vtable()` 填进切片：

```
external   → ExternalFieldData::<E, i>::vtable()
variadic   → RequiredFieldData::<E, i>::vtable_variadic()   // 注意是 variadic 版
required   → RequiredFieldData::<E, i>::vtable()
synthesized→ SynthesizedFieldData::<E, i>::vtable()
ghost      → SettablePropertyData::<E, i>::vtable()
else       → SettableFieldData::<E, i>::vtable()
```

判定顺序很关键：`external` 先判（它「只在文档里出现」），随后 `variadic` 在 `required` 之前（因为 variadic 自动也是 required，必须先拦住），再 `required`，再 `synthesized`，再 `ghost`（ghost 不进 struct 但仍是 settable 性质），最后兜底普通 settable。

**B. field_id 闭包**（[行 414-427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L414-L427)）：遍历**非 internal 且非 external** 字段，为每个生成一条 `#name => Some(#i)` 臂。external 字段不出现在 `field_id` 里——因为它根本不在元素实例上（`has` 恒为 `false`，见运行时 [`field.rs:232`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L232)），但**会**出现在 `fields` 切片里（供文档/自动补全展示）。

> 注意「切片下标」与「`field.i`」的一致性：u4-l1 的 `parse` 已经按 `sort_by_key(internal)` 排序并赋了 `field.i`（非 internal 在前）。这里切片的遍历顺序与 `field.i` 对应，所以 `fields[i]` 正好是第 `i` 个字段的子虚表，`field_id` 返回的 `i` 能正确索引。

#### 4.2.3 源码精读

切片分支见上节链接的 [行 397-412](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L397-L412)。注意每个分支都带上了 const generic 索引 `#i`，例如：

```rust
quote! { #foundations::RequiredFieldData::<#ident, #i>::vtable() }   // required 分支
```

`field_id` 闭包本体（[行 422-427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L422-L427)）展开后形如：

```rust
|name| match name {
    "columns" => Some(0u8),
    "gutter" => Some(1u8),
    ...
    _ => None,
}
```

#### 4.2.4 代码实践（源码阅读 + 推演型）

1. **目标**：给定一组字段，能预测 vtable 切片的顺序与每个槽位调用哪个 `vtable()`，以及 `field_id` 的 arms。
2. **步骤**：假设有字段（均已按 internal 排序、索引从 0 起）：
   - `children: Content`（`#[required]`，`i=0`）
   - `gutter: Dir`（普通 settable，`i=1`）
   - `notes: Vec<Note>`（`#[external]`，`i=2`）
3. **需要观察的现象**：写出 `fields` 切片的三项与各自调用的函数；写出 `field_id` 闭包有几条 arm。
4. **预期结果**：
   - `fields = &[RequiredFieldData::<E, 0>::vtable(), SettableFieldData::<E, 1>::vtable(), ExternalFieldData::<E, 2>::vtable()]`
   - `field_id` 只有两条 arm：`"children" => Some(0)`、`"gutter" => Some(1)`；external 的 `notes` **不进** `field_id`。
5. （external 字段仍占索引 2 并出现在切片里——这是容易踩的坑，请重点验证这一条。）

#### 4.2.5 小练习与答案

**练习 1**：一个 `#[variadic]` 字段会走哪个切片分支？为什么不和普通 `required` 共用？
**答**：走 `RequiredFieldData::vtable_variadic()`（[elem.rs:401-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L401-L402)）。因为 variadic 字段的「取参」是 `args.all()`（吃掉所有剩余位置参数），其 `FieldVtable` 需要把 `variadic` 标志置 `true` 且 `input` 取容器内层类型的 `Reflect::input()`，这由单独的 `vtable_variadic()` 完成（见运行时 [`field.rs:104-129`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L104-L129)）。

**练习 2**：`internal` 字段会出现在 vtable 切片或 `field_id` 里吗？
**答**：都不会。切片与 `field_id` 都用 `filter(|field| !field.internal)`（[elem.rs:397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L397) 与 [行 417](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L417)）。internal 字段对 Typst 用户不可见，所以不进运行时元数据（但它的 `field.i` 索引仍被占用，排在非 internal 字段之后）。

---

### 4.3 逐字段 trait 实现：create_field_impl 与 FIELD 常量

#### 4.3.1 概念说明

4.2 讲的是「切片里每个槽位调用 `XxxFieldData::<E, i>::vtable()`」。这些 `vtable()` 方法从哪来？答案是：**运行时要求元素为每个字段实现一个对应的 trait**（`RequiredField<i>` / `SettableField<i>` / `SynthesizedField<i>` / `ExternalField<i>` / `SettableProperty<i>`），这些 trait 各自带一个 `const FIELD: XxxFieldData<Self, i>` 常量，`vtable()` 就是读着这个 `FIELD` 把函数指针拼装成 `FieldVtable`（见运行时 [`field.rs:78-102`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L78-L102)）。

`create_field_impl` 就是「为单个字段生成这组 trait impl + FIELD 常量」的函数。它在 `create` 里被对**所有字段**（含 internal/external，见 [`elem.rs:256-257`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L256-L257)）逐个调用。

这里要分清两个「字段常量」，它们都叫得很像，但角色完全不同：

| 常量 | 在哪生成（讲次） | 类型 | 角色 |
| --- | --- | --- | --- |
| `Self::columns` | `create_inherent_impl`（u4-l2） | `Field<Self, I>`（零大小句柄） | 用户代码里的「字段把手」，如 `styles.get(TextElem::size)` |
| `<E as RequiredField<I>>::FIELD` | `create_field_impl`（本讲） | `RequiredFieldData<Self, I>` 等 | 运行时 vtable 读取的**实质元数据**（getter 指针、默认值等） |

`Field<Self, I>` 句柄本身不含数据（它是个 `PhantomData`，见运行时 [`field.rs:18-30`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L18-L30)），它唯一的作用是**携带 const generic 索引 `I`**。运行时拿到 `Field<E, I>` 就知道要去找 `<E as ???Field<I>>::FIELD`——这就是 u4-l2 那个 `I` 与本讲 vtable 槽位的真正连接点。

#### 4.3.2 核心流程

`create_field_impl`（[`elem.rs:469-572`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L469-L572)）的分支结构：

```
def_site_key = "Elem::field"          // 比元素级多了 ::field
default     = || #default  或  Default::default   // 有 #[default(..)] 用前者

if external  → impl ExternalField<i>  { type Type; const FIELD: ExternalFieldData }
else if required → impl RequiredField<i> { type Type; const FIELD: RequiredFieldData(.., get: |elem| &elem.f) }
else if synthesized → impl SynthesizedField<i> { ... get: |elem| &elem.f }  // 注意返回 &Option<T>
else (settable):
   slot = || { static LOCK: OnceLock<T>; &LOCK }   // 默认值缓存槽
   with_fold = field.fold.then(|| quote!{ .with_fold() })
   refable   = (!field.fold).then(|| quote!{ impl RefableProperty<i> for E {} })
   if ghost  → impl SettableProperty<i> { const FIELD: SettablePropertyData }
   else      → impl SettableField<i> { const FIELD: SettableFieldData(.., get, get_mut, ..) }
   末尾追加 #refable
```

两条贯穿全分支的主线：

1. **`type Type = #ty`**：把字段的 Rust 类型暴露给运行时（`vtable()` 用它生成 `input()`、默认值等）。
2. **`const FIELD` 的构造参数**：都从 `Field` 的标志取——`name`（kebab 名）、`docs`、`def_site`、`default`、以及若干 getter 闭包（`|elem| &elem.#ident` 这类直接访问 struct 字段的函数指针）。

#### 4.3.3 源码精读

- required 分支（[行 495-507](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L495-L507)）：生成 `impl RequiredField<i>`，`FIELD` 用 `RequiredFieldData::new(name, docs, def_site, |elem| &elem.#ident)`。最后那个 getter 闭包就是「从 struct 取该字段引用」的函数指针。
- synthesized 分支（[行 508-520](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L508-L520)）：结构同 required，但 getter 类型是 `fn(&E) -> &Option<E::Type>`（因为 synthesized 在 struct 里存为 `Option<T>`，见 u4-l2 与运行时 [`field.rs:145`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L145)）。
- external 分支（[行 482-494](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L482-L494)）：没有 getter（字段不在 struct 上），只有 `default` 函数指针，用于文档展示默认值。
- settable 分支（[行 521-571](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L521-L571)）：普通 settable 额外提供 `get_mut`（`|elem| &mut elem.#ident`），ghost 没有（ghost 不在 struct 上，只能从样式链取）。详细条件生成见 4.4。

> 注意 `def_site_key` 在字段级是 `format!("{elem_ident}::{ident}")`（[行 472](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L472)）——形如 `GridElem::columns`，与 u4-l5 的「定位键命名规则」相呼应。

#### 4.3.4 代码实践

1. **目标**：亲手写出 required 字段与普通 settable 字段的 `create_field_impl` 产物，建立「字段标志 → trait 选择 → FIELD 参数」的直觉。
2. **步骤**：见本讲 **第 5 节综合实践**（它正是本模块的完整版）。作为本模块的预热，先回答：一个 `#[default(0)] gap: i64`（普通 settable，非 fold）字段，会生成哪两个 impl？
3. **观察 / 预期结果**：会生成 `impl SettableField<i> for E` 与 `impl RefableProperty<i> for E {}`（因为非 fold，`refable` 为真）。`FIELD` 的 `default` 参数是 `|| 0`（因为带 `#[default(0)]`）。

#### 4.3.5 小练习与答案

**练习 1**：required 字段与 synthesized 字段的 getter 闭包签名有什么不同？为什么？
**答**：required 的 getter 是 `fn(&E) -> &E::Type`（直接返回字段引用）；synthesized 的是 `fn(&E) -> &Option<E::Type>`（返回 `Option` 的引用）。因为 u4-l2 里 synthesized 字段在 struct 中存为 `Option<T>`（初始 `None`，由后续阶段合成填入），运行时据此判断「字段是否已存在」（`has` 看 `is_some()`，见 [`field.rs:176`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L176)）。

**练习 2**：external 字段为什么连 `type Type` 的约束都比别的字段弱（运行时 `ExternalField` 的 `Type` 没有 `Clone` 约束，见 [`field.rs:190-194`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L190-L194)）？
**答**：因为 external 字段「实际不存在于元素」，永远 `has: |_| false`、`get: |_| None`（[`field.rs:232-233`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L232-L233)），只用于文档与默认值展示，所以不需要 `Clone` 这类「真值存取」才需要的约束。

---

### 4.4 折叠与可引用：with_fold、RefableProperty 与 OnceLock slot

#### 4.4.1 概念说明

settable 字段（无论 ghost 还是普通）有三种「附加属性」需要条件生成，全集中在 settable 分支（[`elem.rs:521-571`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L521-L571)）：

1. **`OnceLock` slot（默认值缓存）**：settable 字段有默认值。运行时希望「贵的类型只算一次默认值，便宜的类型每次直接构造」（见 [`field.rs:336-350`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L336-L350)）。为此每个 settable 字段配一个 `static LOCK: OnceLock<T>`，`slot` 闭包返回它的 `&'static` 引用，运行时用 `get_or_init` 懒填充。

2. **`with_fold`（折叠）**：`#[fold]` 字段的值不是「单一来源」，而是把样式链上多层 set 规则的值**用 `Fold::fold` 函数合并**起来（比如字号在不同层级被多次 set，要折叠成一个最终值）。带 fold 的字段，`FIELD` 构造后会链上 `.with_fold()`（见运行时 [`SettableFieldData::with_fold` field.rs:279-287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L279-L287) 与 [`SettablePropertyData::with_fold` field.rs:395-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L395-L402)），它会把 `fold` 字段从 `None` 填成 `Some(E::Type::fold)`。

3. **`RefableProperty`（可引用）**：这是一个 marker trait（[`field.rs:435-437`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L435-L437)），含义是「这个 settable 属性可以被**按引用**访问，因为它不折叠」。折叠字段的最终值依赖整条样式链、需要现场计算合并，无法给出一个稳定的 `&T`；非折叠字段则可以直接给引用。

#### 4.4.2 核心流程

三者在宏里都是**用 `bool.then(|| ...)` 条件生成**的（[`elem.rs:522-534`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L522-L534)）：

```
slot      = || { static LOCK: OnceLock<#ty> = OnceLock::new(); &LOCK }   // 无条件，所有 settable 都有
with_fold = field.fold.then(|| quote!{ .with_fold() })                    // 仅 fold 字段链上
refable   = (!field.fold).then(|| quote!{ impl RefableProperty<#i> for E {} })  // 仅【非 fold】字段生成
```

随后：

- ghost 字段（[行 536-551](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L536-L551)）：生成 `impl SettableProperty<i>`（手动实现，不走 `SettableField`），`FIELD` 是 `SettablePropertyData`，链上 `#with_fold`，块末追加 `#refable`。
- 普通 settable 字段（[行 552-570](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L552-L570)）：生成 `impl SettableField<i>`，`FIELD` 是 `SettableFieldData`（内嵌一个 `SettablePropertyData`），同样链上 `#with_fold`、块末追加 `#refable`。普通 settable 还会通过运行时的 blanket impl（[`field.rs:353-361`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L353-L361)）自动获得 `SettableProperty<i>`，所以 `refable` 里写 `impl RefableProperty<i> for E {}` 能通过其超 trait 约束。

关键不变量：**`with_fold` 与 `refable` 互斥**——一个字段要么折叠（`.with_fold()`），要么可引用（`impl RefableProperty`），不会同时出现。因为 `refable = (!field.fold).then(...)`，二者由同一个 `field.fold` 反向控制。

#### 4.4.3 源码精读

重点看这三行（[`elem.rs:529-534`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L529-L534)）：

```rust
let with_fold = field.fold.then(|| quote! { .with_fold() });
let refable = (!field.fold).then(|| {
    quote! { impl #foundations::RefableProperty<#i> for #elem_ident {} }
});
```

以及 `slot`（[行 522-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L522-L527)）：每个 settable 字段都生成自己专属的 `static LOCK: OnceLock<#ty>`，所以默认值的懒缓存是**每字段独立**的。

#### 4.4.4 代码实践

见 **第 5 节综合实践** 第 3 问（解释为何 fold 字段不实现 `RefableProperty`）。本模块的迷你实践：

1. **目标**：验证「互斥不变量」。
2. **步骤**：在 [`elem.rs:529`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L529) 与 [`elem.rs:530`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L530) 处对照阅读两个 `.then(...)` 的条件。
3. **预期结果**：确认二者条件恰好相反（`field.fold` 与 `!field.fold`），故永远不会同时为真。
4. 「待本地验证」：若想眼见为实，可在一个真实 `#[elem]` 字段上分别加 / 去 `#[fold]`，用 `cargo expand` 观察生成的 impl 里 `RefableProperty` 的有无与 `.with_fold()` 的有无（命令：`cargo expand -p typst-library 2>/dev/null | grep -A3 RefableProperty`，具体能否运行依赖本地工具链，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `slot` 闭包里要写 `static LOCK`，而不是复用一个全局 `OnceLock`？
**答**：因为不同字段类型不同（`#ty` 是字段专属），且每个字段需要**独立**缓存自己的默认值。`static LOCK: OnceLock<#ty>` 在宏展开后位于「为该字段生成的 impl 内部」，每个字段一份，互不干扰。

**练习 2**：一个 `#[ghost]` 且 `#[fold]` 的字段会生成哪些 impl？会有 `RefableProperty` 吗？
**答**：生成 `impl SettableProperty<i>`（ghost 走手动 `SettableProperty` 分支），其 `FIELD` 链上 `.with_fold()`；**没有** `RefableProperty`（因为 `field.fold` 为真，`refable` 为 `None`）。

---

### 4.5 能力系统速览：FORBIDDEN 名单与悬垂指针取虚表

#### 4.5.1 概念说明

「能力（capability）」是 Typst 元素的另一个维度：元素可以声明自己支持某些 trait（在 `#[elem(Locatable, Repr, ...)]` 里列出），运行时据此做动态查询（如「这个内容能不能被定位」）。`ContentVtable` 有一个 `capability: fn(TypeId) -> Option<NonNull<()>>` 槽位（[vtable.rs:110-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L110-L113)）：给它一个 `TypeId`（通常是 `TypeId::of::<dyn SomeTrait>()`），它要么返回一个指向「该元素作为 `dyn SomeTrait` 的原生 Rust 虚表」的裸指针，要么返回 `None` 表示「不支持」。

`create_capable_func` 就是生成这个闭包的函数。还有一组「内省能力」`Locatable` / `Unqueriable` / `Tagged` 不走 `capability` 闭包，而是单独存成 `IntrospectionCapabilities` 三布尔结构（[vtable.rs:318-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L318-L326)），由 `create_introspection_capabilities` 生成。

#### 4.5.2 核心流程

`create_capable_func`（[`elem.rs:659-702`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L659-L702)）：

1. 定义 `FORBIDDEN` 名单（[行 662-675](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L662-L675)）：包括两类——「非对象安全」的 trait（`Debug` / `PartialEq` / `Hash` / `Construct` / `Set` / `Repr` / `LocalName`，它们已通过 vtable 的专用槽位或代码生成处理）和「内省能力」（`Locatable` / `Unqueriable` / `Tagged`，已通过 `IntrospectionCapabilities` 处理）。
2. 过滤掉用户声明但在 `FORBIDDEN` 里的能力（[行 678-681](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L678-L681)），剩下的才生成 `TypeId` 匹配臂。
3. 每个匹配臂（[行 683-693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L683-L693)）用**悬垂指针** `NonNull::<Packed<E>>::dangling()` 构造一个「不指向有效数据」的胖指针，仅为了**提取它的 vtable 指针**（`::typst_utils::fat::vtable(...)`），返回这个指针。

`create_introspection_capabilities`（[行 705-727](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L705-L727)）则做一次校验：`Unqueriable` 必须搭配 `Locatable`（否则 `bail!`，[行 711-715](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L711-L715)），然后把三者压成 `IntrospectionCapabilities { locatable, unqueriable, tagged }`。

#### 4.5.3 源码精读

最值得品读的是悬垂指针那一段（[`elem.rs:683-701`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L683-L701)）：

```rust
let dangling = ::std::ptr::NonNull::<#foundations::Packed<#ident>>::dangling().as_ptr();
// ...
if capability == ::std::any::TypeId::of::<dyn #capability>() {
    // Safety: The vtable function doesn't require initialized
    // data, so it's fine to use a dangling pointer.
    return Some(unsafe {
        ::typst_utils::fat::vtable(dangling as *const dyn #capability)
    });
}
```

这里利用了 Rust 胖指针（`dyn Trait`）的内存布局 = 「数据指针 + 虚表指针」。`fat::vtable` 只读取其中的虚表指针部分，**不解引用数据指针**，所以即便 `dangling` 指向的是未初始化的对齐内存也安全——这正是注释里那句 Safety 论证的依据。返回的虚表指针随后被运行时用来把 `Packed<E>` 当作 `dyn Capability` 使用。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：理解能力的「分流」——哪些进 `capability` 闭包，哪些进 `IntrospectionCapabilities`，哪些被 `FORBIDDEN` 拦下后由别处处理。
2. **步骤**：把 [`FORBIDDEN`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L662-L675) 名单里的 10 项分类：哪些对应 `ContentVtable` 的专用字段（如 `Repr`→`.with_repr()`、`LocalName`→`.with_local_name()`），哪些对应 `IntrospectionCapabilities`（`Locatable`/`Unqueriable`/`Tagged`），哪些是「非对象安全、由代码生成保证」（`Construct`/`Set`/`Debug`/`PartialEq`/`Hash`）。
3. **预期结果**：能解释「为什么这些 trait 不能放进 `capability` 闭包」——它们要么不对象安全（无法 `dyn`），要么已经有更高效的专用通道。

#### 4.5.5 小练习与答案

**练习 1**：用户写 `#[elem(Unqueriable)]` 但没写 `Locatable`，会发生什么？
**答**：`create_introspection_capabilities` 会 `bail!(unqueriable, "only Locatable element can be marked Unqueriable")`（[elem.rs:711-715](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L711-L715)），编译期直接报错。语义上「不可被用户查询」只在「可被定位」的前提下才有意义。

**练习 2**：为什么用 `NonNull::dangling()` 而不是 `&Packed<E>` 的真实引用来取虚表？
**答**：因为这里没有也不需要一个真实的 `Packed<E>` 实例——只需要胖指针里的**虚表指针**分量。`dangling()` 给出一个对齐合法但未初始化的非空指针，避免构造一个完整值（有些类型构造代价大甚至无法在常量/此上下文中构造），而 `fat::vtable` 承诺不解引用数据指针，所以安全（见注释 [`elem.rs:686-687`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L686-L687)）。

---

## 5. 综合实践

把本讲的核心（4.3 + 4.4）串起来。给定这样一个元素骨架（省略无关属性）：

```rust
#[elem]
pub struct CountElem {
    #[required] value: i64,                 // 索引 0，required
    #[default(1)] step: i64,                 // 索引 1，普通 settable，非 fold
    #[fold] weights: SmallVec<[i64; 4]>,     // 索引 2，普通 settable，fold
}
```

**任务**：

1. 为 `value`（required）和 `step`（普通 settable、非 fold）两处，写出 `create_field_impl` 生成的 `impl` 与 `FIELD` 常量（展开 `foundations` 为 `::typst_library::foundations`）。
2. 为 `weights`（fold）写出生成物的**差异**——它多了什么、少了什么。
3. 解释**为什么 `weights` 不实现 `RefableProperty`**，依据是宏里的哪一行代码、运行时的哪条契约。

**参考答案**（示例代码，`#i` 已替换为真实索引）：

```rust
const _: () = {
    // (1a) value —— required 分支（elem.rs:495-507）
    impl ::typst_library::foundations::RequiredField<0u8> for CountElem {
        type Type = i64;
        const FIELD: ::typst_library::foundations::RequiredFieldData<Self, 0u8> =
            ::typst_library::foundations::RequiredFieldData::<Self, 0u8>::new(
                "value", "...docs...",
                ::typst_utils::DefSite { path: file!(), key: "CountElem::value" },
                |elem| &elem.value,
            );
    }

    // (1b) step —— 普通 settable、非 fold（elem.rs:552-570 + refable）
    impl ::typst_library::foundations::SettableField<1u8> for CountElem {
        type Type = i64;
        const FIELD: ::typst_library::foundations::SettableFieldData<Self, 1u8> =
            ::typst_library::foundations::SettableFieldData::<Self, 1u8>::new(
                "step", "...docs...",
                ::typst_utils::DefSite { path: file!(), key: "CountElem::step" },
                false,                                  // positional
                |elem| &elem.step,                       // get
                |elem| &mut elem.step,                   // get_mut
                || 1,                                    // default（来自 #[default(1)]）
                || {                                     // slot
                    static LOCK: ::std::sync::OnceLock<i64> = ::std::sync::OnceLock::new();
                    &LOCK
                },
            );                                           // 没有 .with_fold()
    }
    impl ::typst_library::foundations::RefableProperty<1u8> for CountElem {}  // 非 fold → 有 refable

    // (2) weights —— 普通 settable、fold（elem.rs:552-570 + with_fold，无 refable）
    impl ::typst_library::foundations::SettableField<2u8> for CountElem {
        type Type = SmallVec<[i64; 4]>;
        const FIELD: ::typst_library::foundations::SettableFieldData<Self, 2u8> =
            ::typst_library::foundations::SettableFieldData::<Self, 2u8>::new(
                "weights", "...docs...",
                ::typst_utils::DefSite { path: file!(), key: "CountElem::weights" },
                false,
                |elem| &elem.weights,
                |elem| &mut elem.weights,
                std::default::Default::default,          // 无 #[default] → Default
                || {
                    static LOCK: ::std::sync::OnceLock<SmallVec<[i64; 4]>> = OnceLock::new();
                    &LOCK
                },
            ).with_fold();                               // fold → 链上 .with_fold()
    }
    // 注意：这里【没有】 impl RefableProperty<2u8> for CountElem —— fold 字段不生成 refable
};
```

**(2) 差异**：与 `step` 相比，`weights` 多了 `.with_fold()`，少了 `impl RefableProperty`；并且 `default` 因没有 `#[default(..)]` 而退化为 `Default::default`（见 [`elem.rs:477-480`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L477-L480)）。

**(3) 为什么 fold 字段不实现 `RefableProperty`**：

- **宏侧依据**：`create_field_impl` 里 `let refable = (!field.fold).then(|| quote!{ impl RefableProperty<#i> for #elem {} });`（[`elem.rs:530-534`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L530-L534)）。`weights` 的 `field.fold` 为真，`!field.fold` 为假，`.then(...)` 返回 `None`，所以展开产物里没有这条 impl。
- **运行时契约依据**：`RefableProperty<I>: SettableProperty<I>` 是一个 marker trait，其文档定义为「A settable property that can be accessed by reference (**because it is not folded**)」（[`field.rs:435-437`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L435-L437)）。折叠字段的最终值要把整条样式链上的多个值用 `Fold::fold` 合并后才能得到（`with_fold` 把 `fold` 字段设为 `Some`，见 [`field.rs:279-287`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L279-L287)），它不存在一个「现成的、稳定的」存储位置可以按引用返回；非折叠字段则可以直接借用 struct 里或样式链里那个已存在的值。因此「可引用」与「可折叠」在类型系统里被设计成互斥，宏用 `fold` 与 `!fold` 这对相反条件精确地表达了这一互斥关系。

**自查清单**：
- required 字段：getter 是 `|elem| &elem.value`，没有 `get_mut`、没有 `slot`、没有 `with_fold`、没有 `refable`。
- 普通 settable 非 fold：有 `get` + `get_mut` + `slot`，无 `.with_fold()`，**有** `RefableProperty`。
- 普通 settable fold：有 `get` + `get_mut` + `slot` + `.with_fold()`，**无** `RefableProperty`。

---

## 6. 本讲小结

- `create_native_elem_impl` 生成一个 `unsafe impl NativeElement`，核心是 `const ELEM: Element = Element::from_vtable(&VTABLE)`；`VTABLE` 是一个 `static ContentVtable`，由 `ContentVtable::new(..).with_*(..).erase()` 组装。运行时凭 vtable 指针就能完成元素的构造、set、clone、hash、能力查询等全部操作。
- 元素的字段在 vtable 里用「`fields` 切片 + `field_id` 闭包」二元组描述：`field_id` 把字段名翻译成索引 `u8`（编译期 `match`，利于优化），`fields[i]` 给出该字段的子虚表。`internal` 字段两者都不进，`external` 只进切片不进 `field_id`。
- 切片每个槽位调用 `XxxFieldData::<Self, i>::vtable()`，其取值由 `create_native_elem_impl` 里的六路分支按 external/variadic/required/synthesized/ghost/普通 settable 选定。
- `create_field_impl` 为**每个字段**生成对应的 trait impl（`RequiredField` / `SynthesizedField` / `ExternalField` / `SettableField` / `SettableProperty`）与 `FIELD` 常量，提供 getter / 默认值等实质元数据——这是 `vtable()` 读取的数据源。它与 u4-l2 的 `Field::new()` 句柄通过 const generic 索引 `I` 连接。
- settable 字段有三个条件生成项：每字段独立的 `OnceLock` slot（缓存默认值）、`with_fold`（仅 `#[fold]`）、`RefableProperty`（仅非 fold）。后两者由 `field.fold` 与 `!field.fold` 反向控制，因而**互斥**。
- 能力系统由两部分组成：`create_capable_func` 用 `FORBIDDEN` 名单过滤后，对每个剩余能力生成一条 `TypeId` 匹配臂，借 `NonNull::dangling()` 的悬垂胖指针安全提取原生 Rust 虚表指针；`create_introspection_capabilities` 把 `Locatable` / `Unqueriable` / `Tagged` 压成三布尔，并校验「`Unqueriable` 必须搭配 `Locatable`」。

---

## 7. 下一步学习建议

本讲把元素「注册」进了运行时：它有了身份证（`NativeElement` / `ELEM`）和每个字段的存取能力（各字段 trait + `FIELD`）。但 vtable 的 `construct` / `set` 槽位还硬连着 `<E as Construct>::construct` / `<E as Set>::set`，这两个 impl 的**函数体**——即「如何从用户传的 `Args` 解析出每个字段的值、如何把 settable 字段变成 `Styles`」——还没讲。

- **u4-l4（#[elem] 四：Construct/Set 与能力系统）** 将深入 `create_construct_impl` / `create_set_impl`：`construct_fields` / `set_fields` 这两条过滤器如何决定哪些字段参与构造、哪些参与 set；`create_field_parser` 如何处理 `#[parse({ ... })]` 自定义解析块与四种默认取参分支（`args.all` / `expect` / `find` / `named`）；以及能力系统的更多细节。
- **u4-l5（架构总览）** 会把 `#[func]` 与 `#[elem]` 放到同一张「解析中间结构 → quote! 生成」的架构图下，并对比二者 `def_site_key` 的命名差异（本讲已见端倪：元素级是 `"CountElem"`，字段级是 `"CountElem::value"`）。

建议在进入 u4-l4 前，先回到本讲第 5 节综合实践自测：能否不查答案地为一个 required 字段、一个普通 settable 字段、一个 fold 字段分别写出 `create_field_impl` 的产物，并说清 `RefableProperty` 的有无理由。这是理解「字段四态（required/synthesized/settable/ghost）× 折叠/可引用」矩阵的关键练习。
