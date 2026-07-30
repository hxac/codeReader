# 错误处理、诊断与提示体系

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 Typst 解释器里「字符串错误」与「源诊断」这两个世界的区别，以及 `SourceResult` / `HintedStrResult` / `StrResult` 三种结果类型各用在什么地方。
- 解释 `At` trait 如何把一个「没有位置信息的字符串错误」升级成一个「带 span、带 hint 的源诊断」。
- 熟练使用 `bail!` / `error!` / `warning!` 三个宏的几种调用形式（带 span / 不带 span / 带 hint / 带定位 hint），理解它们为何被 `#[doc(hidden)]` 重导出。
- 读懂 `SourceDiagnostic` 的五字段结构，特别是 `span: DiagSpan`、`hints`、`trace` 各自承载什么；并理解 `DiagSpan` + `SubRange` 如何实现「在整段数字里只高亮那几个非法字符」的精确定位。
- 解释 `Tracepoint` 与 `Trace` trait 如何为深层调用产生的错误叠加「while calling …」的来源回溯。
- 能从 `int_literal_error` / `find_bad_digits` / `hint_if_shadowed_std` 等样板中提炼出「错误信息 + 精确定位 + 修复提示」三要素，并在阅读或扩展 typst-eval 时照此模式写诊断。

本讲是「深入机制」单元的开篇，把前面五单元里反复出现却一直没展开的 `?`、`.at(span)`、`bail!`、`hint:` 统一讲透。**一句话主线：typst-eval 在求值过程中产生的所有失败，最终都要变成「一条让用户能看懂、能定位、能照着改」的诊断。**

## 2. 前置知识

本讲承接 **[u1-l4（Eval trait 与 Vm 虚拟机）](u1-l1-eval-trait-vm.md)**，请确认你已经理解：

### 2.1 `Eval` 的返回类型是 `SourceResult`

回顾贯穿全 crate 的求值签名：

```rust
fn eval(self, vm: &mut Vm) -> SourceResult<Self::Output>;
```

这里的 `SourceResult<Output>` 就是本讲的主角之一。它是一个 `Result`，其 `Err` 分支携带的不是一个字符串，而是一整个**诊断向量**。前面几讲里你无数次看到 `expr.eval(vm)?`、`...at(span)?`，那些 `?` 和 `.at(...)` 正是本讲要拆解的错误处理管道。

### 2.2 `Span` 是「节点编号」而非字节区间

在 typst-syntax 里，`Span` 是给每个语法节点分配的稳定编号（见 u1-l4 的 `trace_at`）。诊断需要把错误贴到源码上，最朴素的做法是给错误配一个 `Span`。但有时一个 `Span` 覆盖的范围太大（例如一整个整数 `0x1Z`），我们想**只高亮其中的 `Z`**——这就需要本讲后半段的 `DiagSpan` + `SubRange`。

### 2.3 typst-eval 不定义诊断类型

重要的事实：**`SourceDiagnostic`、`bail!`、`At` 这些都定义在 `typst-library::diag` 里，`Span`/`DiagSpan`/`SubRange` 定义在 `typst-syntax` 里**。typst-eval 只是它们的**消费者**——本讲会频繁跳到这两个上游 crate 去看定义，再回到 typst-eval 看用法。

## 3. 本讲源码地图

本讲涉及 typst-eval 的 4 个文件，以及 2 个上游定义文件：

| 文件 | 角色 | 本讲用到的核心内容 |
|------|------|--------------------|
| [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) | 诊断样板集散地 | `int_literal_error` / `find_bad_digits`、`warn_for_discarded_content`、`forbidden` 闭包 |
| [src/vm.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs) | 错误增强器 | `hint_if_shadowed_std`（给已有错误追加 hint） |
| [src/ops.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs) | 错误改写 | `overflowing_int_negation_error`、`apply_binary` 里的 `.at` |
| [src/binding.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs) | 错误样板 | `wrong_number_of_elements` |
| [src/call.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs) | 错误定位策略 | `disallowed_field_call_error`、`Args` 用 `Span::detached` 上交定位权 |
| ../typst-library/src/diag.rs | 上游：诊断核心 | `SourceResult`/`SourceDiagnostic`/`At`/`Hint`/`Trace`、三个宏 |
| ../typst-syntax/src/span.rs | 上游：精确定位 | `DiagSpan` / `SubRange` |

> 说明：表中带 `../` 的两个文件位于 typst-eval 的兄弟 crate，相对路径在 GitHub 上可正常解析。

一条诊断从「产生」到「落地」的数据流：

```text
某处求值失败
   │  产生「字符串错误」HintedStrResult（无位置）
   │  或 直接构造 SourceDiagnostic（带位置）
   ▼
.at(span)        ← At trait：补上 / 校准位置，升级成 SourceResult
   ▼
.map_err(增强)   ← hint_if_shadowed_std 等：按需追加修复 hint
   ▼
.trace(world, point, span)  ← Trace trait：叠加 "while calling …" 回溯
   ▼
Err(EcoVec<SourceDiagnostic>)  ← 沿调用栈用 ? 冒泡
   ▼
eval() / eval_string()        ← 顶层收集，连同 sink 里的 warning 一起交给 CLI/IDE 渲染
```

本讲 4.1～4.5 正是按这条流水线自上而下拆开。

## 4. 核心概念与源码讲解

### 4.1 错误的两个世界：字符串错误 vs 源诊断

#### 4.1.1 概念说明

Typst 的错误处理刻意分成了「两个世界」：

