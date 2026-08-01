# 导出 Chrome Trace JSON：export_json

## 1. 本讲目标

在前三讲里，我们已经搞清楚了三件事：`Event` 长什么样（u2-l1）、事件存在哪里、谁来写（u2-l2）、时间戳怎么取（u2-l3）。但到目前为止，这些事件还只是躺在进程内存里的一个 `Vec<Event>`，外部世界看不到。

本讲要解决的就是「最后一公里」：把这些内部事件翻译成一款业界标准格式——**Chrome Trace Event 格式**——并写成 JSON。学完后你应当能够：

1. 读懂 `export_json` 的泛型签名，说清 `W: Write`、`source` 闭包各自的作用。
2. 理解 Chrome Trace Event 格式中 `ph`（B/E 相位）、`pid`、`tid`、`ts`、`args`、`cat`、`name` 各字段的含义，以及它们如何由内部 `Event` 映射而来。
3. 看懂 `Entry`/`Args` 两个序列化结构体，并理解为什么导出前要用 `std::mem::take` 把事件「搬出来」——这是为了防止 `source` 闭包反向触发计时而死锁。

本讲是 u2 单元的收尾，也是把 typst-timing 真正「用起来」的关键一步。

## 2. 前置知识

- **事件模型（u2-l1）**：知道 `Event` 有 `kind`、`timestamp`、`name`、`span: Option<NonZeroU64>`、`thread_id` 五个字段，`EventKind` 只有 `Start`/`End` 两个变体。
- **全局状态（u2-l2）**：知道事件存在全局静态量 `EVENTS: Mutex<Vec<Event>>` 里，写入时加锁。
- **时间戳（u2-l3）**：知道 `Timestamp` 跨平台抽象，以及 `micros_since(self, start)` 会把任意时间差归一化成**微秒**（native 下纳秒 ÷1000，wasm 下毫秒 ×1000）。
- **serde 序列化基础**：本讲会用到 `#[derive(Serialize)]`、`Serializer::serialize_seq`。如果你没接触过 serde，只需理解「把结构体按字段名写成 JSON 对象」即可。
- **Chrome Trace / Perfetto**：两款可视化工具。Chrome 浏览器内置的 `chrome://tracing` 和 Google 的 [Perfetto](https://ui.perfetto.dev/) 都能加载本讲产出的 JSON，把计时事件画成时间轴。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | 本讲主战场。`export_json` 函数、`Entry`/`Args` 序列化结构体、`std::mem::take` 防死锁逻辑都在这里。 |
| [crates/typst-kit/src/timer.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs) | 真实集成案例。`Timer::record` 调用 `export_json`，并通过 `resolve_span` 把裸 span 还原成 `(file, line)`，正好是 `source` 闭包的来源。 |

`Cargo.toml` 里 `serde` 与 `serde_json` 是 workspace 依赖（[crates/typst-timing/Cargo.toml#L15-L18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/Cargo.toml#L15-L18)），这就是 `export_json` 能直接用 `serde_json::Serializer` 的原因。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `export_json` 的签名与 `source` 闭包
2. Chrome Trace Event 格式（B/E 相位、pid、tid、ts、args）
3. `Entry`/`Args` 序列化与 `std::mem::take` 防死锁

### 4.1 `export_json` 的签名与 source 闭包

#### 4.1.1 概念说明

到此为止，typst-timing 内部的事件都是「私有类型」：`Event`、`EventKind` 都没有 `pub`，外部根本拿不到。这意味着我们**不能**直接把 `Vec<Event>` 丢给用户。`export_json` 就是唯一的对外出口——它把私有事件翻译成公开的 JSON，用户只看到「JSON 文本流」，看不到内部结构。

这种「私有内核 + 单一导出门面」的设计有两个好处：

- 内部 `Event` 字段可以随时重构而不破坏兼容性，因为外部依赖的是 **Chrome Trace 标准 JSON**，而不是 Rust 类型。
- 导出逻辑集中在一处，便于保证格式正确。

`source` 闭包是这个门面里最有意思的一环。回忆 u2-l1：`span` 字段是个裸的 `NonZeroU64`，因为 typst-timing 不能依赖 typst-syntax（否则会循环依赖）。所以导出时，typst-timing **自己不知道**这个数字对应哪个文件、哪一行。这个「翻译」工作就交给调用方：`source(NonZeroU64) -> (String, u32)` 把一个裸 span 还原成 `(文件路径, 行号)`。typst-timing 只负责把结果塞进 `args` 字段。

#### 4.1.2 核心流程

`export_json` 的执行流程可以概括为：

```text
1. 加锁 EVENTS，把事件「搬出来」到局部变量（清空全局缓冲区）
2. 创建一个 JSON 数组序列化器（serde_json::Serializer + serialize_seq）
3. 遍历每个事件：
     a. 把 Event 映射成 Entry（含 ph/ts/pid/tid/args 等字段）
     b. 若有 span，调用 source 闭包得到 (file, line)，包成 Args
     c. 把 Entry 序列化进数组
4. 结束数组序列化，返回 Ok(())
```

注意第 1 步是「搬出来」而不是「拷贝出来」——这关系到死锁，我们在 4.3 节细讲。

#### 4.1.3 源码精读

先看 `export_json` 的签名（[crates/typst-timing/src/lib.rs#L101-L104](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L101-L104)）：

```rust
pub fn export_json<W: Write>(
    writer: W,
    mut source: impl FnMut(NonZeroU64) -> (String, u32),
) -> Result<(), String> {
```

逐个参数解释：

- `W: Write`：泛型写入器。只要是实现了 `std::io::Write` 的东西都行——`&mut Vec<u8>`、`File`、`BufWriter`、标准输出都行。这让 `export_json` 既能写磁盘，也能写内存缓冲区。
- `source: impl FnMut(NonZeroU64) -> (String, u32)`：闭包，输入是裸 span，输出是 `(文件路径, 行号)`。用 `FnMut`（而非 `Fn`）是因为解析 span 通常需要可变地借用上下文（比如 `&mut world`），后面在 timer.rs 里能看到真实用法。
- `Result<(), String>`：错误用 `String` 而非 `io::Error`/`serde_json::Error`，是为了在 typst 的 `StrResult` 错误体系里无缝传递。

再看紧贴函数体定义的两个私有序列化结构体（[crates/typst-timing/src/lib.rs#L105-L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L105-L120)）：

```rust
#[derive(Serialize)]
struct Entry {
    name: &'static str,
    cat: &'static str,
    ph: &'static str,
    ts: f64,
    pid: u64,
    tid: u64,
    args: Option<Args>,
}

#[derive(Serialize)]
struct Args {
    file: String,
    line: u32,
}
```

这两个结构体**只**定义在 `export_json` 函数体内部（Rust 允许函数内定义类型），是纯粹的「序列化外壳」。它们不被导出，外部世界永远不会看到 `Entry`/`Args`，只看到它们序列化后的 JSON。这正是「私有内核」思想的具体落地。

`Args` 用 `Option` 包裹成 `args: Option<Args>`：没有 span 的事件，`args` 序列化成 `null`；有 span 的事件，序列化成 `{"file": "...", "line": N}`。

#### 4.1.4 代码实践

**实践目标**：验证 `source` 闭包只对「带 span 的事件」被调用。

**操作步骤**：

下面是一段示例代码（非项目原有代码，仅为演示 API）。你可以在 typst workspace 内新建一个二进制 crate，或直接理解逻辑：

```rust
// 示例代码：演示 export_json 与 source 闭包
use std::num::NonZeroU64;
use typst_timing::{enable, export_json, TimingScope};

fn main() {
    enable();

    // (a) 不带 span：source 不会被调用
    let _a = TimingScope::new("no-span");

    // (b) 带 span：source 会被调用一次（Start 事件）
    let _b = TimingScope::with_span("has-span", NonZeroU64::new(42));
    // _b 在此 drop，产生 End 事件，source 又被调用一次

    let mut out = Vec::<u8>::new();
    let mut call_count = 0u32;
    export_json(&mut out, |span| {
        call_count += 1;
        println!("source 被调用, span={span}");
        (format!("demo.rs"), 10)
    }).unwrap();

    println!("source 共被调用 {} 次", call_count);
    println!("{}", String::from_utf8(out).unwrap());
}
```

**需要观察的现象**：

- (a) 的 `no-span` 事件没有 span，`source` 不会为它调用。
- (b) 的 `has-span` 有 Start 和 End 两条事件，`source` 各调用一次，共 2 次。注意：**同一个 span 会被调用两次**（Start 一次、End 一次），因为两条事件各自独立序列化。

**预期结果**：控制台打印 `source 共被调用 2 次`。若无法在本地编译运行（需把 typst-timing 作为依赖），则标注「待本地验证」，至少通过阅读源码（4.2.3 节的循环体）确认这个调用次数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `source` 的类型是 `FnMut` 而不是 `Fn`？给出一个真实场景。

**参考答案**：因为解析 span 经常需要可变借用。在 [crates/typst-kit/src/timer.rs#L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L89-L92) 中，闭包 `|span| resolve_span(world, ...)` 捕获了 `&mut world` 的上下文，`FnMut` 允许这种可变捕获；`Fn` 不允许。

**练习 2**：如果调用 `export_json` 时 `EVENTS` 里一个事件都没有，会发生什么？

**参考答案**：`std::mem::take` 取出空 `Vec`，序列化器写出一个空 JSON 数组 `[]`，函数返回 `Ok(())`。不会报错（注意 `events[0]` 不会越界，因为循环体在空 Vec 上根本不执行）。

---

### 4.2 Chrome Trace Event 格式：B/E 相位、pid、tid、ts、args

#### 4.2.1 概念说明

Chrome Trace Event 格式是 Google 定义的一套**时间轴事件标准**，被 `chrome://tracing`、Perfetto、`chrome://tracing` 等工具广泛支持。它的核心是「持续时间事件（Duration Event）」：一对 `B`（Begin，开始）和 `E`（End，结束）事件，配合相同的线程号 `tid` 和时间戳，工具就能把它们画成一个有长度的色块。

这套格式和 typst-timing 的内部模型**天然契合**：一个 `TimingScope` 创建时记 `Start`、Drop 时记 `End`，正好是一对 Begin/End。所以映射几乎是直译。

一个完整的 Chrome Trace 事件 JSON 大致长这样：

```json
{
  "name": "task A",
  "cat": "typst",
  "ph": "B",
  "ts": 0.0,
  "pid": 1,
  "tid": 1,
  "args": null
}
```

字段含义：

| 字段 | 全称 | 含义 | typst-timing 中的来源 |
| --- | --- | --- | --- |
| `name` | name | 事件名（如 `"parse"`） | `event.name: &'static str` |
| `cat` | category | 分类（逗号分隔） | 固定字符串 `"typst"` |
| `ph` | phase | 相位：`B`=Begin，`E`=End | `match event.kind`，Start→`"B"`，End→`"E"` |
| `ts` | timestamp | 距首个事件的微秒数 | `event.timestamp.micros_since(events[0].timestamp)` |
| `pid` | process id | 进程号 | 固定 `1` |
| `tid` | thread id | 线程号 | `event.thread_id`（自造的 `u64`） |
| `args` | arguments | 附加信息 | 有 span 时为 `{"file","line"}`，无 span 时为 `null` |

#### 4.2.2 核心流程

事件到 JSON 的映射发生在序列化循环里，关键有三点：

1. **相位的确定**：`EventKind::Start` 映射为 `"B"`，`EventKind::End` 映射为 `"E"`。这就是一对 Begin/End。
2. **时间戳的归一化**：所有 `ts` 都是相对于**第一个事件**的微秒差。设首个事件时间戳为 \( t_0 \)，第 \( i \) 个事件为 \( t_i \)，则：

   \[ \mathrm{ts}_i = \text{micros\_since}(t_i,\ t_0) \]

   首个事件本身 \( \mathrm{ts}_0 = 0 \)。这样整条 trace 的时间起点对齐到 0，工具显示更直观，数值也更小。
3. **分类与进程号固定**：`cat` 恒为 `"typst"`，`pid` 恒为 `1`。因为 typst 不需要区分多进程，但需要区分多线程（用 `tid`）。

`args` 的生成逻辑：对有 span 的事件，`event.span.map(&mut source)` 用闭包把 `NonZeroU64` 变成 `(String, u32)`，再 `.map(|(file, line)| Args { file, line })` 包成 `Args`；无 span 则 `args` 为 `None`（序列化为 `null`）。

#### 4.2.3 源码精读

映射逻辑全部在循环体内（[crates/typst-timing/src/lib.rs#L131-L145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L131-L145)）：

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
    .map_err(|e| format!("failed to serialize event: {e}"))?;
}
```

逐行对应到上表：

- `name: event.name` —— `&'static str` 直接复用，零拷贝。
- `cat: "typst"` —— 硬编码分类。
- `ph` —— 用 `match` 把 `EventKind` 翻译成相位字母，这就是 typst 内部枚举与外部标准格式的「翻译器」。
- `ts: ... micros_since(events[0].timestamp)` —— 注意基准是 `events[0]`，即**第一个**事件，循环中不变。所有事件的 `ts` 都以它为原点。
- `pid: 1` —— 硬编码。
- `tid: event.thread_id` —— 来自 u2-l2 讲过的自造 `u64` 线程号。
- `args` —— 双重 `map`：先 `Option<NonZeroU64> -> Option<(String, u32)>`（调 `source`），再 `Option<(String, u32)> -> Option<Args>`。

整个序列化用 `serde_json::Serializer` 手动驱动（[crates/typst-timing/src/lib.rs#L126-L129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L126-L129)）：

```rust
let mut serializer = serde_json::Serializer::new(writer);
let mut seq = serializer
    .serialize_seq(Some(events.len()))
    .map_err(|e| format!("failed to serialize events: {e}"))?;
```

这里用 `serialize_seq(Some(events.len()))` 而不是把事件收集成 `Vec<Entry>` 再一次序列化，是为了**边遍历边写入**，避免再分配一个临时的 `Vec<Entry>`。对一份可能有成千上万事件的 trace，这是省内存的小技巧。

#### 4.2.4 代码实践

**实践目标**：亲手生成一份 Chrome Trace JSON，并在 Perfetto 里看到时间轴。

**操作步骤**（示例代码，非项目原有）：

```rust
// 示例代码：生成 events.json
use typst_timing::{enable, timed, export_json};

fn main() {
    enable();

    timed!("task A", std::thread::sleep(std::time::Duration::from_millis(40)));
    timed!("task B", std::thread::sleep(std::time::Duration::from_millis(10)));

    let mut buf = Vec::<u8>::new();
    // 这些事件都没有 span，source 不会被调用；闭包可随便返回占位值
    export_json(&mut buf, |_| ("demo.rs".to_string(), 10)).unwrap();

    std::fs::write("events.json", &buf).unwrap();
    println!("已写出 {} 字节", buf.len());
}
```

把 `events.json` 拖进 [https://ui.perfetto.dev/](https://ui.perfetto.dev/)（或在 Chrome 地址栏输入 `chrome://tracing` 并点 Load）。

**需要观察的现象**：

- 时间轴上应出现两个色块：`task A` 较长（约 40000 微秒）、`task B` 较短（约 10000 微秒），先后排列。
- 每个色块展开后，对应一对 `B`/`E` 事件，`name` 相同。
- 第一个 `B` 事件的 `ts` 为 `0.0`。

**预期结果**：JSON 是一个数组，形如（数值为示意）：

```json
[
  {"name":"task A","cat":"typst","ph":"B","ts":0.0,"pid":1,"tid":1,"args":null},
  {"name":"task A","cat":"typst","ph":"E","ts":40123.4,"pid":1,"tid":1,"args":null},
  {"name":"task B","cat":"typst","ph":"B","ts":40124.1,"pid":1,"tid":1,"args":null},
  {"name":"task B","cat":"typst","ph":"E","ts":50130.2,"pid":1,"tid":1,"args":null}
]
```

若本地未配置 typst-timing 依赖无法运行，标注「待本地验证」，但可通过阅读循环体（4.2.3）人工推出这份结构。

#### 4.2.5 小练习与答案

**练习 1**：如果两个事件来自不同线程，它们的 `ts` 是否仍以**同一个** `events[0]` 为基准？这样会不会导致跨线程时间对比失真？

**参考答案**：仍以同一个 `events[0]` 为基准。这正是设计意图：所有线程的事件共享一个全局时间原点，`ts` 直接可比，跨线程对比不失真。前提是时间戳本身跨线程一致——native 下 `SystemTime::now()` 天然一致；wasm 下则靠 u2-l3 讲过的 `time_origin` 叠加来保证。

**练习 2**：为什么 `cat` 不用 `pid` 来区分「不同 typst 编译过程」？

**参考答案**：typst-timing 是进程级单例（`EVENTS` 是全局静态量），假定一次进程生命周期就是一次关注的编译过程，所以 `pid` 固定 `1`、用 `clear()` + `export_json` 来界定「一次录制」。多轮录制由上层 timer.rs 用 `{n}` 文件名区分（见 4.3.4 与 u3-l4），而非塞进 `pid`。

---

### 4.3 Entry/Args 序列化与 std::mem::take 防死锁

#### 4.3.1 概念说明

本模块讲两个「看起来平淡、其实很关键」的细节：

1. **`std::mem::take` 防死锁**：导出时为什么要先把事件「搬出来」，而不是边持锁边序列化。
2. **`Entry`/`Args` 的序列化技巧**：为什么用局部结构体 + `&'static str` 字段，而不是直接 `serde_json::json!` 宏手写。

先说死锁。`EVENTS` 是一个 `parking_lot::Mutex`，而 **parking_lot 的 Mutex 不是可重入的（non-reentrant）**：同一线程若已经持锁，再 `lock()` 会死锁（parking_lot 会 panic 或死等，取决于版本/配置）。

现在考虑 `source` 闭包。这个闭包由调用方提供，它的职责是「解析 span」。在 typst 的真实集成里（timer.rs 的 `resolve_span`），解析 span 要访问 `World`，而访问 `World` 很可能触发更多编译活动，这些活动里**可能埋了 `timed!`**。一旦 `timed!` 执行，它内部就要 `EVENTS.lock().push(...)`。

于是危险出现了：如果 `export_json` 在**持有 `EVENTS` 锁**的情况下调用 `source`，而 `source` 又触发 `timed!` 去抢同一把锁——死锁。源码注释一针见血地指出了这一点（[crates/typst-timing/src/lib.rs#L122-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L122-L124)）。

#### 4.3.2 核心流程

`std::mem::take` 的解法是把「持锁时间」压缩到最短：

```text
加锁 -> 把 Vec 内容整体搬出（Vec 变空）-> 立即释放锁
                                       |
                                       v
                后续序列化（含 source 调用）全部在「无锁」状态下进行
```

`std::mem::take(&mut *EVENTS.lock())` 这一句干了三件事：

1. `EVENTS.lock()` 拿到 `MutexGuard`。
2. `&mut *...` 解引用成对内部 `Vec<Event>` 的可变引用。
3. `std::mem::take` 把这个 `Vec` 替换成默认值（空 `Vec`），返回原来的 `Vec`。
4. 整个表达式结束时 `MutexGuard` 的临时变量析构，**锁立即释放**。

之后 `source` 在序列化循环里被调用，即使它触发 `timed!`，也能顺利拿到锁（因为锁已经释放了）。

副作用：导出后 `EVENTS` 被清空，与文档注释「Will also internally clear the recorded events」一致（[crates/typst-timing/src/lib.rs#L100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L100)）。所以连续两次 `export_json`，第二次会得到空数组。

#### 4.3.3 源码精读

关键一行（[crates/typst-timing/src/lib.rs#L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L124)）：

```rust
// We take out the events to avoid a potential deadlock if `source` happens
// to invoke the timed things.
let events = std::mem::take(&mut *EVENTS.lock());
```

配合 `args` 的生成（[crates/typst-timing/src/lib.rs#L142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L142)）：

```rust
args: event.span.map(&mut source).map(|(file, line)| Args { file, line }),
```

这一行就是 `source` 被调用的地方。它位于序列化循环中，而循环操作的是**局部 `events`**，不再持有 `EVENTS` 锁——所以即便 `source` 触发 `timed!`，也是安全的。

再看真实集成里 `source` 的长相（[crates/typst-kit/src/timer.rs#L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L89-L92)）：

```rust
typst_timing::export_json(writer, |span| {
    resolve_span(world, Span::from_raw(span))
        .unwrap_or_else(|| ("unknown".to_string(), 0))
})?;
```

`resolve_span(world, ...)` 会调用 `world.source(id)` / `world.file(id)`（[crates/typst-kit/src/timer.rs#L99-L116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L99-L116)），这些访问完全可能在内部触发编译、进而触发 `timed!`。这就是「`source` 会反向调用 timed」的真实出处，也是 `std::mem::take` 必须存在的根本原因。

关于 `Entry`/`Args` 的字段类型选择：`name`/`cat`/`ph` 都是 `&'static str`，因为内部 `Event.name` 本就是 `&'static str`（u2-l1 讲过，名字必须是编译期字面量）。这让序列化时**零字符串拷贝、零堆分配**。配合「边遍历边序列化」，整条导出路径在热数据上是零额外分配的。

#### 4.3.4 代码实践

**实践目标**：理解 `resolve_span` 如何把裸 span 还原成 `(file, line)`，体会 `source` 闭包的真实职责。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [crates/typst-kit/src/timer.rs#L99-L116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L99-L116)，阅读 `resolve_span`。
2. 跟踪 `Span::from_raw(span) -> span.get()` 的两个分支：`SpanKind::Number` 与 `SpanKind::Range`，观察它们分别如何从 `world` 取 source/file，再把字节偏移 `range.start` 换算成行号 `byte_to_line`。
3. 注意末尾 `Some((format!("{id:?}"), line as u32 + 1))`：行号 `+1` 是为了把 0 基转成 1 基（人类习惯的行号）。

**需要观察的现象**：

- `resolve_span` 返回 `Option`，无法解析时 `export_json` 里的 `.unwrap_or_else(|| ("unknown". 0))` 兜底成 `("unknown", 0)`，即工具里会显示文件名 `unknown`、行号 `0`。
- 解析过程要访问 `world`，这是 `source` 可能「昂贵」、可能「反向触发 timed」的根源。

**预期结果**：你能在脑中画出「`span(NonZeroU64)` → `Span::from_raw` → `span.get()` → `world.source/file` → `byte_to_line` → `(file, line)`」这条链路。完整的 `Timer::record` 时序（`enable → clear → f → 写文件 → export_json`）将在 u3-l4 综合实践里完整串联，本讲先掌握「`source` = `resolve_span`」这一对应。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `std::mem::take(&mut *EVENTS.lock())` 改成 `let events = EVENTS.lock().clone();`（先不清空、只克隆），会有什么问题？

**参考答案**：两个问题——(a) 持锁时间变长（克隆整份 Vec 时一直持锁），且仍可能在后续 `source` 调用时死锁（因为这种改法通常会把锁留到序列化结束）；(b) 多了一份内存拷贝，失去零分配优势；(c) 导出后 `EVENTS` 不会被清空，下次导出会重复导出旧事件。`std::mem::take` 同时解决了这三点：持锁极短、零拷贝搬移、顺便清空。

**练习 2**：`Entry` 的字段为什么几乎全是引用类型（`&'static str`）而非 `String`？

**参考答案**：因为这些值要么来自 `Event` 的 `&'static str`（`name`），要么是硬编码字面量（`cat`、`ph`）。用 `&'static str` 避免在序列化时为每个事件分配 `String`，导出路径保持零额外堆分配。这正是 u2-l1 里「`name` 用 `&'static str`」决策在导出端的红利。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的导出。本实践是 u2 单元的总收口，也是 u3-l4 集成实战的简化预演。

**任务**：写一个程序，记录**带 span 与不带 span** 的混合事件，导出后在 Perfetto 中区分它们。

```rust
// 示例代码：综合实践
use std::num::NonZeroU64;
use typst_timing::{enable, timed, export_json, TimingScope};

fn main() {
    enable();

    // 不带 span 的任务
    timed!("parse", std::thread::sleep(std::time::Duration::from_millis(30)));

    // 带 span 的任务（模拟有源码位置）
    timed!("eval", span = NonZeroU64::new(7).unwrap(), {
        std::thread::sleep(std::time::Duration::from_millis(20));
        42 // 表达式原样返回
    });

    let mut buf = Vec::<u8>::new();
    export_json(&mut buf, |_span| ("demo.rs".to_string(), 10)).unwrap();
    std::fs::write("events.json", &buf).unwrap();
    println!("{}", String::from_utf8(buf).unwrap());
}
```

**验证清单**（请逐条核对）：

1. `parse` 事件：两条（B/E），`args` 为 `null`。
2. `eval` 事件：两条（B/E），`args` 为 `{"file":"demo.rs","line":10}`。
3. 第一个事件的 `ts` 为 `0.0`，其余 `ts` 单调递增。
4. `cat` 全为 `"typst"`，`pid` 全为 `1`，`tid` 全相同（单线程）。
5. 把 `events.json` 拖进 Perfetto，能看到两个色块，点开 `eval` 色块能看到 `args` 里的文件名和行号。

> 思考题：如果把上面的 `eval` 换成在**新线程**里执行（用 `std::thread::spawn`），观察 `tid` 会变成什么？（提示：回顾 u2-l2 的 `THREAD_DATA` 与自造线程号。）这会与 `events[0]` 的基准时间冲突吗？为什么不会？

---

## 6. 本讲小结

- `export_json<W: Write>(writer, source)` 是 typst-timing **唯一**的对外导出门面，把私有 `Event` 翻译成 Chrome Trace 标准 JSON，内部 `Event` 类型因此可以自由重构。
- `source: FnMut(NonZeroU64) -> (String, u32)` 闭包负责把裸 span 还原成 `(file, line)`，是 typst-timing 为规避循环依赖而「外包」出去的翻译职责；在 typst-kit 里它对应 `resolve_span(world, ...)`。
- Chrome Trace 字段映射是直译：`EventKind::Start/End` → `ph="B"/"E"`；`cat` 固定 `"typst"`；`pid` 固定 `1`；`tid` 来自自造 `u64`；`ts` 是相对**首个事件**的微秒差。
- `Entry`/`Args` 是函数体内的私有序列化外壳，字段大量用 `&'static str`，配合 `serialize_seq` 边遍历边写入，实现导出路径零额外堆分配。
- `std::mem::take(&mut *EVENTS.lock())` 把事件「搬出来」，把持锁时间压到最短，从而避免 `source` 反向触发 `timed!` 时死锁（parking_lot Mutex 不可重入），顺带清空了缓冲区。

## 7. 下一步学习建议

本讲讲完了「数据怎么出去」。接下来：

- **u3-l1（零成本与启用门控设计）**：回到 `enable/is_enabled` 与 `TimingScope::new` 返回 `Option` 的设计，分析「默认关闭」时为何几乎零开销。
- **u3-l4（集成实践：typst-kit Timer）**：本讲提到的 `Timer::record`、`resolve_span`、`{n}` 文件名规则将在那里完整串联，形成 `enable → clear → f → export_json` 的端到端时序，是本讲的天然续集。
- **延伸阅读**：直接阅读 [crates/typst-kit/src/timer.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs) 全文，对照本讲的 `export_json`，体会一个真实 crate 是如何把裸 span、`World`、文件写入串成一个可用的「性能录制器」的。
