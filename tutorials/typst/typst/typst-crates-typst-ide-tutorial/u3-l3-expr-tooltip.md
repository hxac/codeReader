# 表达式与函数调用的 tooltip

## 1. 本讲目标

悬停提示的分发链（u3-l2）把光标交给最匹配的分支后，**最通用、最昂贵**的那个分支——`expr_tooltip`——负责回答「光标所在的表达式，运行起来到底等于什么？」。当你在 `#let x = 1 + 2` 的 `x` 上悬停，弹出的 `3`；在 `#box()` 的 `(` 上悬停，弹出的 `box()`；在 `$pi.alt$` 上悬停，弹出的 `symbol("ϖ")`——全是它干的话。

本讲只回答一个问题：**`expr_tooltip` 是怎么把一个语法节点变成一串候选值，再变成一行提示的？**

学完本讲你应当能够：

- 说清 `expr_tooltip` 的三条出口路径（单值→文档/长度换算；字面量→放弃；多值→合并列表），以及它们**为什么是这个先后顺序**。
- 解释「可分析门槛」`expr.hash() || 匹配数学变量访问` 的设计权衡：为什么只分析可哈希的代码表达式或数学变量访问，而不是所有节点。
- 画出 `analyze_expr` 的取值流程：字面量直接构造（便宜）、字段回溯、复杂表达式回退到 `trace`（重新求值整篇文档）。
- 掌握多值场景下「合并连续重复值并标注 `×N`」「`MAX_VALUES` 截断与结尾 `...`」的展示逻辑。
- 理解 `length_tooltip` 把绝对长度（pt）换算成 mm/cm/in 的展示，以及为什么 `1em` 反而没有这个提示。

## 2. 前置知识

本讲承接以下已建立的概念（若生疏请先回顾）：

- **u2-l4（analyze）**：`analyze_expr` 的返回类型是 `EcoVec<(Value, Option<Styles>)>`——**同一个 span 可能求值多次、得到多个值**；字面量直接构造 `Value`（轻），复杂表达式回退到 `typst::trace`，后者会用 `Traced::new(span)` 标记目标 span 后**把整篇文档重新求值一遍**，求值器命中该 span 时把值连同活动样式存进 `Sink`。这正是本讲「候选值从哪来」的来源。
- **u3-l1（docs）**：`find_value_docs(world, value)` 按 `Value::docs()`（仅原生 Func/Type 命中）→用户闭包源码注释两级回退，命中后用 `Docs::summary()` 给出「第一段第一句」纯文本。本讲会看到它被 `expr_tooltip` 在「单值路径」里优先调用。
- **u3-l2（tooltip 分发）**：`expr_tooltip` 是 `or_else` 短路链的倒数第二个分支，签名 `fn(&dyn IdeWorld, &LinkedNode) -> Option<Tooltip>`；`Tooltip` 只有 `Text`（散文/文档）与 `Code`（值的代码表示）两类。

一个贯穿本讲的直觉：**悬停提示是「值导向」的**。`expr_tooltip` 不在乎你写的是变量名、函数调用还是字段访问，它只想知道「这玩意儿运行时是什么值」，再把值的代码表示（`value.repr()`）或文档摆给你看。

> 比喻：`expr_tooltip` 像一个会「现场试运行」的导览员。对于一眼能看穿的常量（字面量），它懒得开口；对于需要真跑一遍才知道结果的式子，它就借用 `trace` 这台「迷你编译器」悄悄跑一次，把结果抄在卡片上递给你。

## 3. 本讲源码地图

本讲以 `tooltip.rs` 中的 `expr_tooltip` 为主线，`analyze.rs` 提供「取值」后勤，再下沉到 typst 主 crate 与 typst-library 的几处底层定义。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/tooltip.rs` | 悬停提示实现 | `expr_tooltip`（L53–L122）、`length_tooltip`（L174–L184）、`Tooltip` 枚举（L45–L51） |
| `src/analyze.rs` | 表达式取值推断 | `analyze_expr`（L12–L50） |
| `crates/typst/src/lib.rs` | 编译器入口 | `trace`（L84–L95）：标记 span 后重跑文档、收尾返回 `sink.values()` |
| `crates/typst-library/src/engine.rs` | 编译期收集器 | `Sink`（L152–L167）、`MAX_VALUES`（L171）、`value()`（L230–L235）、`values()`（L194–L196） |
| `crates/typst-library/src/foundations/repr.rs` | 值的代码表示与列表格式化 | `pretty_comma_list`（L173–L198）、`Repr` trait（L49–L53） |
| `crates/typst-syntax/src/ast.rs` | 表达式分类 | `Expr::hash()`（L531–L564）、`Expr::is_literal()`（L566–L578） |
| `crates/typst-eval/src/vm.rs` | 求值器记录被追踪值 | `trace_at`/`trace`（L78–L91）、`inspected` 字段（L23–L25） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `expr_tooltip` 全景与「可分析」门槛；② `analyze_expr` 如何产出候选值；③ 单值路径——文档与长度换算（`length_tooltip`）；④ 多值路径——`×N` 合并与 `pretty_comma_list`、`Sink::MAX_VALUES` 截断。

### 4.1 expr_tooltip 全景与「可分析」门槛

#### 4.1.1 概念说明

`expr_tooltip` 拿到的只是一个叶子 `LinkedNode`（比如光标落在某个标识符、括号、点号上）。它要先**沿祖先链向上找到第一个真正的表达式节点**，再决定要不要分析、怎么分析。

这里有一个关键的「门槛」设计：**并不是所有表达式都值得分析**。注释写得很直白——「我们只分析可嵌入代码的表达式，或访问变量的数学表达式」。原因有二：

1. **成本**：复杂表达式的取值要走 `trace`，意味着**重新编译整篇文档**（u2-l4）。对悬停这种高频操作，必须尽量收窄触发面。
2. **信噪比**：数学字面量（`2`、`'`、`^`、`!=`）一目了然，悬停它们没有意义；运算符、关系符也没有「值」可展示。只有变量访问、函数调用这类「不跑就不知道结果」的节点才值得开口。

