# log_error_with_caller：track_caller 与日志定位

## 1. 本讲目标

上一讲（u2-l1）我们看到 `ResultExt` 的每个方法最后都汇入一个私有函数 `log_error_with_caller`。本讲把这个引擎拆开。学完后你应该能够：

- 解释 `#[track_caller]` 与 `Location::caller()` 的工作原理：调用点信息如何成为编译期常量、为什么零运行时开销、为什么能沿着「`log_err` → `log_with_level`」链条透传。
- 手工执行 `crates/<crate>/src/<module>` 路径解析算法：给定任意源文件路径，推导出日志的 `target` 与 `module_path` 字段。
- 说出手工构造 `log::Record` 的三个理由（动态级别、正确的 file/line、自定义 target），以及六个字段各自的来源与下游消费者。
- 解释 `DebugAsDisplay` 适配器为什么能让 `anyhow::Error` 在只接受 `Display` 的日志函数里输出回溯。
- 解决上一讲遗留的谜题：为什么 demo crate 里 `.log_err()` 产生的日志 target 是空字符串。

## 2. 前置知识

### 2.1 调用点（call site）与被调点（callee）

「调用点」是写出函数调用表达式的那一行，「被调点」是函数定义所在的行。日志定位的意义在于：一条错误日志只有指向**你写 `.log_err()` 的那一行**才有排查价值；如果指向 `gpui_util/src/lib.rs` 的某个内部行，所有错误都会堆在同一处，日志等于废掉。`#[track_caller]` 就是 Rust 为「普通函数想拿到调用点信息」提供的机制。

### 2.2 `Location`：编译期就定好的位置常量

`core::panic::Location<'a>` 是一个三字段结构：文件名 `&str`、行号 `u32`、列号 `u32`。它最初为 panic 消息设计（这就是它住在 `panic` 模块里的原因），但任何函数都能用。关键性质：

- `Location` 是 `Copy` 的，且引用的数据被编译进二进制的只读段，因此天然拥有 `'static` 生命周期——这一点后面解释 Future 为什么能把它存起来时至关重要。
- 获取它不需要运行时栈回溯（backtrace），值在编译期就确定了。

### 2.3 `file!()` 家族与 `Location` 的区别

`file!()`、`line!()`、`module_path!()` 是宏，取值发生在**宏展开位置**。如果 `gpui_util` 用 `log::error!("...")` 记录错误，这三个宏取到的都是 `gpui_util/src/lib.rs` 内部的信息——这就是必须绕开宏、手工构造 `Record` 的根源（4.3 节展开）。

### 2.4 日志的 target 与 RUST_LOG 过滤

`log` crate 的每条 `Record` 都带一个 `target` 字符串（默认值是 `module_path!()`）。logger 实现用它做过滤：`env_logger` 支持 `RUST_LOG=myapp::worker=debug` 这种「按 target 前缀过滤」，Zed 自己的 logger `zlog` 也按它决定某条记录是否输出。所以 target 不只是装饰——它决定了这条错误日志**能不能被用户看到**。

### 2.5 anyhow 的三种输出形态（承接 u2-l1 的 2.4 节）

- `{}`：只有最外层错误消息。
- `{:#}`：错误链逐行展开，仍无回溯。
- `{:?}`：结构化输出，**末尾附带回溯**（需 `RUST_BACKTRACE=1` 或 `RUST_LIB_BACKTRACE=1` 才会捕获）。

## 3. 本讲源码地图

本讲主角集中在 `crates/gpui_util/src/lib.rs` 的一个区段，外加两处关联代码与一个下游消费者：

| 位置 | 作用 |
| --- | --- |
| [src/lib.rs:290-321](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L290-L321) | 本讲主角 `log_error_with_caller`：路径解析 + 手工构造 `log::Record` |
| [src/lib.rs:4-13](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L4-L13) | 导入 `std::panic::Location`（L8），整个机制的类型来源 |
| [src/lib.rs:235-280](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L235-L280) | `ResultExt` 实现中所有 `#[track_caller]` 方法：位置如何一路传进引擎 |
| [src/lib.rs:323-326](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L323-L326) | 自由函数 `log_err(&error)`：同样的 `#[track_caller]` 用法 |
| [src/lib.rs:328-336](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L328-L336) | `DebugAsDisplay` 适配器：让 `{:?}` 借道 `Display` 约束 |
| [src/lib.rs:375-382](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L375-L382), [src/lib.rs:433-434](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L433-L434) | `TryFutureExt::log_err` 在**构造时**捕获 `Location<'static>` 存进 `LogErrorFuture`（u2-l4 的伏笔） |
| [crates/zlog/src/zlog.rs:69-98](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L69-L98) | 下游消费者：Zed 的 logger 如何使用 `module_path` 与 `target` |

## 4. 核心概念与源码讲解

### 4.1 `#[track_caller]` 与 `Location`

#### 4.1.1 概念说明

被 `#[track_caller]` 标注的函数，编译器会偷偷多传一个隐藏参数——调用点的 `Location`。函数体内用 `Location::caller()` 取回它。效果：`Location::caller()` 返回**调用者那一行**，而不是函数自己体内的某一行。

你每天都在享受这个机制：`Option::unwrap`、`Result::expect` 报错时能精确指向你写 `.unwrap()` 的那一行，正是因为它们标了 `#[track_caller]`。`gpui_util` 把同一机制搬到了日志上——这就是 `.log_err()` 的日志能定位到你那一行的全部原因。

