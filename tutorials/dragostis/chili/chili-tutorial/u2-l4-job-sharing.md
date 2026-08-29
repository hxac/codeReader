# 任务共享与结果等待的完整链路

## 1. 本讲目标

前几讲我们把 `join` 的三条路径、`ThreadPool` 的生命周期、worker 主循环逐一拆开了。本讲把镜头对准其中最关键的一段:**一次真实的跨线程执行**——任务如何离开发起线程、如何被 worker 接手、发起线程又如何把结果等回来。

学完本讲,你应该能够:

1. 说清 `heartbeat()` 在锁内完成的「投递」动作每一步在做什么,以及为什么用 `Arc` 指针地址做 `shared_jobs` 的键。
2. 解释 `wait_for_sent_job` 的两段式策略:先用一次 `remove` 判断任务是否真的被偷走,再在等待结果的间隙「偷货架上的任务」帮忙干活。
3. 画出(或手写出)一次跨线程 `join` 的完整时序图,精确到 `LockContext` 上每一次加锁期间的状态变化。
4. 准确说出 `time` 字段与 `lock.time` 自增的真实作用——并知道一个容易被误解的细节:它记录投递顺序,但**并不决定**任务的弹出顺序。

## 2. 前置知识

本讲是并发链路精读,先把几个会用到的概念用通俗语言过一遍:

- **临界区(锁内代码)**:同一时刻只有一个线程能持有 `Mutex`。chili 把所有跨线程共享的调度账本(`LockContext`)都放在一把锁后面,所以「插入货架」「从货架取任务」「收回自己的任务」这三个动作是**互斥**的——这是本讲所有正确性论证的地基。
- **`BTreeMap`**:按键排序的有序映射。`pop_first()` 弹出**键最小**的条目。记住这一点,后面关于 `time` 的讨论全靠它。
- **指针地址当身份**:`Arc<T>` 分配在堆上,只要还有人持有它,这块内存就不会被释放,地址也不会被复用。因此「存活期内地址唯一」可以当作对象的身份标识(类似数据库主键)。
- **`Cell`(单线程内部可变性)**:允许对不可变引用背后的值做修改,但类型不承诺线程安全(`!Sync`)。只能用在「确知只有一个线程会碰它」的字段上。
- **忙等 vs 休眠等待**:忙等(busy-wait)是循环里反复检查条件、不交出 CPU;休眠等待是 `Condvar::wait` / `thread::park` 让操作系统把线程挂起。忙等烧 CPU 但响应快,还能顺便干活;休等待省 CPU 但睡着就什么都做不了。本讲的核心设计正是两者的混合。
- **`thread::Result<T>`**:`Result<T, Box<dyn Any + Send +>>` 的别名,`Err` 分支装的是 panic 的载荷,用于把 panic 从执行任务的线程搬运回发起线程(细节在 u4-l2 展开)。
- **内存序(一句话版)**:`Relaxed` 只保证原子性不保证可见性顺序;`Acquire` 读与 `AcqRel` 写配对后,能保证「写之前的所有写入,对读到该值的线程可见」。本讲只用到这一层,深入在 u3-l2。

另外承接 u2-l1 / u2-l3 已建立的两个结论,本讲直接使用:

- 本地 `JobQueue` 的**队头**是递归最外层、通常粒度最大的任务;
- `LockContext` 用一把 `Mutex` 罩住全部账本(`time`、`is_stopping`、`shared_jobs`、`heartbeats`),共享路径是冷路径,热路径完全不碰锁。

## 3. 本讲源码地图

本讲的主战场是 [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs),衔接点在 [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs)。

| 位置 | 作用 |
| --- | --- |
| [src/lib.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80) | `LockContext`:锁内账本,含 `time`、`shared_jobs`、`heartbeats` |
| [src/lib.rs:L98-L103](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L98-L103) | `pop_earliest_shared_job`:worker / 等待者从货架取任务 |
| [src/lib.rs:L280-L282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L280-L282) | `heartbeat_id`:把心跳 `Arc` 地址变成 `shared_jobs` 的键 |
| [src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) | `heartbeat()`:本讲模块一的主角,任务投递 |
| [src/lib.rs:L284-L316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316) | `wait_for_sent_job`:模块二、三的主角,等待与帮助 |
| [src/lib.rs:L348-L390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390) | `join_heartbeat`:调用方,串起投递与等待 |
| [src/lib.rs:L112-L145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L145) | `execute_worker`:取走并执行货架任务的 worker 主循环 |
| [src/job.rs:L255-L274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274) | `JobQueue::pop_front`:送出任务时创建 channel 并把 receiver 装回 |
| [src/job.rs:L49-L51](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L49-L51) | `Receiver::is_empty`:判空循环检查的「结果到了吗」 |
| [src/job.rs:L53-L81](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L53-L81) | `Receiver::recv`:真正阻塞取结果 |
| [src/lib.rs:L717-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L747) | `tests::join_wait`:本讲指定验证用例 |

## 4. 核心概念与源码讲解

### 4.1 heartbeat 任务投递

#### 4.1.1 概念说明

回忆 u2-l1:`join_heartbeat` 会把闭包 `a` 打包成一个 `Job` 压入**本地队列**,然后检查心跳标志。若标志为真,才调用 `heartbeat()`——这是整个库中**唯一**把任务送出线程的地方。

