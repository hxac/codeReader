# SegQueue 的 len、Drop 与迭代器

## 1. 本讲目标

本讲承接 u3-l2（`push`/`pop` 主链路）与 u3-l3（`Block` 内存回收），把目光从「单块的入队/出队」移到「整条块链表的全局视图」。

学完后你应该能够：

- 说出 `SegQueue::len()` 为什么不能简单地用 `tail - head`，并能手算它在跨块场景下的返回值。
- 解释 `len()` 中「块末尾修正」与「按块扣除」两步分别解决什么问题。
- 看懂 `Drop` 如何用一段 `while` 循环同时完成「drop 剩余值」与「释放剩余块」两件事，并理解它为何不需要并发协调。
- 读懂 `IntoIter` 如何把队列「消费式」地逐个读出、边读边释放块，以及它和 `Drop` 的分工。
- 复述 0.3.12 修复的「栈溢出」问题的来龙去脉，并理解 `Block::new` 改用堆直接分配的原因。

## 2. 前置知识

本讲假设你已经掌握 u3-l1 与 u3-l2 中关于 `index` 位编码的内容。这里做最简回顾：

全局 `index` 是一个 `usize`，被切成三段（常量见 [seg_queue.rs:25-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L25-L32)）：

| 比特位 | 含义 | 说明 |
|---|---|---|
| bit 0 | `HAS_NEXT` 元数据位 | 缓存「是否还有下一块」，`SHIFT=1` 即最低 1 位保留 |
| bit 1–5 | 块内偏移 `offset` | `(index >> 1) % 32`，取值 0..=31 |
| bit 6+ | 圈号 `lap`（块号） | `(index >> 1) / 32` |

关键常量：

- `LAP = 32`：一块覆盖 32 个「逻辑位置」。
- `BLOCK_CAP = LAP - 1 = 31`：每块真正存数据的槽是 31 个（offset 0..=30）。
- offset 31 是**缝合位（seam）**：它不存数据，只是「块满，该换下一块」的瞬态信号。

我们把 `index >> SHIFT`（去掉 `HAS_NEXT` 位、保留 offset+lap）称为该位置的**逻辑位置** \(L\)，即

\[
L = \text{offset} + \text{lap} \times \text{LAP}, \quad \text{LAP}=32.
\]

在逻辑位置坐标下，块 0 占据 \(L=0\ldots31\)（其中 \(L=0\ldots30\) 是数据，\(L=31\) 是缝合位），块 1 占据 \(L=32\ldots63\)，依此类推。缝合位出现在每个块的末尾：\(L=31,63,95,\ldots\)，即 \(L = k\cdot\text{LAP}-1\)。

> 小贴士：本讲的难点几乎全在于「缝合位」——它不存数据，却占了一个逻辑位置，所以计数时必须把它扣除。`len()`、`Drop`、`IntoIter` 三处都在各自的方式上处理缝合位。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 本讲涉及的内容 |
|---|---|
| [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) | `is_empty`、`len`、`Drop`、`IntoIterator`/`IntoIter`、以及 `Block::new`（为 0.3.12 修复做铺垫） |
| [tests/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs) | `len`、`into_iter`、`into_iter_drop`、`drops`、`stack_overflow` 等测试 |

一个贯穿全讲的对比：这三个函数的「并发程度」递减——

- `len(&self)`：共享引用，**可能**有其他线程并发 push/pop，所以需要「一致性快照」循环。
- `Drop::drop(&mut self)`：独占引用，编译期排除并发，所以单次读取即权威。
- `IntoIter::next(&mut self)`：同样独占，可以像 `pop_mut` 一样完全跳过原子操作。

理解这个对比，就理解了三者写法差异的根因。

---

## 4. 核心概念与源码讲解

### 4.1 len 的跨块索引算术与块末尾修正

#### 4.1.1 概念说明

对一个普通数组队列，`len()` 似乎只需 `tail - head`。但 `SegQueue` 有两个「坑」：

1. **缝合位占位**：每个块的 offset 31 不存数据，却占了一个逻辑位置。跨块时这些缝合位会被计入 `tail - head`，必须扣除。
2. **并发快照不一致**：`len()` 拿的是 `&self`，读 `head` 和 `tail` 之间，其他线程可能改了它们，导致读到的 `(head, tail)` 从未真实同时存在过。

`len()` 用两招分别对付这两个坑：

- 用**seqlock 风格的一致性快照**对付并发（与 u2-l3 中 `ArrayQueue::len` 完全同构）。
- 用「块末尾修正 + 按块扣除缝合位」的算术对付缝合位。

#### 4.1.2 核心流程

`len()` 的算法可以概括为五步：

```text
loop:
  1. 读 tail(SeqCst) → 读 head(SeqCst) → 再读一次 tail(SeqCst)
     若第二次 tail 与第一次不同：重来（一致性快照）
  2. 抹掉最低位（HAS_NEXT）：head &= !1, tail &= !1
  3. 块末尾修正：若某指针正落在缝合位(offset==31)，把它推进一格进入下一块
  4. 归一化：以 head 的块号为基准，把 head/tail 平移到「head 在第 0 块」
  5. 右移 SHIFT 得到纯逻辑位置 H、T，返回  T - H - T / LAP
```

最终的计数公式（其中 \(H,T\) 为归一化后的逻辑位置）：

