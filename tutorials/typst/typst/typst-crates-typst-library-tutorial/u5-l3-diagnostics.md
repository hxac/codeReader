# 诊断系统：bail/error/warning 与结果类型

## 1. 本讲目标

本讲专讲 Typst 编译器如何「报错」。读完本讲，你应当能够：

- 说清 `SourceResult`、`StrResult`、`HintedStrResult` 三种结果类型的区别，以及它们各自出现在什么场景。
- 看懂 `bail!` 宏的三个分支，尤其是「带 `span`」和「不带 `span`」为什么对应不同的返回类型。
- 掌握 `error!`、`warning!` 宏的用法，以及它们背后的「双下划线改名」技巧。
- 理解 `Hint` trait 如何给错误追加提示，`At` trait 如何把「没有位置的错误」升级成「带源码位置的诊断」。
- 能在真实源码中追踪一条从 `StrResult` 经 `.at(span)` 变成 `SourceResult` 的链路。

---

## 2. 前置知识

本讲是「编译环境与核心机制」单元的第三篇，承接 [u5-l2 Engine、Route、Sink 与 Traced](u5-l2-engine-route-sink.md)。在那里我们见到了 `Sink` 的「延迟错误」桶（`delayed`）和「警告」桶（`warnings`）。本讲要回答：这些桶里装的「错误对象」到底是什么？函数在执行途中是怎么把它们造出来的？

你需要先建立两个直觉：

1. **错误需要「位置」才能被定位**。一条错误信息（`"file not found"`）本身只是一段字符串，编译器要把它画到源码的某一行某一列，必须额外知道一个 `Span`（源码区间句柄）。`Span` 的概念来自 `typst-syntax`，你可以先把它当成「指向源码某段文本的不透明指针」。
2. **不是所有函数都立刻知道「位置」**。一个底层纯计算函数（比如 `calc.pow`）只负责算数，它出错时只知道「算不下去了」，并不知道这个调用在用户源码的哪一行——位置信息要到上层调用点才有。于是 Typst 设计了一套「先报字符串错误，到了有 `span` 的地方再补位置」的两段式机制。

本讲要讲的 `src/diag.rs`（诊断模块）正是这套机制的载体。

> 名词速查
> - **诊断（diagnostic）**：一条带严重级别、源码位置、消息、调用踪迹和提示的结构化错误/警告，是最终交给用户的最小单元。
> - **严重级别（severity）**：`Error`（致命）或 `Warning`（非致命）。
> - **提示（hint）**：附在诊断后面、告诉用户「怎么改」的补充文字。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/diag.rs` | 诊断系统全部核心：三种 `Result` 别名、`SourceDiagnostic` 数据结构、`bail!`/`error!`/`warning!` 宏、`At`/`Hint`/`Trace` trait，以及 `FileError`/`PackageError`/`LoadError` 等文件相关错误。 |
| `src/foundations/calc.rs` | 用作 `#[func]` 中 `.at(span)` 用法的真实范例（`pow` 函数）。 |
| `src/foundations/mod.rs` | 用作「不带 `span`」的 `bail!` 真实范例（`assert` 函数）。 |
| `src/engine.rs` | （前置）`Engine`/`Sink` 所在地，`sink.warn(...)`、`sink.delayed(...)` 在此消费本讲造出的诊断对象。 |

本讲的所有源码精读都以 `src/diag.rs` 为主，另外两个文件只取片段做印证。

---

## 4. 核心概念与源码讲解

### 4.1 诊断数据模型：Severity、SourceDiagnostic 与三种 Result

#### 4.1.1 概念说明

Rust 标准库给了我们一个 `Result<T, E>`，错误类型 `E` 由你自己决定。Typst 围绕「源码编译」这个场景，预定义了三种错误载体，分别对应「我已知位置」「我暂不知位置」「我暂不知位置但想带提示」三种情况：

| 结果类型别名 | 错误载体 | 含义 |
|--------------|----------|------|
| `SourceResult<T>` | `EcoVec<SourceDiagnostic>` | 已带源码位置的诊断，**可以一次携带多条**。 |
| `StrResult<T>` | `EcoString` | 只有一段错误字符串，尚未带位置。 |
| `HintedStrResult<T>` | `HintedString` | 字符串错误 + 若干提示，尚未带位置。 |

