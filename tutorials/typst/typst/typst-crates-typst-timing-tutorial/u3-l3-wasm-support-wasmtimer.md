# WASM 支持与 WebAssembly 计时：WasmTimer

## 1. 本讲目标

在 [u2-l3 跨平台时间戳：Timestamp 抽象](u2-l3-cross-platform-timestamp.md) 里，我们已经知道 `Timestamp` 在 wasm 上把内部表示换成了一个 `f64`（毫秒），并预留了一句「`WasmTimer` 内部留待后续讲义」。本讲就钻进这条 wasm 分支的内部：那个 `f64` 到底从哪来、为什么必须先拿到一个 `Performance` 句柄、又为什么要把一个叫 `time_origin` 的值叠加到每次取时上。

学完本讲，你应当能够：

1. 解释 typst-timing 用「双层 cfg + optional feature」把 wasm 相关代码与 `web-sys` 依赖隔离的三层门控机制；
2. 读懂 `WasmTimer::new` 如何先尝试 `window()`、再回退到 `WorkerGlobalScope`，最终拿到 `Performance` 句柄；
3. 论证为何要为每个线程缓存一份 `time_origin` 并在 `now()` 里叠加，才能让多线程的事件时间戳落在同一时间基准；
4. 说明未启用 `wasm` feature 时 wasm 分支返回 `0.0` 的「优雅退化」行为及其代价。

## 2. 前置知识

### 2.1 浏览器的 performance.now() 与 timeOrigin

`Performance` 是浏览器（及 Web Worker）提供的高分辨率计时接口。两个关键成员：

- `performance.now()`：返回一个 `DOMHighResTimeStamp`（`f64`，**单位毫秒**），但它**不是**「从 Unix 纪元起的绝对时间」，而是相对于当前上下文「时间起点（time origin）」的毫秒数。
- `performance.timeOrigin`：返回该上下文的「时间起点」，单位毫秒，是一个**类绝对时间戳**（在主线程 window 上下文中，约为导航开始时刻距 Unix 纪元的毫秒数）。

这里藏着一个最容易踩的坑：**不同的全局上下文（主线程 window、每个 Web Worker）各自有不同的 time origin。** 主线程的 `performance.now()` 与某个 Worker 里的 `performance.now()`，是从两个不同「零点」开始数的，**直接相减没有意义**。这正是本讲 `WasmTimer` 要解决的核心问题。

### 2.2 cfg 条件编译与 optional feature（复习）

`#[cfg(predicate)]` 让一段代码只在满足条件时被编译；Cargo 的 optional 依赖（`optional = true`）配合 `[features]` 里的 `dep:xxx`，可以把一个依赖做成「按需引入」，不启用 feature 时不参与编译。typst-timing 把这两者叠加，实现了 wasm 子系统的彻底隔离。具体 cfg 写法在 [u2-l3 的 2.2 节](u2-l3-cross-platform-timestamp.md) 已介绍，本讲只关注它们如何**组合成多层门控**。

### 2.3 thread_local 复习（来自 u2-l2）

typst-timing 用 `thread_local!` 的 `THREAD_DATA` 给每个线程存一份 `ThreadData`（含自造 `u64` 线程 id，见 [u2-l2](u2-l2-global-state-threading.md)）。在 wasm 场景下，这份 per-thread 数据会**额外多挂一个 `WasmTimer`**——这就是 `Performance` 句柄的栖身之处。

## 3. 本讲源码地图

本讲只涉及两个文件，且真正的新主角只是 `src/lib.rs` 末尾一段：

| 文件 | 关键行 | 作用 |
|---|---|---|
| `crates/typst-timing/Cargo.toml` | L20-L24 | 声明 wasm 专属 optional 依赖 `web-sys`，并定义 `wasm` feature |
| `crates/typst-timing/src/lib.rs` | L46-L58 | `THREAD_DATA` thread_local，wasm 下多挂一个 `WasmTimer`（字段在 L55-L56） |
| `crates/typst-timing/src/lib.rs` | L246-L256 | `Timestamp::now_with` 的三条 cfg 分支（含退化分支返回 `0.0`） |
| `crates/typst-timing/src/lib.rs` | L272-L283 | `ThreadData` 结构，wasm 下含 `timer: WasmTimer` 字段 |
| `crates/typst-timing/src/lib.rs` | L285-L320 | `WasmTimer` 定义与实现：`new` 取句柄、`now` 叠加 time_origin |

