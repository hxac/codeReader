# 内建 show 规则注册机制

## 1. 本讲目标

本讲聚焦 `src/rules.rs`，回答一个核心问题：**Typst 是怎样知道「在导出 HTML 时，`*强调*` 要变成 `<strong>`、`= 标题 =` 要变成 `<h2>`」的？**

答案是一张「元素 → HTML」的映射表。这张表由 `register()` 在编译器启动时填充，表中每一项都是一个内建 show 规则（built-in show rule）。学完本讲你应当能够：

1. 说出 `register()` 如何按「模型 / 文本 / 可视化 / 数学」分类，把几十条 `XXX_RULE` 注册到 `Target::Html`。
2. 解释 `ShowFn<T>` 的函数签名、`NativeRuleMap` 以 `(Element, Target)` 为键的存储方式，以及内建规则在 realize 阶段如何被查表与调用。
3. 读懂 `STRONG_RULE`、`RAW_RULE`、`HEADING_RULE`、`FOOTNOTE_RULE` 四条代表性规则的真实源码。
4. 解释一个反直觉的设计：**为什么 Typst 的 level 1 标题映射成 `<h2>` 而不是 `<h1>`**，以及为什么 `FrameElem` 在 `Target::Paged` 上被注册成一个「什么都不做」的规则。

本讲承接 [u3-l3《convert_to_nodes 内容转换器》](u3-l3-convert-to-nodes.md)。在 u3-l3 里我们看到 `convert_to_nodes` 把已经「具象化（realize）」的内容逐个翻译成 `HtmlNode`；而本讲要讲的是 **realize 之前的那一步**——是 show 规则把 `StrongElem`、`HeadingElem` 这些 Typst 元素先变成了 `HtmlElem`（即 `html.elem`），`convert_to_nodes` 才有东西可翻译。

---

## 2. 前置知识

### 2.1 什么是 show 规则

在 Typst 里，`show` 规则决定「一个元素长什么样」。用户可以自己写：

```typst
#show heading: it => html.elem("h2", it.body)
```

这条规则的意思是：每当引擎遇到 `heading` 元素，就用后面的函数去「重新实现」它。但用户不可能为 `strong`、`emph`、`list`、`heading`、`table`……每一种元素都手写一条 HTML 映射。于是 typst-html 内置了一整套默认映射，这就是 **内建 show 规则（native / built-in show rule）**。它们用 Rust 写成，性能比用户的 Typst 闭包更高，也负责所有「开箱即用」的 HTML 输出。

### 2.2 Target：编译目标

Typst 有三种编译目标（`Target`）：

- `Target::Paged`：分页导出（PDF / PNG / SVG）。
- `Target::Html`：HTML 导出。
- `Target::Bundle`：把每个 HTML 片段单独打包（用于多文件站点）。

同一个元素在不同 target 下应当「变成不同的东西」。例如 `ImageElem` 在 Paged 下要被布局成图像帧，在 Html 下要变成 `<img>`。`Target` 是区分这些行为的旋钮（参见 [u1-l3](u1-l3-export-pipeline-and-cli.md)：`Target::Html` 会作为样式值注入全局样式链）。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| `ShowFn<T>` | 内建 show 规则的函数签名（一个 Rust 函数指针） |
| `NativeRuleMap` | 存放所有内建 show 规则的表，键是 `(Element, Target)` |
| `NativeShowRule` | 把 `ShowFn<T>` 类型擦除后的统一形式，便于放进同一张表 |
| `Realize` | 「具象化」：对一个元素依次套用 show 规则，直到它变成可输出的内容 |
| `HtmlElem` | typst-html 的 `html.elem` 元素（内容层面的类型，由 `#[elem]` 宏定义） |
| `ARIA` | 无障碍富互联网应用规范，用 `role` 属性给辅助技术（如屏幕阅读器）提供语义 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/rules.rs` | **本讲主角**。定义 `register()` 入口与全部 `XXX_RULE` 常量 |
| `src/lib.rs` | 通过 `pub use self::rules::{..., register}` 把 `register` 暴露给根 crate 调用 |
| `crates/typst-library/src/foundations/styles.rs` | 定义 `ShowFn`、`NativeRuleMap`、`NativeShowRule` 这些承重类型 |
| `crates/typst-realize/src/lib.rs` | realize 主循环：查表并调用内建规则 |
| `crates/typst/src/lib.rs` | 在 `ROUTINES` 里把 `typst_html::register` 接进编译器启动流程 |
| `crates/typst-library/src/model/heading.rs` | `HeadingElem::resolve_level`：标题级别的解析逻辑 |

---

## 4. 核心概念与源码讲解

### 4.1 register：内建 show 规则的注册总入口

#### 4.1.1 概念说明

`register()` 是 typst-html 与 Typst 编译核心之间的一个 **装配点（wiring point）**。它本身不做任何「翻译」工作，只做一件事：把本 crate 里定义的几十个 `XXX_RULE` 常量，按 `(元素, Target::Html)` 的形式塞进一张全局表 `NativeRuleMap`。

为什么需要这样一个集中入口？因为 Typst 的 crate 拆分设计：编译核心（typst-library）不知道「HTML 导出」这回事，它只提供一张空的 `NativeRuleMap` 和「注册」的机制；由各导出器（typst-layout 负责 Paged、typst-html 负责 Html）在启动时各自调用 `register` 来填表。这是一种典型的 **依赖倒置**：核心定义扩展点，插件去填充。

#### 4.1.2 核心流程

注册的整体流程是：

```text
编译器启动
   │
   ▼
typst/src/lib.rs  ROUTINES.rules 闭包被调用
   │   （LazyLock，首次访问 Library 时才执行一次）
   ▼
1. NativeRuleMap::new()        ← 建空表，预填少量「所有 target 都需要」的规则
2. typst_layout::register(..)  ← 填 Paged 相关规则
3. typst_html::register(..)    ← 填 Html 相关规则（本讲重点）
   │
   ▼
之后每次 realize 一个元素时：
   拿到当前 Target → 用 (元素的 func, Target) 查表 → 找到 ShowFn → 调用它
