# Direct 包装器：Prod / Cons / Obs 与即时同步

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `Direct<R, P, C>` 这三个泛型参数（一个「钥匙」类型 `R`、两个 `const` 布尔 `P`/`C`）如何用编译期常量编码「写权 / 读权」。
- 解释 `Direct::new` 如何借助 hold 标志在运行时强制「至多一个生产者、至多一个消费者」的 SPSC 不变量，并因此让重复拆分直接 panic。
- 说清 `Obs` / `Prod` / `Cons` 三个别名各自对应哪种权限组合、各自只覆盖哪些核心 trait。
- 理解 Direct 是「即时同步」策略：它不缓存任何索引，每次观测、每次写入都直连底层缓冲区的索引；并由此理解为什么单线程 `LocalRb.split` 默认就用 Direct。
- 把 Direct 与 Frozen（延迟同步）、Caching（按需同步）放在一起对比，建立「三种同步策略」的整体认识。

## 2. 前置知识

在进入本讲前，请先确认你已经掌握下面这些前置概念（它们在前几讲已建立，这里只做一句话回顾）：

- **拆分（split）**：把一个环形缓冲区对象转成 `Producer`（写端）与 `Consumer`（读端）两个句柄，二者共享同一块存储与 `read`/`write` 索引（见 u2-l4）。
- **钥匙（RbRef）**：指向「拥有缓冲区的对象」的智能指针抽象，如 `&B`、`Rc<B>`、`Arc<B>`；它把「怎么拿到那个缓冲区」这件事统一掉（见 u4-l1）。
- **Wrap**：所有包装器的统一接口，要求能交出自己持有的「钥匙」——`rb_ref()`（借）、`into_rb_ref()`（按值交还），并据此派生 `rb()`（直接取底层缓冲区引用）（见 u4-l1）。
- **hold 标志**：缓冲区上的两个布尔标志（read_held / write_held），用来在运行时记录「现在是否已经有人拿到了写端 / 读端」，是 SPSC 不变量的物理载体（见 u2-l4、u3-l4）。
- **Observer / Producer / Consumer**：只读观测 / 写 / 读三组核心 trait；其中 `Producer` 只强制实现一个 unsafe 方法 `set_write_index`，`Consumer` 只强制实现 `set_read_index`，其余 `try_push` / `try_pop` 等都是基于它们的默认实现（见 u3-l1、u3-l2、u3-l3）。

