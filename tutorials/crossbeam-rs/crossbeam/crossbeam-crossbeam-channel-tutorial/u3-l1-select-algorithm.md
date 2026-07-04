# select 核心算法 run_select / run_ready

> 本讲所属：专家层 u3。前置：u2-l9（使用 `select!` 宏）、u2-l4（阻塞与唤醒 context.rs + waker.rs）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `select!` 宏与 `Select` 动态 API 背后那一个真正的「内核函数」`run_select` 是怎么运转的——从「随便试试」到「登记睡觉」再到「被人叫醒后收割」的全流程。
2. 解释 `Token` / `Operation` / `Selected` 这三件套各自的角色，以及 `SelectHandle` trait 的七个方法如何把「select 算法」和「六种 flavor 实现」解耦。
3. 读懂 `run_ready`（`ready` 家族）为何与 `run_select` 长得像、却只能返回 index 而不「占座」，因而可能虚假唤醒。
4. 复述 `shuffle` 如何带来公平性、`biased` 如何跳过它，以及 `Selected::Aborted` 在三种不同情形下分别意味着什么。
5. 拿着一份真实调用链，逐步标注一次「所有操作都阻塞」的 select 经历了哪些阶段。

本讲只读 `src/select.rs` 与 `src/context.rs` 两个核心文件，并辅以 `src/utils.rs` 的 `shuffle` 与 `src/waker.rs` 的 `try_select`/`disconnect` 作为补充。**不**展开每种 flavor 如何实现 `SelectHandle`（那是 u3-l2 的内容），也**不**展开宏如何把这些函数拼装成 `select!` 语法（那是 u3-l3）。

## 2. 前置知识

在进入内核之前，先用一句话复习本讲要用到的、在前置讲义里已经建立的概念（若仍有陌生，建议先回看对应讲义）：

- **flavor 与公共类型壳**：`Sender`/`Receiver` 只是一个「壳」，内部持有一个 flavor 枚举字段，所有方法都 `match flavor` 后转发（u2-l1）。
- **阻塞三段式**：一次阻塞收发 = 「登记自己 → `park` 睡觉 → 被人 `unpark` 叫醒」（u2-l4）。
- **`Context`**：每个线程本地的「被阻塞者」状态机，内部持一个 `Selected` 原子状态与一个递包槽 `packet`（u2-l4）。
- **`Waker` / `SyncWaker`**：「阻塞者队列」视角，生产/断开时调 `try_select`/`disconnect` 把队列里**别的线程**的状态 CAS 成「就绪」并 `unpark`（u2-l4）。
- **`select!` 的三种分支**：无 `default`（阻塞）、`default`（非阻塞）、`default(timeout)`（限时），底层分别调 `internal::select` / `try_select` / `select_timeout`（u2-l9）。
- **`Select` 动态 API**：运行时注册任意多个操作，`select` 家族返回「必须完成」的 `SelectedOperation`，`ready` 家族只返回 index（u2-l10）。

