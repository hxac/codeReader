# 讲义三：crate 布局与导出路径：从 Cargo.toml 到 ui prelude

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 component crate「只有两个源文件」的极简组织方式，以及 `[lib] path = "src/component.rs"` 这条非默认配置的作用。
2. 解释 `pub use component_layout::*;` 如何制造出「扁平命名空间」，以及私有模块 `mod component_layout;` 为什么让外界只能走扁平路径。
3. 画出符号从定义文件 → component crate → ui prelude → 业务代码的完整流向图，知道 `Component`、`ComponentScope`、`single_example`、`example_group_with_title` 分别「住在哪里、经哪条路到达你的 `use ui::prelude::*`」。
4. 区分 `ui::prelude` 与 `ui::component_prelude` 两条导出通道的符号差异与各自的使用场景。

本讲不深入 Component trait 的七个方法（第 4 讲的任务），只解决一个问题：**这套体系的「文件骨架」和「符号管道」长什么样**。

## 2. 前置知识

阅读本讲前，你需要用通俗语言理解以下几个 Rust 概念（已学过可跳过）：

- **crate 与 package**：Rust 里一个 package（对应一个 `Cargo.toml`）可以包含一个库 crate。库 crate 的「根源文件」默认叫 `src/lib.rs`，但可以在 `Cargo.toml` 里用 `[lib] path = "..."` 改成任意文件名。
- **模块（`mod`）**：根文件里写 `mod foo;` 表示「把 `src/foo.rs` 挂载为名为 `foo` 的子模块」。默认私有，加 `pub` 才对外可见。
- **重导出（`pub use`）**：把别的模块（或别的 crate）里的符号搬到当前路径下，使用者就可以从新路径引用它。`pub use foo::*;`（glob 重导出）会把 `foo` 里所有公开符号一次性全部搬过来。
- **prelude（前奏模块）**：一个约定俗成名字的模块，汇集「写这类代码几乎必然要用的符号」。使用者一句 `use some_crate::prelude::*;` 就能拿到一整套常用类型和函数，而不必写十几行 `use`。Rust 标准库的 `std::prelude` 就是这个模式的鼻祖。
- **命名空间扁平化**：即使符号物理上分散在多个文件，通过 glob 重导出可以让它们在逻辑上「平铺」在 crate 根路径下——`component::single_example` 能用，哪怕它定义在 `component_layout.rs` 里。

上一讲（u1-l2）我们已经知道 `component::init()` 会把链接期登记的注册函数执行一遍、填入全局注册表；本讲来看这些符号是怎么「铺路」到业务代码手边的。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `crates/component/Cargo.toml` | 27 行 | 声明 `[lib] path = "src/component.rs"` 与 6 个依赖 |
| `crates/component/src/component.rs` | 326 行 | crate 根：Component trait、注册表、元数据、scope/status 枚举 |
| `crates/component/src/component_layout.rs` | 206 行 | 预览布局元素（示例卡片、分组）与 4 个辅助函数 |
| `crates/ui/src/ui.rs`（节选） | — | ui crate 根：声明 `prelude`、`component_prelude` 两个公开模块 |
| `crates/ui/src/prelude.rs` | 36 行 | ui 的主 prelude，重导出 component 的 5 个符号 |
| `crates/ui/src/component_prelude.rs` | 7 行 | 面向「写组件的人」的专用 prelude，多导出 3 个符号 |

辅助佐证文件（本讲引用但不精读）：

- `crates/ui/Cargo.toml`：证明 ui 依赖 component 与 ui_macros 的两条边。
- `crates/ui/src/components/button/button.rs`、`crates/ui/src/components/facepile.rs`：ui crate 内部使用 `crate::component_prelude::*` 的实例。
- `crates/settings_ui/src/components/number_field.rs`：跨 crate 使用 `ui::prelude::*` 并注册组件的实例。
- `crates/component_preview/src/component_preview.rs`：注册表层类型直接 `use component::{...}` 的实例。

## 4. 核心概念与源码讲解

### 4.1 Cargo.toml 的 [lib] 配置与两文件极简布局

#### 4.1.1 概念说明

