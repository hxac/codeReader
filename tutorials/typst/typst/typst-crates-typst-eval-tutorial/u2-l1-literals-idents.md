# 基础字面量与标识符求值

## 1. 本讲目标

在 [u1-l4](./u1-l1-eval-trait-vm.md) 里，我们把两块基石——`Eval` trait 与 `Vm` 虚拟机——讲透了，并且把 `ast::Expr::eval` 那个「大 match」当作「总分发器」的范例提了一句。本讲就从那句话正式走进 `code.rs`，把表达式求值拆开来看。

具体说，本讲聚焦「最朴素的求值」：那些不需要调用函数、不需要循环、不需要解构的东西——**字面量**（`none`/`auto`/`true`/`1`/`1.5`/`12pt`/`"hi"`）与**标识符**（变量名 `x`）。它们是解释器里「叶子节点」的求值，理解了它们，后续的数组、字典、调用、控制流才有地基。

读完本讲你应该能：

1. 看懂 `ast::Expr::eval` 这个**总分发器**：它如何用一个 `match` 把几十种表达式变体派发到各自的 `Eval` 实现，为什么部分分支要 `.map(Value::Content)`，以及为什么 `set`/`show` 在「表达式上下文」里被直接报错。
2. 说出每一类字面量（`None`/`Auto`/`Bool`/`Int`/`Float`/`Numeric`/`Str`）的 `Eval` 实现各做了什么，以及它们为什么大多「不需要用到 `Vm`」。
3. 把整数字面量的错误诊断（`int_literal_error` + `find_bad_digits`）讲清楚：溢出与非法数字两条路径、`SubRange` 如何精确高亮坏数字位、以及「错误信息 + 定位 + 修复提示」三要素。
4. 解释标识符求值（`Ident::eval`）如何经 `Scopes::get` 在作用域栈里逐层查找绑定、用 `.at(span)` 把字符串错误转成带 span 的诊断、再用 `read_checked` 做读取检查，并满足统一的 `trace_at` 调用约定。

## 2. 前置知识

本讲承接 [u1-l4](./u1-l1-eval-trait-vm.md)，假设你已熟悉以下概念（不重复展开）：

- **`Eval` trait**：`fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>`，关联类型 `Output` 决定返回类型。表达式 `ast::Expr` 的 `Output` 是 `Value`。
- **`Vm` 与 `trace_at` 调用约定**：`Vm::trace_at(span, value)` 是「条件式 trace」，只有 span 命中 `inspected` 时才真正记录值供 IDE hover。`ast::Expr::eval` 末尾统一调一次 `trace_at`，替几乎所有表达式节点满足这条约定。
- **`Value`**：typst-library 定义的「任意运行时值」枚举，如 `Value::None`、`Value::Int(i64)`、`Value::Float(f64)`、`Value::Str(Str)`、`Value::Content(Content)` 等。`Value::spanned(span)` 只在值是 `Content`/`Func` 时贴 span，其余原样返回。
- **`SourceResult<T>` / `HintedStrResult`**：前者是 `Result<T, EcoVec<SourceDiagnostic>>`（带 span 的最终诊断），后者是「只带字符串 + 可选提示」的轻量错误类型。`.at(span)` 把后者转成前者。
- **`Span`**：源码位置标记，每个 AST 节点都有一个；错误高亮、IDE hover 都靠它定位。

一个常被混淆的点先澄清：**字面量**和**标记（markup）节点**是两回事。`1`、`"hi"`、`true` 这类「代码模式」里的常量是字面量（本讲主角）；而 `*粗体*`、`标题`、`- 列表项` 这类「标记模式」里的文本结构，求值后产出 `Content`，是 [u2-l4](./u2-l4-markup.md) 的主角。本讲的「总分发器」会同时看到这两类，但只在字面量这一层细讲。

## 3. 本讲源码地图

本讲几乎全部落在 `code.rs`，并跨到 typst-syntax / typst-library 两个依赖 crate：

| 文件 | 所属 crate | 作用 | 本讲关注的内容 |
|------|-----------|------|----------------|
| [`src/code.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) | typst-eval | 代码模式的总分发与各类节点 `Eval` | `ast::Expr::eval` 大 match、各字面量 impl、`Ident` impl、`int_literal_error`、`find_bad_digits` |
| [`src/vm.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs) | typst-eval | `Vm` 方法 | `trace_at`（调用约定的承接，[u1-l4](./u1-l1-eval-trait-vm.md) 已细讲） |
| [`src/ast.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs) | typst-syntax | AST 节点定义与文本解析 | `Int::get`（返回 `Result`）、`IntLiteralError`、`NonDecimalBase` |
| [`src/foundations/scope.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs) | typst-library | 作用域与绑定 | `Scopes::get`（逐层查找）、`Binding::read_checked` |

> 说明：typst-syntax 的 `ast.rs` 是 `code.rs` 的**上游输入**——它只负责「把文本切成 AST 并提供 `.get()` 取值」，求值逻辑在 typst-eval 这边。跨 crate 引用时我们会标注清楚归属。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **`ast::Expr::eval` 总分发器**：表达式求值的「总入口」与语法约束。
2. **字面量求值**：从语法节点到 `Value`（None/Auto/Bool/Float/Numeric/Str）。
3. **整数字面量与错误诊断**：`Int` / `int_literal_error` / `find_bad_digits`。
4. **标识符求值**：`Ident` / `Scopes::get` / `read_checked`。

---

### 4.1 `ast::Expr::eval`：表达式总分发器

#### 4.1.1 概念说明

`ast::Expr` 是 typst-syntax 里「表达式」的大枚举，把代码模式里**所有**能作为表达式的东西打包在一起：字面量、标识符、数组、字典、函数调用、`if`/`for`、闭包、运算符……几十种。

