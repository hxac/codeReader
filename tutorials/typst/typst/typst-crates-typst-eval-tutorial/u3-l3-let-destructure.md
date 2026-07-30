# let 绑定与解构赋值

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `let x = 1` 与 `let f(x) = 1` 在求值阶段被区分成哪两种 kind，以及各自如何处理「缺失初始化值」。
- 读懂 `destructure` / `destructure_impl` 这对函数：理解它们如何用一个泛型回调 `f`，把同一套「按模式拆分值」的逻辑复用到「`let` 创建新绑定」和「解构赋值改写已有位置」两种场景。
- 解释数组解构中 spread sink「按数量截取中间元素」和字典解构中 spread sink「收集未被显式命名的键」这两种截然不同的语义。
- 看懂 `wrong_number_of_elements` 如何生成一条带 hint 的高质量错误诊断。

本讲只读 `src/binding.rs` 一个文件，并少量借用 `src/vm.rs` 的 `define` 与 `src/access.rs` 的 `Access` trait 作为对照。

## 2. 前置知识

本讲建立在 u2-l1（字面量与标识符求值）之上。在继续前，请确认你已理解以下概念：

- **`Eval` trait 与 `Vm` 虚拟机**：每个 AST 节点实现 `fn eval(self, vm: &mut Vm) -> SourceResult<Output>`，`Vm` 持有求值状态（作用域栈 `scopes`、控制流事件 `flow` 等）。
- **`vm.define` / `vm.scopes`**：把一个值绑定到当前作用域的标识符上。这一步由 `Vm::define` 完成，它最终调用 `scopes.top.bind(...)` 插入到最顶层作用域。
- **标识符查找**：`Ident::eval` 通过 `vm.scopes.get(&self)` 逐层查找绑定。本讲关注「写入」方向，即如何创建与修改这些绑定。
- **控制流事件 `FlowEvent`**（u3-l2）：`break` / `continue` / `return` 会向 `vm.flow` 写入一个事件。本讲会看到 `let` 求值时如何检查它。

如果你还不熟悉「解构（destructuring）」这个语言特性，下面先用一句话概括：解构就是把一个复合值（数组或字典）**按位置或按键名**拆开，分别赋给多个变量。例如（示例代码）：

```typst
#let (a, b, c) = (1, 2, 3)   // a=1, b=2, c=3
#let (x, ..rest) = (1, 2, 3) // x=1, rest=(2, 3)
#let (name:, age:) = (name: "Ada", age: 30, city: "London") // name="Ada", age=30
```

typst-eval 用一个叫 `Pattern`（模式）的 AST 类型来表达 `=` 左边（或 `let` 关键字后面）这种「待绑定的形状」，本讲的核心就是把一个模式「落实」到具体值上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/binding.rs` | 本讲主角。实现 `LetBinding`、`DestructAssignment` 的 `Eval`，以及解构引擎 `destructure` / `destructure_impl` / `destructure_array` / `destructure_dict` 和错误构造 `wrong_number_of_elements`。 |
| `src/vm.rs` | 提供 `Vm::define` / `Vm::bind`，是「创建新绑定」的实际落点。 |
| `src/access.rs` | 提供 `Access` trait，是「可变改写已有位置」的实际落点，供解构赋值复用。 |
| `crates/typst-syntax/src/ast.rs` | 定义 `Pattern`、`Destructuring`、`DestructuringItem`、`Spread`、`LetBindingKind` 等 AST 类型，是解构逻辑操作的数据结构。 |

## 4. 核心概念与源码讲解

### 4.1 let 绑定：两种 kind 与初始化值处理

#### 4.1.1 概念说明

Typst 里写 `let` 有两种外形，它们在求值阶段被归为两种 `LetBindingKind`：

- **普通绑定 `let x = 1` / `let (a, b) = ...`**：等号左边是一个**模式（Pattern）**，归类为 `Normal(pattern)`。
- **函数语法糖 `let f(x) = 1`**：它是 `let f = (x) => 1` 的简写，归类为 `Closure(ident)`，此时绑定目标只是一个普通标识符（函数名）。

`LetBinding::eval` 要处理三件事：

1. 求出右侧的初始化值（若没有初始化值，默认是 `none`）。
2. 如果右侧求值过程中触发了控制流事件（例如 `let x = return`），就**不绑定**，直接返回。
3. 根据 kind 分派：普通模式走解构引擎；闭包语法糖直接 `define` 函数名。

#### 4.1.2 核心流程

```
LetBinding::eval(self, vm):
  1. value = self.init() 求值，若无 init 则 value = Value::None
  2. 若 vm.flow.is_some(): 直接返回 None（放弃绑定）
  3. match self.kind():
       Normal(pattern)   -> destructure(vm, pattern, value)   // 进入解构引擎
       Closure(ident)    -> vm.define(ident, value)           // 直接绑定函数名
  4. 返回 Value::None（let 语句本身不产生值）
