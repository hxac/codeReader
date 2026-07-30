# 字段访问与方法调用分派

## 1. 本讲目标

本讲承接 u4-l1（函数调用与参数求值），专门拆解 Typst 中「点号（`.`）」的两种用法——**字段读取**与**方法/字段调用**——在解释器里是如何分派的。

读完本讲，你应该能够：

- 说清 `a.b`（读取字段）走 `FieldAccess::eval → access_field`，并理解「元素 settable 字段在 context 下兜底读取」的机制。
- 说清 `a.b(args)`（调用）走 `FuncCall::eval → eval_field_callee`，并按「类型方法 → 元素方法 → 字段」四条优先级路径还原分派顺序。
- 解释为什么字典/命名参数**禁止字段调用**，以及 `disallowed_field_call_error` 如何给出「加括号包裹」或「移除参数」的修复提示。
- 理解变更方法（`push`/`pop`/`insert`/`remove`）为何在普通调用分派之前就被 `maybe_resolve_mutating`「提前拦截」。
- 看懂「读取成功、调用却被拒」这一微妙差异：为什么 `block.stroke`（读）合法，而 `block.stroke(...)`（调用）非法。

## 2. 前置知识

在进入源码前，先用日常语言理清三个概念。

**字段（field）与方法（method）。** 在 Typst 里，很多值身上挂着「子项」：符号 `arrow.l` 的变体、类型 `str.to-unicode` 的关联函数、模块 `pdf.attach` 的导出、字典 `(a: 1).a` 的键、内容元素 `heading.level` 的属性。这些子项统称为**字段**。而**方法**是绑定在某个类型（或元素）上的函数，调用时把目标值作为第一个参数自动传入，例如 `(1, 2, 3).len()`。

**「点」的两种读法。** 同样一个点号，取决于后面**有没有跟括号**，解释器走两条完全不同的路径：

| 写法 | 语义 | 入口 |
|------|------|------|
| `a.b` | 读取字段，得到一个值 | `FieldAccess::eval` |
| `a.b(...)` | 调用一个方法或字段函数 | `FuncCall::eval`（callee 是 FieldAccess） |

**类型 scope 与元素 scope。** Typst 的每个「类型」（如 `array`、`str`、`dict`）都挂着一个关联作用域（scope），里面装着该类型的方法（`array.push`、`str.len`）；每个「元素」（如 `heading`、`block`）也挂着一个 scope，装着元素方法（如 `heading.body` 访问、`math.equation.block`）。方法的查找就是在这两个 scope 里完成的。`target.ty()` 返回目标值的类型，`content.elem()` 返回内容元素的元素函数。

> 这里的「字段读取」「方法分派」都是 `Eval` trait 的不同实现，与 u1-l4 讲的 `Eval` trait + `Vm` 框架一脉相承。本讲只关注「点号」相关的那几条分派路径。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们各自承担不同角色：

| 文件 | 本讲关注的角色 |
|------|----------------|
| `src/code.rs` | 定义 `FieldAccess::eval`（字段读取入口）与 `pub(crate) fn access_field`（读取实现 + settable 兜底） |
| `src/call.rs` | 定义 `FuncCall::eval` 的 FieldAccess 分支、`eval_field_callee`（方法/字段调用分派）、`FieldCallee` 枚举、`disallowed_field_call_error`（禁止字段调用诊断）、`maybe_resolve_mutating`（变更方法拦截） |

此外会顺带引用 `src/methods.rs`（`is_mutating_method` 等分类器）与 `src/access.rs`（`Access` trait，u5-l3 会深入）。这两个文件已在 u4-l1 出场过，本讲只借用其中的少量定义。

## 4. 核心概念与源码讲解

### 4.1 字段读取：FieldAccess::eval 与 access_field

#### 4.1.1 概念说明

`a.b` 是最朴素的字段读取：先求出 `a` 的值，再在这个值上取出名为 `b` 的字段。绝大多数情况一次就成功——字典键、符号变体、模块导出、内容元素属性都属于此类。

但有一种「看似失败、实则需兜底」的情形：**元素的 settable（可设置）字段在 context 下读取**。例如 `block.stroke`。`block` 是一个元素函数（`Value::Func`），它本身并不「存储」一个 `stroke` 值；`stroke` 是它的一个可设置参数，其当前值取决于排版期的样式链（style chain）。因此，在 `context {}` 块里写 `block.stroke` 时，必须**从 context 的样式里现算**这个字段，而不是当成普通字段去查。

这就是 `access_field` 里那段「兜底」要解决的问题。

#### 4.1.2 核心流程

