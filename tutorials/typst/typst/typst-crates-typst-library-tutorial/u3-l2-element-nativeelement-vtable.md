# Element、NativeElement 与能力 vtable

## 1. 本讲目标

上一讲（u3-l1）我们建立了「`Content` 是稳定外壳、`RawContent` 是底层 `Arc<dyn Element>` 表示」的认识，并把 `Element` 与 `NativeElement`、`Packed<T>`、样式折叠都推迟到了后续讲义。本讲就负责填补其中两块拼图：

1. 理解 `Element` 为什么是一个**类型擦除的元素句柄**——它不带具体数据，只携带一张指向 `static` 变量的虚函数表（vtable）指针。
2. 理解 `ContentVtable` 这张**自定义虚函数表**的结构，以及 Typst 为什么不直接用 Rust 自带的 trait object。
3. 理解 `NativeElement` 在 Rust 类型化一侧扮演的角色，以及 `Construct`/`Set`/`Synthesize`/`ShowSet`/`PlainText` 等能力 trait。
4. 掌握 `can::<C>()` / `with::<C>()` 这套**能力（capability）查询机制**，以及 `locatable`/`unqueriable`/`tagged` 三个内省标志如何决定元素行为。

学完本讲，你应当能读懂 `#[elem(Locatable, Refable, ...)]` 这一行属性到底生成了什么，并能解释「为什么一份类型擦除的 `Content` 还能在运行时变回 `&dyn Refable`」。

## 2. 前置知识

本讲默认你已经读过 u3-l1，了解 `Content`/`RawContent` 的「外壳—内核」关系与引用计数设计。在此基础上，需要几个 Rust 概念：

- **trait object（特征对象）**：`dyn Trait` 是把「具体类型」擦除、只保留「能做什么」的胖指针。它在 Rust 里通常由编译器自动生成一张 vtable（虚函数表）。
- **胖指针（fat pointer）**：由「数据指针 + vtable 指针」两部分组成。`&dyn Trait` 就是胖指针。
- **`repr(C)` 与 `transmute`**：`#[repr(C)]` 固定结构体字段在内存里的排列顺序，从而允许在「字段布局完全相同、只是泛型参数不同」的结构体之间用 `transmute` 安全转换。
- **`TypeId`**：Rust 为每个类型在运行时分配的唯一标识，常用于「我手里这个被擦除的对象，是不是某个类型 / 能否当成某个 trait」的判断。

Typst 没有使用 Rust 自带的 `dyn Trait` 来存放文档内容，而是手写了一套 vtable。本讲会解释这样做换来了什么。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `crates/typst-library/src/` 下，永久链接 base 为 `146a58329`）：

| 文件 | 作用 |
| --- | --- |
| `foundations/content/element.rs` | 定义 `Element` 句柄、`NativeElement`/`Construct`/`Set`/`Synthesize`/`ShowSet`/`PlainText` 等 trait |
| `foundations/content/vtable.rs` | 定义自定义虚函数表 `ContentVtable`、字段子表 `FieldVtable`、安全访问层 `Handle` 与 `IntrospectionCapabilities` |
| `foundations/content/raw.rs` | `RawContent` 上的 `is::<E>()` / `with::<C>()` / `with_mut::<C>()` 等能力查询实现 |
| `foundations/content/mod.rs` | `Content` 把上述能力以公开 API（`can`/`with`/`to_packed` 等）重新暴露 |
| `crates/typst-macros/src/elem.rs` | `#[elem(...)]` 过程宏，生成 `NativeElement` 实现与整张 vtable，是连接「源码里的一行属性」与「运行期函数指针表」的桥梁 |
| `model/heading.rs`、`text/mod.rs` | `HeadingElem`、`TextElem` 两个真实元素，演示能力声明 |

---

## 4. 核心概念与源码讲解

### 4.1 Element：类型擦除的元素句柄

#### 4.1.1 概念说明

在 Typst 里，一个「元素」（element）是构成文档的最小单位：`heading`、`text`、`par`、`image`、`math.equation`……每一个都对应一个 Rust 结构体（如 `HeadingElem`、`TextElem`）。但在编译流水线里，这些具体类型必须被打包进同一个容器统一流转——这就是上一讲讲过的 `Content` / `RawContent`。

`Element` 则是这些元素的「身份证」：它**不携带任何元素实例的数据**，只记录「我是哪一种元素、我这种元素能干什么」。你可以把它理解成一个「元素类型」的句柄（handle）。同一个元素类型在整个进程里只对应**唯一一张** vtable（存在 `static` 变量里），因此 `Element` 是 `Copy` 的、可以廉价地到处复制比较。

> 关键直觉：`Element` ≈ 「指向某一种元素类型的说明书」。`Content` ≈ 「一份说明书 + 一份实例数据」。

#### 4.1.2 核心流程

`Element` 的典型生命周期：