```

注意 `let` 语句本身求值结果是 `Value::None`——它是一个「副作用语句」，价值体现在它对作用域的修改，而非返回值。

#### 4.1.3 源码精读

[src/binding.rs:9-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L9-L28) 是 `LetBinding` 的 `Eval` 实现，对应上面的三步：

```rust
let value = match self.init() {
    Some(expr) => expr.eval(vm)?,
    None => Value::None,
};
if vm.flow.is_some() {
    return Ok(Value::None);
}

match self.kind() {
    ast::LetBindingKind::Normal(pattern) => destructure(vm, pattern, value)?,
    ast::LetBindingKind::Closure(ident) => vm.define(ident, value),
}

Ok(Value::None)
```

要点解读：

- `self.init()` 返回 `Option<Expr>`。形如 `#let x;`（没有 `=`）时它返回 `None`，于是 `value` 取 `Value::None`——这正是 Typst 里「未初始化变量等于 `none`」的实现来源。
- `if vm.flow.is_some()` 是一道安全闸：如果右侧求值产生了一个尚未被消费的控制流事件（例如右侧是一个 `return`），就立刻放弃绑定。否则后续的 `define` 会创建一个本不该存在的变量。
- `vm.define(ident, value)` 落到 [src/vm.rs:50-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L50-L52)，它内部构造一个 `Binding` 并调用 `Vm::bind`（[src/vm.rs:58-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L58-L73)），最终插入 `scopes.top`。

`kind()` 的区分逻辑在语法层（typst-syntax），它判断「`let` 后面那棵子树是不是一个 `Closure`」：是则取闭包名归为 `Closure`，否则把整棵树当作 `Normal(Pattern)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「缺失初始化值」与「flow 闸」两点。

**操作步骤**：

1. 阅读 `LetBinding::eval` 的前 6 行，确认 `self.init()` 为 `None` 时 `value` 的取值。
2. 在脑中跟踪下面两段示例代码（示例代码，待本地验证）：

   ```typst
   #let a;       // init() 为 None -> a 绑定为 none
   #let b = 5;   // init() 为 Some -> b 绑定为 5
   ```

3. 思考：在一个函数体内写 `#let x = return;` 会发生什么？——右侧 `FuncReturn::eval` 会把 `vm.flow` 设为某个 `Return` 事件并返回 `None`；随后 `if vm.flow.is_some()` 命中，`x` **不会被创建**，`LetBinding::eval` 直接返回。

**需要观察的现象**：第 1 步确认 `value = Value::None`；第 3 步确认 flow 闸阻止了绑定。

**预期结果**：`#let a;` 后 `a` 为 `none`；含 `return` 的 `let` 不创建变量、`return` 继续向上冒泡被函数消费（参见 u3-l2）。

#### 4.1.5 小练习与答案

**练习 1**：`let f(x) = x + 1` 走的是哪条 kind 分支？最终调用了哪个方法？
**答案**：走 `Closure(ident)` 分支，`ident` 是函数名 `f`，调用 `vm.define(f, value)`，其中 `value` 是由闭包体构造出的 `Func`。

