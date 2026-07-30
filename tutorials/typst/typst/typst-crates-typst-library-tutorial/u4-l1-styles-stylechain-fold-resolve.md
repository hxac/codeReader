# Styles、StyleChain 与 fold/resolve

## 1. 本讲目标

本讲是「样式系统」单元的第一篇，专门回答一个问题：**当用户写下一堆 `set` 规则后，Typst 在编译期到底把这些规则存成了什么、又如何在排版时把「最终生效的值」查出来。**

学完本讲，你应当能够：

- 说清 `Styles`、`Style`、`Property`、`Recipe` 四者的层次关系，以及一条 `set` 规则是如何变成一个 `Property` 被存进样式列表的。
- 理解 `StyleChain` 作为「不可变、零分配的链表视图」如何把多层样式栈串联起来，并按「最内层优先」的顺序查询。
- 区分两种取值方式：**覆盖式（unfolded，后者覆盖前者）** 与 **折叠式（folded，层层叠加）**，并掌握 `Fold` trait 的结合律要求。
- 理解 `Resolve` trait 为何必须再吃一个 `StyleChain` 参数——有些值（如 `em`、百分比、`auto`）只有结合整条样式链才能解析成绝对值。
- 了解 `Smart<T>`（`Auto` / `Custom`）如何充当「智能默认」开关。

## 2. 前置知识

本讲建立在 u3（Content 与元素系统）之上，特别依赖 u3-l3 讲过的 **`#[elem]` 字段标注**。开始前请确认你理解以下概念：

- **`Content` / `Element`**：一切标记与函数调用的产物都是 `Content`；`Element` 是类型擦除的元素句柄（u3-l1、u3-l2）。
- **可设置字段（settable field）**：在 `#[elem]` 宏中，带有默认值、可被 `set` 规则修改的字段。它们又分两类：
  - 普通可设置字段：同时活在「元素实例」和「样式链」里。
  - **`#[ghost]` 幽灵字段**：**只活在样式链里，从不出现在元素实例上**。本讲的字号、字色、方向等几乎都是幽灵字段——这正是为什么它们必须靠 `StyleChain` 查询。
- **`#[fold]` 标注**：u3-l3 提到，标了 `#[fold]` 的字段在取值时不是「后者覆盖前者」，而是「折叠」。本讲会讲清折叠到底怎么发生。
- **`set` 规则**：Typst 语法里 `set text(size: 20pt)` 这样的语句，它会生成一条样式，作用到后续所有内容上。

如果你对「字段如何被擦除成 `Content` 又取回」还不熟悉，建议先读 u3-l3。本讲不再重复字段系统的细节，而是聚焦「样式如何存、如何查」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/foundations/styles.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs) | **本讲主文件**。定义 `Styles`/`Style`/`Property`/`Recipe`/`StyleChain`/`Fold`/`Resolve` 全部核心类型。 |
| [src/foundations/content/field.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs) | 定义 `Field` 零大小访问器与 `SettableProperty` trait（含 `FOLD` 常量与 `with_fold`）。连接「字段标注」与「样式查询」。 |
| [src/text/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs) | `TextElem` 的字段标注（`#[ghost]`/`#[fold]`），以及 `TextSize`、`TextDir` 等的 `Fold`/`Resolve` 实现——本讲的主要实践对象。 |
| [src/layout/length.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs) | `Length` 结构与它的 `Resolve`/`Fold` 实现。 |
| [src/layout/em.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs) | `Em::resolve`——展示「依赖整条样式链」的递归解析。 |
| [src/foundations/auto.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs) | `Smart<T>` 枚举（`Auto` / `Custom`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 Styles 与 Style：样式存成什么

#### 4.1.1 概念说明

当你在 Typst 里写：

```typst
#set text(size: 20pt)
#set rect(stroke: red)
#show heading: it => it.body
```

编译器并不会立刻把这些规则「应用」到任何元素上。相反，它把每条规则打包成一个**样式条目（style entry）**，塞进一个有序列表里，随着内容一起流动。等到真正要排布某个元素时，再去这个列表里查询「对我生效的值是什么」。

这套机制的核心数据结构有三个层次：

- **`Styles`**：一个有序的样式条目列表（包了一层 `EcoVec`）。
- **`Style`**：列表里的单条样式，是一个枚举，有三种变体。
- **`Property` / `Recipe`**：`Style` 枚举的两个主要载荷——前者来自 `set` 规则，后者来自 `show` 规则。

为什么要设计成「列表」而不是「哈希表」？因为样式天然是**有序、可叠加**的：同一个属性可能被多条 `set` 规则反复设置，谁覆盖谁、谁和谁折叠，都取决于顺序。列表保留了顺序语义。

#### 4.1.2 核心流程

一条 `set` 规则的生命周期：

```
set text(size: 20pt)
        │
        │  (求值期，由 typst-eval 触发)
        ▼
Styles::set(TextElem::size, TextSize(20pt))
        │  即 Property::new(field, value)
        ▼
Property { elem: TextElem, id: <size 的字段 id>, value: Block(20pt), .. }
        │
        ▼  push 进某个 Styles 列表
Styles([ .., LazyHash<Style::Property(..)> ])
```

几个关键点：

1. `Property` 用 `(elem, id)` 二元组定位「这是哪个元素的哪个字段」，`id` 是一个 `u8` 字段编号。
2. `value` 被装进一个类型擦除的 `Block`（见 4.1.3），查询时再 downcast 回具体类型。
3. `Styles` 支持 `apply`（把外层样式并进来）、`spanned`（给所有条目打上来源 span）、`outside`/`liftable`（标记样式是否来自 show 规则之外、能否上提到页面级）等批量操作。

#### 4.1.3 源码精读

**`Styles` 是对 `EcoVec<LazyHash<Style>>` 的新类型包装**——一个写时复制的向量，里面装的是「已预先算好哈希的样式条目」：

[styles.rs:22-25 — `Styles` 结构定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L22-L25)

```rust
#[ty(cast)]
#[derive(Default, Clone, PartialEq, Hash)]
pub struct Styles(EcoVec<LazyHash<Style>>);
```

> 说明：`LazyHash<Style>` 表示「懒计算哈希的 `Style`」。因为样式条目在增量编译（comemo）里要反复参与哈希比对，预先缓存哈希能避免重复计算。这也是 u12-l2 会讲的性能手段之一。

**`Style` 枚举有三个变体**：

[styles.rs:214-227 — `Style` 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L214-L227)

```rust
pub enum Style {
    /// 来自 set 规则或构造器的样式属性。
    Property(Property),
    /// show 规则的 recipe。
    Recipe(Recipe),
    /// 禁用某条 show recipe（目前只对正则 recipe 生效）。
    Revocation(RecipeIndex),
}
```

> 说明：本讲聚焦 `Property`；`Recipe` 是 u4-l2 的主题，这里只需知道它和 `Property` 一样是列表里的一种条目。

**`Property` 用 `(elem, id)` 定位字段**：

