# queue 抽象：queue_conn / queue_base / queue<T,Policy>

## 1. 本讲目标

本讲从 `src/libipc/queue.h` 这一个文件出发，讲清 libipc 的「队列抽象层」是怎么搭起来的。学完本讲你应该能够：

1. 说清 `queue_conn`、`queue_base`、`queue` 三层各自负责什么、为什么这样分层。
2. 区分「发送者注册（`ready_sending`）」和「接收者连接（`connect`）」两套截然不同的逻辑。
3. 读懂 `push`/`force_push`/`pop` 如何用回调把「环形队列算法」和「消息类型构造」解耦，并解释 `prep` 回调的作用。
4. 理解 `queue<T, Policy>` 如何用模板把元素类型 `T` 与策略 `Policy` 焊死在一起。

本讲是 U4「无锁循环队列」单元的入口，承接 u3-l1（`ipc.cpp` 全景），并为 u4-l2（`elem_array` 与 `conn_head` 位图）和 u4-l3/u4-l4（`prod_cons` 算法）铺路。本讲只讲「队列怎么把活儿派出去」，不展开底层位图运算和无锁算法本身。

## 2. 前置知识

在进入源码前，先回顾几个本讲会反复用到的概念：

- **共享内存句柄 `shm::handle`**：libipc 跨进程共享一块内存的 RAII 封装。`acquire(name, size)` 创建或打开一块命名共享内存，`get()` 返回其首地址指针，`release()` 释放本进程对它的引用。多个进程用同 `name` 打开，拿到的是同一块物理内存。
- **连接位图 `cc_t`（u2-l4 已讲）**：广播模式下，每个接收者占 `cc_t`（`uint32`）里的 1 个 bit，作为自己的「座位号」。`connect()` 抢最低空闲位、`disconnect()` 归还。32 个座位是上限。
- **座位号 vs 身份证号（u3-l1 已强调）**：`connected_id()`（座位号）是广播读计数用的 bit；`conn_info_head::cc_id_`（身份证号）是单调递增、用于过滤「自己发给自己」的消息。两者不要混淆。
- **PIMPL 与不透明句柄 `handle_t`**：对外只暴露 `void*`，真实类型在 `.cpp` 内部 cast 回来。
- **placement-new `::new (p) T(args...)`**：在已分配好的内存 `p` 上原地构造一个 `T`，不额外分配内存。本讲 `push`/`pop` 的回调里到处都是它。

> 提示：本讲会反复出现「这个成员在本进程的堆上」还是「这块数据在共享内存里」的区分。请在读源码时始终带着这个问题：它到底落在哪一边？

## 3. 本讲源码地图

本讲主要围绕下面几个文件，按「从抽象到具体」的顺序阅读：

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `src/libipc/queue.h` | 三层队列抽象：`queue_conn` / `queue_base` / `queue` | **本讲核心**，全部代码都在这里 |
| `src/libipc/policy.h` | 策略标签 `choose`，把 `Flag` 映射成具体的 `elems_t` | 解释 `queue<T,Policy>` 的 `Policy` 从哪来 |
| `src/libipc/circ/elem_array.h` | `elem_array`：环形数组 + `connect_sender/receiver` + `push/pop` 委托 | queue 层「派出去的活儿」落到这里 |
| `src/libipc/circ/elem_def.h` | `cc_t`、`conn_head` 位图 `connect/disconnect/conn_count` | 解释连接位的来源 |
| `src/libipc/prod_cons.h` | 真正的无锁环形算法 `push/pop` | 解释回调收到的 `void* p` 是什么（u4-l3/u4-l4 详讲） |
| `src/libipc/ipc.cpp` | `queue_generator` 与 `send/recv` 调用链 | 队列的真实使用方 |

## 4. 核心概念与源码讲解

本讲按四个最小模块拆开讲：连接管理、收发角色管理、回调委托、模板封装。

### 4.1 queue_conn：连接位与共享内存句柄

#### 4.1.1 概念说明

