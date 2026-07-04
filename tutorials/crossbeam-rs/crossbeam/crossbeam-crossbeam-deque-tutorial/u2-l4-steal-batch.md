# 批量偷取：steal_batch 与 steal_batch_and_pop

## 1. 本讲目标

上一讲（u2-l3）我们读完了 `Stealer::steal`——**一次只偷一个任务**的乐观两步偷取。本讲把视角放大：`Stealer` 还能**一次偷走「约一半」任务**，整批倒进另一个 `Worker` 的本地队列里。这就是 work-stealing 调度器「空闲线程一次性搬半筐活儿回来」的核心机制，也是 `find_task` 回退链里 `global.steal_batch_and_pop(local)` 这一步的真正实现。

本讲围绕 `Stealer` 的**四个批量偷取方法**展开：

```
steal_batch(&dest)                       → Steal<()>
steal_batch_with_limit(&dest, limit)     → Steal<()>
steal_batch_and_pop(&dest)               → Steal<T>
steal_batch_with_limit_and_pop(&dest, limit) → Steal<T>
```

读完本讲，你应该能够：

1. 说出这四个方法的两层「二选一」关系：**「要不要弹出一个任务返回」**（`steal_batch` 不返回任务、`steal_batch_and_pop` 返回一个）与**「要不要给批量加上限」**（带 `_with_limit` 的版本接受 `limit` 参数，不带的上限为常量 `MAX_BATCH = 32`）。
2. 解释批量大小公式——`steal_batch_with_limit` 用 `min(len.div_ceil(2), limit)`，而 `steal_batch_with_limit_and_pop` 用 `min((len - 1) / 2, limit - 1)`——并理解它们其实都在偷「约一半（`ceil(len/2)`）」，后者只是把其中 1 个任务直接弹给你、剩下 `ceil(len/2) - 1` 个倒进 `dest`。
3. 理解源 `Flavor` 决定了两条**完全不同**的实现路径：**FIFO 源**用一次性批量拷贝 + 单个 CAS 一次性认领整批；**LIFO 源**因为 owner 可能从 `back` 端并发 `pop`，只能**逐个 CAS、逐个读槽位**。
4. 解释为什么把任务偷进 FIFO 目的队列时**要（或不要）反转顺序**，并掌握本讲最重要的不变式：**被偷批次在目的队列里被消费的相对顺序只由「源 flavor」决定，与目的 flavor 无关**。
5. 说清 `Arc::ptr_eq` 的「同队列短路」分支在 `steal_batch` 与 `steal_batch_and_pop` 中行为为何不同，以及批量偷取末尾「`Release fence` + 写目的 `back`」这一收尾的内存序含义。

本讲**不**展开 `resize` / `reserve` 内部的 epoch 回收细节（留到 u2-l5 与 u4-l2），也**不**展开 `Injector` 的批量偷取（那是另一套 block 链表实现，留到 u3）。

## 2. 前置知识

### 2.1 复习：双游标模型与「谁能动什么」

Chase-Lev 队列用两个只增不减（指其差值方向）的 `AtomicIsize` 游标表达队列内容：

\[ \text{len} = \text{back} - \text{front} \quad(\text{包回绕}) \]

- `front`：队头。**owner 的 FIFO `pop`（`fetch_add`）与所有 stealer 的偷取都会推进它。**
- `back`：队尾。**只有 owner 写**（`push` 加 1；LIFO `pop` 减 1）。
- 任务实体存在环形 `Buffer` 的槽位 `[front, back)` 区间里。

批量偷取从 **`front` 端**整段搬走（无论源是 FIFO 还是 LIFO，偷取永远从队头拿），这正是它与 owner 的 LIFO `pop`（从 `back` 端拿）能并发的根本原因。再用一张表强化「谁能动什么」：

| 字段 | 谁会写 | 谁会读 |
|------|--------|--------|
| `back` | **只有 owner**（`push` / LIFO `pop`） | owner 与所有 stealer |
| `front` | owner（FIFO `pop` / LIFO 末元素 CAS）与 stealer（偷取） | owner 与所有 stealer |
| `buffer`（`Inner` 里） | owner（`resize` 时 `swap`） | stealer（偷取时 `load`） |

`Stealer` 与 `Worker` 共享同一份 `Arc<CachePadded<Inner<T>>>`（[src/deque.rs:574-583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L574-L583)），区别在于 `Worker` 单线程私有、`Stealer` 可跨线程共享。

### 2.2 复习：单任务偷取的两步与 `Retry`

上一讲 `Stealer::steal` 的骨架是「读任务 → CAS 推进 `front` → 再校验 `buffer` 是否被换」。批量偷取**复用了完全相同的并发正确性骨架**，区别只在于「读一个」变成「读一批」、「CAS 加 1」变成「CAS 加 `batch_size`」。所以请先回忆两个关键点：

- **`Steal::Retry`** 表示「伪失败，需立即重试」——CAS 被别人抢先、或 `buffer` 被 `resize` 换掉时返回 `Retry`，而**不是** `Empty`。`Retry` 在 `or_else` / `FromIterator` 组合子里会被放大，不会被 `Empty` 吞掉。
- **`epoch::pin()` 顺带发一道 `SeqCst` fence**，并且 `is_pinned()` 为真（可重入）时要**手动补**这道 fence。

这两点在本讲的 `steal_batch_with_limit` / `steal_batch_with_limit_and_pop` 里原样出现，本讲不再重复论证。

### 2.3 两个本讲要用到的小工具

- **`Buffer::write / read`**：槽位读写用 `ptr::write_volatile / read_volatile` 而非原子操作（[src/deque.rs:72-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90)）。批量拷贝时，源槽位用 `read`、目的槽位用 `write`。
- **`cmp::min` + `div_ceil`**：算批量大小。`div_ceil(2)` 即「除以 2 向上取整」，`(len - 1) / 2` 是整数除法「向下取整」。