**练习 2**：为什么 `let` 语句的 `Eval::Output` 是 `Value`，但函数体里写多行 `let` 后还能正常返回别的值？
**答案**：`let` 返回 `Value::None`，在代码块里经 `eval_code` 用 `ops::join` 累加（`None` 是 join 的单位元），所以它不影响代码块的最终返回值，仅以副作用修改作用域。

---

### 4.2 解构分派引擎：destructure 与 destructure_impl

#### 4.2.1 概念说明

模式 `Pattern` 有四种形态（见 `crates/typst-syntax/src/ast.rs`）：

- `Normal(Expr)`：单个表达式，如 `x`。
- `Placeholder`：下划线 `_`，表示「忽略这个值」。
- `Parenthesized`：带括号的子模式，括号透明。
- `Destructuring`：真正的解构，如 `(a, b, ..rest)` 或 `(name:, age:)`。

把一个值「落实」到一个模式上，是一个**递归**过程：遇到括号就剥掉继续，遇到解构就交给数组/字典专用函数，遇到单个表达式/占位符就交给一个回调 `f` 来决定「到底怎么绑定」。

typst-eval 的精妙之处在于：它把「怎么绑定」这一步抽象成一个**泛型回调** `f: Fn(&mut Vm, ast::Expr, Value) -> SourceResult<()>`。同一个 `destructure_impl` 因此能服务两种完全不同的语义——这正是 `let`（新建绑定）与解构赋值（改写已有位置）能共享代码的关键。

#### 4.2.2 核心流程

```
destructure_impl(vm, pattern, value, f):
  match pattern:
    Normal(expr)         -> f(vm, expr, value)              // 叶子：交给回调
    Placeholder(_)       -> {}                              // 忽略
    Parenthesized(p)     -> 递归 destructure_impl(p)        // 剥括号
    Destructuring(d)     -> match value:
        Value::Array  -> destructure_array(...)
        Value::Dict   -> destructure_dict(...)
        其它          -> 报错 "cannot destructure <类型>"
```

`destructure`（用于 `let`）和 `DestructAssignment` 各自传进不同的回调 `f`：

- `destructure` 的 `f`：只接受 `Ident`，调用 `vm.define` **创建新绑定**；遇到非标识符（如字段访问）就报错。
- `DestructAssignment` 的 `f`：调用 `expr.access(vm)` 拿到 `&mut Value` 再写入，**改写已有位置**。

#### 4.2.3 源码精读

[src/binding.rs:45-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L45-L57) 是面向 `let` 的薄包装 `destructure`，它传入「只认标识符、调用 `vm.define`」的回调：

```rust
pub(crate) fn destructure(vm, pattern, value) -> SourceResult<()> {
    destructure_impl(vm, pattern, value, &mut |vm, expr, value| match expr {
        ast::Expr::Ident(ident) => {
            vm.define(ident, value);
            Ok(())
        }
        _ => bail!(expr.span(), "cannot assign to this expression"),
    })
}
```

[src/binding.rs:60-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L60-L82) 是分派主体 `destructure_impl`：

```rust
match pattern {
    ast::Pattern::Normal(expr) => f(vm, expr, value)?,
    ast::Pattern::Placeholder(_) => {}
    ast::Pattern::Parenthesized(parenthesized) => {
        destructure_impl(vm, parenthesized.pattern(), value, f)?;
    }
    ast::Pattern::Destructuring(destruct) => match value {
        Value::Array(value) => destructure_array(vm, destruct, value, f)?,
        Value::Dict(value) => destructure_dict(vm, destruct, value, f)?,
        _ => bail!(pattern.span(), "cannot destructure {}", value.ty()),
    },
}
```

要点：

- `Placeholder` 分支什么都不做——这实现了 `_`「丢弃该值」的语义，且不创建任何绑定。
- `Normal` 分支把决定权完全交给回调 `f`，所以 `destructure_impl` 本身不关心「是新建还是改写」。
- 当模式是 `Destructuring` 但值既不是数组也不是字典时（如对 `none` 或数字解构），用 `value.ty()` 报出清晰的类型错误。

