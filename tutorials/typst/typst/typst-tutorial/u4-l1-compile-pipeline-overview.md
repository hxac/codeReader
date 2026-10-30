# 编译主流程总览：五个阶段的衔接

> 所属单元：u4 排版管线 · 前置讲义：u3-l1 World 抽象（该讲义文件此前缺失，本讲第 2 节会对 World 契约做最小回顾）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 parse → eval → realize → layout → export 五个阶段各自的**输入类型、输出类型与负责的 crate**；
2. 解释 `typst::compile` 内部的**迭代收敛循环**（introspection loop）：为什么求值只执行一次、排版可能执行多轮、上限为什么是 5 轮、不收敛时会发生什么；
3. 会用 CLI 的 `--timings` 参数导出编译计时 JSON（Chrome Tracing 格式），据此统计各阶段耗时占比并绘制柱状图。

## 2. 前置知识

### 2.1 回顾：已经认识的数据形态

- `Source` / `SyntaxNode` / `Span`（u2 系列）：源码文件与其语法树、节点定位编号；
- `World`（u3-l1 规格中的核心契约）：宿主环境向编译器提供 `library()`、`main()`、`source(id)`、`font()` 等输入。本讲只需记住两件事：`world.main()` 返回主文件 id，`world.source(id)` 返回**已经完成解析**的 `Source`；
- `Content` / `Styles` / set 与 show 规则（u3-l4、u3-l7）：求值产物是「带样式的元素树」，show 规则只是登记，尚未执行。

### 2.2 本讲的新概念

1. **流水线**：编译被拆成多个阶段，每个阶段只做一种变换，前一阶段的输出是后一阶段的输入。好处是职责清晰、每阶段可独立缓存。
2. **不动点迭代**：目录里的页码、`counter`、`query` 需要「整本书排完才知道」的信息，而这些信息又会影响排版本身——这是循环依赖。Typst 的解法是反复排版直到结果不再变化，即寻找迭代函数 \(F\) 的不动点 \(D^* = F(D^*)\)。收敛判据是**保守的**：只有当「本轮排版实际读过、且下一轮答案会变」的信息不存在时，才认定收敛。
3. **comemo 与 Constraint**：comemo 是 Typst 自写的增量编译框架。本讲只涉及它的一个侧面：把内省器（introspector）用 `track_with(&constraint)` 包起来后，comemo 会**记录本轮到底读了内省器的哪些条目**；随后 `constraint.validate(新内省器)` 判断换上新内省器后，这些读取是否还会得到相同答案。
4. **泛型 `Output`**：`compile::<T>` 用一个泛型参数区分编译目标——`PagedDocument`（PDF/PNG/SVG 等分页输出）、`HtmlDocument`、`Bundle`。每个目标各自实现 `Output` trait，决定「如何从 Content 造出文档」。
5. **RAII 计时作用域**：`TimingScope` 在创建时记录「开始」事件、在 Drop 时记录「结束」事件，因此一行 `let _scope = TimingScope::new("...")` 就能精确覆盖一个词法作用域的耗时。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/dev/architecture.md` | 官方架构文档：四阶段叙述、各阶段增量性说明（部分内容滞后于代码，本讲会指出） |
| `crates/typst/src/lib.rs` | 总装 crate：`compile` 入口、迭代收敛循环、`ROUTINES` 函数指针表 |
| `crates/typst-syntax/src/source.rs`、`parser.rs` | 解析阶段：`Source::new` 内部调用 `parse` |
| `crates/typst-eval/src/lib.rs` | 求值入口 `eval`：语法树 → `Module`（含 `Content`） |
| `crates/typst-library/src/foundations/target.rs` | `Output` trait 与 `Target` 枚举（Paged/Html/Bundle） |
| `crates/typst-library/src/introspection/convergence.rs` | `MAX_ITERS = 5`、`ITER_NAMES`、不收敛诊断 `analyze` |
| `crates/typst-layout/src/pages/mod.rs` | `layout_document`：realize + layout 的总入口 |
| `crates/typst-layout/src/document.rs` | `PagedDocument` 结构与 `impl Output` |
| `crates/typst-timing/src/lib.rs` | 计时核心：`timed!` 宏、`TimingScope`、`export_json` |
| `crates/typst-kit/src/timer.rs`、`crates/typst-cli/src/compile.rs`、`args.rs` | CLI 侧：`--timings` 参数如何接通计时 |

## 4. 核心概念与源码讲解

### 4.1 五阶段数据流：从字符串到字节

#### 4.1.1 概念说明

Typst 官方文档把编译描述为**四个阶段**：解析、求值、排版、导出。本学习手册把它拆成**五个**——把 realize（内容实现）从 layout 中独立出来，因为它有独立 crate（`typst-realize`）、独立的规则引擎，并且手册为它安排了专门讲义（u4-l2）。这不是矛盾：官方架构文档自己也把 realize 描述为排版阶段的第一步（"To layout `Content`, we first have to _realize_ it"，见下文源码引用）。

五个阶段与数据形态：

| 阶段 | 输入 | 输出 | 负责 crate | 一句话职责 |
| --- | --- | --- | --- | --- |
| parse | `String`（源码文本） | `SyntaxNode`（语法树） | typst-syntax | 纯函数、永不失败，错误进树 |
| eval | `Source`（含语法树） | `Module`（作用域 + `Content`） | typst-eval | 树遍历解释，执行脚本与 set 规则 |
| realize | `Content` | 可排版元素 | typst-realize | 应用 show 规则，改写元素树 |
| layout | 可排版元素 + 样式链 | `PagedDocument`（每页一个 `Frame`） | typst-layout | 区域（regions）协商，定位到具体坐标 |
| export | `PagedDocument` / `Frame` | 字节流（PDF/SVG/PNG/HTML） | typst-pdf 等 | 只消费 Frame 抽象，不依赖编译器内部 |

两个容易踩的认知坑：

- **解析发生在 `Source` 构造时**，不在 `compile` 里。`compile_impl` 调 `world.source(main)` 时拿到的 `Source` 已携带语法树（见 4.1.3）。解析由谁触发？宿主实现 `World::source` 时构造 `Source`。
- **导出不在编译器 crate 里**。`typst::compile` 返回的是 `PagedDocument` 这类结构化文档；把它写成 PDF 字节是 CLI 调用 `typst-pdf` 等导出 crate 完成的。架构文档原话："Exporters live in separate crates"。

另外注意：架构文档部分内容滞后。它说 "HTML export does not exist yet"（见 4.1.3 ⑥），但本仓库中 `typst-html` 已存在、`HtmlDocument` 已实现 `Output`。**架构文档是地图，不是法律；以源码为准**——这个原则在 u1-l3 已出现过，这里再次得到印证。

#### 4.1.2 核心流程

```text
字符串 &str
   │  Source::new：parse(text) + numberize（分配 Span）
   ▼
