# 数据模型：Event 与 EventKind

## 1. 本讲目标

本讲进入 typst-timing 的「内部数据表示」。在 [u1-l2 快速上手](u1-l2-quick-start-timed-macro.md) 中，我们已经会用 `enable()` 打开计时、用 `timed!` 宏包裹代码块，并知道 `TimingScope` 会在创建和销毁时各记一条事件。但「事件」到底是什么？它在内存里长什么样？又是如何变成 Chrome Trace 里的时间区间的？

学完本讲，你应当能够：

1. 说清 `Event` 结构体五个字段（`kind` / `timestamp` / `name` / `span` / `thread_id`）各自的类型与含义。
2. 解释 `EventKind::Start` / `EventKind::End` 与导出时 Chrome Trace 的 `B` / `E` 相位事件对是如何一一对应的。
3. 理清 `TimingScope::new_impl` 与 `Drop::drop` 这两个位置各自 `push` 一条事件的完整流程，以及它们如何复用同一份 `name` / `span` / `thread_id` 天然配对。

## 2. 前置知识

本讲默认你已完成 [u1-l1](u1-l1-project-overview.md) 与 [u1-l2](u1-l2-quick-start-timed-macro.md)。下面补充几个本讲要用到的基础概念。

**性能事件（profiling event）。** 性能分析器（profiler）最核心的工作，就是记录「某件事在什么时刻开始、又在什么时刻结束」。一条「事件」就是时间轴上的一个点：它带有一个名字、一个时间戳，以及「这是开始还是结束」的标记。两个同名的事件——一个开始、一个结束——就拼成了一段可测量的时间区间。

**Chrome Trace 的 B/E 相位。** Google Chrome 自带的 `chrome://tracing` 工具（以及兼容的 Perfetto）使用一种 JSON 格式来描述时间轴。其中最基础的两种「相位（phase）」是：

- `B`（Begin）：一个区间的起点。
- `E`（End）：一个区间的终点。

一条 `B` 紧跟一条同名的 `E`（且通常同一线程），在时间轴上就渲染成一根横条。typst-timing 导出的正是这种格式。

**RAII（Resource Acquisition Is Initialization）。** 复习 [u1-l2](u1-l2-quick-start-timed-macro.md) 的结论：`TimingScope` 在构造时记「开始」，在它被 Rust 自动 `Drop`（离开作用域）时记「结束」。这样开发者永远不需要手写「结束」调用。本讲我们钻进这两次记录的源码细节。

**`NonZeroU64`。** 标准库类型 `std::num::NonZeroU64`，表示一个「保证不为 0」的 `u64`。typst-timing 用 `Option<NonZeroU64>` 来表示「可选的源码位置 span」：`None` 表示没有 span，`Some(非零数)` 表示有一个 span。之所以要求非零，是为了让 `Option<NonZeroU64>` 和普通 `u64` 占用同样大小的内存（利用 `0` 这个原本非法的值来表示 `None`）。span 的具体语义（它如何对应到源码文件和行号）留到 [u3-l2](u3-l2-timed-and-time-attribute-macro.md) 讲宏时展开。

## 3. 本讲源码地图

typst-timing 整个 crate 只有一个源文件，本讲聚焦其中「事件数据模型」与「两次 push」这一段：

| 文件 | 本讲关注的范围 | 作用 |
|------|--------------|------|
| [crates/typst-timing/src/lib.rs:152-157](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L152-L157) | `TimingScope` 结构体 | 持有 `name` / `span` / `thread_id`，是连接 Start 与 End 的「信物」 |
| [crates/typst-timing/src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) | `TimingScope::new_impl` | 创建作用域时 `push` 一条 `Start` 事件 |
| [crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205) | `Drop::drop` | 销毁作用域时 `push` 一条 `End` 事件 |
| [crates/typst-timing/src/lib.rs:208-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L208-L219) | `Event` 结构体 | 一条计时事件的内存表示 |
| [crates/typst-timing/src/lib.rs:222-226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L222-L226) | `EventKind` 枚举 | 只有 `Start` / `End` 两个值 |
| [crates/typst-timing/src/lib.rs:131-145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L131-L145) | `export_json` 的序列化循环 | 把 `EventKind` 映射成 `B` / `E` |