这个门槛用一个布尔量 `analyze` 表达：`expr.hash() || matches!(expr, MathIdent | MathFieldAccess | MathCall)`。

- `expr.hash()` 并非「计算哈希」，而是 [ast.rs:L531-L564](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L531-L564) 定义的方法，含义是「**该表达式能否用 `#` 嵌入到 markup 中**」——即 `Ident`、`FuncCall`、`FieldAccess`、`CodeBlock`、`LetBinding` 等合法的「`#`-表达式」。
- 后半句补上数学模式下的变量访问（`MathIdent`/`MathFieldAccess`/`MathCall`），让 `$pi$`、`$pi.alt$`、`$sin()$` 这类数学变量也能被分析。

#### 4.1.2 核心流程

```
expr_tooltip(world, leaf)
  │
  ├─ 1. 沿祖先向上找首个 ast::Expr 节点（leaf 本身可能不是 Expr）
  │        ancestor = leaf; while !ancestor.is::<ast::Expr>() { ancestor = parent()? }
  │        ── 找不到 → 返回 None
  │
  ├─ 2. 门槛：analyze = expr.hash() || 是数学变量访问
  │        ── analyze == false → 返回 None（不分析）
  │
  ├─ 3. values = analyze_expr(world, ancestor)   ← 取候选值（见 4.2）
  │
  ├─ 4. 【单值路径】若所有候选值都相等：
  │        ├─ find_value_docs 命中 → Text(summary)        （见 4.3）
  │        └─ 值是绝对长度 → length_tooltip → Code(...)    （见 4.3）
  │        （两条都不命中则「漏」到第 5 步，不在这里 return）
  │
  ├─ 5. if expr.is_literal() { return None; }     ← 字面量不值得提示
  │
  ├─ 6. 【多值路径】取前 MAX_VALUES-1 个值，合并连续重复(×N)，结尾可能加 "..."
  │        └─ pretty_comma_list → Code(...)        （见 4.4）
  │
  └─ 7. 空串 → None
```

最容易被忽略、却最关键的一点：**第 4 步的「单值路径」并不 `return` 收尾**——如果既没命中文档、又不是长度，它会**漏到第 5、6 步**，最终走多值列表渲染出 `value.repr()`。这就解释了为什么 `#let x = 1+2` 的 `x`（单值 `3`、无文档、非长度、非字面量）最终给出的是 `Code("3")`。

#### 4.1.3 源码精读

完整函数见 [tooltip.rs:L53-L122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L53-L122)。门槛与祖先攀升部分：

```rust
fn expr_tooltip(world: &dyn IdeWorld, leaf: &LinkedNode) -> Option<Tooltip> {
    let mut ancestor = leaf;
    while !ancestor.is::<ast::Expr>() {
        ancestor = ancestor.parent()?;
    }

    let expr = ancestor.cast::<ast::Expr>()?;
    // We only analyze embeddable code expressions or math expressions that
    // access variables.
    let analyze = expr.hash()
        || matches!(
            expr,
            ast::Expr::MathIdent(_)
                | ast::Expr::MathFieldAccess(_)
                | ast::Expr::MathCall(_)
        );
    if !analyze {
        return None;
    }

    let values = analyze_expr(world, ancestor);
    // ... 单值路径 / 字面量 / 多值路径（见 4.3、4.4）
}
```

- `ancestor.is::<ast::Expr>()` / `ancestor.cast::<ast::Expr>()`：在无类型 `SyntaxNode` 上做带类型视图转换（u2-l2 已介绍 `ast::Expr` 是强类型视图）。叶子可能本身不是 `Expr`（如括号 `(` 是 `LeftParen`），需要向上找到第一个能 cast 成 `Expr` 的祖先——例如整个 `FuncCall`。
- `expr.hash()`：即「能否 `#`-嵌入」，定义在 [ast.rs:L533-L564](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L533-L564)，覆盖 `Ident`/`FuncCall`/`FieldAccess`/`SetRule`/`ForLoop` 等绝大多数「有意义的」代码表达式。
- 门槛把数学字面量、运算符、关系符等一律挡在门外，这正是 u3-l2 引用的 `test_tooltip_math_literals` 里 `$x'^2 &!= ...$` 各字面量位置全部 `must_be_none` 的根因。

#### 4.1.4 代码实践

**实践目标**：用门槛规则预测「哪些光标位置会进入分析、哪些直接放弃」。

**操作步骤**：阅读 [tooltip.rs:L344-L365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L344-L365) 的 `test_tooltip_math_literals`，片段为 `$x'^2 &!= \u{3C0} "is" pi #true$`。

