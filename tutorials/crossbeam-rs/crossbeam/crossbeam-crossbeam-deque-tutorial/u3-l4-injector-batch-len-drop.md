# Injector 批量 steal、len 与 Drop

## 1. 本讲目标

本讲是 Injector 系列的收尾篇。在 [u3-l1](u3-l1-injector-block-and-slot.md)（Block/Slot 数据结构）、[u3-l2](u3-l2-injector-push.md)（push）和 [u3-l3](u3-l3-injector-steal-and-destroy.md)（单任务 steal 与 Block 销毁）之后，我们把 Injector 还剩下的三件事讲透：

1. **批量偷取**：`steal_batch` / `steal_batch_with_limit` / `steal_batch_and_pop` / `steal_batch_with_limit_and_pop` 四个方法。
2. **长度查询**：`len()` 为什么必须用一个「重读校验」循环，并对块边界做修正。
3. **空判定与析构**：`is_empty()` 的精确比较，以及 `Drop` 如何遍历 `head..tail` 逐个释放任务和 block。

学完本讲，你应当能够：

- 说出四个批量偷取方法的「弹不弹出 × 上限可否指定」二维关系，以及它们如何复用同一段执行流程。
- 解释 `advance`（认领多少格）和 `batch_size`（写入目的队列多少个）的计算规则：偷约一半、偷到块尾、受 `limit` 约束。
- 理解 Injector 批量偷取的内存序骨架，以及它在末尾如何触发 Block 的协作销毁。
- 读懂 `len()` 中「两次读取 tail 一致」的一致性循环、块边界哨兵修正、lap 旋转，以及最终公式 `tail - head - tail / LAP`。
- 解释 `Drop` 为何能用 `get_mut()` 跳过原子操作、按 `offset < BLOCK_CAP` 分流「drop 任务 / 释放 block」。

---

## 2. 前置知识

本讲默认你已经读过 u3-l1 ~ u3-l3。为方便查阅，这里简要回顾几个关键常量与结构（不展开推导）：

- **索引编码**：每个任务位置打包进一个 `usize`。低 1 位是 `SHIFT`，`HAS_NEXT=1` 占用最低位表示「后面还有 block」；逻辑位置 `= index >> SHIFT`，块内偏移 `= (index >> SHIFT) % LAP`，第几圈 `= (index >> SHIFT) / LAP`。
- **`LAP=64`，`BLOCK_CAP=63`**：每个 block 覆盖一圈共 64 个位置，其中 offset `0..=62` 是 63 个真实槽位，offset `63` 是**哨兵**（标记本块已满、下一块正在安装，本身不存任务）。
- **Slot 状态位**：`WRITE=1`（任务已写入）、`READ=2`（任务已被读走）、`DESTROY=4`（请求销毁本块）。
- **`Position<T>`**：缓存 `(index: AtomicUsize, block: AtomicPtr<Block<T>>)` 二元组，Injector 的 `head` 与 `tail` 各是一个 `CachePadded<Position<T>>`。
- **跨块推进**：push 写到本块最后一个真实槽位（offset `== BLOCK_CAP-1 == 62`）时，CAS 把 tail 推到哨兵位，紧接着再 store 一次把 tail 越过哨兵推进到下一块 offset 0。因此 tail **可能瞬态停在哨兵位**——这正是 `len()` 要修正的对象。

这些常量的定义集中在 [src/deque.rs:1200-1211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1200-L1211)：

```rust
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;
const LAP: usize = 64;
const BLOCK_CAP: usize = LAP - 1; // 63
const SHIFT: usize = 1;
const HAS_NEXT: usize = 1;
```

`Injector<T>` 本身只是 `head` + `tail` 两个 Position 加一个 `_marker`，见 [src/deque.rs:1332-1341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1332-L1341)。

> 阅读提示：本讲的 `advance`、`batch_size` 等局部变量名直接取自源码，便于你对照行号阅读。

---

## 3. 本讲源码地图

本讲只涉及一个源文件，但它承担了 Injector 几乎所有「读写与回收」逻辑：

| 源码位置 | 作用 |
|---|---|
| `Injector::steal_batch` ([src/deque.rs:1564-1566](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1564-L1566)) | 批量偷取的转发壳，固定上限 `MAX_BATCH=32` |
| `Injector::steal_batch_with_limit` ([src/deque.rs:1601-1743](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1601-L1743)) | 批量偷取的真实实现（不弹出） |
| `Injector::steal_batch_and_pop` ([src/deque.rs:1766-1770](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1766-L1770)) | 批量偷取并弹出一个任务的转发壳 |
| `Injector::steal_batch_with_limit_and_pop` ([src/deque.rs:1805-1952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1805-L1952)) | 批量偷取并弹出的真实实现 |
| `Injector::is_empty` ([src/deque.rs:1967-1971](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1967-L1971)) | 空判定 |
| `Injector::len` ([src/deque.rs:1988-2021](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1988-L2021)) | 长度查询（一致性循环 + 索引修正） |
| `impl Drop for Injector` ([src/deque.rs:2024-2057](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2024-L2057)) | 析构时释放残留任务与 block |
| `Worker::reserve` ([src/deque.rs:326-350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L326-L350)) | 批量偷取前为目的队列预留容量 |
| `Block::destroy` ([src/deque.rs:1284-1301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1284-L1301)) | 协作销毁一个 block（u3-l3 已讲） |
| `Slot::wait_write` ([src/deque.rs:1224-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1224-L1229)) | 等待任务写入（u3-l2 已讲） |

---

## 4. 核心概念与源码讲解

### 4.1 四个批量偷取方法与统一执行流程

#### 4.1.1 概念说明

Injector 的批量偷取方法是一个 **2×2 矩阵**：

| 方法 | 是否弹出一个任务返回 | 上限 |
|---|---|---|
| `steal_batch` | 否 | 固定 `MAX_BATCH=32` |
| `steal_batch_with_limit` | 否 | 调用方指定 |
| `steal_batch_and_pop` | 是 | 固定 `MAX_BATCH+1=33` |
| `steal_batch_with_limit_and_pop` | 是 | 调用方指定 |

不带 `_with_limit` 的两个方法是「转发壳」，分别委托给带 limit 的版本：

