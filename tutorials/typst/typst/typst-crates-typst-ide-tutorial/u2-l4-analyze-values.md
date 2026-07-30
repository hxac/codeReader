# analyze —— 推断表达式可能的值

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `analyze_expr` 的两条求值路径：对字面量直接求值、对复杂表达式回退到 `trace` 追踪。
- 解释 `trace` 机制为什么能拿到「`set` 规则之后」「`context` 之后」的值——它本质上是把整个文档重新求值了一遍。
- 掌握 `analyze_import` 如何借助 `with_engine` 创建的临时引擎，真正执行一次 `import` 得到模块值。
- 理解 `analyze_labels` 返回的 `(labels, split)` 里 `split` 偏移的含义，以及它在标签补全里如何把「文档标签」与「参考文献键」分成两半。
- 能在一段含 `set` 规则或 `context` 的代码里，推断 hover 某个表达式时 `analyze_expr` 会返回哪些值。

## 2. 前置知识

本讲承接 [u2-l1 从光标到语法树节点](u2-l1-cursor-to-syntax-node.md) 与 [u1-l2 IdeWorld —— IDE 功能的数据契约](u1-l2-ideworld-trait.md)。动手前先回忆三件事：

- **`LinkedNode` 与 `ast::Expr`**：上一讲我们用 `node.cast::<ast::Expr>()` 把无类型语法节点强类型转换成表达式视图。本讲的几个函数都以 `node: &LinkedNode` 为输入，第一步往往就是这个 cast。
- **`IdeWorld::upcast()`**：[u1-l2](u1-l2-ideworld-trait.md) 讲过，`IdeWorld` 是 `World` 的子 trait，`upcast()` 把 `&dyn IdeWorld` 变回 `&dyn World`。本讲里 `trace`、`with_engine` 都需要拿到确切的 `&dyn World`，因此会反复出现 `world.upcast()`。
- **求值（eval）vs 解析（parse）**：编译器解析得到语法树，但语法树本身没有「值」。要知道 `#let x = 1 + 2` 里 `x` 等于多少，必须**真正运行一次** Typst 的求值器（evaluator）。IDE 的难点在于：用户只在编辑器里 hover 一个表达式，并不一定想编译整个文档——所以 typst-ide 用了一套「尽量轻、必要时重」的分层策略。本讲就是讲这套策略。

一个贯穿全讲的关键认知：**一个表达式可能有多个值**。比如同一个 span 出现在 `for` 循环体里、或编译器多轮迭代中，它会被求值多次。因此 `analyze_expr` 的返回类型不是单个 `Value`，而是 `EcoVec<(Value, Option<Styles>)>`——一组「值 + 它当时的样式」。

## 3. 本讲源码地图

本讲聚焦两个文件：

| 文件 | 作用 |
| --- | --- |
| `src/analyze.rs` | 定义 `analyze_expr`、`analyze_expr_with_fallback`、`analyze_import`、`analyze_labels` 四个对外函数，是本讲的主体。 |
| `src/utils.rs` | 定义 `with_engine`（创建临时引擎）、`globals`（取标准库作用域）等共享工具，被 `analyze.rs` 依赖。 |

此外会少量引用 `typst`、`typst-eval`、`typst-library` 三个 crate 里的底层设施（`typst::trace`、`Vm::trace_at`、`Traced`、`Sink`），用来解释 `trace` 回退机制「为什么能拿到运行时值」。这些不在 typst-ide 目录下，会给出完整 GitHub 链接并标注「跨 crate 引用」，不逐行展开。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，顺序遵循「先打地基再盖楼」：

1. **`with_engine`**——所有「需要真正求值」的函数共享的临时引擎工厂；
2. **`analyze_expr`**——表达式值推断的主函数（字面量求值 + trace 回退）；
3. **`analyze_expr_with_fallback`**——在 `analyze_expr` 落空时回退到标准库定义；
4. **`analyze_import`**——把一个 `import` 源解析成模块值；
5. **`analyze_labels`**——收集文档标签与参考文献，并用 `split` 偏移把二者分开。

### 4.1 with_engine —— 临时 Engine 工厂

#### 4.1.1 概念说明

Typst 的求值不是「对一个表达式单独算」，而是围绕一个 **`Engine`** 进行的。`Engine` 持有 world（取源码、字体）、library（标准库）、introspector（查询文档布局结果）、route（调用栈，用于检测循环 import）、sink（收集错误/警告/追踪值）等一切求值所需的状态。