**需要观察的现象**：把每个光标位置、对应表达式类别、是否通过门槛填入下表（`pi` 处已填好）：

| 光标 | 字符 | 表达式类别 | `hash()`?/数学变量? | 门槛 | 断言 |
| --- | --- | --- | --- | --- | --- |
| 4 | `2` | 数学字面量 | 否/否 | 拦截 | `must_be_none` |
| 24 | `pi` | `MathIdent` | —/是 | 通过 | `must_be_code("symbol(\"π\", (\"alt\", \"ϖ\"))")` |
| 28 | `true` | 数学内的 `#true` | `Bool.hash()`=是 | 通过 | （但字面量→见 4.3） |

**预期结果**：`2`、`'`、`^`、`&`、`!=`、`\`、`u{...}`、`"is"` 这些都被门槛拦下；只有 `pi`（MathIdent）通过。注意第 28 位的 `#true`：它是 `Bool` 字面量、`hash()` 返回真，**通过了门槛**，但随后会在第 5 步因 `is_literal()` 而返回 `None`（测试也断言 `must_be_none`）——门槛只负责「值不值得启动分析」，字面量那道关留给第 5 步。

> 本实践为源码阅读型，对照测试即可，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`#let x = 1 + 2` 把光标放在 `x`（偏移 5）上得到 `Code("3")`。如果把光标直接放在表达式里的 `2` 上，为什么是 `None`？

**答案**：光标在 `2` 上，叶子是 `Int` 字面量，其本身就是 `ast::Expr`。它 `hash()` 为真（`Int` 在 `hash()` 列表里），通过门槛；`analyze_expr` 直接得到 `[2]`。单值路径：`find_value_docs(Int 2)`→None、非长度→漏下；第 5 步 `expr.is_literal()` 为真（`Int` 是字面量，见 [ast.rs:L567-L578](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L567-L578)）→ `return None`。所以代码模式的纯字面量没有提示。

> 顺带澄清 [tooltip.rs:L338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L338) 的 `test("#let x = 1 + 2", -1, Side::After).must_be_none()`：负数光标 `-1` 解析为 `len + (-1) + 1 = len`，即**字符串末尾之后**的位置（`2` 之后的间隙），`leaf_at` 在越界处返回 `None`，于是连分发都不进入——这和「字面量短路」是两回事，本题讨论的是「光标确实落在 `2` 上」的情形。

**练习 2**：为什么门槛用 `expr.hash()`（能否 `#`-嵌入）作为「代码表达式是否值得分析」的判据，而不是简单地「只要是 `ast::Expr` 就分析」？

**答案**：两个原因。(1) **收窄成本**：`hash()` 列表排除了大量「无值可显示」或「不能独立成表达式」的节点，避免对它们触发昂贵的 `trace`。(2) **语义对齐**：`hash()` 恰好刻画了「能出现在 `#` 后、会产生一个可观察值」的表达式集合，与「悬停想看值」的意图天然吻合。把判据复用这个既有方法，而不是新写一份白名单，既减少重复又保证与语言定义同步。

### 4.2 analyze_expr —— 候选值从哪来

#### 4.2.1 概念说明

`expr_tooltip` 第 3 步调用的 `analyze_expr`（u2-l4 已总览）是「取值」的核心。本讲从 `expr_tooltip` 的视角再聚焦它的三条取值路径，因为它们直接决定了提示的**内容**与**代价**：

1. **字面量直接构造**：`None`/`Auto`/`Bool`/`Int`/`Float`/`Numeric`/`Str` 在语法阶段就能拿到值，无需运行，直接 `eco_vec![(val, None)]`。最便宜，且 `Styles` 恒为 `None`。
2. **`Contextual` 与字段回溯**：`context x` 会递归分析子表达式；若节点是 `FieldAccess`/`MathFieldAccess` 的**字段部分**（`index() > 0`，即点号右边的名字），则回溯去分析整个父字段访问——这就是为什么悬停 `$pi.alt$` 的 `alt` 能得到 `symbol("ϖ")`（整个 `pi.alt` 的值），而不是 `alt` 本身。
3. **`trace` 回退**：其余所有情况（变量、函数调用、运算……）都交给 `typst::trace`——**标记 span、重新求值整篇文档、收集该 span 见过的所有值**。最贵，但能拿到 `set` 规则、`context`、运行时绑定之后的真实值。

#### 4.2.2 核心流程

```
analyze_expr(world, node) -> EcoVec<(Value, Option<Styles>)>
  │
  ├─ node.cast::<ast::Expr>()? 失败 → 返回空 vec
  │
  ├─ 匹配字面量分支：None/Auto/Bool/Int/Float/Numeric/Str → 直接构造 Value（Styles=None）
  │
  └─ 其余（_ 分支）：
       ├─ 若是 Contextual → 递归 analyze_expr(子节点)
       ├─ 若是字段访问的「字段」部分(index>0) → 递归 analyze_expr(父节点)
       └─ 否则 → typst::trace::<PagedDocument>(world.upcast(), node.span())
                   └─ 内部：Traced::new(span) + 重新编译 + sink.values()
```

`trace` 的内部见 [typst/src/lib.rs:L84-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L84-L95)：新建一个 `Sink`、用 `Traced::new(span)` 把目标 span「挂号」，跑一遍 `compile_impl`，最后 `sink.values()` 取回所有挂号期间记录的 `(Value, Option<Styles>)`。「挂号」之所以能捕获值，是因为求值器在求值每个表达式时都会调 `trace_at`：

