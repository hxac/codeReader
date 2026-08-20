# gpui crate 目录结构导览

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `src/` 下五大子目录 `app/`、`elements/`、`platform/`、`text_system/`、`window/` 各自承担的职责，以及 `keymap/`、`profiler/` 两个小目录的定位。
2. 解释 [src/gpui.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs) 如何用约 40 条 `mod` 声明和一大批 `pub use ... *` 重导出，把 40 多个顶层文件、7 个子目录组织成一个「扁平命名空间」的 crate。
3. 掌握「反查定义文件」的基本功：看到一个公开类型名（如 `UniformList`、`KeyBinding`），能通过 `gpui.rs` 的 `pub use` 或 `grep` 快速定位它的真实定义处。
4. 区分三类源码文件：任何配置下都编译的核心机制文件、按操作系统/feature 条件编译的平台与可选功能文件、只在测试和文档构建时出现的文件。

本讲是纯「地图课」：不逐行读实现，而是建立一张后续 30 多讲都能对照使用的导航图。承接 u1-l1 的结论——gpui 是跨平台核心 crate、`gpui.rs` 是 crate 注册表——本讲把这句话展开成完整的模块树。

## 2. 前置知识

本讲默认你已读完 u1-l1（GPUI 定位与三层编程界面）和 u1-l2（hello_world 启动四步曲）。此外需要以下通俗概念：

- **Rust 模块（module）**：Rust 用 `mod` 把一个 crate 拆成多个命名空间。写 `mod foo;` 表示「这个 crate 有个子模块 `foo`，它的内容在 `src/foo.rs` 或 `src/foo/mod.rs` 里」。注意 gpui 自己的约定是「永远用 `foo.rs` + `src/foo/` 目录」这种形式（仓库 CLAUDE.md 明确禁止新建 `mod.rs`），所以你会看到 `src/app.rs` 与 `src/app/` 目录同时存在——`app.rs` 是模块 `app` 的入口，`app/` 里是它的二级子模块。
- **`pub use` 重导出（re-export）**：`pub use foo::*;` 的意思是「把模块 `foo` 里的所有公开名字，再以当前模块的名义公开一次」。这是 Rust 生态常见的「门面（facade）」手法：内部按文件分工，对外呈现一个扁平的命名空间。GPUI 把这个手法用到了极致。
- **glob 导入与扁平 API**：`use gpui::*;` 之后，`div()`、`px()`、`Entity`、`Context` 这些名字全部直接可用，不需要写 `gpui::elements::div::div()`。本讲要解释这个「扁平」是怎么拼出来的。
- **条件编译（`#[cfg(...)]`）**：Rust 可以按条件决定某段代码是否参与编译，条件包括目标操作系统（`target_os = "linux"`）、是否在跑测试（`test`）、是否开启了某个 feature（`feature = "profiler"`）、甚至是否在生成文档（`doc`）。UI 框架天然需要它：macOS/Linux/Windows 的窗口代码完全不同，测试时又希望不开真实窗口。
- **trait 对象（`Rc<dyn Platform>`）**：用引用计数的智能指针装一个「实现了 Platform trait 的未知类型」。GPUI 核心不关心背后是哪个操作系统后端，只要求它实现 `Platform` 接口——这是本讲依赖草图中 `app.rs → platform.rs` 那条边的实现方式。

回顾 u1-l2 的结论：`application()` 来自门面 crate `gpui_platform`，而 `cx.open_window`、`cx.new` 都定义在 gpui 内部。本讲要回答的正是：这些能力分别藏在 `src/` 的哪个角落。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs) | crate 根模块，「注册表」（352 行） | `mod` 声明块、`pub use` 重导出块、`cfg` 门控、少数核心 trait |
| [src/element.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/element.rs) | `Element` trait 与元素机制（792 行） | 模块级文档、`use crate` 里暴露的依赖方向 |
| [src/elements/mod.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/mod.rs) | 内置元素目录的汇总文件（27 行） | 「私有 `mod` + `pub use *`」的标准目录组织模式 |
| [docs/contexts.md](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/docs/contexts.md) | 官方上下文说明文档 | 各上下文与核心类型的职责一句话定义 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs) | `Application`、`App`、实体与全局状态（3137 行） | 顶部 `mod` / `pub use` 块、`platform` 字段 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs) | `Window`、绘制管线、焦点、动作派发（7521 行） | 顶部子模块声明、`layout_engine` 字段 |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs) | 平台抽象 trait（2993 行） | 条件编译的子模块声明 |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs) | `div` 万能元素（5197 行） | 模块文档与 `use crate` 依赖清单 |
| [src/text_system.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/text_system.rs) | 文本系统入口（1206 行 + 子目录） | 与 `elements/mod.rs` 相同的目录组织模式 |
| [src/prelude.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/prelude.rs) | 预导入集合（9 行） | 扁平 API 的「用户入口」 |
| [src/keymap.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/keymap.rs) | 键位映射（927 行） | 同样的目录组织模式 |
| [src/profiler.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler.rs) | 性能剖析本体 + `profiler/` 目录挂载点（1551 行） | 二级模块如何挂 feature 门（4.4 节） |

另外会顺带提到 [src/_ownership_and_data_flow.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/_ownership_and_data_flow.rs) 与 [src/_accessibility.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/_accessibility.rs)——两个以下划线开头的「文档专用」文件，是条件编译一节的有趣案例。

