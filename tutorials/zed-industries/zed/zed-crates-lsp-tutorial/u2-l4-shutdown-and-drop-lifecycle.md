# shutdown 关闭流程与 Drop 自动善后

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LSP 规范规定的**两步关闭**：先发 `shutdown` 请求（有 id、等响应），再发 `exit` 通知（无 id、不等响应），并解释为什么必须分两步。
2. 逐行读懂 [`LanguageServer::shutdown`](#41-shutdown-双定时器竞争与四类结果分支)：它如何在 `SERVER_SHUTDOWN_TIMEOUT`（5 秒）的限制下，用 `select!` 让 Shutdown 请求与外层定时器竞争，并区分四类请求结果分支与一个定时器分支。
3. 讲清楚**收尾管线**的完整链路：清空响应表 → 发 Exit 通知 → 关闭通知通道 → 通道级联排空 → `output_done` barrier 点亮 → kill 子进程，以及 barrier 为什么能保证「Exit 先落盘、进程后被杀」。
4. 解释 [`Drop for LanguageServer`](#43-drop-for-languageserver自动善后幂等与工程意义) 如何用 `Option::take` 实现「自动善后 + 幂等」，以及应用层（project、copilot）为什么还要显式 `await` 关闭 future。

## 2. 前置知识

### 2.1 LSP 的两步关闭协议

上一讲（u2-l3）里客户端与服务器完成了 `initialize` 握手，开始正常工作。会话结束时，LSP 规定了一个和握手对称的**两步关闭**流程：

1. 客户端发送 **`shutdown` 请求**（有 `id`，需要服务器响应）。语义是「请停止处理新请求，做清理（刷新缓存、释放索引、写临时文件……）」，服务器清完后返回一个空响应。
2. 客户端收到响应（或等不下去）后，发送 **`exit` 通知**（无 `id`，不需要响应）。语义是「可以退出了」，服务器收到后**退出进程**。

为什么分两步？因为「清理」可能耗时，客户端需要一个**确认点**（shutdown 的响应）知道服务器已经安全收尾；而 `exit` 是通知，客户端不等服务器死掉就可以继续自己的流程。反过来，如果客户端在 shutdown 响应到来之前就杀进程（或者根本不发 exit 直接杀），服务器就没机会做清理——语言服务器通常持有磁盘索引、缓存数据库（例如 rust-analyzer 的持久化索引），粗暴击杀可能留下脏数据。

但「等响应」又引入新风险：**服务器可能永远不回**（卡死、bug、死循环）。所以客户端必须带超时，超时后不再等待、直接走 exit + kill 路径。这就是本讲主角 `SERVER_SHUTDOWN_TIMEOUT`（5 秒）存在的原因。

### 2.2 关闭为什么是个难题：三件必须按序完成的事

把关闭流程拆开，其实是三件有严格顺序要求的事：

1. **Exit 通知必须真正写到子进程的 stdin 里**（经过 `BufWriter` flush、OS 管道），而不是只塞进内存通道；
2. **之后**才能 kill 子进程——否则 Exit 可能还躺在缓冲区里，进程已经死了；
3. **无论服务器是否配合**（响应 / 超时 / 报错 / 连接断开），流程都要走完，否则进程泄漏。

第 1、2 点的顺序保证靠一个 **barrier（栅栏）**：`postage` crate 提供的一次性「完成信号」通道——`barrier::channel()` 返回 `(Sender, Receiver)`，当 `Sender` 被 drop 时，`Receiver` 的 `recv()` 才完成。写出任务持有 Sender，关闭流程 await Receiver，就形成了「写出任务收尾 → 关闭流程继续」的同步点。

### 2.3 Rust 与异步前置知识

- **`select!`（来自 `futures` crate）**：多个 future 同时轮询，**谁先就绪谁赢**，其余 future 被 drop。参与 `select!` 的 future 必须先 `.fuse()`（防止重复 poll）。本讲中「Shutdown 请求 vs 5 秒定时器」的竞争就是它。
- **`Option::take()` 幂等模式**：把 `Option` 里的值一次性取走（原位置变 `None`）。第一次调用拿到 `Some`，之后所有调用都是 `None`——天然实现「只执行一次」，不需要额外的布尔标志。
- **`Drop` trait 不能是 async**：`fn drop(&mut self)` 是同步的，做不了 `.await`。惯用法是 `drop` 里 `spawn` 一个 async 任务并 `detach()`（不持有返回的 `Task`，让它跑到完），把异步收尾「发射」出去。
- **`async_channel` 的 close 排空语义**（u2-l2 已讲）：`sender.close()` 之后，接收端仍能把**已缓冲**的消息全部收完，然后 `recv()` 才返回 `Err`。这保证「先关通道、再排空」时**消息顺序与完整性不变**——关闭流程靠这个性质保证 Exit 排在所有未发通知之后。
- **GPUI 测试时钟**：`#[gpui::test]` 的 `TestAppContext` 使用模拟时钟，`executor.timer(5s)` 创建的定时器**不会随真实时间流逝而触发**，必须调用 `cx.advance_clock(Duration)` 推进模拟时钟。这是本讲实践任务的关键工具（见 [gpui/src/app/test_app.rs:196-204](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/test_app.rs#L196-L204)，该文件在 lsp crate 之外，链接使用仓库根相对路径）。

### 2.4 与前两讲的衔接

- u2-l2 介绍了 `LanguageServer` 的字段与三路 IO 任务：本讲的 `io_tasks`、`output_done_rx`、`server` 三个字段都来自那里；
- u2-l3 的 `initialize` 按值接收 `self` 并返回 `Task<Result<Arc<Self>>>`——初始化完成后调用方持有的都是 `Arc<LanguageServer>`，**最后一个 `Arc` 被 drop 的那一刻**就是本讲 `Drop` 实现的触发时机。

## 3. 本讲源码地图

本讲只涉及一个源码文件的不同区域，外加两个实践所需的支撑位置：

| 位置 | 作用 |
|---|---|
| [src/lsp.rs:57-58](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L57-L58) | `SERVER_SHUTDOWN_TIMEOUT = 5s` 常量定义 |
| [src/lsp.rs:1110-1167](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1110-L1167) | `LanguageServer::shutdown`——本讲主角 |
| [src/lsp.rs:1750-1756](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1750-L1756) | `Drop for LanguageServer` 自动善后 |
| [src/lsp.rs:137-140](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L137-L140) | 结构体上的 `io_tasks` / `output_done_rx` / `server` 三个字段 |
| [src/lsp.rs:518-519](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L518-L519)、[582-591](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L582-L591)、[634-636](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L634-L636) | barrier 与 IO 任务在 `new_internal` 中的创建与装配 |
| [src/lsp.rs:742-776](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L742-L776) | `handle_outgoing_messages`——barrier 的「发射点」 |
| [src/lsp.rs:1450-1558](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1450-L1558) | `request_internal_with_timer`——Shutdown 请求的内部机制（超时、取消） |
| [src/lsp.rs:1614-1629](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1614-L1629) | `notify_internal`——Exit 通知的发送路径 |
| [src/lsp.rs:1833-1927](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1833-L1927) | `FakeLanguageServer`（实践工具，默认注册即回的 Shutdown handler） |
| [src/lsp.rs:2105-2189](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105-L2189) | `test_fake`——实践任务的样板 |
| [crates/project/src/lsp_store.rs:1328-1344](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L1328-L1344) | 应用层显式调用并 `await` 关闭 future 的真实例子 |

## 4. 核心概念与源码讲解

### 4.1 shutdown()：双定时器竞争与四类结果分支

#### 4.1.1 概念说明

`shutdown()` 做两件事：

1. **「取走」LanguageServer 的全部收尾资源**（IO 任务句柄、barrier 接收端），把一个借用 `&self` 的方法变成一个**不借用 self、可独立运行**的关闭 future；
2. 构造一个带 5 秒超时的 `shutdown` 请求 future，与一个 5 秒的外层定时器放进 `select!` 竞争——**最多等 5 秒**，无论服务器是否配合，流程都继续。

关键设计：**关闭 future 必须是 `'static + Send`**。因为它通常由 `Drop` 发射（`Drop` 时 `self` 马上就要被销毁了，future 绝不能借用 `self`），所以你会看到源码里把所有需要的句柄一个个 clone 出来、甚至把 `next_id` 快照进一个新的 `AtomicI32`——全部是为了和 `self` 解耦。

「双定时器」值得专门解释：请求内部有自己的 5 秒超时（timer #1，超时产生 `ConnectionResult::Timeout`），外层又有一个 5 秒定时器（timer #2）。两者都是 5 秒，但语义不同：timer #1 属于通用请求机制（u3-l3 会展开），负责在超时后**摘除自己的响应 handler** 并给出明确的 `Timeout` 终态；timer #2 是 shutdown 自己的兜底保险，保证即使请求 future 因为任何原因不结束，整个关闭 async 块也会在 5 秒处被定时器分支唤醒、继续走收尾。两个定时器几乎同时启动、几乎同时到期，谁先被 poll 到就谁赢——但这只影响日志走哪个分支，**不影响后续收尾动作**（收尾是无条件执行的）。

#### 4.1.2 核心流程

`shutdown()` 的执行过程（伪代码）：

```text
shutdown(&self) -> Option<Future>:
    tasks           = self.io_tasks.take()          # 已关闭则直接返回 None（幂等）
    （clone 一批 'static 句柄：response_handlers、outbound_tx、
      notification_tx、executor、server 子进程句柄、output_done）
    next_id         = 新 AtomicI32(快照 self.next_id)   # 不借用 self
    shutdown_request = request_internal::<Shutdown>(..., 超时=5s, 参数=())
    timer            = executor.timer(5s)

    返回 async 块:
        select!:
            请求先完成 → 按四类结果记日志（Timeout/ConnectionReset/Err/Ok）
            定时器先到 → 记 info 日志
        （无论哪个分支，都不 return，继续往下）
        清空 response_handlers           → 作废所有在途请求
        发送 Exit 通知（进序列化通道）
        关闭序列化通道                    → 触发排空级联
        await output_done barrier         → 等写出任务把 Exit 真正 flush 完
        kill 子进程（若存在）
        drop(tasks)                       → 取消残余 IO 任务
        返回 Some(())
```

#### 4.1.3 源码精读

先看常量定义——5 秒是给所有语言服务器（注释特意提到包括 Prettier、Copilot 这类内嵌服务）的统一关闭预算：

- [src/lsp.rs:57-58](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L57-L58) — 定义私有的 `SERVER_SHUTDOWN_TIMEOUT` 常量为 5 秒，仅在本 crate 内部使用。

`shutdown()` 的签名与「资源取走」段：

- [src/lsp.rs:1110-1119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1110-L1119) — `shutdown` 的文档注释说明它「发送 shutdown 请求并让 LanguageServer 准备被 drop」。第 1112 行 `self.io_tasks.lock().take()?` 一行完成两件事：拿到两个 IO 任务的所有权，并且**若已经被取过则整个方法立刻返回 `None`**（`?` 作用于 `Option`）——这就是幂等性的全部实现。第 1115 行把 `next_id` 的当前值快照进一个**新的** `AtomicI32`，因为原来的那个还住在 `self` 里，而 `self` 可能在 future 运行时已不存在。第 1119 行取走 barrier 接收端。

```rust
pub fn shutdown(&self) -> Option<impl 'static + Send + Future<Output = Option<()>> + use<> {
    let tasks = self.io_tasks.lock().take()?;
    // ……（clone 各句柄）
    let mut output_done = self.output_done_rx.lock().take().unwrap();
```

> 上面这段是从 [src/lsp.rs:1111-1119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1111-L1119) 摘录的关键行。第 1119 行的 `unwrap()` 为什么安全？因为 `io_tasks` 与 `output_done_rx` 在 `new_internal` 里是**同时**写入 `Some` 的（见 [src/lsp.rs:634-635](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L634-L635)），又只在 `shutdown` 里**同时**被 take：第 1112 行已经确认 `io_tasks` 是 `Some`，那么 `output_done_rx` 必然也是 `Some`。

接着构造 Shutdown 请求与外层定时器：

- [src/lsp.rs:1120-1133](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1120-L1133) — 注意这里是 **`Self::request_internal::<request::Shutdown>`（关联函数、静态调用）**，传入的全是 clone 出来的句柄，所以返回的 future 不借用 `self`。`request::Shutdown` 的参数和结果都是 `()`（来自 lsp_types），第 1127 行直接传 `()`。第 1126 行给这个请求自己的超时也是 `SERVER_SHUTDOWN_TIMEOUT`（timer #1）；第 1133 行再造外层定时器（timer #2）。

```rust
let shutdown_request = Self::request_internal::<request::Shutdown>(
    &next_id, &response_handlers, &outbound_tx,
    &notification_serializers, &executor,
    SERVER_SHUTDOWN_TIMEOUT,      // 请求自身的超时（timer #1）
    (),
);
// ……
let mut timer = self.executor.timer(SERVER_SHUTDOWN_TIMEOUT).fuse();  // timer #2
```

然后是本讲最核心的 `select!`：

- [src/lsp.rs:1137-1156](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1137-L1156) — Shutdown 请求与定时器竞争。请求分支把 `ConnectionResult` 的四种取值各自映射为一条日志；定时器分支只记一条 info。**五个分支没有一个会 `return`**——它们只是记录「服务器配合得怎么样」，随后流程无条件继续。

`ConnectionResult` 四类请求结果分支的含义（结合 [src/lsp.rs:1529-1556](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1529-L1556) 中 `request_internal_with_timer` 的 `select!` 可以看清各自来源）：

| 分支 | 含义 | 何时发生 | 日志级别 |
|---|---|---|---|
| `ConnectionResult::Timeout` | 请求自身的 5 秒超时（timer #1）先到 | 服务器 5 秒内没回响应 | `warn` |
| `ConnectionResult::ConnectionReset` | 响应通道被 `Canceled` | 响应表被整体清空（`response_handlers` 里的 oneshot sender 被 drop），例如写出任务已结束并触发了它的清理 defer（[src/lsp.rs:753-758](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L753-L758)） | `warn` |
| `ConnectionResult::Result(Err(e))` | 服务器回了 JSON-RPC error，或请求根本没发出去（stdin 通道已关） | 服务器拒绝关闭 / 通道已失效 | `error` |
| `ConnectionResult::Result(Ok(()))` | 服务器正常确认关闭 | 正常路径 | 无日志 |

补充一个精妙细节：如果 timer #2（外层）先赢，`shutdown_request` 这个 future 会被 `select!` **drop 掉**，而 `request_internal_with_timer` 内部有一个 `cancel_on_drop` 的 defer（[src/lsp.rs:1516-1526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1516-L1526)），它会在 future 被 drop 时补发一条 `$/cancelRequest` 通知（参数是本次 Shutdown 请求的 id）。所以「外层定时器先赢」的路径里，fake 服务器可能先收到一条 `$/cancelRequest`、再收到 `exit`——这是 u3-l3（取消机制）的伏笔。

#### 4.1.4 代码实践

**实践目标**：亲手复现 `test_fake` 结尾的关闭断言——`drop(server)` 之后，fake 服务器必须收到 `exit` 通知。

**操作步骤**（以下均为示例代码，需你在本地分支的 `src/lsp.rs` 底部 `mod tests`（[src/lsp.rs:2095](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2095)）中临时添加，练习后可还原）：

```rust
#[gpui::test]
async fn test_drop_sends_exit_notification(cx: &mut TestAppContext) {
    cx.update(|cx| {
        release_channel::init(semver::Version::new(0, 0, 0), cx);
    });
    let (server, mut fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "the-lsp".to_string(),
        Default::default(),
        &mut cx.to_async(),
    );

    let server = cx
        .update(|cx| {
            let params = server.default_initialize_params(false, false, cx);
            let configuration = DidChangeConfigurationParams {
                settings: Default::default(),
            };
            server.initialize(params, configuration.into(), DEFAULT_LSP_REQUEST_TIMEOUT, cx)
        })
        .await
        .unwrap();

    // FakeLanguageServer::new 内部已经为 request::Shutdown 注册了
    // 立即返回 Ok 的 handler（src/lsp.rs:1924），无需再注册。
    drop(server);            // 触发 Drop -> 发射关闭 future
    cx.run_until_parked();   // 让 detached 的关闭任务跑完
    fake.receive_notification::<notification::Exit>().await;
}
```

运行：`cargo test -p lsp test_drop_sends_exit_notification`。

**需要观察的现象**：

- `drop(server)` 是同步的、立即返回的——真正的关闭动作发生在 `run_until_parked` 驱动的后台任务里；
- 若在 `drop` 与 `run_until_parked` 之间调用 `fake.try_receive_notification::<notification::Exit>()`，多半收不到（关闭任务还没跑）——可以试着断言体会时序。

**预期结果**：测试通过，`receive_notification::<notification::Exit>()` 拿到 `()` 参数的 Exit 通知。如果测试卡死在 `receive_notification` 上，说明 `run_until_parked` 没能把关闭任务驱动到发出 Exit 的那一步——回头检查是否漏了调用它。（以上行为待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：把第 1126 行传给 `request_internal` 的超时改成 `Duration::MAX`（表示请求永不超时，见 [src/lsp.rs:1586-1592](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1586-L1592)），`select!` 的行为会有什么变化？

**答案**：Shutdown 请求自身永远不会超时，竞争只剩外层 timer #2。服务器不响应时，5 秒后由**定时器分支**（info 日志）结束等待，而不是请求分支的 `ConnectionResult::Timeout`（warn 日志）。总时长不变（仍是 5 秒），只是走不同的日志分支、且不会有「摘除响应 handler」的请求内部清理（改由第 1158 行的整体清空兜底）。

**练习 2**：为什么第 1115 行要 `AtomicI32::new(self.next_id.load(SeqCst))` 新建一个原子量，而不是直接借用 `&self.next_id`？

**答案**：返回的关闭 future 要求 `'static`（签名里写明），最常见的调用方式是 `Drop` 里 `executor.spawn(shutdown).detach()`——future 开始运行时 `self` 已经被销毁了。所有依赖都必须 clone/快照进 future，`next_id` 也不例外。

**练习 3**：`select!` 的五个分支里为什么没有一个提前 `return`？

**答案**：关闭是「必须完成」的动作。服务器超时、断连、报错只影响日志记录，不能阻止后续的 Exit 通知与进程 kill；如果任何一个分支提前返回，就会走「温和等待失败 → 放弃收尾 → 进程泄漏」的坏路径。

### 4.2 收尾管线：Exit 通知、通道级联与 output_done barrier

#### 4.2.1 概念说明

`select!` 结束只是「等完了」，真正的关闭动作在它**后面**的五步收尾里。这一段要解决的核心问题是 2.2 节提出的顺序约束：**Exit 帧必须先被 flush 进子进程的 stdin，然后才能 kill**。

难点在于：调用方（关闭 future）并不直接写 stdin——通知要先经过「序列化通道 → 序列化泵 → outbound 通道 → 写出任务」这条 u2-l2 讲过的两级管线。调用方怎么知道写出任务真的把 Exit 写完了？答案是 **`output_done` barrier**：

- `new_internal` 里 `barrier::channel()` 创建一对 (Sender, Receiver)；
- **Sender 被 move 进写出任务** `handle_outgoing_messages`，任务自然结束时 drop 它；
- **Receiver 存在 `LanguageServer.output_done_rx` 字段里**，`shutdown()` 取走并在收尾时 `await`。

于是「`output_done.recv()` 返回」等价于「写出任务已退出循环、所有帧（包括 Exit）都已 `flush`」——这是 kill 之前的安全同步点。

#### 4.2.2 核心流程

收尾管线的完整链路（严格按执行顺序）：

```text
select! 结束（无论哪个分支）
  │
  ├─ ① response_handlers.lock().take()     整体清空响应表
  │      └─ 所有在途请求的 oneshot sender 被 drop
  │         → 那些请求的 rx 收到 Canceled → 以 ConnectionReset 终止
  │
  ├─ ② notify_internal::<Exit>(())          Exit 的「序列化闭包」进入 notification 通道
  │
  ├─ ③ notification_tx.close()              关闭序列化通道（不再接收新通知）
  │      └─ 序列化泵任务排空缓冲：逐个执行闭包 → 结果转发进 outbound 通道
  │         → 排空后 outbound_tx.close()
  │
  ├─ ④ 写出任务排空 outbound：写 Content-Length 帧 + flush
  │      → 通道关闭且排空 → 循环退出 → drop(output_done_tx)   ★ barrier 点亮
  │
  ├─ ⑤ output_done.recv().await 返回        确认 Exit 已落盘
  │
  ├─ ⑥ server.lock().take() → child.kill()  杀掉子进程（fake 场景为 None，no-op）
  │
  └─ ⑦ drop(tasks)                          取消可能仍在跑的输入任务
```

注意 ③④ 依赖 `async_channel` 的关键性质：**`close()` 之后接收端仍会先把缓冲里的消息收完才报错**。所以「先关通道再排空」不会丢消息，Exit 一定排在之前所有通知之后按序写出。

#### 4.2.3 源码精读

收尾五连（一段 6 行的代码浓缩了上面整张图）：

- [src/lsp.rs:1158-1163](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1158-L1163) — 依次完成「清响应表 → 发 Exit → 关通知通道 → 等 barrier → kill 子进程 → drop 任务」。

```rust
response_handlers.lock().take();
Self::notify_internal::<notification::Exit>(&notification_serializers, ()).ok();
notification_serializers.close();
output_done.recv().await;
server.lock().take().map(|mut child| child.kill());
drop(tasks);
```

Exit 通知的发送路径——和普通通知完全一致，走「序列化闭包」通道：

- [src/lsp.rs:1614-1629](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1614-L1629) — `notify_internal` 把 `Notification { method: "exit", params: () }` 的 **serde 调用打包成 `NotificationSerializer` 闭包**塞进通道，而不是当场序列化。这既避免在调用线程做序列化，也保证 Exit 与其他通知共用一条排队路径、维持全局顺序（u3-l1 会展开这个设计）。

序列化泵——通道级联关闭的「中继站」：

- [src/lsp.rs:598-612](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L598-L612) — `new_internal` 里 spawn 的后台任务：循环取出闭包、执行、把序列化结果转发进 outbound 通道；通道关闭并排空后循环退出，最后调用 `outbound_tx.close()` 把「关闭」信号接力传给写出任务。

写出任务——barrier 的发射点：

- [src/lsp.rs:760-775](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L760-L775) — 循环里每收到一条消息：记 trace 日志、通知 `io_handlers`（LSP 日志面板数据源）、按 `Content-Length` 分帧写出并 `flush`（第 772 行——Exit 就是这里真正到达子进程 stdin 的）。`outbound_rx.recv()` 返回 `Err`（通道关闭且已排空）后退出循环，第 774 行 `drop(output_done_tx)` **点亮 barrier**。

```rust
while let Ok(message) = outbound_rx.recv().await {
    // ……分帧写出……
    stdin.flush().await?;
}
drop(output_done_tx);   // ← barrier 发射点：此后 output_done.recv() 才会返回
```

barrier 与字段的装配：

- [src/lsp.rs:518-519](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L518-L519) — `new_internal` 创建 outbound 通道与 `barrier::channel()`；
- [src/lsp.rs:582-591](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L582-L591) — 写出任务被 spawn，`output_done_tx`（Sender 端）随参数 move 进任务；
- [src/lsp.rs:137-140](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L137-L140) — 结构体字段：`io_tasks`（两个 IO 任务的句柄，`shutdown` 取走后 drop 即取消）、`output_done_rx`（barrier 接收端）、`server`（`Arc<Mutex<Option<Child>>>` 包装的子进程）。

kill 与双保险：

- [src/lsp.rs:1162](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1162) — 显式 kill：把 `Child` 从 `Option` 里取走并 `kill()`。对 fake 而言这里传的是 `None`（见 [src/lsp.rs:1860-1874](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1860-L1874)，`FakeLanguageServer::new` 给 `new_internal` 的第 7 个参数——子进程——传 `None`），所以是 no-op；
- [src/lsp.rs:454-461](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L454-L461) — u2-l1 讲过的 `kill_on_drop(true)` 在这里是**第二道保险**：万一某个路径让 `Child` 被 drop 而没有走显式 kill（例如关闭 future 被丢弃后结构体销毁），进程仍会被杀掉，不会泄漏。

#### 4.2.4 代码实践

**实践目标**：验证「服务器不配合（Shutdown 请求永远不返回）时，超时分支仍会发出 Exit 并走完收尾管线」。

**操作步骤**（示例代码，同样放在 `mod tests` 中）：

```rust
#[gpui::test]
async fn test_shutdown_timeout_still_sends_exit(cx: &mut TestAppContext) {
    cx.update(|cx| {
        release_channel::init(semver::Version::new(0, 0, 0), cx);
    });
    let (server, mut fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "the-lsp".to_string(),
        Default::default(),
        &mut cx.to_async(),
    );

    // 覆盖默认 handler：让 Shutdown 请求永远不返回。
    fake.set_request_handler::<request::Shutdown, _, _>(|_, _| {
        async { std::future::pending::<Result<()>>().await }
    });

    let server = cx
        .update(|cx| {
            let params = server.default_initialize_params(false, false, cx);
            let configuration = DidChangeConfigurationParams {
                settings: Default::default(),
            };
            server.initialize(params, configuration.into(), DEFAULT_LSP_REQUEST_TIMEOUT, cx)
        })
        .await
        .unwrap();

    drop(server);
    cx.run_until_parked();                            // 关闭任务启动，挂在 select! 上等定时器
    cx.advance_clock(Duration::from_secs(5));         // 推进模拟时钟 5 秒（SERVER_SHUTDOWN_TIMEOUT）
    cx.run_until_parked();                            // 超时分支 + 收尾管线跑完
    fake.receive_notification::<notification::Exit>().await;
}
```

运行：`cargo test -p lsp test_shutdown_timeout_still_sends_exit`。

**需要观察的现象**：

1. 没有 `advance_clock` 时测试会**卡死**——测试时钟不走，两个 5 秒定时器永远不响（这正是 GPUI 测试确定性的体现）；
2. 超时路径下 Exit **依然**送达 fake——「等不到服务器的确认」不等于「放弃关闭」；
3. 视察 fake 收到的消息顺序：**可能**先看到一条 `$/cancelRequest`（若外层 timer #2 先赢，Shutdown 请求 future 被 drop，触发 [src/lsp.rs:1516-1526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1516-L1526) 的取消 defer），然后才是 `exit`。由于 `receive_notification` 会跳过不匹配的方法（[src/lsp.rs:1982-1993](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1982-L1993)），断言不受影响；想精确观察顺序可以改用 `try_receive_notification` 逐步取。

**预期结果**：测试通过。日志中应出现 `timeout waiting for language server ... to shutdown`（warn 或 info，取决于哪个定时器先赢；测试中日志是否可见取决于 `zlog::init_test()` 的配置，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 1161 行 `output_done.recv().await` 删掉、直接 kill，会有什么后果？

**答案**：kill 可能发生在 Exit 帧 flush 之前。Exit 此刻可能还躺在序列化通道、outbound 通道或 `BufWriter` 缓冲里，进程一死这些字节就没人写了——服务器收不到 Exit，没机会做清理就被击杀，两步关闭协议退化成粗暴击杀。barrier 把「写出任务收尾」变成 kill 的前置条件，正是为了封死这个竞态窗口。

**练习 2**：Exit 为什么走 `notify_internal`（序列化闭包通道，②③两步），而不是直接 `outbound_tx.send(json)` 一步到位？

**答案**：一是**顺序**——Exit 必须排在之前所有已排队通知之后发出，走同一条队列才能保证；二是**职责**——`shutdown` 的收尾代码不应自己拼 JSON 与分帧，复用 `notify_internal` 让「发通知」只有一个实现。至于「延迟序列化」的完整动机（避免阻塞前台线程），u3-l1 会专门展开。

**练习 3**：第 1158 行清空 `response_handlers` 之后，那些还没得到响应的在途请求会发生什么？

**答案**：它们的 oneshot sender 随表一起被 drop，等待端的 `rx` 收到 `Canceled`，在 `request_internal_with_timer` 的 `select!`（[src/lsp.rs:1536-1539](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1536-L1539)）里被映射为 `ConnectionResult::ConnectionReset`——调用方会看到「连接已重置」而非永远挂起。这保证关闭时不会留下悬挂的请求 future。

### 4.3 Drop for LanguageServer：自动善后、幂等与工程意义

#### 4.3.1 概念说明

Rust 的 RAII 惯用法：资源在 `Drop` 里释放，调用方「忘掉」释放也没关系。`LanguageServer` 把整个两步关闭挂进了 `Drop`——**只要最后一个 `Arc<LanguageServer>` 被 drop，优雅关闭就自动发生**。这在工程上非常重要：Zed 里语言服务器的引用散布在 editor、project、languages 等多个 crate，指望每一处都记得手动关闭不现实；Drop 兜底保证「不存在被遗忘的泄漏进程」。

但 `Drop` 有两个天然限制，决定了它的实现形状：

1. **`drop` 是同步的**，做不了 `.await` → 只能 `spawn` + `detach()`，把关闭 future 发射到后台执行；
2. **Drop 可能在已经手动关闭之后发生** → 需要幂等：`shutdown()` 靠 `io_tasks.lock().take()?` 已经保证第二次调用返回 `None`，Drop 里一句 `if let Some` 就天然跳过。

还有一个使用上的细节值得点破：`shutdown()` 返回的 future 是**惰性的**——构造它只是「取走资源、准备好关闭序列」，真正的动作（发 Shutdown 请求、发 Exit、kill）在 future 被 poll 时才发生。所以应用层如果想要**确定性的关闭完成点**（比如「等旧服务器完全退出后再启动新实例」），就要显式拿到这个 future 并 `await`；而 `Drop` 的自动路径则适合「我不在乎何时完成，别泄漏就行」。两层配合，各取所需。

#### 4.3.2 核心流程

```text
最后一个 Arc<LanguageServer> 被 drop
  │
  └─ Drop::drop(&mut self):
       shutdown = self.shutdown()          # 取资源构造关闭 future
       │
       ├─ 已手动关闭过 → io_tasks 是 None → shutdown() 返回 None → 什么都不做（幂等）
       │
       └─ Some(future) → executor.spawn(future).detach()
                          # 后台执行 4.1 + 4.2 的完整序列；
                          # detach = 不持有 Task 句柄，跑到完、不被中途取消
```

#### 4.3.3 源码精读

- [src/lsp.rs:1750-1756](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1750-L1756) — 整个 `Drop` 实现只有三行：调用 `shutdown()`，`Some` 时 spawn 并 detach。

```rust
impl Drop for LanguageServer {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown() {
            self.executor.spawn(shutdown).detach();
        }
    }
}
```

为什么 `detach()` 是必须的：`executor.spawn` 返回的 `Task` 若不被保存，drop 时会**取消**任务——关闭序列会被腰斩（可能 Exit 还没写出）。`detach()` 明确声明「这个任务独立运行到完成」。

应用层显式关闭的真实例子：

- [crates/project/src/lsp_store.rs:1328-1344](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L1328-L1344) — Zed 的 `LspStore::shutdown_server`：拿到 `server.shutdown()` 返回的 future 后 **`shutdown.await`**（还处理了服务器仍在启动中的 `Starting` 状态）。它需要「确定等到关闭完成」——例如项目关闭、语言服务器重启时，必须等旧实例收尾完再继续。copilot crate（[crates/copilot/src/copilot.rs:393](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/copilot/src/copilot.rs#L393) 附近）同样显式持有并 await 这个 future。

测试样板（本讲实践的依据）：

- [src/lsp.rs:2184-2188](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2184-L2188) — `test_fake` 的结尾三步：注册即回的 Shutdown handler → `drop(server)` → `cx.run_until_parked()` → 断言 fake 收到 `exit`。`run_until_parked` 正是驱动那个 detached 关闭任务的手段。

一个容易踩的坑（机制层面的事实，值得记住）：如果你**手动调用了 `shutdown()` 拿到 future，却把它直接丢掉不 poll**，关闭序列一步都不会执行——而 `Drop` 的兜底也不会再触发（`io_tasks` 已被取走）。所以「调用了 shutdown()」不等于「关闭已发生」，必须让那个 future 被 spawn/await。

#### 4.3.4 代码实践

**实践目标**：验证幂等性（第二次 `shutdown()` 返回 `None`）与「手动关闭后 Drop 不再重复发送 Exit」。

**操作步骤**（示例代码，放在 `mod tests` 中；样板取自 [src/lsp.rs:2105-2189](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105-L2189) 的 `test_fake`）：

```rust
#[gpui::test]
async fn test_manual_shutdown_is_idempotent(cx: &mut TestAppContext) {
    cx.update(|cx| {
        release_channel::init(semver::Version::new(0, 0, 0), cx);
    });
    let (server, mut fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "the-lsp".to_string(),
        Default::default(),
        &mut cx.to_async(),
    );

    let server = cx
        .update(|cx| {
            let params = server.default_initialize_params(false, false, cx);
            let configuration = DidChangeConfigurationParams {
                settings: Default::default(),
            };
            server.initialize(params, configuration.into(), DEFAULT_LSP_REQUEST_TIMEOUT, cx)
        })
        .await
        .unwrap();

    let manual = server.shutdown();
    assert!(manual.is_some());            // 第一次：拿到关闭 future
    assert!(server.shutdown().is_none()); // 第二次：io_tasks 已被取走 → None（幂等）

    manual.unwrap().await;                // 手动驱动完整关闭序列（fake 的 Shutdown
                                          // handler 立即返回 Ok，无需推进时钟）

    drop(server);                         // Drop 兜底：shutdown() 返回 None，不再重复
    cx.run_until_parked();
    fake.receive_notification::<notification::Exit>().await; // Exit 恰好送达一次
}
```

运行：`cargo test -p lsp test_manual_shutdown_is_idempotent`。

**需要观察的现象**：

- 第二次 `shutdown()` 返回 `None`，证明幂等不需要任何额外标志位；
- `manual.unwrap().await` 能在**不推进模拟时钟**的情况下完成——Shutdown 请求正常响应时 `select!` 走请求分支，5 秒定时器 future 直接被 drop；

**预期结果**：三条断言全部通过；`drop(server)` 之后不会出现第二份关闭动作（fake 端只收到一次 Exit）。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`Drop` 里如果写成 `let task = self.executor.spawn(shutdown);`（不 detach、把 Task 存在局部变量里），会发生什么？

**答案**：`drop` 函数返回时局部变量 `task` 被销毁，`Task` 的 drop 会**取消**任务——关闭序列刚被发射就被取消，Exit 可能没发出、进程可能没被杀（只剩 `kill_on_drop` 兜底）。`detach()` 的语义就是「放弃句柄、但让任务运行到完成」。

**练习 2**：既然 `Drop` 已经自动关闭了，`LspStore::shutdown_server` 为什么还要显式 `await`？

**答案**：两者用途不同。`Drop` 的自动路径是**尽力而为的兜底**：不保证何时完成，只保证「不泄漏」；而重启服务器、关闭项目这类场景需要**确定性的完成点**——必须等旧实例完全退出（Exit 已发、进程已 kill）才能启动新实例或回收资源。显式 `await` 关闭 future 才能建立这个顺序关系。

**练习 3**：`shutdown()` 的返回类型为什么是 `Option<impl Future>`，而不是直接 `impl Future`？

**答案**：`Option` 承担双重职责：`None` 表示「已经关闭过/正在关闭」（幂等信号，`Drop` 与双重调用都靠它短路）；`Some(future)` 把「关闭序列的所有权」交还给调用方——想等就 await，想发射就 spawn。若返回裸 future，就无法表达「无事可做」，幂等只能另想办法。

## 5. 综合实践

把本讲三个模块串成一个「关闭行为观察实验」。在 `mod tests` 里新建一个测试，同一个 fake 会话内依次完成三组观察（示例代码骨架，初始化部分与 4.1.4 相同，此处省略）：

```rust
// 场景 A：正常关闭——fake 的默认 Shutdown handler（src/lsp.rs:1924）立即返回 Ok。
//   drop(server) + run_until_parked → Exit 送达，select! 走 Result(Ok(())) 分支。

// 场景 B：服务器卡死——set_request_handler::<request::Shutdown> 返回
//   std::future::pending，drop 后 advance_clock(5s) + run_until_parked
//   → Exit 仍送达（超时不阻断收尾），并观察是否先收到 $/cancelRequest。

// 场景 C：手动关闭——initialize 后先 assert!(server.shutdown().is_some())、
//   再 assert!(server.shutdown().is_none())，await 第一个 future，
//   最后 drop(server) 验证 Drop 不再重复发 Exit。
```

具体要求：

1. 三个场景各自独立建一个测试函数（每个测试都新建 `FakeLanguageServer`，避免状态串扰）；
2. 场景 B 中务必体会「去掉 `advance_clock` 测试就挂起」——这是 GPUI 模拟时钟确定性的直接证据；
3. 全部用 `cargo test -p lsp shutdown_`（给三个测试统一加 `shutdown_` 前缀）一次跑通；
4. 完成后画出本讲的关闭时序图（drop → spawn → select! → 清表 → Exit → close → 排空 → flush → barrier → kill → drop tasks），在每一步标注对应源码行号，检验自己是否真正串起了 4.1–4.3。

预期三个测试全部通过；任何一步卡死，回到对应小节的「核心流程」图定位漏掉的环节。

## 6. 本讲小结

- LSP 关闭是**两步协议**：`shutdown` 请求（等确认，限时 5 秒）+ `exit` 通知（不等）+ kill；`SERVER_SHUTDOWN_TIMEOUT`（[src/lsp.rs:57-58](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L57-L58)）是这一切的时间预算。
- `shutdown()` 用 `select!` 让 Shutdown 请求（自带 timer #1）与外层定时器（timer #2）竞争，四类请求结果分支只影响日志；**收尾在 select 之后无条件执行**。
- 收尾管线五步：清响应表（在途请求以 `ConnectionReset` 终止）→ 经序列化通道发 Exit → 关通道触发排空级联 → **`output_done` barrier** 确认 Exit 已 flush → kill 子进程（`kill_on_drop` 是第二道保险）。
- barrier 的方向是「写出任务 drop Sender → 关闭流程 recv 返回」，保证 **Exit 先落盘、进程后被杀**的顺序。
- `Drop for LanguageServer` 三行实现自动善后：`shutdown()` 的 `Option` 返回值提供幂等，`spawn().detach()` 把异步收尾发射到后台；应用层（`LspStore::shutdown_server`）显式 `await` 同一个 future 以获得确定性的完成点。
- 测试层面：`drop` + `run_until_parked` 驱动正常关闭；卡死服务器的超时场景必须用 `cx.advance_clock` 推进模拟时钟。

## 7. 下一步学习建议

本讲是第二单元（服务器进程生命周期）的收官：至此你已经走完了「启动进程 → IO 管线 → initialize 握手 → shutdown 关闭」的完整一生。下一讲 **u3-l1《发送通知与延迟序列化》** 将深入本讲反复经过的那条「序列化闭包通道」：`notify` 如何泛型于 `Notification::METHOD`、`NotificationSerializer` 为什么要延迟序列化、两级通道如何维护消息顺序与级联关闭——Exit 通知正是沿着这条路径发出的，你在这里的疑问会在那里得到系统解答。之后再进入 u3-l2/u3-l3 的请求机制（`ConnectionResult`、`$/cancelRequest` 的完整故事）。若想提前看真实规模的使用方，推荐浏览 [crates/project/src/lsp_store.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs) 中所有 `server.shutdown()` 的调用点，体会应用层如何组合「显式 await」与「Drop 兜底」两种关闭姿势。
