# RingBuffer trait 与 overwrite 覆盖写入模式

## 1. 本讲目标

在前三讲里，我们已经分别学会了三种能力：用 `Observer`「看」缓冲区状态（u3-l1）、用 `Producer`「写」（u3-l2）、用 `Consumer`「读」（u3-l3）。但有一个场景这三讲都没解决：

> 当缓冲区**满了**，普通 `try_push` 会拒绝写入并返回 `Err(elem)`。可有时候我们不想要「拒绝」，而想要「**挤掉最旧的那个元素，把新元素放进去**」——这就是覆盖写入（overwrite）。

本讲要回答四个问题：

1. `RingBuffer` 这个 trait 在整个 trait 体系里处于什么位置？它为什么是「拥有者超集」？
2. `hold_read` / `hold_write` 两个标志是什么？它们和「能不能 overwrite」有什么关系？
3. `push_overwrite` / `push_iter_overwrite` / `push_slice_overwrite` 三个覆盖写入方法的返回值和执行流程是什么？
4. 为什么覆盖写入**只能在缓冲区未被拆分（un-split）时使用**，一旦 `split()` 之后就不能再调用了？

学完本讲，你应该能够：写出一段覆盖写入代码并准确预测返回值；解释为什么 overwrite 内部要调用 `try_pop`；以及从「类型层面」说清楚为什么 split 后的 `Prod`/`Cons` 句柄拿不到 `push_overwrite` 方法。

---

## 2. 前置知识

本讲默认你已经掌握前三讲（u3-l1 ~ u3-l3）的内容，尤其是下面这些概念：

- **SPSC 不变量**：环形缓冲区要求「至多一个生产者、至多一个消费者」。在 ringbuf 里，这个约束由 `read_held` / `write_held` 两个布尔标志在运行时强制（见 u2-l4、u5-l2）。本讲会用到「谁拥有 read 端、谁拥有 write 端」这一概念。
- **三步写入/读取范式**：写 = 观测 → 写入 `MaybeUninit` 空槽 → `advance_write_index` 提交；读 = 观测 → `assume_init_read` 移出 → `advance_read_index` 提交。只有推进索引这一步发生后，操作才对另一端「可见」。
- **Observer 的派生量**：`occupied_len`（已占用数）、`vacant_len`（空闲数）、`is_empty`、`is_full`，它们的值来自 read/write 双索引的模运算（见 u2-l1）。
- **`try_push` / `try_pop` 的契约**：`try_push` 满则 `Err(elem)` 原样退回；`try_pop` 空则 `None`，否则移除并返回最旧元素。
- **`split()` 的所有权转移**：`split(self)` 会把缓冲区搬进 `Arc`（`SharedRb`）或 `Rc`（`LocalRb`），返回独立的 `Producer` / `Consumer` 句柄，原缓冲区对象不再可用（见 u2-l4）。

如果上面任意一条你还觉得模糊，建议先回看对应讲义再继续。

### 一个直觉：覆盖写入 = 「一边消费最旧的，一边生产最新的」

你可以把 `push_overwrite` 想象成这样一句话：

> 缓冲区满了？那我先把队头最旧的那个元素「踢出去」，再把你给我的新元素「塞进队尾」。

注意它**同时**动了 read 端（踢出旧元素 = 一次消费）和 write 端（塞入新元素 = 一次生产）。这一点是后面所有「为什么必须独占访问」解释的根源。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/traits/ring_buffer.rs` | **本讲核心**。定义 `RingBuffer` trait、`hold_read`/`hold_write`、三个 `*_overwrite` 默认实现，以及 `DelegateRingBuffer` 委托 blanket impl。 |
| `examples/overwrite.rs` | `push_overwrite` 的最小可运行示例，本讲代码实践的蓝本。 |
| `src/rb/shared.rs` | `SharedRb` 对 `RingBuffer` 的实现：hold 标志用 `AtomicBool`，索引用原子。`HeapRb`/`StaticRb` 都是它的别名，所以也实现 `RingBuffer`。 |
| `src/rb/local.rs` | `LocalRb` 对 `RingBuffer` 的实现：hold 标志用 `Cell<bool>`，索引用 `Cell`。 |
| `src/wrap/direct.rs` | `Direct::new` 用 hold 标志断言「至多一个生产者/消费者」；`Prod`/`Cons` 类型别名只实现单侧 trait（这是 split 后无法 overwrite 的类型证据）。 |
| `src/traits/consumer.rs` | `skip` 方法（`push_slice_overwrite` 内部复用它来丢弃旧元素）与 `try_pop`。 |
| `src/traits/producer.rs` | `try_push`、`push_slice`（overwrite 方法内部复用）。 |
| `src/traits/observer.rs` | `is_full`、`vacant_len`（overwrite 的判定基础）。 |

---

## 4. 核心概念与源码讲解

本讲把 `RingBuffer` 拆成四个最小模块来讲：

- **4.1** `RingBuffer` trait 的定位：拥有者超集与 hold 标志
- **4.2** `push_overwrite`：单个元素的覆盖写入
- **4.3** 批量覆盖：`push_iter_overwrite` 与 `push_slice_overwrite`
- **4.4** 独占访问：为何 overwrite 不能在 split 之后使用

---

### 4.1 RingBuffer trait 的定位：拥有者超集与 hold 标志

#### 4.1.1 概念说明

到目前为止我们见过的三个 trait 各管一摊：`Observer` 管看、`Producer` 管写、`Consumer` 管读。但**一个完整的、尚未拆分的环形缓冲区对象**（比如 `HeapRb`、`StaticRb`、`LocalRb`）其实**同时具备这三种能力**——它既能被观察，又能写，又能读。

`RingBuffer` trait 就是用来描述「我同时拥有读写两端」的拥有者类型。它的定义只有一行超集声明，但这一行信息量很大：

```rust
pub trait RingBuffer: Observer + Consumer + Producer { ... }
```

它继承自 `Observer + Consumer + Producer`，意味着「实现了 `RingBuffer` 的类型，必然同时是观察者、消费者和生产者」。

那它在这三者之上**额外**提供了什么？两样东西：

1. **`hold_read` / `hold_write`**：直接读写那两个 SPSC 拥有权标志的能力。
2. **三个 `*_overwrite` 方法**：覆盖写入（本讲主题）。

为什么 overwrite 要放在这个「拥有者」trait 里、而不是放在 `Producer` 里？因为 overwrite 需要同时操作 read 端和 write 端——它不属于「纯生产者」的能力，只有「同时拥有两端」的拥有者才有资格做。这正是它被放在 `RingBuffer` 里的原因。

#### 4.1.2 核心流程

`RingBuffer` 类型在整个体系里的位置可以用一张继承关系图概括：

```
            Observer（看）
           /     |       \
     Producer   Consumer   ──┐（同时继承这三者）
      （写）    （读）        │
                            ▼
                       RingBuffer（拥有者：能写能读，还能 overwrite）
