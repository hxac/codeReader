# 语言工具：typst-ide 的补全、定义与提示

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `typst-ide` 全部公开 API（`autocomplete`、`definition`、`tooltip`、`jump_*`、`analyze_*`、`named_items`、`deref_target`）的输入输出契约，以及 `IdeWorld` 这个扩展 trait 对 `World` 追加了什么。
2. 理解一条补全候选是如何被推导出来的：语法层走 `named_items`（AST 遍历，不求值），语义层走 `analyze_expr` / `analyze_import`（必要时重新编译取值），再叠加 `Library` 的 global / math 作用域。
3. 把 IDE 查询映射回 u2-l4 讲过的 Span / LinkedNode 定位机制：光标字节偏移 → `leaf_at` → 叶子节点 → Span 反查 `Source::range`，并理解 `Definition::Span / File / Std` 三种跳转结果的来历。

本讲的反复出现的核心思想只有一句话：**typst-ide 不拥有任何语法树或作用域，它只是 typst-syntax 与 typst-library 之上的一层纯查询函数**——输入「World + 光标位置」，输出「建议列表 / 跳转目标 / 提示文本」，全程只读、尽力而为（best-effort）、绝不 panic。

## 2. 前置知识

本讲默认你已读过以下前置讲义，相关的旧结论只做一句话回顾：

- **u2-l4（AST 视图与 Span）**：`SyntaxNode` 是无损 CST，`ast.rs` 用 `repr(transparent)` 包装 `&SyntaxNode` 提供类型化视图，`cast` 按 `SyntaxKind` 验身；`Span` 是 8 字节编号（高 16 位 FileId、低 48 位编号），且满足「父编号小于所有子编号、兄弟编号从左到右递增」的有序性。
- **u3-l2（Library 与作用域）**：标准库被装配进 `Library`，其中 `global` 与 `math` 两个模块各自挂一个 `Scope`，`Scope` 里是「名字 → 绑定（Binding）」的有序映射。
- **u4-l7（内省机制）**：标签（label）、引用、计数器等信息只有在**排版完成后的文档**（Introspector）里才存在——这解释了为什么 `autocomplete` / `definition` / `tooltip` 都接受一个可选的 `output` 参数。
- **u5-l1/u5-l2 顺带提到的一点**：导出器只消费 `PagedDocument`。typst-ide 与它们同构——也是 `PagedDocument` / `HtmlDocument` 的纯消费者。

两个本讲新引入的术语先解释清楚：

- **IDE 查询（IDE query）**：编辑器在用户敲键、悬停、Ctrl+点击时发起的即时问题，例如「光标处能补全什么？」「这个标识符定义在哪？」「悬停显示什么？」。它和编译不同：编译产出文档，查询产出建议；查询必须毫秒级响应，且面对的常常是**语法上不完整的半编辑状态**（比如只有一个 `#` 或半个括号）。
- **尽力而为（best-effort）**：查询允许失败（返回 `None` / 空列表），但绝不能 panic、不能陷入死循环、不能因为文档有错就罢工。`ast.rs` 文件头专门用 lint 禁止了 `unwrap/expect/panic`（[crates/typst-syntax/src/ast.rs:80-85](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L80-L85)），注释里明说动机之一就是「来自 typst-ide 的不正确编辑」——typst-ide 是这份「永不 panic 契约」的一等消费者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-ide/src/lib.rs` | crate 门面：模块声明、公开 API 的 `pub use` 再导出、`IdeWorld` trait 定义 |
| `crates/typst-ide/src/complete.rs` | 自动补全：`autocomplete` 主入口、按语法上下文分发的十余个 `complete_*` 函数、`CompletionContext` 与 `Completion` 数据结构 |
| `crates/typst-ide/src/matchers.rs` | 语法层匹配器：`named_items`（光标处可见哪些命名绑定）与 `deref_target`（光标处是什么「可操作对象」） |
| `crates/typst-ide/src/analyze.rs` | 语义层分析：`analyze_expr`（表达式可能的值）、`analyze_import`（import 路径解析成模块）、`analyze_labels`（文档中的标签清单） |
| `crates/typst-ide/src/definition.rs` | 定义跳转：`Definition` 枚举与 `definition` 主入口 |
| `crates/typst-ide/src/tooltip.rs` | 悬停提示：`tooltip` 主入口与六种 tooltip 的尝试链 |
| `crates/typst-ide/src/docs.rs` | 文档来源：为任意 `Value` / `ParamInfo` 找到文档（原生文档或源码里的文档注释） |
| `crates/typst-ide/src/jump.rs` | 预览区点击跳转（source ↔ 预览双向） |
| `crates/typst-ide/src/utils.rs` | 工具：临时 `Engine`、按模式选 global/math 作用域的 `globals`、递归值过滤 |
| `crates/typst-ide/src/tests.rs` | 测试基建：`TestWorld`（同时实现 `World` 与 `IdeWorld`）、`FilePos`（负数从末尾数光标） |
| `crates/typst-syntax/src/node.rs` | `LinkedNode`（带父指针与字节偏移的节点游标）、`Side`、`leaf_at`、`find` |
| `crates/typst-syntax/src/span.rs` | `Span` / `SpanNumber` / `SpanKind` 的位布局 |
| `crates/typst-syntax/src/source.rs` | `Source::find` / `Source::range`：Span → 字节区间 的反查 |
| `crates/typst-syntax/src/ast.rs` | `AstNode` trait 与 `SyntaxNode::cast`，类型化视图的入口 |

一个与大纲不同的勘误：规划时把 `LinkedNode` 归入了 `span.rs`，实际它住在 `node.rs`（[crates/typst-syntax/src/node.rs:968-977](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L968-L977)）；`span.rs` 里只有 `Span` 本身。本讲按真实位置讲解。

## 4. 核心概念与源码讲解

### 4.1 IDE API 面：lib.rs 的再导出与 IdeWorld 契约

#### 4.1.1 概念说明

typst-ide 是给「宿主编辑器集成」（官方 VS Code / Neovim 插件、tinymist 等）用的库。它的 API 面非常克制：`lib.rs` 只再导出 8 组名字，没有 struct 供你注册回调、没有事件循环——每个能力就是一个自由函数。宿主的工作方式是：自己实现 `World`（u3-l1 讲过的编译器与环境契约）并**额外**实现 `IdeWorld`，然后在恰当的编辑器事件里调用这些函数。

#### 4.1.2 核心流程

宿主集成 typst-ide 的完整闭环：

```text
编辑器事件（敲键 / 悬停 / Ctrl+点击）
   │
   ▼
