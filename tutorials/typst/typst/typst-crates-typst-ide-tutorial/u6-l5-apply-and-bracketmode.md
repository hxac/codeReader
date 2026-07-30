# 补全内容生成：apply 片段与 BracketMode

## 1. 本讲目标

上一篇（u6-l4）讲完了「**补什么**」——`scope_completions` 用作用域收集候选，`cast_completions` 用 `CastInfo` 按类型展开候选。本讲解决另一个正交的问题：**选中的候选确定之后，往源码里写入什么文本？**

一条 `Completion` 有四个字段（`kind`/`label`/`apply`/`detail`），其中 `label` 管「**看**」（补全菜单里显示什么），`apply` 管「**做**」（回车后真正写入编辑器的文本）。绝大多数情况下 `apply` 直接等于 `label`，但在补全函数、字符串、特殊键名时，`apply` 会变成带占位符的 **LSP 片段（snippet）**，例如 `list(${})`、`strong[${}]`、`figure(\n  ${}\n)`。

本讲读完，你应当能够：

- 说清 `value_completion_full` 这一个**统一出口**如何同时产出 `label`/`apply`/`detail`，以及它如何按「值类型 + 调用现场」做决策。
- 掌握三个便捷入口 `value_completion` / `call_completion` / `str_completion` 的分工（谁带括号、谁带 detail）。
- 彻底理解 `BracketMode` 四种模式（`RoundWithin` / `RoundAfter` / `RoundNewline` / `SquareWithin`）及其判断依据，能对任意 Typst 内建函数预测它补成什么样子。
- 理解 `detail` 在没有显式文档时的回退生成策略（`repr`、`find_value_docs`）。

## 2. 前置知识

本讲默认你已经掌握以下前置概念（来自前面讲义）：

- **`Completion` 数据模型**（u5-l2）：`kind`/`label`/`apply`/`detail` 四字段；`apply` 采用 LSP 片段语法 `${占位符}`，与 Typst 数学语法 `$...$` **无关**。
- **`CompletionContext`**（u5-l1、u5-l2）：补全全流程共享的可变上下文，本讲反复用到其中的 `after`（光标之后的文本切片）、`leaf`（光标所在的语法树叶子节点）、`world`、`completions`（输出缓冲区）。
- **`leaf.mode_after()`**（u5-l1）：返回光标所在位置的语法模式（`Some(Markup)`/`Some(Math)`/`Some(Code)`），注释与 raw 正文返回 `None`。
- **`find_value_docs`**（u3-l1）：从原生函数或源码注释中提取文档摘要，返回 `Option<Docs>`，可 `.summary()` 得到首句。
- **`value.repr()`**：把一个 `Value` 渲染成 Typst 源码字符串（如 `rgb(…)`、`"hello"`）。
- **Typst 的两种「内容括号」**：函数可以用圆括号 `func(...)` 传参数，也可以用方括号 `func[内容]` 直接传一个 content。例如 `emph[重点]` 比 `emph(重点)` 更地道。

**两个易混点先打预防针**：

1. `${}` 是 LSP 占位符，编辑器收到后会把它变成一个可 Tab 跳转的光标位。源码里写成 `eco_format!("{label}(${{}}")`，其中 `{{`/`}}` 是 `eco_format!` 对字面 `{`/`}` 的转义，最终输出字符串就是 `list(${})`。
2. 「要不要加括号」由 `parens` 形参控制；「加哪种括号」由 `BracketMode` 控制。两者是独立的两层决策。

## 3. 本讲源码地图

本讲全部聚焦在 **`src/complete.rs`** 这一个文件里，分两块区域：

