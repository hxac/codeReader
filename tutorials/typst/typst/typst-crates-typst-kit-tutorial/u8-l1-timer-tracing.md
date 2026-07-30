# Timer 性能追踪

## 1. 本讲目标

本讲讲解 typst-kit 中负责「性能追踪」的 `Timer`（源文件 `src/timer.rs`，受 `timer` 特性门禁）。学完本讲，你应当能够：

- 说清一次 Typst 编译的耗时事件是如何被收集、又如何被导出成可在 Chrome Tracing / Perfetto 里可视化的 JSON；
- 掌握 `Timer` 的三种构造方式（`new` / `placeholder` / `new_or_placeholder`）以及「统一代码路径」的设计意图；
- 逐步复述 `record()` 的内部流程：清理 → 运行编译 → 处理 `{n}` 占位符 → 导出 JSON；
- 理解 `resolve_span` 如何把一个对人类毫无意义的原始数字还原成 `(文件, 行号)`，以及 `Detached` / `Number` / `Range` 三种 span 各自的解析路径。

本讲是「性能追踪与时间处理」单元的第一讲，承接 u1-l3 建立的「typst-kit 是组装 `World` 的积木」这一全局认知。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **World trait**：Typst 编译器与外界唯一的契约（见 u1-l3）。本讲里 `resolve_span` 需要回调 `world.source(id)` 与 `world.file(id)`，它们正是 `World` 的两个方法。
- **特性开关（feature flag）**：typst-kit 默认关闭所有特性（见 u1-l2）。`Timer` 整个文件用 `#![cfg(feature = "timer")]` 门禁，不开启 `timer` 特性时它根本不存在。
- **RAII / Drop**：Rust 中「对象销毁时自动执行清理」的机制。本讲会看到 `TimingScope` 靠 `Drop` 来记录一段耗时的「结束事件」。
- **Chrome Tracing JSON 格式**：一种把性能事件描述成「开始(B)/结束(E)」配对的 JSON 数组，可被 `chrome://tracing` 或 <https://ui.perfetto.dev> 加载。本讲导出的就是这种格式。

一个值得提前知道的关键事实：`Timer` 是「周边工具型」积木——它**不参与编译、不实现 `World` trait**，只是把编译过程「包裹」起来测量它。这一点和 `server`、`watcher` 一样（见 u7-l1、u7-l2）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-kit/src/timer.rs` | **本讲主角**。定义 `Timer` 类型及其 `new` / `placeholder` / `new_or_placeholder` / `record` 方法，以及私有函数 `resolve_span`。 |
| `crates/typst-timing/src/lib.rs` | 底层事件收集库。提供全局开关 `enable/clear`、事件缓冲 `EVENTS`、RAII 作用域 `TimingScope`，以及导出函数 `export_json`。`Timer` 几乎所有动作都是对它的封装。 |
| `crates/typst-macros/src/time.rs` | `#[time]` 过程宏。把「函数入口创建 `TimingScope`」这一行偷偷插进被标注的函数，是事件产生的源头。 |
| `crates/typst-syntax/src/span.rs` | `Span` 与 `SpanKind` 定义。`resolve_span` 的核心就是解析 `SpanKind` 的三个变体。 |
| `crates/typst-cli/src/compile.rs` | CLI 集成方。展示 `Timer` 在真实场景下如何被构造与调用。 |
| `crates/typst-cli/src/watch.rs` | `typst watch` 的循环，会**多次**调用 `record`，这正是 `{n}` 占位符存在的根本原因。 |

## 4. 核心概念与源码讲解

### 4.1 Timer 类型与事件收集总览

#### 4.1.1 概念说明

要回答「编译慢在哪一步」，最直接的办法是在编译的各个阶段打点计时。typst 生态把这件事拆成了两层：

- **底层 `typst-timing`**：一个几乎零依赖的小库，负责「在全局开关打开时，把每一段耗时的开始/结束事件存进一个全局列表」。
- **上层 `Timer`（typst-kit）**：负责「在合适的时机打开开关、运行编译、再把收集到的事件导出成 JSON 文件」。

为什么分两层？因为 `typst-timing` 不能依赖 `typst-syntax`（否则 `typst-syntax` 就没法反过来依赖 `typst-timing` 来打点，形成循环依赖）。所以底层只存「一个原始数字」（`NonZeroU64`），而上层 `Timer` 在导出时，再把这个数字「翻译」回人能看懂的 `(文件, 行号)`。`resolve_span` 就是这个翻译官。

