# maybe_arc 与流拆分优化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `MaybeArc<T>` 这个枚举为什么能让一个「将来可能被多个所有者共享」的值，在「目前只有一个所有者」时**完全不产生堆分配**。
- 读懂 `OptArc` / `OptArcIRC` 两个 trait 如何用类型擦除，把「内联值」和「共享 `Arc`」当成同一种东西来操纵。
- 追踪一次 `PipeStream::split` 如何把内联值**升级**为堆上的 `Arc`，以及一次 `reunite` 如何在条件满足时把它**降级**回内联值。
- 解释 `ptr_eq` 为什么能只凭内存地址就判定收发两半是否来自同一条流。
- 在脑海中画出一条「从未 split 的流」与「split 后的流」在内存布局上的差别。

本讲是 u4-l3 的延续：u4-l3 讲了 `PipeStream` 是什么、消息模式与方向如何用常量泛型编码，并把 `MaybeArc`、`linger_pool` 当作黑盒留到了后续。本讲撬开 `MaybeArc` 这个黑盒；`linger_pool` 已在 u8-l1 讲过。

---

## 2. 前置知识

阅读本讲前，最好先具备以下概念（前序讲义已建立）：

- **`Arc<T>` 的代价**。`Arc`（原子引用计数指针）把 `T` 连同两个原子计数器（strong / weak）一起放在**堆**上，让多个所有者共享同一份数据。它解决「共享所有权」问题，但代价是：创建时要堆分配，读写时要原子地增减计数。如果某个值**永远只有一个所有者**，用 `Arc` 就是纯粹的浪费。
- **u4-l3 的常量泛型 `PipeModeTag`**。`PipeStream<Rm, Sm>` 用 `Rm`/`Sm` 两个标记类型（`pipe_mode::{None, Bytes, Messages}`）在编译期决定收发方向与能力。
- **u7-l2 的句柄所有权**。Windows 句柄在本库里用 `AdvOwnedHandle`（基于 `NonZeroUsize`、零 niche）表示，它本身是**指针大小、不占堆**的。这一点很重要：它意味着「内联存放」一条流几乎不付出额外代价。
- **u8-l1 的 limbo / `NeedsFlush`**。`PipeStream` drop 时若仍有未刷新缓冲，会把句柄送进 `linger_pool` 后台刷新。本讲在讲 `split`/`reunite` 时会顺带说明：升级成 `Shared` 后，这条流就具备了「被多个所有者共享」的形态。
- **`unsafe` 与 no-diverge zone（无分叉区）**。本讲的 `refclone`/`try_make_owned` 含有 `ptr::read`/`ptr::write` 这类位运算搬移，需要保证中间不会 panic，否则会双释放。这与 u8-l1 里 `QueueEnt` 的低比特指针属于同一种「精细 unsafe 纪律」。

一个贯穿全讲的直觉：

> **把堆分配推迟到最后真正需要它的那一刻。** 只要一条流从未被 split，它就是单所有者，没必要进堆；一旦 split，必然出现两个所有者，这时才把它搬上堆（`Arc`）；若日后两半又重新合并回一个所有者，就再把它从堆里搬回内联。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/os/windows/maybe_arc.rs` | 定义 `MaybeArc<T>` 枚举、`OptArc` / `OptArcIRC` 两个 trait，以及它们在 `Arc<T>` 与 `MaybeArc<T>` 上的实现。本讲的核心。 |
| `src/os/windows/named_pipe/stream.rs` | **同步** named pipe 流的结构定义：`PipeStream { raw: MaybeArc<RawPipeStream>, .. }`。证明同步流一律用 `MaybeArc`。 |
| `src/os/windows/named_pipe/stream/enums.rs` | `PipeModeTag` trait，及其关联类型 `type OptArc<T>`。说明 **Tokio** 流按 send 模式在 `MaybeArc` 与 `Arc` 之间选择。 |
| `src/os/windows/named_pipe/stream/impl.rs` | 同步 `PipeStream::split` / `reunite`。 |
| `src/os/windows/named_pipe/tokio/stream.rs` | Tokio 流结构定义：`raw: Sm::OptArc<RawPipeStream>`。 |
| `src/os/windows/named_pipe/tokio/stream/impl.rs` | Tokio `PipeStream::split` / `reunite`。 |
| `src/os/windows/named_pipe/stream/impl/handle.rs` | `TryFrom<PipeStream> for OwnedHandle`：只有内联（未 split）的流才能解包成裸句柄——本讲优化的直接受益点。 |
| `src/os/windows/named_pipe/tokio/stream/impl/send.rs` | Tokio 发送路径用 `Sm::cast_oai` 拿到 `&self` 引用做原子刷新，这正是「能 send 的 Tokio 流必须用 `Arc`」的原因。 |

---

## 4. 核心概念与源码讲解

### 4.1 MaybeArc：把 Arc 的堆分配「推迟到最后需要的时刻」

#### 4.1.1 概念说明

`MaybeArc<T>` 解决的是一个看似矛盾的需求：

- 一条 `PipeStream` **将来可能**被 `split` 成收发两半，两半要共享同一个底层连接，因此它的内部状态必须「能够」变成多所有者共享形态。
- 但在**绝大多数实际使用中**，一条流从创建到销毁只有一个所有者（服务端 accept 出来后直接用、用完直接 drop），根本不会 split。如果一开始就用 `Arc<T>`，每条流都白白多一次堆分配，对高并发服务端来说是可观的浪费。

`MaybeArc` 的做法是把「单所有者」和「多所有者」这两种形态都装进**同一个枚举**：

```rust
pub enum MaybeArc<T> {
    Inline(T),       // 单所有者：T 直接内联，零堆分配
    Shared(Arc<T>),  // 多所有者：搬上堆，Arc 共享
}
```

- 刚创建的流是 `Inline`：`T` 就地存放在枚举载荷里，**没有任何堆分配**。
- 一旦调用 `split`，`Inline` 被升级成 `Shared`：这时才发生一次 `Arc::new` 的堆分配，把 `T` 搬到堆上。
- 若日后两半 `reunite` 回单一所有者，`Shared` 又会被**尝试**降级回 `Inline`，把堆分配还回去。

这就是「**惰性堆分配**」（lazy boxing）：永远只在真正出现第二个所有者时才付堆分配的代价。

#### 4.1.2 核心流程

一条流的生命周期里，`MaybeArc` 的状态机大致如下：

```
       new() / connect / accept
                │
                ▼
         ┌─────────────┐
         │   Inline    │  ◄── 单所有者，零堆分配
         └─────────────┘
           │         ▲
     split │         │ reunite（且此时是唯一所有者）
   refclone│         │ try_make_owned 成功
           ▼         │
         ┌─────────────┐
         │   Shared    │  ◄── 多所有者，已堆分配
         └─────────────┘
                 │
        reunite（仍有其它所有者）
        try_make_owned 失败 → 保持 Shared