```

关键流程要点：

- 一个**未拆分**的缓冲区对象（`HeapRb` 等）直接实现 `RingBuffer`，因为它本来就把 read/write 索引和 hold 标志都握在自己手里。
- `hold_read(flag)` / `hold_write(flag)` 用来设置「read 端 / write 端是否被占用」，返回**旧值**。它们是 `unsafe` 的，因为如果在句柄还活着时把标志错误地清成 `false`，会破坏 SPSC 不变量。
- `split()` 之后产生的 `Prod`/`Cons` 句柄**不**实现 `RingBuffer`（它们各自只实现 `Producer` 或 `Consumer`），因此拿不到 overwrite 方法——这一点我们在 4.4 节详细论证。

#### 4.1.3 源码精读

trait 的完整声明，注意它的超集和两个 unsafe 方法：

[`RingBuffer` 的定义](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L7-L24)（`src/traits/ring_buffer.rs:7-24`）—— 这段定义了 `RingBuffer: Observer + Consumer + Producer` 的超集关系，以及 `hold_read` / `hold_write` 两个 unsafe 方法（都「返回旧值」）。

```rust
/// An abstract ring buffer that exclusively owns its data.
pub trait RingBuffer: Observer + Consumer + Producer {
    /// Tell whether read end of the ring buffer is held by consumer or not.
    ///
    /// Returns old value.
    ///
    /// # Safety
    ///
    /// Must not be set to `false` while consumer exists.
    unsafe fn hold_read(&self, flag: bool) -> bool;

    /// Tell whether write end of the ring buffer is held by producer or not.
    ///
    /// Returns old value.
    ///
    /// # Safety
    ///
    /// Must not be set to `false` while producer exists.
    unsafe fn hold_write(&self, flag: bool) -> bool;
    ...
}
```

文档注释里的「An abstract ring buffer that **exclusively owns** its data」一句话点明了这个 trait 的本质：**独占拥有**。这也是后面「需要 `&mut self`」「split 后不能用」的源头。

那么 `hold_read` / `hold_write` 在底层是怎么存的？看 `SharedRb` 的实现：

[`SharedRb` 实现 `RingBuffer`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L137-L146)（`src/rb/shared.rs:137-146`）—— 用 `AtomicBool` 的 `swap` 原子地交换标志并返回旧值：

```rust
impl<S: Storage + ?Sized> RingBuffer for SharedRb<S> {
    #[inline]
    unsafe fn hold_read(&self, flag: bool) -> bool {
        self.read_held.swap(flag, Ordering::AcqRel)
    }
    #[inline]
    unsafe fn hold_write(&self, flag: bool) -> bool {
        self.write_held.swap(flag, Ordering::AcqRel)
    }
}
```

这两个 `AtomicBool` 字段在结构体里这样声明（[`SharedRb` 的 hold 字段](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L54-L55)，`src/rb/shared.rs:54-55`）。而对应的只读查询 `read_is_held` / `write_is_held` 由 `Observer` 提供（[`read_is_held`/`write_is_held`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L113-L120)，`src/rb/shared.rs:113-120`）。

`LocalRb` 的实现几乎一样，只是把原子换成 `Cell`：

[`LocalRb` 实现 `RingBuffer`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L121-L130)（`src/rb/local.rs:121-130`）—— 用 `Cell::replace` 交换标志并返回旧值。

因为 `HeapRb` 就是 `SharedRb<Heap<T>>` 的别名，所以 `HeapRb` 自动获得 `RingBuffer` 的全部实现——这就是为什么本讲的示例能直接在 `HeapRb` 上调用 `push_overwrite`。

#### 4.1.4 代码实践

**实践目标**：确认 `HeapRb`（未拆分）实现了 `RingBuffer`，并观察 hold 标志的初始值。

**操作步骤**：

1. 新建一个 binary（或改 `examples/overwrite.rs` 的副本），写入下面这段「示例代码」（非项目原有）：
   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn main() {
       let rb = HeapRb::<i32>::new(4);
       // Observer 提供的只读查询
       println!("read_is_held:  {}", rb.read_is_held());
       println!("write_is_held: {}", rb.write_is_held());
       println!("capacity:      {}", rb.capacity());
   }
   ```
