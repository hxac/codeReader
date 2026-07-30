# 源码模块结构地图

## 1. 本讲目标

在上一讲里，我们已经知道 typst-eval 是 Typst 的「代码解释器」，职责是把语法树（AST）求值成运行时值。本讲的目标是把 `crates/typst-eval/src/` 这个目录**整体拆开**给你看：

- 学完后，你应该能说清楚 typst-eval 一共有多少个源文件、每个文件各管哪一类语法节点的求值；
- 给你任意一种 Typst 语法结构（例如 `for` 循环、`import`、字典解构、数学分数），你能**快速定位**到它对应的求值代码在哪个文件；
- 你应该看懂 `lib.rs` 作为 crate 根是如何用 `mod` 声明子模块、用 `pub use` 暴露对外 API、用 `use self::...` 在内部「搭桥」的；
- 你应该理解贯穿全 crate 的 `Eval` trait 是如何把「每种 AST 节点各自的求值逻辑」统一成一个抽象的。

本讲是后续所有讲义的「索引页」——后面每一讲都会深入某个具体文件，所以先把这张地图印在脑子里，后面不会迷路。

## 2. 前置知识

### 2.1 AST 节点（ast 节点）

typst-syntax 把源码文本解析成一棵**语法树**（Syntax Tree），树上的每个节点都对应一段语法结构，例如：

- `1 + 2` 是一个二元表达式 `ast::Binary`；
- `#let x = 1` 是一个 let 绑定 `ast::LetBinding`；
- `*粗体*` 是一个强调标记 `ast::Strong`；
- `$ a/b $` 是一个数学分数 `ast::MathFrac`。

typst-eval 的工作，就是**遍历这棵树，把每个节点求值成一个 `Value` 或 `Content`**。typst-eval 自己不解析文本，它只消费 typst-syntax 产出的 AST。

### 2.2 Rust 的模块系统要点

如果你对 Rust 模块系统还不熟，只需记住三条本讲会用到的规则：

1. **`mod xxx;`**：在当前文件里声明一个名为 `xxx` 的子模块，它的内容来自同目录下的 `xxx.rs`。默认是私有的（仅本 crate 可见）。
2. **`pub use xxx::Y;`**：把 `Y` 重新导出，让它成为对外公开 API 的一部分。
3. **`use self::xxx::*;`**：把子模块里的所有条目引入当前作用域，方便其他子模块通过 `crate::Y` 访问。

第 3 条是 typst-eval 最值得注意的「内部搭桥」技巧，后面会专门讲。

### 2.3 Value 与 Content（回顾）

- **`Value`**：Typst 代码模式（code）里的运行时值，如整数、浮点、字符串、数组、字典、函数等。
- **`Content`**：Typst 排版内容的抽象，一段文本、一个标题、一个列表项最终都是 `Content`。

不同 AST 节点求值后会产出不同的类型，这正是 `Eval` trait 用「关联类型 `type Output`」来表达的关键（见 4.2）。

## 3. 本讲源码地图

本讲聚焦在「目录结构」层面，涉及的关键文件如下表。其中 `lib.rs` 是核心，其余文件主要是举例说明「按节点分文件」的组织方式。

| 文件 | 角色 | 本讲用来做什么 |
| --- | --- | --- |
| `src/lib.rs` | crate 根：模块声明、对外 `pub use`、入口函数 `eval` / `eval_string`、`Eval` trait 定义 | 重点精读（4.1、4.2） |
| `src/vm.rs` | `Vm` 虚拟机结构与 `define` / `bind` / `trace` | 举例说明它被多文件共享 |
| `src/code.rs` | 代码模式表达式总分发 + 字面量/数组/字典/块 | 举例说明「总分发器」 |
| `src/markup.rs` | 标记模式（文本/标题/列表等） | 举例 |
| `src/math.rs` | 数学模式（方程/分数/根号等） | 举例 |
| `src/flow.rs` | 控制流（if/while/for、break/continue/return） | 举例 |
| `src/binding.rs` | let 绑定与解构 | 举例 |
| `src/call.rs` | 函数调用、闭包、参数、变量捕获 | 举例 |
| `src/import.rs` | 模块导入与 include | 举例 |
| `src/rules.rs` | set / show 规则 | 举例 |
| `src/ops.rs` | 一元/二元运算符 | 举例 |
| `src/access.rs` | 可变访问 `Access` trait | 举例 |
| `src/methods.rs` | 内置方法（push/pop/insert/remove/first/at…） | 举例 |

