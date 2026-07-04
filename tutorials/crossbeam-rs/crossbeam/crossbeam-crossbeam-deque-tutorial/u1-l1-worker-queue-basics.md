# Worker 队列上手：push、pop 与 FIFO/LIFO

## 1. 本讲目标

前两讲（u1-l1、u1-l2）帮我们从外部认识了 `crossbeam-deque` 是什么、目录与构建配置长什么样，但**还没真正碰过 `src/deque.rs` 里的实现**。本讲是全手册**第一次进入实现文件**，但只到「会用、能读懂 API」的程度，算法细节（无锁 CAS、内存序、扩缩容）留给进阶层（u2）和专家层（u4）。

学完本讲，你应该能够：

1. 说清 `Flavor` 枚举（`Fifo` / `Lifo`）的语义：**push 在两种模式下完全一样**，区别只在 `pop` 从哪一端取。
2. 画出 `Worker<T>` 的四个字段（`inner` / `buffer` / `flavor` / `_marker`），并解释 `_marker: PhantomData<*mut ()>` 这个「哨兵字段」的作用，以及为什么 `Worker` 是 `Send + !Sync`（只能被单个线程拥有、但可以在线程间 move）。
3. 用 `Worker::new_fifo()` / `Worker::new_lifo()` 创建队列，用 `stealer()` 派生一个可共享的 `Stealer`。
4. 用 `push` / `pop` / `is_empty` / `len` 完成基本的入队、出队、判空、计数，并能**预测 FIFO 与 LIFO 两种 flavor 下的出队顺序差异**。
5. 自己写出一个最小 cargo 例子，验证 FIFO 出队是 `1, 2, 3`、LIFO 出队是 `3, 2, 1`。

本讲**不**讲：`Stealer::steal` / `Injector` 的工作流（那是 u1-l4）、`push`/`pop` 内部的 CAS 与内存序（u2-l2）、缓冲区扩缩容（u2-l5）、`epoch` 内存回收（u4-l2）。

## 2. 前置知识

- **队列的 FIFO 与 LIFO**（u1-l1 已讲，这里复述要点）：
  - FIFO = First In First Out，先进先出，像排队买饭。
  - LIFO = Last In First Out，后进先出，像一摞盘子。
- **Rust 的所有权与 `Send`/`Sync`**：
  - `Send`：类型可以**在线程间 move（转移所有权）**。
  - `Sync`：类型可以**被多个线程通过 `&T`（共享引用）同时访问**。
  - 二者是自动 trait（auto trait）：编译器根据字段自动推导；只要有一个字段不满足，整个类型就不满足，除非用 `unsafe impl` 显式「打开」。
- **`PhantomData<T>`**：一个零大小的「标记类型」，本身不占内存，用来告诉编译器「我假装持有一个 `T`」，从而让自动 trait 推导、生命周期推导按 `T` 来走。
- **`Cell<T>`**：提供「内部可变性」的单线程容器——可以通过 `&Cell<T>` 改里面的值。代价是 `Cell` 永远是 `!Sync`（不能跨线程共享引用）。
- **`Arc<T>`**：原子引用计数的共享指针，多个所有者可以同时持有，是 `Send + Sync`（当 `T: Send + Sync` 时）。

> 如果你还没读过 u1-l2，请先看它对 `src/lib.rs` 模块导出（`pub use ...{Injector, Steal, Stealer, Worker}`）的讲解。本讲只聚焦其中被导出的 `Worker` 类型本身。

## 3. 本讲源码地图

本讲的所有源码都来自**同一个文件** `src/deque.rs`（全 crate 的实现都集中在这里），只看其中最顶层的 `Worker` 部分以及它在 `src/lib.rs` 里的文档说明：

| 文件 / 位置 | 作用 | 本讲精读范围 |
| --- | --- | --- |
| `src/deque.rs` 顶部（`Flavor` 与 `Worker` 定义） | 队列 flavor 与 `Worker` 结构体 | `L147-L211` |
| `src/deque.rs`（`Worker` 构造与派生） | `new_fifo` / `new_lifo` / `stealer` | `L214-L287` |
| `src/deque.rs`（查询方法） | `is_empty` / `len` | `L352-L386` |
| `src/deque.rs`（入队出队） | `push` / `pop` | `L388-L545` |
| `src/lib.rs` | crate 级文档对两种构造器的方向语义说明 | `L17-L27` |
| `tests/fifo.rs` | FIFO 的基本正确性测试 `smoke` | `L12-L46` |