#### 4.2.4 代码实践

**实践目标**：理解回调 `f` 是如何让同一套引擎服务两种语义的。

**操作步骤**：

1. 对照 [src/binding.rs:45-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L45-L57)（`let` 的回调）与 4.5 节将读到的 [src/binding.rs:35-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L35-L39)（赋值的回调），在纸上画一张表：

   | 场景 | 回调 f 做什么 | 叶子能是什么 |
   |------|--------------|--------------|
   | `let (a, b) = ...` | `vm.define(ident, value)` 创建新绑定 | 仅 `Ident` |
   | `(a, b) = ...`（赋值） | `expr.access(vm)` 后写回 | 任何实现 `Access` 的表达式 |

2. 思考为什么 `let (a, b.field) = (1, 2)` 在 Typst 里非法、而 `(a, b.field) = (1, 2)` 合法：前者回调遇到 `b.field`（字段访问，非 `Ident`）命中 `_` 分支报错；后者回调用 `Access` 能取到 `b.field` 的可变引用。

**需要观察的现象**：两个回调签名相同（都是 `Fn(&mut Vm, ast::Expr, Value) -> SourceResult<()>`），但一个 `define`、一个 `access`。

**预期结果**：你应当能总结出「`destructure_impl` 只负责按模式分派，绑定语义全部由回调决定」这一设计。

#### 4.2.5 小练习与答案

**练习 1**：`#let (_, x) = (9, 8)` 中，`9` 去哪了？
**答案**：第一个模式项是 `Placeholder`，`destructure_impl` 的 `Placeholder` 分支什么都不做，`9` 被丢弃，不产生任何绑定。

**练习 2**：`destructure_impl` 为何要把 `f` 设计成回调，而不是直接在里面写 `vm.define`？
**答案**：为了复用。`let`（新建）和解构赋值（改写）需要相同的「按模式拆分」骨架，但叶子处的绑定动作不同。回调把这个差异点抽出来，让 `destructure_impl` / `destructure_array` / `destructure_dict` 保持单一、可复用。

---

### 4.3 数组与字典解构：spread sink 的两种语义

#### 4.3.1 概念说明

`Destructuring` 模式由若干 `DestructuringItem` 组成，每项是三种之一（见 `ast.rs`）：

- `Pattern(Pattern)`：一个子模式。
- `Named(Named)`：`键: 子模式`，**仅字典解构**用，按键名取值。
- `Spread(Spread)`：`..sink`，收集「剩余」部分，即 spread sink。

数组解构与字典解构的 spread sink 语义完全不同，这是本讲最重要的对比：

- **数组 spread sink**：「按数量截取」。它收集**位置上落在中间**的元素，个数由「元素总数 − 其他具名项个数」算出。
- **字典 spread sink**：「收集未被显式命名的键」。它把所有**没有在模式里显式出现的键**及其值收进一个新字典。

#### 4.3.2 核心流程

**数组解构 `destructure_array`**：

```
len = value 长度; i = 0
for item in pattern.items:
  Pattern(p)     -> v = value[i]; 递归 destructure_impl(p, v); i += 1
                   （越界则报 wrong_number_of_elements）
  Spread(spread) -> sink_size = (1 + len) - items 总数
                   sink = value[i .. i+sink_size]   （算不出/越界则报错）
                   f(sink.sink_expr(), Array(sink)); i += sink_size
  Named(_)       -> 报错 "cannot destructure named pattern from an array"
最终 if i < len: 报错（元素太多）
```

**字典解构 `destructure_dict`**：

