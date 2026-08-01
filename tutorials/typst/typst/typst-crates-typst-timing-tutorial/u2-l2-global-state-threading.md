# 全局状态与线程模型：ENABLED、EVENTS 与 thread_local

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `typst-timing` 用哪两个**全局静态量**来承载「开关」和「事件缓冲区」，以及它们各自的数据类型与并发角色。
- 解释 `THREAD_DATA` 这个 **thread_local** 存了什么、它的 `id` 字段是怎么被「每线程自动分配」出来的。
- 论证为什么全文件所有原子操作都用 `Ordering::Relaxed`，并能区分「只需要原子性」与「需要发布/同步其它数据」两种场景。
- 独立写出一个**多线程计时示例**，并通过导出的 JSON 里 `tid` 字段确认不同线程被正确区分。

本讲是上一讲《u2-l1 数据模型：Event 与 EventKind》的延续。上一讲我们看清了一条事件 `Event` 长什么样、`TimingScope` 如何用 RAII 配对 Start/End；本讲回答一个更底层的问题：**这些事件到底存到哪里、由谁写、在多线程下如何做到既正确又几乎零成本？**

## 2. 前置知识

本讲会用到几个 Rust 并发术语，先用大白话过一遍。如果你已经很熟，可以跳到第 3 节。

### 2.1 回顾：Event 与 TimingScope（来自 u2-l1）

- 每条计时记录是一个 `Event`，字段包括 `kind`（`Start`/`End`）、`timestamp`、`name`、`span`、`thread_id`。
- `TimingScope` 是一个「信物」：创建时记一条 `Start`，`Drop`（离开作用域）时记一条 `End`。`timed!` 宏只是把表达式包进一个 `TimingScope`。

本讲关心的正是「记一条」这个动作最终落到**哪个全局变量**上，以及 `thread_id` 这个字段从哪来。

### 2.2 全局静态量 `static`

`static ENABLED: AtomicBool = ...;` 声明的是一个**进程级、存活整个程序**的变量。和普通 `let` 不同，它在编译期就确定初值，所有线程共享同一份。Rust 要求 `static` 的类型是 `Sync`（可安全地被多线程并发访问），这正是下面用 `AtomicBool` / `Mutex` 的原因。

### 2.3 原子类型与内存序 `Ordering`

- `AtomicBool` / `AtomicU64`：对一个布尔/整数的读、写、自增是**不可分割**的（atomic），不会出现「半个值」。
- `Ordering`：规定这次原子操作**对其它内存的可见性**有多强。本讲用到的三种里，`Relaxed` 最弱、最快——只保证这次操作本身原子，不保证它前后的其它读写有顺序关系。

> 一句话：**原子性**解决「不会读到半截值」；**内存序**解决「我这次写，别的线程什么时候能看见」。

### 2.4 互斥锁 `Mutex`

`Mutex<Vec<Event>>` 是一把「带守卫」的锁。`.lock()` 会阻塞到拿到锁，返回一个守卫，通过守卫读写内部数据，守卫离开作用域时自动放锁。所以多个线程同时往同一个 `Vec` 里 `push` 是安全的。

### 2.5 `thread_local!`

`thread_local!` 声明的变量，**每个线程各有一份独立的拷贝**，互不可见。访问它用 `.with(|data| ...)`。线程首次访问时才懒初始化，之后就是廉价的线程本地读取。

## 3. 本讲源码地图

本讲全部内容集中在 crate 的唯一源码文件里：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | 全部实现。本讲重点看其中三处：① `ENABLED`/`EVENTS` 静态量；② `THREAD_DATA` 的 `thread_local!` 块；③ `ThreadData` 结构与 `Ordering::Relaxed` 注释。 |

无需阅读其它 crate 即可学完本讲。

## 4. 核心概念与源码讲解

### 4.1 全局开关与事件缓冲区：ENABLED 与 EVENTS

#### 4.1.1 概念说明

