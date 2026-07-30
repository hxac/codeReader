# 三种语法模式的补全

## 1. 本讲目标

本讲紧接 u5-l2（Completion 数据模型与 CompletionContext）。上一讲我们打开了贯穿分发链的两个对象——单条候选 `Completion` 与可变上下文 `CompletionContext`；本讲则回到分发链的**最末一段**：当所有「更具体」的补全（字段访问、import、规则、参数）都没有命中时，`autocomplete` 会按当前光标所处的**语法模式（SyntaxMode）**分派到三个函数 `complete_markup` / `complete_math` / `complete_code`。

这三个函数是补全引擎的「兜底通用补全」，它们负责识别各自模式下的触发条件（`@` 引用、`#` 嵌入代码、```` ``` ```` raw 块标签、标识符、显式触发等），并产出结构化 snippet 与作用域补全。

学完后你应该能够：

- 说出 `complete_markup` 识别的六类触发条件（引用起点、已有引用、半成品 `let` 绑定、`context` 块、raw 块、显式触发），以及每类分别调用谁。
- 说出 `complete_math` 只有两类触发（已有原子/标识符、显式触发），以及它产出的三类 snippet（下标、上标、分式）。
- 区分 `complete_code` 的三种分支：`Hash`（`hash=true`）、`Ident`、`explicit`，并解释 `hash` 参数如何同时影响 `scope_completions` 的过滤与「function」snippet 的取舍。
- 解释 `markup_completions` / `math_completions` / `code_completions` 三个「snippet 工厂」各自往 `ctx.completions` 里塞了哪些固定片段，以及它们如何借助 `scope_completions` 补作用域名。
- 讲清 `raw_completions` 的数据来源 `RawElem::languages()` 与它如何把语言名/标签压成一条候选。

## 2. 前置知识

本讲建立在已经学过的几讲之上，下面只做要点回顾，不重复展开。

- **语法模式 `SyntaxMode`（来自 u5-l1）**：Typst 源码的每个位置都属于三种模式之一——`Markup`（正文）、`Math`（`$...$` 公式）、`Code`（`#` 后、`{}`、`()` 等代码区）。`autocomplete` 入口用 `ctx.leaf.mode_after()?` 一次性拿到模式，同时这个调用还有一个副作用：**如果光标落在注释里会返回 `None`，整个函数提前退出**，从而把注释屏蔽封装在底层。
- **短路分发链（来自 u5-l1）**：`complete_field_accesses || complete_open_labels || complete_imports || complete_rules || complete_params || match mode { ... }`。三个模式函数位于链的最末，意味着只有前面的专项补全都「不感兴趣」（返回 `false`）时才会执行。候选项以副作用写入共享的 `ctx.completions`，函数返回的 `bool` 仅用于控制是否短路。
- **`Completion` 四字段与 `snippet_completion`（来自 u5-l2）**：`kind / label / apply / detail`。`snippet_completion(label, snippet, docs)` 是一个快捷方法，固定产出 `kind = CompletionKind::Syntax` 的一条候选，`apply` 用 LSP 片段语法 `${}` / `${名称}` / `${1:默认值}` 表示可 Tab 跳转的占位符。**注意：片段里的 `${}` 与 Typst 自身的 `$`（数学）语法完全无关**，前者是编辑器层占位符。
- **`scope_completions(parens, filter)`（来自 u5-l2，定义在 u6 展开）**：把 `named_items` 收集的局部命名项与 `globals` 返回的标准库作用域合并成候选。`parens` 决定函数值是否自动补括号；`filter` 决定哪些值能进列表（并且会经 `check_value_recursively` 放行「容器内含目标类型」的值）。本讲只把它当现成工具用。
- **`globals(world, leaf)`（来自 u2-l5）**：依据 `leaf.mode_after()` 决定返回 `library.math`（仅 `Some(Math)` 时）还是 `library.global`（其余一律）。这正是「数学模式补数学符号、其余模式补代码符号」的根因，本讲的 `complete_math` 测试会再次印证它。
- **负数光标（来自 u1-l3 / u8-l1）**：测试里 `test("$x$", -2)` 表示光标在字符串末尾倒数第 2 个字节处（`-1` 是最末尾之后）。本讲引用测试时会标注光标位置。

## 3. 本讲源码地图

本讲几乎全部落在同一个文件：

| 文件 | 作用 |
| --- | --- |
| `src/complete.rs` | 补全引擎主体。本讲覆盖其中的三个模式分派函数、三个 snippet 工厂、以及 `raw_completions` 方法。 |

涉及的函数与大致行号一览（行号对应当前 HEAD `146a5832`）：

| 函数 | 位置 | 作用 |
| --- | --- | --- |
| `autocomplete`（分发到三模式） | `complete.rs:63-67` | 链尾 `match mode` 把控制权交给下面三者之一 |
| `complete_markup` | `complete.rs:626-686` | Markup 模式触发判定（引用、let、context、raw、显式） |
| `markup_completions` | `complete.rs:690-786` | Markup 模式的 16 个固定 snippet |
| `complete_math` | `complete.rs:789-807` | Math 模式触发判定（原子/显式） |
| `math_completions` | `complete.rs:811-831` | Math 模式：scope 补全 + 3 个 snippet |
| `complete_code` | `complete.rs:834-875` | Code 模式三类分支（Hash / Ident / explicit） |
| `code_completions` | `complete.rs:879-1032` | Code 模式：scope 补全 + 约 22 个 snippet |
| `raw_completions`（方法） | `complete.rs:1194-1213` | raw 块语言标签补全，数据来自 `RawElem::languages()` |
| `scope_completions`（方法） | `complete.rs:1426-1462` | 作用域合并，被 math/code 补全复用 |
| `snippet_completion`（方法） | `complete.rs:1105-1117` | snippet 候选的快捷构造器 |

