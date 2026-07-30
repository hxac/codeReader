# 讲义 u6-l1：set/show 规则与函数参数补全

## 1. 本讲目标

本讲精读 `typst-ide` 补全引擎（`src/complete.rs`）中两条「特定场景」补全链路：**规则补全**（`complete_rules`）与**函数参数补全**（`complete_params`）。

学完后你应当能够：

1. 说清 `#set |`、`#show |`、`#show strong: |` 三处光标各自走哪条补全分支、产出什么候选，以及背后的过滤条件（set 只补「可设置」的函数、show 选择器只补「元素函数」）。
2. 说清 `complete_params` 如何用 `deciding` 节点回溯定位，判断此刻该补「参数名」还是「参数值」。
3. 说清 `param_completions` 如何统计已填的位置/具名参数并对它们去重，从而只补剩余的参数。
4. 说清 `named_param_value_completions` 在 `fill:|` 这类冒号后如何工作。

本讲承接 [u5-l3](./u5-l3-mode-completions.md) 的补全分发管线，是分发链中排在「通用模式补全」之前的「特定补全」之一。

## 2. 前置知识

在进入本讲前，请确认你已掌握以下概念（均在 [u5-l1](./u5-l1-autocomplete-dispatch.md) 与 [u5-l3](./u5-l3-mode-completions.md) 建立）：

- **`autocomplete` 短路分发链**：入口先用 `||` 把若干 `complete_*` 串成短路表达式，**顺序即优先级**，首个返回 `true` 的分支胜出，后续不再执行。
- **`CompletionContext`**：贯穿全流程的可变共享上下文，各分支往 `ctx.completions` 写入候选、按需前移 `ctx.from`，最终返回 `(ctx.from, ctx.completions)`。
- **`ctx.from`**：补全的「替换起点」。默认等于 `cursor`（纯插入），各分支可把它回退到已输入 token 的开头，让 LSP 知道从哪里开始替换。
- **`mode_after()`**：返回光标处的 `SyntaxMode`（Markup/Math/Code），并在注释/raw 正文里返回 `None`，使入口提前退出。
- **`analyze_expr_with_fallback`**（见 [u2-l4](./u2-l4-analyze-values.md)）：把一个被调用者节点推断成 `Value::Func`，是参数补全拿到函数元信息（参数表）的入口。

本讲还用到两个工具方法（详见 [u5-l2](./u5-l2-completion-model.md)）：

- `ctx.snippet_completion(label, snippet, docs)`：快捷产出一条 `kind = Syntax` 的片段候选。
- `ctx.enrich(prefix, suffix)`：给当前所有候选的 `apply` 统一套上前后缀。
- `ctx.scope_completions(parens, filter)`：把作用域命名项 + 全局标准库合并，并用 `filter` 过滤（局部优先）。

> 术语提醒：本讲频繁出现 Typst 的 `set` / `show` 规则。`set` 规则形如 `#set text(size: 12pt)`，用来给元素函数设置默认样式；`show` 规则形如 `#show strong: it => ...`，由「选择器 + 配方（recipe）」两部分组成。本讲的补全正是为这两类规则的各个「空位」服务。

## 3. 本讲源码地图

本讲只涉及一个源文件，但横跨其中六组函数：

| 函数 | 位置 | 作用 |
|---|---|---|
| `complete_rules` | `src/complete.rs:332` | 规则补全总入口，识别 `set` / `show` / `show: ` 三个触发点 |
| `set_rule_completions` | `src/complete.rs:368` | 在 `set` 后补「至少有一个可设置参数」的函数 |
| `show_rule_selector_completions` | `src/complete.rs:378` | 在 `show` 后补元素函数与文本/正则选择器片段 |
| `show_rule_recipe_completions` | `src/complete.rs:400` | 在 `show xxx: ` 后补 recipe 片段与函数 |
| `complete_params` | `src/complete.rs:428` | 参数补全总入口，定位参数列表并用 `deciding` 节点分派 |
| `param_completions` | `src/complete.rs:495` | 统计已填参数、补剩余的位置/具名参数 |

此外会顺带用到 `named_param_value_completions`（[src/complete.rs:566](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L566-L584)，冒号后补参数值）与 `param_value_completions`（[src/complete.rs:587](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L600)，按参数元信息产出值候选）这两个下游函数。

这两组函数在分发链里的位置见 [src/complete.rs:57-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L57-L67)：字段访问 → 开放标签 → import → **规则** → **参数** → 通用模式。注意它们排在通用补全**之前**——也就是说，只要命中规则或参数补全，就不会再走到 `complete_markup/math/code`。

---

## 4. 核心概念与源码讲解

### 4.1 complete_rules：规则补全的总入口与三个触发点

#### 4.1.1 概念说明

