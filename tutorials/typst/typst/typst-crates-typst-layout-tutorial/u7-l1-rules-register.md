# show 规则注册 register 与 paged target

## 1. 本讲目标

本讲是「专家层：规则注册与集成」单元的第一篇，聚焦 `typst-layout` 与 `typst-library` 之间那条最关键的**粘合线**——`register` 函数。

读完本讲你应当能够：

1. 说清楚 `Target::Paged` 这个「编译目标」是什么、它如何决定哪些 show 规则在排版时生效。
2. 看懂 `ShowFn<T>` 这套**原生 show 规则的统一函数签名**，并解释三个参数 `elem / engine / styles` 各自的作用。
3. 理解 `NativeRuleMap` 这张「规则注册表」如何以 `(元素, 目标)` 为键存储规则，以及 `register` 方法为什么会在重复注册时 panic。
4. 看懂 `register` 函数按 **Model / Text / Layout / Visualize / Math / PDF** 六大类注册的 59 条规则，并能区分「纯样式变更型」「挂 layouter 型」两种典型规则写法。
5. 独立追踪 `#strong` / `#emph` 这类语义元素是如何被 `STRONG_RULE` / `EMPH_RULE` 翻译成 `TextElem` 的样式变更的。

本讲不展开单条复杂规则（如 `HEADING_RULE`、`LAYOUT_RULE`）的实现细节，那是下一讲 u7-l2 的内容；本讲只回答「规则是怎么挂上去的、挂在哪、长什么样」。

## 2. 前置知识

本讲假设你已经读过 u1-l3（公共 API 与入口函数），知道：

- **show rule（展示规则）**：Typst 里把一个元素「翻译」成另一种内容（Content）的机制。用户写的 `show heading: it => [...]` 是自定义 show rule；而 typst-layout 里这些 `*_RULE` 是**内置的原生 show rule**，由 Rust 代码实现，每个内置元素都自带一条。
- **realize（现实化）**：排版前的「翻译层」，把任意 Content 展开成扁平的 `Vec<Pair>`（u1-l4）。原生 show rule 就是在 realize 阶段被逐元素调用的——realize 遇到一个 `StrongElem`，就去规则表里查它对应 `Target::Paged` 的那条规则，调用之，把返回的新 Content 继续往下展开。
- **Engine / StyleChain**：`Engine` 是编译上下文（world/library/introspector 等，见 u2-l1），`StyleChain` 是「当前元素所处的样式链」，规则的第三参数读它。
- **`pub use` 门面**：`lib.rs` 只导出 7 类符号，`register` 是其中唯一的「粘合层」导出（u1-l3）。

如果你对这些概念还陌生，建议先回看 u1-l3 与 u1-l4 再继续。

## 3. 本讲源码地图

