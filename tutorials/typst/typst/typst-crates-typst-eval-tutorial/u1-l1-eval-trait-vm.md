# Eval trait 与 Vm 虚拟机

## 1. 本讲目标

本讲是 typst-eval 的「两大基石」精读。在前三讲里你已经知道：

- typst-eval 是一个 tree-walking interpreter（树遍历解释器），把 AST 求值成运行时值（[u1-l1](./u1-l1-overview.md)）；
- 源码按 AST 节点类别切分到十几个文件里，唯 `vm.rs` 不为任何节点实现求值（[u1-l2](./u1-l2-module-map.md)）；
- 两个入口 `eval()` / `eval_string()` 会「准备一个 Vm，然后调用 `markup.eval(&mut vm)`」（[u1-l3](./u1-l1-eval-entry.md)）。

但那个 `markup.eval(...)` 是什么？`Vm` 又装了什么东西？本讲就把这两个最根本的抽象讲透。读完本讲你应该能：

1. 解释 `Eval` trait 的设计意图——为什么用关联类型 `Output`、为什么方法签名是 `eval(self, &mut Vm) -> SourceResult<Output>`、各 AST 节点如何统一在「调 `eval`」这一个动作下。
2. 说出 `Vm` 结构体五个字段（`engine` / `flow` / `scopes` / `inspected` / `context`）各自的含义与读写时机。
3. 理解 `Vm::define` / `bind` / `trace_at` / `trace` 四个方法的作用，以及「每个产生值的表达式都必须调用 trace」这条调用约定。
4. 看懂 `hint_if_shadowed_std` 这类小工具如何为诊断追加「修复提示」。

## 2. 前置知识

本讲假设你已经读过 [u1-l3](./u1-l1-eval-entry.md)，熟悉这些概念（不重复展开）：

- **AST 节点**：typst-syntax 解析后得到的语法树节点，如 `ast::Expr`、`ast::Markup`、`ast::Ident` 等。它们都是枚举类型，本身只携带结构信息，不含「如何求值」的逻辑。
- **`Engine`**：求值引擎句柄，打包了 `world`、`library`、`route`（调用链/循环防护）、`sink`（诊断与追踪收集）、`traced`（被检查的 span）、`introspector` 等「环境」。
- **`Scopes` / `Scope` / `Binding`**：作用域栈。变量绑定以 `Binding` 形式存进 `Scopes::top`（最顶层作用域）。
- **`SourceResult<T>`**：`Result<T, EcoVec<SourceDiagnostic>>`，求值可能返回带 span 的源诊断错误。
- **`Span`**：源码位置标记，每个 AST 节点都有一个 span；IDE 的 hover、错误高亮都靠它定位。

如果你对 `Engine`/`Route`/`Sink`/`Traced` 还没概念，先回到 [u1-l3](./u1-l1-eval-entry.md) 的「六步流程」看一遍。

下面用一个心智模型图来定位本讲内容在整个求值流程中的位置：

```
Source 文本
   │  (typst-syntax 解析，不在本 crate)
   ▼
AST 根节点 (ast::Markup)
   │  入口 eval() 在这里「new 一个 Vm」  ←─ u1-l3 已讲
   ▼
markup.eval(&mut vm)  ◄── 这就是本讲的 Eval trait 方法
   │
   │  递归对子节点调 .eval(&mut vm)，期间读写 Vm 的状态
   ▼
Module / Value
```

本讲聚焦的就是上图里 `.eval(&mut vm)` 这个调用约定，以及它反复读写的那个 `vm`。

## 3. 本讲源码地图

本讲只涉及两个文件，它们正是 typst-eval 的「地基」：

| 文件 | 作用 | 本讲关注的内容 |
|------|------|----------------|
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs) | crate 根，定义入口函数与 `Eval` trait | `Eval` trait 定义（第 177–184 行） |
| [`src/vm.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs) | 定义 `Vm` 虚拟机及其方法、`hint_if_shadowed_std` | `Vm` 结构体、`new`/`define`/`bind`/`trace_at`/`trace` |

为说明 `Eval` trait 在实际节点上的「总分发」用法，本讲还会引用 [`src/code.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) 中 `ast::Expr::eval` 的大 match 与 `ast::Ident::eval` 作为示例（但 `code.rs` 的细节留给 [u2-l1](./u2-l1-literals-idents.md)）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **Eval trait**：节点求值的统一抽象。
2. **Vm 结构体及字段**：求值状态的容器。
3. **Vm 的核心方法 define / bind / trace / trace_at**：求值期如何改变状态、如何服务 IDE。
4. **hint_if_shadowed_std**：用「修复提示」增强诊断的小工具。

