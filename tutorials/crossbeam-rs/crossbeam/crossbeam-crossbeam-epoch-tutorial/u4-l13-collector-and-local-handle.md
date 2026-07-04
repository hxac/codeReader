# Collector 与 LocalHandle：自建收集器

## 1. 本讲目标

前几讲我们一直用 `epoch::pin()` 拿到 `Guard`。这个 `pin()` 背后其实隐藏了一个「进程级单例收集器」和「线程局部句柄」。本讲我们要把这两层拆开，学会**自己创建收集器**。

学完本讲，你应当能够：

1. 说清 `Collector` 到底是什么（提示：它只是 `Arc<Global>` 的一层薄包装），以及它的 `Clone` / `PartialEq` 语义。
2. 说清 `LocalHandle` 是什么、它和 `Guard` 的关系（回想 u3-l9：`Guard` 内部就是一根指向 `Local` 的裸指针）。
3. 解释一个反直觉的现象：**为什么 `drop(collector)` 之后，已经 `register()` 出来的 `LocalHandle` 仍然可以正常 `pin()`**。
4. 独立写出「自建收集器 + 多线程各自 register」的最小例子。

## 2. 前置知识

本讲承接以下已建立的概念（不会再重复展开）：

- **EBR 与宽限期**（u1-l1）：对象从数据结构摘除后不能立刻释放，要等全局 epoch 前进 2 次才能安全回收。
- **`Guard` 是 pin 的凭证**（u3-l9）：`Guard` 的全部状态就是一根 `pub(crate) local: *const Local` 裸指针；`Guard::drop` 会解引用它调用 `unpin`。真正的 pin 状态（`guard_count`、`epoch` 等）住在 `Local` 里，不在 `Guard` 里。
- **指针三剑客**（u2 系列）：`Atomic<T>` / `Owned<T>` / `Shared<'g, T>`，以及 `defer_destroy`。

本讲要回答的核心问题是：u3-l9 里那根 `*const Local` 指针到底指向谁？谁创建了那个 `Local`？答案就是：**`Collector::register()` 创建了 `Local`，并返回一个 `LocalHandle` 给你**。`Guard.local` 和 `LocalHandle.local` 指向的是同一个 `Local`。