三个关键设计点：

1. **零运行时开销**：`Location` 的内容在编译期确定，是嵌在二进制里的常量数据；没有栈回溯、没有字符串格式化、没有分配。
2. **`'static` 生命周期**：因为这些常量躺在只读段里，`Location<'static>` 可以被任意久地保存——Future 把它存到字段里、等到 `poll` 时再用，完全合法（见 [src/lib.rs:433-434](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L433-L434) 的 `LogErrorFuture` 元组字段）。
3. **可透传**：`#[track_caller]` 函数调用另一个 `#[track_caller]` 函数时，外层收到的调用点会继续向内层传递（与 `unwrap` 内部调用 panic 函数仍报告用户行号是同一原理）。这让「`log_err` → `log_with_level` → `log_error_with_caller`」三级链条里最深处拿到的仍是用户调用点。

#### 4.1.2 核心流程

```text
用户代码 crates/foo/src/bar.rs:42
    │  result.log_err()
    ▼
log_err                #[track_caller]     ← 隐藏参数：bar.rs:42
    │  self.log_with_level(Error)
    ▼
log_with_level         #[track_caller]     ← 透传：仍是 bar.rs:42
    │  Err(e) =>
    │      log_error_with_caller(*Location::caller(), e, Error)
    │                    └── 此刻取到的仍是 bar.rs:42（透传的结果）
    ▼
log_error_with_caller  普通函数，不再需要标注
    └── 用 caller.file() / caller.line() 构造日志（4.2、4.3 节）
```

注意 `*Location::caller()` 里的解引用：`caller()` 返回 `&'static Location`，而 `Location` 是 `Copy`，`*` 一次拷贝出值。

#### 4.1.3 源码精读

链条的每一级都标注了属性：

> [src/lib.rs:235-238](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L235-L238) `log_err` 是最外层入口，`#[track_caller]` 让它收到用户调用点，然后原样委托给 `log_with_level`。

> [src/lib.rs:271-280](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L271-L280) `log_with_level` 同样标注 `#[track_caller]`，`Err` 分支里用 `*Location::caller()` 取出（透传后的）调用点，连同错误和运行时级别交给引擎。这是整个日志路径上唯一取 `Location` 的地方。

```rust
#[track_caller]
fn log_with_level(self, level: log::Level) -> Option<T> {
    match self {
        Ok(value) => Some(value),
        Err(error) => {
            log_error_with_caller(*Location::caller(), error, level);
            None
        }
    }
}
```

> [src/lib.rs:290-293](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L290-L293) 引擎本身是普通函数——`Location` 已经作为参数传进来了，自然不需要再标注。签名要求 `E: std::fmt::Display`，这个约束决定了 4.4 节 `DebugAsDisplay` 的存在。

其他入口同理：

> [src/lib.rs:323-326](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L323-L326) 自由函数 `log_err(&error)` 也标 `#[track_caller]`，服务于「错误值已经从 `Result` 里取出来」的场景。

Future 版本则展示了 `Location` 的另一种用法——**先取后用**：

> [src/lib.rs:375-382](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L375-L382) `TryFutureExt::log_err` 在**构造包装 Future 的那一刻**（也就是调用点）取 `Location::caller()` 并存进 [src/lib.rs:433-434](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L433-L434) 的 `LogErrorFuture` 元组。等到很久之后 [src/lib.rs:451](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L451) 的 `poll` 里才把它交给 `log_error_with_caller`。能在 `poll` 时拿到正确的调用点，靠的正是 `'static` 生命周期；细节留给 u2-l4。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「打印的是调用点而非定义点」，并观察透传行为。

1. 新建独立 crate：`cargo new track_caller_demo`，不需要任何依赖。
2. 把 `src/main.rs` 替换为（示例代码）：

   ```rust
   use std::panic::Location;

   // 带属性：报告调用点
   #[track_caller]
   fn report_caller() {
       let caller = Location::caller();
       println!("[track_caller]    {}:{}", caller.file(), caller.line());
   }

   // 对照组：不带属性，Location::caller() 落到函数体内的这一行
   fn report_without_attr() {
       let caller = Location::caller();
       println!("[无属性对照]      {}:{}", caller.file(), caller.line());
   }

   // 透传实验：外层也标注，位置应继续向内传
   #[track_caller]
   fn wrapper() {
       report_caller();
   }

   fn main() {
       report_caller();          // 调用点 A
       report_without_attr();    // 对照
       wrapper();                // 调用点 C，位置应透传给 report_caller
       report_caller();          // 调用点 D
   }
   ```

3. `cargo run`。

**需要观察的现象**：

- 两处 `[track_caller]` 打印的行号分别等于「调用点 A」和「调用点 D」所在行——是 `main` 里的行，不是 `println!` 所在的定义行。
- `wrapper()` 触发的打印行号等于「调用点 C」那一行（透传生效），而不是 `wrapper` 体内调用 `report_caller()` 的行。
- 对照组打印的是 `report_without_attr` 体内 `Location::caller()` 那一行的行号——没有属性时取到的就是「本次调用的物理位置」。

**预期结果**：四行输出中，第 1、3、4 行都指向 `main`（或其调用点），只有第 2 行指向函数定义处。列号与具体行号取决于你的文件排版，属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：既然 `log_with_level` 已经标了 `#[track_caller]`，`log_err` 为什么也要标？