```

`register()` 内部把规则按语义分成四组，注册顺序就是它们在源码里出现的顺序：

1. **Model（模型类）**：`PAR_RULE`、`STRONG_RULE`、`EMPH_RULE`、`LIST_RULE`、`ENUM_RULE`、`HEADING_RULE`、`FOOTNOTE_RULE`、`TABLE_RULE` 等结构性元素。
2. **Text（文本类）**：`SUB_RULE`、`UNDERLINE_RULE`、`RAW_RULE` 等行内文本修饰。
3. **Visualize（可视化类）**：仅 `IMAGE_RULE`。
4. **Math（数学类）**：仅 `EQUATION_RULE`。

末尾还有一条特殊的 `FrameElem` 注册，它注册在 **`Target::Paged`** 而非 `Html` 上，下面 4.1.3 会专门讲。

#### 4.1.3 源码精读

先看 `register` 的签名与分组注册（[src/rules.rs:39-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L39-L92)）：

```rust
/// Registers show rules for the [HTML target](Target::Html).
pub fn register(rules: &mut NativeRuleMap) {
    use Target::{Html, Paged};

    // Model.
    rules.register(Html, PAR_RULE);
    rules.register(Html, STRONG_RULE);
    // ... 几十条 ...
    rules.register(Html, TABLE_RULE);
    rules.register(Html, TABLE_CELL_RULE);

    // Text.
    rules.register(Html, SUB_RULE);
    // ...
    rules.register(Html, RAW_RULE);
    rules.register(Html, RAW_LINE_RULE);

    // Visualize.
    rules.register(Html, IMAGE_RULE);

    // Math.
    rules.register(Html, EQUATION_RULE);

    // FrameElem 的特殊注册（见下文）
    rules.register::<FrameElem>(Paged, |elem, _, _| Ok(elem.body.clone()));
}
```

这里有两个细节值得注意：

- 每条 `rules.register(Html, XXX_RULE)` 中，`XXX_RULE` 的类型是 `ShowFn<某Elem>`（例如 `ShowFn<StrongElem>`）。`register` 是泛型方法，会从闭包类型自动推断出对应的元素类型 `T`，无需手写元素名。这就是为什么这些调用看起来「只有规则、没有元素」——元素信息编码在规则的类型参数里。
- 最后那条 `rules.register::<FrameElem>(Paged, ...)` 显式标注了 `::<FrameElem>`，因为这里传的是一个 **内联闭包** 而非命名常量，编译器无法从一个临时闭包反推元素类型，所以必须显式指定。

这个 `register` 是被谁调用的？答案在根 crate 的 `ROUTINES` 静态变量里（[crates/typst/src/lib.rs:312-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L312-L317)）：

```rust
static ROUTINES: LazyLock<Routines> = LazyLock::new(|| Routines {
    rules: || {
        let mut rules = NativeRuleMap::new();
        typst_layout::register(&mut rules);
        typst_html::register(&mut rules);
        rules
    },
    // ... 其它例程 ...
});
```

这段代码说明：`NativeRuleMap` 是 **懒构造** 的，只有在第一次真正编译文档时，`rules` 闭包才会运行一次，之后整张表就被复用。注释也点明了这是「为了 crate 拆分而做的动态链接」——核心 crate 不直接依赖 typst-html，而是通过函数指针在运行时把 `typst_html::register` 「链接」进来。

**关于 `FrameElem` 的 Paged no-op**：末尾这行（[src/rules.rs:88-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L88-L92)）是本节最反直觉的一处，原文注释解释得很清楚：

```rust
// For the HTML target, `html.frame` is a primitive. In the laid-out target,
// it should be a no-op so that nested frames don't break (things like `show
// math.equation: html.frame` can result in nested ones).
rules.register::<FrameElem>(Paged, |elem, _, _| Ok(elem.body.clone()));
```

回忆 [u1-l4](u1-l4-user-api-html-elem-frame.md)：`html.frame` 的作用是把内容切回 `Target::Paged` 排版成 Frame 再嵌入 HTML。但是「排版成 Frame」这个动作本身又是一次完整的 realize，而这次 realize 会在 **Paged target** 下进行。如果用户写了 `show math.equation: html.frame`，就会得到「Frame 里套着方程，方程又触发 Frame……」的递归。为避免无限嵌套，typst-html 在 Paged target 下把 `FrameElem` 注册成「直接返回它的 body」——也就是 **透明地展开**，让内层 Frame 蒸发。这条规则是 typst-html **唯一一条** 注册在非 Html target 上的规则，也正因如此它显得突兀，被单独放在 `register` 的末尾。

#### 4.1.4 代码实践

**实践目标**：从源码层面建立对 `register` 全貌的把握。

**操作步骤**：

1. 打开 [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs)，定位 `pub fn register`（第 39 行）。
2. 用编辑器的折叠或肉眼，数一下 `rules.register(Html, ...)` 一共有多少条；按注释分成 Model / Text / Visualize / Math 四组各统计数量。
3. 找到唯一一条 `rules.register::<FrameElem>(Paged, ...)`，确认它注册在 `Paged` 而非 `Html`。

**需要观察的现象**：

- Model 组规则最多（约 25 条），覆盖了几乎所有结构性元素（段落、列表、标题、脚注、表格、目录、参考文献等）。
- Visualize 和 Math 各只有 1 条。
- `FrameElem` 的 Paged 注册在所有 `Html` 注册之后，且用闭包而非命名常量。

**预期结果**：你会得到一个「typst-html 关心的 HTML 元素清单」——凡是没有出现在 `register` 里的元素（比如某些纯排版元素），在 HTML 导出时就没有专属映射，会落到 realize 的兜底逻辑（u3-l3 讲过的 `warn` 警告）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `register` 要接收 `&mut NativeRuleMap` 而不是自己 `new` 一个返回？

> **参考答案**：因为 `NativeRuleMap` 是 **跨导出器共享** 的。`NativeRuleMap::new()` 已经预填了一些「所有 target 都需要」的规则（如 `CONTEXT_RULE`、`MetadataElem` 的空规则），随后 `typst_layout::register` 又填了 Paged 规则。typst-html 只能在 **同一张表** 上追加 Html 规则，所以必须以外借 `&mut` 的方式注入，而不能自建一张新表覆盖掉前面的注册。

**练习 2**：如果某天有人误写了 `rules.register(Html, STRONG_RULE)` 两次，会发生什么？

> **参考答案**：`NativeRuleMap::register` 内部用 `insert`，若键 `(StrongElem::ELEM, Html)` 已存在会 panic，提示 `duplicate native show rule for 'strong' on Html target`（见 [styles.rs:1041-1049](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1041-L1049)）。这是启动期的断言，能在开发阶段立刻暴露重复注册。

---

### 4.2 ShowFn 类型与 NativeRuleMap 分发机制

#### 4.2.1 概念说明

上一节我们看到 `register` 往表里塞「规则」，但「规则」到底是什么类型？这一节讲清楚三件事：

1. **`ShowFn<T>` 是函数指针**：每条规则就是一个普通 Rust 函数，签名固定。
2. **`NativeRuleMap` 是一张以 `(Element, Target)` 为键的哈希表**：它要能存放「任意元素类型」的规则，所以需要类型擦除。
3. **查表调用发生在 realize 阶段**，且内建规则是 **兜底**——用户的 `show` 规则优先。

#### 4.2.2 核心流程

一条规则从「被定义」到「被执行」的完整生命周期：

```text
定义期（编译 typst-html 时）
   const STRONG_RULE: ShowFn<StrongElem> = |elem, _, _| { ... };
        │  这是一个 fn 指针，类型里编码了 StrongElem
        ▼