但 IDE 场景里，我们只想「顺手」做一件小事——比如执行一次 `import`。为这件事去完整构造一个 `Engine` 是沉重的。`with_engine` 把这件事封装成一个「借一个临时引擎、跑一段闭包、马上还回去」的工厂函数，让 `analyze_import` 等调用方不必关心引擎构造细节。

#### 4.1.2 核心流程

```
with_engine(world, |engine| { ... 用 engine 做事 ... })
  │
  ├─ introspector = EmptyIntrospector   ← 空的内省器：不依赖任何文档布局结果
  ├─ traced      = Traced::default()    ← 不追踪任何 span
  ├─ sink       = Sink::new()           ← 空的值收集器
  ├─ route      = Route::default()      ← 空的调用栈
  │
  ├─ engine = Engine {
  │     library:    world.library(),          ← 复用 world 自带的标准库
  │     world:      world.upcast().track(),   ← 复用 world（注意 .track()）
  │     introspector: EmptyIntrospector.track(),
  │     traced:     Traced::default().track(),
  │     sink:       Sink.track_mut(),
  │     route:      Route::default(),
  │ }
  │
  └─ f(&mut engine)   ← 把引擎交给调用方的闭包，返回其结果
```

这里有两处刻意的设计：

- **`EmptyIntrospector`**：`with_engine` 创建的引擎「假装文档还没排版」，因此任何依赖「文档布局结果」的查询（如 `query`、`here`、页码）都拿不到有效数据。这是有意为之——`analyze_import` 只需要解析模块，不需要文档布局，用空内省器最便宜。
- **`world.upcast().track()`**：`Engine` 字段类型是 `Tracked<dyn World>`（被 comemo 缓存追踪的 world），所以要用 `IdeWorld::upcast()` 先拿到 `&dyn World`，再 `.track()`。

#### 4.1.3 源码精读

[utils.rs:L20-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L20-L38) 定义了 `with_engine`。它把六个字段一一填好，再调用闭包 `f(&mut engine)`。注意 `library` 直接复用 `world.library()`，没有重新构造标准库——这正是它「轻」的原因。

> 跨 crate 参考：`EmptyIntrospector` 与 `Engine` 的字段定义在 `typst` / `typst-library` 里，本讲不展开。

#### 4.1.4 代码实践

1. **实践目标**：理解 `with_engine` 为什么用 `EmptyIntrospector` 而不是真实的 introspector。
2. **操作步骤**：
   - 打开 [utils.rs:L25-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L25-L31)，定位 `introspector = EmptyIntrospector` 这一行。
   - 思考：如果把它换成一个「真实」的 introspector（需要先排版出整个文档），`analyze_import` 解析一个普通文件 import 会变慢多少？它真的需要文档布局结果吗？
3. **需要观察的现象**：你会意识到 `import "foo.typ"` 只依赖 world 能否提供 `foo.typ` 的源码，与文档是否排版完全无关。
4. **预期结果**：结论是 `EmptyIntrospector` 足够且最优——import 解析不需要、也不应该等待排版。本结论属源码阅读型，**待本地验证**（可尝试构造一个依赖 `query` 的 import 场景，观察是否会因空内省器而失败）。

#### 4.1.5 小练习与答案

**练习 1**：`with_engine` 里 `traced = Traced::default()`，这意味着什么？

**答案**：`Traced::default()` 等价于 `Traced(None)`，即「不追踪任何 span」。在这个临时引擎里求值时，没有任何表达式会被 `trace_at` 命中、不会有值被送进 sink。这与 `analyze_expr` 走 `typst::trace` 路径时「专门标记一个 span」形成对比（见 4.2.2）。

**练习 2**：`world.upcast().track()` 这一行，如果漏掉 `.upcast()` 会怎样？

**答案**：`IdeWorld` 不是 `World` 的 `Tracked` 版本，`Engine.world` 字段要求 `Tracked<dyn World>`。必须先用 `upcast()` 把 `&dyn IdeWorld` 收窄为 `&dyn World`，才能 `.track()`。[u1-l2](u1-l2-ideworld-trait.md) 讲过 `upcast` 必填，正是为了此处及 `trace` 这类需要确切 `&dyn World` 的内部调用。