```
used = {}  // 已显式命名的键集合
for item in pattern.items:
  Pattern(Normal(Ident)) -> 按标识符名从 dict 取值；f(define/access); used 插入该名
  Named(named)           -> 按 named.name() 取值，递归解构到 named.pattern(); used 插入 name
  Spread(spread)         -> 记下 sink_expr（先不处理）
  Pattern(其它)          -> 报错 "cannot destructure unnamed pattern from dictionary"
最后若有 sink:
  剩余 dict = { k:v | k 不在 used 中 }
  f(sink_expr, Dict(剩余))
```

#### 4.3.3 源码精读

数组 spread sink 的核心是「算出截取多少」。[src/binding.rs:105-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L105-L115)：

```rust
ast::DestructuringItem::Spread(spread) => {
    let sink_size = (1 + len).checked_sub(destruct.items().count());
    let sink = sink_size.and_then(|s| value.as_slice().get(i..i + s));
    let (Some(sink_size), Some(sink)) = (sink_size, sink) else {
        bail!(wrong_number_of_elements(destruct, len));
    };
    if let Some(expr) = spread.sink_expr() {
        f(vm, expr, Value::Array(sink.into()))?;
    }
    i += sink_size;
}
```

`sink_size = (1 + len) − items总数`。`items().count()` 包含 spread 这一项本身，因此「其它具名项个数 = 总数 − 1」，于是 `sink_size = len − (总数−1) = (1+len) − 总数`。`checked_sub` 在结果为负（元素不够）时返回 `None`，触发 `wrong_number_of_elements`。这样 `(a, ..rest, b)` 解构 `[1,2,3,4]` 时，`rest` 恰好拿到中间的 `[2, 3]`。

字典 spread sink 的核心是「扣除已用键」。[src/binding.rs:164-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L164-L172)：

```rust
if let Some(expr) = sink {
    let mut sink = Dict::new();
    for (key, value) in dict {
        if !used.contains(key.as_str()) {
            sink.insert(key, value);
        }
    }
    f(vm, expr, Value::Dict(sink))?;
}
```

`used`（[src/binding.rs:139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L139) 初始化为空 `FxHashSet`）在遍历模式项时收集了所有显式取过的键（简写 `Pattern(Ident)` 用标识符名作键、`Named` 用 `name()` 作键）。最后把字典里**不在 `used` 中**的键值对全收进 sink。这就是「收集未被显式命名的键」。

另外注意字典解构里两种取键写法的差别（[src/binding.rs:144-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L144-L156)）：

- 简写 `Pattern(Normal(Ident))`：键名 == 变量名，如 `(name,)` 表示取键 `"name"` 绑给变量 `name`。
- `Named(named)`：键名与绑定可以不同，还能继续嵌套解构，如 `(name: n)` 表示取键 `"name"` 绑给变量 `n`，`(coords: (x, y))` 还能再拆。

任何「不是单个标识符」的裸 `Pattern`（如想直接写 `(coords) = ...` 取键）会落到 [src/binding.rs:158-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L158-L160) 报 `cannot destructure unnamed pattern from dictionary`——字典项必须要么是标识符简写、要么带 `键:` 命名。

#### 4.3.4 代码实践

**实践目标**：亲手推演两种 spread sink 的结果。

**操作步骤**：

1. 对数组例子 `(a, ..rest, b) = (1, 2, 3, 4)`（示例代码）逐步代入 `destructure_array`：
   - `len = 4`，`items().count() = 3`，`sink_size = (1+4) − 3 = 2`。
   - 处理 `a`：`i=0`，取 `value[0]=1`，`i=1`。
   - 处理 `..rest`：`sink = value[1..3] = (2,3)`，`rest=(2,3)`，`i=3`。
   - 处理 `b`：取 `value[3]=4`，`i=4`。
   - 末尾 `i < len` 不成立，通过。

2. 对字典例子 `(name:, ..rest) = (name: "Ada", age: 30, city: "London")`（示例代码）代入 `destructure_dict`：
   - 处理 `name:`：从 dict 取 `"Ada"`，`used = {"name"}`。
   - 处理 `..rest`：记下 sink。
   - 最后构造 sink：遍历 dict，`age` 与 `city` 不在 `used`，故 `rest = (age: 30, city: "London")`。

