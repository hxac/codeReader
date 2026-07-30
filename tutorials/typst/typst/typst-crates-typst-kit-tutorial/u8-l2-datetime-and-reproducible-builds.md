# DateTime 与可复现构建

## 1. 本讲目标

本讲聚焦 typst-kit 的 `datetime` 模块（源文件 `src/datetime.rs`，受 `datetime` 特性门禁），它只有唯一一个对外类型 `Time`，任务是回答 `World::today` 提出的一个问题：「今天是几号？」

学完本讲，你应当能够：

- 理解 `Time` 为什么需要 `Fixed` / `System` 两种内部表示，以及它们对「可复现构建」截然相反的影响。
- 掌握 `today()` 如何处理一个可选的 UTC 时区偏移（`Option<Duration>`），并把秒数安全地从 `f64` 转换为 `i32`。
- 理解 `SOURCE_DATE_EPOCH` 环境变量如何经 `fixed_timestamp` 实现可复现构建。
- 理解 `reset()` 在 `typst watch` 多次编译之间如何刷新系统时间，却又不破坏固定时间。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 World::today 契约

在 [u1-l3](u1-l3-modules-and-world-contract.md) 中我们知道，`World` trait 是 Typst 编译器与外界唯一的契约，共七个回调。其中 `today` 是唯一允许「拒绝回答」的回调——它返回 `Option<Datetime>`：

```rust
// crates/typst-library/src/lib.rs
fn today(&self, offset: Option<Duration>) -> Option<Datetime>;
```

[crates/typst-library/src/lib.rs:90-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L90-L97) 这段代码定义了 `today` 的契约：不传偏移就返回「本地日期」，传了偏移就返回「按该 UTC 偏移换算后的日期」；返回 `None` 时，Typst 脚本里的 `datetime.today()` 会报错。

关键术语：
- `Datetime`：Typst 自己的日期/时间值（年月日，或带时分秒），与底层 `chrono` 解耦。
- `Duration`：Typst 自己的时间段类型，`seconds()` 返回**总秒数**（`f64`），不是「秒分量」。

### 2.2 OnceLock 懒加载模式

`System` 变体用 `OnceLock` 在「首次访问时才真正取系统时间，之后全程复用」。这和 [u2-l1](u2-l1-fontstore-lazy-loading.md) 里 `FontSlot` 的 `OnceLock<Option<Font>>` 是同一个套路：外层 `OnceLock` 管「有没有初始化」，靠 `get_or_init` 保证初始化全局只发生一次。

### 2.3 什么是「可复现构建」

可复现构建（reproducible build）指：**同样的源码与构建环境，无论何时编译，产物都逐字节相同**。如果 Typst 把「今天日期」写进 PDF 元数据或文档内容，那么今天编译和明天编译就会得到不同字节——这破坏了可复现性。解决办法是：允许把时间**钉死**成一个固定值（通常是源码提交时刻）。业界标准做法是读 `SOURCE_DATE_EPOCH` 环境变量（一个 Unix 时间戳，秒），详见 <https://reproducible-builds.org/specs/source-date-epoch/>。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-kit/src/datetime.rs` | 定义 `Time`、`TimeInner` 及 `today/fixed/fixed_timestamp/system/reset` 全部逻辑，本讲核心。 |
| `crates/typst-cli/src/world.rs` | `SystemWorld` 持有 `now: Time` 字段，负责构造、`reset` 与 `World::today` 的转发。 |
| `crates/typst-cli/src/args.rs` | 用 clap 把 `SOURCE_DATE_EPOCH` 读成 `creation_timestamp: Option<i64>`。 |
| `crates/typst-library/src/lib.rs` | `World::today` 的 trait 契约（已在上文给出）。 |

`datetime` 特性在 `Cargo.toml` 里只做一件事——引入 `chrono` 依赖：

```toml
# crates/typst-kit/Cargo.toml
# Enables obtaining the current date via `datetime::Time::today`.
datetime = ["dep:chrono"]
```

