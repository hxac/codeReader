# 目录结构与编译入口：typst::compile::<Bundle> 如何到达 bundle()

## 1. 本讲目标

上一讲（u1-l1）我们建立了对 `typst-bundle` 的整体直觉：它是 Typst 的「bundle 多文件输出目标」，一次编译能产出多个文档和资产。本讲我们**打开引擎盖**，回答三个具体问题：

1. `typst-bundle` 这个 crate 到底由哪几个源码文件组成？每个文件负责什么？
2. 为什么 `Bundle` 这个类型能和 `Target::Bundle` 这个目标「一一对应」？`Output` trait 在其中扮演什么角色？
3. 当我们写下 `typst::compile::<Bundle>(world)` 时，这一行代码是如何一步步走到真正的 `bundle_impl` 的？

学完本讲，你应该能在源码里画出从泛型入口到 bundle 实现的完整调用链，并解释为什么核心实现函数 `bundle_impl` 必须加上 `#[comemo::memoize]`。

## 2. 前置知识

本讲会用到上一讲（u1-l1）已经建立的几个术语，这里只做一句话回顾，不再重新推导：

- **`Target` 枚举**：Typst 的三种编译目标 `Paged` / `Html` / `Bundle`。
- **`Output` trait**：「编译产物类型」的抽象，和 `Target` 是一一对应的。
- **`compile::<T>()` 泛型入口**：通过类型参数 `T` 选择要产出哪种产物。
- **`bundle()` / `bundle_impl()`**：bundle 产物的真正构造函数。

此外，本讲假设你了解一点 Rust 基础概念，初学者可以这样理解：

- **trait（特征）**：Rust 里的「接口」。定义一组方法签名，由具体类型去实现。`Output` 就是一个 trait。
- **泛型函数 `fn compile<T>(...)`**：函数写一次，能用于多种类型 `T`。`T: Output` 表示「`T` 必须实现了 `Output` 这个接口」。
- **记忆化（memoization）**：把「输入 → 结果」缓存起来，下次相同输入直接返回缓存，避免重复计算。Typst 用 `comemo` 库实现，标注 `#[comemo::memoize]` 即可。
- **`Tracked<T>`**：Typst/comemo 里的「可追踪包装类型」。它让系统能记录「这次计算依赖了哪些数据」，从而在数据没变时复用缓存。本讲你不必深究它的实现，只要知道它出现在函数参数里，是为了配合记忆化即可。

> 这一小节的术语会贯穿后续所有讲义，建议先记住中文含义，遇到源码再回看。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用它说明什么 |
| --- | --- | --- |
| `crates/typst-bundle/src/lib.rs` | crate 根模块：定义数据模型、实现 `Output`、提供 `bundle`/`bundle_impl` | 文件职责划分、`Output` 实现、调用链终点 |
| `crates/typst-bundle/Cargo.toml` | 依赖清单 | 说明 bundle 依赖哪些兄弟 crate |
| `crates/typst-bundle/src/export.rs` | 把 `Bundle` 序列化成字节文件系统 | 四文件分工之一 |
| `crates/typst-bundle/src/introspect.rs` | 跨文档统一内省器 | 四文件分工之一 |
| `crates/typst-bundle/src/link.rs` | 跨文档链接锚点 | 四文件分工之一 |
| `crates/typst-library/src/foundations/target.rs` | 定义 `Output` trait 与 `Target` 枚举 | Output ↔ Target 一一对应 |
| `crates/typst/src/lib.rs` | 定义 `compile::<T>()` 泛型入口 | 调用链起点与分发 |

## 4. 核心概念与源码讲解

### 4.1 四个源码文件的职责划分

#### 4.1.1 概念说明

`typst-bundle` 是一个**很小的 crate**：核心源码只有 4 个 `.rs` 文件，加起来约 700 行。它本身**不做底层排版，也不做 PDF/SVG/HTML 的字节编码**，而是充当一个「编排层（orchestrator）」：把兄弟 crate 的能力组合起来，拼出「多文件输出」。

把职责拆到不同文件，是为了**关注点分离**：

- 产物怎么构造（编译主流程 + 数据模型）
- 产物怎么变成字节（导出）
- 多文档之间怎么互相可见（内省）
- 跨文档链接怎么精确跳转（锚点）

这四件事彼此独立，因此各自占一个文件。

#### 4.1.2 核心流程

crate 根 `lib.rs` 用下面的方式声明这四个模块：

```rust
#[path = "export.rs"]
mod export_;
mod introspect;
mod link;
```

