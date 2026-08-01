# Target 与 Output 多目标抽象

## 1. 本讲目标

本讲要回答一个核心问题：**同一个 `compile` 函数，为什么能既产出分页文档（PDF/PNG/SVG 之源）、又产出 HTML、还能产出「多文件 bundle」？**

读完本讲，你应当能够：

- 说清 `Target` 枚举（`Paged` / `Html` / `Bundle`）与 `Output` trait 之间严格的「一一对应」关系。
- 跟着 `compile<T: Output>` 的代码路径，解释它如何靠 `T::target()` 做**特性门控**（feature gating）、靠 `T::create()` 产出不同产物。
- 理解 `TargetElem` 如何把「编译目标」当作一个**样式字段**注入样式系统，并被下游的 realize / layout 读取。
- 看懂 `target()` 这个 contextual（上下文相关）函数为何能在**同一次编译内部**返回不同的值（例如 HTML 导出时，`html.frame` 内部会回到 `paged`）。
- 明白 `AsOutput` trait 解决了什么 Rust 类型擦除的小麻烦。

## 2. 前置知识

本讲承接 **u1-l3「调用 compile() 完成一次编译」**。那里我们已知：

- `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>> where T: Output` —— 泛型 `T` 决定产物类型，调用时用 turbofish `compile::<PagedDocument>(&world)` 指明。
- `Output` 是一个 trait，当时只点到「它有 `target()` / `create()` 等方法」，没展开。
- 返回值是 `Warned<SourceResult<T>>`：外层捆绑「产物 + 告警」，内层是成功或一批错误。

本讲就专门把 `Output` 和它的搭档 `Target` 讲透。如果下面出现不熟悉的词，我们会随文解释。

> 几个名词速查：
> - **门面（facade）**：`typst` crate 本身只做「装配 + 编排」，真正的活儿在 `typst-layout` / `typst-html` / `typst-bundle` 等子 crate。
> - **样式链（StyleChain）**：Typst 把「样式」组织成一条可层层覆盖的链，元素取值时沿链查找。
> - **realize（具现）**：把抽象 content 节点一步步变成可布局的具体元素的过程，期间会查找 show 规则。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-library/src/foundations/target.rs` | 定义 `Output` trait、`AsOutput` trait、`Target` 枚举、`TargetElem` 元素、`target()` 函数。**本讲的主战场。** |
| `crates/typst/src/lib.rs` | `compile_impl` 里读取 `T::target()` 做门控、把目标写入 `TargetElem` 样式、调用 `T::create`，以及 `warn_or_error_for_html/bundle` 两个门控函数。 |
| `crates/typst-layout/src/document.rs` | `impl Output for PagedDocument`（分页产物的实现）。 |
| `crates/typst-html/src/dom.rs` | `impl Output for HtmlDocument`（HTML 产物的实现）。 |
| `crates/typst-bundle/src/lib.rs` | `impl Output for Bundle`（多文件产物的实现）。 |
| `crates/typst-library/src/lib.rs` | `Feature` 枚举与 `Features` 集合、`is_enabled` 判定，以及 `global()` 中按特性条件注册 `html` 模块。 |
| `crates/typst-realize/src/lib.rs` | realize 时读取 `TargetElem::target` 来挑选内置 show 规则——目标如何影响「内容怎么画」。 |
| `crates/typst-html/src/convert.rs` | HTML 导出时，遇到 `html.frame` 会把目标**重置**回 `Paged`，体现「同一次编译内目标可变」。 |

## 4. 核心概念与源码讲解

### 4.1 Target 枚举：编译目标的三种归宿

#### 4.1.1 概念说明

「编译目标（target）」回答的是：**这次编译最终想要哪种形态的产物？** Typst 目前定义了三种：

- `Paged`：分页、完全布局好的内容。这是默认目标，PDF / PNG / SVG 导出都基于它。
- `Html`：HTML 导出。
- `Bundle`：多文件导出，能从**一个** Typst 工程里产出**多个**文档和资源文件（assets）。

`Target` 是一个普通的「标记枚举」——它本身不带数据，只用来区分「我们在哪种世界」。它的派生属性里藏着两个关键设计：

- `#[default]` 标在 `Paged` 上，说明不指定时默认走分页。
- `Cast` 让它能和 Typst 脚本里的字符串互相转换（`"paged"` / `"html"` / `"bundle"`），这是 `target()` 函数能返回它的前提。