本讲横跨三个 crate，主角是 `typst-layout` 里的一个文件，但它注册的规则类型与目标定义在 `typst-library`，而「调用 register」的动作发生在顶层 `typst` crate。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-layout/src/rules.rs` | **主角**。定义 `register` 函数与全部 59 条 `*_RULE` 常量。 |
| `crates/typst-layout/src/lib.rs` | 通过 `pub use self::rules::register;` 把 `register` 暴露出去（第 24 行）。 |
| `crates/typst/src/lib.rs` | 顶层 crate 的 `ROUTINES` 静态量里，在 `rules` 闭包中调用 `typst_layout::register(&mut rules)`。 |
| `crates/typst-library/src/foundations/styles.rs` | 定义 `NativeRuleMap`（规则注册表）、`ShowFn<T>`（规则签名）、`NativeRuleMap::register` 方法。 |
| `crates/typst-library/src/foundations/target.rs` | 定义 `Target` 枚举（含 `Paged` 变体）。 |
| `crates/typst-library/src/layout/container.rs` | 定义 `BlockElem::single_layouter` / `multi_layouter`、`InlineElem::layouter`——规则挂载 layouter 的「卡槽」。 |

> 注意：本讲的永久链接既指向 `typst-layout` crate 内的文件，也指向 `typst-library` / `typst` crate 的文件。前者用本仓库 layout base，后者用各自 crate 的完整 GitHub 路径，但都锁定在同一个 commit `146a58329`。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：`Target` → `ShowFn` → `NativeRuleMap` → `register`。顺序是「先看规则挂在哪张表、表用什么键、每条规则长什么样，最后看谁在什么时候把规则塞进表里」。

### 4.1 Target：编译目标与 Paged

#### 4.1.1 概念说明

`Target`（编译目标）回答的问题是：「这次编译，最终要产出的产物形态是什么？」

同一个 Typst 源码可以编译成不同的产物：分页的 PDF（paged）、网页 HTML、或一次产出多文档的 bundle。不同产物对同一个元素的「展示方式」可能完全不同——比如一个 `table` 元素，在 PDF 里要排成带边框的网格（`typst-layout` 负责），在 HTML 里却可能直接渲染成 `<table>` 标签（`typst-html` 负责）。

因此 Typst 给每条原生 show rule 贴上一个 `Target` 标签，realize 时只取「当前编译目标」对应的那条。`Target::Paged` 就是「分页、完整排版后的内容」这个目标，也就是 `typst-layout` 全力服务的那个目标。

#### 4.1.2 核心流程

`Target` 在编译主链路里的流动可以这样概括：

1. 顶层决定本次编译目标是 `Paged`（默认）。
2. realize 阶段展开某个元素时，拿「元素的类型 + 当前 Target」组成一个键。
3. 到 `NativeRuleMap` 里查这个键，找到对应的 `ShowFn`。
4. 调用该 `ShowFn`，得到新的 Content，继续展开。

关键点：`Target` 是 show rule 的**第二维索引**。同一个元素，在 `Paged` / `Html` / `Bundle` 三个目标下可以有三条不同的规则（实际多数元素只在部分目标注册）。

#### 4.1.3 源码精读

`Target` 是个只有三个变体的简单枚举，定义在 `typst-library`：

[crates/typst-library/src/foundations/target.rs:65-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L65-L76) — 定义 `Target` 枚举，`Paged` 是默认变体（`#[default]`），文档注释说明它是「分页、完整排版后的内容」。

注意 `Paged` 上方的 `#[default]`：当某处需要一个 `Target` 却没显式给出时，默认就是 `Paged`。这也解释了为什么 PDF 排版是 Typst 的「主战场」——它是默认目标。

而 `register` 函数开头有一句 `use Target::Paged;`，把整个文件里所有注册都绑定到这一个目标上：

[crates/typst-layout/src/rules.rs:38-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L38-L40) — `register` 的文档注释与函数头，`use Target::Paged;` 把本文件全部规则锁定到 `Paged` 目标。

也就是说：**typst-layout 注册的全部 59 条规则，都是且只是「Paged 目标的规则」**。HTML 目标的规则在 `typst-html` 的 `register` 里另行注册（见 4.3.3 的调用点）。

#### 4.1.4 代码实践

**实践目标**：确认「同一个元素在不同 Target 下可以挂不同规则」。

**操作步骤**：

1. 在 `crates/typst-library/src/foundations/target.rs` 阅读 `Target` 的三个变体及文档注释。
2. 注意 `NativeRuleMap::new()`（见 4.3.3）会对**所有三个** target 各注册一批「对所有目标都生效」的通用规则（`CONTEXT_RULE` 等），而 typst-layout 只往 `Paged` 注册。
3. 思考：若要新增一个编译目标（比如 EPUB），需要改动哪些地方？

**需要观察的现象 / 预期结果**：

- `Target` 是 `Copy + Eq + Hash` 的，这意味着它可以安全地作为 `NativeRuleMap` 内部 `IndexMap` 键的一部分（`Hash` 是前提）。
- 你应能口头复述：`Paged` 是默认目标；typst-layout 的所有规则都挂在它名下。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Target` 必须实现 `Hash`？

**参考答案**：因为 `NativeRuleMap` 用 `(Element, Target)` 二元组作 `IndexMap` 的键（见 4.3.3），`IndexMap` 内部用哈希表，键类型必须满足 `Hash`。

**练习 2**：`Paged` 变体上的 `#[default]` 有什么实际意义？

**参考答案**：它让 `Target` 实现了 `Default`，且默认值是 `Paged`。这样在「未显式指定目标」的代码路径上会自动落到分页排版，符合 Typst 以 PDF 为主输出的定位。

---

### 4.2 ShowFn：原生 show 规则的统一签名

#### 4.2.1 概念说明

`ShowFn<T>` 是「一条原生 show rule 的类型」。它是一个**类型别名**，泛型参数 `T` 是这条规则所服务的元素类型（如 `StrongElem`、`TableElem`）。