如果给每种各起一个求值函数名，调用方就得记住「这个节点调哪个函数」。`Eval` trait 的解法是统一成 `eval`，但 `ast::Expr` 是枚举，所以它的 `eval` 实现天然是一个**大 `match`**：每个变体派发到「该变体内层节点自己的 `Eval` impl」。这正是 [u1-l4](./u1-l1-eval-trait-vm.md) 提到的「外层枚举做总分发、每个具体类型各司其职」。

除了「分发」，`ast::Expr::eval` 还承担三件事：

- **类型适配**：少数 markup/math 节点的 `eval` 产出的是 `Content`，而 `Expr` 要求统一返回 `Value`，于是用 `.map(Value::Content)` 包一层。
- **语法约束**：`set`/`show` 是「语句级」的样式指令，不能作为子表达式嵌套，于是在这里被拦截报错。
- **统一善后**：所有分支求值完后，统一做 `.spanned(span)` 贴位置、调一次 `trace_at` 服务 IDE。

#### 4.1.2 核心流程

`ast::Expr::eval` 的执行可以画成：

```text
ast::Expr 节点 self
   │
   ├── 记录 span = self.span()
   ├── 定义 forbidden 闭包（拦截 set/show）
   ▼
   match self { ... }                        ← 总分发
   │   · 大多数分支：把内层节点 v 再 v.eval(vm)
   │   · 产出 Content 的节点：.eval(vm).map(Value::Content)
   │   · Array/Dict：.eval(vm).map(Value::Array / Value::Dict)
   │   · SetRule/ShowRule：bail!(forbidden("set"/"show"))   ← 报错
   ▼
   得到 value，用 ? 传播错误
   │
   ├── value.spanned(span)                   ← 贴位置（仅 Content/Func 生效）
   ├── vm.trace_at(span, &value)             ← 统一满足 trace 调用约定
   ▼
   Ok(value)
```

注意 `set`/`show` 的特殊待遇：它们在「代码块/内容块的语句流」里是合法的（由同文件的 `eval_code` 专门处理），但在「表达式上下文」里不合法。这两者的区别，是理解 `forbidden` 闭包的关键。

#### 4.1.3 源码精读

**`forbidden` 闭包**先于 `match` 定义：

[`src/code.rs`:L80-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L80-L83) —— 接收一个名字，产出一条带 span 的错误「`{name}` is only allowed directly in code and content blocks」。它用 `error!` 宏构造诊断，span 就是当前表达式自身的 span。

**大 `match` 的三种分支形态**：

[`src/code.rs`:L85-L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L85-L147) —— 可以观察到三类写法：

1. **直接派发**（最多）：如 `Self::Int(v) => v.eval(vm)`。内层 `v`（这里是 `ast::Int`）有自己的 `Eval` impl，产出的已经是 `Value`，原样返回。
2. **包一层 `Value`**：如 `Self::Strong(v) => v.eval(vm).map(Value::Content)`。`Strong`（`*粗体*`）的 `eval` 产出 `Content`，`Expr` 要 `Value`，所以 `.map` 把 `Content` 包成 `Value::Content`。同理 `Self::Array(v) => v.eval(vm).map(Value::Array)`、`Self::Dict(v) => v.eval(vm).map(Value::Dict)`。
3. **报错**：`Self::SetRule(_) => bail!(forbidden("set"))` 与 `Self::ShowRule(_) => bail!(forbidden("show"))`。

**善后两步**：

[`src/code.rs`:L147-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L147-L154) —— 先 `.spanned(span)` 给结果贴位置，再 `vm.trace_at(span, &value)`。前面那行注释点明这条约定的意义：

```rust
// This satisfies the obligation to call `Vm::trace` for almost all
// value-producing expressions!
vm.trace_at(span, &value);
```

也就是说：因为几乎所有表达式都经此出口，在这里调一次 `trace_at` 就替它们集体满足了「要 trace」的义务（[u1-l4](./u1-l1-eval-trait-vm.md) 已论证过「全量埋点、近乎零成本」）。

**`set`/`show` 在语句流里为何合法**：对比同文件的 `eval_code`：

[`src/code.rs`:L35-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L35-L57) —— 当 `eval_code` 迭代表达式流时，遇到 `SetRule`/`ShowRule` 会**特殊处理**：先求值出 `styles`/`recipe`，再递归求值「后续所有表达式（tail）」，最后把样式/规则应用到 tail 上。也就是说，`set`/`show` 的语义依赖于「它后面还跟着一段内容」。而作为**子表达式**（比如 `#(set text(size: 12pt))` 塞进括号里），它后面没有 tail，没有可应用的对象，所以 `ast::Expr::eval` 用 `forbidden` 把它拦下。这正是「报错」的根因：不是语法解析失败，而是它在表达式位置上**没有定义良好的值语义**。

#### 4.1.4 代码实践

**实践目标**：精读大 `match`，归纳 `.map(Value::Content)` 的分布，并解释 `set`/`show` 为何被 `forbidden` 拦截。

**操作步骤**：

1. 打开 [`src/code.rs`:L85-L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L85-L147)。
2. 把所有 `.map(Value::Content)` 的分支挑出来（约二十多个）。观察它们的共同点：都是**标记/数学/内容类节点**——`Text`、`Space`、`Linebreak`、`Parbreak`、`SmartQuote`、`Strong`、`Emph`、`Raw`、`Link`、`Ref`、`Heading`、`ListItem`、`EnumItem`、`TermItem`、`Equation`、`Math`、`MathText`、`MathAlignPoint`、`MathDelimited`、`MathAttach`、`MathPrimes`、`MathFrac`、`MathRoot`、`ContentBlock`、`Contextual`、`ModuleInclude`。
3. 再找出另外两种 `.map`：`Self::Array(v) => v.eval(vm).map(Value::Array)`、`Self::Dict(v) => v.eval(vm).map(Value::Dict)`。
4. 定位 `forbidden` 闭包（[L80-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L80-L83)）与它的两处触发点（[L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L136)、[L137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L137)）。
5. 对照 `eval_code`（[L35-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L35-L57)）理解 set/show 在「语句流」里的合法用法。