此外，我们会引用一处真实调用作为例子：[crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158) 里 Typst 用 `timed!("check stabilized", ...)` 包裹一次约束校验。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先认识 `Event` 这条数据本身（4.1），再看它的 `kind` 如何对应 Chrome 的 B/E（4.2），最后回到产生事件的两个源码位置 `new_impl` 与 `Drop`（4.3）。

### 4.1 Event 结构体：一条计时事件的五个字段

#### 4.1.1 概念说明

「事件」是 typst-timing 的最小信息单元。无论你调用多少次 `timed!`，最终全局缓冲区 `EVENTS`（一个 `Mutex<Vec<Event>>`，见 [u1-l1](u1-l1-project-overview.md)）里存的就是一条条 `Event`。导出 JSON 时，也只是遍历这个 `Vec`，把每条 `Event` 翻译成一个 Chrome Trace 条目。

因此，理解 `Event` 的字段，就等于理解了 typst-timing 到底「记下了什么」。

#### 4.1.2 核心流程

一条 `Event` 在内存里的生命周期是：

1. 某段被计时的代码开始 → 产生一条 `Event`（`kind = Start`）。
2. 该段代码结束 → 产生另一条 `Event`（`kind = End`）。
3. 这两条 `Event` 拥有**相同的** `name` / `span` / `thread_id`，只有 `kind` 和 `timestamp` 不同。
4. 导出时，这两条 `Event` 被翻译成同名的 `B` / `E` 一对，渲染成时间轴上的一根横条。

字段到 Chrome Trace 输出的对应关系如下表（导出逻辑见 4.2）：

| Chrome Trace 字段 | 来源 `Event` 字段 | 说明 |
|------------------|------------------|------|
| `ph`（相位） | `kind` | `Start → "B"`，`End → "E"` |
| `name` | `name` | 原样复制 |
| `ts`（时间戳） | `timestamp.micros_since(首事件)` | 相对第一条事件的微秒数 |
| `pid`（进程号） | 固定值 `1` | typst-timing 只关心单进程 |
| `tid`（线程号） | `thread_id` | 用来区分不同线程的事件 |
| `args`（附加参数） | `span` 经 `source` 闭包解析 | 解析成 `{ file, line }`；无 span 时为 `null` |

#### 4.1.3 源码精读

`Event` 结构体的定义非常紧凑，五个字段各司其职：

[crates/typst-timing/src/lib.rs:208-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L208-L219) —— 定义了一条计时事件在内存里的样子：

```rust
struct Event {
    /// Whether this is a start or end event.
    kind: EventKind,
    /// The time at which this event occurred.
    timestamp: Timestamp,
    /// The name of this event.
    name: &'static str,
    /// The raw value of the span of code that this event was recorded in.
    span: Option<NonZeroU64>,
    /// The thread ID of this event.
    thread_id: u64,
}
```

逐字段解读：

- **`kind: EventKind`** —— 这条事件是「开始」还是「结束」。它是整条事件唯一的「分类标签」，详见 4.2。
- **`timestamp: Timestamp`** —— 事件发生的时刻。`Timestamp` 是 typst-timing 自己的跨平台时间抽象（native 用 `SystemTime`，wasm 用 `f64`），其内部实现留到 [u2-l3](u2-l3-cross-platform-timestamp.md)。这里只需知道它是一个 `Copy` 类型，记录「此刻」。
- **`name: &'static str`** —— 事件名，通常是被计时操作的人类可读名字。注意类型是 `&'static str`（指向程序只读数据段的引用），**不是** `String`。这个选择背后的取舍是本讲实践题的重点。
- **`span: Option<NonZeroU64>`** —— 可选的源码位置 span 的「裸数值」。它是一个数字而非完整的 `Span` 类型，是因为 `typst-timing` 不能依赖 `typst-syntax`（否则会循环依赖，见 `with_span` 的文档注释 [lib.rs:166-170](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L166-L170)）。导出时再由外部 `source` 闭包把这个数字还原成 `(file, line)`。
- **`thread_id: u64`** —— 产生该事件的线程编号。typst-timing 用自定义计数器生成 `u64`，而非 `std::thread::current().id()`，原因（更便宜、wasm 兼容）留到 [u2-l2](u2-l2-global-state-threading.md)。