字段读取的执行过程：

```
FieldAccess::eval:
  1. target = self.target().eval(vm)   // 先求目标值
  2. field  = self.field()             // 取字段名（编译期标识符）
  3. access_field(vm, target, field.as_str(), field.span())

access_field:
  1. 先按普通字段查：target.field(field, engine)
       Ok(v)  → 直接返回 v             // 99% 的情况到此结束
       Err(e) → 记下错误 e，进入兜底
  2. 兜底（三者同时满足才触发）：
       target 是 Value::Func
       且该 Func 是元素（to_element() == Some）
       且 field 是该元素的 settable 字段
     → styles = vm.context.styles()    // 必须在 context 内，否则这里报错
       返回 field_accessor(styles)     // 从样式链现算字段值
  3. 兜底未命中 → 返回原始错误 e
```

注意第 2 步是个**降级的兜底**：只有当普通字段读取失败、且目标恰好是「带可设置字段的元素函数」时才尝试。这正是 `block.stroke`、`heading.numbering` 这类写法得以工作的原因。

#### 4.1.3 源码精读

先看入口 [`FieldAccess::eval`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L352-L360)：它极简——求目标值、取字段名、把活儿全交给 `access_field`。

真正的逻辑在 [`access_field`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L363-L385)：

```rust
let err = match target.field(field, (&mut vm.engine, field_span)).at(field_span) {
    Ok(value) => return Ok(value),   // 普通字段读取成功，直接返回
    Err(err) => err,                  // 失败，留待兜底
};

// Missing fields may actually be present if they are settable parameters
// on elements accessed with context, e.g. `block.stroke`.
if let Value::Func(func) = &target
    && let Some(element) = func.to_element()
    && let Some(field_accessor) = element.settable_field_accessor(field)
{
    let styles = vm.context.styles().at(field_span)?;
    return Ok(field_accessor(styles));
}

Err(err)
```

要点：

- `target.field(field, sink)` 来自 `typst-library` 的 `Value::field`，它按值类型分派——字典查键、符号取变体、内容取属性、模块查导出等。这是「普通字段读取」的唯一入口。
- 兜底用 `let chains`（`if let ... && let ...`）三重条件，只有「函数 + 元素 + settable 字段」三者齐备才进入；`element.settable_field_accessor(field)` 返回一个 `fn(StyleChain) -> Value`，即「给样式链，算出该字段当前值」的访问器。
- `vm.context.styles()` 要求当前处于 context 求值中；若不在 context 里，这一步本身就会报错，提示需要 `context`。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「普通字段读取」与「settable 字段 context 兜底」两条路径的差异。

**操作步骤（源码阅读型 + 可选运行）：**

1. 在 `access_field` 中找到第 1 步的 `target.field(...)`，确认它成功时直接 `return Ok(value)`，根本不会走兜底——这是绝大多数字段读取的实际路径。
2. 准备一段 Typst 代码（可放入 `.typ` 文件用 typst CLI 编译观察）：

   ```typst
   // (a) 普通字段读取：字典键、模块导出 —— 命中第 1 步
   #(a: 1, b: 2).a
   #math.pi

   // (b) settable 字段兜底：需要 context
   #context [
     当前块描边是 #block.stroke。
   ]
   ```

3. 想象把第 (b) 行的 `context` 去掉，直接写 `#block.stroke`。

**需要观察的现象：**

- (a) 两行都能正常求值，说明它们走的是 `target.field(...)` 的成功分支，不触发兜底。
- (b) 在 `context []` 内能读到 `block.stroke` 的当前样式值——这正是兜底分支 `field_accessor(styles)` 的产物。

**预期结果：**

- 去掉 `context` 后，`block.stroke` 会报「需要 context」之类的错误，因为 `vm.context.styles()` 在非 context 求值时会失败。
- 待本地验证：具体报错文案请以本地 typst CLI 输出为准。

> 为什么不在第 1 步就处理 settable 字段？因为「普通字段」和「从样式链现算字段」是两套截然不同的机制。先试普通读取（便宜、无副作用），失败后再为元素 settable 字段付出「读 context 样式」的代价，是一种典型的**惰性兜底**。

#### 4.1.5 小练习与答案

**练习 1：** `access_field` 的兜底为什么用「先 `target.field()` 失败再尝试」的顺序，而不是先判断是否 settable？

**答案：** 普通字段读取覆盖了字典、符号、模块、内容属性等绝大多数情形，且无副作用、成本低；settable 字段的兜底需要读 `vm.context.styles()`（依赖 context、有代价）。先试便宜的、再兜底昂贵的，既保证常见路径快，又只在真正需要时付成本。

