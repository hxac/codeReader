# select 核心算法 run_select / run_ready

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `select.rs` 里 `Token` / `Operation` / `Selected` / `Timeout` 这四个核心数据类型各自的角色，以及 `Selected` 这台「四态状态机」是如何被多线程用 CAS 推动的。
- 把 `SelectHandle` trait 当成「select 算法与各 flavor 之间的契约」来理解：算法只调 8 个方法，flavor 负责具体的抢占、注册、唤醒后验收。
- 逐步跟踪一次「所有操作都阻塞」的 `select`，讲清楚 `shuffle → try_select → register → wait_until → unregister → accept` 每一步在做什么、为什么必不可少。
- 说出 `run_ready` 与 `run_select` 的关键差异（「只通知就绪、不占座」），以及为什么它可能虚假唤醒。
- 解释 `shuffle` 如何带来公平性、`biased` 又如何跳过 `shuffle` 实现优先级。

本讲是「使用层」的收口：u2-l9 讲了 `select!` 宏怎么写、u2-l10 讲了 `Select` 动态 API 怎么调，而本讲钻进这两者共同的「引擎」——`run_select` / `run_ready`。

## 2. 前置知识

本讲假设你已经掌握 u2-l4（`Context` / `Waker` 阻塞唤醒机制）和 u2-l9 / u2-l10（`select!` 与 `Select` 的用法）。复习几个关键点：

- **阻塞的三段式**：线程把自己的操作「登记」到通道的 `Waker` 队列里 → `park` 睡眠 → 被生产者/断开事件 `unpark` 唤醒。本讲的算法就是把这个三段式同时应用到「一批操作」上。
- **`Selected` 状态机**：一个线程在某次 select 中只会被「至多一个操作」选中，靠的是对线程本地的 `AtomicUsize` 做 CAS——这是「至多一个赢家」的根本保证。
- **抢占与搬运两步走**：array/list/zero flavor 都把收发拆成「抢占游标（`start_send`/`start_recv`）」和「搬运数据（`write`/`read`）」两步。select 算法做的就是在多个抢占机会里挑一个、再让调用者去搬运。
- **`SelectHandle`**：每个 flavor 都实现了这个 trait，算法不关心你到底是 array 还是 zero，只调 trait 方法。