一个真实例子能让这些字段落地。Typst 主 crate 里这一行用 `timed!` 包裹了一次「约束是否稳定」的校验：

[crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158) —— `timed!` 原样返回被包裹表达式的值，这里是一个布尔判断：

```rust
if timed!("check stabilized", constraint.validate(document.introspector())) {
```

当计时开启时，这一行会产生**两条** `Event`：都叫 `"check stabilized"`，只是 `kind` 一为 `Start`、一为 `End`，`timestamp` 分别是校验前后的两个时刻。

#### 4.1.4 代码实践

**实践目标：** 不写代码，纯靠读源码，把一次 `timed!` 调用产生的两条 `Event` 的字段值填出来，建立「字段 ↔ 现实」的直觉。

**操作步骤：**

1. 假设计时已开启，单线程运行到上文的 `timed!("check stabilized", ...)`。
2. 分别考虑「进入作用域」与「离开作用域」两个时刻。
3. 为每个时刻填写一条 `Event` 的五个字段。

**需要观察/填写的内容（下表）：**

| 时刻 | `kind` | `name` | `span` | `thread_id` | `timestamp` |
|------|--------|--------|--------|-------------|-------------|
| 进入作用域（Start） | ? | ? | ?（取决于宏是否传了 `span = ...`） | ? | 校验开始时刻 |
| 离开作用域（End） | ? | ? | ? | ? | 校验结束时刻 |

**预期结果：**

- 两条 `Event` 的 `name` 都是 `"check stabilized"`，`span` 相同（本例没传 span，故均为 `None`），`thread_id` 相同（同一根线程）。
- 唯一不同的是 `kind`（`Start` vs `End`）和 `timestamp`（两个真实时刻）。
- `timestamp` 的具体数值依赖运行环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1：** `Event` 有哪五个字段？分别是什么类型？

> **参考答案：** `kind: EventKind`、`timestamp: Timestamp`、`name: &'static str`、`span: Option<NonZeroU64>`、`thread_id: u64`。

**练习 2：** 为什么 `span` 的类型是 `Option<NonZeroU64>`，而不是直接 `u64` 或 `Option<u64>`？

> **参考答案：** 用 `NonZeroU64` 可以让 `Option<NonZeroU64>` 与 `u64` 占用同样大小（用 `0` 表示 `None`），节省内存；`Option` 则用来表达「这条事件没有 span」。同时它是一个裸数字，避免 `typst-timing` 反向依赖 `typst-syntax` 造成循环依赖。

### 4.2 EventKind 枚举与 Chrome Trace 的 B/E 事件对

#### 4.2.1 概念说明

`EventKind` 是 typst-timing 里最简单的类型——只有两个值的枚举。但它是「一条事件如何变成时间轴上一根横条」的关键开关。

typst-timing 选择用「两条事件」而非「一条带 duration 的事件」来表示一个区间。这契合 Chrome Trace 最基础的 B/E（Begin/End）相位模型：进入区间记 `B`，离开区间记 `E`，工具自动把同名同线程的 `B`/`E` 配对成横条。

#### 4.2.2 核心流程

`EventKind` 的取值与导出相位的映射是一个纯函数式的 `match`：

```
导出时遍历每条 Event：
  若 event.kind == Start → 写出 ph: "B"
  若 event.kind == End   → 写出 ph: "E"
```

之所以能正确配对，是因为 4.1 里强调的事实：**一对 Start/End 拥有相同的 `name` 与 `thread_id`**。Chrome Trace 工具就是靠 `(name, tid)` 把相邻的 `B` 和 `E` 拼成一根横条的。

#### 4.2.3 源码精读