`queue_conn` 是三层抽象里最底的一层，它只管两件事：

1. **持有一块共享内存句柄**（`shm::handle elems_h_`），这块共享内存里放着整个环形元素数组 `Elems`。
2. **记录本进程在这条通道里的「座位号」**（`circ::cc_t connected_`）。

注意一个非常容易踩坑的点：`connected_` 是 `queue_conn` 的**普通成员**，落在**本进程自己的对象内存**里，**不在共享内存中**。每个进程的 `queue_conn` 各持有一个 `connected_`，记录「我在共享位图里占了哪个 bit」。共享内存里那个总的连接位图，由 `Elems`（`conn_head`）持有（u4-l2 详讲）。

`queue_conn` 是 `protected` 成员居多的「实现基类」，库外部用不到它，它被 `queue_base` 继承。

#### 4.1.2 核心流程

`queue_conn` 的生命周期可以概括为：

```text
open(name)         acquire 共享内存 -> 拿到 Elems* -> elems->init() 懒初始化
   |
connect(elems)     elems->connect_receiver() 抢座位 -> 本地 connected_ = 座位bit
   |
connected(elems)   查：本进程的 bit 还在共享位图里吗？
   |
disconnect(elems)  elems->disconnect_receiver(connected_) 归还座位 -> connected_ 置 0
   |
close() / clear()  release / 强制释放共享内存句柄
```

`open` 内部的 `elems->init()` 用的是 DCLP（双检锁，u4-l2/u8-l1 详讲），保证多进程第一次映射到同一块共享内存时，那个 `conn_head_base` 只被构造一次。

#### 4.1.3 源码精读

`queue_conn` 的两个核心成员：

[queue.h:26-30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L26-L30) —— `connected_`（本进程座位号）与 `elems_h_`（共享内存句柄）。

`open` 负责打开共享内存并触发懒初始化：

[queue.h:31-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L31-L48) —— `acquire` 指定大小为 `sizeof(Elems)` 的一块共享内存，`static_cast<Elems*>` 后调用 `elems->init()`。

`connect` 抢座位，注意它返回的是一个三元组 `tuple`：

[queue.h:76-85](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L76-L85) —— `<是否已连上, 是否本次新连上, elems->cursor()（起始游标）>`。已连上就直接返回，不重复占座。

`disconnect` 归还座位，用 `std::exchange(connected_, 0)` 在归还的同时把本地 `connected_` 清零：

[queue.h:87-94](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L87-L94)

两个只读访问器：`connected_id()` 返回本进程座位号，`connected(elems)` 查本进程座位是否仍在位：

[queue.h:67-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L67-L74)

资源清理分两档：`clear()` 强制释放依赖的共享内存句柄（`shm::handle::clear`），`clear_storage(name)` 是静态方法、按名字把残留的共享内存对象整个删除。对应 u2-l3 讲过的 `clear` 与 `clear_storage` 区别：

[queue.h:59-65](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L59-L65)

`connect_receiver()` / `disconnect_receiver()` 真正干活的地方在 `elem_array`，广播模式下转发给 `conn_head::connect`（位运算抢座，u2-l4 已讲，u4-l2 详讲）：

[elem_array.h:111-117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L111-L117)

#### 4.1.4 代码实践

**实践目标**：确认 `connected_` 是「本进程本地」的座位号，而非共享内存里的全局计数。

**操作步骤**（源码阅读型）：

1. 在 [queue.h:26-30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L26-L30) 找到 `connected_` 的声明，注意它没有 `std::atomic`、也没有放在某个共享结构里——它就是个普通成员。
2. 追踪 [queue.h:83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L83) `connected_ = elems->connect_receiver();`，跳到 [elem_array.h:111-113](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L111-L113)，再跳到 [elem_def.h:56-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L56-L71) 的 `conn_head::connect()`，看返回值 `next ^ curr`（单个 bit）。
3. 对比共享内存里那个 `std::atomic<cc_t> cc_`（[elem_def.h:28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L28)），它才是所有进程共享的「全座位图」。