---

### 4.2 analyze_expr —— 表达式值推断

#### 4.2.1 概念说明

`analyze_expr` 是本讲最核心的函数，也是悬停提示（tooltip）、跳转定义（definition）、补全（complete）最常调用的「值推断」入口。它要回答一个问题：**光标处这个表达式，运行时可能等于什么值？**

它的策略是分层的「先轻后重」：

- **第一层（轻）**：如果表达式是字面量（`none`、`auto`、布尔、整数、浮点、数值、字符串），直接按字面量构造 `Value`，根本不启动求值器——这是 O(1) 的。
- **第二层（重）**：否则，启动 `trace` 机制，把整个文档重新求值一遍，捕获这个 span 处产生的值。

返回类型 `EcoVec<(Value, Option<Styles>)>` 之所以是一个「向量」，是因为同一个 span 可能在不同位置（循环体、多轮编译）被求值多次，每次值可能不同；`Option<Styles>` 是该值当时的活动样式（被 trace 一并存入）。

#### 4.2.2 核心流程

```
analyze_expr(world, node):
  ① node.cast::<ast::Expr>() 失败 → 返回空 vec
  ② 匹配字面量分支：
        None   → Value::None
        Auto   → Value::Auto
        Bool   → Value::Bool(..)
        Int    → Value::Int(..)        (取值成功时)
        Float  → Value::Float(..)
        Numeric→ Value::numeric(..)
        Str    → Value::Str(..)
     命中 → 返回 [(val, None)]
  ③ 字面量都不命中，进入特殊重定向：
        a. 节点是 Contextual(#content 包裹的表达式) → 递归 analyze_expr(最后一个子节点)
        b. 节点是 FieldAccess/MathFieldAccess 的「字段」部分(index>0，即点号右边) → 递归 analyze_expr(父节点)
  ④ 都不满足 → typst::trace::<PagedDocument>(world.upcast(), node.span())
```

第 ④ 步是理解「为什么能拿到 set 规则之后的值」的关键。`typst::trace` 的实际工作（跨 crate）是：

1. 新建一个空 `Sink` 和一个 `Traced::new(span)`——后者**把这个 span 标记为「要追踪」**；
2. 调用 `compile_impl` 把**整个文档**重新编译/求值一遍（`world.track()`、`traced.track()` 传进去）；
3. 求值器内部：求值每个表达式时都会调用 `vm.trace_at(span, value)`；当某个表达式的 span 恰好等于被标记的 `span` 时，就把它的值（连同当时的 `styles`）存进 `sink`；
4. 编译结束，返回 `sink.values()`——也就是这个 span 处产生过的全部 `(Value, Option<Styles>)`。

因为整个文档被「真刀真枪」地重新求值了一遍，所以 `set` 规则、`show` 规则、`context`、循环等一切运行时行为都会生效——这正是它能把 `set text(size: 12pt)` 之后的某个函数调用结果算出来的原因。代价是相对昂贵（一次完整编译），所以 typst-ide 只在前面的轻量路径都落空时才走它。

> 跨 crate 引用（不必逐行背）：
> - `typst::trace`：[crates/typst/src/lib.rs:L87-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L87-L95)——构造 Sink + Traced，调用 `compile_impl`，返回 `sink.values()`。
> - `Vm::trace_at` / `Vm::trace`：[crates/typst-eval/src/vm.rs:L75-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L75-L91)——求值时按 span 命中才把值送进 sink。
> - `Traced`：[crates/typst-library/src/engine.rs:L122-L143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L122-L143)——`new(span)` 标记、`get(id)` 在 Vm 创建时取出 `inspected` span。

#### 4.2.3 源码精读

[analyze.rs:L12-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L12-L50) 是 `analyze_expr` 全文。几个要点：

- 开头 [L16-L18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L16-L18)：cast 失败立刻返回空 vec，说明「这不是表达式」时干脆放弃。
- [L20-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L20-L49) 的 `match expr` 把七种字面量各映射成一个 `Value`，命中后统一走末尾 `eco_vec![(val, None)]`（[L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L49)）。注意字面量的 `Styles` 恒为 `None`——字面量不依赖样式。
- [L28-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L28-L46) 是兜底分支 `_ =>`：
  - `Contextual` 节点（即 `#context(..)` 包裹）取最后一个子节点递归（[L29-L33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L29-L33)）；
  - 字段访问的「字段」部分（点号右边那个 ident）则上提到整个 `FieldAccess` 再分析（[L35-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L35-L43)）——这样 hover `a.b` 的 `b` 时，分析的是整个 `a.b` 而不是孤立的 `b`；
  - 其余最终落到 [L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45) 的 `typst::trace::<PagedDocument>(world.upcast(), node.span())`。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「`set` 规则之后的函数调用值能被 `analyze_expr` 拿到」，并解释为什么。
