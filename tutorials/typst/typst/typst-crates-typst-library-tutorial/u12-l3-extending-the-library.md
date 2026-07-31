# 扩展 typst-library：新增元素与函数（综合实践）

> 本讲是「高级与扩展」单元的收口篇，也是整本手册的实战总检阅。前面十几讲我们把 Typst 标准库的零件一个个拆开看过；这一讲要把它们重新拼起来——回答一个最实际的问题：**如果我在 fork 里想给 Typst 加一个新的元素或函数，到底要动哪几处、按什么顺序写？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `Scope` 的四个 `define_*` 方法各自注册什么、以及它们最终都汇入哪一个底层方法。
2. 独立写出「新增一个原生元素」的完整改动清单：`#[elem]` 宏、字段标注、能力 trait 实现、`define_elem` 注册、模块声明与 `pub use`。
3. 独立写出「新增一个原生函数」的完整改动清单：`#[func]` 宏、参数标注、（可选）`#[scope]` 子作用域、`define_func` 注册。
4. 用 `cast!` 宏为自定义 Rust 类型补上 Typst 值与 Rust 值之间的双向转换。
5. 理解元素「参与 show/set 规则」在本 crate 内的真正落地形式（`ShowSet` / `Synthesize`），不再被「Show 能力」这个旧说法误导。

## 2. 前置知识

本讲是综合实践，不再从头讲底层机制，而是把下列讲义的结论当已知前提直接调用。如果你对某条感到陌生，建议先回看对应讲义：

- **u3-l3（elem 宏、字段系统与 Packed）**：`#[elem]` 宏在编译期生成五块代码（struct 改写、`NativeElement`、字段 trait、`Construct`/`Set`、静态 `ContentVtable`）；字段标注分七类（`required`/`default`/`synthesized`/`ghost`/`fold`/`parse`/`external`）；能力 trait 必须写在 `Packed<E>` 而非 `E` 上。
- **u3-l4（func 宏、NativeFunc 与 Args）**：`#[func]` 宏生成清洗后的 `fn`、影子类型（零变体枚举）、静态 `NativeFuncData`、参数解析闭包；`#[scope]` 让函数拥有子作用域（如 `assert.eq`）。
- **u4-l2（属性、set 规则、Selector 与 Recipe）**：`set` 规则经 `Element::set` 变成 `Property` 推入 `Styles`；`show` 规则产 `Recipe`；元素的内置样式经 `ShowSet` 产出。
- **u2-l3（cast、Type、Module 与 Scope）**：三段式转换模型 `Reflect`/`IntoValue`/`FromValue`；`cast!` 宏的三类写法（字面量分支、类型输入分支、`self` 输出分支）；`Scope` 是有序的名字→`Binding` 映射。

一句话回顾贯穿全手册的主线（本讲同样成立）：**typst-library 只负责「定义元素/函数/类型 + 归一化数据」，真正的求值、realization、排版行为住在 typst-eval / typst-realize / typst-layout 等行为 crate，运行期经 `Routines` 函数指针回调。** 所以「扩展本 crate」绝大多数时候是「加定义 + 加注册行」，很少需要碰算法。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来讲什么 |
|------|------|----------------|
| [src/foundations/scope.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs) | `Scope` 的定义与构造方法 | 四个 `define_*` 方法的实现 |
| [src/foundations/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs) | foundations 模块装配 + `panic`/`assert`/`eval` 函数 | `define_type`/`define_func` 的真实调用现场 |
| [src/model/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs) | model 模块装配 | 一长串 `define_elem` 调用 |
| [src/model/heading.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs) | `HeadingElem` 全文 | 「带能力的复杂元素」标准模板 |
| [src/model/strong.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/strong.rs) | `StrongElem` | 「最小元素」模板（一个字段 + required body） |
| [src/model/divider.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/divider.rs) | `DividerElem` | 「空字段 + ShowSet」模板 |
| [src/foundations/repr.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/repr.rs) | `repr` 函数 | 「最小函数」模板（一个位置参数） |
| [src/foundations/func.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs) | `NativeFunc`/`NativeFuncData` | 函数的运行时元数据形状 |
| [src/foundations/cast.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/cast.rs) | `Reflect`/`cast!` 重导出 | 转换模型的入口 |
| [src/layout/spacing.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs) | `Spacing` 的 `cast!` | 多分支 `cast!` 的标准写法 |
| [src/foundations/content/element.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs) | `Synthesize`/`ShowSet`/`Set` 能力 trait | 「show 能力」的真正定义处 |

## 4. 核心概念与源码讲解

### 4.1 注册全景：Scope 的四个 define 方法

#### 4.1.1 概念说明

