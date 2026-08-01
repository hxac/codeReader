# Library 构建与特性开关

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `Library` 结构体的 7 个字段各自代表什么、由谁填充、被编译流程的哪一步使用。
- 看懂建造者（builder）模式：`LibraryExt::default()` / `LibraryExt::builder()` 两个入口，以及 `LibraryBuilder` 的 `from_routines` / `with_inputs` / `with_features` / `build` 四个方法。
- 跟踪 `global()` 函数如何按 **category（分类）** 把 foundations / model / text / layout / visualize / introspection / loading / symbols 八个子模块的 `define` 函数串联起来装配全局作用域，以及 `prelude()` 注入了哪些「无需导入即可使用」的预设值。
- 理解 `Features` / `Feature` 开关的位集合实现，并说出 `Feature::Html`、`Feature::Bundle`、`Feature::A11yExtras` 三者分别门控了哪些条件注册。

本讲是 **advanced** 层：默认你已经学过 u1-l2（`World` trait，知道 `world.library()` 返回 `&LazyHash<Library>`）和 u3-l2（`ROUTINES` 函数指针表与 crate 切分）。本讲要回答的核心问题是：**那个被 `World` 持有的 `Library`，到底是从哪里、按什么流程、被谁「组装」出来的？**

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

- **标准库（standard library）**：在 Typst 里，`#rect()`、`#text()`、`#red`、`#align(center)` 这些「开箱即用」的能力并不是语法层面的关键字，而是注册在一张大表里的值（函数、颜色、元素等）。这张表就是 `Library`。换句话说，**`Library` 是 Typst 运行时的「全家桶」**：求值器在遇到一个标识符（如 `rect`）时，就是到 `Library.global` 这张作用域里去查它的定义。
- **模块（`Module`）与作用域（`Scope`）**：`Scope` 是「名字 → 值」的映射表；`Module` 是给 `Scope` 套了一层名字的封装，可以在脚本里被 `import`。`global` 是默认对所有代码可见的顶层模块，`math` 是数学模式专用模块。
- **category（分类）**：为了让文档生成器（docs）能把上百个函数按「foundations / text / layout …」分组展示，每个值在被注册进作用域时都会被打上一个 `Category` 标签。这不是功能性的，而是「元数据」。
- **特性开关（feature flag）**：Typst 仍在开发中的实验能力（HTML 导出、bundle、无障碍增强等）不希望被普通用户默认依赖。这些能力被收敛成 `Feature` 枚举，只有在构建 `Library` 时显式开启，对应的函数/模块才会被注册进作用域，否则连名字都查不到。
- **建造者模式（builder pattern）**：当一个对象有很多可选配置项时，与其写一个参数巨多的构造函数，不如提供一个 `Builder`，用链式调用 `with_xxx()` 逐项设置，最后 `build()` 产出对象。`LibraryBuilder` 就是这种写法。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | 定义 `Library` 结构体、`LibraryBuilder`、`Features`/`Feature`、`Category`、`global()`、`prelude()`。本讲的主战场。 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 定义 `LibraryExt` trait（`default`/`builder`）和 `ROUTINES` 静态量。这是「门面层」装配 `Library` 的接线点。 |
| [crates/typst-library/src/foundations/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs) | `foundations::define`，演示一个 category 的 `define` 函数长什么样。 |
| [crates/typst-library/src/model/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs) | `model::define`，演示 `Feature::Bundle` 如何门控 `AssetElem` 的注册。 |
| [crates/typst-library/src/pdf/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/mod.rs) | `pdf::module`，演示 `Feature::A11yExtras` 如何门控无障碍函数的注册。 |
| [crates/typst-library/src/foundations/scope.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs) | `Scope::deduplicating` / `start_category` / `reset_category`，是 category 装配的底层机制。 |
| [crates/typst-cli/src/world.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs) | CLI 真实调用 `Library::builder().with_inputs(...).with_features(...).build()` 的地方，是本讲代码实践的依据。 |

> 一个关键认知：`Library` 的**定义**（结构体、字段、`build()` 的装配逻辑）在 `typst-library` 里，但创建 `Library` 的**入口**（`LibraryExt` trait）却在 `typst` crate 里。这种「定义在底层、装配在顶层」的切分，正是 u3-l2 讲过的「crate 切分 + 依赖反转」的又一次体现——下面会展开。

---

## 4. 核心概念与源码讲解

### 4.1 Library 结构体：标准库的「全家桶」

#### 4.1.1 概念说明

在 u1-l2 中我们知道，编译器通过 `world.library()` 拿到一个 `&LazyHash<Library>`。那么这个 `Library` 里到底装了什么？

可以把 `Library` 想象成编译器每次开工前要领的「工具箱」：