**答案**：透传链条必须逐级标注。如果 `log_err` 不标，那么用户调用 `log_err` 时，`log_err` 收不到用户位置；它内部调用 `log_with_level` 时给出的调用点就是 `log_err` 函数体内那一行。`log_with_level` 的透传只能传递「自己收到的位置」，收不到就没得传。

**练习 2**：`Location::caller()` 会不会有运行时开销？它和 `std::backtrace::Backtrace::capture()` 的开销差别是什么？

**答案**：`Location` 是编译期常量，取值近乎免费；而 `Backtrace::capture()` 要在运行时回溯调用栈、符号化信息，开销大得多。这正是 `debug_panic!`（u2-l3）只在真正出错时才捕获回溯、而 `#[track_caller]` 可以放心地用在每个调用点上的原因。

**练习 3**：`log_error_with_caller` 自己为什么不标 `#[track_caller]`？

**答案**：它需要的调用点已经由参数 `caller: core::panic::Location<'_>` 显式传入了。再标注反而引入歧义——它并不想取「上一层函数体内」的位置。把 `Location` 作为显式参数，也是让 Future 这类「先存后用」的调用方能够复用同一引擎的必要设计。

### 4.2 `crates/` 路径解析逻辑

#### 4.2.1 概念说明

拿到调用点之后，`log_error_with_caller` 要解决第二个问题：这条日志的 `target` 该填什么？`Location` 只给了文件路径和行号，没有模块信息（`module_path!()` 是宏，普通函数拿不到）。

作者的选择是**从文件路径反推**。依据是仓库的一条物理约定——注释写得明明白白：

> In this codebase all crates reside in a `crates` directory, so discard the prefix up to that segment to find the crate name

即 zed 工作区所有成员都在 `crates/<crate名>/src/...` 下，于是路径本身就编码了「crate 名 + 模块路径」。解析是纯字符串操作，一次 `split_once("crates/")` + 一次 `split_once("/src/")` 就够了。

Zed 还有一条相关约定（仓库 CLAUDE.md）：新 crate 的库根推荐写成 `[lib] path = "src/<crate名>.rs"` 而不是默认的 `src/lib.rs`——比如 `crates/gpui/src/gpui.rs`、`crates/lsp/src/lsp.rs`。4.2.3 节会看到代码专门为这种「文件名以 crate 名开头」的情况做了特殊分支。

#### 4.2.2 核心流程

解析管线（`file` 变量被影子化三次，类型一路变化）：

```text
caller.file()                     例: "crates/gpui/src/elements/div.rs"
   │  (Windows 先把 '\\' 全部替换成 '/'，见 4.2.3)
   │  split_once("crates/")        &str/String → Option<(&str, &str)>
   ├── None（路径里没有 "crates/"）
   │       └── target 字段 = ""，module_path 字段 = None   ← 上一讲 demo 的现象！
   ▼  Some((前缀, "gpui/src/elements/div.rs"))
   split_once("/src/")             → Option<(krate, module)> = ("gpui", "elements/div.rs")
   │
   ├── module.starts_with(krate)?  例: crates/gpui/src/gpui.rs → ("gpui", "gpui.rs")
   │        是：target = module 去 ".rs"、'/'→"::"        → "gpui"（crate 根）
   │        否：target = krate + "::" + module 处理结果    → "gpui::elements::div"
   ▼
同时：file = "crates/" + 后半段     → "crates/gpui/src/elements/div.rs"（规范化路径）
```

用真实文件验证（这些路径都真实存在于仓库中）：

| 调用点所在文件 | `(krate, module)` | 推导出的 `target` | `module_path` 字段 |
| --- | --- | --- | --- |
| `crates/gpui/src/gpui.rs`（库根） | `("gpui", "gpui.rs")` | `gpui`（走了 `starts_with` 分支） | `crates/gpui/src/gpui.rs` |
| `crates/gpui/src/window.rs` | `("gpui", "window.rs")` | `gpui::window` | `crates/gpui/src/window.rs` |
| `crates/gpui/src/elements/div.rs` | `("gpui", "elements/div.rs")` | `gpui::elements::div` | `crates/gpui/src/elements/div.rs` |
| `crates/lsp/src/lsp.rs`（库根） | `("lsp", "lsp.rs")` | `lsp` | `crates/lsp/src/lsp.rs` |
| 独立 crate 的 `src/main.rs` | 解析失败（`None`） | `""`（空串） | `None` |

#### 4.2.3 源码精读

> [src/lib.rs:294-297](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L294-L297) 平台预处理：Windows 上 `Location::file()` 用反斜杠分隔，先统一替换成 `/`，后续的 `split_once` 才能工作。这是上一讲「互补对」条件编译在函数内部的微缩形态。

> [src/lib.rs:298-301](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L298-L301) 两次切分：先丢掉 `crates/` 之前的所有前缀（绝对路径、CI 目录结构都不影响），再按 `/src/` 分出 crate 名与模块部分。注意局部变量名 `target` 此时存的是 `(krate, module)` 二元组，还不是最终的 target 字符串。

```rust
// In this codebase all crates reside in a `crates` directory,
// so discard the prefix up to that segment to find the crate name
let file = file.split_once("crates/");
let target = file.as_ref().and_then(|(_, s)| s.split_once("/src/"));
```

