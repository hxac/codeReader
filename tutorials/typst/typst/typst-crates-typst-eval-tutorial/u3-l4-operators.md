# 一元与二元运算符求值

## 1. 本讲目标

本讲聚焦 `typst-eval` 中所有「运算符」的求值实现，集中在 `src/ops.rs`（并借助 `src/access.rs` 的可变访问能力）。

学完后你应该能够：

- 说清 `Unary`（一元）与 `Binary`（二元）运算符的 `Eval` 实现如何把「值的实际计算」整体委托给 `typst_library::foundations::ops`，而 `typst-eval` 只负责**编排**：求操作数、处理短路、定位可变写位置、抛出特定错误。
- 理解 `and` / `or` 的短路求值发生在 `typst-eval` 层（`apply_binary`），而非被委托的 `ops::and` / `ops::or`，并理解 Typst「无隐式真值」的设计。
- 理解 `apply_assignment` 如何借助 `Access` trait 实现「读—改—写」，以及为何普通赋值 `obj.field = x` 走 `access_dict` + `dict.insert` 这条「可创建字段」的特殊路径。
- 看懂整数取负溢出错误 `overflowing_int_negation_error` 如何在错误冒泡途中被「拦截改写」成更精准的诊断。

## 2. 前置知识

本讲承接 [u2-l1 基础字面量与标识符求值](u2-l1-literals-idents.md)，默认你已理解：

- **`Eval` trait 与总分发器**：`ast::Expr::eval` 是一个大 `match`，把每种表达式变体派发到各自的 `Eval` 实现，末尾统一 `.spanned(span)` 并调 `vm.trace_at`（见 `src/code.rs`）。`Unary` / `Binary` 只是其中的两个分支。
- **`Value` 与 `SourceResult`**：求值产出 `typst_library::foundations::Value`，可能失败时返回带 `span` 的 `SourceResult`。
- **错误三要素**：typst-eval 的好诊断通常包含「错误信息 + 精确定位（span / SubRange）+ 修复提示（hint）」。

几个本讲会反复出现的术语，先做通俗解释：

- **运算符（operator）**：像 `+ - * /`、`and or not`、`== < >=`、`= +=`、`in` 这样的符号。Typst 把它们分成 **一元**（`+x` `-x` `not x`，对应 `ast::UnOp`）和 **二元**（`a + b` 等，对应 `ast::BinOp`）。
- **短路求值（short-circuit）**：对 `a and b`，只要 `a` 是 `false`，整个表达式一定是 `false`，没必要再算 `b`；`a or b` 同理，`a` 为 `true` 即可短路。typst-eval 利用这一点跳过对右侧（rhs）的求值。
- **可变访问（mutable access）**：赋值 `x = 5`、复合赋值 `x += 1` 都需要拿到变量 `x` 当前的**存储位置**（一个 `&mut Value`），就地修改它。`Access` trait 就是为此设计的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/ops.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs) | 全部运算符的 `Eval` 实现：`Unary`、`Binary`，以及三个内部辅助函数 `apply_binary`（含短路）、`apply_assignment`（含可变写与字段赋值特殊路径）、`overflowing_int_negation_error`（取负溢出诊断）。 |
| [src/access.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs) | `Access` trait：为赋值/复合赋值提供 `&mut Value`；`access_dict` 在字段赋值特殊路径中被复用。 |
| [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) | `ast::Expr::eval` 总分发器，其中 `Self::Unary(v) => v.eval(vm)` 与 `Self::Binary(v) => v.eval(vm)` 是进入本讲的两个入口分支。 |

> 说明：被委托的 `ops::add` / `ops::neg` 等定义在 `typst-library` crate 的 `foundations::ops` 模块中（不在本 crate 工作区内）。本讲只讨论 typst-eval **如何委托**，不深入 `typst-library` 内部的数值语义。

---

## 4. 核心概念与源码讲解

### 4.1 运算符求值的总体架构：从 AST 到 ops 委托

#### 4.1.1 概念说明

一个运算符表达式（比如 `a + b`）求值时，需要做两件事：