**需要观察的现象**：

- `.map(Value::Content)` 只贴在「本该产出 `Content`」的节点上，目的是把它提升进表达式的 `Value` 表示。
- 不带 `.map` 的分支（如 `Ident`/`None`/`Int`/`CodeBlock`/`FuncCall`）其内层 `eval` 本就直接产出 `Value`，无需转换。
- `set`/`show` 没有「求值成某个值」的分支，只有 `bail!`——它们在表达式位置上无值可产。

**预期结果**：你能解释——「`.map(Value::Content)` 的分支都是标记/数学/内容节点，它们求值产出 `Content`，需要包成 `Value::Content` 才符合 `Expr` 统一的 `Value` 返回类型；而 set/show 之所以在表达式上下文报错，是因为它们是依赖『后续 tail』的样式指令，只有在 `eval_code` 的语句流里才有意义，作为子表达式没有可应用的 tail，故被 `forbidden` 拦截。」待本地验证：写 `#(set text(size: 12pt))` 用 typst CLI 编译，应看到 `set is only allowed directly in code and content blocks`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Self::Array(v)` 用 `.map(Value::Array)`，而 `Self::Ident(v)` 不需要任何 `.map`？

> **答案**：`ast::Array` 的 `Eval` 实现把 `Output` 声明成 `Array`（见 [code.rs:232](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L232)），而 `Expr` 要 `Value`，所以 `.map(Value::Array)` 把 `Array` 包成 `Value::Array`。`ast::Ident` 的 `Output` 直接就是 `Value`（[code.rs:159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L159)），自然不需要再包。是否需要 `.map`，完全取决于内层节点的 `Output` 类型。

**练习 2**：`forbidden` 闭包依赖的 `span` 是哪个？为什么用它而不是 `Span::detached()`？

> **答案**：用的是当前表达式自身的 `span`（[code.rs:80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L80)）。用它能把错误精确指到那个 `set`/`show` 上，让用户一眼看到该改哪里；`Span::detached()` 没有位置信息，高亮不出来，体验很差。

**练习 3**：如果用户在代码块顶层直接写一条 `set text(size: 12pt)`（不在括号里），会触发 `forbidden` 吗？

> **答案**：不会。顶层代码块走的是 `eval_code`（[code.rs:26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L26)），它把 `SetRule` 当作语句特殊处理（[L36-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L36-L44)），不会进 `ast::Expr::eval` 的 `SetRule` 分支。`forbidden` 只在「set/show 作为子表达式被求值」时才命中。

---

### 4.2 字面量求值：从语法节点到 Value

#### 4.2.1 概念说明

字面量是「写在源码里的常量」。typst 的代码模式字面量有这几类：

| Typst 写法 | AST 节点 | 求值结果 `Value` |
|-----------|----------|------------------|
| `none` | `ast::None` | `Value::None` |
| `auto` | `ast::Auto` | `Value::Auto` |
| `true` / `false` | `ast::Bool` | `Value::Bool(bool)` |
| `42`、`0xff`、`0b101` | `ast::Int` | `Value::Int(i64)`（可能求值失败） |
| `1.5`、`10e-4` | `ast::Float` | `Value::Float(f64)` |
| `12pt`、`90deg` | `ast::Numeric` | `Value::numeric(...)`（带单位的量） |
| `"hi"`、`#"hi"#` | `ast::Str` | `Value::Str(Str)` |

注意一个反差：**字面量的求值逻辑本身极简**——多数就是「取出文本里编码的值，塞进对应的 `Value` 变体」。真正的难点在两处：`Int` 可能求值失败（下个模块讲），以及这些 impl **几乎都不需要 `Vm`**。

为什么不需要 `Vm`？因为字面量是「自包含」的——它的值完全由源码文本决定，既不查作用域，也不产生控制流，更不触发 trace（trace 由外层 `Expr::eval` 统一负责）。所以它们的签名是 `fn eval(self, _: &mut Vm)`，那个 `_: &mut Vm` 表示「我收到了 vm 但用不上」。

#### 4.2.2 核心流程

```text
字面量节点（如 ast::Bool）
   │
   ▼
.eval(_: &mut Vm)            ← vm 被忽略
   │  · 取出文本里编码的值（.get()）
   │  · 包进对应 Value 变体
   ▼
Ok(Value::...)               ← 直接成功（Int 除外）
   │
   ▼（回到 ast::Expr::eval）
.spanned(span) + trace_at    ← 善后由总分发器统一做
```

`Numeric` 略特殊：它返回的是「带单位的量」，由 `Value::numeric` 按 `ast::Unit`（pt/mm/cm/in/rad/deg/...）分派成对应的 `Abs`/`Angle` 等类型再 `into_value()`，但这对本讲而言是个黑盒（属于 typst-library 的度量类型），我们只关注它「从 `(f64, Unit)` 产出 `Value`」这一步。

#### 4.2.3 源码精读

**`None` / `Auto`**：

[`src/code.rs`:L172-L186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L172-L186) —— 各一行：直接 `Ok(Value::None)` / `Ok(Value::Auto)`。参数是 `_: &mut Vm`，完全不用 vm。

**`Bool`**：

[`src/code.rs`:L188-L194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L188-L194) —— `Ok(Value::Bool(self.get()))`。`self.get()` 从语法节点取出布尔值。

**`Float` / `Numeric` / `Str`**：

[`src/code.rs`:L207-L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L207-L229) —— 三者同样直白：

```rust
// Float
Ok(Value::Float(self.get()))
// Numeric
Ok(Value::numeric(self.get()))
// Str
Ok(Value::Str(self.get().into()))
```

其中 `Str` 的 `self.get()` 返回 `EcoString`，`.into()` 转成 `typst_library::Str`；`Numeric` 的 `self.get()` 返回 `(f64, ast::Unit)`。这些 `.get()` 都定义在 typst-syntax 的 `ast.rs` 里（例如 `Str::get` 解析转义、`Float::get` 解析浮点），本讲不展开解析细节，只强调「取值在 typst-syntax，包装成 `Value` 在 typst-eval」。