为什么要分这么细？核心动机是**职责分层**：底层函数（纯数据解析、算术）只管把「出了什么事」说清楚，用一个轻量的 `EcoString` 即可；而「这件事发生在用户源码哪里」属于位置上下文，应由更了解调用点的上层补上。`SourceResult` 则是「终点格式」——只有它才能被 `Engine`/`Sink` 收纳并最终展示给用户。

注意 `SourceResult` 的错误是 `EcoVec<SourceDiagnostic>` 而不是单个 `SourceDiagnostic`。这是因为一次编译可以同时产生多个错误（例如批量处理一个数组，每个元素各报一个错），用向量把它们一起带回去，比一遇到错就 `return` 更友好。

#### 4.1.2 核心流程

一条诊断从「被发现」到「被展示」的旅程：

```text
某底层操作发现异常
        │  产出
        ▼
   StrResult / HintedStrResult        （只有消息，可能带提示，无 span）
        │  上层调用点有 span，调用 .at(span)
        ▼
   SourceResult (EcoVec<SourceDiagnostic>)   （消息 + span + 提示）
        │  途中可能再 .trace(...) 追加调用踪迹
        ▼
   被 Engine.sink 收纳（错误进 delayed/直接抛出；警告进 warnings）
        │
        ▼
   最终渲染给用户（CLI / IDE 画下划线、列提示）
```

一个 `SourceDiagnostic` 内部包含五个字段：严重级别（`severity`）、源码区间（`span`）、消息（`message`）、调用踪迹（`trace`）、提示列表（`hints`）。

#### 4.1.3 源码精读

先看三种结果类型别名。它们都只是一行 `type`，但定义了全 crate 的「错误词汇」：

- `src/diag.rs:208-210` —— `SourceResult`，错误载体是 `EcoVec<SourceDiagnostic>`，注释明确「推荐用 `bail!` 宏来创建」。
- `src/diag.rs:487-489` —— `StrResult`，错误载体是单个 `EcoString`。
- `src/diag.rs:507-509` —— `HintedStrResult`，错误载体是 `HintedString`（消息 + 提示的复合体）。

再看诊断的核心数据结构 `SourceDiagnostic` 与它的严重级别：

[src/diag.rs:303-321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L303-L321) 定义了 `SourceDiagnostic` 的五个字段：`severity`（严重级别）、`span`（一个 `DiagSpan`，既能表示 Typst 源码区间，也能表示外部文件的字节区间）、`message`（消息）、`trace`（`EcoVec<Spanned<Tracepoint>>` 调用踪迹）、`hints`（提示列表，且提示本身可以各自带 `span`，用于「画在另一段代码上」）。

[src/diag.rs:324-330](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L324-L330) 定义 `Severity` 枚举，只有两个变体 `Error`（致命）与 `Warning`（非致命）。同一个 `SourceDiagnostic` 结构既能装错误也能装警告，靠这个字段区分。

`SourceDiagnostic` 提供了两个构造器：

[src/diag.rs:334-353](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L334-L353) `SourceDiagnostic::error` 和 `::warning`。两者结构完全相同，只是 `severity` 不同，`trace`/`hints` 初始化为空——踪迹和提示留到后面按需追加。

最后看 `HintedString` 的内部表示，它解释了「为什么 `HintedStrResult` 比 `SourceResult` 更轻量」：

[src/diag.rs:519-520](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L519-L520) `HintedString` 内部就是一个 `EcoVec<EcoString>`。看它的文档注释（`src/diag.rs:511-518`）就知道约定：**第一个元素是消息，其余元素是提示**。这样把「消息 + 提示」压进同一个向量，是为了减小 `HintedString` 的体积——它最终只是 `Result` 的错误分支，要尽量小。`message()` 取 `first()`，`hints()` 取 `get(1..)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种结果类型别名对应的真实 Rust 类型。

1. 打开 [src/diag.rs:208-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L208-L210)，分别跳到 `StrResult`（L489）和 `HintedStrResult`（L509）。
2. 在纸上画一张映射表：左列是三个别名，右列填 `Result<T, ?>` 中 `?` 的类型。
3. 思考：如果某函数签名是 `fn foo() -> StrResult<()>`，函数体内能不能直接 `return Err(eco_vec![...])`？

**预期结果**：`SourceResult` → `EcoVec<SourceDiagnostic>`；`StrResult` → `EcoString`；`HintedStrResult` → `HintedString`。第三问答案是不能——类型不匹配，`StrResult` 的错误必须是单个 `EcoString`，想要带 `span` 或多条错误就得先 `.at(span)` 升级成 `SourceResult`（见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SourceResult` 的错误用 `EcoVec<SourceDiagnostic>` 而不是单个 `SourceDiagnostic`？