如果你对「为什么需要 select」还陌生，先回到 u2-l9 看几个 `select!` 例子再回来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 本讲主角。定义 `Token`/`Operation`/`Selected`/`SelectHandle`/`Timeout`，以及核心算法 `run_select` 与 `run_ready`，外加 `Select`/`SelectedOperation` 公共类型。 |
| [src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | 线程本地的「被阻塞者」上下文 `Context`，提供 `try_select`/`selected`/`wait_until`/`store_packet` 等原语，是算法推动状态机的工具。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | 阻塞者队列。其 `try_select`/`disconnect` 是「别的线程选中我」这件事的发生地，理解它才能理解唤醒后为什么要 `accept`。 |
| [src/utils.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs) | `shuffle`（公平性来源）与 `sleep_until`。 |
| [src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 作为 flavor 实现 `SelectHandle` 的真实例子，把抽象 trait 落地。 |

## 4. 核心概念与源码讲解

### 4.1 核心数据类型与状态机：Token / Operation / Selected / Timeout

#### 4.1.1 概念说明

select 要解决的问题是：线程同时盯着多个通道操作，谁先「就绪」就执行谁，其余的当没发生过。要在一批操作之间做仲裁、还要跨线程协调，算法需要四样东西：

1. 一个**临时搬运筐** `Token`——选中的操作把「抢占到的位置信息」放进筐里，调用者随后用这个筐去 `read`/`write` 完成真正的数据搬运。
2. 一个**操作身份证** `Operation`——「这个线程在这个通道上的这次操作」的唯一编号，用于在 `Waker` 队列里登记和注销。
3. 一台**状态机** `Selected`——记录「这次 select 现在是什么状态」，是多线程抢「至多一个赢家」的载体。
4. 一个**超时设定** `Timeout`——把「非阻塞 / 永久阻塞 / 到点超时」三种模式统一成一个枚举，让 `run_select` 一套代码通吃。

#### 4.1.2 核心流程

`Selected` 是其中最关键的「状态机」，它有四个状态，状态的迁移**只通过 CAS 完成**：

```
                ┌─────────────────────────────────────────────┐
                │                                             ▼
        ┌─────────────┐   CAS   ┌──────────────┐  CAS   ┌──────────────────┐
启动 ──▶ │   Waiting   │ ──────▶ │   Aborted    │        │  Disconnected    │
        └─────────────┘         └──────────────┘        └──────────────────┘
              │  CAS                                          ▲
              └────────────▶ ┌──────────────────┐  CAS ──────┘
                             │ Operation(op_id) │
                             └──────────────────┘
```

- `Waiting`：还没人被选中，线程可能正在注册或睡眠。
- `Operation(op_id)`：某个操作赢了（可能是线程自己 fast path 选的，更常见是**别的线程**通过 `Waker::try_select` 替它选的）。`op_id` 指明是哪个操作。
- `Disconnected`：通道断开导致操作就绪（也是别的线程通过 `Waker::disconnect` 设的）。
- `Aborted`：线程主动放弃（超时到了，或非阻塞模式没选到）。

为了让状态机能塞进一个 `AtomicUsize`，三个「非操作」状态被编码成小整数 `0/1/2`，而 `Operation` 直接用操作编号（一个很大的指针地址）。

#### 4.1.3 源码精读

`Token` 是一个「按 flavor 分字段」的搬运筐，全部字段默认初始化，谁抢占成功谁填自己的字段：

[select.rs:L23-L32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L23-L32) —— `Token` 把六种 flavor 各自的搬运数据打包在一起，`#[derive(Default)]` 保证初始为空，最终由 `channel::read`/`channel::write` 按选中 flavor 读取。

`Operation` 用「某个线程本地活变量的地址」当编号，并断言它大于 2 以避免和状态机的 0/1/2 撞车：

[select.rs:L38-L51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L38-L51) —— `Operation::hook` 把 `&mut T` 的地址转成 `usize`；`assert!(val > 2)` 正是「操作编号不会和 `Waiting/Aborted/Disconnected` 混淆」的保证。

`Selected` 枚举与 `usize` 的双向转换实现了「一个原子变量装下整台状态机」：

[select.rs:L55-L80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L55-L80) —— `From<usize>` 里 `0/1/2` 对应三个非操作状态，其余整数还原成 `Operation(val)`。

`Timeout` 把三种阻塞模式收成一个枚举：

[select.rs:L160-L170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L160-L170) —— `Now`（`try_select` 用）、`Never`（`select` 用）、`At(Instant)`（`select_timeout`/`select_deadline` 用）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「`Selected` 编码」与「`Operation` 编码」互不冲突。
2. **步骤**：打开 `src/select.rs`，对照 `From<usize> for Selected`（L70-L80）与 `Operation::hook`（L45-L51）。
3. **观察**：`Waiting=0, Aborted=1, Disconnected=2`；操作编号是栈/堆变量地址，必为 3 的很多倍以上。
4. **预期**：理解 `assert!(val > 2)` 为何不会误伤合法操作编号——任何真实变量地址都远大于 2。
5. **待本地验证**：可写一段 `println!("{:p}", &mut some_local)` 看地址数值，确认它 ≫ 2。

#### 4.1.5 小练习与答案

**Q1**：为什么 `Selected` 不用 `Mutex` 而用 `AtomicUsize` + CAS？
**A**：因为状态转换是单点 CAS（`Waiting → X`），无锁更轻；且 CAS 失败能直接拿到「别人抢先设的状态」，正好用于「发现自己被别的线程选中」这条逻辑。

**Q2**：如果 `Operation::hook` 取到的地址恰好是 `2` 会怎样？
**A**：`assert!(val > 2)` 会触发 panic，拒绝创建该 `Operation`。这是防御性断言，实践中变量地址不可能这么小。

**Q3**：`Token` 为什么不做成 `enum` 而是结构体把所有 flavor 字段都放进来？
**A**：因为 select 算法是 flavor 无关的，提前在 `Token` 里给每种 flavor 预留字段，可以让 `try_select`/`accept` 只填自己那一栏，`read`/`write` 只读自己那一栏，无需运行期判别 flavor、零分配。

---

### 4.2 SelectHandle trait：算法与 flavor 之间的契约

#### 4.2.1 概念说明

`run_select` 是一个「通用算法」——它不懂环形缓冲、不懂链表分块、不懂会合配对。它只通过 `SelectHandle` trait 提供的 8 个方法与各 flavor 对话。你可以把 trait 想成一份合同：

> 「我（算法）会在合适的时机调用你的 `try_select` / `register` / `accept` / `is_ready` / `watch` 等方法；你（flavor）负责告诉我：现在能不能不阻塞地完成一次操作？怎么把你登记进阻塞队列？被唤醒后怎么把抢占到的位置交出来？」

trait 把「select 仲裁逻辑」和「具体队列实现」彻底解耦——这正是 u2-l1 所说「公共算法壳 + 多 flavor 实现」架构的体现。

#### 4.2.2 核心流程

trait 的 8 个方法按用途分两组：

| 方法 | 用途 | 谁调用 |
| --- | --- | --- |
| `try_select(&mut Token) -> bool` | **不阻塞地**尝试完成操作，成功则填好 token | fast path、唤醒后 |
| `register(op, cx) -> bool` | 把操作登记进阻塞队列，返回「登记瞬间是否已就绪」 | `run_select` 阻塞前 |
| `unregister(op)` | 注销登记 | `run_select` 醒来后 |
| `accept(&mut Token, cx) -> bool` | 被**别的线程**选中后，验收并填好 token | `run_select` 发现 `Operation` 状态 |
| `is_ready() -> bool` | 是否可不阻塞完成 | `run_ready` |
| `watch(op, cx) -> bool` | 登记「就绪通知」，返回当前是否已就绪 | `run_ready` |
| `unwatch(op)` | 注销就绪通知 | `run_ready` |
| `deadline() -> Option<Instant>` | 操作自带的最早截止时间（at/tick 用） | 计算阻塞期限 |

注意 `run_select` 用 `try_select/register/unregister/accept`，而 `run_ready` 用 `is_ready/watch/unwatch`——这正是两条路径的分工。

#### 4.2.3 源码精读

trait 定义本身：

[select.rs:L99-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123) —— 注释明确说这是给 select 宏用的私有 API，文档里隐藏。

以 array flavor 的接收端为例，看 trait 如何落地（`register` 把自己塞进 `receivers` 这个 `SyncWaker` 队列，并返回当前是否就绪）：

[array.rs:L614-L637](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L614-L637) —— `try_select` 委托 `start_recv`（抢占游标），`register` 调 `SyncWaker::register` 再查 `is_ready`，`accept` 直接复用 `try_select`，`is_ready` = 「非空或已断开」。

对比 at flavor（定时通道），它的 `deadline()` 会返回真实的投递时间，让算法据此提前醒来：

[at.rs:L159-L172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L159-L172) —— `deadline()` 在「尚未投递」时返回 `Some(delivery_time)`，且 `register` 是空操作（at 通道不进阻塞队列，靠 deadline 自驱动）。这就是 `after`/`at` 能融入 select 的关键。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：体会「同一段算法，不同 flavor」的解耦。
2. **步骤**：在 `src/flavors/` 下分别打开 `array.rs`、`list.rs`、`zero.rs`，找到各自的 `impl SelectHandle`。
3. **观察**：三者 `register` 的实现各不相同（array/list 进 `SyncWaker`，zero 进 `Mutex<Inner>` 里的 `Waker`），但对外都满足同一个 trait 签名。
4. **预期**：理解 `run_select` 为何能对三者一视同仁——它只看见 `&dyn SelectHandle`。

#### 4.2.5 小练习与答案

**Q1**：`register` 返回 `bool` 的含义是什么？为什么算法要这个返回值？
**A**：表示「登记的瞬间，操作是否已经就绪」。因为「检查就绪」和「登记」之间存在窗口，`register` 内部把它们合并成原子动作并顺带返回就绪状态，算法据此不必白白 park 一次。

**Q2**：`accept` 和 `try_select` 有何区别？
**A**：`try_select` 是「我现在主动试试能不能完成」；`accept` 是「我已经被别的线程选中了（状态变成 `Operation`），请把抢占到的位置交给我」。对 array 两者恰好都委托 `start_recv`，但对 zero flavor 二者逻辑不同——`accept` 要从 packet 里读取对方递来的数据。

**Q3**：为什么 `run_ready` 用 `watch` 而不是 `register`？
**A**：因为 `run_ready`（`ready` 家族）只想「知道谁就绪」，并不想抢占座位、也不强制完成。`watch` 登记的是 `Waker` 里的 `observers` 队列（只通知、不配对），而 `register` 登记的是 `selectors` 队列（会被 `try_select` 配对选中）。

---

### 4.3 run_select 全流程精读

这是本讲的重头戏。`run_select` 把「在一批操作里选一个」这件事编成了一段精心编排的并发舞蹈。

#### 4.3.1 概念说明

`run_select` 要同时满足几个互相冲突的目标：

- **正确性**：多个操作中「至多一个」被真正执行，不能重复消费消息。
- **公平性**：多个操作同时就绪时，不能总是偏袒某一个。
- **不漏唤醒**：登记到「准备 park」之间存在窗口，必须保证不被在这窗口里发生的事件遗漏。
- **可超时**：支持「立即返回」「永久等」「到点返回」三种模式。
- **支持配对**：像 zero 这种会合通道，选中之后还要把对方递来的 packet 接过来。

它用一个 `Token` 当搬运筐，靠 `Context` 推动状态机，靠「登记—复查—park—醒来—注销—验收」这套循环堵住所有竞态窗口。

#### 4.3.2 核心流程

完整流程的伪代码（阻塞模式 `Timeout::Never`，所有操作初始都未就绪）：

```
run_select(handles, timeout, is_biased):
    若 handles 为空: 按 timeout 睡眠/返回 None
    若非 biased: shuffle(handles)            # ① 公平性
    token = Token::default()

    # ② fast path：不阻塞地试一遍
    for (handle, i, addr) in handles:
        if handle.try_select(token): return Some(token, i, addr)

    loop:
        res = Context::with(|cx| {            # 每次循环用线程本地 cx
            sel = Waiting
            # ③ 登记全部操作，期间反复查「是否被别的线程选中」
            for (handle, i, _) in handles:
                if handle.register(hook(handle), cx):   # 登记瞬间就绪
                    sel = cx.try_select(Aborted) ? Aborted+记 index_ready : 已有状态
                    break
                sel = cx.selected()            # 别的线程可能已选中我
                if sel != Waiting: break

            if sel == Waiting:
                deadline = min(各 handle.deadline() 与 timeout)
                sel = cx.wait_until(deadline)  # ④ park，醒来拿最终状态

            # ⑤ 注销所有已登记操作
            for handle in handles[:registered_count]: handle.unregister(...)

            # ⑥ 根据醒来后的状态收尾
            match sel:
                Operation(op): 找到 op 对应 handle，调 accept(token, cx) → 成功则返回 (i, addr)
                Disconnected:   返回 None（外层 fast path 会捕到断开的就绪）
                Aborted:        若有 index_ready，try_select 它并返回
            None
        })

        if res.is_some(): return res           # 验收成功，带 token 返回

        # ⑦ 失败重试：再走一遍 fast path（处理断开/虚假唤醒）
        for handle in handles: if handle.try_select(token): return ...
        # ⑧ 超时检查
        match timeout: Now → None; At(when) 若已过 → None; Never → 继续
```

#### 4.3.3 源码精读

**① 空操作与 shuffle**：

[select.rs:L181-L199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L181-L199) —— 没有操作时按 `timeout` 决定睡眠或立即返回；`!is_biased` 时调 `utils::shuffle(handles)` 打乱顺序，保证 fast path 里「谁先被试」是随机的。

**② fast path（不阻塞试一遍）**：

[select.rs:L206-L211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L206-L211) —— 这是最常见的命中路径：已经有消息/已断开，直接 `try_select` 抢占成功，根本不进入阻塞循环。

**③ 登记 + 防漏唤醒**（`run_select` 最精妙的一段）：

[select.rs:L215-L246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L215-L246) —— 重点看三件事：(a) `Timeout::Now` 时先 `cx.try_select(Aborted).unwrap()` 把自己标记成「已放弃」，这样并发线程不会选中一个我们根本不会等的操作；(b) 每登记一个操作就 `register`，若登记瞬间它就绪了，立刻 `try_select(Aborted)` 并记下 `index_ready`；(c) 每次登记后 `cx.selected()` 复查——因为登记上一个操作期间，**别的线程**可能已经通过它把我们选中了（状态变成 `Operation`/`Disconnected`），必须及时 `break`。

关于 (a) 里那个 `.unwrap()` 为何不会 panic：进入闭包前 `Context::with` 已 `reset()` 成 `Waiting`，而我们尚未在任何 `Waker` 里登记，别的线程无从改动我们的状态，故 CAS 必成功。

**④ 计算 deadline 并 park**：

[select.rs:L248-L264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L248-L264) —— deadline 取 `timeout` 与各操作 `handle.deadline()`（at/tick 提供）的最早值；`wait_until` 内部是「循环 load 状态 + park」以容忍虚假唤醒（见 [context.rs:L143-L169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L143-L169)），到点则自行 `try_select(Aborted)`。

**⑤ 注销已登记操作**：

[select.rs:L266-L269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L266-L269) —— 只注销 `registered_count` 个（因为可能中途 `break` 没登记完），把阻塞队列打扫干净。

**⑥ 醒来后按状态收尾**：

[select.rs:L271-L297](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L271-L297) —— 这是 `accept` 登场的地方：若状态是 `Operation(op)`，说明**别的线程**通过 `Waker::try_select` 选中了我（见 [waker.rs:L84-L111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)，它会 CAS 我的状态、`store_packet`、`unpark` 我），我要找到对应 handle 调 `accept` 把 packet/token 接过来。`Disconnected` 分支为空——留给外层 fast path 兜底。`Aborted` 分支则尝试认领登记期间发现的 `index_ready`。

**⑦⑧ 失败重试与超时**：

[select.rs:L302-L322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L302-L322) —— 闭包返回 `None`（典型场景：`Disconnected` 或虚假唤醒）时，再跑一遍 fast path，再按 `timeout` 决定返回还是继续循环。

**入口三件套**（把 `Timeout` 三态接到 `run_select`）：

[select.rs:L456-L521](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L456-L521) —— `try_select`→`Timeout::Now`，`select`→`Timeout::Never`，`select_timeout`→先 `checked_add` 换算 `Instant`（溢出退化为 `Never`）→`Timeout::At`。三者本质都是同一个 `run_select`。

#### 4.3.4 代码实践（主实践：跟踪一次全阻塞的 select）

这是本讲的核心实践任务。

1. **实践目标**：把 `shuffle → try_select → register → wait_until → unregister → accept` 这条链子在脑子里完整跑一遍，并定位 `Aborted` 何时出现。
2. **操作步骤**：
   - 准备两个**空且有界**的通道 `(s1,r1)=bounded(0)`、`(s2,r2)=bounded::<i32>(1)`，构造一个 `Select`，`recv(&r1)` 和 `recv(&r2)`。两个通道此刻都空且未断开，必然全阻塞。
   - 在另一线程 `sleep(100ms)` 后 `s2.send(42)`。
   - 调 `sel.select()`，断言收到 42。
   - 对照源码，给每一步写一句注释（见下表）。
3. **需要观察的现象 / 4. 预期结果**：

   | 步骤 | 源码位置 | 这次执行里它在做什么 |
   | --- | --- | --- |
   | shuffle | L196-L199 | 打乱 `[r1,r2]` 顺序（公平） |
   | try_select (fast path) | L206-L211 | r1、r2 都空，两个 `try_select` 都返回 false，未命中 |
   | register | L224-L246 | 把 r1、r2 依次登记进各自 `SyncWaker`，期间 `cx.selected()` 一直是 `Waiting` |
   | wait_until | L262-L263 / context.rs L143-L169 | deadline=None，`park` 睡眠 |
   | （对方 send） | waker.rs L84-L111 | 生产者 `write` 后 `notify`，`Waker::try_select` 把我的状态 CAS 成 `Operation(r2 的 hook)` 并 `unpark` 我 |
   | unregister | L266-L269 | 醒来后注销 r1、r2 |
   | accept | L284-L295 | 状态是 `Operation`，找到 r2，调 `accept` 拿到 token，返回 `(index_of_r2, addr)` |

4. **Aborted 何时出现**（回答实践问题）：
   - **登记期间就绪**：若某个操作在 `register` 调用瞬间返回 `true`（已就绪），算法会 `cx.try_select(Aborted)` 并记 `index_ready`，随后在 L273-L281 的 `Aborted` 分支里用 `try_select` 认领它。
   - **超时到点**：`Timeout::At` 且 `wait_until` 内部发现 deadline 已过，`context.rs` 的 L159-L163 会自行 `try_select(Aborted)`，于是 `sel=Aborted`。
   - **非阻塞模式**：`Timeout::Now` 在 L220-L222 一进来就把自己标成 `Aborted`，表示「我不想真等」。
   - 在本次「全阻塞 + 对方唤醒」的场景里，状态走的是 `Operation` 分支而**不是** `Aborted`——`Aborted` 只在「自己放弃」或「登记窗口内侥幸命中」时出现。
5. **待本地验证**：实际运行确认收到 42；可在 `register`/`accept` 处临时加日志观察顺序（注意这会改变时序，仅用于学习）。

#### 4.3.5 小练习与答案

**Q1**：为什么登记循环里每登记一个就要 `cx.selected()` 复查一次？
**A**：因为登记上一个操作的过程中，别的线程可能恰好通过该操作把我们选中（状态从 `Waiting` 变 `Operation`/`Disconnected`）。若不复查，我们会继续登记剩余操作甚至 `park`，造成「已被选中却还在等」的错乱。

**Q2**：fast path 已经试过一遍 `try_select`，为什么闭包返回 `None` 后（L307-L312）还要再试一遍？
**A**：因为闭包返回 `None` 的常见原因是 `Disconnected` 或虚假唤醒——这两种情况下通道状态刚刚变化，此刻很可能已经就绪。再试一次 fast path 能直接命中，避免又一轮登记+park。

**Q3**：`Context::with` 为什么用线程本地缓存的 `Context` 而非每次 `new`？
**A**：避免每次 select 都分配 `Arc<Inner>`。线程本地 `Context` 在 `with` 结束后 `cell.set(Some(cx))` 归还，下次复用并 `reset()`，把分配摊销到零。

---

### 4.4 run_ready：就绪通知路径与公平性

#### 4.4.1 概念说明

`run_ready` 服务于 `Select` 的 `ready` 家族（`try_ready`/`ready`/`ready_timeout`/`ready_deadline`）。它与 `run_select` 的根本区别：

- `run_select` 选中后会**抢占座位**（返回 `SelectedOperation`，必须 `recv`/`send` 完成，否则 panic）。
- `run_ready` 只返回**就绪操作的 index**，不抢占、不强制完成。好处是灵活（可以 `try_recv` 重试），坏处是可能「虚假唤醒」——通知你就绪了，等你去 `try_recv` 时消息可能已被别的线程抢走。

因此 `run_ready` 用的是 `is_ready` / `watch` / `unwatch` 这组方法（而不是 `try_select` / `register` / `accept`），登记进 `Waker` 的 `observers` 队列而非 `selectors` 队列。

#### 4.4.2 核心流程

`run_ready` 的结构比 `run_select` 简单，多了一层「自旋重试」：

```
run_ready(handles, timeout, is_biased):
    若 handles 为空: 按 timeout 睡眠/返回 None
    若非 biased: shuffle(handles)
    loop:
        backoff 自旋: 反复 for handle in handles: if handle.is_ready(): return Some(i)
        超时检查 (Now→None, At→可能 None)
        Context::with(|cx| {
            for handle: if handle.watch(hook, cx):  # 登记瞬间就绪
                sel = try_select(Operation(op)) ? Operation : 已有状态; break
            if sel == Waiting:
                deadline = min(...); sel = cx.wait_until(deadline)
            for handle: handle.unwatch(...)
            match sel: Operation(op) → 找到对应 i 返回 Some(i); 其余 → None
        })
        if res.is_some(): return res
```

注意 `run_ready` 没有 fast path 的 `try_select`——它的「fast path」是开头的 `is_ready` 自旋。

#### 4.4.3 源码精读

**自旋重试层**（`run_ready` 独有）：

[select.rs:L352-L378](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L352-L378) —— 内层 `loop` 用 `Backoff` 反复 `is_ready`，退避完成后才进入阻塞登记；这与 `run_ready`「可能虚假唤醒、需要重试」的特性配套。

**watch 登记 + park**：

[select.rs:L381-L427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L381-L427) —— 与 `run_select` 对偶：`watch` 替代 `register`，登记成功用 `Operation(op)` 标记自己（而非 `Aborted`），醒来后只找 index 不调 `accept`（不抢座位）。

**收尾只返回 index**：

[select.rs:L429-L444](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L429-L444) —— `Operation` 分支只比对 `oper` 返回 `Some(*i)`，没有任何 `accept`/`token`，印证了「只通知、不占有」。

**公平性：shuffle 的实现**

[utils.rs:L7-L40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L7-L40) —— 用线程本地的 32 位 Xorshift RNG 生成随机数，配合 Lemire 的快速取模（`(x as u64 * n as u64) >> 32`）做 Fisher-Yates 洗牌。每次 select 调用前打乱 `handles`，使 fast path 里「先试谁」随机化，长期看每个操作被选中的概率趋于均等。

biased 模式跳过 shuffle（[select.rs:L196-L199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) 与 [L347-L350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L347-L350) 的 `if !is_biased`），于是总是按添加顺序优先选靠前的操作——这就是 `select_biased!` / `Select::new_biased()` 的优先级语义。

> 关于「公平」的一个小数学直觉：若同时有 \(k\) 个操作就绪，shuffle 后每个排在最先被尝试位置的概率为 \(1/k\)，因此在 fast path 里它被选中的概率也近似 \(1/k\)，长期频率收敛到均匀。

#### 4.4.4 代码实践（源码阅读型 + 可选运行）

1. **目标**：对比 `ready` 与 `select` 在「消息可能被抢」时的行为差异。
2. **步骤**：写两个接收端 `r1`、`r2`，多线程往 `r1` 对应发送端发消息；用 `Select::new()` + `ready()` 拿到 index 后用 `try_recv`，并在 `try_recv` 返回 `Empty` 时 `continue` 重试（这正是 [select.rs:L592-L606](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L592-L606) 文档示例的写法）。
3. **观察**：偶发地，`ready()` 返回某 index 后 `try_recv` 却拿到 `Empty`（消息被别的线程抢走）——这就是「虚假唤醒」，必须靠重试兜住。
4. **预期**：`select()` 不会出现这种问题，因为它已抢占座位；`ready()` 更轻但需重试。
5. **待本地验证**：高并发下偶发 `Empty`，确认重试逻辑能最终拿到消息。

#### 4.4.5 小练习与答案

**Q1**：`run_ready` 为什么不像 `run_select` 那样在闭包返回 `None` 后再跑 fast path `try_select`？
**A**：因为 `run_ready` 不抢座位，它的「重试」是回到外层 `loop` 开头重新 `is_ready` 自旋（L352-L367），而不是调 `try_select`。它的循环本身就是重试机制。

**Q2**：`biased` 模式下，shuffle 被跳过，会不会损害正确性？
**A**：不会，只影响「公平性」。shuffle 仅用于在多个同时就绪的操作里随机挑一个；跳过它只是改成「按固定顺序挑」，仍是正确的仲裁，只是不再是均匀随机。

**Q3**：`ready` 家族为什么要把操作登记进 `observers` 而不是 `selectors`？
**A**：`selectors` 会被 `Waker::try_select` 选中并配对（占座位），`observers` 只会被 `Waker::notify`/`disconnect` 「通知就绪」而不配对（见 [waker.rs:L127-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L127-L168)）。`ready` 只想要通知，故用 `observers`。

---

## 5. 综合实践

把本讲全部内容串起来：写一个「双源接收 + 超时」的小程序，并**预测每条出口对应 `run_select` 的哪个分支**。

**任务**：

```rust
// 示例代码（非项目原有，仅供练习）
use std::{thread, time::Duration};
use crossbeam_channel::{bounded, Select};

let (s1, r1) = bounded::<i32>(1);
let (s2, r2) = bounded::<i32>(1);

// 场景三选一运行，观察命中哪条分支：
// A) 提前 s1.send(1)，主线程 select 立即命中 r1  → fast path (L206-L211)
// B) 不 send，select_timeout(50ms) 超时         → Aborted via wait_until (context.rs L159-L163)
// C) 另一线程 sleep(20ms) 后 s2.send(2)         → register→wait_until→unregister→accept (L215-L295)

let mut sel = Select::new();
let i1 = sel.recv(&r1);
let i2 = sel.recv(&r2);

match sel.select_timeout(Duration::from_millis(50)) {
    Ok(op) => {
        let (idx, val) = if op.index() == i1 { (i1, op.recv(&r1).unwrap()) }
                         else { (i2, op.recv(&r2).unwrap()) };
        println!("收到来自 r{} 的 {}", idx + 1, val);
    }
    Err(_) => println!("超时"),
}
```

**要求**：

1. 分别构造 A/B/C 三种场景运行，记录每次走的是 `run_select` 的哪一段（fast path / 超时 Aborted / 阻塞唤醒 accept）。
2. 对场景 C，画出「主线程状态机」的时间线：`Waiting →（对方 send→Waker::try_select→CAS+store_packet+unpark）→ Operation(op) → unregister → accept`。
3. 把 `Select::new()` 换成 `Select::new_biased()`，在 A 场景里两个通道都提前放消息，观察是否总是命中 `i1`（验证 biased 跳过 shuffle）。

这个练习一次用到了 4.1 的状态机、4.2 的 trait 契约、4.3 的全流程、4.4 的公平性。

## 6. 本讲小结

- `Selected`（`Waiting`/`Aborted`/`Disconnected`/`Operation`）是一台塞进单个 `AtomicUsize` 的四态状态机，所有迁移靠 CAS，是「至多一个赢家」的根本保证；`Operation` 用变量地址当编号，并断言 `>2` 以避让 `0/1/2`。
- `SelectHandle` trait 是 select 算法与各 flavor 之间的契约：算法只用 `try_select/register/unregister/accept`（select 路径）和 `is_ready/watch/unwatch`（ready 路径）这 8 个方法，对 array/list/zero 一视同仁。
- `run_select` 的核心是「fast path 抢占 → 登记+防漏唤醒复查 → wait_until 阻塞 → 注销 → 醒后 accept」循环；`Timeout` 三态（`Now`/`Never`/`At`）让 `try_select`/`select`/`select_timeout` 共用同一套实现。
- `accept` 只在「被别的线程选中」（状态为 `Operation`）时登场，负责把对方通过 `Waker::try_select` 递来的 packet/token 接过来——这是会合通道能融入 select 的关键。
- `Aborted` 出现于三种时机：非阻塞模式自我标记、登记窗口内侥幸命中（配 `index_ready`）、超时到点；它表示「自己放弃」而非「被选中」。
- `run_ready` 只通知就绪、不占座位，因此更轻但可能虚假唤醒，需调用者用 `try_recv` 重试；公平性由 `utils::shuffle`（Xorshift + Lemire 取模的 Fisher-Yates）提供，`biased` 模式跳过 shuffle 实现优先级。

## 7. 下一步学习建议

- 下一讲 **u3-l2（SelectHandle trait 与 flavor 对接）** 会逐个 flavor 展开 `try_select`/`register`/`accept` 的内部实现，把本讲「trait 当黑盒」的部分打开，重点看 zero flavor 如何用 `accept` 接收 packet。
- 如果你想先看「调用者怎么把 `SelectedOperation` 完成掉」，可读 [select.rs:L1276-L1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1318) 的 `send`/`recv` 完成方法，注意它们用 `mem::forget(self)` 阻止 `Drop` 的 panic。
- 想验证理解，可挑 `tests/select.rs` 里「全部操作阻塞、靠对方唤醒」的用例，对照本讲的步骤表逐行印证。
- 若对内存序与 `unsafe` 正确性感兴趣，u3-l4 会专门讲 array/list/zero 的 `Acquire`/`Release` 选择，本讲的 `Context::try_select` 用 `AcqRel`/`Acquire` 正是其中一例。
