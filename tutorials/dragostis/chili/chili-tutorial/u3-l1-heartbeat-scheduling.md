# 心跳调度机制：execute_heartbeat

## 1. 本讲目标

u2-l4 讲完了「任务如何投递、如何被偷、结果如何等回来」，但那条链路里有一个角色一直只闻其名、未见其人:**心跳标志是谁、在什么时候置位的?** 本讲就把这个幕后角色——心跳线程 `execute_heartbeat`——完整拆开。

学完本讲,你应该能够:

1. 说清心跳的本质:它不是工作线程,而是一个**周期性触发信号**,负责「提醒」各个计算线程把本地队列里的任务分享出去。
2. 解释 `Heartbeat` 结构为什么持有 `Weak<AtomicBool>` 而不是 `Arc`:失效 Scope 的登记项如何被 `retain` 惰性清理、为什么在持锁状态下清理是安全的。
3. 用数学语言描述心跳间隔的**均摊设计**:\(\delta = H/N\) 的睡眠步长如何保证「每个线程」的心跳周期都接近配置值 \(H\)。
4. 精确列出 `scope_created_from_thread_pool` 条件变量的**每一次睡眠与唤醒**时机,并解释谓词 `heartbeats.len() == num_workers` 为什么恰好等价于「池子里没有待并行的用户工作」。

## 2. 前置知识

本讲是全库并发细节最密集的一讲之一,先把要用到的概念用通俗语言过一遍:

- **强引用与弱引用(`Arc` / `Weak`)**:`Arc<T>` 是共享所有权的强引用,强引用计数归零时 `T` 被销毁;`Weak<T>` 是弱引用,**不阻止**销毁。`Weak::upgrade()` 返回 `Option<Arc<T>>`——对象还活着就得到一个临时强引用(`Some`),已经死了就是 `None`。「能用 upgrade 探活、又不延长寿命」正是本讲要利用的性质。
- **`Condvar::wait_while`**:条件变量的循环等待。`condvar.wait_while(guard, 谓词)` 会在**持有锁**的状态下检查谓词:谓词为真就释放锁并挂起线程;被唤醒后重新拿锁、再查谓词,直到谓词为假才返回。「查谓词」和「挂起放锁」是原子的,这是后面论证「唤醒不丢失」的地基。
- **`HashMap::retain`**:遍历中删除。对每个条目跑一个返回 `bool` 的闭包,返回 `false` 的条目被移除并析构。chili 把「垃圾回收」和「升旗」合并进了同一次 `retain` 遍历。
- **`Instant` / `Duration`**:`Instant::now()` 是单调时钟(不受系统改时间影响),适合测量「距离上次心跳多久了」。`Duration::checked_div(u32)` 做除法,**除数为 0 时返回 `None`** 而不是 panic——chili 把这个边界情况变成了控制流。
- **`Ordering::Relaxed`(复习)**:只保证单变量上的原子性,不建立线程间的可见性顺序。u2-l4 已给出结论:心跳标志只是一个「建议位」,晚看到顶多推迟一次分享,真正的数据同步靠互斥锁和 `job.rs` 的 Channel。
- **锁中毒(poisoning)**:线程持锁 panic 后,之后所有 `lock()` 都返回 `Err`。chili 的后台线程用 `.ok()?` 处理——中毒就直接退出线程。

另外承接 u2 已建立的三个结论,本讲直接使用:

- 计算线程在 `join_with_heartbeat_every` 的热路径上只做一次 `Relaxed` 原子读,标志为真才进入加锁的 `heartbeat()`(u2-l1);
- `shared_jobs` 货架以心跳 `Arc` 的堆地址为键,一个 Scope 同时至多挂一个任务(u2-l4);
- worker 线程与心跳线程由 `ThreadPool::with_config` 依次 spawn,Drop 时按「先 worker 后心跳」的顺序 join(u2-l2/u2-l3)。

## 3. 本讲源码地图

本讲全部源码集中在 [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs):

| 位置 | 作用 |
| --- | --- |
| [src/lib.rs:L67-L71](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L67-L71) | `Heartbeat` 结构:登记表里的一条记录(`Weak` + 上次升旗时间) |
| [src/lib.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80) | `LockContext`:`heartbeats` 登记表与 `heartbeat_index` 自增键 |
| [src/lib.rs:L83-L96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L83-L96) | `new_heartbeat`:创建初始为 `true` 的旗并登记 |
| [src/lib.rs:L105-L110](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L105-L110) | `Context`:两个条件变量的定义处 |
| [src/lib.rs:L147-L189](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L147-L189) | `execute_heartbeat`:本讲主角,心跳线程主循环 |
| [src/lib.rs:L233-L239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L233-L239) | `Scope`:持有旗的**强**引用的另一方 |
| [src/lib.rs:L254-L267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L267) | `new_from_thread_pool`:用户 Scope 登记 + `notify_one` |
| [src/lib.rs:L269-L278](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L269-L278) | `new_from_worker`:worker Scope 登记(**不** notify) |
| [src/lib.rs:L280-L282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L280-L282) | `heartbeat_id`:把旗的地址变成身份键 |
| [src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) | `heartbeat()`:消费旗、投递任务、把旗放倒 |
| [src/lib.rs:L362-L364](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L362-L364) | 读旗的一侧:`join_heartbeat` 的 `Relaxed` 加载 |
| [src/lib.rs:L459-L476](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L459-L476) | `Config`:`heartbeat_interval` 的文档与默认值 100µs |
| [src/lib.rs:L513-L546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L546) | `with_config`:spawn 顺序(barrier 前/后)的决定性证据 |
| [src/lib.rs:L615-L633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633) | `Drop`:停机时对心跳线程的唤醒 |
| [src/lib.rs:L807-L832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L807-L832) | `tests::concurrent_scopes`:大量并发 Scope 的压力用例 |