它的本质是一个**裸函数指针** `fn(...)`，不是闭包。这一点非常关键：每条 `*_RULE` 都是一个 `const` 常量，要在程序启动时静态注册到一个全局表里，所以它不能捕获任何环境——必须是「不持有状态」的纯函数指针。

签名有三个参数：

- `elem: &Packed<T>`——被展示的元素本身（已打包、带 location）。规则从这里读元素的字段。
- `engine: &mut Engine`——编译引擎。需要进一步 realize / measure 子内容时用（如 `HEADING_RULE` 要排版编号来测宽）。
- `styles: StyleChain`——当前样式链。从这里读「与当前上下文相关的设置」（如对齐、文字方向）。

返回 `SourceResult<Content>`——可能失败（返回编译错误），成功则返回这段元素「展示后」的新内容。

#### 4.2.2 核心流程

一条原生 show rule 的工作流程是纯粹的「输入元素 → 输出内容」：

```
Packed<T>  +  Engine  +  StyleChain
            │ (ShowFn 调用)
            ▼
       SourceResult<Content>
```

而规则本身一般做两件事之一：

- **纯样式变更型**：克隆元素 body，用 `.set(某字段, 某值)` 给它附着一个样式变更，返回。典型如 `STRONG_RULE` / `EMPH_RULE`。
- **挂 layouter 型**：把元素交给 `BlockElem::multi_layouter(elem, 某排版函数)`，返回一个带自定义排版器的 block。典型如 `TABLE_RULE` / `GRID_RULE`（详见 4.4）。

#### 4.2.3 源码精读

`ShowFn<T>` 的定义极简，就在 `NativeRuleMap` 上方：

[crates/typst-library/src/foundations/styles.rs:991-996](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L991-L996) — `ShowFn<T>` 类型别名：`fn(&Packed<T>, &mut Engine, StyleChain) -> SourceResult<Content>`。

本讲的实践任务主角 `STRONG_RULE` 与 `EMPH_RULE`，是「纯样式变更型」最干净的两个例子：

[crates/typst-layout/src/rules.rs:114-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L114-L122) — `STRONG_RULE` 与 `EMPH_RULE`，两条最短的规则。

逐行拆解：

```rust
const STRONG_RULE: ShowFn<StrongElem> = |elem, _, styles| {
    Ok(elem.body.clone()
        .set(TextElem::delta, WeightDelta(elem.delta.get(styles))))
};

const EMPH_RULE: ShowFn<EmphElem> =
    |elem, _, _| Ok(elem.body.clone().set(TextElem::emph, ItalicToggle(true)));
```

要点：

- 闭包 `|elem, _, styles|` / `|elem, _, _|` 不捕获任何变量，因此能自动协变成裸 `fn` 指针，赋给 `ShowFn`。第二个参数是 `engine`，这里用不到所以写成 `_`。
- `elem.body.clone()`——取出 `#strong[...]` / `#emph[...]` 里的正文内容（克隆一份）。
- `.set(TextElem::delta, WeightDelta(...))`——`Content::set` 把一个样式变更**附着**到这段内容上。`TextElem::delta` 是「字重增量」字段：strong 不是直接设绝对字重，而是给当前字重叠加一个增量（来自 `elem.delta`，默认让字重变粗）。
- `EMPH_RULE` 同理，把 `TextElem::emph` 设成 `ItalicToggle(true)`，下游会把 emph 解释成斜体。
- 两者都**不碰几何、不画框**——它们只是「改样式」。真正的「字重变粗 / 文字倾斜」是由后续 inline 排版（整形）读这些样式字段实现的。这就是「show rule 负责语义→样式翻译，layout 负责样式→几何」的分工。

> 注意一个对照：`STRONG_RULE` 用了第三参数 `styles`（读 `elem.delta.get(styles)`），`EMPH_RULE` 完全没用 `styles`（写死 `ItalicToggle(true)`）。这反映出 emph 是个「单纯开关」，而 strong 的强度可以通过 `delta` 字段配置。

#### 4.2.4 代码实践

**实践目标**：亲手把 `STRONG_RULE` / `EMPH_RULE` 的「语义元素 → 样式变更」翻译过程讲清楚。

**操作步骤**：

1. 打开 [crates/typst-layout/src/rules.rs:114-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L114-L122)。
2. 对照下表，逐项填空（答案已给出，供核对）：

