# typst-realize 是什么：realization 在编译流水线中的位置

## 1. 本讲目标

本讲是整本 `typst-realize` 学习手册的第一篇，目标是让你**从零建立对这个 crate 的整体认知**。读完本讲，你应该能够：

1. 用一句话说清 **realization（具现化）** 是什么、它解决了 Typst 编译过程中的什么问题。
2. 画出 Typst「评估 → 具现化 → 排版/导出」三段式流水线，并指出 `realize` 处在哪一段、负责什么、不负责什么。
3. 看懂 `realize()` 函数的签名——它**接收什么（content + styles）**、**产出什么（`Vec<Pair>`）**。
4. 说出 `typst-realize` crate 的目录结构（`lib.rs` 与 `spaces.rs`）和它依赖了哪些兄弟 crate。
5. 理解为什么 `realize` 不是被直接调用，而是通过一个名为 `Routines` 的「函数指针表」分发的。

本讲**不**深入 `realize` 内部的调度逻辑（那是后面几篇的内容），只做「定位」与「入口」层面的理解。

---

## 2. 前置知识

如果你对以下概念完全陌生，建议先花几分钟了解，再继续本讲。

- **Typst 是什么**：一个用标记语言（markup）写文档、再编译成 PDF / HTML 的排版系统，定位类似 LaTeX，但语法更简洁。可以理解为「写源码 → 出排版结果」的编译器。
- **content（内容）**：Typst 在运行期间表示「待排版内容」的核心数据类型。一段文字、一个标题、一个列表，在内部都是一个 `Content` 值。多个 `Content` 可以组合成一棵「内容树」。
- **样式（styles）**：描述「内容长什么样」的规则，比如字号、颜色、对齐方式。Typst 用 `StyleChain`（样式链）来携带这些信息。
- **show 规则**：Typst 源码里 `show heading: it => [...]` 这类语法，作用是「遇到某种元素时，把它变换成别的内容」。这是用户自定义内容外观的核心机制。
- **crate**：Rust 里的「包」概念。Typst 整个仓库是一个 Cargo workspace，下面拆成了几十个 crate（`typst-library`、`typst-layout`、`typst-html`、`typst-realize` 等），每个 crate 负责一块职责。

> 名词翻译约定：本文把 **realization** 译为「具现化」，强调它把「抽象的内容意图」落实成「排版引擎可直接消费的具体元素清单」。后文也会保留英文 `realize` 便于对照源码。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | 本 crate 的主文件。开头有顶层文档注释解释什么是 realization；`pub fn realize()` 入口函数也在这里。本篇主要看它的前 74 行。 |
| `crates/typst-realize/src/spaces.rs` | 空格折叠（space collapsing）的实现，是 `lib.rs` 之外唯一的源文件。本篇只需知道它存在，细节留到后面的讲义。 |
| `crates/typst-realize/Cargo.toml` | 本 crate 的清单文件，记录依赖了哪些兄弟 crate 和第三方库。 |
| `crates/typst-library/src/routines.rs` | 定义 `Routines` 结构体与 `realize` 在其中的声明，以及 `RealizationKind`、`Pair`、`Arenas` 等关键类型。 |
| `crates/typst-layout/src/flow/mod.rs` | 排版侧的一个真实调用点——这里会通过 `routines.realize` 调用本 crate。本篇用它来体会「谁在用 realize」。 |

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：

1. **realization 是什么**（结合 crate 顶层文档注释）
2. **本 crate 的依赖清单**（结合 `Cargo.toml`）
3. **realize 如何被声明与分发**（结合 `Routines` 结构体中的 `realize` 声明）

---

### 4.1 realization（具现化）的概念与流水线定位

#### 4.1.1 概念说明

先建立一个直觉：Typst 编译文档时，**用户写出来的内容**和**排版后端实际能消费的内容**，并不是同一种东西。

- 用户侧：可以自定义任意元素，可以写 `show` 规则把 `heading` 变成一朵花、把 `figure.where(kind: table)` 重写。也就是说，评估（eval）阶段产出的 `Content` 树是**开放的、用户可扩展的**，里面可能有大量「后端不认识」的元素。
- 后端侧：`typst-layout`（排版成 frame）、`typst-pdf`（出 PDF）、`typst-html`（出 HTML）只认得一组**固定、已知**的元素，比如 `ParElem`（段落）、`TextElem`（文本）、`PageElem`（页面）。

