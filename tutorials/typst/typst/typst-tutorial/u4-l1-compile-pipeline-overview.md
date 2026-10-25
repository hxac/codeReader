# 编译主流程总览：五个阶段的衔接

## 1. 本讲目标

本讲是第四单元（排版管线）的第一讲，我们从「高空俯瞰」整个编译器，把前面三个单元学过的零散知识串成一条完整的流水线。读完本讲，你应该能够：

- 说出 parse → eval → realize → layout → export 五个阶段各自的**输入类型、输出类型和负责的 crate**；
- 精读 `typst::compile` 与 `compile_impl` 的源码，解释为什么 **eval 只执行一次**，而 **layout 要在循环中反复执行直到收敛**（上限 5 轮）；
- 会用 typst-timing 体系（CLI 的 `--timings` 参数）采集 Chrome Tracing 格式的耗时文件，并用脚本聚合出各阶段的耗时柱状图。

## 2. 前置知识

本讲默认你已完成第一、三单元，以下概念会直接使用：

- **World 抽象**（u3-l1）：宿主环境通过 `World` trait 向编译器提供源文件、字体、标准库 `Library` 等输入，并负责缓存。编译器入口 `compile` 的第一个参数就是 `&dyn World`。
- **crate 分层与 Routines 接线**（u1-l3）：类型与 trait 定义在 `typst-library`，各阶段的行为拆在独立 crate（`typst-eval`、`typst-realize`、`typst-layout`……），总装 crate `typst` 用一张 `ROUTINES` 函数指针表把它们「焊接」起来——源码注释称之为 essentially dynamic linking（本质上是动态链接）。
- **Content 与 Styles**（u3-l4、u3-l7）：eval 的产物是一棵带样式的元素树 `Content`；set 规则求值期立即生效，show 规则延迟到 realize 阶段执行。
- **增量编译**（u1-l3、u2-l5）：comemo 按输入哈希记忆化函数结果，`Source::edit` 只重解析局部子树。

本讲新增三个术语，先用一段话解释：

- **流水线（pipeline）**：把一个大任务拆成一串小阶段，每个阶段只做一种变换，前一阶段的输出是后一阶段的输入。好处是职责单一、易于测试和增量缓存。
- **不动点迭代**：当一个阶段的输出会反过来依赖它自己的结果时（比如页码），就反复执行这个阶段，直到连续两次结果一致——结果「不再变化」的点叫不动点。Typst 的布局阶段就是这样收敛的。
- **RAII 计时**：在作用域开始时创建一个对象、对象销毁（离开作用域）时自动记录结束时间。Rust 的确定性析构让这种方式天然配对「开始/结束」事件，typst-timing 的 `TimingScope` 就是这么做的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst/src/lib.rs` | 总装 crate：`compile` 公开入口、`compile_impl` 主流程、内省收敛循环、`ROUTINES` 接线表 |
| `docs/dev/architecture.md` | 官方架构文档：四阶段总述、各阶段与增量编译的关系 |
| `crates/typst-library/src/foundations/target.rs` | `Output` trait 与 `Target` 枚举：编译产物的抽象（Paged / Html / Bundle） |
| `crates/typst-layout/src/document.rs` | `PagedDocument`/`Page` 结构，以及 `impl Output for PagedDocument` |
| `crates/typst-layout/src/pages/mod.rs` | `layout_document` 入口：先 realize 再分页布局 |
| `crates/typst-library/src/introspection/convergence.rs` | `MAX_ITERS`、`ITER_NAMES` 常量与 `analyze` 非收敛诊断 |
| `crates/typst-timing/src/lib.rs` | `timed!` 宏、`TimingScope`、全局开关与 Chrome Tracing 导出 |
| `crates/typst-macros/src/time.rs` | `time` 过程宏：把函数自动包进计时作用域 |
| `crates/typst-kit/src/timer.rs` | CLI 侧的 `Timer`：启用计时并把事件写成 JSON 文件 |
| `crates/typst-cli/src/args.rs`、`crates/typst-cli/src/compile.rs` | `--timings` 参数定义与 compile 子命令主干 |

## 4. 核心概念与源码讲解

### 4.1 五阶段数据流：从字符串到字节输出

#### 4.1.1 概念说明

一份 `.typ` 文件变成一份 PDF，中间要经过五次「数据形态」的变换。总装 crate 的模块文档注释就是官方的权威描述（注意：官方把它写作**四步**，其中 realize 被并入 layouting，本手册为了学习把它单列为一个阶段，原因见下）：

| 阶段 | 输入 | 输出 | 负责 crate | 一句话职责 |
| --- | --- | --- | --- | --- |
| 1. parse（解析） | 源码字符串 `&str` | 语法树 `SyntaxNode` | `typst-syntax` | 词法 + 递归下降解析；解析永不失败，错误进树 |
| 2. eval（求值） | `Source`（解析后的文件） | `Module`（含 `Content` 与作用域） | `typst-eval` | 树遍历解释器，执行脚本与 markup，产出带样式的元素树 |
| 3. realize（实现） | `Content` | `Content`（统一形态） | `typst-realize` | 应用 show 规则，把任意内容改写成「可排版元素」 |
| 4. layout（排版） | `Content` + 样式链 | `PagedDocument`（每页一个 `Frame`） | `typst-layout` | 分配区域、断行、分页，产出固定坐标的图元树 |
| 5. export（导出） | `PagedDocument` / `Frame` | 字节输出（PDF/PNG/SVG/HTML） | `typst-pdf` 等 | 把图元树序列化为目标格式 |

为什么手册要把 realize 从 layout 里拆出来单讲？因为它是 **Content → Content** 的纯变换（只应用 show 规则，不涉及坐标），与「往坐标平面里摆放图元」的 layout 在数据流上是两种完全不同的操作；而且架构文档明确说明 realization 是一个**浅层、懒惰**的过程——收集列表项时并不立刻实现列表项内部的内容，等真正布局到它时才递归 realize。下一讲（u4-l2）将专攻 realize，所以本讲先把它的位置钉在流水线图上。

#### 4.1.2 核心流程

```text
  源码字符串 "Hello, *world*!"
        │  parse（typst_syntax::parse，纯函数，永不失败）
        ▼
     SyntaxNode               无类型语法树（含 Span 编号）
        │  eval（typst_eval::eval，树遍历解释器，只执行一次）
        ▼
  Module ──► Content          带样式的元素树（text / heading / styled / ...）
        │  ┌───────── 内省收敛循环（最多 5 轮）─────────┐
        │  │  realize（应用 show 规则，懒惰触发）        │
        │  ▼                                           │
        │  layout（区域分配 / 断行 / 分页）              │
        │  ▼                                           │
        │  PagedDocument ◄── 上一轮的内省信息反馈给下一轮 │
        │  └── 收敛？（validate 通过则跳出循环）─────────┘
        ▼
  PagedDocument 的 pages: Vec（Page）
        │  export（typst-pdf / typst-svg / typst-render / typst-html）
        ▼
     PDF / SVG / PNG / HTML 字节流