```

关键不变式：**`Shared` 一定意味着堆上有一个 `Arc`；`Inline` 一定意味着没有堆分配。** 从 `Inline` 到 `Shared` 是单向「升级」（必然伴随 `Arc::new`），从 `Shared` 到 `Inline` 是「可能成功的降级」（取决于 `Arc` 此刻是否唯一）。

用一句话概括两个方向的代价：

- 升级 `refclone`：必然分配，因为要把内联值搬上堆。
- 降级 `try_make_owned`：当且仅当 strong 计数为 1 时才能成功（把值从堆里搬回内联并释放堆块）；否则原样保留 `Shared`。

计数关系可用一个简单式子表达。设升级后两个所有者共享同一 `Arc`，其 strong 计数为：

\[
\text{strong}(\text{Arc}) = \#\text{所有指向它的 MaybeArc::Shared 变量}
\]

`try_make_owned` 成功当且仅当 \(\text{strong} = 1\)。

#### 4.1.3 源码精读

先看枚举本身和两个构造入口。`From<T>` 把一个值直接包成 `Inline`，`From<Arc<T>>` 把现成的 `Arc` 包成 `Shared`：

[文件路径:L42-L55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L42-L55) 定义了 `MaybeArc` 枚举及 `From<T>` / `From<Arc<T>>` 两个转换。注意 `Inline` 和 `Shared` 是同一个枚举的两个变体——这正是「单所有者/多所有者」两种形态能在运行期互相切换的基础。

再看 `get()`：无论内联还是共享，都返回 `&T`，对外屏蔽掉两种存储形态的差别：

[文件路径:L56-L71](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L56-L71) 是 `MaybeArc` 对 `OptArc` 的实现中，`get()` 与 `get_arc()` 两个访问器。`get()` 在 `Inline` 时返回内联值的引用、在 `Shared` 时返回 `Arc` 解引用后的引用；`get_arc()` 只在 `Shared` 时返回 `Some(&Arc)`，`Inline` 时返回 `None`。

接下来是本讲最精细的两段 `unsafe` 代码。先看**升级**——`refclone(&mut self) -> Self`，它把一个 `Inline`（或已有的 `Shared`）变成 `Shared`，并额外返回一份新的 `Shared`：

[文件路径:L72-L92](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L72-L92) 是 `refclone`。对 `Shared` 分支只是 `Arc::clone`；对 `Inline` 分支才是重点：它用 `ptr::read` 把内联值按位「搬」出来塞进一个新建的 `Arc`（这一步发生堆分配），再就地用 `ptr::write` 把 `self` 整体改写成 `Shared`，最后 `Arc::clone` 出第二份作为返回值。最终效果是：原本唯一的 `Inline` 所有者，变成 `self`（`Shared`）与返回值（`Shared`）两个所有者，共享同一份堆上的 `Arc`。

这段代码里有两段注释值得注意：

- `// Begin no-diverge zone` 到 `// End no-diverge zone`：在 `ptr::read`（让内联槽位进入「逻辑上已搬走」状态）与 `ptr::write`（重新填回 `Shared`）之间，`self` 处于一种「不能被正常 Drop」的中间态。一旦这中间发生 panic/展开，就会双释放。因此这段窗口里只放了 `Arc::into_raw` / `Arc::from_raw` / `ptr::write` 这些**绝不展开**的操作。这是和 u8-l1 一脉相承的「精细 unsafe 纪律」。
- `ManuallyDrop::new(ptr::read(mx))`：`ptr::read` 拿走了内联值的所有权，但用 `ManuallyDrop` 包住，保证临时变量析构时不会真的去释放底层资源，避免与 `self` 的所有权冲突。

再看**降级**——`try_make_owned(&mut self) -> bool`：