`complete_rules` 负责识别「用户正在写一条 `set` / `show` 规则」的场景，并按规则内部的三个不同空位分派到不同的候选生成函数。它是「**先看光标前一个叶子是什么关键字/符号，再决定补什么**」的典型实现。

#### 4.1.2 核心流程

```
complete_rules(ctx):
  1. 守卫：若当前叶子不是 trivia（空白/注释等），直接返回 false
     —— 只在「关键字 + 空白」的间隙里触发，避免在 token 中间误触
  2. prev = leaf.prev_leaf()      # 取前一个叶子节点
  3. if prev == Set:     → set_rule_completions          # "set |"
  4. if prev == Show:    → show_rule_selector_completions # "show |"
  5. 若 prev.prev 是 Colon 且 Colon 的父节点是 ShowRule:
                        → show_rule_recipe_completions   # "show strong: |"
  6. 否则返回 false
```

三个触发点互斥短路，命中一个即返回 `true`。注意第 5 步针对的是「show 规则已经写到冒号之后」的半成品——此时要补的是 recipe（配方），而不是选择器。

#### 4.1.3 源码精读

完整实现见 [src/complete.rs:332-365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L332-L365)。关键片段：

```rust
// 直接贴着关键字时不补全（避免在 "set|" 中间触发）
if !ctx.leaf.kind().is_trivia() {
    return false;
}
let Some(prev) = ctx.leaf.prev_leaf() else { return false };

// set 关键字之后："set |"
if matches!(prev.kind(), SyntaxKind::Set) {
    ctx.from = ctx.cursor;
    set_rule_completions(ctx);
    return true;
}

// show 关键字之后："show |"
if matches!(prev.kind(), SyntaxKind::Show) {
    ctx.from = ctx.cursor;
    show_rule_selector_completions(ctx);
    return true;
}

// show 规则冒号之后："show strong: |"
if let Some(prev) = ctx.leaf.prev_leaf()
    && matches!(prev.kind(), SyntaxKind::Colon)
    && matches!(prev.parent_kind(), Some(SyntaxKind::ShowRule))
{
    ctx.from = ctx.cursor;
    show_rule_recipe_completions(ctx);
    return true;
}
```

三段都用 `ctx.from = ctx.cursor`：规则补全都是「从头插入新 token」，没有需要替换的已输入文本，所以替换起点就是光标本身。

#### 4.1.4 代码实践

**实践目标**：验证三处触发点的边界，理解「trivia 守卫」的作用。

**操作步骤**：

1. 打开 `src/complete.rs` 的测试模块（约 1497 行起的 `mod tests`）。
2. 在其中新增一个测试，复用现成的 `test(world, pos)` 助手（explicit 模式）：

   ```rust
   #[test]
   fn test_rules_triggers() {
       // "set " 后光标在空格处
       test("#set ", -1).must_include(["text", "page"]);
       // 光标紧贴关键字（无空格）→ 不应触发规则补全
       test("#set", -1).must_exclude(["page"]);
   }
   ```

3. 运行 `cargo test -p typst-ide test_rules_triggers`。

**需要观察的现象**：第一个断言通过（`#set ` 后给出可设置函数）；第二个断言中，因为光标落在 `set` 这个非 trivia 叶子上，`complete_rules` 第一个守卫 `if !ctx.leaf.kind().is_trivia()` 直接返回 `false`，规则补全不触发。

**预期结果**：两条断言均通过。`must_include` / `must_exclude` 的实现见 [src/complete.rs:1549-1570](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1549-L1570)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `complete_rules` 要先判断 `is_trivia()`，而不是直接判断前一个叶子是否为关键字？

**参考答案**：如果当前叶子本身是某个 token（比如光标正落在 `set` 内部），说明用户还在编辑这个 token，此时补全会和正在输入的文本冲突；只有当光标停在 trivia（空白/换行）上、即「关键字已经写完、正准备输入下一个 token」时，补全才有意义。这也呼应了 [u2-l1](./u2-l1-cursor-to-syntax-node.md) 中「`leaf_at` 不主动跳过 trivia，是否过滤由调用方决定」的策略。

---

### 4.2 set_rule_completions：set 规则只补「可设置」的函数

#### 4.2.1 概念说明

并非所有函数都能出现在 `set` 之后。Typst 规定：只有**至少拥有一个可设置（settable）参数**的元素函数才能被 `set`。比如 `#set text(...)` 合法，而 `#set assert(...)` 没有意义。`set_rule_completions` 就是把这条规则编码成一道过滤。

#### 4.2.2 核心流程

```
set_rule_completions(ctx):
  scope_completions(parens=true, filter = |value| {
      value 是 Func，且 func.params() 中存在某个 param.settable() 为真
  })
```