记住一个关键事实：**`push` 在 FIFO 和 LIFO 两种模式下代码完全相同**，唯一的区别藏在 `pop` 里根据 `flavor` 走不同分支。理解了这一点，本讲就抓住了一半。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** `Flavor` 枚举：FIFO 与 LIFO 的方向语义
- **4.2** `Worker<T>` 结构体：字段与「单线程拥有」约束
- **4.3** 构造与派生：`new_fifo` / `new_lifo` / `stealer`
- **4.4** 入队出队：`push` / `pop` / `is_empty` / `len` 与 FIFO/LIFO 顺序对比

### 4.1 Flavor 枚举：FIFO 与 LIFO 的方向语义

#### 4.1.1 概念说明

`crossbeam-deque` 的本地队列（`Worker`）有两种「口味（flavor）」：先进先出（FIFO）和后进先出（LIFO）。这两种口味**共用同一套底层数据结构和同一份 `push` 代码**，只在 `pop` 时从不同的端取任务。

理解方向语义的关键是 Chase-Lev 双端队列的「两个游标」模型：

- `front`：队列的**前端**索引（最早入队任务所在）。
- `back`：队列的**后端**索引（下一个写入位置）。
- 队列里有效任务的下标范围是 `[front, back)`，长度 \( \text{len} = \text{back} - \text{front} \)。

那么 push 和 pop 分别动哪个游标？

- `push` **永远**写在 `back` 处，然后把 `back` 加 1（不管哪种 flavor）。
- `pop` 根据 flavor 分叉：
  - **FIFO**：从 `front` 端取，读 `front` 处的任务后把 `front` 加 1。push 在 back、pop 在 front → **两端相反** → 先进先出。
  - **LIFO**：从 `back` 端取，先把 `back` 减 1，再读 `back` 处的任务。push 在 back、pop 也在 back → **同一端** → 后进先出。

`src/lib.rs` 的 crate 级文档就是用「相反端 / 同一端」来描述这两种构造器的：

- `new_fifo()`：tasks are pushed and popped from **opposite** ends（相反端）。
- `new_lifo()`：tasks are pushed and popped from the **same** end（同一端）。

#### 4.1.2 核心流程

用一个统一的心智模型记住两种 flavor：

```
共享部分（FIFO/LIFO 完全一样）：
  push(task):  写 slot[back]; back += 1

只在 pop 分叉：
  FIFO pop:    task = slot[front]; front += 1   // 取最老的
  LIFO pop:    back -= 1; task = slot[back]      // 取最新的
```

以 `push 1, 2, 3` 为例，写完后游标和槽位如下（`front=0, back=3`，槽位 `slot[0]=1, slot[1]=2, slot[2]=3`）：

- FIFO 连续 pop → 读 `slot[0]`、`slot[1]`、`slot[2]` → 出队序列 **`1, 2, 3`**。
- LIFO 连续 pop → 先 `back=2` 读 `slot[2]`，再 `back=1` 读 `slot[1]`，再 `back=0` 读 `slot[0]` → 出队序列 **`3, 2, 1`**。

#### 4.1.3 源码精读

`Flavor` 是一个极简的内部枚举（注意它是私有的 `enum`，没有 `pub`，使用者只能通过 `new_fifo`/`new_lifo` 间接选择）：

- [src/deque.rs:147-155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L147-L155)：定义 `Flavor { Fifo, Lifo }`，派生了 `Clone/Copy/Debug/Eq/PartialEq`，因为 `Worker`/`Stealer` 会把它作为一个普通值字段存起来。

`src/lib.rs` 对两种构造器的方向语义给出权威说明：

- [src/lib.rs:17-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L17-L27)：`new_fifo()` 在相反端 push/pop；`new_lifo()` 在同一端 push/pop；并强调「每个 `Worker` 只被单个线程拥有，只支持 push 和 pop」。

`Worker` 结构体的文档注释里还直接给出了一个对照示例（先 push 1,2,3，让 stealer 偷走 1，再 pop 剩下的），FIFO 剩下 `2, 3`、LIFO 剩下 `3, 2`，正是本节心智模型的官方佐证：

- [src/deque.rs:162-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L162-L196)：FIFO 文档示例断言 `pop` 得到 `2` 再 `3`；LIFO 文档示例断言 `pop` 得到 `3` 再 `2`。

#### 4.1.4 代码实践

