# Math 模式求值：方程与符号

## 1. 本讲目标

本讲聚焦 `typst-eval/src/math.rs`，把前几讲建立的「`Eval` trait + `Vm` 虚拟机 + `ast::Expr` 总分发器」框架应用到 **Math（数学）模式** 上。学完本讲，你应当能够：

- 说清楚一行 `$ x^2 + 1 $` 是如何被求值成 `EquationElem`（并最终成为 `Content`）的。
- 区分 `Equation`、`Math`、`MathText` 三层节点的职责，理解「方程壳 → 表达式流 → 叶子文本」的分层。
- 解释数学标识符为什么用 `scopes.get_in_math` 而不是普通 `scopes.get`，以及它查的是 **数学作用域**（`base.math`）而非全局作用域（`base.global`）。
- 读懂 `MathFrac::eval` 里 `num_depar` / `denom_depar` 两个「去括号标记」的来源（`was_deparenthesized`）和意义。
- 理解 `ExprExt::eval_display` 这个辅助方法为什么是数学模式的「统一接口」，以及 `MathFieldAccess` 为什么复用 `code.rs` 的 `access_field`。

本讲承接 u2-l4（Markup 模式求值）。Markup 模式把标记流拼接成 `Content::sequence`，Math 模式做的事高度相似——只是叶子节点换成了数学元素，且标识符查找走专门的数学作用域。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：数学也是一种「模式」。** Typst 有三种语法模式：Code（`#`、`{}`）、Markup（正文）、Math（`$ ... $`）。解析器 `typst-syntax` 会为不同模式生成不同 AST 节点。本讲只关心 Math 模式产物：`Equation`、`Math`、`MathText`、`MathIdent`、`MathShorthand`、`MathDelimited`、`MathAttach`、`MathPrimes`、`MathFrac`、`MathRoot` 等。求值器的工作，就是把这些 AST 节点翻译成 `typst-library` 里定义的运行时元素（`EquationElem`、`FracElem`、`RootElem`、`AttachElem` 等）。

**直觉二：求值的输出是「排版用的内容」。** 数学求值的最终产物是 `Content`（排版内容），而不是 `Value`。和 Markup 一样，多个数学片段会被 `Content::sequence` 串起来。但注意：个别数学节点（如 `MathIdent`、`MathShorthand`、函数调用）求值出来的是 `Value`（符号、函数等），需要经过一道「显示」转换才能进入内容流。这道转换由本讲的关键辅助方法 `eval_display` 完成。

**直觉三：数学符号有自己的「字典」。** 在正文里写 `pi` 只是普通文本；在 `$ $` 里写 `pi` 却会变成希腊字母 π。这是因为数学模式有一套独立的符号作用域，`pi`、`alpha`、`sum`、`integral` 这些名字默认指向数学符号，而不是代码里的全局变量。这正是 `get_in_math` 存在的原因。