`Timer` 本身非常轻量，只有两个字段：

```rust
pub struct Timer {
    /// Where to save the recorded timings of each compilation step.
    path: Option<PathBuf>,
    /// The current watch iteration.
    iter: usize,
}
```

- `path`：`Some` 表示「真要录制并写文件」；`None` 表示「占位，不录制」。
- `iter`：当前是第几次录制，用于 `{n}` 占位符编号。

#### 4.1.2 核心流程

把一次「带计时器的编译」串起来，整条链路是这样的：

```
Timer::new(path)
   │
   │  ① typst_timing::enable()   ← 把全局开关 ENABLED 置为 true
   ▼
timer.record(world, |world| 编译())
   │
   │  ② typst_timing::clear()    ← 清空上次残留事件
   │  ③ 运行闭包「编译()」
   │       └─ 每个标注了 #[time] 的函数：
   │            入口 → TimingScope 创建 → (开关开?) push Start 事件
   │            出口 → Drop             → (开关开?) push End   事件
   │  ④ typst_timing::export_json(writer, |raw| resolve_span(raw))
   │       └─ 把全局 EVENTS 里的事件逐条序列化，
   │          每个带 span 的事件回调 resolve_span 得到 (file, line)
   ▼
磁盘上得到一个 Chrome Tracing 兼容的 JSON 文件
```

两个关键点先记住：**事件是「全局的」**（`typst-timing` 用一个进程级的 `Mutex<Vec<Event>>` 收集，跨线程也能收），**开关默认是关的**（关的时候打点几乎零开销）。`Timer` 的工作就是「在编译前后，正确地拨动这个开关并导出数据」。

#### 4.1.3 源码精读

**`Timer` 结构体**——只有 `path` 和 `iter` 两个字段，极其轻量：

[crates/typst-kit/src/timer.rs:17-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L17-L22) 定义了 `Timer`，字段 `path` 决定是否真录制、`iter` 记录当前录制序号。

**全局开关与事件缓冲**（在 `typst-timing` 中）：

