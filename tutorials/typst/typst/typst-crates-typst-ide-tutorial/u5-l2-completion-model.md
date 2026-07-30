# Completion 数据模型与 CompletionContext

## 1. 本讲目标

本讲紧接 u5-l1（`autocomplete` 入口与补全分发管线）。上一讲我们看清了「补全如何被触发、按什么优先级短路分发」，本讲则打开分发链里反复出现的两个东西：**一条候选项长什么样**、**那条贯穿所有分支的可变上下文是什么**。

学完后你应该能够：

- 说出 `Completion` 结构的 `kind / label / apply / detail` 四个字段各自的作用与默认值规则，并能解释 `${}` 片段占位语法。
- 识别 `CompletionKind` 的十种取值，并知道它们分别对应哪类补全来源。
- 画出 `CompletionContext` 的生命周期：它在哪里被创建、被谁修改、最终如何变成 `(from, completions)` 返回值。
- 解释 `snippet_completion`、`enrich`、`seen_casts` 这三个工具方法的作用，并能在阅读补全源码时预测它们对 `completions` 向量的影响。

## 2. 前置知识

- **补全返回值元组 `(from, completions)`**：来自 u5-l1。`from` 是 LSP 客户端做「替换」时的起始字节偏移（默认等于 `cursor`，即纯插入），`completions` 是候选列表。
- **LSP 片段语法（snippet syntax）**：Typst 的补全 `apply` 字段使用与 VS Code/LSP 一致的片段占位语法：`${}` 表示「光标最终落点」，`${名称}` 或 `${1:默认值}` 表示一个占位符（placeholder / tab stop），编辑器会让用户用 `Tab` 在占位符之间跳转。本讲会把这种语法和 Typst 自身的 `#`、`$` 语法区分清楚。
- **`EcoString`**：typst 自研的低开销字符串类型，`Complete.rs` 里大量用 `.into()` 从 `&str` 构造它。
- **`FxHashSet`**：Rust 标准库 `HashSet` 的快速哈希替代（来自 `rustc-hash` / typst 内部），`CompletionContext` 用它做去重。

## 3. 本讲源码地图

本讲几乎全部落在同一个文件里：

| 文件 | 作用 |
| --- | --- |
| `src/complete.rs` | 补全引擎的全部实现，包括本讲的 `Completion`、`CompletionKind`、`CompletionContext` 以及各种构造辅助函数。 |

`complete.rs` 是本项目最大的源文件（两千余行），所以我们会聚焦其中「数据模型」这一薄层，而不是逐行读完所有补全分支。各补全分支（字段、参数、import 等）的内部逻辑留待 u6 系列展开。

## 4. 核心概念与源码讲解

### 4.1 Completion —— 一个补全候选项的四个字段

#### 4.1.1 概念说明

补全过程可以产生很多条候选，比如在 `#` 之后会同时出现 `int`、`if conditional`、`emphasized text` 等等。每一条候选就是一个 `Completion`。它的设计目标是：**用同一个结构体，既描述「这条候选显示成什么样」，又描述「选中它之后要把源码改成什么样」**。

`Completion` 只有四个字段，故意保持精简：

- `kind`：这条候选的「类别标签」，供编辑器画图标、分组。
- `label`：候选列表里显示的文本。
- `apply`：选中后真正写入源码的文本（可选）。如果不提供，编辑器就用 `label` 原样插入。
- `detail`：一行简短说明（可选），通常显示在候选下方。

#### 4.1.2 核心流程

`Completion` 是一个纯数据结构，本身没有「流程」。但理解它的关键在于把握 **`apply` 与 `label` 的分离**：

1. 编辑器先把所有 `Completion` 按 `label` 排列成候选菜单。
2. 用户选中某条后，编辑器读取它的 `apply`：
   - `apply = None` → 把 `label` 原文插入到 `from..cursor` 区间。
   - `apply = Some(s)` → 把 `s` 插入到 `from..cursor` 区间，并解析其中的 `${}` 片段占位符。

也就是说，`label` 负责「看」，`apply` 负责「做」。两者可以不同——例如 `label` 是 `"rgb()"`，`apply` 是 `"rgb(${r}, ${g}, ${b}, ${a})"`。

#### 4.1.3 源码精读

结构定义位于文件开头，紧跟在 `autocomplete` 入口之后：