> 一句话：本讲可以当成「把 u2-l3 的单任务偷取，扩展成『一次偷约一半』并整批倒进另一个队列』」来读，所有内存序结论都直接继承自 u2-l3。

## 3. 本讲源码地图

本讲全部集中在一个文件 `src/deque.rs`，且四个方法其实是**两个**「带 limit 的实现」加上**两个**「转发壳」：

| 位置 | 内容 | 本讲作用 |
|------|------|----------|
| [src/deque.rs:18-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L18-L23) | 常量 `MIN_CAP=64`、`MAX_BATCH=32`、`FLUSH_THRESHOLD_BYTES=1024` | `MAX_BATCH` 是不带 limit 版本的上限 |
| [src/deque.rs:72-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90) | `Buffer::write / read`（volatile 读写） | 批量拷贝的源/目的槽位操作 |
| [src/deque.rs:326-350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L326-L350) | `Worker::reserve` | 偷取前给目的队列**预留容量**，必要时触发 `resize` |
| [src/deque.rs:708-710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L708-L710) | `steal_batch` | 转发壳：`steal_batch_with_limit(dest, MAX_BATCH)` |
| [src/deque.rs:746-925](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L746-L925) | `steal_batch_with_limit` | **本讲主菜之一**：不返回任务的批量偷取 |
| [src/deque.rs:949-951](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L949-L951) | `steal_batch_and_pop` | 转发壳：`steal_batch_with_limit_and_pop(dest, MAX_BATCH)` |
| [src/deque.rs:989-1178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L989-L1178) | `steal_batch_with_limit_and_pop` | **本讲主菜之二**：偷一批并额外弹出一个任务返回 |
| [tests/steal.rs:55-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L55-L211) | 四种 flavor 组合的批量偷取断言 | 本讲代码实践与不变式的「真值来源」 |

下面四个最小模块逐个拆解。

---

## 4. 核心概念与源码讲解

### 4.1 四个 API、`MAX_BATCH` 与「偷一半」的批量大小

#### 4.1.1 概念说明

四个方法名看起来很多，其实是两个正交维度的组合：

| | 不弹出任务 | 弹出 1 个任务返回 |
|---|---|---|
| **上限固定** | `steal_batch(&dest)` | `steal_batch_and_pop(&dest)` |
| **上限可指定** | `steal_batch_with_limit(&dest, limit)` | `steal_batch_with_limit_and_pop(&dest, limit)` |

- **维度一：弹不弹出。** `steal_batch` 把整批都倒进 `dest`，返回 `Steal<()>`（成功时只是个空元组）；`steal_batch_and_pop` 偷一批、把其中**一个任务**直接返回给你（`Steal<T>`），其余倒进 `dest`。后者正是 `find_task` 里 `global.steal_batch_and_pop(local)` 想要的：偷一批回来，立刻弹一个去执行，剩下的留在本地队列。
- **维度二：上限。** 不带 `_with_limit` 的两个方法都是转发壳，把上限固定为常量 `MAX_BATCH`（[src/deque.rs:20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L20)）。真正干活的是带 `_with_limit` 的两个。

**「偷约一半」**是 crossbeam 的批量策略：偷太多会与源队列的 owner 反复抢同一个缓存行，偷太少又达不到「搬一次顶多次单偷」的效果。经验上「偷当前长度的一半」是个好折中，所以两个公式都围绕「一半」来。

#### 4.1.2 核心流程

批量大小在两个实现里分别这样算（`len = back - front` 为偷取瞬间观测到的源队列长度）：

- `steal_batch_with_limit`：
  \[ \text{batch\_size} = \min\!\big(\lceil \text{len}/2 \rceil,\ \text{limit}\big) \]
  这 `batch_size` 个任务**全部**倒进 `dest`。

- `steal_batch_with_limit_and_pop`：
  \[ \text{batch\_size} = \min\!\big(\lfloor (\text{len}-1)/2 \rfloor,\ \text{limit}-1\big) \]
  这 `batch_size` 个倒进 `dest`，**外加 1 个**直接弹出返回。所以**总共偷走**：
  \[ 1 + \lfloor (\text{len}-1)/2 \rfloor = \lceil \text{len}/2 \rceil \]

也就是说，**两个方法总共都偷走 `ceil(len/2)` 个任务**，区别只是 `and_pop` 把其中 1 个直接交到你手上、其余 `ceil(len/2) - 1` 个进 `dest`。`limit` 在 `and_pop` 里减 1，是为了保证「弹出的那 1 个 + 进 dest 的」加起来不超过用户给的 `limit`。

算出 `batch_size` 后，立刻调用 `dest.reserve(batch_size)` 给目的队列预留容量（[src/deque.rs:781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L781)），保证后续往 `dest` 写槽位时不会越界——容量不够就提前 `resize` 翻倍（resize 机制留到 u2-l5）。

#### 4.1.3 源码精读

先看两个转发壳——它们极其薄，只是把 `MAX_BATCH` 当默认上限传下去（[src/deque.rs:708-710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L708-L710)、[src/deque.rs:949-951](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L949-L951)）：

```rust
pub fn steal_batch(&self, dest: &Worker<T>) -> Steal<()> {
    self.steal_batch_with_limit(dest, MAX_BATCH)
}
// ...
pub fn steal_batch_and_pop(&self, dest: &Worker<T>) -> Steal<T> {
    self.steal_batch_with_limit_and_pop(dest, MAX_BATCH)
}
```

常量定义（[src/deque.rs:18-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L18-L23)）——注意 `MAX_BATCH` 注释明确写了它就是这两个批量偷取的上限：

```rust
const MIN_CAP: usize = 64;
// Maximum number of tasks that can be stolen in `steal_batch()` and `steal_batch_and_pop()`.
const MAX_BATCH: usize = 32;
const FLUSH_THRESHOLD_BYTES: usize = 1 << 10;
```

