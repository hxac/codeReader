# AtomicCell 公共 API 与数据结构

## 1. 本讲目标

本讲是 atomic 模块（[u1-l2](u1-l2-module-map.md) 里那张「公开类型总表」中的 `atomic` feature 一行）的第一篇。我们暂时**不**追问 `AtomicCell` 在底层是怎么做到线程安全的——那是 [u2-l2 无锁路径](u2-l2-atomiccell-lockfree.md) 与 [u2-l3 全局锁回退](u2-l3-atomiccell-global-lock-seqlock.md) 的主题。本讲只回答一个问题：**作为使用者，`AtomicCell<T>` 长什么样、有哪些方法、各方法的语义和内存序约定是什么。**

学完后你应当能够：

- 说出 `AtomicCell<T>` 的内部数据结构（`UnsafeCell<MaybeUninit<T>>`）以及为什么这样设计。
- 复述每个公共方法（`new` / `into_inner` / `load` / `store` / `swap` / `take` / `compare_exchange` / `fetch_update`）的语义，以及它们各自要求 `T` 满足什么 trait bound。
- 解释 `AtomicCell` 的内存序约定：**load 用 `Acquire`，store 用 `Release`，swap / compare_exchange 用 `AcqRel`**。
- 理解 `MaybeUninit`、`needs_drop` 与 `Drop` 三者如何配合，避免非 `Copy` 类型在原子单元里发生「泄漏」或「double-drop」。

本讲只读 `src/atomic/atomic_cell.rs` 的**对外 API 部分**（结构定义、各 `impl` 块、`Drop`），以及 `src/atomic/mod.rs` 里 `AtomicCell` 的导出行。底层的 `atomic!` 宏分发、`can_transmute`、`SeqLock` 等机制留给后续两讲。

## 2. 前置知识

### 2.1 `Cell` 与「内部可变性」

Rust 默认「拥有某值的不可变引用 `&T`，就不能修改它」。但有时我们需要「对外看起来是 `&T`，内部仍能改值」——这就是**内部可变性（interior mutability）**。标准库的 `std::cell::Cell<T>` 提供了单线程下的内部可变性：你可以拿着 `&Cell<T>` 调用 `cell.set(...)` 改值。

但 `Cell` **不是 `Sync`**，不能跨线程共享。`AtomicCell<T>` 想做的事就是「`Cell` 的多线程版本」：拿着 `&AtomicCell<T>` 就能读写，且操作是原子的、线程安全的。这一点写在了它的文档注释里——「equivalent to `Cell`, except it can also be shared among multiple threads」。

### 2.2 `UnsafeCell`：内部可变性的编译器开关

`UnsafeCell<T>` 是 Rust 里**唯一**允许「通过 `&T` 改值」的合法途径。任何想要内部可变性的类型（`Cell`、`RefCell`、`AtomicUsize`、本讲的 `AtomicCell`）内部都包着一个 `UnsafeCell`。它本身不做任何同步，只是一个告诉编译器「这里可能发生别名 + 变更，请勿做错误优化」的标记。线程安全由类型自己的 `unsafe impl Sync` 来保证（见 4.1.3）。

### 2.3 `MaybeUninit`：禁止观察「半初始化」状态

`MaybeUninit<T>` 是一个「可能尚未初始化」的包装器，它**不会自动 drop 内部的 `T`**。`AtomicCell` 把值包成 `MaybeUninit` 不是因为真要存未初始化值（它的 API 只接收已初始化的 `T`），而是有两个工程目的：

1. 绕开一个旧 rustc bug，避免编译器观察到「部分初始化」的中间状态（详见源码注释引用的 issue #833）。
2. 把「何时 drop 内部 `T`」的控制权**交给 `AtomicCell` 自己**——这正是 4.4 节 `Drop` 实现的关键。

### 2.4 内存序：`Acquire` / `Release` / `AcqRel` 的直觉

原子操作除了「不可分割」，还有「内存序」这个维度，它约束着该操作前后其他（非原子）读写的可见顺序。本讲只需记住三条直觉：

- **`Release`（写端用）**：「我这次写入之前的所有读写，都会在对端看到这次写入之前对它可见。」——保证发布的数据已就绪。
- **`Acquire`（读端用）**：「我这次读到的值，其写入者发布的那批读写，对我都已可见。」——保证读到数据后能安全使用其关联状态。
- **`AcqRel`（读改写用）**：既是读又是写（如 `swap`、`compare_exchange`），同时具备 `Acquire` 和 `Release` 两端的保证。

> `AtomicCell` 对外**不暴露** `Ordering` 参数：它替你固定好了「load = `Acquire`，store = `Release`，读改写 = `AcqRel`」。这是它比 `core::sync::atomic::AtomicXxx` 更省心的地方，代价是丧失自定义内存序的灵活性。

把 2.1–2.4 串起来：`AtomicCell` 要做「可跨线程共享的 `Cell`」，于是内部用 `UnsafeCell` 拿到内部可变性、用 `MaybeUninit` 掌控 drop 时机、用固定的 `Acquire`/`Release` 内存序保证可见性。这就是本讲全部概念的基础。

## 3. 本讲源码地图

本讲涉及 2 个文件，关注点如下：