## 4. 核心概念与源码讲解

### 4.1 Heartbeat 结构与 Weak 引用

#### 4.1.1 概念说明

先回忆读旗的一侧(u2-l1):每个 `Scope` 内部有一面「心跳旗」——一个 `Arc<AtomicBool>`。计算线程在 join 的热路径上读它,旗立着就走冷路径 `heartbeat()`,把本地队列队头的任务挂上货架,然后把旗放倒。

于是问题变成:**谁来升旗?多久升一次?谁来管 Scope 死掉之后的旗?**

chili 的答案是把「升旗」抽成一个专职线程(下一节),而这个线程面对的是**数量不定、寿命不定**的 Scope:用户每调一次 `tp.scope()` 就多一个,函数返回它就该消失。如果登记表里存强引用 `Arc<AtomicBool>`,会产生两个麻烦:

1. **泄漏**:Scope 死后,登记表还攥着强引用,`AtomicBool` 永远不会释放,登记项也永远无法判断「已失效」;
2. **注销成本**:要么给 `Scope` 实现 `Drop` 去显式注销——每次 Scope 结束都要抢一次全局锁,热路径变热。

存 `Weak` 则一举解决:登记表只「看得见」旗、**不养着**旗。Scope 一死(`Scope` 没有自定义 `Drop`,`Arc` 自然析构,强计数归零),`upgrade()` 就开始返回 `None`,登记项变成一条「死记录」,等心跳线程下次扫描时顺手清掉。注销是**惰性的、零成本摊销进扫描循环**的。

还有一个容易被忽略的正确性红利:`Weak` 和 `Instant` 的析构都**不运行任何用户代码**,所以在**持锁状态**下执行 `retain` 移除条目是安全的——不存在「析构里又去拿同一把锁」的经典死锁。若登记的是带 `Drop` 的复杂对象,这一点就不成立。

#### 4.1.2 核心流程

```text
Scope 创建(用户线程调 tp.scope(),或 worker 启动):
    flag = Arc::new(AtomicBool::new(true))     # 注意:初始就是立着的
    登记表.heartbeats[下一个自增索引] = Heartbeat {
        is_set:          downgrade(flag),       # 弱引用:不延长寿命
        last_heartbeat:  now(),                  # 升旗时间戳,从创建起算
    }

Scope 存活期间:
    心跳线程定期扫描登记表(见 4.2):
        upgrade 成功 -> 旗还活着,按到期规则升旗
        upgrade 失败 -> Scope 已死,retain 移除该登记项   # 自动清理

Scope 结束:
    无任何显式注销代码;Arc 强计数归零,AtomicBool 释放
    登记项最多再活一个扫描周期,之后被 retain 带走
```

两个容易混淆的键空间也要分清(承接 u2-l4):

| 映射 | 键 | 为什么 |
| --- | --- | --- |
| `heartbeats: HashMap<u64, Heartbeat>` | 自增索引 `heartbeat_index` | 只需**遍历**,从不需要按 Scope 反查,递增键最简单 |
| `shared_jobs: BTreeMap<usize, …>` | 旗的堆地址(`heartbeat_id()`) | 需要**按当前 Scope 查找/移除**,而 Scope 手里只有 `Arc`,地址即身份 |

#### 4.1.3 源码精读

先看登记项本身,两个字段一一对应上面的设计:

[src/lib.rs:L67-L71](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L67-L71) 定义 `Heartbeat`:`is_set` 是弱引用,`last_heartbeat` 记录上次升旗时刻,是 4.2 节「到期判定」的依据。

[src/lib.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80) 是锁内账本 `LockContext`,其中 `heartbeats: HashMap<u64, Heartbeat>` 就是登记表,`heartbeat_index: u64` 是自增键。注意 `checked_add(1).unwrap()` 的写法——u64 溢出在实践中不可能,真溢出直接 panic 也符合「账本必须一致」的预期。

再看登记动作:

[src/lib.rs:L83-L96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L83-L96) 是 `new_heartbeat`,四步:① 新建初始值为 `true` 的旗;② `Arc::downgrade` 折出弱引用连同当前时间组装成 `Heartbeat` 插表;③ 取出当前自增索引作为键;④ 索引 +1;最后把**强引用**返回给调用方。