`EventKind` 的定义只有一个 derive 宏和两个变体：

[crates/typst-timing/src/lib.rs:222-226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L222-L226) —— `Start` / `End` 两个变体，`Copy` 派生让它在 `Event` 里复制零成本：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
enum EventKind {
    Start,
    End,
}
```

真正把它翻译成 `B` / `E` 的地方在 `export_json` 的序列化循环里。每个 `Event` 被组装成一个临时的 `Entry` 结构再交给 serde：

[crates/typst-timing/src/lib.rs:131-145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L131-L145) —— `ph` 字段由 `event.kind` 经 `match` 决定，其余字段来自 `Event` 各字段：

```rust
for event in &events {
    seq.serialize_element(&Entry {
        name: event.name,
        cat: "typst",
        ph: match event.kind {
            EventKind::Start => "B",
            EventKind::End => "E",
        },
        ts: event.timestamp.micros_since(events[0].timestamp),
        pid: 1,
        tid: event.thread_id,
        args: event.span.map(&mut source).map(|(file, line)| Args { file, line }),
    })
    ...
}
```

注意几点：

- `cat: "typst"` 是 Chrome Trace 的「类别（category）」，所有事件统一打这个标签，方便在工具里筛选。
- `ts` 用 `micros_since(events[0].timestamp)`，即**相对第一条事件**的微秒数。所以导出 JSON 里第一条事件的 `ts` 恒为 `0.0`。
- `pid: 1` 是写死的进程号——typst-timing 只服务单进程场景。
- `args` 仅当 `span` 为 `Some(..)` 时才调用 `source` 闭包解析出 `(file, line)`；`span` 为 `None` 时 `args` 为 `null`。

#### 4.2.4 代码实践

**实践目标：** 跑一个最小可运行示例，亲眼看到一对 `B` / `E` 出现在导出的 JSON 里。

**操作步骤：**

1. 新建一个二进制 crate，把 `typst-timing` 加为依赖（指向你本地 typst 仓库中该 crate 的路径，或通过 git）。
2. 写入下面的**示例代码** `src/main.rs`：

```rust
// 示例代码：最小化演示一对 B/E 事件的产生
use typst_timing::{enable, export_json, timed};