**练习 2：** 如果 `target` 是 `Value::Content`（一个内容元素实例，比如一个具体的 heading），`block.stroke` 这种兜底还会触发吗？

**答案：** 不会。兜底要求 `target` 是 `Value::Func` 且 `to_element()` 命中。对一个**内容实例**，`target.field(field)` 会走 `Self::Content(content) => content.field_by_name(field)`（见 `typst-library` 的 `Value::field`），属于普通读取路径，不进入这段兜底。本段兜底专门服务于「在元素**函数**上读取其 settable 参数」这一语境。

---

### 4.2 方法与字段调用分派：eval_field_callee

#### 4.2.1 概念说明

当点号后面**带括号**——`a.b(args)`——就进入调用分派。`FuncCall::eval` 会先判断 callee 是不是 `FieldAccess`，若是，则交给 [`eval_field_callee`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L240-L314) 把「目标 + 字段名」解析成一个可调用的东西。

解析结果用 [`FieldCallee`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L216-L225) 枚举表达，有三个变体：

| 变体 | 含义 | 调用时如何处理 |
|------|------|----------------|
| `Method(Func, Value)` | 命中了一个**方法** | 把 `Value`（目标）作为**第一个参数**插入 args，再调用 |
| `Func(Func)` | 命中了一个**字段函数**（非方法） | 直接调用，不插入目标 |
| `NonFunc(Value, err)` | 字段存在但不是函数 | 代码模式报错；数学模式不报错（当内容显示） |

「方法」与「字段函数」的关键区别：方法属于类型/元素的 scope，调用时自动把目标当首参；字段函数只是「挂在值上的一个函数值」（如 `assert.eq`、`arrow.l`），调用时不带这种自动注入。

#### 4.2.2 核心流程

`eval_field_callee` 用一条 `if/else if` 链按**固定优先级**查找 callee，这正是本讲的核心。四条优先级路径外加一条错误分支：

```
is_method_call = false
callee_value =
  ① 类型方法：target.ty().scope().get(field)
       命中 → is_method_call = true                      [最高优先级]
  ② 元素方法：若 target 是 Content，content.elem().scope().get(field)
       命中 → is_method_call = true
  ③ 符号/类型/模块字段：若 target 是 Symbol|Type|Module
       target.field(field)  → 字段调用（允许）
  ④ 函数字段：若 target 是 Func
       target.field(field)
         Ok → 用
         Err 且该 Func 是元素 + field 是 settable
              → 算出值，但 bail! disallowed_field_call_error（禁止调用）
         Err 其他 → 返回错误
  ⑤ 其他（Dict/Args/Length/...）：
       target.field(field)
         Ok   → bail! disallowed_field_call_error（字段存在但不许调用）
         Err  → bail! "{kind} {name} has no method `{field}`"

随后：vm.trace_at(access.span(), &callee_value)
cast callee_value 为 Func：
  Ok + is_method_call → FieldCallee::Method(func, target)
  Ok                  → FieldCallee::Func(func)
  Err                 → FieldCallee::NonFunc(callee_value, err)
```

四条「成功路径」对应注释里点名的四类合法字段调用：函数 `assert.eq`、类型 `str.to-unicode` / `table.cell`、模块 `pdf.attach`、符号 `arrow.l`。它们共同特点是「**非方法**的字段调用仅对这四类值放行」。

#### 4.2.3 源码精读

调用入口在 [`FuncCall::eval`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L34-L70) 的 FieldAccess 分支。它先 `check_call_depth`，再视方法是否「变更方法」决定走捷径（见 4.4），最后把 target 交给 `eval_field_callee`，并依据返回的 `FieldCallee` 变体决定如何拼装 args：

```rust
FieldCallee::Func(func) => {
    let args = ...; call_func(vm, func, args, span)
}
FieldCallee::Method(func, target) => {
    let mut args = ...;
    args.insert(0, target_expr.span(), target); // 方法：target 作首参
    call_func(vm, func, args, span)
}
FieldCallee::NonFunc(_, err) => Err(err).at(callee.span()),
```

注意 `Method` 分支那行 `args.insert(0, target_expr.span(), target)`——这是「方法复用函数机制」的关键：方法不过是一个普通函数，只是调用时编译器替你把目标塞进了第一个参数位。`(1,2,3).push(4)` 在底层等价于 `array.push((1,2,3), 4)`。

分派主体 [`eval_field_callee`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L240-L314) 的前两条路径（①②）：