- **字符串世界**：函数内部只关心「这件事成不成」，不关心位置。比如 `ops::add(int, str)` 失败时返回的是一个 `EcoString`（"cannot add this"）。这类结果类型是 `StrResult<T>`（错误是裸字符串）或 `HintedStrResult<T>`（错误是「字符串 + 若干 hint」）。它们**轻量、无位置、可被任意上下文复用**。
- **源诊断世界**：最终面向用户的错误必须指向源码某个位置。类型是 `SourceDiagnostic`，它带 `span`、`message`、`hints`、`trace`。承载多个诊断的结果类型是 `SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>`。

为什么分两层？因为很多底层操作（类型转换、算术）不知道自己被谁调用、贴在哪个 span 上；让它们只产出「纯错误信息」，再由**调用方**在 `.at(span)` 时贴上具体位置，职责更清晰，也方便复用。

#### 4.1.2 核心流程

三个结果类型与它们之间的转换关系：

```text
StrResult<T>          = Result<T, EcoString>            // 裸字符串错误
HintedStrResult<T>    = Result<T, HintedString>         // 字符串 + hint 列表
SourceResult<T>       = Result<T, EcoVec<SourceDiagnostic>> // 最终带位置的诊断

转换靠 At trait：
  str_result.at(span)         → SourceResult   （实现 1）
  hinted_str_result.at(span)  → SourceResult   （实现 2，会带上 hint）
```

`.at(span)` 的语义是「把这个无位置的错误，贴上 `span` 升级成源诊断」。它本质是个 `map_err`。

#### 4.1.3 源码精读

先看三个类型别名（[../typst-library/src/diag.rs:489-509](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L489-L509)）：

```rust
pub type SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>;
pub type StrResult<T> = Result<T, EcoString>;
pub type HintedStrResult<T> = Result<T, HintedString>;
```

注意 `SourceResult` 的错误是 `EcoVec<SourceDiagnostic>`——**一个向量**。这意味着一次求值可以同时回报多个错误（比如字典里多个键都非法），这是 Typst 诊断体系的关键能力。

再看 `At` trait 的两个实现（[../typst-library/src/diag.rs:493-505](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L493-L505) 与 [L566-L575](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L566-L575)）：

```rust
pub trait At<T> {
    fn at(self, span: Span) -> SourceResult<T>;
}

// 实现 1：任何 Err 能 Into<EcoString> 的 Result（含 StrResult）
impl<T, S: Into<EcoString>> At<T> for Result<T, S> {
    fn at(self, span: Span) -> SourceResult<T> {
        self.map_err(|message| eco_vec![SourceDiagnostic::error(span, message)])
    }
}

// 实现 2：HintedStrResult —— 多了「把 hint 也搬过去」
impl<T> At<T> for HintedStrResult<T> {
    fn at(self, span: Span) -> SourceResult<T> {
        self.map_err(|err| {
            let mut components = err.0.into_iter();
            let message = components.next().unwrap();
            let diag = SourceDiagnostic::error(span, message).with_hints(components);
            eco_vec![diag]
        })
    }
}
```

这就是为什么 typst-eval 里随处可见的 `cast::<bool>().at(span)?`、`ops::join(...).at(span)?` 能工作——`cast` / `ops::*` 返回的是 `HintedStrResult`，`.at(span)` 把它升级成 `SourceResult`，并保留底层提供的 hint。

来看一个真实用例。ops.rs 中二元运算的收尾（[src/ops.rs:69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L69)）：

```rust
op(lhs, rhs).at(binary.span())
```

`op` 是 `fn(Value, Value) -> HintedStrResult<Value>`（委托给 typst_library 的 `ops`，见 u3-l4），它只产生「字符串 + hint」；`.at(binary.span())` 贴上整条二元表达式的位置，变成 `SourceResult<Value>`。短短一行，完成了两个世界的桥接。

而 typst-eval 自己的求值函数签名一律用 `SourceResult`（[src/code.rs:4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L4) 的 import）：

```rust
use typst_library::diag::{At, SourceDiagnostic, SourceResult, bail, error, warning};
```

> 小结：底层 `ops`/`cast` 吐 `HintedStrResult`（字符串世界）；typst-eval 在每个调用点用 `.at(span)` 把它「拉进」`SourceResult`（源诊断世界）。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `.at(span)` 的「贴位置」效果。

1. 打开 [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs)，找到 `Ident::eval`（约 [L158-L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L158-L170)），注意 `vm.scopes.get(&self).at(span)?`：`scopes.get` 返回 `HintedStrResult`，`.at(span)` 贴上标识符位置。
2. 想象去掉 `.at(span)`：函数将无法编译，因为 `Ident::eval` 要求返回 `SourceResult`，而 `HintedStrResult` 类型不符——这正是 `At` 存在的意义。
3. **待本地验证**：若你本地能编译 typst，可临时把某处 `.at(span)` 改成 `.at(Span::detached())`，重新编译运行一段会触发该错误的 Typst 代码，观察 CLI 报错从「精确指向某标识符」变成「无法定位」。

**预期结果**：`.at(span)` 是「字符串错误 → 源诊断」的唯一闸门；去掉它（或换成 detached）会导致位置丢失。

#### 4.1.5 小练习与答案

**练习 1**：`SourceResult<T>` 的 `Err` 为什么是 `EcoVec<SourceDiagnostic>` 而不是单个 `SourceDiagnostic`？

> **答案**：因为一次求值可能同时发现多个独立错误（如字典里多个键类型错误），用向量能把它们一次性回报，而不是发现一个就停。

**练习 2**：下面两行都合法，区别是什么？
`result.at(span)?`  vs  `result.map_err(|e| eco_vec![SourceDiagnostic::error(span, e)])?`