[文件路径:L93-L109](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L93-L109) 是 `try_make_owned`。若已是 `Inline` 直接返回 `true`（无需降级）；若是 `Shared`，则用 `Arc::try_unwrap` 尝试把值从堆里取出来——成功说明此刻 strong 计数为 1（本流是唯一所有者），于是 `ptr::write` 把 `self` 改写成 `Inline`，堆块释放，返回 `true`；失败说明还有别的所有者，返回 `false` 并用 `ManuallyDrop` 安全地「忘掉」临时副本，`self` 维持 `Shared`。这里同样有一个 no-diverge zone 守护 `ptr::read`/`ptr::write` 的中间态。

> 关于 `try_make_owned` 里 `ptr::read(a)` 与引用计数的精确推导较为繁琐（涉及「位拷贝出一个 Arc 句柄但不增计数、再用 `try_unwrap`/`ManuallyDrop` 精确抵消」的配对），本讲不展开逐计数器证明。理解到「它在 strong 计数为 1 时把值搬回内联、否则不动」这一层，已足以掌握本模块的设计意图。

最后是**同源判定**——`ptr_eq`。它有一个默认实现，定义在 `OptArc` trait 上：

[文件路径:L14-L20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L14-L20) 是 `ptr_eq` 的默认实现。逻辑是分三种情况：两边都是 `Shared`（`get_arc()` 返回 `Some`）则比 `Arc::ptr_eq`（即两个 `Arc` 是否指向同一堆块）；两边都是 `Inline` 则用 `std::ptr::eq` 直接比两者的内存地址；一边 `Inline` 一边 `Shared` 则直接判为不同源（返回 `false`）。`reunite` 正是靠它判断收发两半是不是同一条流分裂出来的。

> `ptr_eq` 用到的 `ref2ptr` 只是一个把引用转成裸指针的 `const fn`（定义在 [misc.rs:L168](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L168)），为 `Inline-Inline` 分支取地址服务。

#### 4.1.4 代码实践（源码阅读型）

本实践是规格里指定的核心任务：结合 `PipeStream::split`/`reunite` 源码，说明「从未 split 的流为何不产生堆分配，而 split 后内存布局如何变化」。

**实践目标**：用文字 + ASCII 图把内存布局讲清楚。

**操作步骤**：

1. 阅读 [stream.rs:L57-L60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L57-L60)（同步 `PipeStream` 结构）与构造器 [ctor.rs:L107-L109](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L107-L109)（`Self::new` 里 `raw: raw.into()`）。注意 `raw.into()` 把 `RawPipeStream` 经 `From<T>` 包成 `MaybeArc::Inline`——所以新流一开始就是 `Inline`。
2. 阅读同步 split [impl.rs:L35-L41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L35-L41)：`self.raw.refclone()` 触发 4.1.3 里的升级。
3. 阅读同步 reunite [impl.rs:L45-L53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L45-L53)：`MaybeArc::ptr_eq` 判同源后 `raw.try_make_owned()` 尝试降级。
4. 阅读直接受益点 [handle.rs:L50-L59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L50-L59)：`TryFrom<PipeStream> for OwnedHandle` 只有在 `Inline` 时返回 `Ok`，`Shared` 时原样退还 `Err(s)`。

**预期结果（待本地验证内存大小，但布局推理是确定的）**：

- **从未 split 的同步流**：`PipeStream { raw: MaybeArc::Inline(RawPipeStream), _phantom }`。`MaybeArc` 是个带判别式的枚举，`Inline` 载荷把 `RawPipeStream` 直接内联；而 `RawPipeStream` 内部的句柄是 `AdvOwnedHandle`（u7-l2 已说明它是 `NonZeroUsize`、零堆）。所以整条流**完全在栈/所属结构里，零堆分配**。示意：

  ```
  PipeStream (栈上)
  ├─ MaybeArc::Inline
  │    └─ RawPipeStream { handle: AdvOwnedHandle(usize), is_server, needs_flush }
  └─ _phantom
  （无堆块）
  ```

- **split 之后**：`refclone` 在 `Inline` 分支里调 `Arc::new`，发生**一次堆分配**，两半都变成 `MaybeArc::Shared`，指向同一块堆内存：

  ```
  recv 半 (栈)                 send 半 (栈)
  ├─ MaybeArc::Shared ──┐      ├─ MaybeArc::Shared ──┐
  └─ ...                │      └─ ...                │
                        ▼                           ▼
              ┌─────────────────────────┐  (同一块堆)
              │ Arc 内部:               │
              │   strong 计数 (原子)    │
              │   weak   计数 (原子)    │
              │   RawPipeStream { ... } │
              └─────────────────────────┘
  ```

- **reunite 之后（且无其它所有者）**：`try_make_owned` 因 strong=1 成功，把 `RawPipeStream` 搬回内联，堆块释放，回到第一张图的形态。