> [src/lib.rs:303-309](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L303-L309) 组装 `target` 字符串：`trim_end_matches(".rs")` 去扩展名，`replace('/', "::")` 把目录层级变成模块层级。`starts_with(krate)` 分支服务于「库根文件名 = crate 名」的 Zed 约定——`crates/gpui/src/gpui.rs` 若走通用分支会得到冗余的 `gpui::gpui`，特殊分支把它修成干净的 `gpui`。

> [src/lib.rs:310](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L310) 重新拼出规范化的相对路径 `"crates/" + 后半段`，剥掉了绝对前缀。这个值最终会进 `Record` 的 `module_path` 字段（4.3 节）。

一个值得知道的启发式副作用：`starts_with` 是前缀判断而非全等判断。假如某个文件名只是**恰好**以 crate 名开头（例如想象一个 `crates/gpui/src/gpui_window.rs`），它会走特殊分支，得到 `gpui_window` 而不是 `gpui::gpui_window`——crate 前缀被「吃掉」了。这是把约定当解析依据的代价：简单、零开销，但依赖命名纪律。

另一个边界：`split_once("crates/")` 切在**第一个** `crates/` 上。如果用户的检出路径本身更早含有 `crates` 目录（例如 `/home/u/crates/zed/crates/lsp/...`），且 rustc 拿到的是绝对路径，解析就会切错位置。实践中 zed 从工作区根构建、路径本来就是 `crates/...` 开头的相对路径，所以很少触发——但这是理解「字符串解析依赖目录约定」的最好例子。

#### 4.2.4 代码实践

**实践目标**：把解析规则当成一台纸面机器，对给定路径手工推导结果（本讲主实践·上）。

1. 对下面每个输入，写出 `(krate, module)`、`target` 字段、`module_path` 字段三行的值：

   ```text
   a) crates/foo/src/bar/baz.rs        ← 规格中指定的样例
   b) crates/foo/src/foo.rs
   c) crates/foo/src/util/mod.rs       ← 假想路径，考察边界
   d) /ci/agent/zed/crates/foo/src/bar.rs   ← 带绝对前缀
   e) src/main.rs                      ← 独立 crate
   ```

2. 答完后，把解析逻辑抄进 demo crate 用 `assert` 验证（示例代码，放在 `track_caller_demo/src/main.rs` 里即可）：

   ```rust
   fn derive(file: &str) -> (String, Option<String>) {
       let file = file.split_once("crates/");
       let target = file.as_ref().and_then(|(_, s)| s.split_once("/src/"));
       let module_path = target.map(|(krate, module)| {
           if module.starts_with(krate) {
               module.trim_end_matches(".rs").replace('/', "::")
           } else {
               krate.to_owned() + "::" + &module.trim_end_matches(".rs").replace('/', "::")
           }
       });
       let file = file.map(|(_, file)| format!("crates/{file}"));
       (module_path.unwrap_or_default(), file)
   }

   fn main() {
       assert_eq!(derive("crates/foo/src/bar/baz.rs"), ("foo::bar::baz".into(), Some("crates/foo/src/bar/baz.rs".into())));
   }
   ```

**需要观察的现象**：纸面推导与 `assert` 结果是否一致；特别是 b) 走了哪个分支。

**预期结果**：

| 输入 | target | module_path 字段 |
| --- | --- | --- |
| a | `foo::bar::baz` | `crates/foo/src/bar/baz.rs` |
| b | `foo`（`foo.rs` 以 `foo` 开头，走特殊分支） | `crates/foo/src/foo.rs` |
| c | `foo::util::mod`（`mod.rs` 是普通文件名，不做特殊处理；Zed 仓库本身禁用 `mod.rs`，见 CLAUDE.md「Never create files with mod.rs」） | `crates/foo/src/util/mod.rs` |
| d | `foo::bar`（绝对前缀被 `split_once` 丢掉） | `crates/foo/src/bar.rs` |
| e | `""`（解析失败） | `None` |

#### 4.2.5 小练习与答案

**练习 1**：为什么解析要用 `split_once`（切第一个）而不是 `rsplit_once`（切最后一个）？

**答案**：目标是找到 `crates/` 目录这一约定段。用 `rsplit_once("crates/")` 时，`/home/u/crates/zed/crates/lsp/src/lsp.rs` 会正确切出 `lsp/src/lsp.rs`，看起来更稳；但作者选择切第一个，隐含假设是「传进来的路径通常已是工作区相对路径（`crates/...` 开头）」，此时两者等价、开销相同。这是以仓库构建方式为前提的取舍。

**练习 2**：`trim_end_matches(".rs")` 和 `strip_suffix(".rs")` 在这里有何差别？

**答案**：`trim_end_matches` 会**反复**剥除匹配的后缀（`foo.rs.rs` → `foo`），`strip_suffix` 只剥一次并返回 `Option`。这里用前者代码更短；对合法的源文件名两者结果相同。

**练习 3**：如果不做 4.2.3 的 Windows 反斜杠替换，会发生什么？

**答案**：Windows 上 `caller.file()` 形如 `crates\gpui\src\window.rs`，`split_once("crates/")` 找不到正斜杠版本的分隔符，解析返回 `None`，所有日志的 target 变成空串、过滤失效。一行 `replace` 换来跨平台一致的 target，是典型的「数据规范化前置」。

