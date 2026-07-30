# 闭包定义与 eval_closure 执行

## 1. 本讲目标

本讲承接 u4-l1（函数调用与参数求值），把镜头从「调用方」转到「被调用方」：当 Typst 源码里写下一个函数字面量 `(x, y) => x + y` 或 `let f(x) = ...` 时，解释器是**怎么把它变成一个可调用的值**、又在**每次被调用时如何执行函数体**的。

读完本讲，你应该能够：

- 说清 `Closure::eval` 把一个函数字面量打包成 `Func` 值的三步：求值命名参数默认值 → 收集捕获变量 → 装配 `Closure` 结构体（含 `defaults`、`captured`、`num_pos_params`）。
- 说清 `eval_closure` 执行闭包时的「环境重建」：它**不复用调用点的作用域**，而是用捕获到的 scope 重新建一个干净 `Scopes`，从而杜绝作用域泄漏。
- 还原参数绑定循环对四类参数（`Pos(Normal Ident)`、`Pos(pattern 解构)`、`Spread`、`Named`）的不同处理方式。
- 解释 spread sink（`..args`）如何用 `checked_sub` 算出「剩余位置参数数量」并两阶段收集，以及无名 sink `..` 的丢弃语义。
- 理解函数名如何通过 `vm.define(name, func.clone())` 支持递归自调用。
- 解释 `eval_closure` 末尾对 `FlowEvent::Return` 的三种消费分支：有显式返回值直接返回、无显式返回值回落到函数体产出、非法事件（`break`/`continue`）报错。

## 2. 前置知识

在进入源码前，先用日常语言理清几个概念。

**闭包（closure）与函数（function）。** 在 Typst 里，`let f = (x) => x + 1` 和 `let f(x) = x + 1` 写法不同，但解释器眼里它们都是「用户定义的函数」，统一表示成运行时值 `Value::Func`，其内部由 `Closure` 结构体承载。本讲所说的「闭包」就是指这种用户定义函数（区别于用 Rust 写的内置函数 `FuncInner::Native`）。

**捕获（capture）。** 一个函数内部如果引用了定义它时所在作用域里的变量（而非自己的参数），这些变量就需要被「捕获」、随函数一起带走。例如：

```typst
#let base = 10
#let add-base = (x) => x + base   // base 在函数外定义，被捕获
#add-base(5)                       // 15
```

「捕获」在 `Closure::eval` 阶段就完成（用 `CapturesVisitor` 扫描 AST 决定捕谁，详见 u4-l4），本讲只关心「捕获结果被存进 `Closure::captured`，并在执行时拿来重建作用域」这一环节。

**作用域（scope）与作用域栈（Scopes）。** 变量名到值的映射叫作用域；多个作用域叠成栈就是 `Scopes`（u2-l3 讲过 `enter`/`exit`）。`Scopes` 有三部分：`top`（当前最内层）、`scopes`（外层栈）、`base`（标准库）。本讲的关键问题是：**函数被执行时，它的 `Scopes` 从哪里来？** 答案是——只从捕获来的 scope 来，**绝不从调用点来**。

**位置参数与命名参数。** 调用 `f(1, 2, name: 3)` 时，`1`、`2` 是位置参数（按位置对齐），`name: 3` 是命名参数（按名字对齐）。函数定义端 `(a, b, name) => ...` 的 `a`、`b` 是位置参数，`name` 是带默认值的命名参数。

**带外信号 `FlowEvent`。** Typst 的 `return`/`break`/`continue` 不借助 Rust 异常，而是写入虚拟机字段 `vm.flow`（u3-l2 详讲）。本讲只需要知道：函数体内一旦执行 `return v`，就会往 `vm.flow` 塞一个 `FlowEvent::Return(span, Some(v), false)`，`eval_closure` 末尾负责读出它。

> 本讲全程在 `Eval` trait + `Vm` 框架（u1-l4）内讨论。`Closure::eval` 产出 `Value`，`eval_closure` 是 `#[comemo::memoize]` 的自由函数（被 `Func::call` 分派调用），二者一前一后构成「定义—执行」闭环。

## 3. 本讲源码地图

本讲的核心代码几乎全部集中在 `src/call.rs`，辅以少量类型定义：

| 文件 | 本讲关注的角色 |
|------|----------------|
| `src/call.rs` | `Closure::eval`（定义阶段：打包 Func 值）、`eval_closure`（执行阶段：重建作用域、绑定参数、跑函数体、消费 Return） |
| `crates/typst-library/.../foundations/func.rs` | `Closure` 结构体定义（`node`/`defaults`/`captured`/`num_pos_params`）、`ClosureNode` 枚举、`Func::call_impl` 如何分派到 `eval_closure` |
| `crates/typst-library/.../foundations/scope.rs` | `Scopes` 结构体与 `Scopes::new(None)` |
| `crates/typst-library/.../foundations/args.rs` | `Args` 的 `expect`/`named`/`consume`/`take`/`finish`/`to_pos` 等参数消费方法 |
| `src/vm.rs` | `Vm::new`、`Vm::define`、`Vm::bind` |
| `src/flow.rs` | `FlowEvent` 枚举与 `forbidden()` |
| `src/binding.rs` | `crate::destructure`（pattern 参数解构的复用入口） |

## 4. 核心概念与源码讲解

### 4.1 Closure::eval：把函数字面量打包成 Func 值

#### 4.1.1 概念说明

当解释器在源码里遇到一个函数字面量（`(x, y) => ...` 或 `let f(x) = ...`），它对应的 AST 节点是 `ast::Closure`。`Closure::eval` 的职责很单一：**在「定义此刻」就把所有能在定义期确定的东西算好，打包成一个 `Closure` 值**，然后包成 `Value::Func` 返回。

为什么要在「定义期」就做两件事？

1. **命名参数的默认值**（如 `(x, y: 10) => ...` 里的 `10`）必须在**定义所处的作用域**里求值，而不是「将来某次调用时的作用域」。否则默认值就能看到调用点的变量，语义会乱套。所以默认值必须**现在就求出来**、冻进 `Closure`。
2. **捕获变量**也必须在**定义所处的作用域**里确定——因为函数一旦被传到别处再调用，定义点的作用域早就销毁了，到那时再去找捕获就找不到。所以「谁该被捕获」必须现在就扫描清楚、把值快照下来。

至于函数体本身，**不求值**——只把它的语法节点原样存进 `Closure`，留到执行时再说。

#### 4.1.2 核心流程

`Closure::eval` 的三步：

