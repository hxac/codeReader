# Code 与 Math 解析

## 1. 本讲目标

本讲承接 u4-l3（Markup 解析），把解析器的另外两种语法模式——**Code（`#` 后的代码）** 与 **Math（`$...$` 公式）**——讲透。Code 和 Markup 的根本区别在于：Markup 是「平铺的表达式序列」，而 Code 是「带优先级与结合性的表达式语言」，更像我们熟悉的编程语言；Math 则是一套自成体系的「数学排版语言」，它的算符（分式 `/`、上下标 `^`/`_`）与 Code 的二元运算符完全不是一回事。

学完后你应当能够：

1. 说清 `code_exprs` 主循环如何用换行模式 `AtNewline::ContextualContinue` 把「语句」一行行切开，以及 `#` 嵌入代码 `embedded_code_expr` 为何要把 `atomic` 标志置为 `true`；
2. 用**优先级爬升（precedence climbing）** 的视角读懂 `code_expr_prec` 的核心循环，解释 `+`、`*`、字段访问 `.`、函数调用 `()` 如何体现为 CST 的不同嵌套层级；
3. 复述 `code_primary` 这个「主表达式分发器」如何把 `let`/`set`/`show`/`if`/`while`/`for` 等关键字路由到各自的语句解析函数；
4. 区分 **Code 算符**（`BinOp`，如 `+ * and =`）与 **Math 算符**（`math_op`，如 `/ ^ _`），理解它们各自独立的优先级表与包装节点（`Binary` vs `MathFrac`/`MathAttach`）；
5. 看懂 `math_delimited` 如何把成对括号包成 `MathDelimited`，以及 `math_args` 如何解析数学函数的参数列表 `(a, b; c)`。

本讲只精读一个核心文件 `src/parser.rs`，但会交叉引用 `src/ast.rs`（算符优先级与结合性表）与 `src/set.rs`（`CODE_EXPR`/`BINARY_OP` 等预定义集合）来补全因果链。

## 2. 前置知识

进入本讲前，请确认你已掌握以下概念（均在前置讲义中讲过）：

- **Marker + wrap 事件式解析**：解析函数先把 token 推进扁平的 `nodes` 向量，用 `marker()` 记下位置戳，子树解析完再用 `wrap(m, kind)` 事后打包成内部节点，使函数不必在入口承诺子树边界（u4-l2）。
- **单 token 前瞻原语**：`current()`/`at()`/`at_set()` 查询当前 token，`eat()` 消费它；`directly_at()` 要求「前面没有 trivia」（即紧贴），`eat_if()` 尝试消费并返回布尔（u4-l2）。
- **`AtNewline` 换行模式**：parser 用 `with_nl_mode(mode, closure)` 临时规定「遇到换行时是否叫停」，由 `lex()` 把换行伪造成 `End` 来停止上层循环；`ContextualContinue` 表示「只有遇到 `else`/`.` 这种续行 token 才不停」（u4-l2、u4-l3）。
- **模式切换 `enter_modes`**：`#` 切到 Code、`$` 切到 Math、`[` 切到 Markup，切换时会重置 lexer 游标重 lex（u4-l3）。
- **`SyntaxSet` 位集**：`set::CODE_EXPR`、`set::BINARY_OP` 等是编译期常量集合，`at_set(set)` 判断「当前 token 是否属于该类」（u2-l3）。

本讲会反复用到一句话：**Code 解析的本质是「先吃一个主表达式（primary），再在一个循环里不断尝试贴上后缀算符（二元运算 / 函数调用 / 字段访问），用优先级决定右操作数递归多深」；而 Math 解析用的是另一套算符表。**

## 3. 本讲源码地图

本讲以 `src/parser.rs` 为主战场，交叉引用另两个文件补全算符表与集合定义：

| 文件 | 本讲涉及内容 |
| --- | --- |
| `src/parser.rs` | Code 侧：`code`/`code_exprs`/`embedded_code_expr`/`code_expr`/`code_expr_prec`/`code_primary`/`args`；语句：`let_binding`/`set_rule`/`show_rule`/`contextual`/`conditional`/`while_loop`/`for_loop`；Math 侧：`math`/`math_exprs`/`math_expr`/`math_expr_prec`/`math_op`/`math_delimited`/`math_unparen`/`math_args`/`math_arg`；`AtNewline` 枚举 |
| `src/ast.rs` | `BinOp::precedence`/`BinOp::assoc`（Code 二元算符优先级与结合性表）、`UnOp::precedence`（一元算符优先级）、`Assoc` 枚举（`Left`/`Right`） |
| `src/set.rs` | `STMT`（语句起始 token）、`CODE_EXPR`/`ATOMIC_CODE_EXPR`（可起始表达式的 token）、`UNARY_OP`/`BINARY_OP`（一元/二元算符 token）、`MATH_EXPR`（可起始数学表达式的 token） |

> 提醒：以下所有永久链接的 HEAD 均为 `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

## 4. 核心概念与源码讲解

### 4.1 Code 解析骨架：从 `code_exprs` 到 `code_expr`

#### 4.1.1 概念说明

在 Typst 里，代码出现在两个地方：一是**代码块** `{ ... }` 内部（`SyntaxMode::Code`），二是**正文里以 `#` 开头的嵌入代码**（如 `#let x = 1`）。两者都由同一套「Code 解析器」处理，区别只在于：代码块里一条语句之后只要换行或分号就能分隔；而 `#` 嵌入代码默认只允许**一条表达式**，除非这条表达式本身是「语句」（`let`/`set`/`show` 等）。

这就引出了一个关键标志 **`atomic`**（原子）：`embedded_code_expr` 解析 `#` 后的内容时会把 `atomic` 设为 `true`，它的语义是「这个表达式后面不能再贴二元算符、一元前缀等会改变其边界的算符」。换句话说，`#a + b` 在 Typst 里不是一个整体表达式——`#` 只「抓走」`a`，` + b` 留在 markup 里当文本（这会触发 `expected("semicolon or line break")` 报错）。但如果 `#` 后面是语句（如 `#let f(x) = x + 1`），语句内部会自己调用**非 atomic** 的 `code_expr` 来解析等号右边，于是 `x + 1` 能被完整解析。这正是 `atomic` 设计的精妙之处。

