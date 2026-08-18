# u2-l2 Entity 模型与 App 上下文

## 1. 本讲目标

上一讲(u2-l1)我们建立了 GPUI 的总体地图,知道它是「UI 框架 + 应用运行时」的混合体。本讲深入它的心脏:**状态是怎么存的、代码是怎么拿到并修改状态的、状态变化又是怎么通知别人的**。

学完本讲,你应该能够:

1. 说出 `Entity<T>` 句柄与 `App` 之间的所有权关系,并解释「句柄不拥有状态」的含义。
2. 熟练使用 `Entity<T>` 的 `entity_id` / `read` / `read_with` / `update` / `downgrade` API 和 `WeakEntity<T>` 的 `upgrade` / `update` API。
3. 区分 `App`、`Context<T>`、`AsyncApp` 三种上下文,知道每种上下文在什么回调里出现、能做什么、不能做什么。
4. 解释 `cx.notify()` 之后发生了什么:Effect 队列、去重、`flush_effects` 循环,以及观察者回调为什么在 update 闭包结束**之后**才运行。
5. 区分 `observe`(状态变了,无载荷)与 `subscribe`(类型化事件,有载荷),并用 `Subscription` 与 `WeakEntity` 避免循环引用导致的实体泄漏。

## 2. 前置知识

本讲假设你已了解 u2-l1 的 GPUI 分层地图,另外需要以下 Rust 基础概念,先用一段话各自建立直觉:

- **`Rc` 与引用计数**:`Rc<T>` 是「多个所有者共享一份数据」的智能指针,每 clone 一次计数加一,所有克隆 drop 后数据才释放。GPUI 的 `Entity<T>` 在「引用计数」这一点上像 `Rc`,但**不**像 `Rc` 那样能直接解引用拿到 `T`。
- **`Weak` 弱引用**:`std::rc::Weak` 不增加计数,只提供「试试能否借到」的 `upgrade()`,返回 `Option`。GPUI 对应物是 `WeakEntity<T>`,它的 `update` / `read_with` 返回 `anyhow::Result`,实体已释放时报错而非 panic。
- **`RefCell` 与运行时借用检查**:`RefCell<T>` 把「同一时刻最多一个可变借用」的检查从编译期挪到运行期,违反就 panic。GPUI 把整个应用状态 `App` 放在一个 `RefCell` 里,所以「在实体 A 的 update 闭包里再去 update 实体 A」会直接 panic——这就是 CLAUDE.md 里「Trying to update an entity while it's already being updated must be avoided」的底层原因。
- **`Deref` 解引用链**:`Context<T>` 实现了 `Deref<Target = App>`,所以拿到 `Context<T>` 的地方可以调用所有 `App` 的方法,就像 `String` 能当 `&str` 用一样。理解这条「解引用继承链」是读懂 GPUI 代码的关键。
- **trait 作为「上下文接口」**:GPUI 定义了 `AppContext` trait,让 `App`、`Context<T>`、`AsyncApp` 等不同上下文对 `new` / `update_entity` / `read_entity` 等操作写同一套调用代码。这与「泛型约束 + 多态」的常规 Rust 用法一致。
- **副作用(side effect)与更新周期(update cycle)**:GPUI 把「修改状态」和「通知别人」分成两个阶段:你在闭包里改数据,GPUI 在闭包结束后统一派发通知。这个设计贯穿本讲。

