# 标准库的诞生：Library、global 作用域与 prelude

## 1. 本讲目标

上一讲（u3-l1）我们读完了 `World` trait——编译器与宿主环境之间的契约。`World::library()` 返回的 `LazyHash<Library>` 正是本讲的主角：Typst 的**标准库**。读完本讲，你应当能够：

1. 说出 `Library` 每个字段的职责：`routines`、`global`、`math`、`styles`、`rules`、`std`、`features`、`formats`。
2. 用 `LibraryExt::builder()` 转动标准库的三个旋钮：`inputs`（暴露给 `sys.inputs`）、`features`（实验特性开关）、`formats`（注册导出格式及其规则）。
3. 跟踪 `global()` 函数的装配顺序：八个类别模块 + `format` + `math` 如何依次把自己的定义注入同一个 `Scope`，`start_category` / `reset_category` 如何给绑定打上分类标签。
4. 解释 prelude 为什么让你不写任何 `#import` 就能用 `red`、`ltr`、`center` 这些名字，以及 `prelude_path` 如何把 `format.html`、`format.bundle.asset` 这样的深层绑定「提升」到全局顶层。
5. 亲手写一个测试：构建 `Library`，遍历 global 作用域，打印全部绑定名并按 `Category` 分类计数，再注入一个自定义绑定。

## 2. 前置知识

本讲默认你已读完 u1-l3（crate 分工）与 u3-l1（World 契约），并了解以下概念：

- **模块**：Typst 脚本中一组名字的容器，例如 `calc.abs(-1)` 里的 `calc`、`#import "x.typ": *` 导入的东西。Rust 侧对应 `Module` 类型。
- **作用域**：名字到值的有序映射。一个名字在作用域里的一条记录叫**绑定**：它不只是值，还携带元数据（定义位置的 `Span`、文档分类 `Category`、实验特性门 `Feature` 等）。
- **Value**：Typst 语言中值的 Rust 统一表示，int、str、module、元素等都是 `Value` 的一种。下一讲（u3-l3）会展开，本讲只需把它理解为「语言值」。
- **Category（分类）**：标准库定义的分组方式，与官方文档站的导航一一对应（foundations、model、text、layout……）。它是绑定上的**元数据**，不影响查找，只影响归类。
- **LazyHash**：comemo 提供的「构造时算一次哈希」的包装。`World::library()` 返回 `LazyHash<Library>`，编译器靠哈希判断标准库是否变了，从而决定哪些增量缓存需要失效（详见 u5-l8）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [typst-library/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs) | 本讲主战场：`Library`、`LibraryBuilder`、`Features`/`Feature`、`Category`、`global()`、`prelude()` 全在这一个文件里 |
| [foundations/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs) | foundations 子模块的组织；`foundations::define` 是各模块 `define` 的样板；`eval` 函数示范「字典 → 作用域」注入 |
| [foundations/scope.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs) | `Scope`（映射）、`Binding`（绑定）、`Scopes`（求值期作用域栈） |
| [foundations/module.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/module.rs) | `Module` 类型：名字 + 作用域 |
| [typst-library/src/format.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs) | `Format`（导出格式描述）与全局 `format` 模块的装配 |
| [typst-library/src/math/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/math/mod.rs) | `math::module()`：数学模式作用域的组装 |
| [foundations/sys.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/sys.rs)、[symbols.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/symbols.rs)、[model/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/model/mod.rs)、[text/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs) | 各模块 `define` 的实例（layout、visualize、introspection、loading 的 mod.rs 同理） |
| [typst/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs) | `LibraryExt`（装配入口）、`ROUTINES`（行为接线表）、现成的单元测试 |
| [typst-cli/src/world.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs)、[typst-cli/src/args.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs) | CLI 如何用 builder 定制标准库（`--input`、特性开关） |
| [typst-eval/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs) | 求值器如何以 `Library` 为作用域基座 |

## 4. 核心概念与源码讲解

### 4.1 Library 与 LibraryBuilder：标准库的骨架

#### 4.1.1 概念说明

标准库不是「一堆函数」那么简单。一个可用的标准库至少要回答四个问题：

1. 脚本里裸写的名字（`heading`、`upper`、`red`）从哪来？——**global 作用域**。
2. 数学模式 `$...$` 里的名字（`alpha`、`frac`）从哪来？——**math 作用域**，与 global 分开存放，避免互相污染。
3. 排版默认值是什么？——**styles**（初始为空，真正的默认值在各元素自己的字段默认里）。
4. 编译到什么格式？——**formats**，每个格式还会带来自己的内置规则。

此外还有三样「基础设施」挂在 `Library` 上：`routines`（函数指针表，u1-l3 讲过的「类型在 library、行为在别处」的接线点）、`rules`（内置 show 规则表，供 u4-l2 的 realize 阶段消费）、`std`（把整个 global 模块包成一个值，提供 `std` 这个名字）。