- `steal_batch` 直接调用 `steal_batch_with_limit(dest, MAX_BATCH)`，见 [src/deque.rs:1564-1566](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1564-L1566)。
- `steal_batch_and_pop` 调用 `steal_batch_with_limit_and_pop(dest, MAX_BATCH + 1)`，见 [src/deque.rs:1766-1770](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1766-L1770)。

> 为什么 `and_pop` 用 `MAX_BATCH + 1` 而不是 `MAX_BATCH`？源码里有一行 TODO 注释（[src/deque.rs:1767-1768](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1767-L1768)）说明这只是为了性能略好，未来可能与 `Stealer` 的同名方法对齐，**这是一个实现细节，不应被调用方依赖**。

因此真正需要读的实现只有两个：`steal_batch_with_limit` 与 `steal_batch_with_limit_and_pop`。它们共享几乎完全相同的骨架，只有「认领的格子里有几格要弹出一个」这一处差异。

#### 4.1.2 核心流程

两个真实实现的执行流程可以分为六个阶段，对照 [src/deque.rs:1601-1743](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1601-L1743)（不弹出）与 [src/deque.rs:1805-1952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1805-L1952)（弹出）：

```
1. 加载 head/block/offset —— 若 offset==BLOCK_CAP（哨兵）则 snooze 重读
2. 计算 advance（认领多少格）—— 三分支：偷到块尾 / 偷一半
3. CAS 把 head 推进 advance 格 —— 失败返回 Steal::Retry
4. reserve + 读取 head 端的 block 与目的 Worker 的 back
5. 按目的 flavor 把任务拷进目的队列，Release fence + 写 back 发布
6. 触发 Block 协作销毁（到块尾 / 命中 DESTROY 位）
```

这个骨架与 u3-l3 讲过的单任务 `steal` 高度同构：同样是「Acquire 读 head → 算 offset → 哨兵等待 → CAS 推进 head → 读槽位 → 销毁」。区别只在于**一次 CAS 认领连续的多个格子**，而不是一格。

#### 4.1.3 源码精读

第 1 阶段，加载 head 并处理哨兵，在 [src/deque.rs:1603-1621](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1603-L1621)：

```rust
let backoff = Backoff::new();
loop {
    head = self.head.index.load(Ordering::Acquire);
    block = self.head.block.load(Ordering::Acquire);
    offset = (head >> SHIFT) % LAP;
    // 到达块尾哨兵，说明下一块还没安装好，退避后重读
    if offset == BLOCK_CAP {
        backoff.snooze();
    } else {
        break;
    }
}
```

这段与 `Injector::steal`、`Injector::push` 的开头完全一致：消费者看到 head 落在哨兵位（offset 63），意味着 head 已经跨进了一个尚未安装完毕的新 block，于是用 `Backoff::snooze`（长退避）自旋等待，重新加载 `head.index` 与 `head.block`。

第 3 阶段的 CAS 推进 head，在 [src/deque.rs:1654-1662](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1654-L1662)：

```rust
// 尝试把 head 推进 advance 格
if self.head.index.compare_exchange_weak(
    head, new_head, Ordering::SeqCst, Ordering::Acquire,
).is_err() {
    return Steal::Retry;
}
```

- 成功序用 `SeqCst`，保证「认领一段」对其他消费者/生产者全局可见；
- 失败序用 `Acquire`，让重试时能读到最新 head；
- CAS 失败说明有并发竞争（别的消费者也在偷、或 push 也在改 tail 相关状态），直接返回 `Steal::Retry`，让调用方自行重试（这正是 `Steal::Retry` 的语义，见 u1-l4）。

> 注意：和单任务 steal 一样，**CAS 成功只意味着「认领到了这段格子」**，任务体的读取、拷贝、发布都还在后面。这一步不会失败，只会把后续工作「私有化」给当前线程。

#### 4.1.4 代码实践

**实践目标**：用最小例子确认「转发壳」与「真实实现」的对应关系，并体会 `MAX_BATCH` 与 `MAX_BATCH+1` 的差异只是上限，不是保证偷到的数量。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/deque.rs:1564-1566](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1564-L1566)，确认 `steal_batch` 只有一行委托。
2. 打开 [src/deque.rs:1766-1770](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1766-L1770)，确认 `steal_batch_and_pop` 委托给 `MAX_BATCH + 1`。
3. 阅读 `steal_batch_with_limit` 的文档注释 [src/deque.rs:1568-1600](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1568-L1600)，注意这句：*"How many tasks exactly will be stolen is not specified."*

**预期结果**：你会清楚地看到，调用方传入的 `limit` 只是一个**上限**，真实偷取数量还受「偷一半」策略和块尾位置约束（详见 4.2）。文档明确把具体数量列为「可能在未来改变的实现细节」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `steal_batch_and_pop` 的默认上限是 `MAX_BATCH + 1` 而不是 `MAX_BATCH`？

**参考答案**：因为它在批量偷取之外还要**额外弹出一个任务直接返回**给调用方，所以把上限放宽一格（33 而不是 32）。源码 TODO 注释说明这只是为了性能略好，未来可能与 `Stealer` 的同名方法对齐，调用方不应依赖这个具体数值。

**练习 2**：`steal_batch` 与 `steal_batch_with_limit` 的函数体各自有多长？这说明了什么设计？

**参考答案**：`steal_batch` 函数体只有 1 行（`self.steal_batch_with_limit(dest, MAX_BATCH)`），而 `steal_batch_with_limit` 有约 140 行。这说明四个公开方法共享同一套实现，通过「转发壳 + 默认参数」对外暴露便捷 API，避免重复代码。

---

### 4.2 advance 与 batch_size：偷一半 / 偷到块尾 / limit 约束

#### 4.2.1 概念说明

批量偷取的核心决策是：**这次 CAS 要把 head 推进多少格？** 源码里这个量叫 `advance`。它不是「能偷多少偷多少」，而是遵循两条经验法则：

1. **偷约一半**（`len.div_ceil(2)`）：当 head 与 tail 在同一个 block 内时，偷走当前可用任务的大约一半。这是 work-stealing 的经典折中——既让偷取者一次性拿到多个任务、减少 CAS 次数，又不至于把 owner（生产者）的队列掏空。
2. **偷到块尾**（`BLOCK_CAP - offset`）：当 head 与 tail 已经跨 block 时，干脆把 head 所在的当前 block 从 `offset` 一直偷到哨兵前（offset 62），这样能把整个 block 一次性消费完、立即触发销毁与回收。