两个调用方恰好是一强一弱的「持有关系全景图」:

- [src/lib.rs:L233-L239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L233-L239):`Scope` 的 `heartbeat` 字段持有强引用 `Arc<AtomicBool>`。**全库只有这里养着这面旗**——`Scope` 落地,旗落地。
- [src/lib.rs:L255](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L255) 与 [src/lib.rs:L270](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L270):用户 Scope 和 worker Scope 的创建都调用 `new_heartbeat()`(二者在通知条件变量上的差异见 4.3)。

旗的初始值为什么是 `true`?这是一处很妙的**自举(bootstrap)**设计:新 Scope 的第一次 `join` 走到 [src/lib.rs:L362-L364](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L362-L364) 时旗已经立着,于是立即执行一次 `heartbeat()`——**不用等最长一个心跳周期**,worker 马上就有机会从新 Scope 偷走第一个任务。对短命 Scope(比如 `concurrent_scopes` 测试里那种几微秒的)来说,等 100µs 等于永远等不到。此刻本地队列里恰好只有刚压入的那一个任务,投递它没有副作用。消费侧在 [src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333):`heartbeat()`(标了 `#[cold]`,编译器会把它挪到冷路径)末尾 `store(false, Relaxed)` 把旗放倒,保证一次升旗至多触发一次投递。

#### 4.1.4 代码实践

**实践目标**:亲眼验证「`Weak` 不阻止回收、`upgrade` 能探活」这一 `Heartbeat` 设计赖以成立的语义。

**操作步骤**(纯标准库,新建一个 `heartbeat-weak-demo` 二进制 crate 即可):

```rust
// 示例代码:演示 Arc/Weak 的生命周期语义
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Weak,
};

fn main() {
    let weak = {
        let flag = Arc::new(AtomicBool::new(true)); // 模拟 Scope 手里的强引用
        let weak: Weak<AtomicBool> = Arc::downgrade(&flag); // 模拟登记表
        println!(
            "Scope 存活时 upgrade: {:?}",
            weak.upgrade().map(|f| f.load(Ordering::Relaxed))
        );
        weak
    }; // flag 在块尾被 drop,相当于 Scope 结束

    println!(
        "Scope 死亡后 upgrade: {:?}",
        weak.upgrade().map(|f| f.load(Ordering::Relaxed))
    );
}
```

**需要观察的现象**:第二行输出如何从 `Some(...)` 变成 `None`。

**预期结果**:

```text
Scope 存活时 upgrade: Some(true)
Scope 死亡后 upgrade: None
```

这正是 `execute_heartbeat` 里 `retain` 判断「这条登记项要不要删」的全部依据(见 4.2.3 的 L166-L176)。

#### 4.1.5 小练习与答案

**练习 1**:如果把 `Heartbeat::is_set` 改成 `Arc<AtomicBool>`,除了内存泄漏,还会破坏本讲后文的哪个机制?

**答案**:会永久破坏 4.3 的休眠谓词。强引用让死 Scope 的登记项永远「有效」,`heartbeats.len()` 永远回不到 `num_workers`,条件变量的等待谓词 `len == num_workers` 永远为假,心跳线程从第一次扫描起就永远空转,再也无法休眠。

**练习 2**:为什么 `new_heartbeat` 把旗初始化为 `true` 而不是 `false`?

**答案**:`true` 让每个新 Scope 的第一次 `join_heartbeat` 立即获得一次投递机会(自举),不必等待最长一个 `heartbeat_interval`;对短命 Scope 尤其关键。而此刻本地队列只有刚压入的那一个任务,投递无副作用。若初始化为 `false`,新 Scope 在头一个周期内只能顺序执行。

**练习 3**:`retain` 在持有 `context.lock` 的状态下移除登记项并析构 `Weak`,为什么不怕死锁?

**答案**:`Weak<AtomicBool>` 和 `Instant` 的析构都是平凡析构,不运行任何用户代码,不可能重入锁。这是「登记表只存弱引用和纯数据」带来的隐含安全契约——如果哪天往 `Heartbeat` 里加一个带 `Drop` 的字段,这里就要重新审视。

### 4.2 心跳线程循环:execute_heartbeat 与间隔均摊

#### 4.2.1 概念说明

`execute_heartbeat` 是 `ThreadPool` 里的**唯一**心跳线程,由 [src/lib.rs:L541-L544](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L541-L544) spawn。它不执行任何用户任务,只做两件事:

1. **升旗**:对到期的旗执行 `store(true, Relaxed)`,触发对应计算线程在下一次降频检查时分享任务;
2. **扫墓**:顺路用 `retain` 把死 Scope 的登记项清掉(4.1)。