2. **操作步骤**：
   - 阅读源码确认：`analyze_expr` 对非字面量表达式最终调用 `typst::trace::<PagedDocument>(world.upcast(), node.span())`（[analyze.rs:L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45)）。
   - 跟进 `typst::trace`（[crates/typst/src/lib.rs:L87-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L87-L95)）：它用 `Traced::new(span)` 标记目标 span，然后调用 `compile_impl` 把**整个文档**重新求值一遍。
   - 设想一段代码：

     ```typst
     #set text(size: 12pt)
     #let s = text.size      // 一个对函数调用/字段访问的结果
     #s
     ```

     思考：hover 在 `s`（或 `text.size`）上时，`set text(size: 12pt)` 是否会生效？
3. **需要观察的现象**：因为 `trace` 触发的是一次完整的 `compile_impl`，`set` 规则在这次求值里照常应用，所以 `s` 被求值时拿到的是 `12pt`，而不是默认字号。
4. **预期结果**：hover 显示 `12pt`。**待本地验证**（可在 tinymist/typst-lsp 中打开上述文件并 hover `s`；或在 typst-ide 测试里仿照 tooltip 测试用 `TestWorld` 断言 `analyze_expr` 的返回值包含 `12pt`）。这正说明：`analyze_expr` 之所以「知道」set 之后的值，是因为它把整篇文档真正重算了一遍，而不是静态推断。

#### 4.2.5 小练习与答案

**练习 1**：`#let x = 1 + 2`，hover 在 `x` 的使用处，`analyze_expr` 返回什么？走的是哪条路径？

**答案**：`x` 是一个 `Ident`，不是字面量，于是走第 ④ 步 `trace`。`trace` 重算文档时，求值 `x` 处的访问会命中被追踪的 span，捕获到绑定值 `3`（整数相加的结果）。最终返回 `[(Value::Int(3), None)]`。这正是悬停时显示 `3` 的来源。

**练习 2**：为什么字面量分支不附带 `Styles`（返回 `(val, None)`），而 `trace` 分支的值可能带 `Some(styles)`？

**答案**：字面量的值与当前活动样式无关（`3` 就是 `3`），所以 `None`。而 `trace` 在求值时通过 `Vm::trace` 把「当时的活动样式 `context.styles()`」一并存入 sink（见 [vm.rs:L87-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L87-L91)）。下游（如长度 tooltip 的单位换算）可以利用这份样式信息。

**练习 3**：`a.b` 里 hover 在 `b` 上，`analyze_expr` 分析的是 `b` 还是 `a.b`？

**答案**：分析的是整个 `a.b`。因为 `b` 是 `FieldAccess` 节点中 `index > 0` 的子节点，命中 [analyze.rs:L35-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L35-L43) 的特殊重定向，会 `return analyze_expr(world, parent)` 上提到父节点。

---

### 4.3 analyze_expr_with_fallback —— 回退到标准库

#### 4.3.1 概念说明

`analyze_expr` 有一个盲区：**死代码**。如果某段表达式根本没被求值到（比如写在一个永远不会执行的分支里），`trace` 重算文档时也不会捕获到它的值，`analyze_expr` 就返回空。

但用户在编辑器里 hover 一个标准库函数（如 `#rect`、`#math.pi`）时，仍然希望看到结果。`analyze_expr_with_fallback` 就是干这件事的：先试 `analyze_expr`，拿不到就**回退到标准库定义**（`globals`），从全局作用域里把这个名字读出来。文档注释里明确说它 "gives us best-effort results in dead code"。

#### 4.3.2 核心流程

```
analyze_expr_with_fallback(world, node) -> Option<Value>:
  ① 若 analyze_expr(world, node) 非空 → 取第一个值返回
  ② 否则取 globals = utils::globals(world, node)
  ③ match node.cast::<ast::Expr>()?:
        Ident(ident)              → globals.get(&ident)?.read()
        FieldAccess(access)       → 目标须是 Ident:
              globals.get(&target)?.read().scope()?.get(&field)?.read()
        其它                      → None
```