**关于 `spanned` 与字面量**：字面量产出的 `Value` 多数不是 `Content`/`Func`，所以回到 `Expr::eval` 后的 `.spanned(span)` 对它们是空操作（参见 typst-library 的 `Value::spanned`：只有 `Value::Content`/`Value::Func` 才贴 span，其余原样返回）。这也印证了字面量「自包含、轻量」的特性。

#### 4.2.4 代码实践

**实践目标**：通过对比字面量 impl 的签名与实现，体会「叶子节点不需要 Vm」这一结构特征。

**操作步骤**：

1. 打开 [`src/code.rs`:L172-L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L172-L229)，确认 `None`/`Auto`/`Bool`/`Float`/`Numeric`/`Str` 六个 impl 的签名都是 `fn eval(self, _: &mut Vm)`。
2. 思考：为什么这些 impl 收到 `&mut Vm` 却不用？把它们和 `Ident::eval`（下个模块，要用 `vm.scopes`）对比。
3. 在 typst-syntax 的 `ast.rs` 里找到 `Float::get`、`Str::get`（例如 [`ast.rs`:L1430 附近](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1430)），确认「把文本解析成数值/字符串」发生在上游 crate。

**需要观察的现象**：

- 字面量 impl 体里没有任何 `vm.xxx` 调用——它们是纯函数式的「取值 + 包装」。
- 所有「读作用域、写 trace、改 flow」的能力都在 `Vm` 上，字面量不需要这些，所以能保持极简。

**预期结果**：你能用一句话总结——「字面量求值之所以极简（一行 `Ok(Value::...)`），是因为它的值完全由源码文本决定，不依赖任何运行时状态，于是 `Vm` 被整体忽略；真正用到 `Vm` 的能力（作用域查找、trace、控制流）要从标识符、调用、循环等节点才开始。」

#### 4.2.5 小练习与答案

**练习 1**：`Bool::eval` 的参数为什么写成 `_: &mut Vm` 而不是干脆不传 `Vm`？

> **答案**：因为 `Eval` trait 的方法签名是统一的 `fn eval(self, vm: &mut Vm)`（[lib.rs:183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L183)）。每个 impl 都必须接收 `&mut Vm` 才符合 trait 契约；用不上的就写成 `_` 显式忽略。统一签名让调用方能无差别地 `node.eval(&mut vm)`。

**练习 2**：`Numeric::eval` 产出的是 `Value::Float` 吗？

> **答案**：不是。`12pt`、`90deg` 这类是**带单位的量**，由 `Value::numeric((f64, Unit))` 分派成对应的度量类型（如 `Abs` 绝对长度、`Angle` 角度）再包成 `Value`（见 [value.rs:100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L100)）。`Value::Float` 只对应无单位的浮点字面量 `1.5`。

**练习 3**：`Value::spanned(span)` 对 `Value::Int(42)` 有什么效果？

> **答案**：没有效果。`Value::spanned` 只在匹配到 `Value::Content` 或 `Value::Func` 时贴 span，其余变体原样返回（[value.rs:212-L218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L212-L218)）。所以整数字面量的 span 主要靠 `trace_at` 与错误诊断使用，而非存在 `Value` 里。

---

### 4.3 整数字面量与错误诊断：Int / int_literal_error / find_bad_digits

#### 4.3.1 概念说明

整数是唯一「可能求值失败」的字面量。失败分两种：

1. **溢出**：如 `99999999999999999999999` 超过 `i64` 范围。
2. **非法数字**：如 `0x1g`——`0x` 表示十六进制，但 `g` 不是合法十六进制位。

这里的精妙之处在于：**非法数字的判定与高亮分了工**。typst-syntax 的 `Int::get()` 只负责「尝试解析」并报告「哪段出了 `InvalidDigit` 错误」，而 typst-eval 的 `int_literal_error` 负责把它变成对用户友好的诊断——错误信息、精确高亮哪个坏数字、以及「这个进制只允许哪些数字」的修复提示。

更细一层：`int_literal_error` 又把「找出具体哪些字符坏掉了」委托给 `find_bad_digits`。这个函数返回一个**子区间**（`Range<usize>`）和一个坏字符列表，`int_literal_error` 再把子区间偏移两位（跳过 `0x`/`0o`/`0b` 前缀），用 `SubRange` + `DiagSpan` 精确高亮。

这整套设计是 typst-eval「**错误信息 + 定位 + 修复提示**」三要素诊断哲学的典范（[u6-l1](./u6-l1-diagnostics.md) 会系统汇总）。

#### 4.3.2 核心流程

整数求值与诊断的链路：

```text
ast::Int 节点
   │
   ▼
Int::get()                              ← 在 typst-syntax
   │  · strip_prefix 去掉 0x/0o/0b，得 (base, digits)
   │  · i64::from_str_radix(digits, base) 或 digits.parse()
   ▼
Ok(i64)  ─────────────────────────────► Value::Int(i64)
Err(IntLiteralError)
   │   · PosOverflow { base, max_plus_one }
   │   · InvalidDigit(base, digits)
   ▼
int_literal_error(int, err)             ← 回到 typst-eval (code.rs)
   │  · PosOverflow：提示「太大」，base 为十进制时建议改浮点
   │  · InvalidDigit：调 find_bad_digits(base, digits)
   ▼
find_bad_digits(base, digits)
   │  · 按进制确定「非法字符区间」（hex: g-z/G-Z；octal: 8-9；binary: 2-9）
   │  · 扫描 digits，收集坏字符，算出最小覆盖区间
   ▼
返回 (Range<usize>, Vec<char>)
   │
   ▼
int_literal_error 把区间 +2 偏移（跳过前缀）→ SubRange → DiagSpan
   ▼
error!(span, ...; hint[hint_span]: ...; hint: ...)   ← 精确高亮 + 修复提示
```

#### 4.3.3 源码精读