1. **实践目标**：不写代码，先凭本节的「游标心智模型」预测结果，再与源码文档断言核对。
2. **操作步骤**：
   - 假设 `push 10, 20, 30, 40`，写出 `front`、`back` 的值与各槽位。
   - 预测 FIFO 连续 pop 四次的返回序列。
   - 预测 LIFO 连续 pop 四次的返回序列。
   - 打开 [src/deque.rs:162-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L162-L196)，对比你的预测与文档断言的方向是否一致。
3. **需要观察的现象**：FIFO 序列应与入队顺序相同；LIFO 序列应是入队顺序的反转。
4. **预期结果**：
   - 写完后 `front=0, back=4`，`slot[0..4] = [10,20,30,40]`。
   - FIFO pop 序列：`10, 20, 30, 40`。
   - LIFO pop 序列：`40, 30, 20, 10`。
5. 这是纯推理练习，无需运行；如要验证可留到 4.4 的可运行例子。

#### 4.1.5 小练习与答案

- **练习 1**：如果只想让本地队列表现得像「函数调用栈」（最新提交的任务最先执行），应该选 `new_fifo` 还是 `new_lifo`？
  - **答案**：`new_lifo`。栈就是 LIFO，最新的任务（最后 push 的）最先被 pop 执行。
- **练习 2**：为什么 `Flavor` 不实现成 `pub`，而是私有的内部类型？
  - **答案**：使用者只需要通过 `new_fifo()` / `new_lifo()` 两个构造器表达意图，不必（也不应）直接持有或传递 `Flavor` 值；把它藏起来可以减少 API 表面积，也避免外部对内部表示产生依赖。

---

### 4.2 Worker&lt;T&gt; 结构体：字段与「单线程拥有」约束

#### 4.2.1 概念说明

`Worker<T>` 是「某个工作线程私有的本地队列」。它的设计有一个核心约束：**同一个 `Worker` 在任意时刻只能被一个线程使用**。这个约束不是用 mutex 强制的，而是用 Rust 的类型系统（`Send`/`Sync` 自动 trait）在编译期就挡住非法用法。

要做到这一点，`Worker` 用了一个常见技巧：放一个 `_marker: PhantomData<*mut ()>` 「哨兵字段」。

- `*mut ()`（裸指针）天生是 `!Send + !Sync` 的（Rust 不信任裸指针的线程安全性）。
- `PhantomData<T>` 在自动 trait 推导上「假装」结构体持有一个 `T`，于是 `PhantomData<*mut ()>` 让结构体默认就是 `!Send + !Sync`。
- 这样作者就被**强制**要用 `unsafe impl` 显式声明自己真正想要哪些 trait——这是一种「先全部关掉，再按需打开」的防御性写法。

接着看作者打开了什么：

- [src/deque.rs:211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L211) 写了 `unsafe impl<T: Send> Send for Worker<T> {}`，**把 `Send` 开回来了**。
- 全文件**没有** `unsafe impl ... Sync for Worker<T>`。

所以 `Worker<T>` 的真实自动 trait 状态是：**`Send + !Sync`**（当 `T: Send` 时）。

> 说明：结构体里那行注释 `// !Send + !Sync` 描述的是「这个 marker 字段让默认状态变成 `!Send + !Sync`」，是作者注释 marker 的作用，而非类型最终的 trait 状态。最终因为只有 `unsafe impl Send`、没有 `unsafe impl Sync`，结果是 `Send + !Sync`。学习目标里说的「只能被单个线程拥有」正是由 `!Sync` 保证的：你可以把 `Worker` **move** 进一个线程（`Send` 允许），但不能把 `&Worker` **共享**给多个线程（`!Sync` 禁止）。

为什么必须 `!Sync`？看 `Worker` 还有一个字段 `buffer: Cell<Buffer<T>>`——这是线程私有的「缓冲区指针缓存」，用来在 `push`/`pop` 时避免每次都做一次原子加载（提速）。`Cell` 本身就是 `!Sync`（它允许通过共享引用修改内部值，跨线程共享会数据竞争）。所以「单线程拥有」既是性能优化的前提，也是正确性的要求。

#### 4.2.2 核心流程

`Worker` 的四个字段分工：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `inner` | `Arc<CachePadded<Inner<T>>>` | 与 `Stealer` 共享的「真实」队列状态（`front`/`back`/`buffer`），用 `Arc` 多所有者共享，`CachePadded` 防伪共享 |
| `buffer` | `Cell<Buffer<T>>` | 线程私有的缓冲区指针缓存，`Cell` 提供内部可变性、单线程快速访问，是 `!Sync` 的来源之一 |
| `flavor` | `Flavor` | 记住这是 FIFO 还是 LIFO |
| `_marker` | `PhantomData<*mut ()>` | 哨兵：把默认 trait 状态拉到 `!Send + !Sync`，强制作者显式 `unsafe impl` |