> **答案**：功能上等价（当 `result` 是 `StrResult` 时）。但 `.at(span)` 还能正确处理 `HintedStrResult`——把底层 hint 一起搬过去；手写 `map_err` 会丢掉 hint。所以代码里一律用 `At`。

---

### 4.2 三个构造宏：bail! / error! / warning!

#### 4.2.1 概念说明

`bail!` / `error!` / `warning!` 是 typst-library 提供的声明式宏，是产生诊断的**首选入口**（文档明确说「The recommended way to create an error is with the `bail!`/`error!` macro」）：

- `error!(...)`：**构造**一个错误（`EcoString` / `HintedString` / `SourceDiagnostic`，取决于是否给 span）。
- `warning!(...)`：**构造**一个警告（`SourceDiagnostic`，severity = Warning）。警告不会中断求值，需手动 `engine.sink.warn(...)` 投递。
- `bail!(...)`：**构造并立即 return**。是 `error!` + `return Err(...)` 的语法糖。

三者都支持用分号追加 `hint:`，并且 hint 可以带自己的 span：`hint[span_expr]: "..."`。

#### 4.2.2 核心流程

宏的重载规则（以 `error!` 为例，`bail!`/`warning!` 同构）：

```text
error!("msg", args...)                       → EcoString          （无 span）
error!("msg"; hint: "..."; hint[s]: "...")   → HintedString       （无 span + hint）
error!(span, "msg", args...)                 → SourceDiagnostic   （有 span）
error!(span, "msg"; hint: "..."; hint[s]: "...") → SourceDiagnostic（有 span + 定位 hint）
```

`bail!` 多一条「直接返回已有错误」的规则：

```text
bail!(some_source_diagnostic)                → return Err(eco_vec![some_source_diagnostic])
```

#### 4.2.3 源码精读

宏本体在 [../typst-library/src/diag.rs:53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L53)（`bail!`）、[L100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L100)（`error!`）、[L167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L167)（`warning!`）。它们真实名字是 `__bail`/`__error`/`__warning`（因为 Rust 只能在 crate 根导出 `macro_rules`），通过 [L202-L206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L202-L206) 的 `pub use { __bail as bail, ... }` 重命名后再导出，并用 `#[doc(hidden)]` 隐藏原始名。注释（[L184-L199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L184-L199)）解释了这个「双下划线 + 重导出」的小技巧。

看 `bail!` 的三条规则（[L53-L76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L53-L76)），核心是：

```rust
// 无 span：转发给 error!，返回 StrResult/HintedStrResult
($fmt:literal $(, $arg)* $(; hint: ...)*) => {
    return Err($crate::diag::error!($fmt $(, $arg)* $(; hint: ...)*))
};
// 已有错误：直接包成 eco_vec 返回
($error:expr) => { return Err($crate::diag::eco_vec![$error]) };
// 有 span：转发给 error!，返回 SourceResult
($($tts:tt)*) => {
    return Err($crate::diag::eco_vec![$crate::diag::error!($($tts)*)])
};
```

现在回到 typst-eval 看真实用法。最常见的形式是「带 span + 多条 hint」，例如 flow.rs 里检测到死循环（[src/flow.rs:80-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L80-L83)）：

```rust
if i == 0 && is_invariant(condition.to_untyped()) && !can_diverge(body.to_untyped()) {
    bail!(condition.span(), "condition is always true");
} else if i >= MAX_ITERATIONS {
    bail!(self.span(), "loop seems to be infinite");
}
```

这里 `bail!(span, "msg")` 命中第三条规则，构造一个 `SourceDiagnostic` 并立即 `return Err(eco_vec![..])`。

「带定位 hint」的高级形式见 code.rs 的数组 spread 诊断（[src/code.rs:269-L275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L269-L275)）：

```rust
let fixed = self.to_untyped().full_text().replacen("(", "(: ", 1);
bail!(
    spread.span(), "cannot spread {} into array", v.ty();
    hint: "add a colon to create a dictionary instead: `{fixed}`";
)
```

注意 `;` 之后用 `hint: "..."` 追加修复提示。这种「错误信息 + 一行可照抄的修复」是 typst-eval 最典型的诊断写法。

`warning!` 的用法略不同——它只构造不投递，必须配合 `engine.sink.warn(...)`。见 vm.rs 里对 `is` 标识符的告警（[src/vm.rs:63-L70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L63-L70)）：

```rust
self.engine.sink.warn(warning!(
    var.span(),
    "`is` will likely become a keyword ...";
    hint: "rename this variable to avoid future errors";
    hint: "try `is_` instead";
));
```

一个 `warning!` 可以连写多条 `hint:`。投递后求值照常继续（不中断），这是 warning 与 error 的本质区别。

#### 4.2.4 代码实践

**实践目标**：体会「不带 span 的 bail!」与「带 span 的 bail!」产出的类型不同。

1. 读 [src/binding.rs:55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L55)：`_ => bail!(expr.span(), "cannot assign to this expression")`——带 span，产出 `SourceResult`。
2. 对比 ops.rs 里 `bail!(error)` 这种「已有 SourceDiagnostic 直接返回」的形式（[src/ops.rs:108-L114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L108-L114)）：`bail!(unary.span(), "..."; hint:...; hint:...)`。
3. 找出 code.rs 里 `bail!(forbidden("set"))`（[L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L136)）：这里 `forbidden` 是个返回 `SourceDiagnostic` 的闭包（[L81-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L81-L83)），`bail!` 命中「已有错误直接返回」那条规则。

**预期结果**：你能根据 `bail!` 的参数形状判断它命中哪条宏规则、产出什么类型。

#### 4.2.5 小练习与答案