[complete.rs:72-86 —— Completion 结构定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L72-L86) 这段定义了四个字段，注意 `apply` 和 `detail` 都是 `Option<EcoString>`，且 `apply` 的文档注释明确写了「Should default to the `label` if `None`」（为空时编辑器回退到 label）。

四个字段中，`apply` 的注释特别点出了片段语法 `${lhs} + ${rhs}`：

```rust
pub struct Completion {
    pub kind: CompletionKind,
    pub label: EcoString,
    /// Should default to the `label` if `None`.
    pub apply: Option<EcoString>,
    pub detail: Option<EcoString>,
}
```

之所以 `apply`/`detail` 是 `Option`，是为了让「单纯插入 label、无说明」的简单候选（占大多数）能省去多余字段——构造时直接 `apply: None, detail: None` 即可。

`Completion` 派生了 `Serialize`/`Deserialize`（[complete.rs:73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L73)），意味着它可以直接被语言服务器序列化成 LSP 的 JSON 返回给编辑器。`label/apply/detail` 用 `EcoString` 而非 `String`，是因为补全候选大量来自静态字符串和短文本，`EcoString` 在这种场景下分配更省。

#### 4.1.4 代码实践

**实践目标**：建立对四个字段的直觉——同样的「值」可以生成不同形态的 `Completion`。

**操作步骤**：

1. 阅读 [complete.rs:1272-1333 —— value_completion_full](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1272-L1333)，这是构造「值型」候选项的总入口。观察它如何根据 `value` 类型分别决定 `apply` 与 `detail`：
   - 函数值且 `parens` 为真、且光标后不是 `(` 或 `[` 时，`apply` 会带上括号片段（见 L1296-L1312），例如 `rgb(${r}, ${g}, ${b}, ${a})`。
   - `detail` 在没有显式传入时，会回退：函数/类型取 `find_value_docs` 的摘要，其它类型取 `value.repr()`（仅当 `repr` 与 `label` 不同时才填，见 L1289-L1292）。
2. 对照 [complete.rs:1252-1259 —— value_completion / call_completion](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1252-L1259)：二者只是 `parens` 参数不同（`false` vs `true`）。

**需要观察的现象**：

- 对一个 `Func` 值，`call_completion`（`parens=true`）产出的 `apply` 会带括号和 `${}` 占位符；而 `value_completion`（`parens=false`）产出的 `apply` 通常为 `None`（直接插 label）。
- 对一个普通 `Value::Int`，`detail` 只在 `repr != label` 时才有值——因为 label 本身就是 `repr`，再写一遍是冗余。

**预期结果**：能口头复述「`apply` 是否带括号、`detail` 是否有值」分别由哪几行条件决定。无法在本地运行时，标注「待本地验证」并用 `cargo test` 跑 `complete.rs` 末尾的断言来核对（见 4.5 节的测试助手）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个候选的 `label = "heading"`、`apply = None`、`detail = None`，编辑器选中它后会插入什么文本？

**答案**：因为 `apply` 为 `None` 会回退到 `label`，所以插入 `"heading"`。

**练习 2**：为什么 `apply` 的注释里写成 `${lhs} + ${rhs}` 而不是直接写 `lhs + rhs`？

**答案**：`${lhs}` 是 LSP 片段占位符，表示一个可被 `Tab` 跳转、可被整体选中的输入位；而裸 `lhs` 会被当成普通字符原样插入，用户得不到「光标停在占位符上」的体验。

---

### 4.2 CompletionKind —— 候选项的分类标签

#### 4.2.1 概念说明

`kind` 字段告诉编辑器「这条候选是什么东西」，从而决定它在候选菜单里显示什么图标（函数、类型、文件、颜色……）、是否需要特殊渲染。它是一个枚举，取值覆盖了 Typst 补全能产出的所有类别。

#### 4.2.2 核心流程

`CompletionKind` 的取值与补全来源一一对应。下表把每个变体、它的语义、以及典型产生它的补全分支对齐：