理解它的关键,是想清楚「心跳」到底心跳的是什么。它**不是**「每 100µs 唤醒一个 worker」,而是「每 100µs 给每个**线程自己的**旗一次立起来的机会」。`Config` 的文档原文([src/lib.rs:L465-L466](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L465-L466))写得很精确:*"The interval between heartbeats on any particular thread"*——**对每个特定线程而言**的间隔。默认值 100µs 定义在 [src/lib.rs:L473](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L473)。

#### 4.2.2 核心流程

主循环的伪代码(与源码逐行对应):

```text
loop:
    锁内:
        wait_while(heartbeats.len() == num_workers 且 未停机)   # 没有用户 Scope 就睡(见 4.3)
        若 is_stopping: break                                    # 停机退出
        now = Instant::now()                                     # 一轮只读一次时钟
        heartbeats.retain(|h|):                                  # 一次遍历同时干两件事
            upgrade(h.is_set) 失败 -> 返回 false,移除登记项      #   扫墓:Scope 已死
            成功 且 now - h.last_heartbeat >= H:                 #   升旗:到期了
                is_set.store(true, Relaxed)
                h.last_heartbeat = now
        delta = H / heartbeats.len()                             # 均摊步长(checked_div)
    放锁后才: sleep(delta)                                       # 睡眠绝不持锁
    # delta 为 None(len == 0)时跳过睡眠,直接回循环顶去 wait
```

**均摊的数学**。设心跳周期为 \(H\)(配置项 `heartbeat_interval`),当前登记的旗数为 \(N\)(含 \(num\_workers\) 面常驻的 worker 旗)。心跳线程单线程串行服务这 \(N\) 面旗,它的睡眠步长取

\[
\delta \;=\; \frac{H}{N},
\]

即每 \(H/N\) 的时间扫描**全部** \(N\) 面旗一次。对任何一面特定的旗,「到期」判定用的是它自己的 `last_heartbeat`(只在**真正升旗时**才更新),所以两次升旗的间隔至少是 \(H\);又因为扫描粒度是 \(\delta\),到期最迟被拖延一个步长,于是实际间隔落在

\[
T_{\text{actual}} \;\in\; \left[\, H,\;\; H + \frac{H}{N} \,\right].
\]

这就是「均摊」的含义:**全局扫描频率是 \(N/H\)(随线程数升高),但每个线程体感的心跳周期始终约为 \(H\)**。若不做除法、每轮直接睡 \(H\),每面旗的实际周期会退化成 \(N \cdot H\),8 线程时一个 100µs 的配置变成 800µs 才有一次分享机会。

代价也要看清:每个周期 \(H\) 内有 \(N\) 轮扫描、每轮碰 \(N\) 个条目,心跳线程每周期做 \(\Theta(N^2)\) 次登记项访问。\(N\) 很大时 \(\delta = H/N\) 趋近于 0,设计退化为高频轮询——这是它天然的扩展边界(练习 3 会算一个真实用例)。

**内存序**。升旗用 `Relaxed` 完全够:旗上不发布任何数据,读到旧值(旗已经倒了)或晚一会儿才读到,后果只是「这一次 join 没分享、下一次再分享」。数据本身的安全性由 `context.lock` 互斥(货架)和 `job.rs` 的 Channel(结果)保证。用更强的序只是白付缓存一致性开销。

#### 4.2.3 源码精读

[src/lib.rs:L152-L159](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L152-L159) 是循环的第一段:在 `scope_created_from_thread_pool` 上 `wait_while`,谓词为 `heartbeats.len() == num_workers && !is_stopping`(谓词为什么长这样、何时醒,整个 4.3 展开)。两处 `.ok()?` 是锁中毒处理——中毒即返回 `None`,线程安静退出。

[src/lib.rs:L161-L163](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L161-L163):醒来先查停机旗,`is_stopping` 就 `break`,线程以 `Some(())` 正常收尾,配合 `Drop` 里的 `join`(4.3.3)。

[src/lib.rs:L165-L176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L165-L176) 是全函数最精巧的一段,值得逐行读:

- L165 `now = Instant::now()`:**一轮扫描只读一次时钟**,\(N\) 个条目共享同一时间戳,省掉 \(N-1\) 次时钟读取;判定误差不超过一个扫描步长 \(\delta\),可接受。
- L166 `heartbeats.retain(...)`:遍历中删除。闭包对每个 `Heartbeat`:
  - `h.is_set.upgrade()` 把 `Option<Arc>` 链到 `inspect` 上——`Some` 时执行「到期则升旗」:`now.duration_since(h.last_heartbeat) >= heartbeat_interval` 成立就 `store(true, Relaxed)` 并把 `last_heartbeat` 更新为 `now`;
  - 链尾 `.is_some()` 决定条目去留:`upgrade` 失败(旗已随 Scope 灭亡)返回 `false`,登记项就地移除。

  一遍 `retain` 同时完成了 GC 与信号分发,且 `last_heartbeat` 只在真正升旗时推进,这正是 4.2.2 中区间 \([H, H+H/N]\) 的出处。
