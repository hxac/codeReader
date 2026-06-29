# 全局单例 Udt 与 socket/mux 注册表

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 tokio-udt 为什么需要一个**进程级全局单例** `Udt`，以及它是用什么同步原语实现的（`OnceCell<RwLock<Udt>>`）。
- 复述 `Udt` 内部三张核心注册表——`sockets`、`multiplexers`、`peers`——各自存什么、键是什么、值是什么。
- 跟踪一次 `bind → accept → connect` 的完整握手过程中，这三张表是如何被读写的。
- 理解后台 `cleanup_worker` 为什么每秒跑一次 `garbage_collect_sockets`，以及它对 `Broken` 与 `Closing` 状态的 socket 分别做什么。

本讲是整个进阶单元（u3）的地基：后续讲 UdtSocket 状态机（u3-l2）、Multiplexer（u3-l3）都会反复回到这里的「全局注册表 + 单例锁」模型。

## 2. 前置知识

- **单例模式（Singleton）**：保证一个类型在整个进程里只有一个实例。tokio-udt 用它来集中管理「当前进程里所有的 UDT socket 和所有共享的 UDP 多路复用器」。
- **`OnceCell<T>`**：Rust 里「只能被初始化一次」的容器，适合实现线程安全的延迟初始化单例。`get_or_init(|| …)` 保证闭包只执行一次。
- **`RwLock<T>`（读写锁）**：允许多个读者同时持锁，但写者独占。tokio-udt 用的是 `tokio::sync::RwLock`，它的锁是 `.await` 友好的（持锁期间可以跨 `.await` 挂起）。
- **`Arc<T>` / `Weak<T>`**：原子引用计数与弱引用。socket 在注册表里以 `Arc<UdtSocket>`（别名 `SocketRef`）保存，multiplexer 用 `Arc<UdtMultiplexer>` 保存；socket 反向持有 mux 的 `Weak`，避免循环引用。
- **`BTreeMap` / `BTreeSet`**：基于平衡树的有序容器，tokio-udt 用它们做注册表，键天然有序、查找/插入/删除都是 \(O(\log n)\)。
- **UDT 的 socket id 与 multiplexer**：每个 UDT 逻辑 socket 有一个 32 位 id；一个 multiplexer 持有一个真实 UDP socket，可同时为多个逻辑 socket 收发数据（详见 u3-l3）。

如果你对其中某个概念完全陌生，本讲会用最少的篇幅结合源码再解释一次。

## 3. 本讲源码地图

本讲主要围绕 `src/udt.rs`，配合几处调用点说明注册表是被谁修改的。

| 文件 | 作用 |
| --- | --- |
| [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) | 定义全局单例 `Udt`、三张注册表、`new_socket`/`bind`/`new_connection`/`update_mux`/`garbage_collect_sockets` 等核心方法。本讲的主角。 |
| [src/lib.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) | crate 根，`mod udt;` 把本模块挂进模块树（u1-l3 已讲）。 |
| [src/listener.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs) | 服务端入口。`bind`/`accept` 在这里调用 `Udt::get()` 读写注册表。 |
| [src/connection.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs) | 客户端入口。`connect` 在这里调用 `Udt::get()`。 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | UdtSocket 实现。握手路由 `process_handshake` 与客户端 `connect` 在这里调用 `new_connection` / `update_mux`。 |
| [src/multiplexer.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs) | `UdtMultiplexer::run` 在 `update_mux` 里被启动，是 multiplexers 注册表里每项的「生命周期管理者」。 |

## 4. 核心概念与源码讲解

### 4.1 Udt 单例：进程级的「总账本」

#### 4.1.1 概念说明

UDT 的原始 C++ 实现里有一个全局的 `CUDTUnited` 类，负责管理本进程内所有 UDT socket。tokio-udt 把它复刻为 Rust 的 `Udt` 结构体，并用**进程级单例**来承载它。

为什么必须是单例？因为：

- 多个 UDT socket 可能**共享同一个 UDP socket**（多路复用），所以必须有一个地方知道「当前进程有哪些 mux、它们分别绑在哪个端口」。
- 收包 worker 收到一个 UDP 数据报后，要根据里面的目标 socket id 找到对应的逻辑 socket，这也需要一个全局查找表。
- 客户端和服务端可能在一台机器上甚至一个进程里同时存在，必须统一编号、统一回收。

