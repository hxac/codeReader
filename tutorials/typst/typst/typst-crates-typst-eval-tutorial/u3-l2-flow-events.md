# 控制流事件：break / continue / return

## 1. 本讲目标

`break`、`continue`、`return` 这三个关键字在大多数语言里是「控制流原语」，但在 typst-eval 的解释器里，它们被实现成一种**带外信号（out-of-band signal）**：求值时把意图写进虚拟机的一个字段 `vm.flow`，再由外层的循环或函数去「读取并处理」。本讲专门拆解这套机制的「产生—传递—消费」三段式。学完后你应当能够：

1. 说清 `FlowEvent` 枚举的三个变体（`Break` / `Continue` / `Return`）各自的语义，特别是 `Return` 的第三个布尔字段 `conditional`（「条件 return」）存在的意义。
2. 看懂 `LoopBreak::eval`、`LoopContinue::eval`、`FuncReturn::eval` 这三个「事件生产者」是如何写入 `vm.flow` 的，以及它们都用 `vm.flow.is_none()` 做的「不覆盖」护栏。
3. 理解「保存-恢复」模式（`vm.flow.take()` → 求值 → 按需恢复）如何让控制流事件**不会跨作用域泄漏**，以及循环和函数各自如何消费三种事件。
4. 知道「漂到非法位置」的事件（如模块顶层裸 `return`）如何被 `FlowEvent::forbidden()` 兜底报错，以及 `warn_for_discarded_content` 为何只对「无条件 return」告警。

本讲承接 u3-l1（条件、循环与迭代求值）。u3-l1 讲了 `if`/`while`/`for` 的整体求值与死循环检测，并在结尾提到「`break`/`continue`/`return` 这三个事件的 `Eval` 实现留给下一讲」——本讲就是那一讲。关于「保存-恢复」模式的基本形态，u2-l3（代码块与作用域）也已铺垫过，本讲会把它讲透并串到函数调用上。

## 2. 前置知识

- **`Eval` trait 与 `Output`**：每个 AST 节点实现 `fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>`。`break`/`continue`/`return` 三者的 `Output` 都是 `Value`——它们在 Typst 里是**表达式**，求值结果是 `Value::None`（它们「产出的值」并不重要，重要的是它们写进 `vm.flow` 的那个信号）。
- **`Vm` 与 `vm.flow`**：虚拟机 `Vm` 持有求值状态，其中 `flow: Option<FlowEvent>` 字段记录「当前是否发生了控制流事件」。三种事件**不是**用 Rust 的 `return` 或异常实现的，而是通过修改这个共享字段来传递。
- **`ops::join`**：循环里把每一轮产出累加成一个值的运算，`Value::None` 是它的单位元（u3-l1 讲过）。
- **`SourceDiagnostic` 与 `error!`/`bail!`**：typst-library 的诊断类型与构造宏，用于把「错误信息 + span 定位」打包成带位置的源诊断（u1-l4、u2-l1 讲过）。

> 关键直觉：把 `break`/`continue`/`return` 想成「往 `vm.flow` 信箱里投了一张便条」。投递方（三个 `Eval` 实现）只负责投递；收件方（循环、函数、模块顶层）负责按规则处理便条。便条一旦被某个收件方「签收」就作废，没被签收而漂到顶层的便条会被 `forbidden()` 当作错误退回。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/flow.rs` | 本讲主角。定义 `FlowEvent` 枚举与 `forbidden()` 方法，并实现 `LoopBreak`、`LoopContinue`、`FuncReturn` 的 `Eval`。`WhileLoop`/`ForLoop` 里消费事件的逻辑也在这里（u3-l1 已展开循环本身，本讲聚焦其对 flow 的处理）。 |
| `src/code.rs` | 提供 `eval_code`（流式求值表达式流），其中包含 flow 的「保存-恢复」与 `warn_for_discarded_content` 诊断。还定义了 `Expr::eval` 里的 `forbidden` 闭包（注意它和 `FlowEvent::forbidden()` 是两回事）。 |
| `src/call.rs` | 提供 `eval_closure`（执行一个闭包/函数体），在结尾消费 `Return` 事件、对漂入函数的 `Break`/`Continue` 调 `forbidden()`。 |
| `src/lib.rs` | `eval` 与 `eval_string` 两个顶层入口在最后对残留的 `vm.flow` 调 `forbidden()`，即「模块顶层遇到 return/break/continue」的兜底报错点。 |
| `src/vm.rs` | `Vm` 结构体里 `flow: Option<FlowEvent>` 字段的定义。 |

测试依据（位于本 crate 之外的 typst 仓库根，用于代码实践的源码阅读）：

- `tests/suite/scripting/loop.typ`：循环中 `break`/`continue` 的行为与非法位置报错（u3-l1 也引用过）。
- `tests/suite/scripting/function.typ`：函数体内 `return` 提前返回的行为。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先认识信号本身（`FlowEvent` 枚举），再看谁生产信号（三个 `Eval` 实现），接着看谁消费信号（循环与 `eval_code` 的「保存-恢复」），最后看函数如何消费 `return` 以及漂到非法位置的信号如何被诊断兜底。

### 4.1 FlowEvent 枚举：三种控制流信号的语义

#### 4.1.1 概念说明

当求值过程中遇到 `break`/`continue`/`return`，解释器需要一种统一的「信号载体」来表达「请中断当前求值、改走某条路径」。typst-eval 用一个枚举 `FlowEvent` 来承载这三种信号：

- `Break`：跳出**当前所在的循环**。
- `Continue`：跳过本轮循环**剩余部分**，进入下一轮。
- `Return`：跳出**当前所在的函数**，可携带一个显式返回值。

其中 `Return` 最特殊，它携带三个字段：触发位置 `Span`、可选的返回值 `Option<Value>`（`return x` 带值，裸 `return` 为 `None`），以及第三个布尔字段 `conditional`——表示「这个 return 是否是在某个有条件的分支里触发的」。这个 `conditional` 字段看似不起眼，却是本讲最后那个告警（`warn_for_discarded_content`）能否触发的关键开关。

#### 4.1.2 核心流程

`FlowEvent` 本身只是一个数据载体，它不「执行」任何控制流，真正的执行逻辑分散在循环和函数的求值代码里。它的职责是：**把信号从产生点搬运到消费点**。搬运关系如下：

```text
LoopBreak ─┐
LoopContinue─┼─→ 写入 vm.flow ─→ 循环(While/For) 读取并消费 Break/Continue
FuncReturn ─┘                 └─→ 漂过循环(被放行) ─→ 函数(eval_closure) 消费 Return

