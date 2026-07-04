# Deferred：内联或装箱的 FnOnce

## 1. 本讲目标

本讲深入 `crossbeam-epoch` 内部的「延迟闭包容器」`Deferred`，以及承装它的「袋子」`Bag`。读完本讲，你应当能够：

1. 说清楚 `Deferred` 为什么必须是一个**定长（sized）**结构，却能装下任意大小的不定长 `FnOnce()`。
2. 解释 `Deferred` 用「**类型擦除的函数指针 + 一块固定大小的数据缓冲**」来表示闭包的原理，以及 `_marker: PhantomData<*mut ()>` 带来的 `!Send + !Sync` 含义。
3. 掌握 `Deferred::new` 的「小闭包内联 / 大闭包装箱」分支策略，知道阈值 `DATA_WORDS = 3` 的来历。
4. 读懂 `Bag` 的固定容量数组 `MAX_OBJECTS`、`try_push` / `seal` / `Drop` 三件套，以及 `NO_OP` 哨兵的作用。
5. 把 `Deferred` 与上一讲（u3-l10）的 `defer` / `defer_destroy` / `flush` 串成一条完整的「延迟闭包从产生到执行」的数据通路。

## 2. 前置知识

在继续之前，请确认你已经理解上一讲（u3-l10）建立的几件事：

- `Guard::defer(f)` 会把闭包 `f` 包成一个 `Deferred`，推入当前线程的**本地 bag**；bag 满了才推入**全局 queue**，并由 `flush()` 或周期性 `collect()` 真正执行。
- `defer_destroy(ptr)` 其实就是 `defer_unchecked(move || ptr.into_owned())`，本质也是一个延迟闭包。
- 这些闭包「最终会由另一个线程在宽限期之后执行」，因此对 `Send` 有契约要求（`defer_unchecked` 故意放宽 `Send`）。

本讲要回答的核心问题是：**这个「不定大小的延迟闭包」，到底以什么形态存在内存里？** 为什么不直接用 `Box<dyn FnOnce()>`？为什么 bag 里的数组能用 `[Deferred; 64]` 这种定长数组，却装得下千差万别的闭包？

另外需要一点 Rust 基础：