2. 用 `cargo run --example <你的示例名>`（需在 `Cargo.toml` 注册 example）或放进一个临时 bin 里运行。

**需要观察的现象**：两个 hold 标志在缓冲区**刚创建、尚未 split** 时都是 `false`（没有任何端被占用）。

**预期结果**：
```
read_is_held:  false
write_is_held: false
capacity:      4
```

> 说明：`hold_read` / `hold_write` 这两个**写入**标志的方法是 `unsafe` 的，本实践只调用安全的 `read_is_held` / `write_is_held` 来**观察**。hold 标志由 `split()` / `Direct::new` 等机制自动维护，普通用户不需要手动调用 unsafe 版本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RingBuffer` 继承的是 `Observer + Consumer + Producer` 三个，而不是只继承 `Producer`？

**参考答案**：因为 `RingBuffer` 描述的是「同时拥有读写两端」的拥有者类型，它的 `push_overwrite` 等方法会同时操作 read 端（消费）和 write 端（生产）。只继承 `Producer` 就意味着它只能写不能读，无法实现覆盖写入所需的「先踢掉最旧元素」。

**练习 2**：`hold_read(true)` 返回的是新值还是旧值？为什么这个方法是 `unsafe` 的？

**参考答案**：返回**旧值**（`swap` 的语义）。它是 `unsafe` 的，因为如果在 consumer 句柄还活着时把 `read_held` 清成 `false`，系统会误以为「没有消费者占用 read 端」，从而允许第二次拆分，破坏 SPSC「至多一个消费者」的不变量，引发数据竞争。

---

### 4.2 push_overwrite：单个元素的覆盖写入

#### 4.2.1 概念说明

`push_overwrite` 是覆盖写入的最小单位，也是理解另外两个批量方法的基础。它的语义是：

> 把 `elem` 放进缓冲区。**如果缓冲区满了，就先丢弃最旧的一个元素**，腾出一个空位再放进去。

它的返回值是 `Option<Self::Item>`：

- 返回 `Some(被丢弃的旧元素)` —— 当且仅当缓冲区**当时是满的**，确实发生了一次覆盖；
- 返回 `None` —— 当缓冲区**没满**，直接写入，没有丢弃任何东西。

这个返回值非常有用：它告诉你「这次写入挤掉了谁」。在「只保留最近 N 条日志」这类场景里，你可以据此统计被丢弃了多少条。

#### 4.2.2 核心流程

`push_overwrite` 的实现极其简洁，只有三行，但每一步都对应我们前面学过的能力：

```rust
fn push_overwrite(&mut self, elem: Self::Item) -> Option<Self::Item> {
    let ret = if self.is_full() { self.try_pop() } else { None };
    let _ = self.try_push(elem);
    ret
}
```

逐步拆解：

1. **判定是否满** `self.is_full()`（来自 `Observer`）。
2. **若满**：调用 `self.try_pop()`（来自 `Consumer`）移除并返回**最旧的元素**，把它存进 `ret`。这一步同时把 read 索引向前推进一位，从而腾出 1 个空位。
3. **若不满**：`ret = None`，什么都不丢。
4. **无论如何**：调用 `self.try_push(elem)`（来自 `Producer`）把新元素写进去。此时缓冲区必然至少有 1 个空位（要么本来就没满，要么刚才 pop 腾了一个），所以 `try_push` 一定成功，返回 `Ok(())`，因此用 `let _ =` 丢弃结果。
5. **返回** `ret`（被覆盖的旧元素，或 `None`）。

用伪代码概括覆盖写入的条件分支：

```
push_overwrite(elem):
    if 占用数 == 容量:        # is_full
        旧 = pop 最旧元素      # 腾出 1 个空位
    else:
        旧 = None
    push elem                 # 必定成功
    return 旧
```

注意一个关键点：第 2 步的 `try_pop` 推进了 read 索引，第 4 步的 `try_push` 推进了 write 索引。**一次 `push_overwrite` 同时动了两个索引**——这就是它需要 `&mut self`、需要独占访问的根本原因（详见 4.4）。

#### 4.2.3 源码精读

`push_overwrite` 的默认实现就在 trait 体内：

[`push_overwrite` 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L26-L33)（`src/traits/ring_buffer.rs:26-33`）—— 这是 trait 的默认方法，所有实现 `RingBuffer` 的类型（`SharedRb`、`LocalRb` 及其别名）都直接复用，无需各自重写。

它依赖的两个判定/操作来自前面讲过的 trait：

- `is_full`（[Observer 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L74-L76)，`src/traits/observer.rs:74-76`）—— 满即 `vacant_len() == 0`。
- `try_pop`（[Consumer 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106-L113)，`src/traits/consumer.rs:106-113`）—— 移除并返回最旧元素。
- `try_push`（[Producer 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)，`src/traits/producer.rs:60-70`）—— 写入单个元素。

官方最小示例把这套行为展示得清清楚楚：

[`examples/overwrite.rs` 全文](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/overwrite.rs#L1-L13)（`examples/overwrite.rs:1-13`）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let mut rb = HeapRb::<i32>::new(2);

    assert_eq!(rb.push_overwrite(0), None);    // 没满，直接写
    assert_eq!(rb.push_overwrite(1), None);    // 没满，直接写
    assert_eq!(rb.push_overwrite(2), Some(0)); // 满了！丢弃 0，写入 2

    assert_eq!(rb.try_pop(), Some(1));
    assert_eq!(rb.try_pop(), Some(2));
    assert_eq!(rb.try_pop(), None);
}
```