> 小提示：`src/` 下一共有 **13 个 `.rs` 文件**——1 个 crate 根 `lib.rs` + 12 个子模块。本讲的表格列出了全部 12 个子模块，是 typst-eval 的完整版图。

---

## 4. 核心概念与源码讲解

### 4.1 lib.rs：crate 根、模块声明与对外 API

#### 4.1.1 概念说明

`lib.rs` 是整个 crate 的「根」。它做了三件事：

1. **声明子模块**：用 `mod` 把 12 个文件挂到模块树上；
2. **定义入口函数与核心抽象**：`eval()`、`eval_string()`、`Eval` trait 都在这里；
3. **决定什么是对外可见的**：通过 `pub use` 选择性地把内部符号暴露成公开 API，其余一律不对外。

把 `lib.rs` 理解成「门厅 + 总索引」：外界只能从门厅进，门厅里贴了一张「只允许走这些路」的告示（`pub use`），而内部的走廊（`use self::...`）只给员工（子模块）走。

#### 4.1.2 核心流程

`lib.rs` 顶部的组织遵循这样一个流程：

```text
1. mod 声明       → 把 12 个 .rs 文件挂上模块树（全是内部可见，没有 pub mod）
2. pub use        → 挑选 6 个符号作为对外 API
3. use self::...  → 把部分子模块的 pub(crate) 条目「上提到根」，
                     供其他子模块用 crate::名字 访问
4. use 外部 crate  → 引入 comemo / typst_library / typst_syntax 等
5. 定义 eval / eval_string / Eval trait
```

关键认知点：

- **没有任何子模块是 `pub mod`**，所以 `typst_library` 等上游 crate **看不到** `typst_eval::code`、`typst_eval::call` 这些模块路径。它们纯粹是内部实现。
- 对外只通过 `pub use` 暴露**精挑细选的少量符号**。typst-eval 对外的「公开面孔」其实非常小。

#### 4.1.3 源码精读

**模块声明（第 1 步）**：注意 `ops` 用了 `pub(crate)`，其余都是私有 `mod`，但二者在本场景下含义一致——都表示「不跨 crate 暴露」。

[src/lib.rs:L3-L15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L3-L15) — 声明全部 12 个子模块，没有任何一个对 crate 外公开。

**对外导出（第 2 步）**：这是 typst-eval 的「公开 API 清单」。只有 4 行，共 6 个符号。

[src/lib.rs:L17-L20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L17-L20) — 通过 `pub use` 把 `CapturesVisitor`、`eval_closure`、`FlowEvent`、`import`、`Vm`、`hint_if_shadowed_std` 暴露出去。

| pub use 来源 | 暴露的符号 | 一句话用途 |
| --- | --- | --- |
| `self::call` | `CapturesVisitor`、`eval_closure` | 闭包变量捕获分析器、闭包执行入口 |
| `self::flow` | `FlowEvent` | 控制流事件（break/continue/return） |
| `self::import` | `import` | 按路径/包名导入模块 |
| `self::vm` | `Vm`、`hint_if_shadowed_std` | 虚拟机、错误提示辅助函数 |

再加上 `lib.rs` 自己直接定义的 `pub fn eval`、`pub fn eval_string`、`pub trait Eval`，这就是 typst-eval 对外公开的全部 API 面孔。

**内部搭桥（第 3 步）**：这是理解「子模块之间如何共享代码」的关键。

