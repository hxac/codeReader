# CapturesVisitor：闭包变量捕获

## 1. 本讲目标

本讲承接 u4-l3「闭包定义与 eval_closure 执行」。上一讲我们把 `CapturesVisitor` 当成一个黑盒：给它函数体，它返回一个装满「捕获变量」的 `Scope`。本讲要打开这个黑盒，讲清楚：

- 闭包为什么需要「捕获」变量，捕获与词法作用域（lexical scope）是什么关系。
- `CapturesVisitor` 如何用「内部 / 外部双作用域」判断一个标识符到底是「新建绑定」还是「外部捕获」。
- `visit` 如何按 AST 节点类型（闭包、`let`、`for`、`import`、字段访问、命名对等）遍历，并在正确的时机把标识符登记为内部绑定。
- `Capturer`（`Function` / `Context`）如何决定一个捕获是否「只读」，以及 `finish()` 如何把捕获集合交给 `eval_closure`。
- 如何通过 `test_captures` / `test_captures_in_math` 两个测试用例集验证捕获行为，并预测任意一段代码的捕获结果。

学完后，你应当能对一个闭包或 `context {}` 表达式，手工「执行」一遍 `CapturesVisitor`，准确说出它捕获了哪些变量。

## 2. 前置知识

- **词法作用域（lexical scoping）**：一个名字「指向哪里」由它在源码中的**位置**决定，而不是由调用时谁在调用决定。Typst 采用词法作用域。
- **自由变量与捕获**：函数体内用到的变量，若不是该函数自己的参数或局部绑定，就是「自由变量」。闭包要能在离开定义点后照常运行，就必须把这些自由变量**捕获**（打包带走）一份。
- **`Closure` 值**（u4-l3）：`Closure` 结构体里有一个 `captured: Scope` 字段，存放捕获到的变量；`eval_closure` 执行时会把 `scopes.top = closure.captured` 作为闭包的起始作用域。
- **`Scopes`**（u1-l3 / u2-l3）：作用域栈，提供 `get` / `get_in_math` / `enter` / `exit`。本讲的 visitor **只借用它的「查名 + 进出栈」机制，不关心具体值**。
- **AST 与 `Eval` trait**（u1-l4）：`CapturesVisitor::visit` 遍历的是未求值的 `&SyntaxNode`，靠 `node.cast::<ast::...>()` 把它认成各类 AST 节点——这与真正求值时调用 `eval` 是两套独立的遍历。

## 3. 本讲源码地图

本讲几乎全部集中在 `src/call.rs`，外加两处「调用方」与一处「捕获语义」的支撑代码：

| 文件 | 本讲涉及的内容 |
| --- | --- |
| `src/call.rs` | `CapturesVisitor` 结构体与 `new` / `visit` / `finish` / `capture` / `bind`；`Closure::eval` 中调用 visitor 收集捕获；`eval_closure` 用捕获重建作用域；`test_captures` / `test_captures_in_math`。 |
| `src/code.rs` | `Contextual::eval`：`context {}` 表达式用 `Capturer::Context` 走第二套捕获路径。 |
| `typst-library/.../scope.rs` | `Capturer` 枚举、`Binding::capture`、`BindingKind::Captured`，以及 `Scopes::get` / `get_in_math` 的查找逻辑。 |

> 说明：visitor 自身定义在 typst-eval，但它产出的「捕获只读」语义（`Captured` 这一种 `BindingKind`）落在 typst-library 的 `Binding` 上。本讲会跨这两个 crate 把闭环讲清楚。

## 4. 核心概念与源码讲解

### 4.1 为什么要捕获：闭包的词法作用域

#### 4.1.1 概念说明

看一段 Typst 代码：

```typst
#let n = 10
#let add = (x) => x + n   // 闭包，体内用到了外部的 n
#add(5)                    // 期望得到 15
```

`add` 是一个值，可以被传来传去、存进字典、返回给调用方。当它真正被调用时，**定义点的作用域早已不复存在**——解释器不可能再回头去当时的环境里找 `n`。所以 Typst（和绝大多数支持闭包的语言一样）在**定义闭包的那一刻**，就把函数体里所有「外面带进来的」自由变量拷贝一份，塞进闭包值里带走。这份拷贝就叫**捕获（capture）**。