`CachePadded` 是 `crossbeam-utils` 提供的包装，会把数据对齐并填充到一个缓存行大小（通常 64 字节），避免两个线程各自频繁修改的字段落在同一个缓存行上（「伪共享 / false sharing」会拖慢并发性能）。`inner` 用 `CachePadded` 包裹，是为了和 `Stealer` 在并发访问 `front`/`back` 时不互相干扰。这些细节会在 u2-l1 详讲。

#### 4.2.3 源码精读

- [src/deque.rs:197-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209)：`Worker<T>` 的四个字段定义，`_marker` 那行的注释 `// !Send + !Sync` 解释了 marker 的用途。
- [src/deque.rs:211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L211)：`unsafe impl<T: Send> Send for Worker<T> {}`——只开 `Send`，不开 `Sync`。
- 对比 `Stealer`：[src/deque.rs:583-584](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L583-L584) 同时有 `unsafe impl Send` 和 `unsafe impl Sync`，所以 `Stealer` 是 `Send + Sync`（可以跨线程共享），这与 `Worker` 形成鲜明对比。`Stealer` 之所以能 `Sync`，是因为它**没有** `Cell<Buffer<T>>` 这个私有缓存字段。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `Worker` 是 `!Sync`，理解「单线程拥有」是编译期强制的。
2. **操作步骤**：写一个小程序，尝试用一个 `thread::spawn` 的子线程通过**共享引用**访问主线程的 `Worker`：

   ```rust
   // 示例代码（预期编译失败）
   use crossbeam_deque::Worker;
   use std::thread;

   fn main() {
       let w = Worker::<i32>::new_fifo();
       let w_ref = &w;
       thread::spawn(move || {
           // 试图在子线程里通过 &Worker 访问
           println!("{}", w_ref.is_empty());
       });
   }
   ```

3. **需要观察的现象**：编译器报错，提示 `Worker` 不能在多线程间共享（`!Sync`）。错误信息会指向 `Worker` 中 `!Sync` 的字段（`Cell` 类型的 `buffer`，或 `*mut ()` 相关的 marker）。
4. **预期结果**：编译失败；把 `&w` 换成「把 `w` 直接 move 进闭包」（即 `move || { let _ = w.is_empty(); }`）则能编译——这正说明 `Worker` 是 `Send`（可 move）但 `!Sync`（不可共享 `&`）。
5. **待本地验证**：不同编译器版本给出的具体错误措辞可能不同；重点是确认「共享 `&Worker` 不通过、move `Worker` 通过」这一对比结论。

#### 4.2.5 小练习与答案

- **练习 1**：如果删掉 `_marker` 字段，`Worker` 还会是 `!Send` 吗？还会是 `!Sync` 吗？
  - **答案**：`!Send` 的「默认来源」是 `PhantomData<*mut ()>`，删掉后 `Arc<...>` 和 `Cell<Buffer<T>>` 在 `T: Send` 时一般会让 `Worker` 自动变成 `Send`。而 `!Sync` 的来源是 `Cell<Buffer<T>>`（`Cell` 永远 `!Sync`），所以**即使删掉 `_marker`，`Worker` 仍然是 `!Sync`**。`_marker` 的真正作用是「把 `Send` 也一并关掉，逼作者用 `unsafe impl` 显式声明意图」，是一种防御性设计。
- **练习 2**：为什么 `Worker` 可以 `Send`（move 到别的线程）却不能 `Sync`（共享 `&`）？
  - **答案**：`Send` 只要求「同一时刻只有一个所有者」，move 后原线程不再访问，安全；而 `buffer: Cell<Buffer<T>>` 这个私有缓存允许通过 `&` 修改内部值，若两个线程同时持有 `&Worker` 就会在无同步的情况下并发改这块缓存，构成数据竞争，因此必须 `!Sync`。

---

### 4.3 构造与派生：new_fifo / new_lifo / stealer

#### 4.3.1 概念说明

`Worker` 没有公开的 `new()`，而是提供两个语义清晰的构造器：`new_fifo()` 和 `new_lifo()`。它们除了填入的 `flavor` 不同，其余完全一样——都会：

