# tooltip 总入口与分发策略

## 1. 本讲目标

当你在编辑器里把鼠标悬停到 Typst 源码的某个位置时，IDE 会弹出一个「悬停提示（hover tooltip）」：可能是参数说明、字体摘要、标签详情、也可能是某个表达式的求值结果。本讲只回答一个核心问题——**这些形形色色的提示，是怎么从同一个入口函数 `tooltip` 里被「分发」出来的？**

学完本讲你应当能够：

- 说清 `tooltip` 这个公共入口的完整执行流程：定位叶子 → 跳过 trivia → 短路分发。
- 解释六个分发分支（`named_param` / `font` / `label` / `import` / `expr` / `closure`）**为什么是这个顺序**，尤其是「为什么 `named_param_tooltip` 排在 `expr_tooltip` 前面」。
- 区分 `Tooltip::Text` 与 `Tooltip::Code` 两类返回值的语义与适用场景。
- 理解为什么光标落在空白/注释（trivia）上时直接返回 `None`。
- 能用 `Side::Before` 与 `Side::After` 构造出「同一光标、不同提示」的场景并解释原因。

本讲**只讲分发骨架**，不深入每个分支的内部实现——参数/字体/标签/闭包等具体 tooltip 的细节留到 u3-l3、u3-l4。

## 2. 前置知识

本讲建立在前几讲之上，以下概念默认你已经掌握（若生疏请先回顾对应讲义）：

- **u2-l1（光标到语法树节点）**：`Source::root()` 取无类型语法树根，`LinkedNode::new(root).leaf_at(cursor, side)` 把字节偏移映射到叶子节点。`Side` 的精确语义见 [node.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1329-L1373)：
  - `Side::Before` 调用私有 `leaf_before`，它把光标区间看成**右闭**——当光标恰好在两个 token 的交界点（= 前一个 token 的终点 = 后一个 token 的起点）时，选中**终点在该处的那个 token（前一个）**。
  - `Side::After` 调用私有 `leaf_after`，它把光标区间看成**左闭**——交界点处选中**起点在该处的那个 token（后一个）**。
  - 当光标严格落在某个 token 内部时，两者选中的是同一个 token。**差异只出现在交界点上。**
- **u2-l1（trivia）**：`leaf_at` **不会**主动跳过 trivia（空白、注释等无语义节点）；是否过滤是调用方的策略。本讲的 `tooltip` 正是显式过滤的那一类。
- **u2-l4（analyze）**：`analyze_expr` 通过 trace **重新求值整篇文档**来捕获某个 span 的运行时值，代价较贵；字面量则直接构造。这一点直接决定了分发顺序的成本考量。
- **u3-l1（docs）**：`find_value_docs` / `find_param_docs` 从原生文档常量或源码 `///` 注释中提取文档，并以 `Docs::summary()` 给出「第一段第一句」纯文本。

> 一个直观比喻：`tooltip` 就像医院分诊台。病人（光标）先被快速分流（是不是空白？是不是参数名？是不是字体？……），由最对口、最便宜的科室先接诊；只有都不匹配时，才送到最贵、最通用的「全科」（表达式分析）。

## 3. 本讲源码地图