**练习 1**：为什么 typst-eval 几乎从不用 `panic!` 来报用户错误？

> **答案**：`panic!` 是编译器内部错误（unrecoverable），用户错误必须用 `SourceResult` 的 `Err` 正常返回，才能被 CLI 收集、带位置渲染。typst-eval 里仅 `eval()` 的循环检测用 `panic!`（见 u1-l3），那是「不该发生」的内部不变量。

**练习 2**：写一个 `warning!` 但忘了调 `sink.warn(...)`，会发生什么？

> **答案**：编译能过（`warning!` 只是个表达式，构造出 `SourceDiagnostic`），但警告被丢弃、用户看不到。所以 `warning!` 必须紧跟 `engine.sink.warn(...)`。

---

### 4.3 精确定位：DiagSpan、SubRange 与 HintedString

#### 4.3.1 概念说明

一条 `SourceDiagnostic` 有五个字段（[../typst-library/src/diag.rs:303-L321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L303-L321)）：

```rust
pub struct SourceDiagnostic {
    pub severity: Severity,                         // Error 还是 Warning
    pub span: DiagSpan,                             // 主错误位置（可带子区间）
    pub message: EcoString,                         // 错误信息
    pub trace: EcoVec<Spanned<Tracepoint>>,         // 调用回溯（4.4 讲）
    pub hints: EcoVec<Spanned<EcoString, DiagSpan>>,// 修复提示（每条也能带位置）
}
```

本节聚焦两件事：

1. **`span: DiagSpan`** 不只是一个 `Span`，它还能携带一个 `SubRange`，用来在整段文本里**只圈出关键几个字符**。
2. **hint 有两种形态**：无位置的「通用 hint」（CLI 列在最下方）和带位置的「定位 hint」（标注在另一处代码上）。前者由 `HintedString`/`.hint()` 承载，后者由 `.spanned_hint()` 承载。

#### 4.3.2 核心流程

精确定位的工作流（以「非法整数 `0x1Z3`」为例）：

```text
1. lexer 给整个整数节点一个 Span（覆盖 "0x1Z3"）
2. find_bad_digits(base, "1Z3") 算出非法字符在子串里的相对区间，如 [1..2]（指向 Z）
3. SubRange::new(start+2, end+2)   ← +2 跳过 "0x" 前缀，得到相对整数的区间
4. DiagSpan::from_span(span, Some(sub_range))  ← 把「节点 span + 子区间」打包成 DiagSpan
5. error!(span, "msg"; hint[hint_span]: "...")  ← 用 hint[hint_span] 让这条 hint 标注在子区间上
```

关键点：`SubRange` 是**相对偏移**（相对 span 起点），不是绝对字节位置；`DiagSpan` 把 `Span` 和可选 `SubRange` 压在 16 字节里。

#### 4.3.3 源码精读