Rust 的默认约定是库 crate 根文件叫 `src/lib.rs`。但 Zed 仓库的编码规范（见仓库根 `CLAUDE.md`）明确要求：**创建新 crate 时优先在 `Cargo.toml` 里用 `[lib] path` 指向一个描述性命名的文件**（例如 `gpui.rs`），而不是默认的 `lib.rs`，以保持命名的一致性和描述性。component crate 就是这个约定的产物：它的根文件叫 `component.rs`，与 crate 名一致。

这个 crate 的第二个特点是**极简**：整个 src 目录只有两个文件，合计约 530 行。它是一个典型的「元层」crate——不实现任何业务 UI，只提供契约（trait）、登记簿（registry）和展示壳（layout），所以不需要庞大的文件树。

#### 4.1.2 核心流程

Cargo 编译这个 crate 时的定位过程：

1. Cargo 读 `crates/component/Cargo.toml`，发现 `[lib] path = "src/component.rs"`。
2. 编译器把 `src/component.rs` 当作 crate 根，而不是去找 `src/lib.rs`（本 crate 根本没有这个文件）。
3. 根文件里的 `mod component_layout;` 让编译器接着加载 `src/component_layout.rs` 作为私有子模块。
4. 依赖方面，6 个 crate 各司其职：

| 依赖 | 在本 crate 里干什么 |
| --- | --- |
| `gpui` | 提供 `AnyElement`、`App`、`Window`、`SharedString` 等 UI 基础类型 |
| `inventory` | 链接期分布式注册的机制核心（上一讲讲过） |
| `parking_lot` | 提供 `RwLock`，支撑全局注册表 `COMPONENT_DATA` |
| `strum` | 为 scope/status 枚举派生 `Display` / `EnumString` |
| `collections` | Zed 自己的集合封装，这里用 `HashMap` |
| `theme` | 布局元素渲染时取主题颜色令牌（`ActiveTheme`） |

#### 4.1.3 源码精读

首先是 `[lib]` 配置与依赖声明：

[crates/component/Cargo.toml:11-20](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L11-L20)

这段配置做了两件事：`[lib] path = "src/component.rs"` 把 crate 根从默认的 `lib.rs` 改名为 `component.rs`；`[dependencies]` 列出上面表格里的 6 个依赖。

再看 dev-dependencies（只在测试与示例编译时生效）：

[crates/component/Cargo.toml:22-23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L22-L23)

`documented` 只出现在 dev-dependencies 里——它提供 `Documented` 派生宏，能把 doc 注释变成字符串，配合 Component trait 的 `description()` 使用（详见 trait 文档中的示例，本讲不展开）。

根文件的模块挂载与扁平化入口：

[crates/component/src/component.rs:10-14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L10-L14)

注意第 10 行是 `mod component_layout;` 而**不是** `pub mod`——这个子模块是私有的，外界无法通过 `component::component_layout::...` 路径访问它；第 14 行的 `pub use component_layout::*;` 把它的全部公开符号提升到 crate 根。这就是「扁平命名空间」的实现手法：模块结构只服务于内部分工，对外只暴露一个平面。

#### 4.1.4 代码实践

**实践目标**：亲眼确认这个 crate 的极简布局，并理解 `[lib] path` 的实际效果。

**操作步骤**：

1. 在 Zed 仓库根目录执行 `git ls-files crates/component/src`。
2. 再执行 `ls crates/component/src`（应看到且仅看到 `component.rs` 与 `component_layout.rs` 两个文件）。
3. 用 `wc -l crates/component/src/*.rs` 统计行数。
4. 打开仓库里任意一个「遵循默认约定」的 crate 对比，例如执行 `ls crates/lsp/src | head -3`——如果你看到 `lib.rs` 存在于其他 crate 而 component 没有，就说明两者采用了不同的根文件约定。

**需要观察的现象**：`git ls-files` 只列出两个 `.rs` 文件；`wc -l` 输出约 326 + 206 行。

**预期结果**：整个 crate 的源码体量比很多单个组件文件还小。它是「小 crate 承担关键架构角色」的典型案例。