| 维度 | `STRONG_RULE` | `EMPH_RULE` |
| --- | --- | --- |
| 服务元素 | `StrongElem`（`*粗体*`） | `EmphElem`（`_强调_`） |
| 是否用到 `engine`（第二参数） | 否（`_`） | 否（`_`） |
| 是否用到 `styles`（第三参数） | 是（读 `elem.delta`） | 否 |
| 附着的样式字段 | `TextElem::delta` | `TextElem::emph` |
| 附着的值 | `WeightDelta(elem.delta.get(styles))` | `ItalicToggle(true)` |
| 产物的几何属性是否改变 | 否（仅样式） | 否（仅样式） |

3. 用一句话写下：「为什么这两条规则不返回任何 Frame？」（见下方预期结果）

**需要观察的现象 / 预期结果**：

- 预期结论：show rule 是排版**之前**的翻译步骤，它只产出新的 `Content`；真正画字、算几何是后面 realize → flow → inline 的事。所以 `STRONG_RULE` / `EMPH_RULE` 只「贴标签」（改样式），不「画画」。

**待本地验证**（可选）：在 `STRONG_RULE` 闭包开头临时插一句 `eprintln!("strong show rule fired");`，编译一个含 `#strong[x]` 的文档，观察终端是否打印该行（验证规则确实被 realize 调用）。改动只用于本地观察，**切勿提交**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `STRONG_RULE` 必须写成 `const ... = |elem, _, styles| {...};` 这种「不捕获的闭包」，而不能写成捕获了外部变量的闭包？

**参考答案**：因为 `ShowFn<T>` 是裸 `fn` 指针类型，而 `*_RULE` 是 `const` 常量、要在启动时静态注册进全局表。只有不捕获任何环境的闭包才能协变成 `fn` 指针并被存为常量；捕获环境的闭包类型不匹配 `fn`，且无法 `const` 化。

**练习 2**：如果用户在 Typst 源码里写了 `show strong: it => [【#it】]`，`STRONG_RULE` 还会执行吗？

**参考答案**：不会。用户自定义的 show rule 会**覆盖**该元素的内置 show rule（realize 时用户规则优先）。`STRONG_RULE` 是默认规则，只在用户没有为 `strong` 定义 show rule 时生效。

---

### 4.3 NativeRuleMap 与 register 方法

#### 4.3.1 概念说明

`NativeRuleMap` 是「原生 show 规则注册表」。它把「某元素在某 Target 下的展示规则」存起来，供 realize 阶段查询。

它本质上是一个以 `(Element, Target)` 二元组为键、以 `NativeShowRule`（包装了 `ShowFn`）为值的映射。`Element` 是「元素类型」的运行时句柄（如「这是 HeadingElem」），`Target` 是编译目标（如 `Paged`）。

`NativeRuleMap` 提供两个关键方法：

- `new()`——构造一张预装了「所有目标通用规则」的表（如 `CONTEXT_RULE`、`COUNTER_DISPLAY_RULE`）。
- `register(target, f)`——往表里**追加**一条规则，键为 `(T::ELEM, target)`。

`register` 有一个防御性设计：**重复注册会 panic**。因为同一个「元素 + 目标」组合只能有一条规则，重复注册几乎必然是开发者的笔误，不如启动时直接崩溃暴露问题。

#### 4.3.2 核心流程

规则表的构建发生在程序启动、构建 `Library` 时，只发生一次：

```
NativeRuleMap::new()         // 预装通用规则（对所有 target）
       │
       ▼
typst_layout::register(&mut) // 追加 59 条 Paged 规则
typst_html::register(&mut)   // 追加 Html 规则
       │
       ▼
   不可变的 NativeRuleMap（随 Library 全局共享）
       │
       ▼  realize 阶段反复查询
   拿 (元素, Paged) 查到 ShowFn → 调用 → 新 Content
```

构建完成后这张表就**只读**了；后续整个排版过程都是查表，不改表。

#### 4.3.3 源码精读

`NativeRuleMap` 的结构定义：

[crates/typst-library/src/foundations/styles.rs:985-989](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L985-L989) — `NativeRuleMap` 内部就是一个 `IndexMap<(Element, Target), NativeShowRule>`。键是「元素类型 + 编译目标」二元组。

`register` 方法：

[crates/typst-library/src/foundations/styles.rs:1037-1049](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1037-L1049) — `register` 方法：用 `(T::ELEM, target)` 作键插入；若键已存在（`insert` 返回 `Some`）则 panic，提示「duplicate native show rule」。

