# push 与 pop 的无锁主链路：CAS 循环与 stamp 推进

## 1. 本讲目标

本讲紧接着 [u2-l1](u2-l1-arrayqueue-data-structure.md) 的数据结构，进入 `ArrayQueue` 真正的「发动机舱」：当一个线程调用 `push` 或 `pop` 时，源码里到底发生了一条怎样的无锁（lock-free）调用链。

学完本讲，你应当能够：

- 逐行走读 `push_or_else` 的 CAS 循环：如何把 `tail` 拆成 `index/lap`、如何计算 `new_tail`、如何用 `slot.stamp` 判断「这个槽能不能写」。
- 说清 `compare_exchange_weak` 在这里的作用，以及 `Backoff::spin` 与 `Backoff::snooze` 各自用在什么分支、为什么不同。
- 解释 `push` 与 `force_push` 如何复用同一段主循环、仅在「队列满」时通过一个闭包 `f` 走不同分支。
- 逐行走读 `pop` 的对称逻辑：用 `head + 1 == stamp` 判定可弹出，用 `stamp == head` 加二次确认判定空队列并返回 `None`。
- 对一个 `cap=2` 的小队列，手算出连续入队/出队时 `head`、`tail`、各槽 `stamp` 的完整变化。

本讲**只**讲 `push`/`pop`/`force_push` 的并发主链路与满/空判定；原子内存序的深度论证留给 [u4-l1](u4-l1-atomic-orderings-fence.md)，`push_mut`/`pop_mut` 的独占快路径留给 [u2-l4](u2-l4-arrayqueue-exclusive-mut.md)，`len`/`is_empty`/`is_full` 的查询接口留给 [u2-l3](u2-l3-arrayqueue-force-push-capacity.md)。

## 2. 前置知识

在进入源码前，先用三段话把 [u2-l1](u2-l1-arrayqueue-data-structure.md) 已建立的结论复述一遍，因为本讲每一行代码都依赖它们。

**stamp（戳）编码。** `head` 和 `tail` 都是一个 `usize`，但它同时编码了两段信息：低若干位是 `index`（在 `buffer` 数组里的下标），高位是 `lap`（已经绕了多少圈）。拆分公式是：

\[
\text{index} = \text{stamp}\ \&\ (\text{one\_lap} - 1),\qquad \text{lap} = \text{stamp}\ \&\ !(\text{one\_lap} - 1)
\]

其中 `one_lap = (cap + 1).next_power_of_two()`，既是 2 的幂、又严格大于 `cap`。

**为什么要多绕一圈？** 因为环形缓冲必须区分「空」和「满」：当 `head == tail` 时到底是空还是满？Vyukov 的做法是用 `lap` 来区分——`tail` 比 `head` 多绕一整圈才算满，完全重合才算空。这要求每圈能表示的下标数严格大于 `cap`，所以 `one_lap > cap`。

**Slot 的状态机。** 每个槽 `Slot<T>` 只有一个 `stamp: AtomicUsize` 字段充当状态：

- 当 `slot.stamp == tail` 时，这个槽「轮到被写入」（可写）。
- 写完后生产者把 `slot.stamp` 置为 `tail + 1`；于是当消费者走到 `head` 满足 `head + 1 == slot.stamp` 时，这个槽「可读」。
- 消费者读完后把 `slot.stamp` 置为 `head + one_lap`，等于提前把这个槽「预约」给下一圈的同一个 `index`。

初始时 `slot[i].stamp = i`、`head = tail = 0`，于是只有 `slot[0].stamp == tail == 0`，即一开始只有 0 号槽可写。

**CAS（compare-and-swap）。** 无锁算法的核心原语：原子地「比较内存值是否等于预期，若是则写入新值」。Rust 里是 `compare_exchange_weak`。多个线程同时 CAS 同一个变量时，**只有一个成功**，其余全部失败并拿到最新值重试。这就是无锁算法在无锁的情况下仍能保证「每个槽只被一个生产者写入」的关键。

如果你对 `Ordering::Relaxed/Acquire/Release/SeqCst` 还不熟，本讲先用「直觉版」理解即可（哪一步是「发布数据」、哪一步是「读取数据」），精确的 happens-before 论证在 [u4-l1](u4-l1-atomic-orderings-fence.md)。

## 3. 本讲源码地图

本讲只涉及一个源文件：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | `push_or_else`（入队通用主循环）、`push` 与 `force_push`（两种「满」处理）、`pop`（出队主循环与空判定） |

测试文件 [tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) 会在代码实践里用作阅读材料（`smoke`、`spsc`、`mpmc`）。

## 4. 核心概念与源码讲解

### 4.1 push_or_else：入队的通用 CAS 主循环

#### 4.1.1 概念说明

