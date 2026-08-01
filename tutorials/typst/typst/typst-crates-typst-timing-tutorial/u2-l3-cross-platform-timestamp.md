# 跨平台时间戳：Timestamp 抽象

## 1. 本讲目标

本讲聚焦 typst-timing 内部最「隐藏」的一块——**如何取时间**。

在前面几讲里，我们看到每条 `Event` 都带有一个 `timestamp: Timestamp` 字段，导出时再用它换算成 Chrome Trace 的 `ts`（微秒）。但这个 `Timestamp` 究竟是什么？它在桌面（native）和浏览器（wasm）上为何长得不一样？两条取时路径 `now` 与 `now_with` 又是怎么分工的？

学完本讲，你应当能够：

1. 读懂 `Timestamp` 结构体如何用条件编译在 native（`SystemTime`）与 wasm（`f64`）之间切换内部表示；
2. 看懂 `now` / `now_with` 两条取时路径分别在哪被调用、为什么这么分工；
3. 解释 `micros_since` 如何把两套平台各自的时间差，统一归一化成 Chrome Trace 需要的微秒。

> 本讲承接 [u2-l1 数据模型：Event 与 EventKind](u2-l1-event-data-model.md)：那里我们知道 `Event.timestamp` 记录事件发生时刻，本讲就钻进这个字段背后真正的实现。`thread_id` 的生成细节已在 [u2-l2 全局状态与线程模型](u2-l2-global-state-threading.md) 讲过，本讲会复用其中的 `THREAD_DATA` / `ThreadData` 概念。

## 2. 前置知识

### 2.1 时间单位与 Chrome Trace 的约定

计算机里常见的时间单位从小到大是：纳秒（ns）、微秒（μs）、毫秒（ms）、秒（s）。它们之间是 1000 倍关系：

\[
1\,\text{s} = 10^{3}\,\text{ms} = 10^{6}\,\mu\text{s} = 10^{9}\,\text{ns}
\]

也就是：

\[
1\,\text{ms} = 1000\,\mu\text{s},\qquad 1\,\mu\text{s} = 1000\,\text{ns}
\]

**Chrome Trace（也叫 Perfetto）的事件格式要求 `ts` 字段的单位是微秒（μs）。** typst-timing 最终导出的就是这种格式，所以无论内部用什么单位记时间，最后都必须折算成微秒。这个「最终目标是微秒」的约束，是本讲一切换算的出发点。

### 2.2 Rust 的条件编译 `cfg`

`#[cfg(...)]` 是 Rust 的条件编译属性：只有当括号里的条件成立时，被标注的项才会被编译。本讲会大量见到这几种写法：

- `#[cfg(not(target_arch = "wasm32"))]`：目标平台**不是** wasm32（即 native 桌面/服务器）时编译；
- `#[cfg(target_arch = "wasm32")]`：目标是 wasm32 时编译；
- `#[cfg(all(target_arch = "wasm32", feature = "wasm"))]`：既要是 wasm32，又要启用 `wasm` 这个 Cargo feature（两者「且」）。

`#[cfg]` 让同一份源码可以在不同平台编译出不同的实现，是 typst-timing 跨平台的关键工具。

### 2.3 两种「取时间」的底层来源

