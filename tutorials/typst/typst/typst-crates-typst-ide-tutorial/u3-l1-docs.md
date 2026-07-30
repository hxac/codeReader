# docs —— 查找值与参数的文档

## 1. 本讲目标

在编辑器里把光标悬停在某个函数名或参数名上时，旁边会弹出一段说明文字（悬停提示 tooltip）；在补全列表里，每一项后面也会跟着一行灰色说明（detail）。这些「说明文字」从哪里来？这就是 `typst-ide` 的 `docs.rs` 要解决的问题。

本讲学完后，你应该能够：

1. 说出 `Docs` 的两种来源 `Native` 与 `Comment` 分别对应什么场景。
2. 解释 `find_value_docs` 如何先查原生静态文档、再回退到源码注释；`find_param_docs` 如何按 `ParamInfo` 的三种变体分发。
3. 读懂 `collect_doc_comment` 沿「前置 trivia 兄弟」向上收集行注释 / 块注释的算法，并能手算它收集到的行顺序。
4. 读懂 `summary` 把一段文档压缩成「首句纯文本」的规则（去 Markdown、保留反引号代码、按句号断句）。
5. 理解 `Docs` 通过 `Deref<Target = str>` 被「当作字符串」使用的设计。

本讲只聚焦 [`src/docs.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs) 这一个文件（约 170 行），它是悬停提示和补全说明共享的「文档提取」后勤。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下内容（这些在前置讲义中已建立）：

- **光标 → 语法树节点**（u2-l1）：`Source::root()`、`LinkedNode`、`leaf_at(cursor, side)`、trivia（空白、注释等无语义节点）的概念。本讲里的 `collect_doc_comment` 就是在 `LinkedNode` 之间做兄弟遍历。
- **utils 共享工具集**（u2-l5）：理解 `IdeWorld` 是所有公共函数的首参，以及 typst-ide 的 best-effort 风格。
- **`Value` 与函数对象**：typst 里一切都是 `Value`，函数也是一种 `Value::Func`。原生（Rust 定义）函数和用户（Typst 闭包）函数的文档来源不同。

### 关键术语速览

| 术语 | 含义 |
| --- | --- |
| 原生函数（native function） | 用 Rust 在 `typst-library` 里定义的函数，如 `#rect()`、`#emph()`。它带有编译期就写死的文档字符串。 |
| 用户函数（closure） | 用户在 `.typ` 源码里用 `#let f(...) = ...` 定义的函数，没有静态文档，只能去源码里找注释。 |
| trivia 节点 | 语法树里没有语义的节点：空白（`Space`）、注释（`LineComment`/`BlockComment`）等。 |
| doc comment | 生态里常见的 `///` 三斜杠注释，被当作「文档注释」。typst 语言层面并未标准化它，只是约定俗成。 |
| 首句摘要（summary） | 文档的第一句话，去掉 Markdown 格式后得到的纯文本，适合在补全列表的一行 detail 里展示。 |

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`src/docs.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs) | **本讲核心**。定义 `Docs` 类型、`find_value_docs` / `find_param_docs` 两个入口、`collect_doc_comment` 与 `summary` 两个方法。 |
| [`src/tooltip.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs) | 消费方之一。悬停提示用 `find_value_docs` / `find_param_docs` 取文档后调用 `summary()`。 |
| [`src/complete.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs) | 消费方之二。补全项的 `detail` 字段用这两个函数取文档摘要。 |

跨 crate 的依赖类型（本讲会引用，但不深入）：

| 类型 | 位置 | 作用 |
| --- | --- | --- |
| `ParamInfo` | `typst-library/src/foundations/func.rs` | 描述一个函数参数，有 `Native` / `Closure` / `Plugin` 三种变体。 |
| `Value::docs()` | `typst-library/src/foundations/value.rs` | 对原生函数 / 类型返回静态文档字符串。 |
| `LineComment` / `BlockComment` | `typst-syntax/src/ast.rs` | 行注释 / 块注释的 AST 视图，提供去掉 `//`、`/* */` 的 `text()`。 |
| `prev_sibling_with_trivia` | `typst-syntax/src/node.rs` | 取「前一个兄弟节点（含 trivia）」，是 `collect_doc_comment` 遍历的基石。 |

## 4. 核心概念与源码讲解

本讲按 5 个最小模块展开：先看 `Docs` 类型本身（4.1），再看两个入口 `find_value_docs`（4.2）、`find_param_docs`（4.3），然后深入两个内部方法 `collect_doc_comment`（4.4）和 `summary`（4.5）。

### 4.1 Docs —— 文档的统一类型

#### 4.1.1 概念说明

`Docs` 是一个很小的枚举，代表「一段文档」。它只有两个变体：

- `Docs::Native(&'static str)`：来自原生（Rust）定义的静态文档字符串。典型来源是 `typst-library` 里用 `#[func]` 宏生成的函数 / 类型 / 参数文档。这种文档在编译期就写死，生命周期是 `'static`，不需要解析源码。
- `Docs::Comment(EcoString)`：来自用户源码里的注释，由 `collect_doc_comment` 动态收集而来，是运行时构造的字符串。

为什么要分两类？因为**文档的来源本质上有两种**：

1. 原生函数的文档是 Rust 代码里的常量，直接读即可，便宜且确定。
2. 用户闭包的文档不存在「常量」，只能回到用户的 `.typ` 源码里，把定义前的注释抠出来。

`Docs` 把这两种异构来源统一成一个类型，下游（tooltip / complete）就不必关心文档到底从哪来，统一调用 `.summary()` 即可。

#### 4.1.2 核心流程

`Docs` 还实现了两个 trait，让它在用法上「就像一个字符串」：

- `Deref<Target = str>`：把 `&Docs` 自动解引用成 `&str`。这样在 `summary` 内部可以直接写 `self.split("\n\n")`（`str` 的方法）。
- `From<Docs> for EcoString`：允许 `docs.into()` 得到一个 `EcoString`，方便塞进 tooltip 的文本字段。

流程上：上层拿到 `Option<Docs>` → 若 `Some`，调用 `.summary()` 得首句，或 `.into()` 得完整字符串。

#### 4.1.3 源码精读

`Docs` 的定义只有三行：

[docs.rs:L52-L56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L52-L56) 定义了 `Docs` 枚举，`Native` 持有 `'static str`、`Comment` 持有 `EcoString`。

`Deref` 实现把两个变体都映射成内部的 `&str`：

[docs.rs:L153-L162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L153-L162) `Deref::deref` 对 `Native` 和 `Comment` 分别返回其内部字符串切片，使 `Docs` 可像 `&str` 一样使用。

`From` 实现（[docs.rs:L164-L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L164-L171)）同理，把两种来源统一转成 `EcoString`。

#### 4.1.4 代码实践

**实践目标**：理解「两种来源 → 同一个类型」的合并价值。

**操作步骤**：

1. 打开 [`src/docs.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs)，定位 `Docs` 枚举与两个 trait 实现。
2. 搜索消费方：在 [`src/tooltip.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs) 中查找 `find_value_docs`（约第 79 行）和 `find_param_docs`（约第 234 行），观察它们拿到 `Docs` 后都调用了 `.summary()`。

**需要观察的现象**：tooltip / complete 的调用方**完全没有判断**文档是 `Native` 还是 `Comment`——它们只认 `Option<Docs>` 和 `.summary()`。

**预期结果**：你会确认「来源判定」被封装在 `find_*` 入口里，下游被彻底屏蔽。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Native` 用 `&'static str` 而 `Comment` 用 `EcoString`？

**参考答案**：`Native` 来自 Rust 编译期常量（`typst-library` 里的 `#[func]` 宏生成），其字符串生命周期是 `'static`，可以直接引用、零拷贝；`Comment` 是在用户源码运行时收集、拼接出来的，必须拥有自己的堆字符串，故用 `EcoString`。

**练习 2**：如果删掉 `Deref for Docs` 的实现，`summary` 方法里的 `self.split("\n\n")` 还能编译吗？

**参考答案**：不能（或需要改写）。`self: &Docs`，`Docs` 本身没有 `split` 方法，正是靠 `Deref<Target = str>` 才把方法调用转发到 `str::split` 上。

---

### 4.2 find_value_docs —— 给「值」找文档

#### 4.2.1 概念说明

`find_value_docs(world, value)` 的任务是：给定任意一个 `Value`，尽量找出它的文档。它采用**两级回退**策略：

1. **第一级（快、确定）**：调用 `value.docs()`。只有原生函数（`Value::Func` 里的 native）和类型（`Value::Type`）会返回 `Some(&'static str)`；其它值（数字、字符串、content……）返回 `None`。命中即返回 `Docs::Native`。
2. **第二级（慢、best-effort）**：如果第一级落空，且这个值恰好是一个**用户闭包函数**，就尝试回到源码，在它的 `let` 绑定前找注释。命中即返回 `Docs::Comment`。

#### 4.2.2 核心流程

用伪代码描述第二级的判定链（这是整段函数最精巧的部分）：

```text
value 是 Value::Func(func) 吗？        否 → 返回 None
取 func.span()                         拿到闭包在源码里的位置 span
span.id() 有值吗（来自文件）？          否 → 返回 None（span 在虚拟文件里）
world.source(id) 能读到源码吗？         出错 → 返回 None
source.find(span) 能定位到节点吗？      否 → 返回 None
该节点的父节点是 Closure 吗？           否 → 不是闭包定义，放弃
父节点的父节点是 LetBinding 吗？        否 → 比如 (#f)(x) 这种匿名调用，放弃
对 LetBinding 调 collect_doc_comment   收集它前面的注释
收集到了吗？                            是 → 返回 Docs::Comment；否 → None
```

关键点：**必须同时满足「父 = Closure 且祖父 = LetBinding」**。也就是说，只对 `#let f(...) = ...` 这种「有名字的闭包绑定」去抠注释。匿名闭包（如 `#let f = (x) => x` 里右值、或 `(x) => x` 直接调用）不会被处理，因为它们前面通常没有有意义的 doc 注释结构。

#### 4.2.3 源码精读

[docs.rs:L9-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L9-L31) 是 `find_value_docs` 全文。其中 L11–L13 是第一级（`value.docs()` → `Docs::Native`），L16–L28 是第二级（闭包注释回退），L30 兜底返回 `None`。

第二级里这一串 `&& let ...` 是 Rust 的 let-chain（链式 let 条件），一步步短路：任何一步失败就整体跳到 `None`。注意 L25 调用的是 `Docs::collect_doc_comment(grand.clone())`——传入的是 **LetBinding 节点**，从它开始往前找注释。

`value.docs()` 的定义在跨 crate 处：

[value.rs:L183-L190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L183-L190) 显示 `Value::docs()` 只对 `Func` 和 `Type` 返回文档，其余返回 `None`。

#### 4.2.4 代码实践

**实践目标**：验证「原生函数走第一级、用户闭包走第二级」的分流。

**操作步骤**：

1. 在 [`src/tooltip.rs` 第 79 行附近](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L79) 阅读 `expr_tooltip` 如何调用 `find_value_docs(world, value)` 并把结果包成 `Tooltip::Text(docs.summary())`。
2. 想象两个悬停场景：
   - 光标悬停在 `#rect` 的 `rect` 上：`analyze_expr` 得到原生函数值，`find_value_docs` 走第一级返回 `Docs::Native(...)`。
   - 光标悬停在用户定义的 `#let add(a, b) = a + b` 的 `add` 上：第一级 `None`，走第二级回源码抠注释。

**需要观察的现象**：两种场景下 tooltip 的文字内容来源不同，但对调用方完全透明。

**预期结果**：能口述出「原生 → 静态常量；闭包 → 源码注释」的回退链。若要实际看到运行结果，**待本地验证**（可在 `src/tests.rs` 里仿写一个 tooltip 测试，分别对原生函数和带注释的闭包断言 `must_include`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么第二级要求「祖父是 LetBinding」？如果只要求「父是 Closure」会怎样？

**参考答案**：闭包节点也可能是匿名右值（如 `#let f = (x) => x` 或 `#((x) => x)(1)`），这类闭包前面通常没有紧贴的 doc 注释，或注释归属不明。要求祖父是 `LetBinding`，能锁定「`#let 名字 = 闭包`」这一种结构清晰、注释语义明确的形态，避免误抠。这体现了 best-effort：宁可漏掉一些，也不要乱报。

**练习 2**：`#let x = 10` 里的 `x` 悬停时，`find_value_docs` 会返回什么？

**参考答案**：返回 `None`。第一级 `value.docs()` 对整数返回 `None`；第二级要求 `value` 是 `Value::Func`，整数不满足，直接 `None`。所以整数字面量不会有「文档 tooltip」（tooltip 会走别的分支，比如直接显示值）。

---

### 4.3 find_param_docs —— 给「参数」找文档

#### 4.3.1 概念说明

`find_param_docs(world, param)` 的任务是：给定一个函数参数（`ParamInfo`），找出它的文档。它和 `find_value_docs` 思路一致——**原生走常量、用户走源码注释**——但分发依据是 `ParamInfo` 的三个变体。

`ParamInfo` 描述一个函数参数，有三种变体（见 [func.rs:L525-L534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L525-L534)）：

| 变体 | 含义 | 文档来源 |
| --- | --- | --- |
| `ParamInfo::Native(&'static NativeParamInfo)` | Rust 原生函数的参数 | 直接取 `param.docs`（`'static str`）→ `Docs::Native` |
| `ParamInfo::Closure(Spanned<ClosureParamInfo>)` | 用户闭包的参数 | 回源码找该参数前的注释 → `Docs::Comment` |
| `ParamInfo::Plugin` | 插件函数的唯一可变字节参数 | 没有 → `None` |

#### 4.3.2 核心流程

```text
match param {
  Native(param) => 返回 Docs::Native(param.docs)
  Closure(param) =>
    取 param.span（参数在源码里的位置）
    world.source(id) 读源码
    source.find(span) 定位参数节点
    collect_doc_comment(参数节点) 抠它前面的注释
    有 → Docs::Comment；无 → None
  Plugin => None
}
```

注意：与 `find_value_docs` 不同，这里收集注释的起点是**参数节点本身**，而不是 `LetBinding`。因为参数的 doc 注释（如 `#let f(a, b)` 里的 `a`、`b`）就紧贴在参数 ident 前，在闭包参数列表里作为兄弟节点存在。

#### 4.3.3 源码精读

[docs.rs:L33-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L33-L50) 是 `find_param_docs` 全文。L36 的 `ParamInfo::Native(param) => Some(Docs::Native(param.docs))` 是最快路径；L37–L47 处理闭包参数，L42 调 `Docs::collect_doc_comment(node.clone())`，注意这里 `node` 是 `source.find(param.span)` 找到的参数节点；L48 的 `Plugin => None`。

消费方示例：[tooltip.rs:L230-L237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L230-L237) 显示悬停在参数名上时，先 `func.param(&ident)` 取到 `ParamInfo`，再 `find_param_docs` 取文档，最后 `docs.summary()` 包成 `Tooltip::Text`。

补全里的用法：[complete.rs:L555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L555) 用 `find_param_docs(ctx.world, &param).map(|docs| docs.summary())` 填补全项的 `detail`。

#### 4.3.4 代码实践

**实践目标**：对比原生参数与闭包参数的文档路径。

**操作步骤**：

1. 读 [func.rs:L525-L556](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L525-L556)，确认 `ParamInfo` 三变体与 `name()` 方法。
2. 构造两个悬停场景：
   - 在 `#rect(width:|)` 处补全或悬停 `width`：参数来自原生 `rect`，走 `ParamInfo::Native`，文档是 `param.docs`（`'static str`）。
   - 在用户闭包 `#let f(a, b) = ...` 的 `a` 上悬停：走 `ParamInfo::Closure`，回源码抠注释。

**需要观察的现象**：原生参数的文档是即时可得的常量；闭包参数需要解析源码定位节点。

**预期结果**：能说出三种变体分别返回什么。运行层面的精确输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`find_param_docs` 对 `ParamInfo::Plugin` 返回 `None`，为什么？

**参考答案**：插件（WebAssembly 插件）函数只有一个可变的字节参数，它在 typst 侧没有有意义的、可供展示的参数文档，所以直接返回 `None`。

**练习 2**：`find_param_docs` 在闭包参数分支里，为什么不像 `find_value_docs` 那样要求「祖父是 LetBinding」？

**参考答案**：参数的 doc 注释就紧贴在参数 ident 之前，二者是闭包参数列表里的兄弟节点，从参数节点往前走即可命中，不需要再上升到 `LetBinding` 层级。两种函数针对各自不同的注释布局，选了不同的收集起点。

---

### 4.4 collect_doc_comment —— 沿前置 trivia 收集注释

这是 `docs.rs` 里最值得精读的函数，也是本讲的重头戏。

#### 4.4.1 概念说明

`collect_doc_comment(node)` 的目标是：从给定节点出发，**沿「前一个兄弟（含 trivia）」一路向左走**，把沿途紧挨着的注释全部收起来，拼成一段文档。

源码里的注释特别强调：这是一个 **pragmatic（实用主义）、best-effort** 的函数，只处理「生态里当下在用」的 doc 注释习惯；**它的存在不代表 typst 在语言层面标准化了任何 doc 注释格式**。换言之，`///` 只是约定，不是规范。

#### 4.4.2 核心流程

算法分两阶段：**收集**（向前走，逆序压栈）和**拼接**（逆序出栈，正序拼接）。

阶段一·收集（while 循环，`prev_sibling_with_trivia` 每次取「前一个兄弟，含 trivia」）：

```text
current = node
while let prev = current.prev_sibling_with_trivia():
    if prev 是 LineComment:
        text = prev.text()         // LineComment::text() 已去掉前缀 "//"
        lines.push( text 去掉至多一个前导 "/" )   // 把 "///" 的多余斜杠也去掉
    else if prev 是 BlockComment:
        lines.push( prev.text() )  // BlockComment::text() 已去掉 "/*" "*/"
    else if prev 不是 Space 且不是 Hash:
        break                      // 撞到「真正的代码节点」，注释链断裂，停止
    current = prev                 // Space / Hash 只是「胶水」，继续往前走
若 lines 为空 → 返回 None
```

阶段二·拼接（`for line in lines.iter().rev()`，把逆序收集的行翻成正序）：

```text
output = ""
for line in lines 反转:            // 反转后：最靠上的注释在最前
    output += (line 去掉至多一个前导空格)
    output += "\n"
output 去掉末尾的 "\n"             // pop 掉最后多加的一个换行
返回 Docs::Comment(output)
```

两个关键设计：

1. **斜杠归一化**：`LineComment::text()` 会先去掉 `//`，所以 `/// foo` 变成 `/ foo`，再 `strip_prefix('/')` 变成 ` foo`；而普通 `// foo` 经过 `text()` 后是 ` foo`，`strip_prefix('/')` 不生效（已是空格开头）。结果是 **`/// foo` 与 `// foo` 被归一化成同样的 ` foo`**。这正是源码注释说的「三斜杠很常见，所以剥掉那一个多余的斜杠」。
2. **Space / Hash 是胶水**：空白（含换行）和 `#` 不打断注释链，让算法能跨过 `#let` 前后的空白、`#` 等把多行注释连起来；但一旦撞到别的代码节点（ident、表达式等）就立刻停止。

#### 4.4.3 源码精读

[docs.rs:L58-L98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L58-L98) 是 `collect_doc_comment` 全文。注意 L67 的 doc comment 明确写了「pragmatic / best-effort / 不代表语言层标准化」。L71 的 `prev_sibling_with_trivia` 是向前遍历的核心；L76 的 `strip_prefix('/')` 是三斜杠归一化；L79 的 `!matches!(prev.kind(), SyntaxKind::Space | SyntaxKind::Hash)` 是「胶水」判定；L90 的 `.rev()` 是逆序拼接。

`prev_sibling_with_trivia` 本身在 typst-syntax 里：

[node.rs:L1241-L1249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/node.rs#L1241-L1249) 取紧邻的前一个兄弟（含 trivia），与 `prev_sibling`（跳过 trivia）相对。这正是本函数能「连注释带空白一起走」的原因。

`LineComment::text()` 与 `BlockComment::text()` 的去前缀逻辑：

[ast.rs:L198-L224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L198-L224) 中，`LineComment::text()` 去掉 `//`，`BlockComment::text()` 去掉首 `/*` 与尾 `*/`。

#### 4.4.4 代码实践

**实践目标**：手算一段带 `///` 与 `//` 注释的闭包，`collect_doc_comment` 收集到的行顺序与最终字符串。

**操作步骤**：给定源码片段（示例代码，非项目原有代码）：

```typst
/// Adds two numbers.
/// Returns their sum.
// (implementation note, not a doc comment in spirit, but collected anyway)
#let add(a, b) = a + b
```

假设 `find_value_docs` 已经定位到 `add` 的 `LetBinding` 节点，并以它为起点调用 `collect_doc_comment`。请按阶段一算法从 `LetBinding` 向左走：

1. 前一个兄弟：`Space`（注释与 `#let` 之间的换行/空白）→ 胶水，继续。
2. 前一个兄弟：`LineComment("// (implementation note...)")` → `text()` 得 ` (implementation note...)`，`strip_prefix('/')` 不生效（开头是空格）→ 压入 `lines`。
3. 前一个兄弟：`Space` → 继续。
4. 前一个兄弟：`LineComment("/// Returns their sum.")` → `text()` 得 `/ Returns their sum.`，`strip_prefix('/')` 得 ` Returns their sum.` → 压入。
5. 前一个兄弟：`Space` → 继续。
6. 前一个兄弟：`LineComment("/// Adds two numbers.")` → `text()` 得 `/ Adds two numbers.`，`strip_prefix('/')` 得 ` Adds two numbers.` → 压入。
7. 再往前若无更多紧邻注释 / 撞到代码节点（或到 Markup 起点），循环结束。

于是 `lines`（收集时的顺序，离节点最近的在前）为：

```text
[ " (implementation note...)",
  " Returns their sum.",
  " Adds two numbers." ]
```

阶段二 `.rev()` 翻成正序，每行再去掉至多一个前导空格，用 `\n` 连接并去掉末尾换行，最终：

```text
Adds two numbers.
Returns their sum.
(implementation note...)
```

**需要观察的现象**：

- 三行注释按「从上到下」的阅读顺序出现在结果里（因为收集是逆序、拼接又反转回来）。
- `///` 和 `//` 被同等对待——那句「implementation note」虽是普通 `//`，也被收进来了。

**预期结果**：得到上面的三行字符串。注意「具体的 trivia 兄弟序列取决于解析树的实际形态」，上述手算基于注释紧邻 `#let` 的常见结构；精确的运行时输出**待本地验证**（建议本地写一个 tooltip / docs 测试确认）。

#### 4.4.5 小练习与答案

**练习 1**：如果两行注释之间隔了一个真正的代码节点（例如一个 ident），`collect_doc_comment` 会收到几行？

**参考答案**：只会收到「离起点最近、未被代码节点打断」的那一段。一旦向前走撞到非 Space/Hash/Comment 的节点，循环 `break`，更前面的注释不会被打捞。所以注释链必须是「连续紧邻」的。

**练习 2**：为什么 `/// foo` 和 `// foo` 最终被归一化成同样的结果？

**参考答案**：`LineComment::text()` 先剥掉 `//`，使 `/// foo` → `/ foo`、`// foo` → ` foo`；随后 `collect_doc_comment` 再 `strip_prefix('/')` 一次，把 `/ foo` 也变成 ` foo`。两步合起来抵消了三斜杠多出的那一划，使二者等价。这是为了适配生态里「`///` 当文档注释」的约定，同时不排斥普通 `//`。

---

### 4.5 summary —— 抽取首句纯文本

#### 4.5.1 概念说明

`summary(&self)` 把一段（可能含 Markdown / Typst 标记的）文档，压缩成**第一句话的纯文本**，专门用于补全列表里那一行 detail、或 tooltip 的简短提示。它的处理目标：

- 只取**第一段**（以空行 `\n\n` 分隔）。
- 在第一段里取**第一句**（以句号 `.` + 空白 / 结尾 断句）。
- 去掉 Markdown 强调标记（`*`、`_`）。
- **保留反引号代码**（`` `code` ``），并剥掉其中多余的 `{}` 或 `[]` 包裹。
- 把链接 `[label](url)` / `[label][ref]` 退化成只剩 `label` 文本。

源码注释坦言：doc 注释里到底是纯文本、Markdown 还是 Typst，并不确定，这只是「okay-ish（凑合可用）」的处理。

#### 4.5.2 核心流程

用一个字符级状态机扫描第一段：

```text
paragraph = self 按 "\n\n" 切出的第一段
scanner = Scanner::new(paragraph)
output = ""
link = false                      // 是否处在 [label...] 的 label 中
while c = scanner.eat():
    match c:
      '`' →                        // 反引号代码
        raw = 读到下一个反引号之间的内容
        若 raw 形如 {…} 或 […] → 剥掉最外层一对
        output += "`" + raw + "`"
        吃掉收尾的反引号
      '[' → link = true            // 进入链接 label
      ']' 且 link:
        若后面紧跟 '(' → 跳过 "(url)"   // 内联链接
        否则若后面紧跟 '[' → 跳过 "[ref]" // 引用链接
        link = false
                                   // label 文本已被逐字符 push 进 output
      '*' | '_' → 丢弃              // 去强调
      '.' →
        output += '.'
        若 (到尾 或 下一个是空白) 且 倒数第 3 个字符不是 '.':
            break                  // 句末，停止
      其它 → output += c
返回 output
```

句号断句里的 `scout(-3) != Some('.')` 是个细节优化：它防止在 `e.g.`、`See foo.bar.` 这类**缩写或带点的标识符**处提前断句——如果句号往前第 3 个字符也是句号，就认为这不是真正的句末，继续扫描。

#### 4.5.3 源码精读

[docs.rs:L100-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/docs.rs#L100-L150) 是 `summary` 全文。L106 取第一段；L112–L124 处理反引号代码（含 L114–L118 的 `{}`/`[]` 剥离）；L125–L135 处理链接 label（`[` 置位、`]` 配合 `(` 或 `[` 跳过目标）；L136 丢弃 `*`/`_`；L137–L144 是句号断句与 `scout(-3)` 防缩写优化。

#### 4.5.4 代码实践

**实践目标**：手算 `summary` 对一段文档的输出。

**操作步骤**：接 4.4.4 得到的完整文档字符串（`Docs::Comment(...)` deref 后是三行），但 `summary` 只看**第一段**。本例三行之间是单换行 `\n`（不是空行 `\n\n`），所以整段都属第一段。第一句以第一个「后接空白/结尾的句号」结束。

扫描到第一个 `.` 是 `Adds two numbers.` 末尾的句号：其后是换行（空白），且 `scout(-3)` 往前第 3 个字符不是 `.`，故在此断句。

**需要观察的现象**：尽管完整文档有三行，`summary` 只返回第一句。

**预期结果**：`summary()` 返回 `Adds two numbers.`（含句号）。

再试一个含 Markdown 的例子（示例代码）：

```text
Creates a *red* box. See `color.red`.
```

- `Creates a ` → 逐字输出（`*` 前的空格也输出）
- `*` → 丢弃（去强调），`red` 输出，`*` 丢弃
- ` box` → 输出
- 第一个 `.` 后是空格 → 断句

结果：`Creates a red box.`（`*red*` 被压成 `red`，在第一个句号停止，`` `color.red` `` 这句被丢弃）。精确输出**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：文档是 `"See e.g. the docs.\n\nMore text."`，`summary()` 返回什么？

**参考答案**：返回 `See e.g. the docs.`。第一段是 `See e.g. the docs.`（`\n\n` 之前）。扫描时，`e.g.` 的两个句号后都紧跟字母（非空白），不会断；且第二个 `.` 的 `scout(-3)` 命中前一个 `.`，也不断。直到 `docs.` 后是段尾，才断句。

**练习 2**：文档是 ``"Use `#rect[]` here. Done."``，`summary()` 返回什么？

**参考答案**：返回 `` `Use #rect[] here.` ``。反引号内的 `#rect[]` 被保留（`raw` 形如 `[…]`，外层 `[]` 被 L114–L118 剥掉，于是 `[#rect[]]`……此处需注意：实际 `raw` 是 `#rect[]`，它 `starts_with('[')` 为假，所以不剥，原样保留为 `#rect[]`），随后第一个句号断句。精确结果**待本地验证**（取决于 `raw` 实际内容是否以 `[` 开头）。

## 5. 综合实践

把 5 个模块串起来，完成一次「从值到首句摘要」的完整追踪。

**任务**：给定下面的 Typst 源码（示例代码），请完整描述当用户把光标悬停在 `add` 上时，文档是如何一步步被提取并展示的。

```typst
/// Adds two integers and returns the sum.
#let add(a, b) = a + b

#add
```

**要求你回答**：

1. tooltip 的 `expr_tooltip` 调用 `analyze_expr` 得到的 `value` 是什么类型？（答：`Value::Func`，且是一个闭包函数。）
2. `find_value_docs` 第一级 `value.docs()` 返回什么？为什么？（答：`None`，因为闭包不是原生函数 / 类型。）
3. 第二级如何定位到 `LetBinding`？写出「父 = Closure、祖父 = LetBinding」的判定。
4. `collect_doc_comment` 从 `LetBinding` 向左走，收集到几行？最终 `Docs::Comment` 的字符串是什么？（手算：一行 ` Adds two integers and returns the sum.`，去前导空格后为 `Adds two integers and returns the sum.`。）
5. `summary()` 在哪个字符处断句？返回值是什么？（答：在 `sum.` 的句号处断，返回 `Adds two integers and returns the sum.`。）
6. 最终 tooltip 展示什么？（答：`Tooltip::Text("Adds two integers and returns the sum.")`。）

**进阶**：把上面的 `#add` 改成悬停在原生函数 `#rect` 上，重走第 1–6 步，指出哪一步不同（答：第 2 步 `value.docs()` 直接返回 `Some`，走 `Docs::Native`，跳过 3–4 步的源码抠注释）。

完成后，建议本地在 [`src/tests.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs) 仿写一个 tooltip 测试，用 `must_include` 断言你的手算结果——运行层面的精确输出**待本地验证**。

## 6. 本讲小结

- `Docs` 是文档的统一类型，只有两种来源：`Native(&'static str)`（原生静态文档）与 `Comment(EcoString)`（源码注释），通过 `Deref<Target=str>` 被「当字符串」使用。
- `find_value_docs` 采用两级回退：先 `value.docs()`（仅原生函数 / 类型命中），再对「父=Closure、祖父=LetBinding」的用户闭包回源码抠注释。
- `find_param_docs` 按 `ParamInfo` 三变体分发：`Native` 取常量、`Closure` 回源码抠注释、`Plugin` 返回 `None`；闭包参数的收集起点是参数节点本身。
- `collect_doc_comment` 沿 `prev_sibling_with_trivia` 向左走，收集连续紧邻的行 / 块注释；`Space`/`Hash` 是胶水，撞到别的代码节点即停；`///` 与 `//` 被归一化。
- `summary` 用字符级状态机把文档压成第一段的第一句：去 `*`/`_` 强调、保留反引号代码、链接退化为 label、按句号断句并用 `scout(-3)` 防缩写误断。
- 整个模块是 best-effort / pragmatic 的：`///` 只是生态约定，typst 语言层面并未标准化 doc 注释格式。

## 7. 下一步学习建议

本讲解锁了「文档从哪来」这一后勤，下一步自然进入它的消费方：

- **u3-l2 tooltip 总入口与分发策略**：看 `tooltip.rs` 如何把 `find_value_docs` / `find_param_docs` 串联进短路分发链，以及为何 named_param 优先于 expr。
- **u3-l3 表达式与函数调用的 tooltip**：看 `expr_tooltip` 如何先用 `analyze_expr` 得到候选值，再调 `find_value_docs` 取文档。
- 阅读建议：结合 [`src/tooltip.rs` 第 79 行与第 234 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L79) 与 [`src/complete.rs` 第 555 行与第 1287 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L555) 对照「文档被谁、如何消费」，把本讲的产出和下游串成完整链路。