一个贯穿全讲的直觉：**select 的本质是「多个线程对同一个原子状态做 CAS 抢占，保证至多一个操作胜出」**。`run_select` 只是把 u2-l4 里那套「登记—睡觉—叫醒」机制，**同时**套用到 N 个操作上，并处理「N 个操作之间谁先就绪」「睡觉期间被别人抢了怎么办」「超时了怎么办」这些并发细节。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | **绝对主角**。定义 `Token`/`Operation`/`Selected`/`SelectHandle`/`Timeout`，以及两个内核函数 `run_select`、`run_ready`，外加 `Select`/`SelectedOperation` 的公共 API。 |
| [src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | `Context`：线程本地状态机，提供 `try_select`/`selected`/`wait_until`/`store_packet`，是 `run_select` 睡觉与被叫醒的实际载体。 |
| [src/utils.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs) | `shuffle`：为 `run_select`/`run_ready` 提供公平性（随机化尝试顺序）。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `Waker::try_select`/`disconnect`/`notify`：别的线程如何把「我们」的 `Context` 状态改成 `Operation`/`Disconnected`，从而把 `run_select` 从 `wait_until` 里叫醒。 |

记忆口诀：**`select.rs` 是导演，`context.rs` 是演员（线程自己的状态），`waker.rs` 是别的线程来敲门的人，`utils.rs` 负责排队时掷骰子。**

## 4. 核心概念与源码讲解

### 4.1 select 的数据与状态三件套：Token / Operation / Selected

#### 4.1.1 概念说明

要理解 `run_select`，先要认清它在内存里来回搬运的三样东西：

- **`Token`**：一个临时「数据口袋」。select 在「抢占」阶段往里塞一些 flavor 专属的线索（比如 array flavor 抢到了哪个槽位、zero flavor 的 packet 指针），等真正「搬运」消息时（`channel::read`/`channel::write`）再从口袋里取出来用。它是「抢占」与「搬运」这两步之间的传话筒。
- **`Operation(usize)`**：一个操作的「身份证号」。它由 `Operation::hook(&mut 某变量)` 把一个**线程本地、存活于整个 select 期间**的引用的地址转成数字得来。同一个 select 里，每个被注册的操作都有一张唯一身份证。
- **`Selected`**：一个四态状态机，描述「这次 select / 这次阻塞操作现在到哪一步了」。

三者关系紧密：`Selected` 描述状态，`Operation` 标识「哪个操作」让状态发生了变化，`Token` 则在状态确定后携带「怎么把这个操作做完」的数据。

#### 4.1.2 核心流程

`Selected` 的四态及其转换（一切转换都只通过 `compare_exchange(Waiting → 其它)` 完成，因此「至多一个转换成功」，保证至多一个操作胜出）：

```
            ┌──────────────────────────────────────────────┐
            │                                              │
            ▼                                              │
   ┌─────────────────┐   try_select(CAS 成功)   ┌──────────┴───────┐
   │   Waiting       │ ───────────────────────▶ │  Aborted         │  自己放弃
   │ （还在等）       │                           │ （发现就绪/超时/ │
   └─────────────────┘                           │   非阻塞）        │
     │  │  │                                     └──────────────────┘
     │  │  │  别的线程 Waker::try_select 把我 CAS 成 Operation(我的op)
     │  │  └──────────────────────────────▶ ┌──────────────────────┐
     │  │                                  │ Operation(Operation) │  有人替我选中
     │  │                                  └──────────────────────┘
     │  │
     │  │  别的线程 Waker::disconnect 把我 CAS 成 Disconnected
     │  └──────────────────────────────▶ ┌──────────────────────┐
     │                                   │   Disconnected       │  通道断开
     │                                   └──────────────────────┘
     │
     └────▶ 全部迁移路径只有一个入口：CAS(Waiting → 目标)，
            所以「至多一个赢家」由原子操作本身保证。
```

`Operation::hook` 的关键约束是：转换出来的数字**不能**等于 `Waiting(0)`/`Aborted(1)`/`Disconnected(2)` 这三个保留值，否则状态机和身份证号会撞车。源码用一句 `assert!(val > 2)` 来守护。

#### 4.1.3 源码精读

`Token` 是各 flavor 字段的聚合体，`#[derive(Default)]` 意味着一开始所有口袋都是空的：

[src/select.rs:L23-L32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L23-L32) — `Token`：六个 flavor 各占一个口袋字段，注释里写明它是 select 期间的临时变量，最终被 `channel::read()`/`write()` 消费。之所以用「结构体塞所有 flavor 字段」而非 enum，是为了让算法 flavor 无关、且零分配——`try_select`/`accept` 只填自己那一栏。

`Operation::hook` 把引用地址变成身份证号，并断言它大于 2：

[src/select.rs:L38-L52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L38-L52) — `Operation::hook`：注意它取的是「指向 handle 的引用」的地址，而 handle 本身在 `handles` 切片里，因此同一次 select 调用里同一操作的身份证号既稳定又唯一。

`Selected` 四态枚举，以及它与 `usize` 的互转（这是它能否塞进 `AtomicUsize` 的前提）：

[src/select.rs:L54-L92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L54-L92) — `Selected` 与 `From<usize>`/`From<Selected>`：`0/1/2` 分别对应 `Waiting/Aborted/Disconnected`，其余整数都解释为 `Operation(那个整数)`。这正是 `Operation::hook` 为什么要 `assert!(val > 2)` 的原因。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `Token` 在一次 select 中「被谁写、被谁读」。
2. **步骤**：在 `src/select.rs` 里搜索 `Token::default()`（创建空口袋）、`&mut token`（写口袋）、`channel::read`/`channel::write`（消费口袋）。
3. **观察**：你会看到 `run_select` 里 `let mut token = Token::default();`，随后 `try_select`/`accept` 都以 `&mut token` 往里写，最终 `SelectedOperation::recv`/`send` 把它交给 `channel::read`/`write`。
4. **预期**：`Token` 从不跨线程共享——它就是当前线程栈上的一个临时变量。
5. **运行结果**：纯阅读，无需运行。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `Operation::hook` 必须断言返回值 `> 2`？
  - **A**：因为 `Selected` 用 `0/1/2` 表示 `Waiting/Aborted/Disconnected`。若某操作的身份证号恰好是 0/1/2，状态机就分不清「还在等」和「这个操作被选中了」。
- **Q2**：`Selected` 一共几种状态？哪几种通常由「别的线程」触发？
  - **A**：四种：`Waiting`/`Aborted`/`Disconnected`/`Operation(_)`。其中 `Disconnected` 与 `Operation(_)` 通常由别的线程通过 `Waker` 触发；`Aborted` 多由自己触发（发现自己就绪/超时/非阻塞）。
- **Q3**：`Selected` 为什么用 `AtomicUsize` + CAS 而不是 `Mutex`？
  - **A**：状态迁移是单点 CAS（`Waiting → X`），无锁更轻；且 CAS 失败能直接拿到「别人抢先设的状态」，正好用于「发现自己已被别的线程选中」这条逻辑。

---

### 4.2 SelectHandle trait —— flavor 与 select 算法的七方法契约

#### 4.2.1 概念说明

`run_select` 是一个**与 flavor 无关**的通用算法：它拿到的是一堆 `&dyn SelectHandle`（trait object），不知道也不关心对方是 array、list 还是 zero。每一种 flavor 只需实现 `SelectHandle` 这个 trait，就能「插队」进 select 系统。

这个 trait 一共七个方法，可按「select 家族用」和「ready 家族用」分成两组：

| 方法 | 服务于 | 一句话职责 |
| --- | --- | --- |
| `try_select(&mut token)` | `run_select` fast path | 不阻塞地试一下：能不能现在就完成？成功则往 `token` 写线索并返回 `true`。 |
| `register(oper, cx)` | `run_select` | 把「我（线程）正在等这个操作」登记进该 flavor 的阻塞者队列；返回 `true` 表示登记瞬间发现已经就绪。 |
| `unregister(oper)` | `run_select` | 取消登记（睡醒后收尾）。 |
| `accept(&mut token, cx)` | `run_select` | 「我被别的线程叫醒了，来完成这个操作」；成功返回 `true`。 |
| `deadline()` | 两者 | 这个操作自身有没有截止时间（如 `at`/`tick` 通道的投递时刻）。 |
| `is_ready()` | `run_ready` | 不阻塞地问：现在就绪吗？（不登记、不占座） |
| `watch(oper, cx)` / `unwatch(oper)` | `run_ready` | 登记成「就绪观察者」（observer），就绪时只通知、不替你完成。 |

#### 4.2.2 核心流程

`run_select` 一次循环里，对一个 handle 调用方法的顺序是：

```
try_select  ──(失败)──▶  register  ──(睡醒)──▶  accept
                                  │
                                  └─(收尾)──▶  unregister
```

- 先 `try_select`「碰运气」（fast path）；
- 碰不到就 `register` 把自己挂进阻塞者队列再去睡；
- 睡醒后要么 `accept`（被别人选中了，来完成），要么什么都不做（超时/断开，靠外层 fast path 重试）；
- 最后一律 `unregister` 收尾。

`run_ready` 则简单得多：`is_ready` 轮询，或 `watch` 后睡、`unwatch` 收尾，**绝不 `accept`、绝不占座**。

`&T` 也实现了 `SelectHandle`（透传），这让 `handles` 切片里既能放 `&Sender` 也能放 `&Receiver`，统一成 `&dyn SelectHandle`。

#### 4.2.3 源码精读

`SelectHandle` 的七个方法签名（注释里点明它是给宏用的私有 API）：

[src/select.rs:L99-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123) — `SelectHandle` trait。注意 `try_select` 与 `accept` 都接收 `&mut Token`（写线索），而 `is_ready` 只返回 `bool`（不写线索，因为 ready 家族不负责完成）。

`&T` 的透传实现，让引用也能当 `SelectHandle` 用：

[src/select.rs:L125-L157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L125-L157) — `impl<T: SelectHandle> SelectHandle for &T`：七个方法全部转发到 `**self`。

每个 flavor 究竟如何实现这七个方法，留到 u3-l2 展开；本讲只需把它们当黑盒，关注算法侧如何调用。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把七个方法和「谁调用它」对上号。
2. **步骤**：在 `src/select.rs` 里分别搜索 `handle.try_select`、`handle.register`、`handle.unregister`、`handle.accept`、`handle.is_ready`、`handle.watch`、`handle.unwatch`、`handle.deadline`，看它们出现在 `run_select` 还是 `run_ready`。
3. **预期**：`try_select`/`register`/`unregister`/`accept`/`deadline` 只出现在 `run_select`；`is_ready`/`watch`/`unwatch`/`deadline` 只出现在 `run_ready`。两组几乎不重叠，这正是「两个家族、两套对接方法」的体现。

#### 4.2.5 小练习与答案

- **Q1**：`try_select` 和 `accept` 都往 `Token` 写数据，它们的区别是什么？
  - **A**：`try_select` 是「冷启动试一下」，没有任何前置登记；`accept` 是「我已经被别的线程通过 `Waker` 选中并叫醒了，现在来收割这次选中的成果」。对 array flavor 而言，前者对应「尝试抢占一个槽」，后者对应「读取那个已被对端预留/写好的槽」。
- **Q2**：`register` 返回 `bool` 的含义是什么？算法为什么要这个返回值？
  - **A**：表示「登记的瞬间，操作是否已经就绪」。因为「检查就绪」和「登记进队列」之间存在竞态窗口，flavor 在 `register` 内部把它们合并成原子动作并顺带返回就绪状态，让算法据此不必白白 `park` 一次。
- **Q3**：为什么 `ready` 家族用 `is_ready`/`watch` 而不是 `try_select`/`register`？
  - **A**：`ready` 只想得到「就绪通知」、不打算「占座完成」，它必须允许调用者随后用 `try_recv` 之类自己再试一次（可能失败、需要重试）。`register`/`accept` 这一套是「占座即必须完成」的语义，不适合 ready。

---

### 4.3 run_select：阻塞式选择的全流程（重头戏）

#### 4.3.1 概念说明

`run_select` 是 `select` / `try_select` / `select_timeout` / `select_deadline` 四个公共入口背后**唯一**的阻塞式内核。它接收：

- `handles: &mut [(&dyn SelectHandle, usize, usize)]`：一组三元组 `(操作句柄, index, addr)`。`index` 是给调用者回传的编号，`addr` 是该端的指针地址（完成操作时用来校验「你传进来的端确实是当初注册的那个」）。
- `timeout: Timeout`：三态，决定「不阻塞 / 永远等 / 等到某个时刻」。
- `is_biased: bool`：是否跳过 `shuffle`、按顺序优先。

返回 `Option<(Token, index, addr)>`：`Some` 表示选中了一个操作（携带做它所需的 `Token`），`None` 表示超时/无可选。

#### 4.3.2 核心流程

先看全景伪代码（以「所有操作一开始都阻塞」的典型场景为主线）：

```
fn run_select(handles, timeout, is_biased) -> Option<(Token, usize, usize)>:

  0. 若 handles 为空：按 timeout 睡到点后返回 None（Never 则永久睡）。

  1. 【公平】if !is_biased { utils::shuffle(handles) }   ← 打乱尝试顺序

  2. 【fast path】造一个空 Token，逐个 handle.try_select：
        谁返回 true 就直接 Some((token, i, addr)) 返回。
        （所有操作都阻塞时，这一轮全军覆没，进入 loop。）

  3. loop {
        3a. Context::with(|cx| {
              ── 登记阶段 ──
              逐个 handle.register(oper, cx)：
                · 若 register 返回 true（登记瞬间发现就绪）：
                    尝试 cx.try_select(Aborted)「抢占式放弃等待」：
                      Ok  → 记下 index_ready，sel=Aborted，break
                      Err → 已被别的线程选中，sel=那个状态，break
                · 若 cx.selected() 已被别人改成非 Waiting：break
              ── 睡觉阶段 ──
              若 sel 仍是 Waiting：
                算出最早 deadline（取 timeout 与各 handle.deadline() 的最小值）
                sel = cx.wait_until(deadline)   ← park，直到被 unpark 或超时
              ── 收尾阶段 ──
              对已登记的 handle 逐个 unregister
              match sel {
                Waiting      => unreachable!()
                Aborted      => 若 index_ready 有值：再 try_select 那个具体操作
                Disconnected => {}（不在此完成，留给外层 fast path）
                Operation(_) => 找到匹配的 handle，调 accept 完成它
              }
              返回 Option
        })

        3b. 若 3a 返回 Some：直接返回给调用者。
        3c. 【再次 fast path】再逐个 try_select 一遍（睡醒后世界可能变了）。
        3d. 检查 timeout：Now→返回 None；At 且已过点→返回 None；Never→继续 loop。
     }
```

四个关键设计点：

1. **两处 fast path**（步骤 2 与 3c）：select 在「进入睡眠前」和「被叫醒后」各做一次「逐个 try_select」。前者是常见的乐观命中，后者是「睡醒后先复查一遍，避免又去登记」。
2. **`register` 返回 `true` 即「登记瞬间就绪」**：这是一种防丢失唤醒的精细处理——在 `register` 把自己挂进队列的同一瞬间，消息可能正好到达；flavor 在 `register` 内部会复查一次就绪性，复查到了就返回 `true`，让本线程立刻走「放弃等待、直接抢」的 `Aborted` 路径，而不是傻睡。
3. **`Disconnected` 不在 `match` 里完成**：通道断开只是「唤醒信号」，真正把它翻译成 `RecvError`/`SendError` 留给紧接着的 fast path（步骤 3c）——因为断开的 channel 在 `try_select` 里必然返回 `true`（断开也算就绪）。
4. **`Operation(_)` 才是「被人替我选中」**：此时由 `accept` 完成操作（比如把对端已经替我预留的消息读出来）。

#### 4.3.3 源码精读

`Timeout` 三态枚举：

[src/select.rs:L159-L170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L159-L170) — `Timeout::Now`（不阻塞）/ `Never`（永远等）/ `At(Instant)`（等到某时刻）。

`run_select` 整体签名与「空 handles」短路径：

[src/select.rs:L176-L194](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L194) — 空 handles 时按 timeout 睡眠；注意 `Timeout::Never` 会 `sleep_until(None)` 后 `unreachable!()`（因为永远等且没有操作，理应永不返回）。

公平性：非 biased 时打乱顺序：

[src/select.rs:L196-L199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) — `if !is_biased { utils::shuffle(handles); }`。biased 模式（`select_biased!` / `Select::new_biased`）跳过它，于是「同时就绪时永远先选靠前的」。

fast path——造空 Token 并逐个试：

[src/select.rs:L201-L211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L201-L211) — `let mut token = Token::default();` 后逐个 `handle.try_select(&mut token)`，命中即返回。

登记阶段——`register` 返回 true 时的「抢占式放弃」：

[src/select.rs:L224-L246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L224-L246) — 重点看 `cx.try_select(Selected::Aborted)`：这是「我登记的瞬间发现就绪了，主动放弃等待、待会儿直接抢它」。`Ok` 时记 `index_ready`；`Err(s)` 说明别的线程已经替我选中了，直接采用 `s`。每登记一个就 `cx.selected()` 复查，防止「已被选中却还在登记剩余操作」。

睡觉阶段——算 deadline 并 `wait_until`：

[src/select.rs:L248-L264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L248-L264) — 取 `timeout` 与各 `handle.deadline()`（`at`/`tick` 通道会提供）的最早值，调 `cx.wait_until(deadline)` 真正 park。`Timeout::Now` 在这里直接 `return None`（绝不睡）。

收尾阶段——`unregister` + `match sel`：

[src/select.rs:L266-L297](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L266-L297) — 先对已登记的操作 `unregister`；然后按 `sel` 分支：`Aborted` 且有 `index_ready` 时再 `try_select` 那个操作；`Operation(_)` 时找到匹配 handle 调 `accept`。注意 `Disconnected` 分支体为空——它只负责「不在这里完成」，把翻译工作让给外层 fast path。

`Context::with` 返回后：命中就返回；否则再做一次 fast path，再判超时：

[src/select.rs:L302-L323](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L302-L323) — `if let Some((i,addr)) = res { return Some(...) }`；随后「睡醒后再 fast path 一遍」；最后 `match timeout` 决定返回 `None` 还是继续 `loop`。

四个公共入口如何把 `Timeout` 三态接到 `run_select`：

[src/select.rs:L455-L521](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L455-L521) — `try_select`→`Timeout::Now`，`select`→`Timeout::Never`，`select_timeout`→先 `checked_add` 把 `Duration` 换算 `Instant`（溢出退化为 `Never`）→`Timeout::At`。三者本质都是同一个 `run_select`。

配套的 `Context` 三件套（睡觉与被叫醒的真正实现）：

[src/context.rs:L43-L70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L43-L70) — `Context::with`：用线程本地缓存复用 `Context`（避免每次 select 都分配），`reset()` 把状态归零后运行闭包。

[src/context.rs:L97-L115](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L97-L115) — `Context::try_select`（CAS `Waiting→目标`，失败则返回当前值）与 `selected()`（Acquire 读当前状态）。`run_select` 里一切「状态迁移」都走这两个方法。

[src/context.rs:L143-L169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L143-L169) — `Context::wait_until`：循环里先查状态，没变就 `park`/`park_timeout`；到点了就 `try_select(Aborted)` 自行放弃。这正是「超时 → `Selected::Aborted`」的来源。

而「别的线程怎么把我的状态从 `Waiting` 改成 `Operation`/`Disconnected` 并把我叫醒」：

[src/waker.rs:L82-L111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L82-L111) — `Waker::try_select`：遍历阻塞者队列，挑一个**属于别的线程**的 entry，对其 `cx.try_select(Operation(oper))`，成功则 `store_packet` + `unpark`。这就是 `run_select` 中 `Selected::Operation(_)` 分支的由来。

[src/waker.rs:L153-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L153-L168) — `Waker::disconnect`：把所有阻塞者的 `cx.try_select(Disconnected)`，再 `notify` 观察者。这是 `Selected::Disconnected` 分支的由来。

#### 4.3.4 代码实践（本讲主实践：跟踪一次「全阻塞」的 select）

> 这正是本讲规格里要求的实践任务。

1. **实践目标**：用一份可运行的最小程序，亲历 `shuffle → try_select → register → wait_until → unregister → accept` 的完整时序，并解释 `Aborted` 何时出现。

2. **操作步骤**：把下面这段「示例代码」放进一个 binary（例如 `examples/trace_select.rs`）。它构造一个会合通道 `r1`（操作天然阻塞）与一个会被断开的 `r2`，再开线程延迟投递消息：

   ```rust
   // 示例代码：跟踪一次「所有操作都阻塞」的 select
   use std::thread;
   use std::time::Duration;
   use crossbeam_channel::{bounded, Select};

   fn main() {
       let (s1, r1) = bounded::<i32>(0); // 会合通道，recv 天然阻塞
       let (s2, r2) = bounded::<i32>(0);

       // 200ms 后让 r1 就绪：另起线程接收以完成会合
       let h1 = thread::spawn(move || {
           thread::sleep(Duration::from_millis(200));
           // 这里 send 会阻塞，直到主线程的 select 选中 r1 并 accept
           let _ = s1.send(42);
       });
       let _h2 = thread::spawn(move || {
           let _ = s2; // 持有 s2 一会儿
       });
       drop(s2); // 释放主线程这一侧的 s2，使 r2 走「断开」分支便于对比

       let mut sel = Select::new();
       let i1 = sel.recv(&r1);
       let i2 = sel.recv(&r2);

       println!("[main] 即将 select，两个 recv 一开始都阻塞/断开");
       let op = sel.select(); // 进入 run_select(Timeout::Never)
       match op.index() {
           i if i == i1 => println!("[main] 选中 r1: {:?}", op.recv(&r1)),
           i if i == i2 => println!("[main] 选中 r2(已断开): {:?}", op.recv(&r2)),
           _ => unreachable!(),
       }
       h1.join().unwrap();
   }
   ```

   > 说明：`bounded(0)` 是会合通道（u1-l2、u2-l7）。`s1.send(42)` 因没有接收方而阻塞，直到主线程的 `select` 把 `r1` 注册、进入 `accept`，双方会合成功。`drop(s2)` 让 `r2` 断开，从而 `r2` 在 fast path 里就「就绪」（断开即就绪），很可能主线程一进去就直接选中 `r2`——这是观察「fast path 命中」的好机会。若想强制走「全阻塞→睡觉→叫醒」路径，去掉 `drop(s2)`、让 `s2` 也在另一线程延迟 send 即可。

3. **需要观察的现象 / 对应源码步骤**（按 `run_select` 的顺序填空）：

   | 步骤 | 源码位置 | 在本例中的作用 |
   | --- | --- | --- |
   | `shuffle` | select.rs L196-L199 | 打乱 `(r1, r2)` 的尝试顺序，避免总偏袒某一个 |
   | fast path `try_select` | select.rs L201-L211 | 一开始 r1 阻塞；r2 若已断开则在此命中并返回 |
   | `register` | select.rs L224-L246 | 把「主线程在等 r1/r2」挂进各自 flavor 的阻塞者队列，期间 `cx.selected()` 一直是 `Waiting` |
   | `wait_until` | select.rs L248-L264 / context.rs L143-L169 | deadline=None，`park` 睡眠，200ms 内无 unpark |
   | （别的线程）`Waker::try_select` | waker.rs L82-L111 | s1 端在 `write` 时把主线程的 cx 状态 CAS 成 `Operation`，并 `unpark` |
   | `unregister` | select.rs L266-L269 | 主线程醒来后，把登记的 entry 摘掉 |
   | `accept` | select.rs L284-L296 | 主线程确认是 r1 被选中，调 `accept` 完成会合，把 42 读出来 |

4. **预期结果**：程序在约 200ms 后打印选中 r1（或因 `drop(s2)` 而立刻选中 r2 并得到 `Err(RecvError)`）。若想看到完整「全阻塞」轨迹，去掉 `drop(s2)` 并让两路 send 都延迟。

5. **Aborted 何时出现**（回答实践问题）：在本次「全阻塞 + 对方唤醒」场景里，状态走的是 `Operation` 分支而**不是** `Aborted`。`Aborted` 只在「自己放弃」或「登记窗口内侥幸命中」时出现，共三种来源（详见 4.3.5 Q1）。

6. **运行结果**：待本地验证（取决于 `shuffle` 与线程调度，选中 r1 还是 r2、是否走 fast path 都有随机性，这本身就在演示「公平性」）。

#### 4.3.5 小练习与答案

- **Q1**：`Selected::Aborted` 在 `run_select` 里一共有几种来源？
  - **A**：三种。①登记期间 `register` 返回 `true`（发现就绪），本线程 `cx.try_select(Aborted)` 抢占式放弃，并记 `index_ready`；②`Timeout::At` 到点，`wait_until` 内部 `try_select(Aborted)` 自行放弃（`index_ready` 为 `None`）；③`Timeout::Now`（非阻塞 `try_select`），函数开头就预先 `try_select(Aborted)` 以保证绝不阻塞。
- **Q2**：为什么 `Disconnected` 分支的函数体是空的？
  - **A**：断开只是一个「唤醒理由」，真正把它翻译成 `Err(RecvError)`/`Err(SendError(..))` 是由紧接着的外层 fast path（select.rs L307-L312）中该 flavor 的 `try_select` 完成的——断开的 channel 在 `try_select` 里必然就绪。
- **Q3**：fast path 已经试过一遍 `try_select`，为什么闭包返回 `None` 后（L307-L312）还要再试一遍？
  - **A**：因为闭包返回 `None` 的常见原因是 `Disconnected` 或虚假唤醒——这两种情况下通道状态刚刚变化，此刻很可能已经就绪。再试一次 fast path 能直接命中，避免又一轮登记+park。
- **Q4**：`Context::with` 为什么要用线程本地缓存？
  - **A**：避免每次 select 都 `Arc::new(Inner)`。线程本地存一个 `Context`，`reset()` 后复用，热路径上零分配。

---

### 4.4 run_ready：就绪式选择的对应实现

#### 4.4.1 概念说明

`ready` 家族（`try_ready`/`ready`/`ready_timeout`/`ready_deadline`）背后只有一个内核 `run_ready`。它与 `run_select` 形似神不同：

- **形似**：同样是 `shuffle` → 自旋/登记 → `wait_until` → 收尾 的骨架，同样返回 `Option`。
- **神不同**：它返回的是 `Option<usize>`（就绪操作的 index），**不**返回 `Token`、**不**调用 `accept`、**不**「占座」。这意味着调用者拿到 index 后还得自己用 `try_recv` 之类再试一次——而且可能失败（操作又被别人抢走了），所以文档反复强调「可能虚假唤醒，需要重试」。

为什么需要这套「不占座」的 API？因为它允许调用者「知道哪个通道可能有戏，但暂时不承诺完成」——适合需要结合其他逻辑再决定是否真正消费的场景。

#### 4.4.2 核心流程

```
fn run_ready(handles, timeout, is_biased) -> Option<usize>:

  0. 空 handles：按 timeout 睡到点返回 None。
  1. if !is_biased { shuffle(handles) }
  2. loop {
       2a. 【自旋探测】用 Backoff 自旋，反复 is_ready：
             命中就返回 Some(index)。
       2b. Backoff 用尽仍无就绪：检查 timeout（Now→None；At 过点→None）。
       2c. Context::with(|cx| {
             逐个 handle.watch(oper, cx)：登记成「就绪观察者」
               · watch 返回 true（登记瞬间就绪）→ cx.try_select(Operation(oper)) 占座，break
               · cx.selected() 非 Waiting → break
             若仍 Waiting：算 deadline，wait_until
             逐个 handle.unwatch(oper) 收尾
             match sel { Operation(_) => 找到匹配 handle 返回 Some(*i); 其余 => None }
       })
       2d. 命中就返回。
     }
```

两个值得注意的差异：

1. `run_ready` 在登记前多了一段 **`Backoff` 自旋**（select.rs L352-L367），而 `run_select` 没有。这是因为 `ready` 不占座、开销低，多自旋一会儿、少挂一次线程反而划算。
2. `watch`/`unwatch` 操作的是 `Waker` 的 **observers** 队列（而非 selectors），就绪时由 `Waker::notify` 只做「通知+unpark」，不替你 `store_packet`、不替你完成。

#### 4.4.3 源码精读

`run_ready` 的自旋探测段（`ready` 家族独有的快速轮询）：

[src/select.rs:L352-L367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L352-L367) — 用 `Backoff::snooze` 自旋，逐个 `handle.is_ready()`，命中即返回 index；`backoff.is_completed()` 才退出自旋进入登记。

`watch` 登记段（对比 `run_select` 的 `register`）：

[src/select.rs:L385-L404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L385-L404) — `handle.watch(oper, cx)` 返回 `true` 时 `cx.try_select(Operation(oper))` 占座。注意它不像 `run_select` 那样用 `Aborted` + `index_ready`，而是直接用 `Operation(oper)` 表达「就绪的就是它」。

收尾与 match（只处理 `Operation`，且不 `accept`）：

[src/select.rs:L424-L441](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L424-L441) — `unwatch` 收尾；`Operation(_)` 分支找到匹配 handle 直接返回 `Some(*i)`——没有任何 `accept`/`Token`，印证「不占座、不完成」。

对照 `Waker` 中 observers 的通知路径：

[src/waker.rs:L143-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L143-L151) — `Waker::notify`：对 observers 只做 `try_select(Operation) + unpark`，不递包、不完成，与 `run_ready` 的「只通知」语义吻合。

#### 4.4.4 代码实践（源码阅读 + 行为对比型）

1. **目标**：体会「`ready` 可能虚假唤醒，必须重试」。
2. **步骤**：阅读 `Select::ready` 的文档示例（`src/select.rs` 中 `pub fn ready` 上方的 doc comment，约 L1016-L1061），它正是 `loop { let index = sel.ready(); let res = rs[index].try_recv(); if let Err(e)=res { if e.is_empty() { continue } } return ... }` 这种「ready + try_recv + 重试」模式。
3. **观察**：对比 `Select::select` 的示例——它拿到 `SelectedOperation` 后直接 `op.recv(&r)`，**没有**重试循环，因为 select 已经「占座」、必然成功。
4. **预期**：你能用自己的话说出「为什么 `ready` 要配 `try_recv` + 重试，而 `select` 不用」。
5. **运行结果**：纯阅读；若想实测，可在多接收端并发场景下偶发观察到 `ready` 返回 index 后 `try_recv` 得到 `Empty`。

#### 4.4.5 小练习与答案

- **Q1**：`run_ready` 为什么在登记前要 `Backoff` 自旋，而 `run_select` 不自旋？
  - **A**：`ready` 不占座、单次开销小，自旋等一小会儿可以避免挂起/唤醒线程的昂贵代价；`select` 一旦占座就要走完整的 `accept`+`read/write`，且需要尽快挂进入阻塞者队列以避免丢唤醒，所以直接进入登记。
- **Q2**：`ready` 返回的 index 一定意味着随后 `try_recv` 能成功吗？
  - **A**：不一定。`ready` 只承诺「就绪通知」，在你调用 `try_recv` 之前，消息可能被另一个接收者抢走，或通道恰好排空，导致 `TryRecvError::Empty`。所以文档要求调用者用重试循环。
- **Q3**：`ready` 家族为什么把操作登记进 `observers` 而不是 `selectors`？
  - **A**：`selectors` 会被 `Waker::try_select` 选中并配对（占座位），`observers` 只会被 `Waker::notify`/`disconnect`「通知就绪」而不配对。`ready` 只想要通知，故用 `observers`。

---

### 4.5 公平性深入：shuffle 与 biased 的实现

#### 4.5.1 概念说明

`run_select` 与 `run_ready` 都在开头调用 `utils::shuffle(handles)`（除非 `is_biased`）。这一步把「尝试顺序」随机化，从而在多个操作**同时就绪**时，不会总偏袒列表里靠前的那一个——这就是 `select!` 文档里说的「random / 公平」。

`biased` 模式（`select_biased!` 宏 / `Select::new_biased`）则**跳过** shuffle，于是「同时就绪时永远先尝试靠前的」——这是确定性的、按声明顺序的优先级，方便某些需要严格优先级的场景（如「先处理关闭信号，再处理数据」）。

#### 4.5.2 核心流程

`shuffle` 用的是经典的 **Fisher–Yates** 洗牌，但有两个工程优化：

1. **随机数源**：线程本地的 32 位 **Xorshift** 伪随机数发生器（种子固定，目的是「快」和「分布够乱」，而非密码学安全）。
2. **取模优化**：把 `x % n` 换成 Daniel Lemire 的「乘法 + 移位」无除法取模。设 \( x \) 为 32 位随机数（视作 \( 0..2^{32} \) 的整数），\( n = i + 1 \) 为当前要交换的范围大小，则交换目标下标为：

   \[ j = \left\lfloor \frac{x \cdot n}{2^{32}} \right\rfloor \]

   在 `for i in 1..len` 的每一轮把第 `i` 个元素和随机一个 `j \in 0..=i` 交换，最终得到均匀的随机排列。

一个关于「公平」的直觉：若同时有 \(k\) 个操作就绪，shuffle 后每个操作排在「最先被尝试」位置的概率约为 \(1/k\)，因此在 fast path 里它被选中的概率也近似 \(1/k\)，长期频率收敛到均匀。

#### 4.5.3 源码精读

`utils::shuffle` 的完整实现：

[src/utils.rs:L7-L40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L7-L40) — 线程本地 `Cell<Wrapping<u32>>` 作 Xorshift RNG；内层循环三步异或（`<<13 / >>17 / <<5`）推进；Lemire 取模 `((x as u64).wrapping_mul(n as u64) >> 32)` 得到 `j`；`v.swap(i, j)` 完成 Fisher–Yates 的一步。

`run_select` 调用它的位置（biased 跳过）：

[src/select.rs:L196-L199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) — `if !is_biased { utils::shuffle(handles); }`。`run_ready` 在 [src/select.rs:L347-L350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L347-L350) 有完全相同的一句。

`Select::new_biased` 如何把 `biased` 标志传到内核：

[src/select.rs:L651-L670](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L651-L670) — `new_biased` 设 `biased: true`；随后每个 `try_select`/`select`/`select_timeout`/`ready*` 都把它作为 `is_biased` 透传给 `run_select`/`run_ready`。

#### 4.5.4 代码实践（行为观察型）

1. **目标**：直观看到 shuffle 带来的「均匀」，以及 biased 的「恒定优先」。
2. **步骤**：写一个测试——建两个 `unbounded` 通道，都预先 `send` 一条消息（两个 recv 操作同时就绪），用 `Select::new()` 在循环里选 10000 次，统计选中 r1 与 r2 的次数；再换成 `Select::new_biased()` 各统计一次。
3. **观察**：公平模式下两者应各约占 50%；biased 模式下应几乎 100% 选中靠前的那个。
4. **预期**：公平模式比例接近 1:1（Xorshift 种子固定，但跨多次调用与多线程场景下分布足够均匀）；biased 模式高度偏向 index 小者。
5. **运行结果**：待本地验证（具体数字取决于运行，但定性差异稳定）。

#### 4.5.5 小练习与答案

- **Q1**：`shuffle` 用固定种子的 Xorshift，会不会导致「不公平」？
  - **A**：单线程看，序列是确定的；但每个线程的 RNG 是线程本地的、且每次 select 都会推进它，跨多次调用与多线程场景下分布足够均匀，满足「不系统性偏袒某一操作」的公平性目标。它不是密码学随机，但 select 不需要那个强度。
- **Q2**：Lemire 取模相比 `x % n` 好在哪？
  - **A**：整数除法/取模在 CPU 上比乘法+移位慢得多；Lemire 法用一次 32×32→64 乘法加一次移位替代除法，在 select 这种热路径上是可观的加速。
- **Q3**：`biased` 模式跳过 shuffle，会不会损害正确性？
  - **A**：不会，只影响「公平性」。shuffle 仅用于在多个同时就绪的操作里随机挑一个；跳过它只是改成「按固定顺序挑」，仍是正确的仲裁，只是不再均匀随机。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个小任务：**「带超时的多源 select + 分支预测」**，亲手触发 `run_select` 的不同分支。

**需求**：主线程同时等待三个事件——

1. 一个 `bounded(2)` 数据通道 `r_data`（偶尔有数据）；
2. 一个会合通道 `r_hand`（`bounded(0)`，演示「全阻塞→被叫醒→accept」）；
3. 一个 500ms 的 `after` 超时（演示 `Timeout::At` 与 `Selected::Aborted` 的超时来源）。

**示例代码骨架**（非项目原有，仅供练习）：

```rust
use std::{thread, time::Duration};
use crossbeam_channel::{bounded, after, Select};

let (s_data, r_data) = bounded::<i32>(2);
let (s_hand, r_hand) = bounded::<i32>(0); // 会合
let timeout_rx = after(Duration::from_millis(500));

// 场景三选一运行，观察命中哪条分支：
// A) 提前 s_data.send(1)              → fast path (select.rs L201-L211)
// B) 不 send 任何数据，让 after 兜底   → 超时 Aborted via wait_until (context.rs L143-L169)
// C) 另一线程 sleep(50ms) 后 s_hand.send(2)（并需有接收方配合会合）
//                                    → register→wait_until→unregister→accept (select.rs L224-L296)

let mut sel = Select::new();
let i_data = sel.recv(&r_data);
let i_hand = sel.recv(&r_hand);
let i_to   = sel.recv(&timeout_rx);

match sel.select() { // 简化：用 select_timeout 也行
    op if op.index() == i_data => println!("数据: {:?}", op.recv(&r_data)),
    op if op.index() == i_hand => println!("会合: {:?}", op.recv(&r_hand)),
    op if op.index() == i_to   => { op.recv(&timeout_rx); println!("超时"); }
    _ => unreachable!(),
}
```

**要求**：

1. 分别构造 A/B/C 三种场景运行，记录每次走的是 `run_select` 的哪一段（fast path / 超时 Aborted / 阻塞唤醒 accept）。
2. 对场景 C，画出「主线程状态机」时间线：`Waiting →（对方 send→Waker::try_select→CAS+store_packet+unpark）→ Operation(op) → unregister → accept`。
3. 把 `Select::new()` 换成 `Select::new_biased()`，在 A 场景里两个通道都提前放消息，观察是否总是命中靠前的 index（验证 biased 跳过 shuffle）。
4. 注意：`SelectedOperation` 必须「完成」（`op.recv`/`op.send`），否则 drop 时 panic（见 select.rs 中 `impl Drop for SelectedOperation`）。

**预期产出**：一张表，记录每个轮次命中的分支（fast path / register→wait→accept / timeout→Aborted），并能对照 select.rs 的行号指出该轮走过的代码段。**运行结果待本地验证**，因为线程调度有随机性——这种随机性恰恰来自本讲的 `shuffle` 与操作系统的线程调度。

## 6. 本讲小结

- `Selected`（`Waiting`/`Aborted`/`Disconnected`/`Operation`）是一台塞进单个 `AtomicUsize` 的四态状态机，所有迁移靠 CAS，是「至多一个赢家」的根本保证；`Operation` 用变量地址当编号并断言 `>2` 以避让 `0/1/2`。
- `SelectHandle` trait 是 select 算法与各 flavor 之间的契约：算法只用 `try_select/register/unregister/accept`（select 路径）和 `is_ready/watch/unwatch`（ready 路径）外加 `deadline`，对 array/list/zero 一视同仁。
- `run_select` 的核心是「fast path 抢占 → 登记 + 防漏唤醒复查 → wait_until 阻塞 → 注销 → 醒后 accept」循环；`Timeout` 三态（`Now`/`Never`/`At`）让 `try_select`/`select`/`select_timeout`/`select_deadline` 共用同一套实现。
- `accept` 只在「被别的线程选中」（状态为 `Operation`）时登场，负责把对方通过 `Waker::try_select` 递来的 packet/token 接过来——这是会合通道能融入 select 的关键。
- `Aborted` 出现于三种时机：非阻塞模式自我标记、登记窗口内侥幸命中（配 `index_ready`）、超时到点；它表示「自己放弃」而非「被选中」。
- `run_ready` 只通知就绪、不占座位，因此更轻但可能虚假唤醒，需调用者用 `try_recv` 重试；公平性由 `utils::shuffle`（Xorshift + Lemire 无除法取模的 Fisher–Yates）提供，`biased` 模式跳过 shuffle 实现优先级。

## 7. 下一步学习建议

- **u3-l2 SelectHandle trait 与 flavor 对接**：本讲把 `try_select`/`register`/`accept` 当成黑盒调用；下一讲进入 array/list/zero 各 flavor，看它们如何具体实现这七个方法、如何为 select 准备 `Token`，重点看 zero flavor 的 `accept` 如何接收 packet。
- **u3-l3 select! 宏展开机制**：本讲的 `internal::select`/`try_select`/`select_timeout` 是如何被 `select!` 宏拼装出来的、单分支如何退化为直接 `recv`/`try_recv`/`recv_timeout`，留待宏讲义。
- **延伸阅读**：建议带着本讲的流程图，回到 `tests/select_macro.rs` 与 `tests/golang.rs`（u3-l8），对照真实测试用例验证你对 `Aborted`/`Operation`/超时分支的理解；并可尝试用 `cargo expand` 展开一个 `select!`，看它生成的对 `run_select` 的调用与本讲描述是否一致。
- 若想先看「调用者如何完成 `SelectedOperation`」，可读 [src/select.rs:L1276-L1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1318) 的 `send`/`recv` 完成方法，注意它们用 `mem::forget(self)` 阻止 `Drop` 的 panic。