| 位置（行号） | 内容 | 作用 |
| --- | --- | --- |
| `complete.rs:1252-1270` | 三个便捷入口 `value_completion`/`call_completion`/`str_completion` | 把不同语义的调用收敛成对同一个函数的调用 |
| `complete.rs:1273-1333` | `value_completion_full` | **唯一的候选项生成出口**，产出 `label`/`apply`/`detail` 并 push 进 `completions` |
| `complete.rs:1466-1495` | `enum BracketMode` + `impl BracketMode::of` | 决定函数补全的括号形态 |
| `complete.rs:1812-1825` | 测试 `test_autocomplete_bracket_mode` | 用断言固化了各种括号模式，是本讲最好的实践素材 |
| `complete.rs:73-86` | `Completion` 结构体定义 | 回顾四字段含义 |

> 永久链接 base 为 `https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/`。下文每个代码点都会给出完整链接。

## 4. 核心概念与源码讲解

### 4.1 value_completion_full —— 统一的候选项生成器

#### 4.1.1 概念说明

补全引擎里有几十处地方要「往候选列表里塞一条」：补一个模块成员、补一个字段、补一个颜色常量、补一个原生函数……这些场景的语义各不相同（有的要括号、有的不要；有的自带文档、有的没有；有的 label 是固定字符串、有的来自值本身）。

如果让每个调用方都自己拼 `Completion { kind, label, apply, detail }`，会产生大量重复且容易写错的样板代码。`value_completion_full` 就是把这些决策**收口到一个函数**里：调用方只需提供「值的指针 + 少量开关」，它负责算出最合理的 `label`/`apply`/`detail`/`kind` 四元组。

它的设计哲学是 **best-effort + 默认兜底**：能从值推断的就推断，调用方没指定的字段就按值类型给一个合理默认。

#### 4.1.2 核心流程

`value_completion_full` 接收 5 个参数，其中 4 个是可选的「提示」：

```
value_completion_full(label: Option<EcoString>,   // 可选的固定显示名
                      value: &Value,               // 唯一必填：值本身
                      parens: bool,                // 是否给函数补括号
                      kind: Option<CompletionKind>,// 可选的类别覆盖
                      detail: Option<&str>)        // 可选的一行说明
```

它依次做四件事，每一步都体现「调用方优先，缺了再兜底」：

```
① 算 label
   └─ 有显式 label 用显式；没有就用 value.repr()
   └─ 顺带算 at = 「label 存在且不是合法标识符？」
       （非标识符键名后面要套 at("...")）

② 算 detail  ── 显式 detail 优先
   ├─ Symbol          → None（字形无需文字说明）
   ├─ Func / Type     → find_value_docs(...).summary()  【回退到文档】
   └─ 其它            → value.repr()，但仅当 repr != label 才显示
                       （避免 label 与 detail 雷同）

③ 算 apply   ── 三选一，互斥
   ├─ parens 且是函数 且 光标后还不是 '(' 或 '['
   │     → 用 BracketMode 决定  ()/[ ]/换行  的具体形态
   ├─ at 为真（非标识符键名）
   │     → at("label")
   ├─ label 是带引号字符串 且 光标后已是 '"'
   │     → 去掉 label 末尾的引号（防重复引号）
   └─ 否则
         → None（回退为 label 本身）

④ 算 kind    ── 显式 kind 优先，否则按值类型：
                   Func→Func, Type→Type, Symbol→Symbol(字形), 其余→Constant

最终 push 一条 Completion { kind, label, apply, detail }
```

注意第③步的优先级顺序非常关键：**括号 > at() > 字符串去引号 > None**。这意味着只要满足「补函数括号」的条件，就不会走字符串去引号分支（逻辑上也不会冲突，因为函数和字符串是不同值类型）。

#### 4.1.3 源码精读

先看函数签名与 label / detail 的计算（`src/complete.rs:1273-1293`）：

[complete.rs:1273-1293](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1273-L1293) —— 统一入口：先定 `label`（缺省用 `value.repr()`），再按值类型回退生成 `detail`。

要点对照：