`ArrayQueue` 对外暴露 `push` 和 `force_push` 两个入队 API：满了前者直接放弃（返回 `Err(value)`），后者挤掉最旧元素（返回 `Some(old)`）。但两者**99% 的主循环是相同的**——都是「找到 tail 槽 → 判断能否写 → CAS 推进 tail → 写值 → 更新 stamp」。区别只在于「发现队列满之后怎么办」这一步。

所以源码把这段公共主循环抽成了一个内部泛型函数 `push_or_else`，把「满处理」策略做成一个闭包 `f` 传进去：

- `push` 传入的 `f`：确认满就返回 `Err(v)`，把元素还给调用者。
- `force_push` 传入的 `f`：尝试推进 `head` 把最旧元素挤掉，成功则返回 `Err(old)` 把被挤掉的旧值带出去。

这是一个很值得学的工程手法：**用闭包参数化算法中唯一不同的那一步**，避免复制粘贴一整套 CAS 循环。

#### 4.1.2 核心流程

`push_or_else` 的骨架是一个 `loop`，每轮做四件事：

1. **读 tail**：`Relaxed` 加载当前尾指针（只是找个候选位置，真正的同步靠后面的 stamp）。
2. **拆 stamp + 算 new_tail**：把 `tail` 拆成 `index/lap`，算出「tail 推进一步后」的新值 `new_tail`（同圈 +1 或换圈归零）。
3. **看槽 + 三选一分支**：加载 `slot[index].stamp`（`Acquire`），根据它与 `tail` 的关系走三条路：
   - `tail == stamp` → 这个槽轮到我了，去 CAS 推进 tail（见 4.3）。
   - `stamp + one_lap == tail + 1` → 这个槽还残留着上一圈没被消费的数据，**可能满了**，调用闭包 `f` 决定怎么办。
   - 其它 → 槽状态还没跟上（比如别的生产者刚 CAS 成功 tail 但还没写完值），`snooze` 退避后重读 tail。
4. CAS 成功后写值、更新 stamp、返回；失败则 `spin` 重试。

#### 4.1.3 源码精读

先看函数签名和循环顶部——注意泛型参数 `F` 和它的签名 `(T, usize, usize, &Slot<T>) -> Result<T, T>`：

[src/array_queue.rs:127-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L127-L147) — `push_or_else` 的签名、`Backoff` 创建、`tail` 的 `Relaxed` 加载，以及 `loop` 顶部把 `tail` 拆成 `index/lap`、计算 `new_tail` 的逻辑。

闭包 `f` 收到的是 `(value, tail, new_tail, slot)`：

- `value`：当前要入队的值（按值传递，闭包可以决定「还回去」或「继续试」）。
- `tail` / `new_tail`：当前尾和推进后的尾，`force_push` 要用它们算出对应的 `head` 推进量。
- `slot`：发生冲突的那个槽引用，`force_push` 要直接替换里面的旧值。

返回 `Result<T, T>` 的设计很巧妙：

- `Ok(v)` → 「我不想处理，把值还给我重试」，主循环会 `spin` 一下重新加载 `tail` 再来一轮。
- `Err(t)` → 「我处理完了（或者确认满了），把这个值带出去」，主循环里的 `?` 会立刻把 `Err(t)` 作为 `push_or_else` 的返回值传出去。

所以 `push` 用 `Err` 表示「满了，退还元素」；`force_push` 用 `Err` 表示「我挤掉了一个旧值，把它带出去」。同一个返回类型，两种语义，靠调用方解释。`force_push` 末尾的 `.err()` 就是把 `Result<(), T>` 转成 `Option<T>`（`Ok → None`，`Err(t) → Some(t)`）。

#### 4.1.4 代码实践

**实践目标**：建立「主循环是公共的、只有闭包不同」的直觉。

**操作步骤**：

1. 打开 [src/array_queue.rs:203-215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L203-L215)（`push`）和 [src/array_queue.rs:275-301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L275-L301)（`force_push`）。
2. 对比两个函数体：它们除了传给 `push_or_else` 的闭包不同，调用方式几乎一样。
3. 分别列出两个闭包在「返回 `Ok`」和「返回 `Err`」时各自的语义。

**预期结果**：

| 闭包 | `Ok(v)` 含义 | `Err(t)` 含义 |
| --- | --- | --- |
| `push` 的 `f` | 队列其实没满（head 已被别人推进），重试 | 队列确实满了，把 `value` 退还给调用者 |
| `force_push` 的 `f` | CAS `head` 失败（别的生产者先挤了），重试 | 成功挤掉旧值，把旧值 `old` 带出去 |

#### 4.1.5 小练习与答案

**练习 1**：如果把 `push_or_else` 改成两套分别属于 `push` 和 `force_push` 的复制粘贴代码，会带来什么具体的维护风险？

