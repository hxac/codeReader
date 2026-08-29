# join 的三条执行路径：顺序、心跳与降频检查

## 1. 本讲目标

上一讲（u1-l3）我们学会了调用 `scope.join(a, b)`，并且知道它只承诺"may 并行"——是否真的跨线程由调度决定。本讲要打开这个黑盒，精读 `Scope` 里四个紧密相关的方法：

- `join`：公共入口；
- `join_with_heartbeat_every`：分流器（降频检查层）；
- `join_heartbeat`：具备"分享任务给其他线程"能力的路径；
- `join_seq`：零开销的纯顺序路径。

学完本讲，你应该能够：

1. 说清楚 `join_count` 对 `TIMES` 取模是如何把"检查心跳标志"这个动作降频到每次调用的 \( 1/\text{TIMES} \) 的；
2. 解释 `job_queue.len() < 3` 这个条件为什么能决定"哪些 join 调用有资格走可能分享任务的路径"；
3. 独立画出一次 `join` 调用从入口到返回的完整分支图，包括 `join_heartbeat` 内部"是否真正送出任务"的岔路口。

## 2. 前置知识

### 2.1 fork-join 与 "may 并行"（承接 u1-l3）

`join(a, b)` 把计算分叉成两支再汇合。它只保证两支的结果按参数顺序返回；至于两支是先后在当前线程跑，还是一支被别的线程抢走，调用者无感也无权干预。本讲要回答的正是：**这个决定是在哪里、按什么规则做出的**。

### 2.2 AtomicBool 与 Relaxed 内存序

`AtomicBool` 是一个可以跨线程安全读写的 `bool`。读写时可以附带"内存序"（`Ordering`）：

- `Relaxed`：只保证这一次读/写本身的原子性，不建立线程间的同步关系；
- `Acquire` / `AcqRel` 等：还会建立 happens-before 同步（u3-l2 的通道会用到）。

本讲的心跳标志全程用 `Relaxed`，因为它只是个"提示灯"：读到一个稍旧的值，后果仅仅是晚一拍分享任务，无伤大雅。真正交接任务时的线程安全由 `Mutex` 串行化保证（见 4.2 节）。

### 2.3 const 泛型与"2 的幂取模"

`join_with_heartbeat_every::<TIMES, _, _, _, _>` 里的 `TIMES` 是**编译期常量参数**（const 泛型）。编译器因此能直接知道取模的除数：当 `TIMES` 是 2 的幂（如默认的 64）时，`x % TIMES` 会被优化成一次按位与 `x & 63`，代价接近一次普通整数运算。

### 2.4 VecDeque 的队头与队尾

`JobQueue` 内部是 `VecDeque<NonNull<Job>>`（见 [src/job.rs:236-237](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L236-L237)）。队列存的是指向任务的**裸指针**：

- `push_back`：新任务压到队尾（最新）；
- `pop_front`：从队头取走**最早**压入的任务；
- `pop_back`：从队尾取回自己刚压入、还没人碰的任务。

记住"队头 = 最早 = 递归最外层 = 剩余工作量通常最大"，这是理解 4.2 节的关键。

### 2.5 `#[cold]` 属性

`#[cold]` 是给优化器的提示："这个函数很少被调用"。优化器会把它挪出热点指令路径，让常见路径的指令缓存更紧凑。心跳标志置位后的处理函数就标了 `#[cold]`（[src/lib.rs:318](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318)）。

## 3. 本讲源码地图

| 位置 | 作用 |
| --- | --- |
| [src/lib.rs:234-239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L234-L239) | `Scope` 结构体：本讲的主角，`join_count: u8` 就藏在其中 |
| [src/lib.rs:391-417](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L391-L417) | 公共入口 `join`：固定以 `TIMES = 64` 委托给分流器 |
| [src/lib.rs:419-456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L419-L456) | 分流器 `join_with_heartbeat_every`：取模降频 + 队列长度判断 |
| [src/lib.rs:348-390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390) | 心跳路径 `join_heartbeat`：入队 → 查标志 → 执行 b → 回收 a |
| [src/lib.rs:318-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) | `heartbeat()`：真正把队头任务送入共享队列的地方（`#[cold]`） |
| [src/lib.rs:335-346](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L335-L346) | 顺序路径 `join_seq`：先 b 后 a，无任何簿记 |
| [src/lib.rs:284-316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316) | `wait_for_sent_job`：任务被送出后等待结果的收尾（细节留到 u2-l4） |
| [src/lib.rs:147-189](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L147-L189) | `execute_heartbeat`：专职心跳线程，周期性把标志置 true（细节留到 u3-l1） |
| [src/job.rs:239-275](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L239-L275) | `JobQueue` 的 `len` / `push_back` / `pop_front` / `pop_back` |

本讲的主战场是 `src/lib.rs`；`src/job.rs` 只借用它的队列操作来解释 `join_heartbeat` 的每一步在物理上做了什么。

## 4. 核心概念与源码讲解

### 4.1 模块一：join 计数与降频——分流器 `join_with_heartbeat_every`

#### 4.1.1 概念说明

chili 的核心矛盾是：**`join` 会被调用成千上万次（README 基准里每个节点一次），但"值得把任务送给别的线程"的时机极少**——默认配置下心跳线程每 100µs 才把标志置位一次（[src/lib.rs:469-476](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L469-L476)），而一次 join 可能只要几纳秒。