**需要观察的现象**：数组 sink 由「算术算个数」决定；字典 sink 由「集合做差」决定。

**预期结果**：数组 `rest = (2, 3)`；字典 `rest = (age: 30, city: "London")`。若本地运行 Typst，可用 `#context [(a, ..rest, b)]`（示意）等方式打印验证；不便于运行时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`(a, ..rest, b, c) = (1, 2, 3)` 时 `rest` 是什么？
**答案**：`items().count() = 4`，`sink_size = (1+3) − 4 = 0`，`rest = ()`（空数组）。spread 可以收集到零个元素。

**练习 2**：`(a, b) = (1, 2, 3)`（无 spread，元素偏多）会怎样？
**答案**：处理完 `a`、`b` 后 `i = 2`，末尾 `if i < len`（2 < 3）成立，报 `wrong_number_of_elements`（提示「too many elements」）。

**练习 3**：字典解构里 `(x: y)` 与 `(x)` 有何区别？
**答案**：`(x: y)` 是 `Named`，取键 `"x"` 的值绑给变量 `y`（键名与变量名不同，且可再嵌套）；`(x)` 是简写 `Pattern(Ident)`，取键 `"x"` 绑给变量 `x`（键名必须等于变量名）。

---

### 4.4 解构赋值与元素数量错误诊断

#### 4.4.1 概念说明

除了 `let`，Typst 还支持**解构赋值**：`(a, b) = (1, 2)`、`obj.field = 3`。它不创建新变量，而是改写**已存在的可变位置**。这类语法被解析成 `DestructAssignment` 节点。

与 `let` 的本质区别在于叶子处的动作：

- `let` 走 `destructure` → `vm.define`（在当前作用域**新建**绑定，且叶子必须是标识符）。
- 解构赋值走自己的回调 → `expr.access(vm)` 拿到 `&mut Value` 再写回（**改写**已有位置，叶子可以是任何实现 `Access` 的表达式，如变量、字段访问）。

本节还要看 `wrong_number_of_elements`：一个标了 `#[cold]` 的诊断构造函数，它把「数组长度与模式项数不匹配」翻译成一条带定量描述和 hint 的好错误。

#### 4.4.2 核心流程

**解构赋值 `DestructAssignment::eval`**：

```
value = self.value().eval(vm)            // 求右侧
destructure_impl(vm, self.pattern(), value, |vm, expr, value| {
    location = expr.access(vm)?          // 取可变引用（Access trait）
    *location = value                    // 写回
})
返回 Value::None
```

**`wrong_number_of_elements`**：

```
统计 pattern: count = 普通项数; spread = 是否含 spread
quantifier = if len > count { "too many" } else { "not enough" }
expected = 按 (spread, count) 组合生成人类可读描述
返回 error!(span, "{quantifier} elements to destructure"; hint: ...)
```

#### 4.4.3 源码精读

[src/binding.rs:30-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L30-L42) 是解构赋值：

```rust
fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output> {
    let value = self.value().eval(vm)?;
    destructure_impl(vm, self.pattern(), value, &mut |vm, expr, value| {
        let location = expr.access(vm)?;
        *location = value;
        Ok(())
    })?;
    Ok(Value::None)
}
```

注意它**复用了同一个 `destructure_impl`**，只是换了回调。`expr.access(vm)` 来自 `Access` trait（[src/access.rs:9-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L9-L12)），其 `Expr` 实现（[src/access.rs:14-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L14-L27)）支持 `Ident`、`Parenthesized`、`FieldAccess`、`FuncCall`（访问器方法），其余表达式求值后报 `cannot mutate a temporary value`。这就是为什么 `(a, obj.field) = (1, 2)` 合法而 `let (a, obj.field) = ...` 非法——前者用 `access`，后者用 `define`。

