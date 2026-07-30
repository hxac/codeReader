# 追踪机制与 IDE 支持

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Typst 的 IDE「悬停看值（hover/tooltip）」功能在求值器里是如何实现的——它不是另起一套解释器，而是复用 `typst-eval` 的正常求值路径，靠「追踪（trace）」把某个 span 处的值旁路写进一个 `Sink`。
- 解释 `Traced`、`Sink`、`Vm::inspected`、`trace_at`、`trace` 这几个组件如何串联成一条「IDE 指定 span → 求值时埋点 → IDE 读回值」的链路。
- 理解 `Vm::new` 如何用 `engine.traced.get(id)` 按**文件**过滤、决定是否进入 inspect 模式，以及为什么这个过滤还关系到 comemo 缓存失效。
- 掌握 `Expr::eval` 末尾那一行统一的 `trace_at` 如何用「一次 `Option==Span` 比较」满足几乎全语言的追踪约定，以及哪些「绕过分发器」的节点（如 `MathAccess`）必须**手动补点**。
- 解释 inspect 模式下「即使有语法错误也继续求值」的设计动机，以及 `Binding::read_checked`/`capture` 与 `Capturer::Context`（`context {}`）的语义。

本讲依赖 **u1-l4（Eval trait 与 Vm 虚拟机）** 和 **u4-l1（函数调用与参数求值）**，并承接 **u4-l4（CapturesVisitor）** 的捕获结论。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「悬停看值」。** 当你在 Typst 的 IDE（如 typst-lsp / Tinymist）里把鼠标悬停在 `#let x = 1 + 2` 的 `x` 上，编辑器想显示「这个表达式的值是 `3`」。但 Typst 是一门「编译型」文档语言：正常编译只产出 PDF/内容，并不主动暴露中间表达式的值。于是需要一个机制：IDE 告诉编译器「请你顺便把某个位置（span）求出来的值记下来」，编译器在正常求值的过程中把这个值「旁路」收集起来，编译结束后 IDE 再取走。

**span 是什么。** `Span` 是 typst-syntax 给语法树每个节点分配的唯一身份标签（编码了文件 id 与节点在源码中的位置）。求值器里大量用 span 做两件事：给错误定位、做追踪匹配。本讲里你会看到 `Vm::inspected: Option<Span>` 这个字段——它就是「当前正在被 IDE 盯着的那一个 span」。

**旁路收集而不是重新求值。** 关键设计取舍：typst-eval **不**为 IDE 单独跑一遍解释器。它让正常的 `eval()` 多带一个「被追踪的 span」，在每个产生值的表达式处做一次极便宜的比对，命中才把值写进 `Sink`。这样 IDE 功能几乎是「免费搭车」在正常编译流程上的。本讲要拆解的就是这套「搭车」机制。

> 术语速查：`Traced`（被追踪的 span 容器）、`Sink`（只追加的收集槽）、`Vm::inspected`（当前 VM 是否处于追踪模式）、`trace_at`/`trace`（埋点入口）、inspect 模式（`inspected.is_some()` 时的求值模式）、`Capturer`（捕获来源：函数 vs 上下文）。

## 3. 本讲源码地图

本讲涉及的核心文件及其作用：

| 文件 | 在本讲的作用 |
|------|--------------|
| `src/vm.rs` | `Vm` 虚拟机：定义 `inspected` 字段、`trace_at`/`trace` 埋点方法、`Vm::new` 里进入 inspect 模式的判定 |
| `src/lib.rs` | 求值入口 `eval()`：准备 `Engine`/`Vmm`、inspect 模式下豁免语法错误、装配 `Module` |
| `src/code.rs` | `ast::Expr::eval` 末尾统一的 `trace_at`；`Ident::eval` 的 `read_checked`；`Contextual::eval`（`context {}` 用 `Capturer::Context`） |
| `src/math.rs` | `MathAccess::eval` 手动补 `trace_at`（绕过 `Expr::eval` 的典型案例）；`MathFieldAccess::eval` 展示「绕过」如何发生 |
| `src/call.rs` | `CapturesVisitor::capture`：捕获时给 `Binding` 打上 `Capturer` 标记 |
| `crates/typst-library/src/engine.rs` | `Traced`（被追踪 span 容器与 `get(id)` 文件过滤）、`Sink`（`value`/`values`/`MAX_VALUES`） |
| `crates/typst-library/src/foundations/scope.rs` | `Binding` 的 `read`/`read_checked`/`capture`/`write` 与 `Capturer` 枚举 |

## 4. 核心概念与源码讲解

### 4.1 追踪的整体模型：IDE 悬停看值是怎么实现的

#### 4.1.1 概念说明

先看全局。IDE 悬停看值是三方的协作：

1. **IDE 端**：用户悬停在某个 span，IDE 构造一个 `Traced::new(span)`，连同源文件一起传给 `eval()`。
2. **求值端（typst-eval）**：正常求值，但每个产生值的表达式都「顺手」比对一下自己的 span 是不是被追踪的那个；若是，就把值写进 `Sink`。
3. **IDE 端（回收）**：编译结束后，IDE 从 `Sink::values()` 取回所有被记录的值，挑一个显示在 tooltip 里。