我们逐行推演（容量 = 2）：

| 操作 | 占用数前 | 是否满 | ret | 写入后缓冲区内容（从旧到新） |
| --- | --- | --- | --- | --- |
| `push_overwrite(0)` | 0 | 否 | `None` | `[0]` |
| `push_overwrite(1)` | 1 | 否 | `None` | `[0, 1]` |
| `push_overwrite(2)` | 2 | **是** | `Some(0)`（丢弃 0） | `[1, 2]` |
| `try_pop()` | 2 | — | — | `[2]`，返回 `Some(1)` |
| `try_pop()` | 1 | — | — | `[]`，返回 `Some(2)` |
| `try_pop()` | 0 | — | — | `[]`，返回 `None` |

结果与示例的 `assert_eq!` 完全吻合。

#### 4.2.4 代码实践

**实践目标**：亲手运行官方示例，验证覆盖写入的返回值与最终缓冲区内容。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   cargo run --example overwrite
   ```
   （该 example 在 `Cargo.toml` 中以 `required-features = ["alloc"]` 门控，默认 feature 即含 `alloc`，所以开箱即跑。）
2. 程序**没有输出**——它只靠 `assert_eq!` 断言。若运行无 panic 退出，即说明全部断言通过。
3. 为了「看见」过程，把示例复制成一份本地副本（不要改原文件），把 `assert_eq!` 改成 `println!`，例如：
   ```rust
   // 示例代码（非项目原有，仅为观察而改写）
   println!("{:?}", rb.push_overwrite(0));
   println!("{:?}", rb.push_overwrite(1));
   println!("{:?}", rb.push_overwrite(2));
   println!("{:?}", rb.try_pop());
   println!("{:?}", rb.try_pop());
   println!("{:?}", rb.try_pop());
   ```

**需要观察的现象**：第三行输出 `Some(0)`（覆盖发生），最后三行依次是 `Some(1)`、`Some(2)`、`None`。

**预期结果**：
```
None
None
Some(0)
Some(1)
Some(2)
None
```

#### 4.2.5 小练习与答案

**练习 1**：对一个容量为 3、已经装有 `[10, 20, 30]`（满）的缓冲区连续调用 `push_overwrite(40)` 和 `push_overwrite(50)`，两次的返回值分别是什么？最终缓冲区内容是什么？

**参考答案**：第一次 `push_overwrite(40)` 满 → 丢弃最旧的 `10`，返回 `Some(10)`，内容变 `[20, 30, 40]`。第二次 `push_overwrite(50)` 仍满 → 丢弃 `20`，返回 `Some(20)`，内容变 `[30, 40, 50]`。最终缓冲区从旧到新为 `[30, 40, 50]`。

**练习 2**：`push_overwrite` 里为什么用 `let _ = self.try_push(elem);` 而不是 `self.try_push(elem).unwrap()`？这两种写法在正确性上有区别吗？

**参考答案**：在逻辑上没有区别——因为前面 `is_full()` 为真时已经 `try_pop` 腾出了 1 个空位，`try_push` 必然成功。作者用 `let _ =` 只是为了表达「我确信它会成功，故意忽略返回值」。换成 `.unwrap()` 行为相同，但 `let _ =` 更能传达「这里不可能失败」的意图，也避免了 `unwrap` 带来的「似乎可能 panic」的误导。

---

### 4.3 批量覆盖：push_iter_overwrite 与 push_slice_overwrite

#### 4.3.1 概念说明

单个 `push_overwrite` 解决了「写一个、挤一个」。但如果你有一**大批**数据要灌进去，循环调用 `push_overwrite` 会逐个推进索引（对 `SharedRb` 而言每次推进都是一次跨核原子同步）。为此 `RingBuffer` 提供了两个批量版本：

- `push_iter_overwrite(iter)`：从一个**迭代器**不断取元素覆盖写入，**直到迭代器耗尽**。最终缓冲区里保留的是迭代器的**最后 `min(iter.len(), capacity)` 个元素**。不要求元素 `Copy`。
- `push_slice_overwrite(elems)`：从一个**切片**覆盖写入。要求元素 `Copy`。若切片长度大于容量，只保留切片的**最后 `capacity` 个元素**。

两者的「只保留最后 N 个」语义和 `push_overwrite` 一致：越早进来的越先被挤掉。

> 小贴士：`push_iter_overwrite` 不要求 `Copy`，因为它一个一个 `push_overwrite`，每个元素都被 `try_push`「移动」进缓冲区；`push_slice_overwrite` 要求 `Copy`，是因为它底层用 `push_slice`（按字节复制一段切片），这是 `push_slice` 本身的约束。

#### 4.3.2 核心流程

**`push_iter_overwrite`** 极其直白——它就是对每个元素调用 `push_overwrite`：

```rust
fn push_iter_overwrite<I: Iterator<Item = Self::Item>>(&mut self, iter: I) {
    for elem in iter {
        self.push_overwrite(elem);
    }
}
```

注意它的返回值是 `()`（无返回）——因为它会消费**整个**迭代器，最终保留多少个、挤掉多少个，用户从返回值里也拿不到（想要统计得自己数）。文档强调「消耗迭代器直到结尾」。

**`push_slice_overwrite`** 则精巧得多，它不逐个 pop，而是「先一次性丢弃该丢的，再一次写入该写的」：

```rust
fn push_slice_overwrite(&mut self, elems: &[Self::Item])
where
    Self::Item: Copy,
{
    if elems.len() > self.vacant_len() {
        self.skip(usize::min(elems.len() - self.vacant_len(), self.occupied_len()));
    }
    self.push_slice(if elems.len() > self.vacant_len() {
        &elems[(elems.len() - self.vacant_len())..]
    } else {
        elems
    });
}
```

逐步拆解（设切片长度为 `n`，当前空闲数 `v0`，占用数 `o0`，容量 `c`）：

1. **判断是否需要丢弃**：若 `n > v0`（切片装不下），就要丢弃一些旧元素腾地方。
2. **计算丢弃量**：`min(n - v0, o0)`。`n - v0` 是「还差几个空位」，但不能超过现有占用数 `o0`，所以取 min。用 `skip` 一次性丢弃这 `k` 个最旧元素（`skip` 会 `drop_in_place` 再推进 read 索引）。
3. **决定写入哪一段切片**：再判断一次 `n > 现在的 vacant_len()`（注意：此时 `self.vacant_len()` 已经因为 `skip` 而**增大**了，这里**重新读取**的是新值）：
   - 若 `n` 仍大于新空闲数（说明切片比整个容量还长，`n > c`）：只写切片的**尾部** `&elems[(n - 新空闲)..]`，即最后 `c` 个；
   - 否则（切片现在能全装下了）：写整个 `elems`。
4. **`push_slice`** 把这段切片复制进缓冲区。

这里有一个**容易看错的精妙之处**：步骤 3 里的 `self.vacant_len()` 是在 `skip` 之后**重新求值**的，不是步骤 1 的 `v0`。正因为 `skip` 把空闲数从 `v0` 增加到了 `v0 + k`，步骤 3 的判断和切片下标才会刚好正确。下表用一个具体例子验证。

设容量 `c = 3`，当前缓冲区为 `[10, 20]`（`o0 = 2, v0 = 1`），切片 `elems = [1, 2, 3, 4, 5]`（`n = 5 > c`）：

| 步骤 | 计算 | 结果 |
| --- | --- | --- |
| 1. `n > v0`? | `5 > 1` | 是 |
| 2. 丢弃量 `k` | `min(5-1, 2) = min(4,2) = 2` | `skip(2)`，丢弃 `10,20`，缓冲区空，新空闲数 `= 3` |
| 3. `n > 新空闲`? | `5 > 3` | 是 → 写 `&elems[5-3..] = &elems[2..] = [3,4,5]` |
| 4. `push_slice([3,4,5])` | 复制进空缓冲区 | 最终 `[3, 4, 5]` |

最终缓冲区 `[3, 4, 5]`，正是切片的最后 `capacity = 3` 个元素，与文档「只保留最后 capacity 个」一致。

再验证一个「切片能全装下」的例子：容量 `c = 5`，缓冲区 `[10, 20]`（`o0=2, v0=3`），切片 `[1, 2]`（`n=2`）。`n > v0`? `2 > 3` 否 → 不丢弃；步骤 3 `2 > 3` 否 → 写整个 `[1,2]`。结果 `[10, 20, 1, 2]`，保留了全部旧元素和新元素。

#### 4.3.3 源码精读

两个批量方法的默认实现：

[`push_iter_overwrite`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L35-L43)（`src/traits/ring_buffer.rs:35-43`）—— 逐元素 `push_overwrite`，消耗整个迭代器。

[`push_slice_overwrite`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L45-L60)（`src/traits/ring_buffer.rs:45-60`）—— 先 `skip` 丢弃、再 `push_slice` 写入尾部，注意中间 `self.vacant_len()` 的重新求值。

它们复用的两个底层方法：

- `skip`（[Consumer 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L218-L228)，`src/traits/consumer.rs:218-228`）—— 先对要跳过的元素 `drop_in_place`（安全回收非 `Copy` 资源），再 `advance_read_index` 推进 read 索引。这里用它来「丢弃最旧的 k 个元素腾地方」。
- `push_slice`（[Producer 默认实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L96-L104)，`src/traits/producer.rs:96-104`）—— 按 `Copy` 复制一段切片到空闲槽，最后**一次性** `advance_write_index` 提交。这就是批量方法比逐个 `push_overwrite` 高效的关键：写 N 个只触发一次索引推进（一次跨核同步）。

#### 4.3.4 代码实践

**实践目标**：对比 `push_iter_overwrite` 与 `push_slice_overwrite` 的「只保留最后 N 个」语义，并手算验证。

**操作步骤**：写一段「示例代码」（非项目原有）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    // 1) push_iter_overwrite：容量 3，灌入 0..6
    let mut rb = HeapRb::<i32>::new(3);
    rb.push_iter_overwrite(0..6);
    // 预期只保留最后 3 个：[3, 4, 5]
    let got: Vec<i32> = rb.pop_iter().collect();   // 注意：pop_iter 会真正移除元素
    println!("iter_overwrite: {:?}", got);

    // 2) push_slice_overwrite：容量 3，灌入 &[1,2,3,4,5]
    let mut rb2 = HeapRb::<i32>::new(3);
    rb2.push_slice_overwrite(&[1, 2, 3, 4, 5]);
    // 预期只保留最后 3 个：[3, 4, 5]
    let got2: Vec<i32> = rb2.pop_iter().collect();
    println!("slice_overwrite: {:?}", got2);
}
```