再看两个公式本身。`steal_batch_with_limit`（[src/deque.rs:780-782](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L780-L782)）：

```rust
let batch_size = cmp::min((len as usize).div_ceil(2), limit);
dest.reserve(batch_size);
let mut batch_size = batch_size as isize;
```

`steal_batch_with_limit_and_pop`（[src/deque.rs:1022-1024](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1022-L1024)）——注意是 `(len - 1) / 2` 与 `limit - 1`：

```rust
let batch_size = cmp::min((len as usize - 1) / 2, limit - 1);
dest.reserve(batch_size);
let mut batch_size = batch_size as isize;
```

两个函数开头都有一句 `assert!(limit > 0)`（[src/deque.rs:747](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L747)、[src/deque.rs:990](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L990)），因为 `limit = 0` 在 `and_pop` 里会让 `limit - 1` 下溢、在 `steal_batch` 里毫无意义。

#### 4.1.4 代码实践

**目标：** 用 `div_ceil` 与整数除法手算几个批量大小，建立对「偷一半」的直觉。

**操作步骤（纯演算，无需运行）：** 假设偷取瞬间观测到 `len`，分别用两个公式算 `batch_size` 与「总共偷走数」：

| `len` | `steal_batch` 进 dest | `steal_batch_and_pop` 进 dest | `and_pop` 弹出 | `and_pop` 总共偷走 |
|------|----------------------|------------------------------|---------------|-------------------|
| 1 | `ceil(1/2)=1` | `(1-1)/2=0` | 1 | 1 |
| 4 | `ceil(4/2)=2` | `(4-1)/2=1` | 1 | 2 |
| 6 | `ceil(6/2)=3` | `(6-1)/2=2` | 1 | 3 |
| 5 | `ceil(5/2)=3` | `(5-1)/2=2` | 1 | 3 |

**预期结果：** 最后一列「总共偷走」始终等于 `ceil(len/2)`；`steal_batch` 把它**全**倒进 dest，`and_pop` 把其中 1 个弹出、其余进 dest。

**待本地验证：** 你可以写一个小程序，往一个 FIFO `Worker` push 6 个任务，调用 `steal_batch_and_pop` 后打印返回值与 `dest` 的 `len()`，确认 `dest.len()` 等于 2、返回值等于队头任务。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `and_pop` 的公式是 `(len - 1) / 2` 而不是 `len / 2`？

**答案：** 因为 `and_pop` **额外**有 1 个任务被直接弹出返回，不计入 `batch_size`。为保证「总共偷走 = `ceil(len/2)`」且不超过 `limit`，进 dest 的部分应是 `ceil(len/2) - 1`。对整数而言 `ceil(len/2) - 1 = (len - 1) / 2`（整数除法向下取整），所以公式写成 `(len - 1) / 2`。

**练习 2：** 若调用 `steal_batch_with_limit(&dest, 1)`，源队列 `len = 10`，实际进 dest 几个？

**答案：** `min(ceil(10/2), 1) = min(5, 1) = 1`，进 dest 1 个。

---

### 4.2 公共前置：同队列短路、`epoch::pin` 与 `buffer` 快照

#### 4.2.1 概念说明

在真正开始读槽位之前，两个 `_with_limit` 实现共享一段**完全相同的前置流程**，它做四件事：

1. **同队列短路**：如果 `self`（源 `Stealer`）和 `dest`（目的 `Worker`）其实是**同一个底层队列**（`Arc::ptr_eq` 为真），就没必要「偷自己」。
2. **Acquire 读 `front`**，并在可重入时补 `SeqCst` fence。
3. **`epoch::pin()`** 进入临界区（顺带发 fence）。
4. **Acquire 读 `back`** 判空；非空则 Acquire 读 `buffer` 快照。

这套流程与 u2-l3 的 `Stealer::steal` 前半段**逐字相同**——因为它要解决的并发问题一样：必须在 `front` 与 `back` 之间夹一道全序屏障，才能安全地读 `buffer` 槽位而不踩到 owner 正在做的 `resize`。

#### 4.2.2 核心流程

```
assert!(limit > 0)
if Arc::ptr_eq(self.inner, dest.inner):    # 同一个底层队列
    # steal_batch:        看 dest 空不空 → Empty / Success(())
    # steal_batch_and_pop: 直接 dest.pop() → Empty / Success(task)
Acquire 读 front = f
if epoch::is_pinned(): atomic::fence(SeqCst)   # 可重入时手动补
let guard = epoch::pin()                        # 顺带发 SeqCst fence
Acquire 读 back = b
len = b - f
if len <= 0: return Steal::Empty
算 batch_size；dest.reserve(batch_size)
取 dest 的 buffer 快照 dest_buffer 与 dest 的 back = dest_b（Relaxed）
Acquire 读源 buffer 快照 = buffer（带 guard）
# ↓ 进入 4.3 / 4.4 的 flavor 分支
```

#### 4.2.3 源码精读

**同队列短路。** `steal_batch_with_limit`（[src/deque.rs:748-754](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L748-L754)）：

```rust
if Arc::ptr_eq(&self.inner, &dest.inner) {
    if dest.is_empty() {
        return Steal::Empty;
    } else {
        return Steal::Success(());
    }
}
```

`steal_batch_with_limit_and_pop`（[src/deque.rs:991-996](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L991-L996)）——注意它直接调用 `dest.pop()`，因为 `and_pop` 本来就要返回一个任务：

```rust
if Arc::ptr_eq(&self.inner, &dest.inner) {
    match dest.pop() {
        None => return Steal::Empty,
        Some(task) => return Steal::Success(task),
    }
}
```

这两个分支的差异正是「维度一（弹不弹出）」在最边缘情况下的体现：偷自己时，`steal_batch` 只关心「有没有货」，`and_pop` 则索性把货弹一个出来。