（以上命令都是只读的目录/行数检查，结论已在本讲义编写时通过工具核实；你也可以自行复验。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Cargo.toml` 里的 `[lib] path = "src/component.rs"` 这两行删掉，会发生什么？

**答案**：Cargo 会去找默认的 `src/lib.rs`，而该文件不存在，编译直接失败并报「could not find `lib.rs`（或匹配 `[lib] path` 的文件）」一类的错误。这就是 `[lib] path` 配置的本质：它不是可选的装饰，而是 crate 根定位的唯一依据。

**练习 2**：依赖清单里哪一个是「链接期分布式注册」能够成立的关键？它出现在 `init()` 的哪一行？

**答案**：`inventory`。在 [crates/component/src/component.rs:25-29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L29) 中，`init()` 的函数体 `for f in inventory::iter::<ComponentFn>()` 正是遍历 inventory 在链接期收集起来的注册函数切片（上一讲已详述）。

**练习 3**：为什么 `theme` 是普通依赖，而 `documented` 只是 dev-dependency？

**答案**：`theme` 被 `component_layout.rs` 的渲染代码在正式构建中使用（取 `cx.theme().colors()` 颜色令牌），所以必须是正式依赖；`documented` 只服务于「把 doc 注释当 description」这种开发期便利（其示例出现在 Component trait 的 doc 注释里，且该 crate 自身的测试才需要），运行时代码不引用它，因此放 dev-dependencies 以收敛依赖图。

### 4.2 component_layout 模块：私有子模块与扁平命名空间

#### 4.2.1 概念说明

`component_layout.rs` 承担本 crate 三大职责中的「预览布局」：它定义了两个 GPUI 布局元素（单个示例卡片 `ComponentExample`、分组容器 `ComponentExampleGroup`）和四个便捷构造函数。它与 `component.rs` 的分工是——

- `component.rs`：**机制**（trait 契约、注册表、元数据、scope/status 枚举）；
- `component_layout.rs`：**展示**（预览页面里长什么样的壳子）。

由于 4.1 讲过的私有 `mod` + glob 重导出组合，这两个文件的符号在对外接口上完全平级：使用者写 `component::single_example(...)` 和 `component::Component`（trait）的「路径深度」一样，看不出谁住在哪个文件。这是刻意的接口设计：**模块结构是内部实现细节，扁平接口才是公开契约**，将来把某个符号在两个文件间搬家不会破坏任何调用方代码。

#### 4.2.2 核心流程

符号从定义到暴露的路径：

```text
src/component_layout.rs                       （定义：布局元素 + 辅助函数）
        │  mod component_layout;              （挂载为私有子模块）
        ▼
src/component.rs 的模块树                     （component::component_layout，私有）
        │  pub use component_layout::*;       （glob 重导出，扁平化）
        ▼
crate 根命名空间                              （component::ComponentExample 等，公开）
```

#### 4.2.3 源码精读

两个布局元素的数据结构：

[crates/component/src/component_layout.rs:7-14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L7-L14)

`ComponentExample` 是「单个示例卡片」：变体名、可选描述、被包裹的任意元素、可选宽度。它派生了 `IntoElement`（GPUI 的派生宏，让结构体可以直接作为元素树的 child），渲染逻辑实现在 `RenderOnce::render` 里。

[crates/component/src/component_layout.rs:92-100](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L92-L100)

`ComponentExampleGroup` 是「分组容器」：可选大写标题加一组示例卡片，外加 `width` / `grow` / `vertical` 三个构建器开关。两个结构体的 `RenderOnce::render` 实现分别约 50 行，属于第 8 讲（u3-l1）的精读范围，本讲只需认识它们的「户口」。

四个辅助函数——注意它们**全部定义在 component_layout.rs 里**，但经 glob 重导出后都以 `component::` 开头可用：

[crates/component/src/component_layout.rs:185-205](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L185-L205)

这四个函数是纯语法糖：`single_example` 与 `example_group_with_title` 分别转调 `ComponentExample::new` 与 `ComponentExampleGroup::with_title`，`empty_example` 构造一个「故意留白」的占位卡片。第 9 讲（u3-l2）会讲如何组合它们写出高质量 preview。

回到重导出本身，再看一眼「扁平化」的关键行：

[crates/component/src/component.rs:14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L14)

由于第 10 行的 `mod component_layout;` 没有 `pub`，`component::component_layout` 这条路径对外不存在——**外界唯一的访问方式就是扁平路径**。

#### 4.2.4 代码实践

**实践目标**：用编译器亲自验证「私有模块 + glob 重导出」的访问规则。

**操作步骤**：

1. 在任意一个已依赖 `component` 的 crate（推荐临时改 `crates/component_preview/examples/component_preview.rs`，本地实验后还原，**不要提交**）里加两行函数：

```rust
// 示例代码：用于验证模块可见性，实验后请删除
fn probe_flat_path() {
    let _ = component::single_example("演示", gpui::div().into_any_element());
}