`src/utils.rs` 本讲不直接精读，只通过 u2-l5 已建立的 `globals` 概念间接引用。

## 4. 核心概念与源码讲解

先看三个模式函数在分发链里的位置。它们是整条短路链的最后一站：

[complete.rs:54-67 —— 求模式并短路分发到三模式函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L54-L67) 第 55 行 `ctx.leaf.mode_after()?` 既给出模式又在注释里屏蔽返回 `None`；第 57-62 行是五个专项补全的短路；第 63-67 行 `match mode` 把 `Markup / Math / Code` 分别交给本讲的三个函数。

理解了这层「兜底」定位之后，下面按 markup → math → code → snippet 工厂 → raw 的顺序逐个拆开。

### 4.1 complete_markup —— Markup 模式的六类触发

#### 4.1.1 概念说明

Markup 模式就是 Typst 文档的正文区（不在 `$...$` 里、也不在 `#` 之后的代码区）。正文里大部分「补全」其实是**插入结构化的语法片段**（标题、列表项、强调、raw 块……），少数是**跨模式的桥接**（光标在 `@` 后补标签引用、在 `#let x = ` 后切回代码补全）。

`complete_markup` 要解决的问题是：**正文里光标位置千差万别，但只有几种值得弹补全——其余位置应当「安静」**。所以它本质上是一组按顺序判定的「触发条件」，每个命中就调用对应的补全函数并返回 `true`（短路）。它**显式地依赖 `explicit` 标志**：只有在用户主动请求（`explicit=true`，例如按 Ctrl+Space）时，才会在「任意正文位置」弹出全部 markup snippet；否则只在少数有明确信号的位置才弹。

#### 4.1.2 核心流程

`complete_markup` 的判定顺序（首个命中即返回 `true`，否则落到末尾返回 `false`）：

```
complete_markup(ctx):
  assert 模式确实是 Markup
  ① 光标在 "@" 之后（引用起点）        → label_completions()        [补标签]
  ② 光标在已有 RefMarker 上（@he|）    → label_completions()        [补标签]
  ③ 前一个叶子是 "=" 且父为 LetBinding → code_completions(hash=false) [切回代码]
  ④ 前一个叶子是 "context"             → code_completions(hash=false) [切回代码]
  ⑤ 正好在 ```raw 块``` 的标签位置      → raw_completions()          [补语言标签]
  ⑥ explicit=true（任意正文位置）       → markup_completions()       [补 16 个 snippet]
  否则返回 false
```

几个关键点：

- **①②共用 `label_completions`**，但 `from`（替换起点）不同：① 从光标处开始（`@` 后还没打字），② 从 `RefMarker` 偏移 +1 处开始（跳过开头的 `@`，只替换已打的名字部分）。`label_completions` 内部依赖可选的编译产物 `output`（来自 u6-l3）。
- **③④是「跨模式桥接」**：正文里写 `#let x = ` 或 `#context ` 之后，用户要写的是**代码表达式**，所以这里直接调用 `code_completions(ctx, false)`（`hash=false` 表示不在 `#` 之后、要完整作用域）。
- **⑤用文本扫描而非语法树**判定 raw 块（见 4.5）。
- **⑥是兜底**：只有显式触发才会在普通正文（如 `Hello|`）弹出 `markup_completions` 的全部片段。

#### 4.1.3 源码精读

[complete.rs:626-686 —— complete_markup 全函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L626-L686) 第 627 行的 `debug_assert_eq!(ctx.leaf.mode_after(), Some(SyntaxMode::Markup))` 是一道断言，确认分发链确实把 Markup 位置路由到了这里——三个模式函数开头都有这样一道断言，互为校验。

看 ①②两段引用补全：

```rust
// Start of a reference: "@|".
if ctx.leaf.kind() == SyntaxKind::Text && ctx.before.ends_with("@") {
    ctx.from = ctx.cursor;
    ctx.label_completions();
    return true;
}

// An existing reference: "@he|".
if ctx.leaf.kind() == SyntaxKind::RefMarker {
    ctx.from = ctx.leaf.offset() + 1;
    ctx.label_completions();
    return true;
}
```

注意 ① 用的是 `ctx.before.ends_with("@")`——判断「`@` 之后、还没打字」这个瞬间，此时叶子是 `Text` 且光标前的文本以 `@` 结尾。②的 `ctx.from = ctx.leaf.offset() + 1` 里那个 `+ 1` 正是为了跳过 `RefMarker` 开头的 `@` 字符，只替换名字部分。

看 ③④桥接代码补全：

```rust
// Behind a half-completed binding: "#let x = |".
if let Some(prev) = ctx.leaf.prev_leaf()
    && prev.kind() == SyntaxKind::Eq
    && prev.parent_kind() == Some(SyntaxKind::LetBinding)
{
    ctx.from = ctx.cursor;
    code_completions(ctx, false);
    return true;
}
```

