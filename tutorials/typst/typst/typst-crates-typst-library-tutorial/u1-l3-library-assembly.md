# 标准库的装配：Library、Builder、global 与 prelude

## 1. 本讲目标

前两讲我们确立了 `typst-library` 的定位（既是标准库、又集中了编译器的核心类型定义）和「目录—分类—装配」三张地图。本讲回答一个更具体的问题：

> 当一次编译开始前，Typst 是怎样把散落在十几个目录里的几百个类型、元素、函数「拼装」成一个用户在 Typst 脚本里可以直接使用的标准库的？

读完本讲，你应当能够：

1. 说清 `Library` 结构体的七个字段各自承担什么职责。
2. 用 `LibraryBuilder` 解释标准库的「构造 → 配置 → build」三步流程。
3. 描述 `Category` 如何在装配阶段给每条定义打上分类标签。
4. 按顺序复述 `global()` 总装函数的装配步骤，并区分「直接注入」与「子模块挂载」两种注册模式。
5. 说明 `Features` / `Feature` 三个特性开关如何按需开启额外定义，以及 `prelude()` 如何把颜色、方向、对齐等常用值提升为全局常量。

## 2. 前置知识

本讲假设你已经了解（来自 u1-l1、u1-l2）：

- **类型 vs 行为的拆分**：类型作为公共词汇留在本 crate，求值 / 收敛 / 排版等行为被拆到 `typst-eval` / `typst-realize` / `typst-layout` 等行为 crate，本 crate 通过一张 `Routines` 函数指针表在运行期回调它们。
- **`src/` 的 13 个顶层模块**：10 个标准库内容模块 + 3 个编译器基础设施模块（`diag` / `engine` / `routines`）。
- **`World` / `Library` / `Engine` 三支柱**：`World` 持有 `Library`（标准库配置），`Engine`（编译上下文）把它们组合起来驱动编译。

本讲用到但会现场解释的新概念：

- **作用域（Scope）**：一个「名字 → 绑定（Binding）」的有序映射，是用户可见命名空间的载体。
- **绑定（Binding）**：被注册的一个值（类型、函数、元素或常量），附带元信息（分类、span、是否弃用等）。
- **特性开关（Feature）**：还在开发中、默认关闭的功能，需要显式开启。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | 本讲主战场：定义 `Library`、`LibraryBuilder`、`Features` / `Feature`、`Category`，以及总装函数 `global()` 与 `prelude()`。 |
| [`src/foundations/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs) | `foundations::define()`，是「直接注入式」注册的典型样本，演示 `start_category` / `define_type` / `define_func` 的用法。 |
| [`src/foundations/scope.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs) | `Scope` 与 `Binding` 的实现，注册原语（`define_func` / `define_type` / `define_elem` / `define`）与分类打标签的底层机制都在这里。 |
| [`src/model/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/mod.rs) | `model::define()`，演示特性开关（`Feature::Bundle`）如何按需注册元素。 |
| [`src/pdf/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/mod.rs) | `pdf::module()`，演示「子模块挂载式」注册与 `Feature::A11yExtras` 开关。 |
| [`src/math/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs) | `math::module()`，数学子模块如何被独立构建再挂载到全局。 |
| [`src/routines.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs) | `Routines` 函数指针表，其中 `html_module` 例程用于按需装配 HTML 模块。 |

## 4. 核心概念与源码讲解

### 4.1 Library 与 LibraryBuilder：标准库的配置对象

#### 4.1.1 概念说明

一次 Typst 编译需要一个确定的「标准库」——它告诉编译器：用户写 `int` 指的是哪个类型、`panic` 是哪个函数、`math` 子模块里有哪些元素、默认字号是多大，等等。这个标准库在源码里被抽象成一个结构体 `Library`。

理解 `Library` 的关键，是把它看作**「编译前一次性装配好的配置对象」**：它不是在编译过程中动态生长的，而是在编译开始前由装配流程一次性填满。装配完成后它就被冻结（实际上放在 `LazyHash` 里供增量编译做指纹比对），编译期只读。

`Library` 共有七个字段，分别承载：行为回调表、全局可见定义、数学模式定义、默认样式、内置 show 规则、`std` 绑定、已开启的特性。

#### 4.1.2 核心流程

构造 `Library` 的标准路径是 **builder 模式**：

```
LibraryBuilder::from_routines(routines)   // 起点：注入行为回调表
    .with_inputs(inputs_dict)              // 可选：配置 sys.inputs
    .with_features(features)               // 可选：开启特性开关
    .build()                               // 终点：组装出 Library
```

`build()` 内部会真正调用总装函数 `global()`（见 4.3）把所有定义收集起来。也就是说，**`LibraryBuilder` 负责「可配置」，`global()` 负责「真正装配」**。

#### 4.1.3 源码精读

先看 `Library` 结构体本身——七个字段就是标准库的全部「断面」：