1. **编排（orchestration）**：驱动虚拟机求出操作数的值、决定求值顺序、处理短路、定位写位置、附加 `span` —— 这是 `typst-eval` 的职责，因为它需要 `&mut Vm`。
2. **值的实际计算（semantics）**：两个值怎么相加、怎么比较 —— 这是 `typst-library` 的职责。

typst-eval 选择把第 2 步**整体委托**给 `typst_library::foundations::ops` 模块。于是 `ops.rs` 顶部那条导入就是本讲的「总纲」：

```rust
use typst_library::foundations::{IntoValue, Value, ops};
```

`ops` 是一个函数集合，每个函数签名形如 `fn(Value, Value) -> HintedStrResult<Value>`（二元）或 `fn(Value) -> HintedStrResult<Value>`（一元）。`typst-eval` 只调用它们、把返回的字符串错误用 `.at(span)` 贴上位置。

#### 4.1.2 核心流程

运算符求值的总体流程：

```
ast::Expr::eval (总分发器, code.rs:132/133)
        │
        ├── Self::Unary(v)  →  ast::Unary::eval (ops.rs)
        │       求 expr →（取负溢出拦截）→ ops::pos/neg/not → .at(span)
        │
        └── Self::Binary(v) →  ast::Binary::eval (ops.rs)
                按 BinOp 分派：
                  算术/逻辑/比较 → apply_binary(self, vm, ops::xxx)  （含短路）
                  赋值/复合赋值   → apply_assignment(self, vm, ops::xxx)
```

关键点：**typst-eval 不实现任何算术语义**。它只决定「何时求操作数、按什么顺序、结果贴哪个 span」，真正的计算永远是 `ops::*`。

#### 4.1.3 源码精读

进入本讲的总分发器分支，确认 `Unary` / `Binary` 是普通 `Eval` 分发的一员（与字面量、标识符同级）：

[Unary/Binary 在 Expr 总分发器中的分支 src/code.rs:132-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L132-L133) —— 这里把 `ast::Unary` / `ast::Binary` 直接交给各自的 `eval`。

`Binary::eval` 是一个纯粹的「分派表」，把每种 `BinOp` 映射到 `apply_binary` 或 `apply_assignment`，并传入对应的 `ops::*` 函数指针：

```rust
impl Eval for ast::Binary<'_> {
    type Output = Value;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        match self.op() {
            ast::BinOp::Add => apply_binary(self, vm, ops::add),
            // ... Sub/Mul/Div ...
            ast::BinOp::And => apply_binary(self, vm, ops::and),
            ast::BinOp::Or  => apply_binary(self, vm, ops::or),
            // ... Eq/Neq/Lt/Leq/Gt/Geq/In/NotIn ...
            ast::BinOp::Assign    => apply_assignment(self, vm, |_, b| Ok(b)),
            ast::BinOp::AddAssign => apply_assignment(self, vm, ops::add),
            // ... SubAssign/MulAssign/DivAssign ...
        }
    }
}
```

[Binary::eval 分派表 src/ops.rs:25-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L25-L51) —— 注意普通赋值 `=` 的 op 是个忽略左值的恒等闭包 `|_, b| Ok(b)`，这与复合赋值（复用 `ops::add` 等）形成对照（详见 4.4）。

> 这张表把「运算符 → 执行函数」的映射固化在一处，新增一个运算符只需在此加一行、在 `ops` 加一个函数，职责清晰。

#### 4.1.4 代码实践

**实践目标**：亲手把「运算符求值 = 编排 + 委托」这条链路走一遍。

**操作步骤**：

1. 在 [src/code.rs:132-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L132-L133) 找到 `Self::Binary(v) => v.eval(vm)`。
2. 跳到 [src/ops.rs:30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L30)，确认 `BinOp::Add` 委托给 `apply_binary(self, vm, ops::add)`。
3. 跳到 [src/ops.rs:54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L54) 的 `apply_binary`，看到它求出 `lhs`、`rhs` 后调用 `op(lhs, rhs)`——这个 `op` 就是第 2 步传进来的 `ops::add`。

**需要观察的现象**：从「语法节点 `Binary`」到「实际计算函数 `ops::add`」之间，typst-eval 只插入了「求 lhs / 求 rhs / 贴 span」三件事，没有任何算术逻辑。