[crates/typst-kit/Cargo.toml:72-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L72-L73) 说明整个模块的编译都挂在 `chrono` 上，而文件顶部用 `#![cfg(feature = "datetime")]` 把整文件门禁起来。

---

## 4. 核心概念与源码讲解

### 4.1 Time 与 TimeInner：两种时间表示

#### 4.1.1 概念说明

`Time` 要回答「今天是几号」，但现实中有两种截然相反的需求：

1. **交互式编译**（人手动跑一次 `typst compile`）：用系统时钟此刻的时间即可。
2. **可复现构建 / CI**：把时间钉死，保证产物逐字节稳定。

这两种需求不能由同一段逻辑满足，所以 `Time` 用一个枚举 `TimeInner` 区分两条路径：

- `Fixed(DateTime<Utc>)`：一个不可变的固定时刻，构造时确定，永不再变。
- `System(OnceLock<DateTime<Utc>>)`：一开始为空，首次被问到「今天」时才向系统要时间，并缓存到本次编译结束。

`Time` 本身只是这个枚举的一层 newtype 包装。

#### 4.1.2 核心流程

`Time` 的两条构造路径与一个查询入口的关系：

```
Time::system()        Time::fixed_timestamp(..) / Time::fixed(..)
        │                          │
        ▼                          ▼
   System(OnceLock)            Fixed(DateTime<Utc>)
        │                          │
        └──────────┬───────────────┘
                   ▼
            Time::today(offset)
```

- `Fixed` 路径：时间在构造瞬间就已确定，`today()` 每次都返回同一个值，天然可复现。
- `System` 路径：时间用 `OnceLock` 懒加载——首次 `today()` 调用 `get_or_init(Utc::now)` 抓取当前 UTC 时间并塞进锁里，此后同一次编译内的所有 `today()` 调用都读这个缓存值。这保证了 typst-cli 注释里强调的不变式：「在一次编译内时间始终相同」。

#### 4.1.3 源码精读

类型定义：

```rust
// src/datetime.rs
pub struct Time(TimeInner);

enum TimeInner {
    /// A fixed date and time.
    Fixed(DateTime<Utc>),
    /// The current date and time if the time is not externally fixed.
    System(OnceLock<DateTime<Utc>>),
}
```

[crates/typst-kit/src/datetime.rs:17-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L17-L25) 这段代码定义了 `Time` 的两层结构：对外暴露的 `Time` 只是个包装，真正的策略藏在私有枚举 `TimeInner` 里。

`System` 构造器极其简单——只造一个空 `OnceLock`：

```rust
pub fn system() -> Self {
    Time(TimeInner::System(OnceLock::new()))
}
```

[crates/typst-kit/src/datetime.rs:71-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L71-L74) 注意这里**完全不读系统时钟**，真正的取时被推迟到 `today()` 内部。

懒加载发生在 `today()` 开头：

```rust
TimeInner::System(time) => {
    let now_utc = time.get_or_init(Utc::now);
    ...
}
```

[crates/typst-kit/src/datetime.rs:86-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L86-L87) `get_or_init(Utc::now)` 是关键：若锁空则调一次 `Utc::now` 并写入，否则直接返回已有值。这与 `FontSlot::get` 调 `get_or_init(|| self.source.load())` 是完全相同的手法。

#### 4.1.4 代码实践

**实践目标**：验证 `System` 的「一次编译内时间不变」与 `Fixed` 的「永远不变」。

**操作步骤**：

下面是一段**示例代码**（非项目原有代码）。在一个新的 Cargo 项目中依赖 typst-kit 并启用 `datetime` 特性：

```toml
# Cargo.toml
[dependencies]
typst-kit = { path = "path/to/typst-kit", features = ["datetime"] }
typst-library = { path = "path/to/typst-library" }
time = "1"   # 仅用于构造一个 Duration
```

