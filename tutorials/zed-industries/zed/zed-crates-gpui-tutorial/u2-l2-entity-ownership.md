# Entity 与所有权模型：App 拥有一切

## 1. 本讲目标

上一讲（u2-l1）我们弄清了 `Application` / `AppCell` / `App` 三层结构与单前台线程借用模型。本讲回答一个更根本的问题：**应用的状态到底放在哪里、以什么方式被访问？**

学完本讲，你应该能够：

1. 解释 `Entity<T>` 为什么长得像 `Rc<T>`，却必须借助 `App` 才能读写底层状态。
2. 熟练使用 `cx.new` / `read` / `read_with` / `update` / `write` / `downgrade` 这组 API，并知道各自的适用场景。
3. 说出 `EntityMap` 的三块存储（实体表、引用计数表、已释放队列）与两阶段创建（`reserve` → `insert`）、两阶段更新（`lease` → `end_lease`）机制。
4. 理解实体的回收时机：引用计数归零 ≠ 立刻析构，真正的清理发生在效果刷新（`flush_effects`）阶段。
5. 识别强句柄循环引用导致的实体泄漏，并会用 `WeakEntity<T>` 打破循环。

## 2. 前置知识

### 2.1 Rc 与引用计数（Rust 标准库）

`Rc<T>` 是「引用计数智能指针」：每克隆一次 `Rc`，计数加一；每丢弃一次，计数减一；计数归零时，堆上的 `T` 被析构并释放。它让多个所有者**共享**同一份数据。

GPUI 的 `Entity<T>` 沿用了这套计数逻辑，但做了一个关键拆分：

- `Rc<T>`：句柄本身持有数据指针，克隆句柄 = 共享所有权，随时可解引用。
- `Entity<T>`：**数据不在句柄里，而在 `App` 的仓库（`EntityMap`）里**。句柄只是「编号 + 类型标签」，想碰数据必须回到 `App` 办手续。

官方文档把这句话说得很直白：这个句柄"仅仅是一个惰性标识符加上一个编译期类型标签"（merely an inert identifier plus a compile-time type tag）。

### 2.2 弱引用 Weak

`Rc::downgrade` 得到 `Weak<T>`，它指向数据但不增加强计数；强计数归零后 `Weak` 无法再 `upgrade` 回 `Rc`。这是打破 `Rc` 循环引用的标准手段。GPUI 的 `Entity::downgrade` / `WeakEntity<T>` 完全对应这套心智模型。

### 2.3 承接上一讲

u2-l1 的结论在本讲处处用到：

- 所有前台代码跑在单一前台线程上，`App` 被 `AppCell`（`RefCell<App>`）包裹；
- 每次更新的最外层结束时，`App` 会执行 `flush_effects` 消化积压效果（Notify、Emit、Defer 等）。

本讲会看到：**实体回收也是效果刷新的一部分**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/app/entity_map.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs) | 本讲主战场：`EntityId`、`EntityMap`、`Entity<T>`、`WeakEntity<T>`、`AnyEntity` 及泄漏检测器全部定义在此 |
| [src/_ownership_and_data_flow.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs) | 官方所有权文档模块，用 `Counter` 例子从零讲解实体模型，本讲多处引用 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs) | `App` 持有 `entities: EntityMap` 字段；`AppContext` trait 的 `new`/`update_entity`/`read_entity` 实现演示租约流程与回收时机 |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs) | crate 注册表：`pub use app::*`（L95）把 entity_map 里的类型摊平进 `gpui::` 命名空间；L170-245 定义 `AppContext` trait |

> 反查提示（u1-l3 技能）：`gpui::Entity` 并不定义在某个 `entity.rs` 里。挂载链是 `src/app.rs:65` 的 `mod entity_map` → `src/app.rs:30` 的 `pub use entity_map::*` → `src/gpui.rs:95` 的 `pub use app::*`。用 `Grep` 而不是文件名猜位置。

## 4. 核心概念与源码讲解

### 4.1 EntityMap：App 背后的实体仓库

#### 4.1.1 概念说明

GPUI 里所有的应用状态（计数器、编辑器缓冲区、面板……）都叫**实体**（entity）。官方文档第一句话就定调：