```
Closure::eval:
  1. 求默认值：遍历所有参数，对每个 Named 参数，把它的默认表达式现在求值，
     依次 push 进 defaults: Vec<Value>（保持源码顺序）
  2. 收捕获：用 CapturesVisitor 扫描整个闭包 AST，区分「内部绑定」与「外部捕获」，
     finish() 得到一个装满捕获绑定 的 Scope
  3. 装配：构造 Closure {
        node: ClosureNode::Closure(语法节点.clone()),  // 函数体不求值，只存节点
        defaults,                                       // 命名参数默认值
        captured,                                       // 捕获的变量
        num_pos_params: 统计 Pos 参数个数（含 pattern 参数，各算 1 个）
     }
  4. 返回 Value::Func(Func::from(closure).spanned(params 的 span))
```

注意第 3 步里 `num_pos_params` 的统计口径：它数的是 `Param::Pos(_)` 的**个数**，不管这个位置参数是普通标识符 `(a)` 还是解构模式 `((b, c))`——后者在统计时只算 **1** 个位置参数（因为执行时它只消耗 1 个位置实参，只是拿到后再拆开）。

#### 4.1.3 源码精读

入口 [`impl Eval for ast::Closure`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L598-L631) 全文很短，正好对应上面三步：

```rust
fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
    // 第 1 步：求值命名参数默认值（在定义点作用域里求，现在就冻住）
    let mut defaults = Vec::new();
    for param in self.params().children() {
        if let ast::Param::Named(named) = param {
            defaults.push(named.expr().eval(vm)?);
        }
    }

    // 第 2 步：收集捕获变量（CapturesVisitor 的实现是下一讲 u4-l4 的主题）
    let captured = {
        let mut visitor = CapturesVisitor::new(Some(&vm.scopes), Capturer::Function);
        visitor.visit(self.to_untyped());
        visitor.finish()
    };

    // 第 3 步：装配 Closure 值
    let closure = Closure {
        node: ClosureNode::Closure(self.to_untyped().clone()),
        defaults,
        captured,
        num_pos_params: self
            .params()
            .children()
            .filter(|p| matches!(p, ast::Param::Pos(_)))
            .count(),
    };

    Ok(Value::Func(Func::from(closure).spanned(self.params().span())))
}
```

要点：

- 第 1 步遍历的是 `self.params().children()`，**只对 `Named` 参数求默认值**；`Pos`、`Spread` 参数被跳过。`defaults` 的顺序就是源码里 `Named` 参数出现的顺序——这点很重要，执行期要靠这个顺序对齐（见 4.4）。
- 第 2 步把**当前的 `vm.scopes`** 作为外部作用域交给 `CapturesVisitor`，让它判断哪些标识符是「外面带进来的」。`Capturer::Function` 表示这是普通函数捕获（另一个取值 `Capturer::Context` 用于 `context {}`，见 u6-l2）。本讲不展开 visitor 内部，只把它当成「给我一个装满捕获的 `Scope`」的黑盒。
- 第 3 步 `num_pos_params` 用 `filter + matches!` 数 `Pos` 参数。注意这里 `node` 存的是 `self.to_untyped().clone()`——**整个闭包语法节点的克隆**，函数体就藏在这里面，留待执行期 `node.cast::<ast::Closure>()` 再取出来求值。

装配出的 [`Closure` 结构体](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L725-L734) 字段一目了然：

```rust
pub struct Closure {
    pub node: ClosureNode,      // 语法节点（函数体在这里，不求值）
    pub defaults: Vec<Value>,   // 命名参数默认值
    pub captured: Scope,        // 捕获的外层变量
    pub num_pos_params: usize,  // 位置参数个数
}
```

其中 [`ClosureNode`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L715-L721) 有两个变体：`Closure(SyntaxNode)` 是普通用户函数；`Context(SyntaxNode)` 是为 `context {}` 表达式合成的「无参闭包」（u6-l2 会讲）。本讲只涉及前者。

`Func::from(closure)` 把它包进 `FuncInner::Closure`，于是这个函数值就能像内置函数一样被 `f(...)` 调用了。

#### 4.1.4 代码实践

**实践目标：** 通过阅读测试，验证「默认值在定义点求值、捕获在定义点冻结」。

**操作步骤（源码阅读型）：**

1. 打开 `src/call.rs` 末尾的 `test_captures` 测试（约第 939 行起），找到这几行用例：

   ```rust
   test(s, "#let f = (x, y) => x + y", &[]);      // 没捕获任何外层变量
   test(s, "#let f = (x, y) => f", &["f"]);        // 捕获了外层的 f（自身名字）
   test(s, "#((x, y: x + z) => x + y)", &["x", "z"]); // 默认值 y: x+z 里用到 x、z
   ```

2. 对照 `Closure::eval` 的第 1、2 步解释：
   - 第 1 行：函数体只用参数 `x`、`y`，没有引用外层，`captured` 为空。
   - 第 2 行：函数名 `f` 在 `(x, y) => f` 中是**被引用的外层变量**（注意这是 `let f = (...)` 形式，箭头函数体里的 `f` 指向外层的 `f`，而非自引用——自引用只在 `let f(x) = ...` 形式才有，见 4.3），所以捕获 `f`。
   - 第 3 行：默认值 `x + z` 里的 `x`（前一个位置参数）和 `z`（外层变量）都被捕获——这说明**默认值表达式也参与捕获扫描**，且默认值能看到**排在它前面的参数** `x`。

3. **需要观察的现象：** 第 3 行用例 `#((x, y: x + z) => x + y)` 捕获了 `x` 和 `z`。请思考：为什么默认值 `x + z` 里的 `x` 被算作「捕获」而非「内部参数绑定」？（提示：`CapturesVisitor` 在扫描闭包时，先访问命名参数的默认值表达式，**之后**才把参数名登记为内部绑定。这是 u4-l4 的重点，本讲只确认这个现象。）

**预期结果：** 你能说清「默认值在定义点求值、且默认值参与捕获」这两点，并理解为什么 `Closure::eval` 必须在定义期就把 `defaults` 和 `captured` 都算好。

#### 4.1.5 小练习与答案

**练习 1：** `#let f = (a, (b, c), d: 5) => a + b + c` 的 `num_pos_params` 是多少？`defaults` 里有几个元素？

**答案：** `num_pos_params = 2`（`a` 和 `(b, c)` 各算 1 个 `Pos` 参数，解构模式 `(b, c)` 只算 1）；`defaults` 有 1 个元素（只有 `d: 5` 是 `Named` 参数，其默认值 `5` 被求值存入）。

**练习 2：** 为什么 `Closure::eval` 不在这里求值函数体，而要等到 `eval_closure`？

**答案：** 因为函数体只有在**被调用、且参数已绑定**后才有意义；定义时函数体里引用的参数尚不存在。把函数体存成语法节点、延迟到执行期求值，才能让同一个函数值被反复调用、每次用不同参数。

---

### 4.2 eval_closure 骨架：重建 Scopes 与隔离调用点

#### 4.2.1 概念说明

`Closure::eval` 造出 `Func` 值只是「定义」。真正「执行」发生在某处 `f(args)` 调用它时：`Func::call_impl` 发现这是个闭包，就转去调用 `eval_closure`（[func.rs 的分派代码](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L352-L363)）。