因此 `Udt` 就是 tokio-udt 的「总账本」：所有 socket、所有 mux、所有对端连接都登记在它这里。

#### 4.1.2 核心流程

单例的获取与初始化流程：

1. 任何代码调用 `Udt::get()`。
2. `OnceCell::get_or_init` 检查 `UDT_INSTANCE` 是否已初始化：
   - 若**未**初始化，执行闭包：先 `tokio::spawn` 一个后台 `cleanup_worker` 任务，再用 `Udt::new()` 创建实例并放进 `RwLock`。
   - 若已初始化，直接返回已有引用。
3. `Udt::new()` 把 `next_socket_id` 初始化为一个**随机值**，其余字段走 `Default`（三张表都是空的）。
4. 返回 `&'static RwLock<Udt>`——一个活到进程结束的静态引用。
5. 调用方根据需要 `.read().await`（只读）或 `.write().await`（独占写）来访问账本。

一个关键细节：`cleanup_worker` 是在**初始化闭包里**通过 `tokio::spawn` 调度的，它**不会**在闭包内同步执行；而它内部又调用 `Self::get()`——此时 `UDT_INSTANCE` 已经在初始化中。好在 `tokio::spawn` 只是把 future 排队，等 future 真正被 poll 时 `get()` 早已完成初始化，因此**不会形成递归死锁**。这是一个值得品味的设计点。

#### 4.1.3 源码精读

单例的静态变量与类型别名：

[udt.rs:15-19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L15-L19) — `SocketRef` 是 `Arc<UdtSocket>` 的别名，注册表里存的就是它；`UDT_INSTANCE` 是 `OnceCell<RwLock<Udt>>`，即「一次初始化 + 读写锁」的单例载体。`UDT_DEBUG` 是读环境变量的调试开关。

`Udt::get()` 的实现：

[udt.rs:37-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L37-L42) — 这里是单例的入口。`get_or_init` 的闭包里先 `Udt::cleanup_worker()`（启动后台 GC），再构造实例。注意返回类型是 `&'static`，调用者拿到的是一把「指向全局读写锁」的引用，而不是 `Udt` 本身。

`Udt::new()`：

[udt.rs:30-35](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L30-L35) — `next_socket_id` 从一个随机值起步，三张表默认为空（`..Default::default()`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Udt::get()` 只初始化一次，且会启动后台 GC worker。

**操作步骤**（源码阅读型实践）：

1. 在 [udt.rs:37-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L37-L42) 的 `get_or_init` 闭包里，理清「`cleanup_worker()` → `tokio::spawn` → 内部再次 `Self::get()`」的调用时序。
2. 搜索全仓库对 `Udt::get()` 的调用（见 4.2.3 的调用点清单），统计有多少处是 `.read()`、多少处是 `.write()`。
3. （可选，在自己 fork 的本地副本里）在 `Udt::get()` 的 `get_or_init` 闭包开头加一行 `eprintln!("[udt] init once");`，然后跑一次 `cargo run --bin udt_receiver`，观察这行日志只打印一次。

**需要观察的现象**：

- `get()` 被调用很多次，但初始化闭包只执行一次。
- 后台 GC 任务在进程启动后就开始空转（即便没有 socket，它每秒也会跑一次 `garbage_collect_sockets`，只是空操作）。

**预期结果**：日志只出现一次，证明 `OnceCell` 的「一次初始化」语义成立。**待本地验证**：第 3 步的日志行为需你实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Udt::get()` 返回的是 `&'static RwLock<Udt>`，而不是直接返回 `&'static Udt`？

**参考答案**：因为 `Udt` 内部三张表会被并发读写（收包 worker、发送 worker、用户线程都在访问）。必须套一层锁来串行化访问；返回读写锁本身，让调用者按需取读锁或写锁，既能保证安全，又允许「多读单写」的并发度。直接返回 `&Udt` 会丧失这层同步。

**练习 2**：`cleanup_worker()` 里调用了 `Self::get()`，而 `Self::get()` 又会调用 `cleanup_worker()`。这为什么不是无限递归？