**答案**：CAS 循环、stamp 三分支判定、`Backoff` 退避、`new_tail` 计算这些细节高度敏感（错一个内存序就是数据竞争）。两套副本意味着以后任何一处 bug 修复或性能优化都要改两遍，且很容易漏改一处而导致两条路径行为不一致——这正是抽公共主循环的价值。

**练习 2**：闭包 `f` 的返回类型为什么是 `Result<T, T>` 而不是 `Option<T>`？

**答案**：因为无论是「重试」还是「带出值」，都需要把那个 `T`（可能改头换面，如 `force_push` 换成了旧值）继续在主循环的 `value` 变量里流动。`Result<T,T>` 的 `Ok`/`Err` 两个分支都携带一个 `T`，恰好对应「还回去重试」与「带出去返回」两种情况，配合 `?` 运算符写起来最自然。

### 4.2 stamp 匹配与 new_tail 计算

#### 4.2.1 概念说明

主循环每轮第一件事就是把 `tail` 这个打包值拆开，算出「tail 往前走一步」后的新值 `new_tail`。这一步是后续 CAS 的目标值——CAS 要把 `tail` 从「当前值」推进到 `new_tail`。

`new_tail` 有两种情形：

- **同圈推进**：当前 `index` 还没到数组末尾（`index + 1 < cap`），新值就是 `tail + 1`（lap 不变，index +1）。
- **换圈归零**：当前 `index` 已经是最后一个（`index + 1 == cap`），新值要进入下一圈、index 归零，即 `lap + one_lap`。

注意换圈时是 `lap.wrapping_add(one_lap)`，**用 `wrapping_add`** 是因为 `lap` 的高位随着圈数增加会不断累加，最终可能溢出 `usize`；这里刻意用环绕算术，因为我们只关心 stamp 的「相对差」，不关心绝对圈数。

#### 4.2.2 核心流程

每一轮的拆分与计算可以表示为：

\[
\text{index} = \text{tail}\ \&\ (\text{one\_lap}-1)
\]
\[
\text{lap} = \text{tail}\ \&\ !(\text{one\_lap}-1)
\]
\[
\text{new\_tail} =
\begin{cases}
\text{tail} + 1, & \text{index}+1 < \text{cap} \quad(\text{同圈})\\
\text{lap} + \text{one\_lap}, & \text{index}+1 = \text{cap}\quad(\text{换圈，index 归零})
\end{cases}
\]

算完 `new_tail` 后，去看 `slot[index].stamp`：如果 `stamp == tail`，说明这个槽正等着被「当前 tail」写入，匹配成功，可以走 CAS 路径。

#### 4.2.3 源码精读

[src/array_queue.rs:134-152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L134-L152) — 把 `tail` 拆成 `index/lap`，按 `index + 1 < capacity()` 选「同圈 +1」或「换圈 `lap.wrapping_add(one_lap)`」得到 `new_tail`；然后用 `get_unchecked(index)` 取槽、`Acquire` 加载它的 `stamp`。

两个细节值得注意：

1. `debug_assert!(index < self.buffer.len())`：因为 `one_lap > cap`，`index = tail & (one_lap-1)` 理论上可能落在 `[cap, one_lap)` 区间（即大于 `cap-1` 的下标）吗？不会——因为 `new_tail` 在 `index+1 == cap` 时就强制归零了，所以合法的 `tail` 其 `index` 永远在 `0..cap` 内。这个 `debug_assert` 就是对该不变量的断言。
2. 用 `get_unchecked` 而非 `[]` 是为了跳过边界检查——上面那条不变量已经保证安全，去掉分支指令能让热路径更快。这是无锁队列里典型的「用已证明的不变量换性能」。

`if tail == stamp` 的判定就在 [src/array_queue.rs:155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L155)：这一行是整个算法的「匹配点」，下面 4.3 讲它匹配成功后的 CAS。

#### 4.2.4 代码实践

**实践目标**：能手算 `new_tail`，验证对拆分公式的理解。

**操作步骤**：取 `cap = 3`，则 `one_lap = (3+1).next_power_of_two() = 4`。对下面每个 `tail` 值，写出 `index`、`lap`、`new_tail`：

| tail | index | lap | new_tail（同圈还是换圈） |
| ---- | ----- | --- | ----------------------- |
| 0    | ?     | ?   | ? |
| 2    | ?     | ?   | ? |
| 3    | ?     | ?   | ? |
| 4    | ?     | ?   | ? |
| 7    | ?     | ?   | ? |

**预期结果**（请先自己算再对照）：

| tail | index | lap | new_tail | 说明 |
| ---- | ----- | --- | -------- | ---- |
| 0    | 0     | 0   | 1        | 同圈 |
| 2    | 2     | 0   | 3        | 同圈 |
| 3    | 3     | 0   | 4        | `index+1=4==cap=3`? 不对——注意 `cap=3` 时 `index` 合法范围是 `0..3`，`index=3` 不会出现。见下方说明 |
| 4    | 0     | 4   | 5        | 换圈后已在新圈，`index+1=1<3` 同圈 |
| 7    | 3     | 4   | ?        | 不可能出现（同上） |

