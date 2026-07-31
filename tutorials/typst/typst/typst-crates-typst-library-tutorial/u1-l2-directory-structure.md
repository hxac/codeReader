# 目录结构与模块导览

## 1. 本讲目标

上一讲我们建立了 `typst-library` 在编译器生态中的整体定位：它既是 Typst 的标准库，又集中了编译器的核心类型定义。本讲要把这副「全景图」落到具体的目录和代码上。

学完本讲，你应该能够：

- 说出 `src/` 下的 13 个顶层模块各自负责什么，并能区分「标准库内容模块」与「编译器基础设施模块」。
- 看懂 `lib.rs` 中的 `Category` 枚举，理解它为何有 14 个变体、以及它们如何（并不总是一一对应地）映射到目录。
- 跟着 `global()` 函数走一遍标准库的装配流水线，理解「直接注入全局作用域」与「先构建子模块再挂载」两种注册方式的差别。

## 2. 前置知识

在阅读本讲前，你需要先建立下面几个直觉（上一讲已经覆盖）：

- **crate 与模块**：Rust 中一个 crate 是一个编译单元，模块（`mod`）是 crate 内部的代码组织单位。`typst-library` 是一个 crate，它内部又拆成许多模块。
- **标准库 = 一堆命名定义**：从 Typst 使用者的角度看，标准库就是 `#heading(...)`、`#text(...)`、`#calc.add(...)` 这样一批可以调用的名字。这些名字在 Rust 侧被组织进一个个 `Scope`（作用域）。
- **「类型」与「行为」分离**：类型定义（`Value`、`Content` 等）留在本 crate；求值、收敛、排版等「行为」拆到了 `typst-eval`、`typst-realize`、`typst-layout` 等别的 crate。
- **`Category` 是分类标签**：Typst 文档站把标准库按类别分组展示（如 foundations、text、math），这个分组就是 `Category`。

如果你还不清楚 `World` / `Library` / `Engine` 三者的关系，建议先复习上一讲（u1-l1）。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以若干子模块的入口函数。

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | crate 根：模块声明、`World` trait、`Library`/`LibraryBuilder`、`Category` 枚举、`global()` 装配函数、`prelude()` 常量注入 |
| [src/foundations/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs) | foundations 模块入口，包含 `define()` 注册函数 |
| [src/layout/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs) | layout 模块入口，包含 `define()` 注册函数（代表「直接注入」这一类） |
| [src/math/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs) | math 模块入口，包含 `module()`（代表「先构建子模块」这一类） |
| [src/symbols.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/symbols.rs) | 符号模块入口，含 `define()` 与 `define_math()` |
| [src/pdf/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/mod.rs) | pdf 模块入口，含 `module(features)` |

> 阅读建议：先看 `lib.rs` 的三块——顶部 `pub mod` 列表、`Category` 枚举、`global()` 函数。它们正好对应本讲的三个最小模块。

## 4. 核心概念与源码讲解

### 4.1 模块声明：13 个 `pub mod` 与「内容 / 基础设施」的划分

#### 4.1.1 概念说明

打开 crate 根文件 `src/lib.rs`，最先看到的就是一连串 `pub mod` 声明。这些声明告诉 Rust：「这个 crate 内部由这些子模块组成」。每一个 `pub mod xxx;` 通常对应磁盘上的 `src/xxx/mod.rs`（或 `src/xxx.rs`）。

关键在于：**并不是所有顶层模块都是「标准库内容」**。本 crate 身兼两职，所以 13 个顶层模块可以干净地分成两类：

- **标准库内容模块**（10 个）：会被装配进用户可见的标准库作用域，对应 `#heading`、`#text`、`#calc` 这些用户能调用的名字。
- **编译器基础设施模块**（3 个）：`diag`（诊断）、`engine`（编译上下文）、`routines`（函数指针表）。它们提供类型和机制给编译器自己用，**不会**作为「标准库定义」注册进全局作用域。

