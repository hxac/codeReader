# 测试 GPUI 应用：#[gpui::test] 与 TestAppContext

## 1. 本讲目标

学完本讲，你应该能够：

1. 会写 `#[gpui::test]` 测试：理解宏展开成什么、参数列表如何决定注入哪些上下文、种子（seed）与迭代次数如何让并发测试可复现。
2. 会用 `cx.executor()` 推进虚拟时钟：掌握 `timer` / `advance_clock` / `run_until_parked` / `allow_parking` 的语义，理解「测试里时间是被模拟的」这一核心设计。
3. 掌握输入模拟：用 `dispatch_action`、`simulate_keystrokes`、`simulate_click`、`simulate_event` 驱动窗口级交互，并理解为什么鼠标事件依赖上一帧的渲染结果。
4. 能搭建 headless 视觉测试与泄漏检测：了解 `HeadlessAppContext` 的真实字体/GPU 渲染路径，以及 `LeakDetector` 如何在测试结束时揪出泄漏的实体句柄。

## 2. 前置知识

本讲是第 7 单元的第四讲，默认你已完成以下认知（不重复展开）：

- **实体与上下文**（u2-l2 / u2-l3）：`Entity<T>` 是 App 拥有的状态句柄，读写要走 `update`/`read_with`；`Context<T>` 是 `&mut App` 加弱句柄；`cx.notify()` / `cx.emit()` 产生的是**效果（Effect）**，在「最外层 update 结束」后的 `flush_effects` 中派发。
- **Action 体系**（u5-l3）：动作是命名的用户意图，`actions!` 宏定义，`on_action(cx.listener(...))` 在 paint 阶段注册进下一帧的 DispatchTree，按键经 Keymap 匹配成动作后沿焦点路径派发。
- **绘制管线**（u4-l3）：`cx.notify()` 标脏窗口 → 平台帧回调调用 `Window::draw` 重建元素树；**hitbox 与动作监听器都登记在渲染结果里**，因此输入派发依赖「上一帧」。
- **测试平台**（u7-l1）：`TestPlatform` / `TestWindow` / `TestDispatcher` 是不跑真实事件循环的平台替身，`PlatformDispatcher::now()` 可被覆写是假时钟的根源。

两个本讲的新术语，先用一段话建立直觉：

- **确定性调度（deterministic scheduling）**：普通多线程测试的失败难以复现，因为任务执行顺序随 OS 心情变化。GPUI 的做法是把所有后台/前台任务收进一个 `TestScheduler`，由种子决定「随机」顺序——同一个种子永远得到同一种交错，失败时打印种子，`SEED=<种子>` 重跑即可精确复现。
- **parking（停泊）**：测试执行器默认**禁止**线程真正睡眠等待。如果你的测试 await 了一个 GPUI 管不到的东西（真实文件 IO、网络），调度器会发现「没有任务可跑但 future 未完成」并 panic——这被当成特性而非缺陷，用来检测潜在死锁；确实需要等外部系统时用 `allow_parking()` 显式豁免。

运行测试的标准命令（出自示例文件自身的文档头）：

```bash
# 运行示例里内嵌的测试（需要 test-support feature）
cargo test -p gpui --example testing --features test-support

# 运行 gpui 自身的全部测试（单元测试 + tests/ 下的集成测试）
cargo test -p gpui
```

`test-support` 不是普通 feature，它是一条开关链（见 4.5 节）：`test-support → leak-detection → backtrace`，同时放行 `app/test_context.rs`、`app/test_app.rs`、`app/headless_app_context.rs` 这三个 `#[cfg(any(test, feature = "test-support"))]` 门控的模块。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/test.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/test.rs) | `run_test` 驱动器：按种子循环执行测试体、重试、失败时提示 `SEED` 重跑；`#[gpui::test]` 宏的运行时一半 |
| [crates/gpui_macros/src/test.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs) | `#[gpui::test]` 过程宏：编译期一半，把测试函数包装成标准 `#[test]` 并生成上下文注入与拆卸代码 |
| [src/app/test_context.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs) | `TestAppContext` 与 `VisualTestContext`：本讲两大主角，实体级与窗口级测试 API |
| [src/app/test_app.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs) | `TestApp` / `TestAppWindow`：更新型的简化测试入口，update 后自动 run_until_parked |
| [src/app/headless_app_context.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs) | `HeadlessAppContext`：注入真实字体后端与可选 GPU 渲染器的跨平台 headless 上下文，支持截图 |
| [src/platform/test/dispatcher.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/dispatcher.rs) | `TestDispatcher`：包装 scheduler crate 的 `TestScheduler`，虚拟时钟与确定性调度的心脏 |
| [src/executor.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs) | `BackgroundExecutor` 的测试面：`timer` / `advance_clock` / `run_until_parked` / `block_test` |
| [src/app/entity_map.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs) | `LeakDetector`：实体句柄的创建/释放记账，测试收尾时对泄漏 panic |
| [examples/testing.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs) | 官方测试教学示例：同一份 Counter 代码既可运行也可测试，覆盖本讲所有模式 |
| [tests/action_macros.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/tests/action_macros.rs) | 集成测试样例：普通 `#[test]` 即可测 action 宏（无需 gpui 上下文） |

## 4. 核心概念与源码讲解

### 4.1 `#[gpui::test]` 宏：测试函数如何被生成

#### 4.1.1 概念说明

`#[gpui::test]` 解决的问题是：GPUI 的所有状态都活在一个 `App` 里，而 `App` 必须搭配执行器、平台、文本系统才能工作。这个宏让你只声明「我需要什么上下文」，剩下的事情——构建 `TestDispatcher`、造出 `TestAppContext`、跑完测试后的收尾（冲刷任务、退出 App、丢弃残留任务）——全部自动生成。

它的参数列表是一份「注入清单」：

| 参数类型 | 宏注入什么 |
| --- | --- |
| `cx: &mut TestAppContext` | 一个全新 `TestAppContext`（共享本次运行的 dispatcher） |
| `cx: &mut App` | 同上，但直接借出 `App` 的写锁（同步测试专用） |
| `mut rng: StdRng` | 用本次运行的种子初始化的随机数发生器 |
| `executor: BackgroundExecutor` | 共享 dispatcher 的后台执行器 |

同种类型的参数可以写多个（例如 `cx_a`、`cx_b`），用于模拟分布式系统——两个「应用」共享同一个调度器，`run_until_parked` 时随机决定先跑谁的任务。

#### 4.1.2 核心流程

宏展开后的同步测试大致是：

