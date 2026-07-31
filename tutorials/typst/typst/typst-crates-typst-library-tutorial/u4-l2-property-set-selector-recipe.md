# 属性、set 规则、Selector 与 Recipe

## 1. 本讲目标

学完本讲，你应当能够：

- 说清一条 `set` 规则（如 `set text(red)`）在编译期是如何被拆解、存储成一条 `Property`，并最终汇入 `Styles` 列表的。
- 读懂 `Selector` 枚举的每个变体，特别是 `heading.where(level: 1)` 这类带字段过滤的选择器在源码里是如何被构造与匹配的。
- 理解 `Recipe` 作为 show 规则的运行时载体，以及它如何与 `Content`、`Transformation` 衔接。
- 解释 `NativeRuleMap`（内置 show 规则表）从哪里来、为什么它要通过 `routines.rules` 函数指针装配，以及它和用户写的 show 规则如何分工。

本讲承接 u4-l1（`Styles`/`StyleChain`/`Fold`/`Resolve`）。u4-l1 讲的是「样式存进去之后怎么查」，本讲讲的是「样式是怎么进去的」——set 规则产 `Property`、show 规则产 `Recipe`，二者共同构成 `Style` 的两个分支。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**set 规则与 show 规则的区别。** 在 Typst 源码里写 `set text(size: 12pt)` 是在「调整某个元素的某个参数」，它不创造内容，只改变后续文本的默认行为；写 `show heading: it => [★ #it.body]` 是在「拦截某种元素、把它替换成别的内容」。二者在编译器内部是两条完全不同的路径：前者产生「属性（Property）」，后者产生「配方（Recipe）」。

**类型擦除的代价。** Typst 有几十种元素，每种元素有各自不同的字段。但样式系统需要用一个**统一**的类型来存储「任意元素的任意字段的新值」，否则 `Styles` 就得为每种元素写一份代码。解决办法是类型擦除：`Property` 用一个 `Box<dyn ...>`（源码里叫 `Block`）把任意类型的值装进同一个盒子，再记录「这是哪个元素（`Element`）的第几个字段（`id: u8`）」。读取时凭 `(elem, id)` 把值取回。u3-l2 讲过的 `Element` 句柄与 u3-l3 讲过的字段 ID 在这里派上用场。

**crate 边界。** 「把 `set`/`show` 关键字解析成 `Styles`」「在排版时把 `Recipe` 与元素做匹配」这些**行为**都在 `typst-eval`/`typst-realize` 等行为 crate 里（见 u1-l1、u5-l4）。`typst-library` 只负责**定义** `Property`、`Recipe`、`Selector`、`NativeRuleMap` 这些数据类型，并提供构造与查询的方法。所以本讲你会看到很多「数据如何构造」「数据如何匹配」，但不会看到「`set` 关键字的求值循环」——那在别的 crate。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/foundations/styles.rs` | `Styles`、`Style`、`Property`、`Recipe`、`Transformation`、`NativeRuleMap`、`NativeShowRule` 全部在此。是本讲最核心的文件。 |
| `src/foundations/selector.rs` | `Selector` 枚举、`select_where!` 宏、`LocatableSelector`、`ShowableSelector`。 |
| `src/foundations/content/mod.rs` | `Content` 上与样式相关的方法：`get`、`styled`、`styled_with_map`、`styled_with_recipe`、`set`。是样式挂到内容上的入口。 |
| `src/foundations/content/element.rs` | `Element::set`、`Element::select`、`Element::where_`，以及 `Set`/`Construct` trait。 |
| `src/foundations/content/field.rs` | `Field` 零大小访问器、`Field::set`、`SettableProperty` trait。 |
| `src/foundations/func.rs` | `Func::where_`——用户写的 `heading.where(...)` 最终走到的 Rust 函数。 |
| `src/lib.rs` | `Library.rules` 字段，以及 `build()` 里 `rules: (self.routines.rules)()` 的装配。 |
| `src/routines.rs` | `Routines` 函数指针表中的 `rules` 例程声明。 |

---

## 4. 核心概念与源码讲解

### 4.1 Property：set 规则如何变成一条样式

#### 4.1.1 概念说明

`Property` 是 `set` 规则在运行时的「单条记录」。当编译器遇到 `set text(size: 12pt)` 时，它需要表达「把 `TextElem` 的 `size` 字段设为 `12pt`」这件事。`Property` 就是这个三元组的容器：哪个元素、哪个字段、新值是什么。

这里有两个设计难点。第一，**值的类型是任意的**：`size` 是长度、`fill` 是颜色、`lang` 是枚举……必须用一个类型擦除的盒子统一存放。第二，**字段是「第几个」而非「叫什么」**：用 `u8` 编号比用字符串快，而且能在类型擦除后仍然精确定位字段。这两个难点共同决定了 `Property` 的结构。

#### 4.1.2 核心流程

一条 set 规则从用户源码到 `Styles` 列表，经历以下几步（行为部分在别的 crate，本讲标注其归属）：

1. **求值层（typst-eval）**：解析 `set text(size: 12pt)`，拿到元素函数 `text` 与命名参数 `{size: 12pt}`。
2. **调用 `Element::set`**（本 crate）：把参数喂给该元素的 `Set` trait 实现，它负责把每个命名参数翻译成一条 `Property`，聚合成一个 `Styles`。
3. **`SettableProperty` / `Field`**（本 crate）：每个可设置字段由 `#[elem]` 宏生成一个零大小的 `Field<E, I>` 访问器（`I` 就是字段编号常量），`Field::set(value)` 产出一条 `Property`。
4. **`Property::new`**（本 crate）：构造 `{ elem, id, value: Block, span, liftable, outside }`。
5. **`Styles::set` / `Styles::push`**（本 crate）：把这条 `Property` 包成 `Style::Property`，再裹一层 `LazyHash`，追加进 `Styles` 的 `EcoVec`。

