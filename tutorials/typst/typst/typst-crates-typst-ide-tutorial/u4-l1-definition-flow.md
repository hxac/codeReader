# Definition 类型与 definition 主流程

## 1. 本讲目标

「跳转到定义」（Go to Definition）是 IDE 里被用得最多的功能之一：把光标停在某个名字上，一键跳到它被定义的地方。本讲精读 `src/definition.rs`，讲清楚 typst-ide 的 `definition` 函数是怎么实现这件事的。

学完本讲后，你应该能够：

1. 说出 `Definition` 枚举 `Span` / `File` / `Std` 三种返回值分别对应什么场景，以及为什么需要这三种。
2. 沿着 `definition` 的代码画出从「光标位置」到「定义结果」的主流程：`leaf_at` 定位 → `deref_target` 归类 → 按 `DerefTarget` 变体分派。
3. 解释 `VarAccess` / `Callee` 分支的「三级回退」策略（`named_items` → `analyze_expr` 的 span → `globals` 标准库）。
4. 说清为什么 `Ref`（`@引用`）分支**必须**传入上一次编译产物 `output`，而其他分支不需要。

> 本讲是 u4 单元（跳转定义）的第一篇。它依赖前置讲义 [u2-l2 deref_target](u2-l2-deref-target.md) 与 [u2-l3 named_items](u2-l3-named-items.md)：本讲**把这两个工具当作现成积木来用**，不重复讲它们的内部实现。

## 2. 前置知识

在进入主流程前，先用一句话回顾本讲会用到的三个前置概念（细节见对应讲义）。

- **`leaf_at(cursor, side)`**（来自 [u2-l1](u2-l1-cursor-to-syntax-node.md)）：把光标的字节偏移映射到语法树的一个叶子节点 `LinkedNode`，带上祖先与兄弟的拓扑信息。`Side::Before` / `Side::After` 决定光标落在两个 token 交界处时选中哪一个。
- **`deref_target(leaf)`**（来自 [u2-l2](u2-l2-deref-target.md)）：把任意语法节点归类为七种互斥类别之一——`VarAccess`、`Callee`、`ImportPath`、`IncludePath`、`Code`、`Label`、`Ref`，无法归类返回 `None`。它是 `definition` 的**分派依据**。
- **`named_items(world, node, recv)`**（来自 [u2-l3](u2-l3-named-items.md)）：沿祖先与前置兄弟遍历，收集光标处可见的命名项（`let` 绑定、`import` 项、`for` 变量、闭包参数）。它用回调 `recv` 短路：`recv` 返回 `Some` 就整体停止。

此外还要记得两个贯穿全 crate 的概念：

- **`IdeWorld`**：所有公共函数的第一个参数，是 `World` 的子 trait（见 [u1-l2](u1-l2-ideworld-trait.md)）。`definition` 需要它来取标准库、解析 import、重跑求值。
- **`output: Option<impl AsOutput>`**：上一次编译产物的可选引用。`AsOutput` 是个把 `&T`（`T: Output`）或 `&dyn Output` 统一成 `&dyn Output` 的适配 trait，专门方便「文档可选」的场景。本讲会看到它**只被 `Ref` 分支用到**。

## 3. 本讲源码地图

本讲几乎只涉及一个文件，但它会调用到 crate 内的若干伙伴函数。