`limit` 是对 `advance` 的硬上限：`advance = 计算值.min(limit)`。

`batch_size` 则是**实际写入目的队列的任务数**，它由 `advance` 推导而来：

- 不弹出（`steal_batch_with_limit`）：`batch_size = advance`，全部写进目的队列。
- 弹出（`steal_batch_with_limit_and_pop`）：`batch_size = advance - 1`，因为有一格直接弹出返回给调用方，不进目的队列。

#### 4.2.2 核心流程

`advance` 的三分支判定（两个实现完全相同），在 [src/deque.rs:1623-1649](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1623-L1649)：

```
若 head 已置 HAS_NEXT（已知跨块）:
    advance = (BLOCK_CAP - offset).min(limit)        # 偷到块尾
否则:
    SeqCst fence
    读 tail
    若 head>>SHIFT == tail>>SHIFT:  返回 Steal::Empty
    若 head 与 tail 不同 lap（跨块）:
        new_head |= HAS_NEXT
        advance = (BLOCK_CAP - offset).min(limit)    # 偷到块尾
    否则（同块）:
        len = (tail - head) >> SHIFT
        advance = len.div_ceil(2).min(limit)         # 偷一半
```

注意 `HAS_NEXT` 位的作用：它是 head 上一个「懒缓存」，记录「head 与 tail 是否跨块」。一旦置位，后续偷取就**不再读 tail**，直接按「偷到块尾」处理，省掉一次 `SeqCst fence + tail 加载`。只有当 `HAS_NEXT == 0` 时，才需要补 fence 读 tail 做精确判定——这与 u3-l3 单任务 steal 的 `HAS_NEXT` 懒缓存逻辑完全一致。

`batch_size` 的两处差异：

- 不弹出：`batch_size = new_offset - offset`，见 [src/deque.rs:1665](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1665)。
- 弹出：`batch_size = new_offset - offset - 1`，见 [src/deque.rs:1868](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1868)。

其中 `new_offset = offset + advance`。

#### 4.2.3 源码精读

`advance` 计算的核心代码（不弹出版），在 [src/deque.rs:1626-1649](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1626-L1649)：

```rust
if new_head & HAS_NEXT == 0 {
    atomic::fence(Ordering::SeqCst);
    let tail = self.tail.index.load(Ordering::Relaxed);

    // tail 与 head 相等 → 队列空
    if head >> SHIFT == tail >> SHIFT {
        return Steal::Empty;
    }

    // head 与 tail 不同 lap → 跨块，置 HAS_NEXT，偷到块尾
    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP {
        new_head |= HAS_NEXT;
        advance = (BLOCK_CAP - offset).min(limit);
    } else {
        // 同块：偷一半
        let len = (tail - head) >> SHIFT;
        advance = len.div_ceil(2).min(limit);
    }
} else {
    // 已知跨块：偷到块尾
    advance = (BLOCK_CAP - offset).min(limit);
}
```

几个要点：

- **空判定**用 `head >> SHIFT == tail >> SHIFT`，即比较逻辑位置是否相等（忽略 `HAS_NEXT` 低位）。
- **跨块判定**用 `(index >> SHIFT) / LAP` 比较第几圈，而不是直接比 block 指针——因为 lap 编码已经把圈数编进了 index。
- **偷一半**用 `div_ceil(2)`：长度为奇数时偷上取整（例如 5 个偷 3 个），长度为偶数时正好偷一半。

「弹出」版本对应代码在 [src/deque.rs:1830-1852](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1830-L1852)，逻辑与上面逐字相同，只是 `advance` 之后多减 1 得到 `batch_size`。

`batch_size` 的推导对照：

```rust
// steal_batch_with_limit（不弹出）
new_head += advance << SHIFT;
let new_offset = offset + advance;
...
let batch_size = new_offset - offset;          // == advance
```

```rust
// steal_batch_with_limit_and_pop（弹出）
new_head += advance << SHIFT;
let new_offset = offset + advance;
...
let batch_size = new_offset - offset - 1;      // == advance - 1
```

随后用 `dest.reserve(batch_size)` 给目的队列预留容量，见 [src/deque.rs:1664-1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1664-L1666)。`Worker::reserve`（[src/deque.rs:326-350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L326-L350)）会在容量不足时按 2 的幂倍增，避免后续逐个 `write` 时频繁 resize——这是 u2-l5 讲过的容量管理。

#### 4.2.4 代码实践

**实践目标**：手工模拟一次 `steal_batch_with_limit_and_pop(&w, 2)`，确认 `advance`、`batch_size` 的取值，验证文档示例的断言。

**操作步骤**（纸上推演）：

设 `q.push(1..=6)` 之后，`head.index = 0`，`tail.index = 12`（每次 push 推进 `1<<SHIFT = 2`，共 6 次）。`w = Worker::new_fifo()`，调用 `q.steal_batch_with_limit_and_pop(&w, 2)`：

1. `offset = (0 >> 1) % 64 = 0`。
2. `HAS_NEXT == 0`，补 fence 读 `tail = 12`。`head>>SHIFT = 0`，`tail>>SHIFT = 6`，不等 → 非空。
3. `(0)/64 = 0`，`(6)/64 = 0`，同 lap → 走「偷一半」：`len = (12 - 0) >> 1 = 6`，`advance = 6.div_ceil(2).min(2) = 3.min(2) = 2`。
4. `new_head = 0 + 2<<1 = 4`，`new_offset = 0 + 2 = 2`。
5. `batch_size = 2 - 0 - 1 = 1`。

**需要观察的现象**：`advance = 2`（认领 2 格，即 head 端的前 2 个任务：1 和 2），其中第 1 个（任务 1）直接弹出返回，剩 `batch_size = 1` 个（任务 2）写入 `w`。

**预期结果**：调用返回 `Steal::Success(1)`，随后 `w.pop() == Some(2)`、`w.pop() == None`。这正是 [src/deque.rs:1792-1794](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1792-L1794) 的文档断言。

#### 4.2.5 小练习与答案

**练习 1**：若 `limit = usize::MAX` 且队列里有 6 个任务（同块），`advance` 会是多少？为什么不会偷光全部？

**参考答案**：`advance = 6.div_ceil(2).min(usize::MAX) = 3`。因为同块时永远走「偷一半」分支，`limit` 只是上限，不会让策略变成「偷光」。