注意一个小细节：`export.rs` 这个文件对应的模块名是 `export_`（带下划线），而不是 `export`。这是因为 `lib.rs` 里同时存在一个叫 `export` 的**函数**（从 `export.rs` 里 re-export 出来），如果模块也叫 `export` 就会重名。所以用 `#[path = "export.rs"] mod export_;` 让「模块名」和「文件名」解耦，规避命名冲突。

四个文件的分工如下表：

| 文件 | 关键公开项 | 一句话职责 |
| --- | --- | --- |
| `lib.rs` | `Bundle`、`BundleFile`、`bundle()`、`bundle_impl()`、`impl Output for Bundle` | 数据模型 + 编译主入口 |
| `export.rs` | `export()`、`VirtualFs`、`BundleOptions` | 把 `Bundle` 转成「路径 → 字节」映射 |
| `introspect.rs` | `BundleIntrospector` | 让多个文档共享一个内省循环 |
| `link.rs` | `create_link_anchors()` | 为被链接的目标生成锚点 |

> `lib.rs` 顶部的 `//! Multi-file output for Typst.` 就是整个 crate 的定位语。

#### 4.1.3 源码精读

先看 crate 根的模块声明与 re-export（这部分决定了「外面能看到什么」）：

[crates/typst-bundle/src/lib.rs:1-10](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L1-L10) —— 顶部文档注释说明 crate 定位；第 3–6 行声明三个内部模块（`export_`/`introspect`/`link`）；第 10 行把 `export`、`BundleOptions`、`VirtualFs` 对外暴露。

`export.rs` 定义了导出的最终形态 `VirtualFs`，也就是「虚拟文件系统」：

[crates/typst-bundle/src/export.rs:19-24](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L19-L24) —— `VirtualFs` 就是一个「路径 → 字节」的有序映射；`export()` 用并行迭代把 `Bundle` 转成它。（导出细节留到第 4 单元讲，本讲只确认它的存在。）

`introspect.rs` 与 `link.rs` 分别承载跨文档内省与锚点生成（细节留到第 5 单元）：

[crates/typst-bundle/src/introspect.rs:21-23](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L21-L23) —— `BundleIntrospector` 是 bundle 的统一内省器。

[crates/typst-bundle/src/link.rs:12-23](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L12-L23) —— `create_link_anchors()` 为所有被链接到的元素生成锚点，并让文档/资产自身也可被链接。

最后看依赖清单，它印证了「bundle 是编排层」这一定位：

