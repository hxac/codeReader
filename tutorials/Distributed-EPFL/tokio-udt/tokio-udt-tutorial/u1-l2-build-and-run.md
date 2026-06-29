# 构建运行与示例：跑通 sender / receiver

## 1. 本讲目标

在上一讲（u1-l1）里，我们已经从 README、`Cargo.toml`、`src/lib.rs` 三个门面文件建立了对 tokio-udt 的整体认知。本讲不再讲协议背景，而是把项目真正「跑起来」。

学完本讲，你应该能够：

- 知道仓库自带的两个示例程序 `udt_sender` 和 `udt_receiver` 是怎么被 Cargo 自动发现并编译成 binary 的。
- 在本地用两条 `cargo run` 命令跑通一对收发端，看到它们互相收发数据。
- 看懂 `udt_sender` 在运行时打印的 `Period`（发送周期）与 `Window`（拥塞窗口）两项指标代表什么。
- 初步体会到 UDT 在高速传输时是如何**动态调节发送速率**的——这正是它区别于「死磕一个固定速率发送」的关键。

本讲对应的三个最小模块是：`bin/udt_sender`、`bin/udt_receiver`、`RateControl 只读指标`。

## 2. 前置知识

在动手前，先用通俗语言铺垫几个概念。

**什么是 binary（可执行程序）？**
一个 Rust crate 可以是一个库（给别人 `use`），也可以编译出一个可以直接运行的程序。tokio-udt 既是库（`src/lib.rs` 导出公共 API），又在 `src/bin/` 目录下放了两段「使用这个库」的小程序。放在 `src/bin/` 下的每个 `.rs` 文件，Cargo 会自动当成一个独立的可执行程序来编译，文件名（去掉 `.rs`）就是程序名。

**什么是「客户端 / 服务端」？**
- **服务端（receiver）**：先「占住」一个端口（`bind`），然后守在那儿等别人来连（`accept`）。被动的一方。
- **客户端（sender）**：主动去找服务端的地址端口建立连接（`connect`），然后开始发数据。主动的一方。

本讲的 `udt_receiver` 是服务端，`udt_sender` 是客户端。

**什么是吞吐（throughput）？**
单位时间内传输的数据量，常用 MB/s（每秒多少兆字节）衡量。receiver 每秒打印一次「Received X MB」，就是在告诉我们当前的吞吐。

**什么是发送周期（pkt_send period）和拥塞窗口（congestion window）？**
这是 UDT 拥塞控制两个最直观的旋钮（暂时不用深究原理，后面 u7 单元会专讲）：

- **发送周期（Period）**：相邻两个数据包之间间隔多久发一个。单位是时间（如微秒 µs）。周期越小，发得越快。
- **拥塞窗口（Window）**：在还没收到确认（ACK）之前，最多允许「在路上」同时存在多少个包。窗口越大，越能并行塞满管道。

UDT 会根据网络状况**实时调整**这两个值：网络顺畅就加大窗口、缩短周期（加速）；发现丢包就缩小窗口、拉长周期（减速）。这正是我们在本讲要观察到的「活的」行为。

## 3. 本讲源码地图

本讲涉及的文件非常少，全部是「门面级」的：

| 文件 | 作用 |
|------|------|
| `src/bin/udt_sender.rs` | 客户端示例：连接 `127.0.0.1:9000`，循环发送一段固定数据，并周期打印发送周期 / 拥塞窗口。 |
| `src/bin/udt_receiver.rs` | 服务端示例：在 `0.0.0.0:9000` 监听，接受连接并统计每秒接收到的 MB。 |
| `Cargo.toml` | 声明依赖与特性；其中 `tokio` 的 `rt-multi-thread` 等特性是两个 binary 能跑起来的前提。 |
| `src/rate_control.rs` | `RateControl` 结构体，本讲只关心它暴露的两个只读方法 `get_pkt_send_period` / `get_congestion_window_size`。 |
| `src/connection.rs` | `UdtConnection::rate_control()` 方法，让 sender 拿到内部 `RateControl` 的引用以打印指标。 |

> 这五个文件里，前三个是本讲重点精读对象；后两个只是为了讲清楚「sender 打印的指标是从哪儿来的」，点到为止，深入留到后续单元。

