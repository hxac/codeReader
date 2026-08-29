# u5-l5 睡眠与唤醒协议

## 1. 本讲目标

上一讲（u5-l4）我们读完了 `find_work` 的三级找活优先链，但留下了一个尾巴：**当本地队列、所有同伴的队列、全局注入队列全部落空时，线程去哪里？** 本讲回答这个问题。

线程池里最理想的状态是「有活干活、没活睡觉」：如果找不到工作就一直自旋空转，空闲的线程池会把 CPU 烧到 100%，什么都算不出来。但如果睡得太死，新任务来了没人接，轻则延迟飙升，重则全池睡死、外部任务永远无人处理（死锁）。Rayon 用一个约 300 行的 `sleep` 模块 + 一个被压缩进单个 `usize` 的三合一原子计数器，精巧地平衡了这两者。

学完本讲，你应该能够：

1. 说出 `AtomicCounters` 把哪三个计数器打包进一个机器字，以及它们各自维护的不变量。
2. 画出工作线程「活跃 → 空闲 → 犯困 → 睡着」的完整状态机，包括驱动它的 33 轮计数与 `CoreLatch` 的四态转移。
3. 解释任务发布者如何决定唤醒几个线程、`Mutex<bool>` + `Condvar` 如何保证唤醒信号不丢失，以及两处 `seq-cst` fence 为什么能证明「全池睡死」不可能发生。

## 2. 前置知识

本讲会用到几个并发原语，先用两三句话把直觉建立起来：

- **原子操作与 CAS**：`fetch_add` / `fetch_sub` 是不可分割的「读-改-写」；`compare_exchange`（CAS）则是「我赌旧值是 X，赌对了就换成 Y，赌错了告诉我失败」。多个线程同时改一个计数器时，只有原子操作能保证不丢更新。
- **把多个字段压进一个字（bit packing）**：如果 `sleeping`、`inactive`、`JEC` 是三个独立的 `AtomicUsize`，就没办法「原子地同时」检查一个、修改另一个——中间总有缝隙会让别的线程钻进来。压进同一个字后，一次 CAS 就能同时锁定三个字段的快照。这是本讲一切不变量的物理基础。
- **`Mutex<bool>` + `Condvar`（管程风格等待）**：经典的三步曲——加锁、在锁内检查条件（`while *is_blocked { ... }`）、`condvar.wait(guard)` 原子地「释放锁并入睡」，醒来时重新拿回锁再查一遍条件。因为检查在锁内，通知不会落在「检查完、还没睡下」的缝隙里。
- **内存序与 fence**：`Release`/`Acquire` 只保证「如果读到了那次写，就能看到它之前的所有写」，但**不保证你一定读得到**。`Ordering::SeqCst`（顺序一致）的 fence 则把所有线程的这些操作排成一条全局全序，这正是后面防死锁证明的支点。
- **承接前讲**：u5-l4 讲过的 `find_work` 三级优先链（本地 → 窃取 → 注入队列）、u5-l2 讲过的 `CoreLatch`「单向一次性信号」，以及 u5-l3 讲过的「工作线程阻塞在 terminate 锁存器上边等边干活」，都会在本讲被逐行兑现。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [rayon-core/src/sleep/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md) | 官方协议文档：状态定义、计数器语义、睡眠/发布协议全文、防死锁证明梗概。本讲的「规格说明书」 |
| [rayon-core/src/sleep/counters.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs) | `AtomicCounters`：单个 `AtomicUsize` 里打包三个计数器，及全部读写原语与不变量断言 |
| [rayon-core/src/sleep/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs) | `Sleep` 结构体：睡眠状态机（`start_looking` / `no_work_found` / `sleep`）与唤醒决策（`new_jobs` / `wake_specific_thread`） |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 调用方：`wait_until_cold` 的空闲循环、`push`/`inject` 处的唤醒钩子 |
| [rayon-core/src/latch.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs) | `CoreLatch` 四态状态机（UNSET/SLEEPY/SLEEPING/SET），与 `SpinLatch::set` 触发的定向唤醒 |

## 4. 核心概念与源码讲解

### 4.1 计数器不变量：一个机器字里的三个账本

#### 4.1.1 概念说明

睡眠协议的核心难题是一个**竞态窗口**：

- 线程 A 找了一圈工作，没找到，准备睡觉；
- 恰在此刻线程 B 发布了新任务，B 环顾四周：「没有人在睡啊」，于是不唤醒任何人；
- 线 A 安然入睡——新任务可能就永远没人管了。

要堵住这个窗口，A 在「宣布要睡」和「真正睡着」之间必须能原子地检查「这期间有没有新任务发布」。Rayon 的解法是把**所有相关状态放进同一个原子字**，让「检查 JEC + 睡眠人数 +1」能用一次 CAS 完成。这个字就是 `AtomicCounters`，它同时是三个账本：

1. **睡眠线程数**（sleeping）：正阻塞在条件变量上的线程个数；
2. **不活跃线程数**（inactive）：空闲 + 犯困 + 睡着的总数，即「没在执行任务」的线程个数；
3. **任务事件计数器**（JEC, Jobs Event Counter）：不真正数任务个数，而是用一个**奇偶位**来广播「自我上次有人犯困以来，有没有新任务发布过」。

#### 4.1.2 核心流程

位布局（64 位平台，`THREADS_BITS = 16`）：