**重要修正**：上表里 `tail=3` 与 `tail=7` 是**陷阱行**。因为 `one_lap=4`、`cap=3`，合法 `index` 只有 `0,1,2`（`index+1 < 3` 即 `index ∈ {0,1}` 同圈推进，`index=2` 时 `index+1=3==cap` 触发换圈归零）。所以 `tail` 永远不会是 `3` 或 `7`——换圈直接跳到下一圈的 `index=0`（即 `4`、`8`、…）。如果你算出 `index=3`，说明你还没抓住「`one_lap > cap` 正是为了让换圈跳过这些多余下标」这一点。这正是 [u2-l1](u2-l1-arrayqueue-data-structure.md) 强调的 `one_lap = (cap+1).next_power_of_two()` 的用意。

> 待本地验证：你可以在一个临时测试里 `dbg!(q.one_lap)` 打印 `cap=3` 时的值（需要把测试放在同 crate 内或临时把 `one_lap` 设为 `pub`），确认它是 `4`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `new_tail` 在换圈时是 `lap.wrapping_add(one_lap)` 而不是 `tail + one_lap`？两者数值上相等吗？

**答案**：数值上相等（因为换圈时 `index` 恰好是 `one_lap-1`? 不一定）。更准确地说：换圈发生在 `index+1 == cap`，此时 `tail = lap | index`，`tail + one_lap = lap + index + one_lap`，而 `index < one_lap` 但不一定为 `one_lap-1`，所以 `tail + one_lap ≠ lap + one_lap`。源码想要的是「lap 进一位、index 归零」，即 `lap + one_lap`，**不是** `tail + one_lap`。用 `lap.wrapping_add(one_lap)` 精确表达了「丢弃残余 index、lap 加一圈」的语义，并兼容高位溢出。

**练习 2**：`slot.stamp` 用 `Ordering::Acquire` 加载，而 `tail` 用 `Relaxed` 加载，为什么后者可以更弱？

**答案**：`tail` 只是用来选候选槽和构造 CAS 的预期值，真正建立「数据可见性」的同步点在 `slot.stamp` 的 `Acquire`/`Release` 配对（生产者写完值后 `Release` 存 stamp，消费者 `Acquire` 读 stamp 才能保证看到值）。`tail` 本身不携带需要同步的有效载荷，所以 `Relaxed` 足够。（完整的 happens-before 论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)。）

### 4.3 compare_exchange_weak 与 backoff 退避

#### 4.3.1 概念说明

4.2 解决了「找到候选槽、算出目标 tail」。这一节讲匹配成功（`tail == stamp`）后真正「抢占槽位」的动作：一次 CAS。

CAS 在并发下的行为是：可能有多个生产者同时读到 `tail = 5`、都认为槽 5 是自己的，但 `compare_exchange_weak` 保证**只有一个**能把 `tail` 从 5 改成 6，其余的全部失败、拿到最新 `tail`、重试。这就无锁地保证了「每个槽只被一个生产者写入」。

但 CAS 失败有两种性质不同的原因，需要不同的退避策略，因此源码用了 `Backoff` 的两个方法：

- **`spin()`**：轻量自旋（几次空转）。用在「CAS 被别人抢走了」——槽马上可能又被释放或推进，重试成本低，别让出 CPU。
- **`snooze()`**：更重的退避（可能 `yield` 让出 CPU）。用在「槽状态还没跟上」——比如别的生产者刚推进 `tail` 但还没把值写完，我要等的是「另一个线程完成它的工作」，可能要等一会，长时间忙等是浪费 CPU。

这是无锁编程里非常实用的「分级退避」思想。

#### 4.3.2 核心流程

`push_or_else` 在 `tail == stamp` 匹配成功后的三条子路径：

```
匹配成功 (tail == stamp):
  CAS tail -> new_tail  (成功用 SeqCst，失败用 Relaxed)
  ├─ Ok：写值进 slot.value；slot.stamp.store(tail+1, Release)；返回 Ok(())
  └─ Err(t)：tail = t（最新值）；backoff.spin()；继续 loop

疑似满 (stamp + one_lap == tail + 1):
  fence(SeqCst)
  调用闭包 f：
  ├─ 返回 Err → push_or_else 立刻返回 Err
  └─ 返回 Ok(v) → value=v；backoff.spin()；重读 tail；继续 loop

其它（槽状态滞后）:
  backoff.snooze()；重读 tail；继续 loop
```

#### 4.3.3 源码精读

**CAS 成功路径**——先 CAS，成功后才写值、再 `Release` 发 stamp：