它直接复用 `scope_completions`（见 [u5-l2](./u5-l2-completion-model.md) / [src/complete.rs:1426-1462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1426-L1462)）做「作用域 + 标准库」合并，只是把 `filter` 收紧成「带可设置参数的函数」。`parens=true` 表示函数候选会自动带上括号（如 `text()`）。

#### 4.2.3 源码精读

见 [src/complete.rs:368-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L368-L375)：

```rust
fn set_rule_completions(ctx: &mut CompletionContext) {
    ctx.scope_completions(true, |value| {
        matches!(
            value,
            Value::Func(func) if func.params().any(|param| param.settable())
        )
    });
}
```

两处要点：

- `Value::Func(func) if ...` 是 Rust 的 **`let`-chain 守卫**：先匹配值是函数，再在绑定 `func` 上施加额外条件。
- `func.params().any(|param| param.settable())`：只要参数表里**任意一个**参数可设置，就允许这个函数出现在 `set` 之后。这是「存在量词」语义。

> 串联：这里的 `settable()` 与 4.5 节 `param_completions` 里 `if set && !param.settable() { continue; }` 是同一个属性的两种用法——前者用来「决定哪些函数能被 set」，后者用来「决定被 set 的函数里哪些参数能写」。

#### 4.2.4 代码实践

**实践目标**：验证「无可设置参数的函数不会出现在 `set` 补全里」。

**操作步骤**：在测试模块新增：

```rust
#[test]
fn test_set_rule_filter() {
    // text 有可设置参数（如 size/font），应出现
    test("#set ", -1).must_include(["text"]);
    // assert 无可设置参数，不应出现
    test("#set ", -1).must_exclude(["assert", "repr"]);
}
```

运行 `cargo test -p typst-ide test_set_rule_filter`。

**预期结果**：通过。`assert`、`repr` 等纯函数不在候选中。若想进一步确认，可在 `set_rule_completions` 内临时加一行 `eprintln!("set cand: {:?}", value.repr());` 观察被过滤掉的函数。

#### 4.2.5 小练习与答案

**练习 1**：`#set text(size: 12pt)` 已写完后，把光标放进括号（`#set text(|)`），补全会走哪条分支？为什么 `body`（内容位置参数）不会出现？

**参考答案**：会走 `complete_params`（4.4 节），并且因为外层表达式是 `SetRule`，`complete_params` 会把 `set=true` 传给 `param_completions`；后者在 [src/complete.rs:525-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L525-L527) 用 `if set && !param.settable() { continue; }` 跳过所有不可设置的参数。`body` 是不可设置的位置参数，故被跳过。

---

### 4.3 show_rule_selector_completions 与 show_rule_recipe_completions

#### 4.3.1 概念说明

`show` 规则比 `set` 复杂，它由两部分构成：`#show <选择器>: <配方>`。因此 `show` 关键字之后要补的是**选择器**（决定「对谁生效」），而冒号之后要补的是**配方**（决定「替换成什么」）。这两个函数分别服务这两个空位。

选择器位置的合法值有两类：

- 一个**元素函数**（如 `heading`、`strong`），表示「对这类元素生效」；
- 一个**文本/正则选择器**（如 `"TODO":` 或 `regex("..."):`），表示「对匹配文本生效」。

#### 4.3.2 核心流程

```
show_rule_selector_completions(ctx):           # "show |"
  1. scope_completions(parens=false, filter = 元素函数)
     —— func.to_element().is_some() 才算元素函数
  2. enrich("", ": ")   # 给所有候选追加 ": "，省得用户手敲冒号
  3. snippet "text selector"   -> "${text}"": ${}"
  4. snippet "regex selector"  -> regex("${regex}"): ${}"

show_rule_recipe_completions(ctx):             # "show strong: |"
  1. snippet "replacement"          -> [${content}]
  2. snippet "replacement (string)" -> "${text}"
  3. snippet "transformation"       -> element => [${content}]
  4. scope_completions(parens=false, filter = 任意函数)
```

注意选择器分支用 `func.to_element().is_some()` 过滤（只要元素函数），而配方分支用 `Value::Func(_)` 过滤（任意函数都能做 recipe，因为配方可以是「把元素变换成内容」的函数）。

#### 4.3.3 源码精读

选择器见 [src/complete.rs:378-397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L378-L397)：

```rust
fn show_rule_selector_completions(ctx: &mut CompletionContext) {
    ctx.scope_completions(
        false,
        |value| matches!(value, Value::Func(func) if func.to_element().is_some()),
    );
    ctx.enrich("", ": ");   // 选中后自动补出冒号
    ctx.snippet_completion(
        "text selector", "\"${text}\": ${}", "Replace occurrences of specific text.",
    );
    ctx.snippet_completion(
        "regex selector", "regex(\"${regex}\"): ${}", "Replace matches of a regular expression.",
    );
}
```

