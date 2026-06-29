# Frozen 包装器：手动 freeze/commit/fetch/sync 控制同步时机

## 1. 本讲目标

本讲讲解 ringbuf 的第二种同步策略——**Frozen（冻结）包装器**。学完后你应当能够：

- 说出 Frozen 与上一讲的 Direct（即时同步）在「何时与底层环形缓冲区交换索引」上的本质区别。
- 理解 Frozen 如何把 `read`/`write` 两个索引缓存在本地 `Cell` 中，做到「写本地、延迟回写」。
- 掌握 `commit`（写回）、`fetch`（拉取）、`sync`（双向）、`discard`（撤销）四种显式同步操作的语义与适用时机。
- 看懂为什么 Frozen 能让用户在批量写入后只做一次跨核同步，从而分摊 `SharedRb` 的缓存同步开销。
- 懂得 `freeze` 如何从 `Prod`/`Cons`（或 `CachingProd`/`CachingCons`）进入 Frozen、Drop 时如何自动 `commit` 退出。

## 2. 前置知识

在进入 Frozen 之前，请确认你已经理解下面这些概念（来自前置讲义）：

- **双索引与 2*capacity 模运算**：环形缓冲区用 `read`（最旧元素位置）和 `write`（下一个空槽位置）两个落在 `0..2*capacity` 的索引描述状态，空 ⟺ `read==write`，满 ⟺ `(write-read)%(2c)==c`。
- **Observer / Producer / Consumer trait 与默认方法**：`try_push`、`advance_write_index`、`vacant_slices_mut` 等大多是有默认实现的「派生方法」，只有 `set_*_index`、`read_index`、`write_index`、`unsafe_slices*` 是必须手写的底层原语。这点对理解 Frozen 至关重要——**Frozen 只重写了少数底层原语，就改变了整套读写行为的同步时机**。
- **Direct 包装器（u4-l2）**：Direct 是「即时同步」——每次读写直连底层原子索引，写端一动索引对端立即可见。Frozen 与之对照。
- **Wrap 与 RbRef 抽象（u4-l1）**：包装器经 `rb()` 拿到底层缓冲区，经 `into_rb_ref` 析构并归还「钥匙」。
- **`MaybeUninit<T>` 与内部可变性**：元素的初始化状态由 `read`/`write` 索引管理，存储本身用 `UnsafeCell` 实现内部可变性。

一个直觉比喻：Direct 像两个人共用一块「实时同步的白板」，你一落笔对方就看得见；Frozen 像你先在自己的「草稿本」上算好，确认无误后再「誊抄」到白板上。草稿阶段的改动对方完全看不到，这就是本讲的全部核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/wrap/frozen.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs) | Frozen 包装器的全部实现：结构体、`commit`/`fetch`/`sync`/`discard`、Observer/Producer/Consumer 实现、Drop。本讲主角。 |
| [src/wrap/direct.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs) | `Direct::freeze()`——从即时同步包装器进入 Frozen 的入口。 |
| [src/wrap/caching.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs) | `Caching::freeze()`——`SharedRb::split()` 产出 `CachingProd/Cons`，其 `freeze()` 是从默认按需同步退回到手动同步的入口。 |
| [src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs) | `try_push`、`advance_write_index` 的默认实现，说明 Frozen 如何靠重写底层原语改变行为。 |
| [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) | `vacant_len`/`is_full` 等派生观测方法，依赖 `read_index`/`write_index`。 |
| [src/lib.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs) | 文档「Performance」段点明 `freeze` 是减少同步开销的官方手段。 |
| [src/tests/frozen.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs) | 三个测试 `producer`/`discard`/`consumer`，是理解 Frozen 行为最直观的真实用例。 |

## 4. 核心概念与源码讲解

### 4.1 Frozen 的结构：本地缓存双索引（Frozen / FrozenProd / FrozenCons）

#### 4.1.1 概念说明

上一讲的 Direct 包装器是「即时同步」：每次读写都直接碰底层的原子索引。问题在于——对 `SharedRb` 而言，每次读写原子索引都要触发跨 CPU 核心的缓存同步（cache coherence traffic），这是有开销的。如果你在循环里逐个 `try_push` 一万个元素，就会产生一万次跨核同步。