本讲几乎只依赖一个文件，外加 `lib.rs` 里的一行再导出。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/tooltip.rs` | 悬停提示的全部实现：公共入口、分发链、六个分支、`Tooltip` 枚举、测试 | `tooltip` 入口（L24–L42）、`Tooltip` 枚举（L45–L51）、六个分支函数 |
| `src/lib.rs` | 模块声明与公共 API 再导出 | `pub use self::tooltip::{Tooltip, tooltip};`（L17） |

六个分支函数在 `tooltip.rs` 中的位置一览（后续讲义会逐个精读，本讲只看它们的「分工」与「出场顺序」）：

| 分支函数 | 行号 | 触发场景（简述） | 返回类型倾向 |
| --- | --- | --- | --- |
| `named_param_tooltip` | [L206–L249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L206-L249) | 光标在函数调用/set 规则的具名参数名或枚举型字符串值上 | Text |
| `font_tooltip` | [L263–L284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L263-L284) | 光标在 `font: "..."` 的字符串值上 | Text |
| `label_tooltip` | [L187–L203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L187-L203) | 光标在 `<label>` 或 `@ref` 上（需编译产物） | Text |
| `import_tooltip` | [L124–L140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L124-L140) | 光标在 `import "x": *` 的 `*` 上 | Text |
| `expr_tooltip` | [L53–L122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L53-L122) | 光标在可分析的表达式上（最通用、最贵） | Code 为主 |
| `closure_tooltip` | [L143–L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L143-L171) | 光标在闭包的 `=` 或 `=>` 上 | Text |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① `tooltip` 总入口（定位叶子 + 跳过 trivia）；② `Tooltip` 类型（Text / Code）；③ 短路分发链（`or_else` 的顺序与设计）。

### 4.1 tooltip 总入口：定位叶子并跳过 trivia

#### 4.1.1 概念说明

`tooltip` 是 typst-ide 对外暴露的悬停提示公共入口（见 [lib.rs:L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L17) 的再导出）。语言服务器只需把「world、上次编译产物、源码、光标字节偏移、`Side`」喂给它，就能拿到一个 `Option<Tooltip>`。

它的前两步是所有 IDE 功能共享的「定址」动作，但在本讲里有一个 `tooltip` 特有的策略决定：**显式跳过 trivia**。

回顾 u2-l1：`leaf_at` 不会跳过 trivia，是否过滤交由调用方决定。`tooltip` 选择了「过滤」——因为悬停在空白或注释上时，用户并不期望看到任何提示，贸然返回内容反而会造成误导（比如把空白归到相邻表达式上）。所以这里直接 `return None`。

#### 4.1.2 核心流程

```
tooltip(world, output, source, cursor, side)
  │
  ├─ 1. leaf = LinkedNode::new(source.root()).leaf_at(cursor, side)?
  │        └─ 定位到光标所在的叶子节点；光标越界则整体返回 None
  │
  ├─ 2. if leaf.kind().is_trivia() { return None; }
  │        └─ 空白 / 注释等无语义节点：直接放弃，不进入分发
  │
  └─ 3. 进入短路分发链（见 4.3）
```

注意第 1 步的 `?`：如果 `leaf_at` 返回 `None`（例如光标超出源码范围），整个 `tooltip` 直接返回 `None`，连分发都不进入。

#### 4.1.3 源码精读

入口与 trivia 跳过见 [tooltip.rs:L24-L42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L24-L42)：

```rust
pub fn tooltip(
    world: &dyn IdeWorld,
    output: Option<impl AsOutput>,
    source: &Source,
    cursor: usize,
    side: Side,
) -> Option<Tooltip> {
    let leaf = LinkedNode::new(source.root()).leaf_at(cursor, side)?;
    if leaf.kind().is_trivia() {
        return None;
    }

    named_param_tooltip(world, &leaf)
        .or_else(|| font_tooltip(world, &leaf))
        // ... 其余分支见 4.3
}
```

这段代码做了两件事：

- `LinkedNode::new(source.root()).leaf_at(cursor, side)?`：套上 `LinkedNode`（带偏移与拓扑信息），下钻到光标叶子（u2-l1 标准写法）。`?` 处理「找不到叶子」。
- `if leaf.kind().is_trivia() { return None; }`：用 `SyntaxKind::is_trivia()` 判定空白/注释/Hash 等无语义节点，命中即放弃。

`output: Option<impl AsOutput>` 是「上次编译产物」。它的 doc 注释（[tooltip.rs:L19-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L19-L23)）说得很直白：传入是可选的，但能增强提示——例如标签提示只有在文档可用时才会生成。后面会看到，`label_tooltip` 分支里用 `output?` 把「无产物」直接变成该分支返回 `None`。

#### 4.1.4 代码实践

**实践目标**：直观感受「光标落在 trivia 上时，`tooltip` 返回 `None`」。

**操作步骤**：在 `tooltip.rs` 的测试模块里已有现成的负向断言。打开 [tooltip.rs:L337-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L337-L341) 的 `test_tooltip`：

```rust
test("#let x = 1 + 2", -1, Side::After).must_be_none();      // 光标在末尾空白外
test("#let x = 1 + 2", 5, Side::After).must_be_code("3");    // 光标在 x 上
test("#let x = 1 + 2", 6, Side::Before).must_be_code("3");
```

- 运行：`cargo test -p typst-ide --lib tooltip::tests::test_tooltip`。
- 自己加一行，把光标放到 `#let x = 1 + 2` 中 `x` 后面的空格（字节偏移 6）上、用 `Side::After`：