fn main() {
    enable();

    // timed! 进入作用域记 Start，离开记 End
    let answer = timed!("compute answer", {
        std::thread::sleep(std::time::Duration::from_millis(10));
        6 * 7
    });
    assert_eq!(answer, 42);

    let mut buf = Vec::new();
    export_json(&mut buf, |_| ("demo.rs".to_string(), 1)).unwrap();
    print!("{}", String::from_utf8(buf).unwrap());
}
```

3. 运行 `cargo run`（cargo 命令的可用性**待本地验证**）。

**需要观察的现象：**

- 输出是一个 JSON 数组，**恰好两条**元素。
- 第一条 `"ph": "B"`，第二条 `"ph": "E"`。
- 两条的 `"name"` 都是 `"compute answer"`，`"tid"` 相同，`"cat"` 都是 `"typst"`。
- 因为没传 `span`，两条的 `"args"` 都是 `null`。

**预期结果（结构确定，具体 `ts` 数值待本地验证）：**

```json
[
  {"name":"compute answer","cat":"typst","ph":"B","ts":0.0,"pid":1,"tid":1,"args":null},
  {"name":"compute answer","cat":"typst","ph":"E","ts":<约 10000.0>,"pid":1,"tid":1,"args":null}
]
```

第一条 `ts` 恒为 `0.0`（相对自身）；第二条约 `10000.0`（10 ms = 10000 µs），实际值取决于机器，**待本地验证**。`tid` 为 `1`（首个线程，计数器初值见 [u2-l2](u2-l2-global-state-threading.md)）。

#### 4.2.5 小练习与答案

**练习 1：** 一对 `Start` / `End` 事件在导出后变成 Chrome Trace 里的什么？

> **参考答案：** 变成两条 `ph` 分别为 `"B"` 和 `"E"` 的条目，二者 `name`、`tid` 相同，由工具自动配对成时间轴上的一根横条。

**练习 2（讨论）：** 为什么 typst-timing 用「两条 B/E 事件」而不是「一条带 `duration` 的 Complete（`X`）事件」来表示区间？

> **参考答案：** 因为 B/E 模型与 RAII 天然契合——进入作用域 `push` 一条 `Start`，离开作用域 `push` 一条 `End`，两个时间戳都在真实时刻现场采样，无需事后计算 duration；同时也更灵活地支持嵌套和跨线程。B/E 是 Chrome Trace 最基础、最易生成的相位模型，对一个追求「默认关闭、极简」的 crate 而言是恰当的取舍。

### 4.3 TimingScope 的两次 push：new_impl 记 Start，Drop 记 End

#### 4.3.1 概念说明

[u1-l2](u1-l2-quick-start-timed-macro.md) 已经讲过 `TimingScope` 的 RAII 外壳和 `Option` 门控。本讲我们打开外壳，看那两条 `Event` 究竟在源码的哪两个位置被 `push` 进 `EVENTS`，以及它们如何共享同一份 `name` / `span` / `thread_id`。

关键点是「信物」设计：`TimingScope` 这个结构体在创建时把 `name` / `span` / `thread_id` 存进自己的字段，像一份信物；当它被 `Drop` 时，再把这份信物原样抄进一条 `End` 事件。这样就保证了 Start 与 End 的三个字段完全一致，配对零成本、零出错。

#### 4.3.2 核心流程

一次 `timed!(name, body)`（计时开启时）的完整数据流：

```
timed!(name, body)
  └─ TimingScope::new(name)              // 公开入口，返回 Option
       └─ with_span(name, None)          // 仍返回 Option，做 is_enabled 检查
            └─ is_enabled() == true
                 └─ new_impl(name, None) // 不再检查开关，真正干活
                      ├─ 从 THREAD_DATA 取 thread_id 与 timestamp
                      ├─ EVENTS.lock().push( Event{ kind: Start, ... } )  ← 第 1 次 push
                      └─ 返回 TimingScope { name, span, thread_id }        ← 「信物」
  └─ 执行 body，得到返回值
  └─ 作用域结束，TimingScope 被 Drop
       └─ Drop::drop
            ├─ 取当前 timestamp
            └─ EVENTS.lock().push( Event{ kind: End, name: self.name, ... } )  ← 第 2 次 push
```

注意两次 `push` 的**非对称**：

- **Start（`new_impl`）**：`thread_id` 和 `timestamp` 都来自 `THREAD_DATA`（因为还要顺便把 `thread_id` 存进结构体当信物）。
- **End（`Drop`）**：只重新取一个 `timestamp`（区间的终点），`name` / `span` / `thread_id` 全部复用结构体里存的信物，无需再访问 `THREAD_DATA` 拿 `thread_id`。

#### 4.3.3 源码精读

先看「信物」本身——`TimingScope` 的三个字段就是日后配对所需的全部信息：

[crates/typst-timing/src/lib.rs:152-157](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L152-L157) —— 结构体只持有 End 事件需要的、且无法事后重取的字段：

```rust
pub struct TimingScope {
    name: &'static str,
    span: Option<NonZeroU64>,
    thread_id: u64,
}
```

第 1 次 `push` 发生在 `new_impl`。注意它是私有的，只被「已经确认计时开启」的 `with_span` 调用，所以自身不再做开关检查：

[crates/typst-timing/src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) —— 采样起点时间戳，`push` 一条 `Start`，并把字段存进结构体：

```rust
fn new_impl(name: &'static str, span: Option<NonZeroU64>) -> Self {
    let (thread_id, timestamp) =
        THREAD_DATA.with(|data| (data.id, Timestamp::now_with(data)));
    EVENTS.lock().push(Event {
        kind: EventKind::Start,
        timestamp,
        name,
        span,
        thread_id,
    });
    Self { name, span, thread_id }
}
```

第 2 次 `push` 发生在 `Drop`。这里只取终点时间戳，其余字段全部从 `self` 抄出来：

[crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205) —— 销毁时 `push` 一条 `End`，与 Start 天然配对：

```rust
impl Drop for TimingScope {
    fn drop(&mut self) {
        let timestamp = Timestamp::now();
        EVENTS.lock().push(Event {
            kind: EventKind::End,
            timestamp,
            name: self.name,
            span: self.span,
            thread_id: self.thread_id,
        });
    }
}
```

把两段对照看，配对的秘密就很清楚了：`new_impl` 存进 `Self` 的 `name` / `span` / `thread_id`，在 `drop` 里被**原样**复制到 `End` 事件；只有 `kind` 从 `Start` 翻成 `End`，`timestamp` 换成终点时刻。这正是 RAII 让开发者「永不需手写结束」的内部机制。

#### 4.3.4 代码实践

**实践目标：** 解释 `Event.name` 为何用 `&'static str` 而非 `String`，并量化这种设计在内存与并发上的好处。这是本讲的核心思考题。