整个过程**不改变求值结果**——`Module`/`Value` 的产出和没有追踪时完全一样，追踪只是往 `Sink` 里多写了几条旁路记录。

#### 4.1.2 核心流程

一条典型的追踪链路：

```
IDE: 悬停 span S（属于文件 F）
  ↓ 构造 Traced::new(S)，调用 eval(world, lib, traced, sink, route, source=F)
eval():
  ├─ Engine 持有 traced、sink
  ├─ Vm::new(..., target = root.span()):
  │     inspected = target.id() == F ? traced.get(F) : None
  │     // inspected = Some(S) 当且仅当 S 在文件 F 内
  ├─ 求值 Markup → 每个 Expr 末尾调用 vm.trace_at(expr.span, &value)
  │     若 expr.span == S → vm.trace(value) → sink.value(value, styles)
  └─ 返回 Module（同时 sink 里已经攒下了 S 处的值）
IDE: sink.values() → 取出 (value, styles) 列表 → 选一条显示
```

这里有两个关键设计点会在后续小节展开：`traced.get(id)` 为什么按文件过滤（4.1.3 / 4.2），以及 `trace_at` 为什么几乎零成本（4.2）。

#### 4.1.3 源码精读

先看 `Traced`——它只是「一个 span」的容器，但 `get(id)` 做了文件过滤：

[crates/typst-library/src/engine.rs:120-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L120-L143) — `Traced(Option<Span>)` 持有「全局唯一」的被追踪 span；`get(id)` 只有当该 span 确实属于文件 `id` 时才返回它，否则返回 `None`。注释点明了动机：**只让包含被追踪 span 的那一个文件失效/特判**，其余文件照常走 comemo 缓存。

再看 `Sink`——只追加的收集槽，承担四类东西（introspection、延迟错误、警告、追踪值）。我们只关心追踪值：

[crates/typst-library/src/engine.rs:145-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L145-L167) — `Sink` 内部用 `values: EcoVec<(Value, Option<Styles>)>` 收集被追踪的值，每条还附带当时的样式表（供 IDE 显示「带样式」的值）。

[crates/typst-library/src/engine.rs:230-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L230-L236) — `Sink::value` 把 `(value, styles)` 推入 `values`，但有上限：

[crates/typst-library/src/engine.rs:170-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L170-L171) — `MAX_VALUES = 10`。一个 span 在循环等结构里可能被求值无数次，但 `Sink` 只留前 10 个值，防止内存爆炸；IDE 通常只展示其中一个。

最后 `Sink::values()`（[engine.rs:193-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L193-L196)）以所有权形式吐出整个列表供 IDE 取用。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的链路在源码里逐点对上号。

**操作步骤**：

1. 打开 `src/lib.rs` 的 `eval()`，找到它接收 `traced: Tracked<Traced>` 与 `sink: TrackedMut<Sink>` 两个参数（[src/lib.rs:40-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L40-L47)）。
2. 确认 `Engine` 把 `traced`、`sink` 都装了进去（[src/lib.rs:56-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L56-L63)）。
3. 打开 `src/vm.rs` 的 `Vm::new`，确认 `inspected` 来自 `engine.traced.get(id)`（4.2 节精读）。
4. 打开 `src/code.rs` 的 `Expr::eval`，找到末尾 `vm.trace_at(span, &value)`（4.3 节精读）。
5. 打开 `engine.rs` 的 `Sink::value`，确认写入位置。

**需要观察的现象 / 预期结果**：你能画出一条「参数 `traced` → `Engine.traced` → `Vm.inspected` → `Expr::eval` 的 `trace_at` → `Sink.value` → `Sink.values()`」的完整数据流，且这条流**不参与** `Ok(Module)` 的构造——它是纯旁路。

#### 4.1.5 小练习与答案

**练习 1**：如果 IDE 悬停的 span 位于文件 A，而当前 `eval()` 正在求值被 A 通过 `import` 引入的文件 B，那么求值 B 的那个 `Vm` 的 `inspected` 是什么？为什么？

**答案**：是 `None`。因为 `traced.get(B 的 id)` 会发现被追踪 span 属于 A 而非 B，按文件过滤返回 `None`（见 `engine.rs:140-142`）。这样 B 走完全正常的（无埋点开销的）求值，只有 A 才进入 inspect 模式。

**练习 2**：为什么 `Sink` 要把 `MAX_VALUES` 设成 10 而不是无上限？

**答案**：一个被追踪 span 可能落在 `for`/`while` 循环里被求值成千上万次，无上限会让 `Sink` 无限增长。固定上限既给 IDE 足够样本，又保证内存有界。

---

### 4.2 Vm::inspected、trace_at 与 trace：近乎零成本的埋点

#### 4.2.1 概念说明

求值器在每个产生值的表达式处都「应当」报备自己产出的值，这叫**追踪调用约定（trace contract）**。但绝大多数时候根本没有 IDE 在看，如果每次都真的把值 clone 一份写进 sink，代价不可接受。

typst-eval 的解法是双层闸门：

- `trace_at(span, &value)`：**外层廉价闸门**。只做一次 `self.inspected == Some(span)` 比较；不命中就立刻返回，连引用都不克隆。
- `trace(value)`：**内层昂贵动作**。只有命中时才执行：克隆值、附上当前样式、写进 `Sink`。它被标了 `#[cold]`，告诉编译器这是冷路径。

