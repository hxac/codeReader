# 让出与线程信息：current_thread_index、yield 家族与防饿死

## 1. 本讲目标

前两讲我们配置了自定义线程池（u7-l1），并理解了 `install` 的「帮工等待」（u7-l2）。本讲把视角拉到**正在池里运行的任务自己**：当一个任务运行到一半，想知道「我在哪」「我能不能把执行权让给别人」「我名下还有多少没干完的活」，Rayon 提供了一组查询与让出 API。

学完本讲你应能：

1. 用 `current_thread_index`（自由函数与 `ThreadPool` 方法两个版本）判断当前代码是否运行在池的工作线程上，并区分「不在任何池」与「在别的池」两种 `None`。
2. 说出 `yield_now` 与 `yield_local` 的语义差异：前者沿完整的「本地 → 窃取他人 → 全局注入」三级链找活，后者只消化本线程名下的队列；理解返回值 `Option<Yield>` 三种取值的含义。
3. 用 `current_thread_has_pending_tasks` 实现「外层迭代器」场景下的防饿死/防死锁协议：查询积压 → 决定就地执行还是让出，并亲手构造、修复一个生产者-消费者死锁。

## 2. 前置知识

- **协作式让出（cooperative yield）**：Rayon 没有线程抢占。一个任务占着工作线程时，别的任务（哪怕是同池的）只能等它返回、或等它主动调用 `join`/`scope`/`yield` 之类的点把执行权交回调度器。`yield` 就是把这个「交回点」做成了显式 API。
- **本地 deque 与三级找活链**（复习 u5-l4）：每个工作线程有一条本地双端队列（deque）；线程自己的 `push`/`pop` 走一端，其他线程经 `Stealer` 半边从另一端窃取。找不到活时按「本地 deque → 自己的广播队列 → 窃取他人 → 全局注入队列」的顺序找（u6-l3 精确化过这条链）。
- **不帮工的阻塞等待**（复习 u7-l2）：`channel.recv()`、`Mutex::lock()` 这类纯阻塞调用不会帮池干活。若被等的任务恰好压在本线程的队列里，而本线程又是池里唯一的执行者，就是死锁。
- **`Option` 作为三态信号**：本讲所有查询 API 都返回 `Option`——`None` 表示「问题本身不成立」（当前线程不在池里），`Some(x)` 才是查询结果。这是 Rust 里区分「查询失败」与「结果为假」的惯用法。
- **`std::thread::yield_now` 与 Rayon 的让出**：标准库的让出是向**操作系统调度器**让出 CPU；Rayon 的 `yield_now`/`yield_local` 是向**池内待办任务**让出执行权，二者层级不同，且后者并不真的调用前者。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs) | 本讲主战场：`ThreadPool` 上的 `current_thread_index`/`current_thread_has_pending_tasks`/`yield_now`/`yield_local` 四个方法版本，以及同名的四个自由函数和 `Yield` 枚举 |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 真正的实现层：`Registry::current_thread` 的跨池判定、`take_local_job`/`find_work` 两条找活链、`WorkerThread::yield_now`/`yield_local` |
| [rayon-core/src/thread_pool/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs) | `yield_now_to_spawn`/`yield_local_to_spawn` 测试，以及本讲实践的思想原型 `in_place_scope_no_deadlock` |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | crate 层再导出；`use_current_thread` 文档（主线程不跑主循环，需显式让出）；wasm 单线程回退模式的说明 |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs) | rayon crate 对这些 API 的再导出情况（注意 `current_thread_has_pending_tasks` **没有**被再导出） |

## 4. 核心概念与源码讲解

### 4.1 线程身份查询：current_thread_index

#### 4.1.1 概念说明

`current_thread_index()` 回答一个问题：**「我现在是不是某个池的工作线程？如果是，是几号？」**

为什么需要它：

- **诊断与观测**：打印任务跑在哪个线程上，验证负载是否均衡（u5-l4 的实践就用过）。
- **按线程分片**：给每个工作线程一块独立缓冲（如每线程局部累加器，最后合并），线程索引就是分片下标。
- **「我在不在池上」的分支**：同一段库代码可能被池外主线程调用，也可能被池内任务调用，行为需要分叉（比如池外就不该 `yield`）。

