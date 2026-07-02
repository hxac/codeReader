# 线程索引与类型安全

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `ThreadIndex<'kernel, IndexSpace>` 这个「见证类型（witness type）」是什么、它为什么是线程唯一的、为什么不能被伪造或搬运。
- 区分 `Index1D` 与 `Index2D<ROW_STRIDE>` 两套索引空间，并理解「步长写进类型」如何把 stride 用错变成编译期错误。
- 用 `DisjointSlice::get_mut(idx)` 写出**免手写 `if idx < N`、免锁、免同步**的并行写入，并解释它为什么是内存安全的。
- 看懂并复述 cuda-oxide 的「安全模型」：把不变量从「每次访问」推到「构造点」和「宏注入」，从而让安全代码不可能写出数据竞争。

本讲承接 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)（你已经知道 `#[kernel]`/`#[cuda_module]` 如何展开、`thread::index_1d()` 如何出现在 vecadd 里），不重复宏的展开细节。

## 2. 前置知识

在 GPU 上写并行代码，有几件事和单线程 Rust 不同，先建立直觉：

- **线程层次**：一次 kernel 启动会创建一个 *grid*，grid 划分为若干 *block*，每个 block 含若干 *thread*。硬件给每个线程一组只读「特殊寄存器」：`threadIdx.{x,y,z}`（线程在 block 内的坐标）、`blockIdx.{x,y,z}`（block 在 grid 内的坐标）、`blockDim.{x,y,z}`（block 维度）。cuda-oxide 用 `threadIdx_x()` / `blockIdx_x()` / `blockDim_x()` 等函数暴露它们。
- **数据竞争（data race）**：成千上万个线程同时跑同一段代码。如果两个线程同时往**同一个地址**写，就是未定义行为。CUDA C 里这全靠程序员自觉。
- **见证类型 / capability 模式**：一种 Rust 设计技巧——用一个**无法被自由构造**的类型，来「证明」某件事成立。例如「证明我确实通过了越界检查」。`ThreadIndex` 就是这种类型：拿到它，就等于「证明这是硬件分给我的、唯一的线程号」。
- **`PhantomData` 与自动 trait**：Rust 的 `PhantomData<T>` 不占空间，但会告诉编译器「假装我拥有一个 `T`」，从而影响 `Send`/`Sync`/`Copy`/生命周期推导。特别地，`PhantomData<*mut ()>` 会让类型自动变成 `!Send + !Sync`。
- **生命周期与借用检查**：Rust 用生命周期保证引用不会比它指向的数据活得更久。本讲会用 `'kernel` 这个特殊生命周期，把索引「钉」在 kernel 函数体里。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/cuda-device/src/thread.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs) | 定义 `ThreadIndex` 见证类型、`Index1D`/`Index2D`/`Runtime2DIndex` 索引空间、`KernelScope`，以及 `index_1d`/`index_2d`/`index_2d_runtime` 等索引函数。 |
| [crates/cuda-device/src/disjoint.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs) | 定义 `DisjointSlice<T, IndexSpace>`，以及 `get_mut` / `get_mut_indexed` / `get_unchecked_mut` 三种访问方式。 |
| [crates/cuda-device/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/lib.rs) | 设备端 crate 根，`pub use disjoint::DisjointSlice` 与 `pub use thread::*` 把这些类型重新导出给用户。 |
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs) | `#[kernel]`/`#[device]` 宏把用户写的 `thread::index_1d()` 改写成内部真实 intrinsic，并在函数体顶部注入 `KernelScope`。 |
| [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs) | 最小 1D 例子：`thread::index_1d()` + `DisjointSlice::get_mut`。 |
| [crates/rustc-codegen-cuda/examples/index2d_const/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/index2d_const/src/main.rs) | 2D const-stride 例子：`thread::index_2d::<WIDTH>()` + `DisjointSlice<f32, Index2D<WIDTH>>`。 |

> 名词约定：cuda-oxide 把 `thread.rs` 顶部那段「这套类型为什么安全」的长注释称为 **Safety Model**，本讲会反复对照它。

## 4. 核心概念与源码讲解

### 4.1 ThreadIndex：线程唯一的「见证」类型

#### 4.1.1 概念说明

GPU 上每个线程需要一个「我是第几号线程」的编号来做数组寻址。CUDA C 里这只是个普通 `int`，谁都能伪造、谁都能转手——于是数据竞争全靠人。

cuda-oxide 的做法是：**用一个类型来「见证」这个编号来自硬件**。`ThreadIndex` 就是这个见证类型。它的核心承诺是：