#### 4.1.2 核心流程

Code 解析的整体调用链是分层的：

```
parse_code (顶层 Code)
  └─ code_exprs(stop_set)            # 主循环：一行行吃语句
       └─ code_expr(p)               # 解析一条表达式
            └─ code_expr_prec(atomic=false, min_prec=0)   # 带优先级的解析

embedded_code_expr (# 嵌入)          # markup/math 里的 #...
  └─ code_expr_prec(atomic=true, min_prec=0)
       └─ code_primary(...)          # 主表达式分发（可能进入 let_binding 等）
```

`code_exprs` 主循环的职责很单纯：

1. 做深度检查防栈溢出。
2. 只要当前 token 不在停止集合 `stop_set` 内，就用 `with_nl_mode(ContextualContinue, ...)` 包裹一次「解析一条语句」的过程——`ContextualContinue` 让换行**通常**会终止当前语句，唯独遇到 `else`（接 `if`）或 `.`（接字段访问续行）时不终止。
3. 每条语句解析后，若既没到停止条件、也没吃到分号，就报 `expected("semicolon or line break")`，并对「在代码里写 `<label>`」这种典型错误给出 hint。

#### 4.1.3 源码精读

先看最外层的 `code` 与主循环 `code_exprs`：

[`src/parser.rs:549-576`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L549-L576) —— `code` 把一串表达式包成 `Code` 节点；`code_exprs` 是语句主循环。注意第 561 行用 `with_nl_mode(AtNewline::ContextualContinue, ...)` 控制「换行终止语句」的语义，第 567 行在缺少分号/换行时报错。

`ContextualContinue` 的判定逻辑只有几行，它决定了哪些 token 能「跨过换行续接」：

[`src/parser.rs:1579-1583`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1579-L1583) —— `ContextualContinue` 只在 `kind` 是 `Else` 或 `Dot` 时「继续」（返回 `false` 表示不叫停），其余一律叫停。这解释了为何 `if x { } \n else { }` 能跨行续接 `else`，而 `a \n + b` 不能。

再看 `#` 嵌入代码入口 `embedded_code_expr`，它揭示了 `atomic` 与 `stmt` 的关系：

[`src/parser.rs:579-598`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579-L598) —— `embedded_code_expr`。第 587 行 `let stmt = p.at_set(set::STMT)` 判断 `#` 后是否是语句；第 588 行以 `atomic=true` 调用 `code_expr_prec`；第 591–596 行规定：**只有当 `#` 后是语句（`stmt`）或紧贴分号时**才允许出现分号，否则若是语句且没正常结束就报错。这就是「`#` 通常只吃一条表达式」的来源。

`STMT` 集合定义了哪些关键字算「语句」：

[`src/set.rs:62-63`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L62-L63) —— `STMT = { Let, Set, Show, Import, Include, Return }`。注意 `if`/`while`/`for` **不在** `STMT` 里——Typst 把它们视为**表达式**而非语句，所以 `#if true { 1 }` 后面同样需要分号或换行（由第 594 行的 `stmt` 为 `false` 时走另一条判定）。

#### 4.1.4 代码实践

**目标**：直观感受 `atomic` 标志如何影响 `#` 嵌入代码的解析边界。

**操作步骤**：

1. 在仓库内运行 `cargo test -p typst-syntax --doc` 确认编译无误（命令待本地验证）。
2. 阅读 `embedded_code_expr`（[parser.rs:579-598](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579-L598)），对照下面两段输入预测解析结果。
3. 比较 `#let x = 1 + 2`（`#` 后是语句 `let`）与 `#1 + 2`（`#` 后是普通表达式 `1`）。

**需要观察的现象**：

- `#let x = 1 + 2`：`let_binding` 内部用非 atomic 的 `code_expr` 解析等号右边，`1 + 2` 被完整解析成一个 `Binary` 节点。
- `#1 + 2`：`#` 以 atomic 模式只抓走 `1`，随后 ` + 2` 在 markup 里成了悬空内容，parser 报 `expected("semicolon or line break")`。

**预期结果**：第二条会产出 `Error` 节点。这是因为 `atomic=true` 时 `code_expr_prec` 会在第 637–639 行（见 4.2.3）提前 `break`，不再尝试二元算符 `+`。

> 若无法本地运行，明确标注「待本地验证」，但上面的因果链由源码逻辑保证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `#if true { 1 } else { 2 }` 能跨行写成两段而不报「缺分号」？

**参考答案**：因为 `code_exprs` 用 `ContextualContinue` 模式解析语句，`else` 是续行 token（[parser.rs:1581](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1581)），换行不会叫停，`else` 会被同一个 `conditional`（见 4.3）接住。

**练习 2**：`STMT` 集合里没有 `If`/`For`/`While`，这对 `#if ... { }` 的解析意味着什么？

**参考答案**：意味着 `if` 是表达式。`embedded_code_expr` 里 `stmt=false`，于是第 591 行 `semi` 只有在「紧贴分号」时才为真；若 `#if ... { }` 后还有别的 token 又没分号/换行，就会触发 `expected("semicolon or line break")`。

---

### 4.2 优先级爬升：`code_expr_prec` 与 `code_primary`

#### 4.2.1 概念说明

如何让 `x + 1 * 2` 解析成 `x + (1 * 2)` 而不是 `(x + 1) * 2`？经典做法是**优先级爬升（precedence climbing）**，它是 Pratt 解析器的一种紧凑写法。核心思想是：

> 解析一个表达式时，**先读入左操作数（primary）**，然后**循环地看后面有没有算符**。如果算符的优先级 `prec` 不低于当前允许的最小优先级 `min_prec`，就吃掉它，并**以一个更高的 `min_prec` 递归解析右操作数**——这个「提高门槛」的幅度由**结合性**决定：左结合则门槛 `prec+1`（阻止同级算符继续贴到右操作数上，从而左括号化），右结合则门槛 `prec`（允许同级贴到右边，右括号化）。

Typst 的 Code 用一个函数 `code_expr_prec(p, atomic, min_prec)` 实现这一思想。它对应三类「后缀」会贴到当前表达式上：