> 名词速查：`EquationElem`（方程元素，一个 `$ ... $` 的整体外壳）、`Content`（排版内容）、`Value`（运行时值）、`Symbol`（符号）、`NativeElement`（原生元素 trait，`.pack()` 把强类型元素打包成 `Content`）。这些都在 u1 / u2 前几讲解释过。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/math.rs` | 本讲主战场。为所有数学 AST 节点实现 `Eval`，把它们打包成数学元素；定义 `ExprExt::eval_display`。 |
| `src/code.rs` | 提供 `ast::Expr::eval` 总分发器（数学节点在此被派发），以及被 `MathFieldAccess` 复用的 `access_field` 函数。 |
| `typst-syntax/src/ast.rs` | 定义数学 AST 节点本身，包括 `Math::was_deparenthesized`、`MathText::get` / `MathTextKind`。 |
| `typst-library/src/foundations/scope.rs` | 定义 `Scopes::get`（普通查找）与 `Scopes::get_in_math`（数学查找），是理解标识符分派的关键对照。 |
| `src/call.rs` | 定义 `MathCall::eval`（数学里的函数调用）。本讲只引用其存在与手动 `trace_at` 的约定。 |

## 4. 核心概念与源码讲解

### 4.1 方程的顶层打包：Equation 与 Math

#### 4.1.1 概念说明

一个数学公式 `$ x^2 + 1 $` 在 AST 里被分成两层：

- **`Equation`（方程）**：最外层外壳，对应一对 `$ ... $`。它知道「这是不是一个块级公式」（行内 `$x$` 还是块级 `$ x $`，区别在于 `$` 内侧是否有空格）。
- **`Math`（数学内容）**：外壳里面的真正内容，是一串数学表达式的流（`x^2`、`+`、`1`）。

求值时，`Equation` 负责把 `Math` 求值出的 `Content` 包进 `EquationElem`，并打上「是否块级」的标记；`Math` 负责把内部表达式流逐个求值、拼接成一段 `Content`。这种「外壳负责定位/分类，内层负责内容」的分层，和 Markup 里 `Markup::eval` 委托给 `eval_markup` 的思路完全一致。

#### 4.1.2 核心流程

`Equation::eval` 的流程极简：

1. 先求值 `body()`（一个 `Math` 节点），得到内部 `Content`。
2. 读取 `self.block()` 判断是否块级。
3. 构造 `EquationElem::new(body).with_block(block)`，`.pack()` 成 `Content` 返回。

`Math::eval` 的流程：

1. 取出 `self.exprs()`（数学表达式迭代器）。
2. 对每个表达式调用 `expr.eval_display(vm)`（注意是 `eval_display`，不是普通 `eval`），把每个 `Value` 统一转成 `Content`。
3. 把所有 `Content` 收集进 `Vec`，最后用 `Content::sequence(...)` 串成一段。

#### 4.1.3 源码精读

`Equation::eval`——求值内部 body，包进方程外壳：

[Math::eval 的外壳 Equation::eval](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L12-L20)

> 这段先 `self.body().eval(vm)?` 求值出内部 `Content`，再用 `EquationElem::new(body).with_block(block).pack()` 打包。`block` 来自 `self.block()`，它由解析器根据 `$` 内侧空格判定（见 `typst-syntax/src/ast.rs` 中 `Equation::block`）。

`Math::eval`——把表达式流拼接成内容序列：

[Math::eval 流式拼接](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L22-L32)

> 注意 `.map(|expr| expr.eval_display(vm))`：这里**没有**直接调 `expr.eval(vm)`，而是调 `eval_display`。原因是数学表达式流里既有产出 `Content` 的节点（如 `MathText`），也有产出 `Value` 的节点（如 `MathIdent`、函数调用）。`eval_display` 统一把它们转成 `Content`，这样 `Content::sequence` 才能拼接。`eval_display` 的实现见 4.5。

要理解 `Equation` 与 `Math` 在总分发器里的位置，看 `ast::Expr::eval` 的 match 分支：

[ast::Expr::eval 中数学节点的派发](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L103-L115)

> 可以看到一个清晰规律：产出 `Content` 的数学节点（`Equation`/`Math`/`MathText`/`MathDelimited`/`MathAttach`/`MathPrimes`/`MathFrac`/`MathRoot`/`MathAlignPoint`）都带 `.map(Value::Content)`，把 `Content` 适配成统一的 `Value`；而产出 `Value` 的节点（`MathIdent`/`MathFieldAccess`/`MathShorthand`/`MathCall`）则直接 `v.eval(vm)`。这与 u2-l1 讲过的「`Eval` trait 用关联类型 `Output` 声明各自输出，总分发器用 `.map` 适配」完全吻合。注意 `MathAlignPoint`（对齐点 `&`）也被视为 `Content`。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一行公式 `$ x^2 $` 的求值调用链，理解三层节点的协作。

**操作步骤（源码阅读型）**：

1. 在 `src/math.rs` 打开 `Equation::eval`（L12–L20），确认它调用 `self.body().eval(vm)`——这里的 `body()` 返回 `Math` 节点。
2. 跳到 `Math::eval`（L22–L32），确认它对 `self.exprs()` 里的每个表达式调 `eval_display`。对于 `$ x^2 $`，表达式流大致是 `x`（MathIdent）、`^2`（MathAttach）、可能的空格。
3. 思考：`x` 作为 `MathIdent` 求值得 `Value::Symbol(...)`，它是怎么变成能进入 `Content::sequence` 的 `Content` 的？答案是 `eval_display` 里的 `.display()`（见 4.5）。

**需要观察的现象 / 预期结果**：你能用一句话描述「`Equation`（外壳 + 块级标记）→ `Math`（流式拼接）→ 各叶子节点」三层各自的职责。块级判定结果：`$x$` 是行内（`block == false`），`$ x $`（两侧有空格）是块级（`block == true`）——这一点可在 `typst-syntax/src/ast.rs` 的 `Equation::block` 中验证，逻辑是「第 2 个子节点和倒数第 2 个子节点都是空格」。

> 本实践为源码阅读型，不涉及运行命令；块级 vs 行内的实际排版效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Math::eval` 用 `eval_display` 而不是直接 `expr.eval(vm)`？
**参考答案**：因为表达式流里既有产出 `Content` 的节点，也有产出 `Value`（如 `Value::Symbol`）的节点。`Content::sequence` 只能拼接 `Content`，所以需要一个统一把 `Value` 转成 `Content` 的入口，这正是 `eval_display`。

