# named_items —— 收集作用域内可见命名项

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `NamedItem` 四种变体（`Var` / `Fn` / `Module` / `Import`）分别对应什么样的源码构造。
- 理解 `named_items` 用来收集作用域的「沿祖先向上 + 沿前置兄弟向左」双层遍历策略。
- 解释 `let` 绑定、`import`、`for` 循环、闭包参数这四类构造分别如何被解析成命名项。
- 能在一段含 import 和闭包的代码里，手动推断光标处 `named_items` 会收集到哪些命名项。
- 读懂 `src/matchers.rs` 里的测试写法，并仿写一个验证用例。

## 2. 前置知识

本讲承接 [u2-l2 deref_target](u2-l2-deref-target.md)。在动手前，先回忆两个概念：

- **作用域（scope）**：一段代码里「哪些名字当前可见」。Typst 的名字来自 `let` 绑定、`import`、`for` 循环变量、闭包参数等。越靠近光标的作用域越「内层」，应当优先于外层同名名字。
- **带类型语法树视图 `ast::*`**：上一讲我们用 `node.cast::<ast::Expr>()` 把无类型的 `SyntaxNode` 强类型转换成表达式视图。本讲会大量用到 `ast::LetBinding`、`ast::ModuleImport`、`ast::ForLoop`、`ast::Closure` 等同类视图——它们都是对同一棵语法树的不同「视角」，`.cast::<>()` 成功说明这个节点确实是该构造。

还需要了解一个关键设计：**回调式收集**。`named_items` 不返回一个 `Vec`，而是接收一个回调 `recv`。对每个发现的命名项调用 `recv(item)`：

- 回调返回 `None` → 继续找下一个；
- 回调返回 `Some(t)` → 立即停止遍历，`named_items` 把这个 `t` 返回给调用方。

这种「短路回调」让调用方自己决定是「只取第一个匹配」（如跳转定义）还是「全部收集」（如补全、本讲测试）。我们在 [u2-l2](u2-l2-deref-target.md) 已熟悉 `Definition` 如何据此短路，本讲聚焦收集逻辑本身。

## 3. 本讲源码地图

本讲几乎全部内容集中在单个文件：

| 文件 | 作用 |
| --- | --- |
| `src/matchers.rs` | 定义 `NamedItem` 枚举与 `named_items` 收集函数，以及它们的单元测试。 |

此外会少量引用 `typst-syntax` 的 AST 视图定义（`ast::LetBinding`、`ast::Imports`、`ast::Pattern` 等）来解释 `bindings()`、`imports()` 等辅助方法的含义。这些不在 typst-ide 目录下，故以文字说明为主，不逐行引用。

## 4. 核心概念与源码讲解

### 4.1 NamedItem —— 作用域命名项的统一表达

#### 4.1.1 概念说明

`named_items` 在语法树里找到的「一个可见名字」形形色色：有的只是一个标识符（`#let x = 1` 里的 `x`），有的是一个函数（`#let f() = ..` 里的 `f`），有的是整个被导入的模块（`import "foo"` 里的 `foo`），有的是从模块里挑出来的某一项（`import "foo": bar` 里的 `bar`）。

为了让下游（跳转定义、补全）能用同一套代码处理它们，typst-ide 用一个枚举 `NamedItem` 把这四类统一表达。下游只关心三件事：名字叫什么（`name()`）、能取到值吗（`value()`）、定义在哪里（`span()`），这三者由枚举上的辅助方法统一提供。

#### 4.1.2 核心流程

四种变体对应四种源码来源：

```
NamedItem::Var(ident)     ← 普通变量、for 循环变量、闭包参数
NamedItem::Fn(ident)      ← #let f(...) = .. 形式的函数绑定
NamedItem::Module(..)     ← import 得到的「整个模块」名字（foo、重命名后的 bar）
NamedItem::Import(..)     ← import 挑出的「模块里的某一项」（bar、* 通配的每一项）
```

每个变体都携带足够信息回答三个问题：

- `name()`：返回名字字符串（`Var`/`Fn` 取标识符文本，`Module`/`Import` 取已存的名字）。
- `value()`：尽量返回这个名字当前绑定的值。`Var`/`Fn` 无法静态拿到值返回 `None`；`Module` 返回 `Value::Module`；`Import` 返回导入项解析出的值。
- `span()`：返回定义处的 `Span`，供跳转定位。

