# query 命令与弃用迁移

## 1. 本讲目标

`typst query` 是 Typst CLI 中一个**已被弃用（deprecated）**的子命令。它最初的作用是「从命令行编译一篇文档，再按选择器检索文档里的某些元素，把结果序列化打印出来」——典型用法是给构建系统提取标题、图表、参考文献等元数据。

学完本讲，你应当能够：

1. 说清 `typst query` 的三段流程：**编译文档取内省器 → `retrieve` 检索元素 → `format` 序列化打印**，并指出弃用警告在流程中的注入点。
2. 解释 `retrieve` 如何用 `eval_string` 把命令行上的选择器字符串求值成一个 `LocatableSelector`，再交给 `introspector.query` 完成真正的检索。
3. 掌握 `format` 对 `--one` 与 `--field` 两个选项的处理逻辑，以及它和 `crate::serialize` 的衔接。
4. 读懂 `deprecation_warning` 如何按 `--one` / `--field` 的四种组合，拼出一条**等价的 `typst eval` 命令**，并理解这条迁移命令与原 `query` 在语义上的一处细微差异。

本讲承接上一讲 **u4-l1（eval 表达式求值与文档内省）**：`eval` 子命令正是 `query` 的「超集替代品」，二者共享同一套「编译取内省器 + `eval_string`」骨架，本讲会在对比中反复引用它。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

### 2.1 文档内省器（introspector）

Typst 编译一篇文档时，产物里不仅有用排版好的内容，还有一份「元素清单」：记录了所有**可定位元素（locatable element）**——比如带 `<标签>` 的图表、各级标题、被引用的位置等——以及它们在文档中的先后顺序和位置。这份清单加上一套查询接口，就叫**内省器（introspector）**。

你可以把内省器理解成「文档自己的数据库」：`query` 命令的本质，就是先编译出这个数据库，再去里面查数据。上一讲 u4-l1 已经讲过 `typst eval` 是怎么「编译文档 → 拿到 `introspector()`」的，`query` 走的是完全相同的路。

### 2.2 选择器（selector）与 LocatableSelector

要在内省器里检索，需要一个**选择器（selector）**来描述「我要哪些元素」。选择器在 Typst 里可以是多种形态：

- 标签：`<my-label>`
- 元素函数：`heading`（所有标题）、`figure`（所有图表）
- 正则、`location` 等

在 Typst 内部，命令行传入的选择器字符串会被求值成一个 `LocatableSelector`（一个专门用于 `query` 的、受约束的选择器类型），它的 `.0` 字段就是一个通用的 `Selector`。

### 2.3 为什么 query 被弃用

`typst query` 能做的事——「在文档上下文里求值一段表达式」——`typst eval` 全都能做，而且 `eval` 更通用：`eval` 可以求值任意表达式，`query` 只能求值「一个选择器 + 可选字段」。于是 `query` 成了 `eval` 的真子集，维护两份功能重复的代码不划算，官方决定弃用 `query` 并在每次运行时打印一条「请改用 `typst eval ...`」的迁移提示。本讲的核心看点之一，就是这条提示是**怎样自动生成**的。

> 小提示：`query` 在帮助里是**隐藏的**（`#[command(hide = true)]`），所以 `typst --help` 看不到它，但它仍然能正常运行——这是「弃用但仍可用」的典型做法。

## 3. 本讲源码地图

本讲只涉及 typst-cli 内两个文件，并少量引用同仓库其它 crate 的核心类型定义：

| 文件 | 作用 |
|------|------|
| `src/query.rs` | 本讲主角。`query` 子命令的全部实现：`query()` 主流程、`retrieve()` 检索、`format()` 序列化、`deprecation_warning()` 生成迁移命令。 |
| `src/eval.rs` | 对照对象。`eval` 子命令，本讲引用它的 `evaluate_expression` 来说明 `query` 与 `eval` 在求值选择器时的差异（span 映射精度）。 |
| `src/args.rs` | `QueryCommand` / `EvalCommand` / `Target` 的定义。 |
| `src/main.rs` | `dispatch()` 里把 `Command::Query` 路由到 `query::query`，以及共享的 `serialize` 函数。 |
| `crates/typst-eval/src/lib.rs`（同仓库） | `eval_string` 函数签名——`retrieve` 与 `eval` 都调用它。 |
| `crates/typst-library/src/foundations/selector.rs`（同仓库） | `LocatableSelector` 的定义。 |
| `crates/typst-library/src/introspection/introspector.rs`（同仓库） | `introspector.query` 的实现，真正执行检索的地方。 |
| `crates/typst-syntax/src/lexer.rs`（同仓库） | `is_ident`，被 `deprecation_warning` 用来决定字段访问写成 `.x` 还是 `.at("x")`。 |