一句话定位本讲：**Direct 是「不缓存、直连底层索引」的包装器，是三种同步策略里最简单、最即时的一种，也是 `LocalRb.split` 的默认产物。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/wrap/direct.rs` | Direct 包装器的全部实现：结构定义、`Obs`/`Prod`/`Cons` 别名、`new`/`close`/`observe`/`freeze`、`Wrap`/`Observer`/`Producer`/`Consumer` 实现、`Drop`。本讲的主战场。 |
| `src/wrap/frozen.rs` | Frozen 包装器。本讲只用来做对比——它额外持有两个本地 `Cell` 索引，是「延迟同步」策略。 |
| `src/wrap/caching.rs` | Caching 包装器。本讲只用来做对比——它在 Frozen 之上实现「按需同步」，是 `SharedRb.split` 的默认产物。 |
| `src/wrap/traits.rs` | `Wrap` trait 定义。Direct 实现了它。 |
| `src/rb/traits.rs` | `RbRef`（钥匙）trait 定义及为 `&B`/`Rc<B>`/`Arc<B>` 的实现。 |
| `src/rb/local.rs` | `LocalRb` 实现。它的 `Split` / `SplitRef` 把缓冲区拆成 `Prod<Rc<Self>>` / `Cons<Rc<Self>>`（或引用版本），直接证明了「LocalRb 默认产出 Direct」。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看 Direct 的结构与权限编码，再看 new/close 如何用 hold 标志锁住 SPSC 不变量，最后看「即时同步」到底体现在哪里。

### 4.1 Direct 的结构与 const generic 权限编码

#### 4.1.1 概念说明

一个环形缓冲区被拆分后，需要两个「句柄」分别代表写端和读端。但写端、读端、以及「只想看一眼状态」的纯观测端，三者在「能做什么」上差别很大：

- 写端：能推进 `write` 索引（写权）。
- 读端：能推进 `read` 索引（读权）。
- 纯观测端：什么都推不了，只能查 `capacity` / 索引 / 占用数。

ringbuf 没有为这三种角色各写一个独立结构体，而是**用两个编译期布尔常量 `P`（producer 写权）和 `C`（consumer 读权）在一个结构体内编码出全部角色**。这就是 const generic（常量泛型）的典型用法：把「权限」变成类型的一部分，让编译器在类型层面区分它们——`Prod` 和 `Cons` 是**不同的类型**，不能互相赋值，也不能用错方法。

#### 4.1.2 权限真值表

| `P`（写权） | `C`（读权） | 类型别名 | new 时是否声明 hold | 能做什么 |
|:-:|:-:|---|---|---|
| `false` | `false` | `Obs<R>` | 否 | 只读观测 |
| `true` | `false` | `Prod<R>` | `hold_write(true)` | 写入（实现 `Producer`） |
| `false` | `true` | `Cons<R>` | `hold_read(true)` | 读取（实现 `Consumer`） |
| `true` | `true` | （无别名，类型上允许） | 两个都声明 | 理论组合，`split` 不会用到 |

注意第四行：const generic 允许 `P=C=true` 这种「同时拥有读写权」的类型存在，它对应「未拆分、独占整块缓冲区」的语义（`RingBuffer` 用得到）；但日常 `split` 只会产出前三行。

#### 4.1.3 源码精读

结构定义极其精简，只持有一把「钥匙」`R`：

[文件路径:L21-L23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L21-L23) —— `Direct` 内部只有一个字段 `rb: R`，即一把指向拥有缓冲区对象的钥匙。三个 const generic 参数中，`R: RbRef` 是钥匙类型（`&B`/`Rc<B>`/`Arc<B>`），`P`/`C` 是两个 `bool` 编码读写权限。

三个别名把常用权限组合固定下来：

[文件路径:L25-L30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L25-L30) —— `Obs = Direct<R,false,false>`、`Prod = Direct<R,true,false>`、`Cons = Direct<R,false,true>`。它们都是同一个 `Direct` 结构体的不同「权限实例」，零运行时开销。

`Obs` 还额外实现了 `Clone`：

[文件路径:L32-L36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L32-L36) —— 因为 `Obs` 既不持有写权也不持有读权，它对 SPSC 不变量毫无影响，所以可以自由克隆（克隆只是多一把指向同一缓冲区的钥匙）。这也是为什么任意 Direct 包装器都能用 `observe()` 派生出一个只读 `Obs`。

> 小结：Direct 的「权限」是用类型参数 `P`/`C` 在编译期表达，而非运行时字段；`Prod`、`Cons`、`Obs` 是这同一个结构体的别名，区别纯粹在类型层面。

### 4.2 new / close 与 hold 标志：强制 SPSC 不变量

#### 4.2.1 概念说明

ringbuf 是 SPSC（单生产者单消费者）数据结构，其无锁正确性**依赖一个前提**：同一时刻至多一个写端、至多一个读端。如果两个线程同时拿到写端并对 `write` 索引做读-改-写，无锁算法就会崩。

这个前提不能只靠文档约定，ringbuf 选择**在运行时用 hold 标志强制它**：

- 缓冲区上维护两个布尔：`write_held`（是否已有写端）、`read_held`（是否已有读端）。
- 创建写端时，把 `write_held` 置为 `true`；如果它**之前已经是 `true`**，说明已经有人占着写端，立即 panic。
- 写端销毁（`Drop`）时，把 `write_held` 复位为 `false`，把写权「还回去」。

这样，「第二次拆分」就会被这个断言挡下，从机制上杜绝了「两个写端」的可能。

关键要理解 `hold_write`/`hold_read` 的**返回值语义**：它返回的是**旧值**（设置新值之前的值）。所以 `hold_write(true)` 的意思是「把 write_held 设为 true，并告诉我它之前是不是已经被占着」。

#### 4.2.2 核心流程

创建一个 `Prod`（`new`）时的逻辑，用伪代码描述：

```
fn new(rb):                       // 要拿写权
    if P:                          // 这是写端
        old = rb.hold_write(true)  // 占坑，取回旧值
        assert(!old)               // 旧值必须为 false，否则 panic
    if C:                          // 这是读端
        old = rb.hold_read(true)
        assert(!old)
    return Direct { rb }
```

销毁一个 `Prod`（`close`，被 `Drop` 与 `into_rb_ref` 调用）时：

```
fn close():
    if P: rb.hold_write(false)     // 还坑，忽略返回值
    if C: rb.hold_read(false)