配方见 [src/complete.rs:400-420](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L400-L420)：

```rust
fn show_rule_recipe_completions(ctx: &mut CompletionContext) {
    ctx.snippet_completion("replacement", "[${content}]", "Replace the selected element with content.");
    ctx.snippet_completion("replacement (string)", "\"${text}\"", "Replace the selected element with a string of text.");
    ctx.snippet_completion("transformation", "element => [${content}]", "Transform the element with a function.");
    ctx.scope_completions(false, |value| matches!(value, Value::Func(_)));
}
```

两段都把 `parens` 设为 `false`：选择器/配方里直接出现 `text`、`strong` 这样的裸名字，不需要自动加括号。选择器分支的 `enrich("", ": ")` 是个贴心设计——用户选了 `strong` 后，编辑器会直接得到 `strong: `，光标停在冒号后，无缝衔接下一段「补配方」的补全。

#### 4.3.4 代码实践

**实践目标**：观察选择器分支的 `enrich("", ": ")` 效果。

**操作步骤**：

```rust
#[test]
fn test_show_selector_enrich() {
    let res = test("#show ", -1);
    // heading 是元素函数，应出现
    res.must_include(["heading"]);
    // apply 应被 enrich 追加 ": "
    res.at("heading").must_apply_as("heading: ");
}
```

运行 `cargo test -p typst-ide test_show_selector_enrich`。`must_apply_as` 断言候选的 `apply` 字段，实现见 [src/complete.rs:1586-1591](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1586-L1591)。

**预期结果**：通过；`heading` 的 `apply` 为 `"heading: "`，证明 `enrich` 在已有候选项上加了后缀。

#### 4.3.5 小练习与答案

**练习 1**：为什么选择器分支用 `to_element().is_some()`，而配方分支用任意 `Value::Func(_)`？

**参考答案**：选择器必须指向一个「能被 set/show 的元素」（如 `heading`、`paragraph`），`to_element()` 返回 `Some` 表示该函数对应一个元素类型；非元素函数（如 `calc.sin`）做选择器没有意义。而配方是「接收被选中元素、返回内容」的函数，任何函数都可能担当此角色，故只要求是函数即可，不再限定为元素函数。

---

### 4.4 complete_params：定位参数列表与 deciding 节点回溯

#### 4.4.1 概念说明

`complete_params` 处理「光标在一个函数调用或 set 规则的参数列表里」的场景。它要做两件事：

1. **定位**：从光标叶子向上找到「参数列表 `Args` + 被调用者 `callee`」，并判断这是不是一条 set 规则。
2. **分派**：用一个叫 `deciding` 的节点回溯定位，判断此刻该补「参数名」还是「参数值」。

第二步是本节的灵魂：函数调用的语法里，`(`、`)`、`,`、`:` 这四个结构性符号各自暗示了不同的「空位」。

#### 4.4.2 核心流程

**定位阶段**（[src/complete.rs:430-449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L430-L449)）：

```
从 leaf.parent() 出发：
  - 若父节点是 Named（具名参数 fill:...），再上跳一层到 Args
  - 把父节点 cast 成 ast::Args（参数列表）
  - 取祖父 cast 成 ast::Expr，判断是否 SetRule → set 标志
  - 取 callee：FuncCall → call.callee()；SetRule → set.target()
  - 在祖父子树里 find(callee.span()) 拿到带位置的 callee 节点
任一步失败 → 返回 false（不在参数列表里）
```

**deciding 回溯阶段**（[src/complete.rs:452-462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L452-L462)）：

```
deciding = leaf
while deciding 不是 ( ) , : 之一:
    deciding = deciding.prev_leaf()   # 一直向左找前一个叶子
    找不到就 break
```

**分派阶段**（基于 `deciding` 的种类）：

| `deciding` 种类 | 附加条件 | 走哪条 | 含义 |
|---|---|---|---|
| `Colon`，且前一叶子能 cast 成 `Ident` | — | `named_param_value_completions` | `fill:` 后补**值** |
| `LeftParen` | — | `param_completions` | `rect(` 后补**参数名** |
| `Comma` | `逗号.end < cursor` **或** `explicit` | `param_completions` | `a, ` 后补**参数名** |
| `RightParen` 或其它 | — | 都不匹配 → 返回 `false` | `rect()` 闭合后不补 |

`Comma` 的附加条件是降噪设计：**光标紧贴逗号之后（无空格、非显式触发）时不补**，避免用户每敲一个逗号就弹出一长串参数。只有当逗号和光标之间有间隔（`a, |`），或者用户显式请求补全（`Ctrl+Space`）时，才补参数。

#### 4.4.3 源码精读

定位 + deciding 回溯见 [src/complete.rs:428-492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L428-L492)，关键片段：

