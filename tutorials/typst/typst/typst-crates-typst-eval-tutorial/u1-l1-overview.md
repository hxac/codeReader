# 项目概览与在 Typst 中的定位

## 1. 本讲目标

本讲是 typst-eval 学习手册的第一篇。读完本讲，你应该能够：

- 用一句话说清 typst-eval 是什么——它是 Typst 的**代码解释器（code interpreter）**，负责把「源码文本经解析得到的语法树」求值成 Typst 的运行时值（`Value` / `Content` / `Module`）。
- 看懂 `Cargo.toml` 里每一个 workspace 依赖在本解释器中扮演的角色，尤其是 `typst-syntax`、`typst-library`、`comemo` 这三个最关键的依赖。
- 说清 typst-eval 在 Typst 整体编译流水线中的上下游位置：它**上游**接 typst-syntax（解析器），**运行时类型**来自 typst-library。
- 读懂 `eval()` 这个带 `#[comemo::memoize]` 的入口函数的高层流程（循环防护 → 准备 Engine → 准备 Vm → 收集错误/警告 → 求值 → 装配 Module）。

本讲只做「鸟瞰」，不深入单个节点类型的求值细节——那是后续讲义的任务。

## 2. 前置知识

本讲假设你了解以下基础概念（不熟悉也没关系，下面会顺带解释）：

- **Rust crate**：Rust 的编译单元，类似其他语言的「包」。一个 crate 由 `Cargo.toml` 描述其元信息与依赖。
- **AST（抽象语法树）**：源码文本经过「解析（parse）」后得到的树形结构，树上的每个节点对应一段语法（比如一个数字、一个函数调用）。
- **解释器（interpreter）**：一种直接「遍历 AST 并执行」的程序，区别于「先编译成字节码/机器码再执行」。typst-eval 是一种 **tree-walking interpreter**（遍历语法树式的解释器）。
- **求值（evaluate / eval）**：把一段语法结构转换成「值」的过程。例如把字面量 `1` 求值成整数 `1`，把 `#rect()` 求值成一个矩形内容元素。
- **memoization（记忆化）**：把函数的「输入 → 输出」缓存起来，下次相同输入直接返回缓存结果，避免重复计算。Typst 用 `comemo` 这个库来实现。

如果你完全没接触过编译原理，可以暂时把「解析 → 求值」理解成「读句子 → 理解句子意思」两个阶段。

## 3. 本讲源码地图

本讲只涉及两个最关键的文件：

| 文件 | 作用 |
|------|------|
| [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L1-L33) | 声明 typst-eval 的元信息与全部依赖。通过它一眼看清这个解释器「站在谁的肩膀上」。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L1-L185) | crate 的根文件：声明所有子模块、对外暴露 `pub use` 的 API，并定义了 `eval()` / `eval_string()` 两个入口函数和贯穿全 crate 的 `Eval` trait。 |

