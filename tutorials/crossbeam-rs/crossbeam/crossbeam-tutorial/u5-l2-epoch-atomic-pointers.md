# Atomic/Shared/Owned 原子指针与标签

## 1. 本讲目标

本讲精读 `crossbeam-epoch/src/atomic.rs`，讲清楚无锁数据结构赖以运转的「原子指针」三件套：

1. **`Atomic<T>`**——可在线程间共享的「盒子里的指针」，它是一个机器字（`AtomicPtr<()>`），既是数据结构的并发写点，又是「把元数据塞进地址低位」的载体。
2. **`Owned<T>` 与 `Shared<'g, T>`**——同一根指针的两种「所有权姿态」：前者独占（像 `Box`），后者借用（受 epoch 保护）。
3. **`load / store / swap / compare_exchange` 等操作族**——它们如何在「比较与更新」时同时比较**指针与标签**。

学完后你应当能够：

- 算出任一类型 `T` 能在指针里塞下多少位标签、合法标签取值范围；
- 说清 `Owned` 和 `Shared` 的所有权差异、它们如何经 `Pointer` trait 统一喂给 `Atomic`；
- 写出一段用 `compare_exchange` 原子地「换指针 + 改标签」的代码；
- 解释 `Shared<'g, T>` 的生命周期 `'g` 为什么必须绑定 `Guard`，这是 epoch 内存回收安全性的类型层保障。

## 2. 前置知识

本讲承接 [u5-l1](u5-l1-epoch-overview.md)，默认你已建立 epoch 回收的三段式直觉——**标记（摘除对象并打上当前 epoch）→ 延迟（等推进）→ 销毁**，且知道 `pin()` 返回一个 RAII 凭证 `Guard`。下面补充三块本讲要用到的基础。

### 2.1 为什么无锁结构需要「原子指针」而不是「原子值」

标准库的原子类型（`AtomicUsize`、`AtomicBool`…）只能搬动**定长定宽**的标量。而无锁链表/栈/跳表里，节点是在堆上动态分配的、大小不定的对象，线程之间能「用一个字」交流的，只有**指向该对象的指针**。因此并发数据结构真正需要的是「指向堆对象、可被多个线程同时读写」的原子指针——这正是 `Atomic<T>`。

### 2.2 对齐与「地址里的空闲位」

Rust 中每个类型 `T` 都有一个对齐 `align_of::<T>()`，它是 2 的幂。这意味着：**任何指向 `T` 的合法指针，其地址的最低若干位必然是 0**。例如 `align_of::<i32>() == 4`，而 \(4 = 2^2\)，所以指向 `i32` 的指针最低 2 位恒为 0。这 2 位是「白送的存储空间」，可以拿来塞一个 0~3 的小整数，这就是**标签（tag）**。它常用来标记节点状态（如「逻辑删除」「被冻结」），让一次原子操作同时更新指针与状态。

> 关键：标签不额外占内存，与指针共享一个机器字，因此能用**单条 CAS 指令**同时改指针和标签——这正是无锁算法梦寐以求的「原子地改两件事」。

### 2.3 指针来源（provenance）与严格来源

在现代 Rust 内存模型里，一个指针不只是「一个地址整数」，它还携带「来源」信息（provenance）。直接 `ptr as usize` 再 `as *mut T` 来回转，在严格来源模型下可能丢失来源而被判定 UB。因此本讲会看到 crossbeam 用 `map_addr` / `wrapping_add` 这类**保留来源**的手法改写地址低位（详见 4.1.3）。

## 3. 本讲源码地图

本讲只读一个核心文件，外加一个用于理解 `'g` 生命周期的辅助文件：