1. **函数调用**：紧贴的 `(` 或 `[` → 包成 `FuncCall`；
2. **字段访问**：紧贴的 `.` 后跟标识符 → 包成 `FieldAccess`；
3. **二元算符**：`+ - * / and or = == ...` 或特殊写法 `not in` → 包成 `Binary`。

而 `atomic=true` 会**关闭**前两类之外的大部分后缀（一元前缀、二元算符），把表达式「钉死」成单个原子，这正是 `#` 嵌入代码需要的语义。

#### 4.2.2 核心流程

`code_expr_prec` 的骨架（伪代码）：

```
fn code_expr_prec(p, atomic, min_prec):
    m = marker()
    if 当前是一元算符(Plus/Minus/Not) 且 非 atomic:
        eat 算符
        code_expr_prec(p, atomic, 该一元算符的precedence)   # 递归取操作数
        wrap(m, Unary)
    else:
        code_primary(p, atomic)          # 先吃一个主表达式

    loop:
        # (1) 函数调用：紧贴的 ( 或 [
        if directly_at(LeftParen) 或 directly_at(LeftBracket):
            args(p); wrap(m, FuncCall); continue

        # (2) 字段访问：紧贴的 . 且下一个 token 是 Ident
        at_field = directly_at(Dot) 且 lexer前瞻next()==Ident
        if atomic 且 非 at_field: break     # atomic 模式到此为止

        if eat_if(Dot):
            expect(Ident); wrap(m, FieldAccess); continue

        # (3) 二元算符
        binop = at_set(BINARY_OP) ? BinOp::from_kind(current)
              : (min_prec <= NotIn.prec 且 eat_if(Not)) ? (at(In) ? NotIn : 报错; break)
              : None
        if binop 存在:
            prec = binop.precedence()
            if prec < min_prec: break          # 优先级不够，留给上层
            # 结合性调整门槛
            prec = (Left ? prec+1 : prec)
            eat 算符
            code_expr_prec(p, false, prec)     # 递归取右操作数，抬高门槛
            wrap(m, Binary); continue
        break
```

两个关键点：

- **「抬高门槛」实现结合性**：左结合算符（如 `+`）递归时传 `prec+1`，于是右操作数不再吃同级 `+`，`a + b + c` 就变成 `(a+b) + c`；右结合算符（如 `=`）传 `prec`，于是 `a = b = c` 变成 `a = (b = c)`。
- **`not in` 的特殊处理**：`not` 单独不是 `BINARY_OP` 集合成员，必须紧跟 `in` 才合成 `NotIn` 算符（[parser.rs:649-656](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L649-L656)）。

二元算符的优先级与结合性表由 `ast.rs` 提供（节选）：

| 算符 | 优先级 | 结合性 |
| --- | --- | --- |
| `*` `/` (Mul/Div) | 6 | Left |
| `+` `-` (Add/Sub) | 5 | Left |
| `==` `!=` `<` `<=` `>` `>=` `in` `not in` | 4 | Left |
| `and` | 3 | Left |
| `or` | 2 | Left |
| `=` `+=` `-=` `*=` `/=` (赋值) | 1 | Right |

数值越大优先级越高、绑得越紧。一元算符（`+ - not`）的优先级分别是 `Pos/Neg=7`、`Not=4`。

#### 4.2.3 源码精读

`code_expr_prec` 是本讲最核心的函数，逐段读：

[`src/parser.rs:606-624`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L606-L624) —— 一元前缀处理与主表达式入口。第 610 行 `at_set(set::UNARY_OP)` 判断当前是否 `+`/`-`/`not`；若 `atomic` 则不给用一元前缀（第 617 行 `unexpected()` + hint「请把整个表达式用括号括起来」），否则 eat 后递归取操作数再 `wrap(m, Unary)`。注意第 614 行递归用的是 `op.precedence()` 作为门槛，这保证 `-a * b` 解析为 `(-a) * b` 还是 `-(a*b)` 取决于一元与乘法的优先级对比。

[`src/parser.rs:626-679`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L626-L679) —— 后缀循环。三段分别对应函数调用（627–632）、字段访问（634–645）、二元算符（647–676）。第 662–670 行是优先级爬升的灵魂：`if prec < min_prec { break; }` 决定「优先级不够就交还上层」，随后 `match op.assoc()` 按结合性抬高递归门槛。

二元算符的优先级与结合性定义在 ast.rs：

[`src/ast.rs:1930-1952`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1930-L1952) —— `BinOp::precedence`，穷举每个二元算符的优先级数值。

[`src/ast.rs:1955-1977`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1955-L1977) —— `BinOp::assoc`，穷举结合性。赋值类为 `Right`，其余算术/比较/逻辑为 `Left`。

再看主表达式分发器 `code_primary`，它决定「一个原子表达式从哪些 token 开始」：

[`src/parser.rs:685-745`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L685-L745) —— `code_primary`。这是一张大型 `match`，把每种「能起始表达式」的 token 路由到对应处理：标识符（可能组成闭包 `x => y`）、`{` 代码块、`[` 内容块、`(` 元组/字典/括号、`$` 公式、各类语句关键字（`Let`/`Set`/`Show`/...）、字面量（`Int`/`Float`/`Str`/...）。第 690 行 `if !atomic && p.at(Arrow)` 是闭包识别——`atomic` 模式下不允许裸标识符接 `=>`。

`code_primary` 与 `set::CODE_EXPR` 的对应关系：

[`src/set.rs:95-126`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L95-L126) —— `CODE_EXPR = ATOMIC_CODE_EXPR ∪ UNARY_OP ∪ {Underscore}`，`ATOMIC_CODE_EXPR` 列出全部可起始原子的 token。`code_primary` 的 `match` 分支集合与这张表是同构的：表里列出的 token 在 `match` 里都有归宿，表外的 token 落入 `_ => p.expected("expression")`。

#### 4.2.4 代码实践

**目标**：亲眼看到 `+` 与 `*` 的优先级差异如何体现为 CST 的嵌套层级。这正是本讲规格里要求的核心实践。

**操作步骤**：

1. 阅读下面的最小 Rust 程序（**示例代码**，非项目原有）。