**`Int::eval`**：

[`src/code.rs`:L196-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L196-L205) —— 对 `self.get()` 的结果做 match：成功得 `Value::Int(int)`；失败则把 `err` 交给 `int_literal_error` 构造**单个**诊断，包进 `eco_vec!` 返回。注意它返回的是 `Err(eco_vec![...])`，即一条诊断的向量——typst 的错误永远是 `EcoVec<SourceDiagnostic>`。

**`Int::get()`（上游，typst-syntax）**：

[`src/ast.rs`:L1345-L1375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1345-L1375) —— 先 `NonDecimalBase::strip_prefix` 去前缀得 `(base, digits)`，再按 `base` 用 `i64::from_str_radix` 或十进制 `parse`。关键是它把 `std::num::IntErrorKind` 翻译成自家两种错误：

- `InvalidDigit` → `IntLiteralError::InvalidDigit(base, digits)`（只有非十进制会遇到，因为十进制的非法字符已被词法器挡掉）。
- 其余（`PosOverflow`/`NegOverflow`/`Empty`/`Zero` 等）一律当作**正溢出**处理（Typst 没有负整数字面量），得 `IntLiteralError::PosOverflow { base, max_plus_one }`。

注释还点出一个用意：「我们仍想把 `9223372036854775808`（i64::MAX+1）当成整数来高亮和处理」，这就是为什么 `get()` 返回 `Result` 而不是直接 `i64`——即使溢出，IDE 也能知道「这是一个整数节点」。

**`IntLiteralError` 与 `NonDecimalBase`**：

[`src/ast.rs`:L1380-L1423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1380-L1423) —— `IntLiteralError` 两个变体；`NonDecimalBase` 枚举 `Hex=16 / Octal=8 / Binary=2`，带 `name()`（"hexadecimal"/"octal"/"binary"）和 `strip_prefix`（识别 `0x`/`0o`/`0b`）。

**`int_literal_error`（本模块核心之一）**：

[`src/code.rs`:L436-L498](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L436-L498) —— 标了 `#[cold]`（错误路径很少走）。两个分支：

- **`PosOverflow`**（[L441-L457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L441-L457)）：错误信息「integer value is too large」，必带提示「value does not fit into a signed 64-bit integer」；若 `base.is_none()`（十进制），再追加两条「可用浮点近似」和「在末尾加点变成浮点：`{文本}.`」的提示——**注意：只有十进制溢出才给浮点建议，`0xFF...` 这类非十进制溢出不给**，因为给非十进制加 `.` 不能变成合法浮点。
- **`InvalidDigit`**（[L458-L496](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L458-L496)）：先 `find_bad_digits(base, digits)` 得 `(range, bad_digits)`，再 `SubRange::new(range.start + 2, range.end + 2)`——**这个 `+2` 就是为了跳过 `0x`/`0o`/`0b` 两个前缀字符**，把「digits 内部的偏移」换算回「整个字面量里的绝对偏移」。然后用 `DiagSpan::from_span(span, sub_range)` 构造一个只覆盖坏字符的精确高亮区。错误信息形如「integer contains digits that are not valid for a hexadecimal number」（八进制会多一个 "n" 变成 "an octal number"），并带两条 hint：一条用 `hint[hint_span]` 精确指明「the digit `g` is invalid」，另一条说明「hexadecimal numbers only allow digits 0-9, a-f, A-F」。

`bad_digits` 的展示还按数量分了三种措辞（[L463-L481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L463-L481)）：单个坏数字、两个坏数字、三个及以上坏数字，措辞各不同，保证读起来自然。

**`find_bad_digits`（本模块核心之二）**：

[`src/code.rs`:L500-L527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L500-L527) —— 先按进制确定「非法字符区间」：

```rust
ast::NonDecimalBase::Hex    => &['g'..='z', 'G'..='Z'],  // hex 合法: 0-9 a-f A-F
ast::NonDecimalBase::Octal  => &['8'..='9'],             // octal 合法: 0-7
ast::NonDecimalBase::Binary => &['2'..='9'],             // binary 合法: 0-1
```

然后对每个非法区间里的字符，用 `digits.find(c)` 找它在 `digits` 中**第一次出现**的位置（所以同一坏字符最多被收集一次），记录 `(index, char)`；最后算出所有命中位置的最小覆盖区间 `start..end` 和坏字符列表 `bad_digits`。返回 `(Range<usize>, Vec<char>)`——注意这里返回的是 **`digits` 内部的偏移**（不含前缀），前缀偏移由调用方 `int_literal_error` 补上。

#### 4.3.4 代码实践

**实践目标**：手工推演 `find_bad_digits` 对一个含非法字符的 `0x` 整数的返回值，把「诊断如何精确高亮」走通。

**操作步骤**：

1. 假设用户写了 `0x1g`。
2. 在 [`ast.rs`:L1350-L1354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1350-L1354) 推演 `Int::get()`：`strip_prefix("0x1g")` 得 `(Some(Hex), "1g")`；`i64::from_str_radix("1g", 16)` 失败，错误种类 `InvalidDigit` → `IntLiteralError::InvalidDigit(Hex, "1g")`。
3. 进入 [`code.rs`:L458-L459](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L458-L459)，调 `find_bad_digits(Hex, "1g")`。
4. 在 [`code.rs`:L505-L527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L505-L527) 推演：hex 的非法区间是 `g..=z` 与 `G..=Z`；在 `"1g"` 里，`'g'` 出现在 index `1`，其余非法字符都不出现。于是 `start = 1`、`end = 2`、`bad_digits = ['g']`。返回 `(1..2, vec!['g'])`。
5. 回到 `int_literal_error`（[L461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L461)）：`SubRange::new(1+2, 2+2) = SubRange::new(3, 4)`。在 `"0x1g"` 里（下标 0='0',1='x',2='1',3='g'），区间 `[3,4)` 正好只覆盖坏字符 `g`。

**需要观察的现象**：