它有两个版本，`None` 的含义不同，这是本模块最容易被忽视的细节：

| 版本 | `None` 的含义 |
|---|---|
| 自由函数 `rayon::current_thread_index()` | 当前线程**不是任何池**的工作线程 |
| 方法 `pool.current_thread_index()` | 当前线程不在**这个池**（可能在别的池，也可能不在任何池） |

另外文档明确：索引在线程生命周期内不变，但**不同池的线程可能共用同一个索引**（都从 0 开始编号）。所以「池 + 索引」才是全局唯一身份，索引本身不是。

#### 4.1.2 核心流程

```text
自由函数版本：
  取 thread-local 的 WorkerThread 指针
    ├─ 为 null → 返回 None（不在任何池）
    └─ 非 null → 返回 Some(该线程的 index)

方法版本（pool.current_thread_index()）：
  取 thread-local 的 WorkerThread 指针
    ├─ 为 null → None
    └─ 非 null → 比较其所属 Registry 与 self 的身份（RegistryId，即地址）
        ├─ 不同 → None（在别的池里）
        └─ 相同 → Some(index)
```

#### 4.1.3 源码精读

先看方法版本，重点在「比对池身份」这一步：

[rayon-core/src/thread_pool/mod.rs:L231-L235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L231-L235)——`ThreadPool::current_thread_index` 把判定委托给 `registry.current_thread()`，只有确认当前工作线程属于**本池**才返回索引。

[rayon-core/src/registry.rs:L349-L358](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L349-L358)——`Registry::current_thread`：先读 thread-local 指针拿 `None`，再比较 `worker.registry().id() == self.id()`，跨池时同样返回 `None`。

[rayon-core/src/registry.rs:L360-L367](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L360-L367)——`RegistryId`：一个「不透明」标识，实现就是 Registry 的地址。因为 Registry 只以 `Arc` 装箱存在，地址在生命周期内唯一，拿来当身份最便宜。

再看自由函数版本，它**不比对池身份**，只看「在不在某个池」：

[rayon-core/src/thread_pool/mod.rs:L437-L443](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L437-L443)——`current_thread_index()` 自由函数：直接读 `WorkerThread::current()`，非空就返回 `Some(curr.index())`。哪怕当前线程属于另一个池，也照返回那个池里的索引。

[rayon-core/src/registry.rs:L701-L704](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L701-L704)——所有身份查询的地基：`WORKER_THREAD_STATE` 这个 thread-local 指针（u5-l3 讲过它在 `main_loop` 启动时被 `set_current` 写入）。

[rayon-core/src/registry.rs:L721-L725](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L721-L725)——`WorkerThread::index`：就是建池时分配的编号 `0..num_threads()`。

最后看再导出，这里有一个实用陷阱：

[rayon-core/src/lib.rs:L89-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L89-L92)——rayon-core 把 `current_thread_index`、`current_thread_has_pending_tasks`、`Yield`/`yield_local`/`yield_now` 全部导出。

[src/lib.rs:L115-L116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L115-L116)——但 rayon crate 只再导出了 `Yield, yield_local, yield_now` 和 `current_thread_index`，**没有**再导出 `current_thread_has_pending_tasks`。想用它的自由函数形式，需直接依赖 `rayon-core` crate；或者改用 `ThreadPool` 上的方法版本（`pool.current_thread_has_pending_tasks()`）。

#### 4.1.4 代码实践

1. **实践目标**：体会在三种上下文里调用 `current_thread_index` 的返回值差异，并验证「不同池共用索引」。
2. **操作步骤**：新建独立 Cargo 项目（依赖 `rayon = "1"`），写入下面的程序（示例代码）：

   ```rust
   use rayon::{ThreadPoolBuilder, current_thread_index};

   fn where_am_i(tag: &str) {
       println!("{tag}: {:?}", current_thread_index());
   }

   fn main() {
       where_am_i("主线程");                       // ① 池外
       let a = ThreadPoolBuilder::new().num_threads(2).build().unwrap();
       let b = ThreadPoolBuilder::new().num_threads(2).build().unwrap();
       a.install(|| {
           where_am_i("池 a 任务");                 // ② 池内
           // 方法版本：跨池时为 None
           println!("a.cur(b): {:?}", b.current_thread_index()); // ③ 在 a 的线程上问 b
       });
       b.install(|| where_am_i("池 b 任务"));       // ④ 另一个池，索引会与 a 重叠
   }
   ```