宿主实现 IdeWorld（World + 可选的 packages()/files()）
   │
   ▼
调用 typst-ide 查询函数（autocomplete / definition / tooltip / jump_*）
   │      输入：&dyn IdeWorld、可选的编译产物 output、&Source、光标字节偏移
   ▼
拿到纯数据结果（Vec<Completion> / Option<Definition> / Option<Tooltip> / Jump）
   │
   ▼
宿主自己负责前缀过滤、渲染、跳转动画
```

注意一个分工细节：**typst-ide 返回的是「全量候选 + 替换起点 `from`」**，并不按用户已输入的前缀做过滤——那是编辑器侧的职责（后面 4.2.4 的实践会直接观察到这一点）。

#### 4.1.3 源码精读

crate 门面与全部公开 API：[crates/typst-ide/src/lib.rs:3-17](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/lib.rs#L3-L17) —— 8 个内部模块，再导出为 8 组能力：

| 再导出 | 用途 |
| --- | --- |
| `analyze_expr` / `analyze_import` / `analyze_labels` | 语义分析（表达式取值、import 解析、标签收集），既服务外部也供内部复用 |
| `Completion` / `CompletionKind` / `autocomplete` | 自动补全 |
| `Definition` / `definition` | 定义跳转 |
| `Jump` / `jump_from_click` / `jump_from_click_in_frame` / `jump_from_cursor` | 预览区 ↔ 源码双向跳转 |
| `DerefTarget` / `NamedItem` / `deref_target` / `named_items` | 光标处「是什么 / 能看到什么」的语法层匹配器 |
| `Tooltip` / `tooltip` | 悬停提示 |

`IdeWorld` trait：[crates/typst-ide/src/lib.rs:25-58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/lib.rs#L25-L58) 对 `World` 追加了三个方法：

- `upcast(&self) -> &dyn World`：注释写明是因为 trait 向上转型（trait upcasting）曾长期是实验特性而做的手动桥；实现者直接返回 `self`。
- `packages()`：可选。返回可用的包清单，用于 `#import "@|"` 的包名补全。
- `files(base, prefix)`：可选。返回路径补全候选（相对 `base` 文件所在目录、匹配已输入的 `prefix`）。

这两个可选方法体现了 typst-ide 的「增强而非依赖」哲学：不实现也能补全代码，只是少了包名和文件路径两类候选。