#### 4.1.3 源码精读

枚举定义与文档注释：

[src/matchers.rs:L178-L188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L178-L188) —— 四个变体，注意 `Module` / `Import` 还各带一个 `Option`：模块可能解析失败（`None`），导入项也可能解析失败，此时只剩名字而没有值。

三个辅助方法：

[src/matchers.rs:L190-L215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L190-L215) —— `name()` / `value()` / `span()`。重点看 `value()`：`Var`、`Fn` 一律返回 `None`（它们的值要靠 `analyze_expr` 另行推断），而 `Module` / `Import` 因为 import 解析时已经拿到了实际值，可以直接返回——这就是为什么补全里 import 进来的常量能带上正确的类型/值。

> 小提示：`Var` 与 `Fn` 在 `value()` 上完全一样（都返回 `None`），二者只在「下游展示时是否标记成函数」上有区别（补全里 `Fn` 会得到不同的 `CompletionKind`）。

#### 4.1.4 代码实践

**目标**：确认 `Var` 与 `Fn` 在数据层面「几乎相同」，区别只在于来源。

**步骤**：

1. 阅读 [src/matchers.rs:L200-L206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L200-L206) 的 `value()`，确认 `Var(..) | Fn(..)` 共用一个返回 `None` 的分支。
2. 在 `src/complete.rs` 中搜索 `NamedItem::Fn` 的使用，观察下游如何根据 `Fn` 与 `Var` 给出不同的补全种类。

**预期结果**：`Var` 与 `Fn` 的「值」都拿不到，但下游会依据变体本身（而非值）决定展示形态。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Module` 的 `value()` 返回的是 `Value::Module`，而 `Var` 返回 `None`？

**参考答案**：`Module` 是 import 解析得到的，解析时就已经拿到了真正的 `Module` 值并存在枚举里；而 `Var` 只是源码里的一个标识符，它在运行时绑定的值需要靠求值（`analyze_expr`）才能知道，收集阶段拿不到，故返回 `None`。

---

### 4.2 named_items 的双层遍历骨架

#### 4.2.1 概念说明

`named_items` 要回答的问题是：「站在光标位置，往四周看，能看到哪些名字？」这本质上是模拟词法作用域的查找。Typst 没有把每个节点的可见作用域预先存好，所以 typst-ide 用一个简洁的几何遍历来近似：

- **第一层（沿祖先向上）**：从光标节点出发，不断走到父节点。每升一层就进入一个更外层的作用域（比如从函数体走到外层代码块）。内层先被访问，符合「内层优先」。
- **第二层（沿前置兄弟向左）**：在每一层，从当前节点向左走它的「哥哥节点」（`prev_sibling`）。同一层里，左边（更早写出来）的 `let`/`import` 才是「已经定义」的，右边的还没执行，不应可见。

这两层嵌套，正好覆盖了「向上找作用域 + 同层找已定义名字」的需求。

#### 4.2.2 核心流程

伪代码描述主循环：

```
ancestor = 光标节点
while ancestor 存在:
    sibling = ancestor                      # 第二层从自己开始
    while sibling 存在:
        if sibling 是 LetBinding:   产出 Var/Fn
        if sibling 是 ModuleImport: 产出 Module/Import
        sibling = sibling.prev_sibling()    # 向左
    # 同层处理完，检查 ancestor 的父是否引入了 for/闭包绑定
    if ancestor.parent 是 ForLoop(且不是迭代对象): 产出循环变量
    if ancestor.parent 是 Closure(且 ancestor 在函数体内): 产出参数
    ancestor = ancestor.parent              # 向上
```

两个值得注意的细节：

1. **同层先处理「自己」再向左**：当 `ancestor` 本身恰好就是一个 `LetBinding`/`ModuleImport` 时，第二层循环的第一轮就会把它自己处理掉（比如光标在 `#let f() = 体` 的函数体里时，`f` 会作为 `Fn` 被收集——这正是函数能递归引用自己的原因）。
2. **for/闭包在「上升到父」时处理**：循环变量和函数参数是「父节点绑定、子树里可见」的，所以放在上升到父节点的那一刻统一检查（见 4.5 节）。