```text
#[test] fn 原名() {
    把原函数重命名为 __原名
    gpui::run_test(iterations, &[seeds], retries,
        |dispatcher, seed| {
            let mut cx_0 = TestAppContext::build(dispatcher.clone(), Some("原名"));
            __原名(&mut cx_0);                       // ← 你的测试体
            cx_0.run_until_parked();                  // 拆卸①：跑完剩余任务
            cx_0.update(|cx| { 禁止 parking; cx.quit(); });  // 拆卸②：关闭 App
            cx_0.run_until_parked();
            drop(cx_0);
            dispatcher.drain_tasks();                 // 拆卸③：丢弃被取消的任务
        }, on_failure)
}
```

`run_test` 再在外面套一层种子循环：

1. `calculate_seeds` 计算要跑哪些种子：默认 `{0}`；`iterations = N` 展开为 `0..N`；环境变量 `ITERATIONS` 可覆盖 N；环境变量 `SEED` 直接钉死种子（复现失败用）。
2. 每个种子：`TestDispatcher::new(seed)` → `catch_unwind(测试体)` → `scheduler.end_test()`。
3. panic 时重试至多 `retries` 次（默认 0），仍失败则打印 `failing seed` 与 `SEED` 提示后重新抛出。
4. 多种子运行时每次先打印 `seed = {seed}`，让你知道当前在验哪一种交错。

#### 4.1.3 源码精读

宏入口只做三件事：解析参数、把内层函数改名为 `__原名`、交给生成器：