`eval_closure` 是一个带 `#[comemo::memoize]` 的自由函数，签名一长串 `Tracked`/`TrackedMut` 参数——这是 comemo 记忆化的要求（u6-l3 会讲缓存键判等）。本讲先忽略缓存，只看它**每次执行时做了什么**。

最关键的设计决策写在函数里一句注释里：**「不要泄漏调用点的作用域」**。换句话说，函数内部能看到的外层变量，**只有它定义时捕获的那些**，绝不多看调用点的半个变量。这是词法作用域（lexical scoping）的根基。

#### 4.2.2 核心流程

`eval_closure` 的骨架（参数绑定与 Return 消费留到后面几节）：

```
eval_closure(func, closure, world, library, ..., args):
  1. 从 closure.node 取出 (name, params, body)
       Closure(node) → (closure.name(), closure.params(), closure.body())
       Context(node) → (None, 占位 params, node)   // 本讲不涉及
  2. 重建 Scopes（关键！）：
       scopes = Scopes::new(None)        // 全新空栈，base=None（连标准库都不直接挂！）
       scopes.top = closure.captured     // 把捕获的 scope 作为唯一外层可见变量
  3. 装配 Engine：route = Route::extend(route)  // 在调用链上续一节（深度+1）
  4. 装配 Vm：Vm::new(engine, context, scopes, body.span())
  5. （递归自引用，见 4.3）
  6. （参数绑定，见 4.4 / 4.5）
  7. （收尾与 Return，见 4.6）
```

第 2 步是本节的灵魂。注意 `Scopes::new(None)` 传入 `None` 作为 `base`——这意味着新 `Scopes` **不挂标准库**。那函数体里用的内置函数（`calc.sin`、`array.len` 等）从哪来？答案是：标准库的绑定在被捕获时就已经进了 `closure.captured`（捕获是按需的，用到的才会被捕），或者通过 `std` / 模块访问。这是一个非常干净的隔离模型。

#### 4.2.3 源码精读

先看 [`eval_closure` 的签名与开头](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L634-L677)：

```rust
#[comemo::memoize]
#[expect(clippy::too_many_arguments)]
pub fn eval_closure(
    func: &Func,
    closure: &LazyHash<Closure>,
    world: Tracked<dyn World + '_>,
    library: &LazyHash<Library>,
    introspector: Tracked<dyn Introspector + '_>,
    traced: Tracked<Traced>,
    sink: TrackedMut<Sink>,
    route: Tracked<Route>,
    context: Tracked<Context>,
    mut args: Args,
) -> SourceResult<Value> {
    let (name, params, body) = match closure.node { /* 取 name/params/body */ };

    // Don't leak the scopes from the call site. Instead, we use the scope
    // of captured variables we collected earlier.
    let mut scopes = Scopes::new(None);
    scopes.top = closure.captured.clone();

    // Prepare the engine.
    let introspector = Protected::from_raw(introspector);
    let engine = Engine {
        library, world, introspector, traced, sink,
        route: Route::extend(route),
    };

    // Prepare VM.
    let mut vm = Vm::new(engine, context, scopes, body.span());
    ...
```

要点：

- `Scopes::new(None)` 见 [scope.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L29-L31)：`Self { top: Scope::new(), scopes: vec![], base: None }`。一个空的 `top`、空的外层栈、没有标准库。
- 紧接着 `scopes.top = closure.captured.clone()` 把捕获 scope **整体搬上来当 `top`**。于是函数体里查变量时（`Scopes::get`，[scope.rs:46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L46-L59)），先查 `top`（=捕获）、再查 `scopes`（空）、最后查 `base`（None）——**完全看不到调用点的任何东西**。
- `Route::extend(route)` 见 [engine.rs:295](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L302)：它造一个新的 `Route` 段，`len: 1`，`outer` 指向外层 route。整条 route 的总长度就是调用深度，超限会被 `check_call_depth` 拦下（u4-l1 讲过、u6-l3 会详谈）。每次进入 `eval_closure` 都 `extend` 一次，调用深度就 +1。
- `Vm::new(engine, context, scopes, body.span())` 造一台全新的虚拟机（u1-l4 讲过 `Vm` 的字段：`engine`/`flow`/`scopes`/`inspected`/`context`）。这里 `flow` 被初始化为 `None`——函数体执行前没有任何控制流事件。

> **为什么 `args` 是 `mut` 且按值传入？** 因为参数消费是「 destructive 」的：`args.expect`/`args.named`/`args.consume` 都会把用掉的参数从 `args.items` 里**移除**，最后 `args.finish()` 检查是否还有没被认领的参数（见 4.6）。这种「边吃边删」的设计让「剩余参数」的判定非常自然。

#### 4.2.4 代码实践

**实践目标：** 用一段 Typst 代码验证「函数内部看不到调用点作用域」。

**操作步骤（可运行 + 源码阅读）：**

1. 准备 Typst 代码（存为 `scope.typ`，用 typst CLI 编译）：

   ```typst
   #let outer = 100
   // greet 不捕获任何变量
   #let greet = () => {
     // 函数体内访问 outer：这里会报错，因为 outer 没被捕获
     outer
   }

   #let outer2 = 100
   // greet2 显式用了 outer2，于是它被捕获
   #let greet2 = () => outer2

   #greet2()  // 100，正常
   ```

2. 对照 `eval_closure` 第 2 步解释：`greet` 的函数体里出现 `outer`，但 `CapturesVisitor` 在定义期判断它是「引用的外层变量」，于是**理应**被捕进 `captured`——等等，那为什么说「看不到调用点」？关键区分：**捕获发生在定义点**（`Closure::eval` 时 `vm.scopes` 就是定义点作用域），而不是调用点。`greet` 在定义点能看到 `outer=100`，所以会被捕获；但如果你在**另一个没有 `outer` 的模块**调用 `greet`，它依然返回 100，因为它用的是**被捕快照**，与调用点无关。

3. 把 `greet` 的定义改成完全不引用外层、却在调用点附近临时定义同名变量，验证函数内取到的仍是捕获快照而非调用点值。

**需要观察的现象：** 函数返回的 `outer` 永远是**定义点**的值，即使调用点把 `outer` 改成了别的值也不受影响。这正是 `scopes.top = closure.captured.clone()` 的效果——`captured` 是定义期冻结的快照。

**预期结果：** 你能用自己的话讲清「调用点作用域」与「捕获快照」的差别，并指出代码里那句注释「Don't leak the scopes from the call site」对应的就是 `Scopes::new(None) + scopes.top = closure.captured`。

#### 4.2.5 小练习与答案

**练习 1：** `eval_closure` 里新建的 `Scopes` 的 `base` 是 `None`。这意味着函数体内写 `#calc.sin(0.5)` 中的 `calc` 从哪来？

