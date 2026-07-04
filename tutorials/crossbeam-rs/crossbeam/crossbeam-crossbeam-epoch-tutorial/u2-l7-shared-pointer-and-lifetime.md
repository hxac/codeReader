# Shared：受 epoch 保护的指针与生命周期 'g

## 1. 本讲目标

本讲聚焦「指针三剑客」的第三位成员 —— `Shared<'g, T>`。它是读者在并发数据结构里**最常直接打交道**的指针：每一次 `Atomic::load`、每一次成功的 `compare_exchange`，返回的都是一个 `Shared`。

读完本讲，你应该能够：

1. 说清楚 `Shared<'g, T>` 里的生命周期 `'g` 到底约束了什么、它与 `Guard` 如何绑定、为什么这层绑定能防止 use-after-free。
2. 区分 `as_ref`（安全判空）与 `deref`（直接取值）的差别，并说清楚它们 `unsafe` 的两重契约：**指针有效性**与**内存序/数据竞争**。
3. 会用 `into_owned` / `try_into_owned` 把一个 `Shared` 升级为独占的 `Owned`，并理解它为什么是 `unsafe` 的（尤其是 `Shared` 是 `Copy` 带来的「双重回收」陷阱）。
4. 会读写 `Shared` 上的 tag 标记位（`tag` / `with_tag`），并对比 `Owned::with_tag`（消费式）与 `Shared::with_tag`（借用式）的差异。

本讲只讲 `Shared` 本身的语义与 API，**不**展开 Guard 内部如何 pin/unpin（那是 u3-l9 的主题），也**不**展开 epoch 如何推进（那是 u5 的主题）。

## 2. 前置知识

### 2.1 Rust 的生命周期参数与结构体

在 Rust 里，结构体可以带生命周期参数，例如 `struct Foo<'a, T> { inner: &'a T }`。编译器据此检查：**`Foo` 实例不能比它借用的 `T` 活得更久**。但生命周期参数并不一定真的出现在某个字段类型里 —— 当我们只想「向借用检查器声明一层借用关系」、却不想真存一个引用时，就用 `PhantomData` 占位。本讲的 `Shared` 用的就是这个技巧。

### 2.2 Guard 是 pin 的「凭证」（承接 u1-l3 / u2-l5）

回顾前几讲：