> **参考答案**：一次编译可能同时发现多个独立错误（例如遍历数组时每个元素各报一处）。用向量可以把它们一次性带回去，让用户一次看到全部问题，而不是修一个再编译一次才发现下一个。

**练习 2**：`SourceDiagnostic` 同时能表示错误和警告，靠哪个字段区分？两个构造器 `::error` 和 `::warning` 的差别是什么？

> **参考答案**：靠 `severity: Severity` 字段（`Error` / `Warning`）。两个构造器代码结构完全一致，唯一差别就是写入的 `severity` 值不同。

---

### 4.2 三个宏：bail! / error! / warning!

#### 4.2.1 概念说明

`diag.rs` 提供了三个声明宏来「快速造错误」，按使用频率排序：

- `bail!` —— **early-return** 宏。最常见的用法，写完立刻 `return Err(...)`。它本身只是个分发器，内部转交给 `error!`。
- `error!` —— **构造** 宏。产出一个错误对象（`EcoString` / `HintedString` / `SourceDiagnostic`，取决于写法），但不 `return`，方便你继续操作它（比如先记日志再返回）。
- `warning!` —— 构造一个 `SourceDiagnostic` 且 `severity = Warning`。警告不会自动进任何地方，需要你手动 `engine.sink.warn(warning!(...))`。

这三个宏都有「带 `span`」和「不带 `span`」两种写法，并且都可以追加若干 `hint:`。关键是：**带不带 `span` 决定了产出物的类型**。

#### 4.2.2 核心流程

`bail!` 的三个分支（按声明式宏的匹配顺序）：

```text
bail!(...) 的调用
   │
   ├─ ① bail!("fmt {}", x)          匹配「无 span、仅格式串」分支
   │        → return Err(error!("fmt {}", x))
   │        → error! 无 span 分支返回 EcoString → 喂给 StrResult/HintedStrResult
   │
   ├─ ② bail!(single_expr)          匹配「单个表达式」分支（如 bail!(diag)）
   │        → return Err(eco_vec![single_expr])
   │        → 喂给 SourceResult
   │
   └─ ③ bail!(span, "fmt {}", x)    匹配「剩余 token」分支（首参是 span）
            → return Err(eco_vec![error!(span, "fmt {}", x)])
            → error! 带 span 分支返回 SourceDiagnostic → 包进 eco_vec → 喂给 SourceResult
```

所以：**带 `span` → `SourceResult`（`EcoVec<SourceDiagnostic>`）；不带 `span` → `StrResult`（`EcoString`）或 `HintedStrResult`（`HintedString`）**。最终落到哪个类型，由所在函数的返回签名决定。

`error!` 的产出物随写法变化：

- `error!("fmt")` → `EcoString`（等价于 `eco_format!`）。
- `error!("fmt"; hint: "...")` → `HintedString`。
- `error!(span, "fmt")` → `SourceDiagnostic`（severity = Error）。
- `warning!(span, "fmt")` → `SourceDiagnostic`（severity = Warning），结构同 `error!`，只是构造器换成 `::warning`。

#### 4.2.3 源码精读

先看 `bail!` 宏本体。注意它真正叫 `__bail`（改名的原因见下文）：

[src/diag.rs:53-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L53-L76) 定义了 `macro_rules! __bail` 的三个分支：

- 第 56–65 行是「无 `span`」分支：只匹配 `$fmt:literal` 开头，转发给 `error!`，产出 `StrResult`/`HintedStrResult` 的错误。
- 第 68–70 行是「单个表达式」分支 `($error:expr)`：直接 `return Err(eco_vec![$error])`，用于你手里已经有一个 `SourceDiagnostic` 的情况，对应 `SourceResult`。
- 第 73–75 行是兜底分支 `($($tts:tt)*)`：把首参当 `span`，转给 `error!(span, ...)` 造出 `SourceDiagnostic`，再包进 `eco_vec!`，对应 `SourceResult`。