任意未被消费的事件漂到顶层 ─→ eval()/eval_string() 调 forbidden() 报错
```

注意一个设计要点：`Return` 可以**穿透**循环（循环只消费 `Break`/`Continue`，对 `Return` 选择「放行」让它继续冒泡到函数）；而 `Break`/`Continue` 如果漂到函数体却没被任何循环消费，函数会判定它们非法并报错。这种「谁该消费谁」的分工，正是后面几节要讲的内容。

#### 4.1.3 源码精读

枚举定义在 `flow.rs` 顶部：

[crates/typst-eval/src/flow.rs#L12-L22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L12-L22) —— `FlowEvent` 三变体定义，注意 `Return` 的第三个字段 `bool`（注释说明它表示「return 是否有条件」）。

```rust
#[derive(Debug, Clone, PartialEq)]
pub enum FlowEvent {
    /// Stop iteration in a loop.
    Break(Span),
    /// Skip the remainder of the current iteration in a loop.
    Continue(Span),
    /// Stop execution of a function early, optionally returning an explicit
    /// value. The final boolean indicates whether the return was conditional.
    Return(Span, Option<Value>, bool),
}
```

而 `vm.flow` 这个「信箱」本身只是 `Vm` 上的一个普通字段：

[crates/typst-eval/src/vm.rs#L19-L20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L19-L20) —— `Vm.flow: Option<FlowEvent>`，初值为 `None`（见 `Vm::new` 第 39 行）。

#### 4.1.4 代码实践

这是一个源码阅读型实践。

1. **实践目标**：确认 `Return` 事件的三元组结构，并搞清「带值」与「裸 return」如何区分。
2. **操作步骤**：在 `src/flow.rs` 第 12–22 行找到枚举；再跳到第 213–223 行的 `FuncReturn::eval`（下一节会精读），观察 `FlowEvent::Return(self.span(), value, false)` 里的 `value` 从何而来。
3. **需要观察的现象**：`value` 是 `self.body().map(|body| body.eval(vm)).transpose()?` 的结果——即 `return` 后面若有表达式则求值它，否则为 `None`。
4. **预期结果**：`return 42` 产生 `Return(span, Some(Value::Int(42)), false)`；裸 `return` 产生 `Return(span, None, false)`。两个字段（`Span`、`Option<Value>`）都已就位，第三个 `false` 表示「目前还不知道它是否有条件」。
5. 第三字段 `conditional` 的初始值恒为 `false`，它会在后续被 `Conditional`/`WhileLoop`/`ForLoop` 在事件冒泡时改写为 `true`（见 4.4 节）。

#### 4.1.5 小练习与答案

**练习 1**：`break` 和 `continue` 变体为什么只带一个 `Span`，而 `Return` 要带三个字段？

> **参考答案**：`break`/`continue` 不携带任何「数据值」，只需记录触发位置（用于报错时定位 span），所以只存 `Span`。`return` 既要携带可选的返回值（`Option<Value>`），又要携带「是否有条件」这个供诊断使用的标记（`bool`），因此字段更多。

**练习 2**：`FlowEvent::Return(span, None, false)` 表示用户写了什么代码？

> **参考答案**：表示一个**裸** `return`（`return;` 风格，不带返回值），且当前尚未被任何条件结构标记为「条件 return」。

### 4.2 事件生产者：LoopBreak / LoopContinue / FuncReturn 的 Eval

#### 4.2.1 概念说明

`break`、`continue`、`return` 在 AST 里分别对应 `ast::LoopBreak`、`ast::LoopContinue`、`ast::FuncReturn` 三类节点。它们各自的 `Eval` 实现极其简短，核心都只有一句话：**把自己的信号写进 `vm.flow`**。但三处实现都有一道共同的护栏——`if vm.flow.is_none()`，即「只有当信箱空着时才投递」。

这个「不覆盖」护栏是理解整个机制的钥匙：它保证**最先发生的那个控制流事件胜出**。比如 `return f()` 里 `f()` 本身又触发了 `return`，或者 `return (continue)` 这种嵌套写法——外层事件的求值结果不会把已有的、更内层的事件覆盖掉。

#### 4.2.2 核心流程

三个生产者的行为高度同构，可以用一个伪代码模板概括：

```text
fn eval(self, vm):
    if vm.flow.is_none():        # 护栏：信箱空才投递
        vm.flow = Some(对应变体(self.span(), ...))
    return Value::None           # 三者的 Output 都是 None
