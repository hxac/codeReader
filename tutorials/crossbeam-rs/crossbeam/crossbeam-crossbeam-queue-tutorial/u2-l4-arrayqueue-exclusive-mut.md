# 独占引用变体：push_mut 与 pop_mut

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么当函数签名是 `&mut self` 时，ArrayQueue 的入队/出队可以**完全跳过原子操作、CAS 循环、内存屏障与退避**。
- 理解 `AtomicUsize::get_mut()` 这个标准库方法的安全前提，以及它如何穿透 `CachePadded<AtomicUsize>` 的自动解引用链路。
- 逐行走读 `push_mut` 与 `pop_mut`，并能指出它们与 `push`/`pop` 共享了哪些算术、又省略了哪些并发机制。
- 独立复现 `tests/array_queue.rs` 中的 `exclusive_reference` 测试，并用真实值验证 FIFO 顺序与容量边界。

## 2. 前置知识

本讲假设你已经学过 **u2-l1（ArrayQueue 的数据结构：stamp、lap 与 Slot 模型）** 与 **u2-l2（push 与 pop 的无锁主链路）**。在进入正文前，先用三句话回顾两个关键直觉：

1. **`&self` 意味着「可能共享」**。`push(&self, ...)` / `pop(&self)` 的 `&self` 表明同一时刻可能有多个线程持有同一队列的共享引用。因此指针推进必须用 `compare_exchange_weak`（CAS）来仲裁竞争，stamp 的发布必须用 `Release`、读取必须用 `Acquire`，还要在「疑似满/空」时插入 `fence(SeqCst)` 做二次确认。
2. **`&mut self` 意味着「独占」**。Rust 的借用规则保证：只要存在一个 `&mut ArrayQueue`，就**不存在任何其它引用**（既没有 `&`，也没有第二个 `&mut`）。也就是说，编译期就能证明此刻没有别的线程在碰这个队列。
3. **stamp 编码不变**。`head`/`tail` 仍然是「低位 index + 高位 lap」打包成的单个 `usize`，`one_lap`、`Slot<T>` 模型、满/空判定公式都和并发版本一模一样。`push_mut`/`pop_mut` 只是把「如何读写这个 `usize`」从原子换成普通整数。

一句话概括本讲的核心论点：

> 并发版本里所有用于「和别人协调」的机制（CAS、内存序、fence、Backoff），在 `&mut self` 下都是纯粹的额外开销，因为需要协调的「别人」在编译期就被证明不存在。

> 关键术语：**独占引用（exclusive reference）**即 `&mut T`；**get_mut** 是 `AtomicUsize` 等原子类型提供的、返回底层 `&mut usize` 的安全方法；**DerefMut** 是 `CachePadded` 实现 的 trait，使 `.get_mut()` 能穿透包装直达内部原子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/array_queue.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | ArrayQueue 的全部实现，本讲聚焦其中的 `push_mut`、`pop_mut`，并与 `push_or_else`、`pop` 对照。 |
| [`tests/array_queue.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) | 集成测试，其中 `exclusive_reference` 测试是本讲代码实践的蓝本。 |
| [`../crossbeam-utils/src/cache_padded.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) | `CachePadded<T>` 的定义，关键是它实现了 `DerefMut<Target = T>`，让 `get_mut` 能穿透。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，建议按顺序读：先理解 `get_mut` 这把「钥匙」是怎么工作的（4.1），再看入队（4.2）和出队（4.3）如何用它，最后做一次与并发版本的逐项对照（4.4）。

### 4.1 独占引用如何绕过原子操作：get_mut 访问原子与槽

#### 4.1.1 概念说明

`AtomicUsize` 在运行期本质上就是包了一层的 `usize`，它的「原子性」来自专用的机器指令（如 `lock cmpxchg`）。当你持有 `&AtomicUsize`（共享引用）时，编译器无法排除别的线程也在操作它，所以你必须用 `load`/`store`/`compare_exchange` 这类原子方法。

但标准库为每个原子类型都提供了一个特殊方法：

```rust
// 标准库中 AtomicUsize 的方法（示意，非项目代码）
impl AtomicUsize {
    pub fn get_mut(&mut self) -> &mut usize { ... }
}
```