[styles.rs:316-331 — `Property` 结构](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L316-L331)

```rust
pub struct Property {
    elem: Element,   // 属于哪个元素
    id: u8,          // 该元素的第几个字段
    value: Block,    // 类型擦除的值
    span: Span,      // set 规则的来源位置
    liftable: bool,  // 能否上提到页面级
    outside: bool,   // 是否在 show 规则之外应用
}
```

> 说明：`elem` + `id` 就是字段的「身份证」。`value` 是 `Block`（见下），`span` 让错误信息能指回源码位置。`liftable`/`outside` 两个 bool 用于页面页脚、脚注等「根级内容」的样式筛选（见 `Styles::root`）。

**`Property::new` 由字段访问器 `Field` 构造**，把值装进 `Block`：

[styles.rs:333-348 — `Property::new`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L333-L348)

```rust
pub fn new<E, const I: u8>(_: Field<E, I>, value: E::Type) -> Self
where E: SettableProperty<I>, ... {
    Self {
        elem: E::ELEM,
        id: I,
        value: Block::new(value),
        span: Span::detached(),
        liftable: false,
        outside: false,
    }
}
```

> 说明：`Field<E, I>` 是一个零大小的访问器（见 4.2.3），编译期就携带了「元素类型 `E` 和字段编号 `I`」的信息，所以函数体里能直接用 `E::ELEM` 和 `I`，无需运行期参数。这也是为什么 `Styles::set` 的签名能写成 `set<E, const I: u8>(&mut self, field: Field<E,I>, value)`——`field` 参数在运行期其实是空的，只为让 Rust 推断出返回类型。

**`Block` 是类型擦除的值盒子**：

[styles.rs:379-400 — `Block` 与 `downcast`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L379-L400)

```rust
struct Block(Box<dyn Blockable>);

impl Block {
    fn new<T: Blockable>(value: T) -> Self { Self(Box::new(value)) }
    fn downcast<T: 'static>(&self, func: Element, id: u8) -> &T {
        let inner: &dyn Blockable = &*self.0;
        (inner as &dyn Any).downcast_ref()
            .unwrap_or_else(|| block_wrong_type(func, id, self))
    }
}
```

> 说明：因为同一个 `Styles` 列表里要混存各种不同类型的字段值（字号是 `TextSize`、颜色是 `Paint`、stroke 是 `Stroke`……），`value` 必须类型擦除。`Block` 用 trait 对象 `Box<dyn Blockable>` 存储，查询时靠 `Any::downcast_ref` 还原具体类型。如果写进去的和读出来的类型对不上，`block_wrong_type` 会直接 panic——这是编程错误（内部 bug），不是用户错误。

**`Styles::set` 就是 push 一条 `Property`**：

[styles.rs:48-59 — `Styles::set`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L48-L59)

```rust
pub fn set<E, const I: u8>(&mut self, field: Field<E, I>, value: E::Type)
where E: SettableProperty<I>, ... {
    self.push(Property::new(field, value));
}
```

**`Styles::apply` 把外层样式合并进来**（注意顺序：外层在前）：

[styles.rs:71-75 — `Styles::apply`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L71-L75)

```rust
pub fn apply(&mut self, mut outer: Self) {
    outer.0.extend(mem::take(self).0);
    *self = outer;
}
```

> 说明：`apply` 把 `self`（内层）追加到 `outer`（外层）的后面，再整体替换。结果是「外层在前、内层在后」的一个列表。这与 `StyleChain::chain` 的语义一致（见 4.2）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证「一条 `set` 规则 → 一个 `Property`」的对应关系，并理解 `(elem, id)` 的定位方式。

**操作步骤**：

1. 打开 [text/mod.rs:276-294](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L276-L294)，观察 `TextElem` 的 `size` 字段标注：

   ```rust
   #[parse(args.named_or_find("size")?)]
   #[fold]
   #[default(TextSize(Abs::pt(11.0).into()))]
   #[ghost]
   pub size: TextSize,
   ```

   注意它同时标了 `#[ghost]`（只活在样式链）、`#[fold]`（取值时折叠）、`#[default(...)]`（默认 11pt）。
2. 在本仓库内搜索 `TextElem::size` 的使用点，例如用 `Grep` 搜 `TextElem::size`，你会看到形如 `styles.get(TextElem::size)` 或 `styles.resolve(TextElem::size)` 的调用。
3. 追溯：这些调用最终走 `StyleChain::get_cloned` / `StyleChain::resolve`（见 4.2、4.4），而 `set text(size: 20pt)` 这条规则在求值期经 `TextElem::set` 生成 `Property { elem: TextElem, id: <size>, value: Block(TextSize(20pt)) }`。

**需要观察的现象**：`size` 字段是 `#[ghost]` 的，所以 `TextElem` 的 struct 里**根本没有** `size` 这个字段（u3-l3 讲过 ghost 字段不入 struct）。它的值只能从样式链查——这就是为什么本讲整篇都在讲「查询」。

**预期结果**：你能用自己的话说出「`set text(size: 20pt)` 不会修改任何元素实例，只是往样式列表里 push 了一条 `Property`」。

#### 4.1.5 小练习与答案

**练习 1**：`Style` 枚举有哪三个变体？其中哪个对应 `set` 规则、哪个对应 `show` 规则？

> **答案**：`Property`（set 规则）、`Recipe`（show 规则）、`Revocation`（禁用某条 recipe）。

**练习 2**：`Property` 用哪两个字段唯一确定「这是哪个元素的哪个属性」？为什么 `value` 要用类型擦除的 `Block` 存储？

> **答案**：`elem: Element` 和 `id: u8`。因为同一个 `Styles` 列表要混存不同类型的字段值（`TextSize`/`Paint`/`Stroke` 等），必须类型擦除，查询时再 `downcast` 回具体类型。

---

### 4.2 StyleChain：链式查询的内层机制

#### 4.2.1 概念说明

真实的文档里，样式是**分层嵌套**的。考虑：

```typst
#set text(size: 20pt)        // 最外层
#box[
  #set text(fill: red)       // 中间层
  Hello #text(size: 1.5em)[big]  // 最内层
]
```

「big」这个词同时受到三层样式的影响。如果每次进入一个新作用域都把所有样式**合并复制**一遍，既慢又浪费内存。Typst 的做法是：**不合并，只串联视图**。

`StyleChain` 就是一个「类似链表」的不可变视图，它把多层 `Styles` 像链条一样串起来，查询时从**最内层（最具体）往最外层（最一般）**走，找到第一个匹配项就返回（覆盖式），或者把所有匹配项折叠起来（折叠式）。因为它只是持有引用（`head: &'a [..]` + `tail: Option<&'a Self>`），**构造和传递都是零分配的**。

#### 4.2.2 核心流程

`StyleChain` 的查询分两条路径，取决于字段是否标了 `#[fold]`：