```rust
// deciding 回溯：找到决定「补什么」的结构性符号
let mut deciding = ctx.leaf.clone();
while !matches!(
    deciding.kind(),
    SyntaxKind::LeftParen | SyntaxKind::RightParen
        | SyntaxKind::Comma | SyntaxKind::Colon
) {
    let Some(prev) = deciding.prev_leaf() else { break };
    deciding = prev;
}

// 冒号 → 补参数值："func(param:|)", "func(param: |)"
if let SyntaxKind::Colon = deciding.kind()
    && let Some(prev) = deciding.prev_leaf()
    && let Some(param) = prev.get().cast::<ast::Ident>()
{
    if let Some(next) = deciding.next_leaf() {
        ctx.from = ctx.cursor.min(next.offset());
    }
    named_param_value_completions(ctx, &callee, &param);
    return true;
}

// 左括号 / 逗号 → 补参数名："func(|)", "func(hi|)", "func(12, |)"
if let SyntaxKind::LeftParen | SyntaxKind::Comma = deciding.kind()
    && (deciding.kind() != SyntaxKind::Comma
        || deciding.range().end < ctx.cursor
        || ctx.explicit)
{
    if let Some(next) = deciding.next_leaf() {
        ctx.from = ctx.cursor.min(next.offset());
    }
    param_completions(ctx, &callee, set, args, args_linked);
    return true;
}
```

注意 `ctx.from = ctx.cursor.min(next.offset())`：把替换起点设为「光标」与「deciding 后第一个叶子起点」的较小值。这让已经输入了半个标识符（如 `func(fi|`）时，补全能正确替换掉 `fi` 而不是从光标处追加。

#### 4.4.4 代码实践

**实践目标**：观察「紧贴逗号」与「逗号后有空格」在 explicit / implicit 下的差异（对应降噪设计）。

**操作步骤**：仓库已内置该测试 `test_autocomplete_in_function_params_after_comma_and_colon`，见 [src/complete.rs:1979-1995](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1979-L1995)：

```rust
let document = "#text(size: 12pt, [])";
// 逗号后（pos 17）：explicit 给 font，implicit 为空
test(document, 17).must_include(["font"]);
test_implicit(document, 17).must_be_empty();
```

直接运行 `cargo test -p typst-ide test_autocomplete_in_function_params_after_comma_and_colon`。

**需要观察的现象**：同一位置 17（逗号后紧接空格），`test`（explicit=true）给出 `font` 等参数，而 `test_implicit`（explicit=false）返回空。

**预期结果**：测试通过。这正是 `deciding.kind() == Comma && deciding.range().end < ctx.cursor` 这一条件的体现——此处由 `explicit` 兜底触发。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `RightParen` 被列为 deciding 的「停止符号」，却没有任何分支处理它？

**参考答案**：`RightParen` 是为了让回溯**停下来**——当光标在 `func()|`（闭合括号之后）时，回溯会落在 `)` 上，但分派表里没有匹配 `RightParen` 的分支，于是 `complete_params` 返回 `false`，把机会让给后续的通用补全。也就是说，`RightParen` 是一个「到此为止、且明确不补」的哨兵，对应「闭合括号之后不在参数列表内」这一事实（测试 `#numbering()` 在 -1 处 `must_exclude(["string"])` 印证了这点）。

---

### 4.5 param_completions：跳过已填参数、补剩余参数

#### 4.5.1 概念说明

拿到被调用函数后，`param_completions` 负责「列出还能填的参数」。它的核心难点是**去重**：已经填过的位置参数和具名参数不能再补，否则用户会看到一堆已经写过的选项。

#### 4.5.2 核心流程

```
param_completions(ctx, callee, set, args, args_linked):
  1. value = analyze_expr_with_fallback(world, callee)   # 推断 callee 的值
  2. func = value.cast::<Func>()                          # 必须是函数
  3. 统计已填参数：
       existing_positional = 光标之前的位置参数个数
       existing_named      = { 所有具名参数的名字集合 }
  4. 遍历 func.params()：
       - 若 set 且参数不可设置 → 跳过
       - 位置参数：若还没被 existing_positional 占满 → 补该参数的值候选
       - 具名参数：若名字已在 existing_named → 跳过；否则补 "name: ${}"
  5. 若 before 以逗号结尾 → enrich(" ", "") 给所有候选加前导空格
```

#### 4.5.3 源码精读

统计已填参数见 [src/complete.rs:506-521](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L506-L521)：

```rust
let mut existing_positional = 0;
let mut existing_named = FxHashSet::default();
for arg in args.items() {
    match arg {
        ast::Arg::Pos(_) => {
            // 只统计「光标之前已写完」的位置参数
            let Some(node) = args_linked.find(arg.span()) else { continue };
            if node.range().end < ctx.cursor {
                existing_positional += 1;
            }
        }
        ast::Arg::Named(named) => {
            existing_named.insert(named.name().as_str());
        }
        ast::Arg::Spread(_) => {}   // 展开参数 ..args 不计入
    }
}
```