- `find_bad_digits` 返回的是 **`digits` 内部偏移**（`1..2`），不含 `0x` 前缀；`int_literal_error` 用 `+2` 把它换算成全字面量的绝对偏移（`3..4`）。
- 同一坏字符只收集一次（`digits.find` 返回首次出现）；多个不同坏字符则全部收集，并算出覆盖它们的最小区间。

**预期结果**：对 `0x1g`，`find_bad_digits` 返回 `(1..2, ['g'])`；最终诊断会精确高亮 `g`，信息为「integer contains digits that are not valid for a hexadecimal number」，提示「the digit `g` is invalid」「hexadecimal numbers only allow digits 0-9, a-f, A-F」。**延伸**：若写 `0x1gz`，`find_bad_digits(Hex, "1gz")` 返回 `(1..3, ['g', 'z'])`（`g`@1、`z`@2，最小区间 `1..3`），`+2` 后高亮 `3..5`，提示措辞变成「the digits `g` and `z` are invalid」。待本地验证：用 typst CLI 编译含 `#0x1g` 的文件，观察报错高亮与提示文本。

#### 4.3.5 小练习与答案

**练习 1**：十进制超大整数 `99999999999999999999999` 求值时，`Int::get()` 返回什么？诊断会附带哪些提示？

> **答案**：`Int::get()` 走十进制 `digits.parse::<i64>()`，溢出 → `IntLiteralError::PosOverflow { base: None, max_plus_one: ... }`。诊断信息「integer value is too large」，因 `base.is_none()`（[L447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L447)），额外附两条提示：「可用浮点近似」「在末尾加点变成浮点：`99999999999999999999999.`」。

**练习 2**：为什么 `0xFFFFFFFFFFFFFFFFFF`（十六进制溢出）**不会**给出「改成浮点」的提示？

> **答案**：因为它的 `base = Some(Hex)`，`int_literal_error` 里 `if base.is_none()` 不成立（[L447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L447)），所以只给「does not fit into a signed 64-bit integer」一条提示。给十六进制加 `.` 不能变成合法浮点字面量，提示反而是错的，所以设计上排除了。

**练习 3**：`find_bad_digits` 为什么用 `digits.find(c)`（首次出现）而不是收集所有出现位置？

> **答案**：为了让每个坏字符在提示里**最多出现一次**（[L514 注释](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L514)："Yield at most one copy of each digit"）。比如 `0x1gg`，提示只需说「the digit `g` is invalid」一次，而不必重复罗列。高亮区间则取所有命中位置的最小覆盖，仍能把所有坏位都框进去。

---

### 4.4 标识符求值：Ident / Scopes::get / read_checked

#### 4.4.1 概念说明

标识符（`Ident`）就是变量名，如 `x`、`total`、`page`。它的求值是「最典型的非字面量叶子」：值不在源码文本里，而在**运行时作用域**里。所以 `Ident::eval` 是本讲里**第一个真正用到 `Vm`** 的叶子求值——它要查 `vm.scopes`。

查一个变量名，要回答三个问题：

1. **去哪查**：typst 的作用域是一个**栈**（`Scopes`），由「最顶层作用域 → 外层作用域 → 标准库」逐层查找，第一个命中即返回。
2. **找不到怎么办**：返回「未知变量」错误，并贴上标识符的 span。
3. **找到了还要检查什么**：用 `read_checked` 做读取检查——主要是「弃用（deprecation）」警告（如果这个绑定被标记为已弃用，读取时发警告）。

此外，`Ident::eval` 同样满足 `trace_at` 调用约定——不过它不是自己调，而是回到 `ast::Expr::eval` 末尾由总分发器统一调。

#### 4.4.2 核心流程

```text
ast::Ident 节点（如 total）
   │
   ▼
vm.scopes.get(&self)                     ← 逐层查找（Deref 成 &str）
   │  · top 作用域 → 外层 scopes（逆序）→ base 标准库（含特殊 "std"）
   ▼
HintedStrResult<&Binding>
   │  找不到 → unknown_variable(name)
   ▼
.at(span)?                               ← 字符串错误 → 带 span 的诊断
   ▼
.read_checked((&mut vm.engine, span))    ← 读取检查（弃用警告）
   │
   ▼
.clone()                                 ← 取出 Value 的拷贝
   ▼
Ok(Value)
   │（回到 ast::Expr::eval）
   └── .spanned(span) + trace_at         ← 统一善后
```

一个实现细节：`vm.scopes.get(&self)` 里的 `&self` 是 `&ast::Ident`，而 `Scopes::get` 形参是 `&str`。这能编译通过，是因为 `ast::Ident` 实现了 `Deref<Target = str>`（定义在 typst-syntax 的 `ast.rs`），`&Ident` 会自动解引用强制转换成 `&str`。

#### 4.4.3 源码精读

**`Ident::eval`**：

[`src/code.rs`:L158-L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158-L170) —— 四步链式调用，紧凑地表达了上一节的流程：

```rust
fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
    let span = self.span();
    Ok(vm
        .scopes
        .get(&self)                                   // ① 逐层查找
        .at(span)?                                    // ② 错误贴 span
        .read_checked((&mut vm.engine, span))         // ③ 读取检查
        .clone())                                     // ④ 取出 Value
}
```

注意 `.read_checked(...)` 返回的是 `&Value`，最后 `.clone()` 得到独立的 `Value`。为什么必须 clone？因为 `Binding` 里持有这个值，作用域里可能多次引用同一个绑定，求值结果要返回一个不被作用域生命周期绑定的独立值。

**`Scopes::get`（依赖 crate，typst-library）**：

[`src/foundations/scope.rs`:L46-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59) —— 查找顺序一目了然：

```rust
std::iter::once(&self.top)            // 最顶层作用域
    .chain(self.scopes.iter().rev())  // 外层作用域（由内向外）
    .find_map(|scope| scope.get(var))
    .or_else(|| {                     // 都没找到 → 查标准库
        let base = self.base?;
        match base.global.scope().get(var) {
            Some(binding) => Some(binding),
            None if var == "std" => Some(&base.std),  // 特殊：std 指向标准库自身
            None => None,
        }
    })
    .ok_or_else(|| unknown_variable(var))
```