它的签名是 `&mut self`，返回普通的 `&mut usize`。为什么这是**安全**的？因为 `&mut self` 由 Rust 借用检查器背书：能拿到 `&mut AtomicUsize`，就说明这世上不存在任何其它能访问这个原子的引用。既然没有并发，「原子」就是多余的，直接当普通整数读写即可。`get_mut` 把「原子包装」临时拆开，露出里面那个普通的 `usize`。

ArrayQueue 里还有一个包装层：`head`/`tail` 的类型是 `CachePadded<AtomicUsize>`（用 `CachePadded` 填充缓存行以避免伪共享，详见 u4-l2）。要拿到最里层的 `usize`，需要穿过两层。`CachePadded` 通过实现 `Deref / DerefMut` 完成了这次「穿透」。

#### 4.1.2 核心流程

`*self.tail.get_mut()` 这一个表达式实际上经历了两层方法调用：

```text
self.tail                       :  CachePadded<AtomicUsize>
  .get_mut()                    →  CachePadded 没有 get_mut，但实现了 DerefMut<Target = AtomicUsize>
                                 →  方法解析自动 deref_mut 到 &mut AtomicUsize
                                 →  匹配到 AtomicUsize::get_mut(&mut self) -> &mut usize
*                               →  解引用得到 usize（Copy 类型）
```

注意 `CachePadded` **本身并没有** `get_mut` 方法——上面这行能编译，全靠 Rust 的**自动解引用方法解析**（auto-deref method resolution）：编译器发现 `CachePadded` 上没有 `get_mut`，但 `CachePadded: DerefMut<Target = AtomicUsize>`，于是把 `&mut CachePadded<AtomicUsize>` 自动转成 `&mut AtomicUsize`，再调用 `AtomicUsize::get_mut`。

同理，`Slot<T>` 的 `stamp` 字段类型直接就是 `AtomicUsize`（没有 `CachePadded`），所以 `slot.stamp.get_mut()` 一步到位返回 `&mut usize`。

#### 4.1.3 源码精读

先看 `CachePadded` 的 `DerefMut` 实现，确认它的 `Target` 就是内部值 `T`：

把可变引用透传给内部 `self.value`（[crossbeam-utils/src/cache_padded.rs:195-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L195-L199)）——这段代码就是 `self.tail.get_mut()` 能穿透到 `AtomicUsize` 的根因。

再看字段定义，确认 `head`/`tail` 的类型确实被 `CachePadded` 包裹（[src/array_queue.rs:59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L59) 与 [src/array_queue.rs:67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L67)）：

```rust
head: CachePadded<AtomicUsize>,
tail: CachePadded<AtomicUsize>,
```

而 `Slot.stamp` 没有 `CachePadded`（[src/array_queue.rs:18-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L18-L27)）：

```rust
struct Slot<T> {
    stamp: AtomicUsize,
    value: UnsafeCell<MaybeUninit<T>>,
}
```

`push_mut` 开头这两行就是本模块的「钥匙」（[src/array_queue.rs:232-233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L232-L233)），用 `get_mut` 一次性拿到两个普通的 `usize`：

```rust
let tail = *self.tail.get_mut();
let head = *self.head.get_mut();
```

这两行**没有任何原子指令**，就是普通的整数拷贝。它安全的前提，正是函数签名 `&mut self` 带来的独占保证。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`get_mut` 返回的是同一块内存」，从而建立「它不是 load 一份快照，而是直接读写底层存储」的直觉。

**操作步骤**（在一个临时 binary 或 `#[test]` 里）：

```rust
// 示例代码：验证 AtomicUsize::get_mut 直接操作底层存储
use std::sync::atomic::AtomicUsize;

let mut a = AtomicUsize::new(10);
// 用原子方式读
assert_eq!(a.load(std::sync::atomic::Ordering::Relaxed), 10);
// 用 get_mut 拿到普通可变引用并改写
*a.get_mut() = 99;
// 再用原子方式读，确认改写生效
assert_eq!(a.load(std::sync::atomic::Ordering::Relaxed), 99);
```