它通过 `prev_leaf()` 取前一个叶子，判断是不是 `=` 且其父节点是 `LetBinding`。这样即便 `#let x = ` 之后还没有任何 token，也能识别出「用户正要写等号右边的表达式」。`context` 分支结构完全对称，只是判断 `SyntaxKind::Context`。

看 ⑥显式兜底：

```rust
// Anywhere: "|".
if ctx.explicit {
    ctx.from = ctx.cursor;
    markup_completions(ctx);
    return true;
}

false
```

只有 `ctx.explicit` 为真才调用 `markup_completions`。这也解释了**为什么正文里平时不会弹 snippet、按 Ctrl+Space 才会**——`explicit` 标志把「主动请求」和「被动跟随输入」区分开。

#### 4.1.4 代码实践

**实践目标**：验证 `@` 引用起点的 `from` 设定与 `Side` 的关系。

1. 阅读上面的 ①分支，确认它依赖 `ctx.before.ends_with("@")`。
2. 回顾 u2-l1：`autocomplete` 入口用的是 `leaf_at(cursor, Side::Before)`。思考光标正好在 `@` 字符**之后**（位置 1）时，`Side::Before` 选中的是哪个 token、`ctx.before` 是否以 `@` 结尾。
3. 对比 ②分支：如果用户已经打出 `@he`，叶子变成 `RefMarker`，`from` 会设成 `leaf.offset() + 1`。

**需要观察的现象 / 预期结果**：在源码 `"@"` 上、光标置末尾（等价于测试 `test("@", -1)`），`complete_markup` 走 ①分支，`from = cursor`，调用 `label_completions`；若该测试 world 没有编译产物（`output=None`），`label_completions` 会因 `let Some(output) = self.output else { return };`（见 [complete.rs:1216-1217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1216-L1217)）直接返回——这正是 u1-l2 讲过的「可选增强、优雅降级」。本实践的运行结果**待本地验证**（你可仿照 u8-l1 的测试写法加断言）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ③分支判断 `prev.parent_kind() == Some(SyntaxKind::LetBinding)` 而不是只判断 `prev.kind() == Eq`？
**答案**：因为正文里 `=` 还可能出现在别处（例如术语列表项 `/ term: desc` 之外、或解析错误片段）。加上「父节点是 `LetBinding`」这个上下文，才能确定这个 `=` 确实是 `#let x = ...` 里的赋值号，从而安全地切到代码补全。

**练习 2**：若把 ⑥显式分支挪到 ①之前，会发生什么？
**答案**：`explicit=true` 时 ⑥会先命中并 `return true`，导致 `@`、`#let x =`、raw 块等位置都只会得到 16 个 markup snippet，而不会得到标签补全 / 代码补全 / 语言标签补全。所以当前顺序是有意为之——**更具体的触发条件必须排在通用兜底之前**。

### 4.2 complete_math —— Math 模式的两类触发

#### 4.2.1 概念说明

Math 模式是 `$...$` 公式内部。公式里的「名字」既可能是**数学符号**（`alpha`、`infty`），也可能是用户在代码层定义的**变量/函数**。`complete_math` 是三个模式函数里最简单的：它只识别两类触发，其余一切交给「显式触发」。

它的「简单」背后是 u2-l5 的 `globals` 在兜底——数学模式时 `globals` 返回 `library.math` 作用域，因此只要调一次 `scope_completions`，数学符号就自然进来了，不需要 `complete_math` 自己列举符号。

#### 4.2.2 核心流程

```
complete_math(ctx):
  assert 模式确实是 Math
  ① 叶子是 MathText 或 MathIdent（$a|$、$abc|$）→ from=leaf.offset(); math_completions()
  ② explicit=true（$|$ 任意位置）              → from=cursor;        math_completions()
  否则返回 false
```

注意 ①里 `from = ctx.leaf.offset()`，即从已打文字的开头开始替换——这样补全菜单会按已输入内容过滤。

#### 4.2.3 源码精读

[complete.rs:789-807 —— complete_math 全函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L789-L807) 主体很短：

```rust
// Behind existing atom or identifier: "$a|$" or "$abc|$".
if matches!(ctx.leaf.kind(), SyntaxKind::MathText | SyntaxKind::MathIdent) {
    ctx.from = ctx.leaf.offset();
    math_completions(ctx);
    return true;
}

// Anywhere: "$|$".
if ctx.explicit {
    ctx.from = ctx.cursor;
    math_completions(ctx);
    return true;
}

false
```

`MathText` 是公式里的普通文本原子（如单字符变量 `a`），`MathIdent` 是公式里的标识符（如多字符名字 `abc`）。两者都走 `math_completions`。

这部分的正确性有一个现成测试坐镇，它同时验证了 u2-l5 的 `globals` 行为：

[complete.rs:1670-1676 —— test_autocomplete_math_scope](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1670-L1676) 其中 `test("$col$", -2)`（光标在 `col` 中）会 `must_include(["colon"]).must_exclude(["colbreak"])`——`colon` 是数学符号、来自 `library.math`；而 `test("$#col$", -2)`（`#` 切到代码模式）则反过来 `must_include(["colbreak"]).must_exclude(["colon"])`，因为代码模式 `globals` 返回 `library.global`。这正说明 `complete_math` 与 `complete_code` 的差别最终落在 `globals` 的模式分支上。

#### 4.2.4 代码实践

**实践目标**：用一个已有测试体会「数学模式补数学符号、`#` 后补代码符号」的差异。