**需要观察的现象**：本进程 `connected_` 是单个 bit（如 `1`、`2`、`4`、`8`…），而共享的 `cc_` 是所有进程 bit 的或（如 `0b111` 表示 3 个接收者在线）。

**预期结果**：你能画出「3 个接收者进程 → 各自本地 `connected_ = 1/2/4` → 共享 `cc_ = 7`」的对应关系。

**待本地验证**：若想眼见为实，可在 [queue.h:84](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L84) 临时加一行 `printf` 打印 `connected_`，启动 send_recv 的 recv 进程多次观察值变化（仅用于学习，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connected_` 不需要是 `std::atomic`？
**答案**：它只被本进程的这一个 `queue_conn` 对象读写，不存在跨进程/跨线程并发访问，所以普通成员即可。真正需要原子的是共享内存里所有进程共写的 `cc_`。

**练习 2**：`connect()` 已经连上时为什么直接返回、不重复 `connect_receiver()`？
**答案**：见 [queue.h:82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L82)，已连上时 `connected(elems)` 为真，直接返回，避免重复抢座位、泄漏一个 bit。

---

### 4.2 queue_base：发送/接收角色管理

#### 4.2.1 概念说明

`queue_base<Elems>` 继承 `queue_conn`，在「持有共享内存 + 座位号」之上，再管理两件具体的事：

1. **拿到环形数组的指针 `elems_`**，并维护本接收者的**读游标 `cursor_`**（读到哪了）。
2. **区分本实例是「发送者」还是「接收者」**，并各自提供注册/注销方法。

`queue_base` 仍是库内部用的模板基类，用户直接用的是下一节的 `queue<T,Policy>`。它的三个 protected 成员是：

[queue.h:105-108](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L105-L108) —— `elems_`（环形数组指针）、`cursor_`（本接收者读游标）、`sender_flag_`（本实例是否已注册为发送者）。

#### 4.2.2 核心流程

发送者与接收者这两套逻辑**不对称**，这是本模块最重要的认知：

| 角色 | 注册方法 | 注销方法 | 返回/含义 |
| --- | --- | --- | --- |
| 接收者 | `connect()` | `disconnect()` | 抢一个**唯一座位 bit**（`cc_t`），用于广播读计数；同时拿到起始 `cursor_` |
| 发送者 | `ready_sending()` | `shut_sending()` | 仅一个 **bool 标志**；多生产者恒真，单生产者用原子 flag 抢占 |

为什么不对称？因为在广播模式下，**生产者必须能区分每一个接收者**（才能知道某条消息是否被所有接收者都读过，从而回收槽位），所以接收者需要唯一座位；而**接收者不需要区分生产者**（消息来自谁都一样），所以发送者只需要一个「我在线」的布尔。

发送者注册的惰性设计：`ready_sending` 用「短路或 + 赋值」实现「只注册一次」：

```text
ready_sending() = sender_flag_ || (sender_flag_ = elems_->connect_sender())
                  └─ 已注册则直接真 ─┘   └─ 否则尝试注册并记下结果 ┘