SyntaxNode（无损语法树）
   │  eval（typst-eval，树遍历解释器）
   ▼
Module.content() = Content（元素树 + Styles）
   │  realize（typst-realize，应用 show 规则；可回调闭包再产生 Content）
   ▼
可排版元素（realized content）
   │  layout_document（typst-layout，页面 → flow → inline → grid）
   ▼
PagedDocument { pages: Vec<Page>, … }（每页一个 Frame）
   │  导出器（typst-pdf / typst-svg / typst-render / typst-html）
   ▼
字节输出（.pdf / .svg / .png / .html）
```

其中 realize 与 layout 的边界：realize 是**浅层**的——收集列表项时并不立刻实现列表项内部的内容，等到该项真正被排版时才懒惰进行（见 4.1.3 ⑤ 引用的架构文档）。

#### 4.1.3 源码精读

**① 总装 crate 的文档注释——官方版的阶段清单**

[crates/typst/src/lib.rs:L1-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31) 用 `//!` 文档注释列出 Steps：Parsing（string → tokens → syntax tree）、Evaluation（evaluate → module，产出 content）、Layouting（`typst_layout::layout_document` → `PagedDocument`，每页一个 frame）、Exporting（frames → PDF/PNG/SVG/HTML）。注意这份清单同样把 realize 并入 Layouting 叙述。

**② 架构文档的四阶段与增量性总述**

[docs/dev/architecture.md:L36-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L36-L42) 列出四个阶段：Parsing / Evaluation / Layout / Export；[L44-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L44-L49) 说明编译器是增量的，重编译远快于从零编译，大部分工作由 comemo 完成。

**③ 解析阶段：纯函数、永不失败**

