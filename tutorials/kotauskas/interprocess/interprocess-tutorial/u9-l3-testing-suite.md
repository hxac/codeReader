# 测试体系与测试工具

## 1. 本讲目标

interprocess 是一个跨平台、依赖真实操作系统的 IPC 库，它的正确性高度依赖测试来兜底——Windows 与 Unix 两套后端共用同一套公共接口，只有靠测试才能验证「接口统一、实现分化」真的成立。本讲带你读懂 `tests/` 目录的全部组织方式，学完后你应该能够：

- 说清 interprocess 的测试**为何不以 Cargo 标准集成测试方式运行**，而是被挂载成库内部的一个 `mod tests`；
- 画出 `tests/index.rs` 的聚合关系，理解跨平台与 `tokio` feature 的编译期门控；
- 掌握 `tests::util` 工具箱里每一个辅助工具（`NameGen`、`drive_*`、`Choke`、`wdt`、`xorshift`、`eyre`、`tokio`）的用途与协作方式；
- 读懂 `no_server` / `no_client` / `timeout` 这类**边界场景**是如何被测试的；
- 能够模仿现有测试，自己新增一个跨平台集成测试。

## 2. 前置知识

在进入测试源码前，先澄清几个本讲反复用到的概念。

- **单元测试 vs 集成测试（Rust/Cargo 惯例）**：Rust 默认把 `src/` 内 `#[cfg(test)] mod tests` 视为单元测试（编译进库本体，能访问 `pub(crate)` 私有项）；把 `tests/` 目录下每个 `.rs` 文件视为一个独立的**集成测试二进制**（只能访问公共 API，每个文件单独编译）。interprocess 打破了这个惯例，后面会看到它怎么做的。
- **`autotests`**：Cargo.toml 的一个开关，默认为 `true`，即自动把 `tests/` 顶层每个文件当集成测试编译。设为 `false` 后，Cargo 不再自动发现 `tests/` 下的文件。
- **确定性随机**：测试需要「不容易撞名」的名字（避免并发或历史残留导致 `AddrInUse`），但又必须「可复现」。interprocess 用一个简易 PRNG（xorshift）+ 测试位置 ID 做种子，既不引入 `rand` 依赖，又能保证同一测试每次跑出同样的名字序列。
- **看门狗（watchdog）**：IPC 测试容易因死锁而无限挂起（例如双方同时写满缓冲）。看门狗用一个独立线程给测试设硬超时，超时即判定失败，避免 CI 卡死。
- **`color_eyre` / `eyre`**：`eyre` 是 `anyhow` 的可定制分支，`color_eyre` 在其上提供彩色、带回溯的错误报告。本讲里的 `TestResult` 就是 `eyre::Result<T>`。
- **节流（throttle）/ 信号量**：一个服务端测试要并发起几十个客户端，若全部同时连接可能耗尽系统资源（Windows 管道实例、临时端口）。用一个限流器把「同时在跑」的客户端数量限制在一个小上限（默认 6）。

本讲承接 u1-l3（构建、feature gate、示例运行），你应已经知道 `tokio` feature 默认关闭、示例用 `[[example]]` 显式声明等事实。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` | `autotests = false` 关掉自动集成测试发现 |
| `src/lib.rs` | 用 `#[cfg(test)] #[path = "../tests/index.rs"]` 把整个 `tests/` 挂成内部模块 |
| `tests/index.rs` | 测试聚合根：挂载 `util`、`os` 平台子模块、四大原语测试入口 |
| `tests/util/mod.rs` | 工具箱总装与公共辅助（`num_clients`、`test_wrapper`、`listen_and_pick_name`、`message`） |
| `tests/util/xorshift.rs` | `Xorshift32` 确定性 PRNG |
| `tests/util/namegen.rs` | `NameGen` 名字迭代器、`make_id!` 宏 |
| `tests/util/drive.rs` | 服务端/客户端编排：`drive_pair`、`drive_server_and_multiple_clients`、`exclude_deadconn` |
| `tests/util/choke.rs` | `Choke` 限流信号量 |
| `tests/util/wdt.rs` | 看门狗超时（2 秒） |
| `tests/util/eyre.rs` | `TestResult`、`ensure_eq!`、`opname`、一次性 `color_eyre` 安装 |
| `tests/util/tokio.rs` | 异步版 `test_wrapper` / `drive_*` |
| `tests/local_socket.rs` 及其子目录 | local socket 同步测试与边界用例 |
| `tests/tokio_local_socket.rs` 及其子目录 | local socket 异步测试 |
| `tests/os/windows/named_pipe.rs`、`tests/os/unix/local_socket/*` | 平台专有测试 |

## 4. 核心概念与源码讲解

本讲按两个最小模块展开：`tests`（挂载与聚合、边界用例）与 `tests::util`（名称生成、编排、运行时支撑）。

### 4.1 测试的挂载与聚合机制（最小模块：tests）

#### 4.1.1 概念说明

第一个要纠正的直觉是：「`tests/` 目录里的文件 = Cargo 自动编译的集成测试」。在 interprocess 里**不是**。interprocess 同时做了两件事：

