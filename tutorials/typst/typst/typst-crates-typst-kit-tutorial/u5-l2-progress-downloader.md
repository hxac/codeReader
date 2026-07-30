# 进度下载：ProgressDownloader 与 ProgressReader

## 1. 本讲目标

本讲承接 u5-l1（`Downloader` trait 与 `SystemDownloader`）。在上一讲里，我们已经知道 `Downloader` 是 typst-kit 唯一的网络下载入口，并把 `stream` 的 `key: &dyn Any` 参数暂时搁置。本讲就围绕这个 `key` 展开——讲清楚 typst-kit 如何**在不修改任何下载逻辑的前提下，给一个已有的下载器“无侵入地”加上进度条**。

学完本讲，你应该能够：

1. 理解 `ProgressDownloader` 的“装饰器（wrapper）”设计：它包裹一个内层 `Downloader`，本身不碰网络。
2. 掌握 `key`（如 `PackageSpec` 或 `"package index"` 字符串）如何让上层在工厂闭包里**决定是否显示进度**。
3. 掌握 `ProgressReader` 的分块读取循环、`SAMPLES = 25` 的滑动采样窗口、以及“按固定周期回调”的逻辑。
4. 读懂 `Progress` 的 `Display` 实现：它如何用滑动采样算出 bytes/s 速率与 ETA，以及 `format_byte_unit` 的单位换算。
5. 能动手实现一个最小的 `ProgressReporter`，把进度打印出来。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：进度条本质上是一个“边读边报”的循环。** 网络下载返回的是一个 `Read`（流式读取器）。如果你想显示“已下载 X 字节”，就必须**自己驱动这个 `Read` 的读取循环**，每读一段就累加字节数并刷新显示。标准库的 `read_to_end` 把整段读完后一次性返回，它内部虽然也在循环读取，但不会把“读了多少”回调给你。所以进度上报的关键，是**重新接管读取循环**——这正是 `ProgressReader` 存在的理由。

**直觉二：装饰器模式（Wrapper / Decorator）。** 如果让 `SystemDownloader` 自己去管进度条，下载逻辑和显示逻辑就耦合死了，而且无法复用。typst-kit 的做法是：让 `ProgressDownloader` 实现同一个 `Downloader` trait，内部持有一个真正的下载器 `inner`。对外它“长得像”一个普通下载器，对内它把 `inner` 返回的 reader 包一层 `ProgressReader` 再交给上层。这样任何实现了 `Downloader` 的类型都能被“升级”成带进度的版本，互不干扰。

**直觉三：用 `key` 把“是否显示”的决定权交给调用方。** typst-kit 不知道终端长什么样、不知道哪些下载值得给用户看。于是它给每次下载附带一个动态类型 `key: &dyn Any`（u5-l1 已介绍），把“这次下载该不该显示、显示成什么名字”完全交给上层的工厂闭包决定。例如包下载传 `PackageSpec`，包索引下载传 `"package index"`，上层用 `downcast` 区分，前者显示进度条，后者静默。

> 关键术语回顾：`&dyn Any`（动态类型 trait object，可运行时 `downcast_ref` 还原具体类型）、`Downloader::stream`（返回 `(大小提示, reader)` 的流式原语）。两者都是 u5-l1 的内容。

## 3. 本讲源码地图

本讲几乎全部围绕 typst-kit 的一个文件展开，并补充两个“调用方”视角的文件来印证设计意图。

| 文件 | 作用 |
| --- | --- |
| `src/downloader.rs` | 本讲主角。定义 `ProgressDownloader`、`ProgressReader`、`ProgressReporter` trait、`Progress` 四个类型，全部进度逻辑都在这里。 |
| `crates/typst-cli/src/download.rs` | 真实的 `ProgressReporter` 实现 `PrintProgress`，以及用 `key` 工厂闭包决定显示策略的范例。 |
| `crates/typst-kit/src/packages.rs` | 展示两类真实的 `key`：包下载用 `PackageSpec`、包索引用 `"package index"`。 |

