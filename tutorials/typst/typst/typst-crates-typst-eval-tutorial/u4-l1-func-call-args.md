# 函数调用与参数求值

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `FuncCall::eval` 这个「函数调用总分发器」的整体流程：先做调用深度检查，再分「普通 callee」与「字段访问 callee」两条路径。
- 理解 Typst 求值函数调用时**固定的求值顺序**——先求值被调用者（callee），再求值参数（args），并能解释这个顺序为何重要。
- 掌握 `call_func` 如何用 `stacker::maybe_grow` 动态增长调用栈，以及为什么在 wasm 平台上要禁用。
- 理解 `route.check_call_depth()` 这道「调用深度防线」如何用 `Route::within(80)` 拦截无限递归，并能把它和「循环求值防护」（u1-l3、u6-l3）区分开。
- 看懂 `Args::eval` 如何把 `Pos`（位置参数）、`Named`（命名参数）、`Spread`（展开参数 `..x`）三类语法项转成运行时的 `Arg` 列表，并对 spread 的不同值类型做「展平」。

本讲全部源码集中在 [`src/call.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs)，辅以少量 `typst-library` 的 `engine.rs`、`func.rs`、`args.rs`。

## 2. 前置知识

本讲建立在 u1-l4（`Eval` trait 与 `Vm`）和 u2-l1（字面量与标识符求值）之上。开始前请确认你熟悉以下概念：

- **`Eval` trait 的签名**：`fn eval(self, vm: &mut Vm) -> SourceResult<Output>`。它「消费自身、借用可变 Vm、可能失败」。函数调用 `f(a, b)` 在语法树上就是一个 `ast::FuncCall` 节点，它的 `Output` 是 `Value`。
- **`Vm` 虚拟机**：持有 `engine`（环境句柄，内含 `route`、`world`、`sink` 等）、`scopes`（作用域栈）、`flow`（控制流事件）、`context`（幕后上下文）。本讲里我们会反复用到 `vm.engine.route` 和 `vm.context`。
- **`Value` 与 `Func`**：求值的最終产物是 `Value`；当 callee 不能 cast 成 `Func` 时会报错（见 4.1）。
- **`FlowEvent`**（u3-l2）：`return` 会产生 `FlowEvent::Return` 并被 `eval_closure` 消费。本讲在 4.3 会顺带提到调用与 `return` 的衔接，但消费细节归 u3-l2。
- **`Span` 与诊断**：每个值/错误都带 `Span`（源码位置）。`Span::detached()` 表示「没有具体位置」，本讲 4.5 会解释 `Args` 为何用它。

如果你对上面任何一项感到陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
|------|----------------|
| [`src/call.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs) | **核心文件**。包含 `FuncCall::eval`、`call_func`、`eval_field_callee`、`Args::eval` 等本讲全部主角。 |
| [`crates/typst-library/src/engine.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs) | 定义 `Route` 及其 `check_call_depth`、`within`、`MAX_CALL_DEPTH`（4.4 用）。 |
| [`crates/typst-library/src/foundations/func.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs) | `Func::call_impl` 把调用分派给 native/closure/element/plugin（4.3 用）。 |
| [`crates/typst-library/src/foundations/args.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs) | `Args`、`Arg` 两个结构体的定义（4.5 用）。 |

> 说明：`call.rs` 还包含 `Closure::eval`、`eval_closure`、`CapturesVisitor` 等大量内容，它们属于 u4-l3、u4-l4 的范围，本讲只在「调用如何衔接到闭包执行」时简要带过。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块组织：函数调用总分发器、字段访问型 callee、`call_func` 与栈安全、`Args` 求值。

### 4.1 FuncCall::eval：函数调用总分发器

#### 4.1.1 概念说明

在 Typst 语法里，`f(a, b: 1)`、`array.push(x)`、`calc.min(1, 2)` 都是「函数调用」。它们在 AST 里统一表示为 `ast::FuncCall<'_>` 节点：它有一个 **callee**（被调用者，`f` / `array.push` / `calc.min`）和一组 **args**（参数列表）。

`FuncCall::eval` 就是处理这一类节点的入口。它的职责可以概括为三件事：

1. **先做调用深度检查**——防止无限递归把调用栈打穿（见 4.4）。
2. **区分两类 callee**：普通 callee（`f(...)`）和字段访问 callee（`obj.method(...)`）。二者的求值路径不同，后者还涉及「方法调用」还是「字段调用」的分派（见 4.2）。
3. **按固定顺序求值 callee 与 args，最后交给 `call_func` 执行**。

为什么要把「字段访问 callee」单独分出来？因为 `obj.method(x)` 这种写法既可能是「调用对象所属类型的内置方法」（如 `array.push`），也可能是「先取字段再调用」（如 `math.pi` 不行，但 `str.len` 这种类型上的关联函数可以）。这两类语义差别很大，需要 `eval_field_callee` 来仔细分派。普通 callee（`f(...)`）则简单得多：求值出 `Func` 就直接调用。

#### 4.1.2 核心流程

`FuncCall::eval` 的整体流程（伪代码）：

```
fn eval(FuncCall):
    span = self.span()
    callee = self.callee()         // AST 上的被调用者表达式

    # 第一道关卡：调用深度检查
    vm.engine.route.check_call_depth().at(span)?    # 超深度就返回 Err

    if callee 是 FieldAccess(access):
        # —— 字段访问型 callee（见 4.2）——
        求值 target，用 eval_field_callee 决定 Method/Func/NonFunc
        求值 args，必要时把 target 插到 args 最前面
        call_func(...)
    else:
        # —— 普通 callee ——
        func = callee.eval(vm)?.cast::<Func>()?     # 先求值 callee
        args = self.args().eval(vm)?                # 再求值 args
        call_func(vm, func, args, span)
```

两个关键设计点：

- **求值顺序是「callee 在前，args 在后」**。这一点在源码的普通分支里有明确注释，下文 4.1.3 会引用。
- **`check_call_depth` 在任何求值之前执行**。这样无论 callee 多复杂，只要深度超限就立刻报错，避免做无用的求值工作。

#### 4.1.3 源码精读

先看整个 `FuncCall::eval` 的开头与普通分支：

[call.rs:24-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L24-L82) 是 `impl Eval for ast::FuncCall` 的完整实现。其中第 31 行是入口的调用深度检查：

```rust
vm.engine.route.check_call_depth().at(span)?;
```

`check_call_depth()` 返回 `StrResult<()>`（无 span 的字符串结果），`.at(span)` 把它转成带源码位置的 `SourceResult`。深度超限时它会返回 `"maximum function call depth exceeded"` 的错误，从而 `?` 提前返回。注意这与「循环求值」不同：循环求值走的是 `route.contains(id)`，命中直接 `panic!`（编译器内部错误）；而深度超限走的是 `check_call_depth`，返回的是**用户可见的 Err**。详见 4.4。

普通 callee 分支（`else` 块）是最典型的「求值顺序」展示，见 [call.rs:71-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L71-L80)：

```rust
} else {
    // Function call order: we evaluate the callee before the arguments.
    let func = callee
        .eval(vm)?                              // ① 先求值 callee
        .cast::<Func>()                          // ② 把 Value 强制转型成 Func
        .map_err(|err| hint_if_shadowed_std(vm, &callee, err))  // ③ 失败时附加「是否被标准库遮蔽」提示
        .at(callee.span())?;                     // ④ 贴上 callee 的 span
    let args = self.args().eval(vm)?.spanned(span);  // ⑤ 再求值 args
    call_func(vm, func, args, span)              // ⑥ 交给 call_func 执行
}
```

逐点解读：

1. **① `callee.eval(vm)?`**：callee 本身是一个 `ast::Expr`，递归求值得到一个 `Value`。例如 `f(a)` 中 `f` 是 `Ident`，求值后是 `Value::Func`。
2. **② `.cast::<Func>()`**：不是所有 `Value` 都是函数。如果 callee 求值出 `Value::Int(3)`，这里 cast 失败，报错「expected function, found integer」之类。
3. **③ `hint_if_shadowed_std`**（u1-l4 已介绍）：当 cast 失败时，检查这个标识符是否「遮蔽了某个标准库同名项」，若是就给出 hint「did you mean to use the standard library function?」，帮助用户定位 `#let calc = ...` 这类把标准库函数名覆盖掉的问题。
4. **⑤ `self.args().eval(vm)?`**：求值参数列表得到 `Args`，`.spanned(span)` 把整条调用语句的 span 附加到 `Args` 上（4.5 会解释为什么 `Args::eval` 自己不贴 span）。
5. **求值顺序**：①②③ 在 ⑤ 之前，即 **callee 必须先于 args 求值完毕**。这个顺序保证了：如果 callee 求值或 cast 就出错，根本不会去求值 args，从而避免「args 有副作用（比如调用了别的函数）却因为 callee 错误被丢弃」的困惑。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「callee 先于 args 求值」这一顺序，并理解它对报错时机的影响。

**操作步骤**：

1. 在本仓库根目录新建一个测试用 Typst 文件 `playground.typ`（示例代码，非项目原有文件），内容如下：

   ```typst
   #let log-it(x) = {
     panic("args 被求值了，x = " + str(x))
   }
   // callee 故意写成非函数的整数 3
   #(3)(log-it(1))
   ```

2. 用 typst CLI 编译（假设你已安装 typst）：`typst compile playground.typ`。

**需要观察的现象**：

- 报错信息应当是关于 `3` 「expected function, found integer」，而**不应**出现 `panic("args 被求值了…")`。
- 这说明：因为 callee `3` cast 成 `Func` 失败（步骤 ②③），`?` 提前返回，根本没有走到步骤 ⑤ 去求值 args。

**预期结果**：编译失败，错误定位在 callee `3` 上，提示类型不匹配；args 中的 `panic` 未触发。若你看到的是 panic 被触发，则与源码顺序矛盾，请重新核对。

> 如果无法本地安装 typst，本实践属于「源码阅读型」：对照 [call.rs:71-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L71-L80) 推理即可，结论可标「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`check_call_depth()` 返回的是 `StrResult`，为什么后面要接 `.at(span)`？

**参考答案**：`StrResult` 是只有错误字符串、没有源码位置的结果类型；而 `FuncCall::eval` 必须返回 `SourceResult`（带 `Span` 的诊断）。`.at(span)` 的作用就是把「纯字符串错误」升级为「定位到整条调用语句 `span` 的源诊断」，这样 IDE 和终端报错才能高亮到具体位置。

**练习 2**：如果 callee 求值成功但 cast 成 `Func` 失败，`hint_if_shadowed_std` 会做什么？

**参考答案**：它会检查这个 callee 标识符是否在当前作用域里遮蔽了标准库的同名项（例如用户写了 `#let calc = 5` 把 `calc` 覆盖了）。若确实遮蔽，就在原错误基础上附加一条 hint，提示用户可能是误覆盖了标准库函数，引导其修正。

---

### 4.2 字段访问型 callee：方法调用与字段调用分派

#### 4.2.1 概念说明

`obj.field(args)` 形式的调用，callee 是一个 `FieldAccess`（字段访问表达式）。这类调用在 typst-eval 里语义比普通调用复杂，因为 `obj.field` 到底指什么，取决于 `obj` 的类型和 `field` 的名字。例如：

- `array.push(1)` —— `push` 是数组**类型上的方法**（method）。
- `str.len()` 等价于 `str.len` 的调用 —— `len` 是 `str` 这个**类型上的关联函数**（field call on a Type）。
- `calc.min(..)` —— `min` 是 `calc` **模块里的函数**（field call on a Module）。
- `(foo: () => [])` 然后 `(foo: () => []).foo()` —— 字典里恰好有个键叫 `foo`，这种情况**被禁止**（见 4.2.3）。

`eval_field_callee` 用一个枚举 `FieldCallee` 把这三种结果统一表达出来：调用方拿到结果后再决定怎么组装 `args`。

#### 4.2.2 核心流程

`FuncCall::eval` 中 `FieldAccess` 分支的核心流程（见 [call.rs:34-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L34-L70)）：

```
if callee 是 FieldAccess(access):
    target_expr = access.target()      // 例如 array.push 里的 array
    field = access.field()             // 例如 push

    if is_mutating_method(field):      // push/pop/insert/remove 等变更方法
        尝试 maybe_resolve_mutating(...):
            若 target 是 Array/Dict 且方法确实可变更 -> 直接返回变更结果 Ok(value)
            否则 -> 返回已求值的 (target, args)，交给下面继续处理
    else:
        target = target_expr.eval(vm)

    match eval_field_callee(target, field, ...):
        FieldCallee::Func(func):       args = 求 self.args(); call_func(func, args)
        FieldCallee::Method(func, target):
            args = 求 self.args()
            args.insert(0, target)     # 关键：把 target 作为第一个参数
            call_func(func, args)
        FieldCallee::NonFunc(_, err):  报错 err
```

**最关键的一点**：方法调用（`FieldCallee::Method`）会把 `target` **作为第一个位置参数**插入 `args`。这是因为 Typst 的内置方法在标准库里其实都是普通函数，第一个参数就是「self」。例如 `array.push(x)` 在标准库里对应 `array-push(array, x)` 这样的原生函数，typst-eval 只是在调用前把 `array` 塞到参数列表最前面。这样设计让「方法」和「函数」在运行时共用同一套调用机制。

**变更方法的特殊快路径**：`push`/`insert` 这类会修改对象的方法走 `maybe_resolve_mutating`，它需要先拿到 target 的**可变引用**（`target.access(vm)`），所以必须先求值 args（因为 `access` 会可变借用 `vm`，之后就没法再调 `args.eval(vm)` 了）。这部分的可变访问机制详见 u5-l3，本讲只需知道「变更方法有一条独立的快路径」即可。

#### 4.2.3 源码精读

`FieldCallee` 枚举定义见 [call.rs:216-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L216-L225)：

```rust
enum FieldCallee {
    /// 类型或内容上的方法，target 作为第一个参数。
    Method(Func, Value),
    /// 一个普通函数。
    Func(Func),
    /// 字段访问结果不是函数。在 code 模式会报错，在 math 模式不会。
    NonFunc(Value, HintedString),
}
```

方法调用把 target 插入 args 前部的代码见 [call.rs:60-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L60-L68)：

```rust
FieldCallee::Method(func, target) => {
    let mut args = /* 求值 self.args() */;
    // Method calls pass the target as the first argument.
    args.insert(0, target_expr.span(), target);
    call_func(vm, func, args, span)
}
```

`eval_field_callee` 内部用一组 `if/else if` 链决定 `field` 到底解析成什么，优先级从高到低大致是（见 [call.rs:240-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L240-L314)）：

1. **target 类型 scope 上的方法**（`target.ty().scope().get(field)`）——例如数组的 `push`。
2. **content 元素 scope 上的方法**（`content.elem().scope().get(field)`）——例如某元素的特定方法。
3. **Symbol / Type / Module 的字段调用**（`target.field(...)`）——这些类型允许直接用字段调用语法。
4. **Func 的字段调用**（有特殊处理，涉及 settable 字段，详见 u4-l2）。
5. **其它（包括 Dict）**：即便字段存在也报错。

第 5 点是重点：**字典（Dict）的字段调用被显式禁止**。`disallowed_field_call_error`（[call.rs:332-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L332-L391)）会给字典一条专门的错误信息和 hint：

> dictionary keys cannot be used with method syntax as keys could conflict with built-in method names

原因是：如果允许 `(at: (x) => ...).at(key)` 这种写法，编译器就无法判断 `at` 到底是字典里的键（用户存的函数），还是数组/字典的内置方法 `at`。无论优先选哪个都会带来坏结果（详见 [call.rs:227-239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L227-L239) 的注释）。这一块的完整分派规则属于 u4-l2 的范围，本讲只勾勒骨架。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，理清「方法调用把 target 作为第一个参数」这一机制。

**操作步骤**：

1. 打开 [call.rs:53-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L53-L68)，对比 `FieldCallee::Func` 和 `FieldCallee::Method` 两个分支。
2. 注意 `Func` 分支**没有** `args.insert(0, ...)`，而 `Method` 分支**有**。
3. 思考：为什么 `str.len()` 这种「类型上的关联函数」不需要把 `str` 类型本身作为第一个参数？（提示：它是 `FieldCallee::Func`，不是 `Method`；`len` 的真正参数是它要测长的那个字符串。）

**需要观察的现象**：`Method` 分支多了一行 `args.insert(0, target_expr.span(), target)`；`Func` 分支没有。

**预期结果**：你能用自己的话解释「方法（Method）= 关联函数（Func）+ 自动把 receiver 作为第一个参数」。这就是 typst-eval 把「方法」统一进「函数」体系的 trick。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `(at: x => ...).at(key)` 这种「字典字段调用」被禁止？请用两个字概括核心矛盾。

**参考答案**：**歧义**。`at` 既是用户在字典里存的键，又是数组/字典的内置方法名；无论编译器优先选哪个（优先方法则新增方法成破坏性变更；优先字段则某些字典的方法调用失效），都会出错。所以 typst-eval 选择干脆禁止字典的字段调用语法，并在 `disallowed_field_call_error` 里给出「包一层括号 `(dict.at)(key)` 或去掉参数 `dict.at`」的修复提示。

**练习 2**：`FieldCallee::NonFunc` 在 code 模式和 math 模式分别会怎样？

**参考答案**：在 code 模式（`FuncCall::eval`），`NonFunc(_, err)` 直接 `Err(err).at(callee.span())` 报错；在 math 模式（`eval_math_call`），非函数的 callee **不会**报错，而是把参数当作内容渲染（走 `unparse_math_args`，用 `LrElem` 包裹），因为数学模式里 `$sin(x)$` 这种写法允许 `sin` 不是真函数。

---

### 4.3 call_func：函数执行与 stacker 防爆栈

#### 4.3.1 概念说明

`FuncCall::eval` 的两条分支最终都会调用自由函数 `call_func`。它负责真正「执行」这个函数：把 `Args` 交给 `Func`，拿到返回值。

这里有一个工程难题：typst-eval 是**树遍历解释器（tree-walking interpreter）**，每一次函数调用都是一次真实的 Rust 函数调用（`eval_closure` 又会新建 `Vm`、递归求值 body）。如果用户的 Typst 代码递归很深（比如手写一个递归到 100 层的函数），对应的 Rust 调用栈也会很深，最终可能**栈溢出（stack overflow）**，直接让进程崩溃——这比返回一个 Err 糟糕得多，因为崩溃无法被 typst 捕获成友好的错误。

`call_func` 用 `stacker` crate 解决这个问题：它在栈快要用完时，**在堆上分配一块新栈**继续执行，从而让「逻辑上的递归深度」不再受限于「物理栈大小」。

#### 4.3.2 核心流程

`call_func` 的流程（见 [call.rs:167-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L167-L181)）：

```
fn call_func(vm, func, args, span):
    func = func.spanned(span)             # 给函数贴上调用点 span
    point = || Tracepoint::Call(func.name())   # 构造「调用点」回溯信息
    f = || {
        func.call(&mut vm.engine, vm.context, args)   # 真正执行
            .trace(vm.world(), point, span)           # 失败时拼上调用栈回溯
    }

    if wasm32 平台:
        return f()                        # stacker 在 wasm 上坏了，直接执行
    else:
        stacker::maybe_grow(32KB, 2MB, f) # 红线 32KB / 新栈帧 2MB
```

两个参数的含义：

- `32 * 1024`（32 KB）：**红线（red zone）**。当剩余栈空间低于这个阈值时，触发栈迁移。
- `2 * 1024 * 1024`（2 MB）：**新栈帧大小**。在堆上分配的新栈大小。

`stacker::maybe_grow(red_zone, frame_size, f)` 的语义是：如果当前栈剩余空间多于 `red_zone`，就直接在当前栈上调用 `f`（零成本）；否则在堆上分配一个 `frame_size` 大小的新栈，把 `f` 放到新栈上执行。这样既保证了常见情况（浅递归）没有性能损失，又能在深递归时自动续命。

`Func::call` 的内部分派（[func.rs:335-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L335-L375)）会把执行交给不同后端：

```rust
match &self.inner {
    FuncInner::Native(native) => (native.function.0)(engine, context, &mut args)? ,
    FuncInner::Element(func)  => func.construct(engine, &mut args)? ,   // 构造元素
    FuncInner::Closure(closure) => (engine.library.routines.eval_closure)(...)  // 用户函数，回到 typst-eval
    FuncInner::Plugin(func)  => ... ,                                   // WASM 插件
    FuncInner::With(with)    => ... ,                                   // 部分应用的函数
}
```

其中 `FuncInner::Closure` 这一支会调用 `eval_closure`（本文件 [call.rs:634](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L634) 起），而 `eval_closure` 内部会用 `Route::extend(route)` **让调用深度加 1**，从而 `check_call_depth` 才能在下一层调用时感知到深度变化。这就形成了「深度检查 ↔ 实际递归」的闭环（详见 4.4）。

#### 4.3.3 源码精读

`call_func` 的关键代码，见 [call.rs:167-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L167-L181)：

```rust
fn call_func(vm: &mut Vm, func: Func, args: Args, span: Span) -> SourceResult<Value> {
    let func = func.spanned(span);
    let point = || Tracepoint::Call(func.name().map(Into::into));
    let f = || {
        func.call(&mut vm.engine, vm.context, args)
            .trace(vm.world(), point, span)
    };

    // Stacker is broken on WASM.
    #[cfg(target_arch = "wasm32")]
    return f();

    #[cfg(not(target_arch = "wasm32"))]
    stacker::maybe_grow(32 * 1024, 2 * 1024 * 1024, f)
}
```

三个要点：

1. **`func.spanned(span)`**：函数本身可能没有 span（如标准库函数），这里用「调用点 span」覆盖，保证后续报错能定位到 `f(...)` 这一行，而不是模糊地指向函数定义。
2. **`Tracepoint::Call`**：构造一个「调用回溯点」。当被调函数内部又报错时，`.trace(...)` 会把这个回溯点叠加到错误链上，最终形成类似「in `calc.min` called at ...」的调用栈信息，帮助用户理解错误传播路径。
3. **`#[cfg(target_arch = "wasm32")]`**：wasm32 平台上 `stacker` 有缺陷，所以直接 `return f()` 跳过栈保护。注释 `// Stacker is broken on WASM.` 直白说明了原因。这也是为什么 4.4 的 `check_call_depth` 在 wasm 上更关键——它是 wasm 平台唯一的深度防线。

#### 4.3.4 代码实践

**实践目标**：理解 `stacker` 的「红线 / 新栈」机制，以及 wasm 例外。

**操作步骤**：

1. 阅读源码 [call.rs:175-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L175-L180)。
2. 假设把 `32 * 1024` 改成 `1`（红线极小），思考会发生什么：几乎所有调用都会被认为「剩余栈不足」，从而频繁在堆上分配新栈，性能急剧下降，但功能仍正确。
3. 反过来把 `2 * 1024 * 1024` 改成 `64 * 1024`（新栈很小），思考：深递归时会更频繁地触发栈迁移，同样影响性能。
4. 思考：为什么 wasm 平台必须禁用 stacker？（源码注释已给答案。）

**需要观察的现象 / 预期结果**：你应当能解释「红线控制『何时迁移』，新栈大小控制『迁移成本/频率』」这一权衡。这两个常量是 typst 团队经验选定的平衡点。

> 本实践为参数调整推理型，无需真正改源码运行；若要实测，可标「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `stacker::maybe_grow` 的第一个参数叫「红线（red zone）」？

**参考答案**：因为它划定了一条「危险线」——只要剩余物理栈空间低于这个值（32 KB），就认为继续在当前栈上递归有栈溢出风险，于是提前在堆上开一块新栈（2 MB）来执行后续调用。它是「安全余量」的下限。

**练习 2**：`Tracepoint::Call(func.name())` 的作用是什么？

**参考答案**：它构造一个调用回溯点，记录「这次调用的是哪个函数」。当被调函数体内部抛出错误时，`.trace(world, point, span)` 会把这个回溯点串到错误的回溯链上，使最终错误带有「… called from `calc.min` at line X」这样的调用栈信息，便于用户定位错误来源。

---

### 4.4 check_call_depth：调用深度防线

#### 4.4.1 概念说明

`stacker` 解决了「物理栈溢出」，但还有一个问题：**无限递归**。如果用户的 Typst 函数永远递归调用自己（`#let f() = { f() }`），即便物理栈不会溢出（有 stacker 兜底），程序也会无限运行下去。typst-eval 需要一个**逻辑上的深度上限**来及时识别并报告这个问题，这就是 `route.check_call_depth()`。

它和 `stacker` 是两道互补的防线：

| 防线 | 机制 | 触发后果 | 防的是什么 |
|------|------|----------|-----------|
| `check_call_depth` | 逻辑计数，`Route::within(80)` | 返回 Err（用户错误） | 无限递归 / 过深嵌套 |
| `stacker` | 物理栈剩余空间 | 在堆上开新栈（无感知） | 物理栈溢出崩溃 |

注意区分**三种**「深度/循环」相关机制（u6-l3 会统一讲）：

- `route.contains(id)`：检测**循环求值/循环导入**（A 模块求值时又回到 A），命中直接 `panic!`（编译器内部错误）。
- `check_call_depth`：检测**函数调用嵌套过深**，命中返回 Err（用户错误）。
- `check_show_depth`（不在本讲范围）：检测 show 规则嵌套过深。

#### 4.4.2 核心流程

`Route` 是一条「调用链」的不可变记录（见 [engine.rs:283-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L283-L334)）。它由一串「段（segment）」链成，每段记录一个 `len`（本段贡献的深度）和指向 `outer`（外层段）的指针。`Route::within(depth)` 递归地把各段 `len` 加起来，判断总深度是否 ≤ `depth`。

流程：

```
进入一次函数调用:
    call_func → func.call → eval_closure
        eval_closure 用 Route::extend(route) 新建一个段       # 深度 +1
        再求值 body，body 里若有 f() 又会回到 FuncCall::eval

下一次 FuncCall::eval 开头:
    vm.engine.route.check_call_depth()   # 累加各段 len，判断 <= 80?
        若超过 80 -> bail!("maximum function call depth exceeded")  # 返回 Err
        否则 -> 继续
```

为什么 `MAX_CALL_DEPTH` 是 80？源码注释（[engine.rs:336-339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L336-L339)）说明：不同检查（show=64、call=80、layout=72）的阈值故意不同，使得「当多种深度问题交织时，阈值最低的那种优先报错」，从而给出更准确的错误类型。

#### 4.4.3 源码精读

常量与检查函数见 [engine.rs:350-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L350-L393)：

```rust
/// The maximum function call nesting depth.
const MAX_CALL_DEPTH: usize = 80;

/// Ensures that we are within the maximum function call depth.
pub fn check_call_depth(&self) -> StrResult<()> {
    if !self.within(Route::MAX_CALL_DEPTH) {
        bail!("maximum function call depth exceeded");
    }
    Ok(())
}
```

`within` 的实现见 [engine.rs:405-428](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L405-L428)，它沿着 `outer` 链累加 `len`，并用一个 `upper`（`AtomicUsize`）做**缓存**：记下「已确认不超限的上界」，避免每次都遍历整条链（注释解释：精确长度会破坏 comemo 缓存复用，所以用上界近似）。

`Route::extend` 见 [engine.rs:295-302](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L302)，它在 `eval_closure` 里被调用（[call.rs:672](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L672) 的 `route: Route::extend(route)`），每进入一次用户函数调用就新增一段、深度 +1。

一个值得强调的对比：`check_call_depth` 返回的是 `StrResult`（普通用户错误，用 `bail!`），最终经 `.at(span)` 变成带定位的诊断；而 `route.contains` 命中循环时是 `panic!`（见 u1-l3）。二者的区别本质上是「这是用户写错了（递归太深）」还是「编译器自己出了内部问题（出现了不该出现的循环求值）」。

#### 4.4.4 代码实践

**实践目标**：亲手触发 `MAX_CALL_DEPTH`，验证它是「友好的 Err」而非崩溃。

**操作步骤**：

1. 新建示例 Typst 文件 `recurse.typ`（示例代码）：

   ```typst
   #let f(n) = {
     f(n + 1)   # 无限递归
   }
   #f(0)
   ```

2. 编译：`typst compile recurse.typ`。

**需要观察的现象**：编译失败，错误信息为 `maximum function call depth exceeded`，且**进程没有崩溃**（退出码非 0，但不是段错误/panic）。

**预期结果**：你看到的是一条正常的 typst 诊断错误，定位到 `f(n + 1)` 这一行。这正是 `check_call_depth` 在第 81 层调用时拦截的结果。如果看到的是「stack overflow / segmentation fault」，则说明 `stacker` 没生效（可能编译成了 wasm 或栈配置异常），请核对平台。

> 待本地验证（取决于本地 typst 版本与平台）。

#### 4.4.5 小练习与答案

**练习 1**：`check_call_depth` 命中时返回 Err，而 `route.contains` 命中循环时 `panic!`。为什么两者的处理方式不同？

**参考答案**：函数调用过深通常是**用户代码的问题**（写了过深或无限递归），属于「预期的用户错误」，应当返回 Err 并友好提示；而「循环求值/循环导入」（模块 A 求值时又依赖 A）在 typst 的设计里被认为是**不应该发生**的——因为有 `route` 跟踪本应避免，一旦出现说明编译器逻辑有漏洞，所以用 `panic!` 当作内部错误暴露出来。

**练习 2**：为什么在 wasm32 平台上 `check_call_depth` 比 `stacker` 更重要？

**参考答案**：因为 `stacker` 在 wasm32 上有缺陷被禁用（`call_func` 里 `#[cfg(target_arch = "wasm32")] return f();`），物理栈保护失效。此时 `check_call_depth` 成了 wasm 平台上**唯一**能阻止无限递归把栈打穿的机制，所以它的存在对 wasm 尤为关键。

---

### 4.5 Args::eval：位置 / 命名 / spread 参数求值

#### 4.5.1 概念说明

函数调用的参数列表（`a, b: 1, ..rest`）在 AST 里是 `ast::Args` 节点。它包含三类语法项：

- **位置参数（Pos）**：`a`——按位置传递，没有名字。
- **命名参数（Named）**：`b: 1`——带名字 `b`。
- **展开参数（Spread）**：`..rest`——把 `rest` 这个值「拆开」后并进参数列表。

`Args::eval` 的任务是把这三类语法项统一转成一个运行时的 `Args` 结构体（定义在 typst-library），其核心是一个 `Arg` 列表：

```rust
pub struct Args {
    pub span: Span,            // 整个调用的 span（不是参数列表的 span）
    pub items: EcoVec<Arg>,    // 位置参数与命名参数混在一起
}

pub struct Arg {
    pub span: Span,
    pub name: Option<Str>,     // None 表示位置参数
    pub value: Spanned<Value>,
}
```

#### 4.5.2 核心流程

`Args::eval` 遍历每个参数项，按下表处理：

| 语法项 | 处理方式 | 生成的 `Arg.name` |
|--------|----------|-------------------|
| `Pos(expr)` | 求 `expr` 的值 | `None` |
| `Named(name: expr)` | 求 `expr`，名字取 `name` | `Some(name)` |
| `Spread(..expr)` | 先求 `expr`，再按其值类型展平 | 见下表 |

`Spread` 的展开规则是本模块的重点。它求值 `expr` 后，根据值的类型分派：

| spread 的值类型 | 行为 |
|-----------------|------|
| `Value::None` | 跳过（什么都不加） |
| `Value::Array` | 把每个元素作为**位置参数**并入 |
| `Value::Dict` | 把每个 `(key, value)` 作为**命名参数**（name=key）并入 |
| `Value::Args` | 直接把已有的 `Args.items` 全部并入（参数转发） |
| 其它 | 报错 `cannot spread <type>` |

这套规则让 `..` 既能展开数组（`calc.min(..nums)`），又能展开字典（`text(..style)`），还能把一组参数整体转发（`f(..args)`），语义统一且直观。

#### 4.5.3 源码精读

`Args::eval` 完整实现见 [call.rs:403-453](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L403-L453)。三类参数的处理：

**位置参数**（[call.rs:412-418](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L412-L418)）：

```rust
ast::Arg::Pos(expr) => {
    items.push(Arg {
        span,
        name: None,                                       // 位置参数无名字
        value: Spanned::new(expr.eval(vm)?, expr.span()),
    });
}
```

**命名参数**（[call.rs:419-426](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L419-L426)）：

```rust
ast::Arg::Named(named) => {
    let expr = named.expr();
    items.push(Arg {
        span,
        name: Some(named.name().get().clone().into()),    // 名字取自语法
        value: Spanned::new(expr.eval(vm)?, expr.span()),
    });
}
```

**展开参数**（[call.rs:427-445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L427-L445)）：

```rust
ast::Arg::Spread(spread) => match spread.expr().eval(vm)? {
    Value::None => {}                                          // 跳过
    Value::Array(array) => {
        items.extend(array.into_iter().map(|value| Arg {      // 展成位置参数
            span, name: None,
            value: Spanned::new(value, span),
        }));
    }
    Value::Dict(dict) => {
        items.extend(dict.into_iter().map(|(key, value)| Arg {  // 展成命名参数
            span, name: Some(key),
            value: Spanned::new(value, span),
        }));
    }
    Value::Args(args) => items.extend(args.items),             // 参数转发
    v => bail!(spread.span(), "cannot spread {}", v.ty()),     // 其它报错
},
```

**为什么 `Args` 的 span 用 `Span::detached()`？** 见 [call.rs:449-451](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L449-L451)：

```rust
// We do *not* use the `self.span()` here because we want the callsite
// span to be one level higher (the whole function call).
Ok(Args { span: Span::detached(), items })
```

`Args::eval` 不知道「调用点」是哪一行——它只负责参数列表本身。但报错时我们希望定位到**整条调用语句**（`f(a, b: 1)` 整体），而不是只定位到 `(a, b: 1)` 这段参数列表。所以 `Args::eval` 故意返回 `Span::detached()`（空 span），把「贴 span」的责任交给调用方：`FuncCall::eval` 在拿到 `Args` 后用 `.spanned(span)`（[call.rs:78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L78)）把整条 `FuncCall` 的 span 贴上去。`Args::spanned` 只在 span 还是 detached 时才覆盖（见 [args.rs:78-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L78-L79) `if self.span.is_detached()`），这种「留空 + 上层补齐」的设计让 span 信息总是指向最准确的层级。

> 补充：数学模式的 `MathArgs::eval`（[call.rs:455-540](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L455-L540)）逻辑类似，但额外用分号 `;` 支持「二维参数」（如 `mat(a, b; c, d)` 表示矩阵的行），把分号前的位置参数打包成数组。本讲不展开。

#### 4.5.4 代码实践

**实践目标**：追踪一次 `f(a, b: 1, ..rest)` 的完整求值流程，把本讲 4 个模块串起来。

**操作步骤**：对照源码，逐步填出下表中每个阶段的产物（这是「源码阅读 + 表格推理」型实践）：

| 阶段 | 对应源码 | `f(a, b: 1, ..rest)` 的产物 |
|------|----------|------------------------------|
| 1. 深度检查 | [call.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L31) | `check_call_depth()` 通过（假设深度未超 80） |
| 2. 求 callee | [call.rs:73-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L73-L77) | `f` 求值并 cast 成 `Func` |
| 3. 求 args | [call.rs:78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L78) → [call.rs:403-453](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L403-L453) | 生成 `Args`，其 `items` 含 3 类项（见下） |
| 4. 执行 | [call.rs:79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L79) → `call_func` | 调用 `f`，返回 `Value` |

阶段 3 中 `Args.items` 的具体内容（假设 `rest` 是数组 `(2, 3)`）：

- `a`（Pos）→ `Arg { name: None, value: <a 的值> }`
- `b: 1`（Named）→ `Arg { name: Some("b"), value: 1 }`
- `..rest`（Spread，rest 是 Array）→ 展平为 `Arg { name: None, value: 2 }` 和 `Arg { name: None, value: 3 }`

**需要观察的现象 / 预期结果**：你能解释清楚——`b: 1` 的 `name` 是 `Some("b")`，而 `..rest` 展开出的两项 `name` 都是 `None`（因为数组元素是位置参数）。若 `rest` 是字典 `(x: 9)`，则展开项的 `name` 会是 `Some("x")`。

**回答实践任务里的问题**：为什么 `Args` 的 span 用 `Span::detached()` 而不是 `self.span()`？——因为 `Args::eval` 只看到参数列表，无法知道整条调用语句的位置；它故意留空，让 `FuncCall::eval` 用 `.spanned(span)` 把「整条函数调用」的 span 贴上去，从而报错时能高亮到 `f(...)` 整体而非仅参数括号。

#### 4.5.5 小练习与答案

**练习 1**：`..rest` 中，如果 `rest` 的值是 `none`，会发生什么？

**参考答案**：命中 `Value::None => {}` 分支，什么都不做——展开一个 `none` 等于不传入任何参数。这让 `..none` 成为一种安全的「条件性传参」惯用法（例如 `..(if cond { (x,) } else { none })`）。

**练习 2**：如果 `rest` 既不是数组、也不是字典、也不是 `Args`、也不是 `none`（比如是个整数），会怎样？

**参考答案**：命中最后的 `v => bail!(spread.span(), "cannot spread {}", v.ty())` 分支，报错「cannot spread integer」，定位到 `..rest` 的 spread 位置。这说明 typst 只允许对「可展开」的类型（none/array/dict/args）使用 `..`。

**练习 3**：为什么命名参数和位置参数能混在同一个 `items: EcoVec<Arg>` 里，而不需要分两个集合？

**参考答案**：因为每个 `Arg` 都自带 `name: Option<Str>` 字段——`None` 就是位置参数，`Some(...)` 就是命名参数。被调函数在取参数时，用 `args.expect::<T>()`（取下一个位置参数）或 `args.named::<T>("name")`（按名字取）来区分读取，所以存储层只需一个统一的列表即可。

## 5. 综合实践

把本讲的四个模块串成一条完整的调用链。请完成下面的「调用链追踪表」。

**任务背景**：考虑这段 Typst 代码（示例代码）：

```typst
#let nums = (2, 3, 5)
#let style = (fill: blue)
#calc.min(1, ..nums, ..style)   # 这条调用是要追踪的目标
```

`calc.min` 接收若干位置参数返回最小值，同时我们把字典 `style` 也 spread 进去（虽然 min 不接收命名参数，会报错，但这不影响我们追踪**求值阶段**）。

**请你完成**：

1. **画出 `FuncCall::eval` 的执行顺序**：从 `check_call_depth` 开始，到 `call_func` 结束，列出每一步对应的源码行号。
2. **写出 `Args::eval` 处理 `1, ..nums, ..style` 后 `items` 的内容**（假设 `nums = (2,3,5)`、`style = (fill: blue)`）。提示：注意位置参数和命名参数如何混在一起。
3. **判断**：这条调用最终会在哪一步失败？是 typst-eval 的 `Args::eval` 阶段，还是 `calc.min` 内部（native 函数）的 `args.finish()` 阶段？为什么？（提示：typst-eval 只负责把参数收集成 `Args`，是否有多余参数由被调函数自己用 `args.finish()` 检查，见 [func.rs:344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L344)。）

**参考答案要点**：

1. 顺序：`check_call_depth`([call.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L31)) → 求 callee `calc.min` 并 cast 成 `Func`([call.rs:73-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L73-L77)) → 求 args([call.rs:78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L78)) → `call_func`([call.rs:79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L79)) → `func.call` → native `calc.min` 执行。
2. `items` 为：`[Arg{name:None, value:1}, Arg{name:None, value:2}, Arg{name:None, value:3}, Arg{name:None, value:5}, Arg{name:Some("fill"), value:blue}]`。即 `1` 和数组 `nums` 展开出的 `2,3,5` 都是位置参数（`name:None`），而字典 `style` 展开出的 `fill: blue` 是命名参数（`name:Some("fill")`）。
3. 失败发生在 **native 函数内部**：typst-eval 的 `Args::eval` 会成功收集出上述 `Args`（它不校验参数是否合法），错误发生在 `calc.min` 的原生实现里——它不认识命名参数 `fill`，且 `args.finish()` 检测到有未消费的参数，从而报错。这体现了 typst-eval 与 typst-library 的分工：**eval 负责「收集」，library 负责「校验」**。

## 6. 本讲小结

- `FuncCall::eval` 是函数调用的总分发器：开头先 `check_call_depth`，再按 callee 是「普通表达式」还是「字段访问」分两条路径，最后都汇入 `call_func`。
- **求值顺序固定为「callee 在前、args 在后」**，源码注释明确写出，保证了 callee 出错时不会白白求值 args。
- **字段访问型 callee** 通过 `FieldCallee`（Method/Func/NonFunc）分派；方法调用会把 `target` 作为第一个位置参数塞进 `args`，从而把「方法」统一进「函数」机制；字典的字段调用被禁止以避免歧义。
- `call_func` 用 `stacker::maybe_grow(32KB, 2MB, f)` **动态迁移调用栈**防止物理栈溢出，wasm32 平台因 stacker 缺陷而禁用此机制。
- `check_call_depth` 是**逻辑深度防线**（上限 80 层），命中返回友好的 Err；它与 `stacker`（防物理溢出）、`route.contains`（防循环求值，panic）是三种不同机制，不可混淆。
- `Args::eval` 把 `Pos`/`Named`/`Spread` 三类参数统一成 `Arg` 列表；spread 按 `none`/`Array`/`Dict`/`Args` 分别展平或跳过；`Args` 故意用 `Span::detached()`，把贴 span 的责任上交给 `FuncCall::eval`，使报错定位到整条调用语句。

## 7. 下一步学习建议

- **u4-l2 字段访问与方法调用分派**：本讲 4.2 只勾勒了 `eval_field_callee` 的骨架。下一讲会深入它的四条优先级路径、`disallowed_field_call_error` 的各类提示、以及 settable 字段在 context 下的兜底逻辑。
- **u4-l3 闭包定义与 eval_closure 执行**：本讲多次提到 `call_func → func.call → eval_closure`，但 `eval_closure` 如何新建 `Scopes`、绑定参数、处理 `return`，留待 u4-l3 精读。建议带着「4.3 里 `Route::extend(route)` 让深度 +1」的认知去读。
- **u5-l3 可变访问 Access 与内置方法**：本讲 4.2 提到的 `maybe_resolve_mutating` 变更方法快路径，其底层是 `Access` trait，u5-l3 会完整讲解 `push`/`pop`/`insert` 等方法如何拿到 `&mut Value`。
- **u6-l3 递归安全、栈增长与缓存**：本讲的「三道防线」初探会在 u6-l3 统一收束，并补充 `comemo::memoize` 如何缓存 `eval_closure`、`Tracked<World>` 等参数如何参与缓存键判等。