1. **它无法被自由构造**——只能由 `index_1d()` / `index_2d::<S>()` / `unsafe index_2d_runtime(s)` 这几个「可信构造器」生产。
2. **这些构造器只读硬件特殊寄存器**（`threadIdx`/`blockIdx`/`blockDim`），它们是启动时由运行时分配的只读值。
3. **每个线程拿到的 `ThreadIndex` 在其索引空间内唯一**——这正是并行写入无竞争的前提。

于是，「我持有一个 `ThreadIndex`」这句话本身，就等于「我持有一个硬件签发的、全局唯一的线程号」。类型系统接管了 CUDA C 里靠自觉的那部分。

#### 4.1.2 核心流程

`ThreadIndex` 的「不可伪造」由三道门共同保证：

```text
用户代码: thread::index_1d()
   │
   │  ① #[kernel] 宏把它改写为 thread::__internal::index_1d(&scope)
   │     （公共的 thread::index_1d 只是 unreachable!() 桩，见 4.1.3）
   ▼
宏在函数体顶部注入: let scope = unsafe { make_kernel_scope() };
   │
   │  ② make_kernel_scope 是 pub unsafe fn，只有宏能调用（人为约定）
   │     它造出唯一一个 KernelScope<'kernel>，其 'kernel 借自一个栈局部
   ▼
__internal::index_1d(scope):
   读 threadIdx_x / blockIdx_x / blockDim_x
   计算 bid*bdim + tid
   调 unsafe ThreadIndex::new(raw, scope)  ③ 唯一的构造入口（私有）
```

关键点：构造器 `ThreadIndex::new` 是**私有**的（不带 `pub`），外部代码碰不到；能碰到的 `__internal::index_*` 又必须吃一个 `KernelScope`，而 `KernelScope` 只能由 `unsafe` 的 `make_kernel_scope` 生产，后者按约定只被宏调用。这就形成了一条「可信链」。

#### 4.1.3 源码精读

先看 `ThreadIndex` 的字段定义，它用三个 `PhantomData` 同时布下三道防线：

[crates/cuda-device/src/thread.rs:L205-L210](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L205-L210) —— `raw` 是真实索引值；`_kernel` 把 `'kernel` 生命周期编进类型；`_space` 编进索引空间；`_not_send_sync: PhantomData<*mut ()>` 让它自动 `!Send + !Sync`。

```rust
pub struct ThreadIndex<'kernel, IndexSpace = Index1D> {
    raw: usize,
    _kernel: PhantomData<fn(&'kernel mut ()) -> &'kernel mut ()>,
    _space: PhantomData<fn() -> IndexSpace>,
    _not_send_sync: PhantomData<*mut ()>,
}
```

注意 `_kernel` 用的是 `fn(&'kernel mut ()) -> &'kernel mut ()` 而不是 `&'kernel ()`。`fn(...)` 包装让 `'kernel` 对类型**不变（invariant）**——既不能被缩短也不能被延长，防止借用检查器通过「缩短生命周期」把索引搬运到更外层作用域。

再看私有构造器和两个公开方法（构造是 `unsafe` 且私有，`get`/`in_bounds` 是只读的）：

[crates/cuda-device/src/thread.rs:L212-L238](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L212-L238) —— 注意 `unsafe fn new` 没有 `pub`，是模块私有；`get()` 把见证「降级」回普通 `usize` 供普通切片寻址使用。

然后是真正的 intrinsic（宏改写后的目标）。这是 1D 索引公式的庐山真面目：

[crates/cuda-device/src/thread.rs:L288-L296](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L288-L296) —— 计算 `blockIdx.x * blockDim.x + threadIdx.x`，即经典的「全局线程号」。

```rust
pub fn index_1d<'kernel>(
    scope: &'kernel KernelScope<'kernel>,
) -> ThreadIndex<'kernel, Index1D> {
    let tid = super::threadIdx_x() as usize;
    let bid = super::blockIdx_x() as usize;
    let bdim = super::blockDim_x() as usize;
    unsafe { ThreadIndex::new(bid * bdim + tid, scope) }
}
```

而用户在 `#[kernel]` 里写的 `thread::index_1d()` 是个**桩（stub）**，直接 `unreachable!`：

[crates/cuda-device/src/thread.rs:L376-L381](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L376-L381) —— 公共桩只为了让 `use` 与路径解析通过；它从不真正执行，因为宏会把调用改写到 `__internal`。

最后看宏这一侧：它把自由函数调用按「路径尾」匹配，重写到 `__internal`，并把 `&scope` 拼进去：

[crates/cuda-macros/src/lib.rs:L2037-L2048](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2037-L2048) —— 改写规则示例：`thread::index_1d()` → `thread::__internal::index_1d(&scope)`。