**参考答案**：`cleanup_worker()` 只做了 `tokio::spawn(…)`，把异步任务**排队**后立即返回，并没有在当前调用栈里执行该任务。`get_or_init` 的闭包因此能顺利完成、初始化 `UDT_INSTANCE`。等调度器真正 poll 那个 future 时，`Self::get()` 命中的是已初始化的 cell，不会再走 `get_or_init` 闭包，于是不会再次 spawn worker。

---

### 4.2 sockets / multiplexers / peers：三张注册表

#### 4.2.1 概念说明

`Udt` 这个「总账本」里实际只有三张表（外加一个 id 计数器）：

| 字段 | 类型 | 键 → 值 | 含义 |
| --- | --- | --- | --- |
| `sockets` | `BTreeMap<SocketId, SocketRef>` | 本地 socket id → `Arc<UdtSocket>` | 本进程所有逻辑 socket 的登记簿，是收包分发、accept 取连接的查找依据。 |
| `multiplexers` | `BTreeMap<MultiplexerId, Arc<UdtMultiplexer>>` | mux id → `Arc<UdtMultiplexer>` | 本进程所有共享 UDP 多路复用器的登记簿，决定能否复用已有端口。 |
| `peers` | `BTreeMap<(SocketId, SeqNumber), BTreeSet<SocketId>>` | (对端 socket id, 对端初始序列号) → 本地 socket id 集合 | 服务端用于「同一个对端的重复握手」去重，避免对端重传握手时重复建连。 |
| `next_socket_id` | `SocketId`（u32） | — | 下一个可用的本地 socket id，**递减**分配。 |

注意 `peers` 的键是一个**二元组** `(SocketId, SeqNumber)`：对端的 socket id 加上对端这次连接的初始序列号。仅用对端 socket id 还不够，因为同一个对端可能先后建立多条连接；加上初始序列号才能精确区分「这是不是同一次连接的握手重传」。

#### 4.2.2 核心流程

围绕三张表的关键操作：

1. **分配 socket id**：`get_new_socket_id()` 取当前 `next_socket_id`，然后用 `wrapping_sub(1)` 让计数器**减 1**（在 32 位空间里回绕）。这种「递减」是对原版 UDT C++ 行为的沿用，便于调试时和参考实现对照。
2. **创建 socket**（`new_socket`）：用新 id 构造 `UdtSocket`，尝试用 `Entry::Vacant` 插入 `sockets`；极小概率 id 冲突时返回 `AlreadyExists` 错误。
3. **查找 socket**（`get_socket`）：从 `sockets` 取，但若该 socket 已是 `Closed` 状态则返回 `None`——即注册表里的「逻辑存活」判定。
4. **创建/复用 mux**（`update_mux`，见 4.2.3）：若配置允许 `reuse_mux` 且存在端口与 mss 都匹配的可复用 mux，则复用；否则新建 mux、插入 `multiplexers`、启动它的两个 worker。
5. **建连登记**（`new_connection`）：服务端收到握手后，先查 `peers` 去重；否则新建 socket，写入 `peers`、`sockets`，并塞进 listener 的 `queued_sockets`、`accept_notify.notify_one()` 唤醒 accept。

#### 4.2.3 源码精读

三张表的字段定义：

[udt.rs:21-27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L21-L27) — `Udt` 结构体本体。注意 `peers` 那行注释「peer socket id -> local socket id」点明了它的去重用途。

socket id 递减分配：

[udt.rs:44-48](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L44-L48) — `wrapping_sub(1)` 保证 32 位回绕不会 panic，分配顺序是「随机起点 → 递减」。

`new_socket`：构造并登记到 `sockets`：

[udt.rs:78-92](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L78-L92) — 用 `Entry::Vacant` 保证 id 唯一，插入后返回 `&SocketRef`（调用方一般再 `.clone()` 拿到自己的 `Arc`）。

`get_socket`：带存活判定的查找：

[udt.rs:50-57](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L50-L57) — 即便 id 在表里，状态为 `Closed` 也视作不存在。收包/发送 worker 正是靠它找到目标 socket 的。

`update_mux`：复用或新建 multiplexer，并登记到 `multiplexers`：