`LibraryBuilder` 是定制入口。三个旋钮：

- `with_inputs(Dict)`：键值对，最终暴露为脚本里的 `sys.inputs.key`。
- `with_features(Features)`：开启实验特性（`html`、`bundle`、`a11y-extras`），源码注释原话是「No guarantees whatsover!」。
- `formats`（构造时传入）：注册导出格式，决定 `format` 模块里有哪些格式元素、`rules` 表里注册哪些格式规则。

#### 4.1.2 核心流程

装配一条龙（伪代码）：

```text
LibraryExt::builder(formats)              // typst crate：接上 ROUTINES 和格式列表
  = LibraryBuilder::from_routines(&ROUTINES, formats)
  → .with_inputs(Dict)                    // 可选
  → .with_features(Features)              // 可选
  → .build()
       ├─ math   = math::module()         // 先组装数学模块
       ├─ global = global(&formats, math.clone(), inputs)
       ├─ rules  = (routines.rules)()     // 基础规则（layout/html 注册）
       │           + 每个 format.register_rules(&mut rules)
       └─ Library { routines, global, math,
                    styles: Styles::new(), rules,
                    std: Binding::detached(global),
                    features, formats }
```

注意两个细节：`math` 模块被**克隆出两份用途**——一份作为 `Library.math`（数学模式查这里），一份被 `global.define("math", math)` 塞进 global（所以脚本里也能写 `math.sqrt(2)`）；`global` 模块同样被克隆一份，包成 `std` 绑定。

#### 4.1.3 源码精读

**Library 结构体**——[typst-library/src/lib.rs:L161-L188](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L161-L188) 定义了标准库的全部家当，八个字段逐一对应上面的概念（节选，注释有删减）：

```rust
#[non_exhaustive]
pub struct Library {
    pub routines: &'static Routines,
    /// The module that contains the definitions that are available everywhere.
    pub global: Module,
    /// The module that contains the definitions available in math mode.
    pub math: Module,
    /// The default style properties ...
    pub styles: Styles,
    /// The built-in show rules.
    pub rules: NativeRuleMap,
    /// The standard library as a value. Used to provide the `std` module.
    pub std: Binding,
    /// In-development features that were enabled.
    pub features: Features,
    /// Registered [export formats](crate::format).
    pub formats: Vec<Format>,
}
```

**LibraryBuilder 与三个旋钮**——[typst-library/src/lib.rs:L190-L228](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L190-L228)。字段全部私有，只能通过 builder 方法配置；`from_routines` 带 `#[doc(hidden)]`，说明它本意只给总装 crate 用：

```rust
pub struct LibraryBuilder {
    routines: &'static Routines,
    inputs: Option<Dict>,
    features: Features,
    formats: Vec<Format>,
}
```

**build()**——[typst-library/src/lib.rs:L230-L250](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L230-L250) 是装配的终点，也是本节与 4.2 的交接点（`global(...)` 的内部下一节拆）：

```rust
pub fn build(self) -> Library {
    let math = math::module();
    let inputs = self.inputs.unwrap_or_default();
    let global = global(&self.formats, math.clone(), inputs);
    let mut rules = (self.routines.rules)();
    for format in &self.formats {
        format.register_rules(&mut rules);
    }
    Library {
        routines: self.routines,
        global: global.clone(),
        math,
        styles: Styles::new(),
        rules,
        std: Binding::detached(global),
        features: self.features,
        formats: self.formats,
    }
}
```

**谁调用 builder？**——[typst/src/lib.rs:L289-L323](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L289-L323) 的 `LibraryExt` trait 把 `Library::new(formats)` / `Library::builder(formats)` 变成趁手的入口，并把总装 crate 的 `ROUTINES` 接进来：

```rust
impl LibraryExt for Library {
    fn new(formats: impl IntoIterator<Item = Format>) -> Library {
        Self::builder(formats).build()
    }
    fn builder(formats: impl IntoIterator<Item = Format>) -> LibraryBuilder {
        LibraryBuilder::from_routines(&ROUTINES, formats)
    }
}
```

`ROUTINES` 在 [typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342)，其中 `rules` 闭包先注册 layout 与 html 的基础规则，随后 `build()` 再叠加各格式自己的规则。另外 `typst` crate 用一句 [typst/src/lib.rs:L36](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L36) 的 `pub use typst_library::*;` 把整个标准库类型面（`Library`、`Category`、`Feature`、`foundations::Scope` 等）原样转出——所以下游代码统一写 `typst::Library`。

**Features 与 Feature**——[typst-library/src/lib.rs:L252-L283](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L252-L283) 的 `Features` 就是一个 `SmallBitSet` 位集合；[typst-library/src/lib.rs:L285-L322](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L285-L322) 的 `Feature` 枚举目前只有三个成员，且标了 `#[non_exhaustive]`（还会继续加）：