**预期结果**：你能画出 `1 + 2` 的调用链 `Expr::eval → Binary::eval → apply_binary(ops::add) → ops::add(Value::Int(1), Value::Int(2))`，其中 typst-eval 不含加法实现。（`ops::add` 内部对 `Int + Int` 的处理在 typst-library，待本地验证其返回 `Value::Int(3)`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ops.rs` 里看不到 `1 + 2 == 3` 的加法实现？

> **答**：因为加法语义属于运行时值，由 `typst_library::foundations::ops::add` 实现。typst-eval 只通过函数指针 `ops::add` 委托，自身不含算术。

**练习 2**：如果想新增一个二元运算符 `<?>`，按本讲架构需要改哪几处（仅限 typst-eval 视角）？

> **答**：在 `ast::BinOp`（typst-syntax）新增变体；在 `Binary::eval` 的 `match` 加一行 `apply_binary(self, vm, ops::<?>)`；若它也需短路或特殊写位置，则还要在 `apply_binary`/`apply_assignment` 里分支。计算逻辑仍放 `typst-library` 的 `ops`。

---

### 4.2 一元运算符与整数取负溢出

#### 4.2.1 概念说明

一元运算符只有三个：`+x`（`Pos`）、`-x`（`Neg`）、`not x`（`Not`）。它们都映射到 `ops::pos` / `ops::neg` / `ops::not`。

其中 `-x` 有一个特殊场景：Typst 的整数是带符号 64 位（i64），最大正值是 \(2^{63}-1\)。当你写一个**字面量** `-9223372036854775808`（即 \(-2^{63}\)，正是 i64 的最小值）时，语法层会先解析正字面量 `9223372036854775808`（= \(2^{63}\)），而它已经**溢出**了 i64 的正数范围。typst-syntax 的 `Int::get()` 会返回一个 `PosOverflow` 错误。

typst-eval 在求一元负号时**拦截**这个底层错误，改写成一个更贴合用户意图、带修复提示的诊断——这就是 `overflowing_int_negation_error` 存在的原因。

#### 4.2.2 核心流程

```
ast::Unary::eval
  │
  ├─ value = expr.eval(vm)
  │     └─ 用 .map_err 拦截：若是「Int 字面量 + Neg + PosOverflow」
  │        则用 overflowing_int_negation_error 的错误替换原错误
  │
  ├─ result = match op {
  │     Pos => ops::pos(value),
  │     Neg => ops::neg(value),
  │     Not => ops::not(value),
  │  }
  │
  └─ result.at(self.span())   // 把字符串错误贴上 span
```

注意拦截发生在**产生值之前**：一旦字面量本身溢出，`expr.eval(vm)` 就返回 `Err`，根本到不了 `ops::neg`。

#### 4.2.3 源码精读

一元求值实现（注意 `.map_err` 这一行）：

```rust
impl Eval for ast::Unary<'_> {
    type Output = Value;
    fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
        let expr = self.expr();
        let value = expr.eval(vm).map_err(|err| {
            overflowing_int_negation_error(self, expr).err().unwrap_or(err)
        })?;
        let result = match self.op() {
            ast::UnOp::Pos => ops::pos(value),
            ast::UnOp::Neg => ops::neg(value),
            ast::UnOp::Not => ops::not(value),
        };
        result.at(self.span())
    }
}
```

[Unary::eval src/ops.rs:8-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L8-L23) —— `.map_err` 的逻辑很巧妙：`overflowing_int_negation_error(...)` 返回 `SourceResult<()>`；若它生成了错误（命中取负溢出场景），`.err().unwrap_or(err)` 就用这个**更精准**的错误替换原 `err`；否则保留原 `err`（普通情况不受影响）。

取负溢出诊断函数（标了 `#[cold]`，因为它只在罕见错误路径执行）：