```
StyleChain::get_cloned(field)
        │
        ├── 字段有 FOLD?  ─── 是 ──▶ get_folded:  收集链上所有匹配，reduce(fold)，最后 fold(default)
        │
        └── 否 ───────────────▶ get_unfolded:  返回链上第一个（最内层）匹配；没有则 default
```

关键约定：

- **覆盖式（unfolded）**：最内层的 `set` 规则完全覆盖外层。绝大多数字段属于此类（如 `fill`、`lang`）。
- **折叠式（folded）**：链上每一层都参与，按 `Fold::fold` 合并。字号、字重增量、斜体开关、stroke 属于此类。
- **查询方向**：`Entries` 迭代器按「最内层 → 最外层」产出条目（见 4.2.3 的 `next_back` 细节）。
- **链的构造**：`StyleChain::chain(local)` 把 `local` 作为新的 head 挂在 `self` 前面，`local` 的优先级更高。

`StyleChain` 还是 `Copy` 的——它只是两个引用，复制它极其廉价。它的 `PartialEq` 也只比较指针相等（见源码精读），这让 comemo 的增量记忆化能高效判断「样式链没变」。

#### 4.2.3 源码精读

**`StyleChain` 是 head + tail 的链表**：

[styles.rs:557-570 — `StyleChain` 结构](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L557-L570)

```rust
/// 一条样式链，类似链表。
///
/// 它以非分配的方式把多层元素层级里的属性组合起来。
/// 不是急着合并列表，而是每次访问都从最内层往最外层走，
/// 找到匹配后再和更上层的匹配折叠。
#[derive(Default, Copy, Clone, Hash)]
pub struct StyleChain<'a> {
    head: &'a [LazyHash<Style>],   // 当前这一层（最内层）的样式切片
    tail: Option<&'a Self>,        // 外层链
}
```

> 说明：`head` 是一个样式条目的切片引用，`tail` 指向外层 `StyleChain`。因为只持有引用，`StyleChain` 是 `Copy + Default` 的，到处传递零成本。

**`chain` 把新的一层挂到最前面**：

[styles.rs:679-689 — `StyleChain::chain`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L679-L689)

```rust
pub fn chain<'b, C>(&'b self, local: &'b C) -> StyleChain<'b>
where C: Chainable + ?Sized {
    Chainable::chain(local, self)
}
```

`Chainable` 是个 trait，`Styles`、`[LazyHash<Style>]`、单个 `LazyHash<Style>` 都实现了它。以 `Styles` 为例：

[styles.rs:819-823 — `Chainable for Styles`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L819-L823)

```rust
impl Chainable for Styles {
    fn chain<'a>(&'a self, outer: &'a StyleChain<'_>) -> StyleChain<'a> {
        Chainable::chain(self.0.as_slice(), outer)
    }
}
```

> 说明：`local` 成为新 head，`self`（原链）成为 tail。`local` 的优先级最高。注意切片版本对空切片做了短路（直接返回 outer），避免无意义的空链节点。

**`get_cloned` 是查询的总入口，按 `FOLD` 分派**：

[styles.rs:600-611 — `StyleChain::get_cloned`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L600-L611)

```rust
pub fn get_cloned<E, const I: u8>(self, _: Field<E, I>) -> E::Type
where E: SettableProperty<I> {
    if let Some(fold) = E::FOLD {
        self.get_folded::<E::Type>(E::ELEM, I, fold, E::default())
    } else {
        self.get_unfolded::<E::Type>(E::ELEM, I)
            .cloned()
            .unwrap_or_else(E::default)
    }
}
```

> 说明：`E::FOLD` 是个编译期常量（`Option<fn(T,T)->T>`），由 `#[fold]` 标注在宏展开时设为 `Some(..)`（见 4.3.3）。所以「这个字段要不要折叠」是编译期就定死的，运行期只是分派。`Field<E,I>` 参数同样是零大小的类型推断辅助。

**覆盖式查询 `get_unfolded` + `find`**：返回链上第一个匹配（最内层），没有就用默认值：

[styles.rs:644-669 — `get_unfolded` 与 `find`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L644-L669)

```rust
fn get_unfolded<T: 'static>(self, func: Element, id: u8) -> Option<&'a T> {
    self.find(func, id).map(|block| block.downcast(func, id))
}
fn find(self, func: Element, id: u8) -> Option<&'a Block> {
    self.properties(func, id).next()   // 第一个 = 最内层
}
```

**折叠式查询 `get_folded`**：收集链上全部匹配，`reduce(fold)`，最后再 fold 一次默认值：

[styles.rs:650-664 — `StyleChain::get_folded`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L650-L664)

```rust
fn get_folded<T: 'static + Clone>(
    self, func: Element, id: u8, fold: fn(T, T) -> T, default: T,
) -> T {
    let iter = self.properties(func, id)
        .map(|block| block.downcast::<T>(func, id).clone());
    if let Some(folded) = iter.reduce(fold) { fold(folded, default) } else { default }
}
```

> 说明：`iter.reduce(fold)` 把最内层的值作为累加器，逐个 fold 进更外层的值；最后 `fold(folded, default)` 把默认值当作「最外层」也 fold 进来。若链上完全没有该属性（`iter` 为空），直接返回 `default`。这就是为什么 `#[fold]` 字段即使没被任何 `set` 设置，也能拿到默认值。

**`properties` 迭代器过滤出指定 `(elem, id)` 的全部条目**：

[styles.rs:672-677 — `StyleChain::properties`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L672-L677)

```rust
fn properties(self, func: Element, id: u8) -> impl Iterator<Item = &'a Block> {
    self.entries()
        .filter_map(|style| style.property())
        .filter(move |property| property.is(func, id))
        .map(|property| &property.value)
}
```

**`Entries` 迭代器按「最内层 → 最外层」产出**：

[styles.rs:825-846 — `Entries` 迭代器](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L825-L846)

```rust
fn next(&mut self) -> Option<Self::Item> {
    loop {
        if let Some(entry) = self.inner.next_back() {  // 注意：next_back
            return Some(entry);
        }
        match self.links.next() {
            Some(next) => self.inner = next.iter(),
            None => return None,
        }
    }
}
```

> 说明：`Links` 迭代器先产出 head（最内层链），再产出 tail。而在每一层切片内部，`Entries` 用 `next_back()` **倒序**取——因为同一个 `Styles` 列表里，后 push 的条目更新、优先级更高。两者合起来，`entries()` 就实现了「从最具体到最一般」的遍历顺序，这正是 `find().next()` 取「最内层优先」、`reduce(fold)` 按「内层 fold 外层」正确折叠的基础。

**`PartialEq` 只比指针**——这让 comemo 能 O(1) 判断样式链是否变化：

[styles.rs:777-786 — `StyleChain` 的 `PartialEq`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L777-L786)

```rust
impl PartialEq for StyleChain<'_> {
    fn eq(&self, other: &Self) -> bool {
        ptr::eq(self.head, other.head)
            && match (self.tail, other.tail) {
                (Some(a), Some(b)) => ptr::eq(a, b),
                (None, None) => true,
                _ => false,
            }
    }
}
```