**练习 2**：`Equation::eval` 里 `self.block()` 的值由什么决定？
**参考答案**：由解析器根据 `$` 与内容之间是否有空格决定——`$ x $`（两侧有空格）为块级 `true`，`$x$` 为行内 `false`。求值器只是把这个布尔值透传给 `EquationElem::with_block`。

---

### 4.2 数学标识符与作用域：MathIdent 与 get_in_math

#### 4.2.1 概念说明

这是本讲最重要的「为什么」。在正文里，`pi` 只是一段文本；在 `$ pi $` 里，`pi` 变成了符号 π。这背后是因为数学模式有一套**独立的作用域查找路径**。

回顾 u2-l1：普通标识符 `Ident` 用 `vm.scopes.get(&self)` 查找，按「顶层 → 外层用户作用域 → 标准库全局作用域（`base.global`）」逐层找。数学标识符 `MathIdent` 用的是 `vm.scopes.get_in_math(&self)`，差别只在**最后一层兜底**：它查的是 **数学作用域 `base.math`**，而不是全局作用域 `base.global`。

这个差别解释了为什么 `$ pi $` 是 π（命中数学作用域里的符号），而 `$ #pi $`（用 `#` 逃逸回代码模式）才会去查代码里的 `pi`。

#### 4.2.2 核心流程

`MathIdent::eval` 流程：

1. `vm.scopes.get_in_math(&self)` 在「顶层 → 外层 → 数学作用域」中查找绑定，返回 `&Binding`；未命中则返回带提示的错误。
2. `.at(span)` 把字符串错误贴上源码位置。
3. `.read_checked((&mut vm.engine, span))` 做弃用（deprecated）检查。
4. `.clone()` 返回独立的 `Value`。

与普通 `Ident::eval` 几乎逐行对应，唯一区别是第 1 步调用 `get_in_math` 而非 `get`。

#### 4.2.3 源码精读

`MathIdent::eval`——数学标识符求值：

[MathIdent::eval 走 get_in_math](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L45-L57)

> 与 u2-l1 的 `Ident::eval` 对照：两者都是「`scopes.get_xxx` → `.at(span)` → `read_checked` → `clone()`」。这里换成 `get_in_math`。

对照 `Scopes::get`（普通查找）与 `Scopes::get_in_math`（数学查找），看兜底层差异：

[普通查找 get 兜底 base.global](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)

> `get` 的兜底是 `base.global.scope().get(var)`——标准库**全局**作用域。找不到就报 `unknown_variable(var)`。

[数学查找 get_in_math 兜底 base.math](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L76-L94)

> `get_in_math` 的兜底是 `base.math.scope().get(var)`——**数学**作用域。注意它还调用 `unknown_variable_math(var, ...)` 生成错误，第二个参数 `base.global.scope().get(var).is_some()` 用来判断「这个名字在全局作用域里其实存在」，从而给出「你是不是想用 `#` 逃逸」之类的提示。

两者的**前半段完全相同**（都查 `self.top` + `self.scopes`，即用户自定义的作用域），区别只在兜底查哪个标准库作用域。这意味着：用户在数学模式里 `#let pi = ...` 绑定的 `pi`（进入用户作用域）会优先于数学符号命中；只有用户没定义时，才回落到数学符号字典。

#### 4.2.4 代码实践

**实践目标**：通过对照两个查找函数，理解「为什么数学符号不污染代码、代码变量也不污染数学」。

**操作步骤（源码阅读型）**：

1. 打开 `scope.rs` 的 `get`（L46–L59）和 `get_in_math`（L76–L94），逐行对比。圈出唯一不同的那一行（兜底的 `base.global` vs `base.math`）。
2. 思考以下两段 Typst 代码的求值差异：
   - `$ pi $`：`pi` 是 `MathIdent` → `get_in_math` → 命中 `base.math` 的符号 π。
   - `#pi`：`pi` 是（代码模式的）`Ident` → `get` → 查 `base.global`；若代码里没定义 `pi`，会报错。
3. 再思考 `$ #pi $`：`#` 把 `pi` 切回代码模式，所以走的是普通 `Ident` 查找路径。

**需要观察的现象 / 预期结果**：你能解释「同一个名字 `pi`，在 `$ $` 内外查的是两套不同字典」。预期：`$ pi $` 渲染为 π；`#pi`（未定义时）报「unknown variable」。

> 排版效果待本地验证；作用域分派逻辑可在源码中确认。

#### 4.2.5 小练习与答案

**练习 1**：如果用户写了 `#let alpha = "hello"`，那么 `$ alpha $` 会渲染成什么？
**参考答案**：会渲染成用户绑定的值 `"hello"` 经 `display` 后的内容，而不是希腊字母 α。因为 `get_in_math` 先查用户作用域（`self.top` / `self.scopes`），用户绑定优先于数学符号字典。