[src/lib.rs:L22-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L22-L24) — 把 `access`、`binding`、`methods` 三个模块里的 `pub(crate)` 条目「上提」到 crate 根，于是其他子模块可以直接写 `use crate::{Access, destructure, call_method_mut};`。

举几个真实例子，看看子模块是怎么通过 `crate::` 拿到这些「桥接」符号的：

- `flow.rs` 里：`use crate::{Eval, Vm, destructure};` —— `destructure` 来自 `binding.rs`（经 lib.rs 的 `use self::binding::*` 上提）。
- `ops.rs` 里：`use crate::{Access, Eval, Vm, access_dict};` —— `Access` / `access_dict` 来自 `access.rs`。
- `access.rs` 里：`use crate::{Eval, Vm, call_method_access, is_accessor_method};` —— 来自 `methods.rs`。

于是形成了一个以 `lib.rs` 为中心的「辐条（hub-and-spoke）」式共享结构：所有子模块都从 `crate::` 取公共依赖，而 `crate::` 的内容由 `lib.rs` 顶部的三行 `use self::...` 注入。

#### 4.1.4 代码实践

**实践目标**：亲手验证「typst-eval 的对外 API 很小，内部模块全部私有」。

**操作步骤**：

1. 打开 `src/lib.rs`，找到第 3–24 行。
2. 用编辑器搜索（或 `grep`）整个 `crates/typst-eval/` 目录里出现的 `pub mod`，记录命中数量。
3. 用编辑器搜索 `pub use`，把所有命中的符号抄下来。

**需要观察的现象**：

- 第 2 步：**搜不到任何 `pub mod`**（`ops` 是 `pub(crate) mod`，不是 `pub mod`）。这说明 12 个子模块对外界完全不可见。
- 第 3 步：`pub use` 只命中 4 行，对外符号一共 6 个（见上表）。

**预期结果**：你会直观感受到——typst-eval 把几乎所有实现细节藏在私有模块里，只露出 `eval`、`eval_string`、`Eval`、`Vm`、`eval_closure`、`CapturesVisitor`、`FlowEvent`、`import`、`hint_if_shadowed_std` 这一张很小的「公开面孔」。后续读源码时，凡是不在这些公开符号里的，都是内部实现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `code.rs`、`call.rs` 这些模块要声明成私有 `mod` 而不是 `pub mod`？

> **参考答案**：因为它们只是 `Eval` trait 的实现细节。外界（如 `typst_library`、`typst-cli`）只关心「给我一个 `Source`，我还你一个 `Module`」这个入口（`eval`），并不需要知道求值内部是怎么按节点分发的。保持私有可以避免内部结构变成「被依赖的公开契约」，将来重构（拆分、合并文件）不会破坏上游。

**练习 2**：如果一个新加的内部函数 `foo()` 定义在 `binding.rs` 里，想被 `flow.rs` 调用，至少要做哪两件事？

> **参考答案**：① 在 `binding.rs` 里把 `foo` 标成 `pub(crate)`；② 在 `lib.rs` 已有的 `use self::binding::*;`（由于用了通配符 `*`）会自动把它上提到 crate 根，于是 `flow.rs` 里就能写 `use crate::foo;` 来调用。

---

### 4.2 Eval trait：统一的求值抽象

#### 4.2.1 概念说明

typst-eval 里有几十种 AST 节点，每种都要「求值」。如果每种节点各写一个名字不同的函数（`eval_if`、`eval_for`、`eval_let`……），调用方就得针对每种节点写一个 `match` 分支去调对应函数，非常啰嗦。

`Eval` trait 用 Rust 的**关联类型（associated type）**优雅地解决了这个问题：每种节点都实现 `Eval`，各自声明 `type Output` 是什么（表达式产出 `Value`，markup 产出 `Content`，set 规则产出 `Styles`……），但对外都只暴露同一个方法名 `eval`。这样调用方只要写 `node.eval(vm)`，编译器会根据节点类型自动分发到正确的实现。