\[
\text{len} = (T - H) - \left\lfloor \frac{T}{\text{LAP}} \right\rfloor.
\]

直觉解释：

- \(T - H\) 是 head 到 tail 之间**所有**逻辑位置数（既包含数据槽，也包含缝合位）。
- \(\lfloor T/\text{LAP} \rfloor\) 是区间 \([0, T)\) 内缝合位的个数（每满一个块就有一个缝合位）。
- 由于归一化后 \(H \le 30 < 31\)，第一个缝合位（\(L=31\)）必然 \(\ge H\)，所以 \([H, T)\) 内的缝合位数就等于 \([0, T)\) 内的缝合位数 \(\lfloor T/\text{LAP} \rfloor\)。
- 两者相减，剩下的就是真正的元素数。

#### 4.1.3 源码精读

整个函数见 [seg_queue.rs:563-596](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L563-L596)。

**第一步：一致性快照循环**（[seg_queue.rs:564-567](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L564-L567)、[seg_queue.rs:570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L570)）：

```rust
let mut tail = self.tail.index.load(Ordering::SeqCst);
let mut head = self.head.index.load(Ordering::SeqCst);
// ...
if self.tail.index.load(Ordering::SeqCst) == tail {
    // 用这组 (tail, head) 继续计算
}
// 否则 loop 回到开头重读
```

这段读两次 `tail`、夹住一次 `head`：如果两次 `tail` 相同，就认为读 `head` 的那一刻没有生产者正在改 `tail`，得到的 `(tail, head)` 是一组「真实存在过」的组合。这正是 seqlock 的读端模式（u2-l3 中 `ArrayQueue::len` 已详述）。全部用 `SeqCst` 是为了在多生产者多消费者下拿到一个强一致快照；精确的内存序论证留到 u4-l1。

**第二步：抹掉 HAS_NEXT 位**（[seg_queue.rs:572-573](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L572-L573)）：

```rust
tail &= !((1 << SHIFT) - 1);   // &= !1，清掉 bit0
head &= !((1 << SHIFT) - 1);
```

此后 `head`/`tail` 只剩「offset + lap」信息。

**第三步：块末尾修正**（[seg_queue.rs:575-581](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L575-L581)）：

```rust
if (tail >> SHIFT) & (LAP - 1) == LAP - 1 {   // offset == 31？
    tail = tail.wrapping_add(1 << SHIFT);     // 推进一格，进入下一块
}
if (head >> SHIFT) & (LAP - 1) == LAP - 1 {
    head = head.wrapping_add(1 << SHIFT);
}
```

`(x >> SHIFT) & (LAP-1)` 就是 offset。若指针正落在缝合位（offset 31），就把它推进一格。**因为缝合位不存数据**，「停在缝合位」在语义上等价于「已经到了下一块的 offset 0」，所以计数时直接当作下一块处理。

**第四步：归一化**（[seg_queue.rs:583-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L583-L586)）：

```rust
let lap = (head >> SHIFT) / LAP;                       // head 所在块号
tail = tail.wrapping_sub((lap * LAP) << SHIFT);        // 平移
head = head.wrapping_sub((lap * LAP) << SHIFT);        // 使 head 落在第 0 块
```

把 head/tail 同时减去「head 块号 × 一块的逻辑长度」，相当于把坐标系平移到「head 在第 0 块」。两者之差（跨度）不变，但 head 被规范到一个已知位置，方便后面统一数缝合位。

**第五步：右移并套用公式**（[seg_queue.rs:588-593](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L588-L593)）：

```rust
tail >>= SHIFT;
head >>= SHIFT;
return tail - head - tail / LAP;
```

右移 SHIFT 得到纯逻辑位置 \(T, H\)，然后返回 \(T - H - \lfloor T/\text{LAP} \rfloor\)。

> 旁注：`is_empty` 是 `len` 的「廉价版」——它不做计数，只比较 head/tail 的逻辑位置是否相等（[seg_queue.rs:541-545](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L541-L545)）。注意它**没有**一致性快照循环，因为「相等」是一个相对稳定的判断（一旦不等就几乎不会再变相等，除非绕回，而那需要海量操作）；它返回的也只是一个瞬时快照。

#### 4.1.4 代码实践

**实践目标**：用手算验证 `len()` 的缝合位修正是否正确。

**操作步骤**：

1. 阅读现有测试 [tests/seg_queue.rs:35-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L35-L52) 中的 `len` 测试，它断言「连续 push 50 个、再连续 pop 50 个」过程中每一步 `len()` 都正确。
2. 在仓库内运行该测试：`cargo test -p crossbeam-queue --test seg_queue len`。
3. **手算两个关键场景**（这是本实践的内核）：
   - **场景 A（恰好填满一块）**：push 31 个元素。此时 tail 停在 offset 31（缝合位！）。
     - 块末尾修正把 tail 从逻辑 31 推进到逻辑 32；head 仍在逻辑 0。
     - 公式：\(T-H-\lfloor T/32\rfloor = 32 - 0 - 1 = 31\)。✓
   - **场景 B（跨两块）**：push 50 个元素。block 0 装满 31 个，block 1 装 19 个，tail 停在 block 1 的 offset 19，即逻辑 \(32+19=51\)。
     - 修正：tail 的 offset 是 19，不是缝合位，不修正；head 不动。
     - 归一化后 \(H=0, T=51\)。
     - 公式：\(51 - 0 - \lfloor 51/32\rfloor = 51 - 0 - 1 = 50\)。✓