[crates/typst-timing/src/lib.rs:60-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs#L60-L72) 用一个 `AtomicBool` 当开关、一个 `Mutex<Vec<Event>>` 当事件仓库；`enable()` 只是把原子布尔置真（`Relaxed` 排序即可，因为只需要原子性）。

**RAII 打点 `TimingScope`**——这是事件产生的源头：

[crates/typst-timing/src/lib.rs:171-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs#L171-L205) 展示了核心机制：`with_span` 先看 `is_enabled()`，**关就直接返回 `None`、什么都不做**（这就是「关闭时零开销」的由来）；开则在构造时 push 一条 `Start` 事件、在 `Drop` 时 push 一条 `End` 事件。span 以原始 `NonZeroU64` 存储，正是为了不让 `typst-timing` 依赖 `typst-syntax`。

**`#[time]` 宏如何把打点「种」进函数**：

[crates/typst-macros/src/time.rs:29-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/time.rs#L29-L44) 在函数体最前面插入一行 `let __scope = ::typst_timing::TimingScope::with_span/new(...);`。被绑定的变量活到函数末尾才 Drop，正好覆盖整个函数体。例如 CLI 的入口编译函数就标注了它：

[crates/typst-cli/src/compile.rs:257-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L257-L258) `#[typst_macros::time(name = "compile once")]` 让整次编译被记为一个名为 `"compile once"` 的耗时区间；编译引擎内部还有大量函数同样标注了 `#[time]`，它们会嵌套形成一棵调用树。

#### 4.1.4 代码实践

**实践目标**：亲手生成一个 timing JSON 并可视化，建立感性认识。

**操作步骤**：

1. 准备一个最小的 Typst 文档 `doc.typ`，内容随意，比如 `#set page(width: 10cm); Hello.`。
2. 用官方 CLI 编译并开启计时（注意 `--timings` 是实验性参数）：

   ```bash
   typst compile --timings=trace.json doc.typ
   ```

3. 打开浏览器访问 <https://ui.perfetto.dev>，点 `Open trace file` 选中刚生成的 `trace.json`。

**需要观察的现象**：你会看到一条时间轴上有若干嵌套的色块，最外层名为 `compile once`，内部是更细的阶段（如语法分析、求值、布局、渲染等）。每个色块代表一个 `#[time]` 标注的函数。

**预期结果**：能加载并看到层次化的事件条；放大后能看到每个事件的名字。如果想看到每个事件的 `(file, line)` 参数，在 Perfetto 里点开某个色块查看其 `args`。

> 若你本地未安装 typst CLI，此步骤为「待本地验证」；也可以跳到 4.3.4 直接用源码理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `typst-timing` 把 span 存成原始 `NonZeroU64`，而不是直接存 `typst_syntax::Span`？

> **参考答案**：因为依赖方向。`typst-syntax` 内部需要调用 `typst-timing` 来打点，所以 `typst-timing` 不能反过来依赖 `typst-syntax`，否则形成循环依赖。底层只存一个与类型无关的原始数字，把「数字 → Span」的解释权留给上层（`Timer::resolve_span`）。

**练习 2**：关闭计时器（`ENABLED = false`）时，标注了 `#[time]` 的函数还会有额外开销吗？为什么？

> **参考答案**：几乎为零。`TimingScope::with_span` 第一步就检查 `is_enabled()`，关时立刻返回 `None`，既不分配也不上锁、不碰全局 `EVENTS`。所以 `#[time]` 在生产构建里是安全的，可以长期保留。

---

### 4.2 三种构造：new / placeholder / new_or_placeholder

#### 4.2.1 概念说明

`Timer` 有两种「人格」：

- **真计时器**（`path = Some`）：会打开全局开关、录制、写文件。
- **占位计时器**（`path = None`）：什么都不做。

为什么需要占位？因为调用方（如 CLI）在**运行时**才知道用户有没有传 `--timings`。如果用 `Option<Timer>`，那么每次想录制都得写 `if let Some(timer) = ... { timer.record(...) }`，调用点被 if/else 撕裂。`placeholder` 的巧妙之处在于：它让「不计时」也成为一个 `Timer`，于是调用方**永远**写 `timer.record(...)`，由 `record` 内部判断要不要真做。这就是源码注释里说的「uniform code paths」（统一代码路径）。

#### 4.2.2 核心流程

三个构造函数的关系：

```
            ┌── path = Some ──► Timer::new(path)        ──► enable() 开关置真，path=Some, iter=0
Option<Path>┤
            └── path = None ──► Timer::placeholder()    ──► 不碰开关，    path=None, iter=0

Timer::new_or_placeholder(opt)  根据 opt 是 Some/None 分发到上面两者
```

关键差异只有一个：**只有 `new` 会调用 `typst_timing::enable()`**。`placeholder` 完全不动开关——如果之前没人开过，开关保持关，所有 `TimingScope` 都短路返回 `None`，整条编译链路的打点开销恒为零。

#### 4.2.3 源码精读

**`new`——构造真计时器并打开开关**：

[crates/typst-kit/src/timer.rs:33-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L33-L37) 第一行就是 `typst_timing::enable();`，这是「录制」得以生效的前提；随后存 `path`、置 `iter = 0`。注释还点明了 `{n}` 的契约：多次录制时路径**必须**含 `{n}`。

**`placeholder`——构造空壳**：

[crates/typst-kit/src/timer.rs:42-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L42-L44) 字段与 `new` 同形，但 `path = None` 且**不调用** `enable()`。注意它和「关闭的开关」是两回事——它只是「我不打算录制」，至于开关是否被别处打开，它不管。

**`new_or_placeholder`——按运行时配置二选一**：

[crates/typst-kit/src/timer.rs:48-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L48-L53) 一个 `match` 把 `Option<PathBuf>` 分发到 `new` 或 `placeholder`。

**真实调用点**——CLI 用它把命令行参数转成 `Timer`：

[crates/typst-cli/src/compile.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L39) `let mut timer = Timer::new_or_placeholder(command.args.timings.clone());`。其中 `command.args.timings` 就是 CLI 参数 `--timings`：

[crates/typst-cli/src/args.rs:387-393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L387-L393) 字段 `timings: Option<PathBuf>`，没传就是 `None`（占位），传了就是 `Some`（真录制）。

#### 4.2.4 代码实践

**实践目标**：理解「传与不传 `--timings`」对 `Timer` 构造的影响。

**操作步骤**：阅读 [crates/typst-kit/src/timer.rs:33-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L33-L53)，回答下面的问题。

**需要观察的现象**：在脑中分两种情形推演——(a) 用户没传 `--timings`；(b) 用户传了 `--timings=t.json`。

**预期结果**：

- 情形 (a)：`timings = None` → `placeholder()` → `path = None`、开关未被打开 → 后续 `record` 走空路径。
- 情形 (b)：`timings = Some("t.json")` → `new("t.json")` → `enable()` 打开开关、`path = Some` → 后续 `record` 真录制并写出 `t.json`。

#### 4.2.5 小练习与答案

**练习 1**：如果不引入 `placeholder`，CLI 代码会变成什么样？说出一个具体坏处。

> **参考答案**：`timer` 的类型得变成 `Option<Timer>`，每次录制要写 `if let Some(t) = &mut timer { t.record(...) } else { compile_once(...) }`。坏处：调用点出现分支重复——「编译」这一闭包要在两个分支各写一遍（或抽出来），既冗余又容易让两条路径行为不一致。`placeholder` 把分支吸收进 `record` 内部，调用点永远一行。

**练习 2**：假设某段代码先调用 `Timer::new("a.json")`，再单独构造一个 `Timer::placeholder()`。第二个 `placeholder` 会让已经打开的计时器「关掉」吗？

> **参考答案**：不会。`placeholder` 只是把自身的 `path` 设为 `None`，**根本不碰**全局 `ENABLED` 开关。由于 `new("a.json")` 已经把开关置真，全局事件收集仍处于开启状态——这也提示我们：开关是进程级的、单一的全局状态，`Timer` 实例的字段与它相互独立。

---

### 4.3 record()：清理、录制、导出的完整流程

#### 4.3.1 概念说明

`record` 是 `Timer` 唯一的「动作」方法，它接受一个闭包 `f`（通常就是「编译一次」），把这段执行测量下来并导出。它的设计有三个要点：

1. **先清后录**：运行 `f` 之前先 `clear()`，保证导出的 JSON 只含本次编译的事件，不被上一次残留污染。
2. **占位即透传**：若 `path` 是 `None`（占位器），直接运行 `f(world)` 并返回，完全不碰计时设施。
3. **`{n}` 支持多次录制**：路径里写 `{n}` 会被替换成录制序号，从而在 `typst watch` 这种「反复编译」的场景里生成多个文件而不互相覆盖。

#### 4.3.2 核心流程

`record` 的伪代码（省略类型）：

```
fn record(world, f):
    if path is None:              # 占位器
        return f(world)           # 直接透传，零成本

    typst_timing::clear()         # ① 清空上次事件

    numbered = path 含 "{n}"
    if not numbered and iter > 0: # ② 多次录制却没给 {n}
        bail!("cannot export multiple recordings without `{n}` in path")

    final_path = numbered ? path.replace("{n}", str(iter)) : path   # ③ 算最终路径

    output = f(world)             # ④ 运行编译（事件在此期间产生）
    iter += 1                     # ⑤ 序号自增

    writer = BufWriter(File::create(final_path))   # ⑥ 1MB 缓冲
    typst_timing::export_json(writer, |raw| resolve_span(world, raw)   # ⑦ 导出
                                      .unwrap_or(("unknown", 0)))
    return output
```

几个值得注意的细节：

- 第 ④ 步「运行编译」与「导出」是**分离**的：先让闭包完整跑完（事件全部进 `EVENTS`），再统一导出。这保证 `f` 的返回值能被原样透传，不被导出逻辑干扰。
- 第 ⑦ 步给 `resolve_span` 套了 `unwrap_or(("unknown", 0))`：任何无法解析的 span（例如 `Detached`）都退化成 `("unknown", 0)`，保证导出永远不会因为单条坏 span 而失败。
- 第 ⑤ 步 `iter += 1` 在编译之后、导出之前完成；这意味着第一次录制用 `{n}=0`，第二次用 `1`，依此类推。

#### 4.3.3 源码精读

**`record` 的签名与占位短路**：

[crates/typst-kit/src/timer.rs:57-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L57-L64) 签名 `fn record<W: World, T>(&mut self, world: &mut W, f: impl FnOnce(&mut W) -> T) -> StrResult<T>`；开头 `let Some(path) = &self.path else { return Ok(f(world)); };` 就是占位器的透传短路——这是「统一代码路径」落地的关键一行。

**`{n}` 处理与多次录制保护**：

[crates/typst-kit/src/timer.rs:66-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L66-L83) 先 `clear()`；接着判断路径是否含 `{n}`，若不含且 `iter > 0`（说明这不是第一次录制）就用 `bail!` 报错；含则用 `string.replace("{n}", &self.iter.to_string())` 算出本次的最终路径。注意这里用一个临时变量 `storage` 来持有替换后的 `String`，再借 `Path::new(&storage)` 借出引用——经典的「让临时值活得够久」手法。

**导出 JSON**：

[crates/typst-kit/src/timer.rs:85-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L85-L94) 用 `File::create` 建文件、`BufWriter::with_capacity(1 << 20, ...)`（1 MiB 缓冲）包裹，然后调 `typst_timing::export_json`。注意传给它的回调 `|span| resolve_span(world, Span::from_raw(span)).unwrap_or_else(|| ("unknown".to_string(), 0))`：`export_json` 会在**每个带 span 的事件**上回调它一次，把原始数字翻译成 `(file, line)`。

**为什么 `watch` 必须用 `{n}`**——看真实的反复调用：

[crates/typst-cli/src/watch.rs:60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L60) 是 `watch` 的首次编译调用 `timer.record(...)`；而 [crates/typst-cli/src/watch.rs:68-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L68-L83) 的 `loop` 里 [第 79 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L79) 在每次「文件变化 → 重编译」时再次调用 `timer.record(...)`。同一个 `Timer`、`iter` 不断自增，于是每次重编译都会触发 `record`——如果用户给的路径里没有 `{n}`，第二次就会撞上 4.3 里的 `bail!`。这就是 `{n}` 占位符存在的根本动机。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用真实 CLI 观察 `{n}` 占位符如何为多次录制生成不同文件，并验证「不带 `{n}` 在 watch 下会报错」。

**操作步骤**：

1. 准备 `doc.typ`（内容随意），先做**单次编译**对比：

   ```bash
   typst compile --timings=trace-{n}.json doc.typ
   ls trace-*.json   # 期望看到 trace-0.json
   ```

2. 再做**多次编译**——用 `watch`，并带上 `{n}`：

   ```bash
   typst watch --timings=trace-{n}.json doc.typ &
   # 等 1 秒，让首次编译完成；然后修改 doc.typ 内容并保存
   sleep 1
   echo "// edit" >> doc.typ
   sleep 1
   ```

3. 观察 `trace-*.json` 文件列表，应同时出现 `trace-0.json`（首次）和 `trace-1.json`（重编译）。
4. 退出 watch，**去掉 `{n}`** 再试一次 watch：

   ```bash
   typst watch --timings=trace.json doc.typ
   # 触发一次重编译后，期望看到报错：cannot export multiple recordings without `{n}` in path
   ```

**需要观察的现象**：

- 步骤 1：单次 `compile` 只录制一次，`trace-0.json` 生成。
- 步骤 2-3：`watch` 下每次保存文件都新增一个带递增序号的 JSON。
- 步骤 4：不带 `{n}` 时，第二次 `record` 直接报错退出（正是 [timer.rs:70-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L70-L72) 的保护）。

**预期结果**：步骤 4 应当复现「cannot export multiple recordings without `{n}` in path」错误信息，印证 `record` 的多次录制保护逻辑。

> 若本地无可用的 typst CLI，此实践为「待本地验证」；可改为源码阅读型实践：对照 [watch.rs:60 与 watch.rs:79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L60-L79) 与 [timer.rs:66-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L66-L83)，推演第二次 `record` 的 `iter` 值与 `bail!` 触发条件。

#### 4.3.5 小练习与答案

**练习 1**：`record` 为什么在运行 `f(world)` **之前**就调用 `clear()`，而不是导出之后再清？

> **参考答案**：因为「编译」这一步会**产生**事件。如果不清就录，本次导出的 JSON 会混入上一次（甚至上几次）编译的残留事件，时间轴错乱。先清空，保证 `EVENTS` 里只有本次 `f` 产生的事件。注意 `export_json` 内部也会把事件「取走」（`std::mem::take`），双重保证不残留。

**练习 2**：路径 `a-{n}-{n}.json` 在第二次录制时会变成什么？

> **参考答案**：`String::replace` 会替换**所有**匹配，所以两个 `{n}` 都被替换成同一个序号字符串。第二次录制 `iter = 1`，结果是 `a-1-1.json`。（这也说明 `{n}` 不是分别编号，而是统一用当次的 `iter` 值。）

**练习 3**：`record` 用了 `BufWriter::with_capacity(1 << 20, ...)`，这里 1 MiB 的缓冲有什么意义？

> **参考答案**：导出的 JSON 可能让 `serde_json` 频繁向底层 `File` 写小片段；套一层 1 MiB 的缓冲能把这些小写合并成少量大写，减少系统调用次数、提升导出性能。`BufWriter` 在 Drop/flush 时再把缓冲落盘。

---

### 4.4 resolve_span：把 span 解析回 (文件, 行号)

#### 4.4.1 概念说明

`export_json` 需要给每个事件标注它发生在「哪个文件、第几行」，这样你在 Perfetto 里点开一个色块，就能直接定位到源码。但前面提过：底层存的是「一个原始 `NonZeroU64`」，没有文件名、没有行号。`resolve_span` 就负责这个还原。

还原的依据是 `Span::get()`，它把那个 64 位数字拆成三类 `SpanKind`：

- **`Detached`**：不指向任何文件的「游离 span」。无行号，`resolve_span` 直接返回 `None`（导出时退化成 `("unknown", 0)`）。
- **`Number { id, num }`**：**编号 span**，用于 Typst 源码文件（`.typ`）。源码的每个语法树节点在解析时被分配一个稳定的小整数 `num`，编辑时这些编号尽量不变（这样带缓存的求值不会因为插入文字而大面积失效）。要拿到字节位置，得回 `Source` 里按编号查。
- **`Range { id, range }`**：**字节区间 span**，用于外部文件（如图片、数据文件）。直接保存了 `[start, end)` 字节偏移，但要算行号还得先把字节当成文本数行。

`Number` 和 `Range` 的区分，本质是 `Span` 这个 64 位数字的**位布局**决定的。

#### 4.4.2 核心流程

`Span` 的内部布局（高位 16 位放文件 id，低位 48 位放编号）：

\[
\text{Span bits} = \big(\text{file\_id} \ll 48\big) \;\big|\; \text{number}
\]

而低 48 位的 `number` 又按区间划分用途：落在「普通区间」的是 `Number`（源码节点编号），落在「区间编码段」的是 `Range`（把 start/end 两个 23 位数打包进 48 位）。`Span::get()` 就是靠判断 `number - RANGE_BASE` 是否非负来分辨这两者。

`resolve_span(world, span)` 的判定流程：

```
match span.get():
  Detached ──────────────────────────► return None
  Number { id, num } ─► source = world.source(id)?    // 取已解析的源码
                       range = source.range(num, None)?// 按编号查字节区间
                       line  = source.lines().byte_to_line(range.start)?
  Range  { id, range } ─► file  = world.file(id)?      // 取原始字节
                         lines = file.lines()?         // 当成 UTF-8 文本数行（失败则放弃）
                         line  = lines.byte_to_line(range.start)?

return Some( ( format!("{id:?}"),  line as u32 + 1 ) )
                                └─ 1-indexed，符合人类习惯
```

两个分支最后都把字节起始偏移喂给 `byte_to_line` 得到「行号（0 起）」，再 `+1` 转成「第几行（1 起）」。文件名则是 `FileId` 的 `Debug` 输出（`format!("{id:?}")`）——注意它**不是**磁盘路径，而是一个内部标识的格式化串。

#### 4.4.3 源码精读

**`resolve_span` 全貌**：

[crates/typst-kit/src/timer.rs:98-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L98-L116) 就是上面的流程的落地。`Number` 分支用 `world.source(id).ok()?` 与 `source.range(num, None)?`；`Range` 分支用 `world.file(id).ok()?` 与 `file.lines().ok()?`（`Bytes::lines` 返回 `Result`，非 UTF-8 时为 `Err`，`.ok()?` 直接放弃）。最后统一 `Some((format!("{id:?}"), line as u32 + 1))`。

**`Span` 与 `SpanKind` 的定义**：

[crates/typst-syntax/src/span.rs:62-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs#L62-L83) `Span(NonZeroU64)` 与三变体枚举 `SpanKind`；注释还解释了「编号 span」为什么对编辑稳定、有利于带缓存的求值。`from_raw` 见 [span.rs:140-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs#L140-L142)，它只是把 `NonZeroU64` 包起来。

**`Span::get`——拆出 `SpanKind` 的逻辑**：

[crates/typst-syntax/src/span.rs:177-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs#L177-L187) 先取 `id`（无则 `Detached`），再用 `num.checked_sub(RANGE_BASE)` 判断：能减则把打包值拆回 `(start, end)` 得 `Range`，否则得 `Number`。

**`Source::range`——按编号查字节区间**（`Number` 分支用）：

[crates/typst-syntax/src/source.rs:129-139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L129-L139) 在语法树里找到该编号对应的节点，返回它的字节区间。这正是「编号 span」要回 `Source` 才能定位的原因——编号本身不含位置信息。

**`Bytes::lines`——把原始字节当文本数行**（`Range` 分支用）：

[crates/typst-library/src/foundations/bytes.rs:158-162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L158-L162) 返回 `Result<Lines<String>, Utf8Error>`，且被 `#[comemo::memoize]` 缓存，所以重复对同一文件数行不会反复计算。

**`byte_to_line`——字节偏移转行号**：

[crates/typst-syntax/src/lines.rs:67-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/lines.rs#L67-L74) 用二分查找（`binary_search_by_key`）在每行的起始字节索引里定位，返回 0 起的行号；越界返回 `None`。

#### 4.4.4 代码实践

**实践目标**：阅读 `resolve_span`，说清三种 `SpanKind` 各自如何解析出行号。

**操作步骤**：

1. 打开 [crates/typst-kit/src/timer.rs:98-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs#L98-L116)，对照上面的流程图，为每个 `match` 分支写出「输入 → 用到哪些 World 方法 → 输出」。
2. 若你在 4.1.4 / 4.3.4 生成了 `trace.json`，用文本编辑器打开它，搜索 `"args"`。带有 `(file, line)` 的事件对应 `Number` 或 `Range`；没有 `args` 字段的事件对应 `Detached`（被 `resolve_span` 返回 `None`，但导出端只在 span 存在时才写 `args`，见 `export_json`）。

**需要观察的现象**：

- `Detached` 分支立刻 `return None`，不访问 `World`。
- `Number` 分支访问 `world.source(id)`，再 `source.range(num, None)` 拿到字节区间。
- `Range` 分支访问 `world.file(id)`，再 `file.lines()` 把字节当文本。

**预期结果**：能口头复述「源码文件的耗时事件走 `Number` 分支、外部文件走 `Range` 分支、纯内部计算走 `Detached` 分支」，并解释为什么 `Number` 必须回 `Source` 查位置而 `Range` 不用（前者只存了节点编号、后者直接存了字节区间）。

> 若本地暂无 trace 文件，步骤 2 为「待本地验证」，可仅完成步骤 1 的源码阅读。

#### 4.4.5 小练习与答案

**练习 1**：一个指向某 `.typ` 文件第 10 行的耗时事件，会走 `resolve_span` 的哪个分支？为什么它要先调 `source.range(num, None)` 而不是直接用某个字节偏移？

> **参考答案**：走 `Number { id, num }` 分支。因为 Typst 源码文件用的是**编号 span**——`num` 只是「第几个语法树节点」，不含字节位置。必须先 `source.range(num, None)` 在语法树里把编号翻译成字节区间，再用区间起点 `byte_to_line` 得到行号。

**练习 2**：`Range` 分支里 `file.lines().ok()?` 的 `?` 在什么情况下会提前返回 `None`？这对导出有什么影响？

> **参考答案**：当外部文件不是合法 UTF-8 时，`Bytes::lines()` 返回 `Err(Utf8Error)`，`.ok()?` 把它变成 `None` 提前返回。结果是该事件的 `resolve_span` 返回 `None`，在 `record` 的回调里被 `unwrap_or` 兜底成 `("unknown", 0)`——即导出端**不会因此报错**，只是这条事件拿不到准确行号。

**练习 3**：`resolve_span` 最后返回的「文件名」是 `format!("{id:?}")`，它是一个磁盘路径吗？这会带来什么影响？

> **参考答案**：不是磁盘路径，而是 `FileId` 的 `Debug` 格式化输出（一个内部标识）。所以你在 Perfetto 里看到的 `args.file` 不是 `doc.typ` 这样的真实路径，而是某种内部 id 串。要拿到真实路径，需要集成方额外的映射（`FileId` → 磁盘路径），这超出了 `resolve_span` 的职责。

## 5. 综合实践

把本讲知识串起来，做一个「定位编译瓶颈」的小任务：

1. 用 `typst compile --timings=trace-{n}.json` 对一个**有几十行内容、含图片和包引用**的 `doc.typ` 生成计时文件（图片/包会让 `Range` 分支与文件 IO 相关事件更明显）。
2. 把 `trace-0.json` 拖进 <https://ui.perfetto.dev>。
3. 找到**耗时最长**的那个内层事件（不是最外层的 `compile once`，而是它内部最宽的色块），记下它的名字。
4. 点开该事件查看 `args`：
   - 若有 `(file, line)`，对照 `resolve_span` 判断它来自 `Number` 还是 `Range`，并去对应文件的第 `line` 行看那是什么代码触发的。
   - 若没有 `args`，说明它是 `Detached`（内部计算），解释为什么这类事件定位不到具体文件。
5. 用一句话总结：这次编译最慢的是哪个阶段，它发生在哪里。

> 这个任务同时用到「构造 Timer（4.2）→ record 导出（4.3）→ resolve_span 还原位置（4.4）」三段知识，并复习了「事件由 `#[time]` 产生（4.1）」。若本地无法运行 CLI，可改为：通读 [timer.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/timer.rs) 全文，画出从「用户传 `--timings`」到「JSON 落盘」的完整时序图。

## 6. 本讲小结

- `Timer` 是「周边工具型」积木：不参与编译、不实现 `World`，只负责打开 `typst-timing` 的全局收集开关、运行编译、再把事件导出成 Chrome Tracing 兼容的 JSON。
- 底层 `typst-timing` 用「全局 `AtomicBool` 开关 + 全局 `Mutex<Vec<Event>>` + `TimingScope` 的 RAII（构造记 Start、Drop 记 End）」收集事件；`#[time]` 宏把 `TimingScope` 偷偷插进被标注函数。开关默认关闭，关闭时 `TimingScope` 立刻短路、近乎零开销。
- `Timer` 有「真计时器（`new`）」与「占位器（`placeholder`）」两种人格，`new_or_placeholder` 按运行时 `Option<PathBuf>` 二选一；只有 `new` 会 `enable()`。这让调用方永远写 `timer.record(...)`，无需 if 分支——即「统一代码路径」。
- `record()` 的流程是：占位则透传 → 否则 `clear()` → 校验/替换 `{n}` → 运行闭包 → `iter += 1` → `BufWriter` 包文件 → `export_json(callback)`。多次录制（如 `typst watch`）路径必须含 `{n}`，否则 `bail!`。
- `resolve_span` 是底层「原始数字」到人类「(文件, 行号)」的翻译官：`Detached` 直接放弃；`Number`（源码、编号 span）回 `Source::range` 查字节区间；`Range`（外部文件、字节区间 span）直接数行；最终行号 `+1` 转 1 起，文件名是 `FileId` 的 `Debug` 串而非磁盘路径。

## 7. 下一步学习建议

- **继续本单元**：本单元（u8）还有 u8-l2「DateTime 与可复现构建」，讲解 `Time` 如何为 `World::today` 提供日期。它和 `Timer` 一样是「服务 World 的小积木」，但侧重「可复现构建」而非性能，恰好互补，建议紧接着读。
- **深入底层**：想彻底理解事件收集，可通读 [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-timing/src/lib.rs)，重点看 `export_json` 如何把每条事件序列化成 Chrome 的 `B`/`E`（Begin/End）配对、`ts` 如何取相对首个事件的微秒差、以及它为何用 `std::mem::take` 取走事件以防回调中再次打点导致死锁。
- **关联阅读**：`resolve_span` 用到的 `Source::range` / `byte_to_line` 属于 typst-syntax；编号 span 的「编辑稳定性」与 comemo 的记忆化（memoization）密切相关——如果你对「为什么编辑后增量编译很快」感兴趣，可以从 span 的编号设计出发，去读 typst-engine 里的 comemo 缓存机制。