1. `#[elem(...)]` 宏为每个元素生成一个 `static VTABLE`，再用 `Element::from_vtable(&VTABLE)` 把它的地址包进 `Element`，存为 `T::ELEM` 常量。
2. 任何地方想知道「内容是哪种元素」，拿到 `Element` 句柄即可：比较两个 `Element` 等价于比较两张 vtable 的地址（O(1)，无需动态分发）。
3. 需要做事时，通过 `Element` 调用 vtable 上的函数指针（`construct`、`set`、`capability` 等），由 vtable 转发到具体元素类型的方法。

#### 4.1.3 源码精读

`Element` 本身极其简洁——一个包裹 `Static<ContentVtable>` 的新类型：

[src/foundations/content/element.rs:19-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L19-L21) — `Element` 就是一个指向静态 vtable 的 `Copy` 句柄，派生了 `Eq`/`Hash`，因此比较和哈希都退化为「比地址」。

它的构造只发生在宏生成的常量里：

[src/foundations/content/element.rs:23-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L23-L32) — `of::<T>()` 直接返回 `T::ELEM`（每个元素都通过 `NativeElement` 关联一个 `const ELEM`）；`from_vtable` 则是把 vtable 地址包起来。这是获取 `Element` 的唯一两条路径。

`Element` 上最有代表性的「动作」方法是 `construct` 与 `set`——它们正是用户写 `#heading(...)` / `#set heading(...)` 时最终触发的调用点：

[src/foundations/content/element.rs:65-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L65-L79) — `construct` 把 `(engine, args)` 交给 vtable 上的 `construct` 函数指针；`set` 则先调用 vtable 的 `set` 收集样式，再调用 `args.finish()` 校验没有多余参数。注意它们只持有 `self`（句柄）和参数，**不持有任何元素实例**。

其余方法大多是「读取 vtable 里某段元数据」的薄封装：`name`/`title`/`docs`/`keywords` 读字符串，`scope`/`params` 通过 vtable 的 `store` 字段做惰性初始化，`field_id`/`field_name` 处理字段名↔ID 互转，并特判保留 ID `255`（`label`，上一讲讲过）：

[src/foundations/content/element.rs:149-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L149-L163) — `label` 被硬编码为字段 ID 255，绕过 vtable 的 `field_id` 函数指针，因为它对所有元素都通用。

`Debug`/`Repr`/`Ord` 的实现都只依赖 `name()`，再次印证 `Element` 只代表「类型」而非实例：

[src/foundations/content/element.rs:186-208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L186-L208) — 排序按名字字典序，`Repr` 直接输出元素名（如 `heading`）。

#### 4.1.4 代码实践

**实践目标**：确认「`Element` 等价性 == vtable 地址等价性」，并理解它如何转发 `construct`/`set`。

**操作步骤（源码阅读型）**：

1. 打开 `foundations/content/element.rs`，找到 `impl Element` 块，确认除 `vtable()` 外没有任何「字段数据」。
2. 在 `crates/typst-macros/src/elem.rs` 中找到宏为元素生成的 `const ELEM`，追踪它如何用 `from_vtable` 包裹一张 `static VTABLE`。
3. 全局搜索 `Element::of::<` 与 `.elem().construct`，观察「拿到句柄后调用 construct」的真实调用点（典型在 `typst-eval` 里，本仓库内可搜 `Args` 消费处）。

**需要观察的现象**：`Element` 上没有任何 `&self` 的实例数据；`construct`/`set` 的第一个参数是 `self`（句柄），而非元素实例。

**预期结果**：你会清楚看到「`Element` = 一张静态说明书的引用」，它把工作转发给函数指针，自己不存数据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Element` 可以派生 `Copy`，而 `Content` 不行？

> 参考答案：`Element` 只持有一个 `Static<ContentVtable>`（本质上是一个 `&'static ContentVtable` 的等价物），是廉价的、可随意复制的句柄；`Content`/`RawContent` 背后是引用计数的堆数据（实例字段、span、meta），克隆要走 Arc 引用计数，因此不适合 `Copy`。

**练习 2**：`Element::where_(fields)` 和 `Element::select()` 分别构造什么？

> 参考答案：`select()` 构造 `Selector::Elem(self, None)`（选中这种元素的全部实例）；`where_(fields)` 构造 `Selector::Elem(self, Some(fields))`（再叠加一组字段过滤，对应 `heading.where(level: 1)`）。

---

### 4.2 ContentVtable：自定义元素虚函数表

#### 4.2.1 概念说明

如果 `Element` 是「说明书」，那 `ContentVtable` 就是说明书的具体内容——一张装满「这种元素能干什么」的函数指针与元数据的表。Typst 没有用 Rust 自带的 `dyn Trait`，而是手写了这张 vtable。文件顶部的模块注释直接给出了两条理由：

1. 自定义 vtable 可以挂一张**字段子表（slice of sub-vtables）**，对每个字段做细粒度操作（取值、设值、比较）。
2. 自定义 vtable 不仅能放方法，还能放**纯数据**（名字、文档、标志位），访问这些数据无需动态分发。