#### 4.2.2 核心流程

`Eval` trait 的求值流程可以用下面的伪代码概括：

```text
对某个 AST 节点 node:
  node.eval(&mut Vm) -> SourceResult<Self::Output>
       │
       ├─ 借用虚拟机 vm（读绑定、写 flow、记录 trace）
       ├─ 执行该节点特有的求值逻辑
       └─ 返回 Self::Output（Value / Content / Styles / Array / Dict / Args / Recipe ...）
```

它的三个关键设计：

1. **`self` 按值消费**：`fn eval(self, vm: &mut Vm)`。AST 节点通常很轻（只是带生命周期的引用包装），按值传入可以方便地解构出子节点。
2. **`&mut Vm`**：所有求值都共享同一个可变虚拟机引用，这样才能读写作用域、控制流、追踪信息。
3. **`SourceResult<Self::Output>`**：求值可能失败（返回带源码定位的错误），所以统一用 `SourceResult` 包裹。

#### 4.2.3 源码精读

`Eval` trait 本身定义得极其精简，只有 4 行实质内容：

[src/lib.rs:L177-L184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L177-L184) — `Eval` trait：关联类型 `type Output` + 一个 `eval(self, vm: &mut Vm) -> SourceResult<Self::Output>` 方法。

不同节点的 `Output` 不同，这正是关联类型的价值。下面是几个真实例子（贯穿后续多讲）：

| 节点 | 文件 | `type Output` | 含义 |
| --- | --- | --- | --- |
| `ast::Expr` | `code.rs` | `Value` | 任何表达式都产出统一的 `Value` |
| `ast::Markup` | `markup.rs` | `Content` | 标记流拼成排版内容 |
| `ast::Math` | `math.rs` | `Content` | 数学流拼成排版内容 |
| `ast::Array` | `code.rs` | `Array` | 直接产出数组（不是 `Value`） |
| `ast::Args` | `call.rs` | `Args` | 参数集合 |
| `ast::SetRule` | `rules.rs` | `Styles` | set 规则产出一组样式 |

看一个最简单的实现——`ast::Code`（代码块里的表达式流）：

[src/code.rs:L17-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L17-L23) — `impl Eval for ast::Code`，`Output = Value`，把求值委托给私有函数 `eval_code`。

再看一个产出非 `Value` 类型的实现——`ast::Markup`：

[src/markup.rs:L17-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L17-L23) — `impl Eval for ast::Markup`，`Output = Content`，委托给 `eval_markup`。

> **关键约定**：几乎每个值产出节点的 `eval` 实现末尾都会调用 `vm.trace_at(span, &value)`，用于驱动 IDE 的 hover/tooltip。这一点在 4.3 的「总分发器」里会再次看到，它把「必须 trace」的义务统一收口在 `ast::Expr::eval` 里。关于 tracing 的细节属于第 6 单元，本讲只需知道有这个约定即可。

#### 4.2.4 代码实践

**实践目标**：通过统计 `impl Eval for` 的数量，感受 `Eval` trait 是如何「散落」到各文件的。

**操作步骤**：

1. 在 `crates/typst-eval/src/` 目录下，搜索字符串 `impl Eval for`。
2. 按文件分组统计每个文件各实现了多少个 `Eval`。
3. 任选一个实现，确认它的 `type Output` 是什么。

**需要观察的现象**：

- `code.rs` 里命中数最多（`Ident`、`None`、`Auto`、`Bool`、`Int`、`Float`、`Numeric`、`Str`、`Array`、`Dict`、`CodeBlock`、`ContentBlock`、`Parenthesized`、`FieldAccess`、`Contextual`、`Code`、`Expr` 等十几个）。
- 不同实现的 `Output` 不尽相同（`Value` / `Array` / `Dict` / `Content`）。