| 文件 | 作用 |
| --- | --- |
| [atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | `Atomic`/`Owned`/`Shared`/`Pointer`/`Pointable` 的全部定义与操作；标签打包的私有辅助函数也在其中。 |
| [guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard`（pin 凭证）与 `unprotected()`。本讲只用它来解释 `Shared<'g,T>` 的 `'g` 从何而来。 |

`atomic.rs` 在 `lib.rs` 中被整体重导出（见 [lib.rs:170-177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L177)），对外公开 `Atomic`、`Owned`、`Shared`、`Pointer`、`Pointable`、`CompareExchangeError`、`CompareExchangeValue`。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**① `Atomic<T>` 与低地址位标签打包**；**② `Owned`/`Shared`/`Pointer` 的所有权语义与转换**；**③ `load/store/swap/compare_exchange` 操作族**。

### 4.1 `Atomic<T>` 原子指针与低地址位标签打包

#### 4.1.1 概念说明

`Atomic<T>` 是一个**可在线程间共享的原子指针**，指向堆上的一个 `T`（更准确说是 `Pointable` 对象）。它的全部状态就是 `AtomicPtr<()>`——一个机器字：

```rust
pub struct Atomic<T: ?Sized + Pointable> {
    data: AtomicPtr<()>,
    _marker: PhantomData<*mut T>,
}
```

见 [atomic.rs:274-277](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L274-L277)。两个字段：

- `data: AtomicPtr<()>` 是真正的原子字，裸类型擦除成 `()`，方便统一处理；
- `_marker: PhantomData<*mut T>` 不占内存，只负责在类型系统里「记住」指向的是 `T`，从而 `Deref`、`Debug` 等能正确还原类型。注意是 `*mut T` 而非 `&T`，所以 `Atomic` 本身**不绑定任何生命周期**，可以放进 `static` 或长寿命的数据结构里。

`Atomic` 的 `Send`/`Sync` 要求 `T: Send + Sync`（[atomic.rs:279-280](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L279-L280)）：它只负责安全地「搬指针」，指针指向的对象能否跨线程共享仍由 `T` 决定。

#### 4.1.2 核心流程：标签如何打包进地址

标签机制依赖一个前提：**对齐指针的低位为 0**。给定 `T::ALIGN = 2^k`（对齐恒为 2 的幂），则 `k = ALIGN.trailing_zeros()`，最低 `k` 位可作标签位。可用标签掩码与合法范围为：

\[
\text{low\_bits} = 2^{k} - 1, \qquad \text{tag} \in [\,0,\; 2^{k}-1\,]
\]

「打包」就是把指针地址的低位清零、再或上 `tag`；「拆包」则相反。三个私有函数各司其职：

- `low_bits<T>()` 算掩码；
- `compose_tag(ptr, tag)` 写入标签；
- `decompose_tag(ptr)` 拆出 `(裸指针, 标签)`。

以常见类型为例：

| 类型 `T` | 对齐 | \(k\) | 可用标签位 | 合法 tag |
| --- | --- | --- | --- | --- |
| `i8` / `u8` | 1 | 0 | 0 位 | 只能 0 |
| `i32` / `u32` | 4 | 2 | 2 位 | 0..=3 |
| `i64` / `u64` | 8 | 3 | 3 位 | 0..=7 |

这也解释了源码里两条测试的取值（详见 4.1.5）：`i8` 只能 `with_tag(0)`，`i64` 可以 `with_tag(7)`。

#### 4.1.3 源码精读

**掩码计算**——`low_bits` 直接由对齐推出来：

```rust
fn low_bits<T: ?Sized + Pointable>() -> usize {
    (1 << T::ALIGN.trailing_zeros()) - 1
}
```
见 [atomic.rs:60-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L60-L62)。`T::ALIGN.trailing_zeros()` 就是上文中的 \(k\)，`1 << k` 再减 1 得到 `k` 个连续 1 的掩码。

> 对齐从哪来？由 `Pointable` trait 的 `const ALIGN: usize` 提供（[atomic.rs:117-119](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L117-L119)）。对 sized `T`，它就是 `mem::align_of::<T>()`（[atomic.rs:161-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L161-L162)）。`Pointable` 还把「盒子里的对象」一般化：除了 `T`，它也为 `[MaybeUninit<T>]` 这类「带长度的数组」实现（[atomic.rs:219-263](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L219-L263)），让 `Atomic` 能指向动态大小分配。

**写入与拆出标签**——`compose_tag` 清低位再或上 tag，`decompose_tag` 反向拆分：

```rust
fn compose_tag<T: ?Sized + Pointable>(ptr: *mut (), tag: usize) -> *mut () {
    map_addr(ptr, |a| (a & !low_bits::<T>()) | (tag & low_bits::<T>()))
}

fn decompose_tag<T: ?Sized + Pointable>(ptr: *mut ()) -> (*mut (), usize) {
    (
        map_addr(ptr, |a| a & !low_bits::<T>()),
        ptr as usize & low_bits::<T>(),
    )
}
```
见 [atomic.rs:73-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L73-L85)。注意 `tag & low_bits::<T>()`：传入的 tag 会被**截断**到合法位宽，超出的高位直接丢弃（文档明确说「tag is truncated to fit」）。

**保留来源的地址改写**——`map_addr` 没有用 `as usize`/`as *mut T` 直接来回转，而是用 `wrapping_add`/`wrapping_sub` 在指针上做地址运算：

```rust
fn map_addr<T>(ptr: *mut T, f: impl FnOnce(usize) -> usize) -> *mut T {
    let new_addr = f(ptr as usize);
    ptr.cast::<u8>()
        .wrapping_add(new_addr.wrapping_sub(ptr as usize))
        .cast::<T>()
}
```
见 [atomic.rs:89-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L89-L95)。源码注释点明：这就是标准库未来的 `pointer::map_addr`，只是受 MSRV 限制暂时手写。`wrapping_add` 以原指针为基底偏移，**保留了指针来源**，在严格来源模型（miri）下也合法。

**校验对齐**——`ensure_aligned` 在「裸指针转 `Owned`/`Shared`」等入口断言指针低位确为 0，防止外部塞入未对齐指针而破坏标签协议：

```rust
fn ensure_aligned<T: ?Sized + Pointable>(raw: *mut ()) {
    assert_eq!(raw as usize & low_bits::<T>(), 0, "unaligned pointer");
}
```
见 [atomic.rs:65-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L65-L68)。

**构造**——`Atomic` 提供三条构造路径（[atomic.rs:293-338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L293-L338)）：`new(value)` 直接装箱；`init(init)` 走 `Pointable::init`（支持 DST）；`null()` 造一个空原子指针（`const fn`，可放 `static`）。内部都汇聚到私有的 `from_ptr`（[atomic.rs:314-319](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L314-L319)）。

#### 4.1.4 代码实践：亲手算一次标签位

**实践目标**：用类型系统验证「`i32` 有 2 个标签位、`i64` 有 3 个标签位」，并观察 tag 被截断。

**操作步骤**（新建一个临时测试，例如 `crossbeam-epoch/tests/tag_bits.rs`）：

```rust
// 示例代码：仅用于观察标签位宽，非项目原有代码
use crossbeam_epoch::{Atomic, Shared};
use std::sync::atomic::Ordering::SeqCst;

#[test]
fn observe_tag_bits() {
    // i32 对齐 4 -> 2 个标签位
    let a: Atomic<i32> = Atomic::new(0);
    let guard = &crossbeam_epoch::pin();
    let p = a.load(SeqCst, guard);

    // with_tag(3) 在 [0..=3] 内，能完整保留
    assert_eq!(p.with_tag(3).tag(), 3);
    // with_tag(7) 超出 2 位，被截断为 7 & 0b11 = 3
    assert_eq!(p.with_tag(7).tag(), 3);

    // i64 对齐 8 -> 3 个标签位，tag=7 合法
    let b: Atomic<i64> = Atomic::new(0);
    let q = b.load(SeqCst, guard);
    assert_eq!(q.with_tag(7).tag(), 7);

    unsafe { drop(a.into_owned()); drop(b.into_owned()); }
}
```

**需要观察的现象**：`p.with_tag(7).tag()` 等于 3（被掩码截断），而 `i64` 的 `with_tag(7)` 原样保留。

**预期结果**：断言全部通过。`tag()` 的实现见 [atomic.rs:1489-1492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1489-L1492)，`with_tag` 见 [atomic.rs:1513-1515](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1513-L1515)。

#### 4.1.5 小练习与答案

**练习 1**：`Atomic<u16>` 能塞多少位标签？合法 tag 范围是多少？

> **答**：`u16` 对齐为 2 = \(2^1\)，\(k=1\)，只有 1 个标签位，合法 tag 为 `{0, 1}`。源码测试 `valid_tag_i8`（[atomic.rs:1596-1599](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1596-L1599)）正是验证「对齐为 1 的 `i8` 只能 `with_tag(0)`」。

**练习 2**：为什么 `decompose_tag` 拆出的「裸指针」要再过一遍 `map_addr` 做位与，而不是直接 `ptr & !low_bits`？

> **答**：为了保留指针来源（provenance）。直接 `as usize` 位运算再转回指针会丢失来源，在严格来源模型下可能 UB；`map_addr` 用 `wrapping_add` 偏移，从原指针派生新指针，来源得以保留（见 [atomic.rs:89-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L89-L95)）。

---

### 4.2 `Owned` / `Shared` / `Pointer`：所有权语义与转换

#### 4.2.1 概念说明

同一根堆指针，在不同时刻有不同「姿态」，对应两个类型：

- **`Owned<T>`**——**独占**指针，非常像 `Box<T>`：拥有堆对象，`Drop` 时释放。它**没有生命周期参数**，因为所有权归它自己，不向任何人借用。
- **`Shared<'g, T>`**——**借用**指针，受 epoch 保护：不拥有对象、`Copy`、不释放任何东西。它的生命周期 `'g` 绑定一个 `Guard`（即一次 pin），表达「我只能在这次 grace period 内被解引用」。

为什么需要这两种？因为在并发结构里，「插入一个新节点」和「读取一个已有节点」对所有权的需求完全不同：

- 插入方**拥有**刚 `Box::new` 出来的节点，用 `Owned` 递进去，移交所有权；
- 读取方只是**临时拿到**指向节点的指针，绝不能擅自释放它（别的线程可能也在读），于是用受 `Guard` 约束的 `Shared`。

`Atomic::store/swap/compare_exchange` 这类写操作要能**同时接受** `Owned` 与 `Shared`，于是用 `Pointer<T>` trait 把两者统一封口。该 trait 是 sealed（[atomic.rs:925-939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939)），外部无法实现：

```rust
pub trait Pointer<T: ?Sized + Pointable>: crate::sealed::Sealed {
    fn into_ptr(self) -> *mut ();
    unsafe fn from_ptr(data: *mut ()) -> Self;
}
```

`into_ptr` 把智能指针**消耗**成一个裸机器字（含标签），`from_ptr` 反向重建。这两个方法是 `Owned`/`Shared` 与 `Atomic` 之间的「唯一通道」。

#### 4.2.2 核心流程：消耗、借用与转换

三者的关键差异在「`into_ptr` 如何处理 self」与「`Drop` 做不做」上：

| 维度 | `Owned<T>` | `Shared<'g, T>` |
| --- | --- | --- |
| 字段 | `data: *mut ()` + `PhantomData<Box<T>>` | `data: *mut ()` + `PhantomData<(&'g (), *const T)>` |
| `Copy` / `Clone` | 非 `Copy`；`Clone` 需 `T: Clone` 深拷贝 | `Copy` + `Clone`（廉价位拷贝） |
| `Drop` | **释放**堆对象（`T::drop`） | 无 `Drop`，什么都不释放 |
| `into_ptr` | `mem::forget(self)` 后返回，**防止 drop** | 直接返回 `self.data`（本就是 Copy） |
| 生命周期 | 无 | `'g` 绑定 `Guard` |

`into_ptr` 的差异最能体现所有权语义：

```rust
// Owned：消耗 self 但不能释放对象（要把所有权交给 Atomic）
fn into_ptr(self) -> *mut () {
    let data = self.data;
    mem::forget(self);   // 关键：忘记自己，避免 Drop 把对象释放掉
    data
}

// Shared：Copy，本就不释放，直接给地址
fn into_ptr(self) -> *mut () {
    self.data
}
```
见 [atomic.rs:953-959](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L953-L959) 与 [atomic.rs:1211-1215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1211-L1215)。`Owned::into_ptr` 必须 `mem::forget`，否则 `self` 一旦离开作用域就会触发 `Drop::drop`（[atomic.rs:1100-1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1100-L1107)）把对象释放——而它正要被存进 `Atomic`，释放就悬垂了。

#### 4.2.3 源码精读

**`Owned` 的结构与独占语义**：

```rust
pub struct Owned<T: ?Sized + Pointable> {
    data: *mut (),
    _marker: PhantomData<Box<T>>,
}
```
见 [atomic.rs:947-950](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L947-L950)。`PhantomData<Box<T>>` 告诉编译器「我像一个 `Box`」——于是自动获得正确的 drop 语义与协变行为。它的 `Drop` 调用 `T::drop(raw)` 真正释放对象（[atomic.rs:1100-1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1100-L1107)）。`Owned` 还实现了 `Deref`/`DerefMut`（[atomic.rs:1126-1140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1126-L1140)），所以 `&*owned` 能直接当 `&T` 用，体验与 `Box` 几乎一致。

**`Shared` 的结构与借用语义**：

```rust
pub struct Shared<'g, T: 'g + ?Sized + Pointable> {
    data: *mut (),
    _marker: PhantomData<(&'g (), *const T)>,
}
```
见 [atomic.rs:1197-1200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1197-L1200)。`PhantomData<(&'g (), …)>` 是把 `'g` 绑定进来的关键（见 4.2.5）。`Shared` 是 `Copy`（[atomic.rs:1202-1208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1202-L1208)），没有 `Drop`——它从不释放对象，释放由 epoch 的延迟回收统一负责。

**`Pointer` 统一封口**：sealed trait 见 [atomic.rs:925-939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939)，`Owned` 与 `Shared` 各自实现见 [atomic.rs:953-974](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L953-L974)、[atomic.rs:1211-1224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1211-L1224)。注意 `Pointer::from_ptr` 是 `unsafe`：它假定传入的 `data` 来自一次 `into_ptr`，且**同一份 `data` 不能被多次 `from_ptr`**（否则就是 double-own/double-free）。这是 `compare_exchange` 失败路径把 `new` 还给调用者时必须守住的契约（见 4.3.3）。

#### 4.2.4 转换关系总览

理解 `Owned`/`Shared`/`Atomic`/`Box` 之间的转换是本模块的实操重点，下表汇总（均来自 `atomic.rs` 里的 `From`/`into_*`）：

| 起点 → 终点 | 方法 / `From` | 位置 | 是否 `unsafe` | 说明 |
| --- | --- | --- | --- | --- |
| `Box<T>` → `Owned<T>` | `Owned::from` / `from_raw` | [atomic.rs:1148-1165](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1148-L1165), [atomic.rs:999-1003](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L999-L1003) | `from_raw` unsafe | 接管堆对象 |
| `Owned<T>` → `Box<T>` | `into_box` | [atomic.rs:1016-1020](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1016-L1020) | 否 | 退回标准盒子 |
| `Owned<T>` → `Shared<'g,T>` | `into_shared(&Guard)` | [atomic.rs:1063-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1063-L1065) | 否 | 借出，需 `Guard` 钉住 `'g` |
| `Shared<'g,T>` → `Owned<T>` | `into_owned` | [atomic.rs:1440-1443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1440-L1443) | **unsafe** | 仅当确认无人再持有引用 |
| `Owned<T>` → `Atomic<T>` | `From<Owned>` | [atomic.rs:875-879](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L875-L879) | 否 | 构造原子指针 |
| `Shared<'g,T>` → `Atomic<T>` | `From<Shared>` | [atomic.rs:904-906](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L904-L906) | 否 | 用借用值构造 |
| `Atomic<T>` → `Owned<T>` | `into_owned` / `try_into_owned` | [atomic.rs:780-782](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L780-L782), [atomic.rs:817-824](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L817-L824) | **unsafe** | 析构时常用（确认无并发访问） |

> 注意 `into_shared` 的签名 `pub fn into_shared<'g>(self, _: &'g Guard) -> Shared<'g, T>`（[atomic.rs:1063](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1063)）：参数 `&'g Guard` 实际上**没被函数体使用**（名字是 `_`），它的唯一作用是**借来 `'g` 这个生命周期**缝进返回的 `Shared`。这是一处典型的「用参数把生命周期引入签名」的手法。

#### 4.2.5 为什么 `Shared` 的 `'g` 绑定 `Guard`

这是本模块的核心安全论证，分两步。

**第一步：运行期保证——pin 期间对象不会被回收。**
`Guard` 是一次 `pin()` 的 RAII 凭证（[guard.rs:70-72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L70-L72)）。按 epoch 回收规则，只要本线程处于 pin 状态，本次 grace period 内被摘除的对象就**不会被销毁**（最多被塞进垃圾袋延迟两个 epoch）。所以 `load` 出来的 `Shared` 所指对象，在 `Guard` 存活期间一定有效。

**第二步：编译期保证——生命周期让引用跑不出 pin。**
`load` 的签名把返回值的 `'g` 借自传入的 `Guard`：

```rust
pub fn load<'g>(&self, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    unsafe { Shared::from_ptr(self.data.load(order)) }
}
```
见 [atomic.rs:356-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358)。而 `Shared` 内部 `PhantomData<(&'g (), *const T)>` 把这个 `'g` 贯穿到解引用结果——`deref` 返回的是 `&'g T`（[atomic.rs:1330-1332](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1330-L1332)）。于是借用检查器会强制：**从 `Shared` 派生出的任何 `&T`，都不能活过 `Guard`**。

合起来：运行期保证「`Guard` 在 → 对象在」，类型系统保证「`&T` 不活过 `Guard`」，两者咬合，就杜绝了「指针还捏在手里、对象已被别的线程回收」的 use-after-free。这也回答了规格里的思考题：**`'g` 绑定 `Guard`，是为了把 epoch 的 grace period 约束编码进类型，让悬垂引用在编译期就不可能发生。**

> 旁证：`Owned` 没有 `'g`，所以 `Shared::into_owned`（[atomic.rs:1440-1443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1440-L1443)）是 `unsafe`——一旦把借用升级成独占，生命周期护栏就消失了，调用者必须自行保证「没有别的线程还在用」。

#### 4.2.6 代码实践：追踪一次所有权的「移交 → 借出 → 收回」

**实践目标**：用源码阅读理解所有权在三类型间的流动，画出调用链。

**操作步骤**：

1. 从 `Owned::new(1234)` 出发，追踪 `Owned::into_ptr`（[atomic.rs:953-959](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L953-L959)）如何 `mem::forget(self)`；
2. 经 `Atomic::from(owned)`（[atomic.rs:875-879](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L875-L879)）进入 `Atomic`；
3. 再 `load` 出 `Shared`（[atomic.rs:356-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358)），最终 `Shared::into_owned`（[atomic.rs:1440-1443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1440-L1443)）收回独占。

**需要观察的现象**：在每一步标注「此刻谁拥有这个堆对象」（`Owned` / `Atomic` / `Shared`（不拥有）/ `Owned`）。

**预期结果**：你能用一句话说清——所有权沿 `Owned → Atomic → （Shared 仅借用） → Owned` 流转，期间真正「释放权」只在 `Owned` 的 `Drop` 与 `into_ptr` 的 `forget` 两处发生。

#### 4.2.7 小练习与答案

**练习 1**：为什么 `Shared` 是 `Copy` 而 `Owned` 不是？

> **答**：`Shared` 只是「一个机器字 + 生命周期标记」，拷贝它不会产生两个「释放者」——它本就不释放任何东西，释放由 epoch 统一负责，所以 `Copy` 安全。`Owned` 拥有对象，若 `Copy` 会产生两个都认为自己该释放的副本，导致 double-free，故只能 `mem::forget` 后move（见 [atomic.rs:953-974](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L953-L974)）。

**练习 2**：`Owned::into_ptr` 为什么要 `mem::forget(self)`？不写会怎样？

> **答**：`into_ptr` 是要把对象交给 `Atomic` 存储。若不 `forget`，函数返回时 `self` 被 drop，触发 `T::drop` 把对象释放，`Atomic` 里就存了个悬垂指针。`forget` 阻止 drop，把「释放责任」随裸指针一起移交出去（[atomic.rs:955-958](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L955-L958)）。

**练习 3**：`into_shared` 的参数 `&'g Guard` 在函数体里根本没用到（名为 `_`），为什么还要它？

> **答**：它不参与逻辑，只为把 `'g` 这个生命周期从调用方的 `Guard` 借进来，缝进返回类型 `Shared<'g, T>`，从而让借用检查器把 `Shared` 的可用范围锚定在该 `Guard` 上（[atomic.rs:1063-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1063-L1065)）。

---

### 4.3 `load` / `store` / `swap` / `compare_exchange` 操作族

#### 4.3.1 概念说明

有了 `Atomic` 与 `Pointer`，操作族就很规整了——它们基本是 `AtomicPtr` 对应方法的一层「类型还原 + 标签感知」封装。按「是否需要 `Guard`」分两类：

- **读类**（`load`/`load_consume`）与**读改写类**（`swap`/`compare_exchange`/`fetch_*`）都要 `&'g Guard`，因为它们返回 `Shared<'g, T>`，必须借来 `'g`；
- **纯写类**（`store`）**不需要 `Guard`**——它只往里塞一个 `Pointer`，不返回任何受 epoch 保护的值。

最关键的一点：**`compare_exchange` 比较的是整个机器字（指针 + 标签）**。文档原话：「The tag is also taken into account, so two pointers to the same object, but with different tags, will not be considered equal.」（[atomic.rs:428-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L428-L430)）。这意味着 CAS 天然能做「原子地换指针 + 改标签」。

#### 4.3.2 核心流程：一次 CAS 的成功 / 失败两条路径

`compare_exchange` 的返回类型是一个带丰富信息的 `Result`：

- 成功 → `CompareExchangeValue { old, new }`（[atomic.rs:23-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L23-L29)）：`old` 是替换前的值（应等于 `current`），`new` 是刚写入的值；
- 失败 → `CompareExchangeError { current, new }`（[atomic.rs:41-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L41-L47)）：`current` 是此刻的真实值，`new` 是**没存进去、原样退还**的 `Pointer`（注意类型是泛型 `P`，保持你传入时的形态，比如仍是 `Owned`）。

失败时把 `new` 还给调用者这一设计很巧妙：调用者可以拿 `err.current` 更新预期值后，用**同一个** `err.new` 重试，无需重新构造新值——标准库 `compare_exchange_weak` 的经典循环范式因此能干净写出来（见 [atomic.rs:519-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L519-L543) 的文档示例）。

#### 4.3.3 源码精读

**`store`——不需要 Guard 的纯写**：

```rust
pub fn store<P: Pointer<T>>(&self, new: P, order: Ordering) {
    self.data.store(new.into_ptr(), order);
}
```
见 [atomic.rs:403-405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L403-L405)。它接受任意 `Pointer`（`Owned` 或 `Shared`），`into_ptr` 消耗之取出机器字存入。注意：**它不返回旧值**，若旧对象无人接管便会泄漏——需要旧值时改用 `swap`。

**`swap`——读改写，返回旧值**：

```rust
pub fn swap<'g, P: Pointer<T>>(&self, new: P, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    unsafe { Shared::from_ptr(self.data.swap(new.into_ptr(), order)) }
}
```
见 [atomic.rs:424-426](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L424-L426)。`Guard` 同样仅用于借 `'g`。

**`compare_exchange`——比较整个字（含标签）**：

```rust
pub fn compare_exchange<'g, P>(
    &self,
    current: Shared<'_, T>,
    new: P,
    success: Ordering,
    failure: Ordering,
    _: &'g Guard,
) -> Result<CompareExchangeValue<'g, T>, CompareExchangeError<'g, T, P>>
where
    P: Pointer<T>,
{
    let new = new.into_ptr();
    self.data
        .compare_exchange(current.into_ptr(), new, success, failure)
        .map(|old| unsafe {
            CompareExchangeValue {
                old: Shared::from_ptr(old),
                new: Shared::from_ptr(new),
            }
        })
        .map_err(|current| unsafe {
            CompareExchangeError {
                current: Shared::from_ptr(current),
                new: P::from_ptr(new),   // 把没存进的 new 还原成原类型 P 退还
            }
        })
}
```
见 [atomic.rs:460-486](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L460-L486)。要点有三：

1. `current.into_ptr()` 与底层 `AtomicPtr` 比较的是**完整机器字**——低位标签也在内，所以标签不同即视为「不相等」而失败；
2. 成功路径把底层返回的旧字 `old` 和新字 `new` 都还原成 `Shared`；
3. 失败路径用 `P::from_ptr(new)` 把 `new` 还原回调用者当初传入的类型（如 `Owned`），方便重试——这正是 4.2.3 提到的 `from_ptr` 的「单次」契约：成功时 `new` 已被存入 `Atomic`，失败时 `new` 经 `from_ptr` 退回调用者，两处恰好各一次。

> `compare_exchange_weak`（[atomic.rs:544-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L544-L570)）逻辑相同，只是允许伪失败（spurious failure），在循环里用更高效。

**`load_consume`——RCU 友好的读**：

```rust
pub fn load_consume<'g>(&self, _: &'g Guard) -> Shared<'g, T> {
    unsafe { Shared::from_ptr(self.data.load_consume()) }
}
```
见 [atomic.rs:382-384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L382-L384)。`load_consume` 来自 crossbeam-utils 的 `AtomicConsume`（[atomic.rs:12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L12)），在弱内存模型上比 `Acquire` 便宜，适合 epoch 的「读指针→解引用」读路径（详见 [u2-l4](u2-l4-atomic-consume.md)）。

**标签位运算 `fetch_and/or/xor`**——直接在标签位上做按位与/或/异或，返回旧 `Shared`：

```rust
pub fn fetch_or<'g>(&self, val: usize, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    let val = val & low_bits::<T>();   // 先把 val 截到标签位宽
    // …底层 fetch_or…
}
```
见 [atomic.rs:689-706](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L689-L706)（`fetch_and` 见 [atomic.rs:651-668](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L668)，`fetch_xor` 见 [atomic.rs:727-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L727-L744)）。它们是「只改标签、不动指针」的快捷方式，常用于无锁结构里翻转状态位。注意源码注释里的严格来源取舍：理想用 `AtomicPtr::fetch_*`（严格来源兼容），但它需要 Rust 1.91，受 MSRV 限制目前只在 `cfg(miri)` 下用，其余平台退回经 `AtomicUsize` 转换的写法（仍是合法的 permissive-provenance）。

**`fetch_update`——CAS 循环糖**：`fetch_update`（[atomic.rs:612-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L612-L630)）把「load → 闭包算 next → `compare_exchange_weak` 重试」打包，返回 `Ok(旧值)` 或 `Err(最后见到的值)`。

**`into_owned` / `try_into_owned`——析构专用**：消耗 `Atomic` 拿回 `Owned`（[atomic.rs:780-782](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L780-L782)、[atomic.rs:817-824](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L817-L824)），是数据结构 `Drop` 实现里的常客（文档示例见 [atomic.rs:763-779](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L763-L779)）。

#### 4.3.4 代码实践：用 `compare_exchange` 原子地「换指针 + 改标签」

这是规格里要求的核心实践。**实践目标**：用 `Atomic<i32>`（对齐 4，2 个标签位，tag ∈ 0..=3）存一个带标签的指针，用 CAS 同时更新指针与标签，并验证「同一对象、不同标签」会让 CAS 失败。

**操作步骤**（新建临时二进制，例如 `crossbeam-epoch/examples/cas_tag.rs`，或写进一个 `#[test]`）：

```rust
// 示例代码：演示 compare_exchange 同时更新指针与标签
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // i32 对齐 = 4 = 2^2 -> 2 个标签位，tag 取值 0..=3
    let a: Atomic<i32> = Atomic::new(42);
    let guard = &epoch::pin();

    // 1) 读出当前值：对象 42，标签 0
    let curr = a.load(SeqCst, guard);
    assert_eq!(curr.tag(), 0);
    assert_eq!(unsafe { curr.as_ref() }, Some(&42));

    // 2) CAS：把指针换成 100，同时把标签置为 2
    let new = Owned::new(100).with_tag(2);
    let ok = a.compare_exchange(curr, new, SeqCst, SeqCst, guard).unwrap();
    assert_eq!(unsafe { ok.old.as_ref() }, Some(&42)); // old = 替换前
    assert_eq!(ok.old.tag(), 0);
    assert_eq!(unsafe { ok.new.as_ref() }, Some(&100)); // new = 写入后
    assert_eq!(ok.new.tag(), 2);

    // 3) 验证标签确实打进了原子字
    let now = a.load(SeqCst, guard);
    assert_eq!(now.tag(), 2);
    assert_eq!(unsafe { now.as_ref() }, Some(&100));

    // 4) 同一对象、不同标签 -> CAS 必失败（标签参与比较）
    let same_obj_wrong_tag = now.with_tag(0); // 指针同，tag=0
    let err = a
        .compare_exchange(same_obj_wrong_tag, Owned::new(7), SeqCst, SeqCst, guard)
        .unwrap_err();
    assert_eq!(err.current.as_raw(), now.as_raw()); // 仍是同一对象
    assert_eq!(err.current.tag(), 2);               // 但真实标签是 2，故失败

    // 5) 清理（避免内存泄漏）
    unsafe {
        drop(ok.old.into_owned()); // 回收被替换掉的旧对象 42
        drop(a.into_owned());      // 回收最终存留的对象 100
    }
    println!("所有断言通过：CAS 同时更新了指针与标签。");
}
```

**需要观察的现象**：

- 第 2 步 CAS 成功后，`ok.new.tag()` 为 2，证明标签随指针一起被原子写入；
- 第 4 步即使 `same_obj_wrong_tag` 与存储值指向**同一个对象**，仅因标签 0 ≠ 2，CAS 仍失败，`err.current.tag()` 读出真实标签 2。

**预期结果**：程序打印「所有断言通过」，无内存泄漏。如果想跨线程验证原子性，可把第 2 步换成多线程并发 CAS 并统计成功/失败次数——CAS 的原子性由底层 `AtomicPtr::compare_exchange` 保证。若你无法在本地运行，**待本地验证**。

> 源码佐证：项目自带测试 `compare_exchange_success`（[atomic.rs:1622-1652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1622-L1652)）与 `compare_exchange_failure`（[atomic.rs:1654-1690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1654-L1690)）与本实践一一对应，可直接 `cargo test -p crossbeam-epoch compare_exchange` 运行。

#### 4.3.5 小练习与答案

**练习 1**：`store` 为什么不要求传 `Guard`，而 `swap` 要求？

> **答**：`store` 只是把一个 `Pointer` 写入，不返回任何受 epoch 保护的值，故不需要 `Guard` 来借 `'g`（[atomic.rs:403-405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L403-L405)）。`swap` 要返回**旧的** `Shared<'g, T>`，必须借 `'g` 锚定其可解引用期，故需 `&'g Guard`（[atomic.rs:424-426](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L424-L426)）。

**练习 2**：`compare_exchange` 失败时为什么用 `P::from_ptr(new)` 把 `new` 还原成 `P`，而不是直接返回 `Shared`？

> **答**：为了支持「同一个 new 反复重试」的循环范式。若调用者传入 `Owned`，失败后拿回的仍是 `Owned`，可直接进入下一轮 CAS；若强制变成 `Shared`，则所有权语义被破坏、且要额外 `Guard` 约束。退还原类型 `P` 让 [atomic.rs:519-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L519-L543) 的重试循环写得很自然。同时这遵守了 `from_ptr` 的「单次」契约：成功路径 `new` 已入 `Atomic`，失败路径 `new` 经 `from_ptr` 退回，恰好各一次。

**练习 3**：想「只翻转标签、不动指针」该用哪个方法？为什么不用 `compare_exchange`？

> **答**：用 `fetch_or` / `fetch_and` / `fetch_xor`（[atomic.rs:651-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L744)）。它们直接对低位做原子按位运算，单条指令完成，无需 load-then-CAS 两步，也就不会因并发改指针而反复失败重试。

---

## 5. 综合实践：迷你 Treiber 栈的「逻辑删除位」

把本讲三块知识串起来，实现一个**无锁 Treiber 栈**，并用**标签位**标记节点的「逻辑删除」状态。这是 epoch `Atomic` 最经典的应用，也为 [u5-l3](u5-l3-epoch-guard.md) 学习 `defer_destroy` 埋下伏笔。

**任务**：

1. 定义 `Node<T> { data: ManuallyDrop<T>, next: Atomic<Node<T>> }` 与 `Stack<T> { head: Atomic<Node<T>> }`；
2. `push`：构造 `Owned::new(node)`，循环 `head.compare_exchange(current, new, …)`（标准的 Treiber push），用 `Backoff` 退避失败；
3. 给节点加一个**逻辑删除标签**：用一个对齐 ≥ 4 的字段（或干脆给 `Atomic<Node>` 的 tag 留 2 位），用 `fetch_or(1, …)` 把 tag 最低位置 1 表示「已逻辑删除」；

   > 提示：`Atomic<Node<T>>` 中 `Node<T>` 的对齐取决于其字段，若 ≥ 4 则 tag 至少有 2 位。可读 `align_of` 自行确认（**待本地验证**当前 `Node` 布局的标签位宽）。
4. `pop`：load 出 head，先用 `fetch_or` 标记其逻辑删除位，再 CAS 推进 head；被弹出节点的真正释放**交给 epoch**——用 `guard.defer_destroy(old)`（下一讲 [u5-l3](u5-l3-epoch-guard.md) 详讲）延迟回收。

**验收**：

- 单线程下 push/pop 顺序正确；
- 多线程并发 push/pop 无丢失、无 double-free（用计数器校验）；
- 能解释「为什么 pop 时不能直接 `drop` 节点」——因为别的线程可能正捏着指向它的 `Shared`，这正是 epoch 要解决的核心难题（见 [u5-l1](u5-l1-epoch-overview.md)）。

> 进阶：把第 2 步的 `compare_exchange` 与第 3 步的 `fetch_or` 对照本讲 4.3.3 阅读，体会「CAS 比较整字（含标签）」如何让「标记 + 推进」能原子完成。

## 6. 本讲小结

- `Atomic<T>` 本质是一个机器字 `AtomicPtr<()>`，`_marker: PhantomData<*mut T>` 只在类型层记住指向 `T`，自身无生命周期、可入 `static`。
- 对齐指针的低位为 0，是「白送的标签位」：\(k = \text{ALIGN.trailing\_zeros()}\)，合法 tag ∈ \([0, 2^k-1]\)，由 `low_bits`/`compose_tag`/`decompose_tag` 打包拆包，并用 `map_addr` 保留指针来源。
- `Owned<T>` 独占（像 `Box`，`Drop` 释放，`into_ptr` 需 `forget`）；`Shared<'g,T>` 借用（`Copy`、不释放、`'g` 绑 `Guard`）；二者经 sealed `Pointer<T>` 统一喂给 `Atomic` 的写操作。
- `Shared` 的 `'g` 绑定 `Guard`，把 epoch 的 grace period 编码进类型——运行期保证 pin 期间对象不回收，编译期保证 `&T` 不活过 `Guard`，双保险防 use-after-free。
- `compare_exchange` 比较**整个机器字**（指针 + 标签），故能原子地「换指针 + 改标签」，失败时把 `new` 按原类型 `P` 退还以便重试；`store` 不要 `Guard`、`swap`/读类要 `Guard`；`fetch_and/or/xor` 是「只动标签」的快捷原子位运算。

## 7. 下一步学习建议

- **下一讲 [u5-l3](u5-l3-epoch-guard.md)**：精读 `guard.rs` 与 `deferred.rs`，把本讲反复出现的 `Guard`、`pin()`、`defer_destroy` 讲透——届时你会看清「被 CAS 替换掉的旧 `Shared` 究竟何时、由谁释放」。
- **随后 [u5-l4](u5-l4-epoch-collector.md) / [u5-l5](u5-l5-epoch-internals.md)**：进入 `Collector` 与 `internal.rs`，理解 epoch 如何推进、垃圾如何延迟两个 epoch 后销毁。
- **横向延伸**：回看 [u2-l3](u2-l3-atomic-cell.md) 的 `AtomicCell` 与本讲的 `Atomic` 对比——前者是「值的原子搬动」，后者是「指针 + 标签的原子搬动 + 延迟回收」，两者共同构成 crossbeam 的原子基石。
- **综合应用**：本讲综合实践实现的 Treibor 栈，正是 [u7-l1](u7-l1-skiplist-base.md)/[u7-l2](u7-l2-skiplist-ops.md) 跳表节点并发操作的简化原型，学完 epoch 全貌后可对照跳表源码加深理解。