返回 `HintedStrResult<&Binding>`——「带提示的字符串结果」，比 `SourceResult` 轻（不带 span），后续靠 `.at(span)` 补 span。这里还体现一个细节：`std` 这个名字被特殊处理，它绑定到「标准库自身」（这样用户写 `std.array` 能访问被覆盖的内置函数，呼应 [u1-l4](./u1-l1-eval-trait-vm.md) 讲过的 `hint_if_shadowed_std`）。

**`Binding::read_checked`（依赖 crate）**：

[`src/foundations/scope.rs`:L305-L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L305-L310) —— 注释说明：传 `()` 忽略弃用消息；传 `(&mut engine, span)` 则把弃用警告发进 engine。`Ident::eval` 传的是 `(&mut vm.engine, span)`，所以读取一个已弃用的绑定时会真正发出警告。它返回 `&Value`（未弃用时就是 `&self.value`）。

**关于 `trace_at`**：`Ident::eval` 自身没有调 `trace_at`，但它返回的 `Value` 会经过 `ast::Expr::eval` 末尾的 `vm.trace_at(span, &value)`（[code.rs:152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L152)）。所以 IDE hover 一个变量名时，正是这一步把它的值送进 sink。

#### 4.4.4 代码实践

**实践目标**：追踪一次变量读取的完整链路，把「查找 → 错误转换 → 读取检查 → clone → trace」串起来。

**操作步骤**：

1. 准备一段心算用的 Typst 代码（markup 模式）：`#let total = 40 + 2` 后跟 `#total`。
2. 对 `#total` 这条：它经 `ast::Expr::Ident` → `Ident::eval`（[code.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158)）。
3. 推演 `vm.scopes.get("total")`：先查 `top` 作用域——由于上一条 `let` 刚通过 `Vm::define` 把 `total` 绑进 `top`（[u1-l4](./u1-l1-eval-trait-vm.md) 讲过 `define`/`bind`），所以 `top.get("total")` 命中，返回那个 `Binding`（值为 `42`）。
4. `.at(span)?`：命中了，不报错，透传 `&Binding`。
5. `.read_checked((&mut vm.engine, span))`：假设 `total` 未被标记弃用，不发警告，返回 `&Value::Int(42)`。
6. `.clone()` 得 `Value::Int(42)`；回到 `Expr::eval`，`.spanned(span)`（对 `Int` 无效）+ `trace_at`（若 IDE 正 hover 这个 span 则记录 42）。
7. 把第 3 步换成「`total` 从未定义」：`Scopes::get` 各层都没命中 → `unknown_variable("total")` → `.at(span)?` 把它变成带 span 的诊断，求值以错误结束。

**需要观察的现象**：

- 变量查找是「就近原则」：最顶层作用域优先，外层其次，标准库兜底。
- `.at(span)` 是「字符串错误 → 带 span 诊断」的关键转换点；没有它，`unknown_variable` 就没有位置信息。
- `.clone()` 让返回值脱离作用域的生命周期，成为独立的 `Value`。

**预期结果**：你能解释——「`Ident::eval` 用 `vm.scopes.get(&self)` 按顶层→外层→标准库的顺序查找绑定，找不到则由 `.at(span)` 把 `unknown_variable` 转成精确指向该标识符的诊断；找到后 `read_checked` 做弃用检查，最后 `clone` 出独立 `Value`，再由 `Expr::eval` 末尾的 `trace_at` 服务 IDE。」待本地验证：用 typst CLI 编译只含 `#total`（未定义）的文件，应看到 `unknown variable: total` 并高亮该标识符。

#### 4.4.5 小练习与答案

**练习 1**：`vm.scopes.get(&self)` 里 `&self` 的类型是 `&ast::Ident`，为什么能传给形参为 `&str` 的 `Scopes::get`？

> **答案**：因为 `ast::Ident` 实现了 `Deref<Target = str>`（在 typst-syntax 的 `ast.rs`），Rust 的解引用强制转换（deref coercion）会自动把 `&Ident` 转成 `&str`。所以这里实际查的是标识符的名字字符串。

**练习 2**：`Ident::eval` 末尾为什么必须 `.clone()`？

> **答案**：`read_checked` 返回的是 `&Value`——对 `Binding` 内部值的引用，其生命周期绑定在 `Scopes` 上。而 `eval` 要返回独立的 `Value`（`Output = Value`），不能把作用域里的引用泄露出去（作用域可能随后 `exit`、绑定可能被改写）。`.clone()` 复制一份独立值，切断与作用域的生命周期联系。

**练习 3**：为什么 `Ident::eval` 自己不调 `trace_at`，却能服务 IDE hover？

> **答案**：因为标识符总是作为 `ast::Expr::Ident` 经由 `ast::Expr::eval` 的总分发器求值，而 `Expr::eval` 在**所有**分支末尾统一调了 `vm.trace_at(span, &value)`（[code.rs:152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L152)）。子节点不必各自重复，统一出口一次搞定——这就是「统一善后」的好处。（极少数不经过该出口的节点，如 math 里的 `MathAccess`，才需要自己手动调，[u6-l2](./u6-l2-tracing-ide.md) 会展开。）

---

## 5. 综合实践

把本讲四个模块串起来：**手工追踪一段含字面量、标识符与一个整数错误的 Typst 代码，预测求值结果与诊断**。

**任务**：给定 markup 模式下的源码

```typst
#let magic = 0x2a
#let label = "answer"
#magic
#missing
#0x1g
```

请按步骤写出每个表达式的求值走向（哪个 `Eval` impl、是否用到 `Vm`、结果或错误）。

**操作步骤**：