```

| 关键字 | 对应 Eval | 写入的变体 | 携带数据 |
|--------|-----------|------------|----------|
| `break` | `LoopBreak::eval` | `Break(span)` | 仅 span |
| `continue` | `LoopContinue::eval` | `Continue(span)` | 仅 span |
| `return [expr]` | `FuncReturn::eval` | `Return(span, Option<Value>, false)` | span + 可选返回值 |

#### 4.2.3 源码精读

`LoopBreak` 与 `LoopContinue` 几乎一模一样，差别只在写入的变体名：

[crates/typst-eval/src/flow.rs#L191-L211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L191-L211) —— `LoopBreak::eval` 与 `LoopContinue::eval`：都先判 `vm.flow.is_none()` 再写入对应事件，最后返回 `Value::None`。

```rust
impl Eval for ast::LoopBreak<'_> {
    type Output = Value;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        if vm.flow.is_none() {
            vm.flow = Some(FlowEvent::Break(self.span()));
        }
        Ok(Value::None)
    }
}
```

`FuncReturn` 多了一步：求值 `return` 后面可选的表达式，把它作为返回值塞进事件：

[crates/typst-eval/src/flow.rs#L213-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L213-L223) —— `FuncReturn::eval`：先用 `self.body().map(...).transpose()` 求可选返回值，再（在信箱为空时）写入 `Return(span, value, false)`。

```rust
impl Eval for ast::FuncReturn<'_> {
    type Output = Value;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let value = self.body().map(|body| body.eval(vm)).transpose()?;
        if vm.flow.is_none() {
            vm.flow = Some(FlowEvent::Return(self.span(), value, false));
        }
        Ok(Value::None)
    }
}
```

注意 `.transpose()` 的妙处：`self.body()` 返回 `Option<Expr>`，`.map(|body| body.eval(vm))` 得到 `Option<SourceResult<Value>>`，`.transpose()` 把它「翻转」成 `SourceResult<Option<Value>>`——于是「没有返回值」和「返回值求值失败」被自然区分开。

#### 4.2.4 代码实践

1. **实践目标**：理解「不覆盖」护栏在嵌套写法下的效果。
2. **操作步骤**：阅读上面三段源码，预测下面这段 Typst 代码求值后 `vm.flow` 的最终内容（先不运行）：

   ```typst
   #let f() = {
     return continue   # continue 是 FuncReturn 的「返回值表达式」
   }
   ```
3. **需要观察的现象**：`FuncReturn::eval` 先求 `body()` 即 `continue` 这个表达式。求值 `continue` 时 `LoopContinue::eval` 发现 `vm.flow.is_none()` 成立，于是写入 `Continue(span)`。随后回到 `FuncReturn::eval`，此时 `value` 已是求值 `continue` 得到的 `Value::None`，但 `vm.flow.is_none()` 现在**为假**（已被 `Continue` 占据），所以 `FuncReturn` 的 `if` 分支不执行，不会覆盖。
4. **预期结果**：`vm.flow` 最终是 `Some(Continue(...))` 而非 `Return(...)`。先到的 `continue` 胜出，这正是 `is_none()` 护栏的设计意图。
5. 本结论为基于源码的推理，**待本地验证**（可在 typst 中构造等价用例并观察 `continue` 是否真的穿透到外层循环）。

#### 4.2.5 小练习与答案

**练习 1**：为什么三个生产者都在最后 `return Ok(Value::None)`，而不是返回某种「特殊标记」表示发生了控制流？

> **参考答案**：因为控制流的「标记」已经通过 `vm.flow` 这条带外通道传递了，节点的 `eval` 返回值只负责「正常产出」。返回 `None` 让上层求值（如 `ops::join`）有一个合法的、不影响累加的单位元值可用，无需为控制流发明额外的返回类型。

**练习 2**：如果把 `FuncReturn::eval` 里的 `if vm.flow.is_none()` 护栏删掉，会产生什么问题？

> **参考答案**：`return continue` 这类写法中，`continue` 先写入 `Continue`，随后 `FuncReturn` 会无脑覆盖成 `Return(None)`，导致本应穿透循环的 `continue` 信号丢失，控制流语义被破坏。护栏保证「首个事件优先」。

### 4.3 事件消费者：vm.flow 的「保存-恢复」模式

#### 4.3.1 概念说明

信号投进 `vm.flow` 之后，谁来取走？答案是「知道自己该处理控制流的那些结构」：`while`/`for` 循环消费 `Break`/`Continue`，函数消费 `Return`。但这些结构本身可能**嵌套在另一个已经在处理 flow 的结构里**。比如一个函数体里有个循环、循环体里又有个 `if`——如何保证内层循环消费的 `break` 不会污染外层（比如外层函数）的 flow 状态？

typst-eval 的解法是一个统一的「保存-恢复」三段式：

1. **进入**结构时，先 `let flow = vm.flow.take()`——把信箱**清空**并记住原来的内容。
2. **求值**过程中，若 `vm.flow` 被填入新事件，就按规则消费它。
3. **离开**时，若 `flow.is_some()`（进入前本来就有事件），把它**恢复**回 `vm.flow`。

关键在第 1 步的 `take()`：它让内层结构面对的始终是一个「干净的信箱」。内层产生的事件要么被内层消费（清空 `vm.flow`），要么「放行」（保留在 `vm.flow` 里随 `take()` 之外的逻辑冒泡）。第 3 步只恢复**进入前**就存在的外层事件，从而彻底避免内层事件跨作用域泄漏到外层。

这套模式在 `eval_code`（流式求值代码块/表达式流）、`WhileLoop::eval`、`ForLoop::eval` 中完全一致——三处代码长得几乎一样。u2-l3 已经讲过 `eval_code` 里的版本，本讲把它和循环版本对照，突出「谁消费谁」。

#### 4.3.2 核心流程

以 `WhileLoop` 为例，单轮迭代里对 `vm.flow` 的处理：

```text
进入循环：flow = vm.flow.take()      # 清空信箱，记下旧值
每轮 body 求值后：
    match vm.flow {
        Some(Break(_))    => { vm.flow = None; break; }   # 消费，跳出整个循环
        Some(Continue(_)) => { vm.flow = None; }          # 消费，进入下一轮
        Some(Return(..))  => break;                       # 不消费！放行，跳出循环让函数处理
        None              => {}                           # 无事件，正常下一轮
    }