| 变体 | 含义 | 典型来源分支 |
| --- | --- | --- |
| `Syntax` | 语法片段（snippet） | `snippet_completion` 一律产出它 |
| `Func` | 函数 | `scope_completions` 中的函数值 |
| `Type` | 类型 | `scope_completions` 中的类型值 |
| `Param` | 函数参数 | `param_completions` |
| `Constant` | 常量值 | 普通值的 `value_completion` 兜底 |
| `Path` | 文件路径 | `file_completions` |
| `Package` | 包名 | `package_completions` |
| `Label` | 标签 | `label_completions` |
| `Font` | 字体家族 | `font_completions` |
| `Symbol(EcoString)` | 符号（携带该符号的字形串） | 字段访问里的 `symbol.modifiers` |

其中只有 `Symbol` 是「带数据的变体」（携带符号的实际文本，比如 `≠`），其余九个都是单元变体。

#### 4.2.3 源码精读

枚举定义在 `Completion` 紧下方：

[complete.rs:88-112 —— CompletionKind 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L88-L112) 注意它派生了 `PartialEq`（用于比较）和 `#[serde(rename_all = "kebab-case")]`——序列化时变体名会被改成小写连字符形式，比如 `Constant` → `"constant"`、`Symbol` → `"symbol"`。这是为了让 LSP JSON 输出符合编辑器期望的命名风格。

`value_completion_full` 末尾有一个「按值推断 kind」的兜底逻辑，能看出各类别的归属：

```rust
kind: kind.unwrap_or_else(|| match value {
    Value::Func(_) => CompletionKind::Func,
    Value::Type(_) => CompletionKind::Type,
    Value::Symbol(s) => CompletionKind::Symbol(s.get().into()),
    _ => CompletionKind::Constant,
}),
```

见 [complete.rs:1323-1328](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1323-L1328)。当调用方没有显式指定 `kind` 时，就用值的类型自动归类：函数→`Func`，类型→`Type`，符号→`Symbol(实际字形)`，其余统统→`Constant`。这就是为什么「内部含颜色的模块/字典」补全出来时 `kind` 仍是 `Constant` 而非某种「颜色」——颜色是值，不是独立的 kind。

#### 4.2.4 代码实践

**实践目标**：验证 `kind` 是如何被「显式指定」或「按值兜底」决定的。

**操作步骤**：

1. 读 [complete.rs:1148-1153 —— package_completions](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1146-L1153)，它调用 `str_completion(..., Some(CompletionKind::Package), ...)`——这里**显式**指定了 `Package`。
2. 读 [complete.rs:1252-1254 —— value_completion](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1252-L1254)，它把 `kind` 参数传成 `None`，于是落到上面那段 `unwrap_or_else` 兜底。

**需要观察的现象**：同一份「字符串值」补全，在包名场景被标记 `Package`，在普通常量场景被标记 `Constant`——`kind` 取决于「调用方知道多少」，而不是值本身。

**预期结果**：能解释为何 `str_completion` 需要一个 `kind: Option<CompletionKind>` 形参——因为它既服务包名（`Package`）、字体（`Font`）、路径（`Path`），也服务普通字符串字面量（`None`→兜底 `Constant`）。

#### 4.2.5 小练习与答案

**练习 1**：在 `#table.cell(` 这样的参数补全里，候选 `columns:` 的 `kind` 是什么？依据是哪一行？

**答案**：是 `Param`。依据是 [complete.rs:551-556](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L551-L556)，参数候选被显式写成 `kind: CompletionKind::Param`。

**练习 2**：序列化后，`CompletionKind::Symbol` 会让 `kind` 字段变成什么 JSON？

**答案**：由于 `#[serde(rename_all = "kebab-case")]`，变体名变成 `"symbol"`；但 `Symbol(EcoString)` 是带数据的变体，所以序列化结果是 `{"symbol": "≠"}`（外层键是 kebab-case 的变体名，值是携带的字形串）。本项「待本地验证」，可用 `serde_json::to_string` 对一个 `CompletionKind::Symbol("≠".into())` 实测确认。

---

### 4.3 CompletionContext —— 贯穿补全全流程的可变共享上下文

#### 4.3.1 概念说明

回顾 u5-l1：`autocomplete` 的分发链里有十几个 `complete_*` 函数，它们都接收同一个 `&mut CompletionContext`。这个上下文扮演两个角色：

1. **只读输入**：光标位置、源码文本、`world`、上一次编译产物 `output`、是否显式触发 `explicit`。
2. **可变输出收集器**：所有分支把候选写入它的 `completions` 字段；分支还能修改 `from`（替换起点）。