1. 用 `Buffer::alloc(MIN_CAP)` 分配初始缓冲区（`MIN_CAP = 64`，见 4.3.3）。
2. 构造一个共享的 `Inner { front: 0, back: 0, buffer }`，包进 `Arc<CachePadded<...>>`。
3. 把同一份 `buffer` 也存一份到 `Worker` 的私有缓存字段 `buffer: Cell<Buffer<T>>`（注意 `Buffer` 是 `Copy` 的，存的是指针副本，不重复分配内存）。
4. 填入对应的 `flavor` 和 `_marker: PhantomData`。

构造好 `Worker` 之后，`stealer(&self)` 可以从它派生一个 `Stealer`。`Stealer` 与 `Worker` **共享同一份 `inner`**（`Arc::clone`），但只携带 `inner` 和 `flavor`，没有私有 `buffer` 缓存——这正是它能 `Send + Sync` 的原因。

#### 4.3.2 核心流程

```
new_fifo() / new_lifo():
  buffer = Buffer::alloc(64)          // 初始 64 容量
  inner  = Arc::new(Inner{front:0,back:0,buffer})
  return Worker { inner, buffer: Cell::new(buffer), flavor, _marker: PhantomData }

stealer(&self):
  return Stealer { inner: self.inner.clone(), flavor: self.flavor }   // 共享同一份 Inner
```

因为 `inner` 是 `Arc`，所以 `Worker` 和它的所有 `Stealer` 看到的是**同一份** `front`/`back`/`buffer`：`Worker` 的 `push`/`pop` 改的就是这个 `inner`，`Stealer` 的 `steal` 读的也是这个 `inner`。二者通过原子操作协调（具体在 u2-l2/u2-l3）。

#### 4.3.3 源码精读

- [src/deque.rs:225-240](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L225-L240)：`new_fifo()`，注意 `buffer` 被 `Cell::new(buffer)` 存了一份私有副本，`flavor: Flavor::Fifo`。
- [src/deque.rs:253-268](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L253-L268)：`new_lifo()`，与 `new_fifo` 几乎逐行相同，唯一区别是 `flavor: Flavor::Lifo`。
- [src/deque.rs:282-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282-L287)：`stealer(&self)`，仅 `self.inner.clone()`（`Arc` 引用计数 +1）并复制 `flavor`，没有复制 `buffer` 缓存。
- 关于初始容量常量 `MIN_CAP`：[src/deque.rs:17-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L23) 定义了 `MIN_CAP = 64`、`MAX_BATCH = 32`、`FLUSH_THRESHOLD_BYTES = 1<<10`，本讲用到 `MIN_CAP`，后两个留给 u2-l4/u2-l5。

#### 4.3.4 代码实践

1. **实践目标**：验证 `Worker` 与 `Stealer` 共享同一份底层状态，且 `Stealer` 可被 `Clone`、可在多线程间传递。
2. **操作步骤**：

   ```rust
   // 示例代码
   use crossbeam_deque::Worker;

   fn main() {
       let w = Worker::<i32>::new_fifo();
       let s1 = w.stealer();
       let s2 = s1.clone();        // Stealer 实现了 Clone
       w.push(42);
       // w 和 s1、s2 看到的是同一个队列
       println!("stealer sees len? empty? {}", s1.is_empty());
       println!("cloned stealer is_empty? {}", s2.is_empty());
   }
   ```

3. **需要观察的现象**：`push(42)` 之后，`s1.is_empty()` 和 `s2.is_empty()` 都应为 `false`，证明三者共享同一队列。
4. **预期结果**：两行均打印 `is_empty? false`。`Stealer` 能 `clone()` 说明它是共享句柄；它能在 `thread::spawn` 里使用（`Send + Sync`）则留到 u1-l4 演示。
5. **待本地验证**：`is_empty()` 的返回值依赖运行时状态，但逻辑上 `push` 后必不为空。

#### 4.3.5 小练习与答案

- **练习 1**：`new_fifo()` 和 `new_lifo()` 在源码上只差一个字段值，为什么作者不把它合并成一个带参数的 `new(flavor)`？
  - **答案**：两个命名构造器让 API 意图更清晰，调用点 `Worker::new_fifo()` 自文档化；并且文档可以分别附上 FIFO/LIFO 的用法示例，便于 `cargo doc` 阅读。这是「命名构造器（named constructor）」优于「带参构造器」的常见取舍。