> 说明：两条 `StyleChain` 相等当且仅当它们指向同一份 `head` 切片和同一串 `tail`。因为样式是不可变的（`LazyHash` + 写时复制），指针相等就等价于内容相等，且是 O(深度) 而非 O(条目数)。这是增量编译能高效缓存的关键（u12-l2 会展开）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：跟踪一次样式查询的全过程，确认「最内层优先」的覆盖语义。

**操作步骤**：

1. 想象这段 Typst 代码的样式栈：
   ```typst
   #set text(fill: blue)        // 外层 Styles A
   #box[
     #set text(fill: red)       // 内层 Styles B
     text                       // 这个词的 fill 是？
   ]
   ```
2. 在源码里定位 `fill` 字段：[text/mod.rs:296-316](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L296-L316)，确认它**没有** `#[fold]` 标注（只有 `#[default]` 和 `#[ghost]`），所以是**覆盖式**。
3. 因此查询走 `get_cloned → get_unfolded → find → properties().next()`，返回链上**第一个**匹配，即最内层的 `red`。

**需要观察的现象**：`fill` 是覆盖式，所以即使外层设了 `blue`，只要内层设了 `red`，结果就是 `red`；如果内层没设，才回退到外层的 `blue`；如果都没设，用默认 `Color::BLACK`（来自 `#[default(Color::BLACK.into())]`）。

**预期结果**：你能解释为什么覆盖式查询只需要 `find().next()`（取第一个），而折叠式需要 `reduce(fold)`（收集全部）。

> 待本地验证：如果你想实际观察，可在 typst 主 crate 里给 `get_cloned` 临时加一行日志（仅用于学习，勿提交），打印 `func.name()` 和 `id`，编译一个含上述样式的文档，观察查询顺序。

#### 4.2.5 小练习与答案

**练习 1**：`StyleChain` 为什么设计成 `Copy`？它的 `PartialEq` 为什么只比较指针？

> **答案**：因为它只持有两个引用（`head` 切片 + `tail`），复制零成本；样式条目是不可变的（写时复制），指针相等即内容相等，O(深度) 比指针让 comemo 增量记忆化能高效判断「样式没变」。

**练习 2**：`get_cloned` 如何决定走覆盖式还是折叠式？这个决定是在编译期还是运行期做出的？

> **答案**：看 `E::FOLD` 是否为 `Some`。`FOLD` 是编译期常量（由 `#[fold]` 标注在宏展开时设定），所以是编译期决定，运行期只是分派。

**练习 3**：`Entries` 迭代器在每一层切片内部用 `next_back()`（倒序）取元素，为什么？

> **答案**：同一个 `Styles` 列表里后 `push` 的条目更新、优先级更高，倒序取保证「最具体（最新）的先产出」，配合 `find().next()` 实现覆盖语义。

---

### 4.3 Fold：折叠而非覆盖

#### 4.3.1 概念说明

「覆盖」是最直觉的语义：后写的 `set` 规则完全替换前面的。但有些属性天然需要**叠加**。最典型的就是**字号**：

```typst
#set text(size: 20pt)
very #text(1.5em)[big] text
```

`1.5em` 不是「替换成 1.5em」，而是「在当前 20pt 的基础上放大 1.5 倍 = 30pt」。如果用覆盖语义，`1.5em` 就失去了相对含义。所以这类字段标了 `#[fold]`，取值时不是取最后一个，而是把链上所有值**折叠**成一个最终值。

`Fold` trait 就是描述「两个同类型值如何合并」的接口。不同的字段有不同的折叠策略：

| 字段类型 | 折叠策略 | 含义 |
| --- | --- | --- |
| `bool` | 取内层（忽略外层） | 后者覆盖，但走 fold 路径 |
| `Option<T>` | 内层 `None` 被尊重 | 显式 `None` 不被外层覆盖 |
| `Vec<T>` / `SmallVec` | `outer.extend(inner)` | 列表拼接 |
| `TextSize`（字号） | **线性函数相乘**（见下） | 相对字号层层放大 |
| `WeightDelta`（字重增量） | 相加 | `+200` 再 `+100` = `+300` |
| `ItalicToggle`（斜体开关） | 异或 | 连续两次 `*` 抵消 |
| `Depth` | 相加 | 嵌套层级累加 |

一个硬性要求：**折叠必须满足结合律**，即 `fold(fold(a, b), c) == fold(a, fold(b, c))`。因为 `get_folded` 用 `Iterator::reduce` 折叠，而 `reduce` 的语义依赖结合律才能保证顺序无关的正确性。

#### 4.3.2 核心流程

字段标注如何变成「折叠」：

```
源码：#[elem] pub size: TextSize { #[fold] }
        │  typst-macros 的 elem 宏展开
        ▼
SettablePropertyData::new(..).with_fold()   // 把 fold 字段设为 Some(TextSize::fold)
        │
        ▼
const FOLD: Option<FoldFn<TextSize>> = Some(TextSize::fold)
        │  查询时
        ▼
StyleChain::get_cloned 看到 E::FOLD = Some(..)  →  get_folded(.., fold, default)
        │
        ▼
iter.reduce(fold)  把链上每个 TextSize 折叠，最后 fold(default)
```

#### 4.3.3 源码精读

**`Fold` trait 只有一个方法，签名是「内层 fold 外层」**：

[styles.rs:878-894 — `Fold` trait](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L878-L894)

```rust
/// 一个通过折叠来确定最终值的属性。
///
/// 折叠必须满足结合律：fold(fold(a, b), c) == fold(a, fold(b, c))。
pub trait Fold {
    /// 把这个内层值与一个已折叠的外层值合并。
    fn fold(self, outer: Self) -> Self;
}
```

> 说明：约定 `self` 是**内层**（更具体、更新），`outer` 是**外层**（更一般、更早）。`get_folded` 里 `reduce(fold)` 以最内层为累加器起步，逐层 fold 进外层值，最后 fold 进默认值。理解这个「内外」方向是看懂各 `fold` 实现的关键。

**几个内建 `Fold` 实现，策略各异**：

[styles.rs:896-932 — `bool` / `Option` / `Vec` / `SmallVec` / `OneOrMultiple` 的 Fold](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L896-L932)

```rust
impl Fold for bool {
    fn fold(self, _: Self) -> Self { self }            // 取内层
}
impl<T: Fold> Fold for Option<T> {
    fn fold(self, outer: Self) -> Self {
        match (self, outer) {
            (Some(inner), Some(outer)) => Some(inner.fold(outer)),
            (inner, _) => inner,                        // 内层 None 被尊重，不回退
        }
    }
}
impl<T> Fold for Vec<T> {
    fn fold(self, mut outer: Self) -> Self {
        outer.extend(self); outer                       // 拼接
    }
}
```