3. **需要观察的现象**：① 处打印 `None`；② 处打印 `Some(0)` 或 `Some(1)`；③ 处打印 `None`（在 a 池线程上询问 b 池）；④ 处的索引与 ② 可能相同。
4. **预期结果**：`None / Some(_) / None / Some(_)` 的模式成立；多次运行 ② ④ 的具体数字可能变化（取决于哪个线程接了 `install`）。
5. 以上行为推断自源码与文档，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pool.current_thread_index()` 不能只检查「当前线程是否是工作线程」，还必须比对 RegistryId？
**答案**：因为 thread-local 里只存了一个 `WorkerThread` 指针，不知道它属于哪个池。多个 `ThreadPool` 各有独立的工作线程，编号都从 0 开始；若不比对身份，在池 a 的线程上问池 b 会返回一个毫无意义的 a 池索引。`Registry::current_thread` 通过地址型 `RegistryId` 比对，只有同池才返回 `Some`。

**练习 2**：想给每个工作线程一块局部缓冲，用 `(pool_id, index)` 还是 `index` 做键？为什么？
**答案**：若程序只有一个池（或只用全局池），`index` 足够且实现最简单（`Vec` 按 `index` 下标）；若存在多个池，索引会重叠，必须以「池身份 + 索引」为键——文档明确说明「多个处于不同池的线程可能共享同一索引」。

**练习 3**：`current_thread_index()` 返回 `None` 时，`rayon::yield_now()` 会返回什么？
**答案**：也返回 `None`。二者第一步相同——读 thread-local 的 `WorkerThread` 指针，指针为空即不在任何池，所有这类 API 都把「不在池上」编码为 `None`。

### 4.2 yield 家族：yield_now 与 yield_local

#### 4.2.1 概念说明

`yield_now()` 与 `yield_local()` 把调度器的「找一步活来干」暴露成了公开 API：**在当前任务内部，主动暂停自己，替池执行恰好一个待办任务，然后回来继续**。这正是 u7-l2 讲过的「帮工等待」的单步版本——`install` 跨池等待时在幕后做的事，这里变成了显式调用。

两者的差别只在**去哪里找活**：

| API | 找活范围 | 对应内部函数 |
|---|---|---|
| `yield_local()` | 只消化本线程名下：本地 deque 弹出 + 自己的广播队列 | `take_local_job` |
| `yield_now()` | 三级链全找：本地 → 窃取其他线程 → 全局注入队列 | `find_work` |

返回值 `Option<Yield>` 三态：

- `Some(Yield::Executed)`：找到并执行了一个任务（该任务内部还可能嵌套 `join`、再 `spawn`、再被别人窃取）。
- `Some(Yield::Idle)`：我在池上，但哪儿都没活。
- `None`：当前线程不在任何池上，让出无从谈起。

三个经典用途（都出自官方文档）：

1. **轮询循环里穿插帮工**：等外部事件时（`try_recv` 失败、条件未满足），`yield` 一步代替空转。文档还提醒：Rayon 的 `yield_now` **并不会**调用 `std::thread::yield_now()`，若返回 `Idle` 还应自己向 OS 让出。
2. **单线程回退模式（wasm 等）**：线程不可用的平台上 Rayon 退化为单线程「池」，`spawn` 出的任务没人主动领，只有 `yield_now`/`yield_local` 或低优先级的 `broadcast` 能给它们执行机会。
3. **`use_current_thread` 建的池**：当前线程被编入池但不跑窃取主循环，spawn 进去的任务必须靠显式 `yield`（或 `scope`）才会被执行。

#### 4.2.2 核心流程

```text
yield_now()：
  读 thread-local WorkerThread
    └─ null → None
  find_work():
    ① take_local_job：worker.pop()（本地 deque，LIFO 端）
       └─ 空 → 自己的广播队列 stealer.steal()
    ② steal()：随机起点环形扫描其他线程的本地 deque（ThreadInfo.stealer）
    ③ pop_injected_job()：全局注入队列
  任一步取到 job → execute(job) → Some(Yield::Executed)
  全部落空 → Some(Yield::Idle)