## 4. 核心概念与源码讲解

### 4.1 query 命令的整体流程：编译 → retrieve → format

#### 4.1.1 概念说明

`typst query` 子命令解决的问题是：**不写 Typst 代码、直接从命令行提取文档元数据**。比如构建系统想拿到一篇文档里所有图表的标题，用 `typst query doc.typ "figure" --field caption` 就能拿到一份 JSON。

整个命令的实现只有一个入口函数 `query()`，它把任务拆成清晰的三段：

1. **编译文档**：和 `compile` / `eval` 一样，先 `SystemWorld::new` 装配环境，`reset()` 后用 `typst::compile` 编译，但**产物只取它的 `introspector()`**——排版好的 PDF/PNG 等结果被丢弃，编译的唯一目的是「造出文档数据库」。
2. **检索**：调用 `retrieve()`，把命令行上的选择器字符串求值成 `LocatableSelector`，再在内省器里查出匹配元素。
3. **格式化打印**：调用 `format()`，按 `--one` / `--field` 处理后，用 `crate::serialize` 序列化成 JSON/YAML 打印。

一个贯穿全流程的关键设计是**弃用警告的注入时机**：`query()` 在拿到编译产物之后、进入「成功/失败」分支之前，就**无条件**把一条弃用警告推进 `warnings`。这意味着无论编译成功还是失败，这条「请改用 eval」的提示都会出现。

#### 4.1.2 核心流程

```text
typst query <input> <selector> [--field f] [--one] [--format json|yaml] [--pretty] [--target paged|html|bundle]
        │
        ▼
SystemWorld::new(input, world, process)   ← 装配编译环境（项目根、字体、包）
        │
        ▼
world.reset() + world.source(world.main()) ← 重置缓存、确保主文件存在
        │
        ▼
typst::compile::<按 target 选 PagedDocument/HtmlDocument/Bundle>(&world)
        │  产物擦除成 Box<dyn Output>，只取 .introspector()
        ▼
warnings.push(deprecation_warning(command)) ← ★ 无条件注入弃用警告
        │
        ├── 编译成功(Ok) ─→ retrieve(world, command, introspector) ─→ format(...) ─→ println ─→ print_diagnostics(&[], &warnings)
        │
        └── 编译失败(Err) ─→ set_failed() ─→ print_diagnostics(&errors, &warnings)
```

注意两条分支都调用了 `print_diagnostics`：成功分支里 `errors` 为空、只打印那条弃用警告；失败分支里把编译错误和弃用警告一起打印。这就是「弃用提示永不离场」的机制。

#### 4.1.3 源码精读

入口函数 `query()` 把上面三段串起来。先看它如何按 `target` 选择编译目标类型——这段代码和 `eval.rs` 几乎逐行相同：

[query.rs:L33-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L33-L40) —— 按 `Target` 选择 `PagedDocument` / `HtmlDocument` / `Bundle`，编译后用 `Box::new(output) as Box<dyn Output>` 把三种不同产物**擦除成统一类型**，这样后续只需调用 `output.introspector()` 即可，无需关心具体产物。

紧接着就是弃用警告的注入点：

[query.rs:L42-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L42-L43) —— `warnings.push(deprecation_warning(command))`，在 `match output` 之前执行，故**两条分支都能看到这条警告**。`deprecation_warning` 的细节留到 4.4。

成功分支里三步一气呵成：

[query.rs:L47-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L47-L53) —— `retrieve` 检索 → `format` 序列化 → `println!` 打印到 **stdout**，再用 `print_diagnostics` 把弃用警告打印到 **stderr**（结果与警告走不同通道，方便管道分离）。

失败分支用上一讲提到的软失败机制：

[query.rs:L56-L65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L56-L65) —— 编译失败时 `set_failed()` 把退出码置为失败，但函数仍 `return Ok(())`（见函数末尾 [query.rs:L68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L68)），即「软失败」——这是 u1-l2 讲过的 `thread_local EXIT` 机制。

整个入口的声明：

[query.rs:L24-L27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L24-L27) —— 接收 `&'static QueryCommand`（静态生命周期，因为它来自 `LazyLock` 全局解析的 `ARGS`，见 u1-l2），返回 `HintedStrResult<()>`。

命令本身在 `dispatch()` 里被路由：

[main.rs:L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L74) —— `Command::Query(command) => crate::query::query(command)?`，`?` 把应用级错误冒泡给 `main`。

而它在 `Command` 枚举里是**隐藏**的：

[args.rs:L93-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L93-L95) —— `#[command(hide = true)] Query(QueryCommand)`，这正是「弃用但仍可用、只是不在帮助里露面」的实现。对照下一行 `Eval(EvalCommand)` 没有任何隐藏属性——它是被官方推荐的替代品。