`heartbeat()` 要解决的问题:在不打扰热路径的前提下,把「我这个线程这里有一个值得并行的大任务」这个消息,安全地挂到一个所有 worker 都看得见的地方。这个地方就是 `LockContext::shared_jobs`——本讲称之为「货架」。

两个关键设计先说结论:

1. **货架的键是投递方的身份**。`shared_jobs: BTreeMap<usize, (u64, JobShared)>`,键 `usize` 是 `Scope` 心跳标志 `Arc<AtomicBool>` 的堆地址(由 `heartbeat_id()` 计算)。同一个 `Scope` 在货架上**至多挂一个**任务,由 `Entry::Vacant` 判重保证。
2. **每次成功投递都盖一个时间戳**。`lock.time` 是一把全局逻辑时钟,投递时读取、投递后自增,保证每个任务进入货架时都携带一个单调递增、全局唯一的序号。

#### 4.1.2 核心流程

```text
heartbeat():                              # 仅当本地心跳标志为 true 时被调用(冷路径)
    lock = context.lock.lock()            # ① 进入临界区
    time = lock.time                      # ② 读取当前逻辑时钟
    若 lock.shared_jobs 中没有键 heartbeat_id():   # ③ Vacant:本 scope 名下无挂账任务
        job = 本地队列.pop_front()         # ④ 取队头 = 递归最外层、粒度最大的任务
        若 job 存在:
            shared_jobs[heartbeat_id()] = (time, job)   # ⑤ 上货架
            lock.time += 1                # ⑥ 逻辑时钟 +1
            job_is_ready.notify_one()     # ⑦ 唤醒一个睡着的 worker
    heartbeat.store(false, Relaxed)       # ⑧ 复位一次性标志(锁内做,无妨)
    解锁
```

值得注意的三个细节:

- 第 ④ 步弹出的是**队头**。递归越深,压入的闭包粒度越小;队头是还留在本地队列里最老、最粗的任务。把它送出去,一次跨线程传输换来的工作量最大——这正是 chili「大量小计算、难以估计剩余量」定位下的正确选择。
- 第 ⑦ 步只 `notify_one`,因为货架上只多了一个任务,唤醒一个 worker 恰好够。
- 第 ⑧ 步无论是否真的投递都要执行:心跳标志是「一次性触发器」,不复位的话后续每次 `join` 都会撞进这条冷路径,热路径开销就失控了。

关于时间戳,给出它与等待时长的关系。设被送出的任务 `a` 在 worker 上需要执行 \( t_a \),发起线程执行 `b` 需要 \( t_b \),则发起线程在完成 `b` 之后需要等待的时长至多为:

\[
W = \max(0,\; t_a - t_b)
\]

本讲模块二的核心,就是如何把这段 \( W \) 的闲置变成有用功。而 `time` 时间戳的作用是让「谁先投递」成为可追溯的全局事实:任意时刻货架上的条目各自携带唯一且递增的序号。

**但必须澄清一个容易混淆的点**(承接 u2-l3 已经确立的结论):`pop_earliest_shared_job` 用的是 `BTreeMap::pop_first()`,弹出的是**键最小**的条目,而键是指针地址——**`time` 并不参与排序**。也就是说:

- `time` 与 `lock.time += 1` 保证的是**投递顺序被如实记录**(FIFO 语义的「记账」);
- 实际**弹出顺序**由 scope 的堆地址大小决定,与投递先后无关。

这一点我们通过 git 历史核实过:从初始提交起 `shared_jobs` 就是 `BTreeMap<usize, (u64, Job)>` 的形态,`pop_first()` 一直按键弹出;`time` 自始至终是记录性字段,而非排序键。由于单个 scope 同时至多挂一个条目,货架通常极小,这个「记账 FIFO、取出按地址」的差异在实践中影响有限,但读源码时必须心中有数。

#### 4.1.3 源码精读

先看键是怎么来的——把心跳 `Arc` 的地址转成 `usize`:

[src/lib.rs:L280-L282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L280-L282) —— `heartbeat_id` 用 `Arc::as_ptr` 取出心跳标志的堆地址再转为 `usize`,作为本 scope 在货架上的身份。地址能当身份用,是因为 `Scope` 存活期间一直持有这个 `Arc`(字段 `heartbeat`),堆块不释放、地址不被复用;scope 销毁后,`heartbeats` 表里的 `Weak` 失效,会被心跳线程的 `retain` 清理(见 [src/lib.rs:L166-L176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L166-L176)),而 `heartbeats` 的键是独立的自增 `heartbeat_index`,两表互不干扰。

再看投递本体:

[src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) —— `heartbeat()` 全文不足 16 行:加锁后先读 `lock.time`;`entry()` 返回 `Vacant` 时才继续(本 scope 名下没有挂账任务),`pop_front()` 从本地队列取出队头任务,插入 `(time, job)` 并 `lock.time += 1`、`notify_one` 唤醒一个 worker;最后无条件 `store(false)` 复位标志。

货架的数据结构定义:

[src/lib.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80) —— `LockContext` 中 `shared_jobs: BTreeMap<usize, (u64, JobShared)>`:键是投递 scope 的心跳地址,value 是 `(投递时间戳, 可跨线程执行的任务)`。`time: u64` 就是那把全局逻辑时钟。

投递时 `pop_front` 内部发生了一件对本讲至关重要的事:

[src/job.rs:L255-L274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274) —— `JobQueue::pop_front` 在送出任务之前**才创建 channel**(`channel()` 新建 `Arc<Channel>`),把 `receiver` 用 `Cell::set` 装回**留在发起线程栈上的那个 `Job`**(`job.receiver.set(Some(receiver))`),然后把 `sender` 连同任务指针一起打包成 `JobShared` 返回。这就是「接收器」的来源:任务被送走的那一刻,回信通道同步建好,收件凭证塞回了寄件人手里。

> 顺带一提:这段代码的 SAFETY 注释里还写着 `Cell<Option<NonNull<Future<T>>>>`,而实际字段早已是 `Cell<Option<Receiver<T>>>`——注释没跟上 redesign,阅读时以代码为准。

谁把任务取走?worker 的主循环:

[src/lib.rs:L118-L131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L118-L131) —— `execute_worker` 每轮先在**锁内**调用 `pop_earliest_shared_job` 取走一个货架任务,再在**锁外**执行它。取走发生在临界区内,这一点是模块二正确性论证的前提。

[src/lib.rs:L98-L103](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L98-L103) —— `pop_earliest_shared_job` 就是 `shared_jobs.pop_first()`,丢弃键与时间戳,只留 `JobShared`。注意 `pop_first` 按键(地址)弹出——前文已强调。

最后补一个承上启下的事实:[src/lib.rs:L84](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L84) 中 `new_heartbeat` 把标志初始化为 `true`。也就是说,**每个新 `Scope` 的第一次 `join_heartbeat` 必然尝试投递一次**,不必等心跳线程的第一个周期——这让短生命周期的 scope 也有机会分享工作。

#### 4.1.4 代码实践

**实践:亲眼看见一次跨线程投递。**

1. **实践目标**:在不修改 chili 源码的前提下,用外部可观察的证据(执行线程 id)证明「`a` 闭包真的跑在了另一个线程上」,即投递 → worker 取走 → 执行 三步都发生了。
2. **操作步骤**:
   - 在你的学习副本中新建 `tests/see_sharing.rs`(下面的代码为**示例代码**,不是项目原有文件):

     ```rust
     use std::{
         num::NonZero,
         sync::Mutex,
         thread::{self, ThreadId},
         time::Duration,
     };

     #[test]
     fn a_runs_on_another_thread() {
         let tp = chili::ThreadPool::with_config(chili::Config {
             thread_count: Some(NonZero::new(2).unwrap()),   // 1 个 worker + 发起线程
             heartbeat_interval: Duration::from_micros(1),
             ..Default::default()
         });
         let mut scope = tp.scope();

         let runner: Mutex<Vec<(&'static str, ThreadId)>> = Mutex::new(Vec::new());
         let main_id = thread::current().id();

         scope.join_with_heartbeat_every::<1, _, _, _, _>(
             |_| runner.lock().unwrap().push(("a", thread::current().id())), // a:被投递的一方
             |_| thread::sleep(Duration::from_micros(200)),                  // b:给 worker 留出取货时间
         );

         let ran_a = runner.lock().unwrap()[0].1;
         assert_ne!(ran_a, main_id, "a 应该跑在别的线程上");
     }
     ```

   - 运行 `cargo test --test see_sharing -- --nocapture`。
3. **需要观察的现象**:测试通过;若在断言前打印 `ran_a`,它是 worker 线程的 id 而非测试主线程 id。
4. **预期结果**:`join_with_heartbeat_every::<1>` 让每次 join 都走心跳路径,加上新 scope 标志初值为 `true`,`a` 在第一次 join 就被投递;`b` 睡 200µs 足够 worker(1µs 心跳间隔)被唤醒并取走任务。极端调度抖动下断言可能偶发失败,可加大 `b` 的睡眠时间。**待本地验证**。
5. 若暂时不方便建文件,可改用阅读方式:对照 [src/lib.rs:L717-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L747) 的 `join_wait` 用例,逐行标注其中触发投递的三个条件(`TIMES = 1`、标志置位、`Vacant`)分别由哪些代码行满足。

#### 4.1.5 小练习与答案

**练习 1**:`shared_jobs` 的键为什么用心跳 `Arc` 的指针地址,而不用线程 id 或一个全局自增 id?

**答案**:投递的主体是 `Scope` 而不是线程——同一线程先后可以创建多个 scope,一个 worker 执行偷来的任务时用的也是自己的 scope;而判重要求「同一投递方至多一条」。`Scope` 又没有内建唯一 id 字段,心跳 `Arc` 的堆地址在 scope 存活期内唯一且稳定(持有期间不会被释放或复用),是现成的身份。用它还与 `heartbeats` 表共享同一套身份来源(那张表靠 `Weak` 失效来清理),不需要给 `Scope` 增加任何字段。

**练习 2**:如果把 `Entry::Vacant` 的判重去掉,每次心跳检查都无条件投递,会发生什么?

**答案**:递归深处的每一层 join 都会往货架塞一个本 scope 的任务,货架被同一 scope 的多个细粒度任务占满。这破坏了「队头 = 最大粒度任务」的投递选择(变成把递归深处的小任务也送出去),增加锁内工作量与无效唤醒;收回逻辑也更复杂——发起线程要处理「名下多个任务,有的被偷有的没被偷」的组合。判重把不变量收紧为「一 scope 一任务」,收发双方都只需处理两种状态。