> 提示：`src/` 下一共有 13 个源文件（`access / binding / call / code / flow / import / markup / math / methods / ops / rules / vm`，外加根文件 `lib.rs`）。本讲只鸟瞰 `lib.rs`，其余文件在 [第 2 单元](#7-下一步学习建议) 起逐篇精读。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** typst-eval 是什么：tree-walking 解释器与 `Eval` 抽象
- **4.2** `eval()` 入口函数精读
- **4.3** `Cargo.toml` 依赖声明与上下游关系

---

### 4.1 typst-eval 是什么：tree-walking 解释器与 Eval 抽象

#### 4.1.1 概念说明

打开 `Cargo.toml`，第二行就写明了这个 crate 的自我定位：

```toml
name = "typst-eval"
description = "Typst's code interpreter."
```

参见 [`Cargo.toml:2-3`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L2-L3)。

「code interpreter」即**代码解释器**。它的职责非常明确：

- **输入**：一段 Typst 源码经 typst-syntax 解析后得到的 AST（抽象语法树）。
- **输出**：Typst 的运行时「值」，主要是 `Module`（模块）、`Value`（值）、`Content`（内容）。

也就是说，typst-eval **不负责读文件、不负责解析语法**——那是 typst-syntax 的事；它也**不负责定义「什么是矩形、什么是加法」**——这些运行时类型与内置函数来自 typst-library。typst-eval 只做一件事：**遍历 AST，按规则把每个节点「算」成一个值**。这正是「tree-walking interpreter」的含义。

之所以叫「tree-walking（遍历树）」，是因为它的核心设计是：为每一种 AST 节点类型实现一个 `eval` 方法，求值时从根节点开始，递归地「走」整棵树。

#### 4.1.2 核心流程

整个解释器建立在两个基石之上：

1. **`Eval` trait**：一个统一的「求值接口」。凡是能被求值的 AST 节点（表达式、标记、数学节点……），都实现了这个 trait。
2. **`Vm`（虚拟机）**：求值过程中携带的「状态机」，保存当前的作用域栈、控制流事件、被追踪的 span 等。

求值的核心循环可以概括为伪代码：

```text
对一棵 AST 的根节点 root 调用 root.eval(&mut vm)
  └─ eval 内部根据节点类型，递归地对子节点调用 child.eval(&mut vm)
       └─ 最底层的字面量节点（数字、字符串）直接返回对应的 Value
  └─ 整棵树走完后，根节点返回最终的 Value / Content
```

最关键的抽象就是这个 `Eval` trait：

```rust
/// Evaluate an expression.
pub trait Eval {
    /// The output of evaluating the expression.
    type Output;

    /// Evaluate the expression to the output value.
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>;
}
```

它只有两个要点：每个实现自带一个 `type Output`（不同节点求值后产出不同类型，比如表达式产出 `Value`、Markup 产出 `Content`），以及一个 `eval(self, vm: &mut Vm)` 方法（消费自身、借用虚拟机、返回带源信息的结果）。

#### 4.1.3 源码精读

`Eval` trait 的定义见 [`src/lib.rs:178-184`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L178-L184)。`Eval` 是整本讲义后续所有内容的「总接口」——后面每一篇讲义，本质上都是在讲「某个节点类型如何实现 `Eval`」。

而 `Vm` 虚拟机的结构定义见 [`src/vm.rs:16-28`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L16-L28)，它持有：

- `engine`：底层「虚拟排版器」，提供 `World`（世界，即文件系统/字体等资源）、`Sink`（错误/警告收集）、`Route`（调用路径，防循环）等；
- `flow`：当前正在发生的控制流事件（`break` / `continue` / `return`）；
- `scopes`：作用域栈（变量查找）；
- `inspected`：IDE 正在追踪的 span（用于 hover 提示）；
- `context`：隐式上下文数据。

文档注释里明确写道：**「A new virtual machine is created for each module evaluation and function call.」**（每求值一个模块、每调用一次函数，都会创建一个新的虚拟机）。这一点在 4.2 节会直接看到——`eval()` 里会 `Vm::new(...)` 一个虚拟机。

> 本模块暂不展开 `Vm` 的方法（`define` / `bind` / `trace_at` 等），那是第 1 单元第 4 讲（u1-l4）的主题。这里只需记住「Vm = 求值状态」。

#### 4.1.4 代码实践

**实践目标**：亲手确认「typst-eval 把 AST 求值成运行时值」这条主线，理解 trait 的分发模式。

**操作步骤**：

1. 打开 [`src/lib.rs:178-184`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L178-L184)，阅读 `Eval` trait 定义。
2. 在本仓库中搜索 `impl Eval for`（这是 Rust 里「为某类型实现 Eval」的语法），观察有哪些 AST 节点类型实现了它。
3. 任选一个实现，例如 `src/code.rs` 里的 `impl Eval for ast::Int`（整数字面量），看它如何把一个语法节点变成 `Value::Int`。

**需要观察的现象**：

- 你会发现几乎每一种 AST 节点（`Int`、`Str`、`Array`、`CodeBlock`、`Markup`、`Math`……）都各自有一个 `impl Eval`，且它们的 `type Output` 不尽相同。

**预期结果**：

- 这印证了 tree-walking 解释器的特征：求值 = 按节点类型分派到对应的 `eval` 实现，并递归处理子节点。

> 说明：本步骤为「源码阅读型实践」，无需编译运行。

#### 4.1.5 小练习与答案

**练习 1**：`Eval` trait 的 `eval` 方法签名是 `fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>;`。其中 `self`（而非 `&self`）意味着什么？

> **参考答案**：`self` 表示「按值消费该 AST 节点」。Typst 的 AST 节点本身很轻量（大多是对底层语法树节点的引用/包装），消费后即可在求值过程中自由移动、转换，不必担心生命周期借用冲突。这也意味着每个节点求值后通常不再复用。

**练习 2**：为什么 `Eval` 要用关联类型 `type Output`，而不是统一返回 `Value`？

> **参考答案**：因为不同节点求值后的产物类型不同：表达式（`ast::Expr`）产出 `Value`，Markup 产出 `Content`（内容），Math 产出数学内容等。用关联类型可以让每种节点在类型层面精确声明自己的输出类型，调用方拿到的是强类型结果，而非一个需要再 cast 的万能 `Value`。

---

### 4.2 `eval()` 入口函数精读

#### 4.2.1 概念说明

如果说 `Eval` trait 是「求值每类节点」的细粒度接口，那么 `eval()` 函数就是「求值一整个源文件」的**顶层入口**。它把「一个 `Source`（源文件）」求值成「一个 `Module`（模块）」。

模块（`Module`）是 Typst 里「一个文件求值后的产物」的抽象：它包含该文件顶层定义的所有变量/函数，以及该文件生成的 `Content`。当你写 `#import "foo.typ"` 时，Typst 就是对 `foo.typ` 调用 `eval()` 得到一个 `Module`，再从中取出需要的符号。

除了 `eval()`，还有第二个入口 `eval_string()`：它接受一段字符串和指定的语法模式（Code / Markup / Math），求值后返回单个 `Value`。它主要用于在已有上下文里临时求值一小段代码（IDE、内联计算等场景）。

#### 4.2.2 核心流程

`eval()` 的高层流程可以拆成 6 步：

```text
eval(world, library, traced, sink, route, source)
  1. 循环防护：若 route 已包含本 source.id()，说明出现循环求值，直接 panic。
  2. 准备 Engine：装配 library / world / introspector / traced / sink / route。
  3. 准备 Vm：构造一个空的 Context、一个以 library 为根的作用域栈 Scopes，新建虚拟机。
  4. 收集错误/警告：先扫描语法树的 errors/warnings；警告进 sink，
     若有 errors 且不在 inspect 模式，直接返回错误（跳过求值）。
  5. 求值：把根节点当作 Markup 调用 markup.eval(&mut vm)，得到 Content。
  6. 装配 Module：把 scopes.top（顶层作用域）和求值出的 Content 打包成 Module 返回。
```

有两处「看似奇怪、实则精心设计」的点，先记下，后面章节会解释：

- **循环防护用 `panic!` 而非返回错误**：因为 `route.contains()` 本应被 `import`/递归求值层层拦截，走到这一步说明上游逻辑出了问题，属于不应发生的内部错误。
- **有语法错误时仍可能继续求值**：当 `vm.inspected.is_some()`（IDE 正在追踪某个 span）时，即使有语法错误也会继续求值，目的是尽量为 IDE 提供 hover 信息。

#### 4.2.3 源码精读

`eval()` 的完整定义见 [`src/lib.rs:38-97`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L97)。逐段对应：

- **函数签名与两个 attribute**（[L38-L47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L47)）：`#[comemo::memoize]` 让相同输入直接命中缓存（见 4.3 节）；`#[typst_macros::time(name = "eval", ...)]` 给这次求值打上计时标记，用于性能分析。
- **第 1 步 循环防护**（[L48-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L48-L52)）：`route.contains(id)` 判断当前求值路径上是否已经包含本文件。
- **第 2 步 准备 Engine**（[L54-L63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L54-L63)）：注意 `route: Route::extend(route).with_id(id)`——把当前文件 id 追加进调用路径，这正是后续递归求值能检测循环的依据。
- **第 3 步 准备 Vm**（[L65-L69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L65-L69)）：`Scopes::new(Some(library))` 以标准库为根作用域，`Vm::new(...)` 建虚拟机。`Vm::new` 内部会通过 `engine.traced.get(id)` 决定是否进入 inspect 模式，参见 [`src/vm.rs:32-40`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L32-L40)。
- **第 4 步 收集错误/警告**（[L71-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L71-L82)）：先警告进 sink；`if !errors.is_empty() && vm.inspected.is_none()` 时才提前返回错误。
- **第 5 步 求值**（[L84-L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L84-L86)）：`root.cast::<ast::Markup>()` 把根节点当作 Markup，调用 `markup.eval(&mut vm)`。
- **控制流兜底**（[L88-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L88-L91)）：若求值结束后 `vm.flow` 仍有未消费的控制流事件（比如在文件顶层写了 `return`），用 `bail!(flow.forbidden())` 报错。
- **第 6 步 装配 Module**（[L93-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L93-L96)）：`Module::new(name, vm.scopes.top).with_content(output).with_file_id(id)`。

另一个入口 `eval_string()` 见 [`src/lib.rs:101-175`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L101-L175)。它的关键差异是：根据 `mode` 选择 `parse_code` / `parse` / `parse_math`（[L114-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L114-L118)），最终返回 `Value`（Code 模式返回表达式的值，Markup/Math 模式包成 `Value::Content`，见 [L156-L167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L156-L167)）。

#### 4.2.4 代码实践

**实践目标**：把 `eval()` 的 6 步流程与源码一一对应，建立「函数即流水线」的直觉。

**操作步骤**：

1. 打开 [`src/lib.rs:38-97`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L97)。
2. 在源码旁手绘（或文字列出）这 6 步，标注每一步对应的行号区间。
3. 重点定位 [L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L78) 的判断 `if !errors.is_empty() && vm.inspected.is_none()`，思考：如果去掉 `&& vm.inspected.is_none()` 这个条件，IDE 的 hover 功能会发生什么？

**需要观察的现象**：

- 第 4 步里，警告始终进 sink，而错误在「非 inspect 模式」下才提前返回。
- 第 6 步 `Module::new` 的第一个参数 `name` 来自文件名 stem（`id.vpath().file_stem()`）。

**预期结果**：

- 你能闭着眼复述 `eval()` 从「防循环 → 建 Engine → 建 Vm → 查错误 → 求 Markup → 装 Module」的主线。

> 说明：去掉 inspect 条件后，当 IDE 追踪一个位于有语法错误区域的 span 时，`eval()` 会直接返回错误、不再求值，IDE 就拿不到该 span 处的值，hover 提示会失效。这正是「即使有语法错误也继续求值」的设计动机。

#### 4.2.5 小练习与答案

**练习 1**：`eval()` 返回 `Module`，而 `eval_string()` 返回 `Value`。为什么两者的返回类型不同？

> **参考答案**：`eval()` 求值的是「一整个文件」，文件可以定义大量顶层变量/函数供外部 `import`，因此需要 `Module`（封装顶层作用域 + 内容）；`eval_string()` 求值的是「一小段代码/标记」，目的通常是拿到「这一个值」（一个表达式结果或一段内容），因此返回单个 `Value` 更合适。

**练习 2**：`eval()` 在发现 `vm.flow` 非空时会 `bail!(flow.forbidden())`。请举一个会触发这种情况的用户代码例子。

> **参考答案**：在文件顶层直接写裸的 `return` / `break` / `continue`，例如一份内容仅为 `#return` 的 `.typ` 文件。`return` 只能出现在函数体内；在模块顶层它会一路冒泡到 `eval()` 末尾仍未被消费，于是被 `flow.forbidden()` 判定为非法控制流并报错。

---

### 4.3 `Cargo.toml` 依赖声明与上下游关系

#### 4.3.1 概念说明

要看懂一个 crate「站在谁的肩膀上、又服务于谁」，最快的方法就是读它的 `Cargo.toml`。typst-eval 是 typst workspace 里的一个成员 crate，所有依赖都以 `{ workspace = true }` 的形式指向 workspace 根的统一版本声明。

从依赖可以看出 typst-eval 的三个「核心盟友」：

1. **`typst-syntax`**——上游。提供 AST 节点类型与解析器（`parse` / `parse_code` / `parse_math`）。没有它，typst-eval 没有「树」可走。
2. **`typst-library`**——运行时类型来源。提供 `Value` / `Content` / `Module` / `Scope` / `Scopes` / `Engine` / `World` 等一切运行时类型，以及内置元素与函数。typst-eval 求值的「结果」全是 typst-library 里的类型。
3. **`comemo`**——记忆化缓存。给 `eval` / `eval_closure` 加上 `#[comemo::memoize]`，让相同输入不重复求值。

#### 4.3.2 核心流程

把依赖映射到 typst-eval 的实际使用，可以画出这样一条数据流：

```text
                    ┌─────────────────────────────────────────┐
   Typst 源码文本    │  typst-syntax                            │
   (Source / str) ──▶│  parse / parse_code / parse_math         │
                    │  → 得到 SyntaxNode / ast::* 节点          │
                    └────────────────────┬─────────────────────┘
                                         │  AST（语法树）
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │  typst-eval   ← 本 crate                  │
                    │  eval() / eval_string() / Eval trait      │
                    │  遍历 AST，逐节点求值（tree-walking）       │
                    └────────────────────┬─────────────────────┘
                                         │  求值过程中使用/产出
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │  typst-library                            │
                    │  Value / Content / Module / Func / Element│
                    │  Engine / World / Scope / Scopes ...      │
                    └─────────────────────────────────────────┘
                                         │
                            最终产物：Module（eval）
                                   或 Value（eval_string）

   横切支撑：
   comemo（memoize 缓存 eval/eval_closure）
   ecow / indexmap / rustc-hash（高效集合与字符串）
   unicode-segmentation（按 grapheme 迭代字符串）
   toml（解析包清单 typst.toml）
   stacker（非 wasm：动态增长调用栈，防递归爆栈）
   typst-macros / typst-timing / typst-utils（宏、计时、工具）
```

一句话总结上下游：**typst-syntax 把文本变成树，typst-eval 把树变成值（值由 typst-library 定义）**。

#### 4.3.3 源码精读

依赖声明集中在 [`Cargo.toml:15-29`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L15-L29)。结合 `lib.rs` 顶部的 `use` 语句（[L26-L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L26-L35)），可逐项印证每个依赖的角色：

| 依赖 | 在 typst-eval 中的角色 | 印证 |
|------|------------------------|------|
| `typst-syntax` | **上游**：提供 `Source`、`SyntaxMode`、`ast` 节点、`parse` / `parse_code` / `parse_math` | `lib.rs` L34 `use typst_syntax::{Source, SyntaxMode, ast, parse, parse_code, parse_math};` |
| `typst-library` | **运行时类型**：`Value` / `Content` / `Module` / `Engine` / `Route` / `Sink` / `Traced` / `World` / `Scopes` / `Context` 等 | `lib.rs` L27-L33 一大串 `use typst_library::...` |
| `typst-macros` | 过程宏，例如 `#[typst_macros::time(...)]` 给 `eval` 计时 | `lib.rs` L39 `#[typst_macros::time(name = "eval", ...)]` |
| `typst-utils` | 工具类型 `LazyHash`（标准库的延迟哈希包装）、`Protected`（受保护引用） | `lib.rs` L35 `use typst_utils::{LazyHash, Protected};` |
| `comemo` | **记忆化缓存**：`#[comemo::memoize]` 缓存 `eval` / `eval_closure`；`Tracked` / `TrackedMut` / `Track` 用于跟踪参数 | `lib.rs` L26 `use comemo::{Track, Tracked, TrackedMut};` 与 L38/L101 的 `#[comemo::memoize]` |
| `ecow` | 低成本复制集合（`EcoString` / `EcoVec`），错误信息、内容序列大量使用 | `vm.rs` L2 `use ecow::eco_format;` |
| `indexmap` | 有序哈希表（插入顺序保留），是 `Dict`（字典）的底层结构 | 用于字典求值（见后续 u2-l2） |
| `rustc-hash` | 高速哈希（`FxHashMap` 等），提升字典等结构的查找性能 | 配合集合类型使用 |
| `unicode-segmentation` | 按 **grapheme（字形簇）** 切分字符串，用于 `for` 循环遍历字符串 | 用于 `flow.rs` 中字符串迭代（见 u3-l1） |
| `toml` | 解析 `typst.toml` 包清单，用于 `import "@preview/..."` 解析包 | 用于 `import.rs`（见 u5-l1） |
| `stacker`（仅非 wasm） | 动态增长调用栈，防止深度递归（如函数递归调用）导致栈溢出 | 见 [L28-L29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L28-L29) 的 `cfg(not(target_arch = "wasm32"))`；用于 `call.rs`（见 u6-l3） |

> 关于 `comemo` 的关键性：`eval()` 签名里大量出现 `Tracked<...>` / `TrackedMut<...>` 类型（[L41-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L41-L46)）。这是 comemo 的跟踪引用机制——只有把这些参数以 tracked 形式传入，`#[comemo::memoize]` 才能正确判定「输入是否相等」从而命中缓存。这一点在第 6 单元（u6-l3）会深入。

#### 4.3.4 代码实践

**实践目标**：亲手把 11 个依赖逐一对应到解释器中的具体用途，建立「依赖即职责」的全景认识。

**操作步骤**：

1. 打开 [`Cargo.toml:15-29`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/Cargo.toml#L15-L29)。
2. 仿照上面那张表，自己先用一句话写下每个依赖「你觉得」在本解释器里干什么。
3. 再用全局搜索（在本 crate 的 `src/` 下搜 `use typst_syntax`、`use comemo`、`use ecow` 等）对照修正你的判断。
4. 最后画一条「源码文本 → 解析 → 求值 → 模块/内容」的数据流简图（可参考 4.3.2 的图）。

**需要观察的现象**：

- `typst-syntax` 和 `typst-library` 是被 `use` 次数最多的两个依赖，印证「一个是输入来源、一个是类型来源」。
- `stacker` 只在非 wasm 目标下编译——因为 wasm 栈增长策略不同。

**预期结果**：

- 你能脱表复述：typst-eval 的「三大核心盟友」是 typst-syntax（上游）、typst-library（运行时类型）、comemo（缓存）。

> 说明：本步骤为「源码阅读型实践」，重在理解依赖与职责的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：如果要把 typst-eval 移植到一个「没有文件系统、也没有包管理」的极简环境，哪个依赖最可能被裁掉？为什么？

> **参考答案**：最可能裁掉 `toml` 与 `indexmap` 中和包相关的部分——`toml` 仅用于解析包清单 `typst.toml`（`import "@preview/..."` 场景）。但要注意 `indexmap` 同时支撑 `Dict`，不能整体裁掉；真正「只为包服务」的是 `toml`。

**练习 2**：`comemo::memoize` 缓存了 `eval`。请思考：如果两次 `eval` 同一个 `Source` 但传入了不同的 `Tracked<Traced>`（追踪不同的 span），会命中缓存吗？

> **参考答案**：不会命中。`traced` 是 `eval` 的形参之一，comemo 会把它纳入缓存键的相等性判定。不同的 `Traced`（追踪不同 span）意味着「输入不同」，因此会重新求值。这也解释了为什么 IDE 切换 hover 目标时会触发对应 span 的重新求值。详细机制见 u6-l2 / u6-l3。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务。

**任务**：为一个刚加入团队的同事写一份「typst-eval 一页速览」，要求包含以下三部分，全部基于本讲读过的真实源码：

1. **定位**：用一段话说明 typst-eval 是什么、负责什么、不负责什么（参考 4.1）。
2. **入口**：用编号列表写出 `eval()` 的 6 步流程，并标注每步在 [`src/lib.rs:38-97`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L97) 中的大致行号区间（参考 4.2）。
3. **依赖与流水线**：画一张包含 typst-syntax / typst-eval / typst-library / comemo 四者的数据流简图，并用一句话说明 `Tracked` 参数与 `#[comemo::memoize]` 的关系（参考 4.3）。

**验收标准**：

- 「定位」里明确区分了解析（typst-syntax）与求值（typst-eval）、以及运行时类型来自 typst-library。
- 「入口」的 6 步与源码行号对得上，且提到了「有语法错误时在 inspect 模式下仍继续求值」这个细节。
- 「依赖与流水线」的图能体现「文本 → 树 → 值」的主线，并提到 comemo 缓存依赖 tracked 参数判等。

> 提示：完成后可以把这份速览与本文 4.1.2 / 4.2.2 / 4.3.2 的伪代码和图对照，查漏补缺。

---

## 6. 本讲小结

- typst-eval 是 Typst 的**代码解释器（tree-walking interpreter）**，职责是把 AST 求值成运行时值（`Value` / `Content` / `Module`）；它**不负责解析**（那是 typst-syntax），**不负责定义运行时类型**（那是 typst-library）。
- 全 crate 的核心抽象是 [`Eval` trait`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L178-L184)（为每类 AST 节点实现 `eval`）与 [`Vm` 虚拟机](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L16-L28)（携带求值状态）。
- 顶层入口 [`eval()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L38-L97) 把一个 `Source` 求值成 `Module`，走「循环防护 → 准备 Engine → 准备 Vm → 收集错误/警告 → 求 Markup → 装 Module」6 步。
- 第二入口 [`eval_string()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L101-L175) 按 Code/Markup/Math 三种模式求值一段字符串，返回单个 `Value`。
- 三大核心依赖：`typst-syntax`（上游，提供 AST）、`typst-library`（运行时类型）、`comemo`（`#[memoize]` 缓存，配合 `Tracked` 参数判等）。
- 数据流主线：**源码文本 →（typst-syntax）解析 → AST →（typst-eval）求值 → Module / Value（typst-library 类型）**。

---

## 7. 下一步学习建议

本讲只鸟瞰了 `lib.rs` 与 `Cargo.toml`。接下来建议按手册顺序继续：

- **u1-l2 源码模块结构地图**：逐个认识 `src/` 下 13 个文件（`code.rs` / `markup.rs` / `math.rs` / `flow.rs` / `call.rs` / `binding.rs` / `ops.rs` / `access.rs` / `methods.rs` / `rules.rs` / `import.rs` / `vm.rs`）各自负责求值哪类结构，并搞清 `lib.rs` 通过 `pub use` 对外暴露了哪些 API。
- **u1-l3 解释器入口：eval 与 eval_string**：比本讲更细地拆解两个入口，重点啃 `comemo::memoize`、`Route` 循环防护、`Engine`/`Sink`/`Traced` 的协作。
- **u1-l4 Eval trait 与 Vm 虚拟机**：深入 `Vm` 的 `define` / `bind` / `trace_at` / `trace` 方法，理解 trace 与 IDE 追踪的关系。

读完第 1 单元四篇，你就能带着「整体地图」进入第 2 单元，开始逐节点类型地精读求值代码了。
