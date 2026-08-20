# 初始化流程：init 与 init_from_handle

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行讲出 `gpui_tokio::init` 内部的六步：创建 Builder → 限制 2 个工作线程 → 启用驱动 → 构建 Runtime → 克隆 Handle → 写入 GPUI 全局单例 `GlobalTokio`。
2. 说出 `init` 与 `init_from_handle` 在 **runtime 所有权** 上的根本区别：前者「crate 替你创建并持有运行时」，后者「你自带运行时，crate 只保管遥控器」。
3. 解释 `GlobalTokio` 结构体里 `owned_runtime: Option<Runtime>` 这个 `Option` 为什么是两种初始化路径共用一个全局类型的钥匙。
4. 找到 Zed 主程序中 `gpui_tokio::init(cx)` 的调用位置（`crates/zed/src/main.rs:499`），并能描述它处在启动序列的哪一步。
5. 亲手写一个 GPUI headless 测试，验证两条初始化路径都能让 `Tokio::spawn` 正常工作。

## 2. 前置知识

本讲是第二单元（核心源码精读）的第一讲，建立在第一单元四讲的基础上。你只需要带着下面这些已建立的认知进来，本讲不会重复展开：

- **u1-l2（构建配置）**：`gpui_tokio` 是 workspace 成员，tokio 依赖只启用了 `rt` 和 `rt-multi-thread` 两个 feature——也就是说它只被允许「建运行时、spawn 任务」，不能用 tokio 的其他能力（如 `tokio::net`）。本讲会看到这个约束在代码里如何兑现。
- **u1-l3（GPUI 执行器模型）**：GPUI 的上下文类型（`App`、`Context<T>`、`TestAppContext` 等）都实现了 `AppContext` trait；全局状态挂在每个 `App` 实例上，**每个测试都有独立的全局表**。
- **u1-l4（Tokio 运行时基础）**：`Builder::new_multi_thread().worker_threads(N).enable_all().build()` 手动构建多线程运行时；`Runtime` 拥有线程池（drop 即关停），`Handle` 是可廉价克隆的「遥控器」，`Handle::spawn` 不要求调用线程处于 Tokio 上下文。

还有一个术语先澄清，本讲会反复使用：

- **所有权（ownership）**：Rust 中「谁拥有某个值，谁就负责在它生命结束时清理它」。对 `tokio::runtime::Runtime` 来说，「清理」意味着**关停线程池**。所以「谁持有 runtime」等价于「谁来决定这些 Tokio 线程什么时候退出」——这是理解 `init` 与 `init_from_handle` 差异的唯一关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_tokio/src/gpui_tokio.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs) | 本讲主角，全部 100 行；`init`、`init_from_handle`、`GlobalTokio` 都在这里 |
| [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs) | Zed 桌面主程序，第 499 行调用 `gpui_tokio::init(cx)`，是「谁在启动这座桥」的现场 |
| [crates/gpui/src/app.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs) | GPUI 的 `App` 类型；`set_global` / `global` 的实现在这里，是理解「写入全局」这一步的钥匙 |
| [crates/gpui/src/global.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs) | `Global` marker trait 与 `ReadGlobal` 的定义 |
| [crates/agent/src/tests/mod.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/agent/src/tests/mod.rs) | 真实测试中调用 `gpui_tokio::init` 的样例，综合实践会模仿它 |
| [crates/livekit_client/examples/test_app.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/livekit_client/examples/test_app.rs) | example 程序中调用 `init` 的样例 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 `init` 函数**、**4.2 `GlobalTokio` 结构体定义**、**4.3 `init_from_handle` 函数**。取消语义（`Drop` 里的 `shutdown_background`）只在本讲点到为止，下一讲（u2-l2）专门展开。

### 4.1 模块一：`init` 函数——自己造运行时、自己持有

#### 4.1.1 概念说明

回忆 u1-l1 的结论：「Cargo.toml 里依赖 gpui_tokio」和「运行期能用它」是两回事。`Tokio::spawn` 内部要从 GPUI 全局里取出 `GlobalTokio` 才能拿到 Tokio 的 `Handle`——如果没人提前把这个全局放进去，取值就会失败。

`init` 就是「把 Tokio 运行时安装进 GPUI 全局」的那一次性动作。它做的选择是：**由 gpui_tokio 自己创建一个全新的 Tokio 运行时，并且自己持有它的所有权**。这对使用者最省事——一行代码，什么都不用管；代价是运行时的线程数（2）和生命周期（跟随 GPUI App）都被写死了。如果你需要更多线程、或在 GPUI 之外也要用同一个运行时，源码文档明确指出了出路：自己建运行时，把 `Handle` 交给 `init_from_handle`。

#### 4.1.2 核心流程