#### 4.2.3 源码精读

函数签名与回调短路设计：

[src/matchers.rs:L8-L13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L8-L13) —— `recv: impl FnMut(NamedItem) -> Option<T>`，返回 `Some` 即整体返回、返回 `None` 即继续。

双层循环骨架：

[src/matchers.rs:L14-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L14-L17) —— 外层 `ancestor` 向上，内层 `sibling` 向左（`prev_sibling`）。

内层每轮尝试两种构造、向左推进、并在产出后短路：

[src/matchers.rs:L18-L29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L18-L29) 与 [src/matchers.rs:L31-L121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L31-L121)（`LetBinding` 与 `ModuleImport` 两大分支，详见 4.3、4.4 节），向左推进与循环收尾在 [src/matchers.rs:L123-L124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L123-L124)。

上升到父节点、处理 for/闭包、再继续向上：

[src/matchers.rs:L126-L173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L126-L173) —— 注意 `ancestor = Some(parent.clone()); continue;` 让外层继续往更外层作用域走；到根（无 parent）则 `break`，函数返回 `None`。

#### 4.2.4 代码实践

**目标**：直观感受「内层作用域先被访问」这一顺序。

**步骤**：

1. 想象代码 `#let x = 1; #let f() = { #let x = 2; ⟨光标⟩ }`，光标在最内层。
2. 手动模拟遍历：第一站（内层）会先碰到内层的 `#let x = 2`，再向左；上升到外层后才碰到外层 `#let x = 1`。
3. 思考：若下游用回调「取第一个匹配的名字」，内外两层 `x` 谁会胜出？

**预期结果**：内层 `x` 先被回调收到，因此「内层优先」自然成立——这正是跳转定义 `definition.rs` 里 `named_items` 短路回调能命中「最近的那个定义」的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接返回一个 `Vec<NamedItem>`，而要用回调？

**参考答案**：不同调用方需求不同。跳转定义只想找「第一个名字匹配的定义」，找到就停，回调返回 `Some` 即可短路、避免无谓遍历；补全和测试想收集全部，回调恒返回 `None` 走完全程。回调让两种用法共用同一套遍历代码。

**练习 2**：同层里，光标右边（更晚写）的 `#let y = ..` 会被收集吗？

**参考答案**：不会。第二层只调用 `prev_sibling()` 向左走，右边的兄弟节点访问不到——这正确反映了「还没执行到的绑定不可见」的语义。

---

### 4.3 let 绑定归类：变量（Var）还是函数（Fn）

#### 4.3.1 概念说明

`#let x = 1` 与 `#let f(a) = ..` 在语法上是同一种节点 `ast::LetBinding`，但语义不同：前者绑定一个普通值，后者定义一个函数。typst-ide 用 `LetBindingKind` 区分：

- `LetBindingKind::Normal(pattern)` —— 普通绑定，产出 `NamedItem::Var`；
- `LetBindingKind::Closure(ident)` —— 函数绑定，产出 `NamedItem::Fn`。

判定依据很简单：`let` 后面第一个孩子如果能强转成 `Closure`（即形如 `名字(参数) = 体`），就是闭包绑定；否则就是普通绑定。

此外，普通绑定支持**解构**：`#let (a, b) = pair`、`#for (k, v) in ...`。一个绑定可能引入多个名字，所以用 `bindings()` 返回一个 `Vec<Ident>`，再逐个产出。

#### 4.3.2 核心流程

```
若 sibling 是 LetBinding:
    kind = LetBindingKind
    若 kind 是 Closure:   分类 = Fn
    否则:                 分类 = Var
    for ident in kind.bindings():     # 支持解构，可能多个
        产出 分类(ident)
```

其中 `bindings()` 的来源（定义在 `typst-syntax` 的 `ast.rs`）：

- `Closure(ident)` → `[ident]`（函数名）；
- `Normal(pattern)` → `pattern.bindings()`，而 `Pattern::bindings()` 会递归处理 `Normal(Ident)`、`Parenthesized`、`Destructuring`，从而把 `(a, b, ..rest)` 里的 `a`、`b`、`rest` 全部取出。