- `let at = label.as_deref().is_some_and(|field| !is_ident(field));`（L1281）：`is_ident` 来自 `typst::syntax`（见 `complete.rs:15` 的 import）。当传入的 label 不是合法标识符（比如字典键 `"my-key"`）时 `at` 为真，后面会用 `at("...")` 语法来访问。
- detail 回退里 `(repr.as_str() != label).then_some(repr)`（L1291）：这是细节但很重要——如果值的 `repr()` 和要显示的 label 一模一样（比如 label 就是 `"hello"` 而值也是 `Str("hello")`），就不显示 detail，避免菜单里两列内容雷同。

接着是本讲最核心的 `apply` 计算（`src/complete.rs:1295-1320`）：

[complete.rs:1295-1320](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1295-L1320) —— 三分支互斥地决定 `apply`：函数括号、`at()`、字符串去引号。

逐条解读：

- 条件 `parens && matches!(value, Value::Func(_)) && !self.after.starts_with(['(', '['])`（L1296-L1298）：**只有这三个条件同时成立才补括号**。第三个条件尤其巧妙——如果用户已经在光标后敲了 `(` 或 `[`（如 `#emph(|)`），就**不再画蛇添足**地补括号，让 `apply` 退回 `None`（即只用 label）。
- 数学模式特判（L1301-L1305）：`if self.leaf.mode_after() == Some(SyntaxMode::Math)` 时一律用 `BracketMode::RoundWithin`，因为方括号 content 块在数学模式里不合法，必须用圆括号。
- 四种括号形态对应四种 `eco_format!`（L1306-L1311），下文 4.3 详解。
- `else if at`（L1313-L1314）：非标识符键名，套成 `at("label")`，这是 Typst 用 `.at("key")` 访问特殊键名的写法。
- `else if label.starts_with('"') && self.after.starts_with('"')`（L1315-L1319）：补字符串常量（如字体名、包名）时，若光标后已有引号，去掉 label 末尾的引号以防 `""DejaVu""`。注意这里用 `strip_suffix('"')`，只有确实以引号结尾才处理。

最后是 push（`src/complete.rs:1322-1332`）：

[complete.rs:1322-1332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1322-L1332) —— 推入一条 `Completion`，`kind` 在调用方未指定时按值类型兜底。

注意 `apply` 直接用 `Option<EcoString>` 存进结构体——当它是 `None` 时，调用方（LSP）会回退用 `label`，这正是 `Completion::apply` 字段注释里写的「Should default to the `label` if `None`」（见 `complete.rs:79-83`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `value_completion_full` 对 detail 的「repr 雷同则省略」回退逻辑。

**操作步骤**（源码阅读 + 局部推理型）：

1. 阅读 [complete.rs:1289-1292](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1289-L1292)，确认 detail 对 `Func`/`Type` 走 `find_value_docs`、对其余走「repr 且不等于 label」。
2. 假设以下两个候选都通过 `value_completion(label, value)` 进入（即 `parens=false`, `kind=None`, `detail=None`）：
   - 场景 A：`label = "count"`, `value = Value::Int(3)`
   - 场景 B：`label = "3"`, `value = Value::Int(3)`（label 恰好等于 repr）
3. 手动推演两条候选最终 push 出来的 `detail` 字段。

**需要观察的现象**：

- 场景 A：`repr = "3"`，`label = "count"`，二者不等 → `detail = Some("3")`。
- 场景 B：`repr = "3"`，`label = "3"`，二者相等 → `detail = None`。

**预期结果**：菜单里「count」那一行旁边会显示 `3` 的说明；而「3」那一行不会显示冗余的 `3`。这正是 `(repr != label).then_some(repr)` 的设计意图——避免 label 与 detail 完全重复造成视觉噪声。

> 待本地验证：可在 `tests` 模块里用 `.at("count").must_have_detail("3")` 形式补一条断言跑 `cargo test`。

#### 4.1.5 小练习与答案

**练习 1**：当 `value_completion_full` 收到 `label = None` 时，最终的 `label` 字段由什么决定？`at` 又会是真还是假？