此外，因为 vtable 指针来自 `static` 变量，「两个内容是不是同一种元素」可以用「两个 vtable 指针是否相等」来判断——这就是上一讲提到的 `RawContent::is`，无需任何动态分发。

#### 4.2.2 核心流程

一张 `ContentVtable` 大致包含四类内容：

```text
ContentVtable
├─ 元数据（纯数据）   : name, title, since, docs, def_site, keywords
├─ 字段相关           : fields: &[FieldVtable], field_id(name)->Option<u8>
├─ 元素级行为（函数指针）: construct, set, scope, local_name
│                      capability(TypeId)->Option<vtable ptr>
│                      drop/clone/hash/debug/eq/repr
├─ 内省标志（纯数据）  : introspection: IntrospectionCapabilities { locatable, unqueriable, tagged }
└─ 惰性存储           : store: fn() -> &'static LazyElementStore
```

vtable 是泛型 `ContentVtable<T>`，其中 `T` 在「具体元素」侧是 `Packed<E>`，在「类型擦除」侧是 `RawContent`。借助 `#[repr(C)]` 与 `Packed<E>` 的 `#[repr(transparent)]`，宏在最后用 `erase()` 把 `ContentVtable<Packed<E>>` 通过 `transmute` 变成统一的 `ContentVtable<RawContent>`（即 `ContentVtable`）。所有接收 `T` 的函数指针都被标记为 `unsafe`，调用方负责保证「vtable 与数据来自同一个 `E`」。

这套「不安全集中在 `Handle` 一处」的设计，由 `Handle` 提供安全访问层：它把「数据 + 匹配的 vtable」绑在一起，对外只暴露安全方法。

#### 4.2.3 源码精读

模块开头的注释把设计动机讲得很清楚，强烈建议先读：

[src/foundations/content/vtable.rs:1-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L1-L34) — 解释了为何自造 vtable、`RawContent::is` 如何免动态分发、以及所有方法接收 `Packed<E>` 的工作方式。

`Handle` 是把 unsafe 收敛到单点的关键：

[src/foundations/content/vtable.rs:53-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L53-L73) — `Handle<T, V>` 持有「数据 + 一张承诺匹配的 vtable」，`new` 是 `unsafe`（调用方必须保证匹配），之后通过 `Deref` 暴露 vtable、并提供 `debug`/`hash`/`clone`/`eq` 等安全方法。这样 unsafe 的「契约」只写在一处。

`ContentVtable` 本体（注意 `#[repr(C)]` 保证字段顺序）：

[src/foundations/content/vtable.rs:78-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L78-L141) — 逐字段看：开头是纯数据元数据；`fields` 是字段子表的切片；`construct`/`set` 是元素级行为；`capability(TypeId) -> Option<NonNull<()>>` 是能力查询的核心入口（返回某个 trait 的 vtable 指针，详见 4.4）；`introspection` 是三个布尔标志；`drop/clone/hash/debug/eq/repr` 是对元素实例的标准 trait 转发；`store` 指向一个用于惰性缓存的 `LazyElementStore`。注意 `eq`/`repr` 是 `Option<…>`——为 `None` 时回退到「字段逐个比较 / 通用 名字+字段 表示」。

vtable 的构造由 `ContentVtable::new::<E>` 完成，把各 trait 的实现函数（如 `<E as Construct>::construct`）填进去：

[src/foundations/content/vtable.rs:143-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L143-L181) — `new::<E>` 是一个 `const fn`，把元素类型 `E` 的 `Construct::construct`/`Set::set`、`RawContent::drop_impl`/`clone_impl`、`typst_utils::hash128` 等填入对应槽位；`eq`/`repr`/`local_name`/`scope` 先留空，再由 builder 方法 `with_partial_eq`/`with_repr`/`with_local_name`/`with_scope` 按需补上。

类型擦除发生在 `erase()`：

[src/foundations/content/vtable.rs:232-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L232-L249) — 借助 `repr(C)` 与「所有函数指针布局相同」「`Packed<E>` 和 `RawContent` 同布局（`repr(transparent)`）」三个前提，用 `transmute` 把 `ContentVtable<Packed<E>>` 安全地变成统一的 `ContentVtable<RawContent>`。这就是「自定义动态链接」的落点。

内省标志是一个独立的小结构（纯数据，无动态分发）：

[src/foundations/content/vtable.rs:318-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L318-L326) — `locatable`/`unqueriable`/`tagged` 三个布尔，决定元素在内省、查询、PDF 标签中的可见性（详见 4.4）。

字段子表 `FieldVtable` 是自定义 vtable 的「第二条理由」，对每个字段提供 `has`/`get`/`get_with_styles`/`materialize`/`eq` 等操作，我们留到 u3-l3「elem 宏、字段系统与 Packed」深入，这里只需知道它的存在：

[src/foundations/content/vtable.rs:328-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L328-L376) — 每个字段都带元数据（是否 positional/required/settable/synthesized 等）和一组操作函数指针，使得「按字段 ID 取值」也能在擦除类型后完成。