> 说明：注意 `Option<T>` 的 fold 与普通「`or` 回退」不同——内层显式的 `None` 会被尊重（注释特意说明「不写 `inner.or(outer)`」）。如果某个语境下 `None` 表示「未指定」而非「空」，应改用 `AlternativeFold::fold_or`（[styles.rs:948-963](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L948-L963)）。

**`Depth` 用相加来累加嵌套层级**：

[styles.rs:965-973 — `Depth` 的 Fold](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L965-L973)

```rust
pub struct Depth(pub usize);
impl Fold for Depth {
    fn fold(self, outer: Self) -> Self { Self(outer.0 + self.0) }
}
```

**字段侧：`FOLD` 常量与 `with_fold`**。`#[fold]` 标注在 `elem` 宏展开时调用 `with_fold`，把 `fold` 字段从 `None` 改成 `Some(E::Type::fold)`：

[field.rs:330-334 — `SettableProperty` 的 `FOLD` 常量](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L330-L334)

```rust
pub trait SettableProperty<const I: u8>: NativeElement {
    type Type: Clone;
    const FIELD: SettablePropertyData<Self, I>;
    const FOLD: Option<FoldFn<Self::Type>> = Self::FIELD.fold;
    // ...
}
```

[field.rs:395-402 — `with_fold` 开启折叠](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L395-L402)

```rust
pub const fn with_fold(self) -> Self
where E::Type: Fold {
    Self { fold: Some(E::Type::fold), ..self }
}
```

> 说明：这把「该字段的类型 `E::Type` 的 `Fold::fold` 函数指针」存进 `SettablePropertyData.fold`，再经 `FOLD` 常量暴露给 `StyleChain::get_cloned`。所以 `TextSize` 必须实现 `Fold`，它的 `fold` 才能被注册——这正是 `text/mod.rs` 里 `impl Fold for TextSize` 的存在理由。

**`FoldFn` 就是函数指针类型**：

[styles.rs:934-935 — `FoldFn` 类型别名](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L934-L935)

```rust
pub type FoldFn<T> = fn(T, T) -> T;
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：结合 `TextSize::fold`，解释**字号为何以「线性函数相乘」方式折叠**，并手算一个例子。

**操作步骤**：

1. 先理解 `Length` 的内部表示。[length.rs:42-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L42-L49)：

   ```rust
   pub struct Length {
       pub abs: Abs,   // 绝对分量（如 pt）
       pub em: Em,     // 字体相对分量（如 em）
   }
   ```

   一个 `Length` 实际上代表一个关于字号 `f` 的**线性函数**：

   \[
   \mathrm{value}(f) = \mathrm{em} \cdot f + \mathrm{abs}
   \]

   其中 `f` 是「上级字号」。纯 `20pt` 是 `{abs: 20, em: 0}`；纯 `1.5em` 是 `{abs: 0, em: 1.5}`。

2. 阅读 `TextSize::fold`：

   [text/mod.rs:1129-1137 — `TextSize::fold`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1129-L1137)

   ```rust
   impl Fold for TextSize {
       fn fold(self, outer: Self) -> Self {
           // 把两个线性函数相乘。
           Self(Length {
               em: Em::new(self.0.em.get() * outer.0.em.get()),
               abs: self.0.em.get() * outer.0.abs + self.0.abs,
           })
       }
   }
   ```

   这里 `self` 是内层（如 `1.5em`），`outer` 是外层（如 `20pt`）。代入公式，折叠本质是**复合两个线性函数**：

   \[
   (\mathrm{em}_i \cdot f + \mathrm{abs}_i) \circ (\mathrm{em}_o \cdot f + \mathrm{abs}_o)
   = \mathrm{em}_i \cdot \mathrm{em}_o \cdot f + (\mathrm{em}_i \cdot \mathrm{abs}_o + \mathrm{abs}_i)
   \]

   对应代码里：新 `em = em_i * em_o`，新 `abs = em_i * abs_o + abs_i`。

3. **手算例子**：

   | 场景 | 内层 `self` | 外层 `outer` | 折叠结果 |
   | --- | --- | --- | --- |
   | `1.5em` on `20pt` | `{em:1.5, abs:0}` | `{em:0, abs:20}` | `em=1.5*0=0, abs=1.5*20+0=30` → `30pt` |
   | `0.5em + 2pt` on `2em + 10pt` | `{em:0.5, abs:2}` | `{em:2, abs:10}` | `em=0.5*2=1, abs=0.5*10+2=7` → `1em + 7pt` |
   | `1.5em` on `2em`（纯相对） | `{em:1.5, abs:0}` | `{em:2, abs:0}` | `em=3, abs=0` → `3em`（仍相对） |

   验证第二个例子：`0.5 × (2em + 10pt) + 2pt = 1em + 5pt + 2pt = 1em + 7pt`。✓

4. **对比 `Length` 自带的平凡 fold**：注意 [length.rs:274-278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L274-L278) 里 `impl Fold for Length` 是「取内层、忽略外层」的平凡实现。`TextSize` 之所以要**重写** `fold`（用 `TextSize` 这个新类型包一层），正是因为字号需要「相乘」语义，而普通 `Length` 不需要。

**需要观察的现象**：折叠后，原本带 `em` 分量的内层值往往被「吸收」成纯绝对值（如 `1.5em` + `20pt` → `30pt`，`em` 变 0）。这意味着 `em` 的相对计算在**查询时（fold）**就完成了，而不是延迟到 resolve。

**预期结果**：你能解释「为什么字号用 fold 而不是覆盖」——因为相对字号 `1.5em` 必须作用于外层的绝对字号才有意义；线性函数相乘正是「相对 × 基准」的数学表达。

#### 4.3.5 小练习与答案

**练习 1**：`Fold` trait 的 `fold(self, outer)` 中，`self` 和 `outer` 哪个是内层（更具体）？为什么必须满足结合律？

> **答案**：`self` 是内层，`outer` 是外层。必须满足结合律是因为 `get_folded` 用 `Iterator::reduce(fold)` 折叠，而 `reduce` 的正确性依赖结合律（`fold(fold(a,b),c) == fold(a,fold(b,c))`）。

**练习 2**：手算 `text(2em)[x]` 作用在默认字号 `11pt` 上的折叠结果。

> **答案**：内层 `{em:2, abs:0}`，外层（默认）`{em:0, abs:11}`。折叠：`em = 2*0 = 0`，`abs = 2*11 + 0 = 22` → `22pt`。

**练习 3**：为什么 `TextSize` 要新建一个包装类型，而不是直接给 `Length` 实现「相乘」的 fold？

> **答案**：因为 `Length` 自身已经有 fold 实现（取内层的平凡版本，[length.rs:274-278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L274-L278)），而普通长度字段（如 `tracking`、`baseline`）用的是覆盖语义。新建 `TextSize(Length)` 这个 newtype 才能给「字号」单独实现「相乘」fold，互不干扰。

---

### 4.4 Resolve：依赖整条样式链才能定值

#### 4.4.1 概念说明

`Fold` 解决了「多个值合并成一个」，但合并出来的值可能**还是相对的**。比如上节算出 `1em + 7pt`，这个 `1em` 到底是多少 pt？它取决于「当前字号」——而当前字号本身又是一条样式。

也就是说，有些值**单靠自己无法定值**，必须结合整条样式链才能解析成绝对值。这就是 `Resolve` trait 的职责：它比 `Fold` 多吃一个 `StyleChain` 参数。

`Fold` 与 `Resolve` 的区别要记牢：

- **`Fold`**：`fn fold(self, outer: Self) -> Self`——**同类型**进出，不需要样式链，只是把多个同类型值合并。发生在 `get_cloned` 内部。
- **`Resolve`**：`fn resolve(self, styles: StyleChain) -> Self::Output`——**异类型**（`Self` → `Self::Output`），需要样式链，把相对值变成绝对值。发生在 `get_cloned` **之后**，由 `StyleChain::resolve` 触发。

一个值可以**既 fold 又 resolve**：`TextSize` 就是——先 fold 成一个 `Length`，再 resolve 成一个 `Abs`（绝对长度）。

#### 4.4.2 核心流程

```
StyleChain::resolve(field)
        │  = get_cloned(field) 然后 .resolve(self)
        ▼