其余章节的脚手架（`Timestamp`、`micros_since` 的整体框架）已在 u2-l3 讲过，本讲只引用、不重述。

## 4. 核心概念与源码讲解

### 4.1 双层网关：wasm feature 与 cfg(target_arch = "wasm32")

#### 4.1.1 概念说明

typst-timing 是一个被 syntax、eval、layout、render、pdf、cli 等十余个 crate 依赖的基础设施 crate。它必须同时满足两个看似矛盾的诉求：

1. 在 wasm32 目标上，要能调用浏览器的 `Performance` API 获取高精度时间；
2. 在 native（Linux / macOS / Windows）目标上，**完全不能**拉入 `web-sys`——否则原生编译会因为缺失浏览器 API 而出错、或体积暴涨。

而且 `web-sys` 是个体积大、编译慢的依赖，即便在 wasm32 目标上，也应该让它「可选」：不需要真实计时的人不必为它付出编译时间与体积代价。为此 typst-timing 用了**三层隔离**——两道在 `Cargo.toml`，一道在源码里——把 wasm 计时能力彻底关进笼子。

#### 4.1.2 核心流程

「要让 `WasmTimer` 这段代码被编译」需要同时满足三个条件，缺一不可：

| 条件 | 形式 | 出处 |
|---|---|---|
| A：编译目标架构是 wasm32 | `cfg(target_arch = "wasm32")` | Cargo.toml + 源码 |
| B：使用者显式开启 wasm feature | `feature = "wasm"` | Cargo.toml + 源码 |
| C：web-sys 依赖被真正拉入 | 由 A+B 推出 | Cargo.toml |

三层门控的分工：

| 层 | 位置 | 形式 | 拦下什么 |
|---|---|---|---|
| ① 架构层 | `Cargo.toml` 的 `[target.'cfg(target_arch = "wasm32")'.dependencies]` | `web-sys` 只在 wasm32 目标声明 | native 目标根本看不到 `web-sys` |
| ② 可选层 | `Cargo.toml` 的 `optional = true` + `features.wasm = ["dep:web-sys"]` | 默认不拉 `web-sys` | 不开 feature 的 wasm 用户也不付出代价 |
| ③ 源码层 | `lib.rs` 的 `#[cfg(all(target_arch = "wasm32", feature = "wasm"))]` | 两条件同时成立才编译 | 源码里所有 `web_sys::` 引用被双重保护 |

注意 ③ 用的是 `all(target_arch = "wasm32", feature = "wasm")`，而不是只写 `feature = "wasm"`。这很重要：即便有人误在 native 上开了 `wasm` feature，源码里的 `web_sys::` 调用也不会被编译（虽然那时 Cargo 会因 ① 拉不到 `web-sys` 而先报错，但源码层的 cfg 是第二道保险，让语义自洽：凡用到 `web_sys` 的地方都显式声明「我只在 wasm32 上有意义」）。

#### 4.1.3 源码精读

`Cargo.toml` 里 wasm 依赖与 feature 的声明：

[Cargo.toml:L20-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L20-L24) —— ①②层：wasm32 目标下可选引入 `web-sys`，并定义 `wasm` feature：

```toml
[target.'cfg(target_arch = "wasm32")'.dependencies]
web-sys = { workspace = true, features = ["Window", "WorkerGlobalScope", "Performance"], optional = true }

[features]
wasm = ["dep:web-sys"]
```

- `[target.'cfg(target_arch = "wasm32")'.dependencies]`：`web-sys` 只在 wasm32 目标被声明——第 ① 层。
- `optional = true` 且 `features = ["Window", "WorkerGlobalScope", "Performance"]`：把 `web-sys` 设为可选依赖，并只启用三个子 feature（`Performance` 是计时接口本体，`Window` / `WorkerGlobalScope` 是获取它的两个入口，对应 4.2）。
- `wasm = ["dep:web-sys"]`：第 ② 层。用 `dep:` 语法显式声明「开启 wasm feature 等价于拉入 optional 依赖 `web-sys`」。不开 feature，`web-sys` 不会被编译。

源码层的双重 cfg（第 ③ 层），以 `ThreadData` 字段与 `WasmTimer` 类型本身为例：