```rust
// 示例代码：解析 + 与 * 的优先级（需把 typst-syntax 加入依赖）
use typst_syntax::parse;

fn main() {
    // 解析嵌入在 markup 里的代码：#x + 1 * 2 不行（atomic），
    // 改用代码块内或直接用 parse_code 解析裸表达式。
    let root = typst_syntax::parse_code("x + 1 * 2");
    // 遍历所有 Binary 节点
    for node in root.descendants() {
        if node.kind() == typst_syntax::SyntaxKind::Binary {
            println!("Binary: text={:?}", node.text());
        }
    }
}
```

2. 在仓库外新建一个小 crate（或在 `src/parser.rs` 的 `#[cfg(test)]` 模块里临时加一个测试），加入 `typst-syntax` 依赖后运行（命令与输出格式待本地验证）。

**需要观察的现象**：会打印出**两个** `Binary` 节点——一个覆盖整段 `x + 1 * 2`，另一个覆盖子串 `1 * 2`。

**预期结果**：CST 结构为

```
Binary
├─ Ident "x"
├─ Plus "+"
└─ Binary          ← 这个内层就是 1 * 2，嵌套更深 ⇒ 优先级更高 ⇒ 结合更紧
   ├─ Int "1"
   ├─ Star "*"
   └─ Int "2"
```

**为什么**：解析 `x + 1 * 2` 时，外层 `code_expr_prec(min_prec=0)` 先吃 `x`，见到 `+`（prec=5 ≥ 0）按左结合传门槛 `6` 递归解析右操作数；内层 `code_expr_prec(min_prec=6)` 吃 `1`，见到 `*`（prec=6 ≥ 6）继续，于是 `*` 被吸收进内层，`+` 留在外层——所以 `*` 嵌得更深。这正是「优先级高 ⇒ 嵌套深 ⇒ 结合紧」的直接体现。

> 若暂无独立 crate，可在仓库内用 `cargo test -p typst-syntax` 跑现有用例，或在 parser.rs 测试模块里加一个断言上述结构的测试（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`a = b = c` 会解析成怎样的嵌套？为什么？

**参考答案**：解析成 `a = (b = c)`，即外层 `Binary` 的右操作数是内层 `Binary`。因为 `=` 是 `Right` 结合（[ast.rs:1971](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L1971)），递归门槛传 `prec`（=1）而非 `prec+1`，所以第二个 `=` 能贴进右操作数。

**练习 2**：`#a.b` 能解析（`a.b` 是字段访问），但 `#a + b` 不能——同样是 `atomic=true`，为何结果不同？

**参考答案**：`code_expr_prec` 的循环里专门为字段访问开了口子（[parser.rs:634-637](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L634-L637)）：`atomic && !at_field_or_method` 才 `break`。所以 atomic 模式**允许**字段访问续接，却**禁止**二元算符与函数调用之外的续接。

---

### 4.3 语句解析：`let` / `set` / `show` / `if` / `while` / `for`

#### 4.3.1 概念说明

Typst 的 Code 既有「表达式」也有「语句」，但如前所述，`if`/`while`/`for` 在 Typst 里被设计成**表达式**（可以出现在表达式位置、可作为 `#` 的内容），而 `let`/`set`/`show`/`import`/`include`/`return` 才是真正的**语句**（`STMT` 集合成员）。所有这些构造都由 `code_primary` 里的 `match` 分发到各自的解析函数。

这些函数有一个高度统一的写法模式：

```
fn xxx(p):
    m = marker()
    assert(关键字 token)        # 吃掉开头关键字
    ... 解析各组成部分 ...
    wrap(m, 对应的 SyntaxKind)
```

即「先记位置戳、吃关键字、解析部件、最后打包」。理解了这个套路，阅读任何一个语句解析函数都很轻松。

#### 4.3.2 核心流程

以几个代表性语句为例，它们的解析步骤：

- **`let` 绑定**（`let f(x) = body` 或 `let (a, b) = ...` 或 `let x`）：
  1. 吃 `let`；
  2. 尝试吃 `Ident`：成功且紧接 `(` → 解析参数列表 `params`，标记为闭包 `closure=true`；否则用 `pattern` 解析（可能是解构，标记 `other=true`）；
  3. 视情况 `expect` 或 `eat_if` 等号 `=`（闭包/解构时强制要求 `=`）；
  4. 若有等号，用 `code_expr` 解析右边表达式；
  5. 若是闭包，把 `Ident + params` 包成 `Closure`；最后整体包成 `LetBinding`。

- **`set` 规则**（`set text(red)`、`set list(indent: 2em)`）：吃 `set` → 吃目标标识符（可能带 `.field` 字段访问链）→ 解析参数 `args` → 可选 `if 条件` → 包 `SetRule`。

- **`show` 规则**（`show heading: it => emph(it.body)` 或裸 `show body`）：吃 `show` → 若非紧接 `:` 则先解析一个选择器表达式 → 期望 `:` → 解析替换体表达式 → 包 `ShowRule`。

- **`if` 条件**（`if cond { a } else { b }`）：吃 `if` → 解析条件表达式 → 解析一个 block → 可选 `else`（若 `else` 后又是 `if` 则递归，形成 else-if 链）→ 包 `Conditional`。

- **`while`/`for`**：吃关键字 → `for` 还要先解析循环变量 `pattern` 与 `in` → 解析条件/可迭代表达式 → 解析 block → 包 `WhileLoop`/`ForLoop`。

#### 4.3.3 源码精读

先看 `let_binding`，它展示了「闭包语法糖」如何在解析阶段就组装出来：

[`src/parser.rs:789-817`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L789-L817) —— `let_binding`。第 797–805 行区分两种绑定形式：`let f(...) = ...`（标识符 + 参数，`closure=true`）与 `let (a,b) = ...` 或 `let x`（`pattern`）。第 807 行用函数指针 `f` 选择「闭包/解构时强制要求 `=`，否则可选」——`Parser::expect` 与 `Parser::eat_if` 是两个签名相同的方法。第 812–814 行把 `f(x)` 那部分再包成 `Closure`，使 `let f(x) = body` 与 `let f = (x) => body` 在 CST 层面等价。

接着看 `set_rule` 与 `show_rule`：

[`src/parser.rs:820-836`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L820-L836) —— `set_rule`。第 826–829 行的 `while eat_if(Dot)` 循环把 `text.lang` 这样的字段访问链逐步包成 `FieldAccess`，使 `set text.lang(..)` 这类带点号的目标也能解析。第 832–834 行支持 `set ... if cond` 条件设定。