注意位置参数的统计带「光标之前」判断：`node.range().end < ctx.cursor`。这意味着用户**正在输入**的那个位置参数不会被当作「已填」，避免错位计数。具名参数则无此判断——只要名字出现过就计入集合。

参数遍历与去重见 [src/complete.rs:523-558](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L523-L558)：

```rust
let mut skipped_positional = 0;
for param in func.params() {
    if set && !param.settable() { continue; }      // set 规则只留可设置参数

    if param.positional() {
        // 位置参数按出现顺序「消费」existing_positional 个名额
        if skipped_positional < existing_positional && !param.variadic() {
            skipped_positional += 1;
            continue;
        }
        param_value_completions(ctx, &func, &param);
    }

    if let Some(name) = param.name() && param.named() {
        if existing_named.contains(name) { continue; }   // 已填的具名参数跳过

        // caption 参数特殊：用 content 语法 [..]，其余用 ${}
        let apply = if param.name() == Some("caption") {
            eco_format!("{name}: [${{}}]")
        } else {
            eco_format!("{name}: ${{}}")
        };

        ctx.completions.push(Completion {
            kind: CompletionKind::Param,
            label: name.into(),
            apply: Some(apply),
            detail: find_param_docs(ctx.world, &param).map(|docs| docs.summary()),
        });
    }
}
```

两个去重机制互补：

- **位置参数**用计数器 `skipped_positional`：按参数声明顺序，前 `existing_positional` 个被「消费掉」（`continue` 跳过）。可变参数（`variadic`，如 `..args`）永不跳过。
- **具名参数**用集合 `existing_named`：名字命中即跳过。

具名候选的 `apply` 有个小特例：参数名是 `caption` 时用 `caption: [${}]`（content 语法），其余统一 `name: ${}`。`detail` 来自 `find_param_docs`（见 [u3-l1](./u3-l1-docs.md)）。

末尾的 `if ctx.before.ends_with(',') { ctx.enrich(" ", ""); }`（[src/complete.rs:560-562](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L560-L562)）是个排版细节：逗号后还没敲空格时，给每个候选加一个前导空格，保证 `fill: red, width:...` 中间有空格。

#### 4.5.4 代码实践

**实践目标**：验证 `#rect(fill: red, |)` 如何跳过 `fill`、补出剩余可设置参数。这是本讲的核心实践。

**操作步骤**：

1. 先在源码里人工推演：`rect` 是 `FuncCall`（`set=false`），`existing_named = {"fill"}`，`existing_positional = 0`。遍历 `rect` 的具名参数时，`fill` 命中 `existing_named` 被 `continue` 跳过；`width`、`height`、`stroke`、`inset` 等不命中，各产出 `width: ${}`、`height: ${}` 等候选。

2. 新增测试验证：

   ```rust
   #[test]
   fn test_rect_skips_filled_param() {
       let res = test("#rect(fill: red, )", -2);
       // fill 已填 → 不应再出现
       res.must_exclude(["fill"]);
       // 其它可设置参数应出现
       res.must_include(["width", "height"]);
       // 验证 apply 形式（含前导空格，因为逗号后无空格）
       res.at("width").must_apply_as(" width: ${}");
   }
   ```

3. 运行 `cargo test -p typst-ide test_rect_skips_filled_param`。

**需要观察的现象**：候选里没有 `fill`，但有 `width`/`height` 等；`width` 的 `apply` 为 `" width: ${}"`（前导空格由末尾 `enrich(" ", "")` 产生，因为 `before` 以 `,` 结尾、其后无空格）。

**预期结果**：全部断言通过。**待本地验证** `width`/`height` 等具体名字是否齐全（取决于 `rect` 实际声明的可设置参数），可先用 `res.labels()` 打印全量候选确认。

#### 4.5.5 小练习与答案

**练习 1**：若把 `#rect(fill: red, )` 改成 `#set rect(fill: red, )`，候选会有什么变化？

**参考答案**：`set=true`，于是 [src/complete.rs:525-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L525-L527) 的 `if set && !param.settable() { continue; }` 生效，**所有不可设置的参数都会被跳过**。对 `rect` 而言，位置内容参数等不可设置项不会出现；只保留可设置的具名参数（且 `fill` 仍因已在 `existing_named` 中被跳过）。

**练习 2**：为什么位置参数用「计数器」去重，而具名参数用「集合」去重？