[lib.rs:L281-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L281-L283) —— `ThreadData` 里只在 wasm+feature 下才有 `timer` 字段：

```rust
#[cfg(all(target_arch = "wasm32", feature = "wasm"))]
timer: WasmTimer,
```

[lib.rs:L285-L292](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L285-L292) —— `WasmTimer` 类型本身整段只在 wasm+feature 下存在：

```rust
#[cfg(all(target_arch = "wasm32", feature = "wasm"))]
struct WasmTimer {
    perf: web_sys::Performance,
    time_origin: f64,
}
```

这道 `all(target_arch = "wasm32", feature = "wasm")` 在 `THREAD_DATA`（L55-L56）、`now_with` 分支（L248-L249）、`ThreadData` 字段（L281-L282）、`WasmTimer` 类型（L286）、`impl WasmTimer`（L294）共 **5 处**重复出现，确保整个 wasm 计时子系统「不满足条件就当不存在」。

#### 4.1.4 代码实践

**实践目标**：验证「不开 wasm feature 时，wasm32 目标也能编译，且 `web-sys` 不参与编译；开了 feature 才会被拉入」。

**操作步骤**（需要本机有 `wasm32-unknown-unknown` 工具链；若无，改用文末的阅读型替代实践）：

1. 进入 typst-timing 目录，**不**带 feature 编译：
   ```
   cargo build --target wasm32-unknown-unknown -p typst-timing
   ```
2. 加上 `--features wasm` 再编译一次：
   ```
   cargo build --target wasm32-unknown-unknown -p typst-timing --features wasm
   ```
3. 用 `cargo tree` 对比两次的依赖图：
   ```
   cargo tree -p typst-timing --target wasm32-unknown-unknown
   cargo tree -p typst-timing --target wasm32-unknown-unknown --features wasm
   ```

**需要观察的现象**：

- 第 1 步（不带 feature）应当成功编译。此时 `Timestamp::now_with` 走退化分支（见 4.3.4），`WasmTimer` 根本不存在。
- 第 2 步（带 feature）会真正拉入并编译 `web-sys`，编译耗时明显变长。
- `cargo tree` 的第一次输出里**没有** `web-sys`，第二次里有。

**预期结果**：两个命令都能编译通过；区别仅在于 `web-sys` 是否进入依赖图。**待本地验证**（取决于本地是否装有 wasm32 工具链）。

**阅读型替代实践**（无需 wasm 工具链）：用搜索工具统计 `all(target_arch = "wasm32", feature = "wasm")` 在 `src/lib.rs` 中出现的次数与位置（应为 5 处，见 4.1.3），确认整条 wasm 子系统都被同一道双重 cfg 包裹，没有任何一处「裸用」`web_sys::`（所有 `web_sys::` 调用都在被这层 cfg 保护的块内）。

#### 4.1.5 小练习与答案

**练习 1**：如果把源码里的 cfg 从 `all(target_arch = "wasm32", feature = "wasm")` 改成只写 `feature = "wasm"`，会出什么问题？

> **参考答案**：逻辑上，在 native 目标误开 `wasm` feature 时，源码里的 `web_sys::` 调用会被尝试编译。但由于 `Cargo.toml` 第 ① 层把 `web-sys` 限制在 wasm32 目标，native 下根本拉不到 `web-sys`，仍会编译失败——Cargo.toml 的 ① 层先拦住了。保留 `all(...)` 是「纵深防御」，让源码层语义自洽：凡是用到 `web_sys` 的地方都显式声明「我只在 wasm32 上有意义」。

**练习 2**：为什么 `web-sys` 要用 `optional = true`，而不是无条件（即便已限定在 wasm32 目标下）引入？

> **参考答案**：即便限定在 wasm32 目标，`web-sys` 仍是体积大、编译慢的依赖。`optional` + `wasm` feature 让「只想要 typst-timing 的埋点、不需要真正计时」的 wasm 使用者（如默认嵌入场景）可以不付出 `web-sys` 的编译代价，只在需要 profiling 时才显式开启。这是「默认关闭、按需开启」哲学在依赖维度的体现。

### 4.2 WasmTimer::new：从 window 到 WorkerGlobalScope 获取 performance

#### 4.2.1 概念说明