[src/array_queue.rs:155-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L155-L170) — 匹配 `tail == stamp` 后，用 `compare_exchange_weak(tail, new_tail, SeqCst, Relaxed)` 抢占尾指针；成功则把值写入 `slot.value`，并把 `slot.stamp` 置为 `tail + 1`（`Release`），让消费者能 `Acquire` 看到这个值。

顺序非常重要：**先 CAS 占住 tail，再写值，最后发 stamp**。如果先写值再 CAS，别的线程可能通过 stamp 看到一个「tail 还没推进但值已写入」的中间态；如果先发 stamp 再写值，消费者可能读到未初始化内存。当前的顺序保证了「stamp 变成 `tail+1` 时，值一定已经写好」。

**CAS 失败路径**——拿最新 tail，轻量自旋：

[src/array_queue.rs:171-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L171-L175) — CAS 失败时 `Err(t)` 把最新的 `tail` 给我们，赋值后 `backoff.spin()` 一下立刻进入下一轮 `loop`。

注意用的是 `compare_exchange_weak` 而非 `compare_exchange`：`_weak` 版本允许「伪失败」（spurious failure，即值其实等于预期却仍返回失败）。因为在 `loop` 里伪失败只是多重试一次，代价可接受，而 `_weak` 在某些 CPU 架构（如 ARM/LL-SC）上比 `_strong` 更便宜。

**疑似满分支**——这里就是闭包 `f` 登场的地方：

[src/array_queue.rs:176-180](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L176-L180) — 当 `stamp.wrapping_add(one_lap) == tail + 1`（即这个槽的 stamp 恰好比「当前 tail+1」少一整圈，说明它还残留着上一圈未消费的数据）时，先 `fence(SeqCst)` 做全序同步，再调用闭包 `f` 决定怎么办；闭包返回 `Ok` 则 `spin` 重试，返回 `Err` 则通过 `?` 直接返回。

`push` 的闭包做「权威满检查」：

[src/array_queue.rs:204-214](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L204-L214) — 重新加载 `head`，若 `head.wrapping_add(one_lap) == tail` 则确认满，返回 `Err(v)`；否则返回 `Ok(v)` 让主循环重试。

这里有个精妙的**两级判定**：stamp 的 `+one_lap` 条件只是「可能满」的**廉价提示**（不需要每次 push 都读 head）；只有提示命中时，才在闭包里读 `head` 做**权威确认**。如果确认时发现 head 已被消费者推进（其实没满），就返回 `Ok` 让主循环重新尝试——此时 stamp 也已经被消费者更新，下一轮多半能直接匹配。

**snooze 分支**：

[src/array_queue.rs:181-185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L181-L185) — 当 stamp 既不等于 `tail`、也不是「上一圈残留」时，说明槽处于过渡态（别的生产者占住了 tail 但还没写完值、还没发 stamp），此时用 `backoff.snooze()` 做较重退避，再重读 `tail`。

#### 4.3.4 代码实践

**实践目标**：通过阅读并发测试，理解 CAS 失败与重试在真实多线程下的表现。

**操作步骤**：

1. 打开 [tests/array_queue.rs:216-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L216-L249) 的 `mpmc` 测试。
2. 注意生产者线程里那句 `while q.push(i).is_err() {}`：它不断重试直到 push 成功。这正是 push_or_else 返回 `Err`（队列瞬态已满）时调用方该做的事——背压重试。
3. 运行 `cargo test -p crossbeam-queue mpmc -- --nocapture`（在仓库根目录）。

**需要观察的现象**：测试通过；4 个生产者各推 25000 个 id（miri 下 50 个）、4 个消费者各消费 25000 个，最终每个 id 被消费次数恰好等于 `THREADS`。这验证了「即使 CAS 不断失败重试，最终每个值都恰好被写入并消费一次」。

**预期结果**：测试 `mpmc ... ok`。如果在本机用 `cargo miri test` 运行，规模会自动缩小到 `COUNT=50`（见 `cfg!(miri)` 分支），但语义一致。

> 待本地验证：具体运行耗时取决于机器；如果你在调试中想观察 CAS 失败频率，可在 `push_or_else` 的 `Err(t) =>` 分支临时加一个 `AtomicUsize` 计数器（仅本地学习用，勿提交），高并发下会看到它增长很快。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CAS 成功用 `SeqCst`、失败用 `Relaxed`？

**答案**：成功路径需要和其它线程的 CAS 建立全局顺序（保证「每个 tail 值只有一个生产者赢」的全序可见），所以用最强的 `SeqCst`；失败路径只是「我没抢到，拿最新值重试」，不发布任何数据，`Relaxed` 就够，更便宜。完整论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)。

**练习 2**：把 `compare_exchange_weak` 换成 `compare_exchange`（strong）程序还能正确吗？为什么源码选 `_weak`？

**答案**：正确性不变——`strong` 只是去掉伪失败，逻辑等价。但 `_weak` 在 LL-SC 类架构（如 ARM）上编译出更轻的指令序列，而本处本来就在 `loop` 里重试，伪失败只是多重试一次，代价可接受。所以在循环 CAS 场景里 `_weak` 几乎总是更优选择。