```rust
// crates/typst-eval/src/vm.rs
pub fn trace_at(&mut self, span: Span, value: &Value) {
    if self.inspected == Some(span) {
        self.trace(value.clone());
    }
}
pub fn trace(&mut self, value: Value) {
    self.engine.sink.value(value, self.context.styles().ok().map(|s| s.to_map()));
}
```

只有当 `inspected == Some(span)`（即「正在追踪的正是这个 span」）时，才把值连同当前活动样式 `sink.value(...)` 推进收集器（[vm.rs:L78-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L78-L91)）。`inspected` 在构造 VM 时由 `engine.traced.get(id)` 决定（[vm.rs:L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L38)），且只有当追踪 span 与当前文件 id 一致时才生效——这样 comemo 只需让「含该 span 的那一个文件」失效。

#### 4.2.3 源码精读

`analyze_expr` 见 [analyze.rs:L12-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L12-L50)：

```rust
pub fn analyze_expr(world, node) -> EcoVec<(Value, Option<Styles>)> {
    let Some(expr) = node.cast::<ast::Expr>() else { return eco_vec![]; };
    let val = match expr {
        ast::Expr::None(_) => Value::None,
        ast::Expr::Bool(v) => Value::Bool(v.get()),
        ast::Expr::Int(v) if v.get().is_ok() => Value::Int(v.get().unwrap()),
        ast::Expr::Numeric(v) => Value::numeric(v.get()),
        // ... 其余字面量 ...
        _ => {
            if node.kind() == SyntaxKind::Contextual
                && let Some(child) = node.children().next_back() {
                return analyze_expr(world, &child);
            }
            if let Some(parent) = node.parent()
                && matches!(parent.kind(), FieldAccess | MathFieldAccess)
                && node.index() > 0 {
                return analyze_expr(world, parent);
            }
            return typst::trace::<PagedDocument>(world.upcast(), node.span());
        }
    };
    eco_vec![(val, None)]
}
```

两个对悬停特别重要的细节：

- **字段回溯（`node.index() > 0`）**：点号访问 `a.b` 中，`b` 是 `index()==1` 的子节点。悬停 `b` 时，若不回溯，`analyze_expr` 会把 `b` 当独立标识符去 trace，多半拿不到有意义的结果；回溯到父 `FieldAccess` 后，trace 的是整个 `a.b`，于是 `$pi.alt$` 的 `alt` 能给出 `symbol("ϖ")`。对应测试 [tooltip.rs:L367-L372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L367-L372)：`$pi.alt$` 悬停 `pi`、`.`、`alt` 分别得到整个 symbol、整个 symbol、`symbol("ϖ")`。
- **`Int(v) if v.get().is_ok()`**：整数溢出（`is_ok()` 为假）不当作字面量处理，会落到 `_` 分支——一个边角的安全处理。

#### 4.2.4 代码实践

**实践目标**：跟踪「悬停 `#std.box()` 的 `std` 得到 `<module global>`」这条取值链。

**操作步骤**：对照 [tooltip.rs:L404-L408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L404-L408) 的 `test_tooltip_function_call` 中 `#std.box()` 片段。

1. 光标在 `std`（偏移 1）：叶子 `std` 本身是 `Ident`（`Expr`），通过门槛。
2. `analyze_expr(Ident std)`：非字面量、非 Contextual、非字段部分 → 走 `trace`。
3. `trace` 重跑文档，求值器在求值 `std` 时 `trace_at(std.span, &Module(global))` → `Sink` 收到 `[(Value::Module(global), styles)]`。
4. 回到 `expr_tooltip`：单值、`find_value_docs(Module)`→None、非长度 → 漏到多值路径 → `Value::Module.repr()` = `"<module global>"` → `Code("<module global>")`。

**需要观察的现象**：取值链走的是「最贵的 `trace` 分支」，但因为只悬停一次、且 typst 有 comemo 缓存，实际开销可控。

**预期结果**：与测试断言 `must_be_code("<module global>")` 一致。

> 命令未经本地运行，可用 `cargo test -p typst-ide --lib tooltip::tests::test_tooltip_function_call` 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `analyze_expr` 对字面量「直接构造」而不是统一走 `trace`？

**答案**：字面量的值在语法/解析阶段就已确定，无需重新编译整篇文档。直接构造既避免了 `trace` 的高昂代价（重新求值全文），又保证 `Styles` 为 `None`（字面量不携带样式）。这是「能省则省」的典型 best-effort 优化，让悬停纯字面量时几乎零成本。

**练习 2**：`#{context}`（光标在末尾 `}` 前、`Side::Before`）为何得到 `Code("context()")`？参 [tooltip.rs:L476-L479](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L476-L479)。

**答案**：`context` 是 `Contextual` 节点。`analyze_expr` 命中 `_` 分支的第一条：`Contextual` 时递归分析其子节点。空的 `context` 表达式求值为一个「空的 context 内容」，其 `repr()` 恰为 `context()`。这里体现了「`Contextual` 递归」路径如何把 `context` 这种特殊语法也纳入取值。

### 4.3 单值路径——文档优先与长度换算（length_tooltip）

#### 4.3.1 概念说明

当候选值「全部相等」（含只有一个值的情况）时，`expr_tooltip` 走一条更贴心的**单值路径**，按优先级尝试两种「比裸 `repr()` 更有用」的展示：