- **练习 2**：`stealer()` 多次调用会创建多个独立的队列吗？
  - **答案**：不会。每次 `stealer()` 只是 `Arc::clone` 出一个**指向同一份 `inner`** 的新句柄，引用计数 +1，底层数据只有一份。多个 `Stealer` 看到的是同一个队列。

---

### 4.4 入队出队：push / pop / is_empty / len 与 FIFO/LIFO 顺序对比

#### 4.4.1 概念说明

这是本讲的核心操作模块。四个方法分工：

- `push(&self, task: T)`：把任务入队。**两种 flavor 代码完全一致**——写在 `back` 处，再 `back += 1`。满了就扩容（细节在 u2-l5）。
- `pop(&self) -> Option<T>`：把任务出队。**这里才根据 `flavor` 分叉**：FIFO 取 `front`、LIFO 取 `back-1`。
- `is_empty(&self) -> bool`：队列是否为空，依据 `back - front <= 0`。
- `len(&self) -> usize`：当前任务数，即 `(back - front).max(0)`。

注意 `push` 和 `pop` 的签名都是 `&self`（不是 `&mut self`）。这看起来反直觉——「修改队列怎么不需要可变引用？」答案是 `Worker` 内部用了原子类型（`AtomicIsize`）和 `Cell`（内部可变性），所以通过 `&self` 也能改状态。这也正是它能在「单线程拥有」的前提下被安全使用的原因（`!Sync` 保证不会有第二个线程并发改）。

本节只讲**行为和顺序**，`push`/`pop` 内部的 `fetch_add`、`compare_exchange`、`fence`、扩缩容等机制留到 u2-l2 与 u2-l5。

#### 4.4.2 核心流程

**`push`（两种 flavor 相同）**：

```
b = back; f = front
if (b - f) >= cap:  resize(2*cap)        // 满了扩容，细节见 u2-l5
slot[b] = task
back = b + 1                                  // 发布
```

**`pop`**：

```
b = back; f = front; len = b - f
if len <= 0: return None                       // 空
match flavor:
  Fifo:
    f = front.fetch_add(1)                      // 抢占 front
    if 抢占失败/已被掏空: 回退 front, return None
    task = slot[f]
    (必要时缩容)
    return Some(task)
  Lifo:
    b = back - 1; back = b                      // 先减 back
    fence(SeqCst); f = front
    if (b - f) < 0: 恢复 back, return None       // 空
    task = slot[b]
    if 这是最后一个元素(len==0):
        用 CAS 抢 front；抢不到就放弃 task
    (否则必要时缩容)
    return Some(task) 或 None
```

FIFO 与 LIFO 的出队顺序对比（`push 1, 2, 3` 后 `front=0, back=3`）：

| 操作 | FIFO Worker | LIFO Worker |
| --- | --- | --- |
| 初始（push 1,2,3 后） | `front=0, back=3`，slot=[1,2,3] | 同左 |
| 第 1 次 pop | 读 `slot[0]`=`1`，`front→1` | `back→2`，读 `slot[2]`=`3` |
| 第 2 次 pop | 读 `slot[1]`=`2`，`front→2` | `back→1`，读 `slot[1]`=`2` |
| 第 3 次 pop | 读 `slot[2]`=`3`，`front→3` | `back→0`，读 `slot[0]`=`1` |
| 第 4 次 pop | `None` | `None` |
| **出队序列** | **1, 2, 3** | **3, 2, 1** |

#### 4.4.3 源码精读

- `push`：[src/deque.rs:399-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433)。关键三步：(1) 计算 `len`，若 `len >= cap` 调 `resize(2*cap)` 扩容；(2) `buffer.write(b, MaybeUninit::new(task))` 写槽位；(3) 在 `tsan` 模式外放一道 `Release` fence，再把 `back` 加 1 发布。**整段没有 `match flavor`，两种 flavor 共用**。
- `pop` 的 FIFO 分支：[src/deque.rs:463-487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L463-L487)。用 `front.fetch_add(1, SeqCst)` 抢占式推进 `front`；若 `b - new_f < 0` 说明抢空了，回退 `front` 并返回 `None`；否则读 `slot[f]`，并在 `len` 降到容量 1/4 时缩容。
- `pop` 的 LIFO 分支：[src/deque.rs:489-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L489-L543)。先 `back -= 1` 并 store，再放 `SeqCst` fence 后读 `front`；若 `(back-1) - front < 0` 说明队列原本就空，恢复 `back` 返回 `None`；否则读 `slot[back-1]`。当只剩最后一个元素（`len == 0`）时，还要用 `front.compare_exchange` 与可能并发的 `Stealer::steal` 竞争（抢不到就 `task.take()` 放弃），这正是 `Steal::Retry` 可能出现的原因之一（u2-l3 详讲）。
- `is_empty`：[src/deque.rs:363-367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L363-L367)，`back - front <= 0`（允许瞬时的「负」读数，只要 `<=0` 就判空）。
- `len`：[src/deque.rs:382-386](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L382-L386)，`(back - front).max(0) as usize`。
- 对照真实测试：[tests/fifo.rs:12-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L12-L46) 的 `smoke` 测试验证了 FIFO 的 `pop` 顺序（如 `push 6,7,8,9` 后 `pop` 得 `6`，再 `pop` 得 `8,9`）。