- `routines`：一张函数指针表（u3-l2 已讲），让底层库能反向调用 layout/realize/html 等算法——这是「能干什么活」的能力。
- `global` / `math`：两个 `Module`，分别是普通模式和数学模式下「有哪些名字可用」的查表来源——这是「认识哪些词」的词典。
- `styles`：默认样式（页面大小、默认字体等）——这是「默认怎么排」的偏好。
- `rules`：内置 show 规则——这是「某些元素默认怎么呈现」的预设。
- `std`：把 `global` 整体再包成一个值，供脚本里 `import "std"` 用。
- `features`：本次构建开启了哪些实验特性——这是「开关状态」的备忘。

#### 4.1.2 核心流程

`Library` 本身只是一个 `#[derive(Clone, Hash)]` 的纯数据结构，它不自己装配自己，而是由 `LibraryBuilder::build()` 一次性填好全部 7 个字段。装配的数据流如下（伪代码）：

```
build()
 ├─ math      ← math::module()                  // 独立先建数学模块
 ├─ inputs    ← self.inputs.unwrap_or_default()  // 解析 sys.inputs
 ├─ global    ← global(routines, math, inputs, features)  // 串八个 define
 └─ Library {
        routines,                               // 透传
        global: global.clone(),                 // global 同时进 std
        math,
        styles: Styles::new(),                  // 空样式，由调用方后续 .set()
        rules: (routines.rules)(),              // 调函数指针拿内置 show 规则
        std: Binding::detached(global),         // global 包成值
        features,                               // 透传开关
    }
```

注意一个细节：`global` 被 `clone()` 了两次用途——一份作为 `Library.global` 字段（顶层查表），一份被 `Binding::detached` 包成 `Library.std`（作为可被 `import` 的值）。两者内容一致，只是「身份」不同。

#### 4.1.3 源码精读

`Library` 结构体定义在 typst-library，标注了 `#[non_exhaustive]`（未来可能加字段，外部不得用结构体字面量构造它），且派生了 `Hash`（因为 comemo 增量缓存要以它作为失效判定依据之一）：

```rust
// crates/typst-library/src/lib.rs
#[derive(Debug, Clone, Hash)]
#[non_exhaustive]
pub struct Library {
    pub routines: &'static Routines,
    pub global: Module,
    pub math: Module,
    pub styles: Styles,
    pub rules: NativeRuleMap,
    pub std: Binding,
    pub features: Features,
}
```

字段逐条说明见 [crates/typst-library/src/lib.rs:L164-L183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L164-L183)。

两个字段值得特别点出：

1. `routines: &'static Routines`：是**静态生命周期引用**。回忆 u3-l2，`Routines` 是进程级单例 `ROUTINES`，`Library` 只持有它的引用而不拥有它，因此 `Library` 的克隆/哈希都不涉及函数指针本身（`Routines` 的 `Hash` 实现为空，正是为此）。
2. `features: Features`：把开关状态也存进 `Library`。这一点很重要——装配阶段（`global()`）会读它来决定注册什么，而编译阶段（`compile_impl`）也会读它来做目标门控（见 4.4）。把开关「随身携带」在 `Library` 里，是为了让两处都能拿到。

#### 4.1.4 代码实践

**实践目标**：建立「`Library` 字段 ↔ 它服务的编译阶段」的对应关系。

**操作步骤**：

1. 打开 [crates/typst-library/src/lib.rs:L164-L183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L164-L183)，对照结构体定义。
2. 回忆 u2-l1 的 `compile_impl`：第一行就是 `let library = world.library();`。在 [crates/typst/src/lib.rs:L104-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104-L113) 你会看到它随后用到 `library.styles`（装配样式链）和 `library.features`（目标门控）。
3. 在 [crates/typst/src/lib.rs:L123-L131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L123-L131) 看到 `typst_eval::eval(... library ...)`，求值器会用到 `library.global` / `library.math` 来查名字。

**需要观察的现象 / 预期结果**：自己画一张表，把 7 个字段填到「谁在编译流程中读它」一列。参考答案：

| 字段 | 读取方 | 读取时机 |
| --- | --- | --- |
| `routines` | 求值/布局各处经 `engine.library.routines.X(...)` | 求值、realize、布局全程 |
| `global` | 求值器查顶层标识符 | 求值 |
| `math` | 求值器查数学模式标识符 | 求值（数学模式） |
| `styles` | `compile_impl` 装配 `StyleChain` | 布局前 |
| `rules` | realize 阶段应用内置 show 规则 | realize |
| `std` | 脚本里 `import "std"` | 求值 |
| `features` | `compile_impl` 目标门控 + `global()` 装配 | 装配期 + 编译期 |