`typst-timing` 的埋点宏 `timed!`（以及 `#[time]` 属性）散落在 syntax、eval、layout、render 等十几个 crate 的无数函数里。如果要让每条埋点都「记住自己存到哪」，就得把一个缓冲区顺着函数签名层层传递——这在 Typst 这种巨型代码库里几乎不可行。

于是 crate 用了**两个进程级全局静态量**来一劳永逸地承载状态：

1. **`ENABLED`：一个布尔开关**，回答「现在到底要不要计时？」。默认关闭（`false`），`timed!` 在关闭时几乎不花成本。
2. **`EVENTS`：一个事件缓冲区**，回答「记下的事件存哪？」。所有线程的 Start/End 最终都 `push` 进这同一个 `Vec`。

这是典型的「用全局可变状态换取埋点的人体工学」的设计取舍：状态全局可见，埋点调用零参数、零上下文。

#### 4.1.2 核心流程

整条「开关 + 缓冲」的联动可以画成：

```
enable()  ──store(true)──▶  ENABLED = true
                                │
   任意线程的 timed!(...)      │ 读 is_enabled()
        │                      ▼
        └──TimingScope::new ──▶ 若 enabled：
                                   EVENTS.lock().push(Start)
                              ……执行被计时代码……
                              Drop ──▶ EVENTS.lock().push(End)

export_json(W, source) ──▶ EVENTS.lock() 取出全部事件
                           序列化为 Chrome Trace JSON
                           （取出的同时缓冲区被清空）
```

要点：

- **读**：每个 `timed!` 都先读 `ENABLED` 决定走不走「记录」分支（u1-l2 讲过，关闭时 `TimingScope::new` 直接返回 `None`）。
- **写**：只有在开启状态下，`TimingScope` 创建与 `Drop` 才会各自 `push` 一条到 `EVENTS`。
- **导出即清空**：`export_json` 用 `std::mem::take` 把 `EVENTS` 掏空再序列化，所以导出后缓冲区自然变空（见 u2-l4）。

#### 4.1.3 源码精读

两个静态量的声明，紧挨在一起：

[crates/typst-timing/src/lib.rs:L60-L64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L60-L64) — 声明全局开关 `ENABLED`（`AtomicBool`，初值 `false`）与全局事件缓冲区 `EVENTS`（`Mutex<Vec<Event>>`）。`AtomicBool` 与 `Mutex` 都满足 `Sync`，所以能放进 `static`。

```rust
/// Whether the timer is enabled. Defaults to `false`.
static ENABLED: AtomicBool = AtomicBool::new(false);

/// The list of collected events.
static EVENTS: Mutex<Vec<Event>> = Mutex::new(Vec::new());
```

围绕开关的三个读/写函数——注意它们各自只有一行核心操作：

[crates/typst-timing/src/lib.rs:L66-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L66-L92) — `enable`/`disable`/`is_enabled` 读写 `ENABLED`，`clear` 清空 `EVENTS`。`#[inline]` 把这些单行函数内联进调用点，消除函数调用开销（u3-l1 会展开讲零成本设计）。

```rust
#[inline]
pub fn enable() {
    ENABLED.store(true, Ordering::Relaxed);
}
#[inline]
pub fn disable() {
    ENABLED.store(false, Ordering::Relaxed);
}
#[inline]
pub fn is_enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}
#[inline]
pub fn clear() {
    EVENTS.lock().clear();
}
```

真正「写一条事件」的地方在 `TimingScope` 里——创建时 push Start，Drop 时 push End，两次都经过 `EVENTS.lock()`：

[crates/typst-timing/src/lib.rs:L180-L205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L205) — `new_impl` 在创建时 push 一条 `Start`；`Drop::drop` 在销毁时 push 一条 `End`。两处都先 `EVENTS.lock()` 拿到守卫再 `push`，保证多线程并发写不会损坏 `Vec`。