#### 4.2.4 代码实践

**实践目标**：理解「vtable 把行为与数据都装进一张表」的结构。

**操作步骤（源码阅读型）**：

1. 打开 `foundations/content/vtable.rs`，对照本节给出的「四类内容」树状图，给 `ContentVtable` 的每个字段标注它属于哪一类（元数据 / 字段相关 / 元素级行为 / 内省标志）。
2. 阅读 `Handle` 的几个 `impl` 块（`ContentHandle<&RawContent>` 提供 `debug`/`hash`/`clone`/`repr`，`ContentHandle<&mut RawContent>` 提供 `drop`，`ContentHandle<(&RawContent,&RawContent)>` 提供 `eq`），体会「同一个 vtable 配不同形态的数据，对外暴露不同安全方法」的设计。
3. 找到 `erase()`，对照其注释列出 transmute 成立的三个前提。

**需要观察的现象**：vtable 上的函数指针都被标记 `unsafe`，而 `Handle` 暴露的方法都是安全的——unsafe 边界就在 `Handle::new` 这一关。

**预期结果**：你能用自己的话说出「为什么 Typst 自造 vtable 而不用 `dyn Trait`」：拿到字段子表、把纯数据无分发访问、用指针相等做 `is` 判断。

#### 4.2.5 小练习与答案

**练习 1**：`ContentVtable` 的 `eq` 字段为什么是 `Option<unsafe fn>`？

> 参考答案：并非所有元素都手写了 `PartialEq`。若 `E: PartialEq`，宏会用 `with_partial_eq` 填入 `Some(...)`；否则为 `None`，此时相等性回退到「经 `FieldVtable::eq` 逐字段比较」（见 `ContentHandle<(...)>::eq` 返回 `Option<bool>`，`None` 时由调用方走字段比较）。

**练习 2**：`erase()` 用 `transmute` 把 `ContentVtable<Packed<E>>` 变成 `ContentVtable<RawContent>`，为什么这是安全的？

> 参考答案：因为 (1) 结构体是 `#[repr(C)]`，字段顺序固定；(2) vtable 中除函数指针外不含任何 `E` 特定的数据，而所有函数指针布局相同；(3) `Packed<E>` 与 `RawContent` 都是 `repr(transparent)`，内存布局一致。所以两套泛型实例的二进制表示完全相同，transmute 不改变任何字节。

---

### 4.3 NativeElement：Rust 侧的元素类型

#### 4.3.1 概念说明

如果说 `Element` + `ContentVtable` 是「擦除类型后的运行期表示」，那么 `NativeElement` 就是「Rust 类型化一侧的源头」。每一个用 `#[elem(...)]` 标注的结构体（如 `HeadingElem`）都会被宏实现 `NativeElement`，从而获得：

- 一个关联常量 `const ELEM: Element`（指向自己的 vtable）；
- 一个 `pack(self) -> Content` 方法（把自己装进类型擦除的 `Content`）。

`NativeElement` 还要求实现一组「能力 supertrait」：`Debug + Clone + Hash + Construct + Set + Send + Sync + 'static`。其中 `Construct` 和 `Set` 是每个元素必须实现的「构造」与「set 规则」入口。除此之外，Typst 还定义了一批**可选的能力 trait**（`Synthesize`、`ShowSet`、`PlainText`，以及散落在各模块的 `Refable`、`Count`、`Outlinable`、`Figurable`、`LocalName` 等）——元素按需 `impl` 它们，宏会把这些实现登记进 vtable，供运行期按能力查询调用（见 4.4）。

#### 4.3.2 核心流程

一个元素从「Rust 类型」到「运行期可调度对象」的链路：

```text
#[elem(Locatable, Refable, ...)] struct HeadingElem { ... }
        │
        │  typst-macros::elem 过程宏展开
        ▼
impl NativeElement for HeadingElem {
    const ELEM: Element = Element::from_vtable({
        static VTABLE = ContentVtable::new::<HeadingElem>(...).erase();
        &VTABLE
    });
}
impl Construct for HeadingElem { fn construct(...) }   // ← 填入 vtable.construct
impl Set for HeadingElem { fn set(...) }               // ← 填入 vtable.set
impl Refable for Packed<HeadingElem> { ... }           // ← 填入 capability 表
        │
        │  HeadingElem { ... }.pack()
        ▼
Content（类型擦除），可在运行期经 can/with 变回 &dyn Refable
```

注意一个重要细节：能力 trait（如 `Refable`）几乎总是为 **`Packed<HeadingElem>`** 实现，而不是为 `HeadingElem` 本身——因为运行期拿到的是「擦除后的 packed 表示」，trait 的方法要在这个 packed 句柄上工作。`Packed<T>` 与 `Content`/`RawContent` `repr(transparent)` 等价（详见 u3-l3）。

#### 4.3.3 源码精读

`NativeElement` 是一个 `unsafe trait`，约束了关联常量 `ELEM` 必须正确：

