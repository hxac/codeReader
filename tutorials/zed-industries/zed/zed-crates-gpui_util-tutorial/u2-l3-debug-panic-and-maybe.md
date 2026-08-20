# debug_panic!、some_or_debug_panic 与 maybe! 宏

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `debug_panic!` 在 debug 构建与 release 构建下分别做什么，以及为什么这样设计。
2. 区分 `#[cfg(debug_assertions)]` 与 `cfg!(debug_assertions)` 两种判定形态的判定时机与编译差异。
3. 理解 `some_or_debug_panic` 这个「带断言的恒等函数」的行为与适用场景。
4. 手写展开 `maybe!` 的三种形式（普通块、`async` 块、`async move` 块），并用它在不改变函数签名的前提下使用 `?` 运算符。

## 2. 前置知识

### 2.1 声明宏（macro_rules!）

Rust 的声明宏接收一段「token 流」（token tree，记作 `tt`），按匹配规则改写成另一段代码。本讲的 `debug_panic!` 和 `maybe!` 都是 `#[macro_export]` 的声明宏——这个属性把宏导出到 crate 根，调用方可以 `use gpui_util::debug_panic;` 或直接写 `gpui_util::debug_panic!(...)`（zed 仓库里大量出现的 `util::maybe!(...)` 就是后一种全路径写法）。

### 2.2 debug_assertions 是什么

`debug_assertions` 是 cargo 提供的一个编译期配置：默认在 dev profile（`cargo build`/`cargo run`）下为开，在 release profile（`--release`）下为关。它由 profile 的 `debug-assertions` 字段控制，与优化级别 `opt-level` 是互相独立的配置——release 构建也可以显式打开它，debug 构建也可以关掉它。

它有两种用法，本讲的两个工具恰好各用一种：

- 属性形态 `#[cfg(debug_assertions)]`：编译期「代码剔除」，未选中的分支根本不进入编译产物，甚至可以引用不存在的项。
- 表达式形态 `cfg!(debug_assertions)`：编译期展开成 `true`/`false` 字面量，但语法上是一个运行时表达式，所以 `if` 的两个分支都要通过类型检查。

### 2.3 panic 与回溯

`panic!("fmt", args...)` 会格式化消息、展开栈（unwind）并终止当前线程。`std::backtrace::Backtrace::capture()` 则尝试捕获当前调用栈：只有进程环境变量 `RUST_BACKTRACE` 为 `1` 或 `full` 时才会真正捕获（该环境变量在进程内只读取一次并缓存），否则 `Display` 输出为 `disabled backtrace`。这正是本讲实践环节要设置 `RUST_BACKTRACE=1` 的原因。

### 2.4 `?` 运算符的作用域

`?` 作用于「所在函数（或闭包、块）」的返回类型：对 `Result` 提前 `return Err(...)`（经 `From::from` 转换），对 `Option` 提前 `return None`。关键结论：**`?` 的提前返回只作用于最内层的函数/闭包体**——这是 `maybe!` 能工作的全部原理。

### 2.5 与前几讲的衔接

- u2-l1 讲过 `ResultExt::debug_assert_ok` 在 debug 构建 panic、release 构建放行——它的分叉点正是本讲的 `debug_panic!`。
- u2-l2 讲过 `#[track_caller]` 让 `Location::caller()` 取到调用点位置——本讲的 `some_or_debug_panic` 也标注了它，道理相同。

## 3. 本讲源码地图

本讲的全部定义都集中在 gpui_util 的单文件门面里，另引用三个仓库内的真实调用点作为示例。