即便在 wasm32 + wasm feature 下，要拿到 `Performance` 句柄也不直接。问题在于：**JS 的全局对象不止一种**。

- 在浏览器主线程，全局对象是 `window`，`performance` 挂在 `window` 上。
- 在 Web Worker（含 Service Worker）里，没有 `window`，全局对象是 `WorkerGlobalScope`，`performance` 挂在它上面。

Typst 在 wasm 上既可能跑在主线程（如交互式编辑器），也可能跑在 Worker 里（如后台编译）。`WasmTimer::new` 必须两种情况都照顾到，所以采用「先试主线程、失败再回退到 Worker」的策略。

#### 4.2.2 核心流程

获取 `Performance` 句柄的回退链（伪代码）：

```
perf =
  先试 window：
    web_sys::window()                 // 主线程才有 window，否则 None
      .and_then(|w| w.performance())  // 取 window.performance
  失败则回退到 Worker：
    .or_else(||
      global()                                 // 拿到真正的全局对象 globalThis
        .dyn_into::<WorkerGlobalScope>()       // 尝试转成 Worker 全局
        .ok()                                  // 转失败 -> None
        .and_then(|scope| scope.performance()) // 取 performance
    )
  都失败则 panic：
    .expect("failed to get JS performance handle")
```

直观的决策树：

```
有 window 吗？
 ├─ 有 -> 取 window.performance -> 成功？
 │         ├─ 是 -> 用它
 │         └─ 否 -> 去 Worker 分支
 └─ 无 -> 去 Worker 分支
            global() -> 能转成 WorkerGlobalScope 吗？
             ├─ 是 -> 取 scope.performance -> 用它（或 None 后 panic）
             └─ 否 -> panic
```

关键细节：`or_else` 是**惰性**的——只有 `window()` 这条路返回 `None`（或 `window.performance()` 为 `None`）时，才会去求值 Worker 分支里的闭包，避免在主线程做无谓的 JS 类型探测调用。

#### 4.2.3 源码精读

`WasmTimer::new` 的获取链路：

[lib.rs:L296-L315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L296-L315) —— 拿 perf 句柄，再缓存 time_origin：

```rust
fn new() -> Self {
    let perf = web_sys::window()
        .and_then(|window| window.performance())
        .or_else(|| {
            use web_sys::wasm_bindgen::JsCast;
            web_sys::js_sys::global()
                .dyn_into::<web_sys::WorkerGlobalScope>()
                .ok()
                .and_then(|scope| scope.performance())
        })
        .expect("failed to get JS performance handle");

    let time_origin = perf.time_origin();
    Self { perf, time_origin }
}
```

逐点说明：

- `web_sys::window()`：返回主线程 window 对象的 `Option`。在 Worker 里它返回 `None`，这是触发回退到第二条路的条件。
- `.and_then(|window| window.performance())`：用 `and_then` 把「有 window」和「window 上能取到 performance」两步串起来，任一失败都给出 `None`，交给下一步 `or_else`。
- `.or_else(|| { ... })`：前一条路失败时才执行。闭包内：
  - `use web_sys::wasm_bindgen::JsCast;`：把 `dyn_into` 所需的 trait 局部引入（只有 Worker 场景才用到，故放在闭包内而非文件顶部）。
  - `web_sys::js_sys::global()`：拿到 `globalThis`（任何 JS 环境都存在；主线程下它指向 window，所以这里其实是 Worker 场景的兜底）。
  - `.dyn_into::<WorkerGlobalScope>()`：尝试把任意 JS 值「向下转型」为 `WorkerGlobalScope`。转型失败返回 `Err`，`.ok()` 转成 `None`；Worker 下成功返回 `Ok(scope)`。
  - `.and_then(|scope| scope.performance())`：从 Worker 全局取 `performance`。
- `.expect("failed to get JS performance handle")`：两条路都拿不到时直接 panic。这是「环境连 Performance 都没有」的硬故障，与其返回错误的时间，不如尽早暴露（错误哲学的讨论见 4.2.5）。
- `let time_origin = perf.time_origin();`：缓存时间起点（详见 4.3）。`Self { perf, time_origin }` 把两个值都存进 `WasmTimer`。

#### 4.2.4 代码实践

**实践目标**：把 `WasmTimer::new` 的获取链路画成流程图，并解释每一步在「主线程 / Worker」两种场景下分别走哪条分支。