```

`assert!(!old)` 这一行就是 SPSC 的「安检门」：旧值 `false` ⇒ 之前没人占 ⇒ 放行并占坑；旧值 `true` ⇒ 已经有人占 ⇒ panic。

#### 4.2.3 源码精读

[文件路径:L42-L50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L42-L50) —— `Direct::new`。`P` 为真时 `assert!(!hold_write(true))`：`hold_write(true)` 把 write_held 设为 true 并返回旧值，`!旧值` 为真才能通过断言——即「必须此前未被占用」。`C` 同理处理读端。文档注释明确写出：「Panics if wrapper with matching rights already exists」。

[文件路径:L63-L73](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L63-L73) —— `close`，把对应 hold 标志复位为 `false`，归还权限。返回值被丢弃。它是 `unsafe` 的，因为直接操作 hold 标志绕过了不变量自检（库内部使用）。

`close` 的两处调用点：

[文件路径:L148-L152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L148-L152) —— `Drop` 实现里调用 `close()`，所以包装器一旦离开作用域，hold 标志就自动复位，权限被归还。

[文件路径:L81-L87](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L81-L87) —— `into_rb_ref`（按值交还钥匙）也先 `close()` 再用 `ManuallyDrop` + `ptr::read` 把钥匙搬出去（必须手动 close，因为绕过了 Drop）。这正是 u4-l1 讲过的「销毁收尾」机制。

底层 `hold_write`/`hold_read` 的实现（以 `LocalRb` 为例）：

[文件路径:L121-L130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L121-L130) —— `LocalRb` 用 `Cell<bool>` 存 hold 标志，`hold_read`/`hold_write` 都是 `Cell::replace(flag)`——替换为新值并返回旧值，正好对应上面描述的语义。

> 由此可以回答实践任务里的一个问题：**为什么对同一个缓冲区第二次 split 会 panic？** 因为第一次 split 创建 `Prod` 时 `hold_write(true)` 把 write_held 置为 true；只要那个 `Prod` 还活着（没被 drop），write_held 就一直是 true。第二次 split 再去 `Prod::new`，`hold_write(true)` 这次返回的旧值是 true，`assert!(!true)` 失败 → panic。

### 4.3 即时同步：Observer / Producer / Consumer 的直接转发

#### 4.3.1 概念说明

讲完「权限安检」，本模块讲 Direct 在「同步策略」上的定位。三种包装器（Direct / Frozen / Caching）的差别，本质上就是**「读索引时，是直接读底层、还是读本地缓存」**：

- **Direct（即时同步）**：完全不缓存。每次 `read_index()` / `write_index()` / `is_empty()` 等观测，都直接读底层缓冲区的索引；每次 `try_push` / `try_pop` 也直接把新索引写回底层。
- **Frozen（延迟同步）**：本地缓存一份索引副本，只在显式调用 `commit`/`fetch`/`sync` 或 `Drop` 时才与底层交换。详见 u4-l3。
- **Caching（按需同步）**：在 Frozen 之上做「懒同步」——只有在 `is_full`/`is_empty` 这种必须看对端进度时才 fetch，每次成功操作后立即 commit。详见 u4-l4。

Direct 的「即时」意味着：**写端一动索引，读端下一次观测立刻能看到**，中间没有任何缓冲、没有需要手动触发的事件。

这一点在单线程 `LocalRb` 上几乎免费（底层索引就是 `Cell`，读写无成本），所以 `LocalRb.split` 默认就产出 Direct。而在多线程 `SharedRb` 上，「每次都去读共享的原子」会引发跨核缓存行失效（cache-line bouncing），代价较高，所以 `SharedRb.split` 默认改用 Caching 来摊薄这部分开销。这就是「索引存储的代价驱动包装器选择」。

#### 4.3.2 核心流程：Direct 如何做到即时

Direct 的 `Observer` 实现里，**每一个方法都只是把调用原样转发给 `self.rb()`**（底层缓冲区）：

```
impl Observer for Direct {
    fn read_index()  -> self.rb().read_index()   // 直读底层
    fn write_index() -> self.rb().write_index()  // 直读底层
    fn is_empty()/is_full()/occupied_len()/vacant_len()  // 默认实现，基于上面两个
    ...
}
```

`Producer` 只实现一个 unsafe 的 `set_write_index`（同样直接转发），其余 `try_push` / `push_slice` / `push_iter` / `vacant_slices` 等全部继承自 trait 的**默认实现**——而那些默认实现最终都落到「读 write_index/read_index + 写 set_write_index」这两个原语上。`Consumer` 同理，只实现 `set_read_index`。

于是「即时」是自然结果：高层方法 = 在原语上反复读写底层索引，Direct 对原语做了直连转发，所以**每一步都即时**。

#### 4.3.3 源码精读

先看 `Observer` 的直接转发：

[文件路径:L101-L132](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L101-L132) —— `Observer for Direct`。`read_index()`/`write_index()` 直接调 `self.rb().read_index()`/`write_index()`，没有任何本地缓存；`unsafe_slices`/`unsafe_slices_mut` 也直接转发。注意 `is_empty`/`is_full`/`occupied_len`/`vacant_len` 没有出现在这里——它们是 `Observer` 的默认方法，每次都基于上面这两个 `*_index()` 重新计算，因此**永远反映底层最新状态**。

再看写端、读端各自只覆盖的那一个原语：

[文件路径:L134-L139](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L134-L139) —— `Producer for Prod<R>` 只实现 `set_write_index`，直接转发到 `self.rb().set_write_index(value)`。`try_push`、`push_slice`、`push_iter`、`vacant_slices_mut`、`advance_write_index` 等全部来自 `Producer` trait 的默认实现（如 `producer.rs` 中 `try_push` 的默认实现会在 `is_full()` 为假时 `write` 一个元素并 `advance_write_index(1)`）。

[文件路径:L141-L146](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L141-L146) —— `Consumer for Cons<R>` 只实现 `set_read_index`，同样直接转发。

为方便对比，下面是 `Producer::try_push` 的默认实现，能看到它完全建立在「观测 + set_write_index」之上：

[文件路径:L60-L70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70) —— 默认 `try_push`：先 `is_full()`（读两个索引），不满则写入第一个空闲槽并 `advance_write_index(1)`（内部调 `set_write_index`）。Direct 把这几个原语全部直连底层，所以一次 `try_push` 的全部副作用都即时落到底层索引上。

最后看 `LocalRb` 是如何默认产出 Direct 的——这是「LocalRb.split 默认产出这种直接包装」的直接证据：

[文件路径:L138-L146](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L138-L146) —— `Split for LocalRb` 的关联类型 `Prod = Prod<Rc<Self>>`、`Cons = Cons<Rc<Self>>`，正是 Direct 别名（钥匙用 `Rc`）。`split()` 把自己塞进 `Rc` 再委托给 `Rc<LocalRb>::split`。

[文件路径:L147-L155](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L147-L155) —— `Split for Rc<LocalRb>` 的 `split()` 体：`(Prod::new(self.clone()), Cons::new(self))`。两个 `Direct::new` 在这里触发前面讲过的 hold 标志断言。

对照 `SharedRb`，它的 split 产出的是 Caching：

[文件路径:L155-L162](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L155-L162) —— `Split for SharedRb` 的关联类型是 `CachingProd<Arc<Self>>` / `CachingCons<Arc<Self>>`，不是 Direct。这一对比正好说明：包装器类型由缓冲区类型决定，LocalRb→Direct，SharedRb→Caching。

> 小结：Direct 的「即时同步」来自它对全部索引原语做「零缓存直连转发」，并且因为高层方法都建立在这些原语上，所以整个数据面都是即时的。LocalRb（单线程、底层是 `Cell`）选择 Direct，SharedRb（多线程、底层是 `Atomic`）选择 Caching，是「存储代价驱动包装器」的体现。

### 4.4 代码实践

#### 实践一：读源码，解释「第二次 split 为何 panic」

1. **实践目标**：把 4.2 节的 hold 标志逻辑在源码里走一遍，并亲手复现 panic。
2. **操作步骤**：
   - 打开 [src/wrap/direct.rs:L42-L50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L42-L50)，确认 `Prod::new` 里 `assert!(!hold_write(true))` 的语义。
   - 打开 [src/rb/local.rs:L121-L130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L121-L130)，确认 `hold_write` 是 `Cell::replace`（返回旧值）。
   - 在 `examples/` 下新建一个临时程序（示例代码，**不要提交到仓库**），用 `Rc<LocalRb>` 复现第二次拆分：

     ```rust
     // 示例代码：复现 SPSC 重复拆分的 panic
     use std::rc::Rc;
     use ringbuf::{traits::*, LocalRb, storage::Heap};

     fn main() {
         let rb = Rc::new(LocalRb::<Heap<i32>>::new(8));
         let (_prod, _cons) = rb.clone().split();   // 第一次：hold_write(true)→旧值 false，通过
         let (_p2, _c2) = rb.clone().split();       // 第二次：hold_write(true)→旧值 true → panic
     }
     ```
   - 用 `cargo run --example <你的文件名>` 运行（默认带 `std`/`alloc` feature，`Rc` 与 `Heap` 可用）。
3. **需要观察的现象**：第二次 `split` 时程序 panic，错误信息指向 `assert` 失败（在 `Direct::new` 内）。
4. **预期结果**：你应当能用一句话解释——「第一次 `Prod::new` 把 write_held 置为 true 且 Prod 未 drop，第二次 `hold_write(true)` 返回的旧值是 true，`assert!(!true)` 失败」。若本地环境无法编译（如 feature 缺失），明确记为「待本地验证」。

#### 实践二：用 LocalRb 验证「即时同步」

1. **实践目标**：用 `LocalRb.split` 得到的 `Prod`/`Cons`（即 Direct）证明「一次 try_push 后，对端立即可见」。
2. **操作步骤**：写如下程序（示例代码）：

   ```rust
   // 示例代码：验证 Direct 的即时同步
   use ringbuf::{traits::*, LocalRb, storage::Heap};

   fn main() {
       let (mut prod, mut cons) = LocalRb::<Heap<i32>>::new(8).split();

       assert!(cons.is_empty());            // 初始空
       assert_eq!(cons.occupied_len(), 0);

       prod.try_push(42).unwrap();          // 写端推进 write 索引（直连底层 Cell）

       // 关键：没有任何 commit/fetch/sync 调用，读端立刻看到
       assert_eq!(cons.occupied_len(), 1);  // 即时可见
       assert_eq!(cons.try_pop(), Some(42));
       assert!(cons.is_empty());
   }
   ```
3. **需要观察的现象**：`prod.try_push(42)` 之后，`cons.occupied_len()` 立刻变为 1，无需任何手动同步。
4. **预期结果**：全部断言通过。把它与 4.3 节的源码对照——`cons.occupied_len()` 默认实现每次都读底层 `write_index()`，而 Direct 直连底层，所以「即时」是必然的。

### 4.5 小练习与答案

**练习 1**：如果把 `Direct::new` 里的 `assert!(!hold_write(true))` 改成不检查、直接设值，会破坏什么不变量？为什么？

> **答案**：会破坏 SPSC 不变量——可能同时存在两个写端。两个写端并发推进 `write` 索引时，会出现「读-改-写」竞态（各自基于旧索引计算新值再写回），导致元素相互覆盖或索引错乱，无锁算法的正确性前提（至多一个写端）失效。

**练习 2**：`Obs` 为什么可以自由 `Clone`，而 `Prod` / `Cons` 不行？

> **答案**：`Obs` 的 `P=C=false`，`new` 时既不设 `hold_write` 也不设 `hold_read`，它对 SPSC 不变量毫无影响（只是多一把只读钥匙），所以克隆安全（见 [direct.rs:L32-L36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L32-L36)）。而 `Prod`/`Cons` 持有独占的 hold 标志，克隆出第二个就等于出现两个写端/读端，直接违反 SPSC，因此库不给它们实现 `Clone`。

**练习 3**：`Producer for Prod` 只实现了一个方法 `set_write_index`，为什么 `prod.try_push(...)` 仍能工作？这套方法链是怎样的？

> **答案**：`Producer` trait 把 `set_write_index` 定为唯一需要实现的 unsafe 原语，`try_push`/`push_slice`/`push_iter`/`advance_write_index` 等都是基于它的默认实现（见 [producer.rs:L60-L70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)）。`Prod` 把 `set_write_index` 直连转发到底层 `LocalRb::set_write_index`（即 `Cell::set`），于是 `try_push` → `is_full()`（读索引）→ `write` → `advance_write_index(1)` → `set_write_index` 全部即时落到底层。这套「实现原语、继承默认方法」的设计让 Direct 用极少的代码就获得了完整的写端能力。

## 5. 综合实践

把本讲的三个要点（权限编码、hold 标志、即时同步）串起来，完成下面这个小任务：

> **任务**：用一个 `LocalRb` 走完「创建 → 拆分 → 即时写读 → 派生观测端 → 销毁后重新拆分」全流程，并在每一步用源码解释发生了什么。

建议步骤（示例代码）：

```rust
use ringbuf::{traits::*, LocalRb, storage::Heap};