**练习 3**:`heartbeat()` 末尾的 `heartbeat.store(false, Ordering::Relaxed)` 若被遗漏,后果是什么?

**答案**:标志一旦被心跳线程置位就永远为真,之后**每一次** `join_heartbeat` 都会进入这条 `#[cold]` 路径——加锁、查 `entry`、可能反复投递。热路径与冷路径的比例(由 `TIMES` 控制的 1/64 检查密度)被彻底破坏,`join` 的簿记开销大幅上升。这行是「一次性触发器」的复位开关。

### 4.2 等待时偷任务

#### 4.2.1 概念说明

任务送出去之后,发起线程并不闲着:它先执行 `b`(自己那一半)。`b` 做完后,调用方 `join_heartbeat` 通过 `job.take_receiver()` 发现任务确实被送出过(栈上 `Job` 的 `Cell` 里有 receiver,见 4.1.3 的 `pop_front`),于是进入 `wait_for_sent_job`。

这个函数要回答两个问题:

1. **我的任务到底被拿走了没有?** 货架是公共的,任务可能还躺在那儿没人要。
2. **如果已被拿走、结果还没回来,这段等待怎么过?** chili 的答案不是马上睡觉,而是「**等待即帮忙**」(helping):循环里不断从货架偷别人的任务来执行,直到自己的结果到了、或者货架空了才去阻塞等待。

这是一个经典的 work-stealing 式帮助策略:等待者的时间没有被浪费,而是回流到系统的任务池里。

#### 4.2.2 核心流程

```text
wait_for_sent_job(receiver):
    # 第一段:一次性判定「是否真的被偷走了」
    lock = 加锁
    若 lock.shared_jobs.remove(heartbeat_id()) 返回 Some:   # 条目还在货架 → 没人拿走
        解锁;返回 None          # 信号:调用方应自己执行 a(见 join_heartbeat 的 None 分支)
    解锁                        # remove 返回 None ⟹ 一定已被某线程取走

    # 第二段:帮助式等待
    while receiver.is_empty():          # 结果还没到
        lock = 加锁;job = pop_earliest_shared_job();解锁
        若 job 存在:
            job.execute(self)           # 偷来就干:用「自己这个 scope」执行别人的闭包
        否则:
            break                       # 货架空了,没忙可帮
    返回 Some(receiver.recv())          # 取结果(必要时 park)
```

为什么第一段的 `remove` 能当作判定?因为对货架的**写操作**只有三处,全部发生在同一把 `Mutex` 的临界区内:

| 操作 | 位置 | 效果 |
| --- | --- | --- |
| 投递插入 | `heartbeat()` 的 `e.insert(...)` | 条目出现,键 = 投递方地址 |
| 取走 | worker 或等待者的 `pop_earliest_shared_job()` | 条目消失 |
| 收回 | `wait_for_sent_job` 开头的 `remove` | 条目消失 |

所以「`remove` 返回 `Some`」与「已被取走」是同一时刻下的**互补且完备**的事件:

\[
P(\text{仍在货架}) + P(\text{已被取走}) = 1, \quad \text{且两者互斥}
\]

判定因此是精确的,没有第三种状态,也不存在竞态——锁保证了原子性。

帮助式等待的收益可以用 4.1.2 的不等式量化:发起线程需等待 \( W = \max(0, t_a - t_b) \)。若等待期间它从货架偷走并执行了 \( n \) 个任务、每个耗时 \( t_i \),则只要 \( \sum_{i=1}^{n} t_i \le W \),这些工作就是「白捡的」——既没有推迟自己的返回(结果本来就没好),又替别的线程消化了队列。只有当货架空、实在无忙可帮时,才 `break` 出去走真正的阻塞等待。

还有一个**非常容易误判**的推论(4.2.4 的实践会验证它):在**单 scope** 的递归中,第二段的偷任务分支**永远不会命中**。理由链是:

1. 能进入第二段,说明第一段 `remove` 返回 `None`——本 scope 唯一的货架条目已被取走;
2. 本 scope 再次投递的唯一时机是下一次 `join_heartbeat` 开头的 `heartbeat()`,而现在线程正处在 `wait_for_sent_job` 里,根本不在 join 中;
3. 货架的键判重又保证本 scope 至多一条。

三条合起来:单 scope 场景下,等待者面对的货架**必然是空的**,循环第一轮就 `break`。偷任务真正发挥作用的场景是**多 scope 并发**(比如 u2 里的 `concurrent_scopes` 测试形态):别的 scope 投递的任务躺在货架上,我的等待者顺手取走执行。

#### 4.2.3 源码精读

等待函数全文:

[src/lib.rs:L284-L316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316) —— `wait_for_sent_job` 分两段:开头在临界区内 `remove(&self.heartbeat_id())`,返回 `Some` 就立刻 `return None`(任务没被偷走,交还调用方自己跑);`while receiver.is_empty()` 循环里,每轮在锁内 `pop_earliest_shared_job()` 偷一个任务、锁外 `job.execute(self)` 执行,偷不到就 `break`;最后 `Some(receiver.recv())` 取回结果。

调用方如何消费这三种返回值:

[src/lib.rs:L368-L380](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L368-L380) —— `join_heartbeat` 中 `b` 执行完毕后,`job.take_receiver()` 拿到 `Some(receiver)` 时调用 `wait_for_sent_job`:`Some(Ok(val))` 正常拿结果;`Some(Err(e))` 说明闭包 panic 了,`panic::resume_unwind(e)` 在发起线程上恢复传播;`None` 说明任务没被偷走,`unsafe { (stack.take_once())(self) }` 由发起线程亲自执行闭包 `a`——SAFETY 注释解释了为何此时 `take_once` 只会被调用一次(任务从未真正跨线程执行过)。

偷来的任务用**谁的 scope** 执行:

[src/lib.rs:L303-L309](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L303-L309) —— `job.execute(self)` 传的是**发起线程自己的 `&mut Scope`**。于是偷来的闭包若继续 `join`,新任务会压进**发起线程的本地队列**、沿它的递归展开;而偷来任务自己的结果,则通过它随身的 `sender` 送回**原投递线程**的 receiver——两条线互不干扰。

worker 侧的对照(与 u2-l3 呼应,此处只取与本讲相关的两句):

[src/lib.rs:L138-L141](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L138-L141) —— worker 干完活(或货架本来就没货)后,在 `job_is_ready` 条件变量上休眠,直到 `notify_one` 或停机。发起线程的等待者与 worker 消费的是同一个货架、同一把锁,行为却不同:worker 无限期等待新任务,等待者只「帮到自己的结果到来为止」。

被偷任务的执行终点:

[src/job.rs:L214-L225](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L214-L225) —— `JobShared::execute` 调用任务自带的 `harness`,最终在 [src/job.rs:L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175) 以 `sender.send(panic::catch_unwind(...))` 收尾:闭包的结果(或 panic 载荷)经 channel 送回发起线程。至此「偷」的闭环完成。

#### 4.2.4 代码实践

**实践:验证「单 scope 偷不到任务」的推论。**

1. **实践目标**:用日志证据确认 4.2.2 末尾的推论——在 `join_wait` 这种单 scope 用例中,`wait_for_sent_job` 的偷任务分支不会命中;并解释原因。
2. **操作步骤**(在**你自己的学习副本**中修改,观察后还原;不要在原仓库上改):
   - 打开 [src/lib.rs:L297-L313](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L313),在 `if let Some(job) = job {` 分支内加一行(示例代码):

     ```rust
     eprintln!("[{:?}] stole a job while waiting", thread::current().id());
     ```

   - 运行 `cargo test join_wait -- --nocapture`。
3. **需要观察的现象**:测试通过(10 个元素都被加一),但标准错误中**不会出现**任何 `stole a job while waiting`。
4. **预期结果**:单 scope 下,进入等待分支就意味着本 scope 的唯一货架条目已被取走,而等待期间本 scope 不可能再投递,货架恒空,循环立即 `break`。若想看到日志命中,把 4.1.4 的测试改造成两个线程各自 `tp.scope()` 跑长递归(参考 [src/lib.rs:L807-L832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L807-L832) `concurrent_scopes` 的形态),让双方都有任务挂在货架上。**待本地验证**。
5. 观察完记得还原源码,并重跑 `cargo test` 确认无改动残留。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `wait_for_sent_job` 开头的一次 `remove` 就足以判定「任务是否被偷走」?请用货架上的三类写操作说明。

**答案**:货架的全部写操作——`heartbeat()` 的插入、worker/等待者的 `pop_first` 取走、本处的 `remove` 收回——都发生在同一把 `Mutex` 的临界区内。因此任一时刻「条目仍在」与「已被取走」互斥且完备;`remove` 返回 `Some` 当且仅当没人取走过。判定无竞态,也不需要轮询。

**练习 2**:发起线程在等待期间偷来一个任务执行,这个任务的子 join 会进入谁的本地队列?它的结果又送给谁?

**答案**:子 join 进入**发起线程**(等待者)的本地队列,因为 `job.execute(self)` 传入的是等待者自己的 `&mut Scope`,后续递归沿它的 `job_queue` 展开;而任务的结果通过它创建时(`pop_front`)随身携带的 `sender` 送回**原投递线程**的 receiver。执行权与结果归属是分离的。

**练习 3**:`wait_for_sent_job` 返回 `None` 时,调用方为什么可以直接 `stack.take_once()` 执行闭包,而不担心闭包已被别的线程取走?

**答案**:`None` ⟺ `remove` 返回 `Some` ⟺ 任务从未被任何线程取走。任务的执行只可能发生在 `JobShared::execute` 里,而那要求先从货架取走;既然从未取走,`JobStack::take_once` 必然是第一次也是唯一一次调用(源码 SAFETY 注释表述的正是这一契约)。同时被 `remove` 出来的 `JobShared`(连同它的 `sender`)被直接丢弃,channel 两端一同释放,不留悬空。

### 4.3 接收器判空循环

#### 4.3.1 概念说明

「接收器判空循环」指 `wait_for_sent_job` 第二段的 `while receiver.is_empty()`。它是帮助式等待的**节拍器**:每干完一件偷来的活(或者第一轮刚进来),先看一眼「我的结果到了吗」,到了就收工,没到继续偷。

这里有两个层面的设计:

1. **为什么用 `is_empty()` 轮询,而不是直接 `recv()`?** `recv()` 是阻塞调用:结果没到就把线程 park 掉。线程一旦睡着,既不能偷任务也不能响应任何事。chili 把 `recv()` 推迟到**货架也空了**的最后一刻——先用廉价的原子读(`is_empty` 只是一次 `Acquire` 的 `state` 加载)探路,能不睡就不睡。
2. **`receiver` 从哪来?** 回顾 4.1.3:任务被 `pop_front` 送出时才创建 channel,`receiver` 装回栈上 `Job` 的 `Cell`,`sender` 随 `JobShared` 出境。发起线程做完 `b` 之后 `take_receiver()` 取出。所以「有没有 receiver」本身就是「任务有没有被送出过」的判别——这就是 `join_heartbeat` 区分「等待结果」与「本地执行」的开关。

#### 4.3.2 核心流程

把整个判定整理成状态表(进入 `wait_for_sent_job` 的前提是 `take_receiver()` 返回了 `Some`):

| 检查时刻 | 货架上本 scope 条目 | `receiver.is_empty()` | 动作 | 线程状态 |
| --- | --- | --- | --- | --- |
| 进入时 | 仍在(`remove` → `Some`) | — | 返回 `None`,调用方本地执行 `a` | 运行 |
| 进入时 | 已被取走(`remove` → `None`) | `false`(结果已 `Ready`) | 跳过循环,`recv()` 立即取值 | 运行 |
| 循环条件 | — | `true` | 锁内偷货架任务,偷到则执行 | 运行(帮忙) |
| 循环内 | — | `true` 且货架空 | `break` | 转入阻塞 |
| `break` 后 | — | `true` | `recv()`:CAS `Pending→Waiting`,成功则 `park` | 休眠,直到 `send` 唤醒 |

用时序语言描述:发起线程从「忙等 + 帮忙」降级为「休眠等待」的**唯一条件**是「结果未到且货架空」。反过来,只要还有忙可帮,线程绝不睡觉。而一旦 `recv()` 里 CAS 成功进入 `Waiting`,后续唤醒完全由 `Sender::send` 的 `swap(Ready)` + `unpark` 驱动——那套「先写 `waiting_thread` 再换状态」的防丢唤醒机制属于 u3-l2 的范围,本讲只需要知道接口语义:

- `is_empty()`:结果是否已经写入(读 `state` 是否为 `Ready`,Acquire);
- `recv()`:取结果,必要时休眠直至对方 `send`。

内存序的配对关系一句话:`is_empty` 的 **Acquire 读** 与 `send` 的 **AcqRel 换写**([src/job.rs:L96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L96) `state.swap(State::Ready, AcqRel)`)构成发布-获取对,保证等待线程看到 `Ready` 的同时也能看到 `val` 里结果的写入——不会读到「状态好了但值还没到」的撕裂视图。

#### 4.3.3 源码精读

判空循环本体:

[src/lib.rs:L297-L315](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L315) —— `while receiver.is_empty()` 每轮先做一次廉价原子读;结果没到则锁内取货架、锁外执行;`break` 之后的 `receiver.recv()` 是整个流程中第一次真正的阻塞点。循环体把「检查结果」放在每一段帮忙**之后**,保证一拿到结果就立刻退出,不多干一件活。

接收器判空的实现:

[src/job.rs:L49-L51](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L49-L51) —— `is_empty` 仅加载一次 `state` 并与 `State::Ready` 比较,`Acquire` 序与 `send` 的 `AcqRel` 配对。注意它**不拿任何锁**——与需要进临界区的偷任务相比,这是纯热查询,所以循环把它放在每轮的第一步。

接收器的归宿与阻塞取值:

[src/job.rs:L53-L81](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L53-L81) —— `recv` 先把自己的 `Thread` 写进 `waiting_thread`,再 CAS `Pending→Waiting`,成功才 `park`;醒来后从 `val` 取出结果。本讲只消费它的语义(「取结果,必要时睡」),三态状态机的完整证明留给 u3-l2。

receiver 的装配点(承 4.1.3,这里看字段):

[src/job.rs:L140-L145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L140-L145) —— `Job` 的第三个字段 `receiver: Cell<Option<Receiver<T>>>` 初始为 `None`;[src/job.rs:L185-L187](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L185-L187) 的 `take_receiver` 用 `Cell::take` 一次性取出。`Cell` 之所以可用,是因为这个字段只在**发起线程**手上被写入(`pop_front` 发生在 `heartbeat()` 里,而 `heartbeat()` 由本线程调用,且写完立刻把 `JobShared` 送走、`Cell` 从此不再跨线程)。

最后看调用侧的岔路口:

[src/lib.rs:L366-L389](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L366-L389) —— `b` 完成后的分叉:`take_receiver()` 返回 `Some` 走 `wait_for_sent_job`(本讲主线);返回 `None` 说明任务从未被 `pop_front` 送出,`pop_back()` 把它从本地队列收回,`take_once` 直接执行。收发两方的所有路径在这里闭合成完整的二元判定:**送出过 → 等(或收回);没送出 → 自己跑**。

#### 4.3.4 代码实践

**实践:填写 channel 的「时刻表」。**