> **答案**：`label` 由 `value.repr()` 决定（见 L1282 的 `unwrap_or_else`）。因为 `label.as_deref()` 是 `None`，`is_some_and(...)` 直接返回 `false`，所以 `at` 恒为假。

**练习 2**：为什么 `Symbol` 类型在 detail 回退里被单独写成返回 `None`，而不是走「repr 不等于 label」的通用分支？

> **答案**：符号的「值」就是一个字形字符（如 `→`），它的 `repr()` 通常就等于字形本身，且符号的语义已由字形与 label（通常是变体名）表达，再显示一行 repr 价值不大反而嘈杂。单独短路为 `None` 是有意的降噪。

**练习 3**：条件 `!self.after.starts_with(['(', '['])`（L1298）去掉后会怎样？

> **答案**：用户已敲 `#emph(` 时，光标后 `after` 以 `(` 开头，本应退回不加括号；去掉该守卫后会再补一遍 `emph[…]` 或 `emph(…)`，产生 `#emph(emph[…])` 之类的重复。测试 `test("#()", 1).at("list").must_apply_as(None)`（`complete.rs:1822`）正是固化这一守卫的。

### 4.2 三个便捷入口：value_completion / call_completion / str_completion

#### 4.2.1 概念说明

`value_completion_full` 有 5 个参数，但绝大多数调用方只用其中两三个，其余想用默认值。为了避免到处写一长串 `None`，`CompletionContext` 提供了三个**便捷入口**，它们各自把一组常见默认值「钉死」，只暴露真正变化的参数。这是一次典型的**参数收敛**重构。

三者分工非常清晰：

| 入口 | 用于补什么 | `parens` | `detail` 形参 | label 来源 |
| --- | --- | --- | --- | --- |
| `value_completion` | 普通值（字段、常量、作用域变量） | `false` | `None`（交给兜底） | 调用方给 |
| `call_completion` | 可调用的方法/函数 | `true` | `None`（交给兜底） | 调用方给 |
| `str_completion` | 字符串字面量（字体名、包名、路径） | `false` | 调用方给 | **由字符串值生成** |

#### 4.2.2 核心流程

```
value_completion(label, value)
   └─► value_completion_full(Some(label), value, false, None, None)

call_completion(label, value)
   └─► value_completion_full(Some(label), value, true,  None, None)
                                       ^^^^
                                  唯一区别：parens=true → 函数会带括号

str_completion(string, kind, detail)
   └─► value_completion_full(None, &Value::Str(string), false, kind, detail)
                                ^^^^                    ^^^^^^^^^
                          label 由值生成          kind/detail 由调用方指定
```

注意 `call_completion` 与 `value_completion` **只差一个 `parens` 布尔**：这正是「方法/函数要不要带括号」的总开关。而 `str_completion` 走的是完全不同的路线——它不传 label，让 `value_completion_full` 内部用 `Value::Str(string).repr()` 自己生成带引号的 label，同时把 `kind`/`detail` 交给调用方（因为字体、包、路径各有不同类别和说明）。

#### 4.2.3 源码精读

三个入口都只有一行，但它们的出现让所有调用点变得干净（`src/complete.rs:1252-1270`）：

[complete.rs:1252-1270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1252-L1270) —— 三个便捷入口，各自钉死一组默认参数后委托给 `value_completion_full`。

来看它们各自的典型消费者，能更好理解分工：

- **`call_completion` 的消费者**——方法补全与模块成员补全（`src/complete.rs:184` 与 `src/complete.rs:190`）：

  [complete.rs:179-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L179-L192) —— `field_access_completions` 里补「带 self 参数的方法」与「模块/类型作用域成员」时都用 `call_completion`，因为它们都是可调用函数，要带括号。

  > 注释 L196-L197 指出：**函数字段不能用方法调用语法**，所以那里用的是 `value_completion`（不带括号），见 L200。

- **`value_completion` 的消费者**——字典键、content 字段、Args 具名项、get 规则（`src/complete.rs:218/223/228/238`）：

  [complete.rs:216-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L216-L242) —— 这些是「值」而非「可调用函数」，故用 `value_completion`（不带括号）。