- `FnOnce()` 是**未定大小类型（unsized / DST）**，不能直接放进定长数组。
- 闭包是一个编译器生成的**匿名 sized 类型**，它的大小等于「捕获的所有变量的总大小」。
- `mem::size_of::<T>()` 返回 `T` 的字节数；`mem::align_of::<T>()` 返回对齐字节数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-epoch/src/deferred.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs) | 定义 `Deferred` 类型、`NO_OP` 哨兵、`new()` 的内联/装箱分支与 `call()`，以及一组对照测试。本讲的主角。 |
| [crossbeam-epoch/src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 定义 `Bag`（装 `Deferred` 的袋子）、`SealedBag`（带 epoch 戳的袋子）、`MAX_OBJECTS` 容量常量，以及 `Local::defer` 把闭包喂进 bag 的入口。 |
| [crossbeam-epoch/src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | 公开 API `Guard::defer`，调用 `Deferred::new(move || drop(f()))` 把闭包交给 `Local::defer`。是 `Deferred` 的产生点。 |

---

## 4. 核心概念与源码讲解

### 4.1 Deferred 的字段布局与 `NO_OP` 常量

#### 4.1.1 概念说明

延迟回收的核心动作是「在未来的某个时刻，执行一段闭包」。这段闭包来自千差万别的调用点：

- `defer_destroy(ptr)` 的闭包只捕获一个 `Shared` 指针（一个字）；
- 用户自己 `guard.defer(move || { /* 大数组 */ })` 的闭包可能捕获几十个字。

而承载这些闭包的 `Bag`，内部是一个**定长数组** `[Deferred; MAX_OBJECTS]`。Rust 的定长数组要求元素是 `Sized`，且每个元素大小相同。这就产生了一对矛盾：

> 闭包大小千变万化，但数组元素必须定长且等大。

`crossbeam-epoch` 的解法是**自己手写一个定长的「闭包信封」`Deferred`**，它的体积恒定，内部用一块固定缓冲区装载闭包的数据：小闭包直接塞进缓冲区（内联），大闭包就先 `Box` 装箱上堆，再把那一个指针塞进缓冲区。这样无论闭包多大，`Deferred` 本身永远是「一个函数指针 + 一块固定数据槽」。

这本质上是**手写的类型擦除（type erasure）**：

- 标准库的 `Box<dyn FnOnce()>` 用「胖指针 = 数据指针 + vtable 指针」做擦除，数据一定在堆上；
- `Deferred` 改成「**单态化的函数指针** + **固定数据槽**」，数据**有机会留在原地（栈/数组里）**，从而省掉一次堆分配。

#### 4.1.2 核心流程

`Deferred` 的字段有三块，职责清晰：

```
Deferred {
    call:    unsafe fn(*mut u8),       // ① 知道「怎么执行」的函数指针
    data:    MaybeUninit<[usize;3]>,   // ② 装「闭包数据」的固定缓冲区（3 个字）
    _marker: PhantomData<*mut ()>,     // ③ 标记：!Send + !Sync
}
```

- ① `call`：一个**瘦函数指针**（thin fn pointer），不是 trait object。它由 `Deferred::new::<F>` 在编译期为每种具体闭包类型 `F` **单态化**生成 `call::<F>`，因此它「记住了」`F` 的具体类型——执行时会把 `data` 当成 `F`（或 `Box<F>`）读出来再调用。
- ② `data`：用 `MaybeUninit<[usize; 3]>` 当一块裸字节缓冲。`MaybeUninit` 表示「可能未初始化」，避免编译器自动零填充或自动析构。这块缓冲可能装着内联的 `F`，也可能只装着一个 `Box<F>` 指针。
- ③ `_marker`：`PhantomData<*mut ()>` 让 `Deferred` 在不写 `unsafe impl` 的情况下自动成为 `!Send + !Sync`（因为裸指针既非 `Send` 也非 `Sync`）。这是一个保守默认；真正允许跨线程移动的是外层 `Bag` 的 `unsafe impl Send`（见 4.3）。

而 `NO_OP` 是一个特殊的 `Deferred` 常量：它的 `call` 指向一个什么都不做的函数 `no_op_call`，`data` 未初始化也无妨。它的作用见 4.3——用来填满 `Bag` 的数组、充当安全的占位符。

#### 4.1.3 源码精读

先看常量与类型别名，以及 `Deferred` 结构体本身：

[crossbeam-epoch/src/deferred.rs:9-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L9-L16) — 定义内联缓冲的容量 `DATA_WORDS = 3`（即 3 个 `usize` 字），并把 `Data` 别名为 `[usize; DATA_WORDS]`。注释点明了「3 个字足够大多数场景，例如一个函数指针 + 一个胖指针」。

> 这里的 3 不是随便选的：`defer_destroy(ptr)` 的闭包通常只占一两个字，而常见析构闭包（一个函数指针 / 一个 `Box` 指针 / 一个 `Shared`）都能塞进 3 个字，从而**绝大多数延迟闭包都不必堆分配**。

[crossbeam-epoch/src/deferred.rs:21-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L21-L25) — `Deferred` 结构体的三个字段，与上面的流程图一一对应。注意 `call` 的类型是 `unsafe fn(*mut u8)`：它接收一个 `*mut u8`，由具体的 `call::<F>` 在内部把它 cast 回 `*mut F`。

再看 `NO_OP` 常量：

[crossbeam-epoch/src/deferred.rs:34-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L34-L41) — `NO_OP` 是一个 `const`，它的 `call` 指向局部定义的 `no_op_call(_raw: *mut u8) {}`（什么都不做），`data` 用 `MaybeUninit::uninit()`。因为它是 `const`，所以可以在编译期求值，被用来在 `Bag::default` 里一次性填满整个数组（见 4.3.3）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Deferred` 的体积与它装载的闭包大小**无关**——这正是它能住进定长数组的原因。

**操作步骤**：写一段小程序，用 `size_of` 打印 `Deferred`（被反射为同名结构体）的字节数，并对比不同大小闭包的体积。由于 `Deferred` 在源码里是 `pub(crate)`，我们用一个**等价的示例结构体**来模拟（明确标注为「示例代码」，不是项目原码）：

```rust
// 示例代码：等价复刻 deferred.rs 中 Deferred 的字段布局，仅供观察体积
use std::mem::{self, MaybeUninit};

const DATA_WORDS: usize = 3;
type Data = [usize; DATA_WORDS];

struct Deferred {
    call: unsafe fn(*mut u8),
    data: MaybeUninit<Data>,
    _marker: std::marker::PhantomData<*mut ()>,
}

fn main() {
    println!("size_of::<Deferred>() = {} B", mem::size_of::<Deferred>());
    println!("size_of::<Data>()     = {} B", mem::size_of::<Data>());
}
```

**需要观察的现象**：在 64 位平台上，`size_of::<Deferred>()` 恒为 32 字节（`call` 指针 8 + `data` 24，无额外 `_marker` 占位），与「装的是 1 字节闭包还是 10 KB 闭包」**完全无关**。

**预期结果**：

```
size_of::<Deferred>() = 32 B
size_of::<Data>()     = 24 B
```

> 这说明：`Deferred` 是定长的，所以 `[Deferred; 64]` 这种数组才成立；至于它内部装的小闭包或大闭包（堆指针），都被「归一化」进了那固定 24 字节的 `data` 里。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DATA_WORDS` 从 3 改成 0，`Deferred::new` 会怎么表现？

**答案**：`Data` 变成 `[usize; 0]`（0 字节）。此时 `size_of::<F>() <= 0` 几乎对所有非零大小闭包都不成立，于是 `new` 的内联分支永远走不进去，**所有闭包都会走堆分配分支**（退化成 `Box<F>`）。功能仍正确，但失去「省一次堆分配」的优化。

**练习 2**：`_marker: PhantomData<*mut ()>` 让 `Deferred` 自动成为 `!Send`。但 `Bag` 却能被推入全局队列、由别的线程 drop。这两者矛盾吗？

**答案**：不矛盾。`Deferred` 单独是 `!Send` 是保守默认；外层 `Bag` 在 [crossbeam-epoch/src/internal.rs:78-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L78-L79) 处写了 `unsafe impl Send for Bag`，其安全前提（注释里写明）是「交给另一个线程执行这些函数是安全的」——这正对应 `defer` 要求 `F: Send + 'static`、`defer_unchecked` 由调用方担保安全的契约。即：**约束没有消失，只是从类型系统上移到了 `unsafe` 调用点**。

---

### 4.2 `new()`：内联/堆分配分支与泛型 `call` 函数

#### 4.2.1 概念说明

`Deferred::new::<F>(f)` 是把任意闭包 `f` 装进固定大小信封的「打包机」。它的关键决策是：**这个闭包能不能塞进那 24 字节的缓冲区？**

判据有两点（必须同时满足）：

1. **大小够装**：`size_of::<F>() <= size_of::<Data>()`（即 \( \leq 3W \)，其中 \( W = \text{size\_of}\langle\text{usize}\rangle \)）。
2. **对齐兼容**：`align_of::<F>() <= align_of::<Data>()`（即 \( \leq W \)）。

若都满足，就**内联**：把 `f` 按位写进 `data` 缓冲区，`call` 设为「直接把缓冲区当 `F` 读出并调用」的 `call::<F>`。

否则**装箱**：先 `Box::new(f)` 让闭包上堆，再把那个 `Box<F>` 指针（只有一个字）写进 `data`，`call` 设为「把缓冲区当 `Box<F>` 读出、解引用再调用」的另一版 `call::<F>`。

> 注意两条分支里的函数都叫 `call::<F>`，但它们捕获的「`F`」语义不同：内联版把缓冲区视作 `F` 本体，装箱版把缓冲区视作 `Box<F>`。函数指针的具体实例由编译器单态化区分。

用数学语言描述容量判据（以字为单位）：

\[
C = 3 \cdot W, \qquad W = \text{size\_of}\langle\text{usize}\rangle
\]

\[
\text{inline} \iff \text{size\_of}\langle F\rangle \leq C \;\land\; \text{align\_of}\langle F\rangle \leq W
\]

#### 4.2.2 核心流程

`Deferred::new(f: F)` 的决策流程（伪代码）：

```
Deferred::new(f: F):
  size  = size_of::<F>()
  align = align_of::<F>()
  if size <= 24 且 align <= 8:            // 【内联分支】
      data = MaybeUninit::<[usize;3]>::uninit()
      ptr::write(data.as_mut_ptr() as *mut F, f)   // 把 f 按位塞进缓冲区
      return Deferred { call: call_inline::<F>, data, marker }

  else:                                    // 【装箱分支】
      b    = Box::new(f)                   // 一次堆分配
      data = MaybeUninit::<[usize;3]>::uninit()
      ptr::write(data.as_mut_ptr() as *mut Box<F>, b)  // 只塞一个指针
      return Deferred { call: call_boxed::<F>, data, marker }
```

执行（`call`）时：

```
Deferred::call(self):
  fp = self.call                            // 取出函数指针
  fp(self.data.as_mut_ptr() as *mut u8)     // 把缓冲区首地址传给它
    │
    ├─ call_inline::<F>(raw): f = ptr::read(raw as *mut F);   f()
    └─ call_boxed::<F>(raw): b = ptr::read(raw as *mut Box<F>); (*b)()
```

关键技巧在于 **`ptr::read`**：它做一次「按位拷贝出来」的 move，把闭包从缓冲区里「拿走」。拿走之后 `data` 在逻辑上已未初始化，但 `MaybeUninit` 没有 `Drop`，所以 `Deferred` 自身被 consume 时不会再去碰那块缓冲，**不会双重释放**。

#### 4.2.3 源码精读

`new` 的完整实现与两个分支：

[crossbeam-epoch/src/deferred.rs:44-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L44-L82) — 整个 `new` 方法。开头先取 `size` 与 `align`（第 45-46 行），随后第 49 行正是容量判据：`if size <= mem::size_of::<Data>() && align <= mem::align_of::<Data>()`。

[crossbeam-epoch/src/deferred.rs:49-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L49-L62) — **内联分支**。第 51 行 `ptr::write(data.as_mut_ptr().cast::<F>(), f)` 把闭包按位写进缓冲区；第 53-56 行定义局部泛型函数 `call<F>`，它用 `ptr::read` 把闭包读回来再 `f()` 调用。注意 `call` 既是局部 fn 又被取地址赋给 `Deferred.call`，编译器会为每个 `F` 单态化出一份。

[crossbeam-epoch/src/deferred.rs:63-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L63-L80) — **装箱分支**。第 64 行 `let b: Box<F> = Box::new(f)` 先上堆；第 66 行把这个 `Box<F>`（一个指针）写进缓冲区；第 68-73 行的 `call<F>` 把缓冲区当作 `*mut Box<F>` 读出，`(*b)()` 调用。注释明确说明了把 `raw` 从 `*mut u8` cast 回 `*mut Box<F>` 是安全的，因为 `raw` 本就源自 `*mut Box<F>`。

执行入口 `call`：

[crossbeam-epoch/src/deferred.rs:85-89](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L85-L89) — `pub(crate) fn call(mut self)`：先把函数指针拷出来（第 87 行），再用裸指针把缓冲区首地址传给它（第 88 行，`cast::<u8>()`）。`mut self` 表示消费式调用——`Deferred` 一旦被 `call`，其所有权即交出，缓冲区由被调用的 `call::<F>` 通过 `ptr::read` 负责「搬空」。

最后看一眼**仓库自带的对照测试**，它们正是本讲「代码实践」的依据：

[crossbeam-epoch/src/deferred.rs:103-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L103-L131) — `on_stack` 测试闭包捕获 `[usize; 1]`（小，走内联），`on_heap` 测试闭包捕获 `[usize; 10]`（大，走装箱）。两者都断言闭包确实被 `call()` 触发（`fired` 由 false 变 true），但测试**不直接断言走哪个分支**——这正是下一小节实践要补上的验证。

#### 4.2.4 代码实践

**实践目标**：用 `size_of` 验证「捕获 `[usize; 1]` 的闭包走内联、捕获 `[usize; 10]` 的闭包走堆」，从而确认你对 `Deferred::new` 第 49 行判据的理解。

**操作步骤**：写一个 helper 测量闭包捕获环境的字节数，再对照内联阈值 24 字节（`size_of::<[usize; 3]>()`）。

```rust
// 示例代码：实验性 helper，测量闭包捕获环境占用的字节数
use std::{cell::Cell, convert::identity, mem};

/// F 是编译器为闭包生成的匿名 sized 类型。
/// size_of::<F>() 正好等于「该闭包捕获的所有变量的总大小」。
fn closure_size<F: FnOnce()>(_: &F) -> usize {
    mem::size_of::<F>()
}

fn main() {
    // 对照 deferred.rs 的 on_stack 测试：捕获 [usize;1] + 一个 &Cell
    let fired = Cell::new(false);
    let a = [0usize; 1];
    let f_small = move || {
        let _ = identity(a);
        fired.set(true);
    };

    // 对照 deferred.rs 的 on_heap 测试：捕获 [usize;10] + 一个 &Cell
    let fired = Cell::new(false);
    let a = [0usize; 10];
    let f_big = move || {
        let _ = identity(a);
        fired.set(true);
    };

    let size_small = closure_size(&f_small);
    let size_big = closure_size(&f_big);
    let inline_cap = mem::size_of::<[usize; 3]>(); // = DATA_WORDS * size_of::<usize>()

    println!("small closure = {} B", size_small);
    println!("big   closure = {} B", size_big);
    println!("inline buffer = {} B (3 * usize)", inline_cap);

    assert!(size_small <= inline_cap, "小闭包应能内联");
    assert!(size_big > inline_cap, "大闭包应装箱上堆");
    println!("结论：small -> 内联分支；big -> 装箱分支（与 Deferred::new 一致）");
}
```

**需要观察的现象**：在 64 位平台上会看到 `small closure = 16 B`（`[usize;1]` 占 8 字节 + `&Cell` 引用占 8 字节），`big closure = 88 B`（`[usize;10]` 占 80 字节 + `&Cell` 占 8 字节），`inline buffer = 24 B`。

**预期结果**：`16 <= 24` 成立 → 小闭包走内联；`88 > 24` → 大闭包走装箱。两条断言通过，与 `Deferred::new` 的分支选择完全一致。

> **待本地验证**：不同平台 / 指针宽度下，`size_small`、`size_big` 的具体字节数会变（32 位平台指针为 4 字节），但「`inline_cap = 3 × size_of::<usize>()`」这个阈值也随平台同步缩放，所以**结论（小内联、大装箱）稳定成立**。请在本机跑一次记录实际数值。

#### 4.2.5 小练习与答案

**练习 1**：`Deferred` 自己**没有实现 `Drop`**。那闭包里捕获的对象（比如一个 `String`）什么时候被释放？

**答案**：在 `Deferred::call` 时。`call::<F>` 用 `ptr::read` 把闭包 move 出来并 `f()` 执行；闭包在执行完后作为普通局部变量到达作用域末尾被正常 drop，此时它捕获的 `String` 等一并释放。装箱分支则是先 `(*b)()` 调用，`Box<F>` 在 `call` 结束后 drop，同样释放。所以**析构责任挂在「执行闭包」这一刻，而不是 `Deferred` 的 Drop 上**——这也解释了为什么必须有人（`Bag::drop` 或 `flush`）去调用 `call`。

**练习 2**：为什么两条分支都用 `ptr::write` 写入、用 `ptr::read` 读出，而不是直接赋值？

**答案**：因为 `data` 的类型是 `MaybeUninit<[usize;3]>`，编译器认为它「可能未初始化」，不能直接 `*ptr = f`（会对未初始化内存做读改写，UB）。`ptr::write` 是「无条件按位写入」、`ptr::read` 是「按位读出（move）」，两者都不触碰目标原有的值，是对 `MaybeUninit` 缓冲区做初始化与搬移的正确原语。

---

### 4.3 `Bag`：固定容量的延迟函数队列

#### 4.3.1 概念说明

`Deferred` 解决了「单个闭包怎么存」，`Bag` 解决「**一批闭包怎么攒着**」。上一讲（u3-l10）讲过：每次 `defer` 都立刻把闭包推入全局队列会带来昂贵的同步开销，所以每个线程先攒在自己的**本地 bag** 里，攒满了再一次性入队。`Bag` 就是这个「攒」的容器。

`Bag` 的设计很直接：一个定长数组 `deferreds: [Deferred; MAX_OBJECTS]` 加一个长度 `len`。`MAX_OBJECTS` 是容量上限：

- 正常构建：`MAX_OBJECTS = 64`；
- 在 `miri` 或 thread-sanitizer 下：`MAX_OBJECTS = 4`，**故意把容量调小**，让 bag 更频繁地满、更频繁地入队，从而更容易暴露数据竞争。

#### 4.3.2 核心流程

围绕 `Bag` 的三条主链路：

```
① 入袋：Local::defer(deferred, guard)
   loop:
     match bag.try_push(deferred):
       Ok(())   -> 完成
       Err(d)   -> bag 满了：
                     Global::push_bag(bag, guard)  // 用当前 epoch 封箱入队，并换一个新空 bag
                     deferred = d                  // 把没塞进去的那个重试

② 封箱：Global::push_bag(bag, guard)
   old = mem::replace(bag, Bag::new())   // 取走满 bag，留一个空的
   atomic::fence(SeqCst)                 // 保证此前写入对回收线程可见
   epoch = global.epoch.load(Relaxed)
   queue.push(old.seal(epoch), guard)    // 盖戳入队

③ 执行：Bag::drop  （在某个宽限期后，由 collect 取出并 drop 触发）
   for i in 0..self.len:
     owned = mem::replace(self.deferreds[i], Deferred::NO_OP)
     owned.call()                        // 真正执行那条延迟闭包
```

`seal` 把 `Bag` 和一个 epoch 绑成 `SealedBag`，回收线程用 `SealedBag::is_expired(global_epoch)`（即 `global_epoch - sealed_epoch >= 2`，宽限期判据）判断它是否安全可销毁——这部分细节属于 u4-l16 / u5-l19 的范围，本讲只需知道「`Bag` 入队前要 `seal` 盖戳」。

#### 4.3.3 源码精读

容量常量与 `Bag` 结构：

[crossbeam-epoch/src/internal.rs:64-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L64-L69) — `MAX_OBJECTS`：默认 64，`crossbeam_sanitize` / `miri` 下为 4（注释点明「让潜在数据竞争更容易触发」）。

[crossbeam-epoch/src/internal.rs:71-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L71-L76) — `Bag` 结构：`deferreds: [Deferred; MAX_OBJECTS]` 定长数组 + `len: usize`。正因为 `Deferred` 是定长的（4.1.4 实践验证），这个数组才成立。

[crossbeam-epoch/src/internal.rs:78-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L78-L79) — `unsafe impl Send for Bag`，安全前提是「这些函数交给别的线程执行是安全的」（对应 `defer`/`defer_unchecked` 的契约）。这让满载的 `Bag` 能从本地线程搬到全局队列、再被任意回收线程 drop 执行。

`try_push` 与 `seal`：

[crossbeam-epoch/src/internal.rs:92-108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L92-L108) — `try_push`：`len < MAX_OBJECTS` 就 `deferreds[self.len] = deferred; self.len += 1` 并返回 `Ok(()))`；否则原样把 `deferred` 用 `Err` 还回去（让调用方换 bag 重试，见 `Local::defer` 的循环）。`# Safety` 注释复述了「可由他线程执行」的前提。

[crossbeam-epoch/src/internal.rs:110-113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L110-L113) — `seal(self, epoch)`：消耗 `Bag`，包成 `SealedBag { epoch, _bag: self }`。

`Default`（用 `NO_OP` 填满数组）与 `Drop`（执行全部闭包）：

[crossbeam-epoch/src/internal.rs:116-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L116-L123) — `Default` 把 `deferreds` 初始化为 `[Deferred::NO_OP; MAX_OBJECTS]`。这就是 `NO_OP` 哨兵的核心用途：**定长数组必须每个元素都初始化**，而 `NO_OP.call` 是空操作，即使误调用也安全。

[crossbeam-epoch/src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134) — `Drop`：遍历 `self.deferreds[..self.len]`（注意只到 `len`，不碰尾部那些 `NO_OP` 占位），用 `mem::replace(deferred, no_op)` 取出每条 `Deferred` 并 `.call()`。`replace` 成 `NO_OP` 是防御性的：保证每条闭包**只执行一次**，即便 `Bag` 因 panic 等原因被重复 drop 也不会双重调用。

最后把入口接上：从 `Guard::defer` 到 `Bag::try_push` 的完整调用链。

[crossbeam-epoch/src/guard.rs:195](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L195) — 公开 API `Guard::defer` 的内部实现：`local.defer(Deferred::new(move || drop(f())), self)`。即先把闭包用 `Deferred::new` 打包（4.2），再交给 `Local::defer`。

[crossbeam-epoch/src/internal.rs:377-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L377-L389) — `Local::defer`：取出本地 bag，循环 `bag.try_push(deferred)`；只要返回 `Err(d)`（bag 满），就 `Global::push_bag(bag, guard)` 把满 bag 封箱入队、换新空 bag，然后拿 `d` 重试。这是「本地缓冲 + 满则入队」策略的精确落点。

#### 4.3.4 代码实践

**实践目标**：以「源码阅读 + 跑仓库自带测试」的方式，验证 `try_push` 在 bag 满时正确返回 `Err`、且 `Bag::drop` 会执行全部已入袋的闭包。

**操作步骤**：

1. 打开 [crossbeam-epoch/src/internal.rs:612-635](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L612-L635) 的 `check_bag` 测试，通读它：先 `MAX_OBJECTS` 次成功 `try_push`（每次断言 `FLAG` 仍为 0，说明**入袋并不执行**），再第 `MAX_OBJECTS + 1` 次断言 `try_push` 返回 `Err`，最后 `drop(bag)` 后断言 `FLAG == MAX_OBJECTS`（说明 **drop 时才统一执行**）。
2. 在 `crossbeam-epoch` 目录下运行该测试：

   ```bash
   cargo test --features std check_bag -- --nocapture
   ```

3. 再运行 `deferred.rs` 里的对照测试：

   ```bash
   cargo test --features std on_stack on_heap string -- --nocapture
   ```

**需要观察的现象**：`check_bag` 通过；在 `drop(bag)` 之前 `FLAG` 一直是 0，`drop` 之后立刻跳到 `MAX_OBJECTS`。`on_stack` / `on_heap` 均通过。

**预期结果**：两个测试都 `ok`。这印证了三件事——(a) `try_push` 满了会 `Err`；(b) 闭包入袋后**不会立即执行**；(c) `Bag::drop` 是真正的执行时机。

> **待本地验证**：若想亲眼看「满则入队」的换 bag 行为，可在 `Local::defer`（internal.rs:385 附近）临时加一行 `eprintln!("bag full, pushing to global queue")` 的日志（仅本地实验，不要提交），再跑一个高频 `defer` 的例子观察打印频率。

#### 4.3.5 小练习与答案

**练习 1**：`Bag::drop` 为什么遍历范围是 `[..self.len]` 而不是整个数组？那些尾部元素是什么？

**答案**：尾部（`[len..]`）的元素是 `Default` 初始化时填入的 `Deferred::NO_OP` 占位（见 internal.rs:120）。它们不是真实闭包，`NO_OP.call` 是空操作。遍历到 `len` 为止既省事又语义清晰；即便不小心多遍历也不会出事（`NO_OP` 安全），但代码只取 `[..len]` 以表达「只有这些是真实入袋的」。

**练习 2**：`Bag::try_push` 返回 `Result<(), Deferred>`——为什么把「失败的 `Deferred`」**原样还回来**，而不是丢弃或 panic？

**答案**：因为那条闭包还没被执行过，丢了就等于漏掉一次析构（内存泄漏 / 资源未释放）。把它原样还给调用方，让 `Local::defer` 的循环拿去塞进**换好的新空 bag**（internal.rs:385-388），保证「每条 `defer` 的闭包最终都会被执行，且仅一次」。

**练习 3**：`Local::defer` 用 `while let Err(d)` 循环重试。有没有可能这个循环永远不终止？

**答案**：不会。每次循环要么 `try_push` 成功（结束），要么 bag 满了——此时 `push_bag` 会用 `mem::replace` 换一个**空 bag** 进来（internal.rs:192），于是下一次 `try_push` 必然面对一个 `len == 0` 的空 bag，必然成功。所以每条 `deferred` 最多经历「一次失败 + 一次成功」，循环最多两轮即终止。

---

## 5. 综合实践

把本讲的三块内容（`Deferred` 字段布局、`new` 的内联/装箱、`Bag` 的入袋与执行）串起来，完成一个**端到端的数据通路追踪**：

**任务**：在 `crossbeam-epoch` 目录下编写一个临时二进制（或 test），用默认收集器做一次完整的延迟回收，并在关键节点加观察。

```rust
// 示例代码：可放在 examples/defer_trace.rs 里，用 cargo run --example defer_trace 运行
use crossbeam_epoch as epoch;
use std::sync::atomic::{AtomicUsize, Ordering};

static DROP_COUNT: AtomicUsize = AtomicUsize::new(0);

struct Tracked(u32);
impl Drop for Tracked {
    fn drop(&mut self) {
        DROP_COUNT.fetch_add(1, Ordering::SeqCst);
        println!("[drop] Tracked({}) 被回收", self.0);
    }
}

fn main() {
    {
        let a = epoch::Atomic::new(Tracked(1));
        let guard = &epoch::pin();
        // load 出 Shared，swap 替换，把旧值交给 defer_destroy（内部即一条延迟闭包）
        let old = a.swap(epoch::Owned::new(Tracked(2)), Ordering::SeqCst, guard);
        unsafe { guard.defer_destroy(old); }
        println!("[main] 已 swap 并 defer_destroy，此刻 DROP_COUNT = {}", DROP_COUNT.load(Ordering::SeqCst));
        // 主动 flush，把本地 bag 推入全局队列并尝试回收
        guard.flush();
        println!("[main] flush 后 DROP_COUNT = {}", DROP_COUNT.load(Ordering::SeqCst));
    } // guard drop -> unpin

    // 宽限期（全局 epoch 前进 >= 2）可能需要若干次 pin 才能推进并回收
    for _ in 0..1000 {
        let _g = epoch::pin();
    }
    println!("[main] 若干次 pin 后 DROP_COUNT = {}", DROP_COUNT.load(Ordering::SeqCst));
}
```

**操作步骤**：

1. 把上面这段示例代码放入 `crossbeam-epoch/examples/defer_trace.rs`（仅本地实验）。
2. 运行 `cargo run --example defer_trace --features std`。
3. 对照本讲源码，标注每一次 `defer_destroy` 在数据通路上的位置：
   - `guard.defer_destroy(old)` →（guard.rs:195）`Deferred::new(move || ptr.into_owned())` →（internal.rs:382）`Local::defer` →（internal.rs:385）`Bag::try_push`。
4. 回答：那条 `move || ptr.into_owned()` 闭包，按 4.2 的判据，**大概率走内联还是装箱**？为什么？（提示：它只捕获一个 `Shared`，即一个带 tag 的字。）

**预期结果**：

- `[main] 已 swap ...` 时 `DROP_COUNT` 多半仍是 0（闭包刚入袋，未执行）。
- `flush` 后可能仍为 0（宽限期未必已满），但 `drop(Tracked)` 最终一定会在某次 `pin` 后被触发，`DROP_COUNT` 最终变为 2（`Tracked(1)` 被 defer_destroy，`Tracked(2)` 在 `Atomic` 离开作用域后经 `into_owned` / Drop 回收——具体路径见 u2/u3 相关讲义）。

> **待本地验证**：宽限期何时满足取决于 pin 频率与 `PINNINGS_BETWEEN_COLLECT`（每 128 次 pin 触发一次 collect），所以「第几次 pin 后 `DROP_COUNT` 跳变」在不同机器上不稳定，这本身就是一个观察点。本任务重点是把「延迟闭包的产生→入袋→封箱→执行」这条链路和本讲的源码对应起来。

---

## 6. 本讲小结

- `Deferred` 是一个**定长信封**（`call: unsafe fn(*mut u8)` + `data: MaybeUninit<[usize;3]>` + `PhantomData<*mut ()>`），用「**单态化函数指针 + 固定数据缓冲**」手写实现类型擦除，因此能装进 `[Deferred; N]` 这种定长数组。
- `_marker: PhantomData<*mut ()>` 让 `Deferred` 默认 `!Send + !Sync`；真正允许跨线程移动的是外层 `Bag` 的 `unsafe impl Send`，安全前提由 `defer`/`defer_unchecked` 的契约担保。
- `Deferred::new` 用判据 `size_of::<F>() <= 24 且 align_of::<F>() <= 8` 决定**内联**（闭包按位塞进缓冲）还是**装箱**（`Box::new(f)` 后只塞一个指针），`DATA_WORDS = 3` 是为「常见析构闭包不超过 3 个字」做的经验权衡。
- `call::<F>` 用 `ptr::read` 把闭包从缓冲区搬出并执行；`Deferred` 自身无 `Drop`，析构责任挂在「执行闭包」那一刻。
- `NO_OP` 哨兵（`call` 指向空函数）用于填满 `Bag` 的定长数组，并在 `Bag::drop` 的 `mem::replace` 里充当安全占位，保证每条闭包**只执行一次**。
- `Bag` = `[Deferred; MAX_OBJECTS]` + `len`（默认 64，sanitizer/miri 下为 4），通过 `try_push`（满则 `Err` 还回）/ `seal`（盖 epoch 戳）/ `Drop`（统一执行）三件套，配合 `Local::defer` 的「满则 `push_bag` 换新 bag」循环，实现「本地攒、满则入队、宽限期后执行」的延迟回收通路。

## 7. 下一步学习建议

本讲把「延迟闭包如何存储、如何被攒批执行」讲透了，但还留了两条线索给后续：

1. **`SealedBag` 与宽限期判据**：`seal(epoch)` 盖的戳如何与全局 epoch 比较、`is_expired` 为什么要求 `global_epoch - sealed_epoch >= 2`——这是 u4-l16（Global 与 Bag 队列）和 u5-l19（try_advance 与 collect）的内容。
2. **内存屏障与 epoch 推进**：`push_bag` 里的 `atomic::fence(SeqCst)`、`pin` 时的屏障，以及为什么屏障必须在写完 local epoch 之后——见 u5-l18（pin/unpin 与内存屏障）。

建议下一讲先读 **u3-l12（repin 与 repin_after）**，它继续在 `Guard`/`Local` 层面讨论「长期持有 guard 如何拖慢 epoch 推进」，与本讲的 `Bag` 入队时机紧密相关；之后再进入 u4 单元的 `Collector`/`Global` 全景。