用一个伪代码概括：

```text
set text(size: 12pt)
  → Element::set(text, args)
      → <TextElem as Set>::set(args)
          → for each (field_id, value) in args.named:
                styles.set(TextElem::size, value)
                    → Property::new(TextElem::size, value)
                    → styles.push(Style::Property(property))
  → 返回 Styles
```

#### 4.1.3 源码精读

先看 `Style` 枚举——它就是 `Styles` 列表里每个条目的类型，含三个分支，本节只看 `Property`：

[styles.rs:L216-L227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L216-L227) —— `Style` 分 `Property`（来自 set 规则或构造器）、`Recipe`（来自 show 规则）、`Revocation`（禁用某条 recipe）三种。这三种条目混在同一个 `Styles` 列表里，按相对顺序排列。

接着是 `Property` 本体：

[styles.rs:L316-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L316-L331) —— `Property` 的六个字段。`elem` 是目标元素句柄，`id` 是字段编号，`value` 是被擦除类型的值（`Block`），`span` 记录这条 set 规则在源码里的位置（报错用），`liftable`/`outside` 是两个布尔标记（见下）。

`Property::new` 展示了字段编号 `I` 如何被编译期常量固定下来：

[styles.rs:L333-L348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L333-L348) —— `new<E, const I: u8>(_: Field<E, I>, value)` 的签名里，`I` 是一个 const generic。也就是说「第几个字段」不是运行时算出来的，而是 `#[elem]` 宏在编译期为每个字段生成了带不同 `I` 的方法，调用 `TextElem::size` 时 `I` 就已经被固定。`elem: E::ELEM`、`id: I` 直接填入。

值的类型擦除靠 `Block`：

[styles.rs:L379-L400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L379-L400) —— `Block(Box<dyn Blockable>)` 把任意 `T: Debug + Clone + Hash + Send + Sync + 'static` 装箱。`downcast::<T>(func, id)` 在读取时把 `dyn Any` 还原回具体类型 `&T`，若类型对不上就 panic（`block_wrong_type`）——这是一种「调用方必须保证类型正确」的受检 unsafe 边界。

为什么用 `Box` 而不是直接存值？注释说得很直白：这些值要么本来就在 `Arc` 里（已经在堆上），要么足够小，克隆很廉价。

把 `Property` 装进 `Styles` 的入口有两个：

[styles.rs:L53-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L53-L64) —— `Styles::set` 接受一个 `Field<E, I>` 和值，内部调 `Property::new` 再 `push`；`Styles::push` 则接受任意 `impl Into<Style>`，统一包一层 `LazyHash::new`。`LazyHash`（u2-l2 讲过）保证哈希只算一次、之后克隆廉价。

`Property::is` 是查询时用到的判等：

[styles.rs:L350-L358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L350-L358) —— `is(elem, id)` 判断「这条属性是不是某元素某字段」，u4-l1 的 `StyleChain::properties` 正是用它过滤出同 `(elem, id)` 的所有条目做 fold/覆盖查询。

再看「求值层调进本 crate」的那个入口——`Element::set`：

[content/element.rs:L74-L79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L74-L79) —— `Element::set` 调用 vtable 上的 `set` 函数指针（即该元素的 `Set` 实现），拿到 `Styles`，再 `args.finish()` 校验没有多余参数。

[content/element.rs:L257-L263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L257-L263) —— `Set` trait 只有一个方法：把参数解析成 `Styles`。注释点明「传给 `construct` 的参数是 set 规则执行后剩下的」——set 规则会先「吃掉」它关心的命名参数。

`Set` 的具体实现由 `#[elem]` 宏生成（u3-l3），内部就是一连串 `args.named(...)` 加 `styles.set(...)`。字段访问器 `Field` 与 `SettableProperty` 是这套机制的类型层支柱：

[content/field.rs:L14-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L14-L44) —— `Field<E, I>` 是个只装 `PhantomData` 的零大小类型，纯粹用来在类型系统层面指明「我要 E 的第 I 个字段」，并让 Rust 推断出返回类型。`Field::set` 直接产出一条 `Property`。

[content/field.rs:L330-L351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L330-L351) —— `SettableProperty` trait 暴露 `Type`、`FIELD`、`FOLD`（折叠函数，u4-l1 讲过）与 `default`。`get_cloned` 在 u4-l1 里正是依据 `E::FOLD` 决定走覆盖还是折叠——而 `FOLD` 的来源就是这里的字段元数据。

最后看两个布尔标记 `liftable` 与 `outside` 的语义。它们决定了样式能否被「抬升」到页面根部（页眉页脚等用户内容之外的区域）：

[styles.rs:L104-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L104-L165) —— `Styles::root` 的注释详细解释：只有 `outside && (initial || liftable)` 的样式才会被保留为「主干样式」。`outside` 表示该样式产生于任何 show 规则之外（直接在文档体里写的 set 规则）；`liftable` 表示它来自 set 规则（而非直接构造器调用），因此可以「提前生效」。这是实现 `set text(red)` 能让页脚也变红这类细节的关键。

