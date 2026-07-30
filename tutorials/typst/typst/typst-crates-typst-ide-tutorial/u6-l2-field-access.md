# 字段访问补全

## 1. 本讲目标

在 Typst 里，`#list.range(0, 3)`、`#emoji.face.grin`、`#(5pt).abs`、`#sym.arrow.r` 都属于「点号后的字段访问」。本讲聚焦 typst-ide 如何在用户敲下点号（或点号后已经开始打字）时，给出正确的补全候选。

学完本讲你应当能够：

1. 说清 `complete_field_accesses` 识别的两类触发形态（`expr.` 与 `expr.id|`），以及为什么要区分「真正的点号 token」与「markup/math 里的文本点号」。
2. 解释为什么补全的第一步是「为被访问对象取值」而不是「为整个字段访问取值」，并知道这个值由 `analyze_expr`（参见 u2-l4）提供。
3. 掌握 `field_access_completions` 的五大补全来源：方法（`scope().iter()` + `self` 过滤）、模块/类型自身成员（`value.scope()`）、类型预定义字段（`fields_on`）、容器字段（`Content`/`Dict`/`Args`）与符号修饰符（`Symbol`），外加元素函数的 get 规则。
4. 独立推导一段真实 Typst 代码（如 `#().`、`emoji.fa|`）从光标到候选列表的完整过程。

本讲承接 u6-l1（补全分发管线）与 u2-l4（`analyze_expr`）。字段访问补全位于整个补充分发链的**最顶端**（最优先），是理解 typst-ide 补全引擎绕不开的一环。

## 2. 前置知识

阅读本讲前，请确认你已了解：

- **补全分发链**（u5-l1 / u6-l1）：`autocomplete` 用 `||` 短路的方式依次尝试多个 `complete_*`，谁先返回 `true` 谁就独占补全。本讲的 `complete_field_accesses` 正是这条链上的第一关。
- **`CompletionContext`**（u5-l2）：贯穿整个补全过程的可变上下文，维护 `leaf`（光标叶子）、`cursor`、`from`（替换起点）、`before`/`after`，以及候选列表 `completions`。
- **`analyze_expr`**（u2-l4）：给定一个语法节点，返回它「可能的值」`EcoVec<(Value, Option<Styles>)>`。字面量直接构造，复杂表达式回退到 `typst::trace` 重跑整篇文档捕获运行时值。
- **Typst 的值与方法**：Typst 的「方法」（如数组的 `len`、字符串的 `contains`）本质上是注册在该**类型作用域**里的原生函数，且第一个参数名为 `self`。这是本讲方法补全的根基。

几个关键术语先统一：

- **对象（object）/ 目标表达式**：点号左边的那个表达式，例如 `#().` 里的 `()`、`emoji.fa` 里的 `emoji`。它的运行时值决定点号后能补什么。
- **`value.ty()`**：值的类型（如 `array`、`str`、`module`）。类型有自己的作用域 `scope()`，方法就挂在上面。
- **`value.scope()`**：值自身附带的作用域，只有 `Func`/`Type`/`Module` 三种值有（模块把自己的成员挂在上面）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-ide/src/complete.rs` | 本讲主战场。`complete_field_accesses` 识别触发形态并取值，`field_access_completions` 生成全部候选；`CompletionContext::value_completion_full` 等负责把值渲染成 `Completion`。 |
| `crates/typst-ide/src/analyze.rs` | 提供 `analyze_expr`，为「被访问对象」推断运行时值。 |
| `crates/typst-ide/src/utils.rs` | （承接 u2-l5）`globals`、`check_value_recursively` 等，本讲主要在对比「方法补全」与「作用域补全」时提及。 |
| `crates/typst-library/src/foundations/fields.rs` | `fields_on` 的定义，列出有「预定义字段」的类型（`Length`/`Rel`/`Stroke`/`Alignment`/`Version`）。 |
| `crates/typst-library/src/foundations/value.rs` | `Value::field`、`Value::scope`、`Value::ty` 的定义，解释字段访问在语言层面的语义。 |

## 4. 核心概念与源码讲解

### 4.1 complete_field_accesses：识别两类触发形态

#### 4.1.1 概念说明

字段访问补全要解决的核心问题是：**用户在哪里、对什么对象、想要点号后面的什么**。

typst-ide 把「触发位置」归纳为两类互斥形态：

- **形态 A：`expr.`** —— 点号紧跟在一个完整表达式之后，光标停在点号上或点号后，还没开始打字段名。例如 `#().`、`#assert.`、`#sym.arrow.`。
- **形态 B：`expr.id|`** —— 点号后已经打了一部分标识符，光标停在这个半成品标识符上。例如 `emoji.fa|`、`#assert.e`。

为什么要点出这两种形态？因为它们在语法树里的节点结构完全不同：

- 形态 A 的光标叶子是「点号」本身（`SyntaxKind::Dot`，或 markup/math 模式下的文本点号）。
- 形态 B 的光标叶子是「标识符」（`SyntaxKind::Ident` / `SyntaxKind::MathIdent`），点号是它的前一个兄弟节点。

此外还有一个易错点：在 markup（正文）和 math 模式里，点号不是独立的 `Dot` token，而是被解析成 `Text`/`MathText` 节点且文本内容是 `"."`。这类「文本点号」需要额外检查——不能在点号前面隔着空白时还触发（否则正文里随便一个句号都会触发补全）。

#### 4.1.2 核心流程

`complete_field_accesses` 的整体流程是「先判定形态 → 再定位对象表达式 → 取值 → 委托生成」：