```rust
// src/lib.rs
#[derive(Debug, Clone, Hash)]
#[non_exhaustive]
pub struct Library {
    pub routines: &'static Routines, // 行为回调表（函数指针）
    pub global: Module,              // 全局可见的定义
    pub math: Module,                // 数学模式专属定义
    pub styles: Styles,              // 默认样式（字号、页面等）
    pub rules: NativeRuleMap,        // 内置 show 规则
    pub std: Binding,                // 整个标准库作为一个值（提供 std 模块）
    pub features: Features,          // 已开启的特性
}
```

完整定义见 [src/lib.rs:166-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L166-L183)，这段代码定义了标准库的七字段断面。注意三个细节：

- `routines` 是 `&'static Routines`——一张静态生命周期的函数指针表，由此可在不依赖行为 crate 的前提下回调它们（u1-l1 已述）。
- `global` 与 `math` 都是 `Module`（模块），分别是「普通模式」和「数学模式」下的用户可见命名空间。
- `std: Binding` 把**整套标准库本身**也绑定成一个可访问的值，于是用户在脚本里写 `std`、`std.calc`、`std.list` 都能取到。

再看 builder。`LibraryBuilder` 只持有三个最基础的配置项，其余都在 `build()` 里现算：

```rust
// src/lib.rs:189-193
pub struct LibraryBuilder {
    routines: &'static Routines,
    inputs: Option<Dict>,
    features: Features,
}
```

起点 [`from_routines`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L198-L204) 注入行为回调表；[`with_inputs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L207-L210) 与 [`with_features`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L215-L218) 是两个可选配置方法（builder 风格，消耗并返回 `Self`）。

真正的装配发生在 [`build()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234)：

```rust
// src/lib.rs:221-234
pub fn build(self) -> Library {
    let math = math::module();                                   // ① 先建数学模块
    let inputs = self.inputs.unwrap_or_default();
    let global = global(self.routines, math.clone(), inputs, &self.features); // ② 总装全局
    Library {
        routines: self.routines,
        global: global.clone(),
        math,
        styles: Styles::new(),
        rules: (self.routines.rules)(),                         // ③ 调例程生成内置 show 规则
        std: Binding::detached(global),                          // ④ std 包装「全套」全局模块
        features: self.features,
    }
}
```

这段代码展示了装配的顶层编排，要点有四：

1. **数学模块最先建**（步骤 ①）。它既会作为 `Library.math` 字段，又会被 `global()` 挂载进全局命名空间，所以提前建好后传入。
2. **`global()` 是总装核心**（步骤 ②），4.3 节详解。
3. **内置 show 规则来自例程**（步骤 ③）：`rules` 字段调用 `(self.routines.rules)()`——又一个通过 `Routines` 函数指针把行为交给别处 crate 的例子。
4. **`std` 字段包装的是「全套」全局模块**（步骤 ④）：`Binding::detached(global)` 用的是已经装配完成（含 `math` / `pdf` / prelude 常量）的 `global`，所以 `std` 能访问到一切。`Library.global` 字段则存了一份 `global.clone()`，二者内容相同。

> 关于公共入口：`Library` 的文档注释提到，用户应通过 `LibraryExt` trait 调用 `Library::builder()` / `Library::default()`（见 [src/lib.rs:159-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L159-L163)）。该 trait 不在本 crate 内定义（应在 reexport 本 crate 的主 `typst` crate 中），其实现最终委托给这里的 `LibraryBuilder`。本讲聚焦可在此 crate 直接验证的 `LibraryBuilder` 装配路径。

#### 4.1.4 代码实践

**实践目标**：从源码层面追踪 `Library` 是怎么被 `build()` 出来的，建立「七字段从哪来」的对应关系。

**操作步骤**：

1. 打开 [src/lib.rs:221-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234) 的 `build()`。
2. 对照 `Library` 的七字段（[src/lib.rs:166-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L166-L183)），逐字段在 `build()` 函数体里找到它被赋值的表达式。

**需要观察的现象**：你会看到七字段中，`global`、`math`、`rules`、`std` 都在 `build()` 里被显式构造；`routines`、`features` 是从 builder 透传；唯独 `styles` 用的是 `Styles::new()`（空样式）。

**预期结果**：你能列出这样一张对照表——

| `Library` 字段 | `build()` 中的来源 |
| --- | --- |
| `routines` | `self.routines`（透传） |
| `global` | `global.clone()`（来自 `global()` 总装） |
| `math` | `math::module()`（提前构建） |
| `styles` | `Styles::new()`（初始为空，由 set 规则填充） |
| `rules` | `(self.routines.rules)()`（调例程生成） |
| `std` | `Binding::detached(global)`（包装全套全局模块） |
| `features` | `self.features`（透传） |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `math` 要在 `build()` 里先于 `global()` 构建，而不是在 `global()` 内部构建？

> **参考答案**：因为数学模块要被用到两个地方——既作为 `Library.math` 字段独立存在，又被挂载进全局命名空间（脚本里写 `math.frac`）。在 `build()` 里先建好 `math`，再把它 `clone()` 一份传给 `global()` 挂载，就能让 `Library.math` 与全局 `math` 子模块指向同一份定义，避免重复装配。

**练习 2**：`std` 字段为什么用「已经装配完成」的 `global`，而 `Library.global` 字段用 `global.clone()`？如果反过来（`std` 用 clone、`global` 用原件）会有区别吗？