| 代码点 | 位置 | 作用 |
| --- | --- | --- |
| `debug_panic!` 宏 | lib.rs L173–L183 | 开发期断言：debug 构建 panic，release 构建记录带回溯的错误日志 |
| `some_or_debug_panic` 函数 | lib.rs L185–L192 | 对 `Option` 的恒等断言：debug 构建遇到 `None` 就 panic，release 构建纯透传 |
| `maybe!` 宏 | lib.rs L194–L209 | 展开为立即调用的（async）闭包，让块内可以用 `?` |
| `ResultExt::debug_assert_ok` | lib.rs L259–L264 | `debug_panic!` 的 crate 内调用者（u2-l1 已精读） |
| `TypeIdHasher::write` | lib.rs L544–L558 | `debug_panic!` 的另一个 crate 内调用者（误用哈希器时报警） |
| rope 的坐标换算 | crates/rope/src/chunk.rs L416–L432 | `debug_panic!` 的典型外部调用点 |
| workspace 的面板序列化 | crates/workspace/src/persistence.rs L2285–L2288 | `maybe!` 消除三层嵌套 `?` 的典型外部调用点 |
| zed 的日志查看器 | crates/zed/src/zed.rs L1952–L1992 | `maybe!(async move { ... }).await` 的典型外部调用点 |

## 4. 核心概念与源码讲解

### 4.1 debug_panic! 宏：开发期崩溃、生产期记日志

#### 4.1.1 概念说明

写代码时经常遇到「按理说不可能到达这里」的分支：输入已经在上游校验过、状态机的非法组合、`match` 的兜底分支。直接 `panic!` 太激进——生产环境的编辑器不该因为一个可恢复的奇怪输入而整个退出；但不写断言，开发期又会放过真正的 bug。

`debug_panic!` 就是这两难的标准答案：

- **debug 构建（开发与测试）**：直接 `panic!`。开发者第一时间看到断言失败、拿到完整 panic 回溯。
- **release 构建（发给用户）**：不崩溃，改为记录一条 error 级别的日志，并附上 `std::backtrace::Backtrace`，方便事后从用户日志里定位。

#### 4.1.2 核心流程

伪代码描述宏的行为：

```text
debug_panic!(格式字符串, 参数...) 展开：

如果 cfg!(debug_assertions) 为真（debug 构建）:
    panic!(格式字符串, 参数...)        ← 立即崩溃，带 panic 回溯
否则（release 构建）:
    backtrace = Backtrace::capture()   ← 受 RUST_BACKTRACE 控制
    log::error!("{格式化消息}\n{backtrace}")
    继续执行后续代码                     ← 不中断程序
```

注意一个关键细节：`cfg!(debug_assertions)` 写在 `if` 的条件位置，所以 **panic 分支和日志分支都要通过类型检查**，只是编译器随后会把恒假的分支优化掉。这与 4.2 节 `some_or_debug_panic` 用的属性形态（直接剔除代码）形成对照。

#### 4.1.3 源码精读

宏定义本体：

[crates/gpui_util/src/lib.rs:L173-L183](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L173-L183)

```rust
#[macro_export]
macro_rules! debug_panic {
    ( $($fmt_arg:tt)* ) => {
        if cfg!(debug_assertions) {
            panic!( $($fmt_arg)* );
        } else {
            let backtrace = std::backtrace::Backtrace::capture();
            log::error!("{}\n{:?}", format_args!($($fmt_arg)*), backtrace);
        }
    };
}
```

逐行解读：

- `( $($fmt_arg:tt)* )`：匹配规则捕获「零个或多个任意 token tree」，也就是调用处 `panic!` 风格的全部参数（格式字符串 + 值参数）。用 `tt` 逐个捕获而不是整体匹配，是为了能把同一段 token 流原样转交给 `panic!` 和 `format_args!`。
- `panic!( $($fmt_arg)* )`：debug 分支把参数原样透传给 `panic!`，所以 `debug_panic!("意外状态: {}", x)` 的消息格式与 `panic!` 完全一致。
- `let backtrace = std::backtrace::Backtrace::capture();`：capture 而不是 `force_capture`，是尊重用户环境——没设 `RUST_BACKTRACE` 时几乎零开销地返回「已禁用」的哨兵值，而不是强制付出采集调用栈的代价。
- `log::error!("{}\n{:?}", format_args!($($fmt_arg)*), backtrace)`：用 `format_args!` 惰性构造消息（不产生中间 `String`），再拼上回溯的 `{:?}` 输出。这里走的是 u2-l1/u2-l2 讲过的 log 门面，输出一条 error 级日志后**继续执行**。