## 4. 核心概念与源码讲解

### 4.1 二进制的自动发现与运行方式

#### 4.1.1 概念说明

你可能注意到：`Cargo.toml` 里**没有**任何 `[[bin]]` 段，那 Cargo 怎么知道要编译 `udt_sender` 和 `udt_receiver`？

答案在 Cargo 的「自动发现（auto-discovery）」约定：凡是不在 `src/bin/` 之外的特殊位置、名为 `main.rs` 或放在 `src/bin/` 下的 `.rs` 文件，都会被自动当成一个可执行 target，**文件名即程序名**。所以 `src/bin/udt_sender.rs` → 程序 `udt_sender`，`src/bin/udt_receiver.rs` → 程序 `udt_receiver`。

这点很关键，因为它决定了我们怎么 `cargo run`：必须用 `--bin` 指定程序名，而不是 `--lib`。

#### 4.1.2 核心流程

```text
src/bin/udt_sender.rs   ──(cargo 自动发现)──▶  target/.../udt_sender
src/bin/udt_receiver.rs ──(cargo 自动发现)──▶  target/.../udt_receiver

运行：
  cargo run --bin udt_receiver      # 服务端先起
  cargo run --bin udt_sender        # 客户端再起（另一个终端）
```

两个程序都依赖 `#[tokio::main]` 提供的异步运行时，而运行时需要 `tokio` 的 `rt-multi-thread` 特性。这些特性在 `Cargo.toml` 里已经启用，所以我们什么都不用改，直接跑即可。

#### 4.1.3 源码精读

`Cargo.toml` 中为两个 binary 提供运行时与依赖的关键行：

[依赖声明 Cargo.toml:14-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L14-L24) —— `tokio` 启用了 `macros`（让 `#[tokio::main]` 可用）、`net`（UDP/TCP socket）、`io-util`（`AsyncReadExt` / `AsyncWriteExt`）、`rt-multi-thread`（多线程运行时）等特性。注意 `tokio-timerfd` 只在 Linux 下启用（`cfg(target_os="linux")`），它关系到底层高精度定时，但本讲跑示例时不需要关心。

两个 binary 的入口都标注了 `#[tokio::main]`：

[udt_sender 入口 src/bin/udt_sender.rs:5-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L5-L9) —— `#[tokio::main]` 把 `async fn main` 包装成同步入口并启动 Tokio 运行时；`UdtConnection::connect("127.0.0.1:9000", None)` 里的第二个参数 `None` 表示用默认 `UdtConfiguration`（上一讲已介绍）。

#### 4.1.4 代码实践

1. 实践目标：确认两个 binary 确实被 Cargo 识别。
2. 操作步骤：在仓库根目录执行 `cargo build --bins`（编译所有 binary）或 `cargo build --bin udt_sender`。
3. 需要观察的现象：编译成功，`target/debug/` 下出现 `udt_sender` 和 `udt_receiver` 两个可执行文件。
4. 预期结果：编译无错误。若报缺运行时特性，说明 `Cargo.toml` 被改动过。
5. 实际产物路径：**待本地验证**（不同平台路径略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `src/bin/udt_sender.rs` 改名为 `src/bin/my_sender.rs`，运行命令要怎么变？
**答案**：程序名随文件名变化，需改用 `cargo run --bin my_sender`。Cargo 自动发现以文件名为准。

**练习 2**：为什么不能直接 `cargo run`（不带 `--bin`）？
**答案**：因为这个 crate 有多个 binary target（`udt_sender` 和 `udt_receiver`），`cargo run` 不知道你想跑哪一个，会报错要求用 `--bin` 指定。

---

### 4.2 udt_sender：连接、循环发送与速率指标打印

#### 4.2.1 概念说明

`udt_sender` 是一个「压力发送端」：它连上 receiver 后，**不停地**把一段固定数据写过去，从而制造持续的高速流量，让我们有机会观察到拥塞控制的真实调节过程。

它做的事很朴素：

1. 连接到 `127.0.0.1:9000`。
2. 构造一段 1.2 MB 的固定数据（`"Hello World!"` 重复 100000 次）。
3. 在死循环里反复 `write_all` 这段数据。
4. 每隔 1 秒，打印已发送条数，以及当前的发送周期和拥塞窗口。