#### 4.4.4 代码实践

1. **实践目标**：用一个可运行例子直观对比 FIFO 与 LIFO 的出队顺序。
2. **操作步骤**：新建一个 cargo 二进制项目并添加依赖：

   ```bash
   cargo new worker_demo --bin
   cd worker_demo
   # 在 Cargo.toml 的 [dependencies] 里加： crossbeam-deque = "0.8"
   ```

   然后把 `src/main.rs` 写成：

   ```rust
   use crossbeam_deque::Worker;

   fn main() {
       // FIFO：先进先出
       let fifo = Worker::new_fifo();
       fifo.push(1);
       fifo.push(2);
       fifo.push(3);
       println!("len after push = {}", fifo.len());
       print!("FIFO  pop 序列: ");
       loop {
           match fifo.pop() {
               Some(v) => print!("{} ", v),
               None => break,
           }
        }
       println!();

       // LIFO：后进先出
       let lifo = Worker::new_lifo();
       lifo.push(1);
       lifo.push(2);
       lifo.push(3);
       print!("LIFO  pop 序列: ");
       loop {
           match lifo.pop() {
               Some(v) => print!("{} ", v),
               None => break,
           }
       }
       println!();
   }
   ```

   运行 `cargo run`。
3. **需要观察的现象**：FIFO 行打印 `1 2 3`，LIFO 行打印 `3 2 1`；`len after push = 3`。
4. **预期结果**：

   ```text
   len after push = 3
   FIFO  pop 序列: 1 2 3
   LIFO  pop 序列: 3 2 1
   ```

   这与 [src/deque.rs:162-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L162-L196) 文档示例的方向完全一致。
5. 这是确定性单线程行为，预期结果稳定可复现。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `push` 和 `pop` 的签名是 `&self` 而不是 `&mut self`？
  - **答案**：内部状态用 `AtomicIsize`（原子）和 `Cell`（内部可变性）保存，可以通过共享引用修改，因此不需要 `&mut self`。同时 `Worker` 是 `!Sync`，编译器保证同一时刻只有一个线程持有可访问的 `&Worker`，所以这种「通过 `&self` 改状态」在本类型里是安全的。
- **练习 2**：`is_empty()` 用 `<= 0` 而不是 `== 0`，为什么？
  - **答案**：`back` 和 `front` 是两个独立的原子量，分别读取存在「读到不一致快照」的可能（例如 `back` 读到旧值、`front` 读到新值），差值可能瞬时为负。用 `<= 0` 把「0 或负」都视为空，是并发下一种保守但安全的判空方式。
- **练习 3**：如果对同一个 FIFO Worker，先 `push 1,2,3`，再 `pop()` 一次（得到 1），此时 `front` 和 `back` 各是多少？`len()` 是多少？
  - **答案**：`front=1`（从 0 推进到 1），`back=3`（不变），`len() = back - front = 2`，剩下的两次 `pop` 会得到 `2`、`3`。

---

## 5. 综合实践

把本讲四个模块串起来：用 `Worker` 完成一次「入队 → 观察 `is_empty`/`len` → 出队到空」的完整流程，并分别对 FIFO 和 LIFO 做一遍，验证出队顺序差异。同时把 4.2 学到的 `Send + !Sync` 也验证一下。

**任务**：写一个程序，对 FIFO 和 LIFO 两个 `Worker`，各 `push` 5 个值（比如 `"a","b","c","d","e"`），每 push 一个就用 `is_empty()` 和 `len()` 打印一次状态；然后连续 `pop` 直到 `None`，收集出队序列并打印；最后把 `Worker` **move** 进一个 `std::thread::spawn` 的子线程（验证 `Send`），在子线程里再 push/pop 一个值。