## 4. 核心概念与源码讲解

### 4.1 模块树：gpui.rs 是整个 crate 的注册表

#### 4.1.1 概念说明

一个 Rust crate 必须有一个根模块文件。gpui 的 Cargo.toml 按 Zed 仓库约定用 `[lib] path = "src/gpui.rs"` 指定了 `gpui.rs`（而不是默认的 `lib.rs`）。这个文件只有 352 行，却决定了整个 crate 约 6.7 万行代码的组织方式：

- 每一行 `mod xxx;` 都像一条「目录索引」，把 `src/xxx.rs` 或 `src/xxx/` 目录挂进模块树。
- gpui 有 **40 多个顶层文件模块** 和 **7 个目录模块**（`app`、`elements`、`keymap`、`platform`、`profiler`、`text_system`、`window`）。
- 除注册模块外，`gpui.rs` 还亲自定义了少量最核心的 trait（`AppContext`、`VisualContext`、`EventEmitter` 等），因为它们需要站在 crate 最高处被所有模块引用。

为什么叫「注册表」？因为**任何 `src/` 下的文件如果不在这里声明 `mod`，就完全不会参与编译**——这是 Rust 初学者最常见的困惑来源：新建了文件却忘了声明，编译器根本看不见它。反过来，读 `gpui.rs` 的 `mod` 块就等于读到了 crate 的完整顶层目录。

#### 4.1.2 核心流程

`gpui.rs` 的文件结构可以概括为五段：

```text
第 1-9 行     文档头：include_str! 把 README.md 变成 crate 级文档；
             extern crate self as gpui —— 让宏里能写出 ::gpui:: 路径
第 10-88 行   mod 声明区：约 40 个模块挂载点（部分带 #[cfg]）
第 90-168 行 pub use 重导出区：把各模块名字拍平到 crate 根（4.2 节展开）
第 170-339 行 少量核心 trait 与类型的本体定义（AppContext、VisualContext 等）
第 341-352 行 GpuSpecs 等杂项
```

目录模块的展开规则（以 `elements` 为例）：

```text
gpui.rs 写:  mod elements;                ← 私有挂载，外部不能写 gpui::elements::xxx
              ↓ 编译器寻找
src/elements/mod.rs 存在
              ↓ 里面再写
mod div;      ← 挂载 src/elements/div.rs
pub use div::*;  ← 把 div 的公开名字提升到 elements 模块
```

#### 4.1.3 源码精读

先看文件头。第一行把 README 变成 crate 文档，第七行是一个少见但关键的技巧：

[src/gpui.rs:1-9](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L1-L9) —— `#![doc = include_str!("../README.md")]` 让 `cargo doc` 生成的 crate 首页就是 README；`extern crate self as gpui;` 把当前 crate 显式命名为路径里的 `gpui`，这样 gpui_macros 生成的代码可以放心写 `::gpui::XXX` 而不受用户重命名影响。

接着是模块声明区的开头部分：

[src/gpui.rs:10-27](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L10-L27) —— 注意几个细节：`#[macro_use] mod action;` 让 `actions!` 等宏在整个 crate 内可用；`mod app;` / `mod element;` / `mod elements;` 都是**私有**声明（没有 `pub`），外部世界之所以能用 `gpui::div()`，靠的是后面的 `pub use`；`#[cfg(feature = "profiler")] mod debug_overlay;` 只有开 profiler feature 才编译。

声明区延续到 window：

[src/gpui.rs:28-64](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L28-L64) —— 这一段能遇到 7 个目录模块中的另外 5 个（`keymap`、`platform`、`profiler`、`text_system`、`window`；`app` 与 `elements` 已出现在上一段）。七个目录模块里 `profiler` 是唯一声明为 `pub mod` 的（[src/gpui.rs:39-40](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L39-L40) 带文档注释），其余六个都是私有 `mod` 加 `pub use` 拍平。`#[cfg(any(test, feature = "test-support"))] pub mod test;` 是测试基础设施的总开关，4.4 节展开。

目录模块内部长什么样？`elements` 是最典型的样本：