### 4.3 `log::Record` 构建

#### 4.3.1 概念说明

解析完路径，最后一步是把所有信息打包成 `log::Record` 并提交。这里有个初看很奇怪的写法：**不用 `log::error!` 宏，而是 `log::logger().log(&Record::builder()...build())` 手工造记录**。

原因有三个，每个都指向宏的固有局限：

1. **级别是运行时值**。`log_error_with_caller` 的 `level` 是参数（`.log_err()` 传 Error、`.warn_on_err()` 传 Warn），而 `error!`/`warn!` 的级别固化在宏名里。虽然 `log` crate 提供 `log::log!(level, ...)`，但它解决不了下面两个问题。
2. **file/line 会取错位置**。宏内部用 `file!()`/`line!()` 取**宏展开处**的位置——那是 `gpui_util/src/lib.rs` 的内部行。唯一的正确做法是把 `caller.file()`/`caller.line()` 显式写进 `Record`。
3. **target 需要自定义字符串**。宏默认把 `module_path!()` 当 target，同样会落在 gpui_util 上；我们要的是 4.2 节推导出的 `gpui::elements::div`。

事实上，`log::error!` 宏展开后的底层正是 `log::logger().log(&record)`——手工构造不是绕近路，而是使用了宏赖以实现的同一层 API，只是夺回了每个字段的控制权。

#### 4.3.2 核心流程

六个字段的来源与去向：

| `Record` 字段 | 填入的值 | 来源 | 典型消费者 |
| --- | --- | --- | --- |
| `level` | 运行时参数 | `log_with_level` 的调用方决定 | 级别过滤（`RUST_LOG=error`） |
| `args` | `format_args!("{:#}", error)` | 错误的 alternate Display（anyhow 展开错误链） | 日志输出正文 |
| `target` | `"gpui::elements::div"` 或 `""` | 4.2 节路径推导 | `env_logger` / `zlog` 的按域过滤 |
| `module_path` | `"crates/gpui/src/elements/div.rs"` 或 `None` | 4.2 节重新拼接的规范化路径 | Zed logger 提取 crate 名 |
| `file` | `caller.file()` **原样**（Windows 仍是反斜杠） | `#[track_caller]` | `module_path` 缺失时的兜底显示 |
| `line` | `caller.line()` | `#[track_caller]` | 精确定位 |

注意一个容易误读的细节：**字段名和变量名是交叉的**——变量 `module_path`（推导出的 `gpui::elements::div`）进了 `target` 字段，变量 `file`（`crates/...` 路径）进了 `module_path` 字段。这是有意为之：`log` 生态里 `target` 才是过滤的主键，所以最有信息量的模块串放在那里；`module_path` 字段则携带路径，供下游做 crate 级归并。

下游如何消费？看 Zed 自己的 logger：

> [crates/zlog/src/zlog.rs:73](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L73) `zlog` 优先读 `record.module_path()`、缺失时退回 `record.file()`，然后用 [crates/zlog/src/zlog.rs:240-255](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L240-L255) 的 `extract_crate_name_from_module_path`（截取第一个 `::` 之前的部分）做 crate 级过滤；[crates/zlog/src/zlog.rs:87](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L87) 再用 `record.target()` 做域级判断。`gpui_util` 精心填的两个字段在这里被真实消费。

#### 4.3.3 源码精读

> [src/lib.rs:311-320](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L311-L320) 引擎的最后一步：通过 `log::logger()` 拿到全局 logger（`log` 门面的运行时入口），用 builder 模式逐字段填装 `Record` 后提交。`target` 用 `unwrap_or("")` 兜底、`module_path` 用 `Option` 直传（解析失败即为 `None`），`file` 用 `caller.file()` 原始值。

```rust
log::logger().log(
    &log::Record::builder()
        .target(module_path.as_deref().unwrap_or(""))
        .module_path(file.as_deref())
        .args(format_args!("{:#}", error))
        .file(Some(caller.file()))
        .line(Some(caller.line()))
        .level(level)
        .build(),
);
```

三个容易忽略的点：

1. `args` 用的是 `format_args!("{:#}", error)`——**不分配内存**的惰性格式化参数，真正拼字符串发生在 logger 实现决定输出时。错误链信息（anyhow 的 `{:#}` 形态）在这一步就定型了。
2. `file` 字段填的是 `caller.file()` 原始值而非 4.2 节规范化过的 `crates/...` 路径——Windows 上它仍带反斜杠。规范化的版本只进了 `module_path`。
3. 这行代码也解释了 2.4 节的伏笔：因为 `level` 是普通字段，同一个引擎可以服务 Error、Warn 以及未来任何级别，`log_err` 与 `warn_on_err` 才能共享全部实现。

#### 4.3.4 代码实践

**实践目标**：装一个「探针 logger」，把六个字段全部打出来，亲眼看 `log_error_with_caller` 填了什么（本讲主实践·中）。

1. 继续 `track_caller_demo`，在 `Cargo.toml` 加依赖：

   ```toml
   [dependencies]
   gpui_util = { path = "<你的 zed 仓库路径>/crates/gpui_util" }
   log = "0.4"
   ```