```
usize（64 位）
┌────────────────────────┬────────────────┬────────────────┐
│  bits 32..             │  bits 16..32   │  bits 0..16    │
│  JEC（任务事件计数器）   │  inactive 不活跃 │  sleeping 睡眠 │
└────────────────────────┴────────────────┴────────────────┘
   高位字段加法只会 wrap 自身，          inactive = idle + sleepy + sleeping
   永远不会溢出污染低位字段              （不变量：sleeping ≤ inactive）
```

由此派生的关键不变量：

- **不变量一（包含关系）**：`sleeping ≤ inactive` 恒成立。`inactive` 在线程进入空闲循环时 +1、找到工作时 −1，而「犯困」「睡着」只动 `sleeping` 不动 `inactive`。于是「醒了但空闲」（idle）的线程数可以一步算出：`awake_but_idle = inactive − sleeping`。
- **不变量二（奇偶广播）**：JEC 为**偶数**＝「困」（最近一次自增来自某个要睡的线程）；为**奇数**＝「活跃」（最近一次自增来自某个发任务的线程）。发任务者看到偶数就 +1 拉奇，犯困者看到奇数就 +1 拉平——双方永远在互相「盖章」。
- **不变量三（自环溢出）**：JEC 居最高位字段，`wrapping_add` 只会绕回自身，永远不会进位踩坏两个线程计数器。

计数器随线程状态的增减（摘自官方 README）：

| 事件 | 操作 |
| --- | --- |
| 线程进入空闲（开始找活） | `inactive +1` |
| 线程真正睡着 | `sleeping +1` |
| 有人唤醒一个睡眠线程 | `sleeping −1`（**由唤醒者扣减**，不是被唤醒者） |
| 线程找到工作、退出空闲 | `inactive −1` |

#### 4.1.3 源码精读

先看结构体与位布局声明：

[counters.rs:3-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L3-L16) —— `AtomicCounters` 用一个 `AtomicUsize` 按低位到高位依次存放睡眠数、不活跃数、JEC；注释假设 `THREADS_BITS = 10` 举例，实际 64 位平台取 16（见下一条链接）。

[counters.rs:55-87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L55-L87) —— 定义位宽与「一步加一」常量：64 位平台 `THREADS_BITS = 16`（32 位为 8），因此 `THREADS_MAX = 65535`；`ONE_SLEEPING = 1`、`ONE_INACTIVE = 1 << 16`、`ONE_JEC = 1 << 32`。给整个字加上 `ONE_INACTIVE` 就是不活跃数 +1，其余字段纹丝不动——这就是「打包」带来的原子性红利。

JEC 的奇偶语义封装在 `JobsEventCounter` 里：

[counters.rs:37-53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L37-L53) —— `is_sleepy()` 判断最低位是否为 0（偶数＝困），`is_active()` 取反。注意这里说的「sleepy/active」修饰的是**计数器自身的状态**，不是某个线程的状态。

三个账本的读法：

[counters.rs:240-261](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L240-L261) —— `inactive_threads()` 与 `sleeping_threads()` 用移位加掩码取出对应位段；`awake_but_idle_threads()` 先用 `debug_assert` 断言不变量一（`sleeping ≤ inactive`）再作差——差值就是「醒着但正在找活」的线程数，唤醒决策要靠它。

写路径的原语族：

[counters.rs:119-122](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L119-L122) —— `add_inactive_thread()`：进入空闲循环时无条件 `fetch_add(ONE_INACTIVE)`，注释明确说明从 idle 到 sleepy/sleeping **不会**扣减它，这正是不变量一的来源。

[counters.rs:124-144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L124-L144) —— `increment_jobs_event_counter_if(pred)`：CAS 循环，只要当前 JEC 满足 `pred`（发任务者传 `is_sleepy`、犯困者传 `is_active`）就整体 +1 并重试，直到不满足为止。返回的 `Counters` 保证其 JEC 已处于「对方盖章之后」的状态，供调用方据此做唤醒决策。这是奇偶广播协议的唯一写入口。

[counters.rs:151-170](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L151-L170) —— `sub_inactive_thread()`：找到工作时把不活跃数 −1（两条 `debug_assert` 守护不变量一），然后返回 `min(sleeping, 2)` —— 这是当前的唤醒启发式：**每有一个空闲线程找到工作，就顺手叫醒至多两个睡眠者**。

[counters.rs:176-207](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L176-L207) —— `sub_sleeping_thread()` 只允许在确知 `sleeping > 0` 时调用（安全前提见函数注释）；`try_add_sleeping_thread()` 用 CAS 尝试把睡眠数 +1，**失败意味着计数器被别人改过**（比如 JEC 变了），调用方必须重读再试——这两个函数合起来就是 4.2 节睡眠临界区的主角。

[counters.rs:226-233](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L226-L233) —— `increment_jobs_counter()` 对 JEC 字段做 `wrapping_add`，注释点明因为 JEC 占最高位字段，绕回只影响自己——不变量三的出处。

#### 4.1.4 代码实践

**实践目标**：亲手验证你理解了位打包，能把任意一个 `word` 值拆回三个账本。

**操作步骤**（示例代码，非项目源码）：