**练习 2**：`get` 和 `get_in_math` 在查找用户自定义变量时，行为有区别吗？
**参考答案**：没有区别。两者前半段完全一样——都先查 `self.top` 再查 `self.scopes`（用户作用域栈）。区别仅在用户作用域未命中后的兜底：`get` 查全局作用域 `base.global`，`get_in_math` 查数学作用域 `base.math`。

---

### 4.3 数学结构节点：MathDelimited、MathAttach、MathPrimes、MathRoot

#### 4.3.1 概念说明

除标识符和文本外，数学模式还有一类「结构节点」，它们对应数学排版里的常见结构：

- **`MathDelimited`**：定界分组，如 `( x + 1 )`、`[ a, b ]`。开/闭符号可以是任意数学表达式，求值后包进 `LrElem`（左右定界元素，自动缩放括号）。
- **`MathAttach`**：上下标附着，如 `x^2`、`a_i`、`f'(x)`。把底、上标、下标、撇号（primes）附到一个 `AttachElem` 上。
- **`MathPrimes`**：撇号序列，如 `''`。打包成 `PrimesElem`，记录撇号个数。
- **`MathRoot`**：根号，如 `root(3, x)`。打包成 `RootElem`，带可选的指数。

这些节点的求值套路高度一致：**读出各组成部分 → 构造对应的强类型元素 → `.pack()` 成 `Content`**。其中凡是要放进元素字段的「子表达式」，几乎都走 `eval_display`（转成 `Content`）。

#### 4.3.2 核心流程

`MathDelimited::eval`：求值开符号 `eval_display` → 求值 body（`Math`，直接 `eval` 得 `Content`）→ 求值闭符号 `eval_display` → 用 `LrElem::new(open + body + close)` 拼成一段再打包。

`MathAttach::eval`：

1. 底（base）：`self.base().eval_display(vm)`。
2. 上标（top）：若存在，`self.top().eval_display(vm)`。
3. 撇号（primes）：若存在，`self.primes().eval(vm)`（注意是普通 `eval`，得 `Content`），并固定设到右上角 `tr`（top-right），用 scripts 风格而非 limits 风格。
4. 下标（bottom）：若存在，`self.bottom().eval_display(vm)`。

`MathPrimes::eval`：`PrimesElem::new(self.count()).pack()`，只记录撇号数量。

`MathRoot::eval`：可选指数（`self.index()`）格式化成 `TextElem`（与数字文本节点保持一致），被开方数 `self.radicand().eval_display(vm)`，包进 `RootElem`。

#### 4.3.3 源码精读

`MathDelimited::eval`——定界分组包进 `LrElem`：

[MathDelimited::eval](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L100-L109)

> 开/闭符号用 `eval_display`（它们是任意 `Expr`，可能是标识符、函数调用等，需转成 `Content`）；body 是 `Math`，直接 `eval` 已是 `Content`。最后 `open + body + close` 用 `Content` 的 `+` 拼接，交给 `LrElem` 做自动缩放。

`MathAttach::eval`——上下标与撇号附着：

[MathAttach::eval](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L111-L134)

> 关键细节：撇号（`primes`）被显式设到 `elem.tr`（top-right），并注释说明「撇号总是用 scripts 风格（贴在右上角），而不是 limits 风格（堆在正上方）」。这是为什么 `f'(x)` 的撇号紧贴 `f` 右上、而 `\sum` 类算子的上下限却可以堆叠的求值层根因之一。注意 `primes` 走的是普通 `eval`（`MathPrimes` 产出 `Content`），其余子表达式走 `eval_display`。

`MathPrimes::eval`——撇号数量：

[MathPrimes::eval](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L136-L142)

> 只把撇号个数 `self.count()` 传给 `PrimesElem`，自包含、签名 `_: &mut Vm`。

`MathRoot::eval`——根号与指数：

[MathRoot::eval](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L165-L174)

> 指数用 `TextElem::packed(eco_format!("{i}"))` 而非 `Number` 元素——注释指出这是为了和 `MathText::get` 里 `MathTextKind::Number` 的处理保持一致（都用 `TextElem` 表示数字）。被开方数走 `eval_display`。

#### 4.3.4 代码实践

**实践目标**：理解结构节点「读组成 → 构造元素 → pack」的统一套路，并注意 `eval_display` 与普通 `eval` 的混用。

**操作步骤（源码阅读型）**：