[udt.rs:191-225](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L191-L225) — 复用条件是 `reuse_mux && mux.reusable && mux.port == port && mux.mss == socket_mss`（且 `bind_addr` 提供了非零端口）。否则新建 mux，`self.multiplexers.insert(mux_id, mux.clone())`，再 `UdtMultiplexer::run(mux)` 启动收发两个 worker。注意：**典型的客户端 connect 传入 `bind_addr = None`，所以这段复用分支根本不会命中，必定新建 mux。**

`new_connection`：服务端建连，同时写 `peers` 与 `sockets`：

[udt.rs:94-175](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L94-L175) — 先用 `get_peer_socket` 去重（命中且非 Broken 时直接用旧 socket 的配置回握手，避免重复建连）；否则分配新 id、构造新 socket（关键：用 `with_listen_socket` 让新 socket **复用 listener 的 mux**，不新建 UDP socket）、`connect_on_handshake` 回握手，最后 [udt.rs:166-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L166-L173) 把新 socket 同时登记进 `peers`、`sockets`，并 `listener_socket.queued_sockets.insert` + `accept_notify.notify_one()` 唤醒 accept。

`get_peer_socket`：按 (对端 id, isn) + 对端地址精确匹配：

[udt.rs:59-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L59-L76) — 先按 `peers` 的键 `(socket_id, initial_seq_number)` 取候选集合，再逐个比对 `peer_addr()`，确保是同一个网络对端。

**调用点清单**（谁在读写这三张表）：

- 服务端建 socket：[listener.rs:15-18](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L15-L18) `udt.new_socket(...)`，随后 [listener.rs:29-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L29-L32) `udt.bind(socket_id, bind_addr)`（内部走 `update_mux` 写 `multiplexers`）。
- 服务端 accept 取 socket：[listener.rs:78-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L78-L84) `udt.get_socket(accepted_socket_id)`。
- 客户端建 socket：[connection.rs:40-41](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L40-L41) `udt.new_socket(...)`。
- 客户端建 mux：[socket.rs:1111-1114](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1111-L1114) `udt.update_mux(self, bind_addr)`。
- 服务端握手建连：[socket.rs:425-429](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L425-L429) `Udt::get().write().await.new_connection(self, addr, hs)`。
- 收发 worker 找 socket：[queue/rcv_queue.rs:62](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L62) 与 [queue/snd_queue.rs:53](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L53) 都用 `Udt::get().read().await.get_socket(...)`。

#### 4.2.4 代码实践

**实践目标**：用一张表格，复述「服务端 `bind` → 客户端 `connect` → 服务端 `accept`」全过程中，**服务端进程**的 `sockets` / `multiplexers` / `peers` 三张表是如何演变的。

**操作步骤**（源码跟踪型实践）：

1. 起点：进程刚启动，`Udt::new()` 后三张表都是空的。

   | 时刻 | sockets | multiplexers | peers |
   | --- | --- | --- | --- |
   | 初始 | `{}` | `{}` | `{}` |

2. 跟踪 `UdtListener::bind`（[listener.rs:14-46](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L14-L46)）：
   - `new_socket` 写入 `sockets[L]`（L = listener 的 socket id）。
   - `bind → update_mux` 写入 `multiplexers[M]`（M = 新 mux id）。
   - 填表并写下你的推断。

3. 跟踪客户端 `connect`（发生在**另一个进程**，不改变本表；但如果你把 client/server 放在**同一进程**里跑，则会再写入 `sockets[C]` 和一个新的 `multiplexers`，因为客户端 `bind_addr=None` 不会复用 listener 的 mux）。

4. 跟踪服务端收到握手后的 `new_connection`（[udt.rs:94-175](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L94-L175)）：
   - 新建被接受连接 A，写入 `sockets[A]`。
   - 写入 `peers[(对端id, isn)] = {A}`。
   - **不**新增 mux：A 通过 `with_listen_socket` 复用了 listener 的 mux。
   - A 被塞进 `listener.queued_sockets`，`accept_notify.notify_one()`。

5. 跟踪 `accept`（[listener.rs:78-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L78-L84)）：从 `queued_sockets` 取出 A，`get_socket(A)` 返回 `Arc`，包成 `UdtConnection`。三张表**数量不变**（A 仍在 `sockets` 与 `peers` 里）。

**需要观察的现象 / 预期结果**：最终（单条连接、服务端进程）应为：

