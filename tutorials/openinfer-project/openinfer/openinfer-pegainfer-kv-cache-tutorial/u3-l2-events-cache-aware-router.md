# KV 事件流与缓存感知路由

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `KV_EVENT_CHANNEL_CAPACITY = 65_536` 这个「宽裕」容量背后的风险模型：为什么丢一个事件就等于让路由器的前缀树永久失步，以及这个模型如何反过来决定了容量、排水节奏与「默认关闭」三件事。
2. 复述事件通道的完整构造链：`KvCacheManager::new_with_events`（唯一公共入口）→ `BlockPool::with_events`（`pub(crate)` 内部实现）→ 上游 `kvbm-logical` 的 `EventsManager` / `BlockRegistry` / `tokio::broadcast`。
3. 梳理 `RegisteredBlock` 里并存的**两套哈希体系**：引擎侧 128 位 lineage 哈希（`plh`）与路由侧 u64 哈希（`sequence_hash` / `tokens_hash` / `parent_sequence_hash`），以及为什么逐出事件需要从前者反查后者。
4. 独立推导一个事件消费者的实现契约：忽略 `Create`、在封块点用 `take_newly_registered_blocks` 自产更丰富的 store 事件、把 `Remove` 映射回路由树的节点键。

本讲是专家层的第二讲，承接 u2-l5（前缀缓存与 lineage 哈希链）与 u3-l1（恢复-提交链路）。前面两讲解决的是「单机内缓存如何命中与恢复」，本讲回答的是：**当缓存分布在多个 worker 上时，谁来告诉调度器哪个 worker 手里有哪段前缀？**

## 2. 前置知识

### 2.1 缓存感知路由（cache-aware router）

多副本部署里，同一个 prompt 前缀（比如相同的系统提示词）可能已经在 worker A 上算过 prefill。如果新请求被路由到 A，就能直接跳过这段 prefill；路由到 B 就得重算。**缓存感知路由器**就是干这件事的组件：它给每个 worker 维护一棵**前缀树**（radix tree）——「这个 worker 手里有哪些前缀块」，然后按请求的 token 前缀选目标 worker。

路由器是 **out-of-band**（带外）的：它不在请求的转发路径上做计算，只是旁路订阅每个引擎的 KV 块事件流，据此更新自己那棵树。本讲的主角就是这条旁路事件流。

### 2.2 tokio broadcast 的三条语义

`tokio::sync::broadcast` 是多生产者多消费者通道，理解本讲必须先知道它的三条行为：

1. **环形缓冲 + 定容**：通道容量在创建时固定。容量之内的事件不会阻塞发送方——发送方只管往里塞。
2. **Lagged = 丢历史**：某个接收者消费太慢、落后超过容量时，它会收到一个 `Lagged` 错误，**被跳过的事件永久丢失**，之后继续收新事件。没有补发机制。
3. **晚订阅丢更早的事件**：订阅只从订阅时刻开始收；之前发生的事件看不见。

本讲所有的设计决策——容量 65_536、每步排水、构造前先订阅——都是对这三条语义的回应。

### 2.3 RAII 与 Drop 钩子

Rust 的 RAII 惯用法：把「释放时的副作用」写进一个句柄类型的 `Drop` 实现里。上游 kvbm-logical 用它实现 Remove 事件——块注册时挂一个 `EventReleaseHandle`，当块的最后一个引用被丢弃（被逐出/释放）时，`Drop` 自动广播 `Remove`。你在 u2-l1 已经见过同款手法（`ImmutableBlock` 的 RAII 保证 padding 块不回空闲池）。

### 2.4 回顾：lineage 哈希链

u2-l5 讲过：每个缓存块的身份 = 位置 + 块内容 + 父块哈希 + salt 构成的 **lineage 哈希链**（kvbm 的 128 位 `SequenceHash`）。共享前缀逐位相同、分叉即全变。本讲会看到同一个 128 位哈希在两套消费体系里的两种用途。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/pool.rs` | 绝对主角：容量常量 `KV_EVENT_CHANNEL_CAPACITY`、`BlockPool::with_events`、`RequestKv::emitted_blocks` 光标、`take_newly_registered_blocks`、`RegisteredBlock` |
| `src/manager.rs` | 唯一公共入口 `KvCacheManager::new_with_events` |
| `src/lib.rs` | `KvCacheEvent` 从 kvbm-logical 的再导出，保证类型同一性 |
| `kvbm/kvbm-logical/src/events/protocol.rs`（上游依赖） | `KvCacheEvent` 枚举定义、`EventReleaseHandle` 的 Drop→Remove 机制 |
| `kvbm/kvbm-logical/src/events/manager.rs`（上游依赖） | `EventsManager`：发射策略、通道容量默认值、`subscribe_receiver`、`on_block_registered` 钩子 |

注意后两个文件在 `pegainfer-kv-cache` 之外——它们是本 crate 的上游依赖 `kvbm-logical`（workspace 内的本地 fork，见根 `Cargo.toml` 的 `kvbm/kvbm-logical` path 依赖）。本讲引用它们是为了把「事件从哪来」讲透；日常开发中你只需要消费本 crate 的公共 API。

## 4. 核心概念与源码讲解

### 4.1 通道的构造：`KV_EVENT_CHANNEL_CAPACITY` 与 `BlockPool::with_events`

#### 4.1.1 概念说明

普通 `BlockPool::new` 构造的池是「哑」的：块注册、逐出都只发生在池内部，外界无从得知。`with_events` 构造的池则额外挂了一条事件流：每当有块注册（可被他人复用）或块被彻底丢弃（逐出），都会向通道广播一个事件。路由器订阅这条流，就能把「每个 worker 有哪些前缀块」实时镜像成一棵前缀树。

这里有一个先讲直觉的风险模型，它解释了后面所有数字：

> 路由器的前缀树是**纯增量镜像**——`Create`/store 插入、`Remove` 摘除。没有任何对账协议可以让树重新和实际缓存对齐。所以**丢一个事件，树就和现实永久不一致**：树里多出一个幽灵块（路由器会把请求派去一个其实没有该前缀的 worker），或少一个实际存在的块（前缀复用机会被浪费）。两种都不崩，但都让路由决策建立在幻觉上。

既然「丢事件 = 不可恢复的失步」，设计就只剩两道防线：**容量足够宽裕** + **消费足够勤快**。容量常量的注释把这两点都写明了。

#### 4.1.2 核心流程

`with_events` 的构造流程：

```text
BlockPool::with_events(block_size, num_blocks)
  1. EventsManager::builder()
       .channel_capacity(65_536)        ← 覆盖上游默认 1024
       .build()                          ← 默认 AllEventsPolicy：每块都跟踪
  2. events.subscribe_receiver()         ← 在建池【之前】订阅 → 一个事件不漏
  3. BlockRegistry::builder()
       .event_manager(events).build()    ← 注册表挂上事件钩子
  4. Self::build(block_size, num_blocks, registry)
       ↑ 与普通 new 完全共用同一条构造路径（含 padding 块预留）