- 这一切都发生在**持锁**状态下(守卫 `lock` 从 L154 活到块尾 L179),而睡眠在锁外——扫描期间 worker 拿不到锁,但扫描本身是 \(O(N)\) 的纯内存操作,耗时以微秒计。

[src/lib.rs:L178](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L178) 计算均摊步长:`heartbeat_interval.checked_div(lock.heartbeats.len() as u32)`。用 `checked_div` 而不是 `/`,是为了把「除零」变成 `None`。

[src/lib.rs:L181-L185](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L181-L185) 处理 `None` 分支,注释原文说得很清楚:登记数为 0 时**跳过睡眠直接回到循环顶**去触发 `wait`。len 为 0 只在 `num_workers == 0`(单线程配置)时出现;若此时傻睡一个完整 \(H\),不过是把「进入条件变量休眠」这件事无谓推迟,还多烧一个周期的 CPU。

读、写两侧合起来看,旗的完整生命周期是:**创建即立**(L84)→ 计算线程读到并投递后放倒([L332](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L332))→ 心跳线程到期再立(L171)→ …… → Scope 死亡,旗随 Arc 释放,登记项被 retain 带走。

#### 4.2.4 代码实践

**实践目标**:把均摊公式和真实测试对上号,感受 \(N\) 增大时的步长收缩。

**操作步骤**:

1. 纸笔计算:默认 \(H = 100\,\mu s\)。分别对 \(N = 9\)(8 个 worker + 1 个用户 Scope)和 \(N = 132\)(4 worker + 128 个并发用户 Scope),算 \(\delta = H/N\) 与每面旗的实际周期上界 \(H + H/N\)。
2. 打开压力用例 [src/lib.rs:L807-L832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L807-L832)(`concurrent_scopes`:128 个线程各自创建一个 chili Scope),确认第二步的 \(N=132\) 正是它运行期间的瞬时规模。
3. 运行 `cargo test --lib concurrent_scopes -- --nocapture`,确认该场景下库依然正确。

**需要观察的现象**:测试通过;以及你算出的 \(\delta\) 在 \(N=132\) 时约 \(758\,ns\)——已低于常见操作系统的调度时间片,心跳线程在该测试期间**接近忙轮询**。

**预期结果**:\(N=9\) 时 \(\delta \approx 11.1\,\mu s\),实际周期 \(\in [100, 111.1]\,\mu s\),设计目标达成;\(N=132\) 时 \(\delta \approx 0.76\,\mu s\),每周期 \(\Theta(N^2)\) 的扫描量开始成为不可忽略的开销。这正是 4.2.1 所说扩展边界的具体数字。

#### 4.2.5 小练习与答案

**练习 1**:把 [L178](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L178) 改成 `thread::sleep(heartbeat_interval)`,会发生什么?

**答案**:每面旗的实际心跳周期从约 \(H\) 退化成约 \(N \cdot H\)(8 线程 + 1 Scope 时为 9 倍),分享机会同步减少 9 倍,违背 `Config` 文档「对每个线程而言的间隔」的语义。程序仍正确(may-并行),只是并行度肉眼可见地下降。

**练习 2**:把升旗改成 `store(true, Ordering::Release)`、读旗仍是 `Relaxed`,会更正确吗?

**答案**:不会更正确,只会更慢。旗不发布任何数据,正确性由互斥锁和 Channel 保证;`Release`/`Relaxed` 不配对也不构成 UB,只是多余的内存屏障开销。chili 选 `Relaxed` 是有意为之。

**练习 3**:为什么 L165 的 `Instant::now()` 放在 `retain` 外面、整轮只读一次?

**答案**:减少时钟读取次数(\(N\) 个条目共享一个时间戳);判定「到期」的误差至多一个扫描步长 \(\delta\),落在 4.2.2 论证的误差上界内,不影响承诺的周期语义。

### 4.3 scope_created_from_thread_pool 条件变量

#### 4.3.1 概念说明

`Context` 里有两个条件变量([src/lib.rs:L105-L110](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L105-L110)),服务对象不同:

| 条件变量 | 谁在等 | 等什么 |
| --- | --- | --- |
| `job_is_ready` | worker 线程 | 「货架上有活了」(u2-l3 已讲) |
| `scope_created_from_thread_pool` | **心跳线程** | 「池子里出现用户 Scope 了」或「要停机了」 |

心跳线程绝大多数时间应该**睡着**——池子空闲时若它还在每 100µs 醒一次扫一遍空表,「低开销」就成了空话。`wait_while` 的谓词就是它的睡眠条件:

```text
继续睡的条件: heartbeats.len() == num_workers 且 !is_stopping
```

这个谓词为什么恰好等于「没有待并行的用户工作」?推理链有三步:

1. 每个 worker 的 Scope 在池构造时创建、worker 退出前**永不销毁**([src/lib.rs:L116](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L116),`execute_worker` 里 Scope 建在循环外),所以登记表里**常驻**恰好 `num_workers` 条 worker 记录;
2. 用户每调一次 `tp.scope()` 多一条;Scope 死后记录还要等 `retain` 清理(4.1),所以 `len` 只会**暂时**高于真实存活数;
3. 于是 `len == num_workers` 当且仅当**没有任何用户 Scope 存活、也没有待清理的尸迹**——而没有用户 Scope 就不可能有待分享的任务(worker 执行的都是从某个用户 Scope 分享来的任务),心跳线程自然可以睡。