这两者之间需要一个「翻译 / 规整」步骤：**递归地套用样式和 show 规则，把任意 content 树规整成一个「扁平的、全部由已知元素组成、且每个元素都带着它该有的样式」的列表。** 这个步骤就是 **realization（具现化）**。

> 为什么叫「具现化」：评估阶段产出的是「意图」（我想在这里放一个标题，它具体怎么显示由 show 规则决定）；realization 把这些意图「落实、具现」成排版引擎能直接摆放的「实物」。从「可能」到「确定」，所以叫「具现」。

它解决的核心问题是：**让后端不必关心用户自定义的元素和 show 规则，只面对一组稳定的已知元素。** 这样后端可以保持简单、专注；所有「语义变换」的复杂性都集中在 realization 这一层。

#### 4.1.2 核心流程

Typst 的编译主链路可以概括为三段：

```
源码 (.typ)
   │  ① 评估 eval          （typst-eval / typst-library）
   ▼
Content 内容树（任意、可扩展、可能含用户自定义元素与未应用的 show 规则）
   │  ② 具现化 realize      （typst-realize）  ← 本手册的主角
   ▼
Vec<Pair> = 扁平的「已知元素 + 各自样式」列表
   │  ③ 排版 / 导出          （typst-layout → frame；typst-pdf / typst-html）
   ▼
PDF / HTML / 图像
```

- **职责边界（上游）**：realization 的输入是 eval 的产物 `Content`，**不**负责执行 Typst 代码本身（那是 eval 的事）。
- **职责边界（下游）**：realization 的输出是一个元素**清单**，它**不**负责把元素摆放到页面上算坐标（那是 layout 的事）。它只保证「清单里的每个元素都是后端认得的已知类型，且样式齐全」。

一句话：**realize 把「内容树」压平成「带样式的元素清单」，是 eval 与 layout/导出之间的唯一桥梁。**

#### 4.1.3 源码精读

这个定位最直接的证据，就是 crate 顶层的文档注释：

[crates/typst-realize/src/lib.rs:1-5](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L1-L5) —— 顶层 `//!` 文档注释，用三句话定义了 realization：

```rust
//! Typst's realization subsystem.
//!
//! *Realization* is the process of recursively applying styling and, in
//! particular, show rules to produce well-known elements that can be processed
//! further.
```

翻译：realization 是**递归地套用样式（特别是 show 规则），以产出可被进一步处理的「已知元素」**的过程。这里「可被进一步处理」指的就是下游的 layout 与导出。

而这一切的入口，就是同一个文件里的 `pub fn realize()`。先只看它的签名，体会「输入 / 输出」：

[crates/typst-realize/src/lib.rs:41-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L41-L50) —— `realize` 函数签名：

```rust
/// Realize content into a flat list of well-known, styled items.
#[typst_macros::time(name = "realize")]
pub fn realize<'a>(
    kind: RealizationKind,
    engine: &mut Engine,
    locator: &mut SplitLocator,
    arenas: &'a Arenas,
    content: &'a Content,        // ← 输入：待具现化的内容树
    styles: StyleChain<'a>,      // ← 输入：伴随的样式链
) -> SourceResult<Vec<Pair<'a>>> // ← 输出：扁平的「已知元素 + 样式」列表
```

- 输入核心是 `content`（内容树）与 `styles`（样式链），正对应「意图 + 外观」。
- 输出 `Vec<Pair<'a>>` 中每个 `Pair` 就是「一个已知元素 + 它的样式链」（类型定义见 4.3 节）。
- `kind: RealizationKind` 用来区分「这次具现化是在什么场景下做的」（整篇文档？某个容器内？数学环境里？），它会影响用哪套分组规则——这是后续讲义的重点，本篇先知道有这么个参数即可。

函数体非常短，主干只有三步（[lib.rs:51-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L51-L73)）：

```rust
let mut s = State { /* ...初始化状态机... */ };
visit(&mut s, content, styles)?;   // 递归访问并规整每个元素
finish(&mut s)?;                   // 收尾：闭合所有未完成的分组、折叠空格
Ok(s.sink)                         // 返回累积的结果列表
```