| 文件 | 角色 | 本讲关注点 |
|------|------|------------|
| [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) | `AtomicCell` 实现 | 结构定义、`Send`/`Sync`、各 `impl` 块的公共方法、`Drop` |
| [src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | atomic 子模块根 | `AtomicCell` 的私有声明与 `pub use` 重导出 |

> 本讲**不展开** `atomic!` 宏、`can_transmute`、`atomic_load`/`atomic_store` 等自由函数内部的「选路径」逻辑，只把它们当作「`AtomicCell` 方法最终调用的实现」来引用其内存序。选路径的机制是 [u2-l2](u2-l2-atomiccell-lockfree.md) 的主题。

## 4. 核心概念与源码讲解

### 4.1 AtomicCell 的数据结构与生命周期

#### 4.1.1 概念说明

`AtomicCell<T>` 是一个**线程安全的可变内存位置**。从使用角度看，它就像一个可以跨线程共享的 `Cell<T>`：你拿着 `&AtomicCell<T>` 就能读写其中的 `T`，而不需要包一层 `Mutex`。

它的内部存储只有一行：

```rust
#[repr(transparent)]
pub struct AtomicCell<T> {
    value: UnsafeCell<MaybeUninit<T>>,
}
```

三个设计要点：

1. **`UnsafeCell<MaybeUninit<T>>`**：`UnsafeCell` 提供内部可变性（见 2.2），`MaybeUninit` 接管 drop 时机（见 2.3）。
2. **`#[repr(transparent)]`**：`AtomicCell<T>` 与 `UnsafeCell<MaybeUninit<T>>`、进而与 `T` 拥有**完全相同的内存布局**。这一点至关重要——它让 `AtomicCell` 在底层可以把自己的指针「重解释」成原生原子类型（如 `AtomicUsize`）来执行真正的原子指令。这条无锁路径是 [u2-l2](u2-l2-atomiccell-lockfree.md) 的主题，本讲只需知道「布局相同」是它的前提。
3. **`Send` + `Sync`**：`AtomicCell<T>` 在 `T: Send` 时声明自己是 `Send + Sync`，即「只要内部值能在线程间安全转移，这个原子单元就能被多线程共享」。注意它要求 `T: Send` 而非 `T: Sync`——因为读改写操作会把值「拷出/换入」，相当于在线程间转移所有权，但不允许并发地拿 `&T` 出去共享。

#### 4.1.2 核心流程

「创建 → 使用 → 销毁」的生命周期：

```
new(val)            : 把 val 装进 MaybeUninit，得到 AtomicCell
  ↓
load/store/swap/... : 拿 &AtomicCell 做线程安全的读改写（本讲 4.2、4.3）
  ↓
into_inner(self)    : 按值消费，取出内部的 T（独占，保证无并发）
  或
drop                : AtomicCell 被销毁时，若 T 需要 drop，则 drop_in_place 内部值（本讲 4.4）
```

`new` 和 `into_inner` 都是 `const fn`，可以在常量上下文里使用（例如 `static CELL: AtomicCell<usize> = AtomicCell::new(0);`），这一点在测试里能看到真实用法。

#### 4.1.3 源码精读

**结构定义与文档约定。** 第 32–48 行是整个类型的定义。文档注释（第 18–31 行）已经把本讲最关键的一句话写明了：「Atomic loads use the `Acquire` ordering and atomic stores use the `Release` ordering」——这条约定我们在 4.2 节会从源码里逐条验证。

[src/atomic/atomic_cell.rs:32-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L32-L48) — `AtomicCell<T>` 结构定义：`#[repr(transparent)]` + `UnsafeCell<MaybeUninit<T>>`。

[src/atomic/atomic_cell.rs:18-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L18-L31) — 顶层文档注释，写明 load=`Acquire`、store=`Release` 的固定内存序约定。

**`Send` / `Sync` 的 unsafe 声明。** 第 50–51 行手动声明这两个 trait。`unsafe impl` 意味着作者必须自己担保安全性——这里的担保是：「只要 `T: Send`（能在线程间安全转移），那么对 `AtomicCell<T>` 的所有操作都是原子的/加锁的，不会出现数据竞争，因此可以 `Sync`」。

[src/atomic/atomic_cell.rs:50-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L50-L51) — `unsafe impl<T: Send> Send` 与 `unsafe impl<T: Send> Sync`。

**`new` 构造。** 第 66–70 行把传入的 `val` 用 `MaybeUninit::new` 包好后塞进 `UnsafeCell`。注意它是 `const fn`，所以可以在 `static` 上下文使用（测试 `const_atomic_cell` 里就有 `static CELL: AtomicCell<usize> = AtomicCell::new(0);` 的真实用例）。

[src/atomic/atomic_cell.rs:66-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L66-L70) — `pub const fn new(val: T) -> Self`。

**`into_inner` 消费。** 第 87–127 行按值取出内部的 `T`。因为接收 `self`（按值），调用发生时调用者已放弃访问权，**保证没有其他线程在并发访问**，所以取出是安全的。实现上它没有走 `UnsafeCell::into_inner`（在旧 rustc 的 const 上下文不稳定），而是用一个局部的 `const unsafe fn transmute_copy_by_val`（第 103–118 行，一个 `#[repr(C)] union` 的「const hack」）做按值位转换；安全性注释在第 120–126 行，依据正是 4.1.1 提到的「`repr(transparent)` ⇒ 布局相同」。

[src/atomic/atomic_cell.rs:87-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L87-L127) — `pub const fn into_inner(self) -> T`，含 `transmute_copy_by_val` const hack 与 SAFETY 说明。

> 这个 `transmute_copy_by_val` 的 unsafe 细节（为何用 union 而非 `transmute_copy`）属于专家层内容，留到 [u5-l1 AtomicCell 的 unsafe 深析](u5-l1-atomiccell-unsafe-safety.md) 集中剖析。本讲只需理解「它等价于把 `self` 按位重新解释成 `T` 并返回」。

**`is_lock_free` 探测。** 第 160–162 行是本讲顺带提及的一个方法：它返回「这个 `T` 的操作能否走无锁（原子指令）路径」。它的实现 `atomic_is_lock_free::<T>()` 内部就是 4.2 节要反复出现的那个「选路径」逻辑——本讲只把它当 API 用，机制详讲见 [u2-l2](u2-l2-atomiccell-lockfree.md)。

[src/atomic/atomic_cell.rs:160-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L160-L162) — `pub const fn is_lock_free() -> bool`。

#### 4.1.4 代码实践

**实践目标**：用最小的例子跑通 `AtomicCell` 的「创建 → 读写 → 消费」生命周期，并验证 `repr(transparent)` 带来的一个直接后果——`AtomicCell<T>` 与 `T` 大小相同。

**操作步骤**：

1. 新建一个 binary crate（在 `crossbeam-utils` 目录之外，避免循环依赖），在 `Cargo.toml` 加入依赖（开启 `atomic` feature）：

   ```toml
   [dependencies]
   crossbeam-utils = { path = "<绝对路径>/crossbeam-utils", default-features = false, features = ["atomic"] }
   ```

2. 在 `src/main.rs` 写（示例代码）：

   ```rust
   use crossbeam_utils::atomic::AtomicCell;

   fn main() {
       let a: AtomicCell<u64> = AtomicCell::new(7);
       assert_eq!(a.into_inner(), 7);

       // repr(transparent) 的直接后果：布局与 T 相同
       assert_eq!(std::mem::size_of::<AtomicCell<u64>>(), std::mem::size_of::<u64>());

       // const 上下文可用
       const IS_LOCK_FREE: bool = AtomicCell::<usize>::is_lock_free();
       println!("usize lock-free? {IS_LOCK_FREE}");
   }
   ```

3. 编译运行：

   ```bash
   cargo run
   ```

**需要观察的现象**：`into_inner` 取出的值与传入一致；`AtomicCell<u64>` 与 `u64` 字节数相同（典型 64 位目标上都是 8）；`is_lock_free` 在普通 64 位目标上打印 `true`。

**预期结果**：三个断言全部通过。若你的平台不支持指针宽度原子（`target_has_atomic="ptr"` 为假），则 `AtomicCell` 本身不可用，会编译失败——这正是 [u1-l2](u1-l2-module-map.md) 讲过的 `atomic/mod.rs` 门控。

> 待本地验证：`is_lock_free` 在 miri / loom / sanitizer 下可能被强制为 `false`（走全局锁回退），具体见 [u1-l3](u1-l3-features-build-and-tests.md) 的 `force_fallback` cfg。

#### 4.1.5 小练习与答案

**练习 1**：`AtomicCell` 声明 `unsafe impl<T: Send> Sync`，要求的是 `T: Send` 而非 `T: Sync`。为什么？

> **答案**：`Sync` 要求「`&T` 能跨线程共享」。但 `AtomicCell` 的 API（`load`/`swap`/`compare_exchange`）都是把值**整份读出或换入**，相当于在线程间转移 `T` 的所有权，而不是让多个线程同时持有 `&T`。因此只要 `T` 能安全转移（`Send`）即可；并不需要 `T` 自身支持共享引用（`Sync`）。

**练习 2**：`into_inner` 的文档说「passing `self` by value guarantees that no other threads are concurrently accessing the atomic data」。请用 Rust 的所有权模型解释这句话。

> **答案**：`into_inner(self)` 接收 `self` 按值，调用者必须拥有 `AtomicCell` 的所有权（例如持有 `AtomicCell` 本身，或通过 `Arc::try_unwrap` 拿到独占所有权）。Rust 编译器在编译期保证「按值移动之后，原位置失效」，因此不可能有另一个引用同时访问它——这正是「by value ⇒ 无并发」的安全性来源。

**练习 3**：`new` 和 `into_inner` 为什么都标注成 `const fn`？

> **答案**：让 `AtomicCell` 能在常量上下文使用，最典型的是 `static CELL: AtomicCell<usize> = AtomicCell::new(0);`——用一个原子单元做进程级全局计数器，且无需任何运行期初始化代码。测试 `const_atomic_cell`（[tests/atomic_cell.rs:275-294](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L275-L294)）正是验证这一点。

---

### 4.2 load / store / swap / take：读写语义与固定内存序

#### 4.2.1 概念说明

这一组方法覆盖「最基本的读写」。它们最大的设计特点是：**API 不暴露 `Ordering` 参数**，由 `AtomicCell` 内部固定为前述约定（load=`Acquire`、store=`Release`、读改写=`AcqRel`）。这降低了使用门槛——你不需要思考内存序，但代价是不能做更激进的优化（如 `Relaxed` 计数）。

另一个关键设计是**按 trait bound 分层提供方法**。不同的方法对 `T` 提出不同的能力要求，作者把它们分到不同的 `impl` 块里，让编译器帮你把关：

| 方法 | 所在 `impl` 块的额外约束 | 为什么需要这个约束 |
|------|--------------------------|--------------------|
| `store` / `swap` / `as_ptr` | `impl<T>`（无） | 写入/交换只是搬字节，对 `T` 无要求 |
| `take` | `impl<T: Default>` | 取走后要留一个默认值「占位」 |
| `load` | `impl<T: Copy>` | 读取会复制一份返回，需要 `Copy` |
| `compare_exchange` / `fetch_update` | `impl<T: Copy + Eq>` | 比较交换需要判断「相等」 |

这张表是本节最重要的结构性结论：**看到一个 `AtomicCell` 方法名，先回想它在哪个 `impl` 块、要求 `T` 满足什么**，就能立刻判断它对自定义类型是否可用。

#### 4.2.2 核心流程

四个方法的语义可以用「读 / 写 / 读改写 / 取走」来分类：

```
load()   -> T            读：原子地读出当前值的一份拷贝（Acquire）
store(v)                  写：原子地写入 v（Release；若 T 需要 drop，先回收旧值）
swap(v)   -> T            读改写：原子地写入 v 并返回旧值（AcqRel）
take()   -> T             读改写：等价于 swap(Default::default())，把单元「掏空」成默认值
```

`store` 有一个**容易踩坑的细节**值得单独记：当 `T` 不是 `Copy`（即 `mem::needs_drop::<T>()` 为真）时，`store` 不能像 `Copy` 类型那样「直接覆盖」——否则旧值会被静默泄漏。所以 `store` 在这种情况下改走 `swap`：先把新值换进去、拿到旧值、再 drop 旧值。

#### 4.2.3 源码精读

**`store` 的两条分支。** 第 177–185 行：

```rust
pub fn store(&self, val: T) {
    if mem::needs_drop::<T>() {
        drop(self.swap(val));      // 非 Copy：换出旧值并回收，避免泄漏
    } else {
        unsafe { atomic_store(self.as_ptr(), val); }  // Copy：直接原子写
    }
}
```

[src/atomic/atomic_cell.rs:177-185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L177-L185) — `store`：按 `needs_drop` 分流，「非 Copy 走 swap 回收旧值，Copy 直接原子写」。

**`swap` 与 `as_ptr`。** `swap`（第 200–202 行）只是把工作转交给自由函数 `atomic_swap`；`as_ptr`（第 215–218 行）返回内部数据的裸指针，是底层所有原子操作的公共入口（`UnsafeCell::get().cast::<T>()`）。

[src/atomic/atomic_cell.rs:200-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L200-L202) — `swap`：委托给 `atomic_swap`。

[src/atomic/atomic_cell.rs:215-218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L215-L218) — `as_ptr`：返回内部裸指针，所有底层原子操作的入口。

**`take`。** 第 235–237 行，位于 `impl<T: Default>` 块，一行实现 `self.swap(Default::default())`——把当前值换出来，原地留下 `T::default()`。

[src/atomic/atomic_cell.rs:235-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L235-L237) — `take`：等价于 `swap(Default::default())`。

**`load` 与固定内存序的「证据」。** `load`（第 252–254 行）委托给 `atomic_load`。真正能验证「load=`Acquire`、store=`Release`」这两条约定的，是底层自由函数：`atomic_load` 的无锁分支在第 1041 行调用 `a.load(Ordering::Acquire)`；`atomic_store` 的无锁分支在第 1080 行调用 `a.store(..., Ordering::Release)`；`atomic_swap` 在第 1099 行用 `Ordering::AcqRel`。**这就是本讲开篇引用的那条文档约定的源码出处。**

[src/atomic/atomic_cell.rs:252-254](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L252-L254) — `load`：委托给 `atomic_load`，仅在 `impl<T: Copy>` 块中提供。

[src/atomic/atomic_cell.rs:1033-1069](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1033-L1069) — `atomic_load` 实现，第 1041 行用 `Ordering::Acquire`（固定内存序的源码证据）。其 fallback 分支用到 SeqLock 的乐观读，机制详讲见 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md)。