离开循环：if flow.is_some() { vm.flow = flow }          # 只恢复外层旧事件
```

`ForLoop` 的处理与 `while` 完全相同（只是包在 `iter!` 宏里）。`eval_code` 略有不同：它对**任意**事件都选择 `break`（停止求值后续表达式），但不会「消费」事件——也就是说，代码块里产生的 `return` 会原样留在 `vm.flow` 里，随代码块返回后继续向上冒泡。

#### 4.3.3 源码精读

`WhileLoop::eval` 对 flow 的处理（循环主体在 u3-l1 已讲，这里只看 flow 部分）：

[crates/typst-eval/src/flow.rs#L88-L103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L88-L103) —— 单轮迭代后按事件类型分流：`Break` 清空并 `break`，`Continue` 仅清空，`Return` 不清空直接 `break`（放行给函数）；循环结束后若进入前有外层事件则恢复。

```rust
match vm.flow {
    Some(FlowEvent::Break(_)) => {
        vm.flow = None;
        break;
    }
    Some(FlowEvent::Continue(_)) => vm.flow = None,
    Some(FlowEvent::Return(..)) => break,
    None => {}
}
// ... 循环结束后
if flow.is_some() {
    vm.flow = flow;
}
```

注意 `Some(FlowEvent::Return(..)) => break` 这一行的细节：它**没有** `vm.flow = None`，所以 `Return` 事件仍保留在 `vm.flow` 中。循环只是用 `break` 退出自身，把事件原封不动留给外层函数去处理。这正是「`Return` 穿透循环」的实现。

保存发生在循环最开头（第 68 行 `let flow = vm.flow.take();`），恢复发生在结尾（第 101–103 行）。`ForLoop` 的对应代码在 [crates/typst-eval/src/flow.rs#L119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L119)（take）与 [crates/typst-eval/src/flow.rs#L178-L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L178-L180)（恢复），宏内的消费逻辑在 [crates/typst-eval/src/flow.rs#L133-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L133-L141)，与 `while` 逐字相同。

`eval_code` 的版本略有不同——它不区分事件类型，只要有事件就停：

[crates/typst-eval/src/code.rs#L30-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L30-L31) 与 [crates/typst-eval/src/code.rs#L63-L71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L63-L71) —— `eval_code` 开头 `take()` 清空，遇到任意事件就调用 `warn_for_discarded_content` 后 `break`（不清空，放行冒泡），结尾按需恢复外层事件。

```rust
let flow = vm.flow.take();       // 第 30 行：保存并清空
let mut output = Value::None;
while let Some(expr) = exprs.next() {
    // ... 求值 expr ...
    if let Some(event) = &vm.flow {
        warn_for_discarded_content(&mut vm.engine, event, &output);
        break;                   // 遇到任意事件即停止求值后续表达式
    }
}
if flow.is_some() {
    vm.flow = flow;              // 恢复外层事件
}
```

对比循环与 `eval_code`，差别只有一处：循环会**消费** `Break`/`Continue`（清空 `vm.flow`），而 `eval_code` 对所有事件都只「放行不消费」。这符合语义——代码块本身没有「循环」身份，无权消费 `break`/`continue`，它只能停止自己后续表达式的求值，把事件留给真正有权处理的循环或函数。

#### 4.3.4 代码实践

1. **实践目标**：验证「`Return` 穿透循环、`Break`/`Continue` 被循环消费」的分工。
2. **操作步骤**：对照本节源码，逐行追踪下面 Typst 代码里 `vm.flow` 的变化轨迹（纸笔推演）：

   ```typst
   #let f() => {
     for i in (1, 2, 3) {
       if i == 2 { return "命中" }
     }
     "未命中"
   }
   #f()
   ```
3. **需要观察的现象**：
   - 进入 `for`：`flow = vm.flow.take()`（假设外层为 `None`，记下 `None`）。
   - `i == 1`：`if` 条件假，无事件。
   - `i == 2`：`if` 条件真，求值 `return "命中"` → `FuncReturn::eval` 写入 `Return(span, Some("命中"), false)`。
   - `iter!` 宏内的 `match vm.flow`：命中 `Some(Return(..)) => break`——**不清空**，退出循环。
   - 循环结尾 `if flow.is_some()`：`flow` 是 `None`，不恢复。
   - `vm.flow` 仍持有 `Return(...)`，随 `for` 表达式的值返回，冒泡到 `eval_closure`（见 4.4 节）。
4. **预期结果**：`f()` 返回 `"命中"`（由 `eval_closure` 从 `Return` 的 `Some(explicit)` 取出），循环后的 `"未命中"` 永不执行。`break`/`continue` 若出现在循环内则会被循环消费（清空），不会冒泡到函数。
5. 若把 `return "命中"` 改成 `break`：`iter!` 命中 `Some(Break(_))` 分支，`vm.flow = None` 后 `break`，事件被消费，循环正常结束，`f()` 返回 `"未命中"`。此结论**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`eval_code` 和 `WhileLoop`/`ForLoop` 都用了「保存-恢复」三段式，但它们对 `vm.flow` 的消费策略不同。请说出关键区别。

> **参考答案**：循环会**消费** `Break`/`Continue`（把 `vm.flow` 清空成 `None`），只放行 `Return`；而 `eval_code` 对**任何**事件都只是 `break` 停止后续求值，**不消费**（不清空），让事件原样冒泡。原因是代码块没有「循环」身份，无权消化 `break`/`continue`。

**练习 2**：为什么循环结尾的恢复写成 `if flow.is_some() { vm.flow = flow }`，而不是无条件 `vm.flow = flow`？

> **参考答案**：因为循环内可能产生了**新的** `Return` 事件（已放行保留在 `vm.flow` 里，等待外层函数处理）。若无条件恢复，会用外层旧事件覆盖掉这个新事件。只有当「进入前本来就有外层事件」（`flow.is_some()`）时才需要把它恢复回去；进入前为空时则保留循环内的新事件不动。

### 4.4 函数消费 return 与诊断：eval_closure、forbidden()、warn_for_discarded_content

#### 4.4.1 概念说明

`Return` 事件穿透循环和代码块后，最终抵达**函数**——也就是 `eval_closure`（执行一个闭包/函数体）。函数是 `Return` 的「终点消费者」：它会从 `vm.flow` 里取出 `Return`，把其中的显式返回值作为函数的返回值。而如果漂到函数体的是 `Break`/`Continue`（说明用户在函数里写了 `break` 却不在任何循环内），或者 `Return` 漂到了**模块顶层**（用户在模块顶层写了裸 `return`），这些事件就无人消费了——此时 `FlowEvent::forbidden()` 登场，把「无人签收的便条」转成一条精确的错误诊断。

本节还要解开一个贯穿全讲的悬念：`Return` 第三字段 `conditional` 到底给谁用？答案是给 `warn_for_discarded_content` 这个告警用的。当函数体里**无条件地** `return <有值>`，而 return 之前还有「会产生内容」的表达式（比如直接写了文本 `[前缀]`），那些内容会被丢弃——解释器会发一条警告提醒用户「前面的内容被丢了」。但如果是**条件 return**（在 `if`/`while`/`for` 分支里 return），就不告警，因为那种情况下「内容是否被丢」取决于运行时分支，强行告警会产生误报。

#### 4.4.2 核心流程

函数消费 flow 的决策（在 `eval_closure` 求完函数体后）：

```text
output = body.eval(vm)
match vm.flow {
    Some(Return(_, Some(显式值), _)) => 函数返回「显式值」       # return expr
    Some(Return(_, None, _))        => 函数返回「body 的 output」# 裸 return
    Some(其他事件)                  => 报错 flow.forbidden()     # 函数里出现裸 break/continue
    None                            => 函数返回「body 的 output」 # 没有 return
}
```

`conditional` 字段的流转则是一条副线：`FuncReturn::eval` 出生时是 `false`；当 `Return` 事件冒泡经过 `Conditional`/`WhileLoop`/`ForLoop` 时，这三者的 `eval` 结尾都会把它改写成 `true`。最终 `warn_for_discarded_content` 用模式 `Return(span, Some(_), false)` 做匹配——**只有第三个字段是 `false`（无条件）才告警**，`true`（条件 return）直接跳过。

#### 4.4.3 源码精读

`eval_closure` 在求完函数体后消费 flow：

[crates/typst-eval/src/call.rs#L733-L742](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L733-L742) —— 函数体求值后，按 `vm.flow` 决定返回值：`Return` 带 `Some` 取显式值、带 `None` 或无事件取 body 产出；漂入函数的 `Break`/`Continue` 调 `forbidden()` 报错。

```rust
// Handle control flow.
let output = body.eval(&mut vm)?;
match vm.flow {
    Some(FlowEvent::Return(_, Some(explicit), _)) => return Ok(explicit),
    Some(FlowEvent::Return(_, None, _)) => {}
    Some(flow) => bail!(flow.forbidden()),
    None => {}
}
Ok(output)
```

注意三个分支对 `conditional` 字段（第三个 `_`）**都不关心**——函数取值逻辑与「是否有条件」无关。`conditional` 的唯一读者是下面的告警函数。

`FlowEvent::forbidden()` 把三种「无人消费」的事件各自转成对应错误：

[crates/typst-eval/src/flow.rs#L24-L39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L24-L39) —— `forbidden()` 对 `Break`/`Continue`/`Return` 分别返回「cannot break/continue outside of loop」「cannot return outside of function」三条错误。

```rust
pub fn forbidden(&self) -> SourceDiagnostic {
    match *self {
        Self::Break(span) => error!(span, "cannot break outside of loop"),
        Self::Continue(span) => error!(span, "cannot continue outside of loop"),
        Self::Return(span, _, _) => error!(span, "cannot return outside of function"),
    }
}
```

这个方法在三个地方被调用：函数内漂入的 `Break`/`Continue`（上面 `eval_closure` 第 738 行）、模块顶层 `eval()`、以及 `eval_string()`。后两者是「模块/求值顶层」的兜底：

[crates/typst-eval/src/lib.rs#L88-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L88-L91) —— `eval()` 求值完整个模块后，若 `vm.flow` 仍有残留事件（如模块顶层裸 `return`），调 `flow.forbidden()` 报错。

```rust
// Handle control flow.
if let Some(flow) = vm.flow {
    bail!(flow.forbidden());
}
```

`eval_string()` 中有完全相同的兜底（[crates/typst-eval/src/lib.rs#L169-L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L169-L172)）。这就是「在模块顶层写 `return`」报 `cannot return outside of function` 的完整链路。

最后是 `conditional` 字段的写入点。`Conditional`/`WhileLoop`/`ForLoop` 三者结尾都有同一段代码，把冒泡经过的 `Return` 标记为「条件 return」：

[crates/typst-eval/src/flow.rs#L54-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L54-L57) —— `Conditional::eval` 结尾：若 `vm.flow` 里是 `Return`，把第三字段置 `true`（`WhileLoop` 第 105–108 行、`ForLoop` 第 182–185 行同理）。

```rust
// Mark the return as conditional.
if let Some(FlowEvent::Return(_, _, conditional)) = &mut vm.flow {
    *conditional = true;
}
```

而 `warn_for_discarded_content` 正是用第三个字段做过滤的「唯一读者」：

[crates/typst-eval/src/code.rs#L413-L434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L413-L434) —— 只有 `Return(span, Some(_), false)`（无条件、带值）且前面累加了 `Content` 时才告警「this return unconditionally discards the content before it」。

```rust
fn warn_for_discarded_content(engine: &mut Engine, event: &FlowEvent, joined: &Value) {
    let FlowEvent::Return(span, Some(_), false) = event else { return };
    let Value::Content(tree) = &joined else { return };
    // ... 构造 warning，并检测是否含 state/counter 更新以追加额外 hint
    engine.sink.warn(warning);
}
```

第一行的模式匹配 `Return(span, Some(_), false)` 是点睛之笔：第三个字段必须是 `false`（无条件）才继续，`true`（条件 return）直接 `return` 跳过——这就把「条件 return 不告警」落实了。它由 `eval_code` 在 [crates/typst-eval/src/code.rs#L63-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L63-L66) 检测到事件时调用。

> 旁注：`code.rs` 的 `Expr::eval` 里还有一个**同名的** `forbidden` 闭包（[crates/typst-eval/src/code.rs#L81-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L81-L83)，在第 136–137 行用于禁止 `set`/`show` 出现在表达式位置）。它和本节的 `FlowEvent::forbidden()` 是**两回事**：前者管「语句级关键字不能用在表达式里」，后者管「控制流事件漂到了无人消费的位置」。别被同名混淆。

#### 4.4.4 代码实践

这是本讲的主实践，串联三个消费点。

1. **实践目标**：把「循环消费 `break`/`continue`、函数消费 `return`、顶层 `forbidden()` 兜底」这条链路走通，并理解 `conditional` 字段如何抑制误报告警。
2. **操作步骤**：
   - 打开 `src/call.rs` 第 733–742 行的 `eval_closure` flow 处理，对照 `src/flow.rs` 第 88–96 行（循环消费）和 `src/lib.rs` 第 88–91 行（顶层兜底），用一张表填出「谁消费谁」：
     | 事件 | 循环(While/For) | 函数(eval_closure) | 模块顶层(eval) |
     |------|-----------------|---------------------|-----------------|
     | `Break` | 消费(清空+break) | 报 forbidden() | 报 forbidden() |
     | `Continue` | 消费(清空) | 报 forbidden() | 报 forbidden() |
     | `Return` | 放行(break,不清空) | 消费(取显式值或 body) | 报 forbidden() |
   - 再读 `src/code.rs` 第 413–415 行的 `warn_for_discarded_content`，确认其模式 `Return(span, Some(_), false)` 要求第三字段为 `false`。
3. **需要观察的现象**：
   - `forbidden()` 在模块顶层遇到 `return` 时：求值到顶层 `return` → `FuncReturn::eval` 写入 `Return` → 该事件一路冒泡、无人消费 → `eval()` 第 89–91 行 `if let Some(flow) = vm.flow { bail!(flow.forbidden()) }` → 命中 `Self::Return(span, _, _)` 分支 → 报 `cannot return outside of function`。
   - `conditional` 字段：若 `return` 写在 `if`/`while`/`for` 内部，事件冒泡时被 4.4.3 那段代码置为 `true`，于是 `warn_for_discarded_content` 的 `false` 匹配失败，**不告警**。
4. **预期结果**：
   - 模块顶层 `return 1` → 编译错误 `cannot return outside of function`，span 指向该 `return`。
   - `#let f() = { "前缀"; return "后缀" }` → 因为 `"前缀"` 是被丢弃的内容、return 无条件带值，会触发警告 `this return unconditionally discards the content before it`（提示「试着省略 return 让所有值自动 join」）。
   - `#let f() = { if true { "前缀"; return "后缀" } }` → 因 `return` 在 `if` 内被标为条件 return，**不触发**该警告。