```

普通 `new` 与 `with_events` 的**唯一**区别是第 3 步传进 `build` 的注册表：`new` 传一个不带事件管理器的裸 `BlockRegistry`（[src/pool.rs:L38-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L38-L40)），`with_events` 传挂了钩子的。块的分配、封块、前缀匹配、LRU 逐出逻辑一个字都不用改——事件是纯旁路观测。

#### 4.1.3 源码精读

先看容量常量及其注释——这是本讲最重要的一段注释：

> ```rust
> /// Broadcast capacity for the opt-in KV-event feed. Generous: the scheduler
> /// drains it every step, but a step can register/evict many blocks, and a
> /// dropped event silently desyncs the router's prefix tree. Only allocated on
> /// the [`BlockPool::with_events`] path.
> const KV_EVENT_CHANNEL_CAPACITY: usize = 65_536;
> ```

- [src/pool.rs:L20-L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L20-L24)：容量 65_536。注释三句话对应三个事实：**宽裕**（Generous）——一步可能注册/逐出很多块；**失步是静默的**（silently desyncs）——没有报错、没有自愈；**只有 with_events 路径才付这个分配**（Only allocated on … path）。

再对比上游默认值，体会「宽裕」是相对谁说的：

- [kvbm/kvbm-logical/src/events/manager.rs:L44-L58](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L44-L58)：`EventsManagerSettings` 的 `channel_capacity` 默认 `1024`。本 crate 显式提到 65_536，是默认值的 **64 倍**。

为什么必须放大？做个数量级估算（示例演算，不是源码断言）：block_size = 16 时，一个 8192 token 的 prompt 一步 prefill 就要注册 \(\lceil 8192/16 \rceil = 512\) 个块；`evict_inactive` 冷冲刷（u2-l5 讲过）一次性逐出整个非活跃池——一个 4096 块的池就是上千个 Remove。若消费者一步之内没来得及排空而突发量超过容量，`Lagged` 就触发。安全条件可以写成：

\[
E_{\text{step}} \;=\; \#\text{新注册块} + \#\text{逐出块} \;\le\; C = 65\,536
\]

65_536 给单步突发留了数十倍余量，让这个条件在实际速率下不可能被打破。

接着看 `with_events` 本体：

- [src/pool.rs:L50-L64](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L50-L64)：

  ```rust
  pub(crate) fn with_events(
      block_size: usize,
      num_blocks: usize,
  ) -> anyhow::Result<(Self, broadcast::Receiver<KvCacheEvent>)> {
      // Default emission policy is AllEventsPolicy (every block tracked).
      let events = Arc::new(
          EventsManager::builder()
              .channel_capacity(KV_EVENT_CHANNEL_CAPACITY)
              .build(),
      );
      let rx = events.subscribe_receiver();
      let registry = BlockRegistry::builder().event_manager(events).build();
      let pool = Self::build(block_size, num_blocks, registry)?;
      Ok((pool, rx))
  }
  ```

  三个细节值得停下来：

  1. **`pub(crate)` 可见性**——它不是公共 API，外部使用者不能直接调（4.2 讲公共入口）。把「带事件的池」留在 crate 内部，`BlockPool` 对外的构造面就保持最窄。
  2. **`subscribe_receiver()` 而非 `subscribe()`**——上游对这两个方法的取舍有明确注释：[kvbm/kvbm-logical/src/events/manager.rs:L153-L159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L153-L159)：「给需要**同步排水**的调用方（比如从非异步的调度线程 `try_recv`），且**必须观察到 `Lagged`** 而不是让它被悄悄过滤掉」。`subscribe()` 返回的 Stream 会把 `Lagged` 错误直接滤掉——对本讲的风险模型来说那是灾难性的静默；裸 Receiver 才能让消费者看见失步。
  3. **先订阅、后建池**——`rx` 在 `Self::build` 之前拿到手，所以从块 0（padding 块）注册起的全部事件都可见，不存在晚订阅丢事件的问题。

最后看事件本身长什么样（上游定义）：

- [kvbm/kvbm-logical/src/events/protocol.rs:L28-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/protocol.rs#L28-L34)：

  ```rust
  pub enum KvCacheEvent {
      /// A block has been registered in a worker's cache.
      Create(SequenceHash),
      /// A block has been removed from a worker's cache.
      Remove(SequenceHash),
  }
  ```

  两个变体**都只携带一个 `SequenceHash`**——kvbm 的 128 位 lineage 哈希。注意这个信息量：`Create` 既没有路由侧哈希，也没有父块链接。这个「穷」直接导致了 4.4 的消费契约。

- [kvbm/kvbm-logical/src/events/protocol.rs:L64-L91](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/protocol.rs#L64-L91)：`EventReleaseHandle` 持有 `(seq_hash, event_tx)`，其 `Drop` 实现 `send(Remove(seq_hash))`。它作为附件挂在块的注册句柄上（挂接动作在 [events/manager.rs:L173-L189](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L173-L189) 的 `on_block_registered` 里完成）：块注册时发 `Create` + 挂附件，块最后一个引用消失时附件 `Drop` 发 `Remove`。发送失败（无接收者）被显式忽略——`let _ = send(...)`。

#### 4.1.4 代码实践

**实践目标**：把「容量为什么是 65_536」从一句注释变成你自己的数量级论证。

**操作步骤**：

1. 打开 [src/pool.rs:L20-L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L20-L24) 逐句翻译注释（英译中，写在你自己的笔记里）。
2. 打开 [kvbm/kvbm-logical/src/events/manager.rs:L44-L58](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L44-L58)，确认上游默认容量是 1024，算出 65_536 是它的多少倍。
3. 用下面的参数做一次突发量估算（示例参数，非源码规定）：block_size = 16，池 8192 块，一步 16384 token 的 prefill，紧接着一次 `evict_inactive` 冷冲刷逐出 4096 个非活跃块。算出该步的事件数 E_step，与 65_536 比较。
4. 验证「Only allocated on the with_events path」：在 crate 目录执行 `grep -n "broadcast" src/`，确认除了 import 外，broadcast 通道只出现在 `with_events` 一处构造点。

**需要观察的现象**：grep 结果里 `tokio::sync::broadcast` 的使用点应当只服务于事件通道，普通 `new` 路径的代码里没有任何通道分配。

**预期结果**：步骤 3 中 E_step = 1024（注册）+ 4096（逐出）= 5120，远小于 65_536，安全条件有约 12 倍余量；即使把 prompt 和池都再放大几倍，条件依然成立。步骤 4 的 grep 只命中 `pool.rs` / `manager.rs` 的少量行。

**待本地验证**：grep 的具体命中行数因版本而异，请以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `with_events` 用 `subscribe_receiver()` 拿裸 Receiver，而不用返回 Stream 的 `subscribe()`？

**参考答案**：消费方是引擎调度线程，需要用 `try_recv` 同步排水，不能依赖 async 运行时；更关键的是，`subscribe()` 返回的 Stream 会把 `Lagged` 错误悄悄过滤掉（上游注释原文："The stream filters out lagged errors … and continues delivering subsequent events"），而本讲的风险模型要求消费者**必须**能观察到 `Lagged`——它是「前缀树已失步」的唯一信号。裸 `broadcast::Receiver` 的 `try_recv` 会把 `Lagged` 作为错误返回。

**练习 2**：如果把 `KV_EVENT_CHANNEL_CAPACITY` 改回上游默认的 1024，最可能先出问题的场景是哪个：长 prompt 的一次性 prefill、多个并发请求的常规 decode、还是 `evict_inactive` 冷冲刷？

**参考答案**：`evict_inactive` 冷冲刷。常规 decode 每步每请求至多注册一两个块，量级很小；长 prompt prefill 一步注册 \(\lceil L/b \rceil\) 块，16k token / block_size 16 也只有 1024 块，刚好贴着容量；而冷冲刷一次逐出**整个非活跃池**，池有数千块时会瞬间产生数千个 Remove，轻松越过 1024，消费者一步没排水就 `Lagged` 失步。

**练习 3**：接收者是在建池之前订阅的。据此推断：消费者收到的**第一个**事件可能是什么？（提示：回忆 u2-l1 的 padding 块构造。）

**参考答案**：padding 块的 `Create` 事件。`build` 路径会 `register_block` 注册块 0（u2-l1 讲过：stage 默认哈希 → 注册 → `std::mem::forget`），注册走挂了钩子的 `BlockRegistry`，于是广播 `Create(SequenceHash::default())`。又因为 padding 块被 `std::mem::forget` 永久遗忘、其注册句柄永不 Drop，这个块**永远不会有对应的 Remove**。消费者应当预期这个「开场事件」并将其忽略。（此推断基于注册钩子的挂接机制，具体是否命中待本地验证。）

---

### 4.2 公共入口：`KvCacheManager::new_with_events` 与类型再导出

#### 4.2.1 概念说明

4.1 的 `BlockPool::with_events` 是 `pub(crate)`——外部使用者拿不到。真正暴露给模型 crate / 服务进程的入口在门面 `KvCacheManager` 上。这个分层有两层含义：

1. **门面职责**（u1-l1 讲过）：`KvCacheManager` 把逻辑池和同几何的物理 `KvBuffer` 配成一对，是全注意力模型的引擎级入口。事件能力作为「构造期选项」挂在门面上，而不是散落成裸池的公共构造器，公共面保持窄。
2. **类型同一性**：事件通道里流动的 `KvCacheEvent` 定义在上游 kvbm-logical。如果每个消费者都自己去依赖 kvbm-logical，版本或路径一旦分叉，通道两端的类型就不是同一个，`Receiver<KvCacheEvent>` 对不上。`lib.rs` 的再导出把这个问题钉死。

还有一个显式的设计立场：**默认关闭**。注释原文写明了成本项——单机服务不为路由器付一分钱。

#### 4.2.2 核心流程

```text
KvCacheManager::new_with_events(stream, 几何参数..., num_blocks)
  → KvBuffer::new(...)                    ← 物理缓冲，与 new 完全一样
  → BlockPool::with_events(block_size, num_blocks)
       → (pool, rx)
  → Ok((KvCacheManager { pool, buffer }, rx))   ← 元组：门面 + 事件接收器