**需要观察的现象**：通过 `get_mut()` 写入的 `99`，立刻能被 `load()` 看到，证明 `get_mut` 操作的就是原子变量背后的那块 `usize` 内存，而不是副本。

**预期结果**：断言全部通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AtomicUsize::get_mut` 是安全方法（不带 `unsafe`），而直接通过裸指针读写原子变量是不安全的？

**参考答案**：`get_mut(&mut self)` 的安全保证来自 Rust 的借用检查器——`&mut self` 在编译期就排除了任何并发访问，因此「无需原子指令也能正确」这一结论可以静态成立，不需要程序员再写 `unsafe` 承诺。而裸指针绕过了借用检查，编译器无法保证独占，所以必须由程序员用 `unsafe` 担保。

**练习 2**：如果把 `self.tail.get_mut()` 改成 `self.tail.load(Ordering::Relaxed)`，程序还能通过类型检查吗？语义上会有什么差别？

**参考答案**：能通过类型检查（两者都得到 `usize`），但语义变了：`load` 只是读一份当前值的快照，你随后对它的修改不会写回队列；而 `*self.tail.get_mut() = new_tail` 是真正的写回。`push_mut` 必须写回 `new_tail`，所以只能用 `get_mut`。

---

### 4.2 push_mut：无 CAS、无 fence 的非原子入队

#### 4.2.1 概念说明

`push_mut` 是 `push` 的「单线程加速版」。当你的代码逻辑上确定此刻是**唯一**的入队者（典型场景：构造期批量灌数据、单线程批处理、测试夹具），用 `&mut self` 调 `push_mut` 可以省掉并发版本里全部的协调开销。

它的算法主体和 `push_or_else`（见 u2-l2）**几乎逐行相同**——同样的 index/lap 拆分、同样的 `new_tail` 计算、同样的「写值后把 stamp 设为 `tail + 1`」。差别只有三处：

1. 读 `tail`/`head` 用 `get_mut` 而非原子 `load`。
2. 满/未满的判定**只查一次** `head.wrapping_add(one_lap) == tail`，没有 stamp 提示 + 读 head 的两级判定，也没有 CAS 重试。
3. 推进 `tail` 与发布 `stamp` 都是普通整数赋值，没有 `compare_exchange_weak`、没有 `Ordering::Release`。

#### 4.2.2 核心流程

`push_mut` 的执行过程：

```text
1. tail = *self.tail.get_mut()      # 非原子读
   head = *self.head.get_mut()      # 非原子读
2. 若 head + one_lap == tail：        # 队列满，直接返回 Err(value)
       return Err(value)
3. index = tail & (one_lap - 1)      # 同 push_or_else 的拆分
   lap   = tail & !(one_lap - 1)
   new_tail = index+1 < cap ? tail+1 : lap.wrapping_add(one_lap)
4. *self.tail.get_mut() = new_tail    # 非原子推进 tail（无 CAS）
5. slot = buffer[index]
   slot.value.write(value)            # 写入值（与并发版相同）