fn probe_nested_path() {
    // 下面这行预期编译失败：component_layout 是私有模块
    let _ = component::component_layout::single_example("演示", gpui::div().into_any_element());
}
```

2. 运行 `cargo check -p component_preview --example component_preview`。

**需要观察的现象**：`probe_flat_path` 通过检查；`probe_nested_path` 报错，错误信息形如 `module 'component_layout' is private`。

**预期结果**：证实在这个 crate 里，嵌套路径不是「另一种写法」而是「不存在的路径」。（此实验结论基于 Rust 可见性规则推得，具体错误文案随编译器版本略有差异，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：把 `single_example` 从 `component_layout.rs` 搬到 `component.rs`（并保持签名不变），会破坏哪些调用方？

**答案**：一个都不会破坏。因为对外契约是扁平路径 `component::single_example`，它由 `pub use component_layout::*;` 与定义位置共同决定——搬走后只要函数仍是 crate 内的公开项（或相应调整 glob），扁平路径不变。这正是扁平命名空间的设计收益：内部文件归属可以自由重构。

**练习 2**：`ComponentExample` 派生的 `IntoElement` 和它实现的 `RenderOnce` 是什么关系？

**答案**：`RenderOnce` 是 GPUI 里「一次性渲染元素」的 trait——`render(self, ...)` 拿走所有权而非 `&mut self`；`#[derive(IntoElement)]` 则让这个结构体可以直接出现在 `.child(...)` 的参数位置。两者配合构成「用结构体当轻量组件」的标准姿势（Zed 的 CLAUDE.md 对此也有说明）。

**练习 3**：为什么 `empty_example` 只需要 `variant_name` 一个参数，而 `single_example` 需要两个？

**答案**：`empty_example` 表达的场景是「这个分支合法地渲染为空」，它的元素内容是固定的占位说明文字（见 [crates/component/src/component_layout.rs:192-194](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L192-L194) 中写死的那句 "This space is intentionally left blank..."），不需要调用者传入元素；`single_example` 展示的是真实变体，必须传入要渲染的元素。

### 4.3 ui prelude 重导出：符号如何到达业务代码

#### 4.3.1 概念说明

component crate 自己不直接被业务代码大量使用——Zed 的约定是**业务代码统一从 `ui::prelude` 拿东西**。ui crate 是整个编辑器 UI 的门面（facade），它在 prelude 里重导出了 component 的核心符号，于是业务开发者一句 `use ui::prelude::*;` 就同时获得了「实现 Component + 派生 RegisterComponent + 写 preview」所需的全部入口。

有趣的是 ui 提供了**两条**通道：

- `ui::prelude`：面向「所有写 UI 的人」，混入了 gpui 基础类型、按钮图标标签等组件、以及 component 的核心符号；
- `ui::component_prelude`：面向「写可注册组件的人」，是 component 相关符号的超集，额外多出 `ComponentId`、`ComponentStatus` 和 `documented::Documented`。

两条通道的符号差异（均已在源码中逐一核实）：

| 符号 | 定义处 | `ui::prelude` | `ui::component_prelude` |
| --- | --- | --- | --- |
| `Component`（trait） | component.rs | ✅ | ✅ |
| `ComponentScope` | component.rs | ✅ | ✅ |
| `example_group` | component_layout.rs | ✅ | ✅ |
| `example_group_with_title` | component_layout.rs | ✅ | ✅ |
| `single_example` | component_layout.rs | ✅ | ✅ |
| `ComponentId` | component.rs | ❌ | ✅ |
| `ComponentStatus` | component.rs | ❌ | ✅ |
| `empty_example` | component_layout.rs | ❌ | ❌（需写 `component::empty_example`） |
| `RegisterComponent`（派生宏） | ui_macros | ✅ | ✅ |
| `Documented`（派生宏） | documented | ❌ | ✅ |

两个 prelude 都**不**重导出注册表层类型（`ComponentRegistry`、`ComponentMetadata`、`components()` 函数）——消费注册表的代码（如 component_preview 应用）直接 `use component::{...}`。