```rust
#[cold]
fn overflowing_int_negation_error(unary: ast::Unary, expr: ast::Expr) -> SourceResult<()> {
    if let ast::Expr::Int(int) = expr
        && unary.op() == ast::UnOp::Neg
        && let Err(ast::IntLiteralError::PosOverflow { base, max_plus_one }) = int.get()
    {
        if max_plus_one {
            bail!(unary.span(),
                "cannot write minimum integer manually";
                hint: "Typst integers are always initially positive";
                hint: "2^63 does not fit into a signed 64-bit integer";
                hint: "try writing `int.min`";
            );
        } else {
            // 值比 2^63 还大，给出「改用浮点数」的修复提示
            let mut error = error!(unary.span(), "integer value is too small"; ...);
            ...
        }
    }
    Ok(())
}
```

[overflowing_int_negation_error src/ops.rs:97-134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L97-L134) —— 它把 `Int::get()` 返回的 `PosOverflow { base, max_plus_one }` 细分成两种：
- `max_plus_one == true`：字面量恰好是 \(2^{63}\)，取负后正好等于 i64 最小值；提示用内置常量 `int.min`。
- 否则：值大到连取负都救不了；提示追加小数点改写成浮点数（如 `9223372036854775809.`）。

> 这是一个「错误改写（error rewriting）」的典范：typst-eval 利用自己**知道当前是一元负号**这一上下文，把一个泛泛的「整数溢出」改写成「你不能手写最小整数，请用 `int.min`」这种可操作的提示。这正是「错误信息 + 定位 + 修复 hint」三要素的体现。

#### 4.2.4 代码实践

**实践目标**：体验取负溢出的诊断改写。

**操作步骤**：

1. 准备一段 Typst 代码（待本地验证）：`#(-9223372036854775808)`。
2. 对照源码预测报错信息与 hint：命中 `max_plus_one == true` 分支，应给出 `cannot write minimum integer manually` 及 `try writing \`int.min\`` 提示。
3. 再写一个更大的值，如 `#(-99999999999999999999)`，预测命中另一分支并建议改用浮点。

**需要观察的现象**：报错信息不是泛泛的「integer too large」，而是针对「你在对一个正字面量取负」这一具体场景定制。

**预期结果**：第一条给出 `int.min` 提示；第二条给出「追加小数点变浮点」提示（如 `99999999999999999999.`）。具体措辞以本地运行为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `overflowing_int_negation_error` 用 `let ... && ... && ...`（let 链）做三个条件判断？去掉「`unary.op() == ast::UnOp::Neg`」会怎样？

> **答**：三个条件分别是「表达式是 Int 字面量」「运算符是取负」「`Int::get()` 返回 `PosOverflow`」，三者同时成立才需要改写。若去掉取负判断，`+9223372036854775808`（正号）也会被错误地改写成「最小整数」提示——而它根本不是取负场景。

**练习 2**：`#[cold]` 标注在这里起什么作用？

> **答**：提示编译器该函数很少执行（只在错误路径），可把它移出热路径、不污染指令缓存。一元求值的热路径（`ops::neg` 等）因此更紧凑。

---

### 4.3 二元运算与短路求值

#### 4.3.1 概念说明

绝大多数二元运算（`+ - * /`、比较、`in`）的求值很直接：求左、求右、调用 `ops::*`。但 `and` / `or` 有**短路求值**需求。

typst-eval 的设计选择：**短路逻辑放在 typst-eval 的 `apply_binary` 里**，而不是被委托的 `ops::and` / `ops::or`。原因有二：

- 短路必须**避免求 rhs**，而「求 rhs」需要 `&mut Vm`——`ops::and` 只是个纯函数 `fn(Value, Value) -> ...`，没有也不该有 `Vm`，无法决定要不要求 rhs。
- `ops::and` / `ops::or` 因此可以是无副作用的纯二元函数，只负责「两个布尔如何运算」。

另一个重要点：Typst **没有隐式真值**（truthiness）。`if`、`and`、`or` 都要求布尔操作数；`0`、空字符串等不会被当作 `false`。所以短路判断用的是精确的值比较 `lhs == false.into_value()`（即 `Value::Bool(false)`），不是真值判断。

#### 4.3.2 核心流程

```
apply_binary(binary, vm, op):
  lhs = binary.lhs().eval(vm)         // 总是先求左侧
  if (op 是 And 且 lhs == false)
     or (op 是 Or 且 lhs == true):
       return Ok(lhs)                  // 短路：不求 rhs，直接返回 lhs
  rhs = binary.rhs().eval(vm)          // 否则才求右侧
  return op(lhs, rhs).at(span)         // 委托 ops::* 计算
```