| 时刻 | sockets | multiplexers | peers |
| --- | --- | --- | --- |
| 初始 | `{}` | `{}` | `{}` |
| bind 后 | `{L}` | `{M}` | `{}` |
| new_connection 后 | `{L, A}` | `{M}`（复用，未新增） | `{(peer_id, isn): {A}}` |
| accept 后 | `{L, A}` | `{M}` | `{(peer_id, isn): {A}}` |

**关键结论**：被 accept 出来的连接 A **共享** listener 的 mux，所以一个 listener 无论接受多少连接，`multiplexers` 通常只有 1 项、对应 1 个 UDP socket。

> 说明：若 `reuse_mux=true` 且端口/mss 匹配，`update_mux` 会复用已有 mux 而不新建。但客户端 `connect` 默认 `bind_addr=None`，复用分支不会命中。**待本地验证**：在不同 `reuse_mux`/`udp_reuse_port` 组合下实际观察 `multiplexers` 数量。

#### 4.2.5 小练习与答案

**练习 1**：`peers` 的键为什么是 `(SocketId, SeqNumber)` 二元组，而不是只用对端的 `SocketId`？

**参考答案**：同一个对端（同一个对端 socket id）可能先后发起多条不同的连接，每条连接有不同的初始序列号（isn）。仅用对端 socket id 无法区分这些连接，会把新连接误判成旧连接的重传。加上 isn 才能精确判定「这是不是同一次连接的握手包」，从而正确去重。

**练习 2**：客户端调用 `UdtConnection::connect` 时，`update_mux` 的复用分支几乎一定不会命中。为什么？

**参考答案**：复用分支要求 `bind_addr` 是 `Some` 且端口 > 0（见 [udt.rs:197-208](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L197-L208)）。而客户端 connect 默认不显式 bind，`bind_addr` 传的是 `None`，于是 `if let Some(bind_addr) = bind_addr` 直接跳过，必定走到「新建 mux」分支，复用一个临时高端口。

**练习 3**：`new_socket` 用 `Entry::Vacant` 检查 id 是否已存在。既然 id 是随机起步再递减，冲突概率极低，这段检查有什么意义？

**参考答案**：32 位空间有限，进程长期运行（或刻意构造）后 `wrapping_sub` 会回绕，理论上有可能撞上仍在用的 id。`Entry::Vacant` 把这种极端情况变成一个明确的 `AlreadyExists` 错误，而不是静默覆盖一个正在用的 socket——这是防御性编程，保证账本完整性。

---

### 4.3 garbage_collect_sockets：后台 GC 与状态收尾

#### 4.3.1 概念说明

UDT 的连接关闭是**异步的**：用户调用 `close()` 后，socket 不会立刻从注册表消失，它要先把发送缓冲排空、发出 Shutdown 包、等待收尾。这意味着 `sockets` 表里会短暂出现「正在关闭」「已经损坏」的 socket。如果没人定期清扫，这些 socket 会一直占着 `Arc`、占着内存。

`garbage_collect_sockets` 就是那个「清扫工」，由后台 `cleanup_worker` **每秒**调用一次，分两种情况处理：

- `Broken`（连接已损坏，比如对端长时间无响应）：从 listener 的 `queued_sockets` 里摘掉（如果它还没被 accept），然后 `spawn` 一个任务异步执行 `sock.close()`，让它进入正式关闭流程。
- `Closing`（用户已调 close、收尾基本完成）：直接从 `sockets` 表移除，并把状态置为 `Closed`，完成最终回收。

#### 4.3.2 核心流程

GC 的两阶段逻辑（伪代码）：

```
garbage_collect_sockets():
    for sock in sockets 里所有 status == Broken 的:
        if sock.listen_socket 存在:
            把 sock.socket_id 从该 listener 的 queued_sockets 移除
        spawn: sock.close().await        # 异步触发正式关闭

    to_remove = sockets 里所有 status == Closing 的 id
    for id in to_remove:
        sockets.remove(id)               # 真正摘除
        sock.status = Closed             # 置最终态
```

驱动它的 `cleanup_worker`：

```
cleanup_worker():
    spawn:
        udt = Self::get()
        loop:
            udt.write().await.garbage_collect_sockets().await
            sleep(1 秒).await
```