注册期（编译器首次启动）
   rules.register(Html, STRONG_RULE)
        │  NativeShowRule::new(f) 把 ShowFn<StrongElem> 擦除成
        │  统一的 unsafe fn(&Content, ...) 存进表
        ▼
查表期（每次 realize 一个 StrongElem）
   1. 先查用户写的 show 规则 → 若命中，用用户的，结束
   2. 否则 engine.library.rules.get(Target::Html, &strong_content)
        → 返回 Option<NativeShowRule>
   3. rule.apply(&content, engine, styles)
        → 内部断言元素类型正确，再调原始 ShowFn
        ▼
   得到新的 Content（通常是 HtmlElem::new(tag::strong).pack()）
```

关键点：**类型擦除**。`ShowFn<StrongElem>` 接收 `&Packed<StrongElem>`，而 `ShowFn<HeadingElem>` 接收 `&Packed<HeadingElem>`，二者是不同的函数指针类型，无法塞进同一个 `HashMap`。`NativeShowRule::new` 用 `std::mem::transmute` 把它们统一成 `unsafe fn(&Content, ...)`——之所以安全，是因为 `Packed<T>` 是 `Content` 的透明包装（transparent wrapper），内存布局相同。代价是：调用方必须保证传进去的 `Content` 确实是对应类型的元素，否则未定义行为，所以 `apply` 里有一句 `assert_eq!(content.elem(), self.elem)` 做运行时兜底。

#### 4.2.3 源码精读

**`ShowFn<T>` 的签名**（[crates/typst-library/src/foundations/styles.rs:991-996](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L991-L996)）：

```rust
/// The signature of a native show rule.
pub type ShowFn<T> = fn(
    elem: &Packed<T>,
    engine: &mut Engine,
    styles: StyleChain,
) -> SourceResult<Content>;
```

三个参数分别表示：

- `elem: &Packed<T>`：被 show 的元素本身（只读引用）。
- `engine: &mut Engine`：编译引擎，规则可以借它做内省查询、发警告、解码图片等。
- `styles: StyleChain`：当前的样式链，规则据此读取 `block`、`tight`、`numbering` 等字段。

返回 `SourceResult<Content>`：可能失败（返回错误），成功则给出「具象化后的内容」——这个内容通常是 `HtmlElem`，但也可以是任意 `Content`（例如 `FootnoteElem` 规则返回的是 `HElem + SuperElem + marker` 的组合，并不是单个 `HtmlElem`）。

**`NativeRuleMap` 的存储与注册**（[crates/typst-library/src/foundations/styles.rs:985-1049](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L985-L1049)）：

```rust
pub struct NativeRuleMap {
    rules: IndexMap<(Element, Target), NativeShowRule, FxBuildHasher>,
}

impl NativeRuleMap {
    pub fn register<T: NativeElement>(&mut self, target: Target, f: ShowFn<T>) {
        let res = self.rules.insert((T::ELEM, target), NativeShowRule::new(f));
        if res.is_some() { /* panic: duplicate */ }
    }

    pub fn get(&self, target: Target, content: &Content) -> Option<NativeShowRule> {
        self.rules.get(&(content.func(), target)).copied()
    }
}
```

注意键用的是 `(Element, Target)` 而非元素名 `&str`——`Element` 是类型擦除后的元素标识，比较是廉价的。`get` 用 `content.func()` 拿到元素的 `Element` 句柄再查表。

**类型擦除与调用**（[crates/typst-library/src/foundations/styles.rs:1092-1133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1092-L1133)）：

```rust
pub struct NativeShowRule {
    elem: Element,
    f: unsafe fn(&Content, &mut Engine, StyleChain) -> SourceResult<Content>,
}

impl NativeShowRule {
    pub fn new<T: NativeElement>(f: ShowFn<T>) -> Self {
        Self {
            elem: T::ELEM,
            f: unsafe { std::mem::transmute(f) }, // 安全性靠 Packed<T> 透明包装保证
        }
    }

    pub fn apply(&self, content: &Content, engine: &mut Engine, styles: StyleChain)
        -> SourceResult<Content>
    {
        assert_eq!(content.elem(), self.elem); // 运行时类型校验
        unsafe { (self.f)(content, engine, styles) }
    }
}
```

**查表的时机：realize 主循环**。这一点非常关键，它决定了用户规则和内建规则谁优先。在 [crates/typst-realize/src/lib.rs:506-513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L506-L513)：

```rust
// If we found no user-defined rule, also consider the built-in show rule.
if step.is_none() {
    let target = styles.get(TargetElem::target);
    if let Some(rule) = engine.library.rules.get(target, elem) {
        step = Some(ShowStep::Builtin(rule));
    }
}
```

这段揭示了三层优先级：

1. **用户的 `show` 规则优先**：先在用户的规则链里找（前面那个 `step` 循环）。
2. **找不到才查内建规则**：用当前 target（从样式链读 `TargetElem::target`，HTML 导出时是 `Target::Html`）去 `NativeRuleMap` 查。
3. **若内建规则也没有**：`step` 仍为 `None`，元素保持原样，交给后续的 convert 阶段（可能触发「无法转换为 HTML」的警告）。

随后内建规则被调用（[crates/typst-realize/src/lib.rs:388-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L388-L393)）：

```rust
// Apply a built-in show rule.
ShowStep::Builtin(rule) => {
    rule.apply(&output, s.engine, chained)
        .map(|content| content.spanned(output.span()))
}
```

这正是上文的 `NativeShowRule::apply`。

#### 4.2.4 代码实践

**实践目标**：亲手验证「用户 show 规则覆盖内建规则」的优先级。

**操作步骤**：

1. 准备一个最小 Typst 文件 `demo.typ`：
   ```typst
   = 第一层标题
   *强调文本*
   ```
2. 用 CLI 导出为 HTML（CLI 会根据 `.html` 后缀推断格式，见 [typst-cli/src/compile.rs:118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L118)）：
   ```bash
   typst compile demo.typ demo.html
   # 或显式指定格式
   typst compile --format html demo.typ demo.html
   ```
3. 打开 `demo.html`，观察 `*强调文本*` 变成了什么标签。
4. 在文件开头加一条用户规则覆盖 `strong`：
   ```typst
   #show strong: html.elem("b")
   = 第一层标题
   *强调文本*
   ```
5. 重新编译，对比输出。

**需要观察的现象**：

- 第 3 步：`*强调文本*` 应输出为 `<strong>强调文本</strong>`（来自内建 `STRONG_RULE`）。
- 第 5 步：加了 `#show strong: html.elem("b")` 后，应输出 `<b>强调文本</b>`——用户的规则完全取代了内建规则。