注意：`src/downloader.rs` 这个模块**不受任何 feature 门禁**（`pub mod downloader;` 在 [lib.rs:54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/lib.rs#L54) 是常驻声明）。也就是说，`Downloader`、`ProgressDownloader`、`ProgressReporter`、`Progress` 这四个类型**总是可用**；只有 `SystemDownloader` 才需要 `system-downloader` 特性。这意味着本讲的练习代码无需开启任何特性即可编译。

## 4. 核心概念与源码讲解

### 4.1 ProgressDownloader：装饰器 + key 工厂

#### 4.1.1 概念说明

`ProgressDownloader` 是一个泛型结构体，它**不下载任何东西**，只做两件事：

1. 持有一个真正的内层下载器 `inner: T`（`T: Downloader`）。
2. 持有一个工厂闭包 `progress: F`（`F: Fn(&dyn Any) -> P`）。每次下载开始时，它用本次的 `key` 调用这个闭包，**生产出一个全新的 `ProgressReporter` 实例 `P`** 来负责这次下载的显示。

为什么是“每次下载生产一个新 reporter”？因为下载可能并发（虽然 typst 当前是串行的），而且每次下载的名字、状态都不同。用工厂闭包按需生产，比维护一个全局可变 reporter 要干净。

这个设计的精髓在于：**上层通过闭包对 `key` 做 `downcast`，决定要不要显示、显示成什么名字。** typst-kit 自己不做这个决定。

#### 4.1.2 核心流程

`ProgressDownloader` 实现了 `Downloader` trait 的两个方法，但它的实现方式与“普通转发”不同，存在一个**不对称**：

```text
上层调用 ProgressDownloader.download(key, url)
   └─ 调 inner.stream(key, url)        ← 拿到 (大小提示, reader)，流式
   └─ progress 闭包(key) → 生产一个 reporter
   └─ 用 ProgressReader 包住 reader，自己驱动读取循环（带进度上报）
   └─ 返回完整 Vec<u8>

上层调用 ProgressDownloader.stream(key, url)
   └─ 调 inner.download(key, url)      ← 注意：是内层的 download，已全量读入内存！
   └─ 把结果包进 Cursor 再返回         ← 不经过 ProgressReader，不上报进度
```

也就是说：**只有走 `download()` 这条路才会真正上报进度；走 `stream()` 这条路会退化为“全量缓冲后假装成流”，且不上报进度。** 原因是进度上报必须自己驱动读取循环，而 `stream()` 的契约要求把 reader 交还给上层、由上层驱动读取——`ProgressReader` 没机会插手。所以这里做了一个务实的取舍：要进度就用 `download()`。

> 提示：本讲的 `key` 始终是 `&dyn Any`。在工厂闭包里，上层用 `key.downcast_ref::<PackageSpec>()` 之类的手段还原具体类型来决策。这部分逻辑不在 typst-kit 内，而在 typst-cli（见 4.2）。

#### 4.1.3 源码精读

先看结构体定义与三个泛型约束：`T`（内层下载器）、`F`（工厂闭包）、`P`（reporter），并持有一个 `period: Duration`（进度刷新周期）。

[downloader.rs:213-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L213-L220)：`ProgressDownloader` 结构体定义。注意闭包签名 `F: Fn(&dyn Any) -> P`——它接收 `key`，产出 `P`。

构造函数有两个：`new` 使用固定的 100ms 周期，`with_interval` 允许自定义周期。

[downloader.rs:229-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L229-L235)：`new` 默认 `period = Duration::from_millis(100)`，即每 100ms 最多刷新一次进度。

[downloader.rs:238-240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L238-L240)：`with_interval` 让调用方自定义周期，本讲练习会用到它（把周期调小以便观察到 update 回调）。

接下来是最关键的 `impl Downloader`，注意 `download` 与 `stream` 的不对称实现：

[downloader.rs:249-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L249-L255)：`download` 的实现。先 `inner.stream` 拿 reader，再 `(self.progress)(key)` 生产 reporter，最后用 `ProgressReader::new(...).download()` 驱动读取并上报。这一行就是“装饰器接管读取循环”的总入口。

[downloader.rs:257-264](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L257-L264)：`stream` 的实现。它直接调 `inner.download`（全量读入），再用 `Cursor` 包装返回，`Some(data.len())` 作为精确大小提示。这里**没有** `ProgressReader`，所以不上报进度——印证了上面讲的不对称。

#### 4.1.4 代码实践（源码阅读型）

对照上面两段源码，回答：如果上层通过 `stream()` 拿到 reader，`Progress` 的 `samples` 字段会不会被填充？为什么？再思考：为什么 typst-kit 没有在 `stream()` 里也加进度上报？（提示：谁在驱动读取循环。）

- 需要观察的现象：`stream()` 路径不创建 `ProgressReader`。
- 预期结论：`samples` 不会变化；进度上报要求“读取循环由自己驱动”，而 `stream()` 把驱动权交还给了上层。

#### 4.1.5 小练习与答案

**练习 1**：`ProgressDownloader` 为什么是泛型 `<T, F, P>` 而不是直接持有 `Box<dyn Downloader>` 和 `Box<dyn ProgressReporter>`？

**参考答案**：泛型让具体类型在编译期确定，调用闭包 `F` 与产出 `P` 都是静态分发、零开销；而 reporter 是“每次下载新生成一个”，用单一 `Box<dyn ProgressReporter>` 字段无法表达这种“按 key 工厂生产”的语义。内层 `T` 也可以是任意 `Downloader` 实现（包括 `SystemDownloader` 或测试用的假实现），泛型最自然。

**练习 2**：`download()` 里调用的是 `self.inner.stream(...)` 而不是 `self.inner.download(...)`，为什么？

**参考答案**：因为 `ProgressReader` 需要一个**流式 reader** 来自己分块驱动；如果调 `inner.download`，数据已经被 `read_to_end` 全量读进 `Vec` 了，再无“边读边报”的可能。所以必须取内层的 `stream`，把 reader 拿出来交给 `ProgressReader`。

---

### 4.2 ProgressReporter trait：上报契约

#### 4.2.1 概念说明

`ProgressReporter` 是一个极简的 trait，定义了**三个生命周期回调**：`start`（开始）、`update`（进行中，可能被调用 0 次或多次）、`finish`（结束）。每个回调都接收一个 `&Progress`，里面装着当前下载的全部统计信息（已下载字节、总大小、采样窗口等，见 4.4）。

这个 trait 是“显示策略”的抽象：你可以实现成“在终端清行重写一行”、实现成“写日志”、实现成“更新 TUI 进度条”、甚至实现成“什么都不做”。typst-kit 不关心你怎么显示，只负责在合适的时机把 `&Progress` 喂给你。

#### 4.2.2 核心流程

```text
下载开始        → ProgressReader 调 reporter.start(&progress)
每经过一个 period → ProgressReader 调 reporter.update(&progress)   （可能 0 次）
下载结束        → ProgressReader 调 reporter.finish(&progress)
```

注意 `update` 的次数取决于“下载耗时是否跨越了至少一个 `period`”。一个极快的内存下载，可能从 `start` 直接到 `finish`，中间 `update` 一次都不调。这是本讲练习需要刻意“放慢”读取速度的原因。

#### 4.2.3 源码精读

[downloader.rs:280-289](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L280-L289)：`ProgressReporter` trait 定义，只有三个方法、都接收 `&Progress`。它不带 `Send/Sync` 约束——因为 reporter 由驱动读取循环的同一线程独占使用，无需跨线程。

接下来看真实的实现。typst-cli 的 `download.rs` 给出了“如何用 `key` 决定显示策略”的最佳范例：

[download.rs:22-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L22-L31)：工厂闭包。它对 `key` 做两次 `downcast_ref`：若是 `PackageSpec` 则显示包名；若是值为 `"release"` 的 `&str` 则显示 `"release"`（用于 typst 自更新二进制）；**否则返回 `None`**——也就是不显示。typst-kit 下载包索引用的 key 是 `"package index"`（见下方），它落进 `else` 分支，所以**包索引下载是静默的**。这正是“用 key 把决定权交给上层”的体现。

再看 `PrintProgress` 如何实现这三个回调：

[download.rs:38-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L38-L63)：`PrintProgress(Option<EcoString>)`。`start` 时若有名字则打印 `downloading {name}` 标题，再调一次 `update`；`update` 用 `clear_last_line` + `writeln` 实现“原地刷新一行”；`finish` 直接转发给 `update`。注意所有 `if self.0.is_some()` 守卫——名字为 `None` 时三个回调都变成空操作，于是“不显示”与“显示”共用同一份代码。

最后印证两类真实 key：

[packages.rs:367-367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L367)：包下载以 `spec`（`&PackageSpec`）作为 key。

[packages.rs:427-427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L427)：包索引下载以 `&"package index"` 作为 key。

#### 4.2.4 代码实践（源码阅读型）

阅读 [download.rs:22-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L22-L31)。假设你想让“包索引下载也显示一个进度条”，应该怎样修改这个闭包？（提示：把 `"package index"` 也 `downcast` 出来并给一个名字即可。）

- 预期结论：只需在闭包里增加一个 `else if let Some(&s @ "package index") = key.downcast_ref::<&str>()` 分支，返回 `Some(...)`。无需改动 typst-kit 的任何代码——这就是 key 机制带来的可扩展性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ProgressReporter` 的三个方法都用 `&Progress`（不可变引用）而不是 `&mut Progress`？

**参考答案**：因为 `Progress` 的状态（`downloaded`、`samples` 等）由 `ProgressReader` 内部维护并更新，reporter 只是“观察者”，没有修改它的需求与权限。把数据以只读引用暴露，既保证了 reporter 无法破坏下载统计，也让 trait 更简单。

**练习 2**：如果一个下载快到从未跨越一个 `period`，`update` 会被调用吗？最终用户还能看到进度吗？

**参考答案**：`update` 不会被调用，但 `start` 会调一次 `update`（见 `PrintProgress::start` 末尾的 `self.update(progress)`），`finish` 也会转发到 `update`。所以用户至少会看到一次（往往是 100% 的）进度行。

---

### 4.3 ProgressReader：分块读取与周期采样

#### 4.3.1 概念说明

`ProgressReader` 是本讲“干活”的类型，但它是**私有**的（`struct ProgressReader<'p>`，[downloader.rs:388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L388)，没有 `pub`）。它把内层 reader 包起来，**自己驱动读取循环**：每次读一块（8192 字节），累加字节数；当距离上次上报的时间超过 `period` 时，就把“这一周期内下载的字节数”作为一个采样压入窗口，并回调一次 `reporter.update`。

源码顶部有一句致谢注释：这个实现参考了 rustup 的 `DownloadTracker`。

[downloader.rs:379-381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L379-L381)：注明设计灵感来自 rustup。了解这一点有助于读者去对照参考实现。

#### 4.3.2 核心流程

```text
ProgressReader::new(len, reader, period, reporter)
  ↓
download():
  buf = 8192 字节缓冲区
  data = 按 len 预分配（无 len 则预分配 8192）
  reporter.start(&state)
  downloaded_this_period = 0
  loop:
    n = reader.read(&mut buf)          // 读一块；Ok(0) 结束；Interrupted 重试
    data.extend(&buf[..n])
    last_printed = 首次读取时初始化为 now   // 关键：从“第一次读到数据”才开始计时
    elapsed = now - last_printed
    downloaded_this_period += n
    state.downloaded += n
    if elapsed >= period:
        若 samples 已满 25 → 弹出最旧（pop_back）
        samples.push_front(downloaded_this_period)   // 最新在前
        downloaded_this_period = 0
        reporter.update(&state)
        last_printed = now
  reporter.finish(&state)
  return Ok(data)
```

采样窗口 `SAMPLES = 25`（[downloader.rs:384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L384)）。它是一个固定容量的滑动窗口：`samples` 用 `VecDeque`，满了就 `pop_back()` 丢最旧、`push_front()` 加最新。窗口里每个元素是“一个 period 内下载的字节数”。下游用它算平均速率（见 4.4）。

有两个细节值得记住：

1. **计时从“第一次读到数据”开始**（不是从构造开始）。这样连接建立、TLS 握手等耗时不会算进第一个采样，避免出现一个“看似很慢”的假采样。
2. **`Interrupted` 错误会被重试**（`continue`），因为它是“临时未就绪、稍后再试”的可恢复错误。

#### 4.3.3 源码精读

[downloader.rs:388-397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L388-L397)：`ProgressReader` 字段——`reader`（内层流）、`state: Progress`（下载统计）、`last_progress: Option<Instant>`（上次上报时刻，初始 `None`）、`progress: &mut dyn ProgressReporter`（上报回调）。注意生命周期 `'p` 绑定 reporter 借用。

[downloader.rs:404-422](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L404-L422)：`new`。`samples` 用 `VecDeque::with_capacity(SAMPLES)` 预分配；`start_time` 在此刻取 `Instant::now()`。`last_progress` 初始为 `None`，标记“尚未开始计时”。

[downloader.rs:426-431](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L426-L431)：`download` 的缓冲区准备。`buf` 固定 8192；`data` 按 `content_len` 预分配以减少扩容拷贝（这正是 u5-l1 里 `download` 默认实现用 hint 预分配思想的延续）。

[downloader.rs:436-445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L436-L445)：读取循环的核心。`Ok(0)` 表示流结束 → `break`；`Interrupted` → `continue` 重试；其余错误直接 `return Err`。

[downloader.rs:449-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L449-L457)：“首次读取才初始化 `last_progress`”的逻辑。当 `last_progress` 为 `None` 时，用当前时刻填充它，这样 `elapsed` 从读到第一块数据后才开始累计。

[downloader.rs:462-472](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L462-L472)：周期到达时的采样与上报。窗口满 25 先 `pop_back`，再 `push_front(downloaded_this_period)`，清零计数，调 `reporter.update`，并把 `last_progress` 刷新为现在。

[downloader.rs:475-476](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L475-L476)：循环结束后调 `reporter.finish`，返回完整 `data`。

#### 4.3.4 代码实践（源码阅读型）

带着下面的假设“单步执行”一遍 `download` 循环：`period = 100ms`，reader 每次 `read` 返回 8192 字节且每次耗时 30ms。问：读完一块后 `elapsed` 是多少？读几块之后会触发第一次 `update`？`samples` 第一次被填充时它的值是多少？

- 预期推演：第一块读完后 `last_progress` 刚被初始化，`elapsed ≈ 0`；之后每块 +30ms。到第 4 块时 `elapsed ≈ 90ms`（仍 < 100ms），到第 4 块末或第 5 块时 `elapsed ≥ 100ms`，触发首次 `update`。此时 `downloaded_this_period` 累计了约 4 块 ≈ 32768 字节，于是 `samples = [32768]`。
- 说明：这是基于假想节奏的推演，具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `samples` 是 `VecDeque` 且“最新在前（push_front）、最旧在后（pop_back）”？

**参考答案**：固定容量滑动窗口需要“两端高效操作”——头部插入、尾部淘汰，`VecDeque` 的 `push_front`/`pop_back` 都是 O(1)。虽然求和与顺序无关，但保留“最新 N 个周期”的语义需要一个可两端操作的队列。“最新在前”只是约定，方便读者理解时间方向。

**练习 2**：如果某次 `reader.read` 返回 `Err(Interrupted)`，已经累加的 `downloaded_this_period` 会丢失吗？

**参考答案**：不会。`continue` 直接回到循环顶部重新 `read`，没有重置 `downloaded_this_period`；而且 `Interrupted` 通常在“没读到任何数据”时发生（`read` 返回 `Err` 不会推进 `data`/计数）。所以计数是连续的。

---

### 4.4 Progress：速率、ETA 与格式化

#### 4.4.1 概念说明

`Progress` 是一个纯数据结构，装着“当前下载的全部统计”。它本身不做计算，只是被 `ProgressReader` 不断更新；真正把它“变成人类可读的一行文字”的是它的 `Display` 实现。

`Display` 要回答三个问题：

1. **速率（bytes/s）**：用滑动采样窗口算“平均每个周期下载多少字节”，再换算成每秒。
2. **预计剩余时间（ETA）**：在已知总大小 `content_len` 时，用剩余字节除以速率。
3. **单位换算**：把字节数格式化成 B / KiB / MiB / GiB。

当响应头没有 `Content-Length`（即 `content_len = None`）时，没有百分比、没有 ETA，只能显示“已下载 X，速率 Y/s”。

#### 4.4.2 核心流程与数学

**速率计算**。设采样窗口里有 \(n\) 个采样（\(n \leq 25\)），每个采样是一个 `period` 内的字节数，窗口总和为 \(S\)：

\[
\bar{b}_{period} = \frac{S}{n} \quad (\text{平均每个周期的字节数})
\]

一个周期长度为 `period`，则“每秒有多少个周期”（频率）为：

\[
f = \frac{1\,\text{s}}{\text{period}}
\]

于是每秒字节数：

\[
\text{bytes/s} = \bar{b}_{period} \cdot f
\]

源码里 `f` 用纳秒计算：`1s 的纳秒数 / period 的纳秒数`（见下方精读）。例如 `period = 100ms` 时 \(f = 10\)。

> 边界：当 `samples` 为空（\(n=0\)）时，`sum/len` 会除零，源码用 `.checked_div(len).or(self.content_len).unwrap_or(0)` 兜底——先用 `content_len` 顶上，再不行就返回 0。

**ETA 计算**（仅 `content_len = Some` 时）。设已下载 \(d\)、总大小 \(L\)、平均每周期字节数 \(\bar{b}_{period}\)：

\[
b_{rem} = L - d, \qquad
k_{rem} = \left\lfloor \frac{b_{rem}}{\bar{b}_{period}} \right\rfloor, \qquad
\text{ETA} = \text{period} \cdot k_{rem}
\]

即“按当前速率，还剩多少个周期”，再换算回时间。

**单位换算**（`format_byte_unit`）。阈值用二进制单位（\(1024\)）：

| 字节数范围 | 显示 |
| --- | --- |
| `>= Gi` (\(1024^3\)) | `XXX.X GiB` |
| `>= Mi` (\(1024^2\)) | `XXX.X MiB` |
| `>= Ki` (\(1024\)) | `XXX.X KiB` |
| 否则 | `XXX B` |

注意是 **KiB/MiB/GiB**（1024 进制），不是 KB/MB/GB（1000 进制）。

#### 4.4.3 源码精读

先看 `Progress` 结构体的字段：

[downloader.rs:292-305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L292-L305)：`start_time`、`content_len`、`downloaded`、`samples: VecDeque<usize>`、`period`。所有字段都是 `pub`——reporter 可以自由读取它们做自定义显示。

接下来是 `Display` 的速率计算：

[downloader.rs:310-319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L310-L319)：`bytes_per_period = sum / len`（带 `content_len` 与 `0` 两级兜底）；`frequency = 1s 的纳秒 / period 的纳秒`（用 `checked_div` + `try_into` 安全转 `usize`，失败退化为 1）；`bytes_per_sec = bytes_per_period * frequency`。这两行就是上面公式 \(\bar{b}_{period}\) 与 \(\text{bytes/s}\) 的直接对应。

然后是“有总大小”分支（百分比 + ETA）：

[downloader.rs:321-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L321-L341)：算 `ratio`、`remaining_bytes`、`remaining_buckets`（即 \(k_{rem}\)，带 `try_into` 防溢出）、`eta = period * remaining_buckets`，最后格式化为 `downloaded / total (xx %), xxx/s, ETA: xx s`。

以及“无总大小”分支：

[downloader.rs:343-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L343-L348)：只输出 `downloaded, xxx/s`。

辅助函数：

[downloader.rs:354-372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L354-L372)：`format_byte_unit`。用常量 `KI=1024`、`MI=KI*KI`、`GI=KI*KI*KI` 分级，借助 `typst_utils::display` 把闭包包装成一个 `impl Display`（延迟格式化）。各档用 `{:5.1}` 固定宽度，便于终端对齐刷新。

[downloader.rs:375-377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L375-L377)：`format_seconds` 只保留秒精度（`duration.as_secs()`），所以 ETA 不会显示“xx 分 yy 秒”，而是直接 `xx s`。

#### 4.4.4 代码实践（源码阅读型）

构造一个想象的 `Progress`：`content_len = Some(1_000_000)`、`downloaded = 250_000`、`samples = [10000, 10000]`、`period = 100ms`。手算它的 `Display` 输出。

- `bytes_per_period = 20000 / 2 = 10000`
- `frequency = 1_000_000_000 / 100_000_000 = 10`
- `bytes_per_sec = 10000 * 10 = 100000` → `format_byte_unit(100000)` = ` 97.7 KiB`
- `ratio = 0.25` → 百分比 `25`
- `remaining = 750000`；`remaining_buckets = 750000 / 10000 = 75`；`eta = 100ms * 75 = 7500ms = 7s`
- 预期输出大致：`244.1 KiB / 976.6 KiB ( 25 %),  97.7 KiB/s, ETA: 7 s`
- 说明：上述为手算推演，实际数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ETA 用 `remaining_buckets`（基于平均速率）而不是用 `start_time` 至今的整体平均速率？

**参考答案**：用最近 25 个周期的滑动窗口算速率，能反映**近期**的真实下载速度（网络波动时更灵敏）；而“自开始以来的整体平均”会被早期的慢启动拖累，估算出的 ETA 不准。滑动窗口是速率平滑的常用手法。

**练习 2**：`format_byte_unit` 用 1024 进制，`frequency` 用纳秒计算——这两处都和“单位”有关。如果把 `period` 从 100ms 改成 50ms，`bytes_per_sec` 会怎样变化？

**参考答案**：`bytes_per_sec = bytes_per_period * frequency`，而 `frequency` 与 `period` 成反比（`period` 减半则 `frequency` 翻倍）。但 `period` 减半也意味着每个采样覆盖的时间减半，每个周期内下载的字节数也大致减半，于是 `bytes_per_period` 也减半。两者相乘，`bytes_per_sec` 基本不变——也就是说，刷新周期不影响最终显示的速率，只影响刷新频率。

---

## 5. 综合实践

本实践把四个最小模块串起来：自己写一个 `Downloader`（返回固定字节，但故意读得慢以便观察 `update`）、自己写一个 `ProgressReporter`（在 `start/update/finish` 打印 `Progress`），用 `ProgressDownloader::with_interval` 包起来下载，观察进度回调、速率与 ETA 的输出。

> 下面的代码是**示例代码**（非项目原有代码），需要放在一个依赖了 `typst-kit` 的新 Cargo 项目里。它不使用 `SystemDownloader`，因此**不需要开启任何 feature**（`downloader` 模块本身无门禁，见第 3 节）。

### 示例代码

```rust
// Cargo.toml 需要：
//   typst-kit = { path = "..." }   # 无需 features
use std::any::Any;
use std::io::{self, Cursor, Read};
use std::time::Duration;
use typst_kit::downloader::{Downloader, Progress, ProgressDownloader, ProgressReporter};

// 1) 一个“返回固定字节 + 故意读得慢”的下载器。
//    它不真正联网，只是把内存里的字节假装成网络流。
struct FakeDownloader(Vec<u8>);

impl Downloader for FakeDownloader {
    fn stream(
        &self,
        _key: &dyn Any,
        _url: &str,
    ) -> io::Result<(Option<usize>, Box<dyn Read>)> {
        // hint 设为总长度，Progress 才能算出百分比与 ETA。
        Ok((Some(self.0.len()), Box::new(SlowReader(Cursor::new(self.0.clone())))))
    }
}

// 每次读取睡一小会儿，保证会跨越多个 period，从而触发 update。
struct SlowReader(Cursor<Vec<u8>>);
impl Read for SlowReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        std::thread::sleep(Duration::from_millis(3));
        self.0.read(buf)
    }
}

// 2) 一个最小的 ProgressReporter：三个回调都打印一行。
struct PrintReporter;
impl ProgressReporter for PrintReporter {
    fn start(&mut self, p: &Progress) {
        println!("[start ] {p}");
    }
    fn update(&mut self, p: &Progress) {
        println!("[update] {p}");
    }
    fn finish(&mut self, p: &Progress) {
        println!("[finish] {p}");
    }
}

fn main() {
    let data = vec![0u8; 200_000]; // 约 195 KiB
    let inner = FakeDownloader(data);

    // period 调小到 10ms，配合每次 3ms 的慢读取，update 会被触发多次。
    let dl = ProgressDownloader::with_interval(
        inner,
        |_key: &dyn Any| PrintReporter, // 本练习里 reporter 无视 key，总是打印
        Duration::from_millis(10),
    );

    let bytes = dl.download(&"demo", "fake://localhost/blob").expect("download failed");
    println!("downloaded {} bytes", bytes.len());
}
```

### 操作步骤

1. 新建一个 Cargo 项目，在 `Cargo.toml` 里依赖 `typst-kit`（可用 path 指向本仓库的 `crates/typst-kit`，**无需 features**）。
2. 把上面的示例代码放进 `src/main.rs`。
3. `cargo run`。

### 需要观察的现象

- 终端应打印若干行：一行 `[start ] ...`，多行 `[update] ...`（数量取决于慢读取实际跨越了多少个 10ms 周期），最后一行 `[finish] ...`。
- 每行的内容是 `Progress` 的 `Display` 输出：形如 `xxx KiB / 195.3 KiB ( yy %),  xx KiB/s, ETA: z s`。
- 随着 `downloaded` 增长，百分比应从低到高逼近 100%，ETA 应逐步减小。

### 预期结果

- 你会清楚看到 `start → update(多次) → finish` 的回调时序，印证 4.2 的契约。
- 你会看到 `bytes/s` 与 `ETA` 随窗口滑动而变化，印证 4.3 的采样与 4.4 的计算。
- 把 `with_interval` 的 `period` 调大（例如 500ms），应观察到 `update` 行数明显减少；把 `SlowReader` 的 sleep 去掉，可能观察到 `update` 一次都不触发（下载太快），只剩 `start` 与 `finish`——这印证了 4.2 中“快下载可能没有 update”的结论。

> 说明：由于输出依赖本机读取速度与调度，具体的字节数、百分比与 ETA 数值无法精确预测，**待本地验证**；但上述“时序与趋势”是确定的。

## 6. 本讲小结

- `ProgressDownloader` 是一个**装饰器**：实现同一个 `Downloader` trait，内部包裹真正的下载器，自己不碰网络。
- **`key` 把“是否显示进度”的决定权交给上层**：工厂闭包 `(self.progress)(key)` 每次 download 时生产一个 reporter，上层用 `downcast` 区分 `PackageSpec`（显示）、`"release"`（显示）、`"package index"`（不显示）。
- `download()` 与 `stream()` **不对称**：只有 `download()` 经 `ProgressReader` 驱动读取并上报进度；`stream()` 退化为全量缓冲、不上报。
- `ProgressReader` 用 8192 字节的分块读取循环，**按固定周期 `period` 采样**：把“本周期下载字节数”压入 `SAMPLES = 25` 的滑动窗口，并回调 `update`；`start`/`finish` 必调，`update` 可能 0 次。
- `Progress::Display` 用滑动窗口算平均“每周期字节数”，乘以频率得 bytes/s；有 `content_len` 时还能算出百分比与 ETA；`format_byte_unit` 用 1024 进制的 KiB/MiB/GiB。
- 整套机制让**任何 `Downloader` 都能无侵入地获得进度条**，而 typst-kit 自身对“如何显示”一无所知。

## 7. 下一步学习建议

- 本讲是「网络下载子系统」的第二讲、也是最后一讲。回到更上层，建议阅读 `crates/typst-cli/src/download.rs` 全文，对照本讲的 `PrintProgress`，理解 CLI 如何把 `SystemDownloader + ProgressDownloader` 组装成最终下载器。
- 顺着我向下游追：本讲提到的 `key`（`PackageSpec` / `"package index"`）来自 u4-l3 的 `UniversePackages`。可重读 `packages.rs` 的 `package()` 与 `index()`，看它们如何把下载结果进一步解包（`flate2` + `tar`）。
- 若对“驱动读取循环”的写法感兴趣，可比照本讲致谢的 rustup `DownloadTracker`，体会 typst-kit 在其基础上的简化。
- 下一单元（u6）将离开网络主题，转向「终端诊断输出」——同样是“把内部数据结构适配成终端友好显示”的课题，与本讲的 `Progress::Display` 有异曲同工之处。