**练习 2**：什么条件下 `advance = BLOCK_CAP - offset`（偷到块尾）？此时 `new_offset` 会是多少？

**参考答案**：当 head 与 tail 跨块（不同 lap，或 `HAS_NEXT` 已置位）时。此时 `advance = (63 - offset).min(limit)`，若 `limit` 足够大，`new_offset = offset + (63 - offset) = 63 = BLOCK_CAP`，恰好落在哨兵位，从而在 4.3 中触发 block 切换与销毁。

---

### 4.3 跨块切换、目的队列写入与 Block 销毁

#### 4.3.1 概念说明

CAS 认领成功后，线程进入「私有化」阶段：把认领到的任务从 Injector 的 block 槽位拷贝进目的 `Worker` 的 buffer。这一阶段要做三件事：

1. **跨块切换**：如果本次偷取恰好消费到块尾（`new_offset == BLOCK_CAP`），把 head 推进到下一个 block。
2. **拷贝写入**：逐个 `wait_write` 等任务就绪 → 读槽位 → 写进目的 buffer。FIFO 目的保持原序，LIFO 目的反转顺序。
3. **发布与销毁**：用 `Release fence + Relaxed store`（tsan 下改 `Release store`）发布目的队列的 `back`，然后按是否到块尾触发 Block 的协作销毁。

「弹出」版本与「不弹出」版本在这三件事上几乎完全相同，唯一差别是：弹出版先把 `offset` 处的第一个任务读出来作为返回值，剩余 `batch_size` 个从 `offset+1` 起拷贝；不弹出版则把全部 `batch_size` 个从 `offset` 起拷贝。

#### 4.3.2 核心流程

跨块切换与拷贝写入（不弹出版），在 [src/deque.rs:1672-1710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1672-L1710)：

```
若 new_offset == BLOCK_CAP（消费到块尾）:
    next = block.wait_next()                      # 等下一块装好
    next_index = (new_head 去掉 HAS_NEXT) + 1<<SHIFT   # 落到下一块 offset 0
    若 next.next 非空: next_index |= HAS_NEXT     # 下一块后面还有块
    store head.block = next  (Release)
    store head.index = next_index (Release)

按 dest.flavor 拷贝 batch_size 个任务:
    Fifo: 对 i in 0..batch_size: 读 slot[offset+i]，写 dest_buffer[dest_b + i]
    Lifo: 对 i in 0..batch_size: 读 slot[offset+i]，写 dest_buffer[dest_b + (batch_size-1-i)]
```

发布与销毁（不弹出版），在 [src/deque.rs:1712-1742](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1712-L1742)：

```
#[cfg(not(crossbeam_sanitize_thread))] atomic::fence(Release)
store_order = if tsan { Release } else { Relaxed }
store dest.back = dest_b + batch_size  (store_order)

若 new_offset == BLOCK_CAP:
    Block::destroy(block, offset)                # 整块销毁
否则:
    对 i in offset..new_offset:
        若 slot[i].state.fetch_or(READ, AcqRel) 命中 DESTROY:
            Block::destroy(block, offset); break  # 接力销毁
```

#### 4.3.3 源码精读

跨块切换的代码，在 [src/deque.rs:1674-1683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1674-L1683)：

```rust
if new_offset == BLOCK_CAP {
    let next = (*block).wait_next();
    let mut next_index = (new_head & !HAS_NEXT).wrapping_add(1 << SHIFT);
    if !(*next).next.load(Ordering::Relaxed).is_null() {
        next_index |= HAS_NEXT;
    }
    self.head.block.store(next, Ordering::Release);
    self.head.index.store(next_index, Ordering::Release);
}
```

要点：

- `new_head` 此刻已经推进过了（CAS 成功后），它可能带着 `HAS_NEXT` 位。`(new_head & !HAS_NEXT)` 先剥掉该位，再加 `1 << SHIFT`，恰好越过哨兵落到下一块的 offset 0。
- 若 `next.next` 已经非空（下一块之后还有块），就把 `HAS_NEXT` 重新置上，保持懒缓存正确。
- `head.block` 与 `head.index` 用 `Release` store 成对更新，把「head 已进入新块」发布给其他消费者与 `Block::wait_next`。

FIFO 与 LIFO 目的的拷贝差异，在 [src/deque.rs:1686-1710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1686-L1710)：

```rust
Flavor::Fifo => {
    for i in 0..batch_size {
        let slot = (*block).slots.get_unchecked(offset + i);
        slot.wait_write();
        let task = slot.task.get().read();
        dest_buffer.write(dest_b.wrapping_add(i as isize), task);
    }
}
Flavor::Lifo => {
    for i in 0..batch_size {
        let slot = (*block).slots.get_unchecked(offset + i);
        slot.wait_write();
        let task = slot.task.get().read();
        dest_buffer.write(dest_b.wrapping_add((batch_size - 1 - i) as isize), task);
    }
}
```

- 源端永远按 `offset, offset+1, ...` 顺序读（Injector 是 FIFO，任务按 push 顺序排列在 block 里）。
- FIFO 目的：写到 `dest_b + i`，**保持原序**——先偷到的先进队底，pop 时最先出。
- LIFO 目的：写到 `dest_b + (batch_size - 1 - i)`，**反转顺序**——先偷到的反而在更靠近队顶的位置。这是 LIFO 队列语义的需要，保证整个批次被「后进先出」消费时的相对顺序正确。

> `slot.wait_write()`（[src/deque.rs:1224-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1224-L1229)）用 `Acquire` 加载 state 等待 `WRITE` 位置上，与 push 端的 `fetch_or(WRITE, Release)`（[src/deque.rs:1435](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1435)）配对，确保读到已初始化的任务体。

发布与销毁，在 [src/deque.rs:1712-1739](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1712-L1739)：

```rust
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
dest.inner.back.store(dest_b.wrapping_add(batch_size as isize), store_order);

if new_offset == BLOCK_CAP {
    Block::destroy(block, offset);
} else {
    for i in offset..new_offset {
        let slot = (*block).slots.get_unchecked(i);
        if slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0 {
            Block::destroy(block, offset);
            break;
        }
    }
}
```

两个关键点：