把它想象成一块「共享黑板」：分发链的每个分支都看同一块黑板、往上面贴候选，先贴的（命中）就让短路终止。

#### 4.3.2 核心流程

`CompletionContext` 的生命周期是：

```
autocomplete 入口
   │  new(...) 在这里一次性构造
   ▼
CompletionContext { world, output, text, before, after, leaf, cursor,
                    explicit, from=cursor, completions=[], seen_casts={} }
   │
   │  分发链 complete_field_accesses / complete_params / ... 各取所需：
   │    - 读 ctx.leaf / ctx.before / ctx.after / ctx.world 做判断
   │    - 调 ctx.snippet_completion(...) / ctx.value_completion(...) 往 ctx.completions 推
   │    - 命中时设 ctx.from 并 return true（短路）
   ▼
返回 (ctx.from, ctx.completions)
```

注意 `from` 的初值就是 `cursor`（[complete.rs:1085](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1085)），对应「纯插入」；只有当某个分支需要替换已输入的若干字符时，才会把 `from` 往前移（例如字段访问 `emoji.fa|` 会把 `from` 设成 `fa` 的起点，见 u6-l2）。

#### 4.3.3 源码精读

结构体定义：

[complete.rs:1050-1063 —— CompletionContext 结构](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1050-L1063) 逐字段含义：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `world` | `&'a dyn IdeWorld` | 数据来源（u1-l2），各分支取 library/book/packages/files |
| `output` | `Option<&'a dyn Output>` | 上次编译产物，仅标签补全等少数分支用 |
| `text` | `&'a str` | 整段源码文本 |
| `before` | `&'a str` | 光标**之前**的文本切片 |
| `after` | `&'a str` | 光标**之后**的文本切片 |
| `leaf` | `&'a LinkedNode<'a>` | 光标所在叶子节点（u2-l1） |
| `cursor` | `usize` | 光标字节偏移 |
| `explicit` | `bool` | 是否显式触发（放开更多通用补全，u5-l1） |
| `from` | `usize` | 替换起点，初值 = cursor |
| `completions` | `Vec<Completion>` | 候选输出收集器 |
| `seen_casts` | `FxHashSet<u128>` | cast 去重集合（见 4.5） |

构造函数 `new` 把 `text` 切成 `before`/`after` 两个切片，并初始化 `from = cursor`、`completions = vec![]`、`seen_casts = FxHashSet::default()`：

[complete.rs:1065-1089 —— CompletionContext::new](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1065-L1089)。注意它**不**接收 `text/before/after`——这些是从 `source.text()` 和 `cursor` 派生出来的，避免调用方传不一致的值。

辅助方法 `before_window` 提供一个「光标前 size 个字符」的小窗口，专门用来做廉价的字符串判定：

[complete.rs:1091-1094 —— before_window](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1091-L1094)。它用 `Scanner` 取 `cursor-size..cursor` 切片，并用 `cursor.saturating_sub(size)` 防止下溢。它的一个真实用法在标签补全里——判断光标前 15 个字符是否含 `"cite"`，从而区分「参考文献键」与「普通文档标签」：

```rust
let citation = !at && self.before_window(15).contains("cite");
```

见 [complete.rs:1224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1224)。这里用窗口而不是 `self.before.contains("cite")` 是出于性能——后者每次都对整段「光标前文本」做子串搜索。

> 设计要点：`before_window` 借助 `Scanner` 的字节切片，而 `Scanner::get` 会校验字符边界。源码里专门有一个测试 `test_autocomplete_before_window_char_boundary`（[complete.rs:1764-1767](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1764-L1767)）用 `😀`（多字节 emoji）开头来验证窗口不会切到半个字符上——这是 Rust 字符串切片最容易踩的坑。

#### 4.3.4 代码实践

**实践目标**：理解 `before`/`after`/`cursor` 三者的关系，以及它们如何被分支利用。

**操作步骤**：

1. 读 [complete.rs:1296-1320](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1296-L1320)，观察 `value_completion_full` 如何用 `self.after.starts_with(['(', '['])` 决定是否在 `apply` 里补括号——如果用户已经手敲了 `(`，就不再重复加。
2. 读 [complete.rs:1315-1320](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1315-L1320)，观察字符串补全的「去尾引号」技巧：当 `label` 以 `"` 开头且 `self.after` 以 `"` 开头时，`apply` 去掉末尾的 `"`，避免插入后出现 `""path""`。

