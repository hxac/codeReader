# 条件、循环与迭代求值

## 1. 本讲目标

本讲聚焦 `typst-eval/src/flow.rs` 中三类「让代码动起来」的结构的求值：`if`/`else` 条件、`while` 循环、`for` 循环。学完后你应当能够：

1. 看懂 `Conditional::eval` 如何对条件做 `bool` 类型转换（cast），并理解它结尾那行「标记条件 return」的作用。
2. 说清 `WhileLoop::eval` 用来检测死循环的两道关卡——静态的 `is_invariant`/`can_diverge`（报 `condition is always true`）与动态的 `MAX_ITERATIONS`（报 `loop seems to be infinite`）——以及二者的区别。
3. 理解 `ForLoop::eval` 如何用一个 `iter!` 宏统一处理「数组 / 字典 / 字符串 / 字节」四种可迭代值，并借助 `destructure` 把每个元素按 pattern 解构绑定。
4. 明白 `ops::join` 如何把每一轮循环的产出累加成单个 `Value`，以及循环如何消费 `break`/`continue`/`return` 三种控制流事件。

本讲承接 u2-l1（字面量与标识符求值）建立的「`Eval` trait + `Vm` 虚拟机 + `ast::Expr` 总分发器」框架。关于 `FlowEvent` 的「保存-恢复」模式，u2-l3（代码块与作用域）已经讲过它的基本形态，本讲会把这套机制用到真正的循环里；而 `break`/`continue`/`return` 这三个事件本身的 `Eval` 实现则在下一讲 u3-l2 专门展开。

## 2. 前置知识

在进入源码前，先回顾几个本讲会用到的概念：

- **`Eval` trait 与 `Output`**：每个 AST 节点实现 `fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>`。`if`/`while`/`for` 三者的 `Output` 都是 `Value`（它们本身是表达式，求值后会产出一个值）。
- **`Vm` 与 `vm.flow`**：虚拟机 `Vm` 持有求值状态，其中 `flow: Option<FlowEvent>` 字段记录「当前是否发生了控制流事件」。`break`/`continue`/`return` 不是通过 Rust 异常实现的，而是把自己的意图写进 `vm.flow`，由外层循环或函数去读取并处理。
- **`FlowEvent` 三态**：`Break`（跳出循环）、`Continue`（跳过本轮剩余）、`Return`（带返回值跳出函数）。
- **`ops::join`**：把两个值「拼接」成一个值，是循环累加输出时的单位元运算。
- **`Scopes::enter`/`exit`**：进入/退出一个词法作用域，控制 `let` 绑定的可见范围。

> 关键直觉：在 Typst 里，`while`/`for` 是**表达式**而不是语句，它们的求值结果是一个值（通常是各轮产出的「拼接」）。控制流（`break` 等）则是一种「带外信号」，通过 `vm.flow` 在求值状态里传递，而不是用 Rust 的 `return`/异常。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/flow.rs` | 本讲主角。定义 `FlowEvent` 枚举，并为 `Conditional`、`WhileLoop`、`ForLoop`、`LoopBreak`、`LoopContinue`、`FuncReturn` 实现 `Eval`，还包含死循环检测函数 `is_invariant`/`can_diverge`。 |
| `src/binding.rs` | 提供 `destructure` 函数。`for` 循环每一轮都用它把当前元素按 pattern 解构绑定到作用域。 |
| `../typst-library/src/foundations/ops.rs` | 定义 `ops::join`，是循环累加输出的核心运算。 |
| `../typst-library/src/foundations/scope.rs` | `Scopes::enter`/`exit`，`for` 循环用它们划分循环变量作用域。 |
| `../typst-library/src/foundations/cast.rs` | `impl IntoValue for (&Str, &Value)`，解释为什么字典迭代能被 `(k, v)` 解构。 |

测试依据（用于代码实践）：

- `tests/suite/scripting/loop.typ`：循环 + `break`/`continue` 的行为测试。
- `tests/suite/scripting/destructuring.typ`：`for` 循环多类型迭代与解构、不可迭代类型的报错。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲最简单的条件分支，再讲循环输出的通用累加机制，然后分别深入 `while` 的死循环检测与 `for` 的多类型迭代。

### 4.1 条件求值：bool cast 与「条件 return」标记

#### 4.1.1 概念说明

`if condition { ... } else { ... }` 是最朴素的分支结构。求值时解释器要决定走哪一条分支，因此必须先把 `condition` 的求值结果转换成 `bool`。这里有一个 Typst 的设计选择：**条件必须是布尔值**，而不是像某些语言那样做「真值判断」（truthiness）。也就是说，`if 1 { ... }` 在 Typst 里是非法的——整数 `1` 不会被当作「真」，必须显式写成 `if 1 == 1` 或 `if calc.bool(...)`。

除了选分支，`Conditional::eval` 还做了一件容易被忽略的事：如果 `if`/`else` 的某个分支里执行了 `return`，那么这个 `return` 是「有条件的」（取决于运行时到底走了哪条分支）。解释器会把这种信息记到 `vm.flow` 中的 `Return` 事件里，供函数求值时使用。

#### 4.1.2 核心流程

```text
求值 condition → 得到 Value
  ↓