#### 4.1.2 核心流程

`Target` 在编译流程里扮演「分叉开关」的角色：

1. `compile::<T>` 被调用时，`T::target()` 给出一个 `Target`。
2. 这个 `Target` 在两个层面被使用：
   - **门控层**：决定要不要对实验性功能发警告或报错（见 4.2）。
   - **样式层**：被写进 `TargetElem::target` 字段，沿样式链向下传递给所有元素（见 4.3）。
3. 下游代码（realize、layout、html 转换）按需读取这个样式字段，做出分支选择。

```text
T::target()  ──►  门控（warn_or_error_for_*）
            ──►  TargetElem::target 样式  ──►  realize 选规则 / layout 分支
```

#### 4.1.3 源码精读

枚举定义在 `target.rs`：

> [crates/typst-library/src/foundations/target.rs:66-76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L66-L76) —— `Target` 枚举，`Paged` 带 `#[default]`，三者各自对应一种导出形态。

```rust
#[derive(Debug, Default, Copy, Clone, Eq, PartialEq, Hash, Cast)]
pub enum Target {
    /// The target that is used for paged, fully laid-out content.
    #[default]
    Paged,
    /// The target that is used for HTML export.
    Html,
    /// The target for _bundle_ export. This export target can produce multiple
    /// documents and assets from a single Typst project.
    Bundle,
}
```

枚举上还挂了一个小工具方法 `is_html`，方便下游用 `target.is_html()` 写出更可读的判断：

> [crates/typst-library/src/foundations/target.rs:78-83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L78-L83) —— `Target::is_html`，比如数学公式布局里就用它来决定是否走 HTML 分支（见后文 `resolve.rs`）。

```rust
impl Target {
    /// Whether this is the HTML target.
    pub fn is_html(self) -> bool {
        self == Self::Html
    }
}
```

注意 `Target` 自身**不携带任何产物**，它只是个「标签」。真正产出文档的是下一节的 `Output` trait。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：体会 `Target` 在代码库里被「读取」的地方有多少、各自为了什么。
2. **步骤**：在仓库根目录运行 `grep -rn "TargetElem::target" crates/ --include=*.rs`（排除 tutorial 目录），把命中点分类成两类——「写（`.set(...)`）」和「读（`.get(...)` / `styles.get(...)`）」。
3. **观察**：你会发现写入点很少（`compile_impl`、bundle、measure、html 转换），读取点很多（realize、math resolve、raw 高亮等）。
4. **预期结果**：写出「写少读多」的结论——目标是一个**全局开关**，少数地方设定它，大量地方依据它分支。这正是一个标记枚举的典型用法。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `Target::Paged` 要标 `#[default]`？如果不标会怎样？
  - **答**：因为绝大多数编译（PDF/PNG/SVG）都走分页，把默认设成 `Paged` 让「不显式指定目标」等价于「分页」。不标的话 `Target` 就没有默认值，凡是用到 `Target::default()` 的地方都编译不过。
- **Q2**：`Bundle` 和 `Paged`/`Html` 的本质区别是什么？
  - **答**：`Paged`/`Html` 各产出**一个**文档；`Bundle` 能从一个工程产出**多个**文档和资源文件（assets），它内部还会再嵌套地布局 paged 或 html 文档。

---

### 4.2 Output trait：把「产物类型」抽象成接口

#### 4.2.1 概念说明

`Output` 是「**一种编译产物**」的抽象。它的文档注释直白地点出核心约束：

> Has a 1-1 relationship with the variants of `Target`.
> （与 `Target` 的各个变体一一对应。）

也就是说：**每有一个 `Target` 变体，就恰好有一个实现了 `Output` 的类型。** 目前这「三对」是：