> 提示：`pop_iter()` 是 `Consumer` 提供的惰性移除迭代器（见 u3-l3），在它 `Drop`/`commit` 时才真正推进 read 索引；`collect` 会驱动它取走所有元素。

**需要观察的现象**：两次输出都应该是 `[3, 4, 5]`（各自输入的最后 3 个）。

**预期结果**：
```
iter_overwrite: [3, 4, 5]
slice_overwrite: [3, 4, 5]
```

**若无法本地验证**：以上结论已通过对 `push_slice_overwrite` 的逐步手算（见 4.3.2 表格）得出，逻辑可复现；但你仍应在本地运行一次以确认行为，尤其是 `pop_iter().collect()` 的取走时机。

#### 4.3.5 小练习与答案

**练习 1**：在 `push_slice_overwrite` 中，为什么步骤 3 的 `self.vacant_len()` 必须在 `skip` **之后**重新读取，而不能复用步骤 1 的旧值？

**参考答案**：因为 `skip(k)` 丢弃了 `k` 个最旧元素，把空闲数从 `v0` 增加到了 `v0 + k`。步骤 3 要判断「切片现在是否装得下」，必须用**更新后**的空闲数。若复用旧值 `v0`，会在「切片本已装得下」时仍错误地截取尾部、白白丢掉前面的元素，破坏「只保留最后 min(n, c) 个」的语义。