1. **文档优先**：`find_value_docs(world, value)` 命中 → 返回 `Text(docs.summary())`。这让悬停函数名/类型名时弹出的是**文档说明**，而不是干巴巴的 `box()`。例如 `#box` 的 `box` → `Text("An inline-level container that sizes content.")`。
2. **长度换算**：若值是 `Value::Length` 且是绝对长度（`em` 分量为零），调 `length_tooltip` 把 pt 换算成 mm/cm/in，返回 `Code("10pt = 2.64mm = ...")`。

两条都不命中时，**不在这里 return**，而是漏到第 5、6 步走多值渲染。这个「漏」是有意为之：单值 `3`（无文档、非长度）就是靠漏到多值路径才渲染出 `Code("3")`。

#### 4.3.2 核心流程

```
【单值路径】 if 所有候选值都相等 (含单值):
   ├─ find_value_docs(value) 命中 → return Text(summary)
   └─ value 是 Value::Length 且 length_tooltip 返回 Some → return Code("…pt = …mm = …cm = …in")
   （均不命中 → 继续往下到字面量检查 / 多值路径）

length_tooltip(length):
   └─ length.em.is_zero()? ── 否 → None（相对长度不换算）
                       ── 是 → Code("{pt}pt = {mm}mm = {cm}cm = {in}in")  （各保留 2 位小数）
```

长度换算的物理依据（1 英寸定义）：

\[
1\,\text{in} = 72\,\text{pt} = 25.4\,\text{mm} = 2.54\,\text{cm}
\quad\Longrightarrow\quad
1\,\text{pt} = \tfrac{1}{72}\,\text{in} \approx 0.3528\,\text{mm}
\]

#### 4.3.3 源码精读

单值路径见 [tooltip.rs:L76-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L76-L88)：

```rust
if let [(value, _), rest @ ..]
    && rest.iter().all(|(v, _)| value == v)
{
    if let Some(docs) = find_value_docs(world, value) {
        return Some(Tooltip::Text(docs.summary()));
    }
    if let &Value::Length(length) = value
        && let Some(tooltip) = length_tooltip(length)
    {
        return Some(tooltip);
    }
}
```

- `if let [(value, _), rest @ ..] && rest.iter().all(...)`：解构出「至少一个值，且其余都等于第一个」。空 `values` 不进入此块（直接漏到第 6 步，渲染空串→`None`）。
- 文档优先于长度：对函数值而言，文档比 `repr()` 更有用，故先查文档。

`length_tooltip` 见 [tooltip.rs:L174-L184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L174-L184)：

```rust
fn length_tooltip(length: Length) -> Option<Tooltip> {
    length.em.is_zero().then(|| {
        Tooltip::Code(eco_format!(
            "{}pt = {}mm = {}cm = {}in",
            round_with_precision(length.abs.to_pt(), 2),
            round_with_precision(length.abs.to_mm(), 2),
            round_with_precision(length.abs.to_cm(), 2),
            round_with_precision(length.abs.to_inches(), 2),
        ))
    })
}
```

- `length.em.is_zero()`：只对**绝对长度**换算。`1em` 这种字体相关长度 `em` 非零，`is_zero()` 为假 → 返回 `None`。
- `round_with_precision(..., 2)`：统一保留 2 位小数，避免过长尾数。

**一个微妙的顺序点**：单值路径在第 4 步、字面量检查在第 5 步。对 `#10pt`（`Numeric` 字面量），单值路径命中长度换算并 **`return`**，**根本到不了**第 5 步的字面量放弃——所以绝对长度字面量**有**提示。而对 `#10`（`Int` 字面量），单值路径不命中（无文档、非长度）→ 漏到第 5 步 → `is_literal()` 为真 → `None`。同样是字面量，是否有提示取决于「单值路径是否先行命中」。

#### 4.3.4 代码实践

**实践目标**：验证「绝对长度有换算提示、`em` 与其它字面量没有」的顺序差异。

**操作步骤**：在 `tooltip.rs` 测试模块仿照 `test_tooltip` 风格新增三个断言（**示例代码**，非项目原有）：

```rust
test("#10pt", 1, Side::After).must_be_code(/* 你预测的换算串 */);
test("#10em", 2, Side::After).must_be_none();
test("#10",   2, Side::After).must_be_none();
```

**需要观察的现象 / 预期结果**：

- `#10pt`：`Value::Length(10pt)`，`em` 为零 → `length_tooltip` 返回 `Code`，内容形如 `"10pt = 8.82mm = 0.88cm = 0.14in"`（具体数值以本地为准）。
- `#10em`：`Value::Length(10em)`，`em` 非零 → `length_tooltip` 返回 `None`；漏到第 5 步，`Numeric` 是字面量 → `None`。
- `#10`：`Int` 字面量 → 单值路径不命中 → 第 5 步字面量放弃 → `None`。

> 具体换算数值与各 `to_*` 实现相关，**待本地验证**；命令：`cargo test -p typst-ide --lib tooltip::tests`。

#### 4.3.5 小练习与答案

**练习 1**：悬停 `#box` 的 `box` 得到 `Text`（函数文档），悬停 `#box()` 的 `(` 得到 `Code`（`box()`）。两者都是单值，为什么一个走文档、一个走 `repr`？