```rust
let mut is_method_call = false;
let callee_value = if let Some(method) = target.ty().scope().get(field) {
    is_method_call = true;
    method.read_checked(sink).clone()
} else if let Value::Content(content) = &target
    && let Some(method) = content.elem().scope().get(field)
{
    is_method_call = true;
    method.read_checked(sink).clone()
} else if matches!(target, Value::Symbol(_) | Value::Type(_) | Value::Module(_)) {
    target.field(field, sink).at(field_span)?        // 路径③
} else if let Value::Func(func) = &target {
    // 路径④：函数字段调用，但 settable 字段禁止调用 ...
}
```

`read_checked` 会在读取绑定的同时处理「弃用（deprecated）」警告，把警告汇入 `sink`。方法查找**优先于**字段查找——这点至关重要，它是「方法优先」规则的直接体现。

末尾的类型转换把查到的值归类为三种 `FieldCallee`：

```rust
vm.trace_at(access.span(), &callee_value);          // 满足 trace 约定
match callee_value.clone().cast::<Func>() {
    Ok(func) if is_method_call => Ok(FieldCallee::Method(func, target)),
    Ok(func) => Ok(FieldCallee::Func(func)),
    Err(err) => Ok(FieldCallee::NonFunc(callee_value, err)),
}
```

> `eval_field_callee` 同时服务代码模式与数学模式——它带一个 `in_math: bool` 参数。代码模式（`FuncCall::eval`）传 `false`；数学模式（`eval_math_call`）传 `true`，差别仅在错误提示文案（见 4.3）。

#### 4.2.4 代码实践

**实践目标：** 按优先级梳理方法查找的四条路径，并解释 `(at: x => ...).at(key)` 为何被禁止字段调用。这是本讲的核心练习。

**操作步骤（源码阅读型）：**

1. 打开 `eval_field_callee`，依次定位①②③④四条路径，为每条写下一句「命中条件 + 产出哪种 `FieldCallee`」。
2. 回答下面的推断题。

**四条路径梳理（参考答案框架）：**

| 路径 | 命中条件 | is_method_call | 产出 |
|------|----------|----------------|------|
| ① 类型方法 | `target.ty().scope()` 含 `field` | true | `Method` |
| ② 元素方法 | target 是 Content 且 `content.elem().scope()` 含 `field` | true | `Method` |
| ③ 符号/类型/模块字段 | target 是 Symbol/Type/Module | false | cast 后 `Func`/`NonFunc` |
| ④ 函数字段 | target 是 Func | false | cast 后 `Func`/`NonFunc`（settable 则报错） |

**推断题：为什么 `(at: x => ...).at(key)` 必须被禁止字段调用？**

`(at: x => ...)` 构造了一个字典，其键 `"at"` 恰好与字典内置的访问器方法 `at` **同名**。如果允许对字典使用字段调用语法，`eval_field_callee` 就要面对一个无法调和的二义性：

- 若**字段优先**（优先调用字典键里存的函数）：`(at: x => ...).at(key)` 会去调用键 `"at"` 里那个 `x => ...`，把 `key` 当它的参数——于是你再也用不了内置的 `.at()` 访问器，但凡字典里有个叫 `at` 的键就坏了。
- 若**方法优先**（优先调用内置方法）：那么 Typst 每新增一个内置方法名，都可能让某个含有同名键的旧字典行为改变——任何方法新增都成了破坏性变更。

源码顶部的注释说得直白：这两种选择**都很糟糕**，所以语言干脆**禁止字典的字段调用**，把这个二义性从根上消除。要让 `(at: x => ...).at(key)` 取到键，就用 `.at("at")`（访问器方法）或 `dict.at`（字段**读取**，合法）；要调用键里存的函数，就加括号包裹：`((at: x => ...).at)(args)`。

**需要观察的现象：**

- `.at(key)` 仍可正常工作——因为 `at` 是字典的类型方法（路径①），属合法**方法**调用，不在「字段调用」禁令之内。
- 字段调用禁令只针对「把字段当函数调用」，不影响「方法调用」与「字段读取」。

**预期结果：** 字典只能走路径①（方法）或字段读取；路径⑤对它直接 `bail! disallowed_field_call_error`。

#### 4.2.5 小练习与答案

**练习 1：** `str.to-unicode("A")` 走的是哪条路径？`target` 是什么类型的值？

**答案：** `str` 是一个**类型值**（`Value::Type`），命中路径③（`matches!(target, … | Value::Type(_) | …)`），`is_method_call` 为 `false`，产出 `FieldCallee::Func`。调用时不把 `str` 当首参插入——这正是「字段函数」与「方法」的区别。