**答案：** `calc` 是标准库模块。它要么在被捕获时（定义期，函数体引用了 `calc`）作为外层绑定被捕进了 `closure.captured`；要么通过 `std.calc` 访问（`std` 也是标准库提供的绑定，同样靠捕获或 scope 查找）。由于 `base=None`，标准库不会自动挂在 `Scopes` 上，所有标准库访问都依赖捕获机制。

**练习 2：** 如果删掉 `scopes.top = closure.captured.clone()` 这一行（即 `top` 保持空 `Scope`），闭包还能正常工作吗？

**答案：** 不能。函数体内引用的任何外层变量（包括被捕获的标准库函数）都会查不到，触发 `unknown variable` 错误。`captured` 是函数能看到外界的唯一窗口。

---

### 4.3 递归自引用：让函数能调用自己

#### 4.3.1 概念说明

Typst 有两种定义函数的写法，递归行为不同：

```typst
#let fact(n) = if n <= 1 { 1 } else { n * fact(n - 1) }   // 写法 A：命名函数
#let fact = (n) => if n <= 1 { 1 } else { n * fact(n - 1) } // 写法 B：箭头函数赋值
```

写法 A（`let f(x) = ...`）里，函数体中的 `fact` 指**函数自己**，天然支持递归。写法 B（`let f = (x) => ...`）里，箭头函数体中的 `fact` 指**外层的 `fact` 变量**——而赋值发生时 `fact` 还没绑定，所以默认**不能**这样递归（这是 4.1 测试用例 `#let f = (x, y) => f` 捕获 `f` 的真实含义：它捕的是当时还可能未定义的 `f`）。

本节只关心**写法 A 的递归是怎么实现的**——答案就在 `eval_closure` 里短短三行。

#### 4.3.2 核心流程

```
eval_closure 第 5 步（在参数绑定之前）：
  if let Some(name) = name {              // name 来自 closure.name()
      vm.define(name, func.clone());      // 把「自己」绑定到自己的作用域
  }
```

`name` 是函数名（只有写法 A `let f(x)=...` 或 `let f = (x)=>...` 的具名闭包才有；匿名 `(x)=>...` 没有）。`func` 是 `eval_closure` 的第一个参数——即正在执行的这个 `Func` 值本身。把它绑定进函数自己的 `vm.scopes`，函数体内出现 `name` 时就能查到自己。

#### 4.3.3 源码精读

[递归自引用的代码](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L678-L681)：

```rust
// Provide the closure itself for recursive calls.
if let Some(name) = name {
    vm.define(name, func.clone());
}
```

要点：

- `name` 来自前面 `match closure.node` 分支里的 `closure.name()`（[func.rs:738](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L738-L745)）：对 `ClosureNode::Closure` 取 AST 上的函数名标识符；匿名闭包返回 `None`。
- `vm.define(name, value)` 见 [vm.rs:50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L50-L52)：它内部调用 `vm.bind(var, Binding::new(value, var.span()))`，而 [`Vm::bind`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L58-L73) 会先 `trace_at`（IDE 追踪）再把绑定塞进 `self.scopes.top`。
- `func.clone()` 克隆的是 `Func`（内部是 `Arc`，克隆廉价），这样函数体内的 `name` 解析到「正在跑的这一个函数值」，调用它就再次进入 `eval_closure`，形成递归。
- 这一步在**参数绑定之前**执行——所以即便函数体里某个命名参数默认值引用了函数名，也能查到（不过默认值在定义期已求好，一般用不到这点）。

> **为何写法 B 不能这样递归？** 因为写法 B `(n) => fact` 是箭头函数，它没有函数名（`name` 是 `None`），不触发这里的 `vm.define`。函数体里的 `fact` 走的是**捕获**路径——而捕获发生在 `let fact = ...` 赋值**之前**，那时 `fact` 尚未绑定，所以要么捕到旧值、要么报错。这正是 4.1 测试 `#let f = (x, y) => f` 把 `f` 列为捕获（而非自引用）的原因。

#### 4.3.4 代码实践

**实践目标：** 对比两种写法的递归能力。

**操作步骤（可运行）：**

1. 编译下面两段代码，观察差异：

   ```typst
   // 写法 A：具名函数，能递归
   #let fact-a(n) = if n <= 1 { 1 } else { n * fact-a(n - 1) }
   #fact-a(5)  // 120

   // 写法 B：箭头函数赋值，递归会出问题
   // #let fact-b = (n) => if n <= 1 { 1 } else { n * fact-b(n - 1) }
   // #fact-b(5)  // 报错：fact-b 在定义时未知
   ```

2. 在 `eval_closure` 里定位第 678-681 行，确认写法 A 的 `fact-a` 有 `name`，因此执行了 `vm.define("fact-a", func.clone())`；写法 B 的箭头函数 `name` 为 `None`，跳过此步。

**需要观察的现象：** 写法 A 正常算出 120；写法 B（取消注释）报 `unknown variable: fact-b`（因为捕获时 `fact-b` 还没绑定）。

**预期结果：** 你能说清递归自引用完全由「具名闭包 + `vm.define(name, func.clone())`」实现，且发生在参数绑定之前。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `vm.define(name, func.clone())` 用 `clone()` 而不是移动 `func`？

**答案：** 因为 `func` 是 `&Func`（借用），无法移动；而且 `func` 在 `eval_closure` 后续还可能被用到（例如记忆化缓存键）。`Func` 内部是 `Arc`，`clone()` 只增加引用计数，开销很小。

**练习 2：** 如果一个具名闭包的函数体里**既没用到自己名字**，`vm.define(name, func.clone())` 还会执行吗？会造成问题吗？

**答案：** 仍会执行（只要 `name` 是 `Some`）。它只是把一个没人用的绑定塞进作用域，不会有副作用（多一次 `trace_at` 和一次 scope 插入），代价可忽略。解释器不做「函数体是否引用了自身」的静态判断，统一处理更简单。

---

### 4.4 参数绑定循环：Pos 与 pattern 解构、Named 默认值

#### 4.4.1 概念说明

`eval_closure` 装好 `Vm`、绑定好自引用后，进入核心环节：**把传入的 `args` 逐个对到形参上**。一个 `match p` 循环遍历所有形参 `params.children()`，按形参类型分四条路：

| 形参类型 | 语法示例 | 处理方式 |
|---------|---------|---------|
| `Pos(Normal Ident)` | `(a, b)` | `args.expect::<Value>(&ident)` 取一个位置实参，`vm.define(ident, ...)` |
| `Pos(其它 pattern)` | `((a, b), (x: y))` | `args.expect::<Value>` 取一个位置实参，交给 `crate::destructure` 拆开绑定 |
| `Spread` | `(..rest)` | 记下 sink 名、先消费掉「多出来的位置实参」（见 4.5） |
| `Named` | `(name: 5)` | `args.named::<Value>` 取命名实参，取不到就用 `defaults` 里的默认值 |