1. 在 `MathAttach::eval` 中数一下有几处 `eval_display`、几处普通 `eval`。预期：base/top/bottom 三处用 `eval_display`，primes 一处用普通 `eval`。
2. 思考：为什么 base 必须用 `eval_display`？因为 `self.base()` 返回的是 `ast::Expr`（可能是 `MathIdent` 产出 `Value::Symbol`），`AttachElem::new(base)` 需要的是 `Content`，所以必须 `.display()`。
3. 对照 `MathRoot::eval`，确认指数为什么用 `TextElem` 而不是直接传整数——因为 `RootElem` 的 `index` 字段是 `Content` 类型，需要把数字渲染成文本内容。

**需要观察的现象 / 预期结果**：你能总结出一条规则——「凡元素字段要求 `Content`、而子表达式可能产出任意 `Value` 的，都用 `eval_display`；子表达式本身就直接产出 `Content` 的（如 `MathPrimes`、`Math` body），用普通 `eval`」。

> 排版效果（撇号位置、根号指数）待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`MathDelimited::eval` 里，为什么 open/body/close 三者中，只有 body 用普通 `eval`，open 和 close 用 `eval_display`？
**参考答案**：`body()` 返回 `Math` 节点，其 `Eval::Output = Content`，直接 `eval` 即可；`open()` / `close()` 返回的是任意 `ast::Expr`（可能是产出 `Value` 的标识符或函数调用），需要 `eval_display` 把 `Value` 统一转成 `Content` 才能与 body 拼接。

**练习 2**：`MathAttach::eval` 为什么把 primes 固定设到 `elem.tr`（右上角）？
**参考答案**：撇号在数学排版中约定俗成地贴在底的右上角（scripts 风格），而不像 `\sum` 的上下限那样可以堆叠在正上下方（limits 风格）。求值层通过把 primes 写入 `tr` 字段来强制这一约定。

---

### 4.4 分式与去括号标记：MathFrac 与 was_deparenthesized

#### 4.4.1 概念说明

分式 `x / y` 求值成 `FracElem`（分子 + 分母）。本模块的精华在于两个布尔标记：`num_depar` / `denom_depar`（numerator/denominator deparenthesized，分子/分母「是否曾被括号包裹」）。

考虑两种写法：

- `1 / x` —— 分母 `x` 没有括号。
- `1 / (x + 1)` —— 分母 `(x + 1)` 是用户**显式加了括号**的数学分组。

解析器会把 `(x + 1)` 解析成一个 `Math` 节点，并且这个 `Math` 节点「记得」自己原本被括号包裹。`Math::was_deparenthesized()` 就是问这个问题：「你本来是不是一对括号 `(...)`？」

`MathFrac::eval` 在求值分子分母后，额外读取这两个「去括号标记」，原样传给 `FracElem`。排版层（`typst-library` / `typst-math`）据此决定渲染策略——例如对被括号包裹的分母，可能采用不同的尺寸或缩放处理。求值层的职责只是**忠实地把这个语法事实传递下去**。

#### 4.4.2 核心流程

`MathFrac::eval`：

1. 求分子：`num_expr = self.num()`、`num = num_expr.eval_display(vm)`。
2. 求分母：`denom_expr = self.denom()`、`denom = denom_expr.eval_display(vm)`。
3. 判定分子去括号标记：`num_depar = matches!(num_expr, ast::Expr::Math(math) if math.was_deparenthesized())`。
4. 判定分母去括号标记：同理得 `denom_depar`。
5. 构造 `FracElem::new(num, denom).with_num_deparenthesized(num_depar).with_denom_deparenthesized(denom_depar).pack()`。

`was_deparenthesized` 的判定（在 `typst-syntax`）：检查这个 `Math` 节点的第一个子节点是不是 `LeftParen`、最后一个子节点是不是 `RightParen`。

#### 4.4.3 源码精读

`MathFrac::eval`——求值分式并传递去括号标记：

[MathFrac::eval 读取 was_deparenthesized](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L144-L163)

> `num_depar` / `denom_depar` 用 `matches!` 宏判定：仅当对应表达式是一个 `ast::Expr::Math` 且 `math.was_deparenthesized()` 为真时才为 `true`。这两个布尔值通过 `with_num_deparenthesized` / `with_denom_deparenthesized` 装进 `FracElem`。注意求值器本身**不解释**这两个标记的排版含义，只负责采集与传递。

`Math::was_deparenthesized`——解析器侧的「曾否被括号包裹」判定：

[Math::was_deparenthesized 检查首尾括号](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L894-L901)

> 取子节点迭代器，若第一个是 `LeftParen` 且最后一个是 `RightParen`，返回 `true`。这是一种「语法事实回溯」——括号在解析成 `Math` 节点时已被「吸收」，但通过检查首尾 token 种类仍能还原出「它原本是带括号的」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：读懂 `num_depar` / `denom_depar` 两个标记的采集逻辑，并解释它们传递给 `FracElem` 的意义；同时说明 `MathFieldAccess` 复用 `access_field` 的原因（见 4.5）。