`init` 的执行流程可以画成这样：

```text
调用方（如 zed 主程序 / 某个测试）
    │
    └─ gpui_tokio::init(cx)
         │
         ├─ ① Builder::new_multi_thread()     拿到多线程运行时的构建器
         ├─ ② .worker_threads(2)              只开 2 个工作线程（控制资源占用）
         ├─ ③ .enable_all()                   启用 I/O 驱动 + 定时器驱动
         ├─ ④ .build().expect(...)            生成 Runtime，线程池此刻已启动
         ├─ ⑤ runtime.handle().clone()        从 Runtime 克隆出廉价的 Handle
         └─ ⑥ cx.set_global(GlobalTokio {     把「Runtime + Handle」一起写入 GPUI 全局
                  owned_runtime: Some(runtime),
                  handle,
              })
```

三个关键直觉：

1. **④ 之后线程已经启动**。`build()` 返回的瞬间，两个 Tokio 工作线程已经在后台待命了，哪怕还没有任何任务 spawn 上去。
2. **⑤ 是「克隆遥控器」而不是「交出遥控器」**（u1-l4 讲过：`Handle` 内部是对 runtime 调度器的引用计数句柄，`clone()` 廉价）。这样 `owned_runtime` 里锁着完整的 `Runtime` 所有权，而 `handle` 字段里的克隆可以随便复制给任何想 spawn 的人。
3. **⑥ 是唯一与 GPUI 发生交互的一步**。前五步是纯 Tokio 世界的事，最后一步才把成果「挂进」GPUI 的全局表。

用所有权视角总结这个函数的契约：

\[ \text{init}(cx) \;=\; \underbrace{\text{创建 Runtime}}_{\text{获得所有权}} \;+\; \underbrace{\text{clone Handle}}_{\text{分发遥控器}} \;+\; \underbrace{\text{set\_global}(\text{Some(runtime)},\ \text{handle})}_{\text{所有权入库，遥控器上架}} \]

#### 4.1.3 源码精读

先看完整函数（含文档注释）：

[crates/gpui_tokio/src/gpui_tokio.rs:L8-L25](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L8-L25) —— 文档注释说明了分工：需要更多线程或需要在 GPUI 之外访问运行时，就自己建 runtime 然后调 `init_from_handle`；函数体先建 2 线程运行时，再连同 Handle 一起写入全局。

```rust
/// Initializes the Tokio wrapper using a new Tokio runtime with 2 worker threads.
///
/// If you need more threads (or access to the runtime outside of GPUI), you can create the runtime
/// yourself and pass a Handle to `init_from_handle`.
pub fn init(cx: &mut App) {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        // Since we now have two executors, let's try to keep our footprint small
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("Failed to initialize Tokio");

    let handle = runtime.handle().clone();
    cx.set_global(GlobalTokio {
        owned_runtime: Some(runtime),
        handle,
    });
}
```

逐段拆开看：

**Builder 链（四步构建）**

[crates/gpui_tokio/src/gpui_tokio.rs:L13-L18](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L13-L18) —— 用 `new_multi_thread` + `worker_threads(2)` + `enable_all` 构建运行时，`expect` 在构建失败时直接 panic。

- `.worker_threads(2)` 上方那行注释值得细读："Since we now have two executors, let's try to keep our footprint small"——**two executors 指 GPUI 自己的执行器（前台单线程 + 后台线程池）和即将创建的 Tokio 运行时**。Zed 进程里同时活着两套异步系统，Tokio 这边刻意只开 2 个线程，避免线程数膨胀。这也回应了 u1-l2 的观察：为什么这个 crate 明明很小，却值得单独存在——线程预算是被认真设计过的。
- `.enable_all()` 是 u1-l4 实验验证过的：不启用驱动，`tokio::time::sleep` 会直接 panic。reqwest、tokio-tungstenite 这些库都依赖 I/O 和定时器驱动，所以这一行必不可少。
- `.build()` 返回 `Result<Runtime, Error>`，这里用 `.expect("Failed to initialize Tokio")` 直接 panic。这是**启动期一次性初始化**的典型取舍：运行时都建不起来，进程没有继续的意义，fail-fast 比返回一个「全局里没有 Tokio」的延迟炸弹更诚实。（对比 CLAUDE.md 的编码规范——平时避免 panic，但启动失败的 fail-fast 是例外场景。）

**Handle 克隆与写入全局**

[crates/gpui_tokio/src/gpui_tokio.rs:L20-L24](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L20-L24) —— 先从 `runtime` 克隆出 `handle`，然后把 `owned_runtime: Some(runtime)`（所有权入库）和 `handle`（遥控器上架）一起交给 `set_global`。