「新增元素/函数/类型」的最后一步，永远是**把它注册进某个 `Scope`**。回忆 u1-l3：标准库装配就是把定义一个个塞进全局 `Scope`（一个有序的 `名字 → Binding` 映射）。`Scope` 提供了一族 `define_*` 方法，它们看似不同，实则都汇入同一个底层方法 `define`。理解这一点能帮你记住：**注册的本质就是「起一个名字，绑一个 `Value`」**，`define_*` 只是为三类常见 `Value`（元素句柄、函数、类型）各提供了一个语法糖。

#### 4.1.2 核心流程

注册一台新定义的「流水线」：

```
写 Rust 定义                 注册                           用户可见
─────────────────────────    ──────────────────────────    ──────────────
#[elem] pub struct FooElem   scope.define_elem::<FooElem>()  → #foo[...] / set foo(...) / show foo
#[func] pub fn bar()         scope.define_func::<bar>()      → #bar(...)
#[ty]   pub struct Baz       scope.define_type::<Baz>()      → 类型 baz（可做 cast 目标）
任意 Value v                  scope.define("name", v)         → #name
```

四个方法里，`define` 是基础，另外三个都是「取名字 + 构造 Value + 调 `define`」的薄包装：

- `define_elem::<E>()`：名字取自元素句柄 `E::ELEM.name()`，值是 `Element` 句柄；
- `define_func::<F>()`：名字取自函数数据 `F::data().name`，值是 `Func`；
- `define_type::<T>()`：名字取自类型 `T::ty().short_name()`，值是 `Type`。

#### 4.1.3 源码精读

四个方法的实现都极短，集中在 [src/foundations/scope.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs)：

[src/foundations/scope.rs:L135-L163](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L135-L163) — 三个 `define_*` 各自取出名字、把句柄包成 `Func`/`Type`/`Element`，再调 `define`。注意它们都是泛型方法，约束在 `NativeFunc`/`NativeType`/`NativeElement` 这三个 trait 上——**这些 trait 恰好就是 `#[func]`/`#[ty]`/`#[elem]` 宏为你生成的**。所以「宏生成 trait 实现 → `define_*` 凭该 trait 取名注册」是一条闭合的链路。

[src/foundations/scope.rs:L176-L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L176-L185) — 底层 `define`：把值包成 `Binding::detached`，盖上当前 `category`（分类标签，见 u1-l3），再 `bind` 进 `IndexMap`。`#[track_caller]` 让「重名注册」panic 时能指回调用处。

真实调用现场见两个装配函数。foundations 模块大量用 `define_type` 与 `define_func`：

[src/foundations/mod.rs:L91-L123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L91-L123) — `foundations::define` 先 `start_category(Foundations)`，随后一口气 `define_type::<bool>()` … `define_type::<Regex>()` 注册类型，再 `define_func::<repr::repr>()`、`define_func::<panic>()`、`define_func::<assert>()`、`define_func::<eval>()` 注册函数，最后用 `define("calc", calc::module())` 把子模块挂成全局键。注意 `repr::repr` 这种**带模块前缀的路径**：函数 `repr` 定义在子模块 `repr` 里，宏生成的影子类型也叫 `repr`，所以注册路径是 `repr::repr`；而 `panic`/`assert`/`eval` 定义在本模块根，路径就是裸名。

model 模块则几乎全是 `define_elem`：

[src/model/mod.rs:L53-L80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L53-L80) — `model::define` 把 `StrongElem`/`HeadingElem`/`TableElem` 等逐个 `define_elem`，唯一一个函数 `numbering` 用 `define_func::<numbering>()`。`if features.is_enabled(Feature::Bundle)` 那行（第 56-58 行）展示了特性开关按需注册（见 u12-l1）。

#### 4.1.4 代码实践

**实践目标**：在源码中确认「名字从哪来」。

**操作步骤**：
1. 打开 [src/model/heading.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs)，确认 `pub struct HeadingElem`。
2. 在 [src/model/mod.rs:L68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L68) 看到 `global.define_elem::<HeadingElem>();`。
3. 思考：用户写的 `#heading(...)` 里的 `heading` 这个名字，是结构体名 `HeadingElem` 变来的吗？

**需要观察的现象 / 预期结果**：`define_elem` 调用的是 `T::ELEM.name()`（见 scope.rs:161-162），而 `ELEM` 与 `name()` 都由 `#[elem]` 宏生成。宏会去掉 `Elem` 后缀、把首字母小写，把 `HeadingElem` 映射成用户可见名 `heading`。所以结构体名与用户名之间隔着一层宏的命名转换——**改结构体名不等于改用户名**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `define_elem`/`define_func`/`define_type` 都不需要你显式传名字参数？