反过来,`len > num_workers` 的每一刻——用户 Scope 存活期间——谓词为假,`wait_while` **立即返回**,心跳线程进入 4.2 的「retain → 睡 \(\delta\) → 再 retain」自由运转循环;期间某个用户 Scope 死了也没人发通知,下一轮 `retain` 把尸迹清掉、`len` 落回 `num_workers`,再下一轮 `wait_while` 就重新入睡。**休眠的进入不需要任何显式事件,靠的是谓词轮询的收敛**——只有「唤醒」必须显式 `notify`。

#### 4.3.2 核心流程

把全部睡眠/唤醒事件列成表(这是本讲要求你能默写的内容):

| 事件 | 源码位置 | 对心跳线程的效果 |
| --- | --- | --- |
| 用户调用 `tp.scope()` | [L254-L259](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L259):锁内 `new_heartbeat()` 插表,放锁后 `notify_one()` | `len` 变为 `num_workers + 1`,谓词变假,线程醒来开始巡检 |
| `ThreadPool` 被 Drop | [L615-L623](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L623):锁内置 `is_stopping = true`,放锁后 `notify_one()` | 谓词中 `!is_stopping` 变假,`wait_while` 返回,L161 `break`,线程退出 |
| worker 的 Scope 创建 | [L269-L278](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L269-L278):只有 `new_heartbeat()`,**没有** notify | 不需要唤醒,见下面的启动时序 |
| 用户 Scope 被 drop | 无任何代码 | 无通知;靠下一轮 `retain` 清尸迹、谓词重新成立后**自行入睡** |

**为什么 worker 创建 Scope 不用 notify?** 看 `with_config` 的启动时序([src/lib.rs:L513-L546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L546)),顺序是决定性的:

1. L514-L518 算出 worker 数 `thread_count`(配置线程数 − 1,u2-l2);
2. L519 建 `Barrier(thread_count + 1)`;
3. L527-L535 spawn 全部 worker——每个 worker 在 [L116](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L116) 先建 Scope(登记心跳),干完第一轮后在 [L135](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L135) 到 barrier 报到;
4. L537 主线程 `worker_barrier.wait()`——**返回时全部 worker Scope 已登记**;
5. L541-L544 才 spawn 心跳线程。

也就是说,心跳线程第一次执行 `wait_while` 时,`len` 已经**恰好**是 `num_workers`,谓词成立,直接入睡——「池空闲,零心跳开销」。此后 `len` 偏离 `num_workers` 的唯一日常来源就是用户 Scope,所以只需在 `new_from_thread_pool` 一处 notify。

**为什么 notify 在锁外发也不会丢唤醒?** 唤醒丢失的经典场景是「等待者查完谓词、还没睡下,通知先到了」。`wait_while` 封死了这个窗口:查谓词**持锁**,挂起并放锁是原子步骤。而插入登记项(L255)和置停机旗(L617-L621)都必须持锁。于是任意交错下:插入要么发生在查谓词之前(谓词直接为假,不睡),要么发生在等待者放锁之后(此时 notify 一定落在已睡/将睡的等待者头上,将其唤醒)。`notify_one` 也足够,因为每个池只有一个心跳线程。

#### 4.3.3 源码精读

[src/lib.rs:L109](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L109):条件变量本体,和 `job_is_ready` 并排放在 `Context` 里,由 `Arc<Context>` 共享给所有 worker 与心跳线程。

[src/lib.rs:L254-L267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L267):`new_from_thread_pool`。注意语句顺序——`new_heartbeat()` 在 `let` 语句内完成(临时的 `MutexGuard` 随语句结束而释放),`notify_one()` 在**锁外**执行;插入在前、通知在后,配合上一段的原子性论证,无丢唤醒风险。

[src/lib.rs:L153-L163](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L153-L163):等待侧。`wait_while` 的谓词闭包拿到的是 `LockContext`,每次被(真实或虚假)唤醒都会重新拿锁评估;返回后紧接着检查 `is_stopping` 决定是否收尾。

[src/lib.rs:L615-L631](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L631):`Drop` 的停机广播,顺序呼应 u2-l2:① 持锁置 `is_stopping`;② `job_is_ready.notify_all()` 叫醒全部 worker;③ `scope_created_from_thread_pool.notify_one()` 叫醒心跳线程——正是本讲的唤醒源之二;④ 先 join 所有 worker,最后 join 心跳线程。若心跳线程此刻不在条件变量上而是正睡在 `thread::sleep(\delta)` 里,它最多再睡一个 \(\delta\)(\(\le H\))就会回到循环顶,`wait_while` 因 `!is_stopping` 为假而立即返回,同样走 `break`——所以 Drop 不会被长时间挂住。

