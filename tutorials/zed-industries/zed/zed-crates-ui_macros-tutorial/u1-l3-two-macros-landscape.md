# 全景图：两个宏与它们的消费者

> 前置讲义：u1-l1（ui_macros 是什么）、u1-l2（过程宏 crate 的骨架）。
> 本讲不再重复"过程宏三种形式"和 `TokenStream` 的基础知识，直接站在全局视角看这两个宏在 Zed 仓库里被谁调用、产出什么、又被谁消费。

## 1. 本讲目标

学完本讲，你应该能够：

- 准确说出 `derive_dynamic_spacing!` 在整个 Zed 仓库中的**唯一调用点**（`crates/ui/src/styles/spacing.rs`）以及它的产物（`DynamicSpacing` 枚举，含 14 个变体）。
- 准确说出 `RegisterComponent` 的作用：在编译期生成"trait 断言 + 注册函数 + `inventory` 提交"三段代码，最终在运行时把组件登记进 `component::ComponentRegistry`。
- 亲手画出（或看懂）`ui_macros` 与 `ui`、`component`、`theme`、`workspace` 等 crate 之间的关系图，并区分两类完全不同的箭头：**Cargo 依赖箭头**与**宏展开箭头**。

## 2. 前置知识

前两讲已经建立了这些认知，本讲直接使用：

- `ui_macros` 是一个 `proc-macro = true` 的 crate，正式依赖只有 `syn` 和 `quote`；
- 函数式宏（`#[proc_macro]`）的输出会**替换**调用点，派生宏（`#[proc_macro_derive]`）的输出会**追加**在被标注的类型之后。

本讲还需要补充三个新概念：