[docs/dev/architecture.md:L52-L59](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L52-L59)：解析是 `&str -> SyntaxNode` 的纯函数；解析不能失败，语法错误以错误节点形式保留在树中（u2-l3 已精读）。解析入口是 [crates/typst-syntax/src/parser.rs:L15](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/parser.rs#L15) 的 `pub fn parse(text: &str) -> SyntaxNode`。

而解析的实际触发点在 `Source` 的构造函数：[crates/typst-syntax/src/source.rs:L34-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L34-L42) —— `Source::new` 里 `let mut root = parse(&text);` 然后 `root.numberize(id, Span::FULL)` 完成 Span 编号，最后连同行信息一起装进 `Arc<LazyHash<SourceInner>>`（克隆与哈希都廉价，这是增量编译的基石）。同处还可看到 `feature = "timing"` 下会包一个名为 `"create source"` 的计时作用域。

**④ 求值阶段：Source → Module → Content**

[crates/typst-eval/src/lib.rs:L37-L47](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L37-L47) 是求值入口 `pub fn eval(...)`：`#[comemo::memoize]`（模块级缓存）+ `#[typst_macros::time(name = "eval", ...)]`（计时），接收 `Tracked<dyn World>` 与 `&Source`，返回 `SourceResult<Module>`。函数体里 [L68](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L68) 的 `let root = source.root();` 只是取回 `Source::new` 时已解析好的树；[L74-L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L74-L82) 先收集树上的语法错误与警告（有错误则直接返回）；[L85-L96](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L85-L96) 把根节点 cast 成 `ast::Markup` 后递归求值，最终 `Module::new(...).with_content(output)` —— 模块 = 作用域 + 内容。架构文档对求值的描述见 [docs/dev/architecture.md:L84-L93](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L84-L93)。

**⑤ realize + layout：`layout_document` 是合并入口**

[crates/typst-layout/src/pages/mod.rs:L28-L50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L28-L50)：`layout_document` 的文档注释写明 "This first performs root-level realization and then lays out the resulting elements"——realize 与 layout 在这里衔接。它带 `#[typst_macros::time(name = "layout document")]` 计时，转发给 [L53-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L53-L76) 的 `#[comemo::memoize] fn layout_document_impl`（把 `Engine` 拆散成可追踪参数后记忆化——增量排版缓存就发生在这一层及更细的元素层）。realize 的职责描述见 [docs/dev/architecture.md:L118-L131](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L118-L131)：show 规则可能是 Typst 闭包，因此 realization 会触发求值；且 realization 是浅层、懒惰的。

**⑥ 排版产物与导出的分界**

[crates/typst-layout/src/document.rs:L16-L23](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L16-L23) 定义 `PagedDocument { pages, info, options, introspector }`——导出 crate（u5 系列）消费的就是它。架构文档 [docs/dev/architecture.md:L147-L156](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L147-L156) 说明各导出器（PDF/SVG/像素渲染）各自成 crate；其中 "HTML export does not exist yet" 一句已滞后于代码（`typst-html` 已在仓库中，见 4.2.3 的 `Target::Html`）。CLI 侧的导出分发在 [crates/typst-cli/src/compile.rs:L344](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L344) 的 `compile_and_export`（其下有 `export_html` L367、`export_paged` L383、`export_pdf` L402、`export_bundle` L418 等函数）。

#### 4.1.4 代码实践：填出五阶段速查表（源码阅读型）

1. **实践目标**：不看本讲正文，仅凭源码填写表格，把五阶段「钉」进肌肉记忆。
2. **操作步骤**：
   - 打开 [crates/typst/src/lib.rs:L1-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31) 的文档注释，抄下每个阶段链接指向的函数与类型；
   - 用 `grep -n "pub fn parse(" crates/typst-syntax/src/parser.rs`、`grep -n "pub fn eval(" crates/typst-eval/src/lib.rs`、`grep -n "pub fn layout_document" crates/typst-layout/src/pages/mod.rs` 验证入口；
   - 填写：阶段 | 入口函数 | 输入类型 | 输出类型 | crate。
3. **需要观察的现象**：三个入口函数的签名与你填的输入输出是否一致。
4. **预期结果**（即参考答案）：

   | 阶段 | 入口函数 | 输入 | 输出 | crate |
   | --- | --- | --- | --- | --- |
   | parse | `parse` | `&str` | `SyntaxNode` | typst-syntax |
   | eval | `eval` | `Tracked<dyn World>`、`&Source` 等 | `SourceResult<Module>` | typst-eval |
   | realize | `realize`（经 `ROUTINES` 接线，见 4.2.3） | `Content` 等 | 可排版元素 | typst-realize |
   | layout | `layout_document` | `&mut Engine`、`&Content`、`StyleChain` | `SourceResult<PagedDocument>` | typst-layout |
   | export | `compile_and_export` 内各 `export_*` | `PagedDocument` 等 | 字节写入输出 | typst-pdf 等 |

5. 本实践为纯阅读，无需运行编译（结论已在 4.1.3 逐条核实）。

#### 4.1.5 小练习与答案

1. **练习**：为什么说「解析发生在 `Source::new` 里」而不是「发生在 `compile` 里」？
   **答案**：`Source::new` 内部就调用 `parse(&text)` 并 `numberize`（[source.rs:L39-L40](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-syntax/src/source.rs#L39-L40)）；`compile_impl` 只是通过 `world.source(main)` 取回**已含语法树**的 `Source`，之后 `eval` 里的 `source.root()` 仅是读取。
2. **练习**：官方文档说四阶段、本手册说五阶段，差异在哪？
   **答案**：差异只在 realize 是否单列。官方把 realize 视为 layout 的第一步（`layout_document` 的文档注释也这么说）；手册因其有独立 crate 与规则引擎而单列，便于后续讲义（u4-l2）展开。
3. **练习**：架构文档中哪句话已明显滞后于当前代码？
   **答案**："HTML export does not exist yet"（[architecture.md:L154-L156](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L154-L156)）。当前仓库已有 `typst-html`，且 `Target::Html` 与 `HtmlDocument` 的 `Output` 实现都在。

### 4.2 迭代收敛入口：introspection loop

#### 4.2.1 概念说明

**为什么需要循环**：目录要显示章节所在页码，而页码取决于最终排版；排版又受目录长度影响（目录多一行，后面内容可能被挤到下一页）。这类「自引用」依赖在文档系统里普遍存在（counter、state、`query` 都是）。Typst 不做全局求解，而是朴素的**不动点迭代**：用上一轮的答案排下一轮，直到连续两轮一致。

**关键事实**（都能在源码中指认）：

1. **求值只跑一次，循环的是排版**。`compile_impl` 在进入循环前就完成了 `eval`，循环体内只反复调用 `T::create`（即 layout）。
2. **上限 5 轮**：`MAX_ITERS = 5`。架构文档的说法是 "Most introspections stabilize after one or two iterations. However, some may never stabilize, so we give up after five attempts."（[architecture.md:L133-L139](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L133-L139)）。
3. **不收敛不报错，只警告**：5 轮用尽后，编译器**接受最后一轮文档**，调用 `introspection::analyze` 生成「未收敛」警告，然后 break。编译仍算成功。
4. **收敛判据靠 comemo Constraint**：每轮把内省器 `track_with(&constraint)` 后交给 Engine，comemo 记录本轮实际读取过哪些内省条目；下一份文档产出新内省器后，`constraint.validate(新内省器)` 若为真，说明「读过的都不会变」，即收敛。
5. **第一轮用空内省器**：循环开始时 `history` 为空，用的是 `EmptyIntrospector`——所有查询返回「查无此物」。这与求值阶段的 `EmptyIntrospector`（[typst-eval/src/lib.rs:L55](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L55)）一致：**eval 永远看不到内省信息**，只有 layout 阶段的 `context` 查询才用得上它。

#### 4.2.2 核心流程

`compile_impl` 的迭代循环（对应真实代码 [lib.rs:L137-L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L137-L186)）：

```text
eval(content)                     # 循环外，只执行一次
history = []                      # 最多存 MAX_ITERS-1 份旧文档
loop:
    introspector = history.last()?.introspector() 或 EmptyIntrospector
    constraint = comemo::Constraint::new()
    engine.introspector = introspector.track_with(constraint)   # 记录读取
    document = T::create(engine, content, styles)               # realize + layout
    if constraint.validate(document.introspector()):            # 读过的都没变
        收敛，吸收本轮 sink，break
    if history 已满（4 份）:
        analyze(所有轮次的内省器) → 未收敛警告，break            # 仍返回最后一轮文档
    history.push(document)                                       # 带着新答案进入下一轮
```

用数学语言：设第 \(k\) 轮文档为 \(D_k = F(D_{k-1})\)（\(F\) 把一份文档的内省器喂给下一轮排版）。若 \(\text{validate}(D_k) = \text{true}\)，即在本轮实际读取的范围内 \(F\) 的输出对输入不敏感，则 \(D_k\) 近似为不动点 \(D^* = F(D^*)\)，迭代终止。这是**保守**判定：没被读取的差异不会触发额外迭代，也不影响正确性。

#### 4.2.3 源码精读

**① `compile` 入口：薄壳 + 计时**

[crates/typst/src/lib.rs:L64-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L64-L83)：`pub fn compile<T: Output>(world: &dyn World) -> Warned<SourceResult<T>>`——创建 `Sink` 收集诊断，调用 `compile_impl` 并对错误去重，返回「输出 + 警告」。注意它只挂了 `#[typst_macros::time]` 计时属性，**没有** `#[comemo::memoize]`；增量缓存发生在更细的粒度（模块求值、`layout_document_impl`、元素级布局）。

**② 循环前的准备：目标检查 + 取主文件 + 求值一次**

[lib.rs:L100-L114](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L100-L114)：`T::target()` 匹配 `Target::Paged/Html/Bundle` 做特性检查；把 `library.styles` 与目标样式串成 `StyleChain`。[L117-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L117-L132)：`world.main()` → `world.source(main)`（失败时附「主文件无效」提示），然后 `typst_eval::eval(...)?.content()` —— **求值在循环外**。`history` 声明见 [L134-L135](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L134-L135)。

**③ 循环体：Engine 组装与收敛判定**

[lib.rs:L137-L162](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L137-L162) 逐行看四个关键点：

- `L140`：`let _scope = TimingScope::new(ITER_NAMES[history.len()]);` —— 每轮排版包一个名为 `"iter (1)"`…`"iter (5)"` 的计时作用域（这就是 4.2.4 实践里数轮数的依据）；
- `L141-L155`：内省器取 `history.last()` 或 `EmptyIntrospector`；`Constraint::new()` 后把内省器 `track_with(&constraint)` 装进 `Engine`，同时每轮使用**独立的 subsink**（收敛那轮的诊断才算数）；
- `L157`：`document = T::create(&mut engine, &content, &styles)?` —— 排版入口完全由泛型 `T` 决定；
- `L159`：`if timed!("check stabilized", constraint.validate(document.introspector()))` —— 收敛判定，验证通过则吸收 subsink 并 break。

**④ 不收敛路径**

[lib.rs:L164-L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L164-L186)：`history.is_full()` 时，把「空 + 4 份历史 + 当前」共 `MAX_ITERS + 1` 个内省器交给 `typst_library::introspection::analyze` 生成警告，随后照样 break（返回最后一轮文档）。常量定义在 [crates/typst-library/src/introspection/convergence.rs:L16-L18](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/introspection/convergence.rs#L16-L18)：`MAX_ITERS: usize = 5` 与 `ITER_NAMES`；`analyze` 签名见 [L22-L29](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/introspection/convergence.rs#L22-L29)。循环结束后 [lib.rs:L188-L195](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L188-L195) 提升「延迟错误」并返回文档。

**⑤ `T::create` 的分派：Output trait 与三个实现**

[crates/typst-library/src/foundations/target.rs:L10-L30](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/target.rs#L10-L30) 定义 `Output` trait：`target()`（目标）、`create(engine, content, styles)`（造文档）、`introspector()`（供收敛判定）。它与 [L65-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/target.rs#L65-L76) 的 `Target` 枚举（`Paged` 默认、`Html`、`Bundle`）一一对应。分页目标的实现在 [crates/typst-layout/src/document.rs:L78-L94](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78-L94)：`PagedDocument` 的 `create` 直接转发 `crate::layout_document(...)`。而**新文档的内省器从已排好的页面构建**——[document.rs:L29-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L29-L41) 的 `PagedDocument::new` 里 `PagedIntrospector::new(&pages)`。这正是「排完才有答案、有答案再排一遍」的物证。

**⑥ realize 如何被接线（跨 crate 调用之谜）**

`compile_impl` 里看不到 `realize` 字样，因为它是经 `Routines` 函数指针表间接调用的：[lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342) 的 `static ROUTINES` 把 `typst_eval::eval_string`、`typst_realize::realize`、`typst_layout::layout_frame` 等实现填进表里，注释原话 "essentially dynamic linking and done to allow for crate splitting"。这就是 u1-l3 讲过的「签名住 typst-library、实现在总装 crate 接线」。

#### 4.2.4 代码实践：数一数排版跑了几轮

1. **实践目标**：亲眼看到「无内省 → 1 轮；有目录 → 2 轮或更多」。
2. **操作步骤**：
   1. 准备两份文档（示例代码）：

      ```typst
      // plain.typ：无任何内省特性
      = 第一节
      正文若干行。
      ```

      ```typst
      // toc.typ：含目录（目录长度影响分页，页码又来自排版结果）
      #outline()
      = 第一节
      #lorem(200)
      = 第二节
      #lorem(200)
      ```

   2. 分别编译并导出计时（`--timings` 参数定义见 [args.rs:L465-L471](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L465-L471)）：

      ```bash
      cargo run -p typst-cli -- compile plain.typ --timings plain.json
      cargo run -p typst-cli -- compile toc.typ   --timings toc.json
      ```

   3. 统计两份 JSON 里 `"iter (n)"` 事件的出现次数：

      ```bash
      grep -o '"iter ([0-9])"' plain.json | sort | uniq -c
      grep -o '"iter ([0-9])"' toc.json   | sort | uniq -c
      ```

3. **需要观察的现象**：`plain.json` 中应只出现 `"iter (1)"`；`toc.json` 中应出现 `"iter (1)"` 与 `"iter (2)"`（文档更长、目录跨页时轮数可能更多）。
4. **预期结果**：无内省文档 1 轮即收敛；含 outline 的文档至少 2 轮。具体轮数与文档内容相关，**待本地验证**（grep 模式不依赖 JSON 内部结构，只要事件名写入即可命中）。
5. 若想进一步看「不收敛」长什么样（选做）：构造一个依赖自身最终值的计数器（例如在正文里用 `#context` 读取某 `counter` 的 `final()` 并据此 `step()`），重新编译并观察是否出现未收敛警告。警告文案由 `analyze` 生成，具体措辞**待本地验证**。

#### 4.2.5 小练习与答案

1. **练习**：为什么 eval 不放进循环？
   **答案**：脚本是纯的，不依赖内省信息——typst-eval 组装 Engine 时用的就是 `EmptyIntrospector`（[typst-eval/src/lib.rs:L55](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L55)）。重复求值只会浪费（且 `eval` 本身已 `#[comemo::memoize]`）。需要内省的是 layout 阶段的 `context` 查询，所以循环里只有 `T::create`。
2. **练习**：5 轮用尽后编译失败了吗？
   **答案**：没有。[lib.rs:L164-L183](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L164-L183) 只是把各轮内省器交给 `analyze` 生成**警告**，随后 break 并照常返回最后一轮文档。
3. **练习**：`history` 的容量是多少？为什么不是 5？
   **答案**：`ArrayVec<T, { MAX_ITERS - 1 }>` 即 4（[lib.rs:L134](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L134)）。最多 5 轮排版，但只需留 4 份旧文档的内省器即可在满仓时触发放弃路径；第 5 轮结果直接作为 `document` 返回。

### 4.3 计时与观测：typst-timing 与 --timings

#### 4.3.1 概念说明

讲义规格里「计时 API 细节待确认」的部分，本节全部以源码落实：

- **事件模型**：全局 `EVENTS: Mutex<Vec<Event>>` 收集事件；每事件带名字、可选 Span、线程 id、时间戳。默认**关闭**（`ENABLED: AtomicBool` 初值 `false`），未开启时 `TimingScope::new` 返回 `None`，计时近乎零开销。
- **两种埋点方式**：其一，`timed!("名字", 表达式)` 宏——手动包任意表达式；其二，`#[typst_macros::time]` 过程宏——包整个函数，自动生成计时作用域（可用 `name = ...` 覆盖名称、`span = ...` 记录定位）。
- **输出格式**：Chrome Tracing 兼容 JSON（`export_json` 的文档原话 "Export data as JSON for Chrome's tracing tool"），可在 Chrome 的 `chrome://tracing` 或 Perfetto UI 里火焰图浏览；导出时会顺带清空事件。
- **CLI 接线**：`--timings <OUTPUT_JSON>`（experimental）挂在编译命令参数上；`typst-kit` 的 `Timer` 在路径存在时 `enable()` 计时、在 `record()` 里跑完一次编译后把 JSON 写盘，并负责把 Span 反解成「文件:行号」。watch 模式下多次记录需在路径里放 `{n}` 占位符。

#### 4.3.2 核心流程

```text
CLI 解析 --timings 路径
   → Timer::new_or_placeholder(path)     # Some → typst_timing::enable()
   → timer.record(world, compile_once)   # 先 clear，再跑编译，再 export_json
        编译内部：#[time]/timed! 各处产生 Start/End 事件对
   → File::create + export_json(writer, resolve_span)
   → timing.json（Chrome Tracing 格式）
```

编译器里已经埋好的关键计时点（本讲已逐一遇到）：`compile`（[lib.rs:L74](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L74)）、`"eval"`（[typst-eval/src/lib.rs:L39](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/lib.rs#L39)）、`"iter (n)"`（[lib.rs:L140](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L140)）、`"check stabilized"`（[lib.rs:L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L159)）、`"layout document"`（[pages/mod.rs:L34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L34)）、`"analyze introspections"`（[convergence.rs:L24](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/introspection/convergence.rs#L24)）。

#### 4.3.3 源码精读

**① `timed!` 宏与开关**

[crates/typst-timing/src/lib.rs:L34-L44](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L34-L44)：两个变体（带 `span =` / 不带），都只是创建一个 `TimingScope` 然后求值表达式。[L60-L64](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L60-L64)：`ENABLED` 默认 `false`、`EVENTS` 事件表。[L66-L92](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L66-L92)：`enable`/`disable`/`is_enabled`/`clear` 四个开关函数。

**② TimingScope：RAII 记时**

[typst-timing/src/lib.rs:L152-L190](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L152-L190)：结构体文档写明 "A scope that records an event when it is dropped"；`new`/`with_span` 在未启用时返回 `None`；创建即 push 一个 `EventKind::Start` 事件（Drop 时配对 End，见该结构体 impl 的 Drop 部分，紧随 L190 之后）。Span 用裸数字 `NonZeroU64`，注释解释了原因：typst-timing 不能依赖 typst-syntax（否则后者就无法依赖前者）。

**③ 导出**

[typst-timing/src/lib.rs:L94-L104](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L94-L104)：`export_json(writer, source)` 序列化为 Chrome Tracing JSON；`source` 回调把 Span 映射为 `(文件路径, 行号)`；导出后内部清空事件。

**④ 函数级埋点宏**

[crates/typst-macros/src/lib.rs:L338-L364](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-macros/src/lib.rs#L338-L364)：`#[time]` 过程宏的文档与入口（支持 `#[time(span = ..)]` 记录定位）。`compile`、`eval`、`layout_document` 用的都是它。

**⑤ CLI 全链路**

- [crates/typst-cli/src/args.rs:L465-L471](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L465-L471)：参数定义，`--timings <OUTPUT_JSON>`，帮助文案 "Produces performance timings of the compilation process. (experimental)"。
- [crates/typst-cli/src/compile.rs:L40-L48](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L40-L48)：`Timer::new_or_placeholder(command.args.timings.clone())`，随后 `timer.record(&mut world, |world| compile_once(world, &mut config))` —— **compile_once 整个被计时包裹**（compile 与 watch 共用这段逻辑）。
- [crates/typst-kit/src/timer.rs:L33-L53](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/timer.rs#L33-L53)：`Timer::new` 里 `typst_timing::enable()`；`placeholder()` 让有无计时的代码路径统一。
- [timer.rs:L55-L95](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/timer.rs#L55-L95)：`record` 的完整流程——`clear` → 校验 `{n}` 占位（无占位且多次记录会报错）→ 执行编译 → `File::create` → `typst_timing::export_json(writer, |span| resolve_span(...))`。

#### 4.3.4 代码实践：跑通一次计时并看懂事件名

1. **实践目标**：产出第一份 `timing.json`，并能在其中认出本讲出现过的计时点名字。
2. **操作步骤**：
   1. 用 4.2.4 的 `toc.typ`（或任意文档），执行：

      ```bash
      cargo run -p typst-cli -- compile toc.typ --timings toc.json
      ```

   2. 快速盘点出现过的事件名（示例命令）：

      ```bash
      grep -o '"name":"[^"]*"' toc.json | sort | uniq -c | sort -rn | head -20
      ```

   3. （可选）把 `toc.json` 拖进 Chrome 的 `chrome://tracing` 页面或 Perfetto（ui.perfetto.dev），按线程轨道查看时间轴。
3. **需要观察的现象**：事件名列表里应能找到 `eval`、`layout document`、`iter (1)`/`iter (2)`、`check stabilized` 等本讲讲过的名字；`layout document` 应嵌套在每个 `iter (n)` 内部。
4. **预期结果**：与 4.2.4 的轮数观察互相印证。grep 的字段格式（`"name":"..."` 的确切拼写与空格）取决于 serde 序列化细节，**待本地验证**——若未命中，可先 `head -c 500 toc.json` 看一眼实际 JSON 形状再调整模式。

#### 4.3.5 小练习与答案

1. **练习**：计时默认关闭，开销如何做到接近零？
   **答案**：`TimingScope::new`/`with_span` 先查 `is_enabled()`，未启用直接返回 `None`（[typst-timing/src/lib.rs:L172-L177](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-timing/src/lib.rs#L172-L177)），不产生任何事件、不进锁。
2. **练习**：`#[typst_macros::time]` 与 `timed!` 各适合什么场景？
   **答案**：前者包整个函数（如 `compile`、`eval`、`layout_document`），还能带 `span =` 把耗时定位到源码；后者包任意表达式或语句块（如 `constraint.validate(...)` 这种单行判定）。
3. **练习**：watch 模式下想每次重编译都留一份 JSON，参数该怎么写？
   **答案**：路径里加 `{n}` 占位（如 `--timings 'watch-{n}.json'`），否则第二次记录会因 "cannot export multiple recordings without `{n}` in path" 报错（[timer.rs:L68-L72](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-kit/src/timer.rs#L68-L72)）。

## 5. 综合实践

**任务：给一份中等复杂度文档做「五阶段耗时画像」，并解释谁最贵、为什么。**

1. **准备文档**（示例代码，保存为 `bench.typ`）：

   ```typst
   #set page(width: 20cm, height: auto)
   #outline()

   = 引言
   #lorem(300)

   = 方法
   #figure(
     table(columns: 3, ..range(30).map(i => str(i) + ".tg")),
     caption: [一张 30 格的表]
   )
   $ sum_(k=0)^n k = (n(n+1)) / 2 $
   #lorem(300)
   ```

2. **采集**：

   ```bash
   cargo run -p typst-cli -- compile bench.typ --timings bench.json
   ```

3. **聚合与画柱状图**（示例代码，防御式解析，兼容 Chrome Tracing 的常见两种事件形态；建议保存到仓库外的脚本目录，运行 `python3 -I plot_bench.py bench.json`）：

   ```python
   # plot_bench.py —— 按 name 聚合耗时并打印 ASCII 柱状图（示例代码，结果待本地验证）
   import json, sys, collections

   data = json.load(open(sys.argv[1]))
   events = data.get("traceEvents", data)  # 兼容有无外层 traceEvents 键

   # 形态一：Complete 事件（自带 dur 微秒）；形态二：B/E 配对（按线程栈配对）
   dur = collections.Counter()
   starts = {}
   for ev in events:
       name, ph, tid = ev.get("name"), ev.get("ph"), ev.get("tid", 0)
       if ph == "X":
           dur[name] += ev.get("dur", 0)
       elif ph == "B":
           starts.setdefault(tid, []).append((name, ev.get("ts", 0)))
       elif ph == "E" and starts.get(tid):
           n, ts = starts[tid].pop()
           dur[n] += ev.get("ts", 0) - ts

   total = max(dur.values()) or 1
   for name, d in dur.most_common(15):
       bar = "#" * max(1, round(40 * d / total))
       print(f"{name[:28]:28} {d/1000:9.1f} ms {bar}")
   ```

   > 说明：`export_json` 的事件字段（`ph`/`dur`/`ts` 的确切取值）以实际导出的 JSON 为准；若两种形态都未命中，先 `head -c 500` 查看真实结构再调整脚本。此脚本为本讲示例代码，非仓库文件。

4. **观察与回答**：
   - 五个阶段里，`eval`、`layout document`（含 realize）谁占比最大？`iter (n)` 出现了几次、每次多长？
   - 把 `#outline()` 删掉再采一次：轮数与总耗时有何变化？
   - 用 4.2 学到的机制解释你的观察。参考预期：layout（含 realize）应是最耗时阶段——架构文档原话 "overall layout is the most expensive compilation phase"（[architecture.md:L141-L144](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L141-L144)）。具体数值**待本地验证**。
5. **产出**：一张柱状图截图或文本 + 三五句话的结论（谁最贵、轮数多少、outline 的影响）。

## 6. 本讲小结

- 编译是五阶段流水线：`&str` →（parse，typst-syntax）→ `SyntaxNode` →（eval，typst-eval）→ `Content` →（realize，typst-realize）→ 可排版元素 →（layout，typst-layout）→ `PagedDocument`/`Frame` →（export，独立 crate）→ 字节；解析在 `Source::new` 内完成，导出不在编译器 crate 内。
- `compile_impl` 中**求值只跑一次，循环的只有排版**：`T::create` 每轮带着上一轮的内省器重排，直到 `constraint.validate` 通过（comemo 记录「读过什么」的保守收敛判定）。
- 迭代上限 `MAX_ITERS = 5`；不收敛不失败，`introspection::analyze` 生成警告并返回最后一轮文档；每轮排版包在名为 `iter (n)` 的计时作用域里。
- 泛型 `Output` + `Target`（Paged/Html/Bundle）让同一套主流程服务多种编译目标；realize 等跨 crate 行为经 `ROUTINES` 函数指针表在总装 crate 接线。
- 计时体系三件套：`timed!` 宏、`#[typst_macros::time]` 属性宏、RAII 的 `TimingScope`；CLI 用 `--timings <OUTPUT_JSON>`（typst-kit `Timer` 接线）导出 Chrome Tracing JSON，可火焰图浏览或脚本聚合。
- 架构文档会滞后（如 "HTML export does not exist yet"），读架构要拿源码交叉验证。

## 7. 下一步学习建议

- **u4-l2（realize 阶段）**：深入本讲只点了名的 `typst_realize::realize`——show 规则如何逐个触发、`NativeRuleMap` 与 `typst-layout/src/rules.rs` 的 `register` 如何装配内置规则。
- **u4-l3（页面与文档）**：沿 `layout_document` 往下读 `pages/` 目录的 collect/run/finalize 子模块，弄清页面运行与 `PagedDocument` 的组装。
- **u4-l7（内省机制）**：本讲的收敛循环在那一讲展开为 `Introspector` 的收集细节与 `query`/`counter` 语义。
- **u5-l8（增量编译）**：本讲反复出现的 `#[comemo::memoize]`、`Tracked`、`Constraint` 将在 comemo 专题里系统讲解，并结合 `typst watch`。
- 随手验证工具：`cargo run -p typst-cli -- compile x.typ --timings x.json` + Perfetto，是后续每讲都可用的「X 光机」。
