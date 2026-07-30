# 各类目标的定义解析

## 1. 本讲目标

[上一篇 u4-l1](u4-l1-definition-flow.md) 把 `definition` 的「骨架」讲清楚了：`leaf_at` 定位 → `deref_target` 归类 → 按 `DerefTarget` 变体分派，并指出四条分支各自的**策略**（变量三级回退、import/include 跳文件、ref 查元素）。但那篇把 `named_items`、`analyze_expr`、`analyze_import`、`globals` 都当成了「现成积木」——只说了它们各自负责什么，没有打开看**里面是怎么算出最终那个 span / file_id / Value 的**。

本篇就来「开箱」。学完后，你应该能够：

1. 说清**跨文件跳转**是怎么发生的：`#import "other.typ": x` 之后点击 `#x`，为什么终点落在 `other.typ` 的 `#let x` 上——这正是 `named_items` 第一级里 `NamedItem::Import` 的 span 来自被导入模块**作用域**的机制。
2. 解释当 `named_items` 找不到时，`analyze_expr` 第二级如何靠 `trace` 与「字段访问回溯」拿到一个**跨文件的运行时值 span**，以及为什么定义路径用的是不带回退的 `analyze_expr` 而非 `analyze_expr_with_fallback`。
3. 讲清第三级 `globals` 如何按语法模式在 `math` 与 `global` 标准库之间选择，以及它兜住 `#table` 这类内置符号的过程。
4. 复述 `ImportPath`/`IncludePath` 跳文件时，`analyze_import` 如何用「路径字符串的 span」解析相对路径、`module.file_id()` 为何对包路径返回 `None`。
5. 复述 `Ref` 分支如何用一个 `Selector::Label` 在 introspector 上做「单点查询」，并理解它与 `analyze_labels` 的「全量遍历」是两种不同风格。

> 本讲依赖 [u4-l1](u4-l1-definition-flow.md)（定义的主流程与 `Definition` 三变体）、[u2-l3 named_items](u2-l3-named-items.md)（作用域收集）、[u2-l4 analyze](u2-l4-analyze-values.md)（值与 import 推断）。本讲**默认你已经读过这三篇**，重点放在「把它们的产物接到 `definition` 上」的那段衔接逻辑。

## 2. 前置知识

先用三句话回顾本讲直接要用到的几样东西（细节见对应讲义）：

- **`NamedItem` 四变体与 `span()`**（[u2-l3](u2-l3-named-items.md)）：`Var` / `Fn` 的 `span()` 是绑定处 `Ident` 的 span；`Module` 的 span 是**本地**别名或路径字符串的 span；`Import`（挑项导入）的 span 是被导入项在**源模块里**的 span。这一条是本讲的「关键钥匙」——它决定了哪些命名项能跳到别的文件。
- **`analyze_expr` 的两条取值路径**（[u2-l4](u2-l4-analyze-values.md)）：字面量直接构造 `Value`；其余表达式回退到 `typst::trace`——标记 span、把整篇文档重跑一遍、捕获命中时的值。它返回 `EcoVec<(Value, Option<Styles>)>`，同一个 span 可能对应多个值。
- **`Definition` 三变体**（[u4-l1](u4-l1-definition-flow.md)）：`Span(Span)`（源码位置）、`File(FileId)`（整个文件）、`Std(Value)`（标准库值）。本讲的五个最小模块就是分别「喂」出这三种返回值。

另外两个小概念：

- **`Module` 的 `file_id()`**：一个模块若是「某个 `.typ` 文件解析来的」，它的 `file_id()` 返回 `Some(FileId)`；若是包（`@preview/...`）或标准库模块，则返回 `None`。这是「能否跳到本地文件」的分水岭。
- **introspector 与 `Selector`**：编译产物里有一个 `introspector`，能在已渲染的文档上「按条件查元素」。`Selector::Label(label)` 就是「挑出带某个标签的元素」的选择器。

## 3. 本讲源码地图

本讲横跨四个文件，但**主角不再是 `definition.rs`**（它在上篇已精读），而是被它调用的几个伙伴函数的内部实现。

| 文件 | 本讲关注什么 |
| --- | --- |
| [`src/definition.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs) | 四条分支的**调用点**（行号见各节），作为衔接入口。 |
| [`src/matchers.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs) | `named_items` 内部如何为挑项/通配导入生成**指向源模块**的 span（第一级的核心）。 |
| [`src/analyze.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs) | `analyze_expr` 的字段访问回溯与 `trace`（第二级）、`analyze_import` 的路径解析（跳文件的核心）。 |
| [`src/utils.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs) | `globals` 如何按 `mode_after` 选 math/global 作用域（第三级）。 |

一句话定位：**u4-l1 讲的是「分派骨架」，u4-l2 讲的是「骨架里每一根管子接到哪、怎么算」**。

## 4. 核心概念与源码讲解

### 4.1 第一级 named_items 回退：跨文件 span 是怎么来的

#### 4.1.1 概念说明

u4-l1 里我们写过这样一句回调：

```rust
named_items(world, node.clone(), |item: NamedItem| {
    (*item.name() == name).then(|| Definition::Span(item.span()))
})
```

当时只说「名字匹配就返回它的 span」。但这里藏着一个对「跳转定义」最关键的细节：**对不同种类的 `NamedItem`，`item.span()` 指向的地方完全不同**。尤其对于 `import "foo": x` 这种**挑项导入**，`NamedItem::Import` 的 span 不是当前文件里写的那个 `x`，而是 `x` 在**源模块 `foo` 内部**的定义处。正因为如此，点击 `#x` 才能直接跳到 `foo` 文件里——这就是跨文件跳转的「魔法」所在。