**需要观察的现象**：`cargo test` 通过；手算的两个场景都得到 31 与 50，与 `len()` 实际返回一致。

**预期结果**：测试通过，手算与实现吻合。若你手算时忘记「块末尾修正」，场景 A 会得到 30（漏算），这正是该步骤存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：第三步「块末尾修正」的核心作用是什么？如果删掉它，计数公式 \(\text{len}=T-H-\lfloor T/\text{LAP}\rfloor\) 在什么前提下才会成立？

**答案**：块末尾修正的核心作用是**保证参与计数的 head/tail 永远不落在缝合位（offset 31）上**。这一点之所以关键，是因为计数公式 \(\text{len}=T-H-\lfloor T/\text{LAP}\rfloor\) 依赖一个化简：「\([H,T)\) 内的缝合位数等于 \(\lfloor T/\text{LAP}\rfloor\)」，而该化简又依赖前提 \(H \le 30\)（即 head 在某块的数据区、而非缝合位）。典型危险情形是消费者刚把一块消费空、head 短暂落在 offset 31 的缝合位上：若不修正，归一化会以「缝合位所在块号」为基准，使 \(H\) 落在缝合位、破坏 \(H \le 30\) 的前提，扣除项失准，最终漏数或多数一整块（31 个）。所以块末尾修正不是为了纠正「tail 在缝合位」这单一情形的数字，而是为整个计数公式守住「head/tail 必在数据区」的不变量。

**练习 2**：`is_empty` 为什么可以省掉一致性快照循环，而 `len` 不能？

**答案**：`is_empty` 只判断「head 与 tail 的逻辑位置是否相等」，这是一个偏向保守的瞬时判断；而 `len` 要对 (head, tail) 做**算术运算**（相减、相除），若两者不是同一时刻的组合，算出的数字可能毫无意义（甚至下溢）。因此 `len` 必须用 seqlock 保证读到的 (tail, head) 是真实共存过的，`is_empty` 则不必。

---

### 4.2 Drop 遍历 drop 值并释放块

#### 4.2.1 概念说明

当 `SegQueue<T>` 被销毁时，可能还残留两类资源：

1. **尚未被消费的值**：队列里 [head, tail) 区间的元素，它们是已初始化的 `T`，必须被 `drop`。
2. **尚未被释放的块**：pop 在消费完一块时会用 `Block::destroy` 释放它（见 u3-l3），所以「已经消费空」的块早已归还；但 **head 当前所在的块及之后的所有块**（即还含有数据、或含有 tail 的块）仍然占着堆内存，必须由 `Drop` 释放。

`Drop` 要在一段循环里同时完成这两件事。

关键前提：`Drop::drop(&mut self)` 持有**独占引用**。这意味着：

- 编译期就排除了其他线程，**无需任何原子操作、CAS、Backoff、DESTROY 协调**。
- 单次读取 head/tail 即权威，**不需要 `len()` 那种一致性快照循环**。
- 释放块可以直接 `Box::from_raw`，等价于 u3-l3 里的 `destroy_mut`。

#### 4.2.2 核心流程

```text
读 head、tail、block（都用 get_mut，非原子）
抹掉 head/tail 的 HAS_NEXT 位
while head != tail:
    offset = (head >> 1) % 32
    if offset < BLOCK_CAP:              # 0..=30：数据槽
        drop 掉该槽的值（assume_init_drop）
    else:                                # offset == 31：缝合位
        释放当前块（Box::from_raw），block = block.next
    head += 2                            # 前进一个逻辑位置
# 循环结束后，释放最后剩下的那个块（含 tail 的块）
if block 非空: drop(Box::from_raw(block))
```

注意循环每走一格 `head += 1<<SHIFT`（= 原始 +2，即一个逻辑位置）。遇到数据槽就 drop 值，遇到缝合位就释放整块并切到下一块——和 `len()` 一样，缝合位是「换块信号」。

#### 4.2.3 源码精读

整个 `Drop` impl 见 [seg_queue.rs:599-634](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L599-L634)。

**独占地读取三个字段**（[seg_queue.rs:601-607](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L601-L607)）：

```rust
let mut head = *self.head.index.get_mut();
let mut tail = *self.tail.index.get_mut();
let mut block = *self.head.block.get_mut();

head &= !((1 << SHIFT) - 1);   // 抹掉 HAS_NEXT
tail &= !((1 << SHIFT) - 1);
```

`get_mut()` 把 `AtomicUsize`/`AtomicPtr` 退化成普通 `&mut usize`/`&mut *mut`（u2-l4 已介绍这把「钥匙」），这里完全没有原子开销。

**主循环：数据槽 drop 值 / 缝合位释放块**（[seg_queue.rs:611-626](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L611-L626)）：

```rust
while head != tail {
    let offset = (head >> SHIFT) % LAP;
    if offset < BLOCK_CAP {
        // 数据槽：drop 掉里面的值
        let slot = (*block).slots.get_unchecked(offset);
        (*slot.value.get()).assume_init_drop();
    } else {
        // 缝合位：释放整块，切到下一块
        let next = *(*block).next.get_mut();
        drop(Box::from_raw(block));
        block = next;
    }
    head = head.wrapping_add(1 << SHIFT);   // 前进一个逻辑位置
}
```