```rust
// 示例代码
use typst_kit::datetime::Time;

fn main() {
    // System：构造时不会读时钟
    let t = Time::system();
    let a = t.today(None);
    let b = t.today(None);
    println!("System today(None) x2: {a:?} / {b:?}");

    // Fixed：固定时间戳 1641067200 = 2022-01-01 20:00:00 UTC
    let f = Time::fixed_timestamp(1641067200).unwrap();
    println!("Fixed today(None) = {:?}", f.today(None));
}
```

**需要观察的现象**：

1. `a` 与 `b` 应当完全相等——证明 `OnceLock` 缓存了第一次的结果。
2. `Fixed` 的 `today(None)` 永远返回同一个日期，与运行时刻无关。

**预期结果**：`a == b`；`Fixed today(None)` 返回 `2022-01-01`（详见 4.2 的推算）。具体输出**待本地验证**（取决于你运行时的系统时区）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `System` 变体里存的是 `OnceLock<DateTime<Utc>>`，而不是直接 `DateTime<Utc>`？

> **参考答案**：因为取系统时间有副作用（且代表「此刻」），必须保证同一次编译内只取一次、之后全程复用，否则文档里多次出现的 `datetime.today()` 可能跨过午夜而自相矛盾。`OnceLock` 用 `get_or_init` 提供了「至多初始化一次」的内部可变性，让 `today(&self)`（只读借用）也能完成首次抓取。`Fixed` 则是不可变值，无需这层封装。

**练习 2**：如果把 `datetime` 特性关掉，`Time` 还存在吗？

> **参考答案**：不存在。文件顶部 `#![cfg(feature = "datetime")]` 把整文件门禁，关掉特性后整个模块不参与编译；这正是 typst-kit「默认全关、按需付费」哲学的体现（见 [u1-l2](u1-l2-feature-flags.md)）。

---

### 4.2 today()：可选时区偏移下的日期计算

#### 4.2.1 概念说明

`World::today` 的契约是：无偏移返回**本地日期**，有偏移返回「UTC 时间加上该偏移后的日期」。`today()` 要从一个时刻（instant）算出一个公历日期（年/月/日），并尊重调用方传来的可选时区偏移。难点有两个：

1. `Fixed` 与 `System` 对「无偏移」的解读不同（见下文）。
2. 偏移来自 Typst 的 `Duration`，其 `seconds()` 是 `f64`，必须安全地转成 `chrono::FixedOffset` 所需的 `i32`。

#### 4.2.2 核心流程

`today()` 分三步走：

1. **取基准时刻 `now`（`DateTime<FixedOffset>`）**
   - `Fixed(time)` → `time.fixed_offset()`：把 UTC 时刻转成偏移为 `+0` 的 `FixedOffset` 表示。
   - `System(time)` → 先 `get_or_init(Utc::now)` 拿到 UTC 时刻：
     - 若调用方传了偏移：保留为 UTC（`fixed_offset()`，偏移留到第 2 步应用）；
     - 若调用方没传偏移：转成本地时区 `Local`（这才是「本地日期」）。

2. **应用偏移**（仅当 `offset` 是 `Some`）
   - `seconds = offset.seconds().trunc()`：总秒数取整。
   - 安全检查：`seconds` 必须有限且落在 `i32` 范围内，否则直接返回 `None`。
   - `now.with_timezone(&FixedOffset::east_opt(seconds as i32)?)`：换算到目标偏移下的同一时刻。

3. **抽出年月日**：`Datetime::from_ymd(year, month, day)`。

关于 `f64 → i32` 的安全性，关键是这一段判定：

\[
\text{valid} \iff s \text{ 有限} \;\land\; \text{I32\_MIN} \le s \le \text{I32\_MAX}
\]

其中 \(s = \lfloor \text{offset.seconds()} \rfloor\)。`FixedOffset::east_opt` 还额外要求秒数落在 \([-86400, 86400]\)（一整天）范围内，超出时返回 `None`（由 `?` 传播）。

一个容易踩坑的**关键差异**：对 `Fixed` 变体，即使调用方不传偏移，返回的也是 **UTC 日期**（而非本地日期）。这是有意为之——可复现构建要求结果与运行机器的时区无关，所以 `Fixed` 一律以 UTC 为基准；只有 `System` 才在「无偏移」时退回本地时区。