cast 成 bool（失败则报类型错误）
  ↓
if 为真？ ──是──→ 求值 if_body
        └─否─→ 有 else？──有──→ 求值 else_body
                       └─无──→ 产出 Value::None
  ↓
若 vm.flow 当前是 Return 事件，则把它标记为「条件 return」(conditional = true)
  ↓
返回选中的分支产出
```

#### 4.1.3 源码精读

整个实现非常短，见 [flow.rs:L41-L61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L41-L61)：

```rust
let condition = self.condition();
let output = if condition.eval(vm)?.cast::<bool>().at(condition.span())? {
    self.if_body().eval(vm)?
} else if let Some(else_body) = self.else_body() {
    else_body.eval(vm)?
} else {
    Value::None
};
```

要点：

- `condition.eval(vm)?.cast::<bool>()` 先把条件求值成 `Value`，再用 `cast::<bool>()` 强制转成 Rust 的 `bool`。`.at(condition.span())` 把转换失败的错误（例如条件是整数）挂到条件表达式的源码位置上。
- 三路选择：真 → `if_body`；假且有存在 `else` → `else_body`；假且无 `else` → `Value::None`。

紧接着是「条件 return」标记，见 [flow.rs:L54-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L54-L57)：

```rust
// Mark the return as conditional.
if let Some(FlowEvent::Return(_, _, conditional)) = &mut vm.flow {
    *conditional = true;
}
```

`FlowEvent::Return` 的第三个字段就是这个布尔标记（见 [flow.rs:L19-L21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L19-L21)）。只要 `return` 是从 `if` 分支里冒泡出来的，它必然受运行时条件影响，于是被标记成 `conditional`。这个标记的消费者是函数求值（`eval_closure`），完整的「无条件 vs 条件 return」语义将在 u4-l3 讲解；这里你只需要记住：**条件、循环结构会把经过自己的 `Return` 事件统一降级为「条件 return」**。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Typst 条件必须是布尔值，不做真值判断」。

1. 操作步骤：写一个最小 Typst 文件，分别尝试 `#if 1 { "yes" }` 与 `#if 1 == 1 { "yes" }`。
2. 操作方式：参考仓库测试惯例，在 `tests/suite/scripting/` 下风格的 `.typ` 文件里写：

   ```typst
   // 预期报错：条件不是布尔值
   #if 1 { "yes" }
   // 预期输出 yes
   #if 1 == 1 { "yes" }
   ```

3. 需要观察的现象：第一行应当产生一条类型错误（`expected boolean` 一类），并指向条件 `1`；第二行正常输出。
4. 预期结果：与上述描述一致。
5. 待本地验证：如果你本机能编译 typst（`cargo build`），可用 `cargo test` 跑相关用例确认报错文案；若不方便编译，可对照 `flow.rs:46` 的 `.cast::<bool>().at(...)` 推断报错一定来自这里。

> 不要假装已经运行过命令。以上是「预期行为」，是否完全一致以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：`if` 表达式在没有 `else` 且条件为假时求值成什么？
**答案**：`Value::None`，对应 [flow.rs:L50-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L50-L51) 的兜底分支。

**练习 2**：为什么 `Conditional::eval` 结尾要把 `Return` 事件标记为 conditional？
**答案**：因为分支里的 `return` 是否真正执行，取决于运行时走了哪条分支，并非必然发生；这个「不确定性」需要被传递给函数求值，由它在 u4-l3 中决定后续处理。

**练习 3**：把 `cast::<bool>()` 改成「真值判断」会让 Typst 的哪类现有代码变得有歧义？
**答案**：所有「把 0/空串/空数组当作假」的隐式假设都不复存在；Typst 刻意要求显式布尔值，避免 `if 0` 这类跨语言歧义。

### 4.2 循环输出的累加：ops::join 与控制流消费

#### 4.2.1 概念说明

在深入 `while`/`for` 各自的特性之前，先讲清楚它们共享的一套机制：**循环是表达式，需要把每一轮的产出累加成单个 `Value`**。这个累加用的是 `ops::join`；而对 `break`/`continue`/`return` 的处理则用一套「先取走 `vm.flow`、循环内消费、结束后再恢复」的保存-恢复模式。