**练习 2：** 为什么「方法」要用独立的 `Method` 变体，而不统一成 `Func`？

**答案：** 因为方法调用需要把**目标值**自动作为第一个参数注入（`args.insert(0, …, target)`）。`Method(Func, Value)` 里多带的那一份 `Value` 就是「待插入的目标」。普通字段函数没有这种约定，所以用 `Func` 变体区分。

---

### 4.3 禁止字段调用的诊断：disallowed_field_call_error

#### 4.3.1 概念说明

`eval_field_callee` 的路径⑤（以及路径④的 settable 子分支）会把「字段存在、但不允许当函数调用」的情形交给 [`disallowed_field_call_error`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L332-L391) 生成诊断。

这个函数是 typst-eval「高质量诊断」的典型样本：它不只给出错误信息，还针对不同目标类型、不同「字段值是否真的是函数」给出**差异化的修复提示**。

#### 4.3.2 核心流程

```
判断目标类别：
  is_dict  = target 是 Dict?
  is_named = target 是 Args（命名参数聚合）?

错误主体：
  Dict  → "cannot directly call dictionary keys as functions"
  Args  → "cannot directly call named argument fields as functions"
  其他  → "`{field}` is not a valid method for {kind} `{name}`"
        （kind/name 由 element_or_type_with_name 给出：内容→element，否则→type）

附加 hint（择一）：
  若 callee_value 能 cast 成 Func（字段里确实存了个函数）:
     → 提示「加括号包裹」：`{access.full_text()}(..)`（数学模式还加「先切代码模式 #」）
  否则若 in_math:
     → "try adding a space before the parentheses"
  否则:
     → 提示「移除参数」访问字段：`{access.full_text()}`

补充 hint（仅 Dict/Args）:
  Dict → "dictionary keys cannot be used with method syntax as keys could conflict with built-in method names"
  Args → "named arguments cannot be used with method syntax …"
```

#### 4.3.3 源码精读

错误主体按目标类型分三档：

```rust
let mut err = if is_dict {
    error!(access.span(), "cannot directly call dictionary keys as functions")
} else if is_named {
    error!(access.span(), "cannot directly call named argument fields as functions")
} else {
    let (kind, name) = element_or_type_with_name(&target);
    error!(access.span(), "`{field}` is not a valid method for {kind} `{name}`")
};
```

修复提示则按「字段值是不是函数」二分——这是真正贴心之处：

```rust
if callee_value.clone().cast::<Func>().is_ok() {
    err.hint(eco_format!(
        "to call the stored function, {}wrap the field access \
            in parentheses: `{}({})(..)`",
        if in_math { "use code mode and " } else { "" },
        if in_math { "#" } else { "" },
        access.full_text(),
    ));
} else if in_math {
    err.hint("try adding a space before the parentheses");
} else {
    err.hint(/* 提示移除参数，把字段当值访问 */);
}
```

辅助函数 [`element_or_type_with_name`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L395-L401) 决定错误信息里称呼目标为「element」还是「type」：内容元素 → `("element", 元素名)`，其余 → `("type", 类型长名)`。

> 「读取成功、调用被拒」的对比：回顾 4.1，`access_field`（读取）对字典是**放行**的——`Value::field` 里 `Self::Dict(dict) => dict.get(field).cloned()` 直接返回键值。只有**调用**（带括号）才会进 `disallowed_field_call_error`。这就解释了为什么 `dict.at` 能读、`dict.at(...)`（当字段调用时）会被拦。

#### 4.3.4 代码实践

**实践目标：** 体会「字段值是否为函数」如何改变修复提示，并对比「读」与「调用」的不同待遇。

**操作步骤（源码阅读型）：**

1. 在 `disallowed_field_call_error` 中找到 `callee_value.clone().cast::<Func>().is_ok()` 这条分支，确认它生成的 hint 是「加括号包裹」。
2. 阅读下列两段 Typst 片段，预测各自的诊断：

   ```typst
   // (a) 字段里存的是函数
   #let d = (at: (x) => x + 1)
   // #d.at(5)            // ← 字段调用，预测报错 + 哪条 hint？

   // (b) 字段里存的是普通值
   #let e = (abs: 3pt)
   // #e.abs()            // ← 字段调用，预测报错 + 哪条 hint？
   ```

**需要观察的现象：**

- (a) 因为 `d.at` 取出的是个函数，错误会附带「用括号包裹调用」的 hint：`((d.at))(..)`。
- (b) `e.abs` 取出的是长度值（非函数），错误转而提示「移除参数，直接当字段读」。