**练习 3**： stamp 疑似满分支里 `f.value = f(value, tail, new_tail, slot)?` 中的 `?`，在 `push` 和 `force_push` 场景下分别会导致什么结果？

**答案**：`push` 的闭包确认满时返回 `Err(v)`，`?` 让 `push_or_else` 立刻返回 `Err(v)`，`push` 再把这个 `Err(v)` 返回给调用者（队列满，退还元素）。`force_push` 的闭包成功挤掉旧值时返回 `Err(old)`，`?` 让 `push_or_else` 返回 `Err(old)`，`force_push` 末尾 `.err()` 把它转成 `Some(old)` 返回给调用者。

### 4.4 pop 的对称链路与空队列判定

#### 4.4.1 概念说明

`pop` 是 `push` 的镜像：`push` 推进 `tail`、把值写进槽、把 stamp 从 `tail` 改成 `tail+1`；`pop` 推进 `head`、从槽里读出值、把 stamp 从 `head+1` 改成 `head+one_lap`（把它预约给下一圈）。

两者的判定条件也是对称的：

- `push` 可写：`slot.stamp == tail`。
- `pop` 可读：`slot.stamp == head + 1`（即生产者已经把这个槽的 stamp 从 `tail` 推进到了 `tail+1`，而现在 `head` 追上了当初的 `tail`）。

「空」的判定则借助 `head == tail`：如果 `pop` 走到的槽 `stamp == head`（说明这个槽还没被本圈写入，处于「等待写入」初态），就**怀疑队列空**，于是二次加载 `tail`，若 `tail == head` 则确认空、返回 `None`。

#### 4.4.2 核心流程

```
loop:
  读 head (Relaxed)；拆 index/lap；取 slot[index]；Acquire 读 stamp
  ├─ head+1 == stamp（可读）:
  │     算 new（同圈 +1 或换圈归零）
  │     CAS head -> new  (SeqCst / Relaxed)
  │     ├─ Ok：读出 slot.value；slot.stamp.store(head+one_lap, Release)；返回 Some(value)
  │     └─ Err(h)：head = h；spin
  ├─ stamp == head（疑似空）:
  │     fence(SeqCst)；读 tail (Relaxed)
  │     ├─ tail == head → 确认空，返回 None
  │     └─ 否则 → spin；重读 head
  └─ 其它（槽状态滞后）:
        snooze；重读 head
```

#### 4.4.3 源码精读

**可读判定与 CAS**：

[src/array_queue.rs:333-362](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L333-L362) — `head + 1 == stamp` 表示这个槽已被生产者写好；计算 `new`（与 `push` 算 `new_tail` 同构：同圈 `head+1` 或换圈 `lap+one_lap`），CAS 推进 `head`。成功后用 `slot.value.get().read().assume_init()` 把值搬出来，再把 `slot.stamp` 置为 `head.wrapping_add(one_lap)`（`Release`）——这一步把这个槽「交还」给下一圈的同 `index` 生产者（下一圈 `tail` 走到这里时 `tail` 会等于这个新 stamp）。

读值的顺序同样是「先 CAS 占住 head，再读值」：保证只有一个消费者读到这个值。读完后立刻把 stamp 推进 `one_lap`，使得生产者在下一圈能复用此槽。这段 `unsafe { slot.value.get().read().assume_init() }` 的安全性论证在 [u4-l3](u4-l3-unsafe-maybeuninit-safety.md)。

**疑似空与确认空**：

[src/array_queue.rs:363-373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L363-L373) — 当 `stamp == head`（槽还停在「等待本圈写入」的初态）时，先 `fence(SeqCst)`，再 `Relaxed` 读 `tail`；若 `tail == head` 则确认空，返回 `None`；否则说明有生产者正在路上（tail 已领先），`spin` 后重读 head 继续。

这里的 `fence(SeqCst)` 与 `push` 满分支的 fence 是一对：它们保证「消费者看到空」和「生产者看到满」不会因为重排而同时成立（即不会出现「生产者以为满了退还元素、消费者却以为空返回 None」这种丢数据的一致性违背）。这是整个算法最精妙的全序点之一，[u4-l1](u4-l1-atomic-orderings-fence.md) 会专门剖析。

注意「确认空」返回的 `None` 只反映「调用 `pop` 这一瞬间」队列空，并不保证调用方拿到 `None` 后队列仍然空——别的生产者可能紧接着就 push 了。这是无锁队列的固有语义，和 `std::sync::mpsc` 一致。

**snooze 分支**：

[src/array_queue.rs:374-378](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L374-L378) — 当 stamp 处于过渡态（既不可读、也不像空），`snooze` 退避后重读 head，与 `push` 的 snooze 分支对称。