1. 在 `Cargo.toml` 里把 `autotests` 设为 `false`，让 Cargo **停止**自动发现 `tests/` 下的文件作为集成测试；
2. 在 `src/lib.rs` 里用 `#[cfg(test)] #[path = "../tests/index.rs"] mod tests;`，把整个 `tests/` 目录树**当作库自身的一个内部模块**来编译。

这样做的根本动机是：测试代码需要访问库的 `pub(crate)` 私有项（如 `BoolExt`、`SubUsizeExt`、`misc` 里的工具），而标准集成测试是「外部 crate」，看不到私有项。挂成内部模块后，测试就和 `src/` 里的代码共享同一份编译、同一个 crate 根，`pub(crate)` 项对测试完全可见。代价是测试只有一个测试二进制（库的单元测试二进制），而非每个文件一个。

#### 4.1.2 核心流程

测试运行流程：

1. 执行 `cargo test`（或 `cargo test --lib`），Cargo 以 `cfg(test)` 编译库；
2. 因为 `autotests = false`，Cargo 不在 `tests/` 下另起集成测试二进制；
3. `src/lib.rs` 的 `#[cfg(test)] mod tests` 被激活，`#[path]` 把 `tests/index.rs` 作为模块体纳入；
4. `index.rs` 进一步 `mod` 出 `util`、各原语测试、平台测试，所有 `#[test]` 函数随库二进制一起编译运行。

```
cargo test
   └─ cfg(test) 编译 lib
        └─ mod tests  ←  #[path = "../tests/index.rs"]
              ├─ mod util        (tests/util/mod.rs)
              ├─ mod os          (平台门控: unix/windows)
              ├─ mod local_socket
              ├─ mod unnamed_pipe
              └─ mod tokio_*     (feature = "tokio" 门控)
```

#### 4.1.3 源码精读

首先是 `Cargo.toml` 关掉自动发现：

- [Cargo.toml:15-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L15-L16)：`autotests = false` 与 `autoexamples = false`。没有 `autotests = false`，Cargo 会尝试把 `tests/index.rs`（以及 `tests/local_socket.rs` 等）当独立集成二进制编译，而它们用的是 `crate::...` 路径，会编译失败。

然后是 `src/lib.rs` 的挂载点：

- [src/lib.rs:75-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L75-L78)：三行属性组合——`#[cfg(test)]` 仅测试编译生效；`#[path = "../tests/index.rs"]` 指定模块源文件；`#[allow(...)]` 给测试代码放宽库本体的严格 lint（`unwrap_used`、`arithmetic_side_effects`、`indexing_slicing`），最后一行 `mod tests;` 才是真正的模块声明。

再看聚合根 `index.rs` 全貌：