#### 4.3.4 代码实践

**实践目标**:用计数器直接观测「一次运行里心跳线程到底升了几次旗」,把 4.2/4.3 的抽象循环变成肉眼可见的数字。

**操作步骤**(在**你的本地副本**上做,观察完还原,不影响仓库):

1. 在 `execute_heartbeat` 的 `retain` 闭包里、`is_set.store(true, ...)` 那行旁边,插入一个用 `static` 原子计数器累计的 `eprintln!`(示例代码):

   ```rust
   // 示例代码:临时观测用,观察后请删除
   use std::sync::atomic::AtomicU64;
   static SET_COUNT: AtomicU64 = AtomicU64::new(0);
   // 在 store(true, Relaxed) 旁边:
   SET_COUNT.fetch_add(1, Ordering::Relaxed);
   eprintln!("heartbeat set #{}", SET_COUNT.load(Ordering::Relaxed));
   ```

2. 依次运行 `cargo test --lib join_very_long -- --nocapture` 与 `cargo test --lib concurrent_scopes -- --nocapture`,统计 stderr 里打印的行数。
3. `git checkout -- src/lib.rs` 还原源码。

**需要观察的现象**:`join_very_long`(约 1M 个元素的二分递增,总时长以毫秒计)期间升旗次数是多少量级;`concurrent_scopes` 期间是否明显更多、打印是否密集得多。

**预期结果**(定性,具体数字**待本地验证**):`join_very_long` 在默认 100µs 下,每面旗最多被升十几次到几十次(总时长 / \(H\));`concurrent_scopes` 因 \(N\) 高达 132、\(\delta\) 缩到亚微秒,打印频率应显著更高。另请留意:持锁 `eprintln!` 本身会拖慢扫描、放大耗时,该实验只看**次数**,别拿它测性能。

#### 4.3.5 小练习与答案

**练习 1**:把谓词里的 `==` 改成 `>=`(`l.heartbeats.len() >= num_workers`),程序还能并行吗?

**答案**:几乎不能。`len` 永远 \(\ge num_workers\)(worker 记录常驻),谓词恒真,心跳线程永远睡在条件变量上,再也不会升旗;仅存的分享机会只剩每个 Scope 首次 join 的那次「自举投递」(4.1.3)。程序仍然正确——这恰好从反面印证了 may-并行语义:结果与是否并行无关,变的只是速度。

**练习 2**:删掉 Drop 里 [L623](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L623) 的 `notify_one`,最坏后果是什么?

**答案**:若此刻心跳线程正阻塞在 `wait_while` 上(池空闲、无用户 Scope),没有任何事件会再唤醒它去重估谓词,`is_stopping` 永远不被它看见;Drop 在 L629-L631 `join` 心跳线程时永久阻塞——池销毁死锁。若它恰好在 `sleep(\delta)` 循环里则能经下一轮 `wait_while` 正常退出,所以这是一个**时序相关**的 bug,比必现死锁更险恶。

**练习 3**:谓词里为什么要带 `&& !l.is_stopping`?

**答案**:让「停机」成为独立于 `len` 的唤醒条件。若谓词只看 `len == num_workers`,即便 Drop 的 `notify_one` 把线程叫醒,只要没有用户 Scope,谓词仍为真,它会接着睡回去、错过停机。加上这一项,被唤醒后重估谓词立即为假,配合 L161 的检查干净退出。

## 5. 综合实践

**任务:心跳频率三档扫描——`heartbeat_interval` 取 1µs / 100µs(默认)/ 10ms,重跑 `join_very_long` 的工作负载,记录耗时并解释过高与过低各自的问题。**

`tests::join_very_long`([src/lib.rs:L692-L714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714))用默认配置 `ThreadPool::new()`,无法直接改心跳间隔,所以我们在独立 crate 里复刻它的工作负载(对 `1_024 * 1_024` 个 `u32` 做对半切分的并行递增),只变 `heartbeat_interval` 一个参数。

**步骤 1**:新建二进制 crate `heartbeat-sweep`,依赖 chili:

```toml
# 示例代码:heartbeat-sweep/Cargo.toml
[package]
name = "heartbeat-sweep"
version = "0.1.0"
edition = "2021"

[dependencies]
# 指向你本地的 chili 克隆:
chili = { path = "../dragostis-chili" }
# 或使用已发布版本:
# chili = "0.2.1"
```

**步骤 2**:写入 main(与 `join_very_long` 完全同形的 `increment`,外加计时外壳):