| `Target` | 实现 `Output` 的类型 | 定义位置 | `create()` 委托给 |
|----------|---------------------|----------|-------------------|
| `Paged` | `PagedDocument` | `typst-layout` | `layout_document` |
| `Html` | `HtmlDocument` | `typst-html` | `html_document` |
| `Bundle` | `Bundle` | `typst-bundle` | `bundle` |

`Output` trait 只有三个方法：

- `target()`：返回这个产物对应的 `Target`（与上面表格呼应）。
- `create(...)`：**真正干活的那个**——接收求值后的 `content` 与样式，布局/转换出产物自身。
- `introspector(&self)`：返回这个产物的内省器（introspector），供稳定化循环判定收敛（详见 u2-l2）。

为什么要把产物抽象成 trait？因为 `compile` 想用**同一份代码**服务多种产物。泛型 `T: Output` 让编译器在编译期就知道该调哪个 `create`、该填哪个 `target`，零运行时开销。

#### 4.2.2 核心流程

`compile_impl` 里和 `Output` 直接相关的两处调用，串起了「门控」和「产出」两件事：

```text
① 门控：match T::target() { Paged => {}, Html => warn_or_error_for_html, Bundle => warn_or_error_for_bundle }
② 产出：在稳定化循环每轮调用 T::create(&mut engine, &content, styles) 得到 document
③ 收敛：用 document.introspector()（即 T::introspector 的间接调用）判定是否稳定
```

注意第①步里 `Paged` 是「空臂」`=> {}`——分页是默认且稳定的目标，不需要任何特性门控；而 `Html` / `Bundle` 都还是**实验性功能**，需要根据特性开关决定是发警告还是直接报错。

#### 4.2.3 源码精读

先看 `Output` trait 本身：

> [crates/typst-library/src/foundations/target.rs:10-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L10-L30) —— `Output` trait，三方法 `target/create/introspector`，注释强调与 `Target` 一一对应。

```rust
/// A compilation output for a particular target.
///
/// Has a 1-1 relationship with the variants of [`Target`].
pub trait Output: Any {
    fn target() -> Target where Self: Sized;
    fn create(engine: &mut Engine, content: &Content, styles: StyleChain) -> SourceResult<Self> where Self: Sized;
    fn introspector(&self) -> &dyn Introspector;
}
```

再看三处实现，验证「一一对应」：