#### 4.3.2 核心流程

本讲规格要求的「符号流向图」（文字版，四个目标符号各有一条完整链路）：

```text
定义文件                          component crate 根           ui crate                       业务代码
──────────────────────────────  ─────────────────────────  ─────────────────────────────  ──────────────────
component.rs      Component ─┐
component.rs      ComponentScope ─┤
component_layout.rs single_example ─┼─ pub use（见 4.3.3）→  component::X
component_layout.rs example_group_with_title ─┘                    │
                                                                    │ pub use component::{...}
                                                    ├─→ ui/src/prelude.rs ── pub use prelude::* ─→ ui 根命名空间
                                                    │        │                                  │
                                                    │        └─ use ui::prelude::* ──────────┼─→ settings_ui 等业务 crate
                                                    │                                          │
                                                    └─→ ui/src/component_prelude.rs           │
                                                             │  use crate::component_prelude::* ─┘（ui 内部组件文件）
                                                             └─（跨 crate 亦可 use ui::component_prelude::*）
```

拆成四条具体链路：

1. `Component`：`component/src/component.rs:170` 定义 → `ui/src/prelude.rs:10` 重导出 → `use ui::prelude::*`。
2. `ComponentScope`：`component/src/component.rs:299` 定义 → `ui/src/prelude.rs:10` 重导出 → 同上。
3. `single_example`：`component/src/component_layout.rs:185` 定义 → `component.rs:14` glob 扁平化 → `ui/src/prelude.rs:10` 重导出 → 同上。
4. `example_group_with_title`：`component/src/component_layout.rs:200` 定义 → 同样经扁平化与 `ui/src/prelude.rs:10` → 同上。

#### 4.3.3 源码精读

**第一跳：ui crate 声明依赖边。**

[crates/ui/Cargo.toml:17-31](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L17-L31)

第 17 行 `component.workspace = true` 与第 31 行 `ui_macros.workspace = true` 是两条关键依赖声明——prelude 里所有重导出都建立在这两条边之上（`documented` 在第 18 行，供 component_prelude 使用）。

**第二跳：ui crate 根声明两个公开 prelude 模块。**