```rust
// 示例代码：复刻 counters.rs 的位运算，验证位布局理解
const THREADS_BITS: usize = 16;
const THREADS_MAX: usize = (1 << THREADS_BITS) - 1;
const INACTIVE_SHIFT: usize = 1 * THREADS_BITS;
const JEC_SHIFT: usize = 2 * THREADS_BITS;

fn select_thread(word: usize, shift: usize) -> usize {
    (word >> shift) & THREADS_MAX
}
fn select_jec(word: usize) -> usize {
    word >> JEC_SHIFT
}

fn main() {
    // 模拟：3 个睡眠、5 个不活跃、JEC = 7（奇数 = 活跃）
    let word: usize =
        3                                // sleeping
        | (5 << INACTIVE_SHIFT)          // inactive
        | (7usize << JEC_SHIFT);         // JEC
    println!("sleeping = {}", select_thread(word, 0));
    println!("inactive = {}", select_thread(word, INACTIVE_SHIFT));
    println!("JEC      = {}", select_jec(word));
    println!("awake_but_idle = {}", 5 - 3);
}
```

**需要观察的现象**：输出应为 `sleeping = 3`、`inactive = 5`、`JEC = 7`、`awake_but_idle = 2`；且 `7 & 1 == 1`，即 JEC 处于「活跃」态。

**预期结果**：把 `word` 换成 `counters.rs` 中 `Debug` 实现打印的任意十六进制值（[counters.rs:264-274](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L264-L274) 的 `word` 字段格式即 `{:016x}`），你的拆解结果应与 Debug 输出的 `jobs`/`inactive`/`sleeping` 三个字段完全一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么把三个计数器分成三个独立的 `AtomicUsize` 不行？

**答案**：睡眠协议需要在一次原子操作里**同时**检查 JEC 是否变化并把睡眠数 +1（`try_add_sleeping_thread` 的 CAS），或同时读出「醒着空闲数」与「睡眠数」做唤醒决策。拆成三个字后，任何「先读 A 再改 B」的组合中间都有缝隙，别的线程可以插进来修改，4.1.1 节描述的丢失唤醒竞态就会重新出现。单字打包让三字段快照天然原子。

**练习 2**：`sub_inactive_thread` 为什么要返回 `min(sleeping, 2)` 而不是把睡眠者全叫醒？

**答案**：见 [counters.rs:166-169](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L166-L169) 的注释——这是当前启发式：一个空闲线程找到了工作，说明系统里可能有可窃取的任务，于是叫醒少量帮手（至多 2 个）去碰运气；全叫醒则可能在任务其实不足时造成一轮「集体醒来又集体睡去」的抖动（thundering herd）。

**练习 3**：JEC 会不会加到溢出、进而破坏两个线程计数字段？

**答案**：不会。JEC 位于最高位字段，`increment_jobs_counter` 用的是 `wrapping_add`，只会绕回自身（[counters.rs:228-232](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L228-L232) 的注释）。代价是 JEC 绕回后可能出现「值恰好相等」的极小概率误判，这正是 4.3 节还要兜底防死锁的原因之一。

### 4.2 睡眠状态机：从「找不到活」到「睡在条件变量上」

#### 4.2.1 概念说明

一个工作线程在生命周期里处于四种状态（README 官方定义）：

- **active（活跃）**：正在执行任务；
- **idle（空闲）**：正在搜索工作——弹本地队列、窃取同伴、翻注入队列；
- **sleepy（犯困）**：仍属空闲、还在搜索，但已「预告」即将入睡（通过 JEC 盖章）；
- **sleeping（睡着）**：阻塞在自己的条件变量上，等待被唤醒。

后三者合称 **inactive**。注意「犯困」是一个精心设计的中间态：犯困线程**还没有睡**，它只是把 JEC 从奇数拉成偶数并记住这个值；如果随后有人发布任务，JEC 会被拉回奇数，犯困线程在真正入睡前检查 JEC 就会发现「有新活」，放弃睡觉继续搜索。

与这个「轮次状态机」并行运转的还有上一讲（u5-l2）见过的 `CoreLatch` 四态机：`UNSET → SLEEPY → SLEEPING → SET`。两者配合完成「安全入睡」：latch 负责「我正要睡/已睡着」的可见性，轮次负责「先找多少轮再睡」的节奏控制。

#### 4.2.2 核心流程

**外层节奏（`no_work_found` 驱动）**——`wait_until_cold` 的空闲循环每空手而归一次就调用一次 `no_work_found`：

```
rounds = 0
每轮 find_work 落空 → no_work_found：
  rounds < 32   → yield_now，rounds += 1        # 前 32 轮：边让出 CPU 边找
  rounds == 32  → announce_sleepy()             # 记住 JEC（拉平成偶数），rounds = 33
  rounds == 32 之后再空手一轮                    # 第 33 轮仍无活：
  rounds == 33  → sleep()                       # 进入入睡临界区
```

**入睡临界区（`sleep()`）**：

```
① latch.get_sleepy()          # CAS UNSET→SLEEPY；失败说明 latch 已被 set，直接返回
② lock(is_blocked)            # 先拿互斥锁！（原因见 4.2.3）
③ latch.fall_asleep()         # CAS SLEEPY→SLEEPING；失败说明等待的事已发生 → 完全清醒返回
④ loop:
     counters = load()
     若 JEC != 犯困时记住的值 → wake_partly（rounds 回到 32，再搜一轮）返回
     try_add_sleeping_thread(counters) 成功 → break   # sleeping +1 已原子登记
⑤ fence(SeqCst)               # 给 4.3 节的防死锁证明立下全序点
⑥ has_injected_jobs()?        # 入睡前后最后看一眼注入队列
     有 → sub_sleeping_thread() 自我撤销，回到找活循环
     无 → is_blocked = true; while is_blocked { condvar.wait }   # 真正睡着
⑦ 醒来后：wake_fully（rounds 归零）+ latch.wake_up（SLEEPING→UNSET）
```