如果每次 `join` 都走完整的心跳路径（入队、查标志、出队），簿记开销就会淹没计算本身。解决办法是**降频（throttling）**：给每个 `Scope` 配一个 8 位计数器 `join_count`，每调用一次自增并对 `TIMES` 取模；**只有计数器归零的那一次（以及本地队列很短时）才走心跳路径，其余全部走纯顺序路径**。

检查密度从 1 降到 \( 1/\text{TIMES} \)（默认 \( 1/64 \)），损失的信息却几乎为零：标志本身最多每 100µs 才变一次 true，中间的 63 次检查注定落空，不做也罢。

#### 4.1.2 核心流程

分流器的完整逻辑只有三行：

```text
join_with_heartbeat_every::<TIMES>(a, b):
    join_count = (join_count + 1) % TIMES      # 计数循环：1, 2, ..., TIMES-1, 0, 1, ...
    若 join_count == 0 或 本地队列长度 < 3:
        走 join_heartbeat(a, b)                # 心跳路径（可能分享）
    否则:
        走 join_seq(a, b)                      # 顺序路径（零簿记）
```

以 `TIMES = 8`、队列长度始终 ≥ 3 为例，计数器取值与路径选择是：

| 第 n 次调用 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `join_count` | 1 | 2 | 3 | 4 | 5 | 6 | 7 | **0** | 1 | 2 |
| 路径 | seq | seq | seq | seq | seq | seq | seq | **心跳** | seq | seq |

两个特殊情况值得单独记住：

- `TIMES = 1`：\( (x+1) \bmod 1 \equiv 0 \)，每次调用都归零 → **每一次 join 都走心跳路径**。仓库测试 `join_wait` 正是用它强制高频分享（见 4.1.4）。
- 本地队列长度 < 3：**绕过降频**，无条件走心跳路径。这个条件的意义在 4.2.1 详细讨论——它同时起到"限制本地队列长度上限"的作用。

#### 4.1.3 源码精读

先看 `Scope` 的字段，计数器就住在这里：

```rust
pub struct Scope<'s> {
    context: Arc<Context>,
    job_queue: ThreadJobQueue<'s>,
    heartbeat: Arc<AtomicBool>,
    join_count: u8,
}
```

这是 [src/lib.rs:234-239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L234-L239)。四个字段分别是：全局上下文（含共享队列与锁）、本线程的任务队列、心跳标志（`Arc` 是为了让心跳线程能通过 `Weak` 弱引用追踪它，见 u3-l1）、以及本讲的计数器。`job_queue` 的类型 `ThreadJobQueue` 是个枚举——worker 线程的 `Scope` 借用池里常驻的队列，普通线程的 `Scope` 自持一个队列（[src/lib.rs:191-195](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L191-L195)），两者都自动解引用为 `JobQueue`，所以 `self.job_queue.len()` 在两种身份下都成立。

公共入口 `join` 一行搞定，固定 `TIMES = 64`：

```rust
pub fn join<A, B, RA, RB>(&mut self, a: A, b: B) -> (RA, RB)
where
    A: FnOnce(&mut Scope<'_>) -> RA + Send,
    B: FnOnce(&mut Scope<'_>) -> RB + Send,
    RA: Send,
    RB: Send,
{
    self.join_with_heartbeat_every::<64, _, _, _, _>(a, b)
}
```

见 [src/lib.rs:409-417](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L409-L417)。

> **读代码而非只读注释的一个实例**：`join` 的文档注释写着 "skips checking for a heartbeat every **16** calls"（[src/lib.rs:395-396](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L395-L396)），而实现传的是 `::<64>`（[src/lib.rs:416](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L416)）。两者不一致，**以代码为准**：默认每 64 次检查一次。相比之下，`join_with_heartbeat_every` 自己的文档和 doctest 注释（"Skip checking 7/8 calls"，[src/lib.rs:433](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L433)）与实现是一致的。

分流器本体：

```rust
pub fn join_with_heartbeat_every<const TIMES: u8, A, B, RA, RB>(
    &mut self,
    a: A,
    b: B,
) -> (RA, RB)
where { /* A、B、RA、RB 的约束与 join 相同 */ }
{
    self.join_count = self.join_count.wrapping_add(1) % TIMES;

    if self.join_count == 0 || self.job_queue.len() < 3 {
        self.join_heartbeat(a, b)
    } else {
        self.join_seq(a, b)
    }
}
```

见 [src/lib.rs:438-456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456)，核心三行在 [src/lib.rs:449-455](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L449-L455)。逐点说明：

- `wrapping_add(1)`：因为 `join_count` 始终落在 \( [0, \text{TIMES}-1] \subset [0, 254] \)，自增后最大 255，其实**永远不会溢出**；`wrapping_add` 是防御式写法，同时向读者表明"这里不存在溢出语义问题"。
- `job_queue.len()`：一次普通字段读取（`VecDeque` 的长度是普通数据，不需要原子操作——队列只被本线程读写）。
- 整个分流器的成本：一次 `u8` 加法、一次取模（64 是 2 的幂，编译为按位与）、一次比较、一次长度读取。这就是 README 里"每节点约 3.5ns 摊销开销"在 join 侧的微观基础之一。

#### 4.1.4 代码实践

**实践目标**：亲手验证计数器循环与 `TIMES = 1` 的特殊性。

**操作步骤**（阅读 + 小实验）：