**操作步骤（源码阅读型）**：

1. 打开 `MathFrac::eval`（math.rs L144–L163）。
2. 找到这两行：
   ```rust
   let num_depar = matches!(num_expr, ast::Expr::Math(math) if math.was_deparenthesized());
   let denom_depar = matches!(denom_expr, ast::Expr::Math(math) if math.was_deparenthesized());
   ```
3. 解释 `matches!` 在做什么：它同时满足两个条件才算 `true`——(a) 该表达式是一个 `Math` 节点；(b) 这个 `Math` 节点 `was_deparenthesized()`（首尾是括号）。
4. 追踪 `was_deparenthesized`（ast.rs L894–L901），确认它只看首尾子节点的 `SyntaxKind`。

**解释 `num_depar` / `denom_depar` 的意义**：这两个标记回答的是「用户在源码里有没有给分子/分母显式加括号」。例如 `(x + 1) / 2` 中分子会被标记 `num_depar = true`，`1 / (x + 1)` 中分母会被标记 `denom_depar = true`，而 `x / y` 两者都是 `false`。求值器把这个信息原样传给 `FracElem`，由排版层决定如何渲染（比如对带括号的分母保留恰当的分组视觉）。这正是「解析层捕获语法事实 → 求值层透传 → 排版层消费」的典型三层协作。

**预期结果**：你能说出——`num_depar` / `denom_depar` 不改变求值出的 `Content` 本身，它们是附加在 `FracElem` 上的元信息，供下游排版使用。

> 具体排版层如何利用这两个标记（例如是否影响括号尺寸/分数大小）待本地验证；求值层行为可在源码中确认。

#### 4.4.5 小练习与答案

**练习 1**：写出 `x / (y + z)` 求值时 `num_depar` 和 `denom_depar` 各是什么值。
**参考答案**：`num_depar = false`（分子 `x` 不是带括号的 `Math` 节点），`denom_depar = true`（分母 `(y + z)` 是一个首尾为括号的 `Math` 节点）。

**练习 2**：如果一个分子表达式是 `#f(x)`（代码模式的函数调用，不是 `Math` 节点），`num_depar` 会是 `true` 吗？
**参考答案**：不会。`matches!(num_expr, ast::Expr::Math(math) if ...)` 要求表达式必须是 `ast::Expr::Math` 变体；`#f(x)` 不是 `Math` 节点，所以直接为 `false`，无论它内容如何。

---

### 4.5 统一显示接口：ExprExt::eval_display 与字段访问复用

#### 4.5.1 概念说明

前几个模块反复出现 `eval_display`。它定义在 math.rs 末尾的一个私有扩展 trait `ExprExt` 上，是数学模式的「统一显示接口」。

为什么需要它？因为 `ast::Expr` 是一个枚举，其 `Eval::Output = Value`（见 u2-l1 的总分发器）。但数学元素的字段（如 `AttachElem` 的 base、`FracElem` 的分子分母）需要的是 `Content`。`eval_display` 就是这道「`Value` → `Content`」的桥梁：

\[
\texttt{eval\_display}(\text{expr}) = \texttt{expr.eval(vm)?.display().spanned(expr.span())}
\]

即：先按表达式求值得 `Value`，再 `.display()` 转成 `Content`（`Value::Content` 原样透传，`Value::Symbol` 转成可显示内容，等等），最后贴上源码 span。这样无论子表达式产出什么 `Value`，都能统一喂给需要 `Content` 的数学元素。

本模块还涉及两个「复用」设计：

- **`MathFieldAccess` 复用 `access_field`**：数学里的字段访问 `a.b` 与代码里的 `a.b`，其字段读取语义（包括「settable 字段在 context 下读取」的兜底）完全一样。所以数学版只求值 target（注意 target 是 `MathIdent`，走 `get_in_math`），然后把字段读取整体委托给 `code.rs` 的 `access_field`，避免重复实现。
- **`MathAccess::eval` 手动调 `trace_at`**：数学访问节点不经过 `ast::Expr::eval`（后者末尾有统一的 `trace_at`），所以要自己补上这一调用，以满足 IDE 追踪约定（见 u6-l2）。

#### 4.5.2 核心流程

`ExprExt::eval_display`：`self.eval(vm)?.display().spanned(self.span())`。

`MathFieldAccess::eval`：

1. 求值 target：`self.target().eval(vm)?`（target 是 `MathIdent`，走 `get_in_math`）。
2. 取字段名 `self.field()`。
3. 调 `crate::code::access_field(vm, target, field.as_str(), field.span())`——与代码模式 `FieldAccess::eval` 走同一个函数。

`MathAccess::eval`（数学访问的总入口，可能是 `MathIdent` 或 `MathFieldAccess`）：