1. **实践目标**:把 4.1–4.3 的链路固化成一张数据归属表,做到闭卷也能填对 channel 两端在每个时刻的下落。
2. **操作步骤**:
   - 准备一张五列表格,行是四个时刻,列如下(**示例表格**,请先自己填再对答案):

     | 时刻 | channel 是否已创建 | `receiver` 在谁手里 | `sender` 在谁手里 | `state` |
     | --- | --- | --- | --- | --- |
     | T1:`join_heartbeat` 刚把 `a` 入本地队列 | | | | |
     | T2:`heartbeat()` 内 `pop_front` 刚返回 | | | | |
     | T3:发起线程做完 `b`,`take_receiver()` 之后 | | | | |
     | T4:worker 执行完闭包,`send` 返回之后 | | | | |

   - 对照源码填空:[src/job.rs:L141](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L141)(初始 `Cell::new(None)`)、[src/job.rs:L265-L267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L265-L267)(创建与装回)、[src/job.rs:L269-L273](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L269-L273)(sender 随 `JobShared` 出境)、[src/job.rs:L96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L96)(`swap(Ready)`)。
3. **需要观察的现象**:填完后检查自己是否能在不看答案的情况下,从 T1 推到 T4。
4. **预期结果**(参考答案,可对照):
   - T1:未创建(本地队列里的 `Job` 只有指针和 harness);`receiver`/`sender` 不存在;`state` 不存在。
   - T2:已创建;`receiver` 已装回**发起线程栈上 `Job` 的 `Cell`**;`sender` 在**即将跨线程的 `JobShared`** 里;`state = Pending`。
   - T3:已创建;`receiver` 已被发起线程 `take` 出来握在手里;`sender` 在执行任务的 worker 那里(或已随执行结束被消费);`state = Pending` 或 `Ready`(若 worker 已完成)。
   - T4:`sender` 已被 `send` 消费(它拿走 `self`);`state = Ready`;`val` 装着 `Box<thread::Result<T>>`,等 `recv` 取走。
5. 若某格填错,回到对应源码行重走一遍——这张表就是本讲全部内容的最小闭环。本实践为源码阅读型,**无需运行即成立**。

#### 4.3.5 小练习与答案

**练习 1**:既然最终都要 `recv()`,为什么不一进 `wait_for_sent_job` 就直接调用它?

**答案**:`recv()` 在结果未到时会 park 线程,睡着期间无法偷任务。先循环 `is_empty()` 探路的好处:(1) 结果已到时零成本返回(一次 Acquire 原子读,不加锁);(2) 结果未到但货架有活时,把等待时间转化为有用功;(3) 只有两头都无望(结果未到且货架空)才付出 park 的代价。这是「忙等帮忙 + 最后一刻休眠」的混合策略。

**练习 2**:循环条件里 `is_empty()` 用的是 `Acquire` 而不是 `Relaxed`,它和 `send` 里的哪个操作配对?去掉这个保证会出什么问题?

**答案**:与 `Sender::send` 中 `state.swap(State::Ready, Ordering::AcqRel)` 配对。配对后形成 happens-before:`send` 在 swap 之前对 `val` 的写入(装结果的 `Box`),对任何读到 `Ready` 的线程可见。若改用 `Relaxed`,可能观察到 `state == Ready` 却读不到(或读到旧的)`val` 内容——状态与数据撕裂,等待者拿到空值。

**练习 3**:channel 为什么延迟到 `pop_front`(送出任务时)才创建,而不是 `Job::new` 就创建好?

**答案**:大多数 `join` 走本地路径,任务从未跨线程,channel 根本用不上(本地分支是 `pop_back` + `take_once`,结果直接在栈上交接)。`pop_front` 是「确认要送出」的那一刻,在此刻创建避免了热路径上的堆分配(`Arc<Channel>` + 内部 `Box`)——这与 chili「把开销从每次 join 摊薄到真正跨线程的少数次数」的整体设计一致。

## 5. 综合实践

**手绘一次跨线程 `join` 的时序图,并用 `tests::join_wait` 验证。**

这是本讲指定的主实践,把三个模块串成一条时间线。

### 5.1 任务

手绘(纸上或绘图工具)一张时序图,包含**四条生命线**:发起线程 T0、心跳线程 HB、worker W1、`LockContext`(锁内账本,画成最右侧的窄条)。要求覆盖以下事件,并标注每一步账本的变化(货架内容、`time` 值):

1. T0 调用 `join(a, b)` → `a` 打包入本地队列尾;
2. HB 置位 T0 的心跳标志(Relaxed);
3. T0 在 `join_heartbeat` 中发现标志为真 → 进入 `heartbeat()`;
4. 锁内:`time` 读取、`Vacant` 判重、`pop_front`(含 channel 创建、receiver 装回)、插入货架、`time += 1`、`notify_one`;
5. T0 执行 `b`;
6. W1 被唤醒,锁内 `pop_first` 取走任务,锁外 `job.execute`;
7. W1 侧:`take_once` → 执行 `a` → `catch_unwind` → `sender.send`;
8. T0 完成 `b`,`take_receiver()` → `Some`;
9. T0 进入 `wait_for_sent_job`:锁内 `remove` → `None`;`is_empty` 循环(单 scope 下货架空)→ `break` → `recv()` park;
10. W1 的 `send` 检测到 `Waiting` → `unpark(T0)`;
11. T0 取回结果,返回 `(ra, rb)`。

### 5.2 参考模板