- **`str_completion` 的消费者**——字体名（`complete.rs:1128`）、包名（`complete.rs:1147`）、文件路径（`complete.rs:1171`）：

  [complete.rs:1126-1133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1126-L1133) —— `font_completions` 用 `str_completion(family, Some(Font), Some(detail))`，label 自动生成带引号的字体名。

#### 4.2.4 代码实践

**实践目标**：用 `call_completion` 与 `value_completion` 的区别解释一个真实现象。

**操作步骤**：

1. 阅读 [complete.rs:194-201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L194-L201)，对比「类型预定义字段」用 `value_completion`，而上方「方法」用 `call_completion`。
2. 思考：对 `#("hello",)` 这个数组值（它是 `Value::Array`），在 `.|` 处补全时，`len` 这个方法会被补成 `len()` 还是 `len`？而如果一个 content 的字段叫 `body`，会被补成 `body` 还是 `body()`？

**需要观察的现象**：

- `len` 是 `array` 类型作用域里首参为 `self` 的方法 → 走 `call_completion` → `parens=true` → apply 形如 `len(${})`。
- content 的 `body` 字段 → 走 `value_completion`（L218 的 `content.fields()` 分支）→ `parens=false` → apply 为 `None`（即仅 `body`）。

**预期结果**：同为「点号后的成员」，方法带括号、字段不带括号——这一差异完全由选择哪个便捷入口（`parens` 为 true 还是 false）决定。

> 待本地验证：用 `test("#(1,2,3).", ...)` 等场景跑 `cargo test` 观察实际 apply。

#### 4.2.5 小练习与答案

**练习 1**：`str_completion` 为什么不像另外两个那样接收 `label`，而是接收 `string`？

> **答案**：字符串补全的 label 就是带引号的字符串本身（如 `"DejaVu Sans"`）。它内部构造 `Value::Str(string)` 交给 `value_completion_full`，由后者的 `value.repr()` 自动加上引号，省得调用方自己拼引号，也保证引号风格统一。

**练习 2**：如果你想新增一个「补全某个函数，但不要括号」的调用，应该用哪个入口？为什么没有现成的？

> **答案**：没有现成的「函数不带括号」入口。现有三者里 `call_completion` 必带括号、`value_completion` 不带括号但对函数会触发 `parens=false` 路径——此时 apply 退回 None（仅 label）。所以「补函数名但不带括号」直接用 `value_completion(label, &Value::Func(...))` 即可。这种需求少见（注释 L196 提到的「函数字段」是典型场景），所以没有专门入口。

**练习 3**：`scope_completions` 里对「带值的全局项」调用的是哪个入口？（提示：见 `complete.rs:1459`）

> **答案**：调用的是 `value_completion_full(Some(name), value, parens, None, None)`，直接走全量入口而非便捷入口——因为它要把外部的 `parens`（由 `scope_completions` 的形参传入）透传进去，便捷入口的固定布尔满足不了这个需求。

### 4.3 BracketMode —— 函数补全的括号策略

#### 4.3.1 概念说明

当 `value_completion_full` 决定要给一个函数补括号（`parens=true` 且光标后无 `(`/`[`）时，下一个问题是：**补哪种括号、光标停在哪、要不要换行？**

不同的函数有不同的「地道用法」：

- `#list(...)` 这种带参数的函数，光标应停在括号**里面**：`list(|)`。
- `#pagebreak` 这种几乎不传参的，光标应停在括号**后面**：`pagebreak()|`。
- `#emph[重点]` 这种天然接收 content 的，用方括号更地道：`emph[|]`。
- `#figure(...)` / `#table(...)` 这种参数多、通常换行写的，应自动换行缩进：
  ```
  figure(
    |
  )
  ```