#### 4.1.4 代码实践

**实践目标**：亲手运行一次 `typst query`，确认「结果走 stdout、弃用警告走 stderr」的双通道布局，并验证 `query` 虽隐藏但可用。

**操作步骤**：

1. 准备一个带标签的文档 `doc.typ`：
   ```typ
   = Introduction <intro>
   A paragraph.

   = Methods <methods>
   Another paragraph.
   ```
2. 在 `crates/typst-cli` 下构建：`cargo build`（仓库根的 `default-members` 已指向本 crate，直接 `cargo build` 也可）。
3. 运行（注意 `query` 是隐藏子命令，必须手敲全名）：
   ```bash
   ./target/debug/typst query doc.typ "heading"
   ```
4. 分别重定向 stdout 与 stderr 到不同文件，观察通道分离：
   ```bash
   ./target/debug/typst query doc.typ "heading" > result.json 2> warn.txt
   ```

**需要观察的现象**：

- 步骤 3：终端里先打印一条弃用警告（`the \`typst query\` subcommand is deprecated; hint: use \`typst eval ...\` instead`），随后是一个 JSON 数组，含两个 heading 元素。
- 步骤 4：`result.json` 里只有 JSON 数组；`warn.txt` 里只有弃用警告。

**预期结果**：`result.json` 是两个 heading 的紧凑 JSON；`warn.txt` 提示改用 `typst eval "query(heading)" --in doc.typ`。

> 待本地验证：不同终端 / 非 TTY 下弃用警告的着色可能不同（参见 u2-l4 的 `supports_color` 判定）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `query()` 在 `match output` **之前**就 `push` 弃用警告？如果挪到「成功分支」里，会有什么后果？

**答案**：为了让弃用提示在编译**失败**时也能出现。若挪进成功分支，当文档有编译错误时用户只会看到错误、看不到「请迁移到 eval」的提示，违背「弃用就要反复提醒」的目的。

**练习 2**：`query` 子命令在 `typst --help` 里看不到，但 `typst query doc.typ "heading"` 仍能运行。是哪一行代码同时实现了这两点？

**答案**：[args.rs:L94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L94) 的 `#[command(hide = true)]` 只让它从帮助列表里消失，并不阻止 `Command::Query` 变体被 clap 解析；`dispatch()` 里 [main.rs:L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L74) 仍然照常分发它。

---

### 4.2 retrieve：把选择器字符串求值成 LocatableSelector 并查询内省器

#### 4.2.1 概念说明

`retrieve` 要解决一个看似简单的问题：用户在命令行上敲的是一段**字符串**（如 `"<intro>"` 或 `heading`），而内省器的 `query` 方法需要的是一个**强类型的 `Selector`**。这中间的桥梁是 `typst_eval::eval_string`——把字符串当一段 Typst 代码求值，得到一个值，再把这个值**转换（cast）**成 `LocatableSelector`。

这里有一个值得对照 u4-l1 的细节：`query` 与 `eval` 都调用 `eval_string`，但两者给求值器提供的 **span（位置信息）精度不同**：

- `query` 用 `SpanMode::Uniform(Span::detached())`——所有诊断都挂在一个「游离」span 上，定位粗糙。
- `eval` 用 `SpanMode::Mapped { ... RangeMapper ... }`——把字符一一映射到虚拟文件 `<input-expression>`，诊断能精确到行列（u4-l1 讲过的 `ExpressionWorld`）。

这正是 `eval` 比 `query` 更「先进」的一个具体体现：`eval` 能给出精准的错误定位，`query` 不能。

#### 4.2.2 核心流程

从检索的语义看，`introspector.query(selector)` 本质是对文档元素集合做一次过滤：

\[
\text{results} = \big[\, e \;\big|\; e \in \text{introspector.all()},\ \ \text{selector.matches}(e) \,\big]
\]

`retrieve` 的执行步骤：

```text
命令行字符串 command.selector （如 "<intro>" / "heading"）
        │
        ▼
eval_string(world, library, sink, EmptyIntrospector, Context::none(), selector, Uniform(detached), Code, Scope::default())
        │   把字符串当 Typst 代码求值，得到 Value
        ▼
.cast::<LocatableSelector>()?   ← 把 Value 转成 LocatableSelector（失败则报错）
        │
        ▼
introspector.query(&selector.0) ← 用其内部 Selector 真正检索，返回 EcoVec<Content>
        │
        ▼
Vec<Content>                    ← 收集成 Vec 交给 format
```

#### 4.2.3 源码精读

`retrieve` 全文很短，信息密度很高：

[query.rs:L77-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L77-L97) —— 调用 `eval_string` 把 `command.selector` 求值；`.map_err(...)` 把求值产生的多个错误拼成一条 `failed to evaluate selector: ...` 的消息；`.cast::<LocatableSelector>()?` 再做类型转换。