**需要观察的现象**：`ctx.after` 不仅是「光标后文本」，更是一个**省略符判断器**——它让补全适应「用户已经写了什么」，避免重复符号。

**预期结果**：能解释为什么在已有 `"` 紧跟光标时，字体/路径补全的 `apply` 不带尾引号。该行为可用第 5 节综合实践的测试方法核对。

#### 4.3.5 小练习与答案

**练习 1**：`from` 的初值为什么是 `cursor` 而不是 `0`？

**答案**：因为默认语义是「在光标处插入新文本」，替换区间是空的（`cursor..cursor`），所以起点就是终点 `cursor`。只有需要替换已输入字符的分支才把 `from` 前移。

**练习 2**：`new` 为什么不接收 `before`/`after` 参数，而要在内部计算？

**答案**：因为 `before`/`after` 必须始终与 `text` 和 `cursor` 一致（分别是 `&text[..cursor]` 和 `&text[cursor..]`）。如果让调用方分别传，就可能出现三者不一致的 bug；在构造函数里统一派生能保证这个不变式。

---

### 4.4 snippet_completion —— 语法片段的快捷构造

#### 4.4.1 概念说明

大量补全候选是「固定的语法片段」——比如 markup 模式下的 `*${strong}*`（加粗）、`#code listing`（代码块）、`- ${item}`（列表项）。这些片段的 `kind` 永远是 `Syntax`，`apply` 永远带 `${}` 占位符。为了避免每次都手写四字段的 `Completion { ... }`，typst-ide 提供了 `snippet_completion` 这个三参数快捷构造器。

#### 4.4.2 核心流程

调用形式是 `ctx.snippet_completion(label, snippet, docs)`，它固定产出：

```
Completion {
    kind:   CompletionKind::Syntax,   // 永远
    label:  label,                     // 显示文本
    apply:  Some(snippet),             // 含 ${} 占位符
    detail: Some(docs),                // 一句说明
}
```

三个参数都是 `&'static str`（编译期字符串字面量），因为所有片段都是硬编码的静态内容，不可能来自运行时数据。

#### 4.4.3 源码精读

方法定义：

[complete.rs:1104-1117 —— snippet_completion](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1104-L1117) 短小精悍——三个 `&'static str`，构造一个 `Syntax` 类型的候选直接 `push` 进 `ctx.completions`。

它的大量真实调用集中在 `markup_completions`：

[complete.rs:688-786 —— markup_completions](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L688-L786) 这里逐条列出 markup 模式下的所有片段。挑几个看占位符的不同用法：

- `"expression"` → `"#${}"`：单个最终光标位（用户输入表达式）。
- `"strong text"` → `"*${strong}*"`：命名占位符 `strong`。
- `"code listing"` → `` "```${lang}\n${code}\n```" ``：两个占位符，用户 `Tab` 在 `lang` 与 `code` 间跳转。
- `"enumeration item (numbered)"` → `"${number}. ${item}"`：两个命名占位符 + 字面量 `. `。
- `"hyperlink"` → `"https://${example.com}"`：占位符夹在字面量中间。

这些片段的 `${}` 语法不是 Typst 的，而是 LSP/编辑器层面的 snippet 语法，编辑器选中后会把 `${example.com}` 变成一个可编辑的占位区。`cast_completions` 也大量复用它来产出类型常量补全，例如 `none`/`auto`/`true`/`false` 以及各种颜色构造器：

[complete.rs:1355-1409 —— cast_completions 里的 snippet_completion](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1355-L1409) 比如对 `Color` 类型，连续产出 `luma(${v})`、`rgb(${r}, ${g}, ${b}, ${a})`、`cmyk(...)`、`oklab(...)` 等候选。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标**：手写一个 `snippet_completion("test", "${a} + ${b}", "desc")` 调用，**预测**它生成的 `Completion` 结构，并解释 `apply` 的占位符语法。

**操作步骤**：

1. 假设在某个 `complete_*` 分支里写了：
   ```rust
   ctx.snippet_completion("test", "${a} + ${b}", "desc");
   ```