一个不要求但推荐的阅读:GPUI 源码中有一份专门的官方文档讲解本讲主题——所有权与数据流,见 [crates/gpui/src/_ownership_and_data_flow.rs:1-13](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/_ownership_and_data_flow.rs#L1-L13),其中明确写道:所有实体的真正所有者是 `App`,句柄只是「惰性标识符 + 编译期类型标签 + 引用计数」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/gpui/src/app.rs` | `App` 结构体与所有应用级服务 | `App` 的字段、`observe`/`subscribe`、`notify`、Effect 队列与 `flush_effects` |
| `crates/gpui/src/app/entity_map.rs` | 实体的仓储与句柄类型 | `EntityMap` 的 reserve/insert/lease/read、`Entity<T>`、`WeakEntity<T>` |
| `crates/gpui/src/app/context.rs` | 实体级上下文 `Context<T>` | `observe`/`subscribe`/`notify`/`emit`/`spawn`/`listener` |
| `crates/gpui/src/app/async_context.rs` | 跨 await 点的异步上下文 | `AsyncApp` 如何弱持有 `App` 并转发操作 |
| `crates/gpui/src/subscription.rs` | 订阅的底层容器与 RAII 句柄 | `SubscriberSet` 的惰性激活、`Subscription` 的 Drop 语义 |
| `crates/gpui/src/gpui.rs` | 框架门面与核心 trait | `AppContext` trait、`VisualContext`、`EventEmitter` |
| `crates/gpui/examples/testing.rs` | 官方测试示例 | 一个真实的 `Counter` 实体及其测试,是本讲实践的素材 |
| `crates/gpui/examples/hello_world.rs` | 官方最小 GUI 示例 | `application().run` + `cx.new` 的最小骨架 |

## 4. 核心概念与源码讲解

本讲的三个最小模块:**4.1 Entity 生命周期**、**4.2 上下文类型体系**、**4.3 订阅与观察**。

### 4.1 Entity 生命周期:从 `cx.new` 到释放

#### 4.1.1 概念说明

GPUI 里的每一块可变应用状态——编辑器的 `Editor`、项目的 `Project`、一个设置项、甚至一个按钮的内部状态——都是一个 **entity**。设计要点是所有权分离:

- **真正的所有者是 `App`**:你在 `cx.new(|_| Counter { count: 0 })` 时,`Counter` 这个值被装箱(`Box<dyn Any>`)存进 `App` 内部的 `EntityMap`,从此归 `App` 管。
- **`Entity<T>` 只是句柄**:它不持有 `T`,由三样东西构成——一个全应用唯一的 `EntityId`、一个 `TypeId`(编译期类型标签)、一个指向引用计数表的弱指针。句柄 clone 会增加计数,drop 会减少。
- **访问状态必须出示上下文**:`counter.read(cx)` / `counter.update(cx, ...)` 都要求传入上下文,因为状态存在 `App` 里,必须借 `App` 才能取出。

为什么要这样设计而不是直接 `Rc<RefCell<T>>`?因为把所有权集中到 `App`,GPUI 才能统一提供:观察/订阅分发、窗口失效追踪(哪个窗口正在渲染哪个实体)、实体释放回调、测试环境下的泄漏检测。这些都是散落的 `Rc` 做不到的。

#### 4.1.2 核心流程

一个实体从生到死的流程:

```text
1. cx.new(|cx| 构造 T)
   ├─ EntityMap::reserve()  → 先在计数表里占一个坑,拿到 EntityId(此时实体还不存在)
   ├─ 构造闭包执行,闭包拿到 Context<T>(可在此订阅别人、spawn 任务)
   ├─ EntityMap::insert(slot, value) → 值装箱存入 entities 表,返回 Entity<T> 句柄
   └─ 推入 Effect::EntityCreated(供 observe_new 等机制使用)

2. 句柄流通
   ├─ clone 句柄 → 计数 +1(注意:是句柄计数,不是数据拷贝)
   ├─ downgrade() → WeakEntity<T>,不计数
   └─ EntityId 可安全放入 HashMap/日志(它只是个 u64 包装)

3. 访问
   ├─ read(cx)   → 借出 &T(只读,记入「已访问实体」集合,供窗口失效追踪)
   └─ update(cx) → 把 Box 里的值「租借(lease)」到栈上,给你 &mut T 和 Context<T>;
                     闭包结束后 end_lease 归还。租借期间再次 update 同一实体会 panic

4. 释放
   ├─ 所有强句柄 drop → 计数归零 → 实体 id 进入 dropped_entity_ids
   └─ 效果刷新循环里 take_dropped() 取出 → 触发 release 回调 → 移除该实体的
       observers/event_listeners/窗口追踪记录 → Box drop,状态真正销毁
```

其中「租借」是最精巧的一步:`update` 把实体从 `EntityMap` 的盒子里取出来放到栈上,让你拿到 `&mut T`,同时因为盒子空了,同一实体的嵌套 `update` 会发现取不出值而 panic(错误信息是 "cannot update T while it is already being updated")。这用运行时检查替代了借用检查器无法跨闭包表达的约束。

#### 4.1.3 源码精读

**仓储结构:`EntityMap` 用 SecondaryMap 存装箱的实体,计数表单独放在 Arc<RwLock> 里**,见 [crates/gpui/src/app/entity_map.rs:56-68](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L56-L68)。`EntityId` 由 slotmap 生成,保证全应用唯一且可复用空位,见 [crates/gpui/src/app/entity_map.rs:27-30](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L27-L30)。

**创建实体的两步走:先 `reserve` 占位,再 `insert` 放值**,见 [crates/gpui/src/app/entity_map.rs:114-130](https://github.com/zed-industries-zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L114-L130)。两步拆开的动机:构造闭包里拿到的 `Context<T>` 需要一个指向「自己」的弱句柄(比如在构造时就要 `cx.observe` 别人并保存自己的弱引用),这时实体值还不存在,弱句柄必须先行。`cx.new` 的完整实现在 `App` 的 `AppContext` 实现里,见 [crates/gpui/src/app.rs:2706-2720](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2706-L2720)——注意它把构造动作包进 `self.update(...)`,所以构造期间产生的订阅、通知同样走效果队列。

**`update` 的租借机制**,见 [crates/gpui/src/app.rs:2740-2754](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2740-L2754):`lease` 取盒、闭包执行、`end_lease` 归还。租借与归还的对应实现见 [crates/gpui/src/app/entity_map.rs:134-154](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L134-L154);重复 update 的保护与报错见 [crates/gpui/src/app/entity_map.rs:206-212](https://github.com/zed-industries-zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L206-L212)。

**句柄本体与它的 API 面**,见 [crates/gpui/src/app/entity_map.rs:411-509](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L411-L509)。核心几个:

```rust
// crates/gpui/src/app/entity_map.rs:462-508(节选)
pub fn read<'a>(&self, cx: &'a App) -> &'a T { cx.entities.read(self) }
pub fn read_with<R, C: AppContext>(&self, cx: &C, f: impl FnOnce(&T, &App) -> R) -> R {
    cx.read_entity(self, f)
}
pub fn update<R, C: AppContext>(&self, cx: &mut C,
    update: impl FnOnce(&mut T, &mut Context<T>) -> R) -> R { cx.update_entity(self, update) }
```

`read` 直接要 `&App`;`read_with`/`update` 泛型于 `C: AppContext`,所以 `App`、`Context<U>`、`AsyncApp`、测试上下文都能当参数——这就是「到处都能 update」的实现秘密。`Clone` 只复制三元组并递增计数,见 [crates/gpui/src/app/entity_map.rs:511-519](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L511-L519)。

**弱句柄 `WeakEntity<T>`**:结构见 [crates/gpui/src/app/entity_map.rs:738-745](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L738-L745),`upgrade` / `update` / `read_with` 见 [crates/gpui/src/app/entity_map.rs:765-826](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L765-L826)。注意它的 `update` 返回 `Result<R>`:实体没了就返回 `"entity released"` 错误,由调用方决定 `.ok()` 忽略还是向上传播。这让它成为异步回调、长生命周期闭包里持有「别的实体」的安全姿势。

**释放路径**:计数归零的实体由 `take_dropped` 批量取出,见 [crates/gpui/src/app/entity_map.rs:184-203](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L184-L203);`App::release_dropped_entities` 负责清掉该实体名下的观察者、事件监听、窗口追踪记录,再触发 release 回调,见 [crates/gpui/src/app.rs:1675-1692](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1675-L1692)。「实体死了订阅自动拆掉」就是在这里发生的。

#### 4.1.4 代码实践

官方示例 `testing.rs` 里有一个完整的 `Counter` 实体和一组测试,是观察生命周期的最佳素材。

1. **实践目标**:亲手跑通一个 GPUI 测试,并从测试断言中验证「update 立即生效、副作用延迟生效」的生命周期时序。
2. **操作步骤**(在你自己的克隆里执行):
   ```bash
   cargo test -p gpui --example testing --features test-support -- basic_testing --nocapture
   ```
   然后阅读 [crates/gpui/examples/testing.rs:224-246](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L224-L246) 的 `basic_testing` 测试:创建 Counter、`update` 改值为 42、`read_with` 断言;再 `cx.emit(CounterEvent)` 之后断言 `count == 999`(999 是 `subscribe_self` 回调改写的)。
3. **需要观察的现象**:测试通过;且如果给两处断言前各加一行打印,能看到 `emit` 所在的 `update` 闭包返回**之后**、下一次 `read_with` **之前**,订阅回调已经把值改成 999。
4. **预期结果**:副作用(订阅回调)不在 `update` 闭包内同步执行,而是在最外层 update 结束、效果刷新时执行。该行为同时被示例自己的断言([testing.rs:241-245](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L241-L245))背书;具体打印顺序待本地验证。
5. 若你的环境缺 GUI 依赖导致 `cargo test` 编译失败,请先按 u1-l2 安装平台依赖;`basic_testing` 本身不需要打开窗口。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `Entity<T>` 不能像 `Rc<T>` 那样直接 `counter.count` 访问字段?
**答案**:句柄里根本没有 `T` 的数据,数据装箱存放在 `App` 的 `EntityMap` 中。访问必须经由上下文(`read`/`update`)把数据从 `App` 里借出来。这样 GPUI 才能在借出环节统一做访问登记、租借互斥检查和窗口失效追踪。

**练习 2**:在 `counter.update(cx, |c, cx| { ... })` 的闭包里再调用一次 `counter.update(cx, ...)`,会发生什么?
**答案**:panic。`update` 的第一步是 `lease`——把实体从盒子里搬到栈上;嵌套 update 时第二次 `lease` 发现盒子已空,走 `double_lease_panic`,报错 "cannot update Counter while it is already being updated"。因此 CLAUDE.md 要求避免在自身 update 中再 update 自身。

**练习 3**:`Entity<T>` 的 `entity_id()` 有什么用?既然句柄可以比较相等,为什么还需要 id?
**答案**:`EntityId` 是一个 `u64` 包装,可以放进 `HashMap` 的键、写进日志、跨类型传递;而 `Entity<T>` 携带类型参数、克隆会递增计数。当只需要「标识」而不需要「访问权」时(比如做实体到窗口的反查表 `window_invalidators_by_entity`),用 id 更轻,也不会延长实体生命周期。

### 4.2 上下文类型体系:App、Context<T> 与 AsyncApp

#### 4.2.1 概念说明

「上下文(context)」是你与 GPUI 打交道的把手。你写的几乎每个闭包签名里都有一个 `cx`,但它绝不是同一个类型——GPUI 用一组层层包裹的类型表达「你现在在哪儿、能做什么」:

- **`App`**:应用本体。所有实体、窗口、执行器、全局变量、订阅表都挂在它身上。你在 `application().run(|cx: &mut App| ...)` 的回调里拿到它。
- **`Context<T>`**:`&mut App` 外面再包一层「我是实体 T 的专属上下文」标签(一个 `WeakEntity<T>`)。你在 `cx.new`、`update` 闭包、`Render::render` 里拿到它。它能做 `App` 能做的一切(通过 `Deref`),额外还能:`cx.notify()` 声明 T 变了、`cx.emit(event)` 发事件、`cx.spawn(...)` 派生持有 T 弱引用的任务、`cx.listener(...)` 生成回写 T 的 UI 事件处理器。
- **`AsyncApp`**:可以跨 `await` 点持有的异步上下文。它内部只是对 `App` 的**弱**引用,不借用它,所以 future 挂起期间不会锁死 `RefCell`。你在 `cx.spawn(async move |cx| ...)` 的闭包里拿到它。
- **`AsyncWindowContext` / `VisualTestContext`**:异步版本的窗口上下文,本讲只提一句,留待 u2-l3 与 u8-l5。

三种上下文的关系可以概括为一句话:**`Context<T>` 是「带着身份的 App」,`AsyncApp` 是「可以等着的 App」**。

此外还有一条贯穿的设计:**`AppContext` trait**。`new` / `update_entity` / `read_entity` / `background_spawn` 这些操作被提炼成 trait 方法,`App`、`Context<T>`、`AsyncApp` 都实现了它。于是 `Entity::update<C: AppContext>(&self, cx: &mut C, ...)` 这样的泛型 API 才能在任何上下文里调用。

#### 4.2.2 核心流程

一次典型交互中上下文的流转:

```text
application().run(|cx: &mut App| {            ← 第 1 层:App
    let counter = cx.new(|cx: &mut Context<Counter>| {   ← 第 2 层:构造期 Context<Counter>
        Counter::new(cx)                      // cx 可用于订阅、focus_handle 等
    });

    counter.update(cx, |counter, cx| {        ← 第 2 层:update 期 Context<Counter>
        counter.count += 1;                   // &mut Counter
        cx.notify();                          // 只能由 Context 调用,声明本实体变了
        cx.spawn(async move |this, cx| {      ← 第 3 层:WeakEntity<Counter> + AsyncApp
            this.update(cx, |counter, _| { … }).ok();   // this 是弱句柄,update 返回 Result
        }).detach();
    });
});
```

三条规则务必记住:

1. **闭包里永远用内层 `cx`**:外层 `cx` 已被 `update` 可变借用,内层才是合法入口(CLAUDE.md 的 "the inner cx provided to the closure must be used instead of the outer cx")。
2. **`notify` 是 `Context<T>` 的方法,不是 `App` 的公开日常方法**:声明「我(T)变了」必须站在 T 的上下文里;`App::notify(entity_id)` 是它的内部落地。
3. **能跨 `await` 的只有 `AsyncApp`**:`&mut App` 和 `Context<T>` 都带借用生命周期,塞进 future 会在第二个 `await` 处编译失败;`AsyncApp` 只弱持有,所以能长期存活,代价是每步操作都要经 `RefCell` 重新借入。

#### 4.2.3 源码精读

**`Context<T>` 的定义只有两个字段,却撑起了整个实体级 API**,见 [crates/gpui/src/app/context.rs:20-37](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L20-L37):

```rust
pub struct Context<'a, T> {
    app: &'a mut App,
    entity_state: WeakEntity<T>,   // 「我是谁」的弱标签
}
// Deref/DerefMut → App:所有 App 方法直接可用
```

`cx.entity()` / `cx.weak_entity()` 从这个标签拿回自己的句柄,见 [crates/gpui/src/app/context.rs:44-59](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L44-L59)。注意保存的是**弱**句柄——上下文自己不该延长自己实体的寿命。

**`cx.notify()` 只有一行,委托给 `App::notify(entity_id)`**,见 [crates/gpui/src/app/context.rs:228-231](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L228-L231)。它就是 4.3 节的入口。

**`cx.spawn` 与 `cx.listener` 是「上下文身份」的两个杀手级应用**:

- `spawn` 把自己的弱句柄塞进异步闭包,见 [crates/gpui/src/app/context.rs:237-245](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L237-L245)——异步任务醒来后先 `upgrade` 再改状态,实体若已释放则静默退出。
- `listener` 把 `Fn(&mut T, &E, &mut Window, &mut Context<T>)` 包装成 UI 事件回调需要的 `Fn(&E, &mut Window, &mut App)`,内部自动完成「升级弱句柄 → update 实体」,见 [crates/gpui/src/app/context.rs:252-260](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L252-L260)。UI 元素的 `.on_click(cx.listener(...))` 写法由此而来。

**`AsyncApp`:弱持有的跨 await 上下文**,见 [crates/gpui/src/app/async_context.rs:15-34](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/async_context.rs#L15-L34)。它的每个操作都是「升级弱引用 → `borrow_mut` → 转发给 `App`」,以 `update_entity` 为例见 [crates/gpui/src/app/async_context.rs:59-67](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/async_context.rs#L59-L67)。app 已退出时会 panic(文档注释明言这不应发生在 `cx.spawn` 的任务里)。

**统一接口 `AppContext` trait**,定义于 [crates/gpui/src/gpui.rs:170-245](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L170-L245):`new` / `update_entity` / `read_entity` / `background_spawn` / `read_global` 等。`Context<T>` 对它的实现几乎全是到 `self.app` 的一行转调,见 [crates/gpui/src/app/context.rs:782-825](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L782-L825)。窗口相关的操作另立 `VisualContext` trait(`update_in` / `new_window_entity` 等),见 [crates/gpui/src/gpui.rs:258-292](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L258-L292)。

**而这一切的家:`App` 结构体**,见 [crates/gpui/src/app.rs:692-781](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L692-L781)。字段分几组认一遍,后两讲还会回来:`entities: EntityMap`(702 行,实体仓储)、`pending_effects` / `pending_notifications`(712、745 行,效果队列)、`observers` / `event_listeners` / `release_listeners`(714、715、720 行,三张订阅表)、`windows` / `focus_handles`(704-706 行)、`background_executor` / `foreground_executor`(700-701 行)。它被包在 `RefCell<App>` 组成的 `AppCell` 里,见 [crates/gpui/src/app.rs:78-115](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L78-L115)——「嵌套可变借用会 panic」的物理基础就在这。

#### 4.2.4 代码实践

源码跟踪型实践,用 IDE 的「跳转定义」把一条调用链走通:

1. **实践目标**:亲手验证 `counter.update(cx, ...)` 这一行背后经过的三层实现,体会上文「`Context<T>` 是带着身份的 App」。
2. **操作步骤**:
   - 打开 [crates/gpui/examples/testing.rs:228-230](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L228-L230),光标放在 `counter.update(` 上执行「Go to Definition」,应落到 [entity_map.rs:476-482](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs#L476-L482) 的 `Entity::update`。
   - 它转调 `cx.update_entity`,再跳一次:此处 `cx` 是 `&mut TestAppContext`(它也实现了 `AppContext`),最终抵达 [app.rs:2740-2754](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2740-L2754) 的 `App::update_entity`(租借-执行-归还)。
   - 把沿途三层的函数签名抄成一个三行清单。
3. **需要观察的现象**:三层中没有一层直接持有 `Counter` 的数据;数据始终躺在 `EntityMap` 的盒子里,只被「租借」了很短一段时间。
4. **预期结果**:你得到的调用链是 `Entity::update` → `AppContext::update_entity`(由具体上下文实现)→ `App::update_entity`。这也解释了为什么同一个 `update` 在 `App`、`Context<T>`、`AsyncApp`、`TestAppContext` 下都能用。
5. 不同 IDE 的跳转可能落在 trait 声明而非实现处,需再手动选择实现;具体落点待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:在 `Render::render(&mut self, window, cx)` 里,`cx` 是什么类型?能用它做什么、不能做什么?
**答案**:`cx: &mut Context<Self>`。能:读别的实体、update 别的实体(不能是 Self——Self 正被 render 可变借用,且 update 需要 `&mut C`)、`cx.listener` 生成回调、通过 `Deref` 用一切 `App` 方法。不能:`cx.spawn`(需要 `&self` 且产生跨 await 任务没问题,但注意 render 应保持纯粹)、对 Self 自身调用 `update`。

**练习 2**:为什么 `AsyncApp` 内部持有的是 `Weak<AppCell>` 而不是 `Rc<AppCell>`?
**答案**:两个原因。其一,若异步上下文强持有 App,任何被 spawn 的任务都会让 App 永远无法释放,形成应用级泄漏。其二,`App` 本体在 `RefCell` 里,强持有并直接调用会在跨 await 场景下产生长期可变借用、锁死其他访问;弱持有 + 每次操作时临时 `borrow_mut`,把借用窗口缩到单个方法内。

**练习 3**:`Context<T>` 为什么要实现 `Deref<Target = App>` 而不是把 `App` 的方法逐个重新导出?
**答案**:Deref 让 `Context<T>` 免维护地继承 `App` 的全部公开方法——`App` 增加方法时所有上下文立即可用;逐个转写则要为每个上下文重复数百行样板。代价是阅读代码时看不到方法定义在哪个类型上,需要靠 IDE 跳转(这正是 4.2.4 练习要建立的习惯)。

### 4.3 订阅与观察:notify 的蝴蝶效应

#### 4.3.1 概念说明

状态变了,谁来刷新界面、谁来联动逻辑?GPUI 给出两条互补通道:

- **`cx.notify()` + `observe`:广播「我变了」**。没有载荷,只表示「这个实体的状态需要别人重新看一眼」。观察者回调收到 `(被观察实体句柄, 上下文)`。这是 UI 刷新的主力:render 期间 GPUI 会登记「本窗口读了哪些实体」,notify 命中就安排重绘。
- **`cx.emit(event)` + `subscribe`:发送类型化事件**。有载荷(`Event` 结构体),发送方需声明 `impl EventEmitter<Event> for T {}`,订阅方按事件类型分别注册。适合表达「发生了某件具体的事」,如 `Event::BufferReleased`。

两者背后是同一套**效果(effect)队列**机制:notify/emit 不立即执行回调,而是把一个 `Effect` 压进队列;最外层 `update` 结束时统一 `flush_effects`。这个「先改完、再统一通知」的设计带来三个好处:

1. **一致性**:观察者看到的永远是本次修改完成后的状态,不会观察到中间态。
2. **去重**:同一实体在同一周期内 notify 多次,只会派发一次(队列入口用 `pending_notifications` 集合去重)。
3. **无重入**:回调里再改状态,新效果排到队尾继续处理,不会在调用栈上打穿。

与之配套的是 **`Subscription`**——订阅的 RAII 句柄:drop 即退订,`detach()` 则让它活到被观察实体死亡为止。最后一个关键角色是 **`WeakEntity`**:观察回调内部只弱引用观察者,观察者实体被释放后回调自动拆除,避免「互相持有强句柄 → 双方永不释放」的实体泄漏。

#### 4.3.2 核心流程

```text
counter.update(cx, |c, cx| { c.count += 1; cx.notify(); })
│
├─ 闭包执行:count 已改(此时无人知晓)
├─ cx.notify() → App::notify(counter_id)
│    ├─ 若有窗口正在渲染该实体 → 直接 invalidate 对应窗口(走快速路径)
│    └─ 否则 → pending_notifications 去重后压入 Effect::Notify
└─ 最外层 update 收尾 finish_update():pending_updates 归 1
     └─ flush_effects() 循环:
          ├─ 先 release_dropped_entities():清掉已死实体的订阅
          ├─ pop 队头 Effect
          │    ├─ Notify{emitter} → observers.retain(emitter, 逐个调观察者)
          │    ├─ Emit{…}        → event_listeners 按事件类型派发
          │    ├─ Defer{…}       → 执行延迟回调(订阅激活也靠它)
          │    └─ …
          ├─ 回调里若又 notify/emit → 继续入队,循环处理
          └─ 队列清空 → 结束本周期(测试模式下顺便画脏窗口)
```

订阅注册同样有一条小流程:`observe` 内部 `SubscriberSet::insert` 先返回**惰性(inert)**订阅,再用一个 `defer` 的 activate 闭包把它激活——避免「注册过程中就触发派发」的重入问题。观察者回调返回 `false`(通常因为弱句柄升级失败,即观察者已死)时,`retain` 会顺手移除该订阅。

#### 4.3.3 源码精读

**Effect 枚举:一周期内可能发生的全部副作用**,见 [crates/gpui/src/app.rs:2826-2847](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2826-L2847)。六种:`Notify`(实体变了)、`Emit`(实体发事件)、`RefreshWindows`、`NotifyGlobalObservers`、`Defer`、`EntityCreated`。

**入口:`App::notify` 的双路径**,见 [crates/gpui/src/app.rs:2615-2649](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2615-L2649)。它先查 `window_invalidators_by_entity`:实体若正被某窗口渲染,直接调用 invalidator 让窗口变脏(省去绕队列一圈);否则走队列。去重逻辑在 `push_effect`,见 [crates/gpui/src/app.rs:1593-1609](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1593-L1609)——`pending_notifications.insert` 返回 false 说明本周期已入过队,直接丢弃。

**派发器:`flush_effects` 的「跑到队列干涸」循环**,见 [crates/gpui/src/app.rs:1614-1670](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1614-L1670)。每圈先清死亡实体,再弹一个效果处理;注释明言「Effects can themselves cause effects, so we continue looping until all effects are processed」。它由 `App::update` 的收尾 `finish_update` 触发——`pending_updates` 计数器保证嵌套 update 只在最外层刷新一次,见 [crates/gpui/src/app.rs:1048-1066](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1048-L1066)。`Notify` 效果落到 `apply_notify_effect`,对 `observers` 表按 emitter 逐个 `retain` 派发,见 [crates/gpui/src/app.rs:1717-1723](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1717-L1723)。

**注册:`Context::observe` 把「弱引用观察者」封装到位**,见 [crates/gpui/src/app/context.rs:63-81](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L63-L81)。它先把 `self.weak_entity()`(观察者的弱句柄)捕获进闭包;被观察者 notify 时先 `this.upgrade()`,成功则以 `Context<T>` 重新进入观察者的 update;失败返回 `false`,订阅随之拆除。`observe_self`(自己观察自己)与事件版的 `subscribe`/`subscribe_self` 同构,见 [crates/gpui/src/app/context.rs:84-132](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L84-L132)。不带观察者身份的裸版本是 `App::observe` / `App::subscribe`,见 [crates/gpui/src/app.rs:1069-1081](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1069-L1081) 与 [crates/gpui/src/app.rs:1158-1171](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1158-L1171)。

**事件的发送端:`cx.emit` 只是入队**,见 [crates/gpui/src/app/context.rs:763-780](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L763-L780):事件值分配进事件竞技场(arena,周期结束统一回收),连同 `TypeId::of::<Evt>()` 压入 `Effect::Emit`;`subscribe_internal` 派发时按 `TypeId` 匹配并 `downcast_ref`,见 [crates/gpui/src/app.rs:1182-1207](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1182-L1207)。事件类型与实体的绑定关系就是那个空标记 trait `EventEmitter<E>`,见 [crates/gpui/src/gpui.rs:294-296](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L294-L296)。

**订阅容器 `SubscriberSet` 与 RAII 句柄 `Subscription`**:

- `insert` 返回 `(Subscription, activate)` 二元组,订阅初始惰性,激活被推迟,见 [crates/gpui/src/subscription.rs:46-87](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/subscription.rs#L46-L87);`new_observer` 用 `defer` 安排激活,见 [crates/gpui/src/app.rs:1128-1132](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L1128-L1132)。
- `retain` 派发时处理「回调内再订阅/再退订」的边界(把回调期间新增的订阅合并回来),见 [crates/gpui/src/subscription.rs:110-144](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/subscription.rs#L110-L144);该文件自带测试覆盖这些边角,见 [crates/gpui/src/subscription.rs:207-249](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/subscription.rs#L207-L249)。
- `Subscription` 本体:drop 即退订、`detach` 放手、`join` 合并,见 [crates/gpui/src/subscription.rs:147-194](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/subscription.rs#L147-L194)。它带 `#[must_use]`——丢掉返回值会立刻退订,编译器直接警告你。

**真实项目里的标准姿势**:Zed 的 `Editor` 在构造时把所有订阅收进 `_subscriptions: Vec<Subscription>` 字段(见 [crates/editor/src/editor.rs:1060](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/editor/src/editor.rs#L1060)),并用 `cx.observe(&multi_buffer, Self::on_buffer_changed)` 监听底层缓冲,见 [crates/editor/src/editor.rs:2426-2429](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/editor/src/editor.rs#L2426-L2429)。实体 drop → 字段 drop → 订阅全拆,生命周期天然对齐。

#### 4.3.4 代码实践

headless 测试版实践(不需要显示器,可在任何机器跑):

1. **实践目标**:用一个 `#[gpui::test]` 验证「notify → observe 回调 → 回调时机在 update 闭包之后」这条链路。
2. **操作步骤**:在你自己的克隆里新建一个测试文件(例如把下面内容追加到 `crates/gpui/examples/` 下任一自有示例的 `#[cfg(test)]` 模块,或参照 [testing.rs:224-246](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L224-L246) 的写法放到你自己的测试中),内容为**示例代码**(非项目原有代码):

   ```rust
   use gpui::{Context, Entity, TestAppContext};

   struct Counter { count: i32 }
   struct Watcher { seen: Vec<i32>, _subscription: gpui::Subscription }

   #[gpui::test]
   fn observe_notify_chain(cx: &mut TestAppContext) {
       let counter: Entity<Counter> = cx.new(|_| Counter { count: 0 });
       let watcher = cx.new(|cx: &mut Context<Watcher>| {
           let subscription = cx.observe(&counter, |this: &mut Watcher, counter, cx| {
               this.seen.push(counter.read(cx).count);
           });
           Watcher { seen: Vec::new(), _subscription: subscription }
       });

       counter.update(cx, |c, cx| {
           c.count = 1;
           cx.notify();
           // 此刻回调尚未执行:notify 只是入队
           c.count = 2;
           cx.notify(); // 第二次 notify 会被去重
       });

       let seen = watcher.read_with(cx, |w, _| w.seen.clone());
       assert_eq!(seen, vec![2]); // 只回调一次,且看到的是最终值 2
   }
   ```

   运行:`cargo test -p gpui --example <你的示例名> --features test-support -- observe_notify_chain --nocapture`。
3. **需要观察的现象**:断言 `seen == vec![2]` 通过——两次 `cx.notify()` 只触发一次回调,且回调读到的 `count` 是闭包全部执行完的终值 2。
4. **预期结果**:如上;若把两条 `cx.notify()` 都删掉,`seen` 应为空(`update` 改数据本身不触发 observe)。回调时机(闭包后、update 调用返回前)待本地验证。
5. 若 `#[gpui::test]` 宏找不到,确认启用了 `test-support` feature(参考 testing.rs 顶部的运行说明,[testing.rs:7-8](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L7-L8))。

#### 4.3.5 小练习与答案

**练习 1**:`cx.notify()` 连续调用 5 次和 1 次,观察者回调各执行几次?为什么?
**答案**:都只执行 1 次。`push_effect` 用 `pending_notifications: FxHashSet<EntityId>` 去重——同一实体在同一更新周期内,`Effect::Notify` 只入队一次(见 app.rs:1595-1598)。这就是「先改完、再统一通知」的代价与收益:中间态不会被打扰。

**练习 2**:实体 A 持有 `Entity<B>` 强句柄,B 又持有 `Entity<A>` 强句柄,会发生什么?两种解法是什么?
**答案**:两个实体的句柄计数永不归零,`EntityMap` 里双方永远无法释放,订阅和状态全部泄漏。解法一:至少一侧改用 `WeakEntity<T>`,访问时 `upgrade`/`update` 并处理 `Err`;解法二:若只是订阅关系,依赖 observe/subscribe 内部自带的弱句柄机制——观察者死亡后弱升级失败、回调返回 false,订阅被自动移除。

**练习 3**:`cx.observe(&other, ...)` 返回的 `Subscription` 如果不存进字段而是当场 drop,现象是什么?如果存进字段后实体被销毁呢?
**答案**:当场 drop 会立刻退订(Subscription 的 Drop 调用 unsubscribe),此后 `other` 的 notify 不再触发任何回调——所以返回值带 `#[must_use]`。存进字段后:观察者实体销毁时字段随之 drop、自动退订;被观察者销毁时,`release_dropped_entities` 会整表移除它名下的 observers,回调也不会再触发。两个方向都不会悬挂。

**练习 4**:什么时候该用 `observe`,什么时候该用 `subscribe` + `emit`?
**答案**:`observe` 表达「状态可能变了,请重新读我」——语义弱、无载荷,适合驱动重绘和派生计算(如 Editor 观察 multi_buffer 后重算显示映射)。`subscribe` 表达「发生了一个具体事件」——有类型化载荷,适合向上层传递语义明确的动作(如 `Event::Opened`、`Event::Deleted`),上层无需回读实体状态就能决策。Zed 代码里两者常并存:observe 管刷新,subscribe 管业务。

## 5. 综合实践

把三个模块串起来,写一个完整的最小 GPUI 程序:**Counter 实体 + Watcher 实体,Watcher 通过 `cx.observe` 监听 Counter 并把每次变化记进日志,UI 上用按钮驱动计数,终端同步打印观察日志**。

1. **实践目标**:在一个真实窗口程序里跑通「按钮点击 → `cx.notify()` → observe 回调 → 状态与 UI 更新」全链路,并用对照实验验证 Subscription 的生命周期语义。
2. **操作步骤**(在你自己的克隆中进行;以下为**示例代码**,骨架参照 [crates/gpui/examples/testing.rs:180-201](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L180-L201) 与 [crates/gpui/examples/hello_world.rs:92-109](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L92-L109)):
   - 在 `crates/gpui/examples/` 下新建 `counter_watch.rs`,定义两个实体:

     ```rust
     use gpui::{App, Context, Entity, Render, Subscription, Window, div, prelude::*};

     struct Counter { count: i32 }
     impl Counter {
         fn increment(&mut self, cx: &mut Context<Self>) {
             self.count += 1;
             cx.notify();
         }
     }

     struct Watcher {
         logs: Vec<String>,
         _subscription: Subscription,
     }
     ```

   - 在 `main` 里先建 Counter,再建 Watcher,构造时用 `cx.observe` 挂上监听并 `eprintln!` 打印:

     ```rust
     fn main() {
         gpui_platform::application().run(|cx: &mut App| {
             let counter: Entity<Counter> = cx.new(|_| Counter { count: 0 });
             cx.new(|cx: &mut Context<Watcher>| {
                 let _subscription = cx.observe(&counter, |this, counter, cx| {
                     let count = counter.read(cx).count;
                     this.logs.push(format!("counter -> {count}"));
                     eprintln!("[watcher] counter -> {count}");
                 });
                 Watcher { logs: Vec::new(), _subscription }
             });
             // …打开窗口渲染 Counter 与按钮,按钮 on_click 里调用
             // counter.update(cx, |c, cx| c.increment(cx))
         });
     }
     ```

     窗口与按钮部分可整段照搬 [testing.rs:80-135](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs#L80-L135) 的 Counter 渲染代码,把 `increment` 换成上面的签名即可。
   - 运行:`cargo run -p gpui --example counter_watch`(README 记载的标准运行方式见 [crates/gpui/examples/README.md:3-7](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/README.md#L3-L7))。
3. **需要观察的现象**:每点一次 "+" 按钮,终端立即多一行 `[watcher] counter -> N`,N 与界面数字一致;连点三次出现三行递增日志。
4. **对照实验**:
   - 实验 A:把 `_subscription` 字段从 `Watcher` 中删除、`cx.observe(...)` 的返回值用 `let _ = ...` 丢弃(或直接加分号),再点按钮——终端不应再有任何输出(订阅被立刻退订)。
   - 实验 B:在 observe 回调里打印两条 `eprintln!`(一前一后),并在中间调用 `counter.update(cx, ...)` 再加一——观察是否触发 panic(嵌套 update 同一实体会命中租借检查),加深对 4.1「租借」机制的理解。
5. **预期结果**:主实验每次点击恰好一行日志,数字与 UI 一致;实验 A 无输出;实验 B 在嵌套 update 处 panic(报错含 "while it is already being updated")。GUI 侧的具体渲染效果与 panic 时机待本地验证。
6. 完成后可尝试把 Watcher 的观察方式改写为事件版:给 Counter 实现 `EventEmitter<CounterEvent>`,`increment` 里 `cx.notify()` 之外再 `cx.emit(CounterEvent { new_count })`,Watcher 改用 `cx.subscribe`,比较两种日志的差异。

## 6. 本讲小结

- **所有权分离**:`App` 通过 `EntityMap`(SecondaryMap + slotmap 计数表)真正拥有所有实体;`Entity<T>` 只是「id + 类型标签 + 计数参与」的惰性句柄,访问状态必须出示上下文。
- **update = 租借**:`Entity::update` 把装箱的值租到栈上给闭包 `&mut T`,结束归还;嵌套 update 同一实体触发租借 panic。释放由句柄计数驱动,死亡实体的订阅在效果循环中被统一拆除。
- **上下文是一座塔**:`Context<T>` = `&mut App` + 弱身份标签(靠 `Deref` 继承 App 能力);`AsyncApp` 弱持有 App、可跨 await;`AppContext` trait 让三者共用 `new`/`update_entity`/`read_entity` 等泛型 API。
- **notify 不等于回调执行**:`cx.notify()` 只是去重入队一个 `Effect::Notify`,真正的观察者派发发生在最外层 `update` 收尾的 `flush_effects` 循环——所以回调看到的是终值,且天然防重入。
- **observe 与 subscribe 分工**:前者广播「我变了」(无载荷,驱动重绘),后者发送类型化事件(有载荷,驱动业务);订阅的 RAII 句柄 `Subscription` drop 即退订,必须存进字段或显式 detach。
- **防泄漏三板斧**:观察回调内部弱引用观察者(死亡自动退订)、实体间互相持有用 `WeakEntity`、订阅收进 `_subscriptions: Vec<Subscription>` 字段与实体同寿命——这正是 Editor、Workspace 等真实 crate 的标准写法。

## 7. 下一步学习建议

本讲解决的是「状态存哪、怎么改、怎么通知」,但还有两个关键问题没展开:**状态怎么变成屏幕上的像素**(下一讲),以及**耗时任务怎么不卡 UI**(u2-l6)。建议:

1. **下一讲 u2-l3「Element 与 Render:声明式 UI 渲染」**:`Render` trait 如何消费本讲的 `Context<T>`,`cx.notify()` 触发的重绘在渲染侧如何落地,`RenderOnce` 与 `derive(IntoElement)` 的组件化方式。你会发现「Entity 存状态 + 每帧重建元素树」正是 u2-l1 说的混合立即/保留模式的实现细节。
2. 在进入下一讲前,建议重读官方文档 [crates/gpui/src/_ownership_and_data_flow.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/_ownership_and_data_flow.rs),它用与本讲相同的 Counter 例子把所有权、观察、数据流串讲了一遍,是极佳的复习材料。
3. 顺手通读 [crates/gpui/examples/testing.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/testing.rs) 全文:它的测试部分(含 `run_until_parked`、多 App 模拟分布式系统)预演了 u2-l6 的并发模型与 u8-l5 的测试体系。