Frozen 的设计目标是：**让索引的变化先停留在本地，需要时再一次性同步过去**，从而把 N 次跨核同步摊薄成 1 次。

它的做法非常直接——在包装器内部多拷贝一份 `read` 和 `write` 索引，存在单线程的 `Cell` 里：

```rust
pub struct Frozen<R: RbRef, const P: bool, const C: bool> {
    rb: R,
    read: Cell<usize>,
    write: Cell<usize>,
}
```

[src/wrap/frozen.rs:L22-L26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L22-L26) 这里 `rb` 是指向底层缓冲区的「钥匙」（`RbRef`，见 u4-l1），`read`/`write` 是两份**本地缓存索引**。两个 const generic 布尔 `P`（写权）、`C`（读权）与 Direct 一样用于编译期编码权限，由此给出两个别名：

- `FrozenProd<R> = Frozen<R, true, false>`：只持写权，对应生产端（[src/wrap/frozen.rs:L32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L32)）。
- `FrozenCons<R> = Frozen<R, false, true>`：只持读权，对应消费端（[src/wrap/frozen.rs:L38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L38)）。

关键点：**Frozen 自身不存储数据**，数据仍在底层 `Storage` 里；它缓存的只是「索引」这一份元数据。

#### 4.1.2 核心流程

要理解 Frozen，先分清两套索引：

- **底层索引**：存在于 `SharedRb`/`LocalRb` 里的 `read_b`、`write_b`，是「跨线程可见的真相」。
- **本地索引**：Frozen 的 `read`/`write` 这两个 `Cell`，是「本端看到的副本」。

创建 Frozen 时，本地索引从底层索引初始化一份：

```rust
unsafe fn new_unchecked(rb: R) -> Self {
    Self {
        read: Cell::new(rb.rb().read_index()),
        write: Cell::new(rb.rb().write_index()),
        rb,
    }
}
```

[src/wrap/frozen.rs:L59-L65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L59-L65) 之后，Frozen 把 `Observer` 的 `read_index`/`write_index` 重定向到本地 `Cell`：

```rust
fn read_index(&self) -> usize {
    self.read.get()
}
fn write_index(&self) -> usize {
    self.write.get()
}
```

[src/wrap/frozen.rs:L159-L166](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L159-L166) 这一步是「魔法」的根源。因为 `Observer` 的所有派生方法（`occupied_len`、`vacant_len`、`is_empty`、`is_full`，见 [src/traits/observer.rs:L49-L76](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L49-L76)）都基于 `read_index()`/`write_index()` 计算，于是它们读到的全是本地缓存的索引，而不是底层真相。

那么 `Producer` 端的写入呢？`FrozenProd` 只手写了 `set_write_index` 一个原语：

```rust
impl<R: RbRef> Producer for FrozenProd<R> {
    unsafe fn set_write_index(&self, value: usize) {
        self.write.set(value);
    }
}
```

[src/wrap/frozen.rs:L185-L190](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L190) 注意它把索引写进了**本地 `Cell`**，而不是底层。而默认的 `advance_write_index` 正是调用 `set_write_index`：

```rust
unsafe fn advance_write_index(&self, count: usize) {
    unsafe { self.set_write_index((self.write_index() + count) % modulus(self)) };
}
```

[src/traits/producer.rs:L33-L35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L33-L35) 于是，对 `FrozenProd` 调用默认的 `try_push`（[src/traits/producer.rs:L60-L70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)）时发生的事是：

1. `is_full()` 用本地索引判断 → 读本地 `Cell`。
2. `vacant_slices_mut()` 委托给 `self.rb().unsafe_slices_mut(...)`，**真正把元素写进了底层存储的 `MaybeUninit` 槽**。
3. `advance_write_index(1)` → `set_write_index(...)` → **只前进本地 `write` 这个 `Cell`**。

也就是说：**数据已经物理写入存储，但底层 `write_b` 索引没动**。消费端靠 `[read_b, write_b)` 这个区间判断「有哪些可消费」，既然 `write_b` 没动，它就看不到新元素。这正是「冻结」二字的确切含义。

#### 4.1.3 源码精读