被唤醒的三种出口，对应 `IdleState` 两个复位函数：

- **`wake_fully`**：rounds 归零，从头再数 32 轮——用于「等到的事发生了」或「被唤醒」；
- **`wake_partly`**：rounds 置回 32——用于「JEC 变了但还没找到活」，只再搜一轮就重新尝试入睡，避免又空转 32 轮。

#### 4.2.3 源码精读

状态与常量定义：

[mod.rs:21-27](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L21-L27) —— `Sleep` 结构体：每个 `Registry` 内嵌一个（[registry.rs:128](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L128)），持有「每线程一份睡眠状态」向量与全局共享的 `counters`。

[mod.rs:34-54](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L34-L54) —— `IdleState`（线程的空闲身份：worker 编号、已空转轮次、犯困时记住的 JEC）与 `WorkerSleepState`（`is_blocked: Mutex<bool>` + 每线程一个 `Condvar`）。`IdleState` 的存在本身就是「该线程当前空闲」的凭证——被消费（找到工作）即失效。

[mod.rs:56-57](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L56-L57) —— 节奏常量：先空转找 `ROUNDS_UNTIL_SLEEPY = 32` 轮才犯困，第 33 轮仍无活才真正尝试入睡。这段「梯度退避」让短暂的任务间隙不至于惊动内核（yield 即可），只有持续无活才付出睡眠的系统调用代价。

轮次状态机的驱动器：

[mod.rs:87-108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L87-L108) —— `no_work_found` 按 `rounds` 四分支：前 32 轮只 `yield_now` 递增计数；第 32 轮调 `announce_sleepy` 记住 JEC；最后一格断言 `rounds == ROUNDS_UNTIL_SLEEPING` 后进入 `sleep`。注意每个分支都让出了 CPU（`thread::yield_now`），自旋并不烧满核心。

[mod.rs:110-115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L110-L115) —— `announce_sleepy`：调 4.1 节的 `increment_jobs_event_counter_if(is_active)`，把 JEC 从奇（活跃）拉成偶（困）并返回记住的终值。

入睡临界区全文：

[mod.rs:117-139](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L117-L139) —— `sleep()` 开头三步：`get_sleepy` CAS 失败即返回；**先锁 `is_blocked` 再做后续**；`fall_asleep` 失败则 `wake_fully` 返回。锁的顺序至关重要——源码在 [mod.rs:180-184](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L180-L184) 的注释里解释：互斥锁在「递增睡眠计数之前」就被持有，因此任何前来唤醒的线程要么看到 `is_blocked == false`（我还没睡下，notify 不会白打），要么被挡在锁外直到我进入 `wait` 释放锁——唤醒信号不可能掉进缝隙。

[mod.rs:141-160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L141-L160) —— 循环检查 JEC 并登记入睡：若计数器里的 JEC 已不等于犯困时记住的值，说明「有新任务发布但我没搜到」，`wake_partly`（rounds 回到 32）+ `latch.wake_up` 后返回重搜；否则用 `try_add_sleeping_thread` 的 CAS 把睡眠数 +1，CAS 失败（他人改了计数器）则重读再来。**「检查 JEC」与「+1」在同一字上原子完成**，4.1.1 的竞态窗口就此焊死。

[mod.rs:162-189](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L162-L189) —— 登记成功后的兜底与真正入睡：先 `fence(SeqCst)`，再以 `has_injected_jobs` 最后检查一次注入队列——有则 `sub_sleeping_thread` 自我撤销（注释说明：通常扣减由唤醒者执行，这里是唯一例外，因为我是「自己叫醒自己」）；无则置 `is_blocked = true` 并在 `while *is_blocked { condvar.wait }` 上阻塞。最后一段「最后检查」防的是 JEC 绕回导致漏看外部注入、进而全池睡死的极端情形，详见 4.3.3。

[mod.rs:314-324](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L314-L324) —— 两个复位函数：`wake_fully` 归零重来，`wake_partly` 回到犯困临界点（`rounds = ROUNDS_UNTIL_SLEEPY`），二者都把 `jobs_counter` 重置为 `DUMMY`。

CoreLatch 一侧的四态机：

[latch.rs:57-69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L57-L69) —— 四个状态常量：`UNSET`（未置位、主人醒着）、`SLEEPY`（未置位、主人正要睡）、`SLEEPING`（未置位、主人已睡、必须被唤醒）、`SET`（已置位）。

[latch.rs:90-117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L90-L117) —— 三个转移函数全部用 SeqCst CAS：`get_sleepy`（UNSET→SLEEPY）、`fall_asleep`（SLEEPY→SLEEPING）、`wake_up`（SLEEPING→UNSET，仅在未 SET 时）。[latch.rs:125-135](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L125-L135) 的 `set` 用 `swap(SET)` 并返回旧态是否为 SLEEPING——即「是否需要去唤醒主人」，这是 4.3 节定向唤醒的触发器。