**预期结果**：你会看到「几十个 `impl Eval for`，分布在十几个文件里」——这正是 typst-eval 的核心代码组织方式：**求值逻辑按 AST 节点类型切分到不同文件，每个文件就是一批 `impl Eval for XXX`**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Eval` 的方法签名是 `fn eval(self, vm: &mut Vm)` 而不是 `fn eval(&self, vm: &mut Vm)`？

> **参考答案**：AST 节点大多是通过 `self.body()`、`self.condition()` 这类按值返回的访问器来取子节点的（这些访问器返回的是带生命周期的视图类型，而非 `&Self`）。按值消费 `self` 后可以方便地链式取出子节点并递归求值；用 `&self` 反而要到处写 `(*self).body()` 之类的借用解构，不符合 typst-syntax 的 AST 访问习惯。

**练习 2**：`ast::Array::eval` 的 `Output` 为什么是 `Array` 而不是 `Value`？

> **参考答案**：因为「数组求值」语义上产出的就是数组本身，很多内部调用点需要直接拿到 `Array`（例如 spread 展开 `..array`），如果输出 `Value` 就得每次都 `cast::<Array>()` 一次。声明更精确的 `Output = Array` 既表达意图，又省去无谓的装箱与拆箱。

---

### 4.3 按节点类型分文件的求值组织方式

#### 4.3.1 概念说明

typst-eval 的代码不是按「功能层」（如「解析层」「类型层」）切分，而是**按「AST 节点类别」切分**：一类语法结构对应一个文件。这是 typst-eval 最显著的目录组织特征，也是本讲最重要的结论。

好处非常直接：

- 看到 `#for x in arr { ... }` 卡住了 → 直接去 `flow.rs`；
- 看到 `#import "x.typ": a` 报错 → 直接去 `import.rs`；
- 想知道 `*粗体*` 怎么变 `Content` → 直接去 `markup.rs`。

你不需要记住每行代码，只需要记住「**节点类别 → 文件**」的映射表。

#### 4.3.2 核心流程：12 个子模块职责总表

下表是本讲的核心产出。左列是文件，右列一句话概括它负责求值哪类结构，并标注该文件最典型的 `impl Eval for` 对象（便于你按图索骥）。

| 文件 | 负责求值的结构类别 | 典型 `impl Eval for` 对象 |
| --- | --- | --- |
| `vm.rs` | （非节点求值）虚拟机状态：作用域、控制流、追踪 | `Vm` 结构体及 `define`/`bind`/`trace_at`/`trace` |
| `code.rs` | **代码模式总分发 + 字面量 + 集合 + 块 + 字段访问** | `ast::Expr`（总分发）、`ast::Code`、`ast::Ident`、`ast::Int`、`ast::Array`、`ast::Dict`、`ast::CodeBlock`、`ast::FieldAccess` |
| `markup.rs` | **标记模式**：文本/空格/强调/标题/列表/原文/链接/标签/引用 | `ast::Markup`、`ast::Text`、`ast::Strong`、`ast::Emph`、`ast::Heading`、`ast::ListItem`、`ast::Raw`、`ast::Label`、`ast::Ref` |
| `math.rs` | **数学模式**：方程/符号/上下标/分数/根号/定界 | `ast::Equation`、`ast::Math`、`ast::MathIdent`、`ast::MathFrac`、`ast::MathRoot`、`ast::MathAttach`、`ast::MathDelimited` |
| `flow.rs` | **控制流**：条件/循环/break/continue/return | `ast::Conditional`、`ast::WhileLoop`、`ast::ForLoop`、`ast::LoopBreak`、`ast::FuncReturn`；含 `FlowEvent` 枚举 |
| `binding.rs` | **绑定与解构**：let、解构赋值 | `ast::LetBinding`、`ast::DestructAssignment`；含 `destructure` / `destructure_array` / `destructure_dict` |
| `call.rs` | **函数调用、参数、闭包、变量捕获** | `ast::FuncCall`、`ast::Args`、`ast::Closure`、`eval_closure`、`CapturesVisitor` |
| `import.rs` | **模块系统**：import、include | `ast::ModuleImport`、`ast::ModuleInclude`、`import`、`import_file`、`resolve_package` |
| `rules.rs` | **样式规则**：set、show | `ast::SetRule`、`ast::ShowRule`；含两个兼容性警告 |
| `ops.rs` | **运算符**：一元、二元、赋值 | `ast::Unary`、`ast::Binary`；委托给 `typst_library::foundations::ops` |
| `access.rs` | **可变访问**：为赋值/变更方法提供 `&mut Value` | `Access` trait 及对 `Ident`/`FieldAccess`/`FuncCall` 的实现；`access_dict` |
| `methods.rs` | **内置方法**：push/pop/insert/remove/first/last/at | `is_mutating_method`、`call_method_mut`、`call_method_access` |