**预期结果**：这印证了 [typst-realize/src/lib.rs:506](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L506) 的注释「If we found no user-defined rule, also consider the built-in show rule」——内建规则只在用户没写时才兜底。

> 若本地没有编译好的 `typst` 可执行文件，此步骤标注为「待本地验证」。你也可以在源码层面完成等价练习：在 `typst-realize/src/lib.rs` 中找到 `step.is_none()` 那段，确认用户规则的查找循环在其 **之前**，从而得出优先级结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `NativeShowRule::new` 要用 `transmute` 把 `ShowFn<T>` 转成 `unsafe fn(&Content, ...)`？能不能直接用一个泛型容器？

> **参考答案**：Rust 的不同 `ShowFn<T>`（如 `ShowFn<StrongElem>` 与 `ShowFn<HeadingElem>`）是 **不同的具体类型**，而 `HashMap<K, V>` 要求所有 `V` 同类型。要把它们塞进一张表，必须类型擦除。这里用 `transmute` 成立的前提是 `Packed<T>` 是 `Content` 的透明新类型（`#[repr(transparent)]`），二者内存布局一致，函数指针签名仅在第一个参数的「名义类型」上不同，故可安全转换。用 `Box<dyn Trait>` 也能擦除类型，但会引入堆分配与虚表，而 show 规则在每次编译中被海量调用，函数指针 + transmute 是零成本且可 `Copy` 的方案。

**练习 2**：`apply` 里的 `assert_eq!(content.elem(), self.elem)` 是冗余的吗？

> **参考答案**：不是冗余。它是一道防线：`NativeShowRule.f` 是 `unsafe fn`，调用方有义务保证传入正确类型。realize 里查表用的是 `(content.func(), target)`，理论上 `get` 返回的规则元素类型必然匹配；但 `apply` 是公开 API，未来可能有别的调用路径。这个断言把「类型不匹配」从「未定义行为」降级为「清晰的 panic」，是 unsafe 边界常见的防御性写法。

---

### 4.3 文本与强调规则：STRONG_RULE 与 RAW_RULE

#### 4.3.1 概念说明

本节用两条规则展示「内建 show 规则」的两种复杂度。`STRONG_RULE` 是 **最简单** 的形态：原样包一层标签；`RAW_RULE` 则展示了「需要读取样式、组装子元素序列、根据 `block` 分流」的更典型形态。

#### 4.3.2 核心流程

`STRONG_RULE`（[src/rules.rs:97-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L97-L98)）的执行过程：

```text
输入：StrongElem { body: "...强调..." }
   │  HtmlElem::new(tag::strong)         ← 新建一个 <strong>
   │  .with_body(Some(elem.body.clone())) ← 把原 body 塞进去
   │  .pack()                             ← 装成 Content
   ▼
输出：Content(HtmlElem { tag: strong, body: "...强调..." })
```

整条规则只用到三个参数里的 `elem`，连 `engine` 和 `styles` 都没碰——因为 `*强调*` 的转换不依赖任何样式或上下文。同类型的还有 `EMPH_RULE`（→ `<em>`）、`SUB_RULE`（→ `<sub>`）、`STRIKE_RULE`（→ `<s>`）、`HIGHLIGHT_RULE`（→ `<mark>`）。

`RAW_RULE`（[src/rules.rs:726-755](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L726-L755)）复杂得多，因为它要处理「多行代码」「行内 vs 块级」「语言标注」：

```text
输入：RawElem { lines: [RawLine, RawLine, ...], lang: "rs", block: false }
   │
   ├─ 把每行 RawLine 用 LinebreakElem 连接成序列 seq
   │   （行间插入换行，首行不插）
   │
   ├─ HtmlElem::new(tag::code)
   │   .with_optional_attr("data-lang", lang)   ← 语言写在 data-lang
   │   .with_body(seq)
   │   → code 元素
   │
   └─ if block {
         BlockElem::packed(<pre>{code}</pre>)    ← 块级再包一层 <pre>
       } else {
         code                                     ← 行内只输出 <code>
       }
```

#### 4.3.3 源码精读

**最简形态——STRONG_RULE**（[src/rules.rs:97-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L97-L98)）：

```rust
const STRONG_RULE: ShowFn<StrongElem> =
    |elem, _, _| Ok(HtmlElem::new(tag::strong).with_body(Some(elem.body.clone())).pack());
```

这里出现了一组在 u2 系列讲过的 builder 方法：`HtmlElem::new(tag)` 创建元素（[src/dom.rs:210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L210) 的 `HtmlElement::new` 是其在 DOM 层的对应物），`with_body` 设置 body 字段，`.pack()` 把这个 `HtmlElem` 打包成 `Content` 以便流回 realize 管线。注意 `STRONG_RULE` 没有用 `BlockElem::packed` 包裹——`<strong>` 是行内元素，不需要块级提升（关于块级提升见 u4-l2）。

**复合形态——RAW_RULE**（[src/rules.rs:726-755](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L726-L755)）：

```rust
const RAW_RULE: ShowFn<RawElem> = |elem, _, styles| {
    let lines = elem.lines.as_deref().unwrap_or_default();

    let mut seq = EcoVec::with_capacity((2 * lines.len()).saturating_sub(1));
    for (i, line) in lines.iter().enumerate() {
        if i != 0 {
            seq.push(LinebreakElem::shared().clone());   // 行间换行
        }
        seq.push(line.clone().pack());                   // 每行 RawLine
    }

    let lang = elem.lang.get_ref(styles);
    let code = HtmlElem::new(tag::code)
        .with_optional_attr(const { HtmlAttr::constant("data-lang") }, lang.clone())
        .with_body(Some(Content::sequence(seq)))
        .pack()
        .spanned(elem.span());

    Ok(if elem.block.get(styles) {
        BlockElem::packed(
            HtmlElem::new(tag::pre).with_body(Some(code)).pack().spanned(elem.span()),
        )
    } else {
        code
    })
};
```