6. *slot.stamp.get_mut() = tail + 1   # 非原子发布 stamp（无 Release）
7. return Ok(())
```

为什么第 2 步只查一次就敢直接返回 `Err`？因为在 `&mut self` 下不存在「另一个生产者恰好在这两次读之间把队列填满」的竞态——`head` 和 `tail` 在本函数执行期间**不会被任何人改动**，读到的值就是权威值。并发版的 `push` 之所以要重试，正是为了应对「读 head 之后、写 tail 之前，别人改了状态」这种竞态；这里该竞态不存在，重试也就不需要了。

#### 4.2.3 源码精读

完整的 `push_mut`（[src/array_queue.rs:231-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L231-L256)），逐段说明：

满判定 + 提前返回（[src/array_queue.rs:235-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L235-L237)）——这就是并发版里 `push` 闭包中那段满判定逻辑（`head.wrapping_add(self.one_lap) == tail`）的搬运，只是不再套在 CAS 循环里：

```rust
if head.wrapping_add(self.one_lap) == tail {
    return Err(value);
}
```

index/lap 拆分与 `new_tail` 计算（[src/array_queue.rs:239-245](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L239-L245)）——和 `push_or_else`（[src/array_queue.rs:139-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L139-L147)）逐字相同：

```rust
let index = tail & (self.one_lap - 1);
let lap = tail & !(self.one_lap - 1);
let new_tail = if index + 1 < self.capacity() {
    tail + 1
} else {
    lap.wrapping_add(self.one_lap)
};
```

推进 `tail`、写值、发布 stamp（[src/array_queue.rs:247-255](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L247-L255)）——这里三行赋值对应并发版的「CAS 占位 → 写值 → Release 发 stamp」三连，但全部退化为普通写：

```rust
*self.tail.get_mut() = new_tail;                       // 对比 push 的 compare_exchange_weak(SeqCst)
let slot = unsafe { self.buffer.get_unchecked_mut(index) };
unsafe {
    slot.value.get().write(MaybeUninit::new(value));   // 写值，与并发版相同
}
*slot.stamp.get_mut() = tail + 1;                       // 对比 push 的 store(Release)
Ok(())
```

> 注意第 249 行用了 `get_unchecked_mut(index)` 而不是 `self.buffer[index]`：这是省略一次边界检查的微优化，安全性来自 `index` 由 `tail & (one_lap-1)` 计算且必小于 `capacity`（与并发版 `push_or_else` 第 151 行的 `get_unchecked` 同理）。

#### 4.2.4 代码实践

**实践目标**：用真实值验证 `push_mut` 的 FIFO 顺序与「满则返回 Err」的语义。

**操作步骤**：

```rust
// 示例代码：push_mut 的顺序与边界
use crossbeam_queue::ArrayQueue;

let mut q = ArrayQueue::new(2);
assert_eq!(q.push_mut(10), Ok(()));   // [10]
assert_eq!(q.push_mut(20), Ok(()));   // [10, 20]
assert_eq!(q.push_mut(30), Err(30));  // 满了，原值 30 退还
assert!(q.is_full());
```

**需要观察的现象**：第三次 `push_mut(30)` 返回 `Err(30)`，**原值被完整退还**（和并发版 `push` 一样是 `Result<(), T>`）。`is_full()` 返回 `true`。

**预期结果**：全部断言通过。

#### 4.2.5 小练习与答案

**练习 1**：`push_mut` 里没有 `loop {}`，但并发版 `push_or_else` 是 `loop { ... }`。为什么 `push_mut` 不需要循环？

**参考答案**：并发版的循环是为了在两种情况下重试——(a) CAS 抢占 `tail` 失败（被别的生产者抢了），(b) stamp 还没跟上（别的线程正在写槽，需要 `snooze` 等待）。`push_mut` 没有「别的线程」，`tail` 直接赋值成功，stamp 也不会有别人改动，所以一次直线走完即可，无需循环。

**练习 2**：第 253 行 `*slot.stamp.get_mut() = tail + 1` 没有 `Ordering::Release`。这会不会导致后续 `pop_mut` 读不到正确的 stamp？

**参考答案**：不会。`Release` 序的作用是「把当前线程此前的写操作，对**另一个线程**的 `Acquire` 读可见」。`push_mut` 与 `pop_mut` 通常由同一线程在 `&mut self` 生命周期内先后调用，不存在跨线程的可见性问题；即便穿插 `&self` 的 `pop`，也是在 `push_mut` 的 `&mut` 借用结束之后（借用检查器保证不重叠），那时写操作早已对全部线程可见。所以无需 `Release`。

---

### 4.3 pop_mut：无 CAS、无 fence 的非原子出队

#### 4.3.1 概念说明

`pop_mut` 是 `pop` 的单线程加速版，思路与 `push_mut` 完全对称。出队关心的是「队列是否为空」，空判定公式是 `tail == head`（与并发版 `pop` 中的空判定一致）。由于 `&mut self` 保证没有别的消费者在抢 `head`，空判定只需查一次，读取值后直接推进 `head`，全程不用 CAS、不用 `fence`。

#### 4.3.2 核心流程

```text
1. head = *self.head.get_mut()      # 非原子读
   tail = *self.tail.get_mut()      # 非原子读
2. 若 tail == head：                  # 空，返回 None
       return None