`NativeRuleMap::new()` 预装的通用规则值得一看，它解释了「为什么有些规则不在 typst-layout 里」：

[crates/typst-library/src/foundations/styles.rs:1005-1035](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1005-L1035) — `new()` 对三个 target 都注册了 `CONTEXT_RULE`、`COUNTER_DISPLAY_RULE` 等与具体输出无关的通用规则，以及若干「只为内省、空内容」的元素规则。

也就是说：`CONTEXT_RULE` 这类「与目标无关」的规则由 typst-library 自己在 `new()` 里装好；而**与排版强相关**的规则（table、image、heading 等）才交给 `typst-layout::register` 装。这就是 typst-library 与 typst-layout 的分工线。

最后是真正的调用点——`register` 在哪被调：

[crates/typst/src/lib.rs:311-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L311-L317) — 顶层 `ROUTINES` 静态量的 `rules` 闭包：先 `NativeRuleMap::new()`，再依次调 `typst_layout::register(&mut rules)` 和 `typst_html::register(&mut rules)`。

这是个 `LazyLock`（懒加载静态），第一次用到 `ROUTINES` 时才执行——也就是第一次构建 Library 时。它也是 typst 的「动态链接」手段：顶层 crate 通过函数指针把各子 crate 的能力拼装起来，从而实现 crate 拆分（避免循环依赖）。

而 typst-layout 这边的 `register` 对外导出只有一处：

[crates/typst-layout/src/lib.rs:24-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L24) — `pub use self::rules::register;`，整个 crate 唯一的「粘合层」导出。

#### 4.3.4 代码实践

**实践目标**：验证「register 只在启动时调一次，重复注册会 panic」。

**操作步骤**：

1. 在 `crates/typst/src/lib.rs:311` 附近确认 `ROUTINES` 是 `LazyLock<Routines>`，`rules` 是个闭包字段。
2. 在 `crates/typst-library/src/foundations/styles.rs:1041` 阅读 `register` 的 panic 分支。
3. 思考实验：如果有人在 `typst-layout/src/rules.rs` 的 `register` 里手误把 `STRONG_RULE` 注册了两遍，会发生什么？在哪一行、什么时候报错？

**需要观察的现象 / 预期结果**：

- 预期：由于 `LazyLock` 只初始化一次，`register` 只在「第一次有人访问 `ROUTINES.rules`」时执行一次。
- 若重复注册同一个 `(StrongElem, Paged)`，`insert` 返回 `Some`，触发 `panic!("duplicate native show rule for \`strong\` on Paged target")`。错误发生在程序启动（首次构建 Library）阶段，而非编译某份具体文档时——所以是「fail fast」的启动期断言。

#### 4.3.5 小练习与答案

**练习 1**：`NativeRuleMap` 的键为什么是 `(Element, Target)` 而不仅仅是 `Element`？

**参考答案**：因为同一个元素在不同编译目标（Paged / Html / Bundle）下可能有不同的展示规则。把 Target 纳入键，让每个「元素 × 目标」组合各自独立一条规则，互不覆盖。

**练习 2**：`register` 重复注册时为什么选择 panic 而不是返回 `Result`？

**参考答案**：重复注册是「开发者写注册代码时的笔误」，属于不可恢复的编程错误，且只在启动构建期发生。用 panic（配合 `#[track_caller]`）能在出错点立即暴露问题，比返回 `Result` 让调用方处理更直接、更不容易被忽视。

---

### 4.4 register 的六大分类与 layouter 挂载模式

#### 4.4.1 概念说明

`register` 函数体并不是一锅乱炖，它按 **Model / Text / Layout / Visualize / Math / PDF** 六大分类整齐地注册了 59 条规则。这六个分类恰好对应 `typst-library` 的六个模块（`model`、`text`、`layout`、`visualize`、`math`、`pdf`），也对应 `rules.rs` 顶部的六组 `use` 导入。

统计如下（已逐行核对源码）：

| 分类 | 注册区间（行） | 规则数 | 典型元素 |
| --- | --- | --- | --- |
| Model | 43–67 | 25 | strong, emph, list, enum, heading, figure, footnote, outline, bibliography, table … |
| Text | 70–78 | 9 | sub, super, underline, strike, highlight, smallcaps, raw … |
| Layout | 81–93 | 13 | align, pad, columns, stack, grid, move, rotate, hide, layout … |
| Visualize | 96–103 | 8 | image, line, rect, square, ellipse, circle, polygon, curve |
| Math | 106 | 1 | equation |
| PDF | 109–111 | 3 | attach, artifact, pdf-marker-tag |
| **合计** | | **59** | |