```rust
fn new_impl(name: &'static str, span: Option<NonZeroU64>) -> Self {
    let (thread_id, timestamp) =
        THREAD_DATA.with(|data| (data.id, Timestamp::now_with(data)));  // ← thread_id 来源见 4.2
    EVENTS.lock().push(Event {
        kind: EventKind::Start, timestamp, name, span, thread_id,
    });
    Self { name, span, thread_id }
}

impl Drop for TimingScope {
    fn drop(&mut self) {
        let timestamp = Timestamp::now();
        EVENTS.lock().push(Event {
            kind: EventKind::End, timestamp,
            name: self.name, span: self.span, thread_id: self.thread_id,
        });
    }
}
```

#### 4.1.4 代码实践

**目标**：直观感受 `ENABLED` 的「门控」效果——关闭时不产生任何事件，开启后才写缓冲区。

下面是**示例代码**（非项目原有代码）。由于 `typst-timing` 是 Typst workspace 的内部 crate，未发布到 crates.io，要运行需用 `path` 或 `git` 依赖指向本仓库。最终输出**待本地验证**。

```rust
// 示例代码：演示 ENABLED 的门控效果
use typst_timing::{disable, enable, is_enabled, export_json, timed};

fn dump(label: &str) {
    // export_json 内部会用 std::mem::take 取出并清空 EVENTS
    let mut buf = Vec::new();
    export_json(&mut buf, |_| ("demo.rs".to_string(), 1)).unwrap();
    println!("{label}: {}", String::from_utf8(buf).unwrap());
}

fn main() {
    println!("默认状态 is_enabled = {}", is_enabled()); // false

    // ① 关闭状态：下面的 timed! 不会产生任何事件
    timed!("when disabled", { let _ = 1 + 1; });
    dump("关闭时导出"); // 期望: []（空数组）

    // ② 开启状态：timed! 产生一对 Start/End
    enable();
    println!("开启后 is_enabled = {}", is_enabled()); // true
    timed!("when enabled", { let _ = 2 + 2; });
    dump("开启时导出"); // 期望: 含两条事件（B 与 E）

    disable();
}
```

**操作步骤**：把上面的代码放进一个依赖了 `typst-timing` 的二进制 crate 的 `main.rs`，编译运行。

**观察与预期**：

- `关闭时导出` 打印 `[]`——说明 `timed!` 在 `is_enabled()` 为 `false` 时根本没碰 `EVENTS`。
- `开启时导出` 打印一个含两个对象（`"ph":"B"` 与 `"ph":"E"`）的 JSON 数组——说明开关闭合后事件才被写入。
- 注意：每次 `export_json` 都会清空缓冲区，所以「关闭时导出」即便此前有残留也会被掏空。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `EVENTS` 从 `Mutex<Vec<Event>>` 换成裸 `Vec<Event>`（不加 `Mutex`），在多线程下会出什么问题？

**答案**：`Vec` 的 `push` 不是原子的，多个线程并发 `push` 会导致数据竞争（竞争条件），轻则丢失事件、指针错乱，重则触发未定义行为（UB）。`Vec` 也不是 `Sync`，根本无法放进 `static`。`Mutex` 把「拿锁→改→放锁」串行化，才是多线程安全写入的正解。

**练习 2**：为什么 `clear()` 在导出场景里几乎用不到？

**答案**：因为 `export_json` 内部已经用 `std::mem::take` 把 `EVENTS` 掏空了（参见第 4.1.3 节），导出本身就「顺便清空」。`clear()` 主要用于「不导出、只想丢弃当前记录」的场合（例如 typst-kit 的 `Timer::record` 在正式计时前会先 `clear()` 清掉上一次的残留，见 u3-l4）。

---

### 4.2 每线程数据：THREAD_DATA 与自定义 u64 thread id

#### 4.2.1 概念说明

每个 `Event` 都带一个 `thread_id: u64` 字段。导出成 Chrome Trace 后，它就是时间轴上的 `tid`——有了它，Chrome/Perfetto 才能把不同线程的事件排到不同的**泳道（track）**上，让你一眼看出哪段耗时来自哪个线程。

`typst-timing` 没有直接用 `std::thread::current().id()`，而是**自己用一个 `u64` 计数器给每个线程发号**，并把这个号连同（wasm 下的）计时器一起存进一个 `thread_local`：