yield_local()：
  读 thread-local WorkerThread
    └─ null → None
  take_local_job()（只做上面的 ①）
    → Some(job) → execute → Some(Yield::Executed)
    → None      → Some(Yield::Idle)
```

与 `install` 帮工等待的同构关系（承接 u7-l2）：`install` 的文档写明跨池等待「similar to calling `ThreadPool::yield_now()` in a loop」——即「循环里反复 yield_now 直到目标完成」。而 rayon 内部真正的等待循环 `wait_until_cold` 也正是这个骨架：等 latch 期间先 `take_local_job` 干本地活，再 `find_work` 全面找。公开 yield API 就是把这个内部循环的单步抽出来给了用户。

#### 4.2.3 源码精读

公开层的两对函数极其简短——读 thread-local 指针，转发给 `WorkerThread`：

[rayon-core/src/thread_pool/mod.rs:L381-L396](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L381-L396)——`ThreadPool::yield_now`/`yield_local` 方法版本：先 `registry.current_thread()?`（含跨池判定，不在本池返回 `None`），再调 `WorkerThread` 的同名方法。

[rayon-core/src/thread_pool/mod.rs:L471-L493](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L471-L493)——自由函数版本：只查「在不在任何池」，不比对池身份；文档写明二者区别（`yield_local`「不窃取其他线程」）。

[rayon-core/src/thread_pool/mod.rs:L495-L502](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L495-L502)——`Yield` 枚举：`Executed`（找到并执行了工作）与 `Idle`（无可用工作），派生了 `PartialEq`，可直接 `== Yield::Executed` 比较。

实现层，两个 `WorkerThread` 方法就是「一次找活 + 一次执行」：

[rayon-core/src/registry.rs:L846-L864](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L846-L864)——`WorkerThread::yield_now` 走 `find_work`（三级链），`yield_local` 只走 `take_local_job`；取到就 `execute` 并返回 `Executed`，否则 `Idle`。两个函数的差异被压缩到「调用哪个找活函数」这一行。

[rayon-core/src/registry.rs:L744-L763](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L744-L763)——`take_local_job`：先 `worker.pop()` 弹本地 deque；空了再从 `self.stealer` 偷。注意这个 `stealer` 是**自己广播队列**的半边（见 [rayon-core/src/registry.rs:L651-L652](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L651-L652) 的注释「the 'stealer' half of the worker's broadcast deque」，u6-l3 讲过广播队列只有本线程能取），所以 `yield_local` 的「本地」= 本地 deque + 自身广播队列，不含偷别人。

[rayon-core/src/registry.rs:L835-L844](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L844)——`find_work`：`take_local_job` → `steal()` → `pop_injected_job()` 三级链，注释点明优先级哲学「先做完自己开始的，再接新活」。这正是 u5-l4 分析过的主循环找活链，`yield_now` 完整复用。

[rayon-core/src/registry.rs:L779-L817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L779-L817)——`wait_until_cold`：rayon **自己**等 latch 时的帮工循环，外层 `take_local_job`、内层 `find_work`。对照它就能理解 `install` 的帮工等待与 `yield` 的关系：公开 API 是内部循环的单步切片。

官方测试给出了 `yield` 的最小验证——单线程回退模式下，不 yield 则 spawn 的任务可能永远不执行：

[rayon-core/src/thread_pool/test.rs:L385-L418](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L385-L418)——`yield_now_to_spawn` / `yield_local_to_spawn`：先 `spawn` 一个发消息任务，再在工作线程上 yield 一次，断言能收到 22。注释直说动机：「单线程回退模式下，不 yield 就没机会运行这个 spawn」。

另外两处文档值得一读：

[rayon-core/src/thread_pool/mod.rs:L78-L113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L78-L113)——`install` 的「执行顺序警告」：跨池等待期间当前线程会持续找活干（等价于循环调 `yield_now`），因此单线程全局池上 `do_it` 的打印可能交错出现（`one one two two`），不要假设 `install` 是原子顺序点。

[rayon-core/src/lib.rs:L530-L546](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L530-L546)——`use_current_thread`：当前线程编入池但**不跑主窃取循环**，文档点名「除非你以某种方式向 rayon 让出，比如 `yield_now()`、`yield_local()` 或 `scope()`」，池内任务不会被这个线程自动捡起。

#### 4.2.4 代码实践

1. **实践目标**：观察 `yield_local` 的「恰好执行一个任务」与 LIFO 弹出顺序，对比 `yield_now` 在无本地活时的行为。
2. **操作步骤**：在依赖 `rayon` 的项目中运行（示例代码，`--release` 或 debug 均可）：

   ```rust
   use rayon::{ThreadPoolBuilder, Yield, yield_local, yield_now};

   fn main() {
       let pool = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
       pool.install(|| {
           for i in 0..3 {
               pool.spawn(move || println!("任务 {i} 执行，线程 {:?}", rayon::current_thread_index()));
           }
           // 逐个让出，观察每次执行了谁
           for step in 0..4 {
               let r = yield_local();
               println!("第 {step} 次 yield_local -> {:?}", r);
           }
           // 本地已空，yield_now 会去偷/找注入队列，这里大概率 Idle
           println!("随后 yield_now -> {:?}", yield_now());
       });
   }
   ```

3. **需要观察的现象**：三次 `yield_local` 依次返回 `Executed`，且任务按 `2, 1, 0`（LIFO，spawn 压栈端与 pop 同端）执行；第四次返回 `Idle`；随后的 `yield_now` 也应为 `Idle`（1 线程池无处可偷、注入队列已空）。所有任务的线程索引都是 `Some(0)`。
4. **预期结果**：输出顺序稳定为 `任务 2 → 任务 1 → 任务 0 → 三次 Executed 后 Idle`。依据是 `push`/`pop` 同端（u5-l4：本地端 LIFO）与 `take_local_job` 的实现；**待本地验证**。
5. 若把 `num_threads` 改成 2 再运行，任务可能被另一线程窃走，此时部分 `yield_local` 返回 `Idle` 而程序仍正确——体会「yield 只是尽力而为的让出，不是同步原语」。

#### 4.2.5 小练习与答案

**练习 1**：`yield_local` 为什么不会造成与其他线程的窃取竞争？
**答案**：它只调 `take_local_job`：`worker.pop()` 操作自己的 deque 的本地端，以及自己广播队列的 stealer 半边——这条广播队列的 stealer 只归本线程持有（u6-l3），别的线程摸不到。跨线程竞争只发生在 `yield_now` 的第二级 `steal()`（扫别人的 `ThreadInfo.stealer`）。

**练习 2**：写轮询循环 `while !done() { yield_now(); }`，文档建议在返回 `Idle` 时补什么？为什么？
**答案**：补 `std::thread::yield_now()`（或短暂 sleep）。因为 Rayon 的 `yield_now` 只向池内任务让出，**并不调用** OS 的让出；若池内无活而条件又不满足，纯自旋会占满一个 CPU 核，向 OS 让出能让同核的其他线程/进程有机会推进。

**练习 3**：`yield_now()` 返回 `Yield::Executed` 时，一共执行了几个任务？
**答案**：**至少一个，可能更多**。它精确地找一个 job 并执行，但该 job 的闭包内部可能嵌套 `join`、`spawn`、并行迭代器，这些都会随之展开；文档原话是「Completion of that work might include nested work or further work stealing」。所以 `Executed` 不能当作「推进了恰好一个单位工作量」的计数依据。

### 4.3 防饿死协议：current_thread_has_pending_tasks

#### 4.3.1 概念说明

`current_thread_has_pending_tasks()` 回答：**「我（当前工作线程）的本地 deque 里还有没干完的活吗？」** 返回 `Some(true)`（有积压）、`Some(false)`（本地空）、`None`（不在池上）。

它服务于一类非常常见的结构——**外层迭代器（outer iteration）**：一个串行循环在外层逐项产生工作，每项 `spawn` 成池内任务。这种结构的两个风险：

- **死锁（单线程池的极端形式）**：外层任务在唯一的工作线程上阻塞等待自己 spawn 的任务（如 `rx.recv()`），被等的任务就压在本线程 deque 里，永远没人执行。
- **饥饿/积压（多线程池的一般形式）**：外层生产快于内层消费时，本地 deque 越堆越高。粗略地，积压增长速率 \( \approx \lambda_{\text{生产}} - \lambda_{\text{消费}} \)；\( \lambda_{\text{生产}} > \lambda_{\text{消费}} \) 持续成立时积压无界增长，深 deque 还会拉大窃取线程的单次窃取距离、推迟任务完成。

文档给出的定位是「**启发式（heuristic）**的一部分：决定『再 spawn 一个新任务』还是『就在当前线程上执行』，尤其适合广度优先调度器」。也就是把查询当作反压信号：

```text
外层循环每拿到一项工作：
  本地 deque 有积压（Some(true)）→ 不再 spawn，就地执行（限制积压增长）
  本地 deque 空（Some(false)）   → spawn 出去（保持并行度）
  不在池上（None）              → 按池外逻辑处理