```

读侧的 `empty()` 判空则用游标比较：本接收者已读到写游标位置，即「没有新消息」：

\[
\texttt{empty()} \;=\; \neg\texttt{valid()} \;\lor\; (\texttt{cursor\_} = \texttt{elems\_->cursor()})
\]

#### 4.2.3 源码精读

发送者注册与注销：

[queue.h:144-153](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L144-L153) —— `ready_sending` 惰性注册；`shut_sending` 仅在 `sender_flag_` 为真时注销。

接收者连接，把基类 `queue_conn::connect` 返回的三元组解包，新连上时记下起始游标：

[queue.h:159-166](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L159-L166)

`conn_count()` 转发，返回当前在线接收者数量（`elems_` 为空时返回 `invalid_value` 哨兵）：

[queue.h:172-174](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L172-L174)

判空与有效性：

[queue.h:176-182](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L176-L182)

`connect_sender` / `connect_receiver` 的差异落在 `elem_array` 的两个 checker 上——多生产者恒返回 `true`，单生产者用原子 flag 抢占：

[elem_array.h:103-117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L103-L117)

真实调用方在 `ipc.cpp` 的 `reconnect`：接收者走 `connect()`、发送者走 `ready_sending()`，两者互斥切换（先 `shut_sending` 再 `connect`，反之亦然）：

[ipc.cpp:481-502](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L481-L502)

#### 4.2.4 代码实践

**实践目标**：体会「发送者只占一个 bool、接收者占唯一座位」的不对称设计。

**操作步骤**（源码阅读型）：

1. 读 [queue.h:144-147](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L144-L147) 的 `ready_sending`，确认它返回 `bool`、只记一个 `sender_flag_`。
2. 读 [queue.h:159-166](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L159-L166) 的 `connect`，确认它把座位号存进基类 `connected_`、把起始游标存进 `cursor_`。
3. 跳到 [elem_array.h:48-69](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L48-L69) 看 `sender_checker`：多生产者特化（`true`）的 `connect()` 是 `constexpr static bool` 恒真；单生产者特化（`false`）用 `atomic_flag::test_and_set` 保证只允许一个发送者。

**需要观察的现象**：对 `route`（单生产者），第二个进程想 `ready_sending()` 时，单生产者 checker 的 `test_and_set` 会失败、返回 false；对 `channel`（多生产者），任意数量发送者都能注册成功。

**预期结果**：你能解释「为什么 `route` 只能有一个发送者、`channel` 可以有多个」——答案不在 `queue_base`，而在 `sender_checker` 对 `is_multi_producer` 的特化。

#### 4.2.5 小练习与答案

**练习 1**：`ready_sending()` 里的 `sender_flag_ || (sender_flag_ = ...)` 为什么不会重复注册？
**答案**：C++ 的 `||` 短路求值：`sender_flag_` 一旦为真，右半不再求值，`connect_sender()` 只会被调用一次。

**练习 2**：`conn_count()` 在 `elems_ == nullptr` 时返回 `invalid_value` 而不是 `0`，为什么？
**答案**：见 [queue.h:173](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L173)。「未打开」与「打开了但没人连」语义不同，调用方（如 `ipc.cpp` 的 `recv_count`）需要区分这两种情况，故用哨兵 `invalid_value` 标记「无效」。

---

### 4.3 push / force_push / pop：回调委托机制

#### 4.3.1 概念说明

这是本讲最核心、也最精妙的设计。`queue_base` **自己并不知道**环形队列的算法细节，也**不直接知道**元素类型 `T` 长什么样。它把「把一个消息放进队列」这件事拆成了两层回调，转交给真正持有算法的 `elems_`（`elem_array` → `prod_cons_impl`）：

```text
queue_base::push<T>(prep, args...)        ← 用户层：知道 T、知道 args、知道「是否允许覆盖」
        │
        │  构造一个 lambda: f(p) = if(prep(p)) ::new(p) T(args...)
        ▼
elems_->push(this, f)                     ← 算法层：分配槽位、CAS 推进游标，把槽位指针交给 f
        │
        │  在某个槽位 el 上调用 f(&(el->data_))
        ▼