值得注意：`peers` 表**没有**在 GC 里被显式清理。这是一个有意的设计权衡——`peers` 以 `(对端 id, isn)` 为键，而每条新连接的 isn 几乎不会和旧连接重复，所以旧条目即使留下也不会造成误命中；它可能逐渐累积，但在正常负载下增长很慢。（如果你想严格，可以把这视为一个潜在的小内存泄漏点，留作思考。）

#### 4.3.3 源码精读

`garbage_collect_sockets` 主体：

[udt.rs:227-259](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L227-L259) — 上半段处理 `Broken`：先尝试从所属 listener 的 `queued_sockets` 里移除（避免 accept 取到一个已坏的连接），再用 `tokio::spawn` 异步 `close()`，**不阻塞** GC 主循环。下半段处理 `Closing`：收集所有 `Closing` 的 id，从 `sockets` 移除并把状态写成 `Closed`。

`cleanup_worker`：每秒一次的节奏：

[udt.rs:261-269](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L261-L269) — 在 `Udt::get()` 初始化时被 spawn 一次，之后无限循环：取写锁 → GC → `sleep(1s)`。1 秒的粒度是「及时回收」与「不要频繁抢全局写锁」之间的折中。

为什么 Broken 要 `spawn(close)` 而 Closing 直接摘除？因为 `close()` 本身是 async 的（要发 Shutdown 包、等 linger），GC 持有的是全局 `Udt` 写锁，**不能**在持锁期间长时间 `.await`；所以把真正的关闭工作丢到一个独立任务里，GC 自己只做表项维护。这是「持锁时间最小化」的典型手法。

#### 4.3.4 代码实践

**实践目标**：理解 GC 的两阶段回收，并验证 `Broken` 连接不会卡住 accept。

**操作步骤**（源码阅读 + 思考型实践）：

1. 阅读 [udt.rs:227-259](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L227-L259)，分别标出处理 `Broken` 与 `Closing` 的两段代码。
2. 思考：一个被 `new_connection` 创建、已塞进 `queued_sockets` 但**尚未被 accept** 的连接，如果对端突然断网导致它变成 `Broken`，会发生什么？答：GC 会把它从 `queued_sockets` 摘掉（[udt.rs:233-241](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L233-L241)），于是它永远不会被 `accept` 取到——这正是 u2-l2 里提到的「accept 取到已 Closed 连接会报错」竞态的防护网之一。
3. （可选，本地副本）在 `garbage_collect_sockets` 开头加 `eprintln!("[gc] broken={} closing={}", …)` 统计两类数量，跑 `udt_sender`/`udt_receiver` 后强制断开一端，观察 GC 日志。

**需要观察的现象**：

- 正常收发时 GC 每秒空跑（两类都是 0）。
- 一端异常退出后，另一端会在若干秒内把对应 socket 从 `Broken` 走向关闭、最终从 `sockets` 移除。

**预期结果**：异常断开后，`sockets` 表项数会回落。**待本地验证**：具体几秒回落取决于 EXP 定时器何时判定 Broken（见 u7-l3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么处理 `Broken` 时要用 `tokio::spawn` 去执行 `close()`，而不是直接在 GC 里 `sock.close().await`？

**参考答案**：`close()` 是 async 的，可能涉及发 Shutdown 包、等 linger、跨多个 `.await`。而 `garbage_collect_sockets` 是在持有**全局 `Udt` 写锁**的情况下被调用的（见 [udt.rs:265](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L265)）。如果在持锁期间长时间 await close，会阻塞整进程所有 socket 的注册表访问。把 close 丢到独立任务，GC 自己只做表项维护，把全局写锁的持有时间压到最短。

**练习 2**：`Closing` 状态的 socket 在 GC 里被置为 `Closed` 并从 `sockets` 移除。`get_socket` 对 `Closed` 返回 `None`。这两件事配合起来保证了什么？

**参考答案**：保证一个已经走完关闭流程的 socket 不会再被任何 worker 或用户代码通过注册表访问到——既从表里物理消失，又在状态上逻辑失效（即便还有人持有旧的 `Arc<UdtSocket>`，`get_socket` 也不会再把它交出去）。这是「物理删除 + 逻辑失效」的双重保险。

**练习 3**：`peers` 表在 GC 里没有被清理。这在什么情况下会成为问题？