---

### 4.1 Eval trait：节点求值的统一抽象

#### 4.1.1 概念说明

typst-eval 要对几十种 AST 节点求值：字面量、标识符、数组、字典、`if`、`for`、函数调用、markup 文本、math 方程……如果每类节点都各写一个名字不同的求值函数（比如 `eval_expr`、`eval_markup`、`eval_ident`），调用方就得记住「这个节点该调哪个函数」。

`Eval` trait 把这件事统一成一个方法 `eval`。只要某个 AST 节点类型实现了 `Eval`，调用方就可以无差别地写 `node.eval(&mut vm)`。这就是「多态」在解释器里最朴素也最关键的运用。

它的两个关键设计：

- **关联类型 `type Output`**：不同节点求值后产出**不同类型**的值。例如表达式 `ast::Expr` 产出 `Value`，markup 文本 `ast::Markup` 产出 `Content`，数组字面量产出 `Array`，set 规则产出 `Styles`，参数列表产出 `Args`。用关联类型，调用方在编译期就能知道 `x.eval(&mut vm)` 的返回类型，不需要把一切装箱成 `Value` 再强转，既高效又类型安全。
- **方法签名 `fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>`**：
  - `self`（按值消费）：遍历 AST 节点通常是一次性的，消费语义避免了不必要的克隆，也清楚地表达了「求值走过就走过」。
  - `&mut Vm`：求值会改变虚拟机状态（定义变量、产生控制流事件、写追踪），所以需要可变借用。
  - `SourceResult<...>`：求值可能失败并返回带 span 的诊断，所以包一层 `Result`。

#### 4.1.2 核心流程

解释器的主循环其实就是「递归调 `eval`」：

```text
拿到一个节点 node
  ──► node.eval(&mut vm)
        │
        ├── 对子节点继续调 child.eval(&mut vm)   （递归）
        ├── 读写 vm 的状态（scopes / flow / trace）
        └── 返回 Ok(output)  或  Err(diagnostics)
```

对于**枚举型**节点（如 `ast::Expr` 是一个把所有表达式种类打包的大枚举），它的 `eval` 实现通常是一个 `match`，把每个变体**派发**到该变体自己专门的 `Eval` impl。这种「外层枚举做总分发、每个具体类型各司其职」的模式贯穿全 crate，后续 [u1-l2](./u1-l2-module-map.md) 讲过的 `code.rs` / `markup.rs` / `math.rs` / `flow.rs` 等文件，本质上都是在给不同的 AST 类型补 `impl Eval`。

#### 4.1.3 源码精读

**`Eval` trait 本体**定义在 crate 根：

[`src/lib.rs`:L177-L184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L177-L184) —— 就是上面描述的三行：关联类型 `Output` + 方法 `eval`。注释说得很直白：「Evaluate an expression（求值一个表达式）」。

**总分发的经典例子**是 `ast::Expr::eval`。它先记录 `span`、定义一个 `forbidden` 闭包（用来拒绝 set/show 出现在表达式上下文），然后是一个覆盖全部表达式种类的 `match`：

[`src/code.rs`:L76-L148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L76-L148) —— 注意大部分分支形如 `Self::Int(v) => v.eval(vm)`，即「把这个变体的内层节点 `v` 再 `eval` 一次」。少数分支会加 `.map(Value::Content)`，因为像 `Strong`/`Heading` 这类 markup 节点求值出 `Content`，而 `Expr` 统一要求 `Value`，所以要包一层。最后用 `.spanned(span)` 给结果打上当前节点的 span。

紧跟着的注释点明了一条重要约定：

```rust
// This satisfies the obligation to call `Vm::trace` for almost all
// value-producing expressions!
vm.trace_at(span, &value);
```

[`src/code.rs`:L150-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L150-L152) —— `Expr::eval` 在**所有**分支的末尾统一调用 `trace_at`。这条「调用约定」我们在 4.3 节细讲。

**一个最简单的 impl** 是标识符求值：