```text
T0(发起线程)          HB(心跳线程)         W1(worker)           LockContext(锁内)
   |                     |                    |                    |
   | join(a,b)           |                    |                    |
   | a 入本地队列 [a]     |                    |                    |
   |   标志=true ────────| store(true)        |                    |
   | heartbeat():        |                    |                    |
   |  ┌─ lock ───────────────────────────────────────────────────┐|
   |  │ time=t; Vacant? 是;                                    ||
   |  │   pop_front: channel(); receiver→Job.Cell               ||
   |  │   shared_jobs[addr(T0)] = (t, JobShared); time=t+1      ||
   |  │   notify_one(job_is_ready)                              ||
   |  └─ unlock; store(false) ──────────────────────────────────┘|
   | 执行 b ...           |                    |← 被唤醒            |
   |                     |                    |  ┌ lock           |
   |                     |                    |  │ pop_first       |
   |                     |                    |  └ unlock         |
   |                     |                    |  execute(a)       |
   |                     |                    |  send: swap(Ready)|
   | b 完成; take_receiver=Some                |                    |
   |  ┌ lock: remove(addr)→None ────────────────────────────────┐||
   |  └ unlock                                                ──┘||
   | is_empty()=true → 货架空 → break          |                    |
   | recv(): CAS(Pending→Waiting) → park       |                    |
   |                     |                    |  检测 Waiting      |
   | ← unpark ────────────────────────────────|  unpark(T0)        |
   | recv 返回 ra         |                    |                    |
   | 返回 (ra, rb)        |                    |                    |
```

(此为文字版参考图,你的手绘应更精细——例如标出每次锁的获取/释放区间。)

### 5.3 验证步骤

1. 运行 `cargo test join_wait -- --nocapture`,确认测试通过——这验证链路的**结果正确性**(10 个元素都被加一,见 [src/lib.rs:L742-L746](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L742-L746))。
2. 对照用例配置确认时序前提:[src/lib.rs:L717-L722](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L722) 中 `thread_count = 2`(即 1 个 worker + 发起线程)、`heartbeat_interval = 1µs`;[src/lib.rs:L731](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L731) 的 `join_with_heartbeat_every::<1>` 让每一层 join 都检查心跳;[src/lib.rs:L733](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L733) 的 `sleep(10µs)` 拉长被投递任务的执行时间,保证 worker 有充分窗口取货。
3. 用 4.1.4 的 `ThreadId` 实验补上图中「W1 执行 `a`」一步的**实证**(证明跨线程真的发生)。
4. 用 4.2.4 的日志实验核对你图中「`is_empty` 循环 → 货架空 → break」一段(单 scope 下等待者确实偷不到任务)。
5. 检查你的图:第 4 步的六个锁内动作是否都在同一个临界区里?第 9 步的 `remove` 与第 6 步的 `pop_first` 是否画成互斥的?若是,图与源码一致。

以上运行结果均为**待本地验证**(我未替你执行命令)。

## 6. 本讲小结

- **投递是唯一的跨线程出口**:`heartbeat()` 在一个临界区内完成「读时钟 → `Vacant` 判重 → `pop_front` 队头任务(此时才创建 channel、把 receiver 装回栈上 `Job` 的 `Cell`)→ 插入货架 → `time += 1` → `notify_one`」,最后复位一次性标志。
- **货架的键是投递 scope 的心跳 `Arc` 地址**:存活期内地址唯一稳定,配合 `Entry::Vacant` 判重,保证一个 scope 同时至多挂一个任务——送出的永远是队头那个粒度最大的任务。
- **`time` 是记账不是排序**:全局逻辑时钟给每次投递盖单调递增的唯一时间戳,如实记录 FIFO 投递顺序;但 `pop_earliest_shared_job` 用 `pop_first()` 按键(地址)弹出,`time` 不参与排序。
- **等待分两段**:先在锁内 `remove` 判定任务是否真的被偷走(`Some` → 没被偷,自己收回执行;`None` → 已被取走,必须等);再进入帮助式忙等。
- **等待即帮忙**:`while receiver.is_empty()` 循环里不断偷货架上的任务执行(用发起线程自己的 scope),只有结果未到且货架也空时才 `recv()` 休眠;单 scope 递归中货架恒空,偷任务只在多 scope 并发时真正生效。
- **执行权与结果归属分离**:偷来的任务沿等待者的本地队列继续递归,结果却经自带的 `sender` 送回原投递线程;`is_empty` 的 Acquire 读与 `send` 的 AcqRel 换写配对,保证不出现状态与数据的撕裂。

## 7. 下一步学习建议

本讲结束时,你已经走完 `join` 从投递到收结果的全部主链路。接下来有两个自然方向:

- **u3-l1(心跳调度机制)**:本讲中 HB 线程只以「置位标志」出现。下一讲精读 `execute_heartbeat`:`Weak` 引用如何让失效 scope 的心跳被 `retain` 自动清理、心跳间隔如何在多个线程间均摊(`heartbeat_interval / heartbeats.len()`)、以及 `scope_created_from_thread_pool` 条件变量的唤醒时机。
- **u3-l2(job.rs 的 Channel)**:本讲把 `recv` 当黑盒(「取结果,必要时睡」)。下一讲拆开它:`Pending/Waiting/Ready` 三态状态机、「先写 `waiting_thread` 再 CAS」的防丢唤醒技巧、以及 `park/unpark` 的协作细节。

建议在继续之前,把 5.1 的时序图重画一遍——不看本讲文字、只凭源码画。画得出来,说明本讲真正消化了。