1. 打开 [src/lib.rs:669-690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L669-L690) 的测试 `join_long`，确认它用的是默认 `join`（即 `TIMES = 64`）。
2. 再打开 [src/lib.rs:716-747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747) 的测试 `join_wait`，找到 `join_with_heartbeat_every::<1, _, _, _, _>`，注意它同时配置了 `thread_count: 2` 和 `heartbeat_interval: 1µs`——三个参数配合才能在测试里稳定制造跨线程分享。
3. 在仓库根目录运行这两个现成测试（不修改任何源码）：

   ```bash
   cargo test --lib join_long
   cargo test --lib join_wait
   ```

4. 在**你自己的笔记**里推演：`TIMES = 1` 时 `join_count` 恒为 0，所以 `join_wait` 的每一层递归都走心跳路径、每次都查标志。

**需要观察的现象**：两个测试都通过（结果正确性与走哪条路径无关——这正是 "may 并行" 的体现）。

**预期结果**：`join_long`、`join_wait` 均 ok。具体运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`TIMES = 8`、本地队列长度始终 ≥ 3 时，前 10 次调用的 `join_count` 序列是什么？哪几次走心跳路径？

**答案**：1, 2, 3, 4, 5, 6, 7, 0, 1, 2。只有第 8 次（计数器归零）走 `join_heartbeat`，其余 9 次走 `join_seq`。

**练习 2**：为什么 `join_count == 0` 的判断放在自增取模**之后**，使得第一次调用（从 0 起步）不走心跳路径？

**答案**：先自增再判断意味着第一次调用得到 `1 % TIMES = 1 ≠ 0`。从语义上说这无关紧要（早一次晚一次不影响正确性）；从实现上说，"自增-归零-判断"写成一条语句序列最自然。真正影响行为的是另一个条件：只要队列长度 < 3，**第一次调用也会走心跳路径**。

**练习 3**：如果把默认 `TIMES` 从 64 改成 256，会对行为和性能各产生什么影响？

**答案**：行为上，检查密度从 \( 1/64 \) 降到 \( 1/256 \)，对心跳标志（默认每 100µs 置位一次）的响应最多再延迟约 192 次 join——通常仍远小于 100µs，几乎无感。性能上，队列饱和时进入 `join_heartbeat` 的频率更低、纯顺序路径占比更高，每次 join 的平均开销更小；但队列极短的浅层递归不受影响（那种情况由 `len() < 3` 主导，本来每次都走心跳路径）。代价是任务送出的时机可能略微滞后。注意 256 不是 `u8` 能表达的问题（TIMES 是 u8，最大 255，`256` 无法通过编译——若想做这个实验只能用 `128`）。

### 4.2 模块二：join_heartbeat——真正可能分享任务的路径

#### 4.2.1 概念说明

`join_heartbeat` 是"有资格分享任务"的路径，但它**并不必然分享**。它的策略可以概括为一句话：

> 先把 a 打包成一个待认领的任务压到本地队尾备着；如果此刻心跳标志亮了，就把**队头**（最早、通常最大粒度）的任务送进共享队列；然后当前线程照常先执行 b；执行完再看 a 有没有被别人领走——没被领走就自己收回执行，被领走了就等结果。

这里必须解释分流器里 `job_queue.len() < 3` 的含义。递归 fork-join 中，每次走 `join_heartbeat` 都会往本地队列压一个任务（外层的 a），然后通过 b 继续深入。于是队列会随递归加深而变长。`len() < 3` 的作用是：**一旦本地已经攒了 3 个待认领任务，就不再新增**——后续 join 退化为顺序路径，只有每 64 次一次的心跳检查例外。

为什么封顶在 3 就够？关键在 `heartbeat()` 的去重检查（见下）：**每个 Scope 同一时刻最多只有一个任务躺在共享队列里**。既然一次只能送出一个，队列攒得再长也没有额外收益；而保持队头始终有一个"新鲜的、外层大粒度"任务作候选，才是心跳到来时最想要的。封顶同时把队列的内存与簿记开销限制在常数。（这一段是对设计动机的推断，代码层面可验证的事实是：去重检查存在、队列因该条件近似稳定在长度 3 附近。）

#### 4.2.2 核心流程

```text
join_heartbeat(a, b):
    stack = JobStack::new(a)            # 闭包 a 装进栈上的 JobStack（无堆分配）
    job   = Job::new(&stack)            # 栈上构造 Job：裸指针 + harness + 空 receiver
    job_queue.push_back(&job)           # 队尾压入指向 job 的裸指针

    若 heartbeat 标志 == true:           # 一次 Relaxed 原子读
        heartbeat()                     # 岔路口：真正送出（见下）

    rb = b(self)                        # 当前线程先执行 b（b 内部可继续递归 join）

    若 job.take_receiver() == Some:     # Some ⟺ job 已被 pop_front 弹出过（即已送出）
        wait_for_sent_job(receiver)     # 等结果；等待期间还会帮别的线程干活
            ├─ 自己的还在共享队列 → 取回 → 本地执行 a
            └─ 已被别的线程拿走 → 边消化共享任务边等 → recv() 收结果
    否则:
        job_queue.pop_back()            # 没人要，从队尾收回
        a = stack.take_once()           # 把闭包从栈里取出来
        (a(self), rb)                   # 本地执行 a
```

`heartbeat()` 本身（`#[cold]`，很少执行）：

```text
heartbeat():
    加锁 LockContext
    若 shared_jobs 中没有本 Scope 的条目（Vacant）:
        job = job_queue.pop_front()     # 取队头 = 最早 = 最外层任务
        若取到了:
            pop_front 内部为 job 创建 channel 并装上 Receiver
            把 (time, job) 存入 shared_jobs
            lock.time += 1
            job_is_ready.notify_one()   # 唤醒一个等待中的 worker
    标志写回 false                       # 无论本轮是否真的送出
```