TextSize::resolve(self, styles)        // self 是 fold 后的 TextSize
        │  1. 先算数学因子（上下标缩放）
        │  2. 调 self.0.resolve(styles)   // Length::resolve
        ▼
Length::resolve(self, styles)          // Length → Abs
        │  = self.abs + self.em.resolve(styles)
        ▼
Em::resolve(self, styles)              // Em → Abs
        │  = self.at(styles.resolve(TextElem::size))   // 又回到字号解析！
        ▼
（递归，直到 em 分量为 0 时触底返回 Abs::zero）
```

注意 `Em::resolve` 里又调了 `styles.resolve(TextElem::size)`——看似递归，其实会在 `em` 分量为 0 时触底（见 4.4.4 的分析）。

#### 4.4.3 源码精读

**`Resolve` trait 吃一个 `StyleChain`**：

[styles.rs:861-868 — `Resolve` trait](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L861-L868)

```rust
/// 一个需要结合样式链中其他属性来解析的属性。
pub trait Resolve {
    type Output;
    /// 用样式链解析这个值。
    fn resolve(self, styles: StyleChain) -> Self::Output;
}
```

**`StyleChain::resolve` 是「取值 + resolve」的便捷方法**：

[styles.rs:624-634 — `StyleChain::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L624-L634)

```rust
pub fn resolve<E, const I: u8>(
    self, field: Field<E, I>,
) -> <E::Type as Resolve>::Output
where E: SettableProperty<I>, E::Type: Resolve {
    self.get_cloned(field).resolve(self)
}
```

> 说明：先 `get_cloned`（内部可能 fold）拿到 `E::Type`，再对其调 `resolve(self)`——注意传的是**同一条** `StyleChain`。所以 resolve 看到的是完整样式链，能去查其他字段（如字号）。

**`TextSize::resolve`：先算数学因子，再 resolve 内部 `Length`**：

[text/mod.rs:1139-1152 — `TextSize::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1139-L1152)

```rust
impl Resolve for TextSize {
    type Output = Abs;
    fn resolve(self, styles: StyleChain) -> Self::Output {
        let factor = match styles.get(EquationElem::size) {
            MathSize::Display | MathSize::Text => 1.0,
            MathSize::Script => styles.get(EquationElem::script_scale).0 as f64 / 100.0,
            MathSize::ScriptScript => styles.get(EquationElem::script_scale).1 as f64 / 100.0,
        };
        factor * self.0.resolve(styles)
    }
}
```

> 说明：这里 `styles.get(EquationElem::size)` 是「在解析文本字号时，去查数学公式的上下文」——如果当前在数学公式的下标里（`MathSize::Script`），字号要再缩放一个因子。这正是 `Resolve` 依赖样式链的典型体现：一个字段的最终绝对值，可能依赖**另一个完全不同元素**的字段。最后 `self.0.resolve(styles)` 调到 `Length::resolve`。

**`Length::resolve` 把 `em` 分量解析掉**：

[length.rs:266-272 — `Length::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L266-L272)

```rust
impl Resolve for Length {
    type Output = Abs;
    fn resolve(self, styles: StyleChain) -> Self::Output {
        self.abs + self.em.resolve(styles)
    }
}
```

**`Em::resolve` 又回头查字号——递归触底的关键**：