3. index = head & (one_lap - 1)
   lap   = head & !(one_lap - 1)
   new   = index+1 < cap ? head+1 : lap.wrapping_add(one_lap)
4. slot = buffer[index]
   msg  = slot.value.read().assume_init()   # 读出值（与并发版相同）
5. *slot.stamp.get_mut() = head + one_lap    # 把槽「还给」下一圈（非原子）
   *self.head.get_mut()  = new               # 推进 head（无 CAS）
6. return Some(msg)
```

第 5 步把 stamp 设成 `head + one_lap`，含义是「这个槽要等到下一圈、head 转回来加 1 时才能再被读」，这与并发版 `pop`（[src/array_queue.rs:354-355](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L354-L355)）写入的值完全一致——这是 stamp 状态机的核心约定，独占版绝不改动它。

#### 4.3.3 源码精读

完整的 `pop_mut`（[src/array_queue.rs:397-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L397-L427)）。关键三段：

空判定（[src/array_queue.rs:402-404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L402-L404)）——对比并发版 `pop` 的空判定（`stamp == head` 提示 + `fence(SeqCst)` + 重读 `tail` 确认，见 [src/array_queue.rs:363-370](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L363-L370)），这里只有一个直白的整数比较：

```rust
if tail == head {
    return None;
}
```

读值（[src/array_queue.rs:423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L423)）——与并发版 `pop` 第 353 行**字节级相同**：

```rust
let msg = unsafe { slot.value.get().read().assume_init() };
```

发布 stamp + 推进 head（[src/array_queue.rs:424-425](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L424-L425)）——对比并发版的 `store(head + one_lap, Release)` + CAS 推进 head，这里是两次普通赋值：

```rust
*slot.stamp.get_mut() = head.wrapping_add(self.one_lap);
*self.head.get_mut() = new;
```

#### 4.3.4 代码实践

**实践目标**：验证 `pop_mut` 的空返回 `None`，以及非空时按 FIFO 弹出。

**操作步骤**：

```rust
// 示例代码：pop_mut 的空与 FIFO
use crossbeam_queue::ArrayQueue;