[`src/code.rs`:L158-L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158-L170) —— `ast::Ident::eval` 做的事就是「到作用域栈里查这个名字」：`vm.scopes.get(&self)` 取出 `Binding`，`.at(span)?` 把「找不到」这种字符串错误转成带 span 的诊断，`.read_checked(...)` 做必要的读取检查（与追踪/捕获有关，[u6-l2](./u6-l2-tracing-ide.md) 详讲），最后 `.clone()` 得到一个 `Value`。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `ast::Expr::eval` 的大 match，理解「统一抽象 + 分发」与「表达式上下文禁止 set/show」。

**操作步骤**：

1. 打开 [`src/code.rs`:L76-L156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L76-L156)。
2. 数一数：`match self { ... }` 里有几个分支用了 `.map(Value::Content)`、几个用了 `.map(Value::Array)`、几个用了 `.map(Value::Dict)`。
3. 找到 `forbidden` 闭包的定义与它被 `bail!` 触发的两处（`SetRule` / `ShowRule`）。
4. 思考：为什么 set/show 在「表达式」里要报错，而 `eval_code`（[u1-l3](./u1-l1-eval-entry.md) 提到的流式求值）却允许它们？

**需要观察的现象**：

- `.map(Value::Content)` 出现在**产出 Content 的 markup 类节点**上（Text/Strong/Heading/Math 等），因为它们要被「提升」进表达式语境的 `Value` 统一表示。
- `Self::SetRule(_) => bail!(forbidden("set"))` 与 `Self::ShowRule(_) => bail!(forbidden("show"))` 说明：set/show 只能直接出现在 code/content block 的语句流里，不能作为「子表达式」嵌套。

**预期结果**：你会看到表达式总分发器既「能兜住所有种类」，又「守住了语法约束」。待本地验证：如果你想，可以写一个 Typst 片段 `#(set text(size: 12pt))`（把 set 塞进括号表达式），运行 typst CLI 应当报出这条 `forbidden` 错误。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eval` 的签名用 `self`（消费）而不是 `&self`？

> **答案**：因为求值是「一次性遍历」语义——走过一个节点通常不需要再走第二次，消费语义既表达了这个意图，也避免了在递归里反复克隆 AST 节点，提升性能。

**练习 2**：`ast::Expr::eval` 的 `Output` 是什么类型？为什么不直接统一成 `Content`？

> **答案**：`Output = Value`（见 [code.rs:77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L77)）。表达式可能求值成整数、布尔、数组、字典、函数……`Value` 是「任意运行时值」的统称；`Content` 只代表「文档内容」。如果统一成 `Content`，就无法表达 `1 + 2` 这样的数值计算了。

**练习 3**：`forbidden` 闭包在什么条件下触发？它依赖了哪个 span？

> **答案**：当 `ast::Expr` 本身是 `SetRule` 或 `ShowRule` 时触发，分别报 `set` / `show is only allowed directly in code and content blocks`。它用的是表达式自己的 `span`（[code.rs:80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L80)），把错误精确指到那个 set/show 上。

---

### 4.2 Vm 虚拟机：求值状态的载体（结构体与字段）

#### 4.2.1 概念说明

如果说 `Eval` trait 定义了「怎么算」，那么 `Vm` 就是「算的时候需要的环境与状态」。`Vm` 的文档注释说得明白：

> A new virtual machine is created for each module evaluation and function call.
> （每求值一个模块、每调用一次函数，都新建一个虚拟机。）

为什么每个模块/每次调用都要新建？因为它们各自有**独立的**作用域与控制流：模块 A 里定义的变量不能泄漏进模块 B；函数 f 的 `return` 不能跳出到调用者。新建 `Vm`（或新的 `Scopes`）是隔离这些状态的物理手段。

`Vm` 一共五个字段，下表先给个总览，4.2.3 再逐个对照源码：

| 字段 | 类型 | 作用 |
|------|------|------|
| `engine` | `Engine<'a>` | 底层引擎句柄，访问 world/library/route/sink/traced/introspector |
| `flow` | `Option<FlowEvent>` | 当前正在发生的控制流事件（break/continue/return） |
| `scopes` | `Scopes<'a>` | 作用域栈，词法作用域的载体 |
| `inspected` | `Option<Span>` | 当前被 IDE 检查的 span；`Some` 时进入 tracing 模式 |
| `context` | `Tracked<'a, Context<'a>>` | 「幕后」上下文数据（contextual 求值、样式查询等） |

#### 4.2.2 核心流程

`Vm` 的生命周期围绕「构造 → 求值 → 读取结果」三步：