[`src/parser.rs:839-855`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L839-L855) —— `show_rule`。第 844 行 `if !p.at(Colon)` 决定有没有选择器（裸 `show [ ... ]` 没有选择器）；第 848 行 `eat_if(Colon)` 失败时用 `expected_at(m2, "colon")` 在合适位置插入错误（而不是在当前 token 处），便于报错定位。

再看控制流 `conditional` / `while_loop` / `for_loop`，三者结构高度相似：

[`src/parser.rs:866-879`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L866-L879) —— `conditional`。第 870 行 `code_expr` 解析条件，第 871 行 `block(p)` 解析 then 分支，第 872 行 `if at(If)` 实现 `else if` 链的递归。

[`src/parser.rs:891-911`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L891-L911) —— `for_loop`。第 896 行 `pattern` 解析循环变量；第 898–905 行对 `for a, b in ...` 这种错误写法给出 hint「解构模式必须用括号包裹」；第 907 行 `expect(In)` 吃掉 `in`。

注意这三个函数都用 `block(p)`（[parser.rs:758-764](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L758-L764)）来吃 then/else/loop 体——`block` 会根据当前是 `[` 还是 `{` 分派到内容块或代码块，所以 `if x { 1 }` 与 `if x [Hi]` 都合法。

#### 4.3.4 代码实践

**目标**：跟踪 `#let f(x) = x + 1` 的解析，验证闭包语法糖在 CST 里的形态。

**操作步骤**：

1. 阅读 `let_binding`（[parser.rs:789-817](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L789-L817)），按 4.3.2 的步骤在纸上模拟 `#let f(x) = x + 1` 的解析。
2. 想象（或在本地打印）它的 CST。

**需要观察的现象**：`LetBinding` 的子节点里应该有一个 `Closure` 子树，`Closure` 内含 `Params`（即 `f` + `(x)`）与函数体 `x + 1`。

**预期结果**：CST 大致为

```
LetBinding
├─ Let "#let"... 实际为关键字 token "let"
├─ Closure
│  ├─ Params
│  │  ├─ Ident "f"
│  │  └─ Params "(x)"
│  └─ Binary "x + 1"
└─ (无 trailing)
```

这与手写 `(x) => x + 1` 赋给 `f` 等价，体现了「let + 参数列表 = 闭包」的语法糖在解析期就完成。

> 确切的 trivia（空白）归属与节点文本以本地 `Debug` 打印为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`let x`（没有等号、没有初值）合法吗？源码依据在哪？

**参考答案**：合法。`let_binding` 第 807 行 `f = if closure || other { Parser::expect } else { Parser::eat_if }`，当只是普通标识符（非闭包、非解构）时用 `eat_if`，等号可有可无；没有等号就不解析右边，得到一个「未初始化」的 `LetBinding`。

**练习 2**：为何 `for a, b in y { }` 会报错并提示「必须用括号包裹」？

**参考答案**：`for_loop` 第 898 行检测到逗号 `Comma`，认为用户想解构多个变量，但解构模式必须写成 `(a, b)`，于是第 901 行 `hint("destructuring patterns must be wrapped in parentheses")`（[parser.rs:901](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L901)）。

---

### 4.4 Math 表达式优先级：`math_expr_prec` 与 `math_op`

#### 4.4.1 概念说明

Math 模式（`$...$`）是一套**独立**的表达式语言，它的算符与 Code 完全不同：

- Code 的 `+ - * /` 是二元算符，产生 `Binary` 节点；
- Math 里，`+`、`*` 这些字符大多只是**普通数学文本**（`MathText`/`MathShorthand`），并不构成算符树。真正被当作「算符」处理的是：
  - `/` —— 分式，产生 `MathFrac`（把 `a/b` 排版成竖式分数）；
  - `^`（Hat）—— 上标，`_`（Underscore）—— 下标，产生 `MathAttach`；
  - 撇号 `MathPrimes`（无 trivia 时）—— 后缀附着；
  - `!`（Bang，无 trivia 时）—— 后缀，转成 `MathText` 包进 `Math`。

所以 `$a + b$` 在 CST 里是**三个并列的数学表达式**（`a`、`+`、`b`），没有嵌套；而 `$a / b$` 是**一个** `MathFrac` 节点。这是初学 Typst 数学解析时最容易混淆的点。

Math 同样用优先级爬升，但它有自己的一张优先级表（由 `math_op` 返回），也有自己的结合性（`ast::Assoc`）。此外 Math 有一个 Code 没有的概念——**「可续接」(continuable)**：某些 token（字母、标识符）后面如果**紧贴**着 `(` 或 `{`，会被当作隐式函数调用而分组。

#### 4.4.2 核心流程

`math_expr_prec(p, min_prec, stop_set)` 的骨架：

```
fn math_expr_prec(p, min_prec, stop_set):
    m = marker()
    continuable = false
    match 当前 token:                       # 先吃一个数学「主表达式」
        Hash      => embedded_code_expr(p)  # $#(1+2)$ 嵌入代码
        MathIdent/FieldAccess => eat; 若紧贴 ( 则 math_args + 包 MathCall
        LeftBrace/LeftParen => math_delimited
        Root      => eat; math_expr_prec(MATH_ROOT_PREC); 包 MathRoot
        MathText  => eat; continuable = 是否纯字母
        ... 其它 (撇号、转义、shorthand 等)
        _ => expected("expression")

    # 「可续接」token 紧贴 ( 或 { ⇒ 隐式函数调用分组
    if continuable 且 紧贴 ({ ⇒ math_delimited; 包 Math

    # 优先级爬升循环：贴中缀/后缀算符
    while 当前是算符 且 prec >= min_prec:
        eat 算符
        若是中缀(有 rhs): 按结合性抬高门槛递归 math_expr_prec
        # 附着算符(^ _ 撇号)可链式组合成一个 MathAttach
        while 当前在 chain_set: 吃掉并继续附着
        wrap(m, 对应 wrapper)
```

Math 算符的优先级表（由 `math_op` 与常量给出）：