**需要观察的现象**：如果你想用代码验证「未 split 时更省内存」，可在 Windows 上对「未 split 的 `PipeStream`」与「split 后任一半的 `PipeStream`」分别打印 `std::mem::size_of`。但由于枚举两个变体都要能容纳最大者，`size_of` 本身不会变小——真正的差别在于**是否发生堆分配**，需要用分配器计数（如 `dhat` 或自定义 `GlobalAlloc`）来观测，而非 `size_of`。**结论性堆分配行为待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MaybeArc` 用一个「枚举」而不是「结构体 + `Option<Arc<T>>`」来实现同样的「可能共享」语义？

> **参考答案**：枚举让 `Inline(T)` 与 `Shared(Arc<T>)` 成为同一类型的两个互斥状态，可以原地切换变体（这正是 `refclone`/`try_make_owned` 用 `ptr::write` 改写 `self` 的前提）。若用「结构体 + `Option<Arc<T>>`」，则 `T` 与 `Option<Arc<T>>` 要**同时**占用空间（要么 `T` 总在、再加一个可选 `Arc`，要么用 `union` 自己管布局），既浪费又丢失了「同一槽位、两种解释」的简洁性。枚举是表达「同一份数据、两种所有权形态」最自然的方式。

**练习 2**：`refclone` 在 `Inline` 分支里，为什么必须用 `ManuallyDrop::new(ptr::read(mx))` 而不是直接 `Arc::new(ptr::read(mx))`？

> **参考答案**：`ptr::read(mx)` 把内联值按位「搬走」，此时 `self` 的 `Inline` 槽位在逻辑上已空，但**物理上仍持有同一份 `RawPipeStream`**。如果不包 `ManuallyDrop`，当临时 `Arc` 或后续操作因任何原因展开（panic）时，临时变量和 `self` 都可能尝试析构同一份资源，导致双释放。`ManuallyDrop` 让临时拷贝「不主动析构」，把析构责任明确交给随后 `ptr::write` 改写后的新 `self`，从而守住「资源恰好被释放一次」的不变式。

**练习 3**：`try_make_owned` 在 strong 计数为 2 时被调用，会发生什么？返回什么？`self` 变成什么？

> **参考答案**：`Arc::try_unwrap` 会失败（因为不止一个 strong 所有者），返回 `Err`。函数用 `ManuallyDrop` 安全忘掉那个临时副本，`self` **维持 `Shared` 不变**，函数返回 `false`。也就是说「降级失败、布局不变」。

---

### 4.2 OptArc / OptArcIRC：用类型擦除统一「内联值」与「共享 Arc」

#### 4.2.1 概念说明

`MaybeArc` 自身只是一个具体类型。但本库希望在**同一份代码**里，既能用「总是 `Arc`」的流（Tokio 下能 send 的流），又能用「能内联优化」的流（同步流、或 Tokio 下只收的流），而调用 `split`/`reunite`/`get` 的代码却保持一致。这就需要一个**抽象 trait**——`OptArc`。

`OptArc` 把「持有某个 `T`、并能按需共享」这件事抽象成一组方法：

- `get(&self) -> &Self::Value`：拿到内部值的引用（无论内联还是共享）。
- `get_arc(&self) -> Option<&Arc<Self::Value>>`：如果是共享形态，拿到 `Arc` 的引用；否则 `None`。
- `refclone(&mut self) -> Self`：**升级**并产出第二份（`&mut self` 版本）。
- `try_make_owned(&mut self) -> bool`：**降级**尝试。
- `ptr_eq(&self, other) -> bool`：默认实现，判同源。

关键在于：`Arc<T>` 和 `MaybeArc<T>` **都实现了 `OptArc`**。于是泛型代码（如 `PipeStream::split`）只要约束 `Sm::OptArc<RawPipeStream>: OptArc`，就能对「总是 `Arc`」或「可能是 `MaybeArc`」两种情况写同一套逻辑。

此外还有一个 `OptArcIRC`（IRC = **I**n-place **R**ef**C**lone）trait，它的 `refclone` 签名是 `&self -> Self`（只需共享引用即可克隆）。这个细微差别是区分两类存储形态的钥匙——见 4.2.3。

#### 4.2.2 核心流程

`OptArc` 的两个实现者各自的「性格」：

| 行为 | `Arc<T>` 的实现 | `MaybeArc<T>` 的实现 |
| --- | --- | --- |
| `get()` | `Some` 自己（`&self` 即 `&Arc<T>`，解引用得 `&T`） | 按变体返回内联值或 `Arc` 解引用 |
| `get_arc()` | 恒 `Some(self)` | `Inline`→`None`，`Shared`→`Some` |
| `refclone(&mut self)` | `Arc::clone`（计数+1） | 见 4.1.3，可能触发升级 |
| `try_make_owned` | **恒返回 `false`**（`Arc` 永远是 `Arc`，没法变内联） | strong=1 时返回 `true` 并降级 |
| 实现 `OptArcIRC`？ | **是**（`refclone(&self)` = `Arc::clone`） | **否**（升级需要 `&mut self`，无法用 `&self` 做） |

这张表是本节的精髓。两个最关键的差异：

1. `Arc::try_make_owned` 恒为 `false`——因为它根本不存在「内联」这个目标状态。
2. 只有 `Arc` 实现 `OptArcIRC`——因为 `MaybeArc` 要从 `Inline` 升级到 `Shared` 必须改写 `self`，需要 `&mut self`，无法满足 `&self` 签名。

#### 4.2.3 源码精读

先看 `OptArc` 与 `OptArcIRC` 的定义：

[文件路径:L6-L24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L6-L24) 定义了 `OptArc`（含 `ptr_eq` 默认实现）与 `OptArcIRC`。注意 `OptArc` 的 supertrait 约束里有 `From<Self::Value>`、`From<Arc<Self::Value>>`、`Into<MaybeArc<Self::Value>>`——这三个转换约束让任意 `OptArc` 都能被「装回」`MaybeArc`，这正是 split 在两个不同 `OptArc` 类型之间搬运时用到的桥（见 4.3）。

再看 `Arc<T>` 对这两个 trait 的实现：

[文件路径:L26-L40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L26-L40) 是 `Arc<T>` 的 `OptArc` + `OptArcIRC` 实现。三个要点：`get`/`get_arc`/`refclone` 都是 `#[inline(always)]` 的一行委托；`try_make_owned` **恒返回 `false`**（`Arc` 无内联态可降级）；`OptArcIRC::refclone(&self)` 就是 `Arc::clone`，只需共享引用。