要理解它，得回到 `named_items` 处理 `ModuleImport` 的那段实现。

#### 4.1.2 核心流程

`named_items` 遇到一个 `ModuleImport` 节点时，先解析出源模块的值（`analyze_import`），再按导入形式分三种情况生成命名项：

```text
对一个 import 节点，已拿到 source_value = Value::Module(module)：
- "import foo as name" / "import foo as name: ..."   -> NamedItem::Module(name, 本地 name 的 span, module)
- "import foo"  (裸导入)                              -> NamedItem::Module(自动名, 路径字符串的 span, module)
- "import foo: *"   (通配)                            -> 对 module 作用域里每个 binding，造
                                                        NamedItem::Import(名, binding.span(), 值)
- "import foo: a, b.c"  (挑项)                        -> 沿 module 作用域逐层 .get() 找到 binding，
                                                        造 NamedItem::Import(本地名, binding.span(), 值)
```

关键在最后两种：`NamedItem::Import` 的 span 取的是 **`binding.span()`**——也就是被导入项在**源模块作用域里**那条绑定自带的 span。这个 span 的文件 id 是源模块文件的 id，字节范围是它在源文件里的位置。于是 `definition` 拿到它，包成 `Definition::Span` 后，客户端就能跳到另一个文件。

对比之下，`NamedItem::Module` 的 span 是**本地的**（别名 `Ident` 的 span，或路径字符串的 span），它不会让你跳进源模块——这正是「点 `o`（模块别名）」和「点 `x`（挑出来的项）」行为不同的原因。

#### 4.1.3 源码精读

挑项导入生成 `Import` 命名项（核心）：[src/matchers.rs:94-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L94-L119)

```rust
// import "foo": items;
Some(ast::Imports::Items(items)) => {
    for item in items.iter() {
        let mut iter = item.path().iter();
        let mut binding = source_value
            .and_then(Value::scope)
            .zip(iter.next())
            .and_then(|(scope, first)| scope.get(&first));

        for ident in iter {
            binding = binding.and_then(|binding| {
                binding.read().scope()?.get(&ident)
            });
        }

        let bound = item.bound_name();
        let (span, value) = match binding {
            Some(binding) => (binding.span(), Some(binding.read())),
            None => (bound.span(), None),
        };

        let item = NamedItem::Import(bound.get(), span, value);
```

中文说明：

1. `item.path().iter()` 取出挑项的路径（`a` 或 `b.c` 这种可能多段）。第一段用 `scope.get(&first)` 在**源模块作用域**里查；后续每段在上一层结果的 `.read().scope()` 里继续查——所以 `import "foo": b.c` 会先找 `b` 再在 `b` 里找 `c`。
2. 找到 `binding` 后，`span = binding.span()`——**这就是源模块里的位置**。找不到（导入了一个不存在的名字）则回退到本地 `bound.span()`，但此时 `value` 为 `None`，是个「名字存在但解析失败」的降级项。
3. `NamedItem::Import(bound.get(), span, value)`：第一个字段是**本地绑定名**（用于和光标处的 `name` 比对），第二个字段才是**跳转用的 span**。这两个字段不同源，是这个设计的精妙之处。

通配导入同理，也是直接用 `binding.span()`：[src/matchers.rs:77-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L77-L90)

```rust
Some(ast::Imports::Wildcard) => {
    if let Some(scope) = source_value.and_then(Value::scope) {
        for (name, binding) in scope.iter() {
            let item = NamedItem::Import(
                name,
                binding.span(),
                Some(binding.read()),
            );
```

中文说明：`import "foo": *` 把源模块作用域里**每一项**都变成 `Import`，span 同样来自源模块的 `binding.span()`。所以通配导入的每个名字都能跨文件跳。

而模块项的 span 是本地的，看 `name_and_span`：[src/matchers.rs:45-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L45-L66)

```rust
let name_and_span = match (imports, v.new_name()) {
    // import "foo" as name
    (_, Some(name)) => Some((name.get().clone(), name.span())),
    // import "foo"
    (None, None) => v.bare_name().ok().map(|name| (name, source.span())),
    // import "foo": ..
    (Some(..), None) => None,
};
// ...
if let Some((name, span)) = name_and_span
    && let Some(res) = recv(NamedItem::Module(&name, span, module))
{ ... }
```

中文说明：有 `as name` 时 span 是**本地 `name`** 的 span；裸导入时 span 是**路径字符串 `source.span()`**。两者都在当前文件里——所以「点模块本身」不会把你送到源模块文件，它只在本地打转。

最后看 `NamedItem::span()` 如何把这些统一出口：[src/matchers.rs:208-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L208-L214)

```rust
pub(crate) fn span(&self) -> Span {
    match *self {
        NamedItem::Var(name) | NamedItem::Fn(name) => name.span(),
        NamedItem::Module(_, span, _) => span,
        NamedItem::Import(_, span, _) => span,
    }
}
```

中文说明：`definition` 里 `Definition::Span(item.span())` 取到的就是这个值——`Var`/`Fn` 给本地绑定处，`Import` 给源模块里的定义处，`Module` 给本地别名/路径。**同一个 `.span()` 调用，语义随变体而变**，这就是第一级回退能「既跳本文件、又跳跨文件」的全部秘密。

#### 4.1.4 代码实践

**实践目标**：验证「挑项导入的 span 来自源模块」这一论断，并体会它和「模块项 span 在本地」的差异。

**操作步骤**：