这 59 条规则按「返回什么」又可以归为两种写法，第二种是本模块的重点：

- **纯样式变更型**：返回 `body.set(...)` 或 `body.aligned(...)`。不挂 layouter。如 `STRONG_RULE`、`EMPH_RULE`、`HIDE_RULE`。
- **挂 layouter 型**：把元素塞进 `BlockElem::multi_layouter(elem, 某排版函数)` 或 `BlockElem::single_layouter(...)`，把 typst-layout 里**未导出的**排版函数接进流程。如 `TABLE_RULE → crate::grid::layout_table`、`STACK_RULE → crate::stack::layout_stack`。

第二种写法正是「为什么 `lib.rs` 门面里没有 `layout_grid` / `layout_stack` 这些函数」的答案：它们不需要被外部直接调用，而是通过 `multi_layouter` 卡槽，由 show rule 间接触发（u1-l3 已埋下这个伏笔，本讲给出机制）。

#### 4.4.2 核心流程

挂 layouter 型规则的链路：

```
realize 遇到 TableElem（Target::Paged）
   │  查 NativeRuleMap[(Table, Paged)] → TABLE_RULE
   ▼
TABLE_RULE 调用 BlockElem::multi_layouter(elem, crate::grid::layout_table)
   │  产出一个 BlockElem，其 body 是 MultiLayouter(callback)
   ▼
该 BlockElem 后续在 flow 里被排版时
   │  flow 发现 body 是 MultiLayouter，取出 callback
   ▼
调用 crate::grid::layout_table(elem, engine, locator, styles, regions)
   │  返回 Fragment（若干 Frame）
   ▼
拼进页面
```

关键：layouter 的签名比 `ShowFn` 多了 `locator` 和 `regions`/`region`（几何画布），因为它要真正排版、会跨区域断裂。`ShowFn` 只管「翻译成什么内容」，layouter 才管「排成什么几何」。

`BlockElem` 提供两种 layouter 卡槽，差别在于「能否跨区域断裂、收几个区域」：

- `single_layouter`：收单个 `Region`，返回单个 `Frame`，并自动设 `breakable: false`（不可断裂）。
- `multi_layouter`：收多个 `Regions`，返回 `Fragment`（可多帧），默认可断裂。

此外 `InlineElem::layouter` 是行内版：收单个 `Size`，返回 `Vec<InlineItem>`，供行内公式等使用。

#### 4.4.3 源码精读

`register` 的六大分类主体：

[crates/typst-layout/src/rules.rs:42-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L42-L111) — 六个分类注释（`// Model.` / `// Text.` / …）下的全部 `rules.register(Paged, *_RULE)` 调用。每行一条，顺序与上方统计表一致。

挂 layouter 型最短代表是 `TABLE_RULE`：

[crates/typst-layout/src/rules.rs:526-528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L526-L528) — `TABLE_RULE`：`BlockElem::multi_layouter(elem.clone(), crate::grid::layout_table).pack()`，一行把表格元素挂上 `grid::layout_table` 排版器。

对照 `GRID_RULE`（[rules.rs:686-688](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L686-L688)）、`STACK_RULE`（[rules.rs:682-684](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L682-L684)）、`COLUMNS_RULE`（[rules.rs:678-680](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L678-L680)）都是同一个套路。

而 `single_layouter` 的代表是变换类，如 `ROTATE_RULE`（[rules.rs:724-726](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L724-L726)）——旋转不可断裂，故用 single。

两种 layouter 卡槽的定义在 typst-library：

[crates/typst-library/src/layout/container.rs:402-437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L402-L437) — `single_layouter`（收 `Region`→`Frame`，置 `breakable:false`）与 `multi_layouter`（收 `Regions`→`Fragment`）。两者都是泛型 `T: NativeElement`，把「被捕获的元素」和「回调函数指针」打包进 `BlockBody` 的对应变体。

注意签名差异：layouter 回调比 `ShowFn` 多了 `locator: Locator` 与几何参数（`Region` / `Regions`）。这与 u2 系列讲的「Locator 给身份、Regions 给画布」一脉相承。