依赖关系（[crates/typst-ide/Cargo.toml:15-26](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/Cargo.toml#L15-L26)）也值得一看：正式依赖只有 `typst`、`typst-eval`、`typst-html`、`typst-layout` 等，`typst-pdf` / `typst-render` / `typst-svg` / `typst-bundle` 全部是 **dev-dependencies**——只为测试里 `typst::compile::<PagedDocument>` 与测试库 `Library::builder([...])` 服务（见 4.2.3 里 tests.rs 的 `library()`）。这与 u1-l3 讲过的「导出器只是总装 crate 的消费者」的架构决策一脉相承。

#### 4.1.4 代码实践

1. **实践目标**：亲手生成 typst-ide 的 API 文档，把「编辑器功能 → API」的映射写下来。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo doc -p typst-ide --no-deps`；
   - 打开 `target/doc/typst_ide/index.html`（或直接对照 [crates/typst-ide/src/lib.rs:12-17](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/lib.rs#L12-L17) 的再导出清单）；
   - 为下表左侧的编辑器功能填右侧的 API 与关键输入：

     | 编辑器功能 | API | 关键输入 |
     | --- | --- | --- |
     | 输入时弹候选 | `autocomplete` | source + cursor + `explicit` |
     | Ctrl+点击跳定义 | `definition` | source + cursor + `Side` |
     | 鼠标悬停显示说明 | `tooltip` | source + cursor + `Side` |
     | 点击 PDF 预览跳回源码 | `jump_from_click` | 文档 + 页面坐标 |
     | 从源码定位预览位置 | `jump_from_cursor` | 文档 + source + cursor |
3. **需要观察的现象**：文档里每个函数的签名都只吃「world + 位置」，没有注册、回调或状态。
4. **预期结果**：得到一张 5 行映射表；确认 typst-ide 没有任何需要宿主长期持有的句柄。
5. 构建耗时取决于机器，首次需联网拉取 git 依赖（u1-l2 已说明），属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IdeWorld` 要单独提供 `upcast()`，而不是让调用方直接把 `&dyn IdeWorld` 当 `&dyn World` 用？

**答案**：见 [crates/typst-ide/src/lib.rs:26-32](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/lib.rs#L26-L32) 的文档注释——Rust 的 trait 向上转型（`dyn IdeWorld → dyn World`）在编写此代码时还是实验特性（rust-lang/rust#65991），所以用手动方法桥接，实现者只需返回 `self`。typst-ide 内部大量函数接收 `&dyn IdeWorld`，却要把它交给只认 `&dyn World` 的编译器入口（如 `typst::trace`），`upcast()` 就是那座桥。

**练习 2**：包名补全的数据从哪来？typst-ide 自己会下载包索引吗？

**答案**：不会。数据来自宿主实现的 [`IdeWorld::packages`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/lib.rs#L34-L42)，默认返回空切片；lib.rs 注释还贴心地给出 `@preview` 命名空间索引的下载地址 `https://packages.typst.org/preview/index.json`。typst-ide 只负责把 `(PackageSpec, 描述)` 列表排序去重成候选（见 4.2.3 的 `package_completions`）。

---

### 4.2 补全候选来源：autocomplete 的分发与三层信息

#### 4.2.1 概念说明

补全是 typst-ide 里最复杂的查询（`complete.rs` 约 2100 行，占整个 crate 的四成）。它的根本难题是：**一个光标位置同时处于多层上下文中**——它属于某个语法模式（markup / code / math）、可能正卡在字段访问的点半身后、可能在函数调用的参数列表里、可能在 import 路径字符串里……每种上下文能补全的东西完全不同。

typst-ide 的解法是把「光标处能补什么」拆成三层信息来源，按 ** specificity 递减** 的顺序尝试：

1. **结构化上下文**（最具体）：`#().|` 补方法、`#import "|"` 补路径、`set |` 补可 set 的元素、`func(|)` 补参数名；
2. **语法层绑定**（不需要求值）：`named_items` 沿 AST 向左向上走，收集光标**之前**可见的 `#let` 绑定、import 项、for 循环变量、闭包参数；
3. **全局作用域**（兜底）：`Library` 的 `global` 或 `math` 作用域，按模式二选一。

另有一类特殊来源——**标签 / 文献键**——它们只有编译出文档（Introspector，u4-l7）后才存在，所以必须传入 `output`。

#### 4.2.2 核心流程

`autocomplete` 主干（引用自 [crates/typst-ide/src/complete.rs:38-71](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L38-L71)）：

```rust
pub fn autocomplete(
    world: &dyn IdeWorld,
    output: Option<impl AsOutput>,
    source: &Source,
    cursor: usize,
    explicit: bool,
) -> Option<(usize, Vec<Completion>)> {
    let leaf = LinkedNode::new(source.root()).leaf_at(cursor, Side::Before)?;
    let mut ctx = CompletionContext::new(world, output.as_ref().map(|v| v.as_output()),
                                        source, &leaf, cursor, explicit);

    // 顺带用 mode_after 确认不在注释里
    let mode = ctx.leaf.mode_after()?;

    _ = complete_field_accesses(&mut ctx)      // #().|   x.fa|
        || complete_open_labels(&mut ctx)      // (<la|
        || complete_imports(&mut ctx)          // #import "|",  #import "x.typ": |
        || complete_rules(&mut ctx)            // set |, show |, show strong: |
        || complete_params(&mut ctx)           // func(|), func(param:|)
        // 更泛化的补全放在更具体的之后
        || match mode {
            SyntaxMode::Markup => complete_markup(&mut ctx),
            SyntaxMode::Math => complete_math(&mut ctx),
            SyntaxMode::Code => complete_code(&mut ctx),
        };

    Some((ctx.from, ctx.completions))
}
```

流程要点：

1. **定位叶子**：`leaf_at(cursor, Side::Before)` 把字节偏移换成语法树叶子（4.3.3 详讲）；`mode_after()` 顺便充当「不在注释里」的守卫——注释和 raw 正文没有模式，返回 `None`，整个补全直接放弃。
2. **短路分发**：一串 `||` 意味着第一个返回 `true`（认领了这个位置）的补全器独占结果；全都不认领才落到按模式的通用补全。
3. **返回 `(from, completions)`**：`from` 是替换起点（通常是标识符的开头或光标本身），候选列表未做前缀过滤。

`scope_completions`（第 2、3 层来源的汇合点）的候选收集逻辑：

```text
scope_completions(filter):
    defined ← 空的 BTreeMap<名字, Option<Value>>     // 按名字去重、按字典序输出
    named_items(world, leaf, |item| {                 // 第 2 层：语法层绑定
        若 item.name() 非空 且 (无值 或 值通过 filter):
            defined.insert(name, value)
    })
    对 defined 中每个条目 → value_completion / 纯名字候选
    对 globals(world, leaf) 中每个绑定:                // 第 3 层：全局作用域
        若通过 filter 且 defined 里没有同名者 → 补进候选
        （↑ 这一步保证本地绑定遮蔽全局同名项）
```

其中 `filter` 会被 `check_value_recursively` 包装一层：值本身或它的任何组成部分（字典字段、内容字段、模块绑定）匹配即算通过，且最多递归 1000 步防爆栈——这就是 `#rect(fill: |)` 既能补 `red` 也能补「装着颜色的字典 / 模块」的原因。

#### 4.2.3 源码精读

**候选的数据形状**。[crates/typst-ide/src/complete.rs:74-113](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L74-L113)：`Completion` 有四个字段——`kind`（十种 `CompletionKind`：Syntax / Func / Type / Param / Constant / Path / Package / Label / Font / `Symbol(EcoString)`）、`label`（展示名）、`apply`（实际插入文本，支持 `${}` 片段语法，缺省用 label）、`detail`（一句话描述）。它还 derive 了 `Serialize/Deserialize`——方便宿主跨进程（如 LSP）传输。

**第 2 层来源 `named_items`**：[crates/typst-ide/src/matchers.rs:9-29](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L9-L29) 是一个「双层游走」：

- 外层 `while` 从叶子沿 `parent()` 向上；
- 内层对每个祖先先向左扫 `prev_sibling()`；

游走中识别四类绑定（对应 Typst 的四种作用域入口）：

| 语法 | 代码位置 | 产出的 NamedItem |
| --- | --- | --- |
| `#let x = ..` / `#let f() = ..` | [matchers.rs:18-29](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L18-L29) | `Var` / `Fn`（按是否闭包区分） |
| `#import "foo.typ" [as name][: items]` | [matchers.rs:31-130](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L31-L130) | `Module`（bare 导入 / as 重命名）或 `Import`（逐项、含 `*` 展开与 `a.b` 链式深挖） |
| `for x in ..` 的模式绑定 | [matchers.rs:137-146](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L137-L146) | `Var`（只认祖先是 for 且当前节点不在 `in` 右侧） |
| 闭包参数 | [matchers.rs:148-176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L148-L176) | `Var`（先确认节点确实在闭包**体内**才收参数） |

关键在于：这一层**完全不做求值**，只看语法形状，所以对半编辑状态天然鲁棒；import 项的「值」则借 `analyze_import`（4.3.3）按需解析。

**第 3 层来源 `globals`**：[crates/typst-ide/src/utils.rs:175-182](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/utils.rs#L175-L182) —— 按光标所在模式二选一：

```rust
pub fn globals<'a>(world: &'a dyn IdeWorld, leaf: &LinkedNode) -> &'a Scope {
    let library = world.library();
    if leaf.mode_after() == Some(SyntaxMode::Math) {
        library.math.scope()
    } else {
        library.global.scope()
    }
}
```

这正是 u3-l2 讲过的 `Library` 双作用域在 IDE 侧的消费点：`$col$` 补到的是 `colon`（math 模块里的符号），而 `$#col$`（`#` 已切回 code）补到的是 `colbreak`——现有测试 [complete.rs:1687-1693](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1687-L1693)（`test_autocomplete_math_scope`）就是这条规则的活教材。

**两层汇合**：[crates/typst-ide/src/complete.rs:1436-1475](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1436-L1475) 即上文伪代码对应的 `scope_completions` 实现；本地名存在时全局同名者被 `!defined.contains_key(name)` 挡掉。

**结构化上下文举例——字段访问**：[crates/typst-ide/src/complete.rs:116-161](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L116-L161) `complete_field_accesses` 识别两种形态：`emoji.|`（叶子是点）和 `emoji.fa|`（叶子是点后的半个标识符），先用 `analyze_expr` 求出点左侧表达式的值，再交给 `field_access_completions`（[complete.rs:164-251](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L164-L251)）枚举：类型/元素作用域里带 `self` 参数的方法、值自身的 `scope()`、`fields_on` 列出的字段、符号的修饰变体（`arrow.r`）、字典键、args 的命名键、元素函数的可 set 字段。一个小而精的细节：markup 里的文本点（`#() .`）因为点前有 trivia 会被拒补，测试 [complete.rs:1673-1684](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1673-L1684)（`test_autocomplete_dot_whitespace`）专门锁定了这一行为。

**依赖编译产物的来源——标签**：[crates/typst-ide/src/complete.rs:1226-1259](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1226-L1259) `label_completions` 第一行就是 `let Some(output) = self.output else { return }`；标签清单来自 `analyze_labels`（[crates/typst-ide/src/analyze.rs:110-148](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/analyze.rs#L110-L148)），它从文档的 Introspector 查询带标签元素、用元素 `body` 的纯文本做描述，并把文献键接在后面——返回的 `split` 偏移把「节点标签」和「文献键」分成两段，供 `@|`（全要）与 `#cite(<|)`（只要文献键）分别取用。

#### 4.2.4 代码实践

1. **实践目标**：亲手查询一次补全，观察「候选 = 本地绑定 + 全局作用域」且**不做前缀过滤**。
2. **操作步骤**：
   - 临时在 [crates/typst-ide/src/complete.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs) 文件末尾的 `mod tests` 里（`test` 辅助函数定义在 [complete.rs:1617-1623](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1617-L1623) 之后即可）追加一个测试（**示例代码**，验证完记得还原）：

     ```rust
     #[test]
     fn practice_completion_sources() {
         // 光标 -2：落在 "loc" 标识符上（负数从末尾数，见 tests.rs 的 FilePos）
         let res = test("#{ let local = 1; loc }", -2);
         let (from, completions) = res.unwrap();
         eprintln!("from = {from}");
         for c in &completions {
             eprintln!("{:?}\t{}\tapply={:?}", c.kind, c.label, c.apply);
         }
     }
     ```

   - 运行：`cargo test -p typst-ide --lib practice_completion_sources -- --nocapture`。
3. **需要观察的现象**：
   - `from` 等于 `loc` 的起始字节偏移（而不是光标处的 `}` 前位置）；
   - 候选里同时出现 `local`（kind 为 `Constant`，来自 `named_items` 的第 2 层）和 `upper`、`text`（kind 为 `Func`，来自 global 作用域的第 3 层）以及 `int` 这类 `Type` 候选；
   - 虽然只打了 `loc` 三个字母，`local` 仍以完整形式出现在列表里。
4. **预期结果**：打印出一个按字典序排列的候选清单，能清晰看到三层来源混排；由此确认「前缀过滤是编辑器的职责」。
5. 首次构建 typst-ide 的测试目标需要编译 dev-dependencies（typst-pdf 等），耗时较长属正常。输出条目数较多（global 作用域有数百项），可用 `| grep -E 'local|upper|int\b'` 缩小观察范围（管道命令在 shell 中执行，非 cargo 参数）。

#### 4.2.5 小练习与答案

**练习 1**：`#import "@|"` 的包名候选和 `#read("|")` 的文件候选，typst-ide 从哪里拿数据？

**答案**：分别来自 `IdeWorld` 的两个可选方法。`package_completions`（[complete.rs:1146-1161](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1146-L1161)）读 `world.packages()`，按命名空间、名称排序、版本倒序，且默认去重到每个包只留最新版（路径里含 `:` 表示用户要枚举全部版本）；`file_completions`（[complete.rs:1164-1184](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L1164-L1184)）读 `world.files(base, prefix)`，再按当前文件目录换算成相对路径。测试 [tests.rs:120-135](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tests.rs#L120-L135)（`TestWorld` 对 `files` 的实现）演示了宿主侧的最小实现。

**练习 2**：为什么 `set |` 的候选比 `show |` 少？两者分别怎么过滤？

**答案**：见 `complete_rules` 的两个分支（[complete.rs:375-405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L375-L405)）。`set_rule_completions` 只保留「至少有一个可 set 参数」的函数（`func.params().any(|param| param.settable())`）；`show_rule_selector_completions` 只保留能对应到元素的函数（`func.to_element().is_some()`），并额外附赠文本选择器与正则选择器两个片段。

**练习 3**：用户在 `#text(size: 12pt, |)`（逗号后）显式请求补全，会走哪条路径？如果不显式（隐式触发）呢？

**答案**：走 `complete_params`（[complete.rs:436-500](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L436-L500)）。它先向左找「决定性 token」（`(` / `,` / `:` 等），再对 callee 做 `analyze_expr_with_fallback` 拿到 `Func`，枚举其参数：已给出的位置参数会被跳过、已给出的命名参数不再出现。逗号**正后方**的隐式触发不产生候选（条件 `deciding.range().end < ctx.cursor || ctx.explicit`），只有显式触发才补——现有测试 [complete.rs:2026-2042](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L2026-L2042) 逐偏移断言了这一行为。

---

### 4.3 跳转与定位：definition、deref_target 与 Span 反查链

#### 4.3.1 概念说明

「跳转到定义」要回答的问题是：光标下的这个名字，究竟绑定在哪里？typst-ide 把答案抽象成三种 `Definition`：

- `Span(Span)`：定义在本文件或导入文件中的某段源码（比如 `#let x` 的 `x`）；
- `File(FileId)`：定义就是整个文件（`#import "other.typ"` 的路径字符串）；
- `Std(Value)`：定义在标准库里（比如 `upper`），没有源码位置，只能给出值本身供宿主展示文档。

这条链路的两个支点都是 u2-l4 的老朋友：**`LinkedNode`**（带父指针和字节偏移的节点游标，把「光标字节」变成「树上的节点」）与 **Span 的有序编号**（把「树上的节点」变成「可跨函数传递的 8 字节定位」，再反查回字节区间）。

#### 4.3.2 核心流程

一次 `definition(world, output, source, cursor, side)` 调用：

```text
LinkedNode::new(source.root())
   │ leaf_at(cursor, side)              光标字节 → 叶子节点（Side 消歧边界）
   ▼
deref_target(leaf)                       叶子 → 「可操作对象」分类
   │
   ├─ VarAccess / Callee（变量访问 / 被调函数名）
   │    ① named_items 按名匹配 → 命中 → Definition::Span(该绑定的 span)
   │    ② 未命中 → analyze_expr 取值 → 若值自带非 detached 的 span → Span
   │    ③ 还未命中 → globals 里找到 → Definition::Std(标准库值)
   │
   ├─ ImportPath / IncludePath（import/include 的路径字符串）
   │    analyze_import 解析出模块 → module.file_id() → Definition::File
   │
   └─ Ref（@引用）
        构造 Selector::Label → output 的 Introspector.query_first
        → 元素 span → Definition::Span                 （需要编译产物）
```

其中 ①②③ 的顺序本身就是「定义就近原则」：本地绑定 > 求值结果 > 标准库。

#### 4.3.3 源码精读

**光标 → 叶子**。`Side` 与 `leaf_at`：[crates/typst-syntax/src/node.rs:1192-1197](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1192-L1197) 定义 `Side::Before / After`——当光标恰好在两个 token 的边界上，编辑器必须告诉编译器自己指的哪一侧（这对引号、括号边界尤其重要）；[node.rs:1268-1273](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1268-L1273) 的 `leaf_at` 按此分派给 `leaf_before` / `leaf_after`（[node.rs:1229-1265](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1229-L1265)），两者都**跳过 trivia**（空白与注释），所以悬停在注释上不会得到任何可操作对象。

**叶子 → 分类**。`deref_target`：[crates/typst-ide/src/matchers.rs:229-270](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L229-L270) 先向上爬到第一个「是表达式」的祖先，再按 `ast::Expr` 的变体归类：`FuncCall` 取 callee 子节点归为 `Callee`，`SetRule` 的 target 同理；`Str` 要看父节点是不是 `ModuleImport` / `ModuleInclude` 才归为 `ImportPath` / `IncludePath`；`Label`、`Ref` 各自一类；`Ident` / `FieldAccess`（含数学版）归 `VarAccess`。产物 `DerefTarget` 枚举见 [matchers.rs:272-291](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L272-L291)。这个分类是补全、定义、悬停**共用**的路由层——一处分类，三处消费。

**按名匹配绑定**。`definition` 主体：[crates/typst-ide/src/definition.rs:27-88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L27-L88)。`Definition` 枚举本身在 [definition.rs:11-20](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L11-L20)。VarAccess/Callee 分支的代码骨架（节选自 [definition.rs:40-64](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L40-L64)）：

```rust
DerefTarget::VarAccess(node) | DerefTarget::Callee(node) => {
    let name = node.cast::<ast::Ident>()?.get().clone();
    if let Some(src) = named_items(world, node.clone(), |item: NamedItem| {
        (*item.name() == name).then(|| Definition::Span(item.span()))
    }) { return Some(src); }                      // ① 本地 / 导入绑定

    if let Some((value, _)) = analyze_expr(world, &node).first() {
        let span = match value {
            Value::Content(content) => content.span(),
            Value::Func(func) => func.span(),
            _ => Span::detached(),
        };
        if !span.is_detached() && span != node.span() {
            return Some(Definition::Span(span));  // ② 求值得到的定义处
        }
    }

    if let Some(binding) = globals(world, &leaf).get(&name) /* ③ 标准库 */ ...
}
```

注意 `named_items` 在这里被当「搜索谓词」用：回调返回 `Some` 即提前终止游走——同一个函数，收集模式（4.2）与查找模式（这里）只是回调协议不同。

**语义层的值从哪来**：`analyze_expr`（[crates/typst-ide/src/analyze.rs:12-50](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/analyze.rs#L12-L50)）对字面量（`none` / `auto` / bool / int / float / numeric / str）直接算；对 `context` 表达式深入其子节点；对字段访问的目标节点回溯到父级；**其余一律交给 `typst::trace::<PagedDocument>(world.upcast(), node.span())`**。`typst::trace`（[crates/typst/src/lib.rs:88-96](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L88-L96)）的原理是带着 `Traced::new(span)` **重新编译一遍文档**，求值器路过该 span 时把观测到的值记进 `Sink`，最后返回 `sink.values()`。这是 IDE 里最昂贵的一条兜底路径，也解释了 `analyze_expr` 返回的是「值 + 样式」的**列表**——循环、分支可能让同一表达式产生多个值。另有 `analyze_expr_with_fallback`（[analyze.rs:56-83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/analyze.rs#L56-L83)）在死代码里退化成查 global 作用域，保证 best-effort。

**import 路径 → 模块**：`analyze_import`（[analyze.rs:86-99](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/analyze.rs#L86-L99)）拿到字符串后，通过 `utils::with_engine`（[utils.rs:21-38](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/utils.rs#L21-L38)）临时拼一个只带 `EmptyIntrospector` 的 `Engine`，再调用 `typst_eval::import` 复用求值器真正的导入逻辑——IDE 不重复实现路径解析，而是借 u3-l6 讲过的 `import` 机制本身。

**Span → 字节区间（反查）**。跳转的最后一步是把 `Definition::Span(span)` 变成编辑器能高亮的区间。宿主用 `WorldExt::range`（对 `Source::range` 的封装），而 [crates/typst-syntax/src/source.rs:121-134](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L121-L134) 的实现是 `LinkedNode::new(root).find_number(num)?.range()`。`find_number`（[node.rs:1024-1055](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1024-L1055)）正是 u2-l4 讲过的**有序性剪枝**：父编号小于全部子编号、兄弟编号递增，所以一旦目标编号小于当前节点就整棵剪掉、只有「下一个兄弟的编号大于目标」的子树才需要进入。Span 的位布局（高 16 位 FileId + 低 48 位编号，及 Detached / Number / Range 三形态的 `SpanKind`）见 [crates/typst-syntax/src/span.rs:62-83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L62-L83) 与 [span.rs:92-101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L92-L101)。

**跳转的另一半**：从编译产物跳回源码。`Jump` 枚举（[crates/typst-ide/src/jump.rs:16-23](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/jump.rs#L16-L23)）有 `File(FileId, usize)`、`Url`、`Position(PagedPosition)` 三种去向；`jump_from_click`（[jump.rs:34-48](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/jump.rs#L34-L48)）经密封 trait `JumpFromDocument` 对 `PagedDocument` / `HtmlDocument` 分别实现，最终都落到帧内图元携带的 span，再经 `Jump::from_span`（[jump.rs:25-31](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/jump.rs#L25-L31)）用 `world.range(span)` 换回文件与偏移——与 `definition` 收尾用的是同一条反查链。反方向 `jump_from_cursor`（[jump.rs:340-352](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/jump.rs#L340-L352)）从源码光标找预览位置，同样以 `leaf_at` 开场。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：对一段「含变量与函数调用」的源码，在调用点查询自动补全与定义跳转，打印返回的候选与目标 Span，并反查回源码文本。
2. **操作步骤**：
   - 临时在 [crates/typst-ide/src/definition.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs) 的 `mod tests` 里追加（**示例代码**，验证后还原；所需 `use` 就写在函数体内，不动文件头）：

     ```rust
     #[test]
     fn practice_definition_and_completion() {
         use typst::WorldExt;
         use typst::syntax::Side;
         use typst_layout::PagedDocument;
         use crate::{autocomplete, definition};

         let text = "#let name = \"typst\"; #upper(name)";
         let world = TestWorld::new(text);
         let doc = typst::compile::<PagedDocument>(&world).output.ok();

         // ① 定义跳转：光标在调用处的 name 上（字节偏移 29，即 "name" 内部）
         let def_of_name = definition(&world, doc.as_ref(), &world.main, 29, Side::After);
         eprintln!("definition(name) = {def_of_name:?}");
         if let Some(super::Definition::Span(span)) = &def_of_name {
             eprintln!("  span id = {:?}, range = {:?}", span.id(), world.range(*span));
         }

         // ② 定义跳转：光标在被调函数 upper 上（字节偏移 23）
         let def_of_upper = definition(&world, doc.as_ref(), &world.main, 23, Side::After);
         eprintln!("definition(upper) = {def_of_upper:?}");

         // ③ 补全：光标在 "#" 之后（字节偏移 22）→ code 模式通用补全
         let completions = autocomplete(&world, doc.as_ref(), &world.main, 22, true);
         if let Some((from, list)) = completions {
             eprintln!("autocomplete from = {from}, total = {}", list.len());
             for c in list.iter().filter(|c| c.label.as_str().len() < 10) {
                 eprintln!("  {:?}\t{}", c.kind, c.label);
             }
         }
     }
     ```

   - 运行：`cargo test -p typst-ide --lib practice_definition_and_completion -- --nocapture`。
3. **需要观察的现象**：
   - ① 打印 `Span(..)` 形态的 `Definition`，且 `world.range(span)` 返回 `Some(5..9)`——正是源码里 `#let name` 的 `name` 标识符区间；
   - ② 打印 `Std(..)` 形态——`upper` 是标准库函数，没有源码位置，走的是第 ③ 层兜底（其 `Value::Func` 的 span 是 detached，被 [definition.rs:54](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L54) 的 `is_detached` 检查放行）；
   - ③ 候选里同时有 `name`（本地绑定）与 `upper`、`int` 等全局项。
4. **预期结果**：三条输出与上述现象一致。其中 ① 的 `5..9` 与现有测试 [definition.rs:158-162](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L158-L162)（`test_definition_let` 断言 `5..6`）同源——那套测试用的源码 `#let x;` 更短，区间算法完全相同。③ 的候选总数与全局作用域规模一致（数百项），打印已按短名过滤以免刷屏。
5. 偏移数字依赖源码字符串逐字一致（含分号后的空格），若改了字符串需重新数偏移；`5..9` 之外的精确数值（如候选总数）标记为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`definition` 的文档注释说「传入 document 是可选的，但能增强结果」，哪个分支**必须**有 document？

**答案**：`DerefTarget::Ref` 分支（[definition.rs:76-82](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L76-L82)）——`@hi` 引用要跳到 `<hi>` 标注的元素，而元素的位置只有在排版完成后才能通过 `Introspector.query_first(Selector::Label(..))` 查到（u4-l7 的内省表）。`output?` 在这里直接短路返回 `None`。测试 `test_definition_ref`（[definition.rs:193-196](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L193-L196)）验证的正是这条链。

**练习 2**：`deref_target` 为什么把 `FuncCall` 映射成 `Callee(expr_node.find(call.callee().span())?)`，而不是直接用整个调用表达式？

**答案**：见 [matchers.rs:243-248](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/matchers.rs#L243-L248)。跳转与悬停关心的是「被调用的名字」而不是实参；用 callee 的 span 从表达式节点里 `find` 出子节点，后续 `named_items` / `globals` 的按名匹配才落在 `upper` 这个标识符上，而不是整个 `upper(name)` 调用。

**练习 3**：`analyze_expr` 为什么返回 `EcoVec<(Value, Option<Styles>)>` 而不是单个 `Value`？

**答案**：因为它兜底调用 `typst::trace`（[crates/typst/src/lib.rs:88-96](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L88-L96)），同一表达式在不同迭代 / 分支路径下可能被观测到多个值（及各自生效的样式）。悬停提示里出现的 `red (×3)` 这类聚合就是消费端（4.4.3 的 `expr_tooltip`）对多值列表去重计数后的结果。

---

### 4.4 悬停提示：tooltip 尝试链与文档来源

#### 4.4.1 概念说明

悬停提示（hover tooltip）是把「光标处是什么」翻译成一句话给用户看。它没有统一算法，而是六种专门 tooltip 组成的**尝试链**——这与 4.2 的补全分发同构：具体的先试，泛化的兜底。六种 tooltip 的信息来源各不相同（参数元数据、字体簿、内省器、import 解析、表达式求值、语法形状），正好把本讲前几个模块的机制全部串起来一遍。

#### 4.4.2 核心流程

`tooltip` 主入口（[crates/typst-ide/src/tooltip.rs:24-42](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L24-L42)）：

```text
leaf_at(cursor, side)，trivia 直接放弃
  ① named_param_tooltip   函数参数名 / 参数字符串值 → 参数文档
  ② font_tooltip          text(font: "字体名") → 字体族摘要
 ③ label_tooltip         @引用 / <标签> → 标签指向内容的纯文本（需要 output）
  ④ import_tooltip        import 的 * → "This star imports `a` and `b`"
  ⑤ expr_tooltip          表达式 → 值的 repr / 文档 / 长度换算
  ⑥ closure_tooltip       闭包的 = 或 => → "This closure captures `x`"
按序 or_else，第一个 Some 生效
```

#### 4.4.3 源码精读

**① 参数提示**：[tooltip.rs:206-249](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L206-L249) 用一长串 let-chains 验证「叶子确实在函数调用 / set 规则参数列表的命名对里」，找到 callee 后 `analyze_expr_with_fallback` 取 `Func`，再按悬停的是参数名还是字符串值分别查 `find_param_docs` / `find_string_doc`。注意它同时处理 `Args` 与 `MathArgs`——数学模式里的函数调用也在照顾范围内。

**② 字体提示**：[tooltip.rs:263-284](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L263-L284) 只认 `text(font: "某字符串")` 形态，去 `world.book()`（u3-l1 的字体簿）里找到字体族后，用 `summarize_font_family`（[utils.rs:41-121](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/utils.rs#L41-L121)）汇总变体数、字重区间、是否可变字体、是否含斜体等。

**⑤ 表达式提示（信息量最大的一条）**：[tooltip.rs:54-122](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L54-L122)。只分析「可嵌入 markup 的表达式」或数学变量访问；拿到 `analyze_expr` 的多值列表后依次尝试：单值且有文档 → 文档摘要；长度值 → `12pt = 4.23mm = 0.42cm = 0.17in` 式换算（`length_tooltip`，[tooltip.rs:174-184](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L174-L184)）；否则去重聚合成 `repr::pretty_comma_list`（连续相同值折叠成 `值 (×次数)`，超过 `Sink::MAX_VALUES` 截断为 `...`）。

**⑥ 闭包捕获提示**：[tooltip.rs:143-171](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L143-L171) 是唯一**纯语法层**的 tooltip：只悬停在闭包的 `=` / `=>` 上时触发，用 typst-eval 的 `CapturesVisitor`（u3-l6 讲过闭包捕获）算出捕获名单。一个值得学习的 UX 细节：注释明说「在整个子树上都显示太吵」，所以只挑两个标点 token 作为热点。

**文档的两副面孔**：[crates/typst-ide/src/docs.rs:10-49](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/docs.rs#L10-L49)。`find_value_docs` 先问值自带的 `value.docs()`——这是 u5-l5 讲过的「一份声明、三处消费」宏体系烤进 `NativeFuncData` 的静态文档；若值是**用户闭包**，则退而求其次：经 `func.span()` → `world.source(id)` → `source.find(span)` 反查回源码里的 `Closure` 节点，再由 `Docs::collect_doc_comment`（[docs.rs:66-79](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/docs.rs#L66-L79)）向上收集 `///` 文档注释（三斜杠会剥掉一层）。docs.rs 里那段全大写的注释特别坦诚：这只是对生态现状的务实适配，**不代表**语言层面标准化了任何文档注释格式。

#### 4.4.4 代码实践

1. **实践目标**：亲手触发 `expr_tooltip` 的长度换算，并观察「悬停用户函数看到文档注释」的完整链路。
2. **操作步骤**：
   - 临时在 [crates/typst-ide/src/definition.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs) 的 `mod tests` 追加（**示例代码**，验证后还原）：

     ```rust
     #[test]
     fn practice_tooltip() {
         use typst::syntax::Side;
         use crate::tests::TestWorld;
         use crate::tooltip;

         let world = TestWorld::new("#text(size: 12pt)");
         // "12pt" 是 12..16 的叶子，光标 13 落在其中
         let t = tooltip(&world, None, &world.main, 13, Side::After);
         eprintln!("tooltip(12pt) = {t:?}");

         let world = TestWorld::new(crate::tests::EXAMPLE_CLOSURE);
         // EXAMPLE_CLOSURE 中 "forest" 参数名的位置：见 tests.rs L265-278 的字符串
         let text = world.main.text();
         let pos = text.find("forest").unwrap() as usize;
         let t = tooltip(&world, None, &world.main, pos + 2, Side::After);
         eprintln!("tooltip(forest) = {t:?}");
     }
     ```

   - 运行：`cargo test -p typst-ide --lib practice_tooltip -- --nocapture`。
3. **需要观察的现象**：第一行打印出 `Code` 形态的长度换算串；第二行打印出 `Text` 形态的文档摘要 `More trees.`（来自 EXAMPLE_CLOSURE 里 `// More *trees*.` 注释的 markup 渲染）。
4. **预期结果**：`tooltip(12pt)` 约 `Some(Code("12pt = 4.23mm = 0.42cm = 0.17in"))`（12pt 换算：\( 12 \times \frac{25.4}{72} \approx 4.23 \) 毫米，具体保留位数**待本地验证**）；`tooltip(forest)` 为 `Some(Text("More trees."))`——这正是现有测试 [complete.rs:2113-2121](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/complete.rs#L2113-L2121)（`test_autocomplete_user_function_params`）断言过的同一份摘要，只是这次经由 tooltip 链取出。
5. `EXAMPLE_CLOSURE` 定义在 [crates/typst-ide/src/tests.rs:265-278](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tests.rs#L265-L278)，可先阅读原文对照。

#### 4.4.5 小练习与答案

**练习 1**：tooltip 主入口为什么在一切之前先检查 `leaf.kind().is_trivia()` 并返回 `None`？

**答案**：[tooltip.rs:31-34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L31-L34)。空白与注释不是任何语义对象，悬停它们不该弹出提示；`leaf_at` 虽然跳过 trivia 返回邻近叶子，但光标真的落在 trivia 上时体验上应保持沉默，所以这里显式拦截。

**练习 2**：悬停 `#import "lib.typ": *` 里的 `*` 会看到什么？它需要编译产物吗？

**答案**：`import_tooltip`（[tooltip.rs:125-140](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L125-L140)）会列出「This star imports `a` and `b`」——它经 `analyze_import` 真正加载模块并展开其作用域。不需要 `output`（不走 Introspector），但需要宿主 World 能 `source()` 出那个文件。

**练习 3**：`expr_tooltip` 里为什么有 `if expr.is_literal() { return None; }` 这样一条「提前放弃」？

**答案**：[tooltip.rs:90-92](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tooltip.rs#L90-L92)。字面量（如 `"hello"`、`1.5`）的值就写在源码里，再弹一个一模一样的 repr 只是噪音；唯一的例外是长度字面量——在到达这行之前，`length_tooltip` 已经把 `12pt` 换算成了四种单位的有用信息并返回了。

## 5. 综合实践

把本讲三个最小模块串成一个「迷你 IDE 调查报告」。目标源码是**双文件**场景，一次看全三层候选来源与跨文件跳转：

```text
main.typ:   #import "lib.typ": greet; #let who = "world"; #greet(who)
lib.typ:    /// Says hi.
            #let greet(name) = [Hi, #name!]
```

任务：在 typst-ide 的测试模块里临时写一个测试，用 `TestWorld::new(..).with_source("lib.typ", ..)` 搭好环境（`with_source` 见 [tests.rs:41-47](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/tests.rs#L41-L47)），然后：

1. 在 `#` 后查询补全：验证候选同时包含 `greet`（第 2 层，来自 import 项）、`who`（第 2 层，来自 `#let`）与 `upper`（第 3 层，global 作用域）；
2. 在 `greet` 的调用处查询定义：预期得到 `Definition::Span`，且 `world.range(span)` 落在 **lib.typ** 的 `greet` 标识符上——与现有测试 `test_definition_cross_file`（[definition.rs:173-177](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/definition.rs#L173-L177)）断言的行为一致，跨文件体现为 `span.id()` 的 vpath 不同；
3. 悬停 `greet(?)` 的参数名 `name`：预期显示 `Says hi.`——它沿着「import 项的值 → 用户闭包 → func.span() 反查 lib.typ → 收集 `///` 注释」的完整链路（4.4.3 的 `find_value_docs`）；
4. 把三步结果整理成「查询 → API → 信息来源（named_items / analyze_expr / globals / docs / introspector）」的表格。

验收标准：不追求断言，`eprintln!` 输出能对上上述预期即可；完成后还原全部临时改动（`git diff` 应为空）。偏移量请按实际字符串数，或模仿 tests.rs 的 `FilePos` 负索引写法。整体输出为**待本地验证**。

## 6. 本讲小结

- typst-ide 是 typst-syntax 与 typst-library 之上的**纯查询层**：8 组自由函数 + 一个 `IdeWorld`（World 加可选的 `packages()`/`files()`），没有状态、没有回调，前缀过滤与渲染都留给宿主。
- 所有查询的第一步都是同一条定位链：字节偏移 → `LinkedNode::leaf_at(cursor, side)` → 叶子（跳过 trivia），再用 `deref_target` 把叶子分类为 VarAccess / Callee / ImportPath / Ref 等七种可操作对象——补全、定义、悬停共用这套路由。
- 补全候选来自三层信息：结构化上下文（参数、路径、set/show）短路优先；语法层 `named_items` 沿 AST 收集光标前可见的绑定（不求值、对半编辑鲁棒）；`globals` 按模式在 `Library` 的 global/math 作用域二选一兜底，本地名遮蔽全局名。标签类候选独木桥：必须传入编译产物走 Introspector。
- 定义跳转是「就近原则」的三级瀑布：本地绑定（Span）→ 求值结果的 span（必要时 `typst::trace` 带着目标 span 重新编译取值）→ 标准库（`Definition::Std`）；import 路径跳文件（`Definition::File`），`@引用` 跳排版后的元素（依赖 output）。
- 悬停是六种专门 tooltip 的尝试链，文档有「原生静态文档」与「源码 `///` 注释反查」两副面孔——后者靠 Span → `Source::find` → LinkedNode 反查实现，再次印证 Span 有序编号是整个工具链的粘合剂。
- 全程贯彻 best-effort：允许返回 `None` / 空列表，绝不 panic；`ast.rs` 用 lint 把这条契约焊死在类型系统门口。

## 7. 下一步学习建议

- **u5-l7（字体与包生态）**：本讲两处摸到了 `world.book()`（字体提示、font 补全）与 `world.packages()`，下一讲看 typst-kit 与 CLI 如何在真实宿主里供给这两份数据。
- **u5-l8（增量编译）**：`typst::trace` 每次悬停都重新编译显然太贵——comemo 的记忆化让这条路径在实践中可承受，读完下一讲再回头看 `analyze_expr` 会有全新体感。
- **源码延伸阅读**：官方编辑器集成 [tinymist](https://github.com/Myriad-Dreamin/tinymist)（非本仓库）是把本讲 API 面接成 LSP 的完整范例；仓库内可对照 [crates/typst-ide/src/jump.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-ide/src/jump.rs) 的 PDF 预览点击跳转（`jump_from_click_in_frame` 对帧图元的逐层求交），体会 Span 在「成品文档」侧的另一半生命周期。