而 `scope` 这个栈局部，是宏在函数体**最开头**注入的：

[crates/cuda-macros/src/lib.rs:L2192-L2197](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2192-L2197) —— 只有当函数体里确实出现了被改写的索引调用时，才注入这行；`scope` 的 `'kernel` 因此绑定在一个不可能逃出函数体的局部上。

```rust
if rewriter.rewrote_index_call {
    let scope_stmt: Stmt = parse_quote! {
        let #scope_ident = unsafe { ::cuda_device::thread::__internal::make_kernel_scope() };
    };
    input.block.stmts.insert(0, scope_stmt);
}
```

#### 4.1.4 代码实践

**实践目标**：亲眼看一遍「`ThreadIndex` 无法逃出 kernel 函数体」这个约束如何由借用检查器强制。

**操作步骤**：

1. 打开 [examples/vecadd](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs) 里的 vecadd。
2. 在 `mod kernels` 内、`vecadd` 之外，新增一个**普通 `fn`**（不带 `#[device]`）辅助函数，尝试调用 `thread::index_1d()` 并返回它：

```rust
// 示例代码：预期编译通过、但运行时 panic
fn steal_index() -> cuda_device::thread::ThreadIndex<'static> {
    thread::index_1d()   // 期望:这是桩,没被宏改写
}
```

3. 编译（`cargo oxide build vecadd`），观察它**能编译**（因为桩有合法签名），但若在 kernel 里调用它则会 panic——证明公共桩从不真正执行。
4. 再改为：给一个 `#[device]` 辅助函数，让它调用 `thread::index_1d()` 并**返回**结果。

**需要观察的现象**：步骤 3 里，普通 `fn` 的调用不会被宏改写，桩体 `unreachable!()` 原封不动（这与源码注释 [thread.rs:L370-L376](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L370-L376) 一致）；步骤 4 里，因为 `'kernel` 借自该辅助函数的栈局部 `scope`，返回它会触发借用检查报错（约 E0515「cannot return value referencing local」或等价的生命周期错误）。

**预期结果**：你应当能用源码里的话解释「为什么 `#[device]` 函数可以用 `ThreadIndex` 却不能把它返回出去」。具体报错文本**待本地验证**（取决于 nightly 版本与宏注入的精确 span）。

#### 4.1.5 小练习与答案

**练习 1**：`ThreadIndex` 没有显式写 `impl !Send`，为什么它仍是 `!Send`？

> **答案**：因为它有字段 `_not_send_sync: PhantomData<*mut ()>`。裸指针 `*mut ()` 是 `!Send + !Sync` 的，而 `PhantomData<T>` 在自动 trait 推导上等价于「拥有一个 T」，于是整个结构体继承为 `!Send + !Sync`。

**练习 2**：为什么 `_kernel` 用 `PhantomData<fn(&'kernel mut ()) -> &'kernel mut ()>` 而不是 `PhantomData<&'kernel ()>`？

> **答案**：用 `fn(...)` 包装使 `'kernel` 对类型**不变**。若用 `&'kernel ()`，`'kernel` 是协变的，借用检查器可能把短生命周期「拉长」使用，削弱「索引钉在 kernel 体内」的保证；不变性杜绝了这种搬运。

---

### 4.2 Index1D / Index2D：把索引方案编码进类型

#### 4.2.1 概念说明

「线程唯一」只是安全的一半；另一半是「**索引空间要匹配**」。考虑 2D 网格：每个线程有一个 `(row, col)`，线性化成 1D 通常用 `row * stride + col`。如果两个 kernel 用了不同的 `stride`，但索引类型相同，就可能把一个 kernel 的索引喂给另一个 kernel 的切片——地址全错。

cuda-oxide 的解法：**把索引方案本身变成类型参数**。

- `Index1D`：1D 公式 `bid*bdim + tid`。
- `Index2D<const ROW_STRIDE: usize>`：2D 行主序公式，**步长是 const 泛型**，写进类型里。
- `Runtime2DIndex`：运行时步长逃生舱（后面讲）。

于是 `ThreadIndex<'_, Index2D<128>>` 和 `ThreadIndex<'_, Index2D<256>>` 是**不同类型**，把它们喂给同一个 `DisjointSlice` 会编译失败——stride 用错从「运行时算错地址」变成了「编译期类型错误」。

#### 4.2.2 核心流程

三种索引空间的产出公式与「可信来源」：