#### 4.1.2 核心流程

判定一个模块属于哪一类，可以用一个简单流程：

```text
看 lib.rs 的 pub mod 列表
        │
        ├── 在 global() 里被 define / module 调用？ ── 是 ──▶ 标准库内容模块
        │
        └── 否 ──▶ 编译器基础设施模块（diag / engine / routines）
```

#### 4.1.3 源码精读

13 个顶层模块声明集中在 [src/lib.rs:15-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L15-L27)，这部分代码列出 crate 的全部公开子模块：

```rust
pub mod diag;
pub mod engine;
pub mod foundations;
pub mod introspection;
pub mod layout;
pub mod loading;
pub mod math;
pub mod model;
pub mod pdf;
pub mod routines;
pub mod symbols;
pub mod text;
pub mod visualize;
```

对照分组如下：

| 模块 | 类别 | 说明 |
| --- | --- | --- |
| `foundations` | 内容 | 基础类型与函数（`Value`、`Content`、`calc`、`sys` 等） |
| `model` | 内容 | 文档模型（标题、列表、段落、图表、目录等） |
| `text` | 内容 | 文本与字体（`TextElem`、字体管理、断行等） |
| `layout` | 内容 | 布局（页面、栈、网格、度量与几何） |
| `visualize` | 内容 | 可视化（颜色、描边、形状、图像） |
| `introspection` | 内容 | 内省（`query`、`counter`、`state`、定位） |
| `loading` | 内容 | 数据加载（`csv`、`json`、`yaml` 等） |
| `symbols` | 内容 | 可变符号（注入 `sym` 命名空间） |
| `math` | 内容 | 数学公式（注册为 `math` 子模块） |
| `pdf` | 内容 | PDF 相关用户定义（注册为 `pdf` 子模块） |
| `diag` | **基础设施** | 诊断类型与 `bail!`/`error!` 宏 |
| `engine` | **基础设施** | 编译上下文 `Engine`、`Route`、`Sink` |
| `routines` | **基础设施** | 跨 crate 回调的函数指针表 `Routines` |

> 这张表是本讲最重要的「地图」。后面所有讲义都会反复回到这几个目录。

#### 4.1.4 代码实践

**实践目标**：亲手核对「目录 ↔ 模块声明」的对应关系，并验证三个基础设施模块确实不参与标准库注册。

**操作步骤**：

1. 在仓库根目录列出 `src/` 下的子目录与文件，观察每个内容模块都有对应目录或文件：
   ```bash
   ls crates/typst-library/src
   ```
2. 用 `grep` 找出所有被 `global()` 调用的注册函数，确认内容模块各有 `define` 或 `module`：
   ```bash
   grep -rn "fn define(\|fn module(" crates/typst-library/src/*/mod.rs crates/typst-library/src/symbols.rs
   ```
3. 在 `lib.rs` 的 `global()` 函数体里搜索 `diag`、`engine`、`routines`，确认它们**没有**作为定义被注册（`routines` 只是被作为参数传入，不是用户可见定义）。

**需要观察的现象**：

- 步骤 1 的输出里，10 个内容模块都有同名目录（`foundations/`、`model/` …），而 `diag.rs`、`engine.rs`、`routines.rs` 是单文件。
- 步骤 2 会列出 `foundations/mod.rs:91`、`layout/mod.rs:72`、`math/mod.rs:43` 等注册函数。

**预期结果**：你会得到一张与 4.1.3 表格一致的模块清单。

#### 4.1.5 小练习与答案

**练习 1**：`diag`、`engine`、`routines` 这三个模块为什么不算「标准库内容」？

> **答案**：它们提供的是编译器自身需要的类型与机制（错误诊断、编译上下文、跨 crate 回调），并不向 Typst 用户暴露可直接调用的标准库函数或元素，因此不会被注册进全局作用域。