注意前两种共享 `Pos` 这一层：它们都先 `args.expect` 消耗**一个**位置实参，区别只在拿到值后是「直接绑定」还是「先解构再绑定」。

#### 4.4.2 核心流程

参数绑定循环（Spread 分支的完整逻辑见 4.5）：

```
let num_pos_args = args.to_pos().len();          // 数位置实参个数（不消费）
let sink_size    = num_pos_args.checked_sub(num_pos_params); // 多出来的个数（防下溢）

let mut defaults = closure.defaults.iter();      // 默认值迭代器，与 Named 形参按序对齐

for p in params.children() {
    match p {
        Pos(Pattern::Normal(Ident(ident))) =>
            vm.define(ident, args.expect::<Value>(&ident)?)      // 取 1 个位置实参直接绑定
        Pos(pattern) =>
            crate::destructure(vm, pattern, args.expect::<Value>("pattern parameter")?) // 取 1 个再拆
        Spread(spread) => { /* 见 4.5：记 sink 名 + 消费 sink_size 个位置实参 */ }
        Named(named) => {
            let default = defaults.next().unwrap();              // 取对齐的默认值
            let value = args.named::<Value>(&name)?.unwrap_or_else(|| default.clone());
            vm.define(name, value);                              // 有实参用实参，否则用默认值
        }
    }
}
```

两条不变量：

1. **每个 `Pos` 形参恰好消耗 1 个位置实参**（无论是否解构），所以 `num_pos_params` 个 `Pos` 形参总共消耗 `num_pos_params` 个位置实参，多出来的 `num_pos_args - num_pos_params` 个留给 sink（或被 `args.finish` 判为多余报错）。
2. **`defaults` 迭代器与 `Named` 形参严格按源码顺序对齐**——因为 `Closure::eval` 当初就是按 `Named` 出现顺序把默认值 push 进 `Vec` 的。所以这里每个 `Named` 形参 `defaults.next()` 取到的正是它自己的默认值。

#### 4.4.3 源码精读

[参数绑定循环](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L683-L717)（含 4.5 会细讲的 Spread 分支）：

```rust
let num_pos_args = args.to_pos().len();
let sink_size = num_pos_args.checked_sub(closure.num_pos_params);

let mut sink = None;
let mut sink_pos_values = None;
let mut defaults = closure.defaults.iter();
for p in params.children() {
    match p {
        ast::Param::Pos(pattern) => match pattern {
            ast::Pattern::Normal(ast::Expr::Ident(ident)) => {
                vm.define(ident, args.expect::<Value>(&ident)?);
            }
            pattern => {
                crate::destructure(
                    &mut vm,
                    pattern,
                    args.expect::<Value>("pattern parameter")?,
                )?;
            }
        },
        ast::Param::Spread(spread) => {
            sink = Some(spread.sink_ident());
            if let Some(sink_size) = sink_size {
                sink_pos_values = Some(args.consume(sink_size)?);
            }
        }
        ast::Param::Named(named) => {
            let name = named.name();
            let default = defaults.next().unwrap();
            let value =
                args.named::<Value>(&name)?.unwrap_or_else(|| default.clone());
            vm.define(name, value);
        }
    }
}
```

逐分支要点：

- **`Pos(Normal Ident)`**：[`args.expect::<Value>(&ident)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L150-L158) 「消费并转型第一个位置实参」，没有位置实参时返回 `missing argument: {ident}`。错误信息用形参名当 `what`，很友好（例如「missing argument: b」）。
- **`Pos(其它 pattern)`**：同样 `args.expect::<Value>` 取一个位置实参（`what` 是泛指的 `"pattern parameter"`），再把值交给 [`crate::destructure`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L45-L57)。`destructure` 就是 u3-l3 讲过的解构引擎，它按 pattern 把这一个值拆成多个绑定（数组解构、字典解构、spread sink 等），通过回调 `vm.define` 落到作用域。这里复用 `let` 解构同一套代码，保证语义一致。
- **`Named`**：[`args.named::<Value>(&name)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L218-L236) 「按名查找并移除命名实参」，找到返回 `Some`、没找到返回 `None`。`.unwrap_or_else(|| default.clone())` 实现「调用方没传就用默认值」。注意 `args.named` 会移除**所有**同名命名实参并取最后一个（处理重复传参）。

#### 4.4.4 代码实践

**实践目标：** 追踪 `#let f = (a, (b, c), d: 10) => ...` 调用 `#f(1, (2, 3), d: 4)` 时，四个绑定分别怎么发生。

**操作步骤（源码阅读型）：**

1. 先在「定义期」算好 `Closure` 的字段（对照 4.1）：
   - `num_pos_params = 2`（`a` 和 `(b, c)`）
   - `defaults = [10]`（仅 `d` 的默认值）
2. 进入 `eval_closure`，`num_pos_args = 3`（`1`、`(2,3)` 是两个位置实参——注意 `(2, 3)` 作为整体算 1 个），`sink_size = 3 - 2 = 1`。
3. 进入循环，逐分支标注：
   - 形参 `a`（`Pos(Normal Ident)`）：`args.expect` 取走 `1` → `vm.define("a", 1)`。
   - 形参 `(b, c)`（`Pos(pattern)`）：`args.expect` 取走 `(2, 3)`（整个数组）→ `crate::destructure` 拆成 `b=2`、`c=3`。
   - （没有 Spread 形参，跳过）
   - 形参 `d`（`Named`）：`args.named("d")` 取走 `d: 4` → 找到 `Some(4)` → 用 4（不走默认值 10）→ `vm.define("d", 4)`。
4. 思考：如果调用方写成 `#f(1, (2, 3))`（不传 `d`），第 4 步 `args.named("d")` 返回 `None`，于是 `unwrap_or_else` 用 `default.clone()` 即 `10`。

**需要观察的现象：** 即使命名参数 `d` 有默认值，只要调用方传了，就用调用方的值；默认值是**兜底**。

**预期结果：** 你能画出一张「形参 → 分支 → 实参来源 → 最终绑定」的表，并说清 `defaults` 迭代器为何能与 `Named` 形参一一对应。

#### 4.4.5 小练习与答案

**练习 1：** `args.expect` 在位置实参不够时报什么错？为什么用形参名当 `what`？

**答案：** 报 `missing argument: {形参名}`。用形参名（而非泛指的 "positional argument"）能让用户立刻知道缺哪个参数。[`missing_argument`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L160-L174) 还会检查：若调用方把这个名字当**命名参数**传了（如 `f(b: 1)` 而 `b` 是位置参数），会提示「try removing `b:`」，是很好的修复 hint。

**练习 2：** 为什么 pattern 形参 `args.expect` 的 `what` 用泛指的 `"pattern parameter"` 而非某个名字？