```text
Vm::new(engine, context, scopes, target_span)
   │  · 用 engine.traced.get(id) 决定是否进入 inspect 模式
   ▼
node.eval(&mut vm)   （递归求值，期间读写各字段）
   │
   ▼
读取结果：
   · 模块：读 vm.scopes.top（最外层作用域）装配 Module
   · 控制流：读 vm.flow，若有残留则 forbidden() 报错
```

注意 `Vm::new` 的第四个参数 `target: Span`——它通常是模块根节点的 span，`Vm` 据此判断「这个模块是不是 IDE 当前正盯着看的那个文件」，进而决定要不要开 tracing。这条逻辑承接 [u1-l3](./u1-l1-eval-entry.md) 里讲的「inspect 模式下即使有语法错误也继续求值」。

#### 4.2.3 源码精读

**`Vm` 结构体**：

[`src/vm.rs`:L12-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L12-L28) —— 每个字段都有注释。注意 `flow: Option<FlowEvent>` 的注释是「A control flow event that is currently happening」：它表示「此刻」有没有控制流事件在向上传递，绝大多数时候是 `None`；`inspected` 的注释则点明了 tracing 模式的开关。

**`Vm::new`**：

[`src/vm.rs`:L31-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L31-L40) —— 关键一行：

```rust
let inspected = target.id().and_then(|id| engine.traced.get(id));
```

它的语义是：拿 `target` 这个 span 所属的文件 id 去 `engine.traced` 里查——如果 IDE 正想看这个文件里某个 span 的值，`traced.get(id)` 会返回那个 span，于是 `inspected = Some(span)`，进入 tracing 模式；否则 `None`。`flow` 初始化为 `None`（刚开始没有任何控制流事件）。

入口函数里正是这样创建 `Vm` 的（承接 [u1-l3](./u1-l1-eval-entry.md)，此处不重述整段流程）：[`src/lib.rs`:L69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L69)。

#### 4.2.4 代码实践

**实践目标**：把 `Vm` 的五个字段和「求值期谁读/谁写」对应起来，建立一张状态心智表。

**操作步骤**：

1. 阅读下表（基于 [vm.rs:16-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L16-L28)）。

| 字段 | 典型写入处（举例） | 典型读取处（举例） |
|------|--------------------|--------------------|
| `engine` | 入口装配时填入 | 到处用到：报错 `bail!`、查 library、`route`/`sink` |
| `flow` | `LoopBreak`/`FuncReturn` 等 eval 时写入 | 循环体、函数体执行后消费；模块顶层残留则 `forbidden()` |
| `scopes` | `Vm::bind` 写入最顶层 | `Ident::eval` 通过 `scopes.get` 查找；模块装配读 `scopes.top` |
| `inspected` | `Vm::new` 时由 `traced.get` 设定 | `trace_at` 用它判断是否要 trace |
| `context` | 入口装配时传入 | contextual 求值、样式查询时读取 |

2. 在 [`src/lib.rs`:L89-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L89-L96) 确认：求值结束后，`eval()` 读了 `vm.flow`（判断有没有残留控制流）和 `vm.scopes.top`（装配 Module）。这正是 `Vm` 作为「状态容器」被「读取结果」的两处。

**需要观察的现象**：五个字段都不是「用完即弃的临时变量」，而是跨整个求值过程被反复读写的状态。

**预期结果**：你能向别人解释「为什么 `Vm` 要把这些东西放在一起」——因为它们是同一趟求值共享的全部上下文，放进一个结构体方便在每次 `eval` 调用里以 `&mut vm` 传递。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `flow` 是 `Option<FlowEvent>`？它什么时候是 `None`？

> **答案**：`None` 表示「当前没有控制流事件」，这是常态——大多数语句执行不会触发 break/continue/return。只有当求值到 `LoopBreak` / `LoopContinue` / `FuncReturn` 节点时，才会把对应事件 `Some(...)` 写进 `flow`，向上传递给循环或函数体去消费（详见 [u3-l2](./u3-l2-flow-events.md)）。

**练习 2**：`inspected` 是 `Some(span)` 时，整趟求值会有什么不同？

> **答案**：进入 tracing 模式。之后每次 `trace_at(span, value)` 若命中那个 span，就会把值写进 `engine.sink`，供 IDE 读取做 hover/tooltip。这也是为什么入口 `eval()` 在 inspect 模式下即使有语法错误也继续求值——否则 IDE 就取不到表达式值了（[u1-l3](./u1-l1-eval-entry.md)）。