捕获带来一个直接后果：闭包捕获来的变量是**只读副本**，函数体内不能给它们赋值。这正是 u4-l3 里 `eval_closure` 用 `scopes.top = closure.captured` 重建一个干净作用域的原因——它**只携带捕获，不泄漏调用点的作用域**。

#### 4.1.2 核心流程

判定「一个标识符要不要捕获」的关键是区分两类标识符：

1. **绑定（binding）**：在闭包**内部**新建的名字——参数、`let`、`for` 的循环变量、`import` 进来的名字等。这些是「自己的」，不需要捕获。
2. **捕获（capture）**：在闭包内部使用、却不属于上面任何一类「内部绑定」的名字——它们来自定义闭包的外层环境，需要捕获。

`CapturesVisitor` 的工作就是静态地（不求值）走一遍 AST，把每个标识符归类，把所有「捕获」收集进一个 `Scope`。

### 4.2 CapturesVisitor 的双作用域模型

#### 4.2.1 概念说明

`CapturesVisitor` 用**两套作用域**同时工作，这是理解它的核心：

- `external`：闭包**定义点**的真实作用域（即 `vm.scopes` 的引用）。它用来回答「这个名字在外面能不能找到、找到的 `Binding` 是什么」。
- `internal`：visitor 自己维护的一个**影子作用域**，只用来记录「到目前为止，闭包内部已经绑定了哪些名字」。它**不存真实值**，只存占位用的 `Binding::detached(Value::None)`——因为 visitor 只关心「名字在不在内部」，不关心值。
- `captures`：一个 `Scope`，专门收集被判定为「捕获」的变量。

判定规则只有一句话：

> 遇到一个标识符，先查 `internal`：能查到 → 它是内部绑定，**跳过**；查不到 → 它是外部捕获，从 `external` 取出真实 `Binding` 放进 `captures`。

#### 4.2.2 核心流程

```
visit(节点):
    若节点是 Ident:
        capture(名字, Scopes::get)        # 走「内部? → 跳过 : → 外部捕获」判定
    若节点是会引入绑定的结构（闭包/let/for/import）:
        先 visit 不依赖新绑定的部分（如初值、可迭代对象、默认值）
        在合适的时机 internal.enter() / bind(新名字) / visit(依赖部分) / internal.exit()
    其它节点:
        递归 visit 所有子节点（特殊处理：命名对只 visit 值，字段访问只 visit 目标）
```

#### 4.2.3 源码精读

结构体定义一目了然：三个作用域相关字段 + 一个 `capturer`（4.5 节展开）。