let mut q = ArrayQueue::new(3);
assert!(q.pop_mut().is_none());        // 空队列
q.push_mut('a').unwrap();
q.push_mut('b').unwrap();
assert_eq!(q.pop_mut(), Some('a'));    // FIFO：先入先出
assert_eq!(q.pop_mut(), Some('b'));
assert!(q.pop_mut().is_none());        // 再次空
```

**需要观察的现象**：弹出顺序严格是 `'a'` 然后 `'b'`（单线程下顺序确定，不像多生产者那样无全局序）；空队列返回 `None` 而非 panic。

**预期结果**：全部断言通过。

**待本地验证**：若你在 release 下用 `cargo test --release` 跑上述断言，行为应与 debug 一致；如不一致请回报。

#### 4.3.5 小练习与答案

**练习 1**：并发版 `pop` 在「疑似空」时会先 `fence(SeqCst)` 再读 `tail`（[src/array_queue.rs:364-365](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L364-L365)）。`pop_mut` 为什么省掉了这一步？

**参考答案**：`fence` 的作用是强制一个全局总序，防止「读 stamp 看到 head（疑似空）」与「生产者刚 push 完但 stamp 的 Release 还没对消费者可见」之间出现乱序误判。`pop_mut` 持有 `&mut self`，生产者要么就是我自己（同一线程，程序序天然有序），要么其 `&mut` 借用已经结束（写操作早已全局可见），不存在需要 `fence` 去消除的乱序，因此省略。

**练习 2**：`pop_mut` 里 `assume_init()` 这次 `unsafe` 调用的安全性依据是什么？

**参考答案**：能执行到第 423 行，说明 `tail != head`，即 `head` 指向的槽位于 `[head, tail)` 这个已入队区间内，该槽的值已被某个 `push`/`push_mut` 写入过且尚未被弹出（stamp 状态正确）。加上 `&mut self` 保证没有并发读取，这次 `read().assume_init()` 一定读到已初始化的值，且不会被重复读取。这与并发版 `pop` 的 unsafe 论证角度不同（并发版靠 stamp 状态机 + CAS 独占槽，独占版靠借用检查器），但结论一致。

---

### 4.4 与并发 push/pop 的对照：哪些被省略，为什么仍然安全

#### 4.4.1 概念说明

把 4.2、4.3 的差异集中到一张表，能看得最清楚。核心命题是：**被省略的每一项，都只服务于「与其它线程协调」这一个目的；既然 `&mut self` 已经证明没有其它线程，省略就是安全且无损语义的。**

注意一个常被忽视的点：`push_mut`/`pop_mut` 与 `push`/`pop` 在**语义上完全等价**（满返回 `Err`、空返回 `None`、FIFO 顺序、stamp 推进规则）。你可以放心地在同一段代码里混用——只要 `&mut` 借用与 `&` 借用不重叠（编译器会替你检查）。`tests/array_queue.rs` 的 `exclusive_reference` 测试正是这么做的：先 `push_mut` 两次，再 `pop_mut` 一次，最后用共享 `pop()` 收尾。

#### 4.4.2 核心流程

下表逐项对照 `push`（即 `push_or_else` 内部主链路）与 `push_mut`；`pop` 与 `pop_mut` 的对照结构完全对称，不再单独列表。

| 维度 | `push`（`&self`，并发） | `push_mut`（`&mut self`，独占） | 省略理由 |
| --- | --- | --- | --- |
| 读 tail/head | `self.tail.load(Relaxed)` | `*self.tail.get_mut()` | 无并发，无需原子读 |
| 满/未满判定 | stamp 提示 + CAS 循环 + 读 head 二级确认 | 单次 `head + one_lap == tail` | 无竞态，一次判定即权威 |
| 推进 tail | `compare_exchange_weak(SeqCst, Relaxed)` | `*self.tail.get_mut() = new_tail` | 无竞争者，无需仲裁 |
| 写值 | `slot.value.get().write(...)` | **相同** | — |
| 发布 stamp | `slot.stamp.store(tail+1, Release)` | `*slot.stamp.get_mut() = tail + 1` | 无跨线程可见性需求 |
| 失败退避 | `Backoff::spin` / `snooze` + 重读 tail | 无 | 不会失败，一次成功 |
| fence | `fence(SeqCst)` 二次确认（[L177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L177)） | 无 | 无需全局总序 |

**安全性论证（为什么省了仍然正确）**：

- **可见性**：`Release`/`Acquire` 序是为了让「写线程的写」对「读线程的读」可见。在 `&mut self` 下，写者和读者要么是同一线程（程序序保证可见），要么借用不重叠（前一个借用结束时的所有写已对全部线程可见）。所以不需要内存序注解。
- **原子性**：CAS 是为了把「读-改-写」做成不可分割的原子操作，避免多个生产者同时改 `tail`。独占下只有一个写者，普通赋值天然不可分割。
- **活性（liveness）**：并发版可能因竞争反复失败而需要 `Backoff` 退避并重试。独占版不会失败，不需要退避。

#### 4.4.3 源码精读

并发版 `push_or_else` 的 CAS 主循环（[src/array_queue.rs:134-186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L134-L186)）——把这一整段 loop 与 `push_mut` 的直线代码（4.2.3）摆在一起看，差异最直观。其中 CAS 抢占 `tail`（[src/array_queue.rs:157-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L157-L162)）：

```rust
match self.tail.compare_exchange_weak(
    tail, new_tail, Ordering::SeqCst, Ordering::Relaxed,
) {
    Ok(_) => { /* 写值 + Release 发 stamp */ }
    Err(t) => { tail = t; backoff.spin(); }   // 抢失败，退避重来
}
```

对比 `push_mut` 的等价步骤（[src/array_queue.rs:247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L247)）就是一行普通赋值，没有 `Ok`/`Err` 分支、没有 `backoff`。

官方对「独占引用下省略原子操作」的文档说明，见 `push_mut` 的 doc 注释（[src/array_queue.rs:217-220](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L217-L220)）：

```rust
/// Attempts to push an element using an exclusive reference of the queue.
///
/// Atomic operations and checks are omitted
```

`pop_mut` 的 doc 注释同样点明（[src/array_queue.rs:382-385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L382-L385)）：「Due to having an exclusive reference, atomic operations and checks are omitted」。

#### 4.4.4 代码实践

**实践目标**：亲手验证「`push_mut`/`pop_mut` 与 `push`/`pop` 语义等价、可混用」，这是 `exclusive_reference` 测试的核心断言。

**操作步骤**：在仓库 `crossbeam-queue/tests/` 下新建一个测试文件（例如 `examples/exclusive_demo.rs`，或直接加到你自己的临时项目里），复现并扩展官方测试：

```rust
// 示例代码：混用独占与共享接口，验证状态切换
use crossbeam_queue::ArrayQueue;

