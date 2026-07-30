# eval 表达式求值与文档内省

## 1. 本讲目标

本讲深入 `typst-cli` 的 `src/eval.rs`，讲清楚 `typst eval` 命令如何工作。学完后你应当掌握：

- 理解 `eval` 命令的完整流程：先用 `SystemWorld` 把 `--in` 指向的文档**完整编译一遍**，拿到 `introspector`（文档内省器），再把命令行传入的表达式字符串作为一段独立 Typst 代码**求值**出来。
- 理解 `ExpressionWorld` 这个「包装器」的作用：它为命令行表达式虚构出一个名为 `<input-expression>` 的虚拟文件，让表达式出错时能给出**精确到行列号的诊断信息**。
- 理解 `evaluate_expression` 如何调用核心库 `typst_eval::eval_string`，以及其中 `SpanMode::Mapped` / `RangeMapper` / `SyntaxMode::Code` / `Context` 等参数各自的含义。
- 理解求值结果如何被序列化（JSON / YAML）打印到终端，以及「软失败 + 诊断输出」如何在 eval 中复用。

---

## 2. 前置知识

本讲是专家层讲义，默认你已经读过前置讲义。在进入源码前，先用通俗语言回顾几个关键概念。

### 2.1 introspector（文档内省器）

Typst 编译一份文档后，会得到一个叫 **introspector（内省器）** 的对象，它就像一份「文档地图」：记录了文档里所有带标签（label）的元素、它们出现在第几页、坐标位置、总页数等信息。Typst 标准库里的 `query()`、`locate()`、`here()`、`counter.at()`、`page-numbering()` 这类「上下文函数」都要靠它才能工作——没有内省器，这些函数就无从知道文档的全局布局。

关键点：内省器**只有在完整编译（排版）之后才存在**。这正是 `typst eval` 要先编译文档的原因。

### 2.2 eval_string：把字符串求值成 Value

核心库 `typst-eval` 提供了一个函数 `eval_string`，它能把一段 Typst 代码字符串（比如 `"1 + 2"`）解析并求值，返回一个 `Value`（ Typst 的运行时值，如整数、字符串、内容 `content` 等）。它是「在运行时动态执行 Typst 代码」的统一入口，命令行的 `eval` 与（已弃用的）`query` 都复用它。

### 2.3 Context、Sink、Box<dyn Output>