> 为何需要这个短路？因为后面要 `Acquire` 读 `self.inner.buffer` 并 CAS `self.inner.front`，若 `self` 与 `dest` 是同一个 `Arc`，等于「一边从队头偷、一边写回同一个队列」，逻辑上无意义且会把任务原地「转圈」。短路直接退化成「看看（或弹一个）本地队列」。

**front / fence / pin / back / buffer。** 两个实现这段几乎逐字相同，以 `steal_batch_with_limit` 为例（[src/deque.rs:757-789](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L757-L789)）：

```rust
let mut f = self.inner.front.load(Ordering::Acquire);
if epoch::is_pinned() {
    atomic::fence(Ordering::SeqCst);
}
let guard = &epoch::pin();
let b = self.inner.back.load(Ordering::Acquire);
let len = b.wrapping_sub(f);
if len <= 0 {
    return Steal::Empty;
}
let batch_size = cmp::min((len as usize).div_ceil(2), limit);
dest.reserve(batch_size);
let mut batch_size = batch_size as isize;
let dest_buffer = dest.buffer.get();
let mut dest_b = dest.inner.back.load(Ordering::Relaxed);
let buffer = self.inner.buffer.load(Ordering::Acquire, guard);
```

注意三个细节：

- `dest_b` 用 **`Relaxed`** 读，因为 `dest` 是**当前线程私有**的 `Worker`（`!Sync`），没有别的线程会改它的 `back`，无需同步。
- `dest.buffer.get()` 取的是 `Worker` 私有的 `Cell<Buffer<T>>` 副本（u1-l3 讲过的「快速访问副本」），`reserve` 必要时会更新它。
- 源 `buffer` 用 `Acquire` + `guard` 读，`guard` 保证在这次偷取期间该 `buffer` 不会被 `resize` 真正释放（epoch GC，详见 u4-l2）。

`steal_batch_with_limit_and_pop` 在读 `buffer` 之前还多读了一句「队头任务」（[src/deque.rs:1034](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1034)），这个任务就是 `and_pop` 准备返回给你的那一个——它的命运在 4.4 讲。

#### 4.2.4 代码实践

**目标：** 观察「同队列短路」分支的行为。

**操作步骤：** 把下面这段**示例代码**加进一个 `#[test]`（或临时 `examples/` 二进制），它故意让 `Stealer` 把任务偷回**自己派生出的同一个 `Worker`**：

```rust
// 示例代码：演示 Arc::ptr_eq 短路
use crossbeam_deque::Worker;

let w = Worker::new_fifo();
w.push(1);
w.push(2);
let s = w.stealer();        // s 与 w 共享同一个 Arc<Inner>
// 把 w 自己当 dest —— 同一个底层队列
let r = s.steal_batch(&w);  // 命中 Arc::ptr_eq 短路
assert!(r.is_success());
assert_eq!(w.len(), 2);     // 任务没被搬走，仍在原处
```

**预期结果：** 因为 `Arc::ptr_eq` 为真，`steal_batch` 走短路：`w` 非空，直接返回 `Success(())`，**不移动任何任务**，`w.len()` 仍是 2。

**待本地验证：** 此实践依赖「`Stealer` 与其源 `Worker` 共享同一 `Arc`」这一事实。若你担心 `w` 同时作为 `&self`(经由 s) 与 `&dest` 出现借用问题，可改为先 `let s = w.stealer();` 再传 `&w`——`steal_batch` 只借 `&Worker`，不冲突。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `dest_b` 用 `Relaxed` 而 `front`/`back` 用 `Acquire`？

**答案：** `dest` 是当前线程私有的 `Worker`（`!Sync`），它的 `back` 只有本线程写，没有跨线程同步需求，`Relaxed` 足够。而源队列的 `front`/`back` 会被其他线程（owner 与别的 stealer）修改，必须用 `Acquire` 与对方的 `Release` 配对建立 happens-before。

**练习 2：** 如果删掉 `Arc::ptr_eq` 短路，调用 `s.steal_batch(&w)`（同队列）会发生什么？

**答案：** 逻辑上会「从队头读一批、再写回队尾同一个 buffer」，任务被原地搬动、`len` 不变但顺序可能错乱，是无意义的空操作（且 `and_pop` 还会错误地推进 `front`）。短路把这个无意义情形直接化解。

---

### 4.3 Flavor::Fifo 源：一次性批量拷贝与单个 CAS

#### 4.3.1 概念说明

前置流程走完后，代码按**源 flavor** 分两条路。本模块讲 **FIFO 源**。

FIFO 源的关键优势：**owner 的 FIFO `pop` 也从 `front` 端拿**，所以一旦我们用 CAS 把 `front` 一次性推进 `batch_size` 步，这整段 `[f, f + batch_size)` 就被我们**独占认领**了——owner 的 `fetch_add` 顶多从更后面拿，不会插进我们这批中间。于是 FIFO 源可以**一次性把整批连续拷贝到 dest，再用一个 CAS 认领**，不必逐个 CAS。

拷贝时还要处理「dest 是 FIFO 还是 LIFO」：因为读源是按 `f, f+1, ...`（队头→队尾）顺序，而 dest 的写入位置要保证「消费顺序」正确。这里的技巧是：**FIFO 目的**直接顺序写（`dest_b + i`），**LIFO 目的**则把槽位**预先反向放置**（`dest_b + (batch_size - 1 - i)`），从而避免事后再做一次反转循环。

#### 4.3.2 核心流程

```
match self.flavor {
  Flavor::Fifo =>
    match dest.flavor {
      Fifo => for i in 0..batch_size:
                task = buffer.read(f + i)          # 源顺序读
                dest_buffer.write(dest_b + i, task) # dest 顺序写
      Lifo => for i in 0..batch_size:
                task = buffer.read(f + i)
                dest_buffer.write(dest_b + (batch_size-1-i), task)  # 预反向
    }
    # 一次性认领：若 buffer 没被换 且 CAS(f → f+batch_size) 成功
    if buffer 变了 || CAS 失败: return Steal::Retry
    dest_b += batch_size
}
# （随后是公共收尾：Release fence + 写 dest.back）
```