- `epoch::pin()` 返回一个 [`Guard`](#)，它是「当前线程已 pin」的 RAII 凭证，drop 时自动 unpin。
- `Atomic::load(&Guard)` 必须传一个 `&Guard` 才能调用。
- 在 u2-l5 里我们强调过：`load` 的 `&Guard` 参数**运行时几乎不读 Guard**，它的真正作用是把返回的 `Shared` 的有效期「钉」在 guard 之内。本讲就是把这句话讲透。

### 2.3 数据竞争与内存序（Ordering）

`Shared` 解引用是 `unsafe` 的一个重要原因是**数据竞争**。最小复习：

- `Relaxed`：只保证对该原子变量自身的读写在所有线程间有一致顺序，**不**与其它（包括被指对象的初始化）建立 happens-before 关系。
- `Release`（写端）/ `Acquire`（读端）配对：写端 `Release` 之前的所有写入，对读端 `Acquire` 之后的代码可见。这是跨线程传递「对象已初始化」语义的标准做法。

如果用 `store(..., Relaxed)` 写入一个 `Owned::new(42)`，另一个线程用 `load(Relaxed, guard)` 读出后解引用，那么**指针地址可见 ≠ 指向的 `42` 可见**，这就构成数据竞争（UB）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/atomic.rs` | `Shared<'g, T>` 的定义与几乎全部方法（`null` / `is_null` / `deref` / `as_ref` / `into_owned` / `tag` / `with_tag` 等）；以及 `Atomic::load` 如何把 Guard 的生命周期传给 `Shared`。 |
| `src/guard.rs` | `Guard` 结构本身（本讲只看它的生命周期来源）与 `defer_destroy`（与 `Shared::into_owned` 配合的标准回收路径）。 |

## 4. 核心概念与源码讲解

### 4.1 Shared 的结构与生命周期 'g

#### 4.1.1 概念说明

`Shared<'g, T>` 表示「**从 `Atomic` 里借出来的、受 epoch GC 保护的对象指针**」。它与 `Owned<T>` 的根本区别在于所有权：

- `Owned<T>`：**独占**所有权，drop 时会释放对象（类 `Box`）。
- `Shared<'g, T>`：**没有**所有权，它只是一个「观测窗口」。同一个对象可以同时被多个 `Shared` 指向（多线程并发 `load` 时就是这样），而真正的回收要么靠 `defer_destroy`（延迟到宽限期后），要么靠某个线程把它 `into_owned()` 升级为独占。

`Shared` 的关键字眼是那个生命周期 `'g`：

> 这个指针只在生命周期 `'g` 内有效。

而 `'g` 恰好就是「创建它的那个 `Guard` 的存活期」。换句话说，**`Shared` 不能比生成它的 `Guard` 活得更久**。一旦 Guard 被 drop（线程 unpin），所有从它派生的 `Shared` 在编译层面就失效了，你无法再持有或解引用它们。这是 crossbeam-epoch 用 Rust 类型系统防 use-after-free 的核心机关。

#### 4.1.2 核心流程：load 如何把 Guard 的生命周期「传染」给 Shared

关键在 `Atomic::load` 的签名：

```rust
pub fn load<'g>(&self, order: Ordering, _: &'g Guard) -> Shared<'g, T>
```

注意两点：

1. 参数写作 `_: &'g Guard`（**被忽略**）——运行时根本不读这个 Guard 的内容。
2. 但 `'g` 这个生命周期变量同时出现在「入参的 `&'g Guard`」和「返回值 `Shared<'g, T>`」上。

于是借用检查器推出一条铁律：**返回的 `Shared` 的有效期 ≤ 传入的 `Guard` 的有效期**。Guard 一旦 drop，`Shared` 立即失效。下面看源码。

#### 4.1.3 源码精读

`Shared` 的定义（注意 `data` 字段只是个类型擦除的裸指针，生命周期全靠 `_marker` 表达）：

[src/atomic.rs:1197-1200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1197-L1200) —— `Shared` 结构体定义：内部只有一个 `*mut ()`，生命周期靠 `PhantomData` 表达。

```rust
pub struct Shared<'g, T: 'g + ?Sized + Pointable> {
    data: *mut (),
    _marker: PhantomData<(&'g (), *const T)>,
}
```

`_marker` 是 `PhantomData<(&'g (), *const T)>`，这一对元组向编译器声明了两件事：

- `&'g ()`：假装 `Shared` 借用了一个存活 `'g` 的东西（即 Guard 背后 pin 着的线程状态），从而获得「不能比 `'g` 活更久」的检查。
- `*const T`：假装持有一个 `*const T`，使 `Shared` 在 `T: !Send/!Sync` 时**不**自动变成 `Send/Sync`（裸指针默认 `!Send + !Sync`），避免误把非线程安全的 `T` 跨线程传递。

再看 `load` 如何把 `'g` 接到 `Shared` 上：

[src/atomic.rs:356-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358) —— `Atomic::load`：`&'g Guard` 参数被忽略（`_`），但生命周期 `'g` 流入返回的 `Shared<'g, T>`，这是把指针「钉」在 guard 之内的全部魔法。

```rust
pub fn load<'g>(&self, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    unsafe { Shared::from_ptr(self.data.load(order)) }
}
```

同样的技巧也用在 `Owned::into_shared` 上（把独占指针交给 epoch 保护）：

[src/atomic.rs:1063-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1063-L1065) —— `Owned::into_shared`：同样以 `_: &'g Guard` 把返回值生命周期钉成 `'g`。

```rust
pub fn into_shared<'g>(self, _: &'g Guard) -> Shared<'g, T> {
    unsafe { Shared::from_ptr(self.into_ptr()) }
}
```

> 源码上方的 `#[allow(clippy::needless_lifetimes)]` 也印证了：clippy 觉得这个生命周期参数「多余」（按 elision 规则能省），但作者**故意保留**，正是因为它有教学意义、强调 `'g` 的来源就是 Guard。

#### 4.1.4 Clone / Copy / Pointer：Shared 是廉值的、可复制的

`Shared` 是 `Copy` 的（这是它和 `Owned` 的另一处本质差异）：

[src/atomic.rs:1202-1208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1202-L1208) —— `Clone`/`Copy`：复制一个 `Shared` 只是复制那个裸指针，不增减引用计数、不会双重释放（因为它不拥有对象）。

```rust
impl<T: ?Sized + Pointable> Clone for Shared<'_, T> {
    fn clone(&self) -> Self { *self }
}
impl<T: ?Sized + Pointable> Copy for Shared<'_, T> {}
```

正因为 `Copy`，`Shared` 可以像整数一样随便传来传去、存进数据结构。也正因为不拥有对象，它的复制**不会**触发任何析构 —— 回收必须走显式路径（`defer_destroy` 或 `into_owned`）。

`Shared` 还实现了 sealed trait `Pointer<T>`，使其能作为 `Atomic::store`/`swap`/`compare_exchange` 的入参：

[src/atomic.rs:1211-1224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1211-L1224) —— `Pointer<T>` 实现：`into_ptr` 直接返回 `self.data`（无需 `mem::forget`，因为是 `Copy`），`from_ptr` 重新装回一个 `Shared`。

```rust
impl<T: ?Sized + Pointable> Pointer<T> for Shared<'_, T> {
    #[inline]
    fn into_ptr(self) -> *mut () { self.data }
    #[inline]
    unsafe fn from_ptr(data: *mut ()) -> Self {
        Shared { data, _marker: PhantomData }
    }
}
```

对比 `Owned::into_ptr`（u2-l6 讲过）需要 `mem::forget(self)` 来转移所有权；而 `Shared::into_ptr` 只是把那份 `*mut ()` 拷出来，原 `Shared` 仍然有效（Copy 语义）。这一点会在 4.3 节 `into_owned` 的「陷阱」里再次凸显。

#### 4.1.5 代码实践：用编译器证明 'g 的约束

**实践目标**：亲手触发一次编译错误，直观感受「`Shared` 不能逃出 `Guard` 的作用域」。

**操作步骤**（在你的实验 crate 里，依赖 `crossbeam_epoch`）：

```rust
// 示例代码：尝试让 Shared 逃出 guard 的作用域
use crossbeam_epoch::{self as epoch, Atomic};
use std::sync::atomic::Ordering::SeqCst;

fn leak_shared() -> crossbeam_epoch::Shared<'static, i64> {
    let a = Atomic::new(42_i64);
    let guard = epoch::pin();            // guard 在函数末尾 drop
    let p = a.load(SeqCst, &guard);      // p: Shared<'guard, i64>
    p                                      // ❌ 期望返回 Shared<'static>
}
```

**需要观察的现象**：编译器报类似 `lifetime may not live long enough` / `expected 'static, found anonymous lifetime` 的错误。

**预期结果**：编译失败。把返回类型改成不带生命周期的、或把 guard 提到更外层才能通过。这就是 `'g` 在为你挡下 use-after-free。

> 若不确定本机 Rust 版本的具体报错措辞，可标记「待本地验证」，但**结论（编译失败）是确定的**。

#### 4.1.6 小练习与答案

**练习 1**：`Shared<'g, T>` 里只有一个真实字段 `data: *mut ()`，为什么它还需要 `_marker: PhantomData<(&'g (), *const T)>`？

> **参考答案**：因为裸指针 `*mut ()` 不携带任何生命周期或 `Send/Sync` 信息。`PhantomData` 中的 `&'g ()` 让编译器把 `Shared` 的存活期绑定到 `'g`（即 Guard），`*const T` 让 `Shared` 继承裸指针默认的 `!Send + !Sync`，避免非线程安全的 `T` 被错误跨线程搬运。两者都是「写给类型检查器看」的，不占运行时开销。

**练习 2**：既然 `Shared` 是 `Copy` 的，为什么 `Atomic::clone()` 内部用 `Relaxed` load 不会出问题（提示：结合 u2-l5）？

> **参考答案**：`Atomic::clone` 只是把当前指针值复制到一个新的 `Atomic` 里，它并不涉及被指对象的初始化可见性，也不建立跨线程的 happens-before；如果调用方需要同步语义，应另行用 acquire/release 或 fence。这恰好对应 u2-l5 强调的「`Atomic::clone` 不提供同步」。

---

### 4.2 判空与解引用：as_ref / deref / deref_mut 的 unsafe 契约

#### 4.2.1 概念说明

拿到一个 `Shared` 后，最常做的事就是**读它指向的对象**。库提供了三条路径：

| 方法 | 签名要点 | 是否处理 null | 安全性 |
| --- | --- | --- | --- |
| `as_ref(&self)` | `-> Option<&'g T>` | 是（null 返回 `None`） | `unsafe` |
| `deref(&self)` | `-> &'g T` | 否（null 解引用是 UB） | `unsafe` |
| `deref_mut(&mut self)` | `-> &'g mut T` | 否 | `unsafe`（且要求 `&mut self`） |

这三个方法**全是 `unsafe`**，原因有两层：

1. **指针有效性**：`Shared` 可能指向一个已被回收/从未初始化的地址（尽管 epoch GC + `'g` 约束把风险降到很低，但类型系统无法证明）。
2. **数据竞争**：跨线程读写同一个对象时，若 `store`/`load` 的 `Ordering` 选错（比如都用 `Relaxed`），对象内部数据的初始化可能对读线程不可见，构成数据竞争。

`as_ref` 与 `deref` 的差别**只在于 null 处理**：`as_ref` 先判空再决定是否解引用，是「安全判空」的惯用写法；`deref` 假定非空、直接解引用。源码文档里给出的反例（u2-l5 也提过）正是数据竞争场景。

#### 4.2.2 核心流程

判空与解引用都基于 4.1 的 `decompose_tag`：先把 tag 从 `data` 里剥掉，拿到干净的 `raw` 指针，再调 `Pointable::as_ptr` 取到 `&T`。差别在于：

- `is_null` / `as_ref`：判断 `raw.is_null()`，是则返回 `false` / `None`。
- `deref`：**不判空**，直接 `&*self.as_ptr()`。
- `deref_mut`：调 `as_mut_ptr`（要求 `&mut self`，从而在 Rust 别名规则下保证「同一时刻只有一个 `&mut T`」）。

#### 4.2.3 源码精读

先看两个底层（`pub(crate)`）取指针的函数，它们被 `deref`/`as_ref` 复用：

[src/atomic.rs:1288-1296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1288-L1296) —— `as_ptr` / `as_mut_ptr`：先 `decompose_tag` 剥掉 tag 拿干净 `raw`，再委托 `Pointable::as_ptr` / `as_mut_ptr` 还原出 `*const T` / `*mut T`。

```rust
pub(crate) unsafe fn as_ptr(&self) -> *const T {
    let (raw, _) = decompose_tag::<T>(self.data);
    unsafe { T::as_ptr(raw) }
}
pub(crate) unsafe fn as_mut_ptr(&self) -> *mut T {
    let (raw, _) = decompose_tag::<T>(self.data);
    unsafe { T::as_mut_ptr(raw) }
}
```

`deref`：一行实现，**不判空**，所以对 null 调用是 UB。

[src/atomic.rs:1330-1332](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1330-L1332) —— `deref`：直接 `&*self.as_ptr()`，返回的引用生命周期为 `'g`。

```rust
pub unsafe fn deref(&self) -> &'g T {
    unsafe { &*self.as_ptr() }
}
```

`as_ref`：先剥 tag、判空，null 返回 `None`，否则解引用。这是「安全判空」的推荐入口。

[src/atomic.rs:1407-1414](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1407-L1414) —— `as_ref`：判空后返回 `Option<&'g T>`，是惯用的「先确认非空再读」写法。

```rust
pub unsafe fn as_ref(&self) -> Option<&'g T> {
    let (raw, _) = decompose_tag::<T>(self.data);
    if raw.is_null() {
        None
    } else {
        Some(unsafe { &*T::as_ptr(raw) })
    }
}
```

`deref_mut`：注意它要求 `&mut self`。因为 `Shared` 是 `Copy`，要拿到 `&mut Shared` 必须先有一个 `mut` 绑定，这在 Rust 别名模型下天然保证了「此刻没有并发的可变借用」。但文档依然强调：**它不能阻止其它线程通过另一个 `Shared` 并发读/写同一对象**，所以仍需调用方自己保证无并发访问。

[src/atomic.rs:1371-1373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1371-L1373) —— `deref_mut`：`&mut self` + `as_mut_ptr`，返回 `&'g mut T`。

```rust
pub unsafe fn deref_mut(&mut self) -> &'g mut T {
    unsafe { &mut *self.as_mut_ptr() }
}
```

关于 `unsafe` 契约里「数据竞争」这一条，文档给了精确的反例，务必读懂：

[src/atomic.rs:1306-1314](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1306-L1314) —— `deref` 的 Safety 文档：明确指出 `store(Relaxed)` + `load(Relaxed).as_ref()` 这条路径**无法同步对象初始化**，是数据竞争；正确做法是 `Release`/`Acquire`。

最后看 `null` / `is_null`，它们是构造与判空的基础（`null` 还是 `const fn`，可出现在 `static` 上下文）：

[src/atomic.rs:1261-1266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1261-L1266) 与 [src/atomic.rs:1283-1286](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1283-L1286) —— `null()` 构造空指针；`is_null()` 先剥 tag 再判空（注意是判 `raw` 而非整个 `data`，所以带 tag 的空指针也算空）。

```rust
pub const fn null() -> Self {
    Self { data: ptr::null_mut(), _marker: PhantomData }
}
pub fn is_null(&self) -> bool {
    let (raw, _) = decompose_tag::<T>(self.data);
    raw.is_null()
}
```

> 小细节：`is_null` 判的是剥掉 tag 后的 `raw`，而非 `self.data`。所以一个「地址为 0、tag 非 0」的 `Shared` 仍会被判为空（低位 tag 不算真实地址）。

#### 4.2.4 代码实践：阅读测试里的 deref 用法

**实践目标**：通过现成的单元测试，确认 `deref` 在「确认非空后直接取值」时的典型用法。

**操作步骤**：阅读 `atomic.rs` 内的 `compare_exchange_success` 测试，观察它如何用 `unsafe { result.old.deref() }` 取值并与字面量比较。

[src/atomic.rs:1622-1652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1622-L1652) —— 测试 `compare_exchange_success`：成功后用 `result.old.deref()` 拿到旧值 `&42`、`result.new.deref()` 拿到新值 `&100`，并在最后 `drop(result.old.into_owned())` 回收旧对象。

**需要观察的现象**：测试里 `deref()` 调用都包在 `unsafe {}` 里；回收旧对象用的是 `into_owned()`（4.3 节）。

**预期结果**：`cargo test --lib compare_exchange_success`（在 crossbeam-epoch 包内）通过。若你不在该包内单独运行，可标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`as_ref` 和 `deref` 都返回 `&'g T`，二者的差别只在 null 处理。那为什么库里要同时保留两者，而不是只留 `as_ref`？

> **参考答案**：`as_ref` 返回 `Option<&'g T>`，每次调用都要做一次判空与 `match`/`if let`，对热路径有微小开销；`deref` 在调用方**已确认非空**（例如紧跟在 `!p.is_null()` 之后、或来自 `compare_exchange` 成功分支）时可省掉这一次判空，更贴近「裸指针解引用」的零开销语义。两者是「安全判空」与「零开销直取」的取舍。

**练习 2**：`deref_mut` 为什么要求 `&mut self` 而 `deref` 只需 `&self`？

> **参考答案**：返回 `&mut T` 要求在 Rust 别名模型下保证「当前没有其它 `&T`/`&mut T` 同时存在」，而 `Shared` 是 `Copy`，唯一能借到 `&mut Shared` 的途径是调用方先有一个 `mut` 绑定——这天然排除了「在别处还 `Copy` 出同名 `Shared` 同时读」的部分情形。当然，它无法阻止**其它线程**通过自己的 `Shared` 并发访问，所以文档仍要求调用方自行保证无并发访问。

---

### 4.3 取回所有权：into_owned / try_into_owned

#### 4.3.1 概念说明

`Shared` 没有所有权。但有时候你**确定**当前没有任何其它线程再碰这个对象了（例如：你刚把它从数据结构里摘下来、或处在单线程的析构阶段），想把它的所有权「升级」为一个会自动释放的 `Owned<T>`，这时就用 `into_owned` / `try_into_owned`。

这两个方法都是 `unsafe` 的，且有一个**与 `Copy` 直接相关的陷阱**：

> 因为 `Shared` 是 `Copy`，调用 `into_owned(self)` 只是消费了**一份拷贝**。你完全可以先 `let p = a.load(..); let o = unsafe { p.into_owned() };`，然后**继续使用 `p`** —— 而此时 `p` 指向的内存可能已被 `Owned` 的 drop 释放。这是经典的双重释放 / use-after-free 隐患，类型系统拦不住，只能靠调用方自律（这正是不安全性的来源）。

`into_owned` 与 `try_into_owned` 的差别和 4.2 里的 `deref` / `as_ref` 完全对称：

- `into_owned(self)`：假定非空；null 在 debug 下 panic（`debug_assert!`），release 下是 UB。
- `try_into_owned(self)`：判空，null 返回 `None`。

#### 4.3.2 核心流程

两者都调 `Owned::from_ptr(self.data)` 把那份带 tag 的裸指针包回 `Owned`（注意 tag 会被保留，与 u2-l6 的 `Owned::from_ptr` 一致）。差别只是前置的判空。`Owned` 一旦被 drop，就会执行 `T::drop`（既析构又回收，见 u2-l6 的 `Owned::drop`），于是这条路径成了**确定性回收**（不依赖宽限期）。

它也是 `Guard::defer_destroy` 的底层：`defer_destroy` 实际上是把 `shared.into_owned()` 注册成一个延迟闭包（详见 u3-l10）。

#### 4.3.3 源码精读

[src/atomic.rs:1440-1443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1440-L1443) —— `into_owned`：`debug_assert!` 判空（仅 debug），然后 `Owned::from_ptr(self.data)`（保留 tag）。

```rust
pub unsafe fn into_owned(self) -> Owned<T> {
    debug_assert!(!self.is_null(), "converting a null `Shared` into `Owned`");
    unsafe { Owned::from_ptr(self.data) }
}
```

[src/atomic.rs:1467-1473](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1467-L1473) —— `try_into_owned`：判空后返回 `Option<Owned<T>>`，是「不确定是否非空」时的安全入口。

```rust
pub unsafe fn try_into_owned(self) -> Option<Owned<T>> {
    if self.is_null() {
        None
    } else {
        Some(unsafe { Owned::from_ptr(self.data) })
    }
}
```

与延迟回收的标准入口 `Guard::defer_destroy` 对照（它会最终调到 `into_owned`）：

[src/guard.rs:271-273](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L271-L273) —— `defer_destroy`：把 `ptr.into_owned()` 包成一个 move 闭包延迟执行，本质就是「等宽限期过了再 `into_owned` 并 drop」。

```rust
pub unsafe fn defer_destroy<T: ?Sized + Pointable>(&self, ptr: Shared<'_, T>) {
    unsafe { self.defer_unchecked(move || ptr.into_owned()) }
}
```

> 一句话区分两条回收路径：
> - **现在就确定无人再用** → `unsafe { shared.into_owned() }`，立刻拿到 `Owned`，drop 即回收。
> - **可能有别的线程还在读** → `unsafe { guard.defer_destroy(shared) }`，延迟到宽限期后再回收。

#### 4.3.4 代码实践：用 into_owned 做确定性回收

**实践目标**：写一个最小的「load → into_owned → 自动 drop」单线程闭环，并用带 Drop 计数的类型验证对象确实被释放。

**操作步骤**（示例代码）：

```rust
// 示例代码：验证 into_owned 触发回收
use crossbeam_epoch::{self as epoch, Atomic};
use std::sync::atomic::{AtomicUsize, Ordering};

static DROP_COUNT: AtomicUsize = AtomicUsize::new(0);

struct Tracked(usize);
impl Drop for Tracked {
    fn drop(&mut self) {
        DROP_COUNT.fetch_add(1, Ordering::SeqCst);
    }
}

fn main() {
    let a = Atomic::new(Tracked(42));
    {
        let guard = &epoch::pin();
        let p = a.load(Ordering::SeqCst, guard);
        // 此时确认没有其它线程持有该对象（单线程场景）
        let owned = unsafe { p.into_owned() };
        // owned 离开作用域时 drop -> Tracked::drop 触发
        drop(owned);
    }
    // Atomic 已被「掏空」（对象被 into_owned 带走），不要再 into_owned，否则双重释放
    assert_eq!(DROP_COUNT.load(Ordering::SeqCst), 1);
}
```

**需要观察的现象**：`DROP_COUNT` 最终为 1，证明对象确实被 `into_owned` 后的 drop 回收。

**预期结果**：打印/断言通过。注意此例是单线程；多线程下应改用 `defer_destroy`。

#### 4.3.5 小练习与答案

**练习 1**：下面代码错在哪？

```rust
let p = a.load(SeqCst, guard);
let o = unsafe { p.into_owned() };
println!("{}", unsafe { p.deref().0 }); // 还在用 p
```

> **参考答案**：`Shared` 是 `Copy`，`into_owned(self)` 只消费了一份拷贝，`p` 依然有效。但 `o` 一旦 drop，`p` 指向的内存就被释放了，`p.deref()` 就是 use-after-free。这正是 `into_owned` 标记为 `unsafe` 的根本原因：类型系统无法阻止你「转走所有权后还继续用旧指针」。

**练习 2**：`into_owned` 和 `try_into_owned` 的关系，与 `deref` 和 `as_ref` 的关系有何相似之处？

> **参考答案**：完全对称。`into_owned` / `deref` 假定非空、更直接但 null 时危险（`into_owned` 在 debug 下 panic、`deref` 对 null 是 UB）；`try_into_owned` / `as_ref` 先判空、返回 `Option`，是更稳妥的入口。两者都是「零开销直取」与「安全判空」的取舍。

---

### 4.4 标记位读写：tag / with_tag

#### 4.4.1 概念说明

`Shared` 继承了 u2-l5 讲过的 tagged pointer 机制：地址按 `T::ALIGN` 对齐，低位空闲可塞 tag。`Shared` 提供两个方法读写 tag：

- `tag(&self) -> usize`：读出当前 tag。
- `with_tag(&self, tag) -> Self`：返回一个换了 tag 的新 `Shared`。

这里有个**与 `Owned` 的关键对比**（u2-l6 讲过 `Owned::with_tag` 是 `self` 消费式）：

| 类型 | `with_tag` 签名 | 语义 |
| --- | --- | --- |
| `Owned<T>` | `pub fn with_tag(self, tag) -> Self` | **消费式**（拿走所有权，原变量失效） |
| `Shared<'g, T>` | `pub fn with_tag(&self, tag) -> Self` | **借用式**（因为 `Copy`，原 `Shared` 仍可用） |

`Shared::with_tag` 能用 `&self`，正是因为 `Shared` 是 `Copy`：内部把 `self.data` 复制一份、改个 tag 就返回，原对象原封不动。

tag 的位数仍由 `T::ALIGN` 决定（u2-l5 的 `low_bits`），超出范围的 tag 会被 `tag & low_bits` 静默截断（例如 `i8` 只能存 0）。

#### 4.4.2 核心流程

`tag` 调 `decompose_tag` 取第二返回值；`with_tag` 调 `compose_tag` 在原 `data` 上替换低位，再 `from_ptr` 装回。两者都是 u2-l5 工具函数的薄封装。

#### 4.4.3 源码精读

[src/atomic.rs:1489-1492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1489-L1492) —— `tag`：取 `decompose_tag` 的第二项。

```rust
pub fn tag(&self) -> usize {
    let (_, tag) = decompose_tag::<T>(self.data);
    tag
}
```

[src/atomic.rs:1513-1515](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1513-L1515) —— `with_tag`：`&self` 借用式，返回换了 tag 的新 `Shared`。

```rust
pub fn with_tag(&self, tag: usize) -> Self {
    unsafe { Self::from_ptr(compose_tag::<T>(self.data, tag)) }
}
```

配套的 `as_raw`（`impl<T> Shared<'_, T>` 块，不带 `'g` 约束）返回去 tag 后的 `*const T`，便于和外部裸指针比较：

[src/atomic.rs:1244-1247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1244-L1247) —— `as_raw`：剥掉 tag，返回干净的 `*const T`。

关于「相等」的语义也要留意：`PartialEq`/`Eq` 比较的是整个 `data`（含 tag），所以**指向同一对象但 tag 不同的两个 `Shared` 不相等**：

[src/atomic.rs:1541-1547](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1541-L1547) —— `PartialEq`/`Eq`：比较 `self.data == other.data`，tag 也参与相等判定。

#### 4.4.4 代码实践：tag 的截断与相等语义

**实践目标**：用现成测试验证 tag 的截断与相等语义，复现 u2-l5 的结论在 `Shared` 上同样成立。

**操作步骤**：阅读并理解以下两个测试：

[src/atomic.rs:1596-1604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1596-L1604) —— `valid_tag_i8` 给 `i8` 的 `Shared` 设 tag `0`；`valid_tag_i64` 给 `i64` 设 tag `7`。

```rust
#[test] fn valid_tag_i8()  { Shared::<i8>::null().with_tag(0); }
#[test] fn valid_tag_i64() { Shared::<i64>::null().with_tag(7); }
```

**需要观察的现象 / 预期结果**：

- `i8` 的 `ALIGN = 1`，`low_bits = 0`，任何 tag 与 `0` 按位与都得 `0`，所以只能存 `0`。
- `i64` 的 `ALIGN = 8`，`low_bits = 0b111 = 7`，可存 `0..=7`，设 `7` 合法。

进一步，自己在实验 crate 里验证相等语义（示例代码）：

```rust
// 示例代码：tag 不同的 Shared 不相等
use crossbeam_epoch::Shared;
let base = Shared::<u64>::null();
let a = base.with_tag(1);
let b = base.with_tag(2);
assert_ne!(a, b);          // tag 不同 -> 不等
assert_eq!(a.as_raw(), b.as_raw()); // 剥掉 tag 后地址相同
assert_eq!(a.tag(), 1);
```

> 若未在本机运行，可标记「待本地验证」，但结论由 `PartialEq` 比较整 `data` 直接保证。

#### 4.4.5 小练习与答案

**练习 1**：`Owned::with_tag(self, ..)` 与 `Shared::with_tag(&self, ..)` 一个消费、一个借用，为什么 `Shared` 可以做成借用式？

> **参考答案**：因为 `Shared` 是 `Copy`。`with_tag(&self, ..)` 内部把 `self.data`（一个 `*mut ()`）复制出来改 tag 再返回，不需要「拿走」调用方的任何资源；调用方的原 `Shared` 仍然有效。而 `Owned` 拥有对象、不可 `Copy`，要改 tag 必须消费原值、产出新值。

**练习 2**：`Shared<'g, [MaybeUninit<i32>]>::null().with_tag(3)` 会得到 tag 几？（提示：回顾 u2-l4 数组的 `ALIGN`）

> **参考答案**：`[MaybeUninit<T>]` 的 `Pointable::ALIGN = align_of::<Array<T>>`，即 `Array<T>`（含 `usize` 长度头）的对齐，通常为 `usize` 的对齐（64 位下 8）。于是 `low_bits = 7`，`with_tag(3)` 得到 tag `3`，未被截断。（具体值取决于平台 `usize` 对齐，结论「不被截断」是确定的；若想验证可标记「待本地验证」。）

---

## 5. 综合实践：跨线程 Release/Acquire 的 store+load

把本讲的「生命周期 `'g`」「`as_ref` 判空 + `deref` 取值」「`into_owned` 回收」「Ordering 与数据竞争」串起来，写一个**生产者—消费者**最小例子。

**实践目标**：

1. 用 `Arc<Atomic<i64>>` 在两线程间共享一个原子指针。
2. 生产者用 `Release` 写入一个初始化好的对象；消费者 `pin` 后用 `Acquire` load 出 `Shared`。
3. 消费者先用 `as_ref` 安全判空（轮询直到对象出现），再取值断言。
4. 取到值后用 `into_owned` 做确定性回收，避免泄漏。
5. 解释：把两处 `Ordering` 都改成 `Relaxed` 为什么是数据竞争。

**操作步骤**（示例代码，可直接放入实验 crate 运行）：

```rust
// 示例代码：跨线程 Release/Acquire 读写 Shared
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::thread;

fn main() {
    // 注意：必须用 Arc 共享「同一个 Atomic 存储」。
    // Atomic::clone() 只会复制指针值到独立的新 Atomic，无法跨线程通信。
    let a = Arc::new(Atomic::<i64>::null());

    let producer = {
        let a = Arc::clone(&a);
        thread::spawn(move || {
            // Release：保证 Owned::new(42) 的初始化在 store 之前对其它线程可见
            a.store(Owned::new(42), Ordering::Release);
        })
    };

    let consumer = {
        let a = Arc::clone(&a);
        thread::spawn(move || {
            let guard = &epoch::pin(); // pin 当前线程；Shared 的 'g 即由此而来
            loop {
                // Acquire：与生产者的 Release 配对，确保能看到对象初始化
                let p = a.load(Ordering::Acquire, guard);
                if let Some(val) = unsafe { p.as_ref() } {
                    // as_ref 安全判空成功 -> 取值
                    assert_eq!(*val, 42);
                    // 确认没有其它线程再碰它（生产者已 store 完，所有权早就让出）
                    unsafe { p.into_owned() }; // 回收，避免泄漏
                    return;
                }
                // 还没生产出来，重试（实际项目里可加退避）
            }
        })
    };

    producer.join().unwrap();
    consumer.join().unwrap();
    println!("OK");
}
```

**需要观察的现象**：程序正常打印 `OK`，且无内存泄漏（对象被 `into_owned` 后 drop 回收）。

**预期结果**：通过。可用 `cargo run` 运行。

**思考题（必答）**：若把 `store` 的 `Release` 和 `load` 的 `Acquire` 都换成 `Relaxed`，会发生什么？

> **参考答案**：`Relaxed` 不在生产者「写入 42」与消费者「读到指针」之间建立 happens-before 关系。于是消费者可能**观测到指针地址**（store 生效了），却**观测不到对象内部的 `42`**（初始化尚未对该线程可见），`as_ref().unwrap()` 后读到未初始化/陈旧值 —— 这是典型的数据竞争（UB）。这正是 [src/atomic.rs:1306-1314](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1306-L1314) 文档强调的结论。注意：这种 bug 在 x86 强内存模型下可能长期不暴露，在弱内存模型（ARM/LoongArch 等）或多核交错下才容易复现，因此**绝不能依赖「跑起来没事」来判断正确性**。

> 进阶思考：`epoch::pin()` 只保证「对象不会被 GC 回收」，它**不**提供 store/load 之间的同步；同步完全靠你为 `Atomic` 操作选择的 `Ordering`。pin 与 Ordering 是两件正交的事。

## 6. 本讲小结

- `Shared<'g, T>` 是从 `Atomic` 借出的**无所有权**指针，`'g` 通过 `PhantomData<(&'g (), *const T)>` 绑定到创建它的 `Guard`，编译器据此保证「`Shared` 不能比 Guard 活更久」——这是防 use-after-free 的类型层机关。
- `Atomic::load` 用 `_: &'g Guard` 把生命周期「传染」给返回值：运行时不读 Guard，编译期却把 `Shared` 钉在 guard 之内。
- `Shared` 是 `Clone + Copy`，复制它零开销、不增引用计数、不会双重释放；它还实现了 sealed `Pointer<T>`，可作为 `Atomic` 各写操作的入参。
- 解引用三件套 `as_ref` / `deref` / `deref_mut` 全是 `unsafe`，契约有两层：**指针有效性**与**内存序/数据竞争**（`Relaxed` 的 store+load 无法同步对象初始化，须用 `Release`/`Acquire`）。
- `into_owned` / `try_into_owned` 把 `Shared` 升级为独占 `Owned` 做确定性回收；因 `Shared` 是 `Copy`，存在「转走所有权后还用旧指针」的双重释放陷阱，故为 `unsafe`。
- `tag` / `with_tag` 读写低位标记位；`Shared::with_tag` 是 `&self` 借用式（得益于 `Copy`），与 `Owned::with_tag` 的消费式形成对照；相等比较含 tag，指向同一对象但 tag 不同则不等。

## 7. 下一步学习建议

本讲把 `Shared` 的「读」与「回收」语义讲完了。接下来建议：

1. **u2-l8（CAS 与指针运算）**：`Shared` 是 `compare_exchange` / `fetch_update` 的核心返回类型，下一讲会看到它在循环 CAS 里如何被反复 load、比较、替换，以及 `fetch_and/or/xor` 对 tag 的位运算。
2. **u3-l9（Guard 与 pin 语义）**：本讲反复出现的 `Guard`、`'g` 的「另一端」——Guard 内部如何 pin/unpin、为何 pin 可重入、`unprotected()` 假守卫的用途。
3. **u3-l10（defer / defer_destroy）**：把本讲的 `into_owned` 与 `Guard::defer_destroy` 结合，系统理解「延迟回收」与「宽限期」在 API 层的完整闭环。