调用方的整合点：

[registry.rs:779-817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L779-L817) —— `wait_until_cold`：外层先弹本地活（注释点明「避免无谓改动共享睡眠状态」），然后 `start_looking`（[mod.rs:68-77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L68-L77)，内部即 `add_inactive_thread`），内层循环 `find_work` 有活则 `work_found` 执行任务，无活则 `no_work_found` 推进状态机。当 latch 置位导致内层退出时，也要补一次 `work_found`——注释原话：「就算我们之前只是犯困，现在也不困了——我们『找到工作』了，即外围线程等待的那件事」。

[registry.rs:913-933](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L913-L933) —— `main_loop` 收尾处调 `wait_until_out_of_work`（[registry.rs:819-833](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L819-L833)），即 `wait_until(terminate latch)`——工作线程从出生起就活在这个循环里：有活干活，没活走完 33 轮后睡在条件变量上，直到 terminate latch 置位。这兑现了 u5-l3 讲过的「阻塞在 terminate 锁存器上边等边干活」。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「空闲线程真的睡了，CPU 归零」，并核对线程数与状态机的预测。

**操作步骤**（示例代码）：

```rust
// 示例代码：Cargo.toml 中 rayon = "1"（本仓库可用 path 依赖）
use rayon::ThreadPoolBuilder;
use std::thread;
use std::time::Duration;

fn main() {
    // 2 个线程、带名字，方便在 top/htop 里辨认
    let pool = ThreadPoolBuilder::new()
        .num_threads(2)
        .thread_name(|i| format!("sleepy-worker-{i}"))
        .build()
        .unwrap();

    // 阶段一：派活，让两个线程进入 active
    pool.install(|| {
        (0..10_000_000u64).into_par_iter().for_each(|i| {
            std::hint::black_box(i.wrapping_mul(i));
        });
    });
    println!("phase 1 done, now idling for 6s...");

    // 阶段二：主线程睡觉，不给池派任何任务
    thread::sleep(Duration::from_secs(6));

    // 阶段三：再派一个任务，观察唤醒
    pool.install(|| println!("phase 3: woke up on {:?}", thread::current()));
}
```

在项目目录另开一个终端，运行 `top -H -p $(pgrep -f 你的二进制名)`（或 `htop`，按线程名过滤 `sleepy-worker`），在程序运行的 6 秒空闲窗口内观察。

**需要观察的现象**：阶段一期间两个 `sleepy-worker` 线程 CPU 接近 100%；进入 6 秒空闲窗口后它们的 CPU 占比跌到 0%（线程阻塞在 `condvar.wait`，不再消耗 CPU，也不再有 33 轮自旋的残留——那个窗口只有几微秒量级）；阶段三打印出线程名，证明线程被成功唤醒。

**预期结果**：空闲期 CPU 为 0、派活后立即恢复工作。若想更精细地体会「33 轮梯度退避」，可把阶段一改成「每 100ms 派一个微任务」的循环：线程会在「犯困边缘」被反复拉回，CPU 维持低水平但非零。完整定量数据**待本地验证**（取决于机器与内核调度器）。

#### 4.2.5 小练习与答案

**练习 1**：为什么犯困线程在 `sleep()` 里发现 JEC 变化后用 `wake_partly`（rounds=32）而不是 `wake_fully`（rounds=0）？

**答案**：JEC 变化说明刚有新任务发布，只是我上一轮没搜到。`wake_partly` 把轮次拨回犯困临界点，只再搜一轮：搜到了就去干，搜不到立刻重新尝试入睡（[mod.rs:146-153](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L146-L153) 的注释原文："rather than searching for many rounds"）。`wake_fully` 要重数 32 轮，在这种「确实没活」的场景下纯属浪费。

**练习 2**：`sleep()` 为什么要先 `lock(is_blocked)` 再去递增睡眠计数，而不是反过来？

**答案**：防止唤醒信号丢失。若先登记入睡、后拿锁，可能出现「唤醒者锁上 → 看到 `is_blocked == false` 判断我没睡 → 不 notify → 释放锁」与「我置 `is_blocked = true` 并 wait」交错的时序——notify 被吞，我永远睡死。现在的顺序保证：唤醒者要么在我置位之前看到 false（此时我尚未 wait，随后检查条件会发现有人来过），要么被锁挡到我进入 `wait`（wait 原子地释放锁并入睡）之后才看到 true 并 notify。源码注释见 [mod.rs:180-184](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L180-L184)。

**练习 3**：`ROUNDS_UNTIL_SLEEPY = 32` 这个数字如果改成 1 或改成 100000，分别有什么代价？

**答案**：改成 1——线程几乎立刻入睡，CPU 最省，但任务间隙稍长就付出「睡眠 + 唤醒」两次系统调用与上下文切换的延迟，吞吐在突发负载下明显受损（本讲目标里「睡得太死」一端）。改成 100000——线程长时间空转找活，唤醒延迟最低，但空闲时白烧 CPU（「自旋空转」一端），违背本模块存在的意义。32 是在两者间取的经验折中，且中间还有犯困预告（JEC 盖章）进一步缩小竞态窗口。

### 4.3 唤醒与 Mutex–Condvar 协议：谁去叫人、叫几个

#### 4.3.1 概念说明

睡眠是线程自己的事，唤醒则永远由**别人**触发。Rayon 里有四类唤醒源：

