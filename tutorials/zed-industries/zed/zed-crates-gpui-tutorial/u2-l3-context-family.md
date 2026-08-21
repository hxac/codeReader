# u2-l3 Context 家族：App、Context<T> 与 AsyncApp

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GPUI 中五种上下文（`App`、`Context<T>`、`AsyncApp`、`AsyncWindowContext`、`TestAppContext`）各自的能力边界，以及它们之间的 Deref / 弱引用关系。
2. 解释 `Context<T>` 的本质：一个 `&mut App` 加上一个 `WeakEntity<T>`，以及它为什么只在实体更新期间短暂存在。
3. 掌握由 `notify`/`observe` 与 `emit`/`subscribe`（`EventEmitter`）构成的两条响应式通知路径，并能说出它们在效果队列（`Effect`）中的触发时机。
4. 会用 `cx.listener` 把元素事件回调（`Fn(&E, &mut Window, &mut App)`）适配成绑定到实体方法的闭包。
5. 理解 `AsyncApp` 为什么能跨 `await` 点持有、以及它的方法为什么每次都要短暂借用 `App`。

## 2. 前置知识

本讲建立在 u2-l1 和 u2-l2 之上，先用三段话补齐需要的概念。

**上下文参数（cx）是什么？** GPUI 里几乎所有与框架打交道的函数都有一个名为 `cx` 的参数。它不是全局变量，而是一个普通的引用/值参数，通过它才能访问实体、窗口、全局状态和执行器。官方文档 [docs/contexts.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L1-L4) 的第一句就是：GPUI 大量使用「上下文参数」（通常命名为 `cx`）来提供对应用状态与服务的访问。

**实体与租约（复习 u2-l2）。** App 拥有一切实体，`Entity<T>` 只是「EntityId + 类型标签」的句柄。读写实体必须走 `read` / `update`，更新时实体的状态会被「搬出」实体表租给闭包使用，嵌套 update 同一实体会 panic。本讲会看到 `Context<T>` 正是这个租约机制的载体。

**借用与单前台线程（复习 u2-l1）。** 前台代码全部运行在单一前台线程上，`App` 存放在 `AppCell`（即 `RefCell<App>`）里。同步代码在一次更新中独占可变借用；而异步代码只能持有弱引用、在每次实际交互时短暂借用——这就是 `AsyncApp` 存在的原因。