> **参考答案**：`Module` 是可克隆的内容对象，clone 出来内容一致，所以两种写法在内容上等价。源码选择 `std: Binding::detached(global)`（原件包进 `std`）、`global: global.clone()`（clone 存字段），只是为了表达「`std` 才是这套全局模块的『权威封装』」。核心在于两者内容相同，用户通过 `std.xxx` 与直接写 `xxx` 取到的是同一份定义。

---

### 4.2 Category 与 Scope 注册原语：定义如何被打上分类标签

#### 4.2.1 概念说明

标准库有几百条定义，Typst 文档站需要把它们分组展示（Foundations / Layout / Text / Math…）。这种分组不是事后推断的，而是在**装配时就给每条绑定打上分类标签**。这个标签就是 `Category` 枚举。

而「打标签」这件事的实现，藏在 `Scope` 的注册原语里。`Scope` 提供了一组注册方法：

- `define_type::<T>()`：注册一个原生类型（如 `int`、`str`）。
- `define_func::<F>()`：注册一个原生函数（如 `panic`、`eval`）。
- `define_elem::<E>()`：注册一个原生元素（如 `HeadingElem`）。
- `define(name, value)`：通用注册，按编译期已知的名字注册任意值（如颜色常量、子模块）。

这些方法最终都汇入同一个底层 `define`，并把当前 `Scope` 记录的 `category` 盖到绑定上。

#### 4.2.2 核心流程

分类打标签的机制是一个简单的「作用域级开关」：

```
scope.start_category(Category::Foundations)   // 打开开关：接下来注册的都归 Foundations
    scope.define_type::<i64>(...)              // 这条绑定的 category = Foundations
    scope.define_func::<panic>(...)            // 这条绑定的 category = Foundations
    ...
scope.reset_category()                        // 关闭开关：之后的注册不带分类
```

`start_category` 把一个 `Category` 存进 `Scope` 的 `category` 字段；此后每次 `define` 都把它拷进 `Binding.category`；`reset_category` 清空它。因此**分类是「区间式」的**——一段连续的注册共享同一个分类。

#### 4.2.3 源码精读

先看 `Category` 枚举——共 14 个变体，是标准库的全部分类标签：

```rust
// src/lib.rs:289-304
pub enum Category {
    Foundations, Introspection, Layout, DataLoading, Math, Model,
    Symbols, Text, Visualize, Pdf, Html, Svg, Png, Bundle,
}
```