- [crates/gpui_macros/src/test.rs:L95-L117](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs#L95-L117) —— `pub fn test`：解析 `Args`（`iterations` / `retries` / `seed` / `seeds` / `on_failure`，见 [L13-L93](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs#L13-L93)），把原函数改名为 `__原名`，再生成外层 `#[test]` 函数。

异步分支对每个 `&mut TestAppContext` 参数生成的注入代码：

- [crates/gpui_macros/src/test.rs:L163-L176](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs#L163-L176) —— 每个上下文参数都 `TestAppContext::build(dispatcher.clone(), Some(stringify!(原名)))`（同一 dispatcher，多个 App）；`_entity_refcounts` 拿到实体引用计数的 drop 凭证（泄漏检测要用，见 4.5）；拆卸段依次 `run_until_parked` → `forbid_parking + quit` → `run_until_parked` → `drop`。

- [crates/gpui_macros/src/test.rs:L185-L210](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs#L185-L210) —— 生成的 `#[test]` 外壳：调用 `gpui::run_test(...)`，异步测试体经 `ForegroundExecutor::new(exec).block_test(...)` 同步驱动（[src/executor.rs:L410-L430](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs#L410-L430) 的 `block_test` 允许测试自身调度的前台任务在阻塞期间继续推进，避免同会话死锁）。同步分支额外支持 `&mut App` 参数（[L226-L250](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/test.rs#L226-L250)），整个测试期间持有 `borrow_mut` 写锁。

运行时一半在 gpui 内：

- [src/test.rs:L95-L142](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/test.rs#L95-L142) —— `run_test`：按种子循环、`catch_unwind` 包裹测试体、失败重试、多种子时打印种子与 `SEED` 复现提示。
- [src/test.rs:L144-L190](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/test.rs#L144-L190) —— `calculate_seeds`：`ITERATIONS` / `SEED` 环境变量如何参与种子集合的计算（`SEED` 设置时忽略显式 seeds）。

- [src/test.rs:L1-L27](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/test.rs#L1-L27) —— 模块文档：明确「输出兼容 cargo test / cargo-nextest 等标准运行器」「可以要任意多个上下文来测试协作式界面」。

#### 4.1.4 代码实践

**实践目标**：亲手体验宏的注入与种子机制。

**操作步骤**：

1. 打开 [examples/testing.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs)，在 `mod tests` 里（[L215-L246](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L215-L246) 已有 `basic_testing` 可参照）新增：

   ```rust
   #[gpui::test(iterations = 5)]
   fn test_seeds_demo(cx: &mut TestAppContext) {
       let counter = cx.new(|_| Counter::new(cx));
       let _ = counter; // 只验证宏与种子机制
   }
   ```

2. 运行 `cargo test -p gpui --example testing --features test-support -- test_seeds_demo --nocapture`。
3. 故意让测试失败（加一行 `assert!(false);`），从输出里抄下失败的种子，然后 `SEED=<种子> cargo test -p gpui --example testing --features test-support -- test_seeds_demo` 重跑。

**需要观察的现象**：5 次迭代各自打印 `seed = N`；失败时输出 `failing seed: N` 与「设置 SEED 环境变量重跑」的提示。

**预期结果**：钉死 `SEED` 后重复运行，任务交错顺序完全一致（配合 `--nocapture` 观察示例中 `test_random_interleaving` 的 `spawned/ran` 对齐输出会更直观）。本组命令未在本机执行过，具体输出格式以实际运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`#[gpui::test]` 与普通 `#[test]` 有什么本质区别？什么时候用后者就够了？

**答案**：`#[gpui::test]` 会构建整套测试运行时（dispatcher、执行器、TestPlatform、App）并在结束时自动拆卸，用于需要实体/窗口/任务的测试；不碰 GPUI 状态的纯逻辑测试用普通 `#[test]` 即可——例如 [tests/action_macros.rs:L6-L55](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/tests/action_macros.rs#L6-L55) 用普通 `#[test]` 验证 `actions!` 宏与 `#[derive(Action)]` 生成的代码可编译、可注册。

**练习 2**：为什么拆卸序列里 `cx.quit()` 之前要先 `run_until_parked()`，之后还要再跑一次？

**答案**：之前一次确保测试体派生的任务全部完成、效果全部冲刷，避免「 quit 后还有任务想更新已关闭的 App」；`quit()` 内部调用 `App::shutdown`（[src/app/test_context.rs:L179-L187](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L179-L187)），会触发 `on_app_quit` 收尾 future，这些 future 本身也是任务；之后一次 `run_until_parked` 把它们跑完。同时 `forbid_parking` 保证收尾期间若有人等外部 IO 会直接 panic 而不是挂死测试线程。

**练习 3**：`dispatcher.drain_tasks()`（拆卸③）丢弃的是什么？为什么需要？

**答案**：丢弃所有仍被挂起（未完成也被 await）的任务。测试体 drop 掉 `Task` 即取消，但取消的任务要等下次被轮询才会真正析构其状态；`drain_tasks` 强制清空，让任务持有的实体句柄释放，配合泄漏检测在 `LeakDetector::drop` 时给出干净的判定（宏源码注释也承认：理想情况下只 drop 被取消的任务，但 async-task 暂时不提供这种能力）。

### 4.2 `cx.executor()`：TestDispatcher 与虚拟时钟

#### 4.2.1 概念说明

`TestAppContext::executor()` 返回克隆的 `BackgroundExecutor`，它是测试里操纵时间的遥控器。关键认知：**测试里没有真实时间**。

- `now()` 来自 `TestDispatcher` 覆写的 `PlatformDispatcher::now()`，读的是调度器的虚拟时钟；
- `executor.timer(duration)` 把定时器注册进虚拟时间轴；
- `advance_clock(duration)` 只拨表针、不执行任何任务，但会让到期的 timer 变为就绪；
- `run_until_parked()` 反复 `tick` 直到无可运行任务，且在无任务但有未到期定时器时会自动把时钟推进到下一个定时器（保留历史语义：凡是能取得进展的工作都被消化）。

另一条铁律来自 `PlatformDispatcher::dispatch_after` 在测试下的行为：直接 panic，并提示改用 `BackgroundExecutor::timer()`。这正是项目 CLAUDE.md 中「测试里优先用 GPUI 执行器定时器而非 `smol::Timer`」规则的根源——只有经过调度器的定时器才被虚拟时钟管理，外部定时器会让调度器「无任务可跑」而触发 parking 检查。

#### 4.2.2 核心流程

一个「等待 3 秒后完成」的任务在测试中的生命周期：

```text
executor.timer(3s)            → 定时器挂到虚拟时间轴 t=T₀+3s
某任务 await 该 timer          → 任务挂起，调度器无就绪任务
run_until_parked()            → tick 无果 → 发现未到期定时器
                              → 时钟跳到 T₀+3s，timer 就绪
                              → 继续 tick，任务被唤醒执行
                              → 直到再次无任务且无定时器 → 停
```

若此时还有一个等待**外部** IO 的 future，`allow_parking == false` 会让 `run_until_parked` panic（死锁检测）；`cx.executor().allow_parking()` 豁免后线程可真正睡眠等外部事件。

#### 4.2.3 源码精读

- [src/app/test_context.rs:L196-L204](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L196-L204) —— `executor()` 返回 `BackgroundExecutor`，`foreground_executor()` 返回主线程执行器引用。
- [src/platform/test/dispatcher.rs:L16-L34](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/dispatcher.rs#L16-L34) —— `TestDispatcher` 只是 `Arc<TestScheduler>` 的薄包装；`new(seed)` 的配置说明了一切：`randomize_order: true`（种子决定顺序）、`allow_parking: false`（默认禁停泊）、`timeout_ticks: 0..=1000`。
- [src/platform/test/dispatcher.rs:L119-L137](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/dispatcher.rs#L119-L137) —— `now()` 返回 `scheduler.clock().now()`（虚拟时钟的出处）；`dispatch_after` 直接 panic 并指点用 `BackgroundExecutor::timer()`。
- [src/executor.rs:L183-L192](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs#L183-L192) —— `timer`：零时长直接 `Task::ready`，否则把调度器定时器包成任务。
- [src/executor.rs:L200-L222](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs#L200-L222) —— `advance_clock`（拨表不跑任务）、`tick`（跑一个任务）、`run_until_parked`（文档明确：无可运行任务时推进时钟到下一个定时器，以保留「消化一切能进展的工作」的历史语义）。
- [src/executor.rs:L224-L256](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs#L224-L256) —— `allow_parking` / `forbid_parking` 开关（宏拆卸时会强制 `forbid`，见 4.1）。
- [src/app/test_context.rs:L584-L614](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L584-L614) —— `TestAppContext::condition`：用 `executor().timer(3s)` 与通知流 `race`，是「等到条件成立」的标准写法——超时也是虚拟时钟上的 3 秒，真实耗时几乎为零。
- [src/app/test_context.rs:L648-L671](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L648-L671) —— `Entity::next_notification(advance_clock_by, cx)`：参数直接要求调用者指定「拨多少时钟」，把「推进时间」变成等待语义的一部分。

#### 4.2.4 代码实践

**实践目标**：验证虚拟时钟——等待 24 小时的测试应当瞬间完成。

**操作步骤**：

1. 在 `examples/testing.rs` 的 `mod tests` 中加入（示例代码，非项目原有代码）：

   ```rust
   #[gpui::test]
   async fn test_virtual_clock(cx: &mut TestAppContext) {
       let started = cx.executor().now();
       cx.executor().timer(Duration::from_secs(24 * 60 * 60)).await;
       let elapsed = cx.executor().now() - started;
       assert!(elapsed >= Duration::from_secs(24 * 60 * 60));
   }
   ```

   （需要在文件头部 `use std::time::Duration;`。）

2. 再写一个对照版本：把 `timer(...)` 换成 `std::thread::sleep(Duration::from_secs(1))`（或 `smol::Timer`），观察测试的结局。
3. 运行：`cargo test -p gpui --example testing --features test-support -- test_virtual_clock`。

**需要观察的现象**：第一版瞬间通过且断言成立；对照版本要么真实睡 1 秒、要么因 parking 检查直接 panic（取决于写法）。

**预期结果**：`timer().await` 的完成由 `run_until_parked` 语义（或 async 测试的 `block_test` 驱动）推进时钟实现，`now()` 的差值等于虚拟时长；对照版本演示「绕过调度器的等待」在测试中不可容忍。具体 panic 文案以实际运行为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`advance_clock(1h)` 之后定时器回调执行了吗？

**答案**：没有。`advance_clock` 只移动虚拟时钟、让到期 timer 变为**就绪**，不执行任何任务；要执行还需 `run_until_parked()`（或 `tick()` 逐个驱动）。见 [src/executor.rs:L200-L210](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/executor.rs#L200-L210) 的文档措辞："This does not run any tasks, but does make `timer`s ready"。

**练习 2**：为什么 `dispatch_after` 在测试里选择 panic 而不是走真实定时？

**答案**：真实定时会把测试拖入真实时间（慢且不确定），更糟的是线程睡眠期间调度器看不到任何可推进的工作，`allow_parking == false` 下会触发死锁 panic。强制使用 `BackgroundExecutor::timer()` 保证一切等待都在虚拟时间轴上，既快又可复现（[src/platform/test/dispatcher.rs:L132-L137](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/dispatcher.rs#L132-L137)）。

**练习 3**：`TestAppContext::condition` 的 3 秒超时在 CI 机器卡顿时会误报吗？

**答案**：不会。这 3 秒是虚拟时钟上的时长（`executor().timer(Duration::from_secs(3))`），与真实墙钟无关；「等待」的推进依赖通知流与虚拟时间的赛跑，而不是真实睡眠。见 [src/app/test_context.rs:L591-L613](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L591-L613)。

### 4.3 `TestAppContext`：实体级测试与 TestApp

#### 4.3.1 概念说明

`TestAppContext` 是「测试版的 App 入口」。两个设计要点：

1. **它实现了 `AppContext` trait**，所以你平时在 `Entity<T>` 上用的全部人体工学方法（`entity.update(cx, ...)`、`entity.read_with(cx, ...)`、`cx.new(...)`）原样可用——只是这里的 `cx` 是 `&mut TestAppContext`。
2. **效果即时冲刷**：同步测试里，每次 `update*` 调用返回时，Notify/Emit 效果已经派发完毕（观察者回调已跑）。异步测试则相反——不 yield 控制权，后台/异步副作用不会运行，这给了你精确控制「什么时候让世界前进一拍」的能力。

对于「不想要上下文细节、只想要一个能开窗口的 App」的场景，同文件家族里还有更新型的 `TestApp`：它的 `update` 与窗口的 `update` 在闭包结束后**自动 `run_until_parked`**，代价是你失去了「检查副作用尚未运行」的精细时机。

#### 4.3.2 核心流程

```text
#[gpui::test]                      ← 宏建好 cx（含 dispatcher/平台/App）
cx.new(|cx| Counter::new(cx))       ← 建实体（AppContext::new → App::new）
counter.update(cx, |c, cx| ...)     ← 效果在返回后立即冲刷（同步测试）
cx.run_until_parked()               ← 跑完所有 pending 任务
断言 counter.read_with(cx, ...)     ← 读回终态
```

多上下文协作测试：

```text
cx_a、cx_b 共享同一 dispatcher
b.update(...) 产生后台任务
cx_b.run_until_parked()  ← 随机选择先跑 a 还是 b 的任务（种子决定）
a.update(...) 消费结果
```

#### 4.3.3 源码精读

- [src/app/test_context.rs:L18-L34](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L18-L34) —— `TestAppContext` 结构体：`#[derive(Clone)]`，字段包括双执行器、dispatcher、`Rc<TestPlatform>`、文本系统、测试函数名、`on_quit` 回调表，以及公开的 `app: Rc<AppCell>`。
- [src/app/test_context.rs:L36-L123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L36-L123) —— `impl AppContext for TestAppContext`：每个方法都是 `app.borrow_mut()` 后转调 `App` 的同名方法——这就是「实体方法可直接拿 `&mut TestAppContext` 当 cx」的实现处。注意 `as_mut` 被禁止并提示改用 `update`。
- [src/app/test_context.rs:L126-L149](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L126-L149) —— `build`：用同一个 `Arc<TestDispatcher>` 造出双执行器与 `TestPlatform`，HTTP 客户端是 `FakeHttpClient::with_404_response()`（测试中一切网络请求都得到 404），最后 `App::new_app` 并设置 `GpuiMode::test()`。
- [src/app/test_context.rs:L172-L175](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L172-L175) —— `new_app`：克隆同一 dispatcher 再建一个 `TestAppContext`，多上下文共享调度器的出处。
- [src/app/test_context.rs:L206-L216](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L206-L216) —— `update` / `read`：借出 `&mut App` / `&App` 的最通用入口（`bind_keys`、`open_window` 等无实体版本的方法都从这里进去）。
- [src/app/test_context.rs:L545-L582](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L545-L582) —— `notifications(entity)` 返回实体每次更新的 `Stream<()>`，`events(entity)` 返回实体 emit 事件的接收端——把「观察」变成可 await 的流。
- [examples/testing.rs:L224-L246](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L224-L246) —— `basic_testing`：官方示范——`cx.new` 建计数器、直接改字段、`read_with` 读回；再 `cx.emit(CounterEvent)` 后立刻断言副作用已生效（count 变成 999），证明「效果在 update 结束后立即运行」。
- [examples/testing.rs:L278-L299](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L278-L299) —— `test_async_operations`：反例示范——detach 的 reload 任务在 yield 之前不运行（count 仍 100），`cx.run_until_parked()` 之后才变成 150。
- [examples/testing.rs:L470-L487](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L470-L487) —— `test_app_sync`：双上下文 + mock 网络的「分布式计数器」同步测试。
- [src/app/test_app.rs:L40-L59](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs#L40-L59) —— `TestApp`：声明「比 `TestAppContext` 更干净的 API」（模块文档 [L1-L25](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs#L1-L25) 列出三大卖点：自动冲刷、窗口管理、输入模拟）。
- [src/app/test_app.rs:L109-L118](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs#L109-L118) —— `TestApp::update`：闭包结束后自动 `run_until_parked`——与 `TestAppContext::update` 的关键差异。
- [src/app/test_app.rs:L542-L558](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs#L542-L558) —— 内嵌的 `test_basic_usage`：`TestApp::new()` → `open_window(Counter::new)` → `window.update(...)` → `window.read(...)` 断言，是这套新 API 的最短完整样例。

#### 4.3.4 代码实践

**实践目标**：用 `notifications` / `events` 流观察实体的两条通知路径（u2-l3 理论的落地）。

**操作步骤**：

1. 在 `examples/testing.rs` 的 `mod tests` 中加入（示例代码）：

   ```rust
   use futures::StreamExt;

   #[gpui::test]
   async fn test_observation_streams(cx: &mut TestAppContext) {
       let counter = cx.new(|cx| Counter::new(cx));
       let mut notifications = cx.notifications(&counter);
       let mut events = cx.events(&counter);

       counter.update(cx, |_, cx| cx.notify());
       counter.update(cx, |_, cx| cx.emit(CounterEvent));

       assert!(notifications.next().now_or_never().is_some());
       assert!(events.next().now_or_never().is_some());
   }
   ```

2. 运行：`cargo test -p gpui --example testing --features test-support -- test_observation_streams`。
3. 把两个 `counter.update` 都换成不产生效果的空更新（`|_, _| {}`），重跑。

**需要观察的现象**：空更新版本中两个流的 `now_or_never()` 都返回 `None`（流上没有新元素）。

**预期结果**：`notifications` 只在 `cx.notify()` / 实体更新路径上产出元素，`events` 只在 `emit` 上产出——与 u2-l3 讲的「两条路径、一个入效果队列」完全对应。`now_or_never` 来自 `futures::FutureExt`，示例本身已引入 `futures` 依赖（文件头 `use futures::StreamExt` 未见则需补；`gpui` 的 dev-dependencies 含 futures，待本地验证具体导入）。

#### 4.3.5 小练习与答案

**练习 1**：`basic_testing` 里为什么用 `counter.read_with(cx, ...)` 而不是 `counter.read(cx)`？

**答案**：测试注释直接说明了原因——"TestAppContext doesn't support `read(cx)`"。`read` 需要 `&App`，而 `&mut TestAppContext` 只能 `borrow_mut` 出可变借用，无法同时再给出不可变引用；`read_with` 走 `AppContext::read_entity`（内部 `app.borrow()`），是测试里的标准读法（[examples/testing.rs:L232-L234](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L232-L234)）。

**练习 2**：`TestApp` 与 `TestAppContext` 各适合什么场景？

**答案**：需要精确控制任务推进时机、检查「副作用尚未发生」、多上下文模拟分布式时用 `TestAppContext`（配合 `#[gpui::test]`）；只想快速搭一个 App + 窗口做行为断言、不在乎中间时序时用 `TestApp`——它的 `update` / `TestAppWindow::update` 都自动 `run_until_parked`（[src/app/test_app.rs:L344-L356](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs#L344-L356)），写起来更像普通集成测试。

**练习 3**：测试里发起 HTTP 请求会发生什么？

**答案**：得到 404。`TestAppContext::build` 把 http_client 设为 `FakeHttpClient::with_404_response()`（[src/app/test_context.rs:L133](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L133)），所有网络都被假装失败——外部系统应在测试中 mock（示例的 `MockNetwork` 就是范式）。

### 4.4 `VisualTestContext`：窗口级测试与输入模拟

#### 4.4.1 概念说明

窗口级测试多了一个角色：`VisualTestContext`。它是「`Window` + `App` 的测试等价物」——内部持有一个 `TestAppContext`（可 Deref 回去）加一个 `AnyWindowHandle`，把窗口操作收窄到这一个窗口上。

输入模拟分两档：

- **动作档**：`dispatch_action(action)` 直接把动作派发给当前焦点节点，走与真实按键相同的四阶段派发（u5-l3），跳过 Keymap 匹配。
- **按键档**：`simulate_keystrokes("cmd-shift-p enter")` 逐个解析按键字符串并派发 keystroke，会经过 Keymap 匹配——所以测试里要先 `cx.update(|cx| cx.bind_keys([...]))` 注册绑定。
- **鼠标档**：`simulate_click` / `simulate_mouse_down` / `simulate_mouse_up` / `simulate_event`。**鼠标事件依赖上一帧的 hitbox**（u5-l1/u5-l2 的结论），所以派发前必须保证窗口画过——要么先 `cx.draw(...)` 手动画一帧，要么依赖「每次 `update*` 调用后窗口自动重绘」的特性。

`VisualTestContext::draw` 是自定义元素测试的核心工具：它把任意一个元素当作根，完整走 `layout_as_root → prepaint → paint` 三阶段（u4-l1），从而把 hitbox、DispatchTree、文本样式都登记进渲染帧，随后的 `simulate_event` 才有据可依。

#### 4.4.2 核心流程

```text
路线 A：真实窗口
  cx.update(|cx| cx.open_window(...))          ← 开窗（首帧同步画出）
  VisualTestContext::from_window(window, cx)   ← 换上下文（通常直接遮蔽绑定）
  cx.update(|window, cx| ...)                  ← 每次 update* 后窗口重绘
  cx.simulate_keystrokes("up") / dispatch_action / simulate_click(point)

路线 B：元素级（gpui 内部测试的主流写法）
  let cx = cx.add_empty_window();              ← 空窗口 + VisualTestContext
  cx.draw(origin, size, |_, cx| 视图.into_any_element())  ← 手动画一帧
  cx.simulate_event(MouseDownEvent { ... })    ← 在这一帧的 hitbox 上命中测试
```

#### 4.4.3 源码精读

- [src/app/test_context.rs:L735-L762](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L735-L762) —— `VisualTestContext` 定义（`#[derive(Deref, DerefMut)]` 到内层 `cx: TestAppContext`，外加窗口句柄）与 `from_window` 构造器；文档建议直接遮蔽原变量。
- [src/app/test_context.rs:L747-L752](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L747-L752) —— `update`：给出 `(&mut Window, &mut App)`，窗口级测试最常用的进入点。
- [src/app/test_context.rs:L769-L802](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L769-L802) —— `dispatch_action`（转发给 `TestAppContext::dispatch_action`）、`simulate_keystrokes`、`simulate_input`。
- [src/app/test_context.rs:L480-L508](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L480-L508) —— `TestAppContext` 侧的实现：`dispatch_action` 在 `update_window` 内调用 `window.dispatch_action`，随后 `run_until_parked`；`simulate_keystrokes` 把空格分隔的字符串 `Keystroke::parse` 成逐个按键派发，最后同样 `run_until_parked`——**每次模拟后自动收敛任务**是这些 API 的统一约定。
- [src/app/test_context.rs:L804-L880](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L804-L880) —— 鼠标家族：`simulate_mouse_move/down/up`、`simulate_click`（down+up 成对，click 合成的必要条件，u5-l2）、`simulate_modifiers_change` / `simulate_capslock_change`、`simulate_resize`。
- [src/app/test_context.rs:L893-L921](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L893-L921) —— `draw`：进入元素 arena 作用域 → `Drawable::new` 包装元素 → `layout_as_root` → `prepaint` → `paint` → `window.refresh()` → 清理 arena，返回两个阶段状态。文档明言它是「为模拟事件或动作而绘制元素」的工具。
- [src/app/test_context.rs:L923-L929](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L923-L929) —— `simulate_event`：把任意 `InputEvent` 转成 `PlatformInput` 交给 `TestWindow`，再 `run_until_parked`；文档警告「先调用过 draw」。
- [src/platform/test/window.rs:L160-L169](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/test/window.rs#L160-L169) —— `TestWindow::simulate_input`：取出窗口注册的 `input_callback`（即 `Window` 挂上来的输入处理），调用后放回——输入由此进入真实派发管线。
- [src/elements/list.rs:L1785-L1821](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/list.rs#L1785-L1821) —— gpui 自己的测试里「`add_empty_window` → `draw` → `simulate_event`」的标准配方实例（先画一帧，再灌一个滚轮事件验证状态机）。
- [examples/testing.rs:L251-L273](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L251-L273) —— `test_counter_in_window`：开真实窗口 → `VisualTestContext::from_window` → 通过 `focus_handle.dispatch_action(&Increment, window, cx)` 派发动作 → 断言计数。文档注释（[L248-L250](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L248-L250)）点明「窗口在每次 update* 调用后画出」。
- [examples/testing.rs:L326-L357](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L326-L357) —— `test_counter_random_operations`：属性测试示范——`iterations = 10` + `StdRng` 参数，随机混合 increment/decrement 后断言期望值。
- [src/app/test_context.rs:L968-L982](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L968-L982) —— `into_mut`：把 `VisualTestContext` 装箱泄漏成 `&'static mut Self`（用 `on_quit` 注册回收），`add_window_view` 等返回 `&mut VisualTestContext` 的方法都靠它——这也是为什么这些借用的生命周期总能活到测试结束。

#### 4.4.4 代码实践

**实践目标**：完成一次真正的「点击计数器按钮」测试（hitbox 路径全通）。

**操作步骤**：

1. 在 `examples/testing.rs` 的 `mod tests` 中加入（示例代码；利用 `Entity<V: Render>` 实现了 `IntoElement` 这一事实——[src/view.rs:L95](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/view.rs#L95)）：

   ```rust
   use gpui::{Modifiers, point, size};

   struct ClickCounter(usize);

   impl Render for ClickCounter {
       fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
           div()
               .id("inc")
               .size_full()
               .flex()
               .items_center()
               .justify_center()
               .on_click(cx.listener(|this, _, _, cx| {
                   this.0 += 1;
                   cx.notify();
               }))
               .child(format!("count: {}", self.0))
       }
   }

   #[gpui::test]
   fn test_click_increments(cx: &mut TestAppContext) {
       let cx = cx.add_empty_window();
       let view = cx.new(|_| ClickCounter(0));

       cx.draw(point(px(0.), px(0.)), size(px(100.), px(100.)), |_, _| {
           view.clone().into_any_element()
       });

       cx.simulate_click(point(px(50.), px(50.)), Modifiers::none());

       view.read_with(cx, |counter, _| assert_eq!(counter.0, 1));
   }
   ```

2. 运行：`cargo test -p gpui --example testing --features test-support -- test_click_increments`。
3. 把 `simulate_click` 的坐标改成 `point(px(150.), px(50.))`（窗口外）重跑，再删掉 `cx.draw(...)` 那行重跑。

**需要观察的现象**：原版通过；窗口外坐标版计数保持 0（无命中）；删除 draw 版本无法命中（甚至行为未定义——没有已渲染帧就没有 hitbox）。

**预期结果**：按钮铺满 100×100 的绘制区域，中心点命中其 hitbox，down+up 配对合成 click，监听器把计数加到 1。坐标越界与缺帧两个对照印证「命中测试读的是上一帧」。本实践未在本机运行，细节以实际执行为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `simulate_keystrokes` 在测试里可能「按键没反应」，而 `dispatch_action` 总是有效？

**答案**：`dispatch_action` 直接把动作送到焦点路径的派发树，不查键位表；`simulate_keystrokes` 走完整链路，要求（1）绑定已用 `cx.bind_keys` 注册（应用运行时通常在 `run` 回调里做，测试需自己补，参照 [examples/testing.rs:L182-L185](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L182-L185)）；（2）焦点落在带正确 `key_context` 的元素上。缺一就匹配不到 `KeyBinding`——这正是 u5-l4 排查清单在测试中的体现。

**练习 2**：`add_empty_window` 为什么返回的是 `&mut VisualTestContext` 而不是 `TestAppContext`？

**答案**：开窗之后几乎所有后续操作（update、模拟输入、断言窗口标题）都需要窗口上下文；返回 `VisualTestContext` 并经 `into_mut` 提升为 `&'static mut`（[src/app/test_context.rs:L267-L283](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L267-L283)、[L968-L982](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L968-L982)），调用者可以直接 `let cx = cx.add_empty_window();` 遮蔽原变量，免去生命周期纠缠。

**练习 3**：`simulate_click` 内部为什么是 down + up 两次 `simulate_event`？

**答案**：GPUI 的 click 是合成事件——按下时记录 pending 状态，抬起时配对触发（u5-l2 的 pending_mouse_down 机制）。只发 down 或只发 up 都不构成完整点击；`simulate_click` 帮你成对发出（[src/app/test_context.rs:L849-L864](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L849-L864)）。

### 4.5 `HeadlessAppContext` 与实体泄漏检测

#### 4.5.1 概念说明

**HeadlessAppContext**：`TestAppContext` 的文本系统是假的（字形度量不真实），布局敏感的测试会失真。`HeadlessAppContext` 在保持确定性调度（仍是 `TestDispatcher`）的前提下，允许注入真实 `PlatformTextSystem`（如 Windows 的 DirectWrite、macOS 的 CoreText）与可选的 GPU 渲染器工厂，从而支持 `capture_screenshot` 截图比对。要注意平台现实：`gpui_platform::current_headless_renderer()` 目前**只在 macOS 返回 `Some`**（Metal），Linux/Windows 返回 `None`——视觉快照测试在非 macOS 上需要降级为结构性断言。

**实体泄漏检测**：GPUI 实体是引用计数的，强句柄成环（实体互持、或实体持有引用自己的任务/订阅）就永远释放不了。`LeakDetector` 在测试构建下给**每个句柄**记账（创建时登记、drop 时销账），提供两档断言：

- 单实体档：`weak.assert_released()`——该实体的所有句柄都应已释放；
- 全局档：`snapshot()` + `assert_no_new_leaks(snapshot)`——快照之后新建的实体到断言时都应已释放。

更强的兜底是 `LeakDetector` 自己的 `Drop`：测试结束时若记账表非空，直接 panic 列出泄漏句柄——也就是说**每个 `#[gpui::test]` 都自带泄漏检测**（前提是开了 `test-support`/`leak-detection`）。设 `LEAK_BACKTRACE=1` 还能打印每个泄漏句柄的分配点栈。

#### 4.5.2 核心流程

```text
泄漏检测的接线：
  cargo --features test-support
    → Cargo.toml: test-support 依赖 leak-detection 依赖 backtrace
    → LeakDetector 字段参与编译（#[cfg(any(test, feature = "leak-detection"))]）
  #[gpui::test] 展开
    → TestAppContext::build ...（App 内含带 LeakDetector 的 EntityMap）
    → let _entity_refcounts = app.borrow().ref_counts_drop_handle();  ← 宏取凭证
  测试体（实体可能成环泄漏）
  拆卸：run_until_parked → quit（App::shutdown，窗口关闭、句柄释放）→ drop(cx)
    → EntityMap drop → LeakDetector::drop
    → 记账表非空？→ panic 并列出泄漏（LEAK_BACKTRACE=1 带分配栈）
```

#### 4.5.3 源码精读

- [Cargo.toml:L19-L31](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/Cargo.toml#L19-L31) —— feature 链：`test-support = ["leak-detection", ...]`，`leak-detection = ["backtrace"]`。
- [src/app.rs:L67-L72](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L67-L72) —— `headless_app_context` / `test_app` / `test_context` 三个模块的 `#[cfg(any(test, feature = "test-support"))]` 门控。
- [src/app.rs:L930-L959](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L930-L959) —— `App::ref_counts_drop_handle`（宏用来钉住引用计数表的凭证）、`App::leak_detector_snapshot` / `assert_no_new_leaks` 的公开包装。
- [src/app/entity_map.rs:L913-L952](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L913-L952) —— `LeakDetector` 文档与结构：`entity_handles: HashMap<EntityId, EntityLeakData>`，每个句柄一条记录，`LEAK_BACKTRACE` 开启时附未解析回溯。
- [src/app/entity_map.rs:L961-L996](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L961-L996) —— `handle_created` / `handle_released`：句柄创建（含 `WeakEntity::upgrade`）与 drop 的记账对。
- [src/app/entity_map.rs:L998-L1036](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L998-L1036) —— `assert_released`（单实体，panic 列出仍存活的句柄）与 `snapshot`。
- [src/app/entity_map.rs:L1038-L1082](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L1038-L1082) —— `assert_no_new_leaks`：只追究快照之后**新建**的实体，存量不究——适合「一段操作不应留下新实体」的断言。
- [src/app/entity_map.rs:L1085-L1092](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L1085-L1092) —— `Drop for LeakDetector`：非空且非 panic 中则收集泄漏信息（后续 panic 报出）——每个测试自动 leak 检查的出处。
- [src/app/entity_map.rs:L619-L653](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L619-L653) —— `WeakEntity::assert_released`：用户侧最常用的单实体断言，文档给出 `drop(entity); weak.assert_released();` 范式。
- [src/app/headless_app_context.rs:L38-L101](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs#L38-L101) —— `HeadlessAppContext` 与 `with_platform`：种子同样受 `SEED` 环境变量影响；`TestPlatform::with_platform` 注入真实文本系统与渲染器工厂，其余（FakeHttpClient、GpuiMode::test）与 `TestAppContext` 一致。
- [src/app/headless_app_context.rs:L103-L146](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs#L103-L146) —— `open_window`（`show: false`、`focus: false` 的不可见窗口）、`run_until_parked` / `advance_clock` / `allow_parking` / `forbid_parking`。
- [src/app/headless_app_context.rs:L168-L195](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs#L168-L195) —— `capture_screenshot`（内部 `window.render_to_image()`，要求建context 时给了返回 `Some` 的渲染器工厂）；`Drop` 里先 `shutdown`——注释明言是为了让窗口关闭、实体句柄在 `LeakDetector` 运行前释放。
- [crates/gpui_platform/src/gpui_platform.rs:L83-L97](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L83-L97) —— `current_headless_renderer()`：macOS 返回 Metal 渲染器，其余平台 `None`。

#### 4.5.4 代码实践

**实践目标**：制造一次泄漏并被检测器抓住，再用 `WeakEntity` 修复。

**操作步骤**：

1. 在 `examples/testing.rs` 的 `mod tests` 中加入（示例代码）：

   ```rust
   struct Parent {
       child: Option<gpui::Entity<Child>>,
   }
   struct Child {
       parent: Option<gpui::WeakEntity<Parent>>, // 先用强句柄试一次
   }

   #[gpui::test]
   fn test_no_orphan_entities(cx: &mut TestAppContext) {
       let parent = cx.new(|cx| {
           let parent = cx.reserve_entity();
           let child = cx.insert_entity(parent.clone(), |_| Child {
               parent: Some(parent.downgrade()),
           });
           Parent { child: Some(child) }
       });

       let weak = parent.downgrade();
       drop(parent);
       cx.run_until_parked();
       weak.assert_released(); // 若 Child 持强回指，这里 panic
   }
   ```

2. 先把 `Child.parent` 的类型换成 `Option<gpui::Entity<Parent>>` 并在构造时 `parent: Some(parent.clone())`（注意 `cx.reserve_entity` 的 reservation 可 `clone` 出强句柄），运行观察 panic。
3. 运行 `LEAK_BACKTRACE=1 cargo test -p gpui --example testing --features test-support -- test_no_orphan_entities` 看分配栈。

**需要观察的现象**：强回指版本在 `assert_released`（或测试收尾的 `LeakDetector::drop`）处 panic，报出 `Handles for ... leaked`；换回 `WeakEntity` 后通过；`LEAK_BACKTRACE=1` 时 panic 信息含分配点回溯。

**预期结果**：与 u2-l2 的结论闭环——子回指父必须用弱句柄，泄漏检测把这条纪律变成可执行断言。两版行为差异的准确 panic 位置（`assert_released` 还是收尾 Drop）取决于环上句柄的 drop 时机，以实际运行为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `HeadlessAppContext` 要在 `Drop` 里显式 `shutdown`？

**答案**：注释写明：让窗口关闭、实体句柄在 `LeakDetector` 运行之前释放。如果不关窗，窗口持有的根视图等句柄会被记账表当成泄漏，在检测器 Drop 时误报（[src/app/headless_app_context.rs:L189-L195](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs#L189-L195)）。这也是所有测试上下文「拆卸时 quit」的共通理由。

**练习 2**：`assert_no_new_leaks(snapshot)` 与 `weak.assert_released()` 分别适合什么断言？

**答案**：`assert_released` 针对一个已知实体，验证「我 drop 之后它真的死了」，适合排查特定对象的生命周期；`assert_no_new_leaks` 在操作前打快照、操作后验证没有**新增**存活实体，适合「执行某流程不应残留状态」的整段断言，且不被测试装置自身的长命实体干扰（[src/app/entity_map.rs:L1038-L1082](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L1038-L1082) 的文档强调「快照时已存在的实体被忽略」）。

**练习 3**：在 Linux 上写「视觉回归测试」的现实方案是什么？

**答案**：`current_headless_renderer()` 在非 macOS 返回 `None`（[crates/gpui_platform/src/gpui_platform.rs:L93-L96](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_platform/src/gpui_platform.rs#L93-L96)），像素级截图暂不可用；现实方案是结构性断言——用 `VisualTestContext::draw` 画帧后检查 `debug_bounds(selector)`（[src/app/test_context.rs:L887-L890](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_context.rs#L887-L890)）或直接断言视图状态/`rendered_frame` 的相关字段，把「长什么样」降级为「布局与状态正确」。

## 5. 综合实践

综合实践把本讲全部知识串成一个完整任务：**为计数器按钮写三个测试——模拟点击、模拟按键派发 action、条件编译的视觉快照——并跑通 gpui 自身测试套件**。

以 [examples/testing.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs) 为宿主（它已有 Counter、动作、测试模块，且 `[[example]]` 已在 Cargo.toml 声明），在其 `mod tests` 中完成：

**测试一：模拟点击后计数递增**（4.4 的路线 B）

```rust
#[gpui::test]
fn test_increment_button_click(cx: &mut TestAppContext) {
    let cx = cx.add_empty_window();
    // Counter 的 render 是居中布局，直接复用 4.4.4 的 ClickCounter 思路，
    // 或为本测试写一个按钮铺满窗口的包装视图
    let view = cx.new(|_| ClickCounter(0));
    cx.draw(point(px(0.), px(0.)), size(px(100.), px(100.)), |_, _| {
        view.clone().into_any_element()
    });
    cx.simulate_click(point(px(50.), px(50.)), Modifiers::none());
    view.read_with(cx, |c, _| assert_eq!(c.0, 1));
}
```

要点：`draw` 先登记 hitbox，`simulate_click` 走 down+up 合成。

**测试二：模拟按键派发 action**（4.4 的按键档，走完整 Keymap 链路）

```rust
#[gpui::test]
fn test_up_key_increments(cx: &mut TestAppContext) {
    cx.update(|cx| {
        cx.bind_keys([gpui::KeyBinding::new("up", Increment, Some("Counter"))]);
    });
    let window = cx.update(|cx| {
        cx.open_window(Default::default(), |_, cx| {
            cx.new(|cx| Counter::new(cx))
        })
        .unwrap()
    });
    let mut cx = VisualTestContext::from_window(window.into(), cx);
    let counter = window.root(&mut cx).unwrap();
    let focus_handle = counter.read_with(&cx, |c, _| c.focus_handle.clone());
    cx.update(|window, cx| focus_handle.focus(window, cx));
    cx.simulate_keystrokes("up");
    counter.read_with(&cx, |c, _| assert_eq!(c.count, 1));
}
```

要点：`bind_keys` 要在测试里自己补（应用运行时在 `run_example` 里做，见 [examples/testing.rs:L182-L185](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L182-L185)）；焦点要落到带 `key_context("Counter")` 的根 div 上（`track_focus` 已在 render 里）。若按键路径不生效，回退到已被上游验证的 `focus_handle.dispatch_action(&Increment, window, cx)` 写法（[examples/testing.rs:L263-L266](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/testing.rs#L263-L266)）——那也是「按键派发 action」语义的一半（跳过匹配直达派发）。

**测试三：条件编译的视觉快照**（4.5 的平台现实）

```rust
#[cfg(all(test, feature = "test-support", target_os = "macos"))]
#[test]
fn test_counter_screenshot_macos() {
    use std::sync::Arc;
    // macOS: Metal headless 渲染器可用
    // let text_system = Arc::new(真实平台文本系统);
    // let mut cx = HeadlessAppContext::with_platform(
    //     text_system, Arc::new(Assets), || gpui_platform::current_headless_renderer());
    // let window = cx.open_window(size(px(300.), px(200.)), |_, cx| {
    //     cx.new(|cx| Counter::new(cx))
    // }).unwrap();
    // cx.run_until_parked();
    // let image = cx.capture_screenshot(window.into()).unwrap();
    // assert_eq!((image.width(), image.height()), (300, 200));
}

#[cfg(all(test, feature = "test-support", not(target_os = "macos")))]
#[test]
fn test_counter_visual_structural() {
    // 非 macOS: 渲染器为 None，退化为结构性断言（TestApp + draw + 状态断言）
    let mut app = TestApp::new();
    let mut window = app.open_window(Counter::new);
    window.update(|_counter, _window, cx| cx.notify());
    window.draw(); // 强制重绘一帧，确保渲染路径执行
}
```

macOS 分支需要补一个真实 `PlatformTextSystem` 才能编译运行（gpui 的 dev-dependencies 未直接提供跨平台实现，具体取型参考 [src/app/headless_app_context.rs:L30-L37](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/headless_app_context.rs#L30-L37) 的文档示例），本分支未在本机验证（待本地验证）。

**收尾验证**：

```bash
cargo test -p gpui --example testing --features test-support   # 三个新测试
cargo test -p gpui                                             # gpui 自身测试套件
```

全部通过即完成本讲实践。若 `cargo test -p gpui` 在本机失败，先确认工具链与平台依赖（Linux 需要 wayland/x11 开发库）——失败原因属于环境而非测试逻辑。

## 6. 本讲小结

- `#[gpui::test]` 是「参数即注入清单」的测试生成器：编译期展开出 `TestAppContext` 的构建与拆卸（`run_until_parked → quit → drain_tasks`），运行期由 `run_test` 按种子循环执行，`SEED` 环境变量可精确复现失败的并发交错。
- 测试里没有真实时间：`now()` 读虚拟时钟，`timer` 挂虚拟时间轴，`advance_clock` 拨表不跑任务，`run_until_parked` 消化一切能进展的工作并自动跨到下一个定时器；`dispatch_after` 在测试中直接 panic，一切等待必须经调度器。
- `TestAppContext` 实现 `AppContext` trait，实体方法原样可用；同步测试的效果在每次 `update*` 后即时冲刷，异步测试不 yield 就不推进——`TestApp` 则自动 `run_until_parked`，换取更省心的写法。
- `VisualTestContext` 补上窗口维度：动作档 `dispatch_action` 直达焦点路径，按键档 `simulate_keystrokes` 走完整 Keymap 链路（需自补 `bind_keys` 与焦点），鼠标档 `simulate_click/simulate_event` 依赖 `draw` 先登记的 hitbox。
- `HeadlessAppContext` 用真实文本系统与（仅 macOS 的）GPU 渲染器换取精确度量与截图能力；非 macOS 的视觉测试应降级为结构性断言。
- 泄漏检测内建于每个测试：`LeakDetector` 给句柄记账，`assert_released` / `assert_no_new_leaks` 提供两档断言，收尾的 `Drop` 对残留句柄直接 panic，`LEAK_BACKTRACE=1` 可看分配栈——把 u2-l2 的弱句柄纪律变成可执行检查。

## 7. 下一步学习建议

- **下一讲（u7-l5）**：性能剖析与调试工具——`profiler.rs`、`#[gpui::bench]`、`debug_overlay` 与 `inspector`。测试基础设施（本讲）与基准基础设施（`BenchAppContext`，同样在 `#[cfg(feature = "bench")]` 门控下）是姊妹关系，读完本讲再看 bench 会非常顺。
- **延伸阅读**：[src/app/test_app.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/test_app.rs) 内嵌的三个 `#[test]` 是新 API 的最小样例；scheduler crate（`TestScheduler` 的真正实现）值得打开看看 tick/时钟/随机顺序的具体算法。
- **实战方向**：为你自己在 u5/u6 写过的交互组件补一组测试——每个组件至少一个动作档（`dispatch_action`）与一个鼠标档（`draw` + `simulate_event`）用例，再用 `assert_no_new_leaks` 包住整个流程验证无残留实体。
- **毕业预告（u7-l7）**：综合实战会要求为迷你文件浏览器补 `gpui::test`，本讲的三档输入模拟与泄漏断言都将是验收项。
