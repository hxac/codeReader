# typst-library 项目定位与编译器生态

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `typst-library` 这个 crate 到底承担了什么职责：它既是 **Typst 的标准库**，又集中了**编译器的核心类型定义**。
- 在编译流水线中定位这个 crate，并区分「类型（types）」与「行为（behaviour）」两条不同的拆分路线。
- 对 `World` / `Library` / `Engine` 这三者之间的关系建立初步印象，为后续讲义打基础。

本讲是整套手册的**第一篇**，不要求你已经熟悉 Typst 的任何源码。我们会从 crate 顶部那段最短的文档注释讲起。

## 2. 前置知识

阅读本讲前，最好对以下概念有最基本的了解（不知道也没关系，我们会顺带解释）：

- **Rust crate**：Rust 的编译单元，类似于其他语言里的「包/模块」。一个项目可以拆成多个 crate。
- **标准库（standard library）**：一门语言/系统开箱即用、自带的函数与类型集合。在 Typst 里，`#rect()`、`#text()`、`#counter(...)` 这些「写标记就能用」的东西都属于标准库。
- **trait**：Rust 中定义「一组方法签名」的机制，类似其他语言的接口（interface）。
- **函数指针（function pointer）**：把一个函数当作值传来传去的能力。本讲会看到 Typst 用它来「跨 crate 调用」，是理解项目架构的关键。

如果你完全没用过 Typst，只要知道它是一个「把 `.typ` 文本编译成 PDF/HTML 等输出」的排版系统即可。

## 3. 本讲源码地图

本讲只聚焦两个文件，外加一个跨 crate 的对照点：

| 文件 | 作用 |
| --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | 本 crate 的入口文件。顶部文档注释点明了 crate 的定位；随后声明所有顶层模块，并定义了 `World`、`Library` 等核心类型。 |
| [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml) | 本 crate 的依赖清单。注意它**列出了哪些、又故意没有列出哪些**依赖，这是理解「行为拆分」的关键线索。 |
| `crates/typst/src/lib.rs`（同级主 crate） | 主 `typst` crate 的入口，通过一行 `pub use typst_library::*;` 把本 crate 完整 reexport。 |