1. **调用点（call site）**：源码里实际写下宏调用的那一行。统计调用点是理解一个宏"影响面"的最直接手段——`derive_dynamic_spacing!` 全仓库只有 1 个调用点，而 `RegisterComponent` 有 60 多个。
2. **再导出（re-export）**：一个 crate 用 `pub use` 把别的 crate 的名字搬到自己的命名空间下。`ui` crate 的 prelude 同时再导出了 `RegisterComponent` 宏和 `DynamicSpacing` 枚举，所以组件文件只需要 `use crate::prelude::*` 一行就能同时拿到两者。
3. **分布式注册（inventory 的直觉理解）**：传统做法是维护一份"全部组件"的中心清单，新增组件要同时改清单；而 Zed 借助 [`inventory`](https://docs.rs/inventory) crate 让**每个组件自己提交注册项**，链接器自动把它们汇入同一个集合，不需要任何中心清单。细节留到单元四精读，本讲只需要这个直觉。

另外回顾一个运行时概念：Zed 允许用户在设置里选择 UI 密度（UI Density），`theme` crate 定义了三档 `UiDensity`：`Compact`（紧凑）、`Default`（默认）、`Comfortable`（宽松）。同一个间距值在三档下应对应不同像素——这正是 `DynamicSpacing` 存在的理由。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L43) | 两个宏的声明入口（仅 43 行） | 声明属性与 doc 注释 |
| [src/dynamic_spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L167) | `derive_dynamic_spacing!` 的实现 | 只看生成的产物骨架，精读留给单元二 |
| [src/derive_register_component.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L1-L29) | `RegisterComponent` 的实现（仅 29 行） | 生成哪三段代码 |
| [../ui/src/styles/spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L1-L55) | `derive_dynamic_spacing!` 的唯一调用点 | 宏的真实输入清单 |
| [../ui/src/components/tooltip.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tooltip.rs#L1-L292) | `RegisterComponent` 的典型消费者 | 派生 + `impl Component` 的完整搭配 |
| [../component/src/component.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L1-L326) | 注册表所在的 crate | `init()`、`COMPONENT_DATA`、`Component` trait |
| [../ui/src/prelude.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/prelude.rs#L1-L36) | `ui` crate 的"工具箱总入口" | 把两个宏的产物汇聚到一处 |
| [../theme/src/ui_density.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs#L21-L32) | `UiDensity` 三档设置的定义 | 生成代码在运行时查询的枚举 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- 4.1 `derive_dynamic_spacing` 宏
- 4.2 `RegisterComponent` 宏
- 4.3 消费者 crate（ui / component / theme）全景

### 4.1 derive_dynamic_spacing 宏：一个调用点，一个枚举

#### 4.1.1 概念说明

`derive_dynamic_spacing!` 是一个**函数式过程宏**，解决的问题非常具体：Zed 的 UI 密度设置要求"同一个间距在三档密度下呈现不同像素值"。如果每个组件都手写这套三档切换逻辑，会重复几百遍。

这个宏的做法是：让 `ui` crate 用一份**间距清单**换回一整个 `DynamicSpacing` 枚举。清单里每一行生成一个枚举变体（如 `Base04`），变体携带三档像素值，使用时调用 `.rems(cx)` 或 `.px(cx)` 即可拿到随用户设置变化的间距。

它与 `RegisterComponent` 最大的使用差异在于**调用点数量**：全仓库（`crates/` 目录下的正式源码）只有一处调用——它本质上是"一次性生成一整套类型"的批处理宏，而不是给业务代码反复使用的工具宏。

#### 4.1.2 核心流程

整体数据流是一条编译期的单向流水线：

```text
crates/ui/src/styles/spacing.rs
    │  写下清单：derive_dynamic_spacing![ (0,0,0), …, 24, 32, 40, 48 ];
    ▼
crates/ui_macros/src/ui_macros.rs:8   derive_dynamic_spacing() 入口
    │  一行转发
    ▼
crates/ui_macros/src/dynamic_spacing.rs:49   derive_spacing()
    │  syn 解析清单 → quote 生成代码
    ▼
一份完整的 Rust 源码（enum DynamicSpacing { … } + impl 块）
    │  函数式宏的输出【替换】调用点，所以这份源码就"长在" spacing.rs 里
    ▼
pub use spacing::*（styles.rs:16）→ ui crate 对外导出 → prelude.rs:15 再次导出
    ▼
全仓库的组件代码：DynamicSpacing::Base04.rems(cx)
```

清单支持两种输入形态，语义不同：

- **三元组** `(a, b, c)`：三档密度**直接使用**给定值，即 Compact 取 \(a\)、Default 取 \(b\)、Comfortable 取 \(c\)；
- **单值** `n`：按标准间距公式推导三档，即 \( \max(n-4,\,0) \;|\; n \;|\; n+4 \)（单位 px）。

无论哪种形态，**变体名都取 Default 档的像素值**，并用 `{:02}` 补零到两位，得到 `BaseXX` 形式。

#### 4.1.3 源码精读

**① 宏的声明**——[src/ui_macros.rs:L6-L10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6-L10) 用 `#[proc_macro]` 声明函数式宏，函数体只有一行转发到 `dynamic_spacing::derive_spacing`：

```rust
/// Generates the DynamicSpacing enum used for density-aware spacing in the UI.
#[proc_macro]
pub fn derive_dynamic_spacing(input: TokenStream) -> TokenStream {
    dynamic_spacing::derive_spacing(input)
}
```

**② 唯一的调用点**——[../ui/src/styles/spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)。注意第 3 行先 `use ui_macros::derive_dynamic_spacing;` 导入，第 29 行以 `宏名![ … ]` 的形式调用（函数式宏允许 `()`、`[]`、`{}` 三种定界符，这里用了 `[]`，让清单看起来像一个数组）：

```rust
derive_dynamic_spacing![
    (0, 0, 0),
    (1, 1, 2),
    (1, 2, 4),
    (2, 3, 4),
    (2, 4, 6),
    (3, 6, 8),
    (4, 8, 10),
    (10, 12, 14),
    (14, 16, 18),
    (18, 20, 22),
    24,
    32,
    40,
    48
];
```

这份清单共 14 个值：前 10 个是三元组，后 4 个（24、32、40、48）是单值。调用点上方的注释（[spacing.rs:L5-L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L5-L28)）是输入格式最好的官方说明，值得通读。

**③ 变体命名规则**——[src/dynamic_spacing.rs:L56-L62](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L56-L62) 决定了每个输入值变成什么变体名：三元组取中间值 `b`，单值取 `n` 本身，然后 `format_ident!("Base{:02}", …)` 补零：

```rust
let variant = match v {
    DynamicSpacingValue::Single(n) => {
        format_ident!("Base{:02}", n.base10_parse::<u32>().unwrap())
    }
    DynamicSpacingValue::Tuple(_, b, _) => {
        format_ident!("Base{:02}", b.base10_parse::<u32>().unwrap())
    }
};
```

**④ 产物的骨架**——[src/dynamic_spacing.rs:L127-L164](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L127-L164) 是 `quote!` 模板，也就是宏的最终产物。骨架是"一个枚举 + 一个 impl 块"，其中 `#(#variant_names,)*` 和 `#(#spacing_ratios,)*` 会按清单批量展开：

```rust
pub enum DynamicSpacing {
    #(
        #[doc = #doc_strings]
        #variant_names,
    )*
}

impl DynamicSpacing {
    fn spacing_ratio(&self, cx: &App) -> f32 {
        const BASE_REM_SIZE_IN_PX: f32 = 16.0;
        match self {
            #(#spacing_ratios,)*
        }
    }
    pub fn rems(&self, cx: &App) -> Rems { … }
    pub fn px(&self, cx: &App) -> Pixels { … }
}
```

**⑤ 生成代码与 theme 的连接点**——[src/dynamic_spacing.rs:L68-L72](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L68-L72) 生成的每个 match 分支都会在**运行时**查询当前密度设置：

```rust
DynamicSpacing::#variant => match ::theme::theme_settings(cx).ui_density(cx) {
    ::theme::UiDensity::Compact => (#n - 4.0).max(0.0) / BASE_REM_SIZE_IN_PX,
    ::theme::UiDensity::Default => #n / BASE_REM_SIZE_IN_PX,
    ::theme::UiDensity::Comfortable => (#n + 4.0) / BASE_REM_SIZE_IN_PX,
}
```

注意这里写的是 `::theme::…` 绝对路径——`ui_macros` 自己并没有依赖 `theme`，这些路径是在**调用方 `ui` crate 的依赖图里解析的**（`ui` 的 Cargo.toml 依赖了 `theme`）。这是本讲 4.3 节的核心伏笔。三档密度对应的 `UiDensity` 枚举定义在 [../theme/src/ui_density.rs:L21-L32](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs#L21-L32)。

**⑥ 消费端的样子**——生成物经 [../ui/src/styles.rs:L16](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles.rs#L16) 的 `pub use spacing::*;` 导出，最终在组件里这样用（例如 [../ui/src/components/button/button.rs:L465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465)）：

```rust
.gap(DynamicSpacing::Base04.rems(cx))
```

#### 4.1.4 代码实践：从清单推导全部变体名

1. **实践目标**：不借助编译器，仅凭 4.1.2 的规则推导出 spacing.rs 清单生成的全部 14 个变体名。
2. **操作步骤**：
   - 打开 [spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)，对每一行先判断形态（三元组还是单值）；
   - 三元组取中间值，单值套公式 \( \max(n-4,0) \;|\; n \;|\; n+4 \) 得到三档像素值；
   - 变体名 = Default 档像素值补零两位，前缀 `Base`。
3. **需要观察的现象**：在编辑器里全局搜索 `DynamicSpacing::Base`，观察 `crates/ui/src/components/` 下实际用到了哪些变体。
4. **预期结果**：你推导出的 14 个变体应与下表一致（参考答案）：

| 输入 | 形态 | Compact / Default / Comfortable (px) | 变体名 |
| --- | --- | --- | --- |
| `(0, 0, 0)` | 三元组 | 0 / 0 / 0 | `Base00` |
| `(1, 1, 2)` | 三元组 | 1 / 1 / 2 | `Base01` |
| `(1, 2, 4)` | 三元组 | 1 / 2 / 4 | `Base02` |
| `(2, 3, 4)` | 三元组 | 2 / 3 / 4 | `Base03` |
| `(2, 4, 6)` | 三元组 | 2 / 4 / 6 | `Base04` |
| `(3, 6, 8)` | 三元组 | 3 / 6 / 8 | `Base06` |
| `(4, 8, 10)` | 三元组 | 4 / 8 / 10 | `Base08` |
| `(10, 12, 14)` | 三元组 | 10 / 12 / 14 | `Base12` |
| `(14, 16, 18)` | 三元组 | 14 / 16 / 18 | `Base16` |
| `(18, 20, 22)` | 三元组 | 18 / 20 / 22 | `Base20` |
| `24` | 单值 | 20 / 24 / 28 | `Base24` |
| `32` | 单值 | 28 / 32 / 36 | `Base32` |
| `40` | 单值 | 36 / 40 / 44 | `Base40` |
| `48` | 单值 | 44 / 48 / 52 | `Base48` |

   其中至少 `Base00`（icon.rs 的图标尺寸）、`Base02`、`Base04`（大量组件的 gap）、`Base06`、`Base08`（modal.rs 的内边距）、`Base12`、`Base32`（tab.rs 的标签宽度）能在 `crates/ui/src/components/` 中直接搜到使用处；个别变体可能暂时无人使用——哪些没人用，**待本地验证**。

本实践为源码阅读型任务，不需要运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `(1, 2, 4)` 生成的变体叫 `Base02` 而不是 `Base01` 或 `Base04`？

**答案**：变体名统一取 **Default 档**的像素值。三元组 `(a, b, c)` 中 Default 档取中间值 \(b\)，这里是 2，补零后即 `Base02`。这样命名的用意是：变体名就是"默认设置下用户看到的间距"，可读性最好（`BaseXX` 里的 XX 与注释里 `px|px|px (@16px/rem)` 的中间那个数一致）。

**练习 2**：如果想在 Zed 里新增一个"默认 28px"的间距，需要改哪个文件、加什么内容？

**答案**：只需在 [spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44) 的清单里加一行 `28`（单值形态，自动按公式展开为 24/28/32），或 `(24, 28, 32)`（三元组形态，显式控制三档）。重新编译后 `DynamicSpacing::Base28` 就存在了。不需要改 `ui_macros` 的任何代码——这正是"清单换枚举"设计的价值。

**练习 3**：`derive_dynamic_spacing!` 的输出替换调用点后，`spacing.rs` 里除了枚举还"多"了一个可以直接调用的函数 `ui_density(cx)`（[spacing.rs:L46-L54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L46-L54)）。它的 doc 注释对间距计算提出了什么要求？

**答案**：它要求**不要**用这个函数来计算间距值（"Do not use this to calculate spacing values"），间距必须统一走 `DynamicSpacing`；这个函数只用于"根据密度设置做其他 UI 调整或展示"。也就是说，密度感知的换算逻辑被有意收敛到宏生成的代码这一条路径上。

---

### 4.2 RegisterComponent 宏：六十多个调用点，一张注册表

#### 4.2.1 概念说明

`RegisterComponent` 是一个**派生宏**，服务于 Zed 的组件预览体系：Zed 可以在一个可视化界面（`component_preview`）里按分类浏览所有 UI 组件的实时渲染效果。要被预览，组件必须把自己登记进一个全局注册表——`component::ComponentRegistry`。

这个宏做的事是：在实现了 `Component` trait 的结构体上标注 `#[derive(RegisterComponent)]`，编译期自动生成三段代码：

1. 一个**编译期断言**：如果该类型没有实现 `Component` trait，直接编译报错（而不是等到运行时才发现组件缺失）；
2. 一个**注册函数**：调用 `component::register_component::<T>()` 把组件元数据写入全局注册表；
3. 一条 **`inventory` 提交**：把注册函数登记进分布式集合，等待运行时统一执行。

与 `derive_dynamic_spacing!` 相反，它是典型的"业务代码反复使用"的宏：截至本讲撰写时的 HEAD，`crates/` 下正式源码中共有 **63 处** `#[derive(… RegisterComponent …)]`，分布在 **11 个 crate**（其中 `ui` crate 占 49 处；具体分布见 4.3.4 的实践）。

#### 4.2.2 核心流程

这个宏的流程横跨**编译期、链接期、运行时**三个阶段，这是它与间距宏最大的不同：

```text
【编译期】
tooltip.rs:8  #[derive(RegisterComponent)]
    │  ui_macros 拿到整个 struct 定义（DeriveInput）
    ▼
生成三段代码，追加在 struct Tooltip 之后：
    ① const 断言块   —— 编译期验证 Tooltip 实现了 component::Component
    ② 注册函数       —— fn __component_registry_internal_register_Tooltip()
    ③ inventory 提交 —— component::__private::inventory::submit! { … }
    │
【链接期】
    ▼
链接器把【所有 crate】里 ③ 提交的项汇入同一个集合（无需中心清单）
    │
【运行时】
    ▼
component::init()（由 workspace.rs:786 或 component_preview 例子调用）
    │  for f in inventory::iter::<ComponentFn>() { (f.0)(); }
    ▼
register_component::<Tooltip>()（component.rs:47）
    │  组装 ComponentMetadata（description / preview / scope / …）
    ▼
COMPONENT_DATA: LazyLock<RwLock<ComponentRegistry>>（component.rs:63）
    │
    ▼
component_preview 界面 / component::components() 读取展示
```

#### 4.2.3 源码精读

**① 宏的声明与文档**——[src/ui_macros.rs:L40-L43](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L40-L43) 用 `#[proc_macro_derive(RegisterComponent)]` 声明派生宏。它上方的 doc 注释（[L12-L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L12-L39)）自带一个完整的 doctest 示例——注意这个 doctest 本身也是宏的一个"消费者"（通过 dev-dependencies 使用 `ui` 和 `component`）：

```rust
#[proc_macro_derive(RegisterComponent)]
pub fn derive_register_component(input: TokenStream) -> TokenStream {
    derive_register_component::derive_register_component(input)
}
```

**② 生成的全部代码**——[src/derive_register_component.rs:L13-L27](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L13-L27)。整个实现不到 30 行，`quote!` 模板以 `Tooltip` 为例展开后等价于（`#name` 替换为 `Tooltip`）：

```rust
// ① 编译期断言：Tooltip 必须实现 component::Component
const _: () = {
    struct AssertComponent<T: component::Component>(::std::marker::PhantomData<T>);
    let _ = AssertComponent::<Tooltip>(::std::marker::PhantomData);
};

// ② 注册函数：名字由类型名拼接而来
#[allow(non_snake_case)]
fn __component_registry_internal_register_Tooltip() {
    component::register_component::<Tooltip>();
}

// ③ inventory 提交
component::__private::inventory::submit! {
    component::ComponentFn::new(__component_registry_internal_register_Tooltip)
}
```

三个值得注意的细节：

- `const _: ()` 匿名常量块：给断言一个"不占命名空间"的落点，同一文件派生多个组件也不会重名冲突；
- 注册函数名 `__component_registry_internal_register_{TypeName}`：带上类型名 + `#[allow(non_snake_case)]`，保证每个组件的注册函数互不冲突；
- ③ 里写的是 `component::__private::inventory`——`component` crate 专门开了一个 `#[doc(hidden)]` 的私有模块**再导出** `inventory`（见 [../component/src/component.rs:L41-L45](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L41-L45)），这样被宏展开的调用方即使没有直接依赖 `inventory` crate 也能编译通过。

**③ 使用方的样子**——[../ui/src/components/tooltip.rs:L8-L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tooltip.rs#L8-L13) 在结构体上派生，文件末尾 [L272-L291](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tooltip.rs#L272-L291) 手写 `impl Component for Tooltip`（派生宏要求你**自己实现**这个 trait，它只负责注册）：

```rust
#[derive(RegisterComponent)]
pub struct Tooltip { … }

impl Component for Tooltip {
    fn scope() -> ComponentScope { ComponentScope::DataDisplay }
    fn description() -> &'static str { … }
    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement { … }
}
```

**④ 注册表一侧**——[../component/src/component.rs:L25-L29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L25-L29) 的 `init()` 在运行时遍历 inventory 集合逐个调用注册函数；[L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L39) 声明收集点；[L47-L61](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L47-L61) 的 `register_component::<T>()` 组装 `ComponentMetadata` 并写入全局表：

```rust
pub fn init() {
    for f in inventory::iter::<ComponentFn>() {
        (f.0)();
    }
}

inventory::collect!(ComponentFn);

pub static COMPONENT_DATA: LazyLock<RwLock<ComponentRegistry>> =
    LazyLock::new(|| RwLock::new(ComponentRegistry::default()));
```

`init()` 有两个调用点：主程序经 [../workspace/src/workspace.rs:L786](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/workspace/src/workspace.rs#L786)（`workspace::init` 的第一行）触发；独立的预览程序在 [../component_preview/examples/component_preview.rs:L32](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component_preview/examples/component_preview.rs#L32) 触发。

#### 4.2.4 代码实践：两个方向的"破坏实验"

这是一个**思想实验 + 可选本地验证**的任务，帮你理解派生宏与 trait 实现各自承担什么。

1. **实践目标**：分清两种"错误方向"的后果——「派生了但没实现 trait」与「实现了 trait 但没派生」。
2. **操作步骤**：
   - **方向 A（思想实验）**：假设删掉 tooltip.rs 末尾整个 `impl Component for Tooltip` 块，只保留 `#[derive(RegisterComponent)]`，编译会发生什么？对照 4.2.3 ② 中的 `const` 断言块推导报错位置。
   - **方向 B（思想实验）**：假设只删掉 `#[derive(RegisterComponent)]` 这一行，保留 `impl Component for Tooltip`，编译和运行各会发生什么？
   - **可选本地验证**：把 [ui_macros.rs:L21-L37](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L21-L37) 的 doctest 复制到一个临时练习 crate（或直接改本地副本），分别做上面两个实验后 `cargo check`。这是本地练习，**不要提交对源码的改动**。
3. **需要观察的现象**：方向 A 应在编译期报错，错误信息指向 `AssertComponent<Tooltip>` 处的 trait 约束不满足；方向 B 应编译通过、运行也正常，但组件**静默地**不出现在预览界面里。
4. **预期结果**：
   - 方向 A 报编译错误的根源是生成代码①里的 `T: component::Component` 约束——派生宏用类型系统强制"注册的前提是实现"；
   - 方向 B 揭示设计取舍：`Component` trait 是"能不能被预览"的契约，`RegisterComponent` 是"实际登记"的动作，两者分离意味着漏写派生不会破坏编译，只会缺一条预览项。Rust 的派生宏无法强制"实现了 trait 就必须派生"，这是宏能力的边界。
   - 上述运行期表现**待本地验证**（思想实验部分可仅凭源码推出）。

#### 4.2.5 小练习与答案

**练习 1**：为什么注册函数不直接叫 `register`，而要拼成 `__component_registry_internal_register_Tooltip` 这样又长又怪的名字？

**答案**：因为生成代码落在**使用方的模块**里，同一个文件（如 toggle.rs）可能有多个组件派生这个宏，函数名必须包含类型名才不会冲突。`__` 前缀和 `internal` 标记表明它不是给人类调用的公共 API；`#[allow(non_snake_case)]` 则是因为类型名 `Tooltip` 大写开头，不符合 Rust 的蛇形命名规范。

**练习 2**：`component::init()` 为什么必须在"运行时"被显式调用，而不是 `inventory` 提交时组件就自动进入注册表？

**答案**：`inventory::submit!` 的注册项在**程序启动前的链接期**就已收集完毕，但它收集的只是一个"待执行的函数"（`ComponentFn`）。真正把元数据写入 `COMPONENT_DATA` 的动作发生在 `init()` 里逐个调用这些函数时。这样设计可以让宿主选择初始化时机——主程序在 `workspace::init` 里调用，预览工具在自己的 main 里调用，不注册时几乎零开销。

**练习 3**：git_ui、agent_ui 这些业务 crate 里的组件（如 [../git_ui/src/git_ui.rs:L1186](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/git_ui/src/git_ui.rs#L1186)）也在用 `#[derive(RegisterComponent)]`，但 `component::init()` 是在 workspace crate 里调用的——它们是怎么被"数进来"的？

**答案**：靠链接期的分布式收集。`inventory` 的机制是：只要某个 crate 被**链接进最终二进制**，它内部所有 `submit!` 提交的项就会进入同一个全局集合。git_ui 等业务 crate 被主程序依赖并链接，因此它们的组件无需在任何中心清单里登记，就能被 `component::init()` 一网打尽。这正是"分布式注册"相对"中心清单"的优势：新增组件零协调成本。

---

### 4.3 消费者 crate 全景：ui / component / theme 拼成的一张网

#### 4.3.1 概念说明

看懂本讲全景图的关键，是区分两类完全不同的箭头：

- **Cargo 依赖箭头（实线）**：写在 Cargo.toml 里，决定"谁能 use 谁"。`ui` 依赖 `ui_macros`、`theme`、`component`；而 `ui_macros` 只依赖 `syn` + `quote`，**不依赖** `theme`、`component`、`ui` 中的任何一个。
- **宏展开箭头（虚线）**：编译期发生的一次"文本生成"事件。`ui_macros` 生成的代码里出现了 `::theme::theme_settings`、`component::register_component` 这些路径，但**路径的解析发生在调用方（`ui` 等）的依赖图里**，与 `ui_macros` 自己的 Cargo.toml 无关。

一句话总结：宏"提到"这些名字，但从不"解析"它们。这就是为什么 [Cargo.toml:L15-L17](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L15-L17) 里只有两行正式依赖。谁想用这两个宏，谁就要自己备齐 `theme` / `component` 这些依赖（`ui` 正是这么做的，见 [../ui/Cargo.toml:L17-L31](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L17-L31)）。

#### 4.3.2 核心流程

把两个宏的消费者放进同一张图，就是本讲的最终产物——`ui_macros` 与消费者 crate 关系图：

```text
                    ┈┈┈┈┈┈┈┈ 宏展开（编译期，虚线）┈┈┈┈┈┈┈┈
  crates/ui/src/styles/spacing.rs:29          crates/ui/src/components/tooltip.rs:8
        │                                                │
        │ derive_dynamic_spacing![清单]          #[derive(RegisterComponent)]
        ▼                                                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  ui_macros（proc-macro，仅依赖 syn + quote）                  │
  └─────────────────────────────────────────────────────────────┘
        │ 生成 DynamicSpacing 枚举                │ 生成断言+注册函数+submit!
        ▼（替换调用点，落回 ui）                    ▼（追加在类型后，落回 ui）
  DynamicSpacing（spacing.rs 内）            注册代码（tooltip.rs 内）
        │                                                │ 编译/链接期 inventory 收集
        │ pub use spacing::*（styles.rs:16）             ▼
        ▼                                   component::init()（运行时）
  prelude.rs:15 再导出                        │ workspace.rs:786 ── 主程序
        │                                     │ component_preview.rs:32 ── 预览工具
        ▼                                     ▼
  全仓库组件：                          COMPONENT_DATA（component.rs:63）
  DynamicSpacing::BaseXX.rems(cx)             │
        │                                     ▼
        │ 运行时查询 ::theme::theme_settings  component_preview 界面
        ▼                                    （按 ComponentScope 分组展示）
  theme::UiDensity（ui_density.rs:21，三档密度）

  ─────────── Cargo 依赖（实线，写在 Cargo.toml） ───────────
  ui ──▶ ui_macros（ui/Cargo.toml:31）      ui ──▶ theme（:30）
  ui ──▶ component（:17）                    ui_macros ──▶ 仅 syn + quote
```

各 crate 在这张网里的角色：

| crate | 角色 | 关键证据 |
| --- | --- | --- |
| `ui_macros` | 宏的生产者，编译期工具 | [Cargo.toml](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L11-L17)：`proc-macro = true`，仅 syn/quote |
| `ui` | 两个宏的最大消费者 + 产物再导出枢纽 | spacing.rs 调用、prelude.rs 再导出、49 处 `RegisterComponent` |
| `component` | 注册表宿主 + `Component` trait 定义 + `inventory` 再导出 | component.rs 的 `init()` / `COMPONENT_DATA` / `__private` |
| `theme` | 密度设置的来源，被**生成代码**在运行时查询 | ui_density.rs 的 `UiDensity` 三档 |
| `workspace` | 主程序里触发注册的入口 | workspace.rs:786 调用 `component::init()` |
| `component_preview` | 消费注册表的预览工具 | examples/component_preview.rs:32 |
| 其他 9 个业务 crate | `RegisterComponent` 的分布式使用者 | git_ui、agent_ui、notifications 等（见 4.3.4） |

#### 4.3.3 源码精读

**① prelude：三股线索的交汇点**——[../ui/src/prelude.rs:L10-L15](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/prelude.rs#L10-L15) 是整张图最值得驻足的地方，六行代码同时搬运了三方产物：

```rust
pub use component::{
    Component, ComponentScope, example_group, example_group_with_title, single_example,
};
pub use ui_macros::RegisterComponent;

pub use crate::DynamicSpacing;
```

- 第 10-12 行：从 `component` crate 搬来 `Component` trait 和预览布局辅助函数；
- 第 13 行：从 `ui_macros` 搬来 `RegisterComponent` 派生宏（**宏也可以被再导出**，这正是 tooltip.rs 里没有出现 `use ui_macros::…` 的原因）；
- 第 15 行：搬来本 crate 由 `derive_dynamic_spacing!` 生成的 `DynamicSpacing`。

所以组件文件顶部一句 `use crate::prelude::*;` 就同时拿到了"实现 trait 所需的一切 + 注册宏 + 间距枚举"。

**② spacing.rs 顶部的两种导入方式**——[../ui/src/styles/spacing.rs:L1-L4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L1-L4) 显式 `use ui_macros::derive_dynamic_spacing;`，而 tooltip.rs 走 prelude 间接拿到 `RegisterComponent`。两种风格并存说明：函数式宏必须先导入再调用（像普通函数），派生宏经过再导出后使用方甚至可以完全不知道 `ui_macros` 的存在。

**③ ui 的 Cargo.toml：依赖箭头的实据**——[../ui/Cargo.toml:L17-L31](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L17-L31) 同时声明了 `component`、`theme`、`ui_macros` 三个依赖。生成代码里的 `::theme::…` 与 `component::…` 路径之所以能解析，靠的全是这份清单——这就是 4.3.1 所说的"宏提到、调用方解析"的具体落点。

**④ doctest：第三个消费场景**——[src/ui_macros.rs:L21-L37](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L21-L37) 的 doctest 在文档里定义了一个 `MyComponent` 并派生 `RegisterComponent`。它靠 [Cargo.toml:L19-L21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L19-L21) 的 dev-dependencies（`component`、`ui`）编译——`ui_macros` 对 `ui` 的依赖只存在于测试场景，正式构建中依赖方向始终是 `ui ──▶ ui_macros`，不会形成环。

#### 4.3.4 代码实践：统计 RegisterComponent 的调用点分布

1. **实践目标**：用真实的全局搜索回答"`RegisterComponent` 到底被多少 crate 用了"，验证本讲给出的数字。
2. **操作步骤**：
   - 在 Zed 仓库根目录执行（`rg` 或 `grep` 任选）：

     ```bash
     # 列出所有调用点（文件:行号）
     grep -rn "derive(.*RegisterComponent" crates/ --include="*.rs"

     # 按 crate 统计出现次数
     grep -r "derive(.*RegisterComponent" crates/ --include="*.rs" -l \
       | cut -d/ -f2 | sort | uniq -c | sort -rn
     ```
   - 再统计 `derive_dynamic_spacing!` 的调用点：

     ```bash
     grep -rn "derive_dynamic_spacing!" crates/ --include="*.rs"
     ```
3. **需要观察的现象**：第一个命令的输出行数；第二个命令每个 crate 的计数；第三个命令应当只有 `crates/ui/src/styles/spacing.rs` 一个正式源码命中（教程文档与注释中的出现不算调用点）。
4. **预期结果**：以本讲撰写时的 HEAD（ec18126b）为参考答案——`RegisterComponent` 共 **63 处**，分布：

| crate | 调用点数 |
| --- | --- |
| `ui` | 49（其中 toggle.rs 一个文件里 3 处） |
| `agent_ui` | 4 |
| `git_ui` | 2 |
| `settings_ui` / `ai_onboarding` / `notifications` / `onboarding` / `workspace` / `extensions_ui` / `ui_input` / `language_models` | 各 1 |

   共 11 个 crate。代码持续演进，你本地统计到的数字可能略有出入，以本地结果为准；量级（一个调用点 vs 六十多个调用点）不会变。

本实践需要能在本地运行 `grep`（或 `rg`）；若在只读环境下，可对照上表与 4.2.3 的证据链完成同样的推理。

#### 4.3.5 小练习与答案

**练习 1**：`derive_dynamic_spacing!` 只有 1 个调用点、`RegisterComponent` 有 63 个。从这个数量差异能推断出两个宏各自的设计定位有什么不同？

**答案**：`derive_dynamic_spacing!` 是"**生成基础设施类型**"的批处理宏——`DynamicSpacing` 是全仓库共享的一套类型，只需要在一个地方生成一次；`RegisterComponent` 是"**逐个组件使用**"的工具宏——每新增一个可预览组件就要标注一次。调用点数量直接反映了宏的粒度：前者作用于"体系"，后者作用于"个体"。

**练习 2**：如果 `theme` crate 未来重命名 `theme_settings` 函数，哪些 crate 需要改？`ui_macros` 需要改吗？

**答案**：`ui_macros` **不需要改**。它生成的代码里只有 `::theme::theme_settings(cx)` 这个路径字符串，真正的解析发生在调用方 `ui` crate 的编译过程中——`ui` 依赖的 `theme` 改了名，`ui` 的编译会报"找不到 `theme_settings`"的错误，修好 `theme` 自身（或同步更新 `ui` 的依赖版本）即可。代价是：宏与 `theme` 的 API 之间存在**隐式耦合**，`theme` 单方面改名会让远在 `ui` crate 里的宏展开产物编译失败，报错位置可能让人摸不着头脑——这是过程宏生成"带路径代码"的固有取舍。

**练习 3**：一个新组件想同时用上两个宏的产物（间距自适应 + 可预览），它需要直接依赖 `ui_macros` 吗？

**答案**：不需要。组件文件 `use crate::prelude::*;` 之后，`RegisterComponent`（prelude.rs:13 再导出）和 `DynamicSpacing`（prelude.rs:15 再导出）都已可用。对组件作者来说，`ui_macros` 是一个**透明**的存在——这正是 prelude 再导出设计带来的使用体验。

## 5. 综合实践

把本讲三个模块串起来，完成规格中的全景调查任务：

**任务**：在 `crates/` 目录下全局搜索两个宏的所有调用点，整理成一张"调用者 → 被调用者 → 产物"三列表格，并手绘（或用文本画）`ui_macros` 与消费者 crate 的关系图。

**步骤**：

1. 运行 4.3.4 中的两条搜索命令，拿到两个宏的全部调用点清单；
2. 对每个（类）调用点回答三列问题：
   - **调用者**：哪个 crate 的哪个文件；
   - **被调用者**：`derive_dynamic_spacing!` 还是 `RegisterComponent`；
   - **产物**：宏展开后落在调用者源码里的代码是什么；
3. 参考答案（按调用者归类）：

| 调用者 | 被调用者 | 产物 |
| --- | --- | --- |
| `ui/src/styles/spacing.rs:29` | `derive_dynamic_spacing!` | `DynamicSpacing` 枚举（14 个 `BaseXX` 变体）+ `spacing_ratio` / `rems` / `px` 方法，经 `pub use spacing::*` 对外导出 |
| `ui/src/components/*.rs`（47 个文件，49 处） | `RegisterComponent` | 每处三段：`const` trait 断言 + `__component_registry_internal_register_{Name}` 注册函数 + `inventory::submit!` 提交 |
| `agent_ui`(4) / `git_ui`(2) / `settings_ui` / `ai_onboarding` / `notifications` / `onboarding` / `workspace` / `extensions_ui` / `ui_input` / `language_models`（各处合计 14 处） | `RegisterComponent` | 同上（各自落入自己 crate 的源码，链接期汇入同一集合） |
| `ui_macros/src/ui_macros.rs:25`（doctest） | `RegisterComponent` | 文档示例中的 `MyComponent` 注册代码，仅测试时编译 |

4. 依照 4.3.2 的参考图，把你自己统计到的 crate 画进关系图，用**实线**标 Cargo 依赖（`ui ──▶ ui_macros/theme/component`），用**虚线**标宏展开（`spacing.rs ┈─▶ ui_macros`、`tooltip.rs ──▶ ui_macros`），再补上运行时的 `component::init()` 触发线（`workspace.rs:786`、`component_preview.rs:32`）；
5. 自查：图里是否体现了"`ui_macros` 不依赖 `theme`/`component`，但生成代码提到它们的路径"这一关键事实？

**预期结果**：一张三列表格 + 一张双箭头关系图。画完后你应该能不看讲义回答：两个宏各自的输入是什么、输出落在哪里、谁在运行时消费它们。

## 6. 本讲小结

- `derive_dynamic_spacing!` 全仓库只有 **1 个调用点**（`crates/ui/src/styles/spacing.rs:29`），把一份 14 行的间距清单编译期替换为完整的 `DynamicSpacing` 枚举：三元组直接指定三档像素值，单值按 \( \max(n-4,0)\,|\,n\,|\,n+4 \) 推导，变体名取 Default 档像素值补零成 `BaseXX`。
- `RegisterComponent` 有 **63 处调用点、横跨 11 个 crate**（截至本讲 HEAD），每处生成三段代码：`const` 块编译期断言 `Component` trait 已实现、以类型名拼出的注册函数、`component::__private::inventory::submit!` 分布式提交。
- 注册链路横跨三个阶段：编译期生成 → 链接期由 `inventory` 跨 crate 收集 → 运行时由 `component::init()`（`workspace.rs:786` 或预览例子触发）写入全局 `COMPONENT_DATA`。
- 看全景必须区分两类箭头：`ui ──▶ ui_macros/theme/component` 是 Cargo 依赖（实线）；`spacing.rs/tooltip.rs ┈─▶ ui_macros` 是宏展开（虚线）。`ui_macros` 只有 `syn` + `quote` 两个依赖，生成代码中的 `::theme::…`、`component::…` 路径全部由调用方解析。
- `ui` crate 的 prelude.rs 是三股线索的枢纽：再导出 `component::Component`、`ui_macros::RegisterComponent` 和 `DynamicSpacing`，组件文件一行 `use crate::prelude::*` 即可同时用上两个宏的产物。

## 7. 下一步学习建议

- 下一讲（u2-l1）进入单元二「源码精读」：拆开 `src/dynamic_spacing.rs` 的前半部分，看 `syn` 如何把 `24, (1, 2, 4)` 这样的 token 流解析成 `Single(LitInt)` / `Tuple(LitInt, LitInt, LitInt)`。建议先通读 [src/dynamic_spacing.rs:L1-L46](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L46)。
- 想先了解注册宏内部细节的读者，可以直接精读仅 29 行的 [src/derive_register_component.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L1-L29)（单元四 u4-l1 会逐行拆解）。
- 延伸阅读：[../component/src/component.rs:L170-L258](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/component/src/component.rs#L170-L258) 的 `Component` trait 文档写明了各默认方法的用途，是 u4-l3 的预备材料。
- 如果你想立刻看到注册表的运行效果，可以尝试运行组件预览：`cargo run -p component_preview --example component_preview`（需要本地完整的 Zed 构建环境；预览界面也可在 Zed 内通过命令面板打开 `workspace: open component preview`，具体可用性**待本地验证**）。