**两个 Rust 概念。** 其一是 `Deref`：若类型 `A` 实现了 `Deref<Target = B>`，那么 `&A` 可以自动当作 `&B` 使用，`A` 因此「继承」了 `B` 的所有方法。其二是标记 trait（marker trait）：没有方法的 trait，仅用于给类型「打标签」，让编译器掌握额外的类型信息。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/contexts.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L1-L33) | 官方上下文指南，全篇只有 34 行，是本讲的总纲 |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L311) | crate 根，定义 `AppContext`、`VisualContext`、`EventEmitter`、`BorrowAppContext` 这组顶层 trait |
| [src/app/context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L20-L23) | `Context<T>` 的定义与全部「实体专属」方法：notify、emit、observe、subscribe、listener、spawn 等 |
| [src/app/async_context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L21-L34) | `AsyncApp` 与 `AsyncWindowContext` 的定义与实现 |
| [src/app.rs](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1606-L1683) | `App` 侧的配合实现：`observe`/`subscribe` 的内部存储、`Effect` 枚举、`push_effect` 与 `flush_effects` |
| [examples/ownership_post.rs](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/ownership_post.rs#L1-L50) | 官方所有权文档配套示例，无窗口地演示 subscribe/notify/emit，是本讲实践的基底 |
| [examples/gradient.rs](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/gradient.rs#L51-L57) | `cx.listener` 在真实 UI 中的典型用法 |

## 4. 核心概念与源码讲解

### 4.1 五种上下文总览：能力分层与 Deref 关系

#### 4.1.1 概念说明

为什么要有五种上下文，而不是一个「万能 cx」？因为**能力是被生命周期约束的**：

- 有的代码只在一瞬间需要访问 `App`（同步回调），可以直接拿借用；
- 有的代码属于某个实体的更新过程，需要额外的「我是谁」信息（notify 谁、emit 谁）；
- 有的代码要跨越 `await` 点长时间存活，借用不可能活那么久，只能持有弱引用。

于是 GPUI 把上下文按「谁能用、活多久、多出什么能力」分成了五种。官方文档把它们分为四节介绍（`App`、`Context<T>`、`AsyncApp`/`AsyncWindowContext`、`TestAppContext`），并强调：前两种总是以引用形式传给你；后几种是拥有所有权、可以克隆和跨 `await` 的值。

#### 4.1.2 核心流程

五种上下文的关系可以用一张「亲缘图」概括：

```
TestAppContext（测试专用，test-support feature）
      │  行为类似异步上下文，但访问失败直接 panic
      ▼
AsyncWindowContext ──(Deref/DerefMut)──▶ AsyncApp ──(Weak<AppCell> + 每次调用短暂 borrow_mut)──▶ AppCell = RefCell<App>
      │                                      │
      │ 额外携带 AnyWindowHandle              │ 不实现 Deref 到 App
      ▼                                      ▼
窗口相关方法返回 Result               每个方法自己升级弱引用并借用

Context<T> ──(Deref/DerefMut + Borrow/BorrowMut)──▶ App
      │
      └ 额外携带 WeakEntity<T>：notify()/emit()/listener() 都作用于这个实体
```

关键结论有三条：

1. `Context<T>` 通过 `Deref`「继承」`App` 的一切方法，所以任何接收 `&App` 的函数也能接收 `&Context<T>`。
2. `AsyncWindowContext` 通过 `Deref` 继承 `AsyncApp`。
3. `AsyncApp` **不** Deref 到 `App`——它对 `App` 的每次访问都要经过「升级弱引用 → 短暂借用 `RefCell` → 立刻归还」三步，因此它的窗口类方法大多返回 `Result`（窗口可能已关闭）。

#### 4.1.3 源码精读

官方文档对四种上下文的一句话定义，是理解能力边界的最佳出发点：[docs/contexts.md:L7-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L7-L13) 说明 `App` 是访问全局状态的根上下文、拥有所有实体数据，而 `Context<T>` 在此基础上增加了与特定实体相关的方法（通知观察者、发出事件）并 Deref 到 `App`；[docs/contexts.md:L15-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L15-L21) 说明异步上下文拥有静态生命周期、可以跨 `await` 持有，代价是调用变得可失败，`TestAppContext` 则在访问失败时直接 panic 并附带测试专用功能。

把「不同上下文可以互换使用」落实到代码层的，是 crate 根上的 `AppContext` trait：[src/gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L245) 定义了 `new`、`update_entity`、`read_entity`、`update_window`、`background_spawn`、`read_global` 等一组「所有上下文都能做的基础操作」。它的文档注释直言：这个 trait 让 GPUI 的不同上下文在某些操作上可以互换使用。`App`、`Context<T>`、`AsyncApp`、`AsyncWindowContext` 都实现了它，所以 `entity.update(cx, ...)` 这类调用不关心 `cx` 具体是哪一种。

在此基础上，[src/gpui.rs:L258-L292](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L258-L292) 的 `VisualContext` trait 定义了「必须存在窗口」的上下文额外能力（`update_window_entity`、`new_window_entity`、`replace_root_view`、`focus`），并用关联类型 `type Result<T>` 让不同实现的失败策略不同——`AsyncWindowContext` 的实现是 `type Result<T> = Result<T>`（见 [src/app/async_context.rs:L486-L487](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L486-L487)），因为窗口可能已经不在了。

另一个容易忽略的细节是 [src/gpui.rs:L298-L311](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L298-L311) 的 `BorrowAppContext`：它对所有实现了 `BorrowMut<App>` 的类型做了 blanket 实现，这让 `Context<T>`（它实现了 `Borrow<App>`/`BorrowMut<App>`，见 [src/app/context.rs:L873-L883](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L873-L883)）免费获得了 `set_global` / `update_global` 等全局状态方法。

#### 4.1.4 代码实践

**实践目标**：用 grep 亲眼确认「哪些类型实现了 `AppContext`」，验证本讲的亲缘图。

1. 在 `crates/gpui` 目录下执行 `grep -rn "impl AppContext for" src/ --include="*.rs"`。
2. 再执行 `grep -rn "impl VisualContext for" src/ --include="*.rs"`。
3. 把输出的类型列表与 4.1.2 的亲缘图对照。

**需要观察的现象**：`AppContext` 的实现者应当包括 `App`、`Context<'_, T>`、`AsyncApp`、`AsyncWindowContext`（以及测试设施中的上下文）；`VisualContext` 的实现者明显更少。

**预期结果**：实现者集合与亲缘图一致。`VisualContext` 只被少数需要窗口的类型实现（如 `AsyncWindowContext` 与测试上下文）。具体输出条数「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Context<T>` 能直接调用 `cx.read_global(...)` 这种定义在 `App` 上的方法？

**答案**：因为 `Context<'a, T>` 实现了 `Deref<Target = App>` 与 `DerefMut`（[src/app/context.rs:L25-L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L25-L37)），方法解析会自动穿透到 `App`；此外 `BorrowAppContext` 的 blanket 实现（[src/gpui.rs:L313-L319](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L313-L319)）让所有 `BorrowMut<App>` 的类型都获得全局状态方法。

**练习 2**：`AsyncApp` 为什么不像 `Context<T>` 那样 Deref 到 `App`？

**答案**：`Context<T>` 持有的是真实的 `&mut App` 借用，编译器保证借用期间无人可变访问；`AsyncApp` 要跨 `await` 存活，不可能持有借用，只能持有 `Weak<AppCell>`（[src/app/async_context.rs:L21-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L21-L26)），每次方法调用时手动升级并短暂借用。Deref 语义要求返回稳定的引用，这种「按次借用」模式做不到，所以不实现 Deref。

### 4.2 Context\<T\>：绑定到单个实体的上下文

#### 4.2.1 概念说明

`Context<T>` 回答的问题是：「我正在更新实体 T，给我一套顺手的方法」。它的定义只有两个字段：

- `app: &'a mut App`——可变借用整个应用状态；
- `entity_state: WeakEntity<T>`——一个指向**当前实体**的弱句柄。

注意第二个字段是**弱**句柄。因为 `Context<T>` 存在期间实体正在被更新，实体本身一定存活，用弱句柄不是为了防泄漏，而是复用 `WeakEntity` 这个「EntityId + 类型标签」的轻量结构，方便克隆给观察者回调使用。

生命周期上，`Context<T>` 是昙花一现的：`cx.new(|cx| ...)` 的闭包参数、`entity.update(cx, |state, cx| ...)` 的第二个参数，都是它仅有的两个诞生地。闭包结束，`Context` 连同 `&mut App` 借用一起消失。这也是 CLAUDE.md 中「闭包内的 cx 必须用内层 cx」的根源——外层 `cx` 的借用已经交给了这次更新。

#### 4.2.2 核心流程

以 `cx.new(|cx| Counter { count: 0 })` 为例：

```
调用 cx.new(build)
  └─ App::new（app.rs）
       ├─ entities.reserve()          → 先在实体表预留槽位，拿到 slot
       ├─ Context::new_context(cx, slot.downgrade())
       │    └─ 用「槽位的弱化形式 + &mut App」构造 Context<T>
       ├─ build_entity(&mut Context<T>)  → 你的闭包在这里执行
       │    └─ 闭包内可以 cx.observe(...) 订阅别人，甚至引用自己将来的 EntityId
       ├─ push_effect(Effect::EntityCreated { .. })  → 记录“实体已创建”效果
       └─ entities.insert(slot, entity) → 状态真正放进实体表，返回 Entity<T>
```

两阶段（先预留、后插入）的收益在 u2-l2 已讲过：构造期间就能拿到自己未来的 `EntityId` 与弱句柄，`Context::weak_entity()`（[src/app/context.rs:L57-L59](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L57-L59)）让构造闭包可以把自己的弱句柄交给观察者回调。

#### 4.2.3 源码精读

结构定义：[src/app/context.rs:L19-L23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L19-L23) —— 文档注释称其为「针对给定实体有特化行为的 app 上下文」，字段就是前面分析的两个。

「我是谁」三件套：[src/app/context.rs:L44-L59](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L44-L59) 提供了 `entity_id()`（返回当前实体的 `EntityId`）、`entity()`（升级为强句柄，实体必然存活所以直接 `expect`）、`weak_entity()`（克隆弱句柄）。后续会看到，本文件里几乎所有注册回调的方法第一步都是 `let this = self.weak_entity();`。

诞生地之一：[src/app.rs:L2714-L2733](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2714-L2733) 是 `impl AppContext for App` 中的 `new` 方法，完整展示了「reserve → 构造 `Context` → 执行闭包 → push EntityCreated 效果 → insert」的全过程。

「继承 App」的落地：[src/app/context.rs:L782-L871](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L782-L871) 是 `Context` 对 `AppContext` trait 的实现——通篇都是一行式转发（`self.app.xxx()`）。这印证了：`Context<T>` 的基础能力全部来自 `App`，自己新增的只有「实体专属」那一层。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「`Context<T>` 只存在于更新/构造闭包中，且能报告自己绑定的实体」。

1. 打开 [examples/ownership_post.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/ownership_post.rs#L16-L37)，在本地复制一份（例如存为 `examples/context_probe.rs`，示例文件会被 cargo 自动发现）。
2. 在 `let counter: Entity<Counter> = cx.new(|_cx| Counter { count: 0 });` 之后加一行（示例代码）：
   ```rust
   println!("counter 的实体 id = {}", counter.entity_id().as_u64());
   ```
3. 把 subscriber 的构造闭包改成带类型标注的形式，并在开头打印自己的 id（示例代码）：
   ```rust
   let subscriber = cx.new(|cx: &mut Context<Counter>| {
       println!("构造期间，我自己的实体 id = {}", cx.entity_id().as_u64());
       // ……原有的 subscribe 代码保持不变……
   });
   ```
4. 运行 `cargo run -p gpui --example context_probe`（该示例不创建窗口，跑完回调即结束；若进程未自动退出，Ctrl-C 结束即可——具体行为「待本地验证」）。

**需要观察的现象**：两行日志输出不同的实体 id；构造闭包里通过 `cx.entity_id()` 就能拿到「正在被构造的自己」的编号。

**预期结果**：证明 `Context<T>` 在实体状态尚未插入实体表时就已经携带着它的身份信息，这正是两阶段创建的价值。

#### 4.2.5 小练习与答案

**练习 1**：`Context::entity()` 内部是 `self.weak_entity().upgrade().expect(...)`，为什么这里敢用 `expect`（panic）而 `WeakEntity::update` 要返回 `Result`？

**答案**：`Context<T>` 只在实体的构造/更新闭包内存在，此时实体必然存活（u2-l2：实体在被更新期间被租约保护），upgrade 不可能失败，`expect` 的信息也写明了这一点（[src/app/context.rs:L50-L54](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L50-L54)）。而 `WeakEntity` 可能被任何人长期持有，实体可能早已释放，所以必须以 `Result` 表达失败。

**练习 2**：能否把 `Context<T>` 存到结构体字段里，下次更新时复用？

**答案**：不能。`Context<'a, T>` 携带 `&'a mut App`，生命周期被限制在单次更新内；想跨更新持有对实体的引用，应保存 `Entity<T>`（强句柄）或 `WeakEntity<T>`（弱句柄），下次再经 `update` 重新获得 `Context`。

### 4.3 EventEmitter 与两条通知路径：notify/observe、emit/subscribe

#### 4.3.1 概念说明

实体之间需要通信。GPUI 提供了两条语义不同的通道：

| 通道 | 载荷 | 语义 | 发送方 | 接收方 |
| --- | --- | --- | --- | --- |
| notify → observe | 无 | 「我变了」，观察者应重读我的状态 | `cx.notify()`（作用于当前实体） | `cx.observe(&entity, ...)` |
| emit → subscribe | 有（任意 `Evt` 类型） | 「发生了具体某件事」，携带事件数据 | `cx.emit(event)` | `cx.subscribe(&entity, ...)` |

两条通道的分界由 `EventEmitter` 标记 trait 声明：想让实体 `T` 能发出 `Evt` 类型的事件，就写 `impl EventEmitter<Evt> for T {}`。它是零方法的标记 trait（[src/gpui.rs:L294-L296](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L294-L296)），作用纯粹是类型层面的：「把实体类型和它能发的事件类型绑定起来」，编译器据此检查 `cx.emit` 与 `cx.subscribe` 的类型匹配。一个实体可以声明多个 `EventEmitter` 实现来发出多种事件。

典型分工：状态类变化（计数变了、列表刷新了）用 notify，观察者自行读取最新状态；「用户按了回车」「下载完成」这类**事件本身即信息**的场景用 emit。

#### 4.3.2 核心流程

两条通道都不是「立即调用回调」，而是走 u2-l1 讲过的效果队列：

```
实体 A 的更新闭包内：
  cx.notify()
    └─ App::notify(entity_id)（app.rs L2628）
         ├─ 若有正在显示 A 的窗口 → 直接标记窗口失效（下次重绘）
         └─ 否则 push_effect(Effect::Notify { emitter })
              └─ push_effect 用 pending_notifications 集合去重：同一更新周期内多次
                 notify 同一实体，队列里只留一条 Effect::Notify
  cx.emit(event)
    └─ 事件值分配进 event_arena
       push_effect(Effect::Emit { emitter, event_type: TypeId, event })
         （Emit 不去重：发三次就是三个效果）

最外层更新结束 → flush_effects（app.rs L1627）
  循环弹出 Effect（FIFO，先 Notify 后 Emit —— 取决于你调用的先后）：
    Effect::Notify{emitter} → apply_notify_effect
         └─ observers 表中该 emitter 的所有处理器逐个调用；返回 false 的自动注销
    Effect::Emit{emitter, event_type, event} → apply_emit_effect
         └─ event_listeners 表中该 emitter 的处理器，先比对 TypeId 是否等于
            event_type，相等才调用（同一实体可订阅多种事件）
  处理器内部再产生新效果 → 继续循环，直到队列清空
```

两个值得注意的时序结论：

1. **回调在「最外层更新结束」才执行**。在 `a.update(cx, |a, cx| { cx.notify(); ... })` 的闭包还没返回时，观察者不会被打扰——这保证了回调看到的总是实体更新完成后的状态，也让回调里可以安全地对其他实体做 `update`。
2. **同一周期内 notify 去重、emit 不去重**。三次 `cx.notify()` 只触发一次观察者；三次 `cx.emit(e)` 会触发三次订阅者。

#### 4.3.3 源码精读

发送端两个方法都很短。[src/app/context.rs:L228-L231](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L228-L231) 的 `notify` 只有一行：把**自己的** entity_id 交给 `App::notify`——这就是「Context 绑定实体」的直接体现，notify 不需要参数。[src/app/context.rs:L763-L779](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L763-L779) 的 `emit`（注意它在独立的 `impl<T> Context<'_, T>` 块中，且要求 `T: EventEmitter<Evt>`）把事件值放进 `event_arena`，再把 `Effect::Emit`（含 `TypeId::of::<Evt>()`）压入待处理队列。

效果枚举与去重：[src/app.rs:L2838-L2860](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2838-L2860) 定义了全部六种效果；[src/app.rs:L1606-L1622](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1606-L1622) 的 `push_effect` 在效果是 `Notify` 时先查 `pending_notifications` 集合，已存在就直接丢弃。效果循环本体是 [src/app.rs:L1624-L1683](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1624-L1683) 的 `flush_effects`：先释放引用计数归零的实体，再逐个弹效果、派发，效果再产生效果就继续循环，队列为空时清空 `event_arena` 才结束。

接收端的派发：[src/app.rs:L1730-L1748](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1730-L1748) 中，`apply_notify_effect` 对 `observers` 表按 emitter 查找并 `retain` 调用（处理器返回 `false` 即被移除，实现订阅自清理）；`apply_emit_effect` 先比对存储的 `TypeId` 与事件类型，只有相等才调用处理器——这就是「按事件类型路由」。

`Context` 上的注册方法：[src/app/context.rs:L63-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L63-L81) 的 `observe` 展示了标准套路——先 `let this = self.weak_entity();` 抓住订阅者自己的弱句柄，再委托给 `App::observe_internal`，回调触发时先 `this.upgrade()` 把订阅者实体救活并 `update`，升级失败（订阅者已释放）就返回 `false` 让框架注销这条订阅。[src/app/context.rs:L98-L117](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L98-L117) 的 `subscribe` 结构完全相同，多了事件类型参数与 `T2: EventEmitter<Evt>` 约束。另有观察/订阅自己的快捷方式 `observe_self` / `subscribe_self`（[src/app/context.rs:L84-L95](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L84-L95)、[src/app/context.rs:L120-L132](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L120-L132)）。

官方示例 [examples/ownership_post.rs:L18-L36](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/ownership_post.rs#L18-L36) 把整条链路串在 18 行里：第 14 行声明 `impl EventEmitter<Change> for Counter {}`；subscriber 在自己的构造闭包里 `cx.subscribe(&counter, ...)` 并 `detach()`（见下方练习 2）；随后一次 `counter.update` 中先改状态，再 `cx.notify()`，再 `cx.emit(Change { increment: 2 })`；回调让 subscriber 的 count 增加 `2 * 2 = 4`，最后一行 `assert_eq!(subscriber.read(cx).count, 4)` 验证了「emit 的载荷确实送达订阅者」。

#### 4.3.4 代码实践

**实践目标**：区分两条通知路径的触发条件与时机——这是本讲的核心实践，完整程序放在第 5 节综合实践中（A/B 两实体，observe 与 subscribe 双通道）。此处先做最小版：

1. 本地复制 `examples/ownership_post.rs` 为 `examples/notify_paths.rs`。
2. 在 subscribe 回调里加日志（示例代码）：
   ```rust
   cx.subscribe(&counter, |subscriber, _emitter, event, _cx| {
       println!("[subscribe] 收到 Change {{ increment: {} }}", event.increment);
       subscriber.count += event.increment * 2;
   })
   .detach();
   ```
3. 再注册一个观察者（示例代码）：
   ```rust
   cx.observe(&counter, |_observer: &mut Counter, counter, _cx| {
       println!("[observe]   counter 变了，count = {}", counter.read(_cx).count);
   })
   .detach();
   ```
4. 把 `counter.update` 闭包里的 `cx.notify()` 与 `cx.emit(...)` 拆成两次独立的 update，分别只调用其中一个；再合并回同一次 update 中先后调用。
5. 运行 `cargo run -p gpui --example notify_paths`，对比三种情况下两路日志的出现组合与顺序（进程是否自动退出「待本地验证」，必要时 Ctrl-C）。

**需要观察的现象**：只 notify 时只有 `[observe]`；只 emit 时只有 `[subscribe]`；两者都调用时先出现的日志对应先调用的那个方法（FIFO 效果队列）。

**预期结果**：验证 4.3.2 的时序结论——回调都在 update 结束后的效果刷新阶段执行，且同一次 update 内多次 `cx.notify()` 只产生一次 `[observe]`。

#### 4.3.5 小练习与答案

**练习 1**：一个实体能同时发出多种事件吗？订阅者如何区分？

**答案**：能。为同一实体写多个 `impl EventEmitter<A> for T {}`、`impl EventEmitter<B> for T {}` 即可。区分靠 `TypeId`：`subscribe_internal`（[src/app.rs:L1179-L1204](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1179-L1204)）把 `TypeId::of::<Evt>()` 和处理器存在一起，`apply_emit_effect` 派发时逐一比对类型，只有匹配的处理器被调用。

**练习 2**：`ownership_post.rs` 里的 `.detach()` 是什么意思？不加会怎样？

**答案**：`cx.subscribe` 返回 `Subscription`，它被 drop 时订阅即注销。示例在实体的**构造闭包**里注册订阅，返回值没有地方存放，会在闭包结束时立刻被 drop——订阅刚建好就失效。`.detach()` 让订阅脱离 RAII 管理、存活到应用结束。更工程化的做法是把 `Subscription` 存进实体字段（如 `_subscriptions: Vec<Subscription>`），随实体释放而注销。

**练习 3**：为什么 `cx.notify()` 不需要任何参数，而 `cx.observe()` 必须传入被观察实体？

**答案**：`notify` 作用于 `Context<T>` 绑定的当前实体（[src/app/context.rs:L229-L231](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L229-L231) 直接使用 `self.entity_state.entity_id`）；`observe` 是「我（当前实体）要观察别人」，被观察对象自然要作为参数传入，注册结果记在 `App` 的 `observers` 表里、以被观察者的 entity_id 为键。

### 4.4 cx.listener：把元素事件回调接回实体

#### 4.4.1 概念说明

元素的输入事件回调（如 `.on_click(...)`）签名是 `Fn(&E, &mut Window, &mut App)`——回调拿到的是事件 `E`、窗口和 **App**，而不是你的视图实体。但几乎每个点击处理器都要读写视图状态。`Context::listener` 就是这个缺口的适配器：

> 给它一个 `Fn(&mut T, &E, &mut Window, &mut Context<T>)`（注意第一个参数是你的实体状态），它返回一个 `Fn(&E, &mut Window, &mut App)`（正好是元素想要的形状）。

它本质上是一个「闭包转换器」：捕获视图实体的弱句柄，在事件到来时把实体 update 起来、把 `Context<T>` 重新造出来，再调用你的逻辑。这样 UI 事件处理代码就能以「实体方法 + 专属上下文」的风格书写，与 4.3 的响应式循环无缝衔接——处理器末尾的 `cx.notify()` 会照常触发重绘与观察者。

#### 4.4.2 核心流程

```
render() 期间调用 cx.listener(|this, event, window, cx| { ... })
  └─ 捕获 self.entity().downgrade()  → WeakEntity<T>
  └─ 返回新闭包 move |e, window, cx: &mut App| { ... }

用户点击，元素回调被调用
  └─ view.update(cx, |view, cx| f(view, e, window, cx))
       ├─ upgrade 成功 → 租约式更新实体，你的 f 拿到
       │    (&mut T, &E, &mut Window, &mut Context<T>)
       └─ upgrade 失败（实体已释放）→ .ok() 静默跳过
```

`processor` 是它的值返回版本：回调需要产出返回值（而非仅修改状态）时使用，签名把 `&E` 换成按值的 `E`、返回 `R`。

#### 4.4.3 源码精读

本体只有 9 行：[src/app/context.rs:L247-L260](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L247-L260)。文档注释点明动机：很多 GPUI 回调形如 `Fn(&E, &mut Window, &mut App)`，而访问视图状态需要别的东西，此方法提供了便捷途径。实现里 `let view = self.entity().downgrade();` 抓弱句柄，返回的闭包中 `view.update(cx, |view, cx| f(view, e, window, cx)).ok();` 完成升级+租约+转发；`.ok()` 表示实体若已释放则放弃（元素树中残留的监听器晚于实体释放是正常场景，不值得报错）。

姊妹方法 `processor`：[src/app/context.rs:L262-L272](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L262-L272)。

真实用法学自示例：[examples/gradient.rs:L51-L57](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/gradient.rs#L51-L57) 中一个按钮的 `.on_click(cx.listener(move |this, _, _, cx| { ... this.color_space = ...; cx.notify(); }))`——四参数闭包里改自己的状态、`cx.notify()` 请求重绘，这就是 GPUI 应用里最高频的代码形状。`examples/painting.rs` 与 `examples/animation.rs` 中还能看到它用于 `on_mouse_move`、`on_action` 等各类事件的写法。

#### 4.4.4 代码实践

**实践目标**：体会「有无 listener」的类型差异，并跑通一个真实 UI 中的 listener。

1. 运行 `cargo run -p gpui --example gradient`，点击窗口顶部的黑色按钮，观察渐变色彩空间在 Oklab/Srgb 间切换。
2. 打开 [examples/gradient.rs:L51-L57](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/gradient.rs#L51-L57)，把 `cx.listener(move |this, _, _, cx| { ... })` 临时改成普通闭包 `move |_, _, _| { ... }`（示例代码实验），再 `cargo check -p gpui --example gradient`。
3. 观察编译错误后还原代码。

**需要观察的现象**：第 2 步必然编译失败——普通闭包里没有 `this` 和 `cx`，`this.color_space = ...` 与 `cx.notify()` 无从谈起；这正是 `listener` 存在的意义。

**预期结果**：改回后编译通过、按钮行为如初。UI 显示效果「待本地验证」（需要可用的图形环境）。

#### 4.4.5 小练习与答案

**练习 1**：`cx.listener` 的闭包里能直接 `cx.emit(SomeEvent)` 吗？

**答案**：能。listener 重建的 `cx` 是 `&mut Context<T>`，拥有 `notify`/`emit`/`observe` 等全部实体专属方法（前提是 `T` 实现了对应的 `EventEmitter<SomeEvent>`）。

**练习 2**：为什么 listener 捕获的是**弱**句柄而不是 `Entity<T>` 强句柄？

**答案**：返回的闭包会被元素树持有、存活到当前帧之后，若捕获强句柄就构成「元素树 → 实体」的强引用，可能让实体无法释放（u2-l2 讲过强句柄成环即泄漏）。弱句柄 + 回调时 upgrade，让监听器的生命周期永远不超过实体本身，实体释放后回调自动失效。

### 4.5 AsyncApp 与 AsyncWindowContext：能跨 await 点的上下文

#### 4.5.1 概念说明

异步代码（等待网络、定时器、后台计算）必须把「访问 App」这件事拆散到多个时间点。Rust 的借用不允许 `&mut App` 活过 `await`，于是 GPUI 提供 `AsyncApp`：

- 它是**拥有的值**（`#[derive(Clone)]`），可以存进 future、跨任意多个 `await`；
- 内部只持 `Weak<AppCell>`（弱引用）加前台/后台两个执行器的克隆；
- 每个方法在执行时才升级弱引用并短暂 `borrow_mut` `RefCell`，用完立刻归还。

代价是**调用可失败**：应用可能已退出、窗口可能已关闭，所以窗口类方法返回 `Result`；若 App 整个没了，升级失败直接 panic（文档注明：经由 `cx.spawn()` 产生的前台任务不会遇到这种情况，执行器会先检查 App 存活）。

`AsyncWindowContext` 在其上再加一个 `AnyWindowHandle`：它代表「这个异步任务所属的窗口」，把 `AsyncApp` 的能力通过 Deref 全部继承，并把窗口操作（`update`、`on_next_frame`、`prompt` 等）包装成 `Result` 版本。两者的关系类似 `Context<T>` 之于 `App`——一个带上下文附加信息的包装。

#### 4.5.2 核心流程

`AsyncApp` 从哪里来？只有两条路：`App::to_async()` 手动转换，或 `cx.spawn(...)` 由框架递到你的闭包里：

```
cx.spawn(async move |cx: &mut AsyncApp| { ... })     // App 上的版本，cx 是 AsyncApp
entity.update(cx, |state, cx| {
    cx.spawn(async move |this, cx| { ... })          // Context<T> 上的版本
})   //   ^^^ WeakEntity<T>     ^^^ &mut AsyncApp
```

`Context::spawn` 的独到之处：第一个参数自动给你**当前实体的弱句柄**（这正是 4.2 里 `weak_entity()` 的主要用途），异步代码醒来后用 `this.update(cx, |state, cx| ...)` 回写状态，返回 `Result` 需要处理。

窗口版本 `cx.spawn_in(window, async move |this, cx| { ... })` 中 `cx` 则是 `&mut AsyncWindowContext`。

执行模型（呼应 u2-l1 的单前台线程）：future 在前台执行器上轮询；两次轮询之间别的更新可以发生，所以**每次醒来都要重新借用**——这也是在 spawn 的闭包里做 `cx.update_entity(...)` 可能与外层更新撞车 panic 的原因（不要在 `App::update` 的闭包内同步地深入异步上下文再更新实体）。

#### 4.5.3 源码精读

结构：[src/app/async_context.rs:L15-L34](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L15-L34) —— 文档注释说明它是「对 App 的异步友好版本，持有静态生命周期、可跨 await；内部持弱引用，方法在 App 已释放时会 panic（但 spawn 出的前台任务不会）」，字段为 `Weak<AppCell>` + 两个执行器；`fn app(&self)` 是统一的「升级 + expect」入口。

按次借用的样板：[src/app/async_context.rs:L59-L67](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L59-L67) 的 `update_entity` 三行——升级拿 `Rc<AppCell>`、`borrow_mut`、调用 `App` 的同名方法、隐式归还。整个 `impl AppContext for AsyncApp`（[src/app/async_context.rs:L36-L142](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L36-L142)）里几乎所有方法都是这个形状；其中 `update_window`（[src/app/async_context.rs:L85-L95](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L85-L95)）用了 `try_borrow_mut` 并把「App 已释放 / 正在被借用 / 正在退出」都折算成 `Err`，这就是文档说「异步上下文的调用是可失败的」的具体含义。

同步入口 `update`：[src/app/async_context.rs:L162-L167](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L162-L167) 借用后调用 `App::update`——注意它会连带 `flush_effects`，所以异步代码经 `AsyncApp::update` 修改状态后，观察者与订阅者的回调同样会被正确触发（与 4.3 的循环闭合）。

来源与派生：[src/app.rs:L1875-L1883](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1875-L1883) 是 `App::to_async`；[src/app.rs:L1898-L1914](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1898-L1914) 是 `App::spawn`（`to_async` 后交给前台执行器）。`Context::spawn`：[src/app/context.rs:L236-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L236-L245)，闭包形如 `AsyncFnOnce(WeakEntity<T>, &mut AsyncApp) -> R`，文档强调返回的 Task 必须被持有或 detach。

`AsyncWindowContext`：[src/app/async_context.rs:L278-L302](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L278-L302) —— `#[derive(Clone, Deref, DerefMut)]` 组合 `AsyncApp` 与 `AnyWindowHandle`（Deref 目标是 `AsyncApp`），其 `update` 方法（[src/app/async_context.rs:L299-L302](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L299-L302)）委托 `update_window`，故返回 `Result<R>`。

#### 4.5.4 代码实践

**实践目标**：把 4.3 实践中的「同步递增」改成「延时后异步回写」，跑通 `Context::spawn` + `WeakEntity::update` 模式。

1. 在 4.3 的 `notify_paths.rs`（或第 5 节综合实践的副本）里，于注册完订阅之后加入（示例代码）：
   ```rust
   counter.update(cx, |counter, cx| {
       let start = counter.count;
       cx.spawn(async move |this, cx| {
           cx.background_executor()
               .timer(std::time::Duration::from_millis(300))
               .await;
           this.update(cx, |counter, cx| {
               counter.count = start + 100;
               println!("[spawn] 300ms 后异步回写完成");
               cx.notify();
           })
           .ok();
       })
       .detach();
   });
   ```
2. 把结尾的 `cx.quit()` 暂时注释掉，运行 `cargo run -p gpui --example notify_paths`，观察 `[observe]` 是否在约 300ms 后再次出现；完毕后 Ctrl-C 结束进程，再恢复 `cx.quit()`。
3. 思考并验证：若把 `.detach()` 改成把 `Task` 存进实体字段，行为会有何不同？（提示：u2-l5 将系统讲解 Task 的取消语义。）

**需要观察的现象**：异步回写发生在 run 回调结束之后、应用退出之前；回写里的 `cx.notify()` 走的是与 4.3 完全相同的效果队列，`[observe]` 日志再次打印。

**预期结果**：若 `cx.quit()` 未注释，300ms 定时器可能来不及触发应用就退出了——这本身就是「Task 生命周期 vs 应用生命周期」的直观教材。精确时序「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`Context::spawn` 的闭包为什么拿到的是 `WeakEntity<T>` 而不是 `Entity<T>`？

**答案**：任务可能存活很久（等待网络、长计算），强句柄会把实体一直钉在内存里，即使应用其他部分早已不需要它（u2-l2 的成环泄漏）。弱句柄让实体可以先行释放，任务醒来发现升级失败就放弃回写（[src/app/context.rs:L243-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L243-L245) 把 `self.weak_entity()` 递给闭包）。

**练习 2**：`AsyncWindowContext::update` 与 `AsyncApp::update` 的返回类型为什么不同？

**答案**：`AsyncApp::update` 返回 `R`（App 由 `Weak` 升级失败时 panic），而 `AsyncWindowContext::update` 返回 `Result<R>`——它要经过 `update_window`，窗口句柄可能已经失效（窗口先于任务关闭），这类失败是常态，必须显式处理（[src/app/async_context.rs:L299-L302](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L299-L302)）。

**练习 3**：docs/contexts.md 说 `TestAppContext` 与异步上下文「类似」但访问失败时 panic。结合本讲，它最可能基于哪个类型扩展而来？

**答案**：基于同样的「弱引用 + 按次借用」骨架（行为类似 `AsyncApp`/`AsyncWindowContext`），但把失败路径换成 panic 以便测试尽早暴露问题，并叠加虚拟时钟、输入模拟等测试设施。其深入用法留到 u7-l4（`#[gpui::test]` 与 `TestAppContext`）。

## 5. 综合实践

**任务**：实现两个实体 `Producer`（A）与 `Consumer`（B）。A 的状态变化通过 `cx.notify()` 广播、并通过 `cx.emit` 发出自定义事件；B 同时用 `cx.observe` 和 `cx.subscribe` 监听 A，用日志揭示两条路径的触发条件与时机。这正是本讲规格中指定的实践任务。

1. **创建文件** `crates/gpui/examples/context_family.rs`（示例目录下的 `.rs` 文件会被 cargo 自动发现为示例 target；若未被发现，可仿照 [Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L177-L180) 中既有的 `[[example]]` 声明补充之——「待本地验证」）。

2. **写入以下完整程序**（示例代码，骨架取自 [examples/ownership_post.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/ownership_post.rs#L1-L50)）：

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use gpui::{App, Context, Entity, EventEmitter, prelude::*};
   use gpui_platform::application;

   /// 实体 A：被观察、也对外发事件
   struct Producer {
       value: usize,
   }

   /// A 发出的事件：带载荷
   struct ValueChanged {
       new_value: usize,
   }

   impl EventEmitter<ValueChanged> for Producer {}

   /// 实体 B：双通道监听 A
   struct Consumer {
       last_seen: usize,
       received_events: usize,
   }

   fn run_example() {
       application().run(|cx: &mut App| {
           let producer: Entity<Producer> = cx.new(|_cx| Producer { value: 0 });

           let consumer = cx.new(|cx: &mut Context<Consumer>| {
               // 通道一：observe —— 只在对方 notify 时触发，无载荷
               cx.observe(&producer, |this, producer, cx| {
                   let value = producer.read(cx).value;
                   println!("[observe]    A 通知了，读到最新 value = {value}");
                   this.last_seen = value;
               })
               .detach();

               // 通道二：subscribe —— 只在对方 emit 时触发，带载荷
               cx.subscribe(&producer, |this, _emitter, event: &ValueChanged, _cx| {
                   println!("[subscribe] 收到 ValueChanged，载荷 new_value = {}", event.new_value);
                   this.received_events += 1;
               })
               .detach();

               Consumer { last_seen: 0, received_events: 0 }
           });

           // 第 1 轮：只 notify —— 预期只触发 [observe]
           producer.update(cx, |producer, cx| {
               producer.value += 1;
               cx.notify();
           });

           // 第 2 轮：只 emit —— 预期只触发 [subscribe]
           producer.update(cx, |producer, cx| {
               producer.value += 1;
               cx.emit(ValueChanged { new_value: producer.value });
           });

           // 第 3 轮：先 notify 后 emit —— 预期 [observe] 在前、[subscribe] 在后
           producer.update(cx, |producer, cx| {
               producer.value += 1;
               cx.notify();
               cx.emit(ValueChanged { new_value: producer.value });
           });

           // 第 4 轮：同一轮里 notify 三次 —— 预期 [observe] 只出现一次（去重）
           producer.update(cx, |producer, cx| {
               producer.value += 1;
               cx.notify();
               cx.notify();
               cx.notify();
           });

           let consumer = consumer.read(cx);
           println!(
               "最终：last_seen = {}（应为 4），received_events = {}（应为 2）",
               consumer.last_seen, consumer.received_events
           );

           cx.quit();
       });
   }

   #[cfg(not(target_family = "wasm"))]
   fn main() {
       run_example();
   }

   #[cfg(target_family = "wasm")]
   #[wasm_bindgen::prelude::wasm_bindgen(start)]
   pub fn start() {
       gpui_platform::web_init();
       run_example();
   }
   ```

3. **运行**：`cargo run -p gpui --example context_family`。

4. **观察并记录**（预期输出，精确行为「待本地验证」）：
   - 第 1 轮后仅一行 `[observe]`；第 2 轮后仅一行 `[subscribe]`；
   - 第 3 轮后 `[observe]` 在 `[subscribe]` 之前（效果队列 FIFO）；
   - 第 4 轮后 `[observe]` 只出现一次（`push_effect` 的 `pending_notifications` 去重）；
   - `last_seen = 4`：第 2 轮 emit 不触发 observe，但第 3、4 轮的 notify 让 B 读到了最新值；
   - `received_events = 2`：notify 不产生事件载荷。

5. **延伸改造**（选做）：按 4.5.4 的片段给 Producer 加一个 300ms 后异步回写的 `cx.spawn`，注释掉 `cx.quit()`，验证异步路径与同步路径共用同一套效果队列。

这个实验把本讲四个最小模块全部打通：`Context<T>` 在构造/更新闭包中诞生（4.2）；`EventEmitter` + observe/subscribe 两条通道的注册与派发（4.3）；若把 Producer 改成视图再加一个按钮，`cx.listener` 就是按钮到 `cx.notify()` 的桥（4.4）；异步回写则演示 `AsyncApp` 的按次借用模型（4.5）。

## 6. 本讲小结

- GPUI 有五种上下文：`App`（根，借用）、`Context<T>`（借用 + 绑定实体）、`AsyncApp`（owned，弱引用 + 按次借用）、`AsyncWindowContext`（再叠加窗口句柄，窗口操作返回 `Result`）、`TestAppContext`（测试专用，u7-l4 详述）；`Context<T>` Deref 到 `App`、`AsyncWindowContext` Deref 到 `AsyncApp`，而 `AsyncApp` 靠每次短暂借用而非 Deref。
- `Context<T>` = `&mut App` + `WeakEntity<T>`，只在 `cx.new` / `entity.update` 的闭包内存活；`notify`/`emit` 无参作用于当前实体，`observe`/`subscribe` 需传入对方实体并返回应妥善保存的 `Subscription`。
- 两条通知路径语义不同：`notify → observe` 无载荷、同一更新周期内去重；`emit → subscribe` 有载荷、按 `EventEmitter<Evt>` 标记 trait 与 `TypeId` 路由、不去重。两者都进入 `Effect` 队列，在最外层更新结束后的 `flush_effects` 中派发。
- `cx.listener` 是类型适配器：把 `Fn(&mut T, &E, &mut Window, &mut Context<T>)` 包装成元素回调需要的 `Fn(&E, &mut Window, &mut App)`，内部靠捕获的弱句柄在事件到来时重新租约更新实体。
- `AsyncApp`/`AsyncWindowContext` 让「访问 App」可以跨 `await`：克隆廉价、每次方法调用升级弱引用并短暂借用 `RefCell`，因此窗口类调用可失败；`Context::spawn` 额外把当前实体的 `WeakEntity<T>` 递进异步闭包，是异步回写状态的标准入口。

## 7. 下一步学习建议

- **下一讲 u2-l4（Global 全局状态）**：本讲多次出现的 `read_global` / `BorrowAppContext` blanket impl 将在那里展开；你会看到全局单例如何借助 `observe_global` 加入同一套效果队列。
- **再下一讲 u2-l5（并发模型：executor 与 Task）**：本讲 4.5 只打开了异步上下文的门，Task 的取消、detach、优先级与平台调度器是那里的主题。
- **源码阅读建议**：通读一遍 [src/app/context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L1-L761) 的方法名清单（observe_in、subscribe_in、on_focus 系列、on_release 等），它们都是「弱句柄 + 回调时 upgrade + update」同一套路的变化，读起来会非常快；遇到 `*_in` 后缀的方法留意它们额外绑定了窗口。
- **回看官方文档**：[docs/contexts.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L25-L33) 末尾「非上下文核心类型」一节解释了 `Window` 与 `Entity<T>` 为何不属于上下文家族，读完本讲再看会有更深的体会。