1. 打开测试 [test_definition_cross_file](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L173-L177)：
   ```rust
   let world = TestWorld::new("#import \"other.typ\": x; #x")
       .with_source("other.typ", "#let x = 1");
   test(&world, -2, Side::After).must_be_at("other.typ", 5..6);
   ```
2. 手算 `other.typ` = `#let x = 1` 中 `x` 的字节偏移：`#`(0)`l`(1)`e`(2)`t`(3)` `(4)`x`(5)——`x` 在 `5..6`。这正是断言期望。
3. （源码阅读型，**待本地验证**）思考：如果把测试改成 `#import "other.typ"; #other`（裸导入 + 点模块名 `other`），`named_items` 会产出哪种 `NamedItem`？它的 `span()` 指向哪里？

**需要观察的现象**：挑项导入时，第一级直接命中并返回指向 `other.typ` 的 `Definition::Span`；`named_items` 没有去重跑 `trace`，非常便宜。

**预期结果**：`must_be_at("other.typ", 5..6)` 通过。第 3 步的答案是：会产出 `NamedItem::Module("other", source.span(), module)`，span 是本地路径字符串 `"other.typ"` 的 span，所以点 `other` 只会跳到本文件里的那个路径字符串，**不会**进入 `other.typ`。

#### 4.1.5 小练习与答案

**练习 1**：`#import "lib.typ": a, b.c` 之后点击 `#b.c` 里的 `c`，第一级能命中吗？span 落在哪？

> **参考答案**：能命中。`named_items` 处理挑项 `b.c` 时会沿 `lib` 模块作用域先 `.get("b")`、再在 `b` 的 `.scope()` 里 `.get("c")`，最终 `binding.span()` 是 `c` 在 `lib.typ`（或 `b` 所属子模块）里的定义处。返回指向那里的 `Definition::Span`。注意本地比对用的是 `item.bound_name()`（即 `c`），而跳转用的是源模块的 span，两者通过 `NamedItem::Import(bound.get(), span, value)` 分别携带。

**练习 2**：为什么 `import "foo": x` 里导入了一个**不存在**的名字 `x`（`foo` 里没有 `x`），点击 `#x` 时第一级不会崩溃、却也可能跳不到正确地方？

> **参考答案**：见 [src/matchers.rs:108-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L108-L112)：`binding` 为 `None` 时，`span` 回退到本地 `bound.span()`、`value` 为 `None`。于是第一级仍会返回一个 `Definition::Span`，但它指向**当前文件里挑项列表中的 `x`**（本地），而不是源模块。这是个降级但不报错的 best-effort 行为——之后会把控制权交给后续级别，不过第一级已经返回了 `Some`，所以实际到此为止。

---

### 4.2 第二级 analyze_expr 的 span 比对：trace 与「不自跳」守卫

#### 4.2.1 概念说明

第一级靠静态遍历语法树，又快又准，但它有覆盖不到的场景——最典型的就是**裸导入后的字段访问**：`#import "other.typ"; #other.foo`。这里光标在 `foo` 上，`named_items` 找不到一个叫 `foo` 的本地绑定（裸导入只产生模块项 `other`），第一级落空。这时就轮到第二级 `analyze_expr`：它去**推断这个表达式运行时是什么值**，再从值身上挖出一个 span。

这里有两个本讲要专门讲清的点：

1. **字段访问是怎么被「当成一个整体」去 trace 的**——靠 `analyze_expr` 内部对 `FieldAccess` 父节点的回溯。
2. **`definition` 用的是不带回退的 `analyze_expr`**，而不是 `analyze_expr_with_fallback`。这个选择不是随意的：定义路径把「标准库兜底」单独做成第三级，而不是揉进值推断里。

#### 4.2.2 核心流程

第二级的流程（对应 [src/definition.rs:48-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L48-L57)）：

```text
取 analyze_expr(world, node) 的第一个候选值 (value, _):
  if value 是 Content  -> span = content.span()
  if value 是 Func     -> span = func.span()
  else                 -> span = Span::detached()   # 其它值没有可用 span
  if span 有效 且 span != node.span()  -> Definition::Span(span)   # 两个守卫
否则继续第三级
```

而 `analyze_expr` 内部，对 `other.foo` 这种字段访问的取值路径（对应 [src/analyze.rs:35-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L35-L45)）：

```text
node = 叶子 foo（FieldAccess 的字段部分，index > 0）
  -> 发现父节点是 FieldAccess 且 node.index() > 0
  -> return analyze_expr(world, parent)   # 回溯到整个 other.foo
整个 other.foo 不是字面量
  -> return typst::trace(world.upcast(), node.span())  # 标记 span、重跑文档、捕获值
```

关键在于「字段部分回溯到整个字段访问」这一步：它保证 trace 标记的是 `other.foo` 这整个表达式的 span，求值器命中时捕获的正是 `foo` 这个函数值，于是 `func.span()` 能给出它在 `other.typ` 里的定义处。

#### 4.2.3 源码精读

`analyze_expr` 里对字段访问的回溯：[src/analyze.rs:35-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L35-L45)

```rust
if let Some(parent) = node.parent()
    && matches!(
        parent.kind(),
        SyntaxKind::FieldAccess | SyntaxKind::MathFieldAccess
    )
    && node.index() > 0
{
    return analyze_expr(world, parent);
}

return typst::trace::<PagedDocument>(world.upcast(), node.span());
```

中文说明：当被分析的节点是字段访问的「后半段」（`index() > 0`，即 `.foo` 那部分），就向上回到整个字段访问再分析。否则直接对当前 span 发起 `trace`。`typst::trace` 的语义见 [u2-l4](u2-l4-analyze-values.md)：它把整篇文档重新求值一遍，命中被标记 span 时把值连同样式收进 `Sink`，返回所有捕获值。