**答案**：`box`（标识符）的单值是 `Value::Func(box)`，`find_value_docs` 对原生函数命中 → `Text(summary)` 并 `return`。`box()`（调用）的单值是**调用结果**（一段 content/value），`find_value_docs` 对结果值不命中、非长度 → 漏到多值路径 → 结果的 `repr()` = `"box()"` → `Code`。区别在于「值本身是否带文档」：函数值带文档，调用结果不带。

**练习 2**：为什么 `length_tooltip` 要排除 `em` 非零的相对长度？

**答案**：`em`（字体相关）长度只有在给定字体大小时才能换算成绝对单位；悬停时没有确定的字号上下文，强行换算会给出误导性的数字。`length.em.is_zero()` 只放行纯绝对长度（pt/mm/cm/in），保证换算结果确定且有意义。这也呼应了 u2-l5 里 `summarize_font_family`/`MetricRange` 对字体度量的谨慎处理。

### 4.4 多值路径——×N 合并、MAX_VALUES 截断与 pretty_comma_list

#### 4.4.1 概念说明

当单值路径没「接住」（多值、或单值但无文档非长度且非字面量）时，进入**多值路径**：把候选值逐个 `value.repr()`，合并连续重复值并标 `(×N)`，最后用 `pretty_comma_list` 拼成一行（或太长时换行）。这里有两个关键机制：

- **`Sink::MAX_VALUES` 截断**：`trace` 在收集器里**最多存 10 个值**（`MAX_VALUES = 10`），防止一个被频繁求值的 span（如循环体内）撑爆内存。`expr_tooltip` 进一步只展示前 `MAX_VALUES - 1 = 9` 个，若还有第 10 个，就追加 `"..."` 表示「还有更多」。
- **连续重复合并 `(×N)`**：同一个 span 被求值多次（循环、递归、多次调用）会产生重复值。直接列 `3, 3, 3` 很吵；合并成 `3 (×3)` 更简洁。注意只合并**连续**相同的值。

#### 4.4.2 核心流程

```
【字面量短路】 if expr.is_literal() { return None; }   ← 字面量到此放弃

【多值合并】
  取 values 的前 MAX_VALUES-1(=9) 个：
    for each value:
      若与前一个相同 → 计数+1，跳过
      若不同且前一组计数>1 → 给前一个 piece 追加 " (×N)"
      否则 → 新 push value.repr()
  收尾：最后一组若计数>1 → 追加 " (×N)"
  若取完 9 个后还有剩余(第10个) → pieces.push("...")

  tooltip = repr::pretty_comma_list(&pieces, false)
  └─ 总长 ≤ 50 字符 → 横向 "a, b, c"
  └─ 否则          → 纵向 "a,\nb,\nc,"
  return 非空 ? Code(tooltip) : None
```

#### 4.4.3 源码精读

合并逻辑见 [tooltip.rs:L90-L121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L90-L121)：

```rust
if expr.is_literal() {
    return None;
}

let mut last = None;
let mut pieces: Vec<EcoString> = vec![];
let mut iter = values.iter();
for (value, _) in (&mut iter).take(Sink::MAX_VALUES - 1) {
    if let Some((prev, count)) = &mut last {
        if *prev == value {
            *count += 1;
            continue;
        } else if *count > 1 {
            write!(pieces.last_mut().unwrap(), " (×{count})").unwrap();
        }
    }
    pieces.push(value.repr());
    last = Some((value, 1));
}

if let Some((_, count)) = last && count > 1 {
    write!(pieces.last_mut().unwrap(), " (×{count})").unwrap();
}

if iter.next().is_some() {
    pieces.push("...".into());
}

let tooltip = repr::pretty_comma_list(&pieces, false);
(!tooltip.is_empty()).then(|| Tooltip::Code(tooltip.into()))
```

逐点拆解：

- **字面量短路**（[tooltip.rs:L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L90-L92)）：`is_literal()` 覆盖 `None/Auto/Bool/Int/Float/Numeric/Str`（[ast.rs:L567-L578](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L567-L578)）。字面量值一目了然，不需要提示——所以 `#10`、`#"hi"`、`#true` 悬停都是 `None`。
- **`take(Sink::MAX_VALUES - 1)`**：`MAX_VALUES` 是 [engine.rs:L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L171) 的常量 `10`。`take(9)` 只遍历前 9 个。
- **`(×N)` 合并**：`last` 记录「上一个不同值及其连续计数」。遇到相同值就 `count+=1` 并 `continue`（不 push）；遇到新值时，若上一组计数 >1 就给已 push 的那个 piece 追加 `" (×N)"`。循环结束后再补一次收尾（处理最后一组的计数）。
- **`iter.next().is_some()` → `"..."`**：`take(9)` 用的是一个可变借用迭代器，取完 9 个后，`iter` 上还可能剩第 10 个。`iter.next()` 探一下：若 `Some`，说明被截断了，追加 `"..."`。这正是「候选值超过 `MAX_VALUES-1` 个」时的结尾处理。

`Sink` 的截断定义见 [engine.rs:L230-L235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L230-L235)：

```rust
pub fn value(&mut self, value: Value, styles: Option<Styles>) {
    if self.values.len() < Self::MAX_VALUES {
        self.values.push((value, styles));
    }
}
```