| 算符 | 包装节点 | 中缀/后缀 | 优先级 | 结合性 |
| --- | --- | --- | --- | --- |
| `/` (Slash) | `MathFrac` | 中缀 | 1 | Left |
| `_` (Underscore) | `MathAttach` | 中缀 | 2 | Right |
| `^` (Hat) | `MathAttach` | 中缀 | 2 | Right |
| 撇号 `MathPrimes`（无 trivia） | `MathAttach` | 后缀 | 2 | — |
| `!` (Bang，无 trivia) | `Math` | 后缀 | 3 | — |
| 函数调用 `f(...)` | `MathCall` | — | 2 (`MATH_FUNC_PREC`) | — |
| 根号 `√` (Root) | `MathRoot` | 前缀 | 2 (`MATH_ROOT_PREC`) | — |

#### 4.4.3 源码精读

先看顶层 `math`、`math_exprs` 与单表达式入口 `math_expr`：

[`src/parser.rs:236-264`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L236-L264) —— `math` 把一串数学表达式包成 `Math`；`math_exprs` 是循环（与 `markup_exprs`/`code_exprs` 同构）；`math_expr` 调 `math_expr_prec(p, 0, syntax_set!())`，初始门槛 0、空 stop 集。

数学表达式主函数 `math_expr_prec` 较长，分段读。先是「主表达式」分发：

[`src/parser.rs:268-338`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L268-L338) —— `math_expr_prec` 的 match 段。第 274 行 `Hash` 分支处理嵌入代码 `$#x$`；第 277–286 行处理标识符/字段访问，若紧接 `(` 且 `MATH_FUNC_PREC >= min_prec` 则 `math_args` 并包成 `MathCall`；第 288–290 行 `{`/`(` 走 `math_delimited`；第 317–323 行 `Root`（根号 `√`）递归取操作数并包 `MathRoot`。第 331–338 行是「可续接」隐式调用：如 `f{x}` 中 `f` 是字母、紧贴 `{`，于是整体包成 `Math`。

接着是优先级爬升循环：

[`src/parser.rs:340-397`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L340-L397) —— 后缀/中缀循环。第 345 行 `math_op(op_kind, had_trivia)` 取算符信息，第 346 行 `prec >= min_prec` 是门槛判定。第 371–379 行按结合性（`Assoc::Left → prec+1`，`Right → prec`）抬高门槛递归取右操作数。第 385–392 行的链式循环把 `a^b_c` 这类多个附着算符合并进**一个** `MathAttach` 节点。

算符优先级表与常量：

[`src/parser.rs:399-417`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L399-L417) —— `MATH_FUNC_PREC=2`、`MATH_ROOT_PREC=2`，以及 `math_op` 把 `Slash/Underscore/Hat/MathPrimes/Bang` 映射到 `(wrapper, assoc, prec)`。注意撇号与 `!` 都带 `if !had_trivia` 条件——**紧贴**才算算符，前面有空格就退化成普通文本。

`MATH_EXPR` 集合定义了哪些 token 能起始一个数学表达式：

[`src/set.rs:65-89`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L65-L89) —— `MATH_EXPR` 集合。`math_exprs` 第 250 行用它判断当前 token 能否起始表达式，不能则 `unexpected()`。

#### 4.4.4 代码实践

**目标**：对比 `$a / b / c$`（左结合分式）与 `$x^2_3$`（链式附着）的 CST，体会 Math 算符的优先级与结合性。

**操作步骤**：

1. 阅读 `math_op`（[parser.rs:404-417](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L404-L417)）与爬升循环（[parser.rs:340-397](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L340-L397)）。
2. 用 `parse_math("a / b / c")`（**示例代码**）打印 `MathFrac` 节点。

**需要观察的现象**：`a / b / c` 会出现两个 `MathFrac`，且是左结合嵌套 `(a/b)/c`——外层 `MathFrac` 的左操作数是内层 `MathFrac`。

**预期结果**：

```
MathFrac "(a/b)/c"
├─ MathFrac "a/b"
│  ├─ ... "a"
│  ├─ Slash "/"
│  └─ ... "b"
├─ Slash "/"
└─ ... "c"
```

**为什么左结合**：`/` 是 `Left`（[parser.rs:409](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L409)），递归门槛传 `prec+1=2`，于是内层解析 `b` 时见到第二个 `/`（prec=1 < 2）就停住，第二个 `/` 只能回到外层贴上，形成 `(a/b)/c`。

> 对比：Code 的 `+ - * /` 与 Math 的 `/` 是**两套独立算符**——Code 的 `*` 不会出现在 Math 算符表里，`$a * b$` 里 `*` 只是 `MathShorthand` 文本。

#### 4.4.5 小练习与答案

**练习 1**：`$a + b$` 会产生 `Binary` 节点吗？为什么？

**参考答案**：不会。Math 的 `+` 不在 `math_op` 表里，它被 lexer 切成 `MathShorthand`，在 `math_expr_prec` 的 match 里走第 310 行 `p.eat()`，作为独立的数学表达式序列存在，不构成算符树。

**练习 2**：`$a / (b)$` 与 `$a / b$` 的 CST 一样吗？

**参考答案**：不一样。`a / (b)` 里右操作数 `(b)` 是 `MathDelimited`；但第 366–368 行专门对 `MathFrac` 的左操作数调用 `math_unparen`，**移除**多余括号。不过这里的 `math_unparen(m)` 作用在**左**操作数（`a`）上，对右操作数 `(b)` 的处理见 4.5 的 `math_unparen` 调用（第 378 行），同样会把 `(b)` 的括号还原成普通 `Math`。具体是否完全相等取决于 `math_unparen` 是否命中圆括号判定（待本地验证）。

---

### 4.5 Math 定界与参数：`math_delimited` / `math_unparen` / `math_args`

#### 4.5.1 概念说明

Math 里有三种「成对」结构需要专门处理：

1. **定界表达式** `math_delimited`：`[x + y]`、`(a, b)`、`{x}`。注意 lexer 在 Math 模式下把 `{`/`}`、`(`/`)`、`[`/`]` 都切成普通的 `LeftBrace`/`RightBrace` 等 token，parser 要把它们**重新解释**回数学语义——大多数情况下括号被转成 `MathText`（当成普通字符），只有当成对出现且中间是表达式时才包成 `MathDelimited`。
2. **去括号 `math_unparen`**：在分式 `/` 的操作数上，Typst 会把 `(x)` 这种「包了一层圆括号」的 `MathDelimited` 还原成普通 `Math`，使排版上 `a/(b)` 和 `a/b` 看起来一致（不让多余括号出现在分数里）。
3. **函数参数 `math_args`**：数学函数调用 `vec(x, y)`、`abs(x)` 的参数列表 `(a, b; c)`，结构与 Code 的 `args` 类似，但参数本身是数学表达式序列，且支持命名参数 `thickness: #12pt` 与展开参数 `..args`。