**参考骨架**（请自行补全并运行）：

```rust
use crossbeam_deque::Worker;

fn drain_and_print(w: &Worker<String>, label: &str) {
    print!("{} 出队序列:", label);
    while let Some(v) = w.pop() {
        print!(" {}", v);
    }
    println!();
}

fn main() {
    for (label, worker) in [
        ("FIFO", Worker::<String>::new_fifo()),
        ("LIFO", Worker::<String>::new_lifo()),
    ] {
        for c in ["a", "b", "c", "d", "e"] {
            worker.push(c.to_string());
            println!("[{}] push {} -> is_empty={}, len={}",
                     label, c, worker.is_empty(), worker.len());
        }
        drain_and_print(&worker, label);
        println!("[{}] 清空后 is_empty={}, len={}",
                 label, worker.is_empty(), worker.len());
    }

    // 验证 Worker: Send（可 move 到子线程），但 !Sync（不能共享 &）
    let w = Worker::<i32>::new_lifo();
    let handle = std::thread::spawn(move || {
        w.push(7);
        w.pop()
    });
    println!("子线程 pop = {:?}", handle.join().unwrap());
}
```

**预期观察**：

- 每次 `push` 后 `len()` 递增（1→2→…→5），`is_empty()` 一直为 `false`。
- FIFO 出队序列为 `a b c d e`，LIFO 出队序列为 `e d c b a`。
- 清空后 `is_empty() == true`、`len() == 0`。
- 子线程 `pop` 得到 `Some(7)`（把 `w` move 进线程合法，证明 `Send`）。
- 若你尝试把子线程闭包改成捕获 `&w`（而不是 move `w`），则会**编译失败**，证明 `!Sync`——具体错误信息待本地验证。

## 6. 本讲小结

- `crossbeam-deque` 的本地队列分两种 flavor：`Flavor::Fifo` 和 `Flavor::Lifo`，由私有枚举 [src/deque.rs:147-155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L147-L155) 表达。
- **`push` 两种 flavor 完全相同**：写在 `back` 处并 `back += 1`；**区别只在 `pop`**：FIFO 取 `front`（先进先出），LIFO 取 `back-1`（后进先出）。
- `Worker<T>` 有四个字段；`_marker: PhantomData<*mut ()>` 是把默认 trait 状态拉到 `!Send + !Sync` 的哨兵，作者再用 `unsafe impl Send`（[src/deque.rs:211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L211)）只开 `Send`，最终 `Worker` 是 **`Send + !Sync`**——可 move、不可共享 `&`，从而「只能被单个线程拥有」。
- `new_fifo()` / `new_lifo()` 创建队列（初始容量 `MIN_CAP=64`），`stealer()` 通过 `Arc::clone` 派生一个共享同一份 `inner` 的 `Stealer`（`Send + Sync`，可跨线程共享）。
- `push`/`pop`/`is_empty`/`len` 都用 `&self`（靠原子量与 `Cell` 提供内部可变性）；`push 1,2,3` 后 FIFO 出队为 `1,2,3`、LIFO 为 `3,2,1`。
- 本讲止步于「会用 + 能读懂 API 与字段」；CAS、内存序、扩缩容、epoch 回收等内部机制留给 u2 与 u4。

## 7. 下一步学习建议

- **紧接本讲的下一讲 u1-l4**：`Stealer`、`Injector` 与 `Steal` 结果工作流。本讲只用了 `Worker` 自己 `pop`，下一步将引入「别的线程来偷任务」和「全局注入队列」，并把 `pop` 放进 `find_task` 回退链里。
- **进阶层 u2-l1**：`Buffer` 与 `Inner` 数据结构。本讲反复提到的 `front`/`back`/`buffer` 到底长什么样、`Buffer::at` 如何用 `index & (cap-1)` 实现 O(1) 环形索引，在那里展开。
- **进阶层 u2-l2**：`push` 与 `pop` 的**实现**深读。本讲刻意跳过的 `fetch_add`、`compare_exchange`、`SeqCst fence`、最后一个元素的 CAS 竞争，都在这一讲逐行剖析。
- **专家层 u4-l1**：内存序与 volatile hack。理解 `push` 里那道 `Release fence` 与 `pop`/`steal` 里 `Acquire` 加载、`SeqCst fence` 如何建立跨线程 happens-before 关系。

建议在进入 u2 之前，先把本讲的「游标心智模型」和综合实践例子跑通——它们是理解后续无锁算法的地基。