```

必须记住它的**固有竞态**：你查看的这一瞬间，其他线程可能正在偷走你 deque 里的任务，所以 `true` 只说明「刚才还有」，不是保证。它影响的是效率与公平性，不承担正确性。

#### 4.3.2 核心流程

完整的「防饿死等待协议」把 4.1 的身份查询、4.2 的让出与本模块的积压查询串成一台状态机：

```text
目标：外层任务等待某个条件（如 channel 收到值 / 计数归零），期间不得饿死本地任务

循环：
  ① 条件满足了吗？ → 是：退出
  ② pool.current_thread_has_pending_tasks()
       Some(true)  → yield_local()   # 本地有活：让出去替池干活（帮工）
       Some(false) → 向 OS 让出        # 本地没活：短暂 sleep / std yield
       None        → 向 OS 让出        # 不在池上：只能等
  ③ 回到 ①
```

对照 rayon 内部的 `wait_until_cold`（4.2.3 最后一条引用）：① 对应 `latch.probe()`，② 的 `Some(true)` 分支对应 `take_local_job → execute`——协议骨架与调度器自己的等待循环同构，只是把「找活」换成了用户可见的 `yield`，把「检查完成」换成了业务条件。

#### 4.3.3 源码精读

[rayon-core/src/thread_pool/mod.rs:L258-L262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L258-L262)——`ThreadPool::current_thread_has_pending_tasks`：同池身份判定后，返回 `Some(!curr.local_deque_is_empty())`，注意取反——「非空」即「有积压」。

[rayon-core/src/thread_pool/mod.rs:L237-L257](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L237-L257)——文档全文值得精读：背景段重述了工作窃取模型（新任务进本地 deque，线程优先干自己的，空了去偷别人的），并明确「这是一个固有竞态的检查，其他线程可能正在从你的 deque 偷任务」；首段给出定位——spawn 还是就地执行的启发式，尤其适合广度优先调度器。

[rayon-core/src/registry.rs:L740-L742](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L740-L742)——`local_deque_is_empty`：一行转发给 crossbeam `Worker::is_empty`。所以本查询**只看本地 deque**，不含广播队列与注入队列——「pending tasks」的口径比 `yield_local` 能消化的范围略窄（后者还能吃广播队列），做协议推理时要注意这个口径差。

[rayon-core/src/thread_pool/mod.rs:L451-L457](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L451-L457)——自由函数版本 `current_thread_has_pending_tasks`：与 4.1 同款结构，只判「在不在任何池」。再次提醒：它**没有**被 rayon crate 再导出（见 [src/lib.rs:L115-L116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L115-L116)），要么直接依赖 `rayon-core`，要么用方法版本。

本讲实践的思想原型——上游用一个专门测试证明「普通 scope + 阻塞等待」会死锁：

[rayon-core/src/thread_pool/test.rs:L352-L366](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L352-L366)——`in_place_scope_no_deadlock`：单线程池上 spawn 一个发 channel 消息的任务，然后 `rx.recv()`。注释点破要害：「**用普通 scope，这个闭包永远不会运行，因为 scope 的 op 本身就占着唯一的工作线程**」。上游的修复是 `in_place_scope`（op 留在调用线程执行，u6-l1）；我们下面的实践换用本讲主题的工具——积压查询 + `yield_local`——达到同样效果。

#### 4.3.4 代码实践

1. **实践目标**：构造「外层任务阻塞等待自己 spawn 的任务」的死锁反例，再用 `current_thread_has_pending_tasks` + `yield_local` 修复，打印修复前后的行为差异。
2. **操作步骤**：新建项目，在 `Cargo.toml` 加 `rayon = "1"`（示例代码）：

   ```rust
   use rayon::ThreadPoolBuilder;
   use std::sync::mpsc::{TryRecvError, channel};
   use std::thread;
   use std::time::Duration;

   fn main() {
       let mode = std::env::args().nth(1).unwrap_or_default();
       let pool = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
       let (tx, rx) = channel();

       pool.install(move || {
           // 消费者任务：压入本池（此刻就在唯一工作线程的本地 deque 里）
           pool.spawn(move || {
               println!("  [消费者] 开始执行，线程 {:?}", rayon::current_thread_index());
               tx.send(42).unwrap();
           });

           if mode == "fixed" {
               // 修复版：绝不纯阻塞——本地有活就 yield_local 帮工，没活才睡
               loop {
                   match rx.try_recv() {
                       Ok(v) => { println!("[修复] 收到 {v}"); return; }
                       Err(TryRecvError::Disconnected) => return,
                       Err(TryRecvError::Empty) => match pool.current_thread_has_pending_tasks() {
                           Some(true) => { let _ = pool.yield_local(); } // 帮工一步
                           _ => thread::sleep(Duration::from_millis(1)),
                       },
                   }
               }
           } else {
               // 反例：纯阻塞等待，唯一的工作线程挂死在这里
               println!("[反例] 即将 recv 阻塞……");
               println!("[反例] 收到 {:?}", rx.recv().unwrap());
           }
       });
       println!("install 返回，程序正常结束");
   }
   ```

   先 `cargo run`（反例），再 `cargo run -- fixed`（修复版）。反例建议加超时运行：`timeout 5 cargo run`。
3. **需要观察的现象**：反例打印 `[反例] 即将 recv 阻塞……` 后**无限挂起**（`timeout` 以 124 退出），消费者的「开始执行」永远不出现——它就压在被阻塞线程自己的 deque 里；修复版先打印消费者开始执行（由 `yield_local` 在同一线程就地执行），随后 `[修复] 收到 42`、`install 返回`。
4. **预期结果**：反例死锁、修复版正常结束。死锁成因与上游 `in_place_scope_no_deadlock` 测试注释描述的机制一致（scope op / install 的闭包占住唯一工作线程），修复逻辑直接对应 4.3.2 的协议；**待本地验证**（不同机器上线程调度不影响结论：单线程池中不存在其他执行者）。
5. 把 `num_threads` 改为 2 再跑反例：多数情况下**不再死锁**（另一线程会偷走消费者任务），但输出顺序不定——体会「死锁是饥饿的极端形式，协议在多线程下依然有价值（限制积压）」。

#### 4.3.5 小练习与答案

**练习 1**：为什么修复循环里 `Some(true)` 分支调 `yield_local` 而不是 `yield_now`？
**答案**：查询已经告诉我们本地 deque 非空，最便宜的下一步就是消化自己名下的活（`take_local_job`，无跨线程竞争）；`yield_now` 的额外两级（窃取他人、全局注入队列）此刻是多余开销。反之在 `Some(false)` 时若仍想让出，`yield_now` 才有意义（去别处找活帮全池）。协议按查询结果选择最小足够动作。

**练习 2**：`current_thread_has_pending_tasks()` 返回 `Some(false)`，能断言「此刻池里没有待办任务」吗？
**答案**：不能，两层都不行。其一，口径：它只看**本线程**的本地 deque，不含广播队列、其他线程的 deque 和全局注入队列；其二，竞态：即使对本线程，返回瞬间结果也可能过期（其他线程刚偷走最后一个任务，或本判断之前刚有新任务压入）。它是启发式信号，不是一致性快照。

**练习 3**：除本实践的「帮工等待」外，文档还给这个查询标注了什么用途？举一个伪代码例子。
**答案**：作为「spawn 还是就地执行」的启发式，尤其用于广度优先调度。例如外层迭代器：

```text
for item in 外层源:
    if 本地有积压: 就地处理(item)   # 反压：不再加深队列
    else:         spawn(处理 item)  # 空闲时保持并行度