另外，与 `let` 不同，`DestructAssignment::eval` 在求出 `value` 后**没有** `if vm.flow.is_some()` 的闸门。这是一个值得注意的差异：解构赋值假定右侧不会是控制流语句（语法上 `(a,b) = return` 这类并不常见，且语义上赋值目标要求是已有位置）。

[src/binding.rs:177-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L177-L209) 是错误诊断构造器：

```rust
#[cold]
fn wrong_number_of_elements(destruct, len) -> SourceDiagnostic {
    // 统计 count（普通项）与 spread（是否含 sink）
    let quantifier = if len > count { "too many" } else { "not enough" };
    let expected = match (spread, count) {
        (true, 1) => "at least 1 element".into(),
        (true, c) => eco_format!("at least {c} elements"),
        (false, 0) => "an empty array".into(),
        (false, 1) => "a single element".into(),
        (false, c) => eco_format!("{c} elements",),
    };
    error!(destruct.span(), "{quantifier} elements to destructure";
        hint: "the provided array has a length of {len}, but the pattern expects {expected}";
    )
}
```

它体现 typst-eval 一贯的诊断哲学（错误信息 + 精确定位 + 修复提示三要素）：

- **信息**：`too many` / `not enough elements to destructure`。
- **定量 hint**：把实际长度 `len` 与「模式期望」并列，期望值还随 `spread` 改措辞（有 spread 时说 `at least N`，无 spread 时说 `N elements`，并处理单复数与 `empty`/`single` 特例）。
- **定位**：用 `destruct.span()` 覆盖整个模式。
- **`#[cold]`**：标记此函数冷路径，引导编译器把错误处理代码移出热路径，保持正常解构的性能。

#### 4.4.4 代码实践

**实践目标**：追踪一次解构赋值的完整路径，并理解错误诊断的措辞。

**操作步骤**：

1. 跟踪 `(a, b) = (1, 2)`（假设 `a`、`b` 已存在）：`DestructAssignment::eval` → 求 `(1,2)` 得 `Array` → `destructure_impl(Destructuring)` → 因值是 `Array` 进 `destructure_array` → 每个叶子调回调 `expr.access(vm)` 取 `a`/`b` 的 `&mut Value` 再写回 `1`/`2`。
2. 思考 `(a, b) = (1, 2, 3)`：元素偏多。跟踪到 `destructure_array` 末尾 `if i < len`（2 < 3）成立，调 `wrong_number_of_elements(destruct, 3)`。代入统计：`count=2, spread=false`，故 `quantifier = "too many"`，`expected = "2 elements"`，最终错误正文为「too many elements to destructure」，hint 为「the provided array has a length of 3, but the pattern expects 2 elements」。

**需要观察的现象**：解构赋值与 `let` 共享 `destructure_impl`，仅在叶子回调不同；错误信息随 `spread` 有无自动切换措辞。

**预期结果**：见上。若想确认 `Access` 对临时值的拒绝，可心算 `(1) = (2,)`：左侧 `1` 是字面量，`Expr::access` 命中 `_` 分支求值后报 `cannot mutate a temporary value`。

#### 4.4.5 小练习与答案

**练习 1**：`wrong_number_of_elements` 为何要根据 `spread` 切换 `at least N` 与 `N elements` 两种措辞？
**答案**：含 spread 时，模式接受「N 个或更多」元素（spread 可吸收剩余），故期望写成 `at least N`；无 spread 时长度必须精确等于 N，故写成 `N elements`。措辞与实际语义对齐，避免误导用户。

**练习 2**：解构赋值 `(a, b.field) = (1, 2)` 中，`b.field` 这个叶子是如何被写回的？
**答案**：回调调用 `expr.access(vm)`，`Expr::FieldAccess` 的 `Access` 实现（`access.rs`）经 `access_dict` 取到 `b` 内部字典的 `&mut Dict`，再 `.at_mut("field")` 拿到字段的可变引用并写回 `2`。

---

## 5. 综合实践

把本讲全部知识串起来，完成下面这个**源码阅读 + 对比**任务（对应本讲指定的实践任务）：