#### 4.3.3 源码精读

```rust
fn apply_binary(
    binary: ast::Binary,
    vm: &mut Vm,
    op: fn(Value, Value) -> HintedStrResult<Value>,
) -> SourceResult<Value> {
    let lhs = binary.lhs().eval(vm)?;

    // Short-circuit boolean operations.
    if (binary.op() == ast::BinOp::And && lhs == false.into_value())
        || (binary.op() == ast::BinOp::Or && lhs == true.into_value())
    {
        return Ok(lhs);
    }

    let rhs = binary.rhs().eval(vm)?;
    op(lhs, rhs).at(binary.span())
}
```

[apply_binary src/ops.rs:53-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L53-L70) —— 读法：
- `binary.op()` 即便已经传入了 `op` 函数指针，这里仍需重新读 `op()` 枚举来判断是不是 `And`/`Or`——因为函数指针无法反查「我是谁」。
- 短路返回的是 `Ok(lhs)` 本身（一个 `Value::Bool`），语义正确。
- 非短路时，`op(lhs, rhs)` 是 `ops::add` / `ops::eq` / `ops::and` / …… 的统一调用点，最后 `.at(binary.span())` 贴上整个二元表达式的位置。

> 短路只跳过「求 rhs」，从不跳过「求 lhs」。所以 `false and expensive()` 里 `expensive()` 不会执行；但 `expensive() and false` 里 `expensive()` 仍会先执行——这是左结合、左求值的自然结果。

#### 4.3.4 代码实践

**实践目标**：亲眼验证短路确实跳过了 rhs 求值。

**操作步骤**：

1. 写一段带可观测副作用的 Typst（待本地验证）：
   ```typst
   #let side-effect-counter = counter("c")
   #let f() = { side-effect-counter.step(); false }
   // 情况 A：f() 在 lhs
   #(f() and true)
   // 情况 B：f() 在 rhs
   #(false and f())
   ```
2. 对照 `apply_binary` 预测：情况 A 会先求 `f()`（计数器 +1），然后 `f() == false` 命中 `And` 短路返回 `false`；情况 B 求出 `lhs = false` 立即短路，**根本不调用** `f()`，计数器不变。

**需要观察的现象**：两次之后计数器的值（A 有副作用、B 无副作用）。

**预期结果**：只有情况 A 让计数器增加。这印证「短路发生在求 rhs 之前」。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`0 and true` 会短路吗？结果是什么？

> **答**：不会短路。短路条件是 `lhs == Value::Bool(false)`，而 `lhs` 是 `Value::Int(0)`，不相等。于是继续求 rhs 并调用 `ops::and(Int(0), Bool(true))`——而 `ops::and` 要求布尔操作数，会报类型错误。这正是「Typst 无隐式真值」的体现。

**练习 2**：既然短路逻辑在 typst-eval，那 `ops::and` 还有什么用？

> **答**：处理「两侧都需求值」的常规情况（如 `true and false`、`x and y` 中 `x` 非 `false`），把两个布尔按逻辑与运算。它是无状态的纯函数，与短路互补。

---

### 4.4 赋值、复合赋值与可变访问

#### 4.4.1 概念说明

赋值类运算符（`= += -= *= /=`）与前述运算不同：它们不「计算一个新值」，而是**就地修改一个已存在的存储位置**。比如 `x = 5` 要把变量 `x` 绑定的那个 `Value` 改写成 `5`；`x += 1` 要读出旧值、加 1、再写回。

这就需要一种「拿到 `&mut Value`」的能力——正是 `src/access.rs` 的 `Access` trait。`apply_assignment` 是消费者，`Access` 各实现是提供者。

`apply_assignment` 有两条路径：

1. **字段赋值特殊路径**：当是普通赋值（`=`）且左值是字段访问 `obj.field` 时，走 `access_dict` + `dict.insert`。它**能创建新字段**。
2. **通用路径**：其余情况（普通变量赋值、所有复合赋值）走 `lhs.access(vm)` 拿到 `&mut Value`，用 `std::mem::take` 取出旧值、计算、写回。