- **`THREAD_DATA`**：一个 `thread_local!`，每个线程各持有一份 `ThreadData`。
- **`ThreadData::id`**：本线程的编号，由一个进程级 `AtomicU64` 计数器在「该线程首次访问 `THREAD_DATA` 时」分配。

为什么自造轮子而不是用标准库？源码注释给了两条理由（见 4.2.3）：① **wasm 兼容**；② 更**便宜**（标准库版本会克隆一个 `Arc`）。

#### 4.2.2 核心流程

这里有一个非常容易混淆、但又必须想清楚的点：**「进程共享的计数器」与「每线程的存储」是两样东西**。

```
程序启动：COUNTER = 1 （static，全进程唯一）

线程 A 首次访问 THREAD_DATA ──▶ 初始化器执行：
    COUNTER.fetch_add(1, Relaxed)   返回旧值 1 → A.id = 1，COUNTER 变 2
    （同时构造 wasm 下的 WasmTimer，存进 A 的 ThreadData）

线程 B 首次访问 THREAD_DATA ──▶ 初始化器再执行：
    COUNTER.fetch_add(1, Relaxed)   返回旧值 2 → B.id = 2，COUNTER 变 3

此后 A、B 各自访问自己的 ThreadData：
    THREAD_DATA.with(|d| d.id)   ←  廉价的线程本地读取，返回 1 或 2
```

要点：

- **`static COUNTER` 是真静态量**：它被声明在 `thread_local!` 的初始化表达式**内部**，但 `static` 的语义是「整个程序只有一份」，所以它是**全进程共享**的计数器，不是每线程一份。
- **初始化器是每线程执行一次**：`thread_local!` 的初始化闭包/表达式在**每个线程首次访问该变量时**各跑一次。于是「同一个 `COUNTER` 被 `fetch_add`」恰好实现了「给每个线程发一个不重复的号」。
- **`fetch_add` 返回的是旧值**：初值设为 `1`，第一个访问的线程拿到 `1`，第二个拿到 `2`……保证所有 `tid` 都是非零且互不相同的（Chrome Trace 对 tid 没有非零要求，这里只是顺带方便）。

> 记住这个区分：**`COUNTER` 是「大家共用的发号机」，`THREAD_DATA` 是「每人一个的储物柜」**。发号机进程唯一，储物柜每线程一份。

#### 4.2.3 源码精读

`thread_local!` 块——注意 `id` 字段的初值表达式里嵌着一个 `static COUNTER`：

[crates/typst-timing/src/lib.rs:L46-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L46-L58) — 声明每线程数据 `THREAD_DATA`。`id` 由内嵌的 `static COUNTER: AtomicU64`（初值 1）经 `fetch_add` 分配；wasm 目标下还顺带构造一个 `WasmTimer`（u3-l3 详讲）。

```rust
thread_local! {
    /// Data that is initialized once per thread.
    static THREAD_DATA: ThreadData = ThreadData {
        id: {
            // We only need atomicity and no synchronization of other
            // operations, so `Relaxed` is fine.
            static COUNTER: AtomicU64 = AtomicU64::new(1);
            COUNTER.fetch_add(1, Ordering::Relaxed)
        },
        #[cfg(all(target_arch = "wasm32", feature = "wasm"))]
        timer: WasmTimer::new(),
    };
}
```

`ThreadData` 结构体及其上方的注释——这里就是「为什么不用标准库」的官方解释：

[crates/typst-timing/src/lib.rs:L272-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L272-L283) — `ThreadData` 只有两个字段：`id: u64`（本线程编号）和 wasm 下的 `timer: WasmTimer`。注释明确说明自造 `u64` id 相比 `std::thread::current().id()` 的两点优势。

```rust
/// Per-thread data.
struct ThreadData {
    /// The thread's ID.
    ///
    /// In contrast to `std::thread::current().id()`, this is wasm-compatible
    /// and also a bit cheaper to access because the std version does a bit more
    /// stuff (including cloning an `Arc`).
    id: u64,
    /// A way to get the time from WebAssembly.
    #[cfg(all(target_arch = "wasm32", feature = "wasm"))]
    timer: WasmTimer,
}
```