> 注：把 `set` 关键字解析成对 `Element::set` 的调用，这一步发生在 `typst-eval`；本讲只到「`Element::set` 这个入口」为止。

#### 4.1.4 代码实践

**实践目标**：跟踪一条 set 规则从「字段访问器」到「`Styles` 列表条目」的完整路径。

**操作步骤**：

1. 打开 [content/field.rs:L37-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L37-L43)，确认 `Field::set` 返回 `Property::new(self, value)`。
2. 跟进到 [styles.rs:L333-L348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L333-L348)，看 `Property::new` 如何把 `elem/id/value/span` 组装，初始 `liftable: false, outside: false`。
3. 再看 [styles.rs:L53-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L53-L64)，确认 `Styles::set` → `Property::new` → `self.push(...)`，`push` 把它包成 `Style::Property` 再裹 `LazyHash`。
4. 想象 `set text(size: 12pt)`：在脑中把 `TextElem::size` 代入 `Field`，`12pt` 代入 `value`，`I` 代入 `size` 字段的编号。

**需要观察的现象**：`Property` 在创建时 `liftable`/`outside` 都是 `false`；只有经过 `Styles::liftable()`/`Styles::outside()`（[styles.rs:L93-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L93-L112)）这两个后处理才会被置真。

**预期结果**：你能画出 `Field::set → Property::new → Styles::push → EcoVec<LazyHash<Style>>` 的调用链，并说清每一步加了什么信息（类型擦除、字段编号、哈希缓存）。

#### 4.1.5 小练习与答案

**练习 1**：`Property` 为什么用 `u8` 的 `id` 而不是字段名字符串？

**参考答案**：编号比字符串比较快、占用小；更关键的是类型擦除后已经丢失了字段名信息，而字段编号是 `#[elem]` 宏在编译期为每个字段生成的 const generic 常量，可以在不依赖字符串的情况下精确定位字段。字段名只在调试输出和错误信息里需要（通过 `Element::field_name(id)` 反查）。

**练习 2**：`Block` 的 `dyn_hash` 实现里，为什么除了哈希值本身还要哈希 `TypeId::of::<Self>()`？

**参考答案**：见 [styles.rs:L426-L437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L426-L437) 的注释——两个类型不同但数据恰好相等的值（例如 `i64` 的 `1` 和 `f64` 的 `1.0`）在逻辑上应当是不同的样式值，因此要把类型也纳入哈希，避免哈希碰撞导致错误的样式命中。

---

### 4.2 Selector：where 过滤机制

#### 4.2.1 概念说明

`Selector`（选择器）回答一个问题：「这个元素算不算命中？」它用在两个地方：show 规则（`show heading.where(level: 1): ...`，决定拦截谁）和内省查询（`query(heading.where(level: 1))`，决定返回谁）。

最常见的选择器是「某类元素」，写成 `Selector::Elem(element, None)`——命中所有该类元素。更强的形式是「某类元素且某些字段等于指定值」，即 `Selector::Elem(element, Some(fields))`，对应 Typst 里的 `.where(...)` 语法。除了元素选择器，`Selector` 还能匹配标签（`<my-label>`）、正则文本、位置，以及用 `or`/`and`/`before`/`after`/`within` 组合出更复杂的表达式。

`where` 过滤的核心，是把「字段名 → 值」在构造时翻译成「字段编号 → 值」，这样匹配时就只需比较编号与值，不必再做字符串查表。

#### 4.2.2 核心流程

以 `heading.where(level: 1)` 为例，从用户代码到 `Selector::Elem` 的流程：

1. **求值层**：解析 `heading.where(level: 1)`，识别出 `where` 是元素函数上的方法，收集命名参数 `{level: 1}`。
2. **`Func::where_`**（本 crate）：把命名参数的「键」从字段名翻译成字段编号，丢弃位置参数（`where` 只接受命名参数）。
3. **`Element::where_`**（本 crate）：用 `(element, Some(fields))` 构造 `Selector::Elem`。
4. **匹配时 `Selector::matches`**（本 crate）：对候选元素，先比 `elem()` 是否相等，再对每个 `(id, value)` 调 `Content::get(id, styles)` 取出元素该字段的值，与目标值比相等。

匹配判定的形式化表达：设有过滤字段集合 \(F = \{(id_i, v_i)\}\)，候选元素为 \(t\)，则

\[
\mathrm{match}(t) \;=\; \bigl(t.\mathrm{elem} = E\bigr) \;\land\; \bigwedge_{(id_i, v_i)\in F} \bigl(\mathrm{get}(t, id_i) = v_i\bigr)
\]

即「元素类型相同」且「所有过滤字段都相等」。

#### 4.2.3 源码精读

`Selector` 枚举列出全部变体：

[selector.rs:L76-L104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L76-L104) —— 重点看 `Elem(Element, Option<SmallVec<[(u8, Value); 1]>>)`：第二个分量是「字段编号→值」的小向量（`SmallVec` 容量 1，因为大多数 `.where(...)` 只过滤一个字段，如 `level`）。`None` 表示不过滤字段。其余变体：`Label`/`Regex`/`Location`/`Can` 分别按标签、正则、位置、能力匹配；`Or`/`And` 是逻辑组合；`Before`/`After`/`Within` 是带文档位置关系的组合（后三者只在 query 中支持，show 规则不支持）。

匹配的核心是 `matches`：