fn main() {
    // 1. 创建并拆分：LocalRb.split 默认产出 Direct (Prod<Rc<_>>, Cons<Rc<_>>)
    let (mut prod, mut cons) = LocalRb::<Heap<i32>>::new(4).split();

    // 2. 即时同步：写入后对端立刻可见（无需 commit/fetch）
    prod.try_push(1).unwrap();
    prod.try_push(2).unwrap();
    assert_eq!(cons.occupied_len(), 2);

    // 3. 派生只读观测端：observe() 返回 Obs，可自由克隆、不影响 SPSC
    let obs = prod.observe();
    assert_eq!(obs.write_index(), cons.read_index() + 2); // 写索引比读索引多 2

    // 4. 用完先取回数据
    assert_eq!(cons.try_pop(), Some(1));
    assert_eq!(cons.try_pop(), Some(2));

    // 5. prod / cons / obs 离开作用域 → Drop → close() 把 hold 标志复位
    //    此后该缓冲区（若仍可达）可被重新拆分
}
```

完成后再回答：第 5 步「hold 标志复位」发生在源码的哪一行？如果跳过 `Drop`（例如用 `mem::forget(prod)`）会埋下什么隐患？

> **参考**：复位发生在 [direct.rs:L148-L152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L148-L152) 的 `Drop` → `close()`。若用 `mem::forget` 跳过 Drop，write_held 会一直停留在 true，该缓冲区将永远无法再次拆分出写端（`Prod::new` 必 panic）；同时底层 `LocalRb` 自身的 Drop 也不会触发，未取走的元素不会被正确析构——这是 `unsafe`/`forget` 路径上需要警惕的责任。

## 6. 本讲小结

- `Direct<R, P, C>` 用一把钥匙 `R` 加两个 const generic 布尔 `P`/`C` 编码读写权限，`Obs`/`Prod`/`Cons` 是它的三个常用别名（`false,false` / `true,false` / `false,true`），区别纯在类型层面、零运行时开销。
- `Direct::new` 在 `P`/`C` 为真时用 `assert!(!hold_write(true))` / `assert!(!hold_read(true))` 强制 SPSC 不变量；hold 标志「返回旧值、设置新值」的语义是这套断言的关键，重复拆分因此直接 panic。
- `close`（被 `Drop` 与 `into_rb_ref` 调用）把 hold 标志复位，归还权限；`into_rb_ref` 因用 `ManuallyDrop` 绕过 Drop，必须显式先 `close`。
- Direct 是「即时同步」策略：`Observer` 的每个方法都直连转发底层索引，`Producer`/`Consumer` 只各自实现一个 `set_*_index` 原语，其余方法继承默认实现——故整个数据面即时反映底层状态。
- `LocalRb.split` 默认产出 `Prod`/`Cons`（Direct，钥匙为 `Rc`），而 `SharedRb.split` 默认产出 `CachingProd`/`CachingCons`，体现了「索引存储代价驱动包装器选择」。
- 与 Frozen（延迟同步，需手动 `commit`/`fetch`/`sync`）、Caching（按需同步）相比，Direct 是三种同步策略里最简单、最即时的基线。

## 7. 下一步学习建议

- 下一讲 **u4-l3 Frozen 包装器** 将展示「延迟同步」的另一极：本地缓存索引、显式 `commit`/`fetch`/`sync`、以及 `FrozenProd::discard`。建议带着「Direct 是零缓存、Frozen 是全缓存」的对比去读 `src/wrap/frozen.rs`。
- 紧接着 **u4-l4 Caching 包装器** 讲解「按需同步」如何在 Frozen 之上自动化，理解它为什么成为 `SharedRb` 的默认包装器。
- 若想深入 hold 标志在多线程下的原子实现与无锁正确性，可先读 **u5-l2 Hold flags** 与 **u5-l1 无锁并发**，再回头看本讲的断言在 `SharedRb`（`AtomicBool`）下如何成立。
- 想理解 `Prod`/`Cons` 如何额外获得 `std::io::Write`/`Read`，可阅读 `direct.rs` 末尾的 `impl_producer_traits!` / `impl_consumer_traits!` 宏调用（详见 u8-l3 宏系统）。