1. **本地发布**（`new_internal_jobs`）：某线程往自己 deque 压任务时；
2. **外部注入**（`new_injected_jobs`）：池外线程往全局注入队列投任务时（多一道 SeqCst fence）；
3. **锁存器置位**（`notify_worker_latch_is_set`）：某个 latch 被 `set` 且其主人正睡着时——定向唤醒那个特定线程；
4. **顺手表 Bombilda**（`work_found`）：一个空闲线程找到工作时，按 `sub_inactive_thread` 返回的启发式值顺手叫醒至多 2 个同伴。

前两类走同一套**定量决策**：该叫醒几个人？决策依据正是 4.1 节账本里读出的两个数——醒着空闲的线程数（`awake_but_idle`）与睡眠线程数（`sleeping`）。直觉是：**已有的空闲劳动力够用就不打扰任何人；不够才按缺口叫人，且上限是现有睡眠者数量**。

#### 4.3.2 核心流程

`new_jobs(num_jobs, queue_was_empty)` 的决策树：

```
JEC 盖章（偶→奇，宣告"有新活"）
读账本：awake_but_idle, sleepers
若 sleepers == 0            → 没人可叫，返回
若队列原本非空               → 空闲线程居然没把它干掉，说明人手不够
                               叫醒 min(num_jobs, sleepers)
否则（队列原本为空）
   若 awake_but_idle < num_jobs → 叫醒 min(num_jobs − awake_but_idle, sleepers)
   否则                          → 现有空闲人手足够，不叫
```

`wake_specific_thread(i)` 的 Mutex–Condvar 三步：

```
lock(is_blocked[i])
若 *is_blocked:
    *is_blocked = false        # 在锁内改条件
    condvar.notify_one()       # 发信号
    sub_sleeping_thread()      # 由唤醒者（而非被唤醒者）扣减账本
    return true                # 真的叫醒了一个人
否则: return false             # 它根本没睡
```

「由唤醒者扣减」是本协议最反直觉的一笔：被唤醒的线程从 `wait` 中挣扎醒来可能要等操作调度器安排，若等它自己扣账本，这段延迟内账本会**虚报睡眠人数**，别的发布者会误以为还有可叫的人、白白去敲门。唤醒者当场扣减，账本即时反映真实。

定向唤醒（latch 路径）与定量唤醒（任务路径）殊途同归：`SpinLatch::set` 发现旧态是 `SLEEPING` 时，转手调 `notify_worker_latch_is_set` → `wake_specific_thread`；被唤醒线程回到 `wait_until_cold` 循环、发现 latch 已 SET，退出循环并补一次 `work_found`——**这又是一次唤醒源（第 4 类）**，于是唤醒呈「涟漪」式扩散：一次 latch set 最多可叫醒 1 + 2 = 3 个线程。

#### 4.3.3 源码精读

唤醒入口的两条通道：

[registry.rs:727-732](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L727-L732) —— `WorkerThread::push`：压入本地 deque 后调 `new_internal_jobs(1, queue_was_empty)`。注释（[mod.rs:223-229](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L223-L229)）明确此路径**不保证**一定唤醒成功——竞态下漏叫也没关系，发布者自己最终会 pop 到这个任务。

[registry.rs:426-442](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L426-L442) —— `Registry::inject`：外部线程投任务进注入队列后调 `new_injected_jobs(1, queue_was_empty)`。

[mod.rs:213-238](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L213-L238) —— 两个入口的差异只在开头：`new_injected_jobs` 先执行 `fence(SeqCst)` 再走共同的 `new_jobs`。这道 fence 是防死锁证明的一半（见下文）。

定量决策：

[mod.rs:242-272](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L242-L272) —— `new_jobs` 全文：先用 `increment_jobs_event_counter_if(is_sleepy)` 把 JEC 拉奇（宣告有活），随后读出 `awake_but_idle_threads()` 与 `sleeping_threads()`，按 4.3.2 的决策树计算 `num_to_wake` 并调 `wake_any_threads`。注意 `queue_was_empty` 的语义（[mod.rs:262-265](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L262-L265) 注释）：队列非空还来新活，说明空闲线程不够勤快，无条件叫人；队列为空则只在「醒着空闲数 < 任务数」时补足差额。

[mod.rs:274-311](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L274-L311) —— `wake_any_threads` 从 0 号工作线程顺序扫起，每成功唤醒一个就扣减预算，够数即返；`wake_specific_thread` 即 4.3.2 的三步曲，[mod.rs:296-305](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L296-L305) 的注释完整阐述了「为什么由唤醒者扣减睡眠计数」——缩短账本与现实的偏差窗口。

定向唤醒的触发链：

[latch.rs:194-225](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L194-L225) —— `SpinLatch::set`：`CoreLatch::set` 若返回 true（旧态为 SLEEPING，主人睡着），立刻调 `registry.notify_worker_latch_is_set(target_worker_index)`。注意注释里的高危细节：`set` 之后目标线程可能立即醒来并释放 latch 的内存，所以 `registry` 引用与 `target_worker_index` 都**在 set 之前**就读好了。`OnceLatch`（[latch.rs:300-310](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L300-L310)）与 `CountLatch` 的 Stealing 变体（[latch.rs:417-423](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L417-L423)）走同一条路。