#### 4.2.3 源码精读

`join_heartbeat` 全文（[src/lib.rs:348-390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390)）：

```rust
fn join_heartbeat<A, B, RA, RB>(&mut self, a: A, b: B) -> (RA, RB)
where { /* A、B、RA、RB: Send，同 join */ }
{
    let stack = JobStack::new(a);
    let job = Job::new(&stack);

    // SAFETY: `job` is alive until the end of this scope.
    unsafe { self.job_queue.push_back(&job) };

    if self.heartbeat.load(Ordering::Relaxed) {
        self.heartbeat();
    }

    let rb = b(self);

    if let Some(receiver) = job.take_receiver() {
        let ra = match self.wait_for_sent_job(receiver) {
            Some(Ok(val)) => val,
            Some(Err(e)) => panic::resume_unwind(e),
            // SAFETY: job 没被真正送出，闭包仍在栈上，take_once 只会被调用一次
            None => unsafe { (stack.take_once())(self) },
        };

        (ra, rb)
    } else {
        self.job_queue.pop_back();

        // SAFETY: job 已从队尾弹出，不可能再被别的路径取走闭包
        (unsafe { (stack.take_once())(self) }, rb)
    }
}
```

分步解读：

1. **打包 a（第 355-356 行）**：`JobStack::new(a)` 把闭包放进 `UnsafeCell<ManuallyDrop<F>>`（[src/job.rs:113-117](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L117)），`Job::new(&stack)` 在栈上造一个只含**裸指针、函数指针和空 Receiver 槽**的 `Job`（[src/job.rs:141-145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L141-L145)）。全程零堆分配——这是"低开销"的第一根支柱。
2. **入队（第 360 行）**：`push_back` 只是把 `NonNull::from(&*job)` 压进 `VecDeque`（[src/job.rs:247-249](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L247-L249)）。SAFETY 注释明确了契约：**job 必须存活到被弹出为止**——它在本函数栈帧上，天然满足。
3. **查标志（第 362-364 行）**：一次 `Relaxed` 原子读。标志由专职心跳线程在 `execute_heartbeat` 里周期性置 true（[src/lib.rs:165-176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L165-L176)）。**注意顺序：先入队、后查标志**——这样本轮刚入队的任务就能被本轮心跳送出；反过来（先查后入队）的话，标志会在查完后被清空，新任务要白白等下一个心跳周期。
4. **执行 b（第 366 行）**：当前线程立刻开始干 b 的活。此时 a 只是"挂在队列上待认领"。
5. **二选一回收（第 368-389 行）**：判据是 `job.take_receiver()`（[src/job.rs:185-187](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L185-L187)）。`receiver` 槽**只在一处被填充**：`JobQueue::pop_front`（[src/job.rs:255-274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274)）在弹出任务时会现场创建 channel 并塞进 `job.receiver`。而本地队列的 `pop_front` 只会被本线程在 `heartbeat()` 里调用。所以 `Some(receiver)` 是一个确凿的信号：**这个任务已经（或即将）属于别的线程**，本线程只能等（`wait_for_sent_job`，[src/lib.rs:284-316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316)；若结果里是 `Err`，说明闭包在别的线程 panic 了，用 `resume_unwind` 在本线程重放——细节留到 u4-l2）。`None` 则说明任务还在队尾躺着，`pop_back`（[src/job.rs:251-253](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L251-L253)）收回指针，再用 `stack.take_once()`（[src/job.rs:126-133](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L126-L133)）把闭包从栈上取出来本地执行。

`heartbeat()` 全文（[src/lib.rs:318-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333)）：

```rust
#[cold]
fn heartbeat(&mut self) {
    let mut lock = self.context.lock.lock().unwrap();

    let time = lock.time;
    if let Entry::Vacant(e) = lock.shared_jobs.entry(self.heartbeat_id()) {
        if let Some(job) = self.job_queue.pop_front() {
            e.insert((time, job));

            lock.time += 1;
            self.context.job_is_ready.notify_one();
        }
    }

    self.heartbeat.store(false, Ordering::Relaxed);
}
```

四个要点：

- **去重**：`shared_jobs` 以 `heartbeat_id()`（即 `Arc<AtomicBool>` 的地址，[src/lib.rs:280-282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L280-L282)）为键，`Entry::Vacant` 检查保证**每个 Scope 同时最多送出一个任务**——这是 4.2.1 中"队列封顶 3 就够"的直接依据。
- **送队头**：`pop_front` 取的是最早入队的任务，也就是递归最外层、剩余工作量最大的那支。联系 u1-l3 的结论：`join_very_long`（对半切分）的队头是半个数组，值得送；`join_long`（链式剥离）的队头只处理 1 个元素，送出去几乎无收益——**分享是否有价值，由递归形状决定**。
- **唤醒**：`notify_one` 叫醒一个睡在 `job_is_ready` 上的 worker 线程来领活（worker 侧的完整循环是 u2-l3 的主题）。
- **清标志**：无论本轮有没有真的送出任务，标志都写回 false，等下一个心跳周期。