先看 `DiagSpan` 与 `SubRange` 的定义（[../typst-syntax/src/span.rs:219-L222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs#L219-L222) 与 [L335-L338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs#L335-L338)）：

```rust
pub struct DiagSpan { span: Span, extra: u64 }                 // 16 字节，null-optimized
pub struct SubRange { start: u32, end: NonZeroU32 }            // 非空相对区间
```

`DiagSpan::from_span`（[L259-L266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs#L259-L266)）把可选 `SubRange` 压进 `extra`：

```rust
pub fn from_span(span: Span, sub_range: Option<SubRange>) -> Self {
    let extra = sub_range.map_or(0, |SubRange { start, end }| {
        ((start as u64) << 32) | (end.get() as u64)
    });
    Self { span, extra }
}
```

`SubRange::new`（[L345-L357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs#L345-L357)）要求区间非空（`start < end`），否则返回 `None`——因为 `end` 是 `NonZeroU32`。这也是为什么 code.rs 里 `SubRange::new(...)` 的返回值是 `Option`，可以直接喂给 `from_span(span, sub_range)`（它本来就收 `Option`）。

现在看 typst-eval 里最精彩的定位样板——`int_literal_error` 的非法数字分支（[src/code.rs:458-L496](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L458-L496)）：

```rust
ast::IntLiteralError::InvalidDigit(base, digits) => {
    let (range, bad_digits) = find_bad_digits(base, digits);
    // Offset by two to skip the leading `0b`/`0o`/`0x`.
    let sub_range = SubRange::new(range.start + 2, range.end + 2);
    let hint_span = DiagSpan::from_span(span, sub_range);
    ...
    error!(
        span,
        "integer contains digits that are not valid for a{} {} number", ...;
        hint[hint_span]: "{the_digits_are_invalid}";   // ← 这条 hint 标注在非法字符上
        hint: "{} numbers only allow digits {}", base.name(), ...;
    )
}
```

逐行解读：

- `find_bad_digits` 返回 `(Range<usize>, Vec<char>)`——非法字符在**数字部分子串**里的相对区间和具体字符。
- `+ 2` 把相对区间从「数字部分」平移到「整个字面量」（跳过 `0x`/`0o`/`0b` 前缀）。
- `DiagSpan::from_span(span, Some(sub_range))` 打包成 `hint_span`：主位置仍是整个整数，但带了一个子区间。
- `error!(...; hint[hint_span]: "..."; ...)` 用 `hint[hint_span]:` 语法让「the digit `Z` is invalid」这条 hint **精确标注在 `Z` 那个字符上**，而不是泛泛地贴在整个整数上。

`error!` 宏对两种 hint 的处理见 [../typst-library/src/diag.rs:137-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L137-L142)：无 span 的调 `.hint()`（最终 `Spanned::detached`），有 span 的调 `.spanned_hint()`。

至于「无 span 的字符串世界」里的 hint，由 `HintedString` 承载（[../typst-library/src/diag.rs:519-L555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L519-L555)）：它内部是一个 `EcoVec<EcoString>`，第 0 项是 message，其余是 hints；`.hint()` 就是往尾上 push。`HintedStrResult<T> = Result<T, HintedString>`，经 `.at(span)` 升级时（4.1.3 的实现 2），这些 hint 会被搬到 `SourceDiagnostic.hints`。

#### 4.3.4 代码实践

**实践目标**：搞懂「三要素」在 `int_literal_error` 里如何各就各位。

1. 打开 [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs)，定位 `int_literal_error`（[L437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L437)）和它调用的 `find_bad_digits`（[L505](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L505)）。
2. 对照下表，把三要素逐一对应到代码：

   | 要素 | 代码位置 | 承载类型 |
   |------|----------|----------|
   | 错误信息 | `error!(span, "integer contains digits ...")` | `SourceDiagnostic.message` |
   | 精确定位 | `SubRange::new(...)` + `DiagSpan::from_span(...)` → `hint_span` | `DiagSpan` 内的 `SubRange` |
   | 修复提示 | `hint[hint_span]: "the digit `Z` is invalid"` 与 `hint: "...only allow digits..."` | `SourceDiagnostic.hints` |

3. **关键观察**：`find_bad_digits` 返回的 `range` 是相对「数字部分子串」的——它用 `digits.find(c)` 拿到每个非法字符在子串里的 index（迭代器见 [L515-L517](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L515-L517)，聚合见 [L518-L526](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L518-L526)），所以 `int_literal_error` 必须再 `+ 2` 把它对齐到完整字面量。若忘了 `+ 2`，高亮会向左偏移两个字符（错位到前缀上）。

**预期结果**：你能讲清「为什么是 +2」「`hint_span` 为何能只圈住非法字符」。

#### 4.3.5 小练习与答案

**练习 1**：`SubRange::new(3, 3)` 返回什么？为什么？

> **答案**：返回 `None`。因为 `SubRange` 要求 `start < end`（`end` 是 `NonZeroU32` 且必须大于 start）。空区间没有「高亮一段」的意义。

**练习 2**：`error!(span, "msg"; hint: "a"; hint[other_span]: "b")` 产生的 `SourceDiagnostic.hints` 里，两条 hint 各是什么形态？

> **答案**：`"a"` 是 `Spanned::detached`（无位置，CLI 列在底部）；`"b"` 是 `Spanned::new("b", other_span)`（带位置，标注在 `other_span` 处的代码上）。

**练习 3**：`HintedString` 为什么用 `EcoVec<EcoString>` 而不是 `{ message: EcoString, hints: Vec<EcoString> }` 两个字段？

> **答案**：源码注释（[L515-L518](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L515-L518)）说明是为了**缩小 `HintedString` 的体积**：单 vec 比结构体更紧凑，且保证非空。

---

### 4.4 错误回溯：Tracepoint 与 Trace trait

#### 4.4.1 概念说明

当错误发生在深层嵌套的函数调用里（比如 `f()` 调 `g()` 调 `h()`，`h()` 里类型转换失败），光有出错点的 span 还不够——用户想知道「这个错误是在哪条调用链里触发的」。`Tracepoint` 就是回溯帧，`Trace` trait 负责把帧压到错误的 `trace` 字段上。CLI 渲染时会把这些帧显示成一串「while calling `f` / while calling `g` / ...」。

`Tracepoint` 有四种（[../typst-library/src/diag.rs:432-L441](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L432-L441)）：`Call(Option<EcoString>)`、`Show`、`Import`、`Include`，分别对应函数调用、show 规则应用、模块导入、模块包含。

#### 4.4.2 核心流程

`Trace::trace` 的过滤逻辑很巧妙（[../typst-library/src/diag.rs:463-L485](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L463-L485)）：

```text
对错误向量里的每个错误：
  若「错误 span 落在本次调用 span 的范围内」→ 跳过（不重复加帧）
  否则 → push(Spanned(make_point(), 调用span))
```

即：**只给「比当前调用更深层」的错误加帧**，避免在同一个调用上叠加冗余帧。

#### 4.4.3 源码精读

typst-eval 里 `Trace` 的典型用法在 `call_func`（[src/call.rs:167-L181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L167-L181)）：

```rust
fn call_func(vm: &mut Vm, func: Func, args: Args, span: Span) -> SourceResult<Value> {
    let func = func.spanned(span);
    let point = || Tracepoint::Call(func.name().map(Into::into));   // 构造回溯帧
    let f = || {
        func.call(&mut vm.engine, vm.context, args)
            .trace(vm.world(), point, span)                        // 给内部错误加帧
    };
    ...
}
```

`point` 是个闭包，按需产出 `Tracepoint::Call(Some(函数名))`；`.trace(world, point, span)` 在 `func.call` 失败时把这一帧加到所有更深层错误上。函数名取不到时是 `Tracepoint::Call(None)`，渲染成「while calling function」。

模块导入也用同样模式，帧类型换成 `Tracepoint::Import`（[src/import.rs:44-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L44-L48)）：

```rust
Value::Module(import_file(...).trace(
    vm.engine.world,
    || Tracepoint::Import(id.get().vpath().get_with_slash().into()),
    self.span(),
)?);
```

变更方法调用 `maybe_resolve_mutating` 里也有一处（[src/call.rs:208-L209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L208-L209)），用方法名当帧名。

> 注意：`Trace` 只在 `SourceResult` 上工作（它要改的是 `EcoVec<SourceDiagnostic>`）。这就是为什么 4.1 强调底层 `HintedStrResult` 必须先 `.at(span)` 升级成 `SourceResult`，才有资格被 `.trace(...)` 加帧——顺序固定是「先 `.at`，后 `.trace`」。

#### 4.4.4 代码实践

**实践目标**：理解 trace 帧的「去重」过滤。

1. 读 `Trace::trace` 的实现（[../typst-library/src/diag.rs:463-L485](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L463-L485)），找到那段 `if trace_range.start <= error_range.start && trace_range.end >= error_range.end { continue; }`。
2. 设想嵌套调用 `f(g())`，`g` 内部出错且错误 span 就在 `g()` 这段文本内。当外层 `call_func(f)` 调 `.trace(span_of_f_call)` 时，因为错误已落在 `f` 调用的范围内，会被跳过——避免给同一调用叠两帧。
3. **待本地验证**：构造一个三层嵌套调用、最内层类型错误的小 Typst 文件，编译观察 CLI 输出的 trace 栈深度与调用层数是否一致。

**预期结果**：trace 帧数 ≈ 「错误发生点到顶层之间、span 不互相包含的调用层数」。

#### 4.4.5 小练习与答案

**练习 1**：`point` 为什么是个闭包 `|| Tracepoint::Call(...)` 而不是直接求值的值？

> **答案**：`Trace::trace` 的签名是 `fn trace(self, world, make_point: F, span) where F: Fn() -> Tracepoint`。只有确实发生错误时才会调用 `make_point()` 构造帧，省去成功路径下构造 `EcoString`（取函数名）的开销。

**练习 2**：为什么 trace 帧用 `Spanned<Tracepoint>`（带 span）而不是只存 `Tracepoint`？

> **答案**：渲染时需要把每一帧关联到对应的调用位置（点击可跳转），所以帧要带 span；同时 `Trace::trace` 的过滤逻辑也依赖「调用 span 与错误 span 的范围比较」。

---

### 4.5 高质量诊断样板：int_literal_error、find_bad_digits 与 hint_if_shadowed_std

#### 4.5.1 概念说明

前四节是「零件」，本节是「组装」。typst-eval 把那些**结构复杂、需要精确定位或多条件分支**的诊断抽成独立的 `#[cold]` 函数，返回 `SourceDiagnostic`。`#[cold]` 是给编译器的提示：这函数只在错误路径上调用，优化时可牺牲它的调用效率换主路径的代码布局。本节精读三块样板：

1. `int_literal_error` + `find_bad_digits`——精确子区间定位的范例。
2. `hint_if_shadowed_std`——**给一个已存在的错误追加 hint** 的范例（错误增强）。
3. `wrong_number_of_elements` / `disallowed_field_call_error` / `overflowing_int_negation_error`——同套路的其他实例。

#### 4.5.2 核心流程

`int_literal_error` 的总分发（[src/code.rs:437-L498](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L437-L498)）：

```text
输入：int 节点 + IntLiteralError（来自 typst-syntax 的 Int::get()）
├─ PosOverflow（整数太大）
│    ├─ error!("integer value is too large"; hint: 不超过 64 位)
│    ├─ 若无进制前缀（十进制）→ 再加 hint：可改用浮点 "123."
│    └─ 返回 SourceDiagnostic
└─ InvalidDigit(base, digits)
     ├─ find_bad_digits(base, digits) → (相对区间, 非法字符列表)
     ├─ SubRange::new(+2) → DiagSpan::from_span → hint_span
     ├─ 按非法字符个数 [1]/[2]/[多个] 生成不同措辞 the_digits_are_invalid
     └─ error!(...; hint[hint_span]: 那几个字符非法; hint: 该进制只允许哪些字符)
```

它在 `Int::eval` 里被调用（[src/code.rs:196-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L196-L205)）：

```rust
fn eval(self, _: &mut Vm) -> SourceResult<Self::Output> {
    match self.get() {
        Ok(int) => Ok(Value::Int(int)),
        Err(err) => Err(eco_vec![int_literal_error(self, err)]),
    }
}
```

注意整数是**唯一可能失败的字面量**（见 u2-l1）：其他字面量 `Ok` 闭合，唯独 `Int` 要把 `typst-syntax` 给的 `IntLiteralError` 翻译成用户友好的诊断。

`find_bad_digits` 的算法（[src/code.rs:505-L527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L505-L527)）：对每种进制预设「非法字符区间」（十六进制是 `g..=z` 和 `G..=Z`，八进制是 `8..=9`，二进制是 `2..=9`），扫描数字子串，取所有命中字符的**最小 index 到最大 index+1**作为区间，并去重收集字符。这样多个非法字符会被合并成一个连续高亮区间。

#### 4.5.3 源码精读

**样板一：错误增强 `hint_if_shadowed_std`**（[src/vm.rs:95-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L95-L109)）：

```rust
pub fn hint_if_shadowed_std(
    vm: &mut Vm,
    callee: &ast::Expr,
    mut err: HintedString,
) -> HintedString {
    if let ast::Expr::Ident(ident) = callee {
        let ident = ident.get();
        if vm.scopes.check_std_shadowed(ident) {
            err.hint(eco_format!(
                "use `std.{ident}` to access the shadowed standard library function",
            ));
        }
    }
    err
}
```

它接收一个**已经构造好的 `HintedString` 错误**（来自 `cast::<Func>()` 失败），判断被调用的标识符是否遮蔽了标准库同名函数，若是就 `.hint()` 追加一条「用 `std.xxx` 访问被遮蔽的函数」的修复提示。典型用法在 call.rs（[src/call.rs:73-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L73-L77)）：

```rust
let func = callee
    .eval(vm)?
    .cast::<Func>()
    .map_err(|err| hint_if_shadowed_std(vm, &callee, err))   // ← 增强错误
    .at(callee.span())?;                                      // ← 再贴位置
```

这正是「先增强、再 `.at`」的标准顺序：`cast` 失败得 `HintedString` → `map_err` 用 `hint_if_shadowed_std` 加 hint（仍是 `HintedString`）→ `.at(span)` 升级成 `SourceResult`。

**样板二：`wrong_number_of_elements`**（[src/binding.rs:179-L209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/binding.rs#L179-L209)）：先统计 pattern 期望的元素数和是否有 spread，再据此选择 "too many"/"not enough" 措辞和 "at least N elements"/"a single element" 等定量 hint，最后用 `error!(span, "..."; hint: "the provided array has a length of {len}, but ...")` 一次产出。它体现「**根据上下文动态选择措辞与定量 hint**」的套路。

**样板三：`disallowed_field_call_error`**（[src/call.rs:332-L391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L332-L391)）：按目标类型（字典 / 命名参数 / 其他）给出不同主信息，再按「字段是否真能转成函数」「是否在 math 模式」给出差异化的修复 hint（「加括号包裹」「去掉参数」「加空格」）。它体现「**针对同一错误的不同成因给出不同修复路径**」的套路。

**样板四：`overflowing_int_negation_error`**（[src/ops.rs:98-L134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L98-L134)）：它不是「产生」错误，而是**改写**错误——在一元取负 `-(超大整数)` 时，把通用的「值太大」改写成「cannot write minimum integer manually / try `int.min`」。调用方式很巧妙（[src/ops.rs:13-L15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L13-L15)）：

```rust
let value = expr.eval(vm).map_err(|err| {
    overflowing_int_negation_error(self, expr).err().unwrap_or(err)
})?;
```

即：求操作数失败时，尝试用更精准的错误覆盖原错误；若不适用（返回 `Ok(())`），则保留原错误。这体现「**用 `.map_err` 做条件性错误改写**」的套路。

#### 4.5.4 代码实践

**实践目标**：提炼「三要素」，并用 `hint_if_shadowed_std` 举一个增强实例。

**第一部分：三要素提炼**。对照 `int_literal_error` 的 `InvalidDigit` 分支填表（答案见 4.3.4）：

1. **错误信息**：`error!(span, "integer contains digits ...")` 的第一个参数。
2. **定位**：`SubRange::new(range.start + 2, range.end + 2)` → `DiagSpan::from_span`，只圈非法字符。
3. **修复提示**：`hint[hint_span]: "the digit \`Z\` is invalid"`（告诉你哪个字符错）+ `hint: "...only allow digits 0-9, a-f..."`（告诉你该进制允许什么）。

> 结论：typst-eval 一条高质量诊断至少包含 **错误信息（说清出了什么问题）+ 精确定位（圈到具体字符/节点）+ 修复提示（给出可照抄的改法）** 三要素。

**第二部分：`hint_if_shadowed_std` 增强实例**。设想用户写了：

```typst
#let calc = (1, 2, 3)
#let min = "我遮蔽了 std.min"   // 用户自定义 min 遮蔽标准库
#calc.min()                      // 误以为数组有 .min 方法
```

- `calc.min()` 走 `FuncCall::eval` → `eval_field_callee`：数组的类型 scope 没有 `min` 方法，于是进到「字段不存在」分支（[src/call.rs:300-L303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L300-L303)），报 `array has no method \`min\``。
- 但若是「callee 是个被遮蔽的标准库标识符」的场景（如把上面改成对一个 Ident callee 做 `cast::<Func>` 失败），`hint_if_shadowed_std` 就会检测到 `vm.scopes.check_std_shadowed("min")` 为真，追加 hint：**`use \`std.min\` to access the shadowed standard library function`**——一条典型的「错误已经存在，再补一条修复路径」的增强。

**操作步骤**：

1. 在 [src/call.rs:76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L76) 确认 `hint_if_shadowed_std` 只在 callee 是 `ast::Expr::Ident` 时生效（字段访问 callee 不走这条路）。
2. 在 [src/vm.rs:102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L102) 确认它依赖 `Scopes::check_std_shadowed`——一个只查「是否遮蔽标准库」的判定。
3. **待本地验证**：本地写一个遮蔽 `min`/`max` 等标准库函数名再误用的 Typst 文件，编译观察报错是否带上 `use \`std.min\` ...` 这条 hint。

**预期结果**：你能在任意 typst-eval 错误点判断「它有没有三要素、缺哪个、能否用 `hint_if_shadowed_std` 这类增强器补上」。

#### 4.5.5 小练习与答案

**练习 1**：`int_literal_error` 和 `find_bad_digits` 为什么都标了 `#[cold]`（[L437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L437)、[L500 注释下方](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L500)）？

> **答案**：它们只在错误路径上执行（整数合法时根本不会调用）。`#[cold]` 告诉编译器把它们移出热路径的代码布局，让「正常求值整数」这条高频路径更紧凑、更快。

**练习 2**：`overflowing_int_negation_error` 返回 `SourceResult<()>`（成功表示「不改写」），调用处用 `.err().unwrap_or(err)` 取回。为什么不直接让它返回 `Option<SourceDiagnostic>`？

> **答案**：设计上把它写成「尝试报一个更精准的错误」的断言式风格（成功=不需要改写），用 `Result` 能复用 `bail!`/`error!` 宏来构造诊断（见 [L108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L108)、[L116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L116)），比手写 `Option` 更简洁。

**练习 3**：若 `find_bad_digits` 遇到 `0b12`（二进制里出现 `2`），`bad_digits` 会是什么？`the_digits_are_invalid` 会渲染成什么？

> **答案**：二进制的非法区间是 `2..=9`，数字部分 `"12"` 里 `2` 命中，`bad_digits = ['2']`，区间 `[2..3]`（相对 "12"）。加 `+2` 后高亮整个字面量的第 4 个字符（即 `2`）。`bad_digits` 长度为 1，命中 `[digit] => eco_format!("the digit \`{digit}\` is invalid")`，渲染成 `the digit \`2\` is invalid`。

## 5. 综合实践

**任务**：给 typst-eval 里一个「信息偏弱」的错误点补全三要素，并把诊断流水线走一遍。

**背景**：flow.rs 的 `for` 循环在遇到不可迭代的值时这样报错（[src/flow.rs:173-L175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/flow.rs#L173-L175)）：

```rust
_ => {
    bail!(self.iterable().span(), "cannot loop over {iterable_type}");
}
```

它只有「错误信息 + 主定位」两要素，缺少「修复提示」。

**第一步：追踪流水线**（源码阅读型）。画出 `bail!(span, "...")` 在这里的完整旅程：

1. 命中 `bail!` 第 3 条宏规则 → `error!(span, "...")` 构造 `SourceDiagnostic` → `return Err(eco_vec![..])`。
2. 错误沿 `ForLoop::eval` → `eval_code`（用 `?` 冒泡）→ `Markup::eval` → 顶层 `eval()`（[src/lib.rs:86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L86)）。
3. 顶层把 `Err(EcoVec<SourceDiagnostic>)` 连同 `engine.sink` 里累积的 warning 一起返回给调用方（CLI/IDE）渲染。

**第二步：补全三要素**（设计型，不实际改源码）。把上面那行改写成带修复提示的版本，参考 `int_literal_error` 的写法：

```rust
// 示例代码（非项目原有，仅为练习设计）
_ => {
    bail!(
        self.iterable().span(),
        "cannot loop over {iterable_type}";
        hint: "for loops can iterate over arrays, dictionaries, strings, and bytes";
        hint: "to iterate over the values of an array, write `for x in array {{ ... }}`";
    )
}
```

**第三步：自检**。回答：

1. 改写后命中 `bail!`/`error!` 的哪条宏规则？（答：带 span + 多条 `hint:` 的规则。）
2. 两条 hint 在 `SourceDiagnostic.hints` 里各是什么形态？（答：都是 `Spanned::detached`，因为没有用 `hint[some_span]:`。）
3. 若想让第二条 hint 标注在 `iterable` 节点上，语法应怎么改？（答：`hint[self.iterable().span()]: "..."`，对应宏内部调 `.spanned_hint(...)`。）

**预期结果**：你能独立为一个错误点补全三要素，并说清每条 hint 在 `SourceDiagnostic` 里的最终形态。

## 6. 本讲小结

- typst-eval 的错误分**字符串世界**（`StrResult` / `HintedStrResult`，无位置、可复用）与**源诊断世界**（`SourceResult = Result<T, EcoVec<SourceDiagnostic>>`，带位置、可批量）；`At` trait 的 `.at(span)` 是两者之间唯一的桥。
- `bail!` / `error!` / `warning!` 是产生诊断的首选宏，支持 `;` 后追加 `hint:` 与带位置的 `hint[span]:`；`warning!` 只构造不投递，必须配 `engine.sink.warn(...)`。它们靠 `__` 前缀 + 重导出实现在模块内可见。
- 一条 `SourceDiagnostic` 含 `severity / span: DiagSpan / message / trace / hints`；`DiagSpan` 把 `Span` 和可选 `SubRange` 压在 16 字节里，`SubRange` 是相对偏移，实现「只高亮关键字符」。
- `Tracepoint` + `Trace::trace` 给深层错误叠加「while calling …」回溯帧，且只给比当前调用更深的错误加帧以去重；用法固定为「先 `.at`，后 `.trace`」。
- typst-eval 用 `#[cold]` 函数封装复杂诊断：`int_literal_error`+`find_bad_digits`（子区间定位）、`hint_if_shadowed_std`（给已有错误追加 hint）、`wrong_number_of_elements`（定量措辞）、`disallowed_field_call_error`（分因施策）、`overflowing_int_negation_error`（条件改写错误）。
- **三要素**贯穿始终：一条好的诊断 = 错误信息 + 精确定位（必要时用 `SubRange`）+ 修复提示（可带独立 span）。

## 7. 下一步学习建议

- **下一讲 [u6-l2（追踪机制与 IDE 支持）](u6-l2-tracing-ide.md)**：本讲的 `hint`/`trace`/`sink` 是「错误通道」，而 IDE hover 走的是另一条「值追踪通道」——`Vm::inspected` / `trace_at` / `engine.traced`。两者都经过 `Sink`，建议对比学习，搞清「为什么有语法错误时 inspect 模式仍要继续求值」。
- **继续阅读源码**：
  - 通读 [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) 里所有 `bail!`/`error!`/`warning!` 调用点，给每个标注它用了三要素里的哪几个。
  - 读 [../typst-library/src/diag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs) 的 `internal_error` / `assert_internal`（[L1075-L1125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-library/src/diag.rs#L1075-L1125)），理解「内部错误（该 panic 的）也走诊断管道并带 backtrace hint」的设计。
  - 读 [../typst-syntax/src/span.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs) 的 `RangeMapper`（[L447 起](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/../typst-syntax/src/span.rs#L447)），看 `SubRange` 如何在「源文本非连续」（如文档注释里的代码）时被映射，体会子区间定位的完整能力。