[src/atomic/atomic_cell.rs:1075-1088](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1075-L1088) — `atomic_store` 实现，第 1080 行用 `Ordering::Release`。

> 小结：`store` 的「按 `needs_drop` 分流」、`load` 的「`T: Copy`」、以及「内存序写死在自由函数里」——这三件事共同构成了 `AtomicCell` 读写方法的工程取舍。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `store` 在 `Copy` 与非 `Copy` 类型上的行为差异，特别是「非 `Copy` 类型不会泄漏」。

**操作步骤**：

1. 在 4.1.4 的 binary crate 里写（示例代码）：

   ```rust
   use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
   use crossbeam_utils::atomic::AtomicCell;

   static DROPS: AtomicUsize = AtomicUsize::new(0);

   #[derive(Default)]
   struct WithDrop { _data: Box<u32> }   // Box 让 needs_drop::<WithDrop>() == true
   impl Drop for WithDrop {
       fn drop(&mut self) { DROPS.fetch_add(1, SeqCst); }
   }

   fn main() {
       // Copy 类型：直接 store
       let a = AtomicCell::new(7_i32);
       a.store(8);
       assert_eq!(a.load(), 8);

       // 非 Copy 类型：store 内部走 swap 并 drop 旧值
       let b = AtomicCell::new(WithDrop { _data: Box::new(1) });
       b.store(WithDrop::default());     // 旧值应被 drop
       assert_eq!(DROPS.load(SeqCst), 1); // 1 次 drop（旧值），新值尚未 drop

       let _old = b.take();              // take 再换出一个，又 drop 默认占位的那份
       // 进入 main 末尾时，b 与 _old 相继销毁，DROPS 继续增长
   }
   ```

