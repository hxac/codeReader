# Markup 模式求值：从文本到内容元素

## 1. 本讲目标

上一讲（u2-l3）我们读完了 `code.rs` 里的 `eval_code`，理解了代码块/内容块如何用 `scopes.enter/exit` 划分作用域、`ops::join` 如何把一串表达式累加成单个 `Value`、以及 `set/show` 如何作为「样式作用域」作用于后续 tail。本讲我们从**代码模式（Code）**切到**标记模式（Markup）**，读完整个 [`src/markup.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs)。

学完本讲你应当能够：

1. 说清 `eval_markup` 如何把一段 Markup 标记流**流式拼接**成一个 `Content::sequence`，并与 `eval_code` 做横向对比。
2. 看懂 `Text`、`Space`、`Strong`、`Emph`、`Heading`、`List`、`Enum`、`Term`、`Raw`、`Link` 等标记节点是如何被打包（`pack`）成 `typst-library` 里的 `NativeElement` 内容元素的。
3. 理解 Typst 里独特的 **Label 回溯附加**机制：`<label>` 并不是内容本身，而是被「挂」到它前面最后一个可标记元素上，并知道两种相关警告何时触发。
4. 理解 `@ref` 引用如何被求值成 `RefElem`。

## 2. 前置知识

本讲默认你已经读过 u1-l4（`Eval` trait 与 `Vm`）和 u2-l1/u2-l3（字面量求值、代码块与作用域）。下面用通俗语言补齐本讲要用到的几个概念。

### 2.1 什么是 Markup 模式

Typst 的源文件有三种语法模式（`SyntaxMode`）：

| 模式 | 触发方式 | 典型内容 |
|---|---|---|
| **Markup**（标记） | 文档正文，`.typ` 文件默认就是 | 段落、标题、列表、强调 |
| **Code**（代码） | `#{ ... }` 代码块内 | `let`、`if`、函数调用 |
| **Math**（数学） | `$ ... $` 内 | 方程、符号 |

Markup 模式就是「写文章」的模式：你直接敲文字，用 `*粗体*`、`= 标题`、`- 列表` 这样的标记；需要插入代码逻辑时用 `#` 开头注入（如 `#let`、`#f()`）。本讲关心的就是**这些标记被求值后变成了什么**。

### 2.2 Content、Value、Element 三者关系（回顾）

- `Value`：Typst 运行时的「值」总类型，包括 `Value::Int`、`Value::Str`、`Value::Content` 等。
- `Content`：可被排版输出的「内容」，是文档的基本积木。`Value::Content` 就是包裹了 `Content` 的值。
- `NativeElement`：具体的强类型元素（如 `TextElem`、`HeadingElem`）。每个元素都实现 `pack(self) -> Content`，把强类型自己**类型擦除**成通用的 `Content`。详见 [element.rs:241-243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L241-L243)。

所以「求值一个标记节点」本质上就是：读出语法节点携带的数据 → 构造对应的强类型元素 → `pack()` 成 `Content`。

### 2.3 两个会反复出现的设计模式（来自 u2-l3）

1. **flow 保存-恢复**：进入一个块/循环前 `let flow = vm.flow.take()`，离开时若原本有事件则恢复。保证 `break/continue/return` 不会跨作用域泄漏。
2. **set/show 作用于 tail**：求出 `Styles`/`Recipe` 后，**递归求值其后的剩余表达式**（tail），用 `styled_with_map/recipe` 包裹。

本讲你会看到 `eval_markup` 把这两个模式几乎原样复刻了一遍。

### 2.4 `Content::sequence` vs `ops::join`

这是本讲和 u2-l3 最关键的对比点：

- `eval_code`（代码模式）用 `Value::None` 作单位元，靠 `ops::join` 把多个 `Value` 合并成**一个 `Value`**。
- `eval_markup`（标记模式）用 `Vec<Content>` 收集所有片段，最后用 `Content::sequence(seq)` 拼成**一个 `Content`**。

`Content::sequence` 的行为很直观：空集合→空内容；单元素→直接返回该元素；多元素→包成 `SequenceElem`。见 [content/mod.rs:239-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L239-L248)。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/markup.rs` | **本讲主角**。`Markup::eval` + `eval_markup` 流式求值，以及所有标记节点（Text/Space/Strong/Emph/Heading/List/Enum/Term/Raw/Link/Label/Ref）的 `Eval` 实现。 |
| `src/code.rs` | `Expr::eval` 总分发器（标记叶子在这里被 `.map(Value::Content)`），以及 `eval_code` 用于和 `eval_markup` 横向对比。 |
| `src/lib.rs` | `Eval` trait 定义。 |
| `crates/typst-library/.../content/mod.rs` | `Content::sequence` / `labelled` / `label` / `can` 等运行时方法。 |
| `crates/typst-library/.../label.rs` | `Unlabellable` 标记 trait（决定哪些元素不能被贴标签）。 |
| `crates/typst-library/.../value.rs` | `Value::display`（把任意值转成可显示的 `Content`）。 |

> 提示：本讲引用了少量 `typst-library` 的源码作为支撑理解，它们用同一 HEAD 的完整 GitHub 链接给出。核心讲解仍以 `typst-eval/src/markup.rs` 为主。

## 4. 核心概念与源码讲解

### 4.1 `Markup::eval` 与 `eval_markup`：流式拼接 Content 序列

#### 4.1.1 概念说明

一个 `ast::Markup` 节点本质上是一个**有序的标记表达式流**：一段段文字、空格、强调、标题，以及用 `#` 注入的 `#let`、`#f()`、`#set`、`#show` 等。`Markup::eval` 本身极简，它把全部工作交给一个自由函数 `eval_markup`：

```rust
impl Eval for ast::Markup<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        eval_markup(vm, &mut self.exprs())
    }
}
```

见 [markup.rs:17-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L17-L23)。`self.exprs()` 返回一个迭代器，逐个吐出 `ast::Expr`。`eval_markup` 就是一个边迭代、边把结果塞进 `Vec<Content>` 的流式累加器。

为什么单独抽一个自由函数而不直接写在 `impl` 里？因为 `eval_markup` 要在处理 `set/show` 时**递归调用自己**去求 tail，自由函数签名更方便复用（这点和 `eval_code` 完全一致）。

#### 4.1.2 核心流程

`eval_markup` 的执行流程可以用下面这段伪代码概括：

```
fn eval_markup(vm, exprs) -> Content:
    flow = vm.flow.take()          # 保存进入前可能残留的 flow（保存-恢复模式）
    seq = Vec::new()
    while let Some(expr) = exprs.next():
        match expr:
          SetRule(set):
              styles = set.eval(vm)
              if vm.flow 出现: break
              tail = eval_markup(vm, exprs)     # 递归求「之后所有内容」
              seq.push(tail.styled_with_map(styles))   # 样式作用域
          ShowRule(show):
              recipe = show.eval(vm)
              if vm.flow 出现: break
              tail = eval_markup(vm, exprs)
              seq.push(tail.styled_with_recipe(recipe))
          其它任意 expr:
              value = expr.eval(vm)
              match value:
                Value::Label(label):  # 标签特殊处理（见 4.4）
                    回溯附加到 seq 里最后一个可标记元素
                value:
                    seq.push(value.display().spanned(expr.span()))
        if vm.flow 出现: break       # 任何分支结束后都检查 flow
    if flow 原本存在: vm.flow = flow  # 恢复
    return Content::sequence(seq)
```

三个要点：

1. **三个 match 分支**：`SetRule`、`ShowRule` 单独处理（递归 tail + 样式包裹）；其余所有表达式走第三分支，求值后判断是不是 `Value::Label`。
2. **flow 保存-恢复**包裹整个循环——和 u2-l3 的 `eval_code` 一模一样。
3. **累加器是 `Vec<Content>`**，最后统一 `Content::sequence(seq)`。

#### 4.1.3 源码精读

`eval_markup` 的真实主体见 [markup.rs:26-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L26-L87)。逐段看关键代码：

进入时取出 flow、预分配 `seq`：

```rust
let flow = vm.flow.take();
let mut seq = Vec::with_capacity(exprs.size_hint().1.unwrap_or_default());
```

见 [markup.rs:30-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L30-L31)（`size_hint().1` 是迭代器给出的长度上限，用于预分配容量）。

`SetRule` 分支——求样式、查 flow、递归求 tail、样式包裹：

```rust
ast::Expr::SetRule(set) => {
    let styles = set.eval(vm)?;
    if vm.flow.is_some() { break; }
    seq.push(eval_markup(vm, exprs)?.styled_with_map(styles));
}
```

见 [markup.rs:35-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L35-L42)。`ShowRule` 分支结构对称，只是把 `styled_with_map` 换成 `styled_with_recipe`，见 [markup.rs:43-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L43-L51)。

其余表达式的默认分支（含 Label 特殊处理，详见 4.4）：

```rust
expr => match expr.eval(vm)? {
    Value::Label(label) => { /* 回溯附加，见 4.4 */ }
    value => seq.push(value.display().spanned(expr.span())),
},
```

见 [markup.rs:52-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L52-L74)。注意 `value.display().spanned(expr.span())`：把任意 `Value` 转成可显示 `Content` 并贴上 span。

循环结束后恢复 flow、返回序列：

```rust
if flow.is_some() { vm.flow = flow; }
Ok(Content::sequence(seq))
```

见 [markup.rs:82-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L82-L86)。

**横向对比 `eval_code`（u2-l3）**：二者结构高度同构，差别只在「累加方式」和「值如何入列」：

| 维度 | `eval_code`（代码模式） | `eval_markup`（标记模式） |
|---|---|---|
| 累加器 | `Value::None` + `ops::join` | `Vec<Content>` + `Content::sequence` |
| 单条表达式结果 | 一个 `Value`，join 进 output | 一个 `Value`，`display()` 成 `Content` 后 push |
| set/show 包裹 tail | `tail.display().styled_with_map(...)` | `tail.styled_with_map(...)`（tail 已是 Content，无需再 display） |
| Label 特殊处理 | 无（代码模式写不了 `<label>`） | 有（4.4 详解） |

#### 4.1.4 代码实践

**实践目标**：通过对比源码，理解「为什么标记模式和代码模式需要两个不同的流式求值器」。

**操作步骤**：

1. 打开 [markup.rs:26-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L26-L87) 的 `eval_markup`。
2. 打开 [code.rs:26-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L26-L74) 的 `eval_code`。
3. 回答下面三个问题（在源码里定位到具体行）：
   - 两者各自用什么作为「累加器」的初始值？
   - 在 `SetRule` 分支里，`eval_code` 多调了一次 `.display()`，`eval_markup` 没有。为什么？（提示：递归 `eval_code` 返回 `Value`，递归 `eval_markup` 返回 `Content`。）
   - 为什么 `eval_markup` 有 `Value::Label` 分支而 `eval_code` 没有？

**需要观察的现象 / 预期结果**：

- `eval_code` 的 output 是单个 `Value`（`Value::None` 起步）；`eval_markup` 的 output 是单个 `Content`（`Vec` 收尾）。
- 因为 `eval_markup` 的 tail 本身就是 `Content`，所以不需要 `.display()` 这一步。
- 因为 `<label>` 是标记模式独有的语法，代码模式里根本不会出现 `Value::Label`，所以 `eval_code` 不需要这个分支。

本实践为「源码阅读型」，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`eval_markup` 为什么用 `Vec<Content>` + `Content::sequence`，而不是像 `eval_code` 那样用 `ops::join`？

> **答案**：标记模式的语义是「按顺序排版一连串内容片段」，需要保留顺序与结构（哪些是独立元素），`Content::sequence` 正是为此设计；而 `ops::join` 带有「`None` 是单位元、Content 之间有特殊拼接规则」的值层语义，更适合代码模式把多个值合并成一个值。

**练习 2**：在标记模式里写一个裸的 `#set text(style: "italic")` 会报错吗？

> **答案**：不会。在标记流的语句位置，`set` 是合法的「样式作用域」，它会被 `eval_markup` 的 `SetRule` 分支吃掉并对后续 tail 生效（见 4.1.3）。`set/show` 只是在**表达式位置**（被 `Expr::eval` 直接求值时）才被 `forbidden` 闭包拦截报错，详见 [code.rs:136-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L136-L137)。

### 4.2 文本类节点求值：Text / Space / Strong / Emph / Heading

#### 4.2.1 概念说明

这一类是 Markup 流里最朴素的「叶子」节点。它们的共同特点是：求值结果是一个 `Content`，对应 `typst-library` 里的一个具体元素（`TextElem`、`SpaceElem`、`StrongElem` 等）。

其中**真正自包含**的叶子（`Text`、`Space`、`Linebreak`、`Parbreak`、`Escape`、`Shorthand`、`SmartQuote`、`Raw`、`Link`）连 `Vm` 都不需要——你会看到它们的签名是 `_: &mut Vm`；而**带子体**的节点（`Strong`、`Emph`、`Heading`）需要递归求值 body，所以签名是 `vm: &mut Vm`。

#### 4.2.2 核心流程

这些叶子节点在 `Expr::eval` 总分发器里统一被处理，见 [code.rs:85-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L85-L104)：

```rust
Self::Text(v) => v.eval(vm).map(Value::Content),
Self::Space(v) => v.eval(vm).map(Value::Content),
...
Self::Strong(v) => v.eval(vm).map(Value::Content),
Self::Emph(v) => v.eval(vm).map(Value::Content),
Self::Heading(v) => v.eval(vm).map(Value::Content),
```

每个标记叶子节点的 `eval` 返回 `Content`，再用 `.map(Value::Content)` 适配成统一的 `Value`，最后由 `Expr::eval` 末尾统一调 `vm.trace_at(span, &value)`（满足 u1-l4 讲过的 trace 调用约定）。

#### 4.2.3 源码精读

**纯文本与空白**（自包含，`_: &mut Vm`）：

```rust
// Text: 把字符串打包成 TextElem
impl Eval for ast::Text<'_> {
    type Output = Content;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        Ok(TextElem::packed(self.get().clone()))
    }
}
// Space: 直接克隆一个共享单例
impl Eval for ast::Space<'_> {
    type Output = Content;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        Ok(SpaceElem::shared().clone())
    }
}
```

见 [markup.rs:89-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L89-L103)。`TextElem::packed(...)` 是个便捷构造器，内部调用 `Self::new(text).pack()`（`pack` 见 [element.rs:241-243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L241-L243)）。`SpaceElem::shared()` 返回一个全局共享实例——因为空白不携带任何数据，所有空白都可以共用同一份 `Content` 以省内存。`Linebreak`、`Parbreak` 同理用共享单例，见 [markup.rs:105-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L105-L119)。

**转义与简写**（返回 `Value::Symbol`，不是 Content）：

```rust
impl Eval for ast::Escape<'_> {
    type Output = Value;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        Ok(Value::Symbol(Symbol::runtime_char(self.get())))
    }
}
```

见 [markup.rs:121-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L121-L135)。`Escape`（如 `\#`）和 `Shorthand`（如 `->`）的 `Output` 是 `Value` 而非 `Content`，产出的是 `Value::Symbol`。注意：在 `Expr::eval` 里它们走的是 `Self::Escape(v) => v.eval(vm)`（**没有** `.map(Value::Content)`），见 [code.rs:90-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L90-L91)。那它怎么变成可见内容？答案是 `eval_markup` 默认分支里的 `value.display()`：`Value::Symbol` 经 `display()` 转成 `SymbolElem` 的 `Content`（见 [value.rs:193-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L193-L209)）。

**强调与粗体**（带子体，递归求值）：

```rust
impl Eval for ast::Strong<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let body = self.body().eval(vm)?;
        Ok(StrongElem::new(body).pack())
    }
}
```

见 [markup.rs:145-161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L145-L161)（`Strong` 和 `Emph` 结构完全对称）。关键在 `self.body().eval(vm)?`——body 本身又是一个 `Markup`，递归走回 4.1 的 `eval_markup`。这就是「`*粗体里的 #代码()*`」能嵌套工作的原因。

**标题**（带 depth + body）：

```rust
impl Eval for ast::Heading<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let depth = self.depth();
        let body = self.body().eval(vm)?;
        Ok(HeadingElem::new(body).with_depth(depth).pack())
    }
}
```

见 [markup.rs:210-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L210-L218)。`= ` 是一级标题、`== ` 是二级，`depth` 就是 `=` 的个数；body 同样递归求值。

#### 4.2.4 代码实践

**实践目标**：通过签名判断哪些标记节点自包含、哪些需要递归，并解释 `Escape` 为何返回 `Value::Symbol`。

**操作步骤**：

1. 在 [markup.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs) 中找出所有 `impl Eval`，按签名分两类：
   - 形参是 `_: &mut Vm`（自包含）
   - 形参是 `vm: &mut Vm`（需要 Vm，通常是递归求 body）
2. 对照 [code.rs:85-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L85-L104)，确认 `Escape`/`Shorthand` 没有 `.map(Value::Content)`，而 `Text`/`Strong`/`Heading` 有。

**预期结果**：

- 自包含（`_: &mut Vm`）：`Text`、`Space`、`Linebreak`、`Parbreak`、`Escape`、`Shorthand`、`SmartQuote`、`Raw`、`Link`、`Label`。
- 需要 Vm（`vm: &mut Vm`）：`Strong`、`Emph`、`Heading`、`ListItem`、`EnumItem`、`TermItem`、`Ref`（它们都要递归求子体）。
- `Escape`/`Shorthand` 返回 `Value::Symbol`，所以在 `Expr::eval` 里**不加** `.map(Value::Content)`；它们的 `Value` 流到 `eval_markup` 默认分支后，由 `value.display()` 转成可见的 `SymbolElem` 内容。

#### 4.2.5 小练习与答案

**练习 1**：`Space`、`Linebreak`、`Parbreak` 为什么用 `.shared().clone()` 而不是每次 `new` 一个？

> **答案**：它们不携带任何数据，所有实例完全等价，用全局共享单例可以避免重复分配，省内存、也便于相等性比较。

**练习 2**：在标记正文里写 `#let x = 1`（注意 `#let`），它会渲染出什么？为什么？

> **答案**：什么都不渲染。`#let` 注入的是一个 `LetBinding` 表达式，求值结果是 `Value::None`；流到 `eval_markup` 默认分支后，`Value::None.display()` 返回 `Content::empty()`（见 [value.rs:193-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L193-L209)），push 进 seq 的是一个空内容，自然没有可见输出。这正是「`#let` 只绑定、不输出」的底层原因。

### 4.3 列表、枚举、术语、原始文本与链接

#### 4.3.1 概念说明

这一类是「带结构」的标记节点，都要递归求一个或多个子体，并往往携带额外字段：

| 节点 | 语法 | 关键字段 |
|---|---|---|
| `ListItem` | `- item` | body |
| `EnumItem` | `+ item` / `+ <num> item` | body + 可选 number |
| `TermItem` | `/ term: desc` | term + description |
| `Raw` | `` `code` `` / ` ```block``` ` | lines + block 标志 + 可选 lang |
| `Link` | `https://...` | url（可能非法） |

#### 4.3.2 核心流程

每个节点的求值套路一致：递归求子体 → 构造强类型元素 → 用 `with_xxx` 设置额外字段 → `pack()`。唯一特殊的是 `Link`：解析 URL 可能失败，所以用了 `.at(span)?` 做错误定位。

#### 4.3.3 源码精读

**列表项**：

```rust
impl Eval for ast::ListItem<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        Ok(ListItem::new(self.body().eval(vm)?).pack())
    }
}
```

见 [markup.rs:220-226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L220-L226)。

**枚举项**（带可选起始编号）：

```rust
impl Eval for ast::EnumItem<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let body = self.body().eval(vm)?;
        let mut elem = EnumItem::new(body);
        if let Some(number) = self.number() {
            elem.number.set(Smart::Custom(number));
        }
        Ok(elem.pack())
    }
}
```

见 [markup.rs:228-239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L228-L239)。`Smart` 是 Typst 的「智能或自定义」枚举：没有写编号时默认是 `Smart::Auto`（自动顺延），写了 `+ 3.` 时才设成 `Smart::Custom(number)`。

**术语项**（term + description 两段子体）：

```rust
impl Eval for ast::TermItem<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let term = self.term().eval(vm)?;
        let description = self.description().eval(vm)?;
        Ok(TermItem::new(term, description).pack())
    }
}
```

见 [markup.rs:241-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L241-L249)。

**原始文本**（行集合 + block 标志 + 语言）：

```rust
impl Eval for ast::Raw<'_> {
    type Output = Content;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        let lines = self.lines().map(|line| (line.get().clone(), line.span())).collect();
        let mut elem = RawElem::new(RawContent::Lines(lines)).with_block(self.block());
        if let Some(lang) = self.lang() {
            elem.lang.set(Some(lang.get().clone()));
        }
        Ok(elem.pack())
    }
}
```

见 [markup.rs:163-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L163-L174)。注意两点：(1) 每一行都同时保存了文本和 `span`，方便在多行 raw 块里精确定位错误/支持 IDE；(2) `self.block()` 区分这是行内 `` `code` `` 还是块级 ` ```code``` `，`self.lang()` 取可选的语言标注。

**链接**（URL 解析可能失败）：

```rust
impl Eval for ast::Link<'_> {
    type Output = Content;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        let url = Url::new(self.get().clone()).at(self.span())?;
        Ok(LinkElem::from_url(url).pack())
    }
}
```

见 [markup.rs:176-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L176-L183)。这里 `.at(self.span())?` 是 u6-l1 会详讲的 `At` trait：把 `Url::new` 返回的字符串错误转成带 span 的源诊断。这是本讲里**唯一可能在求值期失败**的叶子节点（其它叶子都返回 `Ok`）。

#### 4.3.4 代码实践

**实践目标**：理解 `Link` 为何是唯一会失败的叶子，以及 `Raw` 如何区分行内/块级。

**操作步骤**：

1. 在 [markup.rs:89-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L89-L249) 的所有 `impl Eval` 中，搜索 `?` 运算符。你会发现只有 `Link`（[L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L180)）用了 `?`，其它叶子全是 `Ok(...)`。
2. 阅读 `Raw` 的 [L167-L168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L167-L168)，确认 `block` 标志来自 `self.block()`。

**可运行实践（待本地验证）**：如果你本地装了 typst CLI，写一个 `test.typ`：

```typst
行内代码：`let x = 1`

```rust
let x = 1;
```

链接：https://typst.app
```

运行 `typst compile test.typ`，观察行内 raw 与块级 raw 的排版差异。

**预期结果**：行内 raw 不换行，块级 raw 独占一段——这种差异在求值期就被编码进了 `RawElem` 的 `block` 字段。

#### 4.3.5 小练习与答案

**练习 1**：`EnumItem` 的 `number` 字段为 `Smart::Auto` 和 `Smart::Custom(n)` 有何区别？

> **答案**：`Smart::Auto` 表示自动按顺序编号（1、2、3……）；`Smart::Custom(n)` 表示用户显式指定了起始编号（如 `+ 5. 第五项`）。求值时只有当 `self.number()` 返回 `Some` 才会设成 `Custom`，否则保持默认的 `Auto`。

**练习 2**：为什么 `Raw` 要给每一行都存 `span`，而不只存合并后的文本？

> **答案**：为了在多行 raw 块内部精确定位错误（比如语法高亮失败）和支持 IDE 功能（如 hover、诊断）。span 是逐行保存的，所以能定位到具体某一行。

### 4.4 Label 附加与 Ref 求值

#### 4.4.1 概念说明

Typst 用 `<name>` 给内容贴标签，用 `@name` 引用它，例如：

```typst
= 引言 <intro>
...
详见 @intro
```

这里有一个反直觉但很关键的设计：**`<intro>` 本身不是内容，它不会产生任何可见输出**。它的语义是「把标签挂到**前面**最近一个可被标记的内容元素上」。所以标签的求值不能简单地 push 进 seq，而要**回溯**修改 seq 里已有的元素。这是 `eval_markup` 默认分支里专门给 `Value::Label` 留一个特殊分支的根本原因——也是本讲的核心难点。

`@intro` 则相反：它产生一个 `RefElem` 内容，排版时再去查找对应标签的目标。

#### 4.4.2 核心流程

整个 Label 机制分两步：

1. **`Label::eval` 产出一个 `Value::Label`**（不是 Content）。
2. **`eval_markup` 默认分支拦截 `Value::Label`**，做回溯附加：
   - 从 seq 末尾向前找第一个「可标记」的元素；
   - 找到 → 把标签贴上去（`.labelled(label)`）；
   - 找到但该元素已有标签 → 警告「content labelled multiple times」；
   - 没找到（seq 为空或全是不可标记元素）→ 警告「label is not attached to anything」。

「可标记」如何判断？用一个标记 trait `Unlabellable`：实现了它的元素**不能**被贴标签。目前有 `SpaceElem`、`ParbreakElem`、`TagElem` 三类实现了它。所以 `seq.iter_mut().rev().find(|node| !node.can::<dyn Unlabellable>())` 的意思是：从后往前，跳过空白/段落分隔等不可标记元素，找到最后一个「真正能挂标签」的内容。

支撑类型回顾：
- `Unlabellable`：空标记 trait，见 [label.rs:109-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L109-L110)。
- `Content::can::<C>()`：能力查询，问「我里面的元素是否实现了 trait C」，见 [content/mod.rs:276-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L276-L281)。
- `Content::labelled(label)`：贴标签，见 [content/mod.rs:121-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L121-L124)。
- `Content::label()`：读取已有标签，见 [content/mod.rs:116-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L116-L118)。

#### 4.4.3 源码精读

`Label` 的求值本身极简——产出 `Value::Label`：

```rust
impl Eval for ast::Label<'_> {
    type Output = Value;
    fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
        Ok(Value::Label(
            Label::new(PicoStr::intern(self.get())).expect("unexpected empty label"),
        ))
    }
}
```

见 [markup.rs:185-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L185-L193)。注意 `Output = Value`，且在 `Expr::eval` 里走 `Self::Label(v) => v.eval(vm)`（无 `.map`），见 [code.rs:97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L97)。

真正的重头戏是 `eval_markup` 默认分支里的 `Value::Label` 处理：

```rust
Value::Label(label) => {
    if let Some(elem) =
        seq.iter_mut().rev().find(|node| !node.can::<dyn Unlabellable>())
    {
        if elem.label().is_some() {
            vm.engine.sink.warn(warning!(
                elem.span(), "content labelled multiple times";
                hint: "only the last label is used, the rest are ignored";
            ));
        }
        *elem = std::mem::take(elem).labelled(label);
    } else {
        vm.engine.sink.warn(warning!(
            expr.span(),
            "label `{}` is not attached to anything",
            label.repr(),
        ));
    }
}
```

见 [markup.rs:53-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L53-L72)。逐行拆解：

- **L54-55** `seq.iter_mut().rev().find(|node| !node.can::<dyn Unlabellable>())`：从后往前找第一个「不实现 `Unlabellable`」的元素，即最后一个可标记元素。
- **L57-62** 如果该元素**已经有标签**，发警告 `content labelled multiple times`，hint 说明「只有最后一个标签生效，其余忽略」。
- **L64** `*elem = std::mem::take(elem).labelled(label)`：`mem::take` 拿走旧内容的所有权（原位置变成空内容占位），调用 `.labelled(label)` 返回一个贴了标签的新内容，再写回 `*elem`。
- **L65-71** `else` 分支：从后往前没找到任何可标记元素，发警告 `label "{}" is not attached to anything`。

`Ref` 的求值则直接产出 `RefElem`，并可带一个 supplement（补充说明）：

```rust
impl Eval for ast::Ref<'_> {
    type Output = Content;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let target = Label::new(PicoStr::intern(self.target()))
            .expect("unexpected empty reference");
        let mut elem = RefElem::new(target);
        if let Some(supplement) = self.supplement() {
            elem.supplement
                .set(Smart::Custom(Some(Supplement::Content(supplement.eval(vm)?))));
        }
        Ok(elem.pack())
    }
}
```

见 [markup.rs:195-208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L195-L208)。`@intro` 的 `target` 就是 `intro`；`@intro [图]` 这种带方括号的形式会设置 `supplement`，排版成「图 1」之类。注意 `target` 用 `Label::new` 构造，和 `<intro>` 用的是同一种 `Label` 类型——这正是「引用」能对上「标签」的基础（标签在求值期已贴到元素上，引用在排版期通过 introspector 查找）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：吃透「标签回溯附加」逻辑，能预测任意标记里标签会挂到哪个元素、会触发哪条警告。

**操作步骤**：

1. 打开 [markup.rs:53-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L53-L72) 的 `Value::Label` 分支。
2. 解释 `seq.iter_mut().rev().find(|node| !node.can::<dyn Unlabellable>())` 这一行：
   - `rev()` 为什么从后往前？（因为标签挂在**前面最近**的元素上，而 seq 里该元素是当前最后一个。）
   - `!node.can::<dyn Unlabellable>()` 为什么取反？（`can` 返回 true 表示「实现了 Unlabellable = 不可标记」，取反后 `find` 命中的是「可标记」的元素。）
3. 对照 [label.rs:109-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/label.rs#L109-L110) 确认 `Unlabellable` 是空 trait，并回忆它由 `SpaceElem`、`ParbreakElem`、`TagElem` 实现。
4. 回答两条警告分别在什么条件下触发（见下「预期结果」）。

**预期结果**：

| 场景 | seq 末尾情况 | 行为 | 警告 |
|---|---|---|---|
| `[Hello]<hi>` | 末尾是 `StrongElem`/`TextElem` 等可标记元素 | 标签挂到它身上 | 无 |
| `[Hello]<a> <b>` | 末尾元素已被 `<a>` 贴过标签 | `<b>` 覆盖之 | `content labelled multiple times`（hint: only the last label is used） |
| `   <x>`（前面只有空白） | 末尾全是 `SpaceElem`（Unlabellable） | 找不到可标记元素 | `label "x" is not attached to anything` |
| 空文档里首个 token 是 `<x>` | seq 为空 | 找不到 | 同上 |

**可运行实践（待本地验证）**：写一个 `label.typ`：

```typst
= 第一节 <a> <b>

正文

<孤立的标签>
```

运行 `typst compile label.typ`，应当看到两条警告：一条提示「content labelled multiple times」（`<a>` 被忽略，只有 `<b>` 生效），一条提示「label `孤立的标签` is not attached to anything」。

#### 4.4.5 小练习与答案

**练习 1**：给定标记流 `= 标题 <a> <b>`（标题后紧跟两个标签），哪个标签最终生效？

> **答案**：`<b>` 生效。处理 `<a>` 时，标题元素无标签，正常贴上；处理 `<b>` 时发现标题已有标签，于是发 `content labelled multiple times` 警告，但 `<b>` 仍覆盖 `<a>`（`*elem = ...labelled(label)` 无条件写回）。hint 提示「只有最后一个标签生效，其余忽略」。

**练习 2**：给定 `正文   <x>`（正文后是几个空格再是标签），`<x>` 能挂上吗？

> **答案**：能。`rev().find(...)` 从后往前扫描时，会**跳过**末尾的 `SpaceElem`（它实现了 `Unlabellable`，`can` 返回 true，取反为 false，不满足 `find` 条件），最终命中前面的 `TextElem`（正文），把 `<x>` 贴到正文上。这正是用 `rev().find` 而非 `seq.last()` 的意义。

**练习 3**：对一个 `HeadingElem` 调用 `can::<dyn Unlabellable>()` 返回什么？为什么？

> **答案**：返回 `false`。`HeadingElem` 没有实现 `Unlabellable` trait（只有 `SpaceElem`、`ParbreakElem`、`TagElem` 实现了），所以 `can` 返回 false，`!false == true`，`find` 会命中它——即标题是「可标记」的。

## 5. 综合实践

把本讲所有知识点串起来：**手工追踪一段 Markup 文档在 `eval_markup` 里的求值过程**，预测每一步 `seq` 的内容，以及最终 `Content::sequence` 的结构。

**任务**：阅读下面这段 Typst 文档，逐行追踪 `eval_markup` 的执行（设进入时 `vm.flow` 为空）：

```typst
#set heading(numbering: "1.")
= 引言 <intro>
- 第一项
详见 @intro
```

**追踪步骤**（请你在源码里核对每一步对应的分支）：

1. 第 1 个 expr 是 `SetRule`（`#set heading(...)`）：
   - 走 [markup.rs:35-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L35-L42)。求出 `styles`，递归 `eval_markup` 求「之后所有内容」（即下面三行）作为 tail。
2. 递归 tail 的第 1 个 expr 是 `Heading`（`= 引言`）：
   - 走默认分支 → [markup.rs:210-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L210-L218)，产出 `HeadingElem`，`value.display()` 后 push 进 seq。
3. tail 的第 2 个 expr 是 `Label`（`<intro>`）：
   - 走默认分支的 `Value::Label` → [markup.rs:53-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L53-L72)。`rev().find` 命中刚 push 的 `HeadingElem`（可标记），把 `intro` 贴上去。
4. tail 的第 3 个 expr 是 `ListItem`（`- 第一项`）：
   - 走默认分支 → [markup.rs:220-226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L220-L226)，产出 `ListItem`，push 进 seq。
5. tail 的第 4 个 expr 是 `Ref`（`@intro`）：
   - 走默认分支 → [markup.rs:195-208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L195-L208)，产出 `RefElem`，push 进 seq。
6. 递归返回的 tail 是「`HeadingElem`(带 intro 标签) → `ListItem` → `RefElem`」的序列。
7. 回到第 1 步的 `SetRule` 分支，把这个 tail 用 `styled_with_map(styles)` 包裹（标题编号样式生效），push 进外层 seq。
8. 外层循环结束，`Content::sequence(seq)` 返回。

**请你完成**：画出最终的 `Content` 树状结构（应是一个带 `heading numbering` 样式的序列，序列里第一个元素是带 `intro` 标签的 `HeadingElem`）。并思考：如果删掉 `#set heading(...)` 这一行，求值路径会发生什么变化？（提示：少了 `SetRule` 分支的递归包裹，四行直接成为顶层 seq 的四个元素。）

## 6. 本讲小结

- `Markup::eval` 把工作全权委托给自由函数 `eval_markup`，后者流式迭代标记表达式流，用 `Vec<Content>` 收集，最后返回 `Content::sequence(seq)`。
- `eval_markup` 与 `eval_code` 结构同构（都做 flow 保存-恢复、都把 set/show 当作 tail 的样式作用域），差别只在累加器（`Vec<Content>` + sequence vs `Value::None` + `ops::join`）和值的入列方式。
- 文本类叶子节点（`Text`/`Space`/`Strong`/`Emph`/`Heading` 等）求值就是「读数据 → 构造强类型元素 → `pack()` 成 `Content`」；自包含的用 `_: &mut Vm`，带子体的用 `vm: &mut Vm` 递归。
- `Escape`/`Shorthand` 返回 `Value::Symbol`（不是 Content），靠 `eval_markup` 默认分支的 `value.display()` 转成可见的 `SymbolElem`；同理 `#let` 返回 `Value::None`，`display()` 后是空内容，所以不产生输出。
- `<label>` 是标记模式独有的特殊值，被 `eval_markup` 的 `Value::Label` 分支**回溯附加**到 seq 里最后一个可标记元素上；`Unlabellable` trait（由 `SpaceElem`/`ParbreakElem`/`TagElem` 实现）决定哪些元素不能被贴标签。
- 两条标签相关警告：`content labelled multiple times`（目标已有标签，后者覆盖前者）与 `label "..." is not attached to anything`（找不到可标记元素）。
- `@ref` 求值直接产出 `RefElem`，其 `target` 与 `<label>` 共用 `Label` 类型，为排版期的交叉引用查找打下基础。

## 7. 下一步学习建议

- **紧接 u2-l5（Math 模式求值）**：数学模式与标记模式高度相似，`Math::eval` 同样是流式拼接，但它产出的是 `EquationElem`，且数学标识符走 `scopes.get_in_math`（而非普通 `get`）。读完本讲再读 `src/math.rs` 会非常顺畅。
- **横向对照 u3-l1/u3-l2（控制流）**：本讲的 flow 保存-恢复只是「外壳」，真正的 `FlowEvent`（break/continue/return）生成与消费在 `src/flow.rs`，u3 会完整拆解。
- **深入 set/show**：本讲只把 `SetRule`/`ShowRule` 当作「求出 styles/recipe 后包裹 tail」的黑盒，其内部如何校验元素函数、如何组装 `Recipe` 在 u5-l2（`src/rules.rs`）详讲。
- **诊断体系**：本讲出现的 `warning!` 宏和 `.at(span)?` 是 typst 诊断体系的一部分，u6-l1 会系统总结「错误信息 + 精确定位 + 修复 hint」三要素。