| 代码位置 | 说明 |
| --- | --- |
| [src/wrap/frozen.rs:L22-L26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L22-L26) | Frozen 结构体，`rb` 钥匙 + `read`/`write` 两个本地 `Cell` 索引。 |
| [src/wrap/frozen.rs:L32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L32) / [L38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L38) | `FrozenProd`/`FrozenCons` 类型别名，用 `P`/`C` 编码读写权限。 |
| [src/wrap/frozen.rs:L59-L65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L59-L65) | `new_unchecked`：本地索引从底层索引拷贝初始化。 |
| [src/wrap/frozen.rs:L159-L166](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L159-L166) | Observer 重写：`read_index`/`write_index` 改读本地 `Cell`，使所有派生观测都基于缓存。 |
| [src/wrap/frozen.rs:L185-L190](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L190) | `FrozenProd::set_write_index` 只写本地 `write`，故 `advance_write_index` 不会触达底层。 |
| [src/wrap/frozen.rs:L192-L197](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L192-L197) | `FrozenCons::set_read_index` 只写本地 `read`，对称地，消费端的推进也不触达底层。 |

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「`FrozenProd::try_push` 只前进本地索引、不碰底层」这一论断。

**操作步骤**：

1. 打开 [src/wrap/frozen.rs:L185-L190](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L190)，确认 `FrozenProd` 实现的 `Producer` **只**重写了 `set_write_index`，没有重写 `try_push`、`advance_write_index`、`vacant_slices_mut`。
2. 跟进 [src/traits/producer.rs:L60-L70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70) 的默认 `try_push`：它先 `is_full()`，再 `vacant_slices_mut().0...write(elem)`，最后 `advance_write_index(1)`。
3. 再跟进 [src/traits/producer.rs:L33-L35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L33-L35) 的默认 `advance_write_index`，确认它最终落到 `self.set_write_index(...)`。

**需要观察的现象**：整条链路里，唯一被 `FrozenProd` 接管的写索引出口是本地 `Cell`，没有任何一处调用 `self.rb().set_write_index(...)`（对比 [src/wrap/direct.rs:L134-L139](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L134-L139) 里 Direct 是直接调底层）。

**预期结果**：你能向自己解释清楚——为什么 `FrozenProd` 上连续 `try_push` 之后，对端 `Cons` 仍然看不到这些元素。

#### 4.1.5 小练习与答案

**练习 1**：`FrozenCons::set_read_index` 写到哪里？为什么消费端 pop 出去的元素，对端 `Prod` 看到的「空闲槽」不会立刻增加？

**答案**：它写到本地 `read` 这个 `Cell`（[src/wrap/frozen.rs:L192-L197](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L192-L197)）。生产端用底层 `read_b` 计算 `vacant_len`，本地 `read` 没回写到底层，故空闲槽暂不增加，要等消费端 `commit` 后才可见。

**练习 2**：既然元素物理上已经写进底层存储，为什么消费端读不到？

**答案**：消费端是通过 `[read_b, write_b)` 这段索引区间界定「可消费范围」的。`FrozenProd` 只前进本地 `write`、没回写 `write_b`，所以新元素落在 `write_b` 之外，对消费端「不存在」。可见性由索引管理，而非物理写入。

### 4.2 显式同步：commit / fetch / sync

#### 4.2.1 概念说明

本地缓存索引解决了「减少同步次数」的问题，但随之而来一个新问题：**什么时候让本地和底层一致？** Frozen 把这个决定权完全交给用户，提供三个显式方法：

- **commit（提交）**：把本端的改动**写回**底层。对生产端是「告诉世界我写了多少」，对消费端是「告诉世界我消费了多少」。
- **fetch（拉取）**：把对端的改动**拉取**到本地。对生产端是「看看消费者腾出了多少空位」，对消费端是「看看生产者又写了多少」。
- **sync（同步）**：先 `commit` 再 `fetch`，双向刷新。

注意一个关键的不对称：**你只能 commit 自己拥有权限的那一侧，也只能 fetch 对端拥有权限的那一侧**。具体到两个别名：

| 操作 | `FrozenProd`（P=true） | `FrozenCons`（C=true） |
| --- | --- | --- |
| `commit` | 把本地 `write` 回写到底层 `write_b` | 把本地 `read` 回写到底层 `read_b` |
| `fetch` | 把底层 `read_b` 拉到本地 `read` | 把底层 `write_b` 拉到本地 `write` |

直白地说：生产端 commit 自己的进度（write）、fetch 对端的进度（read）；消费端反之。