**练习 2**：`src/foundations/` 目录下有 30 多个文件，但 `pub mod foundations;` 只有一行。这些子文件是如何被组织进 `foundations` 模块的？

> **答案**：通过 `foundations/mod.rs` 内部的 `mod array;`、`mod value;` 等私有/公开子模块声明，再配合 `pub use self::array::*;` 这样的重导出，把多个文件聚合为一个对外统一的 `foundations` 模块。

---

### 4.2 `Category` 枚举：标准库的分类标签

#### 4.2.1 概念说明

`Category` 是一个用来给标准库定义分类的枚举。它的作用体现在两处：

1. **文档站点分组**：Typst 官方文档把函数和元素按 Foundations / Text / Math 等类别展示，方便查阅。
2. **作用域标记**：在装配标准库时，每注册一组定义前会调用 `scope.start_category(...)` 打上当前类别标签，这样每个绑定（binding）都知道自己属于哪个分类。

一个容易踩的坑：**`Category` 变体数量（14 个）和顶层 `pub mod` 数量（13 个）并不相等**，也不是严格的一一对应。这是因为有些类别（如 `Svg`、`Png`）对应的输出行为根本不在本 crate 里，而 `DataLoading` 这个类别对应的是 `loading` 目录（名字不同）。

#### 4.2.2 核心流程

`Category` 的典型用法是「配对调用」：

```text
scope.start_category(Category::Xxx)   // 开始一个类别区段
    注册若干 define_type / define_elem / define_func ...
scope.reset_category()                 // 结束该区段
```

每个 `Category` 还通过 `name()` 方法给出 kebab-case 的字符串名，用于文档与序列化。

#### 4.2.3 源码精读

`Category` 枚举定义在 [src/lib.rs:289-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L289-L304)，它带有 `#[serde(rename_all = "kebab-case")]`：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Category {
    Foundations,
    Introspection,
    Layout,
    DataLoading,
    Math,
    Model,
    Symbols,
    Text,
    Visualize,
    Pdf,
    Html,
    Svg,
    Png,
    Bundle,
}
```

`name()` 方法把每个变体映射为字符串，见 [src/lib.rs:306-326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L306-L326)，例如 `DataLoading => "data-loading"`、`Foundations => "foundations"`。

把 14 个 `Category` 与模块对应起来，可以得到下表（注意其中 `Svg`/`Png`/`Bundle` 在本 crate 内**没有**对应的注册代码）：

| `Category` 变体 | `name()` | 对应目录/模块 | 在本 crate 中的注册 |
| --- | --- | --- | --- |
| `Foundations` | foundations | `foundations/` | `foundations::define` |
| `Model` | model | `model/` | `model::define` |
| `Text` | text | `text/` | `text::define` |
| `Layout` | layout | `layout/` | `layout::define` |
| `Visualize` | visualize | `visualize/` | `visualize::define` |
| `Introspection` | introspection | `introspection/` | `introspection::define` |
| `DataLoading` | data-loading | `loading/` | `loading::define` |
| `Symbols` | symbols | `symbols.rs` | `symbols::define` |
| `Math` | math | `math/` | `math::module`（构建为子模块） |
| `Pdf` | pdf | `pdf/` | `pdf::module`（构建为子模块） |
| `Html` | html | （不在本 crate，经 routine 注入） | 仅启用 `Feature::Html` 时注册 |
| `Svg` | svg | （输出行为在 `typst-svg` 等 crate） | 本 crate 无 |
| `Png` | png | （输出行为在 `typst-png` 等 crate） | 本 crate 无 |
| `Bundle` | bundle | （与 `Feature::Bundle` 相关） | 本 crate 无独立模块 |

#### 4.2.4 代码实践

**实践目标**：验证 `start_category` 与 `reset_category` 的配对使用。

**操作步骤**：

1. 阅读 [src/layout/mod.rs:72-100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs#L72-L100) 中的 `define` 函数。
2. 在整个 crate 内搜索 `start_category` 与 `reset_category` 的调用点：
   ```bash
   grep -rn "start_category\|reset_category" crates/typst-library/src
   ```

**需要观察的现象**：每个内容模块的 `define` 函数都以 `start_category` 开头、以 `reset_category` 结尾，中间夹着一批 `define_type` / `define_elem` / `define_func`。

**预期结果**：你会确认「类别 = 一组连续注册的定义」这一结构。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Category` 有 14 个变体，而顶层 `pub mod` 只有 13 个？