`definition` 第二级如何消费这个值：[src/definition.rs:48-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L48-L57)

```rust
if let Some((value, _)) = analyze_expr(world, &node).first() {
    let span = match value {
        Value::Content(content) => content.span(),
        Value::Func(func) => func.span(),
        _ => Span::detached(),
    };
    if !span.is_detached() && span != node.span() {
        return Some(Definition::Span(span));
    }
}
```

中文说明：

- 只取**第一个**候选值（`.first()`）。`analyze_expr` 可能返回多个值（同一 span 多次求值），但定义跳转只需要一个终点。
- 只有 `Content` 和 `Func` 这两种值「身上带 span」——因为它们在源码里有定义处。整数、字符串等纯量值没有源码定义位置，给 `Span::detached()`（占位），随后被守卫挡掉。
- 两个守卫：`!span.is_detached()`（值确实有定义处）且 `span != node.span()`（定义处不是光标当前位置——避免「跳到自己」）。

值得对比的一点：`definition` 这里用的是 **`analyze_expr`**，而不是 [`analyze_expr_with_fallback`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L56-L77)。后者在 `analyze_expr` 落空时会**自己**回退到标准库（处理死代码里的裸 `Ident` 与单层 `FieldAccess`）。`definition` 故意不用它，而是把「标准库兜底」单独拎出来作为第三级 `globals`——原因是第三级返回的是 `Definition::Std(Value)`，语义和第二级的 `Definition::Span` 不同，不能混在值推断里一起返回。`analyze_expr_with_fallback` 真正的使用者是 [tooltip.rs:225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L225) 与 [complete.rs:502](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L502)、[complete.rs:571](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L571)（它们只需要一个值、不区分 `Definition` 变体）。

#### 4.2.4 代码实践

**实践目标**：用 `test_definition_field_access_function` 这个用例，亲手追一遍「裸导入 + 字段访问」走第二级、并跨文件命中的全过程。

**操作步骤**：

1. 阅读用例 [src/definition.rs:162-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L162-L170)：
   ```rust
   let world = TestWorld::new("#import \"other.typ\"; #other.foo")
       .with_source("other.typ", "#let foo(x) = x + 1");
   test(&world, -2, Side::Before).must_be_at("other.typ", 8..11);
   ```
2. 推理链：
   - 光标在 `foo` 上 → `deref_target` 把它归为 `VarAccess`（`foo` 是 `Ident`）。
   - 第一级 `named_items`：只找到模块项 `other`，没有 `foo` → 落空。
   - 第二级 `analyze_expr(world, &foo_node)`：`foo` 的父节点是 `FieldAccess` 且 `index > 0` → 回溯到整个 `other.foo` → `trace` 捕获到函数值 `foo`。
   - `Value::Func(func)` → `func.span()` 落在 `other.typ` 的 `8..11`（测试注释说这是参数列表处的 span——函数值的 span 不完美但可用）。
   - 守卫通过（非 detached、≠ 当前 span）→ 返回 `Definition::Span(8..11 @ other.typ)`。
3. （**待本地验证**）若把 `other.typ` 改成 `#let foo = 1`（`foo` 是个整数而非函数），重跑该用例会怎样？

**需要观察的现象**：第二级靠 trace 拿到了跨文件的函数值 span。注意它比第一级**贵得多**（要重跑整篇文档），这也是 u4-l1 强调「三级回退从便宜到贵」的原因。

**预期结果**：原用例通过。第 3 步：`foo` 变成整数后，`Value::Int` 不带 span → `Span::detached()` → 守卫 `!span.is_detached()` 失败 → 第二级落空 → 进入第三级 `globals` 找 `foo`（标准库没有）→ 最终 `definition` 返回 `None`，`must_be_at` 因解包 `Definition::Span` 失败而 panic。这说明**第二级只对「带 span 的值」（Content/Func）有效**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `definition` 取 `.first()` 而不是遍历 `analyze_expr` 返回的所有候选值？

> **参考答案**：定义跳转只需要一个终点。`analyze_expr` 返回多值是因为同一个 span 可能在文档中被多次求值（如循环里、多次调用），但对「跳转到定义」而言，这些值的定义处通常相同或无意义，取第一个即可。对比 [u3-l3 expr_tooltip](u3-l3-expr-tooltip.md) 会把多值合并展示成 `(×N)`——那是「展示」需求，和「跳转」需求不同。

**练习 2**：把守卫 `span != node.span()` 删掉，对 `#let x = 1; #x` 点击第二个 `x` 会发生什么？

> **参考答案**：第二级 `analyze_expr` 对 `#x`（裸 `Ident`）会回退到 `trace`，捕获到值 `1`（`Value::Int`）。它是整数 → `Span::detached()` → 第一个守卫 `!span.is_detached()` 已经挡住了，所以删不删第二个守卫都不影响这个例子。第二个守卫真正起作用的场景是：当值的 span 恰好等于光标节点 span 时（典型是某些自引用或就地构造的 Content/Func），避免返回「跳到自己」。两个守卫各管一类无效情况。

---

### 4.3 第三级 globals：标准库兜底与语法模式

#### 4.3.1 概念说明

当名字既不在用户作用域（第一级）、也没法被推断出带 span 的值（第二级）时，还有一类常见的来源：**Typst 标准库**。`#table`、`#rect`、`#emoji` 这些内置符号没有 `.typ` 源码，它们的「定义」就是 Rust 编译期注册的那个原生值。第三级 `globals` 就是来兜住这类名字的，返回 `Definition::Std(Value)`。