`and_pop` 版本的 FIFO 分支几乎一样，唯一区别是：**第一个任务 `read(f)` 不进 dest，而是被「弹出」返回**；拷贝循环从 `f + 1` 开始读，CAS 推进 `batch_size + 1` 步（多出的 1 步就是那个被弹出的任务）。

#### 4.3.3 源码精读

**`steal_batch_with_limit` 的 FIFO 分支**（[src/deque.rs:793-832](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L793-L832)）：

```rust
Flavor::Fifo => {
    match dest.flavor {
        Flavor::Fifo => {
            for i in 0..batch_size {
                unsafe {
                    let task = buffer.deref().read(f.wrapping_add(i));
                    dest_buffer.write(dest_b.wrapping_add(i), task);
                }
            }
        }
        Flavor::Lifo => {
            for i in 0..batch_size {
                unsafe {
                    let task = buffer.deref().read(f.wrapping_add(i));
                    dest_buffer.write(dest_b.wrapping_add(batch_size - 1 - i), task);
                }
            }
        }
    }
    // 一次性认领整批：buffer 没被换 且 CAS 成功
    if self.inner.buffer.load(Ordering::Acquire, guard) != buffer
        || self.inner.front.compare_exchange(
            f, f.wrapping_add(batch_size), Ordering::SeqCst, Ordering::Relaxed,
        ).is_err()
    {
        return Steal::Retry;
    }
    dest_b = dest_b.wrapping_add(batch_size);
}
```

读三件事：

1. **拷贝是无锁的「先拷后认领」。** 先把任务读出来写进 dest 的槽位，**但还没有推进 dest 的 `back`**（`dest_b` 只是本地变量）。只有当 CAS `front` 成功后，才在末尾把 `dest_b` 一次性 `store` 进 `dest.inner.back`（见 4.4 末尾的收尾）。所以若 CAS 失败返回 `Retry`，这些写到 dest 槽位的数据**不会被发布**——它们停在 `back` 之外，会被后续写入覆盖。
2. **二次校验 `buffer`** 与 u2-l3 完全同理：CAS 成功还不够，必须确认我们读的 `buffer` 没被 owner 的 `resize` 换掉，否则我们读到的是旧 buffer 的数据、而 CAS 操作的是新 `front`，会错乱。任一条件不满足都返回 `Retry`。
3. **`and_pop` 版本**（[src/deque.rs:1038-1078](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1038-L1078)）的循环从 `f.wrapping_add(i + 1)` 读起（跳过队头），CAS 推进 `f.wrapping_add(batch_size + 1)`，而队头 `read(f)` 的任务（[src/deque.rs:1034](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1034)）在函数末尾被 `assume_init` 返回。

**为什么 LIFO 目的要「预先反向」写？** 看下面的不变式表（4.4 会给出完整推导）：源 FIFO 时，无论 dest 是 FIFO 还是 LIFO，dest 被 pop 出来的顺序都应是「队头→队尾」（即 `1, 2, ...`）。FIFO 目的 pop 从低索引开始，所以顺序写就对；LIFO 目的 pop 从高索引（`back-1`）开始，所以要把最早读的任务放到**最高**索引位，即 `dest_b + (batch_size - 1)`，于是写成 `dest_b + (batch_size - 1 - i)`。

#### 4.3.4 代码实践

**目标：** 验证 FIFO 源 + FIFO 目的的「顺序不变」与单次 CAS 认领。

**操作步骤：** 对照 `tests/steal.rs` 的 `steal_batch_fifo_fifo`（[src/../tests/steal.rs:55-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L55-L67)），手算：FIFO `w` push `1,2,3,4`，`len=4`，`batch_size = ceil(4/2) = 2`，从 `front` 读 `task1, task2` 顺序写入 `w2`。

**预期结果：** `w2`（FIFO）依次 `pop` 得到 `Some(1)`、`Some(2)`，然后 `None`；源 `w` 剩 `3, 4`。这正是该测试的断言。

**待本地验证：** 你可以 `cargo test --test steal steal_batch_fifo_fifo` 直接运行该断言，观察通过。

#### 4.3.5 小练习与答案

**练习 1：** FIFO 源为什么敢用**一个** CAS 认领整批，而不是像 LIFO 源那样逐个 CAS？

**答案：** 因为 FIFO 源的 owner `pop` 也走 `front` 端（`fetch_add`），我们用 CAS 把 `front` 一次性推进 `batch_size`，就把 `[f, f+batch_size)` 整段独占了，owner 最多从 `f+batch_size` 之后拿，不会插入这批中间。所以一次 CAS 足够保证整批原子认领。

**练习 2：** 若在拷贝循环跑完、CAS 之前，owner 恰好 `resize` 换了 buffer，会怎样？

**答案：** 拷贝已经写进 dest 槽位，但随后 `self.inner.buffer.load(...) != buffer` 为真，命中 `return Steal::Retry`。由于还没推进 dest 的 `back`，那些写到 dest 的数据不被发布，函数返回 `Retry` 让调用者重试，不会造成数据错乱或泄漏。

---

### 4.4 Flavor::Lifo 源：逐个 CAS 与「写入 FIFO 目的需反转」

#### 4.4.1 概念说明

LIFO 源比 FIFO 源麻烦。因为 **LIFO 源的 owner `pop` 从 `back` 端拿**，而偷取从 `front` 端拿——两端可能同时被动。更关键的是：当我们想偷「一批」时，没法像 FIFO 那样「一个 CAS 认领整段」，因为队列长度会在偷取过程中被 owner 的 `push`/`pop` 改变。所以 LIFO 源只能**逐个读槽位、逐个 CAS 推进 `front`**，每偷一个就检查一次「队列空了吗 / buffer 被换了吗」，能偷几个算几个。