[selector.rs:L132-L155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L132-L155) —— `Elem` 分支：`target.elem() == *element` 先比类型；再用 `dict.iter().flat_map(|d| d.iter()).all(...)` 遍历每个 `(id, value)`，调 `target.get(*id, styles)` 取值并比相等。注意 `Regex`/`Before`/`After`/`Within` 在这里统一返回 `false`——它们需要文档位置信息，不能在「单个元素 vs 选择器」的纯函数里判定，留给 query/realization 阶段处理。

`target.get(id, styles)` 的实现：

[content/mod.rs:L173-L192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L173-L192) —— `Content::get` 按 ID 取字段。注意第 178 行的特判：`id == 255` 是保留给 `label` 的字段号（u3-l1 讲过 label 是保留字段 ID 255），这样 `where` 也能过滤 `<label>`。`Some(styles)` 时走 `get_with_styles`——意味着 where 过滤会**穿透样式链**取到折叠/解析后的字段值（这正是 `heading.where(level: 1)` 能匹配到由 set 规则间接影响层级的原因）。

现在看 `.where(...)` 的构造链。用户层入口是 `Func::where_`：

[func.rs:L419-L449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L419-L449) —— 关键三步：(1) `args.to_named()` 抽出所有命名参数为 `Dict`；(2) `args.items.retain(|arg| arg.name.is_none())` 把命名参数从 `args` 里删掉（`where` 不消费位置参数，但宏签名声明了 `#[variadic] fields`，所以要手动清理）；(3) 对每个键调 `element.field_id(&key)` 把名字翻译成编号，找不到字段名就报错 `"element ... does not have field ..."`。

`args.to_named()` 的实现印证了「按 `name` 是否为 `None` 区分位置/命名参数」（u3-l4 讲过 Args 的扁平结构）：

[args.rs:L384-L390](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L384-L390) —— 只保留 `item.name` 非空的条目。

字段名→编号翻译完成后，交给 `Element::where_`：

[content/element.rs:L115-L119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L115-L119) —— 直接 `Selector::Elem(self, Some(fields))`。对照它的兄弟方法 `Element::select`（[element.rs:L111-L113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L111-L113)，`Selector::Elem(self, None)`），就能看出「`.select()` 不过滤字段、`.where(...)` 过滤字段」的区别。

在 Rust 代码里构造 where 选择器时，并不需要走 `Func::where_` 那套参数解析，而是直接用 `select_where!` 宏：

[selector.rs:L17-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L17-L38) —— `select_where!(CounterUpdateElem, key => self.0.clone())` 会被展开成：用 `<$ty>::$field.index()` 在编译期拿到字段编号，`IntoValue::into_value($value)` 把值转成 `Value`，push 进 `SmallVec`，最后 `Selector::Elem(<$ty>::ELEM, Some(fields))`。标准库内部多处用它（如 counter、state、figure、outline 的内省），见 [counter.rs:243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L243)、[figure.rs:401](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L401)。

最后，不同上下文对选择器有不同约束，由两个包装类型把关：

[selector.rs:L470-L545](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L470-L545) —— `ShowableSelector`（show 规则用）在 `validate` 里拒绝 `Location`/`Can`/`Before`/`After`/`Within` 以及嵌套的 `Regex`；`LocatableSelector`（query 用，[selector.rs:L366-L462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L366-L462)）则要求元素具备 `locatable` 能力。这解释了为什么 `show selector(...).before(..)` 会报错而 `query` 能用。

#### 4.2.4 代码实践

**实践目标**：写出 `heading.where(level: 1)` 的等价伪代码，并验证它在标准库内部确实被这样构造。

**操作步骤**：

1. 读 [selector.rs:L76-L104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L76-L104) 确认 `Selector::Elem` 的第二分量类型是 `Option<SmallVec<[(u8, Value); 1]>>`。
2. 读 [func.rs:L419-L449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L419-L449)，跟随 `to_named() → field_id("level") → element.where_(fields)`。
3. 读 [selector.rs:L132-L155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L132-L155)，对照 `matches` 的 `Elem` 分支写出判定逻辑。

**`heading.where(level: 1)` 的等价伪代码**：

```text
// 构造阶段（Func::where_ + Element::where_）
let level_id = HeadingElem::level.index();   // 字段名 "level" → 编号 id（编译期常量）
let fields = smallvec![(level_id, Value::Int(1))];
let selector = Selector::Elem(HeadingElem::ELEM, Some(fields));

// 匹配阶段（Selector::matches 的 Elem 分支）
fn matches(target: &Content) -> bool {
    target.elem() == HeadingElem::ELEM
        && target.get(level_id, None).as_ref().ok() == Some(&Value::Int(1))
}
```

**需要观察的现象**：构造时键是**编号**而非字符串；匹配时只比较 `elem()` 相等与各字段值相等，不涉及文档位置——这正是 `Regex`/`Before`/`After`/`Within` 在 `matches` 里返回 `false` 的原因。

**预期结果**：你能解释为何 `heading.where(level: 1)` 能在 show 规则里用作拦截条件（纯字段比较，无需内省），而 `heading.before(...)` 只能用于 query（需要文档位置）。