1. **`#let magic = 0x2a`**：`Expr::LetBinding` → 经 `Expr::eval` 分发 → `LetBinding::eval`（[u3-l3](./u3-l3-let-destructure.md) 详讲）。关键是初值 `0x2a`：
   - `Expr::Int` → `Int::eval`（[code.rs:196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L196)）。
   - `Int::get()`：`strip_prefix("0x2a")` → `(Some(Hex), "2a")`；`i64::from_str_radix("2a", 16)` 成功 → `42`。注意此处的求值**不需要 `Vm`**。
   - 得 `Value::Int(42)`，`Expr::eval` 末尾 `trace_at`（假设非 inspect，不命中）。
   - `Vm::define("magic", 42)` 把它绑进 `scopes.top`（经 `bind`，[u1-l4](./u1-l1-eval-trait-vm.md)）。
2. **`#let label = "answer"`**：类似，初值是 `Str` → `Str::eval`（[code.rs:223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L223)）得 `Value::Str("answer")`，绑定 `label`。
3. **`#magic`**：`Expr::Ident` → `Ident::eval`（[code.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158)）：`scopes.get("magic")` 在 `top` 命中 → `read_checked` → `clone()` 得 `Value::Int(42)`；`Expr::eval` 末尾 `trace_at`。
4. **`#missing`**：`Expr::Ident` → `Ident::eval`：`scopes.get("missing")` 各层都不命中 → `unknown_variable("missing")` → `.at(span)?` 产生带 span 诊断。**这一条会成为错误**，使该表达式求值失败（但 markup 流式求值是否中断，取决于上层 `eval_markup` 的错误聚合策略，[u2-l4](./u2-l4-markup.md) 详讲）。
5. **`#0x1g`**：`Expr::Int` → `Int::eval`：`Int::get()` 失败得 `IntLiteralError::InvalidDigit(Hex, "1g")` → `int_literal_error` → `find_bad_digits(Hex, "1g")` 返回 `(1..2, ['g'])` → `+2` 偏移 → 高亮 `g`，信息「integer contains digits that are not valid for a hexadecimal number」，提示「the digit `g` is invalid」「hexadecimal numbers only allow digits 0-9, a-f, A-F」。

**需要观察的现象与预期结果**：

- 字面量（`0x2a`、`"answer"`）求值时 `Vm` 被忽略，是纯取值；标识符（`magic`、`missing`）求值时**真正用到了 `vm.scopes`**——这是字面量与标识符的本质区别。
- `0x2a`（合法）与 `0x1g`（非法）走的是同一条 `Int::eval`，但前者 `Int::get()` 返回 `Ok`、后者返回 `Err`，后者经 `int_literal_error` + `find_bad_digits` 产出精确高亮的诊断。
- 全程的 `trace_at` 都由 `Expr::eval` 末尾统一调用，叶子节点自身不必关心。

**待本地验证**：以上是对源码逻辑的推理。用 typst CLI 编译这段代码，预期看到两条诊断（`unknown variable: missing` 与 `0x1g` 的非法十六进制数字错误），且 `#magic` 正常输出 `42`。

## 6. 本讲小结

- **`ast::Expr::eval` 是表达式总分发器**：一个大 `match` 把每个变体派发到各自的 `Eval` impl；产出 `Content`/`Array`/`Dict` 的节点用 `.map(Value::...)` 适配成统一的 `Value`；末尾统一 `.spanned(span)` + `trace_at`，替几乎所有表达式满足 trace 调用约定。
- **`set`/`show` 在表达式上下文被 `forbidden` 拦截**：它们是依赖「后续 tail」的样式指令，只有在 `eval_code` 的语句流里才有意义（[code.rs:35-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L35-L57)），作为子表达式无值可产，故报错。
- **字面量求值极简**：`None`/`Auto`/`Bool`/`Float`/`Numeric`/`Str` 各一行 `Ok(Value::...)`，签名 `_: &mut Vm` 表示不用 vm——因为值完全由源码文本决定，不依赖运行时状态。
- **整数是唯一可能失败的字面量**：`Int::get()`（typst-syntax）返回 `Result`，区分溢出与非法数字；`int_literal_error`（typst-eval）把它们变成「信息 + 精确高亮（`SubRange`/`DiagSpan`）+ 修复提示」三要素齐全的诊断，其中 `find_bad_digits` 负责定位具体坏字符。
- **标识符是第一个用到 `Vm` 的叶子**：`Ident::eval` 用 `vm.scopes.get` 按「顶层→外层→标准库」逐层查找，`.at(span)` 贴位置，`read_checked` 做弃用检查，`.clone()` 取出独立值。

## 7. 下一步学习建议

本讲把「叶子节点」的求值讲透了。接下来建议：

- **[u2-l2 集合类型求值：数组与字典](./u2-l2-array-dict.md)**：继续在 `code.rs` 里看 `Array::eval` / `Dict::eval`，它们开始用到 `Vm`（要递归求值元素），并处理展开运算符（spread `..`）。
- **[u2-l3 代码块、内容块与作用域进出](./u2-l3-blocks-scopes.md)**：细讲 `eval_code` 的流式求值、`scopes.enter`/`exit` 如何划分词法作用域，以及 `set`/`show` 对 tail 的样式应用——本讲提到的「set/show 在语句流里合法」在那里完整展开。
- **[u2-l4 Markup 模式求值](./u2-l4-markup.md)**：本讲总分发器里那些 `.map(Value::Content)` 的标记节点（Text/Strong/Heading/...）在那里细讲。
- **[u3-l3 let 绑定与解构赋值](./u3-l3-let-destructure.md)**：本讲综合实践里用到 `let`，那里会讲 `LetBinding::eval` 如何把初值（本讲求值的字面量/标识符）绑定到名字。
- **[u6-l1 错误处理、诊断与提示体系](./u6-l1-diagnostics.md)**：本讲的 `int_literal_error` / `find_bad_digits` 是高质量诊断的典范，那里会系统汇总 `SourceResult`/`At`/`bail!`/`error!`/`Hint` 等诊断基础设施。