逐个偷还有一个副作用：任务按 `f, f+1, ...` 顺序被**顺序写入** dest 的 `dest_b, dest_b+1, ...`。这对 **LIFO 目的**正好（pop 从高索引开始，天然反序消费）；但对 **FIFO 目的**就反了——FIFO pop 从低索引开始会先消费到「最早偷的（队头）」任务，而我们希望它先消费「最后偷的」以保持 LIFO 源的次序。所以**LIFO 源 + FIFO 目的**需要在写完后做一次**反转循环**。

> 关键不变式（本讲最重要的结论）：**被偷批次在目的队列里被消费的相对顺序，只由「源 flavor」决定，与目的 flavor 无关。**
> - 源 FIFO → 消费顺序为队头→队尾（如 `1, 2, 3`）。
> - 源 LIFO → 消费顺序为队尾→队头（如 `3, 2, 1`）。
>
> 4.3 的「FIFO 目的顺序写 / LIFO 目的预反向」和本节的「写完后反转」都是为了让这条不变式在**所有四种 flavor 组合**下成立。

#### 4.4.2 核心流程

```
match self.flavor {
  Flavor::Lifo =>
    original_batch_size = batch_size
    for i in 0..original_batch_size:
      if i > 0:
        atomic::fence(SeqCst)               # 与其它线程同步
        b = back.load(Acquire)
        if b - f <= 0: batch_size = i; break # 队列空了，提前结束
      task = buffer.read(f)                  # 读队头
      if buffer 变了 || CAS(f → f+1) 失败:
        batch_size = i; break                # 这一个没偷到，结束
      dest_buffer.write(dest_b, task)        # 顺序写进 dest
      f += 1; dest_b += 1
    if batch_size == 0: return Steal::Retry  # 一个都没偷到
    if dest.flavor == Fifo:                  # FIFO 目的需反转
      for i in 0..batch_size/2:
        交换 dest_buffer[dest_b - (batch_size-i)] 与 [dest_b - (i+1)]
}
```

`and_pop` 版本的 LIFO 分支更精巧：它先单独 CAS 偷**队头那一个**任务（这个任务最终会返回），再进入逐个偷循环，循环里用 `mem::replace` 把「上一个偷到的」写进 dest、把「新偷到的」留在手上——这样循环结束时手上留的就是**最后一个（最新）**偷到的任务，正好作为返回值。

#### 4.4.3 源码精读

**`steal_batch_with_limit` 的 LIFO 分支**（[src/deque.rs:835-908](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L835-L908)），拆成三段看：

逐个偷循环（[src/deque.rs:839-888](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L839-L888)）：

```rust
let original_batch_size = batch_size;
for i in 0..original_batch_size {
    if i > 0 {
        atomic::fence(Ordering::SeqCst);
        let b = self.inner.back.load(Ordering::Acquire);
        if b.wrapping_sub(f) <= 0 {
            batch_size = i;
            break;
        }
    }
    let task = unsafe { buffer.deref().read(f) };
    if self.inner.buffer.load(Ordering::Acquire, guard) != buffer
        || self.inner.front.compare_exchange(
            f, f.wrapping_add(1), Ordering::SeqCst, Ordering::Relaxed,
        ).is_err()
    {
        batch_size = i;
        break;
    }
    unsafe { dest_buffer.write(dest_b, task); }
    f = f.wrapping_add(1);
    dest_b = dest_b.wrapping_add(1);
}
```

注意 `original_batch_size` 这个临时变量——注释（[src/deque.rs:836-838](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L836-L838)）说明它既是为了避开 clippy「循环里改了循环上限」的告警，也是为了明确「循环次数在进入时就固定」。循环内 `batch_size` 会被改成「实际偷到的个数」，用作后续反转与 `front` 推进的依据。

收尾的「一个都没偷到 → Retry」与「FIFO 目的反转」（[src/deque.rs:891-907](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L891-L907)）：

```rust
if batch_size == 0 {
    return Steal::Retry;
}
if dest.flavor == Flavor::Fifo {
    for i in 0..batch_size / 2 {
        unsafe {
            let i1 = dest_b.wrapping_sub(batch_size - i);
            let i2 = dest_b.wrapping_sub(i + 1);
            let t1 = dest_buffer.read(i1);
            let t2 = dest_buffer.read(i2);
            dest_buffer.write(i1, t2);
            dest_buffer.write(i2, t1);
        }
    }
}
```

反转用的是经典的「头尾两两对调」：`i` 从 0 到 `batch_size/2`，把相对 `dest_b` 偏移 `-(batch_size-i)` 的元素（批次最左）与偏移 `-(i+1)` 的元素（批次最右）交换，`batch_size/2` 次后整批逆序。

**`and_pop` 版本的 LIFO 分支**（[src/deque.rs:1081-1161](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1081-L1161)）多了 `mem::replace` 滑动。先 CAS 偷队头任务（[src/deque.rs:1083-1094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1083-L1094)），再进循环（[src/deque.rs:1102-1146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1102-L1146)）：

```rust
// task 此时持有队头任务（read(f) 的结果）
for i in 0..original_batch_size {
    atomic::fence(Ordering::SeqCst);
    let b = self.inner.back.load(Ordering::Acquire);
    if b.wrapping_sub(f) <= 0 { batch_size = i; break; }
    let tmp = unsafe { buffer.deref().read(f) };
    if /* buffer 变了 || CAS 失败 */ { batch_size = i; break; }
    unsafe {
        dest_buffer.write(dest_b, mem::replace(&mut task, tmp));
    }
    f = f.wrapping_add(1);
    dest_b = dest_b.wrapping_add(1);
}
```