**操作步骤**：

1. 打开 [lib.rs:L296-L315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L296-L315)。
2. 照 4.2.2 的决策树手绘 `window() → and_then(performance) → or_else(global/dyn_into/performance) → expect` 的链路。
3. 在图上标注两个场景：
   - 场景 A（主线程）：`web_sys::window()` 返回 `Some`，走 `and_then` 分支，`or_else` **不执行**（闭包体被跳过）。
   - 场景 B（Worker）：`web_sys::window()` 返回 `None`，触发 `or_else`，`global().dyn_into::<WorkerGlobalScope>()` 成功。

**需要观察的现象**：能清楚说出 `or_else` 的惰性——主线程场景下闭包体（含 `JsCast` 的 `use`、`dyn_into` 调用）完全不执行。

**预期结果**：得到一张清晰标注两个场景的流程图，并口头/文字解释「为何 `window` 优先、`WorkerGlobalScope` 兜底」而不是反过来（因为主线程有 `window`，直接取最便宜、语义最直接；Worker 没有窗口对象，才需要绕 `global()` + 类型转换）。

> 本实践为源码阅读型，不需运行即可完成。若想真机验证，需要 `wasm-pack` + 浏览器/Node 环境，属**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Worker 全局用的是 `js_sys::global().dyn_into::<WorkerGlobalScope>()`，而不是某个类似 `web_sys::worker_global_scope()` 的直接函数？

> **参考答案**：`web-sys` 没有提供「直接拿当前 Worker 全局」的统一函数，因为「当前全局对象是什么」取决于运行环境。`js_sys::global()` 取 `globalThis`（任何 JS 环境都存在），再用 `dyn_into` 尝试转型为具体类型，是 `wasm-bindgen` 里「探测当前全局类型」的惯用法。这正好配合 `web_sys::window()` 失败后的回退：主线程用 window、Worker 用 globalThis 转型。

**练习 2**：最后一步用 `.expect(...)`（会 panic）而非返回 `Result`。这样设计合理吗？

> **参考答案**：在 typst-timing 的设计里，`WasmTimer::new` 是 thread_local 首次访问时构造的，初始化器不返回 `Result`，没有向上传播错误的通道。更关键的是，如果环境连 `Performance` 都拿不到，计时数据本身就无意义；与其静默返回错误时间戳，不如直接 panic 让集成方尽早发现环境问题。这与「未启用 feature 时返回 `0.0` 优雅退化」（见 4.3）是两种**有意为之**的不同哲学：feature 没开是**合法选择**（返回 0），而 feature 开了却拿不到句柄是**环境异常**（panic）。

### 4.3 time_origin 缓存与多线程时间一致性

#### 4.3.1 概念说明

这是本讲最核心、也最容易被忽略的设计点。回顾前置知识 2.1 的陷阱：**主线程和每个 Worker 的 `performance.now()` 各自从不同的 time origin 起算**。

如果 typst 在 wasm 上启用了多线程（比如 Worker 池），那么：

- 主线程的 `perf.now()` = 距「主线程导航开始」的毫秒数；
- Worker A 的 `perf.now()` = 距「Worker A 创建时刻」的毫秒数；
- Worker B 的 `perf.now()` = 距「Worker B 创建时刻」的毫秒数。

三组数字彼此独立、零点不同。如果直接把它们当作时间戳塞进同一份 Chrome Trace，导出后的时间轴会**错乱**——不同线程的事件相对位置完全失真，甚至可能出现「某 Worker 事件的时间戳比主线程事件更小」的倒挂。

解决办法：把各自的 `time_origin`（一个类绝对毫秒时间戳）加到 `perf.now()` 上，得到一个**接近绝对时间**的值：

\[
\text{绝对时间戳} = \text{time\_origin} + \text{perf.now()}
\]

加完之后，无论来自哪个线程/上下文，所有时间戳都落在同一条「距公共基准的毫秒轴」上，彼此可比较。

#### 4.3.2 核心流程

`time_origin` 的生命周期与用法：

```
① 线程首次访问 THREAD_DATA -> WasmTimer::new() 被调用一次（per-thread）
② new() 内：time_origin = perf.time_origin()   // 该上下文的时间起点，缓存进字段
③ 之后该线程每次取时间 -> WasmTimer::now()
④ now() 内：return time_origin + perf.now()    // 叠加成类绝对时间
```