let mut q = ArrayQueue::new(2);

assert_eq!(q.len(), 0);
assert!(q.is_empty());

q.push_mut(1).unwrap();            // 独占入队
assert_eq!(q.len(), 1);
assert!(!q.is_empty());
assert!(!q.is_full());

q.push_mut(2).unwrap();            // 独占入队，填满
assert_eq!(q.len(), 2);
assert!(q.is_full());

assert_eq!(q.pop_mut(), Some(1));  // 独占出队
assert_eq!(q.len(), 1);
assert!(!q.is_full());

assert_eq!(q.pop(), Some(2));      // 切回共享出队，仍能正确读出
assert_eq!(q.len(), 0);
assert!(q.is_empty());
```

**需要观察的现象**：
1. `push_mut` 两次后 `is_full()` 为真、`len()` 为 2；
2. `pop_mut` 弹出的是**第一个**入队的 `1`（FIFO）；
3. 最后用共享 `q.pop()` 弹出 `2`，证明独占写入对后续共享读取完全可见，两种接口的数据视图一致。

**预期结果**：全部断言通过。这与官方 `exclusive_reference` 测试（[tests/array_queue.rs:59-87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L59-L87)）的行为一致（官方测试用 `()` 占位值，上面用真实值 `1`/`2` 额外验证了顺序）。

> 提示：若你直接在仓库内运行，可用 `cargo test -p crossbeam-queue exclusive_reference` 单跑官方那个测试作为对照基准。

#### 4.4.5 小练习与答案

**练习 1**：能否在多线程里用 `Arc<Mutex<ArrayQueue<T>>>` 包一层后，对被锁住的队列调用 `push_mut`？这样做有什么意义？

**参考答案**：可以。`Mutex::lock()` 返回的 `MutexGuard` 提供 `DerefMut`，能拿到 `&mut ArrayQueue`，从而调用 `push_mut`。意义在于：当你本就需要一把外部锁来协调多个操作成「事务」时，锁内的 `push_mut`/`pop_mut` 比锁内的 `push`/`pop` 省掉了无谓的原子操作（反正锁已经保证了互斥）。不过此时你付出的主要是锁的开销，收益有限；ArrayQueue 设计为无锁共享才是它的主场。

**练习 2**：API 里只有 `push_mut`/`pop_mut`，**没有** `force_push_mut`。如果你想在独占引用下做「满则覆盖最旧元素」的环形缓冲，该怎么办？

**参考答案**：没有现成的 `force_push_mut`。可以在独占引用下手动模拟：先用 `pop_mut()` 取出最旧元素（若满），再 `push_mut(value)`。由于是独占引用，这两步不会被并发打断，等价于一次原子的 `force_push`。或者干脆退而用共享 `force_push(&self)`——它语义正确，只是多花了原子操作的开销。这是一个值得在源码里搜索确认「确实没有该变体」的练习（grep `force_push_mut` 应无结果）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个小任务：**实现一个「单线程批量灌入 + 批量排空」的微型基准夹具，并验证它与等价的 `push`/`pop` 序列产生完全相同的结果。**

要求：

1. 新建 `ArrayQueue::new(8)`，用一个 `for` 循环 `push_mut(0..8)` 灌满；中途断言第 9 次 `push_mut` 返回 `Err`，且灌满后 `is_full()` 为真、`len()` 为 8。
2. 用 `pop_mut()` 循环排空，收集到一个 `Vec`，断言它恰好等于 `vec![0, 1, 2, 3, 4, 5, 6, 7]`（验证 FIFO），排空后 `is_empty()` 为真。
3. 重复同样的「灌入 0..8、排空成 Vec」流程，但全部改用共享接口 `push`/`pop`，断言得到的 Vec 与第 2 步**完全相同**——以此证明两种路径语义等价。
4.（选做）用 `std::time::Instant` 粗测两种路径各跑十万轮的总耗时，观察独占版是否更快（结果受机器与编译参数影响，**待本地验证**，不要预设结论）。

参考骨架（**示例代码**，请自行补全断言）：

```rust
// 示例代码：综合实践骨架
use crossbeam_queue::ArrayQueue;