**练习 3**：为什么每求值一个模块/每调用一次函数都要新建 `Vm`（或新建 `Scopes`），而不是复用一个全局 `Vm`？

> **答案**：为了**状态隔离**。不同模块、不同函数调用各有独立的作用域和控制流；复用全局 `Vm` 会让变量绑定和控制流事件跨边界泄漏（比如函数内的 `return` 跳到调用者）。新建 `Vm`/`Scopes` 是最干净的隔离方式。

---

### 4.3 Vm 的核心方法：define / bind / trace / trace_at

#### 4.3.1 概念说明

`Vm` 不只是被动地「装字段」，它还提供一组方法，在求值期**主动改变状态**或**服务 IDE**。本模块讲四个：

- **`define`**：把一个值绑定到某个标识符（`let x = ...` 的 `x`）。它内部构造一个 `Binding`，然后转交给 `bind`。
- **`bind`**：真正「插入绑定」的动作——把绑定放进 `scopes.top`。但它在插入前还做两件「副作用」：（a）针对名为 `is` 的标识符发一条警告；（b）调用 `trace_at`。这四件事（含插入）的关系是本模块的重点。
- **`trace_at(span, value)`**：**条件式** trace。只有当 `span` 恰好等于 `inspected` 时，才把值交给 `trace`。它存在的意义是：求值每时每刻都在产生值，但只有 IDE 正关心的那一个 span 的值才需要被记录，其余的一律跳过，开销几乎为零。
- **`trace(value)`**：**无条件**把一个值写进 `engine.sink`，驱动 IDE 的 hover/tooltip。它被标了 `#[cold]`（冷代码），因为正常运行（非 IDE 模式）时几乎不会真正执行到这里。

这四者串成一条「求值产生值 → 顺带服务 IDE」的链路。

#### 4.3.2 核心流程

绑定一个变量时的调用链：

```text
Vm::define(ident, value)
   │  构造 Binding::new(value, ident.span())
   ▼
Vm::bind(ident, binding)
   ├── 1. trace_at(ident.span(), binding.read())   ← 命中 inspected 才记录
   ├── 2. 若 ident 名为 "is"：发警告（未来会变关键字）
   └── 3. scopes.top.bind(name, binding)            ← 真正插入作用域
```

而「产生值的表达式」则通过 `Expr::eval` 末尾的统一 `trace_at` 满足 trace 调用约定（见 4.1.3）。这条约定可以一句话概括：

> **每个产生值的表达式，都应当（直接或经由统一入口）调用一次 `Vm::trace`（通常用 `trace_at`）。**

为什么？因为 IDE 可能想 hover **任意**一个表达式来查看它的值。解释器无法预知 IDE 要看哪个，于是「来者不拒」地在每个表达式求值后都 `trace_at`；但 `trace_at` 内部用 `==` 比较 span，绝大多数情况下不命中、直接返回，几乎零成本。一旦命中被检查的 span，才真正把值写进 sink。

#### 4.3.3 源码精读

**`define`**：

[`src/vm.rs`:L47-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L47-L52) —— 一行：用值和标识符 span 构造 `Binding`，再调 `bind`。注释说明它「create a `Binding` with the value and the identifier's span」。

**`bind`（本模块核心）**：

[`src/vm.rs`:L54-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L54-L73) —— 三步，顺序就是上面流程图的 1→2→3：

```rust
pub fn bind(&mut self, var: ast::Ident, binding: Binding) {
    self.trace_at(var.span(), binding.read());          // 副作用①：trace
    // 副作用②：is 警告
    if var.get() == "is" {
        self.engine.sink.warn(warning!( ... ));          // 未来会变关键字
    }
    self.scopes.top.bind(var.get().clone(), binding);    // 真正插入
}
```

注意顺序：`trace_at` 在插入**之前**调用——因为 `binding.read()` 取的是绑定内持有的值引用，与「是否已插入作用域」无关。

**`trace_at`**：

[`src/vm.rs`:L75-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L75-L82) —— 一行 `if` 判断：只有 `self.inspected == Some(span)` 时才 `trace`。注释把它定位为「满足 trace 调用约定的统一入口」。

**`trace`**：

[`src/vm.rs`:L84-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L84-L91) —— `#[cold]` 标注；把值（连同可选的样式 map）写进 `engine.sink.value(...)`。注释：「Tracing powers IDE tooltips and hover info.」

**统一 trace 约定的实例**——`Expr::eval` 末尾：