`assume_init_drop()` 是 `MaybeUninit<T>` 的方法，原地 drop 掉里面的 `T`。它的 SAFETY 前提是：**该槽确实持有一个已初始化的 `T`**。这一点由队列不变量保证——`[head, tail)` 区间内的槽都是「已 push 写入、尚未 pop 读出」的，必然已初始化。详细的 unsafe 论证见 u4-l3。

缝合位分支里，`*(*block).next.get_mut()` 取出下一块指针，`Box::from_raw(block)` 把当前块归还堆，然后 `block = next` 继续往后走。注意：缝合位**不调用 `assume_init_drop`**，因为它本来就不存值。

**收尾：释放最后一块**（[seg_queue.rs:629-631](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L629-L631)）：

```rust
if !block.is_null() {
    drop(Box::from_raw(block));
}
```

循环在 `head == tail` 时退出，此时 `block` 指向 tail 所在的那一块（它仍持有堆内存，但其 [head, tail) 内已无数据需要 drop）。这块必须单独释放。

> 为什么可能为 `null`？如果队列从没被 push 过任何元素（空队列直接 drop），`head.block` 一开始就是 `null`（首块由首次 push 现分配，见 u3-l2），循环不执行，这里跳过释放——没有块被分配过，自然没有块要释放。

#### 4.2.4 代码实践

**实践目标**：通过追踪 `drops` 测试，理解 `Drop` 在「部分消费后再 drop 整个队列」时如何保证每个值恰好析构一次。

**操作步骤**：

1. 阅读 [tests/seg_queue.rs:164-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L164-L213) 的 `drops` 测试。它用一个带 `Drop` 计数器的 `DropCounter` 类型，在多线程随机 push/pop 一段时间后，再单线程 push `additional` 个，然后 `drop(q)`，最后断言 `DROPS == steps + additional`。
2. 重点理解这两条断言的来源（[seg_queue.rs:209-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L209-L211) 处的测试代码）：
   - `drop(q)` 之前：`DROPS == steps`——说明已被 pop 出去的 `steps` 个值各析构了一次（在消费者线程里）。
   - `drop(q)` 之后：`DROPS == steps + additional`——说明残留在队列里的 `additional` 个值被 `Drop` impl **恰好各析构一次**，没有漏 drop，也没有重复 drop。
3. 运行：`cargo test -p crossbeam-queue --test seg_queue drops`。

**需要观察的现象**：测试稳定通过，包括多次随机运行（`runs` 次循环）。可在 `drops` 里临时把 `additional` 改大（例如改为 `if cfg!(miri) { 100 } else { 5_000 }`），观察断言依然成立。

**预期结果**：测试通过。这验证了 `Drop` 的 `assume_init_drop` 只作用于「残留未消费的槽」，而已 pop 的槽（在消费者线程里已 `read` 移出）不会被 `Drop` 再次触碰。

> 说明：本实践是「源码阅读 + 运行既有测试」型，不修改库源码。改大 `additional` 只是改测试常量，运行后请还原。

#### 4.2.5 小练习与答案

**练习 1**：`Drop` 为什么不像 `len()` 那样需要「读 tail → 读 head → 复查 tail」的一致性快照循环？

**答案**：因为 `Drop::drop(&mut self)` 持有独占引用，编译期保证此刻没有其他线程访问队列，head/tail 不会在读取过程中被改动。一次 `get_mut()` 读取就是权威值，没有「快照不一致」的风险。`len()` 拿的是 `&self`，必须容忍并发修改，才需要 seqlock。

**练习 2**：循环结束后的 `if !block.is_null() { drop(Box::from_raw(block)); }` 释放的是哪一块？为什么它不会在循环内被释放？

**答案**：释放的是 tail 所在（也就是循环退出时 head==tail 所在）的那一块。循环只在「缝合位（offset 31）」时释放整块并切到下一块；而 tail 指向的位置不可能是缝合位（push 安装新块时 tail 会跳过缝合位，见 u3-l2），所以含 tail 的块不会在循环中被释放，必须收尾单独释放。

---

### 4.3 IntoIter 逐个读取并释放块

#### 4.3.1 概念说明

`SegQueue` 实现了 `IntoIterator`（0.3.4 引入，见 CHANGELOG），让你可以写 `for x in q { ... }` 来**消费**整个队列。这和 `pop` 循环不同：

- `IntoIter` 拥有队列的所有权（`into_iter(self)`），是「一次性消费」。
- 它持有 `&mut` 访问，因此和 `Drop`、`pop_mut` 一样**跳过所有原子操作**。
- 它「边读边释放」：每读出一个值就推进 head；当读到一块的最后一个数据槽（offset 30）时，顺手把整块释放，再切到下一块。

`IntoIter` 还要和一个微妙问题打交道：**提前终止**。如果你写 `q.into_iter().take(50)`，迭代只消费一部分，剩下的值怎么办？答案是——`IntoIter` 自身被 drop 时，会触发内部 `SegQueue` 的 `Drop`，由 4.2 的逻辑负责清理剩余值与块。所以 `IntoIter` 与 `Drop` 是分工合作的：迭代器负责「按需读出并释放已读完的块」，`Drop` 兜底「释放一切剩余」。

#### 4.3.2 核心流程