1. 打开 [complete.rs:1671](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1671) 的 `test_autocomplete_math_scope`。
2. 读这四行断言：`$#col$`（代码模式）期望含 `colbreak`、不含 `colon`；`$col$`、`$(col)$`、`$1/col$`（数学模式）期望含 `colon`、不含 `colbreak`。
3. 运行（待本地验证）：`cargo test -p typst-ide test_autocomplete_math_scope`。

**预期结果**：测试通过，说明 `math_completions` → `scope_completions` → `globals` 链路在数学模式下确实拿到 `library.math`。

#### 4.2.5 小练习与答案

**练习**：为什么 `complete_math` 不像 `complete_markup` 那样单独识别 `@` 引用、raw 块等？
**答案**：因为这些语法结构不属于数学模式——`@` 引用、```` ``` ```` raw 块是正文（Markup）的产物，公式里不会出现。模式函数各自只关心本模式合法的触发条件，这正是按 `SyntaxMode` 分派的收益：每个函数可以放心假设「我只会遇到本模式的语法」。

### 4.3 complete_code —— Code 模式的三种分支

#### 4.3.1 概念说明

Code 模式是 `#` 之后、`{}` 代码块、`()` 参数列表等位置。`complete_code` 识别三类触发，其中最关键的是 `hash` 参数——它表示「这次补全是不是发生在正文/公式里紧跟 `#` 之后」（如 `[#|]`、`$#|$`）。`hash` 会同时影响两件事：

1. **作用域过滤**：`hash=true` 时，`scope_completions` 会过滤掉 `Color`、`Dir`、`Alignment` 三类值，因为它们在正文里直接用 `#` 插入意义不大、却会大量刷屏。
2. **snippet 取舍**：`hash=true` 时，「function」片段（`() => {}`）被省略，因为紧跟 `#` 的位置更适合用 `let binding (function)` 那种带名字的定义。

#### 4.3.2 核心流程

```
complete_code(ctx):
  assert 模式确实是 Code
  ① 叶子是 Hash（[#|]、$#|$）          → code_completions(hash=true)
  ② 叶子是 Ident 且不是具名参数键       → code_completions(hash=false)
  ③ explicit 且不在 Dict 键、且叶子是   → code_completions(hash=false)
     trivia/LeftParen/LeftBrace/Comma/Colon
  否则返回 false
```

注意 ②有一个微妙守卫：`ctx.leaf.index() > 0 || ctx.leaf.parent_kind() != Some(SyntaxKind::Named)`。它排除「字典/参数的具名键」位置（如 `(pa|: 23)` 里的 `pa`），因为这种位置的 `Ident` 是键名、不是变量，不该被当代码变量补全。源码注释也写明了这一点。

#### 4.3.3 源码精读

[complete.rs:834-875 —— complete_code 全函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L834-L875) 看 ①Hash 分支：

```rust
// Start of embedded code in markup or math: "[#|]", "$#|$".
// (if not in markup or math, the kind would be an `Error`).
if ctx.leaf.kind() == SyntaxKind::Hash {
    ctx.from = ctx.cursor;
    code_completions(ctx, true);
    return true;
}
```

注释点出一个重要事实：`#` 只有在正文/公式里嵌入代码时才是合法的 `Hash` 节点；如果它出现在纯代码块里，解析器会把它标成 `Error`，于是不会命中本分支。这里传 `hash=true`。

看 ②Ident 分支的守卫：

```rust
// An existing identifier: "{ pa| }".
// Ignores named pair keys as they are not variables (as in "(pa|: 23)").
if ctx.leaf.kind() == SyntaxKind::Ident
    && (ctx.leaf.index() > 0 || ctx.leaf.parent_kind() != Some(SyntaxKind::Named))
{
    ctx.from = ctx.leaf.offset();
    code_completions(ctx, false);
    return true;
}
```

`ctx.leaf.index()` 是该节点在父节点子序列中的下标。`(pa: 23)` 里键 `pa` 的 `index()==0` 且父是 `Named`，两个条件叠加把它排除掉。

看 ③explicit 分支的「位置白名单」：

```rust
if ctx.explicit
    && ctx.leaf.parent_kind() != Some(SyntaxKind::Dict)
    && (ctx.leaf.kind().is_trivia()
        || matches!(
            ctx.leaf.kind(),
            SyntaxKind::LeftParen | SyntaxKind::LeftBrace
                | SyntaxKind::Comma | SyntaxKind::Colon
        ))
{
    ctx.from = ctx.cursor;
    code_completions(ctx, false);
    return true;
}
```

它要求光标落在「一个表达式可以开始的边界」——空白/注释（trivia）、左括号、左花括号、逗号、冒号之后，并且不在字典键上下文（`Dict` 父节点）。`(1,|)`、`(a:|)`、`{|}` 都满足，但 `(pa: |,)` 这种「具名值位置且属于字典」被排除。

`hash` 参数如何影响 `code_completions` 内部，见 4.4.3 的 `scope_completions` 调用。有一个现成测试印证 Hash 分支：

[complete.rs:1643-1648 —— test_autocomplete_hash_expr](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1643-L1648) `test("#", -1)` 与 `test("$#$", -2)` 都 `must_include(["int", "if conditional"])`——`int` 是 `library.global` 里的常量（代码模式作用域），`if conditional` 是 `code_completions` 的 snippet。这两行说明 `#` 后走的是 Code 模式的 Hash 分支。

#### 4.3.4 代码实践

