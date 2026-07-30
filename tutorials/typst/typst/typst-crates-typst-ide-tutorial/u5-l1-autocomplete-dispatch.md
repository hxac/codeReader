# autocomplete 入口与补全分发管线

## 1. 本讲目标

本讲是「自动补全引擎核心」单元的第一篇。整篇补全逻辑集中在一个约 2000 行的 `complete.rs` 里，本讲不展开任何具体补全分支的内部细节，而是先看清它的**入口骨架**——补全请求是怎么进来的、怎么被分发的、最终怎么把结果交给调用方的。

学完后你应该能够：

- 说清 `autocomplete` 公共函数的输入参数与返回值 `(from, completions)` 中 `from` 的确切含义。
- 理解 `CompletionContext::new` 构造的上下文都装了什么、为什么需要一个共享的可变结构。
- 理解 `mode_after` 一行代码如何同时完成「给出语法模式」和「屏蔽注释」两件事。
- 读懂 `complete_field_accesses → open_labels → imports → rules → params → 通用模式` 这条短路分发链的设计原因，以及 `explicit` 标志如何改变各分支的触发条件。

后续讲义（u5-l2 数据模型、u5-l3 三种模式补全、u6 系列各专项补全）会逐一打开这条链上的每个 `complete_*` 函数，本讲只负责把它们的「门牌」认全。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（来自前置讲义）：

- **从光标到语法树节点**（u2-l1）：`LinkedNode::new(source.root()).leaf_at(cursor, Side::Before)` 把光标字节偏移映射到语法树的某个叶子节点。本讲的入口第一步就是这一行。
- **deref_target / 表达式归类**（u2-l2）：光标处的节点会被归到某个表达式类别。补全的分发并不直接用 `deref_target`，但与它处理的是同一棵语法树、同样的「先定位叶子、再沿祖先判断」思路。
- **analyze_expr 推断值**（u2-l4）：很多补全分支（如字段访问 `complete_field_accesses`、参数补全 `complete_params`）需要调用 `analyze_expr` 得到运行时值。本讲会指出这些「昂贵」操作在分发链中的相对位置。

此外，几个本讲会用到但不展开的术语：

- **SyntaxMode**：Typst 有三种语法模式——`Markup`（正文/标记）、`Math`（公式）、`Code`（`#` 之后的代码）。补全在不同模式下走不同分支。
- **trivia**：空白、注释等没有语义的节点（见 u2-l1）。
- **LSP 的补全模型**：编辑器收到一组候选项后，会把 `[from, cursor)` 这段已有文本替换为用户选中的候选项。`from` 就是「从哪里开始替换」。

## 3. 本讲源码地图

本讲只涉及两个文件，但其中一个是上游库：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `crates/typst-ide/src/complete.rs` | 补全的全部实现 | `autocomplete` 入口（L37–L70）、`CompletionContext` 结构与 `new`（L1051–L1089）、分发链（L57–L67）、各分支的 `explicit` 守卫 |
| `crates/typst-syntax/src/node.rs` | 语法树节点方法 | `mode_after`（L1180–L1209），它决定光标处在哪种语法模式、是否在注释里 |
| `crates/typst-syntax/src/kind.rs` | 语法节点种类 | `kind().mode_after()` 把 `LineComment`/`BlockComment` 映射为 `None`（L585–L586） |
| `crates/typst-ide/src/lib.rs` | 公共导出 | `pub use self::complete::{Completion, CompletionKind, autocomplete};`（L13）|

测试相关（用于代码实践）：

| 文件 | 作用 |
|------|------|
| `crates/typst-ide/src/complete.rs` 末尾 `mod tests` | `test()`（explicit=true）、`test_implicit()`（explicit=false）、`ResponseExt` 链式断言 |

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**`autocomplete` 总入口**、**`CompletionContext::new` 上下文构造**、**`mode_after` 模式判定**、**分发链与短路优先级**。

### 4.1 `autocomplete` 总入口：定位、构造、返回

#### 4.1.1 概念说明

`autocomplete` 是 typst-ide 暴露给语言服务器的**公共补全入口**（在 `lib.rs` 中被 `pub use` 摆上货架）。它的职责非常克制：