```
1. 计算两个标志：after_dot（光标是否在点号上）、textual_dot（点号是否为文本点号）
2. 若形态 A 命中（after_dot 为真）：
     a. 点号的前一个兄弟必须是表达式 prev
     b. 若是文本点号，要求 prev 与点号之间没有 trivia（空白/注释）
     c. prev 必须允许字段访问（markup 模式下要求 prev 前面有 #）
     d. analyze_expr(prev) 取到对象值
     e. ctx.from = cursor（光标处开始替换，即纯插入）
     f. 委托 field_access_completions(value)
3. 若形态 B 命中（光标在 Ident/MathIdent 上）：
     a. 前一个兄弟必须是 Dot
     b. 再前一个兄弟 prev_prev 必须是表达式
     c. analyze_expr(prev_prev) 取到对象值
     d. ctx.from = 叶子起点（替换掉已输入的半个字段名）
     e. 委托 field_access_completions(value)
4. 都不命中 → 返回 false，交给分发链下一关
```

一个关键细节：**两类形态都把 `analyze_expr` 作用在「对象表达式」上，而不是整个字段访问**。形态 A 作用在 `prev`（点号前的对象），形态 B 作用在 `prev_prev`（点号再前的对象）。因为我们想要的是「这个对象身上有哪些可用的字段/方法」，对象本身的值就足以回答。

#### 4.1.3 源码精读

函数首先用一个小 `match` 区分点号的两种来源（[src/complete.rs:116-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L116-L122)）：

```rust
let (after_dot, textual_dot) = match ctx.leaf.kind() {
    SyntaxKind::Dot => (true, false),
    SyntaxKind::Text | SyntaxKind::MathText if ctx.leaf.leaf_text() == "." => {
        (true, true)
    }
    _ => (false, false),
};
```

- `SyntaxKind::Dot`：code 模式下的真正点号 token，`after_dot=true, textual_dot=false`。
- `Text`/`MathText` 且文本是 `"."`：markup/math 里的文本点号，`after_dot=true, textual_dot=true`。
- 其它：都不是点号，`after_dot=false`，形态 A 直接无望。

**形态 A**（[src/complete.rs:125-139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L125-L139)）是一长串 `&&` 的守卫链：