5. 上述错误/告警文本与触发条件均直接对应源码，行为预测有据可查；若要在真实 typst 编译器中复现具体警告行，**待本地验证**（typst 的具体 `.typ` 测试用例位于本 crate 之外的 `tests/suite/scripting/` 目录）。

#### 4.4.5 小练习与答案

**练习 1**：在模块顶层写 `#return`，求值会报什么错？请追到源码具体行。

> **参考答案**：报 `cannot return outside of function`。链路：`FuncReturn::eval`（flow.rs:213-223）写入 `Return` → 一路无人消费 → `eval()`（lib.rs:88-91）检测到残留 `vm.flow` → 调 `flow.forbidden()` → 命中 `Self::Return(span, _, _)` 分支（flow.rs:34-36）返回该错误。

**练习 2**：为什么 `#let f() = { if cond { return 1 } }` 不会触发 `warn_for_discarded_content`，而 `#let f() = { return 1 }`（前面有被丢弃的内容时）会？

> **参考答案**：前者的 `return` 在 `if` 分支内，`Return` 事件冒泡经过 `Conditional::eval` 时被 `*conditional = true`（flow.rs:55-57）标记为「条件 return」。`warn_for_discarded_content` 的入口模式 `Return(span, Some(_), false)` 要求第三字段为 `false`，条件 return 是 `true` 故直接跳过（code.rs:415）。后者是模块直接求值出的无条件 return，第三字段保持 `false`，命中告警。