```text
next(&mut self):
    读 head、tail（get_mut，非原子）
    if head>>1 == tail>>1:           # 逻辑位置相等 → 空
        return None
    block = head.block; offset = (head>>1) % 32
    item = block.slots[offset].value.read().assume_init()   # 把值移出
    if offset + 1 == BLOCK_CAP:      # 刚读完一块最后一个数据槽(offset 30)
        释放当前块；head.block = block.next
        head.index += 2 << SHIFT      # 跳过缝合位(offset 31)，落到下一块 offset 0
        debug_assert 落点 offset == 0
    else:
        head.index += 1 << SHIFT      # 普通前进一格
    return Some(item)
```

注意两处推进量的差别：

- 普通槽：`head += 1 << SHIFT`（= +2，一个逻辑位置）。
- 块末尾：`head += 2 << SHIFT`（= +4，**两个**逻辑位置），因为要跳过 offset 30 后面的缝合位 offset 31，直接落到下一块的 offset 0。

#### 4.3.3 源码精读

`IntoIterator` 实现把 `SegQueue` 包进 `IntoIter`（[seg_queue.rs:648-661](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L648-L661)）：

```rust
impl<T> IntoIterator for SegQueue<T> {
    type Item = T;
    type IntoIter = IntoIter<T>;
    fn into_iter(self) -> Self::IntoIter {
        IntoIter { value: self }
    }
}

#[derive(Debug)]
pub struct IntoIter<T> {
    value: SegQueue<T>,
}
```

`IntoIter` 只是把整个 `SegQueue` 按值拥有进来。

**`next` 的空判定**（[seg_queue.rs:666-671](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L666-L671)）：

```rust
let head = *value.head.index.get_mut();
let tail = *value.tail.index.get_mut();
if head >> SHIFT == tail >> SHIFT {
    None
} ...
```

和 `pop_mut` 一样，用 `get_mut` 非原子读取，再比较逻辑位置。这里也没有 `fence`/`HAS_NEXT` 那一套，因为是独占访问。

**把值移出**（[seg_queue.rs:676-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L676-L683)）：

```rust
// SAFETY: 我们有可变访问，无需担心并发；且这是 head 指向的槽，
// 队列非空意味着它已初始化。
let item = unsafe {
    let slot = (*block).slots.get_unchecked(offset);
    slot.value.get().read().assume_init()
};
```

`MaybeUninit::read()` 把值**按位拷贝移出**（相当于 `ptr::read`），原槽位进入「已移出」状态。因为后续不会再有谁读这个槽（迭代器只往前走，`Drop` 也只处理 `[head, tail)`，head 已经越过它），所以不会重复析构。

**块末尾：释放并跳过缝合位**（[seg_queue.rs:684-697](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L684-L697)）：

```rust
if offset + 1 == BLOCK_CAP {
    unsafe {
        let next = *(*block).next.get_mut();
        drop(Box::from_raw(block));              // 释放刚读完的整块
        *value.head.block.get_mut() = next;     // head.block 切到下一块
    }
    // 缝合位不存值，跳过它
    *value.head.index.get_mut() = head.wrapping_add(2 << SHIFT);
    // 复核：新 head 应落在某块的 offset 0
    debug_assert_eq!((*value.head.index.get_mut() >> SHIFT) % LAP, 0);
}
```

读完一块的最后一个数据槽（offset 30）后，这块已经没有未读数据了，直接 `Box::from_raw` 释放（独占，无需 DESTROY 协调）。然后 `head.index += 2<<SHIFT` 跳过缝合位（offset 31）落到下一块 offset 0，`debug_assert` 兜底确认落点正确。

**普通前进**（[seg_queue.rs:698-700](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L698-L700)）：

```rust
} else {
    *value.head.index.get_mut() = head.wrapping_add(1 << SHIFT);
}
```

> 对比 `pop`（u3-l2）：并发版在读完 offset 30 时要走 `Block::destroy(block, 0)` 的 DESTROY 协调；而 `IntoIter` 是独占的，直接 `Box::from_raw`，等价于 `destroy_mut`。这是「并发版 vs 独占版」的又一次对照。

#### 4.3.4 代码实践

**实践目标**：通过 `into_iter` 与 `into_iter_drop` 两个测试，观察「完整消费」与「提前终止」两种情形下，值与块的回收是否都正确。

**操作步骤**：

1. 阅读 [tests/seg_queue.rs:215-224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L215-L224) 的 `into_iter`：push 0..=99，然后 `q.into_iter().enumerate()` 断言 `i == j`，验证 FIFO 顺序与完整消费。
2. 阅读 [tests/seg_queue.rs:226-235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L226-L235) 的 `into_iter_drop`：push 0..=99，但 `into_iter().enumerate().take(50)` 只消费前 50 个。剩下的 50 个不会被 `next` 读出，而是由 `IntoIter` 析构时触发的 `SegQueue::Drop` 兜底回收。
3. 运行：`cargo test -p crossbeam-queue --test seg_queue into_iter`。
4. **追踪一次提前终止**：在脑中（或纸上）模拟 `take(50)` 后 `IntoIter` 被 drop 的过程——它会调用 `SegQueue::drop`，此时 head 停在第 50 个元素之后（第 1 块已读完并被 `IntoIter` 释放，head 在第 2 块某 offset），tail 在第 99 个元素之后。`Drop` 的 `while head != tail` 循环会 drop 掉剩余 50 个值，并在跨过缝合位时释放中间块，最后释放含 tail 的块。