1. 把光标定位到一个语法树叶子；
2. 构造一个贯穿整个补全过程的可变上下文；
3. 判定语法模式（顺便屏蔽注释）；
4. 按优先级把上下文依次交给若干个 `complete_*` 函数，首个「认领」的分支负责填充结果；
5. 返回 `(from, completions)`。

它的签名值得逐字读：

```rust
pub fn autocomplete(
    world: &dyn IdeWorld,
    output: Option<impl AsOutput>,
    source: &Source,
    cursor: usize,
    explicit: bool,
) -> Option<(usize, Vec<Completion>)>
```

- `world: &dyn IdeWorld`：数据来源（包列表、文件列表、字体簿等，见 u1-l2）。所有 `complete_*` 都通过上下文间接访问它。
- `output: Option<impl AsOutput>`：上一次编译产物（可选）。**缺失则补全功能降级**——例如标签补全 `label_completions` 只有在 `output` 存在时才能列出文档里的标签。
- `source: &Source` / `cursor: usize`：源码与光标的字节偏移。
- `explicit: bool`：用户是否**主动**触发了补全（如按 Ctrl+Space）。它会让一些「默认不弹」的位置也弹出来。
- 返回 `Option<(usize, Vec<Completion>)>`：`from`（替换起点）+ 候选项列表。

#### 4.1.2 核心流程

```
autocomplete(world, output, source, cursor, explicit)
  │
  ├─ leaf = source.root().leaf_at(cursor, Side::Before)?   // 定位叶子；失败→返回 None
  ├─ ctx  = CompletionContext::new(...)                    // 装上下文
  ├─ mode = ctx.leaf.mode_after()?                         // 求模式；注释→返回 None
  ├─ 依次尝试 complete_field_accesses || open_labels || imports
  │       || rules || params || (按 mode 选 markup/math/code)   // 短路分发
  └─ Some((ctx.from, ctx.completions))                     // 最终统一返回
```

一个关键点：函数返回 `None` 的情形只有两种——`leaf_at` 定位不到叶子，或 `mode_after` 判定光标在注释里。**只要光标不在注释里**，即使没有任何分支命中、候选项为空，函数也返回 `Some((cursor, []))`。换言之，`None` 表示「这个位置根本不该补全」，空列表表示「该补全但没东西可补」。

#### 4.1.3 源码精读

入口骨架（含「先定位、再构造、再求模式」三步）：