```rust
if after_dot
    && let Some(prev) = ctx.leaf.prev_sibling()
    // 文本点号时，不允许 prev 与点号之间夹着 trivia：`[#x .|]`
    && (!textual_dot || prev.range().end == ctx.leaf.range().start)
    && prev.is::<ast::Expr>()
    // 该表达式必须允许字段访问
    && (prev.parent_kind() != Some(SyntaxKind::Markup)
        || prev.prev_sibling_kind() == Some(SyntaxKind::Hash))
    && let Some((value, styles)) = analyze_expr(ctx.world, &prev).into_iter().next()
{
    ctx.from = ctx.cursor;
    field_access_completions(ctx, &value, &styles);
    return true;
}
```

四个守卫的含义：

1. `(!textual_dot || prev.range().end == ctx.leaf.range().start)`：只有文本点号才需要这个额外检查，要求 `prev` 的结尾紧贴点号的起点（中间没有空白）。`#() .`（点号前有空格）正是被这一条挡掉的；`#{() .}` 因为是 code 模式（`textual_dot=false`）所以不受限。这正是测试 `test_autocomplete_dot_whitespace` 验证的行为。
2. `prev.is::<ast::Expr>()`：点号前必须是一个表达式，不能是别的奇怪节点。
3. `prev.parent_kind() != Some(Markup) || prev.prev_sibling_kind() == Some(Hash)`：在正文（Markup）里，只有被 `#` 引入的嵌入代码表达式才允许字段访问（如 `#x.`）。正文里裸写的 `x.` 只是一段文本，不应触发补全。
4. `analyze_expr(...).into_iter().next()`：对象必须能取到至少一个值，否则没有依据生成候选。

命中后 `ctx.from = ctx.cursor`，意味着「替换从光标开始」，即候选是纯插入（点号后还没字）。注意 `analyze_expr` 返回的是元组 `(Value, Option<Styles>)`，这里用 `.into_iter().next()` 只取**第一个**候选值——字段访问补全只关心一个「代表性」的值，不去合并多个可能值。

**形态 B**（[src/complete.rs:142-157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L142-L157)）的结构是「叶子 → Dot → 对象」三级回溯：

```rust
if matches!(ctx.leaf.kind(), SyntaxKind::Ident | SyntaxKind::MathIdent)
    && let Some(prev) = ctx.leaf.prev_sibling()
    && prev.kind() == SyntaxKind::Dot
    && let Some(prev_prev) = prev.prev_sibling()
    && prev_prev.is::<ast::Expr>()
    && let Some((value, styles)) =
        analyze_expr(ctx.world, &prev_prev).into_iter().next()
{
    debug_assert!(matches!(
        ctx.leaf.parent_kind(),
        Some(SyntaxKind::FieldAccess | SyntaxKind::MathFieldAccess),
    ));
    ctx.from = ctx.leaf.offset();
    field_access_completions(ctx, &value, &styles);
    return true;
}
```

与形态 A 有两处关键差异：

- 取值对象是 `prev_prev`（叶子是标识符、`prev` 是点号、`prev_prev` 才是对象表达式）。
- `ctx.from = ctx.leaf.offset()`：替换起点是已输入标识符的开头。这样在 `emoji.fa|` 处选中 `face`，会把 `fa` 整体替换成 `face`，而不是追加成 `faface`。

`debug_assert!` 断言此时父节点确实是 `FieldAccess`/`MathFieldAccess`，是对语法树形状的合理性校验（仅 debug 构建生效）。注意形态 B **没有** `textual_dot` 检查——因为既然点号后已经长出标识符，说明点号已被解析为真正的 `Dot`，不会是含糊的文本点号。

最后，函数在两形态都未命中时返回 `false`（[src/complete.rs:159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L159)），让分发链继续尝试后续的 `complete_open_labels`、`complete_imports` 等。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「文本点号 + 空白」会被形态 A 挡下，而 code 模式下的点号不会。

**操作步骤**：

1. 打开 `src/complete.rs` 的测试 `test_autocomplete_dot_whitespace`（[src/complete.rs:1658-1667](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1658-L1667)）。
2. 在 `crates/typst-ide` 目录下运行该测试：

   ```
   cargo test --package typst-ide test_autocomplete_dot_whitespace
   ```
3. 阅读断言：`test("#() .", -1).must_exclude(["insert", "remove", "len", "all"])` —— 正文里 `#() .`（空格隔开）不会补数组方法；而 `test("#{() .}", -2).must_include([...])` —— code 块里同样空格隔开却**会**补。

**需要观察的现象**：同一组字符 `() .`，仅仅因为所处模式不同（Markup vs Code），补全结果完全相反。

**预期结果**：测试通过。原因正是形态 A 的守卫 `(!textual_dot || prev.range().end == ctx.leaf.range().start)`：code 模式下 `textual_dot=false`，守卫恒真放行；markup 模式下 `textual_dot=true`，点号前的空格使 `prev.range().end != leaf.range().start`，守卫失败。

> 待本地验证：若你在自己的编辑器里接入了 typst-ide，在正文输入 `#() .`（中间留空格）应看不到方法补全，输入 `#().` 才会看到。

#### 4.1.5 小练习与答案

**练习 1**：在 `#x.|`（markup 模式，`x` 由 `#` 引入）处，形态 A 的第三个守卫 `prev.parent_kind() != Some(Markup) || prev.prev_sibling_kind() == Some(Hash)` 的两个子条件分别是真是假？整体结果如何？

**答案**：`prev`（即 `x`）的父节点是 `Markup`，故第一个子条件为假；`x` 的前一个兄弟是 `#`（`Hash`），故第二个子条件为真。`假 || 真 = 真`，守卫放行，触发补全。若写成正文里裸的 `x.`（无 `#`），则两个子条件分别为假、假，整体为假，不触发——这正是我们想要的。

**练习 2**：形态 B 为什么不需要 `textual_dot` 检查？

**答案**：形态 B 的前提是光标叶子已是 `Ident`/`MathIdent`，且其前一个兄弟是 `SyntaxKind::Dot`。能成为兄弟节点且 `kind()==Dot`，说明这个点号已被解析成真正的点号 token，绝不可能是含糊的文本点号（文本点号的 kind 是 `Text`/`MathText`），所以无需再判。

---

### 4.2 analyze_expr：为「被访问对象」推断值

#### 4.2.1 概念说明

`field_access_completions` 要生成候选，必须先知道「点号左边的对象是什么值」：是数组、字符串、模块、符号，还是 content？不同值类型走完全不同的补全分支。

这个值由 `analyze_expr`（详见 u2-l4）提供。它的返回类型是 `EcoVec<(Value, Option<Styles>)>`——一个表达式可能被求值多次、得到多个值，每个值还可能附带「求值时的活动样式」`Styles`。

字段访问补全只关心一个代表性的值，因此在 4.1 的两处调用里都用了 `.into_iter().next()` 取第一个。这里我们重点理解**对象表达式**走 `analyze_expr` 时会发生什么，以及它和「分析整个字段访问」的差别。

#### 4.2.2 核心流程

`analyze_expr` 对象表达式的求值路径（简化）：

```
analyze_expr(node):
  若 node 能 cast 成 ast::Expr：
    匹配字面量（None/Auto/Bool/Int/Float/Numeric/Str）→ 直接构造 Value，返回 [(value, None)]
    否则（变量、数组、字段访问、函数调用等复杂表达式）：
      若是 Contextual → 递归分析其子节点
      若 node 的父节点是 FieldAccess/MathFieldAccess 且 node 是字段部分（index>0）→ 递归分析父节点
      否则 → typst::trace：标记 node.span()，重跑整篇文档，求值器命中时把值连同样式存入 Sink，返回这些值
```

对字段访问补全而言，对象表达式（如 `()`、`emoji`、`assert`、`5pt`）大多不是字面量（`5pt` 是 `Numeric` 字面量，会直接构造），于是落到 `typst::trace` 分支：**重新求值整篇文档**，捕获该对象在运行时的真实值。

#### 4.2.3 源码精读

`analyze_expr` 主体（[src/analyze.rs:12-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L12-L50)）：

```rust
pub fn analyze_expr(
    world: &dyn IdeWorld,
    node: &LinkedNode,
) -> EcoVec<(Value, Option<Styles>)> {
    let Some(expr) = node.cast::<ast::Expr>() else {
        return eco_vec![];
    };

    let val = match expr {
        ast::Expr::None(_) => Value::None,
        ast::Expr::Auto(_) => Value::Auto,
        ast::Expr::Bool(v) => Value::Bool(v.get()),
        ast::Expr::Int(v) if v.get().is_ok() => Value::Int(v.get().unwrap()),
        ast::Expr::Float(v) => Value::Float(v.get()),
        ast::Expr::Numeric(v) => Value::numeric(v.get()),
        ast::Expr::Str(v) => Value::Str(v.get().into()),
        _ => {
            // ... Contextual 与 FieldAccess 父节点回退 ...
            return typst::trace::<PagedDocument>(world.upcast(), node.span());
        }
    };

    eco_vec![(val, None)]
}
```

对字段访问补全有两点要特别留意：

1. **`Numeric` 是字面量**（[src/analyze.rs:26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L26)）。所以 `#(5pt).` 里的 `5pt` 会直接得到 `Value::Length`，便宜且 `Styles` 为 `None`。这解释了为什么长度字段（`em`/`abs`，见 4.4）能稳定补出，而不依赖重跑文档。

2. **对象表达式通常走 trace**。像 `()`（空数组）、`emoji`（模块）、`assert`（带作用域的函数）都不是字面量，会落到 `typst::trace`（[src/analyze.rs:45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45)）。trace 会标记该 span 并重新求值整篇文档，捕获运行时值。因此字段访问补全的「取值」阶段可能触发一次完整的文档重算——这是它比分发链里一些纯查表补全更昂贵的原因，但因为它排在分发链最前、且只在确实命中字段访问形态时才执行，代价可控。

父节点回退分支（[src/analyze.rs:35-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L35-L43)）在本讲**不直接生效**，因为我们调用 `analyze_expr` 的对象永远是点号左边的「对象」（`FieldAccess` 的第 0 个子节点，`index==0`），而非字段名部分。该分支主要服务于悬停提示等「分析字段访问整体」的场景（u3-l3）。

#### 4.2.4 代码实践

**实践目标**：确认 `analyze_expr` 对不同对象给出不同类型的值，从而决定 `field_access_completions` 走哪条分支。

**操作步骤**：这是「源码阅读 + 推理」型实践。

1. 对下列四个对象表达式，推断 `analyze_expr` 走字面量分支还是 trace 分支，以及得到的 `Value` 变体：
   - `#(5pt).` 的对象 `5pt`
   - `#().` 的对象 `()`（空数组字面量）
   - `#assert.` 的对象 `assert`
   - `#sym.arrow.` 的对象 `sym.arrow`（已经是字段访问）
2. 对照 [src/analyze.rs:20-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L20-L47) 验证你的推断。

**需要观察的现象**：`5pt` 是 `Numeric` 字面量（直接构造 `Value::Length`）；`()` 虽是「数组字面量」但不在 `analyze_expr` 的字面量枚举里，走 trace 得 `Value::Array`；`assert`、`sym.arrow` 都走 trace，分别得 `Value::Func`（带作用域）和 `Value::Symbol`。

**预期结果**：

| 对象 | 求值路径 | 值变体 |
|------|----------|--------|
| `5pt` | 字面量 `Numeric` | `Value::Length` |
| `()` | trace | `Value::Array`（空） |
| `assert` | trace | `Value::Func`（有 `scope`） |
| `sym.arrow` | trace（其父非 FieldAccess，因它本身是顶层表达式） | `Value::Symbol` |

> 注意：`Value::Array` 的 `ty()` 是 `array`，`field_access_completions` 会据此进入「方法补全」分支（4.3）；`Value::Func`/`Value::Symbol` 则分别进入「成员/get 规则」与「符号修饰符」分支（4.4）。

#### 4.2.5 小练习与答案

**练习**：为什么 `field_access_completions` 只取 `analyze_expr` 返回的第一个值（`.into_iter().next()`），而不是像悬停提示那样把多个值都展示出来？

**答案**：悬停提示（u3-l3）的目的是「展示这个表达式可能等于什么」，所以多值有意义（还会合并出 `(×N)`）。而字段访问补全的目的是「列出对象身上的字段/方法」，这些字段方法由对象的**类型**决定，与「对象具体取哪个值」关系不大——同一个类型的任何实例，方法集合都一样。因此一个代表性值足够，取第一个即可，避免对每个可能值都重复生成一遍候选。

---

### 4.3 field_access_completions：方法与成员补全

#### 4.3.1 概念说明

拿到对象值后，`field_access_completions` 负责生成全部候选。它的核心直觉是：**点号后面能出现什么，取决于对象值「挂载」了哪些名字**。Typst 里这些名字来自两处：

1. **类型作用域里的方法**：数组有 `len`/`insert`/`push`，字符串有 `len`/`contains`，等等。这些方法注册在对应**类型**的 `scope()` 上，且第一个参数名为 `self`。这是 `value.ty().scope()` 的来源。对 `Content` 值，还要额外看它所属**元素**的作用域 `content.elem().scope()`（元素特有的方法）。
2. **值自身作用域里的成员**：当对象本身是 `Module`/`Type`/`Func` 时，它有自己的 `scope()`，里面是它的成员（模块的导出、类型的关联函数等）。这是 `value.scope()` 的来源。

这两个来源用了**两套不同的过滤规则**，这是本模块最关键的设计点：

- 类型作用域：只补「方法」（带 `self` 参数的函数）。因为 `#().len` 是方法调用，不该把数组类型的构造器等非方法项也列出来。
- 值自身作用域：补**全部**成员。因为 `emoji.face` 这种访问，模块成员（子模块、符号）都是合法的访问目标。

#### 4.3.2 核心流程

```
field_access_completions(ctx, value, styles):
  1. 组装 scopes 迭代器：
       - ty_scope = value.ty().scope()              // 类型作用域（方法来源）
       - elem_scope = 若 value 是 Content，取 content.elem().scope()，否则 None
       - scopes = elem_scope 链上 ty_scope
  2. 方法补全：遍历 scopes 里所有 (name, binding)
       - 把 binding 转 Func；取其第一个参数；若参数名 == "self" → ctx.call_completion(name, func)
  3. 成员补全：若 value.scope() 存在（Module/Type/Func）
       - 遍历其所有 (name, binding) → ctx.call_completion(name, binding.read())
  —— 以下 4、5 见模块 4.4 ——
  4. 类型预定义字段（fields_on）
  5. 结构化字段（Symbol 修饰符 / Content 字段 / Dict / Args / Func get 规则）
```

`ctx.call_completion(name, value)` 会转调 `value_completion_full(..., parens=true, ...)`：若该值是函数，就按 `BracketMode` 自动补上括号（如 `len(${})`）；若是普通值，则不补括号。`from` 在此前已由 `complete_field_accesses` 设好。

#### 4.3.3 源码精读

`scopes` 的组装（[src/complete.rs:168-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L168-L175)）：

```rust
let scopes = {
    let ty = value.ty().scope();
    let elem = match value {
        Value::Content(content) => Some(content.elem().scope()),
        _ => None,
    };
    elem.into_iter().chain(Some(ty))
};
```

- `value.ty().scope()`：值类型的作用域。`Type::scope()` 返回挂在该类型上的 `&'static Scope`（[typst-library ty.rs:118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L118)），方法就在其中。
- 对 `Content` 值，再取其元素的 `scope()`。这样元素实例既补「元素特有方法」（来自元素作用域），也补「content 类型通用方法」（来自 content 类型作用域）。

**方法补全循环**（[src/complete.rs:179-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L179-L186)）：

```rust
for (name, binding) in scopes.flat_map(|scope| scope.iter()) {
    let Ok(func) = binding.read().clone().cast::<Func>() else { continue };
    if let Some(param) = func.params().next()
        && param.name() == Some("self")
    {
        ctx.call_completion(name.clone(), binding.read());
    }
}
```

这是本讲的「灵魂」几行：

- `scope.iter()` 枚举作用域里所有 `(名字, 绑定)`。绑定 `binding.read()` 拿到 `Value`，尝试 `cast::<Func>()`；不是函数的就跳过。
- `func.params().next()` 取第一个参数；若其名字是 `"self"`，才认定它是「方法」。这是区分「方法」与「普通函数/构造器」的唯一判据。
- 命中则 `ctx.call_completion(name, value)`，因为 `parens=true`，函数会自动带括号。

> 这就回答了实践任务的核心：`#().` 为什么补 `insert/remove/len/all`。这些方法在 typst-library 的 `array` 类型作用域里以原生函数形式注册，第一个参数都是 `&self`（[typst-library array.rs:180-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L180-L188) 处的 `first`、紧随其后的 `push`/`insert`/`remove` 等同理）。所以它们能通过 `self` 过滤、出现在候选里；而没有 `self` 参数的类型级绑定（如有）则不会出现。

**值自身成员补全循环**（[src/complete.rs:188-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L188-L192)）：

```rust
if let Some(scope) = value.scope() {
    for (name, binding) in scope.iter() {
        ctx.call_completion(name.clone(), binding.read());
    }
}
```

`Value::scope()` 只对 `Func`/`Type`/`Module` 返回 `Some`（[typst-library value.rs:174-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L174-L181)）。注意这里**没有** `self` 过滤——模块/类型/函数的成员是直接可访问的（如 `emoji.face`、`assert.eq`）。测试 `test_autocomplete_field_access`（[src/complete.rs:1680-1693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1680-L1693)）里的 `#assert.` 补出 `eq`/`ne`，正是因为 `assert` 是带作用域的函数，`eq`/`ne` 是其作用域成员。

两个循环的分工可总结为一张表：

| 循环 | 数据来源 | 过滤 | 典型场景 |
|------|----------|------|----------|
| 方法循环 | `value.ty().scope()`（+ 元素作用域） | 仅 `self` 方法 | `#().len`、`#"x".contains` |
| 成员循环 | `value.scope()` | 全部成员 | `emoji.face`、`assert.eq` |

#### 4.3.4 代码实践

**实践目标**：验证「方法循环」与「成员循环」分别服务不同类型的值。

**操作步骤**：

1. 运行两个现成测试，观察候选来源：

   ```
   cargo test --package typst-ide test_autocomplete_array_method
   cargo test --package typst-ide test_autocomplete_field_access
   ```
2. 阅读 `test_autocomplete_array_method`（[src/complete.rs:1651-1654](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1651-L1654)）：`#().` 必含 `insert/remove/len/all`；`#{ let x = (1,2,3); x. }` 必含 `at/push/pop`。
3. 阅读 `test_autocomplete_field_access`（[src/complete.rs:1680-1685](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1680-L1685)）：`#assert.` 必含 `eq/ne`。

**需要观察的现象**：`#().`（数组值，`value.scope()` 为 `None`）的候选**只**来自方法循环；`#assert.`（函数值，有 `scope()`）的 `eq/ne` **只**来自成员循环（`assert` 函数类型的 `ty().scope()` 里并没有 `eq/ne` 这些成员）。

**预期结果**：两组测试均通过。这印证了 4.3.1 的分工：数据值走方法循环，模块/类型/函数值走成员循环。

> 待本地验证：可在测试里临时加一行 `test("#\"hello\".", -1).must_include(["len", "contains"])`（这正是 `test_autocomplete_type_methods` 的内容，[src/complete.rs:1931-1934](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1931-L1934)），确认字符串方法同样来自 `str` 类型作用域的 `self` 方法。

#### 4.3.5 小练习与答案

**练习 1**：`#table().` 为什么会补出 `cell`（如果会的话）？或者按测试 `test_autocomplete_type_methods`，`#table().` 必须 **exclude** `cell`，为什么？

**答案**：`#table().` 的对象 `table()` 求值为一个 `table` 元素的 `Content`。`Content` 走方法循环（元素作用域 + content 类型作用域，只补 `self` 方法）和后面的「Content 字段」分支（4.4，补实例字段）。`cell` 是 `grid`/`table` 的一个**参数/概念**而非 `table` 元素实例的字段或带 `self` 的方法，因此不会被补出，测试用 `must_exclude(["cell"])` 确认了这一点。

**练习 2**：假如某个值同时满足「有类型作用域方法」和「有自身作用域成员」，候选会重复吗？

**答案**：代码里没有显式去重。但因为两个循环的来源不同（类型作用域 vs 值自身作用域），通常不会出现同名项；即便理论上可能出现重名，LSP 客户端侧一般也会按 label 去重展示。 typst-ide 这里选择「按来源分别枚举、不做合并去重」，是务实的 best-effort 取舍。

---

### 4.4 field_access_completions：结构化字段补全

#### 4.4.1 概念说明

除了「方法」和「成员」这两类由作用域驱动的补全，还有一类「结构化字段」需要专门处理，它们由值的内部结构决定，而不是挂在某个作用域上：

- **类型预定义字段**（`fields_on`）：少数类型有「固定字段」，如 `Length` 有 `em`/`abs`、`Stroke` 有 `paint`/`thickness` 等。访问这些字段取出的是数据，不是方法调用。
- **符号修饰符**（`Symbol`）：`sym.arrow.r`、`sym.arrow.dashed` 这种修饰符链，修饰符集合由符号自身的变体表决定。
- **容器字段**：`Content`（元素实例的字段，如 outline entry 的 `body`/`page`）、`Dict`（字典的键）、`Args`（参数对象的具名项）。
- **元素函数的 get 规则**：当对象是一个元素函数（如 `text`、`page`）且带有活动样式时，Typst 支持「get 规则」读取当前样式值，于是把这些可设置字段也作为候选。

这些分支统一放在 `field_access_completions` 末尾的 `match value` 里，按值类型分派。

#### 4.4.2 核心流程

```
在方法循环、成员循环之后：

3'. 类型预定义字段：for field in fields_on(value.ty()):
       value_completion(field, value.field(field))   // 不带括号
4. match value:
     Symbol(symbol)  → 遍历 symbol.modifiers()，每个能 modified 的修饰符 → Completion{kind: Symbol(字形), label: 修饰符}
     Content(content)→ 遍历 content.fields() → value_completion(字段名, 字段值)
     Dict(dict)      → 遍历 dict 的 (k, v) → value_completion(k, v)
     Args(args)      → 遍历 args.to_named() 的 (k, v) → value_completion(k, v)
     Func(func)      → 若 func 是元素函数 且 styles 非空：
                          遍历可设置参数，用 settable_field_accessor 从样式链取当前值 → value_completion(参数名, 值)
     _               → 无
```

注意这里普遍用 `value_completion`（`parens=false`），因为这些是「数据字段」而非「方法调用」，不需要补括号。唯一的例外在前面两个循环里用 `call_completion`（函数要带括号）。

#### 4.4.3 源码精读

**类型预定义字段**（[src/complete.rs:194-201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L194-L201)）：

```rust
for &field in fields_on(value.ty()) {
    ctx.value_completion(field, &value.field(field, ()).unwrap());
}
```

`fields_on` 只对 `Length`/`Rel`/`Stroke`/`Alignment`/`Version` 五种类型返回非空列表（[typst-library fields.rs:77-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/fields.rs#L77-L91)）：

```rust
pub fn fields_on(ty: Type) -> &'static [&'static str] {
    if ty == Type::of::<Version>() { &Version::COMPONENTS }
    else if ty == Type::of::<Length>() { &["em", "abs"] }
    else if ty == Type::of::<Rel>() { &["ratio", "length"] }
    else if ty == Type::of::<Stroke>() { &["paint", "thickness", "cap", "join", "dash", "miter-limit"] }
    else if ty == Type::of::<Alignment>() { &["x", "y"] }
    else { &[] }
}
```

`value.field(field, ())` 取出该字段的值（[typst-library value.rs:157-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L157-L171)），`.unwrap()` 安全是因为这些字段确实属于该类型。所以 `#(5pt).` 会补出 `em`（0pt）和 `abs`（5pt），`#left.`（`left` 是 alignment）会补出 `x`/`y`。

**`match value` 分支**（[src/complete.rs:203-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L203-L244)）。

符号修饰符（[src/complete.rs:204-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L204-L215)）：

```rust
Value::Symbol(symbol) => {
    for modifier in symbol.modifiers() {
        if let Ok(modified) = symbol.clone().modified((), modifier) {
            ctx.completions.push(Completion {
                kind: CompletionKind::Symbol(modified.get().into()),
                label: modifier.into(),
                apply: None,
                detail: None,
            });
        }
    }
}
```

`symbol.modifiers()` 枚举该符号所有可用修饰符（如 `arrow` 的 `r`/`l`/`dashed` 等）。对每个修饰符尝试 `modified(())` 得到具体字形，命中则推一条 `CompletionKind::Symbol(字形串)` 的候选，`label` 是修饰符名。`apply=None` 表示直接用 label 替换。这是 `#sym.arrow.` 补出 `r`/`dashed` 的来源（见 `test_autocomplete_symbol_variants`，[src/complete.rs:1943-1950](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1943-L1950)）。注意它直接 `push`，不走 `value_completion`，因此 `kind` 恒为 `Symbol`。

Content 字段（[src/complete.rs:216-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L216-L220)）：

```rust
Value::Content(content) => {
    for (name, value) in content.fields() {
        ctx.value_completion(name, &value);
    }
}
```

补的是**这个元素实例当前拥有的字段**。测试 `test_autocomplete_content_methods`（[src/complete.rs:1937-1940](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1937-L1940)）里 `show outline.entry: it => it.` 补出 `indented`/`body`/`page`——`body`/`indented` 正是 outline entry 实例的字段，而 `page` 等可能来自元素作用域方法（4.3 的方法循环）。两条路径协同覆盖了 content 的全部可访问项。

Dict 与 Args（[src/complete.rs:221-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L221-L230)）：

```rust
Value::Dict(dict) => {
    for (name, value) in dict {
        ctx.value_completion(name.clone(), value);
    }
}
Value::Args(args) => {
    for (name, value) in &args.to_named() {
        ctx.value_completion(name.clone(), value);
    }
}
```

字典补它的键（如 `#(a:1, b:2).` 补 `a`/`b`，外加来自 dict 类型作用域的方法 `keys` 等——见 `test_autocomplete_dict_fields`，[src/complete.rs:1709-1731](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1709-L1731)）；`arguments(...)` 产生的 `Args` 补它的具名参数（见 `test_autocomplete_argument_fields`）。

Func 的 get 规则（[src/complete.rs:231-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L231-L242)）：

```rust
Value::Func(func) => {
    // Autocomplete get rules.
    if let Some((elem, styles)) = func.to_element().zip(styles.as_ref()) {
        for param in elem.params() {
            if let Some(field_accessor) = elem.settable_field_accessor(param.name) {
                let value = field_accessor(StyleChain::new(styles));
                ctx.value_completion(param.name, &value);
            }
        }
    }
}
```

这是唯一用到 `styles` 参数的分支。触发条件苛刻：对象必须是**元素函数**（`func.to_element().is_some()`），且 `analyze_expr` 必须返回了非空 `Styles`。满足时，对每个「可设置参数」，用 `settable_field_accessor`（[typst-library element.rs:170-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L170-L173)）从样式链读出当前值并作为候选。这对应 Typst 的 get 规则语法：在合适的上下文里，元素函数名加字段可读取当前样式。`styles` 为 `None`（对象是字面量或未在带样式上下文中求值）时该分支整体跳过——又一个「可选增强、优雅降级」的例子。

#### 4.4.4 代码实践

**实践目标**：把 `emoji.fa|` 从光标到候选的完整推导写出来——这是本讲的综合实践之一。

**操作步骤（纯推导，配合源码核验）**：

给定代码 `#emoji.fa`，光标在 `fa` 之后（`|` 表示光标）。

**第 1 步：形态判定。** 光标叶子是 `Ident`(`fa`)。进入 `complete_field_accesses`，`after_dot=false`（不是点号），形态 A 跳过；形态 B 检查：`prev_sibling` 是 `Dot`，`prev_prev` 是 `emoji`（`ast::Expr::Ident`，是表达式），命中（[src/complete.rs:142-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L142-L149)）。

**第 2 步：取对象值。** `analyze_expr(world, &prev_prev)`，即分析 `emoji`。`emoji` 是 `Ident`，非字面量，落到 `typst::trace`（[src/analyze.rs:45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45)）：重跑文档，`emoji` 求值为标准库的 `emoji` 模块，得 `[(Value::Module(emoji), None)]`。取第一个 → `value = Module(emoji)`，`styles = None`。

**第 3 步：`ctx.from = ctx.leaf.offset()`**，即 `fa` 的起点（替换会覆盖 `fa`）。

**第 4 步：`field_access_completions(ctx, &module, &None)`**：

- `value.ty()` = `module` 类型，其 `scope()` 一般为空（模块类型本身不挂方法）→ 方法循环无产出。
- `value.scope()` = `Some(emoji 模块作用域)`（[src/complete.rs:188-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L188-L192)）→ 成员循环枚举模块全部成员：`face`、`food`、`transport`……每个 `call_completion`。
- `fields_on(module 类型)` = `[]` → 跳过。
- `match value` → `Value::Module` 不匹配任何专门分支（Symbol/Content/Dict/Args/Func），落到 `_ => {}`。

**第 5 步：候选过滤。** LSP 客户端按 `from`（`fa` 起点）和已输入文本 `fa` 对候选做前缀过滤，最终留下以 `fa` 开头的成员——典型结果是 `face`（emoji 里的人脸子模块）。

**预期结果**：`emoji.fa|` 补出 `face`（以及任何其它以 `fa` 开头的 emoji 子模块/符号）。由于 `from` 设在 `fa` 起点，选中后 `fa` 被替换为 `face`。

> 待本地验证：可在 typst 编辑器里输入 `#emoji.fa` 触发补全，确认主候选为 `face`；亦可仿照 `test_autocomplete_field_access` 写一个测试断言 `test("#emoji.fa", -1).must_include(["face"])`。

#### 4.4.5 小练习与答案

**练习 1**：`#sym.arrow.` 的候选 `r`/`dashed` 来自哪个分支？为什么 `kind` 是 `Symbol` 而不是 `Constant`？

**答案**：来自 `match value` 的 `Value::Symbol` 分支（[src/complete.rs:204-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L204-L215)）。该分支直接 `push` 一条 `Completion`，显式把 `kind` 设为 `CompletionKind::Symbol(modified.get())`（携带修饰后的字形串），好让 LSP 客户端把符号字形渲染出来；它没有走 `value_completion`，所以不会被兜底归为 `Constant`。

**练习 2**：Func 的 get 规则分支里，为什么用 `.zip(styles.as_ref())`，`styles` 为 `None` 时会怎样？

**答案**：`.zip(styles.as_ref())` 要求 `func.to_element()` 和 `styles.as_ref()` **都为 `Some`** 才进入循环。`styles` 来自 `analyze_expr` 的返回值，只有当对象是在「带活动样式的上下文」中被 trace 捕获时才非 `None`（如字面量 `5pt` 的 `styles` 就是 `None`）。`styles` 为 `None` 时 `zip` 结果为 `None`，整个 get 规则分支跳过——即「没有样式快照就无法读出当前值，于是不补」，这是优雅降级。

**练习 3**：`#(5pt).` 会补出哪些候选？分别来自哪个分支？

**答案**：`5pt` 是 `Value::Length`。补出来源：① 长度类型的 `self` 方法（来自 `value.ty().scope()`，如 length 类型上的方法）；② `fields_on(Length)` = `["em", "abs"]`，补 `em`（值 0pt）与 `abs`（值 5pt），来自「类型预定义字段」分支（[src/complete.rs:194-201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L194-L201)）。`value.scope()` 为 `None`（Length 不是 Module/Type/Func 值），`match` 不命中专门分支，故无其它来源。

## 5. 综合实践

把本讲全部知识串起来，完成下面这个「全链路追踪」任务。

**任务**：解释 `#().` 为什么补出 `insert/remove/len/all`，并把 `emoji.fa|` 的推导补全。要求按下面的提纲写出每一步对应的源码位置。

**提纲**：

1. **分发定位**：`autocomplete` 首先调用 `complete_field_accesses`（分发链第一关，[src/complete.rs:57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L57)）。
2. **`#().` 形态 A**：光标叶子是 `Dot`，`after_dot=true, textual_dot=false`；`prev` 是 `()`（`ast::Expr`），守卫全过；`analyze_expr(())` 走 trace 得空数组 `Value::Array`（[src/complete.rs:125-139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L125-L139)）。
3. **方法补全**：`value.ty()` = `array`；方法循环遍历 `array` 类型作用域，凡首参名为 `self` 的原生函数都补出——`insert/remove/len/all` 正是如此（它们在 typst-library 里以 `&self`/`&mut self` 定义，如 [array.rs:180-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L180-L231) 附近的 `first`/`push` 等）。`value.scope()` 为 `None`，成员循环不执行；`fields_on(array)` 为空；`match Value::Array` 落到 `_`。所以候选=数组方法集合（[src/complete.rs:179-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L179-L186)）。
4. **`emoji.fa|` 形态 B**：见 4.4.4，对象 `emoji` 经 trace 得 `Value::Module`；成员循环（`value.scope()`）枚举模块成员，前缀过滤后主候选为 `face`（[src/complete.rs:142-157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L142-L157) 与 [src/complete.rs:188-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L188-L192)）。

**进阶（可选）**：仿照 `test_autocomplete_field_access`（[src/complete.rs:1679-1693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1679-L1693)）的写法，为 `emoji.fa` 新增一个测试用例（提示：用 `test("#emoji.fa", -1)` 并断言 `must_include(["face"])`），运行 `cargo test --package typst-ide` 验证你的推导。若结果与预期不符，先用本讲的流程图定位是「形态判定」「取值」还是「分支匹配」出了偏差。

## 6. 本讲小结

- `complete_field_accesses` 是补充分发链的**第一关**，识别两类互斥形态：`expr.`（形态 A，光标在点号）与 `expr.id|`（形态 B，光标在半成品标识符）。
- markup/math 里的「文本点号」（`Text`/`MathText` 且文本为 `"."`）需要额外检查：点号前不能有 trivia 空白，且正文里要求对象由 `#` 引入，避免正文句号误触发。
- 两类形态都把 `analyze_expr` 作用在**对象表达式**（点号左边的 `prev` / `prev_prev`）上，取一个代表性值；`ctx.from` 在形态 A 设为光标、形态 B 设为标识符起点。
- `field_access_completions` 有五大补全来源：① 类型/元素作用域里的 **`self` 方法**（`scope().iter()` + 首参名过滤）；② 模块/类型/函数自身作用域的**全部成员**（`value.scope()`）；③ `fields_on` 列出的**类型预定义字段**；④ `match value` 里的**符号修饰符 / Content 字段 / Dict 键 / Args 具名项**；⑤ 元素函数的 **get 规则**（唯一依赖 `styles`）。
- 「方法循环」用 `call_completion`（函数带括号），「结构化字段」用 `value_completion`（数据不带括号）——前者是调用，后者是取值。

## 7. 下一步学习建议

- **u6-l3（import、路径、包、字体、标签补全）**：转向另一组「特定场景」补全，看 `complete_imports` 如何与 `analyze_import`（u2-l4）配合完成跨文件补全。
- **u6-l4（scope_completions 与类型驱动补全）**：对比本讲的「按值类型补全」与那里「按作用域 + `CastInfo` 类型补全」，理解 `scope_completions` 里 `check_value_recursively`（u2-l5）如何让「含目标类型的容器」也参与补全。
- **重温 u2-l4 / u3-l3**：本讲反复依赖 `analyze_expr` 与 trace 机制；若对「为什么重跑整篇文档才能拿到值」仍有疑问，可回看这两讲。
- **源码延伸**：阅读 typst-library 里某个类型（如 `array.rs` 或 `str.rs`）的方法定义，观察 `#[func]` 宏如何把带 `&self` 的 Rust 方法注册进类型作用域，从而被本讲的「方法循环」发现——这能帮你彻底打通「语言层方法」与「IDE 层补全」的关系。