[em.rs:157-163 — `Em::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/em.rs#L157-L163)

```rust
impl Resolve for Em {
    type Output = Abs;
    fn resolve(self, styles: StyleChain) -> Self::Output {
        if self.is_zero() { Abs::zero() }
        else { self.at(styles.resolve(TextElem::size)) }
    }
}
```

> 说明：把 `em` 换算成绝对值，需要乘以「当前字号」——而当前字号正是 `styles.resolve(TextElem::size)`。这看似会无限递归（`TextSize::resolve → Length::resolve → Em::resolve → TextSize::resolve → ...`），但触底条件是 `self.is_zero()`：当 fold 后的字号 `Length` 的 `em` 分量为 0（即已经是纯绝对值，如默认的 `11pt`），`Em::resolve` 直接返回 `Abs::zero()`，递归终止。实践中，fold 通常已经把相对 em 吸收成绝对值（见 4.3.4），所以 resolve 时 `em` 往往就是 0。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：举一个 `Resolve` 依赖 `StyleChain` 的例子，跟踪 `#set text(size: 20pt); #text(1.5em)[big]` 中「big」字号的完整解析链。

**操作步骤**：

1. **fold 阶段**（4.3.4 已算）：`1.5em`（内层）fold `20pt`（外层）= `30pt`，即 `{em:0, abs:30}`。这一步在 `StyleChain::get_cloned(TextElem::size)` 内部完成。
2. **resolve 阶段**：排版代码调 `styles.resolve(TextElem::size)`，即 `get_cloned(..).resolve(self)`：
   - `TextSize({em:0,abs:30}).resolve(styles)`：`factor = 1.0`（不在数学公式里），调 `Length::resolve`。
   - `Length({em:0,abs:30}).resolve(styles)`：`abs(30) + em(0).resolve(styles)`。
   - `Em(0).resolve(styles)`：`self.is_zero()` 为真 → 返回 `Abs::zero()`，**不触发递归**。
   - 最终 `Abs(30pt)`。
3. 对比一个**会触发递归**的场景：若 fold 结果仍带 `em`（比如 `1.5em` 直接 fold 默认的 `11pt` 之外的纯相对外层 `2em`，得 `3em`），则 `Em(3).resolve` 会去查 `styles.resolve(TextElem::size)`，递归一层。但因为默认字号 `11pt` 的 `em=0`，递归必在有限步内触底。

**需要观察的现象**：`TextSize::resolve` 里查的是 `EquationElem::size`（数学公式的字号上下文），这是「文本字段依赖数学元素字段」的跨元素依赖；而 `Em::resolve` 反过来又查 `TextElem::size`。这种「你查我、我查你」正是 `Resolve` 必须拿整条 `StyleChain` 的根本原因——任何字段的绝对值都可能依赖链上的其他字段。

**预期结果**：你能画出从 `TextSize::resolve` 到 `Abs` 的完整调用链，并指出触底条件是 `Em::is_zero()`。

> 待本地验证：递归深度的精确行为依赖具体样式栈；建议用 `cargo expand` 或在 `Em::resolve` 加临时日志观察一次真实文档的解析路径。

#### 4.4.5 小练习与答案

**练习 1**：`Fold` 和 `Resolve` 的方法签名有什么本质区别？它们分别在查询的哪个阶段发生？

> **答案**：`Fold::fold(self, outer: Self) -> Self` 同类型进出、不吃样式链；`Resolve::resolve(self, styles) -> Self::Output` 异类型、吃样式链。Fold 发生在 `get_cloned` 内部（合并多个值），Resolve 发生在 `get_cloned` 之后（把相对值变绝对值，由 `StyleChain::resolve` 触发）。

**练习 2**：`Em::resolve` 调用了 `styles.resolve(TextElem::size)`，为什么不会无限递归？

> **答案**：触底条件是 `self.is_zero()`。当待解析的 `em` 分量为 0（fold 通常已把相对 em 吸收成绝对值，或外层是纯绝对字号如默认 `11pt`），`Em::resolve` 直接返回 `Abs::zero()` 不再递归。

**练习 3**：`TextSize::resolve` 里为什么要查 `EquationElem::size`？

> **答案**：因为文本若出现在数学公式的上下标里，字号要按 `script_scale` 再缩放一个因子。这是「文本字段的绝对值依赖数学元素字段」的跨元素依赖，只有拿整条 `StyleChain` 才能查到。

---

### 4.5 Smart::Auto 与默认值机制

#### 4.5.1 概念说明

很多字段有三种状态：**用户显式指定了一个值**、**用户没指定（用默认）**、**用户写了 `auto`（让 Typst 自己决定）**。前两种用普通的默认值就能表达，但第三种需要专门的类型。

`Smart<T>` 就是这个三态类型：

```rust
pub enum Smart<T> {
    Auto,         // 「自动」，让引擎推断
    Custom(T),    // 用户显式给了一个值
}
```

它和 u2-l1 讲过的 `AutoValue`（`Value::Auto`）一脉相承——`AutoValue` 是 `Value` 层面的 `auto`，而 `Smart<T>` 是具体类型层面「这个字段可能是 auto」的表达。在样式系统里，`Smart` 常作为字段的值类型，让 `auto` 成为一种合法的、可与覆盖/折叠协作的取值。

#### 4.5.2 核心流程

`Smart` 在样式里的典型用法：

```
text(dir: auto)   →  TextDir(Smart::Auto)
text(dir: rtl)    →  TextDir(Smart::Custom(Dir::RTL))

查询时（TextDir 是覆盖式，没有 fold）：
  get_cloned(TextElem::dir) → TextDir(Smart::Auto) 或 Smart::Custom(rtl)

resolve 时：
  TextDir::resolve 看到 Smart::Auto
    → 回退到 styles.get(TextElem::lang).dir()  // 由语言推断方向
  看到 Smart::Custom(dir) → 直接用 dir
```

注意 `Smart` 自身也实现了 `Fold`（[auto.rs:273-276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L273-L276)）：两个 `Custom` 时折叠内层，否则按 `Option` 语义。这让带 `Smart` 的字段也能参与折叠。

#### 4.5.3 源码精读

**`Smart` 枚举**：

[auto.rs:67-72 — `Smart` 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L67-L72)

```rust
pub enum Smart<T> {
    Auto,
    Custom(T),
}
```

**`TextDir` 用 `Smart<Dir>` 表达「方向可自动」**：

[text/mod.rs:1250-1274 — `TextDir` 与其 `Resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1250-L1274)

```rust
pub struct TextDir(pub Smart<Dir>);

impl Resolve for TextDir {
    type Output = Dir;
    fn resolve(self, styles: StyleChain) -> Self::Output {
        match self.0 {
            Smart::Auto => styles.get(TextElem::lang).dir(),  // 由语言推断
            Smart::Custom(dir) => dir,
        }
    }
}
```

> 说明：这是 `Smart::Auto` 配合 `Resolve` 的经典范式——`auto` 不是「啥也不做」，而是「去样式链里查另一个字段（这里查 `lang`），由它推导出本字段的值」。`Dir`（方向）从 `Lang`（语言）推导：阿拉伯语 → RTL，其他 → LTR。类似的还有 `hyphenate: Smart<bool>`（auto 时由语言决定是否断行）、`number_type`/`number_width`（OpenType 数字特性，见 u7-l3）。

**`Smart<Smart<T>>` 还能去嵌套**：当字段本身允许 auto，而用户又可能写 auto 时，会出现双层嵌套，`Smart` 提供了 `flatten` 去掉一层（[auto.rs:211-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L211-L218)）。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：理解 `auto` 如何在「覆盖式取值」与「resolve 推导」之间分工。

**操作步骤**：

1. 在 [text/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs) 里用 `Grep` 搜 `Smart<`，统计有多少 `TextElem` 字段用 `Smart`（应有 `dir`、`hyphenate`、`number_type`、`number_width`、`cjk_latin_spacing`、`script` 等）。
2. 选 `hyphenate: Smart<bool>`（[text/mod.rs:565](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L565) 附近），思考：用户写 `#set text(hyphenate: auto)` 时，`Smart::Auto` 被存进样式；排版时 `hyphenate` 被解析成 `bool`——这个解析在哪发生？（提示：在断行模块 `linebreak.rs` 里，根据 `lang` 决定是否启用连字符断行。）
3. 对比 `TextDir::resolve`：`auto` 时调 `styles.get(TextElem::lang).dir()`。确认「auto 的具体含义由消费方（resolve/排版代码）定义」，`Smart` 本身只负责「记下用户是 auto 还是 custom」。

**需要观察的现象**：`Smart::Auto` 不会在 `get_cloned` 阶段被解析，它原样传给消费方；只有在 `resolve`（或排版逻辑）里，`auto` 才被翻译成具体值。这说明「默认值机制」分两层：`#[default]` 提供「完全没设置时的兜底」，`Smart::Auto` 提供「用户显式要自动时的推导入口」。

**预期结果**：你能区分三种「默认」：① 完全没写 `set` → 用 `#[default]`；② 写了 `set text(dir: rtl)` → `Smart::Custom(rtl)`；③ 写了 `set text(dir: auto)` → `Smart::Auto` → resolve 时由 `lang` 推导。

#### 4.5.5 小练习与答案

**练习 1**：`Smart<T>` 的两个变体分别表示什么？它与 u2-l1 的 `AutoValue` 是什么关系？

> **答案**：`Auto` 表示「自动/让引擎推断」，`Custom(T)` 表示「用户显式指定」。`AutoValue` 是 `Value` 层面的 `auto`（`Value::Auto`），`Smart<T>` 是具体类型层面「这个字段可能是 auto」的表达；`FromValue for AutoValue` 会把 `Value::Auto` 还原成 `AutoValue`，而 `Smart<T>` 的 `FromValue`（[auto.rs:256-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L256-L258)）把 `Value::Auto` 还原成 `Smart::Auto`。

**练习 2**：`TextDir::resolve` 在 `Smart::Auto` 时做了什么？为什么这属于 `Resolve` 而不是 `Fold`？

> **答案**：调 `styles.get(TextElem::lang).dir()`，由语言推导书写方向。它属于 `Resolve` 因为输出类型变了（`TextDir` → `Dir`）且需要样式链（查 `lang`）；`Fold` 是同类型进出且不吃样式链。

**练习 3**：如果一个字段同时实现了 `Fold` 和 `Resolve`（如 `TextSize`），查询时先发生哪个？

> **答案**：先 fold 后 resolve。`StyleChain::resolve` = `get_cloned(field).resolve(self)`，而 `get_cloned` 内部先做 fold（若 `E::FOLD` 为 `Some`），拿到合并后的值，再对其调 `resolve`。

---

## 5. 综合实践

把本讲四个核心概念（`Styles`/`StyleChain`/`Fold`/`Resolve`）串起来，完成下面这个**源码阅读 + 手算**任务。

**场景**：下面这段 Typst 文档：

```typst
#set text(size: 12pt)            // 全局基础字号
#set text(lang: "ar")            // 全局语言阿拉伯语
#box[
  #set text(size: 2em)           // box 内放大
  مرحبا #text(1.5em)[!][big]     // 再放大
]
```

**任务**：

1. **画样式栈**：写出「big」这个词的 `StyleChain` 有几层、每层贡献了哪些 `Property`（用 `(elem, id, value)` 表示）。
2. **fold 字号**：`TextElem::size` 是 `#[fold]` 的。手算「big」的字号经过折叠后的 `TextSize`（即一个 `Length{em, abs}`）。提示：链上从内到外是 `1.5em` → `2em` → `12pt`，按 `TextSize::fold` 逐层折叠。
   > 参考答案：先 fold `1.5em`（内）和 `2em`：`em = 1.5*2 = 3, abs = 1.5*0 + 0 = 0` → `3em`；再 fold `3em`（内）和 `12pt`：`em = 3*0 = 0, abs = 3*12 + 0 = 36` → `36pt`。最后 fold 默认 `11pt`：`em=0, abs = 0*11 + 36 = 36` → `36pt`。
3. **resolve 字号**：确认 fold 后 `em=0`，所以 `Em::resolve` 触底，最终绝对字号是 `36pt`。
4. **resolve 方向**：`TextElem::dir` 是 `Smart` 字段。若用户没显式设 `dir`，则用 `#[default]`（通常是 `auto`）；`TextDir::resolve` 看到 `Smart::Auto` 会调 `styles.get(TextElem::lang).dir()`，阿拉伯语 → `Dir::RTL`。请到 [auto.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs) 与 `Lang::dir` 确认这条推导链。
5. **回答**：为什么 `1.5em` 不能用「覆盖」语义？如果改成覆盖，「big」的字号会是多少（错误结果）？

**验收标准**：你能完整说出「一条 `set` 规则 → `Property` → 进 `Styles` → 被 `StyleChain` 串联 → fold 合并 → resolve 成绝对值」的全链路，并解释每一步为什么必要。

> 待本地验证：手算结果可用 `cargo run` 编译该文档，临时在 `TextSize::resolve` 加日志打印 `self.0` 和返回值来核对（仅学习用，勿提交）。

---

## 6. 本讲小结

- **`Styles` 是有序的样式条目列表**（`EcoVec<LazyHash<Style>>`），每条 `Style` 是 `Property`（set 规则）、`Recipe`（show 规则）或 `Revocation` 三者之一。一条 `set text(size: 20pt)` 经 `Styles::set` 变成一个 `Property { elem, id, value: Block(..) }` 被 push 进列表。
- **`StyleChain` 是零分配的链表视图**（`head` 切片 + `tail` 引用，`Copy`），把多层 `Styles` 串联，按「最内层 → 最外层」查询；其 `PartialEq` 只比指针，支撑 comemo 增量记忆化。
- **取值分两条路**：覆盖式（`get_unfolded`，取链上第一个匹配，最内层优先）与折叠式（`get_folded`，`reduce(fold)` 收集全部）。走哪条由编译期常量 `E::FOLD` 决定，`#[fold]` 标注经 `with_fold` 把它设为 `Some`。
- **`Fold` 是同类型进出、不吃样式链的合并**（`fn fold(self, outer) -> Self`，须满足结合律）；`TextSize::fold` 把两个 `Length` 当线性函数相乘，这正是相对字号「层层放大」的数学表达。
- **`Resolve` 是异类型、吃整条 `StyleChain` 的解析**（`fn resolve(self, styles) -> Output`），把相对值变成绝对值；`TextSize::resolve → Length::resolve → Em::resolve` 形成调用链，`Em::resolve` 在 `em` 为 0 时触底，避免无限递归。
- **`Smart<T>`（`Auto`/`Custom`）表达「智能默认」**：`auto` 在 `get_cloned` 阶段原样保留，到 `resolve`/排版时才由消费方推导（如 `TextDir::resolve` 由 `lang` 推方向）。

---

## 7. 下一步学习建议

本讲只讲了「样式如何存、如何查」，还没讲两条同样重要的东西：

1. **`set` 规则如何从 Typst 语法生成 `Property`、`show` 规则如何生成 `Recipe` 并被匹配执行**——这是 u4-l2「属性、set 规则、Selector 与 Recipe」的主题。建议接着读 [styles.rs 的 `Recipe`/`Transformation`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L445-L549) 与 [selector.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs)，理解 `Selector::Elem + where` 如何过滤、`NativeRuleMap` 如何衔接原生 show 规则。
2. **`Styles` 如何在元素层级间流动、`StyledElem` 如何包裹 content+styles**——这部分在 u3-l1（`Content` 的 `+` 拼接与 `StyledElem` 合并）已有铺垫，建议回头对照，理解「为什么 `StyleChain` 要设计成不可变链表视图而非合并」。
3. **性能侧**：`LazyHash<Style>`、`StyleChain` 的指针相等、comemo `tracked` 如何让「样式没变就跳过重排」——留到 u12-l2「性能与并发」统一讲。

如果你想做一个小练习巩固本讲，可以在 fork 里给某个 `#[ghost]` 字段（如 `TextElem::tracking`）临时加一个 `#[fold]` 标注，观察它从覆盖式变成折叠式后行为如何变化（注意需要该字段类型实现 `Fold`）。