#### 4.2.3 源码精读

第一步——取基准时刻：

```rust
let now = match &self.0 {
    TimeInner::Fixed(time) => time.fixed_offset(),
    TimeInner::System(time) => {
        let now_utc = time.get_or_init(Utc::now);
        if offset.is_some() {
            now_utc.fixed_offset()        // 偏移稍后统一处理
        } else {
            now_utc.with_timezone(&Local).fixed_offset()  // 本地日期
        }
    }
};
```

[crates/typst-kit/src/datetime.rs:84-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L84-L95) 注意 `Fixed` 分支从不碰 `Local`——这正是可复现性的来源。

第二步——应用偏移：

```rust
let with_offset = match offset {
    None => now,
    Some(offset) => {
        let seconds = offset.seconds().trunc();
        if !seconds.is_finite()
            || seconds < f64::from(i32::MIN)
            || seconds > f64::from(i32::MAX)
        {
            return None;
        }
        now.with_timezone(&FixedOffset::east_opt(seconds as i32)?)
    }
};
```

[crates/typst-kit/src/datetime.rs:98-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L98-L111) 这里有两道防线：`f64` 到 `i32` 的范围检查，以及 `east_opt` 对「一整天」范围的检查。任何一道不过关都返回 `None`，让上层 Typst 的 `datetime.today()` 报错。

第三步——抽出年月日：

```rust
Datetime::from_ymd(
    with_offset.year(),
    with_offset.month().try_into().ok()?,
    with_offset.day().try_into().ok()?,
)
```

[crates/typst-kit/src/datetime.rs:113-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L113-L118) `from_ymd` 的签名是 `(year: i32, month: u8, day: u8)`，而 chrono 的 `month()/day()` 返回 `u32`，故用 `try_into().ok()?` 把 `u32` 收窄为 `u8`，溢出时安全返回 `None`。

#### 4.2.4 代码实践

**实践目标**：直观对比「无偏移」与「带偏移」的日期差异，体会时区换算。

**操作步骤**：在 4.1.4 的项目里继续加入（示例代码）：

```rust
use typst_library::foundations::Duration;
// 1641067200 = 2022-01-01 20:00:00 UTC
let f = Time::fixed_timestamp(1641067200).unwrap();

let east8 = Duration::from(time::Duration::hours(8)); // 东八区 +8h

println!("无偏移 (UTC)      : {:?}", f.today(None));
println!("+8h 偏移 (UTC+8) : {:?}", f.today(Some(east8)));
```

**需要观察的现象与预期结果**（推算如下，**待本地验证**）：

| 调用 | 基准时刻 | 换算后时刻 | 日期 |
|------|----------|-----------|------|
| `today(None)` | 2022-01-01 20:00 UTC | 同（`Fixed` 无偏移走 UTC） | **2022-01-01** |
| `today(Some(+8h))` | 同一时刻 | 2022-01-02 04:00 UTC+8 | **2022-01-02** |

关键看点：同一个固定时刻，仅仅因为换了个时区偏移，日期就从 1 日跳到了 2 日。这正好说明 `with_timezone` 改的是「显示偏移」、不变的是「绝对时刻」。

**延伸**：再对 `Time::system()` 调 `today(Some(east8))`，你将得到「当前时刻在东八区下的日期」，而非机器本地时区的日期（见 4.2.2 中 `System` + 有偏移时保留 UTC 的逻辑）。

#### 4.2.5 小练习与答案

**练习 1**：若调用 `today(Some(very_huge_duration))`（比如偏移超过 `i32::MAX` 秒），会发生什么？

> **参考答案**：`offset.seconds()` 会超过 `f64::from(i32::MAX)`，命中第二步的范围检查，`today` 直接返回 `None`；上层 Typst 的 `datetime.today(offset: ..)` 因此报错，而不是 panic 或溢出。这是一处典型的「把不可表示的输入降级为 `Option`」的防御式编程。