数学上，设线程 X 的事件 \(e_1, e_2\)、线程 Y 的事件 \(e_3\)，它们的时间戳为：

\[
T(e_1) = O_X + n_1,\qquad T(e_2) = O_X + n_2,\qquad T(e_3) = O_Y + n_3
\]

其中 \(O_X, O_Y\) 是各自的 `time_origin`，\(n_i\) 是各自的 `perf.now()`。Chrome Trace 在 `micros_since` 时计算两两之差：

\[
T(e_2) - T(e_1) = n_2 - n_1
\]

同线程时 origin 抵消，得到真实耗时；

\[
T(e_3) - T(e_1) = (O_Y - O_X) + (n_3 - n_1)
\]

跨线程时 origin 之差 \(O_Y - O_X\) 正好把两个线程零点的偏移补上。因此无论同线程还是跨线程，差值都落在统一的时间基准上。

#### 4.3.3 源码精读

`time_origin` 在 `new()` 中被缓存（注意源码里的注释一语中的）：

[lib.rs:L310-L314](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L310-L314) —— 缓存该线程的 time_origin：

```rust
// Every thread gets its own time origin. To make the results consistent
// across threads, we need to add this to each `now()` call.
let time_origin = perf.time_origin();

Self { perf, time_origin }
```

注释说得很直白：每个线程都有自己的 time origin，为使结果跨线程一致，必须把它加到每次 `now()` 上。

`now()` 把两值叠加：

[lib.rs:L317-L319](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L317-L319) —— 返回类绝对毫秒时间戳：

```rust
fn now(&self) -> f64 {
    self.time_origin + self.perf.now()
}
```

这个值（毫秒，类绝对时间）会被 `Timestamp::now_with` 装进 `Timestamp.inner`：

[lib.rs:L246-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L246-L249) —— wasm+feature 下用 `WasmTimer` 取时：

```rust
fn now_with(data: &ThreadData) -> Self {
    #[cfg(all(target_arch = "wasm32", feature = "wasm"))]
    return Self { inner: data.timer.now() };
    // ...
}
```

随后导出时，`micros_since` 把毫秒差换算成微秒（乘 1000，单位换算的整体框架见 [u2-l3](u2-l3-cross-platform-timestamp.md)）：

[lib.rs:L259-L260](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L259-L260) —— wasm 分支：差值（毫秒）×1000 → 微秒：

```rust
#[cfg(target_arch = "wasm32")]
return (self.inner - start.inner) * 1000.0;
```

正因为 `inner` 已经是「类绝对时间（毫秒）」，这里只需做一次减法再换算单位，跨线程差值天然正确——这就是 4.3.1 那套数学推导落到源码上的样子。

#### 4.3.4 代码实践（含退化行为讨论）

**实践目标**：解释每线程 `time_origin` 的来源，论证「叠加后多线程时间戳落在同一基准」；并分析未启用 `wasm` feature 时 wasm 分支返回 `0.0` 的退化行为。

**操作步骤**：

1. 阅读 [lib.rs:L296-L319](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L296-L319)，回答下面三个问题（见「需观察的现象」）。
2. 再阅读退化分支 [lib.rs:L251-L252](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L251-L252)：

```rust
#[cfg(all(target_arch = "wasm32", not(feature = "wasm")))]
return Self { inner: 0.0 };
```

3. 追踪：当 `inner` 恒为 `0.0` 时，[lib.rs:L259-L260](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L259-L260) 的 `micros_since` 会算出什么。

**需观察的现象（请用文字回答 A/B/C 三问）**：

- **问题 A**：每线程的 `time_origin` 来自哪里？为何它能把多线程时间戳拉到同一基准？
  - 参考思路：它来自该线程首次构造 `WasmTimer` 时调用 `perf.time_origin()` 的返回值（即该 JS 上下文的「时间起点」类绝对毫秒数）。因为不同上下文的 `perf.now()` 零点不同，加上各自的 origin 后，所有时间戳都变成「距公共纪元的毫秒数」，于是可比。
- **问题 B**：如果**不**叠加 `time_origin`，直接用 `perf.now()`，多线程时间轴会怎样？
  - 参考思路：各线程零点不同，导出到 Chrome Trace 后，不同线程的事件会在时间轴上整体错位，甚至出现「某 Worker 事件比主线程事件时间戳更小」的倒挂，时间轴失真。