那么「到底用 `Arc` 还是 `MaybeArc`」由谁决定？在 **Tokio** 流里，由 send 模式 `Sm` 的关联类型决定。看 `PipeModeTag`：

[文件路径:L26-L35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L26-L35) 定义了 `PipeModeTag` 的关联类型 `type OptArc<T>: OptArc<Value = T> where T: Send + Sync`。注释「Used on the send mode to determine if the raw instance should always be in an `Arc` to enable `&self` refcloning or not」一语道破：send 模式决定底层到底用「总是 `Arc`」还是「可内联的 `MaybeArc`」。

具体取值在 `present_tag!` 宏里：

[文件路径:L63-L94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L63-L94) 用宏为三种 send 模式生成 `OptArc` 取值：`None`（不带 tokio flusher 的 arm，[L64-L72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L64-L72)）取 `MaybeArc<T>`；`Bytes`/`Messages`（默认 arm，[L73-L81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L73-L81)）取 `Arc<T>`。汇总在 [L87-L96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L87-L96)。

为什么「能 send 的 Tokio 流」必须用 `Arc`？因为它的发送路径要做**原子刷新**，需要从 `&self`（共享引用）克隆出 `Arc`：

[文件路径:L29-L35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/send.rs#L29-L35) 调用 `Sm::cast_oai(&self.raw)` 拿到一个实现 `OptArcIRC` 的引用，再交给 `flusher.flush_atomic`。`flush_atomic` 要在并发发送时不持有 `&mut self`，只能靠 `&self` 引用克隆——而只有 `Arc` 满足 `OptArcIRC`。所以 `Bytes`/`Messages` 的 send 模式被钉死为 `Arc`。反观 `None`（只收不发），没有这条原子刷新路径，于是可以享受 `MaybeArc` 的内联优化。

对比 **同步** 流：它没有这条约束，于是简单粗暴地一律用 `MaybeArc`：

[文件路径:L57-L60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L57-L60) 是同步 `PipeStream` 的结构定义，`raw` 直接是 `MaybeArc<RawPipeStream>`，不随模式变化。也就是说：**每一条同步 named pipe 流，只要不 split，都是零堆分配的。**

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：亲手确认「`MaybeArc` 不实现 `OptArcIRC`，而 `Arc` 实现」这一关键事实，并理解其后果。

**操作步骤**：

1. 在 [maybe_arc.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L22-L40) 中搜索 `impl ... OptArcIRC`。你只会找到一处：`impl<T: Send + Sync> OptArcIRC for Arc<T>`。确认**没有** `impl ... OptArcIRC for MaybeArc<T>`。
2. 对照 `OptArcIRC::refclone` 的签名 `fn refclone(&self) -> Self`（[L22-L24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L22-L24)）与 `MaybeArc` 的 `refclone` 签名 `fn refclone(&mut self) -> Self`（[L72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/maybe_arc.rs#L72)）。前者只借共享引用，后者需要独占引用。
3. 思考：`MaybeArc` 处于 `Inline` 时，要克隆出第二份并把自己改成 `Shared`，必须**写入** `self`，因此不可能是 `&self`。

**预期结果**：你会得出结论——**「能否从 `&self` 克隆」是区分两种存储形态的本质能力**。Tokio 发送路径（[send.rs:L29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/send.rs#L29)）正是依赖 `&self` 克隆，所以那条路径上的流被强制为 `Arc`，牺牲内联优化换取并发发送能力。

**需要观察的现象**：无需运行。这是纯类型层面的约束，读懂签名即可。

#### 4.2.5 小练习与答案

**练习 1**：`Arc<T>` 的 `try_make_owned` 为什么恒返回 `false`？

> **参考答案**：`Arc` 这个类型本身**没有「内联」这一目标状态**——它永远是堆上的引用计数指针。`try_make_owned` 的语义是「尝试把共享形态降级为内联单所有者」，对 `Arc` 而言不存在内联态，所以无降级可言，恒返回 `false` 表示「未能变成 owned（指内联 owned）」。注意它并不报错，只是告知调用方「没变化」。

**练习 2**：如果让 `MaybeArc` 也实现 `OptArcIRC`（即提供 `refclone(&self) -> Self`），会遇到什么本质困难？

> **参考答案**：当 `MaybeArc` 处于 `Inline` 时，要克隆出第二份就必须把内联值搬上堆并把 `self` 改写成 `Shared`——这需要**写入** `self`，而 `&self` 是不可变借用，不允许写入。若处于 `Shared`，`Arc::clone(&self)` 倒是可以，但要做一个「先 `get_arc` 判断、再决定能不能克隆」的运行期分支，而且对 `Inline` 直接返回错误又破坏了 `refclone` 的「无失败」语义。因此设计上干脆**不**为 `MaybeArc` 实现 `OptArcIRC`，用「不实现某 trait」来表达「不具备某能力」，让需要该能力的代码（如 Tokio 发送）在类型层就被迫选用 `Arc`。

**练习 3**：`OptArc` 的 supertrait 里为什么要写 `Into<MaybeArc<Self::Value>>`？

> **参考答案**：为了让任意 `OptArc` 实现者（不管是 `Arc` 还是 `MaybeArc`）都能被统一转换成 `MaybeArc`。这在 `split` 跨越两种 `OptArc` 类型时必不可少——例如 Tokio 流 split 时，原流的 `raw` 可能是 `Arc<RawPipeStream>`，而产出的收半（`RecvPipeStream`，其 `Sm=None`）的 `raw` 字段是 `MaybeArc`，需要 `.into()` 把 `Arc` 装成 `MaybeArc::Shared`。有了 `Into<MaybeArc>` 约束，泛型代码就能用统一的 `.into()` 完成这种搬运。详见 4.3。

---

### 4.3 split 的升级与 reunite 的降级：生命周期决定内存布局

#### 4.3.1 概念说明

`MaybeArc` / `OptArc` 的真正用武之地，是 `PipeStream::split` 与 `reunite` 这对操作。它们把 4.1、4.2 的抽象拼成一条完整链路：

- **`split`** 把一条双工流按值拆成「收半」与「发半」。拆完之后，两半要共享同一个底层连接，所以底层必然要从单所有者（`Inline`）升级为多所有者（`Shared`）。这一步调用 `refclone`。
- **`reunite`** 把收半与发半重新合并回一条双工流。合并前要先用 `ptr_eq` 确认两半确实来自同一条流；确认后调用 `try_make_owned` 尝试把形态降级回 `Inline`（如果此刻是唯一所有者）。

之所以说「生命周期决定内存布局」，是因为**是否发生堆分配，完全取决于你有没有 split、以及 split 后有没有 reunite**：

- 从不 split → 永远 `Inline` → 零堆分配。
- split 后不 reunite（或 reunite 时仍有其它引用）→ 永远 `Shared` → 保持一次堆分配。
- split 后 reunite 且无其它引用 → 回到 `Inline` → 堆块释放。

#### 4.3.2 核心流程

`split` 与 `reunite` 互为逆操作，状态流转如下：

```
                PipeStream<Rm, Sm>                  (raw 形态: Inline 或 Shared)
                        │ split()
            refclone 升级 / 拷贝
                        ▼
   ┌─────────────────────────────────────────┐
   │ RecvPipeStream<Rm>      SendPipeStream<Sm>│   (两半的 raw 都成 Shared，共享同一 Arc)
   └─────────────────────────────────────────┘
                        │ reunite()
            ptr_eq 判同源 → try_make_owned 降级
                        ▼
                PipeStream<Rm, Sm>                  (raw 形态: 若唯一所有者则回到 Inline)
```

注意 `split` 调用的 `refclone` 对 `Inline` 必然触发升级（堆分配），对已经是 `Shared` 的流则只是 `Arc::clone`（不再分配）。也就是说，**对同一条流反复 split→reunite→split，只有第一次 split 真正分配**（前提是每次 reunite 都成功降级回 `Inline`）。

#### 4.3.3 源码精读

先看 **Tokio** 版的 split 与 reunite：

[文件路径:L43-L49](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl.rs#L43-L49) 是 Tokio `split`。`raw_ac = self.raw.refclone()` 触发升级并产出第二份；收半用 `raw_a.into()`（这里 `raw_a` 是 `self.raw`，类型 `Sm::OptArc`，可能 `Arc`），经 `Into<MaybeArc>` 装成收半字段所需的 `MaybeArc`；发半直接持有 `raw_ac`。注意 `flusher` 被搬到发半，收半的 `flusher` 是 `()`（因为收半的 `Sm=None`）。

[文件路径:L53-L61](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl.rs#L53-L61) 是 Tokio `reunite`。先用 `MaybeArc::ptr_eq(&rh.raw, &sh.raw)` 判同源——注意这里显式写成 `MaybeArc::ptr_eq`，但 `ptr_eq` 是 `OptArc` 的默认方法，对任意两个 `OptArc<Value = 同一类型>` 都能比较。不同源则 `Err(ReuniteError { rh, sh })` 原样退还两半所有权。同源则 `drop(rh)` 后对 `sh.raw` 调 `try_make_owned`（若发半是 `Arc`，恒返回 `false`、不变；若涉及 `MaybeArc` 且唯一所有者，则降级回 `Inline`），最后拼回 `PipeStream`。

再看 **同步** 版（更简洁，因为没有 `flusher` 和 `Sm::OptArc` 的关联类型间接层）：

[文件路径:L35-L41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L35-L41) 是同步 `split`：`self.raw.refclone()` 升级，两半都直接持有 `MaybeArc`（因为同步流的 `raw` 一律是 `MaybeArc`）。

[文件路径:L45-L53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L45-L53) 是同步 `reunite`：`ptr_eq` 判同源 → `drop(rh)` → `raw.try_make_owned()` 尝试降级 → 拼回。这里的 `try_make_owned` 是真正能成功的：因为同步流始终是 `MaybeArc`，只要 reunite 时没有其它所有者（通常如此），就能把 `Shared` 降回 `Inline`，释放堆块。

最后看「未 split 才有」的特权 API——把流直接解包成裸 `OwnedHandle`：

[文件路径:L50-L59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L50-L59) 是 `TryFrom<PipeStream> for OwnedHandle`。它 `match s.raw`：`Inline(x) => Ok(x.into())`（成功取出裸句柄），`Shared(..) => Err(s)`（流已被 split、处于共享态，无法唯一地取出句柄，原样退还）。这是 4.1 内联优化的**直接受益点**：正因为未 split 的流是 `Inline`，你才能零代价地拿到它唯一的 `OwnedHandle`，进而（配合 u5-l2）把它交给子进程。一旦 split，这条捷径就关闭了。

> 对比 [tokio 版 handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/handle.rs) 你会发现 Tokio 流**没有**这个 `TryFrom<PipeStream> for OwnedHandle` 实现——因为能 send 的 Tokio 流恒为 `Arc`（`Shared`），不存在可靠的「解包成唯一句柄」语义，干脆不提供。

#### 4.3.4 代码实践（源码阅读 + 思维实验型）

**实践目标**：追踪一条同步流「创建 → split → reunite」全过程的形态变化，画出每一步的 `MaybeArc` 变体与堆分配次数。

**操作步骤**：

1. 假设有一条 `DuplexPipeStream<Bytes>`（同步双工字节流），刚由 listener accept 出来。据 [ctor.rs:L107-L109](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L107-L109)，其 `raw` 是 `MaybeArc::Inline`。**累计堆分配：0**。
2. 调用 `split()`。据 [impl.rs:L35-L41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L35-L41)，`refclone` 触发升级：收半与发半的 `raw` 都变成 `Shared`，指向同一块堆。**累计堆分配：1**。
3. 尝试 `OwnedHandle::try_from(收半)`。据 [handle.rs:L50-L59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L50-L59)，此时 `raw` 是 `Shared`，返回 `Err(原流)`——解包失败。**观察：split 后无法取裸句柄。**
4. 调用 `reunite(收半, 发半)`。据 [impl.rs:L45-L53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L45-L53)，`ptr_eq` 判同源（成功，因两半指向同一堆块），`try_make_owned` 因此刻是唯一所有者而成功，`raw` 降回 `Inline`，堆块释放。**累计堆分配净额：0**。
5. 再次 `OwnedHandle::try_from(合并后的流)`。此时 `raw` 是 `Inline`，返回 `Ok(句柄)`——解包成功。

**预期结果**：

| 步骤 | `raw` 变体 | 净堆分配 | 能否解包成 `OwnedHandle` |
| --- | --- | --- | --- |
| 刚 accept | `Inline` | 0 | 能 |
| split 后 | `Shared`（两半共享） | 1 | 不能 |
| reunite 后（无其它引用） | `Inline` | 0 | 能 |

**需要观察的现象**：第 3 步与第 5 步的「能否解包」差异，是内联优化的可观测后果。若想在 Windows 上实测，可写一段同步 named pipe 程序，在 split 前后分别尝试 `OwnedHandle::try_from`，对比 `Ok`/`Err`。**运行结果待本地（Windows）验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reunite` 必须先 `ptr_eq` 判同源，而不能直接 `try_make_owned`？

> **参考答案**：`reunite` 的契约是「把**同一条流** split 出的收发两半合并回去」。如果调用者把来自**不同**流的两半塞进来，强行合并会产生一条语义错乱的流（收半连的是连接 A、发半连的是连接 B）。`ptr_eq` 用内存地址/`Arc` 指针判定两半是否同源，不同源则用 `ReuniteError { rh, sh }` 原样退还两半所有权（u7-l3 讲过 `ReuniteError` 的所有权归还语义），既报错又不丢失调用者的资源。`try_make_owned` 只负责形态降级，不负责判同源，两者职责分离。

**练习 2**：对一条 Tokio 的 `DuplexPipeStream<Bytes>` 调用 `reunite`，`try_make_owned` 会成功降级回 `Inline` 吗？为什么？

> **参考答案**：不会。Tokio 双工流的 `raw` 是 `Sm::OptArc`，而 `Sm=Bytes` 时 `OptArc=Arc`（见 4.2.3）。`Arc::try_make_owned` **恒返回 `false`**（见 4.2.2 表），不存在内联态可降级。所以 Tokio 双工流一旦创建（或 split 后 reunite）始终是堆上的 `Arc` 形态——这是它为了支持 `&self` 并发发送而付出的固定代价。只有 Tokio 的**只收**流（`Sm=None`，用 `MaybeArc`）和所有同步流才能享受降级回 `Inline` 的好处。

**练习 3**：`split` 之后立刻 `reunite`（中间没有其它引用），堆分配的净次数是多少？如果再 `split` 一次呢？

> **参考答案**：净次数为 0。第一次 `split` 分配 1 次（`Arc::new`），紧接着 `reunite` 因唯一所有者而 `try_make_owned` 成功，释放该堆块，净 0。若再 `split` 一次，由于此刻已回到 `Inline`，会再次触发 `Arc::new` 分配 1 次。也就是说 `Inline ↔ Shared` 是可逆的，反复 split/reunite 不会累积堆分配（只要每次 reunite 都成功降级）。

---

## 5. 综合实践

**任务**：为「为什么 interprocess 要为同步流做 `MaybeArc` 内联优化」写一份简短的设计说明（约 200 字），要求覆盖以下要点，并各引一处源码为证：

1. 一条流有两种所有权形态，且可在运行期切换——引用 `MaybeArc` 枚举定义。
2. 未 split 时是内联、零堆分配，且能解包成裸句柄——引用 `Self::new` 的 `raw.into()` 与 `TryFrom<PipeStream> for OwnedHandle`。
3. split 时升级为堆上 `Arc`、reunite 时尝试降级——引用 `refclone` 与 `try_make_owned`。
4. Tokio 下「能 send 的流」被迫放弃内联、恒用 `Arc`，原因是发送路径需要 `&self` 克隆——引用 `present_tag!` 的 `OptArc` 取值与 `flush_atomic` 调用。

**参考思路（供对照，非唯一答案）**：

> interprocess 把每条 named pipe 流的底层连接包进 `MaybeArc<T>`（`enum { Inline(T), Shared(Arc<T>) }`），让「单所有者」与「多所有者」两种形态共存于一个类型。同步流一律用 `MaybeArc`：刚创建时 `Self::new` 用 `raw.into()` 包成 `Inline`，零堆分配，且因处于 `Inline` 可经 `TryFrom<PipeStream> for OwnedHandle` 直接取出裸句柄交给子进程。只有当用户 `split` 时，`refclone` 才把它升级为堆上 `Arc`；`reunite` 则用 `ptr_eq` 判同源后，靠 `try_make_owned` 在唯一所有者时降级回 `Inline`、释放堆块。在 Tokio 下，「能 send 的流」的 send 模式被 `present_tag!` 钉为 `OptArc=Arc`，因为发送路径 `flush_atomic` 要从 `&self` 克隆（`OptArcIRC`），只有 `Arc` 能做到——这是用「放弃内联优化」换取「并发发送能力」的刻意取舍。

**进阶（可选）**：设想你要给同步流增加一个「`&self` 并发读」的能力（类似 Tokio 的 `flush_atomic` 但用于读），你需要让 `MaybeArc` 实现 `OptArcIRC`。请说明这会在 `Inline` 分支遇到什么根本困难（参考 4.2.5 练习 2），并思考一种不破坏现有不变式的替代设计（例如：要求调用者先手动升级到 `Shared`，或干脆像 Tokio 那样让该能力对应的模式直接用 `Arc`）。

---

## 6. 本讲小结

- `MaybeArc<T>` 是 `enum { Inline(T), Shared(Arc<T>) }`：把「单所有者（内联，零堆分配）」与「多所有者（堆上 `Arc`）」两种形态装进同一类型，实现**惰性堆分配**——只有真正 split 出第二个所有者时才上堆。
- `OptArc` trait 用类型擦除统一了 `Arc<T>` 与 `MaybeArc<T>`：`get`/`get_arc`/`refclone`/`try_make_owned`/`ptr_eq` 五个方法让泛型代码（如 `split`/`reunite`）对两种存储形态写同一套逻辑。
- 两者关键差异：`Arc::try_make_owned` 恒返回 `false`（无内联态）；只有 `Arc` 实现 `OptArcIRC`（`&self` 克隆），`MaybeArc` 因升级需要 `&mut self` 而**不**实现它。
- `split` 调 `refclone` 做**升级**（`Inline→Shared`，触发 `Arc::new`）；`reunite` 先 `ptr_eq` 判同源，再 `try_make_owned` 做**降级**（`Shared→Inline`，仅当 strong=1）。
- 同步流一律用 `MaybeArc`（永远可内联）；Tokio 流按 send 模式选——`None`（只收）用 `MaybeArc`，`Bytes`/`Messages`（能 send）用 `Arc`，因为发送路径 `flush_atomic` 需要 `&self` 克隆。
- 内联优化的直接受益点：未 split 的同步流可经 `TryFrom<PipeStream> for OwnedHandle` 零代价取出唯一裸句柄；split 后该捷径关闭。

---

## 7. 下一步学习建议

- **本讲与 u8-l1（linger_pool）合流**：升级为 `Shared` 后，一条流就具备了「被多个所有者共享、drop 时机不确定」的形态，这与 u8-l1 讲的 limbo 刷新机制紧密相关。建议重读 `RawPipeStream::drop`（[stream.rs:L70-L80](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream.rs#L70-L80)），理解「最后一个所有者 drop 时才触发 limbo」与 `Arc` 引用计数的关系。
- **承接 u7-l2（句柄所有权）**：本讲的 `try_make_owned` 与 u7-l2 的 `TryClone`/`AdvOwnedHandle` 同属「句柄所有权管理」主题。可对照阅读 `TryClone::try_clone`（[handle.rs:L92-L99](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L92-L99)），注意它**另起一个 `Inline`**（复制句柄、不复用 `Arc`），与本讲的「升级共享」是两条不同的所有权路径。
- **拓展阅读**：若想验证「未 split 时零堆分配」，可在 Windows 上用一个自定义 `GlobalAlloc`（统计 `alloc`/`dealloc` 次数）包裹程序，对比「accept 后直接用」与「accept 后 split」两种用法的分配差值。这是把本讲的布局推理变成可观测事实的最直接方式。
- **下一站（u8-l3）**：从「流的内部存储优化」转向「流的安全属性」——Windows 安全描述符与 DACL，看 `ListenerOptions::security_descriptor` 如何控制 named pipe 的访问权限。