2. 运行并在每步之后打印 `DROPS.load(SeqCst)`：

   ```bash
   cargo run
   ```

**需要观察的现象**：`b.store(...)` 执行后 `DROPS` 立刻变成 1，说明旧值在 `store` 内部就被回收了（正是 `drop(self.swap(val))` 的效果），而不是泄漏到 `b` 销毁时。

**预期结果**：`store` 后 `DROPS == 1`。如果你把 `WithDrop` 改成 `Copy` 类型（例如 `#[derive(Copy)] struct WithDrop;`，`needs_drop` 为假），`store` 会改走直接覆盖分支，旧值就不会在这一步 drop——这印证了源码第 178 行 `if mem::needs_drop::<T>()` 分流的真实效果。

> 待本地验证：精确的 `DROPS` 计数取决于你在程序末尾保留了多少个 `WithDrop` 所有者；关注「`store` 之后 `DROPS` 是否立刻 +1」这一现象即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load` 要求 `T: Copy`，而 `store` 对 `T` 没有任何要求？

> **答案**：`load() -> T` 要把单元里的值**复制一份**返回给调用者——单元本身仍保留这个值，所以必须 `Copy`。而 `store(val: T)` 是把 `val` 的所有权**移入**单元，单元接收后「独占」该值，不需要 `T` 可复制。

**练习 2**：对 `AtomicCell<String>` 调用 `store` 会走哪条分支？为什么不会泄漏？

> **答案**：`String` 不是 `Copy`，`needs_drop::<String>()` 为真，走 `drop(self.swap(val))` 分支。`swap` 把新值换入、把旧值换出来，随后 `drop` 回收旧 `String` 的堆内存。如果走 `Copy` 那条 `atomic_store` 分支，旧 `String` 的指针会被直接覆盖丢失，造成堆内存泄漏——这正是作者用 `needs_drop` 分流的原因。

**练习 3**：`take` 为什么放在 `impl<T: Default>` 而不是 `impl<T>` 里？

> **答案**：`take` 的语义是「取走当前值，原地留下一个默认值」。要留下占位值就必须能凭空产生一个 `T`，即 `T: Default`。没有 `Default` bound 的类型无法 `take`。

---

### 4.3 compare_exchange / fetch_update：CAS 与条件更新

#### 4.3.1 概念说明

`store` 是无条件覆盖。但并发编程里更常见的需求是**条件更新**：「当且仅当当前值等于我期望的 `current` 时，才把它换成 `new`」。这就是 **CAS（Compare-And-Swap）** 操作，几乎所有无锁算法的核心原语。

`AtomicCell` 把它暴露为 `compare_exchange`，并在其上构建了一个更顺手的高层 API `fetch_update`：

- `compare_exchange(current, new)`：底层 CAS。成功返回 `Ok(旧值)`，失败返回 `Err(当前值)`。失败时你能拿到「现在到底变成了什么」，从而决定是否重试。
- `fetch_update(f)`：CAS 循环的封装。传入一个闭包 `f(当前值) -> Option<新值>`：返回 `Some(new)` 就尝试 CAS，CAS 失败就用读回的新当前值再调一次 `f`，直到成功或 `f` 返回 `None`。它把「读 → 计算 → CAS → 重试」的样板代码封装成一行。

#### 4.3.2 核心流程

`fetch_update` 的循环逻辑（伪代码）：

```
prev = load()
loop:
    match f(prev):
        None      -> return Err(prev)            // 调用者放弃更新
        Some(next):
            match compare_exchange(prev, next):
                Ok(_)      -> return Ok(prev)    // 成功
                Err(actual) -> prev = actual; continue   // 被别人抢先，用 actual 重试