| 文件 | 角色 |
| --- | --- |
| [`src/definition.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs) | 本讲主角。定义 `Definition` 枚举与 `definition` 函数，并含一组测试。 |
| `src/matchers.rs` | 提供 `deref_target` 与 `named_items`（前置讲义已精读，本讲当作工具用）。 |
| `src/analyze.rs` | 提供 `analyze_expr`、`analyze_import`（值与 import 推断，见 [u2-l4](u2-l4-analyze-values.md)）。 |
| `src/utils.rs` | 提供 `globals`（按语法模式返回标准库作用域，见 [u2-l5](u2-l5-utils.md)）。 |
| `src/lib.rs` | 用 `pub use self::definition::{Definition, definition};` 把二者摆上公共货架。 |

公共导出（[`src/lib.rs:14`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L14)）只暴露 `Definition` 与 `definition` 两个名字，接口非常精简。

## 4. 核心概念与源码讲解

### 4.1 Definition：三种返回类型

#### 4.1.1 概念说明

「跳转到定义」的终点是什么？对一段 Typst 源码来说，被定义的东西可能落在三种截然不同的地方：

1. **源码里的某个位置**：比如 `#let x = 1` 里的 `x`，定义就在当前文件或被 import 的另一个文件里的某段文本上。
2. **一整个文件**：比如 `#import "other.typ"` 里的 `"other.typ"`，它的「定义」就是 `other.typ` 这个文件整体。
3. **标准库**：比如 `#table` 里的 `table`，它是 Typst 内置的元素函数，定义在 Rust 编译期常量里，没有对应的 `.typ` 源码位置。

这三种终点无法用同一种数据表示，所以 typst-ide 用一个枚举 `Definition` 来统一它们。

#### 4.1.2 核心流程

`Definition` 的定义本身很简单，关键在于理解每个变体「携带什么、给谁用」：

- `Span(Span)` —— 携带一个 `Span`（Typst 里标识源码片段的句柄，含文件 id 与字节范围）。LSP 客户端拿到它后，能定位到「某个文件的某个位置」并跳过去。
- `File(FileId)` —— 携带一个 `FileId`（文件唯一标识，含虚拟路径）。客户端据此打开整个文件。
- `Std(Value)` —— 携带一个标准库的 `Value`（通常是原生元素函数，如 `table`）。客户端一般跳到一个内置的文档页，或就地展示文档。

注意：客户端最终展示「跳到哪」是 LSP 的事；`definition` 只负责把这三种终点之一算出来并返回。

#### 4.1.3 源码精读

枚举定义与文档注释：[src/definition.rs:11-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L11-L20)

```rust
/// A definition of some item.
#[derive(Debug, Clone)]
pub enum Definition {
    /// The item is defined at the given span.
    Span(Span),
    /// The item is the entire included/imported file.
    File(FileId),
    /// The item is defined in the standard library.
    Std(Value),
}
```

中文说明：`Span` 是源码位置、`File` 是整个文件、`Std` 是标准库里的值。三者互斥，由下面的 `definition` 函数按情况产出。

#### 4.1.4 代码实践

**实践目标**：通过测试里的断言助手，确认三种 `Definition` 变体各自校验的字段，从而反推它们的用途。

**操作步骤**：

1. 打开 [src/definition.rs:103-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L103-L144)（测试里的 `ResponseExt` trait）。
2. 分别阅读三个方法：
   - `must_be_at(path, range)`：要求 `Definition::Span`，并用 `self.0.range(span)` 把 `Span` 还原成 `(path, 字节范围)`。
   - `must_be_file(path)`：要求 `Definition::File`，并取 `file_id` 的虚拟路径比对。
   - `must_be_value(expected)`：要求 `Definition::Std`，并把内部的 `Value` 与期望值比对。

**需要观察的现象**：三个断言分别「解包」三种不同的变体。如果实际返回类型与期望不符，会 `panic!("expected span definition")` 之类。

**预期结果**：你会清楚地看到——`Span` 后面跟的是「文件路径 + 字节区间」，`File` 后面跟的是「文件路径」，`Std` 后面跟的是「一个值」。这正是三种返回类型在客户端侧被使用的方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把 `File(FileId)` 也用 `Span(Span)` 表达（毕竟文件也可以看成「一段范围」）？

> **参考答案**：因为「整个文件」没有自然的源码字节范围可指——它不是当前 source 里的一段文本，而是一个独立资源。`FileId` 才是 Typst 里定位文件的正式句柄；用 `Span` 强行表示会丢失「这是一个文件、请直接打开」的语义，也会让客户端的逻辑变复杂。

**练习 2**：`Definition::Std(Value)` 为什么存的是 `Value` 而不是标准库符号的名字字符串？

> **参考答案**：因为标准库里同名符号可能解析到不同的原生值，直接持有 `Value`（通常是一个原生 `Func` / `Type`）最精确，客户端还能顺便拿到它的文档、类型等信息，省去二次查找。

---

### 4.2 definition 主流程：定位叶子 + deref_target 分派

#### 4.2.1 概念说明

`definition` 是公共入口函数。它的整体策略可以概括成两步：

1. **定位**：用 `leaf_at` 把光标映射到一个语法树叶子。
2. **归类 + 分派**：用 `deref_target` 把这个叶子归类，再按类别走不同的解析逻辑。

这种「先统一归类、再分派」的设计让 `definition` 的代码很扁平：一个 `match deref_target(...) { ... }` 把所有情况摆在眼前，每条分支只关心自己那种 `DerefTarget`。这也是为什么 [u2-l2](u2-l2-deref-target.md) 要专门把 `deref_target` 抽出来——它是多个 IDE 功能（不只 definition）共享的分类层。

#### 4.2.2 核心流程

伪代码描述主流程：

```text
fn definition(world, output, source, cursor, side) -> Option<Definition>:
    leaf = LinkedNode::new(source.root()).leaf_at(cursor, side)?   # 定位叶子，失败则 None
    match deref_target(leaf)?:                                      # 归类
        VarAccess | Callee   -> 变量/函数三级回退（见 4.3）
        ImportPath | IncludePath -> 跳文件（见 4.4）
        Ref                   -> 用 output 查元素（见 4.4）
        其他（Label/Code/None） -> 直接 None
```

两个要点：

- `leaf_at` 与 `deref_target` 任何一步返回 `None`，整个函数就返回 `None`（用 `?` 传播）。这意味着「光标落在空白、注释，或无法归类的节点上」时，没有定义可跳。
- `output` 这个参数在主流程里**先不碰**，只在 `Ref` 分支内部使用。所以对绝大多数跳转，`output` 传不传都不影响结果。

#### 4.2.3 源码精读

函数签名与文档：[src/definition.rs:22-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L22-L33)

```rust
/// Find the definition of the item under the cursor.
///
/// Passing a `document` (from a previous compilation) is optional, but enhances
/// the definition search. Label definitions, for instance, are only generated
/// when the document is available.
pub fn definition(
    world: &dyn IdeWorld,
    output: Option<impl AsOutput>,
    source: &Source,
    cursor: usize,
    side: Side,
) -> Option<Definition> {
```

中文说明：文档注释明确点出 `output` 是**可选增强项**，并举例「标签定义只有文档可用时才能生成」——这正是 4.4 要讲的 `Ref` 分支。

定位叶子并归类：[src/definition.rs:34-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L34-L37)

```rust
let root = LinkedNode::new(source.root());
let leaf = root.leaf_at(cursor, side)?;

match deref_target(leaf.clone())? {
```

中文说明：标准三连——`LinkedNode::new` 套上拓扑上下文、`leaf_at` 下钻到光标叶子、`deref_target` 归类。`leaf.clone()` 是因为后面多个分支还要继续用这个 `leaf`（`globals` 分支会再用到 `&leaf`）。

`match` 的骨架与兜底：[src/definition.rs:37-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L37-L86)

```rust
    match deref_target(leaf.clone())? {
        DerefTarget::VarAccess(node) | DerefTarget::Callee(node) => { /* 4.3 */ }
        DerefTarget::ImportPath(node) | DerefTarget::IncludePath(node) => { /* 4.4 */ }
        DerefTarget::Ref(node) => { /* 4.4，需 output */ }
        _ => {}
    }

    None
}
```

中文说明：未处理的类别（`Label`、`Code`）落到 `_ => {}`，最终走到函数末尾的 `None`。注意 `Label`（`<标签>` 自身的定义处）并没有专门分支——它被 `deref_target` 归类出来，但 `definition` 目前不对它做跳转，所以直接返回 `None`。

#### 4.2.4 代码实践

**实践目标**：跑通现有测试，直观看到主流程对不同输入产出不同变体。

**操作步骤**：

1. 在仓库根目录运行（这是源码阅读型实践，**待本地验证**具体输出）：
   ```bash
   cargo test -p typst-ide -- definition::
   ```
2. 观察输出里这几个用例的状态：`test_definition_let`、`test_definition_import`、`test_definition_std`、`test_definition_ref`。
3. 任选一个用例，对照源码 [src/definition.rs:156-201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L156-L201) 里的输入串，推断：光标处的 `leaf_at` 命中什么叶子？`deref_target` 归成哪一类？最终走哪条 `match` 分支？

**需要观察的现象**：四类用例分别对应 `Span`（let / cross_file / ref）、`File`（import / include）、`Std`（std）三种断言。

**预期结果**：全部通过。若某用例失败，说明你对那条分支的推断与实际行为有出入，正好用来校准理解。

#### 4.2.5 小练习与答案

**练习 1**：把光标放在 `#let x = 1` 的 `let` 关键字上，`definition` 大概率返回什么？为什么？

> **参考答案**：返回 `None`。`leaf_at` 命中 `let` 关键字叶子，`deref_target` 把它归到无法处理的类别（既不是表达式访问也不是路径/引用），`match` 落到 `_ => {}`，最终返回 `None`。只有把光标放在**被定义或被使用的名字**上才有定义可跳。

**练习 2**：为什么 `definition` 用 `match deref_target(...)?`，而不是先 `if leaf.is_trivia() { return None }`（像 tooltip 那样）？

> **参考答案**：`deref_target` 已经会把无法归类的叶子（含 trivia）返回 `None`，`?` 直接传播即可，不需要在前面再加一道显式的 trivia 判断。tooltip 之所以单独判 `is_trivia`，是因为它的分发逻辑里有些分支不经过 `deref_target`，需要自己先把 trivia 挡掉；definition 全程依赖 `deref_target`，所以省了这一步。

---

### 4.3 VarAccess / Callee 分支：变量与函数的三级回退

#### 4.3.1 概念说明

当 `deref_target` 归出 `VarAccess`（`x`、`a.b` 这类变量访问）或 `Callee`（`foo()` 里被调用的 `foo`）时，`definition` 要回答「这个名字是在哪儿定义的」。难点在于：同一个名字可能来自三种不同的来源，而且越靠后的越贵、越不可靠。于是这里采用**三级回退**（fallback）策略，从最可靠/最便宜的查起，命中即返回。

#### 4.3.2 核心流程

三级回退的顺序与依据：

```text
对名字 name（光标处 Ident）依次尝试：
1) named_items  —— 在作用域里找 let/import 绑定。
   命中 -> Definition::Span(item.span())      # 最可靠：直接拿到定义处 span
2) analyze_expr —— 推断表达式可能的运行时值，取其 span。
   值的 span 有效且 ≠ 当前节点 span -> Definition::Span(值的 span)
3) globals      —— 查标准库作用域。
   命中 -> Definition::Std(标准库值)            # 兜底：内置符号
都没命中 -> 继续往后（最终返回 None）
```

为什么是这个顺序？

- **`named_items` 最先**：它纯靠静态遍历语法树（[u2-l3](u2-l3-named-items.md)），又便宜又精确——能直接给出「这个名字在源码里定义在哪」。
- **`analyze_expr` 第二**：它可能要重跑整篇文档来 trace 值（[u2-l4](u2-l4-analyze-values.md)），较贵，但能处理 `named_items` 搞不定的场景，比如值是从标准库函数返回的、或来自 `set` 规则之后的真实运行时值。它返回的是「值的 span」，而不是源码绑定处的 span。
- **`globals` 最后**：当名字既不在用户作用域、也没法被推断出值时，最后看它是不是标准库符号（`table`、`rect` 等），返回 `Definition::Std`。

#### 4.3.3 源码精读

整条 `VarAccess | Callee` 分支：[src/definition.rs:40-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L40-L62)

```rust
DerefTarget::VarAccess(node) | DerefTarget::Callee(node) => {
    let name = node.cast::<ast::Ident>()?.get().clone();
```

中文说明：先把节点 `cast` 成 `ast::Ident` 拿到名字字符串 `name`。注意 `Callee` 的 `node` 已经在 `deref_target` 里被 `find(callee().span())` 定位**回被调用的函数名子节点**（见 [u2-l2](u2-l2-deref-target.md)），所以这里能直接当 `Ident` 处理。

第一级——`named_items`：[src/definition.rs:42-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L42-L46)

```rust
if let Some(src) = named_items(world, node.clone(), |item: NamedItem| {
    (*item.name() == name).then(|| Definition::Span(item.span()))
}) {
    return Some(src);
}
```

中文说明：把回调写成「名字匹配就返回 `Definition::Span(item.span())`」。这正是 `named_items` 回调短路设计的典型用法——只取第一个匹配（[u2-l3](u2-l3-named-items.md)）。`item.span()` 对 `Var`/`Fn` 是绑定处 `Ident` 的 span，对 `Import` 是被导入项在**源模块里的** span（所以跨文件 import 也能跳过去）。

第二级——`analyze_expr` 的 span 比对：[src/definition.rs:48-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L48-L57)

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

中文说明：取推断出的第一个值，从 `Content` / `Func` 里挖出它的 span。两个守卫条件很关键：`!span.is_detached()`（span 必须真实存在）且 `span != node.span()`（值的定义处不能就是光标当前位置——否则等于「跳到自己」，没意义）。测试 `test_definition_field_access_function` 里的注释就解释了这一点：函数值的 span 有时会落在参数列表上，不完美但可接受。

第三级——`globals`：[src/definition.rs:59-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L59-L61)

```rust
if let Some(binding) = globals(world, &leaf).get(&name) {
    return Some(Definition::Std(binding.read().clone()));
}
```

中文说明：`globals(world, &leaf)` 按光标所在的语法模式返回标准库作用域（math 模式给 `library.math`，否则给 `library.global`，见 [u2-l5](u2-l5-utils.md)）。`.get(&name)` 命中说明这是个内置符号，包成 `Definition::Std` 返回。`#table` 就走这条路径。

#### 4.3.4 代码实践

**实践目标**：用三个现成测试，分别验证三级回退里被命中的是哪一级。

**操作步骤**：

1. `#x`（局部变量）→ 看 [test_definition_let](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L156-L160)：`test("#let x; #x", -2, Side::After).must_be_at("main.typ", 5..6)`。这里 `named_items` 在前置兄弟里找到了 `#let x`，返回它的 span（`x` 在偏移 5..6）。**第一级命中**。
2. 跨文件函数 → 看 [test_definition_field_access_function](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L162-L170)：从 `other.typ` import 的 `foo`，光标在 `#other.foo` 上。先经 `named_items`（import 项）拿到 `foo` 在 `other.typ` 的 span；测试注释说明实际命中的是函数值的 span（落在参数 `x` 处，`8..11`）。**第一/二级交互**。
3. `#table`（标准库）→ 看 [test_definition_std](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L198-L201)：`named_items` 找不到、`analyze_expr` 对裸 `table` 也拿不到值，最后 `globals` 命中，返回 `Definition::Std(TableElem)`。**第三级命中**。

**需要观察的现象**：同一个 `VarAccess` 分支，因名字来源不同，分别走第一级、第二级、第三级返回。

**预期结果**：三个用例都通过，且你能指出各自命中的那一级。**待本地验证**：在 `cargo test` 里逐一确认。

#### 4.3.5 小练习与答案

**练习 1**：对于 `#import "other.typ": x` 之后使用的 `#x`，是哪一级回退把它跳到 `other.typ` 里的 `#let x`？

> **参考答案**：第一级 `named_items`。`import` 的「挑项式」会被解析成 `NamedItem::Import`，其 `span()` 是被导入项在**源模块**（`other.typ`）里的 span（见 [u2-l3](u2-l3-named-items.md) 与 [matchers.rs:108-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L108-L114)），所以第一级就直接命中并返回跨文件的 `Definition::Span`。测试 `test_definition_cross_file` 验证了这一点。

**练习 2**：第二级里的守卫 `span != node.span()` 去掉会怎样？

> **参考答案**：当推断出的值恰好就定义在光标当前节点上时（例如某些自引用场景），会返回一个「跳到自己」的定义，用户体验上是点了跳转却原地不动。这个守卫专门过滤掉这种无意义的自跳。

---

### 4.4 跳转到文件（ImportPath/IncludePath）与跳转到元素（Ref）

#### 4.4.1 概念说明

剩下两类 `DerefTarget` 各对应一种很不一样的「终点」：

- **`ImportPath` / `IncludePath`**：光标在 `#import "x.typ"` 或 `#include "x.typ"` 的字符串路径上。终点是**一整个文件**，返回 `Definition::File`。它需要真正解析这个 import/include，拿到目标模块的 `file_id`。
- **`Ref`**：光标在 `@引用` 上。终点是**文档里被引用的那个元素**（带 `<标签>` 的 figure、heading 等），返回 `Definition::Span`（那个元素的 span）。它需要**查询已编译文档**才能找到元素——这就是 `output` 参数的唯一用武之地。

> 回忆 [u2-l2](u2-l2-deref-target.md)：同一个 `Str` 字面量，父节点是 `ModuleImport` 就归 `ImportPath`、是 `ModuleInclude` 就归 `IncludePath`、否则归 `Code`。所以这里两条路径分支共用一段逻辑。

#### 4.4.2 核心流程

**ImportPath / IncludePath**：

```text
node(字符串路径)
 -> analyze_import(world, node)            # 真正执行 import，得到 Value::Module（见 u2-l4）
 -> 取 module.file_id()                    # 模块所在文件的 id
 -> Definition::File(id)
```

**Ref**：

```text
node(@target)
 -> 取 target 字符串，intern 成 PicoStr，构造 Label
 -> Selector::Label(label)                 # 一个「按标签选元素」的选择器
 -> output?.as_output().introspector()     # 从编译产物拿 introspector（output 为 None 则整体 None）
 -> introspector.query_first(&selector)    # 查第一个带该标签的元素
 -> Definition::Span(元素.span())
```

关键差异：`Ref` 分支里出现了 `output?`——这个 `?` 意味着**如果调用方没传 `output`（是 `None`），整个 `Ref` 分支立刻返回 `None`**，即无法跳转。而 `ImportPath`/`IncludePath` 只依赖 `world`（通过 `analyze_import`），不需要 `output`。

#### 4.4.3 源码精读

ImportPath / IncludePath 分支：[src/definition.rs:65-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L65-L71)

```rust
DerefTarget::ImportPath(node) | DerefTarget::IncludePath(node) => {
    let Some(Value::Module(module)) = analyze_import(world, &node) else {
        return None;
    };
    let id = module.file_id()?;
    return Some(Definition::File(id));
}
```

中文说明：`analyze_import`（[u2-l4](u2-l4-analyze-values.md)）在临时引擎上真正执行 import，返回 `Value::Module`；不是模块（解析失败）就返回 `None`。`module.file_id()` 取模块对应的文件 id（包内文件才有，标准库模块没有），包成 `Definition::File`。`#import "o.typ"` 走这条。

Ref 分支：[src/definition.rs:74-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L74-L80)

```rust
DerefTarget::Ref(node) => {
    let label = Label::new(PicoStr::intern(node.cast::<ast::Ref>()?.target()))
        .expect("unexpected empty reference");
    let selector = Selector::Label(label);
    let elem = output?.as_output().introspector().query_first(&selector)?;
    return Some(Definition::Span(elem.span()));
}
```

中文说明：逐句拆解——

1. `node.cast::<ast::Ref>()?.target()`：把节点 cast 成 `Ref`，取它的 target 文本（`@hi` 的 `hi`，见 [ast.rs:778](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L778)）。
2. `PicoStr::intern(...)`：把字符串驻留成内部字符串 id（省内存）。
3. `Label::new(...)`：构造标签，`.expect(...)` 断言引用 target 非空（语法上 `@` 后必须有内容）。
4. `Selector::Label(label)`：构造「按标签选元素」的选择器。
5. `output?.as_output().introspector()`：**关键**——`output?` 在 `output` 为 `None` 时直接让整条表达式返回 `None`。`as_output()` 把 `&T` 统一成 `&dyn Output`（[target.rs:48-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L48-L51)），`introspector()` 拿到能查询文档的 introspector。
6. `query_first(&selector)?`：查第一个带该标签的元素；找不到也返回 `None`。
7. `elem.span()`：取元素在源码里的 span，包成 `Definition::Span`。

#### 4.4.4 代码实践

**实践目标**：验证「`Ref` 跳转依赖 `output`」这一论断，并理解测试如何为它准备 `output`。

**操作步骤**：

1. 看 `Ref` 测试 [test_definition_ref](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L193-L196)：`test("#figure[] <hi> See @hi", -2, Side::After).must_be_at("main.typ", 1..9)`。`@hi` 跳到了 `#figure[]`（偏移 1..9）——这正是 `<hi>` 标签所在元素。
2. 看测试辅助函数 `test` 是如何生成 `output` 的：[src/definition.rs:146-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L146-L154)
   ```rust
   let doc = typst::compile::<PagedDocument>(world).output.ok();
   let def = definition(world, doc.as_ref(), &source, cursor, side);
   ```
   即：先把 world 编译成 `PagedDocument`，再以 `doc.as_ref()`（`Option<&PagedDocument>`）作为 `output` 传入。
3. **思考实验（不改动源码）**：假如把上面这行改成 `let def = definition(world, None, &source, cursor, side);`（即不传 `output`），`test_definition_ref` 会怎样？

**需要观察的现象**：第 3 步中，`Ref` 分支的 `output?` 会因 `output` 为 `None` 而返回 `None`，于是 `definition` 返回 `None`，断言 `must_be_at` 会在解包 `Definition::Span` 时 panic（提示 `expected span definition`）。

**预期结果**：正常情况下（传了 `output`）用例通过；去掉 `output` 后用例失败。这从正反两面证明了「`Ref` 分支必须传入编译产物」。**待本地验证**：可临时在测试里复制一份用例并改参数观察（记得不要提交改动）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ImportPath` / `IncludePath` 不需要 `output`？

> **参考答案**：因为跳到文件只需要知道「这个路径解析成哪个文件」，而这件事由 `analyze_import` 在临时引擎上执行 import 就能拿到（返回 `Value::Module` 及其 `file_id`），它依赖 `world`（拿源码、解析路径），不需要已经渲染好的文档。`output` 里存的是**渲染后的文档元素与标签映射**，对「跳文件」没用。

**练习 2**：`#import "@preview/package:0.1.0"` 这种包路径，`definition` 会返回什么？

> **参考答案**：仍然走 `ImportPath` 分支，`analyze_import` 解析得到表示该包的 `Value::Module`。能否返回 `Definition::File` 取决于 `module.file_id()`：包模块通常没有包内源文件的 `FileId`（它是外部依赖），所以 `file_id()` 多半返回 `None`，整个分支返回 `None`——即「包路径无法跳到本地文件」。这与本地 `.typ` 文件不同。

**练习 3**：`Ref` 分支用 `query_first`（取第一个）而不是 `query`（取全部）。如果文档里有多个元素共享同一标签，会发生什么？

> **参考答案**：只会跳到第一个带该标签的元素。Typst 里标签本应唯一，重复标签属于用户错误；`query_first` 的「取第一个」是一个 best-effort 的简单选择，避免返回多个定义让客户端无所适从。

## 5. 综合实践

把本讲的三种返回类型与四条分支串起来，做一次完整的「调用链追踪」。

**任务**：构造一个跨文件场景，覆盖 `Span`、`File`、`Std` 三种返回，并解释每一步。

参考骨架（你可以仿照 [src/definition.rs:162-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L162-L170) 与 [src/definition.rs:179-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L179-L191) 写一个新测试，**不要提交到源码**，只在本地跑）：

```rust
// 示例代码：仅供练习，非项目原有代码
let world = TestWorld::new(
    "#import \"other.typ\": greet\n#greet\n#import \"other.typ\"\n#table",
)
.with_source("other.typ", "#let greet = \"hi\"");

// 期望你能预测下面三处光标分别返回哪种 Definition，并说明走了哪条分支：
//  1. 光标在 #greet 的 greet 上   -> ?  分支 ?  返回 ?
//  2. 光标在第二个 #import 的路径上 -> ?  分支 ?  返回 ?
//  3. 光标在 #table 上            -> ?  分支 ?  返回 ?
```

**完成步骤**：

1. 先不运行，填好上面三处的预测（分支 + 返回类型）。
2. 用本讲的断言风格（`must_be_at` / `must_be_file` / `must_be_value`）把预测写成断言。
3. 运行 `cargo test -p typst-ide` 核对。

**参考答案**：

1. `#greet`：`deref_target` → `VarAccess`；第一级 `named_items` 命中（import 项 `greet`），返回 `Definition::Span`，指向 `other.typ` 里 `#let greet` 的 `greet`。
2. `#import "other.typ"`：`deref_target` → `ImportPath`；`analyze_import` → `Value::Module` → `file_id()`，返回 `Definition::File("other.typ")`。
3. `#table`：`deref_target` → `VarAccess`；`named_items` 未命中、`analyze_expr` 无值、第三级 `globals` 命中，返回 `Definition::Std(TableElem)`。

**延伸思考**：如果把场景里再加一句 `#figure[] <t> See @t`，并对 `@t` 调用 `definition`，你必须确保测试里像 `test()` 那样先 `typst::compile::<PagedDocument>` 再把产物当 `output` 传入——否则 `Ref` 分支返回 `None`。这正好呼应本讲的最后一条结论。

## 6. 本讲小结

- `Definition` 有三种返回值：`Span`（源码位置）、`File`（整个文件）、`Std`（标准库值），分别对应「跳到某处」「打开某文件」「内置符号」。
- `definition` 的主流程是「`leaf_at` 定位叶子 → `deref_target` 归类 → 按 `DerefTarget` 变体分派」，扁平、易读，无法归类即返回 `None`。
- `VarAccess` / `Callee` 分支用**三级回退**找定义：`named_items`（作用域绑定，最可靠）→ `analyze_expr` 的 span（运行时值的 span）→ `globals`（标准库兜底）。
- `ImportPath` / `IncludePath` 分支靠 `analyze_import` 得到模块，返回 `Definition::File`，不依赖编译产物。
- `Ref` 分支是**唯一**用到 `output` 的分支：`output?` 在未传编译产物时直接返回 `None`，所以引用跳转必须有上一次编译结果支撑。
- `output: Option<impl AsOutput>` 的设计（可选 + `AsOutput` 适配）体现了 typst-ide 一贯的「可选增强、优雅降级」哲学。

## 7. 下一步学习建议

- **本单元下一篇 [u4-l2 各类目标的定义解析](u4-l2-definition-targets.md)**：会更细致地拆解 `VarAccess`/`Callee` 的三级回退细节、`import`/`include` 的 `file_id` 解析、以及 `Ref` 用 `Selector::Label` 查询 introspector 的完整过程，是本讲的展开。
- **复习值推断**：第二级回退和 import 跳转都依赖 `analyze_expr` / `analyze_import`，建议回头对照 [u2-l4 analyze](u2-l4-analyze-values.md) 把「值是从哪算出来的」彻底弄懂。
- **对比悬停与补全的分派**：`definition` 的 `match deref_target` 与 `tooltip` 的短路分发链（[u3-l2](u3-l2-tooltip-dispatch.md)）是同一套思想的不同表达，对比阅读能加深对 typst-ide「先归类、再分派」架构的理解。
- **动手扩展**：若想让 `definition` 支持跳到 `Label`（`<标签>` 自身）的定义处，可以在 `match` 里为 `DerefTarget::Label` 加一条分支——这会把你引向 `analyze_labels`（[u2-l4](u2-l4-analyze-values.md)），是一个不错的二次开发练习。
