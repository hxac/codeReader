# utils —— 共享工具集

## 1. 本讲目标

在前几讲里，你已经认识了 `IdeWorld` 数据契约（u1-l2）、光标到语法树的映射（u2-l1）、表达式归类 `deref_target`（u2-l2）、作用域收集 `named_items`（u2-l3）和值推断 `analyze`（u2-l4）。这些功能分散在多个文件里，但它们都要反复用到一些「基础零件」：

- 一个能跑求值的临时引擎；
- 一个按当前语法模式拿到标准库作用域的入口；
- 一个递归地、带步数上限地检查值是否满足某条件的小工具；
- 一个把字体家族信息汇总成人类可读摘要的函数。

这些零件都集中在 `src/utils.rs` 里，是典型的「公共工具集」。本讲学完后，你应该能够：

1. 理解 `with_engine` 为什么用 `EmptyIntrospector`、`Traced`、`Sink` 各造一份「空」实现，并说明它被谁复用。
2. 掌握 `globals` 如何依据光标后的语法模式（math / 其它）在 `library.math` 与 `library.global` 之间切换。
3. 理解 `check_value_recursively` 用 `max_steps` 防止递归爆炸的设计，并能解释「含颜色的字典/模块也会出现在颜色补全里」这一行为。
4. 看懂 `summarize_font_family` 与 `MetricRange` 如何把一组字体变体的度量信息压缩成一行摘要。

---

## 2. 前置知识

阅读本讲前，请确保你大致了解以下概念（如果陌生，可先回到对应讲义）：

- **`IdeWorld` 与 `World`**（u1-l2）：所有 IDE 公共函数的第一个参数都是 `&dyn IdeWorld`；`IdeWorld: World`，且必填方法 `upcast()` 返回 `&dyn World`。`World` 的方法（`library`、`book`、`source` 等）都被 comemo 的 `track` 缓存。
- **`LinkedNode` 与语法模式**（u2-l1）：`LinkedNode::new(source.root())` 套上偏移与父子兄弟上下文；`leaf_at(cursor, side)` 下钻到叶子节点。
- **求值引擎 `Engine`**（u2-l4）：Typst 求值需要一个 `Engine`，它持有 `library`、`world`、`introspector`、`traced`、`sink`、`route` 六个字段；其中 `Traced` + `Sink` 配合可捕获「某个 span 求值时取到的值」。
- **`Value` 的几种聚合类型**：`Value::Dict`（字典）、`Value::Content`（内容元素）、`Value::Module`（模块）——它们内部都装着更多 `Value`，这是递归检查的基础。

> 名词速查
> - **`EmptyIntrospector`**：一个「永远查不到东西」的内省器。正常的文档求值会查询已渲染出的元素（页码、定位等）；而 IDE 在只分析单个表达式时并没有完整渲染结果，于是用一个空的占位。
> - **`Route`**：求值调用栈深度追踪，用于防止无限递归；这里用 `Route::default()` 表示从顶层开始。
> - **`Scope`（作用域）**：名字到绑定（`Binding`，可读出 `Value`）的容器。标准库的 `library.global` 与 `library.math` 各有一个 `Scope`。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但它被几乎所有其它能力文件引用：

| 文件 | 作用 |
| --- | --- |
| [src/utils.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs) | 工具集：`with_engine`、`globals`、`check_value_recursively`、`summarize_font_family`、`MetricRange` |

调用关系（「谁用了 utils」）一览，帮助你理解它在全局中的位置：

| utils 函数 | 主要消费方 | 说明 |
| --- | --- | --- |
| `with_engine` | `analyze.rs`（`analyze_import`） | 创建临时引擎跑真正的求值（如执行 import） |
| `globals` | `analyze.rs`（`analyze_expr_with_fallback`）、`complete.rs`（`scope_completions`）、`definition.rs` | 按语法模式拿标准库作用域 |
| `check_value_recursively` | `complete.rs`（`scope_completions`） | 递归判断值是否满足过滤条件 |
| `summarize_font_family` | `tooltip.rs`（`font_tooltip`）、`complete.rs`（`font_completions`） | 生成字体家族摘要文案 |

可见 `utils.rs` 虽然没有顶层 `pub use` 暴露给外部使用者（回顾 u1-l1 的「实现私有、接口精简」），却是各能力模块共享的后勤。

---

## 4. 核心概念与源码讲解

### 4.1 with_engine —— 创建临时引擎

#### 4.1.1 概念说明