#### 4.3.3 源码精读

`let` 分支整体：

[src/matchers.rs:L18-L29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L18-L29) —— 用 `matches!(v.kind(), ast::LetBindingKind::Closure(..))` 判定闭包，再对 `v.kind().bindings()` 里每个标识符产出 `Fn` 或 `Var`。

`bindings()` 与 `LetBindingKind` 的定义位于 `typst-syntax/src/ast.rs`（本讲不逐行引用），其 `bindings()` 对 `Normal` 调用 `pattern.bindings()`、对 `Closure` 返回 `[ident]`，`Pattern::bindings()` 则覆盖解构。

测试佐证（`Var` 与 `Fn` 的区分）：

[src/matchers.rs:L357-L365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L357-L365) —— `#let f(a) = 1` 在函数体光标处会收集到参数 `a`；在函数体之外（第二条 `#let b` 处）则收集到 `f` 与 `b`，却不再有 `a`（参数已离开作用域）。

#### 4.3.4 代码实践

**目标**：验证解构绑定会产出多个 `Var`。

**步骤**：

1. 在 `src/matchers.rs` 的 `#[cfg(test)] mod tests` 中临时新增一个测试（见下方「示例代码」），用 `must_include` 断言解构出的两个名字都被收集到。
2. 运行 `cargo test -p typst-ide test_named_items_destructure`（在本 crate 目录下也可直接 `cargo test test_named_items_destructure`）。
3. 跑完后删除该临时测试，避免改动源码测试集。

示例代码（非项目原有，仅用于本次实践）：

```rust
#[test]
fn test_named_items_destructure() {
    // #let (a, b) = (1, 2); 之后的光标应同时看到 a 与 b
    test("#let (a, b) = (1, 2); #x", -1)
        .must_include(["a", "b"]);
}
```

**需要观察的现象**：两个名字 `a`、`b` 都通过断言，说明一次 `let` 解构产出了多个 `Var`。

**预期结果**：测试通过。（若你的环境无法编译/运行，标注「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：`#let f = (a) => a` 这种「把闭包赋给变量」的写法，会被归类成 `Fn` 还是 `Var`？

**参考答案**：归类成 `Var`。因为这里 `let` 后第一个孩子是 `Pattern::Normal(Expr::Closure(...))`？——要注意：`LetBindingKind::Closure` 只匹配 `let 名字(参数) = ..` 这种「函数定义语法」。`#let f = (a) => a` 的绑定模式是普通标识符 `f`，右侧才是闭包，因此 `kind()` 是 `Normal`，产出 `Var(f)`。这体现了「分类看的是绑定形式，而非绑定值的类型」。

> 说明：上面这条结论涉及 `LetBinding::kind()` 的具体判定，建议结合 `typst-syntax` 的 `LetBinding::kind()` 源码确认；若不确定，标注「待确认」。

---

### 4.4 import 的三种形式解析

#### 4.4.1 概念说明

`import` 在 Typst 里有多种写法，`named_items` 必须逐一处理：

| 写法 | 引入的名字 | 产出 |
| --- | --- | --- |
| `import "foo.typ"` | 模块本身（取文件名 `foo`） | 一个 `Module` |
| `import "foo.typ" as bar` | 模块本身，重命名为 `bar` | 一个 `Module` |
| `import "foo.typ": *` | 模块里的**每一项** | 若干 `Import` |
| `import "foo.typ": a, b` | 模块里**指定的项** | 若干 `Import` |
| `import "foo.typ": a.b as c` | 指定项的子路径，重命名 | 一个 `Import`（名为 `c`） |

注意「模块本身」与「模块里的项」是两回事：前者产出 `NamedItem::Module`，后者产出 `NamedItem::Import`。`import "foo": bar` 这种**只挑项**的写法甚至不会产出模块名 `foo`——`foo` 在当前作用域里根本不可见。

为了能给出 `Import` 项的**值**，`named_items` 会先用 `analyze_import` 真正解析模块，再在模块作用域里逐项取值。

#### 4.4.2 核心流程