[src/foundations/content/element.rs:230-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L230-L244) — supertrait 列表里有 `Construct + Set`，所以「能构造、能 set 规则」是每个元素的强制要求；`pack` 默认实现就是 `Content::new(self)`。

`Construct` 与 `Set` 的签名（注意 `construct` 收到的是「执行完 set 规则后剩余的参数」）：

[src/foundations/content/element.rs:246-263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L246-L263) — `Construct::construct` 由 `#[elem]` 宏根据「字段是否标注了构造逻辑」生成默认实现（逐字段从 `Args` 取值）；`Set::set` 默认把所有 `settable` 字段从命名参数收集成 `Styles`。`Element::construct`/`Element::set` 最终调到的就是这两个。

可选能力 trait `Synthesize`、`ShowSet`、`PlainText` 定义在同一文件：

[src/foundations/content/element.rs:265-287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L265-L287) — `Synthesize` 在任意 show 规则之前「准备字段」（典型如标题在展示前回填编号）；`ShowSet` 定义元素自带的「show-set」规则（比用户 show-set 更强，能访问字段）；`PlainText` 把元素转成纯文本（用于无障碍 / 复制）。

其余能力 trait 散布在功能模块，且都是为 `Packed<E>` 实现：

- `Count`（计数能力）— [src/introspection/counter.rs:585](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L585)
- `Refable`（可被引用）— [src/model/reference.rs:427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L427)
- `Outlinable: Refable`（可进目录）— [src/model/outline.rs:452](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L452)
- `Figurable`（可放进图表）— [src/model/figure.rs:686](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L686)
- `LocalName`（本地化名称）— [src/text/lang.rs:616](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L616)

真实元素 `HeadingElem` 把这些能力写进 `#[elem(...)]` 属性：

[src/model/heading.rs:76-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L76-L86) — 这一行声明了 `Locatable`、`Tagged`、`Synthesize`、`Count`、`ShowSet`、`LocalName`、`Refable`、`Outlinable`。宏会把它们分别登记成「内省标志」「vtable 专用槽」「能力表条目」。

而 `TextElem` 声明的能力少得多：

[src/text/mod.rs:90-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L90-L91) — `#[elem(since = "forever", Debug, Construct, PlainText, Repr)]`。注意它显式列出了 `Construct`（这与文本元素「构造时改样式而非新建元素」有关，详见 u7-l1），其唯一的「运行期可查询能力」是 `PlainText`。

#### 4.3.4 代码实践

**实践目标**：把「`#[elem(...)]` 的一行属性」与「最终生成的代码」对上号。

**操作步骤（源码阅读型）**：

1. 打开 `crates/typst-macros/src/elem.rs`，定位宏生成 `unsafe impl NativeElement for #ident` 的那段（约 L440 起）。
2. 对照它如何调用 `ContentVtable::new::<#ident>(...)` 并链式 `.erase()`，确认上一节讲的 `new`/`erase` 在这里被实际使用：

[crates/typst-macros/src/elem.rs:440-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L440-L464) — 宏为元素生成一个 `static VTABLE` 与 `static STORE`，用 `ContentVtable::new::<E>(...)` 逐项填入字段、能力函数、内省标志，再经 builder 方法（`with_keywords`/`with_repr`/`with_partial_eq`/`with_local_name`/`with_scope`）补全，最后 `.erase()` 变成统一的 `ContentVtable`，包成 `const ELEM`。

3. 在 `model/heading.rs` 中找到 `impl Refable for Packed<HeadingElem>`（约 L320）与 `impl Count for Packed<HeadingElem>`（约 L311），确认这些方法确实是「为 packed 句柄实现」。

**需要观察的现象**：宏把「属性里列出的能力」分流——有的变成布尔标志，有的变成 vtable 专用槽，有的进入 `capability` 查询表。

**预期结果**：你能解释 `HeadingElem` 那行 `#[elem(...)]` 里的 8 个标识符分别去了哪里（详见下一节的对照表）。

#### 4.3.5 小练习与答案

**练习 1**：为什么能力 trait（如 `Refable`）都是为 `Packed<HeadingElem>` 实现的，而不是为 `HeadingElem`？

> 参考答案：运行期对内容做能力查询时，拿到的是类型擦除后的 packed 句柄（与 `Content`/`RawContent` 同布局）。能力 trait 的方法必须能在这种擦除句柄上调用，因此实现写在 `Packed<HeadingElem>` 上；`with::<C>()` 再用 `Packed<E>` 的 vtable 重建出 `&dyn C` 胖指针。

**练习 2**：`Construct::construct` 收到的 `args` 是「全部参数」吗？

> 参考答案：不是。文档明确说明它收到的是「执行完该元素的 set 规则后剩余的参数」——`settable` 字段已被 `Set::set` 消费掉，`construct` 只处理剩余的（通常是 `required` 的）构造字段。

---

### 4.4 能力（capability）字段与 can\<C\>/with\<C\> 查询

#### 4.4.1 概念说明