此外需要一点 Rust 基础：`Arc<T>` 的引用计数语义、`ManuallyDrop<T>` 的作用、以及 `unsafe impl Send/Sync` 的含义。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/collector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) | 定义公开类型 `Collector` 和 `LocalHandle`，是本讲的主战场。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 定义 `pub(crate)` 的 `Global`（全局数据）与 `Local`（参与者），`register` / `release_handle` / `finalize` 都在这里。 |
| [src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard` 类型，其 `local: *const Local` 字段是连接 u3-l9 与本讲的桥梁。 |

> 本讲刻意不展开 `Global` 内部的 epoch 推进与垃圾回收链路（留给 u4-l16 / u5 单元），只聚焦「Collector / LocalHandle / Local 三者的归属与引用计数」。

## 4. 核心概念与源码讲解

### 4.1 Collector：Arc\<Global\> 的轻量包装

#### 4.1.1 概念说明

`Collector` 是用户能拥有的「最外层」对象。你可以把它想象成「一个独立的 EBR 垃圾收集器实例」。默认情况下，整个进程共享一个由库惰性创建的全局 `Collector`（那是 u4-l14 的内容）；而当你想要隔离——比如让某个数据结构拥有自己私有的回收队列、随数据结构一起销毁——你就 `Collector::new()` 建一个。

从源码看，`Collector` 极其简单，它内部只有一个字段：

```rust
pub struct Collector {
    pub(crate) global: Arc<Global>,
}
```

也就是说，`Collector` 只是 `Arc<Global>` 的一层包装。`Global`（定义在 internal.rs）才是真正承载数据的结构：它持有「所有参与者的链表」「垃圾袋队列」「全局 epoch」三件套。`Collector` 本身不存这些，它只是握着一根指向 `Global` 的引用计数指针。

为什么用 `Arc`？因为一个收集器会被**多个线程共享**：每个线程都要能 `register()`、要能读全局 epoch、要能把垃圾推进全局队列。`Arc` 提供了跨线程共享 + 自动回收 `Global` 的能力——当最后一个引用消失，`Global` 才被 drop，届时队列里残留的延迟闭包也会被执行。

#### 4.1.2 核心流程

`Collector` 的生命周期可以用下面的伪代码描述：

```
Collector::new()
  └─ Arc::new(Global::new())          // 分配一个全新的 Global
        ├─ locals = List::new()        // 参与者链表（空）
        ├─ queue  = Queue::new()       // 垃圾袋队列（空）
        └─ epoch  = Epoch::starting()  // 全局 epoch 起点

collector.clone()        // 只复制 Arc，指向同一个 Global（引用计数 +1）
collector.register()     // 在 Global 的链表里插入一个新 Local，返回 LocalHandle
collector1 == collector2 // Arc::ptr_eq：判断是否指向同一个 Global
// Collector 自身 drop   // 只是 Arc 引用计数 -1；Global 是否销毁取决于还有没有别的引用
```

关键点：`new` / `clone` / `register` / `==` 全部围绕那根 `Arc<Global>` 打转。`Collector` 没有任何独占数据，所以它很「轻」。

#### 4.1.3 源码精读

先看结构定义与 trait 实现：

[Collector 结构与 Send/Sync：src/collector.rs:24-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L24-L29) —— `Collector` 只有一个 `global: Arc<Global>` 字段。注意它手动 `unsafe impl Send + Sync`：`Global` 内部用的是 `Atomic` / 无锁链表，本身可跨线程共享，但 `Arc<Global>` 是否 `Send+Sync` 取决于 `Global` 是否 `Send+Sync`，这里手动声明以对外暴露「可跨线程共享一个 `Collector`」的能力。

构造函数收敛到 `Default`：

[Collector::new / Default：src/collector.rs:31-44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L31-L44) —— `new()` 直接调 `default()`，而 `default()` 做的事就是 `Arc::new(Global::new())`。那行 `#[allow(clippy::arc_with_non_send_sync)]` 是为了压住 clippy 的误报（见注释里的 issue 链接），与功能无关。

注册句柄：

[Collector::register：src/collector.rs:47-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L47-L49) —— 一行转发到 `Local::register(self)`。真正干活的是 internal.rs（见 4.3.3）。

克隆与相等：

[Clone for Collector：src/collector.rs:52-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L52-L59) —— `clone()` 只克隆内部的 `Arc`，于是两个 `Collector` 共享同一个 `Global`，引用计数 +1。

[PartialEq / Eq for Collector：src/collector.rs:67-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L67-L73) —— `eq` 用 `Arc::ptr_eq` 比较两个 `Arc` 是否指向**同一块** `Global` 堆内存。这意味着：两个独立 `new()` 出来的 `Collector` 不相等（各自有独立 `Global`），但 `c.clone() == c` 为真。

最后看一眼官方文档里那段最精炼的示例，它浓缩了本讲所有要点：

[Collector 顶部文档示例：src/collector.rs:1-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L1-L14) —— `new()` → `register()` → `drop(collector)` → `handle.pin().flush()` 仍然工作。这段示例正是本讲「drop(collector) 后 handle 仍可用」结论的最权威佐证。

#### 4.1.4 代码实践

**实践目标**：验证 `Collector::clone()` 共享同一个 `Global`，而两次 `new()` 各自独立。

**操作步骤**：

1. 在 `crossbeam-epoch` 目录下，参考 `src/collector.rs` 里的 `#[cfg(test)] mod tests` 风格，写一个小测试或 `examples/` 程序（示例代码）：

```rust
// 示例代码：演示 Collector 的 clone 与相等语义
use crossbeam_epoch::Collector;

fn main() {
    let c1 = Collector::new();
    let c2 = c1.clone();      // 共享同一个 Global
    let c3 = Collector::new(); // 全新独立 Global

    assert!(c1 == c2);  // Arc::ptr_eq 为真
    assert!(c1 != c3);  // 不同 Global
    println!("c1 == c2: {}", c1 == c2);
    println!("c1 == c3: {}", c1 == c3);
}
```

2. 如果你把 crossbeam-epoch 作为路径依赖加入自己的实验 crate，运行上述程序。

**需要观察的现象**：`c1 == c2` 为 `true`，`c1 == c3` 为 `false`。

**预期结果**：输出 `c1 == c2: true` 与 `c1 == c3: false`。

> 待本地验证：若你尚未配置路径依赖，可改为在 `crossbeam-epoch` 仓库内临时加一个 `#[test]` 复现同样断言。

#### 4.1.5 小练习与答案

**练习 1**：`Collector` 的 `PartialEq` 为什么不能直接派生 `#[derive(PartialEq)]`？

**参考答案**：派生的 `PartialEq` 会逐字段比较，而 `Collector` 的字段是 `Arc<Global>`，无法比较 `Global` 内容（且 `Global` 没实现 `PartialEq`）。库真正想表达的语义是「是否指向同一个 `Global`」，所以必须用 `Arc::ptr_eq` 手动实现。

**练习 2**：`unsafe impl Send for Collector {}` / `unsafe impl Sync for Collector {}` 这两行如果删掉，会发生什么？

**参考答案**：`Collector` 含 `Arc<Global>`，其 `Send/Sync` 取决于 `Global`。若 `Global` 没有被推导为 `Send+Sync`，删掉后 `Collector` 就不是 `Send+Sync`，便无法跨线程传递（例如不能在多个线程里共用同一个 `Collector` 去 `register`）。这两行是「向调用者承诺可跨线程共享」的显式声明。

---

### 4.2 LocalHandle：线程的参与凭证

#### 4.2.1 概念说明

光有 `Collector` 还不能 `pin`。要 `pin`，当前线程必须先在 `Collector` 里「登记」为一个参与者（participant），这个登记动作就是 `register()`，它返回一个 `LocalHandle`。

你可以把 `LocalHandle` 理解为「**某线程在某 `Collector` 中的会员卡**」。有了这张卡，线程才能：

- `handle.pin()` —— 领一个 `Guard`，正式钉住（回想 u3-l9：`Guard` 的 `local` 字段就指回这张卡背后的 `Local`）。
- `handle.is_pinned()` —— 查自己当前是否被钉住。
- `handle.collector()` —— 反查这张卡属于哪个 `Collector`。

而 `LocalHandle` 的结构也极简——和 `Guard` 一样，就一根裸指针：

```rust
pub struct LocalHandle {
    pub(crate) local: *const Local,
}
```

它指向一个 `Local`（定义在 internal.rs）。`Local` 才是「参与者」的全部状态：本地垃圾袋 `bag`、`guard_count`、`handle_count`、`pin_count`、本地 `epoch`，以及一个指回所属 `Collector` 的引用。换句话说，`LocalHandle` 和 `Guard` 是**同一个 `Local` 的两种视角**：

- `LocalHandle` 是「持有这张卡的句柄视角」，关心 `handle_count`（这张卡本身的存在）。
- `Guard` 是「某次 pin 的凭证视角」，关心 `guard_count`（当前有几层 pin）。

这正是 u3-l9 里那根 `*const Local` 的来历——`Guard::local` 与 `LocalHandle::local` 同源。

#### 4.2.2 核心流程

`LocalHandle` 的使用流程：

```
let handle = collector.register();   // 创建 Local，handle_count = 1，返回 LocalHandle
                                       // LocalHandle.local = &Local

handle.pin()        // (*Local).pin() -> Guard{ local: 同一个 Local }
handle.is_pinned()  // (*Local).is_pinned() -> guard_count > 0
handle.collector()  // (*Local).collector() -> &Collector（存在 Local.collector 字段里）

// handle drop：
//   Local::release_handle(local)
//     handle_count -= 1
//     若 guard_count==0 且 handle_count 减到 0 -> finalize(Local) 真正销毁参与者
```

注意 `pin()` 返回的 `Guard` 借用的是 `&self`（`LocalHandle` 本身没被消费），所以同一个 `LocalHandle` 可以反复 `pin` 出多个 `Guard`——这正是 u3-l9 讲过的 pin 可重入性在 `LocalHandle` 层面的体现。

#### 4.2.3 源码精读

`LocalHandle` 的结构与方法：

[LocalHandle 结构：src/collector.rs:76-78](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L76-L78) —— 只有 `local: *const Local`。注意它**不是** `Send`/`Sync`：会员卡绑定了具体线程的本地状态（`Cell` 计数、`bag`），不能跨线程搬动。

三个方法全是「解引用裸指针后转发」：

[LocalHandle::pin / is_pinned / collector：src/collector.rs:80-98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L80-L98) ——
- `pin()`：`unsafe { (*self.local).pin() }`，调用 `Local::pin()`（见 4.3.3 的 `Local::pin`）。注意 `*self.local` 是 `unsafe` 的解引用。
- `is_pinned()`：转发到 `Local::is_pinned()`，即 `guard_count > 0`。
- `collector()`：转发到 `Local::collector()`，返回存在 `Local` 里的那个 `Collector` 引用（见 4.3.3）。

`Drop` 转发到 `release_handle`：

[LocalHandle::Drop：src/collector.rs:100-107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L100-L107) —— `LocalHandle` 离开作用域时，调用 `Local::release_handle(&*self.local)`，把 `handle_count` 减 1。

注意对比 `Guard::drop`（u3-l9）：`Guard::drop` 调的是 `unpin`（动 `guard_count`），而 `LocalHandle::drop` 调的是 `release_handle`（动 `handle_count`）。两者分别管理「pin 层数」和「会员卡张数」。

官方测试里有一段完美演示 `LocalHandle` 生命周期的代码：

[测试 pin_reentrant：src/collector.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L134-L151) —— `register()` 后立刻 `drop(collector)`，然后对 `handle` 反复 `pin()`、断言 `is_pinned()`。它同时验证了「drop(collector) 后 handle 仍可用」与「pin 可重入」两件事。

#### 4.2.4 代码实践

**实践目标**：体会 `LocalHandle` 与 `Guard` 同源、且 `pin()` 可重入。

**操作步骤**：

阅读并理解 `src/collector.rs` 中的 `pin_reentrant` 测试（上文已引用），然后在本地运行它：

```bash
cargo test --lib pin_reentrant
```

**需要观察的现象**：测试通过。`drop(collector)` 之后，`handle.pin()` 仍返回有效 `Guard`；嵌套两层 `pin()` 期间 `is_pinned()` 恒为 `true`，全部 drop 后变 `false`。

**预期结果**：`pin_reentrant` 测试输出 `ok`。

> 待本地验证：在 miri 下重跑（`cargo +nightly miri test --lib pin_reentrant`）应同样通过，说明不存在数据竞争。

#### 4.2.5 小练习与答案

**练习 1**：`LocalHandle::pin(&self)` 的签名是借用 `&self`，而不是消费 `self`。这意味着什么？

**参考答案**：`LocalHandle` 在 `pin` 后仍然存活，可以再次 `pin`。返回的 `Guard` 借用了 `LocalHandle` 背后的 `Local`，但 `LocalHandle` 自身不被消耗——这与 pin 可重入一致：同一张会员卡可以同时领多张临时凭证。

**练习 2**：为什么 `LocalHandle` 没有 `unsafe impl Send/Sync`？

**参考答案**：`LocalHandle` 背后的 `Local` 含 `Cell<usize>`（非原子的、线程本地的 `guard_count` / `handle_count`）和线程本地的 `bag`，这些状态是**绑定到创建它的那个线程**的。把 `LocalHandle` 送到别的线程会让非原子计数失去同步保证，因此默认 `!Send + !Sync`，禁止跨线程移动。

---

### 4.3 引用计数与 Drop：为什么 drop(collector) 后 handle 仍可用

#### 4.3.1 概念说明

这是本讲最反直觉、也最关键的一点：文档示例里 `drop(collector)` 之后还能 `handle.pin()`。为什么？

答案藏在 **`register` 时发生的「克隆 Collector」**。当一个线程调用 `collector.register()` 时，新创建的 `Local` 并不是只保存一个裸指针指向外部的 `Collector`，而是**把 `Collector` 克隆了一份（克隆 `Arc`，引用计数 +1）存进自己的 `collector` 字段**：

```rust
collector: UnsafeCell<ManuallyDrop<Collector>>,
```

也就是说，`Local` 自己也握着一根指向 `Global` 的 `Arc` 引用。于是引用关系是：

```
Collector (用户持有)  ──Arc──┐
                             ├──> Global  （引用计数 = 2+）
Local.collector       ──Arc──┘
```

当用户 `drop(collector)`：只是把「用户那一根 Arc」释放，引用计数 -1，但 `Local.collector` 那一根还在，`Global` 不会被销毁。所以 `handle.collector()` 仍能拿到一个有效的 `Collector` 引用，`pin()` 仍能读全局 epoch、仍能把垃圾推进队列。

真正销毁 `Global` 的时机是：**所有 `LocalHandle` 都 drop、所有 `Local` 都 `finalize` 之后**，最后一根 `Arc` 消失，`Global` 才被 drop，届时队列里残留的延迟闭包会被全部执行。

#### 4.3.2 核心流程

完整的引用计数与销毁流程：

```
register(collector):
  Local {
    handle_count = 1,                     // 这张会员卡存在
    collector    = collector.clone(),      // Local 自己克隆一份 Arc<Global>（关键！）
    ...
  }
  插入 Global.locals 链表
  返回 LocalHandle{ local: &Local }

// 用户 drop(collector)：用户那根 Arc 释放，但 Local.collector 的 Arc 还在 -> Global 存活

LocalHandle::drop -> release_handle:
  handle_count -= 1
  if guard_count == 0 && handle_count 减到 0:
      finalize(Local):
        临时 pin + push_bag  （把本地残余垃圾推进全局队列）
        entry.delete()       （从 Global.locals 链表逻辑删除）
        drop(Local.collector)（释放 Local 持有的 Arc）
                            （若这是最后一根 Arc -> Global 被 drop -> 队列残余闭包执行）
        // Local 自身的裸内存由链表的延迟回收负责释放（见 u6-l20）
```

注意两个计数的分工：

- `guard_count`：当前有多少个 `Guard` 活着（pin 层数）。由 `pin`/`unpin` 维护。
- `handle_count`：当前有多少个 `LocalHandle`（会员卡张数）。由 `register`/`acquire_handle`/`release_handle` 维护。

只有当 `handle_count == 0`（没有会员卡了）**且** `guard_count == 0`（没有正在进行的 pin）时，这个 `Local` 才可以被 `finalize` 销毁。

#### 4.3.3 源码精读

先看 `Local` 的字段，理解它如何「持有 Collector」：

[Local 结构与字段：src/internal.rs:292-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L292-L318) —— 注意三处：
- `entry: Entry` —— 它是无锁侵入式链表（u6-l20）的节点，且 `#[repr(C)]` 保证 `entry` 是首个字段，使得 `Entry*` 与 `Local*` 可互相 `cast`。
- `collector: UnsafeCell<ManuallyDrop<Collector>>` —— 这是本节的灵魂：`Local` **自己持有一个 `Collector`**（即一根 `Arc<Global>`）。`ManuallyDrop` 是因为 `Local` 的析构要走自定义的 `finalize`，不能让 Rust 自动 drop 它。
- `guard_count` / `handle_count` / `pin_count` —— 用 `Cell`（非原子），印证了 `Local` 是线程本地的、不可跨线程移动。

`register` 的实现：

[Local::register：src/internal.rs:338-357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L338-L357) ——
- 注释「it is safe to use `unprotected`」呼应 u3-l9：这里还没 `pin`，用 `unprotected()` 假守卫来分配和插入链表。
- `handle_count: Cell::new(1)` —— 注册即产生一张会员卡。
- `collector: UnsafeCell::new(ManuallyDrop::new(collector.clone()))` —— **关键一行**：克隆传入的 `Collector`（`Arc` +1），存进 `Local`。这就是「drop 原始 collector 后 handle 仍可用」的直接原因。
- `collector.global.locals.insert(local, ...)` —— 把新建的 `Local` 插入全局链表。
- 返回 `LocalHandle { local: local.as_raw() }`。

`Local::collector`：

[Local::collector / global / is_pinned：src/internal.rs:360-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L360-L375) —— `collector()` 返回存在 `Local.collector` 字段里的那个 `Collector` 的引用（注意是 `Local` 自己克隆的那份，不是用户原始的那份——尽管它们指向同一个 `Global`）。`is_pinned()` 就是 `guard_count > 0`。

`acquire_handle` / `release_handle`：

[acquire_handle / release_handle：src/internal.rs:504-527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L504-L527) —— `release_handle` 把 `handle_count` 减 1；若 `guard_count == 0 && handle_count == 1`（即减完就是 0 且当前没 pin），调用 `finalize(this)` 真正销毁。

`finalize` 的销毁动作：

[Local::finalize：src/internal.rs:530-569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L530-L569) —— 关键步骤：
1. 先把 `handle_count` 临时设回 1，防止接下来的 `pin()` 又触发 `finalize`（递归）。
2. `pin()` 一次，把本地 `bag` 里残余的垃圾推进全局队列（`push_bag`）。
3. 把 `handle_count` 设回 0。
4. `ptr::read` 把 `Local.collector` 字段里的 `Collector` **取出来**（这是「在失去保护前先读到 `Global` 引用」的关键，注释强调了顺序）。
5. `entry.delete(unprotected())` 在链表里标记逻辑删除。
6. `drop(collector)` —— 释放 `Local` 持有的那根 `Arc`；若这是最后一根，`Global` 被 drop，队列残余闭包被执行。

注释 [src/internal.rs:555-558](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L555-L558) 特别提醒：必须在 `entry.delete()` **之前**用 `ptr::read` 取出 `collector` 引用，否则一旦标记删除，别的线程可能已经把 `Global` 整个销毁了，再读就悬空。

> 说明：`finalize` 有两个版本——`Local::finalize(this: *const Self)`（internal.rs 内部参与者退出时调用）与 `IsElement::finalize(entry, guard)`（链表遍历回收节点时调用，[src/internal.rs:583-585](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L583-L585)）。后者把 `Local` 的裸内存交给 guard 延迟回收（详见 u6-l20 的侵入式链表）。本讲只关注前者（参与者退出销毁）。

#### 4.3.4 代码实践

**实践目标**：亲手验证「drop(collector) 后 handle 仍可用」与「多线程各自 register 指向同一 Collector」。

**操作步骤**：

在 `crossbeam-epoch` 仓库内新增一个测试（示例代码，可放在自己的实验 crate 里）。它综合了官方文档示例与 `pin_reentrant` 测试的思路：

```rust
// 示例代码：自建 Collector，两线程各自 register，drop(collector) 后仍可用
use crossbeam_epoch::Collector;
use crossbeam_utils::thread;
use std::sync::atomic::{AtomicUsize, Ordering};

#[test]
fn two_threads_share_one_collector() {
    let collector = Collector::new();
    let collector_clone = collector.clone(); // 仅用于线程间传递（Collector: Send+Sync，可省略 clone 直接用 &）
    let pins = AtomicUsize::new(0);

    thread::scope(|s| {
        for _ in 0..2 {
            s.spawn(|_| {
                let handle = collector_clone.register(); // 每个线程各自注册一张会员卡
                let guard = &handle.pin();               // pin 出 Guard
                pins.fetch_add(1, Ordering::Relaxed);
                assert!(handle.is_pinned());

                // 这张卡所属的 Collector，与原 collector 指向同一个 Global
                assert!(handle.collector() == &collector_clone);
            });
        }
    }).unwrap();

    assert_eq!(pins.load(Ordering::Relaxed), 2);

    // 关键验证：drop 原 collector 后，新 handle 仍可正常使用
    let handle = collector.register();
    drop(collector);              // 用户那根 Arc 释放
    let g = &handle.pin();        // 仍然能 pin
    assert!(handle.is_pinned());
    handle.collector();           // 仍然能拿到 Collector
    g.flush();
    drop(handle);                 // 释放最后一张会员卡
}
```

**需要观察的现象**：
1. 两个子线程里的 `handle.collector() == &collector_clone` 都成立——它们指向同一个 `Global`。
2. `drop(collector)` 之后，`handle.pin()` 不 panic，`is_pinned()` 返回 `true`。

**预期结果**：测试通过。

> 待本地验证：`cargo test --lib two_threads_share_one_collector`（需要把代码放入 `#[cfg(test)] mod tests` 并临时引入）。若你的实验环境无法编译跨 crate 示例，可只保留单线程的 `drop(collector)` 验证部分，对照官方 `pin_reentrant` 测试阅读。

#### 4.3.5 小练习与答案

**练习 1**：假设把 `Local::register` 里的 `ManuallyDrop::new(collector.clone())` 改成「只存一个 `&Collector` 引用」（不克隆 `Arc`），会出什么问题？

**参考答案**：`Local` 的生命周期可能超过用户传入的 `Collector` 借用，借用检查会直接报错；即便绕过借用检查，一旦用户 `drop(collector)` 且那是最后一根 `Arc`，`Global` 会被销毁，`Local` 再去访问 `global.epoch`、`global.queue` 就是 use-after-free。克隆 `Arc` 正是为了让 `Local` 独立持有 `Global` 的所有权，保证参与者存活期间 `Global` 不会被释放。

**练习 2**：`finalize` 里为什么要「先 `ptr::read` 取出 `collector`，再 `entry.delete()`」？顺序反过来会怎样？

**参考答案**：`entry.delete()` 会把这个 `Local` 标记为逻辑删除；若此时还有别的线程正在退出参与者并触发 `Global` 的最后回收，`Global` 可能在我们读 `Local.collector` 之前就被销毁。先用 `ptr::read` 把 `Collector`（`Arc`）「搬」到局部变量，等于先递增了一道安全保证（掌握一根 `Arc`），再标记删除，最后才 `drop` 这根 `Arc`，从而避免悬垂访问。源码注释 [src/internal.rs:555-558](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L555-L558) 明确强调了这一点。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「自建收集器 + Drop 计数验证」的小任务。

**任务**：实现一个最小 demo，证明「自建 `Collector` 在所有 `LocalHandle` 释放后，其 `Global` 才被销毁，且销毁时会执行队列里残留的延迟闭包」。

**步骤**：

1. 用一个带 `Drop` 副作用的类型（参考 `src/collector.rs` 里 `count_drops` 测试的 `Elem`，[src/collector.rs:284-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L284-L315)）：

```rust
// 示例代码
use crossbeam_epoch::{Collector, Owned};
use std::sync::atomic::{AtomicUsize, Ordering};

static DROPS: AtomicUsize = AtomicUsize::new(0);
struct Elem(i32);
impl Drop for Elem {
    fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); }
}

fn main() {
    let collector = Collector::new();      // 模块 1：自建收集器
    {
        let handle = collector.register(); // 模块 2：领会员卡
        let guard = &handle.pin();
        unsafe {
            // 制造一些垃圾，但先不 flush，留在本地 bag
            for _ in 0..3 {
                let a = Owned::new(Elem(1)).into_shared(guard);
                guard.defer_destroy(a);
            }
            guard.flush();                 // 推进全局队列
        }
        // 验证 drop(collector) 后 handle 仍可用（模块 3）
        drop(collector);
        assert!(handle.is_pinned() || { let _g = handle.pin(); handle.is_pinned() });
    } // handle 在此 drop -> release_handle -> finalize -> 推 bag 入队 -> 最后一根 Arc 释放 -> Global drop
      // -> 队列里残留闭包（含 Elem 的析构）被执行

    // 此时 epoch 未必已推进到能回收所有垃圾，需再 pin/collect 推进
    // 但 Global 已被销毁，我们无法再用原 collector 推进——这正是本练习的观察点：
    //   handle 是最后一个引用，drop(handle) 触发 finalize 时，残余 bag 会被 push 进队列，
    //   随 Global 一起 drop 而执行。
    println!("DROPS after handle drop: {}", DROPS.load(Ordering::Relaxed));
}
```

2. **需要观察并思考的现象**：
   - `drop(collector)` 是否让程序 panic？（不应 panic。）
   - `handle` drop 后，`DROPS` 是否非零？为什么在 `Global` 销毁时队列里的闭包会被执行？（提示：`Global::Drop` 会把队列里所有 `SealedBag` drop 掉，而 `Bag::drop` 会执行其中的延迟闭包。）
   - 体会「自建 `Collector` 让一个数据结构拥有私有回收队列、随数据结构销毁而清空」这一设计价值（internal.rs 顶部注释 [src/internal.rs:35-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L35-L36) 提到了这一点）。

3. **预期结果**：程序不 panic；`DROPS` 在 `handle` drop 后有所反映（具体数值取决于宽限期是否已到，可能需要额外 collect 才能全部回收；若你想看到 `==3`，可仿照 `count_drops` 测试在 drop handle 之前多循环 `pin`+`collect`）。

> 待本地验证：垃圾是否「在 `handle` drop 那一刻立即全部回收」取决于 epoch 是否已推进满两次，建议先阅读 `count_drops` 测试（[src/collector.rs:284-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L284-L315)）对照，它用 `while DROPS.load(..) < COUNT { pin(); collect(); }` 主动推进。

## 6. 本讲小结

- `Collector` 只是 `Arc<Global>` 的一层薄包装；`new()` 创建独立 `Global`，`clone()` 共享同一 `Global`（`Arc` +1），`==` 用 `Arc::ptr_eq` 判断同一性。
- `LocalHandle` 是「某线程在某 `Collector` 中的会员卡」，内部就是一根 `*const Local`——和 `Guard.local` 同源；`pin()` / `is_pinned()` / `collector()` 都是解引用后转发到 `Local` 的方法。
- `register()` 在 `Global.locals` 链表里插入一个新 `Local`，并把传入的 `Collector` **克隆（`Arc` +1）** 存进 `Local.collector` 字段——这正是「`drop(collector)` 后 `handle` 仍可用」的根本原因。
- 两个计数分工：`guard_count`（pin 层数，由 `pin`/`unpin` 维护）与 `handle_count`（会员卡张数，由 `register`/`acquire_handle`/`release_handle` 维护）；两者都归 0 时 `Local` 才会被 `finalize` 销毁。
- `finalize` 把本地残余 `bag` 推入全局队列、从链表标记删除、最后 `drop` 掉 `Local` 持有的 `Arc`；若这是最后一根 `Arc`，`Global` 被 drop，队列残余闭包被执行。
- `Collector` 是 `Send+Sync`（可跨线程共享），而 `LocalHandle` / `Guard` 都 `!Send+!Sync`（绑定到具体线程的本地状态）。

## 7. 下一步学习建议

本讲打通了「用户视角的 `Collector` / `LocalHandle`」与「内部 `Local`」的归属与引用计数。接下来建议：

1. **u4-l14 默认收集器与线程局部 HANDLE**：回到开篇遗留的问题——`epoch::pin()` 背后的那个全局单例 `Collector` 是如何惰性初始化的？线程局部 `HANDLE` 在何时注册、线程退出时如何安全处理？这是本讲的「默认实现」对照版。
2. **u4-l15 Local：参与者结构与注册/计数**：更细致地拆解 `Local` 的 `#[repr(C)]` 侵入式布局、`bag`、`pin_count` 与 `IsElement` trait，把本讲略过的字段逐一讲清。
3. **u4-l16 Global 全局数据与 Bag 队列**：进入 `Global` 内部，理解 `locals` / `queue` / `epoch` 三件套与 `SealedBag::is_expired` 的宽限期判据——这是理解「`finalize` 推入的 bag 何时被回收」的关键。
4. 若想立刻看到多线程压力下的回收效果，可先跳读 [src/collector.rs 的 stress 测试：src/collector.rs:416-454](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L416-L454)，它用 8 线程并发 `register`+`pin`+`defer_destroy`，再由主线程 collect 推进回收。