> 关键区别就在第 69 行与第 74 行：69 行直接 `eco_vec![$error]`（已有诊断），74 行 `eco_vec![error!(...)]`（现造诊断，带 `span`）。而 60–64 行不带 `span` 的分支**不**包 `eco_vec!`，所以产出的只是裸 `EcoString`/`HintedString`。

再看 `error!` 宏，它才是真正「造对象」的地方：

[src/diag.rs:100-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L100-L143) 定义 `macro_rules! __error`，含四个分支：

- 第 102–104 行：纯格式串 → `eco_format!(...).into()` → `EcoString`。
- 第 107–115 行：格式串 + `hint:` → `HintedString::new(...).with_hint(...)`，有多少个 `hint:` 就链式 `.with_hint` 多少次。
- 第 119–132 行：带 `span` → 造 `SourceDiagnostic::error(span, ...)`，再用递归宏（137–142 行）逐个加提示。注意 130 行 `$(...;)*` 把每条 `hint`（可带 `[span]`）都展开成一次 `error!(hint...: err, ...)` 调用。
- 第 137–142 行：内部递归宏，专门处理「提示带不带 `span`」两种情况，分别调 `hint()` 与 `spanned_hint()`。注释（134–136 行）解释了为什么必须用递归宏：递归宏必须生成完整表达式，无法用 `.with_hint()` 这种语句。

`warning!` 宏与 `error!` 的「带 `span`」分支几乎逐行相同：

[src/diag.rs:167-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L167-L182) `macro_rules! __warning`，唯一差别是构造器用 `SourceDiagnostic::warning(...)`（severity = Warning）。它复用了 `error!` 的递归提示宏（179 行 `error!(hint...: warning, ...)`），所以提示逻辑完全一致。

**为什么宏都叫 `__xxx` 而不是 `bail`/`error`/`warning`？** 这是一个有意思的 Rust 限制：

[src/diag.rs:200-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L200-L206) 用 `pub use { __bail as bail, __error as error, __warning as warning }` 把它们改名重导出。源码上方的长注释（184–199 行）解释：Rust 的 `macro_rules!` 宏一旦 `#[macro_export]`，**只能**出现在 crate 根，且必须用真实名字（`__bail` 等）导出，没法像普通 item 那样「定义在子模块里」。Typst 的折中是：真名带 `__` 前缀并加 `#[doc(hidden)]` 在根处隐藏，再在本模块用 `pub use ... as ...` 以干净名字 `bail`/`error`/`warning` 重新暴露，并把文档挂回本模块。这样既满足 Rust 限制，又让宏看起来「属于 `diag` 模块」。

#### 4.2.4 代码实践

**实践目标**：亲手核对 `bail!` 三个分支的产出类型，并找到真实的「带 span / 不带 span」范例。