每个变体在 [`Category::name()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L308-L325) 里映射成 kebab-case 字符串（如 `DataLoading → "data-loading"`），用于文档分组与序列化。注意它与目录并非一一对应：`Svg` / `Png` / `Bundle` 在本 crate 并无实现（由行为 crate 或特性开关提供），`DataLoading` 对应 `loading/` 目录。

再看注册原语，全在 `Scope` 上。分类开关两个方法：

```rust
// src/foundations/scope.rs:126-133
pub fn start_category(&mut self, category: Category) { self.category = Some(category); }
pub fn reset_category(&mut self) { self.category = None; }
```

四个注册方法都很薄，最终都落到 `define`：

```rust
// src/foundations/scope.rs:136-163（节选）
pub fn define_func<T: NativeFunc>(&mut self) -> &mut Binding {
    let data = T::data();
    self.define(data.name, Func::from(data))
}
pub fn define_type<T: NativeType>(&mut self) -> &mut Binding {
    let ty = T::ty();
    self.define(ty.short_name(), ty)
}
pub fn define_elem<T: NativeElement>(&mut self) -> &mut Binding {
    let elem = T::ELEM;
    self.define(elem.name(), elem)
}
```

而 `define` 是打标签的真正落点——它把当前 `self.category` 拷进新建的 `Binding`：

```rust
// src/foundations/scope.rs:176-185
pub fn define(&mut self, name: &'static str, value: impl IntoValue) -> &mut Binding {
    #[cfg(debug_assertions)]
    if self.deduplicate && self.map.contains_key(name) {
        panic!("duplicate definition: {name}");   // 调试构建下禁止重名
    }
    let mut binding = Binding::detached(value);
    binding.category = self.category;              // ← 分类标签在这里盖上
    self.bind(name.into(), binding)
}
```

这段代码揭示了三件事：注册名必须是编译期 `&'static str`；调试构建下用 `deduplicate` 防重名；分类标签在 `define` 里一次性盖到 `Binding.category` 上。`Binding` 结构体本身（见 [src/foundations/scope.rs:250-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L250-L261)）含 `value` / `kind` / `span` / `category` / `deprecation` 五项元信息，`category` 正是其中之一。

最后看一个真实样本 `foundations::define`，它把上面的原语串起来用：

```rust
// src/foundations/mod.rs:91-122（节选）
pub(super) fn define(global: &mut Scope, inputs: Dict) {
    global.start_category(crate::Category::Foundations);
    global.define_type::<bool>();
    global.define_type::<i64>();
    // ... define_type 多次 ...
    global.define_func::<repr::repr>();
    global.define_func::<panic>();
    global.define_func::<assert>();
    global.define_func::<eval>();
    global.define_func::<plugin>();
    global.define_func::<target>();
    global.define("calc", calc::module());
    global.define("sys", sys::module(inputs));
    global.reset_category();
}
```

这就是「直接注入式」注册的标准写法：`start_category` 与 `reset_category` 成对包裹，中间的每一行注册都自动带上 `Foundations` 分类。注意 `define("calc", calc::module())` 与 `define("sys", sys::module(inputs))` 是把子模块作为子命名空间挂进全局，它们同样被打上 `Foundations` 分类。

#### 4.2.4 代码实践

**实践目标**：验证「分类标签在装配时被打上」，并理解 `start_category` / `reset_category` 的区间语义。

**操作步骤**：

1. 阅读 [src/foundations/mod.rs:91-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L91-L123) 的 `foundations::define`。
2. 数一数 `start_category(Foundations)` 与 `reset_category()` 之间调用了多少次 `define_type`、多少次 `define_func`、多少次 `define`。
3. 阅读 [src/foundations/scope.rs:176-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L176-L185) 的 `Scope::define`，确认 `binding.category = self.category` 这一行。

**需要观察的现象**：`foundations::define` 里没有任何地方显式写 `category = Foundations`；分类完全由顶部的 `start_category` 隐式赋予。

**预期结果**：约 20 次 `define_type`（bool/i64/f64/Str/Label/Bytes/Content/Array/Dict/Func/Args/Type/Module/Regex/Selector/Datetime/Decimal/Symbol/Duration/Version/RootedPath，可逐行核对）、6 次 `define_func`（`repr` / `panic` / `assert` / `eval` / `plugin` / `target`）、2 次 `define`（`calc` / `sys`），全部归入 `Foundations` 分类。

#### 4.2.5 小练习与答案

**练习 1**：如果某个模块的 `define` 函数忘了写 `reset_category()`，会出什么问题？

> **参考答案**：`Scope` 的 `category` 字段不会被清空，后续该 `Scope` 上注册的所有绑定（包括其它模块注入进来的、prelude 的常量等）都会错误地带上这个分类标签，导致文档分组错乱。因此 `start_category` 与 `reset_category` 必须严格成对出现。

**练习 2**：`define_type` / `define_func` / `define_elem` 三个方法签名很相似，为什么 `define_type` 用 `short_name()`、`define_elem` 用 `name()` 作为注册名？

> **参考答案**：类型与元素/函数的命名约定不同。类型用短名（如 `int` 而非 `integer`）作为用户书写名；元素的名字由 `#[elem]` 宏生成的 `ELEM.name()` 决定（通常是 kebab-case，如 `heading`）。这只是命名来源不同，三者最终都调用同一个 `define`，都走相同的分类打标签流程。

---

### 4.3 global()：标准库的总装流程

#### 4.3.1 概念说明

`global()` 是整个标准库的「总装车间」。它接收行为回调表、数学模块、inputs、特性开关四样输入，产出一个装配好的全局 `Module`。理解 `global()` 就理解了「Typst 标准库由哪些部分组成、以什么顺序拼起来」。

`global()` 里用到了**两种注册模式**，这是本讲最重要的区分：

- **直接注入式**：调用模块的 `define(&mut global, ...)`，把该模块的类型 / 函数 / 元素**直接铺进**全局作用域（用户无需前缀即可访问，如直接写 `panic`、`heading`）。
- **子模块挂载式**：先构建一个独立的 `Module`，再用 `global.define(name, module)` 把它**作为一个子命名空间**挂进全局（用户需带前缀访问，如 `math.frac`、`pdf.attach`）。

哪些模块用哪种模式？答案就在 `global()` 的代码里。

#### 4.3.2 核心流程

`global()` 的执行顺序就是标准库的装配顺序：

```
1. 新建一个去重的 Scope（deduplicating）
2. foundations::define(...)   ─┐
3. model::define(...)          │
4. text::define(...)           │  直接注入式：
5. layout::define(...)         │  八个模块把定义铺进全局作用域
6. visualize::define(...)      │
7. introspection::define(...)  │
8. loading::define(...)        │
9. symbols::define(...)       ─┘
10. global.define("math", math)           ─┐
11. global.define("pdf", pdf::module())    │  子模块挂载式：
12. （若开 Html）global.define("html", …) ─┘  作为子命名空间挂载
13. prelude(&mut global)        ← 注入颜色/方向/对齐等全局常量
14. Module::new("global", global) ← 封装成 Module 返回
```

两个关键点：第 10–12 步是「挂载子模块」，第 13 步 `prelude` 是「把常用值提升为全局」（4.4 节详解）。

#### 4.3.3 源码精读

`global()` 的全部代码很短，却定义了整个标准库的骨架：

```rust
// src/lib.rs:329-355
fn global(routines: &Routines, math: Module, inputs: Dict, features: &Features) -> Module {
    let mut global = Scope::deduplicating();

    self::foundations::define(&mut global, inputs);
    self::model::define(&mut global, features);
    self::text::define(&mut global);
    self::layout::define(&mut global);
    self::visualize::define(&mut global);
    self::introspection::define(&mut global);
    self::loading::define(&mut global);
    self::symbols::define(&mut global);

    global.define("math", math);
    global.define("pdf", self::pdf::module(features));
    if features.is_enabled(Feature::Html) {
        global.define("html", (routines.html_module)());
    }

    prelude(&mut global);

    Module::new("global", global)
}
```

逐段读这段代码：

- **起点** [`Scope::deduplicating()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L120-L123)：创建一个开启去重检查的作用域——调试构建下若注册了重名定义会直接 panic（见 4.2.3 的 `define`），这是防止两个模块意外注册同名定义的安全网。
- **八个直接注入**（[src/lib.rs:337-344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L337-L344)）：`foundations` / `model` / `text` / `layout` / `visualize` / `introspection` / `loading` / `symbols`。注意它们的签名不统一——`foundations` 多收一个 `inputs`（喂给 `sys.inputs`），`model` 多收一个 `features`（按需注册 `AssetElem`，见 4.4）。每个 `define` 内部都会 `start_category` / `reset_category`（如 4.2.3 所见）。
- **三个子模块挂载**（[src/lib.rs:346-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L346-L350)）：`math`、`pdf`、`html`。它们都是先有独立 `Module` 再挂载。为什么 `math` / `pdf` 用挂载式？因为它们是用户需要带前缀访问的子命名空间（`math.frac`、`pdf.attach`），不该把里面的定义直接铺到全局。

来看看挂载式模块是怎么构建的。数学模块 [`math::module()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L42-L70) 用 `Category::Math` 包裹一组 `define_elem`，然后封装成 `Module::new("math", …)`：

```rust
// src/math/mod.rs:43-48（节选）
pub fn module() -> Module {
    let mut math = Scope::deduplicating();
    math.start_category(crate::Category::Math);
    math.define_elem::<EquationElem>();
    // ... 大量 define_elem ...
}
```

PDF 模块 [`pdf::module()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/mod.rs#L12-L24) 同理，并演示了特性开关（4.4 节详解）：

```rust
// src/pdf/mod.rs:13-24
pub fn module(features: &Features) -> Module {
    let mut pdf = Scope::deduplicating();
    pdf.start_category(crate::Category::Pdf);
    pdf.define_elem::<AttachElem>();
    pdf.define_elem::<ArtifactElem>();
    if features.is_enabled(Feature::A11yExtras) {
        pdf.define_func::<table_summary>();
        pdf.define_func::<header_cell>();
        pdf.define_func::<data_cell>();
    }
    Module::new("pdf", pdf)
}
```

- **HTML 模块的特殊性**（[src/lib.rs:348-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L348-L350)）：它是唯一**默认不挂载**、且**经 `Routines` 函数指针装配**的模块。代码 `(routines.html_module)()` 不在本 crate 调用本 crate 的函数，而是回调行为 crate 注入的 `html_module` 例程。这又是 u1-l1 所述「用 `Routines` 做动态链接以实现 crate splitting」的实例——HTML 装配逻辑在别的 crate，本 crate 只握着一个函数指针。

> 为什么 HTML 要走 routine 而数学 / PDF 不用？因为 HTML 导出涉及大量与排版 / 实现化（realization）耦合的元素定义，这些行为都在行为 crate 里；为了不在本 crate 引入对那些 crate 的依赖，只能通过函数指针回调。`html_module` 例程的声明见 [src/routines.rs:100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L100-L101)。

- **prelude 收尾**（[src/lib.rs:352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L352)）：所有「正式模块」注入完后，再注入一批全局常量（4.4 详解）。
- **封装返回**（[src/lib.rs:354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L354)）：`Module::new("global", global)` 把这个作用域封装成名为 `"global"` 的模块，交给 `build()` 存进 `Library.global` 与 `Library.std`。

#### 4.3.4 代码实践

**实践目标**：亲手把 `global()` 的装配顺序整理成清单，并验证「启用 `Feature::Html` 会额外挂载什么」。

**操作步骤**：

1. 打开 [src/lib.rs:329-355](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L329-L355) 的 `global()`。
2. 从上到下逐行编号，把每个 `define(...)` / `module::define(...)` 调用记成一条「装配步骤」，并标注它属于「直接注入」还是「子模块挂载」。
3. 找到 `if features.is_enabled(Feature::Html)` 分支（[src/lib.rs:348-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L348-L350)），点进 `routines.html_module`（[src/routines.rs:100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L100-L101)）看其签名。

**需要观察的现象**：装配顺序严格按源码自上而下；只有 HTML 这一步是条件性的；`math` 与 `pdf` 用 `global.define(name, module)` 挂载，其余八个模块用 `module::define(&mut global)` 注入。

**预期结果**：你应当得到一张如下清单（顺序即装配顺序）——

| # | 步骤 | 模式 |
| --- | --- | --- |
| 1 | `foundations::define(global, inputs)` | 直接注入 |
| 2 | `model::define(global, features)` | 直接注入 |
| 3 | `text::define(global)` | 直接注入 |
| 4 | `layout::define(global)` | 直接注入 |
| 5 | `visualize::define(global)` | 直接注入 |
| 6 | `introspection::define(global)` | 直接注入 |
| 7 | `loading::define(global)` | 直接注入 |
| 8 | `symbols::define(global)` | 直接注入 |
| 9 | `global.define("math", math)` | 子模块挂载 |
| 10 | `global.define("pdf", pdf::module(features))` | 子模块挂载 |
| 11 | （开 Html 时）`global.define("html", (routines.html_module)())` | 子模块挂载（条件 + routine） |
| 12 | `prelude(global)` | 全局常量注入 |

关于「启用 `Feature::Html` 会额外注册什么」：会额外挂载一个名为 `html` 的子模块，其内容由行为 crate 通过 `routines.html_module` 例程提供（本 crate 不实现其内部，故标注为「实现见行为 crate，待在 HTML 相关讲义确认」）。换言之，开了 `Html`，用户脚本里才能访问 `html.div`、`html.span` 等；不开则全局命名空间里根本没有 `html`。

#### 4.3.5 小练习与答案

**练习 1**：`foundations` 和 `math` 都是标准库内容，为什么 `foundations` 用直接注入、`math` 用子模块挂载？

> **参考答案**：因为用户访问方式不同。`foundations` 里的类型与函数（`int`、`panic`、`eval`）是日常高频、需要无前缀直接书写的；而数学元素（`frac`、`matrix`）只在数学模式中用，且数量多、与普通模式隔离，挂成 `math` 子命名空间（`math.frac`）更清晰，也避免与全局名字冲突。模式选择的依据是「用户访问方式」与「命名空间隔离需要」。

**练习 2**：`global()` 接收的 `math` 参数与它内部「第 9 步挂载的 `math`」是同一个对象吗？

> **参考答案**：是的，是同一个（内容相同的克隆）。`math` 由 `build()` 提前构建并 `clone()` 后传入 `global()`（见 4.1.3 的 `build()`）。`global()` 第 9 步 `global.define("math", math)` 把传入的这份挂载进全局。因此 `Library.math` 字段与全局 `math` 子模块指向同一份定义。

---

### 4.4 Features 与 prelude：特性开关与全局常量

#### 4.4.1 概念说明

本模块讲两件收尾性质的事：

**特性开关（Features / Feature）**：Typst 有一些还在开发中、默认关闭的功能。它们用一张小位图（`SmallBitSet`）记录「开了哪些」，装配时据此决定是否注册额外定义。目前有三个特性：

| `Feature` 变体 | 含义 | 装配时的影响 |
| --- | --- | --- |
| `Html` | HTML 导出 | 在 `global()` 中额外挂载 `html` 子模块 |
| `Bundle` | 资源打包（bundle） | 在 `model::define` 中额外注册 `AssetElem` |
| `A11yExtras` | 无障碍附加功能 | 在 `pdf::module` 中额外注册三个函数 |

**prelude（前导常量）**：在所有正式模块注入之后，`prelude()` 再向全局作用域注入一批「常用值」——颜色、方向、对齐等。它们之所以单独放最后注入，是因为这些值需要**不带任何前缀、在任何地方都能直接写**（脚本里直接写 `red`、`center`、`ltr`），属于全局便捷别名。

#### 4.4.2 核心流程

特性开关的数据模型很简单——`Features` 就是一个位集合：

```
Features = SmallBitSet   // 每个比特代表一个 Feature 是否开启
Feature  = { Html, Bundle, A11yExtras }   // 用 as usize 作为比特位下标
```

判定逻辑：`features.is_enabled(Feature::X)` 查位图里对应比特是否为 1。

装配时，三处分别用三个特性做条件注册：

```
model::define:  if Bundle      → 注册 AssetElem
pdf::module:    if A11yExtras  → 注册 table_summary / header_cell / data_cell
global():       if Html        → 挂载 html 子模块（经 routine）
```

#### 4.4.3 源码精读

先看特性开关的类型定义。`Features` 是 `SmallBitSet` 的新类型：

```rust
// src/lib.rs:240-258（节选）
#[derive(Debug, Default, Clone, Hash)]
pub struct Features(SmallBitSet);

impl Features {
    pub fn all() -> Self { Feature::all().collect() }     // 全开
    pub fn none() -> Self { Self::default() }             // 全关
    pub fn is_enabled(&self, feature: Feature) -> bool {
        self.0.contains(feature as usize)                 // 比特位查表
    }
}
```

`Feature` 是个 `#[non_exhaustive]` 枚举（意味着将来可能加新变体），目前三个：

```rust
// src/lib.rs:273-283
pub enum Feature { Html, Bundle, A11yExtras }

impl Feature {
    pub fn all() -> impl Iterator<Item = Self> {
        [Self::Html, Self::Bundle, Self::A11yExtras].into_iter()
    }
}
```

`FromIterator<Feature> for Features`（[src/lib.rs:260-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L260-L268)）把一组 `Feature` 收集成位图，所以可以写 `[Feature::Html].into_iter().collect::<Features>()`。`feature as usize` 把枚举变体转成比特下标——这也是为什么特性开关用位图而非 `HashSet`：特性数量少且固定，位图最省。

再看三处条件注册的真实代码。`Bundle` 在 [`model::define`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/mod.rs#L52-L80)：

```rust
// src/model/mod.rs:54-58
global.start_category(crate::Category::Model);
global.define_elem::<DocumentElem>();
if features.is_enabled(Feature::Bundle) {
    global.define_elem::<AssetElem>();   // 只有开 Bundle 才注册 AssetElem
}
```

`A11yExtras` 在 [`pdf::module`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/mod.rs#L12-L24)（4.3.3 已贴，三个 `define_func` 受开关控制）。`Html` 在 [`global()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L348-L350)（4.3.3 已述）。

最后看 `prelude()`。它在全局作用域里注入一批「无前缀全局常量」：

```rust
// src/lib.rs:358-395（节选）
fn prelude(global: &mut Scope) {
    global.define("black", Color::BLACK);
    // ... 18 个具名颜色：black/gray/silver/white/navy/blue/aqua/teal/eastern
    //     /purple/fuchsia/maroon/red/orange/yellow/olive/green/lime ...
    global.define("luma", Color::luma_data());      // 颜色构造器
    global.define("oklab", Color::oklab_data());
    global.define("oklch", Color::oklch_data());
    global.define("rgb", Color::rgb_data());
    global.define("cmyk", Color::cmyk_data());
    global.define("range", Array::range_data());    // 数组生成辅助
    global.define("ltr", Dir::LTR);                 // 方向
    global.define("rtl", Dir::RTL);
    global.define("ttb", Dir::TTB);
    global.define("btt", Dir::BTT);
    global.define("start", Alignment::START);       // 对齐
    // ... left/center/right/end/top/horizon/bottom ...
}
```

可以把 `prelude` 的注入内容分成五组：

| 组 | 成员 | 数量 | 来源行 |
| --- | --- | --- | --- |
| 具名颜色 | black…lime | 18 | [359-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L359-L376) |
| 颜色构造器 | luma / oklab / oklch / rgb / cmyk | 5 | [377-381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L377-L381) |
| 数组辅助 | range | 1 | [382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L382) |
| 方向 | ltr / rtl / ttb / btt | 4 | [383-386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L383-L386) |
| 对齐 | start / left / center / right / end / top / horizon / bottom | 8 | [387-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L387-L394) |

注意一个细节：`prelude` 注入这些常量时，`global` 的 `category` 字段是 `None`（因为前一步的 `symbols::define` 已 `reset_category`，而 `math` / `pdf` 是另开 `Scope` 构建的）。所以 prelude 常量**不带分类标签**——它们是跨分类的全局便捷别名，不属于任何单一分类。

还有一个关键事实：`prelude` 注入的颜色构造器（`luma` / `rgb` 等）与 `Color` 类型本身是分离的。`Color` 类型由 `visualize::define` 注册（带 `Visualize` 分类），而它的便捷构造器别名由 `prelude` 额外提升为全局无前缀可用——这就是为什么用户既能写 `rgb(…)` 又能写 `color.rgb(…)`。

#### 4.4.4 代码实践

**实践目标**：把三个特性开关与它们各自的「额外注册项」整理成对照表，并验证 prelude 常量不带分类。

**操作步骤**：

1. 阅读 [`Features::is_enabled`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L255-L257) 与 [`Feature` 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L273-L277)，理解 `feature as usize` 如何变成比特下标。
2. 在全 crate 内搜索三处 `is_enabled` 调用（用 Grep 搜 `Feature::` 在 `src/model/mod.rs`、`src/pdf/mod.rs`、`src/lib.rs`），核对每处受控的注册项。
3. 阅读 [`prelude()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L358-L395)，对照 4.2.3 的 `define`，确认这些常量被注册时 `self.category` 为 `None`。

**需要观察的现象**：三个特性各自只影响一处装配；`prelude` 的常量在 `symbols::define` 之后、且没有 `start_category`，故分类为空。

**预期结果**：得到对照表——

| `Feature` | 影响的装配位置 | 额外注册项 |
| --- | --- | --- |
| `Bundle` | `model::define`（src/model/mod.rs:56-58） | `AssetElem` |
| `A11yExtras` | `pdf::module`（src/pdf/mod.rs:18-22） | `table_summary` / `header_cell` / `data_cell` |
| `Html` | `global()`（src/lib.rs:348-350） | `html` 子模块（经 `routines.html_module`） |

#### 4.4.5 小练习与答案

**练习 1**：`Features` 为什么用 `SmallBitSet` 而不是 `HashSet<Feature>`？

> **参考答案**：特性数量少（目前 3 个，未来也不会太多）且变体固定，每个 `Feature` 用 `as usize` 当比特下标，一个位图就能 O(1) 表示「开了哪些」。位图比 `HashSet` 更省内存、查得更快，且 `Features` 派生了 `Hash`，能直接参与 `Library` 的指纹计算（供增量编译比对）——位图的 `Hash` 也比集合更稳定高效。

**练习 2**：为什么 `rgb` 既是颜色构造器、又能被无前缀直接调用？它在装配中被注册了两次吗？

> **参考答案**：没有被注册两次内容，而是有一个「类型 + 全局别名」的组合。`Color` 类型由 `visualize::define` 注册（带 `Visualize` 分类），其上的 `rgb` 方法作为类型的方法存在（`color.rgb`）；同时 `prelude` 又用 `global.define("rgb", Color::rgb_data())` 把同一个构造器提升为全局无前缀别名（不带分类）。所以 `rgb(…)` 和 `color.rgb(…)` 指向同一份构造逻辑，只是访问路径不同、分类标签不同。

**练习 3**：若同时开启 `Html` 和 `A11yExtras`，标准库会比「全关」多出哪些用户可见定义？

> **参考答案**：多出（1）全局 `html` 子模块（`Html` 触发，经 routine 挂载）；（2）`pdf` 子模块里的 `table_summary` / `header_cell` / `data_cell` 三个函数（`A11yExtras` 触发）。`Bundle` 未开，故 `AssetElem` 不会注册。

## 5. 综合实践

**任务：画出一张「标准库装配全景图」，并用源码行号佐证每一个箭头。**

把本讲四个模块串起来，完成下面的全景梳理：

1. **入口与配置**：从 [`LibraryBuilder::from_routines`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L198-L204) 出发，经 `with_inputs` / `with_features`，到 [`build()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234)。画出「builder 三字段 → Library 七字段」的映射。
2. **总装**：进入 [`global()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L329-L355)，画出「八个直接注入模块 → math/pdf/html 挂载 → prelude 注入」的流水线。
3. **分类与开关**：在每个注入模块旁标注它用的 `Category`（由 `start_category` 决定）；在 `model` / `pdf` / `global` 三处标注受哪个 `Feature` 控制。
4. **验证条件分支**：假设用户用 `LibraryBuilder::from_routines(..).with_features([Feature::Html, Feature::Bundle].into_iter().collect()).build()` 构造标准库，在你的全景图上标出哪些定义会出现、哪些不会（例如 `html` 子模块会出现、`pdf` 的三个 a11y 函数不会出现、`AssetElem` 会出现）。

**交付物**：一张手绘或文本版的装配全景图，每个节点旁标注源码 `文件:行号`，并用一句话说明该节点「打了什么分类、受不受特性开关影响」。

**预期结果**：你能凭这张图回答「`panic` 在哪注册、属什么分类」「`math.frac` 为什么带前缀」「开了 `Html` 会多出什么」「`red` 这个名字从哪来」等一系列问题。如果某个节点的行号无法确认，请标注「待确认」，不要编造。

## 6. 本讲小结

- `Library` 是编译前一次性装配好的配置对象，七字段（`routines` / `global` / `math` / `styles` / `rules` / `std` / `features`）覆盖标准库的全部断面；其中 `std` 包装的是全套全局模块，`rules` 与 HTML 模块都通过 `Routines` 函数指针向行为 crate 回调。
- 构造走 builder 模式：`LibraryBuilder::from_routines → with_inputs → with_features → build()`；`build()` 负责把七字段组装出来，并调用 `global()` 完成总装。
- `Category` 是标准库的 14 个分类标签，由 `Scope::start_category` / `reset_category` 区间式地盖到每条 `Binding` 上，使文档分组在装配期就确定。
- `global()` 是总装车间，用两种注册模式：八个模块「直接注入」铺进全局，`math` / `pdf` / `html`「子模块挂载」作为带前缀子命名空间；装配顺序就是源码自上而下的调用顺序。
- `Features`（`SmallBitSet`）+ `Feature`（`Html` / `Bundle` / `A11yExtras`）三处条件注册（`model` 的 `AssetElem`、`pdf` 的三个 a11y 函数、`global` 的 `html` 子模块）控制额外定义；`Html` 模块因耦合行为 crate 而经 `routines.html_module` 装配。
- `prelude()` 在最后注入 36 个全局常量（颜色、构造器、方向、对齐、`range`），它们不带分类标签，是把常用值提升为「无前缀全局可用」的便捷别名。

## 7. 下一步学习建议

本讲把「标准库如何被装配成一个 `Library`」讲完了。接下来建议：

- **深入值与类型系统**：`Library.global` / `Library.math` 装的全是 `Module`，而 `Module` 内部是 `Scope` → `Binding` → `Value`。建议进入第 2 单元（u2-l1「Value 枚举与标量类型」），从 `Value` 枚举开始，理解每条绑定最终装的是什么。
- **理解 `Scope` 的查找语义**：本讲只讲了 `Scope` 的「写入」原语，没讲「读取」。`Scopes::get` 如何回退到标准库 base 与 `std`（见 [src/foundations/scope.rs:46-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)）会在 u2-l3「类型转换系统」中结合求值展开。
- **看 `Routines` 如何被实现**：本讲多次提到「行为在别的 crate、本 crate 只握函数指针」。若想看这些例程的真实实现（如 `eval_string` 在 `eval()` 中的调用，见 [src/foundations/mod.rs:305-321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L305-L321)），建议进入第 5 单元（u5-l4「Routines 与 crate 分离机制」）。