1. **对比两个回调**。打开 [src/binding.rs:45-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L45-L57)（`destructure`，服务于 `let`）与 [src/binding.rs:30-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L30-L42)（`DestructAssignment`）。写出两者回调 `f` 的差异：
   - `let` 的回调：`match expr { Ident(ident) => vm.define(ident, value), _ => bail!("cannot assign to this expression") }`——**新建**绑定，叶子只能是标识符。
   - 赋值的回调：`let location = expr.access(vm)?; *location = value;`——**改写**已有位置，叶子可以是任何 `Access` 表达式（标识符、字段访问、访问器方法调用）。
   - 用一句话总结：二者共享 `destructure_impl` 的「按模式拆分」骨架，差异被完全封装在叶子回调里——`define` 创造，`access` 修改。

2. **解释字典 spread sink 如何收集未命名键**。打开 [src/binding.rs:129-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L129-L175)。指出：
   - 遍历模式项时，简写 `Pattern(Ident)`（[L144-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L144-L150)）把标识符名、`Named`（[L151-L156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L151-L156)）把 `name()` 插入 `used` 集合。
   - 遇到 `Spread`（[L157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L157)）时只记下 sink 表达式，先不处理。
   - 最后（[L164-L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L164-L172)）新建空字典，遍历原字典，凡键不在 `used` 中就插入，从而把「所有未被显式命名的键」收进 sink。

3. **收尾验证**。用上述结论预测 `(a, ..rest) = (1, 2, 3)` 与 `(a:, ..rest) = (a: 1, b: 2, c: 3)` 的 `rest`：前者 `rest = (2, 3)`（数组按数量截取），后者 `rest = (b: 2, c: 3)`（字典收集未命名键）。

完成后，你应当能向别人讲清：typst-eval 用一个泛型回调把「解构的形状」与「绑定的动作」解耦，从而用同一份代码同时支撑 `let` 与解构赋值。

## 6. 本讲小结

- `LetBinding::eval` 先求右侧（缺失则为 `none`），再用 `vm.flow.is_some()` 闸门挡住控制流泄漏，最后按 `Normal(pattern)` / `Closure(ident)` 两种 kind 分派。
- `destructure_impl` 是按模式递归分派的引擎，把「叶子如何绑定」抽象成泛型回调 `f`，使 `let` 与解构赋值共享同一套拆分逻辑。
- 数组 spread sink「按数量截取」：`sink_size = (1 + len) − items总数`，用 `checked_sub` 与末尾 `i < len` 检查共同守护长度匹配。
- 字典 spread sink「收集未命名键」：用 `used` 集合记录显式取过的键，最后把不在集合中的键值全收进 sink。
- `let` 的回调用 `vm.define` 新建绑定（叶子限标识符）；解构赋值的回调用 `expr.access` 改写已有位置（叶子可为字段访问等），这是二者最本质的差异。
- `wrong_number_of_elements` 是 `#[cold]` 诊断样板：错误信息 + 随 `spread` 切换的定量期望 + 覆盖整个模式的定位，体现 typst-eval 的诊断三要素。

## 7. 下一步学习建议

- **运算符与赋值**：本讲的解构赋值是「批量赋值」，单个赋值 `x = 1`、`x += 1`、`obj.field = 2` 走的是 `ops.rs` 的 `apply_assignment`（见 u3-l4）。建议接着读 u3-l4，对比「字段赋值为何走 `access_dict` 的 `insert` 特殊路径」与本讲解构赋值对 `Access` 的通用使用。
- **闭包参数绑定**：`Closure` 的参数也用模式与 spread sink，但绑定发生在 `call.rs` 的 `eval_closure` 里（见 u4-l3）。读完本讲再看 u4-l3，你会发现函数参数绑定与 `let` 解构是同一套思路的不同入口。
- **可变访问边界**：若想了解 `access` 还支持哪些写回目标、以及为何 symbol/content/module 报 `cannot mutate fields`，阅读 u5-l3（`Access` 与内置方法）。