**实践目标**：体会 ②分支对「具名键」的排除。

1. 想象两段输入：`{ pa| }`（代码块里的标识符）与 `( pa|: 23 )`（字典具名键）。
2. 对照守卫 `ctx.leaf.index() > 0 || ctx.leaf.parent_kind() != Some(SyntaxKind::Named)` 分别判断是否命中。
3. `{ pa| }`：父节点不是 `Named`，条件成立 → 补代码作用域。`( pa|: 23 )`：`index()==0` 且父是 `Named`，条件不成立 → 不补。

**预期结果**：`{ pa| }` 弹出以 `pa` 过滤的作用域候选；`( pa|: 23 )` 不弹（键名位置由参数补全 `complete_params` 等负责）。本判断基于源码逻辑，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习**：`hash=true` 时为什么要同时过滤 `Color / Dir / Alignment` 三类？只过滤 `Color` 不行吗？
**答案**：因为正文里 `#` 之后直接插入「方向」「对齐」这类值同样意义不大且会刷屏——它们通常作为函数参数（如 `#align(center, ...)`）才有用，而不是作为独立的 `#center` 表达式。三类一起过滤是为了让正文补全菜单更贴近「正文里真正会直接写的表达式」。注意这个过滤只针对 `hash=true`（紧跟 `#`）；纯代码块 `{ | }` 里（`hash=false`）依然会列出它们。

### 4.4 markup/math/code_completions —— snippet 工厂与作用域补全

#### 4.4.1 概念说明

三个 `*_completions` 函数是真正的「内容生产者」：它们负责往 `ctx.completions` 里塞东西。结构都是「先（可选）补一次作用域 `scope_completions`，再追加若干固定 `snippet_completion`」。区别在于各自补的是本模式的「典型写法」：

- `markup_completions`：**只**产出 16 个 snippet，**不**补作用域（正文里直接弹变量名意义不大）。
- `math_completions`：先 `scope_completions(true, |_| true)`（数学符号 + 用户变量），再追加 3 个 snippet（下标、上标、分式）。
- `code_completions(ctx, hash)`：先 `scope_completions`（按 `hash` 决定是否过滤），再追加约 22 个 snippet（`hash=true` 时少一个「function」片段）。

注意它们都**不返回值**（返回 `()`），全部以副作用写 `ctx.completions`；返回 `bool` 的工作交给了外层的 `complete_*` 分派函数。

#### 4.4.2 核心流程

```
markup_completions(ctx):  连续 16 次 snippet_completion(...)   [无 scope]

math_completions(ctx):
  scope_completions(parens=true, |_| true)     [数学符号 + 变量]
  snippet "subscript"  "${x}_${2:2}"
  snippet "superscript" "${x}^${2:2}"
  snippet "fraction"   "${x}/${y}"

code_completions(ctx, hash):
  scope_completions(parens=true,
                    若 hash → 过滤掉 Color/Dir/Alignment，否则全放行)
  snippet "function call" / "code block" / ... 约 22 个
  if !hash { snippet "function" "(${params}) => ${output}" }
```

#### 4.4.3 源码精读

先看 `code_completions` 的开头，它揭示了 `hash` 的全部作用：

[complete.rs:879-891 —— code_completions 的 scope_completions 分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L879-L891)

```rust
fn code_completions(ctx: &mut CompletionContext, hash: bool) {
    if hash {
        ctx.scope_completions(true, |value| {
            // If we are in markup, ignore colors, directions, and alignments.
            // They are useless and bloat the autocomplete results.
            let ty = value.ty();
            ty != Type::of::<Color>()
                && ty != Type::of::<Dir>()
                && ty != Type::of::<Alignment>()
        });
    } else {
        ctx.scope_completions(true, |_| true);
    }
    // …… 接下来是约 22 个 snippet_completion ……
```

注释原文写明了设计意图：在正文里这些值「useless and bloat the autocomplete results」。`scope_completions` 的签名是 `(parens: bool, filter: impl Fn(&Value) -> bool)`——`parens=true` 让函数值自动补括号；`filter` 决定哪些值入选（并经 `check_value_recursively` 放行容器）。

接着是 ~22 个 snippet，这里只引两段代表（完整列表见 4.4.4 的实践）：

[complete.rs:893-945 —— code_completions 的部分 snippet](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L893-L945) 包括 `function call`、`code block`、`content block`、`set rule`、`show rule`、`context expression`、`let binding`、`let binding (function)` 等。片段里大量使用 `${名称}` 占位符与 `\n\t` 换行缩进，例如 `if conditional` 的 apply 是 `"if ${1 < 2} {\n\t${}\n}"`。

再看末尾条件 snippet：

[complete.rs:1025-1031 —— code_completions 末尾的 function 片段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1025-L1031) 这段被 `if !hash` 包住，确认「匿名函数 `(${params}) => ${output}`」只在纯代码上下文（`hash=false`）出现。

再看 `math_completions`：

[complete.rs:811-831 —— math_completions 全函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L811-L831) 第一行 `ctx.scope_completions(true, |_| true)` 放行所有值；随后三个 snippet 的 apply 分别是 `${x}_${2:2}`、`${x}^${2:2}`、`${x}/${y}`。注意占位符 `${2:2}` 表示「第 2 个 tab stop、默认文本为 `2`」。

`markup_completions` 在 4.4.4 作为实践重点，源码见 [complete.rs:690-786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L690-L786)，结构是连续 16 次 `ctx.snippet_completion(label, apply, detail)`，没有 `scope_completions`。