**练习 3**：函数体里写了一个裸 `break`（不在任何循环内），`eval_closure` 会如何处理？

> **参考答案**：`LoopBreak::eval` 写入 `Break(span)`，事件冒泡到 `eval_closure` 的 `match vm.flow`（call.rs:735-740）。由于不是 `Return`，命中 `Some(flow) => bail!(flow.forbidden())`（第 738 行），`forbidden()` 对 `Break` 返回 `cannot break outside of loop`（flow.rs:28-30）。

## 5. 综合实践

把本讲四个模块串起来，做一次「全链路纸笔追踪」。

**任务**：阅读下面这段 Typst 代码，不求值运行，仅依据本讲源码预测每一处 `vm.flow` 的变化与最终输出，并解释每一步的依据行号。

```typst
#let f() = {
  let acc = ()
  for i in (1, 2, 3, 4) {
    if i == 3 { break }
    if i == 2 { continue }
    acc.push(i)
  }
  return acc
}
#f()
```

**要求完成的步骤**：

1. 标出 `for` 进入时 `vm.flow.take()` 发生在哪一行（flow.rs:119），外层 `flow` 记下的值是什么（函数体刚进入，应为 `None`）。
2. 逐轮（`i=1,2,3,4`）追踪：`break`/`continue` 由 `LoopBreak`/`LoopContinue` 写入（flow.rs:191-211），循环内 `match vm.flow`（flow.rs:133-141）分别命中 `Break`（清空+退出）或 `Continue`（清空+下一轮）分支。说明 `acc.push(i)` 在哪些轮被执行。
3. 解释 `return acc` 写入 `Return(span, Some(acc), false)`（flow.rs:213-223）后，如何穿透 `for`（命中 `Some(Return(..)) => break`，不清空，flow.rs:139），再被 `eval_closure`（call.rs:736）取出 `Some(explicit)` 作为返回值。
4. 判断 `warn_for_discarded_content` 是否会触发：本例 `return` 不在任何 `if`/`while`/`for` 内部，第三字段保持 `false`；但 return 之前累加的 `output` 是 `Value::None`（`let` 与 `for` 都产出 `None`），不是 `Value::Content`，故 `warn_for_discarded_content` 的第二个匹配 `let Value::Content(tree) = &joined` 失败（code.rs:416），**不告警**。