- **问题 C（退化行为）**：未启用 `wasm` feature 时，`now_with` 走 `not(feature = "wasm")` 分支返回 `0.0`，会发生什么？

**预期结果**：

对于问题 C，当 `inner` 恒为 `0.0`：

\[
\text{micros\_since} = (0.0 - 0.0) \times 1000.0 = 0.0
\]

即**所有事件的 `ts` 都是 0**，导出的 Chrome Trace 里所有事件挤在同一时刻、无法分辨先后与耗时。这是一种「优雅退化」：计时数据虽无意义，但代码仍能正常编译与运行、不会 panic，也不需要 `web-sys`。使用者若想要真实计时，显式开启 `wasm` feature 即可切换到 `WasmTimer` 分支拿到真实时间。

把 A/B/C 三问的答案整理成一段说明文字，作为本实践的产出。

> 本实践为源码阅读 + 推理型，无需运行。若要真机对比「叠加 origin vs 不叠加」，需要在浏览器里手写两段 JS（分别打印 `performance.now()` 与 `performance.timeOrigin + performance.now()`），在主线程与 Worker 各打印一次，观察后者跨线程可比、前者不可比——属**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`time_origin` 是 `WasmTimer` 的字段（缓存），而不是每次 `now()` 都重新调用 `perf.time_origin()`。这样缓存有什么好处？

> **参考答案**：一是性能——`now()` 是热路径（每次取时间戳都调用），缓存成字段避免每次都跨 wasm 边界调 JS 取 origin；二是语义稳定——`timeOrigin` 在一个上下文生命周期内是常量，缓存它既无正确性损失，又能保证该线程所有时间戳用同一个基准；三是与「per-thread 构造一次」的 thread_local 模型契合，`WasmTimer::new` 只在线程首次访问时跑一次，天然适合做这种一次性缓存。

**练习 2**：`now()` 返回的是毫秒，而 Chrome Trace 的 `ts` 要求微秒。这个换算在哪一步完成？为什么放在那里？

> **参考答案**：在 `micros_since` 里完成（`(self.inner - start.inner) * 1000.0`）。放在「求差之后」而不是「存进 `inner` 之前」换算，是因为最终被导出的只有差值；存原始毫秒在 `inner`、统一在导出端换算，逻辑更内聚，也与 native 分支（存 `SystemTime`、差值再换算）保持对称。

**练习 3**（综合题）：在「未启用 wasm feature」的退化模式下，`enable()` + `timed!` + `export_json` 整条链路还能跑通吗？产出有意义的时间轴吗？

> **参考答案**：能跑通。`enable()` 仍会打开开关，`timed!` 仍会 push Start/End 事件（只是 timestamp 都是 `0.0`），`export_json` 仍会序列化出合法的 JSON（只是所有事件 `ts` 都是 0）。也就是说，链路不报错、文件可生成、Chrome Trace 能打开，但时间轴上所有事件重叠在同一时刻，**没有实际的计时价值**。这正是「优雅退化」的含义：功能降级但不崩溃，需要真实数据时开 feature 即可。

## 5. 综合实践

把本讲三块内容串成一个端到端的「wasm 计时链路」追踪任务。

**任务**：以一次 wasm 端的 `timed!("parse", source)` 调用为起点，画出从「取时间」到「导出 JSON」的完整链路，并标注每一步在本讲三个模块中的归属。

**步骤**：假设已 `enable()` 且开启了 `wasm` feature。读者顺着下列编号填空（给出文件/行号或文字说明）：