注意它只处理两种形状：裸标识符 `Ident`，和「目标也是标识符」的 `FieldAccess`（如 `math.pi`，但不能是 `foo.bar.baz` 这种多层）。其它形状直接放弃。

#### 4.3.3 源码精读

[analyze.rs:L52-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L52-L77) 是 `analyze_expr_with_fallback` 全文。

- [L60-L62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L60-L62)：先吃 `analyze_expr` 的结果，有就直接用——这是「正常路径优先」。
- [L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L64)：取标准库作用域。`globals`（[utils.rs:L174-L182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L174-L182)）会根据 `leaf.mode_after()` 决定返回 `library.math.scope()` 还是 `library.global.scope()`。
- [L65-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L65-L74)：`Ident` 直接在 globals 里查；`FieldAccess` 则先取目标 ident 的值，再 `.scope()?.get(&field)` 二次下钻（这正是「math.pi = globals[math].scope[pi]」的实现）。

#### 4.3.4 代码实践

1. **实践目标**：体会「死代码回退」的设计意图。
2. **操作步骤**：
   - 阅读 [analyze.rs:L56-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L56-L60) 的文档注释，注意 "best-effort results in dead code" 这句话。
   - 设想：hover 一个写在 `if false { #rect }` 里的 `rect`。`trace` 不会求值到它，`analyze_expr` 返回空；此时 `analyze_expr_with_fallback` 会从 globals 读出 `rect` 的函数值。
3. **需要观察的现象**：即便表达式位于死代码，hover 仍能给出标准库函数的信息。
4. **预期结果**：fallback 成功返回 `Value::Func(..)`。属源码阅读型结论，**待本地验证**（可在 tooltip.rs 现有测试基础上补一个死代码场景断言）。

#### 4.3.5 小练习与答案

**练习 1**：`foo.bar.baz`（三层访问）调用 `analyze_expr_with_fallback` 会得到什么？

**答案**：得到 `None`。回退分支只匹配「目标也是 `Ident`」的 `FieldAccess`（[L67-L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L67-L72)），而 `foo.bar` 不是 `Ident`，命中 `_ => return None`。这种多层访问只能依赖 `analyze_expr` 的 `trace` 路径。

**练习 2**：为什么先试 `analyze_expr` 再回退 globals，而不是反过来？

**答案**：`analyze_expr` 能拿到「用户自己的 `let` 绑定、`set`/`context` 之后」的真实运行时值，比 globals 更贴近用户代码；globals 只是「实在求不到时」的兜底。若反过来优先用 globals，会丢失用户同名覆盖（shadowing）的真实值。

---

### 4.4 analyze_import —— 把 import 解析为模块值

#### 4.4.1 概念说明

`import` 在 Typst 里很灵活，来源既可以是文件路径（`import "foo.typ"`）、包（`import "@preview/..."`），也可以是另一个求值结果为模块的表达式（`import some.module`）。IDE 要支持「import 的悬停/补全/跳转」，就必须把这条 import 实际解析成一个**模块值**（`Value::Module`）。

`analyze_import` 负责这件事。它的输入是 import 语句里「源」对应的那个 `LinkedNode`（可能是字符串字面量，也可能是任意表达式），输出是解析后的模块 `Value`。

#### 4.4.2 核心流程

```
analyze_import(world, source) -> Option<Value>:
  ① source_span = source.span()             ← 记下 span，用于解析相对路径
  ② (source, _) = analyze_expr(world, source).next()?   ← 先推断「源」表达式的值
  ③ 若 source 已经有 scope（本就是模块/类型/符号等）→ 直接返回 Some(source)
  ④ 若 source 是 Value::Str(path):
        with_engine(world, |engine| {
            typst_eval::import(engine, &path, source_span)   ← 真正执行 import
                .ok().map(Value::Module)
        })
  ⑤ 否则 → None
```

关键在于第 ④ 步：文件路径或包名必须**真正执行一次** `typst_eval::import`（在 `with_engine` 借来的临时引擎上），才能把目标文件/包求值成一个模块。`source_span` 在这里不可或缺——相对路径是相对于「import 语句所在文件」解析的，而 span 携带了文件 id 信息。