#### 4.3.3 源码精读：用三个文件验证「按节点切分」

为了让上表不只是「背表」，我们挑三个有代表性的文件，看一眼它们的入口实现，确认这种组织方式的真实样子。

**(1) `code.rs` 是「总分发器」**。`ast::Expr` 是所有表达式的枚举，它的 `eval` 是一个巨大的 `match`，把每种表达式派发到各自的 `Eval` 实现：

[src/code.rs:L76-L156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L76-L156) — `impl Eval for ast::Expr`，对每一种表达式枚举变体调用 `v.eval(vm)`，并在最后统一 `vm.trace_at(span, &value)` 满足追踪约定。

注意这个 `match` 里很多分支只是 `Self::Heading(v) => v.eval(vm).map(Value::Content)`——即「调用对应节点的 `eval`，再把结果包成 `Value`」。也就是说，`code.rs` 这个「总分发器」把活儿最终派发到了 `markup.rs`（如 `Heading`）、`math.rs`（如 `MathFrac`）、`flow.rs`（如 `Conditional`）等文件里的具体实现。**这就是「总分发器 + 按节点分文件」的协作全貌。**

**(2) `flow.rs` 集中处理控制流**。所有跟 if/while/for、break/continue/return 相关的求值都在这里，还定义了跨文件使用的 `FlowEvent` 枚举：

[src/flow.rs:L12-L22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L12-L22) — `FlowEvent` 三种变体 `Break` / `Continue` / `Return`，被 `flow.rs`、`call.rs`、`code.rs`、`lib.rs` 共同使用。

**(3) `vm.rs` 不是「某个节点的求值」，而是所有求值的共享状态**。它定义了每个子模块都要用的 `Vm`：

[src/vm.rs:L16-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L16-L28) — `Vm` 结构体：`engine`（引擎）、`flow`（当前控制流事件）、`scopes`（作用域栈）、`inspected`（IDE 正在审查的 span）、`context`（上下文）。

> 一个有用的判别技巧：12 个文件里，**只有 `vm.rs` 不是「为某类 AST 节点实现 `Eval`」**，它是「所有节点求值时都要用的虚拟机」。其余 11 个文件都直接包含若干 `impl Eval for ast::XXX`。记住这个区别，你就不会把 `vm.rs` 当成「某个语法结构的求值器」。

#### 4.3.4 代码实践

**实践目标**：把本讲的「节点类别 → 文件」映射表自己造一遍，并用真实源码核对。

**操作步骤**：

1. 准备一张表，左列依次写下这 12 个文件名：`code.rs`、`markup.rs`、`flow.rs`、`binding.rs`、`call.rs`、`import.rs`、`rules.rs`、`ops.rs`、`access.rs`、`methods.rs`、`math.rs`、`vm.rs`。
2. 打开每个文件，找到它的**第一个** `impl Eval for` 或导出的核心函数，用一句话概括它管哪类结构（不要照抄本讲，先自己写）。
3. 打开 `lib.rs` 第 17–20 行，把 `pub use` 暴露的全部符号标注在表下方。

**需要观察的现象**：