**参考答案**：位置参数没有名字，只能按**出现顺序**与参数声明顺序一一对应——前 N 个位置实参占用前 N 个声明的位置形参，所以用计数器 `skipped_positional` 顺序消费即可。具名参数靠名字唯一定位，天然适合用哈希集合 `existing_named` 做 O(1) 查重。两种去重方式分别匹配两种参数的语义。

---

### 4.6 named_param_value_completions：冒号后补参数值

#### 4.6.1 概念说明

当 deciding 是冒号（`fill:|`）时，要补的不再是「参数名」，而是这个参数**期望的值**。`named_param_value_completions` 负责把「参数名」解析回它的类型元信息，再交给 `param_value_completions` 产出对应类型的值候选（如颜色、对齐、布尔等）。

#### 4.6.2 核心流程

```
named_param_value_completions(ctx, callee, name):
  1. func = analyze_expr_with_fallback(world, callee).cast::<Func>()
  2. param = func.param(name)     # 按名字查参数元信息
  3. 若 param 不存在 或 不可具名 → 直接返回
  4. param_value_completions(ctx, &func, &param)
  5. 若 before 以 ':' 结尾 → enrich(" ", "")   # fill:| 时自动加空格
```

`param_value_completions`（[src/complete.rs:587-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L600)）做三类特化 + 通用类型展开：

- 参数名是 `font` → `ctx.font_completions()`（补字体家族）；
- 该参数被 `path_completion` 识别为路径参数（如 `image(source:)`）→ 按扩展名补文件；
- `figure` 的 `body` → 补 image/table 专属片段；
- 对原生参数 `ParamInfo::Native` → `ctx.cast_completions(&param.input)`，按参数的 `CastInfo` 递归展开合法值（详见 [u6-l4](./u6-l4-scope-and-cast.md)）。

#### 4.6.3 源码精读

见 [src/complete.rs:566-584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L566-L584)：

```rust
fn named_param_value_completions(ctx: &mut CompletionContext, callee: &LinkedNode, name: &str) {
    let Some(value) = analyze_expr_with_fallback(ctx.world, callee) else { return };
    let Ok(func) = value.cast::<Func>() else { return };

    let Some(param) = func.param(name) else { return };
    if !param.named() { return; }

    param_value_completions(ctx, &func, &param);

    if ctx.before.ends_with(':') {
        ctx.enrich(" ", "");
    }
}
```

`param_value_completions` 见 [src/complete.rs:587-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L600)：

```rust
fn param_value_completions(ctx: &mut CompletionContext, func: &Func, param: &ParamInfo) {
    if param.name() == Some("font") {
        ctx.font_completions();
    } else if let Some(extensions) = path_completion(func, param) {
        ctx.file_completions_with_extensions(extensions);
    } else if func.name() == Some("figure") && param.name() == Some("body") {
        ctx.snippet_completion("image", "image(\"${}\"),", "An image in a figure.");
        ctx.snippet_completion("table", "table(\n  ${}\n),", "A table in a figure.");
    }

    if let ParamInfo::Native(param) = param {
        ctx.cast_completions(&param.input);
    }
}
```

#### 4.6.4 代码实践

**实践目标**：验证 `#rect(fill:|)` 处会补出颜色（以及「含颜色的容器」），并理解空格前缀的来源。这是本讲实践任务的第二部分。

**操作步骤**：仓库已内置近似测试 `test_autocomplete_value_filter`，见 [src/complete.rs:1848-1858](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1848-L1858)：

```rust
let world = TestWorld::new("#import \"design.typ\": clrs; #rect(fill: )")
    .with_source("design.typ", "#let clrs = (a: red, b: blue); #let nums = (a: 1, b: 2)");
test(&world, -2)
    .must_include(["clrs", "aqua"])
    .must_exclude(["nums", "a", "b"]);
```

运行 `cargo test -p typst-ide test_autocomplete_value_filter`。

**需要观察的现象**：`fill:` 之后出现颜色（如 `aqua`）和「内部含颜色的模块/字典」`clrs`，但不出现纯数字字典 `nums`。

**推演**：`fill` 是原生参数，其 `CastInfo` 是颜色类型，`cast_completions` 按类型展开出全部颜色；`clrs` 之所以入选，是因为 `scope_completions`/`cast_completions` 背后用 `check_value_recursively`（见 [u2-l5](./u2-l5-utils.md)）判定「容器内含目标类型的值也算」。关于 `cast_completions` 的递归展开细节，留待 [u6-l4](./u6-l4-scope-and-cast.md) 展开。

**预期结果**：测试通过。

#### 4.6.5 小练习与答案

**练习 1**：`#text(font:|)` 与 `#rect(fill:|)` 走的是同一段 `named_param_value_completions`，为何前者补字体、后者补颜色？