2. 对照 [complete.rs:1111-1116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1111-L1116) 逐字段推导。

**预测结果（这就是答案）**：

```rust
Completion {
    kind:   CompletionKind::Syntax,
    label:  "test".into(),
    apply:  Some("${a} + ${b}".into()),
    detail: Some("desc".into()),
}
```

**`apply` 占位符语法解释**：

- `${a}` 与 `${b}` 是两个**命名占位符**。编辑器选中该候选后，会把 `apply` 文本 `${a} + ${b}` 插入到 `from..cursor` 区间，并把 `${a}`、`${b}` 变成两处可编辑区域。
- 用户按 `Tab` 在 `a`→`b` 之间跳转； `${}`（空）若出现则表示「最后一个光标停留位」。本例没有 `${}`，所以最后一次 `Tab` 后光标停在补全文本末尾。
- 命名占位符（`${a}`）与编号占位符（`${1}`）效果类似，区别仅在于命名更可读。typst-ide 几乎只用命名形式。
- 注意 `apply` 里的 `$` 与 Typst 自身的数学定界符 `$...$` **无关**——前者是 LSP snippet 语法，后者是 Typst 语言语法。这也是为什么 `markup_completions` 里数学片段写成 `"$${x}$"`：外层 `$$`...`$` 是 Typst 数学定界符，内层 `${x}` 是 snippet 占位符。

**需要观察的现象**：编辑器候选菜单里会显示 `test`，说明是 `desc`；选中后源码出现 `a + b` 形态且 `a`、`b` 高亮可编辑。

**预期结果**：上述结构预测与源码逻辑完全一致。如要实测，可仿照第 5 节用 `test(...).at("test").must_apply_as("${a} + ${b}").must_have_detail("desc")` 写一条断言（这两个测试助手见 [complete.rs:1586-1597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1586-L1597)）。本地未运行时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`snippet_completion` 的三个参数为什么是 `&'static str` 而不是 `EcoString`？

**答案**：因为所有片段都是硬编码的静态字面量，用 `&'static str` 既省去运行时分配，又能在源码里直观书写；函数内部再用 `.into()` 转成 `EcoString` 存入 `Completion`。

**练习 2**：若想让一个片段选中后只把光标停在末尾、不要任何占位符，`apply` 该写什么？

**答案**：写一个不含 `${...}` 的普通字符串即可（光标自然停在末尾），或在需要显式标记末尾光标位时加一个 `${}`。typst-ide 里 `none`/`auto` 这类无参常量片段的 `apply` 就是裸字符串 `"none"`（见 [complete.rs:1349-1351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1349-L1351)）。

---

### 4.5 enrich 与 seen_casts —— 批量后处理与去重

#### 4.5.1 概念说明

补全分支常常需要**先批量生成一批候选，再统一加工**。两个典型场景：

1. **加前后缀**：比如 `#show heading: [...]` 里，先按作用域补出所有「元素函数」候选（`heading`、`text`、`list`……），然后给它们**统一加 `: ` 后缀**，因为 show 选择器语法是 `函数: 内容`。这就是 `enrich` 干的事。
2. **去重**：类型驱动补全（`cast_completions`）会递归展开 `CastInfo`，同一个类型可能在多条路径下被展开多次，从而产出重复候选。`seen_casts` 用一个哈希集合记录「已产出过的 cast」，避免重复。

#### 4.5.2 核心流程

**`enrich(prefix, suffix)`**：

```
对 ctx.completions 里每一条已有的候选：
    current = apply.unwrap_or(label)   // apply 为空则用 label
    apply = prefix + current + suffix  // 套上前后缀，写回 apply
```

注意它**只改 `apply`**，不动 `label`——所以候选菜单显示的仍是原名，但选中后插入的文本被包了前后缀。

**`seen_casts`**：

```
cast_completions(cast) 开始时：
    h = hash128(cast)
    若 h 已在 seen_casts 中 → 直接返回（跳过，不重复产出）
    否则插入 h，继续展开 cast
```

#### 4.5.3 源码精读

`enrich` 定义：

[complete.rs:1096-1102 —— enrich](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1096-L1102)。关键一行是 `let current = apply.as_ref().unwrap_or(label);`——用 `apply` 或回退到 `label`，再用 `eco_format!` 包前后缀。