[crates/ui/src/ui.rs:10-18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/ui.rs#L10-L18)

注意 `pub mod component_prelude;` 与 `pub mod prelude;` 都是公开模块，外部可以写 `ui::prelude::*` 或 `ui::component_prelude::*`；同时第 18 行 `pub use prelude::*;` 还把 prelude 的内容（含 component 的重导出）再次提升到 ui crate 根命名空间。

**第三跳：主 prelude 的 component 重导出行。**

[crates/ui/src/prelude.rs:10-13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/prelude.rs#L10-L13)

这两行就是本讲的主角：从 component crate 挑出 5 个符号（`Component`、`ComponentScope`、`example_group`、`example_group_with_title`、`single_example`），再从 ui_macros 拿来 `RegisterComponent` 派生宏。对照 4.2 的定义位置可知：`Component` 走的是「同文件直达」，而三个 `example_*` 系列函数走的是「先在 component.rs:14 被扁平化、再在这里被重导出」的两跳路径。

**第四跳：专用 component_prelude。**

[crates/ui/src/component_prelude.rs:1-6](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/component_prelude.rs#L1-L6)

与主 prelude 相比多了 `ComponentId`、`ComponentStatus`（第 2-3 行）和 `documented::Documented`（第 5 行）——这三个符号只在「注册/治理」场景需要（构造 id、标记状态、把 doc 注释变 description），普通 UI 编码用不上，所以不放进主 prelude 以免污染命名空间。

**消费端实例一：ui crate 内部组件文件。**

[crates/ui/src/components/button/button.rs:1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/button.rs#L1)

[crates/ui/src/components/facepile.rs:1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/facepile.rs#L1)

ui 内部写组件的文件用 `use crate::component_prelude::*;`——因为它们就在 ui crate 里，走 `crate::` 路径最短，而且这些文件正是「写可注册组件的人」，需要那个超集版本。

**消费端实例二：跨 crate 注册组件。**

[crates/settings_ui/src/components/number_field.rs:18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/settings_ui/src/components/number_field.rs#L18)

这一句 `use ui::prelude::*;` 之后，同文件第 257 行就能直接写 `#[derive(IntoElement, RegisterComponent)]`、第 840 行就能写 `impl Component for NumberField<usize>`——这就是「符号管道」的终点：业务代码感觉不到 component crate 的存在，但用的全是它的东西。

**消费端实例三：注册表的消费走另一条路。**

[crates/component_preview/src/component_preview.rs:5](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L5)

component_preview 应用需要的是注册表层 API（`components()` 拿快照、`ComponentMetadata` 渲染条目、`ComponentStatus` 显示徽章、`ComponentId` 做查找），这些不在任何 ui prelude 里，因此直接依赖并导入 component crate 本体。**「写组件的人走 ui prelude，读注册表的人走 component 直连」**——这条分界线是本讲最重要的架构结论之一。

#### 4.3.4 代码实践

这是本讲规格指定的主实践任务，分两步。

**实践目标**：亲手完成符号溯源，并验证 `use ui::prelude::*` 足以支撑一个完整的 Component trait 实现。

**第一步：画符号流向图。**

1. 打开 `crates/ui/src/prelude.rs`，找到第 10-12 行的 `pub use component::{...};`，抄下这 5 个符号名。
2. 打开 `crates/ui/src/component_prelude.rs` 第 1-4 行，找出比上面多出的符号（应为 `ComponentId`、`ComponentStatus`）。
3. 对 `Component`、`ComponentScope`、`single_example`、`example_group_with_title` 四个符号，分别确认定义文件（提示：前两个在 `component.rs`，后两个在 `component_layout.rs`，可用 `grep -n "pub struct ComponentScope\|pub fn single_example\|pub fn example_group_with_title" crates/component/src/*.rs` 快速定位）。
4. 画出（纸笔即可）4.3.2 节那样的流向图，标注每一跳的文件与行号。

**第二步：验证 prelude 足以写出 Component 实现。**

以下是一段**示例代码**（不是仓库已有代码），可在 `crates/component_preview/examples/component_preview.rs` 的 `main` 函数之前临时粘贴做编译验证（实验后删除，**不要提交**）：

```rust
// 示例代码：验证 ui::prelude 提供实现 Component 所需的全部符号
use ui::prelude::*;

pub struct ProbeLabel;

impl Component for ProbeLabel {
    fn scope() -> ComponentScope {
        ComponentScope::Typography
    }
    fn description() -> &'static str {
        "只依赖 ui::prelude 的最小可编译组件。"
    }
    fn preview(_window: &mut gpui::Window, _cx: &mut gpui::App) -> gpui::AnyElement {
        example_group_with_title(
            "最小示例",
            vec![single_example("变体一", gpui::div().into_any_element())],
        )
        .into_any_element()
    }
}
```

注意这个片段同时用到 `Component`、`ComponentScope`、`single_example`、`example_group_with_title` 四个符号——全部来自一句 `use ui::prelude::*;`，无需再写任何 `use component::...`。若给它加上 `#[derive(RegisterComponent)]`（同样在 prelude 里），它就会在 `component::init()` 执行后被登记进注册表。

**需要观察的现象**：`cargo check -p component_preview --example component_preview` 通过，无「unresolved import」或「not found in scope」错误。

**预期结果**：证实主 prelude 对「写一个最小组件」是自足的。（本片段按当前源码的符号集合推得，待本地验证；若编译器提示 `ProbeLabel` 未被使用之类警告，属正常，加 `#[allow(dead_code)]` 即可。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ui/src/prelude.rs` 不干脆 `pub use component::*;` 一次性全量重导出？

**答案**：全量 glob 会把 `ComponentFn`、`COMPONENT_DATA`、`__private` 等注册机制内部件也倾倒进每个业务文件的命名空间，既污染自动补全，也模糊了「哪些是稳定 API、哪些是机制内脏」的边界；显式列举 5 个符号是一份经过筛选的公开契约。这与 4.2 讲的「内部 glob、对外精选」形成对照：component crate 对 ui 用 glob（同团队、下一层门面负责筛选），ui 对业务代码用显式清单（跨团队的稳定界面）。

**练习 2**：`ui/src/ui.rs:18` 的 `pub use prelude::*;` 意味着什么额外用法是可行的？

**答案**：prelude 的内容（包括从 component 重导来的那 5 个符号）被再次提升到 ui crate 根，因此理论上 `ui::Component`、`ui::single_example` 这类根路径写法也应可用。不过仓库惯例是统一用 `ui::prelude::*` 导入，根路径写法只在个别场景（如 `ui::prelude` 与其他 crate 符号冲突时精确引用）才有意义。

**练习 3**：一个新 crate 想注册自己的组件，`Cargo.toml` 里至少要加哪些依赖？

**答案**：至少 `ui`（它传递提供 component 与 ui_macros，prelude 里有 trait、辅助函数和 `RegisterComponent` 宏）。参照 settings_ui 的 `number_field.rs`：只 `use ui::prelude::*` 就完成了 `#[derive(RegisterComponent)]` + `impl Component` 的全部导入；除非要直接消费注册表（像 component_preview 那样调 `components()`），否则不需要直接依赖 component crate。

## 5. 综合实践

**任务：给一个真实组件做「符号溯源档案」。**

从 ui crate 里任选一个已注册组件（推荐 `crates/ui/src/components/facepile.rs`，它只有一行 `use crate::component_prelude::*;`，线索干净），完成以下四步：

1. **列出它的导入清单**：读文件头部的 `use` 语句，标记哪些符号来自 `crate::component_prelude`。
2. **逐符号溯源**：对清单里每个 component 相关符号（至少包括 `Component`、`RegisterComponent` 派生宏、以及 preview 里用到的辅助函数），沿着「本文件 → component_prelude.rs → component.rs / component_layout.rs」的链路回溯到定义行，记录每一跳的文件与行号。
3. **画流向图**：把这些链路画成一张图（形式不限），并标注「哪一跳是 glob、哪一跳是显式列举」。
4. **回答一个判断题**：如果明天 Zed 把 `single_example` 从 `component_layout.rs` 移到 `component.rs`，你的这张图里哪些箭头会失效？哪些行号会变化？`facepile.rs` 本身需要改动吗？

**验收标准**：第 4 步的正确答案是——`component_layout.rs → component.rs` 这条「扁平化」箭头消失，各符号定义行号更新，但 `component_prelude.rs` 的重导出行、`facepile.rs` 的 `use` 语句全部不需要改动。能独立推出这个结论，说明你已真正理解「扁平命名空间 + 显式重导出」两层设计的解耦价值。

（本任务为纯源码阅读型实践，不修改任何源码；全部结论可通过 `grep -n` 复核。）

## 6. 本讲小结

- component crate 极简到只有两个源文件（约 530 行）：`component.rs` 管机制（trait/注册表/枚举），`component_layout.rs` 管展示（示例卡片/分组/辅助函数）；`Cargo.toml` 用 `[lib] path = "src/component.rs"` 遵循仓库「根文件与 crate 同名」的非默认约定。
- 私有 `mod component_layout;` 加 `pub use component_layout::*;` 制造出扁平命名空间：模块分工是内部细节，对外只有 `component::X` 一层平面路径，符号在文件间搬家不破坏调用方。
- ui crate 提供两条导出通道：主 `ui::prelude`（5 个核心符号 + `RegisterComponent`，面向所有写 UI 的人）和 `ui::component_prelude`（再加 `ComponentId`、`ComponentStatus`、`Documented`，面向写可注册组件的人）；`empty_example` 与注册表层 API 不在任何 prelude 里。
- 业务代码（如 settings_ui 的 NumberField）只需 `use ui::prelude::*` 即可完成「派生 + 实现 + preview」三件事；而消费注册表的 component_preview 直接 `use component::{...}`——「写组件走 ui prelude，读注册表走 component 直连」是清晰的架构分界线。

## 7. 下一步学习建议

本讲搞定了「骨架与管道」，下一讲（u2-l1「Component trait 逐方法精读」）将沿着这条管道进入 `component.rs` 的核心：逐个拆解 `Component` trait 的七个方法（`id`、`scope`、`status`、`name`、`sort_name`、`description`、`preview`）的语义、默认实现与覆写时机。建议预习时先自己通读一遍 [crates/component/src/component.rs:160-258](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L258) 的 trait 定义与 doc 注释，带着「每个默认实现返回什么、什么时候该覆写」的问题进入下一讲。