[`src/code.rs`:L150-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L150-L152) —— 因为 `Expr::eval` 是几乎所有表达式求值的公共出口，在这里调一次 `trace_at(span, &value)` 就替几十种表达式节点一次性满足了「要 trace」的义务（注释里写得很清楚：satisfies the obligation ... for almost all value-producing expressions）。注意「almost」——少数不走 `Expr::eval` 末尾的节点（如 math 里的 `MathAccess`）需要自己手动调 `trace_at`，这点 [u6-l2](./u6-l2-tracing-ide.md) 会展开。

#### 4.3.4 代码实践（本讲的主实践）

**实践目标**：精读 `Vm::bind`，讲清它「插入绑定」之外的两件副作用，并论证 trace 调用约定。

**操作步骤**：

1. 打开 [`src/vm.rs`:L54-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L54-L73)。
2. 找到「插入绑定」的核心语句（`self.scopes.top.bind(...)`，[L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L72)）。
3. 找出除插入外的**两件副作用**：
   - **副作用①**（[L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L59)）：`self.trace_at(var.span(), binding.read())` —— 为 IDE 追踪服务。
   - **副作用②**（[L62-L70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L62-L70)）：若标识符名是 `is`，发一条 warning，提示「`is` 未来会成为关键字，建议改名 `is_`」。
4. 思考：为什么 trace 必须在**每次产生值时**都被调用？（提示：IDE 不知道用户会 hover 哪个表达式；解释器只能「全量埋点 + 按需记录」。）

**需要观察的现象**：

- `bind` 不是「单纯写一下哈希表」，它还兼顾了 IDE 追踪和前瞻性语法警告。
- `trace_at` 用 `==` 比较 span，意味着只有**精确命中**被检查 span 的那次求值才会真正记录，其余全被那行 `if` 挡掉。

**预期结果**：你能用一句话回答——「`Vm::bind` 除插入绑定外，还做了（a）对 `is` 标识符的兼容性警告、（b）一次 `trace_at` 调用；而 trace 之所以要在每次产生值时调用，是因为解释器无法预知 IDE 要观察哪个表达式，只能全量埋点，再靠 `trace_at` 的 span 比较把实际记录成本压到接近零。」待本地验证：若你想确认 `is` 警告，可在 Typst 源里写 `#let is = 1` 并编译，观察是否出现该 warning。

#### 4.3.5 小练习与答案

**练习 1**：`define` 和 `bind` 的关系是什么？为什么分成两层？

> **答案**：`define` 是「面向值」的便捷接口——你给它一个 `impl IntoValue` 的值，它帮你构造 `Binding` 再调 `bind`（[vm.rs:50-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L50-L51)）。`bind` 是更底层的「插入绑定」入口，直接接收一个已构造好的 `Binding`（例如解构赋值、带特殊属性的绑定会绕过 `define` 直接调 `bind`）。分层是为了复用同一套「trace + is 警告 + 插入」逻辑。

**练习 2**：`trace` 为什么标 `#[cold]`？

> **答案**：`#[cold]` 告诉编译器「这个函数很少被真正执行」。正常运行（非 IDE 模式）时 `inspected` 是 `None`，`trace_at` 的 `if` 永远不成立，`trace` 永远不会被调到。把它标冷可以让编译器把热路径（`trace_at` 里不命中的那行 `if`）优化得更紧凑，把 `trace` 的代码挪到一边少占指令缓存。

**练习 3**：`trace_at` 用 `self.inspected == Some(span)` 判断。这依赖 span 的什么性质？

> **答案**：依赖「同一个源码位置在不同求值趟次里得到的是**同一个 `Span` 值**」。typst-syntax 在解析/合成阶段为每个节点分配确定的 span，`inspected` 记录的就是 IDE 想看的那一个 span 值；只有求值到**恰好那个节点**时 `==` 才成立。因此 span 必须是稳定可比较的（它们本质上是对源码区间的编码）。

---

### 4.4 hint_if_shadowed_std：用「修复提示」增强诊断

#### 4.4.1 概念说明

除了 `Vm` 的方法，`vm.rs` 里还导出一个独立函数 `hint_if_shadowed_std`。它体现 typst-eval 诊断哲学的一个小而典型的侧面：**错误不仅要告诉你「错了」，还要告诉你「怎么改」**。

场景：用户在某个作用域里定义了一个和标准库同名的标识符（比如自己 `let array = ...` 覆盖了内置 `array`），后来想调用内置的那个却写成了裸名字，结果报错。`hint_if_shadowed_std` 在这种错误上追加一条提示：「用 `std.<名字>` 访问被覆盖的标准库函数」。