1. **tsan 双路径**（u4-l3 会专门讲）：ThreadSanitizer 不理解 `fence`，所以在 `crossbeam_sanitize_thread` 开启时，省掉 `Release fence`，改用 `Release` 序直接 `store` 目的 `back`；正常模式下用 `Release fence + Relaxed store`。这与 `Worker::push` 的双路径写法一致。
2. **Block 销毁的两种触发**：
   - 若偷到块尾（`new_offset == BLOCK_CAP`），整个 block 的有效槽位都被消费完，直接 `Block::destroy(block, offset)` 销毁。
   - 否则，逐个给消费过的槽位打 `READ` 标记；若发现某个槽位已经被打了 `DESTROY` 标记（说明之前有线程想销毁这个 block 但因为本线程正在读而推迟了），就由本线程「接力」调用 `Block::destroy`。

`Block::destroy`（[src/deque.rs:1284-1301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1284-L1301)）是 u3-l3 讲过的协作销毁机制：它反向扫描槽位，给仍在用的槽位打 `DESTROY` 位（交出接力棒），只有当没有任何线程还在读槽位时才真正 `drop(Box::from_raw(block))`，保证一个 block 恰好被释放一次、不会 double-free。

#### 4.3.4 代码实践

**实践目标**：对比 FIFO 目的与 LIFO 目的下，同一批任务被拷贝后的相对顺序差异。

**操作步骤**（源码阅读 + 推演）：

1. 阅读 [src/deque.rs:1686-1710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1686-L1710) 两个分支。
2. 设 Injector 中有任务 `[A, B, C]`（push 顺序），`steal_batch` 全部偷进目的队列，`dest_b = 0`，`batch_size = 3`。
3. 对 FIFO 目的：`write(0, A)`、`write(1, B)`、`write(2, C)`，pop 顺序为 `A, B, C`。
4. 对 LIFO 目的：`write(2, A)`、`write(1, B)`、`write(0, C)`，pop 顺序为 `C, B, A`。

**需要观察的现象**：源端读取顺序固定为 `A, B, C`，但写入目的 buffer 的下标不同——LIFO 分支用 `batch_size - 1 - i` 反转了下标。

**预期结果**：

| 目的 flavor | 写入下标序列 | pop 顺序 |
|---|---|---|
| FIFO | `0, 1, 2`（A→0, B→1, C→2） | A, B, C |
| LIFO | `2, 1, 0`（A→2, B→1, C→0） | C, B, A |

#### 4.3.5 小练习与答案

**练习 1**：为什么「到块尾」分支可以直接调 `Block::destroy`，而「非块尾」分支要逐个 `fetch_or(READ)` 检查 `DESTROY`？

**参考答案**：到块尾意味着本块所有 63 个有效槽位都已被消费（`new_offset == BLOCK_CAP`），本线程是最后一个使用本块的消费者，可以安全销毁。非块尾时，本块可能还有其他槽位正被别的消费者读取，所以只能给自己消费的槽位打 `READ` 标记；只有当发现别人留下的 `DESTROY` 标记（表示有人已等本线程读完）时，才接力销毁。

**练习 2**：跨块切换时，`next_index = (new_head & !HAS_NEXT) + (1 << SHIFT)` 中为什么要先 `& !HAS_NEXT`？

**参考答案**：`new_head` 此刻可能带着 `HAS_NEXT` 位（来自 4.2 的跨块判定）。要落到下一块的 offset 0，必须先用 `& !HAS_NEXT` 剥掉这个懒缓存位，再加一格（`1 << SHIFT`）越过哨兵；之后再根据 `next.next` 是否非空重新决定是否置 `HAS_NEXT`。

---

### 4.4 len() 的一致性重读与索引修正

#### 4.4.1 概念说明

`len()` 看似简单（`tail - head`），但在并发队列里有两个坑：

1. **撕裂读**（torn read）：先读 tail、再读 head 的过程中，tail 可能被生产者推进，导致读到的 head 与 tail 不属于同一时刻，算出的长度可能为负或严重偏大。
2. **哨兵位**：tail 在跨块安装时**会瞬态停在 offset 63（哨兵）**，这个位置不是真实任务，直接相减会把哨兵算进去。

Injector 的解法是：

- **一致性循环**：读 tail、读 head、再读一次 tail，只有当两次 tail 相等时才认为拿到了一致快照；否则重来。
- **索引修正**：清掉 `HAS_NEXT` 低位、把落在哨兵位的 index 推过一格、按 head 的 lap 把整体旋转到「head 落在第 0 圈」，最后用公式 `tail - head - tail / LAP` 扣除每一圈的那个哨兵。

最终公式里的 `tail / LAP` 是「tail 跨越的整圈数」，每一圈恰有一个哨兵不存任务，所以要减掉。

#### 4.4.2 核心流程

`len()` 的完整逻辑（[src/deque.rs:1988-2021](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1988-L2021)）：

```
loop:
    tail = tail.index.load(SeqCst)
    head = head.index.load(SeqCst)
    若 tail.index.load(SeqCst) == tail:           # 一致快照
        tail &= !((1<<SHIFT)-1)                   # 清 HAS_NEXT 低位
        head &= !((1<<SHIFT)-1)
        若 (tail>>SHIFT) & (LAP-1) == LAP-1:       # tail 落在哨兵位
            tail += 1 << SHIFT                      # 推过哨兵
        若 (head>>SHIFT) & (LAP-1) == LAP-1:       # head 落在哨兵位
            head += 1 << SHIFT
        lap = (head>>SHIFT) / LAP                  # head 在第几圈
        tail -= (lap*LAP) << SHIFT                 # 旋转到 head 同圈
        head -= (lap*LAP) << SHIFT
        tail >>= SHIFT                             # 折算成逻辑位置
        head >>= SHIFT
        return tail - head - tail / LAP            # 减掉每圈一个哨兵
```

#### 4.4.3 源码精读

一致性循环的入口，在 [src/deque.rs:1989-1995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1989-L1995)：

```rust
loop {
    // 先读 tail，再读 head
    let mut tail = self.tail.index.load(Ordering::SeqCst);
    let mut head = self.head.index.load(Ordering::SeqCst);
    // 若 tail 没变，说明拿到了一致的一对索引
    if self.tail.index.load(Ordering::SeqCst) == tail {
        ...
    }
}
```

注意这里三次加载都用 `SeqCst`。为什么不复用 `is_empty` 的简单比较？因为 `len()` 需要一个**数值**，对一致性要求更高；`is_empty` 只需要判等，容忍度高很多（见 4.5）。