**预期结果：** 两种情形错误主体一致（都因 Dict 而说「cannot directly call dictionary keys as functions」），但 hint 因字段值类型不同而分流——这正是 `disallowed_field_call_error` 设计的精妙处。待本地验证具体文案。

#### 4.3.5 小练习与答案

**练习 1：** `disallowed_field_call_error` 的文档注释里列了一串「会产生此错误的类型/字段」（`Alignment.x`、`Length.abs`、`Stroke.cap` 等）。它们为什么会落到这里，而不是「no method」错误？

**答案：** 因为这些字段**确实存在**（`target.field(field)` 返回 `Ok`），只是它们的宿主类型（Alignment/Length/Stroke…）不在「允许字段调用」的白名单（Symbol/Type/Module/Func）里。于是走路径⑤的 `Ok` 分支 → `disallowed_field_call_error`，而不是 `Err` 分支的「has no method」。区分「字段存在但不许调用」与「字段根本不存在」，让错误信息更准确地反映用户意图。

**练习 2：** 为什么对 Dict 的错误要额外加一条「keys could conflict with built-in method names」的 hint？

**答案：** 这条 hint 直接点明了禁令的**根因**（见 4.2.4 推断题）——字典键是任意字符串，可能与内置方法名撞车。告诉用户「为什么」比只说「不行」更能引导其改用 `.at("key")` 或字段读取，提升诊断的教育价值。

---

### 4.4 变更方法的提前拦截：maybe_resolve_mutating

#### 4.4.1 概念说明

有一类方法会**就地修改**目标：数组的 `push`/`pop`/`insert`/`remove`、字典的 `insert`/`remove`。它们不能用「求出 callee 函数 → 调用」的常规流程，因为常规流程拿到的是目标值的**拷贝**，改拷贝改不到原变量。

因此 `FuncCall::eval` 在进入 `eval_field_callee` 之前，对变更方法做了一次**提前拦截**：用 `Access` trait（见 u5-l3）拿到目标值的**可变引用**，直接在其上调用 `call_method_mut` 完成修改。这件事由 [`maybe_resolve_mutating`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L190-L213) 完成。