这个 `visit` → `finish` → 返回 `sink` 的结构，就是整本手册后续要逐层拆解的主循环。本篇你只需要记住：**所有规整后的元素最终都堆进 `s.sink` 这个 `Vec`，然后整体返回。**

#### 4.1.4 代码实践

**实践目标**：通过亲手阅读，确认 realization 的「输入 / 输出」定义，并用自己的话复述。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 `crates/typst-realize/src/lib.rs` 第 1–5 行，阅读顶层文档注释。
2. 跳到第 41–50 行的 `pub fn realize` 签名。
3. 在你自己的 fork / 笔记里，针对 `realize` 写一段注释，例如：

   ```text
   // realize 接收：
   //   - content: 任意 content 树（可能含用户自定义元素、未应用的 show 规则）
   //   - styles:  伴随内容的外层样式链
   // realize 产出：
   //   - Vec<Pair>: 扁平列表，每个元素都是后端认识的「已知元素」并带上其样式
   ```

**需要观察的现象 / 预期结果**：你能不看资料，用一句话回答「realize 的输入是什么、输出是什么」。预期答案是：「输入 content + styles，输出扁平的已知元素 + 样式列表 `Vec<Pair>`」。如果你无法用一句话概括，就回头再看一遍 4.1.1。

#### 4.1.5 小练习与答案

**练习 1**：如果没有 realization 这一步，排版后端（`typst-layout`）会面临什么麻烦？
> **参考答案**：后端将不得不自己处理用户自定义的元素和所有 show 规则，导致后端逻辑极其复杂、且与用户脚本耦合；不同导出后端（PDF / HTML）还得各自重复实现一遍。realization 把这些复杂性集中到一层，给后端一个干净的「已知元素清单」。

**练习 2**：`realize` 的输出类型是 `Vec<Pair>`。请回忆 `Pair` 大致代表什么（即使还没精确定义）。
> **参考答案**：`Pair` 是「一个元素 + 作用于它的样式链」的组合。`Vec<Pair>` 就是「一串各自带好样式的已知元素」。

**练习 3**：realization 属于「排版（算坐标、摆放到页面）」这一步吗？
> **参考答案**：不属于。realization 只负责把内容树规整成已知元素清单；「摆放到页面、计算坐标」是下游 `typst-layout` 的工作。realization 是二者之间的桥梁，本身不算坐标。

---

### 4.2 本 crate 的依赖清单（Cargo.toml）

#### 4.2.1 概念说明

要理解一个 crate「干什么」，最快的方法之一是看它**依赖谁**。依赖关系往往暴露了它和哪些子系统打交道。

`typst-realize` 体积很小（主文件一千多行、外加一个空格折叠小文件），但它的依赖清单能告诉我们：它需要操作 Typst 的内容与样式（`typst-library`）、需要处理 HTML 元素（`typst-html`）、需要一套内存分配与集合工具（`bumpalo` / `arrayvec`）、还用到正则（`regex`）。这些都和「递归套用 show 规则、把文本规整成段落」这个职责高度吻合。

#### 4.2.2 核心流程

把依赖按「职责」归类，便于记忆：

| 依赖 | 类别 | 在 realization 中扮演的角色（概述） |
| --- | --- | --- |
| `typst-library` | 兄弟 crate（核心） | 提供 `Content`、`StyleChain`、`Engine`、各种已知元素类型（`ParElem`、`TextElem`…）、`Routines`/`Pair`/`Arenas` 类型。本 crate 几乎所有类型都来自它。 |
| `typst-html` | 兄弟 crate | 提供 `HtmlElem`，用于在分组规则里判断 HTML 元素该归入段落还是作为中性元素。 |
| `typst-macros` | 兄弟 crate | 提供 `#[typst_macros::time(name = "realize")]` 计时属性（见 `realize` 函数上方的标注）。 |
| `typst-syntax` | 兄弟 crate | 提供 `Span`，用于错误定位到源码位置。 |
| `typst-timing` | 第三方（Typst 自有） | 与上面 `time` 宏配合，做性能计时。 |
| `typst-utils` | 第三方（Typst 自有） | 提供工具集合（如 `ListSet`、`SmallBitSet`），用于标签集合、正则撤销位集等。 |
| `arrayvec` | 第三方 | 提供「栈上定长数组」`ArrayVec`，用来存当前活跃的分组（有最大嵌套层数限制）。 |
| `bumpalo` | 第三方 | 提供 bump arena 内存分配器，用于在具现化期间「延长」临时 content/styles 的生命周期。 |
| `comemo` | 第三方 | Typst 用的增量计算（memoization / tracking）框架，本 crate 里用到 `Track` 等 trait。 |
| `ecow` | 第三方 | 提供经济型写时复制字符串 `EcoString`，文本元素大量使用。 |
| `regex` | 第三方 | 支撑「正则 show 规则」（如 `show "ab": …`）在连续文本上做匹配。 |

