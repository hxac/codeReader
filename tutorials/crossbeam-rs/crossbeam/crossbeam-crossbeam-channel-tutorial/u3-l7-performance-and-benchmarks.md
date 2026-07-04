# 性能与基准测试

## 1. 本讲目标

前面几讲我们已经把 crossbeam-channel 的「公共类型壳 + 六种 flavor + select 内核」从源码层面读了一遍。本讲换一个视角——**性能**：项目自己用什么手段来度量通道的吞吐与延迟，又如何用度量结果来佐证「无锁 / 少锁 + `CachePadded`」这套设计的价值。

crossbeam-channel 仓库里其实有**两套互不相同的基准测试**，本讲会把它们都讲清楚：

- [`benchmarks/`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/) —— 一个独立的小 crate，把 crossbeam-channel 与 Go、`std::sync::mpsc`、`flume`、`futures-channel` 等**其它实现横向对比**，用 `run.sh` 串起来，最后用 `plot.py` 画成 `plot.png`。
- [`benches/crossbeam.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs) —— 一个只测 crossbeam-channel 自身的**微基准**，覆盖 `unbounded / bounded_n / bounded_1 / bounded_0` 四类容量下的 `spsc / mpsc / mpmc` 等拓扑。

读完本讲，你应当能够：

1. 说清楚两套基准各自的目的、运行方式与产出格式，不再把它们混为一谈。
2. 复述 `benchmarks/` 里六种标准场景（`seq / spsc / mpsc / mpmc / select_rx / select_both`）的含义，并看懂一个二进制（`crossbeam-channel.rs`）如何通过 `Option<usize>` 容量参数化出 `bounded0 / bounded1 / bounded / unbounded` 四组柱子。
3. 读懂 `benches/crossbeam.rs` 用「`bounded(0)` 当发令枪与终点线」隔离被测代码的模式，并知道它走的是 **nightly 内置 `test::Bencher`**（不是 criterion）。
4. 把基准结果关联回设计：解释 `CachePadded` 防伪共享、array 的环形无锁、list 的「发送方永不阻塞」如何各自转化为性能数字。

本讲依赖 [u2-l5（array flavor）](u2-l5-array-flavor.md)，因为性能解读要反复回到 array 的 `CachePadded<AtomicUsize>` 头尾游标；也会顺带引用 [u2-l6（list flavor）](u2-l6-list-flavor.md) 与 [u2-l7（zero flavor）](u2-l7-zero-flavor.md) 的结论。

> ⚠️ 一个需要先纠正的术语：本讲的某些二手描述把 `benches/crossbeam.rs` 称为「criterion 基准」。**这与源码不符**——该文件第 1–3 行是 `#![feature(test)]` / `extern crate test` / `use test::Bencher`，且 [`Cargo.toml` 的 `[dev-dependencies]`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml) 里只有 `fastrand / rustversion / signal-hook`，**没有任何 criterion 依赖**。所以它是 Rust **nightly 专属的内置 `#[bench]` 微基准**，必须用 `cargo +nightly bench` 运行。本讲后续一律按真实机制讲解。

---

## 2. 前置知识

### 2.1 通道拓扑：spsc / mpsc / mpmc

评估一个并发队列时，「几个线程生产、几个线程消费」会得到完全不同的数字，因此业界约定了几种标准拓扑（topology）：

| 缩写 | 全称 | 含义 |
|------|------|------|
| **seq** | sequential | 单线程，先发后收（无并发，反映单核开销） |
| **spsc** | single-producer single-consumer | 1 个发送线程 + 1 个接收线程 |
| **mpsc** | multi-producer single-consumer | 多个发送线程 + 1 个接收线程（`std::sync::mpsc` 的经典模型） |
| **mpmc** | multi-producer multi-consumer | 多发送 + 多接收，crossbeam-channel 的看家本领 |

> crossbeam-channel 与标准库 `mpsc` 的核心区别正是：它的 `Receiver` 可以 `clone`，所以原生支持 mpmc（见 [u1-l4](u1-l4-clone-sharing-disconnect.md)）；而 `std::sync::mpsc` 只支持 mpsc。

### 2.2 容量（cap）与 flavor 的对应

回顾 [u2-l1](u2-l1-architecture-and-flavors.md)：构造函数按容量分流到不同 flavor——`unbounded`→list、`bounded(cap>0)`→array、`bounded(0)`→zero。基准测试会用同一套场景跑不同容量，观察「缓冲大小」对吞吐的影响，这正是后面 `crossbeam-channel.rs` 用 `Option<usize>` 参数化的原因。

### 2.3 伪共享（false sharing）与 `CachePadded`

现代 CPU 按缓存行（cache line，通常 64 字节）加载内存。如果**两个线程频繁写的字段恰好在同一根缓存行里**，哪怕逻辑上互不相关，硬件也会不断在两个核之间来回「作废 / 同步」这行缓存，导致性能急剧下降——这叫**伪共享**。

`crossbeam_utils::CachePadded<T>` 的做法是用填充字节把 `T` 撑满到缓存行大小的整数倍并按缓存行对齐，保证「每个热点字段独占自己的缓存行」。在 array flavor 里，生产者狂写 `tail`、消费者狂写 `head`，两者若贴在一起就是伪共享重灾区，所以都被 `CachePadded` 包裹（详见 4.4）。

### 2.4 基准测试的两个黄金法则

- **隔离被测代码**：只测「发消息 + 收消息」本身，把线程创建、启动同步、收尾等待排除在计时之外。`benches/crossbeam.rs` 用一对 `bounded(0)` 通道当「发令枪 + 终点线」实现这点（见 4.3）。
- **让线程跑够久**：单次 send/recv 太短，会被计时器噪声淹没。所以基准都跑 `N` 条（这里默认 5,000,000）取总耗时，或用 `#[bench]` 让框架自动反复迭代。

---

## 3. 本讲源码地图

| 文件 | 归属 | 作用 |
|------|------|------|
| [`benchmarks/README.md`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/README.md) | 对比套件 | 定义六种场景、默认参数、运行方式、历史结果环境 |
| [`benchmarks/Cargo.toml`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/Cargo.toml) | 对比套件 | 声明各竞品依赖与若干 `[[bin]]`，每个实现一个二进制 |
| [`benchmarks/run.sh`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh) | 对比套件 | 顺序跑 5 个默认竞品，输出 `*.txt`，再调 `plot.py` 出图 |
| [`benchmarks/crossbeam-channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs) | 对比套件 | crossbeam-channel 自身在六场景下的实现，按容量参数化 |
| [`benchmarks/message.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/message.rs) | 对比套件 | 被测消息类型 `Message([usize; 1])`，刻意做成 1 字（8 字节） |
| [`benchmarks/plot.py`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/plot.py) | 对比套件 | 解析 `*.txt`、按容量分组画 4 子图 `plot.png` |
| [`benches/crossbeam.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs) | 微基准 | nightly 内置 `#[bench]`，测自身各类拓扑 |
| [`src/flavors/array.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 设计回溯 | `CachePadded` 包裹 `head/tail` 的源头 |

补充：

- `benchmarks/` 是一个**独立的 Cargo 包**（`name = "benchmarks"`、`publish = false`，见 [`benchmarks/Cargo.toml`:1-5](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/Cargo.toml)），用相对路径同时依赖父 crate `crossbeam-channel` 与祖父 crate `crossbeam`（提供 `crossbeam::scope` 线程作用域）。它**不参与** crossbeam-channel 自身的构建。
- `benches/` 则是 crossbeam-channel 自己的 `Cargo.toml` 自动发现的基准目录（无显式 `[[bench]]` 声明，也没有 `harness = false`），按 Cargo 默认基准 harness 处理。

---

## 4. 核心概念与源码讲解

### 4.1 两套基准测试：定位与目录组织

#### 4.1.1 概念说明

为什么要分两套？因为它们回答的问题不同：

- **对比套件 `benchmarks/`** 回答：「crossbeam-channel 相对其它通道实现（Go channel、`std::sync::mpsc`、`flume`、`futures-channel` 等）是快是慢？」这是**跨实现**的横向比较，关心的是**绝对吞吐**（每秒多少消息、或跑完 N 条用几秒）。为了让比较公平，每个实现各写一个二进制，**共用同一套场景定义和同一份消息类型**。
- **微基准 `benches/crossbeam.rs`** 回答：「crossbeam-channel 自身在不同容量、不同拓扑下的开销结构如何？」它**只测自己**，关心的是**每次操作的纳秒级成本**与**随容量/并发度的变化曲线**。

#### 4.1.2 核心流程

两套基准的生命周期大致是：

```text
对比套件 benchmarks/:
  run.sh 逐个 cargo run --release --bin <impl>
    → 每个 bin 内部跑六场景×多容量
    → 每个场景打印一行 "test lang impl secs"
    → tee 到 <impl>.txt
  plot.py 读所有 *.txt → 按 cap 分组 → plot.png（4 子图）

微基准 benches/crossbeam.rs:
  cargo +nightly bench
    → #[bench] 框架对每个 bench 函数反复调用 b.iter(...)
    → 自动统计每次迭代平均耗时（ns/iter）
```

#### 4.1.3 源码精读

先看对比套件如何把「多个竞品」组织成一个包。[`benchmarks/Cargo.toml` 的依赖段](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/Cargo.toml)把所有竞品都拉进来：

- [benchmarks/Cargo.toml:7-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/Cargo.toml#L7-L16) —— 同时依赖 `crossbeam`、`crossbeam-channel`、`crossbeam-deque`、`flume`、`futures`、`bus`、`mpmc`、`lockfree`、`atomicring`，把九个实现拉到同一编译单元里。

每个实现对应一个二进制，用 `[[bin]]` 声明并 `doc = false` 隐藏文档，例如 crossbeam-channel 这一格：

- [benchmarks/Cargo.toml:33-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/Cargo.toml#L33-L36) —— `[[bin]] name = "crossbeam-channel" path = "crossbeam-channel.rs"`，把 `crossbeam-channel.rs` 注册为同名二进制（这就是 `run.sh` 里 `--bin crossbeam-channel` 的来源）。

而微基准这边没有任何额外声明，直接靠 Cargo 默认从 `benches/` 自动发现 `crossbeam.rs`：

- [benches/crossbeam.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L1-L3) —— `#![feature(test)]` + `extern crate test;` 标明它依赖 nightly 的不稳定 `test` 框架；这也意味着**用稳定版 `cargo bench` 编译它会直接报错**，必须 `cargo +nightly bench`。

> 小结：两个目录虽都叫「基准」，但 `benchmarks/` 是**多实现对比程序集**（普通 `cargo run --release`，需 Rust+Go+Python 环境），`benches/` 是**单实现微基准**（nightly `#[bench]`）。务必先分清，避免用错命令。

#### 4.1.4 代码实践

1. **目标**：用目录和清单证据把两套基准区分开。
2. **步骤**：
   - 在仓库根执行 `cat benchmarks/Cargo.toml | head -20`，确认它是个独立包且依赖多个竞品。
   - 执行 `grep -n criterion Cargo.toml`（crossbeam-channel 自己的 Cargo.toml），确认**无 criterion**。
   - 执行 `head -3 benches/crossbeam.rs`，确认 `#![feature(test)]`。
3. **观察**：`Cargo.toml` 的 `[dev-dependencies]` 里没有 `criterion`；`benches/crossbeam.rs` 顶部有 `#![feature(test)]`。
4. **预期结果**：三条证据共同证明 `benches/` 走 nightly 内置 harness，`benchmarks/` 走独立二进制对比——两者井水不犯河水。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `benchmarks/Cargo.toml` 里要同时依赖 `crossbeam`（祖父 crate）和 `crossbeam-channel`（父 crate）？

> **答案**：`crossbeam-channel` 提供 `Sender/Receiver/select` 等通道本身；而 `crossbeam`（workspace 根 crate）提供 `crossbeam::scope` 作用域线程，用来在 `mpsc/mpmc` 等场景里安全地 spawn 工作线程并 join。二者职责不同，缺一不可（见后续 4.2 里大量 `crossbeam::scope(|scope| { scope.spawn(...) })` 调用）。

**练习 2**：如果不装 nightly 工具链，`benches/` 还能跑吗？`benchmarks/` 呢？

> **答案**：`benches/` 不能——`#![feature(test)]` 只在 nightly 可用。`benchmarks/` 不受影响，它用的是稳定版 `std::time::Instant` 计时（见 [benchmarks/crossbeam-channel.rs:147-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L147-L160) 的 `run!` 宏），稳定工具链即可，但仍需 Go 工具链来跑 `go.go`。

---

### 4.2 六种对比场景与 run.sh 调度

#### 4.2.1 概念说明

对比套件的核心是一份**场景契约**：所有实现都按同一份场景定义跑，结果才可比。这份契约写在 [`benchmarks/README.md`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/README.md) 里，共六种场景：

- `seq`、`spsc`、`mpsc`、`mpmc`：四种基础拓扑（见 2.1）。
- `select_rx`：`T` 个线程各往**独立通道**发 `N/T` 条；一个接收线程用 `Select` 在这 `T` 个通道上选，收满 `N` 条。
- `select_both`：`T` 个发送线程用 `Select` 选 `T` 个通道发，`T` 个接收线程用 `Select` 选 `T` 个通道收——最重的并发 select 场景。

默认参数 `N = 5_000_000`、`T = 4`（[benchmarks/README.md:12-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/README.md#L12-L16)）。注意 `std::sync::mpsc`、`flume` 等并不支持全部六种场景（比如 `flume` 的 `main` 只跑了 `seq/spsc/mpsc`，见 [benchmarks/flume.rs:153-165](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/flume.rs#L153-L165)），所以对比图里它们的柱子会缺几根——这是正常的，因为它们没有 mpmc/select 能力。

#### 4.2.2 核心流程

`run.sh` 的调度很朴素：**顺序**跑每个竞品二进制，把标准输出原样存成 `<impl>.txt`，最后让 `plot.py` 读所有 `*.txt` 出图。

```text
cargo run --release --bin crossbeam-channel | tee crossbeam-channel.txt
cargo run --release --bin futures-channel   | tee futures-channel.txt
cargo run --release --bin mpsc               | tee mpsc.txt
cargo run --release --bin flume              | tee flume.txt
go run go.go                                 | tee go.txt
./plot.py ./*.txt   → plot.png
```

之所以**顺序**而不是并行跑，是为了让每个实现独占 CPU、避免互相干扰污染数字。代价是总耗时长，但公平性优先。

#### 4.2.3 源码精读

**调度入口** [`benchmarks/run.sh`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh)：

- [benchmarks/run.sh:1-4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L1-L4) —— `set -euxo pipefail` + `IFS=$'\n\t'` 是 bash 严格模式（遇错即停、用未定义变量即停、管道失败即停），`cd "$(dirname "$0")` 保证脚本无论从哪调用都在 `benchmarks/` 内执行，相对路径才正确。
- [benchmarks/run.sh:6-10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L6-L10) —— 默认只跑 5 个「测试最多」的竞品，注释说明原因：太多柱子在图里会重叠（[run.sh:12-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L12-L14)）。
- [benchmarks/run.sh:16-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L16-L22) —— 被注释掉的 `atomicringqueue / atomicring / bus / crossbeam-deque / lockfree / segqueue / mpmc` 等额外竞品，可手动启用。
- [benchmarks/run.sh:24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L24) —— `./plot.py ./*.txt` 把所有结果文件喂给绘图脚本。

**每个 bin 必须打印的输出契约**：`plot.py` 用 `line.split()` 按「空白分五列」解析——`test lang impl secs _`（[benchmarks/plot.py:7-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/plot.py#L7-L15)），再把 test 名按下划线切成 `前缀（容量组）_ 场景`。这正好对应 `crossbeam-channel.rs` 里 `run!` 宏打印的格式：

- [benchmarks/crossbeam-channel.rs:147-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L147-L160) —— `run!` 宏用 `{:25} {:15} {:7.3} sec` 三列打印「测试名 / `Rust crossbeam-channel` / 耗时秒数」。`plot.py` 把第二列 `Rust` 当 `lang`、`crossbeam-channel` 当 `impl`、第三列的数字当 `secs`。

**绘图分组** [benchmarks/plot.py:117-144](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/plot.py#L117-L144)：把所有测试按容量前缀 `bounded0 / bounded1 / bounded / unbounded` 分成 4 组，每组画一个横向条形子图（`plot()` 在 [plot.py:86-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/plot.py#L86-L114)），最终拼成 `plot.png`。

> 注意 `plot.py:108` 写的是 `max(max(scores.values(), key=lambda x: max(x)))`——意图是取所有得分最大值当 x 轴上限，写法略绕（外层 `max` 取的是「最大值最大的那行」而非纯标量），属于历史代码的小瑕疵，但不影响出图。

**六场景的真实实现** [`benchmarks/crossbeam-channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs)。这里最巧妙的设计是**用一个函数参数 `cap: Option<usize>` 让同一个 `seq/spsc/mpsc/...` 函数跑出四种容量**：

- [benchmarks/crossbeam-channel.rs:8-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L8-L13) —— `new<T>(cap)`：`None`→`unbounded()`（list flavor），`Some(cap)`→`bounded(cap)`（按 cap 进入 array 或 zero flavor）。这一行就是把「容量选择」和「flavor 分流」缝合在一起的关键，呼应 [u2-l1](u2-l1-architecture-and-flavors.md) 的分流结论。

四种场景函数本身都很短，以 `spsc` 为例：

- [benchmarks/crossbeam-channel.rs:27-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L27-L42) —— `spsc`：在 `crossbeam::scope` 里 spawn 一个线程发 `MESSAGES` 条，主线程同时收 `MESSAGES` 条；`scope` 结束自动 join，计时由外层 `run!` 宏包裹。

`mpmc`、`select_rx`、`select_both` 同理：

- [benchmarks/crossbeam-channel.rs:63-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L63-L84) —— `mpmc`：`THREADS` 个发送线程 + `THREADS` 个接收线程同时跑。
- [benchmarks/crossbeam-channel.rs:86-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L86-L110) —— `select_rx`：每轮新建 `Select`，`sel.recv(rx)` 注册 `THREADS` 个通道，`sel.select()` 选一个就绪的，`case.recv(...)` 完成（[u2-l10](u2-l10-select-dynamic-api.md) 讲过「必须完成 `SelectedOperation`」）。
- [benchmarks/crossbeam-channel.rs:112-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L112-L145) —— `select_both`：收发两边都在 `T` 个通道上 `Select`，是最考验 select 调度公平性与锁竞争的场景。

最后 `main` 用 `run!` 宏把「场景×容量」的笛卡尔积全跑一遍：

- [benchmarks/crossbeam-channel.rs:162-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L162-L187) —— 依次 `run!("bounded0_*", f(Some(0)))`、`bounded1_*` (`Some(1)`)、`bounded_*` (`Some(MESSAGES)`)、`unbounded_*` (`None`)。注意 zero 容量（`bounded0`）**只跑了 5 种**——因为 `seq`（单线程）在 `bounded(0)` 上会死锁，故缺 `bounded0_seq`。

**被测消息** [`benchmarks/message.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/message.rs)：

- [benchmarks/message.rs:3-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/message.rs#L3-L6) —— `Message([usize; 1])`：刻意做成恰好一个机器字（8 字节），既非 ZST（避免编译器过度优化掉搬运），又足够小让「通道开销」而非「数据拷贝」成为瓶颈。这是基准测试选取消息大小的常见取舍。

#### 4.2.4 代码实践

1. **目标**：单跑 crossbeam-channel 自己的对比程序，亲眼看六场景×四容量的耗时，并理解输出格式。
2. **步骤**：
   ```bash
   cd benchmarks
   cargo run --release --bin crossbeam-channel | tee crossbeam-channel.txt
   ```
   再用 `head -5 crossbeam-channel.txt` 看前几行；试着按 `_` 拆分一个测试名。
3. **观察**：每行形如 `bounded0_mpmc             Rust crossbeam-channel    0.xxx sec`，列与列间用空格分隔；容量越大（`bounded_*` / `unbounded_*`）一般越快，`bounded0_*`（会合）最慢。
4. **预期结果**：你会得到约 21 行结果（`bounded0` 缺 `seq`）。**具体秒数待本地验证**（取决于 CPU、核数、是否争用），但相对趋势应当是：`unbounded_* ≈ bounded_* < bounded1_* < bounded0_*`（缓冲越大吞吐越高），且 `select_*` 普遍比裸 `mpmc` 慢（select 调度有额外开销）。若想出图，需补跑其余竞品并装 Python+matplotlib 后执行 `./plot.py ./*.txt`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bounded0`（容量 0）那一组没有 `seq` 测试，而其余容量组有？

> **答案**：`bounded(0)` 是会合通道（zero flavor，见 [u2-l7](u2-l7-zero-flavor.md)），发送与接收必须同时在场。`seq` 在单线程里先 `send` 后 `recv`，第一个 `send` 永远等不到对端接收 → 死锁。其余容量有缓冲（array/list），单线程可以先塞满再排空，故 `seq` 可跑。

**练习 2**：`run.sh` 为什么把 `flume`、`mpsc` 等也跑进同一张图，明明它们并不支持六种场景？

> **答案**：对比图的价值正在于「同等条件下谁能做什么」。`flume`/`std mpsc` 缺 `mpmc`/`select_*` 的柱子，本身就是信息——它直观告诉读者「这些实现不具备 mpmc / select 能力」。crossbeam-channel 的柱子最全，这本身就是它定位「全能 mpmc」的视觉证据。

---

### 4.3 benches/crossbeam.rs：内置微基准与「发令枪」隔离模式

#### 4.3.1 概念说明

`benches/crossbeam.rs` 不和别人比，只把 crossbeam-channel 自己放到显微镜下。它用 Rust nightly 的内置 `#[bench]`（`test::Bencher`），通过 `b.iter(closure)` 让框架自动反复调用闭包、统计每次迭代平均耗时。基准按容量分成四个 `mod`：

- `mod unbounded`（[crossbeam.rs:19-222](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L19-L222)）
- `mod bounded_n`（[crossbeam.rs:224-405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L224-L405)）
- `mod bounded_1`（[crossbeam.rs:407-567](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L407-L567)）
- `mod bounded_0`（[crossbeam.rs:569-720](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L569-L720)）

每个 mod 里有 `create / oneshot / spsc / spmc / mpsc / mpmc / par_inout` 等函数。其中 `create`/`oneshot`/`inout` 测极短操作（建通道、单发单收），其余测多线程拓扑。

#### 4.3.2 核心流程：用 `bounded(0)` 当「发令枪 + 终点线」

多线程基准最大的坑是「如何只测收发、不测线程启停」。这里的解法极为优雅：**预先 spawn 好一批常驻工作线程，它们阻塞在一个 `bounded(0)` 的 `r1` 上等发令；主线程在 `b.iter` 里通过 `s1.send(())` 发令、用另一个 `bounded(0)` 的 `r2` 等它们全部完工**。

```text
准备阶段（计时外）：
  spawn T 个工作线程，每个循环：
    while r1.recv().is_ok() {       // 阻塞等发令枪
        执行 N/T 次 s.send / r.recv  // 这段才是被测代码
        s2.send(())                  // 冲过终点线
    }

计时阶段（b.iter 闭包内）：
  for _ in 0..T { s1.send(()) }      // 鸣枪：唤醒所有工作线程
  for _ in 0..T { r2.recv() }        // 等所有工作线程报告完工
  → 闭包返回，框架记录「这一轮」耗时
```

发令枪与终点线本身是 `bounded(0)` 会合通道（zero flavor），保证 send 必须等到 recv 在场才返回——天然构成严格同步点，把「唤醒 + 干活 + 汇报」锁死在被测区间内。

#### 4.3.3 源码精读

**框架入口** [benches/crossbeam.rs:1-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L1-L11)：`#![feature(test)]` 开启 nightly，`TOTAL_STEPS = 40_000` 是每个 bench 的总收发步数（比对比套件的 5,000,000 小，因为 `#[bench]` 会自动反复迭代多轮）。

**并发度探测** [benches/crossbeam.rs:13-17](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L13-L17)：`num_cpus()` 用 `std::thread::available_parallelism()` 探测逻辑核数，让 `mpmc`/`mpsc` 的线程数随机器自适应（对比套件则是写死 `THREADS=4`）。

**发令枪模式范例** 以 `unbounded::mpmc` 为例（这是最能体现模式的）：

- [benches/crossbeam.rs:180-221](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L180-L221) ——
  - 预先建被测通道 `(s, r) = unbounded()`；
  - 建发令枪 `(s1, r1)` 与终点线 `(s2, r2)`，都是 `bounded(0)`；
  - spawn `threads/2` 个发送者 + `threads/2` 个接收者，每个都 `while r1.recv().is_ok() { 干活; s2.send(()) }`；
  - `b.iter` 闭包只做「鸣枪 `s1.send(())` × threads」+「等终点 `r2.recv()` × threads」；
  - 计时结束后 `drop(s1)` 让工作线程退出 `while` 循环、随 `scope` 收尾。

**单发基准** 没有「发令枪」需求，直接测极短操作：

- [benches/crossbeam.rs:27-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L27-L34) —— `unbounded::oneshot`：`b.iter` 里每次新建通道、发一条、收一条，测「建+发+收+销毁」一整套的纳秒开销。
- [benches/crossbeam.rs:22-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L22-L25) —— `unbounded::create`：`b.iter(unbounded::<i32>)`，只测建通道本身（list flavor 的初始 `Block` 分配等）。

四个 `mod` 的同拓扑函数结构几乎一样，差别只在「被测通道怎么建」：`unbounded` 用 `unbounded()`、`bounded_n` 用 `bounded(steps*threads)`（大缓冲）、`bounded_1` 用 `bounded(1)`、`bounded_0` 用 `bounded(0)`。横向对照就能看出**容量对每次操作耗时的影响曲线**。

#### 4.3.4 代码实践

1. **目标**：用 nightly 跑微基准，读出 `ns/iter`，比较不同容量/拓扑。
2. **步骤**（需 nightly）：
   ```bash
   rustup toolchain install nightly   # 若尚未安装
   cargo +nightly bench -F std        # 也可只跑某一组：cargo +nightly bench bounded_0
   ```
3. **观察**：每个 `bench` 会打印一行 `test benches::unbounded::mpmc ... bench: N ns/iter (+/- M)`。
4. **预期结果**：相同拓扑下，`bounded_1` 与 `bounded_0` 的每操作耗时通常高于 `bounded_n` / `unbounded`（缓冲小→更频繁同步）；`create` 是最快的（仅建通道）。**具体 ns/iter 数值待本地验证**。若无 nightly，可改为阅读型实践：对照 [unbounded::spsc](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L79-L106) 与 [bounded_0::spsc](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L577-L604)，标出二者唯一区别就是被测通道从 `unbounded()` 换成了 `bounded(0)`。

#### 4.3.5 小练习与答案

**练习 1**：`b.iter` 的闭包里只做了 `s1.send` 和 `r2.recv`，工作线程里真正的 `s.send/r.recv`（被测代码）似乎「不在闭包里」，它怎么会被计入耗时？

> **答案**：发令枪 `s1.send(())` 会**唤醒**阻塞在 `r1.recv()` 的工作线程，且因为是 `bounded(0)` 会合通道，`s1.send` 直到工作线程真正开始干活才返回；终点线 `r2.recv()` 会**阻塞到**工作线程干完活并执行 `s2.send(())`。因此从「鸣枪」到「全员冲线」这段时间，恰好包裹了所有工作线程的 `s.send/r.recv`——闭包的墙钟时间就是被测代码的时间。

**练习 2**：为什么 `bounded_n::mpmc` 在 [crossbeam.rs:363-365](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs#L363-L365) 处有一句 `assert_eq!(threads % 2, 0)`？

> **答案**：该 bench 把 `threads` 平分为「一半发送、一半接收」（`threads/2` 各一类），若 `threads` 为奇数则无法均分、会少 spawn 一个线程导致 `r2.recv()` 永远等不到对应数量的 `s2.send(())` 而死锁。`assert` 在基准启动前就把这个不变量钉死。

---

### 4.4 把性能数字关联回设计：无锁、少锁与 CachePadded

#### 4.4.1 概念说明

跑出数字只是第一步，更重要的是**解释数字**。crossbeam-channel 的高吞吐来自三处具体设计：

1. **array flavor 的无锁环形队列**：`bounded(cap>0)` 用 Vyukov MPMC 环形缓冲，收发各只对自己的游标做一次 CAS，**全程不持锁**（详见 [u2-l5](u2-l5-array-flavor.md)）。
2. **list flavor 的「发送方永不阻塞」**：`unbounded` 永远有地方写（链表按需增长），发送路径无需等待、无需锁（详见 [u2-l6](u2-l6-list-flavor.md)）。
3. **`CachePadded` 隔离热点字段**：把生产者写的 `tail` 与消费者写的 `head` 各自撑到独占缓存行，消除伪共享。

这三点直接决定了 `seq/spsc/mpmc` 场景下的柱子长短。相对地，`std::sync::mpsc` 之类若用一把全局 `Mutex` 保护队列，多线程争用时会大量内核态切换，柱子就会长很多。

#### 4.4.2 核心流程：为什么消除伪共享能提速

假设 `head` 和 `tail` 是两个相邻的 `AtomicUsize`（各 8 字节），它们大概率落在**同一根 64 字节缓存行**里。消费者每 `recv` 一次就改 `head`，生产者每 `send` 一次就改 `tail`：

\[ \text{吞吐损失} \propto (\text{跨核缓存同步次数}) \]

每次一方写入，硬件作废另一方对该缓存行的副本；对方下一次访问就是 cache miss。在 spsc 这种高频场景里，这是**纯开销**——两个线程逻辑上无冲突，却被硬件「假性」串行化。`CachePadded` 给每个字段补齐到缓存行边界后，两根缓存行互不影响，跨核作废次数降到接近 0。

> 在 mpmc（多对多）里，所有生产者争写同一个 `tail`、所有消费者争写同一个 `head`，这部分的 CAS 失败重试是**真竞争**（无法消除），但至少不会再叠加「同行的其它字段被连带作废」的伪共享损失。

#### 4.4.3 源码精读

**array flavor 的 `CachePadded` 包裹**（这是性能解读反复回到的源头）：

- [src/flavors/array.rs:60-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L60-L93) —— `Channel<T>` 的 `head` 与 `tail` 字段类型是 `CachePadded<AtomicUsize>`，注释明确说明 `head` 上 mark bit 恒为 0、`tail` 上 mark bit 表示断开。生产者只写 `tail`、消费者只写 `head`，两者用 `CachePadded` 隔离到不同缓存行。
- [src/flavors/array.rs:125-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L125-L126) —— 构造时用 `CachePadded::new(AtomicUsize::new(...))` 初始化。

**list flavor 同样隔离 `head/tail`**（无界场景下消费端单写 `head`、生产端 CAS 推进 `tail`）：

- [src/flavors/list.rs:179-182](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L179-L182) —— `head: CachePadded<Position<T>>` 与 `tail: CachePadded<Position<T>>`。

**「少锁」的真正含义**：array/list 的**数据搬运路径无锁**（CAS + 版本号），但**阻塞者队列** `senders/receivers: SyncWaker` 内部仍有一把 `Mutex`（见 [u2-l4](u2-l4-blocking-and-waking.md) 与 [u3-l5](u3-l5-utils-and-alloc.md)）。所以严格说是「少锁」而非「全无锁」：

- 只在通道**满/空需要 park** 时才碰 `SyncWaker` 的锁；
- `SyncWaker` 还配了 `AtomicBool(is_empty)` 快速路径，无阻塞者时一次原子 load 即返回，不取锁。
- 阻塞者的临界区只做「指针/原子操作」，且 `Mutex` 是非毒封装（[u3-l5](u3-l5-utils-and-alloc.md)），所以这把锁开销极小、且不会因 panic 永久瘫痪通道。

这就是为什么 `bounded_*` / `unbounded_*` 在 `seq`（根本不阻塞）场景里几乎看不到锁成本，而在 `mpmc` 高争用场景里仍能胜过「全锁」实现。

**会合通道 zero 的定位**：`bounded0` 是 `Mutex<Inner>` 保护的会合通道（[u2-l7](u2-l7-zero-flavor.md)），每次交接都进锁——这解释了为什么基准里 `bounded0_*` 柱子总是最长：它的设计目标是「正确会合」而非「高吞吐」，容量为 0 注定无法靠缓冲掩盖同步开销。

#### 4.4.4 代码实践

1. **目标**：把基准数字与源码设计一一对应。
2. **步骤**：
   - 跑 `cargo run --release --bin crossbeam-channel`（在 `benchmarks/` 下），记录 `unbounded_spsc`、`unbounded_mpmc`、`bounded0_spsc` 的秒数。
   - 打开 [`src/flavors/array.rs:60-93`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L60-L93) 与 [`src/flavors/list.rs:179-182`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L179-L182)，对照确认 `head/tail` 均被 `CachePadded` 包裹。
   - 画一张表：`unbounded_spsc（快）← list 发送不阻塞 + CachePadded`；`bounded_*_mpmc（中）← array 无锁 CAS + 真竞争残留`；`bounded0_*（慢）← zero 每次进锁`。
3. **观察**：缓冲越大越快、会合最慢、select 比裸收发多一层开销。
4. **预期结果**：能用三句话把每个数字归因到具体源码设计，而不是笼统说「它很快」。**精确秒数待本地验证**，但归因链条（CachePadded → 消除伪共享；array CAS → 无锁搬运；list 永不阻塞发送 → spsc 极快；zero 进锁 → 会合最慢）是确定的设计事实。

> 进阶验证（可选）：把 [`src/flavors/array.rs:68`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L68) 的 `CachePadded<AtomicUsize>` 临时改成裸 `AtomicUsize` 重新跑 `bounded_*_mpmc`，理论上 mpmc 数字会变差（伪共享回归）。这只是理解用，**切勿提交**（本讲义禁止改源码）。

#### 4.4.5 小练习与答案

**练习 1**：array flavor 的数据搬运路径「无锁」，那为什么还要在 `Channel` 里放 `senders: SyncWaker` / `receivers: SyncWaker`？它们不也带 `Mutex` 吗？

> **答案**：搬运路径（`start_send`/`write`、`start_recv`/`read`）确实无锁，靠 CAS 推进游标。`SyncWaker` 只服务于「通道满/空时把线程 park、对端 notify 唤醒」这一阻塞唤醒流程（[u2-l4](u2-l4-blocking-and-waking.md)）。它的 `Mutex` 仅在登记/摘除阻塞者时短暂持有，且有 `is_empty` 原子快速路径，不参与搬运——所以整体仍是「数据面无锁、控制面少锁」，吞吐不被锁拖累。

**练习 2**：`bounded1_*`（容量 1）通常比 `bounded_*`（容量 N）慢，也比 `bounded0_*`（容量 0）快。用 array/zero flavor 的区别解释这一中间位置。

> **答案**：`bounded(1)` 是 array flavor（`cap>0`），数据面无锁 CAS，但因缓冲只有 1 格，生产消费几乎步步同步、频繁进 `SyncWaker` 阻塞唤醒；`bounded(N)` 缓冲大，阻塞唤醒次数大幅减少，故更快；`bounded0` 是 zero flavor，每次交接都进 `Mutex<Inner>`，比 array 的 CAS 慢，故最慢。`bounded1` 处在「无锁但频繁同步」的中间档。

---

## 5. 综合实践

把本讲两套基准与设计归因串起来，完成下面这个「基准解读 + 最小复现」任务：

**任务**：为 crossbeam-channel 写一份你自己的**单页性能速查**，要求：

1. 在 `benchmarks/` 下跑 `crossbeam-channel` 二进制，把六场景×四容量的耗时填入一张表（**数值待本地验证**，跑不到的就留空并注明原因，如 `bounded0_seq` 因死锁缺失）。
2. 对表里**最快**和**最慢**的两格，分别引用一条源码证据解释原因（最快格→引用 `CachePadded` 或 list「发送不阻塞」；最慢格→引用 zero 的 `Mutex` 或 select 的调度开销）。
3. 自选一个拓扑（如 `mpsc`），对比 `bounded(0)`、`bounded(1)`、`bounded(N)`、`unbounded` 四种容量的耗时曲线，用一句话总结「容量如何影响吞吐」。
4. （加分项）若装了 nightly，跑 `cargo +nightly bench unbounded::create`，记录 `create` 的 `ns/iter`，并与对比套件里 `seq` 场景的每条消息耗时比较，体会「建一次通道」相对「发一条消息」的量级差异。

通过这个任务，你会亲手把「跑数字 → 读源码 → 解释数字」这条链路走通，而不是停留在「跑出来很厉害」的表层。

---

## 6. 本讲小结

- crossbeam-channel 有**两套基准**：`benchmarks/` 是跨实现（Go/flume/mpsc/futures…）的横向对比套件，用 `run.sh` 串联、`plot.py` 出图；`benches/crossbeam.rs` 是只测自身的 nightly `#[bench]` 微基准。二者不可混用命令。
- `benchmarks/README.md` 定义六种标准场景 `seq/spsc/mpsc/mpmc/select_rx/select_both`，默认 `N=5_000_000`、`T=4`；`crossbeam-channel.rs` 用 `cap: Option<usize>` 参数化，让一套函数跑出 `bounded0/bounded1/bounded/unbounded` 四组结果。
- `run.sh` 顺序跑 5 个默认竞品（避免互相争 CPU），结果按「`test lang impl secs`」五行文本落盘，`plot.py` 按 `_` 拆分容量前缀画 4 子图。
- `benches/crossbeam.rs` 的精髓是「发令枪 + 终点线」隔离模式：用两个 `bounded(0)` 通道把工作线程的收发**精确包裹**在 `b.iter` 闭包的墙钟时间内，线程启停不计入。
- 性能归因有三条主线：array 的无锁 CAS 环形队列、list 的发送方永不阻塞、`CachePadded` 消除 `head/tail` 伪共享；它们共同决定了基准里 `unbounded_*`/`bounded_*` 柱子短、`bounded0_*`（zero 进锁）柱子长。
- 术语纠正：`benches/` 用的是 nightly 内置 `test::Bencher`，**不是 criterion**；项目无 criterion 依赖、无 `[[bench]]`/`harness` 声明。

---

## 7. 下一步学习建议

- 若想看「别人怎么用 crossbeam-channel 写真实程序」，直接进 [u3-l9（综合实践）](u3-l9-comprehensive-practice.md)，它用 `examples/fibonacci.rs`、`examples/matching.rs`、`examples/stopwatch.rs` 串讲 flavor 选择与 select 编排。
- 若想回到正确性内核，读 [u3-l8（测试体系）](u3-l8-testing.md) 看 `tests/` 如何用随机并发测试（含从 Go/mpsc 移植的语义兼容性测试）保证基准里的「快」不以「错」为代价。
- 对基准方法论本身感兴趣的读者，可对比阅读 `benchmarks/` 下其它竞品实现（如 [`flume.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/flume.rs)、[`mpsc.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/mpsc.rs)、[`go.go`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/go.go)），体会「同一套场景契约、不同实现」的对比设计如何复用。