> In GPUI, every model or view in the application is actually owned by a single top-level object called the `App`.
> （在 GPUI 中，应用的每个模型或视图实际上都被一个叫 `App` 的顶层对象拥有。）
> —— [_ownership_and_data_flow.rs:L1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs#L1)

`EntityMap` 就是 `App` 用来履行「拥有者」职责的仓库。它解决三个问题：

1. **类型擦除地存放所有实体**：`App` 不能为每种状态类型建一张表，所以统一装箱为 `Box<dyn Any>` 存进一张以 `EntityId` 为键的表。
2. **跨句柄的引用计数**：计数不存在每个句柄里，而是集中放在一张独立的计数表里，句柄克隆/销毁时远程增减。
3. **延迟回收**：计数归零的实体先登记到「已释放队列」，等效果刷新时统一清理，避免在使用中途析构。

#### 4.1.2 核心流程

一个实体的一生：

```
cx.new(|cx| Counter { count: 0 })
   │
   ├─ ① reserve()      在计数表里占一个空槽，计数初始化为 1，拿到 EntityId
   ├─ ② build_entity   用临时 Context 构造 Counter 状态
   └─ ③ insert()       状态装箱存入实体表，返回 Entity<T> 句柄

使用期：
   read(handle)        从实体表取出 &T（只读，不搬动）
   lease(handle)       把实体从表里【搬到栈上】，得到可变租约 Lease<T>
   end_lease(lease)    更新结束，把实体放回表里

死亡：
   最后一个强句柄 drop  →  计数减到 0  →  EntityId 进 dropped_entity_ids 队列
   下一次 flush_effects → take_dropped() 取出队列 → 清理观察者/监听器 → 析构状态
```

两阶段创建（reserve → insert）的意义：构造 `Counter` 时传进来的 `Context<T>` 需要提前知道「我将来是谁」，这样构造过程中就能 `cx.observe(...)` 甚至引用自己的 `EntityId`——官方文档特意演示了"可以在 Counter 创建之前就挂好回调"（[_ownership_and_data_flow.rs:L66-L67](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs#L60-L77)）。

两阶段更新（lease → end_lease）的意义：把实体**搬出**表再交给你，表里此刻没有这份状态。于是「在同一层更新里再次更新同一实体」会在取件时扑空，直接 panic——这就是 GPUI 防止可变别名（两个 `&mut` 指向同一实体）的物理保证，比 `RefCell` 运行时检查更干脆。

#### 4.1.3 源码精读

**EntityId：全应用唯一的编号。**

[entity_map.rs:L27-L30](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L27-L30) 用 `slotmap` 的宏声明了一个新的 key 类型。slotmap 的 key 内含版本号，槽位复用时旧 key 自动失效，避免「编号被回收后又冒出新实体被旧句柄误访问」：

```rust
slotmap::new_key_type! {
    /// A unique identifier for a entity across the application.
    pub struct EntityId;
}
```

**EntityMap 的三块存储。**

[entity_map.rs:L56-L68](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L56-L68)：左边是结构定义，右边是我加的注释：

```rust
pub(crate) struct EntityMap {
    entities: SecondaryMap<EntityId, Box<dyn Any>>,   // 实体本体：状态装箱存放
    pub accessed_entities: RefCell<FxHashSet<EntityId>>, // 本效果周期内被碰过的实体
    ref_counts: Arc<RwLock<EntityRefCounts>>,          // 引用计数表（独立加锁）
}

pub(crate) struct EntityRefCounts {
    counts: SlotMap<EntityId, AtomicUsize>,  // 每个 EntityId 一个原子计数
    dropped_entity_ids: Vec<EntityId>,       // 计数归零、等待回收的实体
    ...
}
```

注意两个设计点：

- **计数表用 `Arc` 包住**：句柄（`AnyEntity`）只握 `Weak<RwLock<EntityRefCounts>>`，既能在 clone/drop 时远程增减计数，又不延长 `App` 的生命（[entity_map.rs:L249](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L245-L252)）。`assert_valid_context` 还借此校验「句柄属于哪个 App」——拿 A 应用的句柄去更新 B 应用会触发 debug 断言（[entity_map.rs:L167-L172](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L167-L172)）。
- **计数是 `AtomicUsize`**：虽然实体读写只发生在前台线程，但句柄的 clone/drop 可能发生在任意线程（例如后台任务里顺手 clone 了一个句柄），所以计数必须原子。

**两阶段创建。**

[entity_map.rs:L113-L130](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L113-L130)：`reserve` 先在计数表占槽（初始计数为 1，代表返回的 `Slot` 自己持有一个强句柄），`insert` 再把状态装箱入表：

```rust
pub fn reserve<T: 'static>(&self) -> Slot<T> {
    let id = self.ref_counts.write().counts.insert(1.into());
    Slot(Entity::new(id, Arc::downgrade(&self.ref_counts)))
}

pub fn insert<T>(&mut self, slot: Slot<T>, entity: T) -> Entity<T> where T: 'static {
    ...
    let handle = slot.0;
    self.entities.insert(handle.entity_id, Box::new(entity));
    handle
}
```

对应的 `AppContext::new` 实现在 [app.rs:L2719-L2733](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2714-L2733)：`cx.new(...)` 内部正是 `reserve → build_entity → push EntityCreated 效果 → insert`，同时把 `slot.downgrade()` 塞进临时 `Context`，让构造闭包能引用"尚未存在的自己"：

```rust
fn new<T: 'static>(&mut self, build_entity: impl FnOnce(&mut Context<T>) -> T) -> Entity<T> {
    self.update(|cx| {
        let slot = cx.entities.reserve();
        let handle = slot.clone();
        let entity = build_entity(&mut Context::new_context(cx, slot.downgrade()));
        cx.push_effect(Effect::EntityCreated { ... });
        cx.entities.insert(slot, entity)
    })
}
```

`reserve` 也单独暴露为 `cx.reserve_entity()` / `cx.insert_entity()`（[gpui.rs:L180-L191](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L180-L191)，返回的 `Reservation<T>` 可先查出 `entity_id()`，见 [gpui.rs:L247-L256](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L247-L256)）——两个实体互相需要对方编号时用得上。

**两阶段更新与租约。**

[entity_map.rs:L132-L154](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L132-L154)：`lease` 用 `remove` 把实体**搬出**表（不是借用！），`end_lease` 再放回去：

```rust
pub fn lease<T>(&mut self, pointer: &Entity<T>) -> Lease<T> {
    ...
    let entity = Some(
        self.entities
            .remove(pointer.entity_id)
            .unwrap_or_else(|| double_lease_panic::<T>("update")),  // 已被借走 → panic
    );
    Lease { entity, id: pointer.entity_id, entity_type: PhantomData }
}

pub fn end_lease<T>(&mut self, mut lease: Lease<T>) {
    self.entities.insert(lease.id, lease.entity.take().unwrap());
}
```

`Lease<T>`（[entity_map.rs:L214-L240](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L214-L240)）实现 `Deref/DerefMut`，让 `update` 闭包直接把 `Lease` 当 `&mut T` 用；它的 `Drop` 带保险丝——忘了 `end_lease` 直接 panic：

```rust
impl<T> Drop for Lease<T> {
    fn drop(&mut self) {
        if self.entity.is_some() && !panicking() {
            panic!("Leases must be ended with EntityMap::end_lease")
        }
    }
}
```

重入更新的报错文案见 [entity_map.rs:L206-L212](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L206-L212)：`cannot update T while it is already being updated`。

**延迟回收。**

计数归零只是「进队列」（见 4.2.3 中 `Drop for AnyEntity`），真正的清理在 [entity_map.rs:L184-L203](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L184-L203) 的 `take_dropped`：把 `dropped_entity_ids` 排干、删除计数槽、从实体表摘除状态并返回给调用方。

谁来调用它？[app.rs:L1627-L1683](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1624-L1683) 的 `flush_effects` 每轮循环开头都先跑 `release_dropped_entities`；而 `flush_effects` 又由最外层 `App::update` 的收尾触发（[app.rs:L1045-L1063](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1045-L1063)）。[app.rs:L1688-L1705](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1685-L1705) 展示了清理时还要摘掉该实体挂的观察者、事件监听器、窗口失效器，并回调 `release_listeners`：

```rust
fn release_dropped_entities(&mut self) {
    loop {
        let dropped = self.entities.take_dropped();
        if dropped.is_empty() { break; }
        for (entity_id, mut entity) in dropped {
            self.observers.remove(&entity_id);
            self.event_listeners.remove(&entity_id);
            ...
        }
    }
}
```

为什么要延迟？因为 drop 句柄的瞬间可能正处于某个更新链的中途——别的实体或许还握着临时引用、观察者回调可能还在排队。等效果刷新时再统一回收，能保证回收动作发生在稳定的间隙。

#### 4.1.4 代码实践

**实践目标**：不改任何框架代码，仅通过阅读 `EntityMap` 自带的单元测试，验证「两阶段创建」和「弱句柄在回收前就失效」两个行为。

**操作步骤**：

1. 打开 [entity_map.rs:L1185-L1278](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L1185-L1278) 的 `mod test`，通读四个测试。
2. 重点读 [`test_entity_map_weak_upgrade_before_cleanup`](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L1216-L1239)：它先 `insert` 一个实体、`downgrade`、`drop(handle)`，然后立刻 `weak.upgrade()` 并断言结果为 `None`，最后才 `take_dropped`。
3. 运行这些测试：

   ```bash
   cargo test -p gpui --lib entity_map
   ```

**需要观察的现象**：测试全部通过；特别是 `weak.upgrade()` 在 `take_dropped()` 之前就已经返回 `None`。

**预期结果**：这证明「强句柄计数归零」与「实体状态被析构」是两个分离的时刻——计数归零的瞬间弱句柄就升级不回来了，但状态清理要等到 `take_dropped`。输出里还能看到 `test_entity_map_slot_assignment_before_cleanup` 验证了槽位在 `take_dropped` 前不会被提前复用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EntityMap::lease` 用 `remove`（搬出）而不是 `get_mut`（表内可变借用）？

**答案**：`get_mut` 会以 `&mut self`（整张 SecondaryMap）的借用活着，更新闭包执行期间若再通过 `EntityMap` 读写任何其他实体都会被借用检查拒绝；更关键的是防重入——`remove` 之后表里没有这份状态，同一实体被嵌套 `update` 时第二次 `lease` 会取件扑空，走到 `double_lease_panic`，从物理上杜绝了两个 `&mut` 指向同一实体。

**练习 2**：`EntityMap` 里 `accessed_entities` 集合的作用是什么？

**答案**：记录当前效果周期内被读/写/租借过的实体（`insert`、`lease`、`read` 都会往里塞 id，见 [entity_map.rs:L124-L165](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L113-L165)）。配合 `extend_accessed` / `clear_accessed`，框架在处理 `Effect::Notify` 等效果时可以知道哪些实体在本周期被访问过，用于观察者通知与失效判断的传播。（深入效果系统是 u2-l3 的主题。）

### 4.2 Entity\<T\>：带类型标签的引用计数句柄

#### 4.2.1 概念说明

`Entity<T>` 是你日常打交道最多的类型。它的定义出乎意料地"空"（[entity_map.rs:L411-L419](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L411-L419)）：

```rust
pub struct Entity<T> {
    pub(crate) any_entity: AnyEntity,
    pub(crate) entity_type: PhantomData<fn(T) -> T>,  // 只是个类型标签
}
```

而 `AnyEntity`（[entity_map.rs:L245-L252](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L245-L252)）只有三样东西：编号、`TypeId`、指向计数表的弱指针。**没有任何数据指针**。

与 `Rc<T>` 的对比表：

| | `Rc<T>` | `Entity<T>` |
| --- | --- | --- |
| 数据在哪 | 句柄指向的堆分配 | `App` 的 `EntityMap` 里 |
| 计数在哪 | 数据旁边的 RcBox 里 | 独立的 `EntityRefCounts` 表 |
| 解引用 | `*rc` 随时可用 | 必须 `entity.read(cx)` / `entity.update(cx, ...)` |
| 类型擦除 | 需要 `Rc<dyn Any>` | 内建 `AnyEntity`，可 `downcast` 还原类型 |
| 变体 | `Weak<T>` | `WeakEntity<T>` |

为什么强制经过 `App`？官方文档给出的理由是：让状态参与应用级服务（观察、事件、窗口失效等）并与其它实体交互，就必须有一个统一的汇聚点做"访问管理"。`App` 正是这个汇聚点——每一次 `update` 都是一次有登记的访问（进 `accessed_entities`），GPUI 由此获得了 `Rc` 给不了的能力：**跨实体的响应式通知**（u2-l3 展开）。

#### 4.2.2 核心流程

读写一个实体的完整调用链（对照上图 ①②③）：

```
只读：entity.read(cx)
  Entity::read ─→ App::entities.read ─→ 表内 downcast_ref::<T>() ─→ &T

回调式只读：entity.read_with(cx, |t, cx| ...)
  Entity::read_with ─→ AppContext::read_entity ─→ 同上，再进闭包

更新：entity.update(cx, |t, cx| ...)
  Entity::update ─→ AppContext::update_entity(App 实现)
                  ├─ App::update(进入更新计数)
                  ├─ entities.lease(handle)     ← 状态搬出表
                  ├─ update(&mut entity, &mut Context::new_context(...))
                  └─ entities.end_lease(entity) ← 状态放回表
                  （App::update 收尾时 flush_effects）

整体替换：entity.write(cx, value)
  等价于 update(cx, |entity, cx| { *entity = value; cx.notify(); })
```

`update` 闭包拿到的第二个参数 `Context<T>` 是包着 `App` 的"实体级上下文"，能调 `cx.notify()`、`cx.observe(...)`、`cx.emit(...)` 等实体级服务（[_ownership_and_data_flow.rs:L34-L51](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs#L34-L51)）。

#### 4.2.3 源码精读

**read / update 家族。**

[entity_map.rs:L462-L509](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L462-L509) 是 `Entity<T>` 上全部访问方法。`read` 走 `cx.entities.read` 拿 `&T`；`read_with`/`update` 委托给 `AppContext` trait 的对应方法（因此任何上下文——`App`、`Context<U>`、`AsyncApp`——都能用，这是 [gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L210) 定义该 trait 的意义）：

```rust
pub fn read<'a>(&self, cx: &'a App) -> &'a T {
    cx.entities.read(self)
}