#### 4.2.2 核心流程

三个方法的实现极简，正好印证上表：

```rust
pub fn commit(&self) {
    unsafe {
        if P {
            self.rb().set_write_index(self.write.get());
        }
        if C {
            self.rb().set_read_index(self.read.get());
        }
    }
}

pub fn fetch(&self) {
    if P {
        self.read.set(self.rb().read_index());
    }
    if C {
        self.write.set(self.rb().write_index());
    }
}

pub fn sync(&self) {
    self.commit();
    self.fetch();
}
```

[src/wrap/frozen.rs:L110-L136](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L110-L136)

用一次「生产端批量写」的生命周期来理解同步时机：

1. 进入 Frozen（本地索引 = 底层索引）。
2. 连续 `try_push` N 次：每次只前进本地 `write`，**零次**跨核同步。
3. 调一次 `commit()`：把本地 `write` 一次性回写到底层 `write_b`——**一次** Release 原子 store，N 个元素对消费端全部可见。
4. 若还要继续写，需 `fetch()` 拉取消费端腾出的空位，否则本地 `read` 停在旧值，`is_full()` 会误判已满。

把 N 次同步压成 1 次，这就是 Frozen 的性能价值，也是官方文档在「Performance」段推荐的用法：

> …or you can `freeze` producer or consumer and then synchronize threads manually (see items in `frozen` module).

[src/lib.rs:L22-L28](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L22-L28)

一个容易踩的坑：**漏掉 `fetch`**。生产端的 `is_full()` 读本地 `read`，若不 fetch，即便消费端已经 pop 光了所有元素，生产端也会以为缓冲区还是满的、`try_push` 一直返回 `Err`。所以「写一批 → commit →（继续写前）fetch」是常见节奏；懒一点就直接 `sync()`。

#### 4.2.3 源码精读

| 代码位置 | 说明 |
| --- | --- |
| [src/wrap/frozen.rs:L110-L120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L110-L120) | `commit`：按 P/C 把本地 `write`/`read` 回写底层，对 `SharedRb` 即一次 Release store。 |
| [src/wrap/frozen.rs:L122-L130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L122-L130) | `fetch`：按 P/C 把底层 `read`/`write` 拉到本地，对 `SharedRb` 即一次 Acquire load。 |
| [src/wrap/frozen.rs:L132-L136](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L132-L136) | `sync`：commit + fetch 双向刷新。 |

#### 4.2.4 代码实践（阅读型：读懂真实测试）

**实践目标**：通过官方测试 `producer` 验证 commit/sync 前后的可见性差异。