到这里，最关键的问题来了：`Content` 已经被类型擦除了，运行期怎么知道它「能不能被引用」「能不能进目录」「能不能被计数」？这就是**能力查询机制**要解决的。

Typst 的做法是在 vtable 上放一个特殊的函数指针：

```rust
capability: fn(capability: TypeId) -> Option<NonNull<()>>
```

给定一个 trait `C` 的 `TypeId`，这个函数返回 `Some(trait 的 vtable 指针)` 或 `None`。它由 `#[elem(...)]` 宏根据「属性里列出的能力」生成。于是：

- `Element::can::<C>()` / `Content::can::<C>()`：只关心有没有（`Option::is_some`）。
- `Content::with::<C>()`：不但确认有，还现场拼出一个 `&C`（胖指针），让你直接调用 trait 方法。

这相当于在「自己造的 `Arc<dyn Element>`」之上，按需把元素「下转」成 `&dyn Refable`、`&dyn Count` 等任意已登记的 trait 对象。这正是自定义 vtable + `fat::from_raw_parts` 带来的灵活性。

需要特别区分三类「能力」，它们在 vtable 里的归宿不同：

| 你在 `#[elem(...)]` 里写的 | 归宿 | 运行期查询方式 |
| --- | --- | --- |
| `Locatable` / `Unqueriable` / `Tagged` | `IntrospectionCapabilities` 的三个布尔 | `is_locatable()`/`is_unqueriable()`/`is_tagged()` |
| `Debug` / `PartialEq` / `Hash` / `Construct` / `Set` / `Repr` / `LocalName` | vtable 的专用槽（不是对象安全的，或单独处理） | 各自的专用方法（如 `with_repr` 生成的 `repr`） |
| 其它（`Synthesize` / `ShowSet` / `PlainText` / `Count` / `Refable` / `Outlinable` / `Figurable` …） | `capability` 查询表 | `can::<C>()` / `with::<C>()` |

这张分流表来自宏里的 `FORBIDDEN` 列表（下面会引用）。

#### 4.4.2 核心流程

能力查询的调用链（以 `content.with::<dyn Refable>()` 为例）：

```text
content.with::<dyn Refable>()
   │  (Content::with 委托 RawContent::with)
   ▼
RawContent::with::<C>()
   │  1. (self.elem.vtable().capability)(TypeId::of::<dyn Refable>())
   │     → 在该元素的 capability 闭包里逐个比对 TypeId
   │     → 命中则返回该 trait 的 vtable 指针；否则 None
   │  2. fat::from_raw_parts(数据指针, trait vtable 指针)
   │     → 现场拼出 &dyn Refable 胖指针
   ▼
Some(&dyn Refable)，可直接调用 Refable 的方法
```

`can::<C>()` 只走第 1 步并取 `.is_some()`，不构造胖指针，开销更低。而 `is::<E>()`（判断「是不是某个具体元素」，不是「有没有某个能力」）更便宜——它只比 vtable 地址相等。

宏生成的 `capability` 闭包逻辑很直白：对每个「非 FORBIDDEN」能力 `C`，生成一条 `if capability == TypeId::of::<dyn C>() { return Some(vtable of dyn C) }`。

#### 4.4.3 源码精读

`Element::can` 把泛型 `C` 转成 `TypeId` 再查：

[src/foundations/content/element.rs:81-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L81-L108) — `can::<C>()` 调 `can_type_id(TypeId::of::<C>())`，后者调 vtable 的 `capability` 函数指针看是否 `Some`。`is_locatable`/`is_unqueriable`/`is_tagged` 则直接读 vtable 的 `introspection` 布尔，不走 `capability`。

真正的「胖指针拼装」发生在 `RawContent::with` / `with_mut`：

[src/foundations/content/raw.rs:250-283](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L250-L283) — 先用 `capability(TypeId::of::<C>())` 拿到该 trait 的 vtable 指针，再用 `fat::from_raw_parts(data, vtable.as_ptr())` 把「数据指针 + trait vtable」组合成 `&C`。安全性来自宏生成的 `Capable` 实现「保证返回与 `Packed<T>`、`C` 同时匹配的 vtable」。注意它复用了 `RawContent` 自身的数据指针——因为 `Packed<T>`、`Content`、`RawContent` 三者 `repr(transparent)` 同布局。

而判断「是不是某具体元素」的 `is::<E>()` 只比地址：

[src/foundations/content/raw.rs:225-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L225-L228) — `self.elem == E::ELEM`，纯指针比较，无动态分发，正是 vtable 注释里强调的优化点。

`Content` 把这套 API 重新暴露给外部（注意 `can` 委托给 `Element`，`with` 委托给 `RawContent`）：

[src/foundations/content/mod.rs:251-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L251-L314) — 这一段集中了 `is::<T>()`、`to_packed::<T>()`、`unpack::<T>()`（精确下转回具体类型）、`can::<C>()`、三个 `is_*` 标志、以及 `with::<C>()`/`with_mut::<C>()`（按能力下转成 trait 对象）。两套下转的区别是：`to_packed/unpack` 目标是具体元素类型 `T`，`with` 目标是擦除的能力 trait `C`。