而第 ③ 步是个快速通道：如果「源」本身已经是个模块（比如 `import math`，或 `import some_func_returning_module()` 经 `analyze_expr` 算出来就是模块），就没必要再当字符串路径处理，直接返回。

#### 4.4.3 源码精读

[analyze.rs:L79-L93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L79-L93) 是 `analyze_import` 全文。

- [L81-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L81-L82)：先存 `source_span`，再用 `analyze_expr` 推断源表达式的值。注意复用了 4.2 的 `analyze_expr`——如果源是字面量字符串，这里走轻量路径直接得到 `Value::Str`。
- [L84-L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L84-L86)：`source.scope().is_some()` 判断已经是模块，快速返回。
- [L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L88)：只接受 `Value::Str` 作为路径；其它类型直接放弃。
- [L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L90-L92)：在临时引擎上调用 `typst_eval::import`，成功则包成 `Value::Module` 返回。`.ok()` 把失败（如文件不存在、循环 import）静默为 `None`，体现 best-effort。

#### 4.4.4 代码实践

1. **实践目标**：理解相对路径为何必须传 `source_span`。
2. **操作步骤**：
   - 阅读 [analyze.rs:L81-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L81-L91)，注意 `source_span` 被原样传给 `typst_eval::import(engine, &path, source_span)`。
   - 设想：`main.typ` 里写 `import "lib/helper.typ"`。若 `main.typ` 与 `lib/helper.typ` 处于不同的目录层级，解析 `lib/helper.typ` 这个相对路径时，编译器需要知道「这句话写在哪个文件里」。
3. **需要观察的现象**：`source_span` 携带 import 语句所在文件的 `FileId`，`typst_eval::import` 据此把相对路径锚定到正确目录。
4. **预期结果**：相对路径能被正确解析。属源码阅读型结论，**待本地验证**（可用 [u1-l3](u1-l3-build-and-testworld.md) 的 `TestWorld::new(..).with_source(..)` 构造跨目录 import 场景，断言 `analyze_import` 返回非 `None`）。

#### 4.4.5 小练习与答案

**练习 1**：`import "missing.typ"`（文件不存在）时 `analyze_import` 返回什么？为什么不会 panic？

**答案**：返回 `None`。`typst_eval::import` 返回 `Err`，被 `.ok()` 转成 `None`（[L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L91)）。这是 best-effort：IDE 不应因为一个坏 import 而崩溃，下游（tooltip/complete）见到 `None` 自然降级即可。

**练习 2**：`import math`（导入已有的标准库模块）走的是第几步？

**答案**：走到第 ③ 步快速返回。`math` 经 `analyze_expr`/`analyze_expr_with_fallback` 推断出已经是 `Value::Module`，`source.scope().is_some()` 为真，直接 `return Some(source)`，不会进入字符串路径分支。

---

### 4.5 analyze_labels —— 标签与参考文献的收集

#### 4.5.1 概念说明

Typst 里有两类「可被引用的名字」：

1. **文档元素的标签**：`<fig1>`、`<tab-a>` 这类直接贴在 figure/table/heading 等元素上的标签；
2. **参考文献键**：来自 `#bibliography("refs.bib")` 的 BibTeX/Hayagriva 条目键（如 `einstein1905`）。

IDE 的引用补全（`@einst...`）和标签补全（`<fig...>`）需要把它们都列出来，但**两类适用场景不同**：`<label>` 只能填文档标签，`#cite(<键>)` 只该填参考文献键。为了让消费方灵活取舍，`analyze_labels` 不是简单返回一个扁平列表，而是返回 `(Vec<(Label, Option<EcoString>)>, usize)`——那个 `usize` 就是 **`split` 偏移**：列表前半段是文档标签，后半段是参考文献键。

#### 4.5.2 核心流程

```
analyze_labels(output) -> (Vec<(Label, Option<EcoString>)>, usize):
  introspector = output.as_output().introspector()
  output = []   ; seen_labels = {}

  // 第一阶段：文档元素标签
  for elem in introspector.query_labelled():       ← 所有带 label 的元素
      label = elem.label()?
      if label 已在 seen_labels → 跳过(去重)
      // detail 优先用 figure 的 caption，否则取元素 body 的纯文本
      detail = (figure.caption) 或 elem.body 的 plain_text
      output.push((label, Some(detail)))

  split = output.len()          ← ★ 记下分界点

  // 第二阶段：参考文献键
  output.extend(BibliographyElem::keys(introspector.track()))

  return (output, split)
```