注意求值器收到的 `introspector` 参数是 **`EmptyIntrospector`**：

[query.rs:L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L82) —— 求值选择器字符串本身时，内省器是「空的」。这是因为选择器表达式通常只是 `<intro>` 或 `heading` 这样的**纯字面量**，求值它不需要查询文档；真正的查询发生在下一行的 `introspector.query(...)`，用的是从编译产物取出的**真实**内省器。

真正执行检索的是这一行：

[query.rs:L99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L99) —— `introspector.query(&selector.0)`，取 `LocatableSelector` 的内部 `.0`（一个 `Selector`）交给内省器。`.into_iter().collect::<Vec<_>>()` 把返回的 `EcoVec<Content>` 转成 `Vec`。

被检索的 `LocatableSelector` 本质是对 `Selector` 的一层薄封装，定义在同仓库的 typst-library：

[crates/typst-library/src/foundations/selector.rs:L366-L371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L366-L371) —— `pub struct LocatableSelector(pub Selector)`，注释直言「希望未来被更强的查询机制取代」——这也呼应了 `query` 子命令被弃用的背景。

真正干「过滤」这活的是内省器的 `query` 方法：

[crates/typst-library/src/introspection/introspector.rs:L194-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L194-L205) —— 先用选择器的 128 位哈希查 `queries` 缓存，命中则直接返回；未命中再按 `Selector` 的不同变体（`Elem` / `Location` / `Label` / `Or` …）分别过滤。也就是说「检索」其实有缓存，重复查同一个选择器很快。

`eval_string` 的完整签名（`retrieve` 与 `eval` 共用的同一个函数）：

[crates/typst-eval/src/lib.rs:L103-L113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L103-L113) —— 参数依次是 `world`、`library`、`sink`（收集警告）、`introspector`、`context`、待求值字符串、`SpanMode`、`SyntaxMode`、`Scope`。`retrieve` 给的是 `SpanMode::Uniform(Span::detached())` + `SyntaxMode::Code` + `Scope::default()`（不注入任何局部变量）。

对照 `eval` 里更精细的 span 映射：

[src/eval.rs:L110-L127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L110-L127) —— `eval` 用 `SpanMode::Mapped { id: *EXPRESSION_ID, mapper: &RangeMapper, ... }`，把表达式字符映射到虚拟文件 `<input-expression>`。这正是 u4-l1 讲过的「eval 的诊断能精确到行列、query 不能」的根因。

#### 4.2.4 代码实践

**实践目标**：体会「选择器字符串」可以是多种形态，并理解 `cast::<LocatableSelector>()` 对非法选择器的报错行为。

**操作步骤**：

1. 沿用 4.1 的 `doc.typ`。
2. 用标签选择器检索单个元素：
   ```bash
   ./target/debug/typst query doc.typ "<methods>"
   ```
3. 用元素函数选择器检索全部标题：
   ```bash
   ./target/debug/typst query doc.typ "heading"
   ```
4. 故意传入**无法**转换为 `LocatableSelector` 的表达式（比如一个整数）：
   ```bash
   ./target/debug/typst query doc.typ "1 + 2"
   ```

**需要观察的现象**：

- 步骤 2：返回一个只含 `<methods>` 标题元素的数组。
- 步骤 3：返回含两个标题的数组。
- 步骤 4：报错信息形如 `error: failed to evaluate selector: ...` 或 cast 失败提示——因为整数无法 cast 成 `LocatableSelector`。

**预期结果**：合法选择器（标签、元素函数）正常返回；非法表达式在 `.cast::<LocatableSelector>()` 处失败。

> 待本地验证：复合选择器（如 `heading.where(level: 1)`）是否也能作为 `command.selector` 传入并被正确求值——从源码看 `eval_string` 求值的是任意代码，理论上可行，建议亲手验证。

#### 4.2.5 小练习与答案

**练习 1**：`retrieve` 在求值选择器时传入的是 `EmptyIntrospector`，但真正检索用的是文档的真实内省器。为什么求值阶段不直接用真实内省器？

**答案**：因为选择器字符串（如 `heading`、`<intro>`）是**纯字面量**，求值它不需要任何文档信息；而且此时还没拿到真实内省器（真实内省器来自编译产物，正是求值完选择器后才用它去 `query`）。用空内省器既能完成字面量求值，又避免了语义混淆。

**练习 2**：把 `query` 的 `retrieve` 和 `eval` 的 `evaluate_expression` 放在一起，说出它们传给 `eval_string` 的两个关键差异。