`BracketMode` 就是把这四种「地道写法」编码成一个四值枚举，并用一个 `BracketMode::of(func)` 函数按函数的身份（名字、参数）挑选最合适的一种。这是一份**手工维护的经验表**——它知道哪些 Typst 内建函数「习惯上」该怎么写。

#### 4.3.2 核心流程

`of(func)` 的决策是一条**短路链**，从最特殊到最通用：

```
BracketMode::of(func):
  ① 全部参数都叫 self（或没有参数）
        → RoundAfter       函数无需参数，光标放括号后：func()|

  ② 按 func.name() 命名分派（仅原生函数才有 name）：
     ├─ emph / footnote / quote / strong / highlight
     │  overline / underline / smallcaps / strike / sub / super
     │     → SquareWithin   习惯用 content：func[|]
     ├─ colbreak / parbreak / linebreak / pagebreak
     │     → RoundAfter     无参断行/断页：func()|
     ├─ figure / table / grid / stack
     │     → RoundNewline   参数多，换行写：
     │                        func(
     │                          |
     │                        )
     └─ 其它（含用户自定义函数）
           → RoundWithin    默认：func(|)
```

选定模式后，`value_completion_full` 用 `eco_format!` 把模式翻译成 apply 字符串（回顾 4.1.3 的 L1306-L1311）：

| 模式 | apply 字符串 | 写入编辑器后光标位置 |
| --- | --- | --- |
| `RoundAfter` | `func()${}` | `func()`**之后** |
| `RoundWithin` | `func(${})` | `func(`**之内** `)` |
| `RoundNewline` | `func(\n  ${}\n)` | 换行缩进后的**之内** |
| `SquareWithin` | `func[${}]` | `func[`**之内** `]` |

#### 4.3.3 源码精读

先看枚举定义（`src/complete.rs:1466-1475`）：

[complete.rs:1466-1475](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1466-L1475) —— `BracketMode` 四变体，每个变体的文档注释就是它对应的括号形态。

再看 `of(func)` 的判断逻辑（`src/complete.rs:1477-1495`）：

[complete.rs:1477-1495](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1477-L1495) —— 先按参数判断 `RoundAfter`，再按函数名匹配三组白名单，最后兜底 `RoundWithin`。

几个关键点：

- L1481 的 `func.params().all(|param| param.name() == Some("self"))`：当函数**没有参数**（`all` 对空迭代器返回 `true`）或**只有 `self`** 时返回 `RoundAfter`。注释 L1479-L1480 解释了原因——这种函数调用时通常不需要填参，光标停在括号后更顺手。注意这是**第一道短路**，优先于按名字的分派。
- L1485 的 `func.name()`：只有**原生函数**才有名字（用户闭包返回 `None`）。这意味着所有「按名字」的白名单只对 Typst 标准库生效，用户自定义函数一律落到 L1492 的 `_ => RoundWithin`。
- 三组白名单（L1486-L1491）是硬编码的经验列表，维护在源码里。想新增一个「该用方括号」的函数，就是改这里。
- 兜底 `RoundWithin`（L1492）是**最安全的默认**：给括号、光标停里面，用户可继续填参或直接回车。

还有一处特例在 `value_completion_full` 内（回顾 `complete.rs:1301-1305`）：数学模式下**绕过** `BracketMode::of`，强制 `RoundWithin`。因为 `emph[…]` 的方括号 content 在数学公式里不合法，必须用圆括号。这也是测试 `test("$$", 1).at("overline").must_apply_as("overline(${})")`（`complete.rs:1824`）所固化的——`overline` 本该走 `SquareWithin`，但在数学模式下被改写成 `RoundWithin`。

#### 4.3.4 代码实践

**实践目标**：对照源码与现成测试，预测 `emph` 与 `figure` 的 apply 形式，并解释判断依据。这也是本讲义规格指定的实践任务。

**操作步骤**：

1. 阅读 [complete.rs:1485-1492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1485-L1492)，确认 `emph` 与 `figure` 各自命中哪一行。
2. 阅读 [complete.rs:1306-1311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1306-L1311)，把模式翻译成 apply 字符串。
3. 与测试 [complete.rs:1818-1820](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1818-L1820) 的断言核对。