| 索引空间 | 构造器 | 公式 | 唯一性前提 | 可信级别 |
| --- | --- | --- | --- | --- |
| `Index1D` | `index_1d()` | `blockIdx.x*blockDim.x + threadIdx.x` | 1D 启动（Y/Z 均为 1） | 安全 |
| `Index2D<S>` | `index_2d::<S>()` | `row*S + col`（`col<S` 时 `Some`） | 2D 启动（Z 为 1） | 安全 |
| `Runtime2DIndex` | `unsafe index_2d_runtime(s)` | `row*s + col` | 调用方保证所有线程传同一 `s` | **unsafe** |

`index_2d` 的唯一性有个干净的数学保证：当 `col < ROW_STRIDE` 时，`row*stride + col` 是**单射**的。简要证明（摘自源码注释）：

\[
\text{row}_a \cdot \text{stride} + \text{col}_a = \text{row}_b \cdot \text{stride} + \text{col}_b
\;\Longrightarrow\;
(\text{row}_a - \text{row}_b)\cdot \text{stride} = \text{col}_b - \text{col}_a
\]

由于 \(\text{col}_a, \text{col}_b \in [0, \text{stride})\)，右边落在 \((-\text{stride}, \text{stride})\) 内；而左边是 `stride` 的整数倍，唯一解是 \(\text{row}_a = \text{row}_b\) 且 \(\text{col}_a = \text{col}_b\)。即不同硬件线程在 2D 启动下得到不同 `(row, col)`，线性化后也不同。

> ⚠️ 这两个公式都**忽略 Z 维**。`index_1d` 只读 X 寄存器，`index_2d` 假设 `blockDim.z == gridDim.z == 1`。3D 启动会撞号（issue #115）。目前所有示例都是 1D/2D 启动，所以不受影响——见 [thread.rs:L349-L358](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L349-L358)。

#### 4.2.3 源码精读

三个索引空间都是空的标记枚举（类型层面占位，无运行时数据）：

[crates/cuda-device/src/thread.rs:L51-L55](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L51-L55) —— `Index2D` 带一个 const 泛型 `ROW_STRIDE`，这是「步长进类型」的关键。

```rust
pub enum Index1D {}
pub enum Index2D<const ROW_STRIDE: usize> {}
```

2D intrinsic 在 `col >= ROW_STRIDE` 时返回 `None`，把「越界列」折叠成 `Option`：

[crates/cuda-device/src/thread.rs:L302-L315](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L302-L315) —— 返回类型 `ThreadIndex<'kernel, Index2D<ROW_STRIDE>>`，步长焊死在类型里。

而 `Runtime2DIndex` 是「步长是运行时值」的逃生舱。它的 unsafe 契约写在注释里：类型系统看不出两个线程是否传了同一个 `row_stride`，所以「全员同 stride」只能由 `unsafe` 关键字向调用方索要：

[crates/cuda-device/src/thread.rs:L321-L335](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L321-L335) —— `index_2d_runtime` 返回 `ThreadIndex<'_, Runtime2DIndex>`，与 const 版本**不同类型**，避免两者混用。

最后看 `index2d_const` 示例如何把这套类型对上号：切片声明为 `DisjointSlice<f32, thread::Index2D<WIDTH>>`，索引用 `thread::index_2d::<WIDTH>()`——两边的 `WIDTH` 在类型层完全一致：