```
若 sibling 是 ModuleImport:
    source_value = analyze_import(world, 源)        # 解析模块，可能失败
    module       = source_value 中的 Module

    # 1) 决定「模块本身」的名字
    name_and_span = match (imports, new_name):
        (_, Some(改名))        => (改名, span)        # ... as bar
        (None, None)           => (bare_name, span)    # import "foo"
        (Some(..), None)       => None                 # import "foo": .. 无模块名

    若有模块名: 产出 Module(名字, span, module)

    # 2) 决定「挑出的项」
    match imports:
        None                     => 无                  # import "foo"
        Wildcard                 => 遍历模块作用域，逐项产出 Import
        Items([a, b, a.b as c])  => 逐项解析子路径，产出 Import
```

「挑项」的子路径解析（如 `a.b`）会沿着模块作用域一层层下钻：先取 `a`，再在 `a` 的子作用域里取 `b`，最后用 `bound_name()`（可能被 `as` 改名）作为当前作用域里的名字。

#### 4.4.3 源码精读

解析模块值：

[src/matchers.rs:L32-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L32-L43) —— 用 `node.find(source.span())` 定位到源表达式节点，再 `analyze_import`。失败时 `source_value` 为 `None`，后续仍能产出名字但拿不到值。

决定模块名的三种情况（带注释对照）：

[src/matchers.rs:L45-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L45-L66) —— `name_and_span` 的三个分支分别对应 `as 重命名` / 裸导入 / `: 项`。

挑项的三种情况：

[src/matchers.rs:L69-L120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L69-L120) —— `None`（无项）、`Wildcard`（遍历 `scope.iter()`）、`Items`（逐项下钻 `item.path()` 并用 `bound_name()` 取绑定名）。

测试佐证（裸导入取文件名、`as` 重命名、跨文件项的值）：

[src/matchers.rs:L367-L385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L367-L385) —— `#import "foo.typ"` 产出 `foo`；`as bar` 后 `foo` 消失只剩 `bar`；`#import "foo.typ": a.b` 跨两个文件最终拿到 `b` 的值 `1`。

#### 4.4.4 代码实践

**目标**：亲手跑一个跨文件 import，确认 `Import` 项携带了真实值。

**步骤**：

1. 在 `src/matchers.rs` 测试模块中临时新增如下测试（示例代码）：

```rust
#[test]
fn test_named_items_import_value_practice() {
    let world = TestWorld::new("#import \"foo.typ\": bar; #x")
        .with_source("foo.typ", "#let bar = 2;");
    // bar 是 Import 项，且应带上值 2
    test(&world, -1).must_include_value(("bar", Some(&Value::Int(2))));
}
```

2. 运行 `cargo test -p typst-ide test_named_items_import_value_practice`。
3. 观察断言：`bar` 不仅被收集到，还带着从 `foo.typ` 解析出的值 `2`。
4. 跑完删除该临时测试。

**预期结果**：测试通过，说明 `Import` 项的值来自 `analyze_import` 解析模块后在作用域里取到的绑定。（若无法运行，标注「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：`#import "foo.typ": bar` 之后，当前作用域里能看到 `foo` 这个名字吗？为什么？

**参考答案**：看不到。因为 `imports` 是 `Some(Items(..))` 且没有 `as` 改名，`name_and_span` 命中 `(Some(..), None) => None`，不产出 `Module`；只产出 `bar` 这一个 `Import`。模块名 `foo` 没有被绑定。

**练习 2**：`import "foo.typ": *` 通配导入会产出多少个命名项？

**参考答案**：等于模块作用域里导出的项数——对 `source_value` 的作用域 `scope.iter()` 逐项产出 `Import`，每一项都带上自己的值。如果模块解析失败（`source_value` 为 `None`），则一项都不产出。

---

### 4.5 for 循环绑定与闭包参数

#### 4.5.1 概念说明

剩下两类绑定不是「兄弟语句」，而是「父节点绑定、子树可见」：

- **for 循环变量**：`#for x in iter { 体 }` 里，`x` 只在循环体里可见，**不在迭代对象 `iter` 里可见**（`iter` 是在外层作用域求值的）。
- **闭包参数**：`#let f(a, b: 1, ..rest) = 体` 里，参数 `a`、`b`、`rest` 只在函数体里可见，**不在参数列表本身可见**（那里它们正在被声明）。