#### 4.2.2 核心流程

```text
connect(127.0.0.1:9000, None)        # 建立连接（阻塞 async）
  │
  ▼
构造 buffer = "Hello World!" × 100000   # 12 字节 × 100000 = 1,200,000 字节
  │
  ▼
loop {
    write_all(&buffer)                  # 每次发 1.2 MB，成功则 count += 1
    if 距上次打印 > 1 秒 {
        打印 count（条数）
        打印 Period  = rate_control().get_pkt_send_period()
        打印 Window  = rate_control().get_congestion_window_size()
    }
}
```

> 注意：这个循环是**无限循环**，没有退出条件（末尾那行 `connection.close()` 是被注释掉的死代码）。正常退出方式是按 `Ctrl+C` 终止进程。

#### 4.2.3 源码精读

数据块的构造——这是计算吞吐的基准：

[构造发送缓冲 src/bin/udt_sender.rs:13-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L13-L17) —— `repeat(b"Hello World!")` 把这段 12 字节的切片重复 100000 份，`flat_map(|b| *b)` 把每份展开成单个字节再拍平，最终得到一个长度为 \( 12 \times 100000 = 1{,}200{,}000 \) 字节（约 1.2 MB）的 `Vec<u8>`。下一行 `println!("Message length: {}", buffer.len())` 会打印出这个长度，可以据此换算吞吐。

核心发送与打印循环：

[发送与指标打印 src/bin/udt_sender.rs:22-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L22-L42) —— 关键三段：

1. `connection.write_all(&buffer).await`：把整块 1.2 MB 写进去（UDT 内部会切片成多个包，后续单元会讲），成功后 `count += 1`。
2. `if last.elapsed() > Duration::new(1, 0)`：每超过 1 秒进入一次打印分支。
3. 打印 `Period` 和 `Window`——这两个值来自 `connection.rate_control()`，本讲 4.4 节会解释它们的来源。

注意 `count` 计的是「写了多少个 1.2 MB 的块」，所以吞吐可以粗算为 \( \text{count} \times 1.2 \text{ MB/s} \)（粗略，因为 count 是上一秒累积的值）。

#### 4.2.4 代码实践

1. 实践目标：先单独读懂 sender 的输出含义，不急着跑。
2. 操作步骤：阅读 [udt_sender.rs:22-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L22-L42)。
3. 需要观察的现象（静态阅读）：回答「`count` 计的是什么单位」「为什么用 `last.elapsed() > 1s` 而不是 `sleep(1s)`」。
4. 预期结果：`count` 的单位是「1.2 MB 的块数」；用 `elapsed()` 判断是为了「不阻塞发送、只在恰好满 1 秒的某次循环顺便打印」，`sleep` 则会强制等待、扭曲测量。
5. 实际运行数值：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：若想把每次发送的数据量减半，应该改哪一行？改成多少？
**答案**：改 [udt_sender.rs:14](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L14) 的 `.take(100000)` 为 `.take(50000)`，块大小变为 600000 字节。

**练习 2**：`count` 变量在进入打印分支后会被重置吗？
**答案**：不会。代码里 `last` 被重置为 `Instant::now()`，但 `count` 一直累加，所以打印的是「从程序启动以来的累计条数」，而非「这一秒内的条数」。要算每秒速率需自己记录两次 count 的差值。

---

### 4.3 udt_receiver：监听、accept 与吞吐统计

#### 4.3.1 概念说明

`udt_receiver` 是服务端。它先 `bind` 到 `0.0.0.0:9000`（监听所有网卡的 9000 端口），然后在一个循环里不断 `accept` 新连接。每来一个连接，它就**派生（spawn）一个独立的异步任务**去接收数据并统计吞吐。

这里出现了一个重要的并发模式：**主循环只负责 accept，真正的收数据工作丢给 spawn 出去的任务**。这样服务端就能同时接纳多个 sender。

#### 4.3.2 核心流程