#### 4.5.2 核心流程

- `math_delimited`：
  1. 把开括号转成 `MathText`（或特例 `|]`/`[|` 转 `MathShorthand`）并吃掉；
  2. 递归 `math_exprs` 解析中间内容（stop 集含右括号）；
  3. 若遇到匹配的右括号，把中间内容包成 `Math`，吃掉右括号，整体包成 `MathDelimited`；若无右括号，退化为普通 `Math` 序列。

- `math_unparen(m)`：检查位置 `m` 处的节点是不是「用圆括号 `(` `)` 定界的 `MathDelimited`」，若是则把这对括号还原成普通 `LeftParen`/`RightParen` token、节点降级为 `Math`——即「去掉一层圆括号包装」。

- `math_args`：
  1. 吃 `(`；
  2. 循环解析 `math_arg`，用 `,` 或 `;` 分隔（`;` 用于矩阵的行分隔）；
  3. 期望 `)`，整体包成 `MathArgs`。

- `math_arg`：支持三种形态——展开 `..args`（`Spread`）、命名 `name: value`（`Named`，借助 lexer 的 `maybe_math_named_arg` 识别）、普通位置参数（一段数学表达式序列）。

#### 4.5.3 源码精读

`math_delimited`：

[`src/parser.rs:435-456`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L435-L456) —— `math_delimited`。第 437–441 行把开括号 `convert_and_eat(MathText)`（特例 `[|` 转 `MathShorthand`）；第 443 行 `math_exprs` 解析中间内容；第 444–451 行若有匹配右括号则包 `MathDelimited`，否则第 453–455 行退化为 `Math`。注意它把 lexer 产出的 `LeftBrace`/`LeftParen` 「重解释」为 `MathText`，这就是 Math 模式下括号语义的来源。

`math_unparen`：

[`src/parser.rs:460-475`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L460-L475) —— `math_unparen`。第 466–469 行判断「首尾叶子文本分别是 `(` 和 `)`」才动手，把这对括号的 kind 改回 `LeftParen`/`RightParen`，并把节点 kind 从 `MathDelimited` 改回 `Math`。它被 `math_expr_prec` 在分式（第 367 行）与各操作数（第 378、390 行）处调用，目的是消除「被圆括号包了一层」的排版噪音。

`math_args` 与 `math_arg`：

[`src/parser.rs:478-494`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L478-L494) —— `math_args`。结构与 Code 的 `args` 类似：循环解析 `math_arg`，用 `,`/`;` 分隔，期望 `)`。

[`src/parser.rs:497-546`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L497-L546) —— `math_arg`。第 503–507 行处理展开参数（借助 `lexer.maybe_math_spread_arg`）；第 508–518 行处理命名参数（借助 `lexer.maybe_math_named_arg`，并用 `seen` 集合查重，重复则转 `Error`）。第 539–541 行有一个重要细节：**当参数解析出 0 个或多个表达式时才包 `Math`，恰好 1 个时不包**——注释解释若包了会把 `#12pt` 这种非 content 类型强制变成 content，改变语义。

对比 Code 的 `args`：

[`src/parser.rs:1196-1233`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1196-L1233) —— Code 的 `args`。它同时支持 `(` 参数列表与紧贴的 `[` 内容块（第 1228 行 `while directly_at(LeftBracket)`），而 `math_args` 只处理圆括号。函数调用 `f(x)[body]` 的 `[body]` 部分就是这里吃掉的。

#### 4.5.4 代码实践

**目标**：观察 `math_unparen` 如何让 `$a / (b)$` 与 `$a / b$` 产生结构相似的 CST。

**操作步骤**：

1. 阅读 `math_unparen`（[parser.rs:460-475](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L460-L475)）与它在 `math_expr_prec` 分式分支的调用（[parser.rs:366-368](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L366-L368)）。
2. 用 `parse_math("a / (b)")`（**示例代码**）打印 `MathFrac` 子节点，看右操作数的括号是否被「降级」。

**需要观察的现象**：右操作数原本是 `MathDelimited`（带 `MathText` 的 `(` `)`），经 `math_unparen` 后变成普通 `Math`，括号 token 变回 `LeftParen`/`RightParen`。

**预期结果**：`MathFrac` 的右操作数不再以 `MathDelimited` 形式出现，而是 `Math`（或保留括号但 kind 已改）。确切的 CST 形态以本地 `Debug` 输出为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `math_arg` 在 `count == 1` 时不把参数包成 `Math`？

**参考答案**：因为包成 `Math` 会把表达式的类型强制变成 content。例如 `func(#12pt)` 里 `#12pt` 是 size 类型，若包成 `Math` 就变成 content，改变了下游求值的类型（[parser.rs:534-541](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L534-L541) 的注释）。

**练习 2**：`math_delimited` 里为何要把 `LeftParen` `convert_and_eat(MathText)`？

**参考答案**：因为 lexer 在 Math 模式下把 `(` 切成普通 `LeftParen` token，但数学语义下大部分括号只是「普通字符」（如矩阵里的 `(`），只有当 parser 决定把它当定界符时才包成 `MathDelimited`；转成 `MathText` 让「不当定界符」的括号也能作为字符正确出现在 CST 与排版中（[parser.rs:433-441](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L433-L441) 的文档注释）。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个**贯穿性任务**：解析 `#let f(x) = x + 1 * 2`，对照 CST 解释 `+` 与 `*` 的优先级如何体现为不同的嵌套层级。

### 任务步骤