> [crates/typst-layout/src/document.rs:63-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/document.rs#L63-L79) —— `impl Output for PagedDocument`：`target()` 返回 `Target::Paged`，`create()` 委托给 `crate::layout_document`。

```rust
impl Output for PagedDocument {
    fn introspector(&self) -> &dyn Introspector { self.introspector.as_ref() }
    fn target() -> Target { Target::Paged }
    fn create(engine: &mut Engine, content: &Content, styles: StyleChain) -> SourceResult<Self> {
        crate::layout_document(engine, content, styles)
    }
}
```

> [crates/typst-html/src/dom.rs:81-97](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/dom.rs#L81-L97) —— `impl Output for HtmlDocument`：`target()` 返回 `Target::Html`，`create()` 委托给 `crate::html_document`。

> [crates/typst-bundle/src/lib.rs:56-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L56-L72) —— `impl Output for Bundle`：`target()` 返回 `Target::Bundle`，`create()` 委托给同文件的 `bundle` 函数。

> 注意：三处 `impl` 里方法书写的顺序（introspector/target/create）与 trait 声明顺序（target/create/introspector）不同，Rust 不要求顺序一致，这只是各 crate 的风格差异。

现在看 `compile_impl` 如何用 `T::target()` 做门控：

> [crates/typst/src/lib.rs:104-109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104-L109) —— 根据 `T::target()` 分流：`Paged` 空臂，`Html`/`Bundle` 走特性门控。

```rust
let library = world.library();
match T::target() {
    Target::Paged => {}
    Target::Html => warn_or_error_for_html(&library.features, sink)?,
    Target::Bundle => warn_or_error_for_bundle(&library.features, sink)?,
}
```

两个门控函数长得几乎一样：开了对应特性就发一条警告（仍继续编译），没开就直接 `bail!`（致命错误）。

> [crates/typst/src/lib.rs:247-266](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L266) —— `warn_or_error_for_html`：`Feature::Html` 开启则 `sink.warn(...)`，否则 `bail!(...)`。

```rust
fn warn_or_error_for_html(features: &Features, sink: &mut Sink) -> SourceResult<()> {
    const ISSUE: &str = "https://github.com/typst/typst/issues/5512";
    if features.is_enabled(Feature::Html) {
        sink.warn(warning!( /* "html export is under active development ..." */ ));
    } else {
        bail!(Span::detached(), "html export is only available when `--features html` is passed"; /* hints */);
    }
    Ok(())
}
```

> [crates/typst/src/lib.rs:270-286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L270-L286) —— `warn_or_error_for_bundle`：同样依赖 `Feature::Bundle` 是否开启。

特性开关本身定义在 `typst-library`：

> [crates/typst-library/src/lib.rs:270-284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L270-L284) —— `Feature` 枚举（`Html` / `Bundle` / `A11yExtras`，标记 `#[non_exhaustive]` 预留扩展）。

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
#[non_exhaustive]
pub enum Feature {
    Html,
    Bundle,
    A11yExtras,
}
```

> [crates/typst-library/src/lib.rs:240-258](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L240-L258) —— `Features(SmallBitSet)` 集合与 `is_enabled` 判定。

最后看「产出」那一步：

> [crates/typst/src/lib.rs:156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L156) —— 稳定化循环每轮调用 `T::create(&mut engine, &content, styles)`，泛型 `T` 在编译期决定走 `layout_document` / `html_document` / `bundle` 中的哪一个。

```rust
document = T::create(&mut engine, &content, styles)?;
```

> 提示：`Bundle` 内部并不只是「单种产物」。它在 `create` 里会根据每个 `document` 元素声明的格式，再次设置目标（paged 或 html）并嵌套布局，甚至要求 `Feature::Html` 也开启才能产出 HTML 子文档（见 [crates/typst-bundle/src/lib.rs:322-329](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L322-L329)）。这是「目标可以嵌套」的体现。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：亲手验证 `Target` 与 `Output` 的「一一对应」。
2. **步骤**：打开三处 `impl Output`（本节给出的三处链接），各抄下 `target()` 的返回值与 `create()` 的函数体。
3. **观察**：确认每处的 `target()` 返回值都不同，且 `create()` 分别委托给不同 crate 的入口函数。
4. **预期结果**：得到一张「`T::target()` → `T::create()` 实现」的映射表（本节 4.2.1 的表格即为答案）。

#### 4.2.5 小练习与答案

- **Q1**：假如将来要新增一个 `Target::Epub`，至少要改动哪几处？
  - **答**：至少四处——① `Target` 枚举加变体；② 新写一个类型 `impl Output`，其 `target()` 返回 `Target::Epub`；③ `compile_impl` 的 `match` 加一条门控分支（或空臂）；④ 视需要新增 `Feature::Epub` 与对应的 `warn_or_error_for_epub`。
- **Q2**：为什么门控用 `match T::target()` 而不是 `if T::target() == Target::Html`？
  - **答**：`match` 对枚举是穷尽的，将来给 `Target` 加变体时，编译器会强制提醒这里也要处理新分支，避免遗漏门控。

---

### 4.3 TargetElem：把目标注入样式系统

#### 4.3.1 概念说明

光有 `Target` 枚举还不够——`T::target()` 只在 `compile_impl` 顶层用一次。但**下游成千上万的元素**（段落、图片、数学公式、代码块……）都需要知道「现在编译到哪种目标」才能正确分支。怎么把这个信息传下去？

Typst 的答案是：**把目标塞进样式系统。** `TargetElem` 是一个特殊的元素，它存在的**唯一目的**就是承载一个名为 `target` 的样式字段。它本身从不被构造，用户也看不见它——它纯粹是「目标信息」在样式链里的载体。

这样，任何元素在布局时只要从自己的样式链里读 `TargetElem::target`，就能知道当前目标，无需额外的参数传递。

#### 4.3.2 核心流程

```text
compile_impl:
  base = StyleChain::new(&library.styles)          // 标准库自带的基础样式
  target_style = TargetElem::target.set(T::target())  // 把目标包成一条样式
  styles = base.chain(&target_style)                // 拼到基础样式链上
  ──► styles 随 content 一路向下传 ──►
        realize: styles.get(TargetElem::target) 选内置 show 规则
        math:   styles.get(TargetElem::target).is_html() 决定公式怎么画
        raw:    styles.get(TargetElem::target) 决定代码块高亮方式
```

#### 4.3.3 源码精读

先看 `compile_impl` 如何写入这个字段（这是「写少」中最核心的一处）：

> [crates/typst/src/lib.rs:111-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L111-L113) —— 把 `T::target()` 包成 `TargetElem::target` 样式，链接到基础样式链 `base` 之上。

```rust
let base = StyleChain::new(&library.styles);
let target = TargetElem::target.set(T::target()).wrap();
let styles = base.chain(&target);
```

`TargetElem` 的定义极简，注释明说了它「只为承载字段、从不构造、对用户不可见」：

> [crates/typst-library/src/foundations/target.rs:85-91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L85-L91) —— `TargetElem`，只有一个 `target: Target` 字段。

```rust
/// This element exists solely to host the `target` style chain field. It is
/// never constructed and not visible to users.
#[elem]
pub struct TargetElem {
    /// The compilation target.
    pub target: Target,
}
```

再看「读多」里的典型代表——realize 引擎在找不到用户自定义 show 规则时，会读目标来挑选**内置** show 规则：

> [crates/typst-realize/src/lib.rs:507-512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L507-L512) —— `let target = styles.get(TargetElem::target);` 然后用它到规则表里查 `engine.library.rules.get(target, elem)`。

```rust
if step.is_none() {
    let target = styles.get(TargetElem::target);
    if let Some(rule) = engine.library.rules.get(target, elem) {
        step = Some(ShowStep::Builtin(rule));
    }
}
```

正因为 `rules` 表是**按 target 分桶**注册的（`typst-layout` 注册 paged 规则、`typst-html` 注册 html 规则），同一个元素在不同目标下会具现成完全不同的东西。这就是「目标」影响内容呈现的核心机制。

再举一个读取例子——数学公式的解析里用 `is_html()` 分流：

> [crates/typst-library/src/math/ir/resolve.rs:244-248](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L244-L248) —— `if styles.get(TargetElem::target).is_html() { ... }`，HTML 目标下数学走一项、其它目标走另一项。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解 `TargetElem` 如何让「目标」渗透到具体元素的分支。
2. **步骤**：阅读上面 realize 那段（`typst-realize/src/lib.rs:507-512`），再打开 `crates/typst-layout/src/rules.rs` 和 `crates/typst-html` 里注册规则的地方（可用 `grep -n "fn register" crates/typst-layout/src/rules.rs crates/typst-html/src/`）。
3. **观察**：两个 crate 各自把「元素 → show 规则」注册进同一张 `rules` 表，但分别挂在 `Target::Paged` 和 `Target::Html` 下。
4. **预期结果**：能用一句话说清「同一个 heading 元素，为什么在 paged 下变成带分页布局的块、在 html 下变成 `<h1>` 标签」——因为 realize 按 `TargetElem::target` 选了不同的内置规则。

#### 4.3.5 小练习与答案

- **Q1**：`TargetElem` 既然「从不被构造」，那它的字段值是怎么出现的？
  - **答**：通过样式链注入。`compile_impl` 用 `TargetElem::target.set(...)` 生成一条**样式**（不是元素实例），这条样式携带了目标值，链到基础样式上后被下游用 `.get(TargetElem::target)` 读出。Typst 的样式系统允许「设置某元素的字段」而不必「构造该元素」。
- **Q2**：为什么把目标放进样式系统，而不是作为参数层层传递？
  - **答**：样式链是 Typst 已有的、随 content 自动向下流动的机制；复用它能让任何深度的元素零成本读到目标，而无需给所有布局函数加一个 `target` 参数，保持调用链干净。

---

### 4.4 `target()` contextual 函数：让 Typst 代码「看见」目标

#### 4.4.1 概念说明

前面三节讲的都是**编译器内部**如何用 `Target`。本节讲的是**用户写的 Typst 脚本**如何查询当前目标——靠的就是内置函数 `target()`。

`target()` 标注了 `contextual`，意思是它的返回值**依赖上下文**（样式链）。它的实现极其简单：从当前样式里读 `TargetElem::target` 然后返回。但因为样式链可以**局部覆盖**，`target()` 在同一次编译里**不同位置可能返回不同值**——这正是它最有用的特性。

文档给出的典型用途是在模板/show 规则里写「如果是 HTML 就输出 `<kbd>` 标签，否则画一个带圆角底色的盒子」的键盘按键样式，让同一份源码适配多种导出。

#### 4.4.2 核心流程

`target()` 最反直觉的一点：**在一次 HTML 编译里，它并非处处返回 `"html"`。** 当 HTML 转换器遇到一个 `html.frame`（要嵌入为 SVG 的分页内容）时，会**临时把目标重置回 `Paged`** 来布局那块内容。所以在 `html.frame` 内部，`target()` 返回 `"paged"`。

```text
HTML 编译整体：Target::Html
  ├─ 普通文本/元素：target() = "html"
  └─ html.frame（嵌入分页内容）：
       临时 TargetElem::target.set(Target::Paged)  ──►  内部 target() = "paged"
```

#### 4.4.3 源码精读

`target()` 函数本体只有一行：

> [crates/typst-library/src/foundations/target.rs:134-137](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L134-L137) —— `target()` 函数，标注 `#[func(contextual, since = "0.13.0")]`，从当前样式读 `TargetElem::target`。

```rust
#[func(contextual, since = "0.13.0")]
pub fn target(context: Tracked<Context>) -> HintedStrResult<Target> {
    Ok(context.styles()?.get(TargetElem::target))
}
```

它的文档注释（93–133 行）清楚地列出了三种返回值及其适用场景，并专门用一节 *Varying targets* 强调「目标可变」。

> [crates/typst-library/src/foundations/target.rs:108-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L108-L112) —— 文档原文：This function is contextual as the target can vary within a single compilation: When exporting to HTML, the target will be `{"paged"}` while within an `html.frame`.

那「重置回 Paged」发生在哪？就在 `typst-html` 把 `html.frame` 布局成 frame 的地方：

> [crates/typst-html/src/convert.rs:142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/convert.rs#L142) —— 遇到 `FrameElem`（即 `html.frame`）时，`let style = TargetElem::target.set(Target::Paged).wrap();` 把局部样式重置为分页，再调用 `layout_frame` 把它当分页内容布局。

```rust
} else if let Some(elem) = child.to_packed::<FrameElem>() {
    let locator = converter.locator.next(&elem.span());
    let style = TargetElem::target.set(Target::Paged).wrap();
    let frame = (converter.engine.library.routines.layout_frame)( /* ... */ );
```

同样的「强制 Paged」手法还出现在 `measure()`（测量内容尺寸时必然按分页布局）：

> [crates/typst-library/src/layout/measure.rs:94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/measure.rs#L94) —— `let style = TargetElem::target.set(Target::Paged).wrap();`，测量永远以分页语义进行。

#### 4.4.4 代码实践（动手观察型）

1. **目标**：亲眼看到 `target()` 在「同一次编译」里返回不同值。
2. **步骤**：写一个最小的 Typst 文件 `target.typ`：

   ```typst
   #context [整篇目标：#target()]

   #html.frame(
     context [frame 内目标：#target()]
   )
   ```
   然后分别用 `typst compile --format html target.typ`（HTML 导出）和普通 `typst compile target.typ`（paged 导出）查看 context 块输出的字符串。
3. **观察**：HTML 导出时，第一处应是 `"html"`，第二处（frame 内）应是 `"paged"`；paged 导出时两处都是 `"paged"`。
4. **预期结果**：**待本地验证**（具体命令名与开关请以你本地 `typst` 版本为准；当前 HEAD 下 HTML/Bundle 仍属实验特性，需 `--features html` 编译出的 CLI 才支持）。即便不能运行，阅读 4.4.3 两处源码也应能推断出上述结论。

#### 4.4.5 小练习与答案

- **Q1**：为什么 `target()` 必须是 `contextual` 而不是普通纯函数？
  - **答**：因为它的返回值依赖当前样式链（`context.styles()?.get(TargetElem::target)`），而样式链是「位置相关」的——同一文档不同位置样式可能不同。普通纯函数拿不到 context，自然无法实现。
- **Q2**：`measure()` 为什么要强制把目标设成 `Paged`？
  - **答**：测量是为了得到内容的几何尺寸，而 Typst 的几何度量体系建立在分页布局之上；即便身处 HTML 编译，测量也必须按分页语义进行，所以临时把局部目标重置为 `Paged`。

---

### 4.5 AsOutput trait：何时需要它

#### 4.5.1 概念说明

`AsOutput` 是个「小而精」的辅助 trait，专为**接受「任意一种产物」作为参数**的函数服务。它的全部内容就是一个方法 `as_output(&self) -> &dyn Output`。

它存在的理由写在注释里：Rust 里在泛型函数中**没法把 `&impl Output` 强转成 `&dyn Output`**（trait object）。如果你写一个函数想同时接受「具体产物类型的引用」和「`&dyn Output` trait 对象」，直接用 `&dyn Output` 又不太方便（尤其当文档可能为空时）。于是 Typst 定义 `AsOutput`，给「具体类型引用」和「trait 对象引用」都实现它，统一入口。

#### 4.5.2 核心流程

```text
你的函数签名：fn f(document: impl AsOutput)
  调用方传 &PagedDocument    ──► 命中 impl<T: Output> AsOutput for &T
  调用方传 &dyn Output       ──► 命中 impl AsOutput for &dyn Output
函数体内：document.as_output()  ──► 统一拿到 &dyn Output
```

#### 4.5.3 源码精读

> [crates/typst-library/src/foundations/target.rs:32-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L32-L63) —— `AsOutput` trait 及其两处实现，注释解释了为何需要它。

```rust
pub trait AsOutput {
    fn as_output(&self) -> &dyn Output;
}

impl AsOutput for &dyn Output {
    fn as_output(&self) -> &dyn Output { *self }
}

impl<T: Output> AsOutput for &T {
    fn as_output(&self) -> &dyn Output { *self }
}
```

注释还特别提醒：**应写成 `impl AsOutput` 而非 `&impl AsOutput`**，并且推荐阅读 Rust 论坛上那条关于「泛型 unsized 参数转 trait object」的讨论链接——那正是这个设计要绕开的语言限制。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：确认 `AsOutput` 是「给函数参数用的适配层」。
2. **步骤**：在仓库里搜索 `AsOutput` 的使用点：`grep -rn "AsOutput" crates/ --include=*.rs`（排除 tutorial）。
3. **观察**：看哪些**函数签名**用 `impl AsOutput` 接收文档，这些函数往往是要对「任意一种产物」做通用处理（如导出/打印）的地方。
4. **预期结果**：理解 `AsOutput` 不是编译主流程的必经之路，而是**对外 API 的便利层**——让导出器之类的函数既能吃具体类型、也能吃 trait 对象。

#### 4.5.5 小练习与答案

- **Q1**：为什么不直接让函数参数写成 `&dyn Output`？
  - **答**：可以，但不方便。尤其当文档可能不存在（`Option`）或调用方手里只有具体类型时，强行构造 `&dyn Output` 很啰嗦。`AsOutput` 用两个 blanket impl 把「具体类型引用」和「trait 对象引用」都接住，调用更顺滑。
- **Q2**：`impl<T: Output> AsOutput for &T` 这一行里的 `&T` 为什么能自动转成 `&dyn Output`？
  - **答**：因为 `T: Output` 且 `Output: Any`（即 `T` 是 sized 且实现 trait），在**非泛型**的具体函数体内（`as_output` 方法体）把 `&T` 转成 `&dyn Output` 是允许的；问题只出在「泛型函数签名处直接对 `&impl Output` 做强转」，`AsOutput` 用一个中间方法绕开了它。

---

## 5. 综合实践

把本讲所有模块串起来，完成下面这张**「三目标对照表」**（这正是本讲对应的实践任务）。请你**先自己填**，再对照答案核对。

**任务**：为 `PagedDocument`、`HtmlDocument`、`Bundle` 三种产物，分别填出：

1. 对应的 `Target` 变体；
2. `Output` 实现所在的文件 + `create()` 委托给的函数；
3. `compile_impl` 里 `match T::target()` 走的是哪条分支（空臂 / `warn_or_error_for_html` / `warn_or_error_for_bundle`）；
4. 该目标是否依赖某个 `Feature` 开关，开关关闭时是「警告」还是「报错」。

**参考答案表**：

| 产物类型 | `T::target()` | `impl Output` 位置 / `create()` 委托 | `compile_impl` 分支 | Feature 依赖 | 开关关闭时 |
|----------|---------------|--------------------------------------|---------------------|-------------|-----------|
| `PagedDocument` | `Target::Paged` | `typst-layout/src/document.rs:63-79` / `layout_document` | `Target::Paged => {}`（空臂） | 无 | — |
| `HtmlDocument` | `Target::Html` | `typst-html/src/dom.rs:81-97` / `html_document` | `warn_or_error_for_html` | `Feature::Html` | **报错**（`bail!`） |
| `Bundle` | `Target::Bundle` | `typst-bundle/src/lib.rs:56-72` / `bundle` | `warn_or_error_for_bundle` | `Feature::Bundle` | **报错**（`bail!`） |

**延伸思考（可选）**：

- 当 `Feature::Html` **开启**时，`warn_or_error_for_html` 只发一条 `sink.warn(...)` 而不报错，编译仍继续——这说明「实验特性开了就用、但要警告用户它不稳定」。
- `Bundle` 内部若想产出 HTML 子文档，还需 `Feature::Html` 也开启（见 [crates/typst-bundle/src/lib.rs:322-329](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L322-L329)）——即「bundle + html」要同时开两个特性。

如果你能在不看本讲的前提下，对着 `compile_impl` 的 `match` 与三处 `impl Output` 默写出上表，本讲就过关了。

## 6. 本讲小结

- `Target`（`Paged`/`Html`/`Bundle`，默认 `Paged`）是「编译目标」标记枚举，与 `Output` 的实现类型**一一对应**。
- `Output` trait 三方法 `target()`/`create()`/`introspector()` 把「产物类型」抽象成接口，使同一份 `compile<T: Output>` 能服务多种产物，零运行时开销。
- `compile_impl` 用 `match T::target()` 做**特性门控**：`Paged` 空臂，`Html`/`Bundle` 走 `warn_or_error_for_*`（特性开则警告、关则报错）。
- `TargetElem` 是只为承载 `target` 样式字段而存在的「隐形元素」；`compile_impl` 把 `T::target()` 写入它，下游（realize / math / raw 等）通过 `styles.get(TargetElem::target)` 读取并分支。
- contextual 函数 `target()` 让 Typst 脚本查询当前目标；由于样式可局部覆盖，**同一次编译内目标可变**——HTML 导出时 `html.frame` 内会临时回到 `paged`（`measure()` 同理）。
- `AsOutput` 是为「接受任意产物作参数」的函数准备的适配 trait，绕开 Rust「泛型处无法强转 trait object」的限制。

## 7. 下一步学习建议

- **沿着「目标如何影响具现」继续走**：阅读 `crates/typst-realize/src/lib.rs` 里读取 `TargetElem::target` 的整段逻辑，以及 `typst-layout/src/rules.rs` 和 `typst-html` 的 `register`，看 paged/html 各自注册了哪些内置 show 规则。
- **回到主流程**：本讲聚焦「目标分流」，但 `compile_impl` 里 `T::create` 处于**稳定化循环**之中。若你还没读，接下来应学 **u2-l2「内省稳定化循环」** 与 **u2-l3「内省记录与非收敛检测」**，理解为什么 `T::create` 要被反复调用、`introspector()` 如何参与收敛判定。
- **看装配**：若对「三处 `impl Output` 分散在不同 crate、却被同一个 `compile` 调度」的工程手法感兴趣，可学 **u3-l2「ROUTINES 表与 crate 切分」**，那是更底层的「动态链接」机制。