**练习 2**：如果要覆盖写入的数据来自一个 `Vec<String>`（`String` 不是 `Copy`），应该用 `push_iter_overwrite` 还是 `push_slice_overwrite`？为什么？

**参考答案**：用 `push_iter_overwrite`。因为 `push_slice_overwrite` 要求 `Self::Item: Copy`（它底层用 `push_slice` 按字节复制），`String` 不满足。而 `push_iter_overwrite` 逐个 `push_overwrite`，每个 `String` 都是**移动**进缓冲区的，不需要 `Copy`。

---

### 4.4 独占访问：为何 overwrite 不能在 split 之后使用

#### 4.4.1 概念说明

这是本讲最容易踩坑、也最常被面试问到的点。结论先放出来：

> **三个 `*_overwrite` 方法只在缓冲区「未被拆分」时可用。一旦调用 `split()`，你拿到的 `Prod`/`Cons` 句柄就再也调不到它们了。**

原因有**两层**，一层是所有权与类型（编译期就能挡住你），一层是并发安全（运行期的 SPSC 不变量）。理解这两层，你才真正懂了 overwrite。

**第一层：类型层面 —— `Prod`/`Cons` 没有实现 `RingBuffer`。**

`push_overwrite` 等方法定义在 `RingBuffer` trait 上。split 之后你手里只有 `Producer` 句柄（`Prod`/`CachingProd`）和 `Consumer` 句柄（`Cons`/`CachingCons`）。这两个句柄**分别只实现** `Producer` 或 `Consumer`，谁都没有同时实现两者，所以谁都不满足 `RingBuffer: Observer + Consumer + Producer` 的要求。编译器在类型层面就直接拒绝：你压根找不到 `push_overwrite` 方法。

**第二层：并发安全层面 —— overwrite 会同时动 read 和 write 两个索引，违反 SPSC。**

`push_overwrite` 内部调了 `try_pop`（推进 read 索引）和 `try_push`（推进 write 索引）。但在 SPSC 模型里，read 索引是**消费者独占**的、write 索引是**生产者独占**的。如果允许 split 后的 producer 句柄调用 overwrite，就等于让生产者去动消费者的 read 索引——两个执行流同时改 read 索引，立即产生数据竞争。所以即便类型层面不挡，这种行为在语义上也是非法的。

两层原因归结为同一个根本事实：**overwrite 需要同时拥有 read 端和 write 端，而 split 的全部意义就是把这两端分开**。

#### 4.4.2 核心流程

把「能不能 overwrite」和「缓冲区处于什么状态」对应起来：

```
┌─────────────────────────────────┐    ┌──────────────────────────────────┐
│  未拆分：拥有完整 HeapRb (&mut)  │    │  已拆分：split() 之后             │
│                                 │    │                                  │
│  实现 RingBuffer ✅              │    │  Prod: 仅 Producer  ❌无overwrite │
│  → push_overwrite 可用 ✅        │    │  Cons: 仅 Consumer  ❌无overwrite │
│                                 │    │                                  │
│  hold 标志均为 false（无端占用） │    │  hold_read/hold_write 被置 true   │
└─────────────────────────────────┘    └──────────────────────────────────┘
```

- overwrite 方法签名是 `&mut self`，要求**独占可变借用**。未拆分时你持有 `&mut HeapRb`，天然满足；split 后缓冲区已被搬进 `Arc`/`Rc`，你只有共享句柄，拿不到 `&mut`。
- split 时，包装器的 `new` 会把对应的 hold 标志置为 `true`（断言此前没人占用，否则 panic），从此 read/write 两端各归其主。

#### 4.4.3 源码精读

**类型证据一：`Prod`/`Cons` 只实现单侧 trait。**

[`Direct` 包装器与 `Prod`/`Cons` 别名](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L20-L30)（`src/wrap/direct.rs:20-30`）—— `Prod<R> = Direct<R, true, false>`、`Cons<R> = Direct<R, false, true>`，用 const generic 编码「只持有写权」或「只持有读权」。