即收集器**硬上限 10**：第 11 个起直接丢弃。所以 `expr_tooltip` 里 `iter.next().is_some()` 实际只在「恰好存满 10 个」时为真——展示 9 个 + `"..."`，既给上限信号，又不让提示过长。

最终拼接用 `pretty_comma_list`，见 [repr.rs:L173-L198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/repr.rs#L173-L198)：

```rust
pub fn pretty_comma_list(pieces: &[impl AsRef<str>], trailing_comma: bool) -> String {
    const MAX_WIDTH: usize = 50;
    let len = pieces.iter().map(|s| s.as_ref().len()).sum::<usize>()
        + 2 * pieces.len().saturating_sub(1);   // 加上 ", " 的宽度
    if len <= MAX_WIDTH { /* 横向：a, b, c */ }
    else { /* 纵向：每段 trim 后 + ",\n" */ }
}
```

阈值 `MAX_WIDTH = 50`：拼接总长（含分隔符 `", "`）不超过 50 字符就横排，否则每段单独一行。`expr_tooltip` 传 `trailing_comma=false`，所以横排时不带尾逗号。值的 `repr()` 由 `Repr` trait 的 `fn repr(&self) -> EcoString` 提供（[repr.rs:L49-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/repr.rs#L49-L53)），例如 `Value::Int(3).repr()` = `"3"`、`Value::Symbol(...).repr()` = `"symbol(\"π\", (\"alt\", \"ϖ\"))"`。

#### 4.4.4 代码实践

**实践目标**：用纸笔推演 `×N` 合并、`"..."` 截断，再与测试对照。

**操作步骤一（基础场景）**：解释 `#let x = 1 + 2` 悬停 `x`（偏移 5）为何得到 `Code("3")`。

逐步推导：

1. 门槛：`x` 是 `Ident`，`hash()` 为真 → 通过。
2. `analyze_expr(Ident x)` → 走 `trace`。求值器在 `bind` 时对 `x` 的 span 调一次 `trace_at`（[vm.rs:L58-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L58-L59)），值 = `1+2` = `3`。`Sink` 收到 `[(Int(3), None)]`，**仅一个值**。
3. 单值路径：`find_value_docs(Int 3)`→None；非长度 → 不 `return`，漏下。
4. `is_literal()`？`x` 是 `Ident` 不是字面量 → 不短路。
5. 多值合并：`values=[3]`，`take(9)` 取到 `[3]`：`last=None` → push `"3"`，`last=(3,1)`；循环结束，`count=1` 不 >1；`iter.next()` 无更多 → 不加 `"..."`。`pieces=["3"]`。
6. `pretty_comma_list(["3"], false)` = `"3"`（≤50，横排）→ `Code("3")`。

对应测试 [tooltip.rs:L338-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L338-L341)。

**操作步骤二（截断场景）**：说明「候选值超过 `MAX_VALUES-1` 个时结尾 `"..."`」。

- `Sink::MAX_VALUES = 10`，收集器最多存 10 个；`expr_tooltip` 用 `take(MAX_VALUES-1)` = `take(9)` 只渲染前 9 个。
- 取完 9 个后，`iter.next().is_some()` 探测是否还有第 10 个：若该 span 一共产出了 ≥10 个值（被收集器截到 10），则探测为真，`pieces.push("...")`。
- 所以最终形如 `v1, v2, ..., v9, ...`——前 9 个值 + 一个 `...` 表示「被截断」。`...` 是「还有更多没显示」的信号，而非第 10 个值本身。

**需要观察的现象**：设想一个在循环/递归中被求值 ≥10 次的 span（如循环体内的标识符），其悬停提示应以 `...` 结尾。具体哪种代码片段会触发 ≥10 次**待本地验证**（因为 trace 在一次文档求值内对同一 span 的求值次数取决于循环/调用结构）。

> 命令：`cargo test -p typst-ide --lib tooltip::tests`。

#### 4.4.5 小练习与答案

**练习 1**：候选值为 `[3, 3, 3, 5]`（同一 span 求值四次，前三次得 3、第四次得 5），最终提示是什么？

**答案**：`take(9)` 取全部 4 个。遍历：`3`(push,last=(3,1)) → `3`(count=2,continue) → `3`(count=3,continue) → `5`(prev count=3>1 → 给 `"3"` 追加 `" (×3)"` 变 `"3 (×3)"`；push `"5"`,last=(5,1))。收尾 count=1 不追加。`iter.next()` 无更多 → 不加 `...`。`pieces=["3 (×3)", "5"]` → `"3 (×3), 5"` → `Code`。注意 `×3` 标注的是**连续**重复次数。

**练习 2**：为什么 `take(Sink::MAX_VALUES - 1)` 而不是 `take(Sink::MAX_VALUES)`？

**答案**：留出一个名额给 `"..."`。收集器上限是 10，若渲染全部 10 个，就没有余量在结尾表达「被截断」——用户会以为就这 10 个。取 9 个、用第 10 个的存在性触发 `...`，既贴近上限（尽量多展示），又能明确告知「还有更多」。这是一个在「信息量」与「诚实性」之间的精巧平衡。

**练习 3**：`pretty_comma_list` 何时从横排切到竖排？为什么 `expr_tooltip` 传 `trailing_comma=false`？

**答案**：当拼接总长（各段长度之和 + 分隔符 `", "` 占的 `2*(n-1)` 字符）超过 `MAX_WIDTH=50` 时切竖排，每段 `trim` 后加 `",\n"`，避免单行过长。传 `false` 是因为悬停提示是一段「值的罗列」，横排时不需要尾逗号（不像数组字面量 `[a, b,]` 那样有语法意义）。

## 5. 综合实践

把四条主线（门槛、取值、单值、多值）串起来，完成下面这个「悬停结果预测」小任务。

**场景**：源码片段

```typst
#import "lib.typ": foo
#foo(none, width: 1pt)
```

其中 `lib.typ` 定义 `foo` 为一个带文档注释的原生风格函数（可参考 [tooltip.rs:L529-L537](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L529-L537) 的 `test_tooltip_user_function` 用到的 `EXAMPLE_CLOSURE`）。

**任务**：

1. 悬停被调用处的 `foo`（`#foo` 的 `foo`）会得到什么？走的是哪条路径？`Text` 还是 `Code`？
2. 悬停参数名 `width` 会得到什么？走的是 `expr_tooltip` 还是别的分支？
3. 悬停参数值 `1pt` 会得到什么？为什么绝对长度有提示、而若是 `1em` 就没有？
4. 若 `foo` 内部对某 span 求值了 12 次、得到 12 个不同的值，悬停那个 span 时提示长什么样？

**参考答案**：

1. `foo` 是 `Ident`，通过门槛；`analyze_expr` 走 `trace`，得到 `Value::Func(foo)`（单值）。单值路径：`find_value_docs` 对带文档的函数命中 → `Text(foo 的文档摘要)`。所以是 `Text`，走单值路径的「文档优先」分支。
2. 参数名 `width` 不会进 `expr_tooltip`——它在分发链里更靠前被 `named_param_tooltip` 抢先认领（u3-l2/u3-l3 的 `named_param` 分支），给出参数文档 `Text(...)`。这正印证了 u3-l2 讲的「便宜且具体的分支排在 `expr` 之前」。
3. `1pt`：`Numeric` 字面量 → `Value::Length(1pt)`，`em` 为零 → 单值路径命中 `length_tooltip` → `Code("1pt = 0.35mm = 0.04cm = 0.01in")`（数值以本地为准）。它在**第 4 步单值路径就 `return`**，到不了第 5 步的字面量放弃。若改成 `1em`：`length_tooltip` 因 `em` 非零返回 `None`，单值路径不 `return` → 漏到第 5 步 → `Numeric` 是字面量 → `None`。
4. 收集器把 12 个值截到 `MAX_VALUES=10`；`expr_tooltip` 渲染前 9 个（连续相同者合并为 `×N`），第 10 个的存在触发 `iter.next().is_some()` → 追加 `...`。最终形如 `v1, v2, ..., v9, ...`（若有连续重复则相应合并）。

> 第 4 题的具体触发场景与各 `repr` 字符串建议本地 `cargo test` 验证。

## 6. 本讲小结

- `expr_tooltip` 先沿祖先向上找到首个 `ast::Expr`，再用门槛 `expr.hash() || 匹配数学变量访问` 决定是否分析——只分析可 `#`-嵌入的代码表达式或数学变量访问，以收窄昂贵的 `trace` 触发面、过滤无意义的字面量/运算符。
- 候选值由 `analyze_expr` 产出：字面量直接构造（便宜、`Styles=None`）；`Contextual` 与字段访问的「字段部分」递归回溯；其余回退到 `typst::trace`（标记 span、重跑文档、`sink.values()` 收尾）。
- **单值路径**优先尝试 `find_value_docs`（→`Text` 摘要）与 `length_tooltip`（→`Code` 单位换算）；都不命中则「漏」到后续，不在此 `return`。这个「漏」是 `#let x=1+2` 的 `x` 最终得到 `Code("3")` 的关键。
- **字面量短路** `if expr.is_literal() { return None }`：纯字面量（Int/Float/Bool/Str/`1em` 等）没有提示；但绝对长度字面量（`10pt`）因单值路径先行命中换算而**有**提示——顺序决定差异。
- **多值路径**用 `take(Sink::MAX_VALUES - 1)` 取前 9 个、合并连续重复为 `(×N)`、超限追加 `...`，再用 `pretty_comma_list`（阈值 50 字符横排/竖排）拼成 `Code`。`Sink::MAX_VALUES=10` 是收集器硬上限。
- 一条经验法则：单值且值带文档 → `Text`；其余（值本身、调用结果、多值）→ `value.repr()` → `Code`。

## 7. 下一步学习建议

- **u3-l4（特殊场景 tooltip）**：精读 `font_tooltip`/`label_tooltip`/`import_tooltip`/`closure_tooltip`——它们和 `expr_tooltip` 同处一条分发链，但取值与展示各有讲究（字体摘要、标签详情、星号导入列表、闭包捕获）。
- **u4（跳转定义 definition）**：`definition` 同样依赖 `analyze_expr` 取值（如对 `Callee` 先查 `named_items` 再回退 `analyze_expr`）。学完本讲对「取值」的理解可直接迁移。
- 阅读建议：把本讲的 `analyze_expr` 三条路径与 u2-l4 的总览对照，体会「字面量直接构造」如何为悬停这种高频操作省下全文求值。
- 进阶思考：试着构造一个能让同一 span 产出多个不同值的真实片段（如递归函数内部），观察 `×N` 与 `...` 的实际效果，体会 `Sink::MAX_VALUES` 截断在极端场景下的取舍。