1. 匹配并求值，得 `Value`。
2. 手动 `vm.trace_at(self.span(), &value)`（因为没走 `ast::Expr::eval`）。

#### 4.5.3 源码精读

`ExprExt` trait 及其实现——数学模式的统一显示接口：

[ExprExt::eval_display：Value → Content](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L176-L184)

> 这就是 `eval_display` 的全部实现：`self.eval(vm)?` 得 `Value`，`.display()` 转 `Content`，`.spanned(self.span())` 贴位置。它让 `Math::eval`、`MathDelimited::eval`、`MathAttach::eval`、`MathFrac::eval`、`MathRoot::eval` 等处都能用同一句话处理「任意子表达式」。

`MathFieldAccess::eval`——复用代码模式的 `access_field`：

[MathFieldAccess::eval 委托 access_field](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L59-L67)

> 注意它调用的是 `crate::code::access_field(...)`——直接复用 `code.rs` 里的同一个函数。

被复用的 `access_field`（在 code.rs，`FieldAccess::eval` 也调它）：

[access_field：字段读取 + settable 兜底](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L363-L385)

> `access_field` 先试 `target.field(field, ...)`；若失败且目标是元素函数、且该字段是 settable 的，则在 `vm.context` 的样式里读取（这是 `block.stroke` 这类「带 context 读 settable 字段」的兜底逻辑）。`MathFieldAccess` 复用它，意味着数学模式下的字段访问**自动获得完全相同的兜底能力**，无需重复实现。

`MathAccess::eval`——手动补 `trace_at`：