它有三个真实调用点，体现两种用法：

- [complete.rs:384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L384)：在 show 选择器补全里 `ctx.enrich("", ": ")`——空前缀、`": "` 后缀。于是候选 `heading`（`apply=None`）被加工成 `apply = Some("heading: ")`。
- [complete.rs:561](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L561) 与 [complete.rs:582](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L582)：在参数补全里，当光标前是逗号时 `ctx.enrich(" ", "")`——加一个前导空格。这样 `#rect(a, |)` 处补出的参数会变成 ` width`（带前导空格），符合 Typst 的书写习惯。

`seen_casts` 字段与使用：

[complete.rs:1062](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1062) 声明 `seen_casts: FxHashSet<u128>`；在 `cast_completions` 开头用它去重：

[complete.rs:1336-1340 —— cast_completions 的去重](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1336-L1340)。`hash128(cast)` 用 128 位哈希给每个 `CastInfo` 算指纹，`FxHashSet::insert` 返回 `false`（已存在）时直接 `return`，从而保证同一类型只展开一次。

> 为什么用 128 位哈希而不是把 `CastInfo` 直接存进集合？因为 `CastInfo` 不一定实现 `Hash`/`Eq`，而 `typst::utils::hash128` 能对任意可哈希对象产出 `u128`，存储与比较都更轻量。代价是理论上有极小概率哈希碰撞，但 128 位空间下可忽略——这是 best-effort 风格的典型取舍。

`seen_casts` 在 `new` 里初始化为空集（[complete.rs:1087](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1087)），且只被 `cast_completions` 消费——它和 `enrich` 一样是「数据模型之上的薄层工具」，但服务于不同分支。

#### 4.5.4 代码实践

**实践目标**：用现成的测试助手验证 `enrich` 加工后的 `apply`，并理解 `seen_casts` 的去重时机。

**操作步骤**：

1. 阅读 [complete.rs:1581-1597 —— CompletionExt 测试助手](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1581-L1597)，其中 `must_apply_as` 断言某条候选的 `apply`、`must_have_detail` 断言 `detail`。
2. 阅读测试主入口 [complete.rs:1601-1606 —— test](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1601-L1606)：它编译 world、再调 `autocomplete`，返回 `Response = Option<(usize, Vec<Completion>)>`（[complete.rs:1515](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1515)）。
3. 想象一条断言：`test("#show |", -1).at("heading").must_apply_as("heading: ")`，验证 enrich 给选择器候选加了 `: ` 后缀。

**需要观察的现象**：

- `at("heading")` 能在候选里按 `label` 定位到那条候选（见 [complete.rs:1573-1578](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1573-L1578) 的 `at` 实现）。
- `must_apply_as("heading: ")` 断言其 `apply` 确实被 `enrich("", ": ")` 加过工。

**预期结果**：在 show 选择器上下文，元素函数候选的 `apply` 都带 `: ` 后缀。该断言「待本地验证」，可用 `cargo test -p typst-ide test_autocomplete` 跑现有用例（如 `test_autocomplete_hash_expr` [complete.rs:1642-1648](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1642-L1648)）确认测试框架可运行。

#### 4.5.5 小练习与答案

**练习 1**：`enrich` 为什么改 `apply` 而不改 `label`？

**答案**：因为候选菜单要显示「干净的名字」（如 `heading`）方便用户辨认，而真正写入源码时才需要带语法后缀（`heading: `）。`label` 管「看」，`apply` 管「做」，二者解耦。

**练习 2**：如果删掉 `cast_completions` 开头的 `seen_casts` 去重（[complete.rs:1338-1340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1338-L1340)），会出现什么现象？

**答案**：`CastInfo` 递归展开时同一类型（如 `bool` 或某个 union 成员）会被多次产出，导致候选列表里出现重复项（例如多个 `true`/`false`）。`seen_casts` 正是用来消除这类由递归结构引起的重复。

---

## 5. 综合实践

把本讲的四个数据模型要素串起来，完成下面这个「补全数据流追踪」任务。

**任务背景**：考虑 Typst 源码 `#show |`（光标在 `#show ` 之后），用户触发补全。

**要求**：