最后看消费侧：`TimingScope::new_impl` 如何取到 `thread_id`：

[crates/typst-timing/src/lib.rs:L180-L191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) — `THREAD_DATA.with(|data| (data.id, Timestamp::now_with(data)))` 一次性取出本线程 `id` 与当前时间戳。`id` 是个普通 `u64` 字段读取，几乎零开销；它随后被存进 `TimingScope`，`Drop` 时原样抄进 `End` 事件，从而保证一对 Start/End 的 `thread_id` 严格一致。

**两条注释理由展开**（结合源码与 Rust 运行时常识）：

- **wasm 兼容**：在 `wasm32-unknown-unknown` 这类目标上，Rust 标准库的线程模型受限（多数情况是单线程），`std::thread::current()` 的行为/可用性不可靠。自造 `u64` 完全不依赖标准库的线程身份机制，在 native 与 wasm 上走同一条代码路径，行为可预测。
- **更便宜**：`std::thread::current()` 内部要访问线程本地存储并构造/克隆 `Thread` 句柄（涉及一个 `Arc` 的引用计数操作）；而这里把一个裸 `u64` 存进自己的 `thread_local`，后续读取就是一次普通字段访问，无需克隆、无需 `Arc`。对于一个可能被「每秒上百万次」调用的埋点，这个差距有意义。

#### 4.2.4 代码实践

**目标**：这是一个**源码阅读型实践**——亲手追踪 `thread_id` 的生成与复用路径，理清「发号机」与「储物柜」的关系。

**操作步骤**：

1. 打开 [crates/typst-timing/src/lib.rs:L46-L58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L46-L58)，确认 `static COUNTER` 是嵌在 `thread_local!` 初始化表达式**内部**的 `static`。
2. 回答：`COUNTER` 是「每线程一份」还是「全进程唯一」？为什么？（提示：`static` 的语义与 `thread_local!` 的语义分别由谁决定？）
3. 跳到 [L180-L191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191)，确认 `thread_id` 只在创建 `TimingScope` 时取一次，并存进结构体字段。
4. 跳到 [L194-L205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)，确认 `Drop` 时直接复用 `self.thread_id`，**不再**访问 `THREAD_DATA`。

**需要观察/确认的现象**：

- `thread_id` 在 Start 与 End 之间不会被重新查询，因此即便线程在执行被计时代码期间「身份」有变化，Start/End 的 `tid` 也必然相同——这正是 Chrome Trace 能正确配对 B/E 的前提。
- `COUNTER` 永远只增不减：每个新线程（哪怕旧线程已退出）都会拿到一个更大的号。

**预期结果**：你能用自己的话讲清楚「为什么发号机是 `static`、储物柜是 `thread_local`」，以及「Start/End 共享同一个 tid」是如何由代码结构保证的。

#### 4.2.5 小练习与答案

**练习 1**：`COUNTER` 的初值为什么是 `1` 而不是 `0`？如果改成 `0` 会怎样？

**答案**：这与本 crate 把 `span` 用 `Option<NonZeroU64>` 表示、`thread_id` 用 `u64` 表示的设计有关。`thread_id` 本身是 `u64`（可为 0），所以改 `0` 在功能上也能跑。但初值取 `1` 让首个 tid 为 `1`，避免和「未初始化/空」这类用 `0` 表示的哨兵值混淆，是一种更稳健的约定。更关键的是 `fetch_add` 返回的是**旧值**：初值 1 → 第一个线程拿 1，第二个拿 2……保证互不相同。

**练习 2**：`THREAD_DATA` 的初始化器，每个线程会执行几次？

**答案**：每个线程**至多一次**，且是在该线程**首次访问** `THREAD_DATA` 时才执行（懒初始化）。之后同一线程再访问，直接读取已构造好的 `ThreadData`，不再执行 `fetch_add`。所以一个线程的 `id` 在其生命周期内是稳定不变的。

---