pub fn update<R, C: AppContext>(&self, cx: &mut C,
    update: impl FnOnce(&mut T, &mut Context<T>) -> R) -> R {
    cx.update_entity(self, update)
}
```

`update` 的 `App` 实现在 [app.rs:L2753-L2767](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2751-L2767)，与 4.1.3 的租约流程一一对应：

```rust
fn update_entity<T: 'static, R>(&mut self, handle: &Entity<T>,
    update: impl FnOnce(&mut T, &mut Context<T>) -> R) -> R {
    self.update(|cx| {
        let mut entity = cx.entities.lease(handle);
        let result = update(&mut entity,
            &mut Context::new_context(cx, handle.downgrade()));
        cx.entities.end_lease(entity);
        result
    })
}
```

注意 `Context::new_context(cx, handle.downgrade())`：传给闭包的上下文持有的是**弱**句柄，因此 `Context<T>` 自身不会让实体多一个强引用。

`write` 是个便捷方法（[entity_map.rs:L491-L497](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L491-L497)）：整体替换状态并自动 `cx.notify()`。另外 `update_in`（[entity_map.rs:L502-L508](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L502-L508)）要求 `VisualContext`（带窗口的上下文），闭包额外多一个 `&mut Window` 参数——绘制相关更新用，u3 之后再深入。

**克隆 = 计数加一。**

[entity_map.rs:L311-L337](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L311-L337)：

```rust
impl Clone for AnyEntity {
    fn clone(&self) -> Self {
        if let Some(entity_map) = self.entity_map.upgrade() {
            let entity_map = entity_map.read();
            let count = entity_map.counts.get(self.entity_id)
                .expect("detected over-release of a entity");
            let prev_count = count.fetch_add(1, SeqCst);
            assert_ne!(prev_count, 0, "Detected over-release of a entity.");
        }
        ...
    }
}
```

`Entity<T>` 的 `Clone`（[entity_map.rs:L511-L519](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L511-L519)）直接复用它。克隆成本 = 一次原子加法 + 三个字段复制，非常便宜，这也是 GPUI 代码里随处直接 `handle.clone()` 的底气。

**Drop = 计数减一，减到 0 进回收队列。**

[entity_map.rs:L339-L364](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L339-L364)：

```rust
impl Drop for AnyEntity {
    fn drop(&mut self) {
        ...
        let prev_count = count.fetch_sub(1, SeqCst);
        assert_ne!(prev_count, 0, "Detected over-release of a entity.");
        if prev_count == 1 {
            // 我们是最后一个引用，可以把实体移除了
            entity_map.dropped_entity_ids.push(self.entity_id);
        }
    }
}
```

`prev_count == 1` 说明这次 drop 后计数为 0——注意此刻**什么都没析构**，只是把 id 压进 `dropped_entity_ids`，等待 4.1.3 讲的 `flush_effects` 阶段统一处理。

**相等、哈希、排序都看 EntityId。**

[entity_map.rs:L530-L565](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L530-L565)：`Entity<T>` 的 `Hash/PartialEq/Eq/Ord` 全部转发到 `entity_id`。所以 `Entity<T>` 可以直接做 `HashMap` 的键、可以比较——比较的是"是不是同一个实体"，与状态内容无关。`entity_id()` 方法（[entity_map.rs:L441-L445](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L441-L445)）返回这个编号，日志里打印它比打印指针有用得多。

**类型擦除与还原。**

`Entity::into_any` 把强句柄变成 `AnyEntity`（[entity_map.rs:L456-L460](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L456-L460)）；`AnyEntity::downcast::<T>()` 尝试还原成强类型句柄，类型不符时返回 `Err(原句柄)`（[entity_map.rs:L297-L308](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L297-L308)）。框架内部（比如窗口要持有"任意类型的根视图"）大量依赖这组转换。

#### 4.2.4 代码实践

**实践目标**：亲手创建一个 `Counter` 实体，用 `update` 递增并打印，体验「句柄 + 上下文」的访问方式。

**操作步骤**：

1. 在 `crates/gpui/examples/` 下新建 `entity_counter.rs`（以下为**示例代码**，仿照官方文档 [_ownership_and_data_flow.rs:L40-L52](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs#L40-L52) 与 hello_world 骨架编写）：

   ```rust
   // 示例代码：crates/gpui/examples/entity_counter.rs
   use gpui::{App, AppContext, Context, Entity};

   struct Counter {
       count: usize,
   }

   fn main() {
       gpui_platform::application().run(|cx: &mut App| {
           // ① 创建实体：状态从此归 App 所有，我们只拿到句柄
           let counter: Entity<Counter> = cx.new(|_cx| Counter { count: 0 });

           // ② 句柄自身不持有数据，直接 counter.count 会编译报错；
           //    必须通过 update 借出可变租约
           counter.update(cx, |counter: &mut Counter, cx: &mut Context<Counter>| {
               counter.count += 1;
               println!("count = {}", counter.count);
               cx.notify(); // 告知观察者状态变了（本例没有观察者，仅养成习惯）
           });

           // ③ 只读访问：read 需要一个 &App
           println!("final = {}", counter.read(cx).count);
       });
   }
   ```

2. 运行（本 crate 的示例在 `Cargo.toml` 中逐一声明，见 [Cargo.toml:L177-L179](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L177-L179) 的 `hello_world` 条目格式；若新文件未被自动发现，仿照该格式补一条 `[[example]]` 声明）：

   ```bash
   cargo run -p gpui --example entity_counter
   ```

3. 实验失败路径：把 `counter.update(...)` 改成嵌套两次对同一实体的更新——

   ```rust
   // 示例代码：故意触发重入 panic
   counter.update(cx, |_, cx| {
       counter.update(cx, |_, _| {});  // 同一实体嵌套 update
   });
   ```

**需要观察的现象**：

- 步骤 2 输出 `count = 1` 与 `final = 1` 后正常退出；
- 步骤 3 运行时 panic，报错文案形如 `cannot update gpui::...::Counter while it is already being updated`（即 4.1.3 的 `double_lease_panic`）。

**预期结果**：你会直观感受到「`Entity<T>` 只是惰性句柄」——不借 `cx` 什么都干不了；而重入更新被租约机制在运行时硬性拦截。步骤 2 的确切输出与示例是否被自动发现属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`entity.update` 的闭包参数是 `&mut T` 和 `&mut Context<T>`。为什么第二个参数不是 `&mut App`？

**答案**：`Context<T>` 包装了 `App`（Deref 到它）并额外携带"当前正在更新哪个实体"的信息（构造时传入 `handle.downgrade()`，见 [app.rs:L2759-L2763](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2751-L2767)）。有了这个身份，`cx.notify()` 才知道要通知谁的观察者、`cx.emit()` 才知道事件源是谁。直接给 `&mut App` 就丢失了"当前实体"这一上下文。

**练习 2**：如果把一个 `Entity<T>` 句柄存进另一个实体的状态字段里（很常见的持有方式），句柄 clone 了一份，计数如何变化？状态被两份代码共享了吗？

**答案**：计数加一，但状态始终只有一份，躺在 `EntityMap` 里。字段里的句柄只是"又一枚指向同一编号的凭证"。这也是下一节循环引用问题的起点：两个实体的状态里互相存对方的**强**句柄，计数就永远回不到零。

**练习 3**：`entity.write(cx, value)` 与 `entity.update(cx, |e, _| *e = value)` 差一条什么语句？

**答案**：差 `cx.notify()`。`write` 在替换后自动调用通知（[entity_map.rs:L491-L497](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L491-L497)）；手写 `update` 忘了 notify 的话，观察者与依赖此实体的视图都不会刷新——这是 GPUI 新手最常见的"改了状态界面不动"的原因。

### 4.3 WeakEntity\<T\>：打破循环引用

#### 4.3.1 概念说明

强句柄 `Entity<T>` 构成"共享所有权"。如果实体 A 的状态里存着 `Entity<B>`，B 的状态里又存着 `Entity<A>`，就形成**引用计数循环**：A 不释放 → B 的计数不为 0 → B 不释放 → A 的计数也不为 0。没有任何外部句柄时，两个实体谁也进不了 `dropped_entity_ids`，状态永久滞留在 `EntityMap` 里——这就是 GPUI 意义上的**内存泄漏**。

泄漏检测器的文档把这个场景写得非常清楚（[entity_map.rs:L883-L897](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L883-L897)）：

> Entities are reference-counted structures that can own other entities allowing to form cycles. If such a strong-reference counted cycle is created, all participating strong entities in this cycle will effectively leak as they cannot be released anymore.
> （实体是引用计数结构、可以持有其他实体，因而可能成环。一旦形成强引用计数环，环内所有强实体都无法再被释放，实际上就泄漏了。）

除了显式互持，还有一个隐蔽变体：**实体持有的任务或订阅反过来强引用自己**（文档原话 "Cycles can also happen if an entity owns a task or subscription that it itself owns a strong reference to the entity again"）——`cx.spawn` 闭包里 clone 了强 `Entity` 又把任务存回实体字段，同样成环。

解法与 `Rc` 世界完全一致：需要"回指"时（父指子用强句柄、子回指父用弱句柄），用 `WeakEntity<T>`。它不影响强计数，实体释放后 `upgrade()` 返回 `None`，所有操作以 `Result` 收场，天然处理"对方已不在"的情形。

#### 4.3.2 核心流程

```
Entity::downgrade() ──→ WeakEntity<T>（计数不变）