`mem::replace(&mut task, tmp)` 的妙处：它把**当前手上的 `task` 写进 dest**，同时把**新读到的 `tmp` 留在手上**。于是每轮把「上一个」落袋为安、把「这一个」暂存。循环结束时，手上 `task` 就是**最后一个偷到的（最新的）**任务，函数末尾返回它（[src/deque.rs:1177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1177)）：`Steal::Success(unsafe { task.assume_init() })`。

**公共收尾：发布 dest。** 两个 `_with_limit` 函数在 flavor 分支结束后，都要把本地 `dest_b` 写回 `dest.inner.back`（[src/deque.rs:911-924](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L911-L924)）：

```rust
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
dest.inner.back.store(dest_b, store_order);
Steal::Success(())
```

这与 `Worker::push` 的发布顺序同构：**先把整批槽位写好（前面循环里的 `dest_buffer.write`），再发 `Release` fence，最后 `store` 目的 `back`**。这样目的队列的 owner 在「看到 `back` 推进」时，一定也看到了整批写好的任务。`#[cfg(...)]` 的双路径是 ThreadSanitizer 兼容（tsan 不解 fence，改用 `Release store`），详见 u4-l3。

#### 4.4.4 代码实践（本讲主实践）

**目标：** 复现 `tests/steal.rs::steal_batch_and_pop_fifo_fifo`（[src/../tests/steal.rs:141-153](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L141-L153)），验证 FIFO 源下 `and_pop` 弹出队头、其余进 dest。

**操作步骤：** 在 `crossbeam-deque` 仓库里新建一个临时测试文件 `tests/u2l4_practice.rs`（**示例代码**，实践完可删）：

```rust
use crossbeam_deque::{Steal::Success, Worker};

#[test]
fn practice_steal_batch_and_pop_fifo_fifo() {
    let w = Worker::new_fifo();
    for i in 1..=6 {
        w.push(i);
    }
    let s = w.stealer();
    let w2 = Worker::new_fifo();

    // len=6 → batch_size = (6-1)/2 = 2 进 w2，外加弹出 1 个返回
    assert_eq!(s.steal_batch_and_pop(&w2), Success(1)); // 弹出队头 1
    assert_eq!(w2.pop(), Some(2));                        // 批次剩余
    assert_eq!(w2.pop(), Some(3));
    assert_eq!(w2.pop(), None);
}
```

**需要观察的现象：**

1. `steal_batch_and_pop` 返回 `Success(1)`——`read(f)` 读到的正是队头任务 `1`。
2. `w2` 里剩下批次 `{2, 3}`（`batch_size = (6-1)/2 = 2`，从 `f+1, f+2` 读出），FIFO pop 得 `2`、`3`。
3. 源 `w` 此时应剩 `{4, 5, 6}`（可加 `assert_eq!(w.len(), 3);` 验证）。

**预期结果：** 测试通过。运行命令 `cargo test --test u2l4_practice`。

**动手延伸（推荐）：** 把 `Worker::new_fifo()` 全改成 `Worker::new_lifo()`，重跑。此时命中 **LIFO 源 + LIFO 目的**：`steal_batch_and_pop` 会因为 `mem::replace` 滑动而返回 `Success(3)`（最后一个偷到的、最新的任务），`w2.pop()` 得 `2`、`1`。这正是 `steal_batch_and_pop_lifo_lifo`（[src/../tests/steal.rs:156-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L156-L168)）的断言，体现了「源 LIFO → 消费顺序 `3, 2, 1`」的不变式。

#### 4.4.5 小练习与答案

**练习 1：** 用一张表总结 `steal_batch`（源 push `1,2,3,4`，批次为 `{1,2}`）在四种 flavor 组合下，目的 `Worker` 连续 `pop` 的顺序，并验证「消费顺序只由源 flavor 决定」。

**答案：**

| 源 flavor | 目的 flavor | 目的 `pop` 顺序 | 机制 |
|-----------|-------------|-----------------|------|
| FIFO | FIFO | `1, 2` | 顺序写、不反转 |
| FIFO | LIFO | `1, 2` | 预反向写槽位 |
| LIFO | FIFO | `2, 1` | 顺序写 + 反转循环 |
| LIFO | LIFO | `2, 1` | 顺序写、不反转 |

源 FIFO 总是 `1,2`（队头→队尾），源 LIFO 总是 `2,1`（队尾→队头），目的 flavor 不改变相对顺序。这四行分别对应 `tests/steal.rs` 的 `steal_batch_fifo_fifo` / `steal_batch_fifo_lifo` / `steal_batch_lifo_fifo` / `steal_batch_lifo_lifo`（[src/../tests/steal.rs:55-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L55-L112)）。

**练习 2：** LIFO 源逐个偷循环里，为什么 `i > 0` 时要先 `fence` 再 `load back` 判空，而 `i == 0` 时不用？

**答案：** 第一个任务（`i == 0`）的「不空」已经在公共前置里用 `front → fence(pin) → back` 那道屏障保证过了（`len > 0` 才走到这里）。但从第二个开始，队列可能已被 owner 的 `push`/`pop` 改变长度，必须重新发 `SeqCst` fence 同步、再 `Acquire` 读 `back` 重新判空，否则可能读到已经被 owner 拿走的槽位。

**练习 3：** `and_pop` 的 LIFO 分支为什么返回的是「最后一个偷到的」任务，而不是队头任务？

**答案：** 因为 `mem::replace(&mut task, tmp)` 每轮把旧值落袋、新值留手，循环结束时 `task` 持有的是最后一次成功偷到的任务（源 LIFO 中最新的那个）。返回它使整体消费顺序为「最新 → 较旧」（如 `3, 2, 1`），与源 LIFO 的次序一致；若返回队头（最旧的 `1`），顺序就会错乱。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**手算 + 实测对照**」的小任务。