- 11 个「节点文件」的第一个 `impl Eval for` 基本能一眼看出它管的节点类别（例如 `import.rs` 第一个就是 `impl Eval for ast::ModuleImport`）。
- `vm.rs` 没有 `impl Eval for`，只有 `pub struct Vm`。
- `pub use` 暴露的符号只有 6 个：`CapturesVisitor`、`eval_closure`、`FlowEvent`、`import`、`Vm`、`hint_if_shadowed_std`。

**预期结果**：你得到了一张与 4.3.2 类似的表，外加一份「对外 API 清单」。这张表就是后续学习的导航图——以后遇到任何 typst-eval 相关问题，先看「它属于哪类节点」，再查表定位文件。

**进阶（源码阅读型实践）**：挑一个具体语法现象做端到端追踪。例如 `#let (a, b) = (1, 2)`：

- 它是 `let` 绑定 → 查表得 `binding.rs`；
- 打开 `binding.rs`，看 `impl Eval for ast::LetBinding`（[src/binding.rs:L9-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L9-L28)），发现它对 `Normal(pattern)` 调用了 `destructure`；
- 顺着 `destructure`（[src/binding.rs:L45-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L45-L57)）看到它又把活儿交给 `destructure_impl` → `destructure_array`。
- 至此你已经在不写一行代码的情况下，沿着「节点 → 文件 → 函数」把解构赋值的调用链走通了。这正是本讲地图的价值。

#### 4.3.5 小练习与答案

**练习 1**：用户写了 `#set text(size: 12pt)`，报错信息说某个字段不对。你应该去哪个文件读源码？

> **参考答案**：去 `rules.rs`。`set` 是样式规则，由 `impl Eval for ast::SetRule` 处理（[src/rules.rs:L11-L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L11-L35)）。注意：`set text(...)` 里 `text` 是元素函数、`size` 是它的参数，这些**类型定义**在 `typst-library`，但「set 规则如何被求值成 `Styles`」的逻辑在 typst-eval 的 `rules.rs`。

**练习 2**：`vm.rs` 和其余 11 个文件的根本区别是什么？

> **参考答案**：其余 11 个文件都包含若干 `impl Eval for ast::XXX`，即为某一**类 AST 节点**提供求值实现；`vm.rs` 不为任何节点实现 `Eval`，它定义的是 `Vm` 虚拟机——所有节点求值时共享的运行时状态（作用域、控制流、追踪等）。可以理解为：那 11 个文件是「干活的工人」，`vm.rs` 是「工人们共用的工具车」。

**练习 3**：为什么 `math.rs` 里的 `MathFrac`（分数）要单独放在 `math.rs`，而不是和 `code.rs` 里的运算符放一起？

> **参考答案**：因为 Typst 有三种独立的语法模式——code、markup、math。数学模式（`$ ... $`）有自己的一套节点类型（`MathFrac`、`MathRoot`、`MathAttach` 等），它们的求值产出 `Content`（数学排版元素），与 code 模式的运算符（产出 `Value`）在语义和输出类型上都不同。按「AST 节点类别」分文件的自然结果，就是数学相关节点自成一类、放进 `math.rs`。

---

## 5. 综合实践

**任务：绘制 typst-eval 的「节点 → 文件 → 对外符号」三栏导航卡。**

请结合本讲全部内容，完成下面这张综合表（建议手写或用 Markdown）：

| 语法结构示例 | 所属节点类别 | 对应源文件 | 该文件是否被 `pub use` 暴露？ |
| --- | --- | --- | --- |
| `1 + 2` | 二元运算 | `ops.rs` | 否（内部） |
| `#if x { 1 }` | 控制流·条件 | `flow.rs` | 部分（`FlowEvent` 被暴露，但模块本身不暴露） |
| `*粗体*` | 标记·强调 | `markup.rs` | 否 |
| `$ a/b $` | 数学·分数 | `math.rs` | 否 |
| `#let (a, b) = (1, 2)` | 绑定·解构 | `binding.rs` | 否 |
| `#f(1, b: 2)` | 函数调用 | `call.rs` | 部分（`eval_closure`、`CapturesVisitor` 被暴露） |
| `#import "x.typ": a` | 模块导入 | `import.rs` | 部分（`import` 被暴露） |
| `#set text(size: 12pt)` | 样式规则 | `rules.rs` | 否 |