```

调用方拿到 `(manager, rx)` 后：`manager` 照常走 `pool()` / `buffer()` 做推理；`rx` 交给旁路的路由器组件排水。两条路径互不干扰——这正是「带外」的含义。

#### 4.2.3 源码精读

- [src/manager.rs:L83-L103](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L83-L103)：

  ```rust
  /// Like [`new`](Self::new) but the pool emits KV block events; returns the
  /// receiver to drain. See [`BlockPool::with_events`].
  pub fn new_with_events(
      stream: &Arc<CudaStream>,
      num_layers: usize,
      num_kv_heads: usize,
      head_dim: usize,
      block_size: usize,
      num_blocks: usize,
  ) -> anyhow::Result<(Self, broadcast::Receiver<KvCacheEvent>)> {
      let buffer = KvBuffer::new(...)?;
      let (pool, events) = BlockPool::with_events(block_size, num_blocks)?;
      Ok((Self { pool, buffer }, events))
  }
  ```

  三个观察点：

  1. 参数表与 [`new`](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L21-L38)（[src/manager.rs:L21-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L21-L38)）**逐字相同**，只是返回值多了 `broadcast::Receiver<KvCacheEvent>`——对调用方来说，「开事件」是零认知负担的替换。
  2. 内部就是 4.1 的 `BlockPool::with_events`——门面只做转发，不带私货。
  3. 注意它**没有** `scratch_pages` 参数（对比 [src/manager.rs:L45-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L45-L66) 的 `new_with_scratch_pages`）：事件通道与投机 scratch 页两个选项目前不组合。

- [src/pool.rs:L42-L49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L42-L49)，`with_events` 的文档注释，把「默认关闭 + 成本项」写得很明白：

  > ```rust
  > /// ... OFF by
  > /// default: plain single-machine serving uses [`new`](Self::new) and pays
  > /// neither the per-block event attachment nor the channel.
  > ```

  成本有两笔：**(a)** 每个块注册时都要在注册句柄上挂一个 `EventReleaseHandle` 附件（RAII 附件 + 每次 `send`）；**(b)** 通道本身的内存分配。单机 serving 没有路由器，这两笔全是纯浪费——所以普通 `new` 走的是不带 `EventsManager` 的裸注册表（[src/pool.rs:L38-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L38-L40)），一分钱不付。

- [src/lib.rs:L7-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L7-L19)：

  ```rust
  pub use kvbm_logical;
  pub use kvbm_logical::events::KvCacheEvent;
  ```

  两行再导出：前者让下游能经 `pegainfer_kv_cache::kvbm_logical::...` 使用上游类型而不必自己声明依赖；后者专门把 `KvCacheEvent` 提到本 crate 命名空间——保证消费者写 `pegainfer_kv_cache::KvCacheEvent` 时，拿到的与 `new_with_events` 返回的 `Receiver` 里流动的是**同一个类型**（u1-l2 讲过这是类型同一性）。

- 一个诚实的现状说明：截至本讲撰写时的 HEAD（785d0487），workspace 内**尚无模型 crate 调用** `new_with_events` / `take_newly_registered_blocks`（全仓库 grep 仅命中本 crate 自身与教程文档）。这是一条为分布式缓存感知路由**预留的 opt-in 能力**，当前所有模型线都走默认关闭的 `new`。

#### 4.2.4 代码实践

**实践目标**：用编译器亲手验证「公共入口是 `new_with_events`，`BlockPool::with_events` 不可外部触达」。

**操作步骤**：

1. 阅读 [src/pool.rs:L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L50) 确认可见性修饰是 `pub(crate)`，再读 [src/manager.rs:L85](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L85) 确认是 `pub fn`。
2. 在你自己的临时 scratch crate（`cargo new event_probe`，`pegainfer-kv-cache = { path = ... }`）里写一段注定失败的代码：

   ```rust
   // 示例代码：故意越权调用 crate 内部方法，预期编译失败
   use pegainfer_kv_cache::BlockPool;
   let (pool, _rx) = BlockPool::with_events(16, 64).unwrap();
   ```

3. `cargo build` 观察报错；然后删掉这段代码，换成合法写法（`KvCacheManager::new_with_events`，需要 `Arc<CudaStream>`，即需要 CUDA 环境；没有 GPU 的话只需确认签名即可）。

**需要观察的现象**：步骤 2 应当得到一个可见性错误（Rust 的 E0624「method is private」一类），指出 `with_events` 仅在 `pegainfer_kv_cache` crate 内可见。

**预期结果**：编译失败信息证明外部无法绕过门面；同时 [src/lib.rs:L7-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L7-L19) 的导出清单里没有 `with_events` 相关条目，公共面只有 `KvCacheManager::new_with_events` 一个事件入口。

**待本地验证**：确切错误文本以本地 rustc 版本输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `new_with_events` 不做成 `new` 上的一个 `emit_events: bool` 参数？

**参考答案**：bool 参数会让所有调用方都面对一个「绝大多数时候该是 false」的选择题，且返回类型没法区分——带事件时必须额外返回 `Receiver`，签名只能变成 `Result<(Self, Option<Receiver>)>` 之类的丑形态。拆成独立构造器后，普通路径签名零改动（对比 [src/manager.rs:L21-L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L21-L38)），事件路径用元组显式强制调用方接住 Receiver——拿不到接收器就等于白开了通道。这与 crate 里 `new` / `new_with_scratch_pages` / `from_buffer` 的「每个变体一个命名构造器」风格一致。

**练习 2**：删掉 `pub use kvbm_logical::events::KvCacheEvent;`（[src/lib.rs:L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L9)）外部消费者还能用事件通道吗？

**参考答案**：能，但代价是每个消费者必须在自己的 `Cargo.toml` 里直接依赖 kvbm-logical，并且依赖声明必须解析到**同一路径/版本**，否则它写出的 `kvbm_logical::events::KvCacheEvent` 与 `new_with_events` 返回的 `Receiver<KvCacheEvent>` 中的 `KvCacheEvent` 是两个不同类型，`try_recv()` 的返回值无法按消费者自己的类型收尾——编译期就报类型不匹配。再导出把依赖收拢在唯一入口上，消灭了这种分叉可能。

---

### 4.3 封块点自产事件：`emitted_blocks` 光标、`take_newly_registered_blocks` 与 `RegisteredBlock`

#### 4.3.1 概念说明

4.1 说过通道里的 `Create(SequenceHash)` 信息很穷：只有一个 128 位 lineage 哈希，没有路由侧需要的 u64 哈希族，也没有父块链接。那路由器的 store（插入）事件从哪来？答案：**调用方在封块点自己造**。

`RequestKv` 上有一条与请求生命周期平行的「事件产出」机制：一个 `emitted_blocks` 光标记录「这个请求的序列块里，已经有多少个被作为 store 事件汇报给路由器了」，`take_newly_registered_blocks()` 则每次返回自上次调用以来**新注册**的块，打包成信息完整的 `RegisteredBlock` 四元组。调度循环里每个 apply 之后调一次，就能把本请求新贡献给缓存的块如实上报。

而 `RegisteredBlock` 里并存两套哈希体系，是本讲最值得咀嚼的设计：

| 字段 | 位宽 | 体系 | 用途 |
|------|------|------|------|
| `plh` | u128 | 引擎侧（kvbm lineage 哈希） | 与逐出事件对账：`Remove` 只携带 lineage 哈希，靠它反查 |
| `sequence_hash` | u64 | 路由侧（dynamo_tokens） | 路由前缀树的节点键 |
| `tokens_hash` | u64 | 路由侧 | 块内容哈希 |
| `parent_sequence_hash` | `Option<u64>` | 路由侧 | 父块链接，串成链；首块为 `None` |

两套体系各服务一个消费者：引擎内部（前缀匹配、LRU、offload 寻址）用 128 位 lineage；dynamo 生态的路由器用 u64 哈希族建树。`RegisteredBlock` 同时携带两者，等于在块注册的一瞬间完成了一次「双语登记」。

#### 4.3.2 核心流程

`emitted_blocks` 光标的推进规则（配合 u2-l2 的三拍生命周期阅读）：

```text
new_request(...)                     emitted_blocks = 0
    │