f 内部：prep(p) 判断 → placement-new 构造 T 进槽位
```

关键点：算法层只负责**算出该写到哪个槽**（索引运算、CAS、内存序），然后把槽位的 `data_` 区域首地址（一个 `void*`）交给回调 `f`；类型相关的**构造**完全交给回调。这样算法层可以做成类型无关的模板（操作 `byte_t data_[]`），而类型安全由 `queue_base` 的模板保证。

而 `prep`（preparation）回调是「用户层」注入的钩子：它收到即将被写入的槽位指针 `p`，返回一个 `bool`——「是否允许在此槽位上构造新消息」。这就引出了 `push` 与 `force_push` 的本质区别：

- **`push`**：正常入队，槽位一定是空的，`prep` 恒为 `[](void*){ return true; }`。
- **`force_push`**：强制入队（队列满时驱逐旧消息腾位），被驱逐的槽位里**可能还有未被读走的旧消息**。若旧消息是大消息（占用外部 chunk 存储），直接覆盖会造成 chunk 引用泄漏，所以 `prep` 必须先清理旧消息（`clear_message`）再放行。

#### 4.3.2 核心流程

三个方法的委托结构完全同构，以 `push` 为例：

```text
queue_base::push<T, F, P...>(F&& prep, P&&... params)
  ├─ if (elems_ == nullptr) return false;          // 未打开
  └─ return elems_->push(this,                     // 委托给算法层
          [&](void* p) {                            // ← 回调 f
              if (prep(p))                          // ← prep 钩子：允许构造吗？
                  ::new (p) T(std::forward<P>(params)...);  // placement-new
          });
```

`pop` 略有不同：算法层把槽位里的 `T` **move 构造**到调用方传入的 `item` 引用里（同样是 placement-new 进 `&item`），并多了一个 `out` 回调用于「消费后回收槽位」（如递减广播读计数，u4-l4 详讲）：

```text
queue_base::pop<T, F>(T& item, F&& out)
  └─ return elems_->pop(this, &cursor_,
          [&item](void* p) { ::new (&item) T(std::move(*static_cast<T*>(p))); },  // move 出来
          std::forward<F>(out));                                                   // 回收钩子