**参考答案**：因为 `param_value_completions` 第一步就按参数名特化——`param.name() == Some("font")` 时调用 `ctx.font_completions()` 走字体分支（[src/complete.rs:588-589](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L588-L589)）；`fill` 不命中这些特化，落到末尾 `cast_completions(&param.input)` 按颜色类型展开。也就是说，「字体」是按**名字**硬编码的特殊参数，其余参数则按**类型**自动展开。

---

## 5. 综合实践

把本讲两条链路串起来，完成下面这个「规则 + 参数」的端到端跟踪任务。

**任务**：给定下面这段 Typst 源码（光标用 `|` 表示），逐步推断 `autocomplete` 会命中哪条分支、产出哪些候选：

```typst
#set text(size: 11pt)
#show heading: it => {
  set text(font: |)
  [大标题: #it.body]
}
```

请按顺序回答：

1. **第一处光标**（`#set text(size: 11pt)` 写完后无光标干扰，仅作上下文）：若把光标放在 `#set text(|)`，会命中哪个函数？为什么 `body` 不会出现？
2. **第二处光标**（`#show heading: ` 的冒号后）：写到这里时，`complete_rules` 的哪个触发点生效？产出哪些候选？
3. **第三处光标**（`set text(font: |)` 的冒号后）：走 `complete_params` 的哪条分派？最终 `param_value_completions` 会调用哪个特化分支补出什么？

**参考答案**：

1. 命中 `complete_params`（`deciding=LeftParen`）。因外层是 `SetRule`，`set=true`，`param_completions` 用 `if set && !param.settable() { continue; }` 跳过所有不可设置参数；`body` 是不可设置的位置参数，故不出现，只补 `size`/`font`/`fill` 等可设置具名参数（`size` 若已填则被 `existing_named` 跳过）。
2. 命中 `complete_rules` 的第三个触发点（`show xxx: `，[src/complete.rs:355-362](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L355-L362)），走 `show_rule_recipe_completions`，产出 `[content]`、`"text"`、`element => [content]` 三个片段 + 任意函数候选。
3. `deciding=Colon` 且前一叶子是 `font` 这个 `Ident`，走 `named_param_value_completions`（[src/complete.rs:465-475](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L465-L475)）。`param_value_completions` 命中 `param.name() == Some("font")` 分支，调用 `ctx.font_completions()` 补出全部字体家族；因在 `#show math.equation:` 下还会过滤为只补数学字体（`is_in_equation_show_rule`，见 [src/complete.rs:1122-1133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1122-L1133)，但本例是普通 `heading`，不触发该过滤）。

**进阶（可选）**：把上述三处分别用 `test(...)` / `test_implicit(...)` 写成测试，运行 `cargo test -p typst-ide` 验证你的推断。

## 6. 本讲小结

- `complete_rules` 用「光标前一个叶子」识别 `set` / `show` / `show: ` 三个触发点，且只在 trivia 上触发（避免在 token 中间误触）。
- `set_rule_completions` 用 `func.params().any(|p| p.settable())` 过滤，只补「带可设置参数」的函数；`show` 选择器用 `to_element().is_some()` 只补元素函数，并用 `enrich("", ": ")` 自动补冒号。
- `show_rule_recipe_completions` 在冒号后补三类 recipe 片段（content / string / 转换函数）+ 任意函数。
- `complete_params` 先向上定位「参数列表 + callee + 是否 set 规则」，再用 `deciding` 节点回溯到 `(`/`)`/`,`/`:` 之一来分派。
- 分派规则：冒号→补参数值；左括号/逗号（逗号需非紧贴或 explicit）→补参数名；右括号→不补。
- `param_completions` 用「计数器」去重位置参数、用「集合」去重具名参数，并在 set 规则下额外跳过不可设置参数；`named_param_value_completions` 把冒号后交给 `param_value_completions`，按参数名（font/path/figure body）或类型（`cast_completions`）产出值候选。

## 7. 下一步学习建议

- 本讲的「冒号后补值」最终汇聚到 `cast_completions` 的类型递归展开，这正是 [u6-l4 scope_completions 与类型驱动补全](./u6-l4-scope-and-cast.md) 的主题，建议紧接着学习。
- 若想了解「点号后补字段/方法」（`#().` 补出 `insert`/`len` 等），参见 [u6-l2 字段访问补全](./u6-l2-field-access.md)。
- 若想了解 `font`/`path` 这类参数如何借助 `IdeWorld` 的 `book()`/`files()` 生成候选，参见 [u6-l3 import、路径、包、字体、标签补全](./u6-l3-import-path-package-font-label.md)。
- 阅读建议：把 [src/complete.rs:428-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L428-L600) 这一整段（`complete_params` → `param_completions` → `named_param_value_completions` → `param_value_completions` → `path_completion`）连起来读，体会「定位→分派→去重→产出值」的完整管线。