WeakEntity 使用路径：
  weak.upgrade()      → Option<Entity<T>>    尝试升级为强句柄
     ├─ Some(entity)   → 计数 +1，正常使用
     └─ None           → 实体已释放（或正在回收），优雅降级

  weak.update(cx, f)  → Result<R>
     内部：upgrade() 拿强句柄 → cx.update_entity(...) → 释放强句柄
     失败时返回 Err("entity released")

生命周期对比（同一实体，外部强句柄 drop 之后）：
  强互持（A状态含 Entity<B>，B状态含 Entity<A>）：
     计数 A≥1、B≥1（被环内句柄顶着）→ 永不进 dropped_entity_ids → 泄漏
  弱回指（B状态含 WeakEntity<A>）：
     A 计数归零 → 进队列 → flush_effects 回收 → B 里的 weak.upgrade() 返回 None
```

#### 4.3.3 源码精读

**downgrade：只复制编号，不动计数。**

[entity_map.rs:L447-L454](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L447-L454)：

```rust
pub fn downgrade(&self) -> WeakEntity<T> {
    WeakEntity {
        any_entity: self.any_entity.downgrade(),
        entity_type: self.entity_type,
    }
}
```

`AnyEntity::downgrade`（[entity_map.rs:L288-L295](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L288-L295)）只是搬运 `entity_id`、`entity_type` 和指向计数表的弱指针——没有 `fetch_add`，这就是"不延长生命"的全部含义。

**upgrade：一次原子 CAS 式抢票。**

[entity_map.rs:L583-L617](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L583-L617)：

```rust
pub fn upgrade(&self) -> Option<AnyEntity> {
    let ref_counts = &self.entity_ref_counts.upgrade()?;
    let ref_counts = ref_counts.read();
    let ref_count = ref_counts.counts.get(self.entity_id)?;

    if atomic_incr_if_not_zero(ref_count) == 0 {
        // entity_id 已在 dropped_entity_ids 里
        return None;
    }
    ...
}
```

精髓在 `atomic_incr_if_not_zero`：只有当前计数非零才把它加一并升级成功。若计数已为 0（实体等在回收队列里），升级失败返回 `None`——这正是 4.1.4 那个测试断言的行为。用"比较并交换"而不是"先加再检查"，避免了两个线程同时抢救一个刚死亡实体导致计数复活的竞态。配套的 `is_upgradable()`（[entity_map.rs:L582-L590](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L582-L590)）只探测不增计数。

**弱句柄的 update / read_with：Result 风格 API。**

[entity_map.rs:L765-L826](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L765-L826)。强句柄的 `update` 直接返回 `R`（实体必然活着，否则是你用错了上下文）；弱句柄的 `update` 则要先抢救再干活，失败以 `Err` 交付：

```rust
pub fn update<C, R>(&self, cx: &mut C,
    update: impl FnOnce(&mut T, &mut Context<T>) -> R) -> Result<R>