```

注意 `pop` 传的是 `&(this->cursor_)`——本接收者的读游标地址，算法层会边读边推进它。

#### 4.3.3 源码精读

`push` 与 `force_push`——结构同构，都把构造封装进 lambda，区别只在 `prep`：

[queue.h:184-198](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L184-L198) —— 两者都调用 `elems_->push/force_push(this, lambda)`，lambda 内 `if (prep(p)) ::new (p) T(...)`。

`pop`——move 构造到 `item`，额外带 `out` 回收回调：

[queue.h:200-208](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L200-L208)

算法层是如何把槽位指针交给回调的？看 `prod_cons.h` 多对多 `push` 的这一行（`el` 是算出来的槽位，`&(el->data_)` 就是回调收到的 `p`）：

[prod_cons.h:133-134](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L133-L134) —— `auto* el = elems + circ::index_of(cur_ct);` 算出槽位后，下一行 `std::forward<F>(f)(&(el->data_));` 把该槽位数据区首地址交给回调 `f`。算法层只负责选槽，不关心 `T`。

`elem_array` 这一层只是把 `push`/`force_push`/`pop` 再转发给 `head_`（`prod_cons_impl` 策略对象），`block_` 是那 256 个槽位数组：

[elem_array.h:123-137](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L123-L137)

真实使用方在 `ipc.cpp` 的 `send`：正常 `push` 的 `prep` 是 `[](void*){ return true; }`；超时后改用 `force_push`，`prep` 变成 `clear_message`——驱逐旧消息前先释放其外部存储：

[ipc.cpp:591-611](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L591-L611) —— 对比 `push` 的 `[](void*){ return true; }` 与 `force_push` 的 `[info](void* p){ return clear_message<...>(info, p); }`。

`clear_message` 干的事：若被驱逐的旧消息 `storage_` 为真（大消息外部存储），先 `release_storage` 归还 chunk，再返回 `true` 放行覆盖：

[ipc.cpp:363-376](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L363-L376)

`msg_t` 的构造函数正好对应 `push` lambda 里 `::new (p) T(cc_id_, msg_id_, remain, data, size)` 的参数表——这就是为什么 `queue::push(cc_id_, msg_id_, remain, data, size)` 能直接构造出一条消息：

[ipc.cpp:53-63](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L53-L63)

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：精读 `queue_base::push`，说清它如何把消息构造回调转发给 `elems->push`，以及 `prep` 回调的作用。

**操作步骤**（源码阅读型，对应任务书要求）：

1. 打开 [queue.h:184-190](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L184-L190)，找到 `push` 的三行主体。
2. 画出回调嵌套：外层 `prep`（用户给）→ 中层 lambda `f`（queue_base 造）→ `elems_->push(this, f)`（算法层调）。
3. 跟到 [elem_array.h:123-126](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L123-L126)，确认 `elem_array::push` 把 `f` 原样转给 `head_.push(que, f, block_)`。
4. 跟到 [prod_cons.h:133-134](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L133-L134)，确认算法层算出槽位 `el` 后，用 `f(&(el->data_))` 把槽位数据区首地址回传给 `f`。
5. 回到 [ipc.cpp:596-606](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L596-L606)，对比 `push`（`prep` 恒真）与 `force_push`（`prep` = `clear_message`）两种 `prep`。

**需要观察的现象**：
- `prep` 收到的 `void* p` 永远指向某个槽位的 `data_` 区域，而不是任意堆地址。
- 正常 `push` 时 `prep` 不做任何清理；`force_push` 时 `prep` 必须先 `release_storage` 再放行。

**预期结果**：你能用自己的话回答两个问题——
1. *「queue_base::push 如何把构造回调转发给 elems->push？」* 答：它把 `prep(p) ? placement-new T(args) : nothing` 封装成 lambda `f`，连同 `this` 一起交给 `elems_->push`；算法层负责选槽、把槽位 `data_` 指针喂给 `f`，`f` 再调 `prep` 决定是否构造。
2. *「prep 回调的作用？」* 答：它是「是否允许在此槽位构造」的放行钩子。正常入队恒放行；强制入队驱逐旧消息时，先用 `clear_message` 清理被驱逐消息的外部存储（防 chunk 泄漏），再放行覆盖。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `push` 里 `if (prep(p))` 改成无条件 `::new (p) T(...)`，会出什么问题？
**答案**：`force_push` 驱逐旧大消息时不再清理其外部 chunk，造成共享内存 chunk 引用计数无法归零、永久泄漏。`prep` 正是为这种「覆盖前清理」留的口子。

**练习 2**：`pop` 的回调为什么用 `std::move(*static_cast<T*>(p))` 而不是拷贝？
**答案**：move 比拷贝快（尤其 `msg_t` 含定长载荷），且本接收者读完后该槽位即将被回收/复用，内容不必保留，move 走资源即可。

**练习 3**：`queue::pop(T&)` 里 `out` 回调是 `[](bool){}`（空操作），这个 `bool` 参数本该用来干什么？
**答案**：它本用于「消费后回收槽位」——在广播算法里，每个接收者读完要清自己的读计数位，最后一人回收槽位（u4-l4 详讲）。简单 `queue::pop` 不需要回收逻辑，故传空回调。

---

### 4.4 queue<T, Policy>：类型安全的模板封装

#### 4.4.1 概念说明

`queue_conn`、`queue_base` 都是库内部基类，对外暴露的是最顶层的 `queue<T, Policy>`。它的职责只有一件：**把元素类型 `T` 与策略 `Policy` 焊死**，给用户提供类型安全的 `push(T 的构造参数...)` / `pop(T&)` 接口。

`queue` 用 `sizeof(T)` 和 `alignof(T)` 作为环形数组的元素尺寸与对齐，这样每个槽位正好放得下一个 `T`：

```text
queue<msg_t<64,8>, Policy>
   └─ 继承 queue_base< Policy::elems_t< sizeof(msg_t), alignof(msg_t) >
                                   └─ elem_array< prod_cons_impl<Flag>, 64+, 8 >