很多 IDE 功能需要「真正求值一下」才能得到答案——例如 import 一个模块，必须执行文件加载与解析。而 Typst 的求值入口是 `Engine`，它有六个字段，缺一不可。问题在于：IDE 场景并没有一个长期存活的 `Engine`，也不应该为了补全一个 import 路径就跑一遍完整的文档编译。

`with_engine` 就是为这类需求设计的「一次性引擎工厂」：它借 world 已有的 `library`，再把其余字段都填成「空/默认」的实现，临时拼出一个最小可用的 `Engine`，跑完一个闭包后即丢弃。

#### 4.1.2 核心流程

伪代码描述：

```text
fn with_engine(world, f):
    introspector = EmptyIntrospector   # 查不到任何元素，但不会崩
    traced      = Traced::default()    # 不预先标记任何 span
    sink        = Sink::new()          # 新建一个空 sink，准备接住 trace 结果
    engine = Engine {
        library,                       # 复用 world 的标准库（不重新构建）
        world:   world.upcast().track(),   # 复用 world（comemo 跟踪）
        introspector: ...track(),
        traced:  ...track(),
        sink:    ...track_mut(),
        route:   Route::default(),     # 从顶层开始，调用深度为 0
    }
    return f(&mut engine)              # 跑闭包，返回其结果
```

关键点：

- **复用 `library` 与 `world`**：不重新构造标准库，直接用 `world.library()` 与 `world.upcast()`，保证临时引擎看到的世界与真实编译一致。
- **三件「空」实现**：`EmptyIntrospector`、`Traced::default()`、`Sink::new()`。这意味着这个临时引擎**不能**查询已渲染元素（introspector 是空的），但**能**执行普通求值，并且如果调用方提前在 `traced` 里标记了 span，`sink` 还能把命中值接出来（在本讲的 `analyze_import` 用法里没有用这个能力，它只用引擎执行 `import` 本身）。
- **生命周期**：`engine` 是局部变量，闭包结束即销毁，不污染任何全局状态。

#### 4.1.3 源码精读

[src/utils.rs:21-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L21-L38) 定义了 `with_engine`。注意第 25–27 行把三个辅助字段各自造一份「空」，第 28–35 行拼装 `Engine`，第 30 行 `world.upcast().track()` 正是 u1-l2 讲过的「为什么必须有 `upcast`」的落点——`Engine` 需要确切的 `&dyn World`，而 `IdeWorld` 实现者用一行 `self` 就能提供它。

调用方之一在 `analyze.rs`：

[src/analyze.rs:90-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L90-L92) —— `analyze_import` 在推断出 import 源是字符串路径后，用 `with_engine` 跑一次 `typst_eval::import(engine, &path, source_span)`，从而真正加载模块并得到 `Value::Module`。这正是「需要真正求值时复用临时引擎」的典型用法（详见 u2-l4）。

#### 4.1.4 代码实践

实践目标：理解 `with_engine` 的「空 introspector」对功能的影响。

1. 打开 [src/analyze.rs:80-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L80-L93)，确认 `analyze_import` 调用 `typst_eval::import` 时并不依赖任何已渲染元素。
2. 思考：如果某功能想用 `with_engine` 求值一个含 `#context query(<lbl>)` 的表达式，结果会怎样？
3. 需要观察的现象 / 预期结果：由于 introspector 是 `EmptyIntrospector`，任何 `query`/`locate`/`here` 这类依赖已渲染文档的查询都拿不到真实元素，只能得到空或默认结果。**这就是为什么 `with_engine` 只适合「不依赖渲染状态」的轻量求值（如 import、字面量），而不是完整重算。** 复杂表达式的真实运行时值，靠的是 u2-l4 介绍的对**整篇文档**的 `trace`，而不是这里这个空引擎。
4. 结论标注：若你不确定 `query` 在 `EmptyIntrospector` 下的确切返回，请「待本地验证」——但「不能依赖渲染结果」这一设计意图是确定的。

#### 4.1.5 小练习与答案

**练习 1**：`with_engine` 拼出的 `Engine` 有三个字段用「空」实现，分别是什么？为什么 `library` 和 `world` 不能也用空实现？

> **参考答案**：`EmptyIntrospector`、`Traced::default()`、`Sink::new()`。`library` 和 `world` 复用自 `world`，因为求值必须看到正确的标准库与真实文件资源（否则连内置函数、源文件都拿不到），而内省结果对单表达式分析不是必需。

**练习 2**：`Route::default()` 表示什么？