**练习 2**：为什么对 `Fixed` 变体，「无偏移」返回 UTC 日期而非本地日期？

> **参考答案**：可复现构建要求产物与运行机器的时区无关。若 `Fixed` 在「无偏移」时走本地时区，那么同一份 `SOURCE_DATE_EPOCH` 在北京和纽约编译会得到不同日期，破坏可复现性。因此 `Fixed` 一律以 UTC 为基准；只有真正的 `System` 时间才有「本地」概念。

---

### 4.3 可复现构建：fixed / fixed_timestamp 与 SOURCE_DATE_EPOCH

#### 4.3.1 概念说明

可复现构建需要一个「被钉死的时间」。`Time` 提供两条固定路径：

- `fixed_timestamp(i64)`：直接吃一个 Unix 时间戳（秒），最常见的入口，对应 `SOURCE_DATE_EPOCH`。
- `fixed(Datetime)`：吃一个 Typst 的 `Datetime` 值（日期或日期时间），更面向编程式构造。

两者都产出 `TimeInner::Fixed`，从而让 `today()` 变成纯函数——同样的输入永远得到同样的输出。typst-cli 则按惯例从 `SOURCE_DATE_EPOCH` 环境变量读时间戳，让用户无需改代码即可获得可复现性。

#### 4.3.2 核心流程

端到端的可复现构建链路：

```
SOURCE_DATE_EPOCH 环境变量（Unix 秒，如 "1640995200"）
        │  clap 的 env = "SOURCE_DATE_EPOCH"
        ▼
creation_timestamp: Option<i64>          （typst-cli/src/args.rs）
        │  若 Some(t) → Time::fixed_timestamp(t)
        │  若 None    → Time::system()
        ▼
SystemWorld.now: Time                    （typst-cli/src/world.rs）
        │
        ▼
World::today → now.today(offset)         （恒定输出，可复现）
```

`fixed_timestamp` 的实现非常薄：

```rust
pub fn fixed_timestamp(timestamp: i64) -> StrResult<Self> {
    Ok(Time(TimeInner::Fixed(
        DateTime::from_timestamp(timestamp, 0).ok_or("timestamp is out of range")?,
    )))
}
```

[crates/typst-kit/src/datetime.rs:65-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L65-L69) 把 `(timestamp, 0)`（秒 + 纳秒）交给 chrono 的 `from_timestamp` 转成 `DateTime<Utc>`；若时间戳超出 chrono 可表示范围则返回 `None`，用 `ok_or` 转成字符串错误。

`fixed(Datetime)` 稍复杂，因为它要兼容 Typst `Datetime` 的三种形态（`Date` / `Datetime` / `Time`）：

```rust
pub fn fixed(datetime: Datetime) -> StrResult<Self> {
    let date = match datetime {
        Datetime::Date(d) => d,
        Datetime::Datetime(dt) => dt.date(),
        Datetime::Time(_) => bail!("fixed datetime must specify a date"),
    };
    // ... 把 (year, month, day, h, m, s) 当作 naive UTC 组装成 DateTime<Utc>
}
```

[crates/typst-kit/src/datetime.rs:32-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L32-L56) 注意三点：① 只有 `Time`（无日期）会被 `bail!` 拒绝，因为「今天是几号」没有日期就无解；② 缺省的时/分/秒用 `unwrap_or(0)` 补零；③ 用 `from_naive_utc_and_offset(.., Utc)` 把这个 naive 时刻**当作 UTC** 来固定——这是可复现性的另一处体现（不引入任何本地时区）。

在 typst-cli 一侧，clap 把环境变量直连到字段：

```rust
/// The document's creation date formatted as a UNIX timestamp.
#[clap(
    long = "creation-timestamp",
    env = "SOURCE_DATE_EPOCH",
    value_name = "UNIX_TIMESTAMP"
)]
pub creation_timestamp: Option<i64>,
```