[crates/typst-eval/src/call.rs#L745-L751](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L745-L751) — `CapturesVisitor` 持有 `external`（外层作用域引用）、`internal`（影子作用域）、`captures`（收集结果）、`capturer`（捕获者身份）。

构造函数 `new` 把传入的外层作用域存为 `external`，并新建一个**空的** `internal`（`Scopes::new(None)` 表示没有标准库 base）和一个空的 `captures`：

[crates/typst-eval/src/call.rs#L754-L762](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L754-L762) — 构造 visitor，`internal` 与 `captures` 初始都为空。

`finish` 只是消费 visitor、吐出收集好的 `captures`。闭包真正被调用时，`eval_closure` 就把这个 `Scope` 当作起始作用域：

[crates/typst-eval/src/call.rs#L764-L767](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L764-L767) — `finish` 返回捕获集合。

[crates/typst-eval/src/call.rs#L659-L662](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L659-L662) — `eval_closure` 不用调用点作用域，而是用 `closure.captured` 重建 `scopes.top`。注释 "Don't leak the scopes from the call site" 正是词法作用域的体现。

调用方 `Closure::eval` 的捕获片段（u4-l3 已见过，这里看一眼上下文衔接）：

[crates/typst-eval/src/call.rs#L610-L615](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L610-L615) — 在闭包**定义点**，把当前 `vm.scopes` 作为 `external`，`visit` 整个闭包节点，`finish` 得到 `captured`，装入 `Closure` 值。

### 4.3 capture / bind：判定与收集的两个原语

#### 4.3.1 概念说明

所有判定逻辑最终汇聚到两个小函数：

- `bind(ident)`：把一个名字登记进 `internal.top`，表示「这是闭包内部的绑定」。值用 `Value::None` 占位即可。
- `capture(ident, getter)`：判定一个名字是否需要捕获。`getter` 是一个函数指针，可以是 `Scopes::get`（普通模式）或 `Scopes::get_in_math`（数学模式），决定从哪里、用什么 fallback 查找。

#### 4.3.2 核心流程

```
capture(name, getter):
    if internal.get(name) 能查到:
        return               # 内部绑定，不捕获
    match external:
        Some(ext) =>
            match getter(ext, name):
                Ok(binding) => captures.bind(name, binding.capture(capturer))
                Err(_)      => return        # 外层也找不到 → 不是捕获（运行时会报未知变量）
        None => captures.bind(name, 占位Binding)   # IDE 分析模式，无外层作用域
```

注意两个细节：

1. **查不到就静默返回**：如果 `internal` 查不到、`external` 也查不到，`capture` 直接 `return`，**不放进 captures**。这种「两边都没有」的名字留到真正求值时由 `Scopes::get` 报 `unknown variable` 错误。visitor 只负责「该捕获的别漏」，不负责「该报错的提前报」。
2. **捕获带身份标记**：放进 `captures` 的是 `binding.capture(capturer)` 的结果——一个被标记为 `Captured` 的副本（见 4.5）。

#### 4.3.3 源码精读

`bind` 把占位绑定写入 `internal.top`，注释强调「只用 Scopes 的查名机制，不用真实值」：

[crates/typst-eval/src/call.rs#L887-L894](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L887-L894) — 登记内部绑定，值为占位 `Value::None`。

`capture` 的判定主体，先查内部、再查外部：

[crates/typst-eval/src/call.rs#L896-L917](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L896-L917) — 内部命中则跳过；外部命中则 `binding.capture(capturer)` 后写入 `captures`；外部为 `None` 时是 IDE 分析分支（见注释）。

### 4.4 visit：按节点类型遍历 AST

#### 4.4.1 概念说明

`visit` 是一个对 `&SyntaxNode` 的大 `match`。它的设计哲学是：**绝大多数节点直接递归子节点即可**（最后的 `_ =>` 分支），只有那些「会改变作用域」或「名字不该当变量」的节点需要特判。特判遵循一条统一原则——**「先 visit 不依赖新绑定的部分，再 enter/bind，再 visit 依赖部分，最后 exit」**。

#### 4.4.2 核心流程（按分支）

| 节点 | 处理要点 | 为什么特判 |
| --- | --- | --- |
| `Ident` / `MathIdent` | 调 `capture`，分别用 `get` / `get_in_math` | 叶子变量，判定捕获的唯一入口 |
| `CodeBlock` / `ContentBlock` | `enter` → 递归子节点 → `exit` | 块开新作用域，块内 `let` 不外泄 |
| `FieldAccess` / `MathFieldAccess` | 只 visit `target`，**不 visit 字段名** | `.field` 的字段不是变量 |
| `Closure` | 先 visit 所有命名参数默认值 → `enter` → bind 名字/参数 → visit body → `exit` | 默认值必须**早于**参数绑定被 visit |
| `LetBinding` | 先 visit `init`，**之后**才 bind 名字 | `let x = x` 右边的 `x` 指外部旧值 |
| `ForLoop` | 先 visit 可迭代对象 → `enter` → bind 模式变量 → visit body → `exit` | 迭代对象在绑定前求值 |
| `ModuleImport` | 先 visit 源路径 → bind 导入项 | 导入项在路径求值后才生效 |
| 命名对 `Named`（兜底分支里识别） | 只 visit `expr`（值），**不 visit 名字** | `(x: 1)` 的 `x` 是键名不是变量 |

#### 4.4.3 源码精读

**Ident / MathIdent——捕获入口。** 唯一两类会触发 `capture` 的叶子。数学模式用 `get_in_math`（fallback 查 `base.math` 而非 `base.global`，见 u2-l5）：

[crates/typst-eval/src/call.rs#L772-L779](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L772-L779) — 标识符是「潜在的捕获」；真正属于内部绑定的标识符由下面各结构在各自分支里 `bind` 掉，从而不会落到这里。

**代码块 / 内容块——开新作用域。** 用 `internal.enter()`/`exit()` 包裹子节点，块内 `let` 出块即不可见（与真实求值时 `scopes.enter/exit` 对称）：

[crates/typst-eval/src/call.rs#L781-L788](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L781-L788) — 代码块 / 内容块创建一层 `internal` 作用域。

**字段访问——只走目标。** 点号右边的字段名（如 `a.b` 里的 `b`）不是变量，绝不能被捕获，所以只递归 `target`（数学字段访问同理）：

[crates/typst-eval/src/call.rs#L790-L796](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L790-L796) — 字段访问只 visit 目标，跳过字段名。

**闭包——默认值早于参数绑定。** 这是全文件最精巧的分支。注意三段顺序：

[crates/typst-eval/src/call.rs#L798-L831](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L798-L831) — ① 先（在 `enter` **之前**）visit 所有命名参数的默认值；② `enter` 后绑定函数名与各参数（位置参数按 `pattern.bindings()`、命名参数按名字、spread 按 sink 标识符）；③ 再 visit 函数体。

> **关键设计**：默认值在 `enter` 之前被 visit，意味着此刻 `internal` 还没登记任何参数。于是默认值里出现的标识符一律被判为「外部捕获」——即使它与某个参数同名。这正是一条 Typst 语言规则的实现：**命名参数的默认值不能引用任何参数**（无论前后）。详见 4.6 的实践推演。

**`let` 绑定——名字在初值之后才生效。** 先 visit `init`，再 `bind`。所以 `#let x = x` 里右边的 `x` 会去外部找（被捕获），而不是引用正在定义的 `x`：

[crates/typst-eval/src/call.rs#L833-L843](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L833-L843) — `let` 先 visit 初值，再把名字 `bind` 进 `internal`（`expr.kind().bindings()` 对 `#let f = ...` 返回函数名，对 `#let (a,b) = ...` 返回解构名）。

**`for` 循环——迭代对象在前，循环变量在中。** 先 visit 可迭代对象，再 `enter`、绑定模式变量、visit 循环体：

[crates/typst-eval/src/call.rs#L845-L859](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L845-L859) — `for` 的迭代对象在参数绑定前求值，模式变量用 `pattern.bindings()` 收集。

**`import`——路径在前，导入项在后。** 先 visit 源路径，再 bind 各导入项的 `bound_name()`：

[crates/typst-eval/src/call.rs#L861-L870](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L861-L870) — `import` 的源路径先求值，具名导入项随后才绑定。

**兜底分支——命名对只走值，其余全递归。** 这是「绝大多数节点」的归宿。特别地，先用 `node.cast::<ast::Named>()` 探测命名对（如函数调用里的 `x: 1` 或字典里的 `x: 1`），命中则只 visit 值 `expr` 并 `return`，确保键名不被当作变量；否则按从左到右递归所有子节点：

[crates/typst-eval/src/call.rs#L872-L883](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L872-L883) — 兜底：命名对只 visit 值，其余节点递归全部子节点。

### 4.5 Capturer：Function 与 Context 两种捕获

#### 4.5.1 概念说明

捕获不是「无差别拷贝」。Typst 有两种会捕获变量的结构：

- **函数 / 闭包** `(x) => x + n`：捕获者身份是 `Capturer::Function`。
- **`context` 表达式** `context { ... }`（写成 `#context [...]` 或 `context key`）：它的 body 也会捕获外层变量，身份是 `Capturer::Context`。

两者都把捕获来的变量标记为**只读**，区别只在报错时的措辞——`Binding::write()` 在尝试写捕获变量时，会根据 `Capturer` 给出 "variables from outside the **function** are read-only" 或 "...the **context expression** are read-only"。

#### 4.5.2 核心流程

```
Closure::eval:       CapturesVisitor::new(&vm.scopes, Capturer::Function)
Contextual::eval:    CapturesVisitor::new(&vm.scopes, Capturer::Context)

capture 时:           binding.capture(capturer) → 产生 BindingKind::Captured(capturer) 的副本
求值时写捕获变量:      Binding::write() 命中 Captured → 报「只读」错误
```

#### 4.5.3 源码精读

`Capturer` 枚举只有两个变体，定义在 typst-library：

[crates/typst-library/src/foundations/scope.rs#L353-L360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L353-L360) — `Capturer { Function, Context }`。

`Binding::capture` 复制绑定并把 `kind` 改成 `Captured(capturer)`：

[crates/typst-library/src/foundations/scope.rs#L329-L335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L329-L335) — 捕获即「克隆一份并打上 `Captured` 标记」。

`Binding::write` 遇到 `Captured` 就拒绝写入，措辞按 `Capturer` 区分：

[crates/typst-library/src/foundations/scope.rs#L312-L327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L312-L327) — 捕获变量只读，写入时报「来自 function / context expression 的变量只读」。

第二套捕获路径在 `Contextual::eval`，与 `Closure::eval` 完全同构，只是传 `Capturer::Context`、且只 visit `body`：

[crates/typst-eval/src/code.rs#L387-L410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L387-L410) — `context {}` 表达式把 body 当作一个零参「闭包」捕获，用 `Capturer::Context`，最终包成 `ContextElem`。

### 4.6 测试用例：验证捕获集合

#### 4.6.1 概念说明

`call.rs` 末尾自带一个 `#[cfg(test)] mod tests`，用大量断言固化了捕获规则。测试辅助函数 `test(scopes, code, &期望名字列表)` 做三件事：建一个 `Capturer::Function` 的 visitor、`visit` 解析出的根节点、`finish` 后排序比对名字集合。这是验证「我有没有正确理解规则」的最佳工具。

#### 4.6.2 核心流程

```
test(scopes, text, result):
    visitor = CapturesVisitor::new(Some(scopes), Capturer::Function)
    visitor.visit(parse(text))            # 静态遍历，不求值
    captures = visitor.finish()
    names = sort(captures.iter().map(|(k,..)| k))
    assert_eq!(names, result)
```

外层 `scopes` 里预先定义了 `f x y z`（数学测试还多定义 `foo bar x-bar x_bar`）等名字，模拟「定义闭包时的环境」。某个名字出现在期望列表里，就说明 visitor 判定它被捕获。

#### 4.6.3 源码精读

测试辅助与主用例集：

[crates/typst-eval/src/call.rs#L926-L937](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L926-L937) — `test` 辅助函数：建 visitor → visit → finish → 排序比对。

[crates/typst-eval/src/call.rs#L939-L990](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L939-L990) — `test_captures`：覆盖 let、闭包参数、show 规则、for 循环、import、块、字段访问、括号化表达式、赋值等几乎所有结构。

[crates/typst-eval/src/call.rs#L992-L1030](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L992-L1030) — `test_captures_in_math`：聚焦数学模式的特殊性。

几个值得咀嚼的用例（请对照 4.4 的分支自己推演）：

- `#let x = x` → 捕获 `["x"]`：`let` 先 visit 初值 `x`（此时名字还没 bind），右值 `x` 去外部找 → 捕获；随后才 bind `x`。
- `#((x, y: x + z) => x + y)` → 捕获 `["x", "z"]`：默认值 `x + z` 在 `enter` 前 visit，`x`/`z` 都算外部捕获；body 里的 `x`/`y` 是参数（内部绑定）→ 不捕获。**这就是「默认值不能访问参数」的物证**。
- `#{x => x; x}` → 捕获 `["x"]`：代码块里先是一段闭包 `x => x`（参数 `x` 是内部绑定，body 的 `x` 不捕获），块末尾裸用的 `x` 才被捕获。
- `#for x in y {} #x` → 捕获 `["x", "y"]`：`for` 的可迭代对象 `y` 被捕获；循环变量 `x` 在循环体内是内部绑定（不捕获），但循环**之后**的 `#x` 处，`for` 已经 `exit`，`x` 不再是内部绑定 → 被捕获。
- `#x.y.f(z)` → 捕获 `["x", "z"]`：字段访问只走目标，所以 `y`、`f` 这些字段名都不捕获，只捕获 `x` 与参数 `z`。
- `#(x.at(y) = 5)` → 捕获 `["x", "y"]`：`at` 是方法名（字段访问的字段），不捕获；`x`、`y` 是变量，捕获。

#### 4.6.4 代码实践

**实践目标**：亲手「执行」`CapturesVisitor`，预测两段代码的捕获集合，再用测试函数验证。

**操作步骤**：

1. 先不查答案，推演下面两段代码分别捕获哪些变量（外层作用域都有 `f x y z`）：

   ```typst
   // A
   #let f = (x, y) => f

   // B
   #((x, y: x + z) => x + y)
   ```

   推演时请严格按 4.4 的闭包分支顺序：**「先 visit 命名参数默认值 → enter → bind 参数 → visit body」**。

2. 推演要点（B 为例）：
   - 默认值 `x + z` 在 `enter` **之前** visit → 此时 `internal` 为空 → `x`、`z` 都从外部捕获。
   - `enter` 后 bind `x`（位置参数）、`y`（命名参数名）。
   - visit body `x + y` → `x`、`y` 都命中 `internal` → 不捕获。
   - 最终捕获 `["x", "z"]`。

3. 验证方法：在仓库根目录运行该测试（`test_captures` 里已包含这两条用例，分别见源码第 953、959 行）：

   ```bash
   cargo test -p typst-eval --lib call::tests::test_captures
   ```

**需要观察的现象 / 预期结果**：

- A 的预测：捕获 `["f"]`。闭包 body 里的 `f` 此刻（`enter` 后只绑定了参数 `x`、`y`）不在 `internal`，于是去外部找 → 捕获 `f`；参数 `x`、`y` 不捕获。注意 `#let f = ...` 中 `let` 对名字 `f` 的 `bind` 发生在 visit 初值（整个闭包）**之后**，所以闭包内的 `f` 指向外部旧 `f`。
- B 的预测：捕获 `["x", "z"]`（如上推演）。
- 测试应当通过（`test_captures` 里这两条断言正是 `&["f"]` 与 `&["x", "z"]`）。

**关于「默认值为何不能访问后续参数绑定」**：对照闭包分支，所有命名参数的默认值表达式在 `self.internal.enter()` **之前**、在任何参数被 `bind` **之前**统一 visit。因此默认值里出现的标识符（哪怕与某参数同名）此刻都查不到 `internal`，一律判为外部捕获——即默认值只能引用**定义环境**的变量，不能引用**任何**参数（无论是前一个还是后一个）。这条时机设计同时回答了 u4-l3 提到的「默认值为何不能访问后续参数绑定」。

> 说明：若本地未配置完整 typst workspace 编译环境，`cargo test` 可能因依赖问题失败；此时可仅做源码阅读型推演，把预测结果与 `test_captures` 第 953、959 行的断言比对即可（标注「待本地验证」）。

#### 4.6.5 小练习与答案

**练习 1**：预测 `#let f(x, y) = f` 捕获哪些变量，并说明它与 `#let f = (x, y) => f` 的差异原因。

参考答案：捕获 `[]`（空）。`#let f(x, y) = f` 是「函数定义语法糖」，`f` 是**函数名**，会被闭包分支在 `enter` 后 **bind** 进 `internal`（`if let Some(name) = expr.name() { self.bind(name); }`），所以 body 里的 `f` 命中 `internal`，不捕获。而 `#let f = (x, y) => f` 中，等号右边是一个**匿名**闭包（无 `name`），闭包分支里不 bind `f`；`let` 对 `f` 的绑定又发生在 visit 初值之后，于是闭包内 `f` 指向外部 → 捕获 `["f"]`。

**练习 2**：`#show y: x => x + z` 捕获什么？为什么 selector 中的 `y` 也被捕获？

参考答案：捕获 `["y", "z"]`。show 规则把 `y`（选择器）和 `(x => x + z)`（转换函数）都视为需要捕获的标识符：`x` 是转换函数的参数（内部绑定，不捕获），`z` 在 body 中被捕获；而 `y` 出现在 show 规则的选择器位置，被当作普通 `Ident` 捕获。这正是第 964 行 `test(s, "#show y: x => x + z", &["y", "z"])` 的断言。

**练习 3**：为什么 `$ x f(z) $` 捕获为空，而 `$ foo f(bar) $` 捕获 `["bar", "foo"]`？

参考答案：在数学模式里，**单字符**标识符（如 `x`、`f`、`z`）被解析为 `ast::MathText`（字形，最终打成 `SymbolElem`），**不是** `ast::MathIdent`；而 visitor 的捕获入口只匹配 `ast::Expr::MathIdent`，所以单字符「字母」根本不会触发 `capture`。多字符名字（`foo`、`bar`）才会被解析成 `MathIdent` 从而走 `get_in_math` 捕获。要捕获单个字母，需用 `#` 进入代码模式，如 `$ #x #f(z) $` 会捕获 `["f", "x", "z"]`（见第 1008 行）。

## 5. 综合实践

**任务**：给 visitor 当一次「人肉解释器」，预测下列四段代码（外层作用域均有 `f x y z`）的捕获集合，并为每段写出依据的分支；最后设计一条**新的**测试用例加入 `test_captures`，运行测试确认你的理解。

```typst
// (1)
#let g = (a) => { let b = a + z; b + y }

// (2)
#for (k, v) in y { k + v + x }

// (3)
#import z: a, b

// (4) 数学模式
$ #let foo = x; foo + bar $
```

**要求与提示**：

1. 对 (1)：注意闭包 body 是一个**代码块**——它会 `enter` 一层 `internal`；块内的 `let b` 在 visit 初值 `a + z` 之后才 bind。请分别判断 `a`、`b`、`z`、`y` 谁被捕获、谁是内部绑定。
2. 对 (2)：`for` 的模式是解构 `(k, v)`，可迭代对象 `y` 在绑定前 visit；循环体在 `enter` 之后 visit，`k`、`v` 是内部绑定。
3. 对 (3)：`import` 先 visit 源 `z`，再 bind `a`、`b`；`a`、`b` 不在外层作用域里也无妨——它们是**内部绑定**而非捕获。
4. 对 (4)：数学模式，`foo`、`bar` 是多字符 `MathIdent`，`x` 单字符是 `MathText`（不捕获）；注意 `#let foo = x` 中 `x` 是 `#` 后的代码标识符。

**自检答案**（用测试函数 `test` 验证）：
- (1) 捕获 `["y", "z"]`（`a`、`b` 都是内部绑定）。
- (2) 捕获 `["x", "y"]`（`k`、`v` 内部绑定；`x` 在循环体内但属外部 → 捕获；`y` 是可迭代对象 → 捕获）。
- (3) 捕获 `["z"]`（`a`、`b` 是导入项内部绑定）。
- (4) 捕获 `["bar", "x"]`（`foo` 被 `let` 绑定为内部名，body 里第二次出现的 `foo` 不捕获；`x` 因 `#` 进入代码模式而捕获；`bar` 多字符数学标识符捕获）。

把你的新用例仿照现有格式加进 `test_captures` / `test_captures_in_math`，运行 `cargo test -p typst-eval --lib call::tests`，通过即说明你已掌握 visitor 的判定规则。

## 6. 本讲小结

- 闭包要能在离开定义点后运行，必须在**定义时**把函数体里的自由变量捕获带走；`CapturesVisitor` 就是静态（不求值）完成这件事的遍历器。
- 判定的核心是**双作用域**：`internal`（影子作用域，只记「哪些名字是内部绑定」）+ `external`（定义点真实作用域）。一个标识符命中 `internal` 则跳过，否则从 `external` 取 `Binding` 存入 `captures`。
- `capture` 查不到（内部、外部都没有）时静默返回，把「未知变量」错误留给真正求值阶段；`bind` 只写占位值，因为 visitor 只用 `Scopes` 的查名机制。
- `visit` 对绝大多数节点直接递归子节点，只对会改变作用域的结构（闭包 / let / for / import / 代码块）和「名字不当变量」的结构（字段访问、命名对）做特判；统一遵循「先 visit 不依赖新绑定的部分 → enter/bind → visit 依赖部分 → exit」。
- 闭包分支把**命名参数默认值**放在 `enter` 之前 visit，使默认值只能引用定义环境、不能引用任何参数——这是「默认值不能访问后续参数绑定」的根源。
- `Capturer`（`Function` / `Context`）给捕获副本打上身份标记，使捕获变量在求值时成为**只读**，写入时按身份给出差异化错误措辞；`context {}` 表达式走与闭包同构的第二套捕获路径。

## 7. 下一步学习建议

- **横向巩固**：回到 u4-l3，对照 `eval_closure` 看 `closure.captured` 如何成为函数的起始作用域，体会「捕获」与「执行」的闭环。
- **进入第 5 单元**：u5-l1（模块导入）会用到 `CapturesVisitor` 对 `import` 的处理结论；u5-l3（Access 与可变方法）会解释为什么捕获变量是只读、以及 `Binding::write()` 在赋值链路中如何报错。
- **IDE 视角**（u6-l2）：注意 `CapturesVisitor::new(None, …)` 的「无 external」分支——当 `external` 为 `None` 时是 IDE 做捕获分析的特殊用法，可结合 tracing 讲义理解。
- **延伸阅读**：直接精读 `call.rs` 第 745–918 行的 visitor 全文，再逐条对照 `test_captures`（939–990）与 `test_captures_in_math`（992–1030）的断言，这是把规则内化最有效的练习。