```text
bind(0.0.0.0:9000, None)              # 绑定并开始监听
  │
  ▼
loop {
    (addr, connection) = accept()      # 阻塞等待一个新连接
    println("Accepted ...")
    spawn {                            # 为每个连接开一个独立任务
        loop {
            size = connection.read_buf(&mut buffer)
            bytes += size              # 累加本次读取字节数
            if 距上次打印 > 1 秒 {
                打印 "Received {bytes/1e6} MB"
            }
            if buffer.len() >= 10_000_000 { buffer.clear() }   # 防止无限增长
        }
    }
}
```

#### 4.3.3 源码精读

监听与接受连接：

[监听端口 src/bin/udt_receiver.rs:8-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_receiver.rs#L8-L10) —— `UdtListener::bind` 返回一个监听器；第二个参数 `None` 同样表示用默认配置。`0.0.0.0` 表示监听本机所有网卡。

[accept 循环 src/bin/udt_receiver.rs:14-15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_receiver.rs#L14-L15) —— `listener.accept().await` 返回 `(addr, connection)`：对端地址和一个可读写的 `UdtConnection`。

接收数据与吞吐统计（每个连接独立任务）：

[收数据与统计 src/bin/udt_receiver.rs:21-47](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_receiver.rs#L21-L47) —— 几个要点：

1. `let mut bytes = 0;` 在 `spawn` 块内声明，所以**每个连接有自己独立的字节计数器**，互不干扰。
2. `connection.read_buf(&mut buffer).await` 返回本次读到的字节数 `size`，`bytes += size` 累加。
3. 出错（`Err`）时打印累计接收量并 `break`——这就是该连接任务结束的方式（注意拼写是 `Connnection`，三个 n，仓库原文如此）。
4. `buffer.clear()` 在 `buffer.len() >= 10_000_000` 时触发，目的是防止 `read_buf` 不断追加导致缓冲无限膨胀。

> 关于 `bytes as f64 / 1e6`：这是把字节数换算成「十进制兆字节」（1 MB = \(10^6\) 字节）。所以 `Received X MB` 里的 X 直接就是「累计字节 ÷ 一百万」。两次打印的差值近似为这一秒的吞吐（MB/s）。

#### 4.3.4 代码实践

1. 实践目标：理解「累计字节」与「每秒吞吐」的区别。
2. 操作步骤：阅读 [udt_receiver.rs:22-40](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_receiver.rs#L22-L40)。
3. 需要观察的现象：`bytes` 是单调累加的；要算「每秒速率」需要相邻两次打印的 `bytes` 差值。
4. 预期结果：能口算「若两次打印分别是 12.0 MB 和 24.0 MB，则该秒吞吐约 12 MB/s」。
5. 实际运行数值：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bytes` 要声明在 `spawn({...})` 的块里，而不是 `main` 函数顶部？
**答案**：因为每个连接对应一个独立的 spawn 任务，`bytes` 需要「按连接隔离」。若放在 main 顶部并被多个任务共享 `move`，会引发所有权冲突，且无法区分不同连接的流量。

**练习 2**：`buffer.clear()` 的触发阈值是多少？为什么需要它？
**答案**：阈值是 `10_000_000`（10 MB）。因为 `read_buf` 会持续向 buffer 追加数据，不清理会导致内存随时间无限增长；定期清空让缓冲占用有上界。

---

### 4.4 RateControl 只读指标：发送周期与拥塞窗口

#### 4.4.1 概念说明

sender 打印的 `Period` 和 `Window` 不是凭空来的，它们来自连接内部的 `RateControl`（速率控制器）。本讲我们**只把它当一个只读的仪表盘**，理解这两个读数的含义和初始值；至于它如何根据 ACK / 丢包来调整，留到 u7 单元「拥塞控制算法」深入。

`UdtConnection` 提供了一个 `rate_control()` 方法，拿到对内部 `RateControl` 的借用，于是 sender 能读到当前实时指标。

#### 4.4.2 核心流程

```text
connection.rate_control()
      │  返回一把对内部 RateControl 的写锁 guard
      ▼
guard.get_pkt_send_period()         # 读 Period（Duration）
guard.get_congestion_window_size()  # 读 Window（u32，单位：包）
```

两个指标的物理含义：

| 指标 | 类型 | 含义 | 变大意味着 |
|------|------|------|-----------|
| `pkt_send_period` | `Duration` | 相邻两个数据包之间的发送间隔 | 间隔越长 → 发得越慢 |
| `congestion_window_size` | `u32` | 未确认时最多允许「在途」的包数 | 窗口越大 → 越能塞满管道 |

UDT 的总发送速率，本质上由这两个量共同约束：\( \text{速率} \approx \min\left(\frac{1}{\text{Period}}, \frac{\text{Window}}{\text{RTT}}\right) \)。直观地说，受限于「按周期发」或「按窗口发」中更紧的那一个。

#### 4.4.3 源码精读

`rate_control()` 暴露内部控制器：

[rate_control() 方法 src/connection.rs:83-87](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L83-L87) —— 它返回一个 `RwLockWriteGuard<RateControl>`，即对 socket 内部 `RateControl` 的**写锁**引用。注意它给的是写锁（即便本讲只是读取），所以在持有这把 guard 期间，发送侧的拥塞逻辑会被短暂阻塞——本讲只是每秒读一次，影响可忽略；但写入指标时要注意别长时间持锁。

两个只读方法：

[get_pkt_send_period / get_congestion_window_size src/rate_control.rs:79-85](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L79-L85) —— 这就是 sender 打印的两个值的来源。`get_pkt_send_period` 直接返回内部 `pkt_send_period`；`get_congestion_window_size` 把内部的 `f64` 窗口 `as u32` 返回。

初始值（程序刚启动、尚未收到任何反馈时的读数）：

[RateControl 初始值 src/rate_control.rs:35-61](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L35-L61) —— 初始 `pkt_send_period = Duration::from_micros(1)`（1 微秒，非常快），`congestion_window_size = 16.0`（16 个包），且 `slow_start = true`（处于慢启动阶段）。所以你跑起 sender 后，**最早打印的一组值**很可能就是 Period≈1µs、Window=16 附近，随后随着 ACK 反馈逐步变化。

> 这一段只是让你「对得上号」：看到 sender 打印的数字，能知道它对应 `RateControl` 的哪个字段、初始是多少。至于 `slow_start`、`on_ack`、`on_loss` 这些改变这两个值的过程，是 u7 单元的主线。

#### 4.4.4 代码实践

1. 实践目标：在不真正发送大量数据的前提下，验证两个只读方法的返回类型。
2. 操作步骤：写一个最小示例（**注意：这是示例代码，不是仓库原有文件**），只 connect、不进死循环，立刻打印一次指标：
   ```rust
   // 示例代码：可放在 src/bin/peek_rc.rs 后用 cargo run --bin peek_rc 运行
   use tokio_udt::UdtConnection;
   #[tokio::main]
   async fn main() {
       // 需要先在另一终端起 udt_receiver
       let conn = UdtConnection::connect("127.0.0.1:9000", None).await.unwrap();
       let rc = conn.rate_control();
       println!("period={:?}, window={}", rc.get_pkt_send_period(), rc.get_congestion_window_size());
   }
   ```
3. 需要观察的现象：打印出的 `period` 为 `1µs`、`window` 为 `16`（与 4.4.3 的初始值一致）。
4. 预期结果：因为尚未收发数据，`RateControl` 仍是初始状态。
5. 实际运行数值：**待本地验证**（不同环境连接建立耗时略有差异，但初始字段值固定）。

#### 4.4.5 小练习与答案

**练习 1**：`get_congestion_window_size` 返回 `u32`，但内部字段是 `f64`。这种「读出来时截断为整数」会丢失什么信息？
**答案**：会丢失小数部分（如窗口实际是 16.7 时读出 16）。对于「粗略观察」够用，但若要精确分析拥塞行为，应意识到打印值是向下取整的。

**练习 2**：为什么 `rate_control()` 返回的是**写锁** guard 而不是只读 guard？
**答案**：因为 `RateControl` 内部很多修改路径需要写访问，统一提供写锁可以简化借用模型（拿到后既能读也能改）。代价是持有期间会独占——所以像本讲这样频繁长时间持锁的场景应注意只短暂使用。后续单元你会看到 `on_ack` / `on_loss` 都需要这把写锁来修改字段。

---

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「跑起来 + 看曲线」实验。这是本讲的核心动手任务。

**实践目标**：跑通一对收发端，亲眼看到 UDT 在持续高速传输下动态调节发送速率。

**操作步骤**：

1. 打开**两个终端**，都 `cd` 到仓库根目录。
2. 终端 A（服务端）先启动：
   ```bash
   cargo run --bin udt_receiver
   ```
   等待出现 `Waiting for connections...`。
3. 终端 B（客户端）再启动：
   ```bash
   cargo run --bin udt_sender
   ```
4. 让它运行 30 秒以上（让拥塞控制有时间经历「慢启动 → 稳态」过程）。
5. 每秒记录两侧输出：
   - 终端 A（receiver）的 `Received X MB`。
   - 终端 B（sender）的 `Sent N messages`、`Period`、`Window`。
6. 用 `Ctrl+C` 分别停止两个进程。

**需要观察并记录的现象**（建议画成随时间变化的曲线）：

- receiver 侧：相邻两次 `Received X MB` 的差值，即每秒吞吐（MB/s）。注意换算——sender 每条消息是 1.2 MB，可交叉验证。
- sender 侧：`Window` 是否从初始的 16 逐步增长？`Period` 是否从 1µs 开始变化？是否在某些时刻出现 `Period` 变大（说明发生了丢包回退）？

**预期结果**（定性，不保证精确数值）：

- 启动初期：`Window` 较小、`Period` 很短，吞吐较低（慢启动阶段）。
- 稳态：`Window` 增大、吞吐升高并趋于稳定。
- 丢包时：`Period` 明显变大（乘性回退），吞吐短暂下降后又缓慢恢复。

**重要说明**：

- 在 `127.0.0.1`（回环）上，几乎不丢包，拥塞控制动作可能不明显，吞吐主要由本机处理能力决定。**具体吞吐数值待本地验证**。
- 若想更明显地观察丢包与回退，需要在更真实（有延迟、有带宽限制）的网络环境里测试——这超出本讲范围，但记住这个限制。
- 本地环境若端口 9000 被占用，需改两个 binary 的端口（或用上一练提到的 `udp_reuse_port` 配置，留到 u2-l3）。

## 6. 本讲小结

- tokio-udt 在 `src/bin/` 下提供两个自动发现的 binary：`udt_sender`（客户端）和 `udt_receiver`（服务端），无需在 `Cargo.toml` 写 `[[bin]]`，用 `cargo run --bin <名字>` 即可运行。
- `udt_sender` 连接 `127.0.0.1:9000`，循环发送 1.2 MB 的数据块，每秒打印发送条数与两项速率指标。
- `udt_receiver` 监听 `0.0.0.0:9000`，为每个连接 spawn 独立任务累加字节并打印每秒接收量。
- sender 打印的 `Period`（发送周期）与 `Window`（拥塞窗口）来自 `connection.rate_control()`，对应 `RateControl` 的 `pkt_send_period` / `congestion_window_size` 字段，初始值分别为 1µs 和 16。
- 这两个指标会随网络反馈动态变化——这就是 UDT 区别于「定速发送」的核心，也是 u7 拥塞控制单元的预告。
- 在 `127.0.0.1` 回环上拥塞动作可能不明显；观察真实回退需更有挑战的网络环境。

## 7. 下一步学习建议

本讲你已经能「让项目跑起来」并看懂两个指标。接下来：

- **紧接的 u1-l3（目录结构与模块地图）** 会带你从 `src/lib.rs` 的模块声明画出整个 crate 的结构图，建立「文件 → 职责」的全局地图，为你后续深入任何模块提供导航。
- 之后 **u1-l4（公共 API 全貌与配置）** 会系统梳理对外导出的 5 个类型和 `UdtConfiguration` 的关键字段——本讲里反复出现的 `None`（默认配置）届时会被逐字段拆解。
- 如果你对本讲看到的 `Period` / `Window` 如何被改变很好奇，可以直接跳到 **u7-l2（速率控制 RateControl）**，但建议先走完入门层，建立足够上下文。

想加深印象的话，可以先把 u1-l3 的模块地图做了，再回头给两个 binary 加一行日志（例如在 sender 里每秒也打印 `count` 的差分速率），亲手验证你对吞吐换算的理解。