#### 4.4.4 代码实践

**实践目标**：用一个最小冒烟测试验证 `pop` 的满/空边界。

**操作步骤**：

1. 阅读 [tests/array_queue.rs:6-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L6-L16) 的 `smoke` 测试：`cap=1`，push 7 → pop Some(7) → push 8 → pop Some(8) → pop None。
2. 自己写一个 `cap=2` 的版本，覆盖「push 到满 → push 失败 → pop → pop → pop None」全路径：

```rust
// 示例代码（非项目原有，读者自行加入 tests/ 下学习用）
#[test]
fn push_pop_boundary() {
    let q = ArrayQueue::new(2);
    assert_eq!(q.push('a'), Ok(()));
    assert_eq!(q.push('b'), Ok(()));
    assert_eq!(q.push('c'), Err('c')); // 满，退还
    assert_eq!(q.pop(), Some('a'));
    assert_eq!(q.pop(), Some('b'));
    assert!(q.pop().is_none());        // 空
}
```

**需要观察的现象**：第三步 `push('c')` 命中 4.3 的「疑似满 → 确认满」分支，返回 `Err('c')`；最后一步 `pop()` 命中 4.4 的「stamp==head → tail==head → 返回 None」分支。

**预期结果**：测试通过。这个边界与 [src/array_queue.rs:42-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L42-L51) 文档示例完全一致。

#### 4.4.5 小练习与答案

**练习 1**：`pop` 成功读值后为什么把 `slot.stamp` 存成 `head.wrapping_add(one_lap)`，而不是像 `push` 那样存 `head + 1`？

**答案**：因为消费者读完后要**把这个槽交还给下一圈的生产者**。下一圈生产者走到同一个 `index` 时，它的 `tail` 已经比当初多了 `one_lap`（绕了一整圈），所以消费者必须把 stamp 也加上 `one_lap`，才能让下一圈生产者匹配到 `tail == stamp`。`push` 存 `tail+1` 是为了告诉消费者「可读了」；`pop` 存 `head+one_lap` 是为了告诉下一圈生产者「可写了」——两者服务于不同的下一个对象。

**练习 2**：`pop` 里「确认空」之前为什么要 `fence(SeqCst)` 再读 `tail`？直接 `Relaxed` 读 tail 会怎样？

**答案**：`stamp == head` 只是「这个槽看起来没被写」的征兆。在多核上，生产者可能已经推进了 `tail` 并写了值，但相关 store 还没在该消费者的缓存里可见。`fence(SeqCst)` 强制一个全局顺序点，确保随后读到的 `tail` 不会比「已经发布 stamp+值」的生产者更旧，避免「消费者误判空、生产者以为满」的丢数据。直接 `Relaxed` 读 tail 在某些重排下可能读到过期值，导致误报空。（精确论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)。）

**练习 3**：`pop` 返回 `None` 之后，紧接着再调用 `pop` 一定还是 `None` 吗？

**答案**：不一定。`None` 只代表「那次调用的瞬间」队列空。返回后任意时刻都可能有线程 push 新元素，下一次 `pop` 完全可能返回 `Some`。所以调用方不能依赖「连续两次 `None` 之间队列一直空」。

## 5. 综合实践

把本讲四个模块串起来，做一个完整的手算追踪——这正是本讲规格要求的实践任务。

**任务**：对一个 `cap = 2` 的 `ArrayQueue<char>`，画出连续执行 `push('a')`、`push('b')`、`push('c')`（失败）、`pop()` 四步时，`head`、`tail`、各槽 `stamp` 的变化时序图，并标注每次 CAS 是否成功。

**前置计算**：`cap = 2`，`one_lap = (2+1).next_power_of_two() = 4`。`new` 后初始状态：`head = 0`，`tail = 0`，`slot[0].stamp = 0`，`slot[1].stamp = 1`，两槽均未初始化。

**逐步追踪**（用 `{lap, index}` 直观表示 stamp）：

| 步骤 | 操作 | 进入时的 tail/head | 命中分支 | CAS | 写值/读值 | stamp 更新 | 返回 | 操作后状态（head / tail / slot0.stamp / slot1.stamp） |
| ---- | ---- | ------------------ | -------- | --- | --------- | ---------- | ---- | --------------------------------------------------- |
| 0 | `new(2)` | — | — | — | — | — | — | `0 / 0 / 0 / 1` |
| 1 | `push('a')` | tail=0 | `tail==stamp`（slot0: 0==0） | tail 0→1，**成功** | 写 'a'→slot0 | slot0.stamp=0+1=1 | `Ok(())` | `0 / 1 / 1 / 1`（slot0='a'）|
| 2 | `push('b')` | tail=1（index=1, lap=0）| `tail==stamp`（slot1: 1==1）| tail 1→4（换圈），**成功** | 写 'b'→slot1 | slot1.stamp=1+1=2 | `Ok(())` | `0 / 4 / 1 / 2`（slot1='b'）|
| 3 | `push('c')` | tail=4（index=0）| `stamp+one_lap==tail+1`（slot0: 1+4==4+1=5 ✓，疑似满）| **未发起 CAS** | — | 闭包确认 head+one_lap=0+4=4==tail=4 → 满 | `Err('c')` | `0 / 4 / 1 / 2`（不变）|
| 4 | `pop()` | head=0（index=0）| `head+1==stamp`（slot0: 0+1==1 ✓，可读）| head 0→1，**成功** | 读 slot0='a' | slot0.stamp=0+4=4 | `Some('a')` | `1 / 4 / 4 / 2`（slot1='b'）|