> 特例：`EQUATION_RULE` 会按 `block` 字段在 `multi_layouter`（块级公式）与 `InlineElem::layouter`（行内公式）之间二选一，见 [rules.rs:805-812](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L805-L812)。它是唯一一条同时可能用上行内/块级两种卡槽的规则。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手统计六大分类的规则数量，并把「语义元素 → 样式变更」的翻译讲透（对应本讲规格里的实践任务）。

**操作步骤（A：统计）**：

1. 打开 [crates/typst-layout/src/rules.rs:39-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L39-L112)。
2. 数每个 `// 分类.` 注释之后到下一个分类注释之前的 `rules.register(Paged, ...)` 行数。
3. 用下表核对（已从源码逐行核对，可作为答案）：

| 分类 | 数量 |
| --- | --- |
| Model | 25 |
| Text | 9 |
| Layout | 13 |
| Visualize | 8 |
| Math | 1 |
| PDF | 3 |
| **总计** | **59** |

> 验证小窍门：在仓库根目录对 `crates/typst-layout/src/rules.rs` 执行 `grep -c "rules.register(Paged,"`，应得到 `59`（本讲作者已验证）。

**操作步骤（B：STRONG_RULE / EMPH_RULE 语义翻译）**：

4. 回到 [rules.rs:114-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L114-L122)。
5. 用一段话写出它们如何把语义元素转成样式变更。参考表述见下方「预期结果」。

**需要观察的现象 / 预期结果**：

- A 的总数必须是 59；若你数出别的数，多半是漏了某条或多算了空行。
- B 的预期表述：`#strong[正文]` 经 `STRONG_RULE` 变成「正文 + 一个设了 `TextElem::delta = WeightDelta(...)` 的样式」，下游整形时字重叠加该增量从而变粗；`#emph[正文]` 经 `EMPH_RULE` 变成「正文 + 一个设了 `TextElem::emph = ItalicToggle(true)` 的样式」，下游把 emph 解释成斜体。两条规则都不产生几何，只改样式——这正说明 show rule 是「语义层」与「排版层」之间的翻译器。

**待本地验证**：如果你想确认某条规则是否如你所想被分到了正确分类，可以在 `register` 里临时给某条规则加注释掉、编译看哪个内置元素「塌缩」成了原样（例如注释 `IMAGE_RULE` 后 `#image(...)` 可能不再按图形排版）。改动仅用于本地观察。

#### 4.4.5 小练习与答案

**练习 1**：`TABLE_RULE` 用的是 `multi_layouter` 还是 `single_layouter`？为什么？

**参考答案**：用 `multi_layouter`。因为表格可能很高、需要跨页（跨区域）断裂，要接收多个 `Regions` 并返回多帧 `Fragment`。`single_layouter` 只能产单帧、不可断裂，适合旋转/缩放这类「一锤子」变换。

**练习 2**：为什么 `lib.rs` 的 `pub use` 门面里没有 `layout_grid`、`layout_stack` 这些函数？

**参考答案**：因为它们不需要被外部 crate 直接调用。它们的唯一调用者是 `GRID_RULE` / `STACK_RULE`，通过 `BlockElem::multi_layouter(elem, crate::stack::layout_stack)` 这种「show rule 挂 layouter」的方式间接接入。所以门面只导出 `register`（把规则挂上去），而不导出各 layouter 本身。

**练习 3**：`PDF` 分类里的 `ATTACH_RULE` 直接返回 `Content::empty()`（见 [rules.rs:814-814](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L814-L814)），说明什么？

**参考答案**：说明 `AttachElem` 在分页排版中「不产生任何可见内容」——它纯粹是 PDF 附件功能的载体，所有作用都在导出（typst-pdf）阶段体现，排版阶段把它消解为空。这体现了 show rule 也能用来「让某元素在特定目标下静默」。

## 5. 综合实践

把本讲四个模块串起来，完成下面这张「`#strong[x]` 的端到端旅程」追踪表。请先**独立填写**，再对照参考答案。

任务：假设用户编译一份含 `#strong[你好]` 的 Typst 文档，目标为 PDF。请按下表每一行，给出**涉及的概念 / 文件 / 行号**，以及该步骤「做了什么」。

| 阶段 | 涉及对象（文件:行） | 这一步做了什么 |
| --- | --- | --- |
| 1. 注册规则 | （填写） | （填写） |
| 2. 选择目标 | （填写） | （填写） |
| 3. 查表取规则 | （填写） | （填写） |
| 4. 调用规则 | （填写） | （填写） |
| 5. 后续排版 | （填写） | （填写） |