因此这两类不能放进「向左找兄弟」的第二层循环，而是在「上升到父节点」时专门检查，并各加一个判定，确保只在「真正可见的子树」里产出。

闭包参数有三种形态：位置参数 `a`、带默认值的具名参数 `b: 1`、剩余参数 `..rest`。

#### 4.5.2 核心流程

```
上升到 parent 后:
    # for 循环
    if parent 是 ForLoop 且 ancestor 的前一个兄弟不是 in 关键字:
        for ident in 循环模式.bindings():   # #for (k,v) in .. 会产出 k、v
            产出 Var(ident)

    # 闭包
    if parent 是 Closure 且 ancestor 落在函数体内:
        for param in 参数列表:
            Pos(模式)      => 模式.bindings() 各产出 Var
            Named(n)       => 产出 Var(n.name())
            Spread(s)      => 若有 sink_ident(..rest 的 rest): 产出 Var(rest)
```

两个关键判定：

- **`ancestor.prev_sibling_kind() != Some(SyntaxKind::In)`**：`ancestor` 是 ForLoop 的直接孩子。如果它正是迭代对象 `iter`，那它的前一个兄弟就是 `in` 关键字 → 跳过，不产出循环变量（因为 `iter` 求值时 `x` 还不存在）。其余情况（如光标在循环体里）则正常产出。
- **「ancestor 落在函数体内」**：用 `parent.find(体.span())` 找到体节点，再检查 `体.find(ancestor.span())` 是否成立。只有 ancestor 在体内才产出参数，从而排除「光标在参数列表/签名里」的情况。

#### 4.5.3 源码精读

for 循环分支（含 `in` 排除）：

[src/matchers.rs:L127-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L127-L136) —— 用 `pattern.bindings()` 兼容 `#for x in` 与 `#for (k, v) in` 两种形式。

闭包分支（含「在体内」过滤与三种参数）：

[src/matchers.rs:L138-L166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L138-L166) —— `Pos`/`Named`/`Spread` 三种 `Param` 分别处理；`Spread` 仅在存在 `sink_ident`（即写了 `..rest` 而非裸 `..`）时产出。

测试佐证（参数在体内可见、离开函数后不可见）：

[src/matchers.rs:L357-L365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L357-L365) —— `#let f(a) = 1` 在体内（光标 12）收集到 `a`；在体外（光标 19，已到第二条 `#let b`）只收集 `b`、`f`，不再有 `a`。

#### 4.5.4 代码实践

**目标**：验证「迭代对象处看不到循环变量」这一边界。

**步骤**：

1. 在测试模块中临时新增（示例代码）：

```rust
#[test]
fn test_named_items_for_iter_excluded() {
    // 光标落在迭代对象 #x 处：循环变量 i 不应可见
    test("#for i in #x { #i }", -7).must_exclude(["i"]);
    // 光标落在循环体 #i 处：循环变量 i 应可见
    test("#for i in items { #i }", -1).must_include(["i"]);
}
```

2. 运行 `cargo test -p typst-ide test_named_items_for_iter_excluded`。
3. 对照源码 [src/matchers.rs:L127-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L127-L136) 解释 `-7`（落在迭代对象）时为何 `i` 被排除。
4. 跑完删除该临时测试。

> 说明：上面的负数光标 `-7` / `-1` 利用 [u1-l3](u1-l3-build-and-testworld.md) 介绍过的「负数从字符串末尾索引」规则。具体偏移若与本地实际不符，请用 `Side::After` 下的字节位置微调，或标注「待本地验证」。

**预期结果**：迭代对象处 `i` 被排除、循环体处 `i` 被包含，二者差异正是 `In` 关键字判定造成的。

#### 4.5.5 小练习与答案

**练习 1**：`#let f(..args) = 体` 与 `#let f(..) = 体`，闭包分支分别会产出什么？

**参考答案**：前者 `Spread` 有 `sink_ident`（`args`），产出 `Var(args)`；后者是裸 `..`，`sink_ident()` 返回 `None`，不产出任何名字。

**练习 2**：为什么闭包分支要用 `体.find(ancestor.span())` 判断「在体内」，而不能简单地把闭包参数无条件产出？