它还有一个容易被忽略的细节：**按语法模式选择作用域**。在数学模式里，`#pi`、`#alpha` 这类名字来自 `library.math`，而不是 `library.global`。`globals` 用光标所在的 `mode_after` 来区分这两者。

#### 4.3.2 核心流程

```text
scope = globals(world, &leaf):
  if leaf.mode_after() == Some(Math) -> library.math.scope()
  else                                -> library.global.scope()
binding = scope.get(&name)            # 标准库里有没有这个名字
命中 -> Definition::Std(binding.read().clone())   # 克隆出原生 Value
```

#### 4.3.3 源码精读

`globals` 的实现：[src/utils.rs:174-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L174-L182)

```rust
/// The global definitions at the given node.
pub fn globals<'a>(world: &'a dyn IdeWorld, leaf: &LinkedNode) -> &'a Scope {
    let library = world.library();
    if leaf.mode_after() == Some(SyntaxMode::Math) {
        library.math.scope()
    } else {
        library.global.scope()
    }
}
```

中文说明：`leaf.mode_after()` 返回光标「之后」所处的语法模式（[u2-l1](u2-l1-cursor-to-syntax-node.md) 提到过 `LinkedNode` 的这类上下文方法）。数学模式给 `library.math.scope()`，否则一律 `library.global.scope()`。注意它传的是 `&leaf`（光标叶子），不是 `node`（被分析的表达式）——因为「该用哪个标准库」取决于光标所在的模式，与具体表达式无关。

`definition` 第三级调用：[src/definition.rs:59-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L59-L61)

```rust
if let Some(binding) = globals(world, &leaf).get(&name) {
    return Some(Definition::Std(binding.read().clone()));
}
```

中文说明：`.get(&name)` 在作用域里按名查找，返回一个 `Binding`；`.read()` 读出里面的 `Value` 并 `clone()`，包成 `Definition::Std`。这个 `Value` 通常是原生元素函数（如 `TableElem::ELEM`），客户端拿到后可以展示内置文档或跳到文档站。

#### 4.3.4 代码实践

**实践目标**：验证 `#table` 走第三级、并返回 `Definition::Std(TableElem)`。

**操作步骤**：

1. 阅读用例 [test_definition_std](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L198-L201)：
   ```rust
   test("#table", 1, Side::After).must_be_value(typst::model::TableElem::ELEM);
   ```
2. 推理：`table` 是 `VarAccess`；第一级无 `let`/`import` 绑定它；第二级 `analyze_expr` 对裸 `Ident` 走 `trace`，但 `table` 是标准库函数，求值能拿到 `Value::Func`，`func.span()` 多半是 detached（原生函数没有源码 span）→ 第二级守卫挡掉；第三级 `globals` 命中，返回 `Definition::Std(TableElem::ELEM)`。
3. （源码阅读型）思考：在数学片段 `$ pi $` 里对 `pi` 调 `definition`，第三级会查哪个作用域？

**需要观察的现象**：第三级只在「标准库符号」上命中，返回 `Definition::Std`（不是 `Span`）。

**预期结果**：用例通过。第 3 步：`leaf.mode_after() == Some(Math)` → `globals` 返回 `library.math.scope()` → 在数学作用域里找到 `pi`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `globals` 用 `leaf.mode_after()` 而不是固定用 `library.global`？

> **参考答案**：因为 Typst 的标准库分两个作用域：通用 `global`（`table`、`rect`、`emoji` 等）和数学专用 `math`（`pi`、`alpha`、`frac` 等）。同一个名字可能在两个作用域里都有但含义不同。用光标所在模式选作用域，才能保证在数学公式里补/跳到数学符号、在正文里跳到通用符号。

**练习 2**：用户自己写 `#let table = 1` 把 `table` 重新绑定成整数，点击这个 `table` 会返回 `Definition::Std(TableElem)` 吗？

> **参考答案**：不会。第一级 `named_items` 会先在前置兄弟里找到 `#let table = 1`，返回 `Definition::Span(本地 table 的 span)`，根本走不到第三级。三级回退的顺序保证了「用户定义优先于标准库」——这正是 `globals` 排在最后的理由。

---

### 4.4 ImportPath / IncludePath：analyze_import 解析路径，file_id 决定能否跳文件

#### 4.4.1 概念说明

`deref_target` 把 `#import "x.typ"` / `#include "x.typ"` 里的字符串路径归为 `ImportPath` / `IncludePath`（[u2-l2](u2-l2-deref-target.md) 讲过：同一个 `Str` 因父节点不同而归类不同）。这条分支的终点是**一整个文件**，返回 `Definition::File(FileId)`。

要把「字符串路径」变成「一个文件」，需要两步：① 真正执行这次 import，拿到 `Value::Module`；② 从模块取出它的 `file_id`。第一步由 `analyze_import` 完成，里面有个关键设计——**用路径字符串的 span 来解析相对路径**。第二步则揭示了「本地文件能跳、包路径不能跳」的分界。

#### 4.4.2 核心流程

```text
node = 字符串路径节点（如 "other.typ"）
source_span = node.span()                         # 路径字符串的 span（含文件 id）
value = analyze_expr(world, node) 的第一个值       # 通常是 Value::Str("other.typ")
  若 value 本身已是模块 -> 直接用
  否则 Value::Str(path) -> with_engine 执行 typst_eval::import(engine, &path, source_span)
                         -> Value::Module         # 相对路径按 source_span 所在文件解析
module.file_id()                                  # 本地文件 -> Some(FileId)；包/标准库 -> None
命中 -> Definition::File(id)；否则 None
```