```rust
test("#let x = 1 + 2", 6, Side::After).must_be_none();
```

**需要观察的现象**：新加的这行应当通过（`must_be_none`）。

**预期结果**：`Side::After` 在偏移 6 选中了那个空格（trivia），于是第 2 步 `is_trivia()` 命中、直接返回 `None`；而同样偏移 6 用 `Side::Before` 选中的是空格前的 `x`（非 trivia），进入 `expr_tooltip` 得到 `Code("3")`。（这正是 4.3.4 综合实践要深挖的「同光标不同 Side」现象。）

> 本实践基于项目已有的测试风格，命令未经实际运行，请在本地 `cargo test` 验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `if leaf.kind().is_trivia() { return None; }` 这两行删掉，悬停在 `#box(fill: red,)` 的逗号 `,`（偏移 18）上会发生什么？参考 [tooltip.rs:L386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L386)。

**答案**：不会改变结果。逗号 `,` 的 `SyntaxKind` 是 `Comma`，并不是 trivia（trivia 指空白、行注释、块注释、Hash 等）。所以删掉这两行后，逗号仍会进入分发链，最终各分支都不匹配而返回 `None`——测试里 `,` 处确实断言为 `must_be_code(red_box)` 而非 None，是因为它进入了 `expr_tooltip`（逗号所在的祖先 `FuncCall` 是可分析表达式）。trivia 跳过只影响真正的空白/注释节点。

**练习 2**：为什么 trivia 过滤放在「分发之前」而不是放进每个分支函数里各自判断？

**答案**：集中过滤有两点好处——(1) 避免在六个分支里重复写同样的 `is_trivia()` 判定；(2) 提前 `return None` 省掉整条分发链的执行，对悬停这种高频操作是实打实的性能收益（尤其能跳过最贵的 `expr_tooltip`）。

### 4.2 Tooltip 类型：Text 与 Code 的语义区分

#### 4.2.1 概念说明

分发链上的分支最终都要产出同一个返回类型 `Tooltip`。它只有两个变体：

- `Tooltip::Text(EcoString)`：**人类可读的散文/文档**。例如参数说明、字体摘要、标签详情、星号导入项列表、闭包捕获列表、值的文档摘要。
- `Tooltip::Code(EcoString)`：**一段 Typst 代码**，通常代表某个表达式「求值出来的值」。例如 `"3"`、`"rgb(\"#ff4136\")"`、`"box(fill: ...)"`，以及长度换算 `"12pt = 4.23mm = ..."`。

区分两者的意义在于：**让 LSP 客户端能用不同样式渲染**。Text 渲染成普通文本/Markdown，Code 渲染成等宽代码块。这是把「说明性内容」与「值表示」在类型层面区分开的简洁设计。

#### 4.2.2 核心流程

```
分支函数判断出「这是什么」
   │
   ├─ 是说明/文档/列表   → Tooltip::Text(prose)
   └─ 是值的代码表示     → Tooltip::Code(code)
```

一个经验法则：**凡是从 `docs.rs` 的 `summary()`、或 `summarize_font_family`、或拼出来的自然语言句子来的，都是 Text；凡是从 `Value::repr()` 或单位换算拼出来的代码字符串，都是 Code。**

#### 4.2.3 源码精读

枚举定义见 [tooltip.rs:L44-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L44-L51)：

```rust
/// A hover tooltip.
#[derive(Debug, Clone, PartialEq)]
pub enum Tooltip {
    /// A string of text.
    Text(EcoString),
    /// A string of Typst code.
    Code(EcoString),
}
```

两个变体的来源，可以从代码里直接验证：

- **Text 的典型来源**：`expr_tooltip` 里 `find_value_docs(world, value)` 命中时返回 `Tooltip::Text(docs.summary())`（[tooltip.rs:L79-L81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L79-L81)）；`font_tooltip` 返回 `Tooltip::Text(detail)`（[tooltip.rs:L280](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L280)）；`closure_tooltip` 返回 `Tooltip::Text(eco_format!("This closure captures {tooltip}"))`（[tooltip.rs:L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L170)）。

- **Code 的典型来源**：`expr_tooltip` 末尾把多个候选值用 `repr::pretty_comma_list` 拼成代码，返回 `Tooltip::Code(tooltip.into())`（[tooltip.rs:L120-L121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L120-L121)）；`length_tooltip` 把 pt 换算成 mm/cm/in 拼成代码（[tooltip.rs:L174-L184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L174-L184)）。