2. 新建 `src/bin/probe.rs`（示例代码）：

   ```rust
   use gpui_util::ResultExt;
   use std::panic::Location;

   struct Probe;

   impl log::Log for Probe {
       fn enabled(&self, _: &log::Metadata) -> bool {
           true
       }
       fn log(&self, record: &log::Record) {
           println!(
               "level={} target={:?} module_path={:?} file={:?} line={:?}\n  args: {}",
               record.level(),
               record.target(),
               record.module_path(),
               record.file(),
               record.line(),
               record.args()
           );
       }
       fn flush(&self) {}
   }

   #[track_caller]
   fn show_caller() {
       let c = Location::caller();
       println!("show_caller 的调用点: {} : {}", c.file(), c.line());
   }

   fn main() {
       if let Err(error) = log::set_boxed_logger(Box::new(Probe)) {
           eprintln!("安装 logger 失败: {error}");
       }
       log::set_max_level(log::LevelFilter::Info);

       show_caller();
       let _ = std::fs::read_to_string("不存在.txt").log_err();
   }
   ```

3. 运行：`cargo run --bin probe`。

**需要观察的现象**：探针输出中 `file` 的行号是否等于 `main` 里调用 `.log_err()` 的那一行；`target` 与 `module_path` 分别是什么；`show_caller` 打印的行号与之对照。

**预期结果**（在独立 crate 中，源文件路径不含 `crates/`）：

```text
level=Error target="" module_path=None file=Some("src/bin/probe.rs") line=<.log_err() 所在行>
  args: No such file or directory (os error 2)
```

`target` 为空串、`module_path` 为 `None`——这正是 4.2 节「解析失败」分支的样子，也就是上一讲实践里「demo crate 的 target 是空字符串」的完整解释。把同一个 demo 挪到 `crates/` 目录结构下会得到什么，是第 5 节综合实践的任务。`file` 字段是相对路径还是绝对路径取决于 cargo 调用 rustc 的方式，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `args` 字段用 `format_args!` 而不是 `format!`？

**答案**：`format!` 立刻分配 `String` 并完成格式化；`format_args!` 只构造一个惰性的参数包，等 logger 真正要输出时才求值。配合 logger 的级别过滤，被过滤掉的日志连一次分配都不发生。

**练习 2**：如果这里改用 `log::log!(level, "{}", error)`（带运行时级别的宏），还有哪些字段会错？

**答案**：`file`/`line` 会变成 `gpui_util/src/lib.rs` 中宏展开处的位置，`module_path`/`target` 会变成 gpui_util 的模块信息。也就是说级别问题可以用 `log::log!` 解决，但定位与 target 问题只有手工 `Record` 才能解决。

**练习 3**：`target` 与 `module_path` 两个字段的内容为什么不像标准库那样「target=模块路径、module_path=模块路径」，而要交叉着填？

**答案**：`gpui_util` 手里只有 `Location`（路径+行号），拿不到真正的 `module_path!()`；它能造出的两个字符串是「推导的模块串」和「规范化路径」。`log` 生态的过滤主键是 `target`，所以把更有过滤价值的模块串放 `target`，路径放 `module_path` 供 `zlog` 这类消费者取 crate 名。这是在「手头数据受限」前提下的务实选择。

### 4.4 `DebugAsDisplay` 适配器

#### 4.4.1 概念说明

矛盾：引擎 `log_error_with_caller` 要求 `E: Display`（因为 `args` 用 `{:#}` 格式化）；但 `anyhow::Error` 只有 `{:?}` 才输出回溯（2.5 节）。于是「想看回溯」的调用路径需要把 `{:?}` 的输出塞进一个满足 `Display` 的类型里。

`DebugAsDisplay` 就是这个零成本适配器：一个单字段新类型包住 `&E`，它的 `Display` 实现内部转手执行 `{:?}`。对引擎而言它是个普通 `Display` 类型；对输出而言它产出 Debug 格式。类型系统层面的「接口转换」，不拷贝数据、不分配字符串。

#### 4.4.2 核心流程

```text
log_err_with_backtrace (E: Debug)
    Err(e) → log_error_with_caller(loc, DebugAsDisplay(&e), Error)
                                   │
                                   ▼
                    DebugAsDisplay 的 Display::fmt
                                   │  write!(f, "{:?}", self.0)
                                   ▼
                    anyhow::Error 的 Debug 输出 = 错误链 + 回溯（需 RUST_BACKTRACE=1）
```

同一个适配器被两处使用：同步路径的 `log_err_with_backtrace` 与异步路径的 `LogErrorWithBacktraceFuture::poll`——`Display` 约束只关心「能不能格式化」，不关心调用方是谁。

#### 4.4.3 源码精读

> [src/lib.rs:330-336](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L330-L336) 适配器本体：结构体只有一行的 `Display` 实现，注释直接说明了存在理由——让 anyhow 错误输出回溯而非单行错误链。

```rust
// Forces `{:?}` formatting through a `Display`-bounded logging helper so `anyhow::Error` emits a
// backtrace instead of the single-line chained message produced by its `Display`/`{:#}` forms.
struct DebugAsDisplay<'a, E>(&'a E);