- **Context**：Typst 的「求值上下文」，携带当前位置 `location` 和样式链 `styles`，供上下文函数取用。
- **Sink（水槽）**：编译引擎的「警告收集器」，求值过程中产生的警告会被收进 `Sink`，最后和编译警告合并打印。
- **`Output` trait**：Typst 编译产物的统一抽象。无论目标是 PDF（`PagedDocument`）、HTML（`HtmlDocument`）还是 Bundle，都实现了这个 trait；它的关键方法是 [`Output::introspector()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L28-L29)——把编译产物里的内省器交出来。

### 2.4 软失败与 set_failed

承接 u1-l2 / u2-l2：`typst eval` 即使求值失败也返回 `Ok(())`，靠 [`set_failed()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L84-L87) 把进程退出码改写成 `FAILURE`，再把错误以诊断形式打印。这种「返回成功但退出码非 0」的设计叫**软失败**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/eval.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs) | 本讲主角。`eval` 命令的全部实现：编译文档 → 取 introspector → 求值表达式 → 序列化打印。 |
| [`src/world.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | `SystemWorld`。eval 用它编译文档；其中的 `EMPTY_ID` 决定了「不带 `--in`」时编译的是一份空文档。 |
| [`src/args.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | `EvalCommand` 参数结构、`Target` 与 `SerializationFormat` 枚举。 |
| [`src/main.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | 命令分发 `dispatch`、`set_failed`、公共 `serialize` 函数。 |
| [`src/query.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs) | （已弃用的）query 命令，结构上与 eval 高度相似，是理解 eval 设计取舍的绝佳对照。 |
| `crates/typst-eval/src/lib.rs` | 核心 `eval_string` 函数定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 eval 命令的全流程：编译文档 → 取 introspector → 求值表达式

#### 4.1.1 概念说明

`typst eval` 想解决的问题是：**在命令行里直接求值一段 Typst 代码，并打印结果**。

它有两种典型用法：

1. **纯表达式**（不带 `--in`）：`typst eval "1 + 2"`，像计算器一样求值，不需要任何文档。
2. **带文档上下文**（带 `--in doc.typ`）：`typst eval "query(<lbl>)" --in doc.typ`，表达式可以调用 `query()` 这类内省函数，去检索 `doc.typ` 编译后文档里的元素。

第二种用法的关键洞察是：**要让 `query()` 工作，必须先把文档完整排版一遍**，否则不存在内省器。因此 eval 的流程被设计成「先编译，再求值」两段式——编译产物本身不是目的，真正的目的是借用产物里的 `introspector`。

#### 4.1.2 核心流程

用伪代码描述 `eval` 的主干：

```
fn eval(command):
    1. 用 --in（可空）构造 SystemWorld
    2. world.reset()；确保主文件可读（world.source(main)）
    3. 按 command.target 编译主文件 → Warned { output, warnings }
       （output 是 Box<dyn Output>）
    4. 若编译失败 (Err)：
         set_failed()；打印编译诊断；返回
    5. 若编译成功 (Ok(output))：
         a. 用 output.introspector() 拿到内省器
         b. 把表达式包进 ExpressionWorld
         c. evaluate_expression(表达式, sink, expr_world, introspector)
         d. 若求值失败：set_failed()，收集错误
            若求值成功：序列化 Value → println
         e. 合并 sink 警告 + 编译警告 → print_diagnostics
```

注意第 3 步：编译的「主文件」是 `--in` 指向的文档，**不是**命令行表达式。命令行表达式在第 5c 步才被独立求值。

#### 4.1.3 源码精读

整个 `eval` 函数在这里：

[eval.rs:30-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L30-L101) —— `eval` 的入口，完成「建世界 → 编译文档 → 求值表达式 → 打印结果」。

第 1、2 步，构造世界并确保主文件就绪：

```rust
let mut world =
    SystemWorld::new(command.r#in.as_ref(), &command.world, &command.process)?;
world.reset();
world.source(world.main()).map_err(|err| err.to_string())?;
```

[eval.rs:31-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L31-L36) —— `command.r#in` 即命令行的 `--in` 选项（`r#in` 是 Rust 对关键字 `in` 的转义写法）。`world.source(world.main())` 在这里不仅是取源码，更起到「主文件不存在就提前报错」的作用。

第 3 步，按目标编译主文档，并把三种产物统一擦除成 `Box<dyn Output>`：

```rust
let Warned { output, mut warnings } = match command.target {
    Target::Paged => typst::compile::<PagedDocument>(&world)
        .map(|result| result.map(|output| Box::new(output) as Box<dyn Output>)),
    Target::Html => typst::compile::<HtmlDocument>(&world)
        .map(|result| result.map(|output| Box::new(output) as Box<dyn Output>)),
    Target::Bundle => typst::compile::<Bundle>(&world)
        .map(|result| result.map(|output| Box::new(output) as Box<dyn Output>)),
};
```

[eval.rs:39-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L39-L46) —— 这里把 `PagedDocument` / `HtmlDocument` / `Bundle` 三种不同类型用 `Box<dyn Output>` 统一，后续代码就不必再关心具体目标。`Warned` 解构同时拿到产物和编译期的静态警告。

编译成功后，**最关键的一行**是取出内省器：

```rust
let eval_result = evaluate_expression(
    &command.expression,
    &mut sink,
    &expr_world,
    output.introspector(),   // ← 文档编译产物里的内省器
);
```

[eval.rs:57-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L57-L62) —— `output.introspector()` 来自 [`Output` trait](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L28-L29)。这就是「先编译文档」的全部意义：把内省器喂给表达式求值。

> **对照 query.rs**：已弃用的 `query` 命令采用几乎一模一样的「编译 → 取 introspector」结构（见 [query.rs:33-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L33-L40)）。事实上 `typst query` 已经被弃用，官方建议改用 `typst eval "query(...)" --in ...`（见 [query.rs:127-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L127-L160) 的弃用提示生成逻辑）。eval 正是 query 的「超集替代品」。

#### 4.1.4 代码实践

**实践目标**：体会「带 `--in`」与「不带 `--in`」两种模式下，表达式拿到的是不同的内省器。

**操作步骤**：

1. 在 `crates/typst-cli` 下构建 CLI：`cargo build`。
2. 运行纯表达式（无文档上下文）：
   ```bash
   ./target/debug/typst eval "1 + 2"
   ```
3. 准备一个带标签的文档 `doc.typ`，内容例如：
   ```typst
   = 标题一 <lbl>
   这是一段正文。
   ```
4. 运行带文档上下文的内省查询：
   ```bash
   ./target/debug/typst eval "query(<lbl>)" --in doc.typ
   ```

**需要观察的现象**：

- 第 2 步应输出 `3`（这一行为有官方 smoke 测试 [smoke.rs:44-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L44-L48) 固定保证）。
- 第 4 步应输出一个 JSON 数组（默认序列化格式是 JSON），里面是 `<lbl>` 标签命中的元素。

**预期结果**：第 2 步输出 `3`。第 4 步输出一个 JSON 数组（具体序列化形态**待本地验证**）。

5. **关键对比**：再运行一次不带 `--in` 的查询：
   ```bash
   ./target/debug/typst eval "query(<lbl>)"
   ```
   对照 [SystemFiles::new](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L227-L237) 中 `main` 的取值：当 `--in` 为空时 `input` 是 `None`，主文件落到 [`EMPTY_ID`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L178-L183)（一份空文档），其内省器里没有任何元素，因此 `query(<lbl>)` 应返回空数组 `[]`。这就是「无文档上下文」的含义。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--in doc.typ` 改成一个有**语法错误**的文档，`typst eval "1 + 2" --in bad.typ` 会输出 `3` 吗？

> **答案**：不会。文档在第 3 步编译失败，走 [eval.rs:88-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L88-L97) 的 `Err` 分支：`set_failed()` 后直接打印编译诊断、提前返回，根本不会执行到表达式求值。表达式再简单也没用——文档必须先编译通过。

**练习 2**：为什么 eval 要支持 `--target html` / `--target bundle`，而不仅仅是默认的 paged？

> **答案**：因为不同 target 对应不同的 `Output` 实现，各自可能携带不同的内省器视图（例如 HTML 编译产物的内省器见 [typst-html/dom.rs:82-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L82-L84)）。让用户选 target，就能在对应编译产物上做内省查询。

---

### 4.2 ExpressionWorld：为命令行表达式造一个「虚拟文件」

#### 4.2.1 概念说明

现在有个问题：命令行表达式（比如 `"1 + foo"`）并不是磁盘上的文件。如果它求值出错（`foo` 未定义），编译器要怎么生成「`file:line:col`」这种诊断信息？通常诊断都关联到一个 `FileId`，而 `FileId` 又对应一个真实文件。

eval 的解法是：**虚构一个名叫 `<input-expression>` 的虚拟文件**，让表达式的每个字符都映射到这个虚拟文件的对应位置。这样诊断信息就能精确定位到命令行表达式里的具体行列，例如：

```
<input-expression>:1:4: error: unknown variable: foo
```

实现这个虚构文件的就是 `ExpressionWorld`——它是一个**装饰器（包装器）**，内部包着真正的 `SystemWorld`，只在两个地方「拦截」：

- `DiagnosticWorld::name`：把虚拟文件 id 翻译成可读名字 `<input-expression>`。
- `World::file`：当编译器请求读取虚拟文件内容时，返回表达式字符串本身。

其余所有 World 方法都原样转发给内部的 `SystemWorld`。

#### 4.2.2 核心流程

```
EXPRESSION_ID = 一个全局唯一的 FileId，路径名 "<input-expression>"

ExpressionWorld {
    world: SystemWorld,        // 被包装的真实世界
    expression: Bytes,         // 命令行表达式字符串的字节
}

name(id):
    若 id == EXPRESSION_ID → 返回 "<input-expression>"
    否则 → 转发给 world.name(id)

file(id):
    若 id == EXPRESSION_ID → 返回 expression 的字节（即表达式内容）
    否则 → 转发给 world.file(id)

其它方法（library/book/main/source/font/today）：全部转发
```

#### 4.2.3 源码精读

先看这个全局唯一的虚拟文件 id：

```rust
static EXPRESSION_ID: LazyLock<FileId> = LazyLock::new(|| {
    FileId::unique(RootedPath::new(
        VirtualRoot::Project,
        VirtualPath::new("<input-expression>").unwrap(),
    ))
});
```

[eval.rs:132-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L132-L137) —— 用 `FileId::unique` 申请一个不与任何真实文件冲突的 id，虚拟路径名取 `<input-expression>`。承接 u2-l1 讲过的「真实路径 → VirtualPath → FileId」三层模型，这里虚拟根是 `VirtualRoot::Project`，所以诊断里它表现得像项目根下的一个普通文件。

再看包装器结构与两个拦截点：

```rust
struct ExpressionWorld {
    world: SystemWorld,
    /// We use `Bytes`, not `&'static str` to avoid hashing in `World::file`.
    expression: Bytes,
}

impl DiagnosticWorld for ExpressionWorld {
    fn name(&self, id: FileId) -> String {
        if id == *EXPRESSION_ID {
            "<input-expression>".into()
        } else {
            self.world.name(id)
        }
    }
}
```

[eval.rs:141-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L141-L155) —— 注释解释了一个细节：为什么用 `Bytes` 而不是 `&'static str`？因为 `World::file` 返回 `Bytes`，`Bytes` 在比较/哈希时按内容；用 `&'static str` 反而会在 `file` 实现里引入额外的哈希开销。

`World::file` 的拦截是核心：

```rust
fn file(&self, id: FileId) -> FileResult<Bytes> {
    if id == *EXPRESSION_ID {
        Ok(self.expression.clone())
    } else {
        self.world.file(id)
    }
}
```

[eval.rs:174-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L174-L180) —— 当诊断系统为了显示错误片段而去「读取」`<input-expression>` 这个文件时，返回的正是命令行表达式本身。于是 codespan 才能高亮出 `1 + foo` 里的 `foo`。

> **承接 u2-l4**：诊断打印时，`print_diagnostics(&expr_world, ...)` 传入的是 `expr_world` 而非裸 `world`（见 [eval.rs:78-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L78-L83)）。这样 `DiagnosticWorld::name` 才能把 `EXPRESSION_ID` 正确翻译成 `<input-expression>`，`file` 才能交出表达式内容用于错误片段高亮。这是「包装器」存在的直接收益。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `<input-expression>` 这个虚拟文件名出现在诊断里。

**操作步骤**：

1. 故意求值一个未定义变量：
   ```bash
   ./target/debug/typst eval "1 + foo"
   ```

**需要观察的现象**：诊断信息的文件名应是 `<input-expression>`，并精确指出 `foo` 在表达式中的位置（行列号）。

**预期结果**：形如 `<input-expression>:1:5: error: unknown variable: foo`（**待本地验证**具体行列）。对照 [eval.rs:148-150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L148-L150) 理解：正是因为 `name` 把 `EXPRESSION_ID` 翻译成了这个字符串。

2. 再对照「虚拟文件内容」从哪来：阅读 [eval.rs:52-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L52-L55)，确认 `expression` 字段就是 `Bytes::from_string(&*command.expression)`——命令行表达式的字节被存进 `ExpressionWorld`，再由 `file` 在诊断时交出。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `ExpressionWorld`，直接用 `SystemWorld` 去求值表达式，诊断还能正确定位吗？

> **答案**：不能正确定位到命令行表达式。`SystemWorld` 既不认识 `EXPRESSION_ID`（它的 `name` 会把该 id 当成普通项目文件去解析路径，得到一个无意义的路径），`file` 也不会返回表达式内容，诊断就无法显示错误片段。`ExpressionWorld` 的两个拦截点正是为此而存在。

**练习 2**：`ExpressionWorld` 实现了 `World` trait 的全部 7 个方法，但大多数只是转发。这种「只改两点、其余转发」的设计模式叫什么？为什么要这样做？

> **答案**：这是**装饰器模式（Decorator pattern）**。因为编译器依赖的是纯逻辑的 `World` 接口（承接 u2-l1），`ExpressionWorld` 通过实现同一个 trait 来「伪装」成一个 World，只在需要定制的 `name`/`file` 两处改写行为，其余照搬 `SystemWorld`，既复用了全部真实环境能力，又精准地注入了虚拟文件语义。

---

### 4.3 evaluate_expression 与 eval_string 的参数

#### 4.3.1 概念说明

`evaluate_expression` 是一个很短的私有函数，它把命令行表达式交给核心库 `typst_eval::eval_string`。它真正要解决的问题是：**告诉编译器「这段字符串里的每个字符，对应虚拟文件里的哪个位置」**——也就是 span（跨度）映射。

为此它构造了一个 `SpanMode::Mapped`，把表达式整段（`0..len`）映射到 `EXPRESSION_ID` 这个虚拟文件。这样求值产生的每个语法节点、每条错误，都能拿到精确的源码跨度。

它还设置了几个关键参数：

- **`SyntaxMode::Code`**：把字符串当成 Typst **代码模式**（而非 markup 标记模式或 math 数学模式）解析。所以 `typst eval "1 + 2"` 里的 `1 + 2` 是代码表达式，可以直接求值。
- **`Scope::default()`**：求值的起始作用域为空——表达式不能用任何「额外注入的局部变量」，只能访问标准库（标准库通过 `library` 单独传入）。
- **`Context`**：携带库的默认样式链，让上下文函数有 styles 可用；但不带 location（`None`）。

#### 4.3.2 核心流程

```
fn evaluate_expression(expression, sink, world, introspector) -> SourceResult<Value>:
    1. 构造 RangeMapper：把整段表达式 (0..len) 映射到虚拟文件
    2. spans = SpanMode::Mapped { id: EXPRESSION_ID, mapper, mapper_error_span: detached }
    3. library = world.library()
    4. 调用 eval_string(
           world, library, sink, introspector,
           Context(None, 库默认样式),
           expression, spans, SyntaxMode::Code, Scope::default()
       )
```

#### 4.3.3 源码精读

`evaluate_expression` 全文：

```rust
fn evaluate_expression(
    expression: &str,
    sink: &mut Sink,
    world: &dyn World,
    introspector: &dyn Introspector,
) -> SourceResult<Value> {
    let spans = SpanMode::Mapped {
        id: *EXPRESSION_ID,
        mapper: &RangeMapper::new(Some(0..expression.len())).unwrap(),
        mapper_error_span: Span::detached(),
    };
    let library = world.library();
    eval_string(
        world.track(),
        library,
        sink.track_mut(),
        introspector.track(),
        Context::new(None, Some(StyleChain::new(&library.styles))).track(),
        expression,
        spans,
        SyntaxMode::Code,
        Scope::default(),
    )
}
```

[eval.rs:104-128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L104-L128) —— 注意三处 `.track()` / `.track_mut()`：这是 comemo（Typst 的记忆化库）把引用变成「可追踪句柄」的惯用法，让跨函数调用的 memoization 缓存能正确失效。

逐个理解关键参数：

- **`SpanMode::Mapped`** 与 **`RangeMapper`**：`SpanMode` 有两个变体（见 [typst-library/routines.rs:127-151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L127-L151)）。`Uniform` 让所有节点共用一个 span（诊断会很笼统）；`Mapped` 则通过 `RangeMapper` 精确地把「求值文本」的区间映射回「源文件」的区间。这里求值文本就是表达式本身，源文件就是 `<input-expression>`，两者内容完全一致，所以 `RangeMapper::new(Some(0..expression.len()))` 是一个一一对应的恒等映射（`RangeMapper` 定义见 [typst-syntax/span.rs:440-465](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs#L440-L465)）。`mapper_error_span: Span::detached()` 表示「映射本身出错时」用的兜底 span，这里几乎不会触发。

- **`introspector`**：从文档编译产物取来的真实内省器（见 4.1）。它让表达式里的 `query()` 能检索文档元素。

- **`Context::new(None, Some(StyleChain::new(&library.styles)))`**：第一参数 `location` 为 `None`（求值不在文档流中，没有具体位置），第二参数带上库的默认样式（`Context::new` 签名见 [typst-library/foundations/context.rs:30-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L30-L32)）。

> **关键对照——为什么 query 用 `Uniform`，eval 用 `Mapped`？**
> 看 query 求值选择器的代码 [query.rs:77-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L77-L88)：它用的是 `SpanMode::Uniform(Span::detached())` 和 `EmptyIntrospector`（先求值出选择器对象，再另行查询）。因为 query 只关心选择器**能不能解析出来**，不在乎它在命令行里的精确位置，所以用「笼统 span」即可。而 eval 要把求值结果的**错误精确指给用户看**，所以值得用更精细的 `Mapped`。这是同一套 `eval_string` API 在两个场景下的取舍。

#### 4.3.4 代码实践

**实践目标**：通过对照 `Uniform` 与 `Mapped` 的差异，理解 eval 为何能给出精确诊断。

**操作步骤**：

1. 阅读 [eval.rs:111-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L111-L115) 与 [query.rs:85-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs#L85-L86) 两处对 `eval_string` 的调用，列一张参数对照表（`spans`、`introspector` 两列）。
2. 运行 `./target/debug/typst eval "1 + "`（故意留一个不完整的表达式）。

**需要观察的现象**：错误诊断应精确指向表达式里 `+` 之后缺少操作数的位置，文件名为 `<input-expression>`。

**预期结果**：诊断能高亮到具体字符位置（**待本地验证**具体文本）。这正是 `SpanMode::Mapped` + `RangeMapper` 一一映射的效果——若改用 `Uniform`，所有错误都会塌缩到一个笼统位置。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Scope::default()` 是空的，表达式却仍能用 `query`、`calc`、`str` 这些标准库功能？

> **答案**：标准库不是通过 `Scope` 传入的，而是通过 `world.library()` 单独传入（[eval.rs:116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L116)、[eval.rs:118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L118)）。`Scope` 控制的是「额外注入的局部变量」，默认为空意味着表达式拿不到任何命令行注入的局部绑定，但全局标准库照常可用。

**练习 2**：把 `SyntaxMode::Code` 换成 markup 模式会怎样？

> **答案**：`SyntaxMode::Code` 让字符串按 Typst 代码规则解析（`1 + 2` 是算术表达式）。若改成 markup 模式，`1 + 2` 会被当成标记文本（数字、空格、加号、空格、数字），求值结果会变成一段 `content` 而非整数 `3`，`typst eval "1 + 2"` 就不再是「计算器」语义了。这也解释了为什么命令行表达式必须用代码模式。

---

### 4.4 结果序列化、软失败与诊断输出

#### 4.4.1 概念说明

求值成功后，`eval_string` 返回一个 `Value`。这个 `Value` 要打印到终端，但终端是文本，所以需要**序列化**成 JSON 或 YAML。`typst-cli` 把这个能力抽成公共函数 `crate::serialize`，被 `eval`、`query`、`info` 三个命令复用。

输出格式由 `--format` 控制（`json` 默认 / `yaml`），`--pretty` 仅对 JSON 生效（缩进美化）。求值或编译失败时走软失败：`set_failed()` 改退出码，诊断照常打印。

#### 4.4.2 核心流程

```
求值成功:
    serialized = serialize(value, format, pretty)   // JSON / YAML
    println!("{serialized}")
    errors = &[]
求值失败:
    set_failed()
    errors = 错误切片
编译失败 (4.1 已述):
    set_failed()；打印编译诊断；提前返回

最后:
    warnings.extend(sink.warnings())                 // 合并求值期警告
    print_diagnostics(&expr_world, errors, &warnings, diagnostic_format)
```

#### 4.4.3 源码精读

求值结果的两分支处理：

```rust
let errors = match &eval_result {
    Err(errors) => {
        set_failed();
        errors.as_slice()
    }
    Ok(value) => {
        let serialized =
            crate::serialize(value, command.format, command.pretty)?;
        println!("{serialized}");
        &[]
    }
};
```

[eval.rs:63-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L63-L74) —— 成功就序列化打印、错误切片为空；失败就 `set_failed()`、错误切片用于诊断。注意 `set_failed()` 承接 u1-l2 的 `thread_local EXIT` 机制。

公共序列化函数（位于 main.rs）：

```rust
fn serialize(
    data: &impl Serialize,
    format: SerializationFormat,
    pretty: bool,
) -> StrResult<String> {
    match format {
        SerializationFormat::Json => {
            if pretty {
                serde_json::to_string_pretty(data).map_err(|e| eco_format!("{e}"))
            } else {
                serde_json::to_string(data).map_err(|e| eco_format!("{e}"))
            }
        }
        SerializationFormat::Yaml => {
            serde_yaml::to_string(data).map_err(|e| eco_format!("{e}"))
        }
    }
}
```

[main.rs:115-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L115-L132) —— `Value` 实现了 `serde::Serialize`，所以能直接用 `serde_json` / `serde_yaml` 序列化。`--format` 与 `--pretty` 对应 [EvalCommand](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L210-L218) 的 `format: SerializationFormat`（默认 `Json`，见 [args.rs:715-721](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L715-L721)）与 `pretty` 字段。

警告合并与诊断打印：

```rust
// Collect additional warnings from evaluating the expression.
warnings.extend(sink.warnings());

print_diagnostics(
    &expr_world,
    errors,
    &warnings,
    command.process.diagnostic_format,
)
```

[eval.rs:76-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L76-L84) —— 求值过程中 `Sink` 收集的警告被追加到编译警告里，一并交给 `print_diagnostics`（承接 u2-l4）。注意传的是 `&expr_world`，让 `<input-expression>` 的诊断能正确渲染。

#### 4.4.4 代码实践

**实践目标**：观察不同序列化格式与美化选项的输出差异。

**操作步骤**：

1. 默认 JSON（紧凑）：
   ```bash
   ./target/debug/typst eval "(a: 1, b: [1,2,3])"
   ```
2. 美化 JSON：
   ```bash
   ./target/debug/typst eval "(a: 1, b: [1,2,3])" --pretty
   ```
3. YAML：
   ```bash
   ./target/debug/typst eval "(a: 1, b: [1,2,3])" --format yaml
   ```

**需要观察的现象**：第 1 步是单行紧凑 JSON；第 2 步是带缩进的多行 JSON；第 3 步是 YAML 格式。

**预期结果**：三种输出对应 [main.rs:120-131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L120-L131) 的三个分支（**待本地验证**具体文本）。

#### 4.4.5 小练习与答案

**练习 1**：求值成功时为什么还要调用 `print_diagnostics`（`errors` 明明是空的）？

> **答案**：因为编译和求值过程可能产生**警告**（warnings）。承接 u2-l4 的设计：`print_diagnostics` 既打印 errors 也打印 warnings，成功分支传入空 `errors`、非空 `warnings`，专门用来把 `sink.warnings()` 和编译期警告告诉用户。若不调用，警告就丢失了。

**练习 2**：`crate::serialize` 是 `pub(crate)` 的私有函数，却被 eval/query/info 三处复用。如果让你把它提成公共 API，需要注意什么？

> **答案**：它依赖 `SerializationFormat` 这个 CLI 专属枚举（与 clap 绑定）和 `serde_json`/`serde_yaml`。若提成公共 API，需把 `SerializationFormat` 一并暴露或抽象掉，并明确 `pretty` 仅对 JSON 生效的契约。当前作为 `pub(crate)` 函数是合理的封装——它本质是 CLI 层的「输出适配」，不属于编译器核心职责。

---

## 5. 综合实践

**任务**：把本讲的四个模块串起来，亲手验证「编译文档 → 取 introspector → 求值表达式 → 序列化输出」的完整链路，并理解诊断定位机制。

**操作步骤**：

1. 在 `crates/typst-cli` 下 `cargo build`，得到 `./target/debug/typst`。

2. 准备一份用于内省的文档 `report.typ`：
   ```typst
   #set heading(numbering: "1.")

   = 引言 <intro>
   研究背景。

   = 方法 <method>
   实验设计。

   #figure(rect(width: 40%), caption: [示意图]) <fig>
   ```

3. **第 1 问（纯表达式，无文档上下文）**：
   ```bash
   ./target/debug/typst eval "calc.sum(1, 2, 3)"
   ```
   预期输出 `6`。这验证了 4.3 的 `SyntaxMode::Code` + 标准库可用。

4. **第 2 问（带文档上下文的内省查询）**：
   ```bash
   ./target/debug/typst eval "query(heading).len()" --in report.typ
   ```
   预期输出 `2`（文档里有两个 heading）。这验证了 4.1「先编译文档、把 introspector 喂给表达式」。对照 [eval.rs:39-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L39-L46) 理解 `report.typ` 是如何先被编译的。

5. **第 3 问（结果序列化与美化）**：
   ```bash
   ./target/debug/typst eval "query(figure.where(kind: image))" --in report.typ --pretty
   ```
   观察输出的美化 JSON 数组，对照 [main.rs:122-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L122-L124) 理解 `--pretty` 走的是 `to_string_pretty` 分支（**待本地验证**具体序列化形态）。

6. **第 4 问（诊断定位虚拟文件）**：
   ```bash
   ./target/debug/typst eval "query(<nope>)" --in report.typ
   ```
   由于 `figure.where(...)` 的查询在前一步可能为空，这里改为检索一个不存在的标签。观察诊断信息里的文件名是否为 `<input-expression>`、是否精确定位。对照 [eval.rs:132-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L132-L137)（`EXPRESSION_ID`）与 [eval.rs:148-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L148-L154)（`name` 翻译）解释这个文件名从何而来。

**验收标准**：能用一句话说清——为什么 `typst eval "query(...)" --in doc.typ` 必须先把 `doc.typ` 编译一遍（答：为了拿到 introspector 喂给表达式的内省函数），以及命令行表达式出错时为什么能精确定位（答：`ExpressionWorld` 虚构了 `<input-expression>` 文件并用 `SpanMode::Mapped` 一一映射字符位置）。

---

## 6. 本讲小结

- `typst eval` 是「先编译文档、再求值表达式」的两段式：编译的真正目的是借用产物里的 `introspector`，让表达式里的 `query()` 等内省函数有数据可查。
- 三种 `Target`（Paged/Html/Bundle）的编译产物都被擦除成 `Box<dyn Output>`，统一通过 [`Output::introspector()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L28-L29) 取出内省器。
- `ExpressionWorld` 是装饰器模式的 World 包装器，虚构 `<input-expression>` 文件（`EXPRESSION_ID`），只在 `name` 与 `file` 两处拦截，使命令行表达式能获得精确诊断。
- `evaluate_expression` 用 `SpanMode::Mapped` + `RangeMapper` 把表达式字符一一映射到虚拟文件，用 `SyntaxMode::Code` 按代码模式解析、`Scope::default()` 不注入局部变量、`Context` 带默认样式。
- 求值结果经公共 `crate::serialize` 序列化成 JSON/YAML 打印；失败走软失败 `set_failed()`，编译/求值警告合并后由 `print_diagnostics` 打印。
- eval 与已弃用的 query 共享同一套 `eval_string` + 编译取 introspector 结构，但 eval 用更精细的 `Mapped` span，是 query 的超集替代品。

---

## 7. 下一步学习建议

- **紧接阅读** [src/query.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs)：对照本讲的 eval，理解 `deprecation_warning` 如何把一条 `typst query` 命令机械地翻译成等价的 `typst eval` 命令（对应下一讲 u4-l2「query 命令与弃用迁移」）。
- **深入核心**：阅读 `typst-eval` 的 `eval_string` 实现（[typst-eval/src/lib.rs:103-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L103-L113)），理解 `SyntaxMode` 与 `SpanMode` 如何影响解析与诊断。如需系统学习 typst-eval，可参考同仓库的 `typst-crates-typst-eval-tutorial`。
- **扩展视野**：阅读 [`Output` trait](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L13-L30) 与各 target 的实现（`PagedDocument` / `HtmlDocument` / `Bundle`），理解内省器在不同编译产物里是如何构造的，这会帮你判断 `--target` 对 eval 查询结果的影响。