### 4.3 内存序选择：为什么 Ordering::Relaxed 就够了

#### 4.3.1 概念说明

`lib.rs` 里**所有**原子操作——`ENABLED` 的读/写、`COUNTER` 的自增——清一色用 `Ordering::Relaxed`。这不是随手写的，每处都附了同一句注释：

> We only need atomicity and no synchronization of other operations, so `Relaxed` is fine.

这句话是本节的全部核心。要理解它，先区分两件事：

- **原子性（atomicity）**：这个操作本身不可分割。`fetch_add` 不会读到「半加完」的值，两个并发 `fetch_add` 不会丢失其中一次。
- **同步/可见性（synchronization）**：这个操作是否强制「它前后的其它读写」对别的线程也按某种顺序可见。`Release`/`Acquire`/`SeqCst` 会插入内存屏障（fence）来提供这种保证，代价更高。

`Relaxed` **只要原子性，不要任何额外的同步**。它的核心承诺只有一条：所有线程对该原子变量的修改有一个**单一全局顺序**（modification order）。对于「我只是想知道现在开没开 / 我只是想要一个不重复的号」这类用途，这就够了。

#### 4.3.2 核心流程

逐个核对两处使用，看为什么更强的内存序是多余的：

**`ENABLED`（门控开关）**：

- `enable()` 写 `true`，`is_enabled()` 读它。它**不**用来「发布」任何相关数据。
- 真正需要线程安全的是 `EVENTS` 这个 `Vec`，而它的安全性**完全由 `Mutex` 自己的 `Acquire`/`Release` 语义提供**，与 `ENABLED` 无关。
- 哪怕某线程在 `enable()` 之后「稍慢」才看到 `true`，最坏后果只是**漏记几条事件**——对一个性能采样工具而言完全可接受。
- 因此 `ENABLED` 既不需要 `Release`（没有要发布的伴随数据），也不需要 `Acquire`（读到 `true` 后不需依赖别的线程已写入的数据）。

**`COUNTER`（线程发号机）**：

- 我们只需要「`fetch_add` 原子、不重号」。`Relaxed` 足以保证所有线程看到的 `COUNTER` 修改构成一个全局有序的序列，从而每次 `fetch_add` 返回的旧值都不同。
- 发号这件事**不依赖**别的内存读写，所以不需要更强的序。

#### 4.3.3 源码精读

三处带注释的 `Relaxed`，外加 `COUNTER` 处的那句：

[crates/typst-timing/src/lib.rs:L49-L53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L49-L53) — `COUNTER` 的自增用 `Relaxed`：只需要不重号的原子性，不需要同步其它操作。

```rust
// We only need atomicity and no synchronization of other
// operations, so `Relaxed` is fine.
static COUNTER: AtomicU64 = AtomicU64::new(1);
COUNTER.fetch_add(1, Ordering::Relaxed)
```

[crates/typst-timing/src/lib.rs:L66-L86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L66-L86) — `enable`/`disable`/`is_enabled` 对 `ENABLED` 的 store/load 也用 `Relaxed`，注释与上面同义。

为方便对照，下表把本文件用到的内存序与「更保守但更贵」的选项做对比：

| 操作 | 实际用的序 | 更强的序（为何不必要） |
| --- | --- | --- |
| `ENABLED.store(true)` | `Relaxed` | `Release`：用于「我要发布另一块数据」。但 `ENABLED` 不发布任何伴随数据，`EVENTS` 的安全由 `Mutex` 自己保证。 |
| `ENABLED.load()` | `Relaxed` | `Acquire`：用于「我要读到对方发布的数据」。这里读到 `true` 后不依赖对方的别的写入。漏看一小段时间没关系。 |
| `COUNTER.fetch_add(1)` | `Relaxed` | `SeqCst`：提供全局顺序，但我们只需要本变量的修改顺序，`Relaxed` 已能保证不重号。`SeqCst` 会插入额外的全屏障，纯属浪费。 |