#### 4.4.3 源码精读

`definition` 的调用点：[src/definition.rs:65-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L65-L71)

```rust
DerefTarget::ImportPath(node) | DerefTarget::IncludePath(node) => {
    let Some(Value::Module(module)) = analyze_import(world, &node) else {
        return None;
    };
    let id = module.file_id()?;
    return Some(Definition::File(id));
}
```

中文说明：`analyze_import` 拿不到模块（解析失败）就整体 `None`；拿到模块后再取 `file_id()`，若模块不是文件模块（返回 `None`），整个分支也 `None`。

`analyze_import` 的内部实现：[src/analyze.rs:80-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L80-L93)

```rust
pub fn analyze_import(world: &dyn IdeWorld, source: &LinkedNode) -> Option<Value> {
    // Use span in the node for resolving imports with relative paths.
    let source_span = source.span();
    let (source, _) = analyze_expr(world, source).into_iter().next()?;
    if source.scope().is_some() {
        return Some(source);
    }

    let Value::Str(path) = source else { return None };

    crate::utils::with_engine(world, |engine| {
        typst_eval::import(engine, &path, source_span).ok().map(Value::Module)
    })
}
```

中文说明逐句：

1. `source_span = source.span()`——**路径字符串节点的 span**。它的文件 id 标明「这次 import 是从哪个文件发起的」，这正是相对路径（`"other.typ"`）解析的基准。如果没有这个 span，求值器就不知道相对谁去解析。
2. `analyze_expr(world, source)`——先把路径节点求值，通常得到 `Value::Str("other.typ")`。
3. 若值本身已是模块（比如 import 的是一个已经解析好的模块值，少见），直接返回。
4. 否则要求是 `Value::Str`，然后在 `with_engine` 造的临时引擎上调用 `typst_eval::import(engine, &path, source_span)`——真正执行 import，得到 `Value::Module`。`with_engine` 的细节见 [u2-l5](u2-l5-utils.md)：它复用 world 的库、配空 introspector，是一次性轻量求值。

为什么 `module.file_id()` 对包路径返回 `None`？因为包（`@preview/...`）和标准库模块不是「工作区里的某个文件」，它们没有对应的 `FileId`。所以 `#import "@preview/fletcher:0.5"` 点在路径上，这条分支最终返回 `None`——无法跳到一个外部包的本地源文件。

#### 4.4.4 代码实践

**实践目标**：对比本地文件路径与包路径在跳转上的不同。

**操作步骤**：

1. 阅读两个用例 [test_definition_import](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L180-L184) 与 [test_definition_include](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L187-L191)，两者都断言 `must_be_file("other.typ")`。
2. 追 import 用例：光标在 `"other.typ"` 字符串上 → `ImportPath` → `analyze_import` 用 `source_span`（`main.typ` 里的位置）把相对路径解析成 `other.typ` 文件模块 → `file_id()` 命中 → `Definition::File`。
3. （**待本地验证**）思考：若把用例改成 `#import "@preview/cetz:0.3"` 并把光标放在路径上，结果会是什么？

**需要观察的现象**：本地 `.typ` 文件路径能跳；包路径不行。

**预期结果**：两个本地用例通过。第 3 步：`analyze_import` 仍能解析出包模块，但 `module.file_id()` 对包返回 `None` → 整个分支返回 `None`，`must_be_file` 会 panic。这验证了「`file_id()` 是本地/外部的分水岭」。

#### 4.4.5 小练习与答案

**练习 1**：`analyze_import` 里这一句 `let source_span = source.span();` 能删掉、直接用 `Span::detached()` 传给 `typst_eval::import` 吗？

> **参考答案**：不能。`typst_eval::import` 解析**相对路径**时，要相对于「发起 import 的那个文件」。`source_span` 携带的就是这个文件的 id（以及用于报错定位的字节范围）。换成 detached span，求值器就丢失了「相对谁解析」的基准，相对路径 import 会解析失败或解析错位置——跨文件跳转也就失效了。

**练习 2**：`#include "x.typ"` 与 `#import "x.typ"` 在 `definition` 里走的是同一段代码。它们在语义上对跳转有区别吗？

> **参考答案**：对「跳转到定义」没有区别——两者都返回 `Definition::File("x.typ")`，即打开那个文件。`deref_target` 区分 `ImportPath`/`IncludePath` 只是为了表达「这是 import 还是 include 上下文」，但在 `definition` 里它们共用 `analyze_import` + `file_id` 这条逻辑，终点相同。区别体现在别处（include 会把文件内容拼进当前文档），不在跳转上。

---

### 4.5 Ref：用 Selector::Label 在 introspector 上单点查询

#### 4.5.1 概念说明

`@引用`（如 `@hi`）指向文档里**带 `<标签>` 的某个元素**（figure、heading 等）。它的「定义」就是那个被引用的元素本身，所以终点是 `Definition::Span`（元素在源码里的 span）。

要找到这个元素，必须查**已编译的文档**——元素只有在文档渲染后才会被 introspector 索引、才能按标签查到。这就是 u4-l1 反复强调的「`Ref` 分支唯一依赖 `output`」的根本原因：没有编译产物，introspector 就是空的，查不到任何元素。

本讲再补充一个角度：`definition` 在这里用的是「**构造一个选择器、查第一个**」的单点查询，和 [`analyze_labels`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104-L142) 的「全量遍历所有带标签元素」是两种不同风格——后者服务于「列出全部标签供补全/悬停」，前者服务于「我就要这一个」。