随后只有两个针对性的 impl，谁都没碰 `RingBuffer`：

[`impl Producer for Prod`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L134-L139)（`src/wrap/direct.rs:134-139`）—— `Prod` 只实现 `Producer`。

[`impl Consumer for Cons`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L141-L145)（`src/wrap/direct.rs:141-145`）—— `Cons` 只实现 `Consumer`。

`SharedRb::split` 产出的 `CachingProd`/`CachingCons` 也是同理（各自只实现一侧），所以同样拿不到 `push_overwrite`。

**类型证据二：`Direct::new` 用 hold 标志断言唯一性。**

[`Direct::new` 的 hold 断言](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L42-L50)（`src/wrap/direct.rs:42-50`）—— 创建 `Prod`（`P=true`）时 `hold_write(true)` 并断言旧值为 `false`（此前没有 producer）；创建 `Cons` 时同理对 `hold_read`。这就是「重复拆分会 panic」的来源（详见 u5-l2）：

```rust
pub fn new(rb: R) -> Self {
    if P {
        assert!(!unsafe { rb.rb().hold_write(true) });
    }
    if C {
        assert!(!unsafe { rb.rb().hold_read(true) });
    }
    Self { rb }
}
```

**并发安全证据：overwrite 同时动两个索引。**

回到 [`push_overwrite` 的实现](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L26-L33)（`src/traits/ring_buffer.rs:26-33`）：`self.try_pop()` 推进 read 索引、`self.try_push(elem)` 推进 write 索引。在 SPSC 下，read 索引归 consumer、write 索引归 producer，一个方法同时改两者，意味着调用者必须**同时**是这两端的唯一主人——也就是未拆分时的拥有者本人。

#### 4.4.4 代码实践

**实践目标**：亲手验证「split 之后编译器拒绝 overwrite」，并理解报错原因。

**操作步骤**：写一段「示例代码」（非项目原有），**故意**在 split 之后尝试 overwrite：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(4);
    let (mut prod, _cons) = rb.split();
    // 下面这一行会编译失败：
    prod.push_overwrite(0);
}
```

**需要观察的现象**：`cargo build` 时编译器报错，大致是「no method named `push_overwrite` found for `HeapProd<...>`」或「method not found in `&mut CachingProd<...>`」。

**预期结果**：编译失败。原因是 `prod` 的类型（`HeapProd` = `CachingProd<Arc<HeapRb<i32>>>`）只实现了 `Producer`，没有实现 `RingBuffer`，而 `push_overwrite` 是 `RingBuffer` 的方法。

> 思考题（对应规格里的问题）：若已 split，还能否调用 `push_overwrite`？
> **答案**：不能。两层原因：(1) 类型上，split 后的句柄不实现 `RingBuffer`，方法根本不存在；(2) 语义上，overwrite 要同时动 read/write 两个索引，违反 SPSC「read 归消费者、write 归生产者」的独占约定。想要在多线程下用「满了就丢旧的」语义，需要用其它模式（例如消费者端定期 `pop`，或在单端用 `push_overwrite` 前不 split）。

**若无法本地验证**：上述编译期报错是确定性的——`push_overwrite` 只存在于 `RingBuffer` trait，而 `HeapProd` 不实现该 trait，因此必然编译失败。你可以在本地 `cargo build` 复现。

#### 4.4.5 小练习与答案

**练习 1**：假设你想要「多个生产者往同一个缓冲区写，满了就覆盖最旧的」——ringbuf 能直接支持吗？为什么？

**参考答案**：不能。ringbuf 是 SPSC（单生产者单消费者），hold 标志强制「至多一个 producer」。多个生产者会违反这个不变量，`Direct::new` 的 `assert!` 会 panic。而且 overwrite 需要独占 `&mut self`，多生产者下也无法获得。多生产者场景应考虑 MPSC 队列（如 `crossbeam-channel`、`tokio::sync::mpsc`）。

**练习 2**：`push_overwrite(&mut self)` 为什么要求 `&mut self`（独占可变借用），而 `try_push(&mut self, ...)` 同样是 `&mut self` 却能在 split 后的 `Prod` 上调用？

**参考答案**：`Prod` 自身实现了 `Producer`，所以它**自己**就有 `&mut self` 可以调 `try_push`——这里的 `&mut self` 是对 `Prod` 句柄的可变借用，句柄内部通过共享的 `Arc<HeapRb>` 去改 write 索引。但 `push_overwrite` 是 `RingBuffer` 的方法，要求对**整个缓冲区**同时有读和写的能力；`Prod` 只持有写权（不实现 `Consumer`，更不实现 `RingBuffer`），所以拿不到这个方法。关键区别在于：`try_push` 只动 write 索引（producer 的合法权限），而 `push_overwrite` 还要动 read 索引（越权）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「最近 N 条事件日志」的小任务。

**任务描述**：实现一个固定容量的「最近事件缓冲区」——不断写入事件，缓冲区永远只保留**最近 capacity 条**，老的自动被覆盖丢弃；并能查询「上一条写入时是否发生了覆盖、被丢弃的是谁」。

**操作步骤**（写一段「示例代码」，非项目原有）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    // 容量 3：永远只保留最近 3 条事件
    let mut rb = HeapRb::<u32>::new(3);

    // 依次写入 1..=6，记录每次是否覆盖
    for e in 1..=6u32 {
        match rb.push_overwrite(e) {
            Some(dropped) => println!("写入 {e}：缓冲区满，丢弃了 {dropped}"),
            None => println!("写入 {e}：直接放入"),
        }
    }

    // 查看当前保留的内容（应为最近 3 条：4, 5, 6）
    let snapshot: Vec<u32> = rb.iter().copied().collect();
    println!("当前缓冲区内容: {:?}", snapshot);

    // 演示批量覆盖：一次性灌入更多
    rb.push_slice_overwrite(&[100, 200, 300, 400]);
    let after: Vec<u32> = rb.iter().copied().collect();
    println!("批量覆盖后内容: {:?}", after); // 预期 [200, 300, 400]
}
```