match_and_add_prefix / _up_to        emitted_blocks = assigned_blocks()
    │                                （跳过前缀命中块：它们已由首次封块者上报过）
    │
schedule → forward → apply …         每次应用后新块可能被注册
    │
take_newly_registered_blocks()       registered = assigned_blocks()
    │                                若 registered <= emitted_blocks → 返回空
    │                                否则返回 blocks[emitted_blocks..registered]
    │                                并把光标推到 registered
    ▼
（下一拍继续增量产出）
```

`take_newly_registered_blocks` 的算法骨架：

```text
输入: self（请求）, 光标 emitted_blocks
1. registered ← seq.assigned_blocks()          // 已拿到 ImmutableBlock 的块数
2. 若 registered ≤ emitted_blocks: 返回 []       // 本拍没有新注册
3. blocks ← seq.inner().sequence().blocks()     // 按序排列的 token 块
4. debug_assert!(registered ≤ blocks.len())     // 注册数不超过已封 token 块数
5. 对 blocks[emitted_blocks..registered] 逐块映射:
       plh                 ← b.kvbm_sequence_hash().as_u128()
       sequence_hash       ← b.sequence_hash()
       tokens_hash         ← b.block_hash()
       parent_sequence_hash← b.parent_sequence_hash()
6. emitted_blocks ← registered; 返回新块列表（序列序）
```

以 `assigned_blocks`（已注册）而非「已封 token 块」为准是有讲究的——源码注释说明：「A block becomes reusable by other requests once it is *registered* (assigned an `ImmutableBlock`), which lags sealing the token block by a step.」可复用性与注册绑定，路由器只该看见已可复用的块。

#### 4.3.3 源码精读

- [src/pool.rs:L379-L388](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L379-L388)：`RequestKv` 结构体与光标字段的完整文档：

  ```rust
  pub struct RequestKv {
      seq: SchedulableSequence<()>,
      /// Cursor for [`Self::take_newly_registered_blocks`]: how many of this
      /// request's sequence blocks have already been surfaced as KV-router store
      /// events. Starts past the prefix-cache hit (those were stored by whoever
      /// first sealed them — this assumes GPU-resident reuse, i.e. KV offload off,
      /// which holds wherever the event feed is enabled). Untouched on the plain
  ```

  这段注释里藏着一个**显式声明的设计耦合**：光标跳过前缀命中块的前提是「这些块从未离开过 GPU」——即 **KV offload 关闭**。注释接着说这个前提「holds wherever the event feed is enabled」（事件流开启的地方该前提都成立）：事件流与 offload 目前是互斥使用的两个 opt-in 世界。若两者同开，从 CPU 层恢复的块可能从未在本机发过 store 事件，光标语义就破了。

- [src/pool.rs:L183-L186](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L183-L186)：构造时光标清零——`RequestKv { seq, emitted_blocks: 0 }`。

- [src/pool.rs:L414-L422](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L414-L422)：`match_and_add_prefix_up_to` 尾部，光标的第二个初始化点：

  ```rust
  let blocks = self.seq.match_and_add_prefix_up_to(&pool.block_manager, max_blocks)?;
  // Prefix-hit blocks are already in the router's tree (whoever first sealed
  // them stored them, and a GPU hit means they were never evicted),
  // so the store-event cursor skips them.
  self.emitted_blocks = self.seq.assigned_blocks();
  ```

  命中块直接计入「已上报」——它们的上报责任属于第一次封存它们的那个请求（u2-l5 的非活跃池复活机制保证 GPU 命中块从未被逐出，所以那次上报仍然有效）。

- [src/pool.rs:L688-L718](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L688-L718)：`take_newly_registered_blocks` 全文（方法文档 + 实现，伪代码已在上文展开）。注意它是 `&mut self`——推进光标是内部状态变更；返回 `Vec<RegisteredBlock>`，本拍无新块时为空。

- [src/pool.rs:L721-L733](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L721-L733)：`RegisteredBlock` 及其文档，两套哈希的分工写得非常直白：

  ```rust
  /// One full KV block a request just registered, ready to become a KV-router
  /// store event. `sequence_hash`/`tokens_hash`/`parent_sequence_hash` are the
  /// router's u64 hashes (`dynamo_tokens::TokenBlock` accessors, identical by
  /// construction to what the router recomputes); `plh` is the engine's 128-bit
  /// lineage hash, kept only to correlate an eviction event (which carries the
  /// lineage hash) back to `sequence_hash`.
  #[derive(Clone, Copy, Debug)]
  pub struct RegisteredBlock {
      pub plh: u128,
      pub sequence_hash: u64,
      pub tokens_hash: u64,
      pub parent_sequence_hash: Option<u64>,
  }
  ```

  「identical by construction to what the router recomputes」——路由侧三个哈希取自 `dynamo_tokens::TokenBlock` 的访问器，与路由器自己重算的逐位相同，无需任何翻译。唯一需要翻译的是逐出事件（见 4.4）。

- 对照一个边界：u3-l1 讲过 [src/pool.rs:L735-L739](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L735-L739) 的 `sequence_hash_bytes` 把同一个 128 位 lineage 哈希大端编码成 16 字节**内容键**，那是给 **offload 层**寻址用的；本讲的 `plh` 是同一个哈希在**事件空间**里的形态。一个哈希、两个消费方向、两种编码约定——都收敛在 pool.rs 这一个文件里。

#### 4.3.4 代码实践

**实践目标**：用纯逻辑测试（无 GPU）亲手驱动光标，验证「新块增量产出、前缀命中跳过、重复调用为空」三个行为。

**操作步骤**：

1. 仿照 [tests/restore_commit.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L29) 的风格（该文件自述「Pure logical-pool tests, no GPU」），新建 `tests/events_cursor.rs`：

   ```rust
   // 示例代码：读者实践，仿 restore_commit.rs 风格
   use pegainfer_kv_cache::BlockPool;

   const BLOCK_SIZE: usize = 16;

   #[test]
   fn cursor_emits_only_newly_registered_blocks() {
       let pool = BlockPool::new(BLOCK_SIZE, 64).expect("pool");
       // 48 tokens = 3 full blocks；末 token 由 apply 产出
       let prompt: Vec<u32> = (0..48).map(|i| i % 97).collect();

       let mut seed = pool.new_request(prompt.clone(), 1, None);
       // 尚未 apply：没有任何已注册块
       assert!(seed.take_newly_registered_blocks().is_empty());
       seed.schedule_prefill(3 * BLOCK_SIZE, &pool).expect("sched");
       seed.apply_prefill(1, &pool).expect("apply");

       let first = seed.take_newly_registered_blocks();
       assert_eq!(first.len(), 3, "3 full blocks registered at apply");
       assert_eq!(first[0].parent_sequence_hash, None, "genesis block");
       assert!(first[1].parent_sequence_hash.is_some(), "chained");
       // 光标已推进：同拍再取为空
       assert!(seed.take_newly_registered_blocks().is_empty());
       seed.release().expect("release");

       // 同前缀的第二个请求：命中块不再上报（它们由 seed 上报过）
       let mut longer = prompt;
       longer.extend(48..64); // 第 4 块内容不同
       let mut req2 = pool.new_request(longer, 4, None);
       let matched = req2.match_and_add_prefix(&pool).expect("match");
       assert_eq!(matched, 3 * BLOCK_SIZE);
       assert!(req2.take_newly_registered_blocks().is_empty(),
               "prefix-hit blocks are already in the router's tree");
   }
   ```

2. 运行：`cargo test --release -p pegainfer-kv-cache --test events_cursor -- --nocapture`。

**需要观察的现象**：三次断言分别验证光标的三个行为——apply 前为空、apply 后产出 3 块且再次调用为空、命中前缀的后续请求产出为空。

**预期结果**：测试通过。依据：[tests/restore_commit.rs:L37-L41](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L37-L41) 已证实 `schedule_prefill + apply_prefill` 之后 3 个块进入 GPU 缓存（probe 能看到 gpu_hit = 3），即注册发生在 apply 点；光标跳过命中块则直接来自 [src/pool.rs:L421](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L421) 的 `self.emitted_blocks = self.seq.assigned_blocks();`。

**待本地验证**：断言的具体数值（尤其 `first.len() == 3` 与首块 `parent_sequence_hash == None`）是依据源码与既有测试行为的推断，请以本地实际运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `take_newly_registered_blocks` 以 `assigned_blocks()` 为准，而不是「已封 token 块数」？

**参考答案**：源码注释：「A block becomes reusable by other requests once it is *registered* (assigned an `ImmutableBlock`), which lags sealing the token block by a step.」路由器关心的是「这个块现在能被别的请求复用吗」，只有注册（拿到 `ImmutableBlock`）之后答案才是肯定的。若按已封块上报，路由器可能把尚不可复用的块插进前缀树，把请求派过来却发现匹配失败。

**练习 2**：`emitted_blocks` 光标跳过前缀命中块的前提是什么？如果前提被打破会发生什么？

**参考答案**：前提是「GPU 命中块从未被逐出、其 store 事件已由首次封存者上报且仍然有效」——即 KV offload 关闭（注释原文：「this assumes GPU-resident reuse, i.e. KV offload off, which holds wherever the event feed is enabled」）。若同开 offload：块可能被逐出到 CPU 层、其 store 事件的树节点已被 Remove 摘除，之后从 CPU 恢复回 GPU 再被新请求命中时，光标直接跳过、无人重新上报——路由树里永久缺失这段前缀。这就是两个 opt-in 世界目前互斥的原因。

**练习 3**：`RegisteredBlock::plh` 为什么只在 4.4 要讲的「Remove 反查」里用到，路由树本身不用它做键？

**参考答案**：路由树（dynamo 生态）的插入/查询全部使用 u64 哈希族，与路由器自行重算的值 by construction 一致；而逐出事件来自引擎侧的 kvbm 机制，只携带 128 位 lineage 哈希。`plh` 的唯一职责就是充当两套体系间的**关联键**（注释原文：「kept only to correlate an eviction event (which carries the lineage hash) back to `sequence_hash`」）——消费者存一张 plh→sequence_hash 的表，Remove 到来时查表换算。

---

### 4.4 事件消费契约与「丢事件即失步」风险模型

#### 4.4.1 概念说明

把 4.1–4.3 拼起来，一个缓存感知路由的消费者必须遵守一套相当反直觉的契约。`with_events` 的文档注释用一句话预告了全部三条：

> ```rust
> /// The stream carries `Create` events too
> /// (the emission policy can't separate them) — the consumer ignores `Create`
> /// and sources richer store events from the seal site, and maps the lineage
> /// hash in each `Remove` back to the router's `sequence_hash`.
> ```

1. **忽略 `Create`**：通道里必然混着 Create（默认 AllEventsPolicy 无法单独关掉 Create），但它的信息量太穷——只有一个 lineage 哈希，没有路由需要的 u64 键和父链接。
2. **store 事件在封块点自产**：即 4.3 的 `take_newly_registered_blocks`——调用方在每个 apply 之后取增量，转成信息完整的 store 事件喂给路由器。
3. **`Remove` 反查**：Remove 携带 lineage 哈希，消费者必须经 `plh → sequence_hash` 映射表换算出路由树要摘除的节点键。

为什么不一改了之——让 `KvCacheEvent` 直接携带更丰富的信息？因为那是上游 kvbm-logical 的公共协议（还要 Serialize 跨 worker 上线，见 [protocol.rs:L28-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/protocol.rs#L28-L34) 的 serde 派生）。本 crate 选择在消费端补齐语义，不动上游协议——这是一个典型的「适配在边界」取舍。

#### 4.4.2 核心流程

一个调度循环内嵌事件消费者的伪代码（示例代码，展示契约而非真实实现——截至当前 HEAD，workspace 内尚无此消费者）：

```text
每个引擎步 (step):
  ── 推理路径（主线） ─────────────────────────────
  schedule → view → forward → apply
  for rb in request.take_newly_registered_blocks():   # 封块点自产 store
      router_tree.insert(rb.sequence_hash, rb.tokens_hash, rb.parent_sequence_hash)
      plh_map[rb.plh] = rb.sequence_hash              # 登记反查表

  ── 旁路排水（路由器侧，同一线程或专职线程） ───────
  loop:
      match rx.try_recv():
          Ok(Create(_))        → 忽略（信息穷，见契约 1）
          Ok(Remove(lineage))  → plh_map 查 sequence_hash → router_tree.remove(...)
          Err(Empty)           → break               # 本步排空
          Err(Lagged(n))       → 前缀树已失步！
                                 # n 个事件被丢，无补发协议；
                                 # 只能重建整棵树（或告警 + 降级为非感知路由）
```

风险模型的形式化总结：

- 事件量与消费速率满足 \( E_{\text{step}} \le C \) 时树保持同步；越界即 `Lagged`。
- `Lagged` 之后**没有对账协议**——后续事件不会修正被跳过的历史，失步是永久的。
- 因此防线全在事前：容量宽裕（\( C = 65\,536 \)，上游默认的 64 倍）+ 每步排水（调度器天然提供节拍）+ 默认关闭（没有路由器的部署根本不暴露在这个风险面里）。

#### 4.4.3 源码精读

- 契约的权威出处是 [src/pool.rs:L42-L49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L42-L49) 的整段文档注释（上文已引）。「the emission policy can't separate them」指上游的 `EventEmissionPolicy` 只能整体开关（[events/manager.rs:L177-L179](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L177-L179)：`if !self.policy.should_emit(seq_hash) { return Ok(()); }`——策略只决定「这个块发不发事件」，不区分 Create/Remove），所以 Create 一定会流进来，只能由消费者忽略。

- Remove 的产生机制再确认一遍（它决定了 Remove 的时序语义）：

  - [kvbm/kvbm-logical/src/events/manager.rs:L173-L189](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L173-L189)：注册时 `on_block_registered` 发 `Create` 并把 `EventReleaseHandle` 挂到注册句柄。
  - [kvbm/kvbm-logical/src/events/protocol.rs:L85-L91](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/protocol.rs#L85-L91)：

    ```rust
    impl Drop for EventReleaseHandle {
        fn drop(&mut self) {
            let event = KvCacheEvent::Remove(self.seq_hash);
            // Broadcast send only fails if there are no receivers, which is fine
            let _ = self.event_tx.send(event);
        }
    }
    ```

    块的最后一个引用消失（活跃请求全部释放且被 LRU 逐出、或 `evict_inactive` 显式冲刷）时，附件 Drop → Remove 入通道。发送失败（无接收者）被显式忽略——这也解释了为什么 Receiver 必须活得和引擎一样久：中途 drop 再重建接收器，期间的 Remove 全部蒸发。

- 排水接口的选择在 [kvbm/kvbm-logical/src/events/manager.rs:L153-L159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L153-L159)：`subscribe_receiver` 的文档明说服务「drain synchronously (e.g. `try_recv` from a non-async scheduler thread)」且「must observe `Lagged` rather than have it silently filtered」——与 4.1.5 练习 1 呼应。

- 「默认关闭」的成本论证出处：[src/pool.rs:L47-L49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L47-L49)（「pays neither the per-block event attachment nor the channel」）。普通 `new` 的裸注册表构造在 [src/pool.rs:L38-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L38-L40)。

#### 4.4.4 代码实践

**实践目标**：沿源码走通一个 Remove 事件的完整旅程，并验证你对时序的理解。

**操作步骤**：

1. 从 `Remove(SequenceHash)` 出发反向追踪：读 [kvbm/kvbm-logical/src/events/protocol.rs:L64-L91](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/protocol.rs#L64-L91)，画出「块被 LRU 逐出 / `evict_inactive` 冲刷 → 注册句柄最后一个引用 Drop → 附件 Drop → Remove 入通道」的时序链。
2. 在纸上（或笔记里）为下面每个场景标注「会产生什么事件、几个」：
   - (a) 请求 A 封存 3 块后 `release`（块进非活跃池，未逐出）；
   - (b) 随后 `pool.evict_inactive()`；
   - (c) 请求 B 命中这 3 块前缀（非活跃池复活）。
3. 把 4.4.2 的伪代码消费者补全成带错误处理的 Rust 骨架（写在你的笔记里，不需要编译）：重点处理 `Err(RecvError::Lagged(n))` 分支——打印 n 并把路由树标记为不可信。
4. 对照 [src/pool.rs:L688-L695](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L688-L695) 的方法文档，检查你的消费者在「本拍无新块」（返回空 Vec）时是否做了正确的事（答案：什么都不做，光标不动）。

**需要观察的现象**：步骤 2 的标注应能区分「release ≠ 事件」与「逐出 = Remove」——release 只是把块从活跃移入非活跃池（引用仍在），此时不产生 Remove；只有最后一个引用消失（逐出）才产生。

**预期结果**：(a) 产生 3 个 store 事件（封块点自产，非通道）+ 0 个通道事件；(b) 产生 3 个 `Remove`。(c) 0 个事件——命中复用不上报（4.3 的光标语义）。

**待本地验证**：场景 (a) 中 release 是否确实不触发 `EventReleaseHandle` 的 Drop，取决于非活跃池对注册句柄的持有方式；如需实证，可在 4.3.4 测试的基础上追加 `evict_inactive()` 前后各 `try_recv` 一次观察通道内容（需要用 `BlockPool::with_events`，其为 `pub(crate)`，故此验证只能在 crate 内部的单测里做——可在 `src/pool.rs` 的 `#[cfg(test)]` 模块中仿照 [src/pool.rs:L741-L744](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L741-L744) 补一个内联测试；这是练习性质的建议，请勿提交对源码的修改）。

#### 4.4.5 小练习与答案

**练习 1**：消费者为什么必须忽略 `Create` 却不能忽略 `Remove`？

**参考答案**：两者信息与职责不对称。`Create` 只带 lineage 哈希，无法直接构造路由树节点（缺 u64 键和父链接），而封块点的 `take_newly_registered_blocks` 能产出完整四元组——store 语义有更好的来源，Create 是「发射策略关不掉的噪声」。`Remove` 则没有替代来源：逐出发生在池内部深处（LRU 分配时的隐性弹最旧、`evict_inactive` 冲刷），只有 RAII 附件能在那个时刻发出信号——摘除语义只能靠它。

**练习 2**：假设消费者线程每 10 步才排一次水（而不是每步），风险模型里的哪个不等式被收紧了？最坏后果是什么？

**参考答案**：安全条件从 \( E_{\text{step}} \le C \) 变成 \( E_{10\,\text{step}} \le C \)——需要缓冲的事件量放大了约 10 倍，余量从数十倍缩到几倍，突发场景（冷冲刷 + 大 prefill 叠加）就可能越界触发 `Lagged`。最坏后果是前缀树永久失步且无告警（除非消费者显式检查 Lagged）：路由器持续基于幻觉调度。这正是常量注释强调「the scheduler drains it every step」的原因——每步排水把暴露窗口压到最小。

**练习 3**：一个部署里既想开 KV offload（u3-l1）又想开事件流做路由，行不行？

**参考答案**：以当前实现不行，至少不能天真地组合。4.3 讲过光标的「GPU-resident reuse」假设：事件流的世界里，命中块被默认为「从未离开 GPU、store 事件仍有效」；offload 的世界里块会经 CPU 层往返，逐出/恢复的 Remove/重注册节奏会与路由树脱节（恢复路径 `commit_loaded_blocks` 也不经过 `RequestKv` 的光标）。源码用注释显式记录了这个边界（「which holds wherever the event feed is enabled」）——两个 opt-in 各服务一类部署，交界处的语义是留给未来的工作。

## 5. 综合实践

**任务：为 KV 事件流写一份一页纸设计说明（design note）。**

你是本 crate 的维护者，需要向新加入的工程师解释「KV 事件流为什么长这样」。请基于本讲精读的源码，写一份 `u3-l2-design-note.md` 存到你的学习笔记里，**必须引用注释原文佐证**，覆盖以下五节：

1. **容量**：为什么 65_536？引用 [src/pool.rs:L20-L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L20-L24) 的注释原文（"Generous… a dropped event silently desyncs the router's prefix tree"），给出你自己的 \( E_{\text{step}} \le C \) 数量级估算，并对比上游默认 1024（[events/manager.rs:L44-L58](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/events/manager.rs#L44-L58)）。
2. **默认关闭**：引用 [src/pool.rs:L47-L49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L47-L49)（"pays neither the per-block event attachment nor the channel"），列出两笔成本（每块附件 + 通道分配），说明为什么单机 serving 不该付。
3. **消费契约**：引用 [src/pool.rs:L44-L48](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L44-L48)，解释三件事——忽略 Create、封块点自产 store（`take_newly_registered_blocks`，为什么信息更丰富）、Remove 经 `plh → sequence_hash` 反查（[src/pool.rs:L721-L726](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L721-L726)）。
4. **公共入口**：说明外部使用者必须经 `KvCacheManager::new_with_events`（[src/manager.rs:L83-L103](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L83-L103)）拿 Receiver，`BlockPool::with_events` 是 `pub(crate)` 仅供 crate 内部（manager 门面）复用；类型同一性由 [src/lib.rs:L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L9) 的再导出保证。
5. **已知边界**：事件流与 KV offload 互斥的假设（[src/pool.rs:L381-L387](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L381-L387)）、无 in-tree 消费者的现状、`Lagged` 不可恢复的风险。

写完后自查：每一条论断是否都能落到一个可点击的源码行？如果有任何一条只有你的推理没有源码出处，标注「推断」。

**可选加码**：把 4.3.4 的 `tests/events_cursor.rs` 真正跑起来（`cargo test --release -p pegainfer-kv-cache --test events_cursor`，纯逻辑、无 GPU），把实际输出贴进设计说明的附录。

## 6. 本讲小结

- **通道构造**：`KvCacheManager::new_with_events`（唯一公共入口）→ `pub(crate)` 的 `BlockPool::with_events` → 上游 `EventsManager`（容量 65_536 = 上游默认 1024 的 64 倍）+ 挂钩的 `BlockRegistry` + 建池前订阅的裸 `broadcast::Receiver`；普通 `new` 走裸注册表，零事件开销。
- **风险模型**：tokio broadcast 的 `Lagged` 无补发、路由前缀树无对账协议 ⇒ 丢一个事件 = 永久失步；防线是容量宽裕 + 调度器每步排水 + 默认关闭三件套。
- **事件协议**：`KvCacheEvent = Create(SequenceHash) | Remove(SequenceHash)`，都只携带 128 位 lineage 哈希；Remove 由块注册句柄上的 `EventReleaseHandle` 在最后一个引用 Drop 时 RAII 发出。
- **封块点自产 store**：`RequestKv::emitted_blocks` 光标（构造 0；前缀命中后跳到 `assigned_blocks()`，前提是 KV offload 关闭）+ `take_newly_registered_blocks()` 增量产出 `RegisteredBlock` 四元组。
- **两套哈希体系**：`plh`（u128 lineage）只用于把逐出事件反查回路由侧键；`sequence_hash` / `tokens_hash` / `parent_sequence_hash`（u64）与路由器重算值 by construction 一致，直接作前缀树的键与链接。
- **消费契约**：忽略 Create（信息穷且关不掉）、store 在封块点自产（信息全）、Remove 查 plh 映射表摘树；截至当前 HEAD 无 in-tree 消费者，这是一条为分布式缓存感知路由预留的 opt-in 能力。

## 7. 下一步学习建议

- **下一讲（u3-l3）**：测试分层、性能回归与机制演进史——用 `git log` 把本讲这类机制（前缀缓存 #216、事件与公共面收缩等里程碑）按时间线串起来，理解每个 API 为什么在那个时点出现。
- **顺着本讲的线索往下读**：上游 `kvbm/kvbm-logical/src/events/` 目录还有 `policy.rs`（发射策略，如 PowerOfTwoPolicy 只在 2 的幂位置发事件——批量协议 `KvCacheEvents` 注释里提到的基数树高效插入/摘除就是为它设计的）和 `publisher.rs`（带 instance 上下文的线上格式 `KvbmCacheEvents`），这是事件走向真正分布式（多 worker hub）的下一层。
- **对照阅读**：u3-l1 的 `sequence_hash_bytes`（offload 内容键）与本讲的 `plh`——同一个 lineage 哈希的两种出 crate 方式，体会「一个身份、多个消费方向」的接口设计。