> **答案**：二者维度不同。`Category` 是「分类标签」，包含 `Svg`、`Png`、`Bundle` 这类本 crate 不实现（由输出 crate 提供）或由特性开关控制的类别；而 `pub mod` 是「本 crate 实际存在的代码模块」。二者不必相等，也不是一一对应。

**练习 2**：`DataLoading` 类别对应的是哪个目录？为什么名字不一样？

> **答案**：对应 `loading/` 目录。类别名侧重「语义」（数据加载），目录名侧重「实现」（loading 模块），二者通过 `loading::define` 内的 `start_category(Category::DataLoading)` 关联。

---

### 4.3 `global` 函数：标准库的装配流水线

#### 4.3.1 概念说明

`global()` 是标准库的「总装车间」。它接收 routines、math 子模块、inputs 字典和特性开关，产出一个 `Module`（即全局作用域）。所有用户在 Typst 顶层能直接使用的名字（不需要写 `#xxx.yyy` 前缀的那些）都来自这个全局模块。

理解 `global()` 的关键，是区分两种注册风格：

- **直接注入式**：大多数模块（foundations、model、text、layout、visualize、introspection、loading、symbols）的 `define(&mut global)` 直接往**同一个** `global` 作用域里塞定义。
- **子模块挂载式**：`math` 和 `pdf` 先各自用 `module()` 构建一个**独立的** `Module`，再用 `global.define("math", math)` 把它作为**嵌套子模块**挂上去。这也是为什么用户要写 `#math.frac(...)`、`#pdf.embed(...)`，而 `#heading(...)` 可以直接调用。

#### 4.3.2 核心流程

`global()` 的执行顺序（见源码 4.3.3）大致是：

```text
1. 创建一个去重的作用域 Scope::deduplicating()
2. 依次调用 8 个内容模块的 define（直接注入）
3. 把预先建好的 math 子模块挂到 "math"
4. 构建 pdf 子模块并挂到 "pdf"
5. 若启用 Feature::Html，通过 routine 挂载 "html"
6. prelude() 注入一批全局常量（颜色、方向、对齐等）
7. 包装成 Module::new("global", global) 返回
```

注意第 2 步里 `math` 模块本身**不**参与（它是 `define` 风格的例外），它在 `LibraryBuilder::build()` 里提前构建好，再作为参数传入 `global()`。

#### 4.3.3 源码精读

`global()` 函数定义在 [src/lib.rs:329-355](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L329-L355)。它的核心结构如下：

```rust
fn global(
    routines: &Routines,
    math: Module,
    inputs: Dict,
    features: &Features,
) -> Module {
    let mut global = Scope::deduplicating();

    // (1) 直接注入式：8 个内容模块塞进同一个 global
    self::foundations::define(&mut global, inputs);
    self::model::define(&mut global, features);
    self::text::define(&mut global);
    self::layout::define(&mut global);
    self::visualize::define(&mut global);
    self::introspection::define(&mut global);
    self::loading::define(&mut global);
    self::symbols::define(&mut global);

    // (2) 子模块挂载式：把独立 Module 作为嵌套命名空间挂上去
    global.define("math", math);
    global.define("pdf", self::pdf::module(features));
    if features.is_enabled(Feature::Html) {
        global.define("html", (routines.html_module)());
    }

    prelude(&mut global);

    Module::new("global", global)
}
```