注意顺序：必须**先 clone 再 move**。`runtime` 在第 22 行被 move 进结构体后，第 20 行已经拿到的 `handle` 独立于 `runtime` 的生命周期（Handle 内部是引用计数，Runtime 被 Box 装进全局后 Handle 依然有效）。

**真实调用现场：Zed 主程序**

[crates/zed/src/main.rs:L485-L499](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs#L485-L499) —— `gpui_tokio::init(cx)` 位于 `app.run` 闭包内，紧跟在 `release_channel::init` 之后。

```rust
app.run(move |cx| {
    // ...（数据库、可信路径等初始化）
    release_channel::init(app_version, cx);
    gpui_tokio::init(cx);          // ← 第 499 行：Tokio 运行时在此上岗
    if let Some(app_commit_sha) = app_commit_sha {
        AppCommitSha::set_global(app_commit_sha, cx);
    }
    settings::init(cx);
    // ...
```

它在启动序列里的位置很有讲究：**在 `settings::init`、HTTP 客户端创建等一切可能用到 Tokio 的步骤之前**。往后翻 16 行就能看到第一个消费者：

[crates/zed/src/main.rs:L515-L521](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs#L515-L521) —— 用 `Tokio::handle(cx).enter()` 进入运行时上下文后创建 reqwest HTTP 客户端（`let _guard` 是保留 RAII 守卫直到作用域结束的惯用法，细节留到 u3-l1）。

除了桌面主程序，远程服务器进程同样要走这一步（[crates/remote_server/src/server.rs:L654](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/remote_server/src/server.rs#L654)），example 程序也一样（[crates/livekit_client/examples/test_app.rs:L33](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/livekit_client/examples/test_app.rs#L33)）。**规律是：任何一个以 GPUI 为壳、又需要 Tokio 的进程，都要在自己的装配序列里调用一次 init。**

#### 4.1.4 代码实践

**实践目标**：亲眼看到「不 init 就 spawn」会发生什么，从而理解 init 那一步 `set_global` 的必要性。

**操作步骤**：

1. 在本地仓库找一个已经依赖 `gpui`（含 test-support）和 `gpui_tokio` 的 crate，例如 `agent` 或 `extension_host`（也可以在仓库外自建一个 scratch crate，用 path 依赖指向本仓库；注意 `gpui_tokio` 自身没有 dev-dependencies，[crates/gpui_tokio/Cargo.toml:L15-L20](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L15-L20) 里只有普通依赖，所以不建议直接在它里面写测试）。本实验是本地练习，不提交。
2. 在它的测试模块里加一个最小测试（**示例代码**）：

```rust
#[gpui::test]
async fn test_spawn_without_init_panics(cx: &mut TestAppContext) {
    // 第一步：故意不调用 gpui_tokio::init(cx)，直接 spawn
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        cx.update(|cx| {
            gpui_tokio::Tokio::spawn(cx, async { 1 + 1 });
        })
    }));
    assert!(result.is_err(), "未 init 时 spawn 应当 panic");

    // 第二步：补上 init，再 spawn 一次
    cx.update(gpui_tokio::init);
    let task = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 1 + 1 }));
    let value = task.await.unwrap();
    assert_eq!(value, 2);
}
```

3. 用 `cargo test -p <你选的 crate> test_spawn_without_init_panics` 运行（若在 scratch crate 里则 `cargo test`）。

**需要观察的现象**：

- 第一步的 panic 消息里应该出现类似 `no state of type ... GlobalTokio ... exists` 的字样——这正是 [crates/gpui/src/app.rs:L1995-L2002](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1995-L2002) 中 `App::global` 在全局缺失时的 panic 文案（`Tokio::spawn` 经由 `AppContext::read_global` 最终落到这里，见 [crates/gpui/src/app.rs:L2867-L2873](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2867-L2873)）。
- 第二步 init 之后，同样的 spawn 代码立刻可用。

**预期结果**：测试通过，两条断言都成立。panic 的具体文案以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init` 里必须调用 `.enable_all()`？去掉它会伤害哪些下游库？

**参考答案**：`Builder` 默认不启用任何驱动，`.enable_all()` 同时启用 I/O 驱动和定时器驱动。没有定时器驱动，任务里的 `tokio::time::sleep` 会 panic；没有 I/O 驱动，reqwest、tokio-tungstenite 这类网络库无法工作。gpui_tokio 的下游（网络请求、websocket、LiveKit）全都依赖这两类驱动。

**练习 2**：为什么 `init` 的签名是 `&mut App`，而后面会讲到的 `Tokio::handle` 只需要 `&App`？

**参考答案**：`init` 要调用 `cx.set_global(...)`，而 `set_global` 是 `&mut self` 方法（写入全局表是变异操作，见 [crates/gpui/src/app.rs:L2034-L2039](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2034-L2039)）；`Tokio::handle` 只读取全局（`global` 是 `&self` 方法），自然只需不可变引用。签名即文档：一看 `&mut` 就知道这是「装配期」函数。

**练习 3**：`worker_threads(2)` 的注释说 "we now have two executors"，这两个 executor 分别是什么？

**参考答案**：一个是 GPUI 自己的执行器体系（前台单线程 ForegroundExecutor + 后台 BackgroundExecutor 线程池，u1-l3 讲过），另一个是 `init` 即将创建的 Tokio 多线程运行时。Zed 进程同时供养两套异步系统，所以 Tokio 侧刻意压缩到 2 个工作线程以控制总线程占用。

### 4.2 模块二：`GlobalTokio` 结构体定义——全局单例里存了什么

#### 4.2.1 概念说明

`init` 的最后一步是把一个 `GlobalTokio` 塞进 GPUI 的全局表。要理解这一步，得先回答两个问题：GPUI 的「全局」到底是什么？`GlobalTokio` 为什么长成 `Option<Runtime> + Handle` 这个形状？

**GPUI 的全局**是一个「以类型为键的注册表」：每个实现了 `Global` marker trait 的类型，在一个 `App` 实例里最多对应一份值，任何上下文都能按类型取用。它就是 GPUI 版的「应用级单例」，但作用域是单个 `App` 而不是整个进程——这正是 u1-l3 强调「每个测试的全局状态互相独立」的原因。

**`GlobalTokio` 的形状**则是本讲的点睛之处：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `owned_runtime` | `Option<tokio::runtime::Runtime>` | `Some` = 运行时归我管（init 路径）；`None` = 运行时是别人的（init_from_handle 路径） |
| `handle` | `tokio::runtime::Handle` | 永远可用的「遥控器」，spawn 只需要它 |

也就是说，**这个 `Option` 是两种初始化路径共用同一个全局类型的钥匙**：不管运行时从哪来，取出来用的人只关心 `handle`，于是 `Tokio::spawn`、`Tokio::handle` 的代码对两条路径完全统一。

#### 4.2.2 核心流程

`set_global` 写入与读取的全生命周期：

```text
写入（init / init_from_handle 调用 cx.set_global）
    │
    ├─ 以 TypeId::of::<GlobalTokio>() 为键
    ├─ 把值 Box 化后插入 HashMap
    └─ 若该键已有旧值 → 旧值在此刻被 drop（覆盖语义！）
              │
              ▼
读取（Tokio::spawn / Tokio::handle）
    │
    ├─ cx.read_global(|tokio: &GlobalTokio, cx| ...)
    │       └─ 内部调用 App::global::<GlobalTokio>()
    │           ├─ 全局存在 → 返回 &GlobalTokio
    │           └─ 全局缺失 → panic!("no state of type ... exists")
    └─ 只用 tokio.handle 字段去 spawn
```

`GlobalTokio` 的两种合法状态可以看作一个二值状态机：

\[ \text{GlobalTokio} = \begin{cases} (\text{Some}(rt),\ h) & \text{init 路径：runtime 所有权在全局里，App 销毁时统一关停} \\[4pt] (\text{None},\ h) & \text{init\_from\_handle 路径：runtime 所有权在调用方，全局只是遥控器架} \end{cases} \]

#### 4.2.3 源码精读

**结构体定义**

[crates/gpui_tokio/src/gpui_tokio.rs:L35-L40](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L35-L40) —— 定义了两个字段的结构体，并实现空的 `Global` marker trait。

```rust
struct GlobalTokio {
    owned_runtime: Option<tokio::runtime::Runtime>,
    handle: tokio::runtime::Handle,
}

impl Global for GlobalTokio {}
```

注意两点：

- `struct GlobalTokio` 是**私有**的（没有 `pub`）。外部世界不需要知道全局里装的是什么，只能通过 `init` / `init_from_handle` 写入、通过 `Tokio` 的静态方法读取——这是一个完整的封装闭环。
- `impl Global for GlobalTokio {}` 花括号里是空的。[crates/gpui/src/global.rs:L22-L27](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L22-L27) —— `Global` 是一个刻意留空的 marker trait，实现它只是向 GPUI 的全局表「报名」，能力由 `ReadGlobal` 等附带 trait 的 blanket impl 提供（[crates/gpui/src/global.rs:L30-L41](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L30-L41)，`GlobalTokio::global(cx)` 这种写法就来自这里，`Tokio::handle` 在用它）。

**写入端：`App::set_global`**

[crates/gpui/src/app.rs:L2034-L2039](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2034-L2039) —— 以类型 `TypeId` 为键把值 `Box` 化后插入全局表，并通知观察者。

```rust
pub fn set_global<G: Global>(&mut self, global: G) {
    let global_type = TypeId::of::<G>();
    self.push_effect(Effect::NotifyGlobalObservers { global_type });
    self.globals_by_type.insert(global_type, Box::new(global));
}
```

两个细节：

- `globals_by_type` 是 `HashMap<TypeId, Box<dyn Any>>` 风格的表——「以类型为键」意味着**同一种类型全局只能有一份**，第二次 `set_global` 会**覆盖**第一次（`HashMap::insert` 的语义）。被覆盖的旧值在这行语句结束时 drop——对 `GlobalTokio` 来说就是触发它的 `Drop` 实现（[crates/gpui_tokio/src/gpui_tokio.rs:L42-L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48)，内容下一讲专讲）。所以「重复 init」不是报错，而是「换一台新电视机，旧的当场关停」。
- `push_effect(NotifyGlobalObservers)` 说明 set_global 是可被观察的——这在 u2-l2 讲 GPUI Global 机制时会用到。

**读取端：缺失即 panic**

[crates/gpui/src/app.rs:L1995-L2009](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1995-L2009) —— `global` 在全局缺失时 panic，`try_global` 返回 `Option`。

```rust
pub fn global<G: Global>(&self) -> &G {
    self.globals_by_type
        .get(&TypeId::of::<G>())
        .map(|any_state| any_state.downcast_ref::<G>().unwrap())
        .unwrap_or_else(|| panic!("no state of type {} exists", type_name::<G>()))
}
```

这解释了 4.1.4 实践里观察到的 panic：`Tokio::spawn` → `cx.read_global`（[crates/gpui/src/app.rs:L2867-L2873](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2867-L2873)，默认实现转手调用 `global::<G>()`）→ 全局表里没有 `GlobalTokio` → panic。**gpui_tokio 选择「忘 init 就炸」而不是返回错误**，因为「没初始化」是装配顺序 bug，fail-fast 最容易定位。

#### 4.2.4 代码实践

**实践目标**：验证 `set_global` 的覆盖语义——对同一个 App 调用两次 `init`，观察第一个运行时何时被关停。

**操作步骤**：

1. 在与 4.1.4 相同的测试环境里加第二个测试（**示例代码**）：

```rust
#[gpui::test]
async fn test_double_init_replaces_runtime(cx: &mut TestAppContext) {
    // 第一次 init：runtime A 上岗
    cx.update(gpui_tokio::init);
    let task_a = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 10 }));
    // 立刻 await，确保任务在 runtime A 被替换前完成
    assert_eq!(task_a.await.unwrap(), 10);

    // 第二次 init：runtime B 覆盖 runtime A
    cx.update(gpui_tokio::init);
    let task_b = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 20 }));
    assert_eq!(task_b.await.unwrap(), 20);
}
```

2. 运行该测试。

**需要观察的现象**：

- 测试应当通过：task_a 在覆盖前已完成，task_b 跑在新 runtime B 上。
- 值得思考的边界（可作为延伸实验）：如果把 `task_a` 的 await 挪到第二次 init **之后**，task_a 会怎样？根据覆盖语义，runtime A 在第二次 `set_global` 那一行就被 drop 并 `shutdown_background`，task_a 大概率会收到 `JoinError`（取消）而不是 10。

**预期结果**：主路径测试通过；延伸实验中 task_a 的具体错误形态（cancelled JoinError）待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`GlobalTokio` 为什么设计成私有 struct，而 `init` 是 pub fn？

**参考答案**：封装。调用方只需要「初始化」和「使用」（`Tokio::spawn` 等），完全不需要感知全局里的数据形状。把 `GlobalTokio` 藏起来后，未来即使加字段、改内部表示，也不会破坏任何下游代码——本 crate 约 14 个依赖方没有一个直接触碰这个类型。

**练习 2**：如果对同一个 `App` 调用两次 `gpui_tokio::init`，第一次创建的 runtime 什么时候结束生命？

**参考答案**：第二次 `set_global` 执行 `globals_by_type.insert` 时，旧值被替换并在该语句结束处 drop，随即触发 `GlobalTokio::Drop`，其中对 `owned_runtime` 执行 `shutdown_background()`（异步、非阻塞地关停 runtime A 的线程）。也就是说「换新即关旧」，且不会卡住主线程。

**练习 3**：`App::global` 与 `App::try_global` 的区别是什么？gpui_tokio 为什么用会 panic 的那个？

**参考答案**：`global` 在全局缺失时 panic，`try_global` 返回 `Option<&G>`（[crates/gpui/src/app.rs:L2004-L2009](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2004-L2009)）。gpui_tokio 经由 `read_global` 走的是 panic 版：缺 `GlobalTokio` 意味着装配顺序错了（忘了 init），这是 bug 而不是可恢复的运行时状况，fail-fast 并带上类型名的 panic 消息最利于定位。

### 4.3 模块三：`init_from_handle`——只借遥控器、不买电视机

#### 4.3.1 概念说明

如果说 `init` 是「让 gpui_tokio 替你买一台电视机并保管它」，`init_from_handle` 就是「你自己已经有电视机，只是把遥控器复制一份放在 gpui_tokio 的架子上」。

它的存在回答两个 `init` 覆盖不到的需求：

1. **需要更多线程**：2 个工作线程不够用（例如要跑大量并发的 CPU 密集 Tokio 任务），调用方可以自己 `Builder::new_multi_thread().worker_threads(N)` 建一个更大的。
2. **需要在 GPUI 之外访问同一个运行时**：有些代码不跑在 GPUI 上下文里（独立的命令行逻辑、别的线程），但也想和 GPUI 共享同一个 Tokio 运行时，避免供养两套线程池。谁创建、谁分发 Handle，`init_from_handle` 负责把其中一个 Handle 挂进 GPUI 全局。

代价是**生命周期责任转移**：`owned_runtime: None` 意味着 `GlobalTokio` 被 drop 时不会（也不能）去关停运行时——**关停的责任完全在创建 runtime 的那一方**。调用方必须保证：在 drop 自己的 runtime 之前，GPUI 侧没有还在运行的 Tokio 任务。

一个值得知道的事实：在整个 Zed 仓库里搜索 `init_from_handle`，除了它自己的定义和文档引用，**没有任何调用点**。它是刻意保留的公开「逃生舱」API——Zed 自身用 `init` 就够了，但这个 crate 是 `publish = false` 的内部 crate，保留这个口子为嵌入方和未来需求服务。

#### 4.3.2 核心流程

两条初始化路径的对比流程：

```text
路径一：init（自带电视机）
    Builder::new_multi_thread().worker_threads(2).enable_all().build()
        → 得到 runtime（所有权在本函数栈上诞生）
        → handle = runtime.handle().clone()
        → set_global(GlobalTokio { owned_runtime: Some(runtime), handle })
    生命周期：runtime 与 GPUI App 同生共死，App 销毁时 shutdown_background