```

两个要点：

1. `f` **可能被调用多次**——因为别的线程可能在你 CAS 之前抢先修改，导致失败重试。但 `f` 对最终存进单元的那个值「只生效一次」。
2. 成功时返回的是 **`Ok(prev)`（更新前的旧值）**，不是新值；失败（`f` 返回 `None`）时返回 `Err(prev)`（当时的当前值）。这个返回约定和标准库的 `compare_exchange` 一致，便于你拿到「旧值」做后续计算。

`compare_exchange` 自身有一个常被忽略的细节：它在底层可能因为「语义相等但字节不同」而失败重试（例如 `NaN != NaN` 但字节相同的浮点、或带填充字节的结构体）。本讲只点出这个现象，其 unsafe 安全性论证见 [u5-l1](u5-l1-atomiccell-unsafe-safety.md)。

#### 4.3.3 源码精读

**`compare_exchange`。** 第 276–278 行，位于 `impl<T: Copy + Eq>` 块，委托给自由函数 `atomic_compare_exchange_weak`。注意方法名末尾没有 `_weak`——对外的强 CAS 在内部用的是 `compare_exchange_weak`（允许伪失败），由自由函数在循环里处理重试。

[src/atomic/atomic_cell.rs:276-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L276-L278) — `compare_exchange`：委托给 `atomic_compare_exchange_weak`，要求 `T: Copy + Eq`。

[src/atomic/atomic_cell.rs:1118-1168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1118-L1168) — `atomic_compare_exchange_weak` 实现：成功用 `Ordering::AcqRel`、失败用 `Ordering::Acquire`（第 1133–1134 行），并在「语义相等但字节不等」时重试（第 1140–1151 行）。

**`fetch_update`。** 第 299–312 行，是本节最容易看得见「CAS 循环」样板的代码，建议逐行读：

```rust
pub fn fetch_update<F>(&self, mut f: F) -> Result<T, T>
where F: FnMut(T) -> Option<T> {
    let mut prev = self.load();
    while let Some(next) = f(prev) {
        match self.compare_exchange(prev, next) {
            x @ Ok(_) => return x,
            Err(next_prev) => prev = next_prev,   // 被抢先，用读回的当前值重试
        }
    }
    Err(prev)
}
```

[src/atomic/atomic_cell.rs:299-312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L299-L312) — `fetch_update`：CAS 循环封装，`f` 可能被多次调用，但只生效一次。

`compare_exchange` 的文档注释（第 257–278 行）还给出一个重要的语义保证：**成功返回值里的那个旧值「保证等于 `current`」**。这条保证是 `fetch_update` 能在 `Err(next_prev)` 后直接用 `next_prev` 作为新的 `prev` 继续循环的依据。

[src/atomic/atomic_cell.rs:257-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L257-L278) — `compare_exchange` 文档与签名：成功时旧值保证等于 `current`。

#### 4.3.4 代码实践

**实践目标**：用 `fetch_update` 实现一个线程安全的「只增不减」计数器（`fetch_max` 风格），体会 CAS 循环的用法与「可能多次调用闭包」的事实。

**操作步骤**：

1. 在 binary crate 里写（示例代码）：

   ```rust
   use crossbeam_utils::atomic::AtomicCell;

   fn main() {
       let high = AtomicCell::new(5_i64);

       // 只允许把计数器往大推：传入的候选值若大于当前值，则更新
       let prev = high.fetch_update(|cur| Some(cur.max(9))).unwrap();
       assert_eq!(prev, 5);     // 返回更新前的旧值
       assert_eq!(high.load(), 9);

       // 再试一个比当前小的候选值——更新仍会发生，因为 max(9, 7) == 9 不变
       let _ = high.fetch_update(|cur| Some(cur.max(7))).unwrap();
       assert_eq!(high.load(), 9);

       // 让闭包返回 None 表示放弃
       let r = high.fetch_update(|_| None);
       assert_eq!(r, Err(9));
   }
   ```

2. 运行：

   ```bash
   cargo run
   ```

**需要观察的现象**：`fetch_update` 返回的是**旧值**（`Ok(5)`），不是新值（`9`）；闭包返回 `None` 时得到 `Err(当前值)`。

**预期结果**：三个断言全部通过。单线程下闭包只被调用一次；如果你把它放到多线程竞争场景（见第 5 节综合实践），闭包就可能被多次调用——可以加一个 `&Cell<u32>` 计数验证。

#### 4.3.5 小练习与答案

**练习 1**：`compare_exchange` 要求 `T: Eq`（不是 `PartialEq`）。为什么 CAS 需要「相等」这个概念？

> **答案**：CAS 的语义是「当前值等于 `current` 时才替换」。要判断「等于」，就需要对 `T` 做相等性比较，所以需要 `Eq`。注意它在底层比较的是字节（`atomic_compare_exchange_weak` 用原生 CAS 比较比特），但对外用 `T::eq` 来处理「语义相等而字节不同」的重试情形（第 1140 行），因此仍需 `T: Eq`。

**练习 2**：`fetch_update` 的闭包 `f` 在多线程高竞争下可能被调用很多次。这对 `f` 的实现有什么要求？

> **答案**：`f` 必须是**无副作用或幂等**的——因为它可能被重复求值，但只有最后一次成功的 `Some(next)` 真正写进单元。不能在 `f` 里做「累加全局变量」「发起网络请求」等只能执行一次的操作。

**练习 3**：`compare_exchange` 内部为什么用 `compare_exchange_weak`（允许伪失败）而不是强版本？

> **答案**：因为 `compare_exchange` 对外承诺的是「成功或返回真实当前值」，而调用者通常会把 CAS 放在循环里（`fetch_update` 正是这么做的）。`weak` 版本允许「值其实相等却报告失败」的伪失败，但在循环场景下这只是一次额外重试，不影响正确性，却能在某些架构（如 ARM 的 `LDREX/STREX`）上生成更高效的指令序列。`atomic_compare_exchange_weak` 在第 1129–1152 行的 `loop` 里消化了这种伪失败。

---

### 4.4 Drop、needs_drop 与 MaybeUninit 的三角关系

#### 4.4.1 概念说明

这是本讲学习目标里特别要求厘清的一点：**`MaybeUninit`、`needs_drop`、`Drop` 三者如何协同，保证非 `Copy` 类型既不泄漏也不 double-drop。**

逻辑链条是这样的：

1. `AtomicCell<T>` 内部是 `UnsafeCell<MaybeUninit<T>>`。`MaybeUninit` **不会自动 drop** 内部的 `T`。
2. 这意味着：如果什么都不做，非 `Copy` 的 `T`（如 `String`、`Box`）在 `AtomicCell` 销毁时会**泄漏**——没人回收它。
3. 所以 `AtomicCell` 必须自己 `impl Drop`：销毁时手动 `drop_in_place` 内部值。
4. 但 `MaybeUninit` 的好处此时显现：因为它不自动 drop，`AtomicCell::drop` 里手动 `drop_in_place` **不会与任何自动 drop 重复**，避免了 double-drop。
5. `needs_drop::<T>()` 是编译期常量：`Copy` 类型（如 `u64`）返回 `false`，于是 `Drop` 实现里**直接跳过** `drop_in_place`，零开销。

#### 4.4.2 核心流程

```
AtomicCell<T> 即将销毁
  └─ Drop::drop(&mut self)
       ├─ if needs_drop::<T>():                       // 编译期已知
       │     unsafe { self.as_ptr().drop_in_place(); } // 手动回收 T
       └─ else:                                        // Copy 类型，什么都不做
            (无开销)