**需要观察的现象**：两个测试都通过，且 `into_iter_drop` 不会泄漏内存或重复析构。若开启 miri（`cargo +nightly miri test --test seg_queue into_iter_drop`）应能进一步验证没有 undefined behavior。

**预期结果**：测试通过；`into_iter_drop` 验证了「迭代器 + Drop 联动」对提前终止的安全性。

> 说明：本实践为「阅读 + 运行既有测试」型。运行 miri 是可选项，需本地有 nightly 工具链；若无，标注「待本地验证」即可。

#### 4.3.5 小练习与答案

**练习 1**：块末尾分支用 `head.wrapping_add(2 << SHIFT)` 推进两格，而普通分支只推进 `1 << SHIFT` 一格。为什么要差一格？

**答案**：因为读完 offset 30（一块最后一个数据槽）后，紧跟着的 offset 31 是缝合位，不存数据。普通前进一格只会到缝合位，那是错的；必须再前进一格跳过缝合位，落到下一块的 offset 0。所以块末尾总共前进两格（`2 << SHIFT`）。

**练习 2**：如果迭代到一半提前 `break`（例如 `take(50)`），剩余的值由谁负责析构？会不会被 `IntoIter::next` 重复读取？

**答案**：由 `IntoIter` 被 drop 时触发的 `SegQueue::Drop`（4.2）负责析构剩余值并释放剩余块。不会重复读取：`next` 每读出一个值就把 head 推进过去，`Drop` 的循环只处理 `[head, tail)`，head 已经越过已读槽，所以那些槽既不会被 `Drop` 再次 drop，也不会被 `next` 再次读出。

---

### 4.4 stack_overflow 测试与 0.3.12 修复

#### 4.4.1 概念说明

CHANGELOG 0.3.12 记载：