`#[derive(PartialEq)]` 不是偶然——测试里正是用它做断言（见 4.1.4 的 `must_be_code("3")`，本质是 `assert_eq!(result, Some(Tooltip::Code("3".into())))`）。

#### 4.2.4 代码实践

**实践目标**：从测试断言反推每个光标位置返回的是 Text 还是 Code。

**操作步骤**：阅读 [tooltip.rs:L374-L388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L374-L388) 的 `test_tooltip_set`。该测试对 `#set box(fill: red,)` 的多个位置分别断言。

**需要观察的现象**：把每个光标位置、断言方法、你推断的 `Tooltip` 变体填进下表：

| 光标偏移 | 字符 | 断言 | 变体 | 来自哪个分支 |
| --- | --- | --- | --- | --- |
| 5 | `b`(box) | `must_be_text` | Text | expr（值是函数，命中 `find_value_docs`） |
| 9 | `f`(fill) | `must_be_text` | Text | named_param |
| 15 | `r`(red) | `must_be_code` | Code | expr（值的 repr） |

**预期结果**：你会发现「文档/说明」统一是 Text、「值的代码表示」统一是 Code，与 4.2.2 的经验法则一致。

> 本实践为源码阅读型，无需运行，对照测试即可。

#### 4.2.5 小练习与答案

**练习 1**：悬停在 `#let x = 1 + 2` 的 `x` 上得到 `Code("3")`。为什么是 Code 而不是 Text？

**答案**：`expr_tooltip` 用 `analyze_expr` 拿到值 `Value::Int(3)`，对它调用 `value.repr()` 得到代码字符串 `"3"`，所以是 `Tooltip::Code`。只有当值能命中 `find_value_docs`（即原生函数/类型带文档）时才会改走 Text 分支；整数 `3` 没有文档，故走 Code。

**练习 2**：`length_tooltip` 把长度换算成多种单位后返回 `Code`。如果改成返回 `Text`，会有什么问题？

**答案**：语义上，`"12pt = 4.23mm = ..."` 是一串「代码/数值表示」而非自然语言说明，编辑器理应用等宽字体对齐数字与单位。标成 Text 会被当成散文渲染，丢失对齐效果，与用户的直觉（这是一个值的多种表示）不符。

### 4.3 短路分发链：六个分支与 or_else 的执行顺序

#### 4.3.1 概念说明

`tooltip` 的核心是一条用 `Option::or_else` 串起来的**短路分发链**。每个分支函数签名都是 `fn(&..., leaf: &LinkedNode) -> Option<Tooltip>`：返回 `Some` 表示「我认领了这个光标」，整条链立刻短路返回；返回 `None` 表示「不归我管」，交给下一个分支。

链的顺序是：

```
named_param → font → label → import → expr → closure
```

设计原则可以概括为：**从最具体/最便宜，到最通用/最昂贵**。`named_param`、`font`、`label`、`import` 都只认「结构非常精确」的光标位置（具名参数、`font:` 字符串、标签、星号导入），且靠查表/遍历就能回答；而 `expr` 是兜底的「全科」，要靠 `analyze_expr` **重新求值整篇文档**（见 u2-l4），代价最高。把便宜的精确分支放前面，能在大多数悬停场景里避免触发昂贵的全文档求值。

#### 4.3.2 核心流程

```
named_param_tooltip(world, &leaf)      ── Some? ──► 返回（短路）
        │ None
        ▼
font_tooltip(world, &leaf)             ── Some? ──► 返回（短路）
        │ None
        ▼
label_tooltip(output?, &leaf)          ── Some? ──► 返回（短路；无 output 则此分支恒 None）
        │ None
        ▼
import_tooltip(world, &leaf)           ── Some? ──► 返回（短路）
        │ None
        ▼
expr_tooltip(world, &leaf)             ── Some? ──► 返回（短路；最贵）
        │ None
        ▼
closure_tooltip(&leaf)                 ── Some? ──► 返回（短路）
        │ None
        ▼
                                    整体返回 None
```

关键点：`or_else` 接受一个**惰性闭包** `|| ...`，只有前一个返回 `None` 时才会调用下一个——所以未命中的分支不会被执行，这就是「短路」。

