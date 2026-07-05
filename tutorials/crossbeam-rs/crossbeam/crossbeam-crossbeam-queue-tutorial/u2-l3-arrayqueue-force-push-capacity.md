# force_push 环形缓冲与容量查询：capacity / len / is_empty / is_full

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `force_push` 与普通 `push` 的唯一差别：在「队列已满」时，`push` 选择失败退还元素，`force_push` 选择**同时推进 head 与 tail**，覆盖掉最旧元素，从而把 `ArrayQueue` 当成**环形缓冲（ring buffer）**用。
- 解释 `force_push` 内部那个闭包为什么用「head 的 CAS 成功」来确认队列确实满了，以及为什么覆盖完值之后要把 tail 直接 `store` 掉。
- 读懂 `capacity` / `is_empty` / `is_full` 三个查询的实现，并理解它们返回的都是「某个瞬间的快照」。
- 讲清 `len()` 为什么不能只读一次 tail 和 head，而要用「读 tail → 读 head → 再读 tail 确认未变」的**一致性快照循环**，并能把环形缓冲上的元素个数算对。
- 手算一个 `cap=2` 的队列在连续 `force_push` 下 head/tail/stamp 的演化。

本讲只覆盖 `src/array_queue.rs` 里 `force_push`、`capacity`、`is_empty`、`is_full`、`len` 这几个函数，以及它们共同体现的环形缓冲语义。`push`/`pop` 的 CAS 主循环已在 [u2-l2](./u2-l2-arrayqueue-push-pop.md) 讲过，本讲直接承接，不重复。

## 2. 前置知识

### 2.1 环形缓冲（ring buffer）是什么

环形缓冲是一块**固定大小**的数组，配两个指针：一个写指针（tail），一个读指针（head）。写到末尾就绕回开头，逻辑上把数组看成一个「环」。

普通有界队列：写指针追上读指针（缓冲满）时，写入就失败。

环形缓冲：写指针追上读指针时，**直接覆盖最旧的那个元素**，并让读指针也跟着前进一步。代价是「丢掉最旧的数据」，收益是写入永不失败、延迟稳定。

典型场景：音频/视频帧缓冲、滚动日志、传感器采样、限速队列——这些场景里「最新的数据最重要，旧的可以丢」。

### 2.2 stamp 编码速回顾（来自 u2-l1 / u2-l2）

`ArrayQueue` 把 head/tail 压成单个 `usize`：

- 低位 = `index`（数组下标），用 `stamp & (one_lap - 1)` 取出；
- 高位 = `lap`（圈数），用 `stamp & !(one_lap - 1)` 取出。

`one_lap = (cap + 1).next_power_of_two()`，既是 2 的幂、又严格大于 cap，所以「加一圈」就是「加 one_lap」。判定满/空的关键就是 head 与 tail 的 **lap 差**：

- `tail == head`：空。
- `head + one_lap == tail`（即 head 比 tail 落后一整圈）：满。

`push_or_else` 的 CAS 主循环里，每个 `Slot` 还有一个充当状态机的 `stamp` 字段：

- `slot.stamp == tail`：该槽可写。
- `slot.stamp == head + 1`：该槽可读。
- `slot.stamp + one_lap == tail + 1`：**疑似满**（关键提示，本讲会反复用到）。

> 这三条规则记不住没关系，下面遇到时回头看即可。

## 3. 本讲源码地图

本讲只读一个源文件，外加它的测试文件：