> **顺带一个诚实的观察**：`heartbeat()` 给 `shared_jobs` 的值里记录了投递序号 `time`（`lock.time` 自增），但通读全库后可以确认，当前代码中 `lock.time` 只在 [src/lib.rs:322](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L322) 和 [src/lib.rs:327](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L327) 出现，从未被读取用于排序；取任务用的是 `BTreeMap::pop_first`（按键排序，见 [src/lib.rs:98-102](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L98-L102)）。`time` 字段很可能是重构（"Refactored and simplified jobs" 提交）后的遗留。读库时保持这种"注释/字段与实际用法核对"的习惯，比相信叙事更有价值。

#### 4.2.4 代码实践

**实践目标**：用可观察的副效应，验证"a 与 b 的**开始**顺序在多数情况下是 b 先、a 后，且被送出时顺序可能颠倒，但返回值元组顺序恒定"。

**操作步骤**（以下为示例代码，不是仓库原有内容；请在仓库外新建一个 crate 做，不要改动 chili 源码）：

1. 新建 crate：`cargo new chili-order-lab`，在其 `Cargo.toml` 中加依赖 `chili = { path = "<本仓库的绝对或相对路径>" }`。
2. 把下面的 `main.rs` 写进去并 `cargo run` 多次：

   ```rust
   use std::sync::Mutex;
   use chili::Scope;

   fn main() {
       for _ in 0..5 {
           let log: Mutex<Vec<&str>> = Mutex::new(Vec::new());
           let mut s = Scope::global();

           let (ra, rb) = s.join(
               |_| log.lock().unwrap().push("a-start"),
               |_| log.lock().unwrap().push("b-start"),
           );

           let _ = (ra, rb);
           println!("{:?}", log.into_inner().unwrap());
       }
   }
   ```

**需要观察的现象**：多数运行打印 `["b-start", "a-start"]`；偶尔（如果某个心跳周期恰好把 a 送给了 worker）可能出现 `["a-start", "b-start"]`。两种输出都合法。

**预期结果**：顺序以 `b` 先为主——因为两条路径里都是 `b(self)` 先在当前线程执行、a 后被本地执行或被送出后才开始。若看到 `a` 先，说明 a 恰好被送出并在其他线程先行启动。具体的翻转频率取决于机器与心跳周期，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`heartbeat()` 里的 `Entry::Vacant` 检查防的是什么？

**答案**：防止同一个 `Scope` 在共享队列里同时存在两个任务。以 `heartbeat_id` 为键，若已有条目（上一次送出的还没被领走/取回），本轮直接跳过送出。这同时解释了本地队列封顶 3 的合理性：一次只能送一个，攒更多没有意义。

**练习 2**：为什么 `heartbeat()` 用 `pop_front` 而不是 `pop_back`？

**答案**：`pop_front` 取最早入队的任务，即递归最外层的 a，剩余工作量通常最大；把大粒度任务送出去、把细粒度留给自己，才能摊薄跨线程交接的固定成本。`pop_back` 取到的会是刚压入的最内层小任务，分享价值低。（这是"偷最粗的活"这一工作窃取家族的通用直觉在 chili 中的体现。）

**练习 3**：`join_heartbeat` 里，`job.take_receiver()` 返回 `Some` 的充要条件是什么？这个条件由哪段代码制造？

**答案**：充要条件是 `job` 曾被 `JobQueue::pop_front` 弹出（无论之后是被别的线程领走执行，还是仍躺在共享队列里）。`receiver` 槽唯一的填充点是 [src/job.rs:267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L267) 的 `job.receiver.set(Some(receiver))`。而本地队列的 `pop_front` 只会在本线程的 `heartbeat()` 中被调用，所以这个信号在本线程侧是完全确定的。

### 4.3 模块三：join_seq——零开销的顺序路径

#### 4.3.1 概念说明

`join_seq` 是"什么都不做"的路径：不加锁、不碰原子变量、不碰队列，纯粹地**先执行 b 再执行 a**。它是分流器降频后的受益者——在队列饱和的深层递归里，63/64 的 join 都落到这里。

值得强调的是执行顺序：`join_seq` **先 b 后 a**。这不是随意的：`join_heartbeat` 里当前线程也是先跑 b、让 a 在队列里等着。两条路径保持同构，使得"全顺序执行"与"心跳路径的本地分支"行为一致，副效应顺序（b 的先发生）在顺序模式下是稳定的。

还要澄清一个边界：分流器本身的那次"自增取模 + 比较 + 读队列长度"是**每次 join 都要付的**，不属于 `join_seq` 的开销。所谓零开销，指 `join_seq` 函数体内除调用者闭包外没有任何额外指令。

#### 4.3.2 核心流程

```text
join_seq(a, b):
    rb = b(self)     # 先 b；b 内部可以继续递归 join
    ra = a(self)     # 后 a
    返回 (ra, rb)    # 元组顺序永远按参数顺序，与执行顺序无关
```

#### 4.3.3 源码精读

全文只有五行（[src/lib.rs:335-346](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L335-L346)）：

```rust
fn join_seq<A, B, RA, RB>(&mut self, a: A, b: B) -> (RA, RB)
where
    A: FnOnce(&mut Scope<'_>) -> RA + Send,
    B: FnOnce(&mut Scope<'_>) -> RB + Send,
    RA: Send,
    RB: Send,
{
    let rb = b(self);
    let ra = a(self);

    (ra, rb)
}
```

注意两点：

- 两个闭包都拿到 `&mut Scope`（即 `self` 的可变借用依次传递），所以**顺序路径下依然可以递归分叉**——b 内部、a 内部各自继续调用 join，只是这些嵌套调用同样会被分流器各自裁决。
- 返回 `(ra, rb)`：先算出的 `rb` 放在元组第二位。调用者永远按参数顺序解构，感知不到执行顺序。