清低位与哨兵修正，在 [src/deque.rs:1996-2006](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1996-L2006)：

```rust
// 抹掉低位（HAS_NEXT）
tail &= !((1 << SHIFT) - 1);
head &= !((1 << SHIFT) - 1);

// 若落在块尾哨兵，修正到下一块的起点
if (tail >> SHIFT) & (LAP - 1) == LAP - 1 {
    tail = tail.wrapping_add(1 << SHIFT);
}
if (head >> SHIFT) & (LAP - 1) == LAP - 1 {
    head = head.wrapping_add(1 << SHIFT);
}
```

- `(1 << SHIFT) - 1 = 1`，`& !1` 清掉最低位（即 `HAS_NEXT`）。
- `(index >> SHIFT) & (LAP - 1)` 取出块内偏移；若等于 `LAP - 1 = 63`（哨兵），就把 index 加一格推过哨兵。这正是为了应对 tail 在 push 跨块安装时瞬态停在哨兵的情况（见 [src/deque.rs:1423-1429](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1423-L1429)）。

lap 旋转与最终计算，在 [src/deque.rs:2008-2018](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2008-L2018)：

```rust
// 旋转，使 head 落在第一圈
let lap = (head >> SHIFT) / LAP;
tail = tail.wrapping_sub((lap * LAP) << SHIFT);
head = head.wrapping_sub((lap * LAP) << SHIFT);

// 去掉低位
tail >>= SHIFT;
head >>= SHIFT;

// 差值减去 head 与 tail 之间的 block 数（每块一个哨兵）
return tail - head - tail / LAP;
```

为什么要 `tail / LAP`？把 tail 折算成逻辑位置后，它跨越的每一整圈（64 个位置）里都有 1 个哨兵不存任务。`tail / LAP` 就是「tail 跨过的整圈数」，正好等于需要扣除的哨兵总数。

**数值验证**：push 65 个任务后，`head=0`，`tail=132`（前 63 个 push 让 tail 越过块 0 的哨兵跳到 128，再 2 个 push 到 132）：

- 清低位：`tail=132`，`head=0`。
- 偏移检查：`(132>>1)&63 = 66&63 = 2`，非 63，不修正。
- `lap = (0>>1)/64 = 0`，不旋转。
- 折算：`tail = 66`，`head = 0`。
- 返回 `66 - 0 - 66/64 = 66 - 1 = 65`。✓

#### 4.4.4 代码实践

**实践目标**：用 `len()` 观察 Injector 在批量偷取前后的长度变化，验证 4.2 推演的 `advance`。

**操作步骤**（见本讲综合实践的完整可运行版本，此处给出关键断言片段）：

```rust
// 示例代码：验证 len() 的变化
let q = Injector::new();
assert_eq!(q.len(), 0);
for i in 1..=6 { q.push(i); }
assert_eq!(q.len(), 6);

let w = Worker::new_fifo();
let stolen = q.steal_batch_with_limit_and_pop(&w, 2);
assert_eq!(stolen, Steal::Success(1));   // 直接弹出任务 1
assert_eq!(q.len(), 4);                   // 队列里还剩 4 个（advance=2）

assert_eq!(w.pop(), Some(2));             // 批次里剩下的任务 2
assert_eq!(w.pop(), None);
assert_eq!(q.len(), 4);                   // w 的 pop 不影响 Injector
```

**需要观察的现象**：批量偷取让 `q.len()` 从 6 跳到 4（认领 2 格），而目的 `Worker` 上的 `pop` 不会改变 `q.len()`。

**预期结果**：上述所有断言通过。若某次 `steal_batch_with_limit_and_pop` 返回 `Steal::Retry`（极少见的并发竞争），用 u3-l3 / 测试里的 `busy_retry` 包装重试即可。**完整运行结果待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`len()` 为什么要「读 tail → 读 head → 再读 tail」三次，而不是两次？

**参考答案**：第一次读 tail、读 head 之后，无法保证 head 和 tail 属于同一时刻（head 读取期间 tail 可能已被改）。再读一次 tail 并与第一次比较：若相等，则「第一次 tail → 读 head → 第二次 tail」整段时间内 tail 都未变，head 也一定属于这段稳定区间，从而保证 head 与 tail 一致；若不等就重来。

**练习 2**：最终公式为什么是 `tail - head - tail / LAP`，而不是直接 `tail - head`？

**参考答案**：因为每一圈（每个 block 的 64 个位置）里有 1 个哨兵位（offset 63）不存任务。`tail - head` 会把这些哨兵也算成任务，多算的数量恰好等于 tail 跨过的整圈数 `tail / LAP`，所以要从差值里减掉它。

---

### 4.5 is_empty 与 Injector::Drop

#### 4.5.1 概念说明

- **`is_empty()`**：只判定「队列此刻是否为空」，不需要精确数值，所以用最简单的 `SeqCst` 双加载后比较逻辑位置是否相等，没有 `len()` 那套修正。
- **`Drop`**：当 `Injector` 被析构（通常在所有线程退出、`Arc` 计数归零后）时，遍历 `head..tail` 把残留任务逐个 `assume_init_drop`，并在每个块尾释放整个 block。因为此时拥有 `&mut self` 的独占访问，全程用 `get_mut()` 跳过原子操作，直接读裸值。

注意 Injector 的 block **不经过 epoch 回收**（那是 Worker 的 `Buffer` 才用的机制，见 u2-l5 / u4-l2），block 全程用裸 `Box` 指针管理，`Drop` 里直接 `Box::from_raw` 释放。

#### 4.5.2 核心流程

`is_empty`（[src/deque.rs:1967-1971](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1967-L1971)）：

```
head = head.index.load(SeqCst)
tail = tail.index.load(SeqCst)
return (head >> SHIFT) == (tail >> SHIFT)
```

`Drop`（[src/deque.rs:2024-2057](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2024-L2057)）：

```
head, tail, block = get_mut() 取裸值
清掉 head/tail 的 HAS_NEXT 低位
while head != tail:
    offset = (head>>SHIFT) % LAP
    若 offset < BLOCK_CAP:           # 真实槽位
        slot.assume_init_drop()       # drop 任务
    否则 (offset == BLOCK_CAP):       # 哨兵 → 本块消费完
        next = block.next
        drop(Box::from_raw(block))    # 释放本块
        block = next
    head += 1 << SHIFT
drop(Box::from_raw(block))           # 释放最后一块
```