#### 4.4.2 核心流程

```
apply_assignment(binary, vm, op):
  rhs = binary.rhs().eval(vm)            // 先求右值
  lhs_expr = binary.lhs()

  // 特殊路径：obj.field = rhs  （仅普通 =）
  if op 是 Assign 且 lhs_expr 是 FieldAccess:
      dict = access_dict(vm, access)     // 拿到 &mut Dict
      dict.insert(field, rhs)            // 创建或覆盖字段
      return None

  // 通用路径：变量赋值 / 复合赋值
  location = lhs_expr.access(vm)         // 拿到 &mut Value（必须已存在）
  old = std::mem::take(&mut *location)   // 取出旧值，原地暂时变 None
  *location = op(old, rhs).at(span)      // 计算并写回
  return None
```

#### 4.4.3 源码精读

```rust
fn apply_assignment(
    binary: ast::Binary,
    vm: &mut Vm,
    op: fn(Value, Value) -> HintedStrResult<Value>,
) -> SourceResult<Value> {
    let rhs = binary.rhs().eval(vm)?;
    let lhs = binary.lhs();

    // An assignment to a dictionary field is different from a normal access
    // since it can create the field instead of just modifying it.
    if binary.op() == ast::BinOp::Assign
        && let ast::Expr::FieldAccess(access) = lhs
    {
        let dict = access_dict(vm, access)?;
        dict.insert(access.field().get().clone().into(), rhs);
        return Ok(Value::None);
    }

    let location = binary.lhs().access(vm)?;
    let lhs = std::mem::take(&mut *location);
    *location = op(lhs, rhs).at(binary.span())?;
    Ok(Value::None)
}
```

[apply_assignment src/ops.rs:72-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L72-L95) —— 三个要点：

1. **rhs 先于 lhs 求值**：先 `binary.rhs().eval(vm)`，再处理左值位置。这与 C 系语言「先算右、再写左」一致。
2. **字段赋值特殊路径**：`obj.field = x` 调 `access_dict` 拿 `&mut Dict`，再 `dict.insert`——`insert` 是「存在则覆盖、不存在则新增」，所以能**创建**新字段。注意它只用 `let ... && ...` 的 let 链同时判断「是 `Assign`」和「左值是 `FieldAccess`」两个条件，因此**复合字段赋值**（`obj.field += x`）不会进这条路径，而走通用路径。
3. **`std::mem::take` 的读—改—写**：`location` 是 `&mut Value`，`Value` 的 `Default` 是 `Value::None`。`std::mem::take(&mut *location)` 把旧值**搬走**（位置暂时为 `None`），随后 `*location = op(old, rhs)` 写回新值。这是 Rust 中对可变引用做「读—改—写」的经典写法（避免克隆，也避免借用冲突）。

为什么需要 `mem::take` 而不是直接 `*location = op(*location.clone(), rhs)`？因为 `op` 计算期间不应再借用 `location` 内部；`take` 把值搬出后，`location` 指向的内存暂时是合法的 `None`，借用被「释放」，计算完再整体写回。

`Access` trait 的定义与字段访问实现：

```rust
pub(crate) trait Access {
    fn access<'a>(self, vm: &'a mut Vm) -> SourceResult<&'a mut Value>;
}

impl Access for ast::FieldAccess<'_> {
    fn access<'a>(self, vm: &'a mut Vm) -> SourceResult<&'a mut Value> {
        access_dict(vm, self)?.at_mut(self.field().get()).at(self.span())
    }
}
```

[Access trait 与 Expr 分发 src/access.rs:9-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L9-L27) —— `Access` 只对四类表达式实现：`Ident`、`Parenthesized`、`FieldAccess`、`FuncCall`（访问器方法）；其余表达式走默认分支 `bail!("cannot mutate a temporary value")`。

[FieldAccess::access src/access.rs:50-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L50-L54) —— 注意它用 `at_mut(field)`，要求字段**已存在**，否则报错。这正是「通用路径只能改、不能建」的原因，也是字段赋值特殊路径存在的理由。

`access_dict` 决定哪些类型的字段可被取到 `&mut Dict`：