> **参考答案**：调用深度为 0、从顶层开始。`Route` 用于在递归求值时限制深度，防止无限递归；这里从默认值开始，说明临时引擎把闭包里的求值视作一次「顶层」调用。

---

### 4.2 globals —— 按语法模式返回标准库作用域

#### 4.2.1 概念说明

Typst 标准库分两大作用域：

- **全局作用域 `library.global`**：`#` 代码模式里能直接用的名字，如 `rect`、`align`、`red`、`counter` 等。
- **数学作用域 `library.math`**：数学公式（`$...$`）里能直接用的名字，如 `frac`、`sqrt`、`bold`、`bb` 等。

IDE 在做补全或定义跳转时，需要把「当前光标所在语法模式」对应的标准库名字提供给用户。例如在公式 `$x + |$` 处补全，应该出 `library.math` 的内容；而在 `#rect(|)` 处补全，应该出 `library.global` 的内容。`globals` 就是这层「按模式选作用域」的胶水函数。

#### 4.2.2 核心流程

```text
fn globals(world, leaf) -> &Scope:
    library = world.library()
    if leaf.mode_after() == Some(SyntaxMode::Math):
        return library.math.scope()
    else:
        return library.global.scope()
```

核心判定只看 `leaf.mode_after()`：

- 它返回「紧邻该节点之后我们处于哪种语法模式」，类型是 `Option<SyntaxMode>`，其中 `SyntaxMode` 有 `Markup` / `Math` / `Code` 三个变体（见 [typst-syntax/src/lib.rs:43-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/lib.rs#L43-L50)）。
- 当且仅当结果恰好是 `Some(Math)` 时返回 math 作用域；**其它一切情况（包括 `Code`、`Markup`、以及 `None`）都回退到 global 作用域。**

> 为什么把 `Markup` 和 `Code` 都并入 global？因为 typst 的全局标准库本来就是为代码与 markup（含 `#` 嵌入）设计的，只有数学公式是独立的命名空间。`None`（注释、raw 体等无模式位置）回退 global 也只是「给个合理默认」，不影响主路径。

`mode_after` 的细节（重要）：它会**区分对待不同 trivia**——对注释和 raw 体返回 `None`，对空白返回父节点模式，对 `$` 开头返回 `Math`。其语义见 [typst-syntax/src/node.rs:1171-1197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1171-L1197)。这也是为什么 u5-l1 会提到「`mode_after` 既给出语法模式又能排除注释节点」。

#### 4.2.3 源码精读

[src/utils.rs:175-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L175-L182) 即 `globals`。函数很短，但被三个文件共用：

- [src/analyze.rs:64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L64)：`analyze_expr_with_fallback` 在「死代码」里 `analyze_expr` 落空时，回退查 `globals` 里的裸 `Ident` 或单层 `FieldAccess`（见 u2-l4）。
- [src/complete.rs:1456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1456)：`scope_completions` 遍历 `globals` 产出全局补全项（局部命名优先，见 u6-l4）。
- [src/definition.rs:59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L59)：跳转定义里，若 `named_items` 没找到，再尝试在 `globals` 里找（即「标准库符号」的定义，返回 `Definition::Std`，见 u4）。

注意返回类型是 `&'a Scope`，生命周期绑定到 `world`——作用域本身是 `world.library()` 内部已存在的引用，不会额外分配。

#### 4.2.4 代码实践

实践目标：亲手验证 `globals` 在 math 与 global 之间的切换。

1. 阅读上面的 [src/utils.rs:175-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L175-L182)。
2. 构造两个光标场景（用 u1-l3 的 `TestWorld` 或在脑中模拟）：
   - 场景 A：`#rect(|)`，光标在 `rect` 后的参数列表内 → 该处 `mode_after` 是 `Code` → `globals` 返回 `library.global.scope()`，于是能看到 `rect`、`red`、`align` 等。
   - 场景 B：`$frac |$`，光标在数学模式里 → `mode_after` 是 `Some(Math)` → `globals` 返回 `library.math.scope()`，于是能看到 `frac`、`sqrt`、`bb` 等。
3. 需要观察的现象：同样调用 `globals(world, leaf)`，因 `leaf` 不同而返回不同作用域。
4. 预期结果：场景 A 补全项里**不包含** `frac`（除非它也在 global）；场景 B 补全项里**包含**数学专有名字。
5. 若想确认 `library.math` 中确有 `frac` 而 `library.global` 中没有，可在本地用调试打印两边 `scope().iter()` 的名字对照，结果标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：光标停在一段纯注释 `// hi |` 之后，`globals` 会返回哪个作用域？为什么？

> **参考答案**：返回 `library.global.scope()`。因为注释节点的 `mode_after()` 返回 `None`（见 `mode_after` 文档），而 `globals` 里 `None` 走 `else` 分支，回退 global。

**练习 2**：为什么 `globals` 不需要专门处理 `SyntaxMode::Code` 与 `SyntaxMode::Markup` 两个分支？

> **参考答案**：因为这两者共用 `library.global` 作用域，所以只需特判 `Math`，其余一律 global，逻辑最简。

---

### 4.3 check_value_recursively —— 限步递归检查值

#### 4.3.1 概念说明

考虑一个具体需求：用户写 `#rect(fill: |)`，期望补全里出现颜色。标准库里 `red`、`blue` 等是颜色，应该出现——这没问题。但如果用户自己定义了一个字典 `#let palette = (red: red, blue: blue)`，那么这个**字典本身**虽然不是颜色，但它「内部含有颜色」，IDE 也希望把它纳入补全，让用户能写 `#rect(fill: palette.red)`。

「值本身满足条件，或它的某个组成部分满足条件」——这就是 `check_value_recursively` 要做的事。它判断一个 `Value`（或它递归包含的任意子 `Value`）是否能通过给定的谓词（predicate）。

但递归有个风险：值可能自引用、互相嵌套，形成无限结构。于是需要一个**步数上限**来兜底，避免补全时卡死。

#### 4.3.2 核心流程

入口 [src/utils.rs:186-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L186-L195)：

```text
fn check_value_recursively(value, predicate) -> bool:
    searcher = Searcher { steps: 0, predicate, max_steps: 1000 }
    match searcher.find(value):
        Break(matching) => matching
        Continue(())    => false
```

`Searcher::find` 的递归逻辑（[src/utils.rs:209-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L209-L234)）：

```text
fn find(value):
    if predicate(value):          # 命中 → 立即成功
        return Break(true)
    if steps > max_steps:         # 超步数 → 立即放弃（判 false）
        return Break(false)
    steps += 1
    match value:
        Dict(d)     => 递归 d 的所有 value
        Content(c)  => 递归 c 的所有 field value
        Module(m)   => 递归 m.scope() 的所有绑定值
        其它        => 不下钻
    return Continue(())           # 本支未命中，继续外层
```

关键设计：

1. **短路语义**：用 `ControlFlow<bool>` 表达。`Break(true)` 表示「找到，成功」；`Break(false)` 表示「超步数，放弃」；`Continue(())` 表示「本支没找到，回溯继续找兄弟/父级」。
2. **只下钻三类聚合容器**：`Dict`、`Content`、`Module`。其它类型（如 `Str`、`Int`、`Func`）是叶子，不再展开。
3. **步数上限 `max_steps: 1000`**：每访问一个值 `steps += 1`，超过 1000 直接 `Break(false)`。这是防止递归爆炸的安全阀——宁可漏报（某个含颜色的巨型嵌套结构被跳过），也不能让补全卡住。这是一种典型的 **best-effort** 取舍。
4. **模块也能下钻**：注意 `Module` 分支会遍历 `module.scope().iter()`。这意味着「内部含颜色的模块也会出现在颜色补全里」，和字典同理。

#### 4.3.3 源码精读

调用点在 `complete.rs` 的 `scope_completions`：

[src/complete.rs:1426-1431](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1426-L1431) —— 原始 `filter` 被**包了一层**：`let filter = |value: &Value| check_value_recursively(value, &filter);`。源码注释写得很清楚：「当值的任一组成部分匹配，也算匹配。例如 `#rect(fill: |)` 时，除了颜色，也提议含有颜色的字典和模块。」

随后这层包装过的 `filter` 被用于两处：

- [src/complete.rs:1436](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1436)：过滤局部命名项（`named_items` 收集来的，u2-l3）。
- [src/complete.rs:1458](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1458)：过滤全局作用域项（`globals` 提供的，4.2 节）。

`Searcher` 的结构定义见 [src/utils.rs:199-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L199-L203)，`find_iter` 辅助见 [src/utils.rs:236-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L236-L244)，它把 `?` 运算符的 `ControlFlow`「短路传播」用到极致——任意一个子项命中（`Break`）都会立刻把结果抛回最外层。

#### 4.3.4 代码实践

实践目标：用 `check_value_recursively` 解释「含颜色的字典/模块也出现在颜色补全里」。

1. 假设谓词是 `|v| matches!(v, Value::Color(_))`（判断是否为颜色）。
2. 给定 `#let palette = (red: red, blue: blue);` 产生的字典 `Value::Dict { red => Value::Color(red), blue => Value::Color(blue) }`。手动模拟 `check_value_recursively(&palette, is_color)`：
   - `find(palette)`：`predicate(palette)` 为 false；下钻到 Dict，遍历其两个值。
   - `find(red_value)`：`predicate` 为 true → `Break(true)` → 立即短路返回。
3. 结论：`palette` 通过过滤，作为补全项出现。
4. 同理，若 import 了一个含颜色常量的模块 `#import "theme.typ": *`，`Module` 分支会展开该模块作用域，只要其中有颜色，整个模块（或对应的导入项）也会被纳入颜色补全。
5. 需要观察的现象：在 `#rect(fill: |)` 处触发补全时，候选列表里除了 `red`/`blue` 等颜色，还出现用户自定义的、内含颜色的 `palette`（或模块）。
6. 预期结果：列表含颜色与「含颜色的容器」。
7. 超步数边界：「待本地验证」——若构造一个极深嵌套（>1000 层）的字典，预期其因 `max_steps` 被判 false 而不出现，但这属于极端构造场景。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `check_value_recursively` 只对 `Dict`/`Content`/`Module` 下钻，而不对 `Value::Array` 下钻？

> **参考答案**：这是 typst-ide 当前的设计选择（见源码 `match value` 的分支）。颜色补全等场景主要关注「具名字段」容器（字典的键、模块的名字、内容元素的 field），数组的元素没有名字、即便含有颜色也难以作为「`something.xxx`」风格的补全项呈现，因此不下钻。如果你发现实际行为与此不符，请以本地源码为准（「待本地验证」）。

**练习 2**：如果递归中遇到一个自引用导致环（模块 A 引用了模块 B，B 又引用 A），会怎样？

> **参考答案**：`max_steps: 1000` 会兜底——步数耗尽后返回 `Break(false)`，即把该值判为「不匹配」，避免无限递归。这是 best-effort：宁可漏报，也不卡死。

---

### 4.4 summarize_font_family —— 生成字体摘要

#### 4.4.1 概念说明

当用户在字体相关位置悬停或补全时（例如 `#text(font: "Cantarell|")`），IDE 会展示一行简短描述，告诉用户这个字体家族有什么特性：是不是可变字体（variable）、字重范围、字宽范围、是否有斜体/斜倾、是否支持光学尺寸（optical sizing），以及有哪些自定义变化轴（variation axes）。

把「一组 `FontInfo`（同一字体的不同变体，如 Regular/Bold/Italic…）」压缩成这样一行人类可读文本，就是 `summarize_font_family` 的职责。它的输出会出现在两处：

- **悬停** `font_tooltip`（[src/tooltip.rs:279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L279)）；
- **补全** `font_completions`（[src/complete.rs:1126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1126)）。

#### 4.4.2 核心流程

函数遍历传入的每一个 `FontInfo`（代表一个变体），累计以下信息：

```text
for info in variants:
    axes = StandardAxes::parse(info.axes)         # 识别 wght/wdth/ital/slnt/opsz 标准轴
    variable     |= 是否 VARIABLE 标志            # 可变字体？
    has_italics  |= 斜体风格 或 ital 轴存在
    has_obliques |= 斜倾风格 或 slnt 轴存在
    supports_opsz|= opsz 轴存在                  # 光学尺寸
    weight_range.expand(info.variant.weight)     # 累计字重最小/最大
    stretch_range.expand(info.variant.stretch)   # 累计字宽最小/最大
    若有 wght 轴：weight_range 再 expand 其轴范围
    若有 wdth 轴：stretch_range 再 expand 其轴范围
    对每个「非标准」轴：variations[tag] 累计其范围
最后按顺序拼字符串：可变?/变体数 + 字重 + 字宽 + 斜体 + 斜倾 + 光学尺寸 + 各自定义轴
```

输出格式约定（见测试 [src/utils.rs:266-285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L266-L285)）：

| 字体 | 摘要 |
| --- | --- |
| Cascadia Mono | `2 variants. Weight 400–700.` |
| HK Grotesk | `16 variants. Weight 100–900. Has italics.` |
| IBM Plex Sans | `5 variants. Weight 300–700. Stretch 75%–100%.` |
| Cantarell（可变） | `Variable. Weight 100–800.` |
| Fraunces（可变） | `Variable. Weight 100–900. Has italics. Supports optical sizing. SOFT 0–100. WONK 0–1.` |

要点：

- **首词**：可变字体输出 `Variable.`，否则输出 `{count} variant(s).`。
- **度量区间**：字重/字宽/自定义轴用 `MetricRange`（4.5 节）记录 min/max，显示时若 min==max 输出单个值，否则输出 `min–max`（注意是 en dash）。字重和字宽额外用 `display_if_not_default` 跳过「全为默认值」的情况。
- **布尔特性**：斜体/斜倾/光学尺寸只在有就追加 `Has italics.` 等短句。

#### 4.4.3 源码精读

[src/utils.rs:41-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L41-L121) 是完整实现。几处关键：

- 第 56 行 `StandardAxes::parse(&info.axes)`：从字体的轴列表里挑出标准命名轴（wght/wdth/ital/slnt/opsz）。
- 第 63–72 行：先用 `info.variant` 的「静态」字重/字宽扩展区间，再用可变轴的范围进一步扩展——这样可变字体也能显示完整的取值范围（例如 `Weight 100–800`）。
- 第 74–81 行：非标准轴（`!StandardAxes::knows(tag)`）归入 `variations` 这个 `IndexMap`，用轴 tag 作 key 去重累加。
- 第 86–120 行：按「可变/变体数 → 字重 → 字宽 → 斜体 → 斜倾 → 光学尺寸 → 自定义轴」的固定顺序拼字符串。

消费方在 [src/tooltip.rs:279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L279)（把摘要包成 `Tooltip::Text`）与 [src/complete.rs:1126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1126)（把摘要作为补全项 `detail`）。注意 `font_completions` 在 equation show 规则下还会用 `FontFlags::MATH` 过滤，只保留数学字体（见 u6-l3），但摘要函数本身不关心这个过滤。

#### 4.4.4 代码实践

实践目标：通过阅读测试理解摘要格式，并预测一个字体的摘要。

1. 打开 [src/utils.rs:253-287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L253-L287)，这是 `test_summarize_font_family`，它从 `typst_dev_assets::fonts()` 加载真实字体，逐一断言摘要字符串。
2. 运行测试：在 crate 根目录执行 `cargo test -p typst-ide test_summarize_font_family`。
3. 需要观察的现象：测试通过，且你能对照上面的表格，理解每段摘要的来源（变体数、字重范围、可变标志等）。
4. 预期结果：测试绿。若想验证新字体，可在测试里仿照写一行 `assert_eq!(summarize("DejaVu Sans"), "...");`，先手算再对照（手算结果标注「待本地验证」）。
5. 进阶：把 `Cargo.toml` 中 dev-dep `typst-dev-assets` 提供的字体名打印出来（`typst_dev_assets::fonts()`），挑选一个，预测它是否含 `Has italics.`，再用测试验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Fraunces 的摘要里有 `SOFT 0–100. WONK 0–1.`，而 Cascadia Mono 没有？

> **参考答案**：因为 Fraunces 是可变字体，带有名为 `SOFT` 和 `WONK` 的**非标准**自定义轴（不被 `StandardAxes::knows` 识别），它们被收集进 `variations` 并在末尾输出；Cascadia Mono 没有（或只有标准轴），所以末尾无此项。

**练习 2**：一个只有单一字重 400、且不是可变的字体会输出字重信息吗？

> **参考答案**：不会。字重区间虽然会记录 (400, 400)，但 `display_if_not_default` 会过滤掉「min 与 max 都等于默认值（字重默认 400）」的情况，所以摘要里不会出现 `Weight 400`。这一点可对照 IBM Plex Sans 之所以显示 `Weight 300–700` 是因为它的范围跨越了默认值。

---

### 4.5 MetricRange —— 字体度量区间工具

#### 4.5.1 概念说明

`summarize_font_family` 需要在遍历多个变体时，持续维护「字重的最小/最大」「字宽的最小/最大」「每个自定义轴的最小/最大」。这是一个典型的「可空区间累加」需求：初始时区间为空，每来一个值就扩展。`MetricRange<T>` 就是为此设计的内部小工具。

它用 `Option<(T, T)>` 表示区间：`None` 表示还没初始化，`Some((min, max))` 表示已记录的范围。`T` 要求 `Display + Copy + Ord`（既能显示、能复制、能比较大小）。

#### 4.5.2 核心流程

```text
struct MetricRange<T>(Option<(T, T)>)

expand(value):              # 把单个值纳入区间
    expand_range(value, value)

expand_axis(axis, f):       # 把一个可变轴的 [min,max] 纳入区间（经 f 转换）
    expand_range(f(axis.min), f(axis.max))

expand_range(min, max):     # 真正的扩展逻辑
    match self.0:
        None        => self.0 = Some((min, max))
        Some((pmin,pmax)) => self.0 = Some((pmin.min(min), pmax.max(max)))

display():                  # 输出 "min" 或 "min–max"；未初始化返回 None
display_if_not_default():   # 在 display 基础上，过滤掉 min/max 均为默认值的情况
```

要点：

- **惰性初始化**：第一次 `expand_range` 时从 `None` 变 `Some((min, max))`，之后只做 `min`/`max` 的收紧与放宽。
- **`Ord` 决定边界**：用 `prev_min.min(min)` 和 `prev_max.max(max)` 维护区间，依赖 `T: Ord`。
- **两种显示**：`display` 总是输出（若已初始化）；`display_if_not_default` 额外要求 `T: Default`，并跳过全默认区间——这是字重/字宽「默认不显示」的实现。

#### 4.5.3 源码精读

定义与实现见 [src/utils.rs:123-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L123-L172)。注意第 152–154 行 `display` 用了 `typst_utils::display` 构造一个惰性 `Display`：当 `min == max` 输出 `{min}`，否则输出 `{min}–{max}`（en dash `–`，对应测试里 `400–700` 的连字符）。

`display_if_not_default` 在第 159–165 行：先用 `T::default()` 取默认值，再用 `filter` 把「min 与 max 都是默认值」的区间筛成 `None`，最后复用 `display`。

`Default` 实现在第 168–172 行，返回 `Self(None)`——这是「未初始化」的起点。

在 `summarize_font_family` 里有三种用法（[src/utils.rs:51-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L51-L53)）：字重和字宽用 `MetricRange::default()`（即 `MetricRange<FontWeight>` / `MetricRange<FontStretch>`），自定义轴用 `MetricRange<Scalar>`。字重/字宽用 `display_if_not_default`（跳过默认），自定义轴用 `display`（只要有就显示）。

#### 4.5.4 代码实践

实践目标：手算一个 `MetricRange` 的演化过程。

1. 假设某字体家族有三个变体，字重分别是 `400`、`700`、`700`。模拟对 `MetricRange::<FontWeight>::default()` 连续调用三次 `expand`。
2. 步骤推演：
   - 初始 `None`。
   - `expand(400)` → `Some((400, 400))`。
   - `expand(700)` → `Some((400.min(400)? …)` 实为 `Some((min(400,700), max(400,700))) = (400, 700)`。
   - `expand(700)` → `(min(400,700), max(700,700)) = (400, 700)`，不变。
3. 调用 `display_if_not_default()`：默认字重为 400，而这里是 `(400, 700)`，max=700 ≠ 400，**不过滤**，输出 `400–700`。
4. 需要观察的现象：与测试 `summarize("Cascadia Mono") == "2 variants. Weight 400–700."` 一致（Cascadia Mono 有两个变体，字重 400 与 700）。
5. 预期结果：手算区间演化与摘要字符串匹配。
6. 若想验证 `FontWeight::default()` 确为 400，需查 typst 源码，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`MetricRange` 为什么用 `Option<(T, T)>` 而不是直接用 `(T, T)` + 一个 `bool initialized`？

> **参考答案**：用 `Option` 直接编码「未初始化」语义更清晰，且无需为 `T` 提供一个任意的初始占位值——某些 `T`（如自定义的度量类型）未必有合理的默认初值。`None` 天然表示「还没见过任何样本」，省去额外的标志位。

**练习 2**：`display` 与 `display_if_not_default` 的区别是什么？为什么自定义轴用前者、字重字宽用后者？

> **参考答案**：`display` 只要区间已初始化就输出；`display_if_not_default` 还要求 `T: Default` 且区间不全是默认值。字重/字宽有「默认值」（400 / 100%）的概念，对「全默认」的字体不显示更干净；而自定义轴没有「默认度量」的概念（任何存在的非标准轴都值得展示），所以用 `display`。

---

## 5. 综合实践

把本讲的四个工具串起来，设计一个**源码阅读型**综合任务：追踪一次「字体补全」的完整数据流，并解释其中用到了 `utils.rs` 的哪几个工具。

任务背景：用户在编辑器输入 `#text(font: "|")`，光标在引号内，触发字体补全。

请按下列步骤完成：

1. **定位入口**：在 [src/complete.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs) 中找到 `font_completions`（[L1120-L1135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1120-L1135)）。
2. **回答数据来源**：它通过 `self.world.book()`（回顾 u1-l2，`book()` 是 `World` 必填方法）拿到字体簿，遍历 `book.families()`，对每个家族收集变体 `FontInfo`。
3. **调用 utils**：对每个家族的变体调用 `summarize_font_family(variants)`（4.4 节）生成 `detail`，内部又用 `MetricRange`（4.5 节）累计字重/字宽等。
4. **过滤**：若处于 equation show 规则下（`is_in_equation_show_rule`），只保留带 `FontFlags::MATH` 的字体（这是字体补全特有的过滤，不在 utils 内）。
5. **输出**：通过 `str_completion` 把家族名作为补全项 label，`summarize_font_family` 的结果作为 detail。
6. **对比悬停**：同样的字体，在 `#text(font: "Cantarell")` 的 `"Cantarell"` 上悬停时，走的是 `font_tooltip`（[src/tooltip.rs:279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L279)），同样调用 `summarize_font_family`，但包成 `Tooltip::Text`。

产出：

- 画一张「字体补全数据流图」：`IdeWorld::book()` → `FontBook::families()` → `summarize_font_family`（内部 `MetricRange`）→ `Completion.detail`。
- 写一段话说明：在这个数据流里，`with_engine` 和 `globals` **没有**被用到（字体补全不依赖求值引擎或标准库作用域，只依赖字体簿）；而 `summarize_font_family` + `MetricRange` 是核心。这能帮你建立「不同 IDE 功能依赖不同工具子集」的直觉。

> 进阶（可选）：再选一次「颜色补全」数据流 `#rect(fill: |)`，对比它会用到 `globals`（取标准库作用域）+ `check_value_recursively`（容器内含色也补），但**不**用 `summarize_font_family`。两组对比能让你彻底分清 utils 各工具的职责边界。

---

## 6. 本讲小结

- `with_engine` 用 `EmptyIntrospector` + `Traced::default()` + `Sink::new()` 拼出一个一次性临时 `Engine`，复用 world 的 `library`/`world`，供 `analyze_import` 等「需要真正轻量求值」的场景使用；它不能依赖已渲染结果。
- `globals` 依据 `leaf.mode_after()` 在 `library.math.scope()` 与 `library.global.scope()` 之间切换，仅当处于 `Some(Math)` 时返回 math 作用域，其余一律 global。
- `check_value_recursively` 用 `Searcher` + `max_steps: 1000` 递归检查 `Dict`/`Content`/`Module`，让「内含目标类型的容器」也通过过滤，这是 `#rect(fill: |)` 能补出含颜色字典/模块的原因。
- `summarize_font_family` 把一组字体变体压缩成一行摘要（可变/变体数、字重、字宽、斜体、斜倾、光学尺寸、自定义轴），供字体悬停与补全复用。
- `MetricRange<T>` 是支撑摘要的「可空区间累加」工具，用 `Option<(T,T)>` 编码未初始化态，提供 `expand`/`expand_axis`/`display`/`display_if_not_default`。
- `utils.rs` 没有顶层 `pub use`，是各能力模块的共享后勤；不同 IDE 功能依赖它的不同子集。

---

## 7. 下一步学习建议

本讲完成了「语法树定位与表达式分析基石」单元（u2）的最后一块拼图。接下来建议：

- **进入第 3 单元（文档查找与悬停提示）**：先读 u3-l1（`docs`），看 `find_value_docs`/`find_param_docs` 如何为 tooltip 提供文档；本讲的 `summarize_font_family` 会在 u3-l4 的 `font_tooltip` 中再次登场。
- **回顾 u2-l4（analyze）**：如果对 `with_engine` 与 `Traced`/`Sink` 的配合仍有疑问，回到 u2-l4 看 `analyze_expr` 如何用 `Traced::new(span)` + 整篇文档求值来捕获运行时值——它与本讲 `with_engine` 的「空 traced」形成对照。
- **预习 u6-l4（scope_completions 与类型驱动补全）**：本讲的 `check_value_recursively` 和 `globals` 在那里被组装成完整的「类型驱动补全」机制，届时你会看到它们如何与 `cast_completions`、`named_items` 协同。

继续阅读建议源码：[src/complete.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs) 的 `scope_completions`（约 L1426）与 `font_completions`（约 L1120），把本讲的工具放进真实调用链中观察。