#### 4.5.3 源码精读

`is_empty`，在 [src/deque.rs:1967-1971](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1967-L1971)：

```rust
pub fn is_empty(&self) -> bool {
    let head = self.head.index.load(Ordering::SeqCst);
    let tail = self.tail.index.load(Ordering::SeqCst);
    head >> SHIFT == tail >> SHIFT
}
```

- 两次 `SeqCst` 加载建立强同步；比较 `>>SHIFT` 后的逻辑位置（忽略 `HAS_NEXT` 低位）。
- 它**不做一致性重读**：即使读到的 head 与 tail 不是同一时刻，也只可能让 `is_empty` 返回一个「略过期」的布尔值——这在并发语义上是可接受的（调用方本就知道结果是瞬态的）。`len()` 则不行，因为它要返回数值。

`Drop`，在 [src/deque.rs:2024-2057](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2024-L2057)：

```rust
impl<T> Drop for Injector<T> {
    fn drop(&mut self) {
        let mut head = *self.head.index.get_mut();
        let mut tail = *self.tail.index.get_mut();
        let mut block = *self.head.block.get_mut();

        // 清掉低位
        head &= !((1 << SHIFT) - 1);
        tail &= !((1 << SHIFT) - 1);

        unsafe {
            while head != tail {
                let offset = (head >> SHIFT) % LAP;

                if offset < BLOCK_CAP {
                    // 真实槽位：drop 任务
                    let slot = (*block).slots.get_unchecked(offset);
                    (*slot.task.get()).assume_init_drop();
                } else {
                    // 哨兵：释放本块，切到下一块
                    let next = *(*block).next.get_mut();
                    drop(Box::from_raw(block));
                    block = next;
                }

                head = head.wrapping_add(1 << SHIFT);
            }
            // 释放最后一块
            drop(Box::from_raw(block));
        }
    }
}
```

要点：

1. **`get_mut()` 跳过原子操作**：`Drop` 拿到 `&mut self`，意味着没有任何其他线程能访问（Rust 借用规则保证），所以直接 `*AtomicUsize::get_mut()` 取裸值，无需 `load` / 内存序。
2. **按 `offset < BLOCK_CAP` 分流**：真实槽位（offset `0..=62`）调 `assume_init_drop` 析构任务；哨兵位（offset 63）不存任务，而是触发本块的释放与 `block = next` 切换。
3. **`head += 1 << SHIFT` 逐格推进**：每步推进一格逻辑位置，遇到哨兵就换块，直到 `head == tail`。
4. **释放最后一块**：循环结束时 `block` 指向 tail 所在的块，它可能还有未使用的槽位（但已无残留任务），必须单独释放，否则会内存泄漏。
5. **不走 epoch**：整个析构是确定性的单线程释放，不需要延迟回收。这与 `Worker` 的 `Inner::drop`（用 `epoch::unprotected()` 逐个 drop 残留任务再 `dealloc` buffer，见 u2-l1）思路类似，但 Injector 的 block 直接用 `Box::from_raw` 释放。

> 安全性提示：`assume_init_drop` 假设槽位里确实有一个已初始化的 `T`。这成立的前提是 `head..tail` 之间的真实槽位都被 push 写过、且未被 steal 取走——这正是 Injector 的不变式：head 之前（含）的任务已被消费者取走，head 到 tail 之间的任务仍存活。

#### 4.5.4 代码实践

**实践目标**：验证 `is_empty` 与 `len` 在边界上的一致性，并理解 `Drop` 不会泄漏任务。

**操作步骤**（源码阅读 + 推演）：

1. 阅读 `tests/injector.rs` 中的 `is_empty` 测试（[tests/injector.rs:38-57](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L38-L57)），观察它如何交替 push / steal 并断言 `is_empty`。
2. 推演：若 Injector 中残留 N 个任务时被 drop，`Drop` 的 while 循环会执行多少次 `assume_init_drop`、多少次 `Box::from_raw`？
   - 设 N 个任务全部在同一个 block 内（N ≤ 63）：`assume_init_drop` 恰好 N 次，`Box::from_raw` 在循环外 1 次（释放最后一块），循环内 0 次（因为 head 不会推进到哨兵）。
3. 运行 `cargo test --doc injector`（运行 Injector 的所有文档示例，含 `len`/`is_empty` 的 doctest）。

**需要观察的现象**：`is_empty` 与 `len() == 0` 在无并发时结论一致；有并发时 `is_empty` 可能比 `len` 「滞后」一个观察窗口。

**预期结果**：doctest 与 `is_empty` 测试通过。**完整运行结果待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`is_empty` 为什么不做 `len()` 那样的「重读校验」？

**参考答案**：`is_empty` 只返回布尔值，对一致性的容忍度高——即使读到略微过期的 head/tail，至多返回一个「瞬时非精确」的空/非空判断，调用方本就预期并发结果是瞬态的。而 `len()` 要返回具体数值，撕裂读会算出明显错误（甚至为负）的长度，所以必须做一致性校验。

**练习 2**：`Drop` 里为什么用 `get_mut()` 而不是 `load(Ordering::SeqCst)`？

**参考答案**：`Drop` 的签名是 `&mut self`，Rust 借用规则保证此刻没有其他线程能访问该 Injector（通常意味着所有消费者/生产者线程已退出、`Arc` 计数归零）。因此可以安全地用 `get_mut()` 直接取裸值，省掉原子操作和内存序的开销。

**练习 3**：假设 Injector 中残留 130 个任务（跨越 3 个 block），`Drop` 会调用多少次 `Box::from_raw`？

**参考答案**：head 到 tail 之间会经过 2 个哨兵位（block 0 和 block 1 各一个，block 2 未到哨兵），所以循环内 `Box::from_raw` 2 次（释放 block 0、block 1），循环外 1 次（释放最后的 block 2），共 3 次，对应 3 个 block；`assume_init_drop` 恰好 130 次。

---

## 5. 综合实践

把本讲的四个主题（批量偷取 + len 观察 + is_empty + Drop 回收）串成一个完整可运行的小测试。建议把它加入你自己的小项目（依赖 `crossbeam-deque = "0.8"`），或参照 `tests/injector.rs` 的风格写一个 `#[test]`。