[crates/typst-bundle/Cargo.toml:15-31](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/Cargo.toml#L15-L31) —— 依赖了 `typst-layout`（分页排版）、`typst-html`、`typst-pdf`、`typst-svg`、`typst-render`（四种格式编码）、`typst-library`（数据模型与规则）、`rayon`（并行）、`comemo`（记忆化）等。也就是说，底层能力都来自兄弟 crate。

#### 4.1.4 代码实践

**实践目标**：亲手把四个文件的职责对上号。

**操作步骤**：

1. 打开本讲「源码地图」列出的四个 `crates/typst-bundle/src/*.rs` 文件。
2. 在每个文件里找到它**最顶部的文档注释或第一个 `pub` 项**。
3. 填写下面这张表（答案已在 4.1.2 给出，请先自己找一遍再核对）：

| 文件 | 你找到的第一个 `pub` 项 | 你猜测的职责 |
| --- | --- | --- |
| `lib.rs` | ? | ? |
| `export.rs` | ? | ? |
| `introspect.rs` | ? | ? |
| `link.rs` | ? | ? |

**需要观察的现象**：`lib.rs` 明显比另外三个文件长，且只有它出现了 `impl Output for Bundle` 和 `bundle_impl`。

**预期结果**：你会直观感受到「`lib.rs` 是大脑，其余三个是被它调用的工具模块」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `export.rs` 对应的模块名要写成 `export_` 而不是 `export`？

> **参考答案**：因为 `lib.rs` 第 10 行会把一个名为 `export` 的**函数**对外暴露（`pub use ... export`），若模块也叫 `export` 就会与函数同名冲突。用 `#[path = "export.rs"] mod export_;` 把「文件名」和「模块名」解耦，既保留了文件名直观，又避开了命名冲突。

**练习 2**：从 `Cargo.toml` 看，`typst-bundle` 自己实现了 PDF 编码吗？

> **参考答案**：没有。它依赖 `typst-pdf`（以及 `typst-svg`、`typst-render`、`typst-html`）来做事，自己只做编排。这正是「bundle 是编排层」的体现。

### 4.2 Output trait 与 Target 的一一对应

#### 4.2.1 概念说明

上一讲我们提过 `Output` 和 `Target` 是一一对应的，本讲我们把这句话**落到源码**上。

- `Target` 是一个枚举，列出「编译目标有哪几种」。
- `Output` 是一个 trait，描述「一种编译产物类型必须能做什么」。

「一一对应」意味着：每一个 `Target` 变体，都有且只有一种实现了 `Output` 的产物类型代表它。源码注释直白地写了这一点：

> A compilation output for a particular target. Has a 1-1 relationship with the variants of `Target`.

对应关系是：

| `Target` 变体 | 实现 `Output` 的类型 | 定义位置 |
| --- | --- | --- |
| `Target::Paged` | `PagedDocument` | `typst-layout` |
| `Target::Html` | `HtmlDocument` | `typst-html` |
| `Target::Bundle` | `Bundle` | `typst-bundle`（本 crate） |

#### 4.2.2 核心流程

`Output` trait 一共要求三个能力（三个方法）：

1. `target()` —— 「我对应哪个 `Target`？」（关联方法，不需要实例）
2. `create(...)` —— 「给我引擎和内容，把我构造出来。」（真正的编译入口）
3. `introspector()` —— 「把我身上的内省器交出来。」（用于跨迭代收敛）

`Bundle` 实现这个 trait 时，三个方法分别给出：

- `target()` → 直接返回 `Target::Bundle`；
- `create(...)` → 调用本文件的 `bundle(...)` 函数；
- `introspector()` → 返回 `Bundle` 自带的 `BundleIntrospector`。

这就是「类型 = 目标」的机械对应：只要你选了 `Bundle` 这个类型，系统就知道目标是 bundle。

#### 4.2.3 源码精读

先看 `Output` trait 的定义本身：

[crates/typst-library/src/foundations/target.rs:10-30](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L10-L30) —— 注释明说「Has a 1-1 relationship with the variants of `Target`」；trait 里有 `target()`（15–17 行）、`create()`（19–26 行）、`introspector()`（28–29 行）三个方法。

再看 `Target` 枚举的三个变体：

[crates/typst-library/src/foundations/target.rs:66-76](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L66-L76) —— `Target::Paged` / `Html` / `Bundle` 三个变体，其中 `Bundle` 的注释点明它能「从一个 Typst 项目产出多个 documents 和 assets」。

最关键的一步：`Bundle` 如何实现 `Output`：

[crates/typst-bundle/src/lib.rs:56-72](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L56-L72) —— `fn target()` 直接返回 `Target::Bundle`（61–63 行）；`fn create(...)` 把工作转交给 `bundle(...)`（65–71 行）；`fn introspector()` 返回自身的内省器（57–59 行）。

把这几段连起来读，你就理解了「一一对应」不是一句口号，而是靠 `impl Output for Bundle` 这段代码硬连上的：类型选 `Bundle` ⟹ `target()` 返回 `Target::Bundle` ⟹ `create()` 走 bundle 流程。

#### 4.2.4 代码实践

**实践目标**：验证「每个 `Target` 变体都对应一个 `impl Output`」。

**操作步骤**：

1. 用编辑器/`grep` 在整个仓库搜索 `impl Output for`。
2. 你应该能找到三处：`impl Output for PagedDocument`、`impl Output for HtmlDocument`、`impl Output for Bundle`。
3. 对每一处，找到它的 `fn target()` 实现，确认分别返回 `Target::Paged` / `Html` / `Bundle`。

**需要观察的现象**：三处 `impl` 分散在不同 crate，但它们的 `target()` 返回值恰好覆盖 `Target` 的全部三个变体，没有多也没有少。

**预期结果**：你会确信「Target 有几个变体，就有几个 Output 实现」，这就是「一一对应」的实证。

#### 4.2.5 小练习与答案

**练习 1**：`Output` trait 的 `target()` 方法为什么写成 `where Self: Sized`（即「关联方法」，不需要 `&self`）？

> **参考答案**：因为系统需要在**还没有产物实例**的时候（编译刚开始）就知道「这次要往哪个目标走」。所以 `target()` 是关联方法，只依赖类型本身（`Bundle`），不需要先构造出一个 `Bundle` 实例。它被 `compile_impl` 用来做特性门控（feature gate）和设置目标样式。

**练习 2**：如果有人新加了 `Target::Epub` 变体，但不写对应的 `impl Output`，会发生什么？

> **参考答案**：`Target` 枚举能编译通过，但「产物类型」这一侧缺了一环。`compile_impl` 里 `match T::target()` 能匹配到新变体，却没有对应的产物类型 `T` 可以承载它——也就是说，类型系统层面无法用 `compile::<某类型>` 选到这个目标。这正是「一一对应」的约束：每加一个目标，必须同时加一个 `Output` 实现。

### 4.3 compile::<T: Output> 如何触发 Output::create

#### 4.3.1 概念说明

现在我们把起点和终点接起来。用户写的入口是：

```rust
let result = typst::compile::<Bundle>(world);
```

这一行靠**类型参数 `Bundle`** 来选择目标。这种「用类型来决定走哪条编译分支」的写法，叫做**类型驱动的分发（type-driven dispatch）**：`compile` 的函数体里没有 `if target == "bundle"` 这种字符串判断，而是通过泛型 `T` 配合 `T::target()` / `T::create()` 来分发。

分发链上一共有三个关键站点（也就是本讲要求你标出的三处）：

1. **`Output::create`**：泛型入口里的 `T::create(...)` 调用点。
2. **`bundle`**：`<Bundle as Output>::create` 内部转交给的公开函数。
3. **`bundle_impl`**：`bundle` 转交到的、真正干活的内部函数（带记忆化）。

#### 4.3.2 核心流程

完整调用链（自上而下）用伪代码表示：

```
typst::compile::<Bundle>(world)                 // 公共入口
  └─ compile_impl::<Bundle>(world, traced, sink)
       ├─ T::target()  =>  Target::Bundle        // ① 用类型读出目标
       ├─ match Target::Bundle => warn_or_error_for_bundle(...)  // 特性门控
       ├─ TargetElem::target.set(Target::Bundle) // 把目标写进样式链
       ├─ eval(main_source)  => content          // 求值源码得到 content
       └─ 内省循环（反复 layout 直到稳定）
            └─ T::create(engine, content, styles)   // ★ Output::create
                 = <Bundle as Output>::create
                 └─ bundle(engine, content, styles) // ★ bundle
                      └─ bundle_impl(...)           // ★ bundle_impl（#[comemo::memoize]）
```

三点说明：

- **特性门控**：因为 bundle 是实验特性，`compile_impl` 在 `match T::target()` 的 `Bundle` 分支里调用 `warn_or_error_for_bundle`——上一讲讲过，未开 `--features bundle` 时会报错。
- **内省循环**：`T::create` 不是只调用一次，而是被放进一个「反复重排直到内省稳定」的循环里（最多若干次迭代）。这也是下一个问题的背景。
- **样式链注入目标**：把 `Target::Bundle` 写进 `TargetElem::target` 样式，这样源码里的 `target()` 上下文函数才能正确返回 `"bundle"`。

#### 4.3.3 源码精读

先看公共入口 `compile`：

[crates/typst/src/lib.rs:63-82](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L63-L82) —— `pub fn compile<T>(world) where T: Output`；它新建一个 `Sink`（收集错误/警告），把真正的活儿交给 `compile_impl::<T>`，最后把结果和警告包成 `Warned<SourceResult<T>>` 返回。

再看内部分发 `compile_impl` 的关键三段。第一段：读出目标 + 特性门控：

[crates/typst/src/lib.rs:99-109](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L99-L109) —— `match T::target()`（105 行）对 `Target::Bundle` 调用 `warn_or_error_for_bundle(...)`（108 行）。注意这里完全靠类型 `T` 决定走哪条分支。

第二段：把目标注入样式链，然后求值源码：

[crates/typst/src/lib.rs:111-131](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L111-L131) —— 第 112 行 `TargetElem::target.set(T::target())` 把目标写进样式；随后 `typst_eval::eval(...)` 求值主源码得到 `content`。

第三段：内省循环里的 **`Output::create`** 调用点（本讲第一个★）：

[crates/typst/src/lib.rs:136-185](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L136-L185) —— 这是一个「重排直到内省稳定」的循环；第 156 行 `document = T::create(&mut engine, &content, styles)?` 就是 **`Output::create`** 的调用点；第 158 行 `constraint.validate(document.introspector())` 判断是否收敛，收敛则 `break`。对于 `T = Bundle`，这里的 `T::create` 就是下面这段：

[crates/typst-bundle/src/lib.rs:65-71](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L65-L71) —— `<Bundle as Output>::create` 把工作转交给 `bundle(...)`（本讲第二个★）。

接着看公开函数 `bundle`：

[crates/typst-bundle/src/lib.rs:120-136](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L120-L136) —— `bundle(...)` 标了 `#[typst_macros::time]`（只用于计时），它本身不做计算，只是把 `Engine` 里的字段拆成 `Tracked`/`TrackedMut` 形式，转发给 `bundle_impl(...)`。

最后是真正干活的 `bundle_impl`（本讲第三个★）：

[crates/typst-bundle/src/lib.rs:138-150](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L138-L150) —— 注意第 139 行的 `#[comemo::memoize]` 标注。`bundle_impl` 的参数里有 `introspector: Tracked<dyn Introspector>`，这正是它「可被记忆化、可被增量追踪」的关键依赖。

#### 4.3.4 代码实践（本讲主实践）

> 这是本讲义规格里指定的核心实践任务。

**实践目标**：亲手画出从 `typst::compile::<Bundle>(world)` 到 `bundle_impl` 的完整调用链，标出三个站点，并解释 `bundle_impl` 为什么需要 `#[comemo::memoize]`。

**操作步骤**：

1. 打开 `crates/typst/src/lib.rs`，定位第 74 行的 `pub fn compile<T>`、第 105 行的 `match T::target()`、第 156 行的 `T::create(...)`。
2. 打开 `crates/typst-bundle/src/lib.rs`，定位第 65 行的 `fn create(...)`（即 `Output::create`）、第 121 行的 `pub fn bundle(...)`、第 139 行的 `fn bundle_impl(...)`。
3. 在纸上（或注释里）画出 4.3.2 那棵调用树，把三个★标到对应函数上：
   - ★ `Output::create`：`crates/typst/src/lib.rs:156` 的 `T::create(...)` ⟶ `crates/typst-bundle/src/lib.rs:65` 的 `fn create`。
   - ★ `bundle`：`crates/typst-bundle/src/lib.rs:121` 的 `pub fn bundle`。
   - ★ `bundle_impl`：`crates/typst-bundle/src/lib.rs:139` 的 `fn bundle_impl`。
4. 阅读第 156 行所在的循环（138–185 行），注意它**每次迭代都会重新调用一次 `T::create`**。

**需要观察的现象 / 思考题**：`compile_impl` 的内省循环每次迭代都会用「上一轮的内省器」重新构造 Engine，再调用 `T::create`（对 bundle 来说就是 `bundle_impl`）。也就是说，`bundle_impl` 在一次完整编译里可能被调用多次。

**预期结果**（关于 `#[comemo::memoize]` 的解释，请用自己的话写出，下面是参考要点）：

- `bundle_impl` 的入参里有一个 `introspector: Tracked<dyn Introspector>`，它是这次计算**唯一会随迭代变化**的关键输入（world、library、content 等都不变）。
- 加上 `#[comemo::memoize]` 后，comemo 会按入参缓存结果，并**记录 bundle_impl 读取过哪些内省数据**。
- 当某次迭代发现内省器已经不再变化时，`bundle_impl` 的入参与上一轮「等价」，comemo 直接返回缓存的 `Bundle`，不必重新做「实现化 + 并行编译所有文档」这套昂贵操作。
- 更进一步：第 158 行的 `constraint.validate(document.introspector())` 之所以能判断「是否收敛」，正是依赖 comemo 记录的「bundle_impl 这轮到底读到了什么、有没有变」。换句话说，**记忆化既是性能优化，也是收敛检测的机制基础**——没有它，循环无法知道「该停了」。

> 若你无法本地运行 typst，本实践属于「源码阅读型实践」，按要求标注「待本地验证」的部分可省略；以上结论均可直接由静态阅读源码得出。

#### 4.3.5 小练习与答案

**练习 1**：`compile_impl` 里为什么是 `match T::target()` 而不是 `match some_user_option`？

> **参考答案**：因为目标是由**类型参数 `T`** 决定的，不是运行时参数。`T::target()` 从类型读出目标，于是 feature 门控、样式注入都能在编译期/泛型分发阶段确定，不需要额外的运行时配置。这正是「类型驱动分发」。

**练习 2**：公共函数 `bundle`（第 121 行）没有加 `#[comemo::memoize]`，而 `bundle_impl`（第 139 行）加了。为什么记忆化标在 `bundle_impl` 而不是 `bundle`？

> **参考答案**：因为 `bundle` 的入参里有 `&mut Engine`，它是**可变引用**，无法作为记忆化的缓存键（每次都不同，且 comemo 要求参数可追踪/可哈希）。所以作者把 `Engine` 拆成若干 `Tracked`/`TrackedMut` 标量参数，传给 `bundle_impl`，并在 `bundle_impl` 上做记忆化——这样缓存键是「可追踪的值」，comemo 才能正确判定输入是否变化。

**练习 3**：如果 `bundle_impl` 上去掉 `#[comemo::memoize]`，从功能正确性看，bundle 还能编译出结果吗？

> **参考答案**：**结果多半仍能算出**，但有两个严重后果：(1) 性能骤降——每次内省迭代都要完整重算「实现化 + 并行编译所有文档」；(2) 更致命的是收敛检测会失效——`compile_impl` 依赖 comemo 的约束追踪来判断内省是否稳定，去掉记忆化后这套追踪不复存在，循环可能无法正确判定「该停了」，要么多算很多次，要么在最坏情况下触发「迭代上限」告警。所以这个标注既是性能项，也是正确收敛的前提。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「阅读 + 推理」任务：

1. **读目录**：打开 `crates/typst-bundle/src/`，确认只有 `lib.rs`、`export.rs`、`introspect.rs`、`link.rs` 四个文件，并用一句话写下每个文件的职责（参考 4.1）。
2. **读 trait**：打开 `crates/typst-library/src/foundations/target.rs`，找到 `Output` trait 和 `Target` 枚举，确认它们的「一一对应」关系（参考 4.2）。
3. **画链条**：在一张图上画出 `compile::<Bundle>` → `compile_impl` →（feature 门控 + 样式注入 + eval）→ 内省循环 → `T::create` → `bundle` → `bundle_impl` 的完整路径，标出三个★（参考 4.3）。
4. **做推理**：基于你画的图，回答——「如果我同时在 bundle 里放了 10 个 `#document`，那么在一次内省迭代里，`bundle_impl` 会被调用几次？」（提示：`bundle_impl` 负责整个 bundle，内部再并行处理各文档；它在一次迭代里只被外层 `T::create` 调用一次。）

> 这个任务不需要运行任何命令，纯靠静态阅读即可完成，目的是让你在进入第 2 单元（编译主流程）之前，先把「骨架」牢牢掌握。

## 6. 本讲小结

- `typst-bundle` 只有 4 个源码文件：`lib.rs`（数据模型 + 编译入口 + `Output` 实现）、`export.rs`（序列化成 `VirtualFs`）、`introspect.rs`（统一内省）、`link.rs`（跨文档锚点）。
- `Output` trait 与 `Target` 枚举是**一一对应**的：`Bundle` 实现 `Output`，其 `target()` 返回 `Target::Bundle`，`create()` 转交 `bundle()`。
- `typst::compile::<Bundle>(world)` 是**类型驱动的分发**：类型参数 `Bundle` 决定了走 bundle 分支，无需运行时字符串判断。
- 调用链三个关键站点：`Output::create`（`lib.rs:156` 的 `T::create` ⟶ `typst-bundle/lib.rs:65`）→ `bundle`（`lib.rs:121`）→ `bundle_impl`（`lib.rs:139`）。
- `bundle_impl` 上的 `#[comemo::memoize]` 既是性能优化（避免每次内省迭代重算），也是收敛检测的机制基础（让 `constraint.validate` 能判断内省是否稳定）。
- bundle 自身不做排版与字节编码，是组合兄弟 crate 的「编排层」，这一点从 `Cargo.toml` 的依赖清单即可印证。

## 7. 下一步学习建议

本讲我们搭好了「骨架」：知道了文件分工和调用链是怎么连到 `bundle_impl` 的。但 `bundle_impl` 内部到底做了什么，我们还只是一个大概（实现化 → collect → 并行编译）。

下一讲 **u2-l1《从源码到 Bundle：realize 与 collect 的实现化与校验》** 将进入 `bundle_impl` 的函数体，重点讲：

- `RealizationKind::Bundle` 与 `BUNDLE_RULES` 如何把顶层 content 「实现化」成 `Tag`/`Asset`/`Document` 三类子项；
- `collect()` 如何校验顶层元素（只允许这三类，其余报错）并检测路径重复。

建议在进入下一讲前，先重新读一遍 [crates/typst-bundle/src/lib.rs:138-219](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L138-L219) 的 `bundle_impl` 全貌，带着「它在分几步做事」的问题去读，会更顺畅。