fn fill_drain_mut() -> Vec<i32> {
    let mut q = ArrayQueue::new(8);
    for v in 0..8 { assert_eq!(q.push_mut(v), Ok(())); }
    assert_eq!(q.push_mut(99), Err(99));   // 第 9 个必失败
    let mut out = Vec::new();
    while let Some(x) = q.pop_mut() { out.push(x); }
    out
}

fn fill_drain_shared() -> Vec<i32> {
    let q = ArrayQueue::new(8);
    for v in 0..8 { assert_eq!(q.push(v), Ok(())); }
    let mut out = Vec::new();
    while let Some(x) = q.pop() { out.push(x); }
    out
}

fn main() {
    let a = fill_drain_mut();
    let b = fill_drain_shared();
    assert_eq!(a, b);
    assert_eq!(a, (0..8).collect::<Vec<_>>());
    println!("两条路径结果一致: {:?}", a);
}
```

完成后再回头读一遍 `src/array_queue.rs` 中 `push_mut`/`pop_mut` 与 `push_or_else`/`pop` 的源码，确认你能在脑中把每一行非原子写法对应回并发版的原子写法。

## 6. 本讲小结

- `push_mut(&mut self, ...)` 与 `pop_mut(&mut self)` 是 `push`/`pop` 的单线程加速版，**语义完全等价**（满返回 `Err`、空返回 `None`、FIFO 不变）。
- 它们通过 `AtomicUsize::get_mut()` 拿到底层 `&mut usize`，把原子读写退化为普通整数读写；`CachePadded` 的 `DerefMut` 让 `self.tail.get_mut()` 自动穿透到内部 `AtomicUsize`。
- 相比并发版，独占版省掉了：CAS 循环、`Backoff` 退避、`Acquire`/`Release`/`SeqCst` 内存序、`fence(SeqCst)` 二次确认；这些机制全部只服务于「与其它线程协调」，而 `&mut self` 在编译期就排除了其它线程。
- index/lap 拆分、`new_tail`/`new` 计算、写值、stamp 推进规则与并发版**逐字相同**——被简化的只是「如何读写指针与 stamp」，不是算法本身。
- 独占接口与共享接口可安全混用：只要 `&mut` 借用与 `&` 借用不重叠（借用检查器保证），独占期的所有写对后续共享读完全可见。
- 官方 `exclusive_reference` 测试（`tests/array_queue.rs`）正是这类混用的活样本，是本讲代码实践的蓝本。

## 7. 下一步学习建议

- 本讲只解释了「为什么独占版可以省原子操作」，但**没有**深究并发版每一处 `Ordering` 的精确取舍。若想搞清楚 `push_or_else` 里 `SeqCst`/`Acquire`/`Release` 各自不可替代的原因，请继续学习 **u4-l1（原子内存序与 fence 的运用）**。
- `CachePadded` 在本讲里只是「让 `get_mut` 穿透」的包装层；它真正的性能意义是避免伪共享，详见 **u4-l2（CachePadded 与 Backoff）**。
- `pop_mut` 里 `read().assume_init()` 的 unsafe 论证在这里只点了结论；完整的 `unsafe impl Send/Sync` 与 `MaybeUninit` 安全性论证见 **u4-l3（unsafe 的安全性论证与 MaybeUninit）**。
- 想看 ArrayQueue 的「姊妹」无界队列如何用同样的 slot 状态位思想实现块链表回收，进入第三单元 **u3-l1（SegQueue 的分块链表结构）**。