路径二：init_from_handle（只放遥控器）
    调用方自己 build runtime（线程数、驱动、关停时机全由调用方定）
        → handle = runtime.handle().clone()   （调用方自己做）
        → init_from_handle(cx, handle)
        → set_global(GlobalTokio { owned_runtime: None, handle })
    生命周期：GlobalTokio 被 drop 时什么都不关停；
              runtime 的关停由调用方在自己的代码里负责
```

所有权流向图：

```text
init:              gpui_tokio（全局） ──拥有──> Runtime ──派生──> Handle
                                                        （可自由 clone）

init_from_handle:  调用方 ──拥有──> Runtime ──派生──> Handle ──clone──> 全局
                   （全局只有 Handle，Runtime 与全局无所有权关系）
```

#### 4.3.3 源码精读

[crates/gpui_tokio/src/gpui_tokio.rs:L27-L33](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L27-L33) —— `init_from_handle` 只把外部传入的 Handle 存进全局，`owned_runtime` 留空。

```rust
/// Initializes the Tokio wrapper using a Tokio runtime handle.
pub fn init_from_handle(cx: &mut App, handle: tokio::runtime::Handle) {
    cx.set_global(GlobalTokio {
        owned_runtime: None,
        handle,
    });
}
```

只有 6 行，但每处都有信息量：

- **参数按值接收 `handle`**：`Handle` 克隆廉价，调用方传一个克隆进来，自己保留原份继续使用——这正是「共享同一个运行时」的实现方式。
- **`owned_runtime: None`**：这个 `None` 就是 4.2.1 说的状态机的另一态。注意 `Drop` 实现（[crates/gpui_tokio/src/gpui_tokio.rs:L42-L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48)）里是 `if let Some(runtime) = self.owned_runtime.take()`——`None` 时这个分支直接跳过，App 退出时**绝不会**误关调用方的运行时。`Option` 在这里同时承担了「数据」和「该不该由我关停」两重语义。
- **签名同样是 `&mut App`**：和 `init` 一样要走 `set_global` 写全局。

对比两个函数的差异表：

| 维度 | `init` | `init_from_handle` |
| --- | --- | --- |
| 谁创建 Runtime | gpui_tokio | 调用方 |
| worker 线程数 | 固定 2 | 调用方指定 |
| `owned_runtime` | `Some(runtime)` | `None` |
| App 销毁时的关停 | 自动 `shutdown_background` | 不关停，调用方负责 |
| 外部能否共享同一运行时 | 不能（Runtime 被锁在全局里） | 能（Handle 可多方 clone） |
| 仓库内调用点 | 20+ 处（main.rs:499、各测试等） | 0 处（逃生舱 API） |

#### 4.3.4 代码实践

**实践目标**：走通 `init_from_handle` 路径，并体会「runtime 所有权在调用方手里」意味着什么。

**操作步骤**：

1. 在同一测试环境里加第三个测试（**示例代码**）：

```rust
#[gpui::test]
async fn test_init_from_handle_with_own_runtime(cx: &mut TestAppContext) {
    // 调用方自己造一台"电视机"：这次只给 1 个工作线程，证明线程数可自定
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let handle = runtime.handle().clone();

    // 只把遥控器交给 GPUI 全局
    cx.update(|cx| gpui_tokio::init_from_handle(cx, handle));

    let task = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 7 * 6 }));
    assert_eq!(task.await.unwrap(), 42);

    // runtime 的所有权始终在本测试函数手里：函数结束、runtime drop，主动关停
    // （而不是等 App 销毁）
}
```

2. 运行测试，确认通过。
3. 延伸实验（可选）：把 `drop(runtime);` 显式加在 `Tokio::spawn` **之前**，再尝试 spawn 一个带 `tokio::time::sleep` 的任务并观察结果。

**需要观察的现象**：

- 主路径：任务正常返回 42，说明 `None + 外部 Handle` 的全局完全可用，`Tokio::spawn` 的代码路径与 `init` 路径毫无差别（它只读 `handle` 字段）。
- 延伸实验：runtime 被 drop 后进入关停流程，此后在其 Handle 上 spawn 的任务预期拿不到正常结果（可能表现为 JoinError/取消）。

**预期结果**：主路径断言通过；延伸实验的具体错误形态待本地验证。延伸实验也再次提醒：**用 `init_from_handle` 时，先确保 GPUI 侧任务都结束，再 drop runtime**。

#### 4.3.5 小练习与答案

**练习 1**：用 `init_from_handle` 初始化后，GPUI App 正常退出，外部调用方创建的 runtime 会被关停吗？

**参考答案**：不会。`owned_runtime` 是 `None`，`Drop` 里的 `if let Some(...)` 分支不触发。App 销毁只是把「遥控器架子」（GlobalTokio）扔了，电视机（Runtime）还在调用方手里，关停与否由调用方决定。

**练习 2**：既然仓库里没有任何地方调用 `init_from_handle`，为什么还要保留它？

**参考答案**：它是刻意的扩展点（escape hatch）。源码文档写明了用途：需要更多线程、或需要在 GPUI 之外访问同一运行时时使用。删除它意味着这两个需求将来无路可走；保留它的成本几乎为零（6 行代码、无依赖增量），而它让这个 crate 的 API 覆盖了「runtime 所有权」的全部两种安排。

**练习 3**：假设你在一个混合架构的程序里，既有独立的 Tokio 逻辑（不在 GPUI 上下文里），又有 GPUI UI，两者要做网络请求。选 `init` 还是 `init_from_handle`？为什么？

**参考答案**：选 `init_from_handle`。自己创建一个 runtime，把 Handle 的克隆交给 GPUI 全局，独立逻辑继续用同一个 runtime 的 Handle/`enter`。这样整个进程只供养一套 Tokio 线程池，避免两个运行时各开线程、且无法共享定时器/I/O 驱动的浪费；同时关停时机由你统一掌控。

## 5. 综合实践

**任务**：写一个完整的 GPUI headless 测试文件，把两条初始化路径串起来验证，并用注释写清所有权差异。这也是本讲规格里指定的实践任务。

**操作步骤**：

1. 选择落点：在仓库外自建一个 scratch crate（例如 `gpui_tokio_lab`），`Cargo.toml` 里用 path 依赖指向本仓库的 `gpui`（开启 `test-support` feature）、`gpui_tokio`、`tokio`；或者直接在 `agent` / `extension_host` 这类已具备条件的 crate 的测试模块里临时添加（本地实验，不提交）。真实仓库里的测试写法可以参照 [crates/agent/src/tests/mod.rs:L4174-L4191](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/agent/src/tests/mod.rs#L4174-L4191)（`cx.update(|cx| { gpui_tokio::init(cx); ... })`）。
2. 写如下测试（**示例代码**，一个测试覆盖两条路径）：

```rust
#[gpui::test]
async fn test_both_init_paths(cx: &mut TestAppContext) {
    // ── 路径一：gpui_tokio::init ──────────────────────────────
    // gpui_tokio 自己创建 runtime（2 个工作线程）并持有所有权
    cx.update(gpui_tokio::init);
    let from_init = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 1 + 1 }));
    assert_eq!(from_init.await.unwrap(), 2);
    // 所有权：runtime 存在 GlobalTokio.owned_runtime = Some(..) 里，
    // App 销毁时才 shutdown_background。

    // ── 路径二：init_from_handle ──────────────────────────────
    // 我们自己创建 runtime（线程数自定），只把 Handle 交给全局
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    cx.update(|cx| gpui_tokio::init_from_handle(cx, runtime.handle().clone()));
    let from_handle = cx.update(|cx| gpui_tokio::Tokio::spawn(cx, async { 2 + 2 }));
    assert_eq!(from_handle.await.unwrap(), 4);
    // 所有权：runtime 在本函数手里，GlobalTokio.owned_runtime = None，
    // 关停责任在我们：函数结束时 runtime drop（阻塞式 shutdown）。
    //
    // 注意：这次 set_global 覆盖了路径一写入的全局，
    // 路径一的 runtime 已在此刻被 shutdown_background（见 4.2 覆盖语义）。
}
```

3. 运行：`cargo test`（scratch crate 内），或 `cargo test -p <crate> test_both_init_paths`。

**需要观察的现象与预期结果**：

- 两条路径的 `Tokio::spawn` 都成功返回结果（2 和 4），证明 `Tokio::spawn` 对初始化方式完全无感——它只依赖全局里的 `handle` 字段。
- 在代码注释里回答所有权问题（这就是本实践的「交付物」）：
  - `init`：runtime 的所有权进入 GPUI 全局，生命周期 = App 的生命周期，关停时机 = App 销毁，方式 = `shutdown_background`（异步）。
  - `init_from_handle`：runtime 的所有权留在调用方，全局只持 Handle；关停时机和方式完全由调用方决定（示例中是函数返回时的阻塞式 drop）；`GlobalTokio::Drop` 不会碰它。
- 若运行结果与预期不符（例如第二段 await 拿到 JoinError），回到 4.2.4 的覆盖语义分析原因。测试的完整运行输出待本地验证。

## 6. 本讲小结

- `gpui_tokio::init` 六步走：`new_multi_thread` → `worker_threads(2)`（注释点明「两套执行器共存，压缩占用」）→ `enable_all()`（I/O + 定时器驱动）→ `build().expect()`（启动期 fail-fast）→ `runtime.handle().clone()`（克隆遥控器）→ `set_global`（所有权与遥控器一起入库）。
- Zed 主程序在 `crates/zed/src/main.rs:499`、`app.run` 闭包内、`settings::init` 之前调用 init；远程服务器（remote_server）和各 example 也各自调用——规律是「以 GPUI 为壳且需要 Tokio 的进程都要 init 一次」。
- `GlobalTokio { owned_runtime: Option<Runtime>, handle: Handle }` 是私有全局单例；`Option` 的 `Some/None` 恰好编码了「runtime 归我管 / runtime 是别人的」两种状态，让两条初始化路径共用同一套读取代码（`Tokio::spawn`、`Tokio::handle` 只看 `handle` 字段）。
- GPUI 全局是以 `TypeId` 为键的表：`set_global` 插入（覆盖旧值并触发其 Drop），`global` 缺失即 panic——所以「忘 init 就 spawn」会以 `no state of type ... exists` 崩溃，这是刻意的 fail-fast。
- `init_from_handle` 只存外部 Handle、不持有 runtime；关停责任在调用方。当前仓库内没有调用点，它是为零成本保留的逃生舱 API。

## 7. 下一步学习建议

本讲只拆开了 `GlobalTokio` 的「定义与写入」，刻意留下了两块拼图：

1. **下一讲 u2-l2（GlobalTokio 与 GPUI Global 机制、生命周期）**：专讲 `impl Drop for GlobalTokio` 里的 `shutdown_background` 为什么是异步关停而不是阻塞 `shutdown`、GPUI `Global` trait 体系的完整设计（`ReadGlobal`、`UpdateGlobal`、全局观察者），以及 App 销毁时全局表的清理顺序。建议先自己读一遍 [crates/gpui_tokio/src/gpui_tokio.rs:L42-L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48) 带着问题去听。
2. **u2-l3（Tokio::spawn 桥接全流程）**：本讲反复说「spawn 只依赖全局里的 handle 字段」，下一讲就顺着 `Tokio::spawn` 把 `read_global → handle.spawn → abort_handle → defer → background_spawn` 五步走完。
3. **延伸阅读**：Zed 启动序列里 init 的第一个消费者——[crates/zed/src/main.rs:L515-L521](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs#L515-L521) 的 `Tokio::handle(cx).enter()` 创建 reqwest 客户端——那是 u3-l1 的主题，现在混个眼熟即可。