```

## 5. 综合实践

把本讲三个模块串成一个完整的外层迭代器流水线（示例代码，在依赖 `rayon` 的独立项目中完成）：

**任务**：处理 8 个「工作项」，每项耗时略有不同。实现两个版本并对比：

```rust
use rayon::ThreadPoolBuilder;
use std::time::Duration;

fn work(item: usize) -> usize {
    std::thread::sleep(Duration::from_millis(10));
    item * 2
}

fn main() {
    let pool = ThreadPoolBuilder::new().num_threads(2).build().unwrap();
    let items: Vec<usize> = (0..8).collect();

    for version in ["naive", "heuristic"] {
        let results = std::sync::Mutex::new(Vec::new());
        pool.install(|| {
            for &item in &items {
                let results = &results;
                let run = move || { results.lock().unwrap().push(work(item)); };
                // 启发式：本地有积压 -> 就地执行（反压）；空闲 -> spawn
                if version == "heuristic"
                    && pool.current_thread_has_pending_tasks() == Some(true)
                {
                    run();
                } else {
                    pool.spawn(run);
                }
            }
            // 外层产完：帮工排空本地队列，避免池外空等
            while pool.current_thread_has_pending_tasks() == Some(true) {
                if pool.yield_local() == Some(rayon::Yield::Executed) {
                    continue;
                }
            }
        });
        let mut r = results.into_inner().unwrap();
        r.sort_unstable();
        println!("{version}: {:?} (共 {} 项)", r, r.len());
    }
}
```

要求与检查点：

1. 两版本结果集一致（都应是 `[0, 2, 4, …, 14]`，8 项）——**正确性与 spawn/就地执行的选择无关**，这是启发式只影响性能不影响语义的具体体现。
2. 在 `heuristic` 版里加一行计数打印：就地执行了几次、spawn 了几次。2 线程池下应能观察到至少一次就地执行（第一项 spawn 后本地非空，第二项触发反压）。
3. 把池改为 1 线程，删掉末尾的帮工排空循环再运行——若你的外层改为「等待某个由任务发出的完成信号」，就会复现 4.3.4 的死锁；把循环加回来即修复。
4. 记录三种配置（naive/2 线程、heuristic/2 线程、heuristic/1 线程）的耗时与就地执行比例，写成简短结论。运行数据**待本地验证**。

## 6. 本讲小结

- `current_thread_index` 有两个版本：自由函数判「在不在任何池」，`ThreadPool` 方法额外比对 `RegistryId`（地址型身份）判「在不在**这个**池」；索引不是全局唯一，跨池会重叠。
- `yield_now` 走完整三级找活链（本地 deque + 自身广播队列 → 窃取他人 → 全局注入队列），`yield_local` 只走 `take_local_job`（本地名下）；二者都执行**恰好一个**任务（其内部可再嵌套），返回 `Executed`/`Idle`，不在池上返回 `None`。
- Rayon 的 yield 让出的是**池内执行权**而非 OS 时间片；`Idle` 时应自己向 OS 让出。它是 `install`/`wait_until_cold` 帮工等待循环的单步显式化。
- `current_thread_has_pending_tasks` 只看本线程本地 deque，是固有竞态的启发式查询（rayon crate 未再导出其自由函数版本，需用 `rayon_core::` 或 `ThreadPool` 方法）。
- 防饿死协议 = 「条件检查 + 积压查询 + 按查询结果选最小让出动作」的循环，与调度器内部 `wait_until_cold` 的骨架同构；单线程池上「纯阻塞等待自己 spawn 的任务」必死锁，多线程下同一结构退化为积压与饥饿。
- 这组 API 全部是**尽力而为**的提示与机会：影响效率、公平与活性，不承担正确性——结果正确性始终由所有权与 Send/Sync 约束保证。

## 7. 下一步学习建议

本讲结束了单元七（自定义线程池）。接下来两条路线任选：

- **进入单元八（切片并行与并行排序）**：u8-l1 切片生产器家族。你在这里学到的「本地 deque、窃取、粒度」概念将落到最理想的数据源上——连续内存、O(1) `split_at` 的切片，看 Rayon 如何把切分成本压到极致。
- **回补内核视角**：若想再巩固 yield 与等待的关系，重读 [rayon-core/src/registry.rs:L779-L817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L779-L817)（`wait_until_cold`）与 u5-l5 的睡眠唤醒协议，把「让出 → 找活 → 睡眠 → 被唤醒」整条活性链在脑子里连成一体；之后带着这套直觉去读 u9-l3 的性能调优，理解任务粒度如何决定窃取与唤醒的频率。