1. **预测调用链**：先在纸上画出从 `#` 到最终 `Binary` 节点的调用链。它应当是：
   - markup 的 `markup_expr` 见到 `#` → `embedded_code_expr`（[parser.rs:579](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L579)）；
   - `embedded_code_expr` 以 `atomic=true`、`stmt=true`（因 `let` ∈ `STMT`）调用 `code_expr_prec`（[parser.rs:588](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L588)）；
   - `code_primary` 把 `Let` 分派给 `let_binding`（[parser.rs:716](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L716)）；
   - `let_binding` 识别 `f(x)` 为闭包，吃 `=`，用**非 atomic** 的 `code_expr` 解析 `x + 1 * 2`（[parser.rs:809](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L809)）；
   - `code_expr_prec(min_prec=0)` 解析 `x + 1 * 2`：吃 `x` → 见 `+`(prec=5) 左结合传门槛 6 → 内层 `code_expr_prec(min_prec=6)` 吃 `1` → 见 `*`(prec=6 ≥ 6) 继续吃 → 内层吃 `2` → 包内层 `Binary(1*2)` → 回外层包 `Binary(x + (1*2))`。

2. **打印 CST**：用下面的**示例代码**（在独立 crate 或仓库测试模块中运行，命令待本地验证）：

```rust
// 示例代码
use typst_syntax::{parse, SyntaxKind};

fn main() {
    let root = parse("#let f(x) = x + 1 * 2");
    for node in root.descendants() {
        match node.kind() {
            SyntaxKind::LetBinding
            | SyntaxKind::Closure
            | SyntaxKind::Params
            | SyntaxKind::Binary => {
                println!("{:?}: {:?}", node.kind(), node.text());
            }
            _ => {}
        }
    }
}
```

3. **观察并解释**：
   - 应当看到一个 `LetBinding`，内含 `Closure`（其 `Params` 为 `f(x)`）；
   - 函数体里应有**两个** `Binary`：外层 `x + 1 * 2`、内层 `1 * 2`；
   - **解释优先级**：`*`（prec=6）比 `+`（prec=5）优先级高，所以 `*` 的 `Binary` 嵌套**更深**、结合**更紧**；`+` 的 `Binary` 在外层、跨度更大。这正是优先级爬升中「门槛抬高」的直接产物——外层解析右操作数时把门槛抬到 6，挡住了同级的 `+` 却放行了更紧的 `*`。
   - **解释 atomic**：整个 `x + 1 * 2` 能被完整解析，是因为 `let_binding` 内部用的是**非 atomic** 的 `code_expr`；若直接写 `#x + 1 * 2`（`#` 后非语句），`atomic=true` 会让循环在第 637 行 `break`，`+` 不会被吸收。

4. **延伸观察**（可选）：把输入换成 `#let f(x) = x + 1 * 2 + 3`，验证左结合的 `+` 会形成 `(x + (1*2)) + 3` 的左倾嵌套（外层 `Binary` 的左操作数又是 `Binary`）。

> 预期产出的确切文本格式（如是否含 trivia、空白归属）以本地运行结果为准；但上述**结构**（两层 `Binary`、`*` 深 `+` 浅、闭包糖）由源码逻辑确定，可据此核验。

## 6. 本讲小结

- **Code 解析分层**：`code_exprs` 是语句主循环（用 `ContextualContinue` 让 `else`/`.` 续行），`code_expr` → `code_expr_prec` 才是真正的表达式解析；`#` 嵌入代码经 `embedded_code_expr` 以 `atomic=true` 限制只吃一条表达式，但语句（`STMT`）内部会用非 atomic 的 `code_expr` 解析子表达式。
- **优先级爬升是核心范式**：`code_expr_prec(p, atomic, min_prec)` 先吃主表达式，再循环贴后缀；函数调用（`FuncCall`）、字段访问（`FieldAccess`）、二元算符（`Binary`）各有分支；结合性靠「抬高递归门槛」实现——左结合 `prec+1`、右结合 `prec`。优先级高 ⇒ 嵌套深 ⇒ 结合紧。
- **主表达式分发器 `code_primary`**：一张 `match` 把所有 `ATOMIC_CODE_EXPR` token 路由到归宿，语句关键字（`let`/`set`/`show`/...）各自有专门的解析函数，写法统一为「记 marker → 吃关键字 → 解析部件 → wrap」。
- **语句解析的套路**：`let_binding` 在解析期就组装闭包语法糖（`let f(x)=body` ≡ `let f = (x)=>body`）；`set_rule` 支持字段访问链与 `if` 条件；`show_rule` 用 `expected_at` 精准定位缺失的冒号；`conditional`/`while_loop`/`for_loop` 用 `block()` 统一吃 `{ }` 或 `[ ]` 体。
- **Math 是另一套语言**：Math 算符（`/` 分式、`^`/`_` 附着、撇号、`!`）与 Code 的 `+ - * /` 完全不同；`+` 在 Math 里只是文本，不构成算符树。`math_op` 给出独立的优先级表（frac=1、attach=2、bang=3），同样用优先级爬升，并多了「可续接」隐式调用与「链式附着合并进单个 `MathAttach`」的处理。
- **Math 的定界与去括号**：`math_delimited` 把 lexer 的 `{`/`(`/`[` 重解释并包成 `MathDelimited`；`math_unparen` 在分式操作数上移除多余圆括号；`math_args`/`math_arg` 解析数学函数参数，且为避免改变表达式类型，单个表达式参数不被包成 `Math`。

## 7. 下一步学习建议

本讲讲完了 Code 与 Math 的**语法解析**，产出的还是 CST（`SyntaxNode`）。接下来的学习路径：

1. **U5（CST 数据结构）**：深入 `src/node.rs`，看 `SyntaxNode` 的 `Leaf`/`Inner`/`Error`/`Warning` 四种内部形态，以及 `LinkedNode` 如何带父指针遍历——理解本讲产出的 `Binary`/`MathFrac` 节点在内存里到底是什么。
2. **U6（Span 系统）**：看 `numberize` 如何给本讲产出的每个节点盖稳定编号，从而支持编辑后的快速定位。
3. **U7（AST）**：看 `src/ast.rs` 如何把本讲的 `Binary` CST 节点转成类型化的 `Binary` AST 视图，并读出 `BinOp`（左操作数、算符、右操作数）——本讲引用的 `BinOp::precedence`/`assoc` 正来自这里。
4. **若想验证理解**：尝试在 `src/parser.rs` 的 `#[cfg(test)]` 模块里为一个新表达式写断言，或阅读现有测试用例，对照本讲的调用链加深印象。