对照「直接注入式」的典型代表，[src/foundations/mod.rs:91-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L91-L123) 中的 `define` 直接操作传入的 `global`：

```rust
pub(super) fn define(global: &mut Scope, inputs: Dict) {
    global.start_category(crate::Category::Foundations);
    global.define_type::<bool>();
    // ... 大量 define_type / define_func ...
    global.define("calc", calc::module());
    global.define("sys", sys::module(inputs));
    global.reset_category();
}
```

而「子模块挂载式」的代表 [src/math/mod.rs:43-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L43-L108) 则是先建好自己的作用域，再返回一个独立 `Module`：

```rust
pub fn module() -> Module {
    let mut math = Scope::deduplicating();
    math.start_category(crate::Category::Math);
    math.define_elem::<EquationElem>();
    // ... 大量 define_elem / define_func ...
    crate::symbols::define_math(&mut math);   // 注入 sym 符号
    Module::new("math", math)                 // 返回独立模块
}
```

math 子模块是在 [src/lib.rs:221-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L221-L234) 的 `build()` 中提前构建，再传入 `global()`：

```rust
pub fn build(self) -> Library {
    let math = math::module();                       // 先建 math
    let global = global(self.routines, math.clone(), inputs, &self.features);
    Library { ... math, ... }                        // 同时存进 Library.math 与 global
}
```

最后，`prelude()`（[src/lib.rs:358-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L358-L395)）把一批常用值提升为全局常量，让用户可以直接写 `#rect(fill: blue)` 而不是 `#rect(fill: color.blue)`：

```rust
fn prelude(global: &mut Scope) {
    global.define("blue", Color::BLUE);
    // ... 其他颜色 ...
    global.define("ltr", Dir::LTR);
    global.define("center", Alignment::CENTER);
    // ...
}
```

#### 4.3.4 代码实践

**实践目标**：亲手追踪 `global()` 的装配顺序，并标注每个目录由哪一行注册。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs)，定位 `global()` 函数（约 329 行起）。
2. 准备一张空白的「模块树」草图，把 `src/` 下 10 个内容目录列在左边。
3. 逐行阅读 `global()`，在右边填上「注册行号」与「注册方式」，例如：
   - `foundations/` → 第 337 行 `self::foundations::define(&mut global, inputs)`（直接注入）
   - `math/` → 第 346 行 `global.define("math", math)`（子模块挂载，模块本体在 `build()` 第 222 行构建）
   - `pdf/` → 第 347 行 `global.define("pdf", self::pdf::module(features))`（子模块挂载）
4. 用一条命令交叉验证注册函数的位置：
   ```bash
   grep -rn "pub(super) fn define\|pub fn define\|pub fn module" crates/typst-library/src
   ```

**需要观察的现象**：

- 8 个目录走「直接注入」，它们各自 `define` 的第一参数都是 `&mut global`。
- `math` 与 `pdf` 走「子模块挂载」，它们返回独立的 `Module`。
- `html` 只在 `Feature::Html` 启用时才挂载，且其模块体来自 `routines.html_module`（不在本 crate）。

**预期结果**：得到一张完整的「目录 → 注册行号 → 注册方式」对照表（见 4.2.3 与 4.3.3 的表格雏形）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `math` 要用「子模块挂载」而不是像 `layout` 那样直接注入？

> **答案**：因为数学定义在 Typst 中被组织成一个独立的命名空间 `math`（用户写 `#math.frac(...)`），它需要作为一个嵌套 `Module` 存在；而 `layout` 的定义（如 `Length` 类型、`align` 函数）是直接暴露在顶层的。此外，`Library` 结构体还需要单独保存一份 `math` 字段，所以在 `build()` 里提前构建并复用。

**练习 2**：`prelude()` 注入的 `blue`、`center` 这些常量，和 `visualize::define` 注册的内容是什么关系？