分类器定义在 [`methods.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L9-L16)：

```rust
pub(crate) fn is_mutating_method(method: &str) -> bool {
    matches!(method, "push" | "pop" | "insert" | "remove")
}
pub(crate) fn is_dict_mutating_method(method: &str) -> bool {
    matches!(method, "insert" | "remove")
}
```

注意两者的差别：`push`/`pop` 是变更方法，但**只对数组有意义**；字典没有这两个方法。所以 `is_dict_mutating_method` 只认 `insert`/`remove`。

#### 4.4.2 核心流程

```
FuncCall::eval（callee 是 FieldAccess 时）：
  if is_mutating_method(field):
    maybe_resolve_mutating(vm, target_expr, field, args, span):
      1. args = args.eval(vm)              // 必须先求参数！
      2. match target_expr.access(vm)?:    // 拿可变引用（这会可变借用 vm）
           Dict 且 field ∉ {insert, remove}  → 返回 Err((target, args)) 回退
           Array | Dict                      → call_method_mut → Ok(Ok(value))
           其他类型                          → 返回 Err((target, args)) 回退
    Ok(value) → FuncCall::eval 直接 return value（变更已完成）
    Err((target, args)) → 继续走 eval_field_callee → call_func（多半会报错）
  else:
    正常求 target，走 eval_field_callee
```

返回类型 `Result<Value, (Value, Args)>` 是个巧妙设计：外层 `Ok(Ok(value))` 表示「已就地修改并算出返回值」；`Ok(Err((target, args)))` 表示「拦截失败，把已求好的 target 和 args 退还给常规路径」，避免重复求值参数。

#### 4.4.3 源码精读

拦截入口在 [`FuncCall::eval`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L34-L44)：

```rust
let (target, maybe_args) = if is_mutating_method(field.as_str()) {
    match maybe_resolve_mutating(vm, target_expr, field, self.args(), span)? {
        Ok(value) => return Ok(value),          // 拦截成功，直接返回
        Err((target, args)) => (target, Some(args)),
    }
} else {
    (target_expr.eval(vm)?, None)
};
```

[`maybe_resolve_mutating`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L190-L213) 内部：

```rust
// We evaluate the arguments first because `target_expr.access(vm)` mutably
// borrows `vm`, so we won't be able to call `args.eval(vm)` afterwards.
let args = args.eval(vm)?.spanned(span);
match target.access(vm)? {
    // Skip methods that aren't actually mutating for dictionaries.
    target @ Value::Dict(_) if !is_dict_mutating_method(field.as_str()) => {
        Ok(Err((target.clone(), args)))
    }
    // Only arrays and dictionaries have mutable methods.
    target @ (Value::Array(_) | Value::Dict(_)) => {
        let value = call_method_mut(target, &field, args, span);
        ...
        Ok(Ok(value.trace(vm.world(), point, span)?))
    }
    target => Ok(Err((target.clone(), args))),
}
```

两个关键设计：

1. **先求 args，再 access。** 注释解释了原因：`target.access(vm)` 会**可变借用** `vm`，之后再调 `args.eval(vm)` 就借不到了。所以必须把 args 先求出来。这也是为什么拦截器要把 args 退还给常规路径——它已经求好了，常规路径不必重求。
2. **字典的 push/pop 被显式回退。** 第一个 match 臂用 `if !is_dict_mutating_method(field)` 把 `dict.push(...)` 这类（push 不是字典变更方法）退回常规路径，最终在常规路径里报「type dictionary has no method `push`」。如果不回退，就会错误地走到 `call_method_mut` 的字典分支并报 missing。

#### 4.4.4 代码实践

**实践目标：** 追踪 `array.insert(0, x)` 的完整求值路径，体会「提前拦截」如何让就地修改变得可能。

**操作步骤（源码阅读型）：**

1. 假设 Typst 代码：

   ```typst
   #let xs = (1, 2, 3)
   #xs.insert(0, 99)
   #xs
   ```

2. 按下面顺序在源码里走一遍：
   - `FuncCall::eval`：callee `xs.insert` 是 FieldAccess，`field = "insert"` 命中 `is_mutating_method`。
   - 进入 `maybe_resolve_mutating`：先 `args.eval(vm)` 求出位置参数 `[0, 99]`。
   - `target_expr.access(vm)`：`xs` 是 `ast::Ident`，走 `Access for ast::Ident`（access.rs），拿到 `xs` 绑定的**可变引用** `&mut Value::Array`。
   - 命中 `Value::Array(_) | Value::Dict(_)` 臂，调用 `call_method_mut`（methods.rs），其 `Array`/`insert` 分支执行 `array.insert(index, value)`。
   - 返回 `Ok(Ok(value))`，`FuncCall::eval` 直接 `return Ok(value)`，**根本不进入** `eval_field_callee`。

3. 对比：把代码改成 `#xs.insert(0, 99)` 中 `xs` 换成一个临时值 `(1,2,3).insert(0, 99)`（字面量数组直接调用）。

**需要观察的现象：**

- 第 2 步：`xs` 是具名变量，`Access` 能拿到可变引用，修改会写回 `xs`，最后 `#xs` 输出 `(99, 1, 2, 3)`。
- 第 3 步：字面量是临时值，`Access for ast::Expr` 的兜底分支会先 `eval` 再 `bail!("cannot mutate a temporary value")`（见 access.rs）。

**预期结果：** 变更方法只有对「可被 `Access` 的具名目标」（变量、字段访问、访问器方法链）才生效；对临时值会报「cannot mutate a temporary value」。待本地验证。

#### 4.4.5 小练习与答案

**练习 1：** `maybe_resolve_mutating` 为什么返回 `Result<Value, (Value, Args)>`，而不是简单地 `Option<Value>`？

**答案：** 因为拦截失败时（目标类型不支持变更方法，或字典遇到 push/pop），它已经**求好了 args**，应当把 `(target, args)` 退还给常规调用路径（`eval_field_callee → call_func`），避免重复求值参数、也保持「callee 先求、args 后求」的统一顺序。用 `Err((Value, Args))` 携带这些已求值，是零成本回退的惯用法。

**练习 2：** 为什么字典对 `push`/`pop` 要单独回退，而不是让 `call_method_mut` 的字典分支去报「no method」？

**答案：** `call_method_mut` 的字典分支只实现了 `insert`/`remove`，其余会走 `_ => return missing()`。但 `maybe_resolve_mutating` 在 `is_mutating_method` 为真时（push/pop 也算）才会被调用，若不显式回退，`dict.push(...)` 会进入字典臂并以 `call_method_mut` 的 missing 报错——错误信息不够精准。显式回退让它走常规路径，由 `eval_field_callee` 的类型方法查找给出更自然的诊断。

---

## 5. 综合实践

把本讲四条线索串起来，设计一个**对比阅读**任务：用同一份 Typst 代码，跟踪点号在不同写法下分别走哪条路径。

**任务：** 阅读下面这段代码，对**每一行**标注它命中的「入口函数 → 关键分派」路径，并预测结果或错误。

```typst
#let d = (insert: (k, v) => none, at: "hit", len: 9)

// 行 A：字段读取
#d.at

// 行 B：方法调用
#d.at("at")

// 行 C：变更方法
#let xs = (1, 2)
#xs.insert(0, 5)

// 行 D：字段调用（字典键恰好与方法名冲突）
// #d.insert("k", "v")

// 行 E：元素 settable 字段读取
#context { block.stroke }
```

**要求：**

1. **行 A**：说明它走 `FieldAccess::eval → access_field`，命中 `Value::field` 的字典分支（读取成功），返回字符串值 `"hit"`；不触发 settable 兜底。
2. **行 B**：说明它走 `FuncCall::eval`，因 `at` 不是变更方法，进入 `eval_field_callee` 路径①——字典类型的 scope 里有 `at` 方法，产出 `FieldCallee::Method`，把 `d` 作首参插入后调用，返回键 `"at"` 对应的值。
3. **行 C**：说明 `insert` 命中 `is_mutating_method`，走 `maybe_resolve_mutating`，对具名变量 `xs` 拿可变引用就地插入，不进入 `eval_field_callee`。
4. **行 D**：说明 `insert` 命中变更拦截，但 `d` 是 Dict 且 `insert` **是**字典变更方法，故 `call_method_mut` 字典分支执行 `dict.insert("k","v")`——这里会真正插入键值，**并非**走 `disallowed_field_call_error`。请思考：为什么行 D 不会触发「禁止字段调用」？因为变更拦截优先于字段调用禁令，已提前处理。

   > 进一步思考：如果把行 D 改成 `#d.len()`（`len` 不是变更方法，且字典类型 scope 有 `len` 方法），它会走路径①作为方法成功；但如果改成调用一个字典里存的、与内置方法**不**同名的函数字段，例如 `#((f: () => 1)).f()`，会怎样？（答：`f` 不是任何类型/元素方法，落路径⑤ → `disallowed_field_call_error`，提示加括号包裹。）

5. **行 E**：说明 `block` 是元素函数，`access_field` 第 1 步 `target.field("stroke")` 失败，兜底三条件（Func + 元素 + settable）命中，从 `vm.context.styles()` 现算 `stroke`。

**预期产出：** 一张表，列出每行的「入口 → 分派路径 → 结果/错误」。完成后，你应该能清晰说出「读」「方法调用」「字段调用」「变更方法」四类点号用法的判别顺序，以及字典为何禁止字段调用。

## 6. 本讲小结

- 点号分两种：`a.b`（读）走 `FieldAccess::eval → access_field`；`a.b(...)`（调用）走 `FuncCall::eval → eval_field_callee`。
- `access_field` 先做普通字段读取，失败后对「元素函数 + settable 字段」从 `vm.context.styles()` 兜底现算（如 `block.stroke`），是惰性兜底。
- `eval_field_callee` 按固定优先级分派：①类型方法 → ②元素方法 → ③符号/类型/模块字段 → ④函数字段 → ⑤否则报错；方法命中时把目标自动作为首参插入。
- 「字段调用」仅对 Symbol/Type/Module/Func 四类放行；Dict/Args 等即使字段存在也会被 `disallowed_field_call_error` 拒绝，因为字典键可能与内置方法名冲突（二义性无解）。
- 变更方法（push/pop/insert/remove）在常规分派**之前**被 `maybe_resolve_mutating` 提前拦截，用 `Access` 拿可变引用就地修改；拦截失败时把已求好的 `(target, args)` 退还常规路径。
- `disallowed_field_call_error` 是高质量诊断样本：按目标类型和「字段值是否为函数」给出差异化修复提示（加括号包裹 vs 移除参数）。

## 7. 下一步学习建议

- 下一讲 **u4-l3 闭包定义与 eval_closure 执行** 会继续待在 `call.rs`，讲解 `Closure::eval` 如何构造闭包、`eval_closure` 如何重建作用域并绑定参数，与本讲的 `call_func` 闭环。
- 若对「可变引用」机制感兴趣，可先跳读 **u5-l3 可变访问 Access 与内置方法**，深入 `Access` trait 与 `call_method_mut`/`call_method_access`，把本讲 4.4 的「提前拦截」与 `access_dict` 的边界讲透。
- 想理解 settable 字段与样式链的更多细节，可阅读 `typst-library` 中 `Element::settable_field_accessor` 与 `StyleChain` 的实现，体会 `access_field` 兜底背后的排版期机制。