```rust
#[non_exhaustive]
pub enum Feature {
    Html,
    Bundle,
    A11yExtras,
}
```

**真实调用方：CLI**——[typst-cli/src/world.rs:L58-L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs#L58-L75)。`SystemWorld` 构建时把命令行的 `--input` 键值对收成 `Dict`、把特性开关收成 `Features`，然后走 `Library::builder([...]).with_inputs(inputs).with_features(features)`。参数定义在 [typst-cli/src/args.rs:L482-L490](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L482-L490)，文档字符串直白地写着「Add a string key-value pair visible through `sys.inputs`」（`-i/--input key=value`）。

**inputs 的落点**——[foundations/sys.rs:L6](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/sys.rs#L6) 的 `sys::module(inputs)` 接住这份字典，并在 [foundations/sys.rs:L32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/sys.rs#L32) 处 `define("inputs", inputs)`，这就是脚本里 `#sys.inputs.user` 的全部秘密。

顺带一提：`Library` 的文档注释（L161-L166）建议用 `LibraryExt` 来构建；注释里还提到 `Library::default()`，但当前源码中并没有为 `Library` 实现 `Default`（装配必须提供 `Routines` 与 `formats`），以源码为准、以 `LibraryExt::new/builder` 为准。

#### 4.1.4 代码实践：三种姿势构建 Library

1. **实践目标**：亲手构建不同配置的 `Library`，验证 formats 计数、features 开关与 inputs 通路。
2. **操作步骤**：
   - 打开 [typst/src/lib.rs:L344-L357](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L344-L357)，仓库已经留了一个 `formats` 测试作示范（`Library::new([])`、`Library::new([typst_html::FORMAT])`……）。
   - 在同一个 `mod tests` 里追加（示例代码，读者在自己克隆中临时添加，练习后还原）：

   ```rust
   #[test]
   fn tutorial_build_library() {
       // 1) 零格式
       let bare = Library::new([]);
       assert!(bare.formats.is_empty());

       // 2) 注册 html + svg 两个格式（typst_svg 是 dev-dependency）
       let lib = Library::builder([typst_html::FORMAT, typst_svg::FORMAT]).build();
       assert_eq!(lib.formats.len(), 2);

       // 3) 特性开关 + sys 通路
       let lib = Library::builder([typst_html::FORMAT])
           .with_features(Features::from_iter([Feature::Html]))
           .build();
       assert!(lib.features.is_enabled(Feature::Html));
       assert!(lib.global.scope().get("sys").is_some());
   }
   ```

   - 再用 CLI 验证 inputs 通路（无需改代码）：

   ```bash
   echo '#sys.inputs.user' > /tmp/inputs-demo.typ
   cargo run -p typst-cli -- compile --input user=typst /tmp/inputs-demo.typ /tmp/inputs-demo.pdf
   ```

3. **需要观察的现象**：测试通过；三条断言分别命中 formats 计数、features 位集合、global 中的 `sys` 绑定。
4. **预期结果**：`cargo test -p typst --lib tutorial_build_library -- --nocapture` 全绿；编译出的 PDF 里出现文本 `typst`（键值对经 world.rs → `with_inputs` → `sys::module` 注入）。
5. 若在无字体环境编译 PDF 失败，可改用 `--format png` 或仅跑单元测试部分；具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果注册两个「元素同名」的格式，`format` 模块会怎样？
**答案**：`format` 模块的作用域是 `Scope::deduplicating()`（[typst-library/src/format.rs:L91](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs#L91)），debug 构建下重复定义直接 panic（见 4.2 的 `define` 源码）；release 构建下 `bind` 走 `Entry::Occupied` 分支，后定义者覆盖先定义者。

**练习 2**：`with_features(Feature::Html)` 之后，标准库「多出」了哪些东西？
**答案**：几乎没有「多出」定义，多出的是**访问权限**。被 `with_feature` 门住的绑定（例如 prelude 提升的 `html`）平时就在作用域里，但读取时要过 `check_access` 检查（[foundations/scope.rs:L376-L381](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L376-L381)）；未开启特性时读取会得到特性错误。Feature 控制「可见性」而非「存在性」。

**练习 3**：为什么 `Library` 要 `#[derive(Hash)]`？
**答案**：`Library` 挂在 `World::library()` 上且被 `LazyHash` 包装，编译器（comemo）用哈希判断标准库是否变化；宿主改了 inputs 或特性开关，旧的计算缓存随之失效。

### 4.2 global() 装配流程：Scope、Binding 与 Category

#### 4.2.1 概念说明

global 是「所有顶层名字的家」：`#heading`、`#upper()`、`red`、`calc`……全部住在同一个作用域里。装配工作由一个 18 行的私有函数 `global()` 完成，策略非常朴素——**挨家挨户收定义**：

- `foundations`、`model`、`text`、`layout`、`visualize`、`introspection`、`loading`、`symbols` 八个类别模块各自暴露一个 `define(&mut Scope)`，把自家的类型、函数、元素塞进来；
- `format` 模块根据注册的格式动态生成；
- `math` 模块整体挂进来；
- 最后 `prelude()` 补上颜色、方向、对齐等常量。

三个数据结构撑起整个过程：

- **Scope**：`IndexMap<EcoString, Binding>` 的有序映射，外加两个开关（是否查重、当前 Category）。
- **Binding**：一条绑定的完整形态 = 值 + 种类 + Span + 访问检查标记 + 带外元数据盒子 `BindingInfo`（category、feature 门、文档、弃用信息都住在里面）。
- **Category**：15 个成员的枚举，对应文档站导航（foundations、introspection、layout、data-loading、math、model、symbols、text、visualize、format、pdf、html、svg、png、bundle）。

而求值器真正查名字时用的是另一层——**Scopes**（作用域栈）：`top`（当前块的作用域）压在 `scopes` 栈上，栈之外还有一个 `base: Option<&Library>`，标准库作为**最终回落层**。typst-eval 的模块求值入口正是用 `Scopes::new(Some(library))` 起步（[typst-eval/src/lib.rs:L67](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L67)）。

#### 4.2.2 核心流程

`global()` 的注入顺序（[typst-library/src/lib.rs:L369-L385](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L369-L385)）：

```text
Scope::deduplicating()                     // 开启 debug 查重
 1. foundations::define(scope, inputs)     // start_category(Foundations) … reset_category()
 2. model::define(scope)                   // 同样包裹在 Model 类别里
 3. text::define(scope)
 4. layout::define(scope)
 5. visualize::define(scope)
 6. introspection::define(scope)
 7. loading::define(scope)
 8. symbols::define(scope)
 9. define("format", format::module(formats))   // 类别 Format，成员随注册格式变化
10. define("math", math)                        // 此刻类别已复位，绑定 category = None
    prelude(&mut scope)                         // 常量注入，同样 category = None
Module::new("global", scope)
```

每个 `define_*` 调用都会把**此刻 Scope 上的 category** 写进新绑定的元数据——这就是「start_category → 一串 define → reset_category」三明治模式的用处：同一批注入自动获得同一分类。

#### 4.2.3 源码精读

**global() 本体**——[typst-library/src/lib.rs:L368-L386](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L368-L386)：

```rust
fn global(formats: &[Format], math: Module, inputs: Dict) -> Module {
    let mut global = Scope::deduplicating();

    self::foundations::define(&mut global, inputs);
    self::model::define(&mut global);
    self::text::define(&mut global);
    self::layout::define(&mut global);
    self::visualize::define(&mut global);
    self::introspection::define(&mut global);
    self::loading::define(&mut global);
    self::symbols::define(&mut global);
    global.define("format", self::format::module(formats));
    global.define("math", math);

    prelude(&mut global);

    Module::new("global", global)
}
```

**一个类别模块的样板：foundations::define**——[foundations/mod.rs:L90-L123](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs#L90-L123)。先 `start_category`，然后 21 个 `define_type`（bool、int、str、array、dict、func……）、6 个 `define_func`（repr、panic、assert、eval、plugin、target），再把 `calc`、`sys` 两个子模块挂上，最后 `reset_category`（节选）：

```rust
pub(super) fn define(global: &mut Scope, inputs: Dict) {
    global.start_category(crate::Category::Foundations);
    global.define_type::<bool>();
    global.define_type::<i64>();
    // ……共 21 个类型……
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

其他模块如出一辙：[model/mod.rs:L52-L75](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/model/mod.rs#L52-L75)（Model 类别）、[text/mod.rs:L56-L72](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L56-L72)（Text 类别）、[symbols.rs:L6](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/symbols.rs#L6)（符号表注入，同文件 L13 还有给 math 模块用的 `define_math`）。

**Scope 结构**——[foundations/scope.rs:L143-L169](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L143-L169)：

```rust
pub struct Scope {
    map: IndexMap<EcoString, Binding, FxBuildHasher>,
    deduplicate: bool,
    category: Option<Category>,
}
```

**define()：所有注入的终点站**——[foundations/scope.rs:L210-L224](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L210-L224)。debug 构建下查重；**把当前 category 盖章到新绑定**；`bind` 入表：

```rust
pub fn define(&mut self, name: &'static str, value: impl IntoValue) -> &mut Binding {
    #[cfg(debug_assertions)]
    if self.deduplicate && self.map.contains_key(name) {
        panic!("duplicate definition: {name}");
    }

    let mut binding = Binding::detached(value);
    binding.init_info().category = self.category;
    self.bind(name.into(), binding)
}
```

围绕它还有一族便捷方法（[foundations/scope.rs:L171-L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L171-L199)）：`define_func::<T>()` 从 `#[func]` 宏生成的 `NativeFuncData` 里取名字与文档，`define_type::<T>()` 注册类型（名字取 `short_name()`），`define_elem::<T>()` 注册元素。这就是「宏生成数据、scope 统一装配」的分工。

**Scope 的读写口**——[foundations/scope.rs:L263-L291](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L263-L291)：`bind`（覆盖或插入）、`get` / `get_mut`、以及本讲实践要用的 `iter()`（产出「名字、绑定」对）：

```rust
pub fn iter(&self) -> impl Iterator<Item = (&EcoString, &Binding)> {
    self.map.iter()
}
```

**Binding：一条绑定的解剖图**——[foundations/scope.rs:L323-L350](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L323-L350)：

```rust
pub struct Binding {
    value: Value,                    // 语言值本体
    kind: BindingKind,               // Normal | Captured(闭包捕获)
    span: Span,                      // 定义位置
    check_access: bool,              // 读前是否需要过特性/弃用检查（快路径优化）
    info: Option<Box<BindingInfo>>,  // 带外元数据：category、feature、文档……
}
```

`with_category` / `with_feature` 等方法（[foundations/scope.rs:L369-L381](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L369-L381)）都是往 `info` 盒子里写；读取分类用 [category()](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L478)（返回 `Option<Category>`）。读取绑定值则要走 [read(guard)](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L423)，由守卫执行特性门与弃用检查。

**math 与 format 两个特殊成员**：

- [typst-library/src/math/mod.rs:L44-L48](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/math/mod.rs#L44-L48)：`math::module()` 自建一个 deduplicating 作用域，`start_category(Math)` 后用 `define_elem` 装入 Equation、Lr、Frac、Mat 等数学元素。求值器为数学模式准备了专门入口 `Scopes::get_in_math`（[foundations/scope.rs:L56](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L56)）。
- [typst-library/src/format.rs:L89-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs#L89-L103)：`format::module(formats)` 遍历**注册的格式**，把每个格式的元素 define 进来（需要门控的再补 `with_feature`）——所以 `format` 模块的内容是装配期动态决定的：

```rust
pub fn module(formats: &[Format]) -> Module {
    let mut format = Scope::deduplicating();
    format.start_category(crate::Category::Format);
    for f in formats {
        let binding =
            format.define(f.elem.name(), f.elem).with_documentation(f.elem.into());
        if let Some(feature) = f.feature {
            binding.with_feature(feature);
        }
    }
    Module::new("format", format)
}
```

**Scopes：求值期的作用域栈**——[foundations/scope.rs:L19-L46](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L19-L46)：

```rust
pub struct Scopes<'a> {
    pub top: Scope,
    pub scopes: Vec<Scope>,
    /// The standard library.
    pub base: Option<&'a Library>,
}
```

`enter` / `exit` 进出块级作用域；查名字先看 `top`，沿栈下探，最后回落到标准库（`base`）。

#### 4.2.4 代码实践：遍历 global、按 Category 分类计数

1. **实践目标**：构建 `Library`，遍历 global 作用域，打印全部顶层绑定名，统计每个 `Category` 各有多少绑定——把 4.2 的装配顺序「跑」出来看。
2. **操作步骤**：继续在 [typst/src/lib.rs:L344](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L344) 的 `mod tests` 里追加（示例代码，练习后还原）：

   ```rust
   #[test]
   fn tutorial_global_bindings_by_category() {
       use std::collections::HashMap;

       let lib = Library::new([typst_html::FORMAT, typst_svg::FORMAT]);

       let mut names: Vec<&str> = Vec::new();
       let mut counts: HashMap<Option<Category>, usize> = HashMap::new();
       for (name, binding) in lib.global.scope().iter() {
           names.push(name.as_str());
           *counts.entry(binding.category()).or_default() += 1;
       }

       names.sort_unstable();
       println!("共 {} 个顶层绑定", names.len());
       for name in &names {
           println!("  - {name}");
       }

       let mut entries: Vec<_> = counts.into_iter().collect();
       entries.sort_by_key(|&(_, count)| std::cmp::Reverse(count));
       println!("按 Category 分类计数：");
       for (category, count) in entries {
           println!("  {category:?}: {count}");
       }
   }
   ```

3. **需要观察的现象**：输出名单里能按 `global()` 的注入顺序「读」出整部标准库——foundations 的类型与函数（`assert`、`calc`、`eval`、`sys`……）、model/text/layout 等的元素与函数（`heading`、`page`……）、`format` 与 `math` 两个模块、prelude 的常量（`red`、`ltr`、`center`……）以及 `prelude_path` 提升的 `html`。
4. **预期结果**：分类计数应出现 `Some(Foundations)`、`Some(Model)`、`Some(Text)` 等若干桶，外加一个不小的 `None` 桶（prelude 常量与 format/math 等桶外绑定——它们定义时类别都已复位）。具体每桶的数字「待本地验证」。
5. 运行命令：`cargo test -p typst --lib tutorial_global_bindings -- --nocapture`。

#### 4.2.5 小练习与答案

**练习 1**：prelude 里的颜色常量为什么 `category()` 是 `None`？
**答案**：`prelude()` 在八个模块的 `define`（各自以 `reset_category()` 收尾）之后才执行，此刻 `Scope.category` 已是 `None`，而 `define()` 会把「当前 category」盖章进绑定（[foundations/scope.rs:L215-L224](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L215-L224)）。分类标签只服务于文档归类，不影响名字查找。

**练习 2**：数学元素为什么不住在 global 里？
**答案**：`$...$` 内部的名字查找走 `Scopes::get_in_math`，去 `Library.math` 找（`math::module()` 自成一体、类别为 Math）；同时 global 里挂了一个 `math` 模块绑定，供脚本写 `math.sqrt(2)` 做点访问。两份 `Module` 来自 `build()` 里的同一次 `math::module()` 调用（[typst-library/src/lib.rs:L232-L242](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L232-L242)）。

**练习 3**：`deduplicating` 的查重只在 debug 构建生效，为什么够用？
**答案**：它是给标准库开发者防手滑的装置——标准库内容由代码写死，正常情况不可能重名；release 下多一次哈希查找纯属浪费。对应的 `#[cfg(debug_assertions)]` 就写在 `define()` 里。

### 4.3 prelude 常量注入：颜色、方向、对齐与 prelude_path

#### 4.3.1 概念说明

排版语言里有一批「高频到不该要求 import」的名字：颜色（`red`）、方向（`ltr`）、对齐（`center`）。prelude 就是这些名字的家——它们不属于任何子模块，直接躺在 global 顶层的最后一段。

当前版本（HEAD 64c5e8c0）的 prelude 共 **36 个直接 define + 3 个 `prelude_path`**，分四组：

| 组 | 内容 | 数量 |
|---|---|---|
| 颜色常量 | `black`…`lime`（`Color::BLACK` 等关联常量） | 18 |
| 颜色/工具函数 | `luma`、`oklab`、`oklch`、`rgb`、`cmyk`、`range` | 6 |
| 方向常量 | `ltr`、`rtl`、`ttb`、`btt`（`Dir`） | 4 |
| 对齐常量 | `start`、`left`、`center`、`right`、`end`、`top`、`horizon`、`bottom`（`Alignment`） | 8 |

更有意思的是 `prelude_path`：它把**深层绑定**克隆一份提到顶层。例如 `prelude_path(["format", "html"])` 在 global 里找 `format` 模块，下钻到其中的 `html` 绑定，克隆后以路径最后一段的名字重新绑到 global 顶层——于是脚本里可以直接写顶层的 `html`，而不必 `format.html`。而且克隆会**继承原绑定的特性门**（`html` 被门在 `Feature::Html` 后面），未开启特性时读取会被拦截。

#### 4.3.2 核心流程

```text
prelude(global):
  18 × define(颜色常量)        // Color::BLACK 等，值为 color
   6 × define(函数)            // luma/oklab/oklch/rgb/cmyk/range，值为 Func
   4 × define(方向)            // Dir::LTR 等
   8 × define(对齐)            // Alignment::START 等
   3 × prelude_path([...])     // format.html / format.pdf / format.bundle.asset → 顶层
```

`prelude_path` 的下钻算法：取路径首段在自身查找 → 逐段进入子模块的 scope 找下一段 → 到达末段后克隆绑定 → 若沿途遇到特性门则合并（路径上不允许出现两个不同的门，否则 panic）→ 以末段名 `bind` 回自身。

#### 4.3.3 源码精读

**prelude() 本体**——[typst-library/src/lib.rs:L388-L431](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L388-L431)（节选，中间重复的颜色/对齐行有省略）：

```rust
fn prelude(global: &mut Scope) {
    global.define("black", Color::BLACK);
    // ……gray、silver、white、navy、blue、aqua、teal、eastern、
    //    purple、fuchsia、maroon、red、orange、yellow、olive、green、lime……
    global.define("luma", Color::luma_data());
    global.define("rgb", Color::rgb_data());
    global.define("range", Array::range_data());
    global.define("ltr", Dir::LTR);
    global.define("ttb", Dir::TTB);
    global.define("start", Alignment::START);
    global.define("center", Alignment::CENTER);
    global.define("bottom", Alignment::BOTTOM);

    // If available, add some items to the global prelude.
    global.prelude_path(["format", "html"]);
    global.prelude_path(["format", "pdf"]);
    global.prelude_path(["format", "bundle", "asset"]);
}
```

两点值得注意：`luma` / `rgb` / `range` 这些「函数常量」来自 `#[func]` 宏生成的 `*_data()`（`Func::from(&'static NativeFuncData)` 的转换见 [foundations/func.rs:L571](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/func.rs#L571)）；注释「If available」一语道破——若没注册对应格式，`format.html` 不存在，提升自然落空（返回 `None`，不报错）。

**prelude_path()**——[foundations/scope.rs:L226-L260](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L226-L260)（节选）：

```rust
pub(crate) fn prelude_path<'a, const N: usize>(
    &'a mut self,
    path: [&'static str; N],
) -> Option<&'a mut Binding> {
    let [first, path @ .., last] = path.as_slice() else { /* 至少两段 */ };

    let mut binding = self.get(first)?;
    let mut path_feature = binding.feature();
    for name in path.iter().chain([last]) {
        binding = binding.value.scope()?.get(name)?;
        // 沿途收集 feature 门；两个不同的门 → panic
    }

    let mut binding = binding.clone();
    if let Some(feature) = path_feature {
        binding.with_feature(feature);
    }
    Some(self.bind((*last).into(), binding))
}
```

克隆的意义：`format.html` 与顶层 `html` 是**两个独立的绑定**，改其中一个的元数据不影响另一个；但值本体（同一个元素）是共享的。

#### 4.3.4 代码实践：在脚本里注入并调用自定义函数

Typst 语言自带的 `eval` 函数正好是「作用域注入」的用户级出口：`scope: (名字: 值)` 字典会被逐键 `bind` 成一个临时作用域。我们用它完成本讲实践任务——**注入一个自定义函数并在脚本里调用它**。

1. **实践目标**：体验「字典 → Scope 绑定 → 脚本按名调用」的完整链路，并能在源码中指出每一步。
2. **操作步骤**：
   - 新建 `scope-eval.typ`（示例文档）：

   ```typ
   #eval("greet(3)", scope: (greet: n => n * 2 + 1))
   ```

   - 编译：`cargo run -p typst-cli -- compile scope-eval.typ /tmp/scope-eval.pdf`（或已安装的 `typst compile`）。
3. **需要观察的现象**：文档正文出现计算结果；`greet` 是闭包、由 `scope` 参数注入、在求值字符串中被按名调用。
4. **预期结果**：输出 `7`。链路对照源码：
   - 字典变作用域：[foundations/mod.rs:L297-L303](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs#L297-L303)

     ```rust
     let mut scope = Scope::new();
     for (key, value) in dict {
         scope.bind(key.into(), Binding::new(value, span));
     }
     ```

   - 求值调用：[foundations/mod.rs:L305-L321](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs#L305-L321) 经 `routines.eval_string`（即 typst-eval 的 [eval_string](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L99-L113)）执行字符串；名字 `greet` 先在传入 scope 命中，`n`、`*` 等回落到标准库（`Scopes::base`）。
5. 再补一个纯 Rust 侧的最小注入（加在 4.1 的测试模块里，示例代码）：

   ```rust
   #[test]
   fn tutorial_inject_binding() {
       let mut lib = Library::new([]);
       lib.global.scope_mut().define("answer", 42);
       assert!(lib.global.scope().get("answer").is_some());
   }
   ```

   预期：断言通过（`define` 接受任何 `impl IntoValue`；读取绑定值需走 `read(guard)`，见 [foundations/scope.rs:L404-L430](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L404-L430) 的文档示例，属进阶内容）。

#### 4.3.5 小练习与答案

**练习 1**：`prelude_path(["format", "bundle", "asset"])` 执行后，global 顶层多出的名字是什么？它的特性门是什么？
**答案**：名字是 `asset`（以路径**最后一段**命名）；它继承了 bundle 格式的特性门 `Feature::Bundle`——克隆时若沿途有门就 `with_feature` 补上。

**练习 2**：只注册 `typst_svg::FORMAT` 时，顶层会有 `html` 这个名字吗？
**答案**：不会。`format` 模块只 define 已注册格式的元素（[typst-library/src/format.rs:L94-L100](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs#L94-L100)），`format.html` 不存在，`prelude_path` 返回 `None`，提升落空且不报错。

**练习 3**：`#eval("x + 1", scope: (x: 2))` 里 `+` 是从哪来的？
**答案**：`+` 不是绑定而是运算符，由 ops 模块按操作数类型派发（u3-l3 展开）；但运算符两边的名字查找走同一套 `Scopes`：`x` 在注入 scope 命中，标准库由 `base` 兜底。

## 5. 综合实践

**任务：给你的标准库做一份「体检报告」。** 把本讲三个模块串起来，写一个测试（仍放在 crates/typst/src/lib.rs 的 `mod tests`，示例代码，练习后还原）：

```rust
#[test]
fn tutorial_library_health_report() {
    use std::collections::HashMap;

    // ① 定制构建（4.1）：注册 html 格式 + 开启 html 特性
    let mut lib = Library::builder([typst_html::FORMAT])
        .with_features(Features::from_iter([Feature::Html]))
        .build();

    // ② 遍历与分类计数（4.2）
    let mut by_category: HashMap<Option<Category>, usize> = HashMap::new();
    for (_name, binding) in lib.global.scope().iter() {
        *by_category.entry(binding.category()).or_default() += 1;
    }
    let total: usize = by_category.values().sum();
    println!("global 顶层绑定总数：{total}");
    let mut rows: Vec<_> = by_category.into_iter().collect();
    rows.sort_by_key(|&(_, n)| std::cmp::Reverse(n));
    for (category, count) in rows {
        println!("  {category:?}: {count}");
    }

    // ③ 抽查关键名字（4.2 / 4.3）：模块、prelude 常量、prelude_path 提升项
    for name in ["sys", "math", "format", "red", "ltr", "center", "html"] {
        assert!(
            lib.global.scope().get(name).is_some(),
            "global 中缺少 {name}"
        );
    }

    // ④ 注入自定义绑定（4.3 的 Rust 侧）
    lib.global.scope_mut().define("answer", 42);
    assert!(lib.global.scope().get("answer").is_some());
}
```

**验收标准**：

1. 测试通过，报告里能看到 4.2 预期的分类分布（`None` 桶 + 若干 `Some(...)` 桶；具体数字待本地验证）。
2. 七个抽查名字全部命中——每个名字你都能说出它是由 `global()` 的哪一行注入的：`sys` 来自 foundations::define、`red`/`ltr`/`center` 来自 prelude、`html` 来自 `prelude_path`。
3. （进阶，选做）把 ④ 的注入升级为「真的在脚本里跑」：仿照 [foundations/mod.rs:L305-L321](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs#L305-L321) 的调用方式，直接调 `typst_eval::eval_string`（签名见 [typst-eval/src/lib.rs:L99-L113](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L99-L113)），把注入了 `answer` 的 scope 传进去并求值字符串 `answer * 2`。这需要一个最小 `World`（u3-l1 的实践产物）来提供 `Tracked<dyn World>`，完整可运行性「待本地验证」。

## 6. 本讲小结

- `Library` 是标准库的完整快照：`global`/`math` 两个模块、`styles`、`rules`、`std` 绑定、`features` 位集、`formats` 列表，外加 `routines` 函数指针表；由 `LibraryExt::builder(formats).with_inputs(..).with_features(..).build()` 装配。
- `build()` 先做 `math::module()`，再调 `global()` 组装全局作用域，然后从 `ROUTINES` 取基础规则并叠加各格式规则；global 模块本体被克隆一份包成 `std` 绑定。
- `global()` 的装配是「八个类别模块 define + format + math + prelude」的固定顺序；每个模块用 `start_category`/`reset_category` 三明治给自己那批绑定盖分类章，`define()` 把当前章写进 `Binding.info`。
- `Scope` 是 `IndexMap<EcoString, Binding>`；`Binding` = 值 + Span + 种类 + 带外元数据（category/feature/文档）；求值器用 `Scopes` 栈查找名字，标准库是 `base` 回落层。
- prelude 注入 36 个常量与函数（颜色/方向/对齐），`prelude_path` 再把 `format.html`、`format.pdf`、`format.bundle.asset` 克隆提升到顶层，克隆继承特性门。
- CLI 的 `--input key=value` 经 SystemWorld → `with_inputs` → `sys::module` 变成脚本里的 `#sys.inputs.key`。

## 7. 下一步学习建议

- 下一讲 u3-l3「Value 类型系统」：本讲反复出现的 `define(name, value: impl IntoValue)` 里的 `IntoValue` 到底有哪些实现？`str`/`array`/`dict` 的方法又是怎么注册进各自类型的？从 [foundations/value.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs) 和 `#[scope]` 宏（它生成 [foundations/scope.rs:L314-L321](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs#L314-L321) 的 `NativeScope` 实现）入手。
- 想看「格式如何带来自己的规则」，预读 [typst-library/src/format.rs:L1-L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs#L1-L73) 的模块文档（内含自定义 Epub 格式的完整示例），为 u5-l3 铺路。
- 想理解 `math` 模块为何独立，可以跳读 [typst-eval/src/lib.rs:L40-L47](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L40-L47) 的 `eval` 入口，看看 `Scopes::new(Some(library))` 如何成为一切求值的起点（u3-l6 展开）。