**答案：** 因为解构模式没有单一标识符名（如 `(b, c)` 含两个名字），无法用一个名字描述。所以用泛指的 `"pattern parameter"`，报错时说 `missing argument: pattern parameter`。

---

### 4.5 Spread sink：剩余位置参数的两阶段收集

#### 4.5.1 概念说明

`Spread` 形参（`(..rest)`）用来「吃掉」所有剩余的位置实参，把它们打包成一个 `Args`（或数组）绑定到 `rest`。它是 Typst 实现可变参数函数的方式。

收集分**两个阶段**，因为有个先后顺序问题：

- **第一阶段（循环内）：** 在 `Spread` 形参被遍历到时，立刻 `args.consume(sink_size)` 把「多出来的 `sink_size` 个位置实参」从 `args` 里抠出来暂存。**为什么必须现在抠？** 因为循环还要继续，后面的 `Pos`/`Named` 形参也要从 `args` 里取东西，必须先把属于 sink 的位置实参拿走，避免错位。
- **第二阶段（循环后）：** 等 `Pos`/`Named` 都绑完，`args` 里剩下的（主要是命名实参）再 `args.take()` 一次性倒出来，和第一阶段的 `sink_pos_values` 合并，最终绑到 sink 名字上。

`sink_size` 的计算用 `checked_sub`（防下溢）：

\[ \text{sink\_size} = \text{num\_pos\_args} \ominus \text{num\_pos\_params} \]

其中 \(\ominus\) 表示「checked 减法」：若 \(\text{num\_pos\_args} < \text{num\_pos\_params}\)（位置实参比位置形参还少），结果为 `None`——这种情况下其实前面的 `Pos` 形参 `args.expect` 早就因缺参报错了，sink 阶段拿不到也无所谓。

#### 4.5.2 核心流程

两阶段（第一阶段在 4.4 的循环里，第二阶段在循环后）：

```
第一阶段（循环内，Spread 分支）：
  sink = Some(spread.sink_ident())      // 记下 sink 的名字（可能是 None：无名 .. ）
  if let Some(sink_size) = sink_size {  // 有「多余」位置实参才抠
      sink_pos_values = Some(args.consume(sink_size)?)  // 抠走 sink_size 个位置实参暂存
  }

第二阶段（循环后）：
  if let Some(sink) = sink {            // 存在 Spread 形参
      let mut remaining_args = args.take()       // 倒出 args 里剩下的一切（主要是命名实参）
      if let Some(sink_name) = sink {            // sink 有名字（不是裸 ..）
          if let Some(sink_pos_values) = sink_pos_values {
              remaining_args.items.extend(sink_pos_values) // 合并第一阶段抠的位置实参
          }
          vm.define(sink_name, remaining_args)   // 绑定成 Args
      }
      // 若 sink 无名字（裸 ..）：remaining_args 被倒出但丢弃，确保 args 变空以通过 finish
  }
```

注意 `sink` 这个变量的类型是嵌套的 `Option`：外层 `sink: Option<Option<Ident>>`——`Some` 表示「存在 Spread 形参」，内层 `Option<Ident>` 表示「该 sink 是否有名字」（`..rest` 有名字，裸 `..` 无名字）。

#### 4.5.3 源码精读

[第一阶段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L703-L708)（循环内的 Spread 分支）：

```rust
ast::Param::Spread(spread) => {
    sink = Some(spread.sink_ident());
    if let Some(sink_size) = sink_size {
        sink_pos_values = Some(args.consume(sink_size)?);
    }
}
```

[第二阶段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L719-L728)（循环后的 sink 收尾）：

```rust
if let Some(sink) = sink {
    // Remaining args are captured regardless of whether the sink is named.
    let mut remaining_args = args.take();
    if let Some(sink_name) = sink {
        if let Some(sink_pos_values) = sink_pos_values {
            remaining_args.items.extend(sink_pos_values);
        }
        vm.define(sink_name, remaining_args);
    }
}
```

要点：

- [`args.consume(n)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L127-L144) 消费 `n` 个**位置**实参（跳过命名实参），不足 `n` 时报 `not enough arguments`。
- [`args.take()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L250-L255) 用 `mem::take` 把 `args.items` **整体搬空**成新 `Args`。搬完后 `args` 变空，所以紧接着的 `args.finish()` 必然通过——这正是「有 sink 就不会因多余参数报错」的实现。
- 合并后 `vm.define(sink_name, remaining_args)` 把一个 `Args` 值绑到 sink 名字。所以 `rest` 的类型其实是 `Args`（arguments），可以再用 `..` 展开传给别的函数。
- **无名 sink（裸 `..`）的语义**：`spread.sink_ident()` 返回 `None`，于是内层 `if let Some(sink_name)` 不进入，`remaining_args` 被倒出后丢弃。效果是「吸收并忽略多余参数」，且因 `args` 被搬空，`finish` 不会报「unexpected argument」。注释里的 `// ...regardless of whether the sink is named` 正是指 `args.take()` 这一步无论如何都执行。

#### 4.5.4 代码实践

**实践目标：** 追踪 `#let f = (a, ..rest) => rest` 调用 `#f(1, 2, 3, x: 4)` 时 sink `rest` 最终装了什么。

**操作步骤（源码阅读型）：**

1. 定义期：`num_pos_params = 1`（仅 `a`）。
2. 执行期：`num_pos_args = 3`（`1`、`2`、`3`），`sink_size = 3 - 1 = 2`。
3. 循环：
   - `a`（Pos）：`args.expect` 取走 `1` → `a=1`。剩余位置 `[2,3]`、命名 `[x:4]`。
   - `..rest`（Spread）：`sink = Some(Some(rest))`；`sink_size = Some(2)` → `args.consume(2)` 抠走 `2`、`3` → `sink_pos_values = [2, 3]`。剩余位置 `[]`、命名 `[x:4]`。
4. 循环后（第二阶段）：`remaining_args = args.take()` 倒出 `[x:4]`；`sink_name = rest` → `remaining_args.items.extend([2, 3])` → `rest = Args([x:4, 2, 3])`。
5. `args` 现已空，`args.finish()` 通过。

**需要观察的现象：** sink `rest` 是一个 `Args`，**同时**包含了多余的位置实参（`2`、`3`）**和**多余的命名实参（`x: 4`）。你可以接着写 `#(f)(1, 2, 3, x: 4)` 再 `#(..rest)` 转发给别的函数。

**预期结果：** 你能说清 sink 是「位置 + 命名」一起收，且两阶段收集的必要性（先抠位置防错位、后收命名防漏）。

#### 4.5.5 小练习与答案

**练习 1：** 调用 `#f(1)`（只传 1 个位置实参），其中 `f = (a, b, ..rest) => rest`，会发生什么？