于是「全量埋点、几乎零成本」：非追踪时每个表达式只多一次 `Option<Span>` 比较；追踪时只对那**一个** span 多花一次 clone + 写入。

#### 4.2.2 核心流程

进入 inspect 模式的判定发生在 `Vm::new`：

```
Vm::new(engine, context, scopes, target):
    inspected = target.id().and_then(|id| engine.traced.get(id))
    // target 是本次求值的根 span（eval 里传 root.span()）
    // 只有当 traced 里确实有一个属于 target 所在文件的 span 时，inspected 才非空
```

每次产生值时：

```
trace_at(span, &value):
    if self.inspected == Some(span):   // O(1) 比较
        self.trace(value.clone())      // 仅命中时克隆

trace(value):  // #[cold]
    sink.value(value, context.styles().ok().map(|s| s.to_map()))
```

非 inspect 模式下 `inspected == None`，于是 `None == Some(span)` 恒为 `false`，`trace_at` 直接短路——这正是「免费搭车」的实现关键。每个表达式的额外开销仅为

\[ O(1)\ \text{比较} \quad\Rightarrow\quad \text{总开销} = O(\text{表达式数}) \times O(1) \]

且常数极小（一次 `Option` 与 `Span` 的等值比较）。

#### 4.2.3 源码精读

先看 `Vm` 的字段定义与 `Vm::new`：

[crates/typst-eval/src/vm.rs:16-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L16-L28) — `inspected: Option<Span>` 字段的注释写明：当它是 `Some` 时，VM 处于追踪模式，会把「该 span 看到的每一个值」记录下来。

[crates/typst-eval/src/vm.rs:32-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L32-L40) — `Vm::new` 里关键一行 `let inspected = target.id().and_then(|id| engine.traced.get(id));`。它把 4.1 讲的文件过滤与「是否进入 inspect 模式」缝合在了一起：`target.id()` 取本次求值根 span 的文件 id，`traced.get(id)` 只在被追踪 span 属于该文件时返回它。

再看埋点的两个方法：

[crates/typst-eval/src/vm.rs:75-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L75-L82) — `trace_at`：注释直接点明「This method (or `trace`) should be called for every value produced by an expression」，即它就是**追踪调用约定的统一入口**。函数体只有一次比较。

[crates/typst-eval/src/vm.rs:84-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L84-L91) — `trace`：`#[cold]` 标注 + 注释「Tracing powers IDE tooltips and hover info」。它把值连同「当前上下文样式」一起写入 sink——这也是为什么 IDE 能显示「带样式」的值。

最后注意 `Vm::bind` 也调用了 `trace_at`：

[crates/typst-eval/src/vm.rs:58-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L58-L73) — 把绑定插入作用域之前，先用 `self.trace_at(var.span(), binding.read())` 报备。这样当 IDE 悬停在 `#let x = 5` 的 `x` 上时，能在「定义位点」直接拿到值 `5`。注意这里用的是 `binding.read()`（纯读取）而非 `read_checked()`——**绑定/定义位点不应触发弃用警告**，弃用警告属于「读取使用」位点（见 4.5）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「非 inspect 模式下 `trace_at` 是空操作」这一论断。

**操作步骤**：

1. 在 `Vm::trace_at`（`vm.rs:78`）的方法体第一行临时加一行日志，例如 `eprintln!("trace_at check: inspected={:?} span={:?}", self.inspected, span);`（**示例代码，仅为观察；勿提交**）。
2. 用任意方式触发一次**非追踪**的求值（例如直接调用 `eval_string`，它内部用 `Traced::default()` 即空追踪，见 4.4）。
3. 观察日志输出中 `inspected` 字段始终为 `None`。

**需要观察的现象**：无论求值多少个表达式，`inspected` 恒为 `None`，因而 `self.inspected == Some(span)` 恒为 `false`，`trace` 永不被调用。

**预期结果 / 待本地验证**：日志会打印很多行（每个产生值的表达式一行），但每次 `inspected` 都是 `None`，印证「全量埋点、但非追踪时零实际开销」。由于这依赖你本地搭建一次求值调用，具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `trace` 被标成 `#[cold]` 而 `trace_at` 没有？

**答案**：`trace_at` 是**每次产生值都会走**的热路径，必须内联且零分支预测代价，不能标冷；`trace` 只在「恰好命中那一个被追踪 span」时才执行，是真正的冷路径，标 `#[cold]` 可让编译器把它移出热路径、优化 `trace_at` 的内联布局。

**练习 2**：`Vm::bind` 里用 `binding.read()` 而不是 `read_checked()`，如果改成 `read_checked()` 会有什么副作用？

**答案**：`read_checked` 会在读取时向 sink 发弃用警告。若某绑定的 `Binding` 带有 `deprecation` 信息，那么仅仅是「定义/捕获」它（而非真正使用它）就会触发警告，这会污染告警语义——弃用警告应当只在用户**读取使用**该名字时产生（见 4.5 的 `Ident::eval`）。

---

### 4.3 Expr::eval 的统一 trace_at 与手动补点（MathAccess）

#### 4.3.1 概念说明