1. ① `timed!` 展开后，`TimingScope::new_impl` 在 [lib.rs:L180-L191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) 取 Start 时间，它调用的是哪个函数？（答：`Timestamp::now_with`，见 [lib.rs:L246-L256](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L246-L256)）
2. ② 在 wasm+feature 下，`now_with` 最终落到 `data.timer.now()`（[lib.rs:L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L249)）。`data.timer` 是谁、何时构造的？（答：是该线程 `THREAD_DATA` 里的 `WasmTimer`，首次访问时由 `WasmTimer::new` 构造——属 4.2）
3. ③ `WasmTimer::new` 里 `perf` 句柄怎么拿到、`time_origin` 怎么来的？（答：window → WorkerGlobalScope 回退取 perf；`time_origin = perf.time_origin()` 缓存——属 4.2 + 4.3）
4. ④ `timer.now()` 返回什么表达式？（答：`self.time_origin + self.perf.now()`——属 4.3）
5. ⑤ 作用域结束时 `Drop::drop`（[lib.rs:L194-L205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)）取 End 时间，复用同一条取时路径。
6. ⑥ `export_json` 时，wasm 的 `micros_since`（[lib.rs:L259-L260](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L259-L260)）如何把两个类绝对毫秒时间戳变成 Chrome Trace 的微秒 `ts`？跨线程为何仍正确？（答：做差再 ×1000；因 `inner` 已含 `time_origin`，跨线程差值已落在同一基准——属 4.3）

把 ①–⑥ 画成一条竖向时间线，左边标「发生在哪个模块（4.1 / 4.2 / 4.3）」，右边标「对应源码行号」。

**预期结果**：得到一张「取时入口（Timestamp）→ 句柄获取（WasmTimer::new）→ 基准对齐（time_origin 叠加）→ 差值换算（micros_since）」的完整链路图，能说清三个模块如何接力，以及为什么多线程时间戳最终是一致的。

## 6. 本讲小结

- typst-timing 用**三层门控**隔离 wasm 计时：`Cargo.toml` 里 `cfg(target_arch="wasm32")` 限定目标、`optional = true` + `wasm = ["dep:web-sys"]` 让依赖可选；源码里再叠 `cfg(all(target_arch="wasm32", feature="wasm"))` 双重保护，native 下完全不碰 `web-sys`，且该双重 cfg 在源码中共出现 5 处。
- `WasmTimer::new` 用**回退链**取 `Performance` 句柄：先 `web_sys::window()` 取主线程 window 的 performance，失败再用 `js_sys::global().dyn_into::<WorkerGlobalScope>()` 从 Worker 全局取，兼顾主线程与 Worker 两种 wasm 运行环境；`or_else` 保证主线程不执行兜底闭包。
- **多线程时间一致性**是核心：每个线程各自缓存一份 `time_origin`（来自该上下文的 `perf.time_origin()`），`now()` 返回 `time_origin + perf.now()`，把各自零点不同的 `perf.now()` 拉到同一类绝对时间基准，跨线程时间戳因此可比、`micros_since` 做差天然正确。
- **优雅退化**：wasm32 目标但未开 `wasm` feature 时，`now_with` 返回 `0.0`，所有事件 `ts` 归零、时间轴无意义但不崩溃，也不需 `web-sys`。
- 句柄拿不到时用 `.expect(...)` **panic**（环境异常），与「feature 没开返回 `0.0`」（合法选择）形成两种有意的错误哲学。
- `time_origin` 缓存成字段而非每次重取，既省 wasm/JS 边界调用、又契合 thread_local「per-thread 构造一次」的模型。

## 7. 下一步学习建议

- 接 **u3-l4 集成实践：typst-kit Timer 与端到端计时导出**：看真实集成方 `typst-kit` 如何把 `enable → clear → 执行被计时函数 → export_json` 串成一条产品级链路，并经 `World` 把裸 span 还原回 `(file, line)`，正好消费本讲产出的事件流。
- 重读 [u2-l3 跨平台时间戳：Timestamp 抽象](u2-l3-cross-platform-timestamp.md)：带着本讲对 `time_origin + perf.now()` 的理解，回看 `Timestamp` 如何用同一字段名 `inner` + 两条 cfg 实现零判别式平台切换，体会「上层抽象统一、底层平台各异」的分层设计。
- 想深入了解 `performance.now()` / `timeOrigin` 的规范语义，可阅读 MDN 的 [Performance.now()](https://developer.mozilla.org/en-US/docs/Web/API/Performance/now) 与 [Performance.timeOrigin](https://developer.mozilla.org/en-US/docs/Web/API/Performance/timeOrigin)（外部资料，仅供拓展）。
- 若要在自己项目里跑一遍 wasm 计时，建议先装 `wasm32-unknown-unknown` 工具链并按 4.1.4 的步骤确认 feature 开关效果，再结合 `wasm-pack` 在浏览器/Node 里观察真实 `ts` 值。