#### 4.3.4 代码实践

**实践目标**：验证顺序路径下副效应顺序稳定为"先 b 后 a"。

**操作步骤**（示例代码，接 4.2.4 的同一个外部 crate）：

1. 构造一个**本地队列必然饱和**的深度递归，让深层 join 大概率走 `join_seq`：

   ```rust
   use std::sync::Mutex;
   use chili::Scope;

   fn descend(s: &mut Scope, depth: u32, log: &Mutex<Vec<(u32, &str)>>) {
       if depth == 0 {
           return;
       }
       s.join(
           |_| log.lock().unwrap().push((depth, "a")),
           |s| {
               log.lock().unwrap().push((depth, "b"));
               descend(s, depth - 1, log);
           },
       );
   }

   fn main() {
       let log = Mutex::new(Vec::new());
       descend(&mut Scope::global(), 8, &log);
       let log = log.into_inner().unwrap();
       for (depth, who) in &log {
           println!("depth {depth}: {who}");
       }
   }
   ```

2. `cargo run`，观察每一层 depth 上 `a` 与 `b` 的相对顺序。

**需要观察的现象**：在未被送出的层（绝大多数）上，`b` 总是先于同层的 `a` 出现；跨层之间，深层（depth 小）的记录先大量出现（因为 b 一路先深入），随后浅层的 `a` 陆续补上。

**预期结果**：日志呈现"右链先走到底、左路（a）自底向上回填"的模式，且每个 depth 内 `b` 先于 `a`。若个别层出现同层 `a` 先于 `b`，说明那一层的 a 恰好被送出并由 worker 先执行了——同样是正确行为。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`join_seq` 为什么不把 a 也入队、保持与 `join_heartbeat` 完全对称？

**答案**：入队的唯一目的是"让任务有机会被别的线程认领"。顺序路径存在的意义就是省掉这笔簿记（VecDeque 操作、标志读、take_receiver 判断）；如果还要入队再马上收回，降频就白做了。两条路径的非对称正是性能优化的落点。

**练习 2**：顺序路径下，若闭包 b 里发生了 panic，会怎么样？

**答案**：`b(self)` 直接 unwinding，`a` 根本不会执行，panic 沿调用栈向上传播——与普通顺序代码一致。只有当 a 被**送出**到别的线程执行时，才会走 `wait_for_sent_job` → `Some(Err(e))` → `resume_unwind` 的跨线程重放路径（u4-l2 的主题）。

**练习 3**：`join_seq` 的两个闭包能同时借用外部的可变数据吗（例如都写同一个 `&mut Vec`）？

**答案**：不能，借用检查会拒绝——这正是"may 并行"语义在类型层面的体现：既然两支可能真的并行，就必须像并行代码一样证明数据不冲突（惯用法是 `split_at_mut` 之类的拆分借用，回顾 u1-l3）。顺序执行只是运行时的巧合，不是编译器可以依赖的保证。

### 4.4 模块四：全景分支图——一次 join 调用的完整走位

#### 4.4.1 概念说明

把前三个模块拼起来。所谓"三条执行路径"，指的是一次 `join` 调用可能抵达的三种终态：

1. **顺序路径**（`join_seq`）：彻底本地，零簿记；
2. **心跳路径未触发分享**：入了队、查了标志，但标志是 false——"白准备一场"，最后 `pop_back` 收回本地执行；
3. **心跳路径触发分享**：标志是 true，`heartbeat()` 把队头任务送入共享队列——这是唯一会产生跨线程执行的分支。

#### 4.4.2 核心流程

```text
s.join(a, b)                                ← 入口：等价 join_with_heartbeat_every::<64>
  │
  ▼
join_with_heartbeat_every::<TIMES>          ← 分流器（降频检查层）
  │   join_count = (join_count + 1) % TIMES
  │
  ├── join_count == 0 或 本地队列长度 < 3 ?
  │
  ├── 是 ──► join_heartbeat(a, b)           ← 路径②/③：心跳路径
  │            │   stack = JobStack::new(a)
  │            │   job = Job::new(&stack)
  │            │   job_queue.push_back(&job)
  │            │
  │            ├── heartbeat 标志 == true ?
  │            │     ├── 是 ──► heartbeat()：        ← 路径③：真正送出
  │            │     │      锁 LockContext
  │            │     │      Vacant 检查通过则 pop_front 队头任务
  │            │     │        → 装上 channel → 存入 shared_jobs
  │            │     │        → lock.time += 1 → notify_one 唤醒 worker
  │            │     │      标志写回 false
  │            │     └── 否 ──────────────────────  ← 路径②：白准备一场
  │            │
  │            │   rb = b(self)              （当前线程先执行 b）
  │            │
  │            ├── job.take_receiver() == Some ?
  │            │     ├── 是 ──► wait_for_sent_job(receiver)
  │            │     │        ├── 自己送的还在共享队列 → 取回 → 本地执行 a
  │            │     │        └── 已被别的线程拿走 → 边消化共享任务边等 → recv()
  │            └─────└── 否 ──► pop_back() → take_once() → 本地执行 a
  │
  └── 否 ──► join_seq(a, b)                  ← 路径①：顺序路径
               rb = b(self)；ra = a(self)；返回 (ra, rb)
```