```

`store`（4.2.3）的分流与这里的 `needs_drop` 是**同一个判据**的两处应用：一处管「写入时旧值怎么处理」，一处管「销毁时内部值怎么处理」。

#### 4.4.3 源码精读

**`Drop` 实现。** 第 317–329 行，SAFETY 注释把三条前提写得清清楚楚：

[src/atomic/atomic_cell.rs:317-329](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L317-L329) — `Drop for AtomicCell<T>`：仅当 `needs_drop::<T>()` 时 `drop_in_place`。SAFETY 注释列明「`&mut` 保证无并发」「指针来自有效引用」「`MaybeUninit` 防止 double-drop」。

把本讲里三处用到 `needs_drop` / `MaybeUninit` 的地方汇总成一张「内存安全三角」表：

| 位置 | 代码 | 作用 |
|------|------|------|
| 结构（4.1.3，L47） | `UnsafeCell<MaybeUninit<T>>` | 关闭自动 drop，把回收权交给 `AtomicCell` |
| `store`（4.2.3，L178） | `if mem::needs_drop::<T>()` | 非 `Copy` 写入时换出并回收旧值，防泄漏 |
| `Drop`（本节，L319） | `if mem::needs_drop::<T>()` | 销毁时手动回收内部值，防泄漏；`MaybeUninit` 防 double-drop |

> 这三处共同回答了学习目标里的问题：「`MaybeUninit` 不自动 drop ⇒ 必须自己 `impl Drop`；`needs_drop` 让 `Copy` 类型跳过这一切，零开销。」对每处 unsafe 的逐句论证，留到 [u5-l1](u5-l1-atomiccell-unsafe-safety.md)。

## 5. 综合实践

把本讲的 API（`new`/`store`/`load`/`is_lock_free`）串起来，完成规格里要求的实践：**一个线程安全的「最新值快照」结构**——多个写线程不断 `store`，一个读线程周期性 `load`，验证「读到的总是某个完整写入值，绝不会读到撕裂（torn）值」。

这个练习之所以值得做，是因为我们刻意选了一个**非 lock-free** 的类型，强制 `AtomicCell` 走 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md) 的全局 SeqLock 回退路径——这正是「不撕裂」最有价值、也最值得验证的场景（如果是 `u64` 这种 lock-free 类型，单条原子指令本就不会撕裂，演示意义就弱了）。

**实践目标**：让一个 32 字节的结构体在被多线程并发读写时，读端永远观察到「一致快照」，亲眼体会 `AtomicCell` 对外承诺的原子性。

**操作步骤**：

1. 在 binary crate 里写（示例代码）。类型 `Quad` 共 32 字节，超出任何原生原子类型，因此 `is_lock_free()` 为 `false`，必走全局锁回退：

   ```rust
   use std::sync::Arc;
   use std::thread;
   use std::sync::atomic::{AtomicBool, Ordering::SeqCst};

   use crossbeam_utils::atomic::AtomicCell;

   // 32 字节，任何平台都不是 lock-free，强制走 SeqLock 回退路径。
   // 刻意让四个字段始终相等：读端只要发现它们不全等，就说明发生了"撕裂读"。
   #[derive(Copy, Clone, Eq, PartialEq, Default)]
   struct Quad { a: u64, b: u64, c: u64, d: u64 }

   fn main() {
       assert!(!AtomicCell::<Quad>::is_lock_free());   // 确认走回退路径

       let cell = Arc::new(AtomicCell::new(Quad { a: 0, b: 0, c: 0, d: 0 }));
       let stop = Arc::new(AtomicBool::new(false));

       // 写线程：不断写入 a==b==c==d 的完整值
       let writer = {
           let (cell, stop) = (Arc::clone(&cell), Arc::clone(&stop));
           thread::spawn(move || {
               let mut i: u64 = 0;
               while !stop.load(SeqCst) {
                   i = i.wrapping_add(1);
                   cell.store(Quad { a: i, b: i, c: i, d: i });
               }
           })
       };

       // 读线程：周期性 load，断言永远读到四字段全等
       let reader = {
           let (cell, stop) = (Arc::clone(&cell), Arc::clone(&stop));
           thread::spawn(move || {
               for _ in 0..2_000_000 {
                   let q = cell.load();
                   assert!(q.a == q.b && q.b == q.c && q.c == q.d,
                           "检测到撕裂读：a={} b={} c={} d={}", q.a, q.b, q.c, q.d);
               }
               stop.store(true, SeqCst);
           })
       };

       writer.join().unwrap();
       reader.join().unwrap();
       println!("全部读取一致：AtomicCell 保证了不会读到撕裂值");
   }
   ```

2. 运行：

   ```bash
   cargo run
   ```

**需要观察的现象**：

- 程序正常跑完，**不会**触发 `检测到撕裂读` 的 panic——尽管底层走的是「全局 SeqLock 回退」而非单条原子指令。
- `is_lock_free()` 在开头断言为 `false`（普通 64 位目标、非 sanitizer 下）。

**预期结果**：200 万次读取无一撕裂，最后打印「全部读取一致」。

> 思考题（不必运行）：如果把 `cell.store(...)` 和 `cell.load()` 换成「裸指针 + 普通 `ptr::read`/`ptr::write`」（即不用 `AtomicCell`），上面的 `assert` 早晚会失败——因为 32 字节的读写在 CPU 层面不是原子的，读端会读到「写了一半」的中间状态。`AtomicCell` 之所以能保证不撕裂，靠的正是回退路径里的 SeqLock 印戳机制，这就是 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md) 要拆解的内容。
>
> 待本地验证：在 miri 下运行（`cargo +nightly miri run`）时 `is_lock_free()` 会是 `false`（miri 强制走回退），程序同样应跑通——这恰好顺带验证了回退路径的正确性。关于 miri 强制回退的 cfg，见 [u1-l3](u1-l3-features-build-and-tests.md)。

## 6. 本讲小结

- `AtomicCell<T>` 的内部结构是 `#[repr(transparent)]` 包裹的 `UnsafeCell<MaybeUninit<T>>`：`repr(transparent)` 让它布局等同 `T`（是无锁路径的前提），`UnsafeCell` 提供内部可变性，`MaybeUninit` 把 drop 时机交给 `AtomicCell` 自己。
- 它在 `T: Send` 时声明 `Send + Sync`（要求 `Send` 而非 `Sync`，因为操作转移值而非共享引用），`new` / `into_inner` 都是 `const fn`，可用在 `static` 上下文。
- **方法按 trait bound 分层**：`store`/`swap` 无约束、`take` 要 `Default`、`load` 要 `Copy`、`compare_exchange`/`fetch_update` 要 `Copy + Eq`——看到方法名先想它要求 `T` 满足什么。
- **内存序对外固定**：load = `Acquire`、store = `Release`、读改写 = `AcqRel`，证据在底层自由函数 `atomic_load`(L1041)、`atomic_store`(L1080)、`atomic_swap`(L1099)；`AtomicCell` 因此不暴露 `Ordering` 参数。
- `store` 与 `Drop` 都用 `needs_drop::<T>()` 分流：非 `Copy` 类型在写入时通过 `swap` 回收旧值、在销毁时通过 `drop_in_place` 回收内部值，`Copy` 类型则零开销跳过——`MaybeUninit` 在此保证不会 double-drop。
- `fetch_update` 封装了「读 → 计算 → CAS → 重试」的样板；闭包可能被多次调用但只生效一次；`compare_exchange` 成功返回的旧值保证等于 `current`。