（以上对应关系是「源码阅读型」结论，部分读取点的精确行号可自行 `grep` 验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Library` 要派生 `Hash`？

**参考答案**：`World::library()` 返回的 `Library` 被包在 `LazyHash<Library>` 里，且 comemo 的增量缓存需要根据输入是否变化来决定缓存命中与否。派生 `Hash`（配合 `LazyHash` 的记忆化哈希）让编译器能快速比较两次编译的 `Library` 是否一致，从而复用缓存。

**练习 2**：`std` 字段和 `global` 字段内容相同，为什么还要分两个？

**参考答案**：`global` 是「作用域 / 模块」语义，供求值器在顶层查名字；`std` 是「值」语义（`Binding::detached(global)`），目的是让 `std` 本身也能被脚本当作一个普通值来 `import` 和引用。两者身份不同，所以分别存放。

---

### 4.2 LibraryExt 与 LibraryBuilder：建造者模式

#### 4.2.1 概念说明

`Library` 标了 `#[non_exhaustive]`，外部不能直接 `Library { ... }` 构造它。官方提供两个入口：

- `Library::default()`：开箱即用的默认配置（不开启任何实验特性、无自定义 inputs）。
- `Library::builder()...build()`：想要自定义时用，可以链式设置 `inputs` 和 `features`。

这两个入口都来自一个 trait `LibraryExt`。有意思的是，**这个 trait 不在定义 `Library` 的 `typst-library` 里，而在 `typst` crate 里**。原因是：`builder()` 内部要把进程级单例 `ROUTINES`（u3-l2 讲过，它住在 `typst` crate）传给 `LibraryBuilder`。`typst-library` 不能引用 `ROUTINES`（否则又会形成循环依赖），所以「创建 `Library` 的便捷入口」自然只能由依赖所有人的顶层 `typst` crate 来提供。

`LibraryBuilder` 是典型的建造者：

- `from_routines(routines)`：内部构造方法（`#[doc(hidden)]`，不对外），把 `routines` 钉死成 `&'static`，`inputs` / `features` 先置默认。
- `with_inputs(dict)`：把 `sys.inputs` 注入。
- `with_features(features)`：把实验特性开关注入。
- `build()`：消费 builder，执行 4.1.2 描述的装配流程，产出 `Library`。

#### 4.2.2 核心流程

从「外部调用」到「`Library` 落地」的完整链路：

```
Library::default()                          // 用户调用（typst crate）
   └─ Self::builder().build()
        ├─ builder() = LibraryBuilder::from_routines(&ROUTINES)   // 装配 ROUTINES
        │     fields: { routines: &ROUTINES, inputs: None, features: default }
        └─ build()                          // typst-library 里
              ├─ math = math::module()
              ├─ inputs = self.inputs.unwrap_or_default()
              ├─ global = global(routines, math, inputs, &features)
              └─ Library { ... 7 个字段 ... }
```

注意 `LibraryExt` 里 `default()` 就是 `builder().build()` 的语法糖——也就是说**默认库 = builder 的零配置产物**。

#### 4.2.3 源码精读

`LibraryExt` trait 及其实现完全在 `typst` crate，是门面层的「装配点」：

```rust
// crates/typst/src/lib.rs
pub trait LibraryExt {
    fn default() -> Library;
    fn builder() -> LibraryBuilder;
}

impl LibraryExt for Library {
    fn default() -> Library {
        Self::builder().build()
    }
    fn builder() -> LibraryBuilder {
        LibraryBuilder::from_routines(&ROUTINES)   // ← 把 typst 的 ROUTINES 接进来
    }
}
```

见 [crates/typst/src/lib.rs:L288-L305](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L288-L305)。这一行 `from_routines(&ROUTINES)` 就是 u3-l2 所述「装配」动作的落点——`ROUTINES` 静态量在此刻被注入 `LibraryBuilder`，最终存为 `Library.routines`。

`LibraryBuilder` 的本体则在 `typst-library`：

```rust
// crates/typst-library/src/lib.rs
pub struct LibraryBuilder {
    routines: &'static Routines,
    inputs: Option<Dict>,
    features: Features,
}
```

四个方法见 [crates/typst-library/src/lib.rs:L185-L235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L185-L235)，其中最关键的是 `build()`：

```rust
// crates/typst-library/src/lib.rs:L220-L234
pub fn build(self) -> Library {
    let math = math::module();
    let inputs = self.inputs.unwrap_or_default();
    let global = global(self.routines, math.clone(), inputs, &self.features);
    Library {
        routines: self.routines,
        global: global.clone(),
        math,
        styles: Styles::new(),
        rules: (self.routines.rules)(),   // 调函数指针：注册 layout/html 的内置 show 规则
        std: Binding::detached(global),
        features: self.features,
    }
}
```