```

三个要点先记住，后面源码逐一验证：

1. **parse 是纯函数**：`&str -> SyntaxNode`，无任何环境依赖（这也是它能被增量重解析的前提，u2-l5 已学）。
2. **eval 只执行一次**，在循环之外；循环里反复执行的是 realize + layout。
3. **export 在独立的 crate**，编译器本体（`typst::compile`）的返回值是文档对象，不写文件——写文件是 CLI（`typst-cli`）的事。

#### 4.1.3 源码精读

**（1）官方流水线描述**。总装 crate 的模块文档注释（XLSQXcrates/typst/src/lib.rs:L1-L31XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31)）这样概括四步：

```rust
//! # Steps
//! - **Parsing:** The compiler first transforms a plain string into an
//!   iterator of tokens. This token stream is parsed into a syntax tree.
//! - **Evaluation:** The next step is to evaluate the markup. This produces
//!   a module, consisting of a scope of values and content, a hierarchical,
//!   styled representation of what was written in the source file.
//! - **Layouting:** Next, the content is laid out into a
//!   PagedDocument containing one frame per page with items at fixed
//!   positions.
//! - **Exporting:** These frames can finally be exported into an output
//!   format (currently PDF, PNG, SVG, and HTML).
```

（代码片段为便于阅读做了轻微压缩，原文见链接。）这段注释把流程写作 Parsing / Evaluation / Layouting / Exporting 四步，realize 没有单列——它与增量编译的关系在架构文档里补充（XLSQXdocs/dev/architecture.md:L36-L49XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L36-L49)）：编译器是增量的，重复编译远快于从零编译，大部分功劳属于 comemo 框架。

**（2）realize 在 layout 内部的位置**。架构文档 Layout 一节（XLSQXdocs/dev/architecture.md:L118-L131XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L118-L131)）写道：布局之前必须先 realize（应用所有相关 show 规则）；由于 show 规则可能是 Typst 闭包，realization 会触发闭包求值，产出新内容再递归 realize；realization 是浅层的，列表项内部的内容在真正布局到它时才被实现。

源码佐证在 `layout_document` 的入口注释与签名（XLSQXcrates/typst-layout/src/pages/mod.rs:L28-L50XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L28-L50)）：

```rust
/// Layout content into a document.
///
/// This first performs root-level realization and then lays out the
/// resulting elements.
XHASHXtypst_macros::time(name = "layout document")XRSQX
pub fn layout_document(
    engine: &mut Engine,
    content: &Content,
    styles: StyleChain,
) -> SourceResult&lt;PagedDocument&gt; {
```

真正调用 realize 的位置在共享实现 `layout_document_common` 中（XLSQXcrates/typst-layout/src/pages/mod.rs:L164-L176XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L164-L176)）：

```rust
    let mut children = (engine.library.routines.realize)(
        RealizationKind::Document { info: &mut info, options: &mut options },
        &mut engine, &mut locator, &mut arenas, content, styles,
    )?;
    let pages = layout_pages(&mut engine, &mut children, &mut locator, styles)?;
    Ok(PagedDocument::new(pages, info, options))
```

注意 realize 不是直接 `typst_realize::realize(...)` 调进来的，而是经过 `engine.library.routines.realize` 这张函数指针表——这就是 u1-l3 讲过的「类型在 typst-library、行为在总装 crate 接线」。接线表本身在 XLSQXcrates/typst/src/lib.rs:L325-L342XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342)：

```rust
/// This is essentially dynamic linking and done to allow for crate splitting.
static ROUTINES: LazyLock&lt;Routines&gt; = LazyLock::new(|| Routines {
    rules: || { /* typst_layout::register + typst_html::register */ },
    eval_string: typst_eval::eval_string,
    eval_closure: typst_eval::eval_closure,
    realize: typst_realize::realize,
    layout_frame: typst_layout::layout_frame,
    // ...
});
```

realize 还是懒惰的：除页面级之外，flow（XLSQXcrates/typst-layout/src/flow/mod.rs:L155XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/flow/mod.rs#L155)）和 inline（XLSQXcrates/typst-layout/src/inline/mod.rs:L96XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/inline/mod.rs#L96)）在布局到对应层级时才再次经由 routines 调用 realize。

**（3）编译产物如何泛型化**。`compile` 是泛型函数 `compile&lt;T: Output&gt;`，`Output` trait 定义了编译产物的三个能力（XLSQXcrates/typst-library/src/foundations/target.rs:L13-L30XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/target.rs#L13-L30)）：

```rust
pub trait Output: Any {
    fn target() -&gt; Target where Self: Sized;       // 目标：Paged / Html / Bundle
    fn create(engine: &amp;mut Engine, content: &amp;Content,
              styles: StyleChain) -&gt; SourceResult&lt;Self&gt; where Self: Sized;
    fn introspector(&amp;self) -&gt; &amp;dyn Introspector;   // 从产物提取内省信息
}
```

分页目标对应的实现（XLSQXcrates/typst-layout/src/document.rs:L78-L94XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78-L94)）把 `create` 直接转发给 `layout_document`：

```rust
impl Output for PagedDocument {
    fn introspector(&amp;self) -&gt; &amp;dyn Introspector { self.introspector.as_ref() }
    fn target() -&gt; Target { Target::Paged }
    fn create(engine: &amp;mut Engine, content: &amp;Content,
              styles: StyleChain) -&gt; SourceResult&lt;Self&gt; {
        crate::layout_document(engine, content, styles)
    }
}
```

HTML 与 Bundle 目标各有对应实现：`impl Output for HtmlDocument`（XLSQXcrates/typst-html/src/dom.rs:L96XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-html/src/dom.rs#L96)）与 `impl Output for Bundle`（XLSQXcrates/typst-bundle/src/lib.rs:L59XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-bundle/src/lib.rs#L59)）。于是在 `compile_impl` 的主循环里，一句 `T::create(&amp;mut engine, &amp;content, styles)` 就完成了「按目标分发到布局或 HTML 组装」。

**（4）文档可能滞后，以源码为准**。架构文档的 Export 一节仍写着 HTML export does not exist yet（XLSQXdocs/dev/architecture.md:L147-L156XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L147-L156)），但源码中 `typst-html` 已存在且实现了 `Output`——这是 u1-l3 已提醒过的「架构文档可能滞后」的又一个实例，读源码时要有这个意识。

#### 4.1.4 代码实践

**实践目标**：不看表格，凭源码自己把「五阶段入口符号」找齐，建立肌肉记忆。

**操作步骤**：

1. 打开 XLSQXcrates/typst/src/lib.rs:L1-L31XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31) 的文档注释，记下链接指向的入口：`typst_syntax::parse`、`typst_eval::eval`、`typst_layout::layout_document`、`typst_layout::PagedDocument`。
2. 在编辑器里全局搜索确认：`typst_syntax::parse` 定义在 `crates/typst-syntax/src/parser.rs`，入口附近有计时打点 `TimingScope::new("parse")`（XLSQXcrates/typst-syntax/src/parser.rs:L17XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L17)）；`typst_eval::eval` 的计时打点在 XLSQXcrates/typst-eval/src/lib.rs:L39XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L39)。
3. 搜索 `impl Output for`，确认三个实现的位置（Paged / Html / Bundle）。
4. 搜索 `(engine.library.routines.realize)`，列出所有调用文件，验证「realize 在多个布局层级被懒惰调用」。

**需要观察的现象**：每个阶段的入口都能落到一个具体文件的具体函数上；realize 的调用点出现在 pages / flow / inline / html 多处，而不是只有一处。

**预期结果**：与你填写的五阶段表格一致。此实践为纯源码阅读，可直接完成，无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：官方文档把编译写作四步，本手册为什么拆成五个阶段？

**答案**：官方把 realize 并入 layouting（先 realize 再布局是 layout 阶段的内部顺序）；但 realize 是 Content → Content 的纯变换（应用 show 规则），与坐标无关，且下一讲要专讲它，所以手册将其单列为一个阶段便于学习。

**练习 2**：`T::create` 一行代码如何同时支持 PDF 和 HTML 两种完全不同的产物？

**答案**：`compile` 是泛型函数 `compile&lt;T: Output&gt;`，`Output` trait 规定了 `target()`、`create()`、`introspector()` 三个方法；`PagedDocument` 的 `create` 转发到 `layout_document`，`HtmlDocument` 的 `create` 做 HTML 组装。调用方（CLI）通过泛型参数选择目标，主流程代码不变。

**练习 3**：为什么 `layout_document` 里调用 realize 要绕道 `engine.library.routines.realize`，而不是直接 `typst_realize::realize(...)`？

**答案**：crate 拆分下，定义 `Engine` / `Library` 的 `typst-library` 不能依赖行为 crate（会循环依赖），行为以函数指针注入 `Routines` 表，由总装 crate 一次性接线，源码注释称之为 essentially dynamic linking（本质上是动态链接）。

### 4.2 迭代收敛入口：compile_impl 与内省循环

#### 4.2.1 概念说明

有一类排版需求天然是「循环依赖」：目录需要知道每一节在第几页，而页码又取决于目录本身占多大空间；counter、state、交叉引用同理。Typst 的解法是**不动点迭代**：先按「上一轮的内省结果」布局一遍，看这一轮产出的文档的内省信息是否与上一轮一致；一致则收敛，不一致就用新结果再布局一轮，最多 5 轮。

形式化地，设 \( C \) 为 eval 产出的内容，\( \mathcal{I}(D) \) 为从文档 \( D \) 提取的内省信息（各元素的位置、页码、counter 值），布局算子为 \( \mathrm{Layout} \)，则第 \( k \) 轮迭代为

XBOLSQX D_{k+1} = \\mathrm{Layout}\\bigl(C,\\; \\mathcal{I}(D_k)\\bigr) XBORSQX

其中 \( D_0 \) 对应「空内省信息」。收敛条件是本轮布局所依赖的内省查询在 \( D_{k+1} \) 下依然成立（源码中由 comemo 的 `constraint.validate` 判定）。大多数文档 1～2 轮收敛，永不收敛的文档在 5 轮后放弃并给出警告。

架构文档对这一机制的描述（XLSQXdocs/dev/architecture.md:L133-L139XRSQX(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L133-L139)）：内容如何布局（和实现）可能依赖它自身的布局结果（目录里的页码、counter、state 等），Typst 通过内省循环解决这种固有的循环依赖——布局阶段循环执行直到结果稳定，大多数内省一两次迭代后就稳定，但有些可能永不稳定，所以五次尝试后放弃。

#### 4.2.2 核心流程

`compile_impl` 的主循环伪代码：

```text
content = eval(main_source)            # 只执行一次！
history = 容量为 MAX_ITERS - 1 = 4 的数组
loop:
    introspector = history 为空 ? EmptyIntrospector : 上一轮文档.introspector()
    constraint  = comemo::Constraint::new()      # 记录本轮对 introspector 的依赖
    engine      = Engine 组装（introspector 经 constraint 追踪）
    document    = T::create(engine, content, styles)   # 即 realize + layout
    if constraint.validate(document.introspector()):    # 依赖未被打破，收敛
        break
    if history 已满:                                   # 已跑满 5 轮仍不稳定
        analyze(world, 全部内省结果)                     # 生成不收敛警告
        break
    history.push(document)
return document                        # 若 sink 中有延迟错误则返回 Err
```

三个源码级细节值得留意：

- eval 与循环的关系：**eval 在循环之前执行且只执行一次**；循环内每轮重新执行的是 `T::create`（realize + layout）。
- `history` 的容量是 `MAX_ITERS - 1`（存放「未收敛的历史文档」），配合当前一轮，布局最多执行 `MAX_ITERS = 5` 次。
- 每轮开始都会创建 `TimingScope::new(ITER_NAMESXLSQXhistory.len()XRSQX)`，所以用 `--timings` 采集的 trace 里能直接看到 `iter (1)`、`iter (2)`……这正是我们观测收敛轮数的窗口（见 4.3）。