`split` 的设计意图非常清晰：**它之前的都属于文档元素，之后的都属于参考文献**。两个阶段的数据来源也截然不同——文档标签靠 `introspector.query_labelled()`（需要文档已排版，所以本函数要求传入 `output`），参考文献键靠 `BibliographyElem::keys()`（从 bib 文件解析）。

#### 4.5.3 源码精读

[analyze.rs:L95-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L95-L142) 是 `analyze_labels` 全文。

- [L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L105)：从 `output` 取 introspector——注意签名 `output: impl AsOutput`，所以本函数**依赖编译产物**（[u1-l1](u1-l1-project-overview-and-architecture.md) 讲过的可选 `output` 参数）；缺它就没法收集标签。
- [L111-L134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L111-L134)：遍历 `query_labelled()`，用 `seen_labels`（一个 `FxHashSet`）去重——文档注释 [L102-L103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L102-L103) 说明「同名标签只返回第一个」。`detail` 的提取优先尝试 `FigureElem` 的 caption，否则取元素 `body` 字段的 `plain_text`。
- [L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L136)：`let split = output.len();`——这是分界点。注意它记录的是「追加参考文献之前」的长度。
- [L139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L139)：`BibliographyElem::keys(..)` 追加参考文献键；这些键的 `detail` 为 `None`（注意它们没有 caption/body 纯文本那套处理）。
- [L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L141)：返回 `(output, split)`。

#### 4.5.4 代码实践

1. **实践目标**：解释 `split` 前后分别是哪类条目，以及消费方如何据此分半。
2. **操作步骤**：
   - 先看 `analyze_labels` 的返回结构 [analyze.rs:L136-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L136-L141)：`split` 之前是 `query_labelled()` 收集的文档标签，之后是 `BibliographyElem::keys()` 追加的参考文献键。
   - 再看消费方 `complete.rs` 的 `label_completions`：[complete.rs:L1218-L1232](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1218-L1232) 用 `(skip, take)` 三选一：
     - `@` 引用场景 → `(0, usize::MAX)`：全部都要；
     - `citation`（`#cite` 场景）→ `(split, usize::MAX)`：只要参考文献键（跳过前半）；
     - 其它（`<label>` 场景）→ `(0, split)`：只要文档标签（不要参考文献）。
3. **需要观察的现象**：`split` 把一份列表切成「文档标签 | 参考文献键」两段，消费方用 `skip/take` 各取所需，无需 `analyze_labels` 为每种上下文单独跑一遍。
4. **预期结果**：在 `<fig` 处补全只见文档标签；在 `#cite(<` 处只见 bib 键；在 `@` 处两者都见。**待本地验证**（可用 `TestWorld` 配 bib 资源构造一个含 figure + bibliography 的场景，对比三种光标下的补全列表）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `analyze_labels` 不直接返回「标签列表 + 参考文献列表」两个 vec，而要用一个 `split` 偏移？

**答案**：两类条目结构相同（都是 `(Label, Option<EcoString>)`），合并成一个 vec + 偏移更省内存、也便于消费方用统一的 `skip/take` 切片处理（见 `label_completions`）。`split` 用一个 `usize` 就编码了「分界在哪」，是轻量而清晰的设计。

**练习 2**：若文档里两个元素都贴了同名 `<intro>` 标签，`analyze_labels` 返回几个？

**答案**：只返回第一个。`seen_labels.insert(label)` 命中重复时 `continue`（[L113-L115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L113-L115)）。文档注释也明确说 "When multiple labels in the document have the same identifier, this only returns the first one."

**练习 3**：`analyze_labels` 能在「没有编译产物 `output`」时工作吗？

**答案**：不能。它第一行就要 `output.as_output().introspector()`，而文档标签来自 `query_labelled()`——这必须依赖已排版的文档。这也是为什么本函数签名是 `output: impl AsOutput` 而非 `world`。没有 output 时，引用补全/标签 tooltip 等功能会整体降级（见 [u1-l1](u1-l1-project-overview-and-architecture.md) 的可选 `output` 设计）。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「全链路阅读 + 推断」任务。