crate 内部有两个现成的调用者。第一个是 u2-l1 精读过的 `debug_assert_ok`：

[crates/gpui_util/src/lib.rs:L259-L264](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L259-L264)

```rust
fn debug_assert_ok(self, reason: &str) -> Self {
    if let Err(error) = &self {
        debug_panic!("{reason} - {error:#}");
    }
    self
}
```

断言失败时 debug 构建立刻崩溃，release 构建记日志后把 `Result` 原样返回给上层——这正是「不该发生的错误」在两种构建下的不同待遇。

第二个在 `TypeIdHasher::write` 里，用来防御「用户拿这个专用哈希器去哈希非 `TypeId` 数据」的误用：

[crates/gpui_util/src/lib.rs:L544-L558](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L544-L558)

输入不足 8 字节时调用 `debug_panic!` 报「你是不是把这个哈希器用在了别的东西上」。

仓库里最典型的外部用法是 rope（文本数据结构）的坐标换算——行号超出文本范围属于「调用方违约」，值得在开发期崩溃、生产期记日志：

[crates/rope/src/chunk.rs:L416-L432](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/rope/src/chunk.rs#L416-L432)

```rust
pub fn point_to_offset(&self, point: Point) -> usize {
    if point.row > self.lines().row {
        debug_panic!(
            "point {:?} extends beyond rows for string {:?}",
            point,
            self.text
        );
    }
    ...
```

注意 `debug_panic!` 之后没有 `return`——release 构建下断言「失败」只是记日志，函数会继续按（可能不正确的）输入算下去。这是使用 `debug_panic!` 时必须意识到的语义：它不是校验，是事后报警。

#### 4.1.4 代码实践

**实践目标**：亲眼对比 `debug_panic!` 在两种构建下的行为差异。

**操作步骤**（示例代码，建议在仓库外新建独立小工程，避免污染 zed 工作区；gpui_util 未发布到 crates.io，必须用 path 依赖）：

1. 在 zed 仓库外的任意目录（例如 `~/labs`）执行：

```bash
cargo new debug-panic-lab
cd debug-panic-lab
cargo add log anyhow env_logger
cargo add gpui_util --path /path/to/zed/crates/gpui_util
```

2. 把 `src/main.rs` 写成（示例代码）：

```rust
use gpui_util::debug_panic;

fn classify(score: u32) -> &'static str {
    if score > 100 {
        // 按协议 score 只能是 0..=100，走到这里说明上游违约
        debug_panic!("意外状态: score {} 超出 0..=100 区间", score);
    }
    if score >= 60 { "及格" } else { "不及格" }
}

fn main() {
    env_logger::init();
    println!("{}", classify(150));
    println!("程序没有崩溃，继续运行到了这里");
}
```

3. debug 构建运行：`RUST_BACKTRACE=1 cargo run`
4. release 构建运行：`RUST_BACKTRACE=1 RUST_LOG=error cargo run --release`
5. 再做一次对照：去掉 `RUST_BACKTRACE=1`，重复第 4 步。

**需要观察的现象**：

- 第 3 步：进程 panic 退出，终端打印 `unexpected state...` 消息（中文环境下为你的文案）和完整 panic 回溯，`classify` 之后的两行 `println!` 不会执行。
- 第 4 步：进程不退出，日志里出现一条 error（含消息与回溯），两行 `println!` 都执行，`classify(150)` 走完了 `if` 之后的逻辑。
- 第 5 步：日志仍然出现，但回溯位置显示 `disabled backtrace`——`capture()` 尊重 `RUST_BACKTRACE`。

**预期结果**：同一份代码，「开发期崩溃拦截」与「生产期降级记日志」由同一个宏自动切换。（具体输出文案待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`debug_panic!` 为什么必须是宏，而不能是一个普通函数？

**答案**：它需要接收 `panic!` 风格的可变格式化参数（格式字符串 + 任意个值参数）。函数无法接收 `("意外状态: {}", x)` 这样的裸 token 序列，调用方只能自己先构造 `String` 或 `format_args!`；宏则可以把 token 流原样透传给 `panic!` 和 `format_args!`，调用体验与 `panic!` 完全一致。

**练习 2**：release 构建下日志分支明明执行了，为什么看不到回溯内容？

**答案**：`std::backtrace::Backtrace::capture()` 只在 `RUST_BACKTRACE` 为 `1` 或 `full` 时才真正采集调用栈（该环境变量进程内只读一次），否则返回一个 `Display` 为 `disabled backtrace` 的哨兵值。设置 `RUST_BACKTRACE=1` 即可看到。

**练习 3**：`cargo build --release` 下 `debug_assertions` 一定是关的吗？

**答案**：不一定。它由 profile 的 `debug-assertions` 字段独立控制，release profile 里也可以显式设为 `true`。判定依据是 `debug_assertions` 配置本身，不是优化级别。

### 4.2 some_or_debug_panic 与 debug_assertions 的两种判定形态

#### 4.2.1 概念说明

`some_or_debug_panic` 解决的是 `Option` 版的同一个问题：「这个 `Option` 按构造不可能 是 `None`，但类型系统无法证明」。它是恒等函数 + 开发期断言：debug 构建下 `None` 触发 panic，`Some` 原样放行；release 构建下什么都不做、原样返回。

它与 `debug_panic!` 的关键差异在于用了**属性形态**的 `#[cfg(debug_assertions)]`，正好用来对照理解两种判定形态。

#### 4.2.2 核心流程

```text
some_or_debug_panic(option) 的行为：

debug 构建:
    若 option 是 None → panic!("Unexpected None")，panic 位置 = 调用者行号
    否则 → 返回 option
release 构建:
    （if 整段被编译剔除）
    返回 option
```

两种 `debug_assertions` 判定形态的对照表：

| 维度 | `#[cfg(debug_assertions)]`（some_or_debug_panic 用） | `cfg!(debug_assertions)`（debug_panic! 用） |
| --- | --- | --- |
| 判定时机 | 编译期，直接剔除未选中的代码 | 编译期展开为 `true`/`false` 字面量 |
| 未选中分支是否参与编译 | 否，连类型检查都不做，可引用仅 debug 存在的项 | 是，两个分支都要通过类型检查 |
| release 产物 | 检查代码完全消失，零开销 | 恒假分支由优化器消除，实际也近零开销 |
| 适用场景 | 两分支差异是「有无代码」 | 两分支都要写、运行时二选一 |

#### 4.2.3 源码精读

[crates/gpui_util/src/lib.rs:L185-L192](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L185-L192)

```rust
#[track_caller]
pub fn some_or_debug_panic<T>(option: Option<T>) -> Option<T> {
    #[cfg(debug_assertions)]
    if option.is_none() {
        panic!("Unexpected None");
    }
    option
}
```

四个细节：

- `#[track_caller]`：u2-l2 讲过的机制，让 panic 报告的位置是**调用者**的行号，而不是 gpui_util 内部这一行。没有它，断言失败的堆栈会把开发者引到库里来。
- `#[cfg(debug_assertions)]` 标在 `if` 语句上：release 构建里这个 `if` 整段消失，函数体只剩 `option`——一个会被内联掉的纯恒等函数。
- 返回 `Option<T>` 而不是 `T`：它不「解包」，只是断言。调用方接着还是要 `.unwrap()` 或 `match`，但此刻的 unwrap 是在断言保护下的。
- release 构建连日志都没有：与 `debug_panic!` 的 release 分支（记 error 日志）相比更「沉默」。取舍是：适合那些「`None` 完全不该发生、发生了也无从恢复、记日志只是噪音」的场景；如果你希望生产日志里留痕，应该用 `debug_panic!` 组合。

顺带说明：在 crates/ 目录下检索不到这个函数的调用点（本讲写作时已验证），它目前是导出待用的工具函数。这也提示我们：基础工具库的公开 API 不必都有仓内调用者。

#### 4.2.4 代码实践

**实践目标**：验证属性形态 `#[cfg]` 的「代码剔除」效果，并对比它与 `debug_panic!` 的 release 行为。

**操作步骤**（示例代码，沿用 4.1.4 的工程）：

1. 在 `main.rs` 里追加：

```rust
use gpui_util::some_or_debug_panic;

fn guaranteed_field() -> u32 {
    let value: Option<u32> = Some(42);
    let checked = some_or_debug_panic(value);
    checked.unwrap()
}
```

2. debug 与 release 各运行一次（`cargo run` / `cargo run --release`）。
3. 源码阅读型验证：把上面 `Some(42)` 改成 `None`，分别在两种构建下运行，观察差异。
4. 深入一步（可选）：`cargo rustc --release -- --emit=asm` 或在 <https://godbolt.org> 上贴入等价代码，观察 release 产物中 `some_or_debug_panic` 是否被完全内联消除。

**需要观察的现象**：第 3 步 debug 构建在 `some_or_debug_panic` 处 panic，且 panic 位置指向你的调用行（`#[track_caller]` 的效果）；release 构建不在此处 panic，程序继续跑到 `checked.unwrap()` 才因 unwrap `None` 而 panic——因为 release 下该函数是纯透传，断言已经不存在了。

**预期结果**：属性形态 `#[cfg]` 把检查从 release 产物中整段剔除；`#[track_caller]` 让 panic 定位到调用点。（第 4 步汇编层面的观察待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：release 构建下 `some_or_debug_panic(None)` 返回什么？有副作用吗？

**答案**：返回 `None`，无任何副作用——`if` 整段被 `#[cfg(debug_assertions)]` 剔除，函数就是恒等函数，也不打日志（对比 `debug_panic!` 在 release 下会记 error 日志）。

**练习 2**：把 `some_or_debug_panic` 里的 `#[cfg(debug_assertions)]` 改成 `if cfg!(debug_assertions)`，行为会变吗？语义等价吗？

**答案**：运行行为等价（release 下检查恒假、优化器会消掉），但编译语义不同：`cfg!` 形态下 `if` 体仍要参与类型检查；`#[cfg]` 形态下未选中的分支直接不存在。对于「整段代码只想在 debug 存在」的场景，属性形态意图更清晰。

**练习 3**：为什么 `some_or_debug_panic` 要标注 `#[track_caller]` 而 `measure`、`post_inc` 这类函数不需要？

**答案**：`#[track_caller]` 只对「报告调用位置」有意义——panic 消息、日志定位。`some_or_debug_panic` 会 panic，需要把位置归到调用者；`measure`/`post_inc` 不会以调用位置报告任何信息，标注了也没有用处。

### 4.3 maybe! 宏：三条匹配规则与 `?` 运算符的作用域隔离

#### 4.3.1 概念说明

`?` 运算符只能用在返回 `Result`/`Option` 的函数里。但真实代码里常有这种困境：函数签名已经定为返回 `Result<SerializedPaneGroup>`（或干脆返回 `()`），而函数内部某段子计算天然是 `Option` 流水线——「从行里切出键、再切出值、再 parse 成数字，任何一步失败就整段作废」。

改造函数签名会让调用方跟着变；层层嵌套 `if let` / `match` 又让代码变成箭头形。`maybe!` 的解法：把这段子计算包进一个**立即调用的闭包**，闭包自己拥有返回类型，`?` 的提前返回就只作用于这个闭包，外层函数签名纹丝不动。

#### 4.3.2 核心流程

宏只有三条匹配规则，展开各是一次「闭包立即调用」（IIFE，immediately-invoked function expression）：

```text
maybe!({ 块 })            →  (|| 块)()
maybe!(async { 块 })      →  (async || 块)()
maybe!(async move { 块 }) →  (async move || 块)()
```

`?` 在其中工作的原理（以普通块为例）：

```text
maybe!({
    let a = opt_a()?;        // None → 从闭包提前 return None
    let b = opt_b(a)?;       // None → 从闭包提前 return None
    Some(combine(a, b))      // 闭包的返回值
})
// 整个表达式的值 = 闭包的返回值（Option<_>），随后由外层代码自行处理
```

要点：

- 闭包的返回类型由块的最后一个表达式推断，所以同一个宏既能在块里用 `?` 于 `Option`，也能用于 `Result`，还能返回任意类型。
- 匹配规则用的是 `$block:block` 片段说明符，要求实参必须是花括号块——写 `maybe!(1 + 1)` 无法匹配（少了花括号），`maybe!({ 1 + 1 })` 则合法但没用到 `?`、没有意义。
- `async` 两条规则产出的是 **Future**，必须 `.await` 才能得到块的返回值。两条规则的区别是闭包捕获方式：`async` 借用外部变量，`async move` 把变量 move 进 future（跨 `.await` 持有数据、或 future 要交给你不拥有所有权的地方时必须用 move）。`async` 闭包是较新的语言特性（Rust 1.85 起稳定），zed 工作区使用新工具链因此可以直接用。

#### 4.3.3 源码精读

[crates/gpui_util/src/lib.rs:L194-L209](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L194-L209)

```rust
/// Expands to an immediately-invoked function expression. Good for using the ? operator
/// in functions which do not return an Option or Result.
///
/// Accepts a normal block, an async block, or an async move block.
#[macro_export]
macro_rules! maybe {
    ($block:block) => {
        (|| $block)()
    };
    (async $block:block) => {
        (async || $block)()
    };
    (async move $block:block) => {
        (async move || $block)()
    }
}
```

- 文档注释直说了设计意图：「展开为立即调用的函数表达式，适合在不返回 Option/Result 的函数里使用 `?`」，并声明接受三种块。
- 三条规则按「前缀 token 依次增多」排列（`$block` → `async $block` → `async move $block`）。声明宏的自顶向下匹配保证了 `maybe!(async move { ... })` 会命中第三条而不是把 `async move` 误当作普通块内容。
- 展开体没有任何额外逻辑——`(|| $block)()` 就是「定义闭包 + 立刻调用」两步，零抽象开销。

仓库内的真实用法（普通块形态），workspace 把数据库行里的三个可空列合成一个 `Option` 三元组，三个 `?` 替代了三层嵌套 `if let`：

[crates/workspace/src/persistence.rs:L2285-L2288](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/workspace/src/persistence.rs#L2285-L2288)

```rust
.map(|(group_id, axis, pane_id, active, pinned_count, flexes)| {
    let maybe_pane = maybe!({ Some((pane_id?, active?, pinned_count?)) });
    ...
```

注意外层的 `map` 闭包返回的是 `Result`（函数体里随后还有 `transpose()?`），而 `maybe_pane` 是 `Option`——两种类型在同一个小函数里各用各的 `?`，互不干扰，这正是 `maybe!` 的价值。

`async move` 形态的真实用法，zed 打开「日志查看器」时在外层 `async fn`（返回 `()`，本来完全没法用 `?`）内部构造了一个返回 `Option` 的异步块，结尾 `.await` 拿到结果：

[crates/zed/src/zed.rs:L1952-L1992](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zed/src/zed.rs#L1952-L1992)

```rust
maybe!(async move {
    let project = workspace
        .read_with(cx, |workspace, _| workspace.project().clone())
        .ok()?;
    ...
        .ok()
})
.await;
```

块内一连串 `.ok()?` 把「实体已被释放」之类的失败折叠成 `None`，一路贯穿多步异步操作；用 `move` 是因为 future 里要持有 `log`、`paths` 等跨 `.await` 的数据。

#### 4.3.4 代码实践

**实践目标**：把一段三层嵌套 `if let` 的解析函数用 `maybe!` 重写并保持测试通过，再补一个 `async move` 变体。

**操作步骤**（示例代码，沿用 4.1.4 的工程）：

1. 在 `main.rs` 写出「重写前」版本和测试：

```rust
use gpui_util::maybe;

/// 解析形如 "name:8080;active" 的记录，任何一段缺失或非法都返回 None
fn parse_record_nested(line: &str) -> Option<(String, u16)> {
    if let Some((name, rest)) = line.split_once(':') {
        if let Some((port_str, _tail)) = rest.split_once(';') {
            if let Ok(port) = port_str.parse::<u16>() {
                return Some((name.to_string(), port));
            }
        }
    }
    None
}

#[test]
fn parses_valid_records() {
    assert_eq!(
        parse_record_nested("api:8080;active"),
        Some(("api".to_string(), 8080))
    );
    assert_eq!(parse_record_nested("api:8080"), None);
    assert_eq!(parse_record_nested("api:port;active"), None);
}
```

2. 运行 `cargo test`，确认通过。
3. 用 `maybe!` 重写成「重写后」版本（示例代码）：

```rust
fn parse_record(line: &str) -> Option<(String, u16)> {
    maybe!({
        let (name, rest) = line.split_once(':')?;
        let (port_str, _tail) = rest.split_once(';')?;
        let port = port_str.parse::<u16>().ok()?;
        Some((name.to_string(), port))
    })
}
```

把测试里的函数名换成 `parse_record`，再跑 `cargo test`。
4. 追加一个 `async move` 变体并驱动它（需要 `cargo add futures`）：

```rust
async fn fetch_name(id: u32) -> Result<String, String> {
    if id == 0 { Err("unknown id".into()) } else { Ok(format!("user-{id}")) }
}

async fn greet(id: u32) -> Option<String> {
    maybe!(async move {
        let name = fetch_name(id).await.ok()?;
        Some(format!("hello, {name}"))
    })
    .await
}

// main 里：
let msg = futures::executor::block_on(greet(7));
println!("{msg:?}");
```

**需要观察的现象**：第 3 步重写后测试全部通过；`parse_record` 内部从三层嵌套变成三行顺序流水线，函数签名不变。第 4 步 `greet(7)` 打印 `Some("hello, user-7")`；把 7 改成 0 则打印 `None`——`?` 在 `async move` 块内同样只提前返回块本身。

**预期结果**：三种 `maybe!` 形态都能在保持外层签名的前提下使用 `?`。（测试与运行输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`maybe!({ let x = f()?; g(x) })` 中的 `?` 会让外层函数提前返回吗？

**答案**：不会。`?` 的提前返回作用于立即调用的那个闭包，等价于「从闭包 `return Err/None`」；闭包调用的返回值成为整个 `maybe!` 表达式的值，外层函数接着处理这个值。

**练习 2**：`maybe!(async move { ... })` 表达式的值是什么？如何得到块的最终结果？

**答案**：是一个 Future（async 闭包被立即调用返回 future）。必须在后面 `.await`——zed 源码 `zed.rs` L1992 的 `.await;` 就是这么用的。

**练习 3**：为什么 `maybe!` 的匹配规则用 `$block:block` 而不是 `$expr:expr`？写 `maybe!(some_expr)` 会发生什么？

**答案**：`block` 片段说明符要求实参是花括号块，展开时才能原样嵌进 `(|| $block)()` 形成合法闭包体。`maybe!(some_expr)` 不匹配任何一条规则，编译器报「no rules expected the token」一类的宏匹配错误；必须写成 `maybe!({ some_expr })`。

## 5. 综合实践

把本讲三个工具串成一个「迷你配置记录解析器」（示例代码，沿用之前创建的工程）。需求：解析 `"name:port;tag"` 格式的记录，要求端口非零、名字必填。

```rust
use gpui_util::{debug_panic, maybe, some_or_debug_panic};

/// 解析记录；名字缺失、端口非法都返回 None
pub fn parse_record(line: &str) -> Option<(&str, u16, &str)> {
    maybe!({
        let (name, rest) = line.split_once(':')?;
        let (port_str, tag) = rest.split_once(';')?;
        let port = port_str.parse::<u16>().ok()?;
        // 协议保证：经过上游白名单过滤的 tag 一定非空
        // debug 构建下空 tag 会在这里 panic；release 构建下恒等透传，由 ? 把 None 折叠出去
        let tag: &str = some_or_debug_panic(if tag.is_empty() { None } else { Some(tag) })?;
        Some((name, port, tag))
    })
}

/// 记录已经过校验，端口不应为零；违反即为调用方 bug
pub fn endpoint(record: (&str, u16, &str)) -> String {
    if record.1 == 0 {
        debug_panic!("意外状态: 端口为 0 的已校验记录 {:?}", record.0);
    }
    format!("{}#{}:{}", record.2, record.0, record.1)
}
```

任务步骤：

1. 补全 `main`：用一组正常记录（如 `"api:8080;prod"`）和一组边界记录（端口 0、缺 tag）驱动上面两个函数。
2. debug 构建运行（`RUST_BACKTRACE=1 RUST_LOG=error cargo run`），确认端口 0 的记录触发 panic、panic 位置在你的调用行。
3. release 构建运行同样输入，确认程序不崩溃、`endpoint` 对端口 0 记录仍输出结果、日志里出现带回溯的 error。
4. 为 `parse_record` 写三个测试（合法、缺分号、端口非数字），`cargo test` 全绿。
5. 写一段 4.3.4 风格的 `maybe!(async move { ... })`，模拟「异步读取一行再解析」的组合，验证 `?` 在 async 块内同样工作。

验收标准：同一份代码在 debug 下「逢 bug 必崩」、在 release 下「逢 bug 必留日志但不崩」，且所有解析逻辑都没有嵌套超过一层的 `if let`。（运行结果待本地验证。）

## 6. 本讲小结

- `debug_panic!`（lib.rs L173–L183）接收 `panic!` 风格参数，debug 构建 panic、release 构建记一条带回溯的 error 日志后继续执行；release 分支的回溯依赖 `RUST_BACKTRACE=1`。
- `cfg!(debug_assertions)` 展开为编译期布尔但两个分支都要过类型检查；`#[cfg(debug_assertions)]` 则把未选中分支整段从编译中剔除——`debug_panic!` 用前者，`some_or_debug_panic` 用后者。
- `some_or_debug_panic`（lib.rs L185–L192）是对 `Option` 的恒等断言：debug 遇 `None` panic（位置归调用者），release 纯透传、连日志都没有。
- `maybe!`（lib.rs L194–L209）三条规则分别展开为 `(|| 块)()`、`(async || 块)()`、`(async move || 块)()`，用「立即调用的闭包」给 `?` 划出独立作用域。
- `maybe!` 的价值在不改外层函数签名的前提下使用 `?`：persistence.rs 用它折叠三个可空列，zed.rs 用它让返回 `()` 的 async 函数内部也能 `.ok()?`。

## 7. 下一步学习建议

- 下一讲（u2-l4）将精读 `TryFutureExt` 与 `LogErrorFuture`：那里会把「记录错误」延伸到 `Future` 世界，你会再次看到 `Location` 调用点捕获与 `log_error_with_caller` 的复用，以及手写 `Future` 实现中的 `unsafe` Pin 投影——建议先回顾 u2-l2 的 `#[track_caller]` 内容。
- 想看 `maybe!` 更多真实用法，可通读 [crates/dap/src/transport.rs:L711](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/dap/src/transport.rs#L711) 与 [crates/workspace/src/pane.rs:L2708](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/workspace/src/pane.rs#L2708) 附近的调用。
- 想深入宏本身，可以阅读 Rust Reference 的 Macros chapter（片段说明符 `tt`/`block` 与自顶向下匹配规则），再用 `cargo expand` 亲自展开一次 `maybe!`。