`while` 和 `for` 的循环体执行逻辑几乎逐字相同——这正是为什么 `for` 会用一个宏 `iter!` 来复用这套骨架（见 4.3）。本节先把这套公共骨架和 `ops::join` 讲透。

#### 4.2.2 核心流程

循环体每执行一轮：

```text
取走外部已存在的 flow（take）——循环对新事件「从干净状态开始」
  ↓
每一轮：
  求值 body → 得到本轮 Value
  output = ops::join(output, 本轮 Value)   ← 累加
  ↓
  检查 vm.flow：
    Break    → 清空 flow，跳出循环
    Continue → 清空 flow，进入下一轮
    Return   → 不清空，直接跳出（让事件继续冒泡到函数）
    None     → 正常进入下一轮
  ↓
循环结束后：若进入时取走的 flow 存在，则恢复它
  ↓
若当前 vm.flow 是 Return，标记为条件 return
```

这里有个关键约定：循环进入时 `vm.flow.take()` 会**清空**可能已存在的事件（来自更外层、尚未被消费的事件），循环结束后如果原本有事件则恢复。这保证了「本循环产生的事件」不会与「外部残留事件」互相干扰，同时外层的 `Return` 仍能在循环结束后继续向上冒泡。

#### 4.2.3 源码精读

`while` 循环里的累加与消费，见 [flow.rs:L85-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L85-L96)：

```rust
let value = body.eval(vm)?;
output = ops::join(output, value).at(body.span())?;

match vm.flow {
    Some(FlowEvent::Break(_)) => { vm.flow = None; break; }
    Some(FlowEvent::Continue(_)) => vm.flow = None,
    Some(FlowEvent::Return(..)) => break,
    None => {}
}
```

`for` 循环里几乎一模一样的片段被宏 `iter!` 包起来，见 [flow.rs:L126-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L126-L141)。

注意三种事件的处理差异：

- `Break`：`vm.flow = None` 清掉事件后 `break` 跳出——事件被「消费」了。
- `Continue`：`vm.flow = None` 清掉事件，但**不** `break`，于是自然进入下一轮——事件也被「消费」。
- `Return`：**不清空** `vm.flow`，直接 `break`。事件保留着，等循环结束后随「恢复 flow」逻辑继续向上冒泡，最终被函数求值消费。

`ops::join` 的语义是理解「循环产出了什么」的关键，见 [ops.rs:L24-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L24-L44)：

```rust
pub fn join(lhs: Value, rhs: Value) -> StrResult<Value> {
    use Value::*;
    Ok(match (lhs, rhs) {
        (a, None) => a,
        (None, b) => b,
        (Content(a), Content(b)) => Content(a + b),
        (Str(a), Str(b)) => Str(a + b),
        (Array(a), Array(b)) => Array(a + b),
        // ... 其他类型组合
        (a, b) => mismatch!("cannot join {} with {}", a, b),
    })
}
```

核心规律：**`None` 是 join 的单位元**——与 `None` 拼接返回对方。因此循环初始 `output = Value::None`（见 [flow.rs:L69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L69)），第一轮 `join(None, v)` 就得到 `v`。后续每轮把新产出拼接上去：内容拼内容、字符串拼字符串、数组拼数组。如果两轮产出类型不兼容（例如一轮是字符串、一轮是数组），`join` 会返回 `Err`，经 `.at(body.span())?` 转成源诊断。

> 用数学语言描述：设 \( J \) 为 `ops::join`，则 \( J(a, \text{None}) = a \)、\( J(\text{None}, b) = b \)，即 `None` 是其**右单位元与左单位元**。循环累加 \( v_1, v_2, \dots, v_n \) 的过程等价于 \( J(\cdots J(J(\text{None}, v_1), v_2)\cdots, v_n) \)。

这套机制解释了仓库测试里 `while true { ... str(i) ... }` 为什么能拼出 `"12345."` 这样的字符串——每一轮的字符串产出被 `join` 逐个拼接（见 `tests/suite/scripting/loop.typ` 中 `loop-break-join-basic` 用例）。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，验证 `join` 在循环里的拼接行为与单位元性质。