**任务：** 给定一个 FIFO `Worker` `w`，push `1..=6`。请先在纸上预测，然后分别对 `w2 = Worker::new_fifo()` 和 `w2 = Worker::new_lifo()` 调用 `s.steal_batch_and_pop(&w2)`，写下：

1. `steal_batch_and_pop` 的返回值；
2. 随后 `w2` 连续 `pop` 得到的序列；
3. 源 `w` 剩余的 `len()` 与（按 FIFO pop 的）序列。

**纸面预测：**

- `len = 6`，`batch_size = (6-1)/2 = 2`，弹出 1 个 + 进 dest 2 个。
- `w2 = FIFO`：返回 `1`；dest 进 `{2,3}`，FIFO pop → `2, 3`；源剩 `{4,5,6}`（len 3）。
- `w2 = LIFO`：源 FIFO 的 `and_pop` 对 dest-LIFO 走「预反向」写入（[src/deque.rs:1049-1055](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1049-L1055)），dest 槽位排布使 LIFO pop 仍得 `2, 3`；返回值仍是队头 `1`；源剩 `{4,5,6}`。

注意：源 FIFO 时，**无论 dest 是 FIFO 还是 LIFO**，消费顺序都是 `1, 2, 3`——这正是 4.4 的不变式。

**实测：** 写一个 `#[test]`（**示例代码**）跑两遍：

```rust
use crossbeam_deque::{Steal::Success, Worker};

#[test]
fn synth_fifo_dst() {
    let w = Worker::new_fifo();
    for i in 1..=6 { w.push(i); }
    let s = w.stealer();
    let w2 = Worker::new_fifo();
    assert_eq!(s.steal_batch_and_pop(&w2), Success(1));
    assert_eq!(w2.pop(), Some(2));
    assert_eq!(w2.pop(), Some(3));
    assert_eq!(w2.pop(), None);
    assert_eq!(w.len(), 3);
}

#[test]
fn synth_lifo_dst() {
    let w = Worker::new_fifo();
    for i in 1..=6 { w.push(i); }
    let s = w.stealer();
    let w2 = Worker::new_lifo();
    assert_eq!(s.steal_batch_and_pop(&w2), Success(1));
    assert_eq!(w2.pop(), Some(2));
    assert_eq!(w2.pop(), Some(3));
    assert_eq!(w2.pop(), None);
}
```

**预期结果：** 两个测试都通过，证明「源 FIFO → 消费顺序 `1,2,3`，与 dest flavor 无关」。运行 `cargo test --test <你的文件名>`。

**反思题：** 如果把源也换成 `Worker::new_lifo()`，两个测试的断言应分别改成什么？（答：返回 `Success(3)`，pop 序列变为 `2, 1`，对应源 LIFO 的 `3, 2, 1` 次序。）

## 6. 本讲小结

- 四个批量偷取方法是「**弹不弹出** × **上限可否指定**」的二维组合；不带 `_with_limit` 的两个是固定上限 `MAX_BATCH = 32` 的转发壳。
- 批量大小都遵循「**偷约一半**」：`steal_batch_with_limit` 用 `min(ceil(len/2), limit)` 全进 dest；`steal_batch_with_limit_and_pop` 用 `min((len-1)/2, limit-1)` 进 dest、外加弹出 1 个——两者总共都偷 `ceil(len/2)`。
- 公共前置与 u2-l3 的 `steal` 同构：`Arc::ptr_eq` 同队列短路（`steal_batch` 看 `is_empty`、`and_pop` 直接 `dest.pop()`）→ Acquire 读 `front` → 可重入补 `SeqCst` fence → `epoch::pin` → Acquire 读 `back` 判空 → Acquire 读 `buffer` 快照。
- **FIFO 源**用一次性批量拷贝 + 单个 CAS 认领整段（dest 为 LIFO 时预先反向写槽位）；**LIFO 源**只能逐个 CAS、逐个读槽位（因为 owner 会从 `back` 端并发 pop）。
- **核心不变式**：被偷批次在目的队列里的消费顺序**只由源 flavor 决定**（FIFO 源 → 队头→队尾；LIFO 源 → 队尾→队头），目的 flavor 不改变它。LIFO 源写进 FIFO 目的时要追加一次反转循环来维持此不变式。
- `and_pop` 的 LIFO 分支用 `mem::replace` 把「上一个偷到的」写进 dest、「新偷到的」留手，循环结束返回**最新**那个任务；末尾统一用 `Release fence` + 写目的 `back` 发布整批（tsan 下改 `Release store`）。

## 7. 下一步学习建议

- **u2-l5（resize 与 reserve）**：本讲多次调用 `dest.reserve(batch_size)` 却把它当黑盒。下一讲拆开 `Worker::resize` 如何分配新 buffer、`copy_nonoverlapping` 搬运、用 `epoch::pin` + `buffer.swap` + `defer_unchecked` 延迟回收旧 buffer，以及 `FLUSH_THRESHOLD_BYTES` 触发 `guard.flush` 的设计。
- **u3（Injector）**：`Injector` 也有同名的 `steal_batch` / `steal_batch_and_pop`（[src/deque.rs:1564-1987](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1564-L1987)），但底层是 block 链表而非环形 buffer，批量大小按「块尾或一半」计算。学完本讲再读 Injector 的批量偷取，能清楚看到「同一套 `Steal` 语义、两套实现」。
- **u4-l1（内存序）与 u4-l3（tsan 兼容）**：本讲末尾的 `Release fence` + `Relaxed/Release store` 双路径、以及 `front`/`back` 之间那道 `SeqCst` fence，都将在 u4 被横向串讲清楚。
- **u4-l5（实战调度器）**：本讲的 `steal_batch_and_pop` 正是 `find_task` 回退链的中间一环。u4-l5 会用它搭一个完整的多线程 work-stealing 调度器。