> 经验法则：**当一个原子变量只是「独立的状态位 / 独立的计数器」，而不是「用来给另一块数据当发布信号」时，`Relaxed` 通常就够了。** 本文件的 `ENABLED` 与 `COUNTER` 都属于这一类。

#### 4.3.4 代码实践

**目标**：培养判断力——给你一段假设的「更强的序」，你能论证为什么在本场景里多余。这是一个**推理型实践**（不一定要跑代码）。

**操作步骤**：

1. 假设有人把 `enable()` 改成 `ENABLED.store(true, Ordering::Release)`，并把 `is_enabled()` 改成 `ENABLED.load(Ordering::Acquire)`，意图是「让 `enable()` 之前写入的数据对读到 `true` 的线程可见」。
2. 回答：在 `typst-timing` 里，`enable()` 之前**有没有**需要被发布的「相关数据」？被计时的结果最终存到哪、靠谁保证可见？（提示：是 `EVENTS` + `Mutex`。）
3. 结论：`Acquire`/`Release` 在这里只增加了内存屏障的成本，却没有换来任何正确性收益。

**需要观察/确认的现象**（若你想用工具佐证，可选用 `cargo asm` 查看反汇编）：

- `Relaxed` 的 `store`/`load` 在 x86 上通常是普通 MOV 指令，无额外屏障。
- 若强行换成 `SeqCst`，部分操作会生成 `mfence`/`lock` 前缀，开销更高。

**预期结果**：你能写出一段话，说清「本 crate 的正确性不依赖 `ENABLED`/`COUNTER` 提供跨变量同步，因此 `Relaxed` 既是充分的、也是最省的」。这部分结论**待本地验证**（汇编层面因平台而异）。

#### 4.3.5 小练习与答案

**练习 1**：给出一个「`Relaxed` 会不够用」的反例——什么样的用法必须升级到 `Release`/`Acquire`？

**答案**：当原子变量被用来**发布另一块数据**时。例如「线程 A 写好一个复杂结构 `DATA`，再 `FLAG.store(true, Release)`；线程 B 在 `FLAG.load(Acquire)` 返回 `true` 后去读 `DATA`」。这里必须用 `Release`/`Acquire` 才能保证 B 读到完整的 `DATA`。`typst-timing` 的 `ENABLED` 没有这种「伴随数据」要发布，所以 `Relaxed` 够用。

**练习 2**：用 `Relaxed` 后，会不会出现「两个线程拿到相同的 `thread_id`」？

**答案**：不会。`Relaxed` 虽然不提供跨变量同步，但**保证对该 `AtomicU64` 的所有修改构成一个单一全局顺序**。`fetch_add(1)` 是原子的「读改写」操作，两次并发的 `fetch_add` 不会重叠丢失，返回的旧值必然互不相同。这正是注释「we only need atomicity」所指的能力。

---

## 5. 综合实践

**任务**：编写一个**多线程计时示例**，把本讲三块内容（全局开关、每线程 `thread_id`、`Relaxed`）串起来，并通过导出的 JSON 亲眼看 `tid` 区分不同线程。

下面是**示例代码**（非项目原有代码）。运行需以 `path`/`git` 依赖引入 workspace 内的 `typst-timing`；具体数值**待本地验证**。

```rust
// 示例代码：多线程计时，观察 tid 区分
use std::thread;
use typst_timing::{enable, export_json, timed};

fn main() {
    enable(); // 打开全局开关（写 ENABLED，Relaxed）

    // 两个工作线程，各自用 timed! 包裹一段任务
    let h1 = thread::spawn(|| {
        timed!("task on thread 1", {
            thread::sleep(std::time::Duration::from_millis(10));
        });
    });
    let h2 = thread::spawn(|| {
        timed!("task on thread 2", {
            thread::sleep(std::time::Duration::from_millis(10));
        });
    });

    h1.join().unwrap();
    h2.join().unwrap();

    // 导出：source 闭包把 span 还原成 (file, line)；
    // 这里两个任务都没带 span，所以 source 不会被调用。
    let mut buf = Vec::new();
    export_json(&mut buf, |_| ("demo.rs".to_string(), 1)).unwrap();
    println!("{}", String::from_utf8(buf).unwrap());
}
```