顺带确认 `snippet_completion` 本身：

[complete.rs:1105-1117 —— snippet_completion 方法](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1105-L1117) 它固定产出 `kind = CompletionKind::Syntax`，把传入的 `snippet` 原样放进 `apply`、`docs` 放进 `detail`。

#### 4.4.4 代码实践

**实践目标**：阅读 `markup_completions`，整理出 explicit 触发时正文会得到的全部 snippet（这正是本讲主实践的前半部分）。

1. 打开 [complete.rs:690-786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L690-L786)。
2. 逐条抄录每个 `snippet_completion` 的 `(label, apply, detail)`，共 16 条（完整清单见本讲第 5 节综合实践的答案表）。
3. 自检：这 16 条的 `apply` 是否都含 `${}` 占位符？并与 `code_completions` 里的纯字面量片段（如 `break` / `continue`，apply 就是 `break`）对比。

**预期结果**：`markup_completions` 的 16 条**全部**含占位符（如 `expression` 的 `#${}`、`code listing` 的 ```` ```${lang}\n${code}\n``` ````），没有一条是纯字面量。这与 `code_completions` 形成对比——后者既有带占位符的片段，也有 `break` / `continue` 这种纯字面量片段。原因是正文 snippet 都需要用户进一步填入内容（标题文本、列表项……），而 `break`/`continue` 这类关键字选中即完整。

#### 4.4.5 小练习与答案

**练习 1**：`math_completions` 里 `scope_completions(true, |_| true)` 的 `true`（`parens`）对数学补全意味着什么？
**答案**：意味着若作用域里有「函数」类型的值，会自动带上括号插入。不过在数学模式里，大多数候选是符号/常量而非函数，所以这个 `parens=true` 主要影响少数函数型候选的 apply 形态。

**练习 2**：`code_completions` 的 snippet 列表里，哪些片段明显只对「代码块内部」有意义、不适合正文 `#` 之后？举例说明 `hash` 标志如何配合。
**答案**：例如 `return`（`return ${output}`）、`break`、`continue`、`while loop`、`for loop` 这类只在函数/循环体里有意义的片段。它们并未被 `hash` 单独剔除（snippet 列表对 `hash` 只差一个「function」），但因为 `hash=true` 走的是 `scope_completions` 过滤 + 同样的 snippet，所以正文 `#` 后也会出现这些片段——这是 best-effort 设计：与其精确区分，不如统一给全列表，让用户自己挑。

### 4.5 raw_completions —— raw 块语言标签补全

#### 4.5.1 概念说明

正文里写 ```` ``` ```` 开始一个 raw 块，紧跟着的「语言标签」（如 ```` ```rust ````）决定语法高亮。`raw_completions` 负责在「刚敲完 ```` ``` ```` 正要写标签」时弹出可用语言列表。

这里有两个值得注意的点：

1. **触发判定在 `complete_markup` 里、用文本扫描完成**（而不是靠语法树节点类型），因为标签区处于「raw 块刚打开、还没闭合」的半成品状态，语法树未必稳定。
2. **数据来源是 `RawElem::languages()`**——这是 `typst` 库（`typst::text::RawElem`）提供的一个函数，枚举所有可用于 raw 块高亮的语法及其标签别名。typst-ide 只是它的消费方。

#### 4.5.2 核心流程

触发判定（位于 `complete_markup`，[complete.rs:662-676](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L662-L676)）：

```
s = Scanner(text); s.jump(leaf.offset())
if s.eat_if("```"):          # 光标正好在 ``` 之后
    s.eat_while('`')          # 吃掉多余的反引号（``````）
    start = s.cursor()         # 标签起点
    if s.eat_if(is_id_start): s.eat_while(is_id_continue)  # 吃掉已打的标签
    if s.cursor() == cursor:   # 没有别的内容夹在中间
        from = start
        raw_completions()
    return true
```

候选生成（`raw_completions` 方法本身）：

```
raw_completions():
  for (name, mut tags) in RawElem::languages():
      lower = name.to_lowercase()
      if !tags 已含 lower: tags.push(lower)   # 语言名小写也算合法标签
      tags.retain(|t| is_ident(t))            # 丢掉含 `-`/数字开头等非标识符的标签
      if tags 为空: continue
      emit Completion{
          kind  = Constant,
          label = name,                        # 菜单显示规范名，如 "Rust"
          apply = tags[0],                     # 选中后写入第一个合法标签
          detail= "X or Y or Z"                # 全部标签用 or 连接
      }