where C: AppContext {
    let entity = self.upgrade().context("entity released")?;
    Ok(cx.update_entity(&entity, update))
}
```

`update_in` / `read_with` 同理。这就是 CLAUDE.md 里"弱句柄方法总是返回 `anyhow::Result`"的出处——在异步回调和长生命周期订阅里，"目标实体可能已经死了"是常态而非异常，必须显式处理。

**泄漏检测：测试期免费送的安全网。**

在 `test` 或 `leak-detection` feature 下，每次句柄创建/销毁都会被 `LeakDetector` 登记（[entity_map.rs:L932-L1036](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L932-L1036)），并给弱句柄提供 [`assert_released`](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L646-L665)：断言某个实体的所有强句柄都已释放，否则 panic 并列出泄漏句柄的分配栈（设置环境变量 `LEAK_BACKTRACE=1` 可看分配位置，见 [entity_map.rs:L632-L639](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L619-L665)）。`LeakDetector::drop`（[entity_map.rs:L1085-L1118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L1085-L1118)）还会在整场测试结束时兜底检查："Exited with leaked handles"。u7-l4 讲测试时我们会再遇到它。

#### 4.3.4 代码实践

**实践目标**：构造两个互指的实体，先观察强句柄成环导致的"不释放"，再换成 `WeakEntity` 回指，对比 Drop 行为差异。

**操作步骤**：

1. 在 `crates/gpui/examples/` 下新建 `entity_cycle.rs`（**示例代码**，运行方式同 4.2.4，若未被发现需补 `[[example]]` 声明）：

   ```rust
   // 示例代码：crates/gpui/examples/entity_cycle.rs
   use gpui::{App, AppContext, Entity, WeakEntity};

   struct Node {
       name: &'static str,
       // 第一轮实验：强回指（成环）
       peer: Option<Entity<Node>>,
       // 第二轮实验：注释掉上面字段，改用弱回指
       // weak_peer: Option<WeakEntity<Node>>,
   }

   impl Drop for Node {
       fn drop(&mut self) {
           println!("Node({}) dropped", self.name);
       }
   }

   fn main() {
       gpui_platform::application().run(|cx: &mut App| {
           // 用 reserve/insert 两阶段创建，才能在构造时互相拿到对方句柄
           let reservation_a = cx.reserve_entity::<Node>();
           let reservation_b = cx.reserve_entity::<Node>();
           let _id_a = reservation_a.entity_id();
           let _id_b = reservation_b.entity_id();

           // 先占位再互相插入：B 里先放 A 的句柄（此时 A 还没状态）
           let b: Entity<Node> = cx.insert_entity(reservation_b, |_| Node {
               name: "B",
               peer: None, // 稍后回填
           });

           let a: Entity<Node> = cx.insert_entity(reservation_a, |_| Node {
               name: "A",
               peer: Some(b.clone()), // A 强持有 B
           });

           // B 强回指 A —— 环就此形成
           b.update(cx, |b, _| b.peer = Some(a.clone()));

           let weak_a = a.downgrade();
           let weak_b = b.downgrade();

           // 放弃全部外部强句柄
           drop(a);
           drop(b);

           // 用一次 update 驱动 flush_effects（App::update 收尾会刷新效果）
           let probe = cx.new(|_| 0i32);
           probe.update(cx, |_, _| {});

           println!("after flush: a alive? {}, b alive? {}",
               weak_a.upgrade().is_some(),
               weak_b.upgrade().is_some());
           // 若开启 leak-detection，还可以：weak_a.assert_released();
       });
   }
   ```

   注：两阶段创建用到的 `cx.reserve_entity` / `cx.insert_entity` 定义于 [gpui.rs:L180-L191](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L180-L191)。示例里 `probe` 那一步的原理见 [app.rs:L1045-L1063](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1045-L1063)：任何一次实体更新收尾都会触发 `flush_effects`，进而执行实体回收。

2. 第一轮（强回指）运行：

   ```bash
   cargo run -p gpui --example entity_cycle
   ```

3. 第二轮：把 `Node.peer` 换成 `weak_peer: Option<WeakEntity<Node>>`，相应改两处赋值为 `weak_peer: Some(b.downgrade())` 与 `Some(a.downgrade())`，再跑一次。

**需要观察的现象**：

- 第一轮：`Node(A) dropped` / `Node(B) dropped` **一条都不打印**；结尾输出 `after flush: a alive? true, b alive? true`——实体仍在，泄漏实锤。
- 第二轮：两行 `dropped` 都打印出来（打印发生在 `flush_effects` 回收时，可能在 `after flush` 之前或之后，取决于回收批次顺序），结尾输出 `a alive? false, b alive? false`。

**预期结果**：强句柄环让计数永不归零；换弱回指后环断开，外部句柄一放，两个实体在效果刷新时被正常析构。具体打印顺序「待本地验证」（若 run 回调结束时 `flush_effects` 尚未执行到回收阶段，可在结尾再加一次任意 `update` 促发）。

#### 4.3.5 小练习与答案

**练习 1**：既然 `EntityMap` 最终随 `App` 一起析构、所有 `Box<dyn Any>` 都会被倒掉，为什么强句柄环还算"泄漏"？

**答案**：泄漏的伤害期是**应用运行期间**，不是退出时刻。环内实体的状态（连同它持有的缓冲区、任务、订阅）在程序运行的整个生命周期都无法回收，只增不减；而且这些实体永远收不到 `release_listeners` 回调（[app.rs:L1700-L1702](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1688-L1705)），依赖释放钩子的清理逻辑全部失效。退出时连根倒掉只是最后兜底，掩盖不了运行期的占用。测试中 `LeakDetector` 会在退出前就报 "Exited with leaked handles"。

**练习 2**：什么时候该用 `Entity<T>`、什么时候必须 `WeakEntity<T>`？

**答案**：经验的默认取向是——**父持子用强，子回指父用弱；长生命周期方持短生命周期方用弱**。具体判据：问自己"如果对方先死，我这个句柄还该存活吗"。异步任务回写 UI 状态、订阅回调引用主体、子视图引用父容器，都应使用弱句柄（并以 `Result` 处理失败）；普通的数据持有（列表持有条目、面板持有模型）用强句柄即可。另一个信号：`cx.spawn` 的闭包需要 `this: WeakEntity<T>`（CLAUDE.md 记载的 `cx.spawn(async move |this, cx| ...)` 签名），框架已经替你把"任务里引用自己"默认设计成弱引用。

**练习 3**：`weak.upgrade().is_some()` 在强句柄 drop 后立刻调用就会返回 `false`（4.1.4 的测试），但 `assert_released` 的文档却说"实体最近被 drop 但清理未完成时也会 panic"（[entity_map.rs:L641-L645](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L619-L665)）。两者矛盾吗？

**答案**：不矛盾，它们检查的是两个不同层面。`upgrade` 看的是**强计数是否为 0**——drop 后立刻为 0，所以升级失败；`assert_released` 除检查泄漏检测器的句柄登记外，还会检查计数槽是否已从 `counts` 这张 SlotMap 里移除——槽的移除发生在 `take_dropped`（[entity_map.rs:L191](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L184-L203)），即效果刷新阶段。所以测试里要在驱动一次 `flush_effects`（如 `run_until_parked`）之后再 `assert_released`，才不会撞上"已死但未下葬"的中间态。

## 5. 综合实践

**任务：做一个「双计数器观察者」迷你程序，把本讲三个模块串起来。**

要求：

1. **实体建模（模块 4.1/4.2）**：定义 `Counter { count: usize, doubled: Entity<Doubled> }` 与 `Doubled { value: usize }` 两个实体；用 `cx.reserve_entity` + `cx.insert_entity` 的两阶段方式创建，让 `Counter` 构造时就能持有 `Doubled` 的句柄。
2. **租约访问（模块 4.2）**：写一个循环，每次 `counter.update(cx, |c, cx| { c.count += 1; cx.notify(); })` 后，用 `read_with` 打印 `count` 与 `doubled` 的值。
3. **弱句柄回写（模块 4.3）**：给 `Doubled` 增加一个 `WeakEntity<Counter>` 回指字段；构造完成后，通过 `cx.observe`（参见 [_ownership_and_data_flow.rs:L59-L86](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs#L54-L86) 的官方示例）让 `Doubled` 在 `Counter` 每次通知时用弱句柄 `update` 自己为 `count * 2`；注意弱句柄的 `update` 返回 `Result`，用 `if let Err(e)` 打印错误而不是 unwrap。
4. **验证回收（模块 4.3）**：循环结束后 `drop` 掉两个外部强句柄，做一次任意 `update` 促发 `flush_effects`，为两个实体实现 `Drop` 打印遗言，确认它们都被回收。
5. **思考题**（不用写代码）：如果把第 1 步中 `Counter` 对 `Doubled` 的强持有、第 3 步中 `Doubled` 对 `Counter` 的弱回指**同时**反过来（弱持有 + 强回指），还能正常回收吗？

参考判据：第 5 步答案是否定的——只要环上存在一条**双向强引用**路径就会泄漏，方向无所谓；正确姿势是环上至少一端为弱。

本实践不需要窗口，可在 `run` 回调里完成全部逻辑后直接退出（`QuitMode` 默认行为见 u2-l1）；运行结果「待本地验证」。

## 6. 本讲小结

- **App 拥有一切实体**：状态以 `Box<dyn Any>` 存在 `App.entities`（`EntityMap`）里；`Entity<T>` 只是「`EntityId` + `TypeId` + 计数表弱指针」的惰性句柄，与 `Rc` 的本质区别是数据不在句柄侧、访问必须经上下文办租约。
- **创建与更新都是两阶段**：`reserve → insert` 让实体构造期就能引用自己的编号（`cx.reserve_entity`/`insert_entity`）；`lease → end_lease` 把状态搬出表再放回，从物理上杜绝可变别名——嵌套 `update` 同一实体直接 panic。
- **克隆便宜、比较看编号**：clone/drop 是对集中计数表的一次原子加减；`Hash/Eq/Ord` 全部基于 `EntityId`，句柄可放心当 `HashMap` 的键。
- **计数归零 ≠ 立刻析构**：归零只是进 `dropped_entity_ids` 队列，真正的回收（摘观察者、回调 release 监听、析构状态）发生在最外层更新收尾的 `flush_effects` 里；弱句柄的 `upgrade` 在计数归零那一刻就已失败。
- **强句柄成环即泄漏**：实体互持强句柄（或任务/订阅回环强引用自己）会让计数永不归零；子回指父、异步回写一律用 `WeakEntity<T>`，其 `update/read_with` 返回 `Result`，测试期还有 `LeakDetector` 与 `assert_released` 兜底。

## 7. 下一步学习建议

本讲解决的是「状态放在哪、怎么安全访问」。实体之间如何**联动**——`cx.notify()` 之后观察者如何被调度、`emit`/`subscribe` 的事件流怎么走、`cx.listener` 如何把元素事件绑到实体方法——正是下一讲 **u2-l3「Context 家族：App、Context\<T\> 与 AsyncApp」** 的主题，届时 `flush_effects` 里的 `Effect::Notify` / `Effect::Emit` 分支（[app.rs:L1631-L1661](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1624-L1683)）会被逐个拆开。

继续阅读源码的推荐顺序：

1. [src/app/context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs) —— `Context<T>` 如何包装 `App` 并携带实体身份；
2. [src/_ownership_and_data_flow.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/_ownership_and_data_flow.rs) 后半部分 —— `EventEmitter`/`subscribe` 的官方示例，为 u2-l3 预习；
3. [docs/contexts.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md) —— 五种上下文的能力边界总览。