#### 4.3.3 源码精读

分发链见 [tooltip.rs:L36-L41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L36-L41)：

```rust
named_param_tooltip(world, &leaf)
    .or_else(|| font_tooltip(world, &leaf))
    .or_else(|| label_tooltip(output?, &leaf))
    .or_else(|| import_tooltip(world, &leaf))
    .or_else(|| expr_tooltip(world, &leaf))
    .or_else(|| closure_tooltip(&leaf))
```

这里有两处细节值得点出：

1. **`label_tooltip(output?, &leaf)` 里的 `output?`**：`output` 是 `Option<impl AsOutput>`，被移进闭包。闭包内的 `?` 作用于这个 `Option`——若 `output` 为 `None`，则该闭包直接返回 `None`（仅此分支，不影响整条链）。这正好实现「没有编译产物时，标签提示整个分支作废」的优雅降级，呼应入口 doc 注释（[tooltip.rs:L19-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L19-L23)）。

2. **顺序的「成本梯度」**：`named_param_tooltip` 内部只做 `func.param(...)` + `find_param_docs`（查元数据表，便宜）；而 `expr_tooltip` 内部第一件事就是 `analyze_expr(world, ancestor)`（[tooltip.rs:L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L74)），按 u2-l4 的分析，它会 trace 整篇文档。把 `expr` 放在倒数第二，意味着前面任何一个精确分支命中，都能省掉这次全文档求值。

各分支的「认领条件」速览（本讲只到「条件」层面，内部实现见 u3-l3/u3-l4）：

| 分支 | 认领条件（节选） |
| --- | --- |
| `named_param_tooltip` | leaf 在 `Named`（具名参数）里、且祖先是 `Args`/`MathArgs` 下的函数调用或 set 规则；命中参数名或枚举型字符串值 |
| `font_tooltip` | leaf 是 `Str`、父节点 `Named` 的名字恰为 `"font"` |
| `label_tooltip` | leaf 是 `RefMarker`（`@ref`）或 `Label`（`<lab>`），且 `output` 可用 |
| `import_tooltip` | leaf 是 `Star`（`*`）、父节点是 `ModuleImport` |
| `expr_tooltip` | 向上找到首个 `ast::Expr` 祖先，且该表达式「可分析」（可哈希，或是数学变量访问） |
| `closure_tooltip` | leaf 恰好是 `Eq` 或 `Arrow`、父节点是 `Closure` |

#### 4.3.4 代码实践

**实践一：解释为什么 `named_param_tooltip` 排在 `expr_tooltip` 前面。**

理由有三层，按重要性排列：

1. **成本（最实在）**：`named_param_tooltip` 是查表（`func.param()` + `find_param_docs`），非常便宜；`expr_tooltip` 要 `analyze_expr`——按 u2-l4，它会用 trace **重新求值整篇文档**。把便宜且精确的参数检查放前面，悬停参数名时就能秒出文档、完全跳过昂贵的求值。

2. **具体性（精度优先）**：`named_param_tooltip` 识别的是结构上极其精确的情形——「光标正落在一个已知函数的具名参数上」。`expr_tooltip` 则是兜底的通用分析。当多种解释都可能时，应当优先采纳更精确的那种解释。

3. **设计意图（参数名遮蔽）**：设想 `#let width = 10pt` 之后写 `#rect(width: 20pt)`，把光标悬停在参数名 `width` 上。用户的心智模型是「我在看 rect 的 width 参数」，期望看到参数文档，**而不是**同名变量 `width` 的值 `10pt`。把参数检查放在表达式分析之前，就从结构上保证了「参数名优先被当作参数」——即便当前实现里参数名 span 通常不会被 trace 捕获（故 `expr` 多半返回 `None`），这个顺序仍是对该期望的稳健保证，也防御了未来的实现变动。

> 验证建议：对照 [tooltip.rs:L418-L424](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L418-L424) 的 `test_tooltip_function_call`——悬停 `#box(fill:red,)` 的 `fill` 得到 `must_be_text(fill_desc)`（参数文档），而悬停值 `red` 得到 `must_be_code("rgb(\"#ff4136\")")`（表达式值）。两者走的就是不同分支。

**实践二：构造「同一光标、`Side::Before` 与 `Side::After` 得到不同 tooltip」的场景。**