> 待本地验证：若你想观察运行时行为，可在 fork 中给 `Selector::matches` 的 `Elem` 分支加一行 `eprintln!("match? {} {:?}", target.elem().name(), dict);`，编译后用一个含多个层级标题的 `.typ` 文件触发 show 规则，观察打印输出。本讲不假定你已运行此命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Selector::Elem` 的字段向量用 `SmallVec<[(u8, Value); 1]>` 而非 `Vec`？

**参考答案**：绝大多数 `.where(...)` 调用只过滤一个字段（如 `level`、`kind`、`key`）。`SmallVec` 容量 1 时，单字段情况完全在栈上、零堆分配；多字段时才退化到堆。这是样式/选择器热路径上的常见优化。

**练习 2**：`heading.where(level: 1)` 与 `heading`（不带 where）在 `Selector` 层面的区别是什么？匹配开销差多少？

**参考答案**：前者是 `Selector::Elem(HeadingElem::ELEM, Some([(level_id, 1)]))`，后者是 `Selector::Elem(HeadingElem::ELEM, None)`。匹配时前者多走一遍字段过滤（对每个 `(id, value)` 调 `Content::get` 并比相等），后者 `dict` 为 `None` 直接跳过 `all(...)`。所以不带 where 的选择器匹配更廉价。

---

### 4.3 Recipe：show 规则的载体

#### 4.3.1 概念说明

`Recipe` 是 show 规则的运行时记录。一条 `show heading.where(level: 1): it => [★ #it.body]` 在编译期会被翻译成一个 `Recipe`，它装着两样东西：一个**选择器**（命中谁）和一个**变换**（命中后怎么办）。

变换有三种（`Transformation`）：直接替换成固定内容（`show strong: [★]`）、调用一个函数（`show heading: it => ...`）、给命中内容附加一组样式（这是 show-set 规则 `show heading: set text(red)` 的内部表示）。

show 规则与 set 规则的关键区别在于「惰性」：set 规则产出的 `Property` 是一个静态值，等着被查询；show 规则产出的 `Recipe` 是一个**待执行的动作**，必须在排版阶段「遇到匹配的元素、把它抓来喂给变换函数」才会真正生效。这个「抓来喂」的动作就是 `Recipe::apply`。

#### 4.3.2 核心流程

show 规则从用户代码到生效，经历：

1. **求值层**：解析 `show <selector>: <transform>`，产出 `Recipe { selector, transform, span, outside }`。
2. **挂载到内容**（本 crate）：把 `Recipe` 包成 `Style::Recipe`，随 `Styles` 挂到对应的内容子树（`Content::styled` / `Content::styled_with_map`）。
3. **realization 层（typst-realize，行为 crate）**：排版遇到一个元素时，沿 `StyleChain` 自内向外遍历 `recipes()`，对每条 recipe 用其 selector 做匹配，命中就 `Recipe::apply`。
4. **`Recipe::apply`**（本 crate）：按 `Transformation` 分支执行——`Content` 直接返回替换内容；`Func` 调用函数并把返回值 `display()` 成 `Content`；`Style` 给内容附加样式。

一个特例：**没有选择器的 show 规则** `show: rest => ...`（捕获剩余内容）会被**立即（eagerly）应用**，而不是挂载等待——见 `Content::styled_with_recipe`。

#### 4.3.3 源码精读

`Recipe` 结构：

[styles.rs:L445-L461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L445-L461) —— `selector: Option<Selector>`（`None` 即无选择器的捕获式 show 规则）、`transform: Transformation`、`span`、`outside`。`Recipe::new`（[styles.rs:L464-L471](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L464-L471)）是构造入口。

`Transformation` 三分支：

[styles.rs:L530-L555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L530-L555) —— `Content`（替换）、`Func`（函数变换）、`Style(Styles)`（show-set）。底部的 `cast!` 说明从 Typst 值构造时，`Content` 和 `Func` 都能直接作为变换（show-set 的 `Style` 分支由求值层在 `show X: set ...` 时合成，不直接从值 cast）。

`Recipe::apply` 是 show 规则真正「执行」的地方：

[styles.rs:L488-L511](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L488-L511) —— 三个分支分别处理：`Content` 直接克隆替换内容；`Func` 调用 `func.call(engine, context, [content])`（把被命中的元素作为参数 `it` 传入），结果 `.display()` 成 `Content`，并（若有 selector）附加一个 `Show` tracepoint 方便报错定位；`Style` 调 `content.styled_with_map(styles)` 给命中内容附加样式（这就是 show-set）。最后还有一段 span 修补：若结果 content 的 span 是 detached 的，就用 recipe 的 span 标注，保证错误能指回 show 规则。

show 规则如何挂到内容上——`Content::styled_with_recipe`：

[content/mod.rs:L336-L348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L336-L348) —— 这就是那条「特例」的分叉：若 `recipe.selector().is_none()`（捕获式 show），**立即** `recipe.apply`；否则 `self.styled(recipe)` 把它作为 `Style::Recipe` 挂上去，等 realization 阶段匹配。

把 `Recipe` 挂上去的通用机制是 `Content::styled` / `Content::styled_with_map`：

[content/mod.rs:L364-L386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L364-L386) —— 若内容已是 `StyledElem`，就把新样式合并进它的 `styles`（避免层层嵌套 `StyledElem`，与 u3-l1 讲的 sequence 扁平化同理）；否则用 `StyledElem::new(self, styles)` 包一层。这是 `Style`（含 `Property` 与 `Recipe`）进入内容树的统一入口——set 规则产的 `Property` 和 show 规则产的 `Recipe` 走的是**同一条挂载路径**，只是被 realization 分别处理。

查询阶段如何拿到所有 recipe？`StyleChain::recipes`：

[styles.rs:L696-L699](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L696-L699) —— 从 `entries()` 里筛出 `Style::Recipe`，供 realization 层遍历匹配。`Style::element()`（[styles.rs:L256-L266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L256-L266)）对 `Recipe` 会尝试从其 `Selector::Elem` 里取出元素类型，帮助 realization 快速跳过「明显不相关」的 recipe。

最后看 `Revocation`——禁用某条 recipe：

[styles.rs:L221-L227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L221-L227) —— 注释说明它目前只用于 regex recipe。`RecipeIndex`（[styles.rs:L526-L528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L526-L528)）用「从链顶往下数第几条」来定位要禁用的 recipe。

#### 4.3.4 代码实践

**实践目标**：把三种 show 规则语法对应到 `Transformation` 的三个分支。

**操作步骤**：

1. 准备三行 Typst 代码（示例代码，非项目原有）：
   ```typst
   #show strong: [★]               // Transformation::Content
   #show heading: it => [★ #it.body] // Transformation::Func
   #show heading: set text(red)     // Transformation::Style(Styles)
   ```
2. 对照 [styles.rs:L530-L555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L530-L555) 确认三者分别落到哪个分支。
3. 再读 [styles.rs:L488-L511](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L488-L511)，确认 `Func` 分支会把「被命中的 heading 元素」作为唯一参数传给 `it => ...`。

**需要观察的现象**：show-set（第三行）本质上是「命中的元素 + 一组 `Property`」，它复用了 4.1 节的 `Property` 机制——show 与 set 在数据层并非完全割裂，show-set 就是二者的桥梁。

**预期结果**：你能说清 `Recipe::apply` 的 `Func` 分支里，`content`（被命中的元素）是如何作为 `it` 进入用户函数的。

#### 4.3.5 小练习与答案

**练习 1**：`show strong: [★]` 与 `show strong: it => [★]` 产生的 `Transformation` 有何不同？运行结果一样吗？

**参考答案**：前者是 `Transformation::Content([★])`，`apply` 时直接克隆替换内容、不调用任何函数；后者是 `Transformation::Func(<闭包>)`，`apply` 时会调用闭包（尽管闭包忽略了 `it`）。运行结果一样，但前者省一次函数调用。此外，前者不会附加 `Show` tracepoint（因为 trace 只在 `selector.is_some()` 且走 `Func` 分支时加）。

**练习 2**：为什么 `show: rest => ...`（无选择器）要被「立即应用」而不是挂载？

**参考答案**：见 `Content::styled_with_recipe` 的注释——无选择器的 show 规则语义是「捕获当前作用域剩余的全部内容并变换」，它没有可等待匹配的「目标元素类型」，挂载没有意义；只有立即把剩余内容抓来喂给变换函数才能实现其语义。源码称其为 [eagerly applied][Content::styled_with_recipe]。

---

### 4.4 NativeRuleMap：内置 show 规则的来源

#### 4.4.1 概念说明

用户写的 show 规则（`Recipe`）是在求值期动态产生的、挂在 `StyleChain` 上的。但 Typst 还有一类 **内置（native）show 规则**：每个元素「默认长什么样」就是一条内置 show 规则——比如 `rect` 默认画成一个矩形框、`heading` 默认渲染成粗体大字号文本。这些规则用 Rust 写死，不经过 Typst 脚本。

这些内置规则集中存放在 `NativeRuleMap` 里，它是「`(元素, 目标格式) → 一段 Rust show 函数`」的映射表。`目标格式`（`Target`）区分 paged（分页 PDF）、html、bundle 等不同输出后端，因为同一个元素在不同后端的「默认长相」可能不同。

为什么 `NativeRuleMap` 要通过 `Routines` 函数指针装配，而不是直接在 `typst-library` 里建表？因为这正是 u1-l1/u5-l4 讲的 **crate 拆分**：`typst-library` 不依赖 `typst-layout`/`typst-html`（它们是行为 crate，依赖关系反过来），但内置 show 规则的实现（如何把 `rect` 画成框）恰恰住在那些行为 crate 里。于是 `typst-library` 只定义 `NativeRuleMap` 这个「表结构」和建表接口，真正的「填表」由主 `typst` crate 通过 `routines.rules` 完成。

#### 4.4.2 核心流程

`Library.rules`（内置规则表）的装配流程：

1. **`LibraryBuilder::build`**（本 crate）：执行 `rules: (self.routines.rules)()`，调函数指针。
2. **`routines.rules`**（主 `typst` crate）：`let mut rules = NativeRuleMap::new(); typst_layout::register(&mut rules); typst_html::register(&mut rules); rules`——先建空表（含若干特殊内置规则），再让 layout/html crate 各自往里 `register`。
3. **`NativeRuleMap::new`**（本 crate）：创建空 `IndexMap`，预注册若干「全后端通用」的特殊规则。
4. **`NativeRuleMap::register`**（本 crate）：行为 crate 调用它，把「某元素在某 Target 下的 show 函数」插入表里。
5. **查询 `NativeRuleMap::get`**（本 crate）：realization 阶段用 `(target, content)` 查出适用的 `NativeShowRule`，再 `apply`。

#### 4.4.3 源码精读

先看 `Library.rules` 字段及其装配：

[lib.rs:L166-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L166-L183) —— `Library` 第七... 其实是含 `rules: NativeRuleMap` 字段（注释「The built-in show rules」）。

[lib.rs:L220-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L220-L234) —— `build()` 里 `rules: (self.routines.rules)()`。这一行就是「本 crate 调用由别处注入的函数指针」——动态链接的典型写法（u5-l4）。

`routines.rules` 的声明（本 crate 只声明签名，不提供实现）：

[routines.rs:L50-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L50-L53) —— `fn rules() -> NativeRuleMap`。`routines!` 宏（[routines.rs:L20-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L20-L48)）把它变成 `Routines` 结构体的一个 `pub rules: fn() -> NativeRuleMap` 字段。

真正的实现在主 `typst` crate：

[crates/typst/src/lib.rs:L312-L317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L312-L317) —— `rules: || { let mut rules = NativeRuleMap::new(); typst_layout::register(&mut rules); typst_html::register(&mut rules); rules }`。注意它在 `LazyLock<Routines>` 里，是整个进程共享的静态函数指针表。这就是「为什么 `typst-library` 编译时不依赖 `typst-layout`/`typst-html`，运行时却能调到它们的规则」——依赖通过函数指针在运行期注入，编译期无环。

回到本 crate，看 `NativeRuleMap` 本体：

[styles.rs:L985-L996](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L985-L996) —— `NativeRuleMap { rules: IndexMap<(Element, Target), NativeShowRule> }`，键是「元素 + 目标格式」。`ShowFn<T>` 是带类型的 show 函数签名：吃 `&Packed<T>`、`engine`、`styles`，返回 `Content`。

`NativeRuleMap::new` 预注册的特殊规则：

[styles.rs:L998-L1035](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L998-L1035) —— 对 `Paged`/`Html`/`Bundle` 三个 target，预注册 `CONTEXT_RULE`（处理 `ContextElem` 的内省求值）、`COUNTER_DISPLAY_RULE`（用原生方式实现 `counter(..).display(..)`，因为编译器还没有原生闭包），以及一批「仅为内省存在、渲染为空」的元素（`CounterUpdateElem`/`StateUpdateElem`/`MetadataElem`/`PrefixInfo`，都用 `empty` 函数返回空内容）。对 `Paged`/`Html` 还注册了 `ASSET_UNSUPPORTED_RULE`/`DOCUMENT_UNSUPPORTED_RULE`。这些是「无论用户怎么写都必然存在」的内置规则，所以放在 `new` 里。

> 其中 `CONTEXT_RULE` 定义在 [foundations/context.rs:L78-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L78-L82)：它取出 `ContextElem` 的 location，构造一个带位置与样式的 `Context`，再调用元素内嵌的函数——这是 u9 将讲的「context 内省」在 show 规则层面的入口。

`register` 与 `get`：

[styles.rs:L1037-L1069](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1037-L1069) —— `register<T>` 用 `(T::ELEM, target)` 作键插入，若键已存在则 panic（防重复注册）；`replace` 相反，键不存在才 panic。`get(target, content)` 用 `(content.func(), target)` 查表，返回 `Option<NativeShowRule>`。

类型擦除的 show 函数 `NativeShowRule`：

[styles.rs:L1089-L1132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1089-L1132) —— 这是个内部模块。`NativeShowRule` 把带具体类型 `ShowFn<T>`（吃 `&Packed<T>`）的函数，用 `transmute` 擦除成吃 `&Content` 的统一签名（因为 `Packed<T>` 是 `Content` 的 transparent wrapper，u3-l3）。`apply` 时先 `assert_eq!(content.elem(), self.elem)` 保证类型正确，再 `unsafe` 调用。这是又一个「把 unsafe 边界收敛到一处受检转换」的例子（与 u3-l2 的 vtable 哲学一致）。

#### 4.4.4 代码实践

**实践目标**：说明 `Library.rules` 的来源，并追踪一次「内置规则查询」。

**操作步骤**：

1. 读 [lib.rs:L220-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L220-L234)，确认 `rules` 来自 `(self.routines.rules)()`。
2. 读 [routines.rs:L50-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L50-L53)，确认 `rules` 例程的签名是 `fn() -> NativeRuleMap`。
3. 读 [crates/typst/src/lib.rs:L312-L317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L312-L317)，确认实现里调用了 `typst_layout::register` 与 `typst_html::register`。
4. 读 [styles.rs:L1037-L1069](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1037-L1069)，理解 `register`/`get` 的键是 `(Element, Target)`。

**关于 `Library.rules` 来源的说明**：

```text
Library.rules
  = (LibraryBuilder.routines.rules)()           // lib.rs:230，调用函数指针
  = <在主 typst crate 的 ROUTINES.rules 闭包>   // crates/typst/src/lib.rs:312
      let mut rules = NativeRuleMap::new();     // 预注册 CONTEXT_RULE 等特殊规则
      typst_layout::register(&mut rules);       // 各布局元素(rect/circle/...)的 show 函数
      typst_html::register(&mut rules);         // HTML 后端的 show 函数
      rules
```

**需要观察的现象**：`typst-library` 的 `Cargo.toml` 并不依赖 `typst-layout`/`typst-html`（可在 `Cargo.toml` 里核对），但 `Library.rules` 运行时却包含了来自它们的函数。这正是「函数指针实现动态链接、打破编译期循环依赖」的体现。

**预期结果**：你能解释为何 `NativeRuleMap::new()` 要预注册那批「渲染为空」的内省元素——因为它们在文档树中存在（供 query 检索），但在视觉输出中不应产生任何内容。

> 待本地验证：`typst-layout`/`typst-html` 的 `register` 函数体在各自 crate 中，本讲不展开；若要确认它们调用 `NativeRuleMap::register`，可在 `crates/typst-layout`、`crates/typst-html` 内搜索 `NativeRuleMap` 或 `register(Target::`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `NativeRuleMap` 的键是 `(Element, Target)` 而不只是 `Element`？

**参考答案**：同一个元素在不同输出后端（paged PDF vs HTML）有不同的默认渲染。例如表格在 PDF 里是分页布局，在 HTML 里可能变成 `<table>` 标签。用 `(Element, Target)` 作键可以让每个后端为同一元素注册各自的 show 函数，查询时按当前 `Target` 取对应的实现。

**练习 2**：`NativeShowRule::new` 里为什么需要 `transmute`？为什么不直接用泛型存 `ShowFn<T>`？

**参考答案**：`NativeRuleMap` 要在一个表里存放**所有元素**的 show 函数，而每个元素的 `ShowFn<T>` 因 `T` 不同而是不同的具体类型，无法统一存放。解决办法是把它们擦除成统一的 `unsafe fn(&Content, ...)` 签名（`NativeShowRule`）。因为 `Packed<T>` 是 `#[repr(transparent)]` 包裹 `Content`，`&Packed<T>` 与 `&Content` 布局兼容，`transmute` 才合法。`apply` 时的 `assert_eq!` 把这个 unsafe 的正确性义务收敛到调用点。

---

## 5. 综合实践

把本讲四个模块串起来：追踪 `#show heading.where(level: 1): it => text(red)[★ #it.body]` 这一条 show 规则从「写出来」到「命中并执行」的完整数据流，并标注每一步落在哪个类型/方法。

**任务**：

1. **分解语法**：这条规则会被求值层拆成 `Recipe`。指出它的 `selector` 是哪种 `Selector` 变体、`transform` 是哪种 `Transformation` 分支。（参考 4.2、4.3）
2. **构造 selector**：写出 `heading.where(level: 1)` 经 `Func::where_` 构造后的 `Selector::Elem` 等价表达式（含 `HeadingElem::ELEM` 与 `(level_id, Value::Int(1))`）。（参考 4.2.4）
3. **挂载**：说明这条 `Recipe` 会经 `Content::styled` 包成 `Style::Recipe` 挂到哪段内容子树上。（参考 4.3.3）
4. **与内置规则的关系**：当一个 `= 标题` 真的被排版时，realization 既会查用户 recipe（命中本规则），也会查 `Library.rules`（内置 show 规则，决定标题默认长相）。请说明这两类规则的**来源差异**：用户 recipe 来自 `StyleChain::recipes()`，内置规则来自 `(library.routines.rules)()`。（参考 4.4）
5. **执行**：命中后 `Recipe::apply` 走 `Func` 分支，把那个 heading 元素作为 `it` 传进闭包。说明闭包里的 `it.body` 是如何取到的（`Content::field_by_name("body")` / 按 ID 取字段）。（参考 4.1.3、4.3.3）

**预期产出**：一张标注了「数据类型 / 方法名 / 所在文件行号」的流程图或编号清单，能清楚区分「set 规则 → Property」「show 规则 → Recipe」「内置规则 → NativeRuleMap」三条路径。

## 6. 本讲小结

- `Style` 有三种条目：`Property`（set 规则产）、`Recipe`（show 规则产）、`Revocation`（禁用某条 recipe），它们混在同一个 `Styles` 列表里。
- 一条 set 规则经 `Element::set` → `Set` trait → `Field::set` → `Property::new`，把「元素 + 字段编号 + 擦除类型的值」打包，其中字段编号 `id: u8` 是 `#[elem]` 宏生成的 const generic 常量，值的类型擦除靠 `Block(Box<dyn Blockable>)`。
- `Selector::Elem(element, fields)` 是最常见的选择器；`.where(...)` 通过 `Func::where_` 把字段名翻译成编号，匹配时用 `Content::get(id, styles)` 逐字段比相等；`matches` 对需要文档位置的变体（`Regex`/`Before`/`After`/`Within`）返回 false，留给 query 处理。
- `Recipe` 装着 `selector + transform`，`Transformation` 分 `Content`/`Func`/`Style` 三支；无选择器的捕获式 show 规则会被 `Content::styled_with_recipe` 立即应用，其余挂载等 realization 匹配；show-set（`Style` 分支）复用了 `Property` 机制。
- `NativeRuleMap` 是「`(元素, Target) → Rust show 函数`」的内置规则表，`NativeShowRule` 用 `transmute` 把 `ShowFn<T>` 擦除成统一签名。
- `Library.rules` 来自 `(routines.rules)()`，实现在主 `typst` crate 中调 `typst_layout::register`/`typst_html::register` 填表——这是「函数指针实现动态链接、打破 crate 循环依赖」的典型范例。

## 7. 下一步学习建议

- 本讲只到「样式数据的构造与查询」，**set/show 规则的求值循环与 recipe 的匹配调度**发生在 `typst-realize`，建议在学完第 5 单元（编译环境）后阅读 `typst-realize` 源码，把「数据」与「行为」对接起来。
- `Selector` 的 `Before`/`After`/`Within` 以及 `LocatableSelector`/`ShowableSelector` 的能力约束，与第 9 单元「内省」紧密相关——学到 u9-l1（Location/Tag/query）时你会再次用到本讲的 `Selector::matches`。
- 想深入 `Property` 的两个布尔标记 `liftable`/`outside` 如何影响页眉页脚样式，可结合第 6 单元 u6-l4（PageElem）的 `Styles::root` 调用点阅读。
- 下一讲进入第 5 单元 u5-l1（World trait），将离开「样式/内容」数据层，转向编译环境抽象，为理解整个编译流水线打下基础。