**手动推导**：

- **`emph`**：先过 L1481 的参数检查——`emph` 有 `body` 参数（不是 `self`），`all(...)` 为假，不返回 `RoundAfter`。进 `match func.name()`，`"emph"` 命中 L1486-L1488 的第一组白名单 → `SquareWithin` → apply = `emph[${}]`。即选中后写入 `emph[|]`，光标停在方括号里。✅ 对应 `test("#", 1).at("emph"...）`（实际测试用的是同组的 `strong`/`footnote`，`complete.rs:1818-1819`）。
- **`figure`**：参数检查不通过（`figure` 有 `body`/`caption`/`placement` 等参数）。`"figure"` 命中 L1491 第二组 → `RoundNewline` → apply = `figure(\n  ${}\n)`。即写入
  ```
  figure(
    |
  )
  ```
  光标停在换行缩进后。✅ 对应 `test("#", 1).at("figure").must_apply_as("figure(\n  ${}\n)")`（`complete.rs:1820`）。

**需要观察的现象**：两者都补了括号，但形态完全不同——`emph` 用方括号（因为它是 formatting 类，习惯包 content），`figure` 用换行圆括号（因为它是容器类，参数多）。`BracketMode::of` 正是依据「函数名属于哪一组白名单」做出的判断。

**预期结果**：与测试断言完全一致。可运行 `cargo test test_autocomplete_bracket_mode` 验证（见 `complete.rs:1814`）。

#### 4.3.5 小练习与答案

**练习 1**：预测 `#pagebreak` 补全的 apply 形式，并给出完整推导链。

> **答案**：`pagebreak` 无参数 → L1481 的 `all` 对空迭代器为 `true` → 直接返回 `RoundAfter` → apply = `pagebreak()${}`，光标停在 `pagebreak()` 之后。（它也命中 L1490 的命名白名单，但参数检查在 `match` 之前短路，所以走的是第一条。）

**练习 2**：用户写了一个闭包 `#let f(x) = { x + 1 }`，在 `#f.|` 处补全时会补成什么？为什么？

> **答案**：补成 `f(${})`，即 `RoundWithin`。因为闭包没有原生名字（`func.name()` 为 `None`），`match func.name()` 的所有 `Some(...)` 分支都不命中，落到 L1492 的 `_ => RoundWithin`。用户自定义函数一律走这个默认。

**练习 3**：为什么在数学模式里要绕过 `BracketMode::of` 强制 `RoundWithin`？如果不绕过，`overline` 在数学模式里会补成什么、会有什么问题？

> **答案**：`overline` 名字命中白名单会得 `SquareWithin`，补成 `overline[${}]`。但方括号 content 块在 Typst 数学模式里不是合法语法，会触发解析错误。所以 `complete.rs:1301-L1305` 在 `mode_after() == Some(Math)` 时强制 `RoundWithin`，补成合法的 `overline(${})`。这是「语法合法性优先于地道写法」的权衡。

## 5. 综合实践

把本讲三块内容串起来，完成一次**完整的候选项生成追踪**。

**任务**：给定源码 `#rect(fill: l`（光标在 `l` 之后），追踪「补全用户自定义的颜色 `lila`」这一候选是如何从无到有被生成出来并最终呈现给用户的。假设 `lila` 是通过 `#let lila = rgb("#aabbcc")` 定义在某作用域内的。

**追踪步骤**：

1. **入口**：分发链最终落到 `scope_completions`（因为 `fill:` 之后是值位置，类型驱动补全见 u6-l4）。`scope_completions` 收集到名为 `lila` 的局部项，其 `value()` 是 `Some(rgb(...))`（因为 `let` 绑定的值类型是 `Var`，但值通过……见 u2-l3，这里假设能拿到值，或经后续 analyze）。调用 [complete.rs:1445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1445) 的 `self.value_completion(name.clone(), value)`。