**操作步骤**：打开 [src/tests/frozen.rs:L5-L40](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs#L5-L40)，逐行读这个测试。它先 `prod.freeze()` 得到 `frozen_prod`，`try_push(0)` 后 `sync()`，断言 `cons` 能看到 `0..1`。接着进入一个块作用域：

- `frozen_prod.try_push(1)` 后，断言 `cons.iter()` 仍是 `0..1`、`cons.occupied_len()==1`，但 `frozen_prod.occupied_len()==2`——**两端观测不一致**，因为没 sync。
- `cons.try_pop()` 取走 0；此时 `frozen_prod.occupied_len()` 仍为 2（本地 `read` 没更新）。
- `frozen_prod.sync()` 之后，两端一致为 `1..2`、`occupied_len()==1`。

**需要观察的现象**：sync 之前，生产端本地 `occupied_len` 与消费端 `occupied_len` 数值不同；sync 之后两者一致。

**预期结果**：你会清楚地看到，「未同步」状态下两端的观测完全是两套独立的快照，只有显式 `sync` 才让它们对齐。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FrozenProd` 只 `commit`（写回 write）、不 `commit` read？反过来它为什么需要 `fetch` read？

**答案**：`read` 是消费端的权限，生产端无权改动底层 `read_b`，所以 commit 不碰它。但生产端判断空位需要最新的 `read_b`，所以必须 `fetch` 把它拉到本地。

**练习 2**：如果只调用 `commit` 从不调用 `fetch`，生产端会遇到什么问题？

**答案**：本地 `read` 永远停在创建时的旧值，消费端腾出的空位生产端看不到，`is_full()` 会持续误判为满，`try_push` 持续返回 `Err`，表现为「缓冲区卡死」。

### 4.3 discard：撤销未提交的写入（FrozenProd 专属）

#### 4.3.1 概念说明

延迟同步带来一个副产品：既然写入在 `commit` 之前对对端不可见，那就有机会**反悔**——把这些「草稿」扔掉，就像从没写过。`FrozenProd::discard` 就是干这个的。它只在写端存在（消费端丢弃已 pop 元素没有意义，因为 pop 本就是取出）。

典型场景：生产端批量构造了一批元素，写到一半发现这一批不合格，想整体回滚到上次同步点。discard 正好提供这个能力，且会正确 `drop` 掉非 `Copy` 类型元素持有的资源（比如 `String`、`Box`），不会泄漏。

#### 4.3.2 核心流程

```rust
pub fn discard(&mut self) {
    let last_tail = self.rb().write_index();
    let (first, second) = unsafe { self.rb().unsafe_slices_mut(last_tail, self.write.get()) };
    for item_mut in first.iter_mut().chain(second.iter_mut()) {
        unsafe { item_mut.assume_init_drop() };
    }
    self.write.set(last_tail);
}
```

[src/wrap/frozen.rs:L139-L148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L139-L148)

拆解三步：

1. `last_tail = self.rb().write_index()`：直接读**底层** `write_b`，也就是「上次 commit 之后的提交点」。注意这里刻意读底层而非本地——本地 `write` 已经被新写入推过了，底层那个才是「已确认的安全边界」。
2. `unsafe_slices_mut(last_tail, self.write.get())`：取 `[last_tail, 本地write)` 这段——正是「本次草稿写入但尚未提交」的所有槽。
3. 逐个 `assume_init_drop()`：把这些槽里的元素逐个 `drop`（释放它们持有的资源），再把本地 `write` 回退到 `last_tail`。

回退之后，本地 `write` == 底层 `write_b`，本地与底层重新一致，那些草稿元素从索引上和资源上都被彻底抹除。

> ⚠️ discard 必须在 `commit` 之前调用。一旦 commit 了，本地 `write` 已经等于底层 `write_b`，`[last_tail, write)` 区间为空，discard 就无事可做；而且 commit 后元素对消费端可见，已经无法单方面回收。

#### 4.3.3 源码精读

| 代码位置 | 说明 |
| --- | --- |
| [src/wrap/frozen.rs:L141-L148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L141-L148) | `discard`：以底层 `write_b` 为回退点，drop 掉草稿槽、回退本地 `write`。 |
| [src/tests/frozen.rs:L42-L78](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs#L42-L78) | `discard` 测试：`try_push(3)` 后 `discard()`，`cons.occupied_len()` 仍为 2，再 `try_push(2)` + `sync` 后 cons 看到的是 `0..3`（证明 3 被丢弃、2 接在 1 后面）。 |

#### 4.3.4 代码实践（阅读型）

**实践目标**：用真实测试确认 discard 丢弃的是「未提交」的那一份，且不影响已提交内容。

**操作步骤**：读 [src/tests/frozen.rs:L42-L78](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs#L42-L78)。关键序列：`try_push(0)`→`sync`（cons 见 `0..1`）→`try_push(1)`→`sync`（cons 见 `0..2`）→`try_push(3)`（cons 仍 `0..2`，frozen 占用 3）→`discard()`（cons 仍 `0..2`，frozen 占用回到 2）→`try_push(2)`→`sync`（最终 cons 见 `0..3`）。

**需要观察的现象**：discard 之后，frozen 的 `occupied_len` 从 3 回到 2；后续 `try_push(2)` 接在 `1` 之后，cons 最终看到的是 `0,1,2` 而不是 `0,1,3`——`3` 确实被丢弃了。

**预期结果**：你能解释为什么最终序列里没有 `3`——它被 discard 抹除了，`2` 占用了它原本的草稿槽。

#### 4.3.5 小练习与答案

**练习 1**：discard 里为什么读 `self.rb().write_index()`（底层）而不是 `self.write.get()`（本地）作为回退点？

**答案**：本地 `write` 已被本次草稿推进过，读它就没有「可回退的区间」了。底层 `write_b` 是上次 commit 后的稳定提交点，以它为边界才能精确圈出「未提交」的草稿槽。

**练习 2**：如果元素类型是 `String`，discard 会泄漏内存吗？

**答案**：不会。discard 对每个草稿槽调用 `assume_init_drop()`，会真正运行 `String` 的 `Drop`，释放其堆内存，再把本地 `write` 回退。这正是 discard 优于「什么都不做直接覆盖」的地方。

### 4.4 生命周期：freeze 进入、Drop 自动提交、hold 标志

#### 4.4.1 概念说明

你通常不会直接 `Frozen::new`，而是从已有的 `Prod`/`Cons`（Direct）或 `CachingProd`/`CachingCons`（Caching）调用 `freeze()` 进入。进入与退出都遵循 ringbuf 一贯的 SPSC 不变量（至多一个写端、一个读端，由 hold 标志保证）。

两个要点：

1. **freeze 不重复占 hold**：`freeze` 是把已有的写/读权限「转换形态」，从即时/按需同步包装器转成手动同步包装器，权限没有多出来。所以它走 `Frozen::new_unchecked`，跳过 hold 断言（hold 标志在进入 Direct/Caching 时已经置位）。
2. **Drop 自动 commit**：Frozen 的 `Drop` 会先 `commit` 再 `close`。意思是——即便你忘了手动同步，析构时未提交的改动也会被提交，不会丢失。这与模块文档承诺一致：「Changes are not synchronized with the ring buffer until its explicitly requested or when dropped.」（[src/wrap/frozen.rs:L1-L3](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L1-L3)）。

#### 4.4.2 核心流程

**进入：freeze**。Direct 与 Caching 都提供 `freeze(self)`，按值消费旧包装器、吐出 `Frozen`：

```rust
// Direct
pub fn freeze(self) -> Frozen<R, P, C> {
    let this = ManuallyDrop::new(self);
    unsafe { Frozen::new_unchecked(ptr::read(&this.rb)) }
}
```

[src/wrap/direct.rs:L57-L61](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L57-L61) 这里用 `ManuallyDrop` + `ptr::read` 把钥匙 `rb` 「搬」进新的 Frozen，同时绕过 Direct 的 `Drop`（否则 `close` 会复位 hold、归还权限）。Caching 的 `freeze` 更简单——它内部本来就持有一个 `Frozen`，直接交出来（[src/wrap/caching.rs:L39-L42](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L39-L42)）。

**退出一：Drop 自动提交**。

```rust
impl<R: RbRef, const P: bool, const C: bool> Drop for Frozen<R, P, C> {
    fn drop(&mut self) {
        self.commit();
        unsafe { self.close() };
    }
}
```

[src/wrap/frozen.rs:L199-L204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204) 先 `commit`（保住未提交改动），再 `close`（复位 hold 标志，见下）。

**退出二：into_rb_ref 提交后归还钥匙**。把 Frozen 重新拆回裸钥匙时同样先 commit：

```rust
fn into_rb_ref(mut self) -> R {
    self.commit();
    unsafe {
        self.close();
        let this = ManuallyDrop::new(self);
        ptr::read(&this.rb)
    }
}
```

[src/wrap/frozen.rs:L88-L95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L88-L95)

**close：复位 hold**。`close` 把创建时（在 `new` 里，[src/wrap/frozen.rs:L44-L52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L44-L52)）置位的 `hold_write`/`hold_read` 复位，归还写/读权限，使缓冲区可以被再次拆分：

```rust
unsafe fn close(&mut self) {
    if P { unsafe { self.rb().hold_write(false) }; }
    if C { unsafe { self.rb().hold_read(false) }; }
}
```

[src/wrap/frozen.rs:L72-L79](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L72-L79) 注意 `Frozen::new`（当你直接构造而非 freeze 时）仍会做 hold 断言（[src/wrap/frozen.rs:L44-L52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L44-L52)），重复拆分同样会 panic——这和 Direct 一致（见 u4-l2）。

整条生命周期可以画成：

```
Direct/Caching --freeze()--> Frozen --[commit/fetch/sync/discard 手动操作]--> Drop
                                    \                                         ^
                                     --> into_rb_ref --commit+close--> 归还钥匙
            (hold 标志：new 时置位 / freeze 时继承 / close 或 Drop 时复位)
```

#### 4.4.3 源码精读

| 代码位置 | 说明 |
| --- | --- |
| [src/wrap/direct.rs:L57-L61](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L57-L61) | `Direct::freeze`：ManuallyDrop 搬钥匙进 Frozen，绕过 Direct 的 Drop，hold 继承不重复占。 |
| [src/wrap/caching.rs:L39-L42](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L39-L42) | `Caching::freeze`：Caching 内部即 Frozen，直接交出。 |
| [src/wrap/frozen.rs:L44-L52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L44-L52) | `Frozen::new`：直接构造时做 hold 断言，保证 SPSC。 |
| [src/wrap/frozen.rs:L72-L79](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L72-L79) | `close`：复位 hold_write/hold_read，归还权限。 |
| [src/wrap/frozen.rs:L88-L95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L88-L95) | `into_rb_ref`：先 commit 再 close，再搬出钥匙。 |
| [src/wrap/frozen.rs:L199-L204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204) | `Drop`：commit + close，保证析构时未提交改动不丢失。 |

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：验证「freeze 继承 hold、Drop 自动 commit」两条结论。

**操作步骤**：

1. 对比 [src/wrap/direct.rs:L42-L50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L42-L50)（`Direct::new` 做 hold 断言）与 [src/wrap/direct.rs:L57-L61](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L57-L61)（`freeze` 调 `new_unchecked` 跳过断言），确认 freeze 没有再次占 hold。
2. 读 [src/wrap/frozen.rs:L199-L204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204) 的 `Drop`，确认它先调 `self.commit()`。

**需要观察的现象**：freeze 路径上 hold 标志从未被重复置位；Drop 第一行就是 commit。

**预期结果**：你能说明——即便用户从不手动 `commit`，只要 `FrozenProd` 被 drop，它缓存的写入也会被回写到底层而对消费端可见。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Direct::freeze` 用 `ManuallyDrop` 包住 `self` 再 `ptr::read`，而不是直接 `self.rb`？

**答案**：直接移动 `self.rb` 会触发 `Direct` 的 `Drop`，而 `Drop` 会调 `close` 复位 hold、归还权限——那 Frozen 就失去了写/读权限。用 `ManuallyDrop` 绕过 `Drop`，把 hold 连同钥匙一起「继承」给 Frozen。

**练习 2**：如果我 `freeze` 出一个 `FrozenProd`，写入若干元素后**既不 commit 也不 discard** 就让它离开作用域，这些元素会怎样？

**答案**：`Drop` 会自动 `commit`，把它们回写到底层 `write_b`，对消费端可见——不会丢失。若你想丢弃它们，必须在 drop 之前显式调用 `discard()`。

## 5. 综合实践

把本讲四个模块串起来，完成一个**可运行**的小程序，亲手感受「冻结→写→对端不可见→提交→可见→撤销」的完整闭环。基于 `SharedRb`（即 `HeapRb`），符合本讲实践任务要求。

> 以下为**示例代码**（非项目原有文件），可直接放入 `examples/` 之外的自建 crate 或 `tests/` 中运行，需要 `alloc` feature（`HeapRb` 依赖它）。

```rust
// 示例代码：演示 FrozenProd 的 commit / discard 行为
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(4);
    let (prod, cons) = rb.split();          // 得到 CachingProd / CachingCons

    let mut frozen = prod.freeze();          // 进入 FrozenProd（手动同步）

    // ① 连续写入 3 个，但不 commit
    frozen.try_push(10).unwrap();
    frozen.try_push(11).unwrap();
    frozen.try_push(12).unwrap();

    // ② 此时对端看不到任何元素（底层 write_b 未动）
    assert_eq!(cons.occupied_len(), 0);
    assert_eq!(frozen.occupied_len(), 3);    // 本地能看到 3 个

    // ③ commit 一次 → 3 个元素全部对对端可见
    frozen.commit();
    assert_eq!(cons.occupied_len(), 3);

    // ④ 再写 2 个草稿（未提交），然后反悔
    frozen.try_push(13).unwrap();
    frozen.try_push(14).unwrap();
    assert_eq!(frozen.occupied_len(), 5);    // 容量 4 但本地索引可「超前」（见说明）
    frozen.discard();                        // 撤销未提交的 13、14
    assert_eq!(frozen.occupied_len(), 3);    // 回到上次提交点

    // ⑤ drop 时自动 commit（这里本地==底层，无新改动）
    drop(frozen);

    // 对端按 FIFO 读出，应当是 10,11,12，没有 13、14
    let mut got = Vec::new();
    while let Some(x) = cons.try_pop() { got.push(x); }
    assert_eq!(got, vec![10, 11, 12]);
    println!("ok: {:?}", got);
}
```

**操作步骤**：

1. 在仓库根目录建一个临时 crate（或加到 `examples/`，注意 `examples/` 下用 `HeapRb` 需要 `alloc` feature，可 `cargo run --example <name> --features alloc`）。
2. 编译运行，观察断言是否全部通过。
3. 把第 ③ 步的 `frozen.commit();` 注释掉，重新运行，观察 `cons.occupied_len()` 此时是否仍为 0（验证「未 commit 不可见」）。
4. 把第 ④ 步的 `frozen.discard();` 注释掉，重新运行，观察最终 `got` 是否变成了 `vec![10,11,12,13]`（容量 4，13 挤进、14 因满被退回）——对比体会 discard 的作用。

**需要观察的现象**：

- 未 commit 时消费端 `occupied_len()` 为 0；commit 后跳到 3。
- discard 后 `frozen.occupied_len()` 从 5 回到 3；最终结果不含 13、14。

**预期结果**：原始程序打印 `ok: [10, 11, 12]`。若运行环境不具备（如无法本地编译），明确标注「待本地验证」。

> 说明：第 ④ 步中 `frozen.occupied_len()==5` 看似超过容量 4，这是因为本地索引在未 fetch 的情况下可以「超前」于真实约束——Frozen 把一致性责任交给用户，本地观测并不保证落在合法区间内，只有 commit/fetch 之后才与底层对齐。这也是「快照可能瞬时失效」在 Frozen 上的极端体现。

## 6. 本讲小结

- **Frozen = 钥匙 + 两份本地 `Cell` 索引**。它把 `read`/`write` 缓存在本地，重写 `read_index`/`write_index`/`set_*_index` 这几个底层原语，使所有派生方法（`try_push`/`try_pop`/`is_full` 等）都基于本地索引工作（[src/wrap/frozen.rs:L22-L26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L22-L26)、[L185-L197](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L197)）。
- **数据物理写入存储、但索引停在本地**：`try_push` 把元素写进底层 `MaybeUninit` 槽，却只前进本地 `write`，对端靠底层 `write_b` 判断可见性，故看不到——这就是「冻结」。
- **三件套同步**：`commit` 把本端进度回写底层、`fetch` 拉取对端进度、`sync` 双向刷新；生产端 commit write / fetch read，消费端反之（[src/wrap/frozen.rs:L110-L136](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L110-L136)）。
- **discard（FrozenProd 专属）**：以底层 `write_b` 为回退点，drop 掉未提交草稿、回退本地 `write`，支持非 `Copy` 类型且不泄漏（[src/wrap/frozen.rs:L139-L148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L139-L148)）。
- **生命周期**：经 `freeze()`（Direct/Caching）进入并继承 hold；`Drop` 与 `into_rb_ref` 都先 `commit` 再 `close`，保证未提交改动不丢失、hold 正确复位（[src/wrap/frozen.rs:L88-L95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L88-L95)、[L199-L204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204)）。
- **价值与代价**：Frozen 把 N 次跨核同步压成 1 次，代价是用户必须手动管理同步时机（漏 fetch 会误判满、忘 commit 则对端不可见）。

## 7. 下一步学习建议

- **下一讲 u4-l4「Caching 包装器」** 是 Frozen 的「自动化版本」——Caching 内部就持有一个 Frozen，并在 `is_full`/`is_empty` 时自动 `fetch`、操作成功后自动 `commit`（见 [src/wrap/caching.rs:L114-L123](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L114-L123)）。理解了本讲的手动同步，就能看懂 Caching 是如何「按需」替你做 commit/fetch 的，以及为什么 `SharedRb::split()` 默认选 Caching 而非 Frozen。
- 建议继续精读 [src/wrap/caching.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs)，对比 Caching 的 `try_push`/`try_pop` 与 Frozen 默认实现的差异。
- 进阶可阅读 [src/tests/frozen.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs) 的 `consumer` 测试（[L80-L115](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/frozen.rs#L80-L115)），把读端的 fetch/commit 行为也跑一遍。