**答案：** `num_pos_args=1`，`num_pos_params=2`，`sink_size = 1.checked_sub(2) = None`。循环里 `a` 取走 `1`，`b` 执行 `args.expect` 时已无位置实参 → 报 `missing argument: b`。sink 阶段根本到不了。所以「位置实参不够」先在 `Pos` 形参上报错，而非在 sink。

**练习 2：** 为什么第二阶段要 `args.take()` 而不是只挑命名实参？

**答案：** 因为 sink 的语义是「收集所有剩余参数」，包括位置和命名。第一阶段只抠了 `sink_size` 个位置实参（给后面的 `Pos`/`Named` 让路），抠完后 `args` 里可能还残留命名实参（甚至残留位置实参，如果 sink 不在最后）。`take()` 一股脑倒出最稳妥，配合 `extend(sink_pos_values)` 还原完整剩余参数集，且让 `finish()` 必然通过。

---

### 4.6 收尾与 FlowEvent::Return 的消费

#### 4.6.1 概念说明

参数绑完，就该跑函数体了。但「跑完」不等于「直接返回函数体的产出值」——因为函数体里可能执行了 `return` 提前返回。`eval_closure` 末尾必须**消费 `vm.flow`**，把控制流事件翻译成最终的返回值。

回顾 `FlowEvent::Return(Span, Option<Value>, bool)`（[flow.rs:21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L19-L22)）：第二个字段是「显式返回值」（`return v` 的 `v`，没写就是 `None`），第三个字段是「是否条件 return」（u3-l2 讲过）。函数消费时只看前两个字段。

#### 4.6.2 核心流程

收尾三步：

```
1. args.finish()?              // 检查是否还有没被认领的实参（有 sink 时 args 已空，必通过）

2. let output = body.eval(&mut vm)?   // 求值函数体，得到「正常产出」

3. match vm.flow {             // 消费控制流事件
     Some(Return(_, Some(explicit), _)) => return Ok(explicit),  // return v：用 v
     Some(Return(_, None, _))           => {}                    // 空 return：回落到 output
     Some(flow)                         => bail!(flow.forbidden()),// break/continue 漏进函数：报错
     None                               => {}                    // 无事件：用 output
   }
   Ok(output)                  // 返回函数体产出（空 return / 无事件 都走这里）
```

四条分支的语义：

- **`return v`（有显式值）**：函数的返回值是 `v`，**忽略**函数体正常产出 `output`。
- **空 `return`（无显式值）**：函数的返回值是函数体**正常产出** `output`（等价于「在此处结束并返回目前已算出的值」）。
- **`break`/`continue` 漏进函数**：这俩只在循环里有意义，漂到函数体里属非法，调 `flow.forbidden()` 报 `cannot break/continue outside of loop`。
- **没有任何事件**：正常返回 `output`。

#### 4.6.3 源码精读

[收尾代码](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L730-L742)：

```rust
// Ensure all arguments have been used.
args.finish()?;

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

要点：

- [`args.finish()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/args.rs#L259-L267) 在有 sink 时 `args` 已被 `take()` 清空，必然通过；在**无 sink** 时，若调用方传了多余实参（形参没声明），这里报 `unexpected argument: {name}`。所以「参数个数/名字校验」的最终关口在这里。
- `body.eval(&mut vm)` 求值函数体。函数体若执行了 `return v`，`FuncReturn::eval`（[flow.rs:219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L219)）会把 `FlowEvent::Return(span, value, false)` 写进 `vm.flow`，但**不会中断** `body.eval`（它只是返回 `Value::None`，让求值继续走完——这是 u3-l2 讲的「带外信号」机制）。所以 `output` 可能是个无意义的 `None`（被 return 截断后的剩余求值），这正是为什么 `Some(Return(_, Some(explicit), _))` 要**忽略 output 直接返回 explicit**。
- [`flow.forbidden()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L26-L38) 对 `Break`/`Continue`/`Return` 分别给 `cannot ... outside of loop/function` 错误。在 `eval_closure` 这里，`Return` 已被前两条分支处理掉，落到 `Some(flow)` 的只可能是 `Break`/`Continue`，故实际报的是「cannot break/continue outside of loop」。

> 这个 `match` 是 `vm.flow` 的**最终消费点**——函数是 `Return` 事件的终点（u3-l2 讲过 `Return` 冒泡穿循环、最终由函数消费）。消费后 `eval_closure` 返回，`vm` 随之销毁，事件不会泄漏到调用方。

#### 4.6.4 代码实践

**实践目标：** 验证三种返回语义：`return v`、空 `return`、无 `return`。

**操作步骤（可运行）：**

1. 编译下面三段，预测输出：

   ```typst
   // (a) return v：用 v，忽略函数体后续产出
   #let f() = { return 1; 2 }
   #f()  // 1（不是 2）

   // (b) 空 return：用函数体已算出的值
   #let g() = { 1; return; 2 }
   #g()  // 1（return 前的最后产出是 1）

   // (c) 无 return：用函数体产出
   #let h() = { 1; 2 }
   #h()  // 2（代码块 join 到 2）
   ```

2. 对照 `eval_closure` 末尾的 `match vm.flow` 标注每段命中的分支：
   - (a) `vm.flow = Return(_, Some(1), _)` → 命中第一条 → 返回 `1`（`output` 被忽略）。
   - (b) `vm.flow = Return(_, None, _)` → 命中第二条 → 空分支，回落 `Ok(output)`；`output` 是函数体在 `return` 前的产出 `1`。
   - (c) `vm.flow = None` → 命中第四条 → `Ok(output)`，`output` 是 `{1; 2}` 的 join 结果 `2`。

**需要观察的现象：** (a) 返回 `1` 而非 `2`，证明 `return v` 的 `v` 覆盖了函数体后续产出；(b) 空 `return` 不覆盖、沿用已有产出。

**预期结果：** 你能说清 `Some(Return(_, Some(explicit), _))` 为何要**忽略** `output`（因为 return 之后的产出是无意义的「带外信号副产品」），而 `None`/空 `return` 为何要**沿用** `output`。

#### 4.6.5 小练习与答案

**练习 1：** 如果函数体里写了 `break`（但不在任何循环里），`eval_closure` 末尾会怎样？

**答案：** `body.eval` 期间 `LoopBreak::eval` 把 `FlowEvent::Break` 写进 `vm.flow`。末尾 `match` 中 `Return` 分支都不匹配，落到 `Some(flow) => bail!(flow.forbidden())`，`forbidden()` 对 `Break` 报 `cannot break outside of loop`。

**练习 2：** 为什么 `args.finish()` 放在 `body.eval` **之前**，而不是函数开头？

**答案：** 因为 `finish()` 检查的是「形参绑定后是否还有没被认领的实参」，而绑定发生在循环里。必须在循环（+ sink 收尾）之后才能判断剩余。把它放在 `body.eval` 之前是为了「先确保参数全对，再跑函数体」——避免函数体已经产生副作用（如打印、修改全局状态）后才发现参数错误。

---

## 5. 综合实践

**任务：** 下面是一个综合了本讲所有机制的函数。请完整追踪它在「定义」和「调用」两个阶段的全过程。

```typst
#let base = 100
// 具名闭包：位置参数 a、解构参数 (b, c)、命名参数 d（默认值用到捕获的 base）、spread sink rest
#let combine(a, (b, c), d: base + 1, ..rest) => {
  if a < 0 { return (-1, -1, -1) }
  (a + b + c + d, ..rest)
}