2. **便利入口**：`value_completion("lila", &rgb(...))` 转发为 `value_completion_full(Some("lila"), value, false, None, None)`（[complete.rs:1252-1254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1252-L1254)）。

3. **算 label / at**：label = `"lila"`，是合法标识符 → `at = false`。

4. **算 detail**：`parens=false` 不影响 detail。值是 `Color`，不是 Symbol/Func/Type，走 [complete.rs:1289-1292](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1289-L1292) 的兜底分支：`repr = "rgb(...)"`，与 label `"lila"` 不等 → `detail = Some("rgb(#aabbcc)")`（具体 repr 形式由 `Repr` 实现决定）。

5. **算 apply**：`parens=false`，第一分支（函数括号）不满足；`at=false`，第二分支不满足；label 不以 `"` 开头，第三分支不满足 → `apply = None`。

6. **算 kind**：未指定，按值类型 → 颜色既不是 Func/Type/Symbol → `CompletionKind::Constant`。

7. **最终**：push 一条 `{ kind: Constant, label: "lila", apply: None, detail: Some("rgb(...)") }`。LSP 客户端因 `apply=None` 回退用 label，于是选中后写入 `lila`，菜单旁显示颜色的 repr。

**思考延伸**：如果把光标场景换成 `#em`（想补 `emph`），同样走一遍——这次 `parens=true`（函数补全），会进 `BracketMode` 分支，apply 变成 `emph[${}]`。对比两个场景，你能清楚看到 `parens` 开关如何把同一条 `value_completion_full` 路由到完全不同的 apply 生成逻辑。

## 6. 本讲小结

- `value_completion_full` 是**唯一的候选项生成出口**，把 label / detail / apply / kind 的决策全部收口，遵循「调用方优先、缺省兜底」。
- `detail` 的回退策略分三类：Symbol 不显示；Func/Type 查文档（`find_value_docs`）；其余用 `repr()` 且仅当与 label 不同时显示，避免冗余。
- `apply` 三分支互斥：**函数括号 > `at()` 非标识符键名 > 字符串去重引号 > None（回退 label）**。其中函数括号分支还要满足「光标后无 `(`/`[`」。
- 三个便捷入口 `value_completion`（不带括号）/ `call_completion`（带括号）/ `str_completion`（字符串）只是把常见默认值钉死，本质都委托给 `value_completion_full`。
- `BracketMode` 四模式（`RoundWithin`/`RoundAfter`/`RoundNewline`/`SquareWithin`）由 `of(func)` 按参数（self-only → RoundAfter）与函数名白名单（content 类 → 方括号、容器类 → 换行、断行类 → 括号后）挑选，用户自定义函数一律兜底 `RoundWithin`。
- 数学模式会**绕过** `of` 强制 `RoundWithin`，体现「语法合法性优先于地道写法」。

## 7. 下一步学习建议

本讲是补全引擎「内容生成」的收尾。至此，补全模块（`src/complete.rs`）的三大支柱——分发管线（u5-l1）、数据模型与三模式（u5-l2/l3）、特定场景补全（u6-l1~l4）与内容生成（本讲）——已全部讲完。建议：

1. **横向对比 tooltip**：u3-l3 讲过 `expr_tooltip` 如何把值渲染成提示文本。对比本讲的 `value_completion_full` 如何把值渲染成 apply/detail，你会看到「值的展示」在不同功能里复用同一套思路（repr、find_value_docs）。
2. **进入 u7（jump）**：补全与悬停都是「源码 → 信息」，而 jump 是「源码 ↔ 渲染结果」的双向映射，是 typst-ide 最有特色的部分，依赖 `typst-html`/`typst-layout`。
3. **动手扩展**：尝试给 `BracketMode::of` 的白名单新增一个函数（例如某个第三方常见函数该用方括号），跑 `cargo test test_autocomplete_bracket_mode` 验证，体会这份「经验表」的维护方式。