**预期结论**：`acc` 最终为 `(1,)`——`i=1` 时 push `1`；`i=2` 时 `continue` 跳过 push；`i=3` 时 `break` 退出循环；`i=4` 永不执行。`f()` 返回数组 `(1,)`，无告警。整个过程中 `break`/`continue` 被循环消费（清空），`return` 穿透循环被函数消费。本结论基于源码推理，**待本地验证**。

## 6. 本讲小结

- `FlowEvent` 是 typst-eval 对 `break`/`continue`/`return` 的统一信号载体：`Break(Span)`、`Continue(Span)`、`Return(Span, Option<Value>, bool)`，三者都把意图写进 `vm.flow` 这个「信箱」而非用异常。
- 三个生产者 `LoopBreak::eval`/`LoopContinue::eval`/`FuncReturn::eval` 都用 `if vm.flow.is_none()` 护栏保证「首个事件优先、不被覆盖」，且都返回 `Value::None`。
- 「保存-恢复」三段式（`take()` 清空 → 求值 → 按需恢复）让内层控制流事件不会跨作用域泄漏；循环消费 `Break`/`Continue`（清空）、放行 `Return`，而 `eval_code` 对所有事件只放行不消费。
- `Return` 穿透循环后被 `eval_closure` 消费：带 `Some` 取显式值、带 `None` 或无事件取函数体产出；漂入函数的 `Break`/`Continue` 由 `forbidden()` 报错。
- `Return` 的第三字段 `conditional` 由 `Conditional`/`WhileLoop`/`ForLoop` 在事件冒泡时置 `true`，其唯一读者 `warn_for_discarded_content` 据此跳过条件 return，只对「无条件 return 丢弃内容」告警。
- 模块顶层（`eval`/`eval_string`）的残留 `vm.flow` 由 `flow.forbidden()` 兜底，于是顶层裸 `return` 报 `cannot return outside of function`。

## 7. 下一步学习建议

本讲讲完了「控制流事件如何产生、传递、消费」。接下来可以按两条线推进：

- **函数调用全貌**：本讲的 `eval_closure` 只是函数执行的「收尾」。完整的函数调用——参数求值、调用深度检查、`stacker` 防爆栈——在 u4-l1（函数调用与参数求值）展开；闭包如何定义、捕获变量在 u4-l3（闭包定义与 eval_closure 执行）与 u4-l4（CapturesVisitor）。
- **赋值与可变访问**：`return`/`break`/`continue` 是「控制流」层面的副作用，而 `let`、解构赋值、`array.push(...)` 这类是「数据」层面的副作用，后者依赖 `Access` trait 提供可变引用，见 u3-l3（let 绑定与解构赋值）与 u5-l3（可变访问 Access 与内置方法）。
- 建议继续精读的源码：把 `src/flow.rs` 全文（仅 246 行）通读一遍，确认你对 `is_invariant`/`can_diverge`（u3-l1）与三个生产者、`forbidden()`（本讲）的理解一致；再对照 `src/call.rs` 的 `eval_closure` 与 `call_func`，建立「函数调用 → 函数体求值 → return 消费」的完整心智模型。