```

#### 4.5.3 源码精读

先看触发判定，它用 `unscanning::Scanner` 做局部文本扫描：

[complete.rs:662-676 —— complete_markup 里的 raw 块判定](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L662-L676)

```rust
// Directly after a raw block.
let mut s = Scanner::new(ctx.text);
s.jump(ctx.leaf.offset());
if s.eat_if("```") {
    s.eat_while('`');
    let start = s.cursor();
    if s.eat_if(is_id_start) {
        s.eat_while(is_id_continue);
    }
    if s.cursor() == ctx.cursor {
        ctx.from = start;
        ctx.raw_completions();
    }
    return true;
}
```

`is_id_start` / `is_id_continue` 是 `typst::syntax` 导出的字符分类函数（标识符首字符 / 后续字符），和 `is_ident` 一起在文件顶部导入（见 [complete.rs:13-16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L13-L16)）。`s.cursor() == ctx.cursor` 这一句保证「反引号与光标之间只可能有（正在输入的）标签字符」，避免在 raw 正文中间误触发。

再看候选生成：

[complete.rs:1194-1213 —— raw_completions 方法](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1194-L1213)

```rust
fn raw_completions(&mut self) {
    for (name, mut tags) in RawElem::languages() {
        let lower = name.to_lowercase();
        if !tags.contains(&lower.as_str()) {
            tags.push(lower.as_str());
        }

        tags.retain(|tag| is_ident(tag));
        if tags.is_empty() {
            continue;
        }

        self.completions.push(Completion {
            kind: CompletionKind::Constant,
            label: name.into(),
            apply: Some(tags[0].into()),
            detail: Some(repr::separated_list(&tags, " or ").into()),
        });
    }
}
```

几个细节：

- `RawElem::languages()` 由 [complete.rs:17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L17) 的 `use typst::text::{FontFlags, RawElem};` 引入，定义在 **`typst` 库的 `text` 模块**（不在 typst-ide 内）。从 typst-ide 的视角看，它逐个产出 `(name, tags)`：`name` 是语言的规范显示名（如 `"Rust"`），`tags` 是能选中该语言的一组标签字符串（如 `["rs", "rust"]` 之类）。该函数的确切内部实现（具体支持哪些语言、顺序如何）属于 `typst` 库，本讲不展开。
- `kind` 用的是 `CompletionKind::Constant`（而 snippet 用 `Syntax`），因为语言标签更像一个「常量值」而非语法结构。
- `apply` 取 `tags[0]`——即第一个合法标签，通常是更简短的别名；`label` 用规范名便于阅读。
- `detail` 用 `repr::separated_list(&tags, " or ")` 把全部标签拼成 `"rs or rust"` 这种英文列举，告诉用户「这些写法都行」。
- `tags.retain(|tag| is_ident(tag))` 把含连字符、点号或数字开头等不符合 Typst 标识符规则的标签剔除，保证 `apply` 写出来的一定是合法的 raw 标签。

> 说明：仓库近期提交（如 `ad8e9bcec` “Filter and reorder raw syntaxes available to IDE and docs”、`9a1d84e94` “Print list of raw syntaxes in docs”）表明 `RawElem::languages()` 返回的语言集合与排序是在 `typst` 库侧维护并可能调整的，typst-ide 只是消费它。因此「具体弹出的语言清单」**待本地验证**（可运行下方实践查看实际输出）。

#### 4.5.4 代码实践

**实践目标**：亲手触发 raw 块标签补全，观察 `raw_completions` 产出的候选结构。

1. 构造一个最小 world（仿照 u1-l3 的 `TestWorld`），主源码设为 ` ``` `（三个反引号，光标置其后，等价于 `test("```", -1)` 但需 explicit？注意 raw 触发**不依赖 `explicit`**，见 4.5.3 的判定无 `explicit` 检查）。
2. 调用 `autocomplete(&world, output, &source, cursor, false)`。
3. 打印返回的 `completions`，检查每条的 `label`（规范名）、`apply`（首个合法标签）、`detail`（`X or Y`）。

**预期结果**：得到一组 `kind=Constant` 的候选，每条对应一种高亮语言；例如名为 `Rust` 的那条，`apply` 是它的某个标签、`detail` 列出全部等价标签。具体语言清单随 `typst` 库版本变化，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 raw 块的触发判定用 `Scanner` 扫描文本，而不是像引用那样靠 `ctx.leaf.kind()`？
**答案**：因为 ```` ``` ```` 之后、标签未写完时，这一段在语法树里处于「raw 块已开始但未闭合」的半成品/不稳定状态，对应的节点类型可能随输入而变；而 ```` ``` ```` 这三个字符本身在源码文本里是稳定可识别的，用 `Scanner` 直接吃文本更可靠。引用 `@` 则对应稳定的 `RefMarker` 节点，所以可以用节点类型判定。

**练习 2**：`raw_completions` 为什么主动把 `name.to_lowercase()` 加进 `tags`？
**答案**：因为 Typst 对 raw 标签是大小写不敏感的——写 ```` ```Rust ```` 和 ```` ```rust ```` 通常都能识别。把规范名的小写形式补进标签集合，能让 `detail`（全部标签列表）如实反映「这个语言名本身（小写）也能用」，并在某些情况下成为 `apply` 的备选。

## 5. 综合实践

把本讲主要内容串起来：完成规格里要求的两件事——**列出 markup 模式显式触发的全部 snippet**，并**解释 raw 块标签补全的数据来源**。

### 任务一：列出 explicit=true 时 markup 模式补的全部 snippet

依据 [complete.rs:690-786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L690-L786) 的 `markup_completions`，当 `complete_markup` 走到第 ⑥显式分支（`explicit=true`，且前面 ①–⑤都没命中），会调用它，产出下表 16 条 `kind=Syntax` 的候选：