**答案**：因为名字分别来自 `NativeElement::ELEM.name()`、`NativeFuncData::name`、`NativeType::ty().short_name()`，而这些元数据是 `#[elem]`/`#[func]`/`#[ty]` 宏在编译期就生成好的静态数据。`define_*` 方法直接读取它们，省去手写名字、也避免名字与定义不一致。

**练习 2**：若想让一个函数以子模块形式挂到全局（像 `calc`），该用哪个方法？

**答案**：用最底层的 `global.define("名字", 子模块的 Module)`，因为 `Module` 本身就是一个 `Value`，不需要专门的 `define_module`。foundations 里 `global.define("calc", calc::module())` 正是这么做的（[src/foundations/mod.rs:L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L120)）。

---

### 4.2 新增一个原生元素

#### 4.2.1 概念说明

「新增元素」就是把 u3-l3 学的那套搬出来用。一个原生元素需要四样东西：

1. **`#[elem(...)]` 标注的结构体**：宏会生成 struct 改写、`NativeElement`、字段 trait、`Construct`/`Set`、静态 `ContentVtable`（详见 u3-l3）。
2. **字段标注**：用 `#[required]`/`#[default(...)]`/`#[ghost]`/`#[fold]`/`#[parse]` 等声明每个字段如何参与构造与样式（详见 u3-l3）。
3. **（可选）能力 trait 实现**：写在 `Packed<E>` 上，如 `Synthesize`/`ShowSet`/`LocalName`/`Refable`。
4. **注册行**：在所属模块的 `define` 函数里加 `global.define_elem::<E>();`，并在模块 `mod.rs` 里 `mod` + `pub use`。

**关于「show 能力」的一个关键澄清**：你可能在旧资料里见过在 `#[elem(...)]` 里写 `Show` 能力的写法。但在当前代码里，**本 crate 没有任何元素声明 `Show`，本 crate 中也不存在 `pub trait Show`**（你可以用搜索确认：`Show` 只作为 `Tracepoint::Show`、`Recipe` 调试输出等出现，没有任何 `#[elem(..., Show)]`）。原因是元素的「默认显示」（realization）由行为 crate typst-realize 经 `Routines` 自动驱动，已不再需要在元素侧显式声明。那么「元素如何参与 show 规则」？两层：

- 所有元素**天然**能被 `show` 规则匹配与改写（这由 realize 循环处理，不在本 crate）；
- 若想提供「即便用户写了 show 规则也仍然生效」的**内置样式**，在本 crate 内的实现方式是实现 **`ShowSet`** 能力 trait（在 show 之前把样式 `set` 上去）；若想在 show 之前**合成字段**（如标题编号），实现 **`Synthesize`**。

所以本讲把「show 能力」落地为 `ShowSet`/`Synthesize`，这是当前代码的真实形态。

#### 4.2.2 核心流程

新增元素 `FooElem` 的完整改动清单（以 `model` 模块为例）：

```
1. 新建 src/model/foo.rs
   ├─ #[elem(since = "...", <能力>)] pub struct FooElem { ...字段... }
   ├─ impl <能力> for Packed<FooElem> { ... }   // 如 ShowSet/Synthesize/LocalName
2. src/model/mod.rs
   ├─ mod foo;
   ├─ pub use self::foo::*;
   └─ 在 define() 里加: global.define_elem::<FooElem>();
3. （若引入新依赖类型）补 cast! / Reflect
```

字段标注决策树（简化）：

| 字段需求 | 标注 |
|----------|------|
| 用户必须提供、无默认 | `#[required]` |
| 有默认值，可被 `set` 覆盖 | `#[default(...)]`（即 settable） |
| 只活样式链、不进 struct | `#[ghost]` |
| 多层样式要折叠（如字号相乘） | `#[fold]` |
| 编译期由其它字段算出、不参与相等比较 | `#[synthesized]` |
| 覆盖参数解析逻辑 | `#[parse]` |

#### 4.2.3 源码精读

**最小元素模板**——`StrongElem`，只有一个 `#[default]` 字段加一个 `#[required]` body：