追踪调用约定要求「每个产生值的表达式都调用 `trace_at`」。如果让每类节点的 `Eval` 实现各自记得调用，既容易漏又难维护。typst-eval 的巧妙之处在于：**几乎所有表达式都会流经 `ast::Expr::eval` 这个总分发器**，于是只要在分发器末尾调一次 `trace_at`，就一次性满足了全语言的约定。

但「几乎所有」意味着存在**绕过分发器**的节点——它们不经过 `Expr::eval`，因此享受不到这行统一的 `trace_at`，必须在自己的 `Eval` 实现里**手动补点**。数学模式里的 `MathAccess` 就是官方注释明确记录的典型案例。

#### 4.3.2 核心流程

正常路径（绝大多数表达式）：

```
ast::Expr::eval(self, vm):
    span = self.span()
    value = match self { ... 各变体派发到内层 Eval ... }?.spanned(span)
    vm.trace_at(span, &value)   // ← 统一埋点，满足约定
    Ok(value)
```

绕过路径（`MathFieldAccess` 的 target）：

```
MathFieldAccess::eval(self, vm):
    target = self.target().eval(vm)   // target() 返回 MathAccess，直接调它的 eval
    access_field(vm, target, ...)
    // ↑ target 的求值走的是 MathAccess::eval，没经过 Expr::eval，因此没有统一 trace_at

MathAccess::eval(self, vm):
    value = match self { MathIdent | MathFieldAccess }
    vm.trace_at(self.span(), &value)  // ← 必须手动补点！
    Ok(value)
```

判断准则很简单：**某个 `Eval` 实现若是被「另一个 `Eval` 实现直接 `.eval()`」调用、而非被 `Expr::eval` 的大 match 派发，它就绕过了统一埋点，需自行 `trace_at`。**

#### 4.3.3 源码精读

统一埋点位置——`Expr::eval` 末尾：

[crates/typst-eval/src/code.rs:149-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L149-L154) — 注释直言「This satisfies the obligation to call `Vm::trace` for almost all value-producing expressions!」（这满足了「几乎所有产生值的表达式都要调 `Vm::trace`」的义务）。紧跟一行 `vm.trace_at(span, &value)`。`span` 在 [code.rs:80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L76-L156) 开头由 `self.span()` 取得，确保追踪的是「这条表达式自身」的 span。

再看「绕过」是如何发生的——`MathFieldAccess::eval`：

[crates/typst-eval/src/math.rs:59-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L59-L67) — 第 63 行 `let target = self.target().eval(vm)?;`。这里的 `self.target()` 返回的是 `MathAccess` 节点（见 typst-syntax 的 [ast.rs:963](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L963) `fn target(self) -> MathAccess<'a>`），`.eval(vm)` 分派到的是 `MathAccess::eval`，**不是** `Expr::eval`。于是 target 的求值绕过了 `code.rs:152` 那行统一 `trace_at`。

因此 `MathAccess` 必须自己补点：

[crates/typst-eval/src/math.rs:69-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L69-L82) — 注释写明「We need to call `trace_at` for the value because we did not evaluate via `ast::Expr::eval()`」，第 79 行 `vm.trace_at(self.span(), &value);` 就是补回的埋点。

补充：最外层的 `MathFieldAccess` 本身是会经过 `Expr::eval` 的（[code.rs:107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L107) `Self::MathFieldAccess(v) => v.eval(vm)`），所以它有统一埋点；需要补点的只是它**内部 target**（一个 `MathAccess`）。

#### 4.3.4 代码实践

**实践目标**：学会用「是否经过 `Expr::eval`」这把尺子判断一个节点需不需要手动 `trace_at`。

**操作步骤**：

1. 打开 `code.rs` 的 `Expr::eval` 大 match（[code.rs:85-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L85-L147)），确认 `MathFieldAccess`、`MathIdent` 都是 `Expr` 的变体，会享受末尾统一埋点。
2. 打开 `math.rs`，搜索所有 `impl Eval`，逐一检查：它们的 `eval` 是不是被「另一个节点的 `.eval()`」直接调用了？
   - 你会看到 `MathFieldAccess::eval` 调了 `self.target().eval()`（target 是 `MathAccess`）→ `MathAccess` 需要补点 ✅
   - `MathDelimited::eval` 调的是 `self.body().eval(vm)`，而 `body()` 返回 `Math`（见 `Math::eval` [math.rs:22-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L22-L32)），`Math` 产出的是 `Content` 但**不**作为单个 `Value` 被追踪——这里不补点是否符合约定？请结合「`Math` 不经过 `Expr::eval`」思考（见练习 2）。
3. 在 `MathAccess::eval` 的 `vm.trace_at(...)` 处加注释，写下「为什么这里必须有这一行」。

**需要观察的现象**：你能在仓库里找到**至少一处**「绕过分发器因此手动补点」的代码（即 `MathAccess`），并能用一句话解释它的调用方（`MathFieldAccess::eval` 的 `self.target().eval()`）为何绕过。

**预期结果**：`MathAccess::eval` 是目前 crate 内唯一带「手动补点 + 解释性注释」的节点；这印证了「统一埋点 + 极少数手动补点」的设计。

#### 4.3.5 小练习与答案