利用 u2-l1 的边界语义：**当光标恰好在两个 token 的交界点时，`Before` 选中终点在此的前一个 token，`After` 选中起点在此的后一个 token。** 项目里已有现成测试 [tooltip.rs:L509-L515](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L509-L515)：

```rust
#[test]
fn test_tooltip_star_import() {
    let world = TestWorld::new("#import \"other.typ\": *")
        .with_source("other.typ", "#let (a, b, c) = (1, 2, 3)");
    test(&world, -2, Side::Before).must_be_none();
    test(&world, -2, Side::After).must_be_text("This star imports `a`, `b`, and `c`");
}
```

逐步拆解（测试里负数光标 `-2` 的解析规则见 [tests.rs:L238-L244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L238-L244)：`-2` → `len + (-2) + 1 = len - 1`，即最后一个字符 `*` 的偏移）：

- 源码 `"#import \"other.typ\": *"` 字节布局：`:` 在 `[19,20)`、空格在 `[20,21)`、`*` 在 `[21,22)`，长度 22。`-2` 解析为偏移 21，正是 `*` 的起点 = 空格的终点（**交界点**）。
- `Side::Before` → 选中**终点在 21** 的 token = 空格（trivia）→ 第 4.1 步 `is_trivia()` 命中 → 返回 `None`。
- `Side::After` → 选中**起点在 21** 的 token = `*`（Star）→ 进入 `import_tooltip` → 返回 `Text("This star imports ...")`。

**需要观察的现象 / 预期结果**：同一光标偏移 21，`Before` 得 `None`、`After` 得星号导入提示。运行 `cargo test -p typst-ide --lib tooltip::tests::test_tooltip_star_import` 应当全绿。

> 命令未经本地运行，请用 `cargo test` 验证。

#### 4.3.5 小练习与答案

**练习 1**：若把分发链里 `font_tooltip` 和 `expr_tooltip` 的顺序对调（expr 在前、font 在后），悬停 `#text(font: "Arial")` 的 `"Arial"` 时结果会变吗？

**答案**：不会变。`"Arial"` 是字符串字面量，`expr_tooltip` 对字面量的处理是：先看能否命中 `find_value_docs`/长度换算（字符串都不命中），再因 `expr.is_literal()` 为真而 `return None`（[tooltip.rs:L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L90-L92)）。所以即便 expr 在前也返回 `None`，最终仍落到 `font_tooltip` 给出字体摘要。但对调顺序会让每次悬停字体名都白跑一次全文档求值，是纯粹的性能损失——这正说明「便宜分支靠前」的意义主要在效率。

**练习 2**：为什么 `closure_tooltip` 放在链的**最后**，甚至排在 `expr_tooltip` 之后？

**答案**：`closure_tooltip` 的认领条件极窄——只有 leaf 恰好是 `Eq` 或 `Arrow`、且父节点是 `Closure` 时才命中（[tooltip.rs:L146-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L146-L154)）。`=`/`=>` 这些 token 几乎不会被前面的分支认领（它们不是参数名、不是 Str、不是标签、不是 Star、也未必能 cast 成可分析的 `ast::Expr`），所以放最后不会与前面的分支抢光标，同时也符合「越通用的兜底越靠后」的层次。注释里还点明了它**故意只在 `=`/`=>` 上触发**、不在整个闭包子树上显示，以免太吵（[tooltip.rs:L144-L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L144-L145)）。

**练习 3**：`label_tooltip` 分支里的 `output?` 如果改成直接传 `output`（假设改了签名），会带来什么行为变化？

**答案**：`output?` 的作用是「没有编译产物时，让本分支立刻返回 `None`」。若去掉它、强行要求产物，则调用方在没缓存上次编译结果时（例如刚打开文件还没编译完），整个 `tooltip` 调用会变得不可用或需要改类型签名。当前的 `output?` 设计实现了「**有产物则增强、无产物则降级**」——标签提示缺失，但参数/字体/表达式等其他提示照常工作。

## 5. 综合实践

把本讲的三条主线（定址 + 类型 + 分发）串起来，完成下面这个「分发追踪」小任务。

**场景**：源码为

```typst
#set box(fill: red)
```

（即 `test_tooltip_set` 用的同款片段，但去掉尾逗号，长度 20，字节布局：`#`=0,`set`=1–4,空格=5,`box`=6–9,`(`=10,`fill`=11–15,`:`=16,空格=17,`red`=18–21……以本地实际字节为准，下文用相对位置描述。）