[src/model/strong.rs:L21-L35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/strong.rs#L21-L35) — `#[elem(..., Locatable, Tagged)]` 列出能力；`#[default(300)] pub delta: i64` 是可被 `set strong(delta: ...)` 覆盖的样式字段；`#[required] pub body: Content` 是必填内容。它**没有**任何手写的 `impl` 块——纯靠宏生成的默认 `Construct`/`Set` 与 realize 的默认显示工作。这是能写出的最短元素。

**空字段 + ShowSet 模板**——`DividerElem`：

[src/model/divider.rs:L40-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/divider.rs#L40-L58) — 结构体 `pub struct DividerElem {}` 一个字段都没有，但声明了 `ShowSet` 能力，并在 [src/model/divider.rs:L43-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/divider.rs#L43-L58) 的 `impl ShowSet for Packed<DividerElem>` 里产出内置样式（上下间距、线条长度与描边）。注意能力 trait 的对象类型是 `Packed<DividerElem>`，不是 `DividerElem`（u3-l3 反复强调的约定）。`ShowSet` 能力 trait 本身定义在 [src/foundations/content/element.rs:L277-L281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L277-L281)。

**带能力的复杂元素模板**——`HeadingElem`，这是本讲的范本：

[src/model/heading.rs:L76-L86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L76-L86) — `#[elem(...)]` 一次声明了七个能力：`Locatable`/`Tagged`/`Synthesize`/`Count`/`ShowSet`/`LocalName`/`Refable`/`Outlinable`。声明在 `#[elem]` 里的能力，宏会为它们在 vtable 里预留查询槽（详见 u3-l2）。

字段层面覆盖了多种标注，值得逐一看：

- [src/model/heading.rs:L106](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L106) `pub level: Smart<NonZeroUsize>` —— 无 `#[default]` 的 settable 字段，类型用 `Smart<T>` 表「可 auto」。
- [src/model/heading.rs:L115-L116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L115-L116) `#[default(NonZeroUsize::ONE)] pub depth` —— 带默认值的 settable 字段。
- [src/model/heading.rs:L154-L156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L154-L156) `#[internal] #[synthesized] pub numbers` —— 既对用户隐藏（`#[internal]`），又由 `Synthesize` 阶段回填、不参与相等比较（`#[synthesized]`）。
- [src/model/heading.rs:L235-L236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L235-L236) `#[required] pub body: Content` —— 唯一的必填字段。

能力实现也都写在 `Packed<HeadingElem>` 上：

[src/model/heading.rs:L248-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L248-L286) `impl Synthesize` —— 在 show 之前把 `level` 落定、把 `supplement` 解析成内容、把编号 `numbers` 回填进 `#[synthesized]` 字段。

[src/model/heading.rs:L288-L309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L288-L309) `impl ShowSet` —— 按 `level` 算出字号缩放、字重加粗、上下间距、`sticky`，作为内置样式 `set` 上去。这就是「标题默认长成那样」的源头，也是本讲所说的「show 能力」的真实落地处。

[src/model/heading.rs:L356-L358](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/heading.rs#L356-L358) `impl LocalName` —— 一行 `const KEY: &'static str = "heading";`，让本地化名称查表能找到 heading（详见 u7-l3）。

#### 4.2.4 代码实践

**实践目标**：仿照 `heading.rs`/`divider.rs`，设计一个最小自定义元素 `CalloutElem`（带 `#[required]` 字段与 `ShowSet` 能力），写出它所需的 `#[elem]`、字段标注与注册行。

**操作步骤**：在一个 fork 里新建 `src/model/callout.rs`，写入下面的代码（**示例代码**，非项目原有代码；其字段类型、能力名都对照 `heading.rs`/`divider.rs`/`strong.rs` 中已验证可编译的写法）：

```rust
// 示例代码：src/model/callout.rs
use crate::foundations::{Content, Packed, ShowSet, StyleChain, Styles, elem};
use crate::layout::Em;
use crate::text::{FontWeight, TextElem, TextSize};

/// A highlighted callout.（文档注释会变成用户文档）
#[elem(since = "forever", ShowSet)]          // 声明 ShowSet 能力
pub struct CalloutElem {
    /// Whether to render the callout prominently.
    #[default(false)]                         // 可被 set callout(important: true) 覆盖
    pub important: bool,

    /// The callout's content.
    #[required]                               // 必填位置参数
    pub body: Content,
}

// 「show 能力」的落地：内置样式，即使用户写了 show 规则也生效
impl ShowSet for Packed<CalloutElem> {
    fn show_set(&self, _: StyleChain) -> Styles {
        let mut out = Styles::new();
        // 写法对照 heading.rs L302-L303
        out.set(TextElem::size, TextSize(Em::new(1.1).into()));
        out.set(TextElem::weight, FontWeight::BOLD);
        out
    }
}
```

然后在 [src/model/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs) 做三处改动（对照该文件既有的 `mod heading;` / `pub use self::heading::*;` / `global.define_elem::<HeadingElem>();`）：

```rust
// 示例代码：src/model/mod.rs 三处增量
mod callout;                       // 1. 声明模块（紧挨 mod heading;）
pub use self::callout::*;          // 2. 重导出（紧挨 pub use self::heading::*;）
// 在 define() 里，HeadingElem 那一行旁边：
global.define_elem::<CalloutElem>();   // 3. 注册
```

**需要观察的现象 / 预期结果**：

1. 重新构建后，用户能写 `#callout[注意]`，内容会以加粗、1.1em 显示。
2. `#set callout(important: true)` 能覆盖 `important` 字段（因为它是 settable）。
3. `#show callout: set text(red)` 之类的 show 规则照常生效——`ShowSet` 产出的样式与用户 show 规则各司其职。
4. 若忘记第 3 步（`define_elem`），元素定义存在但用户作用域里访问不到，会报 `unknown variable: callout`。

**待本地验证**：以上行为需在你自己的 fork 中 `cargo build` 并用一个最小 `.typ` 文件实测确认。

#### 4.2.5 小练习与答案

**练习 1**：把上面 `CalloutElem` 的 `ShowSet` 改成 `Synthesize` 会有什么不同？

**答案**：`ShowSet`（[element.rs:L277-L281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L277-L281)）产出**样式**（`Styles`），作用于「环境」；`Synthesize`（[element.rs:L267-L271](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L267-L271)）是**改写元素自身的字段**（`&mut self`），发生在 show 之前。两者不互斥：`HeadingElem` 同时实现了二者（合成编号 + 设置字号）。

**练习 2**：为什么能力 trait 要写 `impl ShowSet for Packed<CalloutElem>` 而不是 `for CalloutElem`？

**答案**：因为 vtable 与能力查询的对象类型都是 `Packed<E>`（类型擦除后的具体句柄），而非裸 `E`（u3-l3 已述）。写在 `E` 上不会被能力查询 `can::<C>()`/`with::<C>()` 找到。

---

### 4.3 新增一个原生函数

#### 4.3.1 概念说明

函数比元素更简单——它不需要参与 set/show，只是「参数 → 返回值」。新增函数需要：

1. **`#[func(...)]` 标注的 Rust 函数**：宏生成清洗后的 `fn`、影子类型（零变体枚举，如 `enum panic {}`）、静态 `NativeFuncData`、参数解析闭包（详见 u3-l4）。
2. **参数标注**：位置参数直接写类型；命名参数加 `#[named]`；有默认值加 `#[default(...)]`（会改用 `eat()` 解析）；可变参数加 `#[variadic]`（用 `all()` 取全部）。
3. **（可选）`#[scope]` 子作用域**：让函数拥有 `func.sub()` 形式的子函数（如 `assert.eq`）。
4. **注册行**：`global.define_func::<F>();`。

函数的返回值靠 `IntoResult<Value>` 统一：你可以直接返回 `Value`、`Str`、`Content`，也可以返回 `SourceResult<T>`/`StrResult<T>` 表「可能失败」（u3-l4、u5-l3）。

#### 4.3.2 核心流程

```
1. 在某模块（如 src/model/mod.rs 或新建子文件）写
   ├─ #[func(...)] pub fn foo( #[named] #[default(..)] x: T, ... ) -> R { ... }
   ├─ （可选）#[scope] impl foo { #[func] pub fn sub(..){..} }
2. 在该模块 define() 里加: global.define_func::<foo>();
```

参数解析速查（详见 u3-l4 的 `create_param_parser`）：

| 标注 | 解析方式 |
|------|----------|
| 无标注、必填位置 | `expect::<T>()`（缺失报错） |
| `#[default(...)]` | `eat::<T>().unwrap_or(default)` |
| `#[named]` | `named::<T>()`（仅命名参数） |
| `#[named] #[default]` | `named::<T>()` 缺省回退 |
| `#[variadic]` | `all::<T>()`（吃掉剩余位置参数） |

#### 4.3.3 源码精读

**最小函数模板**——`repr`：

[src/foundations/repr.rs:L41-L47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/repr.rs#L41-L47) — 一个位置参数 `value: Value`，返回 `Str`，函数体只有一行 `value.repr().into()`。文档注释里的 `///` 会成为用户文档，```` ```example ```` 块会成为文档示例。注册见 [src/foundations/mod.rs:L114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L114) `global.define_func::<repr::repr>();`（注意模块前缀）。

**带可变参数与 StrResult 的函数**——`panic`：

[src/foundations/mod.rs:L135-L155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L135-L155) — `#[variadic] values: Vec<Value>` 吃掉所有位置参数；返回 `StrResult<Never>` 表「必失败」。`#[func(since = "forever", keywords = ["error"])]` 里的 `keywords` 会被文档搜索索引收录。

**带命名参数 + scope 的函数**——`assert`：

[src/foundations/mod.rs:L169-L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L169-L185) — `condition: bool` 是必填位置参数；`#[named] message: Option<EcoString>` 是可选命名参数（缺失即为 `None`）。`#[func(scope, ...)]` 的 `scope` 标志告诉宏「这个函数有子作用域」。

[src/foundations/mod.rs:L187-L254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L187-L254) — `#[scope] impl assert { ... }` 收集 `impl` 块里的 `#[func]` 方法为子函数。于是 `assert` 既有构造器本体，又能 `assert.eq(...)`/`assert.ne(...)`。子函数经 `Func::field` 访问（u3-l4）。

函数运行时元数据形状见 [src/foundations/func.rs:L621-L656](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs#L621-L656)：`NativeFunc` trait 暴露 `data()`，`NativeFuncData` 含 `function`（实现指针）、`name`、`scope`、`params`、`returns` 等字段——这些就是 `#[func]` 宏为你填好的静态表，`define_func` 据此把函数挂进作用域。

#### 4.3.4 代码实践

**实践目标**：仿照 `repr`/`panic`，写一个自定义函数 `greet`，并给它一个 `greet.formal` 子函数，最后用 `define_func` 注册。

**操作步骤**：在 fork 的 `src/foundations/mod.rs`（或新文件）加入（**示例代码**；`Str` 实现了 `Display` 与 `From<EcoString>`，见 [src/foundations/str.rs:L712](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/str.rs#L712) 与 [src/foundations/str.rs:L792](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/str.rs#L792)，故可编译）：

```rust
// 示例代码
use ecow::eco_format;
use crate::foundations::{Str, func, scope};

/// Greets someone.
#[func(title = "Greet", since = "forever")]
pub fn greet(
    /// The name to greet.
    name: Str,
) -> Str {
    eco_format!("Hi, {name}!").into()      // Str: Display + From<EcoString>
}

#[scope]
impl greet {
    /// A more formal greeting.
    #[func(title = "Formal Greet")]
    pub fn formal(
        /// The name to greet.
        name: Str,
    ) -> Str {
        eco_format!("Greetings, {name}.").into()
    }
}
```

注册（对照 [src/foundations/mod.rs:L115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L115) 旁）：

```rust
// 示例代码：在 foundations::define 里
global.define_func::<greet>();
```

**需要观察的现象 / 预期结果**：

1. `#greet("World")` 输出 `Hi, World!`。
2. `#greet.formal("World")` 输出 `Greetings, World.`——子函数因带 `parent` 不生成影子类型，只生成 `fn_data()`，经父函数的 scope 访问（u3-l4）。
3. 若把 `name: Str` 改成 `name: &str`，编译会失败——参数类型必须实现 `FromValue`，而 `&str` 没有。

**待本地验证**：需在 fork 中实测。

#### 4.3.5 小练习与答案

**练习 1**：想让 `greet` 的 `name` 参数可选、缺省时用 `"friend"`，怎么改？

**答案**：把签名改成 `#[default("friend".into())] name: Str`（或先 `name: Option<Str>` 再 `#[default]`）。`#[default]` 会让宏改用 `eat()` 解析、缺失时回退默认值（u3-l4 的解析规则表）。

**练习 2**：`define_func::<greet>()` 里的 `greet` 是函数本身还是别的什么？

**答案**：是 `#[func]` 宏生成的**影子类型**（零变体枚举 `enum greet {}`），不是那个 Rust 函数。`define_func` 的泛型约束是 `NativeFunc`，影子类型实现了该 trait 并在 `data()` 里给出静态元数据；真正的函数体被宏包进闭包挂在 `NativeFuncData::function` 上（u3-l4）。

---

### 4.4 新增自定义类型与 cast!

#### 4.4.1 概念说明

当你的新元素/函数用到了**项目里原本没有的 Rust 类型**作为字段或参数（比如一个自定义枚举），你需要让它能和 Typst `Value` 互转——这正是 u2-l3 讲的三段式模型 `Reflect`/`IntoValue`/`FromValue` 的工作。手写这三个 trait 很繁琐，`cast!` 宏用一种紧凑的三段语法一次生成它们。

`cast!` 的三段式（u2-l3）：

```
cast! {
    TargetType,
    self => <表达式>,            // 「输出分支」：TargetType -> Value（IntoValue）
    v: SomeType => <转 Target>,  // 「类型输入分支」：SomeType 这个 Value 可转成 Target（FromValue）
    "literal" => <转 Target>,    // （可选）「字面量分支」：特定字面量可转成 Target
}
```

不是每种类型都需要全部三段；最常见的是「一个 `self =>` 输出 + 若干 `v: T =>` 输入」。

#### 4.4.2 核心流程

```
1. 定义 Rust 类型（struct/enum），derive必要的 trait：Debug/Clone/PartialEq/Hash
2. 写 cast! { ... } 生成 Reflect/IntoValue/FromValue
3. （若要作为一等「类型」暴露给用户）加 #[ty] + define_type::<T>()
   —— 元素字段类型通常不需要这步，只要 cast! 即可参与字段 cast
```

#### 4.4.3 源码精读

最清晰的多分支 `cast!` 例子是 `Spacing`：

[src/layout/spacing.rs:L177-L193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs#L177-L193) — 三段齐全：

- `self => match self { ... }`：**输出分支**。把 `Spacing` 的两个变体分别转成最贴切的 `Value`（`Rel` 部分若为零就只输出绝对量等，做人性化表示）。
- `v: Rel<Length> => Self::Rel(v)`：**类型输入分支 1**。传入一个相对长度就构造成 `Spacing::Rel`。
- `v: Fr => Self::Fr(v)`：**类型输入分支 2**。传入一个分数就构造成 `Spacing::Fr`。

于是用户写 `1fr` 或 `2cm` 都能被 `FromValue` 解析成 `Spacing`，且 `Spacing` 转 `Value` 时会按内容挑最简形式。这正是「同一概念接受多种字面量」的标准实现。

更短的「self 输出 + 单类型输入」例子见 `HAlignment`：

[src/layout/align.rs:L379-L383](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/align.rs#L379-L383) — 输出分支把 `HAlignment` 包成 `Alignment::H(self).into_value()`；输入分支从 `Alignment` 经 `try_from` 还原，失败时用 `?` 传播 `StrResult`（这是输入分支允许返回 `StrResult` 的典型用法）。

`cast!` 宏本身经 [src/foundations/cast.rs:L3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/cast.rs#L3) `pub use typst_macros::{cast, Cast};` 重导出，`Reflect` trait 的职责定义在同文件 [src/foundations/cast.rs:L33-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/cast.rs#L33-L58)（产出 `CastInfo`，复用于文档、补全与「expected X, found Y」错误）。

#### 4.4.4 代码实践

**实践目标**：为 `CalloutElem` 增加一个自定义的「严重程度」字段类型 `Severity`，并用 `cast!` 让它接受字符串字面量。

**操作步骤**：在 `src/model/callout.rs` 顶部加（**示例代码**；写法对照 `OuterHAlignment` 的 `cast!` 与 [src/layout/spacing.rs:L177-L193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs#L177-L193)）：

```rust
// 示例代码
use crate::foundations::{Str, Value, cast};

#[derive(Debug, Clone, Copy, PartialEq, Hash)]
pub enum Severity { Info, Warn, Error }

cast! {
    Severity,
    self => match self {                       // 输出分支
        Self::Info  => "info".into_value(),
        Self::Warn  => "warning".into_value(),
        Self::Error => "error".into_value(),
    },
    v: Str => match v.as_str() {               // 类型输入分支：从字符串还原
        "info" | "i"     => Self::Info,
        "warning" | "w"  => Self::Warn,
        "error" | "e"    => Self::Error,
        _ => bail!("expected info/warning/error, found {}", v),
    },
}
```

然后给 `CalloutElem` 加一个字段：

```rust
// 示例代码：接在 CalloutElem 结构体里
    /// How severe the callout is.
    #[default(Severity::Info)]
    pub severity: Severity,
```

`Severity` 已实现 `Reflect`/`IntoValue`/`FromValue`，故可直接作为 settable 字段类型，无需再改其它地方。

**需要观察的现象 / 预期结果**：

1. `#set callout(severity: "error")` 能解析字符串为 `Severity::Error`。
2. 在 show 规则里 `it.severity` 取出的是字符串值 `"error"`（因为输出分支把它转成了 `Value::Str`）。
3. 传 `severity: "fatal"` 会触发 `bail!` 报错。

**待本地验证**：需在 fork 中实测。注意 `Severity` 只能参与字段 cast；若想让用户也见到一个 `severity` **类型**（可做 `type-of`、可被 `#[ty]` 注册），还需另加 `#[ty]` 并 `define_type`，但绝大多数字段类型不需要这步。

#### 4.4.5 小练习与答案

**练习 1**：`cast!` 的输出分支（`self =>`）生成的是哪个 trait 的实现？

**答案**：`IntoValue`（`self: TargetType → Value`，永不失败）。输入分支（`v: T =>`）生成的是 `FromValue`（可能失败，故可用 `?`/`bail!`）。`Reflect` 则由宏综合输入分支信息生成 `CastInfo`（u2-l3）。

**练习 2**：为什么 `Spacing` 的输出分支要写一个 `match`、而不是简单 `self.into_value()`？

**答案**：因为 `Spacing` 有两个变体（`Rel`/`Fr`），且希望「能化简就化简」（如 `Rel` 的百分比部分为零时只输出绝对量），需要按变体分别挑选最人性化的 `Value` 表示。输出分支的本质就是「决定这个类型在用户面前以什么面貌出现」。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个迷你功能：**为 fork 新增一个 `CalloutElem` 元素 + 一个 `severity` 自定义类型 + 一个 `greet` 函数，并全部注册到标准库**。

请按顺序完成并自检：

1. **类型先行**：在 `src/model/callout.rs` 定义 `Severity` 枚举与它的 `cast!`（4.4.4）。
2. **元素**：在同一文件定义 `CalloutElem`，含 `#[required] body`、`#[default] severity: Severity`、`#[default(false)] important: bool`，并 `impl ShowSet for Packed<CalloutElem>` 产出内置样式（4.2.4）。再 `impl LocalName for Packed<CalloutElem>`（`const KEY = "callout"`）。
3. **函数**：在 `src/foundations/mod.rs` 加 `greet` 及其 `#[scope] impl greet { fn formal }`（4.3.4）。
4. **注册**：
   - `src/model/mod.rs`：`mod callout;` + `pub use self::callout::*;` + `global.define_elem::<CalloutElem>();`
   - `src/foundations/mod.rs` 的 `define()`：`global.define_func::<greet>();`
5. **自检清单**（逐条对照源码确认）：
   - [ ] 结构体的 `#[elem]` 列出的每个能力，是否都有对应的 `impl ... for Packed<CalloutElem>`？
   - [ ] 字段类型 `Severity` 是否有 `cast!`（或已实现 `FromValue`）？
   - [ ] 注册名是否与用户期望一致？（`CalloutElem → callout`，由宏命名转换）
   - [ ] 是否在正确的 `define()` 里注册、并夹在 `start_category`/`reset_category` 之间（见 [src/model/mod.rs:L54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L54) 与 [src/model/mod.rs:L79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/mod.rs#L79)）？
6. **验证**：写一个最小 `.typ` 文件：

   ```typ
   #set callout(severity: "error")
   #show callout: it => block(fill: luma(230))[#it]
   #callout[这里很重要]
   #greet("Typst") / #greet.formal("Typst")
   ```

   预期：callout 带内置加粗样式、被 show 规则套上灰底 block；`greet`/`greet.formal` 输出两句问候。

   **待本地验证**：在 fork 中 `cargo build` 后用 typst CLI 实测。

> 完成这个练习后，你就把 u3-l3（elem/字段/Packed）、u3-l4（func/Args/scope）、u4-l2（set/show/ShowSet）、u2-l3（cast/Scope/define）四讲的知识在真实代码里走通了一遍——这正是「综合实践」的意义。

## 6. 本讲小结

- **注册的本质**：四个 `define_*` 方法都汇入 [scope.rs:L176](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L176) 的底层 `define`——「起名、绑 `Value`、盖分类」；`define_elem`/`define_func`/`define_type` 只是取宏生成的名字与句柄的薄包装。
- **新增元素四步**：`#[elem]` 结构体 + 字段标注 + 能力 trait（写在 `Packed<E>`）+ `define_elem` 注册（外加 `mod`/`pub use`）。
- **新增函数三步**：`#[func]` 函数 + 参数标注（+ 可选 `#[scope]`）+ `define_func` 注册；返回值靠 `IntoResult<Value>` 统一，可返回 `StrResult`/`SourceResult` 表失败。
- **关于 show 能力**：当前代码里元素不再声明 `Show` 能力，默认 realization 由 typst-realize 经 `Routines` 驱动；本 crate 内「参与 show」的落地形式是 `ShowSet`（内置样式）与 `Synthesize`（合成字段），二者写在 `Packed<E>` 上。
- **自定义类型**：用 `cast!` 三段式（`self =>` 输出 + `v: T =>` 输入）一次生成 `Reflect`/`IntoValue`/`FromValue`，即可让新 Rust 类型充当字段/参数类型。
- **贯穿主线不变**：本 crate 只做「定义 + 注册 + 归一化」，真正的求值/realization/排版仍在行为 crate，所以扩展工作集中在「加文件、加标注、加注册行」，几乎不碰算法。

## 7. 下一步学习建议

- **想看「带行为的元素」如何与行为 crate 联动**：阅读 u5-l4（Routines）后，去 typst-realize / typst-layout 源码看 `Show`/realization 与排版的真实循环，理解你写的 `ShowSet` 样式是如何被消费的。
- **想做更严肃的元素**：精读 [src/model/table.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/table.rs) 与 [src/layout/grid/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/grid/mod.rs)，看一个同时用 `#[parse]`、`#[synthesized]`、`Synthesize`、`Celled` 归一化的复杂元素如何组装。
- **想发布给最终用户**：除了改本 crate，还需更新文档生成（docs crate 会扫文档注释与 ```` ```example ````）、补测试（`tests` 目录的离线 snapshot 测试）、留意特性开关（u12-l1）——若新元素依赖不稳定 API，考虑用 `Feature` 门控。
- **回到手册全局**：若想再巩固宏生成的细节，可重读 u3-l3（elem 宏展开）与 u3-l4（func 宏展开），对照本讲你手写的标注，看每一行标注分别触发了宏的哪段代码生成。