此外，本讲会顺带引用 [`src/routines.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs)，用来解释「既然行为在别的 crate，本 crate 怎么调用回来」。该文件的精读属于后续讲义（u5-l4），本讲只看它的设计意图。

---

## 4. 核心概念与源码讲解

### 4.1 typst-library 的双重身份：标准库 + 核心类型定义

#### 4.1.1 概念说明

打开任何一个 Typst 文档，你写的 `#heading`、`#align`、`int`、`str`、颜色……这些东西并不是凭空出现的，它们都由某个 Rust crate 定义。这个 crate 就是 `typst-library`。

但它做的事情比一般意义上的「标准库」更多。它的顶部文档注释用三句话讲清了自己的双重身份：

1. 它是 **Typst 的标准库**（Typst's standard library）。
2. 它**同时包含了编译器的全部核心类型定义**（all of the compiler's central type definitions），因为这些类型和标准库类型是「交织在一起」的。
3. 与「类型」相对，**大部分编译「行为」被拆到了别的 crate**（`typst-eval`、`typst-realize`、`typst-layout` 等）。

为什么要把「类型」和「行为」分开？直觉上的原因是：**类型是被到处引用的「公共词汇」**，而**行为是「怎么处理这些类型」的具体算法**。`Value`（值）、`Content`（内容）、`Module`（模块）这些类型，标准库要定义、行为 crate 也要用到——所以它们必须放在一个被所有人依赖的底层 crate 里。而「如何求值一段代码」「如何把内容排版成页面」这些重算法，则可以单独成 crate、单独演进。

#### 4.1.2 核心流程

可以用下面这张「分层图」来理解 `typst-library` 在整个编译器里的位置：

```text
                ┌─────────────────────────────────────┐
   用户写 .typ →│  解析(parse)  →  求值(eval)          │
                │         →  收敛(realize) → 排版(layout)│ → PDF / HTML / ...
                └─────────────────────────────────────┘
                              ▲              ▲           ▲
                              │ 依赖          │ 依赖       │ 依赖
                   ┌──────────┴──────────────┴───────────┴─────┐
                   │      typst-library                         │
                   │  （标准库 + 核心类型：Value/Content/Module │
                   │     /World/Library/Element ……）            │
                   └────────────────────────────────────────────┘
```

- 上层是各种 **行为 crate**：`typst-eval`（求值）、`typst-realize`（收敛/规整）、`typst-layout`（排版）等，它们实现具体算法。
- 底层是 `typst-library`：它只定义「类型与标准库词汇」，被上面所有行为 crate 共同依赖。

注意箭头方向：**是行为 crate 依赖 `typst-library`，而不是反过来**。这一点我们会在 4.2 节用源码坐实。

#### 4.1.3 源码精读

先看 crate 顶部那段至关重要的文档注释：

[src/lib.rs:L1-L11](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L1-L11) —— 这段注释说明了本 crate 的定位与拆分理由，并提醒「除非你在改编译器本身，否则很少需要直接用本 crate，因为它已被 `typst` crate 完整 reexport」。

紧跟注释的是一行略显特别的声明：

[src/lib.rs:L13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L13) —— `extern crate self as typst_library;`。它的作用是让 crate **在自己的代码里也能用 `typst_library::……` 这样的绝对路径来引用自己**。这在过程宏（procedural macro）生成的代码里很重要：宏展开后的代码需要稳定的、带 crate 名的路径来引用类型，而 `extern crate self` 提供了这样一个稳定入口。

再往下是一串顶层模块声明，这就是「标准库」的骨架：

[src/lib.rs:L15-L27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L15-L27) —— 一共 **13 个顶层 `pub mod`**：`diag`、`engine`、`foundations`、`introspection`、`layout`、`loading`、`math`、`model`、`pdf`、`routines`、`symbols`、`text`、`visualize`。后续讲义会逐个深入，本讲你只需要知道：这 13 个模块拼起来，就是 Typst 标准库的全部「领土」。

关于「被 `typst` crate 完整 reexport」这一点，可以在主 crate 入口里直接验证：

`crates/typst/src/lib.rs` 第 36 行：`pub use typst_library::*;` —— 主 `typst` crate 把本 crate 的所有公开项原样重新导出。这意味着外部使用者通常直接 `use typst::……`，而不必直接依赖 `typst-library`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「reexport」这件事，并理解为什么大多数代码不需要直接依赖 `typst-library`。

**操作步骤**：

1. 打开本讲源码地图里列出的主 crate 入口 `crates/typst/src/lib.rs`（仓库根目录下），找到第 36 行。
2. 观察这一行：`pub use typst_library::*;`。

**需要观察的现象**：主 crate 用一个通配 `*` 把整个 `typst-library` 重新导出，而不是逐个罗列类型。这说明 `typst-library` 的公开 API 非常大（13 个顶层模块、数百个类型），逐项 reexport 不现实。

**预期结果**：你会确认「`typst-library` 是 `typst` 主 crate 的内容主体」这一结论。如果你在写一个嵌入 Typst 的应用，直接依赖 `typst` 即可拿到全部标准库与核心类型。

#### 4.1.5 小练习与答案

**练习 1**：文档注释说本 crate 同时包含「标准库」和「编译器核心类型定义」。请各举一个（从模块名推断即可）分别属于这两类的例子。

> **参考答案**：「标准库」类，如 `text`（字体相关元素）、`model`（标题/列表等文档元素）、`visualize`（颜色与形状）；「核心类型定义」类，如 `foundations`（`Value`/`Content`/`Module`）、`engine`（编译上下文 `Engine`）、`diag`（诊断）。注意 `foundations` 实际上两者兼具——这正是注释所说的「交织」。

**练习 2**：`extern crate self as typst_library;` 这行为什么对本 crate 特别有用？

> **参考答案**：它让 crate 内部能以 `typst_library::……` 的绝对路径引用自己。Typst 大量使用过程宏（如 `#[elem]`、`#[func]`）生成代码，这些生成代码需要一条与 crate 名绑定的稳定路径来引用类型；`extern crate self` 正好提供了这样的入口，避免宏生成代码里路径不稳定或解析失败。

---

### 4.2 类型与行为的拆分：依赖清单与 Routines

#### 4.2.1 概念说明

4.1 节我们说「是行为 crate 依赖 `typst-library`，而不是反过来」。这带来一个自然的疑问：**既然求值、收敛、排版这些「行为」都在别的 crate，那 `typst-library` 内部的代码需要调用这些行为时怎么办？** 比如，标准库里有个 `eval` 函数，它显然要触发「求值」这个行为，而求值实现在 `typst-eval` 里。

如果 `typst-library` 直接 `use typst_eval;`，就会形成一个**循环依赖**（`typst-eval` 依赖 `typst-library`，`typst-library` 又依赖 `typst-eval`），Rust 不允许。

Typst 的解决办法非常优雅：**不在编译期依赖，而在运行期注入**。`typst-library` 定义一个「函数指针表」`Routines`，里面留好「求值」「收敛」「排版」等空位（函数指针）。真正要用到这些行为时，由上层（主 `typst` crate）把 `typst-eval`/`typst-realize`/`typst-layout` 里实现好的函数**填进这个表**，再传给 `typst-library` 使用。

源码里对这套机制有一句直白的注释：「This is essentially dynamic linking and done to allow for crate splitting.」（这本质上就是动态链接，用来实现 crate 拆分）。

#### 4.2.2 核心流程

- **依赖方向（编译期）**：`typst-eval` 等行为 crate → 依赖 → `typst-library`（单向，无环）。
- **行为调用（运行期）**：`typst-library` 持有一个 `Routines` 函数指针表；表中每个指针指向某个行为 crate 提供的真实实现。调用形如 `(library.routines.eval_string)(...)`，即「从表里取出指针，再调用」。

```text
 编译期依赖（单向）            运行期回调（函数指针）
 ┌──────────┐  depends on   ┌──────────────┐
 │ typst-eval│ ────────────→│ typst-library │
 └──────────┘               │  Routines 表  │
 ┌──────────┐  depends on   │  ┌─────────┐ │
 │typst-    │ ────────────→│  │realize  │ │   运行时：把指针塞进表里，
 │realize   │               │  └─────────┘ │   typst-library 就能「回调」
 └──────────┘               └──────────────┘   这些行为 crate。
```

#### 4.2.3 源码精读

先看 `Cargo.toml` 的依赖清单，**注意它故意没有列出哪些 crate**：

[Cargo.toml:L15-L20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml#L15-L20) —— `typst-` 开头的依赖只有：`typst-assets`、`typst-macros`、`typst-syntax`、`typst-timing`、`typst-utils`。**这里面没有 `typst-eval`、没有 `typst-realize`、没有 `typst-layout`**。这就是「不依赖行为 crate」的铁证。

为了坐实「行为 crate 反过来依赖 `typst-library`」，可以看一眼 `typst-eval` 的清单：在 `crates/typst-eval/Cargo.toml` 第 16 行写着 `typst-library = { workspace = true }`。方向完全一致，无环。

那 `typst-library` 怎么「回调」这些行为？看 `Routines` 的定义：

[src/routines.rs:L20-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L20-L48) —— 一个 `routines!` 宏生成 `Routines` 结构体，注释明说它是「a table of function pointers … essentially dynamic linking … to allow for crate splitting」（函数指针表……本质是动态链接……用于实现 crate 拆分）。

[src/routines.rs:L50-L114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L50-L114) —— 这里列出了表里的全部「空位」，每个空位都是一个函数指针。例如：

- `rules`：生成内置 show 规则表；
- `eval_string`：把字符串当代码求值（对应 `typst-eval`）；
- `eval_closure`：调用闭包（对应 `typst-eval`）；
- `realize`：把内容收敛成扁平的、带样式的元素列表（对应 `typst-realize`）；
- `layout_frame`：把内容排版成单帧（对应 `typst-layout`）；
- `html_module`：构造 HTML 输出模块（对应 `typst-html`）。

这些指针的**类型签名**定义在本 crate（`typst-library`），但**具体实现**由对应的行为 crate 提供、在运行时填入。这样 `typst-library` 就能「调用」它编译期并不依赖的代码。

> 小提示：`Routines` 还实现了一个故意「什么都不做」的 `Hash`（见宏里 `fn hash<H: Hasher>(&self, _: &mut H) {}`）。这是因为 `Library` 需要派生 `Hash`，而函数指针无法被有意义地哈希——所以这里把哈希「短路」掉。这是个有意思的工程细节，进阶讲义（u12-l2）会再谈。

#### 4.2.4 代码实践

**实践目标**：亲手验证「依赖方向」与「函数指针表」这两件事。

**操作步骤**：

1. 打开本 crate 的 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml)，在 `[dependencies]` 段里搜索 `typst-eval`、`typst-realize`、`typst-layout`。确认它们**不存在**。
2. 再打开仓库根 `crates/typst-eval/Cargo.toml`，确认其中有一行 `typst-library = { workspace = true }`。
3. 打开 [`src/routines.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs)，浏览 `routines! { ... }` 这一段，数一数表里有几个函数指针空位。

**需要观察的现象**：

- 本 crate 不依赖任何行为 crate；
- 行为 crate `typst-eval` 却依赖本 crate；
- `Routines` 表里每个空位都对应一项「行为」，其签名在本 crate 定义。

**预期结果**：你会得到一张「行为 → 对应 routine → 实现它的 crate」的对照（如 `eval_string`/`eval_closure` → `typst-eval`，`realize` → `typst-realize`，`layout_frame` → `typst-layout`，`html_module` → `typst-html`）。这就是 crate 拆分在源码层面的全貌。

#### 4.2.5 小练习与答案

**练习 1**：假如 `typst-library` 直接 `use typst_eval;`，会发生什么？

> **参考答案**：会形成循环依赖。因为 `typst-eval` 已经依赖 `typst-library`（要用 `Value`/`Content` 等类型），若 `typst-library` 再反过来依赖 `typst-eval`，Rust 编译器会拒绝这个环。`Routines` 函数指针表正是为了打破这个环而存在的。

**练习 2**：`Routines` 是「函数指针表」，那它和普通 trait object（如 `dyn SomeTrait`）有什么本质区别？为什么这里选函数指针？

> **参考答案**：函数指针表是「按名字预先留好的固定空位」，每个空位的签名在编译期就定死，填入时只是把具体函数地址塞进去；它不需要 vtable、不需要对象、调用开销几乎为零。Typst 选它的原因是：要回调的「行为」集合是固定且已知的（求值、收敛、排版……），用一张显式的表比定义一组 trait 更直接、也更利于被 `Library` 这样的结构体直接持有和派生 `Hash`/`Clone`。

---

### 4.3 编译环境三支柱初识：World / Library / Engine

#### 4.3.1 概念说明

4.1、4.2 讲的是「crate 怎么拆」，这一节我们把视角拉回运行时，认识三个反复出现的关键概念。后续讲义（u1-l3、u5 系列）会深入它们的字段与机制，**本节只建立初步印象**。

- **`World`**：编译发生的「外部环境」。它回答的问题是「源文件在哪？字体在哪？今天几号？」——也就是**一切 Typst 自己不掌握、需要外部提供的信息**。
- **`Library`**：标准库本身（作为一份配置数据）。它回答的问题是「这次编译用哪些函数、哪些默认样式、开启了哪些实验性特性？」
- **`Engine`**：一次编译过程中的「活跃上下文」。它回答的问题是「现在求值到第几层了？有没有报错？哪些位置被内省（introspect）到了？」

一句话概括它们的关系：**`World` 提供环境，其中包含一份 `Library`；`Engine` 把 `World`、`Library` 以及本次编译的动态状态聚合在一起，驱动整个编译向前走。**

#### 4.3.2 核心流程

```text
   外部宿主（CLI / 语言服务器 / 嵌入式应用）
        │  实现 World trait，提供 source/file/font/book/...
        ▼
     ┌────────┐  library()  ┌──────────┐
     │ World  │ ──────────→ │ Library  │  （标准库：routines/global/math/...）
     └────────┘             └──────────┘
        │  连同 Library、Introspector、Route、Sink 等
        ▼
     ┌────────┐
     │ Engine │  （活跃编译上下文，真正驱动 eval/realize/layout）
     └────────┘
```

- `World` 是宿主实现的 trait，缓存职责也交给宿主；
- `Library` 是 `World.library()` 返回的「静态配置」；
- `Engine`（定义在 `engine` 模块）把上述资源与本次编译的动态数据（路由、错误收集、被跟踪的 span 等）组合起来。

#### 4.3.3 源码精读

先看 `World` trait 及其上方那段关于「缓存职责」的注释：

[src/lib.rs:L44-L98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L44-L98) —— `World` 用 `#[comemo::track]` 标注（这是增量编译的记忆化机制，进阶讲义细讲），它要求实现 7 个方法：`library`、`book`、`main`、`source`、`file`、`font`、`today`。注释特别强调：**编译器自己不做缓存，缓存交给 `World` 的实现者**——因为只有宿主知道「什么时候某个资源会变」（例如字体通常不变、可跨次编译缓存；源文件每次都可能变、需要每次清理）。

再看 `Library` 结构体：

[src/lib.rs:L164-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L164-L183) —— `Library` 把标准库拆成 7 个字段：

- `routines`：上一节讲的函数指针表；
- `global`：到处可用的全局定义（模块）；
- `math`：仅数学模式可用的定义（模块）；
- `styles`：默认样式（页面大小、字体选择等）；
- `rules`：内置 show 规则；
- `std`：把整份标准库作为一个值暴露给脚本里的 `std` 模块；
- `features`：本次启用的实验性特性。

注意 `World.library()` 返回的就是 `&LazyHash<Library>`——也就是说，`World` 持有的 `Library` 是被哈希缓存包装过的，这对增量编译很重要。

最后看 `Engine`。它定义在 `engine` 模块（在 [src/lib.rs:L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L16) 声明为 `pub mod engine;`），我们能在 `routines.rs` 的导入里看到它聚合了哪些伙伴类型：

[src/routines.rs:L9](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L9) —— `use crate::engine::{Engine, Route, Sink, Traced};`。这说明 `Engine` 与 `Route`（调用路由，防循环/防过深嵌套）、`Sink`（收集警告与延迟错误）、`Traced`（跟踪被检视的 span）是一组。本节你只需记住：**`Engine` 是把这些动态状态聚合起来的活跃上下文**，它的细节留给 u5-l2。

#### 4.3.4 代码实践

**实践目标**：在源码里确认三者「谁拥有谁、谁聚合谁」。

**操作步骤**：

1. 在 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) 中找到 `trait World`，确认它的 `library(&self)` 方法返回 `&LazyHash<Library>`。
2. 找到 `struct Library`，数一数它有几个字段，分别对应「函数指针表 / 全局模块 / 数学模块 / 默认样式 / show 规则 / std 模块 / 特性」中的哪一项。
3. 在 [`src/routines.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs) 顶部导入里，确认 `Engine` 与 `Route`/`Sink`/`Traced` 同属 `engine` 模块。

**需要观察的现象**：`World` 不直接持有 `Engine`；`Engine` 是在编译开始时、由调用方用 `World`、`Library` 等数据现场构造的活跃上下文。

**预期结果**：你能用自己的话说出——`World` 是「环境」、`Library` 是「标准库配置」（由 `World` 提供）、`Engine` 是「把环境与配置加上动态状态后、真正干活的上下文」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `World` 要求实现者自己做缓存（如字体缓存），而不是由 Typst 编译器统一缓存？

> **参考答案**：因为只有宿主（`World` 的实现者）知道「某个资源会不会变」。字体通常长期不变，可以跨多次编译缓存（对 `typst watch` 这类长驻应用尤其有用）；源文件随时可能被用户改动，需要在每次编译后清理。把缓存职责交给更了解变化时机的宿主，能让增量编译更高效、更正确。

**练习 2**：`Library` 里的 `global` 和 `math` 两个字段都是 `Module` 类型，为什么要分成两份？

> **参考答案**：因为 Typst 有「数学模式」这一特殊上下文。`global` 是在任何地方都可用的定义；`math` 是只有进入数学公式（如 `$ ... $`）时才注入的额外定义（如各种数学符号、`frac`、`matrix` 等）。把两者分开存放，编译器就能根据当前是否处于数学模式，把对应的模块挂载进作用域。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务。

**实践目标**：用本讲学到的视角，独立「解说」`typst-library` 这个 crate 的定位与架构。

**操作步骤**：

1. **读文档**：打开 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) 顶部第 1–11 行的模块文档，用自己的话写一段（2–3 句）解释「**为什么大部分编译行为被拆到别的 crate**」。要求同时提到「类型」与「行为」的区别，以及「循环依赖」这个动机。

2. **生成并浏览文档**：在仓库根目录运行：

   ```bash
   cargo doc -p typst-library --no-deps
   ```

   然后用浏览器打开 `target/doc/typst_library/index.html`（或在终端查看生成的目录树）。

3. **列清单**：在根模块页面里，列出 `typst-library` 的全部**顶层 `pub mod`**（应为 13 个），并对照 [src/lib.rs:L15-L27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L15-L27) 核对一致。

4. **画一张依赖方向图**：结合 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/Cargo.toml) 与 `Routines`（[src/routines.rs:L20-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L20-L48)），手绘「编译期依赖方向」与「运行期函数指针回调」两张小图。

**需要观察的现象 / 预期结果**：

- `cargo doc` 能成功生成（若本地环境缺依赖或网络受限而失败，则改为直接阅读 `src/lib.rs` 的 `pub mod` 列表完成任务，并在记录里标注「待本地验证 cargo doc 是否可跑通」）。
- 你应该得到 13 个顶层模块的清单：`diag`、`engine`、`foundations`、`introspection`、`layout`、`loading`、`math`、`model`、`pdf`、`routines`、`symbols`、`text`、`visualize`。
- 你的依赖方向图应当清楚显示：行为 crate →（编译期依赖）→ `typst-library`；`typst-library` →（运行期经 `Routines` 回调）→ 行为 crate。

> 说明：本实践以「源码阅读 + 文档生成」为主，没有要求你修改任何代码；请勿改动源码。

## 6. 本讲小结

- `typst-library` 身兼两职：它是 **Typst 的标准库**，同时集中了**编译器的核心类型定义**（`Value`/`Content`/`Module` 等）。
- 它通过主 `typst` crate 的 `pub use typst_library::*;` 被**完整 reexport**，所以一般使用者直接依赖 `typst` 即可。
- 「类型」与「行为」被刻意分开：类型是公共词汇，留在本 crate；求值/收敛/排版等行为拆到 `typst-eval`/`typst-realize`/`typst-layout` 等 crate。
- **依赖方向是单向的**：行为 crate 依赖 `typst-library`，本 crate 的 `Cargo.toml` 里看不到任何行为 crate。
- 为了在不形成循环依赖的前提下「回调」行为，本 crate 用一张 `Routines` **函数指针表**在运行期注入实现——源码注释称之为「本质上的动态链接」。
- 运行时三支柱：`World`（外部环境）持有 `Library`（标准库配置），`Engine`（活跃编译上下文）把它们与路由/错误收集等动态状态聚合起来驱动编译。

## 7. 下一步学习建议

下一篇讲义 **u1-l2「目录结构与模块导览」** 将带你走进本讲列出的那 13 个顶层模块，把目录结构对应到 `lib.rs` 里 `Category` 枚举与 `global()` 装配函数的各处 `define` 调用。

如果你想在动手前再巩固本讲的直觉，建议先通读一遍：

- [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) 的顶部文档与 `World`/`Library` 定义；
- [`src/routines.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs) 的 `routines!` 宏与函数指针清单。

这两段源码是理解整个 `typst-library` 架构的「地图钥匙」。