**练习 1**：假设新增一个数学节点 `MyMathNode`，它的 `eval` 实现里调用了 `self.inner().eval(vm)`，而 `inner()` 返回一个 `MathAccess`。这会破坏追踪吗？

**答案**：不会，**前提是** `MathAccess::eval` 自己已经补了 `trace_at`（事实如此）。因为「补点」是在被调用方（`MathAccess`）内部完成的，无论谁绕过分发器去调它，埋点都会发生。反过来说，如果某节点既绕过 `Expr::eval` 又不在自己 `eval` 里补点，才会真正漏掉追踪。

**练习 2**：`Math::eval`（[math.rs:22-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L22-L32)）产出一个 `Content`（多个子表达式的 sequence），但它本身没有 `trace_at`，也没有经过 `Expr::eval`。这是不是违反了追踪约定？

**答案**：不构成问题，因为 `Math` 节点**不是用户能悬停的单个表达式值**——它是一个「序列容器」，其 span 通常不对应 IDE 想展示的单个值。用户真正悬停的是序列内的叶子（如某个 `MathIdent`），而那些叶子既可能经 `Expr::eval`（统一埋点）也可能经 `MathAccess::eval`（手动补点）被追踪到。追踪约定的目标是「用户可悬停的值表达式」，而非每一个内部容器节点。

---

### 4.4 inspect 模式：为何有语法错误仍继续求值

#### 4.4.1 概念说明

正常的 `eval()` 在发现语法错误时会立刻返回 `Err`——这是对的，因为语法都错了，求值没意义。但在 inspect 模式下，typst-eval 故意**打破**这条规则：即使有语法错误，也照样继续求值。

原因很现实：IDE 悬停发生在用户**正在编辑**的文件里，而正在编辑的文件几乎总是「半成品」——这里少个括号、那里 `#let x =` 还没写完。如果一有语法错误就放弃求值，IDE 永远无法在这些半成品上提供悬停值，体验会极差。于是 inspect 模式采取「尽力而为」策略：硬着头皮在残缺的语法树上求值，能拿到被追踪 span 的值就拿，拿不到就算了。代价是此时的值**可能不准**，但 IDE 场景下「快速给出一个大概值」远胜于「什么都不给」。

关键点：这条豁免**只**对 inspect 模式生效；正常的命令行编译仍然在有语法错误时立即返回。

#### 4.4.2 核心流程

`eval()` 里的判定（节选）：

```
let (errors, warnings) = root.errors_and_warnings();
for warning in warnings { sink.warn(warning); }
if !errors.is_empty() && vm.inspected.is_none() {
    return Err(errors.into_iter()...);   // 非追踪：有错就返
}
// 追踪模式（inspected.is_some()）：即使有 errors 也继续往下求值
let markup = root.cast::<ast::Markup>().unwrap();
let output = markup.eval(&mut vm)?;
...
```

注意判定条件用的是 `vm.inspected.is_none()`——也就是说「是否豁免」完全由「这次求值有没有进入 inspect 模式」决定，而这又追溯到 `engine.traced.get(id)` 是否命中（4.2）。三者层层咬合：IDE 指了 span → 该文件 `inspected` 非空 → 该文件享受语法错误豁免 → 半成品也能悬停看值。

对比 `eval_string()`（[lib.rs:103-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L103-L175)）：它内部用 `Traced::default()`（[lib.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L140)），即空追踪，所以 `Vm::new` 得到的 `inspected` 恒为 `None`，于是 `eval_string` 永远不豁免语法错误、也永远不追踪任何值——它是「纯求值，不服务 IDE」。

#### 4.4.3 源码精读

判定逻辑在 `eval()` 中段：

[crates/typst-eval/src/lib.rs:71-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L82) — 注释「However, if we're inspecting a span, we keep going with evaluation regardless of syntax errors.」直陈设计意图；代码 `if !errors.is_empty() && vm.inspected.is_none()` 是唯一的判据。

`Vm::new` 的调用与 `inspected` 来源：

[crates/typst-eval/src/lib.rs:65-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L65-L69) — 第 69 行 `Vm::new(engine, context.track(), scopes, root.span())`，把 `root.span()` 作为 `target` 传入；结合 [vm.rs:38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L32-L40) 可知 `inspected` 是否非空，完全取决于被追踪 span 是否落在 `root`（即本文件）里。

`eval_string` 的对照（无追踪）：

[crates/typst-eval/src/lib.rs:139-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L139-L148) — 第 140 行 `let traced = Traced::default();` 后 `traced: traced.track()`，意味着 `eval_string` 构造的 `Engine` 永远拿不到任何被追踪 span，因此其 `Vm` 永远 `inspected = None`，既不豁免也不追踪。

#### 4.4.4 代码实践

**实践目标**：理解「文件过滤 + 豁免」是如何让 IDE 只对「被编辑的那一个文件」做特判的。

**操作步骤**：

1. 阅读注释 [lib.rs:71-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L74)，把它翻译成一句中文需求。
2. 假设 IDE 悬停在文件 `main.typ` 的某个 span 上，而 `main.typ` 里有 `import "utils.typ": helper`，且 `utils.typ` 也含语法错误。请预测：求值 `utils.typ` 时会不会因为它的语法错误而中断？`utils.typ` 的 `Vm.inspected` 是什么？
3. 把你的预测对照 4.1 的练习 1 与本节流程写下来。