[src/elements/mod.rs:1-27](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/mod.rs#L1-L27) —— 全文 27 行：上半部分 `mod anchored; mod animation; ...` 把 13 个内置元素文件挂为私有子模块，下半部分逐个 `pub use xxx::*;` 把它们的名字提升到 `elements` 模块层。没有任何逻辑代码，纯粹是「目录汇总文件」。

同样的模式在四个地方重复出现，你可以自行对照：

- [src/text_system.rs:1-11](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/text_system.rs#L1-L11) —— 挂载 `font_fallbacks`、`font_features`、`line`、`line_layout`、`line_wrapper` 五个文本子模块后，从第 14 行起才是 `TextSystem` 本体代码。
- [src/keymap.rs:1-4](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/keymap.rs#L1-L4) —— `mod binding; mod context;` 对应 `src/keymap/` 目录下两个文件。
- [src/app.rs:62-74](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L62-L74) —— `app.rs` 主体 3100 多行之外，把 `async_context`、`context`、`entity_map` 等子模块也挂进来，其中一半带 `#[cfg]` 门控。
- [src/window.rs:64-65](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L64-L65) —— 7500 多行的 `window.rs` 只挂了两个子模块：`a11y`（无障碍树构建，`pub(crate)` 可见）和 `prompts`（原生对话框）。

最后看 `gpui.rs` 里「注册表之外」的部分——它确实定义了几个核心 trait：

[src/gpui.rs:172-245](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L172-L245) —— `AppContext` trait 定义了 `new`（创建实体）、`update_entity`、`read_entity`、`background_spawn`、`read_global` 等方法签名。u1-l2 里你用过的 `cx.new(|_| HelloWorld {})` 正是这个 trait 的方法。把它放在 crate 根，是为了让 `App`、`Context<T>`、`AsyncApp` 等不同上下文都能统一实现同一套接口（配合 [docs/contexts.md](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/docs/contexts.md) 阅读效果最好，那份文档用四段话分别定义了 `App`、`Context<T>`、`AsyncApp`/`AsyncWindowContext`、`TestAppContext` 的分工）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「模块树第一层」与磁盘目录的对应关系。

**操作步骤**：

1. 在 `crates/gpui/` 目录下执行 `ls src/`，数一数顶层 `.rs` 文件和子目录各有多少个。
2. 执行 `grep -c "^mod \|^pub mod \|^#\[cfg" src/gpui.rs` 粗看声明规模，再执行 `grep -n "^mod \|^pub mod " src/gpui.rs > /tmp/mods.txt`，把全部顶层 `mod` 声明导出到一个文件。
3. 对照 `/tmp/mods.txt`，在纸上或笔记里把名字分成两栏：左栏是「对应单个 `.rs` 文件的模块」（如 `geometry`、`style`），右栏是「对应一个目录的模块」（如 `elements`、`platform`）。
4. 任选右栏一个目录，打开它的汇总文件（如 `src/platform.rs` 顶部），看它又挂了哪些二级模块。

**需要观察的现象**：`mod` 声明的数量与 `src/` 顶层条目数大体对应但有出入——出入来自 `cfg` 门控的模块（如 `debug_overlay` 正常构建也会编译，但 `queue` 在 Linux 上默认不参与）和根目录下的非源码目录（`examples/`、`tests/`、`docs/` 不属于库的模块树）。

**预期结果**：得到一张两栏清单，右栏恰好是 7 个目录模块（`app`、`elements`、`keymap`、`platform`、`profiler`、`text_system`、`window`）。若你数出的数量不同，检查是否漏掉了带 `#[cfg(...)]` 前缀的声明（grep 模式 `^mod` 匹配不到它们，因为行首是 `#[cfg...]`）。

#### 4.1.5 小练习与答案

**练习 1**：`src/app.rs` 和 `src/app/` 目录同时存在，为什么不算冲突？

**参考答案**：Rust 允许「`src/app.rs` + `src/app/` 目录」的组合：`app.rs` 是模块 `app` 的入口文件，目录里的 `.rs` 文件是它的二级子模块，需在 `app.rs` 里声明 `mod context;` 等才会挂载（见 [src/app.rs:62-74](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L62-L74)）。这正是 Zed 仓库规避 `mod.rs` 的约定写法。

**练习 2**：如果在 `src/` 下新建一个 `foo.rs` 但不修改 `gpui.rs`，它会被编译吗？

**参考答案**：不会。Rust 只编译从 crate 根可达的模块；没有 `mod foo;` 声明，`foo.rs` 就是一段孤立的文本。这也是为什么 `gpui.rs` 被称为 crate 的注册表。

**练习 3**：`src/gpui.rs` 里 `#[macro_use] mod action;` 与普通 `mod action;` 有什么区别？

**参考答案**：`#[macro_use]` 把该模块里用 `macro_rules!` 定义并导出的宏（如 `actions!`）注入到整个 crate 的宏作用域，之后任何模块都能直接写 `actions!(...)` 而无需 `use`。普通 `mod` 只挂载类型与函数，宏需要显式路径引入。

### 4.2 `pub use` 重导出：扁平 API 与「反查定义文件」

#### 4.2.1 概念说明

如果模块树保持原样，用户要写 `gpui::elements::div::div()` 才能创建一个 div——层级深、路径丑。GPUI 的选择是：**内部按目录分工，对外只留一层**。手段就是 `gpui.rs` 里那一大片 `pub use xxx::*;`。

这带来一个直接后果，也是本讲最重要的技能：**你在 IDE 里看到的 `gpui::XXX` 并不代表 `XXX` 定义在某个叫 `xxx.rs` 的文件里**。`UniformList` 定义在 `src/elements/uniform_list.rs`，`KeyBinding` 定义在 `src/keymap/binding.rs`，但它们最终都叫 `gpui::UniformList`、`gpui::KeyBinding`。于是「反查定义文件」成了阅读 GPUI 源码的高频动作。

#### 4.2.2 核心流程

一个名字从定义到对外可见，要走两级电梯：

```text
定义处          src/elements/uniform_list.rs 里:  pub struct UniformList ...
第一级电梯      src/elements/mod.rs:              pub use uniform_list::*;
第二级电梯      src/gpui.rs:                      pub use elements::*;
最终名字        gpui::UniformList
```

同时存在三档可见性，读懂它们就能判断「这个名字谁能用」：

```text
pub use  xxx::*;     → 外部用户可见（公开 API）
pub(crate) use xxx::*; → 仅 crate 内部可用（如 arena、tab_stop）
use xxx::*;          → 仅当前模块可用（如 key_dispatch、taffy 的引擎类型）
```

#### 4.2.3 源码精读

重导出区的全貌：

[src/gpui.rs:90-112](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L90-L112) 与 [src/gpui.rs:137-166](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L137-L166) —— 几乎每个模块一行 `pub use xxx::*;`：`pub use action::*;`、`pub use app::*;`、`pub use element::*;`、`pub use elements::*;`……直到 `pub use window::*;`。其中混着几行非公开导入，值得逐个理解：

- [src/gpui.rs:96](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L96) `pub(crate) use arena::*;` —— arena（每帧元素分配的竞技场）是内部机制，不对外。
- [src/gpui.rs:143](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L143) `use key_dispatch::*;` —— 派发树是窗口内部实现细节，连 `pub(crate)` 都不给。
- [src/gpui.rs:159-160](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L159-L160) —— `use taffy::TaffyLayoutEngine;` 是私有导入（gpui 内部用），而紧接着的 `pub use taffy::{AvailableSpace, LayoutId};` 只把两个布局类型作为公开 API 放行。同一个模块可以「部分公开」，这是精确控制 API 面的好例子。

外部 crate 的类型也被顺手重导出，例如 [src/gpui.rs:90-92](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L90-L92) 把 `accesskit`（无障碍库）的 `Role`、`Orientation`、`Toggled` 直接搬进 `gpui` 命名空间，用户不必再依赖 accesskit crate。

用户的入口则是 prelude：

[src/prelude.rs:1-9](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/prelude.rs#L1-L9) —— `pub use crate::{AppContext as _, BorrowAppContext, Context, Element, ..., Styled, ...};` 精选了十余个最常用 trait。示例里那句 `use gpui::prelude::*;` 导入的就是这一小撮名字，而不是整个扁平命名空间——trait（尤其是 `Styled`、`ParentElement` 这类提供链式方法的）必须先进入作用域，方法调用语法才能生效。

#### 4.2.4 代码实践

**实践目标**：练习「从公开名字反查定义文件」。

**操作步骤**：

1. 在 `crates/gpui/` 下执行 `grep -rn "pub fn div()" src/`，找到 `div()` 函数的定义文件与行号。
2. 再执行 `grep -rn "pub struct UniformList " src/` 与 `grep -rn "pub struct KeyBinding" src/`。
3. 对每个结果验证两级电梯：`div` 应出现在 `src/elements/div.rs`，确认 [src/elements/mod.rs:20](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/mod.rs#L20) 与 [src/gpui.rs:104](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L104) 把它一路抬到 `gpui::div`。
4. 用 `grep -n "pub use" src/gpui.rs | wc -l` 统计重导出行的数量。

**需要观察的现象**：三个名字的 `grep` 都只命中一处 `pub` 定义（可能有少量同名测试代码，注意甄别路径）；`KeyBinding` 不在 `keymap.rs` 而在 `keymap/` 目录下的某个文件里。

**预期结果**：`div()` 在 `src/elements/div.rs`，`UniformList` 在 `src/elements/uniform_list.rs`，`KeyBinding` 在 `src/keymap/binding.rs`。此后你看到任何 `gpui::XXX` 都可以用同样一条 `grep -rn "struct XXX\|fn xxx" src/` 定位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gpui::key_dispatch` 模块用 `use key_dispatch::*;`（私有）而不是 `pub use`？

**参考答案**：`DispatchTree` 等类型只服务于窗口内部的按键派发流程，不属于对外承诺的 API。私有导入让它们在 `gpui.rs` 根模块可用（供 `window.rs` 等通过 `crate::DispatchTree` 引用需配合 `pub` 与否的实际声明），同时避免出现在文档与自动补全里，将来重构也不算破坏性变更。

**练习 2**：`use gpui::*` 和 `use gpui::prelude::*` 有何区别？写示例时该用哪个？

**参考答案**：前者引入整个扁平命名空间（所有类型、函数、宏）；后者只引入 [src/prelude.rs:5-9](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/prelude.rs#L5-L9) 列出的十余个 trait。官方示例通常 `use gpui::*;` 一把梭，因为示例里类型（`div`、`px`）和 trait（`Styled`）都密集使用；写库代码时用 prelude 更克制。

**练习 3**：`gpui::AvailableSpace` 来自 taffy 这个外部 crate，为什么用户能在 gpui 命名空间下用到它？

**参考答案**：[src/gpui.rs:160](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L160) 显式 `pub use taffy::{AvailableSpace, LayoutId};`，把外部类型重导出为自己的公开 API。自定义 `Element` 时（u4-l1）需要用它向布局引擎描述可用空间，GPUI 因此替用户屏蔽了对 taffy 的直接依赖。

### 4.3 五大子目录职责地图

#### 4.3.1 概念说明

`src/` 下的 7 个目录模块里，有 5 个构成了 GPUI 的骨架，按「离用户距离」从近到远排列：

| 目录 | 职责一句话 | 代表内容 | 后续讲义 |
| --- | --- | --- | --- |
| `app/` | 应用状态的所有权中心：实体表、上下文族 | `entity_map.rs`（EntityMap）、`context.rs`（Context<T>)、`async_context.rs` | u2 全单元 |
| `elements/` | 内置元素库：能直接放进元素树的所有「零件」 | `div.rs`（5197 行）、`list.rs`、`uniform_list.rs`、`text.rs`、`img.rs`、`svg.rs`、`canvas.rs`、`anchored.rs`、`deferred.rs`、`animation.rs` | u3、u6 |
| `window/` | 单窗口的运行时：绘制管线、焦点、动作派发、无障碍、对话框 | `a11y.rs`（无障碍树）、`prompts.rs`（原生对话框） | u4、u5 |
| `platform/` | 操作系统抽象的「接口侧」：定义 Platform 等 trait，及测试用假实现 | `test/` 目录（无窗口测试平台）、`keystroke.rs`、`app_menu.rs` | u7-l1 |
| `text_system/` | 文本与字体：行布局、换行、字形 | `line.rs`、`line_layout.rs`、`line_wrapper.rs`、`font_fallbacks.rs` | u6-l5/l6 |

另有两个小目录：`keymap/`（`binding.rs` 键位绑定 + `context.rs` 键位上下文）服务于 u5-l4 的按键派发；`profiler/` 下则是三个文件——`actions.rs`（动作耗时统计，任何构建都编译），加上提交 1861e58f98 新增的 `journal.rs`（2759 行，前台工作日志：主线程用固定大小环形缓冲记录任务轮询、动作处理、输入派发、窗口绘制与帧呈现）与 `hang.rs`（1268 行，卡顿检测：消费日志并产出 `HangIncident`）。后两个模块都挂在 `#[cfg(feature = "profiler")]` 门后（4.4 节），与 1551 行的 `src/profiler.rs` 本体一起构成性能剖析与可观测性设施——它们与绘制主链路的关系、内部机制留到 u7-l5 与 u7-l6 精读。

需要特别澄清一点：**目录名 ≠ 能力边界**。五大目录是「逻辑重心」而非「围墙」——例如 `app.rs`（3137 行）本体远大于 `app/` 目录里的文件，`window.rs`（7521 行）同理；焦点、hitbox 等你可能以为属于 `window/` 的机制，实际大量写在 `window.rs` 主体里。判断归属的唯一可靠办法是 grep 定义位置（4.2 节的技能）。

#### 4.3.2 核心流程

一条用户界面的更新请求流经五大目录的路径（细节在 u4-l3 展开，这里只建立方位感）：

```text
用户点击 / cx.notify()
   ↓
app/          实体状态被 update，标记需要重绘
   ↓
window.rs     下一帧到来，Window 遍历根视图的元素树
   ↓
elements/     div 等元素的 request_layout / prepaint / paint 被回调
   ↓
window.rs     paint 产生的 Quad/Path 图元写入 Scene
   ↓
platform/     PlatformWindow 把 Scene 交给 GPU（真正绘图在 gpui_linux 等外部 crate）
   ↘ text_system/  过程中按需提供字形与行布局
```

#### 4.3.3 源码精读

每个关键文件的开头 `use crate::{...}` 清单就是一张「我依赖谁」的自白书。先看元素机制的宪法——`element.rs` 的模块文档：

[src/element.rs:1-32](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/element.rs#L1-L32) —— 这段文档信息量极大：元素是「GPUI 的役马（workhorses）」，负责窗口内所有内容的布局与绘制；元素树每帧由 `Render::render()` 重建、帧末全部丢弃；「大多数时候你不需要自己实现 Element」。它是 u4-l1 的预习材料。

[src/element.rs:34-38](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/element.rs#L34-L38) —— `element.rs` 的依赖清单：用到了 `App`、`Context`（来自 app）、`Window`（来自 window）、`Style`（来自 style）、`AvailableSpace`/`LayoutId`（来自 taffy 封装），并且直接调用了 `window::with_element_arena`——这条「element → window」的边是综合实践中要验证的猜测之一。

再看 `div.rs` 的文档与依赖：

[src/elements/div.rs:1-17](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L1-L17) —— div 被定位为「大多数 GPUI 树的构建中心」，文档还预告了 `Interactivity` + `StyleRefinement` 两大系统如何拼出 div。注意文档提到 GPUI 不直接提供 `click`/`drag` 这类多步事件，只给积木——这是 u5-l2 的伏笔。

[src/elements/div.rs:18-33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L18-L33) —— div 的依赖几乎覆盖半个 crate：`Element`（element.rs）、`Style`/`StyleRefinement`（style.rs）、`MouseDownEvent` 等事件类型（定义在 [src/interactive.rs:139](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L139) 等处）、`Window`、`App`——这就是「div 是集大成者」在 import 层面的证据。

两个「持有关系」证据：

- [src/app.rs:684](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L684) —— `pub(crate) platform: Rc<dyn Platform>,`：`App` 结构体持有平台实现的 trait 对象。「app → platform」不是函数调用偏好，而是所有权事实。
- [src/window.rs:1150](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1150) —— `layout_engine: Option<TaffyLayoutEngine>,`：每个窗口持有一个布局引擎实例。「window → taffy 封装」也落实在字段上。

最后，[docs/contexts.md:25-33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/docs/contexts.md#L25-L33) 用官方口吻划清了 `Window`（「不是 context，必须额外传 `&mut App`」）与 `Entity<T>`（「App 拥有数据，句柄只是标识 + 类型标签」）的边界，可作为本节方位感的权威校对。

#### 4.3.4 代码实践

**实践目标**：通过文件头部 `use crate::{...}` 块，量化「谁依赖谁」。

**操作步骤**：

1. 依次打开 `src/element.rs`、`src/elements/div.rs`、`src/window.rs`（只看前 40 行）、`src/platform.rs`（只看前 45 行）、`src/app.rs`（只看前 65 行）。
2. 每个文件把 `use crate::{...}` 展开清单里出现的「其它四大目录代表类型」记下来：`App`、`Context` 记为 app；`Window` 记为 window；`Element`、`Style` 记为 element/style；`Platform*` 记为 platform。
3. 据此在笔记里画 5 个节点、若干箭头的草图（第 5 节综合实践会给参考答案）。

**需要观察的现象**：`platform.rs` 的 `use crate` 里出现了 `App` 和 `Window`（见 [src/platform.rs:37-44](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L37-L44)）——注意方向：这不是「platform 调用 app 的实现」，而是 Platform trait 的方法签名需要以 `&mut Window`、`&mut App` 作为回调参数（例如 [src/platform.rs:1540](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1540) 的 `dispatch_input(&mut self, input: &str, window: &mut Window, cx: &mut App)`）。

**预期结果**：得到一张有向草图，其中 `elements → element/style/interactive`、`window → taffy/platform`、`app → platform`、`element ↔ window`（互相引用）四组边最明显。

#### 4.3.5 小练习与答案

**练习 1**：用户可见的 `div()`、`styled_text()`、`img()` 分别定义在哪个目录？

**参考答案**：全部在 `src/elements/`：`div.rs`、`text.rs`、`img.rs`。它们经 [src/elements/mod.rs:15-27](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/mod.rs#L15-L27) 与 `gpui.rs` 的两级 `pub use` 以 `gpui::div` 等名字暴露。

**练习 2**：为什么 `platform.rs` 定义的是 trait，而真正的 Linux/macOS 实现在 gpui 之外？

**参考答案**：u1-l1 讲过 crate 分工：gpui 只承载跨平台的状态、布局、元素逻辑，并定义 `Platform`、`PlatformWindow` 等接口；具体实现分散在 `gpui_linux`、`gpui_macos`、`gpui_windows` 等后端 crate，由门面 crate `gpui_platform` 按目标操作系统挑选。这样核心 crate 不被任何平台 SDK 污染，测试平台（`platform/test/`）也能以「又一个实现」的身份插入。

**练习 3**：`Entity<T>` 的定义在 `app/` 目录吗？

**参考答案**：`Entity<T>` 等句柄类型在 `src/app/entity_map.rs`（u2-l2 精读），属于 `app` 目录模块；但注意 `AppContext` trait 定义在 [src/gpui.rs:172](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L172) 起的 crate 根——「能力接口放根上、数据结构放目录里」是 gpui 的常见分工。

### 4.4 条件编译：核心代码、平台代码与测试代码的分界

#### 4.4.1 概念说明

同一份 gpui 源码要在 macOS/Linux/Windows/WebAssembly、正常构建/测试构建/文档构建、开/不开某个 feature 之间复用，靠的是 `#[cfg(...)]` 条件编译。本讲只需建立「看到 cfg 能分类」的能力：

| 门控条件 | 含义 | gpui 中的例子 |
| --- | --- | --- |
| `target_os = "linux"` 等 | 目标操作系统 | `platform.rs` 的 `layer_shell`（Wayland 专用） |
| `test` | 仅 `cargo test` 编译单元测试时 | `app.rs` 的 test 系列模块 |
| `feature = "xxx"` | Cargo feature 开关 | `debug_overlay`（profiler）、`scap_screen_capture`（screen-capture） |
| `test, feature = "test-support"` | 二者任一 | 测试基础设施（让外部 crate 也能用） |
| `doc` | 仅生成文档时 | 两个下划线开头的「指南模块」 |

#### 4.4.2 核心流程

判断一个文件属于哪一类的决策树：

```text
文件顶部 / gpui.rs 中该 mod 声明处有 #[cfg(...)] 吗？
├─ 没有                          → 核心机制文件，任何构建都参与
├─ 有 target_os / target_family  → 平台相关代码
├─ 有 test / test-support        → 测试基础设施
├─ 有 feature = "..."            → 可选功能，查 Cargo.toml 确认谁默认开启
└─ 有 doc                        → 只为文档服务的伪模块
```

#### 4.4.3 源码精读

最巧妙的三处：

**其一，文档专用模块**。[src/gpui.rs:69-72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L69-L72)：

```rust
#[cfg(doc)]
pub mod _accessibility;
#[cfg(doc)]
pub mod _ownership_and_data_flow;
```

`_accessibility.rs` 和 `_ownership_and_data_flow.rs`（u2-l2 的主教材）几乎只有文档注释和示例代码，正常编译它们毫无意义甚至可能因 `no_run` 示例报错，于是只在 `cargo doc` 时作为模块编入，以下划线开头排在文档列表最底部。这是「文档即代码」的漂亮实践。

**其二，测试基础设施的宽门**。[src/gpui.rs:59-60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L59-L60) 用 `#[cfg(any(test, feature = "test-support"))] pub mod test;` 同时放行两种场景：gpui 自己跑测试（`test`），以及下游 crate（比如 zed 主程序）开启 `test-support` feature 复用这套设施。`app.rs` 里五个测试模块同样如此（[src/app.rs:67-74](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L67-L74)），其中 `visual_test_context` 额外要求 macOS（[src/app.rs:73-74](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L73-L74)）——条件还能叠加。

**其三，平台目录的门**。[src/platform.rs:1-33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1-L33) 集中了几乎所有 `target_os` 门控：`layer_shell` 仅在 Linux + wayland feature；`threaded_dispatcher`、`test` 仅测试；`visual_test` 仅 macOS 测试；`scap_screen_capture` 需要 screen-capture feature 且限定操作系统。而 `app_menu`、`keyboard`、`keystroke`、`popup` 无门控——它们是所有平台共享的类型定义。

再看一个「按操作系统选择类型别名」的例子：[src/platform.rs:27-35](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L27-L35) 为 `PlatformScreenCaptureFrame` 在不同组合下定义了不同类型（macOS 用 core_video 的缓冲区、Windows/Linux 用 scap 的帧、不开 feature 时干脆是 `()`）——条件编译不仅开关模块，还能切换类型定义。

普通构建也存在的条件门：[src/gpui.rs:41-49](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L41-L49) 的 `queue` 模块只在 Windows/Linux/Wasm 或测试时编译——macOS 主线程有原生队列可用，就不需要这套实现。

还值得注意的是：**门不一定要开在 `gpui.rs`，目录模块的二级挂载点同样可以带 `#[cfg]`**。提交 1861e58f98 新增的前台日志与卡顿检测模块就是现成例子：

[src/profiler.rs:19-24](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler.rs#L19-L24) —— `mod actions;` 无条件编译（动作耗时统计属于普通功能），而 `pub mod hang;` 与 `pub mod journal;` 各自包在 `#[cfg(feature = "profiler")]` 里。不开 profiler feature 时，这两个文件（合计约 4000 行）完全不参与编译。这是 feature 门控的典型取舍：把昂贵的可观测性设施挡在默认构建之外，需要诊断性能时再用 `--features profiler` 换进来。两个模块内部如何工作（环形日志如何记录一次前台 turn、卡顿事故如何判定）留到 u7-l6。

#### 4.4.4 代码实践

**实践目标**：统计并分类 `src/` 里的条件编译门。

**操作步骤**：

1. 执行 `grep -rn "^#\[cfg(" src/ --include="*.rs" | grep -v "examples" | wc -l` 得到门控总数（含模块级与项级）。
2. 执行 `grep -rn "^#\[cfg(" src/ --include="*.rs" | grep -o 'cfg([^)]*)' | sort | uniq -c | sort -rn | head -15`，看哪类条件最常见。
3. 执行 `cargo tree -f "{p} {f}" -p gpui 2>/dev/null | head -3` 或直接查看 [Cargo.toml](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/Cargo.toml) 的 `[features]` 段，对照哪些 feature 默认开启。
4. 挑一个 feature（如 `test-support`），执行 `grep -rn 'feature = "test-support"' src/ | head`，数数它放行了多少模块。

**需要观察的现象**：`target_os` 与 `any(test, feature = "test-support")` 两类占比最高；绝大多数门控行在 `platform.rs`、`app.rs`、`gpui.rs` 三个文件里，而 `elements/`、`style.rs` 等纯逻辑文件几乎为零——条件编译集中在「边界层」，核心机制保持全平台一致。

**预期结果**：得出「UI 逻辑无门控、平台与测试高密度门控」的结论。如果第 2 步的统计因行首缩进匹配不全（例如模块内部字段的 cfg），改用 `grep -rn "#\[cfg(" src/ | grep -o 'cfg([^)]*)' | sort | uniq -c | sort -rn`。具体数字随仓库版本浮动，属正常现象，记录你当时的结果即可。

#### 4.4.5 小练习与答案

**练习 1**：`cargo build -p gpui` 之后，`_ownership_and_data_flow` 模块存在吗？`cargo doc -p gpui` 呢？

**参考答案**：普通构建不存在——它被 `#[cfg(doc)]` 挡住（[src/gpui.rs:71-72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L71-L72)）；`cargo doc` 时才作为 `pub mod` 编入文档。所以你在 IDE 的正常分析里几乎看不到它，但在 docs.rs/gpui 的文档页能看到。

**练习 2**：zed 主程序的测试想复用 gpui 的 `TestAppContext`，需要做什么？

**参考答案**：在依赖声明中给 gpui 打开 `test-support` feature（如 `[dependencies] gpui = { ..., features = ["test-support"] }` 或 dev-dependencies 中开启）。`test` cfg 只对 gpui crate 自身测试生效，跨 crate 必须走 feature 门，这正是 `any(test, feature = "test-support")` 双条件存在的原因（[src/gpui.rs:59-60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs#L59-L60)）。

**练习 3**：为什么 `elements/div.rs` 里几乎找不到 `#[cfg(target_os = ...)]`？

**参考答案**：div 的工作（组织样式、注册事件回调、生成绘制指令）与操作系统无关，平台差异被压到了 `platform/` 与外部后端 crate。这种「把 cfg 推向边界」的纪律让 UI 层代码可以在所有平台用同一套逻辑测试与维护。

## 5. 综合实践

**任务：手绘 gpui 核心模块依赖草图，并用 grep 验证至少三处猜测。**

这是本讲的收官实践，产物是一张你自己验证过的依赖图——后续每讲精读某个模块时，都可以把新知识钉回这张图上。

**步骤**：

1. **画草图（先猜后验）**：在纸上画 6 个节点：`element.rs`、`elements/div.rs`、`window.rs`、`platform.rs`、`app.rs`、`taffy.rs`。凭 4.3 节的方位感，先凭记忆画出你认为的依赖箭头（A → B 表示「A 的代码里引用了 B 提供的类型/函数」），并给每个箭头写一句理由。
2. **逐条验证**：对每条猜测，用一条 grep 找到证据行。推荐三条起点命令：
   - `grep -n "window::with_element_arena" src/element.rs` —— 验证 element → window；
   - `grep -n "TaffyLayoutEngine" src/window.rs` —— 验证 window → taffy（use 导入 + `layout_engine` 字段两处证据，[src/window.rs:1150](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1150)）；
   - `grep -n "Rc<dyn Platform>" src/app.rs` —— 验证 app → platform（[src/app.rs:684](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L684)）。
3. **标记双向边**：`element.rs` 与 `window.rs` 互有引用（element 的三阶段方法接收 `&mut Window`；window 调用元素的布局与绘制）。用 `grep -n "use crate::{" src/window.rs | head -1` 加查看其导入块确认 window 一侧。

**参考答案（基于当前 HEAD 验证）**：

```text
elements/div.rs ──→ element.rs（实现 Element trait，div.rs:18-33 导入 Element/ElementId）
       │──────────→ style.rs（Style/StyleRefinement）
       │──────────→ interactive.rs（MouseDownEvent 等事件类型，定义于 interactive.rs:139 起）
       └──────────→ app.rs / window.rs（App、Window 参数遍布回调签名）
element.rs ──→ window.rs（element.rs:37 导入 window::with_element_arena）
window.rs  ──→ taffy.rs（window.rs:1150 持有 TaffyLayoutEngine 字段）
window.rs  ──→ platform.rs（导入 PlatformWindow/PlatformAtlas 等接口类型）
app.rs     ──→ platform.rs（app.rs:684 持有 platform: Rc<dyn Platform>）
platform.rs ──→ app.rs / window.rs（Platform trait 方法签名以 &mut App / &mut Window 为参数，
              如 platform.rs:1540 的 dispatch_input —— 注意这是「签名引用」而非「实现调用」）
```

**需要观察的现象与预期结果**：grep 输出的行号与本讲给出的永久链接行号一致（若上游代码演进而漂移，以你本地的 grep 结果为准，这正是「先跑命令再下结论」的训练）。画完的图应呈现出清晰的层次：`elements`（离用户最近）依赖几乎一切；`app` 与 `platform` 位于底部，且通过 trait 对象与回调参数彼此握手；`window` 居中调度。**待本地验证**：三条 grep 命令的输出行号请以你机器上的仓库状态为准。

## 6. 本讲小结

- [src/gpui.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/gpui.rs) 是 crate 的注册表：约 40 条 `mod` 声明挂载全部顶层文件与 7 个目录模块，未在其中声明的文件不参与编译；它同时亲自定义 `AppContext`、`VisualContext` 等最高层 trait。
- 扁平 API 靠两级 `pub use ... *` 电梯拼成：定义文件 → 目录汇总模块（如 [src/elements/mod.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/mod.rs)）→ crate 根；`pub use` / `pub(crate) use` / `use` 三档对应公开 API、内部共享、模块私有。
- 反查定义文件是读 GPUI 的基本功：`gpui::XXX` 的真实定义位置与名字表面无关，一条 `grep -rn "struct XXX\|fn xxx" src/` 即可定位（如 `KeyBinding` 在 `src/keymap/binding.rs`）。
- 五大目录各司其职：`app/` 状态与所有权、`elements/` 内置元素库、`window/` 单窗口运行时（配合 7521 行的 `window.rs` 主体）、`platform/` 操作系统接口侧、`text_system/` 文本与字体；`keymap/`、`profiler/` 是两个小而专的目录，其中 `profiler/` 现含 `actions.rs` + feature 门控的 `journal.rs`/`hang.rs` 三件套。
- 条件编译集中在边界层：`#[cfg(doc)]` 的两个指南模块、`any(test, feature = "test-support")` 的测试设施、`target_os`/feature 门控的 `platform.rs` 子模块；核心 UI 逻辑（div、style、elements）几乎无门控。
- 文件头部的 `use crate::{...}` 块是可靠的依赖自白书；`app.rs:684` 的 `platform: Rc<dyn Platform>` 与 `window.rs:1150` 的 `layout_engine` 两个字段，把「app 持有平台、window 持有布局引擎」落实为所有权事实。

## 7. 下一步学习建议

- **下一讲 u1-l4（示例全景）**：把 `examples/` 目录当地图用，按布局、交互、图片动画、窗口行为四类各跑一个示例，为每个示例记下它「住在 `src/` 的哪个目录」。本讲的五大目录分类正好用作标注坐标。
- **进入 u2 之前**，建议通读 [docs/contexts.md](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/docs/contexts.md)（四段短文）和 [src/_ownership_and_data_flow.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/_ownership_and_data_flow.rs)（`cargo doc -p gpui --open` 后在 crate 文档列表底部能看到渲染版本），它们是 u2 全单元的官方教材。
- **练熟两个 grep 招式**再往下走：`grep -rn "pub struct XXX" src/` 反查定义、`grep -n "use crate::{" src/xxx.rs` 查依赖方向。后续每讲开篇的「源码地图」都可以用它们自行重推。
- 若想提前感受最大的两个文件，可以只浏览 [src/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs) 与 [src/elements/div.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs) 的模块级文档注释（各文件头 30 行），它们分别是 u4（绘制管线）与 u3/u5（div 与交互）的预习材料——现在读不懂细节完全正常，混个脸熟即可。