impl<E: std::fmt::Debug> std::fmt::Display for DebugAsDisplay<'_, E> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}", self.0)
    }
}
```

两个使用点：

> [src/lib.rs:240-256](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L240-L256) `ResultExt::log_err_with_backtrace` 把 `DebugAsDisplay(&error)` 传给引擎，绕过 `E: Display` 的常规路径。

> [src/lib.rs:470-484](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L470-L484) `LogErrorWithBacktraceFuture::poll` 在 Future 完成并得到 `Err` 时用同一适配器，验证了它是可复用的通用桥接件。

为什么是私有类型、只在 crate 内用？因为这个技巧改变输出形态（单行 → 多行带回溯），作为公开 API 容易被误用；crate 的公开面只暴露「语义化」的方法名 `log_err_with_backtrace`，把格式细节封在内部——这也呼应了 u2-l1 提到的那条 doc 注释：大多数调用点应该坚持 `log_err`。

#### 4.4.4 代码实践

**实践目标**：对比同一错误的 `{:#}` 与 `{:?}` 输出，再看两种方法产生的日志正文差异（本讲主实践·下）。

1. 在 demo 的 `Cargo.toml` 再加 `anyhow = "1"`，新建 `src/bin/backtrace_demo.rs`（示例代码）：

   ```rust
   use gpui_util::ResultExt;

   struct Probe;

   impl log::Log for Probe {
       fn enabled(&self, _: &log::Metadata) -> bool {
           true
       }
       fn log(&self, record: &log::Record) {
           println!("[{}] {}", record.level(), record.args());
       }
       fn flush(&self) {}
   }

   fn fallible() -> anyhow::Result<()> {
       Err(anyhow::anyhow!("底层磁盘错误")).context("读取配置失败")?
   }

   fn main() {
       if let Err(error) = log::set_boxed_logger(Box::new(Probe)) {
           eprintln!("安装 logger 失败: {error}");
       }
       log::set_max_level(log::LevelFilter::Info);

       let err = fallible().unwrap_err();
       println!("Display {{:#}} 形态:\n{err:#}\n");
       println!("Debug {{:?}} 形态:\n{err:?}\n");

       let _ = fallible().log_err();              // {:#} 正文
       let _ = fallible().log_err_with_backtrace(); // {:?} 正文（经 DebugAsDisplay）
   }
   ```

2. 运行：`RUST_BACKTRACE=1 cargo run --bin backtrace_demo`。

**需要观察的现象**：前两段 `println` 展示同一错误的两种格式；后两行日志分别是这两种格式的产物——`log_err` 的正文是两行错误链，`log_err_with_backtrace` 的正文末尾多出一段 `stack backtrace:`。

**预期结果**：`{:#}` 输出「读取配置失败\n\nCaused by:\n  底层磁盘错误」式的错误链；`{:?}` 在此基础上附带回溯帧。回溯是否被捕获由 anyhow 按 `RUST_BACKTRACE`/`RUST_LIB_BACKTRACE` 环境变量决定，所以第 2 步必须带 `RUST_BACKTRACE=1`；具体帧数与符号名取决于编译配置，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`DebugAsDisplay` 为什么按引用包住错误（`&'a E`）而不是持有值？

**答案**：使用点形如 `DebugAsDisplay(&error)`，错误本体还留在调用方的 `match` 分支里、之后可能继续使用；适配器只需要在 `log_error_with_caller` 调用期间借用它。按引用包裹使适配器零拷贝，也不对错误施加移动约束。

**练习 2**：如果不引入这个适配器，还有别的办法让回溯版本复用同一个引擎吗？

**答案**：可以给 `log_error_with_caller` 再开一个泛型版本或用两个 trait 约束的枚举参数，但那会让引擎签名复杂化。也可以在调用点先 `format!("{:?}", error)` 成 `String` 再传入（`String` 满足 `Display`），代价是一次立刻发生的分配——即使日志最终被级别过滤掉也得付。新类型适配器是「零开销 + 引擎不动」的最小方案。

**练习 3**：为什么 `DebugAsDisplay` 不实现成 `pub`？

**答案**：它是一个格式转换的内部技巧，不是语义承诺。公开它等于鼓励用户绕过 `log_err_with_backtrace` 这个语义化入口，手工构造「伪装成 Display 的 Debug 输出」，增加误用面。crate 只导出表达意图的方法名（u2-l1 讲过其 doc 注释甚至明确劝阻滥用回溯版本），实现细节保持私有。

## 5. 综合实践

把本讲全部内容串起来：**亲手搭一个迷你 zed 目录结构，验证路径解析端到端生效**。`log_error_with_caller` 的一切设计都以「源文件位于 `crates/<crate>/src/` 下」为前提，我们就造一个满足该前提的最小工程。

1. 建立如下目录（注意：必须是 workspace 结构，cargo 才会以工作区根为基准传 `crates/...` 相对路径给 rustc；这一行为待本地验证）：

   ```text
   track_caller_demo/
   ├── Cargo.toml                 # [workspace] members = ["crates/myapp"]
   └── crates/myapp/
       ├── Cargo.toml             # [lib] path = "src/myapp.rs"（仿 zed 约定）；依赖 gpui_util、log
       └── src/
           ├── myapp.rs           # 库根，文件名 = crate 名 → 应触发 starts_with 分支
           ├── worker.rs          # 普通模块 → 走通用分支
           └── main.rs            # bin 目标 → myapp::main
   ```

2. 三个源文件（示例代码）：

   ```rust
   // crates/myapp/src/myapp.rs
   pub mod worker;

   pub fn load_config() -> Result<String, std::io::Error> {
       std::fs::read_to_string("不存在.ini")
   }
   ```

   ```rust
   // crates/myapp/src/worker.rs
   pub fn ping() -> Result<(), std::io::Error> {
       std::fs::read_to_string("也不存在.ini").map(|_| ())
   }
   ```

   ```rust
   // crates/myapp/src/main.rs
   use gpui_util::ResultExt;
   use myapp::{load_config, worker};

   struct Probe;

   impl log::Log for Probe {
       fn enabled(&self, _: &log::Metadata) -> bool {
           true
       }
       fn log(&self, record: &log::Record) {
           println!(
               "target={:?} module_path={:?} file={:?} line={:?}",
               record.target(),
               record.module_path(),
               record.file(),
               record.line()
           );
       }
       fn flush(&self) {}
   }

   fn main() {
       if let Err(error) = log::set_boxed_logger(Box::new(Probe)) {
           eprintln!("安装 logger 失败: {error}");
       }
       log::set_max_level(log::LevelFilter::Info);

       let _ = worker::ping().log_err();        // 调用点在 main.rs，但错误源自 worker 模块
       let _ = load_config().warn_on_err();     // 换一个级别：level 字段应显示 Warn
   }
   ```

3. 在 `track_caller_demo` 根目录运行 `cargo run -p myapp`。

任务要求（对照 4.2 节的推导表逐项核对）：

1. 两条日志的 `file`/`line` 都应指向 `main.rs` 中各自的调用行——验证 `#[track_caller]` 的定位（4.1 节）。
2. 两条日志的 `target` 都是 `myapp::main`、`module_path` 都是 `crates/myapp/src/main.rs`——**调用点在哪个文件，target 就推导自哪个文件**，与错误产生于哪个模块无关。这是理解「日志定位 = 调用点信息 + 路径解析」的关键。
3. 在 `myapp.rs` 库根里再加一处 `.log_err()` 调用（比如导出一个 `fn probe()` 内部读取文件），从 `main` 调用它：它的 target 应是 `myapp`（`myapp.rs` 以 crate 名开头，走 `starts_with` 特殊分支），与第 2 条形成对照——验证 4.2 节的两个分支。
4. 把 `probe.rs` 里那种探针 logger 换成 `env_logger::init()`，试试 `RUST_LOG=myapp=info` 与 `RUST_LOG=myapp::main=info` 两种过滤：前者两条都出、后者只出 target 恰为 `myapp::main` 的那些——验证 4.3 节「target 是过滤主键」。

预期结果：三次调用产生三条 target 各异的日志（`myapp::main`、`myapp::main`、`myapp`），`env_logger` 的过滤行为与 target 精确对应。若第 2 步看到的 `file` 是绝对路径或不带 `crates/` 前缀，说明当前 cargo 版本传路径的方式与 zed 工作区构建不同，此时 target 会退化为空串——这本身就是 4.2 节「解析依赖目录约定」的最直观教材。整体输出格式待本地验证。

## 6. 本讲小结

- `#[track_caller]` 让函数免费获得调用点：`Location` 是编译进二进制的常量（`Copy` + `'static`），`Location::caller()` 零运行时开销，且沿 `log_err` → `log_with_level` 链条逐级透传（[src/lib.rs:271-280](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L271-L280)）。
- `log_error_with_caller` 用两次 `split_once`（`"crates/"` 与 `"/src/"`）从文件路径反推出 `crate::module` 形式的 target，`starts_with(krate)` 分支专门照顾 Zed「库根文件名 = crate 名」的约定；路径不含 `crates/` 时 target 退化为空串（[src/lib.rs:298-309](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L298-L309)）。
- 不用 `log::error!` 而手工构造 `log::Record`，是为了同时夺回三个控制权：运行时 level、来自 `Location` 的正确 file/line、推导出的自定义 target；字段填写是交叉的——模块串进 `target`（过滤主键），规范化路径进 `module_path`（[src/lib.rs:311-320](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L311-L320)）。
- 这套字段设计有真实消费者：Zed 的 `zlog` logger 用 `module_path().or(file())` 提取 crate 名、用 `record.target()` 做域过滤（[crates/zlog/src/zlog.rs:73](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L73)、[L87](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/zlog/src/zlog.rs#L87)）。
- `DebugAsDisplay` 是零分配新类型适配器：`Display` 实现内部执行 `{:?}`，让 `anyhow::Error` 在 `Display` 约束的引擎里输出回溯（[src/lib.rs:328-336](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L328-L336)）。
- 上一讲的谜题就此解开：demo crate 的 target 为空，是因为它的源文件路径里没有 `crates/`——解析失败走兜底分支，而非 bug。

## 7. 下一步学习建议

同一套「调用点捕获 + 引擎记录」的模式在异步世界会遇到新问题：Future 构造之后可能很久才被 poll，届时早已离开调用点。下一讲 u2-l4（`TryFutureExt`）讲解 `gpui_util` 的解法——在构造时就把 `Location<'static>` 存进 `LogErrorFuture`（[src/lib.rs:375-382](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L375-L382)、[src/lib.rs:443-456](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L443-L456)），以及手动实现 `Future` 所需的 `unsafe` Pin 投影。若想先换个口味，可以平行阅读 u2-l3（`debug_panic!` 与 `maybe!` 宏）看回溯捕获的另一处用法；或直接翻看 `zlog` crate，了解日志离开 `gpui_util` 之后的完整旅程。