```rust
// 示例代码:heartbeat-sweep/src/main.rs
use std::time::{Duration, Instant};

use chili::{Config, Scope, ThreadPool};

fn increment(s: &mut Scope, slice: &mut [u32]) {
    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let mid = slice.len() / 2;
            let (left, right) = slice.split_at_mut(mid);
            s.join(|s| increment(s, left), |s| increment(s, right));
        }
    }
}

fn run(label: &str, interval: Duration) {
    let tp = ThreadPool::with_config(Config {
        thread_count: None, // None = 用满 available_parallelism
        heartbeat_interval: interval,
    });
    let mut vals = vec![0u32; 1_024 * 1_024];

    let start = Instant::now();
    increment(&mut tp.scope(), &mut vals);
    let elapsed = start.elapsed();

    assert_eq!(vals, vec![1u32; 1_024 * 1_024]);
    println!("{label:>6} : {elapsed:?}");
}

fn main() {
    for _ in 0..3 {
        run("1us", Duration::from_micros(1));
        run("100us", Duration::from_micros(100));
        run("10ms", Duration::from_millis(10));
    }
}
```

计时区间只覆盖 `increment` 本体:线程池在 `start` 之前建好(线程 spawn 不计入),`Drop` 在 `elapsed` 取走之后发生(停机 join 不计入),三档可比。

**步骤 3**:`cargo run --release`(务必 release;debug 下差异毫无意义),把结果填进表格(**待本地验证**,以下为模板):

| heartbeat_interval | 第 1 次 | 第 2 次 | 第 3 次 | 中位数 |
| --- | --- | --- | --- | --- |
| 1µs | | | | |
| 100µs(默认) | | | | |
| 10ms | | | | |

**步骤 4:解释观察**。对照本讲源码,预期机理如下(具体数值以你的测量为准):

- **1µs(过高)**:睡眠步长 \(\delta = H/N\) 被压到一两百纳秒,心跳线程趋近忙轮询;每轮都要抢一次 `context.lock`、扫一遍登记表,与 worker 的 `job_is_ready` 等待、计算线程的 `heartbeat()` 投递争锁;旗被频繁立起,`join_heartbeat` 更常走加锁冷路径。分享及时,但**调度开销本身开始吃 CPU**。
- **100µs(默认)**:分享节奏与调度开销的平衡点,README 基准(见 u4-l3)正是基于它取得接近线性的加速。
- **10ms(过低)**:除每面旗初始自举的那次投递外,运行期间几乎没有新的升旗,空闲 worker 得不到再补给,并行度主要靠开局的级联自举撑着;总时长可能变长,但心跳开销趋近于零。

**步骤 5(可选,若差异不显著)**:这个负载非常轻(1M 次整数递增),三档差异可能淹没在线程启动与内存分配的噪声里。可把数组加大到 `4 * 1_024 * 1_024`、或把 `slice[0] += 1` 换成几次重复运算来放大信号,再对比趋势是否与步骤 4 的解释一致。

## 6. 本讲小结

- 心跳的本质是一个**周期性触发信号**:唯一的 `execute_heartbeat` 线程不给 worker 派活,只负责把各线程的旗立起来,「提醒」它们把本地队列队头的任务送上货架;真正昂贵的加锁投递仍由计算线程自己在冷路径完成。
- `Heartbeat` 用 `Weak<AtomicBool>` 登记:旗的生命周期由 `Scope` 的强引用独占,Scope 死后 `upgrade()` 失败,登记项由心跳线程的 `retain` **惰性清理**——没有显式注销、没有 Drop 里的锁,且弱引用与 `Instant` 的平凡析构保证了持锁清理的安全。
- 间隔**均摊**:睡眠步长 \(\delta = H/N\),使「每个线程」的心跳周期稳定在 \([H, H + H/N]\),与线程数无关;代价是每周期 \(\Theta(N^2)\) 的扫描量,\(N\) 很大时退化为高频轮询。
- 旗初始为 `true` 的**自举**设计让每个新 Scope 的第一次 `join` 立即获得一次投递机会,短命 Scope 不必干等一个周期。
- `scope_created_from_thread_pool` 只在两种情况下唤醒心跳线程:用户创建了新 Scope、池在停机;谓词 `len == num_workers && !is_stopping` 之所以能当「无用户工作」判据,靠的是 worker Scope **常驻**、且全部在心跳线程 spawn 之前登记完毕(barrier 时序保证)——空闲的池心跳开销为零。
- 升旗/读旗一律 `Relaxed`:旗是建议位,不发布数据;数据正确性由互斥锁与 `job.rs` 的 Channel 负责。

## 7. 下一步学习建议

心跳把任务送出线程后,「结果如何回来」依赖 `job.rs` 里的手写通道——下一讲 **u3-l2《job.rs 的 Channel:手写 park/unpark 单值通道》**将精读 `Channel`、`Sender`、`Receiver` 的 Pending/Waiting/Ready 三态状态机、先换状态再停车的防丢唤醒技巧,以及 `Acquire`/`AcqRel` 内存序在该通道中的配对,那是本讲反复借用的「数据同步由 Channel 保证」这句话的完整证明。之后再进入 u3-l3 的 Job 类型擦除体系。若想先换个角度巩固本讲,可以把 u2-l1 的 `TIMES` 降频参数与本讲的 `heartbeat_interval` 放在一起做二维扫描,体会「检查频率 × 升旗频率」两个旋钮的交互。