能力分流的关键在宏里的 `FORBIDDEN` 列表与 `capability` 闭包生成：

[crates/typst-macros/src/elem.rs:658-701](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L658-L701) — `FORBIDDEN` 把「不是对象安全 / 单独处理」与「内省能力」两类排除掉，只对剩余能力生成 `if capability == TypeId::of::<dyn C>() { return Some(...) }` 分支。所以 `Locatable` 不会出现在 `capability` 表里——它走的是 `IntrospectionCapabilities`。

内省标志由另一个函数生成（还顺带校验「`Unqueriable` 必须配合 `Locatable`」）：

[crates/typst-macros/src/elem.rs:705-727](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L705-L727) — `Locatable`/`Unqueriable`/`Tagged` 各自被翻译成一个布尔，组装成 `IntrospectionCapabilities`。`Unqueriable` 没有 `Locatable` 时直接编译期报错。

最后看两个真实的运行期用法，体会「按能力下转」如何简化调度代码：

`#ref` 引用：先尝试把目标下转成 `dyn Refable`，下转失败再用 `can::<dyn Figurable>()` 给出更友好的错误提示：

[src/model/reference.rs:284-297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L284-L297) — `elem.with::<dyn Refable>()` 拿到 `&dyn Refable`；若为 `None`，再判断 `elem.can::<dyn Figurable>()` 决定错误文案是「放进 figure 再引用」还是「无法引用」。

计数器：在遍历元素时按 `dyn Count` 下转决定如何更新计数：

[src/introspection/counter.rs:951-954](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L951-L954) — 能下转成 `dyn Count` 就调用其 `update()`；否则默认 `Step(1)`。这就是「标题、图、脚注等不同元素都能被同一个计数循环处理」的根本原因——它们都登记了 `Count` 能力。

#### 4.4.4 代码实践

**实践目标**：列出 `TextElem`/`HeadingElem` 各自具备的能力，并验证 `Element::construct`/`set` 如何被调用。

**操作步骤（源码阅读型）**：

1. 打开 `text/mod.rs:90` 与 `model/heading.rs:76`，分别抄下两行 `#[elem(...)]`。
2. 对照本节「三类能力归宿表」与宏的 `FORBIDDEN` 列表，把每个标识符归入：
   - **内省标志**（`Locatable`/`Unqueriable`/`Tagged`）
   - **vtable 专用槽**（`Debug`/`PartialEq`/`Hash`/`Construct`/`Set`/`Repr`/`LocalName`）
   - **能力表条目**（其余，可被 `can/with` 查询）
3. 填写下面这张预期对照表（答案见练习 1）：

| 元素 | 内省标志 | vtable 专用槽 | 能力表（can/with 可查） |
| --- | --- | --- | --- |
| `TextElem` | ? | ? | ? |
| `HeadingElem` | ? | ? | ? |

4. 说明 `Element::construct`/`Element::set` 如何被调用：在 `foundations/content/element.rs:65-79` 的基础上，解释「用户写 `#heading(level:1)[A]`」时，求值器先拿 `HeadingElem::ELEM` 这个 `Element` 句柄，调 `Element::set` 消费 `settable` 命名参数得到 `Styles`，再调 `Element::construct` 用剩余参数构造 `Content`」（具体的求值调度在 `typst-eval` crate，本讲只确认入口）。

**需要观察的现象**：`HeadingElem` 既有内省标志又有能力表条目，而 `TextElem` 几乎没有运行期可查能力（只有 `PlainText`）。

**预期结果**：你应当得出下表（见练习 1 答案），并能说清 `can::<dyn Refable>()` 对 `TextElem` 返回 `false`、对 `HeadingElem` 返回 `true` 的原因。

> 说明：本实践为「源码阅读型」，不涉及运行命令；具体运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：给出 4.4.4 实践中对照表的答案。

> 参考答案：
>
> | 元素 | 内省标志 | vtable 专用槽 | 能力表（can/with 可查） |
> | --- | --- | --- | --- |
> | `TextElem` | （无） | `Debug`、`Construct`、`Repr` | `PlainText` |
> | `HeadingElem` | `Locatable`、`Tagged` | `LocalName` | `Synthesize`、`Count`、`ShowSet`、`Refable`、`Outlinable` |
>
> 推导依据：`FORBIDDEN` 列表把 `Debug`/`Construct`/`Repr`/`LocalName`（专用槽）与 `Locatable`/`Unqueriable`/`Tagged`（内省标志）排除在 `capability` 表之外，其余进入能力表。

**练习 2**：`content.is::<HeadingElem>()` 与 `content.can::<dyn Refable>()` 有何区别？哪个更便宜？