| 文件 | 作用 |
| --- | --- |
| [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | `ArrayQueue` 全部实现。本讲聚焦 `force_push`、`capacity`、`is_empty`、`is_full`、`len`，以及 `Drop` 中对 len 算术的复用。 |
| [tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) | 测试套件。本讲重点参考 `spsc_ring_buffer`、`mpmc_ring_buffer`、`len_empty_full`、`len` 这几个测试。 |

## 4. 核心概念与源码讲解

---

### 4.1 force_push：在满队列时覆盖最旧元素

#### 4.1.1 概念说明

`push` 在队列满时返回 `Err(value)`，把元素退还给调用者——这是「有界队列」语义，提供背压。

`force_push` 不同：队列满时它**不退还**，而是挤掉队列里**最旧**的那个元素，把新值写进它占的槽，然后让 head 和 tail 各前进一步。返回值是被挤掉的那个旧元素（`Some(old)`）；如果队列没满，就正常写入，返回 `None`。

一句话对比：

| 操作 | 队列未满 | 队列已满 |
| --- | --- | --- |
| `push(v)` | 写入，返回 `Ok(())` | 失败，返回 `Err(v)` |
| `force_push(v)` | 写入，返回 `None` | 覆盖最旧元素 `old`，返回 `Some(old)` |

为什么叫「环形缓冲」？因为满之后继续写不会停，而是绕着 buffer 一圈一圈地覆盖，head 永远跟着 tail 跑，buffer 始终是满的。

#### 4.1.2 核心流程

`force_push` 复用了 u2-l2 讲过的 `push_or_else` 主循环，只是把「疑似满」分支里的行为换掉。流程：

1. 进入 `push_or_else` 的 CAS 循环，加载 `tail`，拆出 `index`/`lap`，算出 `new_tail`。
2. 读 `slot.stamp`：
   - 若 `tail == stamp`：槽可写 → 走**正常写入路径**（CAS 推进 tail → 写值 → Release 发 stamp），返回 `Ok(())`。这条路径与普通 `push` 完全一样。
   - 若 `slot.stamp + one_lap == tail + 1`：**疑似满** → 调用 `force_push` 传入的闭包，闭包负责「覆盖最旧元素」。
3. 闭包里做的事（覆盖路径）：
   - 由「满 = head 落后 tail 一圈」推出期望的旧 head：`head = tail - one_lap`，以及新 head：`new_head = new_tail - one_lap`。
   - 用 `head.compare_exchange_weak(head, new_head)` **抢 head 的推进权**。CAS 成功才说明队列确实满了、且由我负责覆盖。
   - 成功后：直接 `tail.store(new_tail)`（注意是 store 不是 CAS，原因见 4.1.3）、`replace` 掉槽里的旧值拿到 `old`、把 `slot.stamp` 置为 `tail + 1`，最后返回 `Err(old)`。
   - CAS 失败：说明 head 已经被别人（消费者 pop 或别的 force_push）挪动了，本次不是真的满，返回 `Ok(v)` 让外层循环重试。
4. `force_push` 在 `push_or_else` 的返回值上调用 `.err()`：`Ok(())`（正常写入）→ `None`；`Err(old)`（覆盖成功）→ `Some(old)`。

#### 4.1.3 源码精读

先看 `force_push` 全貌，它非常短：

[src/array_queue.rs:L275-L301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L275-L301) —— `force_push` 把覆盖逻辑塞进一个闭包交给 `push_or_else`，最后用 `.err()` 把「被覆盖的旧值」翻成 `Option`。

闭包里的关键几行，逐行解读：

```rust
let head = tail.wrapping_sub(self.one_lap);
let new_head = new_tail.wrapping_sub(self.one_lap);
```

[src/array_queue.rs:L277-L278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L277-L278) —— 这里**没有去 load 真正的 head**，而是直接从 `tail` 算出「如果队列真的满了，head 应该是多少」。因为满的充要条件就是 `head + one_lap == tail`，反过来 `head == tail - one_lap`。`wrapping_sub` 保证圈数绕回时也正确。

```rust
if self.head
    .compare_exchange_weak(head, new_head, Ordering::SeqCst, Ordering::Relaxed)
    .is_ok()
{
    self.tail.store(new_tail, Ordering::SeqCst);
    let old = unsafe { slot.value.get().replace(MaybeUninit::new(v)).assume_init() };
    slot.stamp.store(tail + 1, Ordering::Release);
    Err(old)
} else {
    Ok(v)
}
```

[src/array_queue.rs:L281-L298](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L281-L298) —— 几个要点：

1. **CAS head 是「权威确认」**。外层「疑似满」只是个廉价提示（读了一次 `slot.stamp`），可能过期。真正确认队列满的方式是：能不能把 head 从 `tail - one_lap` 推进到 `new_tail - one_lap`。CAS 成功 ⟺ 此刻 head 确实落后 tail 一圈 ⟺ 队列确实满。

2. **为什么 tail 用 `store` 而不是 CAS？** 因为赢得 head 的 CAS 后，本线程在该 index 上取得了**独占的推进权**。在「满」状态下，普通 `push` 必然失败（它也会判定满并退还），所以不会有别的生产者能推进 tail；而另一个并发的 `force_push` 想推进同一个 tail，必须先赢得同一个 head 的 CAS——它已经被我们赢了，所以它会失败并重试。因此 tail 的推进是无竞争的，直接 `store` 即可，无需 CAS。

3. **`replace` 而不是 `write`**。普通 `push` 写的是「未初始化」的槽，用 `write`；`force_push` 覆盖的是「已初始化」的槽（装着最旧元素），所以用 `replace`：写入新值同时把旧值取出来 `assume_init` 成 `T`。这就是被挤掉的 `old`。

4. **stamp 仍然是 `tail + 1`**。写完后把 `slot.stamp` 设为 `tail + 1`，标记「这个槽现在是最新写入、可被读取的」，与普通 push 完全一致。

5. **`Err(old)` 的方向是反的**。注意 `push_or_else` 的约定：闭包返回 `Ok(v)` 表示「我没处理，外层请重试」，返回 `Err(x)` 表示「我已处理，请把 `x` 作为最终结果返回」。所以**覆盖成功时返回 `Err(old)`**，让 `push_or_else` 把 `old` 一路返回出去，最后被 `.err()` 接成 `Some(old)`。这是 `Result` 在这里的一个稍反直觉的用法：对 `force_push` 而言，`Err` 反而是「成功覆盖」的信号。

对比一下普通 `push` 的闭包，就能看出两者只在「疑似满」分支里分道扬镳：

[src/array_queue.rs:L203-L215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L203-L215) —— `push` 在疑似满时 load 真正的 head 复核，若 `head + one_lap == tail` 则真满，返回 `Err(v)` 退还元素；否则 `Ok(v)` 重试。`force_push` 不复核、直接抢 head CAS：抢到就覆盖，抢不到就 `Ok(v)` 重试。

而调用闭包的那个「疑似满」分支在 `push_or_else` 里：

[src/array_queue.rs:L176-L180](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L176-L180) —— 当 `slot.stamp + one_lap == tail + 1` 时，先来一道 `fence(SeqCst)` 做二次确认（详见 u4-l1），再调用闭包 `f`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `force_push` 在 `cap=2` 上的返回值序列与内部指针演化，把 4.1.2 的流程落到具体数字上。

**操作步骤**：

1. 在仓库根目录新建一个临时 binary（或在 `tests/` 下加一个 `#[test]`），写入：

   ```rust
   // 示例代码：非项目原有，用于观察 force_push 行为
   use crossbeam_queue::ArrayQueue;

   let q = ArrayQueue::new(2);
   assert_eq!(q.force_push(10), None);   // 槽 0：空，正常写入
   assert_eq!(q.force_push(20), None);   // 槽 1：空，正常写入
   assert_eq!(q.force_push(30), Some(10)); // 队列满，覆盖最旧的 10
   assert_eq!(q.pop(), Some(20));        // head 现在指向 20
   assert_eq!(q.pop(), Some(30));        // 接着是 30
   assert_eq!(q.pop(), None);            // 空
   ```

2. 运行（在 `crossbeam-queue` 目录下）：

   ```bash
   cargo test -p crossbeam-queue --test array_queue -- 你的测试名 --nocapture
   ```

   或把它放进 `examples/` 用 `cargo run --example <name>` 跑。

**需要观察的现象 / 预期结果**：

- 三次 `force_push` 依次返回 `None`、`None`、`Some(10)`，与源码 docstring 一致。
- `pop()` 返回 `Some(20)` 而不是 `Some(10)`——证明 `10` 确实被 `30` 覆盖掉了（被当作 `old` 返回过），没有残留在队列里。

**手算时序**（`cap=2`，故 `one_lap = (2+1).next_power_of_two() = 4`，初始 `slot[0].stamp=0, slot[1].stamp=1`）：

| 步骤 | head | tail | slot[0] | slot[1] | 走的路径 | 返回 |
| --- | --- | --- | --- | --- | --- | --- |
| new(2) | 0 | 0 | stamp=0 | stamp=1 | — | — |
| force_push(10) | 0 | 1 | stamp=1,**值=10** | stamp=1 | tail==stamp，正常写入 | None |
| force_push(20) | 0 | 4 | stamp=1,值=10 | stamp=2,**值=20** | tail(1)==stamp(1)，正常写入，tail 绕到 {lap:1,idx:0}=4 | None |
| force_push(30) | 1 | 5 | stamp=5,**值=30** | stamp=2,值=20 | 疑似满：stamp(1)+4==tail(4)+1，CAS head 0→1 成功，覆盖 slot[0] 的 10 | Some(10) |
| pop() | 5 | 5 | stamp=5,值=30 | stamp=5 | head+1==stamp，读 slot[1]=20 | Some(20) |

> 重点看第三行 `force_push(30)`：`tail=4` 的 index 是 `4 & 3 = 0`，所以它瞄准的槽正是装着最旧元素 `10` 的 `slot[0]`；覆盖后 head 从 0 推进到 1（指向下一个槽 slot[1]，里面是 `20`），tail 从 4 推进到 5。队列始终是「满」的，但内容从 `[10,20]` 变成了 `[20,30]`——这就是环形缓冲。

#### 4.1.5 小练习与答案

**练习 1**：把上面时序表里的 `force_push(30)` 换成普通 `push(30)`，会发生什么？返回值是什么？head/tail 又会怎样？

**答案**：`push` 走的是同一个 `push_or_else`，疑似满分支会调用 `push` 的闭包：load 真正的 head=0，检查 `head + one_lap == tail` 即 `0 + 4 == 4` 成立，于是判定真满，返回 `Err(30)`。head/tail **都不变**（仍为 head=0, tail=4），`30` 被原样退还。

**练习 2**：`force_push` 闭包里为什么不直接 `load(head)` 来确认满，而要先 `wrapping_sub` 算出一个期望值再去 CAS？

**答案**：因为「读-判-写」三步在并发下不是原子的：load 到 head 后，head 可能立刻被别的线程改掉，这时基于旧 head 的判定就过期了。CAS 把「期望值」和「写入」绑成一个原子操作：只有当 head 此刻**确实**等于 `tail - one_lap` 时才会推进成功，从而把「确认满」和「取得覆盖权」合并成一步，消除 check-then-act 竞态。

---

### 4.2 capacity / is_empty / is_full：固定容量与瞬时状态

#### 4.2.1 概念说明

这三个是最常被调用的查询接口，且都不修改队列：

- `capacity()`：队列最多能装多少个元素。`ArrayQueue` 在 `new(cap)` 时一次性分配好整个 buffer，**运行期容量永远不变**。
- `is_empty()`：此刻是否没有元素。
- `is_full()`：此刻是否已装满。

关键认知：在并发队列里，`is_empty` / `is_full` 返回的永远是**某个瞬间的快照**。函数返回 `true` 的下一行，别的线程可能已经 push/pop，状态就变了。所以这些接口适合做「启发式检查」（比如决定要不要 sleep、要不要扩容），**不适合**用来做同步保证（不要写 `while q.is_empty() {}` 之类的忙等，更不要假设检查后状态不变）。

#### 4.2.2 核心流程

- `capacity()`：直接返回 `self.buffer.len()`，零原子操作。
- `is_empty()`：用 `SeqCst` 先 load head、再 load tail，判断 `tail == head`。
- `is_full()`：用 `SeqCst` 先 load tail、再 load head，判断 `head + one_lap == tail`。

注意两个查询 load 的**顺序相反**：`is_empty` 先 head 后 tail，`is_full` 先 tail 后 head。这个顺序不是随便写的，它关系到「返回 false 是否安全」，见 4.2.3。

#### 4.2.3 源码精读

**capacity**：

[src/array_queue.rs:L440-L443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L440-L443) —— 返回 `buffer.len()`。buffer 在 `new` 时由 `(0..cap).map(...).collect()` 分配，长度恒等于构造时传入的 `cap`，标了 `#[inline]`，是个零成本查询。

**is_empty**：

[src/array_queue.rs:L458-L468](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L458-L468) —— 先 `SeqCst` 读 head，再 `SeqCst` 读 tail，返回 `tail == head`。

源码注释点出了这里的微妙之处：如果在两次 load 之间 head 被改了（说明发生了 pop，而 pop 意味着队列里曾有过元素），那么「存在一个队列非空的瞬间」，此时返回 `false`（非空）在**线性化（linearizable）**意义下是合法的。

为什么顺序是「先 head 后 tail」？我们想安全地报「空」。`tail == head` 用的是较早的 head 和较晚的 tail。若这期间有 producer push（tail 变大），那么读到的 tail 会大于读到的旧 head，`tail != head` → 返回 `false`——而队列此刻确实非空，正确。换句话说，这种读序让「返回 true（空）」倾向于保守、可信。

**is_full**：

[src/array_queue.rs:L483-L492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L483-L492) —— 与 `is_empty` 完全对称：先 `SeqCst` 读 tail、再 `SeqCst` 读 head，返回 `head.wrapping_add(self.one_lap) == tail`。读序同理：先读「会让自己看起来更满」的 tail，再读 head，若期间有 consumer pop（head 变大），`head + one_lap != tail` → 返回 `false`，而队列此刻确实不满，正确。

> 关于 `SeqCst`：这里用强序是为了让多次 load 之间能形成一个全局一致的时间点，使「返回值在某个瞬间成立」这个保证更扎实。详细的内存序论证留到 u4-l1。

#### 4.2.4 代码实践

**实践目标**：复现并扩展 `tests/array_queue.rs` 里的 `len_empty_full` 测试，亲手观察三个状态在 push/pop 之间的切换。

**操作步骤**：在 `tests/array_queue.rs` 里已经有现成的 [`len_empty_full`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L32-L57) 测试，直接运行：

```bash
cargo test -p crossbeam-queue --test array_queue len_empty_full --nocapture
```

然后自己写一个变体，用一个 `cap=3` 的队列，交替 push/pop，每次操作后打印 `capacity()`、`is_empty()`、`is_full()`、`len()`：

```rust
// 示例代码
let q = ArrayQueue::new(3);
assert_eq!(q.capacity(), 3);
assert!(q.is_empty() && !q.is_full());
q.push(1).unwrap();
q.push(2).unwrap();
q.push(3).unwrap();
assert!(q.is_full() && !q.is_empty());   // 满
q.pop();
assert!(!q.is_full() && !q.is_empty());  // 又「不满」了
```

**需要观察的现象 / 预期结果**：

- `capacity()` 自始至终是 3，不随 push/pop 改变。
- push 到第 3 个时 `is_full()` 才变 `true`；pop 掉一个后立刻变回 `false`。
- `is_empty()` 与 `is_full()` 不会同时为 `true`（除非 `cap==0`，而 `new(0)` 会 panic）。

**待本地验证**：单线程下上述断言必然成立；你可以在多线程下加打印，观察 `is_full()` 在高并发 push 时是否会「抖动」（一会 true 一会 false），以此体会「快照」语义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_empty` 和 `is_full` 都用 `SeqCst` 而不是 `Relaxed`？

**答案**：这两个函数要读两个独立的原子变量，然后基于它们的**关系**（相等 / 差一圈）下结论。`Relaxed` 不保证两次 load 之间的全局顺序，可能读到非常不一致的 (head, tail) 组合，得出违反线性化的结论。`SeqCst` 提供全局总序，使「返回值对应某个真实瞬间」这一保证成立。

**练习 2**：`is_empty` 里把 load 顺序换成「先 tail 后 head」会怎样？给出一个可能产生错误结论的场景。

**答案**：先读 tail、后读 head 时，若两次读之间有 producer push（tail 在你读它之后才变大，但你读到的 head 是更旧的、较小的值），可能读到 (旧 tail, 更旧 head) 凑出 `tail == head` 而误报「空」，可实际上队列里已经有元素了。当前「先 head 后 tail」的读序把这类假「空」规避掉了——详见 4.2.3 的分析（注意：完整的形式化证明涉及 SeqCst 与 linearizability，留待 u4-l1）。

---

### 4.3 len：一致性快照循环与跨圈计数

#### 4.3.1 概念说明

`len()` 要回答「队列里现在有多少个元素」。直觉上似乎是 `tail - head`，但在并发 + 环形缓冲下有两个坑：

1. **两个原子读不是原子的**。读 tail 的瞬间和读 head 的瞬间之间，别的线程可能 push/pop，于是你拿到的 (tail, head) 是「从未同时存在过」的组合，算出来的 len 可能错误。
2. **环形下标要绕回**。`index` 是 `stamp & (one_lap - 1)`，当 head 的 index 大于 tail 的 index 时，元素是「跨过数组末尾绕回来」的，不能直接相减。

第一个坑用「一致性快照循环」解决；第二个坑用分类讨论的算术解决。

#### 4.3.2 核心流程

`len()` 的结构是一个 `loop`：

1. `SeqCst` 读 `tail`（记 `T1`）。
2. `SeqCst` 读 `head`（记 `H`）。
3. `SeqCst` **再读一次 `tail`**，若不等于 `T1`，说明期间 tail 动了 → 回到步骤 1 重试。
4. 若等于 `T1`，则 (T1, H) 是一致的，按下式计算并返回：

设 `hix = H 的 index`，`tix = T1 的 index`：

- 若 `hix < tix`：元素连续，\( \text{len} = \text{tix} - \text{hix} \)。
- 若 `hix > tix`：元素绕过末尾，\( \text{len} = \text{capacity} - \text{hix} + \text{tix} \)。
- 若 `hix == tix`：head 和 tail 落在同一槽，要么空要么满——再看整体 stamp：`tail == head` 则空（len=0），否则满（len=capacity）。

**为什么「再读一次 tail 确认未变」就够了？** 因为 head 是在两次 tail 读**之间**读的。如果第二次读 tail 仍是 `T1`，而 tail 又是单调推进（push 只增不减）的，那就可以推出：在整个区间里 tail 始终是 `T1`——也就是说，读 head 的那一瞬间 tail 也是 `T1`。于是 (T1, H) 是**真实存在过的**组合，len 对某个瞬间成立。这正是「重读首个变量」的一致性套路（类似 seqlock，只不过这里的「版本号」就是 tail 本身）。

> 为什么不复读 head？因为 head 是第二个读的，它读完后 tail 才被复查；head 自己后续变不变不影响「(T1,H) 曾同时成立」。重读**先读的那个**才有意义。

#### 4.3.3 源码精读

[src/array_queue.rs:L510-L532](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L510-L532) —— `len` 全文：

```rust
pub fn len(&self) -> usize {
    loop {
        let tail = self.tail.load(Ordering::SeqCst);
        let head = self.head.load(Ordering::SeqCst);
        if self.tail.load(Ordering::SeqCst) == tail {   // 一致性确认
            let hix = head & (self.one_lap - 1);
            let tix = tail & (self.one_lap - 1);
            return if hix < tix {
                tix - hix
            } else if hix > tix {
                self.capacity() - hix + tix
            } else if tail == head {
                0
            } else {
                self.capacity()
            };
        }
    }
}
```

逐段看：

- 三次 `SeqCst` load 构成「tail → head → 复查 tail」的三明治。若复查失败直接 `loop` 重来，不返回中间值。
- `hix` / `tix` 是 head/tail 的 index 部分（低位）。
- 四分支把环形计数算对，对应 4.3.2 的三个区间加 `hix==tix` 时的空/满二分。

注意 `hix == tix` 那一支：index 相同时，单看 index 区分不出「空」和「满」（这是所有定长环形缓冲的通病），所以必须比完整 stamp：`tail == head` 是空，差一圈则是满。这跟 `is_empty`/`is_full` 的判定完全同源。

**测试里如何被压测**：[`tests/array_queue.rs` 的 `len` 测试](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L89-L145) 在单线程下逐步 push/pop 校验 `len()` 精确递增递减，再起一对生产者/消费者线程，每轮操作后断言 `len <= CAP`——这正是验证「一致性快照在并发下不会算出超过 capacity 的荒谬值」。

#### 4.3.4 代码实践

**实践目标**：体会「一致性快照」为什么必要——构造一个会让「朴素两次读」算错 len 的高并发场景，并确认真正的 `len()` 永远落在 `[0, capacity]`。

**操作步骤**：

1. 直接跑现成测试：

   ```bash
   cargo test -p crossbeam-queue --test array_queue len --nocapture
   ```

2.（可选，源码阅读型）对照源码想清楚：如果不做「复查 tail」这一步，单线程下 `len` 会不会错？（不会，因为没有并发修改。）那为什么还要加？**因为多线程下必须加**——这一步纯粹是为并发正确性买单。

**需要观察的现象 / 预期结果**：

- 测试通过，包括多线程段里 `assert!(len <= CAP)` 全部成立。
- `cfg!(miri)` 时规模缩小到 `COUNT=30, CAP=40`，普通模式则是 `COUNT=25000, CAP=1000`，说明作者有意在大规模下压测 `len` 的并发正确性。

**待本地验证**：尝试 fork 一份 `len` 的「朴素版」（去掉复查 tail 那一行）放进你自己的测试，用 miri 或多线程跑，看能否复现 `len > CAP` 或 `len` 跳变的异常（大概率要跑很多轮才偶发，这正是并发 bug 的特点）。

#### 4.3.5 小练习与答案

**练习 1**：`cap=4`，某瞬间 `head` 的 index 是 3、`tail` 的 index 是 1，且二者整体 stamp 不等也不差一圈。这时刻队列里有几个元素？

**答案**：`hix=3 > tix=1`，元素绕过末尾：\( \text{len} = 4 - 3 + 1 = 2 \)。具体是 slot[3] 和 slot[0] 这两个（tail 指向下一个可写的 slot[1]，所以已写的是 slot[3]、slot[0]）。

**练习 2**：`len` 里复查的是 tail 而不是 head。如果把整段改成「读 head → 读 tail → 复查 head」，逻辑还正确吗？

**答案**：原则上对称地改成「复查最后读的那个之前的、即第一个读的」即可——也就是先读 head 就复查 head。关键是**复查第一个被读的变量**，保证它在你读第二个变量时没变。所以「读 head → 读 tail → 复查 head」同样正确。但项目里固定用「读 tail → 读 head → 复查 tail」，二者等价，任选其一即可。

---

### 4.4 环形缓冲语义总览：从 force_push 到 Drop 的复用

#### 4.4.1 概念说明

把前三个模块串起来：`force_push` 写、`pop` 读、`len/is_full/is_empty` 查询、`Drop` 回收——它们共享同一套环形缓冲模型。本模块做两件事：

1. 用端到端的多生产者测试（`spsc_ring_buffer` / `mpmc_ring_buffer`）验证「force_push 在并发下不丢数据」。
2. 指出 `Drop` 里**原样复用了 `len` 的那段 index 算术**来决定要 drop 多少个槽——一次写、处处用的代码复用。

#### 4.4.2 核心流程

`Drop` 的逻辑：

1. 若 `T` 不需要 drop（`mem::needs_drop::<T>() == false`，比如整数），直接返回，什么都不做。
2. 否则用 `get_mut()` 拿到 head/tail 的非原子副本（独占引用，无需原子）。
3. **用与 `len` 完全相同的算术**算出当前有多少个已初始化的槽（`len`）。
4. 从 head 起，绕回地遍历 `len` 个槽，逐个 `assume_init_drop()`。

#### 4.4.3 源码精读

`Drop` 里那段算 `len` 的代码，和 `len()` 里的四分支**几乎逐字相同**：

[src/array_queue.rs:L545-L553](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L545-L553) —— `Drop` 中计算需回收元素个数的算术，与 `len()` 共享同一套 `hix`/`tix` 分类逻辑。区别仅在于：`Drop` 持有 `&mut self`，head/tail 用 `get_mut()` 直接读，不需要一致性快照循环（drop 时已经没有别的线程访问了）。

随后按 `len` 绕回遍历：

[src/array_queue.rs:L556-L569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L556-L569) —— 从 `hix` 开始，遇到 `hix + i >= capacity` 就绕回，逐个 `assume_init_drop()`。`needs_drop` 守卫保证 `T` 是 `Copy`/平凡类型时这里整段被跳过，零开销。

**端到端的环形缓冲正确性**靠下面两个测试守护：

- [`spsc_ring_buffer`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L176-L213)：单生产者用 `force_push` 推 `0..COUNT`，被覆盖挤出的旧值由生产者自己计数，消费者 pop 也计数，最后断言**每个 id 恰好被「消费」一次**（要么被 pop，要么被 force_push 挤出）。
- [`mpmc_ring_buffer`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L251-L294)：4 生产者 4 消费者版本，每个 id 被消费 `THREADS` 次。

这两个测试是「force_push 不丢数」的最强证据：环形缓冲覆盖最旧元素时，被覆盖的值会通过 `Some(old)` 返回给调用者，调用者必须自行处理（测试里用 `AtomicUsize` 计数）。**`force_push` 的契约是「要么留在队列里被 pop，要么作为返回值交还给你」，绝不静默丢弃。**

#### 4.4.4 代码实践

**实践目标**：参考 `mpmc_ring_buffer`，写一个多生产者 force_push 测试，验证「不丢数」。

**操作步骤**：在 `tests/array_queue.rs` 末尾加一个测试：

```rust
// 示例代码：参考 mpmc_ring_buffer，缩小规模便于快速跑
#[test]
fn my_mpmc_ring_buffer() {
    const COUNT: usize = if cfg!(miri) { 50 } else { 5_000 };
    const THREADS: usize = 4;

    let t = AtomicUsize::new(THREADS);
    let q = ArrayQueue::<usize>::new(8);
    let v = (0..COUNT).map(|_| AtomicUsize::new(0)).collect::<Vec<_>>();

    scope(|scope| {
        for _ in 0..THREADS {
            scope.spawn(|_| loop {
                match t.load(Ordering::SeqCst) {
                    0 if q.is_empty() => break,
                    _ => {
                        while let Some(n) = q.pop() {
                            v[n].fetch_add(1, Ordering::SeqCst);
                        }
                    }
                }
            });
        }
        for _ in 0..THREADS {
            scope.spawn(|_| {
                for i in 0..COUNT {
                    if let Some(n) = q.force_push(i) {
                        v[n].fetch_add(1, Ordering::SeqCst);  // 被挤出的也算「消费」
                    }
                }
                t.fetch_sub(1, Ordering::SeqCst);
            });
        }
    }).unwrap();

    for c in v {
        assert_eq!(c.load(Ordering::SeqCst), THREADS);
    }
}
```

运行：`cargo test -p crossbeam-queue --test array_queue my_mpmc_ring_buffer`。

**需要观察的现象 / 预期结果**：

- 测试通过：每个 id 的计数恰好等于 `THREADS`（4 个生产者各推一次，每次要么被 pop 要么被挤出，合计 4 次）。
- 把 `force_push` 换成普通 `push`（满时重试 `while q.push(i).is_err() {}`）也能通过，但行为不同——普通 push 永不丢数也永不挤出，所有元素最终都在队列里被 pop。

**待本地验证**：用 `MIRIFLAGS="-Zmiri-many-seeds" cargo +nightly miri test ...` 跑小规模版本，体会 miri 对并发正确性的额外把关。

#### 4.4.5 小练习与答案

**练习 1**：`Drop` 里为什么不需要 `len()` 那个「复查 tail」的一致性循环？

**答案**：`Drop` 接收 `&mut self`，拥有独占引用，此时不可能有其他线程在 push/pop（Rust 的别名规则保证），所以 head/tail 不会变化，一次读取就是一致的，无需复查。一致性循环只为「并发共享引用 `&self`」而设。

**练习 2**：如果 `T` 是 `i32`（`Copy` 且 `needs_drop` 为假），`Drop` 会做哪些工作？

**答案**：`mem::needs_drop::<i32>()` 为 `false`，整个 drop 循环被跳过，`Drop::drop` 几乎是空操作——`ArrayQueue` 自己只持有 `Box<[Slot<T>]>`，其内存由 `Box` 的默认 drop 回收，无需逐槽处理。这是 `needs_drop` 守卫带来的零开销。

---

## 5. 综合实践

把本讲全部内容串起来，实现一个「**固定容量的滚动指标缓冲**」：

**需求**：用一个 `ArrayQueue<f64>`（`cap = 16`）作为最近 16 次采样的滚动窗口。多个采样线程不断 `force_push` 新样本（旧的自动被挤出）；一个监控线程每隔一段打印 `len()`、`is_full()`，并把队列里**当前**的全部样本求平均。

**提示**：

1. 用 `std::sync::Arc` 共享队列，多个生产者线程用 `force_push`。
2. 监控线程用 `while !q.is_full() {}` 等到填满后开始（体会「快照」语义）。
3. 求平均时，反复 `pop` 直到 `is_empty()`，累加计数——但注意：边 pop 边 push 的并发下，你 pop 出的是「某个瞬间」的快照，且生产者还在推。你可以用一个本地 `Vec` 收集 pop 到的值，pop 到 `None` 时计算平均，再把值 `force_push` 回去（模拟「看一眼但不消费」）。
4. 观察并解释：为什么你收集到的样本数可能小于 16？为什么平均数会随时间漂移？

**预期收获**：

- 亲手感受 `force_push` 的环形覆盖：旧样本被挤出（返回值里有，但你这里可以忽略）。
- 理解 `is_empty`/`is_full`/`len` 都是快照，不能用来做精确同步。
- 把 `pop` + `force_push` 组合当成「快照读 + 回填」的并发读模式。

**待本地验证**：这个练习没有唯一正确输出，重点是观察并发下的行为并与本讲的「快照语义」「环形覆盖」对得上。

## 6. 本讲小结

- `force_push` 与 `push` 共享 `push_or_else` 的 CAS 主循环，只在「疑似满」分支分叉：`push` 复核后退还元素（`Err(v)`），`force_push` 用 head 的 CAS 抢占覆盖权，成功则**同时推进 head 与 tail**、`replace` 出最旧元素返回。
- `force_push` 的「满」确认不靠 load head，而靠 `head.compare_exchange_weak(tail - one_lap, new_tail - one_lap)`：CAS 成功 ⟺ 真的满。赢得 head CAS 后 tail 用 `store` 即可，因为此时无人能竞争 tail。
- `capacity()` 直接返回 `buffer.len()`，零原子、恒定。`is_empty` / `is_full` 用 `SeqCst` 读两个指针并比较，返回的是「某个瞬间的快照」，且 load 顺序经过精心选择以让「返回 true」倾向保守可信。
- `len()` 用「读 tail → 读 head → 复查 tail」的一致性循环，保证 (tail, head) 是真实存在过的组合；再用 `hix`/`tix` 的三分类 + 空/满二分把环形计数算对。`Drop` 里原样复用了这段算术。
- 环形缓冲的核心契约：被 `force_push` 覆盖的旧值**必然**通过 `Some(old)` 返回给调用者，绝不静默丢失——`spsc_ring_buffer` / `mpmc_ring_buffer` 测试正是为此而生。

## 7. 下一步学习建议

- **横向对照 SegQueue**：`SegQueue` 是无界队列，没有「满」的概念，也就没有 `force_push`/`is_full`/`capacity`。进入 [u3-l1](./u3-l1-segqueue-block-structure.md) 起的 SegQueue 单元，对比「有界环形」与「无界分段链表」两种设计取舍。
- **深挖内存序**：本讲多次出现 `SeqCst`、`Acquire`/`Release`、`fence(SeqCst)`，但只给了直觉解释。完整的形式化论证（为什么 `is_empty` 的读序在线性化意义下合法、`len` 的一致性循环为什么够用）放在 [u4-l1 原子内存序与 fence](./u4-l1-atomic-orderings-fence.md)。
- **unsafe 与 Drop 安全性**：`force_push` 里的 `replace(...).assume_init()`、`Drop` 里的 `assume_init_drop()` 为什么安全，会在 [u4-l3 unsafe 与 MaybeUninit](./u4-l3-unsafe-maybeuninit-safety.md) 系统论证。
- **并发测试方法**：本讲引用的 `ring_buffer` / `len` 多线程测试、`cfg!(miri)` 缩规模等套路，在 [u4-l4 并发测试与可线性化](./u4-l4-concurrency-testing.md) 有系统讲解。