[MathAccess::eval 手动 trace_at](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/math.rs#L69-L82)

> 注释明确写道：「我们需要为这个值调用 `trace_at`，因为我们没有经由 `ast::Expr::eval()` 求值。」这是 trace 调用约定（每个产生值的表达式都要 `trace_at`）在数学访问节点上的手工补丁。同样的手工补丁也出现在 `call.rs` 的 `MathCall::eval` / `eval_math_call` 中。

#### 4.5.4 代码实践

**实践目标**：回答本讲主实践的第二个问题——为什么 `MathFieldAccess` 要复用 `code.rs` 的 `access_field`，而不是自己实现字段访问。

**操作步骤（源码阅读型）**：

1. 打开 `MathFieldAccess::eval`（math.rs L59–L67），看到它只做两件事：求值 target、调 `access_field`。
2. 打开被复用的 `access_field`（code.rs L363–L385），读懂它的兜底逻辑（settable 字段在 context 下读取）。
3. 思考：如果 `MathFieldAccess` 自己实现字段访问，会漏掉什么？会漏掉这段 settable 字段兜底，导致数学模式下的 `elem.field` 读取行为与代码模式不一致。

**解释「为什么复用」**：字段访问的**读取语义是模式无关的**——无论在代码还是数学里，`target.field` 都要：先查目标值的字段，失败时对元素函数的 settable 字段做 context 兜底。两套模式下唯一不同的是 **target 怎么求值**（代码用 `scopes.get`，数学用 `scopes.get_in_math`）。既然 `MathFieldAccess::eval` 已经在第一步用数学路径求出了 target（一个 `Value`），剩下的「在这个 `Value` 上读字段」就完全是模式无关的逻辑，自然应当复用同一个 `access_field`。复用的好处：(1) 避免重复实现兜底逻辑；(2) 保证两种模式下字段访问行为一致；(3) settable 字段的 context 兜底自动对数学模式生效。这是典型的 DRY（Don't Repeat Yourself）设计。

**需要观察的现象 / 预期结果**：你能用一句话总结——「模式差异只体现在 target 查找（`get` vs `get_in_math`），字段读取本身模式无关，故复用 `access_field`」。

> 本实践为源码阅读型，无需运行命令。

#### 4.5.5 小练习与答案

**练习 1**：`eval_display` 和直接 `eval` 的返回类型分别是什么？
**参考答案**：`eval` 返回 `SourceResult<Value>`（因为 `ast::Expr::Eval::Output = Value`）；`eval_display` 返回 `SourceResult<Content>`，它在 `eval` 之后多了 `.display().spanned(span)` 两步转换。

**练习 2**：为什么 `MathAccess::eval` 要手动调用 `vm.trace_at`？
**参考答案**：因为 `MathAccess::eval` 是直接被调用求值的（例如作为函数调用的 callee），没有经过 `ast::Expr::eval`。而 trace 调用约定要求「每个产生值的表达式都要 `trace_at`」（`ast::Expr::eval` 末尾有统一的 `trace_at`）。既然绕过了那条统一路径，就必须在这里手工补上，否则 IDE 的 hover/追踪就拿不到这个表达式的值。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**画一张「`$ f'(x) = (x^2 + 1) / x $` 求值调用树」**，把本讲所有模块串起来。

**任务步骤**：

1. 先拆解这行公式对应的 AST 嵌套（顶层是 `Equation`，内部 `Math` 流里有：`MathIdent(f)`、`MathAttach`（底 `f` + primes `'`）、`=`（`MathText`）、`MathFrac`（分子是 `MathDelimited` 包着 `x^2 + 1`，分母是 `MathIdent(x)`））。
2. 对树中每个节点，标注它走哪个 `Eval` 实现、产出 `Value` 还是 `Content`、是否经过 `eval_display`。预期：
   - `Equation` → `EquationElem`（`Content`）。
   - `MathIdent(f)` → `get_in_math` → `Value::Symbol`/`Value::Func`，再经 `eval_display` 转 `Content`。
   - `MathAttach` 的 primes → `MathPrimes` → `PrimesElem`，设到 `tr`。
   - `MathFrac` 的分子 `MathDelimited` → `LrElem`，并因 `(x^2 + 1)` 被 `was_deparenthesized` 判定，`num_depar = true`；分母 `x` → `denom_depar = false`。
   - `MathFrac` → `FracElem`（带两个去括号标记）。
3. 在图中用箭头标出「`Value` → `eval_display` → `Content`」的转换发生位置（凡是数学元素字段需要 `Content`、而子表达式产出 `Value` 的地方）。
4. 最后回答：这张图里出现了几次 `get_in_math`、几次 `access_field`、几次 `eval_display`？分别在哪条路径上？

**预期产出**：一张手绘或文本描述的调用树，能清楚说明 (a) 三层节点（`Equation`/`Math`/叶子）的分层；(b) `get_in_math` 与 `access_field` 各自负责的模式差异点；(c) `eval_display` 作为「`Value`→`Content` 桥梁」出现的位置；(d) `was_deparenthesized` 标记从解析层经求值层透传到 `FracElem` 的路径。

> 排版渲染效果待本地验证；调用树结构与求值路径可在源码中完整确认。

## 6. 本讲小结

- **三层分层**：`Equation`（外壳 + 块级标记）→ `Math`（表达式流拼接成 `Content::sequence`）→ 各叶子/结构节点（打包成数学元素）。
- **`eval_display` 是数学模式的统一接口**：把任意 `ast::Expr` 求值得 `Value`，再 `.display().spanned()` 转成 `Content`，解决「元素字段要 `Content`、子表达式产出 `Value`」的类型落差。
- **`get_in_math` vs `get`**：两者只在用户作用域未命中后的兜底层不同——数学查 `base.math`（数学符号字典），普通查 `base.global`（标准库全局）。这解释了 `$ pi $` 是 π、`#pi` 走代码路径。
- **结构节点套路一致**：`MathDelimited`/`MathAttach`/`MathPrimes`/`MathRoot` 都是「读组成 → 构造强类型元素 → `.pack()`」，撇号固定设到右上角（scripts 风格）。
- **`was_deparenthesized` 透传**：`MathFrac::eval` 读取分子分母「是否曾被括号包裹」，作为 `num_depar`/`denom_depar` 透传给 `FracElem`，供排版层消费——求值层只采集不解释。
- **复用与约定**：`MathFieldAccess` 复用 `code.rs` 的 `access_field`（字段读取模式无关），`MathAccess`/`MathCall` 因绕过 `ast::Expr::eval` 而手工补 `trace_at`。

## 7. 下一步学习建议

本讲把 Math 模式的求值讲完了。接下来建议：

1. **进入第 3 单元（控制流）**：`src/flow.rs` 的 `Conditional`/`WhileLoop`/`ForLoop` 与 `FlowEvent`。数学模式里也能写 `#for`、`#if`（用 `#` 逃逸），理解控制流有助于读懂更复杂的数学宏。
2. **阅读 `src/call.rs` 的 `MathCall::eval` / `eval_math_call`**：本讲多次提到数学函数调用（如 `root(3, x)`、`abs(x)`），它的 callee 解析、`trace_at` 手工补丁、与 `FieldCallee` 的分派，是数学与代码调用交汇的关键，对应讲义 u4-l1 / u4-l2。
3. **关注 `eval_display` 的下游 `.display()`**：想理解 `Value` 如何具体转成 `Content`（尤其是 `Value::Symbol`、`Value::Func`），可去 `typst-library` 里读 `Value::display` 的实现。
4. **回到排版层验证**：找一个本地 Typst 环境，渲染 `$ (x+1)/x $` 与 `$ x+1 / x $`，观察 `was_deparenthesized` 标记对 `FracElem` 排版的实际影响，把本讲的「求值层透传」与「排版层消费」两端连起来。