它属于**提示（hint）**而非错误本身——错误信息由调用方提供，这个函数只是在合适时往上贴一条 hint。

#### 4.4.2 核心流程

```text
调用方遇到一个 callee 求值/调用错误（已带 err）
   │
   ▼
hint_if_shadowed_std(vm, callee, err)
   ├── callee 是不是 Ident？
   │     否 ──► 原样返回 err
   │     是 ──► 查 vm.scopes.check_std_shadowed(名字)
   │              被覆盖 ──► err.hint("use `std.<名字>` ...")
   ▼
返回增强后的 err
```

#### 4.4.3 源码精读

**`hint_if_shadowed_std`**：

[`src/vm.rs`:L94-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L94-L109) —— 逻辑很短：

```rust
pub fn hint_if_shadowed_std(vm, callee, mut err) -> HintedString {
    if let ast::Expr::Ident(ident) = callee {
        if vm.scopes.check_std_shadowed(ident.get()) {
            err.hint(eco_format!("use `std.{ident}` to access the shadowed ..."));
        }
    }
    err
}
```

两个判断：`callee` 必须是裸 `Ident`（不是 `a.b` 这种字段访问），且 `scopes.check_std_shadowed` 确认它确实覆盖了标准库函数，才追加 hint。注意它接收并返回 `HintedString`（带提示的字符串错误），属于「装饰器」式的小工具。它在 `lib.rs` 里通过 `pub use` 对外暴露（[lib.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L20)），供 call.rs 等调用点使用。

#### 4.4.4 代码实践

**实践目标**：理解 `hint_if_shadowed_std` 与 `check_std_shadowed` 的配合，体会「诊断三要素」。

**操作步骤**：

1. 读 [`src/vm.rs`:L94-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L94-L109)。
2. 在仓库里搜索 `check_std_shadowed` 的定义（它位于 typst-library 的 `Scopes`），理解它判断「这个名字是否覆盖了标准库」的依据。
3. 搜索 `hint_if_shadowed_std` 的调用点（主要在 call.rs），看它贴在哪种错误之后。

**需要观察的现象**：这条 hint 只在「裸标识符覆盖了标准库」这个很具体的情形下出现，避免对无关错误制造噪音。

**预期结果**：你能总结出 typst-eval 一条好诊断的三要素——**①错误信息**（出了什么错）、**②定位**（哪个 span）、**③修复提示**（怎么改）。`hint_if_shadowed_std` 就是第三要素的典型补全。`check_std_shadowed` 的具体定义在依赖 crate 中，本讲不展开，标注「待确认实现细节」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `hint_if_shadowed_std` 只在 `callee` 是 `Ident` 时才考虑加提示？

> **答案**：因为「覆盖标准库」这个提示针对的是「用户写了一个裸名字、本想用内置函数却被自己的同名绑定挡住」的情形。只有 `Ident`（裸标识符）才会发生这种遮挡；像 `a.b`（字段访问）、字面量等 callee 根本不涉及「名字查找」，加提示无意义。

**练习 2**：这个函数产出的是「错误」还是「提示」？为什么这样设计？

> **答案**：产出的是**提示（hint）**，它接收一个已有的 `HintedString` 错误并往上面 `.hint(...)`。错误本身（调用失败）由调用方决定，这个函数只负责在合适情形追加一条修复建议。这样错误与提示解耦，同一个底层错误可以在不同场景挂不同 hint。

---

## 5. 综合实践

把本讲四个模块串起来：**用 `Eval` + `Vm` 的机制，手工追踪一段极简 Typst 代码的求值状态变化**。

**任务**：给定源码片段（在 markup 模式下）

```typst
#let total = 1 + 2
#total
```

请按下面的步骤，写出每一步「哪个 `eval` impl 被触发、`Vm` 的哪个字段被读写、是否触发 `trace`」。

**操作步骤**：

