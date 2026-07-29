# 代码块、内容块与作用域进出

## 1. 本讲目标

上一讲（u2-l1）我们读懂了 `code.rs` 里最朴素的叶子节点——字面量与标识符是怎么被求值的。本讲我们继续沿着 `ast::Expr::eval` 这个总分发器往前走，进入两类「容器」节点：**代码块** `{ ... }` 与**内容块** `[ ... ]`，以及它们背后的核心求值函数 `eval_code`。

读完本讲，你应当能够：

- 说清楚代码块、内容块求值时如何用 `Scopes::enter` / `exit` 划分**词法作用域**，变量为什么「出了块就看不见」。
- 读懂 `eval_code` 如何**流式**迭代一段表达式流、用 `ops::join` 把结果拼接起来，并在遇到 `break` / `continue` / `return` 时如何中断。
- 解释 `eval_code` 开头那句 `let flow = vm.flow.take()` 与结尾 `if flow.is_some() { vm.flow = flow }` 构成的「保存-恢复」模式，为何能让任意层级的嵌套块都正确传递控制流。
- 理解 `set` / `show` 规则为什么在代码块里会被当作「**样式作用域**」、作用在它**之后**的所有内容（tail）上。

本讲几乎全部内容都集中在 [`crates/typst-eval/src/code.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) 这一个文件里，并少量借助 `vm.rs`、`flow.rs`、`rules.rs` 以及 `typst-library` 里的 `scope.rs`、`ops.rs` 来补全上下游细节。

## 2. 前置知识

在进入源码前，先用最通俗的语言把几个关键词讲清楚。

- **代码块（code block）`{ ... }`**：Typst 里用花括号包起来的一段「代码模式」语句序列，语句之间用分号或换行分隔。它的求值结果是一个 `Value`（往往是一段内容，也可能是数字、数组等）。
- **内容块（content block）`[ ... ]`**：用方括号包起来的一段「标记模式」文本，里面可以混排文字、`*粗体*`、`#表达式` 等。它的求值结果是一段 `Content`（即排版内容）。
- **词法作用域（lexical scope）**：变量「在源码文本上的可见范围」。一个 `let` 在块里定义的变量，离开这个块就失效。Typst 用一个**作用域栈**来实现它：进入块压一层、离开块弹一层。
- **控制流事件（flow event）**：`break`、`continue`、`return` 这三类语句不是普通表达式，它们会向虚拟机发出一个「控制流信号」，由循环或函数在合适的层级「消费」。本讲的 `eval_code` 本身**不消费**任何信号，只负责让它穿过去。
- **tail（尾部）**：在 `eval_code` 里，遇到一条 `set`/`show` 规则后，**同层级中它之后的所有表达式**就叫做它的 tail。`set`/`show` 的样式会包裹这个 tail。

> 名词对照：本讲会反复出现 `Value`（运行时值）、`Content`（排版内容）、`Scopes`（作用域栈）、`FlowEvent`（控制流事件）、`Styles`（样式表）、`Recipe`（show 规则配方）。它们的精确含义会在用到时结合源码点明。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-eval/src/code.rs` | 本讲主战场。包含 `Code::eval`、`eval_code`、`CodeBlock::eval`、`ContentBlock::eval`、`Parenthesized::eval`，以及 `set`/`show` 在表达式流里的特殊处理。 |
| `crates/typst-eval/src/vm.rs` | 定义虚拟机 `Vm`，本讲用到 `Vm::bind`（绑定变量时附带 trace）与 `trace_at`。作用域栈 `vm.scopes` 是 `Vm` 的一个字段。 |
| `crates/typst-eval/src/flow.rs` | 定义 `FlowEvent` 枚举，以及 `LoopBreak`/`LoopContinue`/`FuncReturn` 如何「只在没有待处理事件时才写入 `vm.flow`」。理解这一点是看懂保存-恢复模式的关键。 |
| `crates/typst-eval/src/rules.rs` | `SetRule::eval` 返回 `Styles`、`ShowRule::eval` 返回 `Recipe`。本讲关注它们被 `eval_code` 调用后如何作用到 tail。 |
| `crates/typst-eval/src/lib.rs` | 顶层入口 `eval()` / `eval_string()`：求值结束后若 `vm.flow` 仍有残留事件，就用 `forbidden()` 报错。这是控制流「不能泄漏到模块顶层」的兜底。 |
| `crates/typst-library/src/foundations/scope.rs` | `Scopes` 的 `enter`/`exit`/`get`/`top`，以及 `Binding`。作用域的物理实现。 |
| `crates/typst-library/src/foundations/ops.rs` | `ops::join`：把两个值拼接成一个，是 `eval_code` 累加结果的核心。 |

## 4. 核心概念与源码讲解

### 4.1 词法作用域的进出：代码块、内容块与 Scopes

#### 4.1.1 概念说明

无论是代码块 `{ let x = 1; x + 1 }` 还是内容块 `[ #let x = 1; #x ]`，它们都有一个共同的性质：**在块里 `let` 出来的变量，出了块就不可见**。这就是词法作用域。

Typst 的实现思路很直白——维护一个**作用域栈**（`Scopes`）。它有两个字段：当前活动作用域 `top`，以及下方历史作用域组成的栈 `scopes`，再加上一个指向标准库的 `base`：

```rust
// crates/typst-library/src/foundations/scope.rs
pub struct Scopes<'a> {
    /// The active scope.
    pub top: Scope,
    /// The stack of lower scopes.
    pub scopes: Vec<Scope>,
    /// The standard library.
    pub base: Option<&'a Library>,
}
```

「进入一个块」就是把当前的 `top` 压入 `scopes`，并换上一个全新的空 `top`；「离开一个块」就把栈顶弹回来成为新的 `top`。查找变量时，先查 `top`，再沿着 `scopes` 从新到旧逐层查，最后兜底查标准库 `base`。

#### 4.1.2 核心流程

代码块与内容块的求值流程几乎一模一样，只有「求值 body 得到的类型」不同：

```text
CodeBlock::eval            ContentBlock::eval
      │                          │
  scopes.enter()             scopes.enter()      ← 压栈，开新作用域
      │                          │
  body().eval(vm)            body().eval(vm)     ← body 分别是 Code / Markup
      │                          │
  scopes.exit()              scopes.exit()       ← 弹栈，恢复旧作用域
      │                          │
   Value                       Content
```

关键点：`enter` 与 `exit` **总是成对出现**，即便 body 求值中途出错（`?` 提前返回）也不会跳过 `exit` 吗？——注意源码里用的是 `let output = self.body().eval(vm)?;`，`?` 一旦出错会直接从函数返回，此时 `scopes.exit()` **不会执行**。这在 Typst 里是安全的，因为求值失败后这个 `Vm`/模块整体会被丢弃，作用域栈不再使用。这一点在 4.3 的练习里会再讨论。

#### 4.1.3 源码精读

先看 `Code` 节点本身。`ast::Code` 只是把一串表达式交给 `eval_code`（下一节详述）：

> [crates/typst-eval/src/code.rs:17-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L17-L23) —— `Code::eval` 把 `self.exprs()` 这个表达式迭代器交给 `eval_code`，产出单个 `Value`。

代码块的实现极其精简——进作用域、求 body、出作用域：

> [crates/typst-eval/src/code.rs:322-331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L322-L331) —— `CodeBlock::eval`：`scopes.enter()` → `self.body().eval(vm)`（`body()` 是 `ast::Code`）→ `scopes.exit()`。

内容块只有两点不同：输出类型是 `Content`，`body()` 是 `ast::Markup`：

> [crates/typst-eval/src/code.rs:333-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L333-L342) —— `ContentBlock::eval`：同样的 enter/exit 三明治，但产出 `Content`。

`enter` / `exit` 的实现值得看一眼，它用了一个小巧的技巧 `std::mem::take`：

> [crates/typst-library/src/foundations/scope.rs:33-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L33-L43) —— `enter` 把当前 `top` 整体搬进 `scopes`（`take` 后 `top` 变成默认的空 `Scope`）；`exit` 反向把栈顶弹回 `top`。整个操作没有复制变量表，只是搬移所有权。

注意 `exit` 的文档注释写得很明确：「This panics if no scope was entered.」——弹空栈会 panic，所以 `enter`/`exit` 必须严格配对。

**括号表达式 `Parenthesized`**。顺便提一个容易被误会的节点：括号 `( expr )`。你可能会以为括号也开一个新作用域——其实没有：

> [crates/typst-eval/src/code.rs:344-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L344-L350) —— `Parenthesized::eval` 直接返回 `self.expr().eval(vm)`，**既不 enter 也不 exit**，括号在求值层面是「透明」的，只影响解析时的优先级。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「块内 `let` 的变量出了块就不可见」，并对照源码理解为什么。

**操作步骤**（源码阅读型实践）：

1. 准备一个最小 Typst 文件 `scopes.typ`：

   ```typ
   #let outer = {
     let a = 10
     let inner = { let b = 20; a + b }   // b 只在内层块里可见
     // b            // ← 取消注释这一行，会报 "unknown variable: b"
     inner
   }
   #outer
   ```

2. 在本地用 `typst compile scopes.typ` 编译，先看到输出 `30`。
3. 取消注释倒数第三行 `#b`，重新编译，观察报错信息是否为 `unknown variable: b`。
4. 对照源码：内层 `{ let b = 20; a + b }` 走 `CodeBlock::eval`，进入时 `scopes.enter()` 压了一层，`b` 被绑在这一层；离开时 `scopes.exit()` 把这一层弹掉。回到外层后，`Scopes::get("b")` 在 `top`、`scopes`、`base` 里都找不到，于是返回 `unknown_variable("b")`（见 `scope.rs` 的 `get` 实现）。

**需要观察的现象**：块外引用块内变量时报 `unknown variable`；块内可以引用块外变量（`a`），因为查找会沿 `scopes` 向下穿透。

**预期结果**：注释状态下输出 `30`；取消注释后编译失败并提示 `unknown variable: b`。若本地未安装 typst，标记为「待本地验证」，但报错原因可由源码确定推导得出。

#### 4.1.5 小练习与答案

**练习 1**：下面两段代码，哪一段会报错？为什么？

```typ
// A
#let x = { let y = 1; (y) }
#x

// B
#let x = { let y = 1; y }
#{ let y = 2; x + y }
```

> **答案**：B 会报错（`unknown variable: y`），A 不会。A 里的 `(y)` 只是 `Parenthesized`，求值时透明地返回 `y` 的值，`y` 仍在它被定义的那个块的作用域内。B 里第二个块 `{ let y = 2; x + y }` 是一个**新的**代码块，与第一个块的作用域互不相干，第一个块的 `y` 早已随 `scopes.exit()` 弹出。

**练习 2**：如果把 `Scopes::exit` 改成 `self.scopes.pop()`（去掉 `expect`），在最外层模块求值时多调用一次 `exit` 会怎样？

> **答案**：原实现会 `expect("no pushed scope")` 直接 panic（属于内部错误）。改成裸 `pop` 后返回 `Option<Scope>`，`self.top` 会被赋成 `None.unwrap()` 之外的处理结果——总之破坏了 `enter`/`exit` 配对不变量。这正是源码用 `expect` 显式断言的原因：作用域失衡是解释器的 bug，应当尽早暴露而非静默吞掉。

---

### 4.2 eval_code：流式求值与 ops::join 拼接

#### 4.2.1 概念说明

`Code` 节点本质上是「一串表达式」，但 Typst 的求值器并不先把这些表达式收集成一个 `Vec` 再统一处理，而是**流式**地吃进一个迭代器 `Iterator<Item = ast::Expr>`，一条一条求值、一条一条拼接。这个把迭代器吃干抹净的函数就是 `eval_code`。

它的产出是**单个 `Value`**：所有表达式的值被 `ops::join` 累加在一起。这也是为什么 Typst 的代码块「每一条语句的结果都会被拼进输出」（比如 `{ "a"; "b" }` 的结果是内容 `ab`，而 `{ 1; 2 }` 里 `1` 与整数 `2` 无法 join 会报错）。

#### 4.2.2 核心流程

`eval_code` 的骨架（先忽略 `set`/`show` 与 flow 的细节，那些在 4.3、4.4 讲）：

```text
fn eval_code(vm, exprs):
    flow = vm.flow.take()          # 保存进入前的控制流事件
    output = Value::None
    for expr in exprs:
        value = expr.eval(vm)      # 求值当前表达式
        output = ops::join(output, value)   # 累加
        if vm.flow 有事件:          # 本条触发了 break/continue/return
            warn_for_discarded_content(...) # 必要时警告
            break
    if flow.is_some():             # 若进入前就有事件，恢复它
        vm.flow = flow
    return output
```

`ops::join` 的关键规则是：**与 `None` 拼接等于原值**。所以初始值 `Value::None` 在第一条结果就位后会被「替换」，后续每条都用 join 累加。

#### 4.2.3 源码精读

先看 `eval_code` 的整体结构（`set`/`show` 分支先折叠成注释，4.4 再展开）：

> [crates/typst-eval/src/code.rs:26-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L26-L74) —— 整个 `eval_code` 函数。注意第 30 行 `let flow = vm.flow.take();`、第 61 行 `ops::join`、第 63–66 行对 `vm.flow` 的检测与中断、第 69–71 行的恢复。

拼接逻辑全在 `ops::join`，它是一个对 `(Value, Value)` 的大 `match`：

> [crates/typst-library/src/foundations/ops.rs:23-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L23-L44) —— `join` 的规则：`(a, None) => a`、`(None, b) => b`（None 是拼接单位元）；同类内容/字符串/数组/字典分别拼接；其余组合 `mismatch!` 报「cannot join X with Y」。

这里有一个非常重要的设计：`eval_code` 收到的是一个**可变迭代器** `&mut impl Iterator`。这意味着当遇到 `set`/`show` 时，可以把**同一个迭代器**递归地传给内层的 `eval_code` 去吃「剩余的表达式」（即 tail）。下一节会看到，迭代器的「继续往后吃」正好对应 tail 的概念。

`Expr::eval`（总分发器）里，`CodeBlock` 与 `ContentBlock` 是如何被调用的，作为衔接：

> [crates/typst-eval/src/code.rs:124-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L124-L125) —— 在 `Expr::eval` 的总分发 match 中，`Self::CodeBlock(v) => v.eval(vm)`（产出已是 `Value`），`Self::ContentBlock(v) => v.eval(vm).map(Value::Content)`（把 `Content` 包成 `Value`）。

#### 4.2.4 代码实践

**实践目标**：通过 join 的报错，直观感受「代码块里每条语句的结果都会被拼接」。

**操作步骤**：

1. 写一段会触发 join 失败的代码：

   ```typ
   #assert.eq({ "a"; "b" }, "ab")   // 两条字符串 join 成 "ab"
   // #assert.eq({ 1; 2 }, 3)       // ← 取消注释：1 与 2 都是 Int，但 join 不做加法
   ```

2. 编译第一行，确认输出正常（`"ab"`）。
3. 取消注释第二行，编译，阅读报错信息。

**需要观察的现象**：第二行会报类似 `cannot join int with int` 的错误。这说明代码块不是「取最后一条语句的值」，而是把所有值 join 起来；整数之间没有 join 语义，所以失败。

**预期结果**：第一行通过；第二行报 `cannot join ...`。这是 `ops.rs` 第 42 行 `mismatch!("cannot join {} with {}", a, b)` 的直接体现。「待本地验证」运行命令，但结论由源码确定。

**源码阅读型追问**：在 `eval_code` 里，`output` 的初值为什么是 `Value::None` 而不是 `Value::Content(Content::empty())`？——因为 `ops::join(None, b) => b`，用 `None` 当单位元能干净地「吸收」第一条结果，无论它是内容、字符串还是别的。

#### 4.2.5 小练习与答案

**练习 1**：`eval_code` 用 `ops::join` 累加，那 `{ none; none; "x" }` 的结果是什么？

> **答案**：结果是内容 `"x"`。前两条 `none` 与 `None` 拼接仍为 `None`，最后 `join(None, "x") => "x"`。

**练习 2**：为什么 `eval_code` 要传 `&mut impl Iterator` 而不是 `&[ast::Expr]`（切片）？

> **答案**：因为 `set`/`show` 分支需要把「当前位置之后的所有表达式」递归交给内层 `eval_code` 求值（求 tail）。用可变迭代器，外层迭代器直接把剩下的元素「让渡」给内层，内层消费完后外层迭代器恰好到尾，无需拷贝或索引计算。切片做不到这种「消费式」的共享推进。

---

### 4.3 控制流的保存-恢复：`vm.flow.take()` 与嵌套块

#### 4.3.1 概念说明

`break`、`continue`、`return` 不是普通表达式：它们会向虚拟机发出一个**控制流事件** `FlowEvent`，然后这个事件要沿调用栈向上「漂」，直到遇到愿意消费它的构造——循环消费 `break`/`continue`，函数消费 `return`。

`eval_code` 本身**不消费**任何事件（它不是循环也不是函数体），但它必须正确地让事件**穿过**自己。难点在于嵌套：同一个 `vm.flow` 字段是全局共享的，`eval_code` 经常被递归调用（代码块里套代码块、set 的 tail 递归等），如何保证「内层产生的事件能传到外层」「外层已有的事件不被内层误伤」？

答案就是开头那句 `let flow = vm.flow.take();` 与结尾 `if flow.is_some() { vm.flow = flow; }` 组成的**保存-恢复**模式。

先认识事件本身。`FlowEvent` 有三个变体，`Return` 还多两个槽位（可选的显式返回值、是否「条件返回」）：

> [crates/typst-eval/src/flow.rs:14-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L14-L22) —— `Break(Span)`、`Continue(Span)`、`Return(Span, Option<Value>, bool)`。

#### 4.3.2 核心流程

保存-恢复模式的关键，是**三个事实的配合**：

1. **进入时取出**：`let flow = vm.flow.take();` 把进入前可能存在的事件搬到局部变量 `flow`，并把 `vm.flow` 清空。这样后面 `if let Some(event) = &vm.flow` 检测到的，就**只可能是本次求值新生成**的事件。

2. **生成方有「不覆盖」护栏**：`break`/`continue`/`return` 在写入 `vm.flow` 前都会先检查 `vm.flow.is_none()`，**已有事件时不覆盖**：

   ```text
   LoopBreak::eval:    if vm.flow.is_none() { vm.flow = Some(Break(span)) }
   FuncReturn::eval:   if vm.flow.is_none() { vm.flow = Some(Return(...)) }
   ```

   这意味着：如果进入 `eval_code` 时已经有一个事件（被取到 `flow` 里），那么本次循环里的表达式**永远不会**写出新事件去顶撞它。

3. **离开时按需恢复**：`if flow.is_some() { vm.flow = flow; }`——只有当进入前确有事件时才把它放回。
   - 进入前无事件（`flow == None`）：不恢复，`vm.flow` 保留本次新生成的事件，让它继续向上漂。✅
   - 进入前有事件（`flow == Some`）：恢复原事件。由于事实 2，本次循环不可能生成新事件，所以「覆盖」是无害的——本来就没有新事件。✅

换言之，这个模式让 `eval_code` 对「既有事件」**完全透明**，对「新生事件」**完全放行**。三个事实缺一不可。

#### 4.3.3 源码精读

`eval_code` 的保存-恢复两行：

> [crates/typst-eval/src/code.rs:30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L30) —— 进入时 `let flow = vm.flow.take();`，把既有事件搬走、清空 `vm.flow`。
>
> [crates/typst-eval/src/code.rs:69-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L69-L71) —— 离开时 `if flow.is_some() { vm.flow = flow; }`，按需恢复。

中途检测到事件就 `break`，并视情况警告「return 无条件丢弃了前面的内容」：

> [crates/typst-eval/src/code.rs:63-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L63-L66) —— 一旦 `vm.flow` 有值，调用 `warn_for_discarded_content` 后立即跳出循环。

「不覆盖」护栏在三个生成方节点里完全一致：

> [crates/typst-eval/src/flow.rs:191-200](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L191-L200) —— `LoopBreak::eval`：`if vm.flow.is_none() { vm.flow = Some(FlowEvent::Break(...)) }`。
>
> [crates/typst-eval/src/flow.rs:213-223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L213-L223) —— `FuncReturn::eval`：同样的 `is_none()` 护栏，并附带可选的返回值。

`warn_for_discarded_content` 只对「无条件的 `return <有值>`」触发，提示用户前面的内容被丢了：

> [crates/typst-eval/src/code.rs:413-434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L413-L434) —— 只匹配 `FlowEvent::Return(_, Some(_), false)`（有显式返回值且非条件返回），警告「this return unconditionally discards the content before it」，并提示「试着省略 `return` 让所有值自动 join」。

> 旁证：同样的保存-恢复三件套（`take` → 循环 → `if flow.is_some() { vm.flow = flow }`）在 `WhileLoop::eval`、`ForLoop::eval` 里**原样出现**（见 [flow.rs:68 与 101-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L63-L112)、[flow.rs:119 与 178-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L114-L189)）。区别只在于：循环**额外消费** `break`/`continue`（把它们清成 `None`），而 `eval_code` 一个都不消费。这是 typst-eval 里处理嵌套控制流的统一范式。

如果事件一路漂到模块顶层都没人消费，`eval()`/`eval_string()` 会用 `forbidden()` 兜底报错：

> [crates/typst-eval/src/lib.rs:88-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L88-L91) —— 顶层 `if let Some(flow) = vm.flow { bail!(flow.forbidden()); }`，把残留事件翻译成「cannot return/break/continue outside of ...」。

#### 4.3.4 代码实践（对应总体实践任务）

**实践目标**：画出在代码块中遇到控制流事件时的执行流程，并解释保存-恢复为何对嵌套块正确。

**操作步骤**（源码阅读型 + 行为预测）：

1. 阅读下面这段函数，**先不运行**，预测输出与是否有警告：

   ```typ
   #let f() = {
     let a = [前缀]
     return a             // 在代码块里直接 return
     [后缀]               // 不可达
   }
   #f()
   ```

2. 对照 `eval_code` 走一遍：进入 `eval_code`（`flow = None`）→ 求 `let a = ...`（无事件）→ 求 `return a`：`FuncReturn::eval` 因 `vm.flow.is_none()` 成立，写入 `Return(span, Some(a 的值), false)` → 第 63 行检测到 `vm.flow`，调用 `warn_for_discarded_content`（但此时 `output` 还只是 `let` 产生的 `None`，不是 Content，所以**不警告**）→ `break` → 离开时 `flow.is_some()` 为 false，不恢复，`vm.flow` 保持 `Return`。

3. 现在**把 `let a = [前缀]` 改成 `[前缀]`**（去掉 `let a =`，让前缀变成被丢弃的内容），再预测：此时 `output` 在 return 前已经是 `Content("前缀")`，`warn_for_discarded_content` 命中，会发出警告「this return unconditionally discards the content before it」。

4. 本地用 `typst compile` 验证第 3 步是否确实出现该警告。

**保存-恢复与嵌套的解释**（回答实践任务的核心问题）：

- 假设有嵌套块 `{ { return 1 } }`。外层 `eval_code` 进入时 `flow=None`；它求值内层 `CodeBlock`，内层又开一个 `eval_code`，也是 `flow=None`；内层 `return 1` 写入 `vm.flow=Return`；内层 `eval_code` 结束，因 `flow` 是 `None` 不恢复，事件留在 `vm.flow` 里随返回值上浮；外层 `eval_code` 的第 63 行检测到事件，break，同样不恢复，事件继续上浮到 `eval_closure` 被消费。**事件穿透了任意层嵌套而不丢失**。

- 反过来，若外层进入时**已经**有一个事件（`flow=Some`），内层表达式因「不覆盖」护栏（`is_none()` 检查）**绝不会**写出新事件，于是离开时恢复原事件是安全的、无损的。**这就是保存-恢复模式正确处理嵌套块的根本原因：它把「既有事件」和「新生事件」彻底解耦——既有事件被忠实恢复，新生事件被忠实放行，二者永远不会冲突。**

**预期结果**：第 1 步输出 `前缀`、无警告；第 3 步输出 `前缀` 且编译器给出「unconditionally discards the content」警告。命令运行为「待本地验证」，但流程由源码确定。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `eval_code` 开头的 `let flow = vm.flow.take();` 删掉、结尾的恢复也删掉（即完全不碰 `vm.flow`），会发生什么问题？

> **答案**：表面上看，事件似乎仍能上浮。但会出问题：进入时若 `vm.flow` 已有事件，第 63 行的 `if let Some(event) = &vm.flow` 会**立刻**在第一条表达式求值后就触发 break，导致本块的剩余表达式被跳过、且本块的 `join` 结果不完整；同时事件归属会混乱（分不清是本块产生的还是外层既有的）。`take()` 的作用就是先把既有事件「挪开」，让本块的检测只针对新生事件。

**练习 2**：为什么 `LoopBreak::eval` 里要写 `if vm.flow.is_none()`，而不是直接 `vm.flow = Some(Break(...))`？

> **答案**：为了不覆盖一个已经在传递中的（更外层或更早的）事件。例如 `return` 之后又有不可达的 `break`，或者嵌套结构中外层已触发事件时，内层的 `break` 不应篡改它。这个护栏正是 4.3.2 中「事实 2」，是保存-恢复模式能够成立的支柱之一。

**练习 3**（思考）：`CodeBlock::eval` 里 `self.body().eval(vm)?` 用了 `?`，若 body 求值报错，`scopes.exit()` 不会执行。这是 bug 吗？

> **答案**：在 Typst 的实际使用中**不构成问题**。求值出错（返回 `Err`）后，整个模块的求值会被上层（`eval()`）判定为失败、该 `Vm` 与其 `scopes` 会被整体丢弃，不再使用，因此作用域栈是否平衡已无意义。这是「求值要么整体成功、要么整体作废」语义下的合理简化。若要让 `exit` 必定执行，需要改用 `defer`/`Drop` 守卫，但当前实现选择了不引入额外复杂度。

---

### 4.4 set / show 规则：作为「样式作用域」应用到 tail

#### 4.4.1 概念说明

在表达式上下文（`Expr::eval`）里，`set` / `show` 是被**禁止**的——上一讲已经见过那条 `forbidden` 闭包。但在**代码块/内容块的语句流**里，它们是合法的，而且语义很特别：它们不是「求值出一个值参与 join」，而是**作用在它之后的所有内容（tail）上**，像一层「样式作用域」。

例如：

```typ
#[
  #set text(red)
  这行是红色的，   #text(blue)[这行是蓝色]   #set text(green) 这行是绿色
]
```

这里的 `#set text(red)` 并不产生值，而是给「它后面的 tail」包上一层「文字红色」的样式；遇到下一条 `#set text(green)` 又会以那条为界，重新划分 tail。

#### 4.4.2 核心流程

`eval_code` 遇到 `set`/`show` 时的处理（以 `set` 为例，`show` 几乎对称）：

```text
match expr {
  SetRule(set):
      styles = set.eval(vm)               # 求值 set，得到 Styles
      if vm.flow 有事件: break             # set 本身触发了控制流则停
      tail = eval_code(vm, exprs)          # 递归：吃掉「之后所有表达式」= tail
      value = tail.display()               # tail 是 Value，转成 Content
              .styled_with_map(styles)     # 用样式包裹 tail
  ShowRule(show):
      recipe = show.eval(vm)              # 求值 show，得到 Recipe
      if vm.flow 有事件: break
      tail = eval_code(vm, exprs)
      value = tail.display().styled_with_recipe(engine, context, recipe)
  _:
      value = expr.eval(vm)               # 普通表达式，照常求值
}
output = ops::join(output, value)          # 把「带样式的 tail」拼进总输出
```

要点：

- `set`/`show` 的求值结果（`Styles` / `Recipe`）**不直接进 join**，而是先求出 tail、把 tail 转成 `Content`（`.display()`），再用样式/配方包裹，最后把包裹后的内容 join 进去。
- 「tail」之所以是「之后所有表达式」，正是因为这里把**同一个迭代器** `exprs` 递归喂给了内层 `eval_code`——内层消费完剩下的元素后，外层迭代器自然就到了尾部，循环结束。
- 嵌套的 `set`/`show` 会被递归自然处理：内层 `eval_code` 又会遇到下一条 `set`/`show`，再次切分 tail，形成层层包裹的样式洋葱。

#### 4.4.3 源码精读

`eval_code` 里 `set`/`show` 两个特殊分支：

> [crates/typst-eval/src/code.rs:36-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L36-L44) —— `SetRule` 分支：求 `styles` → 检查 flow → 递归 `eval_code(vm, exprs)` 求 tail → `tail.display().styled_with_map(styles)`。
>
> [crates/typst-eval/src/code.rs:45-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L45-L57) —— `ShowRule` 分支：求 `recipe` → 检查 flow → 递归求 tail → `tail.display().styled_with_recipe(&mut vm.engine, vm.context, recipe)?`。

`SetRule::eval` 与 `ShowRule::eval` 的产出（在 `rules.rs`，本讲只点明其返回类型）：

> [crates/typst-eval/src/rules.rs:11-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L11-L35) —— `SetRule::eval` 的 `type Output = Styles`：处理可选条件、校验目标是元素函数、调用 `target.set(..)` 生成 `Styles`。（set 规则的细节是 u5-l2 的主题，这里只需知道它产出 `Styles`。）
>
> [crates/typst-eval/src/rules.rs:37-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L37-L64) —— `ShowRule::eval` 的 `type Output = Recipe`：把 selector 与 transform（含 `set` 子规则）组装成 `Recipe`。

把 tail 的 `Value` 变成可被样式包裹的 `Content`，靠的是 `Value::display`：

> [crates/typst-library/src/foundations/value.rs:192-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L192-L209) —— `Value::display`：`None` 变空内容、`Content` 原样返回、字符串/数字等转成文本元素、其余用代码块形式展示。

最后是「包裹样式」的两条 Content 方法：

> [crates/typst-library/src/foundations/content/mod.rs:374-386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L374-L386) —— `styled_with_map`：若样式为空直接返回；否则若自身已是 `StyledElem` 就把样式并入，否则包成一个新的 `StyledElem`。这就是「样式洋葱」逐层包裹的物理实现。
>
> [crates/typst-library/src/foundations/content/mod.rs:336-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L336-L348) —— `styled_with_recipe`：无 selector 的 recipe 立即应用，有 selector 的则包成 `styled(recipe)` 延迟应用。

> 衔接 `Expr::eval` 的 `forbidden`：在纯表达式上下文（不是语句流）里写 `set`/`show` 之所以报错，正是因为没有「后续 tail」可作用——见 [code.rs:81-83 与 136-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L76-L156)：`forbidden` 闭包给出「set/show is only allowed directly in code and content blocks」。

#### 4.4.4 代码实践

**实践目标**：对照 `eval_code`，画出遇到一条 `set` 时的执行流程，验证「样式只作用在 tail 上」。

**操作步骤**：

1. 准备内容块，故意让 `set` 夹在中间：

   ```typ
   #[
     #set text(red)
     红
     #text(blue)[蓝]
     #set text(green)
     绿
   ]
   ```

2. **画出执行流程**（对照 `eval_code` 的 `SetRule` 分支）：
   - 内容块 `[ ... ]` → `ContentBlock::eval` →（markup 流式求值，最终）其中 `#{...}` 与 `#set` 进入 `eval_code` 处理。
   - 第一条 `set text(red)`：求出 `styles_red` → 检查 flow（无）→ **递归** `eval_code` 求剩余 tail `[红 #text(blue)[蓝] #set text(green) 绿]`。
     - 在这层递归里又会遇到第二条 `set text(green)`：求出 `styles_green` → 再次递归求 tail `[绿]` → `[绿].display().styled_with_map(styles_green)` =「绿色 的『绿』」。
     - 这层递归返回后，把「绿」join 到前面的「红」「蓝」之后，整体再 `.styled_with_map(styles_red)` 包一层红色。
   - 最终：`红`、`蓝`（被 `text(blue)` 局部覆盖为蓝）、`绿`（被 green 包裹）。

3. 本地 `typst compile` 查看：「红」是红色、「蓝」是蓝色（局部 `text` 优先）、「绿」是绿色。

**回答实践任务的另一半——保存-恢复与 set 的配合**：注意 `SetRule` 分支在递归 `eval_code(vm, exprs)` **之前**有一句 `if vm.flow.is_some() { break; }`（[code.rs:38-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L36-L44)）。也就是说，**只有当 set 本身没有触发控制流时，才会去求 tail**。而内层 `eval_code` 沿用自己的保存-恢复（4.3）：若 tail 里发生了 `return`，内层会让该事件正确上浮；外层 `eval_code` 的第 63 行随即检测到事件、break、放行事件。因此「在 set 之后 return」也能正确把 return 事件传出，同时（通过 `warn_for_discarded_content`）提醒用户 set 的样式可能白打了。

**预期结果**：三段文字颜色分别为红、蓝、绿；说明 set 的样式确实只作用于各自之后的 tail。命令运行为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `set` 在代码块里合法，但在表达式位置（如 `#(set text(red))`）却报错？

> **答案**：`set` 的语义是「作用于后续 tail」，必须有「后续语句流」作为对象。`Expr::eval` 的总分发器里，`Self::SetRule(_) => bail!(forbidden("set"))`（[code.rs:136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L136-L137)）——表达式没有 tail 概念，所以禁止。只有 `eval_code` 这种**流式**处理语句的地方，才能递归地取出 tail 并应用样式。

**练习 2**：`styled_with_map` 在样式为空时直接 `return self`。结合 `SetRule::eval` 里「条件为假时返回 `Styles::new()`（空）」，这能带来什么行为？

> **答案**：当 `set` 的条件不满足时（如 `set text(..) if false`），`SetRule::eval` 返回空 `Styles`；`eval_code` 仍会递归求 tail 并调用 `styled_with_map(空)`，由于空样式直接返回原内容，**tail 不会被多余地包一层**。这保证了条件 set 失效时输出与「没有这条 set」完全一致，没有副作用。

**练习 3**：`eval_code` 遇到 `set` 后，把同一个 `exprs` 迭代器递归传入。如果 tail 里**没有**再出现 `set`/`show`，递归调用结束时外层循环还会再转一圈吗？

> **答案**：不会。内层 `eval_code` 会把迭代器「吃到底」（直到 `exprs.next()` 返回 `None`），所以回到外层 `while let Some(expr) = exprs.next()` 时也立即得到 `None`，外层循环结束。这正是用可变迭代器实现 tail 的妙处——一次遍历、无需索引。

## 5. 综合实践

把本讲的四个要点（作用域进出、流式 join、控制流保存-恢复、set/show 的 tail 语义）串起来，做一次「全链路手工求值」。

**任务**：阅读下面的 Typst 代码，**不运行**，逐步推演 `result` 的值、各文字的颜色，以及是否有编译警告；然后再本地运行核对。

```typ
#let make() = {
  #set text(weight: "bold")      // (A) 一条 set
  let parts = {
    [甲]
    return [乙]                  // (B) 在 set 的 tail 里 return
    [丙]                         // (C) 不可达
  }
  parts
}
#let result = make()
#result
```

**推演要点**（请先自己写，再对照）：

1. `make()` 的函数体是一个内容块/代码块混合。进入函数体时 `vm.flow = None`、开新作用域。
2. 遇到 `(A) set text(weight: "bold")`：`eval_code` 的 `SetRule` 分支求出 `styles_bold`，flow 无事件，递归求 tail（即 `let parts = { ... }; parts`）。
3. tail 里求 `let parts = { ... }`：内层代码块 `[甲] return [乙] [丙]` 进入新的 `eval_code`：
   - `[甲]` join 进 `output`（成为 `Content("甲")`）。
   - `return [乙]` 写入 `vm.flow = Return(span, Some("乙"), false)`，第 63 行检测到，`warn_for_discarded_content` 命中（`output` 是 Content「甲」、return 有值、非条件）→ **发出警告「this return unconditionally discards the content before it」**，break。`[丙]` 永不执行。
   - 离开内层 `eval_code`：进入时 flow 为 None，不恢复，事件随返回上浮。`parts` 绑定为内层的 `output`（即「甲」，因为 return 的值走的是 flow 通道、不参与 join，`parts` 拿到的是 join 累加的「甲」）。
4. 回到 tail 递归，继续 `parts`（值「甲」），tail 整体 `.display().styled_with_map(styles_bold)` → 「**粗体的甲**」。
5. 但 `vm.flow` 此刻仍是 `Return("乙")`（来自第 3 步，未被消费）。外层 `eval_code` 第 63 行检测到，break。函数体求值结束，`eval_closure` 消费 `Return`，**函数 `make()` 的返回值是「乙」**（return 的显式值优先于 body 的 join 结果）。
6. 于是 `result = "乙"`，且因第 3 步的 return 发生在 `set` 的 tail 内，那条「discards content」警告会触发；同时「甲」连同它的 bold 样式都被丢弃。

**预测结论**：`result` 显示为「乙」（默认粗细，**不是** bold，因为 bold 样式只作用在被丢弃的 tail「甲」上，而 return 的「乙」不经过那条 `styled_with_map`）；编译器有一条「unconditionally discards the content before it」警告。

**本地验证**：`typst compile` 后核对 (a) 输出文字是「乙」、(b) 它不是粗体、(c) 终端/警告区有 discard 警告。若任一不符，回到 4.3、4.4 对照源码复查。运行为「待本地验证」。

> 这个练习把「作用域进出（函数体/内层块各开作用域）」「流式 join（甲被拼进 output）」「保存-恢复（return 事件穿透 set 的 tail 递归上浮到 eval_closure）」「set 的 tail 语义（bold 只包住被丢弃的甲）」四件事全部用上了。

## 6. 本讲小结

- **代码块/内容块 = enter/exit 三明治**：`CodeBlock::eval` 与 `ContentBlock::eval` 都是 `scopes.enter()` → 求 body → `scopes.exit()`，区别只在 body 类型（`Code`→`Value`，`Markup`→`Content`）。括号 `Parenthesized` **不开**作用域。
- **作用域的物理实现是栈**：`Scopes` 用 `top` + `scopes: Vec<Scope>` + `base`（标准库）；`enter` 用 `mem::take` 压栈、`exit` 弹栈，查找沿栈自顶向下穿透。
- **`eval_code` 流式求值**：吃一个 `&mut Iterator`，逐条 `eval` 并用 `ops::join` 累加成单个 `Value`；`None` 是 join 的单位元，所以初值用 `Value::None`。
- **控制流靠「保存-恢复 + 不覆盖护栏」**：进入时 `vm.flow.take()`、离开时 `if flow.is_some() { vm.flow = flow }`；配合 `break`/`continue`/`return` 写入前的 `is_none()` 护栏，让 `eval_code` 对既有事件透明、对新生事件放行，正确支持任意嵌套。漂到顶层未被消费的事件由 `eval()` 的 `forbidden()` 兜底。
- **`set`/`show` 是「样式作用域」**：在 `eval_code` 里，它们求出 `Styles`/`Recipe` 后，递归求「之后所有表达式」（tail），用 `.display().styled_with_map/styled_with_recipe` 把 tail 包成带样式的 `Content` 再 join；同一迭代器的递归消费让 tail 天然等于「剩余语句」。

## 7. 下一步学习建议

- **u2-l4（Markup 模式求值）**：本讲的 `ContentBlock` 调用了 `Markup::eval`，下一讲正式拆解 markup 流如何把文本/粗体/标题等标记拼接成 `Content`，并讲解 `Label` 如何「回溯」附加到内容上。
- **u3-l1 / u3-l2（条件、循环与控制流事件）**：本讲提到循环会**消费** `break`/`continue`、并复用同样的保存-恢复三件套；那两讲会展开 `WhileLoop`/`ForLoop` 与 `FlowEvent` 的完整消费逻辑。
- **u5-l2（set 与 show 规则求值）**：本讲只用了 `SetRule::eval`/`ShowRule::eval` 的返回类型（`Styles`/`Recipe`），那篇进阶讲义会深入「条件守卫、元素函数校验、selector/transform 组装、`check_show_page_rule` 等兼容性警告」。
- **延伸阅读源码**：可对照 [flow.rs 的 `WhileLoop`/`ForLoop`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs) 体会「保存-恢复 + 消费」的统一范式，以及 [call.rs 的 `eval_closure`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs) 如何在函数返回时消费 `FlowEvent::Return`。