> **答案**：`visualize::define` 会把 `color` 模块（含 `color.blue` 这样的访问路径）注册到全局；而 `prelude()` 额外把 `Color::BLUE` 这个值直接以名字 `blue` 提升到全局，方便用户省略 `color.` 前缀。二者指向相似的值，但暴露的访问路径不同。

**练习 3**：若用户在 Typst 里输入 `#html.fragment(...)`，这段代码的「模块体」来自哪里？

> **答案**：来自 `global()` 中 `(routines.html_module)()` 的调用结果。因为 HTML 输出行为拆在别的 crate，本 crate 通过 `Routines` 函数指针在运行期注入实现，所以 `html` 模块体并不在 `typst-library` 的源码目录里。

## 5. 综合实践

**任务**：绘制一张完整的「`typst-library` 标准库装配图」，把本讲的三条线索——目录结构、`Category` 分类、`global()` 装配——串联成一张图。

要求：

1. **左列**：列出 `src/` 下 10 个内容模块目录（foundations / model / text / layout / visualize / introspection / loading / symbols / math / pdf）。
2. **中列**：标注每个目录对应的 `Category` 变体（如 `loading/` → `DataLoading`）。
3. **右列**：标注 `global()` 中的注册行号与注册方式（直接注入 / 子模块挂载）。
4. **底部**：单独列出 3 个「基础设施模块」（diag / engine / routines），并写明它们不参与 `global()` 注册的原因。
5. **附注**：标出 `Svg`、`Png`、`Bundle` 这三个「无对应目录」的类别，说明它们在标准库装配中缺席的原因。

完成后再回答一个检验性问题：**如果有人想新增一个内容模块（比如 `diagram/`），需要改动 `lib.rs` 的哪几个地方？**

> 参考答案：至少需要 (a) 加一行 `pub mod diagram;`；(b) 在 `Category` 枚举加一个变体并在 `name()` 里映射字符串；(c) 在 `global()` 里加一行 `self::diagram::define(&mut global)`；(d) 在 `diagram/mod.rs` 里实现 `define` 函数，内部用 `start_category` / `reset_category` 包裹注册逻辑。

## 6. 本讲小结

- `typst-library` 的 13 个顶层 `pub mod` 可分为两类：10 个**标准库内容模块**（装配进用户可见的作用域）和 3 个**编译器基础设施模块**（`diag` / `engine` / `routines`，不作为标准库定义注册）。
- `Category` 枚举有 **14 个变体**，是标准库的「分类标签」，通过 `start_category` / `reset_category` 配对使用；它与目录不是一一对应（`Svg` / `Png` / `Bundle` 在本 crate 无实现，`DataLoading` 对应 `loading/`）。
- `global()` 是标准库的总装函数：8 个模块用「直接注入式」`define(&mut global)`，`math` 与 `pdf` 用「子模块挂载式」构建独立 `Module` 再挂载，`html` 按特性开关经 routine 注入。
- `prelude()` 把颜色、方向、对齐等常用值提升为全局常量，让用户可以省略模块前缀。
- 判定一个模块是否属于「标准库内容」，最可靠的依据是看它是否在 `global()` 中被 `define` / `module` 调用。

## 7. 下一步学习建议

本讲给你建立了「目录—分类—装配」的静态地图。接下来的讲义会进入这张地图的内部：

- **u1-l3（标准库的装配：Library、Builder、global 与 prelude）** 会更深入地讲 `Library` 结构体的七个字段、`LibraryBuilder` 的配置流程，以及 `Feature` 开关如何影响装配，是本讲 `global()` 内容的自然延伸。
- **u2（值与类型基础）** 会进入 `foundations/` 目录，从 `Value` 枚举开始讲标准库最核心的类型系统。
- 如果你想立刻看到「行为分离」的全貌，可以先跳读 [src/routines.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs)，它解释了为何本 crate 能在不依赖行为 crate 的前提下回调它们（u5-l4 会专题讲解）。