> 参考答案：`is::<E>` 判断「内容是不是某个**具体元素类型**」，用 vtable 指针相等比较，无动态分发，最便宜。`can::<C>` 判断「内容是否具备某个**能力 trait**」，要调用 vtable 的 `capability` 函数指针、逐个比对 `TypeId`，略贵但仍只返回布尔。二者回答的是不同的问题：一个具体元素类型可以具备多种能力。

**练习 3**：为什么 `Unqueriable` 必须配合 `Locatable`？

> 参考答案：`Unqueriable` 的语义是「让一个本来 locatable 的元素对用户不可被 `query`」（例如某些内部辅助元素）。它依赖 `Locatable` 提供的位置信息才能工作，所以宏在 `create_introspection_capabilities`（elem.rs:711-714）里强制要求：标了 `Unqueriable` 却没标 `Locatable`，直接编译期报错。

---

## 5. 综合实践

把本讲四块知识串起来：**追踪一次「按能力下转」的完整旅程**。

1. **起点**：在 `model/reference.rs:285` 的 `elem.with::<dyn Refable>()` 处，确认 `elem` 此时是一个类型擦除的 `Content`。
2. **向上**：跳到 `foundations/content/mod.rs:300`（`Content::with`），看它委托给 `RawContent::with`。
3. **内核**：跳到 `foundations/content/raw.rs:250`（`RawContent::with`），抄下它 (a) 调 vtable 的 `capability` 拿 trait vtable 指针、(b) 用 `fat::from_raw_parts` 拼胖指针 两步。
4. **来源**：跳到 `crates/typst-macros/src/elem.rs:683`（生成 `capability` 闭包），解释「`HeadingElem` 因为在 `#[elem(...)]` 里列了 `Refable`，所以这里有一条 `if capability == TypeId::of::<dyn Refable>()` 分支返回 `dyn Refable` 的 vtable」。
5. **落地**：回到 `reference.rs:299`，看拿到 `&dyn Refable` 后如何调用其方法（`refable.numbering(...)` 等）。

最后用一段话回答：**为什么 Typst 要自造 vtable、而不是直接 `Box<dyn SomeCommonTrait>`？** 至少应提到三点——(a) 能挂字段子表做字段级操作；(b) 能放纯数据、免分发访问元数据；(c) 用 vtable 指针相等做 `is` 判断，并支持「一个擦除对象按需下转成多种不同能力的 trait 对象」。

> 说明：本实践为源码阅读型，无需运行；若要在本地验证，可在 `typst-library` 里新增一个临时测试（仅本地实验，勿提交），构造一个 `HeadingElem` 的 `Content`，断言 `content.can::<dyn Refable>()` 为真、`content.can::<dyn Figurable>()` 为假——具体编译运行「待本地验证」。

## 6. 本讲小结

- `Element` 是一个 `Copy` 的、**类型擦除的元素句柄**，内部只持有一张指向 `static` vtable 的指针；它代表「元素类型」而非实例。
- `ContentVtable` 是 Typst **自造的虚函数表**（`#[repr(C)]`），既装函数指针也装纯数据；通过 `erase()`（`transmute`）把 `ContentVtable<Packed<E>>` 变成统一的 `ContentVtable<RawContent>`，unsafe 边界收敛在 `Handle`。
- `NativeElement` 是 Rust 类型化一侧的源头（`const ELEM` + `pack`），要求实现 `Construct`/`Set`；元素按需 `impl` 一批能力 trait（`Synthesize`/`ShowSet`/`PlainText`/`Count`/`Refable`/`Outlinable`/`Figurable`…，通常为 `Packed<E>` 实现）。
- 能力分三类归宿：`Locatable`/`Unqueriable`/`Tagged` → `IntrospectionCapabilities` 布尔；`Debug`/`PartialEq`/`Hash`/`Construct`/`Set`/`Repr`/`LocalName` → vtable 专用槽；其余 → `capability` 查询表。
- `can::<C>()` 查「有没有某能力」，`with::<C>()` 现场拼出 `&C` 胖指针直接调用 trait 方法；`is::<E>()` 则用指针相等判断「是不是某具体元素」，三者开销与语义各不同。
- 本讲只触及「元素类型层」与「能力查询」；字段如何声明/取值/折叠（`#[elem]` 的字段标注与 `Packed<T>`）留给 u3-l3，样式折叠留给 u4。

## 7. 下一步学习建议

- 下一讲 **u3-l3「elem 宏、字段系统与 Packed」** 会深入本讲反复提到的 `Packed<T>` 与 `FieldVtable`——看清 `#[elem]` 如何为每个字段生成 `required`/`default`/`ghost`/`fold`/`parse` 行为，以及类型擦除后如何取回字段值。
- 若想先看「能力」在更高层如何被消费，可跳读 u9（内省与上下文），那里的 `query`/`Counter`/`Introspector` 大量使用本讲的 `with::<dyn ...>()`。
- 对 vtable 的内存布局细节感兴趣，建议结合 `typst-utils` 里的 `fat::from_raw_parts` 实现一起读，理解「自定义胖指针」的底层构造。