**需要观察的现象**：

- 前 3 次写入（1、2、3）都是「直接放入」（缓冲区未满）。
- 第 4、5、6 次写入发生覆盖，被丢弃的分别是 1、2、3。
- 写完 1..=6 后缓冲区为 `[4, 5, 6]`。
- 批量覆盖灌入 `[100,200,300,400]`（长度 4 > 容量 3）后，缓冲区只保留最后 3 个 `[200, 300, 400]`。

**预期结果**：
```
写入 1：直接放入
写入 2：直接放入
写入 3：直接放入
写入 4：缓冲区满，丢弃了 1
写入 5：缓冲区满，丢弃了 2
写入 6：缓冲区满，丢弃了 3
当前缓冲区内容: [4, 5, 6]
批量覆盖后内容: [200, 300, 400]
```

**复盘要点**（对照本讲四个模块）：

1. 能在 `rb` 上直接调 `push_overwrite` / `push_slice_overwrite`，是因为**未拆分**的 `HeapRb` 实现了 `RingBuffer`（模块 4.1）。
2. `push_overwrite` 返回的 `Some(被丢弃值)` 精确告诉我们覆盖了谁（模块 4.2）。
3. `push_slice_overwrite` 一次丢弃、一次写入，高效且只保留尾部（模块 4.3）。
4. 全程没有 `split()`——一旦 split，上述方法都会从类型层面消失（模块 4.4）。

> 若无法确定运行结果：本任务的结论已通过对源码的逐步推演得出；建议在本地运行一次以确认 `iter()` 的只读遍历顺序（从最旧到最新）与上述预期一致。

---

## 6. 本讲小结

- `RingBuffer` 是 `Observer + Consumer + Producer` 的**拥有者超集** trait，描述「同时拥有读写两端」的类型（如未拆分的 `HeapRb`、`StaticRb`、`LocalRb`）。它额外提供 `hold_read`/`hold_write` 与三个 `*_overwrite` 方法。
- `hold_read` / `hold_write` 是两个布尔标志（`SharedRb` 用 `AtomicBool`、`LocalRb` 用 `Cell<bool>`），编码 SPSC「至多一个消费者/生产者」的拥有权，由 `unsafe` 方法直接读写并返回旧值。
- `push_overwrite(elem)` 的语义是「满了就先 `try_pop` 丢弃最旧的，再 `try_push` 写入新的」，返回 `Some(被丢弃的旧元素)` 或 `None`。
- `push_iter_overwrite` 逐元素覆盖、消耗整个迭代器、不要求 `Copy`；`push_slice_overwrite` 用 `skip` + `push_slice` 批量覆盖、要求 `Copy`、只保留切片最后 `capacity` 个——注意它内部 `vacant_len()` 在 `skip` 后会重新求值。
- **核心约束**：三个 overwrite 方法都需要 `&mut self` 且会同时动 read/write 两个索引，因此**只能在缓冲区未拆分时使用**。split 后的 `Prod`/`Cons` 只实现单侧 trait、不实现 `RingBuffer`，类型层面就拿不到这些方法；语义上 overwrite 也违反 SPSC 的索引独占约定。
- 批量方法之所以比循环 `push_overwrite` 高效，是因为底层 `push_slice` 只在最后**一次性**推进 write 索引（对 `SharedRb` 即一次跨核同步）。

---

## 7. 下一步学习建议

本讲讲完了 `RingBuffer` 这一「拥有者」trait。接下来的学习方向：

1. **u3-l5 委托机制：Delegate traits 与 Based 的组合魔法**。本讲反复出现的 `DelegateRingBuffer` + blanket impl（[`src/traits/ring_buffer.rs:63-98`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L63-L98)）正是委托机制的体现，下一讲会系统讲清 `Based` / `Delegate*` 如何让包装器零成本复用核心逻辑。
2. **u4 单元：包装器与同步策略**。本讲提到 split 后的 `Prod`/`Cons` 来自 `Direct`/`Caching` 包装器，下一单元会逐一讲透 `Direct`、`Frozen`、`Caching` 三种同步策略及背后的 `Wrap`/`RbRef` 抽象。
3. **u5-l2 Hold flags：保证 SPSC 不变量**。本讲对 `hold_read`/`hold_write` 的介绍偏「定位」，专家层那一讲会深入讲清 hold 标志在 `new`/`clone`/`drop` 路径上的置位与复位、以及它如何运行时强制「至多一个 producer/consumer」。
4. **动手延伸**：试着在不 split 的 `StaticRb`（无堆、`#![no_std]` 可用）上调用 `push_overwrite`，验证覆盖写入同样适用于静态存储后端——这能帮你体会「`RingBuffer` 与存储后端正交」的设计。