```rust
pub(crate) fn access_dict<'a>(vm: &'a mut Vm, access: ast::FieldAccess)
    -> SourceResult<&'a mut Dict>
{
    match access.target().access(vm)? {
        Value::Dict(dict) => Ok(dict),
        value => {
            // Symbol/Content/Module/Func/Args 有自己的字段 getter，不可变
            // 其余类型：要么没有可访问字段，要么「fields not yet mutable」
            ...
        }
    }
}
```

[access_dict src/access.rs:76-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L76-L107) —— 它只对真正的 `Value::Dict` 返回可变引用；对 `Symbol/Content/Module/Func/Args` 报「cannot mutate fields」（它们有专属 getter），对有静态字段但无 setter 的类型报「fields not yet mutable」并提示「新建一个对象」。

#### 4.4.4 代码实践

**实践目标**（本讲核心任务）：在 `apply_assignment` 中找到「字段赋值」特殊分支，解释 `obj.field = x` 为何用 `dict.insert` 而非普通 `access` 修改；并说明 `x += y` 复合赋值如何用 `std::mem::take` 取旧值再写回。

**操作步骤**：

1. **字段赋值分支定位**：在 [src/ops.rs:83-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L83-L89) 找到 `if binary.op() == ast::BinOp::Assign && let ast::Expr::FieldAccess(access) = lhs`。
2. **对比两条路径**：
   - 特殊路径：`access_dict` → `dict.insert(field, rhs)`。`insert` 对 `IndexMap` 是「键存在则覆盖、不存在则新增」，因此 `dict.newkey = 5`（`newkey` 原本不存在）能成功**创建**字段。
   - 通用路径的 `FieldAccess::access`（[access.rs:50-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L50-L54)）用 `at_mut(field)`，字段不存在即报错，**只能改不能建**。
3. **复合赋值的读—改—写**：在 [src/ops.rs:91-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L91-L93) 看 `std::mem::take(&mut *location)`：先把 `location` 旧值搬出（原地变 `None`），`op(old, rhs)` 算出新值，再 `*location = ...` 写回。

**需要观察的现象与解释**：

- **为什么 `obj.field = x` 用 `dict.insert`？** 因为赋值语义允许「给字典新增一个之前没有的键」。普通 `access` 走 `at_mut`，遇缺失键会报错，无法满足「创建字段」的需求。所以 typst-eval 为「普通赋值 + 字段访问」单独开了一条 `access_dict`+`insert` 的路径，注释也写明：*"it can create the field instead of just modifying it"*。
- **`x += y` 如何取旧值再写回？** 复合赋值（`op = ops::add` 等）一律走通用路径：`lhs.access(vm)` 拿到 `&mut Value`，`std::mem::take` 把旧值 `x` 搬出（此时存储位临时为 `None`），`ops::add(old_x, y)` 算出 `old_x + y`，最后写回同一位置。这样既复用了 `ops::add`，又只持有一次可变借用，符合 Rust 借用规则。

**预期结果**：你能用一句话讲清两条路径的差别——「特殊路径 = 可创建字段（insert），通用路径 = 改写已存在位置（take + op + 写回）」。

> 进阶验证（待本地验证）：尝试 `#let d = (a: 1); #d.b = 2`，应能成功新增键 `b`；再尝试 `#let d = (a: 1); #d.a += 10`，应通过通用路径把 `a` 改成 11。

#### 4.4.5 小练习与答案

**练习 1**：`obj.field += x`（复合字段赋值）会走字段赋值特殊路径吗？为什么？

> **答**：不会。特殊路径要求 `op == Assign`；`+=` 是 `AddAssign`，不满足条件，故走通用路径：`FieldAccess::access` → `at_mut(field)`，要求字段**已存在**，否则报错。因此复合字段赋值不能创建新字段，只能改已有字段。

**练习 2**：`3 = 5` 会怎样求值？

> **答**：`3` 是 `Int` 字面量，不是 `Ident/Parenthesized/FieldAccess/FuncCall`，进入 `Access for Expr` 的默认分支：先 `self.eval(vm)` 求出 `3`，再 `bail!("cannot mutate a temporary value")`。字面量是临时值，无法被赋值。