**答案**：① `SpanMode`：`query` 用 `Uniform(Span::detached())`（粗糙），`eval` 用 `Mapped` + `RangeMapper`（精确到行列）；② `introspector`：`query` 求值时传 `EmptyIntrospector`、之后再单独用真实内省器 `query`，而 `eval` 直接把从编译产物取出的真实 `introspector` 传进 `eval_string`，让表达式里的 `query()` 等上下文函数直接可用。

---

### 4.3 format：--one 与 --field 的处理与序列化

#### 4.3.1 概念说明

`retrieve` 给出的是一串 `Content`（文档元素）。`format` 的工作是「把这一串元素变成用户想要的最终文本」：是否只取一个？是否只取某个字段？序列化成 JSON 还是 YAML？

两个核心选项：

- `--field f`：不输出整个元素，而是对每个元素取 `f` 字段（例如 heading 的 `level`、`body`）。底层用 `Content::get_by_name(f)`。
- `--one`：断言「恰好一个」匹配元素；多了或少了都报错。

`format` 最后把结果交给 `crate::serialize`（与 `eval`、`info` 共用的同一个序列化函数，定义在 `main.rs`）。

#### 4.3.2 核心流程

```text
Vec<Content> elements
        │
        ├── command.one == true 且 elements.len() != 1  ──→ bail!("expected exactly one element, found N")
        │
        ▼
对每个元素 c：
   command.field == Some(f) ──→ c.get_by_name(f).ok()   （取不到则丢弃该元素）
   command.field == None     ──→ c.into_value()          （整个元素转成 Value）
   收集成 Vec<Value> mapped
        │
        ├── command.one == true  ──→ serialize(&mapped[0])   （单个值）
        └── command.one == false ──→ serialize(&mapped)      （数组）
```

把四种 `(one, field)` 组合对应的「源码行为」整理成表：

| `--one` | `--field` | `format` 对每个元素做什么 | 最终序列化对象 |
|---------|-----------|--------------------------|----------------|
| 否 | 无 | `c.into_value()`（整个元素） | 元素数组 |
| 否 | `f` | `c.get_by_name(f)` | 字段值数组 |
| 是 | 无 | `c.into_value()`，且断言恰一个 | 单个元素 |
| 是 | `f` | `c.get_by_name(f)`，且断言恰一个 | 单个字段值 |

#### 4.3.3 源码精读

`format` 全文：

[query.rs:L103-L124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L103-L124) —— 开头先做 `--one` 的数量校验：

[query.rs:L104-L106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L104-L106) —— 若 `--one` 但元素数不为 1，立即 `bail!`。注意它在取字段**之前**就校验，所以报的是元素总数，而非字段数。

字段映射用 `filter_map`，取不到字段的元素被**静默丢弃**：

[query.rs:L108-L114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L108-L114) —— 有 `--field` 时 `c.get_by_name(field).ok()`（失败变 `None` 被过滤）；无 `--field` 时 `Some(c.into_value())`（保留整个元素）。

`--one` 的取值与序列化分流：

[query.rs:L116-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L116-L123) —— `--one` 时 `mapped.first()` 取出单个值（若 `mapped` 空——即字段都取不到——再 `bail!("no such field found for element")`），否则序列化整个数组。

`crate::serialize` 的实现（`eval` / `info` 也用它）：

[main.rs:L115-L131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L115-L131) —— 按 `SerializationFormat` 分发：JSON 用 `serde_json`（`--pretty` 控制是否缩进），YAML 用 `serde_yaml`。

`--one` / `--field` / `--format` / `--pretty` 在 `QueryCommand` 里的定义：

[args.rs:L164-L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L164-L180) —— `field: Option<String>`（`--field`，可选）、`one: bool`（默认 `false`）、`format: SerializationFormat`（默认 JSON）、`pretty: bool`（仅 JSON 生效）。

#### 4.3.4 代码实践

**实践目标**：穷举 `--one` / `--field` 的四种组合，对照 4.3.2 的表格逐一验证。

**操作步骤**：沿用 `doc.typ`（含两个 heading）。

1. 无 `--one` 无 `--field`：
   ```bash
   ./target/debug/typst query doc.typ "heading"
   ```
2. 无 `--one`、有 `--field`（取每标题的层级）：
   ```bash
   ./target/debug/typst query doc.typ "heading" --field level
   ```
3. 有 `--one`、无 `--field`，且匹配恰好一个（用标签保证唯一）：
   ```bash
   ./target/debug/typst query doc.typ "<methods>" --one
   ```
4. 有 `--one`、有 `--field`：
   ```bash
   ./target/debug/typst query doc.typ "<methods>" --one --field level
   ```
5. 触发 `--one` 的「数量不符」报错（`heading` 有两个，却要求恰一个）：
   ```bash
   ./target/debug/typst query doc.typ "heading" --one
   ```