**场景**：给定下面这段 Typst 代码（假设已排版出 `output`）：

```typst
#import "lib.typ": greeting
#set text(size: 12pt)
#let fig = figure(image("a.png"), caption: [封面]) <cover>

当前字号：#context text.size
引用：@cover 与 #cite(<einstein1905>)
```

请你：

1. **import 解析**：hover 在 `greeting` 上想看它的值。说明 `definition.rs` / `matchers.rs` 会如何调用 `analyze_import(world, &"lib.typ"节点)`，并描述它内部 `with_engine` + `typst_eval::import` 的执行链。参考 [analyze.rs:L79-L93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L79-L93) 与 [utils.rs:L20-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L20-L38)。
2. **值推断**：hover 在 `text.size` 上。说明 `analyze_expr` 为何返回 `12pt` 而非默认字号——指出它走的是 `trace` 路径，而 `trace` 会带着 `set text(size: 12pt)` 重算整篇文档。参考 [analyze.rs:L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45) 与 [crates/typst/src/lib.rs:L87-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L87-L95)。
3. **标签收集**：在 `@cov|` 处触发引用补全。说明 `analyze_labels(output)` 返回的列表里 `split` 之前、之后分别是什么；并解释 `label_completions` 在 `@` 场景为何用 `(0, usize::MAX)` 取全部、而在 `#cite(<|)` 场景用 `(split, usize::MAX)` 只取后半。参考 [analyze.rs:L136-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L136-L141) 与 [complete.rs:L1218-L1232](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1218-L1232)。

**验收标准**：能把上面三处分别对应到 `analyze_import`、`analyze_expr`(trace)、`analyze_labels`，并说清「为什么 import 要真正求值、为什么 trace 能拿到 set 之后的值、为什么 split 要把标签和文献分开」。涉及运行现象的标注「待本地验证」。

## 6. 本讲小结

- `analyze_expr` 是值推断主入口：**字面量直接求值**（轻、`Styles` 恒 `None`），**复杂表达式回退到 `typst::trace`**（重，但能拿到 `set`/`context` 之后的真实运行时值）。
- `trace` 之所以「无所不知」，是因为它用 `Traced::new(span)` 标记目标 span 后，把**整个文档重新求值一遍**，求值器在命中该 span 时把值连同活动样式存入 `Sink`。
- `analyze_expr_with_fallback` 在 `analyze_expr` 落空时**回退到标准库 `globals`**，为死代码里的 `Ident` / 单层 `FieldAccess` 提供 best-effort 结果。
- `with_engine` 是「借一个临时引擎跑一段闭包」的工厂，用 `EmptyIntrospector` + 空 `Traced` + 空 `Sink`，复用 world 的 library，供 `analyze_import` 等使用。
- `analyze_import` 先用 `analyze_expr` 推断「源」，已是模块则直接返回；否则对字符串路径在临时引擎上执行 `typst_eval::import`（依赖 `source_span` 解析相对路径），失败静默为 `None`。
- `analyze_labels` 返回 `(labels, split)`：`split` 之前是文档元素标签、之后是参考文献键；消费方用 `skip/take` 按上下文（`@` / `<label>` / `#cite`）各取所需。

## 7. 下一步学习建议

本讲建立的「值推断」是后续多个功能的弹药库：

- **悬停提示**：直接读 [u3-l2 tooltip 总入口与分发策略](u3-l2-tooltip-dispatch.md)、[u3-l3 表达式与函数调用的 tooltip](u3-l3-expr-tooltip.md)。`expr_tooltip` 正是把 `analyze_expr` 的多值结果合并、计数（`×N`）、并对长度做单位换算展示。
- **跳转定义**：读 [u4-l2 各类目标的定义解析](u4-l2-definition-targets.md)。`definition` 对 `VarAccess` 的回退链正是 `named_items → analyze_expr(span 比对) → globals`。
- **补全进阶**：读 [u6-l2 字段访问补全](u6-l2-field-access.md)、[u6-l3 import/路径/包/字体/标签补全](u6-l3-import-path-package-font-label.md)。`field_access_completions`、`import_item_completions`、`label_completions` 都以本讲的 `analyze_expr` / `analyze_import` / `analyze_labels` 为数据源。

建议接下来先进入第 3 单元（tooltip），它对本讲的依赖最直接，能立刻把「推断出的值」变成「用户看得见的提示」。