```rust
// 示例代码：综合实践 —— Injector 批量偷取 + len/is_empty/Drop 观察
use crossbeam_deque::{Injector, Steal, Worker};

fn busy_retry<T>(mut f: impl FnMut() -> Steal<T>) -> Steal<T> {
    loop {
        let s = f();
        if !s.is_retry() { return s; }
    }
}

fn main() {
    let q = Injector::new();
    assert!(q.is_empty());
    assert_eq!(q.len(), 0);

    // 1. 注入 1..=6
    for i in 1..=6 {
        q.push(i);
    }
    assert!(!q.is_empty());
    println!("push 1..=6 后 len = {}", q.len()); // 期望 6

    // 2. 批量偷取并弹出一个：对照 src/deque.rs:1792-1794 的文档示例
    let w = Worker::new_fifo();
    let stolen = busy_retry(|| q.steal_batch_with_limit_and_pop(&w, 2));
    assert_eq!(stolen, Steal::Success(1)); // 直接弹出队头任务 1
    println!("steal_batch_with_limit_and_pop(.., 2) 后 len = {}", q.len()); // 期望 4

    // 3. 批次里剩下的任务进入 w
    assert_eq!(w.pop(), Some(2)); // advance=2，其中 1 个弹出，剩 1 个进 w
    assert_eq!(w.pop(), None);
    println!("w 取空后 q.len = {}", q.len()); // 仍是 4（w 的 pop 不影响 Injector）

    // 4. 再做一次不带弹出的批量偷取，对照 src/deque.rs:1587-1591
    let w2 = Worker::new_fifo();
    let _ = busy_retry(|| q.steal_batch_with_limit(&w2, 2));
    println!("steal_batch_with_limit(.., 2) 后 w2.len = {}", w2.len()); // 期望 2（任务 3、4）
    assert_eq!(w2.len(), 2);

    // 5. q 被析构时，Drop 会释放剩余任务（5、6）和所有 block —— 无需我们手动处理
    println!("剩余任务 {} 个将在 q 离开作用域时由 Drop 释放", q.len());
}
```

**实践要点**：

1. 用 `busy_retry` 包装可能返回 `Steal::Retry` 的偷取操作，把并发竞争转化成确定性断言（这正是 `tests/injector.rs` 的做法，见 [tests/injector.rs:13-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L13-L20)）。
2. 对照两处文档示例验证数量：`steal_batch_with_limit_and_pop`（[src/deque.rs:1792-1794](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1792-L1794)）与 `steal_batch_with_limit`（[src/deque.rs:1587-1591](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1587-L1591)）。
3. 最后让 `q` 离开作用域，观察 `Drop` 静默回收——你可以临时把任务类型换成带打印的 `Drop` 实现（例如包一层 `struct DropLog(u32)`），亲眼看到剩余任务被析构。

**预期结果**：所有断言通过，`len` 输出依次为 `6 → 4 → 4`，`w2.len()` 为 `2`。完整运行结果**待本地验证**。

---

## 6. 本讲小结

- Injector 的四个批量偷取方法是「弹不弹出 × 上限可否指定」的二维矩阵；不带 `_with_limit` 的两个是转发壳（`steal_batch → MAX_BATCH`、`steal_batch_and_pop → MAX_BATCH+1`），真实实现只有两个，共享同一套「加载 head → 算 advance → CAS 推进 → reserve → 拷贝 → 发布 → 销毁」骨架。
- `advance` 遵循「偷约一半（`len.div_ceil(2)`）」或「偷到块尾（`BLOCK_CAP - offset`）」两条策略，并受 `limit` 上限约束；`batch_size` 在不弹出时等于 `advance`，在弹出时等于 `advance - 1`（有一格直接弹出返回）。
- 拷贝写入时源端永远按 `offset..` 顺序读；FIFO 目的保持原序（`dest_b + i`），LIFO 目的反转顺序（`dest_b + (batch_size-1-i)`）；末尾用 `Release fence + Relaxed store`（tsan 下改 `Release store`）发布目的 `back`。
- 跨块切换发生在 `new_offset == BLOCK_CAP` 时：剥 `HAS_NEXT`、越过哨兵、用 `Release` 成对更新 `head.block` 与 `head.index`；Block 销毁由「到块尾直接 destroy」或「逐个打 READ、命中 DESTROY 则接力 destroy」两条路径触发。
- `len()` 用「读 tail → 读 head → 再读 tail 校验一致」的循环避免撕裂读，再清 `HAS_NEXT` 低位、修正落在哨兵位的 index、按 head 的 lap 旋转，最终用 `tail - head - tail / LAP` 扣除每圈一个哨兵。
- `is_empty` 只做 `SeqCst` 双加载比较逻辑位置，无需重读校验；`Drop` 借 `&mut self` 用 `get_mut()` 跳过原子操作，遍历 `head..tail` 在真实槽位 `assume_init_drop`、在哨兵位释放 block，最后释放 tail 所在块——全程不走 epoch。

---

## 7. 下一步学习建议

- **横向串讲内存序**：本讲反复出现的 `Acquire/Release/SeqCst` 配对、tsan 双路径，正是 [u4-l1 内存序与 volatile hack](u4-l1-memory-ordering.md) 的主题。建议接着读它，把 Injector 批量偷取里的每一次 fence / store 在「happens-before」图上对齐。
- **理解 epoch 与 Injector block 回收的差异**：本讲的 `Drop` 是确定性单线程释放，而 Worker 的 `Buffer` 靠 epoch 延迟回收。对照 [u4-l2 crossbeam-epoch 与 Buffer 生命周期](u4-l2-epoch-gc.md)，弄清「为何 Injector 不需要 epoch」。
- **测试体系**：本讲引用了 `tests/injector.rs` 的 `busy_retry`、`is_empty` 等测试。[u4-l4 测试体系](u4-l4-testing.md) 会系统梳理 smoke / spsc / stampede / mpmc 四类测试与 Miri 技巧，值得通读一遍。
- **动手实战**：把本讲的批量偷取与 [u1-l4](u1-l4-stealer-injector-steal-workflow.md) 的 `find_task` 模式结合，尝试 [u4-l5 用 find_task 构建调度器](u4-l5-build-work-stealing-scheduler.md)，体会 `Injector` 作为全局任务入口的真实用法。