[registry.rs:463-487](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L463-L487) —— `inject_broadcast`：广播场景对每个线程的队列各压一任务后，逐个 `notify_worker_latch_is_set(i)`——u6-l3 将展开的 broadcast 就靠这里把可能睡着的线程一一拍醒。

涟漪唤醒的闭环：

[mod.rs:79-85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L79-L85) —— `work_found`：调 `sub_inactive_thread()`（inactive −1）拿回「该顺手叫醒几个人」（至多 2），交给 `wake_any_threads`。被 latch 唤醒的线程在 [registry.rs:810-813](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L810-L813) 退出空闲循环时正会走到这里——涟漪的第二跳。

防死锁的 SeqCst fence 证明：

[README.md:149-172](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md#L149-L172) —— 死锁剧本：JEC 因绕回恰好变回旧值 → 犯困线程没察觉新任务 → 入睡；若那是**外部注入**的任务且全体 worker 都睡了，任务永无出头之日。所以内部任务被漏看可以容忍（发布者自己会 pop，见上文），外部注入必须兜底——手段就是 `sleep()` 末尾（递增睡眠数**之后**）对注入队列的最后一次检查。

[README.md:187-219](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md#L187-L219) —— 证明梗概：注入侧「push 任务 → **fence** → 读计数器」，睡眠侧「递增睡眠数 → **fence** → 读注入队列」。两个 SeqCst fence 在全局全序中必有一个在前：若注入侧 fence 在前，则 ReadJob 必能看到那次 push（睡者自查发现任务，自我撤销）；若睡眠侧 fence 在前，则 ReadSleepers 必能看到睡眠数已 +1（注入者发现有人睡着，主动唤醒）。两条路都通向「不会全池睡死」。这正是 Release/Acquire 做不到的——它们只承诺「看到则可见」，不承诺「一定看到」。两道 fence 的落点分别在 [mod.rs:214-221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L214-L221) 与 [mod.rs:170-171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L170-L171)。

#### 4.3.4 代码实践

**实践目标**：从行为侧验证「唤醒是按需的、且外部注入一定能叫醒睡眠者」。

**操作步骤**（示例代码，在 4.2.4 的工程里追加）：

```rust
// 示例代码：主线程（池外线程）注入任务，验证外部唤醒路径
use rayon::{ThreadPoolBuilder, iter::IntoParallelIterator};
use std::thread;
use std::time::Duration;

fn main() {
    let pool = ThreadPoolBuilder::new()
        .num_threads(4)
        .thread_name(|i| format!("wake-worker-{i}"))
        .build()
        .unwrap();

    // 让全池先睡稳：等待足够走过 33 轮
    thread::sleep(Duration::from_secs(1));

    // 池外线程注入：走 Registry::inject → new_injected_jobs → fence → new_jobs
    let handle = pool.spawn(|| {
        println!("injected job ran on {:?}", thread::current());
    });

    // 再压一批并行任务，触发定量唤醒决策
    handle.join().unwrap();
    let sum: u64 = pool.install(|| (1..=100u64).into_par_iter().sum());
    println!("sum = {sum}");
}
```

配合 `strace -f -e trace=futex -p <pid>`（Linux）观察更直观。

**需要观察的现象**：注入前进程安静（无 futex 活动）；`pool.spawn` 瞬间应能看到一次 `futex(..., WAKE, ...)` 系统调用——那就是 `condvar.notify_one`；打印的线程名应是四个 `wake-worker` 之一，证明睡眠者被定向唤醒。

**预期结果**：任务总能被执行、结果正确——无论睡眠竞态如何交错。这正是协议的设计目标：唤醒决策（叫几个）只影响性能，**正确性由 JEC 检查 + 最后的注入队列检查 + fence 三重保险**兜底。futex 观察部分**待本地验证**（strace 需要相应权限，且时序敏感）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `new_internal_jobs`（本地 push）不加 SeqCst fence，`new_injected_jobs`（外部注入）要加？

**答案**：漏看**内部**任务不会死锁——发布任务的线程自己随后会 pop 到它（[mod.rs:223-229](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L223-L229) 注释），最坏只是暂时少几个帮手；而漏看**外部注入**的任务时，池外没有人会替它善后，若全体 worker 都睡了就是死锁。fence 只加在死锁可能发生的那条路径上（README「Note that no fence is needed for work posted to internal queues, since it is ok to overlook work in that case」），因为 SeqCst fence 并不免费。

**练习 2**：`new_jobs` 中「队列原本非空」时为什么无条件叫醒 `min(num_jobs, sleepers)` 个？

**答案**：队列里已经有活却还有空闲线程没把它抢走，说明现有空闲劳动力要么不足要么不够快，再来新活更积压；此时按任务数（封顶睡眠者数）叫人是最直接的补员。反之队列原本为空时，已有空闲线程大概率足以接住新活，只有 `awake_but_idle < num_jobs` 的差额部分才需要补——见 [mod.rs:262-271](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/mod.rs#L262-L271) 的两个分支。

**练习 3**：一次 `SpinLatch::set` 唤醒了一个睡眠线程后，为什么最终可能总共有 3 个线程醒来？

**答案**：第一跳是 `set` 返回旧态 SLEEPING 后的定向唤醒（`notify_worker_latch_is_set`，1 个）；被唤醒线程回到 `wait_until_cold` 发现 latch 已置位，退出空闲循环时补调 `work_found`（[registry.rs:810-813](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L810-L813)），其中 `sub_inactive_thread` 返回 `min(sleeping, 2)`——第二跳再至多叫醒 2 个。合计至多 3 个。这种涟漪式扩散让唤醒从一个点迅速覆盖到「任务量对应的并行度」，而无须发布者精确计算全局该醒几个人。

## 5. 综合实践

**任务：给睡眠协议写一份「时序侦探报告」。**

把 4.2.4 与 4.3.4 的两段示例合并成一个程序，制造三个阶段并用系统工具取证：

1. **忙阶段**：`pool.install` 跑一个大规模 `into_par_iter().for_each`，用 `top -H` 记录每个 worker 线程的 CPU 占用（应接近 100%）。
2. **静默阶段**：主线程 `thread::sleep(10s)` 完全不派活，记录 worker 线程的 CPU（应为 0%），并用 `cat /proc/<pid>/status | grep Threads` 或 `htop` 确认线程仍存活（睡在 futex 上，而非退出）。
3. **复苏阶段**：`pool.spawn` 一个小任务再 `join`，记录从 spawn 到任务执行完成的感知延迟，并用 `strace -f -e trace=futex` 抓一次 WAKE 调用。

然后回到源码，为你在阶段 3 观察到的每一次状态转移**标注对应代码行**，写出完整因果链。预期形态如下（请自行补全行内引用）：

```
主线程 pool.spawn（池外）
  → Registry::inject（registry.rs:426-442）压入注入队列
  → new_injected_jobs（mod.rs:213-221）：fence(SeqCst) + new_jobs
  → new_jobs（mod.rs:242-272）：JEC 拉奇；读账本发现 awake_but_idle=0、sleepers>0、队列非空 → 叫醒 min(1, sleepers)
  → wake_specific_thread（mod.rs:288-311）：锁 is_blocked → 置 false → notify_one → sub_sleeping_thread
worker 线程侧
  → condvar.wait 返回（mod.rs:186-188），is_blocked 已为 false
  → sleep() 收尾：wake_fully + latch.wake_up（mod.rs:191-193）
  → 回到 wait_until_cold 内层循环，latch.probe() 为真 → work_found（registry.rs:810-813）
  → 执行注入的任务
```

**验收标准**：报告能回答三个问题——静默期 CPU 为什么是 0（线程阻塞在 `condvar.wait`，33 轮退避早已走完）；复苏为什么必然发生（三重保险：JEC 检查、最后的注入队列检查、两道 SeqCst fence）；唤醒个数由什么决定（`awake_but_idle`/`sleepers`/`num_jobs`/`queue_was_empty` 四元组的决策树，外加 `work_found` 的涟漪第二跳）。定量数字**待本地验证**。

## 6. 本讲小结

- `AtomicCounters` 把**睡眠数、不活跃数、JEC** 压进单个 `AtomicUsize`，让「检查 JEC + 睡眠数 +1」能用一次 CAS 原子完成；核心不变量是 `sleeping ≤ inactive` 与 JEC 的奇偶广播（偶=困、奇=有新活）。
- 工作线程状态机为 **active → idle →（32 轮退避）→ sleepy（JEC 盖章）→（再 1 轮）→ sleeping**，由 `no_work_found` 按轮次驱动，与 `CoreLatch` 的 UNSET/SLEEPY/SLEEPING/SET 四态机配合完成安全入睡。
- 睡眠者**先锁 `is_blocked` 再递增睡眠计数**，配合 `while *is_blocked { condvar.wait }` 的管程风格等待，保证唤醒信号永不落入缝隙；**唤醒者**（而非被唤醒者）扣减睡眠计数，让账本即时反映现实。
- 唤醒是**按需定量**的：`new_jobs` 依据 `awake_but_idle`、`sleeping`、任务数与队列是否原本为空决定叫醒几个；latch 置位走定向唤醒，再经 `work_found` 的涟漪（至多 1+2=3 个）扩散。
- 两道 **SeqCst fence**（注入侧 push 后、睡眠侧递增睡眠数后）以全序论证堵死了「外部注入被漏看 + 全池睡死」的死锁剧本——这是 Release/Acquire 语义做不到的。
- 整套协议的分工是：**唤醒决策只影响性能，正确性由 JEC 检查 + 注入队列终检 + fence 三重保险兜底**——既不空烧 CPU，也不损失响应。

## 7. 下一步学习建议

本讲补完了单元五（rayon-core 调度内核）的最后一块拼图：从 `join`（u5-l1）经 Job/Latch（u5-l2）、Registry（u5-l3）、工作窃取（u5-l4）到睡眠唤醒，调度链路已全线贯通。接下来进入单元六「作用域与任务生成」：

- **u6-l1 scope**：`scope` 如何借用栈上数据——其中 `CountLatch`（本讲 [latch.rs:417-423](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L417-L423) 提到的 Stealing 变体）的计数归零与唤醒路径会直接复用本讲知识。
- **u6-l3 broadcast**：`inject_broadcast` 对每个线程 `notify_worker_latch_is_set` 的定向唤醒（本讲 [registry.rs:463-487](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L463-L487)）正是它的落地面。
- 若想深挖理论，可读 [sleep/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md) 引用的 Rayon RFC #5 与视频讲解，并思考：如果去掉「最后检查注入队列」一步，构造一个具体的死锁交错序列。