**操作步骤：**

1. 阅读 `new_impl` 与 `Drop::drop` 两次 `push`（上文已贴）。
2. 注意两次 `push` 都发生在 `EVENTS.lock()` 持锁期间。
3. 假设把 `name` 改成 `String`，推演每次 `push` 需要额外做什么。
4. 写出你的结论，对照下面的参考答案。

**需要观察/思考的现象：**

- 用 `&'static str` 时，存一个 `name` 到底复制了什么？
- 如果改成 `String`，`new_impl` 里 `name` 既要存进 `Event` 又要存进 `Self`，会发生什么？`Drop` 里又如何？

**预期结果（参考答案——`&'static str` 相对 `String` 的好处）：**

1. **零堆分配。** 字符串字面量（如 `"check stabilized"`）在编译期就被放进程序的只读数据段（`.rodata`）。`&'static str` 只是一个「指针 + 长度」的引用，`push` 一条 `Event` 时不需要在堆上分配任何东西。若用 `String`，每条事件都要拥有自己的堆缓冲区。

2. **复制廉价（`Copy`）。** `&'static str` 是 `Copy` 类型，`new_impl` 把 `name` 同时抄进 `Event` 和 `Self` 字段、`Drop` 再抄进 `End` 事件，都只是复制一个胖指针（两三个机器字）。`String` 不是 `Copy`，每次都要 `.clone()`，触发堆分配与引用计数调整。

3. **锁内零分配，缩短临界区。** 两次 `push` 都在持有 `EVENTS` 锁时进行。用 `&'static str` 时锁内只做「复制指针 + `Vec::push`」；若用 `String`，锁内还得 `clone`/分配，会延长锁持有时间，在多线程计时下增加争用。

4. **Start/End 天然配对、零成本。** `TimingScope` 持有同一份 `&'static str`，两次 `push` 复制的是同一个引用（连底层指针地址都一致），保证两条事件 `name` 完全相同，无需任何额外配对逻辑。

5. **天然线程安全。** `&'static str` 不可变且生命周期贯穿整个程序，多线程可以无锁共享同一个字面量，不存在数据竞争。

6. **内存占用随调用次数线性增长的是 `Event` 结构体本身（定长），而非 `name` 字符串。** 即便调用成千上万次 `timed!`，`name` 指向的字符串本身不增加任何堆内存。

**代价（API 取舍）：** 这要求传给 `timed!` / `TimingScope::new` 的名字必须是编译期已知的字符串字面量（`&'static str`），不能传运行期拼接的 `String`。typst-timing 接受这个限制，换取「默认关闭、几乎零成本」的计时基础设施。

#### 4.3.5 小练习与答案

**练习 1：** `new_impl` 和 `Drop::drop` 各 `push` 哪种 `kind` 的事件？

> **参考答案：** `new_impl` `push` 的是 `EventKind::Start`；`Drop::drop` `push` 的是 `EventKind::End`。

**练习 2：** 如果被 `timed!` 包裹的代码 `panic` 了，`End` 事件还会被记录吗？