**任务**：

1. 对光标落在 `box`、`fill`、`red` 三个位置，分别写出：命中了哪个分支？返回 `Text` 还是 `Code`？内容是什么？（参考 [tooltip.rs:L379-L388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L379-L388)）
2. 解释为什么悬停 `box` 走的是 `expr_tooltip` 而不是 `named_param_tooltip`——尽管 `box` 看起来也在括号附近。
3. 把片段改成 `#let box = "x"; #set box(fill: red)`，悬停 set 规则里的 `box`。预测：现在 `expr_tooltip` 会返回什么？`named_param_tooltip` 会抢先返回吗？为什么？

**参考答案**：

1. `box` → `expr_tooltip`（`box` 是被调用函数名，`analyze_expr` 得到 `Value::Func(box)`，命中 `find_value_docs` → `Text("An inline-level container that sizes content.")`）；`fill` → `named_param_tooltip`（具名参数名 → `Text("The box's background color.")`）；`red` → `expr_tooltip`（值 `rgb(...)` 的 repr → `Code("rgb(\"#ff4136\")")`）。
2. 因为 `named_param_tooltip` 只认「具名参数名/值」即 `Named` 节点的成分，而 `box` 是 set 规则的**目标函数名**（`SetRule.target`），不是任何 `Named` 的子节点，结构上不满足 `named_param` 的认领条件，所以它返回 `None`、落到 `expr`。
3. 改后 set 规则里的 `box` 仍指向被 `let` 重新绑定后的值 `"x"`（字符串）。`named_param_tooltip` 仍不认领（结构不变）；`expr_tooltip` 会 `analyze_expr` 得到 `Value::Str("x")`，因是字面量、又无文档 → 返回 `None`。即悬停 `box` 将从原来的「函数文档」变成「无提示」——这正是 u2-l4「trace 能拿到 set/绑定之后真实运行时值」在悬停场景的体现。

> 第 3 题的运行时行为建议本地 `cargo test` 验证（可仿照 `test_tooltip_set` 新增一个用例）。

## 6. 本讲小结

- `tooltip` 公共入口的前两步是：用 `LinkedNode::new(source.root()).leaf_at(cursor, side)?` 定位叶子，再 `if leaf.kind().is_trivia() { return None; }` 显式跳过空白/注释。
- 六个分支用 `Option::or_else` 串成**短路分发链**：`named_param → font → label → import → expr → closure`，首个 `Some` 即返回。
- 顺序遵循「**最具体/最便宜在前，最通用/最昂贵在后**」：前四个靠查表/遍历，`expr` 要 `analyze_expr` 重新求值整篇文档（u2-l4），故排在倒数第二。
- `label_tooltip(output?, &leaf)` 里的 `output?` 实现「**有编译产物则增强、无则降级**」——缺产物时仅标签分支作废，其余照常。
- `Tooltip` 只有 `Text`（散文/文档/列表）与 `Code`（值的代码表示/单位换算）两类，让 LSP 客户端能分别渲染。
- `Side::Before` 选中**终点在光标处**的 token、`Side::After` 选中**起点在光标处**的 token；二者只在 token 交界点上不同（如星号导入测试：同一偏移 `Before` 得 trivia→None、`After` 得 `*`→提示）。

## 7. 下一步学习建议

本讲只讲了分发「骨架」，每个分支的内部实现还没展开。建议接下来：

- **u3-l3（表达式与函数调用的 tooltip）**：精读 `expr_tooltip`——它如何用 `analyze_expr` 取候选值、合并重复值并标 `×N`、对字面量与长度做特殊处理，以及 `Sink::MAX_VALUES` 的截断。
- **u3-l4（特殊场景 tooltip）**：精读 `font_tooltip` / `label_tooltip` / `import_tooltip` / `closure_tooltip` 四个分支的内部——字体摘要、标签详情、星号导入列表、闭包捕获是如何拼出来的。
- 阅读建议：把本讲的分发链与 u3-l1 的 `docs`（`find_value_docs`/`find_param_docs`）对照看，理解「分发命中后，文档从哪来」。
- 进阶思考：试着为某个当前返回 `None` 的光标位置新增一个分支（例如悬停 `#include "x.typ"` 的路径），体会「在 `or_else` 链里插入新分支」的扩展方式与顺序权衡。