| label | apply（snippet） | detail |
| --- | --- | --- |
| expression | `#${}` | Variables, function calls, blocks, and more. |
| linebreak | `\\\n${}` | Inserts a forced linebreak. |
| strong text | `*${strong}*` | Strongly emphasizes content by increasing the font weight. |
| emphasized text | `_${emphasized}_` | Emphasizes content by setting it in italic font style. |
| raw text | `` `${text}` `` | Displays text verbatim, in monospace. |
| code listing | `` ```${lang}\n${code}\n``` `` | Inserts computer code with syntax highlighting. |
| hyperlink | `https://${example.com}` | Links to a URL. |
| label | `<${name}>` | Makes the preceding element referenceable. |
| reference | `@${name}` | Inserts a reference to a label. |
| heading | `= ${title}` | Inserts a section heading. |
| list item | `- ${item}` | Inserts an item of a bullet list. |
| enumeration item | `+ ${item}` | Inserts an item of a numbered list. |
| enumeration item (numbered) | `${number}. ${item}` | Inserts an explicitly numbered list item. |
| term list item | `/ ${term}: ${description}` | Inserts an item of a term list. |
| math (inline) | `$${x}$` | Inserts an inline-level mathematical equation. |
| math (block) | `$ ${sum_x^2} $` | Inserts a block-level mathematical equation. |

注意两点：① 这 16 条**全部是 snippet，不含作用域补全**（`markup_completions` 没有调 `scope_completions`）；② 它们**只在 `explicit=true` 时出现**，平时正文不会被动弹出。

### 任务二：解释 raw 块标签补全的数据来源

依据 [complete.rs:1194-1213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1194-L1213)：

- 数据来源是 `RawElem::languages()`，由 [complete.rs:17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L17) 从 `typst::text` 导入。它是 `typst` 库侧的函数，**定义在 typst-ide 之外**，逐个产出 `(name, tags)`——`name` 是语言规范名、`tags` 是一组等价标签。
- typst-ide 对每条做三步加工：把语言名小写也加进标签集（Typst 标签大小写不敏感）、用 `is_ident` 过滤掉非法标签、再压成一条 `Completion`（`label`=规范名、`apply`=首个合法标签、`detail`=`repr::separated_list(&tags, " or ")`）。
- 触发由 [complete.rs:662-676](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L662-L676) 的 `Scanner` 文本扫描完成，**不依赖 `explicit`**：只要光标紧跟 ```` ``` ````（及其后只可能有正在输入的标签字符）就触发。
- 因为语言集合在 `typst` 库侧维护（近期还有相关提交调整），具体清单**待本地验证**。

### 串接小结

这条链完整地把本讲的三个模式、snippet 工厂、raw 补全串了起来：用户在正文按 Ctrl+Space（`explicit=true`）→ `autocomplete` 走到链尾 `match mode` → `complete_markup` 第 ⑥分支 → `markup_completions` 产出 16 条 snippet；用户敲 ```` ``` ```` → `complete_markup` 第 ⑤分支（文本扫描）→ `raw_completions` 消费 `RawElem::languages()` 产出语言标签。

## 6. 本讲小结

- 三个模式函数 `complete_markup / complete_math / complete_code` 位于短路分发链的**最末**，是「兜底通用补全」；它们开头都有一道 `debug_assert_eq!(ctx.leaf.mode_after(), Some(...))` 与分发链互为校验。
- `complete_markup` 识别六类触发（`@` 引用起点、已有 `RefMarker`、半成品 `let`、`context` 块、```` ``` ```` raw 块、显式触发），其中 `let`/`context` 会**跨模式桥接**到 `code_completions(hash=false)`；显式分支才弹 `markup_completions` 的 16 个 snippet。
- `complete_math` 最简单，只识别「已有原子/标识符」与「显式触发」两类，依赖 `globals` 在数学模式返回 `library.math` 来自动补数学符号。
- `complete_code` 有三种分支（`Hash` / `Ident` / `explicit`），其 `hash` 参数同时影响 `scope_completions` 的过滤（`hash=true` 屏蔽 `Color/Dir/Alignment`）与「function」snippet 的取舍；它还排除「具名键」与「字典值」位置。
- 三个 `*_completions` 是内容生产者：`markup` 只产 snippet、`math` 与 `code` 先 `scope_completions` 再追加 snippet；它们都以副作用写 `ctx.completions`、返回 `()`。
- `raw_completions` 的数据来自 `typst::text::RawElem::languages()`，typst-ide 对其 `(name, tags)` 做小写补全、`is_ident` 过滤后压成 `kind=Constant` 的候选；触发用 `Scanner` 文本扫描、不依赖 `explicit`。

## 7. 下一步学习建议

本讲讲的是「按模式兜底的通用补全」，其内部反复调用的 `scope_completions` 与类型驱动补全尚未展开。建议按以下顺序继续：

1. **u6-l4（scope_completions 与类型驱动补全）**：深入 [complete.rs:1426-1462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1426-L1462) 的 `scope_completions`，弄清「局部命名优先于全局」的合并、`defined` 去重，以及 `cast_completions` 如何按 `CastInfo` 递归展开类型补全。
2. **u6-l1（set/show 规则与函数参数补全）**：本讲刻意把 `complete_rules`、`complete_params` 当作「更靠前的专项补全」一笔带过，u6-l1 会展开它们如何回溯到 `deciding` 节点判断补参数还是补值。
3. **u6-l3（import、路径、包、字体、标签补全）**：本讲提到的 `label_completions`（依赖 `output` 与 `analyze_labels` 的 `split`）和 `RawElem::languages()` 的包/字体同类补全，会在 u6-l3 系统讲解。
4. **动手验证**：仿照 u8-l1 的测试风格，为本讲任何一个分支补一个 `must_include` / `must_exclude` 断言，跑 `cargo test -p typst-ide`，把「待本地验证」的地方落实。