**操作步骤**：

1. 把代码放入一个二进制 crate 的 `main.rs`，引入对 `typst-timing` 的依赖并编译。
2. 运行，把打印的 JSON 复制出来。
3. 在输出的 JSON 中找出每个事件对象的 `tid` 字段。

**需要观察的现象**：

- 一共应有 **4 个事件**：两个 `"ph":"B"`、两个 `"ph":"E"`，分别属于 `task on thread 1` 与 `task on thread 2`。
- 属于「线程 1 任务」的两条（一 B 一 E）`tid` 相同；属于「线程 2 任务」的两条 `tid` 也相同；而**两组之间的 `tid` 不同**（例如一组是 `1`、另一组是 `2`，具体编号取决于哪个线程先访问 `THREAD_DATA`，**待本地验证**）。
- 主线程（`main`）没有调用 `timed!`，所以它的 `thread_id`（被分配为最先访问的那个号）不会出现在事件里——但若你先访问 `THREAD_DATA` 的是主线程，编号分配顺序会随之变化。

**预期结果**：你能用第 4.2 节的「发号机 + 储物柜」模型解释这些 `tid` 的来源，并确认：在 Chrome/Perfetto 里加载该 JSON 时，两个线程的事件会落在**两条不同的泳道**上。

**延伸思考（结合源码注释）**：本示例在 native 上用 `std::thread` 没问题；但若目标换成 wasm，`std::thread::current().id()` 这类依赖标准库线程身份的写法就可能出问题——这正是 `typst-timing` 选择自造 `u64` id 的原因（见 [L274-L278](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L274-L278)）。

## 6. 本讲小结

- `typst-timing` 用两个**进程级静态量**承载全局状态：`ENABLED: AtomicBool`（默认关闭的开关）与 `EVENTS: Mutex<Vec<Event>>`（所有线程共享的事件缓冲区）。这是「用全局状态换埋点人体工学」的取舍。
- 事件的写入集中在 `TimingScope`：`new_impl` push `Start`、`Drop` push `End`，两次都经 `EVENTS.lock()`；`export_json` 用 `std::mem::take` 取出事件的同时清空缓冲区。
- `THREAD_DATA` 是 `thread_local!`，每线程一份 `ThreadData`；其 `id` 由内嵌的**进程级** `static COUNTER` 在每线程首次访问时经 `fetch_add` 分配。要分清「共享的发号机」与「每线程的储物柜」。
- 自造 `u64` thread id 而非 `std::thread::current().id()`，原因有二：**wasm 兼容**与**更便宜**（避免标准库克隆 `Arc` 等开销）。
- 全文件原子操作都用 `Ordering::Relaxed`：因为 `ENABLED`/`COUNTER` 只是独立的状态位/计数器，**不发布伴随数据**，`EVENTS` 的线程安全由 `Mutex` 自身保证；`Relaxed` 既充分又最省。
- `Start`/`End` 的 `thread_id` 由 `TimingScope` 在创建时取一次并复用，保证一对事件 tid 严格一致——这是 Chrome Trace 正确配对 B/E 的前提。

## 7. 下一步学习建议

- 想了解 `Event.timestamp` 的具体来源（native 的 `SystemTime` 与 wasm 的 `f64` 如何统一），请继续学 **u2-l3 跨平台时间戳：Timestamp 抽象**。
- 想看清 `export_json` 如何把这里收集到的事件（含 `thread_id`、`span`）序列化为 Chrome Trace 的 `B`/`E`、`pid`/`tid`/`ts`/`args`，请学 **u2-l4 导出 Chrome Trace JSON：export_json**。
- 想理解 `TimingScope::new` 返回 `Option` 与 `#[inline]` 如何把「默认关闭」做到几乎零成本，请学专家层 **u3-l1 零成本与启用门控设计**。
- 想看真实工程里 `enable→clear→record→export_json` 的完整端到端时序，请学 **u3-l4 集成实践：typst-kit Timer 与端到端计时导出**。