- [tests/index.rs:1-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/index.rs#L1-L29)：第 1-3 行用 `#[path]` + `#[macro_use]` 把 `util/mod.rs` 挂为 `mod util` 并把其中定义的宏（`make_id!`、`ensure_eq!`）引入文本作用域；第 5-20 行是平台门控的 `os` 模块；第 22-28 行声明四大原语的测试入口，其中 `tokio_local_socket` 与 `tokio_unnamed_pipe` 被 `#[cfg(feature = "tokio")]` 门控。

平台门控部分值得单独看：

- [tests/index.rs:5-20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/index.rs#L5-L20)：`os` 内部用 `#[cfg(unix)]` / `#[cfg(windows)]` 互斥编译——Unix 下测 `fake_ns`、`mode`、`try_overwrite`；Windows 下测 `local_socket_security_descriptor`、`named_pipe`、`tokio_named_pipe`。这与 u1-l2 讲过的「平台私有后端用 `#[cfg]` 互斥」完全一致，只是发生在测试侧。

#### 4.1.4 代码实践

**目标**：验证「测试确实走内部模块、而非独立集成二进制」。

**操作步骤**：

1. 在仓库根目录运行 `cargo test --lib --no-run`，观察编译输出里**只有一个**测试二进制目标（库自身的），而不是为 `tests/local_socket.rs` 等各生成一个。
2. 再运行 `cargo test --lib stream_file -- --list`（在 Unix 上），确认 `stream_file` 这个测试函数被列出——它是定义在 `tests/local_socket.rs` 里的，却出现在库二进制中。
3. （可选）临时把 `src/lib.rs:76` 的 `#[path]` 行注释掉，重新 `cargo test --lib`，会看到大量 `unresolved module tests` 之类的错误，反向印证挂载点的存在。**注意：实践后请恢复该行，不要真的修改源码提交。**

**需要观察的现象**：步骤 1 只产出一个二进制；步骤 2 能列出测试名。

**预期结果**：测试随库一起编译，`tests/` 下的文件不被当成独立集成 crate。

> 说明：本实践依赖本地 Cargo 环境，若无法运行请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Cargo.toml` 里的 `autotests = false` 删掉（恢复默认 `true`），会发生什么？

**答案**：Cargo 会尝试把 `tests/` 顶层每个 `.rs`（如 `tests/index.rs`、`tests/local_socket.rs`）当成独立集成测试二进制编译。这些文件内部用的是 `crate::local_socket::...`，但在集成测试里 `crate` 指代的是「那个新生成的集成测试 crate」，并没有 `local_socket` 模块，于是编译失败。`autotests = false` 正是为了阻止这种自动发现。

**练习 2**：为什么 `#[allow(clippy::unwrap_used, ...)]` 要写在 `mod tests;` 上，而不是写在 `src/` 的别处？

**答案**：因为这两个 `#[allow]` 只作用于它标注的那个模块（`tests`），即只放宽测试代码的 lint，而不污染库本体的严格 lint 策略。库本体（见 u9-l1）仍是 `forbid` 级别的高标准，测试代码则允许 `unwrap`、算术溢出等「写测试时图省事」的写法。

### 4.2 名称生成：确定性的随机名字（最小模块：tests::util）

#### 4.2.1 概念说明

IPC 测试要给每个监听器/客户端起一个「名字」（local socket 的名字、named pipe 的路径）。这个名字有两个互相冲突的需求：

- **不撞名**：并发跑几十个客户端、或在 CI 上反复跑，不能因为名字重复而撞到残留的 socket 文件导致 `AddrInUse`；
- **可复现**：失败时要能重跑出完全一样的名字序列，方便排查。

interprocess 的解法是「确定性 PRNG + 测试位置 ID 作种子」：每个测试用自己的源码位置（`file!() + line!() + column!()`）算出一个唯一种子，喂给 xorshift PRNG，生成一串「看起来随机、实则固定」的名字。这样既不引入 `rand` 依赖，又满足两个需求。

#### 4.2.2 核心流程

名字从生成到「绑定成功」的流程：

```
make_id!()  ──(file!,line!,column!)──▶  字符串 ID
       │
       ▼
Xorshift32::from_id(id)   哈希 ID → 32 位种子
       │  反复 next()
       ▼
NameGen 无限迭代器  ──每个 rn 生成一个候选名字──▶  Name<'static> / String
       │
       ▼
listen_and_pick_name(gen, bind)  逐个尝试绑定
       ├─ AddrInUse / PermissionDenied  → 跳过，取下一个名字
       ├─ 其它错误                       → 直接返回失败
       └─ 绑定成功                       → 返回 (name, listener)
```

关键：`listen_and_pick_name` 把「撞名」当成正常情况跳过，因为确定性 PRNG 仍可能在跨测试或跨进程时偶发撞名（例如两个测试恰好算出同一种子）。

#### 4.2.3 源码精读

先看 PRNG 本体——一个极简的 32 位 xorshift：

- [tests/util/xorshift.rs:9-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/xorshift.rs#L9-L26)：`Xorshift32(pub u32)` 是 `#[repr(transparent)]` 的透明包装。`from_id` 用 `DefaultHasher`（SipHash）把 ID 字符串哈希成 64 位，再高 32 位与低 32 位异或折叠成 32 位种子；`next` 用经典的 `13/17/5` 三次异或移位推进。注释直言「不想引入 `rand` crate，所以自己写一个」。

再看把 PRNG 包成无限迭代器的 `NameGen`：

- [tests/util/namegen.rs:7-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/namegen.rs#L7-L18)：`NameGen<T, F>` 持有一个 PRNG 和一个「把随机数映射成名字」的闭包 `name_fn`；`Iterator::next` 永远返回 `Some((self.name_fn)(self.rng.next()))`，所以是**无限迭代器**——这正是 `listen_and_pick_name` 能一直重试的前提。

然后是两个面向具体原语的构造器：

- [tests/util/namegen.rs:20-49](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/namegen.rs#L20-L49)：`namegen_local_socket(id, path)` 按 `path: bool` 选择「文件系统路径」还是「命名空间名」——前者在 Windows 上拼成 `\\.\pipe\interprocess-test-XXXXXXXX`、Unix 上拼成 `$TMPDIR/interprocess-test-XXXXXXXX.sock`，并经 `to_fs_name::<GenericFilePath>()` 解释成 `Name`（承接 u2-l4 的名称系统）；后者统一拼成 `interprocess-test-XXXXXXXX` 经 `to_ns_name::<GenericNamespaced>()` 解释。`namegen_named_pipe` 则直接返回裸 `String` 路径（给原生 named pipe 测试用）。

测试位置 ID 由这个宏生成：

- [tests/util/namegen.rs:51-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/namegen.rs#L51-L55)：`make_id!` 展开成 `concat!(file!(), line!(), column!())`，把「哪个文件、第几行、第几列」拼成字符串。由于每个测试调用点位置不同，种子自然不同；同一调用点每次编译后位置稳定，故种子可复现。

最后是「逐个试绑定、跳过撞名」的核心循环：

- [tests/util/mod.rs:54-80](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/mod.rs#L54-L80)：`listen_and_pick_name` 在 `namegen` 上做 `find_map`。关键在第 66-73 行：绑定回调返回的错误里，只有 `AddrInUse` 和 `PermissionDenied` 被当成「这个名字不行、换下一个」（返回 `None` 让迭代器继续），其它错误立刻 `return Some(Err(e))` 上抛。第 76 行 `.unwrap()` 安全，因为 `NameGen` 是无限迭代器，`find_map` 必然会终止（要么绑定成功、要么抛非撞名错误）。

#### 4.2.4 代码实践

**目标**：直观感受「确定性种子」——同一测试每次跑出同一名字，不同测试跑出不同名字。

**操作步骤**：

1. 阅读 `tests/local_socket/no_server.rs`，它只取 `namegen_local_socket(id, path).next()` 的第一个名字（[tests/local_socket/no_server.rs:25-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/no_server.rs#L25-L29)）。
2. 运行两次 `cargo test --lib no_server_file -- --nocapture`，观察 `eprintln!("Trying name ...")` 打出的第一个名字（在 `listen_and_pick_name` 第 61 行）。两次应该**完全相同**。
3. 对比 `no_server_file` 与 `stream_file` 两次打印的名字——应该**不同**，因为它们调用点的 `file!/line!/column!` 不同，种子不同。

**需要观察的现象**：同名测试两次跑出相同名字；不同名测试跑出不同名字。

**预期结果**：确定性 + 跨测试唯一性同时成立。

> 说明：实际打印输出依赖本地运行，若无法运行请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`NameGen` 为什么必须是**无限**迭代器？如果改成「最多生成 100 个名字」会怎样？

**答案**：因为 `listen_and_pick_name` 用 `find_map` 在撞名时不断跳过、取下一个，无法预知要跳过多少次。若迭代器有上限，一旦连续撞名耗尽名额，`find_map` 会返回 `None`，第 76 行的 `.unwrap()` 就会 panic。无限迭代器保证只要不是致命错误，迟早能绑到一个可用名字。

**练习 2**：`from_id` 为什么先把 64 位哈希**折叠**成 32 位，而不是直接用 64 位种子？

**答案**：因为 `Xorshift32` 的状态只有 32 位（`pub u32`）。SipHash 输出 64 位，必须压成 32 位才能当种子；这里用「高低 32 位异或」来尽量保留两部分的熵，避免简单截断丢掉一半信息。

### 4.3 测试编排：驱动、节流、看门狗（最小模块：tests::util）

#### 4.3.1 概念说明

有了名字还不够，一个真实的服务端测试要协调「1 个服务端 + 1 个首客户端 + N 个并发客户端」，还要防死锁、防资源耗尽、给出可读的错误。`tests::util` 提供了一整套编排工具：

- **`drive_pair`**：跑一对「leader/follower」，leader 线程跑到某处通过 `mpsc` 通道发一个值（通常是绑好的名字）给 follower，follower 收到后才开始跑——保证客户端不会在服务端绑定名字之前就连接。
- **`drive_server_and_multiple_clients`**：在 `drive_pair` 之上构建「服务端 + 首客户端 + N 客户端」的标准拓扑。
- **`Choke`**：限流信号量，把同时并发的客户端数限制在 `num_concurrent_clients`（默认 6）。
- **`exclude_deadconn`**：把「对端先出错/先断开」导致的一类连接错误过滤成 `Ok(())`，避免测试因收尾竞态而假阳性失败。
- **`wdt`（watchdog）**：给整个测试设 2 秒硬超时，防死锁卡死 CI。
- **`eyre` + `opname`**：统一错误类型 `TestResult`，并给每个 IO 操作打上名字（`opname("connect")`），失败时报告里一眼看出是哪步炸的。
- **`tokio`** 子模块：上面这些工具的异步镜像版（用 `task::spawn` + Tokio `Semaphore` + `try_join!`）。

#### 4.3.2 核心流程

`drive_server_and_multiple_clients` 的拓扑（同步版）：

```
drive_pair(server_wrapper, "server", client_wrapper, "client")
   │
   ├─ server 线程：listen_and_pick_name → 把 Arc<Name> 经 mpsc 发出 → incoming().take(N) 接 N 个连接
   │
   └─ client_wrapper（主线程收到名字后）：
         ├─ first_client(name)            ← 先用首客户端验证「名字可达」
         └─ thread::scope 内 spawn N 个 client 线程
               每个线程先 choke.take() 拿一个并发名额，跑完 drop 归还
```

异步版（`util/tokio.rs`）把线程换成 `task::spawn`、`Choke` 换成 Tokio `Semaphore`、`mpsc` 换成 `oneshot`、并用 `try_join!` 并发等待 leader/follower。

#### 4.3.3 源码精读

先看同步 `drive_pair`：

- [tests/util/drive.rs:15-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/drive.rs#L15-L46)：`thread::scope` 内 spawn 一个 leader 线程，它持有一个 `Sender<T>`，跑到合适时机把 `T`（一般是被 `Arc` 包起来的名字）发出去；主线程 `receiver.recv()` 一旦收到，就调用 `follower(msg)`。两侧结果都经 `exclude_deadconn` 过滤。第 41-43 行显式检测 leader 是否 panic。

`exclude_deadconn` 是「容忍收尾竞态」的关键：

- [tests/util/drive.rs:50-69](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/drive.rs#L50-L69)：当一侧的错误的根因（`root_cause`）是某个 `io::Error` 且其 `kind()` 属于 `ConnectionRefused/Reset/Aborted/NotConnected/BrokenPipe/WriteZero/UnexpectedEof` 之一时，就视作「对端先退出了，不是本测试关注的逻辑错误」，返回 `Ok(())`。注释里那句 `// oh FUCK OFF` 是给 rustfmt 看的跳过指令。

标准服务端拓扑：

- [tests/util/drive.rs:71-123](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/drive.rs#L71-L123)：`drive_server_and_multiple_clients` 用 `Choke::new(num_concurrent_clients())` 建限流器；`client_wrapper` 先跑 `first_client`（验证名字可达），再在 `thread::scope` 里 spawn `num_clients()` 个（默认 80）客户端线程，每个线程第 95-104 行先 `choke.take()` 拿名额（超限就阻塞等待），把 `ChokeGuard` move 进线程、跑完随线程结束 drop 归还。第 119 行把真正的 server 闭包包一层，注入 `num_clients`。

限流器本体：

- [tests/util/choke.rs:6-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/choke.rs#L6-L24)：`Choke` 是 `Arc<ChokeInner>`，内部用 `Mutex<u32>` 计数 + `Condvar` 通知。`take()` 在计数达到 `limit` 时 `condvar.wait` 阻塞，否则计数 +1 并发一个 `ChokeGuard`。注释点明它「是个限流信号量，但不保护任何并发资源」——纯粹用来限并发数。
- [tests/util/choke.rs:45-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/choke.rs#L45-L52)：`ChokeGuard` 持有 `Weak<ChokeInner>`，`Drop` 时尝试 `upgrade`，成功就 `decrement` 并 `notify_one` 唤醒一个等待者。用 `Weak` 是为了避免 guard 阻止 `Choke` 被释放。

看门狗：

- [tests/util/wdt.rs:7-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/wdt.rs#L7-L23)：常量 `TIMEOUT = Duration::from_secs(2)`。`run_under_wachdog`（注：源码如此拼写）spawn 一个线程跑真正的测试 `f()`，主线程 `recv_timeout(TIMEOUT)` 等它的「完成信号」；若 2 秒超时仍未收到，就 `bail!("watchdog timer has run out")` 判失败。

错误类型与命名：

- [tests/util/eyre.rs:6-15](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/eyre.rs#L6-L15)：`TestResult<T = ()> = eyre::Result<T>`；`install()` 用一个 `static Mutex<bool>` 保证 `color_eyre::install()` 全进程只装一次（重复装会报错）。
- [tests/util/eyre.rs:40-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/eyre.rs#L40-L45)：`WrapErrExt::opname` 把任意 `Result` 包上一层「`{loc} failed`」的上下文——这就是测试里满地 `.opname("connect")?`、`.opname("accept")?` 的来源，失败时报告会清楚标注是哪一步。

公共 `test_wrapper` 把 eyre 安装与看门狗串起来：

- [tests/util/mod.rs:39-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/mod.rs#L39-L42)：`test_wrapper` 先 `eyre::install()`，再把测试函数交给 `wdt::run_under_wachdog`。每个 `#[test]` 函数体通常就是 `test_wrapper(|| { ... })`。

并发量与客户端数量由环境变量可调：

- [tests/util/mod.rs:28-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/mod.rs#L28-L37)：`intvar` 读环境变量并解析为 `u32`；`num_clients` 读 `INTERPROCESS_TEST_NUM_CLIENTS`（默认 80），`num_concurrent_clients` 读 `INTERPROCESS_TEST_NUM_CONCURRENT_CLIENTS`（默认 6）。这样在资源受限的 CI 上可以调小，在本地强力机器上可以调大。

异步版镜像：

- [tests/util/tokio.rs:14-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/tokio.rs#L14-L22)：异步 `test_wrapper` 内部建一个 `current_thread` + `enable_io` 的 Tokio 运行时，再 `block_on(f)`，最外面套同步 `test_wrapper`（于是看门狗依然生效）。
- [tests/util/tokio.rs:51-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/tokio.rs#L51-L89)：异步 `drive_server_and_multiple_clients` 把线程换成 `task::spawn`、把 `Choke` 换成 `Arc<Semaphore>`、用 `acquire_owned().await` 拿许可、最后 `try_join!` 并发等待 leader/follower（见第 88 行调用同步的 `drive_pair` 异步版）。这呼应 u6-l1 讲过的「异步层是同步层的镜像」。

#### 4.3.4 代码实践

**目标**：体会限流器对并发的影响。

**操作步骤**：

1. 阅读并发量配置 [tests/util/mod.rs:32-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/mod.rs#L32-L37)。
2. 分别用两组环境变量跑同一个 stream 测试，对比行为：
   - `INTERPROCESS_TEST_NUM_CLIENTS=10 INTERPROCESS_TEST_NUM_CONCURRENT_CLIENTS=2 cargo test --lib stream_file -- --nocapture`
   - `INTERPROCESS_TEST_NUM_CLIENTS=10 INTERPROCESS_TEST_NUM_CONCURRENT_CLIENTS=10 cargo test --lib stream_file -- --nocapture`
3. （可选）把超时调到很小，制造一次「看门狗触发」：修改不可行（`TIMEOUT` 是 `const`），但你可以临时在某个测试里加一个 `std::thread::sleep(Duration::from_secs(3))`，观察 `bail!("watchdog timer has run out")`。**注意：验证后务必还原，不要提交对测试源码的修改。**

**需要观察的现象**：第 2 步两次都能通过，但第一种（并发 2）耗时更长，因为客户端被限流串行化；第二种（并发 10）更快。

**预期结果**：限流只影响吞吐，不影响正确性；看门狗在 2 秒超时时报错。

> 说明：耗时差异依赖本地机器与运行时状态，属「待本地验证」的观察项。

#### 4.3.5 小练习与答案

**练习 1**：`exclude_deadconn` 为什么是必要的？举个例子。

**答案**：在并发多客户端场景下，服务端可能在某客户端还没收尾时就因为另一个客户端的错误而提前退出，于是这个客户端随后收到 `ConnectionReset`/`BrokenPipe`。这类错误反映的是「测试收尾时的竞态」，而非被测逻辑本身的 bug。若不过滤，测试会因这种与被测功能无关的收尾错误而假阳性失败。`exclude_deadconn` 把它们一律视作 `Ok(())`。

**练习 2**：`ChokeGuard` 为什么持有 `Weak<ChokeInner>` 而不是 `Arc`？

**答案**：若持有 `Arc`，只要还有一个未释放的 guard 在某线程里，`Choke`（也是 `Arc<ChokeInner>`）就永远不会被释放，形成循环。用 `Weak` 让 guard 不阻止 `Choke` 释放：`Drop` 时 `upgrade()`，若 `Choke` 还在就计数减一并唤醒等待者，若 `Choke` 已被释放（`upgrade` 返回 `None`）则什么都不做。

**练习 3**：为什么异步 `test_wrapper`（[tests/util/tokio.rs:14-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/util/tokio.rs#L14-L22)）用的是 `current_thread` 运行时，而不是多线程运行时？

**答案**：测试里需要精确控制任务的调度顺序（例如 `off_runtime_drop` 测试用 `task::yield_now()` 来交错 server/client 的执行，见 4.4.3）。`current_thread` 运行时让任务在调用线程上协作式调度，行为确定、可预测；多线程运行时会把任务撒到线程池，调度顺序不可控，破坏这类测试的前提。

### 4.4 边界场景与测试矩阵（最小模块：tests）

#### 4.4.1 概念说明

「正常收发能通」只是测试的一部分；IPC 库更要测**异常路径**：连一个不存在的服务端会怎样？服务端等一个永不出现的客户端会怎样？收发超时会怎样？interprocess 把这些放进 `tests/local_socket/` 下的 `no_server`、`no_client`、`timeout`，并配合 `stream`（正常双向通信）。

同时，很多行为在「文件系统路径名」与「命名空间名」两种命名方式下都要验证。为了避免为每种命名各手写一份测试，interprocess 用一个 `tests!`/`matrix!` 宏，把一个测试函数在 `path: bool` 两个取值上展开成两个独立的 `#[test]` 函数。

#### 4.4.2 核心流程

边界用例各自的判定逻辑：

| 场景 | 触发方式 | 期望错误 `kind()` |
| --- | --- | --- |
| `no_server` | 客户端 `connect` 一个没人监听的名字 | `NotFound` 或 `ConnectionRefused` |
| `no_client` | 服务端以非阻塞 `Accept` 模式 `accept` | `WouldBlock` |
| `timeout` | 连接后设极短收发超时再读/写 | `WouldBlock` 或 `TimedOut` |
| `stream`（正常） | 服务端 `incoming().take(N)` 接 N 连接，双向收发两轮 | 无错误，且消息内容匹配 |

`tests!` 宏把同一函数在 `path = true`（文件路径名）与 `path = false`（命名空间名）上展开，生成 `xxx_file` 与 `xxx_namespaced` 两个测试。

#### 4.4.3 源码精读

边界用例 1——客户端连不存在的服务端：

- [tests/local_socket/no_server.rs:12-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/no_server.rs#L12-L29)：`run_and_verify_error` 调 `client`，断言连接必然失败；第 18-22 行用 `ensure!` 校验错误 `kind()` 落在 `NotFound | ConnectionRefused`。这与 u3-l2 讲过的「对端不存在时 `connect()` 立即硬失败」互为印证——`wait_mode` 此时不起作用。

边界用例 2——服务端等不到客户端：

- [tests/local_socket/no_client.rs:12-35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/no_client.rs#L12-L35)：用 `ListenerOptions::new().nonblocking(ListenerNonblockingMode::Accept)`（承接 u3-l1、u9-l2 的四态非阻塞模式）创建监听器，然后 `listener.accept()`，第 18-22 行断言得到 `WouldBlock`。

边界用例 3——收发超时：

- [tests/local_socket/timeout.rs:15-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/timeout.rs#L15-L40)：先用 `ConnectWaitMode::Timeout(1ms)` 连接，再 `set_recv_timeout(200µs)` / `set_send_timeout(200µs)`，然后 `read_exact` 一个永远不会有数据到达的缓冲，断言得到 `WouldBlock | TimedOut`。注意这个测试被 `#[cfg(not(windows))]` 门控（见 [tests/local_socket.rs:58-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket.rs#L58-L62)），因为 u3-l3/u9-l2 已说明 Windows named pipe 后端对收发超时恒返回 `Unsupported`。

正常双向通信用例：

- [tests/local_socket/stream.rs:45-83](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/stream.rs#L45-L83)：`server` 用 `listener.incoming().take(num_clients)` 驱动主循环（承接 u3-l1）；`handle_client`/`client` 各自切换 `set_nonblocking(true/false)` 验证阻塞模式切换，并用 `fork`（线程作用域里两侧并发）做「一端先发一端先收」的两轮收发。第 27-43 行 `check_peer_creds` 还顺带验证了 u3-l4 的对端凭据：`pid()` 等于本进程 id，Unix 下 `euid()/egid()` 等于 `libc::geteuid()/getegid()`。

矩阵宏——把一个函数展开成两个测试：

- [tests/local_socket.rs:33-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket.rs#L33-L62)：`tests!` 宏接收「函数名 + 一组 `名字 路径值` 对」，为每对生成一个 `#[test] fn $nm() -> TestResult { test_wrapper(|| $fn(make_id!(), $path)) }`。于是 `tests! {test_stream stream_file true stream_namespaced false}` 展开成 `stream_file`（`path=true`）与 `stream_namespaced`（`path=false`）两个测试，分别覆盖文件路径名与命名空间名。`no_server`/`no_client`/`timeout` 同理各展开两个。

平台专有用例也用类似矩阵。以 Windows named pipe 为例：

- [tests/os/windows/named_pipe.rs:19-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/named_pipe.rs#L19-L51)：`matrix!` 宏在 `bytes/msg`（模式）× `duplex/cts/stc`（方向）三个维度上展开成 6 个测试，覆盖 u4-l3 讲过的「模式 × 方向」矩阵。其中 `msg.rs` 里能看到 `reunite` 的真实用法（见下文综合实践引用）。

#### 4.4.4 代码实践

**目标**：读懂边界用例如何断言，并尝试微调一个断言观察失败报告。

**操作步骤**：

1. 阅读 `tests/local_socket/no_server.rs`，注意它只断言错误 `kind()`，不关心错误消息细节。
2. 运行 `cargo test --lib no_server_file -- --nocapture`，确认通过。
3. **仅作本地实验（不改提交）**：临时把 `no_server.rs` 第 19 行的 `NotFound | ConnectionRefused` 改成一个不可能匹配的 `UnexpectedEof`，再跑同一个测试，观察 `eyre` 报告里如何用 `ensure!` 的消息指出「期望 NotFound 或 ConnectionRefused，实际收到 …」。**实验后务必还原。**

**需要观察的现象**：步骤 3 会失败，报告清楚指出实际收到的错误类型。

**预期结果**：`ensure!` 的断言失败信息可读、定位准确。

> 说明：步骤 3 涉及临时改测试源码，验证后必须还原；若不便修改，可改为「源码阅读型实践」：对照 `no_server.rs` 与 `no_client.rs`，口述二者各自期望的 `kind()` 及其语义来源。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `timeout` 测试要被 `#[cfg(not(windows))]` 门控，而 `no_server`/`no_client` 不用？

**答案**：因为收发超时（`set_recv_timeout`/`set_send_timeout`）在 Windows named pipe 后端恒返回 `Err(Unsupported)`（u3-l3、u9-l2），无法在 Windows 上测出真正的 `TimedOut` 行为，所以该测试只在非 Windows 跑。而 `no_server`（连不存在的服务端 → `NotFound`/`ConnectionRefused`）与 `no_client`（非阻塞 `accept` → `WouldBlock`）在两个平台上行为一致，无需门控。

**练习 2**：`tests!` 宏生成的两个测试（如 `stream_file` 与 `stream_namespaced`）是独立的 `#[test]` 函数，这样做相比「一个测试里跑两次 `path`」有什么好处？

**答案**：独立测试函数意味着它们各自有独立的失败隔离与报告——一个失败不会牵连另一个，且报告中能直接看到是哪种命名方式失败。此外它们的 `make_id!()`（`file!/line!/column!`）不同，种下的 `NameGen` 种子也不同，互不干扰。

## 5. 综合实践

综合本讲内容，请为一个**新场景**补充一个跨平台集成测试：**验证「把来自不同 Stream 的两个半边 reunite 会失败并返回 `ReuniteError`，且归还两半所有权」**。

背景：u3-l3 讲过 `Stream::split` 把流拆成 `RecvHalf`/`SendHalf`，`reunite` 用 `Arc::ptr_eq` 判同源，不同源的半边 reunite 会返回 `ReuniteError`（其 `pub rh`/`sh` 字段原样归还两半）。目前仓库里 `reunite` 的**成功**路径已有覆盖（Windows named pipe 的 [tests/os/windows/named_pipe/msg.rs:44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/named_pipe/msg.rs#L44) 与 [msg.rs:127](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/named_pipe/msg.rs#L127) 里 `DuplexPipeStream::reunite(recver, sender).opname("reunite")?`），但**失败**路径（跨流 reunite）尚无跨平台测试。下面是建议的实现思路（**示例代码，非仓库已有代码**）：

1. **确定放置位置**：这是跨平台的 local socket 行为，应放在 `tests/local_socket/` 下新建一个子模块，例如 `tests/local_socket/reunite_error.rs`，并在 `tests/local_socket.rs` 顶部 `mod reunite_error;` 声明，再用 `tests!` 宏在 `path` 两值上展开（参考 4.4.3 的矩阵写法）。
2. **复用 util 抽象**：用 `namegen_local_socket` + `listen_and_pick_name` 取名字、建监听器；用 `Stream::connect` 建两条**独立**连接 `s1`、`s2`；分别 `s1.split()` 与 `s2.split()`，取出 `s1` 的 `RecvHalf` 与 `s2` 的 `SendHalf`，调用 `reunite`。
3. **断言**：参考 `no_server.rs` 的写法，断言 `reunite` 返回 `Err`，并从返回的 `ReuniteError` 的 `rh`/`sh` 字段取回两半（验证所有权被归还）。
4. **错误处理**：用 `test_wrapper` 包裹，IO 操作用 `.opname(...)` 标注，整体返回 `TestResult`。

示例骨架（**示例代码**）：

```rust
// 示例代码：tests/local_socket/reunite_error.rs（仓库中尚不存在）
use {
    crate::{
        local_socket::{prelude::*, ListenerOptions, Stream, Name},
        tests::util::*,
    },
    color_eyre::eyre::ensure,
};

pub fn run(id: &str, path: bool) -> TestResult {
    let (name, listener) =
        listen_and_pick_name(&mut namegen_local_socket(id, path), |nm| {
            ListenerOptions::new().name(nm.borrow()).create_sync()
        })?;
    // 监听器需要存活到两个连接都建立之后
    let s1 = Stream::connect(name.borrow()).opname("connect 1")?;
    let s2 = Stream::connect(name.borrow()).opname("connect 2")?;

    let (recv1, _send1) = s1.split();
    let (_recv2, send2) = s2.split();

    // 来自不同 Stream 的两半，reunite 必然失败
    let err = match Stream::reunite(recv1, send2) {
        Ok(_) => bail!("reunite of mismatched halves unexpectedly succeeded"),
        Err(e) => e,
    };
    // ReuniteError 的 pub 字段归还两半所有权
    let (_recv1_back, _send2_back) = (err.rh, err.sh);
    drop(listener);
    Ok(())
}
```

随后在 `tests/local_socket.rs` 里展开（**示例代码**）：

```rust
// 示例代码：追加到 tests/local_socket.rs 的 tests! 矩阵
tests! {reunite_error::run
    reunite_error_file       true
    reunite_error_namespaced false
}
```

**实践要点**：

- `split`/`reunite` 是消费式的，在枚举派发层手写 `match`（u3-l3），故不能复用 `dispatch!`，测试时要注意所有权移动顺序。
- `reunite` 的接收/发送半边顺序要与 `split` 返回的顺序一致（`reunite(recv, send)`），否则编译不过——这本身就是所有权类型系统的保护。
- 若你只读不写，可以把这个任务当成「源码阅读型实践」：跟踪 `Stream::reunite` 在 [src/local_socket/stream/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs) 里的手写 `match`，确认它如何把后端的 `ReuniteError` 经 `convert_halves`（u7-l3）桥接成公共 `ReuniteError`，再据此写出上面的断言。

> 说明：上面是建议的实现思路与示例骨架，未经编译验证；实际补测试时请以本地 `cargo test` 通过为准，并遵循仓库既有 lint 风格。

## 6. 本讲小结

- interprocess 打破 Cargo 惯例：`Cargo.toml` 设 `autotests = false`，再用 `src/lib.rs` 的 `#[cfg(test)] #[path = "../tests/index.rs"] mod tests` 把整个 `tests/` 挂成库的**内部模块**，使测试能访问 `pub(crate)` 私有项，且只产出一个测试二进制。
- `tests/index.rs` 是聚合根，用 `#[cfg]` 互斥编译 `os::unix`/`os::windows` 平台专有测试，用 `#[cfg(feature = "tokio")]` 门控异步测试。
- `tests::util` 用确定性 PRNG（`Xorshift32`，种子来自 `make_id!` 的源码位置）+ 无限 `NameGen` + `listen_and_pick_name` 跳过撞名，兼顾「不撞名」与「可复现」。
- 编排工具 `drive_pair`/`drive_server_and_multiple_clients` 提供标准的「服务端 + 首客户端 + N 并发客户端」拓扑，`Choke`/`Semaphore` 限流，`wdt` 看门狗 2 秒兜底，`exclude_deadconn` 容忍收尾竞态。
- 错误统一为 `TestResult`（`eyre::Result`），`opname` 给每步 IO 打标签，`color_eyre` 一次性安装；异步层是同步层的镜像（`current_thread` 运行时 + `task::spawn` + `try_join!`）。
- 边界用例 `no_server`/`no_client`/`timeout` 分别断言 `NotFound|ConnectionRefused`、`WouldBlock`、`WouldBlock|TimedOut`，并用 `tests!`/`matrix!` 宏在「文件路径名 / 命名空间名」等维度上展开成多个独立测试。

## 7. 下一步学习建议

- 顺着 u9-l4（平台探测与二次开发扩展点），思考「新增一个平台后端时，测试矩阵要补哪些维度」——你会更理解 `tests!`/`matrix!` 宏设计的扩展意义。
- 阅读 `tests/os/unix/local_socket/try_overwrite.rs` 与 `tests/os/unix/local_socket/fake_ns.rs`，体会平台专有行为（u3-l1 的 `try_overwrite`、u2-l4 的 `SpecialDirUdSocket` 伪命名空间）是如何被针对性测试的。
- 回到 u7-l3（错误处理体系），把本讲里 `exclude_deadconn`、`opname`、`ensure!` 对 `io::Error`/`eyre::Error` 的使用，与 `ConversionError` 的三段式设计对照，理解测试侧错误处理与库侧错误处理如何衔接。
- 若你想贡献测试，参照第 5 节的综合实践，挑一个尚未覆盖的失败路径（如 `take_error`、`set_nonblocking` 切换的极端时序）补一个跨平台用例，并复用本讲讲过的 `util` 抽象。