完成表格后，回答两个问题：

1. 哪些文件**完全没有任何符号**被 `pub use` 暴露？（答：`markup.rs`、`math.rs`、`binding.rs`、`ops.rs`、`access.rs`、`methods.rs`、`rules.rs`、`code.rs`——它们是纯内部实现。）
2. 如果你想给 typst-eval 增加一种全新的语法节点（比如某种新的循环），你最少要改哪几个文件？（答：① 在对应类别文件里加 `impl Eval for ast::新节点`；② 如果它需要新的跨文件共享辅助函数，记得在 `lib.rs` 的 `use self::xxx::*` 体系里让它可达；③ 视情况在 `code.rs` 的 `ast::Expr::eval` 总分发 `match` 里加一个分支。注意：新增 AST 节点本身属于 typst-syntax 的工作，不在本 crate。）

> 这个综合实践把「节点定位」「对外 API 边界」「内部搭桥」三件事串了起来，是本讲地图的最终落地。

## 6. 本讲小结

- typst-eval 的 `src/` 共 **13 个 `.rs` 文件**：1 个 crate 根 `lib.rs` + 12 个子模块，**没有任何子模块对 crate 外公开**（没有 `pub mod`）。
- `lib.rs` 用 `pub use` 只暴露了 **6 个符号**（`CapturesVisitor`、`eval_closure`、`FlowEvent`、`import`、`Vm`、`hint_if_shadowed_std`），加上它自己定义的 `eval`、`eval_string`、`Eval`，构成了极小的公开面孔。
- `lib.rs` 顶部的 `use self::access::*; use self::binding::*; use self::methods::*;` 把三个模块的 `pub(crate)` 条目「上提」到 crate 根，形成**以 `lib.rs` 为中心的辐条式内部共享**，子模块统一用 `crate::名字` 互相调用。
- `Eval` trait 用关联类型 `type Output` 把「每种 AST 节点各自的求值」统一成一个抽象，不同节点可产出不同类型（`Value`/`Content`/`Array`/`Styles`/`Args`/`Recipe`…）。
- 代码按 **AST 节点类别** 切分到不同文件：`code.rs`（代码模式总分发）、`markup.rs`（标记）、`math.rs`（数学）、`flow.rs`（控制流）、`binding.rs`（绑定解构）、`call.rs`（调用/闭包/捕获）、`import.rs`（模块）、`rules.rs`（set/show）、`ops.rs`（运算符）、`access.rs`（可变访问）、`methods.rs`（内置方法）。
- 12 个子模块里，**只有 `vm.rs` 不是「为某类节点实现 `Eval`」**，它定义所有求值共享的 `Vm` 虚拟机。

## 7. 下一步学习建议

有了这张地图，建议按以下顺序继续：

1. **下一讲 u1-l3《解释器入口：eval 与 eval_string》**：深入 `lib.rs` 里的两个入口函数，看 `eval()` 如何一步步把 `Source` 装配成 `Module`。这是理解「求值从哪里开始」的关键。
2. **再下一讲 u1-l4《Eval trait 与 Vm 虚拟机》**：把本讲提到的 `Eval` trait 和 `Vm` 结构体讲透，为后续逐文件深入打基础。
3. **之后进入第 2 单元**：从 `code.rs` 的「总分发器」开始，按 markup / math 模式逐个文件深入。

阅读建议：在进入下一讲前，先对照本讲的「文件职责表」随便翻几个文件，确认自己能「看文件名猜出它管什么」。一旦这个直觉建立起来，后面读任何讲义都能迅速定位到源码。