> 注意：本 crate **不依赖** `typst-layout`、`typst-pdf`。这印证了 4.1 的职责边界——realization 是 layout 的**上游**，不应反过来依赖排版层。

#### 4.2.3 源码精读

[crates/typst-realize/Cargo.toml:1-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/Cargo.toml#L1-L29) —— 本 crate 的清单与依赖。关键片段：

```toml
[package]
name = "typst-realize"
description = "Typst's realization subsystem."
# version / rust-version / edition 等都继承自 workspace
...

[dependencies]
typst-html = { workspace = true }
typst-library = { workspace = true }
typst-macros = { workspace = true }
typst-syntax = { workspace = true }
typst-timing = { workspace = true }
typst-utils = { workspace = true }
arrayvec = { workspace = true }
bumpalo = { workspace = true }
comemo = { workspace = true }
ecow = { workspace = true }
regex = { workspace = true }
```

两点解读：

- `description = "Typst's realization subsystem."`（第 3 行）再次官方确认了本 crate 的自我定位：**realization 子系统**。
- 所有依赖都写成 `{ workspace = true }`，说明版本统一在根 `Cargo.toml` 的 `[workspace.dependencies]` 里管理。本 crate 在 workspace 中的注册可以在根 `Cargo.toml` 找到（典型 Rust workspace 写法）。

#### 4.2.4 代码实践

**实践目标**：确认 `typst-realize` 在 workspace 中的位置，并理解每个依赖的用途。

**操作步骤**（源码阅读型实践）：

1. 打开仓库根目录的 `Cargo.toml`，找到 `members = [...]` 一行，你会看到它用 `crates/*` 这种通配把 `crates/` 下所有 crate 都纳入 workspace。
2. 在同一个根 `Cargo.toml` 的 `[workspace.dependencies]` 段里搜索 `typst-realize`，你会看到形如 `typst-realize = { path = "crates/typst-realize", version = "0.15.1" }` 的声明——这就是本 crate 在 workspace 里的「注册」。
3. 回到 `crates/typst-realize/Cargo.toml`，对照 4.2.2 的表格，逐个依赖在脑子里回想「它为什么被需要」。

**需要观察的现象 / 预期结果**：你能解释清楚「为什么依赖 `typst-html`」——因为它要在分组规则里识别 HTML 元素（`HtmlElem`）；以及「为什么**没有**依赖 `typst-layout`」——因为 realization 是排版的上游。如果你能答出这两点，说明你理解了依赖背后的架构意图。

#### 4.2.5 小练习与答案

**练习 1**：依赖里的 `arrayvec` 和 `bumpalo` 分别用在 realization 的哪个环节？
> **参考答案**：`arrayvec` 提供定长栈上数组，用来存放「当前活跃的分组」栈（嵌套层数有上限）；`bumpalo` 提供 bump 内存池，用来延长具现化过程中临时产生的 content/styles 的生命周期，避免频繁分配释放。

**练习 2**：为什么 `typst-realize` 依赖 `regex`？
> **参考答案**：因为 Typst 支持「正则 show 规则」（如 `show "x,y": ...`）。realization 需要在连续的文本元素上做正则匹配并切片应用规则，所以需要 `regex`。

**练习 3**：从依赖清单判断，`typst-realize` 能否调用 `typst-layout` 的函数？
> **参考答案**：不能。依赖清单里没有 `typst-layout`，说明 realization 是 layout 的上游；调用方向是「layout 调用 realize」，而不是反过来（否则会形成循环依赖）。

---

### 4.3 realize 如何被声明与分发：Routines 结构体

#### 4.3.1 概念说明

这里有一个初学者容易困惑的点：既然 `realize` 定义在 `typst-realize` 里，为什么下游（如 `typst-layout`）不是直接 `typst_realize::realize(...)` 这样调用，而是写 `(engine.library.routines.realize)(...)`？

原因是 **crate 拆分（crate splitting）**。Typst 的 crate 之间有严格的依赖方向：`typst-realize` 依赖 `typst-library`（要用里面的类型），而 `typst-layout` 也依赖 `typst-library`。如果 `typst-layout` 直接依赖 `typst-realize`，某些情况下会形成不希望的依赖耦合。

Typst 的解决办法是：在「大家都依赖」的 `typst-library` 里定义一个叫 **`Routines`** 的结构体，它本质上是一张**「函数指针表」**（官方注释原话：*"essentially dynamic linking and done to allow for crate splitting"*——本质上是动态链接，目的是支持 crate 拆分）。各个真正的实现（`realize`、`layout_frame`、`eval_string` 等）以**函数指针**的形式注册进这张表，运行时通过 `engine.library.routines.realize(...)` 来间接调用。

> 关于「trait 还是 struct」：讲义规格里把它称作「Routines trait」，但源码里它其实是一个**结构体**（`struct Routines`，每个字段是一个函数指针），由一个 `routines!` 宏生成。它在**概念上**扮演了「接口 / 契约」的角色（声明了有哪些例程、各自签名如何），但实现方式是函数指针表，而非 Rust trait。这一点要和源码对齐，别被「接口」的直觉误导。

#### 4.3.2 核心流程

一次完整的「下游调用 realize」流程如下：

```
typst-layout（如 flow 模块）
   │  想把 content 排版，需要先具现化
   ▼
读取 engine.library.routines.realize   （从 library 里取出函数指针）
   │  传入 RealizationKind::Fragment { .. }、engine、locator、arenas、content、styles
   ▼
函数指针指向 typst_realize::realize   （真正的实现）
   │  递归规整
   ▼
返回 Vec<Pair> 给 layout，layout 继续排版
```

关键在于：调用方只依赖 `typst-library`（拿到 `Routines` 类型与函数指针），不需要在编译期直接依赖 `typst-realize`。这就是「函数指针表」换来的解耦。

#### 4.3.3 源码精读

首先看 `Routines` 是怎么被定义出来的。它由 `routines!` 宏生成：

[crates/typst-library/src/routines.rs:20-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L20-L48) —— 宏把每个 `fn name(...) -> Ret` 声明变成 `Routines` 结构体里的一个**函数指针字段**。宏的文档注释说得很直白：

```rust
/// Defines implementation of various Typst compiler routines as a table
/// of function pointers.
///
/// This is essentially dynamic linking and done to allow for crate
/// splitting.
pub struct Routines { ... }
```

然后看 `realize` 在这张表里的声明（注意它只是**声明签名**，不含函数体）：

[crates/typst-library/src/routines.rs:81-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L81-L89) —— `realize` 例程的声明：

```rust
/// Realizes content into a flat list of well-known, styled items.
fn realize<'a>(
    kind: RealizationKind,
    engine: &mut Engine,
    locator: &mut SplitLocator,
    arenas: &'a Arenas,
    content: &'a Content,
    styles: StyleChain<'a>,
) -> SourceResult<Vec<Pair<'a>>>
```

这与 4.1.3 里 `typst-realize/src/lib.rs` 中 `pub fn realize` 的签名**完全一致**——后者就是这张表里 `realize` 槽位被填入的真实实现。签名一致是函数指针能赋值的前提。

声明紧挨着的几个相关类型也定义在同一个文件，理解它们有助于你看懂签名：

[crates/typst-library/src/routines.rs:195-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L195-L196) —— `Pair` 类型别名，就是「元素 + 样式链」的二元组：

```rust
/// A pair of content and a style chain that applies to it.
pub type Pair<'a> = (&'a Content, StyleChain<'a>);
```

[crates/typst-library/src/routines.rs:182-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L182-L193) —— `Arenas`，具现化期间用的临时内存池（content / styles / bump 三块）：

```rust
/// Temporary storage arenas for lifetime extension during realization.
/// Must be kept live while the content returned from realization is processed.
#[derive(Default)]
pub struct Arenas {
    pub content: typed_arena::Arena<Content>,
    pub styles: typed_arena::Arena<Styles>,
    pub bump: bumpalo::Bump,
}
```

> 小提示：`Arenas` 的注释提醒——返回的 `Vec<Pair>` 里某些 `&Content` 可能指向 arena 里的内存，所以 arena 必须在消费完结果前一直存活。这就是为什么调用方（见下面的 flow 例子）会在栈上先建好 `arenas` 再调用 `realize`。

最后看一个**真实调用点**，体会「函数指针表」的用法：

[crates/typst-layout/src/flow/mod.rs:150-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L150-L159) —— `typst-layout` 的 flow 模块调用 realize：

```rust
let mut kind = FragmentKind::Block;
let arenas = Arenas::default();
let children = (engine.library.routines.realize)(
    RealizationKind::Fragment { kind: &mut kind },
    &mut engine,
    &mut locator,
    &arenas,
    content,
    styles,
)?;
```

注意三件事：

1. 调用形如 `(engine.library.routines.realize)(...)`——先从 `engine` 取到 `library`，再取 `routines`，再取 `realize` 这个函数指针，最后用括号包起来当成函数调用。
2. 这里用的是 `RealizationKind::Fragment { .. }`，表示「在一个容器内部做具现化」，并会用 `kind` 把结果是否「全是行内内容」反馈回来。
3. `arenas` 在调用前创建、调用后还被 `children` 引用，符合上面 `Arenas` 注释的要求。

除了 flow，`typst-layout` 的 `inline/mod.rs` 和 `pages/mod.rs` 也以类似方式调用 `routines.realize`，分别用于行内排版与页面级排版。

#### 4.3.4 代码实践

**实践目标**：亲手找到一个真实的 `routines.realize` 调用点，理解「函数指针表」的调用语法。

**操作步骤**（源码阅读型实践）：

1. 打开 `crates/typst-layout/src/flow/mod.rs`，定位到第 150–159 行（即上面引用的片段）。
2. 注意 `(engine.library.routines.realize)(...)` 这一行的写法：外层括号说明它是一个「函数指针被取出后立即调用」，而不是普通的方法调用。
3. 作为对照，再用编辑器全局搜索 `routines.realize`（或 `library.routines.realize`），你会找到 `typst-layout` 的 `inline/mod.rs`、`pages/mod.rs` 等多处调用，它们的模式都一样。

**需要观察的现象 / 预期结果**：你应当能区分两种写法的差别——

- 直接调用（**不存在**于跨 crate 场景）：`typst_realize::realize(...)`
- 通过例程表调用（**实际**写法）：`(engine.library.routines.realize)(...)`

并理解为什么 Typst 选了后者：为了在 `typst-library` 这个「公共落脚点」声明接口、在 `typst-realize` 提供实现，避免下游 crate 直接耦合到 realize crate。

#### 4.3.5 小练习与答案

**练习 1**：`Routines` 在源码里是 trait 还是 struct？为什么要这样设计？
> **参考答案**：它是 `struct`，由 `routines!` 宏生成，每个字段是一个函数指针。官方注释说这「本质上是动态链接，用于支持 crate 拆分」。用函数指针表而非 trait，可以让接口声明（在 `typst-library`）与实现（在 `typst-realize` 等）分离，下游只依赖 `typst-library` 即可调用，不必直接依赖实现 crate。

**练习 2**：在 `flow/mod.rs` 的调用里，为什么 `arenas` 要在调用 `realize` **之前**创建，并且在调用返回后仍然保持存活？
> **参考答案**：因为 `realize` 返回的 `Vec<Pair>` 中，部分 `&Content` 可能指向 `arenas` 内部分配的内存（用于延长临时 content 的生命周期）。`Arenas` 的文档注释明确要求它必须在结果被消费完之前一直存活，否则会出现悬垂引用。

**练习 3**：`realize` 在 `routines.rs` 里的声明（81–89 行）和 `typst-realize/src/lib.rs` 里的 `pub fn realize` 签名必须保持一致，为什么？
> **参考答案**：因为 `Routines::realize` 是一个函数指针字段，要把 `typst_realize::realize` 这个真实函数赋值给它，二者的参数与返回类型必须完全匹配，否则函数指针无法赋值、编译不过。签名一致正是「声明」与「实现」能对接的前提。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务。

**任务**：在本地 clone typst 仓库后，完成三件事，并把你的发现整理成一份简短笔记。

1. **定位 crate**：找到 `crates/typst-realize/Cargo.toml`，指出它在 workspace 中的位置（提示：根 `Cargo.toml` 的 `members` 与 `[workspace.dependencies]`）。
2. **描述输入 / 输出**：参考 `crates/typst-realize/src/lib.rs` 第 41–50 行的 `pub fn realize` 签名，写一段注释说明 realize **接收什么（content + styles）**、**产出什么（`Vec<Pair>`）**，并说明 `Pair` 的含义（参考 `routines.rs:195-196`）。
3. **找到调用点**：在 `crates/typst-layout/src/flow/mod.rs` 中找到调用 `routines.realize` 的那一行（第 150–159 行附近），记录：
   - 它用的是哪种 `RealizationKind`？（`Fragment`）
   - 调用语法为什么写成 `(engine.library.routines.realize)(...)` 而不是直接调用？

**预期结果**：你的笔记里应能清晰回答——「realization 是 eval 与 layout 之间的桥梁，输入 content+styles、输出扁平的已知元素清单；它通过 `Routines` 函数指针表被下游调用，以实现 crate 解耦」。如果三条都能答上，本讲目标达成。

> 说明：以上均为源码阅读型任务，不要求编译或运行项目；如果你尝试编译，遇到环境问题是正常的，不影响完成本实践。具体运行结果「待本地验证」。

---

## 6. 本讲小结

- **realization（具现化）** 是「递归套用样式与 show 规则，把任意 content 树规整成扁平的、由已知元素组成的清单」的过程——它是 eval 与 layout/导出之间的关键桥梁。
- 入口 `pub fn realize()` 的核心契约是：**输入 `content` + `styles`，输出 `Vec<Pair>`**，其中每个 `Pair = (&Content, StyleChain)` 是「一个已知元素 + 它的样式链」。
- `typst-realize` 目录结构极简：主逻辑在 `src/lib.rs`，空格折叠在 `src/spaces.rs`；依赖里最重要的是 `typst-library`（提供类型）和 `typst-html`（提供 HTML 元素判断），并刻意**不依赖** `typst-layout`，体现「上游」定位。
- `realize` 不被直接跨 crate 调用，而是通过 `typst-library` 里 `Routines` 这张**函数指针表**分发，目的是支持 crate 拆分、让下游只耦合到 `typst-library`。
- `Routines::realize` 的声明（`routines.rs`）与 `typst_realize::realize` 的真实签名必须一致，这是函数指针能赋值的前提。
- 真实调用点之一在 `crates/typst-layout/src/flow/mod.rs`，调用形如 `(engine.library.routines.realize)(...)`，使用 `RealizationKind::Fragment`。

---

## 7. 下一步学习建议

本讲只建立了「定位」与「入口」的认知。建议接下来按手册顺序学习：

- **下一篇（u1-l2）**：逐行精读 `pub fn realize()` 的函数体初始化，并讲清 `RealizationKind` 的五个变体（`Bundle` / `Document` / `Fragment` / `Par` / `Math`）、`Pair`、`Arenas` 等核心数据类型的来源与含义。
- **再下一篇（u1-l3）**：进入 `visit()` 调度流水线，看它按固定顺序依次尝试 TagElem 直推 → kind 规则 → show 规则 → 序列/样式递归 → 分组 → 过滤 → 入 sink。这是理解整个 crate 的「骨架」。
- 想先获得全局直觉的读者，可以先通读 `lib.rs` 第 241–294 行的 `visit` 函数注释，建立「主循环」的心智模型，再回头逐篇深入。

继续阅读的关键源码：`crates/typst-realize/src/lib.rs`（贯穿全手册）、`crates/typst-library/src/routines.rs`（类型定义）。