[complete.rs#L37-L70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L37-L70) —— `autocomplete` 公共函数本体。注意三处：第 44 行 `leaf_at(cursor, Side::Before)?` 定位叶子；第 45–52 行构造 `ctx`；第 54–55 行求 `mode` 同时屏蔽注释；第 57–67 行短路分发；第 69 行无条件返回 `Some((ctx.from, ctx.completions))`。

关键的一行——求模式即为「注释守卫」：

[complete.rs#L54-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L54-L55) —— 注释写着 "Getting the syntax mode also ensures we are not in a comment."。`mode_after()?` 的 `?` 在注释中返回 `None`，从而整个 `autocomplete` 提前返回 `None`。

公共导出（确认它是对外 API）：

[lib.rs#L13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L13) —— `pub use self::complete::{Completion, CompletionKind, autocomplete};`，把补全入口与两个公开类型摆上货架。

#### 4.1.4 代码实践

**实践目标**：确认「光标在注释里时 `autocomplete` 返回 `None`」。

**操作步骤**：

1. 打开 `complete.rs` 末尾的测试模块，找到 `test()` 与 `test_implicit()` 两个辅助函数（见本讲末尾「测试辅助」一节）。`test()` 用 `explicit = true` 调用 `autocomplete`。
2. 想象一个测试用例：源码为 `"// 注释 |"`（光标在注释文字中间，`|` 代表光标）。手动跟踪 `autocomplete`：`leaf_at` 会定位到 `LineComment` 叶子；`mode_after()` 对 `LineComment` 返回 `None`（见 4.3 节），`?` 触发，函数返回 `None`。

**需要观察的现象**：注释里的光标不会弹任何补全；而正文里同样的位置会弹。

**预期结果**：注释内返回 `None`（LSP 端表现为不弹出候选框）。该结论可直接由源码推出，若想用断言固化，可仿照 `test()` 写：`assert!(test("// 注释 ", -1).is_none());`（负数光标从串尾往前数，见 u8-l1 的 `FilePos`）。**待本地验证**：可把这条加进 `#[test]` 运行 `cargo test -p typst-ide` 确认。

#### 4.1.5 小练习与答案

**练习 1**：`autocomplete` 在什么情况下返回 `None`？什么情况下返回 `Some((cursor, vec![]))`（空列表）？

> **答案**：当 `leaf_at` 无法定位叶子（极少见，通常光标越界），或 `mode_after` 返回 `None`（光标在注释或 raw 文本体内）时返回 `None`。当光标不在注释、但没有任何 `complete_*` 分支命中时，返回 `Some((cursor, vec![]))`——`from` 保持为初始值 `cursor`、`completions` 为空。

**练习 2**：为什么 `output` 参数是 `Option`，而 `source` / `cursor` 不是？

> **答案**：`source` 与 `cursor` 是补全的必备输入（没有源码和光标就无从补全）；`output` 是「增强项」——缺失时绝大多数补全仍可用，只有依赖编译产物的分支（如标签补全）会降级。`Option` 表达了「可选增强、优雅降级」的设计哲学（见 u1-l1、u1-l2）。

---

### 4.2 `CompletionContext::new` —— 补全的共享上下文

#### 4.2.1 概念说明

`autocomplete` 自己很瘦，真正的状态都装在一个可变结构 `CompletionContext` 里。它是一份「贯穿整个补全过程的工作台」：所有 `complete_*` 函数都拿到它的 `&mut` 引用，往 `completions` 里追加候选项、必要时改写 `from`。

把它设计成可变共享结构的好处是：分发链上的每个分支不必各自返回一组候选项再合并，而是直接往同一个 `Vec` 里写，副作用驱动，谁命中谁负责。

#### 4.2.2 核心流程

`new` 把如下字段一次性准备好：

| 字段 | 含义 |
|------|------|
| `world` / `output` | 透传的数据来源与编译产物 |
| `text` / `before` / `after` | 全文 / 光标前子串 / 光标后子串（`before` 常用于 `ends_with(...)` 判定）|
| `leaf` | 光标对应的语法树叶子（`&LinkedNode`）|
| `cursor` | 光标字节偏移 |
| `explicit` | 是否主动触发 |
| `from` | **替换起点**，初始为 `cursor`（替换空串）|
| `completions` | 候选项 `Vec`，初始为空 |
| `seen_casts` | 类型补全去重用的 `FxHashSet<u128>`（见 u6-l4）|

`before` / `after` 是 `&text[..cursor]` / `&text[cursor..]` 切出来的零拷贝引用，所以各分支大量使用 `ctx.before.ends_with("@")` 这类判定，开销极低。

#### 4.2.3 源码精读

结构定义：

[complete.rs#L1051-L1063](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1051-L1063) —— `CompletionContext` 的全部字段。注意 `from: usize` 与 `completions: Vec<Completion>` 这两个会被分支改写的「输出」字段。

构造函数：

[complete.rs#L1067-L1089](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1067-L1089) —— `CompletionContext::new`。重点看第 1080–1081 行 `before: &text[..cursor]`、`after: &text[cursor..]`；第 1085 行 `from: cursor`（替换起点的初始值）；第 1086 行 `completions: vec![]`。

关于 `from` 的语义补充——各分支如何调整它。以「正在输入标识符」为例：

[complete.rs#L142-L157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L142-L157) —— 字段访问 `emoji.fa|` 分支：`ctx.from = ctx.leaf.offset()`，把替换起点回退到已输入的 `fa` 的开头，这样选中候选项后 `fa` 会被整体替换。对比「紧跟点号」分支（第 136 行 `ctx.from = ctx.cursor`）什么都不替换。

> **`from` 的定义**：LSP 客户端会用选中候选项的文本替换源码的 `[from, cursor)` 区间。`from == cursor` 表示「在光标处插入、不删除」；`from < cursor` 表示「先把已输入的部分删掉再插入」。

#### 4.2.4 代码实践

**实践目标**：通过阅读断言，确认 `from` 的实际取值。

**操作步骤**：

1. 在 `complete.rs` 测试模块中，`Response` 类型是 `Option<(usize, Vec<Completion>)>`（[complete.rs#L1515](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1515)），即 `(from, completions)`。
2. 现有断言（如 `must_include`、`must_exclude`）只检查 `completions` 里的 label，不检查 `from`。你可以新增一个临时断言，针对 `#().`（紧跟点号）和 `#assert.e`（正在输入字段名）两种场景，打印返回元组的第一个元素（`from`）。

**需要观察的现象**：

- `#().` 光标在点号后：`from` 应等于 `cursor`（点号后没有已输入文本可替换）。
- `#assert.e` 光标在 `e` 后：`from` 应等于 `e` 的字节偏移（即 `assert.` 之后），选中候选项会替换掉 `e`。

**预期结果**：两个 `from` 不同，恰好对应源码第 136 行（`ctx.from = ctx.cursor`）与第 154 行（`ctx.from = ctx.leaf.offset()`）。**待本地验证**：可在测试里加一行 `println!("{:?}", response.map(|(f, _)| f));` 观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `before` / `after` 用切片引用而不是 `String`？

> **答案**：它们是 `&text[..cursor]` / `&text[cursor..]`，零拷贝借用 `source.text()` 的内存。补全在每次击键时都会被调用，避免分配能显著降低延迟。

**练习 2**：`from` 在 `new` 中初始化为 `cursor`。如果一个 `complete_*` 分支命中并填充了候选项，但**忘了改写** `from`，会发生什么？

> **答案**：LSP 客户端会在光标处插入候选项文本，但不会删掉光标前已输入的部分。例如 `#assert.e` 若不改写 `from`，选中 `eq` 后会变成 `#assert.eeq`（`e` 没被替换）。所以「调整 `from`」是各分支的必做动作。

---

### 4.3 `mode_after` —— 语法模式判定与注释屏蔽

#### 4.3.1 概念说明

`mode_after` 是 typst-syntax 提供的方法（不是 typst-ide 的），但 typst-ide 的 `autocomplete` 用它一举完成两件事：

1. **给出当前光标所在的语法模式**（`Markup` / `Math` / `Code`），用于在分发链末尾选择 `complete_markup` / `complete_math` / `complete_code`。
2. **屏蔽注释**：注释节点（`LineComment`、`BlockComment`）会让 `mode_after` 返回 `None`，从而让 `autocomplete` 提前返回 `None`。

`mode_after` 的返回类型是 `Option<SyntaxMode>`。「光标**之后**会进入哪种模式」是它的语义——之所以叫 `after`，是因为它描述的是「在这个节点之后，我们处在什么模式」，这与「光标落在节点末尾」的补全场景天然吻合。

#### 4.3.2 核心流程

`mode_after` 的判定分两层：

1. **节点种类层**（`kind.rs` 的 `mode_after`）：每个 `SyntaxKind` 标注自己属于哪类模式来源——`Known(模式)`（确定模式）、`None`（无模式，如注释、`End`、`Shebang`）、`Text`（可能是 markup，也可能是 raw 正文）、`Parent`（继承父节点模式）等。
2. **节点实例层**（`node.rs` 的 `mode_after`）：结合**父节点种类**与**兄弟节点**把上面的抽象标注解析成具体的 `Option<SyntaxMode>`。

判定规则（节选）：

- `LineComment` / `BlockComment` → `None`（注释无模式）。
- `Text`：父为 `Raw`（raw 块正文）→ `None`；否则 → `Markup`。
- `Dollar`（`$`）在第 0 位 → `Math`。
- `Embeddable`（如 `#` 嵌入）且前驱是 `Hash` → `Code`。
- 其余 → 继承 `parent_mode()`。

#### 4.3.3 源码精读

调用点——一行代码既是「求模式」又是「注释守卫」：

[complete.rs#L54-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L54-L55) —— `let mode = ctx.leaf.mode_after()?;`。

`mode_after` 的实例层实现：

[node.rs#L1180-L1209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1180-L1209) —— `LinkedNode::mode_after`。注意第 1183–1184 行注释 "Comments and the bodies of raw text have no mode." 与 `ModeAfter::None => None`；第 1185 行 raw 正文 → `None`；第 1188 行普通文本 → `Markup`；第 1190 行起始 `$` → `Math`；第 1203–1208 行兜底继承父模式。

节点种类层的映射（注释为何返回 `None` 的根源）：

[kind.rs#L581-L586](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/kind.rs#L581-L586) —— `Self::LineComment => None`、`Self::BlockComment => None`、`Self::End => None`、`Self::Shebang => None`。这些种类在种类层就被标记为「无模式」。

`SyntaxMode` 枚举（三种模式）：

[typst-syntax/src/lib.rs#L43-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/lib.rs#L43-L50) —— `Markup` / `Math` / `Code` 三变体，正是分发链末尾 `match` 的三个分支。

> **设计要点**：`mode_after` 把「屏蔽注释」这层关切完全封装在了 typst-syntax 内部，typst-ide 这边只需一个 `?`。这是一种干净的职责分层——注释处理不需要在补全代码里到处写 `if is_comment()`。

#### 4.3.4 代码实践

**实践目标**：验证「raw 块正文内不补全」「公式内算 Math 模式」。

**操作步骤**：

1. 跟踪源码 `\` \`\`\`rust |let x = 1\`\`\` \`（光标在 raw 块的 `rust` 标记或正文里）。`leaf_at` 定位到 `Text`，其父为 `Raw`，按第 1185 行 `mode_after` 返回 `None`，`autocomplete` 返回 `None`。**这解释了为什么 raw 块内部不弹通用补全**——raw 语言名/主题的补全由 `complete_markup` 里的专门分支（第 662–676 行）在光标恰好在 raw 标签位置时处理，而非靠通用模式。
2. 跟踪 `$ a + b |$`（光标在公式内）。`Text` 父为 `Equation`，`mode_after` 经 `Space`+`Equation` 分支或普通文本分支落到 `Math`，分发链末尾走 `complete_math`。

**需要观察的现象**：raw 正文 → 无补全（`None`）；公式内 → 走数学补全分支（候选项含 `subscript`/`superscript`/`fraction` 等 snippet，见 [complete.rs#L814-L830](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L814-L830)）。

**预期结果**：与源码逻辑一致。**待本地验证**：可用 `test("$ $", -2)` 观察数学补全，用 raw 块观察返回 `None`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mode_after` 返回 `Option` 而不是直接 `SyntaxMode`？

> **答案**：因为有些位置「没有模式」——注释、`End`（文件结束哨兵）、`Shebang`、raw 块正文。用 `Option` 把这些「无模式」位置与三种合法模式区分开，让调用方用 `?` 优雅地提前退出。

**练习 2**：`# let x = 1`（`#` 后有空格再是代码）这种写法，光标在 `let` 上时 `mode_after` 会返回什么？

> **答案**：返回 `Some(SyntaxMode::Code)`。`Hash` 是 `Embeddable`，其后（即使有空格）位置仍处于 `Code` 模式；空格在代码模式下作为 trivia 不改变模式。这正是 `complete_code` 能在 `#` 后补全关键字/标识符的前提。

---

### 4.4 分派链 —— 短路优先级设计

#### 4.4.1 概念说明

补全的分派用一条 `||`（逻辑或）短路表达式实现。每个 `complete_*` 函数返回 `bool`：

- 返回 `true`：表示「这个场景归我管，我已经填好候选项了」——短路，后续分支不再尝试。
- 返回 `false`：表示「这不是我的场景，我没填东西」——继续尝试下一个分支。

由于 Rust 的 `||` 在左侧为 `true` 时短路，**第一个返回 `true` 的分支独占结果**。这条链的顺序因此就是**补全的优先级**：

```
complete_field_accesses   // expr. 或 expr.id|
  || complete_open_labels // 代码里的 <la|
  || complete_imports     // #import "..." 或 #import "...": items
  || complete_rules       // set | / show | / show x: |
  || complete_params      // func( ) / func(a:) 参数与参数值
  || match mode {         // 通用补全（按模式）
        Markup => complete_markup,
        Math    => complete_math,
        Code    => complete_code,
    }
```

设计原则是**「特定的先于通用的，便宜的先于昂贵的」**：

- 字段访问、import、规则、参数都是「结构非常明确」的场景——只要光标落在这些结构上，补全几乎是确定的，且候选项集合小而精准。
- 通用 `markup`/`math`/`code` 补全会把整个作用域、标准库、大量 snippet 都倒出来，候选项多、噪声大，只在前面都没命中时才兜底。

#### 4.4.2 核心流程

```text
对当前光标 leaf：
  field_accesses? ──yes──► 填字段/方法补全，返回
        │no
  open_labels?     ──yes──► 填标签补全，返回
        │no
  imports?         ──yes──► 填包/文件/导入项补全，返回
        │no
  rules?           ──yes──► 填 set/show 函数补全，返回
        │no
  params?          ──yes──► 填参数/参数值补全，返回
        │no
  按 mode 选 markup/math/code ──► 填通用补全
```

注意第 62 行的注释 "Only attempt the general completions after the more specific ones."——这行注释就是整条链设计意图的官方说明。

#### 4.4.3 源码精读

分发链本体：

[complete.rs#L57-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L57-L67) —— 整条 `||` 链。结果被 `let _ = ...` 丢弃——因为候选项都是副作用写进 `ctx.completions` 的，返回值只用来控制短路。

`explicit` 如何改变触发条件——这是本讲的实践重点。`explicit` 在三个分支里改变了「Anywhere（任意位置）」类分支的触发：

**① 通用模式的 Anywhere 分支**（markup / math / code 三处都有）：

[complete.rs#L678-L686](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L678-L686) —— `complete_markup` 末尾 `if ctx.explicit { ... markup_completions(ctx); return true; }`。只有用户主动触发（Ctrl+Space）时，才会在正文「任意位置」倒出全部 markup snippet（`expression`、`linebreak`、`strong text` 等）。

[complete.rs#L799-L805](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L799-L805) —— `complete_math` 的对应分支（公式内任意位置）。

[complete.rs#L855-L875](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L855-L875) —— `complete_code` 的对应分支（`{ | }`、`(|)`、`(1,|)` 等位置，且排除字典键位置）。

**② 参数补全里的「紧跟逗号」情形**：

[complete.rs#L477-L489](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L477-L489) —— 注释明确写着 `func(12,|)` 是 `[explicit mode only]`。条件 `deciding.kind() != SyntaxKind::Comma || deciding.range().end < ctx.cursor || ctx.explicit` 表示：当决定性节点是逗号时，只有「光标已离开逗号（逗号 range.end < cursor，即中间有空格）」或「explicit 触发」才补全参数。换句话说，`#rect(12,|)`（逗号后立刻是光标、无空格）默认不弹，避免每输一个参数就被打断；但 Ctrl+Space 仍可强制弹出。

测试辅助——`test()` 与 `test_implicit()` 的区别正是 `explicit` 的真假：

[complete.rs#L1601-L1614](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1601-L1614) —— `test()` 调 `test_with_doc(..., true)`，`test_implicit()` 调 `test_with_doc(..., false)`。可以用它们直接对照 explicit 的行为差异。

#### 4.4.4 代码实践

**实践目标**：亲手验证「通用补全为何排在最后」与「explicit 如何改变触发条件」。

**实践一：为什么通用 code/markup/math 补全排在分派链最后？**

阅读 [complete.rs#L57-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L57-L67)。理由可归纳为三点，请在源码中逐一找到对应证据：

1. **特定补全更精准，应优先命中**。例如 `#rect(|)` 光标在参数列表里，应补「参数」（`complete_params`），而不是把整个全局作用域倒出来（`complete_code`）。若通用补全在前，参数补全的机会就被抢走了。
2. **避免噪声**。通用 `complete_code` / `complete_markup` 会列出标准库全部函数与大量 snippet，候选项动辄上百；字段访问、import 等场景的候选项只有几个。把通用的放最后，保证「有更具体的候选时不被通用噪声淹没」。
3. **短路语义天然实现「第一个命中即止」**。`||` 短路让顺序即优先级，无需额外的优先级字段或排序。

**实践二：explicit=true 如何改变触发条件？**

利用测试辅助函数对照：

1. `complete_markup` 的 Anywhere 分支。
   - 用 `test("Hello", -1)`（explicit=true）：光标在正文 `Hello` 的文本节点上，无特殊结构命中，走 `complete_markup` 末尾的 `if ctx.explicit` 分支，应返回全部 markup snippet。可断言 `.must_include(["expression", "strong text"])`。
   - 用 `test_implicit("Hello", -1)`（explicit=false）：`complete_markup` 各结构分支都不命中，`if ctx.explicit` 为假返回 `false`，整条链无人命中，返回空列表。可断言 `.must_be_empty()` 或 `.must_exclude(["expression"])`。
2. `complete_params` 的「紧跟逗号」分支。
   - `test("#rect(12,)", -1)`（explicit=true，光标紧贴逗号后）：注释说这是 `[explicit mode only]`，应补出剩余参数。可断言 `.must_include(["fill"])`。
   - `test_implicit("#rect(12,)", -1)`（explicit=false，紧贴逗号）：条件中 `ctx.explicit` 为假、且 `deciding.range().end < ctx.cursor` 也为假（光标紧贴逗号），故不补参数，返回空。

**操作步骤**：在 `complete.rs` 的 `mod tests` 中新增一个 `#[test]`，仿照现有用例风格（[complete.rs#L1642-L1648](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1642-L1648) 是最简单的范例）写出上述断言。

**需要观察的现象**：同一光标位置，`test`（explicit）与 `test_implicit` 的候选项集合不同；explicit 在「任意空白处」和「紧跟逗号处」会额外弹出候选项。

**预期结果**：与上述分析一致。**待本地验证**：运行 `cargo test -p typst-ide test_autocomplete` 确认新增断言通过。

#### 4.4.5 小练习与答案

**练习 1**：分派链里每个 `complete_*` 返回 `bool`，但 `autocomplete` 最终返回的是 `(from, completions)`。候选项是怎么从分支传到最终返回值的？

> **答案**：通过副作用。每个分支拿到 `&mut CompletionContext`，命中时直接往 `ctx.completions` 里 `push`，并按需改写 `ctx.from`。`bool` 返回值只用于控制 `||` 短路。所以 `autocomplete` 末尾直接 `Some((ctx.from, ctx.completions))` 即可——状态都在 `ctx` 里。

**练习 2**：假如把 `complete_code` 移到 `complete_field_accesses` 之前，`#rect(12,|)`（光标在参数列表里）会发生什么？

> **答案**：`complete_code` 在 explicit 模式下（或光标在逗号后）会先命中，把整个全局作用域的函数/类型/snippet 全部倒出来，`complete_params` 永远没机会运行。用户在 `#rect(` 里输参数时会看到上百个无关候选项，而不是精准的 `fill`/`width` 等参数。这就是通用补全必须排最后的实战原因。

**练习 3**：`explicit` 在哪几处改变了 `complete_*` 的触发条件？

> **答案**：三处通用模式的 Anywhere 分支（`complete_markup` L679、`complete_math` L800、`complete_code` L858），以及 `complete_params` 的「紧跟逗号」情形（L481 的 `|| ctx.explicit`）。核心思想是：被动补全（输入时自动弹）只在「结构明确」时弹，避免频繁打扰；主动补全（Ctrl+Space）则在更多位置放开，让用户随时能主动召唤。

---

## 5. 综合实践

**任务**：绘制一张「光标位置 → 命中分支 → from 取值 → 是否需要 explicit」的追踪表，覆盖分发链上的每一环。

请对下面 6 个光标位置（`|` 表示光标）逐一追踪 `autocomplete` 的执行路径，填出表格：

| # | 源码片段 | mode_after | 命中的 complete_* 分支 | from 取值 | 是否依赖 explicit |
|---|---------|-----------|----------------------|----------|------------------|
| 1 | `#assert.|` | | | | |
| 2 | `#import "|\`"` | | | | |
| 3 | `#set |` | | | | |
| 4 | `#rect(fill:|)` | | | | |
| 5 | `#rect(12,|)` | | | | |
| 6 | 正文里 `Hello |`（光标在空白处）| | | | |

**参考答案**（做完后再对照）：

| # | mode_after | 命中分支 | from | 依赖 explicit？ |
|---|-----------|---------|------|----------------|
| 1 | Code | `complete_field_accesses`（紧跟点号） | `cursor`（点号后无输入） | 否 |
| 2 | Code | `complete_imports`（import 路径的 `Str`） | `ctx.leaf.offset()`（路径串开头） | 否 |
| 3 | Code | `complete_rules`（`set` 关键字后） | `cursor` | 否 |
| 4 | Code | `complete_params`（`:` 后的参数值） | `cursor.min(next.offset())` | 否 |
| 5 | Code | `complete_params`（紧跟逗号） | `cursor.min(next.offset())` | **是**（紧贴逗号仅 explicit 触发；`#rect(12, |)` 带空格则不需要） |
| 6 | Markup | `complete_markup` 的 Anywhere 分支 | `cursor` | **是**（正文任意位置仅 explicit 触发） |

**进阶**：把上表中你认为可复现的几行，用 `test()` / `test_implicit()` 写成断言，运行 `cargo test -p typst-ide` 验证你的追踪是否正确。

---

## 6. 本讲小结

- `autocomplete` 是补全的公共入口：定位叶子 → 构造 `CompletionContext` → 用 `mode_after()` 求模式并屏蔽注释 → 短路分发 → 返回 `(from, completions)`。
- `(from, completions)` 中 `from` 是替换起点：LSP 客户端用选中项替换源码的 `[from, cursor)` 区间；`from` 默认为 `cursor`（纯插入），各分支按需回退到已输入 token 的开头。
- `mode_after()` 一行代码同时完成两件事——给出 `Markup`/`Math`/`Code` 模式，并让注释、raw 正文等返回 `None` 使 `autocomplete` 提前退出；注释处理被干净地封装在 typst-syntax 内部。
- 分派链是一条 `||` 短路表达式，顺序即优先级：**字段访问 > 开放标签 > import > 规则 > 参数 > 通用模式**；候选项通过副作用写进 `ctx.completions`，`bool` 只控制短路。
- 通用 `markup`/`math`/`code` 补全排在最后，因为它们候选项多、噪声大，必须让位于更精准的特定补全。
- `explicit=true`（用户按 Ctrl+Space）会放开三处通用「Anywhere」分支，以及参数补全的「紧跟逗号」情形，让用户在「结构不明确」的位置也能主动召唤补全。

---

## 7. 下一步学习建议

本讲只读了补全的「门面」。接下来的讲义会逐一打开分发链上的门：

- **u5-l2 Completion 数据模型与 CompletionContext**：深入 `Completion`/`CompletionKind` 的字段、`snippet_completion`/`enrich` 等方法，理解候选项是怎么被「做出来」的。
- **u5-l3 三种语法模式的补全**：展开本讲末尾的 `complete_markup`/`complete_math`/`complete_code`，看 `@` 引用、`#` 嵌入、raw 标签、公式 snippet 等具体触发条件。
- **u6 系列（进阶）**：逐篇拆解 `complete_field_accesses`、`complete_imports`、`complete_rules`、`complete_params`、`scope_completions`、`cast_completions`、`value_completion_full` 等更复杂的分支，理解 `analyze_expr`、`named_items`、`globals`、`check_value_recursively` 如何为补全提供数据。

建议在进入 u5-l2 前，先用本讲的「综合实践」表确认你能准确判断任意光标命中的是哪个 `complete_*`——这是阅读后续各分支实现的基础。