#### 4.5.2 核心流程

```text
node = @hi 节点
target = node.cast::<ast::Ref>()?.target()       # 取 "hi"
label = Label::new(PicoStr::intern(target))       # 驻留成 Label
selector = Selector::Label(label)                 # 「按标签选元素」的选择器
introspector = output?.as_output().introspector() # output 为 None 则整条 None
elem = introspector.query_first(&selector)?       # 第一个带该标签的元素；找不到也 None
-> Definition::Span(elem.span())
```

#### 4.5.3 源码精读

`Ref` 分支：[src/definition.rs:74-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L74-L80)

```rust
DerefTarget::Ref(node) => {
    let label = Label::new(PicoStr::intern(node.cast::<ast::Ref>()?.target()))
        .expect("unexpected empty reference");
    let selector = Selector::Label(label);
    let elem = output?.as_output().introspector().query_first(&selector)?;
    return Some(Definition::Span(elem.span()));
}
```

中文说明：

1. `node.cast::<ast::Ref>()?.target()`：把节点转成 `Ref`，取出引用目标文本（`@hi` → `hi`）。`?` 保证它确实是个引用节点。
2. `PicoStr::intern(...)`：把字符串驻留成内部 id（省内存，typst 里标签比较频繁）。
3. `Label::new(...).expect(...)`：构造标签，断言非空（`@` 后语法上必须有内容，空引用是语法错误）。
4. `Selector::Label(label)`：构造「按标签选元素」的选择器——introspector 支持多种选择器，这是最简单的一种。
5. `output?.as_output().introspector()`：**关键**。`output?` 在 `output` 为 `None` 时让整条表达式直接得 `None`，即「没传编译产物 → 引用跳转直接失败」。`as_output()` 把 `&T: Output` / `&dyn Output` 统一成 `&dyn Output`。
6. `query_first(&selector)?`：查**第一个**匹配元素；文档里没有这个标签也返回 `None`。
7. `elem.span()`：取元素在源码里的 span，包成 `Definition::Span`。

对比 `analyze_labels`：[src/analyze.rs:104-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104-L141) 用 `introspector.query_labelled()` 一次性拿出**所有**带标签的元素（外加参考文献键），并返回一个 `split` 偏移区分「文档标签」与「参考文献」。那是给补全（`@` 之后列出可选引用）和悬停用的全量清单。`definition` 不需要清单，只需要定位**当前这一个**引用的目标，所以用 `Selector::Label` + `query_first` 这种更直接的单点查询。

#### 4.5.4 代码实践

**实践目标**：追 `@hi` 如何跳到 `<hi>` 所在的 `#figure[]`，并验证「缺 output 则失败」。

**操作步骤**：

1. 阅读用例 [test_definition_ref](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L193-L196)：
   ```rust
   test("#figure[] <hi> See @hi", -2, Side::After).must_be_at("main.typ", 1..9);
   ```
   `@hi` 跳到了 `#figure[]`（偏移 `1..9`，即 `figure` 这个元素）。
2. 看测试辅助函数如何生成 `output`：[src/definition.rs:146-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L146-L154)
   ```rust
   let doc = typst::compile::<PagedDocument>(world).output.ok();
   let def = definition(world, doc.as_ref(), &source, cursor, side);
   ```
   即先编译出 `PagedDocument`，再以它为 `output` 传入。
3. （思考实验，**待本地验证**，勿提交改动）把 `definition(world, doc.as_ref(), ...)` 换成 `definition(world, None, ...)`，重跑该用例。

**需要观察的现象**：步骤 3 里，`output?` 因 `None` 直接让 `Ref` 分支返回 `None`，`must_be_at` 解包 `Definition::Span` 时 panic（`expected span definition`）。正反两面证明引用跳转必须有编译产物。

**预期结果**：原用例通过；去掉 `output` 后失败。

#### 4.5.5 小练习与答案

**练习 1**：文档里有两个元素都标了 `<hi>`（重复标签），`@hi` 会跳到哪个？

> **参考答案**：跳到 introspector 索引到的**第一个**带 `<hi>` 的元素（`query_first` 取第一个）。Typst 里标签本应唯一，重复是用户错误；`definition` 用 `query_first` 做了 best-effort 的简单选择，避免返回多个终点。对比 `analyze_labels` 也会用 `seen_labels` 去重、只保留第一个（[src/analyze.rs:108-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L108-L115)），两者态度一致。

**练习 2**：为什么 `Ref` 分支不也能像 `ImportPath` 那样「不依赖 output、自己临时算一下」？

> **参考答案**：因为被引用的元素只有在**整篇文档渲染之后**才会被 introspector 索引——它可能受 `set`/`show` 规则、上下文查询影响，不是单看源码就能确定的。`analyze_import` 只要解析一个文件路径，靠 `world` + 临时引擎即可；而「`<hi>` 标了哪个元素」需要完整的渲染产物。这正是两类终点本质上的差别：一个是「解析一个静态路径」，一个是「查询动态渲染结果」。

## 5. 综合实践

本讲的实践任务（来自讲义规格）：为 `#import "other.typ": x` 之后使用的 `#x`，**逐步追踪 `definition` 的调用链**，定位最终返回的 `Definition::Span` 及其所在文件与偏移。

**初始场景**（对应 [test_definition_cross_file](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L173-L177)）：

- `main.typ` = `#import "other.typ": x; #x`
- `other.typ` = `#let x = 1`
- 光标：`-2, Side::After`，即落在 `#x` 的 `x`（第二个 `x`）上。