**需要观察的现象**：你会得出「`utils.typ` 不享受豁免、它的 `inspected` 为 `None`，所以它若有语法错误，`import` 这一步会按正常路径报错」的结论。这意味着 IDE 的「容错悬停」**只覆盖用户当前正在编辑的文件**，不会让被依赖文件的语法错误被静默吞掉——这是个很合理的边界。

**预期结果**：能清楚区分「主文件（被追踪，豁免语法错误）」与「依赖文件（不被追踪，正常严格）」两种行为。

#### 4.4.5 小练习与答案

**练习 1**：`eval_string` 为什么永远不可能进入 inspect 模式？

**答案**：因为它在 [lib.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L140) 自己构造了一个空的 `Traced::default()`，没有接收外部传入的被追踪 span，所以 `traced.get(id)` 恒为 `None`，`inspected` 恒为 `None`。`eval_string` 的定位是「程序化求值一段字符串」，不服务 IDE 悬停。

**练习 2**：假设把 `eval()` 里的判据从 `vm.inspected.is_none()` 改成「始终为真」（即任何情况都立即返回语法错误），IDE 体验会出什么问题？

**答案**：用户每敲错一个字符（产生临时语法错误），IDE 悬停就完全失效——因为 `eval` 会提前返回，根本走不到能产生追踪值的求值阶段。这正是 inspect 模式要豁免语法错误的原因。

---

### 4.5 值的读取、捕获与 Capturer 的两种语境

#### 4.5.1 概念说明

前面四节讲的都是「值产生后如何被追踪」。本节补上另外两块拼图：

1. **值是如何被读取的**：标识符求值时，`Ident::eval` 用 `read_checked` 读出绑定值——它除了返回值，还会在绑定被标记为「弃用」时向 sink 发警告。这与追踪相关：被读出的值随后会被 `Expr::eval` 末尾的统一 `trace_at` 报备。同时，`Vm::bind` 用的是不带警告的 `read`（4.2 已述）。

2. **捕获（capture）如何影响值的可写性与追踪语境**：闭包和 `context {}` 块都会从外层「捕获」自由变量。被捕获的绑定会被打上 `BindingKind::Captured(capturer)` 标记，变成**只读**；而 `capturer` 区分 `Function`（普通闭包 `=>`）与 `Context`（`context {}` 块）两种语境，既影响「写入被拒」时的错误措辞，也对应两条同构的捕获路径。

本节把「读取」「捕获」「Capturer」三者与追踪/IDE 的关系收口。

#### 4.5.2 核心流程

读取侧：

```
Ident::eval(self, vm):
    Ok(vm.scopes.get(&self).at(span)?         // 逐层查找绑定
          .read_checked((&mut vm.engine, span)) // 读值 + 弃用警告
          .clone())                             // 返回独立副本
// 随后 Expr::eval 末尾的 trace_at(span, &value) 把它报备给追踪
```

捕获侧（两种语境，同一套骨架）：

```
// 普通闭包：(x, y) => body
Closure::eval:
    CapturesVisitor::new(Some(&vm.scopes), Capturer::Function).visit(body)
    // → 收集 captures，每个捕获的 Binding 标 Captured(Function)

// context 块：context { body }
Contextual::eval:
    CapturesVisitor::new(Some(&vm.scopes), Capturer::Context).visit(body)
    // → 收集 captures，每个捕获的 Binding 标 Captured(Context)
    //   并包装成 Closure{ node: ClosureNode::Context(body), .. }
```

被捕获的绑定尝试写入时：

```
Binding::write() 对 Captured(capturer):
    bail!("variables from outside the {function|context expression} are read-only ...")
```

#### 4.5.3 源码精读

读取——`Ident::eval`：

[crates/typst-eval/src/code.rs:158-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158-L170) — 三步：`scopes.get` 查找、`read_checked((&mut vm.engine, span))` 读值并可能发弃用警告、`.clone()`。这里的元组 `(&mut vm.engine, span)` 实现了 `WarningSink`，把弃用警告投递到 `engine.sink`。

`read` 与 `read_checked` 的差别：

[crates/typst-library/src/foundations/scope.rs:295-310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L295-L310) — `read` 纯返回 `&Value`；`read_checked` 在绑定带 `deprecation` 时通过 `sink.emit(...)` 发警告，再返回值。文档注释说明 sink 可传 `()`（忽略）或 `(&mut engine, span)`（发警告）。

捕获——`Binding::capture` 与 `BindingKind`：

[crates/typst-library/src/foundations/scope.rs:248-270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L248-L270) — `Binding` 内部有 `kind: BindingKind`，分 `Normal`（可变）与 `Captured(Capturer)`（只读）两种。

[crates/typst-library/src/foundations/scope.rs:329-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L329-L335) — `Binding::capture(capturer)` 复制自身并把 `kind` 改成 `Captured(capturer)`。这就是 `CapturesVisitor` 收集自由变量时给每个绑定打的标记。

只读约束与差异化错误：

[crates/typst-library/src/foundations/scope.rs:312-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L312-L327) — `write()` 对 `Captured` 直接 `bail!`，错误信息按 `capturer` 取「function」或「context expression」，措辞精确指明「变量来自哪个外部语境」。