> Fix stack overflow when pushing large value to `SegQueue`. (#1146, #1147, #1159)

问题出在 `Block::new`。`Block<T>` 里有一个 `[Slot<T>; 31]` 数组，每个 `Slot<T>` 含一个 `MaybeUninit<T>`。当 `T` 很大时，整个 `Block<T>` 会非常巨大：

\[
\text{sizeof}(\text{Block}<T>) \approx 31 \times (\text{sizeof}(T) + 8) + 8.
\]

若 \(T\) 是 32KB 的数组，单个 `Block` 就接近 1MB。

问题在于**它在哪里被构造**。如果用「先在栈上构造整个 `Block` 值，再搬到堆上」的方式分配（例如朴素的 `Box::new(Block { ... })` 模式：Rust 会先在调用栈上求值并构造出完整的 `Block` 临时值，再 move 进堆），那么这个 ~1MB 的临时量就会压在**线程栈**上。在线程栈较小（如某些 spawn 的 worker 线程）或与其它栈使用叠加时，就可能**栈溢出**。

0.3.12 的修复思路：**直接在堆上分配一块全零内存，原地当作 `Block` 使用，绝不经过栈**。这既能避免栈溢出，又因为 `Block` 的所有字段都允许零初始化而完全安全。

#### 4.4.2 核心流程

`Block::new` 的新实现（修复后）：

```text
Block::new():
    用 Global::allocate_zeroed(Layout) 直接在堆上分配一块全零内存
    若分配失败 → handle_alloc_error（终止进程，不 unwind）
    用 Box::from_raw 把这块内存重新当 Box<Block<T>> 拥有
    返回
```

它能成立，是因为 `Block<T>` 的每个字段都容忍「全零」初值：

| 字段 | 类型 | 全零是否合法 |
|---|---|---|
| `next` | `AtomicPtr<Block<T>>` | ✓ null 指针即「无下一块」 |
| `slots[i].state` | `AtomicUsize` | ✓ 0 = 无 WRITE/READ/DESTROY 位 |
| `slots[i].value` | `UnsafeCell<MaybeUninit<T>>` | ✓ `MaybeUninit` 任意比特（含全零）都合法 |

因此「分配一块全零内存并直接当作 `Block`」是健全的，且完全绕开了栈。

#### 4.4.3 源码精读

**`Block::new` 与 `LAYOUT`**（[seg_queue.rs:65-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L65-L90)）：

```rust
const LAYOUT: Layout = {
    let layout = Layout::new::<Self>();
    assert!(layout.size() != 0, "Block should never be zero-sized, ...");
    layout
};

fn new() -> Box<Self> {
    // unsafe { Box::new_zeroed().assume_init() } requires Rust 1.92
    match Global.allocate_zeroed(Self::LAYOUT) {
        Some(ptr) => unsafe { Box::from_raw(ptr.as_ptr().cast()) },
        None => handle_alloc_error(Self::LAYOUT),
    }
}
```

几个要点：

- 注释点出「等价于 `Box::new_zeroed().assume_init()`，但那个 API 要 Rust 1.92」，而本 crate 的 MSRV 是 1.60（见 u1-l3），所以手写了 `allocate_zeroed` + `Box::from_raw`。
- `allocate_zeroed` 返回的内存**直接来自堆分配器**，全程不经过调用栈，这是修复栈溢出的关键。
- 内联的 SAFETY 注释（[seg_queue.rs:79-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L79-L84)）逐字段论证了零初始化的合法性。
- 分配失败走 `handle_alloc_error`（来自 `alloc::alloc`），按 Rust 约定**终止进程而非 unwind**（u3-l3 已提及）。

`allocate_zeroed` 的实现在 crate 私有的 `Global` 上（[alloc_helper.rs:44-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L44-L46)），底层就是 `alloc::alloc::alloc_zeroed(layout)`（[alloc_helper.rs:21-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L21-L33)）。零大小布局被特判为返回一个对齐悬垂指针（[alloc_helper.rs:21-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L21-L22)），但 `Block::LAYOUT` 的 `assert` 已保证 `Block` 永不为零大小。

**`stack_overflow` 测试**（[tests/seg_queue.rs:239-250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L239-L250)）：

```rust
// 如果 Block 在栈上创建，slots 数组会把这个 BigStruct 翻倍，
// 可能撑爆线程栈。现在它直接在堆上创建以避免此问题。
#[test]
fn stack_overflow() {
    const N: usize = 32_768;
    struct BigStruct { _data: [u8; N], }

    let q = SegQueue::new();
    q.push(BigStruct { _data: [0u8; N] });

    for _data in q.into_iter() {}
}
```

`BigStruct` = 32KB，于是 `Block<BigStruct>` ≈ 31×(32768+8)+8 ≈ 1016KB ≈ 1MB。这个测试**就是为修复而生的回归测试**：在 0.3.12 之前的实现下，它会因为构造 `Block` 时在栈上撑出 ~1MB 临时量而栈溢出崩溃；修复后因为 `Block::new` 直接堆分配，测试平稳通过。最后的 `for _data in q.into_iter() {}` 还顺带验证了 `IntoIter`（4.3）能正确处理这种大元素。

#### 4.4.4 代码实践（本讲的主实践）

**实践目标**：亲手验证 SegQueue 能正确 push/pop 一个含 32KB 数组的大元素，并解释「堆直接分配」如何避免栈溢出。

**操作步骤**：

1. 在 `tests/seg_queue.rs` 里已有现成的 `stack_overflow` 测试（[tests/seg_queue.rs:239-250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L239-L250)），先直接运行它：`cargo test -p crossbeam-queue --test seg_queue stack_overflow`。
2. **写一个最小补充测试**（可临时追加到 `tests/seg_queue.rs` 末尾，验证后删除），显式做一次 push 后 pop，校验大元素内容完整：

   ```rust
   // 示例代码：临时测试，验证后请删除
   #[test]
   fn big_element_roundtrip() {
       const N: usize = 32_768;
       let q = SegQueue::new();
       let input = [42u8; N];
       q.push(input);
       let output = q.pop().unwrap();
       assert_eq!(input, output);   // 32KB 内容完整往返
       assert!(q.pop().is_none());   // 队列已空
   }
   ```
3. 运行该测试，确认通过。
4. **用一段文字回答**（这是本实践的核心交付）：
   - 为什么把 `Block` 直接分配在堆上能避免大元素导致的栈溢出？
     - 参考答案：朴素分配（如 `Box::new(value)`）会先在调用栈上构造完整的 `Block` 临时值——而 `Block<T>` 含 `[Slot<T>; 31]`，对大 `T` 这个临时量可达 ~1MB，会撑爆线程栈。`Block::new` 改用 `Global::allocate_zeroed(LAYOUT)` 直接向堆分配器申请一块全零内存，再用 `Box::from_raw` 拥有，**整个构造过程不经过栈**，从而避开栈溢出。同时由于 `next`(null)、`state`(0)、`MaybeUninit<T>`(任意比特) 都容忍全零，这种「零初始化即合法」的分配是健全的。

**需要观察的现象**：`stack_overflow` 与 `big_element_roundtrip` 都通过；用 miri 运行（`cargo +nightly miri test --test seg_queue stack_overflow`）也不报错。

**预期结果**：测试通过，32KB 元素能完整地 push 再 pop。若你尝试把 `N` 调得更大（例如 `1 << 20`，1MB），现代主线程栈（通常 8MB）仍可能扛得住单次，但在线程数多或栈更小的场景就会触发原本的 bug——这正是修复存在的意义。

> 说明：补充测试 `big_element_roundtrip` 是「示例代码」，请加在测试文件末尾运行，验证后删除，不要提交。本实践不修改库源码。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Block::new` 能安全地把「一块全零内存」当作 `Block<T>` 使用？请逐字段说明。

**答案**：`Block<T>` 有两类字段。`next: AtomicPtr<Block<T>>` 全零即 null，表示「暂无下一块」，合法。`slots: [Slot<T>; 31]`，每个 `Slot` 含 `value: UnsafeCell<MaybeUninit<T>>` 与 `state: AtomicUsize`：`MaybeUninit<T>` 的全部比特模式（包括全零）都合法（它本就不承诺初始化）；`AtomicUsize` 全零表示 WRITE/READ/DESTROY 都未置位，正是「空槽」的合法初值。因此整体零初始化是健全的（源码 SAFETY 注释 [seg_queue.rs:79-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L79-L84) 同此论证）。

**练习 2**：如果 `T` 是 `u8`（很小），`stack_overflow` 描述的栈溢出风险还存在吗？为什么 0.3.12 的修复仍然有价值？

**答案**：对很小的 `T`，`Block<T>` 本身不大（约 31×(1+8)+8 ≈ 287 字节），栈溢出风险基本不存在。但 `SegQueue` 是泛型的，使用者完全可能放入大 `T`（如大数组、大结构体）。0.3.12 的修复对**所有** `T` 都改用堆直接分配，消除了「大 `T` 触发栈溢出」这一类问题，使 crate 对任意 `T` 都安全可用，因此具有普遍价值。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路追踪」任务。

**任务**：对一个 `SegQueue`，push 64 个 `i32`（会跨块：block 0 装 31 个、block 1 装 31 个、block 2 装 2 个），然后：

1. **预测并验证 `len()`**：手算每一阶段（push 第 31、32、62、63、64 个时）的 `len()` 返回值，重点验证「tail 越过缝合位」时块末尾修正生效。再用一段小程序（可写在 examples 或临时测试里）打印实际 `len()` 对照。
2. **追踪 `Drop`**：若此时直接 `drop(q)`，画出 `Drop` 的 `while head != tail` 循环会如何遍历：哪些 offset 会触发 `assume_init_drop`，哪些 offset（缝合位）会触发 `Box::from_raw` 释放块，最终释放几块、drop 几个值。
3. **改用 `IntoIter` 消费**：把 `drop(q)` 换成 `for (i, v) in q.into_iter().enumerate() { assert_eq!(i as i32, v); }`，验证 64 个值按 FIFO 顺序读出；再换成 `.take(40)` 提前终止，解释剩下 24 个值由谁析构。
4. **回归 0.3.12**：把元素类型从 `i32` 换成 `[u8; 32768]`，push 一个再 pop 一个，确认能正常往返（即复用 `stack_overflow` 的场景）。

**验收标准**：

- (1) 的手算与实际 `len()` 吻合，尤其 push 第 31 个时 `len()==31`（缝合位修正生效）。
- (2) 的遍历描述正确：drop 64 个值，释放 3 块（block 0、1 各在缝合位释放，block 2 在收尾释放）。
- (3) FIFO 顺序正确；`take(40)` 后剩余 24 个值由 `IntoIter` 析构触发的 `SegQueue::Drop` 回收。
- (4) 32KB 元素能完整往返，无栈溢出。

> 这是一个「源码阅读 + 小程序验证」型综合实践，建议把验证程序放在 `examples/` 或临时测试里，完成后删除，不要改动库源码。

## 6. 本讲小结

- `len(&self)` 用 seqlock 风格的一致性快照（读 tail→读 head→复查 tail）拿一组真实共存过的 `(head, tail)`，再做「抹 HAS_NEXT → 块末尾修正 → 归一化 → 右移 → 套公式」的算术，最终 \(\text{len}=T-H-\lfloor T/\text{LAP}\rfloor\)，其中扣除项 \(\lfloor T/\text{LAP}\rfloor\) 正是缝合位个数。
- `is_empty` 是 `len` 的廉价版，只比 head/tail 逻辑位置是否相等，无需快照循环。
- `Drop::drop(&mut self)` 凭独占引用跳过一切并发协调：一段 `while head != tail` 循环，数据槽 `assume_init_drop`、缝合位 `Box::from_raw` 释放并换块，收尾再释放含 tail 的最后一块。
- `IntoIter` 拥有队列、按需 `read().assume_init()` 移出值；读完一块最后数据槽时直接 `Box::from_raw` 释放（独占，无需 DESTROY 协调）并用 `2<<SHIFT` 跳过缝合位；提前终止时由 `SegQueue::Drop` 兜底回收剩余。
- 三者的「并发程度」递减——`len`（共享，需快照）→ `Drop`/`IntoIter`（独占，免原子），写法差异的根因即在于此。
- 0.3.12 修复了「push 大元素导致栈溢出」：`Block::new` 改用 `Global::allocate_zeroed` 直接在堆上分配全零内存并 `Box::from_raw` 拥有，全程不经过栈；这之所以健全，是因为 `Block` 的所有字段（`AtomicPtr` null、`AtomicUsize` 0、`MaybeUninit` 全零）都容忍零初始化。

## 7. 下一步学习建议

- **内存序的精确论证**：本讲多次出现 `SeqCst`（`len`/`is_empty`），但刻意没展开「为什么必须是 SeqCst、能否更弱」。这正是 u4-l1（原子内存序与 fence）的主题，建议接着读它，回头给 `len` 的快照逐处补上内存序论证。
- **unsafe 与 MaybeUninit 的安全性**：本讲里 `assume_init_drop`、`read().assume_init()`、`Box::from_raw`、`Block::new` 的零初始化都依赖安全性不变量。u4-l3 会系统论证 `Send/Sync` 与 `MaybeUninit` 的配合，建议结合本讲的 `Drop`/`IntoIter` 一起精读。
- **并发测试方法**：本讲的 `drops` 测试是「析构计数验证语义」的范例，u4-l4 会把它和 spsc/mpmc/linearizable 等并发测试范式串成一套方法论，值得继续深入。
- **建议继续阅读的源码**：重读 [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) 中 `pop_mut`（[seg_queue.rs:471-526](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L471-L526)），把它与本讲的 `IntoIter::next` 对照，体会「独占访问下，pop/迭代/释放块如何共用同一套位运算骨架」。