1. 打开 [src/diag.rs:53-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L53-L76)，对照本讲 4.2.2 的流程图，把每个分支的 `return Err(...)` 内层类型抄下来。
2. 看一个「不带 span」的真实范例：[src/foundations/mod.rs:170-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/mod.rs#L170-L185) 的 `assert` 函数。注意它的签名是 `-> StrResult<NoneValue>`，函数体里 `bail!("assertion failed: {message}")`（179 行）不带 `span`，正好产出 `EcoString`。
3. 看一个「带 span」的真实范例：[src/foundations/calc.rs:112-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/calc.rs#L112-L122) 的 `pow` 函数。它的签名是 `-> SourceResult<DecNum>`，函数体里 `bail!(span, "zero to the power of zero is undefined")`（114 行）带 `span`，产出 `EcoVec<SourceDiagnostic>`。

**需要观察的现象**：同样是 `bail!`，在 `assert` 里返回 `StrResult`，在 `pow` 里返回 `SourceResult`——差异完全来自「首参是不是 `span`」以及「函数签名要求什么」。

**预期结果**：

| 分支 | 宏写法 | 内层类型 | 适合的结果类型 |
|------|--------|----------|----------------|
| ① 无 span | `bail!("fmt")` | `EcoString`（或 `HintedString`） | `StrResult` / `HintedStrResult` |
| ② 单表达式 | `bail!(diag)` | `eco_vec![diag]` | `SourceResult` |
| ③ 带 span | `bail!(span, "fmt")` | `eco_vec![SourceDiagnostic]` | `SourceResult` |

#### 4.2.5 小练习与答案

**练习 1**：为什么 Typst 不直接提供 `bail!` 这一个宏就好，还要单独留 `error!`？

> **参考答案**：`bail!` 是 early-return，立刻退出函数；但有时你想先拿到错误对象做点别的（例如包装、记录、有条件返回），就需要 `error!` 只「构造」不「返回」。`bail!` 内部其实就是 `return Err(error!(...))`，`error!` 是更原始的能力。

**练习 2**：阅读 `error!` 的第 119–132 行分支。如果一条错误想带两个提示，其中一个提示还要画在另一段代码上，宏该怎么写？

> **参考答案**：写成 `error!(span, "msg"; hint: "通用提示"; hint[other_span]: "针对另一段代码的提示")`。不带位置的提示用 `hint: "..."`（调 `hint()`），带位置的提示用 `hint[span]: "..."`（调 `spanned_hint()`），二者由 137–142 行的递归宏分别处理。

---

### 4.3 Hint：给错误加提示

#### 4.3.1 概念说明

光告诉用户「出错了」往往不够，还要告诉他「怎么改」。Typst 用 `Hint` trait 给错误追加提示文字。提示分两种归宿：

- 在没有 `span` 的 `StrResult`/`HintedStrResult` 阶段，提示会先挂到 `HintedString` 上（成为它内部向量的后续元素）。
- 等升级成 `SourceDiagnostic` 后，提示挂在 `hints` 字段，每条提示**还可以各自带一个 `span`**，渲染时画在对应的另一段代码上。

`Hint` trait 是一个「链式升级」的入口：你在一个 `Result` 上调用 `.hint("...")`，它会把错误载体从 `EcoString` 升级成 `HintedString`，并把提示挂上去。

#### 4.3.2 核心流程

```text
   某操作返回 StrResult（错误是 EcoString）
        │  .hint("建议")
        ▼
   HintedStrResult（错误是 HintedString，含 1 条消息 + N 条提示）
        │  还想加更多？
        ▼
   再次 .hint("...")  → HintedStrResult::hint 追加到同一 HintedString
        │  到了有 span 的地方 .at(span)
        ▼
   SourceResult，提示随之迁移到 SourceDiagnostic.hints
```

注意 `Hint` 有两个 impl：一个 blanket impl 覆盖所有「错误能转成 `EcoString`」的 `Result`（即 `StrResult`），它把载体「升级」成 `HintedString`；另一个专属于 `HintedStrResult`，只是「往已有的 `HintedString` 里再追加」。

#### 4.3.3 源码精读

`Hint` trait 与两个实现：

[src/diag.rs:577-599](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L577-L599) 定义 `Hint` trait，只有一个方法 `fn hint(self, hint) -> HintedStrResult<T>`。第 583–590 行的 blanket impl 作用于所有 `Result<T, S> where S: Into<EcoString>`——也就是 `StrResult`，它把字符串消息包成 `HintedString::new(...).with_hint(...)`（588 行），完成「从 `EcoString` 到 `HintedString` 的升级」。第 592–599 行的专属于 `HintedStrResult` 的 impl 则只是 `error.hint(...)` 往现有向量追加，不再重新包装。

`HintedString` 自身提供了 `hint`/`with_hint`/`with_hints` 等方法，见 [src/diag.rs:539-554](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L539-L554) 区域（`hint` 在 540–542 行，`with_hint` 在 545–548 行，`with_hints` 在 551–554 行），都是往内部 `EcoVec` 末尾 `push`/`extend`。

提示从 `HintedString` 迁移到 `SourceDiagnostic` 的过程藏在 `At` 的专属 impl 里（见 4.4.3）：`.at(span)` 时会把 `HintedString` 的第一个元素当消息，其余元素整体作为 `hints` 传给 `with_hints`。

一个真实且精心设计的「带提示」范例是编译器内部错误（`internal_error`）：

[src/diag.rs:1108-1125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L1108-L1125) 用 `error!("internal error: {msg} ..."; hint: "please report this as a bug")` 造一个带「请提交 bug」提示的错误，并在 `debug_assertions` 下再追加一条编译器回溯提示。这展示了 `error!` 的「带提示」分支在实战中的用法。

#### 4.3.4 代码实践

**实践目标**：理解「提示」如何在两个阶段流动。

1. 阅读 [src/diag.rs:583-590](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L583-L590) 的 blanket impl，确认 `.hint("x")` 会把 `EcoString` 包成 `HintedString`（升级）。
2. 阅读 [src/diag.rs:566-575](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L566-L575) 的 `At for HintedStrResult`，看清 `.at(span)` 时 `components.next()` 取消息、其余 `.with_hints(components)` 当提示（571 行）。
3. 思考：如果一条 `HintedStrResult` 有 1 条消息 + 2 条提示，`.at(span)` 后 `SourceDiagnostic.hints` 有几条？

**预期结果**：2 条。消息进 `message`，2 条提示进 `hints`。

> 待本地验证：若你想亲眼看到提示渲染效果，可在 Typst 源码里故意触发一个带 hint 的错误（例如某些类型转换错误），用 `typst compile` 编译并观察 CLI 输出中 `hint:` 前缀的行。

#### 4.3.5 小练习与答案

**练习 1**：`Hint` 的两个 impl 分别处理什么？为什么需要两个？

> **参考答案**：blanket impl 处理 `StrResult`（错误是 `EcoString`），负责「升级」成 `HintedString`；专属 impl 处理 `HintedStrResult`，只负责「追加」。需要两个是因为第一次加提示要从 `EcoString` 跨到 `HintedString`（类型变了），后续加提示才是同类型追加。

**练习 2**：提示（hint）和消息（message）在 `SourceDiagnostic` 里是同一个字段吗？

> **参考答案**：不是。`message` 是主消息（`EcoString`），`hints` 是独立字段（`EcoVec<Spanned<EcoString, DiagSpan>>`），且每条提示还能各自带 `span`。在 `HintedString` 阶段两者共用一个 `EcoVec`（首元素=消息，其余=提示），到了 `SourceDiagnostic` 才分开存放。

---

### 4.4 At：把字符串错误升级为带位置的诊断

#### 4.4.1 概念说明

`At` trait 是 4.1 节那条「两段式机制」的关键一环：**当底层函数用 `StrResult`/`HintedStrResult` 报了错，上层调用点拿到 `span` 后，用 `.at(span)` 把它升级成 `SourceResult`**。`.at(span)` 的语义是「把这个错误归属于这个源码区间」。

这正是 Typst 能做到「底层函数保持简单（不带 span）、错误却仍能精确定位」的原因：底层只管报 `EcoString`，位置由懂调用上下文的上层补。

#### 4.4.2 核心流程

```text
   底层：some_str_result()  →  Result<T, EcoString>      （无 span）
        │  在调用点：.at(span)
        ▼
   上层：SourceResult<T>  →  Err(eco_vec![SourceDiagnostic::error(span, message)])
```

`At` 同样有两个 impl：

- blanket impl 作用于「错误能转 `EcoString`」的 `Result`（覆盖 `StrResult`），`.at(span)` 时丢弃提示（因为 `EcoString` 本就没有提示），只把消息搬到 `SourceDiagnostic::error(span, message)`。
- 专属 impl 作用于 `HintedStrResult`，`.at(span)` 时**保留提示**——消息进 `message`，提示进 `hints`。

#### 4.4.3 源码精读

`At` trait 与两个实现：

[src/diag.rs:491-505](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L491-L505) 定义 `At` trait（方法 `fn at(self, span: Span) -> SourceResult<T>`）和它的 blanket impl（498–505 行）。注意泛型约束 `S: Into<EcoString>`：任何「错误类型能转成 `EcoString`」的 `Result` 都能用 `.at(span)`。第 503 行 `self.map_err(|message| eco_vec![SourceDiagnostic::error(span, message)])` 一句话完成「补位置 + 包向量」。

[src/diag.rs:566-575](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L566-L575) 是 `At for HintedStrResult` 的专属 impl。它比 blanket 版多一步：先 `components.next()` 取出消息造 `SourceDiagnostic::error(span, message)`，再 `.with_hints(components)`（571 行）把剩余元素当提示挂上。这样 `HintedStrResult` 升级时提示不会丢失。

**真实范例**：`calc.pow` 函数里的 `.at(span)`。

[src/foundations/calc.rs:125-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/calc.rs#L125-L137) 展示了典型的 `.at(span)` 链：

```rust
(DecNum::Int(a), Num::Int(b)) if b >= 0 => a
    .checked_pow(b as u32)
    .map(DecNum::Int)
    .ok_or_else(too_large)   // ← 这里产出 StrResult：Err(EcoString)
    .at(span),               // ← 升级成 SourceResult，补上调用点的 span
```

`checked_pow` 返回 `Option`，`.ok_or_else(too_large)` 把 `None` 变成 `Err(EcoString)`（`too_large` 返回一个 `EcoString`），最后 `.at(span)` 把这条「结果太大」的错误归属到用户写 `#calc.pow(...)` 的那行源码。整条链优雅地完成了「纯算术 → 字符串错误 → 带位置诊断」的升级。同函数里 `return Err(cant_apply_to_decimal_and_float()).at(span)`（136 行）是另一个同型用法。

> 还有一处值得注意的细节：`At` blanket impl 里的 `eco_vec!` 来自 `diag.rs` 顶部第 8 行的 `pub use ecow::{... eco_vec}` 重导出。注释（3–6 行）解释：这样宏才能写 `$crate::diag::eco_vec!`，让下游 crate 不必直接依赖 `ecow` 也能用这些宏。

#### 4.4.4 代码实践

**实践目标**：追踪一条完整的「`StrResult` → `.at(span)` → `SourceResult`」链路，亲手解释每一步。

1. 打开 [src/foundations/calc.rs:125-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/calc.rs#L125-L137)。
2. 找到 `too_large` 的定义（在 `calc.rs` 中搜索 `too_large` 或 `fn too_large`），确认它返回的是 `EcoString`。
3. 解释：`.ok_or_else(too_large)` 这一步之后，表达式的类型是什么？`.at(span)` 之后又变成了什么？`span` 这个变量是从哪里来的（提示：看 `pow` 函数签名里的 `span: Span` 参数，[src/foundations/calc.rs:103-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/calc.rs#L103-L105)）？

**需要观察的现象**：`span` 是 `#[func]` 宏自动注入的「调用点 span」参数（任何标了 `#[func]` 且需要位置信息的函数都能拿到它），它代表用户源码里这次函数调用的区间。`.at(span)` 就是把错误「钉」到这个区间。

**预期结果**：`.ok_or_else(too_large)` 后是 `Result<DecNum, EcoString>`（即 `StrResult<DecNum>`）；`.at(span)` 后变成 `Result<DecNum, EcoVec<SourceDiagnostic>>`（即 `SourceResult<DecNum>`），与 `pow` 的返回签名一致。

> 待本地验证：`too_large` 的确切定义行号请在本机 `Grep` 确认；它在 `calc.rs` 内，返回一个描述「数值过大」的 `EcoString`。

#### 4.4.5 小练习与答案

**练习 1**：`At` 的 blanket impl 和 `At for HintedStrResult` 专属 impl，产出有何不同？

> **参考答案**：blanket 版只搬消息（`SourceDiagnostic::error(span, message)`），提示丢失；专属版额外 `.with_hints(...)` 保留提示。所以带提示的错误要走 `HintedStrResult` 路径，升级时提示才不会丢。

**练习 2**：为什么 `pow` 函数内部不直接 `bail!(span, "...")`，而要先 `ok_or_else(...)` 再 `.at(span)`？

> **参考答案**：因为溢出是 `Option`（`checked_pow` 的 `None`），需要先用 `ok_or_else` 把 `None` 转成「字符串错误」，才有东西可以 `.at(span)`。这里错误源头是 Rust 的 `Option`，不是已经组织好的诊断，所以走「`StrResult` + `.at`」更自然；`bail!` 更适合「我直接判定要报错」的场景。

---

## 5. 综合实践

把本讲四块知识串起来：还原一条诊断「从诞生到带位置」的完整路径。

**任务**：选择 `calc.pow`（[src/foundations/calc.rs:102-150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/calc.rs#L102-L150)）作为研究对象，完成下面的「诊断生命史」表格。

| 阶段 | 代码位置 | 当时错误对象的类型 | 用到的本讲机制 |
|------|----------|--------------------|----------------|
| 1. 直接报错（带 span） | `bail!(span, "zero to the power of zero is undefined")`（L114） | ? | `bail!` 分支 ③ |
| 2. 溢出，先转字符串错误 | `.ok_or_else(too_large)`（L129） | ? | ? |
| 3. 补位置升级 | `.at(span)`（L130） | ? | `At` trait |
| 4. 不可混用类型，直接带 span 返回 | `Err(...).at(span)`（L136） | ? | ? |

操作步骤：

1. 逐行填出「当时错误对象的类型」（提示：阶段 1 是 `EcoVec<SourceDiagnostic>`；阶段 2 是 `EcoString`；阶段 3 是 `EcoVec<SourceDiagnostic>`）。
2. 用一句话解释：为什么 `pow` 的返回类型必须声明为 `SourceResult<DecNum>`？（因为阶段 1、3、4 最终都产出 `SourceResult`，函数只能有一个返回类型。）
3. 进阶：找一个用了 `HintedStrResult` + `.hint()` + `.at(span)` 三件套的函数（可在 `src/foundations/cast.rs` 或 `src/loading/` 里搜索 `.at(span)` 并向上看是否先 `.hint`），记录它的文件与行号，说明它的提示是如何一路带到 `SourceDiagnostic.hints` 的。

> 这是一个「源码阅读型实践」，无需运行编译。重点是讲清楚每个阶段错误对象的类型变迁与所用的 trait/宏。

---

## 6. 本讲小结

- Typst 围绕编译场景定义了三种结果类型：`SourceResult`（`EcoVec<SourceDiagnostic>`，带位置、可多条）、`StrResult`（`EcoString`，无位置）、`HintedStrResult`（`HintedString`，无位置但带提示）。
- `bail!` 是 early-return 分发宏，分三个分支：无 `span` 走 `error!` 产出 `EcoString`/`HintedString`（对应 `StrResult`/`HintedStrResult`）；带 `span` 或单表达式产出 `eco_vec![...]`（对应 `SourceResult`）。**带不带 `span` 决定产出类型**。
- `error!`/`warning!` 是构造宏，前者 severity=Error、后者 severity=Warning；二者都支持 `hint:` 与 `hint[span]:` 追加提示，提示逻辑由递归宏复用。
- 三个宏真名都带 `__`（`__bail` 等），因 Rust 限制只能在 crate 根导出，再用 `pub use ... as ...` 改名重暴露，让它们看起来「属于 `diag` 模块」。
- `Hint` trait 在 `StrResult` 上 `.hint()` 会把错误从 `EcoString` 升级成 `HintedString`，提示随之累积；升级到 `SourceDiagnostic` 后，提示落在独立的 `hints` 字段，每条还能各自带 `span`。
- `At` trait 的 `.at(span)` 是「补位置」的桥梁，把无位置的字符串错误升级为 `SourceResult`；其中 `At for HintedStrResult` 会保留提示，是 `Hint` 与 `At` 的衔接点。

---

## 7. 下一步学习建议

本讲讲清了「诊断对象怎么被造出来」。接下来：

- **诊断怎么被收纳、过滤与展示**：回到 [src/engine.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs) 看 `Sink` 的四只桶（`delayed`/`warnings`/`introspections`/`values`），理解本讲造出的 `SourceDiagnostic` 如何进入 `sink.warn(...)`、`sink.delayed(...)`。这呼应 [u5-l2](u5-l2-engine-route-sink.md) 的延迟错误机制。
- **调用踪迹 trace**：本讲只点到 `SourceDiagnostic.trace` 字段。建议阅读 `src/diag.rs` 的 `Tracepoint`（[L432-L441](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L432-L441)）与 `Trace` trait（[L455-L485](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L455-L485)），看「while calling ...」这种栈式踪迹是如何沿调用链拼出来的。
- **文件相关错误**：`diag.rs` 后半段还有 `FileError`/`PackageError`/`LoadError` 与 `LoadedWithin` trait，它们是数据加载（u11-l1）会用到的错误类型，可在学到 loading 单元时回看。
- **实战**：在阅读任意一个 `#[func]`（如 `src/model/` 或 `src/visualize/` 下的元素构造函数）时，刻意留意它用的是 `bail!(span, ...)`、`bail!("...")` 还是 `.at(span)`，并用本讲的规则推断它的返回类型——这是巩固本讲最快的方式。