#combine(10, (20, 3), x: 5, y: 6)
```

**要求：** 分两阶段作答。

**阶段一（定义期，`Closure::eval`）：**

1. 列出 `Closure` 的四个字段：
   - `num_pos_params` = ?（答：2，`a` 和 `(b, c)`）
   - `defaults` = ?（答：`[101]`——`d` 的默认值 `base + 1` 在定义点求值，`base=100`，得 `101`）
   - `captured` 捕获了哪些变量？为什么？（答：至少捕获 `base`，因为默认值 `base + 1` 引用了它；函数体 `if a < 0 { ... }`、`(a + b + c + d, ..rest)` 只用参数，不引用外层。所以 `captured = { base: 100 }`，可能还含 `combine` 自身——但具名闭包的自引用由 `eval_closure` 的 `vm.define` 提供，不靠捕获。）
   - `node` 存的是什么？（答：整个闭包语法节点的克隆，函数体不求值。）

**阶段二（执行期，`eval_closure`）：**

2. 重建作用域：`Scopes::new(None)` + `scopes.top = captured`（含 `base=100`）。
3. 递归自引用：`name = Some(combine)` → `vm.define("combine", func)`。
4. 算 `num_pos_args` 与 `sink_size`：调用 `combine(10, (20, 3), x: 5, y: 6)` 的位置实参是 `10` 和 `(20, 3)`，共 2 个 → `num_pos_args = 2`；`sink_size = 2 - 2 = 0`。
5. 参数绑定循环逐分支标注：
   - `a`（Pos Normal Ident）：`args.expect` 取 `10` → `a = 10`。
   - `(b, c)`（Pos pattern）：`args.expect` 取 `(20, 3)` → `destructure` 拆成 `b=20`、`c=3`。
   - `..rest`（Spread）：`sink = Some(Some(rest))`；`sink_size = Some(0)` → `args.consume(0)` 抠走 0 个 → `sink_pos_values = Some([])`。
   - `d`（Named）：`args.named("d")` 返回 `None`（调用方没传 `d:`）→ 用默认值 `101` → `d = 101`。
6. sink 第二阶段：`remaining_args = args.take()` 倒出 `[x:5, y:6]`；`extend([])` → `rest = Args([x:5, y:6])`。
7. `args.finish()` 通过（已空）。
8. 跑函数体：`a=10 >= 0`，不进 `return` 分支；求值 `(a+b+c+d, ..rest)` = `(10+20+3+101, x:5, y:6)` = `(134, x: 5, y: 6)`。`vm.flow = None`。
9. 末尾 `match`：`None` 分支 → `Ok(output)` = `(134, x: 5, y: 6)`。

**进阶思考：** 如果把调用改成 `#combine(-1, (20, 3))`，复述阶段二的第 8、9 步：
- `a=-1 < 0` → 执行 `return (-1, -1, -1)` → `vm.flow = Return(_, Some((-1,-1,-1)), false)`。
- 函数体后续 `(a+b+c+d, ..rest)` 因 `return` 截断不参与返回（实际还会被求值到 `None` 副产品，但被忽略）。注意此时 `b`、`c` 仍会被绑定（绑定在循环阶段、`return` 之前），但 `rest` 的 `x:`/`y:` 没传，`rest` 为空 `Args`。
- 末尾 `match`：`Some(Return(_, Some((-1,-1,-1)), _))` → 返回 `(-1, -1, -1)`，`output` 被忽略。

**预期产出：** 一份完整的两阶段追踪表。完成后，你应该能把「定义期冻什么、执行期怎么重建环境、四类参数怎么绑、sink 两阶段怎么收、Return 三分支怎么消费」串成一条无断裂的链路。

## 6. 本讲小结

- `Closure::eval` 在**定义期**做三件事：求命名参数默认值（`defaults`）、收集捕获变量（`captured`，靠 `CapturesVisitor`）、统计位置参数个数（`num_pos_params`），函数体只存语法节点不求值，最终包成 `Value::Func`。
- `eval_closure` 在**执行期**用 `Scopes::new(None) + scopes.top = closure.captured` 重建一个干净作用域，**绝不泄漏调用点**——函数能看到的外界只有定义期冻结的捕获快照。
- 递归自引用由具名闭包的 `vm.define(name, func.clone())` 实现（在参数绑定之前），所以 `let f(x)=...` 能递归、箭头函数 `(x)=>...` 不能（后者靠捕获，而捕获发生在赋值前）。
- 参数绑定循环按 `Pos(Normal Ident)` / `Pos(pattern)` / `Spread` / `Named` 四类分派：前两者都 `args.expect` 取一个位置实参（后者再 `crate::destructure` 拆开），`Named` 用 `args.named` 取命名实参取不到则用 `defaults` 兜底。
- Spread sink 两阶段收集：循环内先 `args.consume(sink_size)` 抠出多余位置实参（防后续绑定错位），循环后 `args.take()` 倒出剩余命名实参再合并绑定；`sink_size = num_pos_args ⊖ num_pos_params` 用 `checked_sub` 防下溢。
- `eval_closure` 末尾消费 `vm.flow`：`return v` 用 `v`（忽略函数体产出）、空 `return` 与无事件沿用函数体产出 `output`、`break`/`continue` 漏入函数则 `forbidden()` 报错；`args.finish()` 是参数个数/名字校验的最终关口。

## 7. 下一步学习建议

- 下一讲 **u4-l4 CapturesVisitor：闭包变量捕获** 会拆开本讲当作黑盒的 `CapturesVisitor`，讲清它如何用「内部/外部双 `Scopes`」判断一个标识符是「绑定」还是「捕获」，以及 `let`/`for`/闭包参数/`import`/字段访问如何更新内部作用域。学完它，本讲 4.1 的 `captured` 从何而来就完全清晰了。
- 若想了解 `eval_closure` 上的 `#[comemo::memoize]` 如何缓存函数调用、`Tracked<World>` 等参数如何参与缓存键判等，可跳读 **u6-l3 递归安全、栈增长与缓存**，那里会把 `Route::extend`、`stacker::maybe_grow`、comemo 三道运行时安全防线讲透。
- 对解构参数 `crate::destructure` 的内部细节（数组 spread sink「按数量截取」、字典 spread sink「收集未命名键」）感兴趣，可重温 **u3-l3 let 绑定与解构赋值**，本讲复用的正是那套 `destructure_impl` 引擎。