[crates/rustc-codegen-cuda/examples/index2d_const/src/main.rs:L17-L34](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/index2d_const/src/main.rs#L17-L34) —— 如果把切片改成 `Index2D<32>` 而索引仍用 `index_2d::<WIDTH>()`（`WIDTH=16`），将得到类型不匹配的编译错误。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次「stride 不匹配 → 编译失败」。

**操作步骤**：

1. 复制 `index2d_const` 示例为新示例（参考 [u1-l3](u1-l3-toolchain-and-cargo-oxide.md) 的 `cargo oxide new`）。
2. 在 kernel 里，把输出切片的类型参数从 `Index2D<WIDTH>` 改成 `Index2D<32>`（一个不同的常量），但 `thread::index_2d::<WIDTH>()` 保持不变。
3. `cargo oxide build <名字>`。

**需要观察的现象**：编译失败，错误指向 `output.get_mut(idx)` 处，提示 `ThreadIndex<'_, Index2D<WIDTH>>` 与期望的 `Index2D<32>` 不匹配。

**预期结果**：你应当得出结论——「stride 用错」在 cuda-oxide 里是**编译期**错误，而非运行时算错地址。具体报错文本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `index_2d` 在 `col >= ROW_STRIDE` 时返回 `None`，而不是直接算一个越界索引？

> **答案**：为了让线性化公式保持单射。如果允许 `col >= stride`，则 `row*stride + col` 不再唯一（例如 `(row=1, col=0)` 与 `(row=0, col=stride)` 撞同一个值）。把越界列直接折叠成 `None`，既保证唯一性，又顺带给上层一个「这个线程在网格外」的信号。

**练习 2**：`Runtime2DIndex` 为什么不能像 `Index2D<S>` 那样做到编译期安全？

> **答案**：因为步长是运行时值，类型系统看不见它。两个不同 `row_stride` 产出的 `ThreadIndex<'_, Runtime2DIndex>` 类型完全相同，编译器无法区分。唯一的防线是 `index_2d_runtime` 上的 `unsafe` 关键字，把「所有线程必须传同一 stride」变成调用方必须人工审核的契约。

---

### 4.3 DisjointSlice：类型安全的并行写入

#### 4.3.1 概念说明

有了线程唯一的 `ThreadIndex`，再看写入端。`DisjointSlice<T, IndexSpace>` 是一个「**只能用线程局部索引访问、每个线程写不同元素**」的切片。它的内存布局和普通切片一样（`{ ptr, len }`），安全性完全来自类型层：

- 默认访问 `get_mut(idx)` **带越界检查**，返回 `Option<&mut T>`，越界线程拿到 `None`。
- `idx` 必须是 `ThreadIndex<'kernel, IndexSpace>`——而它能被生产出来就保证了「线程唯一」，于是「两个线程同时写同一地址」在类型层不可能发生。
- 切片的 `IndexSpace` 必须和索引的索引空间一致，否则编译失败（4.2 已述）。

对比 vecadd 里 CUDA C 的写法 `if (i < N) c[i] = a[i] + b[i];`，cuda-oxide 把那个手写的 `if i < N` 内建进了 `get_mut` 的 `Option`，把「会不会越界」变成「模式匹配有没有命中」。

#### 4.3.2 核心流程

一次典型的并行写入：

```text
#[kernel] fn vecadd(a:&[f32], b:&[f32], mut c: DisjointSlice<f32>) {
    let idx = thread::index_1d();        // 线程唯一见证 (Index1D)
    let i = idx.get();                    // 降级回 usize, 给只读切片 a/b 用
    if let Some(c_elem) = c.get_mut(idx) {// 越界检查; 越界→None, 跳过
        *c_elem = a[i] + b[i];            // 拿到 &mut, 安全写入
    }
}
```

三种访问方式的对比：

| 方法 | 签名要点 | 越界 | 何时用 |
| --- | --- | --- | --- |
| `get_mut(idx)` | `ThreadIndex → Option<&mut T>` | 检查，越界返回 `None` | 默认，绝大多数场景 |
| `get_mut_indexed()` | `&KernelScope → Option<(&mut T, ThreadIndex)>` | 检查，一次算索引+寻址 | 想同时拿到 `&mut` 和见证；索引空间须实现 `IndexFormula` |
| `get_unchecked_mut(i)` | `usize → &mut T`（**unsafe**） | 不检查 | 性能关键且已用算法保证唯一（如 warp 归约只让 lane 0 写） |

#### 4.3.3 源码精读

`DisjointSlice` 的结构：内部就是裸指针 + 长度，外加两个 `PhantomData` 分别管生命周期和索引空间：

[crates/cuda-device/src/disjoint.rs:L89-L95](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L89-L95) —— 默认 `IndexSpace = Index1D`，所以 `DisjointSlice<f32>` 只接受来自 `index_1d()` 的见证。

```rust
#[repr(C)]
pub struct DisjointSlice<'a, T, IndexSpace = Index1D> {
    ptr: *mut T,
    len: usize,
    _marker: PhantomData<&'a mut [T]>,
    _space: PhantomData<fn() -> IndexSpace>,
}
```

默认安全入口 `get_mut`——注意它**不带 `unsafe`**，安全性论证就在它的文档里：

[crates/cuda-device/src/disjoint.rs:L178-L191](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L178-L191) —— 显式 `i < self.len` 越界检查；`ThreadIndex` 的来源保证线程唯一，于是 `&mut *self.ptr.add(i)` 给每个线程的是不同地址，无数据竞争。

```rust
pub fn get_mut<'kernel>(&mut self, idx: ThreadIndex<'kernel, IndexSpace>) -> Option<&mut T> {
    let i = idx.get();
    if i < self.len {
        Some(unsafe { &mut *self.ptr.add(i) })
    } else {
        None
    }
}
```

`get_mut_indexed` 是「一步算索引 + 寻址」的便捷形式，只对实现了 `IndexFormula` 的索引空间可用（`Index1D`、`Index2D<S>`；`Runtime2DIndex` 不行）：

[crates/cuda-device/src/disjoint.rs:L285-L305](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L285-L305) —— 它吃一个 `&'kernel KernelScope`（由宏在 `slice.get_mut_indexed()` 调用点拼接进去），用 `IS::from_scope` 当场铸造见证，省去用户手写 `index_*()`。

最后是自动 trait 的处理：`DisjointSlice` 显式 `Send`（每个线程拿自己的副本，各写各的），但**故意不实现 `Sync`**：

[crates/cuda-device/src/disjoint.rs:L307-L319](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L307-L319) —— 注释解释：若 `Sync`，多个线程共享同一个 `&DisjointSlice` 并各自 `get_mut`，会产出别名 `&mut T`，违和。模型要求「每个线程一份结构体副本（共享底层指针）」，而非「共享引用」。

#### 4.3.4 代码实践

**实践目标**：用 `get_mut` 写一个免 `if i < N`、免锁的并行写入，并体会它与普通 `&mut [T]` 的区别。

**操作步骤**：

1. 以 [examples/vecadd](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs) 为模板。
2. 新增一个 kernel `scale`：读入 `input: &[f32]`、`factor: f32`，写入 `mut output: DisjointSlice<f32>`：

```rust
#[kernel]
pub fn scale(input: &[f32], factor: f32, mut output: cuda_device::DisjointSlice<f32>) {
    let idx = thread::index_1d();
    let i = idx.get();
    if let Some(out) = output.get_mut(idx) {
        *out = input[i] * factor;
    }
}
```

3. 在宿主 `main` 里分配 `DeviceBuffer`、`kernels::load(&ctx)` 后调用 `module.scale(...)`，`LaunchConfig::for_num_elems(N as u32)`，最后 `to_host_vec` 校验。
4. **关键对比**：把 `output` 的类型从 `DisjointSlice<f32>` 换成 `&mut [f32]`，重新编译，观察它仍能编译（普通切片没这套保护），但你需要**自己**写 `if i < N`。

**需要观察的现象**：用 `DisjointSlice` 时，越界线程自动走 `None` 分支不写；你完全不需要 `if i < input.len()`。换成 `&mut [f32]` 后，没有 `ThreadIndex` 见证，编译器也不再帮你挡越界。

**预期结果**：理解 `DisjointSlice` 把「越界检查 + 写入互斥」合并进了类型与 `Option`，并行写入的正确性由类型保证而非靠人。运行结果**待本地验证**（需要 GPU 或用 `build` 仅验证编译）。

#### 4.3.5 小练习与答案

**练习 1**：`get_mut` 内部那一行 `unsafe { &mut *self.ptr.add(i) }` 为什么是安全的？换句话说，`get_mut` 凭什么不带 `unsafe`？

> **答案**：三道保证合一：(1) 上面刚做过 `i < self.len` 越界检查；(2) `idx` 是 `ThreadIndex`，只能由可信构造器从硬件寄存器算出，每线程唯一，不会有两个线程拿到同一 `i`；(3) `DisjointSlice` 由 `from_raw_parts`/`from_mut_slice` 构造时已保证指向有效内存。三者满足，所以这次解引用无 UB。

**练习 2**：`get_mut_indexed` 相比「先 `index_*()` 再 `get_mut`」有什么实际好处？

> **答案**：它把「铸造见证」和「寻址」合并成一次调用，只算一次索引；同时把「线程在网格外（如 2D 的 `col >= stride`）」和「索引越出切片」两种 `None` 折叠成一个匹配点，控制流更平。代价是只支持 `IndexFormula` 索引空间（不含 `Runtime2DIndex`）。

---

### 4.4 越界检查与 laundering 防护：把不变量推到构造处

#### 4.4.1 概念说明

把前三个模块的安全机制收束成一句话：**cuda-oxide 把「每次访问都安全」的负担，转移成了「只在少数构造点保证不变量」**。这是 Rust 安全封装的经典套路，体现在两个层面：

- **越界（out-of-bounds）**：不在每次访问写 `assert`，而是让 `get_mut` 返回 `Option`，把越界变成 `None`，由模式匹配自然处理。唯一的 unsafe 逃生舱 `get_unchecked_mut` 必须显式写 `unsafe`。
- **洗钱（laundering）**：即「把线程索引存起来、转手、跨线程传递」。cuda-oxide 用 `!Copy + !Clone + !Send + !Sync + 'kernel` 五重绑定，让索引既不能被复制、也不能被搬到别的线程、更不能活过 kernel 函数体。

源码注释把这套称为「The unsafe boundary is pushed to the construction of `DisjointSlice` from raw memory, not to the per-access level」——unsafe 边界被推到了「从裸内存构造 `DisjointSlice`」那一处，而不是「每次访问」那一处。

#### 4.4.2 核心流程

防 laundering 的五重绑定，每一重挡掉一种攻击：

| 攻击方式 | 防线 | 由谁实现 |
| --- | --- | --- |
| `let idx2 = idx;`（复制） | `!Copy + !Clone` | 不 derive Copy/Clone |
| 把 `idx` 塞进 `Arc`/全局变量跨线程共享 | `!Send + !Sync` | `PhantomData<*mut ()>` |
| 把 `idx` 返回出函数、活过 kernel 体 | `'kernel` 生命周期 | `KernelScope` 借自栈局部，宏注入 |
| 用整数伪造一个索引 | 构造器私有 + 桩 `unreachable!` | `ThreadIndex::new` 私有；公共桩不执行 |
| 用错步长喂错切片 | 索引空间进类型 | `Index2D<S>` const 泛型 |

#### 4.4.3 源码精读

先看 `thread.rs` 顶部的 Safety Model，它把整套设计意图写得很清楚：

[crates/cuda-device/src/thread.rs:L19-L42](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L19-L42) —— 第 6 条明确：见证是 `!Send + !Sync + !Copy + !Clone` 且 `'kernel` 作用域内，因此「线程没法通过共享内存洗钱它，它也活不过 kernel 体」。

`KernelScope` 本身也带 `!Send + !Sync` 标记，且构造是 `unsafe`：

[crates/cuda-device/src/thread.rs:L106-L120](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L106-L120) —— `_not_send_sync: PhantomData<*mut ()>`；`unsafe fn new` 私有，只有 `make_kernel_scope` 能调。

唯一允许伪造见证的入口是 unsafe 逃生舱 `get_unchecked_mut`，它的契约要求调用方「用算法证明唯一性」：

[crates/cuda-device/src/disjoint.rs:L224-L233](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L224-L233) —— 只保留 `debug_assert`（release 不检查），安全性完全外包给调用方的算法注释。

最后，`disjoint.rs` 顶部把整套「把 unsafe 推到构造点」的哲学写在模块注释里：

[crates/cuda-device/src/disjoint.rs:L19-L35](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L19-L35) —— 四条访问方式逐级放宽，最宽松的 `get_unchecked_mut` 显式 opt-in。

#### 4.4.4 代码实践

**实践目标**：亲手验证「复制 `ThreadIndex`」会被编译器拒绝。

**操作步骤**：

1. 在一个 `#[kernel]` 函数里写：

```rust
// 示例代码:预期编译失败
#[kernel]
pub fn try_copy_index(mut output: cuda_device::DisjointSlice<u32>) {
    let idx = thread::index_1d();
    let idx2 = idx;            // 试图复制见证
    let idx3 = idx2;           // 再复制一次
    if let Some(o) = output.get_mut(idx3) {
        *o = 42;
    }
}
```

2. `cargo oxide build <名字>`。

**需要观察的现象**：因为 `ThreadIndex` 用到了 `raw: usize`（Copy）和若干 `PhantomData`（也 Copy），单看字段它**可能**满足 `Copy`——但 cuda-oxide **故意没有 derive Copy/Clone**，于是 `let idx2 = idx;` 之后的 `idx` 若再被用就会报「use of moved value」（移动语义）；若你试图在两处都用，会触发借用/移动错误。

**预期结果**：你应当确认「见证不可复制」由「不实现 Copy/Clone」保证，而非运行时检查。具体诊断（是 E0382 move 还是其它）**待本地验证**，取决于 `idx` 是否在移动后被再次读取。

#### 4.4.5 小练习与答案

**练习 1**：如果哪天有人给 `ThreadIndex` 加上 `#[derive(Clone, Copy)]`，安全模型哪里会先崩？

> **答案**：一旦 `Copy`，`let idx2 = idx;` 不再移动而是复制，于是同一个线程可以持有两个值相同的见证；更糟的是可以把它们喂给同一 `DisjointSlice` 的两次 `get_mut`，得到两个 `&mut T` 指向同一地址——立刻产生别名 `&mut`，破坏 4.3 的安全论证。所以「不 derive Copy/Clone」是整套模型的承重墙。

**练习 2**：为什么 unsafe 边界被设计在 `from_raw_parts`（构造点）而不是 `get_mut`（访问点）？

> **答案**：访问点（`get_mut`）在每个 kernel 里被高频调用，若每次都要写 `unsafe` 并论证，既啰嗦又容易论证错；而构造点（从裸内存造 `DisjointSlice`）只在绑定层发生少数几次。把昂贵、易错的 unsafe 审核集中到少数构造点，让上层成千上万次访问都是纯安全代码——这正是「把不变量推到构造处」的工程价值。

---

## 5. 综合实践

**任务**：写一个 `double_buffer_copy` kernel，把 1D 输入复制成两份写到输出，并用本讲学到的全部机制保证安全。

要求：

1. 输出用 `DisjointSlice<f32>`（默认 `Index1D`），输入用只读 `&[f32]`。
2. 用 `thread::index_1d()` 拿见证 `idx`，`idx.get()` 得到 `i`。
3. 用 `output.get_mut(idx)` 写第一个副本（`out = input[i]`）。
4. 第二份副本要写到 `i + N`。**思考**：你不能再直接用 `idx` 去写第二个位置，因为 `get_mut` 会用同一个见证算同一地址。请用 `get_unchecked_mut` 的 unsafe 路径，并在注释里写出你的 SAFETY 论证（为什么 `i + N` 对每个线程仍唯一、且 `< len`）。
5. 在宿主用 `LaunchConfig::for_num_elems(N as u32)` 启动，`to_host_vec` 校验两份副本都正确。

**自检要点**：

- 你应当能解释：为什么第 3 步是安全的（带越界检查 + 线程唯一），而第 4 步必须显式 `unsafe`（绕过了见证-地址绑定，唯一性要靠你的人工论证）。
- 把 `N` 故意设成让 `i + N` 越界的值，观察 `get_unchecked_mut` 在 release 下不报错但结果错误（UB），对比 `get_mut` 在越界时优雅返回 `None`。
- 若本机无 GPU，用 `cargo oxide build` 至少验证编译，并把「能否运行 + 实际输出」标注为**待本地验证**。

这个任务串起了本讲全部最小模块：`ThreadIndex` 见证、`Index1D` 索引空间、`DisjointSlice` 安全访问、以及越界/laundering 边界。

## 6. 本讲小结

- `ThreadIndex<'kernel, IndexSpace>` 是「硬件签发的线程唯一编号」的见证类型：构造器私有、公共桩 `unreachable!`、真正逻辑在 `__internal::index_*`，由 `#[kernel]` 宏改写调用并注入 `KernelScope`。
- 索引方案被编码进类型：`Index1D`（1D 公式）、`Index2D<const ROW_STRIDE>`（步长进类型，stride 用错是编译期错误）、`Runtime2DIndex`（运行时步长的 unsafe 逃生舱）。
- `DisjointSlice<T, IndexSpace>` 用 `get_mut(idx) -> Option<&mut T>` 把越界检查和线程唯一写入合并进类型与 `Option`，安全代码无需手写 `if i < N`、无需锁。
- 防洗钱五重绑定：`!Copy + !Clone + !Send + !Sync + 'kernel` + 私有构造，让索引无法被复制、跨线程搬运或活过 kernel 体。
- 设计哲学：把 unsafe 边界从「每次访问」推到「少数构造点」（`from_raw_parts`、`make_kernel_scope`、`get_unchecked_mut`），上层访问全部安全。
- 已知限制：`index_1d`/`index_2d` 忽略高维（Y/Z 或 Z），仅在 1D/2D 启动下唯一；3D 启动会撞号（issue #115）。

## 7. 下一步学习建议

- **继续设备端主线**：本讲的 `DisjointSlice` 解决「线程间写不同地址」。当线程需要**协作**（先各自写共享内存，再互相读）时，就需要 [u2-l3 共享内存与同步](u2-l3-shared-memory-and-sync.md) 的 `SharedArray` 与 `sync_threads`——它们与本讲的 `ThreadIndex` 经常配合使用。
- **回到宿主端**：若你想知道「`DisjointSlice` 底层那块设备内存从哪来、`get_mut` 的指针如何与 `cuLaunchKernel` 对接」，去读 [u2-l5 设备内存与数据搬运](u2-l5-device-memory-and-data-movement.md) 的 `DeviceBuffer` 与 marshalling。
- **深挖宏改写**：本讲只展示了「自由函数按路径尾改写、方法按名字改写」的结果。若想看 `ThreadIndexCallRewriter` 的完整匹配逻辑与保留名清单，直接读 [cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs) 中 `SCOPED_METHODS` 与 `ThreadIndexCallRewriter` 附近（约 L2078–L2198）。
- **练习建议**：把综合实践里的 kernel 改成 2D 版本（用 `index_2d::<N>()` 与 `DisjointSlice<f32, Index2D<N>>`），巩固本讲 4.2 的「步长进类型」。