**请先自己填空，再核对下面的追踪**：

| 步骤 | 发生了什么 | 关键源码位置 |
| --- | --- | --- |
| ① 定位叶子 | `leaf_at(cursor, After)` 命中 `#x` 里的 `x` 这个 `Ident` 叶子 | [definition.rs:34-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L34-L35) |
| ② 归类 | `x` 是 `Ident` → `deref_target` 归为 `VarAccess(x 节点)` | [matchers.rs:239-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L239-L242) |
| ③ 取名字 | `name = "x"` | [definition.rs:40-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L40-L41) |
| ④ 第一级 named_items | 沿前置兄弟找到 `#import "other.typ": x`；挑项 `x` 在 `other` 模块作用域 `.get("x")` 命中，`binding.span()` = `other.typ` 里 `x` 的定义处 | [matchers.rs:94-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L94-L119) |
| ⑤ 回调命中 | `item.name() == "x"` → 返回 `Definition::Span(binding.span())`，整体短路停止 | [definition.rs:42-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L42-L46) |
| ⑥ 终点 | `Definition::Span` 指向 `other.typ` 的 `5..6`（`#let x = 1` 里的 `x`） | 断言 `must_be_at("other.typ", 5..6)` |

**结论**：这个跨文件跳转**完全由第一级 `named_items` 完成**，没有走到昂贵的 `trace`。关键就在于 `NamedItem::Import` 的 span 取自源模块作用域的 `binding.span()`，它天然带着 `other.typ` 的文件 id。

**延伸练习（举一反三）**：把场景换成下面三种，分别预测命中的是哪一级、返回哪种 `Definition`，并用本讲的断言风格写出期望（**待本地验证**）：

1. `main.typ` = `#import "other.typ"; #other.foo`，`other.typ` = `#let foo(x) = x`，光标在 `foo`。
2. `main.typ` = `#import "other.typ"`，光标在路径字符串 `"other.typ"` 上。
3. `main.typ` = `#table`，光标在 `table`。

> 参考答案：① 第二级 `analyze_expr`（字段访问回溯 + trace），`Definition::Span` 指向 `other.typ` 里 `foo` 的定义；② `ImportPath` 分支，`Definition::File("other.typ")`；③ 第三级 `globals`，`Definition::Std(TableElem::ELEM)`。三者恰好覆盖了本讲的三个回退层级与跳文件分支。

## 6. 本讲小结

- **第一级 `named_items` 的跨文件能力**来自 `NamedItem::Import`：挑项与通配导入的 span 取自**源模块作用域**的 `binding.span()`，所以 `import "foo": x` 后点 `#x` 能直接跳进 `foo`；而 `NamedItem::Module` 的 span 是本地别名/路径，点模块名只在本地打转。
- **第二级 `analyze_expr`** 在 `named_items` 落空时接力：靠 `FieldAccess` 父节点回溯把 `other.foo` 当整体去 `trace`，从 `Content`/`Func` 值上挖出 span；两个守卫（非 detached、≠ 当前 span）挡掉无效终点。`definition` 用的是**不带回退**的 `analyze_expr`，标准库兜底被单独做成第三级。
- **第三级 `globals`** 按光标 `mode_after` 在 `library.math` 与 `library.global` 之间选作用域，命中后返回 `Definition::Std(Value)`，兜住 `#table` 这类内置符号，且因顺序靠后而天然「用户定义优先于标准库」。
- **`ImportPath`/`IncludePath`** 靠 `analyze_import` 真正执行 import：用**路径字符串的 span** 作为相对路径解析基准，再 `module.file_id()` 取文件 id——本地文件返回 `Definition::File`，包路径因 `file_id()` 为 `None` 而无法跳转。
- **`Ref`** 是唯一依赖 `output` 的分支：构造 `Selector::Label` 在 introspector 上 `query_first` 单点查询被引用元素，取其 span；缺 `output` 则 `output?` 直接让整条返回 `None`。
- 五个最小模块——`named_items` 回退、`analyze_expr` span 比对、`globals`、`Selector::Label`、`analyze_import`——分别对应「跨文件变量」「运行时值」「标准库」「引用元素」「跳文件」五类终点的解析机制，是把 u4-l1 的分派骨架「接上电源」的关键。

## 7. 下一步学习建议

- **进入补全引擎**：补全（`autocomplete`）同样大量复用 `named_items`、`globals`、`analyze_expr`、`analyze_import` 这些机制（见本讲各处引用的 `complete.rs` 行号）。建议接着读 [u5 单元 自动补全引擎核心](u5-l1-autocomplete-dispatch.md)，你会发现自己已经掌握了它的大部分「原料」。
- **对比悬停的取值方式**：`tooltip` 用 `analyze_expr_with_fallback`（本讲 4.2 提到），而 `definition` 用不带回退的 `analyze_expr`。对照 [u3-l3 表达式 tooltip](u3-l3-expr-tooltip.md) 的多值合并展示，能更清楚「同一套值推断，不同功能取用方式不同」。
- **动手扩展**：本讲的 `Ref` 分支用 `query_first` 取第一个元素。若想做一个「列出所有同名标签」的诊断功能，可以参考 `analyze_labels` 的 `query_labelled` + `seen_labels` 去重写法（[src/analyze.rs:104-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104-L142)），是一个把本讲内容用起来的二次开发练习。
- **复习路径解析**：若对 `analyze_import` 里 `source_span` 解析相对路径的细节还想更深入，可结合 `typst_eval::import` 的实现（在 `typst-eval` crate）阅读，理解「span 如何决定解析基准」这一贯穿 typst 的设计。