1. 实践目标：理解 `output` 初始为 `None` 时各轮产出如何被拼接。
2. 操作步骤：打开 [loop.typ 的 loop-break-join-basic 用例](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/scripting/loop.typ#L20-L33)（仓库路径 `tests/suite/scripting/loop.typ`，第 20–33 行）：

   ```typst
   #let i = 0
   #let x = while true {
     i += 1
     str(i)
     if i >= 5 { "."; break }
   }
   #test(x, "12345.")
   ```

3. 需要观察的现象：每一轮产出依次是 `"1"`、`"2"`、`"3"`、`"4"`，第 5 轮先产出 `"5"` 再产出 `"."`（同一轮里多条 markup/代码表达式的拼接见 u2-l3 的 `eval_code`）。
4. 预期结果：`join` 把它们按字符串拼接成 `"12345."`，与 `#test` 断言一致。
5. 待本地验证：实际 `#test` 结果以本地 `cargo test` 为准；若仅做源码阅读，可对照 [flow.rs:L85-L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L85-L86) 推断累加过程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Break` 和 `Continue` 都要把 `vm.flow` 置为 `None`，而 `Return` 不置？
**答案**：`break`/`continue` 是循环自己负责消费的事件，消费后必须清空，否则会被外层误判；`return` 的消费者是函数，循环无权消费，所以保留它继续冒泡。

**练习 2**：若某轮循环产出 `Value::None`，`output` 会变化吗？
**答案**：不会。`join(output, None) = output`，`None` 是右单位元，因此「什么都没产出」的轮次对累加结果无影响。

**练习 3**：一个循环里第 1 轮产出字符串、第 2 轮产出数组，会发生什么？
**答案**：第 2 轮 `ops::join(Str, Array)` 命中最后的 `mismatch!` 分支返回 `Err`，经 `.at(body.span())?` 转成「cannot join ... with ...」的源诊断。

### 4.3 WhileLoop 求值：死循环检测的三道关卡

#### 4.3.1 概念说明

`while condition { body }` 的难点不在循环本身，而在**如何防止用户写出无限循环把编译器/渲染器卡死**。`WhileLoop::eval` 用了两道互补的关卡：

1. **静态关卡**（`i == 0` 时）：`is_invariant(condition) && !can_diverge(body)` → 直接报 `condition is always true`。它能在第一次进入循环体之前就发现「条件永远为真，且循环体里没有任何 `break`/`return` 能逃出去」。
2. **动态关卡**（`i >= MAX_ITERATIONS` 时）：跑到 10000 轮还没结束 → 报 `loop seems to be infinite`。这是对静态分析漏网之鱼（条件里含变量、但变量其实永不改变）的兜底。

两条错误信息不同，对应「静态确定」与「动态兜底」两种情形，是本模块最重要的区分点。

#### 4.3.2 核心流程

```text
flow = vm.flow.take()        ← 保存外部事件
output = None; i = 0
  ↓
while 求值 condition 并 cast 成 bool：
  若 i==0 且 is_invariant(condition) 且 !can_diverge(body)：
      报错 "condition is always true"      ← 静态关卡
  否则若 i >= MAX_ITERATIONS(=10000)：
      报错 "loop seems to be infinite"      ← 动态关卡
  ↓
  求值 body → join 累加 → 消费 flow（见 4.2）
  i += 1
  ↓
循环结束：恢复外部 flow；若当前是 Return，标记 conditional
```

`MAX_ITERATIONS` 是一个常量，见 [flow.rs:L9-L10](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L9-L10)：

```rust
/// The maximum number of loop iterations.
const MAX_ITERATIONS: usize = 10_000;
```

#### 4.3.3 源码精读

死循环检测的核心片段在 [flow.rs:L76-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L76-L83)：

```rust
if i == 0
    && is_invariant(condition.to_untyped())
    && !can_diverge(body.to_untyped())
{
    bail!(condition.span(), "condition is always true");
} else if i >= MAX_ITERATIONS {
    bail!(self.span(), "loop seems to be infinite");
}
```

三个条件用 `&&` 串联，必须**同时**成立才报静态错误。逐一拆解：

- `i == 0`：只在第一次（条件刚被判为真）检查一次，避免每轮重复检查的开销。
- `is_invariant(condition)`：条件表达式是否「不含可变因素」。见 [flow.rs:L226-L239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L226-L239)：

  ```rust
  fn is_invariant(expr: &SyntaxNode) -> bool {
      match expr.cast() {
          Some(ast::Expr::Ident(_)) => false,
          Some(ast::Expr::MathIdent(_)) => false,
          Some(ast::Expr::FieldAccess(access)) =>
              is_invariant(access.target().to_untyped()),
          Some(ast::Expr::FuncCall(call)) =>
              is_invariant(call.callee().to_untyped())
              && is_invariant(call.args().to_untyped()),
          _ => expr.children().all(is_invariant),
      }
  }
  ```

  规则：只要条件里出现**标识符**（`Ident`/`MathIdent`），就视为「可能变化」，返回 `false`（不判定为不变量）。函数调用要看被调函数和参数是否都不含标识符。其他节点（字面量、运算符）则递归检查所有子节点。注意 `is_invariant` 故意做**保守判断**——`while x == x { }`（`x` 不变也恒真）因为有 `Ident` 而返回 `false`，逃过静态关卡，只能由动态关卡兜底。

- `!can_diverge(body)`：循环体里是否**没有** `break` 或 `return`。见 [flow.rs:L242-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L242-L245)：

  ```rust
  fn can_diverge(expr: &SyntaxNode) -> bool {
      matches!(expr.kind(), SyntaxKind::Break | SyntaxKind::Return)
          || expr.children().any(can_diverge)
  }
  ```

  只要 body 子树里任何位置存在 `break`/`return`，就认为「可能提前退出」，`can_diverge` 返回 `true`，于是 `!can_diverge` 为 `false`，**不报静态错误**。这正是 `while true { ...; break }` 合法的原因——仓库测试 `loop-break-join-basic` 正是这种写法。

三者合起来就是实践任务要回答的「为什么」：**条件恒为真（`is_invariant` 为真）且循环体里没有任何逃逸出口（`can_diverge` 为假）**，意味着条件永远不会变假、循环体也永远不会主动跳出，必然无限循环，故在第一轮就报 `condition is always true`。只要 body 里有 `break`/`return`，第三项 `!can_diverge` 变成 `false`，整条 `&&` 短路为 `false`，就不再报这个静态错误。

`while` 的完整实现还包含 4.2 讲过的 flow 保存恢复与累加，整体见 [flow.rs:L63-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L63-L112)。

#### 4.3.4 代码实践

**实践目标**：在源码中定位 `is_invariant` 与 `can_diverge` 的配合，并预测不同 `while` 写法会触发哪条报错。

1. 实践目标：用本节结论预测三种写法的命运。
2. 操作步骤：在 [flow.rs:L76-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L76-L83) 旁对照分析下列写法：

   | 写法 | `is_invariant(cond)` | `can_diverge(body)` | 结果 |
   |------|----------------------|---------------------|------|
   | `while true { calc.step() }` | `true`（`true` 无标识符） | `false` | `condition is always true` |
   | `while true { if i > 5 { break }; i += 1 }` | `true` | `true`（含 `break`） | 正常运行 |
   | `while x < 10 { }`（`x` 恒不变） | `false`（含 `Ident`） | `false` | 跑满 10000 轮后 `loop seems to be infinite` |

3. 需要观察的现象：第一行立即静态报错；第二行正常运行；第三行不在第 0 轮报错，而是循环到上限后动态报错。
4. 预期结果：与上表一致。关键是体会「静态关卡只抓**无条件**的死循环，动态关卡兜底**有变量但变量不变**的死循环」。
5. 待本地验证：以上为基于源码的预测；若本地可编译 typst，建议把三行分别放进测试用例确认报错文案与触发时机。

#### 4.3.5 小练习与答案

**练习 1**：`while 1 + 1 == 2 { }` 会触发哪条错误？为什么？
**答案**：`condition is always true`。`1 + 1 == 2` 不含标识符，`is_invariant` 递归到字面量全为真；body 无 `break`/`return`，`can_diverge` 为假；故静态关卡命中。

**练习 2**：把 `MAX_ITERATIONS` 改成 `1`，`while x < 10 { }`（`x` 不变）会发生什么？
**答案**：第 0 轮静态关卡因 `is_invariant` 为假不命中；第 1 轮 `i >= MAX_ITERATIONS(=1)` 命中，报 `loop seems to be infinite`。

**练习 3**：`is_invariant` 为什么对 `FieldAccess` 只检查 `target` 而不检查字段名？
**答案**：字段名是标识符字符串、在求值过程中不会变化；真正可能变的是「被访问的对象」`target`，所以只需递归判断 `target` 是否含可变标识符。

### 4.4 ForLoop 求值：iter! 宏与多类型迭代

#### 4.4.1 概念说明

`for pattern in iterable { body }` 比 `while` 多了两件事：

1. **多类型迭代**：`iterable` 可以是数组、字典、字符串、字节四种值，每种值的「一个元素」含义不同，必须分别处理。
2. **解构绑定**：`pattern` 可以是简单标识符 `x`，也可以是解构模式 `(a, b)`、`(k, v)`，甚至带 spread 的 `(a, ..rest)`。每一轮都要把当前元素按 pattern 绑定到作用域。

为了把「按 pattern 绑定 + 执行 body + 累加 + 消费 flow」这套和 `while` 几乎相同的骨架复用四次，`ForLoop::eval` 定义了一个 `iter!` 宏，接收不同的「可迭代源」展开成四份近似代码。

#### 4.4.2 核心流程

```text
flow = vm.flow.take();  output = None
  ↓
pattern = self.pattern();  iterable = self.iterable().eval(vm)
  ↓
按 (pattern 类型, iterable 值类型) match 分派：
  (_, Array)        → 对数组元素迭代
  (_, Dict)         → 对键值对迭代（每对打包成 2 元数组）
  (Normal|Placeholder, Str)  → 对 grapheme（字形簇）迭代
  (Normal|Placeholder, Bytes)→ 对字节（u8 整数）迭代
  (Destructuring, Str|Bytes) → 报错 "cannot destructure values of ..."
  _                            → 报错 "cannot loop over ..."
  ↓
iter! 宏展开（对任意可迭代源）：
  scopes.enter()                      ← 为整个循环开一个作用域
  for value in 可迭代源 {
      destructure(vm, pattern, value.into_value())  ← 按模式绑定
      body_value = body.eval(vm)
      output = ops::join(output, body_value)
      消费 flow（同 while）
  }
  scopes.exit()
  ↓
恢复 flow；若当前是 Return，标记 conditional
```

#### 4.4.3 源码精读

分派 `match` 是理解 for 循环的入口，见 [flow.rs:L153-L176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L153-L176)：

```rust
match (pattern, iterable) {
    (_, Value::Array(array)) => { iter!(for pattern in array); }
    (_, Value::Dict(dict)) => { iter!(for pattern in dict.iter()); }
    (Pattern::Normal(_) | Pattern::Placeholder(_), Value::Str(str)) => {
        iter!(for pattern in str.as_str().graphemes(true));
    }
    (Pattern::Normal(_) | Pattern::Placeholder(_), Value::Bytes(bytes)) => {
        iter!(for pattern in bytes.as_slice());
    }
    (Pattern::Destructuring(_), Value::Str(_) | Value::Bytes(_)) => {
        bail!(pattern.span(), "cannot destructure values of {iterable_type}");
    }
    _ => { bail!(self.iterable().span(), "cannot loop over {iterable_type}"); }
}
```

四个关键设计：

1. **数组与字典对 pattern 没有要求**（`_`）：数组天然可解构，字典迭代出的键值对也是 2 元数组，所以任意 pattern 都能交给 `destructure` 处理。
2. **字符串与字节只接受 `Normal`/`Placeholder` pattern**：因为它们的「元素」是单个字形簇或单个字节这种标量，没有内部结构可解构。这正是实践任务要回答的——为什么字符串和字节不能写 `for (a, b) in "foo"`：会命中第 5 条分支报 `cannot destructure values of string`（对应测试 `destructuring.typ` 第 347–349 行）。
3. **字符串用 `graphemes(true)`，字节用 `as_slice()`**：这是实践任务的第二问，详见下面专项分析。
4. **不可迭代类型**走最后的 `_` 分支报 `cannot loop over {iterable_type}`（如 `for x in [1,2]` 的 content、`for _ in 12306`，对应测试 `destructuring.typ` 第 331–341 行）。

`iter!` 宏把公共骨架封装起来，见 [flow.rs:L122-L146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L122-L146)：

```rust
macro_rules! iter {
    (for $pat:ident in $iterable:expr) => {{
        vm.scopes.enter();
        for value in $iterable {
            destructure(vm, $pat, value.into_value())?;
            let body = self.body();
            let value = body.eval(vm)?;
            output = ops::join(output, value).at(body.span())?;
            match vm.flow { /* Break/Continue/Return，与 while 相同 */ }
        }
        vm.scopes.exit();
    }};
}
```

要点：

- **整个循环共用一个作用域**：`scopes.enter()` 在循环外层调用一次，不是每轮一个新作用域。每轮通过 `destructure`→`vm.define` 把绑定**覆盖写入**同一个作用域。`scopes.enter/exit` 的实现见 [scope.rs:L34-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/foundations/scope.rs#L34-L43)（`enter` 用 `mem::take` 压栈，`exit` 弹栈）。
- **`destructure` 处理任意 pattern**：见 [binding.rs:L44-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L44-L57)，它把值按 `Normal`/`Placeholder`/`Parenthesized`/`Destructuring` 分派，对 `Ident` 调用 `vm.define` 完成绑定。所以 `for (a, b) in pairs` 的解构逻辑与 `let (a, b) = ...` 完全共用（详见 u3-l3）。
- **`value.into_value()`** 是不同迭代源产出统一 `Value` 的桥梁：

  | 迭代源 | 原始元素类型 | `into_value()` 后 |
  |--------|------------|------------------|
  | `Array` | `Value` | 原样 |
  | `dict.iter()` | `(&Str, &Value)` | 2 元数组 `[key, value]` |
  | `str.graphemes(true)` | `&str` | `Str` |
  | `bytes.as_slice()` 的 `&u8` | `u8` | `Int` |

  其中字典的 `(&Str, &Value)` 之所以能变成 2 元数组，靠的是 [cast.rs:L186-L190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/foundations/cast.rs#L186-L190) 的 `impl IntoValue for (&Str, &Value)`，所以 `for (k, v) in (a: 1, b: 2)` 才能用 `(k, v)` 解构出键和值。

**专项：字符串为什么用 graphemes，字节为什么用整数？**

字符串 `str.as_str().graphemes(true)` 来自 `unicode_segmentation::UnicodeSegmentation` trait（见 [flow.rs:L5](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L5) 的 `use`），`true` 表示使用「扩展字形簇」（extended grapheme clusters）。一个**字形簇**是用户感知到的「一个字」——例如表情符号 `😊`、带变音符号的 `é`（可能是单个码点，也可能是 `e` + `◌́` 两个码点组合）。字符串是文本，用户期望 `for c in "foo"` 按可见字符迭代，所以按 grapheme 切分。

字节 `bytes.as_slice()` 返回 `&[u8]`，迭代得到的是原始**字节整数**（`u8`），每个变成 `Value::Int`。字节是二进制数据，其自然单位就是字节本身（取值 \( 0 \le b \le 255 \)），不存在「字形」概念。

对比示例（待本地验证）：表情 `😊` 在 UTF-8 中是 4 个字节、1 个 grapheme。因此 `for c in "😊"` 会迭代 **1 次**（拿到整个表情字符串），而 `for b in bytes("😊")` 会迭代 **4 次**（拿到 4 个整数）。这就是字符串与字节采用不同迭代单位的根本原因——它们承载的是「文本语义」与「二进制语义」两种截然不同的数据。

`for` 的完整实现见 [flow.rs:L114-L189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L114-L189)。

#### 4.4.4 代码实践

**实践目标**：用仓库测试印证 for 循环的分派规则与解构行为。

1. 实践目标：对照分派表，预测四类输入的迭代方式或报错。
2. 操作步骤：阅读 [destructuring.typ 第 325–361 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/scripting/destructuring.typ#L325-L361)，关注以下用例：

   ```typst
   #for (a,b,c) in (("a", 1, bytes(())), ("b", 2, bytes(""))) {}   // 数组解构
   #for (k, v)  in (a: 1, b: 2, c: 3) {}                            // 字典解构
   // Error: cannot loop over content
   #for x in [1, 2] {}
   // Error: cannot destructure values of string
   #for (x, y) in "foo" {}
   ```

3. 需要观察的现象：
   - 第 1 行：每轮把 3 元数组解构成 `a`/`b`/`c`，正常。
   - 第 2 行：字典每轮产出 `[key, value]`，被 `(k, v)` 解构。
   - 第 3 行：content 命中最后的 `_` 分支，报 `cannot loop over content`。
   - 第 4 行：`Destructuring` pattern 配 `Str`，命中第 5 分支，报 `cannot destructure values of string`。
4. 预期结果：与各用例注释中的 `// Error` 完全对应。
5. 待本地验证：若想验证字符串按 grapheme 迭代，可自行写 `#for c in "foo" { c }` 观察迭代次数；emoji 案例需本地确认。

#### 4.4.5 小练习与答案

**练习 1**：`for x in (a: 1, b: 2)` 中，每轮 `x` 绑定到的值是什么？
**答案**：一个 2 元数组 `[<键字符串>, <值>]`，由 `(&Str, &Value)` 经 `into_value()` 打包而成（[cast.rs:L186-L190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/foundations/cast.rs#L186-L190)）。这也是为什么常写成 `for (k, v) in ...`。

**练习 2**：为什么 `iter!` 宏在循环外只调用一次 `scopes.enter()`，而不是每轮一次？
**答案**：循环变量的作用域是整个循环，每轮用 `destructure`→`vm.define` 把同名绑定**覆盖**到同一作用域即可；每轮新建作用域既无必要也会影响 `break` 后变量的可见性语义。

**练习 3**：`for c in bytes("AB")` 迭代几次，每次 `c` 是什么？
**答案**：2 次（`"AB"` 是两个 ASCII 字节），每次 `c` 是 `Value::Int`（65 和 66）。字节按 `u8` 整数迭代，而非字形。

## 5. 综合实践

把本讲四个模块串起来，完成下面的源码阅读型任务。

**任务**：给一段 Typst 循环代码「预测求值过程」，并对照 `flow.rs` 解释每一步。

阅读这段代码（综合了条件、while 死循环检测、for 多类型迭代、join 累加）：

```typst
#let out = for c in "café" {
  if c == "é" { "E" } else { c }
}
// 再观察：
// #while true { }            // 预期：condition is always true
// #while x < 1 { }           // 预期：loop seems to be infinite（x 恒不变）
```

请回答：

1. **多类型迭代**：`"café"` 会被迭代几次？为什么不是 5 次（`é` 在某些规范化下可能是两个码点）？引用 [flow.rs:L162-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L162-L165) 的 `graphemes(true)` 说明。
2. **累加**：写出每一轮 `output` 经 `ops::join` 后的演化过程（初始 `None`），并说明最终 `out` 的值。引用 [flow.rs:L126-L131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L126-L131)。
3. **死循环检测**：分别解释两行注释里的 `while` 为什么报不同的错误，并指出各自命中 [flow.rs:L76-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L76-L83) 的哪一条。
4. **控制流**：如果把循环体改成带 `break` 的版本（例如 `if c == "é" { break }`），`vm.flow` 在循环里是如何被消费的？引用 [flow.rs:L133-L141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L133-L141)。

**参考答案**：

1. `"café"` 按**扩展字形簇**迭代，通常为 4 次（`c`、`a`、`f`、`é`）。即便 `é` 由两个码点组合而成，`graphemes(true)` 也会把它算作 1 个字形簇，所以不是 5 次。
2. 每轮产出依次是 `Str("c")`、`Str("a")`、`Str("f")`、`Str("E")`（最后一轮因 `c == "é"` 走 `if` 分支）。`output` 演化：`None → "c" → "ca" → "caf" → "cafE"`，最终 `out = "cafE"`。
3. `while true { }`：`is_invariant(true)` 为真、body 无 `break`/`return` 故 `can_diverge` 为假 → 第 0 轮命中静态关卡，报 `condition is always true`。`while x < 1 { }`：条件含 `Ident x`，`is_invariant` 为假，静态关卡不命中，只能跑满 10000 轮后命中 `i >= MAX_ITERATIONS`，报 `loop seems to be infinite`。
4. `break` 执行时把 `FlowEvent::Break` 写入 `vm.flow`；本轮 body 求值后，`match vm.flow` 命中 `Some(FlowEvent::Break(_))` 分支，执行 `vm.flow = None` 清空事件并 `break` 跳出循环体。事件被「消费」，不会继续冒泡。

> 提示：第 1、2 问的精确结果（尤其是 `é` 的码点构成）受 Unicode 规范化影响，建议本地用 `cargo test` 或 typst CLI 实测确认。

## 6. 本讲小结

- `Conditional::eval` 强制把条件 `cast` 成 `bool`（不做真值判断），并在结尾把经过自己的 `Return` 事件标记为「条件 return」。
- 循环（`while`/`for`）用「`vm.flow.take()` 进入 → 循环内消费 → 结束后恢复」的保存-恢复模式处理控制流；`break`/`continue` 被消费（清空 flow），`return` 被保留冒泡。
- 循环输出靠 `ops::join` 累加，`None` 是其单位元；类型不兼容的两轮产出会报 `cannot join` 错误。
- `WhileLoop::eval` 用两道关卡防死循环：静态关卡 `is_invariant && !can_diverge` 报 `condition is always true`，动态关卡 `i >= MAX_ITERATIONS(=10000)` 报 `loop seems to be infinite`。
- `ForLoop::eval` 用 `match (pattern, iterable)` 分派四种可迭代类型，并用 `iter!` 宏复用「作用域 + 解构 + 累加 + 消费 flow」骨架；`destructure` 复用自 `binding.rs`，使 `for (a, b) in ...` 与 `let (a, b) = ...` 共享同一套解构逻辑。
- 字符串按 **grapheme（字形簇）** 迭代以符合「文本语义」，字节按 **u8 整数** 迭代以符合「二进制语义」；二者的 pattern 都只能是 `Normal`/`Placeholder`，不可解构。

## 7. 下一步学习建议

1. **下一讲 u3-l2「控制流事件」**：本讲把 `break`/`continue`/`return` 当作「写入 `vm.flow` 的信号」来用，下一讲会精读 `LoopBreak`、`LoopContinue`、`FuncReturn` 三个 `Eval` 实现（[flow.rs:L191-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L191-L223)）、`FlowEvent::forbidden()` 如何在非法位置报错，以及 `Return` 第三字段 `conditional` 在顶层被拦截的细节。
2. **继续阅读 u3-l3「let 绑定与解构赋值」**：本讲只用了 `destructure` 的「读」入口，u3-l3 会完整讲解 `destructure_impl` 对数组/字典的解构、spread sink 与 `wrong_number_of_elements` 诊断。
3. **延伸到 u4-l3「闭包与 eval_closure」**：理解 `Return` 事件（尤其是本讲的 `conditional` 标记）最终如何被函数求值消费并决定返回值。
4. **建议动手源码练习**：在本地 fork typst，把 `MAX_ITERATIONS` 改小（如 5），观察 `while`/`for` 的动态关卡何时触发，加深对两道关卡分工的直观印象。