配合一个量化直觉：设平均单次 join 耗时为 \( t_{join} \)，心跳周期为 \( T \)（默认 \( T = 100\,\mu s \)）。队列饱和时期望每 \( 64 \cdot t_{join} \) 才检查一次标志；只要 \( 64 \cdot t_{join} \ll T \)，降频几乎不会错过任何心跳窗口，却把簿记开销压缩到 \( 1/64 \)。这是"用极小的响应延迟换取大幅开销下降"的经典折中。

#### 4.4.3 源码精读

三个终态对应的三段代码互相对照：

| 终态 | 代码位置 | 关键指令 |
| --- | --- | --- |
| ① 顺序 | [src/lib.rs:335-346](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L335-L346) | `b(self)`、`a(self)`，无其他 |
| ② 心跳未分享 | [src/lib.rs:360-364](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L360-L364) 与 [src/lib.rs:381-389](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L381-L389) | `push_back`、一次 Relaxed 读、`pop_back`、`take_once` |
| ③ 心跳分享 | [src/lib.rs:318-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) 与 [src/lib.rs:284-316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316) | 加锁、`pop_front`、写 `shared_jobs`、`notify_one`、`recv()` 等结果 |

再看两个仓库测试在图上的走位差异：

- `join_long`（[src/lib.rs:669-690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L669-L690)）：链式剥离，队列在递归下降中很快达到 3，此后约 63/64 走终态①；偶发终态③时送出的队头任务只 increment 一个元素——分享几乎无收益，它主要是压力测试。
- `join_very_long`（[src/lib.rs:692-714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714)）：对半切分，队头任务是半个数组，一旦走到终态③，另一个线程能领走一大块活——这是 chili 设计目标中的理想负载。

#### 4.4.4 代码实践

**实践目标**：把分支图与真实递归形状对上号。

**操作步骤**（纯阅读，不改代码）：

1. 重读 [src/lib.rs:673-683](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L673-L683)（`join_long` 的 `increment`）和 [src/lib.rs:696-708](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L696-L708)（`join_very_long` 的 `increment`）。
2. 对每个函数回答三个问题：a 闭包的工作量是多少？队头任务的粒度随深度如何变化？终态③送出的任务值不值得？
3. 把答案填进下面这张表（答案见练习 1）：

| | a 闭包处理多少元素 | 队头（最外层 a）粒度 | 分享价值 |
| --- | --- | --- | --- |
| join_long | ? | ? | ? |
| join_very_long | ? | ? | ? |

**需要观察的现象**：两个函数的 join 调用次数相同（\( 2^k - 1 \) 量级、均为 N−1 次），但队头粒度天差地别。

**预期结果**：见练习 1 的答案。

#### 4.4.5 小练习与答案

**练习 1**：填完 4.4.4 的表。

**答案**：`join_long`：a 处理 1 个元素（`head[0] += 1`）；队头=最外层 a，也只处理 1 个元素；分享价值极低。`join_very_long`：a 处理约一半区间（`left`）；队头是最外层的左半区间（约 N/2 个元素）；分享价值高。两者 join 次数同为 N−1，但可分享的粒度完全不同——再次印证 u1-l3 的结论"递归形状决定并行收益"。

**练习 2**：画出 `TIMES = 1` 且心跳周期极短（如 1µs）时，`join_wait` 测试（[src/lib.rs:716-747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747)）中一次 join 的大致走位。

**答案**：`TIMES = 1` 时分流器恒走 `join_heartbeat`；1µs 心跳 + 10µs 的人工 sleep（在 a 闭包里）保证标志几乎必然在某个 join 处为 true，于是频繁进入终态③：队头任务被送入 `shared_jobs`，被配置了 2 个 worker 的池领走执行，发起线程在 `wait_for_sent_job` 里等结果（期间还会顺手消化其他共享任务）。这就是该测试能稳定触发跨线程执行的原因。

**练习 3**：如果把你自己的 workload 从对半切分改成"切下 1/N、其余递归"，三条路径的走位会怎么变？

**答案**：a 闭包与队头任务的粒度变成约 \( 1/N \) 的区间。join 次数量级不变（仍为 N−1 次合并成链式深度），但每次终态③能送出的活变小、且递归深度变为线性于元素数（深栈风险），分享的摊销收益下降——介于 `join_very_long` 与 `join_long` 之间，且越靠近"链式"一端越差。

## 5. 综合实践

**任务**：给 `join_heartbeat` 逐行写注释，并用实验对比 `TIMES = 1` 与 `TIMES = 64` 的耗时差异。

### 步骤一：逐行注释 `join_heartbeat`

把 [src/lib.rs:348-390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390) 抄进你的笔记，为每一行写出中文注释。你的注释必须能回答这五个问题（答案都能在本讲 4.2.3 找到）：

1. `JobStack::new(a)` 与 `Job::new(&stack)` 各自发生在哪个分配区域？（栈）
2. 为什么 `push_back` 必须发生在 `heartbeat.load` **之前**？
3. `take_receiver()` 返回 `Some` / `None` 分别对应什么物理事实？
4. 两个分支里的 `stack.take_once()` 为什么都只被调用一次（SAFETY 注释在防什么）？
5. `Some(Err(e)) => panic::resume_unwind(e)` 处理的是什么场景？

### 步骤二：对比 `TIMES = 1` 与 `TIMES = 64`

在仓库**外**新建 crate（不要修改 chili 源码）：

```bash
cargo new chili-join-lab
cd chili-join-lab
# 在 Cargo.toml 的 [dependencies] 加：
# chili = { path = "<指向本仓库的路径>" }
```