[crates/typst-cli/src/args.rs:421-429](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L421-L429) clap 的 `env = "SOURCE_DATE_EPOCH"` 让命令行 `--creation-timestamp` 与环境变量等价，二选一即可。

`SystemWorld::new` 据此二选一构造 `now`：

```rust
let now = match world_args.creation_timestamp {
    Some(time) => Time::fixed_timestamp(time)
        .map_err(|_| WorldCreationError::InvalidTimestamp)?,
    None => Time::system(),
};
```

[crates/typst-cli/src/world.rs:70-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L70-L74) 有时间戳就 `Fixed`（可复现），否则 `System`（实时）。注意 `fixed_timestamp` 的字符串错误被映射成专门的 `WorldCreationError::InvalidTimestamp`（时间戳越界）。

#### 4.3.3 源码精读

（已在 4.3.2 中给出全部关键引用：`datetime.rs:32-56`、`datetime.rs:65-69`、`args.rs:421-429`、`world.rs:70-74`。）

补充一处：`SystemWorld` 的字段注释明确写出设计意图：

```rust
/// The current datetime if requested. This is stored here to ensure it is
/// always the same within one compilation.
/// Reset between compilations if not [`Time::Fixed`].
now: Time,
```

[crates/typst-cli/src/world.rs:34-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L34-L37) 这段注释浓缩了本讲两个核心不变式：编译内一致 + 编译间按需 reset。

#### 4.3.4 代码实践

**实践目标**：用真实的 typst-cli 体验 `SOURCE_DATE_EPOCH` 的可复现效果，无需写 Rust。

**操作步骤**：

1. 准备一个最小文档 `date.typ`：

   ```typst
   #datetime.today().display()
   ```

2. 第一次不设环境变量，正常编译（连编两次）：

   ```bash
   typst compile date.typ out1.pdf
   typst compile date.typ out2.pdf
   ```

3. 第二次显式钉死时间戳，连编两次：

   ```bash
   SOURCE_DATE_EPOCH=1640995200 typst compile date.typ out_a.pdf
   SOURCE_DATE_EPOCH=1640995200 typst compile date.typ out_b.pdf
   ```

**需要观察的现象**：

- 步骤 2：两次产物中的日期都是「今天」（本地日期）。
- 步骤 3：两次产物中的日期都变成 `2022-01-01`（UTC），且 `out_a.pdf` 与 `out_b.pdf` 完全一致。

**预期结果**：步骤 3 演示了可复现构建——只要钉死 `SOURCE_DATE_EPOCH`，无论何时、何地（不同时区）编译，日期输出恒定。具体字节是否完全一致**待本地验证**（取决于文档其余部分是否也满足可复现条件）。

#### 4.3.5 小练习与答案

**练习 1**：`fixed_timestamp` 和 `fixed` 都把时间「当作 UTC」处理。如果改用本地时区，会破坏什么？

> **参考答案**：会破坏可复现性。同一个 `SOURCE_DATE_EPOCH=1640995200`，在北京（UTC+8）会被解释成 2022-01-01 08:00 本地，在纽约（UTC-5）会被解释成 2021-12-31 19:00 本地，导致同一份输入在不同机器上产生不同日期甚至不同日期的产物。固定为 UTC 才能让结果与时区无关。

**练习 2**：为什么 `fixed(Datetime::Time(..))` 要 `bail!`？

> **参考答案**：`Time` 只有时分秒、没有日期，而 `World::today` 要回答的是「今天是几号」。没有日期就无法构造 `DateTime<Utc>`，也无法回答任何日期问题，所以直接报错而非静默返回错误结果。

---

### 4.4 reset()：多次编译间刷新系统时间

#### 4.4.1 概念说明

`typst watch` 会在一次进程内**反复编译**：每次源码变动就重新编译一次。这对 `now` 提出了一对矛盾要求：

- `System` 时间必须**每次编译重新取**——否则 watch 跨过午夜时，文档里的「今天」不会更新。
- `Fixed` 时间必须**永不重置**——它是被钉死的可复现基准，重置毫无意义。