1. **定位分支**：根据 u5-l1 的分发链，判断这条补全由哪个分支处理（提示：它属于 `complete_rules` 下的 `show_rule_selector_completions`）。
2. **预测候选项结构**：参照 [complete.rs:377-396](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L377-L396)，写出候选 `heading` 的完整 `Completion` 结构（四个字段都要预测），特别注意 `enrich("", ": ")`（[complete.rs:384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L384)）对 `apply` 的影响、以及 `scope_completions` 用的 `value_completion`（`parens=false`）对 `kind` 与 `detail` 的影响。
3. **写一条断言**：仿照 [complete.rs:1586-1597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1586-L1597) 的助手风格，写一条形如 `test("#show |", -1).at("heading").must_apply_as(???)` 的断言，把 `???` 填成你预测的 `apply`。
4. **验证**：如果本地可运行，把断言加进 `complete.rs` 的 `#[cfg(test)]` 模块，用 `cargo test -p typst-ide` 运行；不能运行则标注「待本地验证」，并说明你预测的依据是哪几行源码。

**参考答案要点**：

- 分支：`show_rule_selector_completions` → 先 `scope_completions`（只挑元素函数），再 `enrich("", ": ")`，最后追加两个 selector snippet。
- `heading` 候选：`kind = Func`（由 `value_completion_full` 的 `Value::Func(_)` 兜底得出，因为 scope 补全没显式给 kind）；`label = "heading"`；`apply = Some("heading: ")`（经 enrich 加 `: ` 后缀，注意 enrich 回退用的是 label）；`detail` 来自 `find_value_docs(world, &heading_func).summary()`（见 [complete.rs:1286-1288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1286-L1288)）。
- 断言：`.must_apply_as("heading: ")`。

> 注意一个易错点：`scope_completions` 走的是 `value_completion`（`parens=false`），所以函数候选**不会**自动带括号；带 `: ` 完全是由后续的 `enrich` 负责。这正体现了「数据模型分层」的价值——同一个 `Completion` 结构能被不同分支用不同方式填充。

## 6. 本讲小结

- `Completion` 用 `kind / label / apply / detail` 四字段统一描述一条候选：`label` 负责显示，`apply`（默认回退 `label`）负责选中后写入，`apply` 用 LSP 片段语法 `${}` 表达占位符。
- `CompletionKind` 有十种取值，覆盖 Syntax/Func/Type/Param/Constant/Path/Package/Label/Font/Symbol；`value_completion_full` 在调用方未指定时按值兜底归类（函数→Func、类型→Type、符号→Symbol、其余→Constant）。
- `CompletionContext` 是贯穿分发链的可变共享上下文：`new` 一次性构造，`before/after` 从 `text`+`cursor` 派生，`from` 初值为 `cursor`，分支把候选写入 `completions` 并按需前移 `from`，最终返回 `(from, completions)`。
- `before_window(size)` 提供光标前的廉价字符窗口，用 `Scanner` 保证字符边界安全，专供 `contains` 这类快速判定。
- `snippet_completion` 是固定产出 `Syntax` 类候选的三参数快捷构造器；`enrich(prefix,suffix)` 对已有候选批量套前后缀（只改 `apply`、回退 label）；`seen_casts` 用 `hash128` 给 `CastInfo` 去重，防递归产出重复。

## 7. 下一步学习建议

本讲把「补全的数据模型」讲透了，但还没展开「补全的内容来源」。下一步建议：

- **u5-l3 三种语法模式的补全**：进入 `complete_markup / complete_math / complete_code`，看 markup 模式如何识别 `@`、`#`、raw 标签等触发条件、如何调本讲的 `snippet_completion` 产出结构化片段。
- **u6 系列（补全进阶）**：尤其是 **u6-l4 scope_completions 与类型驱动补全**（深入 `scope_completions`、`cast_completions`，并解释 `seen_casts` 与 `check_value_recursively` 如何让「含颜色的模块/字典」也出现在颜色补全里）和 **u6-l5 apply 片段与 BracketMode**（展开本讲一笔带过的 `value_completion_full` 中括号模式 `RoundAfter/RoundWithin/RoundNewline/SquareWithin` 的判定逻辑）。
- 配套阅读：`src/complete.rs` 末尾的 `#[cfg(test)]` 模块（[complete.rs:1515 起](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1515)），它既是测试，也是理解 `Completion` 各字段真实取值的最佳样例库。