**参考答案**：因为光标可能在参数列表或函数签名里（那里参数正在被声明，尚未进入作用域）。只有当 ancestor 真正落在函数体子树内时参数才可见，所以必须用「体节点是否包含 ancestor」来过滤，避免在签名处错误地建议参数本身。

---

## 5. 综合实践

把本讲四类构造串起来。给定下面这段含 import 与闭包的代码（`foo.typ` 内容为 `#let bar = 2;`）：

```typst
#import "foo.typ": bar
#let f(a, b: 1) = a
```

假设光标落在函数体 `a` 处（可用负数光标近似，例如在测试里用 `test(&world, -1)`）。

**任务**：

1. 手动列出 `named_items` 此时会收集到的**全部**命名项，并写出每项的**类别**（`Var`/`Fn`/`Module`/`Import`）。
2. 说明每一项分别由 4.3 / 4.4 / 4.5 中的哪一节产出，以及遍历的先后顺序（内层先、外层后；同层先自己后向左）。
3. 用一个临时测试验证你的清单。参考写法（示例代码）：

```rust
#[test]
fn test_named_items_comprehensive_practice() {
    let world = TestWorld::new("#import \"foo.typ\": bar\n#let f(a, b: 1) = a")
        .with_source("foo.typ", "#let bar = 2;");
    test(&world, -1)
        .must_include(["bar", "a", "b", "f"]);   // 四个名字都应可见
}
```

**参考答案**：

| 名字 | 类别 | 产出小节 | 说明 |
| --- | --- | --- | --- |
| `a` | `Var` | 4.5 | 闭包位置参数，光标在体内故可见 |
| `b` | `Var` | 4.5 | 闭包具名参数 `b: 1` 的名字 |
| `f` | `Fn` | 4.3 | `#let f(..) = ..` 是闭包绑定，上升到该 `LetBinding` 时产出 |
| `bar` | `Import` | 4.4 | `import "foo.typ": bar` 挑出的项，带值 `2` |

先后顺序大致为：先 `a`、`b`（闭包参数，最近的内层作用域），上升到 `LetBinding` 时产出 `f`，再向左遇到 `import` 产出 `bar`。注意 `foo` 不会出现——因为这是「挑项」式 import，不绑定模块名。

> 运行结果请以本地 `cargo test` 为准；若环境不可用，标注「待本地验证」。

## 6. 本讲小结

- `NamedItem` 有四种变体：`Var`（普通变量/循环变量/参数）、`Fn`（函数绑定）、`Module`（整个导入模块）、`Import`（从模块挑出的项），统一提供 `name()`/`value()`/`span()`。
- `named_items` 用「沿祖先向上 + 沿前置兄弟向左」的双层遍历近似词法作用域，内层先访问、同层只看左边已定义的名字。
- 收集采用**回调短路**设计：回调返回 `Some` 即停、返回 `None` 继续，兼顾「取第一个」与「全收集」两种用法。
- `let` 绑定按 `LetBindingKind` 区分 `Fn`/`Var`，并通过 `bindings()` 支持解构产出多个名字。
- `import` 三种形式（裸导入、`as` 重命名、`: *` / `: 项`）分别产出 `Module` 或若干 `Import`，挑项式 import 不绑定模块名。
- `for` 循环变量和闭包参数是「父绑定、子树可见」，在上升到父节点时检查；前者用 `in` 关键字排除迭代对象，后者用「在体内」过滤排除签名。

## 7. 下一步学习建议

- 本讲的 `named_items` 是 [u2-l4 analyze](u2-l4-analyze-values.md) 中值推断与 [u5/u6 补全](u5-l1-autocomplete-dispatch.md) 的数据来源，建议接着读 `analyze.rs`，看 `analyze_expr` 如何为 `Var`/`Fn`（`value()` 为 `None` 的项）补上运行时值。
- 跳转定义 `definition.rs` 是 `named_items` 最典型的「短路取第一个」消费者，可在 [u4-l1](u4-l1-definition-flow.md) 中看到它如何把 `NamedItem::span()` 转成 `Definition::Span`。
- 补全 `complete.rs` 的 `scope_completions` 是「全收集」消费者，可在 [u6-l4](u6-l4-scope-and-cast.md) 中看到它如何把 `named_items` 与全局标准库合并、按类型过滤。