```

#### 4.4.2 核心流程

`queue` 的 `push`/`force_push` 只是把 `T` 这个模板参数补回给基类：

```text
queue::push(args...)      →  base_t::push<T>(args...)        // 补上 T
queue::force_push(args...) →  base_t::force_push<T>(args...)
queue::pop(T& item)       →  base_t::pop(item, [](bool){})   // 补上 out=空操作
queue::pop(T& item, F)    →  base_t::pop(item, F)            // 用户自带 out 回调
```

`Policy` 从哪来？由 `policy::choose<circ::elem_array, Flag>` 提供 `elems_t` 模板别名，它把 `Flag` 映射成 `elem_array<prod_cons_impl<Flag>, DataSize, AlignSize>`：

[queue.h:213-216](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L213-L216) —— `queue` 用 `Policy::template elems_t<sizeof(T), alignof(T)>` 作为基类实参。

#### 4.4.3 源码精读

`queue` 的全部四个方法——都是补 `T` 后转发：

[queue.h:222-239](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L222-L239)

`Policy` 的来源，`choose` 把 `Flag` 映射成带 `prod_cons_impl` 的 `elem_array`：

[policy.h:16-22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L16-L22)

真实使用方 `queue_generator` 在 `ipc.cpp`，它把 `msg_t<DataSize, AlignSize>` 作为 `T`、把 `policy_t`（来自 `chan` 的策略标签）作为 `Policy`，组装出库内部用的队列类型：

[ipc.cpp:393-398](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L393-L398) —— `using queue_t = ipc::queue<msg_t<DataSize, AlignSize>, Policy>;`

`queue` 标记为 `final`，意味着它是不可再继承的最终类型——分层到此为止，再往上的 `conn_info_t`（`ipc.cpp`）是**持有**一个 `queue_t que_` 成员，而不是继承它：

[ipc.cpp:400-401](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L400-L401) —— `conn_info_t` 内含 `queue_t que_;`，组合而非继承。

#### 4.4.4 代码实践

**实践目标**：从「用户类型」一路追到「算法类型」，建立完整的类型链。

**操作步骤**（源码阅读型）：

1. 从 [ipc.cpp:398](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L398) `ipc::queue<msg_t<64,8>, policy_t>` 出发。
2. 展开 [policy.h:20-21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L20-L21)，得到 `elems_t = circ::elem_array<ipc::prod_cons_impl<flag_t>, 64, 8>`。
3. 展开 [queue.h:214](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L214)，得到 `queue` 的基类是 `queue_base<elem_array<prod_cons_impl<flag_t>, 64, 8>>`。
4. 读 [elem_array.h:27-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L27-L37)，确认 `elem_max = 256`、`block_size = elem_size * 256`。

**需要观察的现象**：`sizeof(msg_t<64,8>)` 决定每个槽位多大、数组一共 256 个槽位。

**预期结果**：你能写出完整的类型展开式，并指出 `flag_t`（`route` 还是 `channel`）最终决定了 `prod_cons_impl` 走哪套算法（u4-l3/u4-l4）。

#### 4.4.5 小练习与答案

**练习 1**：`queue` 为什么标 `final`？
**答案**：它是面向用户的最终封装，不希望被进一步继承（避免虚函数开销、固化分层）。更上层的 `conn_info_t` 用组合（持有一个 `queue_t` 成员）而非继承来扩展。

**练习 2**：为什么 `DataSize` 取 `sizeof(T)` 而不能直接写死成 `data_length`（64）？
**答案**：`queue` 是泛型模板，`T` 任意大小都必须放得下。库内部恰好用 `msg_t<64,8>` 作 `T`，所以 `sizeof(T)` 恰好让每槽位容纳一条 64 字节载荷的消息；换别的 `T` 就按 `sizeof(T)` 分配，保证类型安全。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从 `send` 一路追到槽位写入」的完整调用链梳理。

**任务**：以 `ipc.cpp` 的 `send` 路径为线索，画出从「用户调用 `chan_wrapper::send`」到「某条 `msg_t` 被 placement-new 进环形数组某个槽位」的完整函数跳转图，并在每一跳旁标注「这一跳属于三层抽象的哪一层」。

**参考步骤**：

1. 入口：`detail_impl::send` 调 `que->push(prep, cc_id_, msg_id_, remain, data, size)`（[ipc.cpp:596-598](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L596-L598)）——`que` 是 `queue<msg_t, Policy>*`，属**用户类型层**。
2. `queue::push` 补 `T` 转发 `base_t::push<T>(...)`（[queue.h:222-225](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L222-L225)）——**类型封装层**。
3. `queue_base::push` 造 lambda `f`，调 `elems_->push(this, f)`（[queue.h:187-189](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L187-L189)）——**收发委托层**。
4. `elem_array::push` 转发 `head_.push(que, f, block_)`（[elem_array.h:123-126](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L123-L126)）——**数组封装层**。
5. `prod_cons_impl::push` 算槽位 `el`，调 `f(&(el->data_))`（[prod_cons.h:133-134](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L133-L134)）——**算法层**。
6. `f` 内 `prep(p)` 放行后 `::new (p) msg_t(cc_id_, msg_id_, remain, data, size)`（[queue.h:188](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L188) + [ipc.cpp:53-63](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L53-L63)）——**构造回灌**。

**预期产物**：一张六跳的调用链图，清晰标注「哪一层负责选槽、哪一层负责构造、`prep` 在哪一步发挥作用」。完成后你就把本讲的「回调委托」彻底吃透了。

**待本地验证**：可开启 `LIBIPC_BUILD_DEMOS` 编译 `send_recv`（u1-l2），在 [queue.h:188](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L188) 加一行日志打印 `p` 与 `sizeof(T)`，运行 send 进程，确认每次写入的槽位地址都在 `[block_, block_ + block_size)` 区间内且按 `sizeof(T)` 步进。

## 6. 本讲小结

- `queue.h` 用三层抽象搭起队列：`queue_conn`（共享内存句柄 + 本进程座位号）→ `queue_base<Elems>`（环形数组指针 + 收发角色管理 + 回调委托）→ `queue<T, Policy>`（把 `T` 与 `Policy` 焊死的最终类型）。
- `connected_` 是**本进程本地**的座位 bit，不在共享内存；共享的全座位图是 `conn_head::cc_`。座位号（`connected_id`）与身份证号（`cc_id_`）是两回事。
- 发送者与接收者**不对称**：发送者只占一个 `bool`（多生产者恒真、单生产者原子抢占），接收者占唯一座位 bit（广播读计数需要）。
- `push`/`force_push`/`pop` 用**两层回调**把「选槽算法」与「类型构造」解耦：算法层算出槽位、把 `data_` 指针喂给回调；回调再调 `prep` 决定是否 placement-new 构造。
- `prep` 钩子是「覆盖前清理」的口子：正常 `push` 恒放行；`force_push` 驱逐旧消息时用 `clear_message` 先释放外部 chunk 存储，防泄漏。
- `Policy` 经 `policy::choose` 映射成 `elem_array<prod_cons_impl<Flag>, ...>`，`Flag`（`route`/`channel`）最终决定底层走哪套无锁算法。

## 7. 下一步学习建议

本讲只讲了「队列怎么把活儿派出去」，刻意没碰两层底层细节。建议接着读：

1. **u4-l2 `elem_array` 与 `conn_head` 连接位图**：搞清 `connect_receiver` 背后的位运算 `next = curr | (curr+1)`、DCLP 懒初始化、以及 256 个槽位的内存布局。
2. **u4-l3 `prod_cons` 单播变体**：看 `prod_cons_impl::push/pop` 的索引运算与 CAS，理解本讲回调收到的 `&(el->data_)` 是怎么算出来的。
3. **u4-l4 `prod_cons` 广播变体**：搞清本讲 `pop` 那个被空操作掉的 `out` 回调，在广播模式下到底怎么用 `rc_` 读计数 + epoch 协议回收槽位。
4. 复习 **u3-l1** 的 `detail_impl` 桥接层，把本讲的 `queue` 放回 `send/recv` 主链路里整体理解。