**参考答案**：

1. **注册规则**：[crates/typst/src/lib.rs:312-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L312-L317) 的 `rules` 闭包里调用 `typst_layout::register(&mut rules)`，其中 [rules.rs:43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L43-L43) 执行 `rules.register(Paged, STRONG_RULE)`，把 `(StrongElem, Paged) → STRONG_RULE` 装进 `NativeRuleMap`。这一步只在启动时做一次。
2. **选择目标**：本次编译目标是 `Target::Paged`（[target.rs:68-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L68-L70) 的默认变体）。
3. **查表取规则**：realize 遇到 `StrongElem` 时，用键 `(StrongElem, Paged)` 在 [NativeRuleMap](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L985-L989)（键定义见 [styles.rs:1041-1042](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1041-L1042)）里查到 `STRONG_RULE`。
4. **调用规则**：执行 [rules.rs:114-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L114-L119) 的 `STRONG_RULE`，返回 `你好.set(TextElem::delta, WeightDelta(...))`——即「『你好』+ 一个加粗样式」。此步不画任何几何。
5. **后续排版**：返回的带样式 Content 继续被 realize 展开，最终在 inline 阶段（整形）读 `TextElem::delta` 把字重加粗，画出粗体「你好」。这一步才真正改变几何/字形。

完成本表后，你应该能脱口而出：**register 负责把规则装进表、Target 决定查哪条、ShowFn 定义规则长什么样、NativeRuleMap 是那张表——四者合力让 `#strong` 变成粗体。**

## 6. 本讲小结

- `Target::Paged` 是「分页排版」编译目标，是 typst-layout 全部服务的默认目标；`Target` 是原生 show rule 的第二维索引，让同一元素在不同产物下有不同展示。
- `ShowFn<T> = fn(&Packed<T>, &mut Engine, StyleChain) -> SourceResult<Content>` 是所有原生 show rule 的统一签名；它是裸函数指针，所以每条 `*_RULE` 都是不捕获的 `const` 闭包。
- `STRONG_RULE` / `EMPH_RULE` 是「纯样式变更型」代表：它们只把语义元素（粗体/强调）翻译成 `TextElem` 的样式变更（`delta` / `emph`），不碰几何——真正的字形变化由后续 inline 排版完成。
- `NativeRuleMap` 是以 `(Element, Target)` 为键的规则注册表；`register` 方法重复注册会 panic（启动期 fail-fast 断言）；与目标无关的通用规则由 typst-library 在 `new()` 里预装，与排版强相关的规则才由 typst-layout 注册。
- `register` 按 Model/Text/Layout/Visualize/Math/PDF 六类共注册 59 条规则；其中「挂 layouter 型」通过 `BlockElem::multi_layouter` / `single_layouter` 把 typst-layout 内部未导出的排版器接入流程，这解释了门面为何不导出 `layout_grid` 等函数。
- 真正的注册调用发生在顶层 `typst` crate 的 `ROUTINES` 懒加载静态量里（`typst_layout::register(&mut rules)`），随 Library 构建执行一次，之后表只读。

## 7. 下一步学习建议

- **下一讲 u7-l2（常用 show 规则实现详解）**：本讲只回答「规则怎么挂」，u7-l2 将深入单条复杂规则的实现，重点看 `HEADING_RULE`（编号测量与悬挂缩进）、`FIGURE_RULE`（caption + float）、`BIBLIOGRAPHY_RULE`（grid / 缩进两种布局）以及 `LAYOUT_RULE`（把 regions 尺寸传给用户函数）。
- **回看 layouter 内部**：本讲提到 `TABLE_RULE → grid::layout_table`、`STACK_RULE → stack::layout_stack` 等挂载点。若想看这些 layouter 被挂上后内部如何排版，可读 u6-l1（GridLayouter）、u6-l5（StackLayouter）、u4-l6（columns）。
- **理解调用时机**：若想进一步确认「realize 何时、如何查 NativeRuleMap 并调用 ShowFn」，建议阅读 typst-realize 的 realize 主循环（u1-l4 已给出宏观链路），把它与本讲的注册表闭环。
- **动手验证**：挑一条本讲统计表里的规则，预测「注释掉它之后哪个内置元素会失效」，再在本地编译验证（仅本地观察，勿提交），以此巩固「每条规则对应一个内置元素」的对应关系。