> **参考答案：** 在 Rust 默认的 `panic = "unwind"` 策略下，栈展开会执行 `TimingScope` 的 `Drop`，因此 `End` 事件仍会被 `push`，区间得以正确闭合——这正是 RAII 的额外好处。若项目配置了 `panic = "abort"`，则进程直接中止，`Drop` 不会运行，`End` 也就不会被记录（该 abort 情形**待本地验证**）。

**练习 3：** `new_impl` 里同时取了 `thread_id` 和 `timestamp`，而 `Drop` 里只取了 `timestamp`。为什么 `Drop` 不需要再取 `thread_id`？

> **参考答案：** 因为 `thread_id` 已经在 `new_impl` 时存进了 `TimingScope` 的字段（「信物」）。`Drop` 复用 `self.thread_id` 即可，既省去一次 `THREAD_DATA` 访问，也保证 End 与 Start 的 `thread_id` 完全一致。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读源码 + 跑示例」的小任务：

**任务：** 选取 Typst 真实代码 [crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158) 的 `timed!("check stabilized", ...)` 作为分析对象。

1. **画时间线。** 在纸上画出从「进入 `timed!`」到「离开 `timed!`」的完整时间线，标注：
   - 两次 `EVENTS.lock().push(...)` 分别发生在哪段代码（`new_impl` / `Drop`）。
   - 两条 `Event` 的 `kind`、`name`、`span`、`thread_id`（哪些相同、哪些不同）。
2. **跑示例验证。** 用 4.2.4 的示例代码（或把名字改成 `"check stabilized"`）运行，确认导出 JSON 里确实是一对 `B`/`E`，且第一条 `ts` 为 `0.0`。
3. **解释设计。** 用你自己的话写一段：为什么 `Event.name` 用 `&'static str`、为什么 `thread_id` 要在 `new_impl` 时存进结构体、为什么两次 `push` 要拆在两个函数里而不是合并。把你的解释对照 4.3.4 的参考答案查漏补缺。

**预期结果：** 你应当能用一张图 + 几句话，向别人讲清「一次 `timed!` 在 typst-timing 内部到底产生了什么数据、存在哪里、又如何被导出」。

## 6. 本讲小结

- `Event` 是 typst-timing 的最小信息单元，五个字段为 `kind`、`timestamp`、`name`、`span`、`thread_id`。
- `EventKind` 只有 `Start` / `End` 两个值；导出时经 `match` 映射成 Chrome Trace 的 `B` / `E` 相位。
- 一对 Start/End 共享相同的 `name` / `span` / `thread_id`，只有 `kind` 与 `timestamp` 不同，这是配对的基础。
- `TimingScope::new_impl` 在创建时 `push` 一条 `Start`，`Drop::drop` 在销毁时 `push` 一条 `End`——这就是 RAII「永不需手写结束」的内部机制。
- `TimingScope` 把 `name` / `span` / `thread_id` 存为字段当「信物」，让 End 复用 Start 的值，配对零成本。
- `name` 用 `&'static str` 而非 `String`，换来零堆分配、锁内零分配、天然线程安全等性能优势，代价是名字必须是编译期字面量。

## 7. 下一步学习建议

本讲只讲了「事件长什么样、在哪产生」，还没讲「事件存在哪、多线程怎么区分」。建议按以下顺序继续：

- **[u2-l2 全局状态与线程模型](u2-l2-global-state-threading.md)：** 接着挖 `ENABLED`、`EVENTS` 这两个全局静态量，以及 `THREAD_DATA` 和本讲反复出现的 `thread_id` 到底怎么生成、为什么用自定义 `u64`。
- **[u2-l3 跨平台时间戳](u2-l3-cross-platform-timestamp.md)：** 本讲刻意回避了 `Timestamp` 内部，下一讲补上 native / wasm 的条件编译与 `micros_since` 的单位换算。
- **[u2-l4 导出 Chrome Trace JSON](u2-l4-export-chrome-trace-json.md)：** 本讲只看了 `export_json` 里 `kind → B/E` 那一段，完整导出流程（含 `source` 闭包、`std::mem::take` 防死锁）留到这一讲。