`Capturer` 枚举与两种使用点：

[crates/typst-library/src/foundations/scope.rs:353-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L353-L360) — `Capturer { Function, Context }`。

- `Capturer::Function`：用于普通闭包，见 [call.rs:612](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L612)（`Closure::eval` 内）。
- `Capturer::Context`：用于 `context {}` 块，见 [code.rs:387-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L387-L411)（`Contextual::eval` 内）。

`Contextual::eval` 把 `context { body }` 包装成一个**特殊的闭包**（`ClosureNode::Context`），其捕获走与普通闭包同构的 `CapturesVisitor`，只是 `capturer` 换成 `Context`：

[crates/typst-eval/src/code.rs:387-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L387-L411) — 注意 `num_pos_params: 0`、`defaults: vec![]`，说明 context 闭包既无参数也无默认值，纯粹是「带捕获的延迟 body」。它的真正执行发生在排版期的上下文求解阶段（不在本讲范围），但其**变量捕获**已经在求值期由 `CapturesVisitor` 完成。

最后看 `CapturesVisitor::capture` 如何调用 `Binding::capture`：

[crates/typst-eval/src/call.rs:896-917](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L896-L917) — 先用 `internal` 作用域判断「是否是内部绑定」（是则跳过，不算捕获）；否则从 `external` 取出绑定并调 `binding.capture(self.capturer)` 打标记，存入 `captures`。这里的 `self.capturer` 就是上面传入的 `Function` 或 `Context`。

> 与追踪的关系小结：被捕获的值，在闭包/context 体内被 `Ident::eval` 读取时，依然会经 `Expr::eval` 的统一 `trace_at` 报备——所以追踪对「闭包体内悬停」也有效。唯一特殊的是 `context {}`：它的 body 是**延迟**求值的，因此对其内部 span 的追踪值要等到排版期上下文求解时才可能产生，这是 `Capturer::Context` 与 `Capturer::Function` 在追踪时序上的本质差异。

#### 4.5.4 代码实践

**实践目标**：通过一个「故意写错」的小实验，看清 `Captured` 只读约束与差异化错误信息。

**操作步骤**：

1. 准备一段 Typst 源码（**示例代码，用于在 Typst 中运行观察，不是 typst-eval 的源码**）：

   ```typst
   #let outer = 10
   #let f = () => {
     outer = 20   // 试图修改从外层捕获的变量
   }
   #f()
   ```

2. 对照 [scope.rs:312-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L312-L327) 预测错误信息里会出现哪个词（`function` 还是 `context expression`）。
3. 把同样的结构改写进 `context` 块：

   ```typst
   #let outer = 10
   #context {
     outer = 20   // 在 context 块里修改捕获变量
   }
   ```

   再次预测错误信息里的措辞。
4. （可选）在本地 Typst 环境运行上述两段，核对预测。

**需要观察的现象**：第一段应报「variables from outside the **function** are read-only and cannot be modified」；第二段应报「variables from outside the **context expression** are read-only and cannot be modified」。

**预期结果 / 待本地验证**：错误措辞的差异完全由 `Capturer` 决定——这印证了 4.5.3 所述「同一套捕获骨架，两种语境标签」。运行结果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`Ident::eval` 用 `read_checked`，而 `Vm::bind` 用 `read`。如果某个绑定是**弃用**的，这两处分别在什么时候触发警告？

**答案**：`read_checked`（在 `Ident::eval` 里）会在用户**读取使用**该名字时触发弃用警告；`read`（在 `Vm::bind` 里）在定义/绑定报备追踪值时**不**触发警告。因此弃用警告只贴在「使用点」，不会被「定义点」重复触发。

**练习 2**：`Closure::eval`（普通闭包）和 `Contextual::eval`（`context {}`）都用了 `CapturesVisitor`，二者传入的 `Capturer` 不同。除了影响错误措辞，这个标签还会通过什么途径影响程序行为？

**答案**：`Capturer` 被写进 `BindingKind::Captured(capturer)`，而 `Binding` 派生了 `Hash`/`Eq`（[scope.rs:249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L248-L261)），因此它会参与绑定的相等性/哈希判定，进而影响依赖 `Binding` 相等性的缓存与去重。此外，在追踪时序上，`Context` 的 body 延迟到排版期求解，而 `Function` 的 body 在调用时立即求值——这导致二者体内 span 的追踪值产生时机不同。

---

## 5. 综合实践

把本讲所有知识点串起来，完成下面这个「IDE 悬停求值」全链路追踪任务。

**场景**：用户在 Tinymist 里打开了 `main.typ`，内容如下，并把鼠标悬停在第 2 行的 `x` 上：

```typst
#let x = 1 + 2
#x
```

**任务**（全部为源码阅读型，无需运行）：

1. **入口参数**：IDE 会以怎样的 `traced` 调用 `eval()`？指出 `target` 是什么（提示：`eval` 里 [lib.rs:65-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L65-L69) 传入的 `root.span()`）。

2. **进入 inspect 模式**：逐步解释 `Vm::new`（[vm.rs:32-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L32-L40)）如何得到 `inspected = Some(第2行 x 的 span)`。要点：为什么 `traced.get(main.typ 的 id)` 返回 `Some`？