**练习 3**：`std::mem::take(&mut *location)` 之后、写回之前，`location` 指向的值是什么？为什么这是安全的？

> **答**：是 `Value::None`（`Value` 的 `Default`）。安全是因为 `Value::None` 是合法值，期间若发生重入或错误，位置不会是未定义状态；计算完成后立即被新值覆盖。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「运算符求值全链路」追踪与改造设计。

**任务**：阅读源码后，为下面四个 Typst 表达式分别写出「`Expr::eval` → 具体实现 → 关键函数 → 委托的 `ops::*`」的调用链，并指出各自命中的特殊逻辑：

1. `#(-9223372036854775808)`
2. `#(false and f())`（`f` 有副作用）
3. `#(x += 1)`
4. `#(d.key = 5)`（`d` 是字典，`key` 原不存在）

**参考答案要点**：

1. `Expr::eval → Unary::eval → expr.eval 触发 Int 溢出 → .map_err 命中 overflowing_int_negation_error → 报「cannot write minimum integer manually」，提示 `int.min``。`ops::neg` 不会被调用。
2. `Expr::eval → Binary::eval(And) → apply_binary(ops::and) → 求 lhs 得 false → 命中短路，**不求 f()**，返回 false。`ops::and` 不会被调用。
3. `Expr::eval → Binary::eval(AddAssign) → apply_assignment(ops::add) → 非字段/非 Assign → 通用路径 → x.access → mem::take 取旧 x → ops::add(old, 1) → 写回。`
4. `Expr::eval → Binary::eval(Assign) → apply_assignment(|_,b|Ok(b)) → 命中字段赋值特殊路径（Assign + FieldAccess）→ access_dict → dict.insert("key", 5) → 新建字段 key。`

**延伸思考**：如果把第 4 条改成 `#(d.key += 5)`，调用链会如何变化？（提示：走通用路径，且 `key` 必须已存在，否则 `at_mut` 报错。）请在本地用 typst 编译验证你的预测（待本地验证）。

## 6. 本讲小结

- **纯委托架构**：`Unary` / `Binary` 的 `Eval` 只做编排（求操作数、贴 span、短路、定位可变写），所有数值/比较语义都委托给 `typst_library::foundations::ops`。
- **短路在 typst-eval 层**：`apply_binary` 用精确的 `lhs == Value::Bool(false/true)` 判断对 `and`/`or` 短路，跳过求 rhs；`ops::and`/`ops::or` 因此是无状态纯函数。Typst 无隐式真值。
- **错误改写**：`overflowing_int_negation_error` 借 `.map_err` 在取负溢出处把泛泛的整数溢出改写成「请用 `int.min` / 改用浮点」的精准诊断，体现「信息 + 定位 + 修复 hint」三要素。
- **赋值靠 Access**：`apply_assignment` 先求 rhs，普通赋值到字段走 `access_dict`+`dict.insert`（可创建字段），其余走 `Access` 拿 `&mut Value`。
- **读—改—写惯用法**：复合赋值用 `std::mem::take` 把旧值搬出、计算、写回，避免克隆与借用冲突。
- **可建 vs 只改**：字段赋值特殊路径能创建新字段；`FieldAccess::access` 的 `at_mut` 只能改已存在字段；`Access` 对临时值（字面量等）直接报「cannot mutate a temporary value」。

## 7. 下一步学习建议

本讲把运算符的「求值 + 委托 + 可变写」讲透了，自然的下一步是：

- **[u4-l1 函数调用与参数求值](u4-l1-func-call-args.md)**：`FuncCall::eval` 与本讲的 `Access for FuncCall`（访问器方法可变访问）紧密相关，建议接着学调用机制与 `stacker` 栈保护。
- **[u5-l3 可变访问 Access 与内置方法](u5-l3-access-methods.md)**：深入 `Access` 各实现、`call_method_mut` / `call_method_access`，以及 `access_dict` 对各类类型的可变性边界——本讲只触及了字段赋值，那里会讲透 `push/pop/insert/remove` 等变更方法如何复用 `Access`。

建议继续精读的源码：`src/ops.rs`（通读 135 行即可掌握全部运算符）、`src/access.rs`（理解 `Access` 与 `access_dict` 的完整边界）。