`reset()` 用一个 `if let` 精准满足两者：只清 `System` 的 `OnceLock`，对 `Fixed` 啥也不做。

#### 4.4.2 核心流程

```
每次编译结束 ──► SystemWorld::reset()
                        │
           ┌────────────┴────────────┐
           ▼                         ▼
     files.reset()              now.reset()
                                      │
                          ┌───────────┴───────────┐
                          ▼                       ▼
                System → take() 清空        Fixed → 无操作
                          │
                          ▼
        下次 today() 再次触发 get_or_init(Utc::now)
```

#### 4.4.3 源码精读

`reset` 实现只有几行：

```rust
pub fn reset(&mut self) {
    if let TimeInner::System(ref mut time_lock) = self.0 {
        time_lock.take();
    }
}
```

[crates/typst-kit/src/datetime.rs:124-128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/datetime.rs#L124-L128) `OnceLock::take()` 在锁有值时取出并清空、锁空时返回 `None`。这里丢弃返回值，只取「清空」的副作用。注意签名是 `&mut self`——因为要改内部状态。

typst-cli 在编译间的统一重置点调用它：

```rust
/// Reset the compilation state in preparation of a new compilation.
pub fn reset(&mut self) {
    self.files.reset();
    self.now.reset();
}
```

[crates/typst-cli/src/world.rs:104-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L104-L107) 文件状态（`FileStore`，见 [u3-l1](u3-l1-filestore-and-fileloader.md)）与时间状态（`Time`）在同处重置，构成「为下一次编译做准备」的统一入口。

最后是 `World` 实现里的一行转发：

```rust
fn today(&self, offset: Option<Duration>) -> Option<Datetime> {
    self.now.today(offset)
}
```

[crates/typst-cli/src/world.rs:142-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L142-L144) `SystemWorld::today` 原样转发给 `Time::today`，印证 typst-kit 的 `Time` 就是 `World::today` 的现成积木。

#### 4.4.4 代码实践

**实践目标**：验证 `reset` 只对 `System` 生效、对 `Fixed` 无影响。

**操作步骤**（示例代码，接 4.1.4 的项目）：

```rust
// System：reset 后下次 today() 会重新读时钟
let mut t = Time::system();
let before = t.today(None);
t.reset();                 // 清空 OnceLock
let after = t.today(None); // 重新 get_or_init(Utc::now)
println!("System reset 前后: {before:?} / {after:?}");

// Fixed：reset 是空操作
let mut f = Time::fixed_timestamp(1641067200).unwrap();
let fb = f.today(None);
f.reset();                 // 命中 Fixed，什么都不做
let fa = f.today(None);
println!("Fixed reset 前后: {fb:?} / {fa:?}");
```

**需要观察的现象**：

1. `System` 的 `before` 与 `after` 通常相同（除非恰好跨午夜），但二者来自**两次独立的** `Utc::now` 调用——说明 `reset` 确实清了缓存。
2. `Fixed` 的 `fb` 与 `fa` 恒等，且永远等于固定值。

**预期结果**：`Fixed` 前后完全一致；`System` 前后通常一致但已被允许变化。精确输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果 `reset` 不区分变体，对 `Fixed` 也「重置」会怎样？

> **参考答案**：语义上 `Fixed` 是不可变值，没有「重置」的概念；即便强行覆盖，也没有别的值可填。当前实现用 `if let TimeInner::System(..)` 把 `Fixed` 排除在外，既正确又零开销（`Fixed` 分支连 `take` 都不调）。

**练习 2**：为什么 `reset` 用 `&mut self`，而 `today` 只用 `&self`？

> **参考答案**：`today` 的「首次写缓存」靠 `OnceLock` 的内部可变性完成，所以只需共享借用 `&self`；而 `reset` 要**清空** `OnceLock`（`take` 需要 `&mut OnceLock`），故必须独占借用 `&mut self`。在 typst-cli 中，`SystemWorld::reset(&mut self)` 与 `World::today(&self)` 的借用权限恰好分别匹配。

---

## 5. 综合实践

**任务**：用 `Time` 模拟一个「迷你世界时钟」，把同一个固定时刻同时显示在三个时区上。

**要求**：

1. 用 `Time::fixed_timestamp(1641067200)`（即 2022-01-01 20:00:00 UTC）构造一个可复现的 `Time`。
2. 分别用 `today(None)`、`today(Some(Duration::from(time::Duration::hours(8))))`（东八区）、`today(Some(Duration::from(time::Duration::hours(-5))))`（西五区）查询日期。
3. 把三个结果打印成一张表，并用文字解释为什么三者可能落在不同的公历日期上。
4. 进一步：把 `Time` 包进一个最小的 `World` 桩（只实现 `today`，其余回调可 `todo!()` 或返回默认值），验证 `World::today` 契约能被你的 `Time` 直接满足。

**预期结论**：同一时刻 2022-01-01 20:00 UTC 在西五区是 2022-01-01 15:00（仍 1 日）、在 UTC 是 2022-01-01（1 日）、在东八区是 2022-01-02 04:00（2 日）。这把本讲的「`Fixed` 以 UTC 为基准」「偏移换算改显示不改时刻」「`f64→i32` 安全转换」三个知识点串了起来。完整可运行性**待本地验证**（需正确配置 typst-kit / typst-library 的 `time` 依赖与 feature）。

---

## 6. 本讲小结

- `Time` 是 `World::today` 的现成积木，内部用枚举 `TimeInner` 区分两条路径：`Fixed(DateTime<Utc>)`（可复现）与 `System(OnceLock<DateTime<Utc>>)`（实时）。
- `System` 复用 [u2-l1](u2-l1-fontstore-lazy-loading.md) 的 `OnceLock` 懒加载手法：构造时不读时钟，首次 `today()` 才 `get_or_init(Utc::now)`，保证一次编译内时间恒定。
- `today()` 三步走：取基准时刻 → 安全应用可选偏移（`f64→i32` 范围检查 + `FixedOffset::east_opt`）→ 抽年月日；任何一步不可表示都返回 `None`。
- 关键差异：`Fixed` 即便无偏移也返回 UTC 日期（与时区无关），只有 `System` 在无偏移时才退回本地时区——这是可复现性的核心保证。
- `SOURCE_DATE_EPOCH` 经 clap 的 `env` 直连 `creation_timestamp`，再由 `SystemWorld::new` 选择 `fixed_timestamp` 或 `system()`，让可复现构建「零代码改动」即可启用。
- `reset(&mut self)` 只 `take` `System` 的 `OnceLock`、对 `Fixed` 无操作，支撑 `typst watch` 多次编译间刷新时间却不破坏固定基准。

---

## 7. 下一步学习建议

- 本讲是 u8「性能追踪与时间处理」单元的第二讲，也是 typst-kit 全部 9 个模块的最后一讲。建议回头重读 [u1-l3](u1-l3-modules-and-world-contract.md)，把 `fonts`/`files`/`datetime` 三个直接服务 `World` 的数据源放在一起对照——你会发现它们都用 `OnceLock`/`LazyHash` 实现「按需取值 + 编译内一致」的统一范式。
- 若对可复现构建感兴趣，可继续阅读 typst-cli 里 `info.rs` 的 `SOURCE_DATE_EPOCH` 读取与 PDF 元数据写入路径，看看固定时间是如何流进最终产物的。
- 若想深入 `chrono` 的时区模型，可对照阅读 `DateTime<Utc>`、`DateTime<FixedOffset>`、`DateTime<Local>` 三者通过 `with_timezone` 互转的语义——这正是本讲 `today()` 第二步的底层原理。
- 至此 typst-kit 学习手册全部讲义已完成。建议用一个真实的 `typst watch` 场景，把字体（u2）、文件（u3）、包（u4）、时间（本讲）、热重载（u7）这几条链路在运行中串起来观察，巩固对「积木库如何拼装成一个 World」的整体理解。