写入如下 `main.rs`（**示例代码**，复刻了 `join_long` 的链式递归形状，并用 const 泛型参数化 TIMES）：

```rust
use std::time::Instant;
use chili::Scope;

const N: usize = 1024;      // 与 tests::join_long 相同规模
const REPS: u32 = 2000;     // 重复多次让总耗时可测

fn increment_every<const TIMES: u8>(s: &mut Scope<'_>, slice: &mut [u32]) {
    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let (head, tail) = slice.split_at_mut(1);
            s.join_with_heartbeat_every::<TIMES, _, _, _, _>(
                |_| head[0] += 1,
                |s| increment_every::<TIMES>(s, tail),
            );
        }
    }
}

fn bench<const TIMES: u8>(label: &str) {
    let mut scope = Scope::global();
    let start = Instant::now();

    for _ in 0..REPS {
        let mut vals = [0; N];
        increment_every::<TIMES>(&mut scope, &mut vals);
        assert_eq!(vals, [1; N]);       // 正确性必须先于性能
    }

    let elapsed = start.elapsed();
    println!(
        "{label}: 总耗时 {elapsed:?}，单轮 {:?}，每次 join 摊销 {:?}",
        elapsed / REPS,
        elapsed / (REPS * N as u32),
    );
}

fn main() {
    bench::<1>("TIMES=1  （每次 join 都走心跳路径）");
    bench::<64>("TIMES=64（默认 join 的行为）");
}
```

运行：

```bash
cargo run --release
```

同时在仓库里跑一次现成用例作为基准（它们分别对应两种参数的"官方版本"）：

```bash
cargo test --lib join_long
cargo test --lib join_wait
```

### 需要观察的现象

1. 两组结果数组都正确（`assert_eq!` 全部通过）——走哪条路径不影响正确性。
2. `TIMES=1` 的单轮耗时高于 `TIMES=64`：链式递归下降阶段队列会一路增长（`TIMES=1` 时每次 join 都入队），每次 join 多付一次 `push_back` + 一次 Relaxed 原子读 + 一次 `pop_back` + `take_receiver` 判断；`TIMES=64` 在队列达到 3 之后，绝大多数 join 只付分流器的"自增取模 + 比较"。

### 预期结果

- 正确性：两种 TIMES 下断言全部通过。
- 耗时：`TIMES=1` 慢于 `TIMES=64`，差距来自本地簿记而非并行度（默认 100µs 心跳 + 毫秒级总时长下，真实的跨线程分享在两种参数里都很少发生——队头任务只有 1 个元素的增量，即使送出也几乎无收益）。具体倍数强烈依赖机器，**待本地验证**。
- 进阶验证（可选）：把 `Config { heartbeat_interval: Duration::from_micros(1), .. }` 配给一个自建 `ThreadPool` 并用 `tp.scope()` 替换 `Scope::global()`，看差距是否变化，体会"簿记开销"与"分享收益"两个变量的分离。

## 6. 本讲小结

- 一次 `join` 先经过分流器 `join_with_heartbeat_every`：`join_count` 对 `TIMES` 取模实现降频，`join_count == 0` **或** 本地队列长度 < 3 才走 `join_heartbeat`，否则走 `join_seq`；默认 `TIMES = 64`（文档注释里的 16 是滞后的，以代码为准）。
- `job_queue.len() < 3` 既让浅层递归无条件具备分享资格，也把本地队列的长度封了顶——配合 `heartbeat()` 的 Vacant 去重（每个 Scope 同时最多送出一个任务），队列攒更长没有收益。
- `join_heartbeat` 的仪式是：栈上打包 a → 队尾入队 → 查一次 Relaxed 心跳标志 → 先执行 b → 依 `take_receiver()` 判定 a 是否已被弹出送出：是则等结果（`wait_for_sent_job`），否则 `pop_back` 本地执行。
- 真正的分享只发生在 `#[cold]` 的 `heartbeat()` 里：加锁、`pop_front` 取**队头**（最外层、通常最大粒度）任务、装入共享队列、`notify_one` 唤醒 worker、清标志。
- `join_seq` 是零簿记的顺序路径，且固定**先 b 后 a**，与 `join_heartbeat` 的本地分支同构；返回元组永远按参数顺序。
- 分享的价值由递归形状决定：`join_very_long` 的队头是半个数组（值得送），`join_long` 的队头只有 1 个元素（送了白送）。

## 7. 下一步学习建议

本讲只回答了"谁决定分享、在哪分享"。接下来两讲补齐这条链路的另外两端：

- **u2-l2（`ThreadPool` 生命周期与 `Config` 配置）**：心跳标志由谁、以什么节奏置位？`heartbeat_interval` 与线程数如何影响本讲的三个终态占比？`Config`、`OnceLock` 全局池与 `Drop` 停机流程都在那一讲。
- **u2-l3（worker 线程与共享上下文）**：`notify_one` 叫醒的 worker 主循环长什么样？`shared_jobs` 如何被领走？这是分支图中"另一端"的完整实现。
- **u2-l4（任务共享与结果等待的完整链路）**：`wait_for_sent_job` 的"边偷任务边等"与 `Receiver::recv` 的细节，把本讲路径③的收尾讲透。
- 若你想先横向巩固，可以回到 [src/lib.rs:419-456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L419-L456) 闭卷重写一遍分流器伪代码，再对照 4.4.2 的分支图查漏。