1. 入口 `eval()` 新建 `Vm`（承接 [u1-l3](./u1-l1-eval-entry.md)）：此时 `flow = None`、`inspected` 由 `traced.get(id)` 决定（假设 IDE 没在看，故 `None`）。
2. `markup.eval(&mut vm)` 触发 `Eval for ast::Markup`，它流式迭代两个表达式：`#let total = 1 + 2` 和 `#total`。
3. 第一个表达式是 `ast::Expr::LetBinding` → 经 `Expr::eval` 分发 → `LetBinding::eval`：
   - 对初值 `1 + 2`（`ast::Expr::Binary`）求值 → 得 `Value::Int(3)`，期间 `Expr::eval` 末尾 `trace_at(span, &3)`（`inspected` 为 `None`，不命中，零成本）。
   - 用 `Vm::define("total", 3)` 绑定 → 进入 `Vm::bind`：① `trace_at`（不命中）、② 名字不是 `is`、③ `scopes.top.bind("total", ...)`。
4. 第二个表达式 `#total` 是 `ast::Expr::Ident` → `Ident::eval`：`vm.scopes.get("total")` 取出刚绑定的 `3`，`.read_checked(...).clone()` → `Value::Int(3)`；再经 `Expr::eval` 末尾 `trace_at`。
5. 求值结束，`eval()` 读 `vm.flow`（`None`，无残留控制流）、读 `vm.scopes.top`（含 `total` 绑定）装配 `Module`。

**需要观察的现象与预期结果**：

- `Vm` 的状态从「空作用域 + flow=None」演进到「含 total=3 的作用域 + flow=None」。
- `trace_at` 在每个表达式求值后都被调用，但因为 `inspected` 为 `None`，没有任何值真正写进 sink——这正是「全量埋点、零成本」的体现。
- 若把场景换成「IDE 正 hover 在 `#total` 的 span 上」，则 `inspected = Some(那个 span)`，第 4 步的 `trace_at` 会命中并把 `3` 写进 sink，IDE 就能显示这个值。

**待本地验证**：以上是对源码逻辑的推理；若想实际观察，需要借助 typst 的 IDE/追踪接口（如 tinymist）触发 inspect，属于 [u6-l2](./u6-l2-tracing-ide.md) 的范畴。

## 6. 本讲小结

- **`Eval` trait** 是节点求值的统一抽象：关联类型 `type Output` 让每种节点返回各自类型，签名 `fn eval(self, vm: &mut Vm) -> SourceResult<Output>` 用「消费 self + 可变借用 Vm + Result 包装」表达「一次性、有副作用、可能失败」的求值语义。
- **`Vm`** 是求值状态的容器，五个字段各司其职：`engine`（环境句柄）、`flow`（控制流事件）、`scopes`（作用域栈）、`inspected`（IDE 检查的 span）、`context`（幕后上下文）；每求值一个模块/每调用一次函数都新建一个。
- **`Vm::new`** 用 `engine.traced.get(id)` 决定是否进入 inspect/tracing 模式，把「IDE 在看哪个文件」转化为 `inspected` 的值。
- **`define` / `bind` / `trace` / `trace_at`** 是求值期主动改状态、服务 IDE 的方法；`bind` 除插入绑定外还做 `trace_at` 和 `is` 警告两件副作用。
- **trace 调用约定**：每个产生值的表达式都应（通常由 `Expr::eval` 末尾统一）调用一次 `trace_at`；`trace_at` 用 span 比较做条件记录，把 IDE 追踪的实际成本压到接近零，`trace` 本体被标 `#[cold]`。
- **`hint_if_shadowed_std`** 体现了 typst-eval 的诊断哲学：错误信息 + 精确定位 + 修复提示三要素齐全。

## 7. 下一步学习建议

本讲把「两大基石」讲完了。接下来建议：

- **[u2-l1 基础字面量与标识符求值](./u2-l1-literals-idents.md)**：深入 `code.rs` 里 `ast::Expr::eval` 这个总分发器，逐类看字面量（None/Bool/Int/Float/Str…）与 Ident 的求值实现，以及整数非法数字的诊断细节——本讲只用了它作示例，那里才细讲。
- **[u3-l2 控制流事件](./u3-l2-flow-events.md)**：本讲提到 `Vm` 的 `flow` 字段，那里会讲 `FlowEvent` 三种变体如何被写入 `flow` 并被循环/函数消费。
- **[u6-l2 追踪机制与 IDE 支持](./u6-l2-tracing-ide.md)**：本讲的 `trace`/`trace_at`/`inspected` 在那里会结合入口的 inspect 模式完整串联，解释「为什么有语法错误也继续求值」。
- 如果想立刻动手验证，可以先把本讲的综合实践「手工追踪 `#let total = 1 + 2`」走一遍，确认你能说清每一步 `Vm` 字段的变化，再进入 u2。