几个值得注意的点：

- `RawLine` 自己也有一条规则 `RAW_LINE_RULE`（[src/rules.rs:771](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L771)），它只是「原样返回 body」，所以最终每行就是纯文本。
- 语言名被放进自定义属性 `data-lang`，用 `const { HtmlAttr::constant("data-lang") }` 在编译期驻留属性名（这是 u2-l3 讲过的 `HtmlAttr::constant`）。注意 `data-lang` **不是** 标准 HTML 属性，而是 typst-html 自己约定的高亮 hook。
- `block.get(styles)` 决定是否再包一层 `<pre>`：块级代码 → `<pre><code>..</code></pre>`，行内代码 → 裸 `<code>..</code>`。`BlockElem::packed` 的作用是把内容标记为块级（呼应 u4-l2 的 display 系统）。

> 对照 [tests/suite/html/syntax.typ:169-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/html/syntax.typ#L169-L191) 的 `html-script` 测试，可以看到 raw 文本的 pretty 打印行为，但那是 `html.script` 走的另一条 raw 路径，与本规则的处理细节（`<pre><code>`）不同。

#### 4.3.4 代码实践

**实践目标**：观察「行内代码」与「块级代码」在 HTML 输出上的差异，并验证 `data-lang` 的存在。

**操作步骤**：

1. 准备 `raw.typ`：
   ```typst
   行内代码：`fn main()`。

   ```rs
   fn main() {
       println!("hi");
   }
   ```
   ```
2. 编译：`typst compile --format html raw.typ raw.html`。
3. 打开 `raw.html` 查看两段代码各自生成的标签结构。

**需要观察的现象**：

- 行内 `` `fn main()` `` 应生成形如 `<code data-lang="...">fn main()</code>`（具体 `data-lang` 取决于默认语言设置；可能为空）。
- 块级代码应生成 `<pre><code ...>fn main() { ... }</code></pre>`。

**预期结果**：与 `RAW_RULE` 源码的两个分支一一对应——`block == false` 走 `code`，`block == true` 走 `BlockElem::packed(pre > code)`。这印证了「同一条规则根据 `styles` 产生不同结构」的典型模式。

> 标注为「待本地验证」：具体 `data-lang` 是否出现、默认语言为何，取决于你的 Typst 版本与设置。

#### 4.3.5 小练习与答案

**练习 1**：`STRONG_RULE` 为什么不调用 `BlockElem::packed`，而 `RAW_RULE` 在块级时却要？

> **参考答案**：`<strong>` 是 HTML 行内（phrasing）元素，应当留在行内流中；而块级 raw 代码语义上是一个独立块（类似段落），需要被当作块级元素参与后续的空白保护与 display 处理（u4-l1、u4-l2）。`BlockElem::packed` 是 typst-html 标记「这块内容是块级」的统一手段，convert 阶段会据此决定是否提升 display 或强制换行。

**练习 2**：`RAW_RULE` 用 `LinebreakElem::shared()` 连接各行，而不是直接在文本里插 `\n`。为什么？

> **参考答案**：因为在 HTML 导出里，换行不是「文本里的 `\n`」而是「`<br>` 元素」（见 u3-l3 的换行处理）。`LinebreakElem` 在后续 convert 阶段会被翻译成 `HtmlNode` 换行，与源码中的行边界保持语义一致；直接插 `\n` 反而会被空白折叠规则吞掉或被 pre-wrap 错误处理。

---

### 4.4 HEADING_RULE：标题级别偏移与无障碍回退

#### 4.4.1 概念说明

`HEADING_RULE` 是本讲最重要、也是面试最爱问的一条规则。它回答了一个反直觉的设计问题：**为什么 Typst 的 `= 标题 =`（level 1）导出后是 `<h2>` 而不是 `<h1>`？**

答案与「文档标题」的语义有关。在 HTML 规范里，`<h1>` 传统上表示 **整个文档的标题**，一篇文档应当只有一个 `<h1>`。而 Typst 把「文档标题」（`TitleElem`，即 `#set document(title: "...")` 设定的标题）与「章节标题」（`HeadingElem`，即 `=`、`==`）区分成了两个不同的元素。为了把 `<h1>` 这个「唯一文档标题位」留给 `TitleElem`，所有章节标题都 **整体下移一级**：Typst level 1 → `<h2>`，level 2 → `<h3>`，依此类推。

#### 4.4.2 核心流程

`HEADING_RULE`（[src/rules.rs:223-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L223-L261)）的逻辑：

```text
输入：HeadingElem { body, numbering?, level }
   │
   ├─ realized = body
   │
   ├─ 若设置了 numbering：
   │     通过 Counter::of(HeadingElem).display_at(..) 算出编号文本
   │     realized = 编号 + 空格 + body        ← 例如 "1.2 引言"
   │
   ├─ level = elem.resolve_level(styles).get()   ← 解析为 usize，最小为 1
   │
   └─ if level >= 6 {
        // HTML 只有 <h1>..<h6>，level 6 对应 h7 越界
        发警告 → 退化成 <div role="heading" aria-level="{level+1}">
     } else {
        // 关键：数组下标 level-1，但元素从 h2 起
        t = [h2, h3, h4, h5, h6][level - 1]
        输出 <t>{realized}</t>
     }
```

这里有一处容易看错的索引：数组 `[tag::h2, tag::h3, tag::h4, tag::h5, tag::h6]` 长度为 5，下标 `level - 1` 取值 `0..=4`，对应 `level` 取值 `1..=5`。也就是说：

| Typst level | 数组下标 | 输出标签 | 对应的 HTML「逻辑级别」 |
|-------------|----------|----------|------------------------|
| 1 (`=`)     | 0        | `<h2>`   | 2 |
| 2 (`==`)    | 1        | `<h3>`   | 3 |
| 3 (`===`)   | 2        | `<h4>`   | 4 |
| 4 (`====`)  | 3        | `<h5>`   | 5 |
| 5 (`=====`) | 4        | `<h6>`   | 6 |
| 6 及以上    | —        | `<div role="heading" aria-level="{level+1}">` | 7+（越界回退） |

所以「level + 1」是贯穿始终的偏移量：Typst level N → HTML level N+1。`level >= 6` 的判断正是因为 level 6 会映射到 h7，超出 HTML 的 h1..h6 范围，必须走 ARIA 回退。

与之对照，`TITLE_RULE`（[src/rules.rs:214-221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L214-L221)）直接用 `tag::h1`，印证了「`<h1>` 留给文档标题」的设计：

```rust
const TITLE_RULE: ShowFn<TitleElem> = |elem, _, styles| {
    Ok(BlockElem::packed(
        HtmlElem::new(tag::h1)
            .with_body(Some(elem.resolve_body(styles).at(elem.span())?))
            .pack()
            .spanned(elem.span()),
    ))
};
```

#### 4.4.3 源码精读

**完整的 HEADING_RULE**（[src/rules.rs:223-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L223-L261)）：

```rust
const HEADING_RULE: ShowFn<HeadingElem> = |elem, engine, styles| {
    let span = elem.span();

    let mut realized = elem.body.clone();
    if let Some(numbering) = elem.numbering.get_ref(styles).as_ref() {
        let location = elem.location().unwrap();
        let numbering = Counter::of(HeadingElem::ELEM)
            .display_at(engine, location, styles, numbering, span)?
            .spanned(span);
        realized = numbering + SpaceElem::shared().clone() + realized;
    }

    // HTML's h1 is closer to a title element. There should only be one.
    // Meanwhile, a level 1 Typst heading is a section heading. For this
    // reason, levels are offset by one: A Typst level 1 heading becomes
    // a `<h2>`.
    let level = elem.resolve_level(styles).get();
    Ok(BlockElem::packed(if level >= 6 {
        engine.sink.warn(warning!(
            span,
            "heading of level {} was transformed to \
             <div role=\"heading\" aria-level=\"{}\">, ...",
            level, level + 1;
            hint: "HTML only supports <h1> to <h6>, not <h{}>", level + 1;
            hint: "you may want to restructure your document ...";
        ));
        HtmlElem::new(tag::div)
            .with_body(Some(realized))
            .with_attr(attr::role, "heading")
            .with_attr(attr::aria_level, eco_format!("{}", level + 1))
            .pack()
            .spanned(elem.span())
    } else {
        let t = [tag::h2, tag::h3, tag::h4, tag::h5, tag::h6][level - 1];
        HtmlElem::new(t).with_body(Some(realized)).pack().spanned(elem.span())
    }))
};
```

几个关键细节：

- **编号注入**：若标题有 `numbering`，规则会调用 `Counter::of(HeadingElem).display_at(..)` 计算编号（如 `1.2`），再拼到 body 前面。这是少有的「规则主动做内省查询」的地方——它需要 `engine` 来读计数器当前值。注意 `display_at` 用于读取「在某 location 处」的计数器值，这正是标题所在位置。
- **`resolve_level`**：`elem.resolve_level(styles)`（[crates/typst-library/src/model/heading.rs:240-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L240-L245)）返回 `NonZeroUsize`，`.get()` 转成 `usize`。`resolve_level` 会综合显式 `level`、`offset` 与 `depth`，所以「实际级别」可能与源码里的 `=` 数量不同。
- **越界回退**：当 `level >= 6`，规则发一条 `warning!`（不是 error，编译继续），并退化成 `<div role="heading" aria-level="{level+1}">`。`role="heading"` + `aria-level` 是 WAI-ARIA 的标准做法，让屏幕阅读器仍能识别这是「第 N 级标题」，即使没有对应 HTML 标签。注意即便退化，偏移仍是 `level + 1`（例如 Typst level 6 → `aria-level="7"`）。
- **块级包裹**：无论哪个分支，最外层都是 `BlockElem::packed(..)`，因为标题是块级元素。

#### 4.4.4 代码实践

**实践目标**：用真实编译验证「Typst level N → `<h{N+1}>`」的偏移，并触发 level ≥ 6 的 ARIA 回退。

**操作步骤**：

1. 准备 `headings.typ`（用 `#set document(title: ..)` 设标题以观察 `<h1>`）：
   ```typst
   #set document(title: "我的文档")

   = Level 1
   == Level 2
   === Level 3
   ====== Level 6
   ```
2. 编译：`typst compile --format html headings.typ headings.html`。
3. 打开 `headings.html`，定位各级标题生成的标签。

**需要观察的现象**：

- 文档标题「我的文档」应出现在 `<h1>`（来自 `TITLE_RULE`）。
- `= Level 1` → `<h2>`，`== Level 2` → `<h3>`，`=== Level 3` → `<h4>`。
- `====== Level 6` → 不生成 `<h7>`（不存在），而是 `<div role="heading" aria-level="7">`，且编译器应输出一条警告（`heading of level 6 was transformed to ...`）。

**预期结果**：完全对应上文的映射表，并直观印证「`<h1>` 留给标题、章节标题从 `<h2>` 起」的设计动机。若你没设 `document(title)`，则看不到 `<h1>`，但 level 1 仍是 `<h2>`。

> 标注为「待本地验证」：警告文本与 `<h1>` 是否出现取决于本地是否设置文档标题及 Typst 版本。

#### 4.4.5 小练习与答案

**练习 1**：如果允许 Typst level 1 直接映射 `<h1>`，会带来什么问题？

> **参考答案**：那么文档里会出现多个 `<h1>`（每个 `=` 标题都是），而 `<h1>` 在 HTML 语义里应是「整个文档的唯一主标题」。这会破坏文档大纲（document outline），对 SEO 和屏幕阅读器都不友好——辅助技术常依赖「唯一 `<h1>` + 嵌套子标题」的结构来导航。把 `<h1>` 专属留给 `TitleElem`、章节标题整体下移一级，正好对齐了「Typst 区分文档标题与章节标题」的元素模型。

**练习 2**：为什么 level 6 走 ARIA 回退，而不是直接用 `<h6>`？

> **参考答案**：因为偏移量是 +1，level 6 对应 HTML 逻辑级别 7，而 HTML 只到 `<h6>`。若硬塞 `<h6>` 会丢失「这是第 7 级」的语义信息（与真正的 level 5 → `<h6>` 混淆）。`<div role="heading" aria-level="7">` 虽然不是原生标签，但 `aria-level` 准确表达了层级，是规范推荐的退化方案。注意阈值是 `level >= 6` 而非 `> 6`，正是因为 level 5 已经占用了 `<h6>`（level 5 → 逻辑 6 → `<h6>`）。

---

### 4.5 FOOTNOTE_RULE：脚注与 ARIA 语义角色

#### 4.5.1 概念说明

`FOOTNOTE_RULE` 展示了 show 规则的另一种典型形态：它 **不直接产出 `HtmlElem`**，而是产出一个「由多个元素拼成的序列」，并借助 `HtmlElem::role` 这个内部字段挂载 **ARIA 角色**，让 HTML 输出对辅助技术更友好。

理解脚注规则需要先知道 typst-html 的脚注由两处协同完成：

1. **正文里的引用**（`FOOTNOTE_RULE`）：在脚注出现的位置生成一个上标编号 + 一个不可见的 `FootnoteMarker`。
2. **文档末尾的脚注容器**（`FOOTNOTE_CONTAINER_RULE`）：收集所有脚注，渲染成 `<section role="doc-endnotes">` 下的列表（这一步在 [u3-l2](u3-l2-finalize-dom-skeleton.md) 讲过，由 `finalize_dom` 注入默认容器）。

本节聚焦第 1 处。

#### 4.5.2 核心流程

`FOOTNOTE_RULE`（[src/rules.rs:330-345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L330-L345)）：

```text
输入：FootnoteElem { ... }
   │
   ├─ link = elem.realize(engine, styles)?
   │      ↑ 脚注编号，且是「指向脚注条目的链接」
   │
   ├─ sup = SuperElem(link)
   │      .styled(HtmlElem::role.set(Some("doc-noteref")))
   │      ↑ 包成上标，并标注 ARIA 角色「doc-noteref」（脚注引用）
   │
   ├─ marker = FootnoteMarker::new()
   │      ↑ 不可见标记，用于让容器知道「这里用了默认脚注规则」
   │
   └─ 输出：HElem::hole() + sup + marker
            （一个占位 + 上标编号 + 标记）
```

这里的 ARIA 角色来自 [DPUB-ARIA 1.1](https://www.w3.org/TR/dpub-aria-1.1/) 规范，typst-html 在多处用到：

| ARIA 角色 | 出现位置 | 含义 |
|-----------|----------|------|
| `doc-noteref` | 脚注引用（上标） | 「这是一个指向脚注的引用」 |
| `doc-backlink` | 脚注条目前缀 | 「这是从脚注指回正文引用的回链」 |
| `doc-endnotes` | 脚注容器 `<section>` | 「这是文档的尾注集合」 |
| `doc-toc` | 目录 `<nav>` | 「这是目录」 |
| `doc-bibliography` | 参考文献 `<section>` | 「这是参考文献」 |

#### 4.5.3 源码精读

**FOOTNOTE_RULE**（[src/rules.rs:330-345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L330-L345)）：

```rust
const FOOTNOTE_RULE: ShowFn<FootnoteElem> = |elem, engine, styles| {
    let span = elem.span();

    // The footnote number that links to the footnote entry.
    let link = elem.realize(engine, styles)?;
    let sup = SuperElem::new(link)
        .pack()
        .styled(HtmlElem::role.set(Some("doc-noteref".into())))
        .spanned(span);

    // Indicates the presence of a default footnote rule to emit an error when
    // no footnote container is available.
    let marker = FootnoteMarker::new().pack().spanned(span);

    Ok(HElem::hole().clone() + sup + marker)
};
```

逐行拆解：

- `elem.realize(engine, styles)?` 产出脚注编号，且它本身是一个 **链接**（指向文档末尾的脚注条目）。这是脚注「点击跳转」能力的来源。
- `SuperElem::new(link)` 把这个链接包成上标（`<sup>`），再用 `.styled(HtmlElem::role.set(Some("doc-noteref".into())))` 给它附加 ARIA 角色。注意 `HtmlElem::role` 是 [u1-l4](u1-l4-user-api-html-elem-frame.md) 讲过的 **内部 ghost 字段**：它会被 convert 阶段读取，转成元素上的 `role="..."` 属性，但只作用于「最外层那个 HTML 元素」而非其后代。
- `FootnoteMarker` 是个 **不可见的哨兵元素**，它有自己的规则 `FOOTNOTE_MARKER_RULE`（[src/rules.rs:347](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L347)），返回 `Content::empty()`，即完全不产生输出。它的存在纯粹是为了让 [u3-l2](u3-l2-finalize-dom-skeleton.md) 里提到的 `footnotes_unsupported_with_custom_dom` 能通过内省查询到「用户用了默认脚注规则」——如果用户自定义了 `<html>/<body>` 又用了默认脚注（没有容器），就报错，避免脚注静默丢失。
- `HElem::hole()` 是一个「占位空格元素」，用于让上标在水平方向上有正确的锚定间距。

**配套的脚注条目规则**（[src/rules.rs:408-423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L408-L423)）给条目前的「回链」标注了 `doc-backlink` 角色，注释里还特别说明了 **为什么不用 `doc-footnote`**：

```rust
// We do not use the ARIA role `doc-footnote` because it "is only for
// representing individual notes that occur within the body of a work" (see
// <https://www.w3.org/TR/dpub-aria-1.1/#doc-footnote>). Our footnotes more
// appropriately modelled as ARIA endnotes.
```

也就是说，typst-html 把脚注统一放在文档末尾渲染（像尾注），所以用 endnotes 语义而非 footnote 语义——这是对规范的忠实考量，注释里还提到「与 Pandoc 的处理一致」。

#### 4.5.4 代码实践

**实践目标**：观察脚注引用在 HTML 中的结构，确认 `doc-noteref` 角色与上标链接。

**操作步骤**：

1. 准备 `footnote.typ`：
   ```typst
   正文中有一个脚注#footnote[这是脚注内容]。
   ```
2. 编译：`typst compile --format html footnote.typ footnote.html`。
3. 打开 `footnote.html`，定位脚注引用处的标签，以及文档末尾的脚注容器。

**需要观察的现象**：

- 引用处应出现一个 `<sup>`（上标），其内是一个链接，且 `<sup>` 带有 `role="doc-noteref"`。
- 文档末尾应有一个 `<section role="doc-endnotes">`，里面是 `<ol>`（列表样式被关掉，因为编号已在上标里），每条是 `<li>`。

**预期结果**：与 `FOOTNOTE_RULE` + `FOOTNOTE_CONTAINER_RULE` 的源码对应。这正是 typst-html「把语义忠实映射到 ARIA」的体现。

> 标注为「待本地验证」：`role` 属性的具体位置（在 `<sup>` 还是内层 `<a>` 上）以本地编译输出为准。

#### 4.5.5 小练习与答案

**练习 1**：`FootnoteMarker` 不产生任何 HTML 输出（它的规则返回 `Content::empty()`），那它为什么还要被插入？

> **参考答案**：它是给 **编译器自己** 看的哨兵。typst-html 在「用户自定义 DOM 又用了默认脚注」时需要报错（防止脚注静默丢失，见 u3-l2 的 `footnotes_unsupported_with_custom_dom`）。这个判断靠内省查询 `FootnoteMarker` 是否存在——而 `FootnoteMarker` 正是由默认 `FOOTNOTE_RULE` 插入的。所以它是一个「我用过默认脚注规则」的可内省标记，对 HTML 输出透明。

**练习 2**：为什么脚注引用用 `SuperElem`（上标）而非直接用 `HtmlElem::new(tag::sup)`？

> **参考答案**：因为 show 规则发生在 **realize 阶段**，此时产出的是「Typst 内容层面的元素」而非最终 HTML。`SuperElem` 是 Typst 的语义元素，它能正确参与后续的样式链、内省与 convert；而直接造 `HtmlElem(sup)` 会绕过 Typst 的语义层，丢失上标应有的样式继承。`SuperElem` 自己也有一条 HTML 规则（`SUPER_RULE` → `<sup>`），所以最终输出仍是 `<sup>`，但中间保留了正确的语义。这是「show 规则产出 Typst 元素、由后续规则再翻译成 HTML」的典型分层。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个「阅读 + 实证」综合任务。

**任务背景**：你想给一个静态站点生成器写 Typst 模板，需要确认 typst-html 对结构化内容（标题、强调、列表、脚注）的开箱即用输出，并理解每条输出是哪条内建规则产生的。

**步骤 1｜源码阅读**。打开 [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs)，为下列 4 条规则各写一句话说明「它把什么 Typst 元素映射成什么 HTML 结构、用到了哪些字段」：

- `LIST_RULE`（[L103-L120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L103-L120)）
- `HEADING_RULE`（[L223-L261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L223-L261)）
- `IMAGE_RULE`（[L774-L820](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L774-L820)）
- `EQUATION_RULE`（[L822-L841](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L822-L841)）

**步骤 2｜追踪调用链**。从 [typst/src/lib.rs:312-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L312-L317) 的 `ROUTINES.rules` 出发，画出「编译器启动 → `typst_html::register` → `NativeRuleMap::register` → realize 阶段 `NativeRuleMap::get` + `NativeShowRule::apply`」的完整序列图，并标注「用户 show 规则在哪一步介入、为何能覆盖内建规则」。

**步骤 3｜实证**。写一份覆盖四种元素的 Typst 文档：

```typst
#set document(title: "综合示例")

= 引言
本文说明 *强调* 与 #footnote[一条脚注] 的导出。

== 要点
- 第一项
- 第二项
```

编译为 HTML，逐段核对输出标签，并为每个标签反查它来自 `src/rules.rs` 的哪条规则。重点验证：

- 文档标题 → `<h1>`（`TITLE_RULE`），章节标题 `=` → `<h2>`（`HEADING_RULE` 的偏移）。
- `*强调*` → `<strong>`（`STRONG_RULE`）。
- 脚注引用 → 带 `role="doc-noteref"` 的 `<sup>`（`FOOTNOTE_RULE`），末尾有 `<section role="doc-endnotes">`（`FOOTNOTE_CONTAINER_RULE`）。
- 列表 → `<ul><li>..</li></ul>`（`LIST_RULE`）。

**预期成果**：你应当能够指着任何一段 HTML 输出，准确说出它由哪条 `XXX_RULE` 产生、该规则读取了哪些字段、是否依赖 `engine`/`styles`。这就达成了本讲的目标。若本地无法编译，步骤 1、2 仍可纯靠源码完成，步骤 3 标注为「待本地验证」。

---

## 6. 本讲小结

- `register()`（[rules.rs:39-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L39-L92)）是 typst-html 的装配点，在编译器启动时由 [typst/src/lib.rs:315](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L315) 调用，把按「Model / Text / Visualize / Math」分类的几十条 `XXX_RULE` 注册进共享的 `NativeRuleMap`。
- 每条规则是 `ShowFn<T>` 函数指针（[styles.rs:991-996](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L991-L996)），经 `NativeShowRule::new` 用 `transmute` 类型擦除后，以 `(Element, Target)` 为键存表，调用时由 `apply` 做 `assert` 兜底（[styles.rs:1092-1133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1092-L1133)）。
- 内建规则是 **兜底**：realize 阶段先查用户的 `show` 规则，找不到才用 `(target, elem)` 查内建表（[typst-realize/src/lib.rs:506-513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L506-L513)），这正是用户 `#show strong: ...` 能覆盖默认 `<strong>` 的原因。
- `STRONG_RULE` 是最简形态（包一层标签），`RAW_RULE` 展示了「读样式 + 组装序列 + 按 `block` 分流」的典型形态。
- `HEADING_RULE` 把 Typst level N 映射为 `<h{N+1}>`，因为 `<h1>` 留给文档标题 `TitleElem`；level ≥ 6 走 `<div role="heading" aria-level="..">` 的 ARIA 回退。
- `FOOTNOTE_RULE` 不产出 `HtmlElem` 而是产出 `HElem + SuperElem + FootnoteMarker` 序列，借助 `HtmlElem::role` 挂载 `doc-noteref` 等 DPUB-ARIA 角色；`FootnoteMarker` 是给编译器自己用的可内省哨兵。
- `FrameElem` 是唯一注册在 `Target::Paged`（而非 Html）上的规则，且是 no-op（直接返回 body），目的是防止 `show math.equation: html.frame` 这类写法造成 Frame 无限嵌套。

---

## 7. 下一步学习建议

- **表格与图片导出**：本讲只点了 `TABLE_RULE` 与 `IMAGE_RULE` 的名字，它们的细节（`thead/tbody/tfoot` 划分、`colspan/rowspan`、图片 base64 与 `image-rendering`）是 [u6-l2《表格与图片导出》](u6-l2-table-image-export.md) 的主题。
- **数学公式导出**：`EQUATION_RULE` 调用了 `convert_math_to_nodes` 把方程转成 MathML，这条链路在 [u5-l5《数学公式到 MathML 的转换》](u5-l5-math-to-mathml.md) 展开。
- **Display 与块级提升**：本讲多次出现 `BlockElem::packed`，它是「标记块级」的手段，其背后的 display 系统在 [u4-l2《display 属性与块级/行内提升》](u4-l2-display-block-inline-promotion.md) 详解。
- **继续阅读源码**：建议通读一遍 [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs) 全文，把每条规则补进你的「元素 → HTML」对照表；遇到 `OutlineElem`、`BibliographyElem`、`QuoteElem` 等复杂规则时，留意它们如何复用 `HtmlElem::role`、`with_css` 和内省查询，这些是 show 规则的进阶写法。