3. **两处可能的命中**：被追踪的 `x`（第 2 行）在求值过程中会出现在两处 span 上——
   - `#let x = 1 + 2` 里**定义位点**的 `x`：由谁触发 `trace_at`？（提示：`Vm::bind`，[vm.rs:58-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L58-L73)）
   - `#x` 里**使用位点**的 `x`：由谁触发 `trace_at`？（提示：`Ident::eval` 读出值后，`Expr::eval` 末尾统一埋点，[code.rs:149-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L149-L154)）
   
   这两处 span 不同，但 IDE 只悬停在第 2 行的 `x`。请判断：哪一处会真正把值写进 `Sink`？为什么另一处不会？（关键：`trace_at` 的 `inspected == Some(span)` 精确匹配）

4. **值的写入与回收**：命中的那次 `trace_at` 会调用 `trace`（[vm.rs:84-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L84-L91)），进而 `sink.value(...)`（[engine.rs:230-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L230-L235)）。编译结束后 IDE 通过 `sink.values()` 取回——它会看到值 `3` 吗？

5. **容错性验证**：假设用户手滑把第 2 行写成了 `#x +`（语法错误）。依据 [lib.rs:71-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L82)，求值会中断吗？IDE 还有机会拿到 `x` 的值吗？

**参考结论**：

1. `traced = Traced::new(第2行 x 的 span)`；`target = source.root().span()`，即整个 `main.typ` 根节点的 span。
2. `target.id()` 得到 `main.typ` 的 `FileId`；`traced.get(该 id)` 发现被追踪 span 恰属于 `main.typ`，返回 `Some(第2行 x 的 span)`，于是 `inspected` 非空。
3. 只有**使用位点**（第 2 行 `#x` 的 `x`）会命中，因为它的 span 正好等于被追踪 span，`trace_at` 触发 `trace` 写入。定义位点（第 1 行 `let x` 的 `x`）span 不同，`inspected == Some(span)` 不成立，不写入。
4. 会。`Ident::eval` 经 `scopes.get` 找到 `x` 的绑定（值 `3`），`read_checked` 读出，`.clone()` 后由 `Expr::eval` 末尾 `trace_at` 命中并写入 sink；IDE 取到 `(3, styles)`。
5. 不会中断。因为 `vm.inspected.is_some()`，`eval` 豁免语法错误继续求值，仍能在第 2 行的 `x` 处产生追踪值——这正是 inspect 模式服务于「编辑中的半成品文件」的设计。

---

## 6. 本讲小结

- IDE 的「悬停看值」**复用** `typst-eval` 的正常求值路径：IDE 传入 `Traced(span)`，求值时把命中 span 的值旁路写进 `Sink`，结束后 IDE 用 `Sink::values()` 回收，全程不影响 `Module` 产出。
- 进入 inspect 模式的唯一判据在 `Vm::new`：`inspected = target.id().and_then(|id| engine.traced.get(id))`。`Traced::get(id)` 的**文件过滤**既决定了「哪个文件享受特判」，也关系到 comemo 缓存只对那一个文件失效。
- 追踪调用约定靠两层闸门实现：「全量埋点、近乎零成本」——`trace_at` 只做一次 `Option==Span` 比较（热路径），命中才调 `#[cold]` 的 `trace` 克隆并写入。
- `ast::Expr::eval` 末尾那行统一的 `vm.trace_at(span, &value)` 一次性满足几乎全语言的约定；凡**绕过** `Expr::eval` 的节点（典型如 `MathAccess`，被 `MathFieldAccess::eval` 的 `self.target().eval()` 直接调用）必须**手动补点**。
- inspect 模式下 `eval()` **豁免语法错误**（`!errors.is_empty() && vm.inspected.is_none()` 才提前返回），以便为正在编辑的半成品文件提供悬停值；`eval_string` 用空 `Traced`，永不进入 inspect 模式。
- 值的读取用 `read_checked`（发弃用警告），定义点用 `read`（不发）；捕获会给 `Binding` 打 `Captured(capturer)` 使其只读，`Capturer` 分 `Function`（闭包）与 `Context`（`context {}`），影响错误措辞与追踪时序。

## 7. 下一步学习建议

- 本讲把「追踪」讲到了求值器层面；若想看 IDE 端如何**消费**这些追踪值（如何选择 `Sink::values()` 里的某一条、如何展示带样式的值），建议阅读 `typst-ide` crate 与 Tinymist/typst-lsp 的相关代码。
- 「运行时安全」是 u6 单元的另一条主线：下一讲 **u6-l3（递归安全、栈增长与缓存）** 会讲解 `route.contains` 的循环求值防护、`check_call_depth` 的调用深度限制、`stacker::maybe_grow` 的防爆栈，以及 `eval`/`eval_closure` 上 `#[comemo::memoize]` 的记忆化——其中 comemo 缓存与本讲反复提到的「`Tracked` 参数参与缓存判等」直接相关，可作为承接。
- 若想加深对捕获与 `context {}` 的理解，可回看 **u4-l4（CapturesVisitor）** 的双作用域判定细节，以及 `Contextual` 在排版期如何被求解（`typst-library` 的 `ContextElem` 与 `realize` 流程）。