## 7. 下一步学习建议

本讲只讲了 `AtomicCell` 的「对外契约」，刻意回避了底层「它是怎么做到的」。接下来按依赖顺序：

1. **[u2-l2 无锁路径](u2-l2-atomiccell-lockfree.md)**：本讲多次提到的 `repr(transparent)` 究竟怎么用——看 `can_transmute` 常量函数（size 相等、align 不小于）和 `atomic!` 宏如何在编译期把 `AtomicCell<T>` 的指针重解释成原生原子类型、走单条原子指令。
2. **[u2-l3 全局锁回退与 SeqLock](u2-l3-atomiccell-global-lock-seqlock.md)**：本讲综合实践里那个 32 字节、非 lock-free 的 `Quad` 到底靠什么「不撕裂」——看 67 个 `CachePadded<SeqLock>` 组成的全局锁池、印戳（stamp）机制，以及 `atomic_load` fallback 分支里的乐观读 + `read_volatile` + `validate_read`。
3. **[u5-l1 unsafe 深析](u5-l1-atomiccell-unsafe-safety.md)**（专家层，后置）：本讲里一带而过的 `transmute_copy_by_val` const hack、`compare_exchange` 的「语义相等但字节不等」重试、`read_volatile` 读可能未初始化值等 unsafe 语句，在那里集中做安全性论证。

阅读建议：在进入 u2-l2 之前，先回头把本讲的「方法 → impl 块 → trait bound」表和「`needs_drop` 三处应用」表记牢——它们是理解后续无锁路径与回退路径各自负责什么的基础地图。