两个要点：

- `rules: (self.routines.rules)()`——这里的 `self.routines.rules` 是 u3-l2 讲过的函数指针字段。它指向 [crates/typst/src/lib.rs:L312-L317](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L312-L317) 里的闭包，该闭包新建一个 `NativeRuleMap` 并调用 `typst_layout::register` 和 `typst_html::register` 把内置 show 规则装进去。**这是「函数指针表」被实际调用、把真实算法接进来的又一处现场。**
- `styles: Styles::new()` 是空的。调用方拿到 `Library` 后可以再 `lib.styles.set(...)` 修改默认样式——`typst-ide` 的测试就这么做（见 4.2.4）。

#### 4.2.4 代码实践

**实践目标**：读懂真实的 `Library::builder()...build()` 调用，并理解每一环在配什么。

**操作步骤**：

1. 阅读 CLI 的真实装配代码 [crates/typst-cli/src/world.rs:L56-L68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L56-L68)：先把命令行 `--input key=val` 收集成 `Dict`，再把 CLI 自己的 `Feature` 枚举 `From` 转换成 `typst::Feature` 并 `collect()` 成 `Features`，最后 `Library::builder().with_inputs(inputs).with_features(features).build()`。
2. 阅读另一个用例 [crates/typst-ide/src/tests.rs:L178-L180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-ide/src/tests.rs#L178-L180)：`typst::Library::builder().with_features(Features::all()).build()`，随后 `lib.styles.set(PageElem::width, ...)`——这就是「`build()` 后再改默认样式」的真实例子。
3. 对照 4.2.3 的 `build()` 源码，在心里逐步推演：调用方的 `inputs` 如何变成 `sys` 模块内容（见 4.3.3），`features` 如何既影响 `global()` 又被存进 `Library.features`。

**需要观察的现象 / 预期结果**：你能用一句话说清「`with_inputs` 的 `Dict` 最终去了哪」——答案是它被传给 `global()` → `foundations::define(&mut global, inputs)` → `sys::module(inputs)`，成为 `sys.inputs` 的内容（详见 4.3.3 的源码引用）。

**预期结果**：待本地验证（若你想确认 CLI 的 `--input` 真的出现在 `sys.inputs`，可在脚本里 `#context sys.inputs` 打印）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LibraryExt` 定义在 `typst` crate 而不是 `typst-library`？

**参考答案**：因为 `builder()` 需要引用 `&ROUTINES` 这个进程级静态量，而 `ROUTINES` 住在 `typst` crate（它同时依赖 layout/html/realize/eval，才能把这些算法填进函数指针表）。`typst-library` 是底层，不能反过来依赖 `typst`，所以装配入口只能上移到顶层门面。

**练习 2**：`from_routines` 为什么标了 `#[doc(hidden)]`？

**参考答案**：它是 builder 的内部构造方法，要求传入 `&'static Routines`，而 `ROUTINES` 是 `typst` crate 的私有静态量。外部用户不应该直接拿到 `Routines` 引用，应通过 `LibraryExt::builder()` 间接使用，故隐藏。

---

### 4.3 global() 模块装配与 prelude 预设

#### 4.3.1 概念说明

`global()` 是装配流程里最「实」的一步：它把八个 category 子模块的 `define` 函数逐一调用，把几百个函数、元素、类型塞进一个 `Scope`，再包成 `Module::new("global", ...)`。

理解 `global()` 要先理解三个底层约定：

1. **`Scope::deduplicating()`**：创建一个「允许同名后定义覆盖前定义」的作用域。之所以用去重作用域，是因为不同子模块可能（在实验特性开关切换时）重复注册同名项，去重作用域让后注册的生效而不会 panic。
2. **`start_category(C)` / `reset_category()`**：是一对「夹板」。在两者之间注册的所有值，都会被打上分类 `C` 的标签（写入 `Scope.category`）。这对夹板纯粹是给文档分组用的元数据。
3. **每个子模块都暴露一个自由函数 `define(&mut Scope, ...)`**（数学模块除外，它是 `math::module()` 返回独立 `Module`）。`define` 内部就是一连串 `global.define_type::<...>()` / `global.define_elem::<...>()` / `global.define_func::<...>()`。

`prelude()` 则负责那些「全局可见、无需模块前缀」的预设值：颜色常量（`red`/`blue`…）、颜色构造器（`rgb`/`oklch`…）、`range`、方向（`ltr`/`rtl`…）、对齐（`left`/`center`…）。它们其实也被注册进同一个 `global` 作用域，只是语义上是「预设」。

#### 4.3.2 核心流程

`global()` 的执行顺序（伪代码）：

```
global(routines, math, inputs, features)
 ├─ scope = Scope::deduplicating()
 ├─ foundations::define(&scope, inputs)   // 含 sys.inputs
 ├─ model::define(&scope, features)       // 可能注册 AssetElem（受 Bundle 门控）
 ├─ text::define(&scope)
 ├─ layout::define(&scope)
 ├─ visualize::define(&scope)
 ├─ introspection::define(&scope)
 ├─ loading::define(&scope)
 ├─ symbols::define(&scope)
 ├─ scope.define("math", math)            // 数学模块作为子模块挂进来
 ├─ scope.define("pdf", pdf::module(features))   // 可能注册 a11y 函数（受 A11yExtras 门控）
 ├─ if features.is_enabled(Html) { scope.define("html", (routines.html_module)()) }  // 条件注册
 ├─ prelude(&mut scope)                   // 注入颜色/方向/对齐等预设
 └─ Module::new("global", scope)
```

两个观察：

- **math 是「模块」而非「define 函数」**：因为数学模式有自己独立的作用域（在数学模式里 `sum`、`alpha` 直接可见，普通模式要 `math.alpha`），所以它单独建一个 `Module`，再被 `define("math", ...)` 挂进全局。
- **`pdf` 和 `html` 是条件性的子模块**：`pdf` 模块总是注册（但其内容受 `A11yExtras` 影响），`html` 模块只在开启 `Feature::Html` 时才注册——这就是「不开启就连名字都查不到」的实现。

#### 4.3.3 源码精读

`global()` 全文见 [crates/typst-library/src/lib.rs:L328-L355](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L328-L355)。最值得逐行看的是这一段：

```rust
// crates/typst-library/src/lib.rs:L335-L354
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
```

注意只有 `foundations::define` 收 `inputs`、只有 `model::define` 收 `features`——这两个参数是「按需下传」，不是每个 define 都要。来验证它们如何使用：

- `foundations::define` 在内部把 `inputs` 传给 `sys::module(inputs)`，使 `--input` 的键值对成为 `sys.inputs`：见 [crates/typst-library/src/foundations/mod.rs:L91-L123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L91-L123)，其中 [L121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L121) 的 `global.define("sys", sys::module(inputs))` 是落点。同时它还示范了 `start_category(Foundations)` / `reset_category()` 夹板用法（[L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L92) 与 [L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L122)）。
- `model::define` 用 `features` 门控 `AssetElem`：见 [crates/typst-library/src/model/mod.rs:L53-L80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L53-L80)，关键行 [L56-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L56-L58)：
  ```rust
  if features.is_enabled(Feature::Bundle) {
      global.define_elem::<AssetElem>();
  }
  ```

底层 `start_category` / `reset_category` / `deduplicating` 的实现非常朴素（只是设置/清空一个 `Option<Category>` 字段）：见 [crates/typst-library/src/foundations/scope.rs:L121-L133](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L121-L133)。

`prelude()` 见 [crates/typst-library/src/lib.rs:L357-L395](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L357-L395)。它把颜色常量、颜色构造器（`luma`/`oklab`/`oklch`/`rgb`/`cmyk`）、`range`、四个方向（`ltr`/`rtl`/`ttb`/`btt`）、八个对齐常量直接 `define` 进全局作用域。注意这些值都来自具体类型的数据方法（如 `Color::rgb_data()`），而非字面量——它们是被「导出」为脚本可调用形式的 Rust 函数/常量。

#### 4.3.4 代码实践

**实践目标**：跟踪一个标识符从「被注册」到「被脚本使用」的全过程。

**操作步骤**：

1. 在 [crates/typst-library/src/model/mod.rs:L67-L68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L67-L68) 找到 `global.define_elem::<HeadingElem>();`——这是 `heading` 函数被注册的位置。
2. 回溯调用链：`global()` 调 `model::define`（[L338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L338)）→ `model::define` 调 `define_elem::<HeadingElem>` → 该绑定落入 `global` 作用域 → 包成 `Module` → 存为 `Library.global`。
3. 写一行 Typst 脚本 `#heading[Hello]`，理解求值器正是到 `Library.global` 这张表里查到 `heading` 这个名字，再调用对应的元素构造器。

**需要观察的现象 / 预期结果**：你能画出 `HeadingElem`（Rust 类型）→ `global` 作用域的一个条目 → 脚本里的 `heading` 标识符 这条三段式映射。

**预期结果**：源码阅读型实践，无需运行；若想验证，可在本地用 `typst query` 或 debugger 观察求值时 `Scope` 的查找命中。

#### 4.3.5 小练习与答案

**练习 1**：`global()` 里为什么用 `Scope::deduplicating()` 而不是普通 `Scope`？

**参考答案**：因为某些注册可能是「条件性重复」的（例如在不同 feature 组合下，同一名字可能被注册多次，或实验特性注册了与默认同名的项）。去重作用域让后注册的定义覆盖前者而不是触发「重复定义」错误，保证装配健壮。

**练习 2**：`math` 为什么不像其他七个 category 那样用 `define` 函数，而是用 `math::module()` 返回独立 `Module`？

**参考答案**：数学模式有独立的作用域语义——数学模式下的标识符（如 `alpha`、`sum`）默认在数学模式可见，在普通模式必须加 `math.` 前缀。因此它需要自己独立的 `Scope`，被包成独立 `Module`，再以 `define("math", math)` 挂为全局的子模块。普通 category 的内容则是无条件全局可见的，所以直接往同一个 `global` 作用域里塞即可。

---

### 4.4 Features / Feature：特性开关与条件注册

#### 4.4.1 概念说明

`Feature` 是一个 `#[non_exhaustive]` 枚举，目前有三个变体，代表三项仍在开发中的实验能力：

- `Feature::Html`：HTML 导出。
- `Feature::Bundle`：bundle（资源打包）相关。
- `Feature::A11yExtras`：PDF 无障碍增强（accessibility extras）。

`Features` 则是这些开关的「集合容器」。它的底层是 `SmallBitSet`——一个小型位集，每个 `Feature` 被转成 `usize` 当作 bit 位下标。这种设计的好处是：

- `Features` 之间可以由迭代器 `collect()` 而成（实现了 `FromIterator<Feature>`）。
- 判定某个开关是否开启只需一次位测试 `set.contains(feature as usize)`，极廉价。
- 作为 `Library` 的字段，它也派生了 `Hash`，参与 comemo 缓存失效判定。

#### 4.4.2 核心流程

开关从「命令行」到「影响注册」的完整数据流：

```
CLI: typst compile --features html
  └─ process_args.features: [Feature::Html]   // CLI 自己的 enum (typst-cli)
       └─ .map(Into::into)                     // 转成 typst::Feature
            └─ .collect::<Features>()          // FromIterator，构造位集
                 └─ Library::builder().with_features(features)
                      └─ build() 存入 Library.features
                           ├─ global() 读 features：if Html => 注册 html 模块
                           ├─ model::define 读 features：if Bundle => 注册 AssetElem
                           ├─ pdf::module 读 features：if A11yExtras => 注册 a11y 函数
                           └─ compile_impl 读 features：Html/Bundle 目标门控（warn/error）
```

一个关键认知：**同一个 `features` 在「装配期」（决定注册什么）和「编译期」（决定目标是否允许）被读了两次**。装配期决定「词典里有没有这个词」，编译期决定「这个词能不能用」。两者必须一致，靠的就是把 `features` 随身存在 `Library` 里。

#### 4.4.3 源码精读

`Features` 与 `Feature` 定义见 [crates/typst-library/src/lib.rs:L237-L284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L237-L284)：

```rust
// crates/typst-library/src/lib.rs:L240-L241
#[derive(Debug, Default, Clone, Hash)]
pub struct Features(SmallBitSet);

// L271-L277
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
#[non_exhaustive]
pub enum Feature {
    Html,
    Bundle,
    A11yExtras,
}
```

三个常用方法：

- `Features::all()`（[L245-L247](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L245-L247)）：全开，等于 `Feature::all().collect()`。
- `Features::none()`（[L249-L252](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L249-L252)）：全关，等于 `default()`。
- `is_enabled(feature)`（[L254-L257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L254-L257)）：位测试。

`FromIterator<Feature>` 把枚举值映射到位下标（[L260-L268](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L260-L268)）：`set.insert(feature as usize)`。

现在看三个 `Feature` 各自门控了什么：

**① `Feature::Html` → 注册 `html` 子模块**（在 `global()` 里）：

```rust
// crates/typst-library/src/lib.rs:L348-L350
if features.is_enabled(Feature::Html) {
    global.define("html", (routines.html_module)());
}
```

注意这里又出现 `(routines.html_module)()`——调用 u3-l2 讲过的函数指针，它指向 `typst_html::module`。**未开启 Html 时，`html` 这个名字根本不在作用域里**，脚本里写 `html.frame` 会直接报「未知标识符」。

**② `Feature::Bundle` → 注册 `AssetElem`**（在 `model::define` 里）：

```rust
// crates/typst-library/src/model/mod.rs:L56-L58
if features.is_enabled(Feature::Bundle) {
    global.define_elem::<AssetElem>();
}
```

**③ `Feature::A11yExtras` → 注册 PDF 无障碍函数**（在 `pdf::module` 里）：

```rust
// crates/typst-library/src/pdf/mod.rs:L18-L22
if features.is_enabled(Feature::A11yExtras) {
    pdf.define_func::<table_summary>();
    pdf.define_func::<header_cell>();
    pdf.define_func::<data_cell>();
}
```

最后看「编译期」的第二处读取——目标门控。`compile_impl` 一进来就按 `T::target()` 分支（u3-l1 已讲），对 `Html`/`Bundle` 目标调用门控函数，这些函数正是读 `library.features`：

```rust
// crates/typst/src/lib.rs:L105-L109
match T::target() {
    Target::Paged => {}
    Target::Html => warn_or_error_for_html(&library.features, sink)?,
    Target::Bundle => warn_or_error_for_bundle(&library.features, sink)?,
}
```

以 [crates/typst/src/lib.rs:L247-L266](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L266) 的 `warn_or_error_for_html` 为例：开启 Html 时只发一条「实验性、不保证稳定」的**警告**（`sink.warn`），未开启时则 `bail!` 成**致命错误**。`warn_or_error_for_bundle`（[L270-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L270-L286)）同理。

CLI 把自己的 `Feature` 枚举映射到 `typst::Feature` 的代码见 [crates/typst-cli/src/world.rs:L349-L357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L349-L357)，CLI 枚举本身见 [crates/typst-cli/src/args.rs:L646-L652](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L646-L652)。这种「CLI 自己定义一份枚举再 `From` 转换」是为了让 CLI 层（依赖 `clap` 的 `ValueEnum`）与核心库解耦。

#### 4.4.4 代码实践

**实践目标**：亲手写一段 `Library::builder()...build()` 配置，并预测「开启 `Feature::Html` 后 global 模块多出什么」。

**操作步骤**：

1. 对照 [crates/typst-cli/src/world.rs:L64-L67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L64-L67) 的真实写法，下面是一段等价的示例代码（**示例代码**，便于理解，非仓库原文件）：

   ```rust
   use typst::LibraryExt;
   use typst_library::{Feature, Features};

   // 1) 只开 HTML
   let lib = Library::builder()
       .with_features([Feature::Html].into_iter().collect::<Features>())
       .build();

   // 2) 传入自定义 sys.inputs 并全开特性
   let mut inputs = typst_library::Dict::new();
   inputs.insert("version".into(), "1.2".into());
   let lib2 = Library::builder()
       .with_inputs(inputs)
       .with_features(Features::all())
       .build();
   ```

   说明：`[Feature::Html].into_iter().collect::<Features>()` 利用的就是 `FromIterator<Feature>`；`Features::all()` 等价于三个特性全开。

2. 推演开启 `Feature::Html` 后 `global` 模块发生的变化：
   - `global()` 执行到 [L348-L350](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L348-L350) 时 `is_enabled(Html)` 为真，于是 `global.define("html", (routines.html_module)())` 被执行——`global` 作用域里多出一个名为 `html` 的子模块（其内容由 `typst_html::module` 提供）。
   - 由于 `Library.std = Binding::detached(global.clone())`，`std` 也同步包含 `html`。
   - 编译期若以 `compile::<HtmlDocument>` 调用，`warn_or_error_for_html` 因 Html 已开，只发警告不报错。

**需要观察的现象 / 预期结果**：未开启 Html 时，脚本里 `html.frame` 会因查不到 `html` 标识符而报错；开启后该标识符可用，且 HTML 导出会附带一条实验性警告。

**预期结果**：可在本地用 `typst compile --features html file.typ`（结合 HTML 后端）验证 `html` 标识符可用性与那条警告；若手写 builder 代码，待本地验证（需自建 `World` 实现，参考 u1-l2）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Features` 用位集（`SmallBitSet`）而不是 `HashSet<Feature>`？

**参考答案**：`Feature` 变体很少（目前 3 个），位集可用极少的内存表示全部组合，且判定 `is_enabled` 是一次位运算，比哈希集合的查找更廉价；同时位集天然支持 `Hash` 派生，便于作为 `Library` 字段参与 comemo 缓存失效判定。

**练习 2**：假设有人开 `Feature::Html` 但用 `compile::<PagedDocument>` 编译，会发生什么？

**参考答案**：装配期 `global()` 仍会注册 `html` 子模块（脚本里 `html.*` 可用）；编译期 `compile_impl` 走 `Target::Paged` 空臂，`warn_or_error_for_html` 根本不被调用，所以不会因 Html 开启而报错或警告（那条警告只在 `compile::<HtmlDocument>` 时才发）。即「开了 Html 不强制你导出 HTML」，两者解耦。

---

## 5. 综合实践

**任务**：本讲贯穿性任务——「补全一份 `Library` 装配说明书」。

请完成以下步骤，把本讲四个模块串起来：

1. **画出装配时序**。以 `Library::builder().with_inputs(inputs).with_features(Features::all()).build()` 为输入，画出从 `LibraryExt::builder()`（[typst/src/lib.rs:L302-L304](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L302-L304)）到 `build()`（[typst-library/src/lib.rs:L220-L234](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L220-L234)）再到 `global()`（[L328-L355](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L328-L355)）的调用时序，标注每一步把哪个数据传给谁。

2. **填一张「特性 ↔ 注册点」对照表**。对 `Feature::Html`、`Feature::Bundle`、`Feature::A11yExtras` 三个开关，分别填出：门控发生在哪个文件/函数、被门控的具体注册项、未开启时的用户可见后果。参考答案骨架（请自行补全文件与行号）：

   | Feature | 门控位置 | 被门控的注册项 | 未开启的后果 |
   | --- | --- | --- | --- |
   | Html | `global()` | `html` 子模块 | `html.*` 标识符不可用 |
   | Bundle | `model::define` | `AssetElem` | `asset` 元素不可用 |
   | A11yExtras | `pdf::module` | `table_summary`/`header_cell`/`data_cell` | 这三个 PDF 函数不可用 |

3. **解释一个设计取舍**：为什么 `features` 既要驱动装配期注册，又要存在 `Library` 里供编译期 `compile_impl` 读取？如果只在装配期用一次、不存进 `Library`，会出什么问题？

   **参考思路**：装配期决定「词典里有哪些词」，编译期还要决定「这些实验目标是否被允许使用」（warn 还是 error）。若不存 `Library.features`，`compile_impl` 就无法知道当前是否开启了 Html/Bundle，目标门控（`warn_or_error_for_*`）将无从读开关，实验性导出的「软警告 / 硬报错」分级就无法实现。把开关随身携带，正是为了让这两处判定基于同一份真相。

完成本任务后，你应该能向别人讲清楚：「我给 Typst 加一个新实验特性 `Foo`，需要在 `Feature` 枚举、`global()` 或某个 `define`、以及对应的 `warn_or_error_for_*` 三处分别动什么」。

## 6. 本讲小结

- `Library` 是 Typst 运行时的「全家桶」，含 7 个字段：`routines`（函数指针表）、`global`/`math`（查表词典）、`styles`（默认样式）、`rules`（内置 show 规则）、`std`（global 的值化包装）、`features`（开关备忘）。
- 创建 `Library` 走建造者模式：`LibraryExt::default()` 是 `builder().build()` 的语法糖；`LibraryExt` 特意定义在 `typst` crate，因为 `builder()` 要把本 crate 的 `ROUTINES` 静态量装配进去。
- `build()` 的装配核心是 `global()`：它用去重作用域，依次调用八个 category 的 `define` 函数，再挂上 `math`/`pdf`/（条件性的）`html` 子模块，最后 `prelude()` 注入颜色、方向、对齐等预设。
- category 装配靠 `start_category` / `reset_category` 夹板给每个值打文档分组标签，是元数据而非功能性。
- `Features` 是基于 `SmallBitSet` 的位集，`Feature` 枚举目前有 `Html`/`Bundle`/`A11yExtras` 三项；同一份 `features` 在装配期（决定注册什么）和编译期（`compile_impl` 的目标门控）被读两次。
- 三个特性各自门控：Html→`html` 子模块、Bundle→`AssetElem`、A11yExtras→PDF 无障碍函数；未开启时对应标识符根本不在作用域。

## 7. 下一步学习建议

- **继续向下钻「装配」的终点**：本讲的 `build()` 调用了 `(self.routines.rules)()`、`(routines.html_module)()` 等函数指针。建议结合 u3-l2，去读 `typst-layout`、`typst-html` 里 `register` / `module` 的实现，看「被装配进来的真实算法」长什么样。
- **看 `Scope` 的更多能力**：本讲只用到 `define` / `define_elem` / `define_func` / `start_category`。可以读 [crates/typst-library/src/foundations/scope.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs)，了解作用域的「分类、常量、函数、捕获（scoped values）」全貌，对求值期的名字查找会有更深理解。
- **顺延到 u3-l4 诊断处理**：本讲提到的 `warn_or_error_for_html` 是「按 feature 决定 warn 还是 error」的典型，u3-l4 会系统讲 `deduplicate` 去重、延迟错误提升、`hint_invalid_main_file` 友好提示等诊断机制，可与本讲的「特性门控告警」对照阅读。