**关键观察**（请对照源码确认）：

1. 步骤 2 的 `new_tail` 走的是**换圈**分支（`index+1=2 == cap=2`），所以 tail 从 1 直接跳到 `lap+one_lap = 0+4 = 4`，**跳过了 2、3**（这正是 4.2 练习里「`one_lap > cap` 跳过多余下标」的体现）。
2. 步骤 3 没有发起 CAS：它命中 4.3 的「疑似满」分支，闭包做权威检查（读 head）后确认满，直接返回 `Err('c')`。这验证了「stamp 提示 → head 确认」的两级判定。
3. 步骤 4 读完后把 `slot[0].stamp` 设成 `head+one_lap = 0+4 = 4`，等于把这个槽预约给下一圈——如果再来一次 `push`，它会匹配 `tail==4==slot0.stamp`，复用 slot0。
4. 步骤 4 之后若再 `pop()`：head=1，slot1.stamp=2，`head+1=2==2` 可读，读出 'b'，head→4（换圈）。再 `pop()`：head=4，slot0.stamp=4，`head+1=5≠4` 且 `stamp==head`（4==4）→ 疑似空 → fence → 读 tail=4 → `tail==head` → 确认空，返回 `None`。

**进阶验证**：把上表与 [src/array_queue.rs:42-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L42-L51) 的官方文档示例对照（完全一致）。然后在该示例基础上追加：连续 `push('a')`、`push('b')` 后改用 `force_push('c')`，预测返回值与各字段状态，再对照 [src/array_queue.rs:264-274](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L264-L274) 的 `force_push` 文档示例验证（应得 `force_push('c') == Some('a')`）。

## 6. 本讲小结

- `push_or_else` 是 `push` 与 `force_push` 共享的**通用 CAS 主循环**；两者只在「疑似满」时通过一个闭包 `f` 走不同分支（退还 vs 覆盖）。
- 每轮先把 `tail` 拆成 `index/lap`，按 `index+1 < cap` 选「同圈 +1」或「换圈 `lap.wrapping_add(one_lap)`」得到 `new_tail`；`one_lap > cap` 保证换圈时跳过多余下标。
- 槽状态用单个 `stamp` 充当：`stamp==tail` 可写、`stamp==head+1` 可读、`stamp+one_lap==tail+1` 疑似满；匹配成功后用 `compare_exchange_weak(SeqCst, Relaxed)` 抢占尾/头指针。
- 写值顺序是「先 CAS 占位 → 写值 → `Release` 发 stamp」，保证 stamp 推进时数据一定就绪；`pop` 读值后把 stamp 设为 `head+one_lap` 把槽交还下一圈。
- CAS 失败用 `Backoff::spin`（轻量、马上重试），等待 stamp 跟上用 `Backoff::snooze`（较重、可能让出 CPU）——分级退避。
- 「疑似满/空」都要先 `fence(SeqCst)` 再做权威二次加载（读 head/tail），避免生产者误判满与消费者误判空同时发生而丢数据。

## 7. 下一步学习建议

- 想了解 `force_push` 如何在满时同时推进 head 与 tail、以及 `len/is_empty/is_full` 的查询实现，继续 [u2-l3](u2-l3-arrayqueue-force-push-capacity.md)。
- 想看「单线程独占快路径」如何完全跳过这些原子操作，继续 [u2-l4](u2-l4-arrayqueue-exclusive-mut.md)。
- 想深入每处 `SeqCst/Acquire/Release/fence` 的精确 happens-before 论证、以及「生产者误判满 + 消费者误判空」为何不会丢数据，跳到 [u4-l1](u4-l1-atomic-orderings-fence.md)。
- 想理解 `pop` 里 `unsafe { ... read().assume_init() }` 为何安全、`Drop` 如何回收未读值，跳到 [u4-l3](u4-l3-unsafe-maybeuninit-safety.md)。
- 横向对照：[u3-l2](u3-l2-segqueue-push-pop.md) 会讲 `SegQueue` 的 push/pop，它用「块链表 + WRITE/READ/DESTROY 状态位」而非 stamp 编码，对照阅读能加深对无锁队列设计取舍的理解。