- **native（桌面/服务器）**：用标准库的 `std::time::SystemTime::now()`，它返回一个系统墙钟（wall clock）时刻；两个时刻相减得到 `Duration`，`as_nanos()` 给出纳秒整数。注意 `SystemTime` 是墙钟，理论上可能因系统时间被调整而「往回走」。
- **wasm（浏览器/Node）**：`SystemTime` 在 wasm 上不可用。取而代之的是浏览器的 [High Resolution Time API](https://developer.mozilla.org/zh-CN/docs/Web/API/Performance/now)：`performance.now()` 返回一个 `f64`，**单位是毫秒**，但小数部分带有微秒级精度。`performance.timeOrigin` 也是毫秒（相对 Unix 纪元）。

> 关键差异小结：native 的「原子」是 `SystemTime`，差值算出来是**纳秒**；wasm 的「原子」是 `f64`，数值是**毫秒**。两边连基础单位都不一样，所以才需要一层抽象把它们藏起来。

### 2.4 为什么 wasm 上不能直接用 `SystemTime`

`SystemTime` 依赖操作系统的系统调用，wasm 运行在浏览器/JS 引擎里没有这些。typst-timing 通过 Cargo feature `wasm` 拉入 `web-sys`，再去访问 `Performance` 接口。`Cargo.toml` 用了三重门控（见 u1-l1）：

```toml
[target.'cfg(target_arch = "wasm32")'.dependencies]
web-sys = { workspace = true, features = ["Window", "WorkerGlobalScope", "Performance"], optional = true }

[features]
wasm = ["dep:web-sys"]
```

也就是说：`web-sys` 只在 wasm32 目标下、且被 `wasm` feature 拉入时才会进入编译。源码里取时间的实现，也跟着这套 cfg 走，下面逐一展开。

## 3. 本讲源码地图

本讲只涉及一个文件，但会反复跳转到它的不同行段：

| 代码位置 | 作用 |
|---|---|
| `Timestamp` 结构体（`lib.rs:228-235`） | 跨平台时间戳的内部表示，靠 `cfg` 在两种类型间切换 |
| `Timestamp::now`（`lib.rs:238-244`） | 通用取时入口；native 直接取，wasm 回落到线程局部数据 |
| `Timestamp::now_with`（`lib.rs:246-256`） | 复用已有 `ThreadData` 引用的取时入口，三分支条件编译 |
| `Timestamp::micros_since`（`lib.rs:258-269`） | 把两个时间戳的差归一化为微秒（Chrome Trace 所需） |
| `ThreadData`（`lib.rs:272-283`） | 每线程数据，其中在 wasm 下携带一个 `WasmTimer` |
| 调用点 `new_impl`（`lib.rs:180-191`） | 创建 `Start` 事件时，用 `now_with` 取时 |
| 调用点 `Drop::drop`（`lib.rs:194-205`） | 销毁时用 `now` 取 `End` 事件的时间 |
| 消费点 `export_json`（`lib.rs:139`） | 用 `micros_since` 把每个事件时刻折算成相对微秒 |

`WasmTimer` 本身（如何拿到 `Performance`、如何缓存 `time_origin`）属于 [u3-l3 WASM 支持](u3-l3-wasm-support-wasmtimer.md) 的主题，本讲只在需要时简要点到，不展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 条件编译**：`Timestamp` 如何用一个字段、两种类型实现跨平台；
- **4.2 取时路径**：`now` 与 `now_with` 的分工与三个 `cfg` 分支；
- **4.3 单位换算**：`micros_since` 如何把任意时间差折算成微秒。

### 4.1 条件编译：一个结构体，两种内部表示

#### 4.1.1 概念说明

`Timestamp` 是 typst-timing 内部的私有结构体，它的职责只有一件：**在任意平台上，存下一个「时刻」**。问题在于 native 和 wasm 取时间的方式完全不同：native 有 `SystemTime`，wasm 没有，只能存一个 `f64`（毫秒）。

如果用一个 `enum` 把两种情况装在一起，比如 `enum { Sys(SystemTime), Wasm(f64) }`，那么每次都要带一个判别式（discriminant），而且其中一半的变体在当前平台上永远用不上、纯属浪费。typst-timing 选择了更省的办法：**同一个字段名 `inner`，用两条互斥的 `cfg` 给它两种不同的类型定义**，让编译器在各自平台上只看到其中一种。

#### 4.1.2 核心流程

- 当目标平台**不是** wasm32 时：`inner: std::time::SystemTime`；
- 当目标平台**是** wasm32 时：`inner: f64`（毫秒）；
- 两个 `#[cfg]` 互斥（一个 `not(...)`、一个不带 `not`），因此任意一次编译**恰好只有一种** `inner` 被编译进来，零浪费、零判别式；
- 因为 `SystemTime` 和 `f64` 都是 `Copy`，所以整个 `Timestamp` 也派生了 `Copy, Clone`，调用方可以按值传递。

#### 4.1.3 源码精读

结构体定义在这里（注释「A cross-platform way to get the current time.」一行点明了它的定位）：

[crates/typst-timing/src/lib.rs:228-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L228-L235) —— `Timestamp` 结构体：`inner` 字段在两套平台下分别定义为 `SystemTime` 与 `f64`。

```rust
#[derive(Copy, Clone)]
struct Timestamp {
    #[cfg(not(target_arch = "wasm32"))]
    inner: std::time::SystemTime,
    #[cfg(target_arch = "wasm32")]
    inner: f64,
}
```

注意三个细节：

1. **字段同名 `inner`**：因为是条件编译，同一时刻只有一条定义「存活」，所以同名不会冲突。
2. **`#[derive(Copy, Clone)]`**：`Timestamp` 在后续 `micros_since` 里被按值传入（`fn micros_since(self, start: Self)`），需要 `Copy`。
3. **它是私有结构体**（没有 `pub`）：外部根本看不到它，只能通过 `Event.timestamp` 间接持有。这样内部表示怎么换都不影响对外 API。

> 小结：`Timestamp` 用条件编译把「平台差异」压缩进一个字段，对外则彻底透明。这是典型的「把平台脏活藏在内部、对外只暴露抽象」的设计。

#### 4.1.4 代码实践

**目标**：直观感受 native 平台下 `inner` 真的就是标准库的 `SystemTime`。

1. 在 typst-timing 之外，新建一个最小 Rust 程序（示例代码，非项目原有）：

   ```rust
   fn main() {
       let now = std::time::SystemTime::now();
       println!("now = {:?}", now);
       // 尝试取出相对 UNIX_EPOCH 的纳秒数，体会 native 分支的「原子单位」
       let d = now.duration_since(std::time::UNIX_EPOCH).unwrap();
       println!("nanos since epoch = {}", d.as_nanos());
   }
   ```

2. 运行 `cargo run`，观察打印的纳秒数是一个非常大的整数（量级在 \(10^{18}\) 纳秒，对应当前年代）。

3. 对照本讲的 `Timestamp`：在 native 上 `inner` 装的就是这样一个 `SystemTime`。两个 `SystemTime` 相减会得到 `Duration`，其 `.as_nanos()` 正是 4.3 要用的换算入口。

**需要观察的现象**：纳秒整数非常巨大，但量级符合「自 1970 年至今的纳秒数」。**预期结果**：程序正常打印，无报错。（wasm 上的对照行为留待 [u3-l3](u3-l3-wasm-support-wasmtimer.md)，本机无法直接复现 wasm 分支。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Timestamp` 用两个互斥 `cfg` 的同名字段，而不是写成 `enum { Native(SystemTime), Wasm(f64) }`？

> **参考答案**：因为任意一次编译只会落在其中一个平台。用 `enum` 会给所有平台都带上一个永远不用的变体和判别式字节，徒增体积和一次 `match` 的运行时开销；而同名 `cfg` 字段让每个平台只编译进它真正需要的那一种类型，零浪费。

**练习 2**：`Timestamp` 没有标记 `pub`，但 `Event` 里有 `timestamp: Timestamp` 字段，这是否矛盾？

> **参考答案**：不矛盾。`Event` 本身也是私有结构体（仅 crate 内部使用），外部世界永远拿不到 `Event` 或 `Timestamp` 的实例；时间值最终只通过 `export_json` 以序列化后的 `ts: f64`（微秒）形式流出。所以 `Timestamp` 可以安全地保持私有。

---

### 4.2 取时路径：`now` 与 `now_with` 的分工

#### 4.2.1 概念说明

`Timestamp` 提供了两个取「当前时刻」的入口：

- `Timestamp::now()`：通用入口，谁都能调，不依赖任何上下文；
- `Timestamp::now_with(data: &ThreadData)`：**当你手上已经有一个 `ThreadData` 引用时**，复用它来取时间。

为什么要两个？因为在 wasm 上取时间必须借助每线程持有的 `WasmTimer`（它存在 `ThreadData` 里，详见 [u2-l2](u2-l2-global-state-threading.md) 与 [u3-l3](u3-l3-wasm-support-wasmtimer.md)）；而在 native 上取时间根本用不到 `ThreadData`，直接 `SystemTime::now()` 即可。

于是出现两类调用方：

- **`Drop::drop`**：销毁作用域时只需要一个终点时间戳（`thread_id` 等已存在自身字段里），它调用 `now()`。
- **`new_impl`**：创建作用域时本来就要访问 `THREAD_DATA` 去取 `thread_id`。与其先取 id、再单独取时间（访问两次线程局部存储），不如在同一个闭包里**一次**拿到 `(id, timestamp)`，这时就用 `now_with(data)`。

#### 4.2.2 核心流程

`now()` 的取时路径（两个 `cfg` 分支）：

```
now()
├─ wasm32  → 回落到 THREAD_DATA，调用 now_with(data)
└─ 非 wasm32 → 直接 SystemTime::now()，完全不碰 ThreadData
```

`now_with(data)` 的取时路径（**三个** `cfg` 分支）：

```
now_with(data)
├─ wasm32 且 feature=wasm → data.timer.now()  （毫秒 f64）
├─ wasm32 但未开 wasm feature → inner = 0.0    （退化兜底）
└─ 非 wasm32 → 转手调用 now()（即 SystemTime::now()）
```

关键点：**即便目标是 wasm32，如果没启用 `wasm` feature，也没有 `WasmTimer`**。这时 `now_with` 返回 `inner = 0.0` 作为退化兜底——代码仍能编译运行，只是所有时间戳都恒为 0，导出的 trace 会是一条「扁平」的时间轴。这是一种「宁可没精度，也不能编译失败」的优雅降级。

#### 4.2.3 源码精读

先看通用入口 `now`：

[crates/typst-timing/src/lib.rs:238-244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L238-L244) —— `now()`：wasm 下经 `THREAD_DATA` 委托给 `now_with`；native 下直接取 `SystemTime`。

```rust
fn now() -> Self {
    #[cfg(target_arch = "wasm32")]
    return THREAD_DATA.with(Self::now_with);

    #[cfg(not(target_arch = "wasm32"))]
    Self { inner: std::time::SystemTime::now() }
}
```

- wasm 分支用 `THREAD_DATA.with(Self::now_with)`：把线程局部的 `ThreadData` 引用传给 `now_with`，由后者真正取时间；
- native 分支则完全不碰 `THREAD_DATA`，一行 `SystemTime::now()` 搞定。

再看带 `data` 的入口 `now_with`：

[crates/typst-timing/src/lib.rs:246-256](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L246-L256) —— `now_with(data)`：三个 `cfg` 分支，分别处理「wasm+feature」「wasm 无 feature」「native」。

```rust
#[allow(unused_variables)]
fn now_with(data: &ThreadData) -> Self {
    #[cfg(all(target_arch = "wasm32", feature = "wasm"))]
    return Self { inner: data.timer.now() };

    #[cfg(all(target_arch = "wasm32", not(feature = "wasm")))]
    return Self { inner: 0.0 };

    #[cfg(not(target_arch = "wasm32"))]
    Self::now()
}
```

这里有个容易忽略的小标记 **`#[allow(unused_variables)]`**：参数 `data` 只在「wasm + feature」分支里被用到（`data.timer.now()`）。在 native 或「wasm 无 feature」编译条件下，`data` 根本没被使用，编译器本会警告「未使用变量」。加这条 allow 就是为了在不同平台编译时都保持安静。

两个真实调用点，对照起来看就明白了：

[crates/typst-timing/src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) —— `new_impl`：已经在 `THREAD_DATA.with` 闭包里，**同一个闭包**既取 `id` 又用 `now_with(data)` 取时间，避免访问线程局部存储两次。

```rust
fn new_impl(name: &'static str, span: Option<NonZeroU64>) -> Self {
    let (thread_id, timestamp) =
        THREAD_DATA.with(|data| (data.id, Timestamp::now_with(data)));
    EVENTS.lock().push(Event { /* ... */ timestamp, /* ... */ });
    // ...
}
```

[crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205) —— `Drop::drop`：销毁时只需要终点时间戳，直接调 `now()`（wasm 下它内部会自己去摸 `THREAD_DATA`）。

```rust
impl Drop for TimingScope {
    fn drop(&mut self) {
        let timestamp = Timestamp::now();
        EVENTS.lock().push(Event { /* ... */ timestamp, /* ... */ });
        // ...
    }
}
```

> 设计直觉：`now` 是「不依赖上下文」的对外门面，`now_with` 是「已有 `ThreadData`」的复用入口。两者在 native 下殊途同归（最终都是 `SystemTime::now()`），在 wasm 下则都必须经过线程局部数据里的 `WasmTimer`——只是入口时机不同。

#### 4.2.4 代码实践

**目标**：跟踪一次 `timed!` 调用，弄清它的两个事件各走哪条取时路径。

1. 回顾 `timed!(\"foo\", expr)` 会展开为（见 u1-l2）：

   ```rust
   let __scope = TimingScope::new(\"foo\");  // 创建 → Start 事件
   expr                                        // 执行被包裹代码
   // __scope 离开作用域 → Drop → End 事件
   ```

2. 阅读源码填空（**源码阅读型实践**）：

   | 时刻 | 触发 | 取时调用 | 在 native 上实际执行 |
   |---|---|---|---|
   | 作用域创建 | `new_impl` | `Timestamp::now_with(data)` | → `Self::now()` → `SystemTime::now()` |
   | 作用域销毁 | `Drop::drop` | `Timestamp::now()` | → `SystemTime::now()` |

3. **需要观察的现象**：在 native 上，Start 与 End 两条事件的时间戳，最终都落在 `SystemTime::now()`；二者唯一的区别是「Start 顺带在同一闭包里取了 `thread_id`，End 没有再取（直接复用存好的 `self.thread_id`）」。

**预期结果**：你能用自己的话解释「为什么 `new_impl` 用 `now_with`、`Drop` 用 `now`」——前者复用已有的线程局部数据访问，后者只缺一个时间戳、且自己没有 `ThreadData` 引用。

#### 4.2.5 小练习与答案

**练习 1**：`now_with` 上的 `#[allow(unused_variables)]` 是为了放过什么警告？为什么必须加？

> **参考答案**：参数 `data` 只在 `cfg(all(wasm32, feature=\"wasm\"))` 分支用到。当编译目标是 native，或 wasm 但未启用 feature 时，`data` 全程未用，会触发「unused variable: `data`」警告。因为这是同一段源码在不同平台编译，总有平台会触发该警告，所以统一 allow 掉。

**练习 2**：如果有人在 wasm32 目标下，**不**启用 `wasm` feature 去运行 typst-timing，导出的 trace 会是什么样？

> **参考答案**：此时 `now_with` 恒返回 `inner = 0.0`，所有事件的 `timestamp.inner` 都是 0.0。于是 `micros_since` 对任意一对事件都算出 0.0 微秒，导出的 trace 里所有事件挤在 `ts=0` 同一瞬间，时间轴变成一条「扁平」的竖线——程序不崩溃，但计时信息全无意义。这是有意的优雅降级。

**练习 3**：`now()` 在 wasm 分支里用 `THREAD_DATA.with(Self::now_with)`，把 `now_with` 当闭包传进去。`now_with` 的参数从哪来？

> **参考答案**：`THREAD_DATA.with(closure)` 会把当前线程的那份 `ThreadData` 的引用 `&ThreadData` 作为参数传给闭包。所以 `now_with` 的 `data` 就是这条闭包被调用时由 `with` 注入的线程局部数据引用。

---

### 4.3 单位换算：`micros_since` 把任意时间差归一化为微秒

#### 4.3.1 概念说明

前面看到，native 的 `inner` 是 `SystemTime`，wasm 的 `inner` 是「毫秒的 `f64`」。两套基础单位南辕北辙，但 Chrome Trace 只认微秒。`micros_since(self, start)` 就是那个「翻译官」：给定起点 `start` 和终点 `self`，无论底层是什么，都算出「经过多少微秒」。

它的名字直白地表达了意图：`micros_since` = **micros**econds **since**（某起点以来过了多少微秒）。

#### 4.3.2 核心流程

两套平台，两种换算：

\[
\Delta_{\mu\text{s}} =
\begin{cases}
\dfrac{\text{duration\_since}(\text{start})\text{.as\_nanos}()\ \text{as f64}}{1000}, & \text{native（纳秒} \to \text{微秒：除以 }1000\text{）} \\[6pt]
\bigl(\text{inner}_{\text{end}} - \text{inner}_{\text{start}}\bigr) \times 1000, & \text{wasm（毫秒} \to \text{微秒：乘以 }1000\text{）}
\end{cases}
\]

- **native**：先用 `self.inner.duration_since(start.inner)` 得到 `Duration`，`.as_nanos()` 取纳秒整数（`u128`），`as f64` 转成浮点，最后 `/ 1000.0` 得到微秒（因为 \(1\,\mu\text{s} = 1000\,\text{ns}\)）。
- **wasm**：`self.inner` 与 `start.inner` 都是毫秒 `f64`，相减得到毫秒差，`* 1000.0` 转成微秒（因为 \(1\,\text{ms} = 1000\,\mu\text{s}\)）。这里能保住微秒精度，是因为 `performance.now()` 的毫秒值在小数部分本身就携带亚毫秒精度。

两条路殊途同归，都输出微秒 `f64`，正好喂给 Chrome Trace 的 `ts`。

> 关于稳健性：native 分支用 `.unwrap_or(std::time::Duration::ZERO)`。`SystemTime::duration_since(earlier)` 在「`self` 早于 `earlier`」时会返回 `Err`——这通常意味着墙钟「往回走」（系统时间被调整、NTP 校时跳变等）。一旦出错，就用 `Duration::ZERO` 兜底，相当于「这段时长记为 0」，避免整个计时因一次时间倒流而 panic。这是面向生产环境的防御性写法。

#### 4.3.3 源码精读

[crates/typst-timing/src/lib.rs:258-269](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L258-L269) —— `micros_since`：wasm 分支做「毫秒×1000」，native 分支做「纳秒÷1000」，并带 `unwrap_or(ZERO)` 防御时间倒流。

```rust
fn micros_since(self, start: Self) -> f64 {
    #[cfg(target_arch = "wasm32")]
    return (self.inner - start.inner) * 1000.0;

    #[cfg(not(target_arch = "wasm32"))]
    (self
        .inner
        .duration_since(start.inner)
        .unwrap_or(std::time::Duration::ZERO)
        .as_nanos() as f64
        / 1_000.0)
}
```

逐行拆解 native 分支：

1. `self.inner.duration_since(start.inner)` —— 两个 `SystemTime` 相减，返回 `Result<Duration, SystemTimeError>`；
2. `.unwrap_or(std::time::Duration::ZERO)` —— 若相减失败（终点早于起点），兜底为零时长；
3. `.as_nanos()` —— `Duration` 转成纳秒整数，类型是 `u128`；
4. `as f64` —— 转成 64 位浮点，为后续除法做准备；
5. `/ 1_000.0` —— 纳秒除以 1000 得微秒。

对照消费点，看它最终怎么被用：

[crates/typst-timing/src/lib.rs:139](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L139) —— `export_json` 中 `ts: event.timestamp.micros_since(events[0].timestamp)`：每个事件都相对**第一条事件**算微秒偏移。

```rust
ts: event.timestamp.micros_since(events[0].timestamp),
```

也就是说，导出时把**首条事件**当作时间零点，其后每个事件都折算成「相对零点过了多少微秒」。这正是 Chrome Trace 期望的相对时间表示。

#### 4.3.4 代码实践

这是本讲的主实践（源码阅读 + 单位推理型）。

**目标**：亲手把 native 分支的纳秒→微秒换算逐行注释清楚，并解释 wasm 分支为什么是「乘以 1000」、`unwrap_or(ZERO)` 为何重要。

**操作步骤**：

1. 打开 `crates/typst-timing/src/lib.rs` 的 `micros_since`（第 258–269 行）。
2. 在你的笔记里，给 native 分支（`#[cfg(not(target_arch = "wasm32"))]`）每一步加上中文注释。参考答案（注释版）：

   ```rust
   // native：SystemTime 差值 → 微秒
   (self
       .inner
       .duration_since(start.inner)           // ① 两个 SystemTime 相减 → Result<Duration, _>
       .unwrap_or(std::time::Duration::ZERO)  // ② 时间倒流时兜底为 0，避免 panic
       .as_nanos()                            // ③ 取纳秒，得到 u128
       as f64                                 // ④ 转为 f64 以便做浮点除法
       / 1_000.0)                             // ⑤ 纳秒 ÷ 1000 = 微秒
   ```

3. 用一句话回答两个问题（写进笔记）：
   - **wasm 分支为什么是 `* 1000.0`？** —— 因为 wasm 的 `inner` 是毫秒（`performance.now()` 返回毫秒），毫秒变微秒要乘以 1000（\(1\,\text{ms} = 1000\,\mu\text{s}\)）。
   - **`unwrap_or(std::time::Duration::ZERO)` 的稳健性意义是什么？** —— `SystemTime` 是墙钟，可能因系统时间被回调而出现「终点早于起点」，此时 `duration_since` 返回 `Err`；用零时长兜底，保证计时不会因偶发的时间倒流而崩溃，是面向生产的防御写法。

**需要观察的现象**：你能向自己解释清楚「同样是换算成微秒，为什么 native 要除以 1000、wasm 却要乘以 1000」——因为两者的原始单位不同（ns vs ms），分别处于微秒的两侧。

**预期结果**：注释完整、两点解释自洽。若想进一步验证，可在 native 下写小程序：取两个 `SystemTime`（中间 `sleep` 2 毫秒），套用同样的 `.duration_since(..).unwrap_or(ZERO).as_nanos() as f64 / 1000.0`，应得到约 `2000`（微秒）。**待本地验证**（结果受调度精度影响，允许有偏差）。

#### 4.3.5 小练习与答案

**练习 1**：`micros_since` 为什么以 `self` 作为终点、`start` 作为参数，而不是 `micros_until(end)`？

> **参考答案**：这与调用点契合——`export_json` 写成 `event.timestamp.micros_since(events[0].timestamp)`，语义是「当前事件相对首事件过了多少微秒」。把终点设为 `self`、起点设为参数，读起来就是「我（this 时刻）距起点多久」，符合自然语言顺序。

**练习 2**：native 分支为什么先 `.as_nanos()` 再 `as f64`，而不是先转 `f64` 再算？

> **参考答案**：`as_nanos()` 返回的是精确的 `u128` 整数纳秒数；先拿到这个精确整数，再一次性 `as f64`，能最大限度保留精度。如果用别的方式（例如把 `Duration` 先变秒再乘回去）会引入更多浮点运算误差。这里在转成 `f64` 前都保持整数运算，精度更可控。

**练习 3**：假设在 wasm（启用 `wasm` feature）下，`performance.now()` 的两次返回值是 `12.345` 与 `13.500`（毫秒），算出的微秒差是多少？

> **参考答案**：`(13.500 - 12.345) * 1000.0 = 1.155 * 1000 = 1155`（微秒）。可见 wasm 分支保留了亚毫秒精度（这里小数 0.155 ms 被还原成 155 μs）。

## 5. 综合实践

把本讲三块内容串起来，做一次「**端到端时间推理**」。

**任务**：在 native 平台，预测下面这段程序导出 JSON 后，那条 `End` 事件的 `ts` 字段大约是多少（单位微秒）。

```rust
fn main() {
    typst_timing::enable();
    timed!("sleep", std::thread::sleep(std::time::Duration::from_millis(2)));
    let mut buf = Vec::new();
    typst_timing::export_json(&mut buf, |_| ("demo.rs".to_string(), 1)).unwrap();
    println!("{}", String::from_utf8(buf).unwrap());
}
```

**请按下列步骤推理（写进笔记）**：

1. **取时**：`timed!` 展开后，创建作用域触发 `new_impl` → 用 `Timestamp::now_with(data)`（native 上即 `SystemTime::now()`）记下 `Start`；睡眠 2ms 后 `Drop` 触发 → 用 `Timestamp::now()` 记下 `End`。两条时间戳都是 `SystemTime`。
2. **换算**：`export_json` 用 `micros_since` 把每条事件折算成相对首条的微秒。首条是 `Start`，它相对自己 = 0；`End` 相对 `Start` ≈ 睡眠时长 2ms。
3. **单位**：native 分支走 `纳秒 ÷ 1000`。2ms ≈ \(2 \times 10^6\) ns，÷ 1000 = 2000 μs。

**预期结果**：导出 JSON 里那条 `End`（`\"ph\":\"E\"`）事件的 `ts` 应在 `2000` 上下（微秒），`Start`（`\"ph\":\"B\"`）的 `ts` 为 `0`。允许因线程调度而略有偏差。

**进阶观察**：

- 把睡眠时间改成 `from_millis(5)`，`End.ts` 应约 `5000`；
- 对照 wasm 分支：若是 wasm，`End - Start` 的毫秒差 ≈ 2.0，`× 1000` 同样得到 ≈ 2000 μs——**两套换算殊途同归**，这正是 `micros_since` 存在的意义。

> 这是一个纯推理型实践，不需要修改源码；如果你愿意，可以在本地新建一个依赖 typst-timing 的小 crate 真实运行一遍，对照预测值。若无法运行 wasm 分支，标注「待本地验证」即可，不要假装已经跑过。

## 6. 本讲小结

- `Timestamp` 是私有跨平台时间戳结构体，靠**互斥的 `#[cfg]`** 让单一字段 `inner` 在 native 下是 `SystemTime`、在 wasm 下是 `f64`（毫秒），实现零判别式的平台切换。
- 取时有两条入口：通用 `now()` 与复用 `ThreadData` 的 `now_with(data)`。native 下二者都落到 `SystemTime::now()`；wasm 下都必须借助每线程的 `WasmTimer`。
- `now_with` 有**三个** `cfg` 分支——「wasm+feature」「wasm 无 feature」「native」，其中无 feature 时返回 `0.0` 是优雅降级，保证代码始终能编译运行。
- 调用分工：`new_impl` 在访问线程局部数据时顺手用 `now_with` 取 `Start` 时间；`Drop::drop` 只缺时间戳，用 `now()` 取 `End` 时间。
- `micros_since` 是「翻译官」：native 把纳秒 `÷ 1000`、wasm 把毫秒 `× 1000`，两路统一输出微秒 `f64`，正好满足 Chrome Trace 的 `ts` 约定。
- native 分支的 `.unwrap_or(Duration::ZERO)` 防御 `SystemTime` 墙钟倒流，是面向生产的稳健写法。

## 7. 下一步学习建议

本讲把 `Timestamp` 的「存储 + 取时 + 换算」讲完了，但刻意没展开 `WasmTimer` 内部。建议接着学：

- **[u3-l3 WASM 支持与 WebAssembly 计时：WasmTimer](u3-l3-wasm-support-wasmtimer.md)**：弄清 `WasmTimer::new` 如何先尝试 `window().performance()`、再回退到 `WorkerGlobalScope`，以及为何每线程要缓存 `time_origin` 并在 `now()` 里叠加，以保证多线程时间戳落在同一基准。
- **[u2-l4 导出 Chrome Trace JSON：export_json](u2-l4-export-chrome-trace-json.md)**：看 `micros_since` 产出的微秒如何被组装进 Chrome Trace 的 `B`/`E` 事件、`pid`/`tid`/`args` 字段，完成从「原始时间戳」到「可视化 trace」的最后一公里。

如果想从源头回顾「这些时间戳是怎么被写进事件的」，可重读 [u2-l1 数据模型](u2-l1-event-data-model.md) 的 `TimingScope` RAII 部分。