**参考答案**：如果一个进程长期运行、并且持续有新的对端连接进来，`peers` 里残留的旧 `(对端 id, isn)` 条目会逐渐累积（正常情况下每条不会很大，因为同一个对端 id+isn 的集合通常只有一个本地 socket）。虽然不会造成功能错误（新连接 isn 不同，不会误命中），但会缓慢占用内存，是一个潜在的轻微泄漏点；严格的实现可以在对应 socket 被回收时同步清理 `peers` 条目。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「全局账本巡视」：

**任务**：在本地跑一对 `udt_receiver`（服务端）与 `udt_sender`（客户端），在不修改源码的前提下，仅凭源码阅读，回答下面这份「巡视报告」。如果你愿意在本地 fork 里加少量 `eprintln!`，可以用实际日志佐证你的推断（标注哪些是推断、哪些是观察）。

巡视报告需包含：

1. **服务端进程**在以下四个时刻的 `sockets` / `multiplexers` / `peers` 三表快照（用集合表示，元素用代号 L/M/A 等）：
   - (a) `UdtListener::bind` 刚返回；
   - (b) 客户端 `connect` 发出的第一个握手包到达之前；
   - (c) 服务端 `new_connection` 执行完、`accept_notify` 已通知；
   - (d) `accept` 已返回该连接、双方正常收发中。
2. 指出从 (a) 到 (d)，`multiplexers` 数量是否变化、为什么。
3. 假设此时**强行 kill 客户端**，描述服务端在后续几秒内：哪个定时器把连接判为 `Broken`（提示：EXP，见 u7-l3）？GC 在下一次运行时对它做了哪两件事？它最终如何从 `sockets` 消失？
4. 用一句话解释：为什么这一切都能在「用户从未直接接触 `Udt` 单例」的情况下自动完成。

**预期产出**：一份简短的报告，体现你能把「单例初始化 → 注册表读写 → 后台 GC」三个模块连成一条完整的生命周期链路。其中涉及定时器与 Broken 判定的细节若不确定，可标注「依赖 u7-l3，待后续学习确认」。

## 6. 本讲小结

- tokio-udt 用 `OnceCell<RwLock<Udt>>` 实现进程级单例 `UDT_INSTANCE`，`Udt::get()` 返回 `&'static` 读写锁引用；初始化时顺带 `spawn` 一个后台 GC worker，且不会因此递归。
- `Udt` 这个总账本里只有三张表：`sockets`（本地 socket 登记簿）、`multiplexers`（共享 UDP 多路复用器登记簿）、`peers`（按 `(对端 id, isn)` 去重的对端连接表），外加递减分配的 `next_socket_id`。
- 服务端 `bind` 写 `sockets`+`multiplexers`；客户端 `connect` 也写这两张表（但 `bind_addr=None` 时不会复用 mux）；握手 `new_connection` 同时写 `peers`+`sockets`，并复用 listener 的 mux、唤醒 accept。
- `get_socket` 带 `Closed` 存活判定，是收发 worker 与 accept 的查找入口。
- `cleanup_worker` 每秒取写锁跑 `garbage_collect_sockets`：`Broken` 连接被 spawn 异步 `close()`，`Closing` 连接被摘除并置 `Closed`；GC 刻意不在持锁期间长时间 await，以保护全局锁的可用性。
- 用户全程不直接接触 `Udt` 单例——它被 `UdtListener` / `UdtConnection` / 收发 worker 在内部透明地使用。

## 7. 下一步学习建议

- 下一讲 **u3-l2「UdtSocket 核心结构与状态机」**：本讲反复出现的 `UdtSocket`、`UdtStatus`（Init/Opened/Listening/Connecting/Connected/Closing/Closed/Broken）将在那里被完整剖析，你会看清 GC 依赖的那些状态是如何迁移的。
- 之后 **u3-l3「多路复用器 UdtMultiplexer」**：本讲里 `update_mux` 复用/新建的 `UdtMultiplexer`，以及 `UdtMultiplexer::run` 启动的两个 worker，会在那里展开。
- 想提前看「Broken 如何被判定」的读者，可跳读 **u7-l3 定时器**，但要先具备 u3-l2 的状态机基础。
- 建议同时打开 [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) 与 [src/listener.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs) 对照阅读，亲手把本讲 4.2.4 的三表演变表填一遍，这是检验理解最有效的方式。