6. 体验 YAML 与 pretty JSON：
   ```bash
   ./target/debug/typst query doc.typ "heading" --format yaml
   ./target/debug/typst query doc.typ "heading" --format json --pretty
   ```

**需要观察的现象**：

- 步骤 1：两个完整 heading 元素的 JSON 数组。
- 步骤 2：`[1, 1]`（两个标题的层级，都是 1）。
- 步骤 3：单个 heading 元素（非数组）。
- 步骤 4：`1`（单个数字）。
- 步骤 5：报错 `error: expected exactly one element, found 2`。
- 步骤 6：YAML 用 `- ` 列表语法；pretty JSON 带缩进换行。

**预期结果**：与 4.3.2 表格完全吻合。

#### 4.3.5 小练习与答案

**练习 1**：步骤 5 的报错是 `format` 里哪一行抛出的？为什么不是在 `retrieve` 阶段就报错？

**答案**：[query.rs:L104-L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L104-L105)。因为「是否恰一个」是**输出格式**层面的约束（由 `--one` 决定），而 `retrieve` 只负责「按选择器检索」，与 `--one` 无关，所以校验放在 `format`。

**练习 2**：如果某元素**没有** `--field` 指定的字段，`format` 会怎么做？

**答案**：`get_by_name(field).ok()` 返回 `None`，被 `filter_map` **静默丢弃**（见 [query.rs:L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L111)），不会报错，结果数组里少一项。

---

### 4.4 deprecation_warning：生成等价的 typst eval 迁移命令

#### 4.4.1 概念说明

`deprecation_warning` 是本讲最精巧的部分。它的任务是：**根据用户这次 `query` 调用所用的选项，自动拼出一条功能等价的 `typst eval` 命令**，作为弃用提示的 hint 打印出来。

直觉上，`typst query doc.typ "<methods>" --field level --one` 应该等价于「在文档上下文里求值 `query(<methods>).first().level`」。`deprecation_warning` 正是把这种直觉**机械化**：它根据 `--one` / `--field` 的组合，在 `query(selector)` 后面追加正确的「访问后缀」，再包进 `typst eval "..." --in doc.typ`。

这里有一个**重要的语义细微差异**值得记住：`format` 里的 `--one` 要求**恰好一个**元素（多了报错），但迁移命令用的 `.first()` 在 Typst 里**只在数组为空时报错**，多于一个时只会静默返回第一个。所以这条自动生成的迁移命令在「匹配多个元素」时与原 `query --one` **并不完全等价**——这是把「恰好一个」简化成「取第一个」带来的代价。官方选择 `.first()` 是因为它更常用、表达式更短。

#### 4.4.2 核心流程

```text
起点：command.selector （用户给的选择器字符串）
        │
        ▼
buf = format!("query({})", selector)      ← 先拼出 query(<selector>)
        │
        定义 access(field)：              ← 决定字段访问的写法
           is_ident(field)? → ".{field}"   （如 .level）
           否则            → ".at({repr})"  （如 .at("a b")）
        │
        按 (command.one, command.field) 追加后缀：
           (false, None)  → 不追加
           (false, Some)  → .map(it => it{access(field)})
           (true,  None)  → .first()
           (true,  Some)  → .first(){access(field)}
        │
        ▼
shell_escape::escape(buf)                  ← 转义成可安全嵌入 shell 的字符串
        │
        ▼
按 input 拼最终命令：
   Input::Path(p) → typst eval {query} --in {p}
   Input::Stdin   → typst eval {query}
        │
        ▼
warning!(Span::detached(), "the `typst query` subcommand is deprecated"; hint: "use `{eval_command}` instead")
```

把四种组合的「生成的表达式」整理成表（与 4.3.2 的 `format` 行为表对照）：

| `--one` | `--field` | `deprecation_warning` 生成的表达式 | `format` 的实际行为 |
|---------|-----------|------------------------------------|---------------------|
| 否 | 无 | `query(selector)` | 整个元素数组 |
| 否 | `f`（合法标识） | `query(selector).map(it => it.f)` | 字段值数组 |
| 否 | `f`（含特殊字符） | `query(selector).map(it => it.at("f"))` | 字段值数组 |
| 是 | 无 | `query(selector).first()` | 单个元素（断言恰一个） |
| 是 | `f` | `query(selector).first().f`（或 `.at(...)`） | 单个字段值（断言恰一个） |

#### 4.4.3 源码精读

`deprecation_warning` 全文：

[query.rs:L127-L146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L127-L146) —— 先 `format!("query({})", command.selector)` 拼出 `query(...)` 主体；定义 `access` 闭包决定字段写法；用 `match (command.one, &command.field)` 追加后缀；最后 `shell_escape::escape` 转义。

`access` 闭包的判断：

[query.rs:L130-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L130-L136) —— 字段名是合法标识符就写 `.level`，否则写 `.at("...")`（用 `repr()` 加引号转义）。`is_ident` 的定义在同仓库 typst-syntax：

[crates/typst-syntax/src/lexer.rs:L1237-L1242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/lexer.rs#L1237-L1242) —— 首字符须是 id-start、其余须是 id-continue（含 `_`、`-`）。所以 `level` 是标识符（写 `.level`），而 `a b`（含空格）不是（写 `.at("a b")`）。

四种组合的分支，注意 `--field` 与 `--one` 的叠加：

[query.rs:L137-L144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L137-L144) —— `(false, None)` 不追加；`(false, Some)` 追加 `.map(it => it{access})`（对所有元素取字段）；`(true, None)` 追加 `.first()`；`(true, Some)` 追加 `.first(){access}`（先取第一个再取字段）。

最终命令按输入是文件还是 stdin 拼装：

[query.rs:L148-L153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L148-L153) —— 文件路径加 `--in {path}`（这正是 `EvalCommand.r#in`，见 [args.rs:L203-L204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L203-L204)）；stdin 则不带 `--in`（eval 默认无文档上下文，但 `query()` 表达式会触发内省——实际 stdin 场景较少见）。

最后用 `warning!` 宏组装成 `SourceDiagnostic`：

[query.rs:L155-L159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L155-L159) —— `Span::detached()` 表示这条警告不挂任何源码位置；`hint:` 子句带上拼好的 `eval_command`。这个 `SourceDiagnostic` 正是 4.1 里被 `push` 进 `warnings` 的对象。

#### 4.4.4 代码实践

**实践目标**：对照 4.4.2 的表格，验证 `deprecation_warning` 生成的迁移命令与原 `query` 输出**一致**（并体会 4.4.1 提到的 `.first()` 与 `--one` 的差异）。

**操作步骤**：沿用 `doc.typ`。对每一种组合，先跑 `query` 读它的弃用提示，再用提示里的 `eval` 命令跑一遍，对比 stdout。

1. `(false, None)`：
   ```bash
   ./target/debug/typst query doc.typ "heading"
   # 读 stderr 的提示，应含：typst eval "query(heading)" --in doc.typ
   ./target/debug/typst eval "query(heading)" --in doc.typ
   ```
2. `(false, Some level)`：
   ```bash
   ./target/debug/typst query doc.typ "heading" --field level
   ./target/debug/typst eval "query(heading).map(it => it.level)" --in doc.typ
   ```
3. `(true, None)`（用标签保证唯一匹配）：
   ```bash
   ./target/debug/typst query doc.typ "<methods>" --one
   ./target/debug/typst eval "query(<methods>).first()" --in doc.typ
   ```
4. `(true, Some level)`：
   ```bash
   ./target/debug/typst query doc.typ "<methods>" --one --field level
   ./target/debug/typst eval "query(<methods>).first().level" --in doc.typ
   ```
5. **观察差异**：让选择器匹配**两个**元素，再对比 `--one` 与 `.first()`：
   ```bash
   ./target/debug/typst query doc.typ "heading" --one               # 期望：报错 found 2
   ./target/debug/typst eval "query(heading).first()" --in doc.typ  # 期望：返回第一个，不报错
   ```

**需要观察的现象**：

- 步骤 1–4：每对命令的 stdout **完全相同**（注意 `query` 输出是紧凑 JSON、`eval` 输出格式可能略有不同，但**数据内容**一致）。
- 步骤 4：`query` 返回 `1`，`eval` 也返回 `1`。
- 步骤 5：`query --one` 报 `expected exactly one element, found 2`；而 `eval "...first()"` 静默返回第一个 heading——**这正是 4.4.1 所说的不等价之处**。

**预期结果**：在「匹配 0 或 1 个元素」时，迁移命令与原命令等价；在「匹配 ≥2 个元素」时，`--one` 报错而 `.first()` 不报错——迁移命令是「最佳努力」建议，不是严格等价。

> 待本地验证：步骤 1–4 中 `query`（紧凑 JSON）与 `eval`（可能 pretty 或不同序列化）的**字符串形式**是否逐字节相同；若不同，请关注数据内容而非排版。

#### 4.4.5 小练习与答案

**练习 1**：用户运行 `typst query doc.typ "figure" --field "my field"`（字段名含空格）。`deprecation_warning` 会生成什么样的表达式？为什么？

**答案**：会生成 `query(figure).map(it => it.at("my field"))`。因为 `"my field"` 含空格，`is_ident` 返回 `false`（见 [lexer.rs:L1237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/lexer.rs#L1237)），`access` 闭包走 `.at({repr})` 分支（[query.rs:L134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L134)），用 `repr()` 给字符串加引号转义。

**练习 2**：为什么 `deprecation_warning` 用 `.first()` 而不是某种「断言恰一个」的写法来对应 `--one`？这带来什么后果？

**答案**：`.first()` 是 Typst 里更地道、更短的表达；但它的语义是「取第一个（空则报错）」，而 `--one` 要求「恰好一个」。后果是：当匹配多个元素时，`--one` 会报错，迁移命令 `.first()` 却静默返回第一个——二者不完全等价。所以这条迁移提示是「最佳努力」，用户在多匹配场景需自行留意（参见 4.4.1）。

## 5. 综合实践

把本讲知识串起来，完成一次「真实迁移」。

**任务**：你有一个脚本依赖 `typst query` 提取文档元数据，现在要把它迁移到 `typst eval`。请按下述步骤完成，并理解每一步对应的源码。

1. 准备 `paper.typ`：
   ```typ
   #let chapters = (
     (title: "Background", body: [Old stuff]),
     (title: "Conclusion", body: [The end]),
   )

   #for ch in chapters [
     = #ch.title <chap>
     #ch.body
   ]
   ```
   这会生成两个带 `<chap>` 标签……不对，循环里给每个标题加同一个标签会冲突。请改用能区分的选择器，例如直接用 `heading`。
2. 用 `query` 提取所有标题的 `body`：
   ```bash
   ./target/debug/typst query paper.typ "heading" --field body
   ```
3. 阅读弃用提示，**抄下**它建议的 `eval` 命令。
4. 对照 [query.rs:L137-L144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L137-L144)，解释为什么提示里的后缀是 `.map(it => it.body)` 而不是别的写法（`body` 是合法标识符 → 走 `.body` 分支）。
5. 运行建议的 `eval` 命令，确认 `body` 内容与步骤 2 一致。
6. 进阶：把「只取第一个标题的 body」分别用 `query --one --field body` 和对应的 `eval` 命令实现，验证它们等价；再制造一个「两个标题」的场景，亲历 `--one` 报错而 `.first()` 不报错的差异。

**验收标准**：你能不看源码，根据任意 `query` 调用的 `--one` / `--field` 组合，预测出 `deprecation_warning` 会生成的 `eval` 表达式，并能说出它在「多匹配」时的不等价风险。

## 6. 本讲小结

- `typst query` 是已弃用子命令，整体三段流程：**编译文档取内省器 → `retrieve` 检索 → `format` 序列化打印**；弃用警告在进入成功/失败分支**之前**无条件注入，故永远出现。
- `retrieve` 用 `eval_string`（`SpanMode::Uniform(Span::detached())`）把选择器字符串求值成 `LocatableSelector`，再用从编译产物取出的真实 `introspector.query` 完成检索；检索本身有哈希缓存。
- `format` 按 `--one`（断言恰一个）与 `--field`（`get_by_name` 取字段、取不到静默丢弃）处理元素，最后经共享的 `crate::serialize` 输出 JSON/YAML。
- `deprecation_warning` 按 `(one, field)` 四种组合在 `query(selector)` 后追加 `.map(...)` / `.first()` / `.at(...)` 后缀，经 `shell_escape` 转义后包成 `typst eval "..." --in <path>`，用 `warning!` 宏带 `Span::detached()` 输出。
- `query` 与替代品 `eval` 共享 `eval_string` 骨架，但 `eval` 用 `Mapped` span 提供更精准的诊断——这是 `eval` 更先进的体现之一。
- 迁移提示是「最佳努力」：在匹配 0/1 个元素时与 `query` 等价；匹配多个时，`--one` 报错而 `.first()` 静默返回第一个，二者**不完全等价**。

## 7. 下一步学习建议

- 本讲已把 `query` 讲透，并且反复对照了 `eval`。如果你还没细读 `eval` 的 `ExpressionWorld` 与 `RangeMapper`，建议回头精读 u4-l1，体会 `Mapped` span 如何把命令行表达式的错误精确定位到行列。
- `query` / `eval` 都依赖 `introspector`。想深入理解「内省器是怎么被构建、`query()` 在 Typst 标记层如何工作」，建议阅读 `crates/typst-library/src/introspection/introspector.rs`（本讲引用了它的 `query` 方法）以及 `crates/typst-library/src/foundations/selector.rs` 的 `LocatableSelector` / `Selector` 实现。
- 想看 `eval_string` 的求值内部（`SyntaxMode`、`Scope`、`Sink` 如何协同），可以阅读同仓库的 `typst-eval` crate（`crates/typst-eval/src/lib.rs`）。
- 下一讲 u4-l3 将转向另一条高级导出路径：HTML / Bundle 导出与 watch 模式下的内置 HTTP 服务器，与本讲的 `Target::Html` / `Target::Bundle` 编译目标衔接。
